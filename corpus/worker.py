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
    from llm.router import get_provider, get_prompt

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

    provider = get_provider(task_id)
    prompt   = get_prompt(task_id)

    if provider == "api":
        from corpus.agent import analyse_segment
        result = await analyse_segment(seg_row, kb_path, system_prompt=prompt)
    else:
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
    import httpx, time
    from llm.client import _cfg

    t0 = time.monotonic()
    report_md = seg_row.get("report_md") or ""

    try:
        payload = {
            "model": _cfg["model"],
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": report_md},
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
            data = resp.json()
            content = data.get("message", {}).get("content", "")

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


# ── Singleton ─────────────────────────────────────────────────────────────────

_worker: AnalysisWorker | None = None


def init_worker() -> AnalysisWorker:
    global _worker
    _worker = AnalysisWorker()
    return _worker


def get_worker() -> AnalysisWorker | None:
    return _worker
