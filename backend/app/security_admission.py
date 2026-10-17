"""Protected-resource admission: policy, ownership, hiding, and one decision."""

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import HITLBookingLog, TripRequest, User
from app.repositories.booking_repository import BookingError
from app.repositories.trips_repository import TripError
from app.schemas import ErrorCode
from app.security_evidence import record_security_event
from app.security_identity import SecurityContext

_ROLE_ACTIONS = {
    "traveler": {
        "trip.read", "trip.create", "trip.update", "trip.plan", "trip.execution.read",
        "flight.search", "booking.read", "booking.request", "booking.approve",
        "booking.cancel", "booking.execute",
    },
    "trip-planner": {
        "trip.read", "trip.create", "trip.update", "trip.plan", "trip.execution.read",
        "flight.search", "booking.read",
    },
    "security-operator": {"security.read", "connector.configure"},
    "admin": {
        "trip.read", "trip.create", "trip.update", "trip.plan", "trip.execution.read",
        "flight.search", "booking.read", "booking.request", "booking.approve",
        "booking.cancel", "booking.execute", "security.read", "connector.configure",
    },
}
_ALLOW_REASONS = {"allowed by role policy", "development security enforcement disabled"}


class SecurityError(Exception):
    def __init__(self, detail: str, status_code: int = 403) -> None:
        self.detail = detail
        self.status_code = status_code
        super().__init__(detail)


def _authorization_reason(
    context: SecurityContext, action: str, require_mfa: bool
) -> str:
    if not get_settings().zero_trust_enforced:
        return "development security enforcement disabled"
    if action not in _ROLE_ACTIONS.get(context.role, set()):
        return "role is not allowed to perform action"
    if require_mfa and not context.mfa_verified:
        return "MFA required for privileged action"
    return "allowed by role policy"


async def authorize(
    context: SecurityContext,
    session: AsyncSession,
    *,
    action: str,
    resource: str,
    request: Request | None = None,
    require_mfa: bool = False,
) -> None:
    reason = _authorization_reason(context, action, require_mfa)
    decision = "allow" if reason in _ALLOW_REASONS else "deny"
    await record_security_event(
        session, context, action=action, resource=resource, decision=decision,
        reason=reason, request=request,
    )
    if decision == "deny":
        raise SecurityError(reason)


async def _admit(
    session: AsyncSession,
    context: SecurityContext,
    *,
    action: str,
    resource: str,
    request: Request | None,
    require_mfa: bool,
    exists: bool,
    owned: bool,
    not_found: Exception,
) -> None:
    if not exists:
        reason = "resource does not exist or is not visible"
    elif get_settings().zero_trust_enforced and not owned:
        reason = "tenant or owner mismatch"
    else:
        reason = _authorization_reason(context, action, require_mfa)
    if reason not in _ALLOW_REASONS:
        await record_security_event(
            session, context, action=action, resource=resource, decision="deny", reason=reason, request=request
        )
        if not exists or not owned:
            raise not_found
        raise SecurityError(reason)
    await record_security_event(
        session, context, action=action, resource=resource, decision="allow", reason=reason, request=request
    )


async def admit_trip(
    session: AsyncSession,
    context: SecurityContext,
    trip_id: int,
    *,
    action: str,
    request: Request | None = None,
    require_mfa: bool = False,
) -> TripRequest:
    trip = await session.get(TripRequest, trip_id)
    owner = await session.get(User, trip.user_id) if trip is not None else None
    owned = bool(
        trip is not None and owner is not None
        and owner.tenant_id == context.tenant_id and trip.user_id == context.user_id
    )
    not_found = TripError(ErrorCode.TRIP_NOT_FOUND, 404, f"No trip {trip_id}.")
    await _admit(
        session, context, action=action, resource=f"trip:{trip_id}", request=request,
        require_mfa=require_mfa, exists=trip is not None, owned=owned, not_found=not_found,
    )
    if trip is None:
        raise not_found
    return trip


async def admit_booking(
    session: AsyncSession,
    context: SecurityContext,
    log_id: int,
    *,
    action: str,
    request: Request | None = None,
    require_mfa: bool = False,
) -> HITLBookingLog:
    booking = await session.get(HITLBookingLog, log_id)
    trip = await session.get(TripRequest, booking.trip_request_id) if booking is not None else None
    owner = await session.get(User, trip.user_id) if trip is not None else None
    owned = bool(
        booking is not None and trip is not None and owner is not None
        and owner.tenant_id == context.tenant_id and trip.user_id == context.user_id
    )
    not_found = BookingError(ErrorCode.BOOKING_NOT_FOUND, 404, f"No booking log {log_id}.")
    await _admit(
        session, context, action=action, resource=f"booking:{log_id}", request=request,
        require_mfa=require_mfa, exists=booking is not None and trip is not None,
        owned=owned, not_found=not_found,
    )
    if booking is None:
        raise not_found
    return booking
