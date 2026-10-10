# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""Очередь заключений: один сегмент — один разбор (v5.0.2).

Старт ставит в очередь и незаконченные строки, и новые закрытия, тумблер — ещё
раз; сегмент не должен уйти в модель дважды. Ручной запуск поднимает приоритет
ожидающего сегмента, а не добавляет второй разбор.

Запуск:  py -3 scripts/test_worker_queue.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import corpus.worker as w  # noqa: E402

_errors: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        _errors.append(msg)


DONE: list[tuple[int, str]] = []


async def _fake_process(seg_id: int, force: bool = False, task_id: str = "seg_auto") -> None:
    DONE.append((seg_id, task_id))
    await asyncio.sleep(0)


w._process_segment = _fake_process


async def main() -> None:
    q = w.AnalysisWorker()
    q.enqueue(1)
    q.enqueue(1)                      # дубль
    q.enqueue(2)
    q.enqueue(2, w.PRIORITY_MANUAL)   # ручной поднимает приоритет
    q.enqueue(3)
    task = asyncio.create_task(q.run())
    for _ in range(50):
        await asyncio.sleep(0)
        if len(DONE) >= 3 and q._queue.empty():
            break
    await asyncio.sleep(0.05)
    check(sorted(s for s, _ in DONE) == [1, 2, 3], f"1: каждый сегмент — один раз: {DONE}")
    check(DONE[0] == (2, "seg_manual"), f"1б: ручной — первым и как ручной: {DONE}")
    check(not q._pending, f"1в: очередь ожидания пуста: {q._pending}")

    # Сегмент в работе: автоматический повтор не ставится, ручной — ставится
    q._current = 7
    q.enqueue(7)
    check(7 not in q._pending, "2: идущий в работе сегмент повторно не ставится")
    q.enqueue(7, w.PRIORITY_MANUAL)
    check(q._pending.get(7) == w.PRIORITY_MANUAL, "2б: ручной перезапуск идущего — ставится")
    q._current = None
    await q.stop()
    task.cancel()


asyncio.run(main())
if _errors:
    print(f"FAIL — {len(_errors)} расхождений:")
    for e in _errors:
        print(f"  • {e}")
    sys.exit(1)
print("Все проверки пройдены.")
