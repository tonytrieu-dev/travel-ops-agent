from datetime import timedelta

import pytest

from app.compliance import evaluate_audit_protections, evaluate_containment, evaluate_mfa
from app.models import SecurityEvent, utcnow

pytestmark = pytest.mark.no_database


def test_privileged_allow_without_historical_mfa_evidence_is_unknown() -> None:
    event = SecurityEvent(
        id=1, tenant_id="tenant-a", device_id="device-a", action="booking.approve",
        resource="booking:1", decision="allow", reason="allowed by role policy",
        source_segment="web", target_segment="api", correlation_id="caller-controlled",
        created_at=utcnow() - timedelta(minutes=1),
    )
    result = evaluate_mfa([event], [])
    assert result.status == "insufficient_evidence"
    assert result.evidence_event_ids == [1]


def _event(identifier: int, action: str, **values) -> SecurityEvent:
    return SecurityEvent(
        id=identifier, tenant_id="tenant-a", actor_user_id=1, session_id="session-a",
        device_id="device-a", action=action, resource="booking:1", decision="allow",
        reason="test", source_segment="web", target_segment="api", correlation_id="same",
        policy_version="1", enforcement_active=True, mfa_required=True, mfa_verified=True,
        **values,
    )


def test_mfa_pass_requires_success_before_admission_for_the_same_session() -> None:
    now = utcnow()
    admission = _event(2, "booking.approve", created_at=now)
    success = _event(1, "auth.mfa.verify", created_at=now - timedelta(seconds=1))
    assert evaluate_mfa([admission], [success]).status == "pass"
    success.created_at = now + timedelta(seconds=1)
    assert evaluate_mfa([admission], [success]).status == "insufficient_evidence"
    success.created_at = now - timedelta(seconds=1)
    success.session_id = "other-session"
    assert evaluate_mfa([admission], [success]).status == "insufficient_evidence"


@pytest.mark.parametrize("field", ["enforcement_active", "mfa_required", "mfa_verified"])
def test_known_mfa_policy_violation_fails(field: str) -> None:
    admission = _event(1, "booking.approve")
    setattr(admission, field, False)
    assert evaluate_mfa([admission], []).status == "fail"


@pytest.mark.parametrize("field", ["enforcement_active", "mfa_required", "mfa_verified"])
def test_missing_snapshot_input_is_insufficient_evidence(field: str) -> None:
    now = utcnow()
    admission = _event(2, "booking.approve", created_at=now)
    success = _event(1, "auth.mfa.verify", created_at=now - timedelta(seconds=1))
    assert evaluate_mfa([admission], [success]).status == "pass"
    setattr(admission, field, None)
    assert evaluate_mfa([admission], [success]).status == "insufficient_evidence"


def test_empty_window_does_not_pass() -> None:
    assert evaluate_mfa([], []).status == "insufficient_evidence"
    assert evaluate_containment([], utcnow()).status == "insufficient_evidence"


@pytest.mark.parametrize("invalid", ["session", "missing_links", "wrong_links", "late"])
def test_containment_requires_a_linked_revocation_after_the_fifth_denial(invalid: str) -> None:
    now = utcnow()
    events = [_event(number, "trip.read", created_at=now - timedelta(seconds=100-number)) for number in range(1, 6)]
    for event in events:
        event.decision = "deny"
    assert evaluate_containment(events, now).status == "fail"
    revocation = _event(6, "session.revoke", created_at=now - timedelta(seconds=94), related_event_ids=[1, 2, 3, 4, 5])
    assert evaluate_containment([*events, revocation], now).status == "pass"
    if invalid == "session":
        revocation.session_id = "other-session"
    if invalid == "missing_links":
        revocation.related_event_ids = None
    if invalid == "wrong_links":
        revocation.related_event_ids = [1, 2, 3, 4, 99]
    if invalid == "late":
        revocation.created_at = events[-1].created_at + timedelta(seconds=61)
    assert evaluate_containment([*events, revocation], now).status == "fail"


@pytest.mark.parametrize("disabled", ["denials", "revocation"])
def test_disabled_enforcement_does_not_pass_containment(disabled: str) -> None:
    now = utcnow()
    events = [_event(number, "trip.read", created_at=now - timedelta(seconds=100-number)) for number in range(1, 6)]
    for event in events:
        event.decision = "deny"
        event.enforcement_active = disabled != "denials"
    revocation = _event(6, "session.revoke", created_at=now - timedelta(seconds=94), related_event_ids=[1, 2, 3, 4, 5])
    revocation.enforcement_active = disabled != "revocation"
    assert evaluate_containment([*events, revocation], now).status == "fail"


def test_unattributed_denial_burst_is_insufficient_evidence() -> None:
    now = utcnow()
    events = [_event(number, "auth.authenticate", created_at=now - timedelta(seconds=100-number)) for number in range(1, 6)]
    for event in events:
        event.decision = "deny"
        event.session_id = None
    assert evaluate_containment(events, now).status == "insufficient_evidence"


def test_audit_configuration_requires_all_tables_and_a_restricted_application_role() -> None:
    protections = [
        {"table_name": name, "mutation_blocked": True, "truncate_blocked": True, "can_bypass": False}
        for name in ("security_event", "booking_transition", "execution_event", "compliance_assessment")
    ]
    assert evaluate_audit_protections(protections).status == "pass"
    protections[0]["can_bypass"] = True
    assert evaluate_audit_protections(protections).status == "fail"
    protections[0]["can_bypass"] = False
    protections[1]["truncate_blocked"] = False
    assert evaluate_audit_protections(protections).status == "fail"
    assert evaluate_audit_protections([]).status == "insufficient_evidence"
