# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Оркестратор аналитического прогона (batch replay).

Порядок работы:
1. Загрузить данные из source-БД через analytics.source
2. Вызвать segmenter.segment() — детерминированная обработка
3. Сериализовать результат в JSON + Markdown
4. Сохранить в analytics-БД через db.analytics
5. Вернуть run_id и сводку

Онлайн-режим (планируется): вместо get_whitelist_history использовать
потоковую подачу данных с накапливающимися сегментами.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import AnalyticsConfig
from .segmenter import segment
from .serializer import to_json, to_markdown, build_run_summary

logger = logging.getLogger(__name__)

def _read_version() -> str:
    try:
        return (Path(__file__).parent.parent / "VERSION").read_text(encoding="utf-8").strip()
    except Exception:
        return "unknown"

ANALYTICS_VERSION = _read_version()


def _tz(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


async def run_analysis(
    router_sn: str,
    equip_type: str,
    panel_id: int,
    engine_sn: str,
    ts_from: datetime,
    ts_to: datetime,
    kb_root: str | Path,
    *,
    controller_id: str | None = None,
    engine_id: str | None = None,
    kb_path: str | None = None,
    tz=None,
) -> dict[str, Any]:
    """Запустить аналитический прогон за период [ts_from, ts_to).

    Возвращает словарь:
    {
        "run_id": str | None,   # UUID в БД (None при ошибке сохранения)
        "segments_count": int,
        "detections_count": int,
        "max_severity": str | None,
        "data_quality_avg": float,
        "duration_ms": int,
        "error": str | None,
        "report_md": str,       # Markdown-отчёт
    }
    """
    t0 = time.monotonic()
    tf = _tz(ts_from)
    tt = _tz(ts_to)

    logger.info(
        "analytics.runner: старт прогона %s/%s/%s %s — %s",
        router_sn, equip_type, panel_id, tf.isoformat(), tt.isoformat(),
    )

    from . import binding
    try:
        cfg = binding.build_config(
            kb_root, controller_id=controller_id, engine_id=engine_id, kb_path=kb_path
        )
    except Exception as exc:
        logger.error("analytics.runner: ошибка загрузки конфигурации: %s", exc)
        return _error_result(str(exc), int((time.monotonic() - t0) * 1000))

    # Детерминированный справочник кодов неисправностей (заменяет RAG)
    fault_ref = None
    try:
        fault_ref = binding.build_fault_ref(
            kb_root, controller_id=controller_id, engine_id=engine_id, kb_path=kb_path
        )
    except Exception as exc:
        logger.warning("analytics.runner: FaultRef не загружен: %s", exc)

    try:
        # ── Загрузка данных из source-БД ──
        history, enum_periods, fault_periods, gaps = await _load_data(
            router_sn, equip_type, panel_id, tf, tt, cfg
        )
        logger.info(
            "analytics.runner: загружено history=%d enum=%d fault=%d gaps=%d",
            len(history), len(enum_periods), len(fault_periods), len(gaps),
        )

        # ── Сегментация и расчёт метрик ──
        segments = segment(
            enum_periods=enum_periods,
            history=history,
            fault_periods=fault_periods,
            gaps=gaps,
            cfg=cfg,
            router_sn=router_sn,
            equip_type=equip_type,
            panel_id=panel_id,
            engine_sn=engine_sn,
            ts_from=tf,
            ts_to=tt,
        )
        logger.info("analytics.runner: получено %d сегментов", len(segments))

        # ── Сериализация ──
        segments_json = to_json(
            segments, router_sn, equip_type, panel_id, tf, tt, ANALYTICS_VERSION
        )
        report_md = to_markdown(
            segments, router_sn, equip_type, panel_id, tf, tt, ANALYTICS_VERSION,
            tz=tz, fault_ref=fault_ref,
        )

        summary = build_run_summary(segments)
        duration_ms = int((time.monotonic() - t0) * 1000)

        # ── Сохранение в analytics-БД ──
        run_id = None
        try:
            from db import analytics as db_analytics
            run_id = await db_analytics.save_analysis_run({
                "router_sn": router_sn,
                "equip_type": equip_type,
                "panel_id": panel_id,
                "engine_sn": engine_sn,
                "ts_from": tf,
                "ts_to": tt,
                "analytics_version": ANALYTICS_VERSION,
                "segments_json": segments_json,
                "report_md": report_md,
                "segments_count": summary["segments_count"],
                "detections_count": summary["detections_count"],
                "max_severity": summary["max_severity"],
                "data_quality_avg": summary["data_quality_avg"],
                "duration_ms": duration_ms,
                "error": None,
            })
            logger.info("analytics.runner: сохранено run_id=%s", run_id)
        except Exception as db_exc:
            logger.warning("analytics.runner: ошибка сохранения в БД: %s", db_exc)

        return {
            "run_id": run_id,
            "report_md": report_md,
            "segments": segments,
            "error": None,
            "duration_ms": duration_ms,
            **summary,
        }

    except Exception as exc:
        duration_ms = int((time.monotonic() - t0) * 1000)
        logger.error("analytics.runner: ошибка прогона: %s", exc, exc_info=True)

        # Попытка сохранить запись об ошибке
        try:
            from db import analytics as db_analytics
            await db_analytics.save_analysis_run({
                "router_sn": router_sn,
                "equip_type": equip_type,
                "panel_id": panel_id,
                "engine_sn": engine_sn,
                "ts_from": tf,
                "ts_to": tt,
                "analytics_version": ANALYTICS_VERSION,
                "error": str(exc),
                "duration_ms": duration_ms,
            })
        except Exception:
            pass

        return _error_result(str(exc), duration_ms)


async def _load_data(
    router_sn: str,
    equip_type: str,
    panel_id: int,
    ts_from: datetime,
    ts_to: datetime,
    cfg: AnalyticsConfig,
) -> tuple[list, list, list, list]:
    """Загрузить все необходимые данные из source-БД параллельно."""
    import asyncio
    from . import source as analytics_source

    history_task = asyncio.create_task(
        analytics_source.get_whitelist_history_chunked(
            router_sn, equip_type, panel_id,
            ts_from, ts_to,
            cfg.whitelist_analog,
        )
    )
    enum_task = asyncio.create_task(
        analytics_source.get_enum_periods(
            router_sn, equip_type, panel_id,
            ts_from, ts_to,
            addrs=analytics_source.ENUM_READ_ADDRS,
        )
    )
    fault_task = asyncio.create_task(
        analytics_source.get_fault_periods(
            router_sn, equip_type, panel_id,
            ts_from, ts_to,
            fault_addrs=cfg.whitelist_fault,
        )
    )
    gaps_task = asyncio.create_task(
        analytics_source.get_data_gaps(
            router_sn, equip_type, panel_id,
            ts_from, ts_to,
        )
    )

    history, enum_periods, fault_periods, gaps = await asyncio.gather(
        history_task, enum_task, fault_task, gaps_task
    )
    # Коды неисправностей (40012/40013) — из постоянного enum_history, не из
    # тающего history_rich (ретенция ~30 дней). Пайплайн ниже не меняется.
    history = analytics_source.apply_fault_code_source_swap(history, enum_periods)
    return history, enum_periods, fault_periods, gaps


def _error_result(error: str, duration_ms: int) -> dict[str, Any]:
    return {
        "run_id": None,
        "segments": [],
        "segments_count": 0,
        "detections_count": 0,
        "max_severity": None,
        "data_quality_avg": 0.0,
        "duration_ms": duration_ms,
        "error": error,
        "report_md": f"# Ошибка аналитического прогона\n\n```\n{error}\n```\n",
    }
