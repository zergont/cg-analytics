"""Детерминированное детектирование отклонений (без ИИ).

Выполняется до обращения к агенту. Формирует структурированный список
аномалий, который передаётся агенту как входной контекст.
"""
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any


def detect(
    history: list[dict[str, Any]],
    events: list[dict[str, Any]],
    register_map: dict[int, dict],
    fault_bitmap_map: dict[int, list[dict]],
    aggregates: dict[str, Any],
) -> list[dict[str, Any]]:
    """Найти все аномалии за сутки.

    Returns:
        Список аномалий, каждая:
        {
            "type": "fault_bit" | "threshold" | "na_value",
            "severity": "shutdown" | "derate" | "warning" | "info",
            "addr": int,
            "bit": int | None,
            "name": str,
            "description": str,
            "episodes": int,         # количество эпизодов
            "duration_min": int,     # суммарная длительность в минутах
            "first_seen": str,       # ISO timestamp
            "last_seen": str,
            "related_events": [...]  # события ±15 мин
        }
    """
    anomalies: list[dict[str, Any]] = []

    anomalies.extend(_detect_fault_bits(history, fault_bitmap_map, events))
    anomalies.extend(_detect_thresholds(aggregates, register_map, events))
    anomalies.extend(_detect_na_values(history, register_map, events))

    # Сортировка: сначала критичные, потом по времени первого появления
    severity_order = {"shutdown": 0, "derate": 1, "warning": 2, "info": 3}
    anomalies.sort(key=lambda a: (
        severity_order.get(a.get("severity", "info"), 99),
        a.get("first_seen", "")
    ))

    return anomalies


def _detect_fault_bits(
    history: list[dict[str, Any]],
    fault_bitmap_map: dict[int, list[dict]],
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Активные биты в fault-bitmap регистрах."""
    anomalies = []

    # Группируем историю по addr
    by_addr: dict[int, list[dict]] = defaultdict(list)
    for row in history:
        by_addr[row["addr"]].append(row)

    for addr, bit_defs in fault_bitmap_map.items():
        if addr not in by_addr:
            continue

        rows = sorted(by_addr[addr], key=lambda r: r["ts"])

        for bit_def in bit_defs:
            bit_n = bit_def["bit"]
            mask = 1 << bit_n

            # Найти интервалы когда бит был установлен
            episodes = []
            episode_start: datetime | None = None

            for r in rows:
                raw = r.get("raw", 0) or 0
                ts = _ensure_tz(r["ts"])
                bit_active = bool(raw & mask)

                if bit_active and episode_start is None:
                    episode_start = ts
                elif not bit_active and episode_start is not None:
                    episodes.append((episode_start, ts))
                    episode_start = None

            if episode_start is not None and rows:
                episodes.append((episode_start, _ensure_tz(rows[-1]["ts"])))

            if not episodes:
                continue

            duration = sum(
                int((e - s).total_seconds() / 60) for s, e in episodes
            )
            first_seen = episodes[0][0]
            last_seen = episodes[-1][1]

            anomalies.append({
                "type": "fault_bit",
                "severity": bit_def.get("severity", "warning"),
                "addr": addr,
                "bit": bit_n,
                "name": bit_def["name"],
                "description": bit_def.get("description", ""),
                "episodes": len(episodes),
                "duration_min": duration,
                "first_seen": first_seen.isoformat(),
                "last_seen": last_seen.isoformat(),
                "related_events": _find_related_events(events, first_seen),
            })

    return anomalies


def _detect_thresholds(
    aggregates: dict[str, Any],
    register_map: dict[int, dict],
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Превышение порогов threshold_warn / threshold_crit.

    Пороги опциональны — появятся после добавления в register_map
    из руководства по эксплуатации.
    """
    anomalies = []
    by_register = aggregates.get("by_register", {})

    for addr_str, agg in by_register.items():
        addr = int(addr_str)
        reg = register_map.get(addr, {})

        warn = reg.get("threshold_warn")
        crit = reg.get("threshold_crit")

        if warn is None and crit is None:
            continue

        max_val = agg.get("max")
        if max_val is None:
            continue

        if crit is not None and max_val > crit:
            severity = "shutdown"
        elif warn is not None and max_val > warn:
            severity = "warning"
        else:
            continue

        anomalies.append({
            "type": "threshold",
            "severity": severity,
            "addr": addr,
            "bit": None,
            "name": agg.get("name", f"reg_{addr}"),
            "description": (
                f"Максимум {max_val} {agg.get('unit', '')} "
                f"превысил порог {'критичный' if severity == 'shutdown' else 'предупреждение'} "
                f"({crit if severity == 'shutdown' else warn})"
            ),
            "episodes": 1,
            "duration_min": 0,
            "first_seen": "",
            "last_seen": "",
            "related_events": [],
        })

    return anomalies


def _detect_na_values(
    history: list[dict[str, Any]],
    register_map: dict[int, dict],
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Регистры, которые постоянно возвращали N/A (недействительное значение)."""
    anomalies = []

    by_addr: dict[int, list[dict]] = defaultdict(list)
    for row in history:
        by_addr[row["addr"]].append(row)

    for addr, rows in by_addr.items():
        reg = register_map.get(addr, {})
        na_set = set(reg.get("na_values", []))
        if not na_set:
            continue

        na_count = sum(
            1 for r in rows if r.get("raw") in na_set or r.get("value") in na_set
        )
        total = len(rows)

        # Считаем аномалией если >50% значений — N/A
        if total < 5 or na_count / total < 0.5:
            continue

        first_ts = _ensure_tz(rows[0]["ts"])

        anomalies.append({
            "type": "na_value",
            "severity": "warning",
            "addr": addr,
            "bit": None,
            "name": reg.get("name", f"reg_{addr}"),
            "description": (
                f"Регистр возвращал N/A в {na_count} из {total} измерений — "
                "возможна неисправность датчика или разрыв связи"
            ),
            "episodes": na_count,
            "duration_min": 0,
            "first_seen": first_ts.isoformat(),
            "last_seen": _ensure_tz(rows[-1]["ts"]).isoformat(),
            "related_events": _find_related_events(events, first_ts),
        })

    return anomalies


def _find_related_events(
    events: list[dict[str, Any]],
    ts: datetime,
    window_minutes: int = 15,
) -> list[dict[str, Any]]:
    """События в окне ±window_minutes минут вокруг timestamps."""
    from datetime import timedelta
    window = timedelta(minutes=window_minutes)
    result = []
    for ev in events:
        ev_ts = _ensure_tz(ev["created_at"])
        if abs((ev_ts - ts).total_seconds()) <= window.total_seconds():
            result.append({
                "type": ev.get("type"),
                "description": ev.get("description"),
                "ts": ev_ts.isoformat(),
            })
    return result


def _ensure_tz(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts
