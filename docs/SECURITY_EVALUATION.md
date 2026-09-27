# Zero-trust evaluation

Results below are recorded from this checkout. PostgreSQL-backed scenarios remain pending because
the current environment has no listener on `localhost:5432`; no integration result is fabricated.

| Scenario | Expected result | Actual result | Security event | Containment |
|---|---|---|---|---|
| Valid identity claim | Allow | 7 focused security tests passed | Covered by policy/audit path | None |
| Tampered or expired token | Deny | 7 focused security tests passed | Auth/policy path | Session remains denied |
| Role/MFA escalation | Deny | 7 focused security tests passed | Policy denial path | Threshold containment when repeated |
| Tenant/resource mismatch | Deny | Pending PostgreSQL integration run | `trip.read` deny | Session revocation after threshold |
| Browser internal-segment spoof | Deny | Pending PostgreSQL integration run | `segment.assert` deny | Session revocation after threshold |
| Repeated denials | Incident + revoke session | Pending PostgreSQL integration run | Denial count and incident row | Affected session revoked |
| Append-only security event | Mutation rejected | Pending PostgreSQL migration test | N/A | Database trigger |
| Authorized booking approval | Allow only with MFA and state gate | Pending PostgreSQL integration run | `booking.approve` allow | Existing FSM remains authoritative |
