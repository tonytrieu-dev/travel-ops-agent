"""SearchApi.io Google Flights adapter. Strategy pattern: FlightProvider Protocol, Live vs
Recorded selected by USE_LIVE_FLIGHT_API. Tolerant — errors/empty results return
unavailable_reason, never a fabricated offer.
"""

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx

from app.config import (
    FLIGHT_CASSETTE_DIR,
    SEARCHAPI_BASE_URL,
    SEARCHAPI_TIMEOUT_SECONDS,
    Settings,
)


@dataclass
class NormalizedFlightOffer:
    """One flight offer, normalized from SearchApi's ``best_flights``/``other_flights`` shape
    into the fields ``FlightSearchResult`` persists."""

    carrier: str
    price_usd: float
    currency: str
    depart_at: str
    arrive_at: str
    stops: int
    booking_token: str
    raw_offer: dict[str, Any]


@dataclass
class FlightSearchOutcome:
    """Real offers, or an honest empty state naming why there are none."""

    offers: list[NormalizedFlightOffer] = field(default_factory=list)
    unavailable_reason: str | None = None


class FlightProvider(Protocol):
    async def search_offers(
        self,
        departure_id: str,
        arrival_id: str,
        outbound_date: str,
        return_date: str | None,
    ) -> FlightSearchOutcome: ...

    async def fetch_booking_options(
        self,
        booking_token: str,
        *,
        departure_id: str,
        arrival_id: str,
        outbound_date: str,
        return_date: str | None,
        booking_token_is_resolved: bool = False,
    ) -> list[dict[str, Any]]: ...


def cache_key(
    departure_id: str, arrival_id: str, outbound_date: str, return_date: str | None
) -> str:
    return f"{departure_id}_{arrival_id}_{outbound_date}_{return_date or 'oneway'}"


def _airport_datetime(airport: dict[str, Any]) -> str:
    # SearchApi returns date and time as separate fields; join them into one ISO datetime so the
    # value is a real timestamp (the field's name promises), not a bare "06:05" that can't be
    # placed on a calendar.
    date = airport.get("date", "")
    time = airport.get("time", "")
    if date and time:
        return f"{date}T{time}"
    return time or date


def derive_flight_legs(raw_offer: dict[str, Any]) -> list[dict[str, Any]]:
    """One entry per flown segment, in order — the connecting airport between two legs is the
    stop a traveler would ask "what is this layover?" about. Derived at read time from the same
    stored raw_offer every other offer field comes from, never a separate persisted shape."""
    return [
        {
            "airline": leg.get("airline", "Unknown"),
            "flight_number": leg.get("flight_number"),
            "departure_airport": leg.get("departure_airport", {}).get("id", ""),
            "depart_at": _airport_datetime(leg.get("departure_airport", {})),
            "arrival_airport": leg.get("arrival_airport", {}).get("id", ""),
            "arrive_at": _airport_datetime(leg.get("arrival_airport", {})),
            "duration_minutes": leg.get("duration"),
        }
        for leg in [*raw_offer.get("flights", []), *raw_offer.get("return_flights", [])]
    ]


def _parse_offers(payload: dict[str, Any]) -> list[NormalizedFlightOffer]:
    # Round-trip offers carry departure_token, not booking_token (confirmed against a real payload).
    offers: list[NormalizedFlightOffer] = []
    for raw_offer in [*payload.get("best_flights", []), *payload.get("other_flights", [])]:
        flights = raw_offer.get("flights", [])
        token = raw_offer.get("booking_token") or raw_offer.get("departure_token")
        if not flights or "price" not in raw_offer or not token:
            continue
        first_leg, last_leg = flights[0], flights[-1]
        offers.append(
            NormalizedFlightOffer(
                carrier=first_leg.get("airline", "Unknown"),
                price_usd=float(raw_offer["price"]),
                currency="USD",
                depart_at=_airport_datetime(first_leg.get("departure_airport", {})),
                arrive_at=_airport_datetime(last_leg.get("arrival_airport", {})),
                stops=len(flights) - 1,
                booking_token=token,
                raw_offer=raw_offer,
            )
        )
    return offers


class LiveSearchApiProvider:
    """Calls the real SearchApi.io Google Flights engine (spends the one-time 100-search quota)."""

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    async def search_offers(
        self,
        departure_id: str,
        arrival_id: str,
        outbound_date: str,
        return_date: str | None,
    ) -> FlightSearchOutcome:
        params = {
            "engine": "google_flights",
            "departure_id": departure_id,
            "arrival_id": arrival_id,
            "outbound_date": outbound_date,
            "currency": "USD",
        }
        if return_date:
            params["return_date"] = return_date
        else:
            # SearchApi's one-way flag is flight_type=one_way, not SerpApi's type=2 convention —
            # the wrong param name was silently ignored, leaving flight_type defaulted to
            # round_trip, which then 400s demanding return_date.
            params["flight_type"] = "one_way"

        try:
            async with httpx.AsyncClient(timeout=SEARCHAPI_TIMEOUT_SECONDS) as client:
                response = await client.get(
                    SEARCHAPI_BASE_URL,
                    params=params,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
        except httpx.HTTPError as error:
            return FlightSearchOutcome(
                unavailable_reason=(
                    f"searchapi flight search failed: network error ({error!r}) "
                    f"for {cache_key(departure_id, arrival_id, outbound_date, return_date)}"
                )
            )

        if response.status_code == 429:
            return FlightSearchOutcome(
                unavailable_reason=(
                    "searchapi flight search failed: HTTP 429 quota exhausted "
                    f"(departure_id={departure_id} arrival_id={arrival_id} "
                    f"outbound_date={outbound_date})"
                )
            )
        if response.status_code != 200:
            return FlightSearchOutcome(
                unavailable_reason=(
                    f"searchapi flight search failed: HTTP {response.status_code} "
                    f"(departure_id={departure_id} arrival_id={arrival_id} "
                    f"outbound_date={outbound_date}): {response.text}"
                )
            )

        offers = _parse_offers(response.json())
        if not offers:
            return FlightSearchOutcome(
                unavailable_reason=(
                    f"searchapi flight search returned no offers for {departure_id}->"
                    f"{arrival_id} on {outbound_date}"
                )
            )
        if return_date is None:
            return FlightSearchOutcome(offers=offers)

        # Prefer nonstop offers, but do not fail a route that only has connecting options.
        nonstop_offers = sorted(
            (offer for offer in offers if offer.stops == 0),
            key=lambda offer: offer.price_usd,
        )[:3]
        displayed_offers = nonstop_offers or sorted(offers, key=lambda offer: offer.price_usd)[:3]
        resolved = await asyncio.gather(
            *(
                self._pair_round_trip_offer(
                    offer,
                    departure_id=departure_id,
                    arrival_id=arrival_id,
                    outbound_date=outbound_date,
                    return_date=return_date,
                )
                for offer in displayed_offers
            ),
            return_exceptions=True,
        )
        paired_offers = [
            result for result in resolved if isinstance(result, NormalizedFlightOffer)
        ]
        if paired_offers:
            return FlightSearchOutcome(offers=paired_offers)
        detail = str(resolved[0]) if resolved else "no outbound offers"
        return FlightSearchOutcome(
            unavailable_reason=f"searchapi could not resolve exact round-trip offers: {detail}"
        )

    async def _get_or_raise(self, params: dict[str, Any], *, action: str, desc: str) -> dict[str, Any]:
        """Shared GET + error handling for calls that raise on failure. search_offers keeps its
        own handling because it degrades to an honest outcome and has a 429-specific branch."""
        try:
            async with httpx.AsyncClient(timeout=SEARCHAPI_TIMEOUT_SECONDS) as client:
                response = await client.get(
                    SEARCHAPI_BASE_URL,
                    params=params,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
        except httpx.HTTPError as error:
            raise RuntimeError(
                f"searchapi {action} failed: network error ({error!r}) for {desc}"
            ) from error

        if response.status_code != 200:
            raise RuntimeError(
                f"searchapi {action} failed: HTTP {response.status_code} for {desc}: {response.text}"
            )
        return response.json()

    async def _resolve_return_offer(
        self,
        departure_token: str,
        *,
        departure_id: str,
        arrival_id: str,
        outbound_date: str,
        return_date: str,
    ) -> NormalizedFlightOffer:
        """A round-trip offer's stored token is a departure_token, not a bookable booking_token
        (see _parse_offers) — SearchApi requires a second call, passing departure_token, to get
        the return-leg options. Each of those carries its own real booking_token. The current UI
        has no separate return-flight-selection step, so this picks the cheapest — the same
        tie-break the rest of the app already uses for "the" flight."""
        params = {
            "engine": "google_flights",
            "departure_token": departure_token,
            "departure_id": departure_id,
            "arrival_id": arrival_id,
            "outbound_date": outbound_date,
            "return_date": return_date,
            "currency": "USD",
        }
        payload = await self._get_or_raise(
            params,
            action="departure_token resolution",
            desc=f"departure_token={departure_token}",
        )
        return_offers = _parse_offers(payload)
        if not return_offers:
            raise RuntimeError(
                f"searchapi returned no return-leg options for departure_token={departure_token}"
            )
        return min(return_offers, key=lambda offer: offer.price_usd)

    async def _pair_round_trip_offer(
        self,
        outbound_offer: NormalizedFlightOffer,
        *,
        departure_id: str,
        arrival_id: str,
        outbound_date: str,
        return_date: str,
    ) -> NormalizedFlightOffer:
        return_offer = await self._resolve_return_offer(
            outbound_offer.booking_token,
            departure_id=departure_id,
            arrival_id=arrival_id,
            outbound_date=outbound_date,
            return_date=return_date,
        )
        outbound_offer.price_usd = return_offer.price_usd
        outbound_offer.booking_token = return_offer.booking_token
        outbound_offer.raw_offer = {
            **outbound_offer.raw_offer,
            "booking_token": return_offer.booking_token,
            "return_flights": return_offer.raw_offer["flights"],
        }
        return outbound_offer

    async def fetch_booking_options(
        self,
        booking_token: str,
        *,
        departure_id: str,
        arrival_id: str,
        outbound_date: str,
        return_date: str | None,
        booking_token_is_resolved: bool = False,
    ) -> list[dict[str, Any]]:
        if return_date and not booking_token_is_resolved:
            booking_token = (
                await self._resolve_return_offer(
                    booking_token,
                    departure_id=departure_id,
                    arrival_id=arrival_id,
                    outbound_date=outbound_date,
                    return_date=return_date,
                )
            ).booking_token

        params = {
            "engine": "google_flights",
            "booking_token": booking_token,
            "departure_id": departure_id,
            "arrival_id": arrival_id,
            "outbound_date": outbound_date,
            "currency": "USD",
        }
        if return_date:
            params["return_date"] = return_date
        else:
            # SearchApi's one-way flag is flight_type=one_way, not SerpApi's type=2 convention —
            # the wrong param name was silently ignored, leaving flight_type defaulted to
            # round_trip, which then 400s demanding return_date.
            params["flight_type"] = "one_way"
        payload = await self._get_or_raise(
            params, action="booking-options fetch", desc=f"booking_token={booking_token}"
        )
        return list(payload.get("booking_options", []))


class RecordedProvider:
    """Replays a real-captured cassette — never a hand-fabricated shape."""

    def __init__(self, cassette_dir: Path) -> None:
        self._cassette_dir = cassette_dir

    def _cassette_path(self, key: str) -> Path:
        return self._cassette_dir / f"{key}.json"

    async def search_offers(
        self,
        departure_id: str,
        arrival_id: str,
        outbound_date: str,
        return_date: str | None,
    ) -> FlightSearchOutcome:
        key = cache_key(departure_id, arrival_id, outbound_date, return_date)
        cassette_path = self._cassette_path(key)
        if not cassette_path.exists():
            return FlightSearchOutcome(
                unavailable_reason=f"no recorded cassette for {key} at {cassette_path}"
            )
        payload = json.loads(cassette_path.read_text())
        offers = _parse_offers(payload)
        if not offers:
            return FlightSearchOutcome(
                unavailable_reason=f"recorded cassette {key} contains no offers"
            )
        if return_date and any(not offer.raw_offer.get("return_flights") for offer in offers):
            return FlightSearchOutcome(
                unavailable_reason=(
                    f"recorded cassette {key} has outbound offers but no exact return pairing"
                )
            )
        return FlightSearchOutcome(offers=offers)

    async def fetch_booking_options(
        self,
        booking_token: str,
        *,
        departure_id: str,
        arrival_id: str,
        outbound_date: str,
        return_date: str | None,
        booking_token_is_resolved: bool = False,
    ) -> list[dict[str, Any]]:
        cassette_path = self._cassette_path(f"booking_{booking_token}")
        if not cassette_path.exists():
            raise RuntimeError(f"no recorded booking-options cassette at {cassette_path}")
        payload = json.loads(cassette_path.read_text())
        return list(payload.get("booking_options", []))


def get_flight_provider(settings: Settings) -> FlightProvider:
    """Strategy selection: the toggle is read exactly once, here."""
    if settings.use_live_flight_api:
        return LiveSearchApiProvider(api_key=settings.searchapi_api_key.get_secret_value())
    return RecordedProvider(cassette_dir=FLIGHT_CASSETTE_DIR)
