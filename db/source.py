"""Чтение данных из основной БД телеметрии (TimescaleDB)."""
import asyncpg
from datetime import date, datetime, timedelta, timezone
from typing import Any

from config import settings, get_tz


async def _connect() -> asyncpg.Connection:
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


async def get_daily_history(
    router_sn: str,
    equip_type: str,
    panel_id: int,
    day: date,
) -> list[dict[str, Any]]:
    """История телеметрии за сутки.

    Границы суток считаются в настроенном часовом поясе (по умолчанию МСК UTC+3),
    затем переводятся в UTC для запроса к БД.
    """
    start = datetime(day.year, day.month, day.day, tzinfo=get_tz()).astimezone(timezone.utc)
    end = start + timedelta(days=1)

    conn = await _connect()
    try:
        rows = await conn.fetch("""
            SELECT addr, ts, value, raw, text, reason, write_reason
            FROM history
            WHERE router_sn = $1
              AND equip_type = $2
              AND panel_id   = $3
              AND ts >= $4
              AND ts <  $5
            ORDER BY addr, ts
        """, router_sn, equip_type, panel_id, start, end)
        return [dict(r) for r in rows]
    finally:
        await conn.close()


async def get_period_history(
    router_sn: str,
    equip_type: str,
    panel_id: int,
    addr: int,
    ts_from: datetime,
    ts_to: datetime,
) -> list[dict[str, Any]]:
    """История одного регистра за произвольный период (для agent tools)."""
    conn = await _connect()
    try:
        rows = await conn.fetch("""
            SELECT ts, value, raw, text
            FROM history
            WHERE router_sn = $1
              AND equip_type = $2
              AND panel_id   = $3
              AND addr       = $4
              AND ts >= $5
              AND ts <  $6
            ORDER BY ts
        """, router_sn, equip_type, panel_id, addr, ts_from, ts_to)
        return [dict(r) for r in rows]
    finally:
        await conn.close()


async def get_daily_state_events(
    router_sn: str,
    equip_type: str,
    panel_id: int,
    day: date,
) -> list[dict[str, Any]]:
    """Смены состояния enum/discrete регистров за сутки из state_events.

    Границы суток — в настроенном часовом поясе (по умолчанию МСК UTC+3).
    """
    start = datetime(day.year, day.month, day.day, tzinfo=get_tz()).astimezone(timezone.utc)
    end = start + timedelta(days=1)

    conn = await _connect()
    try:
        rows = await conn.fetch("""
            SELECT addr, ts, received_at, raw, text, write_reason
            FROM state_events
            WHERE router_sn = $1
              AND equip_type = $2
              AND panel_id   = $3
              AND ts >= $4
              AND ts <  $5
            ORDER BY ts
        """, router_sn, equip_type, panel_id, start, end)
        return [dict(r) for r in rows]
    finally:
        await conn.close()


async def get_daily_events(
    router_sn: str,
    equip_type: str,
    panel_id: int,
    day: date,
) -> list[dict[str, Any]]:
    """События за сутки из таблицы events.

    Границы суток — в настроенном часовом поясе (по умолчанию МСК UTC+3).
    """
    start = datetime(day.year, day.month, day.day, tzinfo=get_tz()).astimezone(timezone.utc)
    end = start + timedelta(days=1)

    conn = await _connect()
    try:
        rows = await conn.fetch("""
            SELECT id, type, description, payload, created_at
            FROM events
            WHERE router_sn = $1
              AND (equip_type = $2 OR equip_type IS NULL)
              AND (panel_id   = $3 OR panel_id   IS NULL)
              AND created_at >= $4
              AND created_at <  $5
            ORDER BY created_at
        """, router_sn, equip_type, panel_id, start, end)
        return [dict(r) for r in rows]
    finally:
        await conn.close()
