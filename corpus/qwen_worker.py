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
import itertools
import logging
from typing import Any

logger = logging.getLogger(__name__)

PRIORITY_MANUAL = 0
PRIORITY_NORMAL = 1
PRIORITY_STATUS = 2   # статус-строка — ниже приоритета анализа сегментов

# Монотонный счётчик — тай-брейкер в PriorityQueue.
# Без него heapq сравнивает dict-элементы при одинаковом приоритете → TypeError.
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
        # Кортеж: (priority, seq, seg_id, task_id) — seq-тай-брейкер, никогда не сравниваем dict/str
        self._queue.put_nowait((priority, next(_seq), seg_id, task_id))
        logger.debug("qwen/worker: enqueue #%d (p=%d task=%s)", seg_id, priority, task_id)

    def enqueue_status(self, data: dict) -> None:
        """Поставить задачу генерации статус-строки в очередь (низкий приоритет)."""
        item = {"type": "status_line", **data}
        # Кортеж: (priority, seq, item_dict) — seq не даёт heapq дойти до сравнения dict
        self._queue.put_nowait((PRIORITY_STATUS, next(_seq), item))
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
                # Формат: (priority, seq, payload, *extra)
                # segment:  (priority, seq, seg_id:int, task_id:str)
                # status:   (PRIORITY_STATUS, seq, item_dict:dict)
                _, _, item, *_extra = await asyncio.wait_for(self._queue.get(), timeout=5.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            # ── Диспетчер по типу задачи ──
            if isinstance(item, int):
                seg_id = item
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


# ── Генерация статус-строки ───────────────────────────────────────────────────

async def _process_status_line(job: dict) -> None:
    """Сгенерировать статус-строку и сохранить в открытый сегмент.

    Провайдер и промпт берутся из llm.router (task_id="status_auto").
    """
    import httpx
    from llm.client import _cfg
    from llm.router import get_provider, get_prompt
    from online.status_assembler import build_status_prompt
    from online import db as online_db

    router_sn     = job["router_sn"]
    equip_type    = job["equip_type"]
    panel_id      = job["panel_id"]
    struct_status = job["structural_status"]
    status_hash   = job["status_hash"]

    logger.debug("qwen/worker: статус-строка %s/%s/%s", router_sn, equip_type, panel_id)

    provider      = get_provider("status_auto")
    system_prompt = get_prompt("status_auto")
    user_msg      = build_status_prompt(struct_status)

    # Один retry: Ollama может «холодно» стартовать модель и обрывать первый запрос
    for attempt in range(2):
        try:
            if provider == "api":
                status_text = await _status_via_api(system_prompt, user_msg)
            else:
                status_text = await _status_via_llm(system_prompt, user_msg, _cfg)

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


async def _status_via_llm(system_prompt: str, user_msg: str, cfg: dict) -> str:
    import httpx
    # Для статус-строки (1-2 фразы) используем status_num_ctx — отдельная настройка,
    # меньше num_ctx для анализа: быстрее загрузка модели, меньше риск ReadTimeout.
    status_num_ctx = cfg.get("status_num_ctx", 2048)
    payload = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_msg},
        ],
        "stream": False,
        "options": {"temperature": 0.1, "num_ctx": status_num_ctx},
    }
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(f"{cfg['base_url']}/api/chat", json=payload)
        resp.raise_for_status()
        return resp.json().get("message", {}).get("content", "").strip()


async def _status_via_api(system_prompt: str, user_msg: str) -> str:
    import anthropic
    from corpus.settings import get_claude_settings
    from config import settings as app_settings

    claude_cfg = get_claude_settings()
    client = anthropic.AsyncAnthropic(api_key=app_settings.anthropic_api_key)
    response = await client.messages.create(
        model=claude_cfg["model"],
        max_tokens=512,
        system=system_prompt,
        messages=[{"role": "user", "content": user_msg}],
    )
    return "".join(b.text for b in response.content if hasattr(b, "text")).strip()


# ── Singleton ─────────────────────────────────────────────────────────────────

_worker: QwenWorker | None = None


def init_worker() -> QwenWorker:
    global _worker
    _worker = QwenWorker()
    return _worker


def get_worker() -> QwenWorker | None:
    return _worker
