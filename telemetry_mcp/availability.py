# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Какие регистры установки реально опрашиваются (v5.2.11; решение владельца
14.11.2026, docs/plans/design_tool_rules_v2.md, разд. 7).

Карта панели (`register_catalog` телеметрии) — одна на тип оборудования: у PCC в ней
1353 регистра. В телеметрию попадают только те, опрос которых заложен в настройке
роутера; опроса по требованию нет. На установку это 110–140 регистров, и набор зависит
от объекта. Модели нужен перечень доступных: иначе она выбирает регистры из всей
карты, тратит вызовы впустую и может принять «записей нет» за «сигнал не менялся».

Списка опроса в БД нет. Два источника, оба — записи самой телеметрии:

1. **Таблица последних значений `latest_state` исходной БД** — основной. В ней строка
   на каждый регистр, который роутер хоть раз прислал, с последним значением. Полна:
   в ней есть и регистры, которые опрашиваются, но ни разу не менялись.
2. **Записи реплики** — `history`, периоды `enum_history`, события `fault_history` —
   запасной, когда исходная БД недоступна. Неполон: запись идёт по изменению, и
   регистр, который не менялся с начала хранения (маска неисправностей без единой
   неисправности), записей не имеет вовсе. В этом режиме сервер говорит «записей нет»,
   а не «не опрашивается».

Записи реплики нужны и в основном режиме: по ним видно, что регистр опрашивается, но
записей изменения по нему нет — ряд по нему пуст, значение держится.

Принятое ограничение: регистр, снятый с опроса, остаётся в перечне — `latest_state`
обновляется по изменению и по периодике, а не на каждом опросе, и по дате обновления
снятый с опроса регистр от неизменного не отличить.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from types import SimpleNamespace
from typing import Any, NamedTuple

logger = logging.getLogger(__name__)

# Сроки обращения к исходной БД за последними значениями
LATEST_CONNECT_TIMEOUT_SEC = 6.0
LATEST_QUERY_TIMEOUT_SEC = 8.0

# DSN исходной БД телеметрии: сервер запоминает его при старте, до подмены источника
# репликой (telemetry_mcp.source_db). None — основной источник недоступен, работает запасной
_source_dsn: str | None = None


def set_source_dsn(dsn: str | None) -> None:
    global _source_dsn
    _source_dsn = (dsn or "").strip() or None


class Polled(NamedTuple):
    """Регистр в перечне. last — время последнего значения. held — записей изменения
    нет: значение держится (value — какое, если известно)."""
    last: datetime
    held: bool = False
    value: float | None = None


class PolledMap(dict):
    """{адрес: Polled}. exact — перечень из таблицы последних значений (полный);
    иначе выведен из записей и неизменных регистров в нём может не быть."""
    exact: bool = True


# (таблица, колонка времени, выражение «период открыт»)
_SOURCES: tuple[tuple[str, str, str], ...] = (
    ("history", "ts", "false"),
    ("enum_history", "state_start", "(state_end IS NULL)"),
    ("fault_history", "fault_start", "(fault_end IS NULL)"),
)
# По записи на адрес карты панели: поиск идёт по индексу (роутер, тип, панель, адрес, время)
_SQL = """
    SELECT c.addr, h.t, h.open
    FROM unnest($4::int[]) AS c(addr)
    CROSS JOIN LATERAL (
        SELECT {col} AS t, {open} AS open FROM {tab}
        WHERE router_sn = $1 AND equip_type = $2 AND panel_id = $3 AND addr = c.addr
        ORDER BY {col} DESC LIMIT 1) h
"""
_SQL_LATEST = """
    SELECT addr, ts, value FROM latest_state
    WHERE router_sn = $1 AND equip_type = $2 AND panel_id = $3
"""

_cache: dict[tuple[str, str, int], tuple[float, PolledMap]] = {}
_locks: dict[tuple[str, str, int], asyncio.Lock] = {}
_refreshing: dict[tuple, Any] = {}


def reset_cache() -> None:
    _cache.clear()


def _ttl_sec() -> float:
    try:
        from .common import settings
        return float(settings().limit("availability_ttl_min")) * 60.0
    except Exception:  # noqa: BLE001
        return 1800.0


async def _query(triple: tuple[str, str, int], addrs: list[int], tab: str, col: str, opened: str) -> list[Any]:
    from analytics import source as _src
    from db import remote
    async with remote.connection(_src._get_pool()) as conn:
        return await conn.fetch(_SQL.format(tab=tab, col=col, open=opened),
                                triple[0], triple[1], int(triple[2]), addrs)


async def _latest(triple: tuple[str, str, int]) -> list[Any] | None:
    """Строки таблицы последних значений исходной БД; None — источник не задан."""
    if not _source_dsn:
        return None
    import asyncpg
    conn = await asyncpg.connect(_source_dsn, timeout=LATEST_CONNECT_TIMEOUT_SEC,
                                 command_timeout=LATEST_QUERY_TIMEOUT_SEC)
    try:
        return await conn.fetch(_SQL_LATEST, triple[0], triple[1], int(triple[2]))
    finally:
        await conn.close()


def merge(parts: dict[str, list[Any]]) -> dict[int, Polled]:
    """Свести записи реплики: есть запись в history — её время; нет — последний
    период или событие (открытый период — «держится с»)."""
    out: dict[int, Polled] = {}
    for r in parts.get("history") or []:
        out[int(r["addr"])] = Polled(r["t"], False)
    for tab in ("enum_history", "fault_history"):
        for r in parts.get(tab) or []:
            a = int(r["addr"])
            if a not in out:
                out[a] = Polled(r["t"], bool(r["open"]))
    return out


def combine(latest: list[Any] | None, records: dict[int, Polled] | None) -> PolledMap | None:
    """Перечень установки из двух источников. Есть таблица последних значений —
    перечень полный: регистр без записей изменения помечается held с его значением.
    Нет — перечень из записей (exact=False). Нет ничего — None."""
    if latest is not None:
        out = PolledMap()
        out.exact = True
        for r in latest:
            a = int(r["addr"])
            no_changes = records is not None and a not in records
            v = r["value"]
            out[a] = Polled(r["ts"], no_changes, float(v) if (no_changes and v is not None) else None)
        return out
    if records is None:
        return None
    out = PolledMap(records)
    out.exact = False
    return out


async def _records(triple: tuple[str, str, int], addrs: list[int]) -> dict[int, Polled]:
    res = await asyncio.gather(*(_query(triple, addrs, tab, col, opened) for tab, col, opened in _SOURCES),
                               return_exceptions=True)
    parts: dict[str, list[Any]] = {}
    for (tab, _c, _o), r in zip(_SOURCES, res):
        if isinstance(r, asyncio.CancelledError):
            raise r
        if isinstance(r, BaseException):
            if tab == "history":
                raise r
            logger.warning("Доступность регистров %s/%s/%s: таблица %s не прочитана: %r", *triple, tab, r)
            continue
        parts[tab] = r
    return merge(parts)


async def _fetch(triple: tuple[str, str, int], addrs: list[int]) -> PolledMap | None:
    """Оба источника разом; отказ одного не мешает другому."""
    latest, records = await asyncio.gather(_latest(triple), _records(triple, addrs), return_exceptions=True)
    for r in (latest, records):
        if isinstance(r, asyncio.CancelledError):
            raise r
    if isinstance(latest, BaseException):
        logger.warning("Доступность регистров %s/%s/%s: таблица последних значений не прочитана (%r) — "
                       "перечень по записям", *triple, latest)
        latest = None
    if isinstance(records, BaseException):
        logger.warning("Доступность регистров %s/%s/%s: записи реплики не прочитаны: %r", *triple, records)
        records = None
    return combine(latest, records)


async def _refresh(unit: Any, catalog: dict[int, Any] | None) -> PolledMap | None:
    triple = tuple(unit.triple)
    lock = _locks.setdefault(triple, asyncio.Lock())
    async with lock:
        hit = _cache.get(triple)
        if hit and time.monotonic() - hit[0] < _ttl_sec():
            return hit[1]
        if catalog is None:
            from .catalog import get_catalog
            catalog = await get_catalog(unit.equip_type)
        if not catalog:
            return hit[1] if hit else None
        try:
            data = await _fetch(triple, sorted(int(a) for a in catalog))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("Доступность регистров %s/%s/%s не определена: %r", *triple, exc)
            data = None
        if data is None:
            return hit[1] if hit else None
        _cache[triple] = (time.monotonic(), data)
        return data


def _refresh_done(triple: tuple, task: "asyncio.Task[Any]") -> None:
    _refreshing.pop(triple, None)
    if not task.cancelled() and task.exception() is not None:
        logger.warning("Доступность регистров %s/%s/%s: обновление в фоне не выполнено: %r",
                       *triple, task.exception())


async def polled_registers(unit: Any, catalog: dict[int, Any] | None = None) -> PolledMap | None:
    """Перечень регистров установки с данными; None — определить не удалось
    (инструменты тогда работают как раньше, без пометок). Кэш на установку: первый
    вызов ждёт запрос, дальше отдаётся кэш, а по истечении срока он обновляется в
    фоне — набор опроса меняется редко, а вызов инструмента ждать не должен."""
    triple = tuple(unit.triple)
    hit = _cache.get(triple)
    if hit:
        if time.monotonic() - hit[0] >= _ttl_sec() and triple not in _refreshing:
            task = asyncio.ensure_future(_refresh(unit, catalog))
            _refreshing[triple] = task
            task.add_done_callback(lambda t, k=triple: _refresh_done(k, t))
        return hit[1]
    return await _refresh(unit, catalog)


async def warm_all() -> int:
    """Прогреть перечни опроса активных установок (сервер зовёт при старте, в фоне):
    первый вызов инструмента тогда не ждёт запрос. Возвращает число прогретых."""
    from .common import UNITS
    tree = await UNITS.tree()
    n = 0
    for o in tree.get("objects", []):
        for u in o.get("units", []):
            if not u.get("active"):
                continue
            p = u["panel"]
            key = (o["router_sn"], p["equip_type"], int(p["panel_id"]))
            if await polled_registers(SimpleNamespace(triple=key, equip_type=p["equip_type"])) is not None:
                n += 1
    return n


# ── Что сказать модели ───────────────────────────────────────────────────────

def is_exact(polled: dict[int, Polled] | None) -> bool:
    return bool(getattr(polled, "exact", True))


def split_polled(prefs: list[Any], polled: dict[int, Polled] | None) -> tuple[list[Any], list[Any]]:
    """(с данными, без данных) из разобранных параметров; доступность не определена —
    все считаются доступными."""
    if polled is None:
        return list(prefs), []
    kept = [p for p in prefs if p.addr in polled]
    return kept, [p for p in prefs if p.addr not in polled]


def unpolled_text(missing: list[Any], who: str = "", exact: bool = True) -> str:
    """Пометка конверта о регистрах без данных. exact — перечень полный, регистр
    точно не опрашивается; иначе — записей нет, а причина неизвестна."""
    items = "; ".join(f"{p.addr} «{p.label}»" for p in missing)
    head = f"у {who} " if who else ""
    if exact:
        return (f"{head}роутером не опрашивается, данных нет: {items} "
                "(перечень опрашиваемых — register_catalog)")
    return (f"{head}записей нет — регистр не опрашивается или его значение не менялось с начала "
            f"хранения истории: {items} (перечень регистров с записями — register_catalog)")


def nothing_text(exact: bool = True) -> str:
    """Ответ инструмента, когда ни по одному запрошенному регистру данных нет."""
    if exact:
        return ("Ни один из запрошенных регистров роутером не опрашивается — данных по ним нет. "
                "Перечень опрашиваемых регистров установки — register_catalog.")
    return ("Ни по одному из запрошенных регистров записей нет: они не опрашиваются или их значения не "
            "менялись с начала хранения истории. Перечень регистров с записями — register_catalog.")


NOTHING_POLLED = nothing_text(True)


def fmt_value(v: float | None) -> str:
    if v is None:
        return ""
    return str(int(v)) if float(v).is_integer() else f"{v:g}".replace(".", ",")


def held_text(prefs: list[Any], polled: dict[int, Polled] | None) -> str | None:
    """Пометка о регистрах, которые опрашиваются, но записей изменения не имеют:
    ряд по ним пуст, значение держится."""
    if polled is None or not is_exact(polled):
        return None
    held = [(p, polled[p.addr]) for p in prefs if p.addr in polled and polled[p.addr].held]
    if not held:
        return None
    items = "; ".join(f"{p.addr} «{p.label}»" + (f" = {fmt_value(x.value)}" if x.value is not None else "")
                      for p, x in held)
    return ("в опросе, но значение не менялось с начала хранения истории — записей изменения нет, "
            f"значение держится: {items}")


def last_text(p: Polled, tz: Any, exact: bool = True) -> str:
    """Колонка «опрос» каталога: «посл. запись 14.11.2026», «значение не менялось (0)»
    или, в перечне по записям, «без изменений с 25.05.2026»."""
    d = p.last.astimezone(tz).strftime("%d.%m.%Y") if tz is not None else p.last.strftime("%d.%m.%Y")
    if p.held and exact:
        return "значение не менялось" + (f" ({fmt_value(p.value)})" if p.value is not None else "")
    if p.held:
        return f"без изменений с {d}"
    return f"посл. запись {d}"
