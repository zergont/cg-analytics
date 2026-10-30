# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Чтение данных из основной БД телеметрии для аналитического блока.

Сегментация и детекторы читают ТОЛЬКО whitelist-регистры (ТЗ, раздел 2.3):
- аналоговые → history_rich (addr IN whitelist_analog)
- enum-периоды → enum_history (addr IN [40011, 40010])
- fault-периоды → fault_history (addr IN 40400-40428)
- пропуски связи → data_gaps

Ленты событий (разбор аварии, лента стоянки) — ВСЁ, что прислала панель:
get_timeline_periods, без фильтра KB (v4.9.96).

Кривые метки времени (кадр застывших часов роутера, метка в будущем, дубли
(addr, ts)) отсеиваются здесь, при чтении, — все пути видят одно и то же:
живой цикл, рез, перечитка, акт, пакетный разбор (ts_guard, решения 30.10.2026).
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Any

import asyncpg

from config import settings
from db import remote

from . import ts_guard

logger = logging.getLogger(__name__)

# Правки кривых меток — в журнал один раз: открытый период с исправленным
# началом читается каждым циклом, сутками
_NOTED: set = set()


def _note_once(key: tuple, msg: str, *args: Any) -> None:
    if key in _NOTED:
        return
    if len(_NOTED) > 5000:
        _NOTED.clear()
    _NOTED.add(key)
    logger.info(msg, *args)

_pool: asyncpg.Pool | None = None
_QUERY_TIMEOUT_SEC: int = 120  # переопределяется из config при init_source_pool()


async def init_source_pool() -> None:
    """Пул source-БД: 3 соединения на движки, гейт, ночной прогон и API.

    v5.1.24: через db/remote — command_timeout, statement_timeout сессии,
    keepalive на сокет; min_size=0 — старт сервиса от VPN не зависит (первое
    соединение откроет первый запрос). Все выдачи — remote.connection (сроки)."""
    global _pool, _QUERY_TIMEOUT_SEC
    _QUERY_TIMEOUT_SEC = settings.source_query_timeout
    _pool = await remote.create_pool(
        settings.source_db_url,
        min_size=0,
        max_size=3,
        label="source-БД",
    )


async def close_source_pool() -> None:
    """Закрыть пул со сроком; не закрылся — соединения обрываются (v5.1.24)."""
    global _pool
    if _pool:
        pool, _pool = _pool, None
        await remote.close_pool(pool, "source-БД")


def _get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Пул соединений с БД не инициализирован")
    return _pool


async def _fetch_whitelist_history(
    router_sn: str,
    equip_type: str,
    panel_id: int,
    ts_from: datetime,
    ts_to: datetime,
    whitelist_addrs: frozenset[int],
) -> list[dict[str, Any]]:
    """Строки как в источнике, с received_at — вход _guard_history."""
    if not whitelist_addrs:
        return []

    async with remote.connection(_get_pool()) as conn:
        rows = await conn.fetch(
            """
            SELECT addr, ts, value, raw, name_ru, unit, received_at
            FROM history_rich
            WHERE router_sn  = $1
              AND equip_type = $2
              AND panel_id   = $3
              AND addr       = ANY($4::int[])
              AND ts >= $5
              AND ts <  $6
            ORDER BY ts ASC, addr ASC, received_at ASC
            """,
            router_sn, equip_type, panel_id,
            list(whitelist_addrs),
            ts_from, ts_to,
        )
    return [dict(r) for r in rows]


def _with_counter(equip_type: str, addrs: frozenset[int]) -> frozenset[int]:
    """Адреса чтения со счётчиком секунд контроллера: без него кадр застывших
    часов в узкой выборке (обороты и частота акта) не проверить."""
    c = ts_guard.counter_addr(equip_type)
    return addrs | {c} if addrs and c is not None and c not in addrs else addrs


def _guard_history(
    rows: list[dict[str, Any]], router_sn: str, equip_type: str, panel_id: int,
    addrs: frozenset[int] | None = None, meta: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Без меток в будущем, кадров застывших часов (по счётчику секунд
    контроллера) и дублей (addr, ts) — ts_guard.filter_history.

    addrs — что просили (счётчик, дочитанный для проверки, из ответа убирается);
    meta["max_ts"] — наибольшая метка прочитанного до проверки: отметка, от
    которой живой цикл дочитывает хвост (отброшенные строки перечитывать незачем).
    """
    if meta is not None:
        meta["max_ts"] = max((r["ts"] for r in rows), default=None)
    counter = ts_guard.counter_addr(equip_type)
    try:
        out, st = ts_guard.filter_history(rows, counter)
    except Exception:
        logger.warning("Проверка меток истории %s/%s/%s не выполнена — строки как есть",
                       router_sn, equip_type, panel_id, exc_info=True)
        out = [{k: v for k, v in r.items() if k != "received_at"} for r in rows]
        st = {"future": 0, "frozen": 0, "dups": 0}
    if addrs is not None and counter is not None and counter not in addrs:
        out = [r for r in out if r["addr"] != counter]
    if st["future"] or st["frozen"]:
        # Пока часы роутера впереди, отбрасывается каждый цикл — в журнал
        # не чаще раза в 10 минут
        _note_once(
            ("hist", router_sn, equip_type, panel_id, int(time.monotonic() // 600)),
            "Кривые метки истории %s/%s/%s: отброшено %d строк с меткой в будущем и %d "
            "строк кадров застывших часов (счётчик %s расходится с меткой)",
            router_sn, equip_type, panel_id, st["future"], st["frozen"], counter,
        )
    if st["dups"]:
        logger.debug("История %s/%s/%s: %d дублей (addr, ts) — взята копия, ближайшая к приёму",
                     router_sn, equip_type, panel_id, st["dups"])
    return out


async def get_whitelist_history(
    router_sn: str,
    equip_type: str,
    panel_id: int,
    ts_from: datetime,
    ts_to: datetime,
    whitelist_addrs: frozenset[int],
    meta: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Аналоговые регистры из whitelist за период [ts_from, ts_to).

    Возвращает строки отсортированные по ts ASC (для causal-обработки).
    Колонки: addr, ts, value, raw, name_ru, unit. Кривые метки и дубли
    отсеяны (_guard_history); meta — см. там.
    """
    rows = await _fetch_whitelist_history(
        router_sn, equip_type, panel_id, ts_from, ts_to,
        _with_counter(equip_type, whitelist_addrs))
    return _guard_history(rows, router_sn, equip_type, panel_id, whitelist_addrs, meta)


# Порция для больших диапазонов history: один SELECT за несколько суток может
# не влезть в command_timeout через WAN/VPN — режем на куски по N часов.
_HISTORY_CHUNK_SEC = 6 * 3600


async def get_whitelist_history_chunked(
    router_sn: str,
    equip_type: str,
    panel_id: int,
    ts_from: datetime,
    ts_to: datetime,
    whitelist_addrs: frozenset[int],
    meta: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """get_whitelist_history порциями по _HISTORY_CHUNK_SEC.

    Каждый кусок — отдельный запрос, гарантированно влезающий в command_timeout.
    Куски идут последовательно [from, to), порядок ts ASC сохраняется. Кривые
    метки отсеиваются по всему окну сразу: кадр на стыке кусков сверяется с
    соседями из обоих.
    """
    out: list[dict[str, Any]] = []
    cur = ts_from
    n_chunks = 0
    fetch_addrs = _with_counter(equip_type, whitelist_addrs)
    while cur < ts_to:
        nxt = min(cur + timedelta(seconds=_HISTORY_CHUNK_SEC), ts_to)
        out.extend(await _fetch_whitelist_history(
            router_sn, equip_type, panel_id, cur, nxt, fetch_addrs
        ))
        cur = nxt
        n_chunks += 1
    if n_chunks > 1:
        logger.debug(
            "history %s/%s/%s: %d строк за %d кусков [%s .. %s]",
            router_sn, equip_type, panel_id, len(out), n_chunks, ts_from, ts_to,
        )
    return _guard_history(out, router_sn, equip_type, panel_id, whitelist_addrs, meta)


def _ts_bad_sql(alias: str, start_col: str, p_skew: int, p_reconnect: int) -> str:
    """SQL «начало периода кривое» (ts_guard.fix_periods).

    Начало позже записи больше чем на SKEW — часы роутера впереди. Раньше записи
    больше чем на SKEW — только если период записан при переподключении (data_gaps
    создан рядом): так пишет кадр застывших часов, а очередь db-writer — нет."""
    return f"""CASE
                WHEN {alias}.{start_col} - {alias}.created_at > make_interval(secs => ${p_skew})
                    THEN TRUE
                WHEN {alias}.created_at - {alias}.{start_col} > make_interval(secs => ${p_skew})
                    THEN EXISTS (
                        SELECT 1 FROM data_gaps g
                        WHERE g.router_sn  = {alias}.router_sn
                          AND g.equip_type = {alias}.equip_type
                          AND g.panel_id   = {alias}.panel_id
                          AND g.created_at BETWEEN {alias}.created_at - make_interval(secs => ${p_reconnect})
                                               AND {alias}.created_at + make_interval(secs => ${p_reconnect}))
                ELSE FALSE
            END"""


# Окно выборки периодов. Начало в будущем видно с момента записи (LEAST). Второе
# условие добирает вывернутый период (метка в будущем: начало позже конца) с
# началом в окне — по нему переносится конец предшественника, лежащий в окне
# (09.10: «Разгрузка» до 14:03:03 при остановленной с 13:09:58 машине). На чистых
# данных лишних строк не даёт: fix_periods всё равно режет по окну
_PERIOD_WINDOW_SQL = """(
                  (LEAST({a}.{s}, {a}.created_at) < $6 AND ({a}.{e} IS NULL OR {a}.{e} > $5))
                  OR ({a}.{s} >= $5 AND {a}.{s} < $6)
              )"""
_ENUM_WINDOW_SQL = _PERIOD_WINDOW_SQL.format(a="e", s="state_start", e="state_end")
_FAULT_WINDOW_SQL = _PERIOD_WINDOW_SQL.format(a="f", s="fault_start", e="fault_end")
_FAULT_TS_BAD_SQL = _ts_bad_sql("f", "fault_start", 7, 8)


def _enum_select(p_skew: int, p_reconnect: int) -> str:
    return f"""
            SELECT
                e.addr,
                e.state_start,
                e.state_end,
                e.value,
                r.name_ru,
                COALESCE(
                    r.states_json->'labels_ru'->>e.value::text,
                    r.states_json->'labels'   ->>e.value::text,
                    e.value::text
                ) AS label,
                e.created_at,
                {_ts_bad_sql('e', 'state_start', p_skew, p_reconnect)} AS ts_bad
            FROM enum_history e
            LEFT JOIN register_catalog r
                ON r.equip_type = e.equip_type AND r.addr = e.addr"""


async def get_enum_periods(
    router_sn: str,
    equip_type: str,
    panel_id: int,
    ts_from: datetime,
    ts_to: datetime,
    addrs: list[int] | None = None,
) -> list[dict[str, Any]]:
    """Периоды enum-состояний из enum_history, пересекающиеся с [ts_from, ts_to).

    По умолчанию — только 40011 (RUN_STATE) и 40010 (SWITCH_POS).
    Колонки: addr, state_start, state_end, value, name_ru, label.
    state_end IS NULL → период активен прямо сейчас.

    Начало, заведённое кадром с кривой меткой, перенесено на момент записи
    вместе с концом периода, который кадр закрыл (ts_guard.fix_periods). Если
    тот период кончился до окна, а после переноса тянется в него, — дочитывается
    вторым запросом (бывает только рядом с кадром).
    """
    if addrs is None:
        addrs = [40011, 40010]

    async with remote.connection(_get_pool()) as conn:
        rows = await conn.fetch(
            _enum_select(7, 8) + f"""
            WHERE e.router_sn  = $1
              AND e.equip_type = $2
              AND e.panel_id   = $3
              AND e.addr       = ANY($4::int[])
              AND {_ENUM_WINDOW_SQL}
            ORDER BY e.addr ASC, e.state_start ASC
            """,
            router_sn, equip_type, panel_id,
            addrs,
            ts_from, ts_to,
            ts_guard.SKEW_SEC, ts_guard.RECONNECT_SEC,
        )
        rows = [dict(r) for r in rows]
        out, need, moved = _fix_periods_safe(
            rows, ts_from, ts_to, "state_start", "state_end", _enum_key, router_sn, panel_id)
        if need:
            pred = await conn.fetch(
                _enum_select(6, 7) + """
                WHERE e.router_sn  = $1
                  AND e.equip_type = $2
                  AND e.panel_id   = $3
                  AND (e.addr, e.state_end) IN (
                      SELECT * FROM unnest($4::int[], $5::timestamptz[]))
                """,
                router_sn, equip_type, panel_id,
                [a for a, _ in need], [t for _, t in need],
                ts_guard.SKEW_SEC, ts_guard.RECONNECT_SEC,
            )
            out, _, moved = _fix_periods_safe(
                rows + [dict(r) for r in pred], ts_from, ts_to,
                "state_start", "state_end", _enum_key, router_sn, panel_id)
    if moved:
        _note_periods("enum", router_sn, equip_type, panel_id, moved)
        out.sort(key=lambda p: (p["addr"], p["state_start"]))
    return out


def _fix_periods_safe(rows, ts_from, ts_to, start, end, key, router_sn, panel_id):
    """ts_guard.fix_periods; сбой — периоды как в источнике (окно как до проверки
    меток), чтение не падает: ровно то, что читалось без защиты."""
    try:
        return ts_guard.fix_periods(rows, ts_from, ts_to, start=start, end=end, key=key)
    except Exception:
        logger.warning("Проверка меток периодов %s %s/%s не выполнена — периоды как есть",
                       start, router_sn, panel_id, exc_info=True)
        return ([{k: v for k, v in r.items() if k not in ("created_at", "ts_bad")} for r in rows
                 if r[start] < ts_to and (r.get(end) is None or r[end] > ts_from)], [], [])


def _enum_key(p: dict[str, Any]) -> int:
    return p["addr"]


def _fault_key(p: dict[str, Any]) -> tuple[int, int]:
    return (p["addr"], p["bit"])


def _note_periods(table: str, router_sn: str, equip_type: str, panel_id: int,
                  moved: list[tuple[Any, datetime, datetime]]) -> None:
    first = min(moved, key=lambda m: m[1])
    _note_once(
        (table, router_sn, equip_type, panel_id, tuple(sorted((str(k), o) for k, o, _ in moved))),
        "Кривая метка времени %s/%s/%s: %d начал периодов %s перенесено на момент записи "
        "(кадр сбитых часов или метка в будущем), первое %s → %s",
        router_sn, equip_type, panel_id, len(moved), table, first[1].isoformat(), first[2].isoformat(),
    )


# ── Коды неисправностей: постоянный источник enum_history ──────────────────────
# 40012 (LAST_FAULT_CODE) / 40013 (LAST_FAULT_TYPE) — enum-типа. Декодер дублирует
# их и в history_rich, но у той таблицы ретенция ~30 дней: на переразборе старых
# сегментов analog-канал пуст. enum_history — полный и постоянный. Читаем оттуда,
# синтезируем analog-строки и подменяем в history — пайплайн характеристик и
# fault_cleared остаётся без изменений (роли LAST_FAULT_CODE/LAST_FAULT_TYPE, kind=analog).
FAULT_CODE_ADDRS: tuple[int, ...] = (40012, 40013)

def enum_read_addrs(cfg: Any) -> list[int]:
    """Все state-регистры для чтения из enum_history: kind=enum из mapping ∪ коды.

    Широкий ридер (Следователь, Фаза 2): журнал ключа 40010 / RUN_STATE 40011 /
    RunCommand 40599 / топл. соленоида / возбуждения / GensetCB / Сброса / кодов
    40012-40013 — всё, из чего реконструктор строит ленту действий. Коды с
    v5.1.1 объявлены kind=enum (характеристика подсегмента — из периодов, см.
    segmenter._state_characteristics); явное добавление — страховка для карт,
    где они ещё analog.
    """
    return sorted(set(cfg.whitelist_enum) | set(FAULT_CODE_ADDRS))


async def get_fault_periods(
    router_sn: str,
    equip_type: str,
    panel_id: int,
    ts_from: datetime,
    ts_to: datetime,
    fault_addrs: frozenset[int] | None = None,
) -> list[dict[str, Any]]:
    """Периоды активных fault-битов из fault_history, пересекающиеся с [ts_from, ts_to).

    По умолчанию — адреса 40400-40428 (маски активных неисправностей).
    Колонки: addr, bit, fault_start, fault_end, fault_name_ru, fault_name, severity, duration_sec.
    fault_end IS NULL → неисправность активна прямо сейчас.

    Начало, заведённое кадром с кривой меткой, перенесено на момент записи
    (ts_guard.fix_periods). Конец бита, снятого таким кадром, не трогаем: его
    не отличить от настоящего снятия в ту же секунду, а ошибка в другую сторону
    хуже — бит «горел» бы лишние сутки.
    """
    if fault_addrs is None:
        fault_addrs = frozenset(range(40400, 40429))

    async with remote.connection(_get_pool()) as conn:
        rows = await conn.fetch(
            f"""
            SELECT
                f.addr,
                f.bit,
                f.fault_start,
                f.fault_end,
                r.states_json->f.bit::text->>'name_ru'  AS fault_name_ru,
                r.states_json->f.bit::text->>'name'     AS fault_name,
                r.states_json->f.bit::text->>'severity' AS severity,
                EXTRACT(EPOCH FROM (
                    COALESCE(f.fault_end, $6) - f.fault_start
                ))::int AS duration_sec,
                f.created_at,
                {_FAULT_TS_BAD_SQL} AS ts_bad
            FROM fault_history f
            LEFT JOIN register_catalog r
                ON r.equip_type = f.equip_type AND r.addr = f.addr
            WHERE f.router_sn  = $1
              AND f.equip_type = $2
              AND f.panel_id   = $3
              AND f.addr       = ANY($4::int[])
              AND {_FAULT_WINDOW_SQL}
            ORDER BY f.fault_start ASC
            """,
            router_sn, equip_type, panel_id,
            list(fault_addrs),
            ts_from, ts_to,
            ts_guard.SKEW_SEC, ts_guard.RECONNECT_SEC,
        )
    rows = [dict(r) for r in rows]
    out, _, moved = _fix_periods_safe(
        rows, ts_from, ts_to, "fault_start", "fault_end", _fault_key, router_sn, panel_id)
    if moved:
        _note_periods("fault", router_sn, equip_type, panel_id, moved)
        # Длительность — заново только у сдвинутых: у остальных остаётся SQL-овая
        same = {(f["addr"], f["bit"], f["fault_start"], f["fault_end"]) for f in rows}
        for f in out:
            if (f["addr"], f["bit"], f["fault_start"], f["fault_end"]) not in same:
                f["duration_sec"] = round(((f["fault_end"] or ts_to) - f["fault_start"]).total_seconds())
        out.sort(key=lambda f: f["fault_start"])
    return out


# ── Ленты событий: всё, что прислала панель ────────────────────────────────────
# Конфиг аналитики (KB) нужен нарезке и детекторам — порогам и ролям. Ленте он
# не нужен: в шумных событиях часто кроются действия персонала, а регистр,
# который забыли объявить в KB, из неё просто пропадал (монолитный слой
# equipment/ так и остался без команд панели). Источник списка адресов —
# каталог регистров самой БД телеметрии.
_CATALOG_TTL_SEC = 3600
_catalog_cache: dict[str, tuple[float, list[int]]] = {}


async def get_catalog_addrs(equip_type: str) -> list[int]:
    """Все адреса каталога регистров для типа оборудования (кэш на час).

    Каталог общий у всех машин типа и меняется только с прошивкой декодера,
    так что один запрос в час на тип. В enum_history/fault_history по этим
    адресам лежит только то, что есть: аналоговые просто не дадут строк.
    """
    import time
    hit = _catalog_cache.get(equip_type)
    if hit and time.monotonic() - hit[0] < _CATALOG_TTL_SEC:
        return hit[1]
    async with remote.connection(_get_pool()) as conn:
        rows = await conn.fetch(
            "SELECT addr FROM register_catalog WHERE equip_type = $1", equip_type,
        )
    addrs = sorted({int(r["addr"]) for r in rows})
    _catalog_cache[equip_type] = (time.monotonic(), addrs)
    return addrs


async def get_timeline_periods(
    router_sn: str,
    equip_type: str,
    panel_id: int,
    ts_from: datetime,
    ts_to: datetime,
    cfg: Any = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Все enum- и fault-периоды машины за окно — для лент событий.

    Тот же запрос, что у нарезки, только список адресов — каталог БД плюс
    списки KB. (enum_periods, fault_periods). Имена и тяжесть битов — из
    каталога.

    Объединение с KB делает результат гарантированным надмножеством того,
    что читает нарезка, — поэтому её вход можно отфильтровать отсюда, не
    читая окно второй раз. Каталог недоступен — остаются списки KB: хуже
    неполной ленты только её отсутствие.
    """
    try:
        addrs = await get_catalog_addrs(equip_type)
    except Exception:
        logger.warning("Каталог регистров %s не прочитан — лента по KB",
                       equip_type, exc_info=True)
        addrs = []
    kb_enum = set(enum_read_addrs(cfg)) if cfg is not None else set()
    kb_fault = set(cfg.whitelist_fault) if cfg is not None else set()
    enum_addrs = sorted(set(addrs) | kb_enum) or None
    fault_addrs = frozenset(set(addrs) | kb_fault) or None
    # Первая ошибка отменяет соседа — его слот пула свободен сразу (v5.1.24)
    enum_periods, fault_periods = await remote.gather_or_cancel(
        get_enum_periods(router_sn, equip_type, panel_id, ts_from, ts_to,
                         addrs=enum_addrs),
        get_fault_periods(router_sn, equip_type, panel_id, ts_from, ts_to,
                          fault_addrs=fault_addrs),
    )
    return enum_periods, fault_periods


async def get_last_value_before(
    router_sn: str, equip_type: str, panel_id: int, addr: int,
    ts_before: datetime, max_age_sec: float,
) -> dict[str, Any] | None:
    """Последний замер регистра до ts_before, не старше max_age_sec (ts, value).

    Ряд пишется по изменению (при постоянном значении — «пульс» ~15 мин):
    значение держится до следующего замера, и без этого замера окно не знает,
    что было на его начале (v5.1.10, нагрузка перед остановом).

    Строки с меткой дальше SKEW от приёма не берутся: соседей для сверки по
    счётчику у одиночной выборки нет, а кадр застывших часов здесь стал бы
    «последним значением до обрыва» (ts_guard)."""
    from datetime import timedelta as _TD
    async with remote.connection(_get_pool()) as conn:
        row = await conn.fetchrow(
            """
            SELECT ts, value FROM history_rich
            WHERE router_sn = $1 AND equip_type = $2 AND panel_id = $3 AND addr = $4
              AND ts < $5 AND ts >= $6 AND value IS NOT NULL
              AND abs(EXTRACT(EPOCH FROM received_at - ts)) <= $7
            ORDER BY ts DESC, abs(EXTRACT(EPOCH FROM received_at - ts)) ASC LIMIT 1
            """,
            router_sn, equip_type, panel_id, addr, ts_before,
            ts_before - _TD(seconds=max_age_sec), ts_guard.SKEW_SEC,
        )
    return dict(row) if row else None


async def get_last_values_before(
    router_sn: str, equip_type: str, panel_id: int, addrs: list[int],
    ts_before: datetime, max_age_sec: float,
) -> dict[int, dict[str, Any]]:
    """Последний замер каждого регистра до ts_before, не старше max_age_sec:
    {addr: {ts, value}} — одним запросом DISTINCT ON (v5.1.21, П10: удержанные
    значения на голове окна — нагрузка, обороты, частота, температуры, давление).
    Строки с меткой дальше SKEW от приёма не берутся — как в get_last_value_before."""
    from datetime import timedelta as _TD
    if not addrs:
        return {}
    async with remote.connection(_get_pool()) as conn:
        rows = await conn.fetch(
            """
            SELECT DISTINCT ON (addr) addr, ts, value FROM history_rich
            WHERE router_sn = $1 AND equip_type = $2 AND panel_id = $3
              AND addr = ANY($4::int[])
              AND ts < $5 AND ts >= $6 AND value IS NOT NULL
              AND abs(EXTRACT(EPOCH FROM received_at - ts)) <= $7
            ORDER BY addr, ts DESC, abs(EXTRACT(EPOCH FROM received_at - ts)) ASC
            """,
            router_sn, equip_type, panel_id, list(addrs), ts_before,
            ts_before - _TD(seconds=max_age_sec), ts_guard.SKEW_SEC,
        )
    return {int(r["addr"]): {"ts": r["ts"], "value": r["value"]} for r in rows}


async def get_value_histogram(
    router_sn: str, equip_type: str, panel_id: int, addr: int, ts_from: datetime,
) -> list[tuple[float, int]]:
    """Распределение положительных показаний регистра с ts_from: [(значение, число)].

    Для оценки шага показаний датчика машины (v5.1.12): раз в сутки на машину.
    Строки с меткой дальше SKEW от приёма не считаются (ts_guard)."""
    async with remote.connection(_get_pool()) as conn:
        rows = await conn.fetch(
            """
            SELECT value, count(*) AS n FROM history_rich
            WHERE router_sn = $1 AND equip_type = $2 AND panel_id = $3 AND addr = $4
              AND ts >= $5 AND value > 0
              AND abs(EXTRACT(EPOCH FROM received_at - ts)) <= $6
            GROUP BY value
            """,
            router_sn, equip_type, panel_id, addr, ts_from, ts_guard.SKEW_SEC,
        )
    return [(float(r["value"]), int(r["n"])) for r in rows]


async def get_data_gaps(
    router_sn: str,
    equip_type: str,
    panel_id: int,
    ts_from: datetime,
    ts_to: datetime,
) -> list[dict[str, Any]]:
    """Пропуски связи из data_gaps, пересекающиеся с [ts_from, ts_to).

    Колонки: gap_start, gap_end (gap_end IS NULL → активен).
    При таймауте запроса — пробрасывает RuntimeError с просьбой уменьшить интервал.
    Таймаут ожидания слота пула (remote.PoolBusy) и ошибки соединения уходят
    как есть (v5.1.24): пустой список пропусков молча дал бы анализ «без дыр».
    [] — только когда таблицы нет или нет прав на неё.
    Конец в будущем относительно записи (часы роутера впереди) перенесён на
    момент записи (ts_guard.fix_gaps).
    """
    try:
        async with remote.connection(_get_pool()) as conn:
            rows = await conn.fetch(
                """
                SELECT gap_start, gap_end, created_at
                FROM data_gaps
                WHERE router_sn  = $1
                  AND equip_type = $2
                  AND panel_id   = $3
                  AND gap_start < $5
                  AND (gap_end IS NULL OR gap_end > $4)
                ORDER BY gap_start ASC
                """,
                router_sn, equip_type, panel_id,
                ts_from, ts_to,
            )
        out, fixed = ts_guard.fix_gaps([dict(r) for r in rows], ts_from, ts_to)
        if fixed:
            _note_once(
                ("gaps", router_sn, equip_type, panel_id, tuple(fixed)),
                "Кривая метка времени %s/%s/%s: у %d разрывов связи конец в будущем — "
                "перенесён на момент записи, первый с %s",
                router_sn, equip_type, panel_id, len(fixed), min(fixed).isoformat(),
            )
        return out
    except remote.PoolBusy:
        raise
    except asyncio.TimeoutError:
        raise RuntimeError(
            f"Запрос пропусков связи превысил {_QUERY_TIMEOUT_SEC} с. "
            "Уменьшите временной интервал анализа."
        )
    except (asyncpg.UndefinedTableError, asyncpg.InsufficientPrivilegeError):
        logger.warning("data_gaps недоступна — пропуски связи не учтены", exc_info=True)
        return []


def kb_slice(
    enum_periods: list[dict[str, Any]],
    fault_periods: list[dict[str, Any]],
    cfg: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Вход нарезки из полной ленты: только то, что объявлено в KB.

    Порядок сохраняется (enum — по адресу и началу, fault — по началу), так
    что результат совпадает с тем, что дали бы отдельные чтения по KB.
    """
    kb_e = set(enum_read_addrs(cfg))
    kb_f = cfg.whitelist_fault
    return ([p for p in enum_periods if p.get("addr") in kb_e],
            [f for f in fault_periods if f.get("addr") in kb_f])
