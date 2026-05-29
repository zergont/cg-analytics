"""Вычисление метрик подсегмента: S (мгновенные), V (скоростные), C (агрегаты).

Все пороги и параметры берутся из AnalyticsConfig — в коде магических чисел нет.
Алгоритмы соответствуют ТЗ, разделы 6.1–6.2.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from .contract import Characteristic, DerivedMetrics
from .config import AnalyticsConfig


# ── Вспомогательные функции ──────────────────────────────────────────────────

def _tz(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2


def _mad(values: list[float]) -> float | None:
    """Median Absolute Deviation = median(|xi - median(x)|)."""
    if not values:
        return None
    med = _median(values)
    if med is None:
        return None
    return _median([abs(v - med) for v in values])


def _linear_slope_per_hour(points: list[tuple[datetime, float]]) -> float | None:
    """Наклон линейного тренда в ед./час по реальным device-timestamp."""
    if len(points) < 2:
        return None
    t0 = _tz(points[0][0])
    # Преобразуем время в часы от t0
    xs = [((_tz(ts) - t0).total_seconds() / 3600) for ts, _ in points]
    ys = [v for _, v in points]
    n = len(xs)
    sum_x = sum(xs)
    sum_y = sum(ys)
    sum_xy = sum(x * y for x, y in zip(xs, ys))
    sum_x2 = sum(x * x for x in xs)
    denom = n * sum_x2 - sum_x * sum_x
    if abs(denom) < 1e-12:
        return None
    return (n * sum_xy - sum_x * sum_y) / denom


def _is_gap_between(
    ts1: datetime,
    ts2: datetime,
    gaps: list[dict[str, Any]],
    heartbeat_sec: float,
    max_multiplier: float,
) -> bool:
    """True если между ts1 и ts2 есть разрыв данных (из data_gaps или по heartbeat)."""
    delta = (_tz(ts2) - _tz(ts1)).total_seconds()
    if delta > heartbeat_sec * max_multiplier:
        return True
    for gap in gaps:
        gs = _tz(gap["gap_start"])
        ge = gap.get("gap_end")
        if ge is not None:
            ge = _tz(ge)
        else:
            ge = _tz(ts2)  # активный gap
        if gs < _tz(ts2) and ge > _tz(ts1):
            return True
    return False


# ── Характеристики непрерывных ролей (C-метрики) ────────────────────────────

def compute_characteristics(
    by_addr: dict[int, list[dict[str, Any]]],
    seg_start: datetime,
    seg_end: datetime,
    cfg: AnalyticsConfig,
) -> dict[str, Any]:
    """Вычислить Characteristic для каждой аналоговой роли в подсегмент.

    Возвращает {role: Characteristic.to_dict()}.
    Срезы вне [seg_start, seg_end) игнорируются.
    """
    result: dict[str, Any] = {}
    t_start = _tz(seg_start)
    t_end = _tz(seg_end)

    for addr, rows in by_addr.items():
        meta = cfg.register_map.get(addr, {})
        if meta.get("kind") != "analog":
            continue
        role = meta.get("role", f"addr_{addr}")
        unit = meta.get("unit", "")

        # Только валидные значения внутри окна
        window: list[tuple[datetime, float]] = []
        for r in rows:
            ts = _tz(r["ts"])
            if ts < t_start or ts >= t_end:
                continue
            v = r.get("value")
            if v is None:
                continue
            try:
                fv = float(v)
                if not math.isfinite(fv):
                    continue
            except (TypeError, ValueError):
                continue
            window.append((ts, fv))

        if not window:
            continue

        vals = [v for _, v in window]
        med = _median(vals)
        mad_v = _mad(vals)
        slope = _linear_slope_per_hour(window)

        char = Characteristic(
            role=role,
            unit=unit,
            sample_count=len(vals),
            median=round(med, 3) if med is not None else None,
            mad=round(mad_v, 3) if mad_v is not None else None,
            min=round(min(vals), 3),
            max=round(max(vals), 3),
            value_start=round(vals[0], 3),
            value_end=round(vals[-1], 3),
            slope=round(slope, 5) if slope is not None else None,
        )
        result[role] = char.to_dict()

    return result


# ── Derived metrics (мгновенные S и скоростные V → агрегируем в подсегмент) ─

def compute_derived_metrics(
    by_addr: dict[int, list[dict[str, Any]]],
    seg_start: datetime,
    seg_end: datetime,
    gaps: list[dict[str, Any]],
    cfg: AnalyticsConfig,
) -> DerivedMetrics:
    """Вычислить производные метрики за подсегмент.

    Параметры (пороги) берутся из cfg — не захардкожены.
    """
    t_start = _tz(seg_start)
    t_end = _tz(seg_end)
    heartbeat = float(cfg.seg("data_quality", "heartbeat_nominal_sec", default=30))
    max_mult = float(cfg.seg("data_quality", "heartbeat_max_multiplier", default=3))

    def _rows_in(addr: int) -> list[tuple[datetime, float]]:
        rows = by_addr.get(addr, [])
        result = []
        for r in rows:
            ts = _tz(r["ts"])
            if ts < t_start or ts >= t_end:
                continue
            v = r.get("value")
            if v is None:
                continue
            try:
                fv = float(v)
                if math.isfinite(fv):
                    result.append((ts, fv))
            except (TypeError, ValueError):
                pass
        return result

    def _addr(role: str) -> int | None:
        return cfg.role_to_addr(role)

    # Токи по фазам
    i1 = _rows_in(_addr("CURRENT_L1") or 0) if _addr("CURRENT_L1") else []
    i2 = _rows_in(_addr("CURRENT_L2") or 0) if _addr("CURRENT_L2") else []
    i3 = _rows_in(_addr("CURRENT_L3") or 0) if _addr("CURRENT_L3") else []

    # Перекос тока: синхронизируем по ts (берём ближайшие)
    imbalances: list[float] = []
    if i1 and i2 and i3:
        # Берём временной ряд по shortest
        i1d = {ts: v for ts, v in i1}
        i2d = {ts: v for ts, v in i2}
        i3d = {ts: v for ts, v in i3}
        common_ts = sorted(set(i1d) & set(i2d) & set(i3d))
        for ts in common_ts:
            v1, v2, v3 = i1d[ts], i2d[ts], i3d[ts]
            i_avg = (v1 + v2 + v3) / 3
            if i_avg > 1e-6:
                imb = max(abs(v1 - i_avg), abs(v2 - i_avg), abs(v3 - i_avg)) / i_avg * 100
                imbalances.append(imb)

    current_imbalance_pct_max = max(imbalances) if imbalances else None
    current_imbalance_pct_med = _median(imbalances) if imbalances else None

    # Перекос мощности
    p1 = _rows_in(_addr("ACTIVE_POWER_L1") or 0) if _addr("ACTIVE_POWER_L1") else []
    p2 = _rows_in(_addr("ACTIVE_POWER_L2") or 0) if _addr("ACTIVE_POWER_L2") else []
    p3 = _rows_in(_addr("ACTIVE_POWER_L3") or 0) if _addr("ACTIVE_POWER_L3") else []

    power_imbalances: list[float] = []
    if p1 and p2 and p3:
        p1d = {ts: v for ts, v in p1}
        p2d = {ts: v for ts, v in p2}
        p3d = {ts: v for ts, v in p3}
        common_ts = sorted(set(p1d) & set(p2d) & set(p3d))
        for ts in common_ts:
            v1, v2, v3 = p1d[ts], p2d[ts], p3d[ts]
            p_avg = (v1 + v2 + v3) / 3
            if p_avg > 1e-6:
                imb = max(abs(v1 - p_avg), abs(v2 - p_avg), abs(v3 - p_avg)) / p_avg * 100
                power_imbalances.append(imb)

    power_imbalance_pct_max = max(power_imbalances) if power_imbalances else None

    # Длительность с перекосом выше порога
    imb_thr = float(cfg.det("PHASE_IMBALANCE", "current_imbalance_warning_pct", default=12.0))
    imbalance_duration_sec: float | None = None
    if imbalances and i1:
        i1d = {ts: v for ts, v in i1}
        i2d = {ts: v for ts, v in _rows_in(_addr("CURRENT_L2") or 0)}
        i3d = {ts: v for ts, v in _rows_in(_addr("CURRENT_L3") or 0)}
        common_ts_sorted = sorted(set(i1d) & set(i2d) & set(i3d))
        dur_above = 0.0
        for j, ts in enumerate(common_ts_sorted[:-1]):
            v1, v2, v3 = i1d[ts], i2d[ts], i3d[ts]
            i_avg = (v1 + v2 + v3) / 3
            if i_avg > 1e-6:
                imb = max(abs(v1 - i_avg), abs(v2 - i_avg), abs(v3 - i_avg)) / i_avg * 100
                if imb > imb_thr:
                    dt = (common_ts_sorted[j + 1] - ts).total_seconds()
                    dur_above += dt
        imbalance_duration_sec = dur_above if dur_above > 0 else None

    # S-consistency: |√(P²+Q²) - S_тег|
    p_total = _rows_in(_addr("ACTIVE_POWER_TOTAL") or 0) if _addr("ACTIVE_POWER_TOTAL") else []
    q_total = _rows_in(_addr("REACTIVE_POWER_TOTAL") or 0) if _addr("REACTIVE_POWER_TOTAL") else []
    s_total = _rows_in(_addr("APPARENT_POWER_TOTAL") or 0) if _addr("APPARENT_POWER_TOTAL") else []
    s_consistencies: list[float] = []
    if p_total and q_total and s_total:
        pd = {ts: v for ts, v in p_total}
        qd = {ts: v for ts, v in q_total}
        sd = {ts: v for ts, v in s_total}
        for ts in sorted(set(pd) & set(qd) & set(sd)):
            computed_s = math.sqrt(pd[ts] ** 2 + qd[ts] ** 2)
            s_consistencies.append(abs(computed_s - sd[ts]))
    s_consistency_max = max(s_consistencies) if s_consistencies else None

    # PF-consistency: |P/S - PF_тег|
    pf_rows = _rows_in(_addr("POWER_FACTOR") or 0) if _addr("POWER_FACTOR") else []
    pf_consistencies: list[float] = []
    if p_total and s_total and pf_rows:
        pd = {ts: v for ts, v in p_total}
        sd = {ts: v for ts, v in s_total}
        pfd = {ts: v for ts, v in pf_rows}
        for ts in sorted(set(pd) & set(sd) & set(pfd)):
            if abs(sd[ts]) > 1e-6:
                pf_consistencies.append(abs(pd[ts] / sd[ts] - pfd[ts]))
    pf_consistency_max = max(pf_consistencies) if pf_consistencies else None

    # Oil-coolant delta
    oil_t = _rows_in(_addr("OIL_TEMP") or 0) if _addr("OIL_TEMP") else []
    cool_t = _rows_in(_addr("COOLANT_TEMP") or 0) if _addr("COOLANT_TEMP") else []
    oil_coolant_deltas: list[float] = []
    if oil_t and cool_t:
        otd = {ts: v for ts, v in oil_t}
        ctd = {ts: v for ts, v in cool_t}
        for ts in sorted(set(otd) & set(ctd)):
            oil_coolant_deltas.append(otd[ts] - ctd[ts])
    oil_coolant_delta_med = _median(oil_coolant_deltas)
    oil_coolant_delta_max = max(oil_coolant_deltas) if oil_coolant_deltas else None

    # RPM stability
    rpm_rows = _rows_in(_addr("RPM") or 0) if _addr("RPM") else []
    rpm_vals = [v for _, v in rpm_rows]
    rpm_stability_mad = _mad(rpm_vals)

    # Freq stability
    freq_rows = _rows_in(_addr("FREQUENCY") or 0) if _addr("FREQUENCY") else []
    freq_vals = [v for _, v in freq_rows]
    freq_stability_mad = _mad(freq_vals)

    # dP_dt_max (кВт/с) — максимальная скорость изменения мощности
    dP_dt_max = _max_speed(p_total, gaps, heartbeat, max_mult, absolute=True)

    # dRPM_dt_max
    dRPM_dt_max = _max_speed(rpm_rows, gaps, heartbeat, max_mult, absolute=True)

    # dCoolant_dt_max (°C/час) — максимальная скорость роста Т ОЖ
    dCoolant_dt_max = _max_speed_per_hour(cool_t, gaps, heartbeat, max_mult, positive=True)

    # dOil_press_dt_min (кПа/с) — максимальная скорость ПАДЕНИЯ давления масла (отрицательная)
    oil_press = _rows_in(_addr("OIL_PRESS") or 0) if _addr("OIL_PRESS") else []
    dOil_press_dt_min = _min_speed(oil_press, gaps, heartbeat, max_mult)

    # coolant_below_60_sec — время с Т ОЖ < порога
    below_c = float(cfg.thr("coolant_temperature", "tunable", "below_60_risk_threshold_c", default=60.0))
    coolant_below_60_sec = _time_below(cool_t, below_c)

    return DerivedMetrics(
        current_imbalance_pct_max=_r(current_imbalance_pct_max),
        current_imbalance_pct_med=_r(current_imbalance_pct_med),
        power_imbalance_pct_max=_r(power_imbalance_pct_max),
        imbalance_duration_sec=_r(imbalance_duration_sec),
        s_consistency_max=_r(s_consistency_max),
        pf_consistency_max=_r(pf_consistency_max),
        oil_coolant_delta_med=_r(oil_coolant_delta_med),
        oil_coolant_delta_max=_r(oil_coolant_delta_max),
        rpm_stability_mad=_r(rpm_stability_mad),
        freq_stability_mad=_r(freq_stability_mad),
        dP_dt_max=_r(dP_dt_max),
        dRPM_dt_max=_r(dRPM_dt_max),
        dCoolant_dt_max=_r(dCoolant_dt_max),
        dOil_press_dt_min=_r(dOil_press_dt_min),
        coolant_below_60_sec=_r(coolant_below_60_sec),
    )


# ── Вспомогательные расчёты скоростей ────────────────────────────────────────

def _max_speed(
    series: list[tuple[datetime, float]],
    gaps: list[dict],
    heartbeat: float,
    max_mult: float,
    absolute: bool = False,
) -> float | None:
    """Максимальная скорость изменения параметра (ед./с)."""
    speeds: list[float] = []
    for i in range(len(series) - 1):
        ts1, v1 = series[i]
        ts2, v2 = series[i + 1]
        if _is_gap_between(ts1, ts2, gaps, heartbeat, max_mult):
            continue
        dt = (_tz(ts2) - _tz(ts1)).total_seconds()
        if dt < 1e-6:
            continue
        speed = (v2 - v1) / dt
        speeds.append(abs(speed) if absolute else speed)
    return max(speeds) if speeds else None


def _max_speed_per_hour(
    series: list[tuple[datetime, float]],
    gaps: list[dict],
    heartbeat: float,
    max_mult: float,
    positive: bool = False,
) -> float | None:
    """Максимальная скорость изменения параметра (ед./час), только позитивная если positive=True."""
    speeds: list[float] = []
    for i in range(len(series) - 1):
        ts1, v1 = series[i]
        ts2, v2 = series[i + 1]
        if _is_gap_between(ts1, ts2, gaps, heartbeat, max_mult):
            continue
        dt_h = (_tz(ts2) - _tz(ts1)).total_seconds() / 3600
        if dt_h < 1e-9:
            continue
        speed = (v2 - v1) / dt_h
        if positive and speed <= 0:
            continue
        speeds.append(speed)
    return max(speeds) if speeds else None


def _min_speed(
    series: list[tuple[datetime, float]],
    gaps: list[dict],
    heartbeat: float,
    max_mult: float,
) -> float | None:
    """Минимальная (наиболее отрицательная) скорость изменения параметра (ед./с)."""
    speeds: list[float] = []
    for i in range(len(series) - 1):
        ts1, v1 = series[i]
        ts2, v2 = series[i + 1]
        if _is_gap_between(ts1, ts2, gaps, heartbeat, max_mult):
            continue
        dt = (_tz(ts2) - _tz(ts1)).total_seconds()
        if dt < 1e-6:
            continue
        speed = (v2 - v1) / dt
        if speed < 0:
            speeds.append(speed)
    return min(speeds) if speeds else None


def _time_below(
    series: list[tuple[datetime, float]],
    threshold: float,
) -> float | None:
    """Суммарное время с значением < threshold (ступенчатая интегрирование)."""
    if not series:
        return None
    total = 0.0
    for i, (ts, v) in enumerate(series[:-1]):
        next_ts = series[i + 1][0]
        if v < threshold:
            total += (_tz(next_ts) - _tz(ts)).total_seconds()
    return total if total > 0 else None


def _r(v: float | None, decimals: int = 3) -> float | None:
    if v is None:
        return None
    return round(v, decimals)
