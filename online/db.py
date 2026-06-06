"""CRUD для онлайн-мониторинга: online_observations, auto_segments."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

import asyncpg

from config import settings

logger = logging.getLogger(__name__)


async def _connect() -> asyncpg.Connection:
    return await asyncpg.connect(settings.analytics_db_url)


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


async def list_observations() -> list[dict[str, Any]]:
    conn = await _connect()
    try:
        rows = await conn.fetch("""
            SELECT o.*, e.name, e.manufacturer, e.model, e.engine_sn, e.kb_path
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
    """Создать или обновить открытый сегмент (DELETE + INSERT)."""
    conn = await _connect()
    try:
        async with conn.transaction():
            await conn.execute("""
                DELETE FROM auto_segments
                WHERE router_sn=$1 AND equip_type=$2 AND panel_id=$3
                  AND t_end IS NULL
            """, data["router_sn"], data["equip_type"], data["panel_id"])
            row = await conn.fetchrow("""
                INSERT INTO auto_segments (
                    router_sn, equip_type, panel_id,
                    t_start, t_end,
                    run_state, coking_risk_json,
                    analytics_version,
                    current_values_json, active_detections_json,
                    continued_from, updated_at
                ) VALUES ($1,$2,$3,$4,NULL,$5,$6::jsonb,$7,$8::jsonb,$9::jsonb,$10,now())
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
                characteristics_json, report_md,
                updated_at
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,$11::jsonb,$12,$13::jsonb,$14,now())
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
            SELECT id, t_start, t_end, run_state, cause_close,
                   split_reason, continued_from, continues_to,
                   coking_risk_json, analytics_version,
                   active_detections_json, characteristics_json
            FROM auto_segments
            WHERE {where}
            ORDER BY t_start ASC
            LIMIT ${idx}
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
    status_text: str, status_hash: str,
) -> None:
    """Обновить статус-строку и хэш открытого сегмента."""
    conn = await _connect()
    try:
        await conn.execute("""
            UPDATE auto_segments
            SET status_text       = $4,
                status_hash       = $5,
                status_updated_at = now(),
                updated_at        = now()
            WHERE router_sn=$1 AND equip_type=$2 AND panel_id=$3
              AND t_end IS NULL
        """, router_sn, equip_type, panel_id, status_text, status_hash)
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
