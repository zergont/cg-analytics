# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""Риск нагара — накопленное состояние, долг до прожига (v5.1.3).

Решения владельца 11.10.2026 (docs/plans/plan_chain_reports_v5.md, «Закоксовка»):
стоп риск не снимает и не «обнаруживает» его — вердикт «Норма» + «перед пуском»;
прожиг — пропорциональный зачёт (×8 при ≥30 %, ×16 при ≥70 %, отрезки ≥15 мин);
х.х. непрерывный >10 мин — замечание, суммарный — в долг с весом 2; уровни:
малая нагрузка 2/4 ч, х.х. 1/3 ч, холодная ОЖ 1/3 ч; эпизодов, гейта и журнала
тревог у риска нет.

Запуск:  py -3 scripts/test_coking_state.py
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analytics import binding  # noqa: E402
from analytics.accumulators import _update_coking  # noqa: E402
from analytics.contract import CokingRisk, RiskAccumulators  # noqa: E402
from analytics.contract import DerivedMetrics  # noqa: E402
from analytics.detectors import _detect_idle_too_long, run_all_detectors  # noqa: E402
from analytics.serializer import build_summary_parts, render_summary_md  # noqa: E402
from corpus.preprocessor import build_claude_input  # noqa: E402
import online.engine as eng  # noqa: E402
from online.status_assembler import (  # noqa: E402
    build_structural_status, compute_analytics_hash, format_status_text, is_analytics_suppressed,
)

_errors: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        _errors.append(msg)


cfg = binding.build_config(ROOT / "knowledge_base", controller_id="pcc3300", engine_id="cummins_kta50")
LOAD, COOL = cfg.role_to_addr("LOAD_PCT"), cfg.role_to_addr("COOLANT_TEMP")
T0 = datetime(2026, 10, 12, 6, 0, tzinfo=timezone.utc)


def rows(minutes: float, load: float, cool: float = 80.0, start=T0):
    out, t = {LOAD: [], COOL: []}, start
    while t < start + timedelta(minutes=minutes):
        out[LOAD].append({"ts": t, "value": load})
        out[COOL].append({"ts": t, "value": cool})
        t += timedelta(seconds=30)
    return out


def upd(cr, minutes, load=None, rs=3, cool=80.0, start=T0):
    by = rows(minutes, load, cool, start) if load is not None else {}
    _update_coking(cr, by, start, start + timedelta(minutes=minutes), "NA", rs,
                   minutes * 60, cfg, [])
    return cr


# 1. ДГУ №2: накопленное с 29.09 переносится через стоп — уровень по новым порогам
cr = CokingRisk(low_load_zone_sec=17303, idle_low_rpm_sec=1621, coolant_below_60_sec=2619,
                last_purge_ts="2026-07-06T07:43:16+00:00")
upd(cr, 60, rs=0)
check(cr.low_load_zone_sec == 17303 and cr.idle_low_rpm_sec == 1621, "1: стоп ничего не меняет")
check(cr.risk_level == "RED", f"1б: 4,8 ч малой нагрузки ≥4 ч — высокий: {cr.risk_level}")
check(abs(cr.debt_sec - (17303 + 2 * 1621 + 2619)) < 1, f"1в: долг с весом х.х. ×2: {cr.debt_sec}")
check(cr.purge_left_low_min == round(cr.debt_sec / 8 / 60, 1) and cr.purge_left_high_min == round(cr.debt_sec / 16 / 60, 1),
      f"1г: до сброса ≈48/24 мин: {cr.purge_left_low_min}/{cr.purge_left_high_min}")
for rs_ in (0, 3):
    dets = run_all_detectors({}, DerivedMetrics(), RiskAccumulators(coking_risk=cr), [], "NA", rs_, T0,
                             T0 + timedelta(hours=1), None, cfg)
    check(not any(d.scenario == "COKING_RISK" for d in dets),
          f"1д: детекции нагара нет (RS {rs_}) — состояние несёт сводка")

# 2. Малая нагрузка — по фактическому LOAD_PCT
cr = upd(CokingRisk(), 60, load=20)
check(abs(cr.low_load_zone_sec - 3600) < 31 and cr.last_added_sec > 3500, f"2: час на 20 % — в долг: {cr.low_load_zone_sec}")
check(cr.risk_level == "GREEN", "2б: 1 ч < 2 ч — уровень низкий")
cr = upd(cr, 70, load=20, start=T0 + timedelta(hours=1))
check(cr.risk_level == "YELLOW", f"2в: 2 ч 10 мин — повышенный: {cr.risk_level}")

# 3. Прожиг: 30 мин при 50 % — 15 мин ждём, затем гасится ×8
cr = CokingRisk(low_load_zone_sec=4 * 3600)
upd(cr, 10, load=50)
check(cr.low_load_zone_sec == 4 * 3600 and cr.purge_pending_sec > 0, "3: отрезок <15 мин — зачёт ждёт")
upd(cr, 20, load=50, start=T0 + timedelta(minutes=10))
check(abs(cr.low_load_zone_sec - (4 * 3600 - 30 * 60 * 8)) < 300,
      f"3б: 30 мин при 50 % погасили ~4 ч из 4 ч: {cr.low_load_zone_sec}")
cr = CokingRisk(low_load_zone_sec=4 * 3600)
upd(cr, 20, load=75)
check(cr.low_load_zone_sec == 0 and cr.last_purge_ts and cr.risk_level == "GREEN",
      f"3в: 20 мин при 75 % (×16 = 5,3 ч) — полный прожиг: {cr.low_load_zone_sec}, {cr.last_purge_ts}")

# 4. Обрыв отрезка до 15 мин — зачёт сгорает; холодный мотор — не прожиг
cr = CokingRisk(low_load_zone_sec=3 * 3600)
upd(cr, 10, load=50)
upd(cr, 5, load=20, start=T0 + timedelta(minutes=10))
upd(cr, 10, load=50, start=T0 + timedelta(minutes=15))
check(cr.low_load_zone_sec > 3 * 3600 and cr.debt_paid_total_sec == 0, f"4: короткие отрезки не гасят: {cr.debt_paid_total_sec}")
cr = upd(CokingRisk(low_load_zone_sec=3 * 3600), 30, load=50, cool=40.0)
check(cr.debt_paid_total_sec == 0, "4б: ОЖ холодная — не прожиг")

# 4в. Провал ниже 30 % короче 60 с — шум у порога, отрезок не рвётся (решение 11.10)
cr = CokingRisk(low_load_zone_sec=3 * 3600)
upd(cr, 10, load=50)
upd(cr, 0.5, load=20, start=T0 + timedelta(minutes=10))
upd(cr, 10, load=50, start=T0 + timedelta(seconds=630))
check(cr.debt_paid_total_sec > 0 and cr.purge_dip_sec == 0, f"4в: провал 30 с не рвёт отрезок: {cr.debt_paid_total_sec}")
cr = CokingRisk(low_load_zone_sec=3 * 3600)
upd(cr, 10, load=50)
upd(cr, 1.5, load=20, start=T0 + timedelta(minutes=10))
upd(cr, 10, load=50, start=T0 + timedelta(seconds=690))
check(cr.debt_paid_total_sec == 0, f"4г: провал 90 с рвёт отрезок: {cr.debt_paid_total_sec}")

# 5. Холостой ход: суммарно в долг ×2, непрерывный >10 мин — замечание
cr = upd(CokingRisk(), 14, rs=2)
check(abs(cr.idle_low_rpm_sec - 840) < 1 and abs(cr.debt_sec - 1680) < 1, f"5: х.х. 14 мин, долг 28 мин: {cr.debt_sec}")
d = _detect_idle_too_long(RiskAccumulators(coking_risk=cr), T0, cfg)
check(len(d) == 1 and "14 мин подряд" in d[0].trigger, f"5б: непрерывный >10 мин: {d}")
cr = upd(CokingRisk(), 5, rs=2)
upd(cr, 30, load=50, start=T0 + timedelta(minutes=5))
upd(cr, 5, rs=5, start=T0 + timedelta(minutes=35))
check(_detect_idle_too_long(RiskAccumulators(coking_risk=cr), T0, cfg) == [] and cr.risk_level == "GREEN",
      "5в: прогрев 5 + охлаждение 5 мин — без замечания и без жёлтого (раньше — жёлтый)")


# 6. Сводка стоп-сегмента: «Норма» + «перед пуском», без «зафиксировал»
def seg(rs, cr_start: CokingRisk, cr_end: CokingRisk):
    sub = NS(characteristics={}, detections=[], risk_accumulators=RiskAccumulators(coking_risk=cr_end))
    return NS(run_state=rs, stop_kind="SIMPLE" if rs == 0 else None, cause_close="DAILY_BOUNDARY",
              sequence_checks=[], t_start=T0.isoformat(), t_end=(T0 + timedelta(hours=24)).isoformat(),
              cause_open="DAILY_BOUNDARY", subsegments=[sub], coking_start=cr_start.to_dict())


red = CokingRisk(low_load_zone_sec=17303, idle_low_rpm_sec=1621, coolant_below_60_sec=2619,
                 last_purge_ts="2026-07-06T07:43:16+00:00", last_accum_ts="2026-09-29T13:06:30+00:00")
upd(red, 1, rs=0)
old_ep = {"source": "analytics", "severity": "CAUTION", "scenario": "COKING_RISK", "gate_suppressed": True,
          "t_open": T0 - timedelta(days=20), "t_close": None, "active_sec": 1.7e6, "blind_sec": 0.0}
from zoneinfo import ZoneInfo  # noqa: E402
MSK = ZoneInfo("Europe/Moscow")
p = build_summary_parts([seg(0, red, red)], [old_ep], tz=MSK)
md = render_summary_md(p)
check(p["verdict"]["level"] == "НОРМА" and p["verdict"]["suppressed_n"] == 0,
      f"6: стоп с накопленным риском — НОРМА, старый эпизод нагара не в счёт: {p['verdict']}")
check(p["coking"]["before_start"] and p["coking"]["level"] == "RED", "6б: метка «перед пуском»")
check("### Перед пуском" in md and "с последнего прожига 06.07 по 29.09 16:06" in md
      and "установка стояла" in md and "2342" not in md and "зафиксир" not in md,
      f"6в: md честно — накоплен при прошлой работе:\n{md}")

# 7. Рабочий сегмент, где долг вырос — замечание, вердикт CAUTION
start = CokingRisk()
upd(start, 1, rs=3, load=50)
end = CokingRisk.from_dict(start.to_dict())
upd(end, 150, load=20, start=T0 + timedelta(minutes=1))
p = build_summary_parts([seg(3, start, end)], [], tz=MSK)
g = [r for r in p["remarks"] if r.get("kind") == "state"]
check(p["verdict"]["level"] == "CAUTION" and len(g) == 1 and p["coking"]["remark"],
      f"7: долг вырос до повышенного — замечание: {p['verdict']}, {p['coking']}")
md = render_summary_md(p)
check("долг до прожига вырос на ~2 ч 30 мин" in md and "до сброса" in md, f"7б: прирост и остаток в md:\n{md}")

# 7в. Прожиг в рабочем сегменте уменьшил долг — замечания нет, «на начало → на конец»
st4 = CokingRisk(low_load_zone_sec=4 * 3600)
upd(st4, 1, rs=0)
en4 = CokingRisk.from_dict(st4.to_dict())
upd(en4, 30, load=55, start=T0)
p = build_summary_parts([seg(3, st4, en4)], [], tz=MSK)
md = render_summary_md(p)
check(p["verdict"]["level"] == "НОРМА" and not p["coking"]["remark"] and (p["coking"]["net_sec"] or 0) < 0,
      f"7в: долг уменьшился — не замечание: {p['coking']}")
check("на начало **высокий** → на конец **низкий** — прожиг завершён" in md, f"7г: строка начало→конец:\n{md}")

# 7д. Переходный сегмент (охлаждение на х.х.) — долг растёт, замечания нет
st5 = CokingRisk(low_load_zone_sec=3 * 3600)
upd(st5, 1, rs=0)
en5 = CokingRisk.from_dict(st5.to_dict())
upd(en5, 2, rs=5)
p = build_summary_parts([seg(5, st5, en5)], [], tz=MSK)
check(p["verdict"]["level"] == "НОРМА" and not p["coking"]["remark"], f"7д: охлаждение — без замечания: {p['verdict']}")

# 7е. Прогрев по строкам: холодные 10 мин не засчитаны, тёплые 20 мин — да
by = rows(30, 50.0)
by[COOL] = [{"ts": r["ts"], "value": 45.0 if r["ts"] < T0 + timedelta(minutes=10) else 75.0} for r in by[COOL]]
cr = CokingRisk(low_load_zone_sec=6 * 3600)
_update_coking(cr, by, T0, T0 + timedelta(minutes=30), "NA", 3, 1800, cfg, [])
check(abs(cr.debt_paid_total_sec - 20 * 60 * 8) < 300, f"7е: засчитаны только тёплые 20 мин: {cr.debt_paid_total_sec}")

# 7ж. Короткий подсегмент без строк нагрузки не рвёт отрезок
cr = CokingRisk(low_load_zone_sec=6 * 3600)
upd(cr, 10, load=50)
_update_coking(cr, {}, T0 + timedelta(minutes=10), T0 + timedelta(minutes=11), "NA", 3, 60, cfg, [])
upd(cr, 10, load=50, start=T0 + timedelta(minutes=11))
check(cr.debt_paid_total_sec > 0, f"7ж: пустой подсегмент — «нет сведений», отрезок идёт: {cr.debt_paid_total_sec}")

# 8. Статус «Состояние» и гейт: нагар — не тревога
row = {"run_state": 0, "characteristics_json": {"stop_kind": "SIMPLE"}, "current_values_json": {},
       "active_detections_json": [{"scenario": "COKING_RISK", "severity": "CAUTION"}],
       "coking_risk_json": red.to_dict(), "t_start": T0}
st = build_structural_status(row)
txt = format_status_text(st)
check(st["analytics_alarms"] == [] and st["severity_level"] == "норма", f"8: нагар не в тревогах: {st['analytics_alarms']}")
check("перед пуском: риск нагара высокий — накоплен с прожига 06.07; до сброса ~48 мин" in txt,
      f"8б: строка «Состояние»: {txt}")
oil = {"scenario": "OIL_DILUTION", "fault_codes": []}
coke = {"scenario": "COKING_RISK", "fault_codes": [2342]}
check(is_analytics_suppressed({"gate_suppressed_hash": compute_analytics_hash([oil])}, [oil, coke]),
      "8в: нагар в составе не мешает снятию гейтом")

# 9. Вход заключения: перенесённое состояние, а не обнаружение
p = build_summary_parts([seg(0, red, red)], [], tz=MSK)
inp = build_claude_input({"id": 1, "run_state": 0, "report_md": "ОТЧЁТ", "characteristics_json": {},
                          "report_summary_json": p})
check("Риск нагара (накопленное состояние, не обнаружение этого сегмента): уровень высокий" in inp
      and "установка стояла" in inp and "Обнаружения: нет" in inp, f"9: шапка заключения:\n{inp[:700]}")
p7 = build_summary_parts([seg(3, start, end)], [], tz=MSK)
inp7 = build_claude_input({"id": 2, "run_state": 3, "report_md": "ОТЧЁТ", "characteristics_json": {},
                           "report_summary_json": p7})
_found = inp7.split("Обнаружения:")[1].split("\n")[0]
check("в этом сегменте вырос — замечание" in inp7 and "нагар" not in _found,
      f"9б: в рабочем — подпись «вырос», в «Обнаружениях» не дублируется:\n{inp7[:600]}")

# 11. По Cummins (v5.1.5): эскалация > 32 ч, прогон T-030, без нагрузки, календарь, QSX15
from analytics.detectors import _detect_no_load_too_long  # noqa: E402
big = CokingRisk(low_load_zone_sec=40 * 3600)
upd(big, 1, rs=0)
check(big.service_needed and big.risk_level == "RED", "11: долг 40 ч — нужен сервисный прожиг")
upd(big, 180, load=50, start=T0 + timedelta(minutes=1))
check(big.service_needed and big.low_load_zone_sec > 0 and big.last_annual_run_ts is None,
      f"11б: 3 ч при 50 % — не T-030 (нет часа ≥75 %), эскалация держится до полного погашения: {big.low_load_zone_sec / 3600:.1f} ч")
big = CokingRisk(low_load_zone_sec=60 * 3600)
upd(big, 1, rs=0)
upd(big, 120, load=50, start=T0)
upd(big, 60, load=80, start=T0 + timedelta(minutes=120))
check(big.low_load_zone_sec == 0 and not big.service_needed and big.last_annual_run_ts and big.risk_level == "GREEN",
      f"11в: прогон T-030 (3 ч, из них 1 ч ≥75 %) — полный прожиг: {big.low_load_zone_sec}, {big.last_annual_run_ts}")
check(big.last_monthly_run_ts is not None, "11г: ежемесячный прогон отмечен")
svc = CokingRisk(low_load_zone_sec=40 * 3600, last_purge_ts="2026-07-06T07:43:16+00:00")
upd(svc, 1, rs=0)
p = build_summary_parts([seg(0, svc, svc)], [], tz=MSK)
md = render_summary_md(p)
check(p["coking"]["service_needed"] and "нужен сервисный прожиг" in md and "пробу масла на топливо" in md,
      f"11д: эскалация в сводке:\n{md}")
check("Прогоны по Cummins: ежемесячный (≥30 мин при ≥30 %) — нет сведений с" in md, f"11е: календарь у стоящей машины:\n{md}")
st_ = build_structural_status({"run_state": 0, "characteristics_json": {"stop_kind": "SIMPLE"},
                               "current_values_json": {}, "active_detections_json": [],
                               "coking_risk_json": svc.to_dict(), "t_start": T0})
check("нужен сервисный прожиг (долг больше 32 ч)" in format_status_text(st_), f"11ж: «Состояние»: {format_status_text(st_)}")
nl = CokingRisk()
upd(nl, 20, load=5)
d = _detect_no_load_too_long(RiskAccumulators(coking_risk=nl), T0, cfg)
check(len(d) == 1 and "20 мин подряд" in d[0].trigger, f"11з: без нагрузки 20 мин — замечание: {d}")
nl = CokingRisk()
upd(nl, 10, load=5)
upd(nl, 2, load=40, start=T0 + timedelta(minutes=10))
upd(nl, 10, load=5, start=T0 + timedelta(minutes=12))
check(_detect_no_load_too_long(RiskAccumulators(coking_risk=nl), T0, cfg) == [],
      "11и: выход под нагрузку на 2 мин рвёт отрезок «без нагрузки»")
old = CokingRisk(calendar_since_ts="2026-08-01T06:00:00+00:00")
upd(old, 1, rs=0)
p = build_summary_parts([seg(0, old, old)], [], tz=MSK)
check(p["coking"]["calendar"]["monthly"]["overdue"], f"11к: ежемесячный просрочен: {p['coking']['calendar']}")
cfg_q = binding.build_config(ROOT / "knowledge_base", controller_id="pcc3300", engine_id="cummins_qsx15")
q = CokingRisk(low_load_zone_sec=3 * 3600)
by = rows(30, 50.0, cool=65.0)
_update_coking(q, by, T0, T0 + timedelta(minutes=30), "NA", 3, 1800, cfg_q, [])
k_ = CokingRisk(low_load_zone_sec=3 * 3600)
_update_coking(k_, by, T0, T0 + timedelta(minutes=30), "NA", 3, 1800, cfg, [])
check(q.debt_paid_total_sec == 0 and k_.debt_paid_total_sec > 0,
      f"11л: ОЖ 65 °C — QSX15 не прожиг (минимум 70), KTA50 прожиг: {q.debt_paid_total_sec}/{k_.debt_paid_total_sec}")

# 10. Движок: эпизодов у нагара нет
opened: list = []


async def _open(*a, **k):
    opened.append(k)
    return 1


async def _none(*a, **k):
    return None


class Cfg:
    def det(self, *a, default=None):
        return default


eng.online_db.open_episode = _open
eng.online_db.find_episode_by_onset = _none
eng.online_db.get_open_segment = _none
e = eng.OnlinePollEngine.__new__(eng.OnlinePollEngine)
e.cfg = Cfg()
e.router_sn, e.equip_type, e.panel_id = "T", "pcc", 1
e._episodes, e._last_open_seg_id, e._supp_recheck = {}, 5, None
e._episode_accrual_ts = T0
asyncio.run(e._process_episodes([{"scenario": "COKING_RISK", "severity": "CAUTION",
                                  "t_detected": T0.isoformat(), "values": {}}], T0 + timedelta(minutes=1), []))
check(opened == [] and e._episodes == {}, f"10: по риску нагара эпизод не заводится: {opened}")

if _errors:
    print(f"FAIL — {len(_errors)} расхождений:")
    for x in _errors:
        print(f"  • {x}")
    sys.exit(1)
print("Все проверки пройдены.")
