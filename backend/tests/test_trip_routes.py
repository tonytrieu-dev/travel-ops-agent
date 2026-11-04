"""Trip route boundaries that are easy to regress without an endpoint-level check."""

from tests.db_helpers import get_trip, run_db, seed_trip


def _trip_payload(**overrides: object) -> dict:
    payload: dict[str, object] = {
        "origin": "JFK",
        "destination": "Paris",
        "destination_airport": "CDG",
        "depart_date": "2030-08-01",
        "age": 30,
        "fitness_level": "moderate",
    }
    payload.update(overrides)
    return payload


def test_list_trips_returns_only_the_authenticated_users_trips_newest_first(client) -> None:
    """A trip seeded under a different user (e.g. another demo session) must not leak into this
    user's sidebar, and the user's own trips must come back newest-created first so the most
    recently started trip lands at the top of the list."""
    other_users_trip_id = run_db(lambda session: seed_trip(session, depart_date="2026-09-01"))

    first_created = client.post("/api/trips", json=_trip_payload(destination_airport="CDG")).json()
    second_created = client.post("/api/trips", json=_trip_payload(destination_airport="LHR")).json()

    async def tie_creation_times(session):
        first = await get_trip(session, first_created["id"])
        second = await get_trip(session, second_created["id"])
        second.created_at = first.created_at
        await session.commit()

    run_db(tie_creation_times)
    response = client.get("/api/trips")

    assert response.status_code == 200, f"expected 200, got {response.status_code}: {response.text}"
    trip_ids = [trip["id"] for trip in response.json()]
    assert other_users_trip_id not in trip_ids, (
        "GET /api/trips must scope to the authenticated user, not return every trip in the DB"
    )
    assert trip_ids[:2] == [second_created["id"], first_created["id"]], (
        f"expected the two most recently created trips newest-first, got {trip_ids}"
    )


def test_patch_nulling_a_required_field_is_422_not_500(client) -> None:
    """Sending an explicit null for a required field used to reach the DB and 500 on the NOT NULL
    insert (or silently corrupt the trip). It must be rejected at the boundary as a 422, and the
    stored value must be left untouched."""
    trip_id = run_db(lambda session: seed_trip(session, depart_date="2026-09-01"))

    response = client.patch(f"/api/trips/{trip_id}", json={"origin": None})

    assert response.status_code == 422, f"expected 422, got {response.status_code}: {response.text}"
    assert response.json()["code"] == "validation_error"
    trip = run_db(lambda session: get_trip(session, trip_id))
    assert trip.origin == "JFK", (
        f"a rejected PATCH must not persist the null origin, found {trip.origin!r}"
    )


def test_flight_search_finalizes_live_cached_and_failed_runs(client, flight_search_spy, monkeypatch):
    trip = client.post("/api/trips", json=_trip_payload()).json()
    search_url = f"/api/trips/{trip['id']}/flights/search"
    for _ in range(2):
        assert client.post(search_url).status_code == 200
    assert flight_search_spy.calls == 1

    runs = client.get(f"/api/trips/{trip['id']}/execution").json()["agent_runs"]
    assert len(runs) == 2
    for run in runs:
        assert run["status"] == "completed" and run["finished_at"] is not None
        assert [step["name"] for step in run["steps"]] == ["search_flights"]
        assert [event["name"] for event in run["events"]] == ["search_flights"]

    async def fail_search(*arguments, **keywords):
        raise RuntimeError("provider unavailable before recording a result")

    monkeypatch.setattr(flight_search_spy, "search_offers", fail_search)
    other_trip = client.post("/api/trips", json=_trip_payload(destination_airport="SFO")).json()
    assert client.post(f"/api/trips/{other_trip['id']}/flights/search").status_code == 500
    failed_runs = client.get(f"/api/trips/{other_trip['id']}/execution").json()["agent_runs"]
    assert len(failed_runs) == 1
    assert failed_runs[0]["status"] == "failed" and failed_runs[0]["finished_at"] is not None
    assert failed_runs[0]["steps"] == []
