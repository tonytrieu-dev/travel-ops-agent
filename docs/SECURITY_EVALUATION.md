# Zero-trust evaluation

Validation on 2026-10-14 uses real PostgreSQL 14.24 in an isolated local cluster, Python 3.13.11,
and migrations through `7c2d4e6f8a10`. FastAPI requests run through `TestClient`; flight/search and
model providers are test doubles. No live booking or paid provider calls are part of this run.

The default suite runs focused regressions; the latency benchmark is opt-in.

The focused backend suite passed: **49 tests passed, 1 benchmark skipped**. The type checker
reported **0 errors**.

| Scenario | Observed behavior | Evidence |
|---|---|---|
| Password-only login | Token reaches ordinary routes; booking approval returns 403 for missing MFA | `test_security_integration.py::test_password_only_login_allows_reads_but_not_booking_approval` |
| MFA-enabled login | Pending token rejected; verified token reaches owned resources | `test_pending_mfa_token_cannot_reach_protected_routes`, `test_full_login_mfa_token_round_trip_reaches_an_owned_resource` |
| Tampered or expired token | HTTP 401 | Separate signature and expiry regressions in `test_security.py` |
| Role/MFA escalation | Denied by policy | `test_security.py::test_privileged_action_needs_mfa_and_the_right_role` |
| Cross-tenant access and containment | Five 403 responses, one incident, then 401 for the revoked session; owner still gets 200 | `test_cross_tenant_denials_revoke_only_the_attacking_session` |
| Browser internal-segment spoof | HTTP 403 and persisted denial event | `test_segment_spoof_header_is_rejected_as_403_through_the_handler`, security policy tests |
| Append-only enforcement | UPDATE/DELETE rejected for booking transitions, execution events, and security events; security-event TRUNCATE rejected | `test_append_only.py` |
| Human approval gate | Execute before confirmation returns 409 without contacting the provider or changing booking state | `test_execute_before_confirmation_never_calls_provider` |
| Concurrent booking execution | Two approved execute requests share one reference, provider call, and executed transition | `test_concurrent_execution_claims_the_approved_booking_once` |
| Expired fare | Execute returns 409, never calls the provider, and persists an EXPIRED transition | `test_expired_booking_never_reaches_the_provider` |
| Provider failure | Approved execution preserves its executed state and reference and returns no fabricated links | `test_booking_options_failure_preserves_approved_execution` |
| Failed planner run | The exchange completed before a provider crash persists in one failed run | `test_dbos_runtime.py::test_planner_failure_preserves_completed_exchanges` |
| Low-fitness guardrail | High intensity raises a recoverable model retry; moderate intensity passes unchanged | `test_planner_guardrails.py::test_low_fitness_intensity_boundary` |
| Slack approval boundary | Valid signed callback returns 401 under enforcement without changing booking state or audit; development callback still confirms | `test_signed_slack_approval_requires_development_mode` |
| MFA input boundary | Six non-ASCII digits return 422 before MFA verification | `test_non_ascii_mfa_digits_are_rejected_before_verification` |
| Contract references | Every local OpenAPI reference resolves, including the login token response | `test_openapi_contract.py::test_contract_references_resolve` |

The MFA regression was reproduced before the fix: password login returned 200, then the issued
token received 403 on `/api/trips`. New sessions now start with `mfa_verified=False`; only a
successful TOTP check changes it to true. Disabling MFA permits ordinary password-authenticated
access and does not satisfy privileged-action MFA requirements.

The merge-review regressions were run before implementation: the contract check named the
missing `TokenOut` schema, a correctly signed Slack approval succeeded under enforcement, and
non-ASCII MFA digits reached the verifier and returned 500. Each check now passes at its named
boundary. Slack callbacks, notifications, and enabling the connector are unavailable under
enforcement until clicking users can be checked for ownership, role, and MFA. The connector is
development-only; it is not a second authenticated approval path.

The deleted tests were reviewed selectively. Only the failed-planner-run regression and the
low-fitness intensity boundary were retained from that restoration. The other 21 files remain
deleted in this PR; this evaluation does not claim their old coverage was replaced. Booking tests
separate approval, concurrent claims, fare expiry, and provider failure so a failure identifies
the responsible gate. Signature rejection remains at the authentication seam instead of adding
another HTTP test of the same decision.

Test-only connection pooling is disabled to avoid sharing async connections between the portal
and pytest event loops; the application's production pool is unchanged.

## Repeat the validation

Create a dedicated test database once, then migrate it. For the repository's Docker defaults:

```bash
docker compose up -d postgres
docker compose exec -T postgres createdb -U travel_agent travel_agent_test
cd backend
export TEST_DATABASE_URL=postgresql+asyncpg://travel_agent:travel_agent@localhost:5432/travel_agent_test
export DATABASE_URL="$TEST_DATABASE_URL"
uv run alembic upgrade head
uv run pytest -q
uv run pyrefly check
```

Use the configured local credentials if PostgreSQL is installed natively. The test harness
requires a database name ending in `_test` or `_testing` and clears test tables between cases.
Keep it separate from the application database. Required provider configuration is loaded from
the normal `.env`; this validation does not call those providers.

For a shorter repeatable demonstration of security, booking, append-only enforcement, and failed
planner behavior, run from `backend` with the same database variables:

```bash
uv run pytest -q tests/test_security.py tests/test_security_integration.py tests/test_append_only.py tests/test_dbos_runtime.py tests/test_planner_guardrails.py
```

## Modest load comparison

The comparison sends 100 measured `GET /api/trips` requests at concurrency 4 in each mode, after
five warm-up requests per mode. Both use the same seeded user, route, audit path, and PostgreSQL
instance. The baseline uses the development identity fallback with enforcement disabled; the
enforced case checks a signed token, persisted identity, and live session on every request.
Password hashing and TOTP login occur before timing. Any non-200 response fails the run.

Recorded local result (100/100 HTTP 200 responses in each measured mode):

| Mode | p50 | p95 | Throughput |
|---|---|---|---|
| Development baseline | 48.033 ms | 51.727 ms | 83.48 requests/s |
| Enforced authentication | 52.352 ms | 60.061 ms | 75.53 requests/s |
| Enforced minus baseline | +4.319 ms | +8.334 ms | — |

```bash
RUN_SECURITY_LOAD=1 uv run pytest -q -s tests/test_security_integration.py::test_modest_load_reports_authentication_overhead
```

The command prints p50/p95 latency, throughput, and the enforced-minus-baseline latency delta.
These are in-process HTTP-harness measurements with test-only unpooled database connections;
they include database and audit work, but exclude real network/TLS, browser, and reverse-proxy
costs. Baseline precedes enforcement, so cache/order effects remain. This small sample is not a
production capacity estimate or a benchmark of authentication cryptography alone.

To measure a running server with an already-issued token in `TRAVEL_OPS_TOKEN`, use:

```bash
uv run python -m scripts.security_load --requests 100 --concurrency 4
```

`TRAVEL_OPS_URL` defaults to `http://localhost:8000`. This command measures enforced HTTP latency
only; it does not change the server's enforcement setting or print the token.

## Trust boundaries and limits

A submitted device ID matching the database/token/session proves attribute consistency, not
device possession, health, or attestation. Segment labels and spoof-header rejection demonstrate
an application boundary, not network microsegmentation, service identity, or mTLS. Tokens are
custom HMAC-signed bearer credentials; the local issuer does not implement OIDC.

[NIST SP 800-207](https://csrc.nist.gov/pubs/sp/800/207/final) calls for explicit authentication
and authorization without implicit trust from network location or asset ownership. This bounded
demo illustrates selected application controls; it does not claim NIST conformance or a complete
enterprise deployment. Database-owner privileges can disable audit triggers. The planner test
checks failure persistence, not durable-step replay or operating-system crash/restart
recovery or successful airline purchases. No independent penetration test was performed.
