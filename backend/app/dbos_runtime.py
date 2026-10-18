"""DBOS durable-execution wiring: booking execute and the agent run replay-safe across crashes.

Both entry points take only serializable arguments (a DBOS constraint) and rebuild whatever
session/provider they need internally, rather than receiving them injected — this is the only
change from the plain, already-tested versions of the functions they call.
"""

from dbos import DBOS, DBOSConfig
from pydantic_ai import UnexpectedModelBehavior
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models import Model
from pydantic_ai.usage import RunUsage
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.activities_tavily import TavilyActivityProvider
from app.adapters.flights_searchapi import get_flight_provider
from app.agent.execution_log import execution_context
from app.agent.observability import persist_agent_run
from app.agent.planner import PlannerDeps, agent, default_usage_limits
from app.config import CEREBRAS_MODEL, get_settings
from app.db import get_session_factory
from app.models import AgentRun as PersistedAgentRun
from app.models import FlightSearchResult, TripRequest
from app.rate_limit import acquire_agent_run_slot, release_agent_run_slot
from app.request_context import bind_correlation_id, correlation_id
from app.repositories import booking_repository as repository
from app.schemas import (
    BookingLogOut,
    ClarificationOut,
    PlannerOutput,
    PlanTooComplexOut,
)


def _as_durable_step(step_name: str, bound_method):
    # DBOS.step setattrs registration metadata onto its target; bound methods can't hold
    # arbitrary attributes, so wrap in a plain function that delegates to the bound method.
    @DBOS.step(name=step_name)
    async def _step(*args, **kwargs):
        return await bound_method(*args, **kwargs)

    return _step


# Patched once at import (not in launch_dbos(), which fires once per test via the
# function-scoped `client` fixture and would double-wrap on every call).
assert isinstance(agent.model, Model), "agent must be built with a real Model instance"
agent.model.request = _as_durable_step("cerebras_request", agent.model.request)


async def _persist_run(
    session: AsyncSession,
    trip_id: int,
    persisted_run: PersistedAgentRun | None,
    *,
    message_history: list[ModelMessage],
    usage: RunUsage,
    status: str = "completed",
) -> None:
    await persist_agent_run(
        session,
        trip_request_id=trip_id,
        model=CEREBRAS_MODEL,
        message_history=message_history,
        usage=usage,
        status=status,
        agent_run=persisted_run,
    )


def launch_dbos() -> None:
    settings = get_settings()
    DBOS(config=DBOSConfig(name="travel-agent", system_database_url=settings.dbos_database_url))
    DBOS.launch()


def shutdown_dbos() -> None:
    DBOS.destroy()


@DBOS.step(name="fetch_booking_options")
async def _fetch_booking_options_step(flight: FlightSearchResult) -> list[dict]:
    provider = get_flight_provider(get_settings())
    flights = flight.raw_offer["flights"]
    async with get_session_factory()() as session:
        trip = await session.get(TripRequest, flight.trip_request_id)
    assert trip is not None, "flight references a trip that no longer exists"
    return await provider.fetch_booking_options(
        flight.booking_token,
        departure_id=flights[0]["departure_airport"]["id"],
        arrival_id=flights[-1]["arrival_airport"]["id"],
        outbound_date=flights[0]["departure_airport"]["date"],
        return_date=trip.return_date,
        booking_token_is_resolved=flight.raw_offer.get("booking_token") == flight.booking_token,
    )


@DBOS.workflow(name="execute_booking")
async def execute_booking_durable(log_id: int, trace_id: str | None = None) -> BookingLogOut:
    with bind_correlation_id(trace_id):
        async with get_session_factory()() as session:
            booking = await repository.execute_booking(session, log_id, _fetch_booking_options_step)
            return BookingLogOut.model_validate(booking)


@DBOS.workflow(name="run_planner")
async def _run_planner_workflow(trip_id: int, prompt: str, trace_id: str) -> PlannerOutput:
    settings = get_settings()
    async with (
        get_session_factory()() as session,
        execution_context(session, trip_id, run_model=CEREBRAS_MODEL, correlation=trace_id) as persisted_run,
    ):
        trip = await session.get(TripRequest, trip_id)
        flight_provider = get_flight_provider(settings)
        flight_provider.search_offers = _as_durable_step(
            "search_flights_offers", flight_provider.search_offers
        )
        activity_provider = TavilyActivityProvider(settings.tavily_api_key.get_secret_value())
        activity_provider.search = _as_durable_step("web_search", activity_provider.search)
        deps = PlannerDeps(
            flight_provider=flight_provider,
            activity_provider=activity_provider,
            fitness_level=trip.fitness_level if trip is not None else None,
        )
        async with agent.iter(prompt, deps=deps, usage_limits=default_usage_limits()) as agent_run:
            try:
                async for node in agent_run:
                    pass
            except Exception as error:
                # Keeps whatever tool calls ran before the crash on the execution panel, not just successes.
                await _persist_run(
                    session,
                    trip_id,
                    persisted_run,
                    message_history=agent_run.ctx.state.message_history,
                    usage=agent_run.ctx.state.usage,
                    status="failed",
                )
                if isinstance(error, UsageLimitExceeded):
                    # A real, expected outcome on a research-heavy trip (MAX_CONTEXT_TOKENS).
                    return PlanTooComplexOut(
                        reason=(
                            "This trip needed more planning work than fits in one pass. "
                            "Try a shorter trip or a simpler request."
                        )
                    )
                if not isinstance(error, UnexpectedModelBehavior):
                    raise
                # The model exhausted its retries without producing a valid itinerary (e.g. no
                # groundable activities) — ask the user instead of crashing the request.
                return ClarificationOut(
                    questions=[
                        "I couldn't find enough verified activity information to complete this "
                        "itinerary. Could you narrow the destination or share specific interests "
                        "to search for?"
                    ]
                )

            result = agent_run.result
            assert result is not None, "agent_run finished iterating without producing a result"
            await _persist_run(
                session,
                trip_id,
                persisted_run,
                message_history=result.all_messages(),
                usage=result.usage,
            )
    return result.output


async def run_planner_durable(trip_id: int, prompt: str, trace_id: str | None = None) -> PlannerOutput:
    """Not itself a DBOS workflow: the concurrency slot is plain in-process state, and acquiring
    it inside a replayable workflow body risks a double-acquire if DBOS re-enters that body
    during its own internal record/persist resolution (observed empirically) — so the slot wraps
    the durable call from the outside instead."""
    await acquire_agent_run_slot()
    try:
        return await _run_planner_workflow(trip_id, prompt, trace_id or correlation_id())
    finally:
        release_agent_run_slot()
