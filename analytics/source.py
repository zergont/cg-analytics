"""Чтение данных из основной БД телеметрии для аналитического блока.

Читает ТОЛЬКО whitelist-регистры (ТЗ, раздел 2.3):
- аналоговые → history_rich (addr IN whitelist_analog)
- enum-периоды → enum_history (addr IN [40011, 40010])
- fault-периоды → fault_history (addr IN 40400-40415)
- пропуски связи → data_gaps
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import asyncpg

from config import settings

_pool: asyncpg.Pool | None = None
_QUERY_TIMEOUT_SEC = 30


async def init_source_pool() -> None:
    global _pool
    _pool = await asyncpg.create_pool(
        settings.source_db_url,
        min_size=1,
        max_size=3,
        command_timeout=_QUERY_TIMEOUT_SEC,
    )


async def close_source_pool() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


def _get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Пул соединений с БД не инициализирован")
    return _pool


async def get_whitelist_history(
    router_sn: str,
    equip_type: str,
    panel_id: int,
    ts_from: datetime,
    ts_to: datetime,
    whitelist_addrs: frozenset[int],
) -> list[dict[str, Any]]:
    """Аналоговые регистры из whitelist за период [ts_from, ts_to).

    Возвращает строки отсортированные по ts ASC (для causal-обработки).
    Колонки: addr, ts, value, raw, name_ru, unit.
    """
    if not whitelist_addrs:
        return []

    async with _get_pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT addr, ts, value, raw, name_ru, unit
            FROM history_rich
            WHERE router_sn  = $1
              AND equip_type = $2
              AND panel_id   = $3
              AND addr       = ANY($4::int[])
              AND ts >= $5
              AND ts <  $6
            ORDER BY ts ASC, addr ASC
            """,
            router_sn, equip_type, panel_id,
            list(whitelist_addrs),
            ts_from, ts_to,
        )
    return [dict(r) for r in rows]


async def get_enum_periods(
    router_sn: str,
    equip_type: str,
    panel_id: int,
    ts_from: datetime,
    ts_to: datetime,
    addrs: list[int] | None = None,
) -> list[dict[str, Any]]:
    """Периоды enum-состояний из enum_history, пересекающиеся с [ts_from, ts_to).

    По умолчанию — только 40011 (RUN_STATE) и 40010 (SWITCH_POS).
    Колонки: addr, state_start, state_end, value, label.
    state_end IS NULL → период активен прямо сейчас.
    """
    if addrs is None:
        addrs = [40011, 40010]

    async with _get_pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                e.addr,
                e.state_start,
                e.state_end,
                e.value,
                COALESCE(
                    r.states_json->'labels_ru'->>e.value::text,
                    r.states_json->'labels'   ->>e.value::text,
                    e.value::text
                ) AS label
            FROM enum_history e
            LEFT JOIN register_catalog r
                ON r.equip_type = e.equip_type AND r.addr = e.addr
            WHERE e.router_sn  = $1
              AND e.equip_type = $2
              AND e.panel_id   = $3
              AND e.addr       = ANY($4::int[])
              AND e.state_start < $6
              AND (e.state_end IS NULL OR e.state_end > $5)
            ORDER BY e.addr ASC, e.state_start ASC
            """,
            router_sn, equip_type, panel_id,
            addrs,
            ts_from, ts_to,
        )
    return [dict(r) for r in rows]


async def get_fault_periods(
    router_sn: str,
    equip_type: str,
    panel_id: int,
    ts_from: datetime,
    ts_to: datetime,
    fault_addrs: frozenset[int] | None = None,
) -> list[dict[str, Any]]:
    """Периоды активных fault-битов из fault_history, пересекающиеся с [ts_from, ts_to).

    По умолчанию — адреса 40400-40415.
    Колонки: addr, bit, fault_start, fault_end, fault_name_ru, fault_name, severity, duration_sec.
    fault_end IS NULL → неисправность активна прямо сейчас.
    """
    if fault_addrs is None:
        fault_addrs = frozenset(range(40400, 40416))

    async with _get_pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                f.addr,
                f.bit,
                f.fault_start,
                f.fault_end,
                r.states_json->f.bit::text->>'name_ru'  AS fault_name_ru,
                r.states_json->f.bit::text->>'name'     AS fault_name,
                r.states_json->f.bit::text->>'severity' AS severity,
                EXTRACT(EPOCH FROM (
                    COALESCE(f.fault_end, $6) - f.fault_start
                ))::int AS duration_sec
            FROM fault_history f
            LEFT JOIN register_catalog r
                ON r.equip_type = f.equip_type AND r.addr = f.addr
            WHERE f.router_sn  = $1
              AND f.equip_type = $2
              AND f.panel_id   = $3
              AND f.addr       = ANY($4::int[])
              AND f.fault_start < $6
              AND (f.fault_end IS NULL OR f.fault_end > $5)
            ORDER BY f.fault_start ASC
            """,
            router_sn, equip_type, panel_id,
            list(fault_addrs),
            ts_from, ts_to,
        )
    return [dict(r) for r in rows]


async def get_data_gaps(
    router_sn: str,
    equip_type: str,
    panel_id: int,
    ts_from: datetime,
    ts_to: datetime,
) -> list[dict[str, Any]]:
    """Пропуски связи из data_gaps, пересекающиеся с [ts_from, ts_to).

    Колонки: gap_start, gap_end (gap_end IS NULL → активен).
    При таймауте — пробрасывает RuntimeError с просьбой уменьшить интервал.
    При других ошибках (таблица недоступна и т.п.) возвращает [].
    """
    try:
        async with _get_pool().acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT gap_start, gap_end
                FROM data_gaps
                WHERE router_sn  = $1
                  AND equip_type = $2
                  AND panel_id   = $3
                  AND gap_start < $5
                  AND (gap_end IS NULL OR gap_end > $4)
                ORDER BY gap_start ASC
                """,
                router_sn, equip_type, panel_id,
                ts_from, ts_to,
            )
        return [dict(r) for r in rows]
    except asyncio.TimeoutError:
        raise RuntimeError(
            f"Запрос пропусков связи превысил {_QUERY_TIMEOUT_SEC} с. "
            "Уменьшите временной интервал анализа."
        )
    except Exception:
        return []
