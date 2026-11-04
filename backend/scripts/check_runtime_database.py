"""Assert runtime database credentials cannot dismantle append-only evidence."""
import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.exc import DBAPIError

from app.config import get_settings


async def main() -> None:
    engine = create_async_engine(get_settings().database_url)
    try:
        async with engine.connect() as connection:
            for table in ("security_event", "booking_transition", "execution_event", "compliance_assessment"):
                for statement in (
                    f"UPDATE {table} SET id=id", f"DELETE FROM {table}",
                    f"TRUNCATE {table}", f"ALTER TABLE {table} DISABLE TRIGGER ALL",
                    f"DROP TABLE {table} CASCADE",
                ):
                    transaction = await connection.begin()
                    try:
                        await connection.execute(text(statement))
                    except DBAPIError as error:
                        assert getattr(error.orig, "sqlstate", None) == "42501", str(error)
                    else:
                        raise AssertionError(f"runtime bypassed evidence protection: {statement}")
                    finally:
                        await transaction.rollback()
    finally:
        await engine.dispose()
    print("Runtime audit protections verified.")


if __name__ == "__main__":
    asyncio.run(main())
