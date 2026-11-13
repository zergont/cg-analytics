# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Адаптер OpenAI-формата для цикла с инструментами: OpenAI, DeepSeek, Gemini и
прямой режим шлюза «Горыныч» (MCP Э2б; design_mcp_e2.md, разд. 4.3).

`POST {base_url}/v1/chat/completions` без потока, `tools` + `tool_choice`. Адрес,
ключ и особенности провайдера (температура, рассуждение, поля шлюза) собирает общий
клиент (`llm.client._build_request`) — здесь добавляются только инструменты и
история разговора.

Особенности:
- DeepSeek в режиме рассуждений требует возвращать `reasoning_content` ответа во
  всех следующих запросах разговора с инструментами (иначе 400) — сообщение модели
  кладётся в историю вместе с ним;
- Gemini привязывает вызовы инструментов к своим рассуждениям подписью
  (`extra_content` у вызова и у сообщения) и ждёт её обратно — сообщение модели
  кладётся в историю целиком, как пришло; схемы аргументов ему даются упрощёнными
  (без «значение или null», title и умолчаний);
- картинка графика (модели со зрением) идёт следующим сообщением за результатами:
  в сообщение `role: tool` картинку положить нельзя (Р21);
- после предела разбора список инструментов не меняется, а первый итоговый шаг идёт с
  прежним `tool_choice: auto`: смена этого поля сбрасывает кэш разговора у провайдера
  (приёмка 13.11.2026 — DeepSeek и Gemini пересчитали весь вход заново). `none`
  ставится, только если модель и после пометки снова просит инструменты.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import httpx

from llm.agent_loop import LoopError, ToolCall, Turn
from llm.mcp_client import ToolResult, ToolSpec, to_openai

logger = logging.getLogger(__name__)

# Повтор запроса: перегрузка и обрыв связи; остальное — сразу ошибка
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
# (приёмка 13.11: Gemini трижды подряд ответил 503 «высокая нагрузка» за 8 с — паузы длиннее)
_RETRY_DELAYS = (3.0, 10.0, 30.0)
# DeepSeek понимает только low / high / max
_DEEPSEEK_EFFORT = {"low": "low", "medium": "high", "high": "high", "xhigh": "max"}


class OpenAIChatAdapter:

    def __init__(self, *, entry: dict, model: str | None = None, depth: str | None = None,
                 priority: str | None = None) -> None:
        self.entry = entry
        self.provider = str(entry.get("provider") or "openai")
        self.model = model or entry.get("model") or ""
        self.depth = depth
        self.priority = priority
        self.vision = bool(entry.get("vision"))
        self._url = ""
        self._headers: dict[str, str] = {}
        self._base: dict[str, Any] = {}
        self._messages: list[dict[str, Any]] = []
        self._tools: list[dict[str, Any]] = []
        self._system_chars = 0
        self._final_steps = 0

    # ── разговор ─────────────────────────────────────────────────────────────

    def start(self, system: str, user: str, tools: list[ToolSpec]) -> None:
        from llm import client as llm_client
        url, payload, headers = llm_client._build_request(
            system, user, self.model, False, self.entry, None, self.priority)
        self._url, self._headers = url, headers
        self._messages = list(payload.pop("messages"))
        self._base = payload
        level = str(self.entry.get("reasoning") or "default")
        if self.provider == "deepseek" and level in _DEEPSEEK_EFFORT:
            self._base["reasoning_effort"] = _DEEPSEEK_EFFORT[level]
        self._tools = to_openai(tools, simplify=self.provider == "gemini")
        self._system_chars = len(self._messages[0].get("content") or "")

    def add_results(self, results: list[tuple[ToolCall, ToolResult]], note: str | None) -> None:
        pictures: list[dict[str, Any]] = []
        for call, res in results:
            self._messages.append({"role": "tool", "tool_call_id": call.id,
                                   "content": res.text or "(пусто)"})
            for mime, data in res.images:
                pictures.append({"type": "image_url",
                                 "image_url": {"url": f"data:{mime};base64,{data}"}})
        if pictures:
            self._messages.append({"role": "user", "content": [
                {"type": "text", "text": "Графики к результатам инструмента chart выше "
                                         "(числа — только из сводки результата)."}, *pictures]})
        if note:
            self._messages.append({"role": "user", "content": note})

    def add_note(self, note: str) -> None:
        self._messages.append({"role": "user", "content": note})

    # ── вызов ────────────────────────────────────────────────────────────────

    def _tool_choice(self, final: bool) -> str:
        """Выбор инструмента на шаге: до предела и на первом итоговом шаге — auto
        (кэш разговора цел), дальше — none."""
        if final:
            self._final_steps += 1
        return "none" if self._final_steps > 1 else "auto"

    def _http(self, timeout: float) -> httpx.AsyncClient:
        from llm import client as llm_client
        kw: dict[str, Any] = {"timeout": httpx.Timeout(timeout, connect=20.0)}
        if llm_client._TRANSPORT is not None:
            kw["transport"] = llm_client._TRANSPORT
        elif self.entry.get("proxy"):
            kw["proxy"] = self.entry["proxy"]
        return httpx.AsyncClient(**kw)

    def _input_chars(self) -> int:
        n = 0
        for m in self._messages:
            c = m.get("content")
            if isinstance(c, str):
                n += len(c)
            elif isinstance(c, list):
                n += sum(len(str(p.get("text") or "")) for p in c if isinstance(p, dict))
        return n

    async def _post(self, payload: dict[str, Any], timeout: float) -> httpx.Response:
        t_end = time.monotonic() + timeout
        last: BaseException | None = None
        for attempt in range(len(_RETRY_DELAYS) + 1):
            left = t_end - time.monotonic()
            if left <= 1:
                break
            try:
                async with self._http(left) as http:
                    resp = await http.post(self._url, json=payload, headers=self._headers)
                if resp.status_code not in _RETRY_STATUS:
                    return resp
                last = LoopError(f"{self.provider}: HTTP {resp.status_code}: {resp.text[:300]}")
            except httpx.TransportError as exc:
                last = exc
            if attempt < len(_RETRY_DELAYS):
                await asyncio.sleep(min(_RETRY_DELAYS[attempt], max(0.0, t_end - time.monotonic())))
        if isinstance(last, LoopError):
            raise last
        raise LoopError(f"{self.provider}: нет ответа ({type(last).__name__ if last else 'срок'})")

    async def step(self, *, final: bool, timeout: float) -> Turn:
        from llm import usage as _usage
        payload = {**self._base, "messages": self._messages}
        if self._tools:
            payload["tools"] = self._tools
            payload["tool_choice"] = self._tool_choice(final)
        t0 = time.monotonic()
        in_chars = self._input_chars()

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
            choice = (data.get("choices") or [{}])[0]
            message = dict(choice.get("message") or {})
        except Exception as exc:  # noqa: BLE001
            await _fail(f"ответ не разобран: {exc!r}")
            raise LoopError(f"{self.provider}: ответ не разобран") from None

        raw_calls = message.get("tool_calls") or []
        calls: list[ToolCall] = []
        for i, tc in enumerate(raw_calls):
            fn = (tc or {}).get("function") or {}
            args_raw = fn.get("arguments")
            bad = None
            try:
                args = json.loads(args_raw) if isinstance(args_raw, str) and args_raw.strip() else (args_raw or {})
                if not isinstance(args, dict):
                    args, bad = {}, "ожидался объект"
            except json.JSONDecodeError as exc:
                args, bad = {}, f"не JSON: {exc.msg}"
            calls.append(ToolCall(id=str(tc.get("id") or f"call_{i}"), name=str(fn.get("name") or ""),
                                  args=args, bad_args=bad))
        text = message.get("content") or ""
        if isinstance(text, list):       # части содержимого — склеить текстовые
            text = "".join(str(p.get("text") or "") for p in text if isinstance(p, dict))

        # История: сообщение модели как пришло (вызовы; у DeepSeek — и рассуждение)
        keep: dict[str, Any] = {"role": "assistant", "content": message.get("content")}
        if raw_calls:
            keep["tool_calls"] = raw_calls
        if self.provider == "deepseek" and message.get("reasoning_content") is not None:
            keep["reasoning_content"] = message["reasoning_content"]
        if self.provider == "gemini":
            # целиком: подписи рассуждений лежат в extra_content вызовов и самого сообщения
            keep = {**message, "role": "assistant"}
        self._messages.append(keep)

        usage = data.get("usage")
        g = data.get("gorynych") or {}
        finish = choice.get("finish_reason")
        await _usage.record(
            provider=self.provider, model=data.get("model") or self.model, entry=self.entry,
            depth=self.depth, backend=g.get("backend"), reasoning=self.entry.get("reasoning"),
            priority=self.priority if self.provider == "gorynych" else None,
            system_chars=self._system_chars, input_chars=in_chars, output_chars=len(text),
            usage=usage, duration_ms=int((time.monotonic() - t0) * 1000),
            status="ok" if (text or calls) else "empty",
            gateway_task_id=resp.headers.get("x-gorynych-task-id") or g.get("task_id"),
            tool_calls=len(calls) or None)
        return Turn(text=text, calls=calls, usage=_usage.normalize_usage(self.provider, usage),
                    stop="length" if finish == "length" else ("tools" if calls else "end"),
                    model=g.get("backend") or data.get("model") or self.model)

    async def aclose(self) -> None:
        return None
