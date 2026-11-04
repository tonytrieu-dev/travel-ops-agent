"""The low-fitness guard must reject high intensity without rejecting moderate activity."""

from typing import Literal

import pytest
from pydantic_ai import ModelRetry, RunContext, UnexpectedModelBehavior
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from app.agent.planner import PlannerDeps, agent, reject_unsafe_intensity
from app.models import FitnessLevel
from app.schemas import ActivityOut, ItineraryDayOut, ItineraryOut

pytestmark = pytest.mark.no_database


@pytest.mark.asyncio
async def test_planner_retries_instead_of_accepting_an_empty_itinerary() -> None:
    model = TestModel(call_tools=[], custom_output_args={"days": []})
    with agent.override(model=model):
        with pytest.raises(UnexpectedModelBehavior, match="output retries"):
            await agent.run("Plan a trip", deps=PlannerDeps(
                flight_provider=None,  # type: ignore[arg-type]
                activity_provider=None,  # type: ignore[arg-type]
            ))


@pytest.mark.parametrize("intensity", ["high", "moderate"])
def test_low_fitness_intensity_boundary(intensity: Literal["high", "moderate"]) -> None:
    context = RunContext(
        deps=PlannerDeps(
            flight_provider=None,  # type: ignore[arg-type]
            activity_provider=None,  # type: ignore[arg-type]
            fitness_level=FitnessLevel.LOW,
        ),
        model=TestModel(),
        usage=RunUsage(),
    )
    itinerary = ItineraryOut(days=[ItineraryDayOut(
        day_number=1,
        summary="Explore",
        activities=[ActivityOut(
            name="Summit hike",
            description="A mountain trail.",
            intensity=intensity,
            source_url="https://example.test/hike",
        )],
    )])
    if intensity == "high":
        with pytest.raises(ModelRetry, match="Summit hike"):
            reject_unsafe_intensity(context, itinerary)
        return
    assert reject_unsafe_intensity(context, itinerary) is itinerary
