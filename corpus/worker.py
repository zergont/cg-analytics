"""Очередь и воркер для анализа сегментов (Этап 2).

Singleton-воркер запускается в main.py lifespan.
Интеграция:
  - online/engine.py: enqueue(seg_id) после insert_closed_segment()
  - web/routes.py:    enqueue(seg_id, PRIORITY_MANUAL) по кнопке
  - main.py:          enqueue_pending() при старте (исторический batch)

Провайдер и промпт для каждого запроса берутся из llm.router:
  PRIORITY_NORMAL  → task_id="seg_auto"
  PRIORITY_MANUAL  → task_id="seg_manual"
"""
from __future__ import annotations
import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Приоритеты (меньше = выше приоритет в PriorityQueue)
PRIORITY_MANUAL = 0   # ручной запуск из UI
PRIORITY_NORMAL  = 1   # авто-закрытие сегмента + исторический batch


class AnalysisWorker:
    def __init__(self) -> None:
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._current: int | None = None
        self._running: bool = False
        self._task: asyncio.Task | None = None

    # ── Очередь ──────────────────────────────────────────────────────────────

    def enqueue(self, seg_id: int, priority: int = PRIORITY_NORMAL, force: bool = False) -> None:
        """Добавить сегмент в очередь на анализ.

        force=True разрешает обработку открытых сегментов (t_end IS NULL).
        Автоматически выставляется при priority=PRIORITY_MANUAL.
        """
        _force = force or (priority == PRIORITY_MANUAL)
        task_id = "seg_manual" if priority == PRIORITY_MANUAL else "seg_auto"
        self._queue.put_nowait((priority, seg_id, _force, task_id))
        logger.debug("corpus/worker: enqueue #%d (p=%d force=%s task=%s)",
                     seg_id, priority, _force, task_id)

    async def enqueue_pending(self) -> int:
        """Batch: все закрытые сегменты без анализа → NORMAL очередь."""
        from corpus.db import get_unanalyzed_segments
        seg_ids = await get_unanalyzed_segments()
        for seg_id in seg_ids:
            self.enqueue(seg_id, PRIORITY_NORMAL)
        if seg_ids:
            logger.info(
                "corpus/worker: batch-старт — %d сегментов добавлено в очередь",
                len(seg_ids),
            )
        return len(seg_ids)

    # ── Основной цикл ─────────────────────────────────────────────────────────

    async def run(self) -> None:
        self._running = True
        logger.info("corpus/worker: запущен")

        while self._running:
            try:
                _, seg_id, _force, _task_id = await asyncio.wait_for(
                    self._queue.get(), timeout=5.0
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            self._current = seg_id
            try:
                await _process_segment(seg_id, force=_force, task_id=_task_id)
            except Exception:
                logger.exception(
                    "corpus/worker: необработанная ошибка при анализе #%d", seg_id
                )
            finally:
                self._current = None
                self._queue.task_done()

        logger.info("corpus/worker: остановлен")

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    def get_status(self) -> dict[str, Any]:
        return {
            "running":            self._running,
            "processing_seg_id":  self._current,
            "queue_size":         self._queue.qsize(),
        }


# ── Обработка одного сегмента ─────────────────────────────────────────────────

async def _process_segment(
    seg_id: int,
    force: bool = False,
    task_id: str = "seg_auto",
) -> None:
    """Полный цикл обработки: анализ → сохранение в БД.

    Провайдер и промпт берутся из llm.router по task_id.
    force=True — разрешить анализ открытого сегмента (ручной запуск).
    """
    import corpus.db as corpus_db
    from llm.router import get_provider, get_prompt, get_chain
    from llm.registry import get_entry

    try:
        from analytics.runner import ANALYTICS_VERSION
    except Exception:
        ANALYTICS_VERSION = "3.0.0"

    logger.info("corpus/worker: начинаю #%d (task=%s)", seg_id, task_id)

    await corpus_db.set_status(seg_id, "processing")

    seg_row = await corpus_db.get_segment_row(seg_id)
    if not seg_row:
        await corpus_db.set_status(seg_id, "error", "Сегмент не найден в БД")
        return

    if seg_row.get("t_end") is None and not force:
        await corpus_db.set_status(seg_id, "error", "Сегмент ещё открытый (t_end IS NULL)")
        return
    if seg_row.get("t_end") is None:
        logger.info("corpus/worker: сегмент #%d открытый — ручной запуск (force=True)", seg_id)

    kb_path = await corpus_db.get_equipment_kb_path(
        seg_row["router_sn"], seg_row["equip_type"], seg_row["panel_id"]
    )

    prompt = get_prompt(task_id)

    # Цепочка приоритетов моделей (реестр) имеет приоритет над одиночным провайдером
    chain_entries = [e for e in (get_entry(eid) for eid in get_chain(task_id)) if e]
    if chain_entries:
        provider = "chain"
        result = await _analyse_segment_chain(seg_row, kb_path, prompt, chain_entries)
    elif get_provider(task_id) == "api":
        provider = "api"
        from corpus.agent import analyse_segment
        result = await analyse_segment(seg_row, kb_path, system_prompt=prompt)
    else:
        provider = "llm"
        result = await _analyse_segment_llm(seg_row, prompt)

    if result["error"]:
        logger.error("corpus ERROR #%d | %s", seg_id, result["error"])
        await corpus_db.upsert_analysis(seg_id, {**result, "status": "error"})
        return

    await corpus_db.upsert_analysis(seg_id, {
        **result,
        "status":            "done",
        "humanized_md":      None,
        "analytics_version": ANALYTICS_VERSION,
    })

    from db.analytics import get_app_setting
    qwen_auto = await get_app_setting("qwen_auto_analyze", "false")
    if qwen_auto == "true":
        from corpus.qwen_worker import get_worker as get_qwen_worker
        qwen_w = get_qwen_worker()
        if qwen_w:
            qwen_w.enqueue(seg_id, task_id="human_auto")
            logger.debug("corpus/worker: сегмент #%d → очередь хуманизации", seg_id)

    logger.info(
        "corpus OK #%d | provider=%s | вердикт=%s | уровень=%s | "
        "токены=%d | тулы=%d | %.1fс",
        seg_id, provider,
        result.get("verdict", "—"),
        result.get("alarm_level", "—"),
        result.get("tokens_used", 0),
        result.get("tool_calls_count", 0),
        result.get("generation_time_sec", 0),
    )


async def _analyse_segment_llm(seg_row: dict, system_prompt: str) -> dict[str, Any]:
    """Анализ сегмента через локальную LLM (без инструментов, простой вызов)."""
    import time
    from llm.client import _cfg, chat
    from llm.router import format_ai_signature

    t0 = time.monotonic()
    report_md = seg_row.get("report_md") or ""

    try:
        # chat() сам ретраит сеть/429/5xx и знает текущего провайдера (Ollama/LM Studio)
        content = await chat(system_prompt, report_md)
        if content:
            content += format_ai_signature(_cfg["model"])

        return {
            "verdict":            "LLM",
            "alarm_level":        None,
            "conclusion_md":      content,
            "error":              None,
            "tokens_used":        0,
            "tool_calls_count":   0,
            "loops_count":        0,
            "generation_time_sec": round(time.monotonic() - t0, 1),
            "debug_json":         {"provider": "llm", "model": _cfg["model"]},
            "claude_model":       _cfg["model"],
        }
    except Exception as exc:
        return {
            "verdict": None, "alarm_level": None, "conclusion_md": "",
            "error": repr(exc), "tokens_used": 0, "tool_calls_count": 0,
            "loops_count": 0, "generation_time_sec": 0, "debug_json": {},
            "claude_model": None,
        }


# ── Цепочка приоритетов моделей ───────────────────────────────────────────────

# Оценка размера промпта: ~3 символа на токен для русского текста (консервативно)
_EST_CHARS_PER_TOKEN = 3
# Резерв контекста под ответ модели
_RESPONSE_RESERVE_TOKENS = 4000


async def _analyse_segment_chain(
    seg_row: dict,
    kb_path: str | None,
    system_prompt: str,
    entries: list[dict],
) -> dict[str, Any]:
    """Анализ по цепочке приоритетов: проактивный отбор по размеру + fallback по ошибкам.

    Для каждой записи цепочки по порядку:
      1. промпт не влезает в max_ctx_tokens → пропуск без запроса;
      2. type="api"  → Claude-агент с инструментами;
         type="llm"  → plain chat через запись реестра (стрим по настройке записи);
      3. ошибка запроса (сеть после ретраев, 4xx сразу) → следующая запись.
    След маршрутизации пишется в debug_json.routing.
    """
    import time
    from llm.client import chat

    report_md  = seg_row.get("report_md") or ""
    est_tokens = (len(system_prompt) + len(report_md)) // _EST_CHARS_PER_TOKEN \
                 + _RESPONSE_RESERVE_TOKENS
    trace: list[dict] = []

    for entry in entries:
        if est_tokens > entry["max_ctx_tokens"]:
            trace.append({"entry": entry["id"],
                          "action": "skip_size",
                          "detail": f"~{est_tokens} ток. > лимита {entry['max_ctx_tokens']}"})
            logger.info("corpus/chain: #%s → «%s» пропущена по размеру (~%d > %d ток.)",
                        seg_row.get("id"), entry["name"], est_tokens, entry["max_ctx_tokens"])
            continue

        t0 = time.monotonic()
        try:
            if entry["type"] == "api":
                from corpus.agent import analyse_segment
                result = await analyse_segment(seg_row, kb_path, system_prompt=system_prompt)
                if result.get("error"):
                    trace.append({"entry": entry["id"], "action": "error",
                                  "detail": str(result["error"])[:300]})
                    continue
            else:
                from llm.router import format_ai_signature
                content = await chat(system_prompt, report_md,
                                     entry=entry, stream=entry.get("stream", True))
                if content:
                    content += format_ai_signature(entry["model"])
                result = {
                    "verdict":            "LLM",
                    "alarm_level":        None,
                    "conclusion_md":      content,
                    "error":              None,
                    "tokens_used":        0,
                    "tool_calls_count":   0,
                    "loops_count":        0,
                    "generation_time_sec": round(time.monotonic() - t0, 1),
                    "debug_json":         {"provider": entry["provider"]},
                    "claude_model":       f"{entry['name']} ({entry['model']})",
                }
            trace.append({"entry": entry["id"], "action": "ok"})
            dbg = result.get("debug_json") or {}
            dbg["routing"] = trace
            dbg["est_prompt_tokens"] = est_tokens
            result["debug_json"] = dbg
            return result
        except Exception as exc:
            # сеть/429/5xx уже отретраены внутри chat(); сюда доходят
            # исчерпанные ретраи и 4xx — в обоих случаях идём к следующей модели
            trace.append({"entry": entry["id"], "action": "error", "detail": repr(exc)[:300]})
            logger.warning("corpus/chain: #%s → «%s» ошибка, перехожу дальше: %r",
                           seg_row.get("id"), entry["name"], exc)

    detail = "; ".join(f"{t['entry']}: {t['action']}" for t in trace) or "цепочка пуста"
    return {
        "verdict": None, "alarm_level": None, "conclusion_md": "",
        "error": f"Ни одна модель цепочки не обработала сегмент "
                 f"(промпт ~{est_tokens} ток.): {detail}",
        "tokens_used": 0, "tool_calls_count": 0, "loops_count": 0,
        "generation_time_sec": 0,
        "debug_json": {"routing": trace, "est_prompt_tokens": est_tokens},
        "claude_model": None,
    }


# ── Singleton ─────────────────────────────────────────────────────────────────

_worker: AnalysisWorker | None = None


def init_worker() -> AnalysisWorker:
    global _worker
    _worker = AnalysisWorker()
    return _worker


def get_worker() -> AnalysisWorker | None:
    return _worker
