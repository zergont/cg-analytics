# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""Тест пола окна взгляда назад (v4.9.78).

Серия попыток пуска пересказывала сама себя: на ДЭС №3 16.09 машина трижды
за восемь минут упала по перегреву ОЖ, и пятиминутная преамбула второго и
третьего актов затягивала события первого — 85 событий в ленте вместо
полутора десятков. Характер-гейт при этом видел в десятиминутном окне чужое
охлаждение и объявлял немедленный останов контролируемым.

Пол ставится по концу предыдущей стоянки: окно покрывает ровно текущую
попытку работы. В обычном случае (машина отработала часы и упала) пол лежит
далеко позади и ничего не ограничивает.

Запуск:  py -3 scripts/test_stop_lookback_floor.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analytics.classifier import (  # noqa: E402
    build_stop_incident,
    classify_stop_character,
    prev_stop_end,
)

_errors: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        _errors.append(msg)


def T(h: int, m: int, s: int = 0) -> datetime:
    return datetime(2026, 9, 16, h, m, s, tzinfo=timezone.utc)


def rs(value: int, start: datetime, end: datetime | None) -> dict:
    return {"addr": 40011, "value": value, "state_start": start,
            "state_end": end, "label": f"RS={value}"}


def ev(addr: int, label: str, start: datetime) -> dict:
    return {"addr": addr, "value": 1, "state_start": start,
            "state_end": None, "label": label}


def fp(severity: str, start: datetime, end: datetime | None) -> dict:
    return {"severity": severity, "fault_start": start, "fault_end": end,
            "addr": 40404, "bit": 3, "fault_name_ru": "Температура ОЖ"}


# Реальная цепочка 16.09: работа → падение → попытка пуска → снова падение.
STOP_A = T(13, 26, 11)
RESTART = T(13, 29, 0)
STOP_B = T(13, 29, 55)
END_B = T(13, 31, 0)

RS_CHAIN = [
    rs(3, T(13, 0, 0), T(13, 25, 0)),
    rs(5, T(13, 25, 0), STOP_A),        # охлаждение ПЕРЕД первым падением
    rs(0, STOP_A, RESTART),
    rs(3, RESTART, STOP_B),             # попытка пуска
    rs(0, STOP_B, END_B),
]
EVENTS = [
    ev(40286, "Сброс неисправностей", T(13, 26, 19)),   # хвост первой аварии
    ev(40599, "EmergencyStop", T(13, 29, 58)),          # уже вторая
]
FAULTS = [
    fp("shutdown", STOP_A, T(13, 28, 52)),
    fp("shutdown", STOP_B, T(13, 30, 31)),
]
ENUM = RS_CHAIN + EVENTS


# 1. Сам пол
check(prev_stop_end(ENUM, STOP_A) is None,
      "1a: перед первым падением стоянок не было, пол должен быть пустым")
check(prev_stop_end(ENUM, STOP_B) == RESTART,
      f"1b: пол второго падения должен быть {RESTART}, "
      f"получили {prev_stop_end(ENUM, STOP_B)}")

# 2. Стоянка, внутри которой лежит stop_ts, полом не считается
check(prev_stop_end(ENUM, T(13, 27, 0)) is None,
      "2: объемлющая стоянка попала в пол")

# 3. Незакрытая стоянка полом не становится
check(prev_stop_end([rs(0, T(13, 0, 0), None)], T(13, 30, 0)) is None,
      "3: открытая стоянка попала в пол")

# 4. Берётся ПОСЛЕДНЯЯ из нескольких
check(prev_stop_end([rs(0, T(12, 0, 0), T(12, 5, 0)),
                     rs(0, T(12, 40, 0), T(12, 50, 0))], T(13, 0, 0))
      == T(12, 50, 0), "4: взят не последний конец стоянки")

# 5. Лента второго акта не пересказывает первую аварию
inc_b = build_stop_incident(ENUM, FAULTS, STOP_B, END_B, None, stop_kind="EMERGENCY")
check(inc_b is not None, "5a: акт второго падения не построен")
if inc_b:
    stale = [e for e in inc_b["chronology"]
             if e["ts"] and datetime.fromisoformat(e["ts"]) < RESTART]
    check(not stale,
          f"5б: в ленте второго акта {len(stale)} событий из прошлой аварии: "
          f"{[e['ts'] for e in stale]}")

# 6. Обычный случай не тронут: преамбула первого акта на месте
inc_a = build_stop_incident(ENUM, FAULTS, STOP_A, T(13, 28, 52), None,
                            stop_kind="EMERGENCY")
check(inc_a is not None, "6a: акт первого падения не построен")
if inc_a:
    got = [e["ts"] for e in inc_a["chronology"]
           if e["ts"] and datetime.fromisoformat(e["ts"]) == T(13, 25, 0)]
    check(bool(got),
          "6б: охлаждение за минуту до падения выпало из преамбулы первого акта")

# 7. Вердикт второго падения больше не заражён чужим охлаждением
v_b = classify_stop_character(ENUM, STOP_B, None)
check(v_b["signals"]["passed_cooldown"] is False,
      "7а: гейт увидел охлаждение из прошлой попытки")
check(v_b["character"] == "immediate",
      f"7б: второе падение должно быть немедленным, получили {v_b['character']}")

# 8. А первое падение по-прежнему читается как контролируемое — там охлаждение своё
v_a = classify_stop_character(ENUM, STOP_A, None)
check(v_a["signals"]["passed_cooldown"] is True,
      "8: своё охлаждение перед первым падением потерялось")

if _errors:
    print("ПРОВАЛЕНО:")
    for e in _errors:
        print("  -", e)
    sys.exit(1)
print("Все проверки пройдены.")
