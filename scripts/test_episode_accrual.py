# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""Тест разложения времени эпизода тревоги (v4.9.75).

Воздействие считается астрономически: залипшая неисправность висит на панели,
пока её не сбросят, и обрыв связи её не отменяет. Слепая доля не вычитается из
воздействия, а копится рядом — сброс мог произойти внутри слепого куска, и
читающий отчёт должен это видеть. Под данными живёт только дебаунс закрытия:
объявлять тревогу снятой, пока мы её не видим, нельзя.

Запуск:  py -3 scripts/test_episode_accrual.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from online.engine import _accrual_split  # noqa: E402

_errors: list[str] = []


def check(got, want, msg: str, eps: float = 1e-6) -> None:
    if isinstance(want, tuple):
        ok = len(got) == len(want) and all(abs(a - b) <= eps for a, b in zip(got, want))
    else:
        ok = abs(got - want) <= eps
    if not ok:
        _errors.append(f"{msg}: ожидали {want}, получили {got}")


def T(s: int) -> datetime:
    return datetime(2026, 9, 19, 12, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=s)


def gap(a: int, b: int | None) -> dict:
    return {"gap_start": T(a), "gap_end": T(b) if b is not None else None}


# 1. Связь есть: всё время идёт и в воздействие, и в дебаунс
check(_accrual_split(T(0), T(30), []), (30.0, 0.0, 30.0), "1: чистый цикл")

# 2. Полная дыра: воздействие идёт, дебаунс стоит
check(_accrual_split(T(0), T(30), [gap(0, 30)]), (30.0, 30.0, 0.0),
      "2: цикл целиком в дыре")

# 3. Частичная дыра: слепая доля не отнимается от воздействия
check(_accrual_split(T(0), T(30), [gap(10, 20)]), (30.0, 10.0, 20.0),
      "3: дыра внутри цикла")

# 4. Незакрытая дыра (обрыв идёт прямо сейчас) считается до конца окна
check(_accrual_split(T(0), T(30), [gap(10, None)]), (30.0, 20.0, 10.0),
      "4: открытая дыра")

# 5. Дыра шире цикла не даёт слепому времени превысить воздействие
check(_accrual_split(T(10), T(20), [gap(0, 100)]), (10.0, 10.0, 0.0),
      "5: дыра шире окна")

# 6. Первый цикл движка: базы ещё нет, начислять нечего
check(_accrual_split(None, T(30), []), (0.0, 0.0, 0.0), "6: базы нет")

# 7. Окно не двинулось или уехало назад — нулевое начисление, не отрицательное
check(_accrual_split(T(30), T(30), []), (0.0, 0.0, 0.0), "7: нулевое окно")
check(_accrual_split(T(30), T(10), []), (0.0, 0.0, 0.0), "7б: окно назад")

# 8. Инвариант на наборе случаев: span == live + gap, и все члены неотрицательны
for prev, now, gs in [
    (T(0), T(45), []),
    (T(0), T(45), [gap(5, 15), gap(30, None)]),
    (T(5), T(6), [gap(0, 100)]),
]:
    span, g, live = _accrual_split(prev, now, gs)
    check(span, live + g, f"8: span != live+gap на {prev}-{now}")
    if min(span, g, live) < 0:
        _errors.append(f"8: отрицательный член на {prev}-{now}: {(span, g, live)}")

if _errors:
    print("ПРОВАЛЕНО:")
    for e in _errors:
        print("  -", e)
    sys.exit(1)
print("Все проверки пройдены.")
