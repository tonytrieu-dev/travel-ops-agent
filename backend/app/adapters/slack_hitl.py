"""Slack HITL connector adapter: builds the Block Kit approval message, verifies Slack's
webhook signature, and resolves an approve/reject click against the real booking state machine.
No SDK — the surface is one outbound POST and one signed inbound callback, well within what
httpx + stdlib hmac covers on their own.
"""

import hashlib
import hmac
import logging
import time
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import SLACK_API_TIMEOUT_SECONDS, Settings
from app.models import ConnectorSetting, FlightSearchResult, HITLBookingLog, TripRequest
from app.repositories import booking_repository as repository
from app.repositories.booking_repository import BookingError

logger = logging.getLogger(__name__)

SLACK_SIGNATURE_MAX_AGE_SECONDS = 5 * 60
_APPROVE_ACTION_ID = "approve_booking"
_REJECT_ACTION_ID = "reject_booking"


def slack_configured(settings: Settings) -> bool:
    # shortcut: Slack users are unmapped; add owner/role/MFA checks before enabling enforced approvals.
    return bool(
        not settings.zero_trust_enforced
        and settings.slack_bot_token
        and settings.slack_signing_secret
        and settings.slack_approvals_channel_id
    )


async def connector_setting_row(session: AsyncSession) -> ConnectorSetting:
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
    row = await connector_setting_row(session)
    return row.slack_enabled


def _format_local_flight_time(raw: str) -> str:
    """flight.depart_at/arrive_at are wall-clock strings in the departure/arrival airport's own
    local time, straight from SearchApi, with no UTC offset attached — so this only reformats
    for readability, it never converts a timezone (there's no zone info to convert from)."""
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return raw
    return parsed.strftime("%b %-d, %Y, %-I:%M %p")


def _slack_date_directive(moment_utc: datetime) -> str:
    """Slack's `<!date^...>` renders client-side in each viewer's own local timezone, unlike a
    plain string — the right tool here because expires_at is a real UTC instant (unlike
    flight.depart_at above, which has no zone to convert from)."""
    epoch_seconds = int(moment_utc.replace(tzinfo=UTC).timestamp())
    fallback = moment_utc.isoformat()
    return f"<!date^{epoch_seconds}^{{date_short_pretty}} at {{time}}|{fallback}>"


def _compute_slack_signature(raw_body: bytes, timestamp: str, signing_secret: str) -> str:
    base_string = f"v0:{timestamp}:".encode() + raw_body
    digest = hmac.new(signing_secret.encode(), base_string, hashlib.sha256).hexdigest()
    return f"v0={digest}"


def verify_slack_signature(
    raw_body: bytes,
    timestamp: str,
    signature: str,
    signing_secret: str,
    *,
    now: float | None = None,
) -> bool:
    try:
        timestamp_seconds = int(timestamp)
    except ValueError:
        return False
    current_time = now if now is not None else time.time()
    if abs(current_time - timestamp_seconds) > SLACK_SIGNATURE_MAX_AGE_SECONDS:
        return False
    expected = _compute_slack_signature(raw_body, timestamp, signing_secret)
    return hmac.compare_digest(expected, signature)


async def resolve_approve(session: AsyncSession, booking_log_id: int) -> str:
    try:
        await repository.confirm_booking(session, booking_log_id)
    except BookingError as error:
        return error.detail
    return "Approved — continue in the app to complete booking with the airline."


async def resolve_reject(session: AsyncSession, booking_log_id: int) -> str:
    try:
        await repository.cancel_booking(session, booking_log_id)
    except BookingError as error:
        return error.detail
    return "Rejected — this fare will not be booked."


def build_approval_blocks(
    trip: TripRequest, flight: FlightSearchResult, booking: HITLBookingLog
) -> dict[str, Any]:
    stops_text = "Nonstop" if flight.stops == 0 else f"{flight.stops} stop(s)"
    return {
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "Flight approval needed"},
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Trip:*\n#{trip.id}"},
                    {
                        "type": "mrkdwn",
                        "text": f"*Route:*\n{trip.origin} → {trip.destination_airport}",
                    },
                    {"type": "mrkdwn", "text": f"*Carrier:*\n{flight.carrier}"},
                    {
                        "type": "mrkdwn",
                        "text": f"*Price:*\n${flight.price_usd:,.2f} {flight.currency}",
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*Departs:*\n{_format_local_flight_time(flight.depart_at)}",
                    },
                    {"type": "mrkdwn", "text": f"*Stops:*\n{stops_text}"},
                    {
                        "type": "mrkdwn",
                        "text": f"*Approval expires:*\n{_slack_date_directive(booking.expires_at)}",
                    },
                ],
            },
            {"type": "divider"},
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": (
                            "Approve to confirm this fare, or reject to cancel the request. "
                            "Approving does not complete the booking — you'll continue in "
                            "the app to finish it with the airline."
                        ),
                    }
                ],
            },
            {
                "type": "actions",
                "block_id": "booking_actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": _APPROVE_ACTION_ID,
                        "style": "primary",
                        "text": {"type": "plain_text", "text": "Approve flight"},
                        "value": str(booking.id),
                    },
                    {
                        "type": "button",
                        "action_id": _REJECT_ACTION_ID,
                        "style": "danger",
                        "text": {"type": "plain_text", "text": "Reject"},
                        "value": str(booking.id),
                    },
                ],
            },
        ],
    }


def build_resolution_message(
    outcome_text: str, original_blocks: list[dict[str, Any]]
) -> dict[str, Any]:
    if outcome_text.startswith("Approved"):
        status = (
            ":white_check_mark: *Approved* — continue in the app to complete booking "
            "with the airline."
        )
    elif outcome_text.startswith("Rejected"):
        status = ":no_entry_sign: *Rejected* — return to the app to choose another flight."
    else:
        status = f":warning: {outcome_text}"
    return {
        "text": status,
        "blocks": [
            *[
                block
                for block in original_blocks
                if block.get("type") not in {"actions", "context"}
            ],
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": status},
            },
        ],
    }


async def update_approval_message(
    settings: Settings, payload: dict[str, Any], outcome_text: str
) -> bool:
    resolution = build_resolution_message(
        outcome_text, payload.get("message", {}).get("blocks") or []
    )
    container = payload.get("container", {})
    channel_id = container.get("channel_id") or payload.get("channel", {}).get("id")
    message_ts = container.get("message_ts") or payload.get("message", {}).get("ts")
    if not channel_id or not message_ts or not settings.slack_bot_token:
        return False
    try:
        async with httpx.AsyncClient(timeout=SLACK_API_TIMEOUT_SECONDS) as client:
            response = await client.post(
                "https://slack.com/api/chat.update",
                headers={
                    "Authorization": f"Bearer {settings.slack_bot_token.get_secret_value()}"
                },
                json={
                    "channel": channel_id,
                    "ts": message_ts,
                    "text": resolution["text"],
                    "blocks": resolution["blocks"],
                    "attachments": [],
                },
            )
            if response.is_error or not response.json().get("ok", False):
                logger.warning("Slack chat.update failed for booking action: %s", response.text)
                return False
            return True
    except (httpx.HTTPError, ValueError) as error:
        logger.warning("Slack chat.update failed for booking action: %r", error)
        return False


def parse_block_action(
    payload: dict[str, Any], expected_channel_id: str
) -> tuple[str, int] | None:
    if payload.get("type") != "block_actions":
        return None
    if payload.get("channel", {}).get("id") != expected_channel_id:
        return None
    actions = payload.get("actions") or []
    if len(actions) != 1:
        return None
    action_id = actions[0].get("action_id")
    if action_id not in (_APPROVE_ACTION_ID, _REJECT_ACTION_ID):
        return None
    try:
        booking_log_id = int(actions[0].get("value"))
    except (TypeError, ValueError):
        return None
    return action_id, booking_log_id


async def resolve_block_action(
    session: AsyncSession, payload: dict[str, Any], expected_channel_id: str
) -> str | None:
    """Owns decode -> dispatch -> resolve for a Slack interaction payload. Returns the
    resolution message to show in Slack, or None if the payload wasn't a recognized
    approve/reject click (the caller should just 200 it and move on)."""
    parsed = parse_block_action(payload, expected_channel_id=expected_channel_id)
    if parsed is None:
        return None
    action_id, booking_log_id = parsed
    if action_id == _APPROVE_ACTION_ID:
        return await resolve_approve(session, booking_log_id)
    return await resolve_reject(session, booking_log_id)


async def notify_pending_approval(
    settings: Settings, booking: HITLBookingLog, trip: TripRequest, flight: FlightSearchResult
) -> bool:
    """Returns whether the Slack message was actually posted. A booking request must never
    fail because Slack is unreachable, but the caller still needs to know the human wasn't
    notified there, so it can fall back to telling them to use the in-app buttons instead."""
    assert settings.slack_bot_token is not None
    assert settings.slack_approvals_channel_id is not None
    blocks = build_approval_blocks(trip, flight, booking)
    try:
        async with httpx.AsyncClient(timeout=SLACK_API_TIMEOUT_SECONDS) as client:
            await client.post(
                "https://slack.com/api/chat.postMessage",
                headers={
                    "Authorization": f"Bearer {settings.slack_bot_token.get_secret_value()}"
                },
                json={"channel": settings.slack_approvals_channel_id, **blocks},
            )
        return True
    except httpx.HTTPError as error:
        logger.warning("slack notify_pending_approval failed: %r for booking=%r", error, booking.id)
        return False
