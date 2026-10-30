"""Zero-trust decision path, exercised against the real Postgres the app runs on.

Each test targets a distinct failure mode: when it goes red, it names one root cause a developer
can act on. Overlapping or trivial-one-liner checks are intentionally absent — the append-only
guarantee lives in test_append_only.py, and login throttling in test_rate_limit.py.
"""

import asyncio
from collections.abc import Iterator
from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import col

from app.config import Settings
from app.security_identity import (
    SecurityContext,
    authenticate_identity,
    create_security_session,
    issue_access_token,
)
from app.models import SecurityEvent, SecurityIncident, SecuritySession, User, utcnow
from app.repositories.trips_repository import TripError
from app.security_evidence import (
    DENIAL_THRESHOLD,
    record_security_event,
)
from app.security_admission import SecurityError, admit_trip, authorize
from tests.db_helpers import TEST_DATABASE_URL, run_db

_SIGNING_SECRET = "unit-test-signing-secret-at-least-32-bytes"


def _settings(*, enforced: bool) -> Settings:
    return Settings(
        cerebras_api_key="x",
        searchapi_api_key="x",
        tavily_api_key="x",
        database_url=TEST_DATABASE_URL,
        zero_trust_enforced=enforced,
        zero_trust_signing_secret=_SIGNING_SECRET if enforced else "local-development-only",
    )


def _patch_settings(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[Settings]:
    monkeypatch.setattr("app.config.get_settings", lambda: settings)
    yield settings


@pytest.fixture
def enforced(monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    yield from _patch_settings(monkeypatch, _settings(enforced=True))


@pytest.fixture
def unenforced(monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    yield from _patch_settings(monkeypatch, _settings(enforced=False))


async def _add_user(
    session: AsyncSession,
    *,
    tenant_id: str = "tenant-a",
    role: str = "traveler",
    device_id: str = "device-a",
    mfa_enabled: bool = True,
) -> User:
    user = User(
        email=f"{role}-{device_id}@{tenant_id}.test",
        tenant_id=tenant_id,
        role=role,
        device_id=device_id,
        mfa_enabled=mfa_enabled,
    )
    session.add(user)
    await session.flush()
    assert user.id is not None
    return user


def _context(user: User, *, mfa_verified: bool, session_id: str | None = None) -> SecurityContext:
    assert user.id is not None
    return SecurityContext(user.tenant_id, user.id, user.device_id, user.role, mfa_verified, session_id)


async def _token(session: AsyncSession, user: User) -> str:
    security_session = await create_security_session(session, user, user.device_id)
    security_session.mfa_verified = True
    await session.flush()
    return issue_access_token(security_session, user)


def _events(action: str | None = None, decision: str | None = None) -> list[SecurityEvent]:
    async def _read(session: AsyncSession) -> list[SecurityEvent]:
        statement = select(SecurityEvent)
        if action is not None:
            statement = statement.where(col(SecurityEvent.action) == action)
        if decision is not None:
            statement = statement.where(col(SecurityEvent.decision) == decision)
        return list(await session.scalars(statement.order_by(col(SecurityEvent.id))))

    return run_db(_read)


# --- Identity verification ---------------------------------------------------------------------
# One test per rejection cause, because each is a separate branch in authenticate_identity and a
# separate bug if it regresses: a good token, a forged signature, stale claims, expiry, and
# revocation. They are not interchangeable — e.g. revocation and expiry are different code paths,
# and a bug can break one while the other still passes.


def test_tampered_token_signature_is_rejected(enforced: Settings) -> None:
    async def _work(session: AsyncSession) -> None:
        user = await _add_user(session)
        token = await _token(session, user)
        forged = token[:-1] + ("0" if token[-1] != "0" else "1")
        await authenticate_identity(f"Bearer {forged}", session)

    with pytest.raises(HTTPException) as error:
        run_db(_work)
    assert error.value.status_code == 401


def test_claims_that_disagree_with_persisted_state_are_rejected(enforced: Settings) -> None:
    """The core zero-trust property: a correctly-signed token stops working once it no longer
    matches live user state, because claims are re-checked every request, not trusted blind."""

    async def _work(session: AsyncSession) -> None:
        user = await _add_user(session, role="traveler")
        token = await _token(session, user)
        user.role = "admin"
        await session.flush()
        await authenticate_identity(f"Bearer {token}", session)

    with pytest.raises(HTTPException) as error:
        run_db(_work)
    assert error.value.status_code == 403


def test_expired_token_is_rejected(enforced: Settings) -> None:
    async def _work(session: AsyncSession) -> None:
        user = await _add_user(session)
        security_session = await create_security_session(session, user, user.device_id)
        security_session.mfa_verified = True
        security_session.expires_at = utcnow() - timedelta(minutes=1)
        await session.flush()
        await authenticate_identity(
            f"Bearer {issue_access_token(security_session, user)}", session
        )

    with pytest.raises(HTTPException) as error:
        run_db(_work)
    assert error.value.status_code == 401


def test_revoked_session_rejects_an_otherwise_valid_token(enforced: Settings) -> None:
    """Revocation is the containment primitive — a still-unexpired, correctly-signed token must
    die the instant its backing session is revoked. Distinct path from expiry above."""

    async def _work(session: AsyncSession) -> None:
        user = await _add_user(session)
        security_session = await create_security_session(session, user, user.device_id)
        security_session.mfa_verified = True
        token = issue_access_token(security_session, user)
        security_session.revoked_at = utcnow()
        await session.flush()
        await authenticate_identity(f"Bearer {token}", session)

    with pytest.raises(HTTPException) as error:
        run_db(_work)
    assert error.value.status_code == 401


# --- MFA and RBAC ------------------------------------------------------------------------------


def test_privileged_action_needs_mfa_and_the_right_role(enforced: Settings) -> None:
    """authorize() is the single policy gate: it must deny an unverified session, deny a role
    that lacks the action, and allow only when both role and MFA are satisfied. One test, three
    assertions, because they share one code path and one failure would implicate it."""

    async def _work(session: AsyncSession) -> None:
        traveler = await _add_user(session)
        with pytest.raises(SecurityError, match="MFA"):
            await authorize(
                _context(traveler, mfa_verified=False), session,
                action="booking.approve", resource="booking:1", require_mfa=True,
            )
        with pytest.raises(SecurityError, match="role"):
            await authorize(
                _context(traveler, mfa_verified=True), session,
                action="security.read", resource="x", require_mfa=True,
            )
        operator = await _add_user(session, role="security-operator", device_id="device-op")
        await authorize(
            _context(operator, mfa_verified=True), session,
            action="security.read", resource="security-events", require_mfa=True,
        )

    run_db(_work)


# --- Micro-segmentation and tenant isolation ---------------------------------------------------


def test_missing_resource_records_denial_when_enforcement_is_disabled(
    unenforced: Settings,
) -> None:
    async def _work(session: AsyncSession) -> None:
        context = _context(await _add_user(session), mfa_verified=False)
        with pytest.raises(TripError):
            await admit_trip(session, context, 999_999, action="trip.read")

    run_db(_work)
    events = _events(action="trip.read")
    assert events and events[-1].decision == "deny"


# --- Continuous monitoring ---------------------------------------------------------------------


def test_development_mode_allows_but_still_records_the_decision(unenforced: Settings) -> None:
    """Enforcement off must never silently skip the audit write, or the monitoring trail has a
    hole in dev; the recorded reason must also name the bypass honestly."""

    async def _work(session: AsyncSession) -> None:
        context = _context(await _add_user(session), mfa_verified=False)
        await authorize(context, session, action="security.read", resource="x", require_mfa=True)

    run_db(_work)
    events = _events(action="security.read")
    assert events and events[-1].decision == "allow" and "disabled" in events[-1].reason


# --- Incident response -------------------------------------------------------------------------


def test_denial_burst_opens_one_incident_and_revokes_the_session(enforced: Settings) -> None:
    """DENIAL_THRESHOLD denials from one device must open exactly one incident (not one per
    denial) and revoke that device's live session so its token dies."""

    async def _work(session: AsyncSession) -> tuple[int, SecuritySession]:
        user = await _add_user(session)
        security_session = await create_security_session(session, user, user.device_id)
        await session.flush()
        context = _context(user, mfa_verified=True, session_id=security_session.session_id)
        for _ in range(DENIAL_THRESHOLD):
            await record_security_event(
                session, context, action="trip.read", resource="trip:x",
                decision="deny", reason="forced denial",
            )
        incident_count = await session.scalar(
            select(func.count(col(SecurityIncident.id))).where(
                col(SecurityIncident.device_id) == user.device_id
            )
        )
        await session.refresh(security_session)
        return incident_count or 0, security_session

    incident_count, security_session = run_db(_work)
    assert incident_count == 1, "a denial burst must open exactly one incident, not pile them up"
    assert security_session.revoked_at is not None, "the offending session must be revoked"


def test_one_denial_below_the_threshold_opens_no_incident(enforced: Settings) -> None:
    """The off-by-one boundary: THRESHOLD-1 denials must not trip containment (guards >= vs >)."""

    async def _work(session: AsyncSession) -> int:
        context = _context(await _add_user(session, device_id="device-quiet"), mfa_verified=True)
        for _ in range(DENIAL_THRESHOLD - 1):
            await record_security_event(
                session, context, action="trip.read", resource="trip:x",
                decision="deny", reason="forced denial",
            )
        return await session.scalar(
            select(func.count(col(SecurityIncident.id))).where(
                col(SecurityIncident.device_id) == "device-quiet"
            )
        ) or 0

    assert run_db(_work) == 0


def test_concurrent_denials_open_one_incident_and_revoke_the_session(
    enforced: Settings,
) -> None:
    async def _work() -> tuple[int, bool]:
        engine = create_async_engine(TEST_DATABASE_URL)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with session_factory() as session:
                user = await _add_user(session)
                security_session = await create_security_session(
                    session, user, user.device_id
                )
                context = _context(
                    user,
                    mfa_verified=True,
                    session_id=security_session.session_id,
                )

            start = asyncio.Barrier(DENIAL_THRESHOLD)

            async def _record_denial() -> None:
                await start.wait()
                async with session_factory() as session:
                    await record_security_event(
                        session,
                        context,
                        action="trip.read",
                        resource="trip:x",
                        decision="deny",
                        reason="forced concurrent denial",
                    )

            await asyncio.gather(
                *(_record_denial() for _ in range(DENIAL_THRESHOLD))
            )

            async with session_factory() as session:
                incident_count = await session.scalar(
                    select(func.count(col(SecurityIncident.id))).where(
                        col(SecurityIncident.tenant_id) == context.tenant_id,
                        col(SecurityIncident.device_id) == context.device_id,
                        col(SecurityIncident.status) == "open",
                    )
                )
                persisted_session = await session.scalar(
                    select(SecuritySession).where(
                        col(SecuritySession.session_id) == context.session_id
                    )
                )
                assert persisted_session is not None
                return incident_count or 0, persisted_session.revoked_at is not None
        finally:
            await engine.dispose()

    assert asyncio.run(_work()) == (1, True)
