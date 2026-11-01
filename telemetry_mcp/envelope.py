# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Конверт результата (design_mcp_telemetry.md, разд. 6.1, Р9).

У каждого ответа: метка [Tn], установка, инструмент, окно, слой-источник,
единицы, очистка (что отброшено и почему, разрывы связи в окне), усечение,
примечания. Модель не должна гадать, полные перед ней данные или нет.
Та же информация — структурой (для хоста) и строками текста (для головы).
"""
from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from analytics.labels import fmt_local, fmt_duration, tz_label

# Счётчики меток [Tn]: по ключу задачи (task_id хоста), иначе общий на процесс.
_counters: dict[str, "itertools.count[int]"] = {}
_lock = threading.Lock()


def next_label(key: str | None) -> str:
    k = key or "_"
    with _lock:
        c = _counters.get(k)
        if c is None:
            c = _counters[k] = itertools.count(1)
        return f"T{next(c)}"


def reset_labels() -> None:
    with _lock:
        _counters.clear()


def fmt_window(t_from: datetime | None, t_to: datetime | None) -> str:
    """«13.10.2026 12:00:00–13:30:00 МСК» (вторая дата — только если другой день)."""
    if t_from is None and t_to is None:
        return ""
    tzl = tz_label()
    if t_from is not None and t_to is not None and t_from == t_to:
        return f"момент {fmt_local(t_from, day=True)[:5]}.{_year(t_from)} {fmt_local(t_from)} {tzl}"
    if t_from is None:
        return f"до {fmt_local(t_to, day=True)} {tzl}"
    if t_to is None:
        return f"с {fmt_local(t_from, day=True)} {tzl}"
    a = fmt_local(t_from, day=True) or ""
    same_day = (fmt_local(t_from, day=True) or "")[:5] == (fmt_local(t_to, day=True) or "")[:5]
    b = fmt_local(t_to, day=not same_day) or ""
    year = _year(t_from)
    return f"{a[:5]}.{year} {a[6:]}–{b} {tzl}"


def _year(ts: datetime) -> int:
    from analytics.labels import _local_tz
    z = _local_tz()
    return (ts.astimezone(z) if z is not None and ts.tzinfo else ts).year


@dataclass
class Envelope:
    label: str
    tool: str
    unit: str                                  # «ДЭС №3 (установка 7)»
    unit_id: int | None = None
    window_from: datetime | None = None
    window_to: datetime | None = None
    layer: str = ""                            # «очищенный слой (удержание по пакетам)»
    units: dict[str, str] = field(default_factory=dict)   # колонка → единица
    dropped: dict[str, int] = field(default_factory=dict) # причина → строк
    gaps: list[tuple[datetime, datetime | None]] = field(default_factory=list)
    truncation: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    rows: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label, "tool": self.tool, "unit": self.unit,
            "window": {"from": self.window_from.isoformat() if self.window_from else None,
                       "to": self.window_to.isoformat() if self.window_to else None},
            "layer": self.layer, "units": self.units, "dropped": self.dropped,
            "gaps": [{"from": a.isoformat(), "to": b.isoformat() if b else None} for a, b in self.gaps],
            "truncation": self.truncation, "notes": self.notes, "rows": self.rows,
        }

    def text(self) -> str:
        head = f"[{self.label}] {self.unit} · {self.tool}"
        w = fmt_window(self.window_from, self.window_to)
        if w:
            head += f" · {w}"
        if self.layer:
            head += f" · слой: {self.layer}"
        lines = [head]
        if self.units:
            lines.append("единицы: " + " · ".join(f"{k} {v}" for k, v in self.units.items() if v))
        clean: list[str] = []
        dropped = {k: v for k, v in self.dropped.items() if v}
        if dropped:
            total = sum(dropped.values())
            clean.append(f"отброшено {total} строк (" + ", ".join(
                f"{_DROP_RU.get(k, k)} {v}" for k, v in dropped.items()) + ")")
        if self.gaps:
            parts = []
            for a, b in self.gaps[:6]:
                dur = fmt_duration((b - a).total_seconds()) if b else "продолжается"
                parts.append(f"{fmt_local(a)}–{fmt_local(b) if b else '…'} ({dur})")
            more = f" и ещё {len(self.gaps) - 6}" if len(self.gaps) > 6 else ""
            clean.append(f"разрывы связи в окне: {'; '.join(parts)}{more}")
        if clean:
            lines.append("очистка: " + "; ".join(clean))
        elif self.window_from is not None or self.window_to is not None:
            lines.append("очистка: строк не отброшено, разрывов связи в окне нет")
        if self.truncation:
            lines.append("усечение: " + "; ".join(self.truncation))
        if self.notes:
            lines.append("примечания: " + "; ".join(self.notes))
        return "\n".join(lines)


_DROP_RU = {"future": "метка в будущем", "frozen": "застывшие часы", "dups": "дубли",
            "late": "поздние"}
