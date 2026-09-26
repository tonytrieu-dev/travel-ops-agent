"""PostgreSQL-backed zero-trust invariants; run with the migrated test database."""

from sqlalchemy import select
from sqlmodel import col

from app.config import get_settings
from app.dependencies import _password_hash, _totp_code
from app.models import SecurityEvent, SecurityIncident, User
from tests.db_helpers import run_db


def test_revoked_session_is_denied_and_denial_threshold_contains_it(client, monkeypatch) -> None:
    monkeypatch.setenv("ZERO_TRUST_ENFORCED", "true")
    monkeypatch.setenv("ZERO_TRUST_SIGNING_SECRET", "integration-secret")
    monkeypatch.setenv("AGENT_SERVICE_TOKEN", "integration-service-secret")
    get_settings.cache_clear()
    run_db(lambda session: _seed_user(session, "traveler@tenant-a.test", "tenant-a", "device-a"))
    login = client.post(
        "/api/auth/login",
        json={"email": "traveler@tenant-a.test", "password": "demo-password", "device_id": "device-a"},
    )
    assert login.status_code == 200
    pending = login.json()
    verified = client.post(
        "/api/auth/mfa",
        json={"session_id": pending["session_id"], "code": _totp_code("JBSWY3DPEHPK3PXP")},
    )
    assert verified.status_code == 200
    headers = {"Authorization": f"Bearer {verified.json()['access_token']}"}
    for _ in range(5):
        assert client.get("/api/trips/999999", headers=headers).status_code == 404
    incidents = run_db(_incidents)
    assert incidents and incidents[0].status == "open"
    assert client.get("/api/trips", headers=headers).status_code == 401
    get_settings.cache_clear()


def test_missing_and_mfa_unverified_authentication_failures_are_audited(client, monkeypatch) -> None:
    monkeypatch.setenv("ZERO_TRUST_ENFORCED", "true")
    monkeypatch.setenv("ZERO_TRUST_SIGNING_SECRET", "integration-secret")
    get_settings.cache_clear()
    run_db(lambda session: _seed_user(session, "mfa@tenant-a.test", "tenant-a", "device-a"))

    assert client.get("/api/trips").status_code == 401
    login = client.post(
        "/api/auth/login",
        json={"email": "mfa@tenant-a.test", "password": "demo-password", "device_id": "device-a"},
    )
    assert login.status_code == 200
    pending_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    assert client.get("/api/trips", headers=pending_headers).status_code == 403

    events = run_db(_auth_denials)
    assert len(events) >= 2
    assert {event.action for event in events} == {"auth.authenticate"}
    assert all(event.tenant_id == "unknown" and event.device_id == "unknown" for event in events)
    get_settings.cache_clear()


def test_login_validates_device_and_skips_mfa_when_disabled(client, monkeypatch) -> None:
    monkeypatch.setenv("ZERO_TRUST_ENFORCED", "true")
    monkeypatch.setenv("ZERO_TRUST_SIGNING_SECRET", "integration-secret")
    monkeypatch.setenv("AGENT_SERVICE_TOKEN", "integration-service-secret")
    get_settings.cache_clear()
    run_db(
        lambda session: _seed_user(
            session, "no-mfa@tenant-a.test", "tenant-a", "device-a", mfa_enabled=False
        )
    )

    rejected = client.post(
        "/api/auth/login",
        json={"email": "no-mfa@tenant-a.test", "password": "demo-password", "device_id": "wrong"},
    )
    assert rejected.status_code == 401

    login = client.post(
        "/api/auth/login",
        json={"email": "no-mfa@tenant-a.test", "password": "demo-password", "device_id": "device-a"},
        headers={"x-correlation-id": "login-correlation"},
    )
    assert login.status_code == 200
    assert login.json()["mfa_required"] is False
    event = run_db(_latest_login_allow)
    assert event is not None
    assert event.correlation_id == "login-correlation"
    assert event.tenant_id == "tenant-a"
    assert event.device_id == "device-a"
    assert event.session_id == login.json()["session_id"]
    get_settings.cache_clear()


async def _seed_user(
    session, email: str, tenant_id: str, device_id: str, *, mfa_enabled: bool = True
) -> None:
    user = User(
        email=email,
        tenant_id=tenant_id,
        role="traveler",
        device_id=device_id,
        mfa_enabled=mfa_enabled,
        password_hash=_password_hash("demo-password"),
    )
    session.add(user)
    await session.flush()


async def _incidents(session) -> list[SecurityIncident]:
    return list(await session.scalars(select(SecurityIncident)))


async def _auth_denials(session) -> list[SecurityEvent]:
    return list(
        await session.scalars(
            select(SecurityEvent).where(
                col(SecurityEvent.action) == "auth.authenticate",
                col(SecurityEvent.decision) == "deny",
            )
        )
    )


async def _latest_login_allow(session) -> SecurityEvent | None:
    return await session.scalar(
        select(SecurityEvent)
        .where(
            col(SecurityEvent.action) == "auth.login",
            col(SecurityEvent.decision) == "allow",
        )
        .order_by(col(SecurityEvent.id).desc())
    )
