"""Local recurring assessments; missing runs are exposed through result freshness."""

import asyncio
import logging

from app.db import get_session_factory
from app.repositories.compliance_repository import assess_security_controls

logger = logging.getLogger(__name__)
ASSESSMENT_INTERVAL_SECONDS = 60


async def monitor_security_controls() -> None:
    while True:
        try:
            async with get_session_factory()() as session:
                await assess_security_controls(session)
        except Exception:
            logger.exception("Security-control assessment failed; previous results will become stale")
        await asyncio.sleep(ASSESSMENT_INTERVAL_SECONDS)
