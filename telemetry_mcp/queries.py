# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Чтение БД аналитики для инструментов сегментов и актов — то, чего нет в
online/db.py в нужном виде (список с признаками акта и числом разборов)."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any


async def _connect():
    from db.pool import acquire_analytics
    return await acquire_analytics()


def _j(v: Any) -> Any:
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return None
    return v


_SEG_COLS = """
    id, run_state, t_start, t_end, cause_close, split_reason, continued_from, continues_to,
    characteristics_json, report_summary_md,
    (incident_json IS NOT NULL) AS has_act,
    COALESCE(jsonb_array_length(warning_analyses), 0) AS n_analyses
"""


async def segments_in_window(router_sn: str, equip_type: str, panel_id: int,
                             t_from: datetime, t_to: datetime, limit: int = 400) -> list[dict[str, Any]]:
    """Сегменты (и открытый), пересекающие окно, по времени начала."""
    conn = await _connect()
    try:
        rows = await conn.fetch(f"""
            SELECT {_SEG_COLS}
            FROM auto_segments
            WHERE router_sn = $1 AND equip_type = $2 AND panel_id = $3
              AND t_start < $5 AND (t_end IS NULL OR t_end > $4)
            ORDER BY t_start ASC
            LIMIT $6
        """, router_sn, equip_type, panel_id, t_from, t_to, int(limit))
        out = []
        for r in rows:
            d = dict(r)
            d["characteristics_json"] = _j(d.get("characteristics_json"))
            out.append(d)
        return out
    finally:
        await conn.close()


async def segment_with_report(seg_id: int) -> dict[str, Any] | None:
    conn = await _connect()
    try:
        r = await conn.fetchrow(f"""
            SELECT {_SEG_COLS}, report_md, router_sn, equip_type, panel_id
            FROM auto_segments WHERE id = $1
        """, int(seg_id))
        if not r:
            return None
        d = dict(r)
        d["characteristics_json"] = _j(d.get("characteristics_json"))
        return d
    finally:
        await conn.close()


async def acts_in_window(router_sn: str, equip_type: str, panel_id: int,
                         t_from: datetime, t_to: datetime, limit: int = 50) -> list[dict[str, Any]]:
    """Сегменты с актом аварийного останова в окне (по времени начала стопа)."""
    conn = await _connect()
    try:
        rows = await conn.fetch("""
            SELECT id, run_state, t_start, t_end, incident_json, warning_analyses
            FROM auto_segments
            WHERE router_sn = $1 AND equip_type = $2 AND panel_id = $3
              AND incident_json IS NOT NULL
              AND t_start >= $4 AND t_start < $5
            ORDER BY t_start DESC
            LIMIT $6
        """, router_sn, equip_type, panel_id, t_from, t_to, int(limit))
        return [_act_row(r) for r in rows]
    finally:
        await conn.close()


async def act_by_id(seg_id: int) -> dict[str, Any] | None:
    conn = await _connect()
    try:
        r = await conn.fetchrow("""
            SELECT id, run_state, t_start, t_end, incident_json, warning_analyses,
                   router_sn, equip_type, panel_id
            FROM auto_segments WHERE id = $1 AND incident_json IS NOT NULL
        """, int(seg_id))
        return _act_row(r) if r else None
    finally:
        await conn.close()


def _act_row(r: Any) -> dict[str, Any]:
    d = dict(r)
    d["incident_json"] = _j(d.get("incident_json")) or {}
    wa = _j(d.get("warning_analyses"))
    d["warning_analyses"] = wa if isinstance(wa, list) else []
    return d


async def hourly_aggregates(router_sn: str, equip_type: str, panel_id: int, addr: int,
                            t_from: datetime, t_to: datetime, limit: int = 2000) -> list[dict[str, Any]]:
    """Запасной путь долгого тренда: часовые агрегаты писателя (ПО ОТСЧЁТАМ).
    Колонка времени в агрегате — `ts` (в руководстве db-writer она звалась bucket)."""
    from analytics import source as _src
    from db import remote
    async with remote.connection(_src._get_pool()) as conn:
        rows = await conn.fetch("""
            SELECT ts AS bucket, avg_value, min_value, max_value, sample_count
            FROM history_1hour
            WHERE router_sn = $1 AND equip_type = $2 AND panel_id = $3 AND addr = $4
              AND ts >= $5 AND ts < $6
            ORDER BY ts LIMIT $7
        """, router_sn, equip_type, panel_id, int(addr), t_from, t_to, int(limit))
    return [dict(r) for r in rows]
