# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Расход ИИ: API для карточки настроек (v5.2.7, MCP Э2б; design_mcp_e2.md, разд. 6.2).

GET /api/llm-usage?days=7 — по дням и провайдерам, по видам задач, по инструментам
сервера телеметрии. Только итоги: текстов запросов и ответов, ключей и адресов
здесь нет.
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
