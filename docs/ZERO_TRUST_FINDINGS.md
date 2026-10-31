# Zero-trust architecture: before, after, and demonstration evidence

Updated: October 28, 2026. Living engineering record prepared with AI assistance.
This is project memory and source material for the students' own analysis, not a
student-authored report, hypothesis, or claim of certification.

## Purpose and working scope

Preserve how TravelOps changed from a travel-planning prototype into an application
with explicit identity, authorization, evidence, and containment boundaries. Record
implementation results separately from design intent. Keep this document current
when fixes or validation change those results; do not erase earlier failures.

Review findings F1–F4 below have been fixed and checked locally. Preserve the
existing dependency injection, repositories, booking finite state machine, flight
provider strategy, and DBOS durable execution. Security belongs at request admission
and booking transitions; monitoring observes those controls rather than replacing them.

## Project objective supplied by the user

**Title:** Design and Implementation of a Zero Trust Architecture for Enterprise Networks

**Objective:** Develop a Zero Trust security model for an enterprise network that
enforces strict identity verification for every user and device attempting to access
resources, regardless of their location within or outside the network.

**Key areas:** Identity and access management, multi-factor authentication,
micro-segmentation, continuous monitoring, and incident response.

This is the target against which design and implementation should be assessed.
The current TravelOps application demonstrates selected controls; it does not yet
fulfill every part of this objective. Preserve that distinction in future work.

| Objective area | Current implementation | Gap against the stated objective |
|---|---|---|
| Identity and access management | Protected API requests validate signed credentials against persisted identity, session, tenant, role and ownership | Local issuer and classroom identities; device ID matching does not authenticate a device or prevent bearer-token replay from another device |
| Multi-factor authentication | Password and TOTP session flow; verified MFA required for privileged actions | Local demonstration enrollment and shared synthetic TOTP secrets are not enterprise identity provisioning |
| Micro-segmentation | API admission, role/tenant resource isolation, rejection of spoofed internal-segment headers | No enforced network/workload segments, firewall policy or authenticated service-to-service boundary; do not label these API checks network micro-segmentation |
| Continuous monitoring | Append-only decisions and recurring MFA/audit/containment assessments with evidence/history | Local process schedule and bounded observation window; not enterprise-wide network/device telemetry |
| Incident response | Denial threshold, incident creation, session revocation and linked evidence | Automatic containment only; investigation, recovery and resolution workflows are outside implemented scope |
| Location-independent verification | Protected routes use the same identity/session policy rather than treating an internal-source assertion as trusted | Internal/external network-path equivalence has not been demonstrated in a deployed network experiment |

The mini use case below demonstrates authenticated resource isolation, monitoring
and session containment. It does not establish device authentication or network
micro-segmentation. Closing those gaps requires an explicit design decision and
implementation scope; this objective alone is not recorded as approval for a new
infrastructure deployment or a broader enterprise platform build.

## Before and after

The historical baseline is commit `888c127c1ee03499c9739a9798a76ac0be02f070`.
The reviewed PR head is `e2452654853a300920401582b91fb3d8a0eb8d7d`.
Monitoring and current fixes also include uncommitted workspace changes. The baseline
was inspected in git; it has not been rerun as an attack experiment in this session.
Development mode in the current application is not an exact historical baseline.

| Area | Before: travel-planning prototype | After: implemented application boundary | Evidence / qualification |
|---|---|---|---|
| Identity | `get_current_user` resolved or created one fixed demo user | Password login, signed bearer credentials, persisted sessions; protected requests reconcile claims with live user/session state | `security_identity.py`, `dependencies.py`; local issuer, not enterprise OIDC |
| Access policy | Demo identity did not establish an authenticated caller's tenant, role, or MFA | Shared admission checks action permission, tenant and owner; hidden resources return non-disclosing 404 | `security_admission.py`; operator security reads do not imply trip access |
| Privileged actions | Booking confirmation existed as a business gate | Booking approval/cancellation/execution and operator actions require the relevant role and verified MFA | Pending sessions cannot satisfy privileged policy; booking state machine remains a separate business gate |
| API boundary | No authenticated web-to-API security context | Dependencies validate context and reject browser assertions of internal source segments | `security_segment.py`; this is application segmentation, not a network firewall or mTLS |
| Agent authority | Planner researched and proposed; booking execution used deterministic application code | Planner retains read-only provider tools; booking stays outside the model's tools behind admission and the state machine | `docs/ARCHITECTURE.md`; caller authorization is not repeated inside each planner tool call |
| Audit | Booking/execution history already had mutation-rejecting database triggers | Security decisions and monitoring assessments add append-only evidence; truncation protection extends audit coverage | Existing audit protections are reused, not attributed entirely to the zero-trust work |
| Response | No equivalent security denial-window/session-revocation mechanism in the baseline identity boundary | Five denials within five minutes in a tenant/device bucket open an incident and revoke the triggering identifiable session | `security_evidence.py`; source attribution must be trustworthy (F1) |
| Control assessment | Execution history described activity, not recurring control outcomes | Recurring MFA, audit-configuration and containment assessments retain window/version/evidence/history | `compliance.py`, `compliance_monitoring.py`, compliance repository and UI; outcomes are pass/fail/insufficient evidence |
| Alternate approval | Signed Slack callback supplied a connector-level approval path | Legacy Slack approval is unavailable under enforcement until actor identity, ownership, role and MFA can be established | Shared signing secret alone does not authenticate the approving application user |

Implementation paths above are under `backend/app/` unless otherwise stated.

## Presentation scope and three-scenario demonstration

Present this as a first, bounded zero-trust application prototype with TravelOps
as the protected enterprise-style workload. Keep the supplied enterprise-network
project title as the objective, and identify device authentication and network
micro-segmentation as implementation gaps. This is engineering/demo guidance;
students write their own report interpretation and disclose assistance as required.

1. **Legitimate access:** sign in, complete TOTP MFA, read an owned trip, and show
   that a password-only session cannot perform a privileged booking action.
   Evidence: `test_password_only_login_allows_reads_but_not_booking_approval`,
   `test_pending_mfa_token_cannot_reach_protected_routes`, and
   `test_enrolled_legacy_owner_can_access_original_trip` in
   `backend/tests/test_security_integration.py`.
2. **Unauthorized access and containment:** tenant A attempts tenant B's booking,
   receives five non-disclosing denials, then receives 401 with the revoked token.
   Evidence: `test_cross_tenant_denials_produce_traceable_containment` in
   `backend/tests/test_compliance.py`.
3. **Operator explanation:** an MFA-verified tenant-A operator inspects the linked
   incident, assessment and event evidence. Show pass, fail and insufficient
   evidence honestly; explain why a database-owner role fails audit protection.
   Evidence: the containment test above and
   `test_monitoring_requires_operator_mfa_and_hides_other_tenants` in that file.

Use the existing isolated-PostgreSQL test instructions in `SECURITY_EVALUATION.md`.
These scenarios use synthetic data and stub providers. The current standalone
`scripts.security_demo` uses a missing-trip denial loop rather than the full
cross-tenant assessment scenario; do not describe its output as proof of all
three scenarios. Browser rehearsal and a captured demo remain separate work.

## Mini use case: detect, contain, and explain an unauthorized read burst

This is the concrete demonstration reconstructed from the retained handoff and
existing integration scenario: cross-tenant reads followed by evidence-backed
containment. The exact wording of an earlier conversational proposal was not retained.
The associated small monitoring use case is an operator inspecting whether the
existing MFA, audit and containment controls have supporting evidence.

**Actors and assets:** two synthetic tenants, a traveler in tenant A, a booking
owned by a traveler in tenant B, and an MFA-verified security operator in tenant A.
Use an isolated test database; no real traveler data or airline purchase is needed.

1. Authenticate the tenant-A traveler. Attempt to read the tenant-B booking.
2. Admission rejects the read with 404 without revealing ownership or existence.
   The decision record retains the authenticated actor, tenant/device/session,
   requested action, decision and reason.
3. Repeat until the fifth denial within five minutes. The same transaction links
   the triggering denial, incident and revocation evidence to session containment.
4. Retry with the still-unexpired token. Live session validation returns 401.
   A valid token signature does not override revoked persisted state.
5. Sign in as the tenant-A security operator with MFA and run/wait for assessment.
   Inspect containment evidence and assessment history. Tenant-B evidence remains
   unavailable to that operator.
6. Explain the three outcomes honestly: observed MFA/containment can pass; a
   database-owner application role causes audit protection to fail; no applicable
   activity or missing legacy evidence yields insufficient evidence.

Existing executable scenario:
`backend/tests/test_compliance.py::test_cross_tenant_denials_produce_traceable_containment`.
It checks denials, subsequent 401, passing containment assessment and incident linkage.
Do not attribute an explicit victim-still-accesses-200 assertion to this test: that
assertion is absent. A separate test would be required to substantiate that claim.

**Before/after interpretation:** the baseline's fixed identity could not establish
these two authenticated tenants or contain a particular authenticated session.
The implemented system rejects the read, records its reason, revokes a repeat
violator's session and exposes evidence to an authorized operator. This establishes
a bounded application behavior, not organization-wide breach prevention.

## Design and implementation findings

| Finding | Before / problem | Design or implementation response | Status / implication |
|---|---|---|---|
| Explicit identity is necessary | A demo user is not evidence of who sent a request | Reconcile signed claims with live identity/session records on protected reads | Implemented; signature, expiry, state mismatch and revocation have focused tests |
| Approval and authentication solve separate problems | A confirmed booking alone does not establish authority to execute it | Retain the finite state machine and add role, ownership and MFA admission | Implemented; preserve both gates during fixes |
| An allow event alone is weak assessment evidence | Earlier history omitted MFA inputs and independent success linkage | Record policy/enforcement/MFA inputs plus same-actor/session/tenant MFA-success evidence | Implemented; unsupported historical evidence stays unknown |
| Capped event queries cannot prove a complete window | Latest-200 API loses older events | Evaluate a repeatable-read database snapshot over 24 hours with overlap | Implemented and tested with 205 events/late commits; no retention or high-volume ingestion claim |
| Database privileges affect audit protection | Owner can disable triggers | Report ownership/superuser/replication bypass as failure | Implemented; do not hide the demo's owner-role failure |
| UI automation can trigger containment | Forbidden polling accumulates denials without further user action | Stop shared polling after a permission denial | F2 verified; operator trip permissions remain unchanged |
| Target account is not verified request source | Bad credentials were attributed to the target's registered device | Separate unauthenticated evidence from identified-session denial windows | F1 regressions pass; see issue ledger |
| Configured trigger does not necessarily protect rows | An enabled expected trigger can have `WHEN (false)` | Require unconditional expected trigger configuration | F3 verified; function-body verification remains excluded |
| Migration must retain a usable owner identity | Existing users receive empty password hashes and cannot log in | Explicit local enrollment without moving trip ownership or assigning a default password | F4 verified; existing MFA settings still apply |

## Review issue ledger

Stable identifiers refer to the October 28 review; preserve before/after evidence.

| ID | Severity / observed before | Fix and after evidence | Current status |
|---|---|---|---|
| F1 | Must fix: four attacker login failures for a victim plus one victim denial revoked the victim's session; ordinary access changed from 200 to 401 | Login failures no longer claim the registered device; containment and its evaluator separate sessionless and session-bearing denial windows. Regression tries both a wrong and spoofed matching device ID | Three PostgreSQL/API cases pass, including a victim whose device ID is `unknown`; full backend suite passes |
| F2 | Must fix: execution-history polling repeatedly returned 403 to an operator, reached the denial threshold and revoked the session | Shared hook suppresses further requests and manual refresh after 401/403 for the denied fetcher; 500 retries remain. Removed the monitoring-only workaround. Timer/request regressions first failed, then passed | Verified with hook harness; browser check not run |
| F3 | Must fix: replacing the expected enabled audit trigger with `WHEN (false)` allowed modification while assessment still passed under a restricted role | Assessment requires `tgqual IS NULL`; evaluator version is now 2. Real PostgreSQL regression first reported the conditional trigger protective, then correctly reported it unprotective; normal trigger remains protective | Verified; owner bypass still reports failure |
| F4 | Must fix: upgraded owners have empty hashes; synthetic seeding skips existing accounts, leaving their original trips inaccessible through authentication | `scripts.enroll_identity` prompts for a chosen password and refuses disabled, missing or already-enrolled accounts. Regression goes from login 401 to MFA-authenticated original-trip 200 without changing ownership | Verified using the migration-equivalent empty-hash state, not a fresh historical migration replay |
| F5 | Should fix: connector fetch error is hidden by the loading branch | Render initial error before loading | Fixed; component regressions cover 403 and network failures |
| F6 | Should fix: historical validation matrix cites absent tests | Reconcile claims against actual assertions | Fixed; four matrix rows corrected as recorded below |

## Follow-up Ponytail audit

The [October 28 PR #4 audit](PR4_AUDIT_2026-10-28.md) reviewed the committed PR
and current local fixes. Original F1–F4 reproductions remain fixed locally, but
this follow-up found two additional containment cases. A prior passing suite is
not a clean bill of health. No application code was changed during the audit.

| ID | Before / observed behavior | Proposed change | Status |
|---|---|---|---|
| F7 | Shared-device sessions pool denials and revoke an innocent session | Scope containment, evaluator windows and incident uniqueness to exact session; preserve sessionless grouping | Fixed; PostgreSQL/API regression covers two actors and two sessions of one actor |
| F8 | Booking remounts fetch privileged connector settings; five traveler denials cause revocation | Read public auth configuration; enforced booking uses in-app approval | Fixed; five component remounts make zero privileged requests |
| F9 | Failed authentication names the target account as actor | Unknown actor; target in resource, MFA session correlation retained | Fixed; password/MFA operator-visible API regressions |

These fixes retain the stable audit IDs (connector UX is user F4 / artifact F5;
report integrity is user F5 / artifact F6). Default synthetic seeded accounts have
distinct device IDs. F7 concerns migrated or deliberately shared-device accounts.

F7/F9 interaction: containment uses session correlation, independently of actor attribution.
Failed MFA stays in its target session's window even with an unknown actor; it cannot
join another legitimate session's window. Attempts without sessions retain their
sessionless grouping. Migration `3b7d9f1a2c45` preserves evidence and replaces incident
uniqueness indexes; downgrade duplicate-status reconciliation has not been exercised.

Exactly four security-evaluation matrix rows changed: MFA-enabled login now cites the
existing enrollment/owned-trip roundtrip; cross-tenant containment cites the real
compliance test and drops the unasserted victim-200 claim; Provider failure and MFA
input boundary were removed because their cited regressions do not exist. Surrounding
prose now distinguishes historical results from current coverage.

## Validation provenance

- Retained October 27 implementation validation: fresh isolated PostgreSQL 14.24,
  all migrations through `2a6c8e0f1b34`; 66 backend tests passed; pyrefly reported
  zero errors; frontend build, lint and existing API tests passed. These historical
  results did not cover the subsequently discovered F1–F4 failures.
- October 28 F1 regression: both parameter cases first failed (victim read returned
  401 instead of 200), then passed after the source-attribution fix. Test:
  `test_failed_logins_cannot_poison_victim_containment` in
  `backend/tests/test_security_integration.py`.
- No new latency benchmark was authorized or run. Numbers in
  `SECURITY_EVALUATION.md` are historical, not measurements of current fixes.
- No production deployment, application-database migration, live provider call,
  real purchase, browser end-to-end check or independent penetration test is
  established by this work.

## October 28 critical-fix validation

- Full backend suite against isolated PostgreSQL: **77 passed**.
- Backend `pyrefly check`: **0 errors**. `git diff --check` passed.
- Frontend API and polling test files, production build and lint passed. Polling
  tests use a minimal hook/timer harness without a DOM; this is not a browser test.
- The real PostgreSQL audit regression inspects assessment evidence for normal
  and conditional triggers, so database-owner failure cannot hide the bug. A new
  restricted-role attack experiment was not run; the earlier reproduction remains
  historical evidence.
- Enrollment tests also cover refusal of missing/disabled/already-enrolled accounts
  and passwords outside the accepted length. Existing accounts need explicit local
  enrollment; no application accounts were modified by validation.
- No commit, push, deploy, benchmark or application-database migration performed.

## Limits and future-session instructions

October 29 follow-up validation: **81 backend tests passed** against isolated PostgreSQL
at migration `3b7d9f1a2c45`; pyrefly **0 errors** (2 suppressed). Frontend `npm test`
passed all three test files; production build and lint passed. `git diff --check`
passed. F9 password/MFA cases first failed on actor attribution, then passed in the
full suite. Connector 403/network cases first rendered perpetual loading, then passed.
Component checks use the hook harness and server markup, without a browser lifecycle.
No new benchmark, commit, push, deployment or application-database migration.

Device ID consistency is not device authentication or attestation. Segment headers
are not network microsegmentation. The local bearer issuer is not an enterprise IdP.
The planner is constrained by its tools, not continuously reauthorized inside its
loop. DBOS is a reliability layer; it is not the authorization policy.

Monitoring is a local background loop and snapshot-based evidence check. It cannot
prove omitted events, continuous historical database protection, database-admin
resistance, complete incident response, or organizational compliance. Missing
evidence must not become a fabricated pass. SOC 2 mapping is a proposed subset;
certification and exact criterion coverage are not established. PCI scope is unresolved.

For future work: read this document alongside `ARCHITECTURE.md`,
`COMPLIANCE_MONITORING.md` and the current code. Preserve zero-trust boundaries and
append measured before/after outcomes here as each critical fix is completed.
Students retain hypothesis, final design interpretation, report and slide authorship.

## October 29 architecture-review fixes

A follow-up architecture review (report `/tmp/architecture-review-*.html`) and a meticulous
three-agent audit of the whole PR surfaced correctness defects in controls the "Before and
after" table already describes. These fixes harden those controls; they do not add new trust
boundaries. Identifiers `AR1`–`AR9` are local to this section and do not reuse the `F1`–`F9`
review-ledger IDs above. Every fix landed test-first: the regression failed on the unpatched
code and passed after. No application code path was left without a runnable check.

**Zero-trust boundary (report-relevant):**

| ID | Before / problem | Design or implementation response | Status / evidence |
|---|---|---|---|
| AR1 | Booking admission preloaded the row via `session.get`; the later `SELECT ... FOR UPDATE` returned the cached instance, so a concurrent cancel was invisible and a confirm/execute could revive a CANCELLED booking and write a false transition | `_lock_booking` re-reads under the lock with `execution_options(populate_existing=True)`; confirm and execute share the request session, execute uses its own workflow session | Verified: synchronized confirm-vs-cancel and execute-vs-cancel HTTP regressions (`test_booking_regressions.py`) — cancel commits first, the gated action then 409s, booking stays CANCELLED, no spurious transition, no options fetch |
| AR2 | A traveler visiting the Connectors tab issued a privileged `GET /api/connectors` on every mount; five denials within the window tripped session containment through ordinary navigation | `ConnectorsPanel` reuses the polled-resource denial cache, so a known-denied resource is not re-requested; backend authorization stays authoritative | Verified: six forbidden remounts make exactly one request (`security.test.js`) |
| AR3 | Lowering a traveler's fitness (or age) left the cached high-intensity itinerary in place; `/plan` returned it without re-running the low-fitness safety validator | `age` and `fitness_level` join the itinerary-invalidation criteria (flight offers, which do not depend on them, are untouched) | Verified: `test_trip_planning_regressions.py` — fitness/age change drops the itinerary and forces a replan; a non-criteria edit keeps it |
| AR4 | Expiry was derived from booking-request time, so a stale cached quote (up to the ~45-minute cache+TTL ceiling) was re-granted a fresh 30-minute approval window | Expiry derives from the original quote timestamp; already-elapsed quotes are rejected; the cross-trip cache copy preserves the source quote timestamp | Verified: 14-minute quote yields a ~16-minute window, 16-minute quote is rejected EXPIRED; cross-trip copy keeps `created_at` (`test_booking_regressions.py`) |

**Durability and state integrity:**

| ID | Before / problem | Design or implementation response | Status / evidence |
|---|---|---|---|
| AR5 | Two concurrent `/plan` requests both saw no itinerary, both ran the planner (double LLM spend), and one hit the unique-constraint `IntegrityError` (500) | `get_or_create_itinerary` takes a per-trip `pg_advisory_xact_lock` before the cache check and holds it through the re-read; the second caller finds the first's itinerary and skips the planner. Advisory, not a trip-row lock, because per-step event recording also locks that row | Verified: two concurrent `/plan` invoke the planner once, both 200, one itinerary (`test_concurrent_http_planning_invokes_planner_once`) |
| AR6 | Editing a trip mid-plan persisted an itinerary computed for the old criteria | `persist_itinerary_if_current` re-derives the planner prompt from the current trip and persists only if it matches the prompt planned for; the caller 409s when the row is absent | Verified: plan then edit mid-run yields 409 and no stale itinerary (`test_edit_during_planning_discards_obsolete_itinerary`) |
| AR7 | The itinerary was persisted by the HTTP caller after the durable workflow returned; a crash in that gap lost committed planning work | Persistence runs as a DBOS step inside `_run_planner_workflow`, idempotent on replay | Verified by invoking the registered workflow without the HTTP caller and replaying its workflow ID (`test_durable_workflow_persists_without_the_http_caller`). No process-kill/restart experiment was run |
| AR8 | Editing a trip with a booking deleted the booking's referenced offer, breaking the foreign key (500) and threatening audit history | `update_trip` rejects a route/date change with 409 before any mutation when a booking references the trip's offers; unbooked trips still invalidate | Verified: booked-trip route edit 409s with offer + booking intact; unbooked still invalidates (`test_trip_planning_regressions.py`) |

**Client request lifecycle and display integrity:**

| ID | Before / problem | Design or implementation response | Status / evidence |
|---|---|---|---|
| AR9 | Several frontend paths let a late response land under the wrong context: a resolving plan overwrote a newly selected trip; a run's final refresh was lost when a poll was in flight; a suppressed denial showed perpetual "Loading…" after remount; evidence landed under a switched assessment; an expired MFA challenge had no recovery; one trip's "clear" hid another's activity; date-only values rendered a day early in negative-offset zones | Request/selection-identity guards discard obsolete results (`App.tsx` selection generation, `CompliancePanel` evidence request id); `usePolledResource` rejects obsolete responses, preserves one pending refresh, and surfaces a cached denial as an error; `LoginPanel` adds an MFA restart; `LiveActivity` scopes its clear cutoff to its trip; date-only values are formatted without UTC conversion | Verified with mounted React tests (`@testing-library/react` + jsdom): `App.test.js`, `usePolledResource.test.js`, `selection.test.js`, `activity.test.js`, `dates.test.js`. The prior hand-mocked hook harness that could not reproduce rerenders was replaced |

Also corrected: the `README` zero-trust demo omitted the `uvicorn` start step and copied a native
`DATABASE_URL` mismatched with the Docker service, and overstated booking-options provider-failure
preservation beyond what a regression actually asserts. Both are now accurate (see `README.md`,
reconciled against `SECURITY_EVALUATION.md`).

### October 29 architecture-review validation

- Full backend suite against isolated PostgreSQL (`travel_agent_test`, migration `3b7d9f1a2c45`):
  **97 passed**. `-m no_database` subset: **34 passed**. `pyrefly check`: **0 errors** (2 suppressed).
- Frontend `npm test`: **20 passed** (run under `TZ=America/Los_Angeles` for deterministic date and
  timestamp assertions); production build and `oxlint` clean (0 warnings, 0 errors).
- RED→GREEN confirmed for the new fixes: neutralizing the advisory lock, the mid-run prompt re-check,
  and the booking guard turned AR5/AR6/AR8 red; removing the App selection guard turned AR9's stale-plan
  case red; restoring each turned them green.
- `git diff --cached --check` passed. No commit, push, deployment, application-database migration, live
  provider call, real purchase, browser end-to-end check, or new benchmark was performed. All work is
  staged locally for review only. Agent-tooling directories remain untracked and unstaged.
