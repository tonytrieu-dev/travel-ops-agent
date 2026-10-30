"""Small security-operations surface for the local enterprise demo."""

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.dependencies import get_security_context
from app.models import utcnow
from app.repositories.compliance_repository import get_assessments
from app.schemas import (
    ComplianceAssessmentOut, ComplianceMonitoringOut,
    SecurityEventOut, SecurityEventQuery, SecurityIncidentOut,
)
from app.security_admission import authorize_route
from app.security_evidence import get_security_event, get_security_events, get_security_incidents
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


@router.get(
    "/monitoring", response_model=ComplianceMonitoringOut,
    dependencies=[Depends(authorize_route("security.read", "compliance-monitoring", require_mfa=True))],
)
async def get_monitoring(
    session: AsyncSession = Depends(get_session),
    context: SecurityContext = Depends(get_security_context),
) -> ComplianceMonitoringOut:
    assessments = await get_assessments(session, context.tenant_id)
    return ComplianceMonitoringOut(
        stale=not assessments or assessments[0].assessed_at < utcnow() - timedelta(minutes=3),
        assessments=[ComplianceAssessmentOut.model_validate(assessment) for assessment in assessments],
    )


@router.get(
    "/events/{event_id}", response_model=SecurityEventOut,
    dependencies=[Depends(authorize_route("security.read", "security-event", require_mfa=True))],
)
async def get_event(
    event_id: int,
    session: AsyncSession = Depends(get_session),
    context: SecurityContext = Depends(get_security_context),
) -> SecurityEventOut:
    event = await get_security_event(session, context.tenant_id, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Security event not found")
    return SecurityEventOut.model_validate(event)
