# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Сквозные аккумуляторы рисков — ТЗ раздел 3.6.

Аккумуляторы продолжаются через границы подсегментов одной зоны/режима.
Сброс — только по явному условию (прожиг, выход из режима).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .contract import CokingRisk, ThermalRisk, RiskAccumulators
from .config import AnalyticsConfig


def _tz(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def update_accumulators(
    acc: RiskAccumulators,
    by_addr: dict[int, list[dict[str, Any]]],
    seg_start: datetime,
    seg_end: datetime,
    load_zone: str,
    run_state: int,
    cfg: AnalyticsConfig,
) -> RiskAccumulators:
    """Обновить аккумуляторы рисков по данным одного подсегмента.

    Возвращает НОВЫЙ объект RiskAccumulators (иммутабельный стиль).
    """
    new_acc = acc.copy()
    duration_sec = (_tz(seg_end) - _tz(seg_start)).total_seconds()

    _update_coking(new_acc.coking_risk, by_addr, seg_start, seg_end,
                   load_zone, run_state, duration_sec, cfg)
    _update_thermal(new_acc.thermal_risk, by_addr, seg_start, seg_end,
                    load_zone, duration_sec, cfg)

    return new_acc


def reset_accumulators_on_purge(
    acc: RiskAccumulators,
    purge_ts: datetime,
) -> RiskAccumulators:
    """Сбросить coking_risk после подтверждённого «прожига»."""
    new_acc = acc.copy()
    new_acc.coking_risk.idle_low_rpm_sec = 0.0
    new_acc.coking_risk.coolant_below_60_sec = 0.0
    new_acc.coking_risk.low_load_zone_sec = 0.0
    new_acc.coking_risk.risk_level = "GREEN"
    new_acc.coking_risk.last_purge_ts = _tz(purge_ts).isoformat()
    return new_acc


# ── Coking Risk ──────────────────────────────────────────────────────────────

def _update_coking(
    cr: CokingRisk,
    by_addr: dict[int, list[dict[str, Any]]],
    seg_start: datetime,
    seg_end: datetime,
    load_zone: str,
    run_state: int,
    duration_sec: float,
    cfg: AnalyticsConfig,
) -> None:
    t_start = _tz(seg_start)
    t_end = _tz(seg_end)

    # 1. Idle на малых оборотах (RUN_STATE = 2, 5 — Warmup/Cooldown at Idle)
    # RUN_STATE=6 «Rated to Idle Transition Delay» — короткий переходный период (единицы секунд),
    # не накапливаем idle-риск: это не длительный холостой ход, а переход между режимами.
    idle_states = {2, 5}
    if run_state in idle_states:
        cr.idle_low_rpm_sec += duration_sec

    # 2. Т ОЖ < порога (wet stacking risk)
    # Только при работающем двигателе (run_state != 0): на остановленной машине
    # холодная ОЖ — норма, риска закоксовки нет.
    cool_addr = cfg.role_to_addr("COOLANT_TEMP")
    below_c = float(cfg.thr("coolant_temperature", "tunable", "below_60_risk_threshold_c", default=60.0))
    if run_state != 0 and cool_addr and cool_addr in by_addr:
        rows = [r for r in by_addr[cool_addr]
                if t_start <= _tz(r["ts"]) < t_end and r.get("value") is not None]
        for i, r in enumerate(rows[:-1]):
            v = _fv(r)
            if v is not None and v < below_c:
                dt = (_tz(rows[i + 1]["ts"]) - _tz(r["ts"])).total_seconds()
                cr.coolant_below_60_sec += max(0, dt)

    # 3. Зона LOW под нагрузкой (run_state = 3)
    if run_state == 3 and load_zone == "LOW":
        cr.low_load_zone_sec += duration_sec

    # 4. Проверить прожиг: run_state=3, зона NORMAL+, Т ОЖ >= below_c, длительность >= порога
    purge_load_pct = float(cfg.thr("engine_operation", "tunable", "purge_min_load_pct", default=30.0))
    purge_dur_sec = float(cfg.thr("engine_operation", "tunable", "purge_min_duration_sec", default=300.0))
    purge_coolant_c = float(cfg.det("COKING_RISK", "purge_min_coolant_c", default=60.0))

    if (run_state == 3
            and load_zone not in ("LOW", "NA")
            and duration_sec >= purge_dur_sec):
        # Проверяем, что ОЖ была прогрета
        avg_cool = _avg_in_window(by_addr, cfg.role_to_addr("COOLANT_TEMP"), t_start, t_end)
        if avg_cool is not None and avg_cool >= purge_coolant_c:
            # Прожиг подтверждён — сброс
            cr.idle_low_rpm_sec = 0.0
            cr.coolant_below_60_sec = 0.0
            cr.low_load_zone_sec = 0.0
            cr.last_purge_ts = t_end.isoformat()

    # 5. Обновить уровень риска
    yellow_idle = float(cfg.det("COKING_RISK", "risk_yellow_idle_sec", default=600))
    red_idle = float(cfg.det("COKING_RISK", "risk_red_idle_sec", default=1800))
    yellow_low = float(cfg.det("COKING_RISK", "risk_yellow_low_load_sec", default=3600))
    red_low = float(cfg.det("COKING_RISK", "risk_red_low_load_sec", default=7200))

    if (cr.idle_low_rpm_sec >= red_idle or cr.low_load_zone_sec >= red_low
            or cr.coolant_below_60_sec >= red_idle * 3):
        cr.risk_level = "RED"
    elif (cr.idle_low_rpm_sec >= yellow_idle or cr.low_load_zone_sec >= yellow_low):
        cr.risk_level = "YELLOW"
    else:
        cr.risk_level = "GREEN"


# ── Thermal Risk ─────────────────────────────────────────────────────────────

def _update_thermal(
    tr: ThermalRisk,
    by_addr: dict[int, list[dict[str, Any]]],
    seg_start: datetime,
    seg_end: datetime,
    load_zone: str,
    duration_sec: float,
    cfg: AnalyticsConfig,
) -> None:
    t_start = _tz(seg_start)
    t_end = _tz(seg_end)

    # 1. Накопление или спад elevated_zone_sec
    # THERMAL — ОБРАТИМЫЙ риск: снятие нагрузки → мотор остывает → риск уходит.
    # Накапливаем в ELEVATED/OVERLOAD, спадаем с настраиваемой скоростью вне них.
    if load_zone in ("ELEVATED", "OVERLOAD"):
        tr.elevated_zone_sec += duration_sec
    else:
        decay = float(cfg.det("THERMAL_HIGHLOAD", "thermal_decay_rate_per_sec", default=0.5))
        tr.elevated_zone_sec = max(0.0, tr.elevated_zone_sec - duration_sec * decay)

    # 2. Время с Т ОЖ вблизи порога HCT Warning (только в ELEVATED/OVERLOAD)
    if load_zone in ("ELEVATED", "OVERLOAD"):
        hct_warn = float(cfg.thr("coolant_temperature", "controller", "warning_c", default=97.8))
        near_pct = float(cfg.det("THERMAL_HIGHLOAD", "coolant_near_limit_pct", default=5.0))
        hct_near = hct_warn * (1 - near_pct / 100)
        cool_addr = cfg.role_to_addr("COOLANT_TEMP")
        if cool_addr and cool_addr in by_addr:
            rows = [r for r in by_addr[cool_addr]
                    if t_start <= _tz(r["ts"]) < t_end and r.get("value") is not None]
            for i, r in enumerate(rows[:-1]):
                v = _fv(r)
                if v is not None and v >= hct_near:
                    dt = (_tz(rows[i + 1]["ts"]) - _tz(r["ts"])).total_seconds()
                    tr.coolant_near_limit_sec += max(0, dt)
    else:
        decay = float(cfg.det("THERMAL_HIGHLOAD", "thermal_decay_rate_per_sec", default=0.5))
        tr.coolant_near_limit_sec = max(0.0, tr.coolant_near_limit_sec - duration_sec * decay)

    # 3. Время с Т масла вблизи порога HOT Warning (только в ELEVATED/OVERLOAD)
    if load_zone in ("ELEVATED", "OVERLOAD"):
        hot_warn = float(cfg.thr("oil_temperature", "controller", "warning_c", default=105.0))
        near_pct_oil = float(cfg.det("THERMAL_HIGHLOAD", "oil_near_limit_pct", default=5.0))
        hot_near = hot_warn * (1 - near_pct_oil / 100)
        oil_addr = cfg.role_to_addr("OIL_TEMP")
        if oil_addr and oil_addr in by_addr:
            rows = [r for r in by_addr[oil_addr]
                    if t_start <= _tz(r["ts"]) < t_end and r.get("value") is not None]
            for i, r in enumerate(rows[:-1]):
                v = _fv(r)
                if v is not None and v >= hot_near:
                    dt = (_tz(rows[i + 1]["ts"]) - _tz(r["ts"])).total_seconds()
                    tr.oil_near_limit_sec += max(0, dt)
    else:
        decay = float(cfg.det("THERMAL_HIGHLOAD", "thermal_decay_rate_per_sec", default=0.5))
        tr.oil_near_limit_sec = max(0.0, tr.oil_near_limit_sec - duration_sec * decay)

    # 4. Обновить уровень риска
    yellow_elev = float(cfg.det("THERMAL_HIGHLOAD", "risk_yellow_elevated_sec", default=3600))
    red_elev = float(cfg.det("THERMAL_HIGHLOAD", "risk_red_elevated_sec", default=7200))

    if tr.elevated_zone_sec >= red_elev:
        tr.risk_level = "RED"
    elif tr.elevated_zone_sec >= yellow_elev:
        tr.risk_level = "YELLOW"
    else:
        tr.risk_level = "GREEN"


# ── Вспомогательные ──────────────────────────────────────────────────────────

def _fv(row: dict) -> float | None:
    v = row.get("value")
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _avg_in_window(
    by_addr: dict[int, list[dict]],
    addr: int | None,
    t_start: datetime,
    t_end: datetime,
) -> float | None:
    if addr is None or addr not in by_addr:
        return None
    vals = []
    for r in by_addr[addr]:
        ts = _tz(r["ts"])
        if t_start <= ts < t_end:
            v = _fv(r)
            if v is not None:
                vals.append(v)
    return sum(vals) / len(vals) if vals else None
