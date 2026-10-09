"""HTTP-level wiring of the zero-trust controls, through the real FastAPI dependency graph.

test_security.py proves the *decisions*; this file proves the *wiring* those decisions hang on,
which no direct-call unit test can see:

- the protected routers actually depend on get_security_context + enforce_api_segment,
- get_security_context returns 401 when enforcement is on and no bearer token is presented,
- SecurityError is mapped to a 403 ProblemDetail by the app's exception handler,
- a full login -> MFA -> bearer-token -> protected-request round trip succeeds.

A route that silently dropped its security dependency would pass every unit test and fail here,
which is the whole reason these three exist. More than three would just re-litigate decision
logic through a slower HTTP seam.
"""

import pytest

from app import config
from app.config import Settings
from app.dependencies import _totp_code
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
    config.get_settings.cache_clear()


def _seed_user(email: str, tenant_id: str, role: str, device_id: str) -> int:
    from app.dependencies import _password_hash
    from app.models import User

    async def _work(session) -> int:
        user = User(
            email=email,
            tenant_id=tenant_id,
            role=role,
            device_id=device_id,
            password_hash=_password_hash("demo-password"),
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
