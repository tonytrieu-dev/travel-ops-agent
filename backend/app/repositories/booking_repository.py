"""The HITL booking state machine, enforced against the database.

Every state change goes through here and, in the same transaction, writes an append-only
BookingTransition — so "a human confirmed before the gated write" is a fact recorded in an
immutable audit row, not a claim. ``execute_booking`` is the only gated action and the highest-
value guard: it claims the row with SELECT ... FOR UPDATE, re-checks state under the lock, and
fetches booking options exactly once, so concurrent double-clicks can never double-book or burn
a second quota call.
"""

import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.config import BOOKING_TTL_MINUTES
from app.models import (
    BookingTransition,
    FlightSearchResult,
    HITLBookingLog,
    TripRequest,
    User,
    utcnow,
)
from app.request_context import correlation_id
from app.schemas import ErrorCode
from app.state import ALLOWED_TRANSITIONS, BookingState, BookingTransitionReason

BookingOptionsFetcher = Callable[[FlightSearchResult], Awaitable[list[dict]]]
_ACTIVE_BOOKING_STATES = (
    BookingState.PENDING_USER_CONFIRMATION,
    BookingState.CONFIRMED,
)


class BookingError(Exception):
    """A domain rejection carrying the client-facing code, HTTP status, and root-cause detail."""

    def __init__(self, code: ErrorCode, status_code: int, detail: str) -> None:
        self.code = code
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def _is_expired(booking: HITLBookingLog) -> bool:
    return booking.expires_at < utcnow()


def _expiry_detail(booking: HITLBookingLog) -> str:
    quoted_at = booking.expires_at - timedelta(minutes=BOOKING_TTL_MINUTES)
    return (
        f"This fare was quoted at {quoted_at:%Y-%m-%dT%H:%MZ} and its "
        f"{BOOKING_TTL_MINUTES}-minute price-freshness window ended at "
        f"{booking.expires_at:%Y-%m-%dT%H:%MZ}; "
        "search again for current prices."
    )


def _record_transition(
    session: AsyncSession,
    booking: HITLBookingLog,
    to_state: BookingState,
    reason: BookingTransitionReason,
    actor_user_id: int | None,
) -> None:
    assert booking.id is not None, "cannot record a transition for an unpersisted booking"
    session.add(
        BookingTransition(
            booking_log_id=booking.id,
            from_state=booking.state,
            to_state=to_state,
            reason=reason.value,
            actor_user_id=actor_user_id,
            correlation_id=correlation_id(),
        )
    )
    booking.state = to_state


async def _lock_booking(session: AsyncSession, log_id: int) -> HITLBookingLog | None:
    result = await session.execute(
        select(HITLBookingLog).where(col(HITLBookingLog.id) == log_id).with_for_update()
    )
    return result.scalar_one_or_none()


async def request_booking(
    session: AsyncSession, trip_id: int, flight_search_result_id: int
) -> HITLBookingLog:
    trip = await session.get(TripRequest, trip_id)
    if trip is None:
        raise BookingError(ErrorCode.TRIP_NOT_FOUND, 404, f"No trip {trip_id}.")

    flight = await session.get(FlightSearchResult, flight_search_result_id)
    if flight is None or flight.trip_request_id != trip_id:
        raise BookingError(
            ErrorCode.FLIGHT_NOT_FOUND,
            404,
            f"Flight offer {flight_search_result_id} does not belong to trip {trip_id}.",
        )

    active_booking_query = select(HITLBookingLog).where(
        col(HITLBookingLog.trip_request_id) == trip_id,
        col(HITLBookingLog.flight_search_result_id) == flight_search_result_id,
        col(HITLBookingLog.state).in_(_ACTIVE_BOOKING_STATES),
    )
    existing = await session.scalar(active_booking_query)
    if existing is not None:
        return existing

    booking = HITLBookingLog(
        trip_request_id=trip_id,
        flight_search_result_id=flight_search_result_id,
        requested_by_user_id=trip.user_id,
        expires_at=utcnow() + timedelta(minutes=BOOKING_TTL_MINUTES),
    )
    session.add(booking)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = await session.scalar(active_booking_query)
        if existing is None:
            raise
        return existing
    return booking


async def get_booking_with_transitions(
    session: AsyncSession, log_id: int
) -> tuple[HITLBookingLog, list[BookingTransition]]:
    booking = await session.get(HITLBookingLog, log_id)
    if booking is None:
        raise BookingError(ErrorCode.BOOKING_NOT_FOUND, 404, f"No booking log {log_id}.")
    transitions = list(
        await session.scalars(
            select(BookingTransition)
            .where(col(BookingTransition.booking_log_id) == log_id)
            .order_by(col(BookingTransition.created_at), col(BookingTransition.id))
        )
    )
    return booking, transitions


async def actor_emails_for(
    session: AsyncSession, transitions: list[BookingTransition]
) -> dict[int, str]:
    """One lookup covering every actor in the given transitions, so rendering "who approved this"
    can't fan out into a query per audit row. An anonymized user has no email and is simply
    absent from the map — the audit row itself still stands.
    """
    actor_ids = {
        transition.actor_user_id
        for transition in transitions
        if transition.actor_user_id is not None
    }
    if not actor_ids:
        return {}
    rows = await session.execute(
        select(col(User.id), col(User.email)).where(
            col(User.id).in_(actor_ids), col(User.email).is_not(None)
        )
    )
    return {user_id: email for user_id, email in rows if email is not None}


async def list_bookings_with_transitions_for_user(
    session: AsyncSession, user_id: int
) -> list[tuple[HITLBookingLog, list[BookingTransition]]]:
    """Every booking this user requested, newest first, each with its own transition trail — the
    approval-history tab is global so an approval doesn't disappear once its trip stops being the
    active one. Two bulk queries grouped in Python rather than one per booking.
    """
    bookings = list(
        await session.scalars(
            select(HITLBookingLog)
            .where(col(HITLBookingLog.requested_by_user_id) == user_id)
            .order_by(col(HITLBookingLog.created_at).desc())
        )
    )
    if not bookings:
        return []
    transitions = await session.scalars(
        select(BookingTransition)
        .where(col(BookingTransition.booking_log_id).in_([booking.id for booking in bookings]))
        .order_by(col(BookingTransition.created_at), col(BookingTransition.id))
    )
    transitions_by_booking_id: dict[int, list[BookingTransition]] = defaultdict(list)
    for transition in transitions:
        transitions_by_booking_id[transition.booking_log_id].append(transition)
    return [
        (booking, transitions_by_booking_id[booking.id]) for booking in bookings if booking.id
    ]


async def confirm_booking(session: AsyncSession, log_id: int) -> HITLBookingLog:
    booking = await _lock_booking(session, log_id)
    if booking is None:
        raise BookingError(ErrorCode.BOOKING_NOT_FOUND, 404, f"No booking log {log_id}.")
    if booking.state == BookingState.CONFIRMED:
        return booking  # idempotent: a double-clicked confirm is expected human behavior
    if booking.state == BookingState.PENDING_USER_CONFIRMATION and _is_expired(booking):
        _record_transition(
            session, booking, BookingState.EXPIRED, BookingTransitionReason.EXPIRE, None
        )
        await session.commit()
        raise BookingError(ErrorCode.BOOKING_EXPIRED, 409, _expiry_detail(booking))
    if booking.state != BookingState.PENDING_USER_CONFIRMATION:
        raise BookingError(
            ErrorCode.INVALID_TRANSITION,
            409,
            f"Cannot confirm a booking in state {booking.state}.",
        )

    booking.confirmed_at = utcnow()
    _record_transition(
        session,
        booking,
        BookingState.CONFIRMED,
        BookingTransitionReason.CONFIRM,
        booking.requested_by_user_id,
    )
    await session.commit()
    return booking


async def execute_booking(
    session: AsyncSession, log_id: int, fetch_options: BookingOptionsFetcher
) -> HITLBookingLog:
    booking = await _lock_booking(session, log_id)
    if booking is None:
        raise BookingError(ErrorCode.BOOKING_NOT_FOUND, 404, f"No booking log {log_id}.")
    if booking.state == BookingState.EXECUTED:
        return booking  # idempotent: the losing racer reuses the stored result, no second fetch
    if booking.state == BookingState.CONFIRMED and _is_expired(booking):
        _record_transition(
            session, booking, BookingState.EXPIRED, BookingTransitionReason.EXPIRE, None
        )
        await session.commit()
        raise BookingError(ErrorCode.BOOKING_EXPIRED, 409, _expiry_detail(booking))
    if booking.state != BookingState.CONFIRMED:
        raise BookingError(
            ErrorCode.INVALID_TRANSITION,
            409,
            f"Cannot execute a booking in state {booking.state}; a human must confirm it first.",
        )

    flight = await session.get(FlightSearchResult, booking.flight_search_result_id)
    assert flight is not None, "booking references a flight row that no longer exists"
    # The human's confirm-then-execute decision is already real and structural (see
    # DECISIONS.md); booking_options are supplementary airline/OTA links, not the thing being
    # gated. An upstream hiccup fetching them must not block the execute itself — degrade
    # honestly (no links) rather than fabricate a link or block a real, human-confirmed action.
    try:
        booking_options = await fetch_options(flight)
    except Exception:
        booking_options = []
    booking.booking_options = booking_options
    booking.booking_reference = f"TA-{booking.id}-{uuid.uuid4().hex[:10].upper()}"
    booking.executed_at = utcnow()
    _record_transition(
        session,
        booking,
        BookingState.EXECUTED,
        BookingTransitionReason.EXECUTE,
        booking.requested_by_user_id,
    )
    await session.commit()
    return booking


async def cancel_booking(session: AsyncSession, log_id: int) -> HITLBookingLog:
    booking = await _lock_booking(session, log_id)
    if booking is None:
        raise BookingError(ErrorCode.BOOKING_NOT_FOUND, 404, f"No booking log {log_id}.")
    if booking.state == BookingState.CANCELLED:
        return booking  # idempotent
    if BookingState.CANCELLED not in ALLOWED_TRANSITIONS[booking.state]:
        raise BookingError(
            ErrorCode.INVALID_TRANSITION,
            409,
            f"Cannot cancel a booking in terminal state {booking.state}.",
        )

    _record_transition(
        session,
        booking,
        BookingState.CANCELLED,
        BookingTransitionReason.CANCEL,
        booking.requested_by_user_id,
    )
    await session.commit()
    return booking
