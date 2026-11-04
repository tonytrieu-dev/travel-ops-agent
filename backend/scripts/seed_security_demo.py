"""Seed synthetic identities for the zero-trust walkthrough."""

import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings
from app.repositories.credentials_repository import seed_identities


async def main() -> None:
    engine = create_async_engine(get_settings().database_url)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            await seed_identities(session)
    finally:
        await engine.dispose()
    print("Seeded synthetic users. Password: demo-password. TOTP secret: JBSWY3DPEHPK3PXP")


if __name__ == "__main__":
    asyncio.run(main())
