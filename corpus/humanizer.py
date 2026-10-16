"""Очеловечивание dry conclusion для UI оператора.

Правило изоляции: humanized_md идёт только в UI, НЕ в корпус.
В корпус (обучающие данные) идёт сухое заключение Claude (conclusion_md).

Кто пересказывает — строка «Пересказ для оператора» таблицы «Кто и как
разбирает» (v5.1.11); промпт — llm.router (human_auto / human_manual).
"""
from __future__ import annotations
import logging

import httpx

logger = logging.getLogger(__name__)


async def humanize(conclusion_md: str, task_id: str = "human_auto",
                   meta: dict | None = None) -> str:
    """Переписать сухое заключение в прозу для оператора.

    Кто разбирает — строка «Пересказ для оператора» таблицы (llm.router.get_route,
    v5.1.11); промпт — по task_id.
    Возвращает пустую строку при ошибке — не критично для пайплайна.

    v5.1.22 (В4): чужой язык в пересказе (#35624 — «风险信息…» и протёкшее
    «Final answer should be clean») — один проход «очистка / перевод» быстрой
    моделью; не принят — исходник с вычищенными иероглифами. Отметка — в
    meta["lang_check"] (воркер пишет её в debug_json).
    """
    if not conclusion_md:
        return ""

    block2_text = _extract_block2(conclusion_md)
    if not block2_text:
        return ""

    from llm.router import claude_model_for, format_ai_signature, get_prompt, get_route
    provider = get_route("humanize").get("provider", "llm")
    system_prompt = get_prompt(task_id)
    user_msg = f"Перепиши для оператора:\n\n{block2_text}"

    try:
        if provider == "api":
            model = claude_model_for("humanize")
            result = await _humanize_api(system_prompt, user_msg, model)
        else:
            from llm.client import gateway_label, get_llm_settings
            _gw: dict = {}
            result = await _humanize_llm(system_prompt, user_msg, _gw)
            model = gateway_label(_gw, get_llm_settings()["model"])
        if result:
            try:
                from llm.lang_check import clean_text
                from llm.router import get_depth
                result, _lang = await clean_text(result, block2_text,
                                                 priority=get_depth("humanize").get("priority"))
                if _lang and meta is not None:
                    meta["lang_check"] = _lang
            except Exception:
                logger.warning("corpus/humanizer: проверка языка не выполнена", exc_info=True)
        return result + format_ai_signature(model) if result else result
    except Exception as exc:
        logger.warning("corpus/humanizer: ошибка (некритично): %s", repr(exc))
        return ""


async def _humanize_llm(system_prompt: str, user_msg: str, meta: dict | None = None) -> str:
    from llm.client import chat

    # chat() сам ретраит сеть/429/5xx и знает текущего провайдера; у шлюза
    # «Горыныч» модель и приоритет — по градации («пересказ для оператора»)
    result = await chat(system_prompt, user_msg, depth="humanize", meta=meta)
    logger.debug("corpus/humanizer LLM: %d символов", len(result))
    return result


async def _humanize_api(system_prompt: str, user_msg: str, model: str | None = None) -> str:
    import anthropic
    from corpus.settings import get_claude_settings
    from config import settings as app_settings

    claude_cfg = get_claude_settings()
    # API ходит через прокси из настроек Claude (как corpus/agent и WarningGate)
    http_client = None
    if claude_cfg.get("proxy"):
        http_client = httpx.AsyncClient(proxy=claude_cfg["proxy"])
    try:
        client = anthropic.AsyncAnthropic(
            api_key=app_settings.anthropic_api_key,
            http_client=http_client,
            # SDK сам ретраит 429/5xx/сетевые ошибки с экспоненциальным backoff
            max_retries=3,
        )
        response = await client.messages.create(
            model=model or claude_cfg["model"],
            max_tokens=claude_cfg["max_tokens"],
            system=system_prompt,
            messages=[{"role": "user", "content": user_msg}],
        )
        result = "".join(b.text for b in response.content if hasattr(b, "text")).strip()
        logger.debug("corpus/humanizer API: %d символов", len(result))
        return result
    finally:
        if http_client:
            await http_client.aclose()


def _extract_block2(conclusion_md: str) -> str:
    """Извлечь Блок 2 (аналитика) из полного заключения.

    Поддерживает оба формата: легаси-маркеры «═══ БЛОК 3» и новый
    футер-курсив «*Модель: …» (v4.9.32+, перед ним стоит `---`).
    """
    lines = conclusion_md.split("\n")
    block2_lines: list[str] = []
    in_meta = True

    for line in lines:
        if "═══ БЛОК 3" in line or line.startswith("*Модель:"):
            break
        if line.startswith("## "):
            in_meta = False
        if not in_meta:
            block2_lines.append(line)

    # Отрезать хвостовой разделитель футера (--- перед «*Модель:»)
    while block2_lines and block2_lines[-1].strip() in ("", "---"):
        block2_lines.pop()

    text = "\n".join(block2_lines).strip()
    return text if text else conclusion_md
