from datetime import datetime

import pytest
from pydantic import SecretStr

from app.adapters import slack_hitl
from app.config import Settings
from app.models import FitnessLevel, FlightSearchResult, HITLBookingLog, TripRequest


@pytest.mark.no_database
@pytest.mark.asyncio
async def test_slack_notification_rejects_an_api_level_failure(monkeypatch) -> None:
    class Response:
        is_error = False
        text = '{"ok":false,"error":"channel_not_found"}'

        def json(self):
            return {"ok": False, "error": "channel_not_found"}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *arguments):
            return None

        async def post(self, *arguments, **keywords):
            return Response()

    monkeypatch.setattr(slack_hitl.httpx, "AsyncClient", lambda **keywords: Client())
    settings = Settings(
        cerebras_api_key=SecretStr("x"),
        searchapi_api_key=SecretStr("x"),
        tavily_api_key=SecretStr("x"),
        database_url="postgresql+asyncpg://localhost/test",
        zero_trust_enforced=False,
        slack_bot_token=SecretStr("xoxb-test"),
        slack_signing_secret=SecretStr("secret"),
        slack_approvals_channel_id="channel",
    )
    trip = TripRequest(
        user_id=1,
        origin="SFO",
        destination="Paris",
        destination_airport="CDG",
        depart_date="2099-01-01",
        age=30,
        fitness_level=FitnessLevel.MODERATE,
    )
    flight = FlightSearchResult(
        trip_request_id=1,
        offer_index=0,
        carrier="Test Air",
        price_usd=100,
        currency="USD",
        depart_at="2099-01-01T09:00:00",
        arrive_at="2099-01-01T17:00:00",
        stops=0,
        booking_token="token",
        raw_offer={"flights": []},
    )
    booking = HITLBookingLog(
        trip_request_id=1,
        flight_search_result_id=1,
        requested_by_user_id=1,
        expires_at=datetime(2099, 1, 1),
    )

    assert await slack_hitl.notify_pending_approval(settings, booking, trip, flight) is False
