# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""Тест окраски стоп-сегмента (v4.9.73).

В режиме 0 панель копит сообщения, не меняя режим: максимум за период оставил
бы стоянку красной до конца суток. Поэтому СТОП красится по активному на конец
окна, аварийный СТОП — всегда красный (снятие аварии и есть его граница),
остальные режимы — по максимуму за период, как и раньше.

Запуск:  py -3 scripts/test_stop_severity.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from web.segment_view import _seg_gate_checked, _seg_severity  # noqa: E402
from online.status_assembler import compute_analytics_hash  # noqa: E402

_errors: list[str] = []


def check(got, want, msg: str) -> None:
    if got != want:
        _errors.append(f"{msg}: ожидали {want!r}, получили {got!r}")


def fault(severity: str, bit: int, fault_end: str | None = None) -> dict:
    return {"scenario": "CONTROLLER_FAULT", "severity": severity,
            "values": {"addr": 40404, "bit": bit, "fault_end": fault_end}}


def analytic(scenario: str = "RPM_UNDERSPEED") -> dict:
    return {"scenario": scenario, "severity": "CAUTION", "fault_codes": [], "values": {}}


def chars(*subs: list[dict]) -> dict:
    return {"subsegments": [{"detections": list(d)} for d in subs]}


CLEARED = "2026-09-16T21:37:52+00:00"

# 1. Аварийный стоп красный всегда — даже когда маску уже сняли
check(_seg_severity(chars([fault("SHUTDOWN", 3, CLEARED)]), stop_kind="EMERGENCY"),
      "SHUTDOWN", "1a: снятая маска обесцветила аварийный стоп")
check(_seg_severity(chars([]), stop_kind="EMERGENCY"),
      "SHUTDOWN", "1b: пустой аварийный стоп не красный")

# 2. Простой стоп: снятая маска цвет не держит
check(_seg_severity(chars([fault("WARNING", 5, CLEARED)]), stop_kind="SIMPLE"),
      None, "2: снятая маска красит простой стоп")

# 3. Простой стоп: висящая маска красит
check(_seg_severity(chars([fault("WARNING", 5)]), stop_kind="SIMPLE"),
      "WARNING", "3: висящая маска не покрасила простой стоп")

# 4. Ядро правила: авария была и снята, сейчас висит предупреждение —
#    стоп оранжевый, а не красный до конца суток
check(_seg_severity(chars([fault("SHUTDOWN", 3, CLEARED)], [fault("WARNING", 5)]),
                    stop_kind="SIMPLE"),
      "WARNING", "4: простой стоп остался в цвете снятой аварии")

# 5. Прочие режимы не тронуты: максимум за период
check(_seg_severity(chars([fault("SHUTDOWN", 3, CLEARED)], [fault("WARNING", 5)])),
      "SHUTDOWN", "5: рабочий сегмент потерял максимум за период")

# 6. Аналитика: её видно, пока гейт не отменил
check(_seg_severity(chars([analytic()]), stop_kind="SIMPLE"),
      "CAUTION", "6a: аналитика не покрасила простой стоп")
_h = compute_analytics_hash([analytic()])
check(_seg_severity(chars([analytic()]), gate_suppressed_hash=_h, stop_kind="SIMPLE"),
      None, "6b: вердикт гейта не подавил аналитику на простом стопе")
check(_seg_severity(chars([analytic()]), gate_suppressed_hash=_h),
      None, "6c: вердикт гейта не подавил аналитику в обычном сегменте")

# 7. Старая модель (вида нет) на стоп-сегменте ведёт себя как раньше
check(_seg_severity(chars([fault("SHUTDOWN", 3, CLEARED)], [fault("WARNING", 5)]),
                    stop_kind=None),
      "SHUTDOWN", "7: поведение без вида изменилось")

# 8. Вердикт гейта не перебивает живой сигнал панели
_an = {"scenario": "RPM_UNDERSPEED", "severity": "CAUTION", "fault_codes": []}
_h8 = compute_analytics_hash([_an])
check(_seg_gate_checked([_an], _h8), True,
      "8a: вердикт по чистой панели должен действовать")
check(_seg_gate_checked([_an, fault("SHUTDOWN", 5)], _h8), False,
      "8б: вердикт действует при живой аварии — «проверено ИИ» поверх красного")
check(_seg_gate_checked([_an, fault("WARNING", 5)], _h8), False,
      "8в: вердикт действует при живом предупреждении панели")
check(_seg_severity(chars([_an, fault("SHUTDOWN", 5)]),
                    gate_suppressed_hash=_h8, stop_kind="SIMPLE"),
      "SHUTDOWN", "8г: авария потеряла цвет из-за вердикта гейта")

if _errors:
    print("ПРОВАЛЕНО:")
    for e in _errors:
        print("  -", e)
    sys.exit(1)
print("Все проверки пройдены.")
