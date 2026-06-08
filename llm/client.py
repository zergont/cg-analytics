# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Async streaming клиент Ollama для генерации текста."""
import json
import logging
from collections.abc import AsyncIterator

import httpx

from llm.prompts import ANALYSIS_SYSTEM_PROMPT

logger = logging.getLogger(__name__)

# ── In-memory конфигурация LLM (применяется при старте и через веб-морду) ──────
_cfg: dict = {
    "base_url":    "http://localhost:11434",
    "model":       "qwen2.5:14b",
    "temperature": 0.1,
    "num_ctx":     16384,
    "prompt":      ANALYSIS_SYSTEM_PROMPT,
}


def apply_llm_settings(
    base_url: str,
    model: str,
    temperature: float,
    num_ctx: int,
    prompt: str,
) -> None:
    """Обновить конфигурацию LLM в памяти (вступает в силу немедленно)."""
    _cfg["base_url"]    = base_url.rstrip("/")
    _cfg["model"]       = model.strip()
    _cfg["temperature"] = float(temperature)
    _cfg["num_ctx"]     = int(num_ctx)
    _cfg["prompt"]      = prompt.strip()
    logger.info("LLM настройки обновлены: model=%s url=%s", _cfg["model"], _cfg["base_url"])


def get_llm_settings() -> dict:
    """Вернуть копию текущей конфигурации LLM."""
    return dict(_cfg)


async def stream_analysis(md_packet: str) -> AsyncIterator[str]:
    """Стримить ответ модели токен за токеном.

    Args:
        md_packet: Markdown-пакет телеметрии (выход _build_analysis_md)

    Yields:
        Строки-токены по мере генерации.
    """
    payload = {
        "model": _cfg["model"],
        "messages": [
            {"role": "system", "content": _cfg["prompt"]},
            {"role": "user",   "content": md_packet},
        ],
        "stream": True,
        "options": {
            "temperature": _cfg["temperature"],
            "num_ctx":     _cfg["num_ctx"],
        },
    }

    logger.info("LLM запрос: model=%s, ctx=%d, prompt_len=%d",
                _cfg["model"], _cfg["num_ctx"], len(md_packet))

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
                        yield token
                    if data.get("done"):
                        logger.info("LLM завершил генерацию: eval_count=%s",
                                    data.get("eval_count", "?"))
                        return
                except json.JSONDecodeError:
                    continue
