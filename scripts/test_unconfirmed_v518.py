# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""v5.1.8: эпизоды аналитики, не подтверждённые итоговым расчётом сегмента, — с
пометкой и вне вердикта (решение 11.10, а); «останов без охлаждения» — один раз
на останов, а не на каждый следующий стоп-сегмент.

Запуск:  py -3 scripts/test_unconfirmed_v518.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analytics import binding  # noqa: E402
from analytics.contract import (  # noqa: E402
    Detection, DerivedMetrics, RiskAccumulators, Segment, Subsegment,
)
from analytics.segmenter import _check_cooldown_transition  # noqa: E402
from analytics.serializer import (  # noqa: E402
    apply_gate_suppression, build_summary_parts, render_summary_md,
)
from corpus.preprocessor import _remarks_from_summary  # noqa: E402

_errors: list[str] = []
MSK = ZoneInfo("Europe/Moscow")
T0 = datetime(2026, 10, 10, 6, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(hours=2)


def check(cond: bool, msg: str) -> None:
    if not cond:
        _errors.append(msg)


def det(sc):
    return Detection(scenario=sc, severity="CAUTION", t_detected=T0.isoformat(), source="METRIC_RULE",
                     trigger="t", related_roles=[], fault_codes=[], description_key="k", values={})


def sub(i, dets, zone="NORMAL"):
    s = T0 + timedelta(hours=i - 1)
    return Subsegment(id=f"s.{i}", parent_segment_id="s", t_start=s.isoformat(),
                      t_end=(s + timedelta(hours=1)).isoformat(), duration_sec=3600,
                      load_zone=zone, cause_open="SEG_START", cause_close=None,
                      characteristics={}, derived_metrics=DerivedMetrics(),
                      risk_accumulators=RiskAccumulators(), detections=dets, data_quality=1.0)


def seg(subs, rs=3, t0=T0, hours=2, cause_open="REPORT_START"):
    return Segment(id="s", router_sn="R", equip_type="pcc", panel_id=1, engine_sn="", run_state=rs,
                   run_state_label="?", t_start=t0.isoformat(),
                   t_end=(t0 + timedelta(hours=hours)).isoformat(), duration_sec=hours * 3600,
                   engine_hours_start=None, cause_open=cause_open, cause_close=None,
                   preamble_included=False, data_quality=1.0, subsegments=subs,
                   sequence_checks=[], events=[])


def ep(sc, a, b=None, supp=False, src="analytics"):
    return {"source": src, "scenario": sc, "severity": "CAUTION", "gate_suppressed": supp,
            "addr": 40408, "bit": 11, "open_values_json": {"fault_name": "Общая авария"},
            "t_open": T0 + timedelta(minutes=a),
            "t_close": (T0 + timedelta(minutes=b)) if b is not None else None,
            "active_sec": ((b or 120) - a) * 60.0, "blind_sec": 0.0}


def parts(eps, s=None):
    return build_summary_parts([s or seg([sub(1, [det("OIL_DILUTION")]), sub(2, [])])], eps,
                               tz=MSK, t_from=T0, t_to=T1)


def grp(p, sc):
    return next((g for g in p["remarks"] if g.get("scenario") == sc), None)


# 1. Подтверждённый и призрак: вердикт считает только подтверждённый
p = parts([ep("OIL_DILUTION", 10, 40), ep("COOLING_FAILURE", 70, 76)])
cf, oil = grp(p, "COOLING_FAILURE"), grp(p, "OIL_DILUTION")
check(oil and not oil["unconfirmed"] and cf and cf["unconfirmed"] == 1,
      f"1: OIL подтверждён, COOLING — призрак: {oil and oil['unconfirmed']}, {cf and cf['unconfirmed']}")
check(p["verdict"]["level"] == "CAUTION" and p["verdict"]["remarks_n"] == 1
      and p["verdict"]["unconfirmed_n"] == 1, f"1б: в вердикте только подтверждённый: {p['verdict']}")
md = render_summary_md(p)
check("ЗАМЕЧАНИЯ К РАБОТЕ — 1" in md and "по живым данным (" in md and "в итоговом расчёте не подтверждено" in md,
      f"1в: пометка в md:\n{md}")
check(p["remarks"][0]["scenario"] == "OIL_DILUTION", "1г: неподтверждённое — ниже живого")

# 2. Только призрак — НОРМА с пояснением
p = parts([ep("COOLING_FAILURE", 70, 76)])
check(p["verdict"]["level"] == "НОРМА" and "итоговым расчётом не подтверждены" in (p["verdict"]["note"] or ""),
      f"2: НОРМА с пояснением: {p['verdict']}")

# 3. Открытый призрак: не «активен на конец», поздняя отмена гейтом не уводит снятые за count
p = parts([ep("COOLING_FAILURE", 70)])
g = grp(p, "COOLING_FAILURE")
check(g["unconfirmed"] == 1 and g["open_live"] == 0 and not g["at_end"] and p["end_level"] == "НОРМА",
      f"3: открытый призрак не висит: {g}")
q = apply_gate_suppression(p, {"COOLING_FAILURE"})
gq = grp(q, "COOLING_FAILURE")
check(gq["suppressed"] + gq["unconfirmed"] <= gq["count"] and q["verdict"]["level"] == "НОРМА",
      f"3б: поздняя отмена — без двойного счёта: {gq}")

# 4. Снятое ИИ и неподтверждённое не суммируются
p = parts([ep("COOLING_FAILURE", 70, 76, supp=True)])
g = grp(p, "COOLING_FAILURE")
check(g["suppressed"] == 1 and g["unconfirmed"] == 0, f"4: снятие главнее: {g}")

# 5. Эпизод с начала до окна, детекция в окне — подтверждён; панель не сверяется
e5 = dict(ep("OIL_DILUTION", 0), t_open=T0 - timedelta(hours=30))
p = parts([e5, ep("CONTROLLER_FAULT", 5, src="panel")])
check(not grp(p, "OIL_DILUTION")["unconfirmed"] and not grp(p, "CONTROLLER_FAULT")["unconfirmed"],
      "5: «с начала» подтверждён детекцией в окне; панель — всегда")

# 6. Вход модели: пометка в шапке
p = parts([ep("OIL_DILUTION", 10, 40), ep("COOLING_FAILURE", 70, 76)])
r = _remarks_from_summary({"report_summary_json": p})
check("только по живым данным" in (r or "") and "в отчёте ниже" in (r or ""), f"6: шапка корпуса: {r}")

# 6б. Проверки перехода не сверяются: итоговый расчёт стопа начинается с его головы
# и работы перед ним не видит (35610: настоящее «без охлаждения» выпало бы из вердикта)
st = seg([sub(1, [det("OIL_DILUTION")]), sub(2, [])], rs=0)
p = parts([ep("COOLDOWN_VIOLATION", 0, 1)], s=st)
g = grp(p, "COOLDOWN_VIOLATION")
check(g and g["unconfirmed"] == 0 and p["verdict"]["level"] == "CAUTION" and p["verdict"]["remarks_n"] == 1,
      f"6б: «без охлаждения» без работы в окне — в вердикте: {g}, {p['verdict']}")
p = parts([ep("WARMUP_VIOLATION", 0, 1)])
check(grp(p, "WARMUP_VIOLATION")["unconfirmed"] == 0, "6в: «пуск без прогрева» — тоже")

# 7. «Без охлаждения» — только на первом стопе после работы
cfg = binding.build_config(ROOT / "knowledge_base", controller_id="pcc3300", engine_id="cummins_kta50")
run = seg([sub(1, [], zone="ELEVATED")], rs=3, hours=1)
run.subsegments[0].characteristics["LOAD_PCT"] = {"max": 80.0}
st1 = seg([sub(2, [])], rs=0, t0=T0 + timedelta(hours=1), hours=1, cause_open="RUN_STATE_CHANGE")
st2 = seg([sub(3, [])], rs=0, t0=T0 + timedelta(hours=2), hours=1, cause_open="SHUTDOWN_CLEARED")
_la = cfg.role_to_addr("LOAD_PCT")
_rows = {_la: [{"ts": T0 + timedelta(hours=1) - timedelta(seconds=s), "value": 80.0} for s in (200, 100, 10)]}
_check_cooldown_transition(st1, [run], cfg, Detection, _rows)
_check_cooldown_transition(st2, [run, st1], cfg, Detection, _rows)
n1 = sum(d.scenario == "COOLDOWN_VIOLATION" for d in st1.subsegments[0].detections)
n2 = sum(d.scenario == "COOLDOWN_VIOLATION" for d in st2.subsegments[0].detections)
check(n1 == 1 and n2 == 0 and not st2.sequence_checks, f"7: один раз на останов: {n1}/{n2}")

if _errors:
    print(f"FAIL — {len(_errors)} расхождений:")
    for x in _errors:
        print(f"  • {x}")
    sys.exit(1)
print("Все проверки пройдены.")
