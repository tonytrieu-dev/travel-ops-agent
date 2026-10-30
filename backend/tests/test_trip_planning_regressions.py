import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic, sleep

from sqlalchemy import select, text

import pytest

from app.models import FlightSearchResult, Itinerary, TripRequest
from tests.db_helpers import run_db, seed_flight_search_results, seed_itinerary


def create_trip(client):
    response = client.post('/api/trips', json={
        'origin': 'JFK', 'destination': 'Paris', 'destination_airport': 'CDG',
        'depart_date': '2030-08-01', 'age': 30, 'fitness_level': 'high',
    })
    assert response.status_code == 200, response.text
    return response.json()['id']


@pytest.mark.parametrize('changes,invalidated', [
    ({'fitness_level': 'low'}, True), ({'age': 78}, True), ({'age': 30}, False),
])
def test_traveler_changes_invalidate_only_itinerary(client, planner_spy, changes, invalidated):
    trip_id = create_trip(client)

    async def seed(session):
        await seed_itinerary(session, trip_id)
        return (await seed_flight_search_results(session, trip_id))[0]

    flight_id = run_db(seed)
    response = client.patch(f'/api/trips/{trip_id}', json=changes)
    assert response.status_code == 200, response.text

    async def read(session):
        itinerary = await session.scalar(select(Itinerary).where(Itinerary.trip_request_id == trip_id))
        flight = await session.get(FlightSearchResult, flight_id)
        return itinerary, flight

    itinerary, flight = run_db(read)
    assert (itinerary is None) == invalidated
    assert flight is not None
    planned = client.post(f'/api/trips/{trip_id}/plan')
    assert planned.status_code == 200, planned.text
    assert planner_spy.calls == int(invalidated)


@pytest.mark.parametrize('booked', [True, False])
def test_route_edit_preserves_booking_history_or_invalidates_unbooked_offers(client, booked):
    trip_id = create_trip(client)
    flight_id = run_db(lambda session: seed_flight_search_results(session, trip_id))[0]
    booking_id = None
    if booked:
        booking = client.post(f'/api/trips/{trip_id}/booking/request', json={'flight_search_result_id': flight_id})
        assert booking.status_code == 200, booking.text
        booking_id = booking.json()['id']
    response = client.patch(f'/api/trips/{trip_id}', json={'destination': 'London', 'destination_airport': 'LHR'})
    assert response.status_code == (409 if booked else 200), response.text
    snapshot = client.get(f'/api/trips/{trip_id}/snapshot').json()
    assert snapshot['trip']['destination'] == ('Paris' if booked else 'London')
    assert bool(snapshot['flight_search']) == booked
    if booked:
        retained = client.get(f"/api/bookings/{booking_id}")
        assert retained.status_code == 200, retained.text
        assert retained.json()['flight_search_result_id'] == flight_id


def test_concurrent_http_planning_invokes_planner_once(client, planner_spy, monkeypatch):
    trip_id = create_trip(client)
    started = Event()
    release = Event()

    async def slow_planner(*arguments):
        planner_spy.calls += 1
        started.set()
        assert await asyncio.to_thread(release.wait, 10), 'planner was never released'
        return planner_spy.output

    monkeypatch.setattr('app.dbos_runtime._run_planner_workflow', slow_planner)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(client.post, f'/api/trips/{trip_id}/plan')
        try:
            assert started.wait(10), 'first planner never started'
            second = pool.submit(client.post, f'/api/trips/{trip_id}/plan')
            deadline = monotonic() + 5
            while planner_spy.calls < 2:
                waiting = run_db(lambda session: session.scalar(text(
                    "SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND NOT granted"
                )))
                if waiting:
                    break
                assert monotonic() < deadline, 'second request neither planned nor waited for its trip lock'
                sleep(0.01)
        finally:
            release.set()
        responses = [first.result(timeout=15), second.result(timeout=15)]
    assert [response.status_code for response in responses] == [200, 200], [r.text for r in responses]
    assert planner_spy.calls == 1
    assert client.get(f'/api/trips/{trip_id}/snapshot').json()['plan']['status'] == 'ready'


def test_edit_during_planning_discards_obsolete_itinerary(client, planner_spy, monkeypatch):
    trip_id = create_trip(client)
    started = Event()
    release = Event()

    async def slow_planner(*arguments):
        started.set()
        assert await asyncio.to_thread(release.wait, 10), 'planner was never released'
        return planner_spy.output

    monkeypatch.setattr('app.dbos_runtime._run_planner_workflow', slow_planner)
    with ThreadPoolExecutor(max_workers=1) as pool:
        planning = pool.submit(client.post, f'/api/trips/{trip_id}/plan')
        try:
            assert started.wait(10), 'planner never started'
            edited = client.patch(f'/api/trips/{trip_id}', json={'destination': 'London'})
            assert edited.status_code == 200, edited.text
        finally:
            release.set()
        response = planning.result(timeout=15)
    assert response.status_code == 409, response.text
    snapshot = client.get(f'/api/trips/{trip_id}/snapshot').json()
    assert snapshot['trip']['destination'] == 'London'
    assert snapshot['plan'] is None


def test_durable_planning_persists_before_http_caller_continues(client, monkeypatch):
    from app.db import get_session_factory
    from app.dbos_runtime import run_planner_durable

    trip_id = create_trip(client)

    async def check_persisted(*arguments, **keywords):
        output = await run_planner_durable(*arguments, **keywords)
        async with get_session_factory()() as session:
            itinerary = await session.scalar(select(Itinerary).where(Itinerary.trip_request_id == trip_id))
            assert itinerary is not None, 'durable work returned without persisting its itinerary'
        return output

    monkeypatch.setattr('app.routes.trips.run_planner_durable', check_persisted)
    response = client.post(f'/api/trips/{trip_id}/plan')
    assert response.status_code == 200, response.text
