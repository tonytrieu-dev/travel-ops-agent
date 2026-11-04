"""Derives ordered run steps from real message history; missing fields stay null."""

import json

from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)

from app.models import AgentRunStep, AgentStepKind


def _duration_ms(start, end) -> int | None:
    if start is None or end is None:
        return None
    return round((end - start).total_seconds() * 1000)


def _find_tool_return(
    message_history: list[ModelMessage], tool_call_id: str, search_from_index: int
) -> ToolReturnPart | None:
    for message in message_history[search_from_index:]:
        for part in message.parts:
            if isinstance(part, ToolReturnPart) and part.tool_call_id == tool_call_id:
                return part
    return None


def _summarize_value(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict | list):
        return json.dumps(value)
    return str(value)


def _is_output_tool(tool_name: str) -> bool:
    """pydantic-ai returns a structured result by calling a synthetic tool named `final_result`,
    or `final_result_<Type>` when `output_type` is a union — the result, not an agent tool."""
    return tool_name == "final_result" or tool_name.startswith("final_result_")


def derive_steps(message_history: list[ModelMessage]) -> list[AgentRunStep]:
    """One step per model call plus one per tool call or structured output, in the order they
    occurred."""
    steps: list[AgentRunStep] = []
    seq = 1
    previous_timestamp = message_history[0].timestamp if message_history else None

    for index, message in enumerate(message_history):
        if not isinstance(message, ModelResponse):
            continue

        text_parts = [part.content for part in message.parts if isinstance(part, TextPart)]
        steps.append(
            AgentRunStep(
                seq=seq,
                kind=AgentStepKind.MODEL,
                name=message.model_name or "model",
                status="completed",
                duration_ms=_duration_ms(previous_timestamp, message.timestamp),
                input_summary=None,
                output_summary=" ".join(text_parts) or None,
                tokens=message.usage.output_tokens if message.usage else None,
            )
        )
        seq += 1

        for part in message.parts:
            if not isinstance(part, ToolCallPart):
                continue
            tool_return = _find_tool_return(message_history, part.tool_call_id, index + 1)
            is_output_tool = _is_output_tool(part.tool_name)
            # A refused output attempt has no tool return, and "rejected" says why: validation
            # refused it and the model was retried. A real tool just never produced a result.
            steps.append(
                AgentRunStep(
                    seq=seq,
                    kind=AgentStepKind.OUTPUT if is_output_tool else AgentStepKind.TOOL,
                    name=part.tool_name,
                    status="completed"
                    if tool_return is not None
                    else ("rejected" if is_output_tool else "no_result"),
                    duration_ms=_duration_ms(
                        message.timestamp, tool_return.timestamp if tool_return else None
                    ),
                    input_summary=_summarize_value(part.args),
                    output_summary=_summarize_value(tool_return.content) if tool_return else None,
                    tokens=None,
                )
            )
            seq += 1

        previous_timestamp = message.timestamp

    return steps
