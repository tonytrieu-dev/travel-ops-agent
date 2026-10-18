"""Slack interactivity callback for the booking-approval message. Verifies Slack's webhook
signature before doing anything else, then approves or rejects the booking named by the
clicked button. Never executes a booking here — see docs/DECISIONS.md for why.
"""

import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from app.adapters.slack_hitl import (
    resolve_block_action,
    update_approval_message,
    verify_slack_signature,
)
from app import config
from app.db import get_session_factory
from app.routes.connectors import slack_configured
from app.schemas import SlackAuthErrorOut

router = APIRouter(prefix="/api/slack", tags=["slack"])

_UNCONFIGURED_OR_UNSIGNED = JSONResponse(
    status_code=401, content={"detail": "Slack is not configured or the signature is invalid."}
)


@router.post(
    "/interactions",
    responses={401: {"model": SlackAuthErrorOut}},
)
async def slack_interactions(request: Request) -> Response:
    settings = config.get_settings()
    if not slack_configured(settings):
        return _UNCONFIGURED_OR_UNSIGNED
    assert settings.slack_signing_secret is not None
    assert settings.slack_approvals_channel_id is not None

    raw_body = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")
    if not verify_slack_signature(
        raw_body, timestamp, signature, settings.slack_signing_secret.get_secret_value()
    ):
        return _UNCONFIGURED_OR_UNSIGNED

    form = await request.form()
    payload_raw = form.get("payload")
    if not isinstance(payload_raw, str):
        return Response(status_code=200)
    try:
        payload: dict[str, Any] = json.loads(payload_raw)
    except json.JSONDecodeError:
        return Response(status_code=200)

    async with get_session_factory()() as session:
        outcome = await resolve_block_action(
            session, payload, expected_channel_id=settings.slack_approvals_channel_id
        )

    if outcome is None:
        return Response(status_code=200)
    await update_approval_message(settings, payload, outcome)
    return Response(status_code=200)
