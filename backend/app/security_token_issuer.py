"""Local HMAC-signed bearer tokens; identity and session checks live in security_identity."""

import base64
import binascii
import hashlib
import hmac
import json

from fastapi import HTTPException

from app import config


def issue_token(payload: dict[str, object]) -> str:
    encoded = (
        base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode())
        .decode()
        .rstrip("=")
    )
    secret = config.get_settings().zero_trust_signing_secret.get_secret_value()
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def verify_token(token: str) -> dict[str, object]:
    try:
        payload, signature = token.rsplit(".", 1)
        secret = config.get_settings().zero_trust_signing_secret.get_secret_value()
        expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("invalid signature")
        claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
        if not isinstance(claims, dict):
            raise ValueError("token payload must be an object")
        return claims
    except (binascii.Error, UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError):
        raise HTTPException(status_code=401, detail="invalid zero-trust access token") from None
