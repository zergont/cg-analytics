# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Контракт выхода аналитического блока — dataclasses по ТЗ раздел 3.

Структура: СЕГМЕНТ → ПОДСЕГМЕНТ → CHARACTERISTIC + DERIVED_METRICS + RISK_ACCUMULATORS + DETECTION.
Все nullable baseline-поля зарезервированы (= None) для Этапа 1.5.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


# ── 3.4 CHARACTERISTIC ──────────────────────────────────────────────────────

@dataclass
class Characteristic:
    """Характеристика непрерывной роли в подсегменте."""
    role: str
    unit: str
    sample_count: int
    median: Optional[float]
    mad: Optional[float]
    min: Optional[float]
    max: Optional[float]
    value_start: Optional[float]
    value_end: Optional[float]
    slope: Optional[float]              # ед./с, по реальным device-timestamp (Addendum v1.4)
    baseline_resid: None = None         # Этап 1.5 — отклонение от модели нормы
    baseline_z: None = None             # Этап 1.5 — нормированное отклонение

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "unit": self.unit,
            "sample_count": self.sample_count,
            "median": self.median,
            "mad": self.mad,
            "min": self.min,
            "max": self.max,
            "value_start": self.value_start,
            "value_end": self.value_end,
            "slope": self.slope,
            "baseline_resid": self.baseline_resid,
            "baseline_z": self.baseline_z,
        }


@dataclass
class CharacteristicDiscrete:
    """Характеристика дискретной роли (RUN_STATE, SWITCH_POS)."""
    role: str
    values_seen: list[int]
    transition_count: int
    final_value: Optional[int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "values_seen": self.values_seen,
            "transition_count": self.transition_count,
            "final_value": self.final_value,
        }


# ── 3.5 DERIVED_METRICS ─────────────────────────────────────────────────────

@dataclass
class DerivedMetrics:
    """Вычисляемые метрики подсегмента (агрегаты и экстремумы)."""
    # ── Ток обратной последовательности I₂ (Negative Sequence, Метод А) ──────
    # Физически корректная метрика несимметрии. Заводская защита PCC3300 (12%).
    neg_seq_i2_pct_max: Optional[float] = None      # I₂% макс в подсегменте
    neg_seq_i2_pct_med: Optional[float] = None      # I₂% медиана
    neg_seq_i2_duration_sec: Optional[float] = None  # время I₂% > порога приближения
    # ── Перекос фазных токов и ток нейтрали (v4.9.97) ────────────────────────
    # Обе — в % номинального тока генератора, по реальным замерам (без ff).
    # *_run_sec — самый длинный НЕПРЕРЫВНЫЙ участок выше порога детектора:
    # одиночные всплески в норме бывают, длительных — нет.
    phase_spread_pct_max: Optional[float] = None     # (I_max − I_min) / I_ном, %
    phase_spread_pct_med: Optional[float] = None
    phase_spread_run_sec: Optional[float] = None
    neutral_current_pct_max: Optional[float] = None  # |Σ I_k| / I_ном, % (углы по P, Q)
    neutral_current_pct_med: Optional[float] = None
    neutral_current_run_sec: Optional[float] = None
    # ── Справочные метрики несимметрии (НЕ основа детектора) ────────────────
    current_imbalance_pct_max: Optional[float] = None   # грубый перекос токов (справочно)
    current_imbalance_pct_med: Optional[float] = None
    power_imbalance_pct_max: Optional[float] = None
    imbalance_duration_sec: Optional[float] = None
    s_consistency_max: Optional[float] = None
    pf_consistency_max: Optional[float] = None
    oil_coolant_delta_med: Optional[float] = None
    oil_coolant_delta_max: Optional[float] = None
    rpm_stability_mad: Optional[float] = None
    rpm_underspeed_fronts_count: Optional[int] = None  # число фронтов (переходов RPM ниже порога)
    freq_stability_mad: Optional[float] = None
    # ── Класс A (быстрые/электрические, RUN_STATE=3) ──
    dP_dt_max: Optional[float] = None          # кВт/с   — скорость изменения мощности
    dRPM_dt_max: Optional[float] = None        # об/мин/с — быстрый отклик оборотов
    # Переходный процесс по ГОСТ ISO 8528-5 (вычисляются при наличии LOAD_STEP)
    freq_dip_pct: Optional[float] = None       # просадка частоты при набросе, % от 50 Гц
    freq_rise_pct: Optional[float] = None      # заброс частоты при сбросе, % от 50 Гц
    freq_recovery_sec: Optional[float] = None  # время возврата частоты в коридор ±settled%, сек
    # ── Класс B (медленные/инерционные) — СПРАВОЧНЫЕ (детекторы используют slope из chars) ──
    dCoolant_dt_max: Optional[float] = None    # °C/с    — Class B, для LLM Stage 2
    dOil_press_dt_min: Optional[float] = None  # кПа/с   — Class B, для LLM Stage 2
    coolant_below_60_sec: Optional[float] = None
    coolant_asymptote_c: Optional[float] = None  # °C — оценка асимптоты прогрева (Phase 1 predictor)

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


# ── 3.6 RISK_ACCUMULATORS ───────────────────────────────────────────────────

@dataclass
class CokingRisk:
    """Риск нагара (wet stacking) — накопленное СОСТОЯНИЕ машины, долг до прожига.

    Копится при недогрузе (малая нагрузка, холостой ход, холодная ОЖ), стоп его
    не снимает, снимает только работа под нагрузкой — пропорционально (v5.1.3,
    решения 11.10.2026; docs/plans/research_coking_wet_stacking.md). Счётчики —
    непогашенный остаток с последнего полного прожига.
    """
    idle_low_rpm_sec: float = 0.0       # холостой ход (RUN_STATE 2/5), суммарно
    coolant_below_60_sec: float = 0.0   # работа с ОЖ ниже порога
    low_load_zone_sec: float = 0.0      # работа ниже порога нагрузки (по LOAD_PCT)
    risk_level: str = "GREEN"           # GREEN / YELLOW / RED
    last_purge_ts: Optional[str] = None  # последний ПОЛНЫЙ прожиг (долг погашен)
    last_accum_ts: Optional[str] = None  # последний момент, когда долг рос
    debt_sec: float = 0.0               # долг в «секундах малой нагрузки» (с весами)
    debt_added_total_sec: float = 0.0   # монотонно: всего начислено (прирост за период = разность)
    debt_paid_total_sec: float = 0.0    # монотонно: всего погашено прожигом
    last_added_sec: float = 0.0         # прирост долга в последнем подсегменте
    purge_run_sec: float = 0.0          # текущий непрерывный отрезок под нагрузкой ≥ порога
    purge_pending_sec: float = 0.0      # зачёт отрезка, ждущий минимальной длины
    idle_run_sec: float = 0.0           # текущий непрерывный холостой ход
    purge_left_low_min: Optional[float] = None   # до сброса — минут при ≥ порога
    purge_left_high_min: Optional[float] = None  # то же при высокой нагрузке
    purge_low_pct: Optional[float] = None    # пороги прожига (для подписей без cfg)
    purge_high_pct: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "idle_low_rpm_sec": self.idle_low_rpm_sec,
            "coolant_below_60_sec": self.coolant_below_60_sec,
            "low_load_zone_sec": self.low_load_zone_sec,
            "risk_level": self.risk_level,
            "last_purge_ts": self.last_purge_ts,
            "last_accum_ts": self.last_accum_ts,
            "debt_sec": self.debt_sec,
            "debt_added_total_sec": self.debt_added_total_sec,
            "debt_paid_total_sec": self.debt_paid_total_sec,
            "last_added_sec": self.last_added_sec,
            "purge_run_sec": self.purge_run_sec,
            "purge_pending_sec": self.purge_pending_sec,
            "idle_run_sec": self.idle_run_sec,
            "purge_left_low_min": self.purge_left_low_min,
            "purge_left_high_min": self.purge_left_high_min,
            "purge_low_pct": self.purge_low_pct,
            "purge_high_pct": self.purge_high_pct,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CokingRisk":
        """Из coking_risk_json; поля, которых нет в старых записях, — по умолчанию."""
        def f(k: str) -> float:
            try:
                return float(d.get(k) or 0.0)
            except (TypeError, ValueError):
                return 0.0

        def o(k: str) -> Optional[float]:
            v = d.get(k)
            try:
                return float(v) if v is not None else None
            except (TypeError, ValueError):
                return None
        return cls(
            idle_low_rpm_sec=f("idle_low_rpm_sec"),
            coolant_below_60_sec=f("coolant_below_60_sec"),
            low_load_zone_sec=f("low_load_zone_sec"),
            risk_level=d.get("risk_level") or "GREEN",
            last_purge_ts=d.get("last_purge_ts"),
            last_accum_ts=d.get("last_accum_ts"),
            debt_sec=f("debt_sec"),
            debt_added_total_sec=f("debt_added_total_sec"),
            debt_paid_total_sec=f("debt_paid_total_sec"),
            last_added_sec=f("last_added_sec"),
            purge_run_sec=f("purge_run_sec"),
            purge_pending_sec=f("purge_pending_sec"),
            idle_run_sec=f("idle_run_sec"),
            purge_left_low_min=o("purge_left_low_min"),
            purge_left_high_min=o("purge_left_high_min"),
            purge_low_pct=o("purge_low_pct"),
            purge_high_pct=o("purge_high_pct"),
        )


@dataclass
class ThermalRisk:
    elevated_zone_sec: float = 0.0
    coolant_near_limit_sec: float = 0.0
    oil_near_limit_sec: float = 0.0
    risk_level: str = "GREEN"

    def to_dict(self) -> dict[str, Any]:
        return {
            "elevated_zone_sec": self.elevated_zone_sec,
            "coolant_near_limit_sec": self.coolant_near_limit_sec,
            "oil_near_limit_sec": self.oil_near_limit_sec,
            "risk_level": self.risk_level,
        }


@dataclass
class RiskAccumulators:
    coking_risk: CokingRisk = field(default_factory=CokingRisk)
    thermal_risk: ThermalRisk = field(default_factory=ThermalRisk)

    def to_dict(self) -> dict[str, Any]:
        return {
            "coking_risk": self.coking_risk.to_dict(),
            "thermal_risk": self.thermal_risk.to_dict(),
        }

    def copy(self) -> "RiskAccumulators":
        """Сделать копию для передачи в следующий подсегмент."""
        import copy
        return copy.deepcopy(self)


# ── 3.7 DETECTION ───────────────────────────────────────────────────────────

# Сценарии-состояния: накопленное состояние машины, а не событие сегмента.
# Эпизодов тревог, разбора гейтом и журнала тревог у них нет — история в
# цепочке coking_risk_json по сегментам, в сводке своя часть (v5.1.3).
STATE_SCENARIOS = frozenset({"COKING_RISK"})

@dataclass
class Detection:
    """Сработавший диагностический сценарий."""
    scenario: str                  # LOAD_STEP / PHASE_IMBALANCE / ...
    severity: str                  # INFO / CAUTION / WARNING / SHUTDOWN
    t_detected: str                # ISO8601
    source: str                    # PASSPORT_THRESHOLD / CONTROLLER_FAULT / METRIC_RULE
    trigger: str                   # описание триггера (метрика + значение + порог)
    related_roles: list[str]
    fault_codes: list[int]
    description_key: str           # ключ для LLM/локализации, НЕ человеческий текст
    values: dict[str, Any]         # снимок значений на момент срабатывания

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "severity": self.severity,
            "t_detected": self.t_detected,
            "source": self.source,
            "trigger": self.trigger,
            "related_roles": self.related_roles,
            "fault_codes": self.fault_codes,
            "description_key": self.description_key,
            "values": self.values,
        }


# ── 3.3 SUBSEGMENT ──────────────────────────────────────────────────────────

@dataclass
class Subsegment:
    id: str
    parent_segment_id: str
    t_start: str
    t_end: Optional[str]
    duration_sec: float
    load_zone: str                          # LOW / NORMAL / ELEVATED / OVERLOAD / NA
    cause_open: str
    cause_close: Optional[str]
    characteristics: dict[str, Any]        # role → Characteristic.to_dict()
    derived_metrics: DerivedMetrics
    risk_accumulators: RiskAccumulators
    detections: list[Detection]
    data_quality: float                    # 0.0 – 1.0
    # Интервалы потери связи внутри подсегмента (обрезаны по его границам).
    # Каждый: {"start": iso, "end": iso, "duration_sec": float}. Пусто = связь ровная.
    data_gaps: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "parent_segment_id": self.parent_segment_id,
            "t_start": self.t_start,
            "t_end": self.t_end,
            "duration_sec": self.duration_sec,
            "load_zone": self.load_zone,
            "cause_open": self.cause_open,
            "cause_close": self.cause_close,
            "characteristics": self.characteristics,
            "derived_metrics": self.derived_metrics.to_dict(),
            "risk_accumulators": self.risk_accumulators.to_dict(),
            "detections": [d.to_dict() for d in self.detections],
            "data_quality": self.data_quality,
            "data_gaps": self.data_gaps,
        }


# ── 3.2 SEGMENT ─────────────────────────────────────────────────────────────

@dataclass
class Segment:
    id: str
    router_sn: str
    equip_type: str
    panel_id: int
    engine_sn: str
    run_state: int
    run_state_label: str
    t_start: str
    t_end: Optional[str]
    duration_sec: float
    engine_hours_start: Optional[float]
    cause_open: str
    cause_close: Optional[str]
    preamble_included: bool
    data_quality: float
    subsegments: list[Subsegment]
    sequence_checks: list[dict[str, Any]]
    events: list[dict[str, Any]]
    # Вид стоп-сегмента: 'EMERGENCY' — останов аварийный (в его начале панель
    # держит аварию: маска аварийной тяжести или тип последней неисправности
    # Shutdown, v4.9.94), 'SIMPLE' — обычный. None у всех остальных
    # режимов и у сегментов, нарезанных до появления модели (v4.9.67).
    stop_kind: Optional[str] = None
    # Риск нагара на начало сегмента (CokingRisk.to_dict) — для прироста и
    # погашения за сегмент в сводке (v5.1.3)
    coking_start: Optional[dict[str, Any]] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "router_sn": self.router_sn,
            "equip_type": self.equip_type,
            "panel_id": self.panel_id,
            "engine_sn": self.engine_sn,
            "run_state": self.run_state,
            "run_state_label": self.run_state_label,
            "t_start": self.t_start,
            "t_end": self.t_end,
            "duration_sec": self.duration_sec,
            "engine_hours_start": self.engine_hours_start,
            "cause_open": self.cause_open,
            "cause_close": self.cause_close,
            "preamble_included": self.preamble_included,
            "data_quality": self.data_quality,
            "subsegments": [s.to_dict() for s in self.subsegments],
            "sequence_checks": self.sequence_checks,
            "events": self.events,
            "stop_kind": self.stop_kind,
            "coking_start": self.coking_start,
        }
