"""Rate limiting for the two quota-spending routes (/plan, /flights/search): a per-IP request
cap, and a global concurrency cap on real LLM calls specifically.
"""

import asyncio
import time
from collections import defaultdict

from fastapi import Request

from app.config import (
    MAX_CONCURRENT_AGENT_RUNS,
    RATE_LIMIT_MAX_REQUESTS,
    RATE_LIMIT_WINDOW_SECONDS,
)
from app.schemas import ErrorCode, LoginRequest

_agent_run_lock = asyncio.Lock()
_agent_runs_in_flight = 0

_request_timestamps: dict[str, list[float]] = defaultdict(list)


class RateLimitError(Exception):
    """A domain rejection carrying the client-facing code, detail, and Retry-After seconds."""

    def __init__(self, code: ErrorCode, detail: str, retry_after_seconds: int) -> None:
        self.code = code
        self.detail = detail
        self.retry_after_seconds = retry_after_seconds
        super().__init__(detail)


def _enforce_rate_limit(key: str) -> None:
    now = time.monotonic()
    window_start = now - RATE_LIMIT_WINDOW_SECONDS

    recent = [timestamp for timestamp in _request_timestamps[key] if timestamp > window_start]
    if len(recent) >= RATE_LIMIT_MAX_REQUESTS:
        raise RateLimitError(
            ErrorCode.RATE_LIMIT_EXCEEDED,
            f"Rate limit exceeded: max {RATE_LIMIT_MAX_REQUESTS} requests per "
            f"{RATE_LIMIT_WINDOW_SECONDS}s from this client on this route.",
            retry_after_seconds=RATE_LIMIT_WINDOW_SECONDS,
        )
    recent.append(now)
    _request_timestamps[key] = recent


async def enforce_request_rate_limit(request: Request) -> None:
    _enforce_rate_limit(request.client.host if request.client else "unknown")


async def enforce_login_rate_limit(body: LoginRequest, request: Request) -> None:
    client_ip = request.client.host if request.client else "unknown"
    _enforce_rate_limit(f"login:{client_ip}:{body.email.strip().casefold()}")


async def acquire_agent_run_slot() -> None:
    """Non-blocking: an already-full concurrency cap rejects immediately (429) instead of
    queuing the request behind an indefinite wait for a real LLM call.

    A plain lock-guarded counter, not asyncio.Semaphore: ``asyncio.wait_for(sem.acquire(),
    timeout=0)`` is a known asyncio footgun — the zero-timeout callback can fire before the
    acquire coroutine gets a chance to run at all, spuriously rejecting even an uncontended
    semaphore (confirmed empirically: it failed on the very first call every time).
    """
    global _agent_runs_in_flight
    async with _agent_run_lock:
        if _agent_runs_in_flight >= MAX_CONCURRENT_AGENT_RUNS:
            raise RateLimitError(
                ErrorCode.RATE_LIMIT_EXCEEDED,
                f"Too many concurrent planning runs (max {MAX_CONCURRENT_AGENT_RUNS}); "
                "retry shortly.",
                retry_after_seconds=5,
            )
        _agent_runs_in_flight += 1


def release_agent_run_slot() -> None:
    global _agent_runs_in_flight
    _agent_runs_in_flight = max(0, _agent_runs_in_flight - 1)
