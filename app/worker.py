from __future__ import annotations

import asyncio
import logging

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.services.estate_alert_service import run_scheduled_payment_reminders
from app.services.subscription_lifecycle_service import run_subscription_lifecycle_jobs


settings = get_settings()
logger = logging.getLogger(__name__)


async def _run_payment_reminders_forever() -> None:
    while True:
        db = SessionLocal()
        try:
            run_scheduled_payment_reminders(db)
        except Exception:
            logger.exception("Scheduled payment reminder worker cycle failed.")
        finally:
            db.close()
        await asyncio.sleep(6 * 60 * 60)


async def _run_subscription_lifecycle_forever() -> None:
    while True:
        db = SessionLocal()
        try:
            run_subscription_lifecycle_jobs(db)
        except Exception:
            logger.exception("Scheduled subscription lifecycle worker cycle failed.")
        finally:
            db.close()
        await asyncio.sleep(60 * 60)


async def main() -> None:
    logger.info("Starting worker process with role '%s'.", settings.PROCESS_ROLE)
    await asyncio.gather(
        _run_payment_reminders_forever(),
        _run_subscription_lifecycle_forever(),
    )


if __name__ == "__main__":
    asyncio.run(main())
