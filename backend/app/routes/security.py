"""Small security-operations surface for the local enterprise demo."""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.dependencies import get_security_context
from app.schemas import SecurityEventOut, SecurityEventQuery, SecurityIncidentOut
from app.security_admission import authorize
from app.security_evidence import get_security_events, get_security_incidents
from app.security_identity import SecurityContext
from app.security_segment import enforce_api_segment

router = APIRouter(
    prefix="/api/security",
    tags=["security"],
    dependencies=[Depends(enforce_api_segment)],
)


@router.get("/events", response_model=list[SecurityEventOut])
async def list_security_events(
    request: Request,
    query: SecurityEventQuery = Depends(),
    session: AsyncSession = Depends(get_session),
    context: SecurityContext = Depends(get_security_context),
) -> list[SecurityEventOut]:
    await authorize(context, session, action="security.read", resource="security-events", request=request, require_mfa=True)
    events = await get_security_events(session, context.tenant_id, query)
    return [SecurityEventOut.model_validate(event) for event in events]


@router.get("/incidents", response_model=list[SecurityIncidentOut])
async def list_incidents(
    request: Request,
    session: AsyncSession = Depends(get_session),
    context: SecurityContext = Depends(get_security_context),
) -> list[SecurityIncidentOut]:
    await authorize(context, session, action="security.read", resource="security-incidents", request=request, require_mfa=True)
    incidents = await get_security_incidents(session, context.tenant_id)
    return [SecurityIncidentOut.model_validate(incident) for incident in incidents]
