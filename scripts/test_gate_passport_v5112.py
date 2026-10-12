# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""Гейт v5.1.12: паспорт своего двигателя, шаг датчика, честная панель, все параметры.

До v5.1.12 детектор у ДЭС №3 (QSX15) сравнивал давление масла с минимумом KTA50
(310 кПа) и горел всегда; гейт писал «сигналов панели нет» при «Общей аварии» с
кодом и судил о температуре масла по ОЖ — её не было в параметрах гейта.

Запуск:  py -3 scripts/test_gate_passport_v5112.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analytics.config import AnalyticsConfig  # noqa: E402
from analytics.detectors import _detect_oil_dilution  # noqa: E402
from analytics.metrics import compute_derived_metrics, estimate_sensor_step  # noqa: E402
import online.status_assembler as sa  # noqa: E402
from online.engine import _passport_stale  # noqa: E402

_errors: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        _errors.append(msg)


KB = Path(__file__).resolve().parent.parent / "knowledge_base"
T0 = datetime(2026, 10, 8, 6, 0, tzinfo=timezone.utc)

# ── 1. Шаг датчика по истории ────────────────────────────────────────────────
des3 = [(203, 36), (212, 80), (220, 169), (223, 407), (232, 524), (240, 4763), (248, 12797),
        (252, 3856), (260, 590), (268, 14), (276, 6), (280, 8), (288, 6), (296, 9), (0, 500)]
kta = [(v, 300) for v in (372, 376, 380, 383, 388, 392, 396, 400, 403, 408, 412, 416, 420)]
check(estimate_sensor_step(des3) == 8.0, f"1: ДЭС №3 — шаг 8: {estimate_sensor_step(des3)}")
check(estimate_sensor_step(kta) == 4.0, f"1б: KTA50 — шаг 4: {estimate_sensor_step(kta)}")
check(estimate_sensor_step([(240, 10), (248, 20)]) is None, "1в: мало истории — None")


# ── 2. Детектор: ряд по пакетам ──────────────────────────────────────────────
def cfg_for(engine: str, step: float | None = None) -> AnalyticsConfig:
    c = AnalyticsConfig.from_pair(KB, "pcc3300", engine)
    if step is not None:
        c.set_sensor_step("OIL_PRESS", step)
    return c


def run(engine: str, profile, minutes: int = 10, step: float | None = None, gap=None,
        oil_temp: bool = False):
    """profile(t_sec) -> (давление, обороты); пакет раз в 10 с."""
    c = cfg_for(engine, step)
    op, rpm = c.role_to_addr("OIL_PRESS"), c.role_to_addr("RPM")
    hb = int(c.seg("data_quality", "heartbeat_addr"))
    by: dict = {op: [], rpm: [], hb: []}
    for k in range(minutes * 6 + 1):
        t = T0 + timedelta(seconds=10 * k)
        if gap and gap[0] <= 10 * k < gap[1]:
            continue
        p, r = profile(10 * k)
        by[hb].append({"ts": t, "value": k})
        by[op].append({"ts": t, "value": p})
        by[rpm].append({"ts": t, "value": r})
    gaps = ([{"gap_start": T0 + timedelta(seconds=gap[0]), "gap_end": T0 + timedelta(seconds=gap[1])}]
            if gap else [])
    t1 = T0 + timedelta(minutes=minutes, seconds=10)
    dm = compute_derived_metrics(by, T0, t1, gaps, c)
    chars = {"OIL_TEMP": {"min": 80, "max": 82, "median": 81}} if oil_temp else {}
    return dm, _detect_oil_dilution(chars, dm, T0, c)


def dip(lo, a, b, base=248, rpm=1500):
    return lambda t: ((lo if a <= t < b else base), rpm)


_, d = run("cummins_qsx15", dip(240, 100, 137), step=8)
check(not d, f"2: QSX15 — провал до 240 на 37 с не тревога: {[x.trigger for x in d]}")
dm, d = run("cummins_qsx15", dip(240, 100, 200), step=8)
check(len(d) == 1 and d[0].values["limit"]["hard"] is False and "в пределах шага" in d[0].trigger
      and "не норма" in d[0].trigger, f"2б: 240 90 с — ниже паспорта в пределах шага: {[x.trigger for x in d]}")
check(d and d[0].values["limit"]["limit"] == 242 and d[0].values["limit"]["step"] == 8
      and d[0].values["limit"]["step_source"] == "history", f"2в: паспорт QSX15 242, шаг 8: {d and d[0].values}")
_, d = run("cummins_qsx15", dip(232, 100, 200), step=8)
check(len(d) == 1 and d[0].values["limit"]["hard"] is True and "нарушение паспорта" in d[0].trigger,
      f"2г: 232 — нарушение: {[x.trigger for x in d]}")
_, d = run("cummins_qsx15", lambda t: (0, 0), step=8)
check(not d, "2д: стоящий двигатель (обороты 0, давление 0) — не нарушение")
_, d = run("cummins_qsx15", dip(100, 60, 300), step=8)
check(not d, "2е: ниже уставки останова панели, а панель молчит — данные, не паспорт")
_, d = run("cummins_qsx15", dip(232, 100, 220), step=8, gap=(150, 400))
check(not d, f"2ж: дыра связи рвёт участок: {[x.trigger for x in d]}")
_, d = run("cummins_qsx15", dip(232, 100, 200), step=8, oil_temp=False)
check(len(d) == 1, "2з: паспорт — без температуры масла")
_, d = run("cummins_kta50", lambda t: (300, 1500), minutes=3, step=4)
check(len(d) == 1 and d[0].values["limit"]["hard"] and d[0].values["limit"]["limit"] == 310,
      f"2и: KTA50 300 кПа — нарушение паспорта 310: {[x.trigger for x in d]}")
_, d = run("cummins_kta50", lambda t: (308, 1500), minutes=3, step=4)
check(len(d) == 1 and not d[0].values["limit"]["hard"], "2к: KTA50 308 — в пределах шага 4")
_, d = run("cummins_kta50", lambda t: (400, 1500), minutes=3, step=4)
check(not d, "2л: KTA50 400 — норма")
dm, _ = run("cummins_qsx15", dip(240, 100, 200), step=50)
check(abs(dm.oil_press_step_kpa - 12.1) < 0.05, f"2м: шаг не больше 5 % паспорта: {dm.oil_press_step_kpa}")
dm, d = run("cummins_qsx15", dip(240, 100, 200))
check(dm.oil_press_step_source == "default" and dm.oil_press_step_kpa == 4.0 and d
      and not d[0].values["limit"]["hard"],
      f"2н: без истории — шаг по умолчанию 4 (240 ≥ 242−4 — в пределах шага): {dm.oil_press_step_kpa}")
_, d = run("cummins_qsx15", dip(232, 100, 200))
check(d and d[0].values["limit"]["hard"], "2о: без истории, 232 < 238 — нарушение")

# ── 3. Живой состав: давно закончившееся нарушение — не живое ────────────────
end = T0 + timedelta(hours=1)
old = {"values": {"limit": {"kind": "passport", "until": (end - timedelta(minutes=20)).isoformat(),
                            "ongoing": False}}}
fresh = {"values": {"limit": {"kind": "passport", "until": (end - timedelta(minutes=3)).isoformat(),
                              "ongoing": False}}}
going = {"values": {"limit": {"kind": "passport", "until": end.isoformat(), "ongoing": True}}}
check(_passport_stale(old, end) and not _passport_stale(fresh, end) and not _passport_stale(going, end),
      "3: в живом составе — пока идёт или закончилось < 10 мин назад")


# ── 4. Состав и промпт гейта ─────────────────────────────────────────────────
def det_passport(hard: bool) -> dict:
    return {"scenario": "OIL_DILUTION", "severity": "CAUTION", "fault_codes": [], "ref_codes": [143, 415],
            "related_roles": ["OIL_PRESS"],
            "trigger": "давление масла ниже паспортного минимума Cummins QSX15 242 кПа",
            "values": {"oil_press_min": 232 if hard else 240, "passport_min_kpa": 242,
                       "limit": {"kind": "passport", "param": "OIL_PRESS", "limit": 242, "unit": "кПа",
                                 "value": 232 if hard else 240, "run_sec": 600, "step": 8,
                                 "step_source": "history", "hard": hard, "engine": "Cummins QSX15"}}}


LP_OIL_TEMP = {"scenario": "LIMIT_PROXIMITY", "severity": "CAUTION", "fault_codes": [], "ref_codes": [214],
               "related_roles": ["OIL_TEMP"], "trigger": "Температура масла: 100.00 в пределах 10% от порога 110.0",
               "values": {}}
NO_LOAD = {"scenario": "NO_LOAD_TOO_LONG", "severity": "CAUTION", "fault_codes": [], "related_roles": ["LOAD_PCT"],
           "trigger": "без нагрузки 17 мин", "values": {}}


def cf(fault_start: datetime, code: int = 1453) -> dict:
    return {"scenario": "CONTROLLER_FAULT", "severity": "CAUTION", "fault_codes": [code],
            "related_roles": ["FAULT_MASK_8", "LAST_FAULT_CODE"],
            "trigger": f"Общая авария (CommonAlarm) при несброшенном коде неисправности {code}",
            "values": {"fault_start": fault_start.isoformat(), "fault_end": None, "last_fault_code": code}}


VALUES = {"RPM": {"value": 1500, "unit": "rpm", "min": 1498, "max": 1502},
          "LOAD_PCT": {"value": 21, "unit": "%", "min": 18, "max": 25},
          "OIL_PRESS": {"value": 248, "unit": "kPa", "min": 240, "max": 252},
          "OIL_TEMP": {"value": 97, "unit": "°C", "min": 96, "max": 100},
          "COOLANT_TEMP": {"value": 83, "unit": "°C", "min": 83, "max": 85},
          "CURRENT_L1": {"value": 111, "unit": "A"}, "HEARTBEAT": {"value": 233208204},
          "LAST_FAULT_CODE": {"value": 1453}}


def row(dets, code_at_start: int = 1453, t_start=T0, supp=None):
    return {"id": 1, "run_state": 3, "t_start": t_start, "gate_suppressed_hash": supp,
            "characteristics_json": {"subsegments": [{"characteristics": {
                "LAST_FAULT_CODE": {"value_start": code_at_start, "value_end": 1453}}}]},
            "current_values_json": {"values": VALUES}, "active_detections_json": dets}


s = sa.build_structural_status(row([cf(T0 - timedelta(days=6)), LP_OIL_TEMP]))
check(s["panel_alarms"][0]["fresh"] is False and sa.gate_can_cancel(s),
      "4: «Общая авария» висела на начало — снятие не запрещено")
p = sa.build_warning_prompt(s)
check("сигналов панели нет" not in p and "висела на начало сегмента" in p and "1453" in p
      and "cancel ДОПУСТИМ" in p, f"4б: честная строка панели: {p[:600]}")
check("у уставки останова панели" in p and "у паспортного порога" not in p, "4в: LIMIT_PROXIMITY — уставка панели")
check("OIL_TEMP: 97" in p and "← параметр тревоги A1" in p and "(мин 96 / макс 100)" in p,
      "4г: все параметры, параметр тревоги с мин/макс")
check("HEARTBEAT" not in p and "LAST_FAULT_CODE:" not in p and "Генератор:" in p, "4д: без служебных, группы")
check("Правила вердикта" in p and "по её собственному параметру" in p, "4е: правила вердикта")

s = sa.build_structural_status(row([cf(T0 + timedelta(hours=2)), LP_OIL_TEMP]))
r = sa.uncancellable_reasons(s)
check(s["panel_alarms"][0]["fresh"] and [x["code"] for x in r] == ["panel_fresh"]
      and "отмена НЕДОСТУПНА" in sa.build_warning_prompt(s), f"4ж: появилась в сегменте — запрет: {r}")
s = sa.build_structural_status(row([cf(T0 - timedelta(days=6)), LP_OIL_TEMP], code_at_start=1452))
check(s["panel_alarms"][0]["fresh"], "4з: смена кода на фоне той же «Общей аварии» — новое срабатывание")

s = sa.build_structural_status(row([det_passport(hard=True)]))
r = sa.uncancellable_reasons(s)
p = sa.build_warning_prompt(s)
check([x["code"] for x in r] == ["passport"] and "НАРУШЕНИЕ ПАСПОРТА" in p and "отмена НЕДОСТУПНА" in p,
      f"4и: нарушение паспорта — запрет: {r}")
s_soft = sa.build_structural_status(row([det_passport(hard=False)]))
check(sa.gate_can_cancel(s_soft) and "в пределах шага датчика" in sa.build_warning_prompt(s_soft),
      "4к: в пределах шага — снимаемо, но «не норма»")
check(sa.compute_analytics_hash([det_passport(True)]) != sa.compute_analytics_hash([det_passport(False)]),
      "4л: нарушение паспорта — другой состав (старое снятие на него не переносится)")
h = sa.compute_analytics_hash([det_passport(True)])
check(not sa.is_analytics_suppressed({"gate_suppressed_hash": h}, [det_passport(True)]),
      "4м: снятие нарушение паспорта не гасит")
facts = sa.gate_facts_md(s, "cancel", False, r)
check(facts.startswith("**Факты (система, не ИИ):**") and "нарушение паспорта" in facts and "не применён" in facts,
      f"4н: шапка фактов: {facts}")

# Режимные замечания — вне состава гейта, но в уровне и строке
s = sa.build_structural_status(row([NO_LOAD]))
check(not s["analytics_alarms"] and s["regime_alarms"] and s["severity_level"] == "предупреждение"
      and "аналитика" in sa.format_status_text(s), f"4о: «без нагрузки» — не гейту, но видно: {s['severity_level']}")
check(sa.compute_analytics_hash([LP_OIL_TEMP, NO_LOAD]) == sa.compute_analytics_hash([LP_OIL_TEMP]),
      "4п: режимные замечания не меняют состав гейта")
h = sa.compute_analytics_hash([LP_OIL_TEMP])
s = sa.build_structural_status(row([LP_OIL_TEMP, NO_LOAD], supp=h))
check(s["analytics_suppressed"] and s["severity_level"] == "предупреждение",
      "4р: снятие состава не гасит режимное замечание")
check(sa.compute_fault_hash(sa.build_structural_status(row([LP_OIL_TEMP, NO_LOAD])))
      == sa.compute_fault_hash(sa.build_structural_status(row([LP_OIL_TEMP]))),
      "4с: режимные замечания не дают нового разбора")

# Хронология: прошлое снятие — не довод
p = sa.build_warning_prompt(s, episode_timeline=[{"scenario": "OIL_DILUTION", "severity": "CAUTION",
                                                  "t_open": T0, "t_close": None, "gate_suppressed": True}])
check("снималась гейтом (прошлое решение, не довод)" in p and "отменена гейтом" not in p, "4т: хронология")

# ── 5. Вход заключения: разбор — мнение, вердикт помечен ─────────────────────
from corpus.preprocessor import _format_gate_analyses  # noqa: E402
txt = _format_gate_analyses({"warning_analyses": json.dumps([
    {"t": T0.isoformat(), "alarm_text": "Давление масла", "md": "Разбор", "decision": "cancel", "applied": False,
     "rules": "5.1.12"}])})
check("не противоречь" not in txt and "Факты (система, не ИИ)" in txt and "снятие не применено" in txt,
      f"5: вход заключения: {txt[:300]}")

# ── 6. Перенос промпта гейта в БД ────────────────────────────────────────────
from llm.router import GATE_PANEL_RULE_NEW, migrate_gate_prompt  # noqa: E402
old_p = "…вердикт: decision=\"cancel\" — ложное; допустимо ТОЛЬКО когда сигналов панели нет.\n- pass…"
new_p = migrate_gate_prompt(old_p)
check(new_p is not None and GATE_PANEL_RULE_NEW in new_p and "сигналов панели нет" not in new_p, "6: фраза заменена")
check(migrate_gate_prompt("свой промпт владельца") is None and migrate_gate_prompt(new_p) is None,
      "6б: переписанный промпт и повтор — не трогаем")

# ── 7. Ревью v5.1.12 ─────────────────────────────────────────────────────────
# 7а. Давнее «нарушение» при свежем «в пределах шага» — в живом составе снимаемо
from online.engine import _passport_live_hardness  # noqa: E402
lim = {"kind": "passport", "limit": 242, "unit": "кПа", "value": 232, "run_sec": 300, "step": 8,
       "hard": True, "engine": "Cummins QSX15", "until": end.isoformat(), "ongoing": True,
       "last_value": 240, "hard_until": (end - timedelta(hours=7)).isoformat(), "hard_ongoing": False}
dd = {"scenario": "OIL_DILUTION", "trigger": "x", "values": {"oil_press_min": 232, "limit": dict(lim)}}
_passport_live_hardness(dd, end)
check(dd["values"]["limit"]["hard"] is False and dd["values"]["limit"]["value"] == 240
      and "в пределах шага" in dd["trigger"] and lim["hard"] is True,
      f"7а: давнее нарушение не держит запрет: {dd}")
dd2 = {"scenario": "OIL_DILUTION", "trigger": "x",
       "values": {"limit": dict(lim, hard_until=(end - timedelta(minutes=2)).isoformat())}}
_passport_live_hardness(dd2, end)
check(dd2["values"]["limit"]["hard"] is True, "7а2: свежее нарушение — запрет остаётся")
dm, d = run("cummins_qsx15", lambda t: (232 if 60 <= t < 360 else (240 if t >= 7140 else 248), 1500),
            minutes=120, step=8)
check(d and d[0].values["limit"]["hard"] and d[0].values["limit"]["last_value"] == 240
      and d[0].values["limit"]["hard_ongoing"] is False, f"7а3: метрики свежести нарушения: {d and d[0].values}")

# 7б. Дыра связи рвёт участок, даже если пауза короче 90 с
c7 = cfg_for("cummins_qsx15", 8)
_, d = run("cummins_qsx15", dip(232, 100, 140), step=8, gap=(140, 200))
check(not d, f"7б: слепой интервал не засчитывается: {[x.trigger for x in d]}")

# 7в. Режимные замечания: строка при снятии, «снято» не ставится, уровень API
h = sa.compute_analytics_hash([LP_OIL_TEMP])
s = sa.build_structural_status(row([LP_OIL_TEMP, NO_LOAD], supp=h))
st = sa.format_status_text(s)
check("✓" in st and "Температура масла" not in st and "без нагрузки" in st.lower()
      and "Температура масла" not in (sa.extract_alarm_text(s) or ""),
      f"7в: при снятии видно режимное, снятое — нет: {st} | {sa.extract_alarm_text(s)}")
check([x["scenario"] for x in sa.level_dets([LP_OIL_TEMP, NO_LOAD, cf(T0)], True)]
      == ["NO_LOAD_TOO_LONG", "CONTROLLER_FAULT"], "7г: уровень при снятии — панель и режимные")
from analytics.serializer import SCENARIO_RU, apply_gate_suppression  # noqa: E402
parts = {"remarks": [{"source": "analytics", "scenario": "NO_LOAD_TOO_LONG", "open_live": 1, "count": 1}]}
check(apply_gate_suppression(parts, ["NO_LOAD_TOO_LONG"]) is parts, "7д: поздняя отмена режимное не снимает")
check("уставк" in SCENARIO_RU["LIMIT_PROXIMITY"] and "паспорт" not in SCENARIO_RU["LIMIT_PROXIMITY"],
      "7е: LIMIT_PROXIMITY — уставка панели")
from corpus.preprocessor import _extract_verdict  # noqa: E402
check(_extract_verdict([{"scenario": "NO_LOAD_TOO_LONG", "severity": "CAUTION"}], gate_suppressed=True)[1]
      == "CAUTION", "7ж: снятие не гасит режимное замечание в вердикте")
# 7з. Фраза «всегда pass» тоже переносится
from llm.router import GATE_PANEL_PASS_NEW  # noqa: E402
p2 = migrate_gate_prompt("допустимо ТОЛЬКО когда сигналов панели нет.\n- pass; при любых сигналах панели "
                         "управления (внимание/авария) — всегда pass.")
check(p2 and GATE_PANEL_PASS_NEW in p2 and "внимание/авария" not in p2, "7з: «всегда pass» — по запрету")
# 7и. Паспорт главнее комбинированной ветки всегда: нарушение не пропадает из записи
c9 = cfg_for("cummins_kta50", 4)
from analytics.contract import DerivedMetrics  # noqa: E402
dm9 = DerivedMetrics(oil_press_passport_kpa=310, oil_press_step_kpa=4, oil_press_step_source="history",
                     oil_press_below_passport_run_sec=300, oil_press_below_passport_min_kpa=296,
                     oil_press_hard_run_sec=300, oil_press_hard_min_kpa=296,
                     oil_press_passport_until=(T0 + timedelta(minutes=15)).isoformat(),
                     oil_press_passport_ongoing=False,
                     oil_press_hard_until=(T0 + timedelta(minutes=15)).isoformat(),
                     oil_press_hard_ongoing=False)
chars9 = {"OIL_PRESS": {"min": 296, "median": 390}, "OIL_TEMP": {"max": 104, "median": 92}}
d9 = _detect_oil_dilution(chars9, dm9, T0, c9, T0 + timedelta(hours=1))
check(len(d9) == 1 and d9[0].description_key == "OIL_DILUTION.below_passport_min"
      and d9[0].values["limit"]["hard"], f"7и: запись — нарушение паспорта: {[x.description_key for x in d9]}")

# 7к. Нарушение внутри продолжающегося участка: живая копия — число из хвоста в пределах шага
dm, d = run("cummins_qsx15", lambda t: (248 if t < 600 else (232 if t < 900 else 240), 1500),
            minutes=60, step=8)
check(d and d[0].values["limit"]["hard"] and d[0].values["limit"]["last_value"] == 240,
      f"7к: хвост после нарушения — 240: {d and d[0].values['limit']}")
dd3 = d[0].to_dict()
_passport_live_hardness(dd3, T0 + timedelta(minutes=60))
check(dd3["values"]["limit"]["hard"] is False and dd3["values"]["limit"]["value"] == 240
      and "мин. 240" in dd3["trigger"], f"7к2: живая копия: {dd3['trigger']}")
# 7л. Короткий провал дальше шага внутри участка «в пределах шага» — назван отдельно
_, d = run("cummins_qsx15", lambda t: (232 if 200 <= t < 230 else (240 if 100 <= t < 400 else 248), 1500),
           minutes=10, step=8)
check(d and not d[0].values["limit"]["hard"] and d[0].values["limit"]["value"] == 240
      and "кратковременно до 232" in d[0].trigger, f"7л: {d and d[0].trigger}")
# 7м. Эпизод, в котором было нарушение, снятым не помечается
check(sa.passport_hard_seen({"values": {"limit": {"kind": "passport", "hard": False,
                                                   "hard_until": end.isoformat()}}})
      and not sa.passport_hard_seen(det_passport(False)), "7м: «было нарушение» видно и в сниженной копии")

if _errors:
    print(f"FAIL — {len(_errors)} расхождений:")
    for x in _errors:
        print(f"  • {x}")
    sys.exit(1)
print("Все проверки пройдены.")
