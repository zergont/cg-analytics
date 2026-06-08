# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Сегментация суточной временной шкалы на смысловые участки.

Layer 1 — три шага:
  1. Построить временны́е периоды из state_events (40011) → fallback RPM (40068)
  2. К каждому периоду прикрепить события state_events и аномалии
  3. Для каждого периода рассчитать взвешенные по времени статистики аналоговых параметров
"""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

# ── Константы ─────────────────────────────────────────────────────────────────

# Первичный сигнал: RunSequenceState
_RUN_SEQ_ADDR = 40011

# Сопоставление raw-значения RunSequenceState → тип сегмента
_RUN_SEQ_TO_TYPE: dict[int, str] = {
    0: "standstill",
    1: "startup_window",    # TimeDelayToStart
    2: "warmup",            # WarmupAtIdle
    3: "normal_operation",  # RatedFreqAndVoltage
    4: "cooldown",          # CooldownStopDelay
    5: "cooldown",          # CooldownAtIdle
    6: "cooldown",
    7: "fault_window",      # EmergencyShutdown
}

# Fallback: Engine Speed (RPM)
_RPM_ADDR = 40068
_RPM_IDLE_MIN = 1       # < 1 RPM → стоит
_RPM_RATED_MIN = 1400   # ≥ 1400 RPM → номинал

# Единицы измерения, которые включаем в параметры сегмента
_KEY_UNITS = {"°C", "kPa", "rpm", "kW", "kVA", "Hz", "Vac", "Vdc", "Amps", "%"}

# Порог значимого отклонения от взвешенного среднего (%)
_SPIKE_THRESHOLD_PCT = 20.0


# ── Публичный API ─────────────────────────────────────────────────────────────

def segment(
    history: list[dict[str, Any]],
    state_events: list[dict[str, Any]],
    anomalies: list[dict[str, Any]],
    operation_rules: dict[str, Any],
    register_map: dict[int, dict],
    day_start: datetime,
    day_end: datetime,
) -> list[dict[str, Any]]:
    """Разбить сутки на смысловые сегменты.

    Аргументы:
        history      — аналоговые данные из таблицы history
        state_events — смены состояния enum/discrete из таблицы state_events
        anomalies    — аномалии от detector
        operation_rules — правила из knowledge base
        register_map — карта регистров
        day_start / day_end — границы суток (UTC)

    Каждый сегмент содержит:
        type, start, end, duration_min, label, source,
        parameters  — взвешенные по времени статистики (min/max/mean/max_deviation),
        events      — события state_events внутри сегмента (кроме 40011),
        notes       — заметки по operation_rules и скачкам,
        related_anomalies
    """
    day_start = _ensure_tz(day_start)
    day_end   = _ensure_tz(day_end)

    # Аналоговые данные по адресам (отсортированные)
    by_addr: dict[int, list[dict]] = defaultdict(list)
    for row in history:
        by_addr[row["addr"]].append(row)
    for rows in by_addr.values():
        rows.sort(key=lambda r: _ensure_tz(r["ts"]))

    # Шаг 1: временны́е периоды
    periods, source = _build_periods(state_events, by_addr, day_start, day_end)

    # Шаг 2: строим сегменты
    segments: list[dict[str, Any]] = []
    for p_start, p_end, p_type in periods:
        if p_end <= p_start:
            continue

        params     = _calc_parameters(by_addr, p_start, p_end, register_map)
        seg_events = _events_in_window(state_events, p_start, p_end, exclude_addr=_RUN_SEQ_ADDR)
        notes      = _build_notes(p_type, p_start, p_end, by_addr, state_events,
                                  params, operation_rules, register_map)
        seg_anom   = _seg_anomalies(anomalies, p_start, p_end)

        segments.append({
            "type":              p_type,
            "start":             p_start.isoformat(),
            "end":               p_end.isoformat(),
            "duration_min":      _dur(p_start, p_end),
            "label":             _make_label(p_type, p_start, p_end),
            "source":            source,
            "parameters":        params,
            "events":            seg_events,
            "notes":             notes,
            "related_anomalies": seg_anom,
        })

    # Шаг 3: fault_window для аномалий вне существующих fault-сегментов
    existing_faults = [s for s in segments if s["type"] == "fault_window"]
    for anom in anomalies:
        if not anom.get("first_seen"):
            continue
        a_start = _parse_ts(anom["first_seen"])
        a_end   = _parse_ts(anom.get("last_seen") or anom["first_seen"])
        if any(_parse_ts(fw["start"]) <= a_start <= _parse_ts(fw["end"]) for fw in existing_faults):
            continue
        fw_s = max(day_start, a_start - timedelta(minutes=5))
        fw_e = min(day_end,   a_end   + timedelta(minutes=5))
        params = _calc_parameters(by_addr, fw_s, fw_e, register_map)
        segments.append({
            "type":         "fault_window",
            "start":        fw_s.isoformat(),
            "end":          fw_e.isoformat(),
            "duration_min": _dur(fw_s, fw_e),
            "label":        f"Авария: {anom.get('name', '?')} (severity={anom.get('severity','?')})",
            "source":       source,
            "parameters":   params,
            "events":       _events_in_window(state_events, fw_s, fw_e),
            "notes":        [anom.get("description", "")],
            "related_anomalies": [{
                "name":     anom.get("name"),
                "severity": anom.get("severity"),
                "type":     anom.get("type"),
            }],
        })

    segments.sort(key=lambda s: s["start"])
    return segments


# ── Шаг 1: построение временны́х периодов ─────────────────────────────────────

def _build_periods(
    state_events: list[dict],
    by_addr: dict[int, list[dict]],
    day_start: datetime,
    day_end: datetime,
) -> tuple[list[tuple[datetime, datetime, str]], str]:
    """Вернуть (периоды, источник).

    Источники (приоритет):
      - "run_sequence_40011" — state_events, addr=40011
      - "rpm_40068"          — history, addr=40068
      - "no_data"            — нет сигнала, весь день = standstill
    """
    periods = _periods_from_run_sequence(state_events, day_start, day_end)
    if periods is not None:
        return periods, "run_sequence_40011"

    periods = _periods_from_rpm(by_addr, day_start, day_end)
    if periods is not None:
        return periods, "rpm_40068"

    return [(day_start, day_end, "standstill")], "no_data"


def _periods_from_run_sequence(
    state_events: list[dict],
    day_start: datetime,
    day_end: datetime,
) -> list[tuple[datetime, datetime, str]] | None:
    """Периоды из RunSequenceState (addr=40011). None если данных нет."""
    evs = sorted(
        [e for e in state_events if int(e.get("addr", -1)) == _RUN_SEQ_ADDR],
        key=lambda e: _ensure_tz(e["ts"]),
    )
    if not evs:
        return None

    # Определяем состояние до первого события суток
    first_raw = int(evs[0].get("raw", 0) or 0)
    if first_raw == 0:
        # Стоп — значит до этого работала (пришла с предыдущих суток)
        pre_type = "normal_operation"
    elif first_raw in (4, 5, 6):
        # Охлаждение — тоже работала
        pre_type = "normal_operation"
    else:
        # Пуск или warmup — стояла
        pre_type = "standstill"

    periods: list[tuple[datetime, datetime, str]] = []

    first_ts = _ensure_tz(evs[0]["ts"])
    if first_ts > day_start:
        periods.append((day_start, first_ts, pre_type))

    for i, ev in enumerate(evs):
        ts      = _ensure_tz(ev["ts"])
        raw     = int(ev.get("raw", 0) or 0)
        p_type  = _RUN_SEQ_TO_TYPE.get(raw, "standstill")
        next_ts = _ensure_tz(evs[i + 1]["ts"]) if i + 1 < len(evs) else day_end
        if ts < next_ts:
            periods.append((ts, next_ts, p_type))

    return periods


def _periods_from_rpm(
    by_addr: dict[int, list[dict]],
    day_start: datetime,
    day_end: datetime,
) -> list[tuple[datetime, datetime, str]] | None:
    """Fallback: периоды по оборотам двигателя (addr=40068). None если нет данных."""
    if _RPM_ADDR not in by_addr or not by_addr[_RPM_ADDR]:
        return None

    def _to_type(val) -> str:
        if val is None:
            return "standstill"
        v = float(val)
        if v < _RPM_IDLE_MIN:
            return "standstill"
        if v < _RPM_RATED_MIN:
            return "warmup"   # холостой ход — прогрев или охлаждение, уточнить нельзя
        return "normal_operation"

    rows = by_addr[_RPM_ADDR]  # уже отсортированы
    periods: list[tuple[datetime, datetime, str]] = []

    first_ts   = _ensure_tz(rows[0]["ts"])
    first_type = _to_type(rows[0].get("value"))

    # до первой точки — простой (неизвестно)
    if first_ts > day_start:
        periods.append((day_start, first_ts, "standstill"))

    prev_ts, prev_type = first_ts, first_type

    for row in rows[1:]:
        ts  = _ensure_tz(row["ts"])
        cur = _to_type(row.get("value"))
        if cur != prev_type:
            if prev_ts < ts:
                periods.append((prev_ts, ts, prev_type))
            prev_ts, prev_type = ts, cur

    if prev_ts < day_end:
        periods.append((prev_ts, day_end, prev_type))

    return periods


# ── Шаг 2а: взвешенные по времени статистики ─────────────────────────────────

def _calc_parameters(
    by_addr: dict[int, list[dict]],
    seg_start: datetime,
    seg_end: datetime,
    register_map: dict[int, dict],
) -> dict[str, Any]:
    """Рассчитать min/max/mean(взвешенное)/max_deviation для аналоговых параметров."""
    result: dict[str, Any] = {}

    for addr, rows in by_addr.items():
        reg  = register_map.get(addr, {})
        unit = reg.get("unit", "")
        if unit not in _KEY_UNITS:
            continue

        na_set = {float(v) for v in reg.get("na_values", [])}

        # Точки внутри окна (ts, value)
        window: list[tuple[datetime, float]] = []
        for r in rows:
            ts = _ensure_tz(r["ts"])
            if not (seg_start <= ts <= seg_end):
                continue
            v = r.get("value")
            if v is None:
                continue
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            if fv in na_set:
                continue
            window.append((ts, fv))

        if len(window) < 2:
            continue

        values = [v for _, v in window]

        # Взвешенное по времени среднее:
        # каждое значение держится до следующего измерения
        total_w = 0.0
        weighted_sum = 0.0
        for i, (ts, v) in enumerate(window):
            next_ts = window[i + 1][0] if i + 1 < len(window) else seg_end
            dur = (next_ts - ts).total_seconds()
            if dur > 0:
                weighted_sum += v * dur
                total_w += dur

        wmean = weighted_sum / total_w if total_w > 0 else values[-1]
        vmin  = min(values)
        vmax  = max(values)
        max_dev = max(abs(v - wmean) for v in values)
        max_dev_pct = round(max_dev / wmean * 100, 1) if wmean != 0 else 0.0

        result[str(addr)] = {
            "name":              reg.get("name", f"reg_{addr}"),
            "unit":              unit,
            "min":               round(vmin, 2),
            "max":               round(vmax, 2),
            "mean":              round(wmean, 2),
            "max_deviation":     round(max_dev, 2),
            "max_deviation_pct": max_dev_pct,
            "count":             len(window),
        }

    return result


# ── Шаг 2б: события внутри сегмента ──────────────────────────────────────────

def _events_in_window(
    state_events: list[dict],
    start: datetime,
    end: datetime,
    exclude_addr: int | None = None,
) -> list[dict]:
    result = []
    for ev in state_events:
        if exclude_addr is not None and int(ev.get("addr", -1)) == exclude_addr:
            continue
        ts = _ensure_tz(ev["ts"])
        if start <= ts <= end:
            result.append({
                "ts":   ts.isoformat(),
                "addr": ev.get("addr"),
                "text": ev.get("text", ""),
                "raw":  ev.get("raw"),
            })
    return sorted(result, key=lambda x: x["ts"])


# ── Шаг 2в: заметки ──────────────────────────────────────────────────────────

def _build_notes(
    seg_type: str,
    seg_start: datetime,
    seg_end: datetime,
    by_addr: dict[int, list[dict]],
    state_events: list[dict],
    params: dict,
    operation_rules: dict,
    register_map: dict[int, dict],
) -> list[str]:
    notes: list[str] = []

    if seg_type == "startup_window":
        notes += _notes_startup(seg_start, seg_end, by_addr, state_events, params,
                                operation_rules, register_map)
    elif seg_type == "warmup":
        notes += _notes_warmup(params, operation_rules, register_map)
    elif seg_type == "normal_operation":
        notes += _notes_normal(params, operation_rules)
    elif seg_type == "cooldown":
        notes += _notes_cooldown(params, operation_rules)
    elif seg_type == "standstill":
        notes += _notes_standstill(params, operation_rules, register_map)
    elif seg_type == "fault_window":
        notes += _notes_fault(state_events, seg_start, seg_end)

    # Универсально: скачки параметров
    notes += _notes_spikes(params)

    return notes


def _notes_startup(
    seg_start, seg_end, by_addr, state_events, params, operation_rules, register_map
) -> list[str]:
    notes: list[str] = []
    ss = operation_rules.get("startup_sequence", {})

    # Время выхода на номинальный режим (raw=3 в 40011)
    evs_40011 = [
        e for e in state_events
        if int(e.get("addr", -1)) == _RUN_SEQ_ADDR
        and int(e.get("raw", -1) or -1) == 3
        and seg_start <= _ensure_tz(e["ts"]) <= seg_end
    ]
    if evs_40011:
        elapsed = int((_ensure_tz(evs_40011[0]["ts"]) - seg_start).total_seconds())
        notes.append(f"Выход на номинальный режим: {elapsed} сек")

    # Давление масла после пуска
    lop_delay = ss.get("lop_enable_time_sec", {})
    lop_sec   = lop_delay.get("value") if isinstance(lop_delay, dict) else 10
    min_press = ss.get("min_oil_pressure_after_start_kpa", {})
    min_press_val = min_press.get("value") if isinstance(min_press, dict) else 138

    oil_addr = _find_addr_by_keyword(register_map, ["oil", "pressure"])
    if oil_addr:
        p = params.get(str(oil_addr))
        if p:
            if p["max"] < min_press_val:
                notes.append(
                    f"Давление масла не достигло {min_press_val} кПа при пуске "
                    f"(макс {p['max']} кПа)"
                )
            else:
                notes.append(f"Давление масла при пуске: мин {p['min']} / макс {p['max']} кПа ✓")
    return notes


def _notes_warmup(params, operation_rules, register_map) -> list[str]:
    notes: list[str] = []
    normal_op = operation_rules.get("normal_operation", {})
    norm_min  = (
        normal_op.get("coolant_temperature_c", {})
        .get("normal", {})
        .get("min")
    )
    for addr_str, p in params.items():
        if "°C" in p.get("unit", "") and "coolant" in p.get("name", "").lower():
            notes.append(f"ОЖ за прогрев: {p['min']}–{p['max']} °C")
            if norm_min and p["max"] < norm_min:
                notes.append(
                    f"К концу прогрева ОЖ {p['max']}°C < нормы {norm_min}°C"
                )
    return notes


def _notes_normal(params, operation_rules) -> list[str]:
    """Проверка параметров нормальной работы на выход за порог."""
    notes: list[str] = []
    normal_op = operation_rules.get("normal_operation", {})

    def _check(param_name_kw, unit_kw, rules_key, bound_key, label, suffix=""):
        rule = normal_op.get(rules_key, {})
        threshold = rule.get(bound_key, {})
        val = threshold.get("value") if isinstance(threshold, dict) else None
        if val is None:
            return
        for addr_str, p in params.items():
            if unit_kw in p.get("unit", "") and param_name_kw in p.get("name", "").lower():
                if bound_key == "max" and p["max"] > val:
                    notes.append(f"{p['name']}: макс {p['max']}{suffix} > порога {val}{suffix}")
                elif bound_key == "min" and p["min"] < val:
                    notes.append(f"{p['name']}: мин {p['min']}{suffix} < порога {val}{suffix}")

    _check("coolant", "°C",  "coolant_temperature_c",   "max", "Т ОЖ", " °C")
    _check("oil",     "kPa", "oil_pressure_kpa",        "min", "Р масла", " кПа")
    _check("oil",     "°C",  "oil_temperature_c",       "max", "Т масла", " °C")
    return notes


def _notes_cooldown(params, operation_rules) -> list[str]:
    notes: list[str] = []
    for addr_str, p in params.items():
        if "°C" in p.get("unit", "") and "coolant" in p.get("name", "").lower():
            notes.append(f"ОЖ при охлаждении: {p['min']}–{p['max']} °C")
    return notes


def _notes_standstill(params, operation_rules, register_map) -> list[str]:
    notes: list[str] = []
    pre = operation_rules.get("pre_start_conditions", {})
    target = pre.get("coolant_heater_target_temp_c", {})
    target_c = target.get("value") if isinstance(target, dict) else None
    if not target_c:
        return notes
    for addr_str, p in params.items():
        if "°C" in p.get("unit", "") and "coolant" in p.get("name", "").lower():
            if p["min"] < target_c:
                notes.append(
                    f"Т ОЖ на простое: мин {p['min']}°C "
                    f"(ниже рекомендуемых {target_c}°C для подогревателя)"
                )
    return notes


def _notes_fault(state_events, seg_start, seg_end) -> list[str]:
    """События state_events внутри fault-окна как заметки."""
    notes: list[str] = []
    for ev in state_events:
        ts = _ensure_tz(ev["ts"])
        if seg_start <= ts <= seg_end:
            text = ev.get("text", "")
            if any(kw in text.lower() for kw in ("fault", "shutdown", "emergency", "error")):
                notes.append(f"{ts.strftime('%H:%M:%S')} [{ev.get('addr')}] {text}")
    return notes


def _notes_spikes(params: dict) -> list[str]:
    """Скачки параметров (отклонение > порога от взвешенного среднего)."""
    notes: list[str] = []
    for addr_str, p in params.items():
        dev_pct = p.get("max_deviation_pct", 0)
        if dev_pct >= _SPIKE_THRESHOLD_PCT and p.get("mean", 0) != 0:
            notes.append(
                f"⚡ {p['name']}: скачок до {p['max']} {p['unit']} "
                f"при среднем {p['mean']} (отклонение {dev_pct}%)"
            )
    return notes


# ── Вспомогательные функции ───────────────────────────────────────────────────

def _make_label(seg_type: str, start: datetime, end: datetime) -> str:
    labels = {
        "standstill":       "Простой",
        "startup_window":   "Пуск",
        "warmup":           "Прогрев",
        "normal_operation": "Нормальная работа",
        "cooldown":         "Охлаждение",
        "fault_window":     "Авария",
    }
    base = labels.get(seg_type, seg_type)
    return f"{base} ({start.strftime('%H:%M')}–{end.strftime('%H:%M')})"


def _seg_anomalies(anomalies: list[dict], start: datetime, end: datetime) -> list[dict]:
    result = []
    for a in anomalies:
        fs = a.get("first_seen")
        if not fs:
            continue
        try:
            if start <= _parse_ts(fs) <= end:
                result.append({
                    "name":     a.get("name"),
                    "severity": a.get("severity"),
                    "type":     a.get("type"),
                })
        except (ValueError, TypeError):
            pass
    return result


def _find_addr_by_keyword(register_map: dict[int, dict], keywords: list[str]) -> int | None:
    for addr, reg in register_map.items():
        name = reg.get("name", "").lower()
        if all(kw in name for kw in keywords):
            return addr
    return None


def _parse_ts(s: str | datetime) -> datetime:
    if isinstance(s, datetime):
        return _ensure_tz(s)
    return _ensure_tz(datetime.fromisoformat(str(s).replace("Z", "+00:00")))


def _ensure_tz(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _dur(start: datetime, end: datetime) -> int:
    return max(0, int((end - start).total_seconds() / 60))
