# Durable planner steps — design

**Problem:** `_run_planner_workflow` (`backend/app/dbos_runtime.py:75-117`) is a `@DBOS.workflow`, but nothing inside its `agent.iter()` loop is a `@DBOS.step`. On crash-replay, DBOS re-runs the whole workflow body from scratch, so an already-completed Cerebras call, `search_flights` call, or `web_search` call gets re-issued — spending real external quota twice. The only existing step precedent, `_fetch_booking_options_step` (`backend/app/dbos_runtime.py:51-52`), wraps a provider method, not a whole workflow.

**Goal:** wrap the three non-idempotent external-call boundaries in `@DBOS.step` so replay re-uses their recorded result instead of re-calling out, with the smallest possible diff and zero behavior change for every non-DBOS caller (tests, evals, direct tool-unit tests).

## Key finding that shapes this design

DBOS steps only need their **return value** to be picklable for replay — not their arguments. `check_operation_execution`/`record_operation_result` (`dbos/_sys_db.py`) key off `(workflow_id, step_sequence_number)` and persist the output; inputs are recomputed by deterministic workflow replay. Also confirmed directly from `dbos/_core.py`'s `decorate_step`: a step called outside an active DBOS workflow context just calls the plain function — no recording, no error. Both facts mean a step decorator can be slapped onto an already-constructed instance's bound method, safely, even for instances shared with non-DBOS code paths.

This rules out wrapping pydantic-ai's raw `agent.iter()` graph nodes (`ModelRequestNode`, `CallToolsNode` — confirmed present in `pydantic_ai._agent_graph`): those nodes close over `ctx.deps`, which holds live, unpicklable state (the DB session, HTTP-backed providers). The three real boundaries are one level down, where the actual I/O happens:

| Call | Exact boundary | Return type |
|---|---|---|
| Cerebras completion | `Model.request(messages, model_settings, model_request_parameters)` — called once per round-trip by `ModelRequestNode._make_request` | `ModelResponse` (plain dataclass) |
| Flight search | `FlightProvider.search_offers(...)` | list of offers |
| Activity search | `ActivityProvider.search(...)` | list of `NormalizedActivityResult` |

(`FlightProvider.fetch_booking_options` is not part of the planner loop — booking execution already has its own step — so it's out of scope here.)

## Architecture

No new classes, no second agent. Patch the bound method on the instance actually used by the durable path, in place:

- **Cerebras**: the shared `agent` singleton (`backend/app/agent/planner.py`) is built once at import time and reused everywhere (tests, evals, the DBOS workflow). Patch `agent.model.request` once, at **module level in `dbos_runtime.py`**, right after the `agent` import — not inside `launch_dbos()`. `launch_dbos()` is called from FastAPI's lifespan, and the `client` test fixture (`tests/conftest.py`) is function-scoped (`with TestClient(app):` per test), so `launch_dbos()` fires once per test; patching there would stack a new `@DBOS.step` wrapper around an already-wrapped method on every single test, corrupting the shared instance. Module-level code runs exactly once per process no matter how many times `launch_dbos()`/`create_app()` run, so that's the only patch site that's actually idempotent by construction. Confirmed `Agent.model` is a plain property returning the exact `CerebrasModel` instance passed to the constructor (no lazy re-wrapping into an `InstrumentedModel` on assignment), so this patches the real, persistent instance.

  ```python
  # dbos_runtime.py, module level, right after `from app.agent.planner import ... agent ...`
  agent.model.request = DBOS.step(name="cerebras_request")(agent.model.request)
  ```

  Because the step no-ops outside workflow context, every existing non-DBOS caller of `agent` (tests, evals) is unaffected — same object, same behavior, just also replay-safe when DBOS is driving it.

- **Flight/activity search**: `_run_planner_workflow` already constructs fresh provider instances per call (`get_flight_provider(settings)`, `TavilyActivityProvider(...)`, lines 84-85). Patch each right after construction, inline:

  ```python
  flight_provider = get_flight_provider(settings)
  flight_provider.search_offers = DBOS.step(name="search_flights_offers")(flight_provider.search_offers)

  activity_provider = TavilyActivityProvider(settings.tavily_api_key.get_secret_value())
  activity_provider.search = DBOS.step(name="web_search")(activity_provider.search)
  ```

  Both provider classes are plain `@dataclass`es (no `__slots__`), confirmed patchable by direct construction + attribute assignment.

No changes to `planner.py`'s `search_flights`/`web_search` tool functions, `FlightSearchService`, `_build_agent()`, or any provider adapter class. All new lines live in `dbos_runtime.py`, matching its existing docstring: *"this is the only change from the plain, already-tested versions of the functions they call."*

## Error handling

Unchanged. A step that raises still propagates the exception into the workflow body exactly as the unwrapped call did today; `_run_planner_workflow`'s existing `except UsageLimitExceeded` / `except UnexpectedModelBehavior` handling around the `agent.iter()` loop needs no changes.

## Testing

Follow the existing precedent for `_fetch_booking_options_step` in `tests/test_dbos_runtime.py`: a call-count spy on the inner provider/model method, asserting a step only re-invokes the underlying call once per step-id even when DBOS's execution machinery re-enters (simulated replay), not on every workflow re-run. Three new cases, same shape as the existing one:

1. `agent.model.request` patched — spy asserts the underlying Cerebras call fires once across a simulated replay.
2. `flight_provider.search_offers` patched — same assertion.
3. `activity_provider.search` patched — same assertion.

Full existing suite (124 backend tests) must stay green; this is purely additive at the `dbos_runtime.py` call sites.

## Docs to update once implemented

- `docs/ARCHITECTURE.md` — "Durable execution (DBOS)" section currently documents per-call replay risk as a known, accepted limitation; update to describe the three step boundaries instead.
- `docs/DECISIONS.md` — same, in its DBOS section.
