"""Сегментация суточной временной шкалы на смысловые участки.

Layer 1: разбивает сутки на контекстные окна вокруг событий
(пуск, останов, аварийные эпизоды) для передачи агенту.
"""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

# Размеры контекстных окон
_STARTUP_PRE_MIN = 2
_STARTUP_POST_MIN = 10
_SHUTDOWN_PRE_MIN = 5
_SHUTDOWN_POST_MIN = 2
_FAULT_CONTEXT_MIN = 5

# Ключевые единицы измерения для per-сегментных агрегатов
_KEY_UNITS = {"°C", "kPa", "rpm", "kW", "kVA", "Hz", "Vac", "Vdc", "Amps", "%"}

# Адреса статусных регистров PCC3300
_RUN_SEQUENCE_ADDR = 40011   # 0=Stop,1=TimeDelay,2=WarmupIdle,3=RatedFreqVolt,...
_ENGINE_STATE_ADDR = 40283   # 0=Off,3=Starting,4=Idle,5=Rated,6=StopNormal,...
_OIL_PRESS_ADDR = 40100      # давление масла (типовой адрес — уточняется по register_map)


def segment(
    history: list[dict[str, Any]],
    operating_intervals: list[tuple[str, str]],
    anomalies: list[dict[str, Any]],
    events: list[dict[str, Any]],
    operation_rules: dict[str, Any],
    register_map: dict[int, dict],
    day_start: datetime,
    day_end: datetime,
) -> list[dict[str, Any]]:
    """Разбить сутки на смысловые сегменты.

    Returns:
        Хронологически упорядоченный список сегментов. Контекстные окна
        пуска/останова могут перекрываться с соседними сегментами.
    """
    by_addr: dict[int, list[dict]] = defaultdict(list)
    for row in history:
        by_addr[row["addr"]].append(row)
    # Сортируем каждый адрес по времени один раз
    for rows in by_addr.values():
        rows.sort(key=lambda r: r["ts"])

    warmup_min, cooldown_min = _extract_timing(operation_rules)

    parsed: list[tuple[datetime, datetime]] = [
        (_parse_ts(s), _parse_ts(e)) for s, e in operating_intervals
    ]
    parsed.sort()

    segments: list[dict[str, Any]] = []

    # Простои
    prev = day_start
    for start, end in parsed:
        if prev < start:
            segments.append(_standstill_seg(prev, start, by_addr, register_map, operation_rules))
        prev = end
    if prev < day_end:
        segments.append(_standstill_seg(prev, day_end, by_addr, register_map, operation_rules))

    # Сегменты по каждому интервалу работы
    for start, end in parsed:
        # Окно пуска
        win_s = max(day_start, start - timedelta(minutes=_STARTUP_PRE_MIN))
        win_e = min(day_end, start + timedelta(minutes=_STARTUP_POST_MIN))
        segments.append({
            "type": "startup_window",
            "start": win_s.isoformat(),
            "end": win_e.isoformat(),
            "duration_min": _dur(win_s, win_e),
            "label": f"Пуск в {start.strftime('%H:%M')}",
            "parameters": _agg_window(by_addr, win_s, win_e, register_map),
            "notes": _analyze_startup(by_addr, start, win_e, operation_rules, register_map),
            "related_anomalies": _seg_anomalies(anomalies, win_s, win_e),
        })

        # Прогрев
        warmup_end = min(end, start + timedelta(minutes=warmup_min))
        if _dur(start, warmup_end) > 2:
            segments.append({
                "type": "warmup",
                "start": start.isoformat(),
                "end": warmup_end.isoformat(),
                "duration_min": _dur(start, warmup_end),
                "label": f"Прогрев ({start.strftime('%H:%M')}–{warmup_end.strftime('%H:%M')})",
                "parameters": _agg_window(by_addr, start, warmup_end, register_map),
                "notes": _analyze_warmup(by_addr, start, warmup_end, operation_rules, register_map),
                "related_anomalies": _seg_anomalies(anomalies, start, warmup_end),
            })

        # Нормальная работа
        op_s = warmup_end
        op_e = max(op_s, end - timedelta(minutes=cooldown_min))
        if _dur(op_s, op_e) > 0:
            segments.append({
                "type": "normal_operation",
                "start": op_s.isoformat(),
                "end": op_e.isoformat(),
                "duration_min": _dur(op_s, op_e),
                "label": f"Нормальная работа ({op_s.strftime('%H:%M')}–{op_e.strftime('%H:%M')})",
                "parameters": _agg_window(by_addr, op_s, op_e, register_map),
                "notes": [],
                "related_anomalies": _seg_anomalies(anomalies, op_s, op_e),
            })

        # Охлаждение перед остановом
        cool_s = op_e
        if _dur(cool_s, end) > 0:
            segments.append({
                "type": "cooldown",
                "start": cool_s.isoformat(),
                "end": end.isoformat(),
                "duration_min": _dur(cool_s, end),
                "label": f"Охлаждение перед остановом в {end.strftime('%H:%M')}",
                "parameters": _agg_window(by_addr, cool_s, end, register_map),
                "notes": [],
                "related_anomalies": _seg_anomalies(anomalies, cool_s, end),
            })

        # Окно останова
        shut_s = max(day_start, end - timedelta(minutes=_SHUTDOWN_PRE_MIN))
        shut_e = min(day_end, end + timedelta(minutes=_SHUTDOWN_POST_MIN))
        segments.append({
            "type": "shutdown_window",
            "start": shut_s.isoformat(),
            "end": shut_e.isoformat(),
            "duration_min": _dur(shut_s, shut_e),
            "label": f"Останов в {end.strftime('%H:%M')}",
            "parameters": _agg_window(by_addr, shut_s, shut_e, register_map),
            "notes": _analyze_shutdown(by_addr, end, operation_rules, register_map),
            "related_anomalies": _seg_anomalies(anomalies, shut_s, shut_e),
        })

    # Fault windows для аварийных эпизодов
    for anom in anomalies:
        if anom.get("type") != "fault_bit" or not anom.get("first_seen"):
            continue
        first = _parse_ts(anom["first_seen"])
        last = _parse_ts(anom["last_seen"])
        fw_s = max(day_start, first - timedelta(minutes=_FAULT_CONTEXT_MIN))
        fw_e = min(day_end, last + timedelta(minutes=_FAULT_CONTEXT_MIN))
        segments.append({
            "type": "fault_window",
            "start": fw_s.isoformat(),
            "end": fw_e.isoformat(),
            "duration_min": _dur(fw_s, fw_e),
            "label": f"Авария: {anom['name']} (severity={anom.get('severity','?')})",
            "severity": anom.get("severity", "warning"),
            "parameters": _agg_window(by_addr, fw_s, fw_e, register_map),
            "notes": [anom.get("description", "")],
            "related_anomalies": [anom],
        })

    segments.sort(key=lambda s: s["start"])
    return segments


# ── Построение отдельных сегментов ───────────────────────────────────────────

def _standstill_seg(
    start: datetime,
    end: datetime,
    by_addr: dict[int, list[dict]],
    register_map: dict[int, dict],
    operation_rules: dict,
) -> dict[str, Any]:
    params = _agg_window(by_addr, start, end, register_map)
    notes = []

    # Предупреждение если ОЖ упала ниже порога подогревателя
    cool_warn = (
        operation_rules.get("pre_start_conditions", {})
        .get("coolant_heater_target_temp_c", {})
    )
    target_c = cool_warn.get("value") if isinstance(cool_warn, dict) else None
    if target_c:
        for addr_str, p in params.items():
            reg = register_map.get(int(addr_str), {})
            if "coolant" in reg.get("name", "").lower() and "temp" in reg.get("name", "").lower():
                if p.get("min") is not None and p["min"] < target_c:
                    notes.append(
                        f"Температура ОЖ опустилась до {p['min']}°C "
                        f"(ниже рекомендуемого минимума {target_c}°C для подогревателя)"
                    )

    return {
        "type": "standstill",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "duration_min": _dur(start, end),
        "label": f"Простой ({start.strftime('%H:%M')}–{end.strftime('%H:%M')})",
        "parameters": params,
        "notes": notes,
        "related_anomalies": [],
    }


# ── Анализ пуска ──────────────────────────────────────────────────────────────

def _analyze_startup(
    by_addr: dict[int, list[dict]],
    start: datetime,
    window_end: datetime,
    operation_rules: dict,
    register_map: dict[int, dict],
) -> list[str]:
    notes: list[str] = []
    ss = operation_rules.get("startup_sequence", {})

    # Время от пуска до выхода на номинальный режим (state=3 RatedFreqandVoltage)
    rated_rows = _rows_in_window(by_addr.get(_RUN_SEQUENCE_ADDR, []), start, window_end)
    rated_ts = next((r["ts"] for r in rated_rows if (r.get("raw") or 0) == 3), None)
    if rated_ts:
        elapsed = int((_ensure_tz(rated_ts) - start).total_seconds())
        crank_norm = ss.get("max_crank_time_sec", {})
        norm_sec = crank_norm.get("value") if isinstance(crank_norm, dict) else None
        if norm_sec and elapsed > norm_sec * 3:
            notes.append(
                f"Выход на номинальный режим занял {elapsed} сек "
                f"(ожидалось быстрее нескольких циклов по {norm_sec} сек)"
            )
        else:
            notes.append(f"Выход на номинальный режим: {elapsed} сек")

    # Появление давления масла
    lop_delay = ss.get("lop_enable_time_sec", {})
    lop_sec = lop_delay.get("value") if isinstance(lop_delay, dict) else 10
    min_press = ss.get("min_oil_pressure_after_start_kpa", {})
    min_press_val = min_press.get("value") if isinstance(min_press, dict) else 138

    oil_addr = _find_addr_by_keyword(register_map, ["oil", "pressure", "rifle"])
    if oil_addr and oil_addr in by_addr:
        press_rows = _rows_in_window(by_addr[oil_addr], start, window_end)
        first_ok = next(
            (r for r in press_rows if (r.get("value") or 0) >= min_press_val), None
        )
        if first_ok:
            press_delay = int((_ensure_tz(first_ok["ts"]) - start).total_seconds())
            if press_delay > (lop_sec or 10) + 5:
                notes.append(
                    f"Давление масла ≥{min_press_val} кПа появилось через {press_delay} сек "
                    f"(норматив мониторинга: {lop_sec} сек)"
                )
            else:
                notes.append(f"Давление масла вышло на норму за {press_delay} сек ✓")
        else:
            notes.append(f"Давление масла не достигло {min_press_val} кПа в окне пуска")

    return notes


# ── Анализ прогрева ───────────────────────────────────────────────────────────

def _analyze_warmup(
    by_addr: dict[int, list[dict]],
    start: datetime,
    warmup_end: datetime,
    operation_rules: dict,
    register_map: dict[int, dict],
) -> list[str]:
    notes: list[str] = []
    normal_op = operation_rules.get("normal_operation", {})

    # Температура ОЖ в конце прогрева
    coolant_addr = _find_addr_by_keyword(register_map, ["coolant", "temperature"])
    if coolant_addr and coolant_addr in by_addr:
        rows = _rows_in_window(by_addr[coolant_addr], start, warmup_end)
        if rows:
            t_start_val = rows[0].get("value")
            t_end_val = rows[-1].get("value")
            norm = normal_op.get("coolant_temperature_c", {}).get("normal", {})
            norm_min = norm.get("min")
            if t_start_val is not None and t_end_val is not None:
                notes.append(
                    f"ОЖ прогрелась: {t_start_val:.0f}°C → {t_end_val:.0f}°C"
                )
            if norm_min and t_end_val is not None and t_end_val < norm_min:
                notes.append(
                    f"К концу прогрева ОЖ {t_end_val:.0f}°C < нормы {norm_min}°C — "
                    "прогрев не завершён"
                )

    return notes


# ── Анализ останова ───────────────────────────────────────────────────────────

def _analyze_shutdown(
    by_addr: dict[int, list[dict]],
    stop_ts: datetime,
    operation_rules: dict,
    register_map: dict[int, dict],
) -> list[str]:
    notes: list[str] = []
    sd = operation_rules.get("shutdown_sequence", {})

    # Проверка наличия охлаждения (state=5 CooldownAtIdle перед остановом)
    pre_window = stop_ts - timedelta(minutes=_SHUTDOWN_PRE_MIN)
    seq_rows = _rows_in_window(
        by_addr.get(_RUN_SEQUENCE_ADDR, []), pre_window, stop_ts
    )
    had_cooldown = any((r.get("raw") or 0) in (4, 5) for r in seq_rows)

    rec_min_str = sd.get("normal_shutdown", {}).get("recommended_pre_stop_no_load_min", {})
    rec_min = rec_min_str.get("value", "3-5") if isinstance(rec_min_str, dict) else "3-5"

    if had_cooldown:
        notes.append(f"Останов с охлаждением ✓ (рекомендуется {rec_min} мин без нагрузки)")
    else:
        # Проверяем был ли это аварийный останов (state=7 EmergencyShutdown)
        was_emergency = any((r.get("raw") or 0) in (7,) for r in seq_rows)
        if was_emergency:
            notes.append("Аварийный останов без охлаждения")
        else:
            notes.append(
                f"Останов без охлаждения — рекомендуется {rec_min} мин на холостом ходу"
            )

    return notes


# ── Вспомогательные функции ───────────────────────────────────────────────────

def _agg_window(
    by_addr: dict[int, list[dict]],
    start: datetime,
    end: datetime,
    register_map: dict[int, dict],
) -> dict[str, Any]:
    """Агрегировать ключевые параметры за временное окно."""
    result: dict[str, Any] = {}
    for addr, rows in by_addr.items():
        reg = register_map.get(addr, {})
        unit = reg.get("unit", "")
        if unit not in _KEY_UNITS:
            continue

        window_rows = _rows_in_window(rows, start, end)
        na_set = set(reg.get("na_values", []))
        values = [
            float(r["value"]) for r in window_rows
            if r.get("value") is not None and float(r["value"]) not in na_set
        ]
        if len(values) < 2:
            continue

        result[str(addr)] = {
            "name": reg.get("name", f"reg_{addr}"),
            "unit": unit,
            "min": round(min(values), 2),
            "max": round(max(values), 2),
            "mean": round(sum(values) / len(values), 2),
            "count": len(values),
        }
    return result


def _rows_in_window(rows: list[dict], start: datetime, end: datetime) -> list[dict]:
    """Строки в диапазоне [start, end]."""
    result = []
    for r in rows:
        ts = _ensure_tz(r["ts"])
        if start <= ts <= end:
            result.append(r)
    return result


def _seg_anomalies(
    anomalies: list[dict],
    start: datetime,
    end: datetime,
) -> list[dict]:
    """Аномалии, которые попадают в окно сегмента."""
    result = []
    for a in anomalies:
        fs = a.get("first_seen")
        if not fs:
            continue
        try:
            a_ts = _parse_ts(fs)
            if start <= a_ts <= end:
                result.append({"name": a["name"], "severity": a.get("severity"), "type": a.get("type")})
        except (ValueError, TypeError):
            pass
    return result


def _find_addr_by_keyword(register_map: dict[int, dict], keywords: list[str]) -> int | None:
    """Найти адрес регистра по ключевым словам в имени."""
    for addr, reg in register_map.items():
        name = reg.get("name", "").lower()
        if all(kw in name for kw in keywords):
            return addr
    return None


def _extract_timing(operation_rules: dict) -> tuple[int, int]:
    """Извлечь warmup_min и cooldown_min из operation_rules."""
    ss = operation_rules.get("startup_sequence", {})
    load_ready = ss.get("time_to_load_ready_sec", {})
    load_sec = load_ready.get("value") if isinstance(load_ready, dict) else None
    warmup_min = max(5, int(load_sec / 60)) if isinstance(load_sec, (int, float)) else 10

    sd = operation_rules.get("shutdown_sequence", {}).get("normal_shutdown", {})
    rec = sd.get("recommended_pre_stop_no_load_min", {})
    rec_val = rec.get("value", "3-5") if isinstance(rec, dict) else "3-5"
    try:
        cooldown_min = int(str(rec_val).split("-")[0])
    except (ValueError, TypeError):
        cooldown_min = 3

    return warmup_min, cooldown_min


def _parse_ts(s: str | datetime) -> datetime:
    if isinstance(s, datetime):
        return _ensure_tz(s)
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return _ensure_tz(dt)


def _ensure_tz(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts


def _dur(start: datetime, end: datetime) -> int:
    return max(0, int((end - start).total_seconds() / 60))
