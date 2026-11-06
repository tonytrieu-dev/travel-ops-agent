# Security-control monitoring: implementation and findings

October 29 follow-up: containment and evaluator windows now use exact session IDs,
with sessionless attempts grouped separately. Incident uniqueness follows that boundary
in migration `3b7d9f1a2c45`; evaluator version is 3. Failed password/MFA events retain
unknown actors and target-account resources. Validation: 81 backend tests passed,
pyrefly zero errors; frontend tests, build and lint passed. See ZERO_TRUST_FINDINGS.md
for regression evidence and the four corrected security-evaluation matrix rows.

Engineering notes generated with AI assistance on October 27, 2026. This is implementation
documentation, not a student-authored hypothesis, analysis, report, or claim of certification.

## Implemented scope

TravelOps evaluates three bounded controls every minute while the backend is running. Each
assessment retains its version, UTC observation window, execution time, findings, and evidence
event IDs. Assessments are append-only. Operators and administrators need MFA to read them;
both assessment history and individual evidence reads are restricted to their tenant.

| Check | Pass means | Fail means | Insufficient evidence means |
|---|---|---|---|
| Privileged MFA | Every observed privileged admission has enforcement enabled, an MFA requirement, verified MFA, and a preceding MFA-success event for the same actor/session/tenant | A recorded admission explicitly bypassed enforcement, the MFA requirement, or verified MFA | No privileged admissions, legacy/unsupported evidence, incomplete snapshots, or no matching MFA-success record |
| Audit protections | The four audit/assessment tables currently have the expected enabled mutation and truncation triggers, and the application role has no detected ownership/superuser/replication bypass | A required trigger is missing/disabled or the role can bypass protections | Required tables cannot be found; collection errors instead leave the previous assessment stale |
| Session containment | Every observed threshold crossing has enforcement enabled and linked revocation evidence for its session within 60 seconds | Enforcement was disabled or a known-session crossing lacks timely revocation evidence after the 60-second allowance | No crossing, legacy evidence, unidentified session, or the response allowance has not elapsed |

Privileged actions are booking approval, cancellation and execution; connector configuration;
and security reads. A passing MFA check concerns admission, not operation completion. Simply
viewing monitoring can exercise the security-read MFA check; it does not demonstrate a booking.

Containment retains the existing policy: five denial events within five minutes, grouped by
tenant/device and whether the event has an identified session, revoke the triggering identifiable session. The 60-second evaluation allowance
is a prototype check definition, not a SOC 2 requirement. Revocation evidence records the
denial IDs in the same transaction as session revocation. New incidents retain their triggering
event ID. The evaluator does not claim incident investigation, recovery, or resolution coverage.

## Evidence changes and lessons

- Historical events lacked MFA inputs. New decision records capture policy version, enforcement
  mode, actor role, MFA requirement and verified state. Missing historical inputs stay unknown;
  the migration deliberately does not backfill them from today's mutable state.
- Successful MFA previously only changed a session flag. It now records an immutable success
  event in the same transaction as that change.
- An authorization `allow` is the enforcement system's assertion. The evaluator checks its
  recorded inputs against a fixed expected privileged-action set and separate MFA-success
  evidence. It remains dependent on application instrumentation; it is not an independent audit.
- The newest-200-events API cannot provide complete ingestion. Assessments read the database
  directly using one repeatable-read snapshot and no event cap. Each run rereads the preceding
  24 hours, plus five minutes of denial context and relevant earlier MFA successes. A transaction
  committed after one snapshot can appear in the next overlapping assessment.
- A caller-supplied correlation ID is not a trusted unique operation ID. Evidence links use
  database event IDs, not correlation IDs.
- UPDATE/DELETE protection did not prevent TRUNCATE on booking/execution audit tables. The
  migration adds those protections and protects the assessment table as well.
- Database ownership is a real limitation: the usual owner-run development database should
  report an audit-protection failure. The implementation does not conceal that by weakening
  the check or automatically changing database roles.
- Reading security endpoints creates security events. The monitor itself reads directly;
  the screen's reads are still audited. The screen stops polling after a forbidden response
  so an unauthorized visit does not automatically accumulate a denial burst.

## Framework mapping and exclusions

The proposed mapping is to selected AICPA Trust Services Criteria security topics: logical
access for the MFA evidence, and system monitoring/response for audit protections and containment.
These technical checks are supporting evidence for broader criteria, not complete criteria tests.
Exact criterion/point-of-focus mappings remain to be checked against the official text and
selected by the students. No framework-wide score is calculated.

[AICPA Trust Services Criteria](https://www.aicpa-cima.com/resources/article/2017-trust-services-criteria-with-revised-points-of-focus-2022)
and the [AICPA SOC services overview](https://www.aicpa-cima.com/resources/landing/system-and-organization-controls-soc-suite-of-services)
describe the framework and assurance context. This tool produces neither a SOC 2 examination
nor a PCI DSS compliance determination.

PCI applicability was not established. Lack of stored card data alone does not exclude a service:
[PCI SSC FAQ 1579](https://www.pcisecuritystandards.org/faqs/1579/) and
[FAQ 1580](https://www.pcisecuritystandards.org/faqs/1580/) cover services that affect payment-data
security. The earlier handoff's FAQ 1574 scope citation was incorrect.

Tenant isolation remains an existing enforcement mechanism and a tested containment scenario;
comprehensive tenant-isolation monitoring is excluded. Also excluded: device authentication,
network microsegmentation, complete incident response, payment-security assessment, organizational
policies, personnel controls, retention assurance, and a general policy engine.

## Operation and demonstration

1. With the application's normal environment and database configured, run
   `cd backend && uv run alembic upgrade head`, then start the backend normally.
2. If demo identities are needed, use the existing `uv run python -m scripts.seed_security_demo`.
   They are synthetic classroom accounts, not production credentials.
3. Sign in as `operator@tenant-a.test` using that script's credentials and an authenticator code.
   Open **Security monitoring**. Allow a minute for the first assessment after activity.
4. Inspect the MFA result and its decision/success records. On an owner-run database the audit
   check should fail and identify the bypass. With no denial burst, containment should say
   insufficient evidence. This shows all three outcomes without fabricating passing controls.
5. The cross-tenant integration regression exercises five denied reads, revocation, rejection
   of the revoked token, incident linkage, and a passing containment assessment. Reproduce in
   a separate test database with
   `uv run pytest -q tests/test_compliance.py tests/test_compliance_unit.py tests/test_append_only.py`.

The API exposes the latest ten assessments at `GET /api/security/monitoring` and individual
tenant-visible records at `GET /api/security/events/{event_id}`. The screen displays the window,
assessment time, version, history, evidence and findings. Results older than three minutes
are marked stale; failed reads also warn that displayed results may be outdated.

## Practical limits

The background loop is local to the server process. Exceptions are logged and retried; stopped
processes do not run assessments. An advisory transaction lock prevents overlapping evaluators,
but this is not a durable schedule or a guarantee of one run per minute across multiple workers.

A 24-hour reread avoids a fragile cursor but does not prove collection completeness. Omitted
events, clock errors, privileged tampering, and transactions delayed beyond the rolling window
remain outside the guarantee. Old assessments are historical snapshots, not rewritten conclusions.
Unattributed events cannot be assigned to a tenant's assessment. Full-window reads and evidence
lists target local demonstration volumes; retention and large-volume ingestion are not implemented.

Audit checks inspect current trigger names, modes, unconditional configuration and role privileges. They do not verify trigger
function bodies, establish uninterrupted historical protection, or defeat a database administrator.
Mutation rejection is validated separately against real PostgreSQL in the integration suite.

## Validation on October 27, 2026

- Fresh isolated PostgreSQL 14.24 cluster: migrations through `2a6c8e0f1b34` applied successfully.
- Full backend suite: **66 passed**. Backend `pyrefly check`: **0 errors**.
- Frontend production build, lint, and existing API tests passed.
- Regression coverage includes missing/historical MFA inputs, same-session temporal matching,
  explicit enforcement bypass, tenant-protected assessment/evidence reads, stale history,
  repeated background assessments, 205-event collection, late commits, cross-tenant denial and
  session containment, and UPDATE/DELETE/TRUNCATE rejection for all four protected tables.
- No benchmarks, live provider calls, browser visual verification, or application-database
  migration were performed. Existing unrelated workspace changes were preserved.


## October 28 critical-fix follow-up

Evaluator version 2 requires unconditional triggers and separates sessionless from
session-bearing containment evidence. Failed password attempts no longer claim the
target account's registered device. The shared UI polling hook now suppresses further
401/403 requests, including execution-history polling; transient failures still retry.
An explicit local enrollment command restores password setup for existing empty-hash
accounts while preserving ownership and MFA policy.

See [the architecture findings and mini use case](ZERO_TRUST_FINDINGS.md) for the
before/after record, critical-issue ledger, current validation and remaining limits.
