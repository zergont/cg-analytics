"""Очередь и воркер для Claude-анализа сегментов (Этап 2).

Singleton-воркер запускается в main.py lifespan.
Интеграция:
  - online/engine.py: enqueue(seg_id) после insert_closed_segment()
  - web/routes.py:    enqueue(seg_id, PRIORITY_MANUAL) по кнопке
  - main.py:          enqueue_pending() при старте (исторический batch)
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
        self._current: int | None = None      # seg_id, который сейчас обрабатывается
        self._running: bool = False
        self._task: asyncio.Task | None = None

    # ── Очередь ──────────────────────────────────────────────────────────────

    def enqueue(self, seg_id: int, priority: int = PRIORITY_NORMAL) -> None:
        """Добавить сегмент в очередь на анализ."""
        self._queue.put_nowait((priority, seg_id))
        logger.debug("corpus/worker: enqueue #%d (p=%d)", seg_id, priority)

    async def enqueue_pending(self) -> int:
        """Batch: все закрытые сегменты без анализа → NORMAL очередь.

        Вызывается при старте приложения.
        """
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
        """Основной цикл воркера. Запускается как asyncio.Task в lifespan."""
        self._running = True
        logger.info("corpus/worker: запущен")

        while self._running:
            try:
                _, seg_id = await asyncio.wait_for(self._queue.get(), timeout=5.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            self._current = seg_id
            try:
                await _process_segment(seg_id)
            except Exception:
                logger.exception(
                    "corpus/worker: необработанная ошибка при анализе #%d", seg_id
                )
            finally:
                self._current = None
                self._queue.task_done()

        logger.info("corpus/worker: остановлен")

    async def stop(self) -> None:
        """Остановить воркер при shutdown приложения."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    # ── Статус ────────────────────────────────────────────────────────────────

    def get_status(self) -> dict[str, Any]:
        return {
            "running":            self._running,
            "processing_seg_id":  self._current,
            "queue_size":         self._queue.qsize(),
        }


# ── Обработка одного сегмента ─────────────────────────────────────────────────

async def _process_segment(seg_id: int) -> None:
    """Полный цикл обработки сегмента: Claude → humanizer → сохранение в БД."""
    import corpus.db as corpus_db
    from corpus.agent import analyse_segment
    from corpus.humanizer import humanize

    try:
        from analytics.runner import ANALYTICS_VERSION
    except Exception:
        ANALYTICS_VERSION = "3.0.0"

    logger.info("corpus/worker: начинаю обработку сегмента #%d", seg_id)

    # 1. Пометить как «в обработке»
    await corpus_db.set_status(seg_id, "processing")

    # 2. Загрузить данные сегмента
    seg_row = await corpus_db.get_segment_row(seg_id)
    if not seg_row:
        logger.warning("corpus/worker: сегмент #%d не найден в БД", seg_id)
        await corpus_db.set_status(seg_id, "error", "Сегмент не найден в БД")
        return

    if seg_row.get("t_end") is None:
        logger.warning("corpus/worker: сегмент #%d открытый — пропускаю", seg_id)
        await corpus_db.set_status(seg_id, "error", "Сегмент ещё открытый (t_end IS NULL)")
        return

    # 3. Получить kb_path для поиска по документации
    kb_path = await corpus_db.get_equipment_kb_path(
        seg_row["router_sn"], seg_row["equip_type"], seg_row["panel_id"]
    )

    # 4. Анализ через Claude API
    result = await analyse_segment(seg_row, kb_path)

    if result["error"]:
        logger.error(
            "corpus API ERROR: сегмент #%d | ошибка=%s", seg_id, result["error"]
        )
        await corpus_db.upsert_analysis(seg_id, {**result, "status": "error"})
        return

    # 5. Очеловечить через qwen (некритично — при ошибке пустая строка)
    humanized = ""
    if result["conclusion_md"]:
        humanized = await humanize(result["conclusion_md"])

    # 6. Сохранить финальный результат
    await corpus_db.upsert_analysis(seg_id, {
        **result,
        "status":             "done",
        "humanized_md":       humanized,
        "analytics_version":  ANALYTICS_VERSION,
    })

    logger.info(
        "corpus API OK: сегмент #%d | вердикт=%s | уровень=%s | "
        "токены=%d (вх=%d вых=%d) | тулы=%d | петли=%d | %.1fс",
        seg_id,
        result.get("verdict", "—"),
        result.get("alarm_level", "—"),
        result.get("tokens_used", 0),
        result.get("debug_json", {}).get("tokens_input", 0),
        result.get("debug_json", {}).get("tokens_output", 0),
        result.get("tool_calls_count", 0),
        result.get("loops_count", 0),
        result.get("generation_time_sec", 0),
    )


# ── Singleton ─────────────────────────────────────────────────────────────────

_worker: AnalysisWorker | None = None


def init_worker() -> AnalysisWorker:
    """Создать и запомнить глобальный экземпляр воркера."""
    global _worker
    _worker = AnalysisWorker()
    return _worker


def get_worker() -> AnalysisWorker | None:
    """Вернуть глобальный экземпляр (None если не инициализирован)."""
    return _worker
