"""HTTP-level wiring of the zero-trust controls, through the real FastAPI dependency graph.

test_security.py proves the *decisions*; this file proves the *wiring* those decisions hang on,
which no direct-call unit test can see:

- the protected routers actually depend on get_security_context + enforce_api_segment,
- get_security_context returns 401 when enforcement is on and no bearer token is presented,
- SecurityError is mapped to a 403 ProblemDetail by the app's exception handler,
- a full login -> MFA -> bearer-token -> protected-request round trip succeeds.

A route that silently dropped its security dependency would pass direct-call tests and fail here.
The booking and containment walkthroughs also exercise the state machine and audit persistence
with enforcement enabled, so legacy tests running in development mode cannot mask a wiring gap.
"""

import pytest

from app.config import Settings
from app.models import SecurityEvent, TripRequest
from app.security_identity import totp_code
from app.state import BookingState
from tests.db_helpers import TEST_DATABASE_URL, run_db

_SIGNING_SECRET = "integration-test-signing-secret-at-least-32-bytes"


@pytest.fixture
def enforced_client(client, monkeypatch: pytest.MonkeyPatch):
    """The shared TestClient, with zero-trust enforcement switched on for the request path."""
    settings = Settings(
        cerebras_api_key="x",
        searchapi_api_key="x",
        tavily_api_key="x",
        database_url=TEST_DATABASE_URL,
        zero_trust_enforced=True,
        zero_trust_signing_secret=_SIGNING_SECRET,
    )
    monkeypatch.setattr("app.config.get_settings", lambda: settings)
    yield client


def _seed_user(
    email: str, tenant_id: str, role: str, device_id: str, *, mfa_enabled: bool = True
) -> int:
    from app.models import User
    from app.security_identity import password_hash

    async def _work(session) -> int:
        user = User(
            email=email,
            tenant_id=tenant_id,
            role=role,
            device_id=device_id,
            password_hash=password_hash("demo-password"),
            mfa_enabled=mfa_enabled,
        )
        session.add(user)
        await session.flush()
        assert user.id is not None
        return user.id

    return run_db(_work)


def _sign_in(enforced_client, email: str, device_id: str) -> str:
    login = enforced_client.post(
        "/api/auth/login",
        json={"email": email, "password": "demo-password", "device_id": device_id},
    )
    assert login.status_code == 200, login.text
    body = login.json()
    mfa = enforced_client.post(
        "/api/auth/mfa",
        json={"session_id": body["session_id"], "code": totp_code("JBSWY3DPEHPK3PXP")},
    )
    assert mfa.status_code == 200, mfa.text
    return mfa.json()["access_token"]


def _owned_booking(user_id: int) -> int:
    from tests.db_helpers import get_booking, seed_booking

    async def _work(session) -> int:
        booking_id = await seed_booking(
            session, state=BookingState.PENDING_USER_CONFIRMATION, expires_in_minutes=30
        )
        booking = await get_booking(session, booking_id)
        trip = await session.get(TripRequest, booking.trip_request_id)
        assert trip is not None
        trip.user_id = user_id
        booking.requested_by_user_id = user_id
        return booking_id

    return run_db(_work)


def test_password_only_login_allows_reads_but_not_booking_approval(enforced_client) -> None:
    user_id = _seed_user("traveler@tenant-a.test", "tenant-a", "traveler", "device-a", mfa_enabled=False)
    response = enforced_client.post(
        "/api/auth/login",
        json={"email": "traveler@tenant-a.test", "password": "demo-password", "device_id": "device-a"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["mfa_required"] is False
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
    assert enforced_client.get("/api/trips", headers=headers).status_code == 200

    booking_id = _owned_booking(user_id)
    approval = enforced_client.post(f"/api/bookings/{booking_id}/confirm", headers=headers)
    assert approval.status_code == 403, approval.text
    assert "MFA" in approval.json()["detail"]


@pytest.fixture
def authenticated_booking(enforced_client) -> tuple[int, dict[str, str]]:
    user_id = _seed_user("traveler@tenant-a.test", "tenant-a", "traveler", "device-a")
    token = _sign_in(enforced_client, "traveler@tenant-a.test", "device-a")
    return _owned_booking(user_id), {"Authorization": f"Bearer {token}"}


def test_execute_before_confirmation_never_calls_provider(
    enforced_client, authenticated_booking, booking_options_spy
) -> None:
    from tests.db_helpers import get_booking

    booking_id, headers = authenticated_booking
    response = enforced_client.post(f"/api/bookings/{booking_id}/execute", headers=headers)

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "invalid_transition"
    assert booking_options_spy.calls == 0
    booking = run_db(lambda session: get_booking(session, booking_id))
    assert booking.state == BookingState.PENDING_USER_CONFIRMATION
    assert booking.booking_reference is None


def test_concurrent_execution_claims_the_approved_booking_once(
    enforced_client, authenticated_booking, booking_options_spy
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from tests.db_helpers import count_transitions_into

    booking_id, headers = authenticated_booking
    approval = enforced_client.post(f"/api/bookings/{booking_id}/confirm", headers=headers)
    assert approval.status_code == 200, approval.text
    with ThreadPoolExecutor(max_workers=2) as pool:
        requests = [
            pool.submit(enforced_client.post, f"/api/bookings/{booking_id}/execute", headers=headers)
            for _ in range(2)
        ]
        responses = [request.result() for request in requests]

    assert [response.status_code for response in responses] == [200, 200]
    references = {response.json()["booking_reference"] for response in responses}
    assert len(references) == 1 and None not in references
    assert booking_options_spy.calls == 1, "the atomic claim must prevent a second provider call"
    assert run_db(lambda session: count_transitions_into(
        session, booking_id, BookingState.EXECUTED
    )) == 1


def test_expired_booking_never_reaches_the_provider(
    enforced_client, authenticated_booking, booking_options_spy
) -> None:
    from datetime import timedelta
    from app.models import utcnow
    from tests.db_helpers import count_transitions_into, get_booking

    booking_id, headers = authenticated_booking
    approval = enforced_client.post(f"/api/bookings/{booking_id}/confirm", headers=headers)
    assert approval.status_code == 200, approval.text

    async def expire_quote(session) -> None:
        booking = await get_booking(session, booking_id)
        booking.expires_at = utcnow() - timedelta(seconds=1)

    run_db(expire_quote)
    response = enforced_client.post(f"/api/bookings/{booking_id}/execute", headers=headers)

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "booking_expired"
    assert booking_options_spy.calls == 0
    assert run_db(lambda session: get_booking(session, booking_id)).state == BookingState.EXPIRED
    assert run_db(lambda session: count_transitions_into(
        session, booking_id, BookingState.EXPIRED
    )) == 1


def test_pending_mfa_token_cannot_reach_protected_routes(enforced_client) -> None:
    _seed_user("traveler@tenant-a.test", "tenant-a", "traveler", "device-a")
    response = enforced_client.post("/api/auth/login", json={
        "email": "traveler@tenant-a.test", "password": "demo-password", "device_id": "device-a"
    })
    assert response.status_code == 200
    assert response.json()["mfa_required"] is True
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
    assert enforced_client.get("/api/trips", headers=headers).status_code == 403


def test_global_execution_history_applies_trip_read_role_policy(enforced_client) -> None:
    from sqlalchemy import select
    from sqlmodel import col

    _seed_user("operator@tenant-a.test", "tenant-a", "security-operator", "device-a")
    token = _sign_in(enforced_client, "operator@tenant-a.test", "device-a")

    response = enforced_client.get(
        "/api/execution", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 403, response.text
    denial = run_db(lambda session: session.scalar(
        select(SecurityEvent).where(
            col(SecurityEvent.action) == "trip.read",
            col(SecurityEvent.resource) == "execution",
            col(SecurityEvent.decision) == "deny",
        )
    ))
    assert denial is not None


@pytest.mark.parametrize("enforced", [True, False])
def test_signed_slack_approval_requires_development_mode(
    enforced_client, monkeypatch, enforced: bool
) -> None:
    import json
    import time
    from unittest.mock import AsyncMock
    from urllib.parse import urlencode

    from pydantic import SecretStr
    from sqlalchemy import func, select
    from sqlmodel import col

    from app.adapters.slack_hitl import _compute_slack_signature
    from app.config import get_settings
    from app.models import BookingTransition
    from tests.db_helpers import get_booking

    settings = get_settings()
    settings.zero_trust_enforced = enforced
    settings.slack_bot_token = SecretStr("test-bot")
    settings.slack_signing_secret = SecretStr("test-slack-secret")
    settings.slack_approvals_channel_id = "test-channel"
    update_message = AsyncMock(return_value=True)
    monkeypatch.setattr("app.routes.slack.update_approval_message", update_message)
    user_id = _seed_user("owner@tenant-a.test", "tenant-a", "traveler", "device-a", mfa_enabled=False)
    booking_id = _owned_booking(user_id)
    payload = {
        "type": "block_actions",
        "channel": {"id": "test-channel"},
        "user": {"id": "unmapped-slack-user"},
        "actions": [{"action_id": "approve_booking", "value": str(booking_id)}],
    }
    body = urlencode({"payload": json.dumps(payload)}).encode()
    timestamp = str(int(time.time()))
    response = enforced_client.post("/api/slack/interactions", content=body, headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Slack-Request-Timestamp": timestamp,
        "X-Slack-Signature": _compute_slack_signature(body, timestamp, "test-slack-secret"),
    })

    assert response.status_code == (401 if enforced else 200), response.text
    expected_state = BookingState.PENDING_USER_CONFIRMATION if enforced else BookingState.CONFIRMED
    assert run_db(lambda session: get_booking(session, booking_id)).state == expected_state
    assert run_db(lambda session: session.scalar(
        select(func.count(col(BookingTransition.id))).where(col(BookingTransition.booking_log_id) == booking_id)
    )) == (0 if enforced else 1)
    assert update_message.await_count == (0 if enforced else 1)


def test_protected_route_without_a_token_is_401_when_enforced(enforced_client) -> None:
    response = enforced_client.get("/api/trips")

    assert response.status_code == 401, (
        f"an enforced protected route must demand a bearer token, got {response.status_code}: "
        f"{response.text}"
    )
    assert response.json() == {
        "code": "authentication_required",
        "detail": "zero-trust access token required",
    }


def test_segment_spoof_header_is_rejected_as_403_through_the_handler(enforced_client) -> None:
    _seed_user("traveler@tenant-a.test", "tenant-a", "traveler", "device-a")
    token = _sign_in(enforced_client, "traveler@tenant-a.test", "device-a")

    response = enforced_client.get(
        "/api/trips",
        headers={"Authorization": f"Bearer {token}", "x-source-segment": "internal"},
    )

    assert response.status_code == 403, (
        f"a browser-asserted internal segment must be refused as a 403, got "
        f"{response.status_code}: {response.text}"
    )
    assert response.json()["code"] == "forbidden", (
        "SecurityError must surface through the app's ProblemDetail handler, not a bare 500"
    )
