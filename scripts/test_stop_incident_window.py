# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""Тест окна акта аварийного останова (v4.9.79).

Окно акта отсчитывается не от числа минут, а от последнего НОРМАЛЬНОГО
останова: цикл оборудования — работа → расхолаживание → стоп, и если прошлый
стоп прошёл штатно, то на тот момент с машиной было всё в порядке, а всё
после — возможные предвестники. Так окно ограничено количеством переходов в
цикле, а не длительностью смены.

Три границы:
  * начало — якорь (последний нормальный останов), но не глубже предыдущей
    АВАРИИ: её акт уже есть, пересказывать незачем;
  * конец — не позднее лага стабилизации после останова: сопутствующие
    неисправности приходят уже после падения и относятся к той же аварии;
  * отдельно срез «висело на момент останова» — маска могла подняться
    задолго до окна и в ленту не попасть никогда.

Запуск:  py -3 scripts/test_stop_incident_window.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analytics.classifier import (  # noqa: E402
    build_stop_incident,
    find_baseline_anchor,
    prev_emergency_stop_end,
    standing_faults,
)

_errors: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        _errors.append(msg)


def T(h: int, m: int, s: int = 0) -> datetime:
    return datetime(2026, 9, 16, h, m, s, tzinfo=timezone.utc)


class _Cfg:
    _MAP = {"shutdown": "SHUTDOWN", "shutdown_cooldown": "SHUTDOWN",
            "derate": "WARNING", "warning": "WARNING", "none": "INFO"}

    def bitmap_severity(self, raw: str | None) -> str:
        return self._MAP.get(raw or "none", "WARNING")


CFG = _Cfg()


def rs(value: int, start: datetime, end: datetime | None) -> dict:
    return {"addr": 40011, "value": value, "state_start": start,
            "state_end": end, "label": f"RS={value}"}


def ev(label: str, start: datetime) -> dict:
    return {"addr": 40286, "value": 1, "state_start": start,
            "state_end": None, "label": label}


def fp(severity: str, start: datetime, end: datetime | None,
       bit: int = 3, name: str = "Температура ОЖ") -> dict:
    return {"severity": severity, "fault_start": start, "fault_end": end,
            "addr": 40404, "bit": bit, "fault_name_ru": name}


# Цикл: работа → охлаждение → НОРМАЛЬНЫЙ стоп → пуск → работа →
#       АВАРИЯ A → попытка пуска → АВАРИЯ B
NORMAL_STOP = T(12, 55)
STOP_A = T(13, 26, 11)
CLEAR_A = T(13, 28, 52)
RESTART = T(13, 29, 0)
STOP_B = T(13, 29, 55)
END_B = T(13, 31, 0)

RS_CHAIN = [
    rs(3, T(12, 0), T(12, 50)),
    rs(5, T(12, 50), NORMAL_STOP),          # расхолаживание — останов штатный
    rs(0, NORMAL_STOP, T(13, 10)),
    rs(3, T(13, 10), STOP_A),
    rs(0, STOP_A, RESTART),
    rs(3, RESTART, STOP_B),
    rs(0, STOP_B, END_B),
]
EVENTS = [
    ev("Сброс неисправностей", T(13, 5)),        # внутри нормальной стоянки
    ev("Хвост первой аварии", T(13, 26, 19)),
    ev("Поздний хвост первой", T(13, 28, 0)),    # за лагом стабилизации A
    ev("Попытка пуска", T(13, 29, 10)),
]
FAULTS = [
    fp("shutdown", STOP_A, CLEAR_A),
    fp("shutdown", STOP_B, T(13, 30, 31)),
    fp("warning", T(10, 0), None, bit=7, name="Уровень ОЖ ниже нормы"),
    fp("warning", T(11, 0), T(11, 30), bit=9, name="Давно снятое"),
]
ENUM = RS_CHAIN + EVENTS


# 1. Якорь — последний нормальный останов
got, why = find_baseline_anchor(ENUM, FAULTS, STOP_A, CFG)
check(got == NORMAL_STOP and why == "normal_stop",
      f"1: якорь должен быть {NORMAL_STOP}/normal_stop, получили {got}/{why}")

# 2. Аварийная стоянка эталоном не становится
got, why = find_baseline_anchor(ENUM, FAULTS, STOP_B, CFG)
check(got == NORMAL_STOP,
      f"2: якорь для второй аварии должен быть тем же нормальным, получили {got}")

# 3. Стоп с обрывом цепочки (3→0 напрямую) эталоном не становится
broken = [rs(3, T(12, 0), T(12, 30)), rs(0, T(12, 30), T(12, 40)),
          rs(3, T(12, 40), STOP_A), rs(0, STOP_A, None)]
got, why = find_baseline_anchor(broken, [], STOP_A, CFG)
check(why == "fallback",
      f"3: останов без расхолаживания принят за эталон ({got}/{why})")

# 4. Фолбэк: три последних периода, но окно не короче суток
got, why = find_baseline_anchor(broken, [], STOP_A, CFG)
check(got <= STOP_A - timedelta(hours=24),
      f"4: фолбэк дал окно короче суток: {got}")

# 5. Пол — только предыдущая АВАРИЙНАЯ стоянка
check(prev_emergency_stop_end(ENUM, FAULTS, STOP_B, CFG) == RESTART,
      "5a: пол второй аварии должен быть концом первой")
check(prev_emergency_stop_end(ENUM, FAULTS, STOP_A, CFG) is None,
      "5б: перед первой аварией аварийных стоянок не было")

# 6. Акт первой аварии берёт весь цикл от нормального останова
inc_a = build_stop_incident(ENUM, FAULTS, STOP_A, CLEAR_A, CFG,
                            stop_kind="EMERGENCY")
check(inc_a is not None, "6a: акт первой аварии не построен")
if inc_a:
    check(inc_a["window"]["from"] == NORMAL_STOP.isoformat(),
          f"6б: окно должно начинаться с нормального останова, "
          f"получили {inc_a['window']['from']}")
    labels = [e.get("label") for e in inc_a["chronology"]]
    check("Сброс неисправностей" in labels,
          "6в: события нормальной стоянки должны быть в окне")

# 7. Конец окна ограничен лагом стабилизации
if inc_a:
    check(inc_a["window"]["to"] == (STOP_A + timedelta(seconds=60)).isoformat(),
          f"7a: конец окна должен быть на лаге, получили {inc_a['window']['to']}")
    labels = [e.get("label") for e in inc_a["chronology"]]
    check("Хвост первой аварии" in labels, "7б: событие внутри лага потерялось")
    check("Поздний хвост первой" not in labels,
          "7в: событие за лагом попало в акт")

# 8. Акт второй аварии не пересказывает первую
inc_b = build_stop_incident(ENUM, FAULTS, STOP_B, END_B, CFG,
                            stop_kind="EMERGENCY")
check(inc_b is not None, "8a: акт второй аварии не построен")
if inc_b:
    check(inc_b["window"]["baseline"] == "prev_emergency",
          f"8б: окно должно упереться в предыдущую аварию, "
          f"получили {inc_b['window']['baseline']}")
    labels = [e.get("label") for e in inc_b["chronology"]]
    check("Хвост первой аварии" not in labels,
          "8в: события первой аварии попали во второй акт")
    check("Попытка пуска" in labels, "8г: события своей попытки потерялись")

# 9. Срез «висело на момент останова»
st = standing_faults(FAULTS, STOP_A)
names = {e["name"] for e in st}
check("Уровень ОЖ ниже нормы" in names,
      f"9a: давно висящая маска не попала в срез: {names}")
check("Давно снятое" not in names, "9б: снятая маска попала в срез")
check("Температура ОЖ" in names, "9в: маска самой аварии не попала в срез")
old = next((e for e in st if e["name"] == "Уровень ОЖ ниже нормы"), None)
if old:
    check(abs(old["age_sec"] - (STOP_A - T(10, 0)).total_seconds()) < 1,
          "9г: возраст маски посчитан неверно")
if inc_a:
    check(len(inc_a.get("standing") or []) == len(st),
          "9д: срез не попал в акт")

if _errors:
    print("ПРОВАЛЕНО:")
    for e in _errors:
        print("  -", e)
    sys.exit(1)
print("Все проверки пройдены.")
