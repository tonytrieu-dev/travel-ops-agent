"""Context-local ExecutionEvent recorder — deep call-stack code records without threading
trip_request_id through every signature. Commits immediately so a later failure still leaves
prior events recorded.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic_ai.messages import ModelMessage
from pydantic_ai.usage import RunUsage
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.agent.observability import derive_steps
from app.models import AgentRun, AgentRunStep, ExecutionEvent, ExecutionEventKind, TripRequest, utcnow
from app.request_context import bind_correlation_id, correlation_id


@dataclass
class _ExecutionContext:
    session: AsyncSession
    trip_request_id: int
    agent_run: AgentRun | None
    # AsyncSession isn't safe for concurrent use; serializes record_event() within this context
    # only — cross-context seq safety is the FOR UPDATE claim in record_event(), not this lock.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_current: ContextVar[_ExecutionContext | None] = ContextVar("execution_context", default=None)


@asynccontextmanager
async def execution_context(
    session: AsyncSession, trip_request_id: int, *, run_model: str | None = None, correlation: str | None = None
) -> AsyncIterator[AgentRun | None]:
    """Bind one execution so its append-only events retain both trip and run ownership."""
    with bind_correlation_id(correlation or correlation_id()):
        agent_run = (
            AgentRun(trip_request_id=trip_request_id, status="running", model=run_model)
            if run_model is not None
            else None
        )
        if agent_run is not None:
            session.add(agent_run)
            await session.commit()
            assert agent_run.id is not None

        token = _current.set(
            _ExecutionContext(
                session=session,
                trip_request_id=trip_request_id,
                agent_run=agent_run,
            )
        )
        try:
            yield agent_run
        except Exception:
            if agent_run is not None and agent_run.status == "running":
                await finish_run("failed")
            raise
        finally:
            _current.reset(token)


def _bound_context(caller_name: str) -> _ExecutionContext:
    context = _current.get()
    if context is None:
        raise RuntimeError(
            f"{caller_name}() called with no execution_context bound — every "
            "adapter/tool/protocol call must run inside execution_log.execution_context()"
        )
    return context


def current_trip(caller_name: str) -> tuple[AsyncSession, int]:
    """Exposed so a tool can query the trip's own data without threading session/trip_id
    through PlannerDeps."""
    context = _bound_context(caller_name)
    return context.session, context.trip_request_id


async def record_event(
    kind: ExecutionEventKind,
    name: str,
    status: str,
    detail: str,
    duration_ms: int | None = None,
    data: dict[str, Any] | None = None,
    provider: str | None = None,
) -> None:
    context = _bound_context("record_event")
    async with context.lock:
        # FOR UPDATE on the trip row serializes seq allocation across concurrent runs (e.g. a
        # double-submitted /plan) — a locally-cached counter would let two runs read the same
        # starting point and hand out colliding seq values.
        await context.session.execute(
            select(TripRequest).where(col(TripRequest.id) == context.trip_request_id).with_for_update()
        )
        result = await context.session.execute(
            select(func.max(ExecutionEvent.seq)).where(
                col(ExecutionEvent.trip_request_id) == context.trip_request_id
            )
        )
        next_seq = (result.scalar_one_or_none() or 0) + 1
        context.session.add(
            ExecutionEvent(
                trip_request_id=context.trip_request_id,
                agent_run_id=context.agent_run.id if context.agent_run is not None else None,
                seq=next_seq,
                kind=kind,
                name=name,
                provider=provider,
                status=status,
                detail=detail,
                duration_ms=duration_ms,
                data=data,
                correlation_id=correlation_id(),
            )
        )
        await context.session.commit()


async def finish_run(
    status: str,
    *,
    steps: list[AgentRunStep] | None = None,
    total_ms: int | None = None,
    started_at: datetime | None = None,
    usage: RunUsage | None = None,
) -> None:
    context = _bound_context("finish_run")
    agent_run = context.agent_run
    if agent_run is None:
        raise RuntimeError("finish_run requires an execution context with a run")
    finished_at = utcnow()
    agent_run.status = status
    if started_at is not None:
        agent_run.started_at = started_at
    agent_run.finished_at = finished_at
    agent_run.total_ms = (
        total_ms if total_ms is not None
        else round((finished_at - agent_run.started_at).total_seconds() * 1000)
    )
    if usage is not None:
        agent_run.total_input_tokens = usage.input_tokens
        agent_run.total_output_tokens = usage.output_tokens
    assert agent_run.id is not None
    for step in steps or []:
        step.agent_run_id = agent_run.id
        context.session.add(step)
    await context.session.commit()


async def finish_planner_run(
    message_history: list[ModelMessage], usage: RunUsage, *, status: str = "completed",
) -> None:
    started_at = message_history[0].timestamp if message_history else None
    finished_at = message_history[-1].timestamp if message_history else None
    total_ms = (
        round((finished_at - started_at).total_seconds() * 1000)
        if started_at is not None and finished_at is not None else 0
    )
    await finish_run(status, steps=derive_steps(message_history), total_ms=total_ms, usage=usage)
