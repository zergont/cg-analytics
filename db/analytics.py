"""Чтение и запись данных в аналитическую БД."""
import json
import logging
from datetime import date
from typing import Any
from uuid import UUID

import asyncpg

from config import settings

logger = logging.getLogger(__name__)


async def _connect() -> asyncpg.Connection:
    return await asyncpg.connect(settings.analytics_db_url)


async def init_db() -> None:
    """Создание таблиц при первом запуске (применяет schema.sql)."""
    schema = (settings.knowledge_base_path.parent / "db" / "schema.sql").resolve()
    # Путь относительно рабочей директории проекта
    import pathlib
    schema_path = pathlib.Path(__file__).parent / "schema.sql"
    sql = schema_path.read_text(encoding="utf-8")

    conn = await _connect()
    try:
        await conn.execute(sql)
        logger.info("Схема аналитической БД применена.")
    finally:
        await conn.close()


# ── Equipment registry ────────────────────────────────────────────────────────

async def upsert_equipment(equipment: dict[str, Any]) -> None:
    """Добавить или обновить запись об оборудовании (полная перезапись — для ручного редактирования)."""
    conn = await _connect()
    try:
        await conn.execute("""
            INSERT INTO equipment_registry
                (router_sn, equip_type, panel_id, name, manufacturer, model, engine_sn, kb_path, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, now())
            ON CONFLICT (router_sn, equip_type, panel_id) DO UPDATE SET
                name         = EXCLUDED.name,
                manufacturer = EXCLUDED.manufacturer,
                model        = EXCLUDED.model,
                engine_sn    = EXCLUDED.engine_sn,
                kb_path      = EXCLUDED.kb_path,
                updated_at   = now()
        """,
            equipment["router_sn"],
            equipment["equip_type"],
            equipment["panel_id"],
            equipment.get("name"),
            equipment.get("manufacturer"),
            equipment.get("model"),
            equipment.get("engine_sn"),
            equipment.get("kb_path"),
        )
    finally:
        await conn.close()


async def sync_equipment_from_source(equipment: dict[str, Any]) -> None:
    """Синхронизация из основной БД: добавляет новые записи и заполняет пустые поля.

    Не перезаписывает поля, которые уже заполнены в реестре аналитики —
    локальные правки сохраняются. Данные из источника применяются только
    если соответствующее поле в реестре равно NULL.
    """
    conn = await _connect()
    try:
        await conn.execute("""
            INSERT INTO equipment_registry
                (router_sn, equip_type, panel_id, name, manufacturer, model, engine_sn, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, now())
            ON CONFLICT (router_sn, equip_type, panel_id) DO UPDATE SET
                name         = COALESCE(equipment_registry.name,         EXCLUDED.name),
                manufacturer = COALESCE(equipment_registry.manufacturer, EXCLUDED.manufacturer),
                model        = COALESCE(equipment_registry.model,        EXCLUDED.model),
                engine_sn    = COALESCE(equipment_registry.engine_sn,    EXCLUDED.engine_sn),
                updated_at   = now()
        """,
            equipment["router_sn"],
            equipment["equip_type"],
            equipment["panel_id"],
            equipment.get("name"),
            equipment.get("manufacturer"),
            equipment.get("model"),
            equipment.get("engine_sn"),
        )
    finally:
        await conn.close()


async def get_equipment_registry() -> list[dict[str, Any]]:
    """Список всего оборудования из реестра аналитики."""
    conn = await _connect()
    try:
        rows = await conn.fetch("""
            SELECT router_sn, equip_type, panel_id,
                   name, manufacturer, model, engine_sn, kb_path,
                   active, created_at, updated_at
            FROM equipment_registry
            ORDER BY router_sn, equip_type, panel_id
        """)
        return [dict(r) for r in rows]
    finally:
        await conn.close()


async def delete_equipment(router_sn: str, equip_type: str, panel_id: int) -> None:
    """Удалить запись об оборудовании из реестра."""
    conn = await _connect()
    try:
        await conn.execute("""
            DELETE FROM equipment_registry
            WHERE router_sn = $1 AND equip_type = $2 AND panel_id = $3
        """, router_sn, equip_type, panel_id)
    finally:
        await conn.close()


async def clear_equipment_registry() -> int:
    """Очистить весь реестр оборудования. Возвращает число удалённых записей."""
    conn = await _connect()
    try:
        result = await conn.execute("DELETE FROM equipment_registry")
        # result вида "DELETE N"
        return int(result.split()[-1])
    finally:
        await conn.close()


async def set_equipment_active(
    router_sn: str, equip_type: str, panel_id: int, active: bool
) -> None:
    conn = await _connect()
    try:
        await conn.execute("""
            UPDATE equipment_registry
            SET active = $4, updated_at = now()
            WHERE router_sn = $1 AND equip_type = $2 AND panel_id = $3
        """, router_sn, equip_type, panel_id, active)
    finally:
        await conn.close()


# ── Daily reports ─────────────────────────────────────────────────────────────

async def save_report(report: dict[str, Any]) -> str:
    """Сохранить суточный отчёт. Возвращает UUID записи."""
    conn = await _connect()
    try:
        row = await conn.fetchrow("""
            INSERT INTO daily_reports (
                date, router_sn, equip_type, panel_id,
                manufacturer, model, engine_sn,
                status, uptime_minutes, starts_count,
                anomalies, aggregates,
                ai_report, ai_model, tokens_used,
                tool_calls_count, generation_time_sec
            ) VALUES (
                $1, $2, $3, $4,
                $5, $6, $7,
                $8, $9, $10,
                $11, $12,
                $13, $14, $15,
                $16, $17
            )
            ON CONFLICT (date, router_sn, equip_type, panel_id) DO UPDATE SET
                status              = EXCLUDED.status,
                uptime_minutes      = EXCLUDED.uptime_minutes,
                starts_count        = EXCLUDED.starts_count,
                anomalies           = EXCLUDED.anomalies,
                aggregates          = EXCLUDED.aggregates,
                ai_report           = EXCLUDED.ai_report,
                ai_model            = EXCLUDED.ai_model,
                tokens_used         = EXCLUDED.tokens_used,
                tool_calls_count    = EXCLUDED.tool_calls_count,
                generation_time_sec = EXCLUDED.generation_time_sec,
                created_at          = now()
            RETURNING id
        """,
            report["date"],
            report["router_sn"],
            report["equip_type"],
            report["panel_id"],
            report.get("manufacturer"),
            report.get("model"),
            report.get("engine_sn"),
            report["status"],
            report.get("uptime_minutes"),
            report.get("starts_count"),
            json.dumps(report.get("anomalies"), ensure_ascii=False),
            json.dumps(report.get("aggregates"), ensure_ascii=False),
            report.get("ai_report"),
            report.get("ai_model"),
            report.get("tokens_used"),
            report.get("tool_calls_count"),
            report.get("generation_time_sec"),
        )
        return str(row["id"])
    finally:
        await conn.close()


async def get_report(report_id: str) -> dict[str, Any] | None:
    conn = await _connect()
    try:
        row = await conn.fetchrow(
            "SELECT * FROM daily_reports WHERE id = $1", report_id
        )
        if not row:
            return None
        r = dict(row)
        r["id"] = str(r["id"])
        return r
    finally:
        await conn.close()


async def get_latest_reports(limit: int = 50) -> list[dict[str, Any]]:
    """Последние отчёты для главной страницы."""
    conn = await _connect()
    try:
        rows = await conn.fetch("""
            SELECT DISTINCT ON (router_sn, equip_type, panel_id)
                id, date, router_sn, equip_type, panel_id,
                manufacturer, model, engine_sn,
                status, uptime_minutes, starts_count,
                tool_calls_count, tokens_used, created_at
            FROM daily_reports
            ORDER BY router_sn, equip_type, panel_id, date DESC
            LIMIT $1
        """, limit)
        return [{**dict(r), "id": str(r["id"])} for r in rows]
    finally:
        await conn.close()


async def get_equipment_history(
    router_sn: str, equip_type: str, panel_id: int, limit: int = 90
) -> list[dict[str, Any]]:
    """История отчётов одной ГУ за последние N дней."""
    conn = await _connect()
    try:
        rows = await conn.fetch("""
            SELECT id, date, status, uptime_minutes, starts_count,
                   tokens_used, tool_calls_count, created_at
            FROM daily_reports
            WHERE router_sn = $1 AND equip_type = $2 AND panel_id = $3
            ORDER BY date DESC
            LIMIT $4
        """, router_sn, equip_type, panel_id, limit)
        return [{**dict(r), "id": str(r["id"])} for r in rows]
    finally:
        await conn.close()
