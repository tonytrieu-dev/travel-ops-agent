import pytest
from sqlalchemy import select
from sqlmodel import col

from app.models import User
from app.repositories.credentials_repository import enroll_identity
from tests.db_helpers import run_db


def test_shared_authenticator_migration_retires_only_affected_credentials() -> None:
    from datetime import timedelta
    from importlib.util import module_from_spec, spec_from_file_location
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    from app.models import SecuritySession, utcnow

    async def work(session):
        affected = User(email="shared@example.test", tenant_id="tenant-a", device_id="device-a",
                        role="trip-planner", totp_secret="JBSWY3DPEHPK3PXP",
                        password_hash="preserve-password", last_totp_time_step=123)
        independent = User(email="independent@example.test", totp_secret="INDEPENDENTSECRET",
                           password_hash="preserve-other-password", last_totp_time_step=456)
        session.add_all([affected, independent])
        await session.flush()
        before = [user.model_dump() for user in (affected, independent)]
        sessions = []
        for user in (affected, independent):
            assert user.id is not None
            sessions.append(SecuritySession(user_id=user.id, tenant_id=user.tenant_id,
                                            device_id=user.device_id, mfa_verified=True,
                                            expires_at=utcnow() + timedelta(minutes=15)))
        session.add_all(sessions)
        await session.flush()

        def upgrade(connection):
            specification = spec_from_file_location("retire_shared_totp", Path(__file__).parents[1] / "alembic/versions/6e0a2c4d8f10_retire_shared_totp.py")
            assert specification is not None and specification.loader is not None
            migration = module_from_spec(specification)
            specification.loader.exec_module(migration)
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()

        await (await session.connection()).run_sync(upgrade)
        for user, expected in zip((affected, independent), before):
            await session.refresh(user)
            if user is affected:
                expected.update(totp_secret="", last_totp_time_step=None)
            assert user.model_dump() == expected
        for security_session in sessions:
            await session.refresh(security_session)
        assert sessions[0].revoked_at is not None
        assert sessions[1].revoked_at is None

    run_db(work)


@pytest.mark.parametrize("account", ["missing", "disabled"])
def test_enrollment_cannot_create_or_enable_accounts(account) -> None:
    async def _work(session):
        if account != "missing":
            session.add(User(
                email="existing@example.test", disabled=account == "disabled",
            ))
            await session.flush()
        existing = await session.scalar(select(User).where(col(User.email) == "existing@example.test"))
        before = existing.model_dump() if existing is not None else None
        with pytest.raises(ValueError):
            await enroll_identity(session, "existing@example.test", "chosen-password")
        await session.flush()
        if existing is not None:
            await session.refresh(existing)
        persisted = await session.scalar(select(User).where(col(User.email) == "existing@example.test"))
        assert (persisted.model_dump() if persisted is not None else None) == before

    run_db(_work)


def test_enrollment_provisions_independent_authenticators() -> None:
    from app.security_identity import create_security_session, totp_code, verify_session_mfa

    async def work(session):
        first = User(email="first@example.test")
        second = User(email="second@example.test")
        session.add_all([first, second])
        await session.commit()
        assert first.email is not None and second.email is not None
        await enroll_identity(session, first.email, "chosen-password")
        await enroll_identity(session, second.email, "chosen-password")
        assert first.totp_secret and second.totp_secret != first.totp_secret
        pending = await create_security_session(session, second, second.device_id)
        assert not await verify_session_mfa(session, pending, totp_code(first.totp_secret))
        assert await verify_session_mfa(session, pending, totp_code(second.totp_secret))
    run_db(work)


def test_synthetic_seed_repairs_retired_mfa_and_revokes_sessions():
    from app.security_identity import create_security_session, totp_code, verify_session_mfa
    from app.repositories.credentials_repository import seed_identities

    async def work(session):
        user = User(email="traveler@tenant-a.test", tenant_id="tenant-a",
                    role="traveler", device_id="device-a-traveler", totp_secret="",
                    last_totp_time_step=123)
        session.add(user)
        await session.commit()
        old_session = await create_security_session(session, user, user.device_id)
        await seed_identities(session)
        await session.refresh(old_session)
        assert old_session.revoked_at is not None
        assert user.last_totp_time_step is None
        pending = await create_security_session(session, user, user.device_id)
        assert await verify_session_mfa(session, pending, totp_code(user.totp_secret))
        user.device_id = "unrelated-device"
        await session.commit()
        previous_secret = user.totp_secret
        with pytest.raises(ValueError, match="mismatched"):
            await seed_identities(session)
        assert user.device_id == "unrelated-device" and user.totp_secret == previous_secret
    run_db(work)


def test_mfa_reset_preserves_password_and_policy_and_invalidates_old_session(monkeypatch):
    from types import SimpleNamespace
    from urllib.parse import parse_qs, urlparse

    from fastapi import HTTPException

    from app import security_identity
    from app.security_identity import (
        authenticate_identity, create_security_session, issue_access_token,
        totp_code, verify_session_mfa,
    )

    timestamp = int(security_identity.time.time())
    monkeypatch.setattr(security_identity, "time", SimpleNamespace(time=lambda: timestamp))

    async def work(session):
        email = "reset@example.test"
        user = User(email=email, tenant_id="tenant-reset",
                    role="traveler", device_id="device-reset")
        session.add(user)
        await session.commit()
        await enroll_identity(session, email, "original-password")
        original_hash = user.password_hash
        original_secret = user.totp_secret
        original_policy = (user.tenant_id, user.role, user.device_id, user.mfa_enabled, user.disabled)
        old_session = await create_security_session(session, user, user.device_id)
        assert await verify_session_mfa(session, old_session, totp_code(original_secret, timestamp))
        old_token = issue_access_token(old_session, user)
        assert user.last_totp_time_step is not None

        provisioning_uri = await enroll_identity(
            session, email, "unused-password", reset_mfa=True,
        )
        await session.refresh(user)
        await session.refresh(old_session)
        assert user.password_hash == original_hash
        assert (user.tenant_id, user.role, user.device_id, user.mfa_enabled, user.disabled) == original_policy
        assert user.totp_secret != original_secret
        assert parse_qs(urlparse(provisioning_uri).query)["secret"] == [user.totp_secret]
        assert user.last_totp_time_step is None
        assert old_session.revoked_at is not None
        with pytest.raises(HTTPException, match="session revoked or expired"):
            await authenticate_identity(f"Bearer {old_token}", session)

        pending = await create_security_session(session, user, user.device_id)
        assert await verify_session_mfa(session, pending, totp_code(user.totp_secret, timestamp))

    run_db(work)
