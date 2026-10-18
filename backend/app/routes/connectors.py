"""Connectors routes: a live, DB-backed toggle for the Slack HITL connector — no separate
repository module, this is a single row with two simple queries."""

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app import config
from app.adapters.slack_hitl import connector_setting_row, slack_configured
from app.db import get_session
from app.schemas import (
    ConnectorsOut,
    ConnectorStatusOut,
    ConnectorToggleUpdate,
    ErrorCode,
    ProblemDetail,
)
from app.security_admission import authorize_route
from app.security_segment import enforce_api_segment

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


@router.get(
    "",
    response_model=ConnectorsOut,
    dependencies=[Depends(authorize_route("connector.configure", "connectors", require_mfa=True))],
)
async def get_connectors(
    session: AsyncSession = Depends(get_session),
) -> ConnectorsOut:
    settings = config.get_settings()
    row = await connector_setting_row(session)
    configured = slack_configured(settings)
    return ConnectorsOut(
        slack=ConnectorStatusOut(configured=configured, enabled=configured and row.slack_enabled)
    )


@router.patch(
    "/slack",
    response_model=ConnectorsOut,
    responses=_NOT_CONFIGURED,
    dependencies=[Depends(authorize_route("connector.configure", "slack", require_mfa=True))],
)
async def set_slack_connector(
    body: ConnectorToggleUpdate,
    session: AsyncSession = Depends(get_session),
) -> ConnectorsOut:
    settings = config.get_settings()
    if body.enabled and not slack_configured(settings):
        raise ConnectorError(
            ErrorCode.CONNECTOR_NOT_CONFIGURED,
            409,
            "Slack approvals require configured credentials and development mode.",
        )
    row = await connector_setting_row(session)
    row.slack_enabled = body.enabled
    session.add(row)
    await session.commit()
    configured = slack_configured(settings)
    return ConnectorsOut(
        slack=ConnectorStatusOut(configured=configured, enabled=configured and row.slack_enabled)
    )
