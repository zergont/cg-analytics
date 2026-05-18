"""Генерация графиков для agent tools. Возвращает PNG в base64."""
import base64
import io
from datetime import datetime, timezone
from typing import Any

import matplotlib
matplotlib.use("Agg")  # без GUI, для серверного использования
import matplotlib.pyplot as plt
import matplotlib.dates as mdates


def timeseries_chart(
    series: dict[str, list[tuple]],
    title: str = "",
    unit: str = "",
) -> str:
    """График временного ряда одного или нескольких параметров.

    Args:
        series: {label: [(ts, value), ...]}
        title: заголовок графика
        unit: единица измерения (для оси Y)

    Returns:
        base64-encoded PNG
    """
    fig, ax = plt.subplots(figsize=(12, 4))

    for label, points in series.items():
        if not points:
            continue
        timestamps = [_ensure_tz(p[0]) for p in points]
        values = [float(p[1]) for p in points if p[1] is not None]
        if len(timestamps) != len(values):
            timestamps = timestamps[:len(values)]
        ax.plot(timestamps, values, label=label, linewidth=1)

    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax.xaxis.set_major_locator(mdates.HourLocator(interval=2))
    fig.autofmt_xdate()

    if title:
        ax.set_title(title, fontsize=11)
    if unit:
        ax.set_ylabel(unit)
    if len(series) > 1:
        ax.legend(fontsize=8)

    ax.grid(True, alpha=0.3)
    plt.tight_layout()

    return _fig_to_b64(fig)


def correlation_chart(
    x_points: list[tuple],
    y_points: list[tuple],
    x_label: str = "X",
    y_label: str = "Y",
    title: str = "",
) -> str:
    """График корреляции двух параметров (scatter plot).

    Args:
        x_points: [(ts, value), ...]
        y_points: [(ts, value), ...] — выровнивается по времени с x_points

    Returns:
        base64-encoded PNG
    """
    # Сопоставить значения по ближайшему timestamp
    x_vals, y_vals = _align_series(x_points, y_points)

    fig, ax = plt.subplots(figsize=(7, 5))
    ax.scatter(x_vals, y_vals, alpha=0.5, s=10)

    ax.set_xlabel(x_label)
    ax.set_ylabel(y_label)
    if title:
        ax.set_title(title, fontsize=11)
    ax.grid(True, alpha=0.3)
    plt.tight_layout()

    return _fig_to_b64(fig)


def hourly_bar_chart(
    hourly: dict[int, float],
    title: str = "",
    unit: str = "",
) -> str:
    """Столбчатый график почасовых средних (24 часа)."""
    hours = list(range(24))
    values = [hourly.get(h, 0) for h in hours]

    fig, ax = plt.subplots(figsize=(12, 4))
    ax.bar(hours, values, color="steelblue", alpha=0.8)
    ax.set_xticks(hours)
    ax.set_xlabel("Час (UTC)")
    if unit:
        ax.set_ylabel(unit)
    if title:
        ax.set_title(title, fontsize=11)
    ax.grid(True, axis="y", alpha=0.3)
    plt.tight_layout()

    return _fig_to_b64(fig)


# ── helpers ───────────────────────────────────────────────────────────────────

def _fig_to_b64(fig: plt.Figure) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100)
    plt.close(fig)
    buf.seek(0)
    return base64.standard_b64encode(buf.read()).decode("ascii")


def _ensure_tz(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts


def _align_series(
    x_points: list[tuple],
    y_points: list[tuple],
    max_gap_seconds: int = 300,
) -> tuple[list[float], list[float]]:
    """Сопоставить два временных ряда по ближайшему timestamp."""
    if not x_points or not y_points:
        return [], []

    x_vals, y_vals = [], []
    y_sorted = sorted(y_points, key=lambda p: p[0])
    y_times = [p[0] for p in y_sorted]

    for x_ts, x_val in x_points:
        if x_val is None:
            continue
        # Бинарный поиск ближайшего y
        import bisect
        i = bisect.bisect_left(y_times, x_ts)
        candidates = []
        if i < len(y_sorted):
            candidates.append(y_sorted[i])
        if i > 0:
            candidates.append(y_sorted[i - 1])

        if not candidates:
            continue
        closest = min(candidates, key=lambda p: abs((p[0] - x_ts).total_seconds()))
        gap = abs((closest[0] - x_ts).total_seconds())

        if gap <= max_gap_seconds and closest[1] is not None:
            x_vals.append(float(x_val))
            y_vals.append(float(closest[1]))

    return x_vals, y_vals
