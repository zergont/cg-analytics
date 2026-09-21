# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""Тест окна акта аварийного останова (v4.9.80).

Окно акта отсчитывается не от числа минут, а от последнего НОРМАЛЬНОГО
останова: если прошлый стоп прошёл штатно, то на тот момент с машиной было
всё в порядке, а всё после — возможные предвестники.

Нормальный останов — стоп-сегмент, который не аварийный И открылся не снятием
аварии. Второе условие несёт основную нагрузку: после аварии машину не пустят
без сброса, и как только сбросили, аварийный стоп становится простым. Но это
состояние восстановления, хвост той же аварии, а не самостоятельный останов.
Считать его эталоном значит обрезать серию попыток на первом звене, тогда как
следующая авария должна вбирать предыдущие — они одна история.

Расхолаживания в условии нет: останов без него бывает штатным.

Размер ленты ограничен числом событий, а не временем: длительность цикла
диагностична сама по себе, а вот контекстное окно запроса к ИИ конечно.

Запуск:  py -3 scripts/test_stop_incident_window.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analytics.classifier import (  # noqa: E402
    build_stop_incident,
    summarize_events,
    classify_stop_character,
    find_baseline_anchor,
    prev_stop_end,
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


# Цикл: работа → НОРМАЛЬНЫЙ стоп → пуск → работа → АВАРИЯ A →
#       (сброс, хвост) → попытка пуска → АВАРИЯ B
NORMAL_STOP = T(12, 55)
STOP_A = T(13, 26, 11)
CLEAR_A = T(13, 28, 52)      # сняли маску — здесь наш рез
RESTART = T(13, 29, 0)
STOP_B = T(13, 29, 55)
END_B = T(13, 31, 0)

RS_CHAIN = [
    rs(3, T(12, 0), T(12, 50)),
    rs(5, T(12, 50), NORMAL_STOP),          # расхолаживание — но оно не условие
    rs(0, NORMAL_STOP, T(13, 10)),
    rs(3, T(13, 10), STOP_A),
    rs(0, STOP_A, RESTART),                 # режется на EMERGENCY + хвост
    rs(3, RESTART, STOP_B),
    rs(0, STOP_B, END_B),
]
EVENTS = [
    ev("Сброс неисправностей", T(13, 5)),
    ev("Хвост первой аварии", T(13, 26, 19)),
    ev("Поздний хвост первой", T(13, 28, 0)),
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

# 2. ЯДРО: хвост аварии эталоном не становится, якорь уходит за первую аварию
got, why = find_baseline_anchor(ENUM, FAULTS, STOP_B, CFG)
check(got == NORMAL_STOP and why == "normal_stop",
      f"2: хвост аварии принят за эталон — якорь {got}/{why}, "
      f"ожидали {NORMAL_STOP}")

# 3. Расхолаживание НЕ обязательно: останов 3→0 напрямую — нормальный
plain = [rs(3, T(12, 0), T(12, 30)), rs(0, T(12, 30), T(12, 40)),
         rs(3, T(12, 40), STOP_A), rs(0, STOP_A, None)]
got, why = find_baseline_anchor(plain, [], STOP_A, CFG)
check(got == T(12, 30) and why == "normal_stop",
      f"3: останов без расхолаживания должен быть эталоном, получили {got}/{why}")

# 4. Фолбэк, когда эталона нет вовсе: окно не короче суток
only_emg = [rs(3, T(12, 0), STOP_A), rs(0, STOP_A, None)]
got, why = find_baseline_anchor(only_emg, FAULTS, STOP_A, CFG)
check(why == "fallback", f"4a: ожидали фолбэк, получили {why}")
check(got <= STOP_A - timedelta(hours=24), f"4б: фолбэк дал окно короче суток: {got}")

# 5. Пол вердикта — предыдущая стоянка любая (у ленты его нет)
check(prev_stop_end(ENUM, STOP_B) == RESTART,
      "5a: пол вердикта должен быть концом прошлой стоянки")
v_b = classify_stop_character(ENUM, STOP_B, CFG, fault_periods=FAULTS)
check(v_b["signals"]["passed_cooldown"] is False,
      "5б: вердикт увидел расхолаживание из прошлой попытки")

# 6. Акт первой аварии берёт цикл от нормального останова
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
    check("Поздний хвост первой" not in labels, "7в: событие за лагом попало в акт")

# 8. ЯДРО: акт второй аварии ВБИРАЕТ первую — это одна история
inc_b = build_stop_incident(ENUM, FAULTS, STOP_B, END_B, CFG,
                            stop_kind="EMERGENCY")
check(inc_b is not None, "8a: акт второй аварии не построен")
if inc_b:
    check(inc_b["window"]["from"] == NORMAL_STOP.isoformat(),
          f"8б: окно второй аварии обрезано: {inc_b['window']['from']}")
    labels = [e.get("label") for e in inc_b["chronology"]]
    check("Хвост первой аварии" in labels,
          "8в: первая авария должна попасть в акт второй — это серия попыток")
    check("Попытка пуска" in labels, "8г: события своей попытки потерялись")

# 9. Срез «висело на момент останова»
st = standing_faults(FAULTS, STOP_A)
names = {e["name"] for e in st}
check("Уровень ОЖ ниже нормы" in names, f"9a: давно висящая маска не в срезе: {names}")
check("Давно снятое" not in names, "9б: снятая маска попала в срез")
# Маска, поднявшаяся В момент останова, — это его причина, а не «висело»
check("Температура ОЖ" not in names,
      f"9б2: маска самой аварии попала в срез «висело»: {names}")
old_e = next((e for e in st if e["name"] == "Уровень ОЖ ниже нормы"), None)
if old_e:
    check(abs(old_e["age_sec"] - (STOP_A - T(10, 0)).total_seconds()) < 1,
          "9в: возраст маски посчитан неверно")

# 10. Свод по видам вмещает всю историю, детали — окрестность останова
many = (
    [{"ts": T(11, 0, i).isoformat(), "kind": "state", "addr": 40011,
      "name": "Режим"} for i in range(50)]
    + [{"ts": T(12, 0).isoformat(), "kind": "fault", "addr": 40404, "bit": 3,
        "severity": "shutdown", "name": "важный фронт"},
       {"ts": T(12, 30).isoformat(), "kind": "fault", "addr": 40404, "bit": 3,
        "severity": "shutdown", "name": "важный фронт"}]
    + [{"ts": T(13, 30).isoformat(), "kind": "state", "addr": 40599,
        "name": "после останова"}]
)
summary, detail = summarize_events(many, T(13, 0), detail_limit=5)
by_name = {g["name"]: g for g in summary}
check(len(summary) == 3, f"10a: ожидали три вида, получили {len(summary)}")
check(by_name["Режим"]["count"] == 50,
      f"10б: смены режима недосчитаны: {by_name['Режим']['count']}")
check(by_name["важный фронт"]["count"] == 2,
      "10в: повторный фронт не сгруппирован")
check(by_name["важный фронт"]["first"] == T(12, 0).isoformat()
      and by_name["важный фронт"]["last"] == T(12, 30).isoformat(),
      "10г: границы группы посчитаны неверно")
check(summary[0]["kind"] == "fault", "10д: фронты должны идти в своде первыми")
check(len(detail) == 6, f"10е: ожидали 5 до + 1 после, получили {len(detail)}")
check(detail[-1]["name"] == "после останова",
      "10ж: событие после останова должно остаться в деталях")
check(sum(g["count"] for g in summary) == len(many),
      "10з: свод должен покрывать все события без потерь")
_, only_sum = summarize_events(many, T(13, 0), detail_limit=0)
check(len(only_sum) == 1, "10и: при нулевом пороге остаётся только то, что после")

# 11. Акт несёт и свод, и общее число событий окна
if inc_b:
    check(inc_b.get("summary"), "11a: свод не попал в акт")
    check(inc_b.get("events_total", 0) >= len(inc_b["chronology"]),
          "11б: events_total меньше показанного")

if _errors:
    print("ПРОВАЛЕНО:")
    for e in _errors:
        print("  -", e)
    sys.exit(1)
print("Все проверки пройдены.")
