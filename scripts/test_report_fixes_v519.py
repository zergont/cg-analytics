# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""v5.1.9: исправления по отчётам 12.10 — повтор снятого гейтом состава, время
неподтверждённых эпизодов, начало пуска, конец накопления нагара, журнал «активно
на конец», оценка токенов, мелочи отчёта.

Запуск:  py -3 scripts/test_report_fixes_v519.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from online.status_assembler import GATE_RULES_VERSION as RULES  # noqa: E402

from analytics.contract import (  # noqa: E402
    CokingRisk, Detection, DerivedMetrics, RiskAccumulators, Segment, Subsegment,
)
from analytics.serializer import build_summary_parts, render_summary_md, to_markdown  # noqa: E402
from corpus.preprocessor import _remarks_from_summary  # noqa: E402
from online import engine as eng  # noqa: E402
from online import manager as mgr  # noqa: E402

_errors: list[str] = []
MSK = ZoneInfo("Europe/Moscow")
T0 = datetime(2026, 10, 11, 6, 0, tzinfo=timezone.utc)


def check(cond: bool, msg: str) -> None:
    if not cond:
        _errors.append(msg)


def det(sc):
    return Detection(scenario=sc, severity="CAUTION", t_detected=T0.isoformat(), source="METRIC_RULE",
                     trigger="t", related_roles=[], fault_codes=[], description_key="k", values={})


def sub(i, dets, chars=None):
    s = T0 + timedelta(hours=i - 1)
    return Subsegment(id=f"s.{i}", parent_segment_id="s", t_start=s.isoformat(),
                      t_end=(s + timedelta(hours=1)).isoformat(), duration_sec=3600,
                      load_zone="NORMAL", cause_open="SEG_START", cause_close=None,
                      characteristics=chars or {}, derived_metrics=DerivedMetrics(),
                      risk_accumulators=RiskAccumulators(), detections=dets, data_quality=1.0)


def seg(subs, rs=3, events=None):
    return Segment(id="s", router_sn="R", equip_type="pcc", panel_id=1, engine_sn="", run_state=rs,
                   run_state_label="Работа", t_start=T0.isoformat(),
                   t_end=(T0 + timedelta(hours=len(subs))).isoformat(), duration_sec=3600 * len(subs),
                   engine_hours_start=None, cause_open="REPORT_START", cause_close=None,
                   preamble_included=False, data_quality=1.0, subsegments=subs,
                   sequence_checks=[], events=events or [])


def ep(sc, a, b, **kw):
    d = {"source": "analytics", "scenario": sc, "severity": "CAUTION", "gate_suppressed": False,
         "t_open": T0 + timedelta(minutes=a), "t_close": T0 + timedelta(minutes=b),
         "active_sec": (b - a) * 60.0, "blind_sec": 0.0}
    d.update(kw)
    return d


# ── 1. Гейт: повтор уже снятого состава — снятие применяется заново ────────
calls: list[tuple] = []


async def _set_supp(r, e, p, suppressed_hash, segment_id=None, ts=None):
    calls.append(("supp", suppressed_hash, segment_id))
    return True


async def _mark(r, e, p, scs):
    calls.append(("mark", tuple(scs)))


async def _log(r, e, p, segment_id=None, ts=None, event=None):
    calls.append(("log", event.get("decision"), event.get("provider")))
    return True


mgr.online_db.set_segment_gate_suppression = _set_supp
mgr.online_db.set_episodes_gate_suppressed = _mark
mgr.online_db.append_segment_gate_event = _log

alarms = [{"scenario": "OIL_DILUTION", "fault_codes": [], "ref_codes": [143, 415]},
          {"scenario": "LIMIT_PROXIMITY", "fault_codes": [], "ref_codes": [214]}]
struct = {"analytics_alarms": alarms, "panel_alarms": [], "panel_severity": "норма",
          "analytics_suppressed": False, "severity_level": "предупреждение"}
row = {"id": 35771, "gate_suppressed_hash": "b73fbb556aef",
       "gate_log": json.dumps([{"fault_hash": "5cf22d2ee7d6", "decision": "cancel", "decision_applied": True,
                                "rules": RULES},
                               {"fault_hash": "b73fbb556aef", "decision": "cancel", "decision_applied": True,
                                "rules": RULES},
                               {"fault_hash": "aaaa", "decision": "pass", "decision_applied": False},
                               # снятие по старым правилам гейта заново не применяется (v5.1.12)
                               {"fault_hash": "old0", "decision": "cancel", "decision_applied": True}])}
check(mgr._cancelled_hashes(row) == {"5cf22d2ee7d6", "b73fbb556aef"}, "1: снятые составы — из журнала гейта")
_engine = NS(router_sn="R", equip_type="pcc", panel_id=1)
asyncio.run(mgr.OnlineManager._reapply_cancel(None, _engine, row, struct, "5cf22d2ee7d6"))
check([c[0] for c in calls] == ["supp", "mark", "log"] and calls[1][1] == ("OIL_DILUTION", "LIMIT_PROXIMITY")
      and calls[2][1:] == ("cancel", "repeat"), f"1б: снятие применено заново, эпизоды помечены, журнал: {calls}")
from online.status_assembler import compute_analytics_hash  # noqa: E402
calls.clear()
asyncio.run(mgr.OnlineManager._reapply_cancel(
    None, _engine, dict(row, gate_suppressed_hash=compute_analytics_hash(alarms)), struct, "5cf22d2ee7d6"))
check(not calls, "1в: хеш снятия уже этот — повтора нет")
calls.clear()
asyncio.run(mgr.OnlineManager._reapply_cancel(
    None, _engine, row, dict(struct, analytics_alarms=alarms + [{"scenario": "NEGATIVE_SEQUENCE"}]), "x"))
check(not calls, "1г: несимметрия — не снимаем и повтором")

# ── 2. Неподтверждённые — со временем, в сводке и во входе модели ──────────
s2 = seg([sub(1, [det("COOLING_FAILURE")]), sub(2, [])])
p2 = build_summary_parts([s2], [ep("COOLING_FAILURE", 10, 18), ep("COOLING_FAILURE", 70, 76)],
                         tz=MSK, t_from=T0, t_to=T0 + timedelta(hours=2))
g = next(x for x in p2["remarks"] if x["scenario"] == "COOLING_FAILURE")
check(g["unconfirmed"] == 1 and g["unconfirmed_at"] == ["11.10 10:10–10:16"], f"2: время неподтверждённого: {g}")
md2 = render_summary_md(p2)
check("не подтверждено: 1 из 2 (11.10 10:10–10:16)" in md2, f"2б: в сводке:\n{md2}")
tag = _remarks_from_summary({"report_summary_json": p2})
check("ещё 1 из 2 — только по живым данным (11.10 10:10–10:16): в отчёте ниже их нет" in (tag or ""),
      f"2в: вход модели: {tag}")

# ── 3. Начало пуска — по накопленному времени режима ───────────────────────
head = NS(run_state=3, t_start=T0.isoformat())
check(eng._origin_by_rs({3: 151141.0}, head) == T0 - timedelta(seconds=151141),
      "3: начало пуска = голова − накопленное время режима")
check(eng._origin_by_rs({0: 100.0}, head) is None and eng._origin_by_rs({}, head) is None,
      "3б: нет времени этого режима — запасной путь")

# ── 4. Конец накопления нагара на стоящей машине ──────────────────────────
cr = CokingRisk()
cr.debt_sec = 23164.0
fake = NS(inherited_coking_risk=cr, inherited_run_state_sec={0: 3600.0 * 24})
eng.OnlinePollEngine._fill_coking_accum_end(fake, T0)
check(cr.last_accum_ts == (T0 - timedelta(hours=24)).isoformat(), f"4: по началу стоянки: {cr.last_accum_ts}")
cr2 = CokingRisk()
cr2.debt_sec = 10.0
cr2.last_accum_ts = "2026-09-29T13:06:30+00:00"
eng.OnlinePollEngine._fill_coking_accum_end(NS(inherited_coking_risk=cr2, inherited_run_state_sec={0: 50.0}), T0)
check(cr2.last_accum_ts == "2026-09-29T13:06:30+00:00", "4б: известный конец не трогаем")

# ── 5. Журнал: активное на конец — «активно», без двойного пробела ─────────
evs = [{"type": "FAULT", "t": "2026-05-25T22:13:09+00:00", "fault_end": None, "name_ru": "Не в автоматическом режиме",
        "severity": None, "duration_sec": 12_000_000},
       {"type": "FAULT", "t": T0.isoformat(), "fault_end": (T0 + timedelta(minutes=5)).isoformat(),
        "name_ru": "Остановка", "severity": "shutdown", "duration_sec": 300}]
md5 = to_markdown([seg([sub(1, [], {"HEARTBEAT": {"median": 1, "min": 1, "max": 1, "unit": None}})], rs=0,
                       events=evs)], "R", "pcc", 1, T0, T0 + timedelta(hours=1), "5.1.9", tz=MSK)
check("- `Не в автоматическом режиме` @" in md5 and "активно на конец периода" in md5,
      "5: висящее событие — «активно на конец», без двойного пробела")
check("🔴 `Остановка`" in md5 and md5.count("активно на конец периода") == 1, "5б: снятое — без пометки, с эмодзи")
check("| None |" not in md5, "5в: единица None не печатается")

# 5г. Снятая ровно на конце сегмента (рез SHUTDOWN_CLEARED) — не «активно» и не «не сброшена»
_end = T0 + timedelta(hours=1)
ev_cut = [{"type": "FAULT", "t": T0.isoformat(), "fault_end": _end.isoformat(), "name_ru": "Кнопка E-Stop",
           "severity": "shutdown", "duration_sec": 3600},
          {"type": "FAULT", "t": T0.isoformat(), "fault_end": _end.isoformat(), "name_ru": "Готов к нагрузке",
           "severity": None, "duration_sec": 3600}]
from analytics.fault_ref import FaultRef  # noqa: E402
try:
    _fr = FaultRef(str(ROOT / "knowledge_base" / "controllers" / "pcc3300"))
except Exception:
    _fr = None
md5g = to_markdown([seg([sub(1, [])], rs=0, events=ev_cut)], "R", "pcc", 1, T0, _end, "5.1.9", tz=MSK,
                   fault_ref=_fr)
check("активно на конец периода" not in md5g, "5г: снятая на резе — без «активно на конец»")
check("не сброшены на закрытие" not in md5g, "5д: снятая на резе — не в «Активных неисправностях»")

# ── 6. «С начала» — со слепой долей до периода ─────────────────────────────
e6 = ep("OIL_DILUTION", 0, 120, t_open=T0 - timedelta(hours=40), t_close=None, blind_sec=6719.0)
p6 = build_summary_parts([seg([sub(1, [det("OIL_DILUTION")]), sub(2, [det("OIL_DILUTION")])])], [e6],
                         tz=MSK, t_from=T0, t_to=T0 + timedelta(hours=2))
check("из них 1ч 51м 59с без связи)" in render_summary_md(p6), f"6: слепая доля «с начала»:\n{render_summary_md(p6)}")
# 6б. Слепая доля «с начала» — по всем онсетам и не меньше, чем за период
p6b = build_summary_parts([seg([sub(1, [det("OIL_DILUTION")]), sub(2, [det("OIL_DILUTION")])])],
                          [dict(e6, t_close=T0 + timedelta(minutes=10), blind_sec=600.0),
                           ep("OIL_DILUTION", 30, 90, blind_sec=2400.0)],
                          tz=MSK, t_from=T0, t_to=T0 + timedelta(hours=2))
g6 = p6b["remarks"][0]
check(g6["total_blind_sec"] >= 3000 and g6["total_blind_sec"] >= g6.get("period_blind_sec", 0),
      f"6б: «с начала» без связи — по всем онсетам: {g6.get('total_blind_sec')} / {g6.get('period_blind_sec')}")
# 6в. Вердикт разбора, перенесённый в новую строку, виден повтору снятия
check(mgr._cancelled_hashes({"gate_log": [{"fault_hash": "X", "decision": "cancel", "decision_applied": True,
                                           "provider": "carried", "rules": RULES}]}) == {"X"},
      "6в: перенесённый вердикт виден повтору снятия")

# ── 7. Оценка токенов ───────────────────────────────────────────────────────
from corpus import worker  # noqa: E402
check(worker._EST_CHARS_PER_TOKEN == 1.7, "7: 1,7 символа на токен")

if _errors:
    print(f"FAIL — {len(_errors)} расхождений:")
    for x in _errors:
        print(f"  • {x}")
    sys.exit(1)
print("Все проверки пройдены.")
