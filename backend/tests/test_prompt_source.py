"""load_system_prompt must extract PlanTrip's instruction text without the template tail."""

import pytest

from app.agent.prompts import load_system_prompt

pytestmark = pytest.mark.no_database


def test_extractor_returns_instruction_text_only() -> None:
    prompt = load_system_prompt()
    assert prompt.strip(), "extractor returned empty text; prompt-backtick boundary drifted"
    # Positive anchor: an empty/token-free check alone passes on wrong text; pin the real text.
    assert prompt.startswith("You are a travel agent. Plan a day-by-day itinerary"), (
        "extractor did not return the planner instruction text"
    )
    assert "ctx.output_format" not in prompt, "extractor leaked the output_format directive"
    assert "${trip_details}" not in prompt, "extractor leaked the trip_details placeholder"
    assert prompt == prompt.strip(), "extractor left BAML source indentation in the prompt"
