"""Small security-operations surface for the local enterprise demo."""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.dependencies import get_security_context
from app.schemas import SecurityEventOut, SecurityEventQuery, SecurityIncidentOut
from app.security_admission import authorize_route
from app.security_evidence import get_security_events, get_security_incidents
from app.security_identity import SecurityContext
from app.security_segment import enforce_api_segment

router = APIRouter(
    prefix="/api/security",
    tags=["security"],
    dependencies=[Depends(enforce_api_segment)],
)


@router.get(
    "/events",
    response_model=list[SecurityEventOut],
    dependencies=[Depends(authorize_route("security.read", "security-events", require_mfa=True))],
)
async def list_security_events(
    query: SecurityEventQuery = Depends(),
    session: AsyncSession = Depends(get_session),
    context: SecurityContext = Depends(get_security_context),
) -> list[SecurityEventOut]:
    events = await get_security_events(session, context.tenant_id, query)
    return [SecurityEventOut.model_validate(event) for event in events]


@router.get(
    "/incidents",
    response_model=list[SecurityIncidentOut],
    dependencies=[Depends(authorize_route("security.read", "security-incidents", require_mfa=True))],
)
async def list_incidents(
    session: AsyncSession = Depends(get_session),
    context: SecurityContext = Depends(get_security_context),
) -> list[SecurityIncidentOut]:
    incidents = await get_security_incidents(session, context.tenant_id)
    return [SecurityIncidentOut.model_validate(incident) for incident in incidents]
