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

import os
from datetime import datetime

import pytest

from app.config import Settings
from app.dependencies import _totp_code
from app.models import HITLBookingLog, SecurityEvent, SecurityIncident, TripRequest
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
    for module in (
        "app.config", "app.security", "app.dependencies", "app.routes.auth",
        "app.routes.slack", "app.routes.connectors", "app.routes.booking",
    ):
        monkeypatch.setattr(f"{module}.get_settings", lambda: settings)
    yield client


def _seed_user(
    email: str, tenant_id: str, role: str, device_id: str, *, mfa_enabled: bool = True
) -> int:
    from app.dependencies import _password_hash
    from app.models import User

    async def _work(session) -> int:
        user = User(
            email=email,
            tenant_id=tenant_id,
            role=role,
            device_id=device_id,
            password_hash=_password_hash("demo-password"),
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
        json={"session_id": body["session_id"], "code": _totp_code("JBSWY3DPEHPK3PXP")},
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


def test_known_account_login_failure_is_visible_to_its_tenant(enforced_client) -> None:
    from sqlalchemy import select
    from sqlmodel import col

    user_id = _seed_user(
        "traveler@tenant-a.test", "tenant-a", "traveler", "device-a"
    )
    response = enforced_client.post("/api/auth/login", json={
        "email": "traveler@tenant-a.test",
        "password": "wrong-password",
        "device_id": "device-a",
    })

    assert response.status_code == 401
    assert response.json()["code"] == "authentication_required"
    event = run_db(lambda session: session.scalar(
        select(SecurityEvent).where(col(SecurityEvent.action) == "auth.authenticate")
    ))
    assert event is not None
    assert (event.tenant_id, event.actor_user_id, event.device_id) == (
        "tenant-a",
        user_id,
        "device-a",
    )


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


def test_booking_options_failure_preserves_approved_execution(
    enforced_client, authenticated_booking, booking_options_spy
) -> None:
    from tests.db_helpers import get_booking

    booking_id, headers = authenticated_booking
    approval = enforced_client.post(f"/api/bookings/{booking_id}/confirm", headers=headers)
    assert approval.status_code == 200, approval.text
    booking_options_spy.should_fail = True

    response = enforced_client.post(f"/api/bookings/{booking_id}/execute", headers=headers)

    assert response.status_code == 200, response.text
    assert response.json()["booking_options"] == []
    assert booking_options_spy.calls == 1, "the test must exercise the provider failure"
    booking = run_db(lambda session: get_booking(session, booking_id))
    assert booking.state == BookingState.EXECUTED
    assert booking.booking_reference == response.json()["booking_reference"]
    assert booking.booking_reference


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


def test_cross_tenant_denials_revoke_only_the_attacking_session(enforced_client) -> None:
    from sqlalchemy import select

    owner_id = _seed_user("owner@tenant-a.test", "tenant-a", "traveler", "device-a")
    booking_id = _owned_booking(owner_id)
    booking = run_db(lambda session: session.get(HITLBookingLog, booking_id))
    assert booking is not None
    _seed_user("intruder@tenant-b.test", "tenant-b", "traveler", "device-b")
    owner_token = _sign_in(enforced_client, "owner@tenant-a.test", "device-a")
    intruder_token = _sign_in(enforced_client, "intruder@tenant-b.test", "device-b")
    headers = {"Authorization": f"Bearer {intruder_token}"}
    for _ in range(5):
        response = enforced_client.get(f"/api/trips/{booking.trip_request_id}", headers=headers)
        assert response.status_code == 404, response.text
        assert response.json()["code"] == "trip_not_found"
    assert enforced_client.get("/api/trips", headers=headers).status_code == 401
    assert enforced_client.get(
        f"/api/trips/{booking.trip_request_id}", headers={"Authorization": f"Bearer {owner_token}"}
    ).status_code == 200
    incidents = list(run_db(lambda session: session.scalars(select(SecurityIncident))))
    assert len(incidents) == 1
    assert incidents[0].device_id == "device-b"


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


def test_security_event_since_filter_converts_offsets_to_utc(enforced_client) -> None:
    operator_id = _seed_user(
        "operator@tenant-a.test", "tenant-a", "security-operator", "device-a"
    )

    async def _seed_events(session) -> None:
        for resource, created_at in (
            ("before", datetime(2026, 1, 1, 7, 30)),
            ("after", datetime(2026, 1, 1, 8, 30)),
        ):
            session.add(SecurityEvent(
                tenant_id="tenant-a",
                actor_user_id=operator_id,
                device_id="device-a",
                source_segment="web",
                target_segment="api",
                action="time-boundary",
                resource=resource,
                decision="allow",
                reason="test event",
                correlation_id=f"time-boundary-{resource}",
                created_at=created_at,
            ))

    run_db(_seed_events)
    token = _sign_in(enforced_client, "operator@tenant-a.test", "device-a")
    response = enforced_client.get(
        "/api/security/events",
        params={"action": "time-boundary", "since": "2026-01-01T00:00:00-08:00"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.text
    assert [event["resource"] for event in response.json()] == ["after"]


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
    from app.dependencies import get_settings
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


def test_non_ascii_mfa_digits_are_rejected_before_verification(enforced_client, monkeypatch) -> None:
    from unittest.mock import AsyncMock

    from app.dependencies import verify_session_mfa

    _seed_user("traveler@tenant-a.test", "tenant-a", "traveler", "device-a")
    login = enforced_client.post("/api/auth/login", json={
        "email": "traveler@tenant-a.test", "password": "demo-password", "device_id": "device-a",
    })
    assert login.status_code == 200, login.text
    verify = AsyncMock(wraps=verify_session_mfa)
    monkeypatch.setattr("app.routes.auth.verify_session_mfa", verify)
    response = enforced_client.post("/api/auth/mfa", json={
        "session_id": login.json()["session_id"], "code": "１２３４５６",
    })

    assert response.status_code == 422, response.text
    assert response.json()["code"] == "validation_error"
    verify.assert_not_awaited()


@pytest.mark.skipif(os.getenv("RUN_SECURITY_LOAD") != "1", reason="explicit latency benchmark")
def test_modest_load_reports_authentication_overhead(enforced_client, monkeypatch) -> None:
    import json

    from app.dependencies import get_settings
    from scripts.security_load import measure

    _seed_user("traveler@tenant-a.test", "tenant-a", "traveler", "device-a")
    token = _sign_in(enforced_client, "traveler@tenant-a.test", "device-a")
    settings = get_settings()
    monkeypatch.setattr("app.dependencies.DEMO_USER_EMAIL", "traveler@tenant-a.test")
    settings.zero_trust_enforced = False
    baseline = measure(enforced_client, {}, requests=100, concurrency=4)
    settings.zero_trust_enforced = True
    enforced = measure(
        enforced_client, {"Authorization": f"Bearer {token}"}, requests=100, concurrency=4
    )
    print(json.dumps({
        "development_baseline": baseline,
        "enforced": enforced,
        "p50_overhead_ms": round(enforced["p50_ms"] - baseline["p50_ms"], 3),
        "p95_overhead_ms": round(enforced["p95_ms"] - baseline["p95_ms"], 3),
    }, sort_keys=True))


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


def test_full_login_mfa_token_round_trip_reaches_an_owned_resource(enforced_client) -> None:
    run_db_user_id = _seed_user("traveler@tenant-a.test", "tenant-a", "traveler", "device-a")
    assert run_db_user_id is not None

    token = _sign_in(enforced_client, "traveler@tenant-a.test", "device-a")
    response = enforced_client.get("/api/trips", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200, (
        f"a fully authenticated, MFA-verified request must reach its own tenant's trips, got "
        f"{response.status_code}: {response.text}"
    )


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
