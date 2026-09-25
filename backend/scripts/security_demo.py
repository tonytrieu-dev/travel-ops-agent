"""Deterministic HTTP walkthrough for the local zero-trust controls."""

import base64
import hashlib
import hmac
import json
import os
import time

import httpx


def totp(secret: str) -> str:
    counter = int(time.time()) // 30
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, counter.to_bytes(8, "big"), hashlib.sha1).digest()
    offset = digest[-1] & 15
    return f"{(int.from_bytes(digest[offset : offset + 4], 'big') & 0x7FFFFFFF) % 1_000_000:06d}"


def login(client: httpx.Client, email: str, device_id: str, mfa: bool = False) -> str:
    response = client.post(
        "/api/auth/login",
        json={"email": email, "device_id": device_id, "password": "demo-password"},
    )
    response.raise_for_status()
    body = response.json()
    if mfa:
        response = client.post(
            "/api/auth/mfa", json={"session_id": body["session_id"], "code": totp("JBSWY3DPEHPK3PXP")}
        )
        response.raise_for_status()
        body = response.json()
    return body["access_token"]


def show(label: str, response: httpx.Response) -> None:
    print(f"{label}: {response.status_code} {json.dumps(response.json())[:240]}")


with httpx.Client(base_url=os.getenv("TRAVEL_OPS_URL", "http://localhost:8000")) as client:
    traveler = login(client, "traveler@tenant-a.test", "device-a-traveler", mfa=True)
    operator = login(client, "operator@tenant-a.test", "device-a-operator", mfa=True)
    headers = {"Authorization": f"Bearer {traveler}"}
    show("owned trips", client.get("/api/trips", headers=headers))
    show("browser segment spoof", client.get("/api/trips", headers={**headers, "x-source-segment": "agent"}))
    for attempt in range(5):
        show(f"denial {attempt + 1}", client.get("/api/trips/999999", headers=headers))
    show(
        "security incidents",
        client.get("/api/security/incidents", headers={"Authorization": f"Bearer {operator}"}),
    )
    show("forged token", client.get("/api/trips", headers={"Authorization": f"Bearer {traveler[:-1]}0"}))
