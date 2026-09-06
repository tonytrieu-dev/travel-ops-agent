# Decisions

Load-bearing choices, each with the alternative and why it was rejected.

## Why this stack, as a whole
FastAPI + Postgres/SQLModel + Pydantic AI (Cerebras-hosted `gpt-oss-120b`) + React, chosen together
for a relational application that calls replaceable external providers. Trips own flight results,
itineraries, and booking logs, while booking-state changes need transactions and row locking.
Postgres was the one piece of infrastructure that could serve both the app's data model and
DBOS's durability layer without standing up a second system (see "PostgreSQL over a NoSQL store"
below). FastAPI + Pydantic AI were picked for the same reason as the LLM host itself: Pydantic AI
is a swappable provider layer,
not a wrapper around one vendor's SDK; that boundary allowed the model host to change without
rewriting the planner (see "Cerebras-hosted open-weight model" below). The entries below document
the narrower trade-offs behind each remaining dependency.

## PostgreSQL over a NoSQL store
The datastore is Postgres 16, not a document/NoSQL store (MongoDB, DynamoDB, Firestore).
**Alternative:** a document store. **Rejected** — this system's data is relational (a trip owns
flight results, an itinerary, and a booking log; a booking owns its transition history; an agent
run owns its steps), and the booking FSM uses `SELECT ... FOR UPDATE` plus an audit write in the
same transaction (see "HITL booking is a REST state machine" above). The append-only audit tables'
`BEFORE UPDATE/DELETE` trigger and the `NOT NULL`
constraint on age/fitness (see "Age and fitness level are mandatory intake fields" above) are both
database constraints used by this implementation. **Chosen because:** Postgres fits the structured,
transactional data and lets DBOS reuse the same instance in its own
`dbos` schema instead of standing up a second database just for durability (see "DBOS for durable
execution" below). Hosted Postgres remains a deployment option, but no provider or service tier is
assumed by the architecture.

## Dependency Injection over constructing dependencies inline
DB sessions and external clients (`FlightProvider`, `ActivityProvider`, the booking-options
fetcher) are injected — via FastAPI `Depends` in routes, via constructor/dataclass args
(`PlannerDeps`) in the agent — never instantiated inside the function that uses them.
**Alternative:** construct them where needed (e.g. `LiveSearchApiProvider()` directly inside a
route or the planner tool). **Rejected** — that couples every call site to one concrete
implementation, so testing the booking state machine's concurrency (`test_double_execute_books_once`)
or the planner's tool-calling loop would require mocking internals instead of handing in a real
fake object that behaves like the thing it replaces.

## Repository pattern over inline queries in routes
All Postgres access lives in `app/repositories/*` (`trips_repository.py`, `booking_repository.py`);
route handlers call a repository function and shape the HTTP response, never build a query or own
a transaction boundary directly. **Alternative:** query the ORM directly from route handlers, the
FastAPI default. **Rejected** — the state machine's guard-clause-plus-audit-write pattern
(see below) has to be atomic and consistent everywhere a transition happens; centralizing DB access
in one layer is what makes "every transition writes an audit row in the same transaction" a
property of the repository, not something each route has to remember to do correctly on its own.

## HITL booking is a REST state machine, not an agent tool
The booking write moves through `PENDING → CONFIRMED → EXECUTED` (or `CANCELLED`/`EXPIRED`) via
explicit `/bookings/*` calls driven by human clicks. **Alternative:** expose booking as an agent
tool gated by an approval prompt. **Rejected** because a prompt-gated tool makes "a human
confirmed first" a prompt-dependent hope; a state machine outside the agent makes it structural —
the agent has no tool that can move booking state.

## Agent output is a union: `Itinerary | ClarificationOut`
A genuinely ambiguous input (e.g. a destination name that could mean more than one place) can
produce clarifying questions instead of an itinerary. **Alternative:** always return an itinerary and
let the prompt beg the model to ask. **Rejected** — "ask, don't assume" as a type is enforced by
validation; as prose it's optional. Age/fitness level used to be the main trigger for this path
until they became mandatory at trip intake (see the "mandatory intake fields" note below) — the
union stays for whatever's still genuinely ambiguous.

## Age and fitness level are mandatory intake fields
`TripRequestCreate.age`/`.fitness_level` are required, not optional-then-clarified. **Alternative:**
keep them optional and let the agent's `ClarificationOut` path ask when missing (the original
design). **Rejected** — every itinerary needs them to pace activities, so the clarify-then-resubmit
round trip was required whenever either field was missing; validating at intake removes it.
`TripRequest.age`/`.fitness_level` are now `NOT NULL`
in the DB too (migration `12c1788c`) — a nullable column let a handful of legacy rows carry no
age/fitness, and `reject_optional_clarification` blocks any clarifying question that mentions
"age"/"fitness" once other trip details are present, so a genuinely-null row trapped the model
asking for the one thing it wasn't allowed to ask about, burning all 3 output retries. Closing the
nullability gap fixes that structurally instead of special-casing the validator. The migration
backfills legacy rows that have audit history and removes legacy rows that do not.

## Flight search is its own user-facing capability, not gated behind full itinerary generation
`POST /api/trips/{trip_id}/flights/search` lets a user look up flight offers for a trip directly,
independent of `POST /api/trips/{trip_id}/plan`. **Alternative:** only expose flight search as the
agent's internal `search_flights` tool, reachable solely by triggering full itinerary generation.
**Rejected** —
flights and itineraries are different asks with different costs: a flight lookup is one
deterministic SearchApi call, while planning drives the whole agent loop (Cerebras reasoning,
`web_search`, output validation) — slower, and it spends real, rate-limited LLM quota
(`MAX_CONCURRENT_AGENT_RUNS` caps concurrent *LLM* calls specifically; see `app/rate_limit.py`, and
note the flight-search route does not compete for that slot the way the planning route does).
Forcing a user who just
wants prices through the full planning path would burn that scarce budget on a task that never
needed an LLM at all. **Reflected in the two callers' cache behavior, not just the route split:**
the direct route persists results and reuses them across trips (`persist=True,
allow_cross_trip_cache=True`); the planner's tool call is deliberately more conservative
(`persist=False, allow_cross_trip_cache=False`), since it's an internal grounding step inside one
planning run, not a user-facing catalog browse — see the entry right below for where that shared
logic actually lives.

## Flight search and execution-run lifecycle are extracted services, not inline route/tool logic
`FlightSearchService` (`app/services/flight_search.py`) and `ExecutionService`/`ExecutionRun`
(`app/agent/execution_log.py`) sit behind `POST /api/trips/{trip_id}/flights/search`, the
planner's `search_flights` tool, and the DBOS-wrapped planner run. **Alternative:** leave the
caching, persistence, and ordering logic inline in `routes/trips.py` and `agent/planner.py`, as it
originally was. **Rejected** — the
route and the planner tool need the *same* cheapest-first/cache/round-trip-completeness behavior
(same-trip TTL reuse, cross-trip identical-search reuse, explicit unavailable results) and had
drifted into two near-duplicate implementations; one service parameterized by
`persist`/`allow_cross_trip_cache` is the single place that logic can be verified once
(`test_route_and_planner_tool_modes_agree_on_offer_ordering_and_shape` pins the two callers can't
silently diverge again). Same reasoning for `ExecutionService`: before the extraction, the DBOS
workflow and its failure-path cleanup had two separate ways to finalize an `AgentRun`
(`persist_agent_run` called directly from two branches); `ExecutionRun.persist_result` is now the
one path, so `_persist_failed_run` and the success path can't fall out of sync on what "finalized"
means. Both extractions were scoped to change no observable behavior — the full plan and
task-by-task TDD trail live in
`docs/superpowers/plans/2026-07-24-flight-search-execution-services.md`.

## The agent has only two read-only tools
Only `search_flights` and `web_search` are registered on the planner, both with strict JSON
schemas. Booking remains outside the agent as the REST state machine above, so the model has no
write tool to invoke.

## Audit tables are append-only at the database
`booking_transition` and `execution_event` have `BEFORE UPDATE/DELETE` triggers that raise.
**Alternative:** enforce immutability in application code. **Rejected** — app-level convention is
one bug away from a silent tamper; the DB trigger holds regardless of the code path.

## DBOS for durable execution, crash-recovery only
The planner run and booking execute are `@DBOS.workflow`s reusing the app's Postgres. Deliberately
**no** DBOS-level dedup on top of the existing `SELECT ... FOR UPDATE` claim — one mechanism, one
job (DBOS = crash recovery). **Alternative:** add `SetWorkflowID` dedup too. **Rejected** as
redundant with the tested atomic claim.

### Planner-loop external calls are wrapped as individual DBOS steps
`agent.model.request`, `FlightProvider.search_offers`, and `ActivityProvider.search` are each
patched with `@DBOS.step` at the point they're used, not left as plain calls inside the
workflow body. **Why:** without this, a crash-recovery replay re-runs the whole workflow body
from scratch, re-issuing an already-completed Cerebras call or search/activity call and paying
for it twice. **Why free functions, not the bound methods directly:** `DBOS.step`'s decorator
`setattr`s registration metadata onto its target, and bound methods don't support arbitrary
attribute assignment — `_as_durable_step` wraps each bound method in a plain function instead.
**Why patched at the instance, not via a second agent/provider class:** the model is a shared,
module-level singleton reused by every non-DBOS caller (tests, evals); `@DBOS.step` no-ops to a
plain call outside workflow context, so patching the singleton once at import is safe for those
callers too, and cheaper than building a parallel `Agent`. The two providers are already built
fresh per run inside `_run_planner_workflow`, so patching each instance right after construction
can't double-wrap anything.

### The concurrency slot lives *outside* the DBOS workflow body
`run_planner_durable` acquires the concurrency slot, then calls the `@DBOS.workflow`. The slot is
plain in-process state (a lock-guarded counter). **Why outside:** DBOS's record/persist machinery
re-enters the workflow body during replay, so mutating in-process state *inside* it double-counts
(observed: one acquire showed as two). Keeping the slot in the plain outer function is the fix the
[ARCHITECTURE](ARCHITECTURE.md) durable-execution section refers to. Related: the non-blocking
acquire uses a lock-guarded counter, not `asyncio.wait_for(sem.acquire(), timeout=0)`, which can
spuriously time out even uncontended.

## Cerebras-hosted open-weight model
The planner uses `gpt-oss-120b` through Pydantic AI's `CerebrasModel`. The choice met the project's
development-budget and context-window needs when it was selected. Pydantic AI keeps provider-specific
construction at the application boundary, and the earlier Groq-to-Cerebras change did not require
rewriting the planner or its tools.

This is not a benchmark claim that an open-weight model or Cerebras is generally better than a
proprietary model or another host. Model quality, structured-output behavior, rate limits, pricing,
and support need to be evaluated for the intended deployment. Those provider conditions can change
independently of this repository.

## Cerebras over Groq (over Gemini)
Cerebras runs `gpt-oss-120b` directly through Pydantic AI's native `CerebrasModel`/
`CerebrasProvider`. The model name lives in `config.py::CEREBRAS_MODEL`, and the app reads
`CEREBRAS_API_KEY` from settings. **Alternative 1:** Groq, also serving `gpt-oss-120b`.
**Rejected** — the available Groq account returned HTTP 413 rate-limit errors during multi-tool-call
runs, while the available Cerebras limits allowed the tested itineraries to complete. These are
observations from development, not current plan guarantees. **Alternative 2:**
`llama-3.3-70b-versatile`. **Rejected** — it emits its native `<function=...>` text format instead
of JSON tool calls, which Pydantic AI can't parse.

## SearchApi.io over Amadeus/Duffel/Skyscanner/raw scraping, for flights
`flights_searchapi.py` calls SearchApi.io's Google Flights engine. **Alternative 1:** Amadeus or
Duffel. **Not selected:** their access and product scope did not fit the project's account and
budget constraints at selection time. **Alternative 2:** scrape Google Flights directly.
**Rejected:** markup is not a stable application interface. **Chosen because:** SearchApi returns
structured Google Flights responses, including ranked flight arrays. The application still applies
its own `cheapest_first` ordering rather than relying only on provider order. Provider access,
pricing, response shape, and ranking behavior must be rechecked before deployment.

## Tavily over Serper/Bing/SerpAPI/Google Custom Search, for activity research
`activities_tavily.py` calls Tavily for the itinerary's activity research. **Alternative:**
general-purpose search APIs (Serper, Bing Search API, SerpAPI, Google Custom Search), which expose
different result shapes and would require a different adapter. **Chosen because:** Tavily returns
content and a source URL per result in a shape the `web_search` tool can pass to the model. The
output validator checks URL attribution only; it does not independently verify the activity text.

## Provider data and explicit degradation
The flight adapters normalize provider or recorded-fixture responses; they do not synthesize
fallback offers. On quota, rate-limit, or empty responses they return cached provider data when
available or an `unavailable_reason`. Activities are generated by the model from search results,
so their source URLs provide attribution rather than independent factual verification. Booking-options fetches
(`departure_id`/`arrival_id`/`outbound_date` forwarded alongside `booking_token`, all derived from
the flight's stored `raw_offer`) work end-to-end for one-way and round-trip alike. Round-trip
offers store a `departure_token`, not a real `booking_token` (see `_parse_offers`); resolving it
costs one extra SearchApi call (`_resolve_return_booking_token`) that fetches the return-leg
options and picks the cheapest — the current UI has no separate return-flight-selection step, so
this is the same cheapest tie-break the rest of the app already uses. Any failure in that
resolution returns no booking links, matching the rest of the booking-options failure path.

## Custom Slack HITL adapter over chat-sdk-python
`app/adapters/slack_hitl.py` hand-rolls signature verification (stdlib `hmac`/`hashlib`) and Block
Kit message building for one outbound POST and one signed callback. **Alternative:**
a multi-platform chat SDK. **Rejected:** the current integration needs one Slack message and one
signed callback, so adding a broader connector abstraction would increase the dependency and
configuration surface without serving another implemented connector. The
`notify_pending_approval`/`resolve_approve`/`resolve_reject` boundary keeps Slack-specific code
isolated if another connector is added later.

**Local dev needs a public callback URL.** Slack's Interactivity config can't POST to
`localhost`, so `POST /api/slack/interactions` has to be reachable from the internet even during
local development. `ngrok http 8000` is the tunnel used for this (see `docs/SLACK_SETUP.md`) —
no code depends on ngrok specifically, any HTTPS tunnel pointed at port 8000 works the same way.

## Slack approve only confirms; execute stays in the frontend
The Slack callback (`routes/slack.py`) calls only `confirm_booking`/`reject_booking`, never
`execute_booking`. **Alternative:** let Approve in Slack also execute the booking.
**Rejected** — Slack requires an ack within 3 seconds, and execute calls SearchApi and can take up
to `SEARCHAPI_TIMEOUT_SECONDS`, well past that budget; a fire-and-forget execute call from the
Slack handler would leave Slack with no reliable way to report back whether it actually succeeded.
Execute stays a synchronous action behind the frontend's existing Execute button, where the UI
already discloses ("your flight hasn't been purchased") what execute does and doesn't do.
**What this means for the approver:** clicking Approve in Slack records the human-in-the-loop
decision — it moves the booking `PENDING_USER_CONFIRMATION → CONFIRMED` — but it doesn't hand back
checkout links; whoever approved still has to open the app and click Execute to get them. That's a
two-step, two-surface flow. **Possible extension:**
ack Slack immediately, run execute as a background job, and post a follow-up Slack message with the
checkout links once it completes.

## Connector enablement is a DB-backed toggle, not just an env var
`connector_setting.slack_enabled` is a single-row table flipped via `/api/connectors`, separate
from whether Slack credentials exist in settings. **Alternative:** treat "credentials present" as
"enabled." **Rejected** — that collapses configuration and intent into one flag, so any deployment
with the env vars set would silently start posting to Slack; the toggle lets an operator configure
Slack once and still flip it off at runtime without a restart or an env change.

## Trip history is a list endpoint plus a global execution feed, not per-trip-only
`GET /api/trips` lists every trip the (single demo) user owns, newest first; `GET /api/execution`
mirrors that scoping across every `AgentRun` those trips own, also newest first — both live in
`trips_repository.py` next to the existing per-trip `get_trip`/`get_execution_panel`, sharing the
same user-scoping query rather than introducing a second concept. **Why global execution, not just
per-trip:** the frontend used to require a trip to be "active" (in `localStorage`) before its
execution history was visible at all — switching trips, or never having picked one, hid the tab
entirely. A tab that only ever shows one trip's history can't answer "what has this agent done,
period," which is the actual job of an execution/observability view. **Alternative considered:**
keep it per-trip and add a trip switcher. **Rejected** — that still frames execution history as a
property of one trip instead of an audit trail, and duplicates the trip-switching UI the new
"Your trips" tab already provides. **Filtering is client-side, not query params:** both the route
search and status filter on the execution tab, and the date-range filter on "Your trips," filter
the already-fetched list in the browser rather than adding query parameters to either GET. At this
scale (one user, dozens of trips) a server-side filtered query is speculative infrastructure;
revisit if the trip count grows enough that shipping every trip/run to the client stops being
cheap. The one thing kept server-side either way: user-scoping, since that's a security boundary,
not a display preference.

## Rate limiting protects scarce third-party quota
`enforce_request_rate_limit` (`app/rate_limit.py`) applies a per-IP request cap plus a process-local
concurrency cap on real LLM calls, gating `POST /api/trips/{trip_id}/plan` and
`POST /api/trips/{trip_id}/flights/search`. **Alternative:** no
limiting, rely on each provider's own rate-limit response. **Rejected** — a burst of retries
(accidental double-clicks or a buggy client) can consume provider quota before the upstream limit
responds. This limiter reduces that risk for one running process; it resets on restart and is not
a distributed quota-control mechanism.

## Current scope and deferred work
HITL booking is a checkout-link handoff, not a purchase. The optional Slack connector supports one
workspace and one approval interaction rather than a general chat-platform abstraction.

Not implemented: authentication, multi-user isolation, payment processing, episodic/semantic/
procedural agent memory, or Saga compensation for a multi-step airline booking. Some code boundaries
could support those changes, but their implementation effort and behavior have not been validated.
