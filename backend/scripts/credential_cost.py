"""Measure the password KDF's wall-clock cost and the attacker guess-rate it imposes.

Reuses the application's own `password_hash`/`password_matches` so the number reflects
the deployed credential primitive, not a reimplementation. Prints a single JSON object;
never prints a password or a hash.
"""

import argparse
import json
from statistics import median
from time import perf_counter

from app.security_identity import password_hash, password_matches
from app.models import User


def measure(samples: int) -> dict:
    if samples < 1:
        raise ValueError("samples must be positive")
    # One stored hash the "attacker" repeatedly tries to match; a wrong guess still pays
    # the full KDF cost, which is the whole point of the work factor.
    stored = password_hash("correct horse battery staple")
    probe = User(email="probe@example.test", password_hash=stored)

    hash_durations = []
    for _ in range(samples):
        started = perf_counter()
        password_hash("correct horse battery staple")
        hash_durations.append((perf_counter() - started) * 1000)

    verify_durations = []
    for _ in range(samples):
        started = perf_counter()
        password_matches(probe, "wrong-guess")  # a miss pays the full cost
        verify_durations.append((perf_counter() - started) * 1000)

    median_verify_ms = median(verify_durations)
    # Single-core offline guess rate an attacker gets against one stolen hash.
    guesses_per_second = round(1000 / median_verify_ms, 1)
    return {
        "iterations": 600_000,
        "algorithm": "pbkdf2_hmac_sha256",
        "salt_bytes": 16,
        "samples": samples,
        "median_hash_ms": round(median(hash_durations), 3),
        "median_verify_miss_ms": round(median_verify_ms, 3),
        "attacker_guesses_per_second_single_core": guesses_per_second,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=25)
    arguments = parser.parse_args()
    print(json.dumps(measure(arguments.samples), sort_keys=True))


if __name__ == "__main__":
    main()
