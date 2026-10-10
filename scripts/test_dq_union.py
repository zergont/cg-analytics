# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""Качество данных по объединению разрывов связи (v5.0.4).

Разрывы перекрываются (несколько записей внешнего сервиса на одно окно плюс
синтетический по max(history.ts)) — сумма длин занижала качество вплоть до 0.
ДЭС №3 09.10, подсегмент 1.8: разрывы 17:28:21→19:28:20 и 17:33:28→19:33:27 —
по сумме 3 ч 59 мин 58 с, по объединению 2 ч 5 мин 6 с.

Запуск:  py -3 scripts/test_dq_union.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analytics.segmenter import _compute_data_quality, union_seconds  # noqa: E402

_errors: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        _errors.append(msg)


def T(s: str) -> datetime:
    return datetime.fromisoformat(f"2026-10-08T{s}+00:00")


check(union_seconds([]) == 0, "0: пусто")
check(union_seconds([(T("17:28:21"), T("19:28:20")), (T("17:33:28"), T("19:33:27"))]) == 7506,
      "1: перекрытие ДЭС №3 — 2 ч 5 мин 6 с")
check(union_seconds([(T("10:00:00"), T("11:00:00")), (T("12:00:00"), T("12:30:00"))]) == 5400, "2: без перекрытия — сумма")
check(union_seconds([(T("10:00:00"), T("12:00:00")), (T("10:30:00"), T("11:00:00"))]) == 7200, "3: вложенный")

gaps = [{"gap_start": T("17:28:21"), "gap_end": T("19:28:20")},
        {"gap_start": T("17:33:28"), "gap_end": T("19:33:27")}]
dq = _compute_data_quality({}, T("15:00:00"), T("19:37:32"), gaps, None)
check(abs(dq - round(1 - 7506 / 16652, 3)) < 1e-9, f"4: качество подсегмента 1.8 — по объединению: {dq}")
check(_compute_data_quality({}, T("10:00:00"), T("11:00:00"),
                            [{"gap_start": T("09:00:00"), "gap_end": None}], None) == 0.0,
      "5: незакрытый разрыв на всё окно — 0")

if _errors:
    print(f"FAIL — {len(_errors)} расхождений:")
    for e in _errors:
        print(f"  • {e}")
    sys.exit(1)
print("Все проверки пройдены.")
