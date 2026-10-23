# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Участки и кластеры нарушений аналитики — один раз на сегмент (v5.1.21, П2).

Жалоба 15.10.2026 (ДГУ №1, #35890): «Просадка оборотов — 1 эпизод, воздействие
24 ч» от одиночных отсчётов. Детекторы решали по min/max подсегмента, начало
эпизода было началом подсегмента, конец — его концом: одна точка давала сутки.
Теперь нарушение — участок по самим отсчётам:
- начало — первая строка за порогом, при переносе через начало окна — само
  начало окна; конец — первая строка в норме; на краю окна участок «идёт»;
- значение держится между строками, пока идут пакеты связи (регистры пишутся
  только при изменении). Дыра связи из data_gaps участок не рвёт (догма
  19.09): он тянется до первого пакета после дыры, время дыры — слепая доля.
  Пауза пакетов длиннее 90 с без строки data_gaps — участок кончается на
  последнем пакете перед ней, время паузы — не воздействие;
- кластер — участки, между которыми не больше LIVE_RECENT_SEC видимого времени
  (астрономическое минус data_gaps). Кластер — это эпизод.

Ключи (решения владельца В1–В7): RPM_UNDERSPEED — по v* (обороты и частота
опроса ниже порога, analytics/rpm_freq.py); RPM_OVERSPEED (v5.1.23, В2′) — то
же сверху: обороты и k·f_max опроса выше порога (1575 об/мин = уставка разноса
1725 − 150, 91,3 % уставки), без частоты заброса нет, без участков нет и
детекции; LIMIT_PROXIMITY — три роли в одном
наборе кластеров, участки по ролям внутри кластера; давление ниже уставки
останова — не замер (пол, В3); COOLING_FAILURE — ветка близости к HCT;
OIL_DILUTION — паспорт давления (участки oil_passport_runs из metrics, начало
эпизода — начало участка, В7).

Окно — рабочая часть сегмента «Работа» [head_end, tail_start). Перенос через
начало окна — только при продолжении режима (REPORT_START без разгона): иначе
нули стоянки давали бы «нарушение с начала». Модуль без зависимостей от движка.

Свежесть (П4): detection_live / alive_detections — одна функция живого состава
для движка (engine._extract_open_segment_data) и сводки (analytics_at_end):
нарушение живо, пока его кластер идёт или кончился не больше N видимого
времени назад; ключ держится взглядом назад через смену зоны.
"""
from __future__ import annotations

import bisect
import copy
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .metrics import oil_passport_params, oil_passport_runs, passport_instep
from .rpm_freq import (
    _PACKET_TOL_SEC, build_poll, dip_marks, freq_ok, over_marks, overspeed_params, packet_times,
    rpm_freq_params,
)

logger = logging.getLogger(__name__)

# Окно свежести и склейки N (В2, 15.10.2026): участки, между которыми не больше
# N видимого времени, — один кластер; нарушение живо, пока идёт или кончилось
# не больше N назад. То же окно, что у паспорта давления с v5.1.12
LIVE_RECENT_SEC = 600

# Роли, чьи удержанные значения переносятся через начало окна (и частота опроса)
HELD_ROLES = ("RPM", "FREQUENCY", "OIL_TEMP", "COOLANT_TEMP", "OIL_PRESS")

_FAR = datetime.max.replace(tzinfo=timezone.utc)


def _tz(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _as_dt(v: Any) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, str):
        try:
            return _tz(datetime.fromisoformat(v))
        except ValueError:
            return None
    return _tz(v) if isinstance(v, datetime) else None


def _iso(ts: datetime) -> str:
    return _tz(ts).isoformat()


# ── Пороги близости — одни для участков и детекторов ────────────────────────

def limit_checks(cfg) -> list[dict[str, Any]]:
    """Пороги близости к уставкам останова панели (LIMIT_PROXIMITY).

    near — порог близости (в пределах proximity_pct от уставки); below — плохо
    «ниже» (давление) или «выше» (температуры, строгое «>»). floor — у давления:
    значение ниже самой уставки останова на рабочих оборотах без останова
    панели — не замер (В3: нулевой кадр ДЭС №3 13.10 14:59:56 давал близость)."""
    pct = float(cfg.det("LIMIT_PROXIMITY", "proximity_pct", default=10.0))
    p = float(cfg.thr("oil_pressure", "controller", "shutdown_rated_kpa", default=241.3))
    c = float(cfg.thr("coolant_temperature", "controller", "shutdown_c", default=103.9))
    o = float(cfg.thr("oil_temperature", "controller", "shutdown_c", default=110.0))
    return [
        {"role": "OIL_PRESS", "stat": "min", "limit": p, "label": "Давление масла", "codes": [415],
         "below": True, "near": p * (1 + pct / 100), "floor": p, "pct": pct},
        {"role": "COOLANT_TEMP", "stat": "max", "limit": c, "label": "Температура ОЖ", "codes": [151],
         "below": False, "near": c * (1 - pct / 100), "floor": None, "pct": pct},
        {"role": "OIL_TEMP", "stat": "max", "limit": o, "label": "Температура масла", "codes": [214],
         "below": False, "near": o * (1 - pct / 100), "floor": None, "pct": pct},
    ]


def limit_bad(chk: dict[str, Any], v: float) -> bool:
    """Значение в пределах близости к уставке (строгие сравнения, как у детектора)."""
    return v < chk["near"] if chk["below"] else v > chk["near"]


def cooling_proximity(cfg) -> dict[str, float]:
    """Близость ОЖ к уставке HCT Warning панели (COOLING_FAILURE, v5.1.15): ≥ near."""
    hct = float(cfg.thr("coolant_temperature", "controller", "warning_c", default=97.8))
    pct = float(cfg.det("COOLING_FAILURE", "proximity_warning_pct", default=10.0))
    return {"hct_warning_c": hct, "pct": pct, "near": hct * (1 - pct / 100)}


# ── Дыры связи и разрывы пакетов ────────────────────────────────────────────

def _merge_gaps(gaps: list[dict] | None) -> list[tuple[datetime, datetime]]:
    """Дыры data_gaps — объединённые интервалы; открытая (синтетический хвост) — до ∞.

    Принимает и дыры подсегмента ({start, end} ISO, обрезанные по нему) и готовые
    кортежи (v5.1.21, П4: свежесть живого состава считается по data_gaps
    подсегментов)."""
    iv = []
    for g in gaps or []:
        if isinstance(g, tuple):
            gs, ge = _as_dt(g[0]), _as_dt(g[1]) or _FAR
        else:
            gs = _as_dt(g.get("gap_start", g.get("start")))
            ge = _as_dt(g["gap_end"] if "gap_end" in g else g.get("end")) or _FAR
        if gs is None:
            continue
        if ge > gs:
            iv.append((gs, ge))
    iv.sort()
    out: list[tuple[datetime, datetime]] = []
    for s, e in iv:
        if out and s <= out[-1][1]:
            if e > out[-1][1]:
                out[-1] = (out[-1][0], e)
        else:
            out.append((s, e))
    return out


def _overlap(gl: list[tuple[datetime, datetime]], a: datetime, b: datetime) -> float:
    """Секунды дыр внутри [a, b]."""
    tot = 0.0
    for gs, ge in gl:
        lo, hi = max(a, gs), min(b, ge)
        if hi > lo:
            tot += (hi - lo).total_seconds()
    return tot


def _covered(gl: list[tuple[datetime, datetime]], a: datetime, b: datetime) -> bool:
    return any(gs < b and ge > a for gs, ge in gl)


def visible_sec(a: datetime, b: datetime, gaps: list[dict] | list[tuple] | None) -> float:
    """Видимое время между a и b: астрономическое минус дыры data_gaps."""
    gl = gaps if gaps and isinstance(gaps[0], tuple) else _merge_gaps(gaps)  # type: ignore[arg-type]
    return (b - a).total_seconds() - _overlap(gl, a, b)


def _breaks(packets: list[datetime], gl: list[tuple[datetime, datetime]],
            t0: datetime, t1: datetime, max_gap: float) -> list[tuple[datetime, datetime | None, str]]:
    """Разрывы связи в окне между соседними пакетами: (a, b, вид).

    a — последний пакет до разрыва, b — первый после (None — до края окна
    пакетов больше нет). «gap» — разрыв пересекает дыру data_gaps (в том числе
    синтетический хвост открытого окна): участок не рвётся. «pause» — пауза
    пакетов длиннее max_gap без строки data_gaps: участок кончается на a."""
    out: list[tuple[datetime, datetime | None, str]] = []
    i = bisect.bisect_left(packets, t0)
    j = bisect.bisect_left(packets, t1)
    pk = packets[max(0, i - 1):j]
    for a, b in zip(pk, pk[1:]):
        if _covered(gl, a, b):
            out.append((a, b, "gap"))
        elif (b - a).total_seconds() > max_gap:
            out.append((a, b, "pause"))
    if pk and pk[-1] < t1:
        a = pk[-1]
        if _covered(gl, a, t1):
            out.append((a, None, "gap"))
        elif (t1 - a).total_seconds() > max_gap:
            out.append((a, None, "pause"))
    return out


# ── Удержанные значения на начало сегмента ──────────────────────────────────

def _real_rows(rows: list[dict] | None, t0: datetime | None = None,
               t1: datetime | None = None) -> list[tuple[datetime, float]]:
    out = []
    for r in rows or []:
        if r.get("is_carried_forward"):
            continue
        v = r.get("value")
        if v is None:
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        ts = _tz(r["ts"])
        if (t0 is not None and ts < t0) or (t1 is not None and ts >= t1):
            continue
        out.append((ts, fv))
    out.sort(key=lambda x: x[0])
    return out


def held_before(by_addr: dict[int, list[dict]], cfg, t: datetime,
                seed: dict[str, tuple[datetime, float]] | None = None,
                gaps: list[dict] | None = None,
                with_passport: bool = True) -> dict[str, Any]:
    """Удержанные значения на момент t (начало сегмента): {роль: (момент строки, значение)}.

    Последняя реальная строка роли до t, а если её нет в окне истории — посев
    перед окном (engine._load_seed, П10). Значение, между строкой которого и t
    была дыра data_gaps или пауза пакетов длиннее 90 с, неизвестно — его нет.
    До начала истории пакетов не видно: у посева проверяются только дыры (их
    отдал посев) и паузы от первого известного пакета.
    with_passport — ещё «_passport_since» (начало идущего на t участка ниже
    паспорта) для переноса через начало окна; нужен только у REPORT_START."""
    t = _tz(t)
    prm = rpm_freq_params(cfg)
    max_gap = prm["packet_max_gap_sec"]
    packets = packet_times(by_addr, prm)
    gl = _merge_gaps(gaps)
    out: dict[str, tuple[datetime, float]] = {}
    for role in HELD_ROLES:
        addr = cfg.role_to_addr(role)
        rows = _real_rows(by_addr.get(addr), t1=t) if addr is not None else []
        from_seed = not rows
        h = rows[-1] if rows else (seed or {}).get(role)
        if h is None:
            continue
        ts, v = _tz(h[0]), float(h[1])
        if ts >= t or _covered(gl, ts, t):
            continue
        i = bisect.bisect_right(packets, ts)
        j = bisect.bisect_left(packets, t)
        seq = packets[i:j]
        if not from_seed:
            seq = [ts] + seq
        if seq:
            seq = seq + [t]
            if any((b - a).total_seconds() > max_gap for a, b in zip(seq, seq[1:])):
                continue
        out[role] = (ts, v)
    if with_passport and "OIL_PRESS" in out:
        since = _passport_since(by_addr, cfg, t, seed, gaps, packets)
        if since is not None:
            out["_passport_since"] = since
    return out


# Насколько назад от начала сегмента искать начало идущего участка ниже паспорта
_PASSPORT_SINCE_LOOKBACK_SEC = 1800


def _passport_since(by_addr, cfg, t, seed, gaps, packets) -> tuple | None:
    """С какого момента давление ниже паспорта (и ниже на шаг) непрерывно до t —
    по строкам окна истории до t тем же рядом, что у паспорта (oil_passport_runs).

    Нижняя оценка начала перенесённого участка: строка, удержанная на t, бывает
    повтором того же значения (ДЭС №3 14.10: 240 в 08:58:48 и снова в 08:59:50),
    и от её метки участок 72 + 8 с через рез не набрал бы выдержку.
    → (soft_since, hard_since) — None у части, что на t не идёт."""
    prm = oil_passport_params(cfg)
    if prm is None:
        return None
    lo = t - timedelta(seconds=_PASSPORT_SINCE_LOOKBACK_SEC)
    rows = {}
    for role, key in (("OIL_PRESS", "p_addr"), ("RPM", "rpm_addr")):
        allr = _real_rows(by_addr.get(prm[key]), t1=t)
        k = bisect.bisect_left([x[0] for x in allr], lo)
        # Строка до границы поиска — удержанное значение на её начало
        r = allr[max(0, k - 1):]
        s = (seed or {}).get(role)
        if s is not None and (not r or _tz(s[0]) < r[0][0]):
            r = [(_tz(s[0]), float(s[1]))] + r
        rows[role] = r
    pk = packets[bisect.bisect_left(packets, lo):bisect.bisect_left(packets, t)]
    if not rows["OIL_PRESS"] or not pk:
        return None
    runs = oil_passport_runs(rows["OIL_PRESS"], rows["RPM"], pk, gaps or [], prm)
    out = []
    for part in ("soft", "hard"):
        rr = [r for r in runs[part] if r.get("open")]
        out.append(rr[-1]["start"] if rr else None)
    return tuple(out) if out[0] is not None else None


# ── Участки ─────────────────────────────────────────────────────────────────

def _spans(points: list[tuple], t0: datetime, t1: datetime,
           brk: list[tuple[datetime, datetime | None, str]],
           init: tuple | None = None, role: str | None = None) -> list[dict[str, Any]]:
    """Участки нарушения по отсчётам одного правила.

    points — [(момент, за порогом, значение, доп.)] по возрастанию, моменты в
    [t0, t1); init — удержанный на t0 отсчёт того же вида (момент < t0) при
    переносе. Значение держится между строками; разрыв «pause» кончает участок
    на последнем пакете, «gap» — нет: участок тянется до первого пакета после
    дыры и продолжается, только если в этом пакете пришла строка (свежая
    строка решает сама), иначе кончается на этом пакете. Участок — dict:
    _s/_e (datetime), ongoing, carried, role, _pts [(момент, значение, доп.)]."""
    tol = timedelta(seconds=_PACKET_TOL_SEC)
    out: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None

    def close(te: datetime) -> None:
        nonlocal cur
        cur["_e"] = max(te, cur["_s"])
        cur["ongoing"] = False
        out.append(cur)
        cur = None

    if init is not None and init[1]:
        cur = {"_s": t0, "carried": True, "role": role, "_pts": [(init[0], init[2], init[3])]}
    pi, bi = 0, 0
    n, m = len(points), len(brk)
    while pi < n or bi < m:
        if bi < m and (pi >= n or points[pi][0] > brk[bi][0] + tol):
            a, b, kind = brk[bi]
            bi += 1
            if cur is None:
                continue
            if kind == "pause":
                close(max(a, cur["_pts"][-1][0]))
            elif b is not None and not (pi < n and points[pi][0] <= b + tol):
                close(b)
            continue
        ts, bad, v, x = points[pi]
        pi += 1
        if bad:
            if cur is None:
                cur = {"_s": ts, "carried": False, "role": role, "_pts": []}
            cur["_pts"].append((ts, v, x))
        elif cur is not None:
            close(ts)
    if cur is not None:
        cur["_e"] = t1
        cur["ongoing"] = True
        out.append(cur)
    # Перенесённый участок, кончившийся на самом t0, без отсчёта в окне — не нарушение окна
    return [s for s in out if s["_e"] > s["_s"] or any(p[0] >= t0 for p in s["_pts"])]


def _rpm_points(seg_by_addr, cfg, prm, t0, t1, held, carry, gaps, packets, horizon):
    """Отсчёты оборотов с признаком провала v* (1б): [(момент, в провале, обороты,
    k·f_min)]; отсчёт правила B на живом краю (None) пропускается до следующего.
    horizon None — закрытое окно: живого края нет, ждать нечего."""
    addr = cfg.role_to_addr("RPM")
    if addr is None:
        return [], None
    rows = _real_rows(seg_by_addr.get(addr))
    h = held.get("RPM") if carry else None
    if h is not None:
        # Удержанный отсчёт — на место строк до окна (без пинга forward-fill их не убирает)
        h = (_tz(h[0]), float(h[1]))
        rows = [h] + [r for r in rows if r[0] >= t0]
    poll = build_poll(seg_by_addr, cfg, prm, held.get("FREQUENCY"), gaps, packets)
    pts, init = [], None
    for ts, v, fmin, bad in dip_marks(rows, poll, prm, horizon):
        if bad is None:
            continue
        kf = prm["k"] * fmin if bad and freq_ok(fmin, prm) else None
        if ts < t0:
            if h is not None and ts == h[0]:
                init = (ts, bool(bad), v, kf)
            continue
        if ts >= t1:
            break
        pts.append((ts, bool(bad), v, kf))
    return pts, init


def _above(r: dict, thr: float) -> bool:
    """Реальная строка выше порога (копии forward-fill и нечисла — нет)."""
    v = r.get("value")
    if v is None or r.get("is_carried_forward"):
        return False
    try:
        return float(v) > thr
    except (TypeError, ValueError):
        return False


def _over_points(seg_by_addr, cfg, oprm, t0, t1, held, carry, gaps, packets):
    """Отсчёты оборотов с признаком заброса (v5.1.23, п.2): [(момент, в заборе,
    обороты, k·f_max)]. oprm — overspeed_params. Ни одного отсчёта выше порога
    (почти всегда) — сразу пусто: опрос частоты не строится. Перенос через
    начало окна — как у провала (удержанный отсчёт на место строк до окна)."""
    addr = cfg.role_to_addr("RPM")
    if addr is None:
        return [], None
    thr = oprm["threshold_rpm"]
    h = held.get("RPM") if carry else None
    if h is not None:
        h = (_tz(h[0]), float(h[1]))
    raw = seg_by_addr.get(addr) or []
    # Дешёвый просмотр до разбора строк: заброса нет почти никогда
    if (h is None or h[1] <= thr) and not any(_above(r, thr) for r in raw):
        return [], None
    rows = _real_rows(raw)
    if h is not None:
        rows = [h] + [r for r in rows if r[0] >= t0]
    poll = build_poll(seg_by_addr, cfg, oprm, held.get("FREQUENCY"), gaps, packets)
    pts, init = [], None
    for ts, v, fmax, bad in over_marks(rows, poll, oprm):
        kf = oprm["k"] * fmax if bad else None
        if ts < t0:
            if h is not None and ts == h[0]:
                init = (ts, bad, v, kf)
            continue
        if ts >= t1:
            break
        pts.append((ts, bad, v, kf))
    return pts, init


def _value_points(rows, bad, floor=None) -> list[tuple]:
    return [(ts, bad(v), v, None) for ts, v in rows if floor is None or v >= floor]


def _held_init(held, role, carry, bad, floor=None) -> tuple | None:
    h = held.get(role) if carry else None
    if h is None or (floor is not None and h[1] < floor):
        return None
    return (h[0], bad(h[1]), h[1], None)


def _passport_spans(seg_by_addr, cfg, t0, t1, gaps, held, carry, packets, trail_pause):
    """Участки ниже паспорта давления (OIL_DILUTION, v5.1.12) по пакетам окна.

    Ряд и участки — metrics.oil_passport_runs (как у DerivedMetrics подсегмента);
    квалификация (≥ выдержки видимого времени) — без перехода через дыру и паузу.
    Перенесённому через начало окна участку длительность для квалификации
    считается от метки удержанной строки (нижняя оценка, дыр там нет — посев их
    отбросил): участок 40 + 40 с через суточный рез квалифицируется. Начало
    участка — начало эпизода (В7), выдержка только подтверждает."""
    prm = oil_passport_params(cfg)
    if prm is None:
        return []
    p_rows = _real_rows(seg_by_addr.get(prm["p_addr"]), t0, t1)
    r_rows = _real_rows(seg_by_addr.get(prm["rpm_addr"]), t0, t1)
    pk = [p for p in packets if t0 <= p < t1]
    # (несёт ли перенос участок, добавка к длительности): soft — ниже паспорта, hard — ниже на шаг
    c_soft = c_hard = (False, 0.0)
    hp, hr = (held.get("OIL_PRESS"), held.get("RPM")) if carry else (None, None)
    if hp is not None and hr is not None:
        p_rows = [(t0, float(hp[1]))] + p_rows
        r_rows = [(t0, float(hr[1]))] + r_rows
        if not pk or pk[0] > t0:
            pk = [t0] + pk
        ok = hr[1] >= prm["rpm_min"] and (prm["floor"] is None or hp[1] >= prm["floor"])
        # От начала идущего участка по ряду окна истории (held_before), без него —
        # от метки удержанной строки давления (у повтора того же значения она позже начала)
        base = _tz(hp[0])
        ps = held.get("_passport_since") or (None, None)
        if ok and hp[1] < prm["limit"]:
            c_soft = (True, max(0.0, (t0 - (ps[0] or base)).total_seconds()))
        if ok and hp[1] < prm["limit"] - prm["step"]:
            c_hard = (True, max(0.0, (t0 - (ps[1] or base)).total_seconds()))
    runs = oil_passport_runs(p_rows, r_rows, pk, gaps, prm)
    series = runs["series"]
    min_run = prm["min_run"]

    def qual(r, cr):
        carried = cr[0] and r["start"] == t0
        return r["sec"] + (cr[1] if carried else 0.0), carried

    hard_ok = [r for r in runs["hard"] if qual(r, c_hard)[0] >= min_run]
    out = []
    for r in runs["soft"]:
        sec, carried = qual(r, c_soft)
        if sec < min_run:
            continue
        ongoing = bool(r.get("open")) and not trail_pause
        hh = [h for h in hard_ok if r["start"] <= h["start"] and h["end"] <= r["end"]]
        lh = max(hh, key=lambda h: h["end"]) if hh else None
        # Живая копия после давнего нарушения — хвост участка после него в пределах шага
        tf = lh["end"] if lh is not None and lh["end"] < r["end"] else r["start"]
        tail = passport_instep(series, prm, tf, r["end"], runs["ts"])
        out.append({
            "_s": r["start"], "_e": t1 if ongoing else r["end"], "ongoing": ongoing,
            "carried": carried, "role": "OIL_PRESS", "_pts": [], "sec": sec,
            "min": r["min"], "instep_min": r.get("instep_min"),
            "last_value": min(tail) if tail else r["min"],
            "_hard": [{"_s": h["start"], "min": h["min"],
                       "_e": t1 if (ongoing and h.get("open")) else h["end"],
                       "ongoing": bool(ongoing and h.get("open"))} for h in hh],
        })
    return out


# ── Кластеры ────────────────────────────────────────────────────────────────

def _union_sec(iv: list[tuple[datetime, datetime]]) -> float:
    tot, cs, ce = 0.0, None, None
    for s, e in sorted(iv):
        if ce is None or s > ce:
            if ce is not None:
                tot += (ce - cs).total_seconds()
            cs, ce = s, e
        elif e > ce:
            ce = e
    if ce is not None:
        tot += (ce - cs).total_seconds()
    return tot


def _rnd(v: float | None, d: int = 4) -> float | None:
    return round(float(v), d) if v is not None else None


def _clusters(spans: list[dict], gl: list[tuple[datetime, datetime]], key: str,
              n: float = LIVE_RECENT_SEC) -> list[dict[str, Any]]:
    """Склейка участков в кластеры: пересекаются или между ними ≤ n видимого времени.

    Публичные поля (метки — ISO): start, end, ongoing, carried, viol_sec (чистое
    время — объединение участков), blind_sec (дыры внутри кластера), vmin, vmax,
    spans (число участков); у LIMIT_PROXIMITY ещё roles — участки по ролям; у
    паспорта — run_sec, hard, hard_min, hard_until, hard_ongoing, instep_min,
    last_value. Служебные (с «_») — участки и отсчёты, только в памяти."""
    groups: list[dict[str, Any]] = []
    for s in sorted(spans, key=lambda x: (x["_s"], x["_e"])):
        if groups:
            g = groups[-1]
            if s["_s"] <= g["_e"] or visible_sec(g["_e"], s["_s"], gl) <= n:
                g["_spans"].append(s)
                if s["_e"] > g["_e"]:
                    g["_e"] = s["_e"]
                continue
        groups.append({"_s": s["_s"], "_e": s["_e"], "_spans": [s]})
    out = []
    for g in groups:
        sp = g["_spans"]
        c: dict[str, Any] = {
            "start": _iso(g["_s"]), "end": _iso(g["_e"]),
            "ongoing": any(s.get("ongoing") for s in sp),
            "carried": any(s.get("carried") for s in sp if s["_s"] == g["_s"]),
            "viol_sec": round(_union_sec([(s["_s"], s["_e"]) for s in sp]), 1),
            "blind_sec": round(_overlap(gl, g["_s"], g["_e"]), 1),
            "spans": len(sp),
        }
        if key == "OIL_DILUTION":
            mins = [s["min"] for s in sp if s.get("min") is not None]
            c["vmin"], c["vmax"] = _rnd(min(mins)) if mins else None, None
            c["run_sec"] = round(max(s["sec"] for s in sp), 1)
            inst = [s["instep_min"] for s in sp if s.get("instep_min") is not None]
            c["instep_min"] = _rnd(min(inst)) if inst else None
            c["last_value"] = _rnd(max(sp, key=lambda s: s["_e"])["last_value"])
            hh = [h for s in sp for h in s["_hard"]]
            c["hard"] = bool(hh)
            if hh:
                lh = max(hh, key=lambda h: h["_e"])
                c["hard_min"] = _rnd(min(h["min"] for h in hh))
                c["hard_until"] = _iso(lh["_e"])
                c["hard_ongoing"] = bool(lh["ongoing"])
        else:
            vals = [p[1] for s in sp for p in s["_pts"]]
            c["vmin"] = _rnd(min(vals)) if vals else None
            c["vmax"] = _rnd(max(vals)) if vals else None
        if key == "LIMIT_PROXIMITY":
            roles: dict[str, Any] = {}
            for r in sorted({s["role"] for s in sp if s.get("role")}):
                rs = [s for s in sp if s.get("role") == r]
                rv = [p[1] for s in rs for p in s["_pts"]]
                roles[r] = {"spans": len(rs),
                            "viol_sec": round(_union_sec([(s["_s"], s["_e"]) for s in rs]), 1),
                            "vmin": _rnd(min(rv)) if rv else None,
                            "vmax": _rnd(max(rv)) if rv else None}
            c["roles"] = roles
        c["_s"], c["_e"], c["_spans"] = g["_s"], g["_e"], sp
        out.append(c)
    return out


# ── Точка входа сегментатора ────────────────────────────────────────────────

def segment_spans(
    seg_by_addr: dict[int, list[dict]],
    packets: list[datetime] | None,
    cfg,
    t0: datetime,
    t1: datetime,
    gaps: list[dict] | None,
    held: dict[str, Any] | None = None,
    carry: bool = False,
    horizon: datetime | None = None,
) -> dict[str, list[dict[str, Any]] | None]:
    """Участки и кластеры нарушений рабочей части сегмента [t0, t1): {ключ: [кластер]}.

    seg_by_addr — строки сегмента (берутся только реальные, без копий
    forward-fill); packets — моменты пакетов связи (None — по пингу, как у
    частоты опроса); gaps — дыры data_gaps сегмента; held — удержанные значения
    на начало сегмента (held_before): частота — опросу оборотов всегда, остальное
    — переносу, только при carry (REPORT_START без разгона); horizon — край
    данных для правила B на живом краю (dip_needs_freq: false), только у
    открытого окна. None — окно закрыто (рез, смена режима): будущих строк не
    будет, отсчёт без следующего решается по себе (ревью v5.1.21, analytics-3:
    горизонт = рез терял последний отсчёт провала в минуте до 09:00).
    Ключа без участков в ответе нет. Ключ со сбоем расчёта — None (v5.1.23,
    п.7): каждый ключ считается в своём try, сбой одного не гасит соседей,
    детектор этого ключа идёт по подсегменту, как при live_spans=None. Сбой
    общей части (пакеты, разрывы) — исключение наружу, в страховку сегментатора.
    O(n) по строкам адреса."""
    t0, t1 = _tz(t0), _tz(t1)
    if t1 <= t0:
        return {}
    held = held or {}
    prm = rpm_freq_params(cfg)
    if packets is None:
        packets = packet_times(seg_by_addr, prm)
    gl = _merge_gaps(gaps)
    brk = _breaks(packets, gl, t0, t1, prm["packet_max_gap_sec"])
    trail_pause = bool(brk) and brk[-1][1] is None and brk[-1][2] == "pause"
    out: dict[str, list[dict[str, Any]] | None] = {}

    def add(key: str, spans: list[dict]) -> None:
        for s in spans:
            s["blind_sec"] = round(_overlap(gl, s["_s"], s["_e"]), 1)
        if spans:
            out[key] = _clusters(spans, gl, key)

    def guarded(key: str, calc: Callable[[], None]) -> None:
        # Ключ — в своём try: до v5.1.23 сбой одного ключа (общий try
        # сегментатора) переводил на подсегмент все четыре детектора
        try:
            calc()
        except Exception:
            out[key] = None
            logger.warning("Участки ключа %s [%s, %s) не посчитаны — детектор по подсегменту",
                           key, _iso(t0), _iso(t1), exc_info=True)

    # Просадка оборотов по v* (В1)
    def _rpm() -> None:
        pts, init = _rpm_points(seg_by_addr, cfg, prm, t0, t1, held, carry, gaps, packets,
                                _tz(horizon) if horizon else None)
        add("RPM_UNDERSPEED", _spans(pts, t0, t1, brk, init, "RPM"))
    guarded("RPM_UNDERSPEED", _rpm)

    # Заброс оборотов по v* (v5.1.23, п.2, В2): обороты и k·f_max опроса выше
    # порога; уставки разноса в KB нет — ключа нет
    def _over() -> None:
        oprm = overspeed_params(cfg, prm)
        if oprm is None:
            return
        pts, init = _over_points(seg_by_addr, cfg, oprm, t0, t1, held, carry, gaps, packets)
        add("RPM_OVERSPEED", _spans(pts, t0, t1, brk, init, "RPM"))
    guarded("RPM_OVERSPEED", _over)

    # Близость к уставкам: три роли — один набор кластеров, пол давления (В3)
    def _limit() -> None:
        lp: list[dict] = []
        for chk in limit_checks(cfg):
            addr = cfg.role_to_addr(chk["role"])
            if addr is None:
                continue
            bad = (lambda c: (lambda v: limit_bad(c, v)))(chk)
            rows = _real_rows(seg_by_addr.get(addr), t0, t1)
            lp += _spans(_value_points(rows, bad, chk["floor"]), t0, t1, brk,
                         _held_init(held, chk["role"], carry, bad, chk["floor"]), chk["role"])
        add("LIMIT_PROXIMITY", lp)
    guarded("LIMIT_PROXIMITY", _limit)

    # ОЖ у уставки HCT Warning (COOLING_FAILURE, ветка близости)
    def _cooling() -> None:
        c_addr = cfg.role_to_addr("COOLANT_TEMP")
        if c_addr is None:
            return
        near = cooling_proximity(cfg)["near"]
        bad_c = lambda v: v >= near  # noqa: E731
        rows = _real_rows(seg_by_addr.get(c_addr), t0, t1)
        add("COOLING_FAILURE", _spans(_value_points(rows, bad_c), t0, t1, brk,
                                      _held_init(held, "COOLANT_TEMP", carry, bad_c), "COOLANT_TEMP"))
    guarded("COOLING_FAILURE", _cooling)

    # Паспорт давления масла (OIL_DILUTION)
    guarded("OIL_DILUTION", lambda: add("OIL_DILUTION", _passport_spans(
        seg_by_addr, cfg, t0, t1, gaps, held, carry, packets, trail_pause)))
    return out


# ── Детекторам: что из участков попадает в подсегмент ───────────────────────

def _meets(s: dict, a: datetime, b: datetime) -> bool:
    """Участок пересекает [a, b) (точечный — если лежит в нём)."""
    if s["_s"] >= b:
        return False
    return s["_e"] > a or (s["_e"] == s["_s"] and s["_s"] >= a)


def sub_hits(clusters: list[dict] | None, a: datetime, b: datetime,
             role: str | None = None) -> list[tuple[dict, list[dict]]]:
    """Кластеры, у которых есть участок (своей роли), пересекающий [a, b), — с этими участками."""
    a, b = _tz(a), _tz(b)
    out = []
    for c in clusters or []:
        sp = [s for s in c.get("_spans") or []
              if (role is None or s.get("role") == role) and _meets(s, a, b)]
        if sp:
            out.append((c, sp))
    return out


def sub_samples(spans: list[dict], a: datetime, b: datetime) -> list[tuple]:
    """Отсчёты нарушения участков внутри [a, b) и удержанный на a (участок начался раньше)."""
    a, b = _tz(a), _tz(b)
    out = []
    for s in spans:
        before = None
        for p in s["_pts"]:
            if p[0] < a:
                before = p
            elif p[0] < b:
                out.append(p)
        if before is not None:
            out.append(before)
    return out


def public(c: dict[str, Any]) -> dict[str, Any]:
    """Кластер для values.live: без служебных полей, копия (JSON)."""
    return copy.deepcopy({k: v for k, v in c.items() if not k.startswith("_")})


def live_values(hits: list[tuple[dict, list[dict]]], a: datetime) -> dict[str, Any]:
    """Блок values.live детекции: пересекающие подсегмент кластеры, onset — начало
    последнего, first — первое нарушение внутри подсегмента, until/ongoing — конец
    последнего кластера (на краю окна — край окна и ongoing)."""
    a = _tz(a)
    cl = [public(c) for c, _ in hits]
    first = min(max(s["_s"], a) for _, sp in hits for s in sp)
    return {"clusters": cl, "onset": cl[-1]["start"], "first": _iso(first),
            "until": cl[-1]["end"], "ongoing": cl[-1]["ongoing"]}


# ── Свежесть: живая ли детекция на конец (П4) ───────────────────────────────

def _sub_attr(sub: Any, name: str) -> Any:
    """Поле подсегмента — объекта или словаря (заглушки тестов без data_gaps/t_end)."""
    return sub.get(name) if isinstance(sub, dict) else getattr(sub, name, None)


def _det_dict(d: Any) -> dict | None:
    if isinstance(d, dict):
        return d
    return d.to_dict() if hasattr(d, "to_dict") else None


def live_block(det: Any) -> dict | None:
    """values.live детекции (Detection или dict); None — детекция без участков."""
    dd = _det_dict(det)
    lv = ((dd or {}).get("values") or {}).get("live")
    return lv if isinstance(lv, dict) else None


def live_end(lv: dict) -> tuple[datetime | None, bool]:
    """(конец последнего кластера, идёт ли он) из блока values.live."""
    cl = lv.get("clusters") or []
    last = cl[-1] if cl and isinstance(cl[-1], dict) else {}
    until = _as_dt(lv.get("until") or last.get("end"))
    ongoing = lv.get("ongoing") if "ongoing" in lv else last.get("ongoing")
    return until, bool(ongoing)


def subs_gaps(subs: list | None) -> list[tuple[datetime, datetime]]:
    """Дыры связи подсегментов (data_gaps, синтетический хвост открытого окна среди
    них) — объединённые интервалы."""
    raw: list = []
    for sub in subs or []:
        raw.extend(_sub_attr(sub, "data_gaps") or [])
    return _merge_gaps(raw)


def detection_live(det: Any, horizon: Any, gaps: Any = None) -> bool | None:
    """Жива ли детекция на момент horizon (v5.1.21, П4; одна для движка и сводки).

    Живая — если её последний кластер идёт или кончился не больше
    LIVE_RECENT_SEC видимого времени назад: (horizon − end) − дыры ≤ N (В2).
    Кончившееся до обрыва связи за время обрыва не устаревает (догма 19.09).
    None — у детекции нет values.live: вызывающий применяет прежнюю логику
    (детекция последнего подсегмента — живая). gaps — data_gaps / дыры
    подсегментов / кортежи."""
    lv = live_block(det)
    if lv is None:
        return None
    until, ongoing = live_end(lv)
    if ongoing:
        return True
    h = _as_dt(horizon)
    if h is None or until is None or until >= h:
        return True
    gl = gaps if gaps and isinstance(gaps[0], tuple) else _merge_gaps(gaps)
    return visible_sec(until, h, gl) <= LIVE_RECENT_SEC


def alive_detections(subs: list | None, horizon: Any = None, gaps: Any = None) -> list[dict]:
    """Детекции, живые на конец сегмента (v5.1.21, П4): dict-копии (to_dict).

    Последний подсегмент: детекция без values.live — как раньше (есть — живая);
    с live — если жив её кластер (detection_live). Взгляд назад: сценарий без
    живой детекции с live в последнем подсегменте берётся из самого позднего
    прежнего подсегмента того же сегмента, где у него есть детекция с live, —
    если её кластер жив. Смена зоны нагрузки нарушение не обрывает (у #35872
    четыре границы за 16 мин): кластер считается на сегменте, а детекция есть
    только в подсегментах, которые пересекает его участок. Самый поздний
    подсегмент несёт и самый поздний кластер, дальше назад не смотрим.
    horizon — по умолчанию конец последнего подсегмента; gaps — по умолчанию
    дыры подсегментов. Всё в памяти, без запросов."""
    subs = list(subs or [])
    if not subs:
        return []
    last = subs[-1]
    if horizon is None:
        horizon = _sub_attr(last, "t_end") or _sub_attr(last, "t_start")
    gl = subs_gaps(subs) if gaps is None else (
        gaps if gaps and isinstance(gaps[0], tuple) else _merge_gaps(gaps))
    out: list[dict] = []
    found: set = set()
    for d in _sub_attr(last, "detections") or []:
        dd = _det_dict(d)
        if dd is None:
            continue
        alive = detection_live(dd, horizon, gl)
        if alive is False:
            continue
        if alive:
            found.add(dd.get("scenario"))
        out.append(dd)
    for sub in reversed(subs[:-1]):
        here: dict[Any, list[dict]] = {}
        for d in _sub_attr(sub, "detections") or []:
            dd = _det_dict(d)
            if dd is None or live_block(dd) is None or dd.get("scenario") in found:
                continue
            here.setdefault(dd.get("scenario"), []).append(dd)
        for sc, lst in here.items():
            found.add(sc)
            out.extend(x for x in lst if detection_live(x, horizon, gl))
    return out
