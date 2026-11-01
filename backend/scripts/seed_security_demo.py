"""Seed synthetic identities for the zero-trust walkthrough."""

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings
from app.models import User
from app.security_identity import password_hash


async def main() -> None:
    engine = create_async_engine(get_settings().database_url)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        identities = (
            ("traveler@tenant-a.test", "tenant-a", "traveler", "device-a-traveler"),
            ("operator@tenant-a.test", "tenant-a", "security-operator", "device-a-operator"),
            ("traveler@tenant-b.test", "tenant-b", "traveler", "device-b-traveler"),
        )
        for email, tenant_id, role, device_id in identities:
            user = await session.scalar(select(User).where(User.email == email))
            if user is None:
                session.add(
                    User(
                        email=email,
                        tenant_id=tenant_id,
                        role=role,
                        device_id=device_id,
                        password_hash=password_hash("demo-password"),
                    )
                )
        await session.commit()
    await engine.dispose()
    print("Seeded synthetic users. Password: demo-password. TOTP secret: JBSWY3DPEHPK3PXP")


if __name__ == "__main__":
    asyncio.run(main())
