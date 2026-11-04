"""Measure the signed-token boundary: what fraction of tampered or forged tokens are rejected.

Reuses the application's own `issue_token`/`verify_token`. For each trial it mints a valid
token, confirms it is accepted, then applies each tamper class and counts rejections. Prints a
single JSON object; never prints a token or the signing secret.
"""

import argparse
import base64
import json
import time
from fastapi import HTTPException

from app.security_token_issuer import issue_token, verify_token


def _rejected(token: str) -> bool:
    try:
        verify_token(token)
        return False
    except HTTPException:
        return True


def _tampered_variants(valid: str) -> list[str]:
    payload, signature = valid.rsplit(".", 1)
    flipped = signature[:-1] + ("0" if signature[-1] != "0" else "1")
    forged_payload = (
        base64.urlsafe_b64encode(json.dumps({"user_id": 1, "expires_at": 9_999_999_999}).encode())
        .decode()
        .rstrip("=")
    )
    return [
        f"{payload}.{flipped}",      # flipped signature byte
        f"{forged_payload}.{signature}",  # forged claims, reused old signature
        "not.a.token",               # malformed
        "",                          # empty
    ]


def measure(trials: int) -> dict:
    if trials < 1:
        raise ValueError("trials must be positive")
    accepted_valid = 0
    tamper_attempts = 0
    tamper_rejected = 0
    for _ in range(trials):
        valid = issue_token({"user_id": 1, "expires_at": int(time.time()) + 900})
        if not _rejected(valid):
            accepted_valid += 1
        for variant in _tampered_variants(valid):
            tamper_attempts += 1
            if _rejected(variant):
                tamper_rejected += 1
    return {
        "trials": trials,
        "valid_tokens_accepted": accepted_valid,
        "tamper_attempts": tamper_attempts,
        "tamper_rejected": tamper_rejected,
        "tamper_rejection_rate": round(tamper_rejected / tamper_attempts, 4),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials", type=int, default=250)
    arguments = parser.parse_args()
    print(json.dumps(measure(arguments.trials), sort_keys=True))


if __name__ == "__main__":
    main()
