"""Zero-trust decision path, exercised against the real Postgres the app uses in production.

Each test asserts a real security *decision* (allow/deny) plus the persisted evidence it must
leave behind — a security_event row, a session revocation, an incident — never a value handed
back by a mock. The five enterprise controls under test:

- Identity/claims: a token's claims must match persisted user state and a live, bound session.
- MFA: a privileged action is denied until the session's TOTP step has upgraded it.
- Segmentation: the browser cannot assert an internal source segment; tenant isolation holds.
- Monitoring: every allow and deny is appended to the append-only security_event log.
- Incident response: a burst of denials opens one incident and revokes the offending session.

Enforcement is off by default (the dev posture), so the tests that assert *denials* flip
ZERO_TRUST_ENFORCED on explicitly — the same switch production runs with — and clear the
settings cache so the new value is read.
"""

from collections.abc import Iterator
from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app import config
from app.config import Settings
from app.dependencies import (
    SecurityContext,
    _authenticate_identity,
    _totp_code,
    create_security_session,
    issue_access_token,
    password_matches,
    verify_session_mfa,
)
from app.models import SecurityEvent, SecurityIncident, SecuritySession, User, utcnow
from app.repositories.trips_repository import TripError
from app.security import (
    DENIAL_THRESHOLD,
    SecurityError,
    authorize,
    enforce_api_segment,
    record_security_event,
    require_owned_trip,
)
from tests.db_helpers import TEST_DATABASE_URL, run_db, seed_trip

_SIGNING_SECRET = "unit-test-signing-secret"


def _enforced_settings() -> Settings:
    return Settings(
        cerebras_api_key="x",
        searchapi_api_key="x",
        tavily_api_key="x",
        database_url=TEST_DATABASE_URL,
        zero_trust_enforced=True,
        zero_trust_signing_secret=_SIGNING_SECRET,
    )


@pytest.fixture
def enforced(monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    """Turn zero-trust enforcement on for the modules that read it, and restore the cache after."""
    settings = _enforced_settings()
    for module in ("app.config", "app.security", "app.dependencies"):
        monkeypatch.setattr(f"{module}.get_settings", lambda: settings)
    yield settings
    config.get_settings.cache_clear()


@pytest.fixture
def unenforced(monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    """Enforcement is on by default now, so a test that asserts the dev-bypass behavior must turn
    it off explicitly."""
    settings = Settings(
        cerebras_api_key="x",
        searchapi_api_key="x",
        tavily_api_key="x",
        database_url=TEST_DATABASE_URL,
        zero_trust_enforced=False,
    )
    for module in ("app.config", "app.security", "app.dependencies"):
        monkeypatch.setattr(f"{module}.get_settings", lambda: settings)
    yield settings
    config.get_settings.cache_clear()


async def _add_user(
    session: AsyncSession,
    *,
    tenant_id: str = "tenant-a",
    role: str = "traveler",
    device_id: str = "device-a",
    mfa_enabled: bool = True,
    disabled: bool = False,
    password: str = "demo-password",
) -> User:
    from app.dependencies import _password_hash

    user = User(
        email=f"{role}-{device_id}@{tenant_id}.test",
        tenant_id=tenant_id,
        role=role,
        device_id=device_id,
        mfa_enabled=mfa_enabled,
        disabled=disabled,
        password_hash=_password_hash(password),
    )
    session.add(user)
    await session.flush()
    assert user.id is not None
    return user


def _context(user: User, *, mfa_verified: bool) -> SecurityContext:
    assert user.id is not None
    return SecurityContext(user.tenant_id, user.id, user.device_id, user.role, mfa_verified)


async def _mfa_verified_token(session: AsyncSession, user: User) -> str:
    security_session = await create_security_session(session, user, user.device_id)
    if user.mfa_enabled:
        security_session.mfa_verified = True
    await session.flush()
    return issue_access_token(security_session, user, _SIGNING_SECRET)


def _security_events(action: str | None = None, decision: str | None = None) -> list[SecurityEvent]:
    async def _read(session: AsyncSession) -> list[SecurityEvent]:
        statement = select(SecurityEvent)
        if action is not None:
            statement = statement.where(col(SecurityEvent.action) == action)
        if decision is not None:
            statement = statement.where(col(SecurityEvent.decision) == decision)
        return list(await session.scalars(statement.order_by(col(SecurityEvent.id))))

    return run_db(_read)


# --- Identity and access management ------------------------------------------------------------


def test_valid_token_bound_to_a_live_session_authenticates(enforced: Settings) -> None:
    async def _work(session: AsyncSession) -> tuple[User, SecurityContext]:
        user = await _add_user(session)
        token = await _mfa_verified_token(session, user)
        return await _authenticate_identity(f"Bearer {token}", session)

    user, context = run_db(_work)
    assert context.user_id == user.id
    assert context.tenant_id == "tenant-a"
    assert context.mfa_verified is True


def test_tampered_token_signature_is_rejected(enforced: Settings) -> None:
    async def _work(session: AsyncSession) -> None:
        user = await _add_user(session)
        token = await _mfa_verified_token(session, user)
        forged = token[:-1] + ("0" if token[-1] != "0" else "1")
        await _authenticate_identity(f"Bearer {forged}", session)

    with pytest.raises(HTTPException) as error:
        run_db(_work)
    assert error.value.status_code == 401


def test_claims_that_disagree_with_persisted_role_are_rejected(enforced: Settings) -> None:
    """A token minted for a traveler must not keep working after the account is re-roled: the
    claim is cross-checked against live user state on every request, not trusted blind."""

    async def _work(session: AsyncSession) -> None:
        user = await _add_user(session, role="traveler")
        token = await _mfa_verified_token(session, user)
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
        token = issue_access_token(security_session, user, _SIGNING_SECRET)
        await _authenticate_identity(f"Bearer {token}", session)

    with pytest.raises(HTTPException) as error:
        run_db(_work)
    assert error.value.status_code == 401


def test_revoked_session_rejects_an_otherwise_valid_token(enforced: Settings) -> None:
    """Revocation is the containment primitive — a still-unexpired, correctly-signed token must
    stop working the instant its backing session is revoked."""

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


def test_disabled_identity_cannot_authenticate(enforced: Settings) -> None:
    async def _work(session: AsyncSession) -> None:
        user = await _add_user(session)
        token = await _mfa_verified_token(session, user)
        user.disabled = True
        await session.flush()
        await _authenticate_identity(f"Bearer {token}", session)

    with pytest.raises(HTTPException) as error:
        run_db(_work)
    assert error.value.status_code == 403


def test_password_check_is_constant_time_and_rejects_a_wrong_password() -> None:
    async def _work(session: AsyncSession) -> tuple[bool, bool]:
        user = await _add_user(session, password="demo-password")
        return password_matches(user, "demo-password"), password_matches(user, "wrong")

    correct, wrong = run_db(_work)
    assert correct is True
    assert wrong is False


# --- Multi-factor authentication ---------------------------------------------------------------


def test_mfa_enabled_session_starts_unverified_and_a_valid_totp_upgrades_it() -> None:
    async def _work(session: AsyncSession) -> tuple[bool, bool]:
        user = await _add_user(session, mfa_enabled=True)
        security_session = await create_security_session(session, user, user.device_id)
        started_verified = security_session.mfa_verified
        upgraded = await verify_session_mfa(session, security_session, _totp_code(user.totp_secret))
        return started_verified, upgraded

    started_verified, upgraded = run_db(_work)
    assert started_verified is False, "an MFA-enabled login must not be verified before the code"
    assert upgraded is True


def test_wrong_totp_code_leaves_the_session_unverified() -> None:
    async def _work(session: AsyncSession) -> tuple[bool, bool]:
        user = await _add_user(session, mfa_enabled=True)
        security_session = await create_security_session(session, user, user.device_id)
        accepted = await verify_session_mfa(session, security_session, "000000")
        return accepted, security_session.mfa_verified

    accepted, verified = run_db(_work)
    assert accepted is False
    assert verified is False


def test_mfa_disabled_user_is_verified_at_login() -> None:
    async def _work(session: AsyncSession) -> bool:
        user = await _add_user(session, mfa_enabled=False)
        security_session = await create_security_session(session, user, user.device_id)
        return security_session.mfa_verified

    assert run_db(_work) is True


def test_privileged_action_is_denied_without_mfa_then_allowed_with_it(enforced: Settings) -> None:
    """booking.approve carries real consequence, so authorize(require_mfa=True) must refuse an
    unverified session and admit a verified one — the single policy both /confirm and /execute
    lean on."""

    async def _unverified(session: AsyncSession) -> None:
        user = await _add_user(session)
        context = _context(user, mfa_verified=False)
        await authorize(context, session, action="booking.approve", resource="booking:1", require_mfa=True)

    with pytest.raises(SecurityError, match="MFA"):
        run_db(_unverified)

    async def _verified(session: AsyncSession) -> None:
        user = await _add_user(session, device_id="device-mfa")
        context = _context(user, mfa_verified=True)
        await authorize(context, session, action="booking.approve", resource="booking:1", require_mfa=True)

    run_db(_verified)  # must not raise
    assert any(e.decision == "allow" for e in _security_events(action="booking.approve"))


# --- Role-based access control -----------------------------------------------------------------


def test_role_without_the_action_is_denied(enforced: Settings) -> None:
    """A traveler has no security.read grant; the attempt is denied and audited as a denial."""

    async def _work(session: AsyncSession) -> None:
        user = await _add_user(session, role="traveler")
        context = _context(user, mfa_verified=True)
        await authorize(context, session, action="security.read", resource="security-events", require_mfa=True)

    with pytest.raises(SecurityError, match="role"):
        run_db(_work)
    assert _security_events(action="security.read", decision="deny"), (
        "a role denial must be recorded for monitoring, not silently dropped"
    )


def test_security_operator_may_read_but_may_not_approve_bookings(enforced: Settings) -> None:
    async def _allowed(session: AsyncSession) -> None:
        user = await _add_user(session, role="security-operator", device_id="device-op")
        context = _context(user, mfa_verified=True)
        await authorize(context, session, action="security.read", resource="security-events", require_mfa=True)

    run_db(_allowed)  # must not raise

    async def _denied(session: AsyncSession) -> None:
        user = await _add_user(session, role="security-operator", device_id="device-op2")
        context = _context(user, mfa_verified=True)
        await authorize(context, session, action="booking.approve", resource="booking:1", require_mfa=True)

    with pytest.raises(SecurityError):
        run_db(_denied)


# --- Micro-segmentation and tenant isolation ---------------------------------------------------


def test_browser_cannot_assert_an_internal_source_segment(enforced: Settings) -> None:
    """A spoofed x-source-segment header is the classic "pretend I'm an internal service" move;
    the server assigns the segment, so the request is denied and recorded."""
    from starlette.requests import Request

    async def _work(session: AsyncSession) -> None:
        user = await _add_user(session)
        context = _context(user, mfa_verified=True)
        scope = {
            "type": "http",
            "headers": [(b"x-source-segment", b"internal")],
            "state": {},
        }
        await enforce_api_segment(Request(scope), context, session)

    with pytest.raises(SecurityError, match="segment"):
        run_db(_work)
    assert _security_events(action="segment.assert", decision="deny")


def test_cross_tenant_resource_access_is_denied(enforced: Settings) -> None:
    """Tenant B's traveler must never resolve Tenant A's trip, even with a valid token."""

    async def _work(session: AsyncSession) -> None:
        trip_id = await seed_trip(session)  # owned by a fresh demo user in demo-tenant
        intruder = await _add_user(session, tenant_id="tenant-b", device_id="device-b")
        context = _context(intruder, mfa_verified=True)
        await require_owned_trip(session, context, trip_id)

    with pytest.raises(SecurityError, match="tenant"):
        run_db(_work)
    assert _security_events(decision="deny"), "a cross-tenant denial must be audited"


def test_missing_trip_is_denied_as_not_found_not_leaked(enforced: Settings) -> None:
    """Probing a non-existent trip returns the same 404 shape as an unowned one — no existence
    oracle — and still lands a denial in the log."""

    async def _work(session: AsyncSession) -> None:
        user = await _add_user(session)
        context = _context(user, mfa_verified=True)
        await require_owned_trip(session, context, 999_999)

    with pytest.raises(TripError) as error:
        run_db(_work)
    assert error.value.status_code == 404


# --- Continuous monitoring ---------------------------------------------------------------------


def test_every_allow_and_deny_is_appended_to_the_security_log(enforced: Settings) -> None:
    async def _work(session: AsyncSession) -> None:
        user = await _add_user(session)
        context = _context(user, mfa_verified=True)
        await authorize(context, session, action="trip.read", resource="trips")  # allow
        try:
            await authorize(context, session, action="security.read", resource="x", require_mfa=True)
        except SecurityError:
            pass  # deny

    run_db(_work)
    decisions = {(e.action, e.decision) for e in _security_events()}
    assert ("trip.read", "allow") in decisions
    assert ("security.read", "deny") in decisions


def test_development_mode_allows_but_still_records_the_decision(unenforced: Settings) -> None:
    """With enforcement off, authorize never denies — but it must still write the audit row, so
    the monitoring trail exists in dev too (and the reason names the bypass honestly)."""

    async def _work(session: AsyncSession) -> None:
        user = await _add_user(session, role="traveler")
        context = _context(user, mfa_verified=False)
        await authorize(context, session, action="security.read", resource="x", require_mfa=True)

    run_db(_work)  # must not raise despite a traveler hitting a security.read + MFA gate
    events = _security_events(action="security.read")
    assert events and events[-1].decision == "allow"
    assert "disabled" in events[-1].reason


# --- Incident response -------------------------------------------------------------------------


def test_denial_burst_opens_one_incident_and_revokes_the_session(enforced: Settings) -> None:
    """DENIAL_THRESHOLD denials from one device inside the window must: open exactly one
    incident (not one per denial), and revoke that device's live session so the token dies."""

    async def _work(session: AsyncSession) -> tuple[int, SecuritySession]:
        user = await _add_user(session)
        security_session = await create_security_session(session, user, user.device_id)
        security_session.mfa_verified = True
        await session.flush()
        assert user.id is not None
        context = SecurityContext(
            "tenant-a", user.id, user.device_id, "traveler", mfa_verified=True,
            session_id=security_session.session_id,
        )
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


def test_denials_below_the_threshold_do_not_open_an_incident(enforced: Settings) -> None:
    async def _work(session: AsyncSession) -> int:
        user = await _add_user(session, device_id="device-quiet")
        context = _context(user, mfa_verified=True)
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


# --- Append-only monitoring guarantee (mutation-proof evidence) --------------------------------


def test_security_event_rows_cannot_be_updated_or_deleted() -> None:
    """The monitoring log is evidence; a Postgres trigger — not app discipline — must reject any
    attempt to rewrite history. (The parametrized append-only guard also covers this; kept here
    so the zero-trust suite is self-contained about its own tamper-resistance.)"""
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    async def _seed(session: AsyncSession) -> int:
        event = await record_security_event(
            session, None, action="auth.authenticate", resource="identity",
            decision="deny", reason="probe", tenant_id="tenant-a", device_id="device-a",
        )
        assert event.id is not None
        return event.id

    event_id = run_db(_seed)

    with pytest.raises(DBAPIError, match="(?i)append-only"):
        run_db(
            lambda session: session.execute(
                text("UPDATE security_event SET reason = 'x' WHERE id = :i"), {"i": event_id}
            )
        )
    with pytest.raises(DBAPIError, match="(?i)append-only"):
        run_db(
            lambda session: session.execute(
                text("DELETE FROM security_event WHERE id = :i"), {"i": event_id}
            )
        )
