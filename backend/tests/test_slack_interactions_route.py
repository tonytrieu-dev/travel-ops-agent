import hashlib
import hmac
import time

import httpx
import pytest

from app.config import Settings
from app.main import app

pytestmark = pytest.mark.no_database


@pytest.mark.asyncio
async def test_signed_slack_form_payload_reaches_interaction_parser(monkeypatch) -> None:
    secret = "slack-test-secret"
    settings = Settings(
        cerebras_api_key="test",
        searchapi_api_key="test",
        tavily_api_key="test",
        database_url="postgresql+asyncpg://localhost/test",
        slack_bot_token="xoxb-test",
        slack_signing_secret=secret,
        slack_approvals_channel_id="C123",
    )
    monkeypatch.setattr("app.routes.slack.get_settings", lambda: settings)
    timestamp = str(int(time.time()))
    body = b"payload=%7B%7D"
    base_string = f"v0:{timestamp}:".encode() + body
    signature = "v0=" + hmac.new(secret.encode(), base_string, hashlib.sha256).hexdigest()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/slack/interactions",
            content=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Slack-Request-Timestamp": timestamp,
                "X-Slack-Signature": signature,
            },
        )

    assert response.status_code == 200
