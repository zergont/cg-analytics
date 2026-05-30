"""Детекторы диагностических сценариев — ТЗ раздел 7.1.

Каждый детектор — детерминированное правило: метрики → Detection.
Все пороги из cfg (AnalyticsConfig) — магических чисел нет.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from .contract import Detection, DerivedMetrics, RiskAccumulators
from .config import AnalyticsConfig


def _tz(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _iso(ts: datetime) -> str:
    return _tz(ts).isoformat()


def run_all_detectors(
    characteristics: dict[str, Any],
    derived: DerivedMetrics,
    accumulators: RiskAccumulators,
    fault_periods_in_seg: list[dict[str, Any]],
    load_zone: str,
    run_state: int,
    seg_start: datetime,
    seg_end: datetime,
    prev_zone: str | None,
    cfg: AnalyticsConfig,
) -> list[Detection]:
    """Запустить все включённые детекторы, вернуть список сработавших Detection."""
    detections: list[Detection] = []

    # LOAD_STEP: Класс A (быстрые/электрические) — только в RUN_STATE=3
    if cfg.det("LOAD_STEP", "enabled", default=True) and run_state == 3:
        detections.extend(_detect_load_step(derived, seg_start, seg_end, cfg))

    if cfg.det("PHASE_IMBALANCE", "enabled", default=True):
        detections.extend(_detect_phase_imbalance(derived, seg_start, cfg))

    if cfg.det("COOLING_FAILURE", "enabled", default=True):
        detections.extend(_detect_cooling_failure(characteristics, derived, run_state, seg_start, seg_end, cfg))

    # Масляные детекторы валидны ТОЛЬКО при работающем двигателе (RUN_STATE=3).
    # На остановленном/останавливающемся моторе давление масла = 0 — это норма.
    _oil_valid = set(cfg.det("OIL_DILUTION", "valid_run_states", default=[3]) or [3])
    if cfg.det("OIL_DILUTION", "enabled", default=True) and run_state in _oil_valid:
        detections.extend(_detect_oil_dilution(characteristics, seg_start, cfg))

    if cfg.det("COKING_RISK", "enabled", default=True):
        detections.extend(_detect_coking_risk(accumulators, run_state, load_zone, seg_start, cfg))

    # WARMUP_VIOLATION и COOLDOWN_VIOLATION требуют анализа соседних сегментов —
    # они вызываются из _inter_segment_checks() в segmenter.py, а не здесь.

    if cfg.det("START_FAILURE", "enabled", default=True) and run_state == 1:
        detections.extend(_detect_start_failure(characteristics, derived, seg_start, seg_end, cfg))

    # THERMAL_HIGHLOAD срабатывает только в зонах высокой нагрузки:
    # необратимого накопления нет (thermal_risk обратим — спадает при выходе из ELEVATED).
    if cfg.det("THERMAL_HIGHLOAD", "enabled", default=True) and load_zone in ("ELEVATED", "OVERLOAD"):
        detections.extend(_detect_thermal_highload(accumulators, characteristics, seg_start, cfg))

    _lp_valid = set(cfg.det("LIMIT_PROXIMITY", "valid_run_states", default=[3]) or [3])
    if cfg.det("LIMIT_PROXIMITY", "enabled", default=True) and run_state in _lp_valid:
        detections.extend(_detect_limit_proximity(characteristics, seg_start, cfg))

    if cfg.det("CONTROLLER_FAULT", "enabled", default=True):
        detections.extend(_detect_controller_faults(fault_periods_in_seg, seg_start, seg_end, cfg))

    return detections


# ── LOAD_STEP ────────────────────────────────────────────────────────────────

def _detect_load_step(
    derived: DerivedMetrics,
    seg_start: datetime,
    seg_end: datetime,
    cfg: AnalyticsConfig,
) -> list[Detection]:
    """Детектор наброса/сброса нагрузки с классификацией по ГОСТ ISO 8528-5.

    Класс A (быстрые/электрические): мгновенная скорость dP_dt_max валидна.
    Gate: только RUN_STATE=3 (проверяется в run_all_detectors).

    Классификация результата:
    - INFO  — штатный рампинг (плавное изменение в окне рампинга PCC)
    - INFO  — переходный процесс в пределах нормы выбранного класса ISO
    - WARNING — превышение нормы класса (просадка/заброс/время восстановления)
    """
    if derived.dP_dt_max is None:
        return []

    thr = float(cfg.det("LOAD_STEP", "dP_dt_threshold_kw_per_s", default=50.0))
    if derived.dP_dt_max <= thr:
        return []

    # Параметры ISO 8528-5
    load_class = cfg.det("LOAD_STEP", "load_class_iso", default="G3")
    iso_classes = cfg.det("LOAD_STEP", "iso_classes", default={}) or {}
    class_norm = iso_classes.get(load_class, {})
    freq_drop_norm = float(class_norm.get("freq_drop_pct", 7.0))
    freq_rise_norm = float(class_norm.get("freq_rise_pct", 10.0))
    recovery_norm = float(class_norm.get("freq_recovery_sec", 3.0))

    # Фактические значения переходного процесса
    dip = derived.freq_dip_pct
    rise = derived.freq_rise_pct
    rec = derived.freq_recovery_sec

    # Нарушения нормативов ISO
    violations: list[str] = []
    if dip is not None and dip > freq_drop_norm:
        violations.append(f"freq_dip={dip:.1f}% > {load_class}:{freq_drop_norm:.1f}%")
    if rise is not None and rise > freq_rise_norm:
        violations.append(f"freq_rise={rise:.1f}% > {load_class}:{freq_rise_norm:.1f}%")
    if rec is not None and rec > recovery_norm:
        violations.append(f"recovery={rec:.1f}с > {load_class}:{recovery_norm:.1f}с")

    if violations:
        severity = "WARNING"
        trigger = (
            f"dP_dt_max={derived.dP_dt_max:.2f} кВт/с; "
            f"нарушение {load_class}: {'; '.join(violations)}"
        )
        description_key = "LOAD_STEP.iso_norm_exceeded"
    else:
        severity = cfg.det("LOAD_STEP", "severity_default", default="INFO")
        parts = [f"dP_dt_max={derived.dP_dt_max:.2f} кВт/с"]
        if dip is not None:
            parts.append(f"freq_dip={dip:.1f}%")
        if rec is not None:
            parts.append(f"recovery={rec:.1f}с")
        trigger = f"{'; '.join(parts)} (в пределах {load_class})"
        description_key = "LOAD_STEP.iso_norm_ok"

    return [Detection(
        scenario="LOAD_STEP",
        severity=severity,
        t_detected=_iso(seg_start),
        source="METRIC_RULE",
        trigger=trigger,
        related_roles=["ACTIVE_POWER_TOTAL", "FREQUENCY", "RPM"],
        fault_codes=[],
        description_key=description_key,
        values={
            "dP_dt_max_kw_per_s": derived.dP_dt_max,
            "freq_dip_pct": dip,
            "freq_rise_pct": rise,
            "freq_recovery_sec": rec,
            "load_class_iso": load_class,
            "norm_freq_drop_pct": freq_drop_norm,
            "norm_freq_rise_pct": freq_rise_norm,
            "norm_recovery_sec": recovery_norm,
            "violations": violations,
        },
    )]


# ── PHASE_IMBALANCE ──────────────────────────────────────────────────────────

def _detect_phase_imbalance(
    derived: DerivedMetrics,
    seg_start: datetime,
    cfg: AnalyticsConfig,
) -> list[Detection]:
    imb = derived.current_imbalance_pct_max
    dur = derived.imbalance_duration_sec
    if imb is None:
        return []

    thr_pct = float(cfg.det("PHASE_IMBALANCE", "current_imbalance_warning_pct", default=12.0))
    dur_thr = float(cfg.det("PHASE_IMBALANCE", "duration_warning_sec", default=60.0))

    if imb < thr_pct:
        return []
    if dur is None or dur < dur_thr:
        return []

    return [Detection(
        scenario="PHASE_IMBALANCE",
        severity=cfg.det("PHASE_IMBALANCE", "severity_default", default="WARNING"),
        t_detected=_iso(seg_start),
        source="METRIC_RULE",
        trigger=(f"current_imbalance_pct_max={imb:.1f}% > {thr_pct}% "
                 f"на протяжении {dur:.0f}с > {dur_thr}с"),
        related_roles=["CURRENT_L1", "CURRENT_L2", "CURRENT_L3", "CURRENT_AVG"],
        fault_codes=[],
        description_key="PHASE_IMBALANCE.current_unbalance",
        values={
            "current_imbalance_pct_max": imb,
            "imbalance_duration_sec": dur,
            "threshold_pct": thr_pct,
            "duration_threshold_sec": dur_thr,
        },
    )]


# ── COOLING_FAILURE ──────────────────────────────────────────────────────────

def _detect_cooling_failure(
    chars: dict[str, Any],
    derived: DerivedMetrics,
    run_state: int,
    seg_start: datetime,
    seg_end: datetime,
    cfg: AnalyticsConfig,
) -> list[Detection]:
    """Детектор отказа охлаждения.

    Критерии (все должны выполняться):
    1. RUN_STATE = 3 (установившаяся работа, не переходный режим)
    2. Длительность подсегмента >= min_segment_duration_sec
    3. Устойчивый slope Т ОЖ (°C/ч) превышает порог

    Slope из характеристик (линейная регрессия) отражает ТРЕНД за весь
    подсегмент и нечувствителен к мгновенным качкам. dCoolant_dt_max
    (мгновенный максимум на коротком интервале) для этого детектора НЕ
    применяется — он вызывал ложные ALARM на нормальной тепловой качке.
    """
    results: list[Detection] = []

    # Gate 1: только в установившейся работе
    if run_state != 3:
        return []

    # Gate 2: минимальная длительность
    duration_sec = (_tz(seg_end) - _tz(seg_start)).total_seconds()
    min_dur = float(cfg.det("COOLING_FAILURE", "min_segment_duration_sec", default=300.0))
    if duration_sec < min_dur:
        return []

    # Slope Т ОЖ (°C/ч) из Characteristic.slope (линейная регрессия по реальным точкам)
    cool_char = chars.get("COOLANT_TEMP", {})
    slope = cool_char.get("slope")  # °C/ч; None если < 2 реальных точек
    if slope is None:
        return []

    alarm_thr = float(cfg.det("COOLING_FAILURE", "cooling_slope_alarm_c_per_h", default=40.0))
    warn_thr = float(cfg.det("COOLING_FAILURE", "cooling_slope_warning_c_per_h", default=20.0))

    if slope < warn_thr:
        # Проверяем ещё приближение к порогу HCT Warning по max-значению
        cool_max = cool_char.get("max")
        hct_warn = float(cfg.thr("coolant_temperature", "controller", "warning_c", default=97.8))
        prox_pct = float(cfg.det("COOLING_FAILURE", "proximity_warning_pct", default=10.0))
        prox_thr = hct_warn * (1 - prox_pct / 100)
        if cool_max is not None and cool_max >= prox_thr:
            results.append(Detection(
                scenario="COOLING_FAILURE",
                severity="WARNING",
                t_detected=_iso(seg_start),
                source="PASSPORT_THRESHOLD",
                trigger=(f"coolant_temp_max={cool_max:.1f}°C в пределах "
                         f"{prox_pct:.0f}% от HCT Warning {hct_warn}°C"),
                related_roles=["COOLANT_TEMP"],
                fault_codes=[144],
                description_key="COOLING_FAILURE.approaching_hct_limit",
                values={
                    "coolant_temp_max": cool_max,
                    "hct_warning_c": hct_warn,
                    "proximity_threshold_c": prox_thr,
                },
            ))
        return results

    severity = "ALARM" if slope >= alarm_thr else "WARNING"
    results.append(Detection(
        scenario="COOLING_FAILURE",
        severity=severity,
        t_detected=_iso(seg_start),
        source="METRIC_RULE",
        trigger=(
            f"coolant_slope={slope:.1f} °C/ч > {warn_thr:.1f} °C/ч "
            f"(устойчивый тренд за {duration_sec:.0f}с)"
        ),
        related_roles=["COOLANT_TEMP"],
        fault_codes=[144],
        description_key="COOLING_FAILURE.sustained_overheating_trend",
        values={
            "coolant_slope_c_per_h": slope,
            "warn_threshold_c_per_h": warn_thr,
            "alarm_threshold_c_per_h": alarm_thr,
            "segment_duration_sec": duration_sec,
        },
    ))
    return results


# ── OIL_DILUTION ─────────────────────────────────────────────────────────────

def _detect_oil_dilution(
    chars: dict[str, Any],
    seg_start: datetime,
    cfg: AnalyticsConfig,
) -> list[Detection]:
    oil_press_char = chars.get("OIL_PRESS")
    oil_temp_char = chars.get("OIL_TEMP")
    if not oil_press_char or not oil_temp_char:
        return []

    press_min = oil_press_char.get("min")
    press_med = oil_press_char.get("median")
    temp_max = oil_temp_char.get("max")
    temp_med = oil_temp_char.get("median")

    if press_min is None or temp_max is None or press_med is None or temp_med is None:
        return []

    press_drop_thr = float(cfg.det("OIL_DILUTION", "oil_press_drop_kpa", default=50.0))
    temp_rise_thr = float(cfg.det("OIL_DILUTION", "oil_temp_rise_threshold_c", default=10.0))
    abs_press_warn = float(cfg.det("OIL_DILUTION", "oil_press_abs_warning_kpa", default=310.0))

    press_drop = press_med - press_min
    temp_rise = temp_max - temp_med

    if press_drop >= press_drop_thr and temp_rise >= temp_rise_thr:
        return [Detection(
            scenario="OIL_DILUTION",
            severity=cfg.det("OIL_DILUTION", "severity_default", default="WARNING"),
            t_detected=_iso(seg_start),
            source="METRIC_RULE",
            trigger=(f"oil_press_drop={press_drop:.0f}кПа >= {press_drop_thr:.0f}, "
                     f"oil_temp_rise={temp_rise:.1f}°C >= {temp_rise_thr:.1f}"),
            related_roles=["OIL_PRESS", "OIL_TEMP"],
            fault_codes=[],
            description_key="OIL_DILUTION.combined_pressure_temp_anomaly",
            values={
                "oil_press_drop_kpa": round(press_drop, 1),
                "oil_temp_rise_c": round(temp_rise, 1),
                "oil_press_min": press_min,
                "oil_temp_max": temp_max,
            },
        )]

    # Также: давление ниже паспортного минимума при номинале
    if press_min < abs_press_warn:
        return [Detection(
            scenario="OIL_DILUTION",
            severity="WARNING",
            t_detected=_iso(seg_start),
            source="PASSPORT_THRESHOLD",
            trigger=f"oil_press_min={press_min:.0f}кПа < паспортного минимума {abs_press_warn:.0f}кПа",
            related_roles=["OIL_PRESS"],
            fault_codes=[143, 415],
            description_key="OIL_DILUTION.below_passport_min",
            values={"oil_press_min": press_min, "passport_min_kpa": abs_press_warn},
        )]

    return []


# ── COKING_RISK ──────────────────────────────────────────────────────────────

def _detect_coking_risk(
    acc: RiskAccumulators,
    run_state: int,
    load_zone: str,
    seg_start: datetime,
    cfg: AnalyticsConfig,
) -> list[Detection]:
    cr = acc.coking_risk
    if cr.risk_level == "GREEN":
        return []

    idle_thr = float(cfg.det("COKING_RISK", "idle_duration_warning_sec", default=600.0))
    low_thr = float(cfg.det("COKING_RISK", "low_load_zone_warning_sec", default=3600.0))
    cool_thr = float(cfg.det("COKING_RISK", "coolant_below_60_warning_sec", default=1800.0))

    triggers = []
    if cr.idle_low_rpm_sec >= idle_thr:
        triggers.append(f"idle_low_rpm_sec={cr.idle_low_rpm_sec:.0f}с >= {idle_thr:.0f}с")
    if cr.low_load_zone_sec >= low_thr:
        triggers.append(f"low_load_zone_sec={cr.low_load_zone_sec:.0f}с >= {low_thr:.0f}с")
    if cr.coolant_below_60_sec >= cool_thr:
        triggers.append(f"coolant_below_60_sec={cr.coolant_below_60_sec:.0f}с >= {cool_thr:.0f}с")

    if not triggers:
        return []

    return [Detection(
        scenario="COKING_RISK",
        severity=cfg.det("COKING_RISK", "severity_default", default="WARNING"),
        t_detected=_iso(seg_start),
        source="METRIC_RULE",
        trigger="; ".join(triggers),
        related_roles=["COOLANT_TEMP", "RPM", "LOAD_PCT"],
        fault_codes=[2342],
        description_key=f"COKING_RISK.risk_{cr.risk_level.lower()}",
        values={
            "risk_level": cr.risk_level,
            "idle_low_rpm_sec": cr.idle_low_rpm_sec,
            "low_load_zone_sec": cr.low_load_zone_sec,
            "coolant_below_60_sec": cr.coolant_below_60_sec,
            "last_purge_ts": cr.last_purge_ts,
        },
    )]


# ── WARMUP_VIOLATION ─────────────────────────────────────────────────────────

def _detect_warmup_violation(
    chars: dict[str, Any],
    seg_start: datetime,
    seg_end: datetime,
    cfg: AnalyticsConfig,
) -> list[Detection]:
    duration_sec = (_tz(seg_end) - _tz(seg_start)).total_seconds()
    min_warmup = float(cfg.det("WARMUP_VIOLATION", "min_warmup_sec", default=180.0))
    cold_thr = float(cfg.det("WARMUP_VIOLATION", "cold_start_coolant_c", default=21.0))

    # Проверяем что это был холодный пуск
    cool_char = chars.get("COOLANT_TEMP")
    if cool_char is None:
        return []
    start_temp = cool_char.get("value_start")
    if start_temp is None or start_temp >= cold_thr:
        return []  # не холодный пуск

    if duration_sec >= min_warmup:
        return []

    return [Detection(
        scenario="WARMUP_VIOLATION",
        severity=cfg.det("WARMUP_VIOLATION", "severity_default", default="WARNING"),
        t_detected=_iso(seg_start),
        source="PASSPORT_THRESHOLD",
        trigger=(f"warmup_duration={duration_sec:.0f}с < {min_warmup:.0f}с "
                 f"при холодном пуске (Т ОЖ={start_temp:.1f}°C < {cold_thr}°C)"),
        related_roles=["COOLANT_TEMP"],
        fault_codes=[],
        description_key="WARMUP_VIOLATION.insufficient_warmup",
        values={
            "warmup_duration_sec": duration_sec,
            "required_sec": min_warmup,
            "coolant_start_c": start_temp,
            "cold_threshold_c": cold_thr,
        },
    )]


# ── COOLDOWN_VIOLATION ───────────────────────────────────────────────────────

def _detect_cooldown_violation(
    prev_zone: str | None,
    seg_start: datetime,
    cfg: AnalyticsConfig,
) -> list[Detection]:
    required_after = cfg.det("COOLDOWN_VIOLATION", "required_after_zone", default="ELEVATED")
    if prev_zone != required_after:
        return []

    return [Detection(
        scenario="COOLDOWN_VIOLATION",
        severity=cfg.det("COOLDOWN_VIOLATION", "severity_default", default="WARNING"),
        t_detected=_iso(seg_start),
        source="PASSPORT_THRESHOLD",
        trigger=f"Останов после зоны {prev_zone} без фазы охлаждения (RUN_STATE 4/5)",
        related_roles=["COOLANT_TEMP"],
        fault_codes=[611],
        description_key="COOLDOWN_VIOLATION.no_cooldown_after_elevated",
        values={"previous_zone": prev_zone, "required_after_zone": required_after},
    )]


# ── START_FAILURE ─────────────────────────────────────────────────────────────

def _detect_start_failure(
    chars: dict[str, Any],
    derived: DerivedMetrics,
    seg_start: datetime,
    seg_end: datetime,
    cfg: AnalyticsConfig,
) -> list[Detection]:
    results: list[Detection] = []

    crank_dur = (_tz(seg_end) - _tz(seg_start)).total_seconds()
    crank_alarm = float(cfg.det("START_FAILURE", "crank_time_alarm_sec", default=45.0))
    if crank_dur >= crank_alarm:
        results.append(Detection(
            scenario="START_FAILURE",
            severity=cfg.det("START_FAILURE", "severity_default", default="ALARM"),
            t_detected=_iso(seg_start),
            source="PASSPORT_THRESHOLD",
            trigger=f"crank_duration={crank_dur:.0f}с >= {crank_alarm:.0f}с",
            related_roles=["RPM"],
            fault_codes=[359, 1438],
            description_key="START_FAILURE.excessive_crank_time",
            values={"crank_duration_sec": crank_dur, "threshold_sec": crank_alarm},
        ))

    # Просадка АКБ
    bat_char = chars.get("BATTERY_V")
    if bat_char:
        bat_min = bat_char.get("min")
        sag_alarm = float(cfg.det("START_FAILURE", "battery_sag_alarm_v", default=19.0))
        if bat_min is not None and bat_min < sag_alarm:
            results.append(Detection(
                scenario="START_FAILURE",
                severity="ALARM",
                t_detected=_iso(seg_start),
                source="METRIC_RULE",
                trigger=f"battery_min={bat_min:.1f}V < {sag_alarm:.1f}V при прокрутке",
                related_roles=["BATTERY_V"],
                fault_codes=[1442, 1443],
                description_key="START_FAILURE.battery_sag",
                values={"battery_min_v": bat_min, "threshold_v": sag_alarm},
            ))

    return results


# ── THERMAL_HIGHLOAD ─────────────────────────────────────────────────────────

def _detect_thermal_highload(
    acc: RiskAccumulators,
    chars: dict[str, Any],
    seg_start: datetime,
    cfg: AnalyticsConfig,
) -> list[Detection]:
    tr = acc.thermal_risk
    if tr.risk_level == "GREEN":
        return []

    cool_prox = float(cfg.det("THERMAL_HIGHLOAD", "coolant_proximity_pct", default=10.0))
    oil_prox = float(cfg.det("THERMAL_HIGHLOAD", "oil_proximity_pct", default=10.0))
    hct_warn = float(cfg.thr("coolant_temperature", "controller", "warning_c", default=97.8))
    hot_warn = float(cfg.thr("oil_temperature", "controller", "warning_c", default=105.0))

    cool_thr = hct_warn * (1 - cool_prox / 100)
    oil_thr = hot_warn * (1 - oil_prox / 100)

    triggers = [f"thermal_risk={tr.risk_level}, elevated_zone_sec={tr.elevated_zone_sec:.0f}с"]
    if tr.coolant_near_limit_sec > 0:
        triggers.append(f"coolant near HCT: {tr.coolant_near_limit_sec:.0f}с >= {cool_thr:.1f}°C")
    if tr.oil_near_limit_sec > 0:
        triggers.append(f"oil near HOT: {tr.oil_near_limit_sec:.0f}с >= {oil_thr:.1f}°C")

    return [Detection(
        scenario="THERMAL_HIGHLOAD",
        severity=cfg.det("THERMAL_HIGHLOAD", "severity_default", default="WARNING"),
        t_detected=_iso(seg_start),
        source="METRIC_RULE",
        trigger="; ".join(triggers),
        related_roles=["COOLANT_TEMP", "OIL_TEMP", "LOAD_PCT"],
        fault_codes=[144, 212],
        description_key=f"THERMAL_HIGHLOAD.risk_{tr.risk_level.lower()}",
        values={
            "risk_level": tr.risk_level,
            "elevated_zone_sec": tr.elevated_zone_sec,
            "coolant_near_limit_sec": tr.coolant_near_limit_sec,
            "oil_near_limit_sec": tr.oil_near_limit_sec,
        },
    )]


# ── LIMIT_PROXIMITY ──────────────────────────────────────────────────────────

def _detect_limit_proximity(
    chars: dict[str, Any],
    seg_start: datetime,
    cfg: AnalyticsConfig,
) -> list[Detection]:
    prox_pct = float(cfg.det("LIMIT_PROXIMITY", "proximity_pct", default=10.0))
    results: list[Detection] = []

    checks = [
        ("OIL_PRESS", "min",
         float(cfg.thr("oil_pressure", "controller", "shutdown_rated_kpa", default=241.3)),
         "oil_press_min", "Давление масла", [415], True),
        ("COOLANT_TEMP", "max",
         float(cfg.thr("coolant_temperature", "controller", "shutdown_c", default=103.9)),
         "coolant_temp_max", "Температура ОЖ", [151], False),
        ("OIL_TEMP", "max",
         float(cfg.thr("oil_temperature", "controller", "shutdown_c", default=110.0)),
         "oil_temp_max", "Температура масла", [214], False),
    ]

    for role, stat_key, limit, val_key, label, fcodes, below_is_bad in checks:
        char = chars.get(role)
        if not char:
            continue
        val = char.get(stat_key)
        if val is None:
            continue

        if below_is_bad:
            # Плохо = значение слишком низкое: val < limit * (1 + prox_pct/100)
            near_thr = limit * (1 + prox_pct / 100)
            if val < near_thr:
                results.append(_limit_detection(role, val, limit, label, fcodes, seg_start, cfg))
        else:
            # Плохо = значение слишком высокое: val > limit * (1 - prox_pct/100)
            near_thr = limit * (1 - prox_pct / 100)
            if val > near_thr:
                results.append(_limit_detection(role, val, limit, label, fcodes, seg_start, cfg))

    return results


def _limit_detection(role, val, limit, label, fcodes, seg_start, cfg) -> Detection:
    prox_pct = float(cfg.det("LIMIT_PROXIMITY", "proximity_pct", default=10.0))
    return Detection(
        scenario="LIMIT_PROXIMITY",
        severity=cfg.det("LIMIT_PROXIMITY", "severity_default", default="WARNING"),
        t_detected=_iso(seg_start),
        source="PASSPORT_THRESHOLD",
        trigger=f"{label}: {val:.2f} в пределах {prox_pct:.0f}% от порога {limit:.1f}",
        related_roles=[role],
        fault_codes=fcodes,
        description_key=f"LIMIT_PROXIMITY.{role.lower()}_near_limit",
        values={"value": val, "shutdown_limit": limit, "proximity_pct": prox_pct},
    )


# ── CONTROLLER_FAULT ─────────────────────────────────────────────────────────

def _detect_controller_faults(
    fault_periods: list[dict[str, Any]],
    seg_start: datetime,
    seg_end: datetime,
    cfg: AnalyticsConfig,
) -> list[Detection]:
    results: list[Detection] = []
    t_start = _tz(seg_start)
    t_end = _tz(seg_end)

    for fp in fault_periods:
        fs = _tz(fp["fault_start"])
        fe_raw = fp.get("fault_end")
        fe = _tz(fe_raw) if fe_raw else t_end

        if fs >= t_end or fe <= t_start:
            continue

        # severity=None означает, что для бита не задана серьёзность → INFO (игнорируем).
        # fp.get("severity", "warning") возвращает None если ключ есть со значением NULL,
        # поэтому явно проверяем на отсутствие/пустоту и маппим на "none" → INFO.
        raw_sev = fp.get("severity") or "none"
        severity = cfg.bitmap_severity(raw_sev)
        if severity == "INFO":
            continue  # информационные биты (ReadyToLoad, NotInAuto и т.п.) не детектируем

        name = fp.get("fault_name_ru") or fp.get("fault_name") or f"addr={fp['addr']} bit={fp['bit']}"
        addr = fp.get("addr", 0)
        bit = fp.get("bit", 0)

        results.append(Detection(
            scenario="CONTROLLER_FAULT",
            severity=severity,
            t_detected=_iso(fs),
            source="CONTROLLER_FAULT",
            trigger=f"{name} (addr={addr}, bit={bit})",
            related_roles=[f"FAULT_MASK_{addr - 40400}" if 40400 <= addr <= 40415 else str(addr)],
            fault_codes=[],
            description_key=f"CONTROLLER_FAULT.addr_{addr}_bit_{bit}",
            values={
                "addr": addr,
                "bit": bit,
                "fault_name": name,
                "fault_start": _iso(fs),
                "fault_end": _iso(fe) if fe_raw else None,
                "duration_sec": fp.get("duration_sec"),
                "raw_severity": raw_sev,
            },
        ))

    return results
