# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Воркер LLM-обработки: хуманизация Claude-заключений через Ollama.

Логика:
  - Входная очередь: seg_id сегментов со статусом done + conclusion_md, но без humanized_md
  - Обрабатывает через corpus/humanizer.py (Ollama)
  - Сохраняет результат в segment_analyses.humanized_md
  - Запускается только при qwen_auto_analyze = true

Интеграция:
  - main.py:          запускает воркер и batch-старт при включённом авто-анализе
  - corpus/worker.py: enqueue() после успешного Claude-анализа
"""
from __future__ import annotations
import asyncio
import itertools
import logging
from typing import Any

logger = logging.getLogger(__name__)

PRIORITY_MANUAL = 0
PRIORITY_NORMAL = 1

# Монотонный счётчик — тай-брейкер в PriorityQueue.
_seq = itertools.count()


class QwenWorker:
    def __init__(self) -> None:
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._current: int | None = None
        self._running: bool = False
        self._task: asyncio.Task | None = None

    # ── Очередь ──────────────────────────────────────────────────────────────

    def enqueue(self, seg_id: int, priority: int = PRIORITY_NORMAL,
                task_id: str = "human_auto") -> None:
        self._queue.put_nowait((priority, next(_seq), seg_id, task_id))
        logger.debug("qwen/worker: enqueue #%d (p=%d task=%s)", seg_id, priority, task_id)

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
                # Формат: (priority, seq, payload, *extra)
                # формат: (priority, seq, seg_id:int, task_id:str)
                _, _, item, *_extra = await asyncio.wait_for(self._queue.get(), timeout=5.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            if not isinstance(item, int):
                self._queue.task_done()
                logger.warning("qwen/worker: неизвестный тип задачи: %s", type(item))
                continue

            seg_id  = item
            task_id = _extra[0] if _extra else "human_auto"
            from db.analytics import get_app_setting
            qwen_auto = await get_app_setting("qwen_auto_analyze", "false")
            if qwen_auto != "true" and task_id == "human_auto":
                self._queue.task_done()
                logger.debug("qwen/worker: авто-анализ выключен, сегмент #%d пропущен", seg_id)
                continue
            self._current = seg_id
            try:
                await _process_segment(seg_id, task_id=task_id)
            except Exception:
                logger.exception("qwen/worker: ошибка обработки сегмента #%d", seg_id)
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
            "running":           self._running,
            "processing_seg_id": self._current,
            "queue_size":        self._queue.qsize(),
        }


# ── Обработка одного сегмента ─────────────────────────────────────────────────

async def _process_segment(seg_id: int, task_id: str = "human_auto") -> None:
    """Прогнать conclusion_md сегмента через хуманизатор, сохранить humanized_md.

    Провайдер и промпт берутся из llm.router по task_id.
    """
    import corpus.db as corpus_db
    from corpus.humanizer import humanize

    logger.info("qwen/worker: хуманизирую #%d (task=%s)", seg_id, task_id)

    analysis = await corpus_db.get_analysis(seg_id)
    if not analysis:
        logger.warning("qwen/worker: анализ для сегмента #%d не найден", seg_id)
        return

    conclusion_md = analysis.get("conclusion_md") or ""
    if not conclusion_md:
        logger.warning("qwen/worker: conclusion_md пустой у #%d — пропускаю", seg_id)
        return

    humanized = await humanize(conclusion_md, task_id=task_id)
    if not humanized:
        logger.warning("qwen/worker: хуманизатор вернул пустой результат для #%d", seg_id)
        return

    await corpus_db.set_humanized_md(seg_id, humanized)
    logger.info("qwen/worker: #%d обработан (%d символов)", seg_id, len(humanized))


# ── Singleton ─────────────────────────────────────────────────────────────────

_worker: QwenWorker | None = None


def init_worker() -> QwenWorker:
    global _worker
    _worker = QwenWorker()
    return _worker


def get_worker() -> QwenWorker | None:
    return _worker
