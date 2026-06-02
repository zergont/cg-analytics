"""Agentic loop через Anthropic API с tool use."""
import logging
from typing import Any, TYPE_CHECKING

import anthropic

from config import settings
from agent.tools import TOOLS
from agent.executor import execute_tool
from agent.prompt import build_system_prompt, build_user_prompt
from knowledge.retriever import retrieve_context

if TYPE_CHECKING:
    from pipeline.runner import RunContext

logger = logging.getLogger(__name__)


async def run(ctx: "RunContext") -> dict[str, Any]:
    """Запустить agentic loop и вернуть результат.

    Returns:
        {
            "report": str,          # итоговый текст отчёта
            "model": str,           # версия модели
            "tokens_used": int,     # суммарный расход токенов
            "tool_calls_count": int,
        }
    """
    import httpx as _httpx
    _http = _httpx.Client(proxy=settings.anthropic_proxy) if settings.anthropic_proxy else None
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key, http_client=_http)

    # RAG: извлечь релевантные описания регистров и fault-кодов
    active_addrs = list(ctx.history_series.keys())
    fault_addrs = [a["addr"] for a in ctx.anomalies if a.get("type") == "fault_bit"]
    rag_context = retrieve_context(ctx.kb_path, active_addrs, fault_addrs)

    system_prompt = build_system_prompt(ctx, rag_context)
    user_prompt = build_user_prompt(ctx)

    messages: list[dict] = [{"role": "user", "content": user_prompt}]

    total_tokens = 0
    tool_calls_count = 0

    logger.debug("Запуск agentic loop | макс. инструментов: %d", settings.max_tool_calls)

    while tool_calls_count < settings.max_tool_calls:
        response = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=settings.max_tokens,
            system=system_prompt,
            tools=TOOLS,
            messages=messages,
        )

        total_tokens += response.usage.input_tokens + response.usage.output_tokens

        if response.stop_reason == "end_turn":
            # Агент завершил анализ
            report_text = _extract_text(response.content)
            logger.info(
                "Агент завершил анализ | токены=%d | вызовов инструментов=%d",
                total_tokens, tool_calls_count,
            )
            return {
                "report": report_text,
                "model": response.model,
                "tokens_used": total_tokens,
                "tool_calls_count": tool_calls_count,
            }

        if response.stop_reason != "tool_use":
            # Неожиданная причина остановки
            logger.warning("Неожиданный stop_reason: %s", response.stop_reason)
            break

        # Выполнить все запрошенные инструменты
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            tool_calls_count += 1
            logger.info(
                "Вызов инструмента %d/%d: %s",
                tool_calls_count, settings.max_tool_calls, block.name,
            )

            result = execute_tool(block.name, block.input, ctx)

            # Результат может быть строкой или списком content blocks (для изображений)
            if isinstance(result, list):
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })
            else:
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": str(result),
                })

        # Добавить ответ агента и результаты инструментов в историю
        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    # Лимит инструментов исчерпан — запросить финальный отчёт
    logger.warning("Достигнут лимит вызовов инструментов (%d). Запрашиваю финальный отчёт.", settings.max_tool_calls)

    messages.append({
        "role": "user",
        "content": "Лимит вызовов инструментов исчерпан. Составь итоговый отчёт на основе уже собранных данных.",
    })

    final_response = client.messages.create(
        model=settings.anthropic_model,
        max_tokens=settings.max_tokens,
        system=system_prompt,
        messages=messages,
    )
    total_tokens += final_response.usage.input_tokens + final_response.usage.output_tokens

    return {
        "report": _extract_text(final_response.content),
        "model": final_response.model,
        "tokens_used": total_tokens,
        "tool_calls_count": tool_calls_count,
    }


def _extract_text(content: list) -> str:
    """Извлечь текст из content blocks ответа."""
    parts = []
    for block in content:
        if hasattr(block, "type") and block.type == "text":
            parts.append(block.text)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block["text"])
    return "\n".join(parts).strip()
