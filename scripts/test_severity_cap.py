# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""Догма уровней (v5.1.7): WARNING и SHUTDOWN выставляет только панель, аналитика
не выше CAUTION; «двигатель не остановился» — наблюдение, аварию несёт панель.

Запуск:  py -3 scripts/test_severity_cap.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analytics import binding  # noqa: E402
from analytics.contract import (  # noqa: E402
    Detection, DerivedMetrics, RiskAccumulators, Segment, Subsegment, cap_severity,
)
from analytics.segmenter import _check_cooldown_transition, _sequence_checks  # noqa: E402
from analytics.serializer import build_summary_parts, render_summary_md  # noqa: E402
from corpus.preprocessor import _extract_detections, _extract_verdict  # noqa: E402
from online.engine import _max_severity  # noqa: E402
from online.status_assembler import build_warning_prompt  # noqa: E402

_errors: list[str] = []
MSK = ZoneInfo("Europe/Moscow")
T0 = datetime(2026, 10, 10, 6, 0, tzinfo=timezone.utc)


def check(cond: bool, msg: str) -> None:
    if not cond:
        _errors.append(msg)


def sub(i, dets, zone="LOW", hours=1):
    s = T0 + timedelta(hours=hours * (i - 1))
    return Subsegment(id=f"s.{i}", parent_segment_id="s", t_start=s.isoformat(),
                      t_end=(s + timedelta(hours=hours)).isoformat(), duration_sec=hours * 3600,
                      load_zone=zone, cause_open="SEG_START", cause_close=None,
                      characteristics={}, derived_metrics=DerivedMetrics(),
                      risk_accumulators=RiskAccumulators(), detections=dets, data_quality=1.0)


def seg(subs, rs, checks=None, t0=T0, hours=1):
    return Segment(id="s", router_sn="R", equip_type="pcc", panel_id=1, engine_sn="", run_state=rs,
                   run_state_label="?", t_start=t0.isoformat(),
                   t_end=(t0 + timedelta(hours=hours)).isoformat(), duration_sec=hours * 3600,
                   engine_hours_start=None, cause_open="RUN_STATE_CHANGE", cause_close=None,
                   preamble_included=False, data_quality=1.0, subsegments=subs,
                   sequence_checks=checks or [], events=[])


def det(sc, sev):
    return Detection(scenario=sc, severity=sev, t_detected=T0.isoformat(), source="METRIC_RULE",
                     trigger="t", related_roles=[], fault_codes=[], description_key="k", values={})


# 1. Потолок: аналитика ≤ CAUTION, панель — как есть
for sev in ("WARNING", "SHUTDOWN", "ALARM"):
    check(cap_severity("STOP_PROFILE", sev) == "CAUTION", f"1: аналитика {sev} → CAUTION")
check(cap_severity("OIL_DILUTION", "CAUTION") == "CAUTION" and cap_severity("LOAD_STEP", "INFO") == "INFO",
      "1б: CAUTION/INFO не трогаем")
check(cap_severity("CONTROLLER_FAULT", "SHUTDOWN") == "SHUTDOWN"
      and cap_severity("CONTROLLER_FAULT", "WARNING") == "WARNING", "1в: панель — как есть")

# 2. Детекция не может выйти выше CAUTION, какой бы уровень ей ни дали
check(det("LIMIT_PROXIMITY", "WARNING").severity == "CAUTION", "2: Detection аналитики WARNING → CAUTION")
check(det("CONTROLLER_FAULT", "SHUTDOWN").severity == "SHUTDOWN", "2б: Detection панели SHUTDOWN остаётся")

# 3. Действующая KB всех трёх связок: у аналитики уровень ≤ CAUTION
_SC = ("LOAD_STEP", "NEGATIVE_SEQUENCE", "PHASE_CURRENT_SPREAD", "NEUTRAL_CURRENT", "COOLING_FAILURE",
       "OIL_DILUTION", "COKING_RISK", "IDLE_TOO_LONG", "NO_LOAD_TOO_LONG", "WARMUP_VIOLATION",
       "COOLDOWN_VIOLATION", "START_FAILURE", "THERMAL_HIGHLOAD", "LIMIT_PROXIMITY", "STOP_PROFILE",
       "RPM_UNDERSPEED")
for eng in ("cummins_kta50", "cummins_qsx15"):
    cfg_e = binding.build_config(ROOT / "knowledge_base", controller_id="pcc3300", engine_id=eng)
    bad = {sc: cfg_e.det(sc, "severity_default") for sc in _SC
           if cfg_e.det(sc, "severity_default") not in (None, "INFO", "CAUTION")}
    check(not bad, f"3: {eng}: уровень аналитики выше CAUTION в KB: {bad}")
    check(cfg_e.det("STOP_PROFILE", "severity_default") is None, f"3б: {eng}: у профиля останова уровня нет")
cfg = binding.build_config(ROOT / "knowledge_base", controller_id="pcc3300", engine_id="cummins_kta50")

# 4. KB с WARNING у аналитики — понижение при загрузке, а не отказ запуска
cfg.detectors.setdefault("LIMIT_PROXIMITY", {})["severity_default"] = "WARNING"
cfg.detectors.setdefault("CONTROLLER_FAULT", {})["severity_default"] = "WARNING"
cfg._validate()
check(cfg.det("LIMIT_PROXIMITY", "severity_default") == "CAUTION", "4: KB WARNING у аналитики → CAUTION")
check(cfg.det("CONTROLLER_FAULT", "severity_default") == "WARNING", "4б: панельный ключ не трогаем")
cfg = binding.build_config(ROOT / "knowledge_base", controller_id="pcc3300", engine_id="cummins_kta50")

# 5. Останов без охлаждения после высокой нагрузки — CAUTION (был зашит WARNING)
run = seg([sub(1, [], zone="ELEVATED")], 3)
stp = seg([sub(1, [])], 0, t0=T0 + timedelta(hours=1))
_check_cooldown_transition(stp, [run], cfg, Detection)
cd = [d for d in stp.subsegments[0].detections if d.scenario == "COOLDOWN_VIOLATION"]
check(cd and cd[0].severity == "CAUTION" and "заклинивания" in cd[0].trigger,
      f"5: охлаждение после ELEVATED — CAUTION, риск в тексте: {cd and (cd[0].severity, cd[0].trigger)}")

# 6. Старый эпизод «не остановился» WARNING — в сводке 🟡, не 🟠
ep = {"source": "analytics", "scenario": "STOP_PROFILE", "severity": "WARNING", "gate_suppressed": False,
      "t_open": T0, "t_close": T0 + timedelta(seconds=40), "active_sec": 40, "blind_sec": 0}
p = build_summary_parts([seg([sub(1, [])], 0)], [ep], tz=MSK, t_from=T0, t_to=T0 + timedelta(hours=1))
md = render_summary_md(p)
check(p["remarks"] and p["remarks"][0]["severity"] == "CAUTION" and "🟠" not in md,
      f"6: старый WARNING аналитики читается CAUTION:\n{md}")

# 7. «Обороты не упали» и активная аварийная маска — наблюдения, на вердикт не влияют
obs = {"check": "rpm_stop_profile", "passed": False,
       "details": "обороты не упали ниже 50 об/мин за 95 с после останова"}
p = build_summary_parts([seg([sub(1, [])], 0, checks=[obs])], [], tz=MSK,
                        t_from=T0, t_to=T0 + timedelta(hours=1))
md = render_summary_md(p)
check(p["verdict"]["level"] == "НОРМА" and "ℹ️ обороты не упали" in md,
      f"7: наблюдение — НОРМА и строка ℹ️: {p['verdict']}\n{md}")
fp = [{"severity": "shutdown", "fault_start": T0 + timedelta(minutes=10), "fault_end": None,
       "fault_name_ru": "Остановка двигателя"}]
ch = [c for c in _sequence_checks(0, [sub(1, [])], fp, cfg) if c["check"] == "no_active_shutdown_fault"]
check(ch and ch[0]["passed"] is False and "Остановка двигателя" in ch[0]["details"],
      f"7б: аварийная маска на конце сегмента видна (раньше сравнение регистра не срабатывало): {ch}")
fp2 = [dict(fp[0], fault_end=T0 + timedelta(minutes=20))]
ch2 = [c for c in _sequence_checks(0, [sub(1, [])], fp2, cfg) if c["check"] == "no_active_shutdown_fault"]
check(ch2 and ch2[0]["passed"] is True, f"7в: снятая до конца сегмента — не активна: {ch2}")
fp3 = [dict(fp[0], fault_end=T0 + timedelta(hours=1))]
ch3 = [c for c in _sequence_checks(0, [sub(1, [])], fp3, cfg) if c["check"] == "no_active_shutdown_fault"]
check(ch3 and ch3[0]["passed"] is True, f"7г: снятая ровно на конце (рез SHUTDOWN_CLEARED) — не активна: {ch3}")

# 7д. Маска на конце сегмента — только в данных: в сводке и отчёте её уже несут
# панельное замечание «активен на конец» и блок «⚠ Активные неисправности»
from analytics.serializer import to_markdown  # noqa: E402
nasf = {"check": "no_active_shutdown_fault", "passed": False,
        "details": "на конце сегмента активна аварийная маска панели: Остановка двигателя"}
sg7 = seg([sub(1, [])], 0, checks=[nasf])
p = build_summary_parts([sg7], [], tz=MSK, t_from=T0, t_to=T0 + timedelta(hours=1))
md7 = to_markdown([sg7], "R", "pcc", 1, T0, T0 + timedelta(hours=1), "5.1.7", tz=MSK)
check(not p["failed_checks"] and "аварийная маска" not in md7,
      f"7д: маска на конце не дублируется: {p['failed_checks']}")

# 8. Корпус: старый сегмент со «не остановился» WARNING — не «предупреждение панели»
chars = {"subsegments": [{"detections": [det("STOP_PROFILE", "CAUTION").to_dict() | {"severity": "WARNING"}]}]}
v = _extract_verdict(_extract_detections(json.dumps(chars)))
check(v == ("требует внимания", "CAUTION"), f"8: запасной вердикт корпуса: {v}")
chars_p = {"subsegments": [{"detections": [det("CONTROLLER_FAULT", "WARNING").to_dict()]}]}
check(_extract_verdict(_extract_detections(chars_p))[1] == "WARNING", "8б: панельный WARNING — как есть")

# 9. Эпизод не протаскивает старый WARNING аналитики; панель — как есть
check(_max_severity("WARNING", "CAUTION", "STOP_PROFILE") == "CAUTION", "9: эпизод аналитики ≤ CAUTION")
check(_max_severity("WARNING", "CAUTION", "CONTROLLER_FAULT") == "WARNING", "9б: эпизод панели — максимум")

# 10. Промпт гейта: старый эпизод — [CAUTION]
pr = build_warning_prompt({"analytics_alarms": [], "panel_alarms": [], "mode_label": "Стоп", "run_state": 0,
                           "load_pct": None, "severity_level": "норма", "panel_severity": "норма",
                           "analytics_severity": "норма"},
                          episode_timeline=[dict(ep, t_close=None)])
check("[CAUTION] STOP_PROFILE" in pr and "[WARNING]" not in pr, "10: хронология гейта — [CAUTION]")

# 11. Консоль: уровень старого заключения ИИ — WARNING только при панели
from web.segment_view import _cap_ai_level  # noqa: E402
_an = {"subsegments": [{"detections": [{"scenario": "RPM_UNDERSPEED", "severity": "WARNING"}]}]}
_pn = {"subsegments": [{"detections": [{"scenario": "CONTROLLER_FAULT", "severity": "WARNING"}]}]}
check(_cap_ai_level("WARNING", json.dumps(_an)) == "CAUTION", "11: заключение WARNING без панели → CAUTION")
check(_cap_ai_level("WARNING", _pn) == "WARNING" and _cap_ai_level("ALARM", _pn) == "WARNING",
      "11б: с панельным WARNING — WARNING (легаси ALARM тоже)")
check(_cap_ai_level("SHUTDOWN", {"stop_kind": "EMERGENCY"}) == "SHUTDOWN"
      and _cap_ai_level("CAUTION", _an) == "CAUTION" and _cap_ai_level("НОРМА", _an) == "НОРМА",
      "11в: аварийный стоп и нижние уровни — как есть")

if _errors:
    print(f"FAIL — {len(_errors)} расхождений:")
    for x in _errors:
        print(f"  • {x}")
    sys.exit(1)
print("Все проверки пройдены.")
