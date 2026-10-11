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


def _gap_overlap_sec(gaps: list[dict], t_start: datetime, t_end: datetime) -> float:
    """Суммарное время дыр связи внутри окна [t_start, t_end).

    Незакрытый gap (gap_end IS NULL) считается до t_end — как в data_quality.
    """
    total = 0.0
    for g in gaps:
        gs = max(_tz(g["gap_start"]), _tz(t_start))
        ge_raw = g.get("gap_end")
        ge = min(_tz(ge_raw) if ge_raw else _tz(t_end), _tz(t_end))
        if ge > gs:
            total += (ge - gs).total_seconds()
    return total


def _pair_spans_gap(ts1: datetime, ts2: datetime, gaps: list[dict]) -> bool:
    """True если между соседними строками лежит дыра связи (dt через дыру — не факт)."""
    for g in gaps:
        gs = _tz(g["gap_start"])
        ge_raw = g.get("gap_end")
        ge = _tz(ge_raw) if ge_raw else _tz(ts2)
        if gs < _tz(ts2) and ge > _tz(ts1):
            return True
    return False


def update_accumulators(
    acc: RiskAccumulators,
    by_addr: dict[int, list[dict[str, Any]]],
    seg_start: datetime,
    seg_end: datetime,
    load_zone: str,
    run_state: int,
    cfg: AnalyticsConfig,
    gaps: list[dict] | None = None,
) -> RiskAccumulators:
    """Обновить аккумуляторы рисков по данным одного подсегмента.

    Возвращает НОВЫЙ объект RiskAccumulators (иммутабельный стиль).
    В дырах связи риски НЕ накапливаются (мы не знаем, что делала машина):
    накопление идёт по effective-длительности (минус дыры), спад теплового
    риска — по стенному времени (мотор остывает независимо от телеметрии).
    """
    new_acc = acc.copy()
    gaps = gaps or []
    duration_sec = (_tz(seg_end) - _tz(seg_start)).total_seconds()
    effective_sec = max(0.0, duration_sec - _gap_overlap_sec(gaps, seg_start, seg_end))

    _update_coking(new_acc.coking_risk, by_addr, seg_start, seg_end,
                   load_zone, run_state, effective_sec, cfg, gaps)
    _update_thermal(new_acc.thermal_risk, by_addr, seg_start, seg_end,
                    load_zone, effective_sec, duration_sec, cfg, gaps)

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

# Холостой ход: прогрев и охлаждение на х.х. (RUN_STATE=6 — переход в единицы
# секунд, не длительный х.х.)
IDLE_RUN_STATES = frozenset({2, 5})

def _update_coking(
    cr: CokingRisk,
    by_addr: dict[int, list[dict[str, Any]]],
    seg_start: datetime,
    seg_end: datetime,
    load_zone: str,
    run_state: int,
    duration_sec: float,   # effective: стенное время минус дыры связи
    cfg: AnalyticsConfig,
    gaps: list[dict] | None = None,
) -> None:
    """Риск нагара — долг до прожига (v5.1.3, решения 11.10.2026).

    Копится при недогрузе: холостой ход (RUN_STATE 2/5) — с весом
    debt_weight_idle (х.х. вреднее: MAN — без нагрузки ≤1 ч против ~200 ч при
    17 %), работа с холодной ОЖ, работа ниже purge_min_load_pct по ФАКТИЧЕСКОЙ
    нагрузке LOAD_PCT (не по зоне — гистерезис держит NORMAL и при 25 %).
    Стоп ничего не меняет: отложения остаются до прожига.

    Прожиг — пропорциональный зачёт: минута под нагрузкой ≥ purge_min_load_pct
    гасит purge_rate_low минут долга (Cat: 30 мин на каждые 4 ч недогруза),
    ≥ purge_high_load_pct — purge_rate_high (MAN). Засчитываются только
    непрерывные отрезки от purge_min_duration_sec (15 мин; источники не
    засчитывают меньше): до этой длины зачёт копится в ожидании и сгорает при
    обрыве. Отрезок идёт сквозь границы подсегментов, обрывается сменой
    режима, разрывом связи, малой нагрузкой и холодной ОЖ. Гасится долг
    пропорционально по всем счётчикам; погашен целиком — полный прожиг.
    """
    gaps = gaps or []
    t_start = _tz(seg_start)
    t_end = _tz(seg_end)

    w_idle = float(cfg.det("COKING_RISK", "debt_weight_idle", default=2.0))
    w_cold = float(cfg.det("COKING_RISK", "debt_weight_cold", default=1.0))
    low_pct = float(cfg.thr("engine_operation", "tunable", "purge_min_load_pct", default=30.0))
    high_pct = float(cfg.det("COKING_RISK", "purge_high_load_pct", default=70.0))
    rate_low = float(cfg.det("COKING_RISK", "purge_rate_low", default=8.0))
    rate_high = float(cfg.det("COKING_RISK", "purge_rate_high", default=16.0))
    min_block = float(cfg.thr("engine_operation", "tunable", "purge_min_duration_sec", default=900.0))
    purge_cool = float(cfg.det("COKING_RISK", "purge_min_coolant_c", default=60.0))
    dip_tol = float(cfg.det("COKING_RISK", "purge_dip_tolerance_sec", default=60.0))
    # По Cummins (v5.1.5): прогон T-030 §7.5.5 (≥3 ч, из них ≥1 ч около номинала) —
    # полный прожиг; ежемесячно ≥30 мин при ≥30 % (A035B516, T-030 §7.5.3);
    # без нагрузки (<10 %) на номинальных оборотах — не дольше 15 мин (A035B516 §5.4.2)
    t030_run = float(cfg.det("COKING_RISK", "t030_run_min_sec", default=10800.0))
    t030_hi_pct = float(cfg.det("COKING_RISK", "t030_high_pct", default=75.0))
    t030_hi = float(cfg.det("COKING_RISK", "t030_high_min_sec", default=3600.0))
    monthly_min = float(cfg.det("COKING_RISK", "monthly_run_min_sec", default=1800.0))
    service_debt = float(cfg.det("COKING_RISK", "service_purge_debt_sec", default=115200.0))
    noload_pct = float(cfg.det("NO_LOAD_TOO_LONG", "load_pct", default=10.0))
    if cr.calendar_since_ts is None:
        cr.calendar_since_ts = t_start.isoformat()   # учёт прогонов — с первого обновления
    below_c = float(cfg.thr("coolant_temperature", "tunable", "below_60_risk_threshold_c", default=60.0))

    added = 0.0
    last_acc: datetime | None = None

    def debt() -> float:
        return cr.low_load_zone_sec + w_idle * cr.idle_low_rpm_sec + w_cold * cr.coolant_below_60_sec

    def pay(credit: float, ts: datetime) -> None:
        d = debt()
        if credit <= 0 or d <= 0:
            return
        k = max(0.0, 1.0 - credit / d)
        cr.debt_paid_total_sec += min(credit, d)
        cr.low_load_zone_sec *= k
        cr.idle_low_rpm_sec *= k
        cr.coolant_below_60_sec *= k
        if debt() < 1.0:
            cr.low_load_zone_sec = cr.idle_low_rpm_sec = cr.coolant_below_60_sec = 0.0
            cr.last_purge_ts = ts.isoformat()

    def break_run() -> None:
        cr.purge_run_sec = 0.0
        cr.purge_pending_sec = 0.0
        cr.purge_dip_sec = 0.0
        cr.purge_high_sec = 0.0
        cr.t030_done = False

    def full_purge(ts: datetime) -> None:
        """Прогон по T-030 — лечение по Cummins выполнено: долг погашен целиком."""
        cr.debt_paid_total_sec += debt()
        cr.low_load_zone_sec = cr.idle_low_rpm_sec = cr.coolant_below_60_sec = 0.0
        cr.purge_pending_sec = 0.0
        cr.last_purge_ts = ts.isoformat()

    def noload_reset() -> None:
        cr.noload_run_sec = 0.0
        cr.noload_break_sec = 0.0

    # 1. Холостой ход: суммарно — в долг, непрерывный отрезок — для замечания
    if run_state in IDLE_RUN_STATES:
        cr.idle_low_rpm_sec += duration_sec
        cr.idle_run_sec += duration_sec
        added += w_idle * duration_sec
        if duration_sec > 0:
            last_acc = t_end
    else:
        cr.idle_run_sec = 0.0

    # 2. Холодная ОЖ — только при работающем двигателе (на стоящем — норма)
    cool_addr = cfg.role_to_addr("COOLANT_TEMP")
    if run_state != 0 and cool_addr and cool_addr in by_addr:
        rows = [r for r in by_addr[cool_addr]
                if t_start <= _tz(r["ts"]) < t_end and r.get("value") is not None]
        for i, r in enumerate(rows[:-1]):
            v = _fv(r)
            if v is not None and v < below_c:
                if _pair_spans_gap(r["ts"], rows[i + 1]["ts"], gaps):
                    continue  # dt через дыру связи — не знаем, что было
                dt = max(0.0, (_tz(rows[i + 1]["ts"]) - _tz(r["ts"])).total_seconds())
                cr.coolant_below_60_sec += dt
                added += w_cold * dt
                if dt > 0:
                    last_acc = _tz(rows[i + 1]["ts"])

    # 3. Нагрузка под работой: построчно — малая копит долг, достаточная гасит
    load_addr = cfg.role_to_addr("LOAD_PCT")
    if run_state != 3:
        break_run()
        noload_reset()
    elif load_addr and load_addr in by_addr:
        # ОЖ по строкам — последняя известная к моменту строки нагрузки. Прожиг
        # не засчитывается только при ИЗВЕСТНОЙ холодной ОЖ: нет датчика или
        # строки (ОЖ пишется по изменению) — по одной нагрузке
        cool_rows = sorted(
            ((_tz(r["ts"]), _fv(r)) for r in (by_addr.get(cool_addr) or [] if cool_addr else [])
             if _fv(r) is not None and _tz(r["ts"]) < t_end),
            key=lambda x: x[0])
        ci, cool_now = 0, None
        rows = sorted((r for r in by_addr[load_addr]
                       if t_start <= _tz(r["ts"]) < t_end and _fv(r) is not None),
                      key=lambda r: _tz(r["ts"]))
        for i, r in enumerate(rows):
            ts = _tz(r["ts"])
            while ci < len(cool_rows) and cool_rows[ci][0] <= ts:
                cool_now = cool_rows[ci][1]
                ci += 1
            nxt = _tz(rows[i + 1]["ts"]) if i + 1 < len(rows) else t_end
            if i + 1 < len(rows) and _pair_spans_gap(r["ts"], rows[i + 1]["ts"], gaps):
                break_run()       # не знаем, что было в дыре
                noload_reset()
                continue
            if i + 1 == len(rows) and _gap_overlap_sec(gaps, ts, nxt) > 0:
                break_run()
                noload_reset()
                continue
            dt = max(0.0, (nxt - ts).total_seconds())
            if dt <= 0:
                continue          # дубль метки — не интервал
            warm = cool_now is None or cool_now >= purge_cool
            v = _fv(r)
            # Без нагрузки на номинальных оборотах — непрерывный отрезок с тем же
            # допуском на короткие выходы, что и у прожига
            if v < noload_pct:
                cr.noload_run_sec += dt
                cr.noload_break_sec = 0.0
            elif cr.noload_run_sec > 0:
                cr.noload_break_sec += dt
                if cr.noload_break_sec > dip_tol:
                    noload_reset()
            if v < low_pct:
                cr.low_load_zone_sec += dt
                added += dt
                if dt > 0:
                    last_acc = nxt
                # Короткий провал ниже порога внутри отрезка — шум регулирования у
                # границы 30 % (опрос 9 с): отрезок не рвётся, пока провал короче
                # допуска (решение 11.10). Без него ДЭС №3, работающая у порога,
                # засчитывала в прожиг 23 % времени при ≥30 %, и риск не гас никогда
                cr.purge_dip_sec += dt
                if cr.purge_run_sec <= 0 or cr.purge_dip_sec > dip_tol:
                    break_run()
                else:
                    cr.purge_run_sec += dt
            elif not warm:
                break_run()       # нагрузка есть, но мотор холодный — не прожиг
            else:
                cr.purge_dip_sec = 0.0
                credit = dt * (rate_high if v >= high_pct else rate_low)
                cr.purge_run_sec += dt
                if v >= t030_hi_pct:
                    cr.purge_high_sec += dt
                if cr.purge_run_sec - dt >= min_block:
                    pay(credit, nxt)
                else:
                    cr.purge_pending_sec += credit
                    if cr.purge_run_sec >= min_block:
                        pay(cr.purge_pending_sec, nxt)
                        cr.purge_pending_sec = 0.0
                if cr.purge_run_sec >= monthly_min:
                    cr.last_monthly_run_ts = nxt.isoformat()
                if (not cr.t030_done and cr.purge_run_sec >= t030_run
                        and cr.purge_high_sec >= t030_hi):
                    full_purge(nxt)
                    cr.t030_done = True
                    cr.last_annual_run_ts = nxt.isoformat()
    # RS 3 без строк нагрузки (короткий подсегмент) — «нет сведений»: отрезок
    # не обрывается и не растёт

    cr.last_added_sec = added
    cr.debt_added_total_sec += added
    if last_acc is not None:
        cr.last_accum_ts = last_acc.isoformat()

    # 4. Уровень — по каждому счётчику (решения 11.10): малая нагрузка 2/4 ч
    # (Cat, Fort Calhoun, EMD), х.х. суммарно 1/3 ч, холодная ОЖ 1/3 ч
    y_low = float(cfg.det("COKING_RISK", "risk_yellow_low_load_sec", default=7200))
    r_low = float(cfg.det("COKING_RISK", "risk_red_low_load_sec", default=14400))
    y_idle = float(cfg.det("COKING_RISK", "risk_yellow_idle_sec", default=3600))
    r_idle = float(cfg.det("COKING_RISK", "risk_red_idle_sec", default=10800))
    y_cold = float(cfg.det("COKING_RISK", "risk_yellow_cold_sec", default=3600))
    r_cold = float(cfg.det("COKING_RISK", "risk_red_cold_sec", default=10800))
    if (cr.low_load_zone_sec >= r_low or cr.idle_low_rpm_sec >= r_idle
            or cr.coolant_below_60_sec >= r_cold):
        cr.risk_level = "RED"
    elif (cr.low_load_zone_sec >= y_low or cr.idle_low_rpm_sec >= y_idle
            or cr.coolant_below_60_sec >= y_cold):
        cr.risk_level = "YELLOW"
    else:
        cr.risk_level = "GREEN"

    # Для показа: долг и сколько прожига до сброса (отрезок не короче минимума)
    cr.purge_low_pct, cr.purge_high_pct = low_pct, high_pct
    cr.debt_sec = debt()
    # Эскалация: потолка долга у Cummins нет, а штатное лечение — годовой прогон
    # T-030 (32 ч в наших ставках). Выше — «нужен сервисный прожиг»; снимается
    # только полным погашением или прогоном T-030 (решение 11.10.2026)
    if cr.debt_sec > service_debt:
        cr.service_needed = True
    elif cr.debt_sec < 1.0:
        cr.service_needed = False
    if cr.debt_sec >= 1.0:
        cr.purge_left_low_min = round(max(min_block, cr.debt_sec / rate_low) / 60.0, 1)
        cr.purge_left_high_min = round(max(min_block, cr.debt_sec / rate_high) / 60.0, 1)
    else:
        cr.purge_left_low_min = cr.purge_left_high_min = None


# ── Thermal Risk ─────────────────────────────────────────────────────────────

def _update_thermal(
    tr: ThermalRisk,
    by_addr: dict[int, list[dict[str, Any]]],
    seg_start: datetime,
    seg_end: datetime,
    load_zone: str,
    effective_sec: float,   # накопление: стенное время минус дыры связи
    wall_sec: float,        # спад: мотор остывает независимо от телеметрии
    cfg: AnalyticsConfig,
    gaps: list[dict] | None = None,
) -> None:
    gaps = gaps or []
    t_start = _tz(seg_start)
    t_end = _tz(seg_end)

    # 1. Накопление или спад elevated_zone_sec
    # THERMAL — ОБРАТИМЫЙ риск: снятие нагрузки → мотор остывает → риск уходит.
    # Накапливаем в ELEVATED/OVERLOAD, спадаем с настраиваемой скоростью вне них.
    if load_zone in ("ELEVATED", "OVERLOAD"):
        tr.elevated_zone_sec += effective_sec
    else:
        decay = float(cfg.det("THERMAL_HIGHLOAD", "thermal_decay_rate_per_sec", default=0.5))
        tr.elevated_zone_sec = max(0.0, tr.elevated_zone_sec - wall_sec * decay)

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
                    if _pair_spans_gap(r["ts"], rows[i + 1]["ts"], gaps):
                        continue  # dt через дыру связи — не знаем, что было
                    dt = (_tz(rows[i + 1]["ts"]) - _tz(r["ts"])).total_seconds()
                    tr.coolant_near_limit_sec += max(0, dt)
    else:
        decay = float(cfg.det("THERMAL_HIGHLOAD", "thermal_decay_rate_per_sec", default=0.5))
        tr.coolant_near_limit_sec = max(0.0, tr.coolant_near_limit_sec - wall_sec * decay)

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
                    if _pair_spans_gap(r["ts"], rows[i + 1]["ts"], gaps):
                        continue  # dt через дыру связи — не знаем, что было
                    dt = (_tz(rows[i + 1]["ts"]) - _tz(r["ts"])).total_seconds()
                    tr.oil_near_limit_sec += max(0, dt)
    else:
        decay = float(cfg.det("THERMAL_HIGHLOAD", "thermal_decay_rate_per_sec", default=0.5))
        tr.oil_near_limit_sec = max(0.0, tr.oil_near_limit_sec - wall_sec * decay)

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
