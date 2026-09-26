import logging

import httpx
import pytest

from app.main import create_app

pytestmark = pytest.mark.no_database


async def test_unhandled_error_returns_and_logs_the_request_correlation_id(caplog) -> None:
    app = create_app()

    @app.get("/failure")
    async def _failure() -> None:
        raise RuntimeError("sensitive failure detail")

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    with caplog.at_level(logging.ERROR, logger="app.main"):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/failure", headers={"x-correlation-id": "failure-correlation"}
            )

    assert response.status_code == 500
    assert response.headers["x-correlation-id"] == "failure-correlation"
    assert "sensitive failure detail" not in response.text
    assert "failure-correlation" in caplog.text
