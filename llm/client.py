# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Async клиент локальной LLM: Ollama или LM Studio (OpenAI-совместимый API).

Все обращения к локальной модели в проекте идут через chat_stream()/chat() —
провайдер и параметры берутся из _cfg (настраивается через веб-морду).
"""
import asyncio
import json
import logging
from collections.abc import AsyncIterator

import httpx

logger = logging.getLogger(__name__)

# Ретраи при недоступности LLM: попытки и базовая задержка (2с, 4с)
_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY_SEC = 2.0

_TIMEOUT_SEC = 600.0

PROVIDERS = ("ollama", "lmstudio")


def retriable_llm_error(exc: Exception) -> bool:
    """Стоит ли повторять запрос к LLM: сетевые ошибки, 429 и 5xx."""
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or code >= 500
    return False


# ── In-memory конфигурация LLM (применяется при старте и через веб-морду) ──────
# Системные промпты живут в AI-роутере (llm/router.py) — здесь только подключение.
_cfg: dict = {
    "provider":    "ollama",   # ollama | lmstudio (OpenAI-совместимый API)
    "base_url":    "http://localhost:11434",
    "model":       "qwen2.5:14b",
    "temperature": 0.1,
    "num_ctx":     16384,      # только Ollama; в LM Studio контекст задаётся при загрузке модели
    "stream":      True,       # False — модели без поддержки стриминга (например gemma)
}


def apply_llm_settings(
    base_url: str,
    model: str,
    temperature: float,
    num_ctx: int,
    stream: bool = True,
    provider: str = "ollama",
) -> None:
    """Обновить конфигурацию LLM в памяти (вступает в силу немедленно)."""
    _cfg["provider"]    = provider if provider in PROVIDERS else "ollama"
    _cfg["base_url"]    = base_url.rstrip("/")
    _cfg["model"]       = model.strip()
    _cfg["temperature"] = float(temperature)
    _cfg["num_ctx"]     = int(num_ctx)
    _cfg["stream"]      = bool(stream)
    logger.info("LLM настройки обновлены: provider=%s model=%s url=%s num_ctx=%d stream=%s",
                _cfg["provider"], _cfg["model"], _cfg["base_url"], _cfg["num_ctx"], _cfg["stream"])


def get_llm_settings() -> dict:
    """Вернуть копию текущей конфигурации LLM."""
    return dict(_cfg)


# ── Запрос/разбор по провайдерам ───────────────────────────────────────────────

def _build_request(system: str, user: str, model: str | None, stream: bool) -> tuple[str, dict]:
    """URL и payload под текущего провайдера."""
    messages = [
        {"role": "system", "content": system},
        {"role": "user",   "content": user},
    ]
    mdl = (model or _cfg["model"]).strip()
    if _cfg.get("provider") == "lmstudio":
        # OpenAI-совместимый API: длина контекста задаётся при загрузке модели в LM Studio
        return f"{_cfg['base_url']}/v1/chat/completions", {
            "model":       mdl,
            "messages":    messages,
            "stream":      stream,
            "temperature": _cfg["temperature"],
        }
    return f"{_cfg['base_url']}/api/chat", {
        "model":    mdl,
        "messages": messages,
        "stream":   stream,
        "options": {
            "temperature": _cfg["temperature"],
            "num_ctx":     _cfg["num_ctx"],
        },
    }


async def _iter_ollama_stream(response) -> AsyncIterator[str]:
    """NDJSON-стрим Ollama: по JSON-объекту на строку, конец — done=true."""
    async for line in response.aiter_lines():
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        token = data.get("message", {}).get("content", "")
        if token:
            yield token
        if data.get("done"):
            logger.info("LLM завершил генерацию: eval_count=%s", data.get("eval_count", "?"))
            return


async def _iter_openai_stream(response) -> AsyncIterator[str]:
    """SSE-стрим OpenAI-совместимого API (LM Studio): 'data: {...}', конец — 'data: [DONE]'."""
    async for line in response.aiter_lines():
        if not line or not line.startswith("data:"):
            continue
        data_str = line[5:].strip()
        if data_str == "[DONE]":
            logger.info("LLM завершил генерацию (lmstudio)")
            return
        try:
            data = json.loads(data_str)
        except json.JSONDecodeError:
            continue
        choices = data.get("choices") or []
        if choices:
            token = (choices[0].get("delta") or {}).get("content", "")
            if token:
                yield token


def _extract_content(data: dict) -> str:
    """Текст ответа из non-stream JSON под текущего провайдера."""
    if _cfg.get("provider") == "lmstudio":
        choices = data.get("choices") or []
        return (choices[0].get("message") or {}).get("content", "") if choices else ""
    return data.get("message", {}).get("content", "")


# ── Публичный API ──────────────────────────────────────────────────────────────

async def chat_stream(
    system: str,
    user: str,
    *,
    model: str | None = None,
    stream: bool | None = None,
) -> AsyncIterator[str]:
    """Ответ локальной LLM токен за токеном (или одним блоком при stream=False).

    Единая точка вызова для всех потребителей (ручной анализ, humanizer,
    corpus-воркер, playground). Ретраит сеть/429/5xx, но только до первого
    отданного токена — часть ответа уже у клиента.
    """
    use_stream = _cfg.get("stream", True) if stream is None else stream
    url, payload = _build_request(system, user, model, use_stream)

    logger.info("LLM запрос: provider=%s model=%s prompt_len=%d stream=%s",
                _cfg.get("provider"), payload["model"], len(user), use_stream)

    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        yielded = False
        try:
            if use_stream:
                async with httpx.AsyncClient(timeout=_TIMEOUT_SEC) as client:
                    async with client.stream("POST", url, json=payload) as response:
                        response.raise_for_status()
                        it = (_iter_openai_stream(response)
                              if _cfg.get("provider") == "lmstudio"
                              else _iter_ollama_stream(response))
                        async for token in it:
                            yielded = True
                            yield token
                return
            else:
                async with httpx.AsyncClient(timeout=_TIMEOUT_SEC) as client:
                    resp = await client.post(url, json=payload)
                    resp.raise_for_status()
                    content = _extract_content(resp.json())
                    logger.info("LLM завершил генерацию (no-stream): %d символов", len(content))
                    if content:
                        yield content
                return
        except Exception as exc:
            if yielded or attempt == _RETRY_ATTEMPTS or not retriable_llm_error(exc):
                raise
            delay = _RETRY_BASE_DELAY_SEC * attempt
            logger.warning("LLM недоступен (попытка %d/%d), повтор через %.0fс: %r",
                           attempt, _RETRY_ATTEMPTS, delay, exc)
            await asyncio.sleep(delay)


async def chat(system: str, user: str, *, model: str | None = None) -> str:
    """Ответ локальной LLM одной строкой (non-stream, с ретраями)."""
    parts = [t async for t in chat_stream(system, user, model=model, stream=False)]
    return "".join(parts).strip()


async def stream_analysis(md_packet: str) -> AsyncIterator[str]:
    """Заключение по Markdown-пакету телеметрии (страница ручного анализа).

    Args:
        md_packet: Markdown-пакет телеметрии (выход _build_analysis_md)

    Yields:
        Строки-токены по мере генерации (или вся строка сразу при stream=False).
    """
    from llm.router import get_prompt
    system = get_prompt("analyze_page")
    async for token in chat_stream(system, md_packet):
        yield token
