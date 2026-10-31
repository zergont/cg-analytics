# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""v5.1.30 «По событию» (docs/plans/design_v5123_plus.md, §2.3; решения
владельца В-27.1, В-27.2, В-30.1). Без БД, без шлюза и LLM.

1. Ряд I₂ по пакетам (В-27.1, «непрерывно ≥ 60 с»):
   1а одна точка 6,56 % перед паузой 117 с (ДЭС №3 09.10) — эпизода нет;
   1б переходный кадр и нулевой ток фаз (ДГУ №2 27.09) — эпизода нет;
   1в дыра data_gaps внутри участка — время дыры не непрерывность;
   1г кластер 14 участков (ДГУ №1 12.10): эпизод от начала до конца кластера,
      детекция во всех подсегментах, которые пересекают участки, одна t_detected;
      ряд фаз строится один раз, метрики подсегментов берут его срез;
   1д три коротких кластера — ничего;
   1е метрики подсегмента: длительность — непрерывный участок, не сумма;
      live=None — путь по метрикам;
   1ж открытое окно: идущий участок квалифицируется, когда дотянет до 60 с.
2. Перекос и нейтраль (29.09): 31 участок в одном кластере, pct_max и run_sec
   из кластера, ключи values прежние, I₂ при провале фазы молчит.
3. NO_LOAD_TOO_LONG (В-27.2): отрезок, кончившийся внутри подсегмента LOW
   (регресс 12.10 15:18 и 18:17); начало — +900 с; допуск 60 с; дыра рвёт;
   перенос 870 с через рез (начало t0 + 30 с), перенос короткого выхода;
   режим не 3; выход на краю окна.
4. Устойчивость: сбой ряда фаз — три ключа None, детекторы по подсегменту;
   сбой «без нагрузки» — по аккумулятору; соседи не задеты.
5. Движок с подменой БД: точный эпизод I₂ от начала кластера; запрет снятия
   (несимметрия в составе) — пока кластер жив и ещё 600 с; закрытие по концу.
6. В-30.1: разрыв пустого окна — с первого цикла; кусок [prev_base, t_from) —
   не живое время; полный путь _update_open_window (подмена источников и
   online_db): сценарии 29.10 S1–S7 — ни одного закрытия (S4 и S7 раньше
   закрывались «вслепую»), три суточных реза многосуточного обрыва — ни одного;
   связь вернулась — дебаунс закрывает как раньше.

Запуск:  py -3 scripts/test_events_v5130.py
"""
from __future__ import annotations

import asyncio
import logging
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import analytics.live as _lv  # noqa: E402
from analytics.config import AnalyticsConfig  # noqa: E402
from analytics.contract import CokingRisk, DerivedMetrics, RiskAccumulators  # noqa: E402
from analytics.detectors import run_all_detectors  # noqa: E402
from analytics.live import alive_detections, detection_live, segment_spans  # noqa: E402
from analytics.metrics import _compute_neg_seq_i2, compute_derived_metrics, nominal_current  # noqa: E402
from analytics.segmenter import segment  # noqa: E402

_errors: list[str] = []
MSK = ZoneInfo("Europe/Moscow")
U = timezone.utc


def check(cond: bool, msg: str) -> None:
    if not cond:
        _errors.append(msg)


class _LogCatch(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


LOG = _LogCatch()
for _name in ("analytics", "online"):
    logging.getLogger(_name).addHandler(LOG)
    logging.getLogger(_name).propagate = False


def logged(text: str) -> bool:
    return any(text in r.getMessage() for r in LOG.records)


KB = ROOT / "knowledge_base"
CFG = AnalyticsConfig.from_pair(KB, "pcc3300", "cummins_kta50")     # ДГУ №1 / №2
CFG3 = AnalyticsConfig.from_pair(KB, "pcc3300", "cummins_qsx15")    # ДЭС №3
HB = int(CFG.seg("data_quality", "heartbeat_addr"))
RPM, FREQ = CFG.role_to_addr("RPM"), CFG.role_to_addr("FREQUENCY")
IA = [CFG.role_to_addr(f"CURRENT_L{k}") for k in (1, 2, 3)]
PA = [CFG.role_to_addr(f"ACTIVE_POWER_L{k}") for k in (1, 2, 3)]
QA = [CFG.role_to_addr(f"REACTIVE_POWER_L{k}") for k in (1, 2, 3)]
RKW, LOAD = CFG.role_to_addr("RATING_KW"), CFG.role_to_addr("LOAD_PCT")
I_NOM = nominal_current(CFG, [1000.0])
L0 = datetime(2026, 10, 12, 17, 0, tzinfo=U)     # 12.10 20:00 МСК
I2K, SPK, NEK, NLK = "NEGATIVE_SEQUENCE", "PHASE_CURRENT_SPREAD", "NEUTRAL_CURRENT", "NO_LOAD_TOO_LONG"


def S(t: datetime, sec: float) -> datetime:
    return t + timedelta(seconds=sec)


def M(d, h, m, s=0):
    return datetime(2026, 10, d, h, m, s, tzinfo=MSK).astimezone(U)


def h(addr, ts, v):
    return {"addr": addr, "ts": ts, "value": v}


def rs(v, a, b=None):
    return {"addr": 40011, "value": v, "state_start": a, "state_end": b}


def ang(i, phi):
    """Токи фаз и углы → (I, P, Q): P = k·I·cos φ, Q = k·I·sin φ."""
    return (tuple(float(x) for x in i), tuple(x * 0.23 * math.cos(p) for x, p in zip(i, phi)),
            tuple(x * 0.23 * math.sin(p) for x, p in zip(i, phi)))


def delta_for(pct: float, i: float = 1000.0) -> float:
    """Сдвиг угла третьей фазы, дающий I₂ = pct % I_ном при равных токах i:
    I₂ = 2·i·sin(δ/2)/3."""
    return 2 * math.asin(3 * pct / 100 * I_NOM / (2 * i))


NORM = ang((1000, 1000, 1000), (0.6, 0.6, 0.6))


def I2F(pct: float):
    return ang((1000, 1000, 1000), (0.6, 0.6, 0.6 + delta_for(pct)))


ZERO = ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (0.0, 0.0, 0.0))
# 29.09 (Сининда): медианы ДГУ №1 при провале L3 — I₂ остаётся ниже 6 %
TRIP = ((932.5, 905.0, 93.5), (157.0, 206.0, 11.0), (140.0, -13.0, -18.0))

# Сверка генератора картинок с формулой детектора
_i, _p, _q = I2F(7.0)
_v = _compute_neg_seq_i2(_i[0], _p[0], _q[0], _i[1], _p[1], _q[1], _i[2], _p[2], _q[2]) / I_NOM * 100
check(abs(_v - 7.0) < 1e-6, f"0: генератор I₂ = 7 % даёт {_v}")


def rows_for(a, b, phase=None, load=None, pre=300, step=10, skip=None):
    """Строки, как их пишет роутер: пакет каждые step с (пинг, обороты, частота,
    токи, P, Q фаз, нагрузка), номинальная мощность — раз в 5 мин. phase(s) /
    load(s) / skip(s) — от секунды s относительно a (skip — пакета нет)."""
    phase = phase or (lambda s: NORM)
    load = load or (lambda s: 50.0)
    skip = skip or (lambda s: False)
    out, k, s = [], 0, -pre
    while S(a, s) < b:
        if not skip(s):
            t = S(a, s)
            out += [h(HB, t, float(k)), h(RPM, t, 1500.0 + (k % 2)), h(FREQ, t, 50.0)]
            i, p, q = phase(s)
            for j in range(3):
                out += [h(IA[j], t, i[j]), h(PA[j], t, p[j]), h(QA[j], t, q[j])]
            lv = load(s)
            if lv is not None:
                out.append(h(LOAD, t, float(lv)))
            if (s + pre) % 300 == 0:
                out.append(h(RKW, t, 1000.0))
        s += step
        k += 1
    return sorted(out, key=lambda r: r["ts"])


def by_addr(rows):
    out: dict[int, list[dict]] = {}
    for r in sorted(rows, key=lambda r: r["ts"]):
        out.setdefault(r["addr"], []).append(r)
    return out


def seg(rows, a, b, gaps=(), cfg=CFG, cr=None, seed=None, live=False, enum=None):
    return segment(enum or [rs(3, S(a, -3600))], rows, [], list(gaps), cfg, "SN", "pcc", 1, "E", a, b,
                   initial_coking_risk=cr, load_seed=seed, live_edge=live)


def dets(segs, sc):
    return [(sub, d) for s in segs for sub in s.subsegments for d in sub.detections
            if d.scenario == sc and not sub.transient]


def clusters(segs, sc):
    seen, out = set(), []
    for _sub, d in dets(segs, sc):
        for c in ((d.values or {}).get("live") or {}).get("clusters") or []:
            if c["start"] not in seen:
                seen.add(c["start"])
                out.append(c)
    return sorted(out, key=lambda c: c["start"])


def between(spans_sec: list[tuple[float, float]], f_in, f_out=None):
    """Картина по секундам: внутри одного из [a, b) — f_in, иначе f_out."""
    def fn(s):
        return f_in if any(a <= s < b for a, b in spans_sec) else (f_out or NORM)
    return fn


def gap(a, b):
    return {"gap_start": a, "gap_end": b}


def iso(t):
    return t.isoformat()


# ══ 1. Ряд I₂ по пакетам ═════════════════════════════════════════════════════

# ── 1а. Одна точка 6,56 % перед паузой 117 с (ДЭС №3 09.10: прежний ряд — «117 с») ──
B1 = S(L0, 1800)
_r1a = rows_for(L0, B1, phase=between([(600, 610)], I2F(6.56)), skip=lambda s: 600 < s < 717)
_s1a = seg(_r1a, L0, B1)
check(not dets(_s1a, I2K), f"1а: одна точка перед паузой — эпизода нет: {[d.trigger for _, d in dets(_s1a, I2K)]}")
_sp1a = segment_spans(by_addr(_r1a), None, CFG, L0, B1, [], {}, False)
check(I2K not in _sp1a, f"1а: короткий кластер отброшен: {_sp1a.get(I2K)}")
_dm1a = compute_derived_metrics(by_addr(_r1a), L0, B1, [], CFG)
check(_dm1a.neg_seq_i2_duration_sec is None or _dm1a.neg_seq_i2_duration_sec < 60,
      f"1а: метрика подсегмента — интервал через паузу не засчитан: {_dm1a.neg_seq_i2_duration_sec}")

# ── 1б. Переходный кадр (I₂ ≈ 10 %), затем все токи 0 (ДГУ №2 27.09: «780 с») ──
_r1b = rows_for(L0, B1, phase=lambda s: I2F(10.0) if s == 600 else (ZERO if 610 <= s < 1390 else NORM))
_s1b = seg(_r1b, L0, B1)
check(not dets(_s1b, I2K), "1б: переходный кадр и нулевой ток фаз — эпизода нет")
_dm1b = compute_derived_metrics(by_addr(_r1b), L0, B1, [], CFG)
check((_dm1b.neg_seq_i2_duration_sec or 0) <= 10,
      f"1б: нулевой ток фазы не нарушение и рвёт участок: {_dm1b.neg_seq_i2_duration_sec}")

# ── 1в. Дыра data_gaps внутри участка: время дыры — не непрерывность ──
# 7 % на [300, 350] и [420, 450], дыра [350, 420] без пакетов: 160 с подряд астрономически,
# непрерывно — 50 с
_g1v = [gap(S(L0, 350), S(L0, 420))]
_r1v = rows_for(L0, B1, phase=between([(300, 360), (420, 460)], I2F(7.0)), skip=lambda s: 350 < s < 420)
_sp1v = segment_spans(by_addr(_r1v), None, CFG, L0, B1, _g1v, {}, False)
check(I2K not in _sp1v, f"1в: участок через дыру 50 + 40 с — не эпизод: {_sp1v.get(I2K)}")
# Вторая часть 80 с — эпизод; слепая доля — дыра
_r1v2 = rows_for(L0, B1, phase=between([(300, 360), (420, 500)], I2F(7.0)), skip=lambda s: 350 < s < 420)
_cl1v = (segment_spans(by_addr(_r1v2), None, CFG, L0, B1, _g1v, {}, False).get(I2K) or [])
check(len(_cl1v) == 1 and _cl1v[0]["longest_sec"] == 80.0 and _cl1v[0]["blind_sec"] == 70.0
      and _cl1v[0]["start"] == iso(S(L0, 300)),
      f"1в: часть 80 с после дыры — эпизод, непрерывно 80 с, слепая доля 70 с: {_cl1v}")
_lf = _lv._longest_free
_GL = [(S(L0, 10), S(L0, 20)), (S(L0, 50), S(L0, 55))]
check((_lf(L0, S(L0, 100), []), _lf(L0, S(L0, 100), _GL), _lf(S(L0, 15), S(L0, 40), _GL),
       _lf(S(L0, 12), S(L0, 18), _GL), _lf(S(L0, 52), S(L0, 90), _GL)) == (100.0, 45.0, 20.0, 0.0, 35.0),
      "1в: самый длинный кусок без дыр — края, дыра на старте, участок внутри дыры")
_dm1v = compute_derived_metrics(by_addr(_r1v2), L0, B1, _g1v, CFG)
check(_dm1v.neg_seq_i2_duration_sec == 80.0,
      f"1в: метрика — интервал через дыру не засчитан: {_dm1v.neg_seq_i2_duration_sec}")

# ── 1г. Кластер 14 участков (ДГУ №1 12.10 23:15–23:41, непрерывно до 89 с) ──
_D14 = [20, 30, 90, 10, 20, 40, 10, 30, 20, 10, 40, 20, 30, 20]     # 390 с чистого
_iv14, _t = [], 900
for _k, _d in enumerate(_D14):
    _iv14.append((_t, _t + _d))
    _t += _d + (30 if _k % 3 else 120)
B14 = S(L0, 5400)
# Нагрузка 50 → 85 → 50 %: смена зоны режет подсегменты посреди кластера
_ld14 = (lambda s: 85.0 if 1400 <= s < 1900 else 50.0)
_r14 = rows_for(L0, B14, phase=between(_iv14, I2F(6.9)), load=_ld14)
_s14 = seg(_r14, L0, B14)
_c14 = clusters(_s14, I2K)
check(len(_c14) == 1 and _c14[0]["spans"] == 14 and _c14[0]["longest_sec"] == 90.0
      and _c14[0]["viol_sec"] == 390.0 and _c14[0]["visible_sec"] == 390.0
      and _c14[0]["start"] == iso(S(L0, _iv14[0][0])) and _c14[0]["end"] == iso(S(L0, _iv14[-1][1])),
      f"1г: один кластер 14 участков, чистое 390 с, непрерывно 90 с: {_c14}")
_d14 = dets(_s14, I2K)
_subs_hit = {sub.id for sub, _ in _d14}
_subs_meet = {sub.id for s in _s14 for sub in s.subsegments if not sub.transient
              and any(datetime.fromisoformat(sub.t_start) < S(L0, b) and S(L0, a) < datetime.fromisoformat(sub.t_end)
                      for a, b in _iv14)}
check(len(_subs_hit) >= 2 and _subs_hit == _subs_meet,
      f"1г: детекция во всех подсегментах, которые пересекают участки: {_subs_hit} / {_subs_meet}")
check({d.t_detected for _, d in _d14} == {iso(S(L0, _iv14[0][0]))},
      f"1г: t_detected — начало кластера, одно во всех подсегментах: {[d.t_detected for _, d in _d14]}")
_v14 = _d14[0][1].values if _d14 else {}
check(_v14.get("duration_sec") == 90.0 and abs((_v14.get("i2_pct_max") or 0) - 6.9) < 0.01
      and {"i2_pct_max", "i2_pct_med", "duration_sec", "proximity_threshold_pct",
           "factory_threshold_pct", "norm_ref", "live"} <= set(_v14),
      f"1г: значения детекции — из кластера, ключи прежние плюс live: {_v14}")
check(_d14 and "непрерывно 90с ≥ 60с" in _d14[0][1].trigger and "≥ 6%" in _d14[0][1].trigger,
      f"1г: текст — «непрерывно … ≥ …»: {_d14[0][1].trigger if _d14 else None}")

# Ряд фаз строится один раз на рабочую часть: метрики подсегментов берут его срез
import analytics.metrics as _mt  # noqa: E402
_n_ser = {"live": 0, "metrics": 0}
_o_lv, _o_mt = _lv.phase_series, _mt.phase_series


def _cnt(kind, fn):
    def w(*a, **k):
        _n_ser[kind] += 1
        return fn(*a, **k)
    return w


_lv.phase_series, _mt.phase_series = _cnt("live", _o_lv), _cnt("metrics", _o_mt)
try:
    _s14b = seg(_r14, L0, B14)
finally:
    _lv.phase_series, _mt.phase_series = _o_lv, _o_mt
_nsub14 = sum(1 for s in _s14b for sub in s.subsegments if not sub.transient)
check(_n_ser == {"live": 1, "metrics": 0} and _nsub14 >= 3,
      f"1г: ряд фаз — один раз на {_nsub14} подсегментов (срезы в метрики): {_n_ser}")
check(max((sub.derived_metrics.neg_seq_i2_duration_sec or 0) for s in _s14b for sub in s.subsegments) == 90.0,
      "1г: метрика подсегмента по срезу ряда — непрерывно 90 с")

# ── 1д. Три коротких кластера (по 50 с, через 15 мин) — ничего ──
_r1d = rows_for(L0, B14, phase=between([(600, 650), (1550, 1600), (2500, 2550)], I2F(7.5)))
check(not dets(seg(_r1d, L0, B14), I2K), "1д: три коротких кластера — эпизода нет")

# ── 1е. Метрики: непрерывный участок, не сумма; live=None — путь по метрикам ──
_dm14 = compute_derived_metrics(by_addr(_r14), L0, B14, [], CFG)
check(_dm14.neg_seq_i2_duration_sec == 90.0,
      f"1е: метрика подсегмента — самый длинный непрерывный участок (90), не сумма (390): "
      f"{_dm14.neg_seq_i2_duration_sec}")
_old = run_all_detectors({}, DerivedMetrics(neg_seq_i2_pct_max=7.0, neg_seq_i2_duration_sec=120.0),
                         RiskAccumulators(), [], "NORMAL", 3, L0, S(L0, 600), None, CFG, live_spans=None)
_old_ns = [d for d in _old if d.scenario == I2K]
check(len(_old_ns) == 1 and _old_ns[0].t_detected == iso(L0) and "live" not in _old_ns[0].values,
      f"1е: live=None — по метрикам подсегмента, как раньше: {[d.to_dict() for d in _old_ns]}")
_none = run_all_detectors({}, DerivedMetrics(neg_seq_i2_pct_max=7.0, neg_seq_i2_duration_sec=120.0),
                          RiskAccumulators(), [], "NORMAL", 3, L0, S(L0, 600), None, CFG, live_spans={})
check(not [d for d in _none if d.scenario == I2K], "1е: участков ключа нет — детекции нет")

# ── 1ж. Открытое окно: идущий участок квалифицируется, дотянув до 60 с ──
B1j = S(L0, 1200)
for _len, _want in ((50, False), (70, True)):
    _rj = rows_for(L0, B1j, phase=between([(1200 - _len, 1200)], I2F(7.0)))
    _cj = segment_spans(by_addr(_rj), None, CFG, L0, B1j, [], {}, False, horizon=B1j).get(I2K) or []
    check(bool(_cj) is _want and (not _cj or _cj[0]["ongoing"]),
          f"1ж: идущий участок {_len} с на живом краю — эпизод {_want}: {_cj}")

# ── 1з. Ревью: хвост без пакетов до края окна — не «непрерывно» ──
from analytics.rpm_freq import packet_times, rpm_freq_params  # noqa: E402
_PRM = rpm_freq_params(CFG)


def raw_clusters(rows, a, b, gaps=()):
    """Кластеры I₂ до квалификации (сырые участки ряда фаз окна)."""
    ba = by_addr(rows)
    pk = packet_times(ba, _PRM)
    gl = _lv._merge_gaps(list(gaps))
    brk = _lv._breaks(pk, gl, a, b, _PRM["packet_max_gap_sec"])
    ser, prm = _lv._phase_series(ba, CFG, a, b, list(gaps), pk)
    sp = _lv._phase_spans(ser, 1, prm["i2_thr"], a, b, brk, "I2")
    return _lv._clusters(sp, gl, I2K) if sp else []


# Одна точка 7 % и 70 с без пакетов до края (обрыв ещё не разрыв) — эпизода нет
_b1z = S(L0, 1170)
_r1z = rows_for(L0, _b1z, phase=between([(1100, 1110)], I2F(7.0)), skip=lambda s: s > 1100)
for _hz in (None, _b1z):
    check(I2K not in segment_spans(by_addr(_r1z), None, CFG, L0, _b1z, [], {}, False, horizon=_hz),
          f"1з: одна точка и 70 с без пакетов до края (горизонт {_hz}) — эпизода нет")
check(not dets(seg(_r1z, L0, _b1z), I2K), "1з: закрытое окно (обрыв за 70 с до реза) — эпизода нет")
_rc1z = raw_clusters(_r1z, L0, _b1z)
check(_rc1z and _rc1z[0]["longest_sec"] == 0.0 and _rc1z[0]["end"] == iso(_b1z) and _rc1z[0]["ongoing"],
      f"1з: длина идущего участка — до последнего отсчёта, конец эпизода — край окна: {_rc1z}")
# 30 с превышения (4 пакета) и хвост 40 / 60 / 85 с без пакетов — непрерывно 30 с
for _tail in (40, 60, 85):
    _bt = S(L0, 1130 + _tail)
    _rt = rows_for(L0, _bt, phase=between([(1100, 1140)], I2F(7.0)), skip=lambda s: s > 1130)
    _ct = raw_clusters(_rt, L0, _bt)
    check(len(_ct) == 1 and _ct[0]["longest_sec"] == 30.0 and _ct[0]["visible_sec"] == 30.0,
          f"1з: 30 с превышения и хвост {_tail} с — непрерывно 30 с: {_ct}")
    check(I2K not in segment_spans(by_addr(_rt), None, CFG, L0, _bt, [], {}, False, horizon=_bt),
          f"1з: 30 с + хвост {_tail} с — эпизода нет")
# Хвост после дыры без отсчётов: 50 с до дыры [1200, 1260], после неё пакетов нет до края
_bg = S(L0, 1330)
_rg = rows_for(L0, _bg, phase=between([(1150, 1200)], I2F(7.0)), skip=lambda s: s > 1190)
_cg = raw_clusters(_rg, L0, _bg, gaps=[gap(S(L0, 1190), S(L0, 1260))])
check(len(_cg) == 1 and _cg[0]["longest_sec"] == 40.0,
      f"1з: хвост [конец дыры, край] без отсчётов — не непрерывность: {_cg}")
# Живой результат не больше итогового: идущий 70 с (живой — 60) и закрытый (70)
_bl = S(L0, 1200)
_live70 = raw_clusters(rows_for(L0, _bl, phase=between([(1130, 1200)], I2F(7.0))), L0, _bl)
_fin70 = raw_clusters(rows_for(L0, S(L0, 1300), phase=between([(1130, 1200)], I2F(7.0))), L0, S(L0, 1300))
check(_live70[0]["longest_sec"] == 60.0 and _fin70[0]["longest_sec"] == 70.0,
      f"1з: живой ≤ итогового, запаздывание — один пакет: {_live70[0]['longest_sec']} / "
      f"{_fin70[0]['longest_sec']}")

# ── 1и. Ревью: пара устойчивого максимума — соседние отсчёты без дыры и без пропуска ──
_ph1i = (lambda s: I2F(9.0) if s in (300, 420) else (I2F(7.0) if 290 <= s < 520 else NORM))
_r1i = rows_for(L0, B1, phase=_ph1i, skip=lambda s: 300 < s < 420)
_c1i = segment_spans(by_addr(_r1i), None, CFG, L0, B1, [gap(S(L0, 300), S(L0, 420))], {}, False).get(I2K) or []
check(len(_c1i) == 1 and abs(_c1i[0]["smax"] - 7.0) < 0.01 and abs(_c1i[0]["vmax"] - 9.0) < 0.01,
      f"1и: пара через дыру в устойчивый максимум не идёт (9 и 9 по краям дыры): {_c1i}")
from analytics.metrics import _sustained_max_adj  # noqa: E402
_sa = [(S(L0, 0), 9.0), (S(L0, 10), None), (S(L0, 20), 9.0), (S(L0, 30), 5.0)]
check(_sustained_max_adj(_sa, 90) == 5.0, "1и: кадр без значения рвёт пару (было бы 9)")
check(_sustained_max_adj([(S(L0, 0), 9.0), (S(L0, 10), 9.0)], 90, [gap(S(L0, 2), S(L0, 8))]) is None,
      "1и: пара через дыру data_gaps не засчитана")
check(_sustained_max_adj([(S(L0, 0), 4.0), (S(L0, 10), None)], 90) == 4.0,
      "1и: одно значение в ряду — оно само")

# ══ 2. Перекос и нейтраль: 31 участок в одном кластере (29.09) ═══════════════
_iv31, _t = [], 600
for _k in range(31):
    _d = 120 if _k == 15 else 20 + 10 * (_k % 4)
    _iv31.append((_t, _t + _d))
    _t += _d + 20
B31 = S(L0, _t + 600)
_s31 = seg(rows_for(L0, B31, phase=between(_iv31, TRIP)), L0, B31)
for _sc in (SPK, NEK):
    _c = clusters(_s31, _sc)
    _d = dets(_s31, _sc)
    _v = _d[0][1].values if _d else {}
    check(len(_c) == 1 and _c[0]["spans"] == 31 and _c[0]["longest_sec"] == 120.0
          and _c[0]["start"] == iso(S(L0, 600)),
          f"2: {_sc}: один кластер 31 участок, непрерывно 120 с, с начала первого: {_c}")
    check(_v.get("run_sec") == 120.0 and _v.get("pct_max") == _c[0]["smax"]
          and {"pct_max", "pct_med", "run_sec", "threshold_pct", "duration_threshold_sec", "norm_ref",
               "live"} <= set(_v) and {d.t_detected for _, d in _d} == {iso(S(L0, 600))},
          f"2: {_sc}: pct_max и run_sec из кластера, ключи values прежние: {_v}")
check(not dets(_s31, I2K), "2: при провале фазы I₂ ниже 6 % — эпизода I₂ нет")
check(abs((clusters(_s31, SPK)[0]["smax"] or 0) - 46.5) < 0.5,
      f"2: устойчивый максимум перекоса ≈46,5 % I_ном: {clusters(_s31, SPK)[0]['smax']}")

# ══ 3. NO_LOAD_TOO_LONG по отрезкам (В-27.2) ═════════════════════════════════
BN = S(L0, 4200)


def ld(spans: list[tuple[float, float, float]], base: float = 50.0):
    """Нагрузка: [(с, по, значение)] поверх base."""
    def fn(s):
        for a, b, v in spans:
            if a <= s < b:
                return v
        return base
    return fn


# ── 3а. Отрезок кончился внутри подсегмента LOW (регресс 12.10 15:18 и 18:17) ──
# 5 % 20 мин, затем 20 % 10 мин (зона та же — LOW, отрезок кончился), затем 50 %
_r3a = rows_for(L0, BN, load=ld([(600, 1800, 5.0), (1800, 2400, 20.0)]))
_s3a = seg(_r3a, L0, BN)
_c3a = clusters(_s3a, NLK)
check(len(_c3a) == 1 and _c3a[0]["start"] == iso(S(L0, 1500)) and _c3a[0]["end"] == iso(S(L0, 1800))
      and _c3a[0]["run_sec"] == 1200.0 and _c3a[0]["run_start"] == iso(S(L0, 600)),
      f"3а: нарушение 600+900 → 1800 (отрезок 20 мин): {_c3a}")
_d3a = dets(_s3a, NLK)
check(_d3a and {d.t_detected for _, d in _d3a} == {iso(S(L0, 1500))}
      and _d3a[0][1].values.get("noload_run_sec") == 1200.0
      and _d3a[0][1].values.get("run_start") == iso(S(L0, 600))
      and "20 мин подряд" in _d3a[0][1].trigger and "UTC" not in _d3a[0][1].trigger,
      f"3а: детекция с момента превышения, длина отрезка, без часов в тексте: "
      f"{[(d.t_detected, d.trigger, d.values) for _, d in _d3a]}")
# Прежний путь (аккумулятор на конце подсегмента) этот отрезок терял
_old3a = []
for _s in _s3a:
    for _sub in _s.subsegments:
        if not _sub.transient:
            _old3a += [d for d in run_all_detectors(
                _sub.characteristics, _sub.derived_metrics, _sub.risk_accumulators, [], _sub.load_zone, 3,
                datetime.fromisoformat(_sub.t_start), datetime.fromisoformat(_sub.t_end), None, CFG,
                live_spans=None) if d.scenario == NLK]
check(not _old3a, f"3а: прежний путь по аккумулятору этот отрезок терял: {[d.t_detected for d in _old3a]}")

# ── 3б. Ровно +900 с; отрезок 900 с — не нарушение (строго больше нормы) ──
_c3b = clusters(seg(rows_for(L0, BN, load=ld([(600, 1500, 5.0)])), L0, BN), NLK)
check(not _c3b, f"3б: отрезок ровно 900 с — нарушения нет: {_c3b}")
_c3b2 = clusters(seg(rows_for(L0, BN, load=ld([(600, 1510, 5.0)])), L0, BN), NLK)
check(len(_c3b2) == 1 and _c3b2[0]["start"] == iso(S(L0, 1500)) and _c3b2[0]["end"] == iso(S(L0, 1510)),
      f"3б: 910 с — нарушение с +900 с: {_c3b2}")

# ── 3в. Допуск выхода 60 с ──
_c3v = clusters(seg(rows_for(L0, BN, load=ld([(600, 1200, 5.0), (1200, 1250, 25.0), (1250, 1850, 5.0)])),
                    L0, BN), NLK)
check(len(_c3v) == 1 and _c3v[0]["start"] == iso(S(L0, 1500)) and _c3v[0]["end"] == iso(S(L0, 1850))
      and _c3v[0]["run_start"] == iso(S(L0, 600)),
      f"3в: выход 50 с отрезок не рвёт: {_c3v}")
_c3v2 = clusters(seg(rows_for(L0, BN, load=ld([(600, 1200, 5.0), (1200, 1270, 25.0), (1270, 1870, 5.0)])),
                     L0, BN), NLK)
check(not _c3v2, f"3в: выход 70 с — два отрезка по 10 мин, нарушения нет: {_c3v2}")

# ── 3г. Дыра связи рвёт отрезок (как noload_reset аккумулятора) ──
_g3g = [gap(S(L0, 1200), S(L0, 1260))]
_r3g = rows_for(L0, BN, load=ld([(600, 1860, 5.0)]), skip=lambda s: 1200 < s < 1260)
check(not clusters(seg(_r3g, L0, BN, gaps=_g3g), NLK),
      "3г: дыра посреди 21 мин без нагрузки — два отрезка по 10 мин, нарушения нет")
# Пауза пакетов без строки data_gaps — тоже рвёт
_r3g2 = rows_for(L0, BN, load=ld([(600, 1860, 5.0)]), skip=lambda s: 1200 < s < 1320)
check(not clusters(seg(_r3g2, L0, BN), NLK), "3г: пауза пакетов 120 с — отрезок рвётся")

# ── 3д. Перенос через суточный рез: noload_run_sec 870, удержанная нагрузка 0 ──
CUT = M(13, 9, 0)
BC = S(CUT, 1800)
_r3d = rows_for(CUT, BC, load=ld([(-300, 600, 0.0)]))
_seed = {"held": {"LOAD_PCT": {"ts": S(CUT, -400), "value": 0.0}}, "held_gaps": []}
_s3d = seg(_r3d, CUT, BC, cr=CokingRisk(noload_run_sec=870.0), seed=_seed)
_c3d = clusters(_s3d, NLK)
check(len(_c3d) == 1 and _c3d[0]["start"] == iso(S(CUT, 30)) and _c3d[0]["end"] == iso(S(CUT, 600))
      and _c3d[0]["carried"] is False and _c3d[0]["carry_sec"] == 870.0 and _c3d[0]["run_sec"] == 1470.0
      and _c3d[0]["run_start"] == iso(CUT),
      f"3д: перенос 870 с — нарушение с t0 + 30 с (участок начался в окне, не перенесён): {_c3d}")
# Перенос 1000 с — нарушение уже идёт на резе: с самого t0, перенесённый участок
_c3d1 = clusters(seg(_r3d, CUT, BC, cr=CokingRisk(noload_run_sec=1000.0), seed=_seed), NLK)
check(len(_c3d1) == 1 and _c3d1[0]["start"] == iso(CUT) and _c3d1[0]["carried"] is True,
      f"3д: перенос больше нормы — нарушение с начала окна, перенесённое: {_c3d1}")
_d3d = dets(_s3d, NLK)
check(_d3d and _d3d[0][1].t_detected == iso(S(CUT, 30)) and _d3d[0][1].values.get("carry_sec") == 870.0,
      f"3д: детекция с t0 + 30 с, перенос в values: {[(d.t_detected, d.values) for _, d in _d3d]}")
# Без переноса (смена режима на начале окна) — 10 мин < 15
_s3d0 = seg(_r3d, CUT, BC, cr=CokingRisk(noload_run_sec=870.0), seed=_seed,
            enum=[rs(0, S(CUT, -3600), CUT), rs(3, CUT)])
check(not clusters(_s3d0, NLK), "3д: смена режима на начале окна — переноса нет")
# Перенос идёт выходом: нагрузка 15 % на резе (выход 20 с), через 20 с снова 0 — отрезок продолжается
_r3d2 = rows_for(CUT, BC, load=ld([(-300, 20, 15.0), (20, 600, 0.0)]))
_seed2 = {"held": {"LOAD_PCT": {"ts": S(CUT, -20), "value": 15.0}}, "held_gaps": []}
_c3d2 = clusters(seg(_r3d2, CUT, BC, cr=CokingRisk(noload_run_sec=870.0, noload_break_sec=20.0),
                     seed=_seed2), NLK)
check(len(_c3d2) == 1 and _c3d2[0]["start"] == iso(S(CUT, 30)) and _c3d2[0]["end"] == iso(S(CUT, 600)),
      f"3д: короткий выход на резе — отрезок с переносом продолжается: {_c3d2}")
# Выход на резе 50 с + 20 с в окне > 60 с — отрезок кончился, новый без переноса
_c3d3 = clusters(seg(_r3d2, CUT, BC, cr=CokingRisk(noload_run_sec=870.0, noload_break_sec=50.0),
                     seed=_seed2), NLK)
check(not _c3d3, f"3д: выход дольше допуска через рез — переноса нет: {_c3d3}")

# Ревью: удержанная нагрузка на t0 выпала — последняя строка нагрузки (−610 с) за паузой
# пакетов 200 с, дальше пакеты идут, нагрузка не меняется (строк нет); аккумулятор
# помнит 870 с — перенесённый отрезок с t0. С дырой data_gaps на t0 — переноса нет
def _ld3p(s):
    if s < -600:
        return 0.0
    if s < 0:
        return None
    return 0.0 if s < 600 else 50.0


_r3p = rows_for(CUT, BC, load=_ld3p, pre=900, skip=lambda s: -600 < s < -400)
_c3p = clusters(seg(_r3p, CUT, BC, cr=CokingRisk(noload_run_sec=870.0)), NLK)
check(len(_c3p) == 1 and _c3p[0]["start"] == iso(S(CUT, 30)) and _c3p[0]["carry_sec"] == 870.0,
      f"3д: удержанной нагрузки нет, перенос аккумулятора — с t0: {_c3p}")
_r3p2 = rows_for(CUT, BC, load=_ld3p, pre=900, skip=lambda s: -600 < s < -400 or -20 < s < 10)
_c3p2 = clusters(seg(_r3p2, CUT, BC, gaps=[gap(S(CUT, -20), S(CUT, 10))],
                     cr=CokingRisk(noload_run_sec=870.0)), NLK)
check(not _c3p2, f"3д: дыра связи на t0 — переноса нет: {_c3p2}")
# Пауза пакетов на самом резе (в преамбуле окна) — отрезок рвётся по правилам участков
_r3p3 = rows_for(CUT, BC, load=ld([(-300, 600, 0.0)]), skip=lambda s: -150 < s < 0)
check(not clusters(seg(_r3p3, CUT, BC, cr=CokingRisk(noload_run_sec=870.0)), NLK),
      "3д: пауза 150 с на резе — перенос рвётся (участки рвутся на паузе, аккумулятор — нет)")

# ── 3д′. Ревью: хвост без пакетов не досчитывает отрезок до нарушения (850 + 80 с) ──
_b3t = S(L0, 300 + 850 + 80)
_r3t = rows_for(L0, _b3t, load=ld([(300, 2000, 5.0)]), skip=lambda s: s > 300 + 850)
for _hz in (None, _b3t):
    check(NLK not in segment_spans(by_addr(_r3t), None, CFG, L0, _b3t, [], {}, False, horizon=_hz),
          f"3д′: 850 с без нагрузки и 80 с без пакетов (горизонт {_hz}) — нарушения нет")
_r3t2 = rows_for(L0, _b3t, load=ld([(300, 2000, 5.0)]))
_c3t2 = segment_spans(by_addr(_r3t2), None, CFG, L0, _b3t, [], {}, False).get(NLK) or []
check(len(_c3t2) == 1 and _c3t2[0]["start"] == iso(S(L0, 1200)) and _c3t2[0]["end"] == iso(_b3t)
      and _c3t2[0]["run_sec"] == 920.0,
      f"3д′: пакеты идут до края — нарушение, длина до последнего пакета (920), конец — край: {_c3t2}")

# ── 3д″. Ревью: два нарушения в одном подсегменте — отрезок того, что даёт t_detected ──
_b3w = S(L0, 4500)
_s3w = seg(rows_for(L0, _b3w, load=ld([(600, 1600, 5.0), (1600, 1700, 15.0), (1700, 3500, 5.0)])), L0, _b3w)
_d3w = [(sub, d) for sub, d in dets(_s3w, NLK) if len(d.values["live"]["clusters"]) == 2]
check(_d3w and _d3w[0][1].t_detected == iso(S(L0, 1500)) and _d3w[0][1].values["noload_run_sec"] == 1000.0
      and _d3w[0][1].values["run_start"] == iso(S(L0, 600)) and "17 мин подряд" in _d3w[0][1].trigger,
      f"3д″: два нарушения в подсегменте — отрезок первого (1000 с, «17 мин»): "
      f"{[(d.t_detected, d.values.get('noload_run_sec'), d.trigger) for _, d in dets(_s3w, NLK)]}")

# ── 3е. Режим не 3 — нет; live=None — по аккумулятору, как раньше ──
_acc = RiskAccumulators(coking_risk=CokingRisk(noload_run_sec=1000.0))
_lvnl = clusters(_s3a, NLK)
check(not [d for d in run_all_detectors({}, DerivedMetrics(), _acc, [], "LOW", 0, L0, BN, None, CFG,
                                        live_spans={NLK: _lvnl}) if d.scenario == NLK],
      "3е: режим не 3 — детекции нет")
_d3e = [d for d in run_all_detectors({}, DerivedMetrics(), _acc, [], "LOW", 3, L0, S(L0, 600), None, CFG,
                                     live_spans=None) if d.scenario == NLK]
check(len(_d3e) == 1 and _d3e[0].t_detected == iso(L0) and _d3e[0].values["noload_run_sec"] == 1000.0,
      f"3е: live=None — по аккумулятору подсегмента: {[d.to_dict() for d in _d3e]}")

# ── 3ж. Окно кончилось на выходе (меньше допуска) — отрезок кончился на начале выхода ──
B3j = S(L0, 1700)
_c3j = clusters(seg(rows_for(L0, B3j, load=ld([(600, 1660, 5.0)], base=50.0)), L0, B3j), NLK)
check(len(_c3j) == 1 and _c3j[0]["end"] == iso(S(L0, 1660)) and not _c3j[0]["ongoing"],
      f"3ж: выход за 40 с до края окна — конец отрезка на начале выхода: {_c3j}")
_c3j2 = clusters(seg(rows_for(L0, B3j, load=ld([(600, 1700, 5.0)])), L0, B3j), NLK)
check(len(_c3j2) == 1 and _c3j2[0]["end"] == iso(B3j) and _c3j2[0]["ongoing"],
      f"3ж: отрезок идёт на краю — до края, ongoing: {_c3j2}")

# ══ 4. Устойчивость ══════════════════════════════════════════════════════════


def _boom(*a, **k):
    raise RuntimeError("тест")


_r4 = rows_for(L0, BN, phase=between([(300, 400)], I2F(7.0)), load=ld([(600, 1800, 5.0)]))
_base4 = segment_spans(by_addr(_r4), None, CFG, L0, BN, [], {}, False)
check(_base4.get(I2K) and _base4.get(NLK), f"4: без сбоев — I₂ и без нагрузки есть: {list(_base4)}")
_orig = _lv._phase_series
_lv._phase_series = _boom
LOG.records.clear()
try:
    _r = segment_spans(by_addr(_r4), None, CFG, L0, BN, [], {}, False)
    check(all(k in _r and _r[k] is None for k in (I2K, SPK, NEK)),
          f"4: сбой ряда фаз — три ключа None: {[(k, _r.get(k, 'нет')) for k in (I2K, SPK, NEK)]}")
    check(_r.get(NLK) == _base4[NLK] and all(_r.get(k) == _base4.get(k) for k in _base4 if k not in
                                             (I2K, SPK, NEK)),
          "4: сбой ряда фаз не гасит соседей")
    check(logged("Ряд фаз"), "4: сбой ряда фаз — запись в лог")
    # Детекторы ключей None — по подсегменту (метрики того же ряда)
    _s4 = seg(_r4, L0, BN)
    check(dets(_s4, I2K) and all("live" not in d.values for _, d in dets(_s4, I2K)),
          "4: I₂ при сбое ряда — по метрикам подсегмента")
finally:
    _lv._phase_series = _orig
_orig = _lv._noload_spans
_lv._noload_spans = _boom
try:
    _r = segment_spans(by_addr(_r4), None, CFG, L0, BN, [], {}, False)
    check(NLK in _r and _r[NLK] is None and _r.get(I2K) == _base4[I2K],
          "4: сбой «без нагрузки» — ключ None, I₂ цел")
finally:
    _lv._noload_spans = _orig

# ══ 5. Движок с подменой БД: эпизод I₂ и запрет снятия ═══════════════════════
import online.engine as eng  # noqa: E402
from online.status_assembler import uncancellable_reasons  # noqa: E402

# Кластер I₂ [900, 1000) — 100 с подряд; открытое окно до разных горизонтов
_iv5 = [(900, 1000)]


def open_seg(end_sec):
    b = S(L0, end_sec)
    return seg(rows_for(L0, b, phase=between(_iv5, I2F(7.0))), L0, b, live=True)[-1]


for _end, _want in ((1300, True), (1590, True), (1610, False), (2400, False)):
    _, _dd = eng._extract_open_segment_data(open_seg(_end))
    _has = any(d.get("scenario") == I2K for d in _dd)
    _unc = [r["code"] for r in uncancellable_reasons({"analytics_alarms": _dd})]
    check(_has is _want and (("asymmetry" in _unc) is _want),
          f"5: окно до +{_end} с — I₂ в составе {_want} (кластер кончился на 1000 + 600): "
          f"{_has}, запреты {_unc}")


class FakeDB:
    def __init__(self):
        self.calls: list[tuple] = []
        self.next_id = 700

    async def open_episode(self, *a, **k):
        self.next_id += 1
        self.calls.append(("open", self.next_id, k))
        return self.next_id

    async def find_episode_by_onset(self, *a, **k):
        return None

    async def reopen_episode(self, ep_id, **k):
        self.calls.append(("reopen", ep_id, k))

    async def update_episode(self, ep_id, **k):
        self.calls.append(("update", ep_id, k))

    async def close_episode(self, ep_id, t_close, reason, **k):
        self.calls.append(("close", ep_id, t_close, reason, k))

    async def set_episodes_gate_suppressed(self, *a, **k):
        pass

    def of(self, kind):
        return [c for c in self.calls if c[0] == kind]


_DBN = ("open_episode", "find_episode_by_onset", "reopen_episode", "update_episode", "close_episode",
        "set_episodes_gate_suppressed")
_orig_db = {n: getattr(eng.online_db, n) for n in _DBN}
import analytics.source as _src  # noqa: E402
_orig_gd = _src.get_data_gaps


async def _gd_empty(*a, **k):
    return []


async def _noop(*a, **k):
    return None


_db5 = FakeDB()
for _n in _DBN:
    setattr(eng.online_db, _n, getattr(_db5, _n))
_src.get_data_gaps = _gd_empty
try:
    _e5 = eng.OnlinePollEngine.__new__(eng.OnlinePollEngine)
    _e5.cfg = CFG
    _e5.router_sn, _e5.equip_type, _e5.panel_id = "SN", "pcc", 1
    _e5._episodes, _e5._episode_accrual_ts, _e5._last_open_seg_id = {}, S(L0, 1270), 5
    _e5._supp_recheck, _e5._prev_seg_hint, _e5.cursor_ts = None, None, None
    _e5._attach_trip_context = _noop
    _e5._inherit_gate_suppression = _noop
    for _end in (1300, 1330, 1360, 1590, 1620, 1650, 1680):
        _os = open_seg(_end)
        _, _dd = eng._extract_open_segment_data(_os)
        asyncio.run(_e5._process_episodes(
            _dd, S(L0, _end), [], cleared_at=eng._analytics_cleared_at([_os], _e5._episodes),
            gaps_from=L0, run_state=3))
    _op5, _cl5 = _db5.of("open"), _db5.of("close")
    check(len(_op5) == 1 and _op5[0][2]["t_open"] == S(L0, 900) and _op5[0][2]["scenario"] == I2K,
          f"5: точный эпизод I₂ — с начала кластера: {_op5}")
    check(len(_cl5) == 1 and _cl5[0][2] == S(L0, 1000) and _cl5[0][3] == "condition_cleared",
          f"5: закрыт по концу кластера после дебаунса: {_cl5}")
finally:
    for _n, _f in _orig_db.items():
        setattr(eng.online_db, _n, _f)
    _src.get_data_gaps = _orig_gd

# ══ 6. В-30.1: разрыв пустого окна с первого цикла ═══════════════════════════
CUT29 = M(29, 9, 0)
_g = eng._synth_connectivity_gap([], CUT29, S(CUT29, 5), CFG3)
check(_g is not None and _g["gap_start"] == CUT29 and _g.get("start_unknown") is True and _g["gap_end"] is None,
      f"6а: пустое окно 5 с — разрыв с t_from, start_unknown (раньше — только после 90 с): {_g}")
check(eng._synth_connectivity_gap([h(HB, S(CUT29, -20), 1.0)], CUT29, S(CUT29, 5), CFG3) is None,
      "6а: окно со строкой в преамбуле — льгота 90 с от последней строки, как раньше")
_g2 = eng._synth_connectivity_gap([h(HB, S(CUT29, -200), 1.0)], CUT29, S(CUT29, 5), CFG3)
check(_g2 is not None and _g2["gap_start"] == S(CUT29, -200) and not _g2.get("start_unknown"),
      f"6а: последняя строка 205 с назад — разрыв от неё, как раньше: {_g2}")

_prev = S(CUT29, -0.4)
_gu = [{"gap_start": CUT29, "gap_end": None, "start_unknown": True}]
_sp, _gp, _lvs = eng._accrual_split(_prev, S(CUT29, 5), _gu, window_from=CUT29)
check((round(_sp, 3), round(_gp, 3), _lvs) == (5.4, 5.4, 0.0),
      f"6б: [prev_base, t_from) того же обрыва — слепое, живого времени нет: {(_sp, _gp, _lvs)}")
check(round(eng._accrual_split(_prev, S(CUT29, 5), _gu)[2], 3) == 0.4,
      "6б: без window_from — как раньше (0,4 с «живого»)")
_sp, _gp, _lvs = eng._accrual_split(S(CUT29, -400), S(CUT29, 5), _gu, window_from=CUT29)
check((_sp, _gp, _lvs) == (405.0, 305.0, 100.0),
      f"6б: слепое — не раньше t_from − преамбула (300 с): {(_sp, _gp, _lvs)}")
_gk = [{"gap_start": CUT29, "gap_end": None}]
check(eng._accrual_split(_prev, S(CUT29, 5), _gk, window_from=CUT29)[2] > 0.39,
      "6б: разрыв с известным началом не продлевается")
check(_gu[0]["gap_start"] == CUT29, "6б: входной список разрывов не меняется")

# ── 6в. Полный путь _update_open_window: сценарии 29.10 S1–S7 (sim_2910) ──
import analytics.source as src  # noqa: E402
from db import remote  # noqa: E402

_calls: list = []


class _AnyDB:
    def __getattr__(self, name):
        if name in ("episode_key",):
            return getattr(__import__("online.db", fromlist=["x"]), name)

        async def fn(*a, **k):
            _calls.append((name, a, k))
            return None
        return fn


_ENUM = [{"addr": 40010, "state_start": M(24, 15, 59, 40), "state_end": None, "value": 2,
          "name_ru": "Ключ", "label": "Авто"},
         {"addr": 40011, "state_start": M(24, 15, 59, 40), "state_end": None, "value": 3,
          "name_ru": "Режим", "label": "Работа"}]


async def _empty(*a, **k):
    return []


async def _enum(*a, **k):
    return [dict(r) for r in _ENUM]


async def _tl(*a, **k):
    return [dict(r) for r in _ENUM], []


async def _none(*a, **k):
    return None


async def _ttb(func, **k):
    return func()


async def _goc(*aws):
    return list(await asyncio.gather(*aws))


_SAVE = {"db": eng.online_db, "ls": eng._load_seed, "ttb": remote.to_thread_beating,
         "goc": remote.gather_or_cancel}
_SRC_NAMES = ("get_whitelist_history_chunked", "get_whitelist_history", "get_enum_periods",
              "get_fault_periods", "get_data_gaps", "get_timeline_periods")
_SAVE_SRC = {n: getattr(src, n) for n in _SRC_NAMES}
eng.online_db = _AnyDB()
src.get_whitelist_history_chunked = _empty
src.get_whitelist_history = _empty
src.get_enum_periods = _enum
src.get_fault_periods = _empty
src.get_data_gaps = _empty
src.get_timeline_periods = _tl
eng._load_seed = _none
remote.to_thread_beating = _ttb
remote.gather_or_cancel = _goc


def mk29(until, exact=True, t_open=None, acc=None):
    e = eng.OnlinePollEngine("1126246373", "pcc", 1, "x", CFG3, poll_interval_sec=30, daily_hour=9, tz=MSK)
    key = "LIMIT_PROXIMITY" if exact else I2K
    e._episodes = {key: ({"id": 15057, "exact": True, "t_open": t_open or M(29, 3, 51, 29), "until": until,
                          "until_kind": "end", "miss": 0, "first_miss_ts": None, "severity": "CAUTION"}
                         if exact else
                         {"id": 999, "exact": False, "t_open": M(29, 3, 0), "until": None, "until_kind": None,
                          "miss": 0, "first_miss_ts": None, "severity": "CAUTION"})}
    e._episode_accrual_ts = acc or datetime(2026, 10, 29, 8, 59, 59, 613669, tzinfo=MSK).astimezone(U)
    e._last_open_run_state = 3
    e.cursor_ts = CUT29
    return e


async def _run29(e, offsets, cut=CUT29):
    closes = []
    for off in offsets:
        n0 = len(_calls)
        await e._update_open_window(cut, S(cut, off))
        closes += [c for c in _calls[n0:] if c[0] == "close_episode"]
    return closes


try:
    REAL, FAST = [31, 63, 105, 136], [5, 36, 80, 112]
    _SC = (("S1", CUT29, REAL, True, None), ("S2", CUT29, FAST, True, None),
           ("S3", S(CUT29, -86400), REAL, True, M(28, 3, 51, 29)),
           ("S4", S(CUT29, -86400), FAST, True, M(28, 3, 51, 29)),
           ("S5", M(29, 8, 51, 30), FAST, True, None),
           ("S6", None, REAL, False, None), ("S7", None, FAST, False, None))
    for _nm, _u, _offs, _ex, _to in _SC:
        _e = mk29(_u, _ex, _to)
        _cl = asyncio.run(_run29(_e, _offs))
        check(not _cl and len(_e._episodes) == 1,
              f"6в: {_nm} — эпизод не закрыт «вслепую» в первые циклы после реза: закрытия {_cl}, "
              f"память {list(_e._episodes)}")

    # ── 6г. Три суточных реза многосуточного обрыва (sim_debounce2) — ни одного закрытия ──
    _e = mk29(M(25, 0, 11, 3), True, M(25, 0, 5), acc=M(26, 8, 59, 40))
    _e._episodes["IDLE_TOO_LONG"] = {"id": 102, "exact": False, "t_open": M(24, 22, 0), "until": None,
                                     "miss": 0, "first_miss_ts": None, "severity": "CAUTION"}
    _cl = []
    for _day in (26, 27, 28):
        _t0 = M(_day, 9, 0)
        _e._episode_accrual_ts = M(_day, 8, 59, 40)
        _e.cursor_ts = _t0
        _e._open_history_cache = None
        _e._open_history_cache_ts = None
        _cl += asyncio.run(_run29(_e, [10 + 30 * k for k in range(6)], cut=_t0))
    check(not _cl and set(_e._episodes) == {"LIMIT_PROXIMITY", "IDLE_TOO_LONG"},
          f"6г: три реза в обрыве — эпизоды не закрыты: {_cl}")

    # ── 6д. Связь вернулась (в окне есть строки, разрыв кончился) — дебаунс закрывает как раньше ──
    _hist = [h(HB, S(CUT29, s), float(s)) for s in range(-280, 200, 10)]

    async def _hist_fn(*a, **k):
        return [dict(r) for r in _hist]
    src.get_whitelist_history_chunked = _hist_fn
    src.get_whitelist_history = _hist_fn
    _e = mk29(None, False)
    _cl = asyncio.run(_run29(_e, [31, 63, 95, 126]))
    check(len(_cl) == 1 and _cl[0][1][0] == 999 and not _e._episodes,
          f"6д: данные идут — неточный эпизод закрыт дебаунсом, как раньше: {_cl}")

    # ── 6е. Ревью (остаток В-30.1, sim_residual): обрыв за x с до реза — строки в
    # преамбуле есть, синтетического разрыва ещё нет; окно без своих строк — не
    # живое время, неточный эпизод не закрывается «вслепую» в 09:00:03 ──
    def _hist_rng(rows):
        async def fn(sn, et, p, a, b, wl, meta=None):
            out = [dict(r) for r in rows if a <= r["ts"] < b]
            if meta is not None:
                meta["max_ts"] = max((r["ts"] for r in out), default=None)
            return out
        return fn

    def mk_res(exact, x):
        e = eng.OnlinePollEngine("1126246373", "pcc", 1, "x", CFG3, poll_interval_sec=30, daily_hour=9,
                                 tz=MSK)
        key = "LIMIT_PROXIMITY" if exact else "COOLING_FAILURE"
        e._episodes = {key: ({"id": 1, "exact": True, "t_open": M(29, 8, 40), "until": CUT29,
                              "until_kind": "end", "miss": 0, "first_miss_ts": None, "severity": "CAUTION"}
                             if exact else
                             {"id": 2, "exact": False, "t_open": M(29, 8, 0), "until": None,
                              "until_kind": None, "miss": 0, "first_miss_ts": None, "severity": "CAUTION"})}
        e._episode_accrual_ts = S(CUT29, -(x - 5)) if x > 5 else S(CUT29, -1)
        e._last_open_run_state = 3
        e.cursor_ts = CUT29
        return e

    for _x in (20, 10):
        src.get_whitelist_history_chunked = src.get_whitelist_history = _hist_rng(
            [h(HB, S(CUT29, s), float(s)) for s in range(-290, -_x + 1, 10)])
        for _ex in (False, True):
            for _offs in ([3, 34, 65], [10, 41, 72, 103]):
                _cl = asyncio.run(_run29(mk_res(_ex, _x), _offs))
                check(not _cl, f"6е: обрыв за {_x} с до реза, {'точный' if _ex else 'неточный'}, "
                               f"циклы +{_offs} — закрытия «вслепую» нет: {_cl}")
    # Связь живая, первый цикл (+3 с) без своих строк: дебаунс сдвигается на цикл —
    # промахи с +34, закрытие на третьем (+96), конец — первый промах 09:00:34
    src.get_whitelist_history_chunked = src.get_whitelist_history = _hist_rng(
        [h(HB, S(CUT29, s), float(s)) for s in range(-290, 300, 10) if not (-1 < s < 8)]
        + [h(HB, S(CUT29, s), float(s)) for s in range(8, 300, 10)])
    _e = mk_res(False, 1)
    _seen = []
    for _off in (3, 34, 65, 96, 127):
        _n0 = len(_calls)
        asyncio.run(_e._update_open_window(CUT29, S(CUT29, _off)))
        _seen.append((_off, [c for c in _calls[_n0:] if c[0] == "close_episode"],
                      dict((k, v["miss"]) for k, v in _e._episodes.items())))
    _closes6 = [(o, c) for o, c, _m in _seen if c]
    check(len(_closes6) == 1 and _closes6[0][0] == 96 and _closes6[0][1][0][1][1] == S(CUT29, 34)
          and _seen[0][2] == {"COOLING_FAILURE": 0},
          f"6е: живая связь — дебаунс сдвинут на цикл, без ложного удержания: {_seen}")
finally:
    eng.online_db = _SAVE["db"]
    eng._load_seed = _SAVE["ls"]
    remote.to_thread_beating = _SAVE["ttb"]
    remote.gather_or_cancel = _SAVE["goc"]
    for _n, _f in _SAVE_SRC.items():
        setattr(src, _n, _f)

# ══ Итог ═════════════════════════════════════════════════════════════════════
if _errors:
    print(f"ОШИБОК: {len(_errors)}")
    for _x in _errors:
        print("  •", _x)
    sys.exit(1)
print("Все проверки пройдены.")
