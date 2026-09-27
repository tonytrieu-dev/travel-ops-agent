"""Guards request rate limits through their HTTP boundaries."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.config import RATE_LIMIT_MAX_REQUESTS, RATE_LIMIT_WINDOW_SECONDS
from app.db import get_session
from app.main import app
from app.rate_limit import _enforce_rate_limit, _request_timestamps
from tests.db_helpers import run_db, seed_trip


@pytest.fixture(autouse=True)
def _clear_rate_limits():
    _request_timestamps.clear()
    yield
    _request_timestamps.clear()


def test_flights_search_is_rate_limited_after_max_requests_from_one_client(client) -> None:
    trip_id = run_db(lambda session: seed_trip(session))
    url = f"/api/trips/{trip_id}/flights/search"

    responses = [client.post(url) for _ in range(RATE_LIMIT_MAX_REQUESTS + 1)]

    statuses = [response.status_code for response in responses]
    assert statuses[:RATE_LIMIT_MAX_REQUESTS] == [200] * RATE_LIMIT_MAX_REQUESTS, (
        f"the first {RATE_LIMIT_MAX_REQUESTS} requests must succeed (well under the cap), "
        f"got {statuses[:RATE_LIMIT_MAX_REQUESTS]}"
    )
    last_response = responses[-1]
    assert last_response.status_code == 429, (
        f"request {RATE_LIMIT_MAX_REQUESTS + 1} exceeds the per-IP cap and must be rejected, "
        f"got {last_response.status_code}: {last_response.text}"
    )
    assert last_response.json()["code"] == "rate_limit_exceeded"
    assert "Retry-After" in last_response.headers, (
        "a 429 must tell the client when it's safe to retry"
    )


@pytest.mark.no_database
async def test_repeated_login_failures_are_rate_limited_by_client_and_normalized_email(
    monkeypatch,
) -> None:
    session = MagicMock()
    session.scalar = AsyncMock(return_value=None)

    async def _session_override():
        yield session

    app.dependency_overrides[get_session] = _session_override
    monkeypatch.setattr("app.routes.auth.record_authentication_failure", AsyncMock())
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            responses = [
                await client.post(
                    "/api/auth/login",
                    json={
                        "email": "TARGET@EXAMPLE.TEST" if index % 2 else "target@example.test",
                        "password": "wrong",
                        "device_id": "guessed-device",
                    },
                )
                for index in range(RATE_LIMIT_MAX_REQUESTS + 1)
            ]
    finally:
        app.dependency_overrides.clear()

    assert [response.status_code for response in responses[:-1]] == [
        401
    ] * RATE_LIMIT_MAX_REQUESTS
    assert responses[-1].status_code == 429
    assert responses[-1].json()["code"] == "rate_limit_exceeded"
    assert "Retry-After" in responses[-1].headers


@pytest.mark.no_database
async def test_rotating_login_emails_still_reaches_client_limit(monkeypatch) -> None:
    session = MagicMock()
    session.scalar = AsyncMock(return_value=None)

    async def _session_override():
        yield session

    app.dependency_overrides[get_session] = _session_override
    monkeypatch.setattr("app.routes.auth.record_authentication_failure", AsyncMock())
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            responses = [
                await client.post(
                    "/api/auth/login",
                    json={
                        "email": f"rotated-{index}@example.test",
                        "password": "wrong",
                        "device_id": "guessed-device",
                    },
                )
                for index in range(RATE_LIMIT_MAX_REQUESTS + 1)
            ]
    finally:
        app.dependency_overrides.clear()

    assert [response.status_code for response in responses[:-1]] == [
        401
    ] * RATE_LIMIT_MAX_REQUESTS
    assert responses[-1].status_code == 429


@pytest.mark.no_database
async def test_login_rejects_oversized_email_before_database_access(monkeypatch) -> None:
    session = MagicMock()
    session.scalar = AsyncMock(return_value=None)

    async def _session_override():
        yield session

    app.dependency_overrides[get_session] = _session_override
    monkeypatch.setattr("app.routes.auth.record_authentication_failure", AsyncMock())
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/auth/login",
                json={"email": "x" * 255, "password": "wrong", "device_id": "device"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422


@pytest.mark.no_database
def test_rate_limit_store_purges_expired_keys(monkeypatch) -> None:
    now = 1_000.0
    _request_timestamps["expired"] = [now - RATE_LIMIT_WINDOW_SECONDS - 1]
    monkeypatch.setattr("app.rate_limit.time.monotonic", lambda: now)

    _enforce_rate_limit("active")

    assert "expired" not in _request_timestamps
