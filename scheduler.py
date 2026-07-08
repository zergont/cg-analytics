# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

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
    """Запустить аналитику v2 для всего активного оборудования за вчерашний день."""
    from datetime import datetime, timezone
    from db.analytics import get_equipment_registry
    from analytics.runner import run_analysis
    from config import settings

    yesterday = date.today() - timedelta(days=1)
    ts_from = datetime(yesterday.year, yesterday.month, yesterday.day, tzinfo=timezone.utc)
    ts_to = ts_from + timedelta(days=1)

    logger.info("Плановый запуск аналитики v2 за %s", yesterday)

    registry = await get_equipment_registry()
    active = [eq for eq in registry if eq.get("active")]

    if not active:
        logger.warning("Нет активного оборудования в реестре. Пропуск.")
        return

    logger.info("Активных ГУ: %d", len(active))

    for eq in active:
        controller_id = eq.get("controller_id")
        engine_id = eq.get("engine_id")
        kb_path_rel = eq.get("kb_path")
        if not ((controller_id and engine_id) or kb_path_rel):
            logger.warning(
                "Нет привязки конфига (пара или kb_path) для %s/%s/%s — пропуск",
                eq["router_sn"], eq["equip_type"], eq["panel_id"],
            )
            continue
        try:
            result = await run_analysis(
                router_sn=eq["router_sn"],
                equip_type=eq["equip_type"],
                panel_id=eq["panel_id"],
                engine_sn=eq.get("engine_sn", ""),
                ts_from=ts_from,
                ts_to=ts_to,
                kb_root=settings.knowledge_base_path,
                controller_id=controller_id,
                engine_id=engine_id,
                kb_path=kb_path_rel,
            )
            if result.get("error"):
                logger.error(
                    "Ошибка аналитики для %s/%s/%s: %s",
                    eq["router_sn"], eq["equip_type"], eq["panel_id"],
                    result["error"],
                )
            else:
                logger.info(
                    "Аналитика %s/%s/%s: сегментов=%d обнаружений=%d run_id=%s",
                    eq["router_sn"], eq["equip_type"], eq["panel_id"],
                    result["segments_count"], result["detections_count"],
                    result.get("run_id"),
                )
        except Exception:
            logger.exception(
                "Ошибка аналитики для %s/%s/%s",
                eq["router_sn"], eq["equip_type"], eq["panel_id"],
            )
