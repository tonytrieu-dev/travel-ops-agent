"""Test harness: a real Postgres (``travel_agent_test``, migrated to head so the append-only
triggers exist), the FastAPI app wired to it via a synchronous Starlette ``TestClient``, and
per-test truncation.

Why sync ``TestClient`` rather than an async httpx client: pytest-bdd 8 runs step functions
synchronously, so the request calls inside steps must be synchronous too. ``TestClient`` drives
the async app through an anyio portal on a single background event loop — which is exactly what
makes ``test_double_execute_books_once`` real: two threaded ``execute`` calls become two
concurrent tasks on that one loop, each with its own DB connection, so the ``SELECT ... FOR
UPDATE`` in one genuinely blocks the other in Postgres.

Direct DB seeding (a booking already CONFIRMED, or one whose fare has expired) runs through
``run_db`` on its own short-lived engine/loop, fully committed and disposed before the request —
so app connections (portal loop) and seed connections (seed loop) never cross event loops.
"""

import asyncio
import os
from dataclasses import dataclass, field

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.adapters.flights_searchapi import FlightSearchOutcome, NormalizedFlightOffer
from app.schemas import ClarificationOut, ItineraryDayOut, ItineraryOut
from tests.db_helpers import TEST_DATABASE_URL, run_db

# Must run before any ``app.*`` import: ``app.db``/``app.config`` build their engine once from
# ``DATABASE_URL`` at first import, and ``execute_booking_durable`` (DBOS can't take an injected
# session) reads that same module-level engine directly, bypassing the FastAPI dependency
# override below entirely — so the test DB has to be correct at the source, not just at the DI
# seam.
os.environ.setdefault("DATABASE_URL", TEST_DATABASE_URL)

_ALL_TABLES = (
    "booking_transition, execution_event, agent_run_step, agent_run, hitl_booking_log, "
    "itinerary, flight_search_result, trip_request, user_account, connector_setting, security_event, security_session, security_incident"
)


@pytest.fixture(autouse=True)
def _truncate_between_tests(request: pytest.FixtureRequest) -> None:
    if request.node.get_closest_marker("no_database") is not None:
        return
    async def _truncate(session: AsyncSession) -> None:
        await session.execute(text(f"TRUNCATE {_ALL_TABLES} RESTART IDENTITY CASCADE"))

    run_db(_truncate)


@pytest.fixture(autouse=True)
def _reset_rate_limit_state() -> None:
    """The rate limiter's per-IP counters and in-flight-run counter are plain module-level
    state (no DB truncation reaches them) — without this, request counts leak across tests that
    share the TestClient's fixed IP and make rate-limit tests order-dependent."""
    from app import rate_limit

    rate_limit._request_timestamps.clear()
    rate_limit._agent_runs_in_flight = 0


@dataclass
class BookingOptionsFetchSpy:
    """Stands in for the real SearchApi ``FlightProvider`` and counts booking-options calls.

    Shaped as a ``FlightProvider`` (not the old ``BookingOptionsFetcher`` closure) because
    ``execute_booking_durable``'s DBOS step resolves its provider via ``get_flight_provider``,
    the same seam ``test_flight_provider_strategy.py`` already exercises — not an injected
    closure, which DBOS workflow args can't carry.
    """

    options: list[dict] = field(
        default_factory=lambda: [
            {"provider": "Test Air", "price_usd": 512.0, "booking_url": "https://example.test/book"}
        ]
    )
    calls: int = 0
    should_fail: bool = False
    last_call_params: dict[str, str | None] | None = None

    async def fetch_booking_options(
        self,
        booking_token: str,
        *,
        departure_id: str,
        arrival_id: str,
        outbound_date: str,
        return_date: str | None,
        booking_token_is_resolved: bool = False,
    ) -> list[dict]:
        self.calls += 1
        self.last_call_params = {
            "booking_token": booking_token,
            "departure_id": departure_id,
            "arrival_id": arrival_id,
            "outbound_date": outbound_date,
            "return_date": return_date,
        }
        if self.should_fail:
            raise RuntimeError("simulated upstream failure")
        return self.options

    async def search_offers(
        self, departure_id: str, arrival_id: str, outbound_date: str, return_date: str | None
    ) -> FlightSearchOutcome:
        # Booking tests never exercise this leg; present but unused, just enough to conform to
        # the FlightProvider protocol (the durable planner workflow wraps it as a DBOS step
        # unconditionally, whether or not search_flights is ever called in a given test).
        return FlightSearchOutcome(offers=[], unavailable_reason=None)


@dataclass
class FlightSearchSpy:
    """Stands in for the real SearchApi ``FlightProvider`` and counts live search calls, so a
    cache-hit test can assert the provider was genuinely skipped, not just that a response looks
    plausible."""

    offers: list[NormalizedFlightOffer] = field(
        default_factory=lambda: [
            NormalizedFlightOffer(
                carrier="AF",
                price_usd=512.0,
                currency="USD",
                depart_at="2026-08-01T09:00:00",
                arrive_at="2026-08-01T21:30:00",
                stops=0,
                booking_token="tok-live",
                raw_offer={"price": 512.0},
            )
        ]
    )
    unavailable_reason: str | None = None
    calls: int = 0
    last_search_params: dict[str, str | None] | None = None

    async def search_offers(
        self, departure_id: str, arrival_id: str, outbound_date: str, return_date: str | None
    ) -> FlightSearchOutcome:
        self.calls += 1
        self.last_search_params = {
            "departure_id": departure_id,
            "arrival_id": arrival_id,
            "outbound_date": outbound_date,
            "return_date": return_date,
        }
        return FlightSearchOutcome(
            offers=self.offers, unavailable_reason=self.unavailable_reason
        )

    async def fetch_booking_options(
        self,
        booking_token: str,
        *,
        departure_id: str,
        arrival_id: str,
        outbound_date: str,
        return_date: str | None,
        booking_token_is_resolved: bool = False,
    ) -> list[dict]:
        raise NotImplementedError("FlightSearchSpy only stands in for search_offers")


@dataclass
class PlannerRunSpy:
    """Stands in for ``run_planner_durable`` so trip-planning tests never spend real LLM
    quota, and counts calls so an idempotent /plan can assert the agent ran at most once."""

    output: ItineraryOut | ClarificationOut = field(
        default_factory=lambda: ItineraryOut(
            days=[
                ItineraryDayOut(
                    day_number=1,
                    summary="Explore",
                    activities=[],
                )
            ]
        )
    )
    calls: int = 0

    async def __call__(self, trip_id: int, prompt: str) -> ItineraryOut | ClarificationOut:
        self.calls += 1
        return self.output


@pytest.fixture
def booking_options_spy() -> BookingOptionsFetchSpy:
    return BookingOptionsFetchSpy()


@pytest.fixture
def flight_search_spy() -> FlightSearchSpy:
    return FlightSearchSpy()


@pytest.fixture
def planner_spy() -> PlannerRunSpy:
    return PlannerRunSpy()


@pytest.fixture
def client(
    booking_options_spy: BookingOptionsFetchSpy,
    flight_search_spy: FlightSearchSpy,
    planner_spy: PlannerRunSpy,
    monkeypatch: pytest.MonkeyPatch,
):
    from starlette.testclient import TestClient

    from app.db import get_engine, get_session
    from app.main import app

    app_engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    app_session_factory = async_sessionmaker(app_engine, expire_on_commit=False)

    async def _override_get_session():
        async with app_session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = _override_get_session
    monkeypatch.setattr("app.dbos_runtime.get_flight_provider", lambda settings: booking_options_spy)
    monkeypatch.setattr("app.routes.trips.get_flight_provider", lambda settings: flight_search_spy)
    monkeypatch.setattr("app.routes.trips.run_planner_durable", planner_spy)
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    asyncio.run(app_engine.dispose())
    # execute_booking_durable can't take a Depends-injected session (DBOS args must be
    # serializable), so it opens one from app.db's own module-level engine directly — that
    # engine's pooled connections would otherwise outlive this test's TestClient portal loop and
    # get reused on the next test's (different) loop, which asyncpg rejects outright.
    asyncio.run(get_engine().dispose())
