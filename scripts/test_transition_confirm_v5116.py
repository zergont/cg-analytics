# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""Подтверждение выхода из стопа (v5.1.16, решения владельца 13.10.2026).

ДЭС №3 13.10.2026 (UTC): четыре попытки за 5 минут с оборотами 0 — «Прогрев»
и три «Работы» с ложными «давление масла 0» / «обороты 0»; трижды 1438
«Отказ прокрутки»; стоп после «Работы» 07:40:28–37 (ключ «Ручной» → «Выкл»)
прошёл как нормальный, и акты №2 и №3 серии не увидели первую аварию.
Теперь: выход из стопа подтверждается фактом (обороты > 250, частота или
«Готов к нагрузке»), неподтверждённый — одна плашка стопа-исхода.

Запуск:  py -3 scripts/test_transition_confirm_v5116.py
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analytics.config import AnalyticsConfig  # noqa: E402
from analytics import classifier as clf  # noqa: E402
from analytics import labels  # noqa: E402
from analytics.segmenter import segment  # noqa: E402
from analytics.transitions import Link  # noqa: E402

_errors: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        _errors.append(msg)


KB = ROOT / "knowledge_base"
CFG = AnalyticsConfig.from_pair(KB, "pcc3300", "cummins_qsx15")


def T(h: int, m: int, s: int = 0, d: int = 13) -> datetime:
    return datetime(2026, 10, d, h, m, s, tzinfo=timezone.utc)


def rs(v, a, b=None):
    return {"addr": 40011, "value": v, "state_start": a, "state_end": b}


def en(addr, v, a, b=None):
    return {"addr": addr, "value": v, "state_start": a, "state_end": b}


def fp(addr, bit, sev, a, b=None, name=None):
    return {"addr": addr, "bit": bit, "severity": sev, "fault_start": a, "fault_end": b,
            "fault_name_ru": name, "fault_name": name}


def h(addr, ts, v):
    return {"addr": addr, "ts": ts, "value": v}


RPM, FREQ, OIL, CLT, OILT = 40068, 40044, 40062, 40064, 40063

# ── Серия 13.10 ──────────────────────────────────────────────────────────────
RS = [rs(0, T(5, 16, 56), T(7, 40, 1)), rs(2, T(7, 40, 1), T(7, 40, 11)),
      rs(0, T(7, 40, 11), T(7, 40, 28)), rs(3, T(7, 40, 28), T(7, 40, 37)),
      rs(0, T(7, 40, 37), T(7, 44, 4)), rs(3, T(7, 44, 4), T(7, 44, 13)),
      rs(0, T(7, 44, 13), T(7, 45, 7)), rs(3, T(7, 45, 7), T(7, 45, 26)),
      rs(0, T(7, 45, 26), T(10, 12, 26)), rs(3, T(10, 12, 26), T(10, 13, 2)),
      rs(5, T(10, 13, 2), T(10, 14, 59)), rs(0, T(10, 14, 59))]
ENUM = RS + [
    en(40013, 4, T(5, 33, 43), T(7, 39, 44)), en(40013, 0, T(7, 39, 44), T(7, 40, 11)),
    en(40013, 4, T(7, 40, 11), T(7, 40, 20)), en(40013, 0, T(7, 40, 20), T(7, 44, 13)),
    en(40013, 4, T(7, 44, 13), T(7, 44, 31)), en(40013, 0, T(7, 44, 31), T(7, 45, 26)),
    en(40013, 4, T(7, 45, 26), T(10, 11, 45)), en(40013, 1, T(10, 11, 45)),
    en(40012, 1433, T(5, 33, 43), T(7, 39, 44)), en(40012, 0, T(7, 39, 44), T(7, 40, 11)),
    en(40012, 1438, T(7, 40, 11), T(7, 40, 20)), en(40012, 0, T(7, 40, 20), T(7, 44, 13)),
    en(40012, 1438, T(7, 44, 13), T(7, 44, 31)), en(40012, 0, T(7, 44, 31), T(7, 45, 26)),
    en(40012, 1438, T(7, 45, 26), T(10, 11, 45)), en(40012, 1248, T(10, 11, 45)),
]
FAULTS = [
    fp(40407, 5, "shutdown", T(5, 33, 43), T(7, 39, 44), "Местная кнопка аварийного останова"),
    fp(40407, 8, "shutdown", T(7, 40, 11), T(7, 40, 20), "Отказ прокрутки стартером"),
    fp(40407, 8, "shutdown", T(7, 44, 13), T(7, 44, 31), "Отказ прокрутки стартером"),
    fp(40407, 8, "shutdown", T(7, 45, 26), T(10, 11, 45), "Отказ прокрутки стартером"),
    fp(40406, 2, "warning", T(10, 11, 45), None, "Предупреждение двигателя"),
    fp(40408, 7, None, T(10, 12, 26), T(10, 13, 2), "Готов к нагрузке"),
]
HIST = []
t = T(7, 30, 19)
while t < T(10, 11, 30):                      # стоит: обороты 0 раз в минуту
    HIST.append(h(RPM, t, 0.0))
    HIST.append(h(OIL, t + timedelta(seconds=3), 0.0))
    t += timedelta(minutes=1)
for ts, v in ((T(10, 11, 45), 0.0), (T(10, 12, 25), 1498.0), (T(10, 12, 34), 1497.0),
              (T(10, 12, 43), 1496.0), (T(10, 12, 53), 1494.0), (T(10, 13, 1), 750.0),
              (T(10, 13, 10), 749.0), (T(10, 13, 46), 748.0), (T(10, 14, 31), 749.0),
              (T(10, 14, 58), 239.0), (T(10, 15, 7), 0.0)):
    HIST.append(h(RPM, ts, v))
for ts, v in ((T(10, 11, 45), 0.0), (T(10, 12, 25), 316.0), (T(10, 12, 43), 308.0),
              (T(10, 13, 1), 252.0), (T(10, 14, 58), 80.0), (T(10, 15, 7), 0.0)):
    HIST.append(h(OIL, ts, v))
for ts, v in ((T(7, 45, 19), 0.0), (T(10, 12, 26), 49.8), (T(10, 13, 1), 0.0)):
    HIST.append(h(FREQ, ts, v))
HIST += [h(CLT, T(7, 35), 45.0), h(OILT, T(7, 35), 55.0)]
HIST.sort(key=lambda r: r["ts"])


def run(ts_from, ts_to, ctx=None, enum=ENUM, hist=HIST, faults=FAULTS, gaps=None):
    # Как источник: периоды, пересекающие окно (state_end > ts_from строго)
    enum = [p for p in enum if p["state_start"] < ts_to
            and (p["state_end"] is None or p["state_end"] > ts_from)]
    hist = [r for r in hist if ts_from - timedelta(seconds=300) <= r["ts"] < ts_to]
    return segment(enum, hist, faults, gaps or [], CFG, "SN", "pcc", 1, "ESN",
                   ts_from, ts_to, transition_ctx=ctx)


def lab(s):
    return labels.label_from_chars(s.to_dict()) or {0: "Стоп", 2: "Прогрев", 3: "Работа",
                                                     5: "Охлаждение"}.get(s.run_state)


def dets(s, scen):
    return [d for sub in s.subsegments for d in sub.detections if d.scenario == scen]


segs = run(T(7, 39), T(10, 16))
got = [(datetime.fromisoformat(s.t_start).strftime("%H:%M:%S"), lab(s)) for s in segs]
# v5.1.23 (решение В4): подпись — по прокрутке, а не по режиму панели. Обороты
# 0: с кодом 1438 и аварией — «Неудачный пуск · Авария», без кода и аварии
# (ключ «Ручной» → «Выкл» 07:40:37) — «Отменённый пуск · Стоп».
# v5.1.25 (решение А): отсчёты оборотов здесь раз в минуту (:19), внутри
# попытки 07:40:28–37 их нет — ноль из 07:40:19 отмены не доказывает:
# «Неудачный пуск · Стоп»
want = [("07:39:00", "Стоп"), ("07:40:01", "Неудачный пуск · Авария"),
        ("07:40:20", "Стоп"), ("07:40:28", "Неудачный пуск · Стоп"),
        ("07:44:04", "Неудачный пуск · Авария"), ("07:44:31", "Стоп"),
        ("07:45:07", "Неудачный пуск · Авария"), ("10:11:45", "Стоп"),
        ("10:12:26", "Работа"), ("10:13:02", "Охлаждение"), ("10:14:59", "Стоп")]
check(got == want, f"1: серия 13.10 — плашки вместо «Прогрева» и «Работ»:\n   {got}")

by = {datetime.fromisoformat(s.t_start).strftime("%H:%M:%S"): s for s in segs}
p1, p2, p3, p4 = by.get("07:40:01"), by.get("07:40:28"), by.get("07:44:04"), by.get("07:45:07")
if p1 and p2 and p3 and p4:
    check(all(p.run_state == 0 for p in (p1, p2, p3, p4)), "1б: плашка — стоп (run_state 0)")
    check(p1.stop_kind == "EMERGENCY" and p1.cause_close == "SHUTDOWN_CLEARED"
          and p1.attempt["t_fall"] == T(7, 40, 11).isoformat() and p1.attempt["path"] == [2],
          f"1в: «Неудачный пуск · Авария» до сброса, падение 07:40:11: {p1.attempt}, {p1.cause_close}")
    check(p2.stop_kind == "SIMPLE" and p2.attempt["outcome"] == "SIMPLE"
          and p2.t_end == T(7, 44, 4).isoformat()
          and p2.attempt.get("rpm_in") == 0 and p2.attempt.get("cranked") is None,
          f"1г: «Неудачный пуск · Стоп» (отсчётов внутри нет) до следующей попытки: {p2.t_end}")
    check(p4.t_end == T(10, 11, 45).isoformat() and p4.attempt["facts"]["rpm_max"] == 0.0
          and p4.attempt["facts"]["ready"] is False,
          f"1д: третья авария — до сброса 2 ч 26 мин, обороты 0: {p4.t_end} {p4.attempt}")
    check(all(not dets(p, "LIMIT_PROXIMITY") and not dets(p, "RPM_UNDERSPEED") for p in (p1, p2, p3, p4)),
          "1е: на плашках нет «давление масла 0» и «обороты 0»")
    check(any(d.fault_codes == [1438] for d in dets(p4, "CONTROLLER_FAULT")),
          f"1ж: отказ прокрутки с кодом панели 1438: {[d.fault_codes for d in dets(p4, 'CONTROLLER_FAULT')]}")

work = by.get("10:12:26")
if work:
    subs = work.subsegments
    check(not dets(work, "RPM_UNDERSPEED") and not dets(work, "LIMIT_PROXIMITY"),
          f"2: пробная работа — 750 об/мин за секунду до холостого хода не «просадка»: "
          f"{[(d.scenario, d.trigger) for sub in subs for d in sub.detections]}")
    check(subs[-1].transient and subs[-1].cause_open == "RPM_RUNDOWN"
          and subs[-1].t_start == T(10, 13, 1).isoformat() and not subs[0].transient,
          f"2б: сброс оборотов перед охлаждением — переходный хвост: "
          f"{[(s.t_start, s.transient, s.cause_open) for s in subs]}")
    check(work.attempt is None and not work.attempt_pending, "2в: подтверждённая работа — не попытка")
cool = by.get("10:13:02")
check(cool is not None and any(d.fault_codes == [1248] for d in dets(cool, "CONTROLLER_FAULT")),
      "2г: предупреждение двигателя несёт код 1248")

# ── Живое окно: попытка идёт → ждём подтверждения; вернулась в стоп → плашка ──
CTX = [Link(run_state=0, t_start=T(7, 40, 20), t_end=T(7, 40, 28))]
live = run(T(7, 40, 28), T(7, 40, 33), ctx=CTX)
check(len(live) == 1 and live[0].run_state == 3 and live[0].attempt_pending
      and lab(live[0]) == "Работа — ждём подтверждения"
      and all(s.transient for s in live[0].subsegments)
      and not dets(live[0], "LIMIT_PROXIMITY") and not dets(live[0], "RPM_UNDERSPEED"),
      f"3: попытка идёт — «ждём подтверждения», аналитики работы нет: {[(lab(s), s.attempt_pending) for s in live]}")
live2 = run(T(7, 40, 28), T(7, 41, 30), ctx=CTX)
# v5.1.25 (решение А): внутри попытки 07:40:28–37 отсчётов оборотов нет
check(len(live2) == 1 and live2[0].run_state == 0 and lab(live2[0]) == "Неудачный пуск · Стоп"
      and live2[0].cause_close is None,
      f"3б: вернулась в стоп — та же строка стала плашкой (без закрытия): {[(lab(s), s.cause_close) for s in live2]}")
live0 = run(T(7, 40, 28), T(7, 40, 33), ctx=None)
check(live0[0].attempt_pending is False, "3в: без контекста (режим до окна неизвестен) — не попытка")

# ── ДГУ №2 20.09: стартер крутит ~100 об/мин, мотор не схватил, без кода ──
E2 = [rs(0, T(12, 40, 0, 20), T(12, 51, 50, 20)), rs(2, T(12, 51, 50, 20), T(12, 51, 59, 20)),
      rs(0, T(12, 51, 59, 20))]
H2 = [h(RPM, T(12, 51, 20, 20), 0.0), h(RPM, T(12, 51, 53, 20), 103.0), h(RPM, T(12, 51, 58, 20), 0.0)]
s2 = run(T(12, 45, 0, 20), T(12, 55, 0, 20), enum=E2, hist=H2, faults=[])
check([lab(s) for s in s2] == ["Стоп", "Неудачный пуск · Стоп"],
      f"4: прокрутка без пуска — «Неудачный пуск · Стоп» (v5.1.23, В4): {[lab(s) for s in s2]}")

# ── Склейка: короткий неподтверждённый прогрев перед подтверждённой работой ──
E3 = [rs(0, T(9, 0), T(9, 10)), rs(2, T(9, 10), T(9, 10, 5)), rs(3, T(9, 10, 5), T(9, 20)), rs(0, T(9, 20))]
H3 = [h(RPM, T(9, 9, 30), 0.0), h(RPM, T(9, 10, 2), 120.0), h(RPM, T(9, 10, 8), 900.0),
      h(RPM, T(9, 10, 15), 1500.0), h(RPM, T(9, 19, 0), 1500.0)]
s3 = run(T(9, 5), T(9, 25), enum=E3, hist=H3, faults=[])
check([(lab(s), datetime.fromisoformat(s.t_start).strftime("%H:%M:%S")) for s in s3]
      == [("Стоп", "09:05:00"), ("Работа", "09:10:00"), ("Стоп", "09:20:00")],
      f"5: прогрев 5 с без оборотов — часть пуска «Работы»: {[(lab(s), s.t_start) for s in s3]}")
w3 = s3[1]
check(w3.subsegments[0].transient and w3.subsegments[0].t_end == T(9, 10, 15).isoformat()
      and not dets(w3, "RPM_UNDERSPEED"),
      f"5б: разгон до 1500 — переходный подсегмент, без «просадки»: "
      f"{[(s.t_start, s.t_end, s.transient) for s in w3.subsegments]}")

# ── Нет данных: ни оборотов, ни частоты — неудачу не объявляем ──
E4 = [rs(0, T(9, 0), T(9, 10)), rs(3, T(9, 10), T(9, 10, 20)), rs(0, T(9, 10, 20))]
s4 = run(T(9, 5), T(9, 15), enum=E4, hist=[h(CLT, T(9, 0), 40.0)], faults=[])
check([lab(s) for s in s4] == ["Стоп", "Работа", "Стоп"], f"6: нет данных — как раньше: {[lab(s) for s in s4]}")

# ── Якорь акта: стоп после неудачной попытки не эталон ──
rsp = [p for p in ENUM if p["addr"] in (40011, 40013)]
kb_faults = [f for f in FAULTS if f["addr"] in CFG.whitelist_fault]
a0, why0 = clf.find_baseline_anchor(rsp, kb_faults, T(7, 45, 26), CFG)
check(a0 == T(7, 40, 37) and why0 == "normal_stop", f"7: без проверки якорь — стоп после попытки: {a0}")
a1, why1 = clf.find_baseline_anchor(rsp, kb_faults, T(7, 45, 26), CFG, skip_stops={T(7, 40, 37)})
check(a1 == T(5, 16, 56), f"7б: стоп после попытки пропущен — якорь на утреннем останове: {a1}")
check(clf.exit_chain_before(rsp, T(7, 40, 37)) == (T(7, 40, 28), T(7, 40, 37)),
      f"7в: цепочка перед стопом: {clf.exit_chain_before(rsp, T(7, 40, 37))}")

inc = clf.build_stop_incident(ENUM, FAULTS, T(7, 45, 26), T(10, 11, 45), CFG, stop_kind="EMERGENCY",
                              anchor=(T(5, 16, 56), "normal_stop"), min_window_sec=12 * 3600,
                              attempt=p4.attempt if p4 else {"t_fall": "x"})
w = inc["window"]
check(w["from"] == (T(7, 45, 26) - timedelta(hours=12)).isoformat()
      and w["anchor"] == T(5, 16, 56).isoformat() and w["min_window_h"] == 12.0
      and inc["failure"] == "start" and inc["attempt"],
      f"8: окно ≥ 12 ч, якорь отдельно, отказ пуска: {w} {inc.get('failure')}")

# ── Движок: якорь с проверкой по оборотам ──
import online.engine as eng  # noqa: E402
import analytics.source as src  # noqa: E402

CALLS: dict = {"hist": []}


async def _enum(*a, addrs=None, **k):
    return [p for p in ENUM if addrs is None or p["addr"] in addrs]


async def _faults(*a, **k):
    return list(FAULTS)


async def _hist(r, e, p, t0, t1, addrs):
    CALLS["hist"].append((t0, t1))
    return [x for x in HIST if t0 <= x["ts"] < t1 and x["addr"] in addrs]


async def _gaps(*a, **k):
    return []


async def _tl(r, e, p, t0, t1, cfg):
    CALLS["tl_from"] = t0
    return list(ENUM), list(FAULTS)


src.get_enum_periods, src.get_fault_periods = _enum, _faults
src.get_whitelist_history, src.get_data_gaps, src.get_timeline_periods = _hist, _gaps, _tl
ctx = asyncio.run(eng._load_incident_context("SN", "pcc", 1, T(7, 45, 26), T(7, 47), CFG))
check(ctx is not None and ctx[3] == (T(5, 16, 56), "normal_stop"),
      f"9: якорь акта №3 — утренний останов (попытка проверена по оборотам): {ctx and ctx[3]}")
check(CALLS.get("tl_from") == T(7, 45, 26) - timedelta(hours=12) - timedelta(seconds=60),
      f"9б: данные акта — не меньше 12 ч: {CALLS.get('tl_from')}")
check(eng._stop_ts_of(p4) == T(7, 45, 26) if p4 else False, "9в: момент останова плашки — падение")
check(eng._attempt_stops(segs) == {T(7, 40, 11), T(7, 40, 37), T(7, 44, 13), T(7, 45, 26)},
      f"9г: стопы после попыток в окне: {eng._attempt_stops(segs)}")

# ── Разбор аварии: отказ пуска, якорь, предыдущие аварии серии — только факты ──
from online.incident_gate import build_incident_prompt  # noqa: E402

cyc = [{"run_state": s.run_state, "t_start": datetime.fromisoformat(s.t_start),
        "t_end": datetime.fromisoformat(s.t_end) if s.t_end else None,
        "characteristics_json": s.to_dict()} for s in segs
       if datetime.fromisoformat(s.t_start) < T(7, 45, 7)]
pr = build_incident_prompt(inc, None, cyc, {"code_name": lambda c: {1438: "Отказ прокрутки стартером"}.get(c)})
check(pr.startswith("Разбери отказ пуска"), f"10: заголовок — отказ пуска: {pr[:60]}")
# v5.1.22 (P1-6): время во входе — МСК (T — UTC: 05:16:56 → 08:16:56)
check("Последний штатный останов: 13.10 08:16:56" in pr and "Окно данных: с" in pr
      and "фон" in pr, "10б: якорь и окно ≥ 12 ч размечены")
ser = pr.split("Предыдущие аварии этой серии", 1)[-1].split("\n\n", 1)[0]
check("13.10 10:40:11 — Неудачный пуск · Авария; код: 1438 «Отказ прокрутки стартером»" in ser
      and "10:44:13 — Неудачный пуск · Авария" in ser and "сброшена через" in ser
      and "Коротко" not in ser, f"10в: предыдущие аварии серии — факты:\n{ser}")
check("Характер по детерминированному гейту" not in pr, "10г: гейт «падения с работы» для отказа пуска не печатается")

# ── Подписи ──
# v5.1.23 (В4): подпись по прокрутке; оборотов нет — «Неудачный пуск» (отмены не видно)
check(labels.attempt_label({"from_state": 1, "outcome": "SIMPLE", "facts": {"rpm_max": 0.0}}) == "Отменённый пуск · Стоп"
      and labels.attempt_label({"from_state": 1, "outcome": "SIMPLE"}) == "Неудачный пуск · Стоп"
      and labels.pending_label(2) == "Прогрев — ждём подтверждения", "11: подписи")

# ── Строка статуса: код панели и понятный текст ──
from online.status_assembler import build_structural_status, format_status_text as build_status_text  # noqa: E402


class _FR:
    def lookup(self, code):
        return {1248: {"verified": True, "description": {"ru": "Предупреждение двигателя — общее"}}}.get(code)


row = {"id": 1, "run_state": 0, "t_start": T(10, 14, 59), "characteristics_json": {},
       "current_values_json": {"values": {}},
       "active_detections_json": [{"scenario": "CONTROLLER_FAULT", "severity": "WARNING",
                                   "fault_codes": [1248],
                                   "trigger": "Предупреждение двигателя (addr=40406, bit=2)",
                                   "values": {"addr": 40406, "bit": 2,
                                              "fault_name": "Предупреждение двигателя"}}]}
st = build_structural_status(row, _FR())
txt = build_status_text(st)
check("код 1248 «Предупреждение двигателя — общее»" in txt and "addr=" not in txt,
      f"12: строка статуса — код и текст вместо addr/bit: {txt}")
row["active_detections_json"][0]["fault_codes"] = []
txt2 = build_status_text(build_structural_status(row, _FR()))
check("addr=" not in txt2 and "Предупреждение двигателя" in txt2, f"12б: без кода — имя бита: {txt2}")

# ── Заключение плашки — факты без модели ──
from corpus.worker import _attempt_conclusion  # noqa: E402

fc = _attempt_conclusion({"characteristics_json": p4.to_dict()}) if p4 else None
check(fc is not None and fc["claude_model"] == "Система (без ИИ)" and fc["verdict"] == "авария"
      and "1438" in fc["conclusion_md"], f"13: плашка — факты без модели: {fc and fc['humanized_md']}")
check(_attempt_conclusion({"characteristics_json": work.to_dict()}) is None if work else False,
      "13б: работа — обычное заключение")

# ══ Ревью v5.1.16 ══════════════════════════════════════════════════════════
# 14. Живой пуск через два состояния: опрос между ними — одно открытое звено
E5 = [rs(0, T(9, 0), T(9, 10)), rs(2, T(9, 10), T(9, 10, 5)), rs(3, T(9, 10, 5), T(9, 20)), rs(0, T(9, 20))]
H5 = [h(RPM, T(9, 9, 30), 0.0), h(RPM, T(9, 10, 2), 110.0), h(RPM, T(9, 10, 9), 900.0),
      h(RPM, T(9, 10, 15), 1500.0), h(RPM, T(9, 19, 0), 1500.0)]
CTX5 = [Link(run_state=0, t_start=T(9, 0), t_end=T(9, 10))]
l1 = run(T(9, 10), T(9, 10, 7), ctx=CTX5, enum=E5, hist=H5, faults=[])
check(len(l1) == 1 and l1[0].attempt_pending and l1[0].cause_close is None
      and l1[0].t_start == T(9, 10).isoformat(),
      f"14: исход не решён — одно открытое звено от начала цепочки: {[(lab(s), s.t_start, s.cause_close) for s in l1]}")
l2 = run(T(9, 10), T(9, 25), ctx=CTX5, enum=E5, hist=H5, faults=[])
r2 = run(T(9, 5), T(9, 25), enum=E5, hist=H5, faults=[])
check([(lab(s), s.t_start) for s in l2] == [(lab(s), s.t_start) for s in r2[1:]],
      f"14б: живой и перечитка совпадают: {[(lab(s), s.t_start) for s in l2]} / {[(lab(s), s.t_start) for s in r2]}")
H5f = [h(RPM, T(9, 9, 30), 0.0), h(RPM, T(9, 10, 2), 110.0), h(RPM, T(9, 10, 30), 0.0)]
l3 = run(T(9, 10), T(9, 25), ctx=CTX5, enum=E5[:2] + [rs(3, T(9, 10, 5), T(9, 10, 40)), rs(0, T(9, 10, 40))],
         hist=H5f, faults=[])
check([lab(s) for s in l3] == ["Неудачный пуск · Стоп"] and l3[0].attempt["path"] == [2, 3],
      f"14в: 0→2→3→0 без вращения — одна плашка: {[lab(s) for s in l3]}")

# 15. Задержка пуска — свой сегмент, выход из стопа считается через неё
E6 = [rs(0, T(9, 0), T(9, 10)), rs(1, T(9, 10), T(9, 10, 30)), rs(3, T(9, 10, 30), T(9, 10, 50)), rs(0, T(9, 10, 50))]
H6 = [h(RPM, T(9, 9, 30), 0.0), h(RPM, T(9, 10, 40), 90.0)]
r6 = run(T(9, 5), T(9, 15), enum=E6, hist=H6, faults=[])
check([lab(s) for s in r6] == ["Стоп", "1", "Неудачный пуск · Стоп"] or
      [lab(s) for s in r6] == ["Стоп", None, "Неудачный пуск · Стоп"],
      f"15: задержка пуска своим сегментом, плашка — с «Работы»: {[(s.run_state, lab(s)) for s in r6]}")
CTX6 = [Link(run_state=0, t_start=T(9, 0), t_end=T(9, 10)), Link(run_state=1, t_start=T(9, 10), t_end=T(9, 10, 30))]
l6 = run(T(9, 10, 30), T(9, 15), ctx=CTX6, enum=E6, hist=H6, faults=[])
check([lab(s) for s in l6] == ["Неудачный пуск · Стоп"],
      f"15б: живой после задержки пуска — плашка (контекст без задержки): {[lab(s) for s in l6]}")

# 16. Суточный рез: попытка за 5 с до 09:00 решается по данным за границей
E7 = [rs(0, T(8, 0), T(8, 59, 55)), rs(3, T(8, 59, 55), T(10, 0)), rs(0, T(10, 0))]
H7 = [h(RPM, T(8, 59, 30), 0.0), h(RPM, T(9, 0, 3), 1200.0), h(RPM, T(9, 0, 10), 1500.0)]
cut = segment([p for p in E7 if p["state_start"] < T(9, 0)], H7, [], [], CFG, "SN", "pcc", 1, "E",
              T(8, 30), T(9, 0), confirm_to=T(9, 30))
check(not cut[-1].attempt_pending and cut[-1].run_state == 3,
      f"16: подтверждение за границей — рез как у работы: {[(lab(s), s.attempt_pending) for s in cut]}")
cut0 = segment([p for p in E7 if p["state_start"] < T(9, 0)], H7, [], [], CFG, "SN", "pcc", 1, "E",
               T(8, 30), T(9, 0))
check(cut0[-1].attempt_pending, "16б: без взгляда за границу — ждём подтверждения (рез переносится)")

# 17. Акт отказа пуска: «откуда остановилась» — эта попытка, не прошлая плашка
cyc_open = [c for c in cyc if c["t_start"] < T(7, 45, 7)]   # своя плашка ещё открыта
pr3 = build_incident_prompt(inc, None, cyc_open, {})
blk = [ln for ln in pr3.splitlines() if ln.startswith("Сегмент, из которого машина остановилась")]
# v5.1.22 (P1-6): время — МСК
check(blk and "10:45:07" in blk[0] and "10:44:04" not in blk[0],
      f"17: сегмент падения — эта попытка: {blk}")

# 18. Серия: продолжение после суточного реза — не новая авария; коды — свои
def _row(rs_, t0, t1, chars):
    return {"run_state": rs_, "t_start": t0, "t_end": t1, "characteristics_json": chars}


def _lfc(*vals):
    return [{"characteristics": {"LAST_FAULT_CODE": {"value_start": vals[0], "max": max(vals),
                                                     "value_end": vals[-1]}}}]


from online.incident_gate import series_facts  # noqa: E402
rows = [
    _row(3, T(18, 0, d=12), T(20, 30, d=12), {"subsegments": []}),
    _row(0, T(20, 30, d=12), T(21, 0, d=12), {"stop_kind": "EMERGENCY", "cause_close": "DAILY_BOUNDARY",
                                               "subsegments": _lfc(1438)}),
    _row(0, T(21, 0, d=12), T(23, 10, d=12), {"stop_kind": "EMERGENCY", "cause_close": "SHUTDOWN_CLEARED",
                                               "cause_open": "REPORT_START", "subsegments": _lfc(1438, 0)}),
]
sf = series_facts(rows, T(1, 0), T(17, 0, d=12))
# v5.1.22 (P1-6): время серии — МСК с датой (12.10 20:30 UTC → 12.10 23:30)
check(len(sf) == 1 and "12.10 23:30:00" in sf[0] and "сброшена через 2 ч" in sf[0] and "падение с работы" in sf[0],
      f"18: продолжение после реза не стало второй аварией: {sf}")
plate_ch = {"stop_kind": "EMERGENCY", "cause_close": "SHUTDOWN_CLEARED",
            "attempt": {"from_state": 3, "outcome": "EMERGENCY", "t_start": T(10, 20).isoformat(),
                        "t_fall": T(10, 20, 9).isoformat(), "codes": [1438], "facts": {}},
            "subsegments": _lfc(1248, 1438, 0)}
sf2 = series_facts([_row(0, T(10, 20), T(10, 30), plate_ch)], T(11, 0), T(9, 0))
check(sf2 and "1438" in sf2[0] and "1248" not in sf2[0], f"18б: код, висевший до попытки, не её: {sf2}")
rows3 = [_row(2, T(9, 0), T(9, 5), {"subsegments": []}),
         _row(0, T(9, 5), T(9, 8), {"stop_kind": "EMERGENCY", "cause_close": "SHUTDOWN_CLEARED",
                                     "subsegments": _lfc(1452)})]
sf3 = series_facts(rows3, T(9, 30), T(8, 0))
check(sf3 and "из режима «Прогрев»" in sf3[0] and "падение с работы" not in sf3[0],
      f"18в: авария из прогрева — не «падение с работы»: {sf3}")

# 19. Код панели: сосед, поднявшийся вместе и снятый в прошлом подсегменте, — код не его
from analytics.detectors import _detect_controller_faults  # noqa: E402
_fa = fp(40404, 3, "warning", T(9, 0), T(9, 1), "A")
_fb = fp(40406, 2, "warning", T(9, 0), None, "B")
_lfc_p = {CFG.role_to_addr("LAST_FAULT_CODE"): [en(40012, 151, T(9, 0))]}
d19 = _detect_controller_faults([_fb], {}, T(9, 5), T(9, 10), CFG, _lfc_p, onset_faults=[_fa, _fb])
check(d19 and d19[0].fault_codes == [], f"19: код соседа не приписан: {[d.fault_codes for d in d19]}")

# ══ Ревью-2 v5.1.16 ═════════════════════════════════════════════════════════
# 20. Неудачная попытка через суточный рез: голова — «Авария», как и при перечитке
E8 = [rs(0, T(8, 0), T(8, 59, 30)), rs(3, T(8, 59, 30), T(9, 0, 20)), rs(0, T(9, 0, 20))]
EN8 = E8 + [en(40013, 4, T(9, 0, 20)), en(40012, 1438, T(9, 0, 20))]
F8 = [fp(40407, 8, "shutdown", T(9, 0, 20), None, "Отказ прокрутки стартером")]
H8 = [h(RPM, T(8, 59, 10), 0.0), h(RPM, T(9, 0, 10), 0.0)]
head8 = segment([p for p in EN8 if p["state_start"] < T(9, 0, 40)], H8, F8, [], CFG, "SN", "pcc", 1, "E",
                T(8, 30), T(9, 0), confirm_to=T(9, 0, 40))
check(lab(head8[-1]) == "Неудачный пуск · Авария" and head8[-1].stop_kind == "EMERGENCY"
      and head8[-1].attempt["codes"] == [1438],
      f"20: голова плашки за резом — авария: {[(lab(s), s.stop_kind) for s in head8]}")
CTX8 = [Link(run_state=0, t_start=T(8, 59, 30), t_end=T(9, 0))]
cont8 = run(T(9, 0), T(9, 30), ctx=CTX8, enum=EN8, hist=H8, faults=F8)
check([lab(s) for s in cont8] == ["Неудачный пуск · Авария"]
      and cont8[0].attempt["t_fall"] == T(9, 0, 20).isoformat(),
      f"20б: продолжение — та же плашка, падение в нём: {[(lab(s), s.attempt) for s in cont8]}")

# 21. Пуск подтверждён после 09:00, прогрев склеен: продолжение — «Работа», без «Прогрева»
E9 = [rs(0, T(8, 0), T(8, 59, 50)), rs(2, T(8, 59, 50), T(9, 0, 5)), rs(3, T(9, 0, 5), T(10, 0)), rs(0, T(10, 0))]
H9 = [h(RPM, T(8, 59, 30), 0.0), h(RPM, T(8, 59, 55), 150.0), h(RPM, T(9, 0, 8), 1500.0),
      h(RPM, T(9, 30), 1500.0)]
head9 = segment([p for p in E9 if p["state_start"] < T(9, 0, 40)], H9, [], [], CFG, "SN", "pcc", 1, "E",
                T(8, 30), T(9, 0), confirm_to=T(9, 0, 40))
check(lab(head9[-1]) == "Работа" and head9[-1].t_start == T(8, 59, 50).isoformat()
      and not head9[-1].attempt_pending, f"21: голова — склеенная «Работа»: {[(lab(s), s.t_start) for s in head9]}")
CTX9 = [Link(run_state=3, t_start=T(8, 59, 50), t_end=T(9, 0))]
cont9 = run(T(9, 0), T(9, 40), ctx=CTX9, enum=E9, hist=H9, faults=[])
check([(lab(s), s.t_start) for s in cont9] == [("Работа", T(9, 0).isoformat())],
      f"21б: продолжение — «Работа» с 09:00, без «Прогрева»: {[(lab(s), s.t_start) for s in cont9]}")

# 22. Хвост после снятия аварии: предупреждение не получает код аварии
F10 = [fp(40406, 2, "warning", T(9, 0), None, "Предупреждение двигателя")]   # авария снята до окна
d22 = _detect_controller_faults(F10, {}, T(9, 10), T(9, 20), CFG,
                                {CFG.role_to_addr("LAST_FAULT_CODE"): [en(40012, 1438, T(9, 0))]},
                                onset_faults=F10)
check(d22 and d22[0].fault_codes == [], f"22: код аварии 1438 не приписан предупреждению: {[d.fault_codes for d in d22]}")
d22b = _detect_controller_faults(F10, {}, T(9, 10), T(9, 20), CFG,
                                 {CFG.role_to_addr("LAST_FAULT_CODE"): [en(40012, 1248, T(9, 0))]},
                                 onset_faults=F10)
check(d22b and d22b[0].fault_codes == [1248], f"22б: свой код предупреждения — приписан: {[d.fault_codes for d in d22b]}")

# 23. Тяжесть кода — по уровню: предупреждение по маслу 40403/14 ↔ 421 («de»)
check(CFG.code_fits_bit(421, "warning") and not CFG.code_fits_bit(1438, "warning")
      and CFG.code_fits_bit(1438, "shutdown") and CFG.code_fits_bit(1248, "warning"),
      "23: совместимость кода и бита — по уровню тяжести")

if _errors:
    print(f"FAIL — {len(_errors)} расхождений:")
    for x in _errors:
        print(f"  • {x}")
    sys.exit(1)
print("Все проверки пройдены.")
