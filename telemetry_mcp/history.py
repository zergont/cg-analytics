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
