# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""CRUD для онлайн-мониторинга: online_observations, auto_segments."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

import asyncpg

from config import settings

logger = logging.getLogger(__name__)


async def _connect():
    """Соединение из общего пула (conn.close() вернёт его в пул)."""
    from db.pool import acquire_analytics
    return await acquire_analytics()


async def init_online_schema() -> None:
    """Применить online_schema.sql к аналитической БД."""
    import pathlib
    schema_path = pathlib.Path(__file__).parent.parent / "db" / "online_schema.sql"
    sql = schema_path.read_text(encoding="utf-8")
    conn = await _connect()
    try:
        await conn.execute(sql)
        logger.info("Онлайн-схема применена.")
    finally:
        await conn.close()


# ── online_observations ───────────────────────────────────────────────────────

async def upsert_observation(data: dict[str, Any]) -> None:
    conn = await _connect()
    try:
        await conn.execute("""
            INSERT INTO online_observations
                (router_sn, equip_type, panel_id, start_date, status,
                 poll_interval_sec, batch_end_ts, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, now())
            ON CONFLICT (router_sn, equip_type, panel_id) DO UPDATE SET
                start_date        = EXCLUDED.start_date,
                status            = EXCLUDED.status,
                poll_interval_sec = EXCLUDED.poll_interval_sec,
                -- batch_end_ts фиксируется ОДИН РАЗ при первом старте,
                -- при повторном ПУСК (resume) не перезаписывается
                batch_end_ts      = COALESCE(online_observations.batch_end_ts,
                                             EXCLUDED.batch_end_ts),
                updated_at        = now()
        """,
            data["router_sn"], data["equip_type"], data["panel_id"],
            data["start_date"], data.get("status", "stopped"),
            data.get("poll_interval_sec", 30),
            data.get("batch_end_ts"),
        )
    finally:
        await conn.close()


async def get_observation(
    router_sn: str, equip_type: str, panel_id: int
) -> dict[str, Any] | None:
    conn = await _connect()
    try:
        row = await conn.fetchrow("""
            SELECT * FROM online_observations
            WHERE router_sn=$1 AND equip_type=$2 AND panel_id=$3
        """, router_sn, equip_type, panel_id)
        return dict(row) if row else None
    finally:
        await conn.close()


async def update_observation_last_data_ts(
    router_sn: str, equip_type: str, panel_id: int, ts: datetime
) -> None:
    """Обновить отметку свежести телеметрии (максимальный виденный ts history)."""
    conn = await _connect()
    try:
        await conn.execute("""
            UPDATE online_observations
            SET last_data_ts = $4
            WHERE router_sn = $1 AND equip_type = $2 AND panel_id = $3
        """, router_sn, equip_type, panel_id, ts)
    finally:
        await conn.close()


async def list_observations() -> list[dict[str, Any]]:
    conn = await _connect()
    try:
        rows = await conn.fetch("""
            SELECT o.*, e.name, e.manufacturer, e.model, e.engine_sn, e.kb_path,
                   e.controller_id, e.engine_id
            FROM online_observations o
            LEFT JOIN equipment_registry e
                ON e.router_sn = o.router_sn
               AND e.equip_type = o.equip_type
               AND e.panel_id   = o.panel_id
            ORDER BY o.router_sn, o.equip_type, o.panel_id
        """)
        return [dict(r) for r in rows]
    finally:
        await conn.close()


async def set_observation_status(
    router_sn: str, equip_type: str, panel_id: int, status: str
) -> None:
    conn = await _connect()
    try:
        await conn.execute("""
            UPDATE online_observations
            SET status = $4, updated_at = now()
            WHERE router_sn=$1 AND equip_type=$2 AND panel_id=$3
        """, router_sn, equip_type, panel_id, status)
    finally:
        await conn.close()


async def delete_observation(
    router_sn: str, equip_type: str, panel_id: int
) -> None:
    conn = await _connect()
    try:
        await conn.execute("""
            DELETE FROM online_observations
            WHERE router_sn=$1 AND equip_type=$2 AND panel_id=$3
        """, router_sn, equip_type, panel_id)
    finally:
        await conn.close()


# ── auto_segments ─────────────────────────────────────────────────────────────

async def get_last_closed_segment(
    router_sn: str, equip_type: str, panel_id: int
) -> dict[str, Any] | None:
    """Последний ЗАКРЫТЫЙ сегмент — точка возобновления при рестарте."""
    conn = await _connect()
    try:
        row = await conn.fetchrow("""
            SELECT * FROM auto_segments
            WHERE router_sn=$1 AND equip_type=$2 AND panel_id=$3
              AND t_end IS NOT NULL
            ORDER BY t_end DESC
            LIMIT 1
        """, router_sn, equip_type, panel_id)
        return dict(row) if row else None
    finally:
        await conn.close()


async def get_open_segments_all() -> dict[str, dict[str, Any]]:
    """Все открытые сегменты одним запросом: {"sn|type|panel": row}.

    Для роутов, отдающих статус всего парка (/api/machines и т.п.) —
    вместо get_open_segment по каждой машине в цикле.
    """
    conn = await _connect()
    try:
        rows = await conn.fetch("SELECT * FROM auto_segments WHERE t_end IS NULL")
        return {
            f"{r['router_sn']}|{r['equip_type']}|{r['panel_id']}": dict(r)
            for r in rows
        }
    finally:
        await conn.close()


async def get_open_segment(
    router_sn: str, equip_type: str, panel_id: int
) -> dict[str, Any] | None:
    conn = await _connect()
    try:
        row = await conn.fetchrow("""
            SELECT * FROM auto_segments
            WHERE router_sn=$1 AND equip_type=$2 AND panel_id=$3
              AND t_end IS NULL
        """, router_sn, equip_type, panel_id)
        return dict(row) if row else None
    finally:
        await conn.close()


async def upsert_open_segment(data: dict[str, Any]) -> int:
    """Создать или обновить открытый сегмент.

    Стратегия UPDATE + INSERT (вместо DELETE + INSERT):
    - Если открытый сегмент уже есть → UPDATE (status_text/hash/updated_at не трогаем).
    - Если нет → INSERT.

    Это устраняет гонку: qwen пишет prose в существующую строку, а UPDATE её не удаляет
    и не перезаписывает статус-поля, поэтому проза сохраняется.
    """
    conn = await _connect()
    try:
        async with conn.transaction():
            # Пытаемся обновить существующий открытый сегмент.
            # status_text / status_hash / status_updated_at намеренно НЕ обновляем —
            # они пишутся только через update_open_segment_status() (планировщик / qwen).
            row = await conn.fetchrow("""
                UPDATE auto_segments SET
                    t_start                = $4,
                    run_state              = $5,
                    coking_risk_json       = $6::jsonb,
                    analytics_version      = $7,
                    current_values_json    = $8::jsonb,
                    active_detections_json = $9::jsonb,
                    continued_from         = $10,
                    characteristics_json   = COALESCE($11::jsonb, characteristics_json),
                    report_md              = COALESCE($12, report_md),
                    report_summary_md      = COALESCE($13, report_summary_md),
                    updated_at             = now()
                WHERE router_sn=$1 AND equip_type=$2 AND panel_id=$3
                  AND t_end IS NULL
                RETURNING id
            """,
                data["router_sn"], data["equip_type"], data["panel_id"],
                data["t_start"],
                data.get("run_state"),
                json.dumps(data.get("coking_risk_json"), ensure_ascii=False),
                data.get("analytics_version", "2.2.0"),
                json.dumps(data.get("current_values_json"), ensure_ascii=False),
                json.dumps(data.get("active_detections_json"), ensure_ascii=False),
                data.get("continued_from"),
                json.dumps(data.get("characteristics_json"), ensure_ascii=False)
                    if data.get("characteristics_json") is not None else None,
                data.get("report_md"),
                data.get("report_summary_md"),
            )

            if row:
                return row["id"]

            # Открытого сегмента нет (первый цикл или после закрытия) → INSERT
            row = await conn.fetchrow("""
                INSERT INTO auto_segments (
                    router_sn, equip_type, panel_id,
                    t_start, t_end,
                    run_state, coking_risk_json,
                    analytics_version,
                    current_values_json, active_detections_json,
                    continued_from, characteristics_json, report_md,
                    report_summary_md, updated_at
                ) VALUES ($1,$2,$3,$4,NULL,$5,$6::jsonb,$7,$8::jsonb,$9::jsonb,$10,$11::jsonb,$12,$13,now())
                RETURNING id
            """,
                data["router_sn"], data["equip_type"], data["panel_id"],
                data["t_start"],
                data.get("run_state"),
                json.dumps(data.get("coking_risk_json"), ensure_ascii=False),
                data.get("analytics_version", "2.2.0"),
                json.dumps(data.get("current_values_json"), ensure_ascii=False),
                json.dumps(data.get("active_detections_json"), ensure_ascii=False),
                data.get("continued_from"),
                json.dumps(data.get("characteristics_json"), ensure_ascii=False)
                    if data.get("characteristics_json") is not None else None,
                data.get("report_md"),
                data.get("report_summary_md"),
            )
        return row["id"]
    finally:
        await conn.close()


async def insert_closed_segment(data: dict[str, Any]) -> int:
    """Вставить закрытый сегмент. При конфликте по t_start — обновить."""
    conn = await _connect()
    try:
        row = await conn.fetchrow("""
            INSERT INTO auto_segments (
                router_sn, equip_type, panel_id,
                t_start, t_end,
                run_state, cause_close, split_reason,
                continued_from,
                coking_risk_json, forward_fill_json,
                analytics_version,
                characteristics_json, report_md, report_summary_md,
                incident_json,
                updated_at
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,$11::jsonb,$12,$13::jsonb,$14,$15,$16::jsonb,now())
            ON CONFLICT (router_sn, equip_type, panel_id, t_start)
                WHERE t_end IS NOT NULL
            DO UPDATE SET
                t_end               = EXCLUDED.t_end,
                run_state           = EXCLUDED.run_state,
                cause_close         = EXCLUDED.cause_close,
                split_reason        = EXCLUDED.split_reason,
                coking_risk_json    = EXCLUDED.coking_risk_json,
                forward_fill_json   = EXCLUDED.forward_fill_json,
                analytics_version   = EXCLUDED.analytics_version,
                characteristics_json = EXCLUDED.characteristics_json,
                report_md           = EXCLUDED.report_md,
                report_summary_md   = EXCLUDED.report_summary_md,
                incident_json       = EXCLUDED.incident_json,
                updated_at          = now()
            RETURNING id
        """,
            data["router_sn"], data["equip_type"], data["panel_id"],
            data["t_start"], data["t_end"],
            data.get("run_state"),
            data.get("cause_close"),
            data.get("split_reason"),
            data.get("continued_from"),
            json.dumps(data.get("coking_risk_json"), ensure_ascii=False),
            json.dumps(data.get("forward_fill_json"), ensure_ascii=False),
            data.get("analytics_version", "2.2.0"),
            json.dumps(data.get("characteristics_json"), ensure_ascii=False),
            data.get("report_md"),
            data.get("report_summary_md"),
            json.dumps(data.get("incident_json"), ensure_ascii=False)
                if data.get("incident_json") is not None else None,
        )
        seg_id = row["id"]

        # ── Двусторонняя связь НАЗАД ──────────────────────────────────────
        # Если у нового сегмента есть continued_from — обновить continues_to у предка
        if data.get("continued_from"):
            await conn.execute("""
                UPDATE auto_segments SET continues_to=$2 WHERE id=$1
            """, data["continued_from"], seg_id)

        # ── Двусторонняя связь ВПЕРЁД ─────────────────────────────────────
        # Только для DAILY_BOUNDARY: найти преемника с t_start = новый.t_end
        # у которого continued_from = NULL (ссылка была оборвана — например после удаления).
        # Восстанавливает цепочку при повторном анализе удалённого периода.
        if data.get("t_end") and data.get("cause_close") == "DAILY_BOUNDARY":
            next_row = await conn.fetchrow("""
                SELECT id FROM auto_segments
                WHERE router_sn=$1 AND equip_type=$2 AND panel_id=$3
                  AND t_start = $4
                  AND t_end IS NOT NULL
                  AND continued_from IS NULL
                  AND id != $5
                LIMIT 1
            """,
                data["router_sn"], data["equip_type"], data["panel_id"],
                data["t_end"], seg_id,
            )
            if next_row:
                next_id = next_row["id"]
                await conn.execute(
                    "UPDATE auto_segments SET continued_from=$2 WHERE id=$1",
                    next_id, seg_id,
                )
                await conn.execute(
                    "UPDATE auto_segments SET continues_to=$2 WHERE id=$1",
                    seg_id, next_id,
                )

        return seg_id
    finally:
        await conn.close()


async def delete_open_segment(
    router_sn: str, equip_type: str, panel_id: int
) -> None:
    conn = await _connect()
    try:
        await conn.execute("""
            DELETE FROM auto_segments
            WHERE router_sn=$1 AND equip_type=$2 AND panel_id=$3
              AND t_end IS NULL
        """, router_sn, equip_type, panel_id)
    finally:
        await conn.close()


async def update_continues_to(seg_id: int, continues_to_id: int) -> None:
    conn = await _connect()
    try:
        await conn.execute(
            "UPDATE auto_segments SET continues_to=$2 WHERE id=$1",
            seg_id, continues_to_id,
        )
    finally:
        await conn.close()


async def close_open_as_operator_stop(
    router_sn: str, equip_type: str, panel_id: int,
    t_end: datetime, analytics_version: str,
) -> int | None:
    """Принудительно закрыть открытый сегмент с причиной OPERATOR_STOP."""
    conn = await _connect()
    try:
        row = await conn.fetchrow("""
            UPDATE auto_segments
            SET t_end = $4, cause_close = 'OPERATOR_STOP', updated_at = now()
            WHERE router_sn=$1 AND equip_type=$2 AND panel_id=$3
              AND t_end IS NULL
            RETURNING id
        """, router_sn, equip_type, panel_id, t_end)
        return row["id"] if row else None
    finally:
        await conn.close()


async def get_segment_before(
    router_sn: str, equip_type: str, panel_id: int,
    before_t_start: datetime,
) -> dict[str, Any] | None:
    """Последний закрытый сегмент, начавшийся ДО before_t_start."""
    conn = await _connect()
    try:
        row = await conn.fetchrow("""
            SELECT * FROM auto_segments
            WHERE router_sn=$1 AND equip_type=$2 AND panel_id=$3
              AND t_start < $4
              AND t_end IS NOT NULL
            ORDER BY t_start DESC
            LIMIT 1
        """, router_sn, equip_type, panel_id, before_t_start)
        return dict(row) if row else None
    finally:
        await conn.close()


async def delete_segment_by_id(seg_id: int) -> None:
    conn = await _connect()
    try:
        await conn.execute("DELETE FROM auto_segments WHERE id=$1", seg_id)
    finally:
        await conn.close()


async def get_segment_after(
    router_sn: str, equip_type: str, panel_id: int,
    after_t_start: datetime,
) -> dict[str, Any] | None:
    """Следующий сегмент после after_t_start (для навигации ‹ / ›)."""
    conn = await _connect()
    try:
        row = await conn.fetchrow("""
            SELECT id, t_start, t_end, run_state
            FROM auto_segments
            WHERE router_sn=$1 AND equip_type=$2 AND panel_id=$3
              AND t_start > $4
            ORDER BY t_start ASC
            LIMIT 1
        """, router_sn, equip_type, panel_id, after_t_start)
        return dict(row) if row else None
    finally:
        await conn.close()


async def get_segment_by_id(seg_id: int) -> dict[str, Any] | None:
    conn = await _connect()
    try:
        row = await conn.fetchrow("SELECT * FROM auto_segments WHERE id=$1", seg_id)
        return dict(row) if row else None
    finally:
        await conn.close()


async def get_segments_for_calendar(
    router_sn: str, equip_type: str, panel_id: int,
    ts_from: datetime | None = None,
    ts_to: datetime | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Все сегменты машины в заданном диапазоне (включая открытый), от старых к новым."""
    conn = await _connect()
    try:
        conditions = [
            "router_sn=$1", "equip_type=$2", "panel_id=$3",
        ]
        params: list[Any] = [router_sn, equip_type, panel_id]
        idx = 4
        if ts_from:
            conditions.append(f"(t_end IS NULL OR t_end >= ${idx})")
            params.append(ts_from)
            idx += 1
        if ts_to:
            conditions.append(f"t_start <= ${idx}")
            params.append(ts_to)
            idx += 1
        params.append(limit)
        where = " AND ".join(conditions)
        rows = await conn.fetch(f"""
            SELECT * FROM (
                SELECT id, t_start, t_end, run_state, cause_close,
                       split_reason, continued_from, continues_to,
                       coking_risk_json, analytics_version,
                       active_detections_json, characteristics_json,
                       gate_suppressed_hash
                FROM auto_segments
                WHERE {where}
                ORDER BY t_start DESC
                LIMIT ${idx}
            ) recent
            ORDER BY t_start ASC
        """, *params)
        return [dict(r) for r in rows]
    finally:
        await conn.close()


async def clear_segments(
    router_sn: str,
    equip_type: str,
    panel_id: int,
    ts_from: datetime | None = None,
    ts_to: datetime | None = None,
) -> int:
    """Удалить сегменты с опциональным фильтром по диапазону дат.

    Перед удалением обнуляет self-ссылки (continued_from / continues_to)
    из сегментов ВНЕ диапазона, чтобы не нарушать FK-ограничения.
    Возвращает число удалённых записей.
    """
    conn = await _connect()
    try:
        conditions = ["router_sn=$1", "equip_type=$2", "panel_id=$3"]
        params: list[Any] = [router_sn, equip_type, panel_id]
        idx = 4
        if ts_from:
            conditions.append(f"t_start >= ${idx}")
            params.append(ts_from)
            idx += 1
        if ts_to:
            conditions.append(f"t_start < ${idx}")
            params.append(ts_to)
            idx += 1
        where = " AND ".join(conditions)

        async with conn.transaction():
            # Собираем id удаляемых сегментов
            id_rows = await conn.fetch(
                f"SELECT id FROM auto_segments WHERE {where}", *params
            )
            del_ids = [r["id"] for r in id_rows]

            if del_ids:
                # Обнуляем ссылки из ДРУГИХ сегментов на удаляемые
                await conn.execute(
                    "UPDATE auto_segments SET continued_from = NULL"
                    " WHERE continued_from = ANY($1::bigint[])",
                    del_ids,
                )
                await conn.execute(
                    "UPDATE auto_segments SET continues_to = NULL"
                    " WHERE continues_to = ANY($1::bigint[])",
                    del_ids,
                )

            result = await conn.execute(
                f"DELETE FROM auto_segments WHERE {where}", *params
            )

        return int(result.split()[-1])
    finally:
        await conn.close()


async def update_open_segment_status(
    router_sn: str, equip_type: str, panel_id: int,
    status_text: str,
    status_struct: dict | None = None,
) -> None:
    """Обновить детерминированную статус-строку (и её структурную форму) открытого сегмента."""
    import json as _json
    conn = await _connect()
    try:
        await conn.execute("""
            UPDATE auto_segments
            SET status_text        = $4,
                status_struct_json = COALESCE($5::jsonb, status_struct_json),
                status_updated_at  = now(),
                updated_at         = now()
            WHERE router_sn=$1 AND equip_type=$2 AND panel_id=$3
              AND t_end IS NULL
        """, router_sn, equip_type, panel_id, status_text,
             _json.dumps(status_struct, ensure_ascii=False) if status_struct else None)
    finally:
        await conn.close()


async def get_run_state_origin_ts(seg_id: int):
    """Вернуть t_start первого сегмента в цепочке continued_from.

    Нужно для корректного расчёта времени в режиме: суточный срез создаёт
    новый сегмент с тем же run_state, поэтому t_start текущего сегмента
    не отражает реальное время начала режима.
    """
    conn = await _connect()
    try:
        row = await conn.fetchrow("""
            WITH RECURSIVE chain AS (
                SELECT id, t_start, continued_from
                  FROM auto_segments WHERE id = $1
                UNION ALL
                SELECT s.id, s.t_start, s.continued_from
                  FROM auto_segments s
                  JOIN chain c ON s.id = c.continued_from
            )
            SELECT t_start FROM chain WHERE continued_from IS NULL LIMIT 1
        """, seg_id)
        return row["t_start"] if row else None
    finally:
        await conn.close()


async def update_open_segment_warning(
    router_sn: str, equip_type: str, panel_id: int,
    analysis_md: str, fault_hash: str,
    alarm_text: str | None = None,
) -> None:
    """Сохранить Claude-анализ предупреждения в открытый сегмент.

    warning_analysis_md/warning_analyzed_hash — последний разбор (совместимость);
    warning_analyses — append-only история: смена состава тревог (сброс, кнопка
    останова) не затирает разбор исходной аварии.
    """
    import json as _json
    from datetime import datetime, timezone
    entry = _json.dumps({
        "t":          datetime.now(timezone.utc).isoformat(),
        "fault_hash": fault_hash,
        "alarm_text": alarm_text,
        "md":         analysis_md,
    }, ensure_ascii=False)
    conn = await _connect()
    try:
        await conn.execute("""
            UPDATE auto_segments
            SET warning_analysis_md   = $4,
                warning_analyzed_hash = $5,
                warning_analyses      = COALESCE(warning_analyses, '[]'::jsonb) || $6::jsonb,
                updated_at            = now()
            WHERE router_sn=$1 AND equip_type=$2 AND panel_id=$3
              AND t_end IS NULL
        """, router_sn, equip_type, panel_id, analysis_md, fault_hash, entry)
    finally:
        await conn.close()


async def append_segment_gate_event(
    router_sn: str, equip_type: str, panel_id: int,
    event: dict,
) -> None:
    """Добавить запись в append-only журнал гейта открытого сегмента."""
    import json as _json
    conn = await _connect()
    try:
        await conn.execute("""
            UPDATE auto_segments
            SET gate_log   = COALESCE(gate_log, '[]'::jsonb) || $4::jsonb,
                updated_at = now()
            WHERE router_sn=$1 AND equip_type=$2 AND panel_id=$3
              AND t_end IS NULL
        """, router_sn, equip_type, panel_id, _json.dumps([event], ensure_ascii=False))
    finally:
        await conn.close()


async def set_segment_gate_suppression(
    router_sn: str, equip_type: str, panel_id: int,
    suppressed_hash: str,
) -> None:
    """Зафиксировать вердикт «отменить»: подавить аналитику для данного состава детекций."""
    conn = await _connect()
    try:
        await conn.execute("""
            UPDATE auto_segments
            SET gate_suppressed_hash = $4,
                updated_at           = now()
            WHERE router_sn=$1 AND equip_type=$2 AND panel_id=$3
              AND t_end IS NULL
        """, router_sn, equip_type, panel_id, suppressed_hash)
    finally:
        await conn.close()


async def delete_segments_by_ids(seg_ids: list[int]) -> int:
    """Удалить конкретные сегменты по списку ID.

    Перед удалением обнуляет self-ссылки (continued_from / continues_to)
    из сегментов вне списка, чтобы не нарушать FK-ограничения.
    Возвращает число удалённых записей.
    """
    if not seg_ids:
        return 0
    conn = await _connect()
    try:
        async with conn.transaction():
            # Обнуляем ссылки из ДРУГИХ сегментов на удаляемые
            await conn.execute(
                "UPDATE auto_segments SET continued_from = NULL"
                " WHERE continued_from = ANY($1::bigint[])"
                "   AND id <> ALL($1::bigint[])",
                seg_ids,
            )
            await conn.execute(
                "UPDATE auto_segments SET continues_to = NULL"
                " WHERE continues_to = ANY($1::bigint[])"
                "   AND id <> ALL($1::bigint[])",
                seg_ids,
            )
            result = await conn.execute(
                "DELETE FROM auto_segments WHERE id = ANY($1::bigint[])",
                seg_ids,
            )
        return int(result.split()[-1])
    finally:
        await conn.close()


# ── detection_events ──────────────────────────────────────────────────────────

async def insert_detection_events(
    router_sn: str,
    equip_type: str,
    panel_id: int,
    events: list[dict[str, Any]],
) -> None:
    """Записать события срабатывания детекторов (одно событие = один фронт).

    events — список dict: {scenario, detected_at, segment_id?, severity?, run_state?}
    Дублирование по (router_sn, equip_type, panel_id, scenario, segment_id) не
    контролируется на уровне unique-constraint (сегменты уникальны по id, повтор
    маловероятен). При необходимости защиты добавить UNIQUE INDEX позднее.
    """
    if not events:
        return
    conn = await _connect()
    try:
        await conn.executemany("""
            INSERT INTO detection_events
                (router_sn, equip_type, panel_id, scenario,
                 detected_at, segment_id, severity, run_state, front_count)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        """, [
            (
                router_sn, equip_type, panel_id,
                ev["scenario"],
                ev["detected_at"],
                ev.get("segment_id"),
                ev.get("severity"),
                ev.get("run_state"),
                int(ev.get("front_count", 1)),
            )
            for ev in events
        ])
    finally:
        await conn.close()


# ── alarm_episodes ────────────────────────────────────────────────────────────

def episode_key(scenario: str | None, addr, bit) -> str:
    """Ключ тревоги: панельные — по-битно, аналитика — по сценарию.

    Должен совпадать с engine._alert_key (тот берёт addr/bit из detection.values).
    """
    if scenario == "CONTROLLER_FAULT":
        return f"CONTROLLER_FAULT|{addr}|{bit}"
    return scenario or "?"


async def open_episode(
    router_sn: str, equip_type: str, panel_id: int, *,
    scenario: str, source: str, severity: str | None,
    t_open: datetime, open_values: dict | None, segment_id: int | None = None,
    addr: int | None = None, bit: int | None = None,
) -> int:
    """Открыть эпизод тревоги (t_close IS NULL = висит). Возвращает id."""
    conn = await _connect()
    try:
        row = await conn.fetchrow("""
            INSERT INTO alarm_episodes
                (router_sn, equip_type, panel_id, scenario, source, severity,
                 t_open, open_values_json, segment_id_open, addr, bit)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            RETURNING id
        """, router_sn, equip_type, panel_id, scenario, source, severity,
             t_open, json.dumps(open_values or {}, ensure_ascii=False, default=str),
             segment_id, addr, bit)
        return int(row["id"])
    finally:
        await conn.close()


async def insert_closed_episode(
    router_sn: str, equip_type: str, panel_id: int, *,
    scenario: str, source: str, severity: str | None,
    t_open: datetime, t_close: datetime, active_sec: float,
    open_values: dict | None = None,
    addr: int | None = None, bit: int | None = None,
) -> int:
    """Сразу закрытый эпизод — для детекций короткого сегмента, не живших
    в снимке открытого окна (например START_FAILURE за один цикл)."""
    conn = await _connect()
    try:
        row = await conn.fetchrow("""
            INSERT INTO alarm_episodes
                (router_sn, equip_type, panel_id, scenario, source, severity,
                 t_open, t_close, close_reason, active_sec, open_values_json, addr, bit)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'condition_cleared', $9, $10, $11, $12)
            RETURNING id
        """, router_sn, equip_type, panel_id, scenario, source, severity,
             t_open, t_close, active_sec,
             json.dumps(open_values or {}, ensure_ascii=False, default=str),
             addr, bit)
        return int(row["id"])
    finally:
        await conn.close()


async def update_episode(
    episode_id: int, *, active_sec_add: float = 0.0, severity: str | None = None,
) -> None:
    """Приращение живого времени и/или эскалация severity открытого эпизода."""
    conn = await _connect()
    try:
        await conn.execute("""
            UPDATE alarm_episodes
            SET active_sec = active_sec + $2,
                severity   = COALESCE($3, severity),
                updated_at = now()
            WHERE id = $1
        """, episode_id, active_sec_add, severity)
    finally:
        await conn.close()


async def close_episode(episode_id: int, t_close: datetime, reason: str) -> None:
    conn = await _connect()
    try:
        await conn.execute("""
            UPDATE alarm_episodes
            SET t_close = $2, close_reason = $3, updated_at = now()
            WHERE id = $1 AND t_close IS NULL
        """, episode_id, t_close, reason)
    finally:
        await conn.close()


async def get_open_episodes(
    router_sn: str, equip_type: str, panel_id: int,
) -> list[dict[str, Any]]:
    conn = await _connect()
    try:
        rows = await conn.fetch("""
            SELECT * FROM alarm_episodes
            WHERE router_sn = $1 AND equip_type = $2 AND panel_id = $3
              AND t_close IS NULL
            ORDER BY t_open
        """, router_sn, equip_type, panel_id)
        return [dict(r) for r in rows]
    finally:
        await conn.close()


async def get_open_episodes_all() -> dict[str, list[dict[str, Any]]]:
    """Открытые эпизоды всего парка одним запросом: {"sn|type|panel": [rows]}."""
    conn = await _connect()
    try:
        rows = await conn.fetch(
            "SELECT * FROM alarm_episodes WHERE t_close IS NULL ORDER BY t_open"
        )
        result: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            key = f"{r['router_sn']}|{r['equip_type']}|{r['panel_id']}"
            result.setdefault(key, []).append(dict(r))
        return result
    finally:
        await conn.close()


async def set_episodes_gate_suppressed(
    router_sn: str, equip_type: str, panel_id: int, scenarios: list[str],
) -> None:
    """Пометить открытые аналитические эпизоды вердиктом гейта «отменить»."""
    if not scenarios:
        return
    conn = await _connect()
    try:
        await conn.execute("""
            UPDATE alarm_episodes
            SET gate_suppressed = TRUE, updated_at = now()
            WHERE router_sn = $1 AND equip_type = $2 AND panel_id = $3
              AND t_close IS NULL AND scenario = ANY($4::text[])
        """, router_sn, equip_type, panel_id, scenarios)
    finally:
        await conn.close()


async def get_episodes_overlapping(
    router_sn: str, equip_type: str, panel_id: int,
    t_from: datetime, t_to: datetime,
) -> list[dict[str, Any]]:
    """Эпизоды, пересекающие окно [t_from, t_to) — «Замечания» отчёта сегмента."""
    conn = await _connect()
    try:
        rows = await conn.fetch("""
            SELECT * FROM alarm_episodes
            WHERE router_sn = $1 AND equip_type = $2 AND panel_id = $3
              AND t_open < $5
              AND (t_close IS NULL OR t_close > $4)
            ORDER BY t_open
        """, router_sn, equip_type, panel_id, t_from, t_to)
        return [dict(r) for r in rows]
    finally:
        await conn.close()


async def set_episode_context(episode_id: int, context: dict) -> None:
    """Прикрепить контекст аварии (Фаза C) к эпизоду."""
    conn = await _connect()
    try:
        await conn.execute("""
            UPDATE alarm_episodes
            SET context_json = $2, updated_at = now()
            WHERE id = $1
        """, episode_id, json.dumps(context, ensure_ascii=False, default=str))
    finally:
        await conn.close()


async def count_episodes_batch(
    router_sn: str,
    equip_type: str,
    panel_id: int,
    scenarios: list[str],
    window_days: int = 30,
    since_ts: datetime | None = None,
) -> dict[str, dict[str, float]]:
    """Счётчики эпизодов одним запросом: фронты И суммарная длительность.

    Возвращает {scenario: {count_window, dur_window, count_since, dur_since}}.
    Считаются эпизоды с t_open в окне (включая ещё открытые); длительность —
    сумма active_sec (время «под связью»). since_ts=None → счётчики «с пуска» = 0.
    """
    if not scenarios:
        return {}
    conn = await _connect()
    try:
        rows = await conn.fetch("""
            SELECT scenario, addr, bit,
                   COUNT(*) FILTER (
                       WHERE t_open > now() - ($5 || ' days')::interval) AS count_window,
                   COALESCE(SUM(active_sec) FILTER (
                       WHERE t_open > now() - ($5 || ' days')::interval), 0) AS dur_window,
                   COUNT(*) FILTER (
                       WHERE $6::timestamptz IS NOT NULL AND t_open >= $6) AS count_since,
                   COALESCE(SUM(active_sec) FILTER (
                       WHERE $6::timestamptz IS NOT NULL AND t_open >= $6), 0) AS dur_since
            FROM alarm_episodes
            WHERE router_sn = $1 AND equip_type = $2 AND panel_id = $3
              AND scenario = ANY($4::text[])
              AND (t_open > now() - ($5 || ' days')::interval
                   OR ($6::timestamptz IS NOT NULL AND t_open >= $6))
            GROUP BY scenario, addr, bit
        """, router_sn, equip_type, panel_id, scenarios, str(window_days), since_ts)
        # Ключ — per-fault (совпадает с engine._alert_key)
        result: dict[str, dict[str, float]] = {}
        for r in rows:
            result[episode_key(r["scenario"], r["addr"], r["bit"])] = {
                "count_window": int(r["count_window"]),
                "dur_window":   float(r["dur_window"]),
                "count_since":  int(r["count_since"]),
                "dur_since":    float(r["dur_since"]),
            }
        return result
    finally:
        await conn.close()


# ── alert_journal ─────────────────────────────────────────────────────────────

async def insert_alert_events(
    router_sn: str,
    equip_type: str,
    panel_id: int,
    events: list[dict[str, Any]],
) -> None:
    """Записать события жизненного цикла детекций (OPENED / UPDATED / CLOSED)."""
    if not events:
        return
    conn = await _connect()
    try:
        await conn.executemany("""
            INSERT INTO alert_journal
                (router_sn, equip_type, panel_id, scenario,
                 event_type, ts, severity, trigger_text, values_json, segment_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10)
        """, [
            (
                router_sn, equip_type, panel_id,
                ev["scenario"],
                ev["event_type"],
                ev["ts"],
                ev.get("severity"),
                ev.get("trigger"),
                json.dumps(ev.get("values"), ensure_ascii=False)
                    if ev.get("values") is not None else None,
                ev.get("segment_id"),
            )
            for ev in events
        ])
    finally:
        await conn.close()


async def get_active_alerts(
    router_sn: str,
    equip_type: str,
    panel_id: int,
) -> list[dict[str, Any]]:
    """Вернуть список сценариев с незакрытой тревогой (последнее событие ≠ CLOSED).

    Используется при инициализации движка для восстановления _active_alerts.
    """
    conn = await _connect()
    try:
        rows = await conn.fetch("""
            SELECT DISTINCT ON (scenario, values_json->>'addr', values_json->>'bit')
                scenario, event_type, ts, severity, trigger_text, values_json, segment_id
            FROM alert_journal
            WHERE router_sn = $1
              AND equip_type = $2
              AND panel_id   = $3
            ORDER BY scenario, values_json->>'addr', values_json->>'bit', ts DESC
        """, router_sn, equip_type, panel_id)
        result = []
        for r in rows:
            if r["event_type"] != "CLOSED":
                vj = r["values_json"]
                result.append({
                    "scenario":  r["scenario"],
                    "severity":  r["severity"],
                    "trigger":   r["trigger_text"],
                    "values":    json.loads(vj) if isinstance(vj, str) else (vj or {}),
                    "segment_id": r["segment_id"],
                })
        return result
    finally:
        await conn.close()


async def has_segments(
    router_sn: str, equip_type: str, panel_id: int
) -> bool:
    conn = await _connect()
    try:
        row = await conn.fetchrow("""
            SELECT 1 FROM auto_segments
            WHERE router_sn=$1 AND equip_type=$2 AND panel_id=$3
            LIMIT 1
        """, router_sn, equip_type, panel_id)
        return row is not None
    finally:
        await conn.close()
