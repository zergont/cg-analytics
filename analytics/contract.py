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
    slope: Optional[float]              # ед./час, по реальным device-timestamp
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
    current_imbalance_pct_max: Optional[float] = None
    current_imbalance_pct_med: Optional[float] = None
    power_imbalance_pct_max: Optional[float] = None
    imbalance_duration_sec: Optional[float] = None
    s_consistency_max: Optional[float] = None
    pf_consistency_max: Optional[float] = None
    oil_coolant_delta_med: Optional[float] = None
    oil_coolant_delta_max: Optional[float] = None
    rpm_stability_mad: Optional[float] = None
    freq_stability_mad: Optional[float] = None
    dP_dt_max: Optional[float] = None          # кВт/с
    dRPM_dt_max: Optional[float] = None        # об/мин/с
    dCoolant_dt_max: Optional[float] = None    # °C/час
    dOil_press_dt_min: Optional[float] = None  # кПа/с (отрицательная)
    coolant_below_60_sec: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


# ── 3.6 RISK_ACCUMULATORS ───────────────────────────────────────────────────

@dataclass
class CokingRisk:
    idle_low_rpm_sec: float = 0.0
    coolant_below_60_sec: float = 0.0
    low_load_zone_sec: float = 0.0
    risk_level: str = "GREEN"           # GREEN / YELLOW / RED
    last_purge_ts: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "idle_low_rpm_sec": self.idle_low_rpm_sec,
            "coolant_below_60_sec": self.coolant_below_60_sec,
            "low_load_zone_sec": self.low_load_zone_sec,
            "risk_level": self.risk_level,
            "last_purge_ts": self.last_purge_ts,
        }


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

@dataclass
class Detection:
    """Сработавший диагностический сценарий."""
    scenario: str                  # LOAD_STEP / PHASE_IMBALANCE / ...
    severity: str                  # INFO / WARNING / ALARM / SHUTDOWN
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
        }
