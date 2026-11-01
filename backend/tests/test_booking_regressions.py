"""Booking state and quote-freshness regressions at authenticated HTTP boundaries."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event

import pytest

from app.models import FlightSearchResult, TripRequest, utcnow
from tests.db_helpers import (
    get_booking,
    get_flight_search_results,
    run_db,
    seed_flight_search_results,
    seed_trip,
)
from tests.test_security_integration import (
    _seed_user,
    _sign_in,
    authenticated_booking,
    enforced_client,
)


@pytest.mark.parametrize("action", ["confirm", "execute"])
def test_cancellation_committed_after_admission_prevents_transition(
    enforced_client, authenticated_booking, booking_options_spy, monkeypatch, action
) -> None:
    from app import security_admission

    booking_id, headers = authenticated_booking
    if action == "execute":
        approval = enforced_client.post(f"/api/bookings/{booking_id}/confirm", headers=headers)
        assert approval.status_code == 200, approval.text

    admitted = Event()
    release = Event()
    original_admission = security_admission.admit_booking
    delayed_action = "booking.approve" if action == "confirm" else "booking.execute"

    async def pause_after_admission(*arguments, **keywords):
        # Keep the admitted instance alive, as FastAPI's dependency result cache does.
        booking = await original_admission(*arguments, **keywords)
        if keywords["action"] == delayed_action:
            admitted.set()
            released = await asyncio.to_thread(release.wait, 10)
            assert released, "competing cancellation never released the admitted request"
        return booking

    monkeypatch.setattr(security_admission, "admit_booking", pause_after_admission)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(
            enforced_client.post, f"/api/bookings/{booking_id}/{action}", headers=headers
        )
        try:
            assert admitted.wait(10), "request never reached authenticated booking admission"
            cancellation = enforced_client.post(
                f"/api/bookings/{booking_id}/cancel", headers=headers
            )
            assert cancellation.status_code == 200, cancellation.text
        finally:
            release.set()
        response = pending.result(timeout=15)

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "invalid_transition"
    history = enforced_client.get(f"/api/bookings/{booking_id}", headers=headers)
    assert history.status_code == 200, history.text
    assert history.json()["state"] == "CANCELLED"
    expected_states = ["CANCELLED"] if action == "confirm" else ["CONFIRMED", "CANCELLED"]
    assert [row["to_state"] for row in history.json()["transitions"]] == expected_states
    assert booking_options_spy.calls == 0


def test_confirmation_records_the_authenticated_owner_transition(
    enforced_client, authenticated_booking
) -> None:
    booking_id, headers = authenticated_booking
    response = enforced_client.post(f"/api/bookings/{booking_id}/confirm", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "CONFIRMED"

    history = enforced_client.get(f"/api/bookings/{booking_id}", headers=headers)
    assert history.status_code == 200, history.text
    [transition] = history.json()["transitions"]
    assert transition["from_state"] == "PENDING_USER_CONFIRMATION"
    assert transition["to_state"] == "CONFIRMED"
    assert transition["reason"] == "confirm"
    assert transition["actor_email"] == "traveler@tenant-a.test"
    booking = run_db(lambda session: get_booking(session, booking_id))
    assert transition["actor_user_id"] == booking.requested_by_user_id


@pytest.mark.parametrize("quote_age_minutes", [14, 16])
def test_booking_admission_uses_original_quote_age(
    enforced_client, monkeypatch, quote_age_minutes
) -> None:
    user_id = _seed_user("fare@tenant-a.test", "tenant-a", "traveler", "device-a")
    token = _sign_in(enforced_client, "fare@tenant-a.test", "device-a")
    headers = {"Authorization": f"Bearer {token}"}
    now = datetime(2026, 10, 29, 12, 0)
    quoted_at = now - timedelta(minutes=quote_age_minutes)

    async def seed_offer(session):
        trip_id = await seed_trip(session)
        trip = await session.get(TripRequest, trip_id)
        assert trip is not None
        trip.user_id = user_id
        [offer_id] = await seed_flight_search_results(session, trip_id)
        offer = await session.get(FlightSearchResult, offer_id)
        assert offer is not None
        offer.created_at = quoted_at
        return trip_id, offer_id

    trip_id, offer_id = run_db(seed_offer)
    monkeypatch.setattr("app.repositories.booking_repository.utcnow", lambda: now)
    response = enforced_client.post(
        f"/api/trips/{trip_id}/booking/request",
        json={"flight_search_result_id": offer_id},
        headers=headers,
    )
    if quote_age_minutes == 16:
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "booking_expired"
        history = enforced_client.get("/api/bookings", headers=headers)
        assert history.status_code == 200, history.text
        assert history.json() == [], "rejected stale quotes must not create active bookings"
        return

    assert response.status_code == 200, response.text
    assert response.json()["state"] == "PENDING_USER_CONFIRMATION"
    assert datetime.fromisoformat(response.json()["expires_at"]) == datetime(2026, 10, 29, 12, 16, tzinfo=UTC)


def test_cross_trip_cache_preserves_original_quote_timestamp(flight_search_spy) -> None:
    from app.agent.execution_log import execution_context
    from app.services.flight_search import FlightSearchService

    quoted_at = utcnow() - timedelta(minutes=14)

    async def search_cached_offer(session):
        source_trip_id = await seed_trip(session)
        [source_offer_id] = await seed_flight_search_results(session, source_trip_id)
        source_offer = await session.get(FlightSearchResult, source_offer_id)
        assert source_offer is not None
        source_offer.created_at = quoted_at
        target_trip_id = await seed_trip(session)
        await session.commit()

        async with execution_context(session, target_trip_id):
            outcome = await FlightSearchService(session, flight_search_spy).search(target_trip_id)
        assert outcome.source == "cross_trip_cache"
        [copied_offer] = await get_flight_search_results(session, target_trip_id)
        assert copied_offer.id != source_offer_id
        assert copied_offer.created_at == quoted_at
        assert copied_offer.booking_token == source_offer.booking_token

    run_db(search_cached_offer)
    assert flight_search_spy.calls == 0
