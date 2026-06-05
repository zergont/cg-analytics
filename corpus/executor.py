"""Выполнение инструментов для анализа сегментов (corpus pipeline).

RAG-инструмент search_manual удалён: семантический поиск по документации заменён
детерминированным справочником кодов неисправностей (analytics/fault_ref.py).
Описания кодов подставляются блоком аналитики в report_md до отправки в Claude.
"""
from __future__ import annotations
import logging
from typing import Any

logger = logging.getLogger(__name__)


def execute_tool(tool_name: str, tool_input: dict[str, Any], kb_path: str | None) -> str:
    """Диспетчер инструментов сегментного анализа.

    Возвращает строку — результат для Anthropic API (tool_result content).
    """
    logger.warning("corpus/executor: неизвестный инструмент: %s", tool_name)
    return f"Неизвестный инструмент: {tool_name}"
