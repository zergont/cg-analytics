# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""Одиночный ноль источника (v5.1.17, решение владельца 13.10.2026).

Декодер иногда пишет «нулевой кадр»: обороты и/или давление масла ровно 0
одной точкой посреди работы, бывает вместе с током и мощностью. ДГУ №1 12.10
14:09:17 МСК: обороты 1499 → 0 → 1501 при частоте 50 Гц; ДЭС №3 13.10
14:59:56: давление масла 300 → 0 → 316 при 1515 об/мин. Такой ноль — сбой
источника, в аналитику не идёт; настоящее падение (нули подряд, смена режима
в стоп) — идёт.

Запуск:  py -3 scripts/test_zero_glitch_v5117.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analytics.config import AnalyticsConfig  # noqa: E402
from analytics.segmenter import _drop_zero_glitches, segment  # noqa: E402

_errors: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        _errors.append(msg)


KB = ROOT / "knowledge_base"
CFG = AnalyticsConfig.from_pair(KB, "pcc3300", "cummins_kta50")
RPM, FREQ, OIL, KW, CUR, OILT = 40068, 40044, 40062, 40034, 40029, 40063


def T(h, m, s=0, d=12):
    return datetime(2026, 10, d, h, m, s, tzinfo=timezone.utc)


def rs(v, a, b=None):
    return {"addr": 40011, "value": v, "state_start": a, "state_end": b}


def h(addr, ts, v):
    return {"addr": addr, "ts": ts, "value": v}


def run(enum, hist, a, b):
    return segment(enum, sorted(hist, key=lambda r: r["ts"]), [], [], CFG, "SN", "pcc", 1, "E", a, b)


def dets(segs, scen):
    return [(d.trigger) for s in segs for sub in s.subsegments for d in sub.detections if d.scenario == scen]


def chars_min(segs, role):
    vals = [sub.characteristics.get(role, {}).get("min") for s in segs if s.run_state == 3
            for sub in s.subsegments if not sub.transient]
    vals = [v for v in vals if v is not None]
    return min(vals) if vals else None


# ── 1. ДГУ №1 12.10: обороты 1499 → 0 → 1501 за 9 с ──
E1 = [rs(0, T(10, 0), T(11, 9, 2)), rs(3, T(11, 9, 2))]
H1 = [h(RPM, T(11, 8, 2), 0.0), h(RPM, T(11, 9, 2), 333.0), h(RPM, T(11, 9, 13), 1499.0),
      h(RPM, T(11, 9, 17), 0.0), h(RPM, T(11, 9, 22), 1501.0), h(RPM, T(11, 15), 1500.0),
      h(FREQ, T(11, 9, 13), 50.0), h(FREQ, T(11, 9, 22), 50.1),
      h(OIL, T(11, 9, 10), 600.0), h(OIL, T(11, 14), 590.0), h(OILT, T(11, 0), 60.0)]
s1 = run(E1, H1, T(11, 0), T(11, 20))
check(not any("RPM мин. 0" in t for t in dets(s1, "RPM_UNDERSPEED")),
      f"1: «обороты 0» нулевого кадра не срабатывают: {dets(s1, 'RPM_UNDERSPEED')}")
check(chars_min(s1, "RPM") and chars_min(s1, "RPM") > 1000, f"1б: минимум оборотов без нуля: {chars_min(s1, 'RPM')}")
gc = [c for s in s1 for c in s.sequence_checks if c.get("check") == "source_zero_glitch"]
check(gc and gc[0]["count"] == 1 and "обороты 11:09:17" in gc[0]["details"],
      f"1в: сбой учтён в проверках сегмента: {gc}")

# ── 2. ДЭС №3 13.10: давление масла 300 → 0 → 316 при 1515 об/мин ──
CFG3 = AnalyticsConfig.from_pair(KB, "pcc3300", "cummins_qsx15")
E2 = [rs(0, T(10, 0, d=13), T(11, 59, 49, d=13)), rs(3, T(11, 59, 49, d=13))]
H2 = [h(RPM, T(11, 59, 26, d=13), 0.0), h(OIL, T(11, 58, 56, d=13), 0.0),
      h(RPM, T(11, 59, 49, d=13), 1515.0), h(OIL, T(11, 59, 49, d=13), 300.0), h(FREQ, T(11, 59, 49, d=13), 50.1),
      h(OIL, T(11, 59, 56, d=13), 0.0), h(OIL, T(11, 59, 58, d=13), 316.0), h(RPM, T(11, 59, 58, d=13), 1496.0),
      h(OIL, T(12, 0, 16, d=13), 308.0), h(RPM, T(12, 5, d=13), 1497.0), h(OIL, T(12, 5, d=13), 300.0),
      h(OILT, T(11, 50, d=13), 60.0)]
s2 = segment(E2, sorted(H2, key=lambda r: r["ts"]), [], [], CFG3, "SN", "pcc", 1, "E",
             T(11, 50, d=13), T(12, 10, d=13))
check(not any("Давление масла: 0.00" in t for t in dets(s2, "LIMIT_PROXIMITY")),
      f"2: «давление масла 0 у порога» нет: {dets(s2, 'LIMIT_PROXIMITY')}")
check(not dets(s2, "OIL_DILUTION"), f"2б: ложного падения давления масла нет: {dets(s2, 'OIL_DILUTION')}")

# ── 3. Нулевой кадр: обороты, давление, мощность, ток — уходят вместе ──
tz0 = T(8, 33, 12, d=14)
H3 = {RPM: [h(RPM, tz0 - timedelta(seconds=7), 1499.0), h(RPM, tz0, 0.0), h(RPM, tz0 + timedelta(seconds=22), 1500.0)],
      OIL: [h(OIL, tz0 - timedelta(seconds=7), 516.0), h(OIL, tz0, 0.0), h(OIL, tz0 + timedelta(seconds=22), 508.0)],
      KW: [h(KW, tz0 - timedelta(seconds=30), 600.0), h(KW, tz0, 0.0), h(KW, tz0 + timedelta(seconds=30), 590.0),
           h(KW, tz0 + timedelta(minutes=5), 0.0)],
      CUR: [h(CUR, tz0 - timedelta(seconds=30), 1000.0), h(CUR, tz0, 0.0), h(CUR, tz0 + timedelta(seconds=30), 990.0)]}
out3, g3 = _drop_zero_glitches(H3, CFG, tz0 + timedelta(minutes=10))
check(all(r["ts"] != tz0 for a in (RPM, OIL, KW, CUR) for r in out3[a]),
      f"3: нулевой кадр ушёл целиком: {[(a, [r['value'] for r in out3[a]]) for a in (RPM, OIL, KW, CUR)]}")
check(any(r["ts"] == tz0 + timedelta(minutes=5) for r in out3[KW]),
      "3б: ноль мощности вне сбойного кадра — настоящий (сброс нагрузки), остаётся")
check(sorted(r for _, r in g3) == ["OIL_PRESS", "RPM"], f"3в: сбой по обеим ролям: {g3}")

# ── 4. Настоящий останов: 1500 → 0 со сменой режима в стоп — не сбой ──
t4 = T(9, 0)
H4 = {RPM: [h(RPM, t4 - timedelta(seconds=9), 1500.0), h(RPM, t4, 0.0), h(RPM, t4 + timedelta(seconds=20), 1500.0)]}
out4, g4 = _drop_zero_glitches(H4, CFG, t4 + timedelta(minutes=5), [(t4 + timedelta(seconds=2), t4 + timedelta(minutes=1))])
check(not g4 and len(out4[RPM]) == 3, f"4: ноль у смены режима в стоп — настоящий: {g4}")

# ── 5. Ноль держится (отказ датчика давления без смены режима) — настоящий ──
H5 = {OIL: [h(OIL, t4, 300.0), h(OIL, t4 + timedelta(seconds=10), 0.0), h(OIL, t4 + timedelta(seconds=20), 0.0),
            h(OIL, t4 + timedelta(seconds=80), 0.0)]}
out5, g5 = _drop_zero_glitches(H5, CFG, t4 + timedelta(minutes=5))
check(not g5 and len(out5[OIL]) == 4, f"5: нули подряд не трогаем: {g5}")

# ── 6. Край живого окна: ноль последней точкой — ждём; через минуту без смены — настоящий ──
H6 = {RPM: [h(RPM, t4, 1500.0), h(RPM, t4 + timedelta(seconds=9), 0.0)]}
_, g6 = _drop_zero_glitches(H6, CFG, t4 + timedelta(seconds=20))
check(len(g6) == 1, "6: ноль на краю окна — пока сбой (ждём следующего показания)")
_, g6b = _drop_zero_glitches(H6, CFG, t4 + timedelta(seconds=80))
check(not g6b, "6б: показаний нет больше минуты — ноль держится, настоящий")

# ── 7. Выключатель KB ──
check(CFG.seg("zero_glitch", "enabled") is True and CFG.seg("zero_glitch", "roles", "RPM") == 1000,
      "7: параметры в KB")

if _errors:
    print(f"FAIL — {len(_errors)} расхождений:")
    for x in _errors:
        print(f"  • {x}")
    sys.exit(1)
print("Все проверки пройдены.")
