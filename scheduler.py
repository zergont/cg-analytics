"""APScheduler: ежедневный запуск pipeline в 00:05 МСК (21:05 UTC)."""
import asyncio
import logging
from datetime import date, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from config import settings

logger = logging.getLogger(__name__)

_scheduler = AsyncIOScheduler(timezone="UTC")


def start_scheduler() -> None:
    trigger = CronTrigger(
        hour=settings.schedule_hour,
        minute=settings.schedule_minute,
        timezone="UTC",
    )
    _scheduler.add_job(
        _run_all_equipment,
        trigger=trigger,
        id="daily_analytics",
        replace_existing=True,
    )
    _scheduler.start()
    logger.info(
        "Планировщик запущен. Ежедневный запуск в %02d:%02d UTC.",
        settings.schedule_hour, settings.schedule_minute,
    )


def stop_scheduler() -> None:
    _scheduler.shutdown(wait=False)
    logger.info("Планировщик остановлен.")


async def _run_all_equipment() -> None:
    """Запустить pipeline для всего активного оборудования за вчерашний день."""
    from db.analytics import get_equipment_registry
    from pipeline.runner import run_pipeline

    yesterday = date.today() - timedelta(days=1)
    logger.info("Плановый запуск аналитики за %s", yesterday)

    registry = await get_equipment_registry()
    active = [eq for eq in registry if eq.get("active")]

    if not active:
        logger.warning("Нет активного оборудования в реестре. Пропуск.")
        return

    logger.info("Активных ГУ: %d", len(active))

    for eq in active:
        try:
            await run_pipeline(
                router_sn=eq["router_sn"],
                equip_type=eq["equip_type"],
                panel_id=eq["panel_id"],
                day=yesterday,
            )
        except Exception:
            logger.exception(
                "Ошибка pipeline для %s/%s/%s",
                eq["router_sn"], eq["equip_type"], eq["panel_id"],
            )
