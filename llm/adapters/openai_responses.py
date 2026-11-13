# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Адаптер OpenAI Responses API для цикла с инструментами (MCP Э2б).

Новые модели OpenAI не принимают инструменты вместе с рассуждением через
`/v1/chat/completions` (ответ 400: «use /v1/responses»), поэтому цикл ходит к OpenAI
через `POST /v1/responses` (приёмка 13.11.2026, gpt-6.1-sol). Заключения без
инструментов по-прежнему идут общим клиентом через chat/completions.

Отличия формата от chat/completions:
- системный промпт — поле `instructions`, разговор — список `input`;
- ответ — список `output`: рассуждение (`reasoning`), вызовы (`function_call` с
  `call_id`), текст (`message` с частями `output_text`). В историю он кладётся
  целиком и в исходном порядке: рассуждения обязаны вернуться вместе с вызовами;
- результат инструмента — элемент `function_call_output` с тем же `call_id`;
- картинка графика — следующим сообщением пользователя частью `input_image`;
- разговор на стороне OpenAI не хранится (`store: false`): рассуждения возвращаются
  зашифрованными внутри своих элементов;
- расход: `input_tokens` включает кэш (`input_tokens_details.cached_tokens`),
  `output_tokens` — рассуждения (`output_tokens_details.reasoning_tokens`).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from llm.adapters.openai_chat import OpenAIChatAdapter
from llm.agent_loop import LoopError, ToolCall, Turn
from llm.mcp_client import ToolResult, ToolSpec

logger = logging.getLogger(__name__)

# Уровень рассуждения записи → reasoning.effort. «Выкл» новые модели отвергают — низший уровень
_EFFORT = {"off": "low", "low": "low", "medium": "medium", "high": "high", "xhigh": "high"}


class OpenAIResponsesAdapter(OpenAIChatAdapter):

    def __init__(self, *, entry: dict, model: str | None = None, depth: str | None = None,
                 priority: str | None = None) -> None:
        super().__init__(entry=entry, model=model, depth=depth, priority=priority)
        self._instructions = ""
        self._input: list[dict[str, Any]] = []
        self._effort = _EFFORT.get(str(entry.get("reasoning") or "default"))

    # ── разговор ─────────────────────────────────────────────────────────────

    def start(self, system: str, user: str, tools: list[ToolSpec]) -> None:
        self._url = f"{self.entry['base_url']}/v1/responses"
        self._headers = ({"Authorization": f"Bearer {self.entry['api_key']}"}
                         if self.entry.get("api_key") else {})
        self._instructions = system
        self._system_chars = len(system or "")
        self._input = [{"role": "user", "content": user}]
        self._tools = [{"type": "function", "name": t.name, "description": t.description,
                        "parameters": t.input_schema} for t in tools]

    def add_results(self, results: list[tuple[ToolCall, ToolResult]], note: str | None) -> None:
        pictures: list[dict[str, Any]] = []
        for call, res in results:
            self._input.append({"type": "function_call_output", "call_id": call.id,
                                "output": res.text or "(пусто)"})
            for mime, data in res.images:
                pictures.append({"type": "input_image", "image_url": f"data:{mime};base64,{data}"})
        if pictures:
            self._input.append({"role": "user", "content": [
                {"type": "input_text", "text": "Графики к результатам инструмента chart выше "
                                               "(числа — только из сводки результата)."}, *pictures]})
        if note:
            self._input.append({"role": "user", "content": note})

    def add_note(self, note: str) -> None:
        self._input.append({"role": "user", "content": note})

    def _input_chars(self) -> int:
        n = 0
        for item in self._input:
            for key in ("content", "output", "arguments"):
                val = item.get(key)
                if isinstance(val, str):
                    n += len(val)
                elif isinstance(val, list):
                    n += sum(len(str(p.get("text") or "")) for p in val if isinstance(p, dict))
        return n

    # ── вызов ────────────────────────────────────────────────────────────────

    async def step(self, *, final: bool, timeout: float) -> Turn:
        from llm import usage as _usage
        payload: dict[str, Any] = {"model": self.model, "instructions": self._instructions,
                                   "input": self._input, "store": False}
        if self._tools:
            payload["tools"] = self._tools
            payload["tool_choice"] = self._tool_choice(final)
        if self._effort:
            payload["reasoning"] = {"effort": self._effort}
        t0 = time.monotonic()
        in_chars = self._system_chars + self._input_chars()

        async def _fail(msg: str) -> None:
            await _usage.record(provider=self.provider, model=self.model, entry=self.entry,
                                depth=self.depth, system_chars=self._system_chars, input_chars=in_chars,
                                duration_ms=int((time.monotonic() - t0) * 1000),
                                status="error", error=msg[:300])

        try:
            resp = await self._post(payload, timeout)
        except asyncio.CancelledError:
            raise
        except LoopError as exc:
            await _fail(str(exc))
            raise
        if resp.status_code >= 400:
            msg = f"{self.provider}: HTTP {resp.status_code}: {resp.text[:300]}"
            await _fail(msg)
            raise LoopError(msg)
        try:
            data = resp.json()
            output = [item for item in (data.get("output") or []) if isinstance(item, dict)]
        except Exception as exc:  # noqa: BLE001
            await _fail(f"ответ не разобран: {exc!r}")
            raise LoopError(f"{self.provider}: ответ не разобран") from None
        if data.get("error"):
            msg = f"{self.provider}: {json.dumps(data['error'], ensure_ascii=False)[:300]}"
            await _fail(msg)
            raise LoopError(msg)

        calls: list[ToolCall] = []
        texts: list[str] = []
        refusal: str | None = None
        for i, item in enumerate(output):
            kind = item.get("type")
            if kind == "function_call":
                raw = item.get("arguments")
                bad = None
                try:
                    args = json.loads(raw) if isinstance(raw, str) and raw.strip() else (raw or {})
                    if not isinstance(args, dict):
                        args, bad = {}, "ожидался объект"
                except json.JSONDecodeError as exc:
                    args, bad = {}, f"не JSON: {exc.msg}"
                calls.append(ToolCall(id=str(item.get("call_id") or item.get("id") or f"call_{i}"),
                                      name=str(item.get("name") or ""), args=args, bad_args=bad))
            elif kind == "message":
                for part in item.get("content") or []:
                    if isinstance(part, dict) and part.get("type") == "output_text":
                        texts.append(str(part.get("text") or ""))
                    elif isinstance(part, dict) and part.get("type") == "refusal":
                        refusal = str(part.get("refusal") or "")
        text = "".join(texts)
        # История — ответ целиком и в исходном порядке (рассуждения вместе с вызовами)
        self._input.extend(output)

        incomplete = data.get("status") == "incomplete"
        reason = (data.get("incomplete_details") or {}).get("reason") if incomplete else None
        usage = data.get("usage")
        await _usage.record(
            provider=self.provider, model=data.get("model") or self.model, entry=self.entry,
            depth=self.depth, reasoning=self.entry.get("reasoning"),
            system_chars=self._system_chars, input_chars=in_chars, output_chars=len(text),
            usage=usage, duration_ms=int((time.monotonic() - t0) * 1000),
            status="ok" if (text or calls) else "empty", tool_calls=len(calls) or None)
        if refusal is not None and not calls and not text:
            raise LoopError(f"модель отказалась отвечать: {refusal[:200]}")
        if incomplete and reason and reason != "max_output_tokens":
            raise LoopError(f"{self.provider}: ответ не завершён ({reason})")
        return Turn(text=text, calls=calls, usage=_usage.normalize_usage(self.provider, usage),
                    stop="length" if incomplete else ("tools" if calls else "end"),
                    model=data.get("model") or self.model)
