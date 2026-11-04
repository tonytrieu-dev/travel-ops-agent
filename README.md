# TravelOps Agent

A bounded **zero-trust application prototype**, using a stateful travel planner as
the protected resource. It demonstrates user/session verification, role and tenant
access policy, privileged MFA, recorded security decisions, and automatic session
containment. It is a first implementation toward the enterprise-network project
objective; the isolated deployment adds certificate possession and private service networks.
Deployment-level verification still requires Docker.

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

The SPA signs in through `POST /api/auth/browser/login` and
`POST /api/auth/browser/mfa`. The API sets a 15-minute HttpOnly, SameSite=Strict cookie;
browser responses omit the bearer token. Requests include cookies, and browser writes
must send the configured `FRONTEND_ORIGIN`. HTTPS is required outside loopback development;
use the same site for the frontend and API so Strict cookies are sent. The original
`/api/auth/login` and `/api/auth/mfa` endpoints remain available to bearer-token CLI clients.
Set `ZERO_TRUST_SIGNING_SECRET` and use the seeded demo credentials or a
local user; MFA-enabled users must complete the TOTP step. Connector settings are deployment-wide,
not tenant-owned, and both reading and changing them require the `security-operator` or `admin` role
plus verified MFA.

Slack approval callbacks and notifications are disabled while enforcement is enabled, even
with credentials configured. Slack users are not mapped to trip owners or verified application
sessions; approve and cancel bookings in the app. The legacy Slack flow is development-only.

Device IDs demonstrate consistency between submitted and stored identity attributes; they do not
prove device health or hardware attestation. The isolated deployment adds mutual TLS
and private service networks; see [deployment instructions](#running-it). See the [trust boundaries](docs/ARCHITECTURE.md#zero-trust-enterprise-network-overlay).

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

Follow [deployment setup](#running-it) first, including the private environment secrets.
For the synthetic walkthrough, issue both devices before starting the gateway:

```bash
python3 deploy/issue-device.py device-a-traveler
python3 deploy/issue-device.py device-a-operator
docker compose -f compose.zero-trust.yml up --build -d
docker compose -f compose.zero-trust.yml run --rm migrate python -m scripts.seed_security_demo
cd backend
uv sync
baml generate
uv run python -m scripts.security_demo
```

Stop API writers while applying migration `5d9f1b3c4e67`; deploy the updated API
and frontend together. This migration clears fixed-salt password hashes and revokes their sessions.
It preserves account ownership and modern password hashes. Cleared hashes cannot be
restored by downgrade; those accounts must enroll a new password.
For an existing account left with an empty password hash, run
`uv run python -m scripts.enroll_identity --email <email> --provisioning-file <private-new-file>`
from `backend` as a trusted database operator. This provisions a unique authenticator and
revokes previous sessions while preserving ownership and policy. Import the private URI into
an authenticator and remove the file. Migration `6e0a2c4d8f10` retires the public shared TOTP
secret and its sessions; already password-enrolled accounts need `--reset-mfa` to provision
a replacement authenticator. Synthetic demo seeds explicitly retain public demo credentials.
Login, MFA, and protected requests require the registered client certificate through the
gateway described in [deployment instructions](#running-it).

The [architecture findings and mini use case](docs/ZERO_TRUST_FINDINGS.md) preserve
the baseline, implemented changes, before/after evidence, and outstanding review issues.

The synthetic accounts all use `demo-password` and the TOTP secret
`JBSWY3DPEHPK3PXP` (the script calculates the current six-digit code).
Each identity can consume a code only once; wait for the next 30-second code before
repeating MFA sign-in, including when rerunning the demo. The demo shows a valid
tenant-scoped request, MFA-gated operator access, token/session rejection,
browser segment spoof rejection, denials triggering session revocation, and the
security-operations event/incident query. These are throwaway demo credentials; never reuse them.

For the repeatable PostgreSQL demonstration, including authorized booking, cross-tenant denial,
tampered tokens, revocation, and completed planner exchanges retained after a later model failure, follow
the [evaluation commands](docs/SECURITY_EVALUATION.md). That harness uses isolated test data and
stubbed external providers; it does not purchase flights or consume model/search quota.
If the booking-options provider is unavailable, execution stays confirmed and the user can retry;
the app does not create a terminal handoff without checkout links.

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

The enforced deployment uses `compose.zero-trust.yml`. It builds the frontend and backend,
serves the browser application through an HTTPS gateway, and keeps the API and PostgreSQL
ports private. Docker with Compose and OpenSSL are required. The original `docker-compose.yml`
starts only a loopback development database; it does not provide the certificate gateway or
restricted application role. Existing development database volumes are not copied into the
isolated stack automatically.

### 1. Configure the environment

From the repository root:

```bash
cp .env.example .env
```

Fill in `CEREBRAS_API_KEY`, `SEARCHAPI_API_KEY`, and `TAVILY_API_KEY`. Add these four settings
to the root `.env`, using a different random value for each:

| Setting | Purpose |
|---|---|
| `OWNER_DATABASE_PASSWORD` | Administrative database access for provisioning and migrations |
| `RUNTIME_DATABASE_PASSWORD` | Restricted database access used by the running API and DBOS |
| `ZERO_TRUST_SIGNING_SECRET` | Signs application access tokens and keeps them valid across restarts |
| `DEVICE_GATEWAY_SECRET` | Authenticates the gateway's certificate-verification headers to the API |

Generate each value separately with `openssl rand -hex 32`. Use the resulting hex strings
without adding them to source control. Hex avoids special-character escaping in the database
URL and gateway configuration. Compose supplies the container database URLs and sets
`FRONTEND_ORIGIN=https://localhost:8443` for this deployment.

### 2. Issue and import device certificates

An administrator must first confirm the account's registered device ID. Issue a certificate
for that exact ID from the repository root:

```bash
python3 deploy/issue-device.py <registered-device-id>
```

The script creates a local certificate authority, the localhost server certificate, and the
requested client certificate under the ignored `.device-certificates/` directory. For the
synthetic walkthrough, issue `device-a-traveler` and `device-a-operator` as shown in the
[demo commands](#zero-trust-demo).

Import the device's `.p12` file into the browser's client-certificate store, using the password
in its matching `.password` file. Trust `ca.crt` for the local HTTPS server. Keep private keys
and password files private, and never distribute `ca.key`: anyone holding that key could issue
another trusted device certificate. The gateway receives only the CA certificate and its own
server certificate and key.

The gateway validates the client certificate and overwrites device-identity headers. The API
checks the gateway secret and matches the certified device ID to the account or session during
login, MFA, and protected requests. A copied access token alone cannot satisfy this boundary.
These are exportable software keys, so the check establishes key possession rather than device
health or hardware attestation.

Device and server certificates expire after seven days. This small deployment has no
per-certificate revocation service. Urgent revocation requires replacing the CA, reissuing the
remaining devices, and restarting the gateway. A sustained deployment needs managed certificate
renewal and revocation.

### 3. Start the isolated stack

```bash
docker compose -f compose.zero-trust.yml up --build -d
```

Compose starts PostgreSQL, applies migrations using the owner credential, grants the runtime
role its permissions, and then starts the API and gateway. The backend image installs BAML
toolchain 0.23.0 and runs `baml generate`, so the ignored SDK is generated during the build.
The frontend is built with `/api` as its API URL and served from the same HTTPS origin.

Only the gateway publishes a host port, at `https://localhost:8443`. Gateway and API share an
internal application network. The API and provisioning containers join the separate database
network; the gateway does not. The API also has an outbound network for model and search
provider calls. Keep the API and PostgreSQL ports private when changing this configuration.

The owner password is supplied only to provisioning containers. The runtime role can read and
append protected audit evidence but cannot update or delete it, truncate its tables, disable
its triggers, or drop those tables. It owns the DBOS schema so durable execution can initialize
its system tables without owning the application's tables. Permission grants run after migrations.

### 4. Enroll accounts and verify access

Ordinary accounts must already exist with the intended tenant, role, and device assignment.
A trusted database operator enrolls an existing account with:

```bash
docker compose -f compose.zero-trust.yml run --rm migrate python -m scripts.enroll_identity \
  --email <existing-account-email> --provisioning-file /provisioning/enrollment-uri
```

The command prompts for a password and writes a unique authenticator provisioning URI to
`.device-certificates/enrollment-uri` on the host. The file must not already exist. Transfer it
securely to the account owner, import the URI into an authenticator, and remove the file after
enrollment. Provisioning preserves the account's ownership and policy and revokes its existing
sessions. For a password-enrolled account whose shared MFA secret was retired, add `--reset-mfa`
to rotate its authenticator while preserving its password.

The [synthetic seed command](#zero-trust-demo) is only for a disposable demonstration: its
accounts deliberately use public passwords and authenticator secrets. Do not use those
identities for a real deployment.

Open `https://localhost:8443` with the registered client certificate, then sign in and complete
MFA. Tenant security operators and admins see their own tenant's security evidence. The separate
`platform-operator` role can investigate cross-tenant events and incidents, including attacks
that could not be attributed to an authenticated account. Assign that role only through trusted
administrative account management.

From the repository root, check the deployed boundaries with an issued device ID:

```bash
sh deploy/check-boundaries.sh <registered-device-id>
```

The script checks gateway-to-API access, blocks gateway-to-database access, checks certificate
admission, and verifies audit protections through the API's runtime database credentials.
The configuration and local certificate issuance have been checked, and a restricted database
login has passed the audit-protection checks and started DBOS in its own schema. Container
builds, deployed network isolation, and the gateway TLS handshake remain unverified in the
current development environment because Docker is unavailable there.

For the authenticated load script, set `TRAVEL_OPS_CA`, `TRAVEL_OPS_CERTIFICATE`, and
`TRAVEL_OPS_PRIVATE_KEY` to the certificate files, and `TRAVEL_OPS_TOKEN` to the corresponding
session token. CLI clients validate the server certificate and present their device certificate.

For backend development outside containers, install BAML toolchain 0.23.0, matching the locked
`baml-bridge` runtime, then run `uv sync` and `baml generate` from `backend`. Regenerate the SDK
when `baml_src/` changes. A directly started API with enforcement enabled still requires the
trusted certificate gateway. The legacy frontend development server (`cd frontend && npm install
&& npm run dev`) listens on port 5173; it does not supply that gateway. Use the isolated stack
for the enforced browser walkthrough.

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
