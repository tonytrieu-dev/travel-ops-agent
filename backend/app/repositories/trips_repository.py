"""Trip lifecycle: create/update a trip, search flights against it, and generate its itinerary.

Mirrors booking_repository's shape (a domain error carrying code/status/detail, thin functions
taking an already-open session) so the two repositories read the same way.
"""

from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from app.config import FLIGHT_CACHE_TTL_MINUTES
from app.models import (
    AgentRun,
    AgentRunStep,
    ExecutionEvent,
    FlightSearchResult,
    HITLBookingLog,
    Itinerary,
    TripRequest,
    TripStatus,
    utcnow,
)
from app.schemas import (
    ClarificationOut,
    ErrorCode,
    ItineraryOut,
    PlannerOutput,
    PlanTooComplexOut,
    TripRequestCreate,
    TripRequestUpdate,
    validate_trip_dates,
)

PlannerRunner = Callable[[int, str], Awaitable[PlannerOutput]]


class TripError(Exception):
    """A domain rejection carrying the client-facing code, HTTP status, and root-cause detail."""

    def __init__(self, code: ErrorCode, status_code: int, detail: str) -> None:
        self.code = code
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


async def create_trip(session: AsyncSession, user_id: int, data: TripRequestCreate) -> TripRequest:
    trip = TripRequest(user_id=user_id, **data.model_dump())
    session.add(trip)
    await session.commit()
    return trip


async def get_trip(session: AsyncSession, trip_id: int) -> TripRequest:
    """Fetch a persisted trip so the frontend can recover it after a page refresh."""
    trip = await session.get(TripRequest, trip_id)
    if trip is None:
        raise TripError(ErrorCode.TRIP_NOT_FOUND, 404, f"No trip {trip_id}.")
    return trip


async def list_trips(session: AsyncSession, user_id: int) -> list[TripRequest]:
    """Every trip this user has created, newest first — lets the UI recover a trip that a newer
    one has since overwritten as the client's "active" pointer."""
    return list(
        await session.scalars(
            select(TripRequest)
            .where(col(TripRequest.user_id) == user_id)
            .order_by(col(TripRequest.created_at).desc())
        )
    )


# Changing any of these invalidates a trip's flight search and itinerary: the stored offers were
# priced for the old route/dates, and the itinerary was researched for the old destination.
_CRITERIA_FIELDS = ("origin", "destination", "destination_airport", "depart_date", "return_date")
_ITINERARY_CRITERIA_FIELDS = (*_CRITERIA_FIELDS, "age", "fitness_level")


async def _invalidate_trip_derived_data(session: AsyncSession, trip_id: int) -> None:
    """Drop cached flight offers and any generated itinerary so a later /flights/search or /plan
    can't hand back data computed for the trip's previous criteria."""
    stale_flight_results = await session.scalars(
        select(FlightSearchResult).where(col(FlightSearchResult.trip_request_id) == trip_id)
    )
    for flight_result in stale_flight_results:
        await session.delete(flight_result)
    stale_itinerary = await session.scalar(
        select(Itinerary).where(col(Itinerary.trip_request_id) == trip_id)
    )
    if stale_itinerary is not None:
        await session.delete(stale_itinerary)


async def update_trip(session: AsyncSession, trip_id: int, data: TripRequestUpdate) -> TripRequest:
    trip = await session.get(TripRequest, trip_id)
    if trip is None:
        raise TripError(ErrorCode.TRIP_NOT_FOUND, 404, f"No trip {trip_id}.")

    criteria_before = {field_name: getattr(trip, field_name) for field_name in _ITINERARY_CRITERIA_FIELDS}
    patch = data.model_dump(exclude_unset=True)
    route_changed = any(
        field_name in patch and patch[field_name] != criteria_before[field_name]
        for field_name in _CRITERIA_FIELDS
    )
    # A booking references an offer by foreign key; deleting the offer to invalidate it would
    # break the FK and destroy audit history. Reject before any mutation.
    if route_changed and await _trip_has_booking(session, trip_id):
        raise TripError(
            ErrorCode.INVALID_TRANSITION,
            409,
            "This trip has a booking tied to its flight offers; cancel it or start a new trip "
            "before changing the route or dates.",
        )

    for field_name, value in patch.items():
        setattr(trip, field_name, value)

    try:
        validate_trip_dates(trip.depart_date, trip.return_date)
    except ValueError as error:
        await session.rollback()
        raise TripError(ErrorCode.VALIDATION_ERROR, 422, str(error)) from error

    criteria_changed = any(
        getattr(trip, field_name) != criteria_before[field_name] for field_name in _CRITERIA_FIELDS
    )
    if criteria_changed:
        await _invalidate_trip_derived_data(session, trip_id)
        trip.status = TripStatus.CREATED
    elif any(getattr(trip, field_name) != criteria_before[field_name] for field_name in _ITINERARY_CRITERIA_FIELDS):
        itinerary = await session.scalar(select(Itinerary).where(col(Itinerary.trip_request_id) == trip_id))
        if itinerary is not None:
            await session.delete(itinerary)
        trip.status = TripStatus.CREATED

    await session.commit()
    return trip


async def _trip_has_booking(session: AsyncSession, trip_id: int) -> bool:
    return (
        await session.scalar(
            select(HITLBookingLog.id).where(col(HITLBookingLog.trip_request_id) == trip_id).limit(1)
        )
    ) is not None


def cheapest_first(offers: list[FlightSearchResult]) -> list[FlightSearchResult]:
    """The take-home requires surfacing the cheapest flights, and the frontend lists offers in
    the order the API returns them — so ascending price order is a backend guarantee, held on
    every path (fresh live search, own-trip reuse, cross-trip cache) so all three agree. Shared
    with services/flight_search.py, which is the only other caller."""
    return sorted(offers, key=lambda offer: offer.price_usd)


async def get_trip_snapshot(
    session: AsyncSession, trip_id: int
) -> tuple[TripRequest, list[FlightSearchResult], Itinerary | None, bool]:
    trip = await get_trip(session, trip_id)
    batch_started_at = await session.scalar(
        select(FlightSearchResult.created_at)
        .where(
            col(FlightSearchResult.trip_request_id) == trip_id,
            col(FlightSearchResult.offer_index) == 0,
        )
        .order_by(col(FlightSearchResult.created_at).desc())
        .limit(1)
    )
    offers = (
        list(
            await session.scalars(
                select(FlightSearchResult).where(
                    col(FlightSearchResult.trip_request_id) == trip_id,
                    col(FlightSearchResult.created_at) >= batch_started_at,
                )
            )
        )
        if batch_started_at is not None
        else []
    )
    itinerary = await session.scalar(
        select(Itinerary).where(col(Itinerary.trip_request_id) == trip_id)
    )
    is_stale = (
        batch_started_at < utcnow() - timedelta(minutes=FLIGHT_CACHE_TTL_MINUTES)
        if batch_started_at is not None
        else False
    )
    return trip, cheapest_first(offers), itinerary, is_stale


def _build_planner_prompt(trip: TripRequest) -> str:
    parts = [
        f"Plan a trip from {trip.origin} to {trip.destination} (airport {trip.destination_airport}), "
        f"departing {trip.depart_date}"
        + (f", returning {trip.return_date}." if trip.return_date else " (one-way).")
    ]
    parts.append(f"Traveler age: {trip.age}.")
    parts.append(f"Fitness level: {trip.fitness_level.value}.")
    return " ".join(parts)


async def get_or_create_itinerary(
    session: AsyncSession, trip_id: int, run_planner: PlannerRunner
) -> PlannerOutput:
    trip = await session.get(TripRequest, trip_id)
    if trip is None:
        raise TripError(ErrorCode.TRIP_NOT_FOUND, 404, f"No trip {trip_id}.")

    # Serialize planning per trip: a concurrent /plan waits here, then finds the itinerary the
    # first run persisted below and skips a second LLM run. Advisory (not a trip-row lock) because
    # record_event locks the trip row per step and would deadlock against a held row lock.
    await session.execute(text("SELECT pg_advisory_xact_lock(:trip_id)"), {"trip_id": trip_id})

    existing = await session.scalar(
        select(Itinerary).where(col(Itinerary.trip_request_id) == trip_id)
    )
    if existing is not None:
        return ItineraryOut(days=existing.days)

    output = await run_planner(trip_id, _build_planner_prompt(trip))
    if isinstance(output, ClarificationOut | PlanTooComplexOut):
        return output

    # run_planner persists the itinerary inside its durable workflow (so a crash can't lose it) and
    # skips persistence if the trip's criteria changed mid-run; an absent row means the plan is now
    # obsolete. The advisory lock is still held, so the re-read serializes against a concurrent plan.
    persisted = await session.scalar(
        select(Itinerary)
        .where(col(Itinerary.trip_request_id) == trip_id)
        .execution_options(populate_existing=True)
    )
    if persisted is None:
        raise TripError(
            ErrorCode.INVALID_TRANSITION,
            409,
            "The trip changed while it was being planned; plan it again for the new details.",
        )
    return ItineraryOut(days=persisted.days)


async def persist_itinerary_if_current(
    session: AsyncSession, trip_id: int, planned_prompt: str, output: ItineraryOut
) -> bool:
    """Persist a freshly planned itinerary unless the trip's criteria changed since planning began.
    Idempotent: a replayed durable workflow re-persisting the same result is a no-op."""
    trip = await session.get(TripRequest, trip_id)
    if trip is None or _build_planner_prompt(trip) != planned_prompt:
        return False
    if await session.scalar(select(Itinerary).where(col(Itinerary.trip_request_id) == trip_id)):
        return True
    session.add(Itinerary(trip_request_id=trip_id, days=[day.model_dump() for day in output.days]))
    trip.status = TripStatus.ITINERARY_READY
    await session.commit()
    return True


async def _execution_runs_for_trips(
    session: AsyncSession, trip_ids: list[int]
) -> list[tuple[AgentRun, list[AgentRunStep], list[ExecutionEvent]]]:
    """Every run across the given trips, newest first, each with its own steps/events."""
    if not trip_ids:
        return []
    agent_runs = list(
        await session.scalars(
            select(AgentRun)
            .where(col(AgentRun.trip_request_id).in_(trip_ids))
            .order_by(col(AgentRun.started_at).desc())
        )
    )
    all_steps = list(
        await session.scalars(
            select(AgentRunStep)
            .where(col(AgentRunStep.agent_run_id).in_([run.id for run in agent_runs]))
            .order_by(col(AgentRunStep.seq))
        )
    )
    steps_by_run_id: dict[int, list[AgentRunStep]] = defaultdict(list)
    for step in all_steps:
        steps_by_run_id[step.agent_run_id].append(step)
    all_events = list(
        await session.scalars(
            select(ExecutionEvent)
            .where(col(ExecutionEvent.trip_request_id).in_(trip_ids))
            .order_by(col(ExecutionEvent.seq))
        )
    )
    events_by_run_id: dict[int, list[ExecutionEvent]] = defaultdict(list)
    for event in all_events:
        if event.agent_run_id is not None:
            events_by_run_id[event.agent_run_id].append(event)
    return [
        (run, steps_by_run_id[run.id], events_by_run_id[run.id])
        for run in agent_runs
        if run.id
    ]


async def get_execution_panel(
    session: AsyncSession, trip_id: int
) -> tuple[
    list[tuple[AgentRun, list[AgentRunStep], list[ExecutionEvent]]],
    list[ExecutionEvent],
]:
    """Every run with its own steps/events, plus the same event stream for LiveActivity."""
    trip = await session.get(TripRequest, trip_id)
    if trip is None:
        raise TripError(ErrorCode.TRIP_NOT_FOUND, 404, f"No trip {trip_id}.")

    runs_with_details = await _execution_runs_for_trips(session, [trip_id])
    events = list(
        await session.scalars(
            select(ExecutionEvent)
            .where(col(ExecutionEvent.trip_request_id) == trip_id)
            .order_by(col(ExecutionEvent.seq))
        )
    )
    return runs_with_details, events


async def list_execution_runs_for_user(
    session: AsyncSession, user_id: int
) -> list[tuple[AgentRun, list[AgentRunStep], list[ExecutionEvent]]]:
    """Every agent run across every trip this user owns, newest first — the execution history tab
    is global so a run doesn't disappear just because a newer trip has replaced it as "active"."""
    trip_ids = [trip.id for trip in await list_trips(session, user_id) if trip.id is not None]
    return await _execution_runs_for_trips(session, trip_ids)
