"""Connectors routes: a live, DB-backed toggle for the Slack HITL connector — no separate
repository module, this is a single row with two simple queries."""

from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import get_session
from app.dependencies import SecurityContext, get_security_context
from app.models import ConnectorSetting
from app.schemas import (
    ConnectorsOut,
    ConnectorStatusOut,
    ConnectorToggleUpdate,
    ErrorCode,
    ProblemDetail,
)
from app.security import authorize, enforce_api_segment

router = APIRouter(
    prefix="/api/connectors",
    tags=["connectors"],
    dependencies=[Depends(enforce_api_segment)],
)

_NOT_CONFIGURED: dict[int | str, dict[str, Any]] = {409: {"model": ProblemDetail}}


class ConnectorError(Exception):
    def __init__(self, code: ErrorCode, status_code: int, detail: str) -> None:
        self.code = code
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def slack_configured(settings: Settings) -> bool:
    return bool(
        settings.slack_bot_token
        and settings.slack_signing_secret
        and settings.slack_approvals_channel_id
    )


async def _get_or_create_row(session: AsyncSession) -> ConnectorSetting:
    row = await session.scalar(select(ConnectorSetting))
    if row is None:
        row = ConnectorSetting()
        session.add(row)
        await session.commit()
    return row


async def slack_notifications_enabled(session: AsyncSession, settings: Settings) -> bool:
    """The single definition of "should we post to Slack": configured on this deployment
    *and* toggled on in ``connector_setting`` — every caller shares this instead of
    re-deriving the pair independently."""
    if not slack_configured(settings):
        return False
    row = await _get_or_create_row(session)
    return row.slack_enabled


@router.get("", response_model=ConnectorsOut)
async def get_connectors(
    request: Request,
    session: AsyncSession = Depends(get_session),
    context: SecurityContext = Depends(get_security_context),
) -> ConnectorsOut:
    await authorize(context, session, action="connector.configure", resource="connectors", request=request, require_mfa=True)
    settings = get_settings()
    row = await _get_or_create_row(session)
    return ConnectorsOut(
        slack=ConnectorStatusOut(configured=slack_configured(settings), enabled=row.slack_enabled)
    )


@router.patch("/slack", response_model=ConnectorsOut, responses=_NOT_CONFIGURED)
async def set_slack_connector(
    body: ConnectorToggleUpdate,
    request: Request,
    session: AsyncSession = Depends(get_session),
    context: SecurityContext = Depends(get_security_context),
) -> ConnectorsOut:
    await authorize(context, session, action="connector.configure", resource="slack", request=request, require_mfa=True)
    settings = get_settings()
    if body.enabled and not slack_configured(settings):
        raise ConnectorError(
            ErrorCode.CONNECTOR_NOT_CONFIGURED,
            409,
            "Slack is not configured on this deployment (missing bot token, signing secret, "
            "or channel id).",
        )
    row = await _get_or_create_row(session)
    row.slack_enabled = body.enabled
    session.add(row)
    await session.commit()
    return ConnectorsOut(
        slack=ConnectorStatusOut(configured=slack_configured(settings), enabled=row.slack_enabled)
    )
