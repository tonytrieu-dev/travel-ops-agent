# AGENTS.md

Repo instructions for coding agents working on Travel Agent.

## Workflow
- Code-first TDD: runtime code and focused tests are the source of truth. Add the smallest regression
  test at the boundary that can fail; do not duplicate behavior in Gherkin or a hand-authored API spec.
- Security changes must exercise the affected trust boundary: identity/session validation, role and
  MFA policy, tenant ownership, API segmentation, append-only audit, or automatic containment.
- `cd backend && uv run pytest -q` and `uv run pyrefly check` before committing (needs PostgreSQL;
  `uv run pytest -q -m no_database` runs the subset that doesn't).
- Fail-fast/never-nest everywhere, except inside the agent's tool-calling loop: a recoverable
  tool-call error raises `ModelRetry`, not an exception, so the model can self-correct.
- No abbreviations in names. Comments only for non-obvious WHY.

## Architecture (enforced)
DI (`Depends`) · Finite State Machine (`app/state.py`) · Repository (`app/repositories/`) ·
Strategy (`FlightProvider`: Live vs Recorded by `USE_LIVE_FLIGHT_API`) · Durable Execution
(DBOS wraps booking `execute` and the agent run).

## Travel-agent system prompt

The planner's LLM boundary (system prompt, output schemas `ItineraryOut`/`ClarificationOut`/
`ItineraryDayOut`/`ActivityOut`, and the Cerebras client) is defined once in
`backend/baml_src/travel_planner.baml` and generated into `baml_sdk`. Edit the prompt there, then
run `baml generate` from `backend`. `app/agent/prompts.py` reads the prompt text from that BAML
source; `app/schemas.py` re-exports the generated output types. The pydantic-ai agent in
`app/agent/planner.py` still owns the tool loop and the stateful safety validators.
