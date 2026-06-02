"""Выполнение инструментов для анализа сегментов (corpus pipeline)."""
from __future__ import annotations
import logging
from typing import Any

logger = logging.getLogger(__name__)


def execute_tool(tool_name: str, tool_input: dict[str, Any], kb_path: str | None) -> str:
    """Диспетчер инструментов сегментного анализа.

    Возвращает строку — результат для Anthropic API (tool_result content).
    """
    match tool_name:
        case "search_manual":
            return _search_manual(tool_input.get("query", ""), kb_path)
        case _:
            logger.warning("corpus/executor: неизвестный инструмент: %s", tool_name)
            return f"Неизвестный инструмент: {tool_name}"


def _search_manual(query: str, kb_path: str | None) -> str:
    """Семантический поиск по PDF-документации оборудования."""
    if not kb_path:
        return "База знаний не настроена для данного оборудования."
    if not query.strip():
        return "Пустой запрос."

    try:
        from knowledge.retriever import search_manual_docs
        result = search_manual_docs(query.strip(), kb_path, top_k=4)
        if result:
            logger.debug("corpus/executor: search_manual '%s' → %d символов", query[:60], len(result))
            return result
        return "По запросу ничего не найдено в документации."
    except Exception as e:
        logger.warning("corpus/executor: search_manual ошибка: %s", e)
        return f"Ошибка поиска по документации: {e}"
