# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Расход ИИ и состояние сервера телеметрии: API для карточек настроек (MCP Э2б;
design_mcp_e2.md, разд. 3 и 6.2).

GET /api/llm-usage?days=7 — по дням и провайдерам, по видам задач, по инструментам
сервера телеметрии. Только итоги: текстов запросов и ответов, ключей и адресов
здесь нет.
GET /api/telemetry-mcp/status — доступен ли сервер телеметрии хосту цикла и сколько
у него инструментов (строка «Разбор аварии» таблицы «Кто и как разбирает»).
"""
from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from db import llm_usage as _usage

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/llm-usage", response_class=JSONResponse)
async def llm_usage(days: int = 7):
    """Сводка расхода за последние days суток (1…365), сутки — в поясе сервиса."""
    try:
        data = await _usage.summary(days)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Расход ИИ: сводка не построена: %r", exc)
        return JSONResponse({"ok": False, "detail": "сводка не построена — см. журнал сервиса"})
    return JSONResponse({"ok": True, **data})


@router.get("/api/telemetry-mcp/status", response_class=JSONResponse)
async def telemetry_mcp_status():
    """Состояние сервера телеметрии со стороны хоста цикла."""
    from llm.mcp_client import probe
    st = await probe()
    return JSONResponse({"ok": st["ok"], "tools": st["tools"], "url": st["url"],
                         "detail": st.get("detail", "")})
