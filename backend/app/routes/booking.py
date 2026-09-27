"""HITL booking routes. Each handler stays thin: call the repository (which owns the state
machine + audit), then shape the ORM row into the response model. Domain rejections raise
BookingError, rendered as a ProblemDetail by the app-level handler in main.py.
"""


import logging
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.slack_hitl import notify_pending_approval
from app.config import get_settings
from app.db import get_session
from app.dbos_runtime import execute_booking_durable
from app.dependencies import SecurityContext, get_current_user, get_security_context
from app.models import (
    BookingTransition,
    FlightSearchResult,
    HITLBookingLog,
    TripRequest,
    User,
)
from app.repositories import booking_repository as repository
from app.routes.connectors import slack_notifications_enabled
from app.security import authorize, enforce_api_segment, require_owned_booking, require_owned_trip
from app.request_context import correlation_id
from app.schemas import (
    BookingLogOut,
    BookingRequestCreate,
    BookingTransitionOut,
    ProblemDetail,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api",
    tags=["booking"],
    dependencies=[Depends(enforce_api_segment)],
)

_NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"model": ProblemDetail}}
_NOT_FOUND_OR_CONFLICT: dict[int | str, dict[str, Any]] = {
    404: {"model": ProblemDetail},
    409: {"model": ProblemDetail},
}
_EXECUTE_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: {"model": ProblemDetail},
    409: {"model": ProblemDetail},
    502: {"model": ProblemDetail},
}


def _to_transition_out(
    transition: BookingTransition, actor_emails: dict[int, str]
) -> BookingTransitionOut:
    out = BookingTransitionOut.model_validate(transition)
    if transition.actor_user_id is not None:
        out.actor_email = actor_emails.get(transition.actor_user_id)
    return out


def _to_out(
    booking: HITLBookingLog,
    transitions: list[BookingTransition] | None = None,
    actor_emails: dict[int, str] | None = None,
) -> BookingLogOut:
    out = BookingLogOut.model_validate(booking)
    if transitions is not None:
        out.transitions = [
            _to_transition_out(transition, actor_emails or {}) for transition in transitions
        ]
    return out


@router.post(
    "/trips/{trip_id}/booking/request", response_model=BookingLogOut, responses=_NOT_FOUND
)
async def request_booking(
    trip_id: int,
    body: BookingRequestCreate,
    request: Request,
    session: AsyncSession = Depends(get_session),
    context: SecurityContext = Depends(get_security_context),
) -> BookingLogOut:
    await authorize(context, session, action="booking.request", resource=f"trip:{trip_id}", request=request)
    await require_owned_trip(session, context, trip_id, request)
    booking = await repository.request_booking(session, trip_id, body.flight_search_result_id)
    await _notify_slack_if_enabled(session, booking)
    return _to_out(booking)


async def _notify_slack_if_enabled(session: AsyncSession, booking: HITLBookingLog) -> None:
    settings = get_settings()
    if not await slack_notifications_enabled(session, settings):
        return
    trip = await session.get(TripRequest, booking.trip_request_id)
    flight = await session.get(FlightSearchResult, booking.flight_search_result_id)
    assert trip is not None and flight is not None, (
        "request_booking already validated these exist"
    )
    if not await notify_pending_approval(settings, booking, trip, flight):
        logger.warning("booking=%r requested but Slack was not notified", booking.id)


@router.get("/bookings", response_model=list[BookingLogOut])
async def list_bookings(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
    context: SecurityContext = Depends(get_security_context),
) -> list[BookingLogOut]:
    """Backs the global approval-history tab: every booking this user requested, each with its
    append-only transition trail."""
    assert user.id is not None, "get_current_user must always return a persisted user"
    await authorize(context, session, action="trip.read", resource="bookings", request=request)
    bookings = await repository.list_bookings_with_transitions_for_user(session, user.id)
    every_transition = [transition for _, transitions in bookings for transition in transitions]
    actor_emails = await repository.actor_emails_for(session, every_transition)
    return [_to_out(booking, transitions, actor_emails) for booking, transitions in bookings]


@router.get("/bookings/{log_id}", response_model=BookingLogOut, responses=_NOT_FOUND)
async def get_booking(
    log_id: int, request: Request, session: AsyncSession = Depends(get_session), context: SecurityContext = Depends(get_security_context)
) -> BookingLogOut:
    await authorize(context, session, action="trip.read", resource=f"booking:{log_id}", request=request)
    await require_owned_booking(session, context, log_id, request)
    booking, transitions = await repository.get_booking_with_transitions(session, log_id)
    return _to_out(booking, transitions, await repository.actor_emails_for(session, transitions))


@router.post(
    "/bookings/{log_id}/confirm", response_model=BookingLogOut, responses=_NOT_FOUND_OR_CONFLICT
)
async def confirm_booking(
    log_id: int, request: Request, session: AsyncSession = Depends(get_session), context: SecurityContext = Depends(get_security_context)
) -> BookingLogOut:
    await authorize(context, session, action="booking.approve", resource=f"booking:{log_id}", request=request, require_mfa=True)
    await require_owned_booking(session, context, log_id, request)
    booking = await repository.confirm_booking(session, log_id)
    return _to_out(booking)


@router.post(
    "/bookings/{log_id}/execute", response_model=BookingLogOut, responses=_EXECUTE_RESPONSES
)
async def execute_booking(log_id: int, request: Request, session: AsyncSession = Depends(get_session), context: SecurityContext = Depends(get_security_context)) -> BookingLogOut:
    await authorize(context, session, action="booking.execute", resource=f"booking:{log_id}", request=request, require_mfa=True)
    await require_owned_booking(session, context, log_id, request)
    return await execute_booking_durable(log_id, correlation_id())


@router.post(
    "/bookings/{log_id}/cancel", response_model=BookingLogOut, responses=_NOT_FOUND_OR_CONFLICT
)
async def cancel_booking(
    log_id: int, request: Request, session: AsyncSession = Depends(get_session), context: SecurityContext = Depends(get_security_context)
) -> BookingLogOut:
    await authorize(context, session, action="booking.approve", resource=f"booking:{log_id}", request=request, require_mfa=True)
    await require_owned_booking(session, context, log_id, request)
    booking = await repository.cancel_booking(session, log_id)
    return _to_out(booking)
