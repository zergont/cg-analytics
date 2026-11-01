# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Сборка MCP-сервера телеметрии: инструменты, транспорт streamable HTTP,
пулы к БД на старте процесса, /health.

Пулы открываются в lifespan Starlette-приложения (один раз на процесс), а не в
lifespan MCPServer — тот играется на каждую сессию, а в stateless-режиме сессия
равна запросу.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import __version__
from .config import McpSettings, load
from . import common
from . import tools_equipment, tools_segments, tools_series, tools_timeline

logger = logging.getLogger(__name__)

INSTRUCTIONS = """Сервер телеметрии «Честной Генерации»: данные генераторных установок для разбора.
Правила для модели:
• Данные — только из инструментов; что не запрошено, не утверждать; цифры по памяти не писать.
• Начни с equipment_tree: имена и id установок, соседи по объекту, когда были последние данные.
• Время во входе и в ответах — пояс сервиса (МСК), формат «ДД.ММ.ГГГГ ЧЧ:ММ[:СС]».
• Сначала timeline (что происходило), потом series (как менялись параметры); соседей смотреть,
  если машины на одном объекте (series/timeline with_neighbors=true).
• Каждый ответ начинается конвертом [Tn]: окно, слой, что отброшено, усечение. Если данные
  усечены или окно внутри разрыва связи — скажи об этом, не додумывай.
• В конце ответа перечисли использованные вызовы блоком «## Данные» по меткам [Tn].
• Запись в телеметрию идёт по изменению: значение держится до следующей записи; пофазные
  медианы сегмента — не показатель несимметрии; средние «по отсчётам» смещены."""


def build_server(st: McpSettings | None = None) -> MCPServer:
    st = st or load()
    common.SETTINGS = st
    server = MCPServer(
        name="cg-telemetry",
        title="Телеметрия «Честной Генерации»",
        instructions=INSTRUCTIONS,
        version=__version__,
        log_level=st.log_level if st.log_level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL") else "INFO",
    )
    for mod in (tools_equipment, tools_series, tools_timeline, tools_segments):
        mod.register(server)

    @server.custom_route("/health", methods=["GET"])
    async def health(_request: Request) -> JSONResponse:
        tools = await server.list_tools()
        return JSONResponse({"status": "ok", "service": "cg-telemetry-mcp", "version": __version__,
                             "tools": [t.name for t in tools], "path": st.path})

    return server


async def startup(st: McpSettings) -> None:
    from analytics.source import init_source_pool
    from db.analytics import get_app_setting
    from config import settings as _settings, set_tz
    await init_source_pool()
    try:
        tz = await get_app_setting("timezone", _settings.timezone_name)
        set_tz(tz)
    except Exception:  # noqa: BLE001
        logger.warning("Часовой пояс из app_settings не прочитан — пояс конфига", exc_info=True)
    try:
        from db.units import get_tree
        await get_tree(panel_types=st.panel_types, ensure=True)
    except Exception:  # noqa: BLE001
        logger.warning("Карта установок при старте не материализована", exc_info=True)
    logger.info("cg-telemetry-mcp %s: %s:%d%s", __version__, st.host, st.port, st.path)


async def shutdown() -> None:
    import asyncio
    from analytics.source import close_source_pool
    from db.pool import close_pools
    await asyncio.gather(close_source_pool(), close_pools(), return_exceptions=True)


def build_app(st: McpSettings | None = None) -> Any:
    st = st or load()
    server = build_server(st)
    app = server.streamable_http_app(
        streamable_http_path=st.path,
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        host=st.host,
    )
    orig = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(_app: Any):
        await startup(st)
        try:
            async with orig(_app):
                yield
        finally:
            await shutdown()

    app.router.lifespan_context = lifespan
    return app
