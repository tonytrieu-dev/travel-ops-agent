import pytest
from sqlalchemy import select
from sqlmodel import col

from app.models import User
from scripts.enroll_identity import enroll_identity
from tests.db_helpers import run_db


@pytest.mark.parametrize("account", ["missing", "disabled", "enrolled"])
def test_enrollment_cannot_create_enable_or_replace_credentials(account) -> None:
    async def _work(session):
        if account != "missing":
            session.add(User(
                email="existing@example.test", disabled=account == "disabled",
                password_hash="existing-hash" if account == "enrolled" else "",
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


@pytest.mark.no_database
@pytest.mark.parametrize("password", ["", "short", "x" * 201])
def test_enrollment_rejects_unusable_password_before_database_access(password) -> None:
    import asyncio
    from unittest.mock import AsyncMock

    session = AsyncMock()
    with pytest.raises(ValueError, match="12 to 200"):
        asyncio.run(enroll_identity(session, "existing@example.test", password))
    session.scalar.assert_not_called()
