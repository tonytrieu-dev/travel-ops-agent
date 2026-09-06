# Evals

`backend/evals/` (`dataset.py`, `evaluators.py`, `run.py`) tests the planner agent's behavior, not
just its code. The default suite contains deterministic evaluators only. Run it like a test suite
(`uv run python -m evals.run --repeat 3`, alongside `pytest`) before considering a change to the
agent, prompt, or tools done.

`FitnessAppropriateness` is optional because it uses an LLM judge. Include it explicitly with
`uv run python -m evals.run --with-judge` (and add `--repeat 3` when repeat runs are wanted).

## Why evals, not just unit tests

Unit tests check that functions return what they're supposed to given fixed input. The planner
agent doesn't have that property: the same prompt can legitimately produce different tool-call
sequences and itinerary wording across runs, because an LLM sits in the loop. A unit test that
asserts on exact output would be flaky by construction. Evals instead assert on selected
properties that should hold regardless of exact wording — source-URL attribution, tool-call
trajectory, output shape, and intensity-label rules — and can run each case multiple times
(`--repeat 3`) so a pass isn't one lucky sample.

## Exact evaluation vs. subjective evaluation

Every evaluator in `evaluators.py` falls into one of two families, and the split is deliberate:

| | Exact (objective) | Subjective |
|---|---|---|
| **What it checks** | Deterministic, verifiable outcomes | Quality that doesn't reduce to a rule |
| **How** | Plain code against the recorded tool-call trace / output shape | An `LLMJudge` (`gemini-3.6-flash`, `GEMINI_JUDGE_MODEL` in `app/config.py`) scoring against a written rubric |
| **This project's evaluators** | `OutputTypeMatches`, `CitationGrounding`, `NoFlightActivities`, `FlightSearchTrajectory`, `WebSearchTrajectory`, `LowFitnessSafety`, `PhysicalLoad`/`PhysicalLoadComparisons` | `FitnessAppropriateness` |

**The low-fitness intensity rule is exact, not judged.** `LowFitnessSafety` checks that an
itinerary for a traveler marked `low` fitness contains no activity labeled `high`. The agent's
output validator enforces the same label rule at generation time. This does not verify the actual
physical demands, accessibility, medical suitability, or overall safety of an activity; those
qualities are not established by the current eval suite.

`FitnessAppropriateness` is the one place quality genuinely doesn't reduce to a rule — "is this
itinerary's intensity and pacing actually well-suited to a 24-year-old vs. a 78-year-old" is a
judgment call, which is exactly the category `LLMJudge` is for.

## Why an LLM judge instead of a human judge

Human review can assess nuance that the deterministic evaluators cannot, but repeating it for every
run is manual work. The optional LLM judge provides a repeatable automated quality signal across
the four-case dataset. It is not a substitute for human validation and has not been calibrated
against a labeled human-reviewed dataset.

## Evaluation pipeline

| Stage | This project |
|---|---|
| **1. Test set** | `dataset.py` — 4 cases crossing traveler age (24, 78) × fitness level (low, high), same JFK→SAN route/dates |
| **2. System version** | `gpt-oss-120b` on Cerebras, current prompt (`app/agent/prompts.py`), `search_flights`/`web_search` tools |
| **3. Evaluator** | 7 exact evaluators by default, plus the `PhysicalLoadComparisons` report evaluator; optional `LLMJudge` with `--with-judge` |
| **4. Scores** | Pass/fail per assertion, plus a `physical_load` metric (sum of activity intensities) |
| **5. Decision** | Manual: a case failure or judge failure means don't ship that change until understood |
| **6. Monitoring** | Not built; the Agent Execution Panel observes application runs, not eval regressions |
| **Learn → update evals** | See the recorded-fixture bug below — a real example of this loop closing |

## Provider modes

`run.py` supports two modes, chosen for different reasons:

- **`recorded` (default)** — real *captured* SearchApi/Tavily payloads replayed from
  `tests/fixtures/recorded/`, but the LLM call itself is always live (Cerebras has no cassette
  equivalent — the model's tool-selection and generation *is* what's under test). This is what
  `--repeat 3` runs against: deterministic third-party data isolates evaluator failures to the
  model's behavior, not provider flakiness.
- **`--live-smoke`** — provider APIs end-to-end, one case, no repeats. It consumes the configured
  providers' quotas, so it is intended as an occasional manual compatibility check rather than a
  routine regression suite.

The recorded suite expects exactly one successful `search_flights` call with the case's route and
dates, plus exactly one successful broad, non-flight `web_search` call for destination activities.

## A fixture bug the evals caught, in themselves

The recorded cassette `tests/fixtures/recorded/flights/JFK_SAN_2026-09-01_2026-09-08.json`
originally held only the *first* step of SearchApi's round-trip flow (an outbound-only response
with a `departure_token`, no `return_flights`). `RecordedProvider` correctly refuses to fabricate
a return-leg pairing it was never given (see `test_recorded_provider_does_not_expose_unpaired_round_trip_offers`
in `tests/test_flight_provider_strategy.py`), so every recorded round-trip search for that route
returned zero offers. The agent, seeing no offers, retried `search_flights` with varied arguments
until it burned through the tool-call budget and the token budget, then failed to produce a
grounded itinerary — cascading into most of a `--repeat 3` run failing with
`UnexpectedModelBehavior`/`UsageLimitExceeded`, not a clean evaluator failure.

The fix: capture the real second step (SearchApi's `departure_token` → booking-options resolution,
the same pairing `LiveSearchApiProvider._pair_round_trip_offer` does live) and store the *paired*
result as the cassette, matching the shape `RecordedProvider` expects. This is the "learn and
update evals" loop in practice — the eval didn't just fail the agent, it surfaced a gap in the
eval's own recorded data, which running the suite end-to-end (rather than trusting individual unit
tests in isolation) is what exposed.

## Output reliability fix

Four reliability issues surfaced by running the live eval repeatedly, not by unit tests:

1. **Duplicate tool calls.** The model sometimes called `search_flights` twice per trip (searching
   the return leg separately, even though the first response already covers both legs) and
   `web_search` more than once, burning tool-call and token budget for no new information.
   `PlannerDeps` now tracks a per-run `_search_flights_called` / `_web_search_called` flag; a
   second call within the same run raises `ModelRetry` instead of re-hitting the provider.
2. **Free-text intensity.** `ActivityOut.intensity` was `str`, and the model wrote descriptive
   phrases ("low to moderate (tram seated)") that no downstream safety check could match against a
   fixed term list. It's now `Literal["low", "moderate", "high"]`, so pydantic rejects an
   out-of-vocabulary value at parse time — and the system prompt now states the closed vocabulary
   directly, so the model gets it right on the first attempt instead of relying on a `ModelRetry`
   correction loop (each retry resends the full conversation, which is what was driving both
   `UsageLimitExceeded` and `Exceeded maximum output retries` failures).
3. **Output envelope mismatch.** Left on `auto`, pydantic-ai picks native structured output for
   `gpt-oss-120b` and wraps the `ItineraryOut | ClarificationOut` union in `{"result": {"kind":
   ...}}`. A live run showed all three output retries were the model fumbling that envelope
   (missing `"result"`, missing `"result.kind"`, then a bad `"kind"` literal) rather than a
   validator rejection — and each retry resends the full itinerary, so one run stopped at 32,882
   tokens against Cerebras's 30,000/minute limit. Fixed by pinning `output_type` to
   `[ToolOutput(ItineraryOut), ToolOutput(ClarificationOut)]` in `app/agent/planner.py`, removing
   the envelope ambiguity entirely.
4. **Prose instead of a tool call.** Pinning tool output above closed the envelope retries but
   surfaced a new one: the model sometimes replied with the itinerary as plain text instead of
   calling the result tool. Fixed by naming the delivery contract directly in the system prompt
   (`AGENTS.md`): "Deliver your final answer by calling the result tool for it... Never write the
   itinerary or the question as plain text in a reply."

**Live-verified result, all four fixes in place** (`--repeat 3 --with-judge`, 12 case-runs,
`gpt-oss-120b` via Cerebras, recorded flight/activity fixtures + live LLM calls): all 12 case-runs
completed cleanly — no `UsageLimitExceeded`, no exhausted output retries. 84/84 deterministic
assertions passed (100%), and all four `PhysicalLoadComparisons` rows passed with real samples on
both sides of every comparison (previously blocked by the retry exhaustion above starving 3 of the
4 age/fitness buckets of any completed sample). The `--with-judge` evaluator itself,
`FitnessAppropriateness`, passed 11/12: `age_78_high_fitness [3/3]` failed with the same
`physical_load` score as a *passing* run for the same case, because the itinerary leaned on
shuttles/rest stops/minimized walking despite the traveler's stated high fitness level — a genuine
tone/pacing miss the numeric score can't see, which is exactly the class of failure
`FitnessAppropriateness` exists to catch (see "Why an LLM judge instead of a human judge" above).
The token-budget/retry-exhaustion failure did not recur in this sample. That result does not prove
the failure mode is eliminated across models, prompts, inputs, or provider conditions. The judge
failure remains a recorded model-behavior finding from this run.

## Operational boundaries

This project demonstrates several reliability and safety mechanisms, but it has not been load
tested, security audited, or operated as a multi-user production service. The notes below describe
the implemented behavior without treating those mechanisms as evidence of broader readiness.

**Resource controls.** `MAX_CONCURRENT_AGENT_RUNS` limits concurrent LLM calls, while
`MAX_TOOL_STEPS` and `MAX_CONTEXT_TOKENS` bound each agent run. Per-IP rate limiting protects
third-party quota, but it is stored in process memory and resets whenever the process restarts. Its
behavior behind a hosted reverse proxy has not been verified. The synchronous
`POST /api/trips/{trip_id}/plan` request can
also run for the full duration of the model and tool calls. No load test currently establishes
throughput, latency under contention, or safe horizontal scaling.

**Recovery.** DBOS checkpoints planner and booking workflows for crash recovery. This is tested in
the project's supported paths, but it is not a claim of end-to-end availability or exactly-once
behavior across every external provider call. Deployment recovery, database failover, and cold
starts still need environment-specific testing.

**Security controls and gaps.** Configuration secrets use Pydantic `SecretStr`; the agent's tools
cannot change booking state; booking and execution audit rows are protected by append-only database
triggers; and Slack callbacks are signature-checked. Web-search content is delimited as untrusted
input and the sanitizer has unit coverage. These are narrow controls, not a security assessment.
There is no application authentication or per-visitor data isolation, the rate limiter is not a
durable abuse-prevention mechanism, and adversarial web content has not been exercised through an
end-to-end eval. An unrestricted public deployment would need those gaps addressed first.

**Integrations.** Provider implementations are separated behind dependency injection and strategy
interfaces, and connector enablement is stored in the database. The execution panel reads persisted
run events. These choices make the current integrations easier to inspect and replace, but the
project does not implement organizational requirements such as SSO, role-based access control,
centralized telemetry, retention policy, or compliance reporting.

**Why there is no vector database.** Activity data comes from live web search rather than a local
document corpus, so this application has no retrieval use case that requires embeddings or vector
search. Activities are instead checked for source URLs returned by the search tool.
