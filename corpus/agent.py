# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Agentic loop через Anthropic AsyncAPI для анализа одного сегмента."""
from __future__ import annotations
import logging
import time
from typing import Any

import anthropic
import httpx

from config import settings
from corpus.settings import get_claude_settings
from corpus.tools import TOOLS
from corpus.executor import execute_tool
from corpus.prompt import build_user_message
from corpus.preprocessor import build_claude_input, extract_verdict_alarm

logger = logging.getLogger(__name__)


async def analyse_segment(
    segment_row: dict,
    kb_path: str | None,
    system_prompt: str | None = None,
) -> dict[str, Any]:
    """Прогнать сегмент через Claude API.

    system_prompt — если передан, используется вместо промпта из corpus/settings.
    Returns dict со всеми полями для corpus/db.upsert_analysis():
        conclusion_md, verdict, alarm_level, claude_model,
        tokens_used, tool_calls_count, loops_count,
        generation_time_sec, debug_json, error
    """
    t0 = time.monotonic()
    seg_id = segment_row.get("id")
    claude_cfg = get_claude_settings()

    # Формируем вход и извлекаем вердикт до вызова API
    claude_input = build_claude_input(segment_row)
    verdict, alarm_level = extract_verdict_alarm(segment_row)

    debug: dict[str, Any] = {
        "segment_id": seg_id,
        "input_length": len(claude_input),
        "iterations": [],
        "tokens_input": 0,
        "tokens_output": 0,
        "total_tokens": 0,
    }

    http_client: httpx.AsyncClient | None = None
    if claude_cfg["proxy"]:
        http_client = httpx.AsyncClient(proxy=claude_cfg["proxy"])

    try:
        client = anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key,
            http_client=http_client,
            # SDK сам ретраит 429/5xx/сетевые ошибки с экспоненциальным backoff
            max_retries=4,
        )

        messages: list[dict] = [
            {"role": "user", "content": build_user_message(claude_input)}
        ]

        tool_calls_count = 0
        loops_count = 0
        final_text = ""
        _model          = claude_cfg["model"]
        _max_tool_calls = claude_cfg["max_tool_calls"]
        _max_tokens     = claude_cfg["max_tokens"]
        from llm.router import get_prompt as _router_prompt
        _system_prompt  = system_prompt or _router_prompt("seg_auto")

        while tool_calls_count < _max_tool_calls:
            loops_count += 1
            iter_info: dict[str, Any] = {"loop": loops_count}

            response = await client.messages.create(
                model=_model,
                max_tokens=_max_tokens,
                system=_system_prompt,
                tools=TOOLS,
                messages=messages,
            )

            debug["tokens_input"]  += response.usage.input_tokens
            debug["tokens_output"] += response.usage.output_tokens
            iter_info["stop_reason"] = response.stop_reason

            if response.stop_reason == "end_turn":
                final_text = _extract_text(response.content)
                debug["iterations"].append(iter_info)
                break

            if response.stop_reason != "tool_use":
                logger.warning(
                    "corpus/agent seg#%s: неожиданный stop_reason=%s",
                    seg_id, response.stop_reason,
                )
                debug["iterations"].append(iter_info)
                break

            # Выполнить тулы
            tool_results = []
            tools_used: list[dict] = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                tool_calls_count += 1
                logger.info(
                    "corpus/agent seg#%s: тул %s [%d/%d], input: %s",
                    seg_id, block.name, tool_calls_count, _max_tool_calls,
                    str(block.input)[:120],
                )
                result = execute_tool(block.name, block.input, kb_path)
                tools_used.append({
                    "name": block.name,
                    "input": block.input,
                    "result_length": len(result) if isinstance(result, str) else 0,
                })
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": str(result),
                })

            iter_info["tools"] = tools_used
            debug["iterations"].append(iter_info)

            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})

        else:
            # Лимит тулов — запросить финальный ответ без тулов
            logger.warning(
                "corpus/agent seg#%s: достигнут лимит тулов (%d), финальный запрос",
                seg_id, _max_tool_calls,
            )
            messages.append({
                "role": "user",
                "content": (
                    "Лимит инструментов исчерпан. "
                    "Составь итоговое заключение на основе уже собранных данных."
                ),
            })
            final_resp = await client.messages.create(
                model=_model,
                max_tokens=_max_tokens,
                system=_system_prompt,
                messages=messages,
            )
            debug["tokens_input"]  += final_resp.usage.input_tokens
            debug["tokens_output"] += final_resp.usage.output_tokens
            final_text = _extract_text(final_resp.content)

        debug["total_tokens"] = debug["tokens_input"] + debug["tokens_output"]

        # Собираем полное структурированное заключение (Блоки 1+2+3)
        conclusion_md = _build_conclusion(
            segment_row=segment_row,
            verdict=verdict,
            alarm_level=alarm_level,
            claude_block2=final_text,
            model=_model,
        )

        elapsed = time.monotonic() - t0
        logger.info(
            "corpus/agent seg#%s: завершён | токены=%d (in=%d out=%d) | "
            "тулы=%d | петли=%d | %.1fс",
            seg_id,
            debug["total_tokens"], debug["tokens_input"], debug["tokens_output"],
            tool_calls_count, loops_count, elapsed,
        )

        return {
            "conclusion_md":      conclusion_md,
            "verdict":            verdict,
            "alarm_level":        alarm_level,
            "claude_model":       _model,
            "tokens_used":        debug["total_tokens"],
            "tool_calls_count":   tool_calls_count,
            "loops_count":        loops_count,
            "generation_time_sec": round(elapsed, 2),
            "debug_json":         debug,
            "error":              None,
        }

    except Exception as e:
        elapsed = time.monotonic() - t0
        logger.exception("corpus/agent seg#%s: ошибка", seg_id)
        debug["total_tokens"] = debug["tokens_input"] + debug["tokens_output"]
        return {
            "conclusion_md":      None,
            "verdict":            verdict,
            "alarm_level":        alarm_level,
            "claude_model":       claude_cfg["model"],
            "tokens_used":        debug["total_tokens"],
            "tool_calls_count":   0,
            "loops_count":        loops_count if "loops_count" in dir() else 0,
            "generation_time_sec": round(elapsed, 2),
            "debug_json":         debug,
            "error":              str(e),
        }

    finally:
        if http_client:
            await http_client.aclose()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_text(content: list) -> str:
    """Извлечь текст из content blocks ответа Anthropic."""
    parts = []
    for block in content:
        if hasattr(block, "type") and block.type == "text":
            parts.append(block.text)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block["text"])
    return "\n".join(parts).strip()


def _build_conclusion(
    segment_row: dict,
    verdict: str,
    alarm_level: str,
    claude_block2: str,
    model: str,
) -> str:
    """Собрать полное структурированное заключение: Блок1 + Блок2 + Блок3."""
    from datetime import datetime, timezone
    from corpus.preprocessor import (
        ALARM_LEVEL_META, RUN_STATE_RU,
        _extract_detections, _fmt_detections_hierarchy, _gate_suppressed,
    )

    try:
        from analytics.runner import ANALYTICS_VERSION
    except Exception:
        ANALYTICS_VERSION = "3.0.0"

    run_state = segment_row.get("run_state")
    run_state_label = (
        RUN_STATE_RU.get(run_state, str(run_state))
        if run_state is not None else "—"
    )

    chars_json = segment_row.get("characteristics_json")
    dets_block = _fmt_detections_hierarchy(chars_json)

    emoji, level_ru = ALARM_LEVEL_META.get(alarm_level, ("⚪", alarm_level))
    level_note = level_ru
    if alarm_level == "НОРМА" and _gate_suppressed(
        segment_row, _extract_detections(chars_json)
    ):
        level_note = "предупреждения аналитики сняты ИИ"

    # Сегмент закрыт по устранению неисправностей → строка MTTR
    # (от первого фронта панельного кода до момента чистоты)
    mttr_row = ""
    if segment_row.get("cause_close") == "FAULT_CLEARED" and segment_row.get("t_end"):
        from corpus.preprocessor import _fmt_dur
        starts = [
            d.get("values", {}).get("fault_start")
            for d in _extract_detections(chars_json)
            if d.get("source") == "CONTROLLER_FAULT"
            and d.get("severity") in ("SHUTDOWN", "WARNING")
        ]
        starts = [s for s in starts if s]
        if starts:
            try:
                t_first = datetime.fromisoformat(min(starts))
                if t_first.tzinfo is None:
                    t_first = t_first.replace(tzinfo=timezone.utc)
                t_end = segment_row["t_end"]
                if t_end.tzinfo is None:
                    t_end = t_end.replace(tzinfo=timezone.utc)
                mttr = (t_end - t_first).total_seconds()
                if mttr > 0:
                    mttr_row = f"| Устранение | ⏱ за {_fmt_dur(mttr)} (от первого кода до сброса) |\n"
            except (ValueError, TypeError):
                pass

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    block1 = (
        f"### 🤖 Детерминированная аналитика\n\n"
        f"| | |\n"
        f"|---|---|\n"
        f"| Вердикт | {emoji} **{verdict}** |\n"
        f"| Уровень тревоги | {emoji} {alarm_level} *({level_note})* |\n"
        f"| Режим | {run_state_label} |\n"
        f"{mttr_row}\n"
        f"**Обнаружения**\n\n"
        f"{dets_block}\n\n"
    )

    block3 = (
        f"\n\n---\n"
        f"*Модель: {model} · Версия аналитики: {ANALYTICS_VERSION} · "
        f"Источник: черновик_claude · {now_str}*\n"
    )

    return block1 + claude_block2 + block3
