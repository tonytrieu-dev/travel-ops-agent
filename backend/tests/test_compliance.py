import pytest

from sqlalchemy import select
from sqlmodel import col

from app.models import ComplianceAssessment, SecurityEvent, utcnow
from app.repositories.compliance_repository import assess_security_controls
from tests.db_helpers import run_db
from tests.test_security_integration import enforced_client, _owned_booking, _seed_user, _sign_in


def test_monitoring_requires_operator_mfa_and_hides_other_tenants(enforced_client) -> None:
    _seed_user("operator@a.test", "tenant-a", "security-operator", "operator-a")
    _seed_user("operator@b.test", "tenant-b", "security-operator", "operator-b")
    _seed_user("traveler@a.test", "tenant-a", "traveler", "traveler-a")
    _seed_user("password@a.test", "tenant-a", "security-operator", "password-a", mfa_enabled=False)
    token = _sign_in(enforced_client, "operator@a.test", "operator-a")
    _sign_in(enforced_client, "operator@b.test", "operator-b")
    headers = {"Authorization": f"Bearer {token}"}
    assert enforced_client.get("/api/security/monitoring").status_code == 401
    empty = enforced_client.get("/api/security/monitoring", headers=headers)
    assert empty.status_code == 200
    assert empty.json() == {"stale": True, "assessments": []}
    run_db(assess_security_controls)

    async def _foreign_assessment(session):
        assessment = ComplianceAssessment(
            tenant_id="tenant-b", evaluator_version="1", window_start=utcnow(),
            window_end=utcnow(), assessed_at=utcnow(), results=[{
                "control": "privileged_mfa", "status": "fail",
                "summary": "foreign-tenant-only-evidence", "evidence_event_ids": [], "details": {},
            }],
        )
        session.add(assessment)
        await session.flush()
        return assessment.id

    foreign_assessment_id = run_db(_foreign_assessment)

    response = enforced_client.get("/api/security/monitoring", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["stale"] is False
    assert len(body["assessments"]) == 1
    assert foreign_assessment_id not in {assessment["id"] for assessment in body["assessments"]}
    assert "foreign-tenant-only-evidence" not in response.text
    results = {result["control"]: result for result in body["assessments"][0]["results"]}
    assert results["privileged_mfa"]["status"] == "pass"
    assert results["audit_protections"]["status"] == "fail"  # Test DB role owns its tables.
    assert results["session_containment"]["status"] == "insufficient_evidence"

    async def _foreign_evidence(session):
        return (await session.scalars(select(SecurityEvent).where(col(SecurityEvent.tenant_id) == "tenant-b"))).first()

    foreign = run_db(_foreign_evidence)
    assert foreign is not None
    assert enforced_client.get(f"/api/security/events/{foreign.id}", headers=headers).status_code == 404
    own_id = results["privileged_mfa"]["evidence_event_ids"][0]
    assert enforced_client.get(f"/api/security/events/{own_id}", headers=headers).status_code == 200

    traveler = _sign_in(enforced_client, "traveler@a.test", "traveler-a")
    assert enforced_client.get("/api/security/monitoring", headers={"Authorization": f"Bearer {traveler}"}).status_code == 403
    password = enforced_client.post("/api/auth/login", json={
        "email": "password@a.test", "password": "demo-password", "device_id": "password-a",
    }).json()["access_token"]
    assert enforced_client.get("/api/security/monitoring", headers={"Authorization": f"Bearer {password}"}).status_code == 403


def test_recurring_assessment_preserves_history_and_stale_results(enforced_client) -> None:
    from datetime import timedelta
    from app.models import utcnow

    _seed_user("operator@a.test", "tenant-a", "security-operator", "operator-a")
    token = _sign_in(enforced_client, "operator@a.test", "operator-a")

    async def _old_assessment(session):
        session.add(ComplianceAssessment(
            tenant_id="tenant-a", evaluator_version="1", window_start=utcnow()-timedelta(days=1),
            window_end=utcnow()-timedelta(minutes=5), assessed_at=utcnow()-timedelta(minutes=5), results=[],
        ))

    run_db(_old_assessment)
    headers = {"Authorization": f"Bearer {token}"}
    assert enforced_client.get("/api/security/monitoring", headers=headers).json()["stale"] is True
    run_db(assess_security_controls)
    body = enforced_client.get("/api/security/monitoring", headers=headers).json()
    assert body["stale"] is False
    assert len(body["assessments"]) == 2


def test_cross_tenant_denials_produce_traceable_containment(enforced_client) -> None:
    _seed_user("attacker@a.test", "tenant-a", "traveler", "attacker-a")
    victim = _seed_user("victim@b.test", "tenant-b", "traveler", "victim-b")
    _seed_user("operator@a.test", "tenant-a", "security-operator", "operator-a")
    booking = _owned_booking(victim)
    attacker = _sign_in(enforced_client, "attacker@a.test", "attacker-a")
    attacker_headers = {"Authorization": f"Bearer {attacker}"}
    for _ in range(5):
        assert enforced_client.get(f"/api/bookings/{booking}", headers=attacker_headers).status_code == 404
    assert enforced_client.get(f"/api/bookings/{booking}", headers=attacker_headers).status_code == 401
    operator = _sign_in(enforced_client, "operator@a.test", "operator-a")
    headers = {"Authorization": f"Bearer {operator}"}
    run_db(assess_security_controls)
    results = enforced_client.get("/api/security/monitoring", headers=headers).json()["assessments"][0]["results"]
    containment = next(result for result in results if result["control"] == "session_containment")
    assert containment["status"] == "pass"
    incident = enforced_client.get("/api/security/incidents", headers=headers).json()[0]
    assert incident["trigger_event_id"] in containment["evidence_event_ids"]


def test_assessment_reads_beyond_200_events_and_reassesses_late_commits(enforced_client) -> None:
    from datetime import timedelta
    from app.models import utcnow

    _seed_user("operator@a.test", "tenant-a", "security-operator", "operator-a")
    token = _sign_in(enforced_client, "operator@a.test", "operator-a")
    headers = {"Authorization": f"Bearer {token}"}
    run_db(assess_security_controls)

    async def _late_events(session):
        for number in range(205):
            session.add(SecurityEvent(
                tenant_id="tenant-a", device_id="sample", action="booking.approve", resource=f"booking:{number}",
                source_segment="web", target_segment="api", decision="allow", reason="synthetic regression input",
                correlation_id="duplicate", policy_version="1", enforcement_active=False,
                mfa_required=True, mfa_verified=False, created_at=utcnow()-timedelta(minutes=1),
            ))

    run_db(_late_events)
    run_db(assess_security_controls)
    results = enforced_client.get("/api/security/monitoring", headers=headers).json()["assessments"][0]["results"]
    mfa = next(result for result in results if result["control"] == "privileged_mfa")
    assert mfa["status"] == "fail"
    assert len(mfa["details"]["violation_event_ids"]) == 205


def test_background_monitor_persists_repeated_assessments(monkeypatch) -> None:
    import asyncio
    from contextlib import suppress
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.compliance_monitoring import monitor_security_controls
    from app.repositories.compliance_repository import get_assessments

    _seed_user("operator@a.test", "tenant-a", "security-operator", "operator-a")

    async def _work(session):
        factory = async_sessionmaker(session.bind, expire_on_commit=False)
        monkeypatch.setattr("app.compliance_monitoring.get_session_factory", lambda: factory)
        monkeypatch.setattr("app.compliance_monitoring.ASSESSMENT_INTERVAL_SECONDS", 0)
        task = asyncio.create_task(monitor_security_controls())

        async def _wait_for_history():
            while len(await get_assessments(session, "tenant-a")) < 2:
                await asyncio.sleep(0.01)

        try:
            await asyncio.wait_for(_wait_for_history(), timeout=5)
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    run_db(_work)


def test_audit_assessment_rejects_conditional_mutation_trigger() -> None:
    from sqlalchemy import text
    from app.models import User

    async def _setup(session):
        session.add(User(email="audit@example.test"))
        return await session.scalar(text("""SELECT pg_get_triggerdef(oid) FROM pg_trigger
            WHERE tgname = 'security_event_append_only'"""))

    definition = run_db(_setup)

    async def _protection(session):
        await assess_security_controls(session)
        assessment = await session.scalar(select(ComplianceAssessment).order_by(col(ComplianceAssessment.id).desc()))
        result = next(result for result in assessment.results if result["control"] == "audit_protections")
        return next(table for table in result["details"]["tables"] if table["table_name"] == "security_event")

    async def _replace(session):
        await session.execute(text("DROP TRIGGER security_event_append_only ON security_event"))
        await session.execute(text("""CREATE TRIGGER security_event_append_only
            BEFORE UPDATE OR DELETE ON security_event FOR EACH ROW
            WHEN (false) EXECUTE FUNCTION reject_security_event_mutation()"""))

    async def _restore(session):
        await session.execute(text("DROP TRIGGER security_event_append_only ON security_event"))
        await session.execute(text(definition))

    assert run_db(_protection)["mutation_blocked"] is True
    try:
        run_db(_replace)
        assert run_db(_protection)["mutation_blocked"] is False
    finally:
        run_db(_restore)


@pytest.mark.parametrize("same_actor", [False, True])
def test_shared_device_denials_are_scoped_to_each_session(enforced_client, same_actor) -> None:
    _seed_user("first@a.test", "tenant-a", "traveler", "shared-device")
    if not same_actor:
        _seed_user("second@a.test", "tenant-a", "traveler", "shared-device")
    _seed_user("operator@a.test", "tenant-a", "security-operator", "operator-a")
    first = _sign_in(enforced_client, "first@a.test", "shared-device")
    second = _sign_in(enforced_client, "first@a.test" if same_actor else "second@a.test", "shared-device")
    operator = _sign_in(enforced_client, "operator@a.test", "operator-a")
    first_headers = {"Authorization": f"Bearer {first}"}
    second_headers = {"Authorization": f"Bearer {second}"}
    operator_headers = {"Authorization": f"Bearer {operator}"}
    for _ in range(4):
        assert enforced_client.get("/api/trips/999999", headers=first_headers).status_code == 404
    assert enforced_client.get("/api/trips/999999", headers=second_headers).status_code == 404
    assert enforced_client.get("/api/trips", headers=second_headers).status_code == 200
    run_db(assess_security_controls)
    results = enforced_client.get("/api/security/monitoring", headers=operator_headers).json()["assessments"][0]["results"]
    assert next(result for result in results if result["control"] == "session_containment")["status"] == "insufficient_evidence"
    assert enforced_client.get("/api/trips/999999", headers=first_headers).status_code == 404
    assert enforced_client.get("/api/trips", headers=first_headers).status_code == 401
    assert enforced_client.get("/api/trips", headers=second_headers).status_code == 200
    for _ in range(4):
        assert enforced_client.get("/api/trips/999999", headers=second_headers).status_code == 404
    assert enforced_client.get("/api/trips", headers=second_headers).status_code == 401
    incidents = enforced_client.get("/api/security/incidents", headers=operator_headers).json()
    assert len(incidents) == 2
    assert len({incident["session_id"] for incident in incidents}) == 2
    run_db(assess_security_controls)
    results = enforced_client.get("/api/security/monitoring", headers=operator_headers).json()["assessments"][0]["results"]
    assert next(result for result in results if result["control"] == "session_containment")["status"] == "pass"
