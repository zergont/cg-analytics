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

import logging
import math
from datetime import datetime, timezone
from typing import Any

from .contract import Detection, DerivedMetrics, RiskAccumulators
from .config import AnalyticsConfig
from .labels import fmt_number
from .live import (
    LIVE_RECENT_SEC, cooling_proximity, limit_checks, live_values, sub_hits, sub_samples,
)
from .rpm_freq import overspeed_params

logger = logging.getLogger(__name__)


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
    onset_faults: list[dict[str, Any]] | None = None,
    live_spans: dict[str, list[dict] | None] | None = None,
) -> list[Detection]:
    """Запустить все включённые детекторы, вернуть список сработавших Detection.

    onset_faults — маски всего сегмента: по ним проверяется, что код панели
    принадлежит биту (v5.1.16), — в подсегменте соседний бит мог уже сняться.

    live_spans — участки нарушений сегмента (analytics/live.py, v5.1.21, П3):
    None — как раньше, по min/max подсегмента (тесты с ручными DerivedMetrics,
    откат при сбое расчёта участков); словарь — RPM_UNDERSPEED, LIMIT_PROXIMITY
    (по роли), близость COOLING_FAILURE и паспорт OIL_DILUTION срабатывают,
    только если участок их ключа пересекает [seg_start, seg_end). Ключа нет —
    детекции нет: срабатывание по строке преамбулы или копии forward-fill ушло.
    Ключ со значением None — сбой расчёта его участков (v5.1.23, п.7): этот
    детектор идёт по подсегменту, как при live_spans=None, соседи — по участкам.
    RPM_OVERSPEED (v5.1.23, п.2) — только по участкам: без них (None, сбой
    ключа) детекции нет.
    t_detected — начало первого пересекающего кластера (одно во всех
    подсегментах кластера), values.live — кластеры, onset, first.
    """
    detections: list[Detection] = []

    def _lv(key: str) -> list[dict] | None:
        if live_spans is None:
            return None
        if key in live_spans and live_spans[key] is None:
            return None
        return live_spans.get(key) or []

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
        detections.extend(_detect_cooling_failure(characteristics, derived, run_state, seg_start, seg_end, cfg,
                                                  live=_lv("COOLING_FAILURE")))

    # Масляные детекторы валидны ТОЛЬКО при работающем двигателе (RUN_STATE=3).
    # На остановленном/останавливающемся моторе давление масла = 0 — это норма.
    _oil_valid = set(cfg.det("OIL_DILUTION", "valid_run_states", default=[3]) or [3])
    if cfg.det("OIL_DILUTION", "enabled", default=True) and run_state in _oil_valid:
        detections.extend(_detect_oil_dilution(characteristics, derived, seg_start, cfg, seg_end,
                                               live=_lv("OIL_DILUTION")))

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
        detections.extend(_detect_limit_proximity(characteristics, seg_start, cfg,
                                                  live=_lv("LIMIT_PROXIMITY"), seg_end=seg_end))

    if cfg.det("CONTROLLER_FAULT", "enabled", default=True):
        detections.extend(_detect_controller_faults(
            fault_periods_in_seg, characteristics, seg_start, seg_end, cfg, state_periods,
            onset_faults=onset_faults,
        ))

    if cfg.det("RPM_UNDERSPEED", "enabled", default=True) and run_state == 3:
        detections.extend(_detect_rpm_underspeed(characteristics, derived, seg_start, cfg,
                                                 live=_lv("RPM_UNDERSPEED"), seg_end=seg_end))

    # Заброс оборотов (v5.1.23, п.2) — в своём try: новый детектор не роняет
    # подсегмент (сегментатор перезапустил бы все детекторы без участков)
    if cfg.det("RPM_OVERSPEED", "enabled", default=True) and run_state == 3:
        try:
            detections.extend(_detect_rpm_overspeed(seg_start, cfg, live=_lv("RPM_OVERSPEED"),
                                                    seg_end=seg_end))
        except Exception:
            logger.warning("Заброс оборотов [%s, %s) не оценён", _iso(seg_start), _iso(seg_end),
                           exc_info=True)

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
    live: list[dict] | None = None,
) -> list[Detection]:
    """Детектор отказа охлаждения (v5.1.15, решение владельца 13.10, вариант Г).

    live — кластеры COOLING_FAILURE сегмента (v5.1.21): близость — только при
    участке, пересекающем подсегмент, максимум — по его отсчётам; None — по
    максимуму подсегмента, как раньше. Прогноз прогрева — без участков.

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
    _cp = cooling_proximity(cfg)
    hct_warn, prox_pct, prox_thr = _cp["hct_warning_c"], _cp["pct"], _cp["near"]
    t_det, live_v = _iso(seg_start), None
    if live is not None:
        # По участкам (v5.1.21): максимум — по отсчётам участков подсегмента
        hits = sub_hits(live, seg_start, seg_end)
        pts = sub_samples([s for _, sp in hits for s in sp], seg_start, seg_end)
        cool_max = max(p[1] for p in pts) if pts else None
        if cool_max is not None:
            t_det, live_v = hits[0][0]["start"], live_values(hits, seg_start)
    if cool_max is not None and cool_max >= prox_thr:
        values = {"coolant_temp_max": cool_max, "hct_warning_c": hct_warn,
                  "proximity_threshold_c": prox_thr}
        if live_v is not None:
            values["live"] = live_v
        results.append(Detection(
            scenario="COOLING_FAILURE",
            severity="CAUTION",
            t_detected=t_det,
            source="PASSPORT_THRESHOLD",
            trigger=(f"ОЖ дошла до {cool_max:.0f} °C — не ниже {prox_thr:.1f} °C "
                     f"({prox_pct:.0f} % до уставки предупреждения панели {hct_warn:g} °C)"),
            related_roles=["COOLANT_TEMP"],
            fault_codes=[144],
            description_key="COOLING_FAILURE.approaching_hct_limit",
            values=values,
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
# стольких секунд назад (online.engine._extract_open_segment_data), v5.1.12.
# С v5.1.21 — псевдоним общего окна свежести участков (analytics/live.py, В2)
PASSPORT_RECENT_SEC = LIVE_RECENT_SEC


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


def _passport_from_live(live: list[dict], seg_start: datetime,
                        seg_end: datetime | None) -> dict[str, Any] | None:
    """Поля паспорта из кластеров, пересекающих подсегмент (v5.1.21): значение,
    run_sec, until/ongoing, hard/hard_until/last_value. None — участка нет или
    нужного поля нет (детекции нет — без падения на форматировании)."""
    if seg_end is None:
        return None
    hits = sub_hits(live, seg_start, seg_end)
    if not hits:
        return None
    cls = [c for c, _ in hits]
    last = cls[-1]
    hard_cls = [c for c in cls if c.get("hard") and c.get("hard_min") is not None]
    mins = [c["vmin"] for c in cls if c.get("vmin") is not None]
    if not mins:
        return None
    soft_min = min(mins)
    brief = None
    if hard_cls:
        value = min(c["hard_min"] for c in hard_cls)
        lh = max(hard_cls, key=lambda c: datetime.fromisoformat(c["hard_until"]))
        hard_until, hard_ongoing = lh["hard_until"], bool(lh.get("hard_ongoing"))
    else:
        inst = [c["instep_min"] for c in cls if c.get("instep_min") is not None]
        value = min(inst) if inst else soft_min
        if soft_min < value:
            brief = soft_min
        hard_until, hard_ongoing = None, None
    return {
        "value": value, "brief": brief, "hard": bool(hard_cls),
        "run": max(float(c.get("run_sec") or 0.0) for c in cls),
        "until": last["end"], "ongoing": bool(last["ongoing"]),
        "last_value": last.get("last_value"),
        "hard_until": hard_until, "hard_ongoing": hard_ongoing,
        "t_detected": cls[0]["start"], "live": live_values(hits, seg_start),
    }


def _passport_detection(derived: DerivedMetrics, seg_start: datetime,
                        cfg: AnalyticsConfig, live: list[dict] | None = None,
                        seg_end: datetime | None = None) -> Detection | None:
    """Давление масла ниже паспорта своего двигателя (v5.1.12, решения 12.10).

    Ниже паспорта устойчиво (≥ выдержки) — CAUTION «ниже паспорта». Ниже больше
    чем на шаг датчика — «нарушение паспорта»: гейт его не снимает (код), в
    пределах шага — снимаемо, но нормой не называется. limit — для гейта,
    статуса и заключения; oil_press_min/passport_min_kpa — для сводки отчёта.

    live — кластеры OIL_DILUTION сегмента (v5.1.21): значение, run_sec, until,
    ongoing, hard, hard_until — из кластера, из DerivedMetrics — только шаг
    датчика и паспортный предел; начало эпизода — начало участка (В7).
    """
    min_run = float(cfg.det("OIL_DILUTION", "passport_min_run_sec", default=60))
    limit = derived.oil_press_passport_kpa
    step = derived.oil_press_step_kpa or 0.0
    t_det, extra = _iso(seg_start), {}
    if live is not None:
        lv = _passport_from_live(live, seg_start, seg_end)
        if limit is None or lv is None or lv["value"] is None:
            return None
        value, brief, hard, run = lv["value"], lv["brief"], lv["hard"], lv["run"]
        until, ongoing, last_value = lv["until"], lv["ongoing"], lv["last_value"]
        hard_until, hard_ongoing = lv["hard_until"], lv["hard_ongoing"]
        t_det, extra = lv["t_detected"], {"live": lv["live"]}
    else:
        run = derived.oil_press_below_passport_run_sec or 0.0
        if limit is None or run < min_run or derived.oil_press_below_passport_min_kpa is None:
            return None
        hard = (derived.oil_press_hard_run_sec or 0.0) >= min_run
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
        until, ongoing = derived.oil_press_passport_until, derived.oil_press_passport_ongoing
        last_value = derived.oil_press_last_min_kpa
        hard_until, hard_ongoing = derived.oil_press_hard_until, derived.oil_press_hard_ongoing
    limit_d = {
        "kind": "passport", "param": "OIL_PRESS", "direction": "min",
        "limit": limit, "unit": "кПа", "value": value,
        "run_sec": run, "min_run_sec": min_run,
        "step": step, "step_source": derived.oil_press_step_source,
        "hard": hard, "engine": cfg.engine_label, "brief_min": brief,
        "until": until,
        "ongoing": ongoing,
        # Свежесть «нарушения» отдельно: в живом составе давнее нарушение при
        # свежем «в пределах шага» не держит запрет снятия (ревью v5.1.12)
        "last_value": last_value,
        "hard_until": hard_until,
        "hard_ongoing": hard_ongoing,
    }
    return Detection(
        scenario="OIL_DILUTION",
        severity="CAUTION",
        t_detected=t_det,
        source="PASSPORT_THRESHOLD",
        trigger=passport_trigger(limit_d),
        related_roles=["OIL_PRESS"],
        fault_codes=[143, 415],
        description_key="OIL_DILUTION.below_passport_min",
        values={"oil_press_min": value, "passport_min_kpa": limit, "limit": limit_d, **extra},
    )


def _detect_oil_dilution(
    chars: dict[str, Any],
    derived: DerivedMetrics,
    seg_start: datetime,
    cfg: AnalyticsConfig,
    seg_end: datetime | None = None,
    live: list[dict] | None = None,
) -> list[Detection]:
    # Паспорт — первым и без температуры масла: раньше ветка паспорта шла после
    # комбинированной и пропадала без OIL_TEMP (v5.1.12). Паспорт главнее всегда:
    # комбинированная ветка считает по всему подсегменту и сработала бы на том же
    # провале — нарушение паспорта пропало бы из записи подсегмента (ревью).
    # live — кластеры паспорта сегмента (v5.1.21); комбинированная ветка — без них
    passport = _passport_detection(derived, seg_start, cfg, live=live, seg_end=seg_end)
    if passport is not None:
        return [passport]
    combined = _combined_oil_detection(chars, seg_start, cfg)
    return [combined] if combined else []


def _combined_oil_detection(
    chars: dict[str, Any], seg_start: datetime, cfg: AnalyticsConfig,
) -> Detection | None:
    """Падение давления вместе с ростом температуры масла — признак разжижения.

    Пол давления (v5.1.21, В3): минимум подсегмента ниже уставки останова на
    рабочих оборотах без останова панели — не замер (нулевой кадр), ветка в
    этом подсегменте не оценивается. ДЭС №3 13.10 14:59:56: давление 300 → 0 →
    316 дало строку «разжижения» на 22 мин 37 с (14902). Ветка — без участков
    (по событию — v5.1.22).

    Выключатель combined_enabled (v5.1.23, решение В6 = А): в KB false — все
    срабатывания за 60 дней были прогревом после холодного пуска или нулевым
    кадром; паспортная ветка работает. Ключа нет — ветка включена, как раньше."""
    if not cfg.det("OIL_DILUTION", "combined_enabled", default=True):
        return None
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
    _floor = cfg.thr("oil_pressure", "controller", "shutdown_rated_kpa")
    if _floor is not None and press_min < float(_floor):
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
    live: list[dict] | None = None,
    seg_end: datetime | None = None,
) -> list[Detection]:
    """Близость к уставкам останова панели — одна детекция на роль.

    live — кластеры LIMIT_PROXIMITY сегмента (v5.1.21): три роли в одном наборе
    кластеров, детекция роли — только при участке этой роли в подсегменте,
    значение — по отсчётам участков; пол давления (В3) уже в участках. None —
    по min/max подсегмента, как раньше. Пороги — live.limit_checks."""
    results: list[Detection] = []
    for chk in limit_checks(cfg):
        role, limit = chk["role"], chk["limit"]
        if live is not None:
            hits = sub_hits(live, seg_start, seg_end, role=role) if seg_end is not None else []
            pts = sub_samples([s for _, sp in hits for s in sp], seg_start, seg_end) if hits else []
            if not pts:
                continue
            vals = [p[1] for p in pts]
            val = min(vals) if chk["below"] else max(vals)
            results.append(_limit_detection(role, val, limit, chk["label"], chk["codes"], seg_start, cfg,
                                            hits=hits))
            continue
        char = chars.get(role)
        if not char:
            continue
        val = char.get(chk["stat"])
        if val is None:
            continue
        # Плохо: давление — val < limit·(1 + pct), температуры — val > limit·(1 − pct)
        if (val < chk["near"]) if chk["below"] else (val > chk["near"]):
            results.append(_limit_detection(role, val, limit, chk["label"], chk["codes"], seg_start, cfg))

    return results


def _limit_detection(role, val, limit, label, fcodes, seg_start, cfg, hits=None) -> Detection:
    prox_pct = float(cfg.det("LIMIT_PROXIMITY", "proximity_pct", default=10.0))
    values = {"value": val, "shutdown_limit": limit, "proximity_pct": prox_pct}
    t_det = _iso(seg_start)
    if hits:
        # v5.1.21: начало — первый кластер (общий для трёх ролей ключа), роль —
        # задел на ключ по параметру (v5.1.22)
        t_det = hits[0][0]["start"]
        values.update(role=role, live=live_values(hits, seg_start))
    return Detection(
        scenario="LIMIT_PROXIMITY",
        severity=cfg.det("LIMIT_PROXIMITY", "severity_default", default="CAUTION"),
        t_detected=t_det,
        source="PASSPORT_THRESHOLD",
        trigger=f"{label}: {val:.2f} в пределах {prox_pct:.0f}% от порога {limit:.1f}",
        related_roles=[role],
        fault_codes=fcodes,
        description_key=f"LIMIT_PROXIMITY.{role.lower()}_near_limit",
        values=values,
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
    onset_faults: list[dict[str, Any]] | None = None,
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
                # Имя бита из каталога (v5.1.23, решение В5): с привязанным
                # кодом имя замечания — по коду (labels.panel_fault_name)
                "bit_name": name,
                "fault_start": _iso(fs),
                "fault_end": _iso(fe) if fe_raw else None,
                "duration_sec": fp.get("duration_sec"),
                "raw_severity": raw_sev,
            },
        ))

    # Код панели к биту (v5.1.16). 40012 «последний код» встаёт в том же кадре,
    # что и бит: без привязки строка статуса и гейт получали «Предупреждение
    # двигателя (addr=40406, bit=2)» и советовали «расшифровать бит», хотя код
    # 1248 и его описание есть в справочнике (ДЭС №3 13.10). Привязываем, только
    # если в этот момент поднялся ровно один громкий бит — иначе код не его.
    # Соседей считаем по маскам всего окна: в подсегменте сосед мог уже
    # сняться (ревью v5.1.16). Сосед, снятый до начала окна (живое окно после
    # снятия аварии), не виден — поэтому ещё и тяжесть кода из справочника
    # должна подходить биту: предупреждение не получит код аварии 1438.
    _lfc_all = (state_periods or {}).get(cfg.role_to_addr("LAST_FAULT_CODE")) or []
    if _lfc_all and results:
        _tol = float(cfg.det("CONTROLLER_FAULT", "code_onset_tol_sec", default=15.0))
        _loud = [_tz(fp["fault_start"]) for fp in (onset_faults if onset_faults is not None
                                                     else fault_periods)
                 if cfg.bitmap_severity(fp.get("severity") or "none") != "INFO"]
        for d in results:
            fs = _tz(datetime.fromisoformat(d.values["fault_start"]))
            if sum(1 for o in _loud if abs((o - fs).total_seconds()) <= _tol) != 1:
                continue
            best = None
            for p in _lfc_all:
                try:
                    v = int(p.get("value") or 0)
                except (TypeError, ValueError):
                    continue
                gap = abs((_tz(p["state_start"]) - fs).total_seconds())
                if v > 0 and gap <= _tol and (best is None or gap < best[0]):
                    best = (gap, v)
            if best is not None and cfg.code_fits_bit(best[1], d.values.get("raw_severity")):
                d.fault_codes = [best[1]]
                d.values["panel_code"] = best[1]
                # Имя кода из справочника KB (v5.1.23, решение В5): «Разнос
                # двигателя — превышение частоты вращения (код 234)» вместо
                # имени бита каталога «Скорость/положение КВ: выше нормы»
                try:
                    _cn = cfg.fault_code_name(best[1])
                except Exception:
                    _cn = None
                if _cn:
                    d.values["code_name"] = _cn

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
    live: list[dict] | None = None,
    seg_end: datetime | None = None,
) -> list[Detection]:
    """Детектор просадки оборотов под нагрузкой (Addendum v1.6).

    Ловит мгновенные провалы RPM ниже порога в режиме RUN_STATE=3.
    Панель (код 1448) реагирует только при удержании ниже 1350 ~10 сек —
    блок перекрывает эту «дыру», фиксируя разовые провалы до того, как
    панель сработает.

    Gate: только RUN_STATE=3 (проверяется в run_all_detectors).
    Задержки нет (hold_sec=0): даже один срез ниже порога = детекция.
    Условие: min(RPM) за сегмент < threshold_rpm.

    live — кластеры RPM_UNDERSPEED сегмента (v5.1.21, В1): провал — по v*,
    обороты и частота опроса ниже порога (analytics/rpm_freq.py). Детекция —
    только при участке провала в подсегменте; rpm_min — по отсчётам провала,
    freq_rpm_min — обороты по частоте генератора (k·f_min), front_count —
    число участков в подсегменте. None — по минимуму подсегмента, как раньше.
    """
    rpm_char = chars.get("RPM") or {}
    threshold = float(cfg.det("RPM_UNDERSPEED", "threshold_rpm", default=1485.0))
    freq_rpm_min, live_v, t_det = None, None, _iso(seg_start)
    if live is not None:
        hits = sub_hits(live, seg_start, seg_end) if seg_end is not None else []
        spans = [s for _, sp in hits for s in sp]
        pts = sub_samples(spans, seg_start, seg_end) if spans else []
        if not pts:
            return []
        rpm_min = min(p[1] for p in pts)
        kfs = [p[2] for p in pts if p[2] is not None]
        freq_rpm_min = round(min(kfs), 1) if kfs else None
        front_live: int | None = len(spans)
        t_det, live_v = hits[0][0]["start"], live_values(hits, seg_start)
    else:
        if not rpm_char:
            return []
        rpm_min = rpm_char.get("min")
        if rpm_min is None:
            return []
        if rpm_min >= threshold:
            return []
        front_live = None

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
    if front_live is not None:
        front_count = front_live
    else:
        front_count = derived.rpm_underspeed_fronts_count if derived.rpm_underspeed_fronts_count else 1

    values = {
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
    }
    if live_v is not None:
        values.update(freq_rpm_min=freq_rpm_min, live=live_v)
    return [Detection(
        scenario="RPM_UNDERSPEED",
        severity=cfg.det("RPM_UNDERSPEED", "severity_default", default="CAUTION"),
        t_detected=t_det,
        source="METRIC_RULE",
        trigger=(
            f"RPM мин. {rpm_min:.0f} об/мин"
            + (f" (по частоте ≈ {freq_rpm_min:.0f})" if freq_rpm_min is not None else "")
            + f" — на {below:.0f} ниже порога {threshold:.0f}"
            + (f" (размах {spread:.0f} об/мин от макс. {rpm_max:.0f})" if spread is not None else "")
            + f", фронтов: {front_count}"
        ),
        related_roles=["RPM", "ACTIVE_POWER_TOTAL"],
        fault_codes=[fault_code],
        description_key="RPM_UNDERSPEED.below_threshold",
        values=values,
    )]


# ── RPM_OVERSPEED ─────────────────────────────────────────────────────────────

def _detect_rpm_overspeed(
    seg_start: datetime,
    cfg: AnalyticsConfig,
    live: list[dict] | None = None,
    seg_end: datetime | None = None,
) -> list[Detection]:
    """Заброс оборотов (v5.1.23, п.2, решения владельца В2′, В3 17.10.2026).

    Панель глушит разнос на уставке overspeed_rpm (код 234), аналитика
    предупреждает раньше: порог 1575 об/мин = уставка 1725 − 150 (91,3 %
    уставки, В2′; прежние 1552,5 «на 10 % строже» давали у ДЭС №3 десятки
    одиночных эпизодов на скачках нагрузки), строго «выше». ДГУ №1
    16.10 09:26:03 — 1696 об/мин при 56,8 Гц на пуске с заданием 30,6 → 55 Гц,
    через минуты — разнос 234; ДГУ №2 15.10 12:42:19 — 1606 при 53,6 Гц, гейт
    тогда снял состав как переходный процесс.

    Только по участкам (live — кластеры RPM_OVERSPEED сегмента, analytics/live.py):
    отсчёт в заборе, если выше порога и обороты, и k·f_max опроса
    (rpm_freq.over_marks). None — участков нет (сбой ключа, расчёт без
    участков) — детекции нет: по максимуму подсегмента заброс ловился бы по
    строке преамбулы. Код 234 справочный — в fault_codes остаётся, только если
    панель его выставила (confirm_panel_codes). Gate: RUN_STATE=3 (в
    run_all_detectors). Вне пуска (сброс нагрузки) гейт снимать вправе (В3)."""
    if live is None or seg_end is None:
        return []
    prm = overspeed_params(cfg)
    if prm is None:
        return []
    hits = sub_hits(live, seg_start, seg_end)
    spans = [s for _, sp in hits for s in sp]
    pts = sub_samples(spans, seg_start, seg_end) if spans else []
    if not pts:
        return []
    rpm_max = max(p[1] for p in pts)
    kfs = [p[2] for p in pts if p[2] is not None]
    freq_rpm_max = round(max(kfs), 1) if kfs else None
    hz_max = round(freq_rpm_max / prm["k"], 2) if freq_rpm_max is not None and prm["k"] else None
    thr, ovs, pct = prm["threshold_rpm"], prm["overspeed_rpm"], prm["proximity_pct"]
    margin = ovs - rpm_max
    values = {
        "rpm_max": rpm_max,
        "freq_rpm_max": freq_rpm_max,
        "freq_hz_max": hz_max,
        "threshold_rpm": round(thr, 1),
        "overspeed_rpm": ovs,
        "proximity_pct": pct,
        "margin_rpm": round(margin, 1),
        "front_count": len(spans),
        "live": live_values(hits, seg_start),
    }
    return [Detection(
        scenario="RPM_OVERSPEED",
        severity=cfg.det("RPM_OVERSPEED", "severity_default", default="CAUTION"),
        t_detected=hits[0][0]["start"],
        source="METRIC_RULE",
        trigger=(
            f"Обороты макс. {rpm_max:.0f} об/мин"
            + (f" ({fmt_number(hz_max, 1)} Гц, по частоте ≈ {freq_rpm_max:.0f})" if hz_max is not None else "")
            + f" — выше порога {fmt_number(thr, 1)} ({fmt_number(100 - pct, 1)} % уставки разноса {ovs:.0f}), "
            + (f"до уставки осталось {margin:.0f}" if margin > 0 else f"выше уставки на {-margin:.0f}")
            + f", фронтов: {len(spans)}"
        ),
        related_roles=["RPM", "FREQUENCY"],
        fault_codes=[prm["fault_code"]],
        description_key="RPM_OVERSPEED.above_threshold",
        values=values,
    )]
