# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""Сводка частями — единый вердикт сегмента (v5.1.0).

Раньше вердикт считался в трёх местах: сводка (по эпизодам), корпус (по
детекциям и хешу снятия, которого в закрытых сегментах нет) и API. Сегмент с
«🟢 НОРМА, сняты ИИ» уходил модели как CAUTION «не пересматривать» и в календарь
без бейджа. Теперь корпус и API читают части сводки.

Запуск:  py -3 scripts/test_summary_parts.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analytics.serializer import (  # noqa: E402
    build_summary_md, build_summary_parts, no_data_summary_parts, render_summary_md,
)
from corpus.preprocessor import extract_verdict_alarm  # noqa: E402
from web.segment_view import gate_checked_from_parts, severity_from_parts, summary_parts  # noqa: E402

_errors: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        _errors.append(msg)


T0 = datetime(2026, 10, 9, 6, 0, tzinfo=timezone.utc)


def seg(rs=3, stop_kind=None, cc=None):
    sub = NS(characteristics={}, risk_accumulators=NS(coking_risk=NS(risk_level="GREEN")))
    return NS(run_state=rs, stop_kind=stop_kind, cause_close=cc, sequence_checks=[],
              t_start=T0.isoformat(), t_end=(T0 + timedelta(hours=24)).isoformat(),
              cause_open="DAILY_BOUNDARY", subsegments=[sub])


def ep(source="analytics", sev="CAUTION", supp=False, closed=False, before=False, sc="COKING_RISK"):
    return {"source": source, "severity": sev, "gate_suppressed": supp,
            "scenario": sc if source == "analytics" else "CONTROLLER_FAULT", "addr": 40407, "bit": 5,
            "t_open": T0 - timedelta(hours=400) if before else T0 + timedelta(hours=1),
            "t_close": T0 + timedelta(hours=2) if closed else None,
            "active_sec": 3600.0, "blind_sec": 0.0, "open_values_json": {"fault_name": "Кнопка"}}


# 1. #35739: стоп, риск нагара снят ИИ — НОРМА везде
p = build_summary_parts([seg(rs=0, stop_kind="SIMPLE")], [ep(supp=True, before=True)])
check(p["verdict"]["level"] == "НОРМА" and p["verdict"]["suppressed_n"] == 1, f"1: вердикт НОРМА: {p['verdict']}")
check(p["end_level"] == "НОРМА", "1б: на конец — норма (снятое не считается)")
check(p["remarks"][0]["before"] and not p["remarks"][0]["appeared"], "1в: фаза — было до сегмента")
check(render_summary_md(p) == build_summary_md([seg(rs=0, stop_kind="SIMPLE")], [ep(supp=True, before=True)]),
      "1г: md из частей = build_summary_md")
check(severity_from_parts(p, "SIMPLE") is None and gate_checked_from_parts(p), "1д: календарь — без цвета, «проверено ИИ»")
row = {"report_summary_json": json.dumps(p), "characteristics_json": {}, "gate_suppressed_hash": None}
check(extract_verdict_alarm(row) == ("норма", "НОРМА"), f"1е: корпус — норма из частей: {extract_verdict_alarm(row)}")

# 2. Работа: живое замечание аналитики и снятое — CAUTION, бейджа нет
p = build_summary_parts([seg()], [ep(sc="OIL_DILUTION"), ep(supp=True)])
check(p["verdict"]["level"] == "CAUTION" and severity_from_parts(p, None) == "CAUTION", "2: CAUTION")
check(not gate_checked_from_parts(p), "2б: при живом замечании «угрозы нет» не пишем")
check(extract_verdict_alarm({"report_summary_json": p}) == ("требует внимания", "CAUTION"), "2в: корпус — CAUTION")

# 3. Простой стоп: авария была и снята в окне — вердикт периода SHUTDOWN, цвет по концу — норма (v4.9.73)
p = build_summary_parts([seg(rs=0, stop_kind="SIMPLE")], [ep("panel", "SHUTDOWN", closed=True)])
check(p["verdict"]["level"] == "SHUTDOWN" and p["end_level"] == "НОРМА", f"3: {p['verdict']['level']}/{p['end_level']}")
check(severity_from_parts(p, "SIMPLE") is None and severity_from_parts(p, None) == "SHUTDOWN", "3б: правило стопа сохранено")
check(severity_from_parts(p, "EMERGENCY") == "SHUTDOWN", "3в: аварийный стоп — всегда SHUTDOWN")

# 4. Без связи
nd = no_data_summary_parts()
check(nd["no_data"] and nd["verdict"] is None and severity_from_parts(nd, None) is None, "4: без связи — вердикта нет")
check(extract_verdict_alarm({"report_summary_json": nd, "characteristics_json": {}})[1] == "НОРМА",
      "4б: корпус без частей вердикта — прежний расчёт (детекций нет → НОРМА)")

# 5. Старые сегменты — прежний расчёт
check(summary_parts(None) is None and summary_parts("{}") is None, "5: частей нет у старых")

# 6. Решение 1а: живой панельный CAUTION («Общая авария» + несброшенный 40012) — замечание
p = build_summary_parts([seg()], [ep("panel", "CAUTION")])
check(p["verdict"]["level"] == "CAUTION" and "ЗАМЕЧАНИЯ" in p["verdict"]["title"], f"6: панельный CAUTION: {p['verdict']}")
check(severity_from_parts(p, None) == "CAUTION", "6б: календарь — жёлтый")

# 7. Решение 2а: обрыв связи — строкой в замечаниях, на вердикт не влияет
s7 = seg()
s7.sequence_checks = [{"check": "subseg_data_coverage", "passed": False, "details": "отсутствие связи/данных: 19:20–00:31"}]
p = build_summary_parts([s7], [])
check(p["verdict"]["level"] == "НОРМА", f"7: связь не замечание: {p['verdict']}")
check("❗ отсутствие связи" in render_summary_md(p), "7б: строка о связи в замечаниях осталась")
s7.sequence_checks.append({"check": "order", "passed": False, "details": "порядок нарушен"})
check(build_summary_parts([s7], [])["verdict"]["remarks_n"] == 1, "7в: прочие проверки — замечания")


# 8. Простой стоп: маска снята панелью (fault_end), эпизод ещё в дебаунсе — не висит
class Det:
    def __init__(self, sc, values=None):
        self.scenario, self.values = sc, values or {}

    def to_dict(self):
        return {"scenario": self.scenario, "values": self.values}


s8 = seg(rs=0, stop_kind="SIMPLE")
s8.subsegments[0].detections = [Det("CONTROLLER_FAULT", {"addr": 40407, "bit": 5, "fault_end": "2026-10-09T10:05:00+00:00"})]
p = build_summary_parts([s8], [ep("panel", "WARNING")])
check(p["end_level"] == "НОРМА" and severity_from_parts(p, "SIMPLE") is None,
      f"8: снятая панелью маска не красит стоп: {p['end_level']}")
s8.subsegments[0].detections = [Det("CONTROLLER_FAULT", {"addr": 40407, "bit": 5})]
check(build_summary_parts([s8], [ep("panel", "WARNING")])["end_level"] == "WARNING", "8б: висящая — красит")

# 9. Поздняя отмена гейтом пересчитывает части
from analytics.serializer import apply_gate_suppression  # noqa: E402
p = build_summary_parts([seg()], [ep(sc="OIL_DILUTION")])
p2 = apply_gate_suppression(p, ["OIL_DILUTION"])
check(p["verdict"]["level"] == "CAUTION" and p2["verdict"]["level"] == "НОРМА"
      and p2["verdict"]["suppressed_n"] == 1 and gate_checked_from_parts(p2), f"9: {p2['verdict']}")
p3 = apply_gate_suppression(build_summary_parts([seg()], [ep(sc="NEGATIVE_SEQUENCE")]), ["NEGATIVE_SEQUENCE"])
check(p3["verdict"]["level"] == "CAUTION", "9б: несимметрию поздняя отмена тоже не снимает")
check(render_summary_md(p2).startswith("## 🟢 НОРМА\nПредупреждения аналитики (1) сняты ИИ."), "9в: md перепечатан")

# 10. Тревога, появившаяся в последнем цикле закрываемого сегмента
import online.engine as eng  # noqa: E402


class Det2(Det):
    def __init__(self, sc, sev, t, values=None):
        super().__init__(sc, values)
        self.severity, self.t_detected = sev, t

    def to_dict(self):
        return {"scenario": self.scenario, "severity": self.severity, "t_detected": self.t_detected,
                "values": self.values}


s10 = seg()
s10.subsegments[0].detections = [Det2("CONTROLLER_FAULT", "WARNING", "2026-10-10T05:59:50+00:00",
                                      {"addr": 40405, "bit": 1, "fault_name": "Горячий останов"})]
syn = eng._live_new_episodes([s10], [], {"CONTROLLER_FAULT|40405|1"}, T0 + timedelta(hours=24))
check(len(syn) == 1 and syn[0]["source"] == "panel" and syn[0]["t_close"] is None, f"10: синтетический эпизод: {syn}")
p = build_summary_parts([s10], syn)
check(p["verdict"]["level"] == "WARNING" and p["remarks"][0]["appeared"], "10б: в сводке, фаза «появилось»")
check(eng._live_new_episodes([s10], [], set(), T0) == [], "10в: без живых ключей — ничего")

# 11. Шапка модели: обнаружения из частей
from corpus.preprocessor import _remarks_from_summary  # noqa: E402
line = _remarks_from_summary({"report_summary_json": build_summary_parts([seg()], [ep(supp=True)])})
check(line and "снято ИИ" in line, f"11: шапка — снято ИИ: {line}")
s11 = seg()
s11.sequence_checks = [{"check": "subseg_data_coverage", "passed": False, "details": "отсутствие связи/данных: 19:20–00:31"},
                       {"check": "order", "passed": False, "details": "порядок нарушен"}]
line = _remarks_from_summary({"report_summary_json": build_summary_parts([s11], [])})
check("19:20–00:31 [связь, на вердикт не влияет]" in line and "порядок нарушен [проверка]" in line,
      f"11б: шапка — проверки: {line}")

# 12. Маска снята панелью и поднята снова в том же подсегменте — висит
s12 = seg(rs=0, stop_kind="SIMPLE")
s12.subsegments[0].detections = [
    Det("CONTROLLER_FAULT", {"addr": 40407, "bit": 5, "fault_end": "2026-10-09T10:05:00+00:00"}),
    Det("CONTROLLER_FAULT", {"addr": 40407, "bit": 5}),
]
p = build_summary_parts([s12], [ep("panel", "WARNING")])
check(p["end_level"] == "WARNING", f"12: поднятая снова маска висит: {p['end_level']}")
s12.subsegments[0].detections = []
check(build_summary_parts([s12], [ep("panel", "WARNING")])["end_level"] == "НОРМА",
      "12б: детекции в последнем подсегменте нет — не висит (как _seg_active_dets)")

# 13. Поздняя отмена снимает только открытые живые эпизоды
p = build_summary_parts([seg()], [ep(sc="OIL_DILUTION", closed=True), ep(sc="OIL_DILUTION")])
p2 = apply_gate_suppression(p, ["OIL_DILUTION"])
g = p2["remarks"][0]
check(g["suppressed"] == 1 and g["count"] == 2 and p2["verdict"]["level"] == "CAUTION",
      f"13: закрытая вспышка остаётся замечанием: {g}, {p2['verdict']}")
check(apply_gate_suppression(p2, ["OIL_DILUTION"]) == p2, "13б: повторный пересчёт ничего не меняет")
line = _remarks_from_summary({"report_summary_json": p2})
check("снято ИИ: 1 из 2" in line, f"13в: шапка — частичное снятие: {line}")
old = json.loads(json.dumps(build_summary_parts([seg()], [ep(sc="OIL_DILUTION")])))
for _g in old["remarks"]:
    _g.pop("open_live")
    _g.pop("at_end")
old["end_level"] = "CAUTION"
p3 = apply_gate_suppression(old, ["OIL_DILUTION"])
check(p3["remarks"][0]["suppressed"] == 1 and p3["verdict"]["level"] == "НОРМА",
      f"13г: части без open_live — снимается всё живое: {p3['verdict']}")
check(p3["end_level"] == "CAUTION", "13д: без отметок «на конец» уровень конца прежний")

# 14. Закрытая прежняя вспышка того же ключа не мешает синтетическому эпизоду
closed_prev = {"scenario": "CONTROLLER_FAULT", "addr": 40405, "bit": 1, "t_close": T0 + timedelta(hours=3)}
syn = eng._live_new_episodes([s10], [closed_prev], {"CONTROLLER_FAULT|40405|1"}, T0 + timedelta(hours=24))
check(len(syn) == 1, f"14: закрытый эпизод не покрывает текущую вспышку: {syn}")
open_same = dict(closed_prev, t_close=None)
check(eng._live_new_episodes([s10], [open_same], {"CONTROLLER_FAULT|40405|1"}, T0) == [],
      "14б: открытый эпизод того же ключа — синтетика не нужна")


# 15. Синтетический аналитический эпизод снятого гейтом состава — снят
class Risk:
    risk_level = "GREEN"

    def to_dict(self):
        return {"risk_level": "GREEN"}


s15 = seg()
s15.run_state_label = "RUN"
s15.subsegments[0].t_start = s15.subsegments[0].t_end = T0.isoformat()
s15.subsegments[0].risk_accumulators = NS(coking_risk=Risk())
s15.subsegments[0].detections = [Det2("OIL_DILUTION", "CAUTION", "2026-10-10T05:59:50+00:00")]
from online.status_assembler import compute_analytics_hash  # noqa: E402
h = compute_analytics_hash([{"scenario": "OIL_DILUTION"}])
supp = eng._gate_suppressed_scenarios({"suppressed_hash": h}, s15)
check(supp == {"OIL_DILUTION"}, f"15: состав снят гейтом: {supp}")
check(eng._gate_suppressed_scenarios({"suppressed_hash": "чужой"}, s15) == set(), "15б: другой состав — не снят")
syn = eng._live_new_episodes([s15], [], {"OIL_DILUTION"}, T0 + timedelta(hours=24), supp)
p = build_summary_parts([s15], syn)
check(syn[0]["gate_suppressed"] and p["verdict"]["level"] == "НОРМА", f"15в: синтетика снята: {p['verdict']}")

if _errors:
    print(f"FAIL — {len(_errors)} расхождений:")
    for e in _errors:
        print(f"  • {e}")
    sys.exit(1)
print("Все проверки пройдены.")
