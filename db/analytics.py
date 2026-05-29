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


# ── App settings ──────────────────────────────────────────────────────────────

async def get_app_setting(key: str, default: str = "") -> str:
    """Получить настройку из БД. Возвращает default если ключ не найден."""
    conn = await _connect()
    try:
        row = await conn.fetchrow("SELECT value FROM app_settings WHERE key = $1", key)
        return row["value"] if row else default
    finally:
        await conn.close()


async def set_app_setting(key: str, value: str) -> None:
    """Сохранить настройку в БД (upsert)."""
    conn = await _connect()
    try:
        await conn.execute("""
            INSERT INTO app_settings (key, value, updated_at)
            VALUES ($1, $2, now())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
        """, key, value)
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


async def get_equipment_kb_path(
    router_sn: str, equip_type: str, panel_id: int
) -> str | None:
    """Вернуть kb_path для конкретного устройства из реестра аналитики."""
    conn = await _connect()
    try:
        row = await conn.fetchrow("""
            SELECT kb_path FROM equipment_registry
            WHERE router_sn = $1 AND equip_type = $2 AND panel_id = $3
        """, router_sn, equip_type, panel_id)
        return row["kb_path"] if row else None
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


# ── Analysis runs (v2 analytics) ─────────────────────────────────────────────

async def save_analysis_run(run: dict[str, Any]) -> str:
    """Сохранить результат аналитического прогона. Возвращает UUID записи."""
    conn = await _connect()
    try:
        row = await conn.fetchrow("""
            INSERT INTO analysis_runs (
                router_sn, equip_type, panel_id, engine_sn,
                ts_from, ts_to, analytics_version,
                segments_json, report_md,
                segments_count, detections_count, max_severity, data_quality_avg,
                duration_ms, error
            ) VALUES (
                $1, $2, $3, $4,
                $5, $6, $7,
                $8::jsonb, $9,
                $10, $11, $12, $13,
                $14, $15
            )
            ON CONFLICT (router_sn, equip_type, panel_id, ts_from, ts_to) DO UPDATE SET
                engine_sn         = EXCLUDED.engine_sn,
                analytics_version = EXCLUDED.analytics_version,
                segments_json     = EXCLUDED.segments_json,
                report_md         = EXCLUDED.report_md,
                segments_count    = EXCLUDED.segments_count,
                detections_count  = EXCLUDED.detections_count,
                max_severity      = EXCLUDED.max_severity,
                data_quality_avg  = EXCLUDED.data_quality_avg,
                duration_ms       = EXCLUDED.duration_ms,
                error             = EXCLUDED.error,
                created_at        = now()
            RETURNING id
        """,
            run["router_sn"],
            run["equip_type"],
            run["panel_id"],
            run.get("engine_sn"),
            run["ts_from"],
            run["ts_to"],
            run.get("analytics_version", "2.0.0"),
            run.get("segments_json"),
            run.get("report_md"),
            run.get("segments_count"),
            run.get("detections_count"),
            run.get("max_severity"),
            run.get("data_quality_avg"),
            run.get("duration_ms"),
            run.get("error"),
        )
        return str(row["id"])
    finally:
        await conn.close()


async def get_analysis_run(run_id: str) -> dict[str, Any] | None:
    """Загрузить результат прогона по UUID."""
    conn = await _connect()
    try:
        row = await conn.fetchrow(
            "SELECT * FROM analysis_runs WHERE id = $1", run_id
        )
        if not row:
            return None
        r = dict(row)
        r["id"] = str(r["id"])
        return r
    finally:
        await conn.close()


async def get_analysis_run_for_period(
    router_sn: str,
    equip_type: str,
    panel_id: int,
    ts_from: Any,
    ts_to: Any,
) -> dict[str, Any] | None:
    """Найти прогон по ГУ и точному периоду."""
    conn = await _connect()
    try:
        row = await conn.fetchrow("""
            SELECT * FROM analysis_runs
            WHERE router_sn = $1 AND equip_type = $2 AND panel_id = $3
              AND ts_from = $4 AND ts_to = $5
            ORDER BY created_at DESC
            LIMIT 1
        """, router_sn, equip_type, panel_id, ts_from, ts_to)
        if not row:
            return None
        r = dict(row)
        r["id"] = str(r["id"])
        return r
    finally:
        await conn.close()


async def list_analysis_runs(
    router_sn: str,
    equip_type: str,
    panel_id: int,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Список прогонов для одной ГУ, от новых к старым."""
    conn = await _connect()
    try:
        rows = await conn.fetch("""
            SELECT id, ts_from, ts_to, analytics_version,
                   segments_count, detections_count, max_severity,
                   data_quality_avg, duration_ms, error, created_at
            FROM analysis_runs
            WHERE router_sn = $1 AND equip_type = $2 AND panel_id = $3
            ORDER BY created_at DESC
            LIMIT $4
        """, router_sn, equip_type, panel_id, limit)
        return [{**dict(r), "id": str(r["id"])} for r in rows]
    finally:
        await conn.close()
