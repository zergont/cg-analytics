# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""v5.1.10: проверки перехода с контекстом предыдущего сегмента и разовые события.

Решения владельца 12.10: нарушение перехода — разовое событие (гейт не снимает);
стоп с событием — жёлтый; охлаждение — от последней нагрузки ≥ 30 % до останова
не меньше 3 мин, режим докрутки не важен; пуск без фазы прогрева — норма парка,
холодный — наблюдением; событие не в «Состоянии».

Запуск:  py -3 scripts/test_transitions_v5110.py
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
from analytics.segmenter import (  # noqa: E402
    _check_cooldown_transition, _check_warmup_transition, _inter_segment_checks,
)
from analytics.serializer import apply_gate_suppression, build_summary_parts, render_summary_md  # noqa: E402
from analytics.transitions import (  # noqa: E402
    chain_before, ctx_for_window, link_from_row, link_from_segment, push_ctx,
)

_errors: list[str] = []
MSK = ZoneInfo("Europe/Moscow")
T0 = datetime(2026, 9, 16, 10, 0, tzinfo=timezone.utc)
cfg = binding.build_config(ROOT / "knowledge_base", controller_id="pcc3300", engine_id="cummins_kta50")
LA = cfg.role_to_addr("LOAD_PCT")


def check(cond: bool, msg: str) -> None:
    if not cond:
        _errors.append(msg)


def sub(t0, sec, dets=None, chars=None, zone="NORMAL"):
    return Subsegment(id="x", parent_segment_id="s", t_start=t0.isoformat(),
                      t_end=(t0 + timedelta(seconds=sec)).isoformat(), duration_sec=sec,
                      load_zone=zone, cause_open="SEG_START", cause_close=None,
                      characteristics=chars or {}, derived_metrics=DerivedMetrics(),
                      risk_accumulators=RiskAccumulators(), detections=dets or [], data_quality=1.0)


def seg(rs, t0, sec, cause_open="RUN_STATE_CHANGE", load_max=None, coolant=None, events=None):
    chars = {}
    if load_max is not None:
        chars["LOAD_PCT"] = {"max": load_max}
    if coolant is not None:
        chars["COOLANT_TEMP"] = {"value_start": coolant}
    return Segment(id="s", router_sn="R", equip_type="pcc", panel_id=1, engine_sn="", run_state=rs,
                   run_state_label="?", t_start=t0.isoformat(), t_end=(t0 + timedelta(seconds=sec)).isoformat(),
                   duration_sec=sec, engine_hours_start=None, cause_open=cause_open, cause_close=None,
                   preamble_included=False, data_quality=1.0, subsegments=[sub(t0, sec, chars=chars)],
                   sequence_checks=[], events=events or [])


def loads(stop_t, pairs):
    """Ряд LOAD_PCT: [(секунд до останова, значение)]."""
    return {LA: [{"ts": stop_t - timedelta(seconds=s), "value": v} for s, v in pairs]}


def cool(chain_segs, stop, rows=None):
    _check_cooldown_transition(stop, chain_segs, cfg, Detection, rows or {})
    dets = [d for d in stop.subsegments[0].detections if d.scenario == "COOLDOWN_VIOLATION"]
    chk = {c["check"]: c for c in stop.sequence_checks}
    return dets, chk


# ── 1. Цепочка ─────────────────────────────────────────────────────────────
run = seg(3, T0, 3600, load_max=52.5)
ph5 = seg(5, T0 + timedelta(seconds=3600), 17)
stop_t = T0 + timedelta(seconds=3617)
lk = [link_from_segment(run), link_from_segment(ph5)]
ch = chain_before(lk, stop_t.isoformat())
check(ch is not None and ch.anchor.run_state == 3 and [x.run_state for x in ch.between] == [5],
      f"1: цепочка 3→5→голова: {ch}")
gap_lk = [link_from_segment(run), link_from_segment(seg(5, T0 + timedelta(seconds=3610), 7))]
check(chain_before(gap_lk, stop_t.isoformat()) is None, "1б: разрыв больше 2 с — цепочки нет")
check(ctx_for_window(lk, stop_t) == lk and ctx_for_window(lk, stop_t + timedelta(seconds=30)) == [],
      "1в: контекст стыкуется с началом окна, иначе пусто")
check(len(push_ctx(lk * 5, lk[0])) == 8, "1г: не больше 8 звеньев")
row = {"run_state": 3, "t_start": run.t_start, "t_end": run.t_end, "cause_open": "RUN_STATE_CHANGE",
       "load_pct_max": [12.0, 52.5, None], "coolant_start": None}
check(link_from_row(row) == link_from_segment(run), "1д: звено из БД = звено из памяти")

# ── 2. Охлаждение — правило 12.10 на случаях из данных ─────────────────────
# 16.09 13:29 ДЭС №3: охлаждение 17 с после 52 % — горячий останов
st = seg(0, stop_t, 600)
d2, c2 = cool([run, ph5], st, loads(stop_t, [(200, 52.5), (60, 48.0), (17, 0.0)]))
check(d2 and c2["cooldown_after_elevated"]["passed"] is False and c2["cooldown_after_elevated"]["cooled_sec"] == 17,
      f"2: 17 с после 52 % — нарушение: {c2}")
check("3→5→0" == d2[0].values.get("path") if d2 else False, "2б: путь в values")
# 27.09 07:28 ДГУ №1: 41 % и 4,5 мин охлаждения — норма
run41 = seg(3, T0, 3600, load_max=41.0)
ph45 = seg(4, T0 + timedelta(seconds=3600), 271)
st45 = seg(0, T0 + timedelta(seconds=3871), 600)
d3, c3 = cool([run41, ph45], st45, loads(T0 + timedelta(seconds=3871), [(290, 41.0), (271, 0.0)]))
check(not d3 and c3["cooldown_after_elevated"]["passed"], f"3: 4,5 мин охлаждения — норма: {c3}")
# 27.09 16:03 ДГУ №1/№2: нагрузку сняли за 10 мин, докрутили на номинале (3→0)
run27 = seg(3, T0, 5900, load_max=27.9)
st27 = seg(0, T0 + timedelta(seconds=5900), 600)
d4, c4 = cool([run27], st27, loads(T0 + timedelta(seconds=5900), [(290, 0.3), (60, 0.0)]))
check(not d4 and c4["cooldown_after_elevated"]["passed"], f"4: нагрузка < 30 % — охлаждать нечего: {c4}")
run40 = seg(3, T0, 5900, load_max=40.0)
st40 = seg(0, T0 + timedelta(seconds=5900), 600)
d4b, c4b = cool([run40], st40, loads(T0 + timedelta(seconds=5900), [(290, 0.5), (200, 0.0)]))
check(not d4b and c4b["cooldown_after_elevated"]["passed"],
      f"4б: нагрузку сняли раньше чем за 3 мин — это охлаждение: {c4b}")
# Нет данных о нагрузке — «не оценено», без замечания
d5, c5 = cool([run40], seg(0, T0 + timedelta(seconds=5900), 600), {})
check(not d5 and "cooldown_not_evaluated" in c5, f"5: нет данных — не оценено: {c5}")
# 3→6→0 прямо с нагрузки — нарушение (режим 6 — не охлаждение)
run6 = seg(3, T0, 3600, load_max=45.0)
r6 = seg(6, T0 + timedelta(seconds=3600), 20)
st6 = seg(0, T0 + timedelta(seconds=3620), 600)
d6, _ = cool([run6, r6], st6, loads(T0 + timedelta(seconds=3620), [(120, 45.0), (20, 44.0)]))
check(bool(d6), "6: 3→6→0 с нагрузки — нарушение")
# Продолжение стопа (рез по снятию аварии) — ничего
stc = seg(0, stop_t, 600, cause_open="SHUTDOWN_CLEARED")
d7, c7 = cool([run, ph5], stc, loads(stop_t, [(60, 52.5)]))
check(not d7 and not c7, "7: продолжение стопа — без проверки")
# Аварийный останов — префикс в тексте
sta = seg(0, stop_t, 600, events=[{"type": "FAULT", "severity": "shutdown"}])
d8, _ = cool([run, ph5], sta, loads(stop_t, [(60, 52.5)]))
check(d8 and d8[0].trigger.startswith("АВАРИЙНЫЙ ОСТАНОВ"), "8: аварийный — с префиксом")

# Удержанное значение (ряд по изменению, пульс ~15 мин): замер перед окном истории
run_u = seg(3, T0, 600)            # короткий прогон без LOAD_PCT в подсегментах
st_u = seg(0, T0 + timedelta(seconds=600), 600)
_check_cooldown_transition(st_u, [run_u], cfg, Detection, {},
                           load_seed={"ts": T0 + timedelta(seconds=25), "value": 0.0, "gaps": []})
cu = {c["check"]: c for c in st_u.sequence_checks}
check(cu.get("cooldown_after_elevated", {}).get("passed") is True, f"5б: удержанные 0 % — норма, не «не оценено»: {cu}")
st_h = seg(0, T0 + timedelta(seconds=600), 600)
_check_cooldown_transition(st_h, [seg(3, T0, 600, load_max=80.0)], cfg, Detection, {},
                           load_seed={"ts": T0 + timedelta(seconds=270), "value": 80.0, "gaps": []})
check(any(d.scenario == "COOLDOWN_VIOLATION" for d in st_h.subsegments[0].detections),
      "5в: удержанные 80 % до останова (330 с без замеров) — нарушение")
st_g = seg(0, T0 + timedelta(seconds=600), 600)
_check_cooldown_transition(st_g, [seg(3, T0, 600, load_max=80.0)], cfg, Detection, {},
                           load_seed={"ts": T0 + timedelta(seconds=270), "value": 80.0,
                                      "gaps": [{"gap_start": T0 + timedelta(seconds=300),
                                                "gap_end": T0 + timedelta(seconds=590)}]})
cg = {c["check"]: c for c in st_g.sequence_checks}
check(not st_g.subsegments[0].detections and "обрыв связи" in cg.get("cooldown_not_evaluated", {}).get("details", ""),
      f"5г: обрыв связи перед остановом — не оценено, без события: {cg}")

# ── 3. Контекст из прошлых окон: голова окна видит работу перед ней ────────
ctx = push_ctx(push_ctx([], link_from_segment(run)), link_from_segment(ph5))
win_stop = seg(0, stop_t, 600)
_inter_segment_checks([win_stop], loads(stop_t, [(60, 52.5)]), cfg, {}, transition_ctx=ctx, ts_from=stop_t)
check(any(d.scenario == "COOLDOWN_VIOLATION" for d in win_stop.subsegments[0].detections),
      "9: голова окна с контекстом — нарушение в данных сегмента")
win_stop2 = seg(0, stop_t, 600)
_inter_segment_checks([win_stop2], loads(stop_t, [(60, 52.5)]), cfg, {}, transition_ctx=ctx,
                      ts_from=stop_t + timedelta(seconds=5))
check(not win_stop2.subsegments[0].detections, "9б: контекст не стыкуется с окном — не оцениваем")

# ── 4. Прогрев ──────────────────────────────────────────────────────────────
s0 = seg(0, T0, 3600, cause_open="REPORT_START")
w2 = seg(2, T0 + timedelta(seconds=3600), 300, coolant=15.0)
r3 = seg(3, T0 + timedelta(seconds=3900), 3600)
_check_warmup_transition(r3, [s0, w2], cfg, Detection)
check({c["check"] for c in r3.sequence_checks} >= {"cold_start_context", "warmup_duration"}
      and not r3.subsegments[0].detections, "10: холодный пуск, прогрев 300 с — норма")
w2s = seg(2, T0 + timedelta(seconds=3600), 60, coolant=15.0)
r3s = seg(3, T0 + timedelta(seconds=3660), 3600)
_check_warmup_transition(r3s, [s0, w2s], cfg, Detection)
check([d.scenario for d in r3s.subsegments[0].detections] == ["WARMUP_VIOLATION"], "11: прогрев 60 с при холодном — событие")
r3w = seg(3, T0 + timedelta(seconds=3600), 3600, coolant=35.0)
_check_warmup_transition(r3w, [s0], cfg, Detection)
cw = {c["check"]: c for c in r3w.sequence_checks}
check(cw.get("warmup_presence", {}).get("passed") is True and "cold_start_no_warmup" not in cw
      and not r3w.subsegments[0].detections, "12: тёплый пуск без прогрева — запись, без замечания")
r3c = seg(3, T0 + timedelta(seconds=3600), 3600, coolant=12.0)
_check_warmup_transition(r3c, [s0], cfg, Detection)
cc = {c["check"]: c for c in r3c.sequence_checks}
check(cc.get("cold_start_no_warmup", {}).get("passed") is False and not r3c.subsegments[0].detections,
      "13: холодный пуск без прогрева — наблюдение, без события")
r3r = seg(3, T0 + timedelta(seconds=3600), 3600, cause_open="REPORT_START")
_check_warmup_transition(r3r, [s0], cfg, Detection)
check(not r3r.sequence_checks, "14: продолжение работы после реза — без проверки прогрева")
# Второй пуск в окне не заимствует прогрев первого
seq = [s0, w2, seg(3, T0 + timedelta(seconds=3900), 600), seg(0, T0 + timedelta(seconds=4500), 600)]
r3b = seg(3, T0 + timedelta(seconds=5100), 600, coolant=40.0)
_check_warmup_transition(r3b, seq, cfg, Detection)
check({c["check"]: c for c in r3b.sequence_checks}.get("warmup_presence", {}).get("details") == "пуск без фазы прогрева",
      "15: второй пуск — без чужого прогрева")

# ── 5. Сводка: событие, вердикт, цвет стопа ────────────────────────────────
ev_stop = seg(0, stop_t, 3600)
_check_cooldown_transition(ev_stop, [run, ph5], cfg, Detection, loads(stop_t, [(60, 52.5)]))
p = build_summary_parts([ev_stop], [], tz=MSK, t_from=stop_t, t_to=stop_t + timedelta(hours=1))
g = next((x for x in p["remarks"] if x.get("kind") == "event"), None)
check(g and g["count"] == 1 and not g["at_end"], f"16: группа события: {g}")
check(p["verdict"]["level"] == "CAUTION" and p["verdict"]["remarks_n"] == 1 and p["end_level"] == "НОРМА",
      f"16б: вердикт CAUTION, одно замечание, на конец — норма: {p['verdict']} / {p['end_level']}")
check(not p["failed_checks"], f"16в: парная проверка вторым замечанием не считается: {p['failed_checks']}")
md = render_summary_md(p)
check("событие 16.09 14:00:17" in md and "воздействие" not in md.split("Нарушение охлаждения")[1].split("\n")[0],
      f"16г: «событие ЧЧ:ММ:СС» без воздействия:\n{md}")
from web.segment_view import severity_from_parts  # noqa: E402
check(severity_from_parts(p, "SIMPLE") == "CAUTION", "17: простой стоп с событием — жёлтый (решение 12.10)")
q = apply_gate_suppression(p, {"COOLDOWN_VIOLATION"})
check(q["verdict"]["level"] == "CAUTION", "18: гейт событие не снимает")
# Событие — выше целиком снятых замечаний (контракт: живые выше)
sup = {"source": "analytics", "scenario": "OIL_DILUTION", "severity": "CAUTION", "gate_suppressed": True,
       "t_open": stop_t, "t_close": None, "active_sec": 3600.0, "blind_sec": 0.0}
ps = build_summary_parts([ev_stop], [sup], tz=MSK, t_from=stop_t, t_to=stop_t + timedelta(hours=1))
check([x.get("kind") for x in ps["remarks"]][:1] == ["event"], f"18б: событие выше снятого: {[x['scenario'] for x in ps['remarks']]}")
# Запасной источник — точечный эпизод, если детекции нет
ep = {"source": "analytics", "scenario": "COOLDOWN_VIOLATION", "severity": "CAUTION", "gate_suppressed": False,
      "t_open": stop_t, "t_close": stop_t, "active_sec": 0.0, "blind_sec": 0.0, "close_reason": "transition_event"}
p2 = build_summary_parts([seg(0, stop_t, 3600)], [ep], tz=MSK, t_from=stop_t, t_to=stop_t + timedelta(hours=1))
check(any(x.get("kind") == "event" for x in p2["remarks"]) and p2["verdict"]["remarks_n"] == 1,
      "19: нет детекции — замечание из точечного эпизода")

# ── 6. Живой канал, гейт, корпус ────────────────────────────────────────────
from online.engine import _extract_open_segment_data, _put_counts  # noqa: E402
_, act = _extract_open_segment_data(ev_stop)
check(not any(a.get("scenario") == "COOLDOWN_VIOLATION" for a in act), "20: событие — не живая тревога")
dd = next(d for d in ev_stop.subsegments[0].detections if d.scenario == "COOLDOWN_VIOLATION")
_put_counts(dd, {"count_window": 3, "dur_window": 0, "count_since": 1, "dur_since": 0}, None)
check(dd.values["history_count_30d"] == 3 and "history_duration_30d_sec" not in dd.values,
      "21: счётчик события — без «суммарно»")
from online.status_assembler import _without_state, build_warning_prompt, compute_analytics_hash  # noqa: E402
_c = {"scenario": "COOLDOWN_VIOLATION", "fault_codes": [611]}
_o = {"scenario": "OIL_DILUTION", "fault_codes": [], "ref_codes": [143, 415]}
check(_without_state([_c, _o]) == [_o] and compute_analytics_hash([_c, _o]) == compute_analytics_hash([_o]),
      "22: событие вне состава гейта")
pr = build_warning_prompt({"analytics_alarms": [], "panel_alarms": [], "mode_label": "Стоп", "run_state": 0,
                           "load_pct": None, "severity_level": "норма", "panel_severity": "норма",
                           "analytics_severity": "норма"}, episode_timeline=[ep])
check("разовое событие" in pr and "воздействие" not in pr.split("COOLDOWN_VIOLATION")[1].split("\n")[0],
      "23: хронология гейта — «разовое событие»")
from corpus.preprocessor import _remarks_from_summary  # noqa: E402

# 25. Открытый сегмент: точечный эпизод события — один раз, после 60 с
import asyncio  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from online import engine as _eng  # noqa: E402
_ins: list = []


async def _onsets(r, e, p, ts):
    return set()


async def _insert(r, e, p, **kw):
    _ins.append(kw)
    return 1


_eng.online_db.existing_episode_onsets = _onsets
_eng.online_db.insert_closed_episode = _insert
_fake = SimpleNamespace(cfg=cfg, router_sn="R", equip_type="pcc", panel_id=1, key="R/pcc/1",
                        _events_seeded=set())
asyncio.run(_eng.OnlinePollEngine._seed_open_events(_fake, ev_stop, stop_t + timedelta(seconds=30)))
check(not _ins, "25: раньше 60 с — не сеем")
asyncio.run(_eng.OnlinePollEngine._seed_open_events(_fake, ev_stop, stop_t + timedelta(seconds=90)))
asyncio.run(_eng.OnlinePollEngine._seed_open_events(_fake, ev_stop, stop_t + timedelta(seconds=120)))
check(len(_ins) == 1 and _ins[0]["t_close"] == _ins[0]["t_open"] and _ins[0]["active_sec"] == 0.0
      and _ins[0]["close_reason"] == "transition_event", f"25б: один точечный эпизод: {_ins}")
check("разовое событие 16.09 14:00:17" in (_remarks_from_summary({"report_summary_json": p}) or ""),
      "24: вход модели — «разовое событие», не тревога")

if _errors:
    print(f"FAIL — {len(_errors)} расхождений:")
    for x in _errors:
        print(f"  • {x}")
    sys.exit(1)
print("Все проверки пройдены.")
