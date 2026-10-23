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

Здесь же — пул source-БД для history_sync (v5.1.24: через db/remote — сроки
запросов, keepalive, обрыв при ошибке; выдача — remote.connection).
"""
from __future__ import annotations

import asyncio
import logging

import asyncpg

from config import settings
from db import remote

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
            # Завершённая работа с локальной БД — тоже продвижение движка
            # (медленная, но живая БД не выглядит зависанием, v5.1.24)
            remote.touch_owner()


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


async def sync_source_pool() -> asyncpg.Pool:
    """Пул удалённой БД телеметрии для history_sync.

    Отдельный маленький пул: держит соединения тёплыми между циклами
    синхронизации (реконнект через WAN дорогой). v5.1.24: command_timeout
    (раньше не было вовсе — запрос на полуоткрытом соединении мог висеть
    ~15 мин или вечно, а с ним весь воркер), keepalive, statement_timeout;
    min_size=0 — при лежащем VPN создание пула не падает на каждом тике.
    Соединения берутся только через remote.connection(pool).
    """
    global _sync_source_pool
    if _sync_source_pool is None:
        async with _sync_source_lock:
            if _sync_source_pool is None:
                _sync_source_pool = await remote.create_pool(
                    settings.source_db_url, min_size=0, max_size=4,
                    label="history_sync (source-БД)",
                )
                logger.info("Пул source-БД для history_sync создан (min=0, max=4)")
    return _sync_source_pool


async def close_pools() -> None:
    """Закрыть пулы при остановке приложения — параллельно и со сроком.

    Pool.close() ждёт выданные соединения без срока (22.10: 90 с → SIGKILL):
    не закрылся за pool_close_timeout — соединения обрываются (v5.1.24)."""
    global _analytics_pool, _sync_source_pool
    an, _analytics_pool = _analytics_pool, None
    ss, _sync_source_pool = _sync_source_pool, None
    jobs = []
    if an is not None:
        jobs.append(remote.close_pool(an, "аналитической БД"))
    if ss is not None:
        jobs.append(remote.close_pool(ss, "source-БД для history_sync"))
    if jobs:
        await asyncio.gather(*jobs, return_exceptions=True)
