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

import pytest

from app.config import Settings
from app.dependencies import _totp_code
from app.models import HITLBookingLog, SecurityIncident, TripRequest
from app.state import BookingState
from tests.db_helpers import TEST_DATABASE_URL, run_db

_SIGNING_SECRET = "integration-test-signing-secret"


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
    for module in ("app.config", "app.security", "app.dependencies", "app.routes.auth"):
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
        assert response.status_code == 403, response.text
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
