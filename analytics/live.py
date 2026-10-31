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

v5.1.30 «По событию» (решения владельца В-27.1, В-27.2):
- NEGATIVE_SEQUENCE (I₂), PHASE_CURRENT_SPREAD, NEUTRAL_CURRENT — по ряду фаз
  по пакетам (metrics.phase_series, тот же, что у метрик подсегмента): отсчёт
  без значения (ток фазы 0 у I₂, нет удержанного значения) закрывает участок;
  кластер — эпизод, только если в нём есть участок непрерывно не короче
  выдержки KB (duration_warning_sec, 60 с; время дыры data_gaps — не
  непрерывность). Короткий кластер отбрасывается. Эпизод I₂ — от начала до
  конца кластера, а не от начала подсегмента до суточного реза (у ДГУ №1
  15037/15061 — по 21 ч при превышении 96–160 с суммой);
- NO_LOAD_TOO_LONG — отрезки нагрузки LOAD_PCT ниже load_pct: короткий выход
  (до COKING_RISK.purge_dip_tolerance_sec, 60 с) отрезок не рвёт, дыра связи и
  пауза пакетов — рвут (как noload_reset аккумулятора). Нарушение — от
  «начало отрезка + (max_continuous_sec − перенос)» до конца отрезка; перенос
  через суточный рез — noload_run_sec аккумулятора на начало окна (только при
  переносе, REPORT_START без разгона). Раньше отрезок, кончившийся внутри
  подсегмента, терялся (27 из 42 за 60 дней), а эпизод начинался с начала
  подсегмента.

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

from .metrics import (
    nominal_current, oil_passport_params, oil_passport_runs, passport_instep, phase_params,
    phase_series,
)
from .rpm_freq import (
    _PACKET_TOL_SEC, build_poll, dip_marks, freq_ok, over_marks, overspeed_params, packet_times,
    rpm_freq_params,
)

logger = logging.getLogger(__name__)

# Окно свежести и склейки N (В2, 15.10.2026): участки, между которыми не больше
# N видимого времени, — один кластер; нарушение живо, пока идёт или кончилось
# не больше N назад. То же окно, что у паспорта давления с v5.1.12
LIVE_RECENT_SEC = 600

# Роли, чьи удержанные значения переносятся через начало окна (и частота опроса).
# v5.1.30: нагрузка — переносу отрезка «без нагрузки» через суточный рез
HELD_ROLES = ("RPM", "FREQUENCY", "OIL_TEMP", "COOLANT_TEMP", "OIL_PRESS", "LOAD_PCT")

# Ключи по ряду фаз (v5.1.30): (ключ, индекс в точке phase_series, порог в
# phase_params, роль участка)
_PHASE_KEYS = (("NEGATIVE_SEQUENCE", 1, "i2_thr", "I2"),
               ("PHASE_CURRENT_SPREAD", 2, "spread_thr", "PHASE_SPREAD"),
               ("NEUTRAL_CURRENT", 3, "neutral_thr", "NEUTRAL"))
# Ключи «по событию» v5.1.30: у их кластеров ещё visible_sec, longest_sec, smax
_EVENT_KEYS = frozenset(k for k, *_ in _PHASE_KEYS) | {"NO_LOAD_TOO_LONG"}

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


# ── Ряд фаз и «без нагрузки» (v5.1.30, «По событию») ───────────────────────

def _rows_any(rows: list[dict] | None, t0: datetime | None = None,
              t1: datetime | None = None) -> list[tuple[datetime, float]]:
    """Строки адреса с числом в [t0, t1) — вместе с копиями forward-fill первых
    пакетов (как у метрик подсегмента: начальное значение на старте)."""
    out = []
    for r in rows or []:
        v = r.get("value")
        if v is None:
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if fv != fv or fv in (float("inf"), float("-inf")):
            continue
        ts = _tz(r["ts"])
        if (t0 is not None and ts < t0) or (t1 is not None and ts >= t1):
            continue
        out.append((ts, fv))
    out.sort(key=lambda x: x[0])
    return out


def _longest_free(a: datetime, b: datetime, gl: list[tuple[datetime, datetime]]) -> float:
    """Самый длинный кусок [a, b] без дыр data_gaps, сек (непрерывность участка:
    время дыры — не подтверждение, что нарушение длилось)."""
    best, cur = 0.0, a
    for gs, ge in gl:
        if ge <= cur:
            continue
        if gs >= b:
            break
        if gs > cur:
            best = max(best, (gs - cur).total_seconds())
        cur = max(cur, ge)
        if cur >= b:
            return best
    if b > cur:
        best = max(best, (b - cur).total_seconds())
    return best


def _evidence_end(s: dict) -> datetime:
    """Конец участка для квалификации (ревью v5.1.30): у идущего — последний
    реальный отсчёт, а не край окна t1. Иначе один пакет выше порога, за
    которым обрыв или пауза до 90 с (ещё не разрыв), «держался бы непрерывно»
    61–89 с — фантом-эпизод, и в закрытом окне, если обрыв начался меньше чем
    за 90 с до реза; то же у хвоста [конец дыры, t1] без отсчётов после неё.
    Живой результат так не больше итогового, квалификация запаздывает не
    больше чем на пакет. Конец эпизода (_e, until) этим не меняется. «_ev» —
    явный конец (у отрезков «без нагрузки» — последний пакет: строки нагрузки
    редкие, значение держится, пока идут пакеты)."""
    if s.get("_ev") is not None:
        return max(s["_s"], s["_ev"])
    if not s.get("ongoing"):
        return s["_e"]
    pts = [p[0] for p in s.get("_pts") or [] if p[0] >= s["_s"]]
    return max(pts) if pts else s["_s"]


def _phase_series(seg_by_addr, cfg, t0, t1, gaps, packets) -> tuple[list[tuple], dict] | None:
    """Ряд фаз рабочей части [t0, t1) по пакетам (metrics.phase_series) и его
    параметры. None — считать не из чего (нет токов фаз в карте или
    номинального тока)."""
    prm = phase_params(cfg)
    if prm is None:
        return None
    r_addr = cfg.role_to_addr("RATING_KW")
    i_nom = nominal_current(cfg, [v for _, v in _rows_any(seg_by_addr.get(r_addr))]
                            if r_addr is not None else [])
    if i_nom <= 0:
        return None
    rows = {a: _rows_any(seg_by_addr.get(a), t0, t1) for a in prm["addrs"]}
    pk = packets[bisect.bisect_left(packets, t0):bisect.bisect_left(packets, t1)]
    return phase_series(rows, pk, gaps or [], prm, i_nom), prm


def _phase_spans(series: list[tuple], idx: int, thr: float, t0: datetime, t1: datetime,
                 brk: list[tuple[datetime, datetime | None, str]], role: str) -> list[dict]:
    """Участки «≥ порога» одного правила ряда фаз: отсчёт на каждый пакет, без
    значения — закрывает участок (у I₂ ток фазы 0 — не нарушение)."""
    pts = [(p[0], p[idx] is not None and p[idx] >= thr, p[idx], None) for p in series]
    return _spans(pts, t0, t1, brk, None, role)


def _noload_spans(seg_by_addr, cfg, t0: datetime, t1: datetime,
                  brk: list[tuple[datetime, datetime | None, str]],
                  gl: list[tuple[datetime, datetime]], held: dict[str, Any], carry: bool,
                  carry_sec: float, break_sec: float,
                  packets: list[datetime] | None = None, max_gap: float = 90.0) -> list[dict]:
    """Нарушения NO_LOAD_TOO_LONG рабочей части [t0, t1) (v5.1.30, В-27.2).

    Отрезок — строки LOAD_PCT ниже load_pct (значение держится между строками,
    пока идут пакеты): выход не дольше допуска (purge_dip_tolerance_sec) отрезок
    не рвёт; выход дольше — отрезок кончился на начале выхода; дыра data_gaps и
    пауза пакетов рвут отрезок на последнем пакете перед ними, после них
    значение неизвестно до свежей строки (как noload_reset аккумулятора).
    Длина — видимое время отрезка плюс перенос, если отрезок перенесён через
    начало окна (carry: удержанная на t0 нагрузка ниже порога — или идёт
    короткий выход, break_sec аккумулятора). Нарушение — участок [начало
    отрезка + (max_continuous_sec − перенос), конец отрезка], если длина больше
    нормы (строго, как у прежнего детектора). Участок — dict как у _spans, плюс
    run_start, run_sec (длина с переносом) и carry_sec.

    Длина идущего отрезка — до последнего пакета (_evidence_end, ревью
    v5.1.30): хвост без пакетов перед обрывом не досчитывает «850 + 80 с» до
    нарушения; конец эпизода — край окна, как раньше.

    Перенос — счётчик аккумулятора нагара (CokingRisk.noload_run_sec) на
    начало окна. Удержанной нагрузки на t0 нет (строка до реза за паузой
    пакетов длиннее 90 с, held_before её отбросил), а аккумулятор отрезок
    помнит — перенесённый отрезок идёт с t0, если на t0 нет дыры data_gaps и
    первый пакет окна не дальше паузы (ревью v5.1.30: иначе перенос терялся
    целиком). Правила аккумулятора и участков различаются: аккумулятор копит
    только время ниже порога (короткий выход в длину не входит, участок
    считает его видимым временем отрезка) и рвёт отрезок только на дыре
    data_gaps (пауза пакетов без строки data_gaps его не рвёт, участок — рвёт).
    Поэтому перенос — оценка снизу или сверху на величину коротких выходов и
    пауз до реза; на окне сравнение с аккумулятором — не равенство."""
    addr = cfg.role_to_addr("LOAD_PCT")
    if addr is None:
        return []
    pct = float(cfg.det("NO_LOAD_TOO_LONG", "load_pct", default=10))
    limit = float(cfg.det("NO_LOAD_TOO_LONG", "max_continuous_sec", default=900))
    tol = float(cfg.det("COKING_RISK", "purge_dip_tolerance_sec", default=60.0))
    rows = _real_rows(seg_by_addr.get(addr), t0, t1)
    pk = list(packets or [])
    ptol = timedelta(seconds=_PACKET_TOL_SEC)
    runs: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None

    def close(te: datetime, ongoing: bool = False) -> None:
        nonlocal cur
        cur["_e"] = max(te, cur["_s"])
        cur["ongoing"] = ongoing
        runs.append(cur)
        cur = None

    h = held.get("LOAD_PCT") if carry else None
    if h is not None:
        hv = float(h[1])
        if hv < pct:
            cur = {"_s": t0, "carried": True, "exit": None, "_pts": [(_tz(h[0]), hv, None)],
                   "last": t0}
        elif carry_sec > 0 and 0 < break_sec <= tol:
            # На резе шёл короткий выход внутри отрезка (аккумулятор его помнит)
            cur = {"_s": t0, "carried": True, "exit": t0 - timedelta(seconds=break_sec),
                   "_pts": [], "last": t0}
    elif carry and carry_sec > 0:
        # Удержанная нагрузка выпала (пауза до реза), аккумулятор отрезок помнит:
        # перенос — с t0, если на t0 связь была (нет дыры, первый пакет рядом)
        i0 = bisect.bisect_left(pk, t0)
        first = pk[i0] if i0 < len(pk) else None
        if (first is not None and (first - t0).total_seconds() <= max_gap
                and not any(gs <= t0 < ge for gs, ge in gl)):
            cur = {"_s": t0, "carried": True,
                   "exit": t0 - timedelta(seconds=break_sec) if 0 < break_sec <= tol else None,
                   "_pts": [], "last": t0}
    ri, bi = 0, 0
    nr, nb = len(rows), len(brk)
    while ri < nr or bi < nb:
        if bi < nb and (ri >= nr or rows[ri][0] > brk[bi][0] + ptol):
            a, _b, _kind = brk[bi]
            bi += 1
            if cur is not None:
                close(cur["exit"] if cur["exit"] is not None else max(a, cur["last"]))
            continue
        ts, v = rows[ri]
        ri += 1
        if cur is not None and cur["exit"] is not None \
                and (ts - cur["exit"]).total_seconds() > tol:
            close(cur["exit"])          # выход дольше допуска — отрезок кончился на его начале
        if v < pct:
            if cur is None:
                cur = {"_s": ts, "carried": False, "exit": None, "_pts": [], "last": ts}
            cur["exit"] = None
            cur["_pts"].append((ts, v, None))
            cur["last"] = ts
        elif cur is not None:
            if cur["exit"] is None:
                cur["exit"] = ts
            cur["last"] = ts
    if cur is not None:
        if cur["exit"] is not None:
            close(cur["exit"])          # на краю окна идёт выход — отрезок кончился на нём
        else:
            close(t1, ongoing=True)
    out: list[dict[str, Any]] = []
    for r in runs:
        c_sec = float(carry_sec) if r["carried"] and r["_s"] == t0 else 0.0
        # Идущий отрезок — до последнего пакета в нём (не до края окна)
        ev = r["_e"]
        if r["ongoing"]:
            j = bisect.bisect_left(pk, r["_e"]) - 1
            ev = max(r["_s"], r["last"], pk[j] if j >= 0 and pk[j] >= r["_s"] else r["_s"])
        total = visible_sec(r["_s"], ev, gl) + c_sec
        if total <= limit:
            continue
        vs = r["_s"] + timedelta(seconds=max(0.0, limit - c_sec))
        if r["_e"] <= vs or ev <= vs:
            continue
        # Отсчёты нарушения и удержанный на его начало (значения для vmin/vmax)
        before = [p for p in r["_pts"] if p[0] < vs]
        pts = ([before[-1]] if before else []) + [p for p in r["_pts"] if p[0] >= vs]
        out.append({"_s": vs, "_e": r["_e"], "_ev": ev, "ongoing": r["ongoing"],
                    "carried": bool(c_sec and vs == t0), "role": "LOAD_PCT", "_pts": pts,
                    "run_start": r["_s"], "run_sec": round(total, 1), "carry_sec": round(c_sec, 1)})
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
            vals = [p[1] for s in sp for p in s["_pts"] if p[1] is not None]
            c["vmin"] = _rnd(min(vals)) if vals else None
            c["vmax"] = _rnd(max(vals)) if vals else None
        if key in _EVENT_KEYS:
            # v5.1.30: видимое время нарушения (объединение участков без дыр),
            # самый длинный непрерывный участок без дыр (квалификация ряда фаз)
            # и устойчивый максимум — держался хотя бы два отсчёта подряд
            # (ревью E1: на переходном кадре одна фаза приходит раньше двух других)
            # Ревью v5.1.30: конец идущего участка — последний реальный отсчёт
            # (_evidence_end), а не край окна; пара максимума — соседние отсчёты
            # одного участка без дыры между ними
            iv = sorted((s["_s"], _evidence_end(s)) for s in sp)
            merged: list[list[datetime]] = []
            for a, b in iv:
                if merged and a <= merged[-1][1]:
                    merged[-1][1] = max(merged[-1][1], b)
                else:
                    merged.append([a, b])
            c["visible_sec"] = round(sum(visible_sec(a, b, gl) for a, b in merged), 1)
            c["longest_sec"] = round(max(_longest_free(s["_s"], _evidence_end(s), gl) for s in sp), 1)
            pairs = [min(p[1], q[1]) for s in sp for p, q in zip(s["_pts"], s["_pts"][1:])
                     if p[1] is not None and q[1] is not None and not _covered(gl, p[0], q[0])]
            c["smax"] = _rnd(max(pairs)) if pairs else c["vmax"]
        if key == "NO_LOAD_TOO_LONG":
            # Отрезок первого нарушения — того, что даёт начало кластера и
            # t_detected (ревью v5.1.30: «28 мин подряд» брались от второго)
            first = min(sp, key=lambda s: s["_s"])
            c["run_sec"] = first.get("run_sec")
            c["run_start"] = _iso(first["run_start"]) if first.get("run_start") else None
            c["carry_sec"] = first.get("carry_sec") or 0.0
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
    noload_carry_sec: float = 0.0,
    noload_break_sec: float = 0.0,
    phase_out: dict | None = None,
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
    noload_carry_sec / noload_break_sec (v5.1.30, В-27.2) — отрезок «без
    нагрузки» и короткий выход в нём на начало окна по аккумулятору нагара
    (CokingRisk.noload_run_sec / noload_break_sec), только при carry.
    phase_out — сюда кладётся посчитанный ряд фаз ("series": [(момент, I₂,
    перекос, нейтраль)]): сегментатор отдаёт его срезы метрикам подсегментов,
    чтобы не строить тот же ряд второй раз (v5.1.30, время цикла).
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

    # Ряд фаз (v5.1.30, В-27.1): I₂, перекос фаз, ток нейтрали — один ряд по
    # пакетам на три ключа; кластер — эпизод, только если в нём есть участок
    # непрерывно не короче выдержки KB. Сбой ряда — None у всех трёх
    phase_keys = [pk for pk in _PHASE_KEYS if cfg.det(pk[0], "enabled", default=True)]
    ser: tuple[list[tuple], dict] | None = None
    if phase_keys:
        try:
            ser = _phase_series(seg_by_addr, cfg, t0, t1, gaps, packets)
        except Exception:
            for pk in phase_keys:
                out[pk[0]] = None
            logger.warning("Ряд фаз [%s, %s) не посчитан — I₂, перекос и нейтраль по подсегменту",
                           _iso(t0), _iso(t1), exc_info=True)
            phase_keys = []

    def _phase(key: str, idx: int, thr_key: str, role: str) -> None:
        series, pprm = ser  # type: ignore[misc]
        min_run = float(cfg.det(key, "duration_warning_sec", default=60.0))
        add(key, _phase_spans(series, idx, pprm[thr_key], t0, t1, brk, role))
        if out.get(key):
            # Короткий кластер (нет участка непрерывно ≥ выдержки) — не эпизод
            out[key] = [c for c in out[key] if c["longest_sec"] >= min_run]
            if not out[key]:
                del out[key]
    if ser is not None:
        if phase_out is not None:
            phase_out["series"] = ser[0]
        for key, idx, thr_key, role in phase_keys:
            guarded(key, (lambda k, i, tk, r: (lambda: _phase(k, i, tk, r)))(key, idx, thr_key, role))

    # Без нагрузки дольше нормы (v5.1.30, В-27.2): нарушение — с момента
    # превышения нормы, перенос через рез — по аккумулятору нагара
    if cfg.det("NO_LOAD_TOO_LONG", "enabled", default=True):
        guarded("NO_LOAD_TOO_LONG", lambda: add("NO_LOAD_TOO_LONG", _noload_spans(
            seg_by_addr, cfg, t0, t1, brk, gl, held, carry,
            float(noload_carry_sec or 0.0) if carry else 0.0,
            float(noload_break_sec or 0.0) if carry else 0.0,
            packets, float(prm["packet_max_gap_sec"]))))
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
