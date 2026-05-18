"""Выполнение инструментов агента. Получает RunContext, возвращает результат."""
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any, TYPE_CHECKING

from agent import charts

if TYPE_CHECKING:
    from pipeline.runner import RunContext


def execute_tool(
    tool_name: str,
    tool_input: dict[str, Any],
    ctx: "RunContext",
) -> Any:
    """Диспетчер инструментов. Возвращает строку или dict для Anthropic API."""
    match tool_name:
        case "get_timeseries_chart":
            return _get_timeseries_chart(tool_input, ctx)
        case "get_correlation_chart":
            return _get_correlation_chart(tool_input, ctx)
        case "get_aggregates":
            return _get_aggregates(tool_input, ctx)
        case "get_events":
            return _get_events(tool_input, ctx)
        case _:
            return f"Неизвестный инструмент: {tool_name}"


# ── Инструменты ───────────────────────────────────────────────────────────────

def _get_timeseries_chart(
    inp: dict[str, Any], ctx: "RunContext"
) -> list[dict]:
    """Возвращает PNG в base64 как image block для Anthropic API."""
    addrs = inp["addrs"]
    from_h = inp.get("from_hour", 0)
    to_h = inp.get("to_hour", 24)

    series = {}
    for addr in addrs:
        points = ctx.history_series.get(addr, [])
        filtered = _filter_by_hours(points, from_h, to_h, ctx.day)
        reg = ctx.register_map.get(addr, {})
        label = f"{reg.get('name', addr)} ({reg.get('unit', '')})"
        series[label] = [(p[0], p[1]) for p in filtered if p[1] is not None]

    if not any(series.values()):
        return [{"type": "text", "text": "Нет данных за указанный период."}]

    unit = ""
    if addrs and len(addrs) == 1:
        unit = ctx.register_map.get(addrs[0], {}).get("unit", "")

    b64 = charts.timeseries_chart(
        series=series,
        title=f"Период {from_h}:00–{to_h}:00 UTC",
        unit=unit,
    )

    return [
        {"type": "text", "text": f"График параметров {addrs} за {from_h}:00–{to_h}:00 UTC"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
    ]


def _get_correlation_chart(
    inp: dict[str, Any], ctx: "RunContext"
) -> list[dict]:
    addr_x = inp["addr_x"]
    addr_y = inp["addr_y"]

    x_pts = [(p[0], p[1]) for p in ctx.history_series.get(addr_x, []) if p[1] is not None]
    y_pts = [(p[0], p[1]) for p in ctx.history_series.get(addr_y, []) if p[1] is not None]

    if not x_pts or not y_pts:
        return [{"type": "text", "text": "Нет данных для одного из параметров."}]

    reg_x = ctx.register_map.get(addr_x, {})
    reg_y = ctx.register_map.get(addr_y, {})

    b64 = charts.correlation_chart(
        x_points=x_pts,
        y_points=y_pts,
        x_label=f"{reg_x.get('name', addr_x)} ({reg_x.get('unit', '')})",
        y_label=f"{reg_y.get('name', addr_y)} ({reg_y.get('unit', '')})",
        title=f"Корреляция: {reg_x.get('name', addr_x)} vs {reg_y.get('name', addr_y)}",
    )

    return [
        {"type": "text", "text": f"График корреляции: addr {addr_x} vs {addr_y}"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
    ]


def _get_aggregates(
    inp: dict[str, Any], ctx: "RunContext"
) -> str:
    addrs = inp["addrs"]
    from_h = inp.get("from_hour", 0)
    to_h = inp.get("to_hour", 24)

    result = []
    for addr in addrs:
        points = ctx.history_series.get(addr, [])
        filtered = _filter_by_hours(points, from_h, to_h, ctx.day)
        values = [float(p[1]) for p in filtered if p[1] is not None]

        reg = ctx.register_map.get(addr, {})
        na_set = set(reg.get("na_values", []))
        values = [v for v in values if v not in na_set]

        if not values:
            result.append({"addr": addr, "name": reg.get("name", f"reg_{addr}"), "error": "нет данных"})
            continue

        result.append({
            "addr": addr,
            "name": reg.get("name", f"reg_{addr}"),
            "unit": reg.get("unit", ""),
            "min": round(min(values), 4),
            "max": round(max(values), 4),
            "mean": round(statistics.mean(values), 4),
            "median": round(statistics.median(values), 4),
            "count": len(values),
            "period": f"{from_h}:00–{to_h}:00 UTC",
        })

    import json
    return json.dumps(result, ensure_ascii=False, indent=2)


def _get_events(
    inp: dict[str, Any], ctx: "RunContext"
) -> str:
    from_h = inp.get("from_hour", 0)
    to_h = inp.get("to_hour", 24)
    type_filter = inp.get("event_type_filter", "").lower()

    from datetime import timezone as tz
    day_start = datetime.combine(ctx.day, datetime.min.time()).replace(tzinfo=timezone.utc)
    t_from = day_start + timedelta(hours=from_h)
    t_to = day_start + timedelta(hours=to_h)

    filtered = []
    for ev in ctx.events:
        ev_ts = ev["created_at"]
        if ev_ts.tzinfo is None:
            ev_ts = ev_ts.replace(tzinfo=timezone.utc)
        if not (t_from <= ev_ts < t_to):
            continue
        if type_filter and type_filter not in (ev.get("type") or "").lower():
            continue
        filtered.append({
            "ts": ev_ts.isoformat(),
            "type": ev.get("type"),
            "description": ev.get("description"),
        })

    if not filtered:
        return "Событий в указанном периоде не найдено."

    import json
    return json.dumps(filtered, ensure_ascii=False, indent=2)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _filter_by_hours(
    points: list[tuple],
    from_h: int,
    to_h: int,
    day: Any,
) -> list[tuple]:
    """Фильтрация временного ряда по диапазону часов UTC."""
    from datetime import date as date_type
    day_start = datetime.combine(day, datetime.min.time()).replace(tzinfo=timezone.utc)
    t_from = day_start + timedelta(hours=from_h)
    t_to = day_start + timedelta(hours=to_h)

    result = []
    for p in points:
        ts = p[0]
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if t_from <= ts < t_to:
            result.append(p)
    return result
