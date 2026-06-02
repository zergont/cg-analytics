"""Очеловечивание dry conclusion для UI оператора через qwen.

Правило изоляции: humanized_md идёт только в UI, НЕ в корпус.
В корпус (обучающие данные) идёт сухое заключение Claude (conclusion_md).
"""
from __future__ import annotations
import logging

import httpx

logger = logging.getLogger(__name__)

_HUMANIZER_PROMPT = (
    "Ты переписываешь технический отчёт в понятный для оператора текст.\n\n"
    "СТРОГИЕ ПРАВИЛА:\n"
    "- Переформулируй ТОЛЬКО то, что написано во входящем тексте.\n"
    "- НЕ добавляй факты, выводы, рекомендации, которых нет во входе.\n"
    "- НЕ придумывай детали.\n"
    "- Пиши просто, понятно, без технического жаргона там где это возможно.\n"
    "- Сохраняй все выводы и рекомендации из исходника.\n"
    "- Не используй технические заголовки вроде 'БЛОК 1', 'БЛОК 2' — пиши связным текстом."
)


async def humanize(conclusion_md: str) -> str:
    """Переписать сухое заключение (Блок 2) в прозу для оператора через qwen.

    Использует существующую инфраструктуру llm/client.py (Ollama).
    Возвращает пустую строку при ошибке — не критично для пайплайна.
    """
    if not conclusion_md:
        return ""

    block2_text = _extract_block2(conclusion_md)
    if not block2_text:
        return ""

    try:
        from llm.client import _cfg

        payload = {
            "model": _cfg["model"],
            "messages": [
                {"role": "system", "content": _HUMANIZER_PROMPT},
                {"role": "user",   "content": f"Перепиши для оператора:\n\n{block2_text}"},
            ],
            "stream": False,
            "options": {
                "temperature": _cfg["temperature"],
                "num_ctx":     _cfg["num_ctx"],
            },
        }

        async with httpx.AsyncClient(timeout=300.0) as client:
            resp = await client.post(
                f"{_cfg['base_url']}/api/chat",
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            result = data.get("message", {}).get("content", "").strip()
            logger.debug("corpus/humanizer: получено %d символов", len(result))
            return result

    except Exception as e:
        logger.warning("corpus/humanizer: ошибка (некритично): %s", e)
        return ""


def _extract_block2(conclusion_md: str) -> str:
    """Извлечь Блок 2 (аналитика Claude) из полного заключения для передачи qwen."""
    lines = conclusion_md.split("\n")
    block2_lines: list[str] = []
    in_meta = True  # пропускаем Блок 1 до первого ##

    for line in lines:
        # Конец при встрече Блока 3
        if "═══ БЛОК 3" in line:
            break
        # Начало Блока 2 — первый заголовок ##
        if line.startswith("## "):
            in_meta = False
        if not in_meta:
            block2_lines.append(line)

    text = "\n".join(block2_lines).strip()
    return text if text else conclusion_md
