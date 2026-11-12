# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Расход ИИ: сводки журнала обращений к моделям и его хранение (v5.2.7, MCP Э2б;
docs/plans/design_mcp_e2.md, разд. 6).

Журнал `llm_calls` (одна строка — один вызов модели) хранится полностью 90 дней;
старше — сворачивается в суточные итоги `llm_calls_daily` (сутки — в поясе сервиса)
и удаляется одной командой: либо свёрнуто и удалено, либо ничего. Сводка для
карточки «Расход ИИ» читает обе таблицы и журнал инструментов `mcp_calls`.

Чего провайдер не отдал — NULL и в итогах (сумма NULL — NULL, а не 0): «нет данных»
не превращается в «ноль токенов». Стоимость есть только у вызовов, чья запись
реестра имела цены на момент вызова; рядом — сколько вызовов оценено.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

logger = logging.getLogger(__name__)

KEEP_DAYS = 90            # столько суток журнал хранится построчно
MAX_SUMMARY_DAYS = 365
QUERY_TIMEOUT_SEC = 20.0
ROLLUP_TIMEOUT_SEC = 120.0

# Числовые колонки, которые суммируются (в суточных итогах — те же имена)
SUM_COLUMNS: tuple[str, ...] = (
    "tokens_in", "tokens_out", "tokens_cache_read", "tokens_cache_write",
    "tokens_reasoning", "tool_calls", "duration_ms", "cost_usd",
)
FAIL_STATUSES = ("error", "empty")


async def _connect():
    from db.pool import acquire_analytics
    return await acquire_analytics()


def _tz():
    from config import get_tz
    return get_tz()


def _missing(exc: Exception) -> bool:
    """Таблиц журнала ещё нет (сервис не перезапущен после обновления схемы)."""
    import asyncpg
    return isinstance(exc, asyncpg.UndefinedTableError)


def _plain(v: Any) -> Any:
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    return v


def _rows(rows) -> list[dict[str, Any]]:
    return [{k: _plain(v) for k, v in dict(r).items()} for r in rows]


def _fails() -> str:
    return ", ".join(f"'{s}'" for s in FAIL_STATUSES)


# ── Хранение ─────────────────────────────────────────────────────────────────

def _merge(col: str) -> str:
    """Сложение итога с добавкой, где NULL + NULL остаётся NULL."""
    return (f"{col} = CASE WHEN d.{col} IS NULL AND EXCLUDED.{col} IS NULL THEN NULL "
            f"ELSE COALESCE(d.{col}, 0) + COALESCE(EXCLUDED.{col}, 0) END")


def rollup_sql() -> str:
    sums = ", ".join(f"sum({c}) AS {c}" for c in SUM_COLUMNS)
    cols = ", ".join(SUM_COLUMNS)
    merges = ", ".join(_merge(c) for c in SUM_COLUMNS)
    return f"""
        WITH old AS (
            DELETE FROM llm_calls WHERE ts < $1 RETURNING *
        ), agg AS (
            SELECT (ts AT TIME ZONE $2::text)::date AS day,
                   COALESCE(task_kind, '') AS task_kind,
                   COALESCE(provider, '')  AS provider,
                   COALESCE(model, '')     AS model,
                   count(*)::int AS calls,
                   (count(*) FILTER (WHERE status IN ({_fails()})))::int AS errors,
                   (count(*) FILTER (WHERE cost_usd IS NOT NULL))::int AS priced,
                   {sums}
            FROM old GROUP BY 1, 2, 3, 4
        ), ins AS (
            INSERT INTO llm_calls_daily AS d
                   (day, task_kind, provider, model, calls, errors, priced, {cols})
            SELECT day, task_kind, provider, model, calls, errors, priced, {cols} FROM agg
            ON CONFLICT (day, task_kind, provider, model) DO UPDATE SET
                calls  = d.calls + EXCLUDED.calls,
                errors = d.errors + EXCLUDED.errors,
                priced = d.priced + EXCLUDED.priced,
                {merges}
            RETURNING 1
        )
        SELECT (SELECT count(*) FROM old)::int AS removed,
               (SELECT count(*) FROM ins)::int AS days
    """


def cutoff_for(keep_days: int, now: datetime | None = None) -> datetime:
    """Граница хранения — начало суток пояса сервиса: сутки не делятся между
    построчным журналом и суточными итогами."""
    tz = _tz()
    now = now.astimezone(tz) if now is not None else datetime.now(tz)
    return datetime.combine(now.date() - timedelta(days=int(keep_days)), time.min, tzinfo=tz)


async def rollup_old(keep_days: int = KEEP_DAYS) -> dict[str, int]:
    """Свернуть строки старше keep_days суток в суточные итоги и удалить их.
    Не бросает: сбой — предупреждение, журнал остаётся как был."""
    cutoff = cutoff_for(keep_days)

    async def _run() -> dict[str, int]:
        conn = await _connect()
        try:
            row = await conn.fetchrow(rollup_sql(), cutoff, str(_tz()))
            return {"removed": int(row["removed"]), "days": int(row["days"])}
        finally:
            await conn.close()

    try:
        out = await asyncio.wait_for(_run(), ROLLUP_TIMEOUT_SEC)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        if not _missing(exc):
            logger.warning("Журнал llm_calls: свёртка старше %d суток не выполнена: %r",
                           keep_days, exc)
        return {"removed": 0, "days": 0}
    if out["removed"]:
        logger.info("Журнал llm_calls: свёрнуто в суточные итоги и удалено %d строк (старше %s), "
                    "затронуто итогов: %d", out["removed"], cutoff.date().isoformat(), out["days"])
    return out


# ── Сводка для карточки «Расход ИИ» ──────────────────────────────────────────

def _union(group_raw: str, group_daily: str, day_param: str = "$3") -> str:
    """Построчный журнал и суточные итоги одним набором (сутки не пересекаются:
    свёрнутое из журнала удалено). day_param — номер параметра с первым днём."""
    nums = ", ".join(f"{c}::numeric AS {c}" for c in SUM_COLUMNS)
    return f"""
        SELECT {group_raw}, 1 AS calls, (COALESCE(status, '') IN ({_fails()}))::int AS errors,
               (cost_usd IS NOT NULL)::int AS priced, {nums}
        FROM llm_calls WHERE ts >= $1
        UNION ALL
        SELECT {group_daily}, calls, errors, priced, {nums}
        FROM llm_calls_daily WHERE day >= {day_param}
    """


def _sums() -> str:
    return ", ".join(
        f"sum({c})::float8 AS {c}" if c == "cost_usd" else f"sum({c})::bigint AS {c}"
        for c in SUM_COLUMNS)


def by_day_sql() -> str:
    src = _union("(ts AT TIME ZONE $2::text)::date AS day, COALESCE(provider, '') AS provider",
                 "day, provider")
    return f"""
        SELECT day, provider, sum(calls)::int AS calls, sum(errors)::int AS errors,
               sum(priced)::int AS priced, {_sums()}
        FROM ({src}) t GROUP BY 1, 2 ORDER BY 1 DESC, 2
    """


def by_task_sql() -> str:
    src = _union("COALESCE(task_kind, '') AS task_kind, COALESCE(provider, '') AS provider, "
                 "COALESCE(model, '') AS model",
                 "task_kind, provider, model", day_param="$2")
    return f"""
        SELECT task_kind, provider, model, sum(calls)::int AS calls, sum(errors)::int AS errors,
               sum(priced)::int AS priced, {_sums()}
        FROM ({src}) t GROUP BY 1, 2, 3 ORDER BY sum(calls) DESC, 1, 2, 3
    """


BY_TOOL_SQL = """
    SELECT tool, count(*)::int AS calls,
           (count(*) FILTER (WHERE status IS DISTINCT FROM 'ok'))::int AS errors,
           (count(*) FILTER (WHERE COALESCE(truncated, '') <> ''))::int AS truncated,
           avg(bytes)::float8 AS avg_bytes, max(bytes)::bigint AS max_bytes,
           sum(bytes)::bigint AS sum_bytes, avg(duration_ms)::float8 AS avg_ms
    FROM mcp_calls WHERE ts >= $1
    GROUP BY tool ORDER BY sum(bytes) DESC NULLS LAST, tool
"""

BY_SOURCE_SQL = """
    SELECT COALESCE(source, '') AS source, count(*)::int AS calls, sum(bytes)::bigint AS sum_bytes
    FROM mcp_calls WHERE ts >= $1
    GROUP BY 1 ORDER BY count(*) DESC, 1
"""


def cache_share(r: dict[str, Any]) -> float | None:
    """Доля входа, прочитанная из кэша: чтение кэша / весь вход (вход без кэша +
    чтение + запись). None — провайдер кэш не отдаёт или входа нет."""
    read = r.get("tokens_cache_read")
    if read is None:
        return None
    total = (r.get("tokens_in") or 0) + read + (r.get("tokens_cache_write") or 0)
    return round(read / total, 4) if total > 0 else None


def totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Итог по набору строк сводки; сумма только из NULL — None."""
    out: dict[str, Any] = {"calls": 0, "errors": 0, "priced": 0}
    for c in SUM_COLUMNS:
        vals = [r[c] for r in rows if r.get(c) is not None]
        out[c] = (round(sum(vals), 6) if c == "cost_usd" else int(sum(vals))) if vals else None
    for r in rows:
        for k in ("calls", "errors", "priced"):
            out[k] += int(r.get(k) or 0)
    out["cache_share"] = cache_share(out)
    return out


async def summary(days: int = 7) -> dict[str, Any]:
    """Сводка расхода за последние days суток (включая сегодня), сутки — в поясе сервиса."""
    days = max(1, min(MAX_SUMMARY_DAYS, int(days)))
    tz = _tz()
    day_from = datetime.now(tz).date() - timedelta(days=days - 1)
    ts_from = datetime.combine(day_from, time.min, tzinfo=tz)
    tzname = str(tz)

    async def _run() -> dict[str, Any]:
        conn = await _connect()
        try:
            try:
                by_day = _rows(await conn.fetch(by_day_sql(), ts_from, tzname, day_from))
                by_task = _rows(await conn.fetch(by_task_sql(), ts_from, day_from))
                journal = True
            except Exception as exc:  # noqa: BLE001
                if not _missing(exc):
                    raise
                by_day, by_task, journal = [], [], False
            try:
                by_tool = _rows(await conn.fetch(BY_TOOL_SQL, ts_from))
                by_source = _rows(await conn.fetch(BY_SOURCE_SQL, ts_from))
            except Exception as exc:  # noqa: BLE001
                if not _missing(exc):
                    raise
                by_tool, by_source = [], []
            return {"by_day": by_day, "by_task": by_task, "by_tool": by_tool,
                    "by_source": by_source, "journal": journal}
        finally:
            await conn.close()

    data = await asyncio.wait_for(_run(), QUERY_TIMEOUT_SEC)
    for r in data["by_day"] + data["by_task"]:
        r["cache_share"] = cache_share(r)
    return {
        "days": days, "from": day_from.isoformat(), "tz": tzname, "keep_days": KEEP_DAYS,
        "totals": totals(data["by_day"]), **data,
    }
