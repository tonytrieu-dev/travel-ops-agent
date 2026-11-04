"""Device possession is verified by the private mutual-TLS gateway."""
import hmac
import re

from fastapi import HTTPException, Request

from app import config
from app.security_posture import enforcement_active


def verified_device_id(request: Request) -> str | None:
    secret = config.get_settings().device_gateway_secret
    if (
        secret is None
        or not hmac.compare_digest(request.headers.get("x-device-gateway", "").encode(), secret.get_secret_value().encode())
        or request.headers.get("x-device-verified") != "SUCCESS"
    ):
        return None
    match = re.fullmatch(r"CN=([A-Za-z0-9_-]{1,100})", request.headers.get("x-device-subject", ""))
    return match[1] if match else None


def verify_device_certificate(request: Request, device_id: str) -> None:
    if not enforcement_active():
        return
    if verified_device_id(request) != device_id:
        raise HTTPException(status_code=401, detail="registered device certificate required")
