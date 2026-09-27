"""The audit guarantee is enforced by the DATABASE, not app discipline: BEFORE UPDATE/DELETE
triggers on booking_transition and execution_event raise. If the trigger migration is ever
dropped, these go red — that is the whole point.
"""

from collections.abc import Awaitable, Callable

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import BookingTransition, ExecutionEvent, ExecutionEventKind, SecurityEvent
from app.state import BookingState
from tests.db_helpers import get_booking, run_db, seed_booking


async def _seed_transition(session: AsyncSession) -> int:
    log_id = await seed_booking(session, state=BookingState.CONFIRMED, expires_in_minutes=30)
    transition = BookingTransition(
        booking_log_id=log_id,
        from_state=BookingState.PENDING_USER_CONFIRMATION,
        to_state=BookingState.CONFIRMED,
        reason="confirm",
    )
    session.add(transition)
    await session.flush()
    assert transition.id is not None
    return transition.id


async def _seed_event(session: AsyncSession) -> int:
    log_id = await seed_booking(
        session, state=BookingState.PENDING_USER_CONFIRMATION, expires_in_minutes=30
    )
    booking = await get_booking(session, log_id)
    event = ExecutionEvent(
        trip_request_id=booking.trip_request_id,
        seq=1,
        kind=ExecutionEventKind.HITL,
        name="booking.request",
        status="ok",
        detail="seed",
    )
    session.add(event)
    await session.flush()
    assert event.id is not None
    return event.id


async def _seed_security_event(session: AsyncSession) -> int:
    event = SecurityEvent(
        tenant_id="tenant-a",
        device_id="device-a",
        source_segment="web",
        target_segment="api",
        action="test",
        resource="test",
        decision="allow",
        reason="test",
        correlation_id="correlation-test",
    )
    session.add(event)
    await session.flush()
    assert event.id is not None
    return event.id


@pytest.mark.parametrize(
    ("table", "seed", "column", "original"),
    [
        ("booking_transition", _seed_transition, "reason", "confirm"),
        ("execution_event", _seed_event, "status", "ok"),
        ("security_event", _seed_security_event, "reason", "test"),
    ],
)
def test_audit_rows_are_append_only(
    table: str, seed: Callable[[AsyncSession], Awaitable[int]], column: str, original: str
) -> None:
    row_id = run_db(seed)

    with pytest.raises(DBAPIError) as update_error:
        run_db(
            lambda session: session.execute(
                text(f"UPDATE {table} SET {column} = 'tampered' WHERE id = :row_id"),
                {"row_id": row_id},
            )
        )
    assert "append-only" in str(update_error.value).lower(), (
        f"UPDATE on {table} must be blocked by the append-only trigger, not silently allowed"
    )

    with pytest.raises(DBAPIError, match="(?i)append-only"):
        run_db(
            lambda session: session.execute(
                text(f"DELETE FROM {table} WHERE id = :row_id"), {"row_id": row_id}
            )
        )

    surviving = run_db(
        lambda session: session.scalar(
            text(f"SELECT {column} FROM {table} WHERE id = :row_id"), {"row_id": row_id}
        )
    )
    assert surviving == original, (
        f"{table}.{column} was mutated despite the append-only trigger (got {surviving!r})"
    )
