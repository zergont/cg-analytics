# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

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
PRIORITY_STATUS = 2   # статус-строка — ниже приоритета анализа сегментов


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

    def enqueue_status(self, data: dict) -> None:
        """Поставить задачу генерации статус-строки в очередь (низкий приоритет)."""
        item = {"type": "status_line", **data}
        self._queue.put_nowait((PRIORITY_STATUS, item))
        logger.debug(
            "qwen/worker: enqueue status для %s/%s/%s",
            data.get("router_sn"), data.get("equip_type"), data.get("panel_id"),
        )

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
                _, item = await asyncio.wait_for(self._queue.get(), timeout=5.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            # ── Диспетчер по типу задачи ──
            if isinstance(item, int):
                # Анализ сегмента (основной поток)
                seg_id = item
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
                    logger.exception("qwen/worker: ошибка обработки сегмента #%d", seg_id)
                finally:
                    self._current = None
                    self._queue.task_done()

            elif isinstance(item, dict) and item.get("type") == "status_line":
                # Генерация статус-строки (ИИ-оператор Уровень 1)
                try:
                    await _process_status_line(item)
                except Exception:
                    logger.exception("qwen/worker: ошибка генерации статус-строки")
                finally:
                    self._queue.task_done()

            else:
                self._queue.task_done()
                logger.warning("qwen/worker: неизвестный тип задачи: %s", type(item))

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


# ── Генерация статус-строки ───────────────────────────────────────────────────

async def _process_status_line(job: dict) -> None:
    """Сгенерировать статус-строку через qwen и сохранить в открытый сегмент."""
    import httpx
    from llm.client import _cfg
    from online.status_assembler import build_status_prompt
    from online import db as online_db

    router_sn     = job["router_sn"]
    equip_type    = job["equip_type"]
    panel_id      = job["panel_id"]
    struct_status = job["structural_status"]
    status_hash   = job["status_hash"]

    logger.debug("qwen/worker: статус-строка %s/%s/%s", router_sn, equip_type, panel_id)

    prompt = build_status_prompt(struct_status)

    payload = {
        "model": _cfg["model"],
        "messages": [
            {
                "role": "system",
                "content": (
                    "Ты формируешь краткую текстовую сводку состояния дизельного генератора "
                    "для оператора пульта управления. "
                    "Используй ТОЛЬКО приведённые факты. "
                    "НЕ добавляй оценки, предположения, рекомендации, которых нет в фактах. "
                    "Облеки факты в 1-2 коротких естественных фразы на русском языке."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "options": {
            "temperature": 0.1,
            "num_ctx": _cfg.get("num_ctx", 4096),
        },
    }

    # Один retry: Ollama может «холодно» стартовать модель и обрывать первый запрос
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(f"{_cfg['base_url']}/api/chat", json=payload)
                resp.raise_for_status()
                data = resp.json()
                status_text = data.get("message", {}).get("content", "").strip()

            if not status_text:
                logger.warning("qwen/worker: статус-строка пустая для %s/%s/%s",
                               router_sn, equip_type, panel_id)
                return

            await online_db.update_open_segment_status(
                router_sn, equip_type, panel_id,
                status_text=status_text,
                status_hash=status_hash,
            )
            logger.info("qwen/worker: статус обновлён %s/%s/%s (%d симв.)",
                        router_sn, equip_type, panel_id, len(status_text))
            return

        except Exception as exc:
            exc_desc = repr(exc) if not str(exc) else str(exc)
            if attempt == 0:
                logger.debug(
                    "qwen/worker: попытка 1 неудачна (%s/%s/%s): %s — повтор через 10с",
                    router_sn, equip_type, panel_id, exc_desc,
                )
                await asyncio.sleep(10)
            else:
                logger.warning(
                    "qwen/worker: ошибка статус-строки %s/%s/%s: %s",
                    router_sn, equip_type, panel_id, exc_desc,
                )


# ── Singleton ─────────────────────────────────────────────────────────────────

_worker: QwenWorker | None = None


def init_worker() -> QwenWorker:
    global _worker
    _worker = QwenWorker()
    return _worker


def get_worker() -> QwenWorker | None:
    return _worker
