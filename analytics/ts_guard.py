# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Защита от кривых меток времени телеметрии — вторая линия при чтении.

Первая линия — до записи: буфер с переподписью по 40290 в cg-decoder, проверки
db-writer (решения владельца 30.10.2026, docs/plans/design_time_guard.md). Здесь —
то, что всё равно лежит в источнике или проскочит мимо:

- кадр застывших часов: первый опрос после загрузки роутера подписан временем,
  сохранённым до выключения (ошибка = время без питания, до 100 ч). db-writer
  заводит по нему периоды задним числом — 30.09, 01.10, 03.10, 22.09 это ~75 ч
  фиктивной «Работы» и эпизоды с началом за сутки до события;
- метка в будущем (часы роутера +2 ч до 09.10): вывернутые периоды, концы
  разрывов в будущем;
- дубли (addr, ts) с разными значениями: порядок выборки не задан.

Функции чистые: на входе строки источника с received_at / created_at, на выходе —
то, что видели бы потребители, будь кадр подписан временем приёма. Когда первая
линия заработает, правила ничего не меняют.
"""
from __future__ import annotations

import bisect
from datetime import datetime, timedelta
from typing import Any, Callable, Hashable

# Расхождение метки и момента записи, после которого метка подозрительна.
# Норма: received_at − ts ≤ 38 с (p99.99), created_at − начало периода ≤ 52 с;
# кадры застывших часов — от 2,6 ч, метки «+2 ч» — около −2 ч
SKEW_SEC = 600.0
# Период записан в момент переподключения: рядом (±60 с) создана запись data_gaps.
# Так отличаем кадр застывших часов от очереди db-writer, где разрыва нет
RECONNECT_SEC = 60.0
# Строки одной пачки приёма — шаг received_at не больше
BURST_SEC = 5.0
# Счётчик секунд контроллера (+1 с/с) — по типу оборудования. Кадр застывших
# часов несёт свежий счётчик со старой меткой, настоящее опоздание — согласованный
COUNTER_ADDRS: dict[str, int] = {"pcc": 40290}


def counter_addr(equip_type: str | None) -> int | None:
    """Регистр-счётчик секунд контроллера для типа оборудования (или None)."""
    return COUNTER_ADDRS.get(equip_type or "")


def _lag(row: dict[str, Any]) -> float | None:
    """received_at − ts, с; None — момент приёма неизвестен (считаем нормой)."""
    ra = row.get("received_at")
    if ra is None or row.get("ts") is None:
        return None
    return (ra - row["ts"]).total_seconds()


def _strip(row: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if k not in keys}


# ── История ────────────────────────────────────────────────────────────────────

def filter_history(
    rows: list[dict[str, Any]],
    counter: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Строки history без кривых меток и дублей; received_at из строк убирается.

    rows — по ts, addr, received_at (так их отдаёт source). Правила:
    - метка в будущем (ts − received_at > SKEW_SEC) — строка отбрасывается всегда;
    - поздние строки (received_at − ts > SKEW_SEC) собираются в пачки приёма.
      Пачка отбрасывается, только если её счётчик секунд расходится с ближайшей
      по ts нормальной строкой счётчика больше чем на SKEW_SEC — это кадр
      застывших часов. Пачка без счётчика или без нормального соседа остаётся:
      проверить нечем, а очередь db-writer и настоящее опоздание — живые данные;
    - из дублей (addr, ts) — копия с наименьшим |received_at − ts|, при равенстве
      — пришедшая раньше.

    Возвращает (строки, {"future": n, "frozen": n, "dups": n}).
    """
    stats = {"future": 0, "frozen": 0, "dups": 0}
    drop: set[int] = set()
    late: list[int] = []
    cnt_ts: list[datetime] = []
    cnt_val: list[float] = []
    skew = timedelta(seconds=SKEW_SEC)
    for i, r in enumerate(rows):
        ra = r.get("received_at")
        d = ra - r["ts"] if ra is not None else None
        if d is None or -skew <= d <= skew:
            if r["addr"] == counter and counter is not None and r["value"] is not None:
                cnt_ts.append(r["ts"])
                cnt_val.append(float(r["value"]))
        elif d < -skew:
            drop.add(i)
            stats["future"] += 1
        else:
            late.append(i)

    if late and cnt_ts:
        late.sort(key=lambda i: rows[i]["received_at"])
        bursts: list[list[int]] = []
        for i in late:
            if bursts and (rows[i]["received_at"] - rows[bursts[-1][-1]]["received_at"]).total_seconds() <= BURST_SEC:
                bursts[-1].append(i)
            else:
                bursts.append([i])
        for burst in bursts:
            miss = []
            for i in burst:
                r = rows[i]
                if r.get("addr") != counter or r.get("value") is None:
                    continue
                k = bisect.bisect_left(cnt_ts, r["ts"])
                near = [j for j in (k - 1, k) if 0 <= j < len(cnt_ts)]
                j = min(near, key=lambda j: abs((cnt_ts[j] - r["ts"]).total_seconds()))
                miss.append(abs((r["ts"] - cnt_ts[j]).total_seconds() - (float(r["value"]) - cnt_val[j])))
            if miss and sorted(miss)[len(miss) // 2] > SKEW_SEC:
                drop.update(burst)
                stats["frozen"] += len(burst)

    # Дубли (addr, ts) стоят рядом: порядок ts, addr, received_at. Ключ выбора
    # считается только у совпадений — их единицы на сутки
    def _pick(r: dict[str, Any]) -> tuple[float, float]:
        lag = _lag(r)
        ra = r.get("received_at")
        return (abs(lag) if lag is not None else 0.0,
                ra.timestamp() if ra is not None else float("inf"))

    out: list[dict[str, Any]] = []
    prev: dict[str, Any] | None = None
    for i, r in enumerate(rows):
        if i in drop:
            continue
        if prev is not None and r["ts"] == prev["ts"] and r["addr"] == prev["addr"]:
            stats["dups"] += 1
            if _pick(r) < _pick(prev):
                prev = r
            continue
        if prev is not None:
            out.append({k: v for k, v in prev.items() if k != "received_at"})
        prev = r
    if prev is not None:
        out.append({k: v for k, v in prev.items() if k != "received_at"})
    return out, stats


# ── Периоды enum / fault ────────────────────────────────────────────────────────

def fix_periods(
    rows: list[dict[str, Any]],
    ts_from: datetime,
    ts_to: datetime,
    *,
    start: str,
    end: str,
    key: Callable[[dict[str, Any]], Hashable],
) -> tuple[list[dict[str, Any]], list[tuple[Hashable, datetime]],
           list[tuple[Hashable, datetime, datetime]]]:
    """Периоды так, будто кадр с кривой меткой подписан моментом записи.

    rows — периоды источника с created_at и ts_bad (кривое начало, его считает
    SQL: начало в будущем или задним числом при переподключении). Кривое начало
    переносится на created_at; на тот же момент — конец предшественника того же
    ключа (кадр закрывает его на своей метке, ровно). Периоды нулевой и
    отрицательной длины отбрасываются, перекрытия у исправленных пар режутся по
    началу следующего. Пары, которых правка не коснулась, остаются как есть:
    в источнике бывают свои перекрытия и дыры.

    Возвращает (периоды окна [ts_from, ts_to) без служебных полей,
    недостающие предшественники [(ключ, старое начало)],
    переносы [(ключ, старое начало, новое)]).
    Предшественник нужен, когда новое начало легло позже ts_from, а периода,
    который кадр закрыл, в выборке нет — его дочитывают и зовут функцию снова.
    """
    work = [dict(r) for r in rows]
    moved: dict[tuple[Hashable, datetime], datetime] = {}
    ends = {(key(r), r[end]) for r in work if r.get(end) is not None}
    need: list[tuple[Hashable, datetime]] = []
    for r in work:
        if r.get("ts_bad") and r.get("created_at") is not None and r[start] != r["created_at"]:
            k = key(r)
            moved[(k, r[start])] = r["created_at"]
            # Предшественник с концом в окне выборка уже вернула бы — дочитывать
            # стоит только того, кто кончился до окна
            if (r[start] <= ts_from < r["created_at"]) and (k, r[start]) not in ends:
                need.append((k, r[start]))
            r[start] = r["created_at"]
            r["_fixed"] = True
    if not moved:
        return [_strip(r, ("created_at", "ts_bad", "_fixed")) for r in work
                if r[start] < ts_to and (r.get(end) is None or r[end] > ts_from)], [], []
    for r in work:
        e = r.get(end)
        if e is not None and (key(r), e) in moved:
            r[end] = moved[(key(r), e)]
            r["_fixed"] = True

    # Ключи без правок — как есть, со своими странностями источника: иначе вход
    # нарезки зависел бы от того, есть ли в выборке кривой период другого
    # регистра (лента читает весь каталог, нарезка — только KB)
    by_key: dict[Hashable, list[dict[str, Any]]] = {}
    for r in work:
        by_key.setdefault(key(r), []).append(r)
    kept: list[dict[str, Any]] = []
    for lst in by_key.values():
        if not any(r.get("_fixed") for r in lst):
            kept.extend(lst)
            continue
        lst = [r for r in lst if r.get(end) is None or r[end] > r[start]]
        lst.sort(key=lambda r: r[start])
        for a, b in zip(lst, lst[1:]):
            if (a.get("_fixed") or b.get("_fixed")) and (a.get(end) is None or a[end] > b[start]):
                a[end] = b[start]
        kept.extend(r for r in lst if r.get(end) is None or r[end] > r[start])
    out = [_strip(r, ("created_at", "ts_bad", "_fixed")) for r in kept
           if r[start] < ts_to and (r.get(end) is None or r[end] > ts_from)]
    return out, need, [(k, old, new) for (k, old), new in moved.items()]


# ── Разрывы связи ──────────────────────────────────────────────────────────────

def fix_gaps(
    rows: list[dict[str, Any]],
    ts_from: datetime,
    ts_to: datetime,
) -> tuple[list[dict[str, Any]], list[datetime]]:
    """data_gaps без концов в будущем; created_at из строк убирается.

    Конец позже момента записи больше чем на SKEW_SEC (часы роутера впереди)
    переносится на created_at: фиктивный двухчасовой разрыв 09.10 сжимается до
    секунд, у настоящего возвращается верный конец. Разрыв с началом в будущем
    и вывернутый — отбрасываются. Конец задним числом не трогаем: так же выглядит
    настоящее опоздание. Возвращает (разрывы окна, начала исправленных).
    """
    out: list[dict[str, Any]] = []
    fixed: list[datetime] = []
    for r in rows:
        r = dict(r)
        ca = r.pop("created_at", None)
        if ca is not None:
            if (r["gap_start"] - ca).total_seconds() > SKEW_SEC:
                fixed.append(r["gap_start"])
                continue
            if r.get("gap_end") is not None and (r["gap_end"] - ca).total_seconds() > SKEW_SEC:
                r["gap_end"] = ca
                fixed.append(r["gap_start"])
        if r.get("gap_end") is not None and r["gap_end"] <= r["gap_start"]:
            continue
        if r["gap_start"] < ts_to and (r.get("gap_end") is None or r["gap_end"] > ts_from):
            out.append(r)
    return out, fixed
