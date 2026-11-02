# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""История регистров для инструментов — тем же путём, что у движка
(analytics.source: порции, счётчик секунд, ts_guard), но со статистикой
очистки наружу: конверт печатает, сколько строк отброшено и почему.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from analytics import source as _src
from analytics import ts_guard


async def load_history(router_sn: str, equip_type: str, panel_id: int,
                       t_from: datetime, t_to: datetime, addrs: frozenset[int],
                       ) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """(строки без кривых меток и дублей, {"future", "frozen", "dups"}).
    Счётчик секунд контроллера дочитывается для проверки и из ответа убирается."""
    if not addrs:
        return [], {"future": 0, "frozen": 0, "dups": 0}
    fetch = _src._with_counter(equip_type, addrs)
    rows: list[dict[str, Any]] = []
    cur = t_from
    chunk = timedelta(seconds=_src._HISTORY_CHUNK_SEC)
    while cur < t_to:
        nxt = min(cur + chunk, t_to)
        rows.extend(await _src._fetch_whitelist_history(router_sn, equip_type, panel_id, cur, nxt, fetch))
        cur = nxt
    counter = ts_guard.counter_addr(equip_type)
    try:
        out, st = ts_guard.filter_history(rows, counter)
    except Exception:  # noqa: BLE001
        out = [{k: v for k, v in r.items() if k != "received_at"} for r in rows]
        st = {"future": 0, "frozen": 0, "dups": 0}
    if counter is not None and counter not in addrs:
        out = [r for r in out if r["addr"] != counter]
    return out, st


def gap_pairs(gaps: list[dict[str, Any]]) -> list[tuple[datetime, datetime | None]]:
    return [(g["gap_start"], g.get("gap_end")) for g in gaps if g.get("gap_start") is not None]


# ── Укрупнение на стороне БД для длинных окон (v5.2.3, приёмка Э1) ──────────
#
# Ряд длиннее series_raw_window_hours сырыми строками не вычитать за срок вызова
# (18 суток × 2 регистра не уложились в 20 с). Считаем в TimescaleDB по
# корзинам time_bucket: минимум, максимум, сумма «значение × время удержания до
# следующей записи» и сумма этого времени (среднее ПО ВРЕМЕНИ), число записей,
# первое и последнее значение. Метки в будущем отброшены по SKEW_SEC; кадр
# застывших часов в SQL не отличить — об этом говорит конверт.

# Без функций TimescaleDB (time_bucket/first/last): реплика в БД аналитики — обычный
# PostgreSQL. Корзина — floor(epoch/шаг)·шаг, выровнена по эпохе UTC, как time_bucket.
_BUCKET_SQL = """
    WITH r AS (
        SELECT addr, ts, value::float8 AS value,
               LEAD(ts) OVER (PARTITION BY addr ORDER BY ts) AS ts_next
        FROM history
        WHERE router_sn = $1 AND equip_type = $2 AND panel_id = $3
          AND addr = ANY($4::int[])
          AND ts >= $5 AND ts < $6 AND value IS NOT NULL
          AND ts <= received_at + ($8::float8) * interval '1 second'
    )
    SELECT addr,
           to_timestamp(floor(EXTRACT(EPOCH FROM ts) / $7::float8) * $7::float8) AS b,
           min(value) AS vmin, max(value) AS vmax,
           sum(value * EXTRACT(EPOCH FROM (LEAST(COALESCE(ts_next, $6), $6) - ts))::float8)::float8 AS svd,
           sum(EXTRACT(EPOCH FROM (LEAST(COALESCE(ts_next, $6), $6) - ts))::float8)::float8 AS sd,
           count(*) AS n,
           (array_agg(value ORDER BY ts))[1] AS vfirst,
           (array_agg(value ORDER BY ts DESC))[1] AS vlast,
           (array_agg(ts ORDER BY value, ts))[1] AS t_min,
           (array_agg(ts ORDER BY value DESC, ts))[1] AS t_max,
           min(ts) AS t_first, max(ts) AS t_last
    FROM r GROUP BY addr, b ORDER BY addr, b
"""


async def bucketed_history(router_sn: str, equip_type: str, panel_id: int,
                           t_from: datetime, t_to: datetime, addrs: frozenset[int],
                           step_sec: int, chunk_days: float = 4.0) -> dict[int, list[dict[str, Any]]]:
    """{addr: [{b, vmin, vmax, avg, n, vfirst, vlast, t_first, t_last}, …]} по корзинам
    шага step_sec (выровнены по эпохе UTC, как time_bucket). Запрос идёт порциями
    по chunk_days суток; корзина на стыке порций сшивается."""
    if not addrs:
        return {}
    from db import remote
    acc: dict[tuple[int, datetime], dict[str, Any]] = {}
    cur = t_from
    chunk = timedelta(days=chunk_days)
    while cur < t_to:
        nxt = min(cur + chunk, t_to)
        async with remote.connection(_src._get_pool()) as conn:
            rows = await conn.fetch(_BUCKET_SQL, router_sn, equip_type, panel_id, list(addrs),
                                    cur, nxt, float(step_sec), float(ts_guard.SKEW_SEC))
        for r in rows:
            key = (int(r["addr"]), r["b"])
            # Decimal из PostgreSQL (numeric) → float: дальше арифметика в Python
            vmin, vmax = float(r["vmin"]), float(r["vmax"])
            svd, sd = float(r["svd"] or 0.0), float(r["sd"] or 0.0)
            vfirst = float(r["vfirst"]) if r["vfirst"] is not None else None
            vlast = float(r["vlast"]) if r["vlast"] is not None else None
            a = acc.get(key)
            if a is None:
                acc[key] = {"b": r["b"], "vmin": vmin, "vmax": vmax, "svd": svd, "sd": sd, "n": int(r["n"]),
                            "vfirst": vfirst, "vlast": vlast, "t_first": r["t_first"], "t_last": r["t_last"],
                            "t_min": r["t_min"], "t_max": r["t_max"]}
            else:
                if vmin < a["vmin"]:
                    a["vmin"], a["t_min"] = vmin, r["t_min"]
                if vmax > a["vmax"]:
                    a["vmax"], a["t_max"] = vmax, r["t_max"]
                a["svd"] += svd
                a["sd"] += sd
                a["n"] += int(r["n"])
                if r["t_first"] < a["t_first"]:
                    a["t_first"], a["vfirst"] = r["t_first"], vfirst
                if r["t_last"] > a["t_last"]:
                    a["t_last"], a["vlast"] = r["t_last"], vlast
                a["vmin"] = min(a["vmin"], vmin)
                a["vmax"] = max(a["vmax"], vmax)
        cur = nxt
    out: dict[int, list[dict[str, Any]]] = {}
    for (addr, _b), a in sorted(acc.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        a["avg"] = a["svd"] / a["sd"] if a["sd"] > 0 else (a["vfirst"] if a["vfirst"] is not None else a["vmin"])
        out.setdefault(addr, []).append(a)
    return out


async def retention_cutoff(table: str = "history") -> datetime | None:
    """Момент, раньше которого записей таблицы в источнике уже нет; None — срок неизвестен."""
    from datetime import datetime as _dt, timezone
    pol = await retention_policies()
    keep = _parse_interval(pol.get(table, "")) if pol.get(table) else None
    return (_dt.now(timezone.utc) - keep) if keep is not None else None


_retention: tuple[float, dict[str, str]] | None = None


async def retention_policies() -> dict[str, str]:
    """{таблица: drop_after} из политик TimescaleDB источника (кэш на час); {} — не прочитать."""
    global _retention
    import time
    now = time.monotonic()
    if _retention and now - _retention[0] < 3600:
        return _retention[1]
    out: dict[str, str] = {}
    try:
        from db import remote
        async with remote.connection(_src._get_pool()) as conn:
            rows = await conn.fetch("""
                SELECT hypertable_name, config FROM timescaledb_information.jobs
                WHERE proc_name LIKE '%retention%'
            """)
        import json
        for r in rows:
            cfg = r["config"]
            cfg = json.loads(cfg) if isinstance(cfg, str) else (cfg or {})
            if r["hypertable_name"] and cfg.get("drop_after"):
                out[str(r["hypertable_name"])] = str(cfg["drop_after"])
        _retention = (now, out)
    except Exception:  # noqa: BLE001
        if _retention:
            return _retention[1]
    return out


def _parse_interval(s: str) -> timedelta | None:
    parts = s.split()
    if len(parts) != 2:
        return None
    try:
        n = int(parts[0])
    except ValueError:
        return None
    unit = parts[1].rstrip("s")
    return {"day": timedelta(days=n), "hour": timedelta(hours=n), "week": timedelta(weeks=n),
            "month": timedelta(days=30 * n), "year": timedelta(days=365 * n)}.get(unit)


async def retention_note(t_from: datetime, t_to: datetime, table: str = "history") -> str | None:
    """Пометка, если окно (целиком или частично) старше срока хранения таблицы источника."""
    from datetime import datetime as _dt, timezone
    pol = await retention_policies()
    keep = _parse_interval(pol.get(table, "")) if pol.get(table) else None
    if keep is None:
        return None
    oldest = _dt.now(timezone.utc) - keep
    if t_to <= oldest:
        return (f"окно старше срока хранения таблицы {table} в телеметрии ({pol[table]}): сырых записей "
                "уже нет; остаются сегменты, эпизоды и акты аналитики (segments, episodes, incident_acts, trend)")
    if t_from < oldest:
        return f"начало окна старше срока хранения {table} ({pol[table]}): записи есть только с {oldest:%d.%m.%Y}"
    return None
