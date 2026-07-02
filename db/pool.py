# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Общий пул соединений к аналитической БД.

Пул создаётся лениво при первом запросе, поэтому разовые скрипты
работают без инициализации. CRUD-модули сохраняют прежний шаблон:

    conn = await _connect()
    try:
        ...
    finally:
        await conn.close()   # ← возвращает соединение в пул, не закрывает

Обёртка PooledConnection перехватывает close() и делает pool.release().
"""
from __future__ import annotations

import asyncio
import logging

import asyncpg

from config import settings

logger = logging.getLogger(__name__)

_analytics_pool: asyncpg.Pool | None = None
_analytics_lock = asyncio.Lock()

_sync_source_pool: asyncpg.Pool | None = None
_sync_source_lock = asyncio.Lock()


class PooledConnection:
    """Соединение из пула, у которого close() возвращает его в пул."""

    __slots__ = ("_pool", "_conn")

    def __init__(self, pool: asyncpg.Pool, conn) -> None:
        self._pool = pool
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    async def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            await self._pool.release(conn)


async def acquire_analytics() -> PooledConnection:
    """Соединение с аналитической БД из общего пула."""
    global _analytics_pool
    if _analytics_pool is None:
        async with _analytics_lock:
            if _analytics_pool is None:
                _analytics_pool = await asyncpg.create_pool(
                    settings.analytics_db_url, min_size=2, max_size=10
                )
                logger.info("Пул аналитической БД создан (min=2, max=10)")
    return PooledConnection(_analytics_pool, await _analytics_pool.acquire())


async def acquire_sync_source() -> PooledConnection:
    """Соединение с удалённой БД телеметрии для history_sync.

    Отдельный маленький пул: держит соединения тёплыми между циклами
    синхронизации (реконнект через WAN дорогой). При обрыве VPN мёртвые
    соединения отбрасываются пулом, следующий acquire откроет новое.
    """
    global _sync_source_pool
    if _sync_source_pool is None:
        async with _sync_source_lock:
            if _sync_source_pool is None:
                _sync_source_pool = await asyncpg.create_pool(
                    settings.source_db_url, min_size=1, max_size=4
                )
                logger.info("Пул source-БД для history_sync создан (min=1, max=4)")
    return PooledConnection(_sync_source_pool, await _sync_source_pool.acquire())


async def close_pools() -> None:
    """Закрыть все пулы при остановке приложения."""
    global _analytics_pool, _sync_source_pool
    if _analytics_pool is not None:
        await _analytics_pool.close()
        _analytics_pool = None
        logger.info("Пул аналитической БД закрыт")
    if _sync_source_pool is not None:
        await _sync_source_pool.close()
        _sync_source_pool = None
        logger.info("Пул source-БД для history_sync закрыт")
