# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""MCP-клиент хоста цикла: подключение cg-analytics к серверу телеметрии
(MCP Э2б; docs/plans/design_mcp_e2.md, разд. 3).

Сервер телеметрии (`telemetry_mcp`, отдельный процесс) про модели не знает; хост
берёт у него список инструментов и исполняет вызовы модели. Сквозной номер прогона
уходит заголовком `X-Task-Id` и в `_meta` каждого вызова: сервер по нему нумерует
метки [Tn] и пишет журнал `mcp_calls`.

Сбой сети разбор не роняет: вызов возвращает текст «ОШИБКА: сервер телеметрии
недоступен», модель видит его как результат инструмента; недоступность на входе
— `McpUnavailable`, цикл идёт без инструментов (fail-open).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

SOURCE = "analytics"
# Срок вызова: срок сервера (20 с) + запас на сеть
CALL_TIMEOUT_SEC = 25.0
CONNECT_TIMEOUT_SEC = 10.0
NETWORK_ERROR_TEXT = "ОШИБКА: сервер телеметрии недоступен"


class McpUnavailable(Exception):
    """Сервер телеметрии недоступен на входе в прогон."""


@dataclass(frozen=True)
class ToolSpec:
    """Инструмент сервера: имя, описание, схема аргументов (JSON Schema)."""
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass
class ToolResult:
    """Результат вызова: текст (первой строкой — конверт [Tn]) и картинки
    [(mime, base64)]. network_error — до сервера не дошли."""
    text: str
    images: list[tuple[str, str]] = field(default_factory=list)
    is_error: bool = False
    network_error: bool = False

    @property
    def chars(self) -> int:
        return len(self.text or "")


def to_anthropic(tools: list[ToolSpec]) -> list[dict[str, Any]]:
    """Схемы инструментов для Claude API — как есть."""
    return [{"name": t.name, "description": t.description, "input_schema": t.input_schema}
            for t in tools]


def simplify_schema(schema: Any) -> Any:
    """Упростить схему аргументов для провайдера со строгим разбором схем (Gemini):
    «значение или null» → само значение (необязательность уже задаёт список
    required), без title и умолчаний. Имена параметров не трогаются."""
    if isinstance(schema, list):
        return [simplify_schema(x) for x in schema]
    if not isinstance(schema, dict):
        return schema
    out: dict[str, Any] = {}
    for key, val in schema.items():
        if key in ("title", "default", "additionalProperties", "$schema"):
            continue
        if key == "properties" and isinstance(val, dict):
            out[key] = {name: simplify_schema(sub) for name, sub in val.items()}
            continue
        if key in ("anyOf", "oneOf") and isinstance(val, list):
            alts = [a for a in val if not (isinstance(a, dict) and a.get("type") == "null")]
            if len(alts) == 1 and isinstance(alts[0], dict):
                for k2, v2 in simplify_schema(alts[0]).items():
                    out.setdefault(k2, v2)
                continue
            out[key] = [simplify_schema(a) for a in alts]
            continue
        out[key] = simplify_schema(val)
    return out


def to_openai(tools: list[ToolSpec], *, simplify: bool = False) -> list[dict[str, Any]]:
    """Схемы инструментов для OpenAI-формата (OpenAI, DeepSeek, Gemini, шлюз).
    simplify — упрощённые схемы (Gemini)."""
    return [{"type": "function",
             "function": {"name": t.name, "description": t.description,
                          "parameters": simplify_schema(t.input_schema) if simplify else t.input_schema}}
            for t in tools]


def default_url() -> str:
    try:
        from telemetry_mcp.config import load
        return load().client_url
    except Exception:  # noqa: BLE001
        return "http://127.0.0.1:8095/mcp"


def _result(res: Any) -> ToolResult:
    texts: list[str] = []
    images: list[tuple[str, str]] = []
    for c in getattr(res, "content", None) or []:
        kind = getattr(c, "type", "")
        if kind == "text":
            texts.append(str(getattr(c, "text", "") or ""))
        elif kind == "image" and getattr(c, "data", None):
            mime = getattr(c, "mime_type", None) or getattr(c, "mimeType", None) or "image/png"
            images.append((str(mime), str(c.data)))
    text = "\n".join(t for t in texts if t)
    is_error = bool(getattr(res, "is_error", False) or getattr(res, "isError", False))
    return ToolResult(text=text, images=images,
                      is_error=is_error or text.startswith("ОШИБКА"))


class TelemetrySession:
    """Сервер телеметрии для одного прогона. Каждая операция (список, вызов) — своё
    короткое подключение: сервер без состояния, а транспорт библиотеки mcp при
    обрыве связи отменяет задачу, в которой открыт, — на одном подключении на весь
    прогон обрыв выглядел бы как остановка сервиса. Здесь он остаётся ошибкой сети
    одного вызова. Вход (`async with`) проверяет доступность и берёт список
    инструментов."""

    def __init__(self, run_id: str, *, url: str | None = None, source: str = SOURCE) -> None:
        self.run_id = run_id
        self.url = url or default_url()
        self.source = source
        self._tools: list[ToolSpec] | None = None

    async def _op(self, fn: Any) -> Any:
        """Открыть подключение, выполнить fn(session), закрыть. Сбой транспорта
        выходит отсюда обычным исключением (в т. ч. группой), отмена — отменой."""
        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

        http = create_mcp_http_client(headers={"X-Source": self.source, "X-Task-Id": self.run_id})
        async with streamable_http_client(self.url, http_client=http) as streams:
            read = getattr(streams, "read_stream", None) or streams[0]
            write = getattr(streams, "write_stream", None) or streams[1]
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await fn(session)

    async def __aenter__(self) -> "TelemetrySession":
        try:
            res = await asyncio.wait_for(self._op(lambda s: s.list_tools()), CONNECT_TIMEOUT_SEC)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 — в т. ч. группы исключений транспорта
            raise McpUnavailable(_brief(exc)) from None
        out = []
        for t in getattr(res, "tools", None) or []:
            schema = getattr(t, "input_schema", None) or getattr(t, "inputSchema", None) or {"type": "object"}
            out.append(ToolSpec(name=str(t.name), description=str(getattr(t, "description", "") or ""),
                                input_schema=dict(schema)))
        self._tools = sorted(out, key=lambda x: x.name)   # порядок постоянный: от него зависит кэш промпта
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        self._tools = None

    async def list_tools(self) -> list[ToolSpec]:
        if self._tools is None:
            raise McpUnavailable("подключение не открыто")
        return list(self._tools)

    async def call(self, name: str, args: dict[str, Any] | None) -> ToolResult:
        """Вызвать инструмент. Не бросает (кроме отмены задачи): сбой сети и срок —
        текст ошибки с признаком network_error."""
        meta = {"task_id": self.run_id, "source": self.source}
        try:
            res = await asyncio.wait_for(
                self._op(lambda s: s.call_tool(name, dict(args or {}), meta=meta)), CALL_TIMEOUT_SEC)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            logger.warning("MCP-клиент: %s не ответил за %.0f с (прогон %s)", name, CALL_TIMEOUT_SEC, self.run_id)
            return ToolResult(text=f"{NETWORK_ERROR_TEXT} (нет ответа за {CALL_TIMEOUT_SEC:.0f} с)",
                              is_error=True, network_error=True)
        except BaseException as exc:  # noqa: BLE001
            logger.warning("MCP-клиент: вызов %s не выполнен (прогон %s): %s", name, self.run_id, _brief(exc))
            return ToolResult(text=f"{NETWORK_ERROR_TEXT} ({_brief(exc, 80)})",
                              is_error=True, network_error=True)
        return _result(res)


def _brief(exc: BaseException, limit: int = 200) -> str:
    """Короткое имя причины; у группы исключений — первой вложенной."""
    inner = exc
    while getattr(inner, "exceptions", None):
        inner = inner.exceptions[0]
    return f"{type(inner).__name__}: {str(inner)}"[:limit]


async def probe(url: str | None = None) -> dict[str, Any]:
    """Состояние сервера телеметрии для карточки настроек: доступен ли, сколько
    инструментов. Не бросает."""
    target = url or default_url()
    try:
        async with TelemetrySession("probe", url=target, source=f"{SOURCE}-probe") as s:
            tools = await s.list_tools()
        return {"ok": True, "url": target, "tools": len(tools), "names": [t.name for t in tools]}
    except asyncio.CancelledError:
        raise
    except BaseException as exc:  # noqa: BLE001
        return {"ok": False, "url": target, "tools": 0, "names": [], "detail": str(exc)[:200]}
