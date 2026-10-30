# TravelOps Agent

A bounded **zero-trust application prototype**, using a stateful travel planner as
the protected resource. It demonstrates user/session verification, role and tenant
access policy, privileged MFA, recorded security decisions, and automatic session
containment. It is a first implementation toward the enterprise-network project
objective; device authentication and network micro-segmentation remain gaps.

The planner searches flights and destination activities and proposes an itinerary.
Booking approval and execution stay outside the model's tools, behind application
policy and a human-approval state machine. The system returns checkout links;
it does not purchase flights.

The [architecture findings](docs/ZERO_TRUST_FINDINGS.md) preserve the baseline,
before/after evidence, design limits and three-scenario security demonstration.

## Zero-trust architecture

Zero-trust enforcement is **on by default** — every protected API route requires a short-lived
bearer token, with no toggle to flip first. The local issuer uses custom HMAC-signed tokens,
passwords, and TOTP; it does not implement OIDC discovery or standard OIDC token validation.
With no `ZERO_TRUST_SIGNING_SECRET` set, the app generates a fresh per-process signing key; set an explicit secret
for a deployment whose tokens must survive a restart. Production should put Keycloak or another
OIDC provider in front of the API and use mTLS/service-mesh identity for internal traffic.

The SPA signs in through `POST /api/auth/login`, submits the returned session to
`POST /api/auth/mfa`, stores the resulting 15-minute bearer token in browser storage, and attaches it
to every protected request. Set `ZERO_TRUST_SIGNING_SECRET` and use the seeded demo credentials or a
local user; MFA-enabled users must complete the TOTP step. Connector settings are deployment-wide,
not tenant-owned, and both reading and changing them require the `security-operator` or `admin` role
plus verified MFA.

Slack approval callbacks and notifications are disabled while enforcement is enabled, even
with credentials configured. Slack users are not mapped to trip owners or verified application
sessions; approve and cancel bookings in the app. The legacy Slack flow is development-only.

Device IDs demonstrate consistency between submitted and stored identity attributes; they do not
prove device possession or health. Segment checks enforce an application boundary, without
network segmentation or workload authentication. See the [trust boundaries](docs/ARCHITECTURE.md#zero-trust-enterprise-network-overlay).

## Start here

- [Architecture](#architecture) — the system boundary and critical request path.
- [Zero Trust control plane](docs/ARCHITECTURE.md#zero-trust-enterprise-network-overlay) — how the
  five required areas (identity and access management, MFA, segmentation, continuous monitoring,
  incident response) map to concrete controls, and what is deliberately out of scope.
- [Evaluation results](#evaluation-results) — 84/84 deterministic assertions passed in a 12-run
  sample; the optional quality judge passed 11/12.
- [Zero-trust demo](#zero-trust-demo) — deterministic identities, MFA, attacks, containment, and
  security-operations queries.
- [Engineering decisions](docs/DECISIONS.md) — deeper rationale and trade-offs.
- [Security evaluation](docs/SECURITY_EVALUATION.md) — PostgreSQL validation, repeatable scenarios,
  and a modest local latency comparison.

There is no public hosted demo yet. Run the local stack below, or use the walkthrough as a short
recorded demo script. The project intentionally does not present itself as a production travel
service: it does not purchase flights and has not had production-scale load testing or an
independent security audit.

## Zero-trust demo

```bash
docker compose up -d postgres
cp .env.example .env
# Set DATABASE_URL in .env to match the Docker service:
# postgresql+asyncpg://travel_agent:travel_agent@localhost:5432/travel_agent
# Enforcement is already on by default; optionally set ZERO_TRUST_SIGNING_SECRET for stable tokens.
cd backend
uv run alembic upgrade head
uv run python -m scripts.seed_security_demo
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Keep that API terminal running. In a second terminal from the repository root:

```bash
cd backend
uv run python -m scripts.security_demo
```

For an existing account left with an empty password hash by the security migration,
run `uv run python -m scripts.enroll_identity --email <existing-account-email>` from
`backend`. A trusted local database operator must run this command. It prompts for
and confirms a chosen password without placing it in command arguments. It refuses
unknown, disabled, or already-enrolled accounts and preserves identity, trip ownership,
device and MFA settings. It is enrollment, not a password-reset endpoint. Existing
MFA enrollment must still be available to the owner; the migration's shared demo
TOTP secret is suitable only for local synthetic accounts.

The [architecture findings and mini use case](docs/ZERO_TRUST_FINDINGS.md) preserve
the baseline, implemented changes, before/after evidence, and outstanding review issues.

The synthetic accounts all use `demo-password` and the TOTP secret
`JBSWY3DPEHPK3PXP` (the script calculates the current six-digit code). The demo shows a valid
tenant-scoped request, MFA-gated operator access, token/session rejection,
browser segment spoof rejection, denials triggering session revocation, and the
security-operations event/incident query. These are throwaway demo credentials; never reuse them.

For the repeatable PostgreSQL demonstration, including authorized booking, cross-tenant denial,
tampered tokens, revocation, and completed planner exchanges retained after a later model failure, follow
the [evaluation commands](docs/SECURITY_EVALUATION.md). That harness uses isolated test data and
stubbed external providers; it does not purchase flights or consume model/search quota.
Booking-options provider-failure preservation has no regression test and is not a validated guarantee.

## Security demonstration

Use three scenarios to show the bounded prototype:

1. **Legitimate access:** a traveler completes MFA and reads an owned trip. A
   password-only session cannot approve a booking.
2. **Unauthorized access and containment:** a tenant-A traveler attempts to read
   tenant B's booking. Five denied reads trigger session revocation; the next
   protected request with that token returns 401.
3. **Operator evidence:** an MFA-verified operator inspects the linked incident
   and assessment evidence. Show pass, fail and insufficient evidence honestly;
   a database-owner application role should fail the audit-protection assessment.

The [findings document](docs/ZERO_TRUST_FINDINGS.md#presentation-scope-and-three-scenario-demonstration)
maps these scenarios to actual tests and explains the before/after design. With
an isolated PostgreSQL test database prepared as described in
[Security evaluation](docs/SECURITY_EVALUATION.md#repeat-the-validation), run from `backend`:

```bash
uv run pytest -q tests/test_security_integration.py tests/test_compliance.py
```

These tests use synthetic accounts and stub external providers. They do not
purchase flights or spend model/search quota. The standalone HTTP script above
is a smaller missing-resource walkthrough; it does not cover the full
cross-tenant assessment scenario. Browser rehearsal remains separate work.

## Architecture

```mermaid
flowchart LR
    UI[React frontend] -->|REST /api| API[FastAPI]
    API --> Repo[Repositories]
    Repo --> DB[(Postgres 16)]
    API --> Agent[Pydantic AI planner]
    Agent -->|read-only tools| Providers[Flight and activity providers]
    Agent --> Cerebras[(Cerebras gpt-oss-120b)]
    DBOS[DBOS workflows] -.->|checkpoint and replay| Agent
    DBOS -.->|durable booking execute| Repo
    DBOS --> DB
    API -->|development only; enforcement off| Slack[Legacy Slack approval connector]
    Slack -->|signed callback; enforcement off| API
```

The critical boundary is deliberate: the LLM researches and proposes; deterministic application
code owns booking state, durability, and accountability.

```mermaid
sequenceDiagram
    participant Traveler
    participant UI as React UI
    participant API as FastAPI
    participant DBOS
    participant Agent
    participant Providers
    participant DB as Postgres

    Traveler->>UI: Submit trip details
    UI->>API: POST /api/trips/{id}/plan
    API->>DBOS: Start durable planner workflow
    DBOS->>Agent: Run bounded tool loop
    Agent->>DBOS: Durable search step
    DBOS->>Providers: Call provider if not checkpointed
    Providers-->>DBOS: Search results
    DBOS->>DB: Persist checkpoint and execution event
    Agent-->>DBOS: Itinerary or clarification
    DBOS->>DB: Persist result and completed run
    DBOS-->>UI: Render result and trace
    Traveler->>UI: Approve selected flight
    UI->>API: POST /api/bookings/{id}/confirm
    API->>DB: Lock, validate, append audit row
    DB-->>UI: Confirmed with actor and timestamp
```

### Why DBOS durability matters

The planner and booking execution are `@DBOS.workflow`s backed by the same Postgres instance.
The planner's Cerebras completion, flight search, and activity search are individual durable
steps. If the process crashes, DBOS replays the workflow from its checkpoints and reuses completed
step results instead of charging the external provider again. This is crash recovery, not a claim
of exactly-once behavior across every provider.

## Approval and audit trail

Booking is a REST finite-state machine, not an agent tool:

`PENDING_USER_CONFIRMATION → CONFIRMED → EXECUTED`

Cancellation and fare expiry are terminal paths. Every legal transition is validated under a
`SELECT ... FOR UPDATE` lock and writes a `booking_transition` row in the same transaction. The
audit table is append-only at the database level: a Postgres trigger rejects updates and deletes.
The UI's **Approval history** tab shows the booking, route, state transition, human actor, reason,
and timestamp. Automatic expiry is distinguishable from a human decision because it has no actor.
With enforcement disabled for local development, the legacy Slack connector can confirm or reject
through the same state machine. Enforcement disables this path because a Slack identity is not an
authenticated application identity and cannot prove ownership, role, or MFA.

## Evaluation results

The eval suite uses four cases crossing age (`24`, `78`) and fitness (`low`, `high`) on the same
route, with recorded provider data and a live planner model. It checks output type, source URL
grounding, tool-call trajectory, flight exclusion, low-fitness safety, and physical-load
comparisons.

| Run | Result |
|---|---|
| Deterministic evals | 84/84 assertions passed across 12 case-runs (100%) |
| Optional `FitnessAppropriateness` judge | 11/12 passed |
| Judge finding | One high-fitness/older case was too conservative despite passing numeric load checks |

The judge result is intentionally reported rather than hidden: deterministic checks establish
hard invariants, while pacing and suitability still need subjective review.

## Failure and recovery examples

- **Stale fare:** after the 30-minute freshness window, approval or execution returns `EXPIRED`,
  records a system transition, and the UI asks the traveler to search again.
- **Planner over budget:** tool, token, and request limits bound the run; a `UsageLimitExceeded`
  result becomes `PlanTooComplexOut` instead of an unbounded loop.
- **Provider failure:** flight and activity providers are behind strategies, so recorded fixtures
  support repeatable tests and live providers can fail without leaking provider-specific code into
  the planner.
- **Process crash:** DBOS resumes checkpointed planner or booking work from Postgres. Persisted
  execution events and run rows remain available after restart.

The detailed implementation notes remain in [ARCHITECTURE.md](docs/ARCHITECTURE.md),
[EVALS.md](docs/EVALS.md), and [DECISIONS.md](docs/DECISIONS.md).

## Project status and limitations

Security monitoring now includes recurring MFA, audit-protection, and session-containment checks.
See [implementation, findings, and demonstration steps](docs/COMPLIANCE_MONITORING.md).

This is a portfolio and reference implementation intended for local development and evaluation.
It is not a production travel service; testing includes only modest local load, with no independent
security audit. The local password/TOTP issuer and tenant/owner checks are bounded demonstration
controls, not a production identity or device-management system. Activity citations show that a URL came from the
configured search provider; they do not independently verify every generated claim. The booking
flow returns third-party checkout links and does not reserve, purchase, or hold a fare.

See [Operational boundaries](docs/EVALS.md#operational-boundaries) for the detailed runtime,
security, recovery, and integration limitations.

## What it does

1. **Plan a trip** — origin, destination, dates, age, and fitness level are all required at
   intake. It can still ask a clarifying question if a provided value is ambiguous.
2. **Search flights** — SearchApi.io Google Flights results in live-provider mode, with
   route-and-date caching; recorded fixtures are available for repeatable tests and evals.
3. **Generate an attributed itinerary** — the agent researches activities through Tavily and
   returns a day-by-day plan. Each activity must cite a URL returned by that run's web search.
4. **Human-approved booking handoff** — review a proposed flight, explicitly approve it, then
   retrieve real checkout links, as three separate steps. Nothing here books a flight. Approval
   unlocks a deterministic, audited workflow whose output is airline/OTA checkout links with the
   chosen flight already attached; you complete the purchase on the carrier's own site, and the
   reference this app stores is its own audit id, not an airline confirmation number. Because no
   fare is actually held, a 30-minute freshness window guards against handing you a stale price:
   approving or executing past it marks the booking `EXPIRED` and asks you to search again.
5. **Watch the agent work** — an execution panel shows every run's tool calls, token usage,
   context-budget utilization, and timing. It's global across all of your trips (filterable
   by route and status), not just the one you're currently planning. A separate "Approval history"
   tab shows the other half of the audit trail: every human decision on a booking — who approved
   what and when — read straight from the append-only `booking_transition` table.
6. **Revisit any past trip** — a "Your trips" tab lists everything you've created, newest first
   and filterable by date range. Trips remain in Postgres until their rows are removed.

## Stack

- **Backend:** FastAPI, Pydantic AI, SQLModel/asyncpg, PostgreSQL 16, Alembic, DBOS (durable
  workflow execution, reuses the same Postgres instance).
- **LLM:** Cerebras `gpt-oss-120b` via Pydantic AI.
- **Flights:** SearchApi.io Google Flights structured responses, or recorded fixtures.
- **Activities:** Tavily web search.
- **Frontend:** React 19 + Vite + Tailwind CSS v4, TypeScript. A structured trip form drives the
  agent; a live activity feed streams its tool calls inline on the trip page, and a separate
  execution panel shows the full run trace across every trip. A "Your trips" tab lists every trip
  persisted for the demo account.
- **Evals:** `pydantic-evals` — deterministic scoring by default, with optional LLM-judged
  fitness-appropriateness scoring, separate from the pytest suite that gates system correctness.

Live-provider operation requires accounts and API credentials for the configured services.
Availability, pricing, and quotas are controlled by those providers and can change. For why each
provider was selected, see [DECISIONS.md](docs/DECISIONS.md).

## Running it

### 1. Database

Either Docker or a local Postgres install works.

```bash
# Docker
docker compose up -d
```

or, if you'd rather run Postgres natively (e.g. via Homebrew on macOS), just make sure a
`travel_agent` database exists and matches the `DATABASE_URL` you set in `.env` below.

### 2. Environment

```bash
cp .env.example .env
```

Fill in `CEREBRAS_API_KEY`, `SEARCHAPI_API_KEY`, and `TAVILY_API_KEY` (links to get each one are
in the file's comments). Adjust `DATABASE_URL` if you're not using the Docker default.

### 3. Backend

```bash
cd backend
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
```

Backend serves on `http://localhost:8000`; interactive docs at `/docs`.

### 4. Frontend

```bash
cd frontend
npm install
npm run dev
```

Frontend serves on `http://localhost:5173` (already whitelisted by the backend's CORS config).

### 5. Tests

```bash
cd backend
uv run pytest -q
uv run pyrefly check
# The suite needs PostgreSQL; the subset that does not is marked and can run without it:
uv run pytest -q -m no_database
```

The zero-trust decision path (identity-claim verification, role/MFA policy, tenant ownership,
browser segment spoofing, denial-threshold containment) is exercised against a live PostgreSQL
instance by `tests/test_security.py`; the append-only audit guarantee is enforced by a database
trigger and checked in `tests/test_append_only.py`.

### 6. Evals

```bash
cd backend
uv run python -m evals.run --repeat 3
# Optional Gemini judge:
uv run python -m evals.run --with-judge
```

Scores the agent against a small dataset: do activity URLs come from the recorded search results,
does the run follow the expected tool-call trajectory, and do low-fitness cases avoid activities
labeled `high` intensity? These checks cover specific output properties; they do not establish
overall itinerary correctness or traveler safety. The default suite is deterministic;
`--with-judge` opts into the Gemini `FitnessAppropriateness` evaluator. The planner model call is
live in both modes, so running evals consumes Cerebras quota.

### 7. (Development only) Legacy Slack human-in-the-loop approvals

Only when `ZERO_TRUST_ENFORCED=false`, booking approval can be routed through Slack instead of the
in-app UI. Slack's Interactivity callback needs a public HTTPS URL, so local development requires
a tunnel:

```bash
ngrok http 8000
```

Use the printed `https://*.ngrok.io` URL as the Slack app's Request URL. Full setup steps
(app creation, tokens, channel config) are in [`docs/SLACK_SETUP.md`](docs/SLACK_SETUP.md).
Nothing here is required for the app to run. Under zero-trust enforcement the Connectors tab keeps
the Slack toggle unavailable even when credentials are configured, and booking approval stays
in-app.

## Key decisions

The reasoning behind the REST state machine, typed outputs, provider failure handling, DBOS
workflows, append-only audit tables, request limits, and provider choices is documented in
[docs/DECISIONS.md](docs/DECISIONS.md).
