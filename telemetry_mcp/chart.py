# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Картинка ряда для инструмента `chart` (v5.2.5, Э2а; решение Р17).

Форму ряда картинка передаёт лучше таблицы (раскачка, провал, выброс), но числа
с неё модель читает плохо — поэтому картинка идёт только вместе с числовой
сводкой того же вызова. Здесь — чистый рендер без БД: панель на параметр, общая
ось времени, своя установка сплошной линией, соседи пунктиром, разрывы связи
штриховкой, номинал точечной линией. Значение держится до следующей записи —
линии ступенчатые; при укрупнении — линия среднего по времени и полоса мин–макс.

matplotlib — объектный API (Figure + Agg), без pyplot: рендер идёт в потоке.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

WIDTH_PX = 1200
DPI = 100
PANEL_IN = 2.3           # высота панели, дюймы
HEAD_IN = 0.9            # заголовок + ось времени
OWN_COLOR = "#1f77b4"
NEIGHBOR_COLORS = ("#d62728", "#2ca02c", "#9467bd", "#8c564b")


@dataclass
class Line:
    name: str                               # имя установки
    neighbor: bool
    cells: list[Any]                        # по сетке времени: число | (мин, ср, макс) | None


@dataclass
class Panel:
    label: str                              # «Частота генератора»
    unit: str                               # «Гц»
    lines: list[Line] = field(default_factory=list)
    nominal: float | None = None


def panels_from(cols: list[dict[str, Any]], cells: dict[str, list[Any]],
                nominals: dict[str, float] | None = None) -> list[Panel]:
    """Колонки сбора (gather_series) → панели по параметрам в порядке запроса:
    своя установка первой, соседи следом. nominals — {параметр: номинал}."""
    nominals = nominals or {}
    by_param: dict[str, Panel] = {}
    for c in cols:
        p = by_param.get(c["param"])
        if p is None:
            p = by_param[c["param"]] = Panel(label=c["param"], unit=c.get("unit") or "",
                                             nominal=nominals.get(c["param"]))
        p.lines.append(Line(name=c["unit_name"], neighbor=bool(c.get("neighbor")),
                            cells=cells.get(c["key"]) or []))
    for p in by_param.values():
        p.lines.sort(key=lambda ln: ln.neighbor)
    return list(by_param.values())


def has_data(panels: list[Panel]) -> bool:
    return any(c is not None for p in panels for ln in p.lines for c in ln.cells)


def _split(cells: list[Any]) -> tuple[list[float], list[float], list[float], bool]:
    """Ячейки → (мин, среднее, макс) списками с nan на пустых; band — есть ли полоса."""
    nan = float("nan")
    lo, mid, hi = [], [], []
    band = False
    for c in cells:
        if c is None:
            lo.append(nan), mid.append(nan), hi.append(nan)
        elif isinstance(c, tuple):
            mn, av, mx = (float(x) for x in c)
            lo.append(mn), mid.append(av), hi.append(mx)
            band = band or (mx - mn) > 1e-9
        else:
            v = float(c)
            lo.append(v), mid.append(v), hi.append(v)
    return lo, mid, hi, band


def render_png(panels: list[Panel], times: list[datetime], t0: datetime, t1: datetime, *,
               title: str, gaps: list[tuple[datetime, datetime | None]] | None = None,
               tz: Any = None, multi_day: bool = False) -> bytes:
    """PNG шириной 1200 px: панель на параметр, общая ось времени в поясе tz."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    import matplotlib.dates as mdates
    import math

    n = max(1, len(panels))
    fig = Figure(figsize=(WIDTH_PX / DPI, HEAD_IN + PANEL_IN * n), dpi=DPI)
    FigureCanvasAgg(fig)
    axes = fig.subplots(n, 1, sharex=True, squeeze=False)
    gaps = gaps or []
    # Ступенчатая линия тянется до конца окна: последнее значение держится
    xs = list(times) + [t1]

    for i, p in enumerate(panels):
        ax = axes[i][0]
        for a, b in gaps:
            ax.axvspan(max(a, t0), min(b or t1, t1), facecolor="0.88", edgecolor="0.6",
                       hatch="//", linewidth=0, zorder=0)
        nb = 0
        vmin, vmax = math.inf, -math.inf
        for ln in p.lines:
            lo, mid, hi, band = _split(ln.cells)
            if not mid:
                continue
            color = NEIGHBOR_COLORS[nb % len(NEIGHBOR_COLORS)] if ln.neighbor else OWN_COLOR
            nb += 1 if ln.neighbor else 0
            ys = mid + [mid[-1]]
            ax.plot(xs[:len(ys)], ys, drawstyle="steps-post", color=color, linewidth=1.4,
                    linestyle="--" if ln.neighbor else "-", label=ln.name, zorder=3)
            if band:
                ax.fill_between(xs[:len(ys)], lo + [lo[-1]], hi + [hi[-1]], step="post",
                                color=color, alpha=0.18, linewidth=0, zorder=2)
            finite = [v for v in lo + hi if v == v]
            if finite:
                vmin, vmax = min(vmin, min(finite)), max(vmax, max(finite))
        if p.nominal is not None and vmin <= vmax:
            span = max(vmax - vmin, abs(p.nominal) * 0.02, 1e-9)
            # Номинал рисуем, только если он рядом с данными — иначе он сожмёт шкалу
            if vmin - 0.5 * span <= p.nominal <= vmax + 0.5 * span:
                ax.axhline(p.nominal, color="0.35", linestyle=":", linewidth=1.0, zorder=1)
                ax.annotate(f"номинал {p.nominal:g}", xy=(1.0, p.nominal), xycoords=("axes fraction", "data"),
                            xytext=(-4, 3), textcoords="offset points", ha="right", va="bottom",
                            fontsize=8, color="0.3")
        ax.set_ylabel(f"{p.label}, {p.unit}" if p.unit else p.label, fontsize=9)
        ax.grid(True, color="0.85", linewidth=0.6)
        ax.tick_params(labelsize=8)
        ax.margins(y=0.08)
        if len(p.lines) > 1:
            ax.legend(fontsize=8, loc="best", framealpha=0.85)
        if not any(c is not None for ln in p.lines for c in ln.cells):
            ax.text(0.5, 0.5, "записей нет", transform=ax.transAxes, ha="center", va="center",
                    fontsize=10, color="0.4")

    last = axes[-1][0]
    last.set_xlim(t0, t1)
    loc = mdates.AutoDateLocator(tz=tz, minticks=5, maxticks=12)
    span = t1 - t0
    if multi_day or span > timedelta(hours=30):
        fmt = "%d.%m %H:%M" if span <= timedelta(days=4) else "%d.%m"
    else:
        fmt = "%H:%M:%S" if span <= timedelta(minutes=20) else "%H:%M"
    last.xaxis.set_major_locator(loc)
    last.xaxis.set_major_formatter(mdates.DateFormatter(fmt, tz=tz))
    if gaps:
        from matplotlib.patches import Patch
        axes[0][0].legend(handles=axes[0][0].get_legend_handles_labels()[0]
                          + [Patch(facecolor="0.88", edgecolor="0.6", hatch="//", label="нет связи")],
                          fontsize=8, loc="best", framealpha=0.85)
    fig.suptitle(title, fontsize=10, x=0.01, ha="left")
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=DPI)
    return buf.getvalue()


def warm_up() -> None:
    """Первый рендер строит кэш шрифтов matplotlib (секунды): делаем его при старте
    сервиса в фоне, чтобы первый вызов chart не упёрся в срок вызова."""
    from datetime import timezone
    t = datetime(2026, 1, 1, tzinfo=timezone.utc)
    render_png([Panel("прогрев", "", [Line("a", False, [0.0, 1.0])])],
               [t, t + timedelta(seconds=1)], t, t + timedelta(seconds=2), title="прогрев")


def png_size(data: bytes) -> tuple[int, int]:
    """(ширина, высота) PNG из заголовка IHDR."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("не PNG")
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
