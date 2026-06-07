"""Чтение данных из основной БД телеметрии (TimescaleDB)."""
import asyncpg
from datetime import datetime
from typing import Any

from config import settings

# "external" → settings.source_db_url (10.10.10.1)
# "local"    → settings.analytics_db_url (локальная реплика)
_source_mode: str = "external"


def set_source_mode(mode: str) -> None:
    global _source_mode
    _source_mode = mode


def get_source_mode() -> str:
    return _source_mode


async def _connect() -> asyncpg.Connection:
    if _source_mode == "local":
        return await asyncpg.connect(settings.analytics_db_url)
    return await asyncpg.connect(settings.source_db_url)


async def get_active_equipment() -> list[dict[str, Any]]:
    """Всё оборудование из основной БД для синхронизации реестра."""
    conn = await _connect()
    try:
        rows = await conn.fetch("""
            SELECT
                router_sn, equip_type, panel_id,
                name, manufacturer, model, engine_sn,
                last_seen_at
            FROM equipment
            ORDER BY router_sn, equip_type, panel_id
        """)
        return [dict(r) for r in rows]
    finally:
        await conn.close()


async def get_equipment_info(
    router_sn: str, equip_type: str, panel_id: int
) -> dict[str, Any] | None:
    """Метаданные одного устройства."""
    conn = await _connect()
    try:
        row = await conn.fetchrow("""
            SELECT router_sn, equip_type, panel_id,
                   name, manufacturer, model, engine_sn,
                   first_seen_at, last_seen_at
            FROM equipment
            WHERE router_sn = $1 AND equip_type = $2 AND panel_id = $3
        """, router_sn, equip_type, panel_id)
        return dict(row) if row else None
    finally:
        await conn.close()


async def get_history_range(
    router_sn: str,
    equip_type: str,
    panel_id: int,
    ts_from: datetime,
    ts_to: datetime,
) -> list[dict[str, Any]]:
    """Аналоговые регистры за произвольный UTC-диапазон [ts_from, ts_to).

    Использует history_rich (history LEFT JOIN register_catalog).
    Фильтр: register_kind = 'analog'.
    Колонки: addr, ts, value, raw, name_ru, unit.
    """
    conn = await _connect()
    try:
        rows = await conn.fetch("""
            SELECT addr, ts, value, raw, name_ru, unit
            FROM history_rich
            WHERE router_sn     = $1
              AND equip_type    = $2
              AND panel_id      = $3
              AND register_kind = 'analog'
              AND ts >= $4
              AND ts <  $5
            ORDER BY addr, ts
        """, router_sn, equip_type, panel_id, ts_from, ts_to)
        return [dict(r) for r in rows]
    finally:
        await conn.close()


async def get_enum_history_range(
    router_sn: str,
    equip_type: str,
    panel_id: int,
    ts_from: datetime,
    ts_to: datetime,
) -> list[dict[str, Any]]:
    """Периоды enum-состояний, пересекающиеся с диапазоном [ts_from, ts_to).

    Возвращает периоды, у которых state_start < ts_to И (state_end IS NULL OR state_end > ts_from).
    Колонки: addr, name_ru, state_start, state_end, value, label, duration_sec.
    duration_sec считается до ts_to для незакрытых (активных) периодов.
    state_end IS NULL — состояние активно прямо сейчас.
    """
    conn = await _connect()
    try:
        rows = await conn.fetch("""
            SELECT
                e.addr,
                r.name_ru,
                e.state_start,
                e.state_end,
                e.value,
                COALESCE(
                    r.states_json->'labels_ru'->>e.value::text,
                    r.states_json->'labels'   ->>e.value::text,
                    e.value::text
                ) AS label,
                EXTRACT(EPOCH FROM (
                    COALESCE(e.state_end, $5) - e.state_start
                ))::int AS duration_sec
            FROM enum_history e
            LEFT JOIN register_catalog r
                ON r.equip_type = e.equip_type AND r.addr = e.addr
            WHERE e.router_sn  = $1
              AND e.equip_type = $2
              AND e.panel_id   = $3
              AND e.state_start < $5
              AND (e.state_end IS NULL OR e.state_end > $4)
            ORDER BY e.addr, e.state_start
        """, router_sn, equip_type, panel_id, ts_from, ts_to)
        return [dict(r) for r in rows]
    finally:
        await conn.close()


async def get_fault_history_range(
    router_sn: str,
    equip_type: str,
    panel_id: int,
    ts_from: datetime,
    ts_to: datetime,
) -> list[dict[str, Any]]:
    """Периоды fault-битов, пересекающиеся с диапазоном [ts_from, ts_to).

    Возвращает периоды, у которых fault_start < ts_to И (fault_end IS NULL OR fault_end > ts_from).
    Колонки: addr, bit, fault_start, fault_end, fault_name_ru, fault_name, severity, duration_sec.
    fault_end IS NULL — неисправность активна прямо сейчас.
    """
    conn = await _connect()
    try:
        rows = await conn.fetch("""
            SELECT
                f.addr,
                f.bit,
                f.fault_start,
                f.fault_end,
                r.states_json->f.bit::text->>'name_ru'  AS fault_name_ru,
                r.states_json->f.bit::text->>'name'     AS fault_name,
                r.states_json->f.bit::text->>'severity' AS severity,
                EXTRACT(EPOCH FROM (
                    COALESCE(f.fault_end, $5) - f.fault_start
                ))::int AS duration_sec
            FROM fault_history f
            LEFT JOIN register_catalog r
                ON r.equip_type = f.equip_type AND r.addr = f.addr
            WHERE f.router_sn  = $1
              AND f.equip_type = $2
              AND f.panel_id   = $3
              AND f.fault_start < $5
              AND (f.fault_end IS NULL OR f.fault_end > $4)
            ORDER BY f.fault_start
        """, router_sn, equip_type, panel_id, ts_from, ts_to)
        return [dict(r) for r in rows]
    finally:
        await conn.close()


async def get_events_range(
    router_sn: str,
    equip_type: str,
    panel_id: int,
    ts_from: datetime,
    ts_to: datetime,
) -> list[dict[str, Any]]:
    """Системные события / аварии за произвольный UTC-диапазон."""
    conn = await _connect()
    try:
        rows = await conn.fetch("""
            SELECT id,
                   type        AS event_type,
                   description,
                   payload,
                   created_at  AS ts
            FROM events
            WHERE router_sn = $1
              AND (equip_type = $2 OR equip_type IS NULL)
              AND (panel_id   = $3 OR panel_id   IS NULL)
              AND created_at >= $4
              AND created_at <  $5
            ORDER BY created_at
        """, router_sn, equip_type, panel_id, ts_from, ts_to)
        return [dict(r) for r in rows]
    finally:
        await conn.close()
