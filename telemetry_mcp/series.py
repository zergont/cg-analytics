# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Ряды для модели: удержание значения до следующей записи и укрупнение шага.

Запись в history идёт по изменению (и «пульсом»): значение действует до
следующей записи. Поэтому ряд для модели — ступенчатая функция, а при укрупнении
на точку — минимум, среднее ПО ВРЕМЕНИ и максимум (среднее по отсчётам смещено:
отсчёты кучкуются там, где значение менялось). Внутри разрыва связи значение
неизвестно — None. Чистые функции без БД (тесты scripts/test_mcp_e1.py).
"""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

Point = tuple[datetime, float]

_NICE_STEPS = (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800,
               21600, 43200, 86400)


def nice_step(sec: float) -> int:
    """Ближайший «круглый» шаг не меньше sec."""
    for s in _NICE_STEPS:
        if s >= sec:
            return s
    return int(math.ceil(sec / 86400.0)) * 86400


def held_points(rows: list[dict[str, Any]], addr: int) -> list[Point]:
    """Точки регистра из строк истории (addr, ts, value), по времени, без None и
    без строк удержания движка (is_carried_forward)."""
    pts: list[Point] = []
    for r in rows:
        if r.get("addr") != addr or r.get("value") is None or r.get("is_carried_forward"):
            continue
        try:
            pts.append((r["ts"], float(r["value"])))
        except (TypeError, ValueError):
            continue
    pts.sort(key=lambda p: p[0])
    return pts


def in_gap(t: datetime, gaps: list[tuple[datetime, datetime | None]]) -> bool:
    for a, b in gaps:
        if a <= t and (b is None or t < b):
            return True
    return False


class Stepper:
    """Ступенчатая функция по точкам: значение в t — последняя точка не позже t."""

    def __init__(self, pts: list[Point], gaps: list[tuple[datetime, datetime | None]] | None = None):
        self.pts = pts
        self.ts = [p[0] for p in pts]
        self.gaps = gaps or []

    def value_at(self, t: datetime) -> float | None:
        i = bisect.bisect_right(self.ts, t) - 1
        if i < 0 or in_gap(t, self.gaps):
            return None
        # значение, записанное до разрыва, через разрыв не переносится
        last_ts = self.ts[i]
        for a, b in self.gaps:
            if last_ts < a and (b is None or b <= t):
                return None
        return self.pts[i][1]

    def bucket(self, a: datetime, b: datetime) -> tuple[float, float, float] | None:
        """(min, среднее по времени, max) на [a, b) — ступенчато; None — неизвестно."""
        v0 = self.value_at(a)
        lo = bisect.bisect_left(self.ts, a)
        hi = bisect.bisect_left(self.ts, b)
        inside = self.pts[lo:hi]
        vals = [v for v in [v0] + [p[1] for p in inside] if v is not None]
        if not vals:
            return None
        mn, mx = min(vals), max(vals)
        # интеграл ступенчатой функции
        area, known = 0.0, 0.0
        cur_t, cur_v = a, v0
        for t, v in inside:
            if cur_v is not None and not in_gap(cur_t, self.gaps):
                d = (t - cur_t).total_seconds()
                area += cur_v * d
                known += d
            cur_t, cur_v = t, v
        if cur_v is not None and not in_gap(cur_t, self.gaps):
            d = (b - cur_t).total_seconds()
            area += cur_v * d
            known += d
        avg = area / known if known > 0 else (sum(vals) / len(vals))
        return mn, avg, mx


@dataclass
class Resampled:
    mode: str                                  # raw | bucket
    step_sec: int                              # 0 — raw
    times: list[datetime]
    cells: dict[str, list[Any]]                # key → по времени: value | (min,avg,max) | None
    summary: dict[str, dict[str, Any]] = field(default_factory=dict)
    n_raw: dict[str, int] = field(default_factory=dict)


def resample(series: dict[str, list[Point]], t0: datetime, t1: datetime, n_points: int,
             gaps: list[tuple[datetime, datetime | None]] | None = None) -> Resampled:
    """Ряды на общей сетке. Если различных меток в окне не больше n_points —
    сырые метки (объединение по всем рядам, значения удержаны); иначе — сетка с
    круглым шагом, в ячейке (min, среднее по времени, max)."""
    gaps = gaps or []
    steppers = {k: Stepper(pts, gaps) for k, pts in series.items()}
    stamps: set[datetime] = set()
    for pts in series.values():
        stamps.update(t for t, _ in pts if t0 <= t < t1)
    summary = {k: _summary(pts, t0, t1) for k, pts in series.items()}
    n_raw = {k: sum(1 for t, _ in pts if t0 <= t < t1) for k, pts in series.items()}
    if len(stamps) <= max(1, n_points):
        times = sorted(stamps)
        cells = {k: [st.value_at(t) for t in times] for k, st in steppers.items()}
        return Resampled("raw", 0, times, cells, summary, n_raw)
    span = (t1 - t0).total_seconds()
    step = nice_step(span / max(1, n_points))
    times: list[datetime] = []
    t = t0
    while t < t1:
        times.append(t)
        t = t + timedelta(seconds=step)
    cells = {}
    for k, st in steppers.items():
        col = []
        for i, a in enumerate(times):
            b = times[i + 1] if i + 1 < len(times) else t1
            col.append(st.bucket(a, b))
        cells[k] = col
    return Resampled("bucket", step, times, cells, summary, n_raw)


def _summary(pts: list[Point], t0: datetime, t1: datetime) -> dict[str, Any]:
    win = [(t, v) for t, v in pts if t0 <= t < t1]
    if not win:
        return {}
    mn = min(win, key=lambda p: p[1])
    mx = max(win, key=lambda p: p[1])
    return {"first": win[0], "last": win[-1], "min": mn, "max": mx, "n": len(win)}
