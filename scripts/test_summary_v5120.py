# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""v5.1.20: сводка сегмента понятнее (решения владельца 14.10.2026).

- длительности как в UI: «18 ч 00 мин», «6 мин 15 с», «40 с» (было «18ч 11с»);
- таблица «Ключевые показатели», вариант А: период в заголовке, «Обычно /
  Минимум / Максимум», русские названия и единицы, числа без лишних нулей;
- неподтверждённое итоговым расчётом — вне «воздействия», отдельной оговоркой;
- «активен на конец» — до оговорки о неподтверждённых (сводка и вход модели);
- пик риска нагара внутри периода.

Запуск:  py -3 scripts/test_summary_v5120.py
"""
from __future__ import annotations

import ast
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analytics.contract import (  # noqa: E402
    CokingRisk, Detection, DerivedMetrics, RiskAccumulators, Segment, Subsegment,
)
from analytics.labels import fmt_duration, fmt_number, number_decimals  # noqa: E402
from analytics.serializer import (  # noqa: E402
    SUMMARY_PARTS_VERSION, _key_metrics_part, build_summary_parts, render_summary_md,
)
from corpus.preprocessor import _fmt_dur as pre_fmt, _remarks_from_summary  # noqa: E402
from online.status_assembler import _fmt_duration as status_fmt  # noqa: E402

_errors: list[str] = []
MSK = ZoneInfo("Europe/Moscow")
T0 = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)


def check(cond: bool, msg: str) -> None:
    if not cond:
        _errors.append(msg)


def det(sc):
    return Detection(scenario=sc, severity="CAUTION", t_detected=T0.isoformat(), source="METRIC_RULE",
                     trigger="t", related_roles=[], fault_codes=[], description_key="k", values={})


def sub(i, dets, chars=None, dq=1.0, coking=None):
    s = T0 + timedelta(hours=i - 1)
    ra = RiskAccumulators()
    if coking is not None:
        ra.coking_risk = coking
    return Subsegment(id=f"s.{i}", parent_segment_id="s", t_start=s.isoformat(),
                      t_end=(s + timedelta(hours=1)).isoformat(), duration_sec=3600,
                      load_zone="NORMAL", cause_open="SEG_START", cause_close=None,
                      characteristics=chars or {}, derived_metrics=DerivedMetrics(),
                      risk_accumulators=ra, detections=dets, data_quality=dq)


def seg(subs, rs=3):
    return Segment(id="s", router_sn="R", equip_type="pcc", panel_id=1, engine_sn="", run_state=rs,
                   run_state_label="Работа", t_start=T0.isoformat(),
                   t_end=(T0 + timedelta(hours=len(subs))).isoformat(), duration_sec=3600 * len(subs),
                   engine_hours_start=None, cause_open="REPORT_START", cause_close=None,
                   preamble_included=False, data_quality=1.0, subsegments=subs,
                   sequence_checks=[], events=[])


def ep(sc, a, b, **kw):
    d = {"source": "analytics", "scenario": sc, "severity": "CAUTION", "gate_suppressed": False,
         "t_open": T0 + timedelta(minutes=a), "t_close": (T0 + timedelta(minutes=b)) if b is not None else None,
         "active_sec": ((b if b is not None else 120) - a) * 60.0, "blind_sec": 0.0}
    d.update(kw)
    return d


# ── 1. Длительности ─────────────────────────────────────────────────────────
for sec, want in ((0, "0 с"), (40, "40 с"), (40.9, "40 с"), (375, "6 мин 15 с"), (480, "8 мин"),
                  (3600, "1 ч 00 мин"), (64811, "18 ч 00 мин"), (86400, "24 ч 00 мин"),
                  (6719, "1 ч 51 мин"), (12_000_000, "3333 ч 20 мин"), (None, "0 с"), (-5, "0 с")):
    check(fmt_duration(sec) == want, f"1: {sec} → {fmt_duration(sec)!r}, ждали {want!r}")
check(fmt_duration(375, seconds=False) == "6 мин" and fmt_duration(40, seconds=False) == "40 с",
      "1б: без секунд — для строки статуса")
check(status_fmt(58080) == "16 ч 08 мин" and status_fmt(375) == "6 мин",
      f"1в: строка статуса: {status_fmt(58080)!r}, {status_fmt(375)!r}")
check(pre_fmt(3900) == "1 ч 05 мин" and pre_fmt(300) == "5 мин" and pre_fmt(40) == "40 с",
      f"1г: вход модели: {pre_fmt(3900)!r}")
_src = (ROOT / "web" / "routes.py").read_text(encoding="utf-8")
_fn = next(n for n in ast.parse(_src).body if isinstance(n, ast.FunctionDef) and n.name == "_fmt_dur")
_ns: dict = {}
exec(compile(ast.Module(body=[_fn], type_ignores=[]), "routes._fmt_dur", "exec"), _ns)
check(_ns["_fmt_dur"](64811) == "18 ч 00 мин" and _ns["_fmt_dur"](None) == "—"
      and _ns["_fmt_dur"](5, is_open=True) == "активно", "1д: пакет для ИИ (routes)")

# ── 2. Числа таблицы ────────────────────────────────────────────────────────
_d = number_decimals([20.83, 2.38, 49.36])
check([fmt_number(x, _d) for x in (20.83, 2.38, 49.36)] == ["20,8", "2,4", "49,4"],
      "2: одна точность на строку")
_d = number_decimals([0.85, 0.5, 0.9])
check([fmt_number(x, _d) for x in (0.85, 0.5, 0.9)] == ["0,85", "0,5", "0,9"], "2б: малые — два знака")
check(fmt_number(1500, 0) == "1500" and fmt_number(100, 0) == "100", "2г: без дробной части нули целых не режутся")
check(fmt_number(1500.0) == "1500" and fmt_number(None) == "—" and fmt_number(-0.001) == "0"
      and fmt_number(49.9) == "49,9", "2в: без лишних нулей, запятая")

# ── 3. Таблица «Ключевые показатели» (вариант А) ─────────────────────────────
chars = {"OIL_PRESS": {"median": 248.0, "min": 0.0, "max": 316.0, "unit": "kPa"},
         "RPM": {"median": 1498.0, "min": 1487.0, "max": 1515.0, "unit": "rpm"},
         "LOAD_PCT": {"median": 20.83, "min": 2.38, "max": 49.36, "unit": "%"},
         "BOOST_PRESSURE": {"median": 5.5, "min": 0.5, "max": 17.1, "unit": "psi"},
         "XX_NEW": {"median": 1.0, "min": 1.0, "max": 1.0, "unit": "u"}}
s3 = seg([sub(1, [], chars), sub(2, [], chars, dq=0.5)])
km = _key_metrics_part([s3], ["OIL_PRESS", "RPM", "LOAD_PCT", "BOOST_PRESSURE", "XX_NEW"])
check(km["work_raw_sec"] == 7200 and km["work_sec"] == 5400, f"3: время работы без взвешивания: {km}")
# Подсегмент без связи (dq 0) — тоже работа (ревью v5.1.20)
km0 = _key_metrics_part([seg([sub(1, [], chars), sub(2, [], chars, dq=0.0)])], ["OIL_PRESS"])
check(km0["work_raw_sec"] == 7200 and km0["work_sec"] == 3600, f"3а: dq 0 в заголовке: {km0}")
check([r["name"] for r in km["rows"]][:2] == ["Давление масла", "Обороты двигателя"], "3б: русские имена в частях")
md3 = render_summary_md(build_summary_parts([s3], [], tz=MSK,
                                            trip_roles=["OIL_PRESS", "RPM", "LOAD_PCT", "BOOST_PRESSURE", "XX_NEW"]))
check("### Ключевые показатели за 2 ч 00 мин работы" in md3, f"3в: заголовок с периодом:\n{md3}")
check("| Параметр | Обычно | Минимум | Максимум | Ед. |" in md3 and "Медиана" not in md3
      and "медиана — по времени" not in md3, f"3г: колонки и без строки-пояснения:\n{md3}")
check("| Давление масла | 248 | 0 | 316 | кПа |" in md3 and "| Обороты двигателя | 1498 | 1487 | 1515 | об/мин |" in md3
      and "| Нагрузка | 20,8 | 2,4 | 49,4 | % |" in md3 and "| Давление наддува | 5,5 | 0,5 | 17,1 | psi |" in md3
      and "| XX_NEW | 1 | 1 | 1 | u |" in md3, f"3д: строки таблицы:\n{md3}")
# Старые части (до v5.1.20): без имени и work_raw_sec — перепечатка поздней отменой гейтом
old_km = {"rows": [{"role": "OIL_TEMP", "median": 97.0, "min": 42.0, "max": 100.0, "unit": "°C"}],
          "basis": "period", "work_sec": 64811.0, "subsegments": 14}
md3b = render_summary_md({"version": 2, "verdict": {"title": "x"}, "key_metrics": old_km})
check("### Ключевые показатели за 18 ч 00 мин работы" in md3b and "| Температура масла | 97 | 42 | 100 | °C |" in md3b,
      f"3е: старые части:\n{md3b}")
md3c = render_summary_md({"version": 1, "verdict": {"title": "x"},
                          "key_metrics": {"rows": old_km["rows"]}})
check("### Ключевые показатели\n" in md3c and "за " not in md3c.split("### Ключевые показатели")[1].split("\n")[0],
      f"3ж: без периода — без «за …»:\n{md3c}")

# ── 4. Неподтверждённое — вне воздействия ────────────────────────────────────
check(SUMMARY_PARTS_VERSION == 3, "4: версия частей 3")
# Сценарий есть только в подсегменте 1 (0–60 мин): эпизод 10–18 подтверждён, 70–76 — нет
s4 = seg([sub(1, [det("COOLING_FAILURE")]), sub(2, [])])
p4 = build_summary_parts([s4], [ep("COOLING_FAILURE", 10, 18), ep("COOLING_FAILURE", 70, 76)],
                         tz=MSK, t_from=T0, t_to=T0 + timedelta(hours=2))
g4 = next(x for x in p4["remarks"] if x["scenario"] == "COOLING_FAILURE")
check(g4["period_sec"] == 480 and g4["unconfirmed_sec"] == 360 and g4["onsets"] == 1,
      f"4б: воздействие — только подтверждённое: {g4}")
md4 = render_summary_md(p4)
check("1 эпизод, воздействие за период 8 мин; ещё 1 по живым данным (13.10 16:10–16:16), за период 6 мин"
      " — *в итоговом расчёте не подтверждено*" in md4, f"4в: смешанная группа:\n{md4}")
check("ЗАМЕЧАНИЯ К РАБОТЕ — 1" in md4, f"4г: вердикт не изменился:\n{md4}")
# Только неподтверждённое — без «воздействия» (и не ветка v1 с active_sec)
p4b = build_summary_parts([s4], [ep("COOLING_FAILURE", 70, 76)], tz=MSK,
                          t_from=T0, t_to=T0 + timedelta(hours=2))
g4b = p4b["remarks"][0]
md4b = render_summary_md(p4b)
check(g4b["period_sec"] == 0 and g4b["unconfirmed_sec"] == 360
      and "1 эпизод по живым данным (13.10 16:10–16:16), за период 6 мин — *в итоговом расчёте не подтверждено*" in md4b
      and "воздействие" not in md4b, f"4д: только неподтверждённое:\n{md4b}")
# Неподтверждённое внутри подтверждённого: своё время, а не «0 с» (ревью v5.1.20)
s4c = seg([sub(1, [det("COOLING_FAILURE")]), sub(2, [])])
p4c = build_summary_parts([s4c], [ep("COOLING_FAILURE", 30, 110), ep("COOLING_FAILURE", 70, 80)],
                          tz=MSK, t_from=T0, t_to=T0 + timedelta(hours=2))
g4c = p4c["remarks"][0]
md4c = render_summary_md(p4c)
check(g4c["period_sec"] == 4800 and g4c["unconfirmed_sec"] == 600
      and "ещё 1 по живым данным (13.10 16:10–16:20), за период 10 мин" in md4c and "0 с" not in md4c,
      f"4е: вложенный неподтверждённый:\n{md4c}")
# Начался до периода: интервал — целиком, время — за период
p4d = build_summary_parts([seg([sub(1, []), sub(2, [det("COOLING_FAILURE")])])], [ep("COOLING_FAILURE", -3000, 30)], tz=MSK,
                          t_from=T0, t_to=T0 + timedelta(hours=2))
md4d = render_summary_md(p4d)
check("1 эпизод по живым данным (11.10 13:00–13.10 15:30), за период 30 мин" in md4d,
      f"4ж: начался до периода:\n{md4d}")

# ── 5. «Активен на конец» — до оговорки ──────────────────────────────────────
s5 = seg([sub(1, []), sub(2, [det("OIL_DILUTION")])])
p5 = build_summary_parts([s5], [ep("OIL_DILUTION", 5, 20), ep("OIL_DILUTION", 70, None)],
                         tz=MSK, t_from=T0, t_to=T0 + timedelta(hours=2))
md5 = render_summary_md(p5)
check("— **активен на конец периода**; ещё 1 по живым данным (13.10 15:05–15:20), за период 15 мин" in md5,
      f"5: порядок в сводке:\n{md5}")
tag5 = _remarks_from_summary({"report_summary_json": p5}) or ""
check(tag5.index("активно на конец") < tag5.index("ещё 1 из 2"), f"5б: порядок во входе модели: {tag5}")
# Старые части (v2): прежний текст, но «на конец» тоже раньше оговорки
old_g = {"name": "Пониженное давление масла", "source": "analytics", "scenario": "OIL_DILUTION",
         "severity": "CAUTION", "count": 2, "active_sec": 8098.0, "blind_sec": 0.0, "suppressed": 0,
         "unconfirmed": 1, "unconfirmed_at": ["13.10 14:59–15:22"], "open": 1, "open_live": 1,
         "at_end": True, "period_sec": 8098.0, "period_blind_sec": 0.0, "onsets": 2}
md5b = render_summary_md({"version": 2, "verdict": {"title": "x"}, "has_remarks": True, "remarks": [old_g]})
check("2 эпизода, воздействие за период 2 ч 14 мин — **активен на конец периода** — "
      "*в итоговом расчёте не подтверждено: 1 из 2 (13.10 14:59–15:22)*" in md5b, f"5в: части v2:\n{md5b}")

# ── 6. Пик риска нагара ──────────────────────────────────────────────────────
def ck(level, debt, added, paid):
    return CokingRisk(risk_level=level, debt_sec=debt, debt_added_total_sec=added, debt_paid_total_sec=paid,
                      low_load_zone_sec=debt)


s6 = seg([sub(1, [], coking=ck("YELLOW", 9000, 9000, 0)), sub(2, [], coking=ck("RED", 58140, 58140, 0)),
          sub(3, [], coking=ck("GREEN", 4500, 58140, 53640))])
s6.coking_start = ck("GREEN", 0, 0, 0).to_dict()
p6 = build_summary_parts([s6], [], tz=MSK, trip_roles=[])
c6 = p6["coking"]
check(c6["level_peak"] == "RED" and c6["debt_peak_sec"] == 58140 and c6["debt_peak_text"] == "13.10 17:00",
      f"6: пик по подсегментам: {c6.get('level_peak')} {c6.get('debt_peak_sec')} {c6.get('debt_peak_text')}")
md6 = render_summary_md(p6)
check("Риск нагара: на начало **низкий** → поднимался до **высокого** (долг до ~16 ч 09 мин, 13.10 17:00)"
      " → на конец **низкий** — прожигом погашено ~14 ч 54 мин долга" in md6, f"6б: строка нагара:\n{md6}")
# Пик не выше концов — без вставки
s6b = seg([sub(1, [], coking=ck("YELLOW", 9000, 9000, 0)), sub(2, [], coking=ck("YELLOW", 9500, 9500, 0))])
s6b.coking_start = ck("YELLOW", 8000, 8000, 0).to_dict()
md6b = render_summary_md(build_summary_parts([s6b], [], tz=MSK, trip_roles=[]))
check("поднимался" not in md6b, f"6в: пик не выше концов:\n{md6b}")

if _errors:
    print(f"FAIL — {len(_errors)} расхождений:")
    for x in _errors:
        print(f"  • {x}")
    sys.exit(1)
print("Все проверки пройдены.")
