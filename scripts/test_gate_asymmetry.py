# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""Гейт не снимает ранний сигнал несимметрии (v5.0.1, решение 11.10).

I₂, перекос фаз и ток нейтрали — предвестник отказа (Сининда 29.09). Гейт снимает
состав целиком, поэтому состав с несимметрией не снимается весь; старый вердикт
«отменить» несимметрию тоже не гасит — ни в статусе, ни в вердикте корпуса, ни в
бейдже календаря.

Запуск:  py -3 scripts/test_gate_asymmetry.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from corpus.preprocessor import _gate_suppressed  # noqa: E402
from online.status_assembler import (  # noqa: E402
    build_warning_prompt, compute_analytics_hash, gate_can_cancel, has_uncancellable,
    is_analytics_suppressed,
)
from web.segment_view import _seg_gate_checked  # noqa: E402

_errors: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        _errors.append(msg)


LOW = {"scenario": "COKING_RISK", "severity": "CAUTION", "fault_codes": [2342]}
I2 = {"scenario": "NEGATIVE_SEQUENCE", "severity": "CAUTION", "fault_codes": []}
NEU = {"scenario": "NEUTRAL_CURRENT", "severity": "CAUTION", "fault_codes": []}
PANEL = {"scenario": "CONTROLLER_FAULT", "severity": "WARNING", "fault_codes": [1453]}

check(gate_can_cancel({"panel_severity": "норма", "analytics_alarms": [LOW]}), "1: чистая аналитика — снимать можно")
check(not gate_can_cancel({"panel_severity": "норма", "analytics_alarms": [LOW, I2]}), "1б: с I₂ — нельзя")
check(not gate_can_cancel({"panel_severity": "норма", "analytics_alarms": [NEU]}), "1в: ток нейтрали — нельзя")
check(not gate_can_cancel({"panel_severity": "внимание", "analytics_alarms": [LOW]}), "1г: сигнал панели — нельзя, как раньше")
check(has_uncancellable([{"scenario": "PHASE_CURRENT_SPREAD"}]) and not has_uncancellable([LOW]), "1д: перекос фаз")

# Старый вердикт (хеш состава с несимметрией) не гасит её
h = compute_analytics_hash([LOW, I2])
check(not is_analytics_suppressed({"gate_suppressed_hash": h}, [LOW, I2]), "2: статус — старый вердикт не гасит I₂")
check(not _gate_suppressed({"gate_suppressed_hash": h}, [LOW, I2]), "2б: вердикт корпуса — не гасит")
check(not _seg_gate_checked([LOW, I2], h), "2в: бейдж «проверено ИИ» — нет")
h2 = compute_analytics_hash([LOW])
check(is_analytics_suppressed({"gate_suppressed_hash": h2}, [LOW]), "2г: без несимметрии снятие работает как раньше")
check(_gate_suppressed({"gate_suppressed_hash": h2}, [LOW]) and _seg_gate_checked([LOW], h2), "2д: корпус и бейдж — как раньше")

# Промпт: модель знает, что снять нельзя
s = {"severity_level": "предупреждение", "panel_severity": "норма", "analytics_severity": "предупреждение", "run_state": 3,
     "analytics_alarms": [LOW, I2], "panel_alarms": [], "mode_label": "Работа", "time_in_mode_sec": 0}
try:
    p = build_warning_prompt(s)
    check("несимметрии" in p and "отмена НЕДОСТУПНА" in p, "3: в промпте — отмена недоступна из-за несимметрии")
    s["analytics_alarms"] = [LOW]
    check("cancel ДОПУСТИМ" in build_warning_prompt(s), "3б: без несимметрии — допустим")
except Exception as e:  # промпту могут быть нужны поля, которых нет в заглушке
    check(False, f"3: build_warning_prompt упал на заглушке: {e!r}")
_ = PANEL

if _errors:
    print(f"FAIL — {len(_errors)} расхождений:")
    for e in _errors:
        print(f"  • {e}")
    sys.exit(1)
print("Все проверки пройдены.")
