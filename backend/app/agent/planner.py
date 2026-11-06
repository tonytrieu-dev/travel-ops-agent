"""The travel-planner agent: union output (asks or plans), two composable read-only tools."""

import re
import time
from dataclasses import dataclass
from typing import Literal

from openai import AsyncOpenAI
from pydantic_ai import Agent, ModelRetry, RunContext, ToolOutput
from pydantic_ai.messages import ModelMessage, ToolReturnPart
from pydantic_ai.models.cerebras import CerebrasModel
from pydantic_ai.providers.cerebras import CerebrasProvider
from pydantic_ai.usage import UsageLimits

from app.adapters.activities_tavily import ActivityProvider
from app.adapters.flights_searchapi import FlightProvider
from app.agent.execution_log import current_trip, record_event
from app.agent.prompts import load_system_prompt, sanitize_web_content
from app.config import (
    CEREBRAS_BASE_URL,
    CEREBRAS_MODEL,
    MAX_CONTEXT_TOKENS,
    MAX_OUTPUT_RETRIES,
    MAX_REQUESTS_PER_RUN,
    MAX_TOOL_STEPS,
    MAX_WEB_SEARCH_RESULTS,
    get_settings,
)
from app.models import ExecutionEventKind, FitnessLevel, TripRequest
from app.schemas import ClarificationOut, ItineraryOut
from app.services.flight_search import FlightSearchService, offer_summary

_IATA_CODE_PATTERN = re.compile(r"^[A-Z]{3}$")
_FLIGHT_ACTIVITY_PATTERN = re.compile(
    r"\b(?:flight|fly|flying|airfare|airline)\b", re.IGNORECASE
)
_OPTIONAL_CLARIFICATION_PATTERN = re.compile(
    r"\b(?:budget|interests?|preferences?|prefer|age|fitness|dates?|depart|return|origin|airport)\b",
    re.IGNORECASE,
)


@dataclass
class PlannerDeps:
    flight_provider: FlightProvider
    activity_provider: ActivityProvider
    fitness_level: FitnessLevel | None = None
    # Per-run guards, not a cache: one search_flights response already covers both legs.
    _search_flights_called: bool = False
    _web_search_called: bool = False


def _activity_provider_name(provider: ActivityProvider) -> str:
    return {
        "TavilyActivityProvider": "Tavily",
        "RecordedActivityProvider": "Recorded activities",
    }.get(type(provider).__name__, type(provider).__name__)


def default_usage_limits() -> UsageLimits:
    return UsageLimits(
        request_limit=MAX_REQUESTS_PER_RUN,
        tool_calls_limit=MAX_TOOL_STEPS,
        total_tokens_limit=MAX_CONTEXT_TOKENS,
    )


async def search_flights(
    ctx: RunContext[PlannerDeps],
    departure_id: str,
    arrival_id: str,
    outbound_date: str,
    return_date: str | None = None,
) -> dict:
    """Search real Google Flights offers between two IATA airport codes. When return_date is
    set, the single response already contains both the outbound and return legs — call this at
    most once per trip; never call it again to search the return leg separately."""
    if ctx.deps._search_flights_called:
        raise ModelRetry(
            "search_flights already ran for this trip earlier in this conversation and its "
            "result already covers both the outbound and return legs. Reuse that result; do not "
            "call search_flights again."
        )
    if not _IATA_CODE_PATTERN.match(departure_id) or not _IATA_CODE_PATTERN.match(arrival_id):
        raise ModelRetry(
            f"departure_id and arrival_id must be 3-letter IATA codes (e.g. JFK), got "
            f"departure_id={departure_id!r} arrival_id={arrival_id!r}"
        )

    # Route/dates never change mid-plan, and this tool must never write offers or reach across
    # trips (see FlightSearchService.search's persist/allow_cross_trip_cache — the route path,
    # not this tool, owns full persistence and cross-trip reuse).
    session, trip_id = current_trip("search_flights")
    trip = await session.get(TripRequest, trip_id)
    if trip is None:
        raise ModelRetry(f"trip {trip_id} is gone; cannot search flights for it")
    outcome = await FlightSearchService(session, ctx.deps.flight_provider).search(
        trip_id, persist=False, allow_cross_trip_cache=False
    )
    ctx.deps._search_flights_called = True
    return {
        "offers": [offer_summary(offer) for offer in outcome.offers],
        "unavailable_reason": outcome.unavailable_reason,
        "source": "cached" if outcome.source == "same_trip_cache" else "live",
    }


async def web_search(
    ctx: RunContext[PlannerDeps], query: str, max_results: int = MAX_WEB_SEARCH_RESULTS
) -> list[dict]:
    """Research real, source-attributed activities or information. Call this at most once per
    trip with one broad query (e.g. "things to do in {destination}") — never call it again."""
    if ctx.deps._web_search_called:
        raise ModelRetry(
            "web_search already ran for this trip earlier in this conversation. Reuse that "
            "result; do not call web_search again."
        )
    started_at = time.monotonic()
    # Clamp both ends: the ceiling protects the provider token budget, and the floor of 1 keeps a
    # model-supplied 0 or negative from reaching Tavily (which would waste the call on no results).
    clamped_max_results = max(1, min(max_results, MAX_WEB_SEARCH_RESULTS))
    session, trip_id = current_trip("web_search")
    trip = await session.get(TripRequest, trip_id)
    if trip is None:
        raise ModelRetry(f"trip {trip_id} is gone; cannot search activities for it")
    # A free-text destination (e.g. "Ontario") can collide with a same-named place elsewhere;
    # anchor on the unambiguous airport code so results can't silently drift to the wrong place.
    anchored_query = f"{query} near {trip.destination_airport} airport"
    results = await ctx.deps.activity_provider.search(
        anchored_query, max_results=clamped_max_results
    )
    duration_ms = round((time.monotonic() - started_at) * 1000)
    await record_event(
        ExecutionEventKind.API_CALL,
        "web_search",
        "ok",
        f"{len(results)} results for query={anchored_query!r}",
        duration_ms,
        data={"results": [{"title": result.title, "url": result.url} for result in results]},
        provider=_activity_provider_name(ctx.deps.activity_provider),
    )
    ctx.deps._web_search_called = True
    return [
        {
            "title": result.title,
            "url": result.url,
            "content": sanitize_web_content(result.content),
            "score": result.score,
        }
        for result in results
    ]


def _build_agent() -> Agent[PlannerDeps, ItineraryOut | ClarificationOut]:
    settings = get_settings()
    model = CerebrasModel(
        CEREBRAS_MODEL,
        provider=CerebrasProvider(
            openai_client=AsyncOpenAI(
                api_key=settings.cerebras_api_key.get_secret_value(),
                base_url=CEREBRAS_BASE_URL,
            ),
        ),
    )
    built_agent = Agent(
        model,
        deps_type=PlannerDeps,
        # Pinned to tool output: left on auto, pydantic-ai picks native structured output for
        # this model and wraps the union in {"result": {"kind": ...}}. gpt-oss-120b fumbles that
        # envelope, and each retry resends the whole itinerary — 3 fumbles blow the token limit.
        output_type=[ToolOutput(ItineraryOut), ToolOutput(ClarificationOut)],
        system_prompt=load_system_prompt(),
        retries={"output": MAX_OUTPUT_RETRIES},
    )
    built_agent.instrument = True
    built_agent.tool(strict=True)(search_flights)
    built_agent.tool(strict=True)(web_search)
    return built_agent


agent = _build_agent()


def _web_search_urls(messages: list[ModelMessage]) -> set[str]:
    urls: set[str] = set()
    for message in messages:
        for part in message.parts:
            if not isinstance(part, ToolReturnPart) or part.tool_name != "web_search":
                continue
            if isinstance(part.content, list):
                urls.update(
                    result["url"]
                    for result in part.content
                    if isinstance(result, dict) and result.get("url")
                )
    return urls


def _is_flight_activity(name: str) -> bool:
    # Name only: scanning the description false-positives on real activities that mention flying
    # (USS Midway "flight deck"), which the model can't remove — it retries until it dies.
    return bool(_FLIGHT_ACTIVITY_PATTERN.search(name))


@agent.output_validator
def reject_optional_clarification(
    _context: RunContext[PlannerDeps], output: ItineraryOut | ClarificationOut
) -> ItineraryOut | ClarificationOut:
    if not isinstance(output, ClarificationOut):
        return output
    if not any(_OPTIONAL_CLARIFICATION_PATTERN.search(question) for question in output.questions):
        return output
    raise ModelRetry(
        "Plan directly from the provided origin, destination, dates, age, and fitness level. "
        "Do not ask about optional budget, interests, preferences, or already-provided trip fields."
    )


@agent.output_validator
def reject_flight_activities(
    _context: RunContext[PlannerDeps], output: ItineraryOut | ClarificationOut
) -> ItineraryOut | ClarificationOut:
    if not isinstance(output, ItineraryOut):
        return output
    flights = [
        activity.name
        for day in output.days
        for activity in day.activities
        if _is_flight_activity(activity.name)
    ]
    if flights:
        raise ModelRetry(
            f"Flights are not itinerary activities: {flights}. Remove them; flight options are "
            "shown separately to the traveler."
        )
    return output


@agent.output_validator
def reject_ungrounded_itinerary(
    ctx: RunContext[PlannerDeps], output: ItineraryOut | ClarificationOut
) -> ItineraryOut | ClarificationOut:
    """Every activity's source_url must be a URL web_search actually returned this run — the
    structural enforcement of "never fabricate an activity" (the prompt alone doesn't hold)."""
    if not isinstance(output, ItineraryOut):
        return output
    if not output.days:
        raise ModelRetry("The itinerary has no days. Return a day-by-day plan for this trip.")
    grounded = _web_search_urls(ctx.messages)
    ungrounded = [
        activity.source_url
        for day in output.days
        for activity in day.activities
        if activity.source_url not in grounded
    ]
    if ungrounded:
        raise ModelRetry(
            f"{len(ungrounded)} activity source_url(s) were not returned by web_search: "
            f"{ungrounded}. Call web_search to research real activities for this destination, then "
            "set every activity's source_url to a URL web_search actually returned. Never invent "
            "an activity or a URL, and never use a flight search as an activity."
        )
    return output


# Policy, not vocabulary: "moderate" stays allowed for low fitness on purpose.
UNSAFE_INTENSITY_FOR_LOW_FITNESS: Literal["low", "moderate", "high"] = "high"


@agent.output_validator
def reject_unsafe_intensity(
    ctx: RunContext[PlannerDeps], output: ItineraryOut | ClarificationOut
) -> ItineraryOut | ClarificationOut:
    """Structural enforcement of "match intensity to fitness" — the prompt alone doesn't hold,
    same reasoning as reject_ungrounded_itinerary above."""
    if not isinstance(output, ItineraryOut) or ctx.deps.fitness_level != FitnessLevel.LOW:
        return output
    unsafe = [
        activity.name
        for day in output.days
        for activity in day.activities
        if activity.intensity == UNSAFE_INTENSITY_FOR_LOW_FITNESS
    ]
    if unsafe:
        raise ModelRetry(
            f"Traveler fitness level is {ctx.deps.fitness_level.value}, but these activities are "
            f"too high intensity: {unsafe}. Replace them with gentler, shorter-distance options."
        )
    return output
