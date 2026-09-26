"""Guards the automatic ExecutionEvent recorder (Phase 4): events sequence correctly within a
run and resume correctly across multiple runs on the same trip.
"""

import asyncio
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import col

from app.agent.execution_log import execution_context, record_event
from app.models import ExecutionEvent, ExecutionEventKind
from app.request_context import bind_correlation_id, correlation_id
from tests.db_helpers import TEST_DATABASE_URL, run_db, seed_trip


async def _events_for(session, trip_id: int) -> list[ExecutionEvent]:
    result = await session.execute(
        select(ExecutionEvent)
        .where(col(ExecutionEvent.trip_request_id) == trip_id)
        .order_by(col(ExecutionEvent.seq))
    )
    return list(result.scalars())


@pytest.mark.no_database
async def test_execution_context_inherits_request_correlation_unless_explicitly_overridden() -> None:
    with bind_correlation_id("request-correlation"):
        async with execution_context(MagicMock(), 1):
            assert correlation_id() == "request-correlation"
        async with execution_context(MagicMock(), 1, correlation="workflow-correlation"):
            assert correlation_id() == "workflow-correlation"
        assert correlation_id() == "request-correlation"


def test_recorded_data_survives_as_the_real_structured_payload_not_just_the_detail_string() -> None:
    """detail is a short human summary ("4 offers"); data must carry the real payload behind it
    so the execution panel can render actual offers/results, not just a count."""

    async def _work(session):
        trip_id = await seed_trip(session)
        async with execution_context(session, trip_id):
            await record_event(
                ExecutionEventKind.API_CALL,
                "search_flights",
                "ok",
                "1 offers",
                data={"offers": [{"carrier": "United", "price_usd": 412.0}]},
            )
        return await _events_for(session, trip_id)

    events = run_db(_work)

    assert events[0].data == {"offers": [{"carrier": "United", "price_usd": 412.0}]}


def test_a_second_run_on_the_same_trip_resumes_seq_instead_of_restarting() -> None:
    async def _work(session):
        trip_id = await seed_trip(session)
        async with execution_context(session, trip_id):
            await record_event(ExecutionEventKind.PROTOCOL, "first_run_event", "ok", "run 1")
        async with execution_context(session, trip_id):
            await record_event(ExecutionEventKind.PROTOCOL, "second_run_event", "ok", "run 2")
        return await _events_for(session, trip_id)

    events = run_db(_work)

    assert [event.seq for event in events] == [1, 2]


def test_concurrent_record_event_calls_get_sequential_non_colliding_seq() -> None:
    """record_event() writes go through a per-run asyncio.Lock because AsyncSession isn't safe
    for concurrent use; pydantic_ai runs tool calls from the same model turn concurrently, so
    three record_event() calls racing via asyncio.gather must still land as non-colliding,
    strictly increasing seq numbers rather than colliding or being silently dropped."""

    async def _work(session):
        trip_id = await seed_trip(session)
        async with execution_context(session, trip_id):
            await asyncio.gather(
                record_event(ExecutionEventKind.API_CALL, "a", "ok", "1"),
                record_event(ExecutionEventKind.API_CALL, "b", "ok", "2"),
                record_event(ExecutionEventKind.API_CALL, "c", "ok", "3"),
            )
        return [event.seq for event in await _events_for(session, trip_id)]

    seqs = run_db(_work)
    assert seqs == [1, 2, 3], f"concurrent record_event calls must not collide on seq; got {seqs}"


async def test_two_concurrent_runs_on_the_same_trip_never_collide_on_seq() -> None:
    """A double-submitted /plan opens two execution_context()s for the same trip on two separate
    DB connections, not one — the in-process asyncio.Lock above can't serialize across those, so
    this is the case a locally-cached "next seq" counter would race on and hand out duplicate
    seq values, scrambling the trip's append-only event order."""

    async def _seed() -> int:
        engine = create_async_engine(TEST_DATABASE_URL)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                trip_id = await seed_trip(session)
                await session.commit()
                return trip_id
        finally:
            await engine.dispose()

    async def _run_events(trip_id: int, label: str) -> None:
        engine = create_async_engine(TEST_DATABASE_URL)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                async with execution_context(session, trip_id):
                    for index in range(3):
                        await record_event(
                            ExecutionEventKind.PROTOCOL, f"{label}_{index}", "ok", label
                        )
        finally:
            await engine.dispose()

    trip_id = await _seed()
    await asyncio.gather(_run_events(trip_id, "run_a"), _run_events(trip_id, "run_b"))

    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            events = await _events_for(session, trip_id)
    finally:
        await engine.dispose()

    seqs = [event.seq for event in events]
    assert seqs == list(range(1, 7)), (
        f"two concurrent runs on the same trip must claim distinct, contiguous seq values via "
        f"the trip-row lock, not race on a cached counter; got {seqs}"
    )
