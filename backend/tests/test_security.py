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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import col

from app.config import Settings
from app.dependencies import (
    SecurityContext,
    _authenticate_identity,
    create_security_session,
    issue_access_token,
)
from app.models import SecurityEvent, SecurityIncident, SecuritySession, User, utcnow
from app.repositories.booking_repository import BookingError
from app.repositories.trips_repository import TripError
from app.security import (
    DENIAL_THRESHOLD,
    SecurityError,
    authorize,
    enforce_api_segment,
    record_security_event,
    require_owned_booking,
    require_owned_trip,
)
from app.state import BookingState
from tests.db_helpers import TEST_DATABASE_URL, run_db, seed_booking, seed_trip

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
    for module in ("app.config", "app.security", "app.dependencies"):
        monkeypatch.setattr(f"{module}.get_settings", lambda: settings)
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
    return issue_access_token(security_session, user, _SIGNING_SECRET)


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
# One test per rejection cause, because each is a separate branch in _authenticate_identity and a
# separate bug if it regresses: a good token, a forged signature, stale claims, expiry, and
# revocation. They are not interchangeable — e.g. revocation and expiry are different code paths,
# and a bug can break one while the other still passes.


def test_valid_token_bound_to_a_live_session_authenticates(enforced: Settings) -> None:
    async def _work(session: AsyncSession) -> tuple[User, SecurityContext]:
        user = await _add_user(session)
        return await _authenticate_identity(f"Bearer {await _token(session, user)}", session)

    user, context = run_db(_work)
    assert context.user_id == user.id
    assert context.mfa_verified is True


def test_tampered_token_signature_is_rejected(enforced: Settings) -> None:
    async def _work(session: AsyncSession) -> None:
        user = await _add_user(session)
        token = await _token(session, user)
        forged = token[:-1] + ("0" if token[-1] != "0" else "1")
        await _authenticate_identity(f"Bearer {forged}", session)

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
        await _authenticate_identity(f"Bearer {token}", session)

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
        await _authenticate_identity(
            f"Bearer {issue_access_token(security_session, user, _SIGNING_SECRET)}", session
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
        token = issue_access_token(security_session, user, _SIGNING_SECRET)
        security_session.revoked_at = utcnow()
        await session.flush()
        await _authenticate_identity(f"Bearer {token}", session)

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


def test_browser_cannot_assert_an_internal_source_segment(enforced: Settings) -> None:
    """A spoofed x-source-segment header is the "pretend I'm an internal service" move; the
    server assigns the segment, so the request is denied and the denial recorded."""
    from starlette.requests import Request

    async def _work(session: AsyncSession) -> None:
        user = await _add_user(session)
        scope = {"type": "http", "headers": [(b"x-source-segment", b"internal")], "state": {}}
        await enforce_api_segment(Request(scope), _context(user, mfa_verified=True), session)

    with pytest.raises(SecurityError, match="segment"):
        run_db(_work)
    assert _events(action="segment.assert", decision="deny")


def test_cross_tenant_resource_access_is_denied(enforced: Settings) -> None:
    """A caller cannot distinguish another tenant's trip from an absent trip."""

    async def _work(session: AsyncSession) -> tuple[TripError, TripError]:
        trip_id = await seed_trip(session)
        intruder = await _add_user(session, tenant_id="tenant-b", device_id="device-b")
        context = _context(intruder, mfa_verified=True)
        with pytest.raises(TripError) as hidden:
            await require_owned_trip(session, context, trip_id)
        with pytest.raises(TripError) as absent:
            await require_owned_trip(session, context, trip_id + 1)
        return hidden.value, absent.value

    hidden, absent = run_db(_work)
    assert (hidden.code, hidden.status_code) == (absent.code, absent.status_code) == (
        "trip_not_found",
        404,
    )
    assert _events(decision="deny")


def test_cross_tenant_booking_is_indistinguishable_from_missing(enforced: Settings) -> None:
    async def _work(session: AsyncSession) -> tuple[BookingError, BookingError]:
        booking_id = await seed_booking(
            session,
            state=BookingState.PENDING_USER_CONFIRMATION,
            expires_in_minutes=30,
        )
        intruder = await _add_user(session, tenant_id="tenant-b", device_id="device-b")
        context = _context(intruder, mfa_verified=True)
        with pytest.raises(BookingError) as hidden:
            await require_owned_booking(session, context, booking_id)
        with pytest.raises(BookingError) as absent:
            await require_owned_booking(session, context, booking_id + 1)
        return hidden.value, absent.value

    hidden, absent = run_db(_work)
    assert (hidden.code, hidden.status_code) == (absent.code, absent.status_code) == (
        "booking_not_found",
        404,
    )


# --- Continuous monitoring ---------------------------------------------------------------------


def test_both_allow_and_deny_decisions_are_recorded(enforced: Settings) -> None:
    async def _work(session: AsyncSession) -> None:
        context = _context(await _add_user(session), mfa_verified=True)
        await authorize(context, session, action="trip.read", resource="trips")
        try:
            await authorize(context, session, action="security.read", resource="x", require_mfa=True)
        except SecurityError:
            pass

    run_db(_work)
    decisions = {(e.action, e.decision) for e in _events()}
    assert ("trip.read", "allow") in decisions
    assert ("security.read", "deny") in decisions


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


def test_denial_threshold_is_scoped_to_one_tenant_and_device(enforced: Settings) -> None:
    async def _work(session: AsyncSession) -> tuple[int, SecuritySession]:
        tenant_a = await _add_user(
            session, tenant_id="tenant-a", device_id="shared-device"
        )
        tenant_b = await _add_user(
            session, tenant_id="tenant-b", device_id="shared-device"
        )
        for _ in range(DENIAL_THRESHOLD - 1):
            await record_security_event(
                session,
                _context(tenant_a, mfa_verified=True),
                action="trip.read",
                resource="trip:x",
                decision="deny",
                reason="forced denial",
            )
        security_session = await create_security_session(
            session, tenant_b, tenant_b.device_id
        )
        await record_security_event(
            session,
            _context(
                tenant_b,
                mfa_verified=True,
                session_id=security_session.session_id,
            ),
            action="trip.read",
            resource="trip:x",
            decision="deny",
            reason="forced denial",
        )
        incident_count = await session.scalar(
            select(func.count(col(SecurityIncident.id)))
        )
        await session.refresh(security_session)
        return incident_count or 0, security_session

    incident_count, security_session = run_db(_work)
    assert incident_count == 0
    assert security_session.revoked_at is None


def test_only_one_open_incident_exists_per_tenant_device(enforced: Settings) -> None:
    async def _insert_duplicates(session: AsyncSession) -> None:
        session.add_all([
            SecurityIncident(tenant_id="tenant-a", device_id="device-a", reason="first"),
            SecurityIncident(tenant_id="tenant-a", device_id="device-a", reason="second"),
        ])

    with pytest.raises(IntegrityError):
        run_db(_insert_duplicates)


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
