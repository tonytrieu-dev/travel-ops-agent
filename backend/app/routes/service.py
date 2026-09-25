"""Internal service boundary: browsers never receive or select this token."""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.schemas import ProblemDetail, ServiceHeartbeatOut
from app.security import record_security_event, require_service_token

router = APIRouter(prefix="/internal", tags=["internal"])


@router.post("/agent/heartbeat", response_model=ServiceHeartbeatOut, responses={401: {"model": ProblemDetail}})
async def agent_heartbeat(request: Request, session: AsyncSession = Depends(get_session)) -> ServiceHeartbeatOut:
    require_service_token(request, get_settings().agent_service_token.get_secret_value())
    await record_security_event(
        session,
        None,
        action="service.call",
        resource="agent-planner",
        decision="allow",
        reason="signed internal service token",
        request=request,
        source_segment="agent",
    )
    return ServiceHeartbeatOut()
