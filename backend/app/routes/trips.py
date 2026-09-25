"""Trip routes. Each handler stays thin: call the repository (which owns validation, caching,
and itinerary persistence), then shape the result into the response model. Domain rejections
raise TripError, rendered as a ProblemDetail by the app-level handler in main.py.
"""

from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.flights_searchapi import derive_flight_legs, get_flight_provider
from app.agent.execution_log import execution_context
from app.config import (
    LLM_INPUT_PRICE_PER_MILLION_TOKENS,
    LLM_OUTPUT_PRICE_PER_MILLION_TOKENS,
    MAX_CONTEXT_TOKENS,
    get_settings,
)
from app.db import get_session
from app.dbos_runtime import run_planner_durable
from app.dependencies import SecurityContext, get_current_user, get_security_context
from app.models import AgentRun, AgentRunStep, ExecutionEvent, User
from app.rate_limit import enforce_request_rate_limit
from app.repositories import trips_repository as repository
from app.schemas import (
    AgentRunOut,
    AgentRunStepOut,
    ClarificationOut,
    ExecutionEventOut,
    ExecutionPanelOut,
    FlightLegOut,
    FlightOfferOut,
    FlightSearchOut,
    GlobalExecutionPanelOut,
    ItineraryOut,
    PlanNeedsClarificationOut,
    PlanOut,
    PlanReadyOut,
    PlanTooComplexOut,
    ProblemDetail,
    TripRequestCreate,
    TripRequestOut,
    TripRequestUpdate,
    TripSnapshotOut,
)
from app.services.flight_search import FlightSearchService, flight_provider_name
from app.security import authorize, enforce_api_segment, require_owned_trip

router = APIRouter(
    prefix="/api",
    tags=["trips"],
    dependencies=[Depends(enforce_api_segment)],
)

_VALIDATION: dict[int | str, dict[str, Any]] = {422: {"model": ProblemDetail}}
_NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"model": ProblemDetail}}
_NOT_FOUND_OR_VALIDATION: dict[int | str, dict[str, Any]] = {
    404: {"model": ProblemDetail},
    422: {"model": ProblemDetail},
}
_NOT_FOUND_OR_RATE_LIMITED: dict[int | str, dict[str, Any]] = {
    404: {"model": ProblemDetail},
    429: {"model": ProblemDetail},
}


@router.post("/trips", response_model=TripRequestOut, responses=_VALIDATION)
async def create_trip(
    body: TripRequestCreate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
    context: SecurityContext = Depends(get_security_context),
) -> TripRequestOut:
    await authorize(context, session, action="trip.create", resource="trip", request=None)
    assert user.id is not None, "get_current_user must always return a persisted user"
    trip = await repository.create_trip(session, user.id, body)
    return TripRequestOut.model_validate(trip)


@router.get("/trips", response_model=list[TripRequestOut])
async def list_trips(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
    context: SecurityContext = Depends(get_security_context),
) -> list[TripRequestOut]:
    await authorize(context, session, action="trip.read", resource="trips")
    assert user.id is not None, "get_current_user must always return a persisted user"
    trips = await repository.list_trips(session, user.id)
    return [TripRequestOut.model_validate(trip) for trip in trips]


@router.get("/trips/{trip_id}", response_model=TripRequestOut, responses=_NOT_FOUND)
async def get_trip(
    trip_id: int, request: Request, session: AsyncSession = Depends(get_session), context: SecurityContext = Depends(get_security_context)
) -> TripRequestOut:
    await authorize(context, session, action="trip.read", resource=f"trip:{trip_id}", request=request)
    trip = await require_owned_trip(session, context, trip_id, request)
    return TripRequestOut.model_validate(trip)


@router.patch(
    "/trips/{trip_id}", response_model=TripRequestOut, responses=_NOT_FOUND_OR_VALIDATION
)
async def update_trip(
    trip_id: int, body: TripRequestUpdate, request: Request, session: AsyncSession = Depends(get_session), context: SecurityContext = Depends(get_security_context)
) -> TripRequestOut:
    await authorize(context, session, action="trip.create", resource=f"trip:{trip_id}", request=request)
    await require_owned_trip(session, context, trip_id, request)
    trip = await repository.update_trip(session, trip_id, body)
    return TripRequestOut.model_validate(trip)


def _to_flight_offer_out(offer: Any) -> FlightOfferOut:
    offer_out = FlightOfferOut.model_validate(offer)
    offer_out.legs = [FlightLegOut(**leg) for leg in derive_flight_legs(offer.raw_offer)]
    return offer_out


@router.get("/trips/{trip_id}/snapshot", response_model=TripSnapshotOut, responses=_NOT_FOUND)
async def get_trip_snapshot(
    trip_id: int, request: Request, session: AsyncSession = Depends(get_session), context: SecurityContext = Depends(get_security_context)
) -> TripSnapshotOut:
    await authorize(context, session, action="trip.read", resource=f"trip:{trip_id}", request=request)
    await require_owned_trip(session, context, trip_id, request)
    trip, offers, itinerary, is_stale = await repository.get_trip_snapshot(session, trip_id)
    return TripSnapshotOut(
        trip=TripRequestOut.model_validate(trip),
        flight_search=FlightSearchOut(
            offers=[_to_flight_offer_out(offer) for offer in offers],
            is_stale=is_stale,
        )
        if offers
        else None,
        plan=PlanReadyOut(itinerary=ItineraryOut(days=itinerary.days))
        if itinerary is not None
        else None,
    )


@router.post(
    "/trips/{trip_id}/flights/search",
    response_model=FlightSearchOut,
    responses=_NOT_FOUND_OR_RATE_LIMITED,
    dependencies=[Depends(enforce_request_rate_limit)],
)
async def search_trip_flights(
    trip_id: int, request: Request, session: AsyncSession = Depends(get_session), context: SecurityContext = Depends(get_security_context)
) -> FlightSearchOut:
    await authorize(context, session, action="flight.search", resource=f"trip:{trip_id}", request=request)
    await require_owned_trip(session, context, trip_id, request)
    provider = get_flight_provider(get_settings())
    await repository.get_trip(session, trip_id)
    async with execution_context(
        session, trip_id, run_model=flight_provider_name(provider)
    ) as agent_run:
        assert agent_run is not None
        outcome = await FlightSearchService(session, provider).search(trip_id, agent_run=agent_run)
    return FlightSearchOut(
        offers=[_to_flight_offer_out(offer) for offer in outcome.offers],
        unavailable_reason=outcome.unavailable_reason,
    )


@router.post(
    "/trips/{trip_id}/plan",
    response_model=PlanOut,
    responses=_NOT_FOUND_OR_RATE_LIMITED,
    dependencies=[Depends(enforce_request_rate_limit)],
)
async def plan_trip(
    trip_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
    context: SecurityContext = Depends(get_security_context),
) -> PlanOut:
    await authorize(context, session, action="trip.plan", resource=f"trip:{trip_id}", request=request)
    await require_owned_trip(session, context, trip_id, request)
    output = await repository.get_or_create_itinerary(session, trip_id, run_planner_durable)
    if isinstance(output, PlanTooComplexOut):
        return output
    if isinstance(output, ClarificationOut):
        return PlanNeedsClarificationOut(questions=output.questions)
    return PlanReadyOut(itinerary=output)


def _to_agent_run_out(
    agent_run: AgentRun,
    steps: list[AgentRunStep],
    events: list[ExecutionEvent],
) -> AgentRunOut:
    estimated_cost_usd = (
        agent_run.total_input_tokens * LLM_INPUT_PRICE_PER_MILLION_TOKENS
        + agent_run.total_output_tokens * LLM_OUTPUT_PRICE_PER_MILLION_TOKENS
    ) / 1_000_000
    budget_utilization_pct = (
        100 * (agent_run.total_input_tokens + agent_run.total_output_tokens) / MAX_CONTEXT_TOKENS
    )
    agent_run_out = AgentRunOut.model_validate(agent_run)
    agent_run_out.steps = [AgentRunStepOut.model_validate(step) for step in steps]
    agent_run_out.events = [ExecutionEventOut.model_validate(event) for event in events]
    agent_run_out.estimated_cost_usd = round(estimated_cost_usd, 6)
    agent_run_out.budget_utilization_pct = round(budget_utilization_pct, 2)
    return agent_run_out


def _to_panel_out(
    trip_id: int,
    runs_with_details: list[
        tuple[AgentRun, list[AgentRunStep], list[ExecutionEvent]]
    ],
    events: list[ExecutionEvent],
) -> ExecutionPanelOut:
    return ExecutionPanelOut(
        trip_request_id=trip_id,
        agent_runs=[
            _to_agent_run_out(run, steps, run_events)
            for run, steps, run_events in runs_with_details
        ],
        events=[ExecutionEventOut.model_validate(event) for event in events],
    )


@router.get("/trips/{trip_id}/execution", response_model=ExecutionPanelOut, responses=_NOT_FOUND)
async def get_trip_execution(
    trip_id: int, request: Request, session: AsyncSession = Depends(get_session), context: SecurityContext = Depends(get_security_context)
) -> ExecutionPanelOut:
    await authorize(context, session, action="trip.read", resource=f"trip:{trip_id}:execution", request=request)
    await require_owned_trip(session, context, trip_id, request)
    runs_with_details, events = await repository.get_execution_panel(session, trip_id)
    return _to_panel_out(trip_id, runs_with_details, events)


@router.get("/execution", response_model=GlobalExecutionPanelOut)
async def get_all_execution(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> GlobalExecutionPanelOut:
    assert user.id is not None, "get_current_user must always return a persisted user"
    runs_with_details = await repository.list_execution_runs_for_user(session, user.id)
    return GlobalExecutionPanelOut(
        agent_runs=[
            _to_agent_run_out(run, steps, events) for run, steps, events in runs_with_details
        ]
    )
