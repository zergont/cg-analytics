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

import bisect
import cmath
import logging
import math
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from .contract import Characteristic, DerivedMetrics
from .config import AnalyticsConfig

logger = logging.getLogger(__name__)


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
        unit = meta.get("unit") or ""

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
            real_count=len(window_real),
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
    phase: list[tuple] | None = None,
) -> DerivedMetrics:
    """Вычислить производные метрики за подсегмент.

    Параметры (пороги) берутся из cfg — не захардкожены.

    phase — готовый ряд фаз этого подсегмента (v5.1.30: срез ряда сегмента
    phase_series, посчитанного для участков analytics/live.py): тот же ряд не
    строится второй раз. Значения держатся и через границу подсегмента, пока
    идут пакеты. None — ряд строится по строкам подсегмента, как раньше.
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
    # Требует пофазных Q (40035/36/37), добавленных в whitelist. Считается в
    # общем ряду по пакетам ниже (v5.1.30).

    # Номинальный ток для нормировки I₂%
    i_nominal_a = nominal_current(
        cfg, [v for _, v in _rows_in(_addr("RATING_KW") or 0)] if _addr("RATING_KW") else [])

    neg_seq_i2_pct_max: float | None = None
    neg_seq_i2_pct_med: float | None = None
    neg_seq_i2_duration_sec: float | None = None

    # ── Перекос фазных токов и ток нейтрали (v4.9.97) ────────────────────────
    # Заводские защиты PCC3300 такую картину не видят: защиты по току нейтрали
    # нет, а I₂ в % номинала при провале одной фазы остаётся единицами
    # процентов. 29.09.2026 (Сининда, перед сгоранием повышающего
    # трансформатора): L3 упала до ~10% от L1/L2, ток нейтрали ~1440 А, I₂ —
    # 2–4% номинала. Нормируем на номинальный ток, как и I₂: на малой нагрузке
    # перекос в 20% номинала просто не набирается, шум отсекается сам.
    #
    # Ряд строится по ПАКЕТАМ связи с удержанием последнего значения каждого
    # регистра (_held_samples), а не по совпадению реальных строк: роутер шлёт
    # регистр только при изменении, и пофазная Q молчит до 200 с — точное
    # совпадение девяти регистров рвало бы непрерывность, а мёртвая фаза с
    # постоянным нулём не попадала бы в ряд вовсе.
    #
    # v5.1.30 («По событию», В-27.1): I₂ — в том же ряду по пакетам (phase_series,
    # общий с участками сегмента analytics/live.py). Прежний ряд по точному
    # совпадению девяти регистров был разреженным (у ДЭС №3 — 341 точка за
    # 5,5 ч), а время за порогом складывалось интервалом до следующей точки и
    # через паузу: «117 с» ДЭС №3 09.10 набрала одна точка, «780 с» ДГУ №2
    # 27.09 — один переходный кадр. Теперь длительность — самый длинный
    # непрерывный участок «≥ порога» (как у перекоса и нейтрали): рвётся на
    # паузе пакетов, дыре связи и замере без I₂ (ток фазы 0 — не нарушение).
    phase_spread_pct_max: float | None = None
    phase_spread_pct_med: float | None = None
    phase_spread_run_sec: float | None = None
    neutral_current_pct_max: float | None = None
    neutral_current_pct_med: float | None = None
    neutral_current_run_sec: float | None = None
    _pprm = phase_params(cfg, heartbeat, max_mult)
    if _pprm is not None and (phase is not None or i_nominal_a > 0):
        max_gap = _pprm["max_gap"]
        hb_addr = _pprm["hb_addr"]
        series = phase if phase is not None else phase_series(
            {a: _rows_in(a) for a in _pprm["addrs"]},
            [ts for ts, _ in _rows_in(hb_addr)] if hb_addr else [],
            gaps, _pprm, i_nominal_a,
        )
        spread_ser = [(ts, s) for ts, _i2, s, _n in series]
        neutral_ser = [(ts, n) for ts, _i2, _s, n in series]
        spread = [(ts, s) for ts, s in spread_ser if s is not None]
        neutral = [(ts, n) for ts, n in neutral_ser if n is not None]
        i2_ser = [(ts, i2v) for ts, i2v, _s, _n in series]
        i2_ok = [(ts, v) for ts, v in i2_ser if v is not None]
        # Максимум — устойчивый (держится хотя бы два пакета подряд): на скачке
        # нагрузки одна фаза приходит новым значением раньше двух других, и
        # один пакет даёт фантомный перекос (у ДЭС №3 — 61% на одном замере).
        # Детектору это не мешает — ему нужна выдержка, — но в отчёте такой
        # максимум сбивал бы с толку. Ревью v5.1.30: пара — два соседних пакета
        # ряда, без кадра без значения и без дыры связи между ними
        if spread:
            phase_spread_pct_max = _sustained_max_adj(spread_ser, max_gap, gaps)
            phase_spread_pct_med = _median([v for _, v in spread])
            phase_spread_run_sec = _longest_run_at_or_above(spread, _pprm["spread_thr"], max_gap)
        if neutral:
            neutral_current_pct_max = _sustained_max_adj(neutral_ser, max_gap, gaps)
            neutral_current_pct_med = _median([v for _, v in neutral])
            neutral_current_run_sec = _longest_run_at_or_above(neutral, _pprm["neutral_thr"], max_gap)
        if i2_ok:
            # Тот же устойчивый максимум (ревью E1: ряд по пакетам с удержанием
            # даёт ложный всплеск I₂ на переходном кадре)
            neg_seq_i2_pct_max = _sustained_max_adj(i2_ser, max_gap, gaps)
            neg_seq_i2_pct_med = _median([v for _, v in i2_ok])
            neg_seq_i2_duration_sec = _longest_run_at_or_above(i2_ser, _pprm["i2_thr"], max_gap, gaps)

    # ── Давление масла ниже паспорта своего двигателя (v5.1.12) ──────────────
    # Паспорт — слой двигателя (KTA50 310, QSX15 242); раньше детектор у QSX15
    # сравнивал с 310 от KTA50 и горел всегда (ДЭС №3: 47 подсегментов из 47).
    # Ряд — по пакетам с удержанием (как перекос фаз), только на рабочих оборотах
    # и не ниже уставки останова панели: нули остановленного двигателя при
    # RUN_STATE=3 и сбои датчика, на которые панель промолчала, — не нарушение.
    op = _oil_passport_runs(_rows_in, _addr, gaps, cfg, heartbeat, max_mult)

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

    # RPM_UNDERSPEED front count: число падений RPM ниже порога (переходов сверху вниз)
    rpm_underspeed_fronts_count: int | None = None
    if rpm_rows:
        _thr_rpm = float(cfg.det("RPM_UNDERSPEED", "threshold_rpm", default=1485.0))
        _fronts = 0
        _was_above: bool | None = None
        for _, _v in rpm_rows:
            _cur_above = _v >= _thr_rpm
            if _was_above is True and not _cur_above:
                _fronts += 1
            _was_above = _cur_above
        if _fronts > 0:
            rpm_underspeed_fronts_count = _fronts

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

    # dP_dt_max (кВт/с) — максимальная скорость изменения мощности.
    # v5.1.25 (решение В-25.1): пары ближе LOAD_STEP.min_pair_dt_sec не в счёт
    # (пульс db-writer — 9 ложных CAUTION за 60 дней); момент наибольшей и
    # онсет (первая пара выше порога детектора) — для t_detected события
    dP_dt_max_at = dP_dt_onset_at = None
    try:
        _ls_min_dt = float(cfg.det("LOAD_STEP", "min_pair_dt_sec", default=1.0) or 0.0)
        _ls_thr = float(cfg.det("LOAD_STEP", "dP_dt_threshold_kw_per_s", default=50.0))
        dP_dt_max, dP_dt_max_at, dP_dt_onset_at = _max_speed(
            p_total_real, gaps, heartbeat, max_mult, absolute=True,
            min_dt=_ls_min_dt, with_ts=True, onset_thr=_ls_thr)
    except Exception:
        logger.warning("dP/dt по парам дальше min_pair_dt_sec не посчитан — как раньше",
                       exc_info=True)
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
    # Обороты есть и все ≤ 0 — двигатель стоит: переходных процессов нет (у стопа
    # выходило «провал частоты 100 %»); ряда оборотов нет вовсе — считать как прежде
    rpm_stays_working = (not _rpm_vals) or (
        _rpm_max > 0 and _rpm_min >= _rpm_max * rpm_min_pct / 100)

    if rpm_stays_working:
        freq_dip_pct, freq_rise_pct, freq_recovery_sec = _freq_transient_metrics(
            freq_rows_real, freq_nominal, freq_settled_pct, t_start
        )
    else:
        freq_dip_pct = freq_rise_pct = freq_recovery_sec = None

    # Шаг опроса и подпись восстановления (v5.1.25, решение В-25.1): шаг больше
    # нормы класса — восстановление короче шага не измерить, «не более N с»
    freq_poll_step_sec = freq_recovery_text = None
    if freq_recovery_sec is not None:
        try:
            freq_poll_step_sec = _poll_step_sec(by_addr, t_start, t_end, cfg)
            _cls = cfg.det("LOAD_STEP", "load_class_iso", default="G3")
            _norm = float(((cfg.det("LOAD_STEP", "iso_classes", default={}) or {})
                           .get(_cls) or {}).get("freq_recovery_sec", 3.0))
            if freq_poll_step_sec is not None and freq_poll_step_sec > _norm:
                freq_recovery_text = recovery_bound_text(freq_recovery_sec, freq_poll_step_sec, _cls)
        except Exception:
            logger.warning("Шаг опроса для восстановления частоты не посчитан — как раньше",
                           exc_info=True)
            freq_poll_step_sec = freq_recovery_text = None

    # ── Предиктор асимптоты прогрева ОЖ (Класс B, Phase 1 COOLING_FAILURE) ────
    # Оценивает T_равн для растущего ряда — используется в _detect_cooling_failure
    # чтобы обнаружить «нацеленность на перегрев» ещё до достижения опасной T.
    coolant_asymptote_c = _estimate_thermal_asymptote(cool_t_real)

    return DerivedMetrics(
        neg_seq_i2_pct_max=_r(neg_seq_i2_pct_max),
        neg_seq_i2_pct_med=_r(neg_seq_i2_pct_med),
        neg_seq_i2_duration_sec=_r(neg_seq_i2_duration_sec),
        phase_spread_pct_max=_r(phase_spread_pct_max),
        phase_spread_pct_med=_r(phase_spread_pct_med),
        phase_spread_run_sec=_r(phase_spread_run_sec),
        neutral_current_pct_max=_r(neutral_current_pct_max),
        neutral_current_pct_med=_r(neutral_current_pct_med),
        neutral_current_run_sec=_r(neutral_current_run_sec),
        current_imbalance_pct_max=_r(current_imbalance_pct_max),
        current_imbalance_pct_med=_r(current_imbalance_pct_med),
        power_imbalance_pct_max=_r(power_imbalance_pct_max),
        imbalance_duration_sec=_r(imbalance_duration_sec),
        s_consistency_max=_r(s_consistency_max),
        pf_consistency_max=_r(pf_consistency_max),
        oil_coolant_delta_med=_r(oil_coolant_delta_med),
        oil_coolant_delta_max=_r(oil_coolant_delta_max),
        rpm_stability_mad=_r(rpm_stability_mad),
        rpm_underspeed_fronts_count=rpm_underspeed_fronts_count,
        freq_stability_mad=_r(freq_stability_mad),
        dP_dt_max=_r(dP_dt_max),
        dRPM_dt_max=_r(dRPM_dt_max),
        freq_dip_pct=_r(freq_dip_pct),
        freq_rise_pct=_r(freq_rise_pct),
        freq_recovery_sec=_r(freq_recovery_sec),
        freq_poll_step_sec=_r(freq_poll_step_sec, 1),
        freq_recovery_text=freq_recovery_text,
        dP_dt_max_at=dP_dt_max_at,
        dP_dt_onset_at=dP_dt_onset_at,
        dCoolant_dt_max=_r(dCoolant_dt_max),
        dOil_press_dt_min=_r(dOil_press_dt_min),
        coolant_below_60_sec=_r(coolant_below_60_sec),
        coolant_asymptote_c=_r(coolant_asymptote_c, decimals=1),
        **op,
    )


def estimate_sensor_step(hist: list[tuple[float, int]], min_samples: int = 1000) -> float | None:
    """Шаг показаний датчика по истории: самый частый промежуток между соседними
    различными положительными значениями (v5.1.12, решение владельца 12.10).

    ДЭС №3 (QSX15, дополнительный ESM): …232, 240, 248, 252… → 8 кПа; ДГУ №1/№2
    (KTA50): …376, 380, 383, 388… → 4. Редкие показания ложатся на ту же сетку,
    поэтому выбросы шаг не сбивают. Мало истории — None (берётся умолчание KB).
    """
    pos = [(round(float(v), 1), int(n)) for v, n in hist or []
           if v is not None and float(v) > 0 and int(n or 0) > 0]
    if sum(n for _, n in pos) < min_samples:
        return None
    vals = sorted({v for v, _ in pos})
    if len(vals) < 5:
        return None
    gaps = Counter(round(b - a, 1) for a, b in zip(vals, vals[1:]) if b - a > 0)
    if not gaps:
        return None
    step, _ = max(gaps.items(), key=lambda kv: (kv[1], -kv[0]))   # при равенстве — меньший
    return float(step)


def _runs_below(
    series: list[tuple[datetime, float | None, bool]], threshold: float, max_gap_sec: float,
    gaps: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Непрерывные участки «значение < порога» по пакетам: рвутся на непригодном
    замере (ok=False), на замере не ниже порога, на паузе длиннее max_gap_sec и
    на дыре связи из data_gaps (слепой интервал — не подтверждение)."""
    runs: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for (ts, v, ok), (ts_next, _, _) in zip(series, series[1:]):
        dt = (ts_next - ts).total_seconds()
        if (ok and v is not None and v < threshold and dt <= max_gap_sec
                and not _gap_between(ts, ts_next, gaps)):
            if cur is None:
                cur = {"start": ts, "end": ts_next, "sec": 0.0, "min": v}
            cur["sec"] += dt
            cur["end"] = ts_next
            cur["min"] = min(cur["min"], v)
        elif cur is not None:
            runs.append(cur)
            cur = None
    if cur is not None:
        runs.append(cur)
    return runs


def oil_passport_params(cfg: AnalyticsConfig, heartbeat: float | None = None,
                        max_mult: float | None = None) -> dict[str, Any] | None:
    """Параметры паспорта давления масла (v5.1.12): общие для метрик подсегмента
    и участков сегмента (analytics/live.py, v5.1.21). None — паспорта/регистров нет."""
    p_addr, rpm_addr = cfg.role_to_addr("OIL_PRESS"), cfg.role_to_addr("RPM")
    p_min = cfg.thr("oil_pressure", "passport", "rated_min_kpa")
    if not p_addr or not rpm_addr or p_min is None:
        return None
    if heartbeat is None:
        heartbeat = float(cfg.seg("data_quality", "heartbeat_nominal_sec", default=30))
    if max_mult is None:
        max_mult = float(cfg.seg("data_quality", "heartbeat_max_multiplier", default=3))
    limit = float(p_min)
    step, source = cfg.sensor_step("OIL_PRESS")
    cap = limit * float(cfg.det("OIL_DILUTION", "passport_step_max_pct", default=5.0)) / 100
    step = min(float(step), cap) if step else 0.0
    floor = cfg.thr("oil_pressure", "controller", "shutdown_rated_kpa")
    _hb = cfg.seg("data_quality", "heartbeat_addr", default=None)
    return {
        "p_addr": p_addr, "rpm_addr": rpm_addr,
        "limit": limit, "step": step, "step_source": source if step > 0 else "none",
        "rpm_min": float(cfg.det("OIL_DILUTION", "passport_min_rpm", default=1400)),
        "min_run": float(cfg.det("OIL_DILUTION", "passport_min_run_sec", default=60)),
        "floor": float(floor) if floor is not None else None,
        "max_gap": heartbeat * max_mult, "tol": heartbeat / 2,
        "hb_addr": int(_hb) if _hb is not None else None,
    }


def passport_instep(series: list[tuple[datetime, float | None, bool]], prm: dict[str, Any],
                    t0: datetime, t1: datetime, ts_list: list[datetime] | None = None) -> list[float]:
    """Пригодные показания ряда в [t0, t1) ниже паспорта, но в пределах шага датчика.
    ts_list — моменты ряда (двоичный поиск вместо прохода: участков за сутки сотни)."""
    lo, limit = prm["limit"] - prm["step"], prm["limit"]
    if ts_list is not None:
        series = series[bisect.bisect_left(ts_list, t0):bisect.bisect_left(ts_list, t1)]
    return [v for ts, v, ok in series
            if ok and v is not None and t0 <= ts < t1 and lo <= v < limit]


def oil_passport_runs(p_rows: list[tuple[datetime, float]], rpm_rows: list[tuple[datetime, float]],
                      packet_ts: list[datetime], gaps: list[dict[str, Any]],
                      prm: dict[str, Any]) -> dict[str, Any]:
    """Ряд давления по пакетам с удержанием и участки ниже паспорта (v5.1.21).

    Одна логика на два потребителя: поля DerivedMetrics подсегмента
    (_oil_passport_runs) и участки сегмента (analytics/live.py) — без дублирования.
    Ряд — только на рабочих оборотах и не ниже уставки останова панели (нули
    остановленного двигателя и сбои датчика — не замер). Участок рвётся на
    непригодном замере, на паузе пакетов и на дыре связи (_runs_below).

    → {"series": [(момент, давление, пригоден)], "soft": [...], "hard": [...]}:
    участки ниже паспорта и ниже паспорта больше чем на шаг. У каждого участка
    start, end, sec (видимое время), min и open — идёт на краю ряда (последний
    замер ряда сам ниже порога). У участка soft ещё instep_min — минимум
    показаний в пределах шага, и признак hard — в нём есть «нарушение» (участок
    hard не короче выдержки).
    """
    p_addr, rpm_addr = prm["p_addr"], prm["rpm_addr"]
    samples = _held_samples({p_addr: p_rows, rpm_addr: rpm_rows}, packet_ts,
                            gaps, prm["max_gap"], prm["tol"])
    floor, rpm_min = prm["floor"], prm["rpm_min"]
    series: list[tuple[datetime, float | None, bool]] = []
    for ts, held in samples:
        v, rpm = held.get(p_addr), held.get(rpm_addr)
        ok = (v is not None and rpm is not None and rpm >= rpm_min
              and (floor is None or v >= floor))
        series.append((ts, v, ok))
    limit, step = prm["limit"], prm["step"]
    soft = _runs_below(series, limit, prm["max_gap"], gaps)
    hard = _runs_below(series, limit - step, prm["max_gap"], gaps) if step > 0 else soft
    if series:
        t_last, v_last, ok_last = series[-1]
        for runs, thr in ((soft, limit), (hard, limit - step)):
            for r in runs:
                r["open"] = (r["end"] == t_last and ok_last and v_last is not None and v_last < thr)
    hard_ok = [r for r in hard if r["sec"] >= prm["min_run"]]
    ts_list = [s[0] for s in series]
    for r in soft:
        inst = passport_instep(series, prm, r["start"], r["end"], ts_list)
        r["instep_min"] = min(inst) if inst else None
        r["hard"] = any(r["start"] <= h["start"] and h["end"] <= r["end"] for h in hard_ok)
    return {"series": series, "soft": soft, "hard": hard, "ts": ts_list}


def _oil_passport_runs(_rows_in, _addr, gaps, cfg: AnalyticsConfig,
                       heartbeat: float, max_mult: float) -> dict[str, Any]:
    """Поля DerivedMetrics oil_press_* (пустой dict — паспорта/регистров нет).
    Ряд и участки — oil_passport_runs (v5.1.21: общий с участками сегмента)."""
    prm = oil_passport_params(cfg, heartbeat, max_mult)
    if prm is None:
        return {}
    hb_addr = prm["hb_addr"]
    runs = oil_passport_runs(_rows_in(prm["p_addr"]), _rows_in(prm["rpm_addr"]),
                             [ts for ts, _ in _rows_in(hb_addr)] if hb_addr else [], gaps, prm)
    series, soft, hard = runs["series"], runs["soft"], runs["hard"]
    limit, step, min_run = prm["limit"], prm["step"], prm["min_run"]
    soft_ok = [r for r in soft if r["sec"] >= min_run]
    hard_ok = [r for r in hard if r["sec"] >= min_run]
    out: dict[str, Any] = {
        "oil_press_passport_kpa": limit,
        "oil_press_step_kpa": _r(step, 1),
        "oil_press_step_source": prm["step_source"],
    }
    if soft:
        out["oil_press_below_passport_run_sec"] = _r(max(r["sec"] for r in soft), 1)

    def _instep(t0, t1) -> list[float]:
        return passport_instep(series, prm, t0, t1, runs["ts"])

    lh = max(hard_ok, key=lambda r: r["end"]) if hard_ok else None
    if soft_ok:
        out["oil_press_below_passport_min_kpa"] = _r(min(r["min"] for r in soft_ok), 1)
        inst = [v for r in soft_ok for v in _instep(r["start"], r["end"])]
        if inst:
            out["oil_press_instep_min_kpa"] = _r(min(inst), 1)
        last = max(soft_ok, key=lambda r: r["end"])
        out["oil_press_passport_until"] = _tz(last["end"]).isoformat()
        out["oil_press_passport_ongoing"] = bool(series) and last["end"] == series[-1][0]
        # Живая копия после давнего нарушения — число из хвоста участка после
        # нарушения и только в пределах шага (ревью: иначе «в пределах шага, мин. 232»)
        t0 = lh["end"] if lh is not None and last["start"] <= lh["end"] < last["end"] else last["start"]
        tail = _instep(t0, last["end"])
        out["oil_press_last_min_kpa"] = _r(min(tail) if tail else last["min"], 1)
    if hard:
        out["oil_press_hard_run_sec"] = _r(max(r["sec"] for r in hard), 1)
    if lh is not None:
        out["oil_press_hard_min_kpa"] = _r(min(r["min"] for r in hard_ok), 1)
        out["oil_press_hard_until"] = _tz(lh["end"]).isoformat()
        out["oil_press_hard_ongoing"] = bool(series) and lh["end"] == series[-1][0]
    return out


# ── Вспомогательные расчёты скоростей ────────────────────────────────────────

def _max_speed(
    series: list[tuple[datetime, float]],
    gaps: list[dict],
    heartbeat: float,
    max_mult: float,
    absolute: bool = False,
    min_dt: float = 0.0,
    with_ts: bool = False,
    onset_thr: float | None = None,
):
    """Максимальная скорость изменения параметра (ед./с).

    v5.1.25 (решение В-25.1): min_dt — пары отсчётов ближе не учитываются
    (пульс db-writer пишет ту же мощность второй строкой через доли секунды).
    with_ts — (скорость, момент наибольшей, момент первой пары выше
    onset_thr); момент — второй отсчёт пары, ISO."""
    best: float | None = None
    best_at: datetime | None = None
    first_at: datetime | None = None
    for i in range(len(series) - 1):
        ts1, v1 = series[i]
        ts2, v2 = series[i + 1]
        if _is_gap_between(ts1, ts2, gaps, heartbeat, max_mult):
            continue
        dt = (_tz(ts2) - _tz(ts1)).total_seconds()
        if dt < max(1e-6, min_dt):
            continue
        speed = (v2 - v1) / dt
        speed = abs(speed) if absolute else speed
        if best is None or speed > best:
            best, best_at = speed, _tz(ts2)
        if first_at is None and onset_thr is not None and speed > onset_thr:
            first_at = _tz(ts2)
    if not with_ts:
        return best
    return (best, best_at.isoformat() if best_at else None,
            first_at.isoformat() if first_at else None)


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


def _held_samples(
    rows_by_addr: dict[int, list[tuple[datetime, float]]],
    packet_ts: list[datetime],
    gaps: list[dict[str, Any]],
    max_gap_sec: float,
    tol_sec: float,
) -> list[tuple[datetime, dict[int, float]]]:
    """Значения регистров на каждый пакет связи, с удержанием последнего.

    Роутер шлёт регистр только при изменении: молчание значит «не
    изменилось», пока связь жива. Поэтому значение держится, пока идут
    пакеты, и сбрасывается только при потере связи — пауза между пакетами
    длиннее max_gap_sec или дыра из data_gaps между ними. Начальное значение
    на старте подсегмента дают строки forward-fill первого пакета.

    Якоря — пакеты (пинг-регистр); если его нет — моменты любых строк этих
    регистров. Строки в пределах tol_sec после пакета относятся к нему.
    """
    anchors = sorted(packet_ts) or sorted({ts for rows in rows_by_addr.values() for ts, _ in rows})
    if not anchors:
        return []
    # Окно «строка относится к пакету» не шире половины интервала между
    # пакетами: иначе в один момент подмешиваются значения СЛЕДУЮЩЕГО пакета.
    # С ±15 с при пакетах раз в 9–10 с у ДЭС №3 получался фантомный перекос
    # 61% на скачке нагрузки (одна фаза — новое значение, две — старые).
    steps = sorted((b - a).total_seconds() for a, b in zip(anchors, anchors[1:]))
    if steps:
        tol_sec = min(tol_sec, steps[len(steps) // 2] / 2)
    ordered = {a: sorted(rows, key=lambda r: r[0]) for a, rows in rows_by_addr.items()}
    pos = {a: 0 for a in ordered}
    held: dict[int, float] = {}
    out: list[tuple[datetime, dict[int, float]]] = []
    prev: datetime | None = None
    for ts in anchors:
        broken = prev is not None and (
            (ts - prev).total_seconds() > max_gap_sec or _gap_between(prev, ts, gaps)
        )
        if broken:
            held = {}
        for a, rows in ordered.items():
            k = pos[a]
            while k < len(rows) and rows[k][0] <= ts + timedelta(seconds=tol_sec):
                # После обрыва связи старое значение не принимаем: только то,
                # что пришло вместе с этим пакетом
                if not broken or rows[k][0] >= ts - timedelta(seconds=tol_sec):
                    held[a] = rows[k][1]
                k += 1
            pos[a] = k
        out.append((ts, dict(held)))
        prev = ts
    return out


def _sustained_max(
    series: list[tuple[datetime, float]], max_gap_sec: float,
) -> float | None:
    """Максимум, державшийся хотя бы два соседних замера (без дыры связи).

    Одиночный замер — максимум его самого, только если ряд из одной точки.
    """
    if not series:
        return None
    if len(series) == 1:
        return series[0][1]
    best: float | None = None
    for (ts, v), (ts_next, v_next) in zip(series, series[1:]):
        if (ts_next - ts).total_seconds() <= max_gap_sec:
            m = min(v, v_next)
            best = m if best is None else max(best, m)
    return best


def _sustained_max_adj(
    series: list[tuple[datetime, float | None]], max_gap_sec: float,
    gaps: list[dict[str, Any]] | None = None,
) -> float | None:
    """Устойчивый максимум по ряду с пропусками (v5.1.30, ревью): пара — два
    соседних пакета ряда с значениями, не дальше max_gap_sec и без дыры
    data_gaps между ними; кадр без значения (None) пару рвёт. Одно значение в
    ряду — оно само (как _sustained_max)."""
    vals = [v for _, v in series if v is not None]
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    best: float | None = None
    for (ts, v), (ts_next, v_next) in zip(series, series[1:]):
        if v is None or v_next is None or (ts_next - ts).total_seconds() > max_gap_sec:
            continue
        if gaps and _gap_between(ts, ts_next, gaps):
            continue
        m = min(v, v_next)
        best = m if best is None else max(best, m)
    return best


def _gap_between(t0: datetime, t1: datetime, gaps: list[dict[str, Any]]) -> bool:
    """Есть ли дыра связи из data_gaps, пересекающая (t0, t1)."""
    for g in gaps or []:
        gs = _tz(g["gap_start"])
        ge = _tz(g["gap_end"]) if g.get("gap_end") else None
        if gs < t1 and (ge is None or ge > t0):
            return True
    return False


def _compute_neutral_current(
    currents: list[float], p: list[float], q: list[float],
) -> float | None:
    """Ток нейтрали |I_L1 + I_L2 + I_L3| по модулям токов и пофазным P, Q.

    Угол тока фазы относительно её напряжения — atan2(Q, P); напряжения
    считаются симметричными (0°, −120°, +120°), как и в расчёте I₂.

    В отличие от I₂, нулевой ток фазы здесь законен: это и есть провал фазы,
    который ищем, — вектор просто нулевой. Пропускаем замер, только если ток
    есть, а угол взять не из чего (P и Q обе ≈ 0).
    """
    total = 0j
    for k, theta in enumerate((0.0, -2 * math.pi / 3, 2 * math.pi / 3)):
        i_mag = currents[k]
        if i_mag < 1e-3:
            continue
        if abs(p[k]) < 1e-4 and abs(q[k]) < 1e-4:
            return None
        total += i_mag * cmath.exp(1j * (theta - math.atan2(q[k], p[k])))
    return abs(total)


def _longest_run_at_or_above(
    series: list[tuple[datetime, float | None]], threshold: float, max_gap_sec: float,
    gaps: list[dict[str, Any]] | None = None,
) -> float | None:
    """Самый длинный непрерывный участок, где значение ≥ порога, сек.

    Непрерывность рвётся на замере ниже порога и на дыре в данных длиннее
    max_gap_sec: дыра — не подтверждение, что превышение длилось. Именно
    непрерывный участок, а не сумма: одиночные всплески в норме бывают
    (у ДЭС №3 за месяц — до 23% перекоса), длительных — нет.

    v5.1.30: замер без значения (None — у I₂ ток фазы 0) рвёт участок; gaps —
    интервал через дыру data_gaps не засчитывается (у I₂; перекос и нейтраль —
    как раньше, без gaps).
    """
    best = cur = 0.0
    for (ts, v), (ts_next, _) in zip(series, series[1:]):
        dt = (ts_next - ts).total_seconds()
        if (v is not None and v >= threshold and dt <= max_gap_sec
                and not (gaps and _gap_between(ts, ts_next, gaps))):
            cur += dt
            best = max(best, cur)
        else:
            cur = 0.0
    return best if best > 0 else None


# ── Ряд фаз по пакетам: I₂, перекос, нейтраль (v5.1.30) ─────────────────────

def phase_params(cfg: AnalyticsConfig, heartbeat: float | None = None,
                 max_mult: float | None = None) -> dict[str, Any] | None:
    """Адреса и пороги ряда фаз (v5.1.30): общие для метрик подсегмента и
    участков сегмента (analytics/live.py). None — токов фаз в карте нет.

    with_pq — есть пофазные P и Q: без них нет углов, а значит нет I₂ и тока
    нейтрали (перекос — по одним токам)."""
    i_addrs = [cfg.role_to_addr(f"CURRENT_L{k}") for k in (1, 2, 3)]
    if not all(i_addrs):
        return None
    p_addrs = [cfg.role_to_addr(f"ACTIVE_POWER_L{k}") for k in (1, 2, 3)]
    q_addrs = [cfg.role_to_addr(f"REACTIVE_POWER_L{k}") for k in (1, 2, 3)]
    with_pq = all(p_addrs) and all(q_addrs)
    if heartbeat is None:
        heartbeat = float(cfg.seg("data_quality", "heartbeat_nominal_sec", default=30))
    if max_mult is None:
        max_mult = float(cfg.seg("data_quality", "heartbeat_max_multiplier", default=3))
    _hb = cfg.seg("data_quality", "heartbeat_addr", default=None)
    return {
        "i": i_addrs, "p": p_addrs, "q": q_addrs, "with_pq": with_pq,
        "addrs": i_addrs + ((p_addrs + q_addrs) if with_pq else []),
        "max_gap": heartbeat * max_mult, "tol": heartbeat / 2,
        "hb_addr": int(_hb) if _hb is not None else None,
        "i2_thr": float(cfg.det("NEGATIVE_SEQUENCE", "i2_proximity_warning_pct", default=6.0)),
        "spread_thr": float(cfg.det("PHASE_CURRENT_SPREAD", "spread_warning_pct", default=20.0)),
        "neutral_thr": float(cfg.det("NEUTRAL_CURRENT", "neutral_warning_pct", default=25.0)),
    }


def nominal_current(cfg: AnalyticsConfig, rating_kw_values: list[float]) -> float:
    """Номинальный ток генератора, А: из KB (NEGATIVE_SEQUENCE.i_nominal_a) или
    по номинальной мощности RATING_KW (медиана строк), U_ном и cos φ_ном.
    0 — посчитать не из чего."""
    i_nom = float(cfg.det("NEGATIVE_SEQUENCE", "i_nominal_a", default=0) or 0)
    if i_nom > 0:
        return i_nom
    rating_kw = _median([float(v) for v in rating_kw_values if v is not None]) or 0.0
    u_ll = float(cfg.det("NEGATIVE_SEQUENCE", "u_nominal_v", default=400.0))
    pf_n = float(cfg.det("NEGATIVE_SEQUENCE", "pf_nominal", default=0.8))
    if rating_kw > 0 and u_ll > 0 and pf_n > 0:
        return rating_kw * 1000.0 / (math.sqrt(3) * u_ll * pf_n)
    return 0.0


def phase_series(
    rows_by_addr: dict[int, list[tuple[datetime, float]]],
    packet_ts: list[datetime],
    gaps: list[dict[str, Any]],
    prm: dict[str, Any],
    i_nominal_a: float,
) -> list[tuple[datetime, float | None, float | None, float | None]]:
    """Ряд фаз по пакетам связи с удержанием (v5.1.30): [(момент, I₂ %, перекос %,
    нейтраль %)] в % номинального тока — по одной точке на пакет.

    Значение, которое в пакете не определено, — None: нет удержанного значения
    какого-то регистра (после обрыва связи, до первой строки), у I₂ — ещё и
    ток фазы 0 или нет угла (_compute_neg_seq_i2: «нулевые токи — I₂
    нестабилен»; провал фазы ловят перекос и нейтраль). Один проход на три
    правила: метрики подсегмента и участки сегмента строят ряд одинаково."""
    if i_nominal_a <= 0:
        return []
    i_a, p_a, q_a, with_pq = prm["i"], prm["p"], prm["q"], prm["with_pq"]
    samples = _held_samples({a: rows_by_addr.get(a) or [] for a in prm["addrs"]},
                            packet_ts, gaps, prm["max_gap"], prm["tol"])
    k = 100.0 / i_nominal_a
    out: list[tuple[datetime, float | None, float | None, float | None]] = []
    for ts, held in samples:
        cur = [held.get(a) for a in i_a]
        if any(v is None for v in cur):
            out.append((ts, None, None, None))
            continue
        spread = (max(cur) - min(cur)) * k
        i2v = neut = None
        if with_pq:
            p_ = [held.get(a) for a in p_a]
            q_ = [held.get(a) for a in q_a]
            if not any(v is None for v in p_ + q_):
                i_n = _compute_neutral_current(cur, p_, q_)
                neut = i_n * k if i_n is not None else None
                i2_abs = _compute_neg_seq_i2(cur[0], p_[0], q_[0], cur[1], p_[1], q_[1],
                                             cur[2], p_[2], q_[2])
                i2v = i2_abs * k if i2_abs is not None else None
        out.append((ts, i2v, spread, neut))
    return out


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
        # Первый момент после наддира, когда |f - nominal| ≤ settled_hz.
        # v5.1.25 (В-25.1): отсчёт — от минимума, как в docstring; до v5.1.25
        # от начала подсегмента (t_start) — до 85 276 с. t_start остаётся ради
        # сигнатуры (test_v13)
        post_nadir = [(ts, v) for ts, v in freq_series if ts >= nadir_ts]
        for ts, v in post_nadir:
            if abs(v - nominal_hz) <= settled_hz:
                dt = (_tz(ts) - _tz(nadir_ts)).total_seconds()
                freq_recovery_sec = max(0.0, dt)
                break

    return (
        round(freq_dip_pct, 2) if freq_dip_pct > 0.1 else None,
        round(freq_rise_pct, 2) if freq_rise_pct > 0.1 else None,
        round(freq_recovery_sec, 1) if freq_recovery_sec is not None else None,
    )


def _poll_step_sec(
    by_addr: dict[int, list[dict[str, Any]]],
    t_start: datetime,
    t_end: datetime,
    cfg: AnalyticsConfig,
) -> float | None:
    """Шаг опроса в подсегменте (v5.1.25, решение В-25.1): медиана интервалов
    между соседними пакетами связи (rpm_freq.packet_times — пинг, без него
    любые реальные строки) в [t_start, t_end). Шаг пакетов, а не ряда: регистр
    частоты пишется по изменению. Интервалы длиннее packet_max_gap_sec (обрыв
    связи) не в счёт. Меньше двух пакетов — None.
    Срез по окну подсегмента до разбора пакетов: история окна — до суток."""
    from .rpm_freq import packet_times, rpm_freq_params
    prm = rpm_freq_params(cfg)
    t0, t1 = _tz(t_start), _tz(t_end)

    def _win(rows):
        # Строки адреса идут по метке (сортирует БД, как у срезов segmenter._row_at)
        rows = rows or []
        a = bisect.bisect_left(rows, t0, key=lambda r: _tz(r["ts"]))
        return rows[a:bisect.bisect_left(rows, t1, key=lambda r: _tz(r["ts"]))]

    hb = prm.get("heartbeat_addr")
    sub = {hb: _win(by_addr.get(hb))} if hb is not None else {}
    if not any(not r.get("is_carried_forward") for rows in sub.values() for r in rows):
        sub = {a: _win(rows) for a, rows in by_addr.items()}
    pk = [t for t in packet_times(sub, prm) if t0 <= t < t1]
    if len(pk) < 2:
        return None
    max_gap = float(prm.get("packet_max_gap_sec") or 90.0)
    iv = [(b - a).total_seconds() for a, b in zip(pk, pk[1:])]
    iv = [x for x in iv if 0 < x <= max_gap]
    if not iv:
        return None
    return float(_median(iv))


def recovery_bound_text(rec: float | None, step: float | None, load_class: str) -> str | None:
    """«не более N с (шаг опроса M с, норма G3 не проверяется)» (решение В-25.1).

    N — оценка сверху: восстановление от отсчёта-минимума плюс шаг — настоящий
    минимум частоты мог быть до отсчёта на шаг опроса (критика v5.1.25, п. 9)."""
    if rec is None or step is None:
        return None
    from .labels import fmt_number
    st = round(step) if step >= 1 else round(step, 1)
    return (f"не более {fmt_number(round(rec + step))} с (шаг опроса {fmt_number(st)} с, "
            f"норма {load_class} не проверяется)")


def _r(v: float | None, decimals: int = 3) -> float | None:
    if v is None:
        return None
    return round(v, decimals)
