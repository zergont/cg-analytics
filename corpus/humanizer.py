"""Очеловечивание dry conclusion для UI оператора.

Правило изоляции: humanized_md идёт только в UI, НЕ в корпус.
В корпус (обучающие данные) идёт сухое заключение Claude (conclusion_md).

Провайдер и промпт берутся из llm.router (human_auto / human_manual).
"""
from __future__ import annotations
import logging

import httpx

logger = logging.getLogger(__name__)


async def humanize(conclusion_md: str, task_id: str = "human_auto") -> str:
    """Переписать сухое заключение в прозу для оператора.

    Провайдер берётся из llm.router по task_id (human_auto или human_manual).
    Возвращает пустую строку при ошибке — не критично для пайплайна.
    """
    if not conclusion_md:
        return ""

    block2_text = _extract_block2(conclusion_md)
    if not block2_text:
        return ""

    from llm.router import get_provider, get_prompt
    provider = get_provider(task_id)
    system_prompt = get_prompt(task_id)
    user_msg = f"Перепиши для оператора:\n\n{block2_text}"

    try:
        if provider == "api":
            return await _humanize_api(system_prompt, user_msg)
        else:
            return await _humanize_llm(system_prompt, user_msg)
    except Exception as exc:
        logger.warning("corpus/humanizer: ошибка (некритично): %s", repr(exc))
        return ""


async def _humanize_llm(system_prompt: str, user_msg: str) -> str:
    from llm.client import _cfg

    payload = {
        "model": _cfg["model"],
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_msg},
        ],
        "stream": False,
        "options": {
            "temperature": _cfg["temperature"],
            "num_ctx":     _cfg["num_ctx"],
        },
    }
    async with httpx.AsyncClient(timeout=300.0) as client:
        resp = await client.post(f"{_cfg['base_url']}/api/chat", json=payload)
        resp.raise_for_status()
        result = resp.json().get("message", {}).get("content", "").strip()
        logger.debug("corpus/humanizer LLM: %d символов", len(result))
        return result


async def _humanize_api(system_prompt: str, user_msg: str) -> str:
    import anthropic
    from corpus.settings import get_claude_settings
    from config import settings as app_settings

    claude_cfg = get_claude_settings()
    client = anthropic.AsyncAnthropic(api_key=app_settings.anthropic_api_key)
    response = await client.messages.create(
        model=claude_cfg["model"],
        max_tokens=claude_cfg["max_tokens"],
        system=system_prompt,
        messages=[{"role": "user", "content": user_msg}],
    )
    result = "".join(b.text for b in response.content if hasattr(b, "text")).strip()
    logger.debug("corpus/humanizer API: %d символов", len(result))
    return result


def _extract_block2(conclusion_md: str) -> str:
    """Извлечь Блок 2 (аналитика) из полного заключения."""
    lines = conclusion_md.split("\n")
    block2_lines: list[str] = []
    in_meta = True

    for line in lines:
        if "═══ БЛОК 3" in line:
            break
        if line.startswith("## "):
            in_meta = False
        if not in_meta:
            block2_lines.append(line)

    text = "\n".join(block2_lines).strip()
    return text if text else conclusion_md
