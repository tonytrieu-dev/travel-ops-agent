"""Deterministic HTTP walkthrough for the local zero-trust controls."""

import json
import os
import ssl
from pathlib import Path

import httpx

from app.security_identity import totp_code as totp


def login(client: httpx.Client, email: str, device_id: str) -> str:
    response = client.post(
        "/api/auth/login",
        json={"email": email, "device_id": device_id, "password": "demo-password"},
    )
    response.raise_for_status()
    body = response.json()
    if not body["mfa_required"]:
        return body["access_token"]
    response = client.post(
        "/api/auth/mfa", json={"session_id": body["session_id"], "code": totp("JBSWY3DPEHPK3PXP")}
    )
    response.raise_for_status()
    body = response.json()
    return body["access_token"]


def show(label: str, response: httpx.Response) -> None:
    print(f"{label}: {response.status_code} {json.dumps(response.json())[:240]}")


def client_for(device_id: str) -> httpx.Client:
    certificates = Path(os.environ.get("TRAVEL_OPS_CERTIFICATES", "../.device-certificates"))
    transport = ssl.create_default_context(cafile=str(certificates / "ca.crt"))
    transport.load_cert_chain(str(certificates / f"{device_id}.crt"), str(certificates / f"{device_id}.key"))
    return httpx.Client(base_url=os.getenv("TRAVEL_OPS_URL", "https://localhost:8443"), verify=transport)


with client_for("device-a-traveler") as client, client_for("device-a-operator") as operator_client:
    traveler = login(client, "traveler@tenant-a.test", "device-a-traveler")
    operator = login(operator_client, "operator@tenant-a.test", "device-a-operator")
    headers = {"Authorization": f"Bearer {traveler}"}
    show("owned trips", client.get("/api/trips", headers=headers))
    show("browser segment spoof", client.get("/api/trips", headers={**headers, "x-source-segment": "agent"}))
    for attempt in range(5):
        show(f"denial {attempt + 1}", client.get("/api/trips/999999", headers=headers))
    show(
        "security incidents",
        operator_client.get("/api/security/incidents", headers={"Authorization": f"Bearer {operator}"}),
    )
    forged = traveler[:-1] + ("0" if traveler[-1] != "0" else "1")
    show("forged token", client.get("/api/trips", headers={"Authorization": f"Bearer {forged}"}))
