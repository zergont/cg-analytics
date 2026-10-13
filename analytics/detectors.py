# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

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


def panel_codes_in(periods: list[dict] | None, t0: datetime, t1: datetime) -> set[int]:
    """Коды, которые 40012 принимал в [t0, t1] — по периодам состояния (v5.1.1)."""
    if isinstance(t0, str):
        t0 = datetime.fromisoformat(t0)
    if isinstance(t1, str):
        t1 = datetime.fromisoformat(t1)
    out: set[int] = set()
    for p in periods or []:
        try:
            v = int(p.get("value") or 0)
        except (TypeError, ValueError):
            continue
        # Граница включительно: код, выставленный ровно на конце подсегмента (рез по
        # самой неисправности), относится к нему (151 у ДЭС №3 — 0 из 2 без этого)
        if v <= 0 or _tz(p["state_start"]) > _tz(t1):
            continue
        if p.get("state_end") is not None and _tz(p["state_end"]) <= _tz(t0):
            continue
        out.add(v)
    return out


def confirm_panel_codes(dets: list, codes: set[int]) -> None:
    """Аналитике — только коды, которые панель реально выставила (решение B, v5.1.6).

    Статичные списки детекторов (OIL_DILUTION → 143/415, LIMIT_PROXIMITY → 214…)
    становятся справочными (ref_codes): за 60 дней панель ни разу не выставила
    143, 415, 214, 1448, 2342, а модель по их справочнику советовала «нажмите СБРОС».
    """
    for det in dets:
        if det.scenario == "CONTROLLER_FAULT":
            continue      # у панели код настоящий
        if not det.ref_codes:
            det.ref_codes = list(det.fault_codes)
        det.fault_codes = [c for c in det.ref_codes if c in codes]


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
    state_periods: dict[int, list[dict]] | None = None,
) -> list[Detection]:
    """Запустить все включённые детекторы, вернуть список сработавших Detection."""
    detections: list[Detection] = []

    # LOAD_STEP: Класс A (быстрые/электрические) — только в RUN_STATE=3
    if cfg.det("LOAD_STEP", "enabled", default=True) and run_state == 3:
        detections.extend(_detect_load_step(derived, seg_start, seg_end, cfg))

    # NEGATIVE_SEQUENCE заменяет PHASE_IMBALANCE (Addendum v1.5):
    # - физически корректная величина I₂ вместо грубого перекоса |I_k - I_avg|
    # - тот же gate: RUN_STATE=3 (задан в valid_run_states конфига)
    _ns_valid = set(cfg.det("NEGATIVE_SEQUENCE", "valid_run_states", default=[3]) or [3])
    if cfg.det("NEGATIVE_SEQUENCE", "enabled", default=True) and run_state in _ns_valid:
        detections.extend(_detect_negative_sequence(derived, seg_start, cfg))

    # Перекос фазных токов и ток нейтрали (v4.9.97): то, чего I₂ не видит —
    # провал одной фазы с током через нейтраль. Пороги из норм, см. KB.
    for _scn, _fn in (("PHASE_CURRENT_SPREAD", _detect_phase_current_spread),
                      ("NEUTRAL_CURRENT", _detect_neutral_current)):
        _valid = set(cfg.det(_scn, "valid_run_states", default=[3]) or [3])
        if cfg.det(_scn, "enabled", default=True) and run_state in _valid:
            detections.extend(_fn(derived, seg_start, cfg))

    if cfg.det("COOLING_FAILURE", "enabled", default=True):
        detections.extend(_detect_cooling_failure(characteristics, derived, run_state, seg_start, seg_end, cfg))

    # Масляные детекторы валидны ТОЛЬКО при работающем двигателе (RUN_STATE=3).
    # На остановленном/останавливающемся моторе давление масла = 0 — это норма.
    _oil_valid = set(cfg.det("OIL_DILUTION", "valid_run_states", default=[3]) or [3])
    if cfg.det("OIL_DILUTION", "enabled", default=True) and run_state in _oil_valid:
        detections.extend(_detect_oil_dilution(characteristics, derived, seg_start, cfg, seg_end))

    # Риск нагара — накопленное состояние, а не событие подсегмента (v5.1.3):
    # детекции нет, его несут часть сводки «нагар» и таблица «Риски» отчёта.
    # Раньше детекция с кодом 2342 повторялась в каждом подсегменте (ДЭС №3 —
    # 501 за месяц) и на стоящей машине читалась моделью как «зафиксировал».

    if cfg.det("IDLE_TOO_LONG", "enabled", default=True) and run_state in (2, 5):
        detections.extend(_detect_idle_too_long(accumulators, seg_start, cfg))

    if cfg.det("NO_LOAD_TOO_LONG", "enabled", default=True) and run_state == 3:
        detections.extend(_detect_no_load_too_long(accumulators, seg_start, cfg))

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
        detections.extend(_detect_controller_faults(
            fault_periods_in_seg, characteristics, seg_start, seg_end, cfg, state_periods
        ))

    if cfg.det("RPM_UNDERSPEED", "enabled", default=True) and run_state == 3:
        detections.extend(_detect_rpm_underspeed(characteristics, derived, seg_start, cfg))

    lfc = (state_periods or {}).get(cfg.role_to_addr("LAST_FAULT_CODE")) or []
    confirm_panel_codes(detections, panel_codes_in(lfc, seg_start, seg_end))
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
    - CAUTION — превышение нормы класса (просадка/заброс/время восстановления)
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
        severity = "CAUTION"
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


# ── NEGATIVE_SEQUENCE ────────────────────────────────────────────────────────

def _detect_negative_sequence(
    derived: DerivedMetrics,
    seg_start: datetime,
    cfg: AnalyticsConfig,
) -> list[Detection]:
    """Детектор тока обратной последовательности I₂ (Addendum v1.5).

    Физически корректная метрика несимметрии фаз.
    Заводская защита PCC3300 (Neg Seq Overcurrent) реагирует на I₂%, а НЕ на
    грубый перекос |I_k - I_avg|/I_avg (расхождение ~в 6-8 раз).

    Порог — из паспорта генератора, а не от заводской уставки панели: Stamford
    (генераторы на Cummins) допускает длительно ≤8% номинала и советует защиту
    на 8%; уставка PCC3300 12% выше этого предела. Предупреждаем раньше (KB).
    Gate: RUN_STATE=3 (проверяется в run_all_detectors).
    """
    i2_pct = derived.neg_seq_i2_pct_max
    dur = derived.neg_seq_i2_duration_sec

    if i2_pct is None:
        return []

    prox_thr = float(cfg.det("NEGATIVE_SEQUENCE", "i2_proximity_warning_pct", default=6.0))
    dur_thr = float(cfg.det("NEGATIVE_SEQUENCE", "duration_warning_sec", default=60.0))
    factory_thr = float(cfg.det("NEGATIVE_SEQUENCE", "factory_threshold_pct", default=12.0))
    norm_ref = cfg.det("NEGATIVE_SEQUENCE", "norm_ref", default="")

    if i2_pct < prox_thr:
        return []
    if dur is None or dur < dur_thr:
        return []

    return [Detection(
        scenario="NEGATIVE_SEQUENCE",
        severity=cfg.det("NEGATIVE_SEQUENCE", "severity_default", default="CAUTION"),
        t_detected=_iso(seg_start),
        source="METRIC_RULE",
        trigger=(
            f"I₂={i2_pct:.1f}% > {prox_thr:.0f}% "
            f"(заводской порог PCC3300: {factory_thr:.0f}%"
            + (f"; {norm_ref}" if norm_ref else "") + ") "
            f"за {dur:.0f}с > {dur_thr:.0f}с"
        ),
        related_roles=[
            "CURRENT_L1", "CURRENT_L2", "CURRENT_L3",
            "ACTIVE_POWER_L1", "ACTIVE_POWER_L2", "ACTIVE_POWER_L3",
            "REACTIVE_POWER_L1", "REACTIVE_POWER_L2", "REACTIVE_POWER_L3",
        ],
        fault_codes=[],
        description_key="NEGATIVE_SEQUENCE.approaching_factory_threshold",
        values={
            "i2_pct_max": i2_pct,
            "i2_pct_med": derived.neg_seq_i2_pct_med,
            "duration_sec": dur,
            "proximity_threshold_pct": prox_thr,
            "factory_threshold_pct": factory_thr,
            "norm_ref": norm_ref or None,
        },
    )]


# ── PHASE_CURRENT_SPREAD / NEUTRAL_CURRENT ───────────────────────────────────

def _detect_phase_current_spread(
    derived: DerivedMetrics,
    seg_start: datetime,
    cfg: AnalyticsConfig,
) -> list[Detection]:
    """Перекос фазных токов: I_max − I_min в % номинального тока генератора.

    Норма — ПТЭД 1993 п. 4.1.8: длительная работа ДЭС допустима при разности
    токов фаз не более 20% номинального. В отличие от I₂ видит провал одной
    фазы: ток уходит в нейтраль, обратная последовательность почти не растёт.
    Срабатывание — непрерывное превышение не короче выдержки (KB).
    """
    return _metric_run_detection(
        derived.phase_spread_pct_max, derived.phase_spread_pct_med,
        derived.phase_spread_run_sec, seg_start, cfg,
        scenario="PHASE_CURRENT_SPREAD", thr_key="spread_warning_pct", thr_default=20.0,
        what="Перекос фазных токов I_max − I_min",
        related=["CURRENT_L1", "CURRENT_L2", "CURRENT_L3"],
        description_key="PHASE_CURRENT_SPREAD.above_norm",
    )


def _detect_neutral_current(
    derived: DerivedMetrics,
    seg_start: datetime,
    cfg: AnalyticsConfig,
) -> list[Detection]:
    """Ток нейтрали в % номинального тока генератора.

    Норма — ГОСТ 11677-85 п. 3.9.6 (нейтраль НН трансформатора Y/Yн: 25%
    номинального тока длительно) и Stamford AGN 017 (нагрузка фаза–нейтраль
    не более 25% номинала генератора). Панель PCC3300 ток нейтрали не
    контролирует — защиты 50N/51N у неё нет, это видит только аналитика.
    """
    return _metric_run_detection(
        derived.neutral_current_pct_max, derived.neutral_current_pct_med,
        derived.neutral_current_run_sec, seg_start, cfg,
        scenario="NEUTRAL_CURRENT", thr_key="neutral_warning_pct", thr_default=25.0,
        what="Ток нейтрали",
        related=["CURRENT_L1", "CURRENT_L2", "CURRENT_L3",
                 "ACTIVE_POWER_L1", "ACTIVE_POWER_L2", "ACTIVE_POWER_L3",
                 "REACTIVE_POWER_L1", "REACTIVE_POWER_L2", "REACTIVE_POWER_L3"],
        description_key="NEUTRAL_CURRENT.above_norm",
    )


def _metric_run_detection(
    pct_max: float | None, pct_med: float | None, run_sec: float | None,
    seg_start: datetime, cfg: AnalyticsConfig, *,
    scenario: str, thr_key: str, thr_default: float, what: str,
    related: list[str], description_key: str,
) -> list[Detection]:
    """Общий вид: метрика в % I_ном, порог и выдержка из KB, ссылка на норму.

    run_sec — уже посчитанный в метриках самый длинный непрерывный участок
    выше порога из KB, так что здесь он только сравнивается с выдержкой.
    """
    if pct_max is None or run_sec is None:
        return []
    thr = float(cfg.det(scenario, thr_key, default=thr_default))
    dur_thr = float(cfg.det(scenario, "duration_warning_sec", default=60.0))
    if pct_max < thr or run_sec < dur_thr:
        return []
    norm_ref = cfg.det(scenario, "norm_ref", default="")
    return [Detection(
        scenario=scenario,
        severity=cfg.det(scenario, "severity_default", default="CAUTION"),
        t_detected=_iso(seg_start),
        source="METRIC_RULE",
        trigger=(
            f"{what}: до {pct_max:.0f}% номинального тока ≥ {thr:.0f}%"
            + (f" ({norm_ref})" if norm_ref else "")
            + f", непрерывно {run_sec:.0f}с ≥ {dur_thr:.0f}с"
        ),
        related_roles=related,
        fault_codes=[],
        description_key=description_key,
        values={
            "pct_max": pct_max,
            "pct_med": pct_med,
            "run_sec": run_sec,
            "threshold_pct": thr,
            "duration_threshold_sec": dur_thr,
            "norm_ref": norm_ref or None,
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
    """Детектор отказа охлаждения (v5.1.15, решение владельца 13.10, вариант Г).

    БЛИЗОСТЬ к уставке HCT Warning панели — главная проверка: максимум ОЖ за
    подсегмент в пределах proximity_warning_pct от уставки. Проверяется ВСЕГДА в
    работе — при любом наклоне, с любого значения на начало подсегмента и в
    прогреве (раньше молчала при высоком наклоне, у ДГУ №2 с рабочей полосой
    77–79 °C не проверялась вовсе из-за входа «≥ working_range_low на начало»).

    ТРЕНДА больше нет: датчик ОЖ шагает по 1 °C и пишет по изменению, наклон
    по подсегменту считался по 2–3 показаниям — качание термостата 84→85 за 80 с
    давало «устойчивый тренд за 469 с» (ДЭС №3 — 76, ДГУ №1 — 58 ложных за 73 дня
    вне жары). Честный тренд по окну опережает близость на −1…+2 мин.

    ПРОГРЕВ (фаза 1, асимптота) — как было; при срабатывании близости не нужна.

    Gate: только RUN_STATE=3. Близость — на подсегменте любой длины (уровень —
    факт: у ДЭС №3 зона нагрузки скачет каждые 1–4 мин, и 14.08 ОЖ 90 °C пришлась
    на подсегмент 4 мин 39 с); прогрев — от min_segment_duration_sec.
    """
    results: list[Detection] = []

    if run_state != 3:
        return []

    duration_sec = (_tz(seg_end) - _tz(seg_start)).total_seconds()
    min_dur = float(cfg.det("COOLING_FAILURE", "min_segment_duration_sec", default=300.0))

    cool_char = chars.get("COOLANT_TEMP", {})
    t_start_val = cool_char.get("value_start")
    t_end_val = cool_char.get("value_end")
    cool_max = cool_char.get("max")

    # ── Близость к уставке HCT Warning панели ───────────────────────────────
    hct_warn = float(cfg.thr("coolant_temperature", "controller", "warning_c", default=97.8))
    prox_pct = float(cfg.det("COOLING_FAILURE", "proximity_warning_pct", default=10.0))
    prox_thr = hct_warn * (1 - prox_pct / 100)
    if cool_max is not None and cool_max >= prox_thr:
        results.append(Detection(
            scenario="COOLING_FAILURE",
            severity="CAUTION",
            t_detected=_iso(seg_start),
            source="PASSPORT_THRESHOLD",
            trigger=(f"ОЖ дошла до {cool_max:.0f} °C — не ниже {prox_thr:.1f} °C "
                     f"({prox_pct:.0f} % до уставки предупреждения панели {hct_warn:g} °C)"),
            related_roles=["COOLANT_TEMP"],
            fault_codes=[144],
            description_key="COOLING_FAILURE.approaching_hct_limit",
            values={"coolant_temp_max": cool_max, "hct_warning_c": hct_warn,
                    "proximity_threshold_c": prox_thr},
        ))
        return results

    # ── Прогрев: пересечение нижней границы рабочего диапазона ──────────────
    if duration_sec < min_dur:
        return results
    working_low = float(cfg.thr("coolant_temperature", "tunable", "working_range_low_c", default=80.0))
    asym_norm = float(cfg.thr("coolant_temperature", "tunable", "asymptote_norm_max_c", default=92.0))
    warming_crossing = (
        t_start_val is not None
        and t_end_val is not None
        and t_start_val < working_low <= t_end_val
    )
    if warming_crossing:
        T_asym = derived.coolant_asymptote_c
        if T_asym is not None and T_asym > asym_norm:
            results.append(Detection(
                scenario="COOLING_FAILURE",
                severity="CAUTION",
                t_detected=_iso(seg_start),
                source="METRIC_RULE",
                trigger=(
                    f"T_асимпт={T_asym:.1f}°C > {asym_norm:.1f}°C "
                    f"— прогрев нацелен выше нормы при входе в рабочий диапазон"
                ),
                related_roles=["COOLANT_TEMP"],
                fault_codes=[144],
                description_key="COOLING_FAILURE.warmup_asymptote_above_norm",
                values={
                    "coolant_asymptote_c": T_asym,
                    "asymptote_norm_c": asym_norm,
                    "working_range_low_c": working_low,
                    "coolant_t_end": t_end_val,
                },
            ))
    return results


# ── OIL_DILUTION ─────────────────────────────────────────────────────────────

# В живом составе нарушение паспорта — пока идёт или закончилось не раньше
# стольких секунд назад (online.engine._extract_open_segment_data), v5.1.12
PASSPORT_RECENT_SEC = 600


def passport_trigger(lim: dict[str, Any]) -> str:
    """Текст срабатывания «ниже паспорта» по values.limit детекции."""
    run = float(lim.get("run_sec") or 0)
    run_txt = f"{run / 60:.0f} мин" if run >= 120 else f"{run:.0f} с"
    engine = lim.get("engine") or "двигателя"
    limit, value = lim.get("limit"), lim.get("value")
    if lim.get("hard"):
        return (f"давление масла ниже паспортного минимума {engine} {limit:.0f} кПа — "
                f"нарушение паспорта: мин. {value:.0f} кПа, ниже паспорта непрерывно {run_txt}")
    brief = lim.get("brief_min")
    brief_txt = f" (кратковременно до {brief:.0f})" if brief is not None else ""
    return (f"давление масла ниже паспортного минимума {engine} {limit:.0f} кПа "
            f"в пределах шага датчика {float(lim.get('step') or 0):.0f} кПа: мин. {value:.0f} кПа"
            f"{brief_txt}, непрерывно {run_txt} — не норма")


def _passport_detection(derived: DerivedMetrics, seg_start: datetime,
                        cfg: AnalyticsConfig) -> Detection | None:
    """Давление масла ниже паспорта своего двигателя (v5.1.12, решения 12.10).

    Ниже паспорта устойчиво (≥ выдержки) — CAUTION «ниже паспорта». Ниже больше
    чем на шаг датчика — «нарушение паспорта»: гейт его не снимает (код), в
    пределах шага — снимаемо, но нормой не называется. limit — для гейта,
    статуса и заключения; oil_press_min/passport_min_kpa — для сводки отчёта.
    """
    min_run = float(cfg.det("OIL_DILUTION", "passport_min_run_sec", default=60))
    limit = derived.oil_press_passport_kpa
    run = derived.oil_press_below_passport_run_sec or 0.0
    if limit is None or run < min_run or derived.oil_press_below_passport_min_kpa is None:
        return None
    hard = (derived.oil_press_hard_run_sec or 0.0) >= min_run
    step = derived.oil_press_step_kpa or 0.0
    brief = None
    if hard and derived.oil_press_hard_min_kpa is not None:
        value = derived.oil_press_hard_min_kpa
    else:
        # В пределах шага — минимум показаний в пределах шага; короткий провал
        # дальше шага (< выдержки) называем отдельно, а не «в пределах шага, мин. 232»
        value = (derived.oil_press_instep_min_kpa
                 if derived.oil_press_instep_min_kpa is not None
                 else derived.oil_press_below_passport_min_kpa)
        if (derived.oil_press_below_passport_min_kpa is not None and value is not None
                and derived.oil_press_below_passport_min_kpa < value):
            brief = derived.oil_press_below_passport_min_kpa
    limit_d = {
        "kind": "passport", "param": "OIL_PRESS", "direction": "min",
        "limit": limit, "unit": "кПа", "value": value,
        "run_sec": run, "min_run_sec": min_run,
        "step": step, "step_source": derived.oil_press_step_source,
        "hard": hard, "engine": cfg.engine_label, "brief_min": brief,
        "until": derived.oil_press_passport_until,
        "ongoing": derived.oil_press_passport_ongoing,
        # Свежесть «нарушения» отдельно: в живом составе давнее нарушение при
        # свежем «в пределах шага» не держит запрет снятия (ревью v5.1.12)
        "last_value": derived.oil_press_last_min_kpa,
        "hard_until": derived.oil_press_hard_until,
        "hard_ongoing": derived.oil_press_hard_ongoing,
    }
    return Detection(
        scenario="OIL_DILUTION",
        severity="CAUTION",
        t_detected=_iso(seg_start),
        source="PASSPORT_THRESHOLD",
        trigger=passport_trigger(limit_d),
        related_roles=["OIL_PRESS"],
        fault_codes=[143, 415],
        description_key="OIL_DILUTION.below_passport_min",
        values={"oil_press_min": value, "passport_min_kpa": limit, "limit": limit_d},
    )


def _detect_oil_dilution(
    chars: dict[str, Any],
    derived: DerivedMetrics,
    seg_start: datetime,
    cfg: AnalyticsConfig,
    seg_end: datetime | None = None,
) -> list[Detection]:
    # Паспорт — первым и без температуры масла: раньше ветка паспорта шла после
    # комбинированной и пропадала без OIL_TEMP (v5.1.12). Паспорт главнее всегда:
    # комбинированная ветка считает по всему подсегменту и сработала бы на том же
    # провале — нарушение паспорта пропало бы из записи подсегмента (ревью)
    passport = _passport_detection(derived, seg_start, cfg)
    if passport is not None:
        return [passport]
    combined = _combined_oil_detection(chars, seg_start, cfg)
    return [combined] if combined else []


def _combined_oil_detection(
    chars: dict[str, Any], seg_start: datetime, cfg: AnalyticsConfig,
) -> Detection | None:
    """Падение давления вместе с ростом температуры масла — признак разжижения."""
    oil_press_char = chars.get("OIL_PRESS")
    oil_temp_char = chars.get("OIL_TEMP")
    if not oil_press_char or not oil_temp_char:
        return None

    press_min = oil_press_char.get("min")
    press_med = oil_press_char.get("median")
    temp_max = oil_temp_char.get("max")
    temp_med = oil_temp_char.get("median")

    if press_min is None or temp_max is None or press_med is None or temp_med is None:
        return None

    press_drop_thr = float(cfg.det("OIL_DILUTION", "oil_press_drop_kpa", default=50.0))
    temp_rise_thr = float(cfg.det("OIL_DILUTION", "oil_temp_rise_threshold_c", default=10.0))

    press_drop = press_med - press_min
    temp_rise = temp_max - temp_med

    if press_drop >= press_drop_thr and temp_rise >= temp_rise_thr:
        return Detection(
            scenario="OIL_DILUTION",
            severity=cfg.det("OIL_DILUTION", "severity_default", default="CAUTION"),
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
        )

    return None


# ── COKING_RISK ──────────────────────────────────────────────────────────────

def _detect_idle_too_long(
    acc: RiskAccumulators,
    seg_start: datetime,
    cfg: AnalyticsConfig,
) -> list[Detection]:
    """Непрерывный холостой ход дольше нормы РЭ — замечание сегмента (v5.1.3).

    Норма — один отрезок х.х. (K38-50 РЭ, Cummins O&M: ≤10 мин; код панели
    2342 Max Idle Time тоже про непрерывный). Суммарный х.х. идёт в долг риска
    нагара, а не сюда.
    """
    limit = float(cfg.det("IDLE_TOO_LONG", "max_continuous_sec", default=600))
    run = acc.coking_risk.idle_run_sec
    if run <= limit:
        return []
    return [Detection(
        scenario="IDLE_TOO_LONG",
        severity=cfg.det("IDLE_TOO_LONG", "severity_default", default="CAUTION"),
        t_detected=_iso(seg_start),
        source="METRIC_RULE",
        trigger=f"холостой ход {run / 60:.0f} мин подряд при норме РЭ ≤{limit / 60:.0f} мин",
        related_roles=["RPM"],
        fault_codes=[],
        description_key="IDLE_TOO_LONG.default",
        values={"idle_run_sec": run, "limit_sec": limit},
    )]


def _detect_no_load_too_long(
    acc: RiskAccumulators,
    seg_start: datetime,
    cfg: AnalyticsConfig,
) -> list[Detection]:
    """Работа без нагрузки на номинальных оборотах дольше нормы Cummins (v5.1.5).

    A035B516 вып. 6 §5.4.2: без нагрузки — не дольше 15 мин подряд. Детектор
    холостого хода (RUN_STATE 2/5) машину, крутящуюся на номинале в ожидании
    нагрузки, не видит.
    """
    limit = float(cfg.det("NO_LOAD_TOO_LONG", "max_continuous_sec", default=900))
    pct = float(cfg.det("NO_LOAD_TOO_LONG", "load_pct", default=10))
    run = acc.coking_risk.noload_run_sec
    if run <= limit:
        return []
    return [Detection(
        scenario="NO_LOAD_TOO_LONG",
        severity=cfg.det("NO_LOAD_TOO_LONG", "severity_default", default="CAUTION"),
        t_detected=_iso(seg_start),
        source="METRIC_RULE",
        trigger=(f"без нагрузки (<{pct:.0f} %) на номинальных оборотах {run / 60:.0f} мин подряд "
                 f"при норме Cummins ≤{limit / 60:.0f} мин"),
        related_roles=["LOAD_PCT", "RPM"],
        fault_codes=[],
        description_key="NO_LOAD_TOO_LONG.default",
        values={"noload_run_sec": run, "limit_sec": limit, "load_pct": pct},
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
            severity=cfg.det("START_FAILURE", "severity_default", default="CAUTION"),
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
                severity=cfg.det("START_FAILURE", "severity_default", default="CAUTION"),
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
        severity=cfg.det("THERMAL_HIGHLOAD", "severity_default", default="CAUTION"),
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
        severity=cfg.det("LIMIT_PROXIMITY", "severity_default", default="CAUTION"),
        t_detected=_iso(seg_start),
        source="PASSPORT_THRESHOLD",
        trigger=f"{label}: {val:.2f} в пределах {prox_pct:.0f}% от порога {limit:.1f}",
        related_roles=[role],
        fault_codes=fcodes,
        description_key=f"LIMIT_PROXIMITY.{role.lower()}_near_limit",
        values={"value": val, "shutdown_limit": limit, "proximity_pct": prox_pct},
    )


# ── CONTROLLER_FAULT ─────────────────────────────────────────────────────────

def _last_nonzero(periods: list[dict] | None, a: datetime, b: datetime) -> tuple[int, datetime | None]:
    """Последний ненулевой код периодов состояния в [a, b) и момент его конца.

    Не значение ровно на снятии: сброс гасит 40012 то через 4 с после бита, то раньше.
    """
    best, at = 0, None
    for p in periods or []:
        s = _tz(p["state_start"])
        e = _tz(p["state_end"]) if p.get("state_end") else b
        if s >= b or e <= a:
            continue
        try:
            v = int(p.get("value") or 0)
        except (TypeError, ValueError):
            continue
        end = min(e, b)
        if v > 0 and (at is None or end >= at):
            best, at = v, end
    return best, at


def _detect_controller_faults(
    fault_periods: list[dict[str, Any]],
    characteristics: dict[str, Any],
    seg_start: datetime,
    seg_end: datetime,
    cfg: AnalyticsConfig,
    state_periods: dict[int, list[dict]] | None = None,
) -> list[Detection]:
    results: list[Detection] = []
    t_start = _tz(seg_start)
    t_end = _tz(seg_end)

    # CommonAlarm (Общая авария) — индикаторный INFO-бит «есть ошибка».
    # Запоминаем его период отдельно: в связке с несброшенным кодом 40012
    # при молчащих остальных активных битах он даёт CAUTION (см. ниже).
    ca_addr = cfg.det("CONTROLLER_FAULT", "common_alarm_addr", default=None)
    ca_bit = cfg.det("CONTROLLER_FAULT", "common_alarm_bit", default=None)
    common_alarm_fps: list[dict[str, Any]] = []
    loud: list[tuple[datetime, datetime]] = []   # периоды не-INFO битов

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
            if ca_addr is not None and fp.get("addr") == ca_addr and fp.get("bit") == ca_bit:
                common_alarm_fps.append(fp)
            continue  # информационные биты (ReadyToLoad, NotInAuto и т.п.) не детектируем
        loud.append((max(fs, t_start), min(fe, t_end)))

        name = fp.get("fault_name_ru") or fp.get("fault_name") or f"addr={fp['addr']} bit={fp['bit']}"
        addr = fp.get("addr", 0)
        bit = fp.get("bit", 0)

        results.append(Detection(
            scenario="CONTROLLER_FAULT",
            severity=severity,
            t_detected=_iso(fs),
            source="CONTROLLER_FAULT",
            trigger=f"{name} (addr={addr}, bit={bit})",
            related_roles=[f"FAULT_MASK_{addr - 40400}" if 40400 <= addr <= 40428 else str(addr)],
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

    # Несброшенная неисправность: CommonAlarm активен, остальные не-INFO биты в
    # это время молчат, код 40012 не сброшен (сбрасывается только кнопкой после
    # устранения) → промежуточное состояние между НОРМОЙ и WARNING → CAUTION.
    # v5.1.6: каждый период аварии в подсегменте отдельно (раньше — только
    # последний), код — последний ненулевой за период по периодам 40012 (раньше —
    # value_end подсегмента: авария, сброшенная внутри подсегмента, пропадала)
    lfc_p = (state_periods or {}).get(cfg.role_to_addr("LAST_FAULT_CODE"))
    lft_p = (state_periods or {}).get(cfg.role_to_addr("LAST_FAULT_TYPE"))
    for ca in common_alarm_fps:
        fs = _tz(ca["fault_start"])
        fe_raw = ca.get("fault_end")
        a_ = max(fs, t_start)
        b_ = min(_tz(fe_raw) if fe_raw else t_end, t_end)
        if b_ <= a_ or any(s < b_ and e > a_ for s, e in loud):
            continue      # панель в это время говорит громче — CAUTION не нужен
        if lfc_p is not None:
            code, at = _last_nonzero(lfc_p, a_, b_)
            ftype = (_last_nonzero(lft_p, a_, at)[0] or None) if at and lft_p is not None else None
        else:             # без периодов (старые вызовы) — как раньше, по концу подсегмента
            lfc = characteristics.get("LAST_FAULT_CODE") or {}
            try:
                code = int(lfc.get("value_end") or 0)
            except (TypeError, ValueError):
                code = 0
            lft = characteristics.get("LAST_FAULT_TYPE") or {}
            try:
                ftype = int(lft["value_end"]) if lft.get("value_end") is not None else None
            except (TypeError, ValueError):
                ftype = None
        if code <= 0:
            continue
        results.append(Detection(
            scenario="CONTROLLER_FAULT",
            severity=cfg.det("CONTROLLER_FAULT", "unconfirmed_severity", default="CAUTION"),
            t_detected=_iso(fs),
            source="CONTROLLER_FAULT",
            trigger=f"Общая авария (CommonAlarm) при несброшенном коде неисправности {code}",
            related_roles=[f"FAULT_MASK_{ca_addr - 40400}", "LAST_FAULT_CODE"],
            fault_codes=[code],
            description_key="CONTROLLER_FAULT.common_alarm_unconfirmed",
            values={
                "addr": ca_addr,
                "bit": ca_bit,
                "fault_name": "CommonAlarm (Общая авария)",
                "last_fault_code": code,
                "last_fault_type": ftype,
                "fault_start": _iso(fs),
                "fault_end": _iso(_tz(fe_raw)) if fe_raw else None,
            },
        ))

    return results


# ── RPM_UNDERSPEED ────────────────────────────────────────────────────────────

def _detect_rpm_underspeed(
    chars: dict[str, Any],
    derived: DerivedMetrics,
    seg_start: datetime,
    cfg: AnalyticsConfig,
) -> list[Detection]:
    """Детектор просадки оборотов под нагрузкой (Addendum v1.6).

    Ловит мгновенные провалы RPM ниже порога в режиме RUN_STATE=3.
    Панель (код 1448) реагирует только при удержании ниже 1350 ~10 сек —
    блок перекрывает эту «дыру», фиксируя разовые провалы до того, как
    панель сработает.

    Gate: только RUN_STATE=3 (проверяется в run_all_detectors).
    Задержки нет (hold_sec=0): даже один срез ниже порога = детекция.
    Условие: min(RPM) за сегмент < threshold_rpm.
    """
    rpm_char = chars.get("RPM")
    if not rpm_char:
        return []
    rpm_min = rpm_char.get("min")
    if rpm_min is None:
        return []

    threshold = float(cfg.det("RPM_UNDERSPEED", "threshold_rpm", default=1485.0))
    if rpm_min >= threshold:
        return []

    panel_setpoint = float(cfg.det("RPM_UNDERSPEED", "panel_setpoint_rpm", default=1350.0))
    rated = float(cfg.det("RPM_UNDERSPEED", "rated_rpm", default=1500.0))
    fault_code_raw = cfg.det("RPM_UNDERSPEED", "fault_code", default=1448)
    fault_code = int(fault_code_raw) if fault_code_raw is not None else 1448

    rpm_max = rpm_char.get("max")
    # «Просадка 37» читалась как «на 37 ниже порога», а это размах от максимума:
    # ниже порога 1485 − 1473 = 12 (v5.1.6)
    below = threshold - rpm_min
    spread = (rpm_max - rpm_min) if rpm_max is not None else None
    drop_rpm, drop_pct = spread, (spread / rated * 100) if spread is not None else None  # устар., = размах
    front_count = derived.rpm_underspeed_fronts_count if derived.rpm_underspeed_fronts_count else 1

    return [Detection(
        scenario="RPM_UNDERSPEED",
        severity=cfg.det("RPM_UNDERSPEED", "severity_default", default="CAUTION"),
        t_detected=_iso(seg_start),
        source="METRIC_RULE",
        trigger=(
            f"RPM мин. {rpm_min:.0f} об/мин — на {below:.0f} ниже порога {threshold:.0f}"
            + (f" (размах {spread:.0f} об/мин от макс. {rpm_max:.0f})" if spread is not None else "")
            + f", фронтов: {front_count}"
        ),
        related_roles=["RPM", "ACTIVE_POWER_TOTAL"],
        fault_codes=[fault_code],
        description_key="RPM_UNDERSPEED.below_threshold",
        values={
            "rpm_min": rpm_min,
            "rpm_max": rpm_max,
            "rpm_med": rpm_char.get("median"),
            "threshold_rpm": threshold,
            "panel_setpoint_rpm": panel_setpoint,
            "rated_rpm": rated,
            "below_threshold_rpm": round(below, 1),
            "spread_rpm": spread,
            "drop_rpm": drop_rpm,
            "drop_pct": round(drop_pct, 1) if drop_pct is not None else None,
            "front_count": front_count,
        },
    )]
