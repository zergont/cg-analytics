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

# Провайдеры с OpenAI-совместимым API (/v1/chat/completions):
# LM Studio, llama-server (llama.cpp), DeepSeek
_OPENAI_COMPAT = ("lmstudio", "llamacpp", "deepseek")


def _build_request(
    system: str, user: str, model: str | None, stream: bool, cfg: dict,
) -> tuple[str, dict, dict]:
    """URL, payload и заголовки под провайдера из cfg (глобальный _cfg или запись реестра)."""
    messages = [
        {"role": "system", "content": system},
        {"role": "user",   "content": user},
    ]
    mdl = (model or cfg["model"]).strip()
    headers: dict = {}
    if cfg.get("api_key"):
        headers["Authorization"] = f"Bearer {cfg['api_key']}"
    if cfg.get("provider") in _OPENAI_COMPAT:
        # OpenAI-совместимый API: LM Studio (контекст задаётся при загрузке модели) и DeepSeek
        return f"{cfg['base_url']}/v1/chat/completions", {
            "model":       mdl,
            "messages":    messages,
            "stream":      stream,
            "temperature": cfg["temperature"],
        }, headers
    return f"{cfg['base_url']}/api/chat", {
        "model":    mdl,
        "messages": messages,
        "stream":   stream,
        "options": {
            "temperature": cfg["temperature"],
            "num_ctx":     cfg["num_ctx"],
        },
    }, headers


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


def _extract_content(data: dict, cfg: dict) -> str:
    """Текст ответа из non-stream JSON под провайдера из cfg."""
    if cfg.get("provider") in _OPENAI_COMPAT:
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
    entry: dict | None = None,
) -> AsyncIterator[str]:
    """Ответ LLM токен за токеном (или одним блоком при stream=False).

    Единая точка вызова для всех потребителей (ручной анализ, humanizer,
    corpus-воркер, playground). Ретраит сеть/429/5xx, но только до первого
    отданного токена — часть ответа уже у клиента.

    entry — запись реестра моделей (llm.registry): параметры подключения
    берутся из неё, лимит одновременных запросов — её семафор. Без entry —
    глобальный _cfg (legacy-путь, настройка «LLM — локальная модель»).
    """
    cfg = entry if entry is not None else _cfg
    use_stream = cfg.get("stream", True) if stream is None else stream
    url, payload, headers = _build_request(system, user, model, use_stream, cfg)

    logger.info("LLM запрос: provider=%s model=%s prompt_len=%d stream=%s",
                cfg.get("provider"), payload["model"], len(user), use_stream)

    if entry is not None:
        from llm.registry import get_semaphore
        sem = get_semaphore(entry["id"])
    else:
        sem = None

    if sem is not None:
        await sem.acquire()
    try:
        for attempt in range(1, _RETRY_ATTEMPTS + 1):
            yielded = False
            try:
                if use_stream:
                    async with httpx.AsyncClient(timeout=_TIMEOUT_SEC) as client:
                        async with client.stream("POST", url, json=payload, headers=headers) as response:
                            response.raise_for_status()
                            it = (_iter_openai_stream(response)
                                  if cfg.get("provider") in _OPENAI_COMPAT
                                  else _iter_ollama_stream(response))
                            async for token in it:
                                yielded = True
                                yield token
                    return
                else:
                    async with httpx.AsyncClient(timeout=_TIMEOUT_SEC) as client:
                        resp = await client.post(url, json=payload, headers=headers)
                        resp.raise_for_status()
                        content = _extract_content(resp.json(), cfg)
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
    finally:
        if sem is not None:
            sem.release()


async def chat(
    system: str, user: str, *, model: str | None = None,
    entry: dict | None = None, stream: bool | None = None,
) -> str:
    """Ответ LLM одной строкой (с ретраями).

    stream=None — как задано в конфигурации подключения; для цепочек corpus
    передаётся stream=True: поток токенов служит признаком «сервер жив»,
    таймаут httpx становится межтокенным, а не на весь ответ.
    """
    use_stream = stream if stream is not None else False
    parts = [t async for t in chat_stream(system, user, model=model,
                                          stream=use_stream, entry=entry)]
    return "".join(parts).strip()


async def list_server_models(provider: str, base_url: str, api_key: str = "") -> list[str]:
    """Имена моделей, которые отдаёт сервер: Ollama — /api/tags, остальные — /v1/models.

    Бросает исключение при недоступности; для anthropic список не запрашивается.
    """
    if provider == "anthropic":
        return []
    base_url = base_url.rstrip("/")
    url = f"{base_url}/api/tags" if provider == "ollama" else f"{base_url}/v1/models"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    if provider == "ollama":
        return [m.get("name", "") for m in data.get("models", []) if m.get("name")]
    return [m.get("id", "") for m in data.get("data", []) if m.get("id")]


async def ping_entry(entry: dict) -> dict:
    """Проверка доступности подключения из реестра (кнопка «Проверить» в UI).

    Возвращает {"ok": bool, "detail": str}. Модель не дёргаем — только список
    моделей эндпоинта; для anthropic проверяем лишь наличие ключа в конфигурации.
    """
    provider = entry.get("provider")
    if provider == "anthropic":
        import os
        from config import settings as _app_settings
        has_key = bool(_app_settings.anthropic_api_key
                       or os.environ.get("ANTHROPIC_API_KEY")
                       or entry.get("api_key"))
        return {"ok": has_key,
                "detail": "ключ API найден (config.yml)" if has_key
                          else "ключ не задан ни в config.yml, ни в ANTHROPIC_API_KEY"}
    try:
        models = await list_server_models(provider, entry["base_url"], entry.get("api_key", ""))
    except Exception as exc:
        return {"ok": False, "detail": repr(exc)}
    if entry.get("model") and entry["model"] not in models:
        shown = ", ".join(models[:5]) + (" …" if len(models) > 5 else "")
        return {"ok": True,
                "detail": f"сервер доступен, но модель «{entry['model']}» не найдена. "
                          f"Сервер отдаёт: {shown or '—'}"}
    return {"ok": True, "detail": f"доступен, моделей: {len(models)}"}


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
