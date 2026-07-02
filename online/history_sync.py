# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Синхронизация history из источника (TimescaleDB) в локальную аналитическую БД.

Воркер периодически (каждые N секунд) копирует новые строки history,
используя received_at как монотонный курсор. Прогресс хранится в
таблице history_sync_state (per-device).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

_INITIAL_WINDOW_DAYS = 7
_BATCH_LIMIT         = 50_000
_BUFFER_SEC          = 2       # не трогаем строки моложе N сек (защита от частичных батчей)


async def _src():
    """Соединение с source-БД из пула (тёплое между циклами, conn.close() вернёт в пул)."""
    from db.pool import acquire_sync_source
    return await acquire_sync_source()


async def _local():
    """Соединение с локальной БД из общего пула (conn.close() вернёт его в пул)."""
    from db.pool import acquire_analytics
    return await acquire_analytics()


async def _get_cursors() -> dict[str, datetime]:
    """Возвращает last_sync_at для всех устройств из history_sync_state."""
    conn = await _local()
    try:
        rows = await conn.fetch(
            "SELECT router_sn, equip_type, panel_id, last_sync_at FROM history_sync_state"
        )
        return {
            f"{r['router_sn']}|{r['equip_type']}|{r['panel_id']}": r["last_sync_at"]
            for r in rows
        }
    finally:
        await conn.close()


async def _sync_device(
    router_sn: str,
    equip_type: str,
    panel_id: int,
    last_sync_at: datetime,
) -> int:
    """Копирует одну пачку истории для устройства. Возвращает число скопированных строк."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=_BUFFER_SEC)

    src_conn = await _src()
    try:
        rows = await src_conn.fetch(
            """
            SELECT router_sn, equip_type, panel_id, addr, ts, received_at, value, raw
            FROM history
            WHERE router_sn  = $1
              AND equip_type = $2
              AND panel_id   = $3
              AND received_at > $4
              AND received_at < $5
            ORDER BY received_at
            LIMIT $6
            """,
            router_sn, equip_type, panel_id, last_sync_at, cutoff, _BATCH_LIMIT,
        )
    finally:
        await src_conn.close()

    if not rows:
        return 0

    max_received_at = max(r["received_at"] for r in rows)
    records = [
        (r["router_sn"], r["equip_type"], r["panel_id"],
         r["addr"], r["ts"], r["received_at"], r["value"], r["raw"])
        for r in rows
    ]

    loc_conn = await _local()
    try:
        async with loc_conn.transaction():
            await loc_conn.executemany(
                """
                INSERT INTO history
                    (router_sn, equip_type, panel_id, addr, ts, received_at, value, raw)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                records,
            )
            await loc_conn.execute(
                """
                INSERT INTO history_sync_state
                    (router_sn, equip_type, panel_id, last_sync_at)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (router_sn, equip_type, panel_id) DO UPDATE
                    SET last_sync_at = EXCLUDED.last_sync_at
                """,
                router_sn, equip_type, panel_id, max_received_at,
            )
    finally:
        await loc_conn.close()

    return len(rows)


class HistorySyncWorker:
    """Фоновый воркер синхронизации history из источника в локальную БД."""

    def __init__(self, interval_sec: int = 30) -> None:
        self._interval_sec = interval_sec
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="history_sync_worker")
        logger.info("HistorySyncWorker: запущен (интервал %d сек)", self._interval_sec)

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _run(self) -> None:
        while True:
            try:
                await self._tick()
            except Exception:
                logger.exception("HistorySyncWorker: ошибка тика")
            await asyncio.sleep(self._interval_sec)

    async def _tick(self) -> None:
        from db.analytics import get_equipment_registry

        registry = await get_equipment_registry()
        active = [eq for eq in registry if eq.get("active")]
        if not active:
            return

        cursors  = await _get_cursors()
        init_ts  = datetime.now(timezone.utc) - timedelta(days=_INITIAL_WINDOW_DAYS)

        coros = [
            _sync_device(
                eq["router_sn"], eq["equip_type"], int(eq["panel_id"]),
                cursors.get(f"{eq['router_sn']}|{eq['equip_type']}|{eq['panel_id']}", init_ts),
            )
            for eq in active
        ]
        results = await asyncio.gather(*coros, return_exceptions=True)

        total = 0
        for eq, result in zip(active, results):
            if isinstance(result, Exception):
                logger.error(
                    "HistorySyncWorker[%s/%s/%s]: %s",
                    eq["router_sn"], eq["equip_type"], eq["panel_id"], result,
                )
            else:
                total += result

        if total:
            logger.info("HistorySyncWorker: скопировано %d строк", total)
