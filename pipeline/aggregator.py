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


def _calc_uptime(
    by_addr: dict[int, list[dict]],
    register_map: dict[int, dict],
) -> tuple[int, int, list[tuple[str, str]]]:
    """Определить наработку и количество пусков по статусным регистрам.

    Ищет регистр с именем содержащим 'Run Sequence State' (addr 40011 для PCC)
    или любой регистр с group='status'. Значение > 0 = работа.
    """
    # Найти статусный регистр (GensetRun Sequence State, addr 40011)
    status_addr = None
    for addr, reg in register_map.items():
        name = reg.get("name", "").lower()
        if "run sequence" in name or "gensetrun" in name:
            status_addr = addr
            break

    if status_addr is None or status_addr not in by_addr:
        return 0, 0, []

    rows = sorted(by_addr[status_addr], key=lambda r: r["ts"])

    intervals: list[tuple[str, str]] = []
    starts = 0
    run_start: datetime | None = None
    total_minutes = 0

    for r in rows:
        raw = r.get("raw", 0) or 0
        ts: datetime = r["ts"]
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        is_running = raw > 0  # 0=Stop, всё остальное — работа

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
