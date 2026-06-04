"""Воркер Qwen-обработки: очеловечивание Claude-заключений через Ollama.

Логика:
  - Входная очередь: seg_id сегментов со статусом done + conclusion_md, но без humanized_md
  - Обрабатывает через corpus/humanizer.py (Qwen/Ollama)
  - Сохраняет результат в segment_analyses.humanized_md
  - Запускается только при qwen_auto_analyze = true

Интеграция:
  - main.py:          запускает воркер и batch-старт при включённом авто-анализе
  - corpus/worker.py: enqueue() после успешного Claude-анализа
"""
from __future__ import annotations
import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

PRIORITY_MANUAL = 0
PRIORITY_NORMAL = 1


class QwenWorker:
    def __init__(self) -> None:
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._current: int | None = None
        self._running: bool = False
        self._task: asyncio.Task | None = None

    # ── Очередь ──────────────────────────────────────────────────────────────

    def enqueue(self, seg_id: int, priority: int = PRIORITY_NORMAL) -> None:
        self._queue.put_nowait((priority, seg_id))
        logger.debug("qwen/worker: enqueue #%d (p=%d)", seg_id, priority)

    async def enqueue_pending(self) -> int:
        """Batch: все сегменты с готовым Claude-анализом без Qwen-обработки."""
        from corpus.db import get_unhumanized_segments
        seg_ids = await get_unhumanized_segments()
        for seg_id in seg_ids:
            self.enqueue(seg_id, PRIORITY_NORMAL)
        if seg_ids:
            logger.info(
                "qwen/worker: batch-старт — %d сегментов добавлено в очередь",
                len(seg_ids),
            )
        return len(seg_ids)

    # ── Основной цикл ─────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Основной цикл воркера. Запускается как asyncio.Task в lifespan."""
        self._running = True
        logger.info("qwen/worker: запущен")

        while self._running:
            try:
                _, seg_id = await asyncio.wait_for(self._queue.get(), timeout=5.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            # Проверяем флаг перед каждым сегментом
            from db.analytics import get_app_setting
            qwen_auto = await get_app_setting("qwen_auto_analyze", "false")
            if qwen_auto != "true":
                self._queue.task_done()
                logger.debug("qwen/worker: авто-анализ выключен, сегмент #%d пропущен", seg_id)
                continue

            self._current = seg_id
            try:
                await _process_segment(seg_id)
            except Exception:
                logger.exception(
                    "qwen/worker: необработанная ошибка при обработке #%d", seg_id
                )
            finally:
                self._current = None
                self._queue.task_done()

        logger.info("qwen/worker: остановлен")

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

async def _process_segment(seg_id: int) -> None:
    """Прогнать conclusion_md сегмента через Qwen, сохранить humanized_md."""
    import corpus.db as corpus_db
    from corpus.humanizer import humanize

    logger.info("qwen/worker: обрабатываю сегмент #%d", seg_id)

    analysis = await corpus_db.get_analysis(seg_id)
    if not analysis:
        logger.warning("qwen/worker: анализ для сегмента #%d не найден", seg_id)
        return

    conclusion_md = analysis.get("conclusion_md") or ""
    if not conclusion_md:
        logger.warning("qwen/worker: conclusion_md пустой у сегмента #%d — пропускаю", seg_id)
        return

    humanized = await humanize(conclusion_md)
    if not humanized:
        logger.warning("qwen/worker: Qwen вернул пустой результат для #%d", seg_id)
        return

    await corpus_db.set_humanized_md(seg_id, humanized)
    logger.info("qwen/worker: сегмент #%d обработан (%d символов)", seg_id, len(humanized))


# ── Singleton ─────────────────────────────────────────────────────────────────

_worker: QwenWorker | None = None


def init_worker() -> QwenWorker:
    global _worker
    _worker = QwenWorker()
    return _worker


def get_worker() -> QwenWorker | None:
    return _worker
