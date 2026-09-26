"""Executable acceptance criteria for trip planning (features/trip_planning.feature).

Each scenario drives the real /api/trips* endpoints through the sync TestClient and asserts the
system's observable reaction plus the persisted state — never a value configured on a mock. The
flight-search and planner providers are counting spies so "reused the cache" / "never called
twice" are real assertions about calls made, not about a fabricated return value.
"""

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from app.adapters.flights_searchapi import NormalizedFlightOffer
from app.schemas import ClarificationOut, ItineraryDayOut, ItineraryOut
from tests.conftest import FlightSearchSpy, PlannerRunSpy
from tests.db_helpers import (
    get_agent_runs,
    get_execution_events,
    get_flight_search_results,
    get_itinerary,
    get_trip,
    run_db,
    seed_flight_search_results,
    seed_itinerary,
    seed_trip,
)

scenarios("../../features/trip_planning.feature")


@pytest.fixture
def bag() -> dict:
    return {}


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


@given("a trip request with a depart date next month and no return date")
def _valid_trip_request(bag: dict) -> None:
    bag["payload"] = _trip_payload(depart_date="2030-09-01")


@given("a trip request whose return date is before its depart date")
def _return_before_depart(bag: dict) -> None:
    bag["payload"] = _trip_payload(depart_date="2030-09-10", return_date="2030-09-01")


@given("a trip request missing age and fitness level")
def _missing_age_and_fitness(bag: dict) -> None:
    payload = _trip_payload()
    del payload["age"]
    del payload["fitness_level"]
    bag["payload"] = payload


@when("the trip is created")
def _create_trip(client, bag: dict) -> None:
    bag["response"] = client.post("/api/trips", json=bag["payload"])


@then(parsers.parse('the response is {status:d} with status "{status_value}"'))
def _response_with_status(bag: dict, status: int, status_value: str) -> None:
    response = bag["response"]
    assert response.status_code == status, (
        f"expected {status}, got {response.status_code}: {response.text}"
    )
    assert response.json()["status"] == status_value


@then(parsers.parse('the response is {status:d} with error code "{code}"'))
def _response_with_error_code(bag: dict, status: int, code: str) -> None:
    response = bag["response"]
    assert response.status_code == status, (
        f"expected {status}, got {response.status_code}: {response.text}"
    )
    assert response.json()["code"] == code


@given("an existing trip", target_fixture="trip_id")
def _existing_trip() -> int:
    return run_db(lambda session: seed_trip(session, depart_date="2030-09-01"))


@given("an existing trip with no prior flight search", target_fixture="trip_id")
def _existing_trip_no_search() -> int:
    return run_db(lambda session: seed_trip(session, depart_date="2030-09-01"))


@given(
    "an existing round-trip trip with an outbound-only cached flight search",
    target_fixture="trip_id",
)
def _round_trip_with_outbound_only_cache() -> int:
    async def _work(session):
        trip_id = await seed_trip(
            session, depart_date="2030-09-01", return_date="2030-09-08"
        )
        await seed_flight_search_results(session, trip_id, minutes_ago=1)
        return trip_id

    return run_db(_work)


@given(
    "an existing trip that already has a generated itinerary", target_fixture="trip_id"
)
def _existing_trip_with_itinerary() -> int:
    async def _work(session):
        trip_id = await seed_trip(session, depart_date="2030-09-01")
        await seed_itinerary(session, trip_id)
        return trip_id

    return run_db(_work)


@given(
    "an existing trip with stale saved flights and a generated itinerary",
    target_fixture="trip_id",
)
def _existing_trip_with_stale_snapshot(bag: dict) -> int:
    async def _work(session):
        trip_id = await seed_trip(session, depart_date="2030-09-01")
        await seed_flight_search_results(session, trip_id, minutes_ago=45)
        latest_flight_ids = await seed_flight_search_results(session, trip_id, minutes_ago=30)
        await seed_itinerary(session, trip_id)
        return trip_id, latest_flight_ids

    trip_id, latest_flight_ids = run_db(_work)
    bag["latest_flight_ids"] = latest_flight_ids
    return trip_id


@given("flights have already been searched live for that route and those dates")
def _prior_live_search() -> None:
    async def _work(session):
        other_trip_id = await seed_trip(session, depart_date="2030-09-01")
        await seed_flight_search_results(session, other_trip_id, minutes_ago=5)

    run_db(_work)


@when("flights are searched for the trip")
def _search_flights(client, trip_id: int, bag: dict) -> None:
    bag["response"] = client.post(f"/api/trips/{trip_id}/flights/search")


@when("the trip snapshot is fetched")
def _get_trip_snapshot(client, trip_id: int, bag: dict) -> None:
    bag["response"] = client.get(f"/api/trips/{trip_id}/snapshot")


@then(parsers.parse('the response is 200 with offers sourced "{source}"'))
def _offers_sourced(bag: dict, source: str) -> None:
    response = bag["response"]
    assert response.status_code == 200, f"expected 200, got {response.status_code}: {response.text}"
    offers = response.json()["offers"]
    assert offers
    assert all(offer["source"] == source for offer in offers)


@then("the flight provider is called exactly once")
def _flight_provider_called_once(flight_search_spy: FlightSearchSpy) -> None:
    assert flight_search_spy.calls == 1, (
        f"a cache-miss search must call the live provider exactly once, got "
        f"{flight_search_spy.calls}"
    )


@then(parsers.parse('a "{name}" execution event is recorded for the trip'))
def _execution_event_recorded(trip_id: int, name: str) -> None:
    events = run_db(lambda session: get_execution_events(session, trip_id))
    assert any(event.name == name for event in events), (
        f"expected an execution event named {name!r} for trip {trip_id} — the button-triggered "
        f"search must bind execution_context and record_event just like the agent's own tool "
        f"call does, or it never shows up in the execution panel; got events "
        f"{[event.name for event in events]}"
    )


@then("a flight-search agent run with metrics is recorded for the trip")
def _flight_search_agent_run_recorded(client, trip_id: int) -> None:
    runs = run_db(lambda session: get_agent_runs(session, trip_id))
    assert len(runs) == 1
    run = runs[0]
    assert run.model == "FlightSearchSpy"
    assert run.status == "completed"
    assert run.total_input_tokens == 0
    assert run.total_output_tokens == 0
    assert run.total_ms >= 0
    assert run.finished_at is not None

    response = client.get(f"/api/trips/{trip_id}/execution")
    assert response.status_code == 200
    panel_run = response.json()["agent_runs"][0]
    assert panel_run["estimated_cost_usd"] == 0
    assert panel_run["budget_utilization_pct"] == 0
    assert len(panel_run["steps"]) == 1
    step = panel_run["steps"][0]
    assert step["kind"] == "tool"
    assert step["name"] == "search_flights"
    assert step["tokens"] == 0


@then("the flight-search agent run owns its API event")
def _flight_search_agent_run_owns_event(client, trip_id: int) -> None:
    response = client.get(f"/api/trips/{trip_id}/execution")
    assert response.status_code == 200
    events = response.json()["agent_runs"][0]["events"]
    assert len(events) == 1
    assert events[0]["kind"] == "api_call"
    assert events[0]["name"] == "search_flights"


@then("the flight provider is never called")
def _flight_provider_never_called(flight_search_spy: FlightSearchSpy) -> None:
    assert flight_search_spy.calls == 0, (
        f"a cache hit must skip the live provider entirely (the seeded prior search was "
        f"inserted directly into the DB, not through this spy); got {flight_search_spy.calls} "
        f"calls"
    )


@then("the snapshot contains the stale saved flights and itinerary")
def _snapshot_contains_saved_state(bag: dict) -> None:
    response = bag["response"]
    assert response.status_code == 200, f"expected 200, got {response.status_code}: {response.text}"
    body = response.json()
    assert [offer["id"] for offer in body["flight_search"]["offers"]] == bag["latest_flight_ids"]
    assert body["flight_search"]["is_stale"] is True
    assert body["plan"]["status"] == "ready"
    assert body["plan"]["itinerary"]["days"]


@then("no agent run is recorded for the restore")
def _restore_records_no_run(trip_id: int) -> None:
    assert run_db(lambda session: get_agent_runs(session, trip_id)) == []


@given("the planner will produce a ready itinerary")
def _planner_ready(planner_spy: PlannerRunSpy) -> None:
    planner_spy.output = ItineraryOut(
        days=[ItineraryDayOut(day_number=1, summary="Explore the city", activities=[])]
    )


@given("the planner will ask a clarifying question")
def _planner_clarify(planner_spy: PlannerRunSpy) -> None:
    planner_spy.output = ClarificationOut(questions=["What is your budget?"])


@when("the trip is planned")
def _plan_trip(client, trip_id: int, bag: dict) -> None:
    bag["response"] = client.post(f"/api/trips/{trip_id}/plan")


@then('the response is 200 with status "ready" and an itinerary')
def _plan_ready(bag: dict) -> None:
    response = bag["response"]
    assert response.status_code == 200, f"expected 200, got {response.status_code}: {response.text}"
    body = response.json()
    assert body["status"] == "ready"
    assert body["itinerary"]["days"]


@then("the trip's status becomes \"itinerary_ready\"")
def _trip_status_itinerary_ready(trip_id: int) -> None:
    trip = run_db(lambda session: get_trip(session, trip_id))
    assert trip.status == "itinerary_ready"


@then('the response is 200 with status "needs_clarification" and no itinerary stored')
def _plan_needs_clarification(bag: dict, trip_id: int) -> None:
    response = bag["response"]
    assert response.status_code == 200, f"expected 200, got {response.status_code}: {response.text}"
    body = response.json()
    assert body["status"] == "needs_clarification"
    assert body["questions"]
    trip = run_db(lambda session: get_trip(session, trip_id))
    assert trip.status != "itinerary_ready", (
        f"a clarification response must not flip the trip to itinerary_ready, got {trip.status}"
    )


@then("the planner is never called")
def _planner_never_called(planner_spy: PlannerRunSpy) -> None:
    assert planner_spy.calls == 0, (
        f"an already-planned trip must reuse the stored itinerary, not re-run the agent; "
        f"got {planner_spy.calls} calls"
    )


@given("the flight provider will return offers priced 812, 499, and 640 USD")
def _provider_returns_unordered_prices(flight_search_spy: FlightSearchSpy) -> None:
    flight_search_spy.offers = [
        NormalizedFlightOffer(
            carrier=f"carrier-{price}",
            price_usd=price,
            currency="USD",
            depart_at="2030-09-01T09:00:00",
            arrive_at="2030-09-01T21:30:00",
            stops=0,
            booking_token=f"tok-{price}",
            raw_offer={"price": price},
        )
        for price in (812.0, 499.0, 640.0)
    ]


@given("the flight provider will return a paired outbound and return offer")
def _provider_returns_round_trip(flight_search_spy: FlightSearchSpy) -> None:
    flight_search_spy.offers = [
        NormalizedFlightOffer(
            carrier="Air France",
            price_usd=772,
            currency="USD",
            depart_at="2030-09-01T09:00",
            arrive_at="2030-09-01T21:30",
            stops=0,
            booking_token="resolved-token",
            raw_offer={
                "flights": [
                    {
                        "airline": "Air France",
                        "departure_airport": {
                            "id": "JFK",
                            "date": "2030-09-01",
                            "time": "09:00",
                        },
                        "arrival_airport": {
                            "id": "CDG",
                            "date": "2030-09-01",
                            "time": "21:30",
                        },
                    }
                ],
                "booking_token": "resolved-token",
                "return_flights": [
                    {
                        "airline": "Air France",
                        "departure_airport": {
                            "id": "CDG",
                            "date": "2030-09-08",
                            "time": "13:00",
                        },
                        "arrival_airport": {
                            "id": "JFK",
                            "date": "2030-09-08",
                            "time": "15:30",
                        },
                    }
                ],
            },
        )
    ]


@then("the response offer contains the outbound and return legs in travel order")
def _response_contains_round_trip_legs(bag: dict) -> None:
    response = bag["response"]
    assert response.status_code == 200, f"expected 200, got {response.status_code}: {response.text}"
    legs = response.json()["offers"][0]["legs"]
    assert [(leg["departure_airport"], leg["arrival_airport"]) for leg in legs] == [
        ("JFK", "CDG"),
        ("CDG", "JFK"),
    ]


@then("the response is 200 with offers ordered cheapest first")
def _offers_ordered_cheapest_first(bag: dict) -> None:
    response = bag["response"]
    assert response.status_code == 200, f"expected 200, got {response.status_code}: {response.text}"
    prices = [offer["price_usd"] for offer in response.json()["offers"]]
    assert prices == [499.0, 640.0, 812.0], (
        f"search_flights must return offers ascending by price (the take-home's 'find the "
        f"cheapest flights' requirement depends on it, and the frontend lists offers in this "
        f"order without re-sorting); provider returned them as 812/499/640 but the API must "
        f"reorder to 499/640/812, got {prices}"
    )


@given("an existing trip with cached flights and a generated itinerary", target_fixture="trip_id")
def _trip_with_cached_flights_and_itinerary() -> int:
    async def _work(session):
        trip_id = await seed_trip(session, depart_date="2030-09-01")
        await seed_flight_search_results(session, trip_id, minutes_ago=1)
        await seed_itinerary(session, trip_id)
        return trip_id

    return run_db(_work)


@when("the trip's destination airport is changed")
def _change_destination_airport(client, trip_id: int, bag: dict) -> None:
    bag["response"] = client.patch(f"/api/trips/{trip_id}", json={"destination_airport": "LHR"})


@then("the response is 200 and the trip has no cached flight offers")
def _no_cached_flight_offers(bag: dict, trip_id: int) -> None:
    response = bag["response"]
    assert response.status_code == 200, f"expected 200, got {response.status_code}: {response.text}"
    flight_results = run_db(lambda session: get_flight_search_results(session, trip_id))
    assert flight_results == [], (
        f"changing the trip's route must invalidate flight offers priced for the old route, or "
        f"/flights/search reuses them within the TTL; found {len(flight_results)} stale offer(s)"
    )


@then("the trip has no stored itinerary")
def _no_stored_itinerary(trip_id: int) -> None:
    itinerary = run_db(lambda session: get_itinerary(session, trip_id))
    assert itinerary is None, (
        "changing the trip's route must invalidate the itinerary researched for the old "
        "destination, or /plan returns the stale itinerary instead of re-planning"
    )
