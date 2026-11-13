# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Адаптер Claude API для цикла с инструментами (MCP Э2б; design_mcp_e2.md, разд. 4.2).

Один шаг цикла — один `messages.create` официального SDK. Что важно:

- разговор только дополняется: ответ модели кладётся в историю целиком (вместе с
  блоками рассуждений — без них следующий запрос к новым моделям вернёт 400),
  список инструментов и system за прогон не меняются;
- кэш промпта: метка на system (закрывает инструменты и system) и автоматическая
  метка на хвосте разговора — цикл пересылает весь разговор на каждом шаге, без
  кэша цена растёт с числом вызовов;
- картинка графика возвращается внутри результата инструмента;
- глубина рассуждений — `output_config.effort` при адаптивном рассуждении
  (бюджет рассуждений в токенах новые модели не принимают); «по умолчанию» —
  параметры не шлются, решает модель;
- принудительный выбор инструмента не используется (новые модели его отвергают):
  после предела вызовы получают отказ результатом (см. llm/agent_loop.py).

Ключ — записи реестра, иначе config.yml; прокси — записи, иначе карточки
«Claude API (облако)».
"""
from __future__ import annotations

import logging
from typing import Any

from llm.agent_loop import LoopError, ToolCall, Turn
from llm.mcp_client import ToolResult, ToolSpec, to_anthropic

logger = logging.getLogger(__name__)

# Уровень рассуждения записи реестра → effort. «Выкл» у новых моделей нет: низший уровень
_EFFORT = {"off": "low", "low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh"}
CACHE = {"type": "ephemeral"}


class ClaudeAdapter:
    provider = "anthropic"

    def __init__(self, *, model: str, entry: dict | None = None, depth: str | None = None,
                 max_tokens: int | None = None, client: Any = None) -> None:
        from corpus.settings import get_claude_settings
        cfg = get_claude_settings()
        self.entry = entry
        self.model = model or (entry or {}).get("model") or cfg["model"]
        self.depth = depth
        self.vision = bool(entry.get("vision", True)) if entry else True
        self.effort = _EFFORT.get(str((entry or {}).get("reasoning") or "default"))
        self.max_tokens = int(max_tokens or cfg["max_tokens"])
        self._proxy = (entry or {}).get("proxy") or cfg.get("proxy") or None
        self._client = client          # подмена в тестах
        self._own_client = client is None
        self._system: list[dict[str, Any]] = []
        self._tools: list[dict[str, Any]] = []
        self._messages: list[dict[str, Any]] = []

    # ── разговор ─────────────────────────────────────────────────────────────

    def start(self, system: str, user: str, tools: list[ToolSpec]) -> None:
        self._system = [{"type": "text", "text": system, "cache_control": CACHE}]
        self._tools = to_anthropic(tools)
        self._messages = [{"role": "user", "content": user}]

    def add_results(self, results: list[tuple[ToolCall, ToolResult]], note: str | None) -> None:
        """Все результаты шага — одним сообщением; пометка — текстом после них."""
        content: list[dict[str, Any]] = []
        for call, res in results:
            body: list[dict[str, Any]] = [{"type": "text", "text": res.text or "(пусто)"}]
            for mime, data in res.images:
                body.append({"type": "image",
                             "source": {"type": "base64", "media_type": mime, "data": data}})
            block: dict[str, Any] = {"type": "tool_result", "tool_use_id": call.id, "content": body}
            if res.is_error:
                block["is_error"] = True
            content.append(block)
        if note:
            content.append({"type": "text", "text": note})
        self._messages.append({"role": "user", "content": content})

    def add_note(self, note: str) -> None:
        self._messages.append({"role": "user", "content": [{"type": "text", "text": note}]})

    # ── вызов ────────────────────────────────────────────────────────────────

    def _get_client(self) -> Any:
        if self._client is None:
            import anthropic
            from config import settings as app_settings
            http = anthropic.DefaultAsyncHttpxClient(proxy=self._proxy) if self._proxy else None
            self._client = anthropic.AsyncAnthropic(
                api_key=(self.entry or {}).get("api_key") or app_settings.anthropic_api_key,
                http_client=http, max_retries=2)
        return self._client

    async def step(self, *, final: bool, timeout: float) -> Turn:
        from llm import usage as _usage
        kwargs: dict[str, Any] = {
            "model": self.model, "max_tokens": self.max_tokens,
            "system": self._system, "messages": self._messages,
            "cache_control": CACHE,     # автоматическая метка на хвосте разговора
        }
        if self._tools:
            kwargs["tools"] = self._tools
        if self.effort:
            kwargs["thinking"] = {"type": "adaptive"}
            kwargs["output_config"] = {"effort": self.effort}
        client = self._get_client()
        if hasattr(client, "with_options"):
            client = client.with_options(timeout=timeout)
        resp = await _usage.anthropic_create(client, depth=self.depth, entry=self.entry, **kwargs)

        content = list(getattr(resp, "content", None) or [])
        stop = getattr(resp, "stop_reason", None)
        if stop == "refusal":
            cat = getattr(getattr(resp, "stop_details", None), "category", None)
            raise LoopError("модель отказалась отвечать" + (f" (категория: {cat})" if cat else ""))
        # История — ответ целиком: блоки рассуждений и вызовов возвращаются как пришли
        self._messages.append({"role": "assistant", "content": content})
        calls = [ToolCall(id=b.id, name=b.name, args=dict(b.input or {}))
                 for b in content if getattr(b, "type", "") == "tool_use"]
        text = "".join(b.text for b in content if getattr(b, "type", "") == "text")
        if stop == "max_tokens":
            logger.warning("Claude: ответ обрезан по max_tokens=%s (модель %s)", self.max_tokens, self.model)
        return Turn(text=text, calls=calls,
                    usage=_usage.normalize_usage("anthropic", getattr(resp, "usage", None)),
                    stop="length" if stop == "max_tokens" else ("tools" if calls else "end"),
                    model=getattr(resp, "model", None) or self.model)

    async def aclose(self) -> None:
        if self._own_client and self._client is not None:
            client, self._client = self._client, None
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                logger.debug("Claude: закрытие клиента с ошибкой", exc_info=True)
