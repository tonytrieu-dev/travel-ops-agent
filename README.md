# TravelOps Agent

An AI travel-planning application that searches flight data, researches destination activities,
and generates an age- and fitness-aware itinerary. Required trip fields are validated at intake,
while ambiguous values can produce a clarifying question instead of an itinerary. A
**human-in-the-loop** state machine keeps booking handoff outside the agent: the model can research
and propose flights, but it has no tool that can approve or execute booking state changes.

## Project status and limitations

This is a portfolio and reference implementation intended for local development and evaluation.
It is not a production travel service and has not been load tested or security audited. There is
no authentication: every request currently uses the same demo account, so the application must
not be exposed as an unrestricted public service. Activity citations show that a URL came from the
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
```

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

### 7. (Optional) Slack human-in-the-loop approvals

Booking approval can be routed through Slack instead of the in-app UI. Slack's Interactivity
callback needs a public HTTPS URL, so local development requires a tunnel:

```bash
ngrok http 8000
```

Use the printed `https://*.ngrok.io` URL as the Slack app's Request URL. Full setup steps
(app creation, tokens, channel config) are in [`docs/SLACK_SETUP.md`](docs/SLACK_SETUP.md).
Nothing here is required for the app to run — without it, the Connectors tab shows the Slack
toggle greyed out and booking approval stays in-app.

## Key decisions

The reasoning behind the REST state machine, typed outputs, provider failure handling, DBOS
workflows, append-only audit tables, request limits, and provider choices is documented in
[docs/DECISIONS.md](docs/DECISIONS.md).
