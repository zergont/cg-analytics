# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

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
    """Создание таблиц при первом запуске (применяет schema.sql + online_schema.sql)."""
    import pathlib
    db_dir = pathlib.Path(__file__).parent

    conn = await _connect()
    try:
        for schema_file in ("schema.sql", "online_schema.sql", "corpus_schema.sql"):
            schema_path = db_dir / schema_file
            if schema_path.exists():
                sql = schema_path.read_text(encoding="utf-8")
                await conn.execute(sql)
        logger.info("Схема аналитической БД применена (schema + online + corpus).")
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


async def delete_analysis_run(run_id: str) -> bool:
    """Удалить прогон по UUID. Возвращает True если запись была найдена и удалена."""
    conn = await _connect()
    try:
        result = await conn.execute("DELETE FROM analysis_runs WHERE id = $1", run_id)
        return result == "DELETE 1"
    finally:
        await conn.close()


async def list_equipment_with_runs() -> list[dict[str, Any]]:
    """Список оборудования у которого есть прогоны, с кратким резюме."""
    conn = await _connect()
    try:
        rows = await conn.fetch("""
            SELECT
                r.router_sn, r.equip_type, r.panel_id,
                e.name,
                COUNT(*)                        AS runs_count,
                MAX(r.created_at)               AS last_run_at,
                MAX(r.ts_to)                    AS last_period_to,
                SUM(CASE WHEN r.detections_count > 0 THEN 1 ELSE 0 END) AS runs_with_detections
            FROM analysis_runs r
            LEFT JOIN equipment_registry e
                ON e.router_sn = r.router_sn
               AND e.equip_type = r.equip_type
               AND e.panel_id   = r.panel_id
            GROUP BY r.router_sn, r.equip_type, r.panel_id, e.name
            ORDER BY MAX(r.created_at) DESC
        """)
        return [dict(row) for row in rows]
    finally:
        await conn.close()


# ── Здоровье БД ───────────────────────────────────────────────────────────────

def _fmt_bytes(n: int) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} ТБ"


async def get_db_health_stats() -> dict:
    """Статистика аналитической БД: размеры, синхронизация history."""
    conn = await _connect()
    try:
        db_size_bytes = await conn.fetchval(
            "SELECT pg_database_size(current_database())"
        )

        table_names = [
            "history", "enum_history", "fault_history", "events", "data_gaps",
            "register_catalog", "objects", "equipment", "parameter_history",
            "auto_segments", "online_observations", "history_sync_state",
        ]
        tables = []
        for t in table_names:
            try:
                size_bytes = await conn.fetchval(
                    "SELECT pg_total_relation_size($1::regclass)", t
                )
                size_pretty = await conn.fetchval(
                    "SELECT pg_size_pretty(pg_total_relation_size($1::regclass))", t
                )
                row_count = await conn.fetchval(f"SELECT COUNT(*) FROM {t}")
                tables.append({
                    "name": t,
                    "size_bytes": size_bytes,
                    "size_pretty": size_pretty,
                    "row_count": row_count,
                })
            except Exception:
                pass

        sync_rows = await conn.fetch("""
            SELECT hs.router_sn, hs.equip_type, hs.panel_id, hs.last_sync_at,
                   EXTRACT(EPOCH FROM (now() - hs.last_sync_at))::int AS lag_sec,
                   er.name AS device_name
            FROM history_sync_state hs
            LEFT JOIN equipment_registry er
                ON  er.router_sn   = hs.router_sn
                AND er.equip_type  = hs.equip_type
                AND er.panel_id    = hs.panel_id
            ORDER BY hs.router_sn, hs.panel_id
        """)

        daily_rows = await conn.fetchval(
            "SELECT COUNT(*) FROM history WHERE received_at > now() - interval '24 hours'"
        )
        history_entry = next((t for t in tables if t["name"] == "history"), None)
        if history_entry and history_entry["row_count"] and history_entry["row_count"] > 0:
            avg_row = history_entry["size_bytes"] / history_entry["row_count"]
        else:
            avg_row = 0
        daily_mb  = round(daily_rows * avg_row / 1024 / 1024, 1)
        monthly_mb = round(daily_mb * 30, 0)

        return {
            "db_size_bytes": db_size_bytes,
            "db_size_pretty": _fmt_bytes(db_size_bytes),
            "tables": tables,
            "sync_state": [dict(r) for r in sync_rows],
            "daily_rows": daily_rows,
            "daily_mb": daily_mb,
            "monthly_mb": int(monthly_mb),
        }
    finally:
        await conn.close()
