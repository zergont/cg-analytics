# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Журнал вызовов сервера (таблица mcp_calls, design_mcp_telemetry.md, разд. 8.3).

Один вызов — одна строка: сквозной task_id хоста, источник, инструмент, установка,
аргументы (без серийников), окно, сколько строк и байт отдано, усечение, что
отброшено, длительность, статус. Единственная запись сервера в БД аналитики;
её отказ не ломает ответ — только предупреждение в журнал процесса.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

_warned = False


async def log_call(
    *, tool: str, task_id: str | None, source: str | None, unit_id: int | None,
    args: dict[str, Any] | None, window_from: datetime | None, window_to: datetime | None,
    rows: int | None, nbytes: int | None, truncated: str | None,
    dropped: dict[str, Any] | None, duration_ms: int, status: str, error: str | None,
) -> None:
    global _warned
    try:
        from db.pool import acquire_analytics
        conn = await acquire_analytics()
        try:
            await conn.execute("""
                INSERT INTO mcp_calls (task_id, source, tool, unit_id, args_json, window_from,
                                       window_to, rows, bytes, truncated, dropped_json,
                                       duration_ms, status, error)
                VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9, $10, $11::jsonb, $12, $13, $14)
            """, task_id, source, tool, unit_id,
                 json.dumps(args or {}, ensure_ascii=False, default=str),
                 window_from, window_to, rows, nbytes, truncated,
                 json.dumps(dropped or {}, ensure_ascii=False, default=str) if dropped else None,
                 int(duration_ms), status, (error or None) and str(error)[:2000])
        finally:
            await conn.close()
    except Exception:
        if not _warned:
            _warned = True
            logger.warning("Журнал mcp_calls недоступен — вызовы не записываются "
                           "(таблицу создаёт cg-analytics при старте)", exc_info=True)
