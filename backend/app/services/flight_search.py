"""FlightSearchService: the one implementation of flight-offer search, caching, filtering,
persistence, and ordering behind both the /flights/search route and the planner's
search_flights tool. The two callers differ only in persist/allow_cross_trip_cache — the
route persists and reaches across trips, the planner tool trusts only this trip's own
recent search and never writes offers (see planner.search_flights for why)."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from app.adapters.flights_searchapi import FlightProvider, NormalizedFlightOffer
from app.agent.execution_log import record_event
from app.config import FLIGHT_CACHE_TTL_MINUTES
from app.models import (
    AgentRun,
    AgentRunStep,
    AgentStepKind,
    ExecutionEventKind,
    FlightResultSource,
    FlightSearchResult,
    TripRequest,
    TripStatus,
    utcnow,
)
from app.repositories.trips_repository import ErrorCode, TripError, cheapest_first


@dataclass
class TripFlightSearchOutcome:
    offers: list[FlightSearchResult]
    unavailable_reason: str | None
    source: str  # "same_trip_cache" | "cross_trip_cache" | "live"


def _complete_flight_results(
    offers: list[FlightSearchResult], return_date: str | None
) -> list[FlightSearchResult]:
    if return_date is None:
        return offers
    return [
        offer
        for offer in offers
        if offer.raw_offer.get("return_flights")
        and offer.raw_offer.get("booking_token") == offer.booking_token
    ]


def offer_summary(offer: FlightSearchResult | NormalizedFlightOffer) -> dict:
    return {
        "carrier": offer.carrier,
        "price_usd": offer.price_usd,
        "currency": offer.currency,
        "depart_at": offer.depart_at,
        "arrive_at": offer.arrive_at,
        "stops": offer.stops,
    }


def flight_provider_name(provider: FlightProvider) -> str:
    return {
        "LiveSearchApiProvider": "SearchApi",
        "RecordedProvider": "Recorded flights",
    }.get(type(provider).__name__, type(provider).__name__)


class FlightSearchService:
    def __init__(self, session: AsyncSession, provider: FlightProvider) -> None:
        self._session = session
        self._provider = provider

    async def search(
        self,
        trip_id: int,
        *,
        agent_run: AgentRun | None = None,
        persist: bool = True,
        allow_cross_trip_cache: bool = True,
    ) -> TripFlightSearchOutcome:
        trip = await self._session.get(TripRequest, trip_id)
        if trip is None:
            raise TripError(ErrorCode.TRIP_NOT_FOUND, 404, f"No trip {trip_id}.")
        # Captured once per call so every branch (cache hit, cross-trip cache, live) can
        # finalize agent_run.started_at/total_ms consistently with the pre-refactor
        # _record_search_flights_run, which timed the whole call, not just the provider hop.
        run_started_at = utcnow()
        run_started_monotonic = time.monotonic()

        same_trip_cache = await self._get_same_trip_cache(trip)
        if same_trip_cache:
            await self._log(
                trip, agent_run, "ok",
                f"{len(same_trip_cache)} offers (reused, already searched within TTL)",
                same_trip_cache, run_started_at, run_started_monotonic,
            )
            return TripFlightSearchOutcome(same_trip_cache, None, "same_trip_cache")
        if allow_cross_trip_cache:
            cross_trip_offers = await self._get_cross_trip_cache(trip)
            if cross_trip_offers:
                results = [
                    FlightSearchResult(
                        trip_request_id=trip_id, offer_index=source_offer.offer_index,
                        carrier=source_offer.carrier, price_usd=source_offer.price_usd,
                        currency=source_offer.currency, depart_at=source_offer.depart_at,
                        arrive_at=source_offer.arrive_at, stops=source_offer.stops,
                        booking_token=source_offer.booking_token, raw_offer=source_offer.raw_offer,
                        source=FlightResultSource.CACHED,
                        created_at=source_offer.created_at,
                    )
                    for source_offer in cross_trip_offers
                ]
                self._session.add_all(results)
                trip.status = TripStatus.FLIGHTS_SEARCHED
                await self._session.commit()
                await self._log(
                    trip, agent_run, "ok",
                    f"{len(results)} offers (cached from an identical route/date search)",
                    results, run_started_at, run_started_monotonic,
                )
                return TripFlightSearchOutcome(cheapest_first(results), None, "cross_trip_cache")
        provider_started = time.monotonic()
        outcome = await self._provider.search_offers(
            trip.origin, trip.destination_airport, trip.depart_date, trip.return_date
        )
        provider_duration_ms = round((time.monotonic() - provider_started) * 1000)
        results = [
            FlightSearchResult(
                trip_request_id=trip_id, offer_index=index, carrier=offer.carrier,
                price_usd=offer.price_usd, currency=offer.currency, depart_at=offer.depart_at,
                arrive_at=offer.arrive_at, stops=offer.stops, booking_token=offer.booking_token,
                raw_offer=offer.raw_offer, source=FlightResultSource.LIVE,
            )
            for index, offer in enumerate(outcome.offers)
        ]
        if persist:
            self._session.add_all(results)
            if results:
                trip.status = TripStatus.FLIGHTS_SEARCHED
            await self._session.commit()
        await self._log(
            trip, agent_run,
            "ok" if outcome.unavailable_reason is None else "unavailable",
            f"{len(results)} offers" if outcome.unavailable_reason is None else outcome.unavailable_reason,
            outcome.offers if not persist else results,
            run_started_at, run_started_monotonic,
            provider_duration_ms,
        )
        return TripFlightSearchOutcome(cheapest_first(results), outcome.unavailable_reason, "live")

    async def _get_same_trip_cache(self, trip: TripRequest) -> list[FlightSearchResult]:
        cutoff = utcnow() - timedelta(minutes=FLIGHT_CACHE_TTL_MINUTES)
        results = list(
            await self._session.scalars(
                select(FlightSearchResult).where(
                    col(FlightSearchResult.trip_request_id) == trip.id,
                    col(FlightSearchResult.created_at) >= cutoff,
                )
            )
        )
        return cheapest_first(_complete_flight_results(results, trip.return_date))

    async def _get_cross_trip_cache(self, trip: TripRequest) -> list[FlightSearchResult]:
        cutoff = utcnow() - timedelta(minutes=FLIGHT_CACHE_TTL_MINUTES)
        source_trip_id = await self._session.scalar(
            select(FlightSearchResult.trip_request_id)
            .join(TripRequest, col(FlightSearchResult.trip_request_id) == col(TripRequest.id))
            .where(
                col(TripRequest.origin) == trip.origin,
                col(TripRequest.destination_airport) == trip.destination_airport,
                col(TripRequest.depart_date) == trip.depart_date,
                col(TripRequest.return_date) == trip.return_date,
                col(FlightSearchResult.created_at) >= cutoff,
            )
            .order_by(col(FlightSearchResult.created_at).desc())
            .limit(1)
        )
        if source_trip_id is None:
            return []
        source_offers = list(
            await self._session.scalars(
                select(FlightSearchResult).where(
                    col(FlightSearchResult.trip_request_id) == source_trip_id,
                    col(FlightSearchResult.created_at) >= cutoff,
                )
            )
        )
        return _complete_flight_results(source_offers, trip.return_date)

    async def _log(
        self,
        trip: TripRequest,
        agent_run: AgentRun | None,
        status: str,
        detail: str,
        offers: list[FlightSearchResult] | list[NormalizedFlightOffer],
        run_started_at: datetime,
        run_started_monotonic: float,
        duration_ms: int | None = None,
    ) -> None:
        await record_event(
            ExecutionEventKind.API_CALL, "search_flights", status, detail, duration_ms,
            data={"offers": [offer_summary(offer) for offer in offers]},
            provider=agent_run.model if agent_run is not None else flight_provider_name(self._provider),
        )
        if agent_run is None:
            return
        finished_at = utcnow()
        total_ms = round((time.monotonic() - run_started_monotonic) * 1000)
        assert agent_run.id is not None
        agent_run.status = "completed" if status == "ok" else status
        agent_run.started_at = run_started_at
        agent_run.finished_at = finished_at
        agent_run.total_ms = total_ms
        self._session.add(
            AgentRunStep(
                agent_run_id=agent_run.id, seq=1, kind=AgentStepKind.TOOL, name="search_flights",
                status=status, duration_ms=duration_ms if duration_ms is not None else total_ms,
                input_summary=(
                    f"{trip.origin} to {trip.destination_airport}, "
                    f"{trip.depart_date} to {trip.return_date or 'one-way'}"
                ),
                output_summary=detail, tokens=0,
            )
        )
        await self._session.commit()
