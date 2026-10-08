# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""Тест тяжести аварийного стопа без аварийной маски (v4.9.95).

С v4.9.94 стоп аварийный и по типу последней неисправности (40013), а не
только по маскам: 1452 «Отказ включения автомата» роняет панель в Shutdown
без бита. Календарь и /api/machines смотрят на вид стопа и красят такой
сегмент красным, а живой статус, сводка сегмента и вердикт корпуса брали
тяжесть только из детекций — и писали «параметры в норме», «🟢 НОРМА» и
«норма — не пересматривать» рядом с актом аварии.

Правило владельца: аварию определяет реакция панели. Shutdown в 40013 —
реакция панели; Warning в 40013 без останова — нет, там остаётся CAUTION.

Запуск:  py -3 scripts/test_emergency_severity.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analytics.config import AnalyticsConfig  # noqa: E402
from analytics.fault_ref import FaultRef  # noqa: E402
from analytics.segmenter import segment  # noqa: E402
from analytics.serializer import build_summary_md  # noqa: E402
from corpus.preprocessor import build_claude_input, extract_verdict_alarm  # noqa: E402
from online.status_assembler import (  # noqa: E402
    build_structural_status, extract_alarm_text, format_status_text,
)

_errors: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        _errors.append(msg)


def T(h: int, m: int, s: int = 0) -> datetime:
    return datetime(2026, 9, 17, h, m, s, tzinfo=timezone.utc)


KB = Path(__file__).resolve().parent.parent / "knowledge_base"
FR = FaultRef(search_paths=[KB / "engines" / "cummins_kta50", KB / "controllers" / "pcc3300"])

# Панельная тревога 1452 — то, что реально висит в детекциях: CommonAlarm при
# несброшенном коде, тяжесть CAUTION
CA_1452 = {"scenario": "CONTROLLER_FAULT", "severity": "CAUTION",
           "trigger": "Общая авария (CommonAlarm) при несброшенном коде неисправности 1452",
           "fault_codes": [1452]}
MASK_SD = {"scenario": "CONTROLLER_FAULT", "severity": "SHUTDOWN",
           "trigger": "Высокая температура ОЖ (addr=40404, bit=3)", "fault_codes": []}
WARN_1453 = {"scenario": "CONTROLLER_FAULT", "severity": "CAUTION",
             "trigger": "Общая авария (CommonAlarm) при несброшенном коде неисправности 1453",
             "fault_codes": [1453]}


def row(stop_kind, dets, code=None) -> dict:
    vals = {"LAST_FAULT_CODE": {"value": code}} if code is not None else {}
    return {"run_state": 0,
            "characteristics_json": {"stop_kind": stop_kind},
            "active_detections_json": dets,
            "current_values_json": {"values": vals}}


# ── 1. Живой статус ──────────────────────────────────────────────────────────
s = build_structural_status(row("EMERGENCY", [CA_1452], 1452), FR)
check(s["panel_severity"] == "авария" and s["severity_level"] == "авария",
      f"1a: авария 1452 не авария в статусе: {s['panel_severity']}/{s['severity_level']}")
check("код 1452" in (s.get("emergency_text") or "")
      and "Отказ включения автомата" in (s.get("emergency_text") or ""),
      f"1б: подпись аварии без кода/имени: {s.get('emergency_text')!r}")
txt = format_status_text(s)
check("🔴 панель: аварийный останов, код 1452" in txt and "в норме" not in txt,
      f"1в: строка статуса: {txt!r}")
check(extract_alarm_text(s) == s["emergency_text"],
      f"1г: текст тревоги подписан не аварией: {extract_alarm_text(s)!r}")

# Без справочника — код без имени, но всё равно авария
s = build_structural_status(row("EMERGENCY", [], 1452))
check(s.get("emergency_text") == "аварийный останов, код 1452",
      f"1д: без справочника: {s.get('emergency_text')!r}")
# Без кода — просто аварийный останов
s = build_structural_status(row("EMERGENCY", []))
check(s.get("emergency_text") == "аварийный останов", f"1е: {s.get('emergency_text')!r}")

# Авария по маске — подпись по маске, текст тревоги по-прежнему составом
s = build_structural_status(row("EMERGENCY", [CA_1452, MASK_SD], 151), FR)
check(s.get("emergency_text") == MASK_SD["trigger"],
      f"1ж: авария по маске подписана не маской: {s.get('emergency_text')!r}")
check(extract_alarm_text(s) == f"{MASK_SD['trigger']} + ещё 1",
      f"1з: состав тревог аварии по маске сломан: {extract_alarm_text(s)!r}")

# Warning без останова (1126246373: 1453 при Warning в 40013) — не авария
s = build_structural_status(row("SIMPLE", [WARN_1453], 1453), FR)
check(s["panel_severity"] == "норма" and s.get("emergency_text") is None,
      f"1и: предупреждение без останова стало аварией: {s['panel_severity']}")
check("🔴" not in format_status_text(s), "1к: красная строка у простого стопа")

# ── 2. Сводка сегмента (report_summary_md) ───────────────────────────────────
cfg = AnalyticsConfig.from_pair(KB, "pcc3300", "cummins_kta50")
STOP, RESET, START = T(20, 28, 37), T(20, 32, 13), T(20, 36, 17)
ENUM = [
    {"addr": 40011, "value": 3, "state_start": T(20, 0), "state_end": STOP},
    {"addr": 40011, "value": 0, "state_start": STOP, "state_end": START},
    {"addr": 40011, "value": 3, "state_start": START, "state_end": None},
    {"addr": 40013, "value": 4, "state_start": STOP, "state_end": RESET},
    {"addr": 40013, "value": 0, "state_start": RESET, "state_end": None},
]
segs = segment(ENUM, [], [], [], cfg, "r", "pcc3300", 1, "e", T(20, 0), T(21, 0))
head = next((x for x in segs if x.stop_kind == "EMERGENCY"), None)
tail = next((x for x in segs if x.run_state == 0 and x.stop_kind == "SIMPLE"), None)
check(head is not None and tail is not None, "2a: нарезка не дала голову и хвост")
if head is not None:
    md = build_summary_md([head], episodes=[])
    check("🔴 АВАРИЯ" in md, f"2б: вердикт аварии 1452: {md.splitlines()[:2]}")
    check("Авария снята" in md and "3м" in md,
          f"2в: нет строки «Авария снята» от начала стопа (3м36с): {md!r}")
    # Продолжение после суточного реза начала аварии не знает — без MTTR
    segs2 = segment(ENUM, [], [], [], cfg, "r", "pcc3300", 1, "e", T(20, 30), T(21, 0))
    cont = next((x for x in segs2 if x.stop_kind == "EMERGENCY"), None)
    if cont is not None:
        md2 = build_summary_md([cont], episodes=[])
        check("🔴 АВАРИЯ" in md2 and "Авария снята" not in md2,
              f"2г: продолжение соврало временем до устранения: {md2!r}")
if tail is not None:
    md = build_summary_md([tail], episodes=[])
    check("🟢 НОРМА" in md, f"2д: хвост после сброса не норма: {md.splitlines()[:2]}")

# ── 3. Вердикт корпуса ───────────────────────────────────────────────────────
def chars(stop_kind, dets) -> dict:
    return {"stop_kind": stop_kind, "subsegments": [{"detections": dets}]}


check(extract_verdict_alarm({"characteristics_json": chars("EMERGENCY", [CA_1452])})
      == ("авария", "SHUTDOWN"), "3a: вердикт корпуса аварии 1452 не авария")
check(extract_verdict_alarm({"characteristics_json": chars("SIMPLE", [WARN_1453])})
      == ("требует внимания", "CAUTION"), "3б: вердикт простого стопа с CAUTION изменился")
inp = build_claude_input({"characteristics_json": chars("EMERGENCY", [CA_1452]),
                          "run_state": 0})
check("Вердикт: авария" in inp and "Уровень тревоги: SHUTDOWN" in inp,
      "3в: ИИ получает «норма — не пересматривать» рядом с аварией")

if _errors:
    print(f"FAIL — {len(_errors)} расхождений:")
    for e in _errors:
        print(f"  • {e}")
    sys.exit(1)
print("Все проверки пройдены.")
