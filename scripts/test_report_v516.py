# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""Дефекты отчёта (v5.1.6): время в режиме, виды, русские имена, свёртка, коды
панели, «Общая авария + код» по периодам, наработка в часах, показатели за сегмент,
воздействие за период, недоскорость, профиль останова, вердикт консоли.

Запуск:  py -3 scripts/test_report_v516.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analytics import binding  # noqa: E402
from analytics.contract import (  # noqa: E402
    CokingRisk, Detection, DerivedMetrics, RiskAccumulators, Segment, Subsegment,
)
from analytics.detectors import _detect_controller_faults, confirm_panel_codes  # noqa: E402
from analytics.segmenter import _apply_scale, _check_stop_profile  # noqa: E402
from analytics.serializer import (  # noqa: E402
    _check_details, _fmt_dq, _key_metrics_part, build_summary_parts, render_summary_md, to_markdown,
)
from online.status_assembler import compute_analytics_hash  # noqa: E402
from web.segment_view import segment_verdict  # noqa: E402

_errors: list[str] = []
MSK = ZoneInfo("Europe/Moscow")


def check(cond: bool, msg: str) -> None:
    if not cond:
        _errors.append(msg)


cfg = binding.build_config(ROOT / "knowledge_base", controller_id="pcc3300", engine_id="cummins_kta50")
T0 = datetime(2026, 10, 10, 6, 0, tzinfo=timezone.utc)


def det(sc, sev="CAUTION", trig="t", codes=None, values=None, t=None, dk="k"):
    return Detection(scenario=sc, severity=sev, t_detected=(t or T0).isoformat(), source="METRIC_RULE",
                     trigger=trig, related_roles=[], fault_codes=list(codes or []), description_key=dk,
                     values=dict(values or {}))


def sub(i, n, dets, chars=None, dq=1.0, hours=8):
    s = T0 + timedelta(hours=hours * (i - 1))
    return Subsegment(id=f"s.{i}", parent_segment_id="s", t_start=s.isoformat(),
                      t_end=(s + timedelta(hours=hours)).isoformat(), duration_sec=hours * 3600,
                      load_zone="LOW", cause_open="SEG_START", cause_close=None,
                      characteristics=chars or {}, derived_metrics=DerivedMetrics(),
                      risk_accumulators=RiskAccumulators(), detections=dets, data_quality=dq)


def seg_of(subs, rs=3, cause_open="REPORT_START", checks=None):
    return Segment(id="s", router_sn="R", equip_type="pcc", panel_id=1, engine_sn="", run_state=rs,
                   run_state_label="Работа", t_start=T0.isoformat(),
                   t_end=(T0 + timedelta(hours=24)).isoformat(), duration_sec=86400,
                   engine_hours_start=15226.0, cause_open=cause_open, cause_close=None,
                   preamble_included=False, data_quality=0.997, subsegments=subs,
                   sequence_checks=checks or [], events=[])


lfc = {"LAST_FAULT_CODE": {"kind": "state", "median": 1453, "min": 1453, "max": 1453,
                           "value_end": 1453, "unit": ""}}
oil = lambda: det("OIL_DILUTION", trig="oil_press_min=248кПа < паспортного минимума 310кПа")  # noqa: E731
s1 = seg_of([sub(1, 3, [oil()], dict(lfc)), sub(2, 3, [oil(), det("LIMIT_PROXIMITY", trig="Температура масла: 100")],
                                                  dict(lfc)), sub(3, 3, [oil()], dict(lfc))])
md = to_markdown([s1], "R", "pcc", 1, T0, T0 + timedelta(hours=24), "5.1.6", tz=MSK,
                 inherited_run_state_sec={3: 3600.0})

# 1. Время в режиме: за период и с начала состояния
check("| Время под нагрузкой (RUN_STATE=3) за период | 24ч |" in md
      and "с начала состояния (с 10.10 08:00) | 25ч |" in md, f"1: время в режиме:\n{md[:900]}")
# 2. Виды и срабатывания, без уровня и вердикта
check("| Обнаружения детекторов | 2 вида, 4 срабатывания по подсегментам |" in md
      and "Максимальный уровень" not in md and "ПРЕДУПРЕЖДЕНИЕ АНАЛИТИКИ" not in md, "2: подсчёт по видам")
# 3. Русские имена и свёртка
check("**Пониженное давление масла** (CAUTION) — во всех 3 подсегментах" in md and "OIL_DILUTION" not in md,
      "3: русское имя, свёртка")
check("в подсегменте 1.2" in md, "3б: одиночное вхождение — номер подсегмента")
# 4. Состояния — только при смене
check(md.count("| LAST_FAULT_CODE") == 1 and "| 1453 | 1453 | 1453 |" in md, f"4: код один раз и целым: {md.count('| LAST_FAULT_CODE')}")
# 5. Качество с одним знаком, наработка в часах
check(_fmt_dq(0.997) == "99.7%" and _fmt_dq(1.0) == "100%" and "Наработка: 15226 ч" in md, "5: качество/наработка")
# 6. Окно связи — в местном времени
txt = _check_details({"check": "subseg_data_coverage", "ranges": [
    {"start": "2026-10-10T19:20:16+00:00", "end": "2026-10-10T21:31:50+00:00"}]}, MSK)
check(txt == "отсутствие связи/данных: 10.10 22:20:16 – 11.10 00:31:50", f"6: {txt}")

# 7. Коды панели: аналитике — только выставленные; хеш состава тот же
d = det("OIL_DILUTION", codes=[143, 415])
confirm_panel_codes([d], set())
check(d.fault_codes == [] and d.ref_codes == [143, 415], "7: код не выставлен — не ссылаемся")
check(compute_analytics_hash([d.to_dict()]) == compute_analytics_hash([{"scenario": "OIL_DILUTION", "fault_codes": [143, 415]}]),
      "7б: хеш состава гейта не изменился")
d2 = det("LIMIT_PROXIMITY", codes=[151])
confirm_panel_codes([d2], {151})
check(d2.fault_codes == [151], "7в: выставленный код остаётся")

# 8. «Общая авария + код» — каждый период аварии, код за период
ca = lambda s, e: {"addr": 40408, "bit": 11, "fault_start": s, "fault_end": e, "severity": None}  # noqa: E731
p1 = (T0 + timedelta(hours=1), T0 + timedelta(hours=2))
p2 = (T0 + timedelta(hours=3), T0 + timedelta(hours=5))
lfc_p = [{"addr": 40012, "value": 1451, "state_start": p1[0], "state_end": p1[1] + timedelta(seconds=4)},
         {"addr": 40012, "value": 0, "state_start": p1[1] + timedelta(seconds=4), "state_end": p2[0]},
         {"addr": 40012, "value": 1451, "state_start": p2[0], "state_end": p2[1]},
         {"addr": 40012, "value": 0, "state_start": p2[1], "state_end": None}]
res = _detect_controller_faults([ca(*p1), ca(*p2)], {}, T0, T0 + timedelta(hours=8), cfg, {40012: lfc_p})
check(len(res) == 2 and all(r.fault_codes == [1451] for r in res), f"8: два периода аварии — две CAUTION: {res}")
loud = {"addr": 40405, "bit": 1, "fault_start": p2[0], "fault_end": p2[1], "severity": "warning",
        "fault_name": "Горячий останов"}
res = _detect_controller_faults([ca(*p1), ca(*p2), loud], {}, T0, T0 + timedelta(hours=8), cfg, {40012: lfc_p})
check(sum(1 for r in res if r.severity == "CAUTION") == 1, "8б: во время громкого бита CAUTION не нужен")

# 9. Наработка: секунды → часы, строки копируются
by = {40070: [{"ts": T0, "value": 54812520.0}]}
orig = by[40070][0]
out = _apply_scale(by, cfg)
check(abs(out[40070][0]["value"] - 15225.7) < 0.1 and orig["value"] == 54812520.0, "9: с → ч без мутации кэша")

# 10. Показатели за весь период — медиана по времени
km = _key_metrics_part([NS(run_state=3, subsegments=[
    NS(duration_sec=3600 * 20, data_quality=1.0, characteristics={"LOAD_PCT": {"median": 20, "min": 15, "max": 25}}),
    NS(duration_sec=3600 * 4, data_quality=1.0, characteristics={"LOAD_PCT": {"median": 50, "min": 45, "max": 55}})])],
    ["LOAD_PCT"])
check(km["rows"][0]["median"] == 20 and km["rows"][0]["min"] == 15 and km["rows"][0]["max"] == 55,
      f"10: показатели за период: {km}")

# 11. Воздействие за период и с начала; дубли фронта — один эпизод
ep = {"source": "analytics", "scenario": "OIL_DILUTION", "severity": "CAUTION", "gate_suppressed": False,
      "t_open": T0 - timedelta(hours=18), "t_close": None, "active_sec": 42 * 3600, "blind_sec": 0}
dup = dict(ep, t_close=T0 - timedelta(hours=2), active_sec=16 * 3600)
p = build_summary_parts([s1], [ep, dup], tz=MSK, t_from=T0, t_to=T0 + timedelta(hours=24))
g = p["remarks"][0]
check(abs(g["period_sec"] - 86400) < 1 and g["since"] and g["onsets"] == 1 and g["count"] == 1,
      f"11: за период 24 ч, с начала, один фронт: {g}")
md2 = render_summary_md(p)
check("воздействие за период 24ч (с начала 42ч" in md2 and "ЗАМЕЧАНИЯ К РАБОТЕ — 1" in md2, f"11б:\n{md2}")

# 12. «Активен на конец» — по at_end, снятое ИИ не висит
ep_s = dict(ep, gate_suppressed=True)
md3 = render_summary_md(build_summary_parts([s1], [ep_s], tz=MSK, t_from=T0, t_to=T0 + timedelta(hours=24)))
check("снято ИИ" in md3 and "активен на конец" not in md3, f"12:\n{md3}")

# 13. Недоскорость: «на 12 ниже порога (размах 37)»
from analytics.detectors import _detect_rpm_underspeed  # noqa: E402
rd = _detect_rpm_underspeed({"RPM": {"min": 1473, "max": 1510, "median": 1497}}, DerivedMetrics(), T0, cfg)
check(rd and "на 12 ниже порога 1485" in rd[0].trigger and "размах 37" in rd[0].trigger
      and rd[0].severity == "CAUTION", f"13: {rd and rd[0].trigger}")


# 14. Профиль останова: только при смене режима; не судить раньше норматива
def stop(cause_open, rows, dur=600, cause_close=None):
    s = Segment(id="x", router_sn="R", equip_type="pcc", panel_id=1, engine_sn="", run_state=0,
                run_state_label="Стоп", t_start=T0.isoformat(), t_end=(T0 + timedelta(seconds=dur)).isoformat(),
                duration_sec=dur, engine_hours_start=None, cause_open=cause_open, cause_close=cause_close,
                preamble_included=False, data_quality=1.0, subsegments=[sub(1, 1, [])], sequence_checks=[],
                events=[])
    by_ = {cfg.role_to_addr("RPM"): [{"ts": T0 + timedelta(seconds=t), "value": v} for t, v in rows]}
    _check_stop_profile(s, by_, cfg)
    return s


def _not_fell(s):
    """«Обороты не упали» — наблюдение без детекции (v5.1.7: только панель)."""
    return (not s.subsegments[0].detections
            and any(c["check"] == "rpm_stop_profile" and not c["passed"] for c in s.sequence_checks))


s_ = stop("REPORT_START", [(10, 0), (40, 0)])
check(not s_.sequence_checks, "14: суточный рез — проверки профиля нет")
s_ = stop("RUN_STATE_CHANGE", [(-5, 1500), (10, 709), (20, 545)], dur=20)
check(not s_.sequence_checks, "14б: окно 20 с < норматива — «не упали» нет")
s_ = stop("RUN_STATE_CHANGE", [(-5, 1500), (10, 900), (40, 800), (60, 700)], dur=70)
check(not s_.sequence_checks, "14в: 60 с < горизонта 90 с — медленный останов ещё не «не упали»")
s_ = stop("RUN_STATE_CHANGE", [(-5, 1500), (10, 900), (40, 800), (95, 800)], dur=120)
check(_not_fell(s_), "14г: не упали за 95 с — наблюдение без детекции")
s_ = stop("RUN_STATE_CHANGE", [(-5, 1500), (10, 900), (50, 800), (95, 800)], dur=20, cause_close="FAULT_CLEARED")
check(_not_fell(s_), "14д: голова 20 с, обрезанная снятием аварии, — судим по ряду после реза")
s_ = stop("RUN_STATE_CHANGE", [(-5, 1500), (10, 900), (50, 800), (95, 800)], dur=20, cause_close="RUN_STATE_CHANGE")
check(not s_.sequence_checks, "14е: голова до нового пуска — чужой ряд не берём")

# 14ж. Свёртка: худшее значение не теряется
from analytics.serializer import _det_worst  # noqa: E402
_ents = [(i, {"scenario": "RPM_UNDERSPEED", "trigger": f"t{i}", "values": {"rpm_min": v}})
         for i, v in enumerate([1480, 1460, 1475, 1478, 1482])]
w = _det_worst(_ents)
check(w and w[0] == 1, f"14ж: худшее — минимум оборотов: {w}")
from analytics.serializer import _append_detections  # noqa: E402
_sg = NS(subsegments=[NS(detections=[dict(d, severity="CAUTION", source="METRIC_RULE")]) for _, d in _ents])
_ln: list[str] = []
_append_detections(_ln, _sg, 1, lambda x: str(x))
_txt = "\n".join(_ln)
check("худшее (1.2): t1" in _txt and "последнее (1.5): t4" in _txt and "первое (1.1): t0" in _txt,
      f"14ж2: свёртка — первое, худшее, последнее:\n{_txt}")

# 14з. Код панели на конце подсегмента — относится к нему
from analytics.detectors import panel_codes_in  # noqa: E402
_pc = panel_codes_in([{"value": 151, "state_start": T0 + timedelta(seconds=60), "state_end": None}],
                     T0, T0 + timedelta(seconds=60))
check(_pc == {151}, f"14з: граница включительно: {_pc}")

# 14и. Цепочка «с пуска»: смена режима на границе — без ссылки
from online.engine import _chain_link  # noqa: E402
check(_chain_link(0, 7, NS(run_state=1), NS(run_state=0)) is None
      and _chain_link(0, 7, NS(run_state=0), NS(run_state=0)) == 7
      and _chain_link(1, 7, None, NS(run_state=0)) is None, "14и: ссылка только голове в том же режиме")

# 15. Консоль: вердикт по частям
pp = build_summary_parts([s1], [ep_s], tz=MSK, t_from=T0, t_to=T0 + timedelta(hours=24))
v = segment_verdict({"report_summary_json": pp}, {})
check(v["level"] == "НОРМА" and v["gate_checked"], f"15: баннер = сводка: {v}")

if _errors:
    print(f"FAIL — {len(_errors)} расхождений:")
    for x in _errors:
        print(f"  • {x}")
    sys.exit(1)
print("Все проверки пройдены.")
