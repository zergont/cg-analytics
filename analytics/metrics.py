# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Вычисление метрик подсегмента: S (мгновенные), V (скоростные), C (агрегаты).

Все пороги и параметры берутся из AnalyticsConfig — в коде магических чисел нет.
Алгоритмы соответствуют ТЗ, разделы 6.1–6.2.
"""
from __future__ import annotations

import cmath
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


def _linear_slope_per_second(points: list[tuple[datetime, float]]) -> float | None:
    """Наклон линейного тренда в ед./с по реальным device-timestamp.

    Единица /с выбрана для всех внутрисегментных/переходных метрик (Addendum v1.4):
    исключает абсурдные числа на коротких переходных сегментах
    (RPM/ч, Гц/ч при шаге 20с — нечитаемы; RPM/с и Гц/с — вменяемы).
    """
    if len(points) < 2:
        return None
    t0 = _tz(points[0][0])
    xs = [(_tz(ts) - t0).total_seconds() for ts, _ in points]  # в секундах
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

        # Все валидные значения в окне (включая forward-fill) — для агрегатов.
        # Только реальные измерения (is_carried_forward=False) — для slope/тренда.
        window: list[tuple[datetime, float]] = []
        window_real: list[tuple[datetime, float]] = []  # только реальные, без ff
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
            if not r.get("is_carried_forward", False):
                window_real.append((ts, fv))

        if not window:
            continue

        vals = [v for _, v in window]
        med = _median(vals)
        mad_v = _mad(vals)
        # Slope считается только по реальным измерениям — ff-копии не вносят
        # ложных ступенек при экстраполяции тренда.
        slope = _linear_slope_per_second(window_real if window_real else window)

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

    def _rows_in(addr: int, real_only: bool = False) -> list[tuple[datetime, float]]:
        """Выборка значений по адресу в окне подсегмента.

        real_only=True — пропускать строки с is_carried_forward=True.
        Используется для скоростных метрик (V), чтобы ff-строки не порождали
        артефактных скачков на длинных стабильных участках.
        """
        rows = by_addr.get(addr, [])
        result = []
        for r in rows:
            if real_only and r.get("is_carried_forward", False):
                continue
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

    # Длительность с грубым перекосом выше порога (справочно; БАГ исправлен:
    # теперь используем уже вычисленные i1/i2/i3, а не повторные _rows_in)
    imb_ref_thr = float(cfg.det("NEGATIVE_SEQUENCE", "factory_threshold_pct", default=12.0))
    imbalance_duration_sec: float | None = None
    if imbalances and i1 and i2 and i3:
        i1d_map = {ts: v for ts, v in i1}
        i2d_map = {ts: v for ts, v in i2}
        i3d_map = {ts: v for ts, v in i3}
        common_ts_sorted = sorted(set(i1d_map) & set(i2d_map) & set(i3d_map))
        dur_above = 0.0
        for j, ts in enumerate(common_ts_sorted[:-1]):
            v1, v2, v3 = i1d_map[ts], i2d_map[ts], i3d_map[ts]
            i_avg = (v1 + v2 + v3) / 3
            if i_avg > 1e-6:
                imb = max(abs(v1 - i_avg), abs(v2 - i_avg), abs(v3 - i_avg)) / i_avg * 100
                if imb > imb_ref_thr:
                    dt = (common_ts_sorted[j + 1] - ts).total_seconds()
                    dur_above += dt
        imbalance_duration_sec = dur_above if dur_above > 0 else None

    # ── Ток обратной последовательности I₂ (Метод А, Addendum v1.5) ──────────
    # Физически корректная метрика: именно на неё реагирует заводская защита PCC3300.
    # Требует пофазных Q (40035/36/37), добавленных в whitelist.
    q1_rows = _rows_in(_addr("REACTIVE_POWER_L1") or 0) if _addr("REACTIVE_POWER_L1") else []
    q2_rows = _rows_in(_addr("REACTIVE_POWER_L2") or 0) if _addr("REACTIVE_POWER_L2") else []
    q3_rows = _rows_in(_addr("REACTIVE_POWER_L3") or 0) if _addr("REACTIVE_POWER_L3") else []

    # Номинальный ток для нормировки I₂%
    i_nominal_cfg = float(cfg.det("NEGATIVE_SEQUENCE", "i_nominal_a", default=0) or 0)
    i_nominal_a: float = i_nominal_cfg
    if i_nominal_a <= 0:
        rating_kw_rows = _rows_in(_addr("RATING_KW") or 0) if _addr("RATING_KW") else []
        rating_kw = _median([v for _, v in rating_kw_rows]) or 0.0
        u_ll = float(cfg.det("NEGATIVE_SEQUENCE", "u_nominal_v", default=400.0))
        pf_n = float(cfg.det("NEGATIVE_SEQUENCE", "pf_nominal", default=0.8))
        if rating_kw > 0 and u_ll > 0 and pf_n > 0:
            i_nominal_a = rating_kw * 1000.0 / (math.sqrt(3) * u_ll * pf_n)

    neg_seq_i2_pct_max: float | None = None
    neg_seq_i2_pct_med: float | None = None
    neg_seq_i2_duration_sec: float | None = None

    if i_nominal_a > 0 and i1 and i2 and i3 and p1 and p2 and p3 and q1_rows and q2_rows and q3_rows:
        i1d_ns = {ts: v for ts, v in i1}
        i2d_ns = {ts: v for ts, v in i2}
        i3d_ns = {ts: v for ts, v in i3}
        p1d_ns = {ts: v for ts, v in p1}
        p2d_ns = {ts: v for ts, v in p2}
        p3d_ns = {ts: v for ts, v in p3}
        q1d_ns = {ts: v for ts, v in q1_rows}
        q2d_ns = {ts: v for ts, v in q2_rows}
        q3d_ns = {ts: v for ts, v in q3_rows}

        ns_common = sorted(
            set(i1d_ns) & set(i2d_ns) & set(i3d_ns) &
            set(p1d_ns) & set(p2d_ns) & set(p3d_ns) &
            set(q1d_ns) & set(q2d_ns) & set(q3d_ns)
        )

        i2_pct_series: list[tuple[datetime, float]] = []
        for ts in ns_common:
            i2_abs = _compute_neg_seq_i2(
                i1d_ns[ts], p1d_ns[ts], q1d_ns[ts],
                i2d_ns[ts], p2d_ns[ts], q2d_ns[ts],
                i3d_ns[ts], p3d_ns[ts], q3d_ns[ts],
            )
            if i2_abs is not None:
                i2_pct_series.append((ts, i2_abs / i_nominal_a * 100))

        if i2_pct_series:
            pcts = [pct for _, pct in i2_pct_series]
            neg_seq_i2_pct_max = max(pcts)
            neg_seq_i2_pct_med = _median(pcts)

            # Суммарное время I₂% > порога приближения к заводской защите
            i2_prox = float(cfg.det("NEGATIVE_SEQUENCE", "i2_proximity_warning_pct", default=10.0))
            dur = 0.0
            for j in range(len(i2_pct_series) - 1):
                ts_j, pct_j = i2_pct_series[j]
                ts_next, _ = i2_pct_series[j + 1]
                if pct_j > i2_prox:
                    dur += (ts_next - ts_j).total_seconds()
            neg_seq_i2_duration_sec = dur if dur > 0 else None

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

    # Скоростные метрики (V) — только реальные измерения (real_only=True),
    # forward-fill строки не участвуют: они несут «последнее известное», а не
    # новое измерение, и не должны создавать артефактных скачков скорости.
    p_total_real = _rows_in(_addr("ACTIVE_POWER_TOTAL") or 0, real_only=True) if _addr("ACTIVE_POWER_TOTAL") else []
    rpm_rows_real = _rows_in(_addr("RPM") or 0, real_only=True) if _addr("RPM") else []
    cool_t_real = _rows_in(_addr("COOLANT_TEMP") or 0, real_only=True) if _addr("COOLANT_TEMP") else []
    oil_press_real = _rows_in(_addr("OIL_PRESS") or 0, real_only=True) if _addr("OIL_PRESS") else []

    # dP_dt_max (кВт/с) — максимальная скорость изменения мощности
    dP_dt_max = _max_speed(p_total_real, gaps, heartbeat, max_mult, absolute=True)

    # dRPM_dt_max
    dRPM_dt_max = _max_speed(rpm_rows_real, gaps, heartbeat, max_mult, absolute=True)

    # dCoolant_dt_max (°C/с) — максимальная мгновенная скорость роста Т ОЖ.
    # Класс B — справочная метрика для LLM Stage 2. Детекторы используют slope.
    dCoolant_dt_max = _max_speed_per_second(cool_t_real, gaps, heartbeat, max_mult, positive=True)

    # dOil_press_dt_min (кПа/с) — максимальная скорость ПАДЕНИЯ давления масла (отрицательная)
    dOil_press_dt_min = _min_speed(oil_press_real, gaps, heartbeat, max_mult)

    # coolant_below_60_sec — время с Т ОЖ < порога
    below_c = float(cfg.thr("coolant_temperature", "tunable", "below_60_risk_threshold_c", default=60.0))
    coolant_below_60_sec = _time_below(cool_t, below_c)

    # ── Переходные процессы по ГОСТ ISO 8528-5 (Класс A, freq transient) ──────
    # freq_dip/rise/recovery — для LOAD_STEP детектора. Только реальные измерения.
    # Gate (Addendum v1.4 п.3): если RPM упали ниже rpm_min_pct% от максимума —
    # это штатный останов/переход на ХХ, а НЕ нагрузочный переходный процесс.
    # Без gate: при остановке freq_dip_pct = 100% (артефакт).
    freq_nominal = float(cfg.det("LOAD_STEP", "freq_nominal_hz", default=50.0))
    freq_settled_pct = float(cfg.det("LOAD_STEP", "freq_settled_pct", default=0.5))
    freq_rows_real = _rows_in(_addr("FREQUENCY") or 0, real_only=True) if _addr("FREQUENCY") else []

    rpm_for_freq_gate = _rows_in(_addr("RPM") or 0, real_only=True) if _addr("RPM") else []
    rpm_min_pct = float(cfg.det("LOAD_STEP", "rpm_min_pct_for_freq_metrics", default=50.0))
    _rpm_vals = [v for _, v in rpm_for_freq_gate]
    _rpm_max = max(_rpm_vals) if _rpm_vals else 0.0
    _rpm_min = min(_rpm_vals) if _rpm_vals else 0.0
    rpm_stays_working = (_rpm_max <= 0 or _rpm_min >= _rpm_max * rpm_min_pct / 100)

    if rpm_stays_working:
        freq_dip_pct, freq_rise_pct, freq_recovery_sec = _freq_transient_metrics(
            freq_rows_real, freq_nominal, freq_settled_pct, t_start
        )
    else:
        freq_dip_pct = freq_rise_pct = freq_recovery_sec = None

    # ── Предиктор асимптоты прогрева ОЖ (Класс B, Phase 1 COOLING_FAILURE) ────
    # Оценивает T_равн для растущего ряда — используется в _detect_cooling_failure
    # чтобы обнаружить «нацеленность на перегрев» ещё до достижения опасной T.
    coolant_asymptote_c = _estimate_thermal_asymptote(cool_t_real)

    return DerivedMetrics(
        neg_seq_i2_pct_max=_r(neg_seq_i2_pct_max),
        neg_seq_i2_pct_med=_r(neg_seq_i2_pct_med),
        neg_seq_i2_duration_sec=_r(neg_seq_i2_duration_sec),
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
        freq_dip_pct=_r(freq_dip_pct),
        freq_rise_pct=_r(freq_rise_pct),
        freq_recovery_sec=_r(freq_recovery_sec),
        dCoolant_dt_max=_r(dCoolant_dt_max),
        dOil_press_dt_min=_r(dOil_press_dt_min),
        coolant_below_60_sec=_r(coolant_below_60_sec),
        coolant_asymptote_c=_r(coolant_asymptote_c, decimals=1),
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


def _max_speed_per_second(
    series: list[tuple[datetime, float]],
    gaps: list[dict],
    heartbeat: float,
    max_mult: float,
    positive: bool = False,
) -> float | None:
    """Максимальная скорость изменения параметра (ед./с), только позитивная если positive=True."""
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


def _compute_neg_seq_i2(
    i1_mag: float, p1: float, q1: float,
    i2_mag: float, p2: float, q2: float,
    i3_mag: float, p3: float, q3: float,
) -> float | None:
    """Ток обратной последовательности I₂ — Метод А (через пофазный Q).

    Возвращает |I₂| в тех же единицах что входные токи (А).

    Алгоритм (преобразование Фортескью):
      1. φ_k = atan2(Q_k, P_k)          — точный угол со знаком
      2. I_Lk = |I_k| · e^(j·(θ_U_k − φ_k))  — ток-вектор (напряжения симметричны)
      3. I₂ = (1/3)·|I_L1 + a²·I_L2 + a·I_L3|,  a = e^(j·2π/3)
    """
    # Углы со знаком (atan2 = None если и P и Q → 0)
    def phase_angle(p: float, q: float) -> float | None:
        if abs(p) < 1e-4 and abs(q) < 1e-4:
            return None
        return math.atan2(q, p)

    phi1 = phase_angle(p1, q1)
    phi2 = phase_angle(p2, q2)
    phi3 = phase_angle(p3, q3)

    if phi1 is None or phi2 is None or phi3 is None:
        return None
    if i1_mag < 1e-3 or i2_mag < 1e-3 or i3_mag < 1e-3:
        return None  # нулевые токи — I₂ нестабилен

    # Углы напряжений (симметричная 3-фазная система: 0°, −120°, +120°)
    TWO_PI_3 = 2 * math.pi / 3
    cIL1 = i1_mag * cmath.exp(1j * (0           - phi1))
    cIL2 = i2_mag * cmath.exp(1j * (-TWO_PI_3   - phi2))
    cIL3 = i3_mag * cmath.exp(1j * (+TWO_PI_3   - phi3))

    # Оператор поворота a = e^(j·2π/3)
    a = cmath.exp(1j * TWO_PI_3)

    # Отрицательная последовательность: I₂ = (1/3)·(IL1 + a²·IL2 + a·IL3)
    I2 = (cIL1 + a**2 * cIL2 + a * cIL3) / 3
    return abs(I2)


def _estimate_thermal_asymptote(
    series: list[tuple[datetime, float]],
) -> float | None:
    """Оценить асимптотическую температуру для фазы нагрева (предиктор COOLING_FAILURE Фаза 1).

    Алгоритм: геометрический ряд последовательных приращений температуры.
    Если приращения убывают (r < 1) → сумма геометрического ряда даёт оценку
    оставшегося подъёма и, следовательно, асимптоты T_равн.

    Требует ≥ 3 реальных точек на монотонно растущей серии.
    Возвращает None если данных недостаточно или серия не на прогреве.
    """
    if len(series) < 3:
        return None

    vals = [v for _, v in series]

    # Серия должна быть в целом растущей
    if vals[-1] <= vals[0]:
        return None

    # Положительные приращения
    deltas = [vals[i + 1] - vals[i] for i in range(len(vals) - 1)]
    pos_deltas = [d for d in deltas if d > 0.1]  # порог 0.1°C — фильтруем шум
    if len(pos_deltas) < 2:
        return None

    # Коэффициент затухания r = d_{i+1} / d_i (медиана пар)
    ratios = []
    for i in range(len(pos_deltas) - 1):
        r = pos_deltas[i + 1] / pos_deltas[i]
        if 0.0 < r < 0.99:  # затухает, но не мгновенно
            ratios.append(r)

    last_d = pos_deltas[-1]

    if not ratios:
        # Нет явного затухания — консервативная оценка: плюс ещё один шаг
        return round(vals[-1] + last_d, 1)

    r_med = sorted(ratios)[len(ratios) // 2]  # медиана

    if r_med >= 1.0:
        return None  # ускоряется — не экспоненциальный прогрев

    # Оставшийся подъём через геометрический ряд: S = last_d * r / (1 - r)
    remaining = last_d * r_med / (1.0 - r_med)
    return round(vals[-1] + remaining, 1)


def _freq_transient_metrics(
    freq_series: list[tuple[datetime, float]],
    nominal_hz: float,
    settled_pct: float,
    t_start: datetime,
) -> tuple[float | None, float | None, float | None]:
    """Вычислить метрики переходного процесса частоты по ГОСТ ISO 8528-5.

    Возвращает (freq_dip_pct, freq_rise_pct, freq_recovery_sec).

    freq_dip_pct   — максимальная просадка ниже номинала, % (наброс нагрузки)
    freq_rise_pct  — максимальный заброс выше номинала, % (сброс нагрузки)
    freq_recovery_sec — время от минимума частоты до первого входа в коридор
                        ±settled_pct от номинала, сек

    Все три = None если рядов < 2 или нет отклонений от номинала.
    """
    if len(freq_series) < 2 or nominal_hz <= 0:
        return None, None, None

    vals = [v for _, v in freq_series]
    f_min = min(vals)
    f_max = max(vals)

    freq_dip_pct = max(0.0, (nominal_hz - f_min) / nominal_hz * 100)
    freq_rise_pct = max(0.0, (f_max - nominal_hz) / nominal_hz * 100)

    # Нет заметных отклонений — нет переходного процесса
    if freq_dip_pct < 0.1 and freq_rise_pct < 0.1:
        return None, None, None

    # Время восстановления: от момента наддира до входа в settled-коридор
    settled_hz = nominal_hz * settled_pct / 100  # e.g. 50 * 0.5/100 = 0.25 Гц

    # Найти наддир (минимальную точку)
    nadir_ts: datetime | None = None
    for ts, v in freq_series:
        if v == f_min:
            nadir_ts = ts
            break

    freq_recovery_sec: float | None = None
    if nadir_ts is not None:
        # Первый момент после наддира, когда |f - nominal| ≤ settled_hz
        post_nadir = [(ts, v) for ts, v in freq_series if ts >= nadir_ts]
        for ts, v in post_nadir:
            if abs(v - nominal_hz) <= settled_hz:
                dt = (_tz(ts) - _tz(t_start)).total_seconds()
                freq_recovery_sec = max(0.0, dt)
                break

    return (
        round(freq_dip_pct, 2) if freq_dip_pct > 0.1 else None,
        round(freq_rise_pct, 2) if freq_rise_pct > 0.1 else None,
        round(freq_recovery_sec, 1) if freq_recovery_sec is not None else None,
    )


def _r(v: float | None, decimals: int = 3) -> float | None:
    if v is None:
        return None
    return round(v, decimals)
