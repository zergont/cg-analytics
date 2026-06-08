# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Агрегация телеметрии за сутки по каждому регистру."""
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any


def aggregate(
    history: list[dict[str, Any]],
    register_map: dict[int, dict],
) -> dict[str, Any]:
    """Вычислить агрегаты и почасовые срезы из сырой истории.

    Args:
        history: строки из history (addr, ts, value, raw, text, write_reason)
        register_map: карта регистров, ключ — addr

    Returns:
        {
            "by_register": {addr: {min, max, mean, median, std, count, hourly, last_text}},
            "uptime_minutes": int,
            "starts_count": int,
            "operating_intervals": [(start_iso, end_iso), ...]
        }
    """
    # Группировка по адресу регистра
    by_addr: dict[int, list[dict]] = defaultdict(list)
    for row in history:
        by_addr[row["addr"]].append(row)

    by_register: dict[str, Any] = {}

    for addr, rows in by_addr.items():
        reg = register_map.get(addr, {})
        na_values = set(reg.get("na_values", []))

        # Отфильтровать N/A значения
        values = [
            float(r["value"]) for r in rows
            if r["value"] is not None and float(r["value"]) not in na_values
        ]

        if not values:
            continue

        # Почасовые средние (24 бакета, индекс = час UTC)
        hourly: dict[int, list[float]] = defaultdict(list)
        for r in rows:
            if r["value"] is not None and float(r["value"]) not in na_values:
                ts: datetime = r["ts"]
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                hourly[ts.hour].append(float(r["value"]))

        hourly_avg = {h: round(sum(v) / len(v), 4) for h, v in hourly.items()}

        # Последнее текстовое значение (для enum-регистров)
        last_text = next(
            (r["text"] for r in reversed(rows) if r.get("text")), None
        )

        by_register[str(addr)] = {
            "addr": addr,
            "name": reg.get("name", f"reg_{addr}"),
            "unit": reg.get("unit", ""),
            "min": round(min(values), 4),
            "max": round(max(values), 4),
            "mean": round(statistics.mean(values), 4),
            "median": round(statistics.median(values), 4),
            "std": round(statistics.stdev(values), 4) if len(values) > 1 else 0.0,
            "count": len(values),
            "hourly": hourly_avg,
            "last_text": last_text,
        }

    uptime_minutes, starts_count, intervals = _calc_uptime(by_addr, register_map)

    return {
        "by_register": by_register,
        "uptime_minutes": uptime_minutes,
        "starts_count": starts_count,
        "operating_intervals": intervals,
    }


def calc_uptime_from_state_events(
    state_events: list[dict],
    run_seq_addr: int = 40011,
) -> tuple[int, int, list[tuple[str, str]]]:
    """Наработка и пуски из state_events по RunSequenceState (addr=40011).

    Значение raw=0 → стоп, raw>0 → работа.
    Возвращает (uptime_minutes, starts_count, intervals_iso).
    """
    rows = sorted(
        [e for e in state_events if e.get("addr") == run_seq_addr],
        key=lambda r: r["ts"] if r["ts"].tzinfo else r["ts"].replace(tzinfo=timezone.utc),
    )

    if not rows:
        return 0, 0, []

    intervals: list[tuple[str, str]] = []
    starts = 0
    run_start: datetime | None = None
    total_minutes = 0

    # Определяем начальное состояние: если первое событие — стоп,
    # значит машина работала с начала суток
    first_raw = int(rows[0].get("raw", 0) or 0)
    if first_raw == 0:
        # первое событие — останов, значит работа началась до начала суток
        first_ts = rows[0]["ts"]
        if first_ts.tzinfo is None:
            first_ts = first_ts.replace(tzinfo=timezone.utc)
        run_start = first_ts.replace(hour=0, minute=0, second=0, microsecond=0)
        starts += 1

    for r in rows:
        raw = int(r.get("raw", 0) or 0)
        ts: datetime = r["ts"]
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        is_running = raw > 0

        if is_running and run_start is None:
            run_start = ts
            starts += 1
        elif not is_running and run_start is not None:
            delta = int((ts - run_start).total_seconds() / 60)
            total_minutes += delta
            intervals.append((run_start.isoformat(), ts.isoformat()))
            run_start = None

    # Если сутки закончились в работе
    if run_start is not None and rows:
        last_ts = rows[-1]["ts"]
        if last_ts.tzinfo is None:
            last_ts = last_ts.replace(tzinfo=timezone.utc)
        delta = int((last_ts - run_start).total_seconds() / 60)
        total_minutes += delta
        intervals.append((run_start.isoformat(), last_ts.isoformat()))

    return total_minutes, starts, intervals


def _calc_uptime(
    by_addr: dict[int, list[dict]],
    register_map: dict[int, dict],
) -> tuple[int, int, list[tuple[str, str]]]:
    """Устаревший метод: 40011 теперь в state_events, а не в history.
    Оставлен как заглушка — возвращает нули."""
    return 0, 0, []


# ── Агрегация для произвольного диапазона ─────────────────────────────────────

def compute_register_stats(
    history: list[dict[str, Any]],
    ts_to: datetime,
) -> dict[int, dict[str, Any]]:
    """Вычислить мин/макс/взвешенное-среднее по каждому регистру за диапазон.

    Args:
        history: строки из history_rich (addr, ts, value, name_ru, unit)
        ts_to:   правая граница диапазона (UTC) — нужна для веса последнего значения

    Returns:
        {addr: {name, unit, min, max, wmean, count, first_ts, last_ts}}
    """
    by_addr: dict[int, list[tuple[datetime, float]]] = defaultdict(list)
    meta: dict[int, dict] = {}

    for row in history:
        if row["value"] is None:
            continue
        try:
            v = float(row["value"])
        except (TypeError, ValueError):
            continue
        addr = row["addr"]
        by_addr[addr].append((row["ts"], v))
        if addr not in meta:
            meta[addr] = {
                "name": row.get("name_ru") or "",
                "unit": row.get("unit") or "",
            }

    result: dict[int, dict[str, Any]] = {}
    for addr, readings in by_addr.items():
        readings.sort(key=lambda x: x[0])
        values = [v for _, v in readings]

        # Взвешенное среднее: каждое значение весится длительностью до следующего
        weighted_sum = 0.0
        total_w = 0.0
        for i, (ts, v) in enumerate(readings):
            next_ts = readings[i + 1][0] if i + 1 < len(readings) else ts_to
            dur = (next_ts - ts).total_seconds()
            if dur > 0:
                weighted_sum += v * dur
                total_w += dur

        wmean = round(weighted_sum / total_w, 2) if total_w > 0 else values[-1]

        result[addr] = {
            "name": meta[addr]["name"],
            "unit": meta[addr]["unit"],
            "min":  round(min(values), 3),
            "max":  round(max(values), 3),
            "wmean": wmean,
            "count": len(readings),
            "first_ts": readings[0][0],
            "last_ts":  readings[-1][0],
        }

    return result
