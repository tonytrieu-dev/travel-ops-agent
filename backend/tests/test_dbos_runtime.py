"""A failed planner run must preserve the exchange completed before the provider crashed."""

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import col

from app.agent.planner import agent
from app.dbos_runtime import run_planner_durable
from app.models import AgentRun, AgentRunStep
from tests.db_helpers import TEST_DATABASE_URL, seed_trip


async def test_planner_failure_preserves_completed_exchanges(client) -> None:
    engine = create_async_engine(TEST_DATABASE_URL)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    requests = 0

    def fail_after_one_exchange(messages, information) -> ModelResponse:
        nonlocal requests
        requests += 1
        if requests == 1:
            return ModelResponse(parts=[TextPart(content="thinking")])
        raise RuntimeError("provider failed after one exchange")

    try:
        async with session_factory() as session:
            trip_id = await seed_trip(session)
            await session.commit()
        with agent.override(model=FunctionModel(fail_after_one_exchange)):
            with pytest.raises(RuntimeError, match="provider failed"):
                await run_planner_durable(trip_id, "Plan a trip to Paris.")

        async with session_factory() as session:
            runs = list(await session.scalars(
                select(AgentRun).where(col(AgentRun.trip_request_id) == trip_id)
            ))
            assert len(runs) == 1, "failure must finalize the existing run, not lose or duplicate it"
            assert runs[0].status == "failed"
            steps = list(await session.scalars(
                select(AgentRunStep).where(col(AgentRunStep.agent_run_id) == runs[0].id)
            ))
            assert len(steps) == 1, "the completed exchange must survive the later provider failure"
    finally:
        await engine.dispose()
