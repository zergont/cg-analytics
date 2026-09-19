# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""Тест гейта акта аварийного останова (v4.9.70).

Проверяет `build_stop_incident`: когда сегментатор отдал вид стоп-сегмента,
решает он, а не характер-гейт. Ключевой сценарий — оконный дефект: период
RUN_STATE=3 не попал в окно пересчёта, rs_before=None, характер-гейт молчит.
Именно из-за него за 30 дней построился один акт на восемь аварий.

Запуск:  py -3 scripts/test_stop_incident_gate.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analytics.classifier import build_stop_incident, classify_stop_character  # noqa: E402

_errors: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        _errors.append(msg)


def T(h: int, m: int, s: int = 0) -> datetime:
    return datetime(2026, 9, 16, h, m, s, tzinfo=timezone.utc)


def rs(value: int, start: datetime, end: datetime | None = None) -> dict:
    return {"addr": 40011, "value": value, "state_start": start, "state_end": end}


def fp(severity: str, start: datetime, end: datetime | None = None) -> dict:
    return {"severity": severity, "fault_start": start, "fault_end": end,
            "addr": 40404, "bit": 3, "label": "High Coolant Temp"}


STOP = T(21, 35, 50)
CLEAR = T(21, 37, 52)

# Реальная цепочка 16.09: маска аварийной тяжести активна на момент останова,
# снята в 21:37:52 — сегментатор режет там и даёт голове вид EMERGENCY.
FAULTS = [fp("shutdown", T(21, 35, 50), CLEAR)]

# Оконный дефект: периода RUN_STATE=3 в окне НЕТ, есть только сам стоп.
RS_WINDOW_DEFECT = [rs(0, STOP, CLEAR)]
# Полное окно: видно падение из работы без охлаждения.
RS_FULL = [rs(3, T(20, 0, 0), STOP), rs(0, STOP, CLEAR)]
# Штатный останов через охлаждение.
RS_COOLDOWN = [rs(3, T(20, 0, 0), T(21, 30, 0)),
               rs(5, T(21, 30, 0), STOP),
               rs(0, STOP, CLEAR)]


# 1. Оконный дефект + EMERGENCY: характер-гейт молчит, акт всё равно строится.
v = classify_stop_character(RS_WINDOW_DEFECT, STOP, None)
check(v["is_incident"] is False,
      "1: предпосылка теста неверна — характер-гейт не должен срабатывать "
      "при rs_before=None")
inc = build_stop_incident(RS_WINDOW_DEFECT, FAULTS, STOP, CLEAR, None,
                          stop_kind="EMERGENCY")
check(inc is not None, "1: акт не построен, хотя вид сегмента EMERGENCY")
if inc is not None:
    check(inc["stop_kind"] == "EMERGENCY", "1: вид не записан в артефакт")
    check(inc["character"]["character"] == "immediate",
          "1: вердикт характера должен ехать в артефакт описанием")
    check(len(inc["chronology"]) > 0, "1: лента событий пуста")

# 2. SIMPLE: акта нет, даже если характер immediate (человек нажал «Стоп»,
#    маска аварийной тяжести на момент останова не висела).
inc = build_stop_incident(RS_FULL, [], STOP, CLEAR, None, stop_kind="SIMPLE")
check(inc is None, "2: акт построен на простом стопе")

# 3. Старая модель (флаг выключен, stop_kind не передан) — поведение прежнее.
check(build_stop_incident(RS_FULL, FAULTS, STOP, CLEAR, None) is not None,
      "3a: старый гейт перестал строить акт на падении из работы")
check(build_stop_incident(RS_COOLDOWN, FAULTS, STOP, CLEAR, None) is None,
      "3b: старый гейт построил акт на штатном останове через охлаждение")
check(build_stop_incident(RS_WINDOW_DEFECT, FAULTS, STOP, CLEAR, None) is None,
      "3c: старый гейт изменил поведение при оконном дефекте")

# 4. Вид перевешивает вердикт в обе стороны: штатное охлаждение, но панель
#    записала аварию — акт нужен; и наоборот падение без аварии — не нужен.
inc = build_stop_incident(RS_COOLDOWN, FAULTS, STOP, CLEAR, None,
                          stop_kind="EMERGENCY")
check(inc is not None, "4a: вид EMERGENCY не перевесил вердикт controlled")
if inc is not None:
    check(inc["character"]["character"] == "controlled",
          "4a: вердикт должен остаться честным (controlled), а не подгоняться")
check(build_stop_incident(RS_FULL, [], STOP, CLEAR, None, stop_kind="SIMPLE") is None,
      "4b: вид SIMPLE не перевесил вердикт immediate")

if _errors:
    print("ПРОВАЛЕНО:")
    for e in _errors:
        print("  -", e)
    sys.exit(1)
print("Все проверки пройдены.")
