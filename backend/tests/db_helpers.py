"""Direct-DB helpers for tests: seed a booking/trip into a precise state and read facts back.

``run_db`` runs each unit of work on its own short-lived engine/loop so seed connections never
share the app's portal event loop (see conftest for why that separation matters).
"""

from collections.abc import Awaitable, Callable
from datetime import timedelta
import os
from typing import Any, TypeVar

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import col

from app.models import (
    AgentRun,
    AgentRunStep,
    AgentStepKind,
    BookingTransition,
    ExecutionEvent,
    ExecutionEventKind,
    FitnessLevel,
    FlightResultSource,
    FlightSearchResult,
    HITLBookingLog,
    Itinerary,
    TripRequest,
    User,
    utcnow,
)

_DEFAULT_TEST_AGE = 30
_DEFAULT_TEST_FITNESS_LEVEL = FitnessLevel.MODERATE
from app.state import BookingState

TEST_DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+asyncpg://tony@localhost:5432/travel_agent_test"
)

_ResultT = TypeVar("_ResultT")


def run_db(work: Callable[[AsyncSession], Awaitable[_ResultT]]) -> _ResultT:
    async def _runner() -> _ResultT:
        engine = create_async_engine(TEST_DATABASE_URL)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                result = await work(session)
                await session.commit()
                return result
        finally:
            await engine.dispose()

    import asyncio

    return asyncio.run(_runner())


async def seed_booking(
    session: AsyncSession, *, state: BookingState, expires_in_minutes: int
) -> int:
    """Insert a full trip→flight→booking chain and return the booking log id."""
    user = User()
    session.add(user)
    await session.flush()
    assert user.id is not None

    trip = TripRequest(
        user_id=user.id,
        origin="JFK",
        destination="Paris",
        destination_airport="CDG",
        depart_date="2026-08-01",
        age=_DEFAULT_TEST_AGE,
        fitness_level=_DEFAULT_TEST_FITNESS_LEVEL,
    )
    session.add(trip)
    await session.flush()
    assert trip.id is not None

    flight = FlightSearchResult(
        trip_request_id=trip.id,
        offer_index=0,
        carrier="AF",
        price_usd=512.0,
        currency="USD",
        depart_at="2026-08-01T09:00:00",
        arrive_at="2026-08-01T21:30:00",
        stops=0,
        booking_token="tok-abc",
        raw_offer={
            "flights": [
                {
                    "departure_airport": {"id": "JFK", "date": "2026-08-01", "time": "09:00"},
                    "arrival_airport": {"id": "CDG", "date": "2026-08-01", "time": "21:30"},
                }
            ]
        },
    )
    session.add(flight)
    await session.flush()
    assert flight.id is not None

    confirmed_at = utcnow() if state is BookingState.CONFIRMED else None
    booking = HITLBookingLog(
        trip_request_id=trip.id,
        flight_search_result_id=flight.id,
        state=state,
        requested_by_user_id=user.id,
        expires_at=utcnow() + timedelta(minutes=expires_in_minutes),
        confirmed_at=confirmed_at,
    )
    session.add(booking)
    await session.flush()
    assert booking.id is not None
    return booking.id


async def seed_trip(
    session: AsyncSession,
    *,
    origin: str = "JFK",
    destination_airport: str = "CDG",
    depart_date: str = "2026-08-01",
    return_date: str | None = None,
    age: int = _DEFAULT_TEST_AGE,
    fitness_level: FitnessLevel = _DEFAULT_TEST_FITNESS_LEVEL,
) -> int:
    """Insert a bare user+trip (no flight/booking) and return the trip id."""
    user = User()
    session.add(user)
    await session.flush()
    assert user.id is not None

    trip = TripRequest(
        user_id=user.id,
        origin=origin,
        destination="Paris",
        destination_airport=destination_airport,
        depart_date=depart_date,
        return_date=return_date,
        age=age,
        fitness_level=fitness_level,
    )
    session.add(trip)
    await session.flush()
    assert trip.id is not None
    return trip.id


async def seed_flight_search_results(
    session: AsyncSession,
    trip_id: int,
    *,
    source: FlightResultSource = FlightResultSource.LIVE,
    minutes_ago: int = 0,
) -> list[int]:
    """Attach one flight offer to ``trip_id``, backdated by ``minutes_ago``, and return its id
    wrapped in a list (kept plural since a real search returns several offers)."""
    result = FlightSearchResult(
        trip_request_id=trip_id,
        offer_index=0,
        carrier="AF",
        price_usd=512.0,
        currency="USD",
        depart_at="2026-08-01T09:00:00",
        arrive_at="2026-08-01T21:30:00",
        stops=0,
        booking_token="tok-abc",
        raw_offer={"price": 512.0},
        source=source,
        created_at=utcnow() - timedelta(minutes=minutes_ago),
    )
    session.add(result)
    await session.flush()
    assert result.id is not None
    return [result.id]


async def seed_itinerary(session: AsyncSession, trip_id: int) -> int:
    days: list[dict[str, Any]] = [
        {
            "day_number": 1,
            "summary": "Arrival",
            "activities": [
                {
                    "name": "Check in",
                    "description": "Settle into the hotel.",
                    "intensity": "low",
                    "source_url": "https://example.test/hotel",
                }
            ],
        }
    ]
    itinerary = Itinerary(trip_request_id=trip_id, days=days)
    session.add(itinerary)
    await session.flush()
    assert itinerary.id is not None
    return itinerary.id


async def seed_agent_run(
    session: AsyncSession,
    trip_id: int,
    *,
    total_input_tokens: int = 1000,
    total_output_tokens: int = 200,
) -> int:
    """Insert one AgentRun with a single MODEL step and return the run id."""
    agent_run = AgentRun(
        trip_request_id=trip_id,
        status="completed",
        model="llama-3.3-70b-versatile",
        total_input_tokens=total_input_tokens,
        total_output_tokens=total_output_tokens,
        total_ms=1500,
        finished_at=utcnow(),
    )
    session.add(agent_run)
    await session.flush()
    assert agent_run.id is not None

    session.add(
        AgentRunStep(
            agent_run_id=agent_run.id,
            seq=1,
            kind=AgentStepKind.MODEL,
            name="llama-3.3-70b-versatile",
            status="completed",
            duration_ms=1500,
            output_summary="Here is your itinerary.",
            tokens=total_output_tokens,
        )
    )
    await session.flush()
    return agent_run.id


async def seed_execution_event(
    session: AsyncSession,
    trip_id: int,
    *,
    agent_run_id: int | None = None,
    name: str = "search_flights",
) -> int:
    event = ExecutionEvent(
        trip_request_id=trip_id,
        agent_run_id=agent_run_id,
        seq=1,
        kind=ExecutionEventKind.API_CALL,
        name=name,
        status="ok",
        detail="3 offers",
        duration_ms=250,
    )
    session.add(event)
    await session.flush()
    assert event.id is not None
    return event.id


async def get_trip(session: AsyncSession, trip_id: int) -> TripRequest:
    trip = await session.get(TripRequest, trip_id)
    assert trip is not None, f"trip {trip_id} vanished from the DB"
    return trip


async def get_flight_search_results(
    session: AsyncSession, trip_id: int
) -> list[FlightSearchResult]:
    return list(
        await session.scalars(
            select(FlightSearchResult).where(col(FlightSearchResult.trip_request_id) == trip_id)
        )
    )


async def get_itinerary(session: AsyncSession, trip_id: int) -> Itinerary | None:
    return await session.scalar(
        select(Itinerary).where(col(Itinerary.trip_request_id) == trip_id)
    )


async def get_execution_events(session: AsyncSession, trip_id: int) -> list[ExecutionEvent]:
    return list(
        await session.scalars(
            select(ExecutionEvent)
            .where(col(ExecutionEvent.trip_request_id) == trip_id)
            .order_by(col(ExecutionEvent.seq))
        )
    )


async def get_agent_runs(session: AsyncSession, trip_id: int) -> list[AgentRun]:
    return list(
        await session.scalars(
            select(AgentRun)
            .where(col(AgentRun.trip_request_id) == trip_id)
            .order_by(col(AgentRun.started_at))
        )
    )


async def get_booking(session: AsyncSession, log_id: int) -> HITLBookingLog:
    booking = await session.get(HITLBookingLog, log_id)
    assert booking is not None, f"booking log {log_id} vanished from the DB"
    return booking


async def count_transitions_into(session: AsyncSession, log_id: int, to_state: BookingState) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(BookingTransition)
        .where(
            col(BookingTransition.booking_log_id) == log_id,
            col(BookingTransition.to_state) == to_state,
        )
    )
    return result.scalar_one()
