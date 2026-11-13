# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Адаптеры провайдеров для цикла с инструментами (llm/agent_loop.py).

Claude — родной SDK (claude.py); OpenAI — Responses API (openai_responses.py);
DeepSeek, Gemini (точка OpenAI-формата Google), прямой режим шлюза и локальные
серверы с OpenAI-совместимым API — адаптер chat/completions (openai_chat.py).
"""
from __future__ import annotations

from typing import Any

OPENAI_FORMAT = ("openai", "deepseek", "gemini", "gorynych", "lmstudio", "llamacpp")


def make_adapter(entry: dict | None, *, model: str | None = None, depth: str | None = None,
                 priority: str | None = None) -> Any:
    """Адаптер по записи реестра. entry=None — Claude с ключом и прокси сервиса
    (как разбор шёл до записи реестра)."""
    provider = (entry or {}).get("provider") or "anthropic"
    if provider == "anthropic":
        from llm.adapters.claude import ClaudeAdapter
        return ClaudeAdapter(entry=entry, model=model or "", depth=depth)
    if provider == "openai":
        # Новые модели OpenAI принимают инструменты с рассуждением только через Responses API
        from llm.adapters.openai_responses import OpenAIResponsesAdapter
        return OpenAIResponsesAdapter(entry=entry, model=model, depth=depth, priority=priority)
    if provider in OPENAI_FORMAT:
        from llm.adapters.openai_chat import OpenAIChatAdapter
        return OpenAIChatAdapter(entry=entry, model=model, depth=depth, priority=priority)
    raise ValueError(f"провайдер «{provider}» цикл с инструментами не поддерживает")
