"""Small authenticated HTTP load check; prints latency and throughput, never access tokens."""

import argparse
import json
import math
import os
from concurrent.futures import ThreadPoolExecutor
from statistics import median
from time import perf_counter

import httpx


def measure(client: httpx.Client, headers: dict[str, str], *, requests: int, concurrency: int) -> dict:
    if requests < 1 or concurrency < 1:
        raise ValueError("requests and concurrency must be positive")

    def request(_: int) -> float:
        started = perf_counter()
        response = client.get("/api/trips", headers=headers)
        response.raise_for_status()
        assert response.status_code == 200, response.status_code
        return (perf_counter() - started) * 1000

    for index in range(5):
        request(index)
    started = perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        durations = sorted(pool.map(request, range(requests)))
    elapsed = perf_counter() - started
    return {
        "requests": requests,
        "concurrency": concurrency,
        "p50_ms": round(median(durations), 3),
        "p95_ms": round(durations[math.ceil(requests * 0.95) - 1], 3),
        "requests_per_second": round(requests / elapsed, 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=4)
    arguments = parser.parse_args()
    with httpx.Client(base_url=os.getenv("TRAVEL_OPS_URL", "http://localhost:8000")) as client:
        result = measure(
            client, {"Authorization": f"Bearer {os.environ['TRAVEL_OPS_TOKEN']}"},
            requests=arguments.requests, concurrency=arguments.concurrency,
        )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
