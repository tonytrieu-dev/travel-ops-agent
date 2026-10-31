import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic, sleep
from uuid import uuid4

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

    monkeypatch.setattr('app.dbos_runtime._run_planner_agent', slow_planner)
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

    monkeypatch.setattr('app.dbos_runtime._run_planner_agent', slow_planner)
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


def test_durable_workflow_persists_without_the_http_caller(client, planner_spy) -> None:
    from dbos import SetWorkflowID

    from app.dbos_runtime import _run_planner_workflow
    from app.repositories.trips_repository import _build_planner_prompt, get_trip

    trip_id = create_trip(client)
    trip = run_db(lambda session: get_trip(session, trip_id))
    prompt = _build_planner_prompt(trip)
    workflow_id = str(uuid4())
    async def run_workflow():
        with SetWorkflowID(workflow_id):
            await _run_planner_workflow(trip_id, prompt, "persistence-regression")

    assert client.portal is not None
    client.portal.call(run_workflow)
    assert client.get(f'/api/trips/{trip_id}/snapshot').json()['plan']['status'] == 'ready'
    client.portal.call(run_workflow)
    assert planner_spy.calls == 1


def test_trip_edit_waits_for_final_itinerary_write(client) -> None:
    from sqlalchemy import event
    from app.db import get_engine

    trip_id = create_trip(client)
    checked = Event()
    release = Event()
    engine = get_engine().sync_engine

    def pause_before_insert(connection, cursor, statement, parameters, context, executemany):
        if not statement.startswith("INSERT INTO itinerary"):
            return
        checked.set()
        assert release.wait(10), "itinerary write was never released"

    event.listen(engine, "before_cursor_execute", pause_before_insert)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            planning = pool.submit(client.post, f'/api/trips/{trip_id}/plan')
            assert checked.wait(10), "planner never reached its final write"
            # A separate connection can try the edit while the app loop is paused.
            async def edit(session):
                from app.repositories.trips_repository import update_trip
                from app.schemas import TripRequestUpdate
                return await update_trip(session, trip_id, TripRequestUpdate(fitness_level="low"))

            editing = pool.submit(run_db, edit)
            try:
                deadline = monotonic() + 5
                while not run_db(lambda session: session.scalar(text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                    "AND wait_event_type = 'Lock' AND query LIKE '%trip_request%'"
                ))):
                    assert not editing.done(), "trip edit bypassed the final itinerary write"
                    assert monotonic() < deadline, "trip edit never waited for the trip lock"
                    sleep(0.01)
            finally:
                release.set()
            assert planning.result(timeout=15).status_code in (200, 409)
            editing.result(timeout=15)
    finally:
        release.set()
        event.remove(engine, "before_cursor_execute", pause_before_insert)
    snapshot = client.get(f'/api/trips/{trip_id}/snapshot').json()
    assert snapshot['trip']['fitness_level'] == 'low'
    assert snapshot['plan'] is None


def test_flight_search_edit_race(client, flight_search_spy, monkeypatch):
    started, release = Event(), Event()
    original = flight_search_spy.search_offers
    async def paused(*arguments):
        started.set()
        assert await asyncio.to_thread(release.wait, 10)
        return await original(*arguments)
    monkeypatch.setattr(flight_search_spy, 'search_offers', paused)
    trip = client.post('/api/trips', json={'origin':'JFK','destination':'Paris','destination_airport':'CDG','depart_date':'2030-08-01','age':30,'fitness_level':'high'}).json()
    with ThreadPoolExecutor(max_workers=1) as pool:
        search = pool.submit(client.post, f"/api/trips/{trip['id']}/flights/search")
        try:
            assert started.wait(10)
            assert client.patch(f"/api/trips/{trip['id']}", json={'destination':'London','destination_airport':'LHR'}).status_code == 200
        finally:
            release.set()
        assert search.result(timeout=15).status_code == 409
    snapshot = client.get(f"/api/trips/{trip['id']}/snapshot").json()
    assert snapshot['trip']['destination_airport'] == 'LHR'
    assert snapshot['flight_search'] is None, 'Old CDG search was persisted under updated LHR trip'
