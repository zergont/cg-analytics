"""Async streaming клиент Ollama для генерации текста."""
import json
import logging
from collections.abc import AsyncIterator

import httpx

from config import settings
from llm.prompts import ANALYSIS_SYSTEM_PROMPT

logger = logging.getLogger(__name__)


async def stream_analysis(md_packet: str) -> AsyncIterator[str]:
    """Стримить ответ модели токен за токеном.

    Args:
        md_packet: Markdown-пакет телеметрии (выход _build_analysis_md)

    Yields:
        Строки-токены по мере генерации.
    """
    payload = {
        "model": settings.llm_model,
        "messages": [
            {"role": "system", "content": ANALYSIS_SYSTEM_PROMPT},
            {"role": "user",   "content": md_packet},
        ],
        "stream": True,
        "options": {
            "temperature": settings.llm_temperature,
            "num_ctx":     settings.llm_num_ctx,
        },
    }

    logger.info("LLM запрос: model=%s, ctx=%d, prompt_len=%d",
                settings.llm_model, settings.llm_num_ctx, len(md_packet))

    async with httpx.AsyncClient(timeout=600.0) as client:
        async with client.stream(
            "POST",
            f"{settings.llm_base_url}/api/chat",
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
