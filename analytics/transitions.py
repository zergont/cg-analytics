# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Контекст проверок перехода: цепочка закрытых сегментов перед окном (v5.1.10).

Онлайн-окно начинается с головы текущего сегмента, и проверки перехода
(охлаждение перед остановом, прогрев перед работой) не видели, что было до
неё: нарушение жило один цикл и в данные закрытого сегмента не попадало
(за 30 дней — 1 случай из 13). Теперь сегментатор получает «звенья» —
облегчённые закрытые сегменты от последнего устойчивого режима до курсора.

Звено строится одной функцией из сегмента в памяти и из строки БД, поэтому
контекст после перезапуска тот же, что до него. Цепочка обязана стыковаться
(≤ tol с между концом звена и началом следующего) — иначе проверка на стыке
не делается (коррекция часов ДЭС №3 09.10, поздние enum-периоды).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

# Устойчивые режимы — якоря цепочки: стоп и работа
_ANCHORS = (0, 3)


@dataclass(frozen=True)
class Link:
    """Звено цепочки — закрытый сегмент без тяжёлых данных."""
    run_state: int | None
    t_start: datetime
    t_end: datetime
    cause_open: str | None = None
    load_pct_max: float | None = None   # максимум LOAD_PCT по подсегментам
    coolant_start: float | None = None  # Т ОЖ на начало сегмента

    @property
    def duration_sec(self) -> float:
        return max(0.0, (self.t_end - self.t_start).total_seconds())


@dataclass(frozen=True)
class Chain:
    """Цепочка перед головой: якорь, переходные звенья и куски той же работы."""
    anchor: Link
    between: tuple[Link, ...]       # от якоря (не включая) до головы, по времени
    run_pieces: tuple[Link, ...]    # якорь-работа и подряд идущие куски RS=3 до него
    synthetic_anchor: bool = False  # начало пусковой последовательности без стопа в данных


def _dt(v: Any) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        dt = v
    else:
        try:
            dt = datetime.fromisoformat(str(v))
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _num(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _load_max(subs: Iterable[Any]) -> float | None:
    vals = []
    for sub in subs or []:
        ch = sub.get("characteristics") if isinstance(sub, dict) else getattr(sub, "characteristics", None)
        lp = (ch or {}).get("LOAD_PCT") if isinstance(ch, dict) else None
        v = _num((lp or {}).get("max")) if isinstance(lp, dict) else None
        if v is not None:
            vals.append(v)
    return max(vals) if vals else None


def _coolant_start(subs: list[Any]) -> float | None:
    if not subs:
        return None
    s0 = subs[0]
    ch = s0.get("characteristics") if isinstance(s0, dict) else getattr(s0, "characteristics", None)
    ct = (ch or {}).get("COOLANT_TEMP") if isinstance(ch, dict) else None
    return _num((ct or {}).get("value_start")) if isinstance(ct, dict) else None


def link_from_segment(seg: Any) -> Link | None:
    """Звено из Segment сегментатора (или словаря того же вида)."""
    get = (lambda k: seg.get(k)) if isinstance(seg, dict) else (lambda k: getattr(seg, k, None))
    t0, t1 = _dt(get("t_start")), _dt(get("t_end"))
    if t0 is None or t1 is None:
        return None
    subs = list(get("subsegments") or [])
    rs = get("run_state")
    return Link(
        run_state=int(rs) if rs is not None else None,
        t_start=t0, t_end=t1,
        cause_open=get("cause_open"),
        load_pct_max=_load_max(subs),
        coolant_start=_coolant_start(subs),
    )


def link_from_row(row: dict) -> Link | None:
    """Звено из проекции строки auto_segments (online.db.get_transition_chain)."""
    t0, t1 = _dt(row.get("t_start")), _dt(row.get("t_end"))
    if t0 is None or t1 is None:
        return None
    lmax = row.get("load_pct_max")
    if isinstance(lmax, (list, tuple)):
        vals = [x for x in (_num(v) for v in lmax) if x is not None]
        lmax = max(vals) if vals else None
    rs = row.get("run_state")
    return Link(
        run_state=int(rs) if rs is not None else None,
        t_start=t0, t_end=t1,
        cause_open=row.get("cause_open"),
        load_pct_max=_num(lmax),
        coolant_start=_num(row.get("coolant_start")),
    )


def push_ctx(ctx: list[Link], link: Link | None, max_links: int = 8) -> list[Link]:
    """Добавить закрытый сегмент в контекст (не больше max_links последних)."""
    if link is None:
        return list(ctx)
    return (list(ctx) + [link])[-max_links:]


def ctx_for_window(ctx: list[Link] | None, ts_from: datetime | None,
                   tol_sec: float = 2.0) -> list[Link]:
    """Контекст, стыкующийся с началом окна, — иначе пусто (проверка на стыке не делается).

    Звенья позже начала окна отбрасываются: окно могли переиграть с того же
    начала после сбоя, а контекст уже ушёл вперёд.
    """
    if not ctx or ts_from is None:
        return []
    t = _dt(ts_from)
    keep = [link for link in ctx if (link.t_end - t).total_seconds() <= tol_sec]
    if not keep or abs((keep[-1].t_end - t).total_seconds()) > tol_sec:
        return []
    return keep


def chain_before(links: list[Link], head_t_start: Any, *, tol_sec: float = 2.0,
                 max_links: int = 8, allow_start_sequence: bool = False) -> Chain | None:
    """Цепочка звеньев, стыкующаяся с головой, до ближайшего якоря (стоп/работа).

    None — цепочка рвётся (зазор > tol_sec) или якоря нет. allow_start_sequence —
    для прогрева: если данные начинаются с пусковой последовательности (RS 1/2)
    без стопа перед ней, её первое звено считается якорем.
    """
    head = _dt(head_t_start)
    if head is None:
        return None
    expected = head
    collected: list[Link] = []
    idx = len(links) - 1
    while idx >= 0 and len(collected) < max_links:
        link = links[idx]
        if abs((link.t_end - expected).total_seconds()) > tol_sec:
            return None
        if link.run_state in _ANCHORS:
            between = tuple(reversed(collected))
            run_pieces = [link]
            if link.run_state == 3:
                j, exp = idx - 1, link.t_start
                while j >= 0 and links[j].run_state == 3 \
                        and abs((links[j].t_end - exp).total_seconds()) <= tol_sec:
                    run_pieces.append(links[j])
                    exp = links[j].t_start
                    j -= 1
            return Chain(anchor=link, between=between, run_pieces=tuple(run_pieces))
        collected.append(link)
        expected = link.t_start
        idx -= 1
    if allow_start_sequence and idx < 0 and collected and collected[-1].run_state in (1, 2):
        first = collected[-1]
        return Chain(anchor=first, between=tuple(reversed(collected[:-1])),
                     run_pieces=(), synthetic_anchor=True)
    return None
