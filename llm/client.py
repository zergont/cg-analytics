# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Async streaming клиент Ollama для генерации текста."""
import asyncio
import json
import logging
from collections.abc import AsyncIterator

import httpx

logger = logging.getLogger(__name__)

# Ретраи при недоступности Ollama: попытки и базовая задержка (2с, 4с)
_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY_SEC = 2.0


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
    "base_url":    "http://localhost:11434",
    "model":       "qwen2.5:14b",
    "temperature": 0.1,
    "num_ctx":     16384,
    "stream":      True,   # False — модели без поддержки стриминга (например gemma)
}


def apply_llm_settings(
    base_url: str,
    model: str,
    temperature: float,
    num_ctx: int,
    stream: bool = True,
) -> None:
    """Обновить конфигурацию LLM в памяти (вступает в силу немедленно)."""
    _cfg["base_url"]    = base_url.rstrip("/")
    _cfg["model"]       = model.strip()
    _cfg["temperature"] = float(temperature)
    _cfg["num_ctx"]     = int(num_ctx)
    _cfg["stream"]      = bool(stream)
    logger.info("LLM настройки обновлены: model=%s url=%s num_ctx=%d stream=%s",
                _cfg["model"], _cfg["base_url"], _cfg["num_ctx"], _cfg["stream"])


def get_llm_settings() -> dict:
    """Вернуть копию текущей конфигурации LLM."""
    return dict(_cfg)


async def stream_analysis(md_packet: str) -> AsyncIterator[str]:
    """Отдать ответ модели токен за токеном (stream=True) или одним блоком (stream=False).

    Args:
        md_packet: Markdown-пакет телеметрии (выход _build_analysis_md)

    Yields:
        Строки-токены по мере генерации (или вся строка сразу при stream=False).
    """
    from llm.router import get_prompt
    system = get_prompt("analyze_page")

    use_stream = _cfg.get("stream", True)
    payload = {
        "model": _cfg["model"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": md_packet},
        ],
        "stream": use_stream,
        "options": {
            "temperature": _cfg["temperature"],
            "num_ctx":     _cfg["num_ctx"],
        },
    }

    logger.info("LLM запрос: model=%s, ctx=%d, prompt_len=%d, stream=%s",
                _cfg["model"], _cfg["num_ctx"], len(md_packet), use_stream)

    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        yielded = False  # после первого токена ретраить нельзя — часть ответа уже ушла клиенту
        try:
            if use_stream:
                async with httpx.AsyncClient(timeout=600.0) as client:
                    async with client.stream(
                        "POST",
                        f"{_cfg['base_url']}/api/chat",
                        json=payload,
                    ) as response:
                        response.raise_for_status()
                        async for line in response.aiter_lines():
                            if not line:
                                continue
                            try:
                                data = json.loads(line)
                                token = data.get("message", {}).get("content", "")
                                if token:
                                    yielded = True
                                    yield token
                                if data.get("done"):
                                    logger.info("LLM завершил генерацию: eval_count=%s",
                                                data.get("eval_count", "?"))
                                    return
                            except json.JSONDecodeError:
                                continue
                return
            else:
                # Модель не поддерживает стриминг — ждём полный ответ, отдаём одним блоком
                async with httpx.AsyncClient(timeout=600.0) as client:
                    resp = await client.post(
                        f"{_cfg['base_url']}/api/chat",
                        json=payload,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    content = data.get("message", {}).get("content", "")
                    logger.info("LLM завершил генерацию (no-stream): eval_count=%s",
                                data.get("eval_count", "?"))
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
