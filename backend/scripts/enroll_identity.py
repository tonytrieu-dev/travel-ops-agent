"""Enroll an existing local account after migration, preserving its ownership and policy."""

import argparse
import asyncio
from getpass import getpass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import col

from app.config import get_settings
from app.models import User
from app.security_identity import password_hash


async def enroll_identity(session: AsyncSession, email: str, password: str) -> None:
    if not 12 <= len(password) <= 200:
        raise ValueError("password must contain 12 to 200 characters")
    user = await session.scalar(select(User).where(col(User.email) == email).with_for_update())
    if user is None or user.disabled:
        raise ValueError("enabled existing account required")
    if user.password_hash:
        raise ValueError("account is already enrolled")
    user.password_hash = password_hash(password)
    await session.commit()


async def _enroll(email: str, password: str) -> None:
    engine = create_async_engine(get_settings().database_url)
    try:
        async with async_sessionmaker(engine)() as session:
            await enroll_identity(session, email, password)
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True)
    arguments = parser.parse_args()
    password = getpass("New password (12–200 characters): ")
    if password != getpass("Confirm password: "):
        parser.error("passwords do not match")
    try:
        asyncio.run(_enroll(arguments.email, password))
    except ValueError as error:
        parser.error(str(error))
    print("Account enrolled. Existing device and MFA settings still apply.")


if __name__ == "__main__":
    main()
