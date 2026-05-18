"""Оркестратор суточного pipeline: агрегация → детектирование → агент → сохранение."""
import logging
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from config import settings
from db import source, analytics
from knowledge.loader import load_knowledge
from pipeline import aggregator, detector
from agent import loop as agent_loop

logger = logging.getLogger(__name__)


@dataclass
class RunContext:
    """Контекст одного запуска pipeline — передаётся в executor агента."""
    router_sn: str
    equip_type: str
    panel_id: int
    day: date

    manufacturer: str = ""
    model: str = ""
    engine_sn: str = ""
    equipment_name: str = ""

    # Карты из knowledge_base
    register_map: dict[int, dict] = field(default_factory=dict)
    fault_bitmap_map: dict[int, list[dict]] = field(default_factory=dict)
    enum_map: dict[str, dict] = field(default_factory=dict)

    # Результаты агрегации
    aggregates: dict[str, Any] = field(default_factory=dict)

    # Сырая история для построения графиков агентом
    # addr → sorted list of (ts, value) tuples
    history_series: dict[int, list[tuple]] = field(default_factory=dict)

    # События за сутки
    events: list[dict[str, Any]] = field(default_factory=list)

    # Найденные аномалии
    anomalies: list[dict[str, Any]] = field(default_factory=list)


async def run_pipeline(
    router_sn: str,
    equip_type: str,
    panel_id: int,
    day: date,
) -> dict[str, Any]:
    """Запустить полный pipeline для одной ГУ.

    Returns:
        Словарь с результатами, сохранёнными в analytics DB.
    """
    t_start = time.monotonic()
    label = f"{router_sn}/{equip_type}/{panel_id} за {day}"
    logger.info("Запуск pipeline: %s", label)

    # 1. Метаданные оборудования
    equip_info = await source.get_equipment_info(router_sn, equip_type, panel_id)
    if not equip_info:
        raise ValueError(f"Оборудование не найдено в основной БД: {label}")

    manufacturer = equip_info.get("manufacturer") or ""
    model = equip_info.get("model") or ""
    engine_sn = equip_info.get("engine_sn") or ""

    if not manufacturer or not model:
        raise ValueError(
            f"Поля manufacturer/model не заполнены для {label}. "
            "Заполните в разделе «Оборудование» основного UI."
        )

    # 2. Загрузка карт регистров из knowledge_base
    logger.info("Загрузка knowledge base: %s / %s", manufacturer, model)
    kb = load_knowledge(manufacturer, model)

    # 3. Загрузка истории и событий
    logger.info("Загрузка истории телеметрии...")
    history = await source.get_daily_history(router_sn, equip_type, panel_id, day)
    events = await source.get_daily_events(router_sn, equip_type, panel_id, day)
    logger.info("Загружено %d строк истории, %d событий", len(history), len(events))

    # 4. Агрегация
    logger.info("Агрегация данных...")
    agg_result = aggregator.aggregate(history, kb["register_map"])

    # Подготовить history_series для agent executor
    from collections import defaultdict
    history_series: dict[int, list[tuple]] = defaultdict(list)
    for row in history:
        history_series[row["addr"]].append((row["ts"], row["value"], row.get("raw")))

    # 5. Детектирование отклонений
    logger.info("Детектирование отклонений...")
    anomalies = detector.detect(
        history=history,
        events=events,
        register_map=kb["register_map"],
        fault_bitmap_map=kb["fault_bitmap_map"],
        aggregates=agg_result,
    )
    logger.info("Обнаружено аномалий: %d", len(anomalies))

    # 6. Формирование контекста
    ctx = RunContext(
        router_sn=router_sn,
        equip_type=equip_type,
        panel_id=panel_id,
        day=day,
        manufacturer=manufacturer,
        model=model,
        engine_sn=engine_sn,
        equipment_name=equip_info.get("name") or "",
        register_map=kb["register_map"],
        fault_bitmap_map=kb["fault_bitmap_map"],
        enum_map=kb["enum_map"],
        aggregates=agg_result,
        history_series=dict(history_series),
        events=events,
        anomalies=anomalies,
    )

    # 7. Agentic loop
    logger.info("Запуск agentic loop...")
    agent_result = await agent_loop.run(ctx)

    # 8. Определение итогового статуса
    status = _determine_status(anomalies, agent_result)

    # 9. Сохранение в analytics DB
    generation_time = round(time.monotonic() - t_start, 2)
    report = {
        "date": day,
        "router_sn": router_sn,
        "equip_type": equip_type,
        "panel_id": panel_id,
        "manufacturer": manufacturer,
        "model": model,
        "engine_sn": engine_sn,
        "status": status,
        "uptime_minutes": agg_result.get("uptime_minutes"),
        "starts_count": agg_result.get("starts_count"),
        "anomalies": anomalies,
        "aggregates": agg_result["by_register"],
        "ai_report": agent_result.get("report"),
        "ai_model": agent_result.get("model"),
        "tokens_used": agent_result.get("tokens_used"),
        "tool_calls_count": agent_result.get("tool_calls_count"),
        "generation_time_sec": generation_time,
    }

    report_id = await analytics.save_report(report)
    await analytics.upsert_equipment(equip_info)

    logger.info(
        "Pipeline завершён: %s | статус=%s | токены=%d | время=%.1fс | id=%s",
        label, status, agent_result.get("tokens_used", 0), generation_time, report_id
    )

    return {**report, "id": report_id}


def _determine_status(
    anomalies: list[dict],
    agent_result: dict,
) -> str:
    """Определить итоговый статус ГУ за сутки."""
    # Наличие shutdown аномалий → critical
    if any(a.get("severity") == "shutdown" for a in anomalies):
        return "critical"
    # Наличие предупреждений → attention
    if anomalies:
        return "attention"
    return "ok"
