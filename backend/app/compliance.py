"""Bounded security-control checks; results do not establish SOC 2 compliance."""

from collections import defaultdict, deque
from datetime import datetime, timedelta
from typing import Any

from app.models import SecurityEvent
from app.schemas import ControlResultOut
from app.security_evidence import DENIAL_THRESHOLD, DENIAL_WINDOW_MINUTES

PRIVILEGED_ACTIONS = {
    "booking.approve", "booking.cancel", "booking.execute", "connector.configure", "security.read",
}
AUDIT_TABLES = {"security_event", "booking_transition", "execution_event", "compliance_assessment"}


def evaluate_mfa(events: list[SecurityEvent], successes: list[SecurityEvent]) -> ControlResultOut:
    allowed = [event for event in events if event.action in PRIVILEGED_ACTIONS and event.decision == "allow"]
    failed: list[int] = []
    missing: list[int] = []
    evidence = {event.id for event in allowed if event.id is not None}
    for event in allowed:
        assert event.id is not None
        if event.policy_version != "1":
            missing.append(event.id)
            continue
        inputs = (event.enforcement_active, event.mfa_required, event.mfa_verified)
        if False in inputs:
            failed.append(event.id)
            continue
        if None in inputs:
            missing.append(event.id)
            continue
        verified = [success for success in successes if (
            success.action == "auth.mfa.verify" and success.decision == "allow"
            and success.policy_version == "1" and success.mfa_verified is True
            and event.actor_user_id is not None
            and success.session_id is not None and success.session_id == event.session_id
            and success.actor_user_id == event.actor_user_id and success.tenant_id == event.tenant_id
            and success.created_at <= event.created_at
        )]
        if not verified:
            missing.append(event.id)
        evidence.update(success.id for success in verified if success.id is not None)
    status = "fail" if failed else "insufficient_evidence" if missing or not allowed else "pass"
    return ControlResultOut(
        control="privileged_mfa", status=status,
        summary=f"{len(allowed)} privileged admissions; {len(failed)} violations; {len(missing)} missing MFA evidence.",
        evidence_event_ids=sorted(evidence),
        details={"violation_event_ids": failed, "missing_evidence_event_ids": missing},
    )


def evaluate_containment(
    events: list[SecurityEvent], window_end: datetime, window_start: datetime | None = None,
) -> ControlResultOut:
    windows: dict[tuple[str, str, str | None], deque[SecurityEvent]] = defaultdict(deque)
    revocations: dict[str, list[SecurityEvent]] = defaultdict(list)
    for event in events:
        if event.action == "session.revoke" and event.session_id and event.decision == "allow":
            revocations[event.session_id].append(event)
    failed: list[int] = []
    missing: list[int] = []
    evidence: set[int] = set()
    triggered = 0
    for event in sorted(events, key=lambda item: (item.created_at, item.id or 0)):
        if event.decision != "deny":
            continue
        window = windows[(event.tenant_id, event.device_id, event.session_id)]
        since = event.created_at - timedelta(minutes=DENIAL_WINDOW_MINUTES)
        while window and window[0].created_at < since:
            window.popleft()
        window.append(event)
        if len(window) < DENIAL_THRESHOLD or (window_start is not None and event.created_at < window_start):
            continue
        assert event.id is not None
        triggered += 1
        evidence.update(denial.id for denial in window if denial.id is not None)
        if event.policy_version != "1" or event.session_id is None or event.enforcement_active is None:
            missing.append(event.id)
            continue
        if event.enforcement_active is False:
            failed.append(event.id)
            continue
        deadline = event.created_at + timedelta(seconds=60)
        matched = [revocation for revocation in revocations[event.session_id] if (
            revocation.tenant_id == event.tenant_id and revocation.device_id == event.device_id
            and revocation.policy_version == "1" and revocation.enforcement_active is True
            and event.created_at <= revocation.created_at <= deadline
            and event.id in (revocation.related_event_ids or [])
            and len({denial.id for denial in window} & set(revocation.related_event_ids or [])) >= DENIAL_THRESHOLD
        )]
        if matched:
            evidence.update(revocation.id for revocation in matched if revocation.id is not None)
            continue
        if deadline > window_end:
            missing.append(event.id)
            continue
        failed.append(event.id)
    status = "fail" if failed else "insufficient_evidence" if missing or not triggered else "pass"
    return ControlResultOut(
        control="session_containment", status=status,
        summary=f"{triggered} threshold crossings; {len(failed)} containment violations; {len(missing)} unresolved.",
        evidence_event_ids=sorted(evidence),
        details={"violation_event_ids": failed, "missing_evidence_event_ids": missing, "response_seconds": 60},
    )


def evaluate_audit_protections(protections: list[dict[str, Any]]) -> ControlResultOut:
    present = {row["table_name"] for row in protections}
    missing = sorted(AUDIT_TABLES - present)
    failed = [row["table_name"] for row in protections if (
        not row["mutation_blocked"] or not row["truncate_blocked"] or row["can_bypass"]
    )]
    status = "fail" if failed else "insufficient_evidence" if missing else "pass"
    return ControlResultOut(
        control="audit_protections", status=status,
        summary="Current trigger configuration and application-role privileges; historical integrity is not evaluated.",
        details={"tables": protections, "missing_tables": missing, "failed_tables": failed},
    )
