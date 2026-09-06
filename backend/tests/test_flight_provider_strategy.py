"""Guards the flight-fetching Strategy seam (Phase 3): USE_LIVE_FLIGHT_API selects the
provider once at composition, the never-called-live RecordedProvider degrades honestly
instead of fabricating an offer when a cassette is missing, and offer parsing is verified
against a real captured SearchApi payload (never a hand-fabricated shape).
"""

import json
from pathlib import Path

import httpx
import pytest

from app.adapters.flights_searchapi import (
    LiveSearchApiProvider,
    RecordedProvider,
    derive_flight_legs,
    get_flight_provider,
)
from app.config import FLIGHT_CASSETTE_DIR, Settings


async def test_live_provider_booking_options_error_includes_the_upstream_response_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard: a bare status code told a developer nothing about *why* SearchApi
    rejected the token (invalid vs expired vs malformed) — the response body is the only place
    that distinction lives."""

    async def _fake_get(self, url, params=None, headers=None) -> httpx.Response:
        return httpx.Response(400, json={"error": "booking_token has expired"})

    monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get)
    provider = LiveSearchApiProvider(api_key="test-key")

    with pytest.raises(RuntimeError, match="booking_token has expired"):
        await provider.fetch_booking_options(
            "some-token",
            departure_id="JFK",
            arrival_id="CDG",
            outbound_date="2026-08-15",
            return_date=None,
        )


@pytest.mark.parametrize(
    ("return_date", "expected_params"),
    [
        (None, {"flight_type": "one_way", "return_date": None}),
        ("2026-08-22", {"flight_type": None, "return_date": "2026-08-22"}),
    ],
)
async def test_live_provider_booking_options_sends_route_and_date_params(
    monkeypatch: pytest.MonkeyPatch,
    return_date: str | None,
    expected_params: dict[str, str | None],
) -> None:
    """Regression guard: SearchApi's booking-options engine 400s "Missing required parameter
    departure_id"/"return_date" when the full route+date context isn't sent (both observed
    against the live API). One-way needs `flight_type=one_way` (SearchApi's actual param —
    SerpApi's `type=2` convention is a different API and is silently ignored here, which is
    exactly the bug this regression guards); round-trip needs `return_date` — dropping either
    param regresses one of the two branches search_offers already handles correctly. Round-trip
    also makes a first call to resolve the stored departure_token into a real booking_token —
    this only captures the params of the final, real booking-options request."""
    captured_params: dict[str, object] = {}

    async def _fake_get(self, url, params=None, headers=None) -> httpx.Response:
        params = params or {}
        if "departure_token" in params:
            # The departure_token -> booking_token resolution call, round-trip only.
            return httpx.Response(
                200,
                json={
                    "best_flights": [
                        {
                            "flights": [{"airline": "United", "departure_airport": {}, "arrival_airport": {}}],
                            "price": 250,
                            "booking_token": "resolved-tok",
                        }
                    ]
                },
            )
        captured_params.update(params)
        return httpx.Response(200, json={"booking_options": []})

    monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get)
    provider = LiveSearchApiProvider(api_key="test-key")

    await provider.fetch_booking_options(
        "tok-123",
        departure_id="JFK",
        arrival_id="CDG",
        outbound_date="2026-08-15",
        return_date=return_date,
    )

    assert captured_params.get("flight_type") == expected_params["flight_type"], (
        f"one-way booking-options request must set flight_type=one_way (return_date={return_date!r}), "
        f"got params={captured_params}"
    )
    assert captured_params.get("return_date") == expected_params["return_date"], (
        f"round-trip booking-options request must forward return_date (return_date={return_date!r}), "
        f"got params={captured_params}"
    )
    assert captured_params.get("departure_id") == "JFK"
    assert captured_params.get("arrival_id") == "CDG"
    assert captured_params.get("outbound_date") == "2026-08-15"


async def test_live_provider_round_trip_booking_options_use_the_cheapest_resolved_return_leg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A round-trip offer's stored token is a departure_token, not a bookable booking_token —
    using it directly against the booking-options engine is the pre-fix bug. The resolution call
    returns several return-leg choices (the current UI has no return-flight-selection step), and
    the real booking-options request must use the cheapest one's booking_token, not the original
    departure_token or a pricier alternative."""
    captured_params: dict[str, object] = {}

    async def _fake_get(self, url, params=None, headers=None) -> httpx.Response:
        params = params or {}
        if "departure_token" in params:
            assert params["departure_token"] == "dep-tok-original", (
                f"resolution call must forward the stored departure_token, got {params}"
            )
            return httpx.Response(
                200,
                json={
                    "best_flights": [
                        {
                            "flights": [{"airline": "United", "departure_airport": {}, "arrival_airport": {}}],
                            "price": 400,
                            "booking_token": "pricier-tok",
                        }
                    ],
                    "other_flights": [
                        {
                            "flights": [{"airline": "Delta", "departure_airport": {}, "arrival_airport": {}}],
                            "price": 150,
                            "booking_token": "cheapest-tok",
                        }
                    ],
                },
            )
        captured_params.update(params)
        return httpx.Response(200, json={"booking_options": [{"book_with": "Delta"}]})

    monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get)
    provider = LiveSearchApiProvider(api_key="test-key")

    result = await provider.fetch_booking_options(
        "dep-tok-original",
        departure_id="JFK",
        arrival_id="CDG",
        outbound_date="2026-08-15",
        return_date="2026-08-22",
    )

    assert captured_params.get("booking_token") == "cheapest-tok", (
        f"the real booking-options request must use the cheapest resolved return leg's "
        f"booking_token, got booking_token={captured_params.get('booking_token')!r}"
    )
    assert result == [{"book_with": "Delta"}]


async def test_live_provider_search_offers_unavailable_reason_includes_the_upstream_response_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same gap, the other call: a non-429 non-200 search failure previously discarded the
    upstream body, hiding *why* (malformed params vs an account issue vs anything else)."""

    async def _fake_get(self, url, params=None, headers=None) -> httpx.Response:
        return httpx.Response(400, json={"error": "unsupported currency"})

    monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get)
    provider = LiveSearchApiProvider(api_key="test-key")

    outcome = await provider.search_offers("JFK", "CDG", "2026-08-01", None)

    assert outcome.unavailable_reason is not None and "unsupported currency" in outcome.unavailable_reason, (
        f"unavailable_reason must surface the upstream body, got {outcome.unavailable_reason!r}"
    )


async def test_live_provider_allows_slow_searchapi_flight_searches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_get(self, url, params=None, headers=None) -> httpx.Response:
        assert self.timeout.read == 60.0, (
            "SearchApi took just over 15 seconds for a real round-trip search; "
            f"the client must allow that normal latency, got {self.timeout.read}s"
        )
        return httpx.Response(200, json={"best_flights": [], "other_flights": []})

    monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get)

    await LiveSearchApiProvider(api_key="test-key").search_offers(
        "ONT", "SFO", "2026-11-26", "2026-12-01"
    )


async def test_live_round_trip_search_returns_exact_pairs_and_reuses_the_resolved_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial_payload = json.loads(
        (
            FLIGHT_CASSETTE_DIR / "JFK_CDG_2026-08-15_2026-08-22.json"
        ).read_text()
    )
    resolved_departure_tokens: list[str] = []

    async def _fake_get(self, url, params=None, headers=None) -> httpx.Response:
        params = params or {}
        if "departure_token" in params:
            resolved_departure_tokens.append(params["departure_token"])
            index = len(resolved_departure_tokens)
            return httpx.Response(
                200,
                json={
                    "best_flights": [
                        {
                            "flights": [
                                {
                                    "airline": "Air France",
                                    "flight_number": f"AF {index}",
                                    "departure_airport": {
                                        "id": "CDG",
                                        "date": "2026-08-22",
                                        "time": "13:00",
                                    },
                                    "arrival_airport": {
                                        "id": "JFK",
                                        "date": "2026-08-22",
                                        "time": "15:30",
                                    },
                                }
                            ],
                            "price": 800 + index,
                            "booking_token": f"resolved-{index}",
                        }
                    ]
                },
            )
        if "booking_token" in params:
            return httpx.Response(200, json={"booking_options": [{"book_with": "Air France"}]})
        return httpx.Response(200, json=initial_payload)

    monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get)
    provider = LiveSearchApiProvider(api_key="test-key")

    outcome = await provider.search_offers("JFK", "CDG", "2026-08-15", "2026-08-22")

    assert len(outcome.offers) == 3
    assert all(
        [
            (leg["departure_airport"], leg["arrival_airport"])
            for leg in derive_flight_legs(offer.raw_offer)
        ]
        == [("JFK", "CDG"), ("CDG", "JFK")]
        for offer in outcome.offers
    )
    await provider.fetch_booking_options(
        outcome.offers[0].booking_token,
        departure_id="JFK",
        arrival_id="CDG",
        outbound_date="2026-08-15",
        return_date="2026-08-22",
        booking_token_is_resolved=True,
    )
    assert len(resolved_departure_tokens) == 3


async def test_live_round_trip_search_falls_back_to_connecting_outbound_offers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A route may have no nonstop outbound offers; connecting offers are the usable fallback."""
    resolved_tokens: list[str] = []

    async def _fake_get(self, url, params=None, headers=None) -> httpx.Response:
        params = params or {}
        if "departure_token" in params:
            resolved_tokens.append(params["departure_token"])
            return httpx.Response(
                200,
                json={
                    "best_flights": [
                        {
                            "flights": [
                                {
                                    "airline": "Return Air",
                                    "departure_airport": {},
                                    "arrival_airport": {},
                                }
                            ],
                            "price": 300,
                            "booking_token": "resolved-return",
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "best_flights": [
                    {
                        "flights": [
                            {
                                "airline": "Connecting Air",
                                "departure_airport": {},
                                "arrival_airport": {},
                            },
                            {
                                "airline": "Connecting Air",
                                "departure_airport": {},
                                "arrival_airport": {},
                            },
                        ],
                        "price": 500,
                        "departure_token": "connecting-departure",
                    }
                ]
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get)
    outcome = await LiveSearchApiProvider(api_key="test-key").search_offers(
        "JFK", "CDG", "2026-08-15", "2026-08-22"
    )

    assert len(outcome.offers) == 1
    assert outcome.offers[0].stops == 1
    assert resolved_tokens == ["connecting-departure"]


@pytest.mark.parametrize(
    ("return_date", "expected_params"),
    [
        (None, {"flight_type": "one_way", "return_date": None}),
        ("2026-08-22", {"flight_type": None, "return_date": "2026-08-22"}),
    ],
)
async def test_live_provider_search_offers_sends_flight_type_for_one_way(
    monkeypatch: pytest.MonkeyPatch,
    return_date: str | None,
    expected_params: dict[str, str | None],
) -> None:
    """Regression guard for a real live-demo 400 ("Missing required parameter return_date." on a
    one-way search): the code sent SerpApi's `type=2` one-way convention, which SearchApi's
    google_flights engine doesn't recognize, so it silently fell back to its round_trip default
    and demanded return_date. SearchApi's actual param is `flight_type=one_way`."""
    captured_params: dict[str, object] = {}

    async def _fake_get(self, url, params=None, headers=None) -> httpx.Response:
        captured_params.update(params or {})
        return httpx.Response(200, json={"best_flights": [], "other_flights": []})

    monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get)
    provider = LiveSearchApiProvider(api_key="test-key")

    await provider.search_offers("JFK", "CDG", "2026-08-01", return_date)

    assert captured_params.get("flight_type") == expected_params["flight_type"], (
        f"one-way search must set flight_type=one_way (return_date={return_date!r}), got "
        f"params={captured_params}"
    )
    assert captured_params.get("return_date") == expected_params["return_date"], (
        f"round-trip search must forward return_date (return_date={return_date!r}), got "
        f"params={captured_params}"
    )


def _settings(use_live_flight_api: bool) -> Settings:
    return Settings(
        cerebras_api_key="test-cerebras-key",
        searchapi_api_key="test-searchapi-key",
        tavily_api_key="test-tavily-key",
        database_url="postgresql+asyncpg://unused/unused",
        use_live_flight_api=use_live_flight_api,
    )


@pytest.mark.parametrize(
    ("use_live_flight_api", "expected_type", "wrong_selection_consequence"),
    [
        (True, LiveSearchApiProvider, "the live demo would silently replay stale cassettes"),
        (False, RecordedProvider, "tests/dev-reloads would burn the one-time 100-search quota"),
    ],
)
def test_use_live_flight_api_selects_the_matching_provider(
    use_live_flight_api: bool, expected_type: type, wrong_selection_consequence: str
) -> None:
    provider = get_flight_provider(_settings(use_live_flight_api=use_live_flight_api))

    assert isinstance(provider, expected_type), (
        f"USE_LIVE_FLIGHT_API={use_live_flight_api} must select {expected_type.__name__}, got "
        f"{type(provider).__name__}; a wrong selection here means {wrong_selection_consequence}."
    )


async def test_recorded_provider_missing_cassette_is_honest_empty_not_fabricated(
    tmp_path: Path,
) -> None:
    provider = RecordedProvider(cassette_dir=tmp_path)

    outcome = await provider.search_offers("JFK", "CDG", "2026-08-01", None)

    assert outcome.offers == []
    assert outcome.unavailable_reason is not None and "JFK_CDG_2026-08-01" in outcome.unavailable_reason, (
        f"the unavailable_reason must name the missing cache key so a developer can find/capture "
        f"it, got {outcome.unavailable_reason!r}"
    )


async def test_recorded_provider_does_not_expose_unpaired_round_trip_offers() -> None:
    provider = RecordedProvider(cassette_dir=FLIGHT_CASSETTE_DIR)

    outcome = await provider.search_offers("JFK", "CDG", "2026-08-15", "2026-08-22")

    assert outcome.offers == []
    assert outcome.unavailable_reason is not None and "no exact return pairing" in outcome.unavailable_reason


async def test_derive_flight_legs_carries_one_entry_per_flown_segment() -> None:
    """The frontend expand-to-see-stops feature needs the real per-leg breakdown, not just the
    aggregate stops count — this must come from the real captured payload's flights array."""
    raw_offer = json.loads(
        (FLIGHT_CASSETTE_DIR / "JFK_CDG_2026-08-15_2026-08-22.json").read_text()
    )["best_flights"][0]

    legs = derive_flight_legs(raw_offer)

    assert len(legs) == len(raw_offer["flights"])
    assert legs[0]["departure_airport"] == raw_offer["flights"][0]["departure_airport"]["id"]


async def test_parsed_offer_departure_carries_its_date_not_just_a_clock_time() -> None:
    """SearchApi returns an airport's ``date`` and ``time`` as separate fields; the adapter must
    join them so ``depart_at`` holds a placeable timestamp. Dropping the date (the original bug)
    left a bare ``06:05`` that the UI rendered as 'Invalid Date'. Fails red if the date is
    dropped again."""
    raw_offer = json.loads(
        (FLIGHT_CASSETTE_DIR / "JFK_CDG_2026-08-15_2026-08-22.json").read_text()
    )["best_flights"][0]
    departure_date = raw_offer["flights"][0]["departure_airport"]["date"]
    legs = derive_flight_legs(raw_offer)
    assert departure_date in legs[0]["depart_at"], (
        f"depart_at must include the offer's departure date {departure_date!r} so it parses to a "
        f"real datetime, got {legs[0]['depart_at']!r} — the SearchApi date field is dropped"
    )
