# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""CRUD для таблицы segment_analyses (Этап 2)."""
from __future__ import annotations
import json
import logging
from typing import Any

import asyncpg

from config import settings

logger = logging.getLogger(__name__)


async def _connect():
    """Соединение из общего пула (conn.close() вернёт его в пул)."""
    from db.pool import acquire_analytics
    return await acquire_analytics()


# ── CRUD ─────────────────────────────────────────────────────────────────────

async def upsert_analysis(auto_segment_id: int, data: dict[str, Any]) -> None:
    """INSERT или UPDATE записи анализа сегмента (одна строка на сегмент)."""
    debug_json = data.get("debug_json")
    debug_str = (
        json.dumps(debug_json, ensure_ascii=False, default=str)
        if debug_json is not None else None
    )

    conn = await _connect()
    try:
        await conn.execute(
            """
            INSERT INTO segment_analyses (
                auto_segment_id, status,
                conclusion_md, humanized_md,
                verdict, alarm_level,
                claude_model, analytics_version,
                tokens_used, tool_calls_count, loops_count,
                generation_time_sec,
                debug_json, error,
                updated_at
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13::jsonb,$14,now())
            ON CONFLICT (auto_segment_id) DO UPDATE SET
                status              = EXCLUDED.status,
                conclusion_md       = EXCLUDED.conclusion_md,
                humanized_md        = EXCLUDED.humanized_md,
                verdict             = EXCLUDED.verdict,
                alarm_level         = EXCLUDED.alarm_level,
                claude_model        = EXCLUDED.claude_model,
                analytics_version   = EXCLUDED.analytics_version,
                tokens_used         = EXCLUDED.tokens_used,
                tool_calls_count    = EXCLUDED.tool_calls_count,
                loops_count         = EXCLUDED.loops_count,
                generation_time_sec = EXCLUDED.generation_time_sec,
                debug_json          = EXCLUDED.debug_json,
                error               = EXCLUDED.error,
                updated_at          = now()
            """,
            auto_segment_id,
            data.get("status", "done"),
            data.get("conclusion_md"),
            data.get("humanized_md"),
            data.get("verdict"),
            data.get("alarm_level"),
            data.get("claude_model"),
            data.get("analytics_version"),
            data.get("tokens_used"),
            data.get("tool_calls_count"),
            data.get("loops_count"),
            data.get("generation_time_sec"),
            debug_str,
            data.get("error"),
        )
    finally:
        await conn.close()


async def set_status(
    auto_segment_id: int,
    status: str,
    error: str | None = None,
) -> None:
    """Быстрое обновление только статуса (для очереди/воркера)."""
    conn = await _connect()
    try:
        await conn.execute(
            """
            INSERT INTO segment_analyses (auto_segment_id, status, error, updated_at)
            VALUES ($1, $2, $3, now())
            ON CONFLICT (auto_segment_id) DO UPDATE SET
                status     = EXCLUDED.status,
                error      = EXCLUDED.error,
                updated_at = now()
            """,
            auto_segment_id, status, error,
        )
    finally:
        await conn.close()


async def get_analysis(auto_segment_id: int) -> dict | None:
    """Получить запись анализа по ID сегмента."""
    conn = await _connect()
    try:
        row = await conn.fetchrow(
            "SELECT * FROM segment_analyses WHERE auto_segment_id = $1",
            auto_segment_id,
        )
    finally:
        await conn.close()
    return dict(row) if row else None


async def get_unanalyzed_segments(limit: int = 500) -> list[int]:
    """ID закрытых сегментов без анализа — для batch-прогона при старте."""
    conn = await _connect()
    try:
        rows = await conn.fetch(
            """
            SELECT a.id
            FROM auto_segments a
            LEFT JOIN segment_analyses sa ON sa.auto_segment_id = a.id
            WHERE a.t_end IS NOT NULL
              AND sa.id IS NULL
            ORDER BY a.t_start DESC
            LIMIT $1
            """,
            limit,
        )
    finally:
        await conn.close()
    return [r["id"] for r in rows]


async def get_unhumanized_segments(limit: int = 500) -> list[int]:
    """ID сегментов с готовым анализом Claude, но без Qwen-обработки."""
    conn = await _connect()
    try:
        rows = await conn.fetch(
            """
            SELECT auto_segment_id
            FROM segment_analyses
            WHERE status = 'done'
              AND conclusion_md IS NOT NULL
              AND (humanized_md IS NULL OR humanized_md = '')
            ORDER BY updated_at DESC
            LIMIT $1
            """,
            limit,
        )
    finally:
        await conn.close()
    return [r["auto_segment_id"] for r in rows]


async def set_humanized_md(auto_segment_id: int, humanized_md: str) -> None:
    """Сохранить результат Qwen-обработки в существующую запись анализа."""
    conn = await _connect()
    try:
        await conn.execute(
            """
            UPDATE segment_analyses
            SET humanized_md = $2, updated_at = now()
            WHERE auto_segment_id = $1
            """,
            auto_segment_id, humanized_md,
        )
    finally:
        await conn.close()


async def get_segment_row(seg_id: int) -> dict | None:
    """Загрузить строку auto_segments с полями, необходимыми для анализа."""
    conn = await _connect()
    try:
        row = await conn.fetchrow(
            """
            SELECT id, router_sn, equip_type, panel_id,
                   run_state, characteristics_json, report_md,
                   t_start, t_end
            FROM auto_segments
            WHERE id = $1
            """,
            seg_id,
        )
    finally:
        await conn.close()
    return dict(row) if row else None


async def get_analyses_for_segments(seg_ids: list[int]) -> dict[int, dict]:
    """Загрузить статусы анализа для списка сегментов одним запросом (для календаря)."""
    if not seg_ids:
        return {}
    conn = await _connect()
    try:
        rows = await conn.fetch(
            """SELECT auto_segment_id, status, verdict, alarm_level,
                      (humanized_md IS NOT NULL AND humanized_md <> '') AS has_qwen
               FROM segment_analyses
               WHERE auto_segment_id = ANY($1)""",
            seg_ids,
        )
    finally:
        await conn.close()
    return {r["auto_segment_id"]: dict(r) for r in rows}


async def get_equipment_kb_path(
    router_sn: str, equip_type: str, panel_id: int
) -> str | None:
    """Получить kb_path для оборудования из equipment_registry."""
    conn = await _connect()
    try:
        row = await conn.fetchrow(
            """
            SELECT kb_path FROM equipment_registry
            WHERE router_sn = $1 AND equip_type = $2 AND panel_id = $3
            """,
            router_sn, equip_type, panel_id,
        )
    finally:
        await conn.close()
    return row["kb_path"] if row else None


async def clear_all_analyses() -> int:
    """Удалить все записи анализа (Claude + Qwen). Возвращает число удалённых строк."""
    conn = await _connect()
    try:
        result = await conn.execute("DELETE FROM segment_analyses")
        # asyncpg возвращает строку вида "DELETE N"
        return int(result.split()[-1])
    finally:
        await conn.close()


async def clear_all_humanized() -> int:
    """Обнулить humanized_md во всех записях (сброс Qwen-анализа)."""
    conn = await _connect()
    try:
        result = await conn.execute(
            "UPDATE segment_analyses SET humanized_md = NULL, updated_at = now()"
            " WHERE humanized_md IS NOT NULL"
        )
        return int(result.split()[-1])
    finally:
        await conn.close()
