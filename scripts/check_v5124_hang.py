# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""Стенд «чёрная дыра» v5.1.24 (план docs/plans/design_v5124_watchdog.md, §6.2).

17.10 ~13:40 запрос движка ДГУ №2 сорвался по command_timeout на деградировавшем
VPN, а возврат соединения в пул ждал подтверждения отмены без срока — движок
молчал до 22.10 09:25. Стенд воспроизводит это на настоящей PostgreSQL через
локальный TCP-прокси и проверяет исправление:

  прокси 127.0.0.1:0 → host:port из DSN. Соединение, в потоке которого клиент
  прислал маркер /*bh*/, замолкает в обе стороны: сокеты открыты, без FIN и
  RST, прокси читает и выбрасывает. Пока «дыра» включена, новые соединения
  принимаются и молчат — так ведёт себя служебное соединение отмены через
  мёртвый VPN. ssl=False, чтобы маркер был виден открытым текстом.

  А. «Как было»: asyncpg.create_pool + pool.acquire() без срока — возврат
     соединения висит (в стеке release), слот занят; отмена задачи слот не
     освобождает (shield).
  Г. Закрытие пула с таким соединением: remote.close_pool — False за срок,
     соединения оборваны.
  Б. Исправление: remote.create_pool + remote.connection — TimeoutError за срок
     запроса, слот свободен сразу; после «дыры» тот же пул отвечает.
  В. Сокет и сессия: SO_KEEPALIVE = 1; statement_timeout — тот же после
     возврата и повторной выдачи соединения (К1); контроль: SET в init
     стирается RESET ALL; statement_timeout снимает pg_sleep на сервере;
     SSL-соединение — keepalive через SSL-транспорт (Р14).
  Д. Сторож целиком: движок читает по-старому и виснет — ERROR со стеком, где
     видно release, перезапуск, новый движок (через помощника) отрабатывает.
  Е. Слот у неизвестного пути: соединение помощника у «сироты» движка —
     сторож обрывает его при перезапуске, слот возвращается.
  Ж. Первое соединение нового пула (ревью W1): init не шлёт запросов — дыра
     на «SHOW statement_timeout» выдачу не задевает; первый запрос в дыре —
     TimeoutError за срок, слот свободен.

Только чтение: default_transaction_read_only = on; запросы — SELECT 1,
SELECT pg_sleep(…), SHOW, pg_backend_pid(); запрос с маркером до сервера не
доходит вовсе. DSN — из файла, не печатается. Аналитическая БД сервиса
(config.yml) не используется: обращения к ней в стенде запрещены заглушкой.

Запуск:  py -3 scripts/check_v5124_hang.py --dsn-file <путь к .dsn>
         без --dsn-file — «Стенд пропущен», код 0.
"""
from __future__ import annotations

import argparse
import asyncio
import faulthandler
import logging
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, urlunparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import asyncpg  # noqa: E402

from config import settings as S  # noqa: E402
from db import remote  # noqa: E402
import db.pool as dbpool  # noqa: E402
import online.db as odb  # noqa: E402
import online.engine as eng  # noqa: E402
import online.manager as mgr  # noqa: E402

RO = {"default_transaction_read_only": "on"}
MARK = b"/*bh*/"

_errors: list[str] = []
_lines: list[str] = []


def check(cond: bool, msg: str) -> None:
    _lines.append(("  OK   " if cond else "  FAIL ") + msg)
    print(_lines[-1], flush=True)
    if not cond:
        _errors.append(msg)


def note(msg: str) -> None:
    _lines.append("       " + msg)
    print(_lines[-1], flush=True)


# ── Журнал — в память ─────────────────────────────────────────────────────────

class _Cap(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.recs: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.recs.append(record)


CAP = _Cap()
_root = logging.getLogger()
for _h in list(_root.handlers):
    _root.removeHandler(_h)
_root.addHandler(CAP)
_root.setLevel(logging.INFO)


def logs(since: int, level: int, sub: str = "") -> list[str]:
    return [r.getMessage() for r in CAP.recs[since:] if r.levelno == level and sub in r.getMessage()]


# Аналитическая БД сервиса (config.yml — рабочий сервер) в стенде запрещена
async def _no_real_db(*a, **k):
    raise RuntimeError("стенд: обращение к аналитической БД сервиса запрещено")

dbpool.acquire_analytics = _no_real_db


# ── Прокси «чёрная дыра» ──────────────────────────────────────────────────────

class BlackHoleProxy:
    def __init__(self, host: str, port: int) -> None:
        self.up_host, self.up_port = host, port
        self.hole = False                 # новые соединения — молчат
        self.marks = [MARK]               # маркеры, по которым соединение замолкает
        self.hole_on_silence = False      # замолчало — и новые соединения молчат
        self.silenced = 0                 # соединений, замолчавших по маркеру
        self.swallowed = 0                # соединений, принятых «дырой»
        self.writers: list[asyncio.StreamWriter] = []
        self.server: asyncio.AbstractServer | None = None
        self.port = 0

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def _sink(self, r: asyncio.StreamReader) -> None:
        try:
            while await r.read(65536):
                pass
        except Exception:
            pass

    async def _handle(self, cr: asyncio.StreamReader, cw: asyncio.StreamWriter) -> None:
        self.writers.append(cw)
        if self.hole:
            self.swallowed += 1
            await self._sink(cr)          # молчим, не закрываем
            return
        try:
            ur, uw = await asyncio.open_connection(self.up_host, self.up_port)
        except Exception:
            cw.close()
            return
        self.writers.append(uw)
        dead = {"v": False}

        async def c2u() -> None:
            tail = b""
            try:
                while True:
                    data = await cr.read(65536)
                    if not data:
                        break
                    if not dead["v"] and any(mk in tail + data for mk in self.marks):
                        dead["v"] = True
                        self.silenced += 1
                        if self.hole_on_silence:
                            self.hole = True      # служебное соединение отмены — тоже в дыру
                    tail = data[-max(len(mk) for mk in self.marks):]
                    if dead["v"]:
                        continue          # читаем и выбрасываем
                    uw.write(data)
                    await uw.drain()
            except Exception:
                pass
            finally:
                # Клиент ушёл (обрыв) — отпустить бэкенд на сервере
                uw.close()

        async def u2c() -> None:
            try:
                while True:
                    data = await ur.read(65536)
                    if not data:
                        break
                    if dead["v"]:
                        continue
                    cw.write(data)
                    await cw.drain()
            except Exception:
                pass
            finally:
                # Сервер закрыл соединение (штатный Terminate) — закрыть и
                # клиенту; замолчавшее по маркеру остаётся молчать
                if not dead["v"]:
                    cw.close()

        await asyncio.gather(c2u(), u2c(), return_exceptions=True)

    async def close(self) -> None:
        for w in self.writers:
            try:
                w.close()
            except Exception:
                pass
        if self.server is not None:
            self.server.close()


def _with_host(dsn: str, host: str, port: int) -> str:
    u = urlparse(dsn)
    auth = ""
    if u.username:
        auth = u.username + (f":{u.password}" if u.password is not None else "") + "@"
    return urlunparse(u._replace(netloc=f"{auth}{host}:{port}", query=""))


def qsize(pool) -> int:
    return pool._queue.qsize()


# ── Сценарии ──────────────────────────────────────────────────────────────────

async def scen_a_g(proxy: BlackHoleProxy, pdsn: str) -> None:
    print("А. «Как было»: pool.acquire() без срока, срыв по command_timeout 2 с", flush=True)
    pool = await asyncpg.create_pool(pdsn, min_size=1, max_size=1, command_timeout=2,
                                     ssl=False, server_settings=RO)
    check(await pool.fetchval("SELECT 1") == 1 and qsize(pool) == 1, "А: пул через прокси отвечает")
    proxy.hole = True

    async def old_way():
        async with pool.acquire() as c:
            await c.fetch("SELECT 1 /*bh*/")

    t = asyncio.create_task(old_way(), name="old_way")
    await asyncio.sleep(12)
    st = remote.task_stack(t)
    check(not t.done(), "А: через 12 с задача не завершена (command_timeout был 2 с)")
    check(any("release" in x for x in st), "А: в цепочке await — release")
    note("цепочка: " + " → ".join(x.split(" ", 1)[-1] for x in st))
    check(qsize(pool) == 0, "А: слот пула занят")
    t.cancel()
    await asyncio.wait({t}, timeout=3)
    check(t.done(), "А: внешняя отмена возвращает управление задаче")
    check(qsize(pool) == 0, "А: но слот остаётся занят (shield) — картина 17.10 и остановки 22.10")

    print("Г. Закрытие пула с зависшим соединением", flush=True)
    m0 = len(CAP.recs)
    t0 = time.monotonic()
    ok = await remote.close_pool(pool, "стенд А", timeout=2)
    dt = time.monotonic() - t0
    check(ok is False and dt <= 3, f"Г: close_pool вернул False за {dt:.1f} с (≤ 3)")
    check(logs(m0, logging.WARNING, "не закрылся за 2 с — соединения оборваны"), "Г: WARNING «не закрылся»")
    check(not logs(m0, logging.WARNING, "taking over 60 seconds"), "Г: предупреждения asyncpg о 60 с нет")
    proxy.hole = False


async def scen_b_v(proxy: BlackHoleProxy, pdsn: str, dsn: str) -> None:
    print("Б. Исправление: remote.create_pool + remote.connection", flush=True)
    pool = await remote.create_pool(pdsn, min_size=0, max_size=1, label="стенд Б",
                                    extra_server_settings=RO, ssl=False)
    async with remote.connection(pool, label="стенд Б") as c:
        check(await c.fetchval("SELECT 1") == 1, "Б: через помощника отвечает")
    proxy.hole = True
    t0 = time.monotonic()
    err = None
    try:
        async with remote.connection(pool, label="стенд Б") as c:
            await c.fetch("SELECT 1 /*bh*/")
    except BaseException as e:      # noqa: BLE001
        err = e
    dt = time.monotonic() - t0
    check(isinstance(err, TimeoutError) and dt <= 3,
          f"Б: TimeoutError за {dt:.1f} с (command_timeout 2, ≤ 3): {type(err).__name__}")
    check(qsize(pool) == 1, "Б: слот свободен сразу (соединение оборвано, а не возвращено)")
    proxy.hole = False
    async with remote.connection(pool, label="стенд Б") as c:
        check(await c.fetchval("SELECT 1") == 1, "Б: после «дыры» тот же пул отвечает")

    print("В. Сокет и сессия", flush=True)
    async with remote.connection(pool, label="стенд Б") as c:
        sock = c._transport.get_extra_info("socket")
        ka = sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE)
        st1 = await c.fetchval("SHOW statement_timeout")
        pid1 = await c.fetchval("SELECT pg_backend_pid()")
    async with remote.connection(pool, label="стенд Б") as c:
        st2 = await c.fetchval("SHOW statement_timeout")
        pid2 = await c.fetchval("SELECT pg_backend_pid()")
        ro = await c.fetchval("SHOW default_transaction_read_only")
    check(ka != 0, "В: SO_KEEPALIVE = 1 на сокете соединения пула")
    check(pid1 == pid2 and st1 == st2 == f"{S.source_statement_timeout}s" and ro == "on",
          f"В: statement_timeout после возврата и повторной выдачи того же соединения — "
          f"{st1} → {st2} (К1), только чтение")
    t0 = time.monotonic()
    err = None
    try:
        async with remote.connection(pool, label="стенд Б") as c:
            await c.fetchval("SELECT pg_sleep(1.5)")
    except asyncpg.QueryCanceledError as e:
        err = e
    dt = time.monotonic() - t0
    check(err is not None and dt < 2 and qsize(pool) == 1,
          f"В: statement_timeout снял pg_sleep на сервере за {dt:.1f} с, слот свободен")
    check(await remote.close_pool(pool, "стенд Б", timeout=2) is True, "В: пул Б закрыт штатно")

    # Контроль К1: SET в init стирается первым же RESET ALL при возврате
    async def _init_set(con):
        await con.execute(f"SET statement_timeout = '{S.source_statement_timeout}s'")

    pc = await asyncpg.create_pool(pdsn, min_size=0, max_size=1, ssl=False, server_settings=RO,
                                   init=_init_set)
    async with pc.acquire() as c:
        a1 = await c.fetchval("SHOW statement_timeout")
    async with pc.acquire() as c:
        a2 = await c.fetchval("SHOW statement_timeout")
    await remote.close_pool(pc, "стенд контроль К1", timeout=2)
    check(a1 == f"{S.source_statement_timeout}s" and a2 == "0",
          f"В: контроль — SET в init: {a1} → {a2} после возврата (почему параметр запуска)")

    # Р14: keepalive через SSL-транспорт (прямое соединение)
    try:
        con = await asyncpg.connect(dsn, ssl="require", server_settings=RO, timeout=10)
    except Exception as e:      # noqa: BLE001
        note(f"В: SSL-соединение не установлено ({type(e).__name__}) — проверка SSL пропущена")
    else:
        try:
            ap = remote.apply_keepalive(con, label="стенд SSL")
            s2 = con._transport.get_extra_info("socket")
            check(ap is not None and s2.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) != 0,
                  f"В: SSL-транспорт ({type(con._transport).__name__}) — SO_KEEPALIVE = 1 (Р14)")
        finally:
            await con.close()


KEY = "6003790403|pcc|2"


async def scen_d(proxy: BlackHoleProxy, pdsn: str) -> None:
    print("Д. Сторож целиком: движок читает по-старому и виснет", flush=True)
    old_pool = await asyncpg.create_pool(pdsn, min_size=1, max_size=1, command_timeout=2,
                                         ssl=False, server_settings=RO)
    new_pool = await remote.create_pool(pdsn, min_size=0, max_size=1, label="стенд Д",
                                        extra_server_settings=RO, ssl=False)
    done = {"n": 0}

    def make(kind: str):
        e = eng.OnlinePollEngine("6003790403", "pcc", 2, "E", cfg=None, poll_interval_sec=0.2)

        async def cycle():
            if kind == "old":
                async with old_pool.acquire() as c:          # как было: без срока
                    await c.fetch("SELECT 1 /*bh*/")
            else:
                async with remote.connection(new_pool, label="стенд Д") as c:
                    if await c.fetchval("SELECT 1") == 1:
                        done["n"] += 1
                        e._open_ok = True

        async def initialize(start_date, allow_gap_fill=False):
            return None

        e._poll_cycle = cycle
        e.initialize = initialize
        return e

    async def build(obs):
        return make("new")

    async def get_obs(r, e, p):
        return {"router_sn": r, "equip_type": e, "panel_id": p, "status": "running",
                "start_date": datetime(2026, 10, 1, tzinfo=timezone.utc), "poll_interval_sec": 0.2}

    odb.get_observation = get_obs
    m = mgr.OnlineManager()
    m._build_engine = build
    first = make("old")
    m._wanted.add(KEY)
    sw0 = proxy.swallowed
    proxy.hole = True
    m0 = len(CAP.recs)
    t0 = time.monotonic()
    m._launch(first)
    m._start_watchdog()
    end = time.monotonic() + 30
    # Старый движок завис, когда «дыра» проглотила служебное соединение отмены;
    # дальше дыру можно выключить — мёртвое соединение и отмена так и висят
    while time.monotonic() < end and proxy.swallowed == sw0:
        await asyncio.sleep(0.05)
    t_hang = time.monotonic()
    proxy.hole = False
    t_err = None
    while time.monotonic() < end and (t_err is None or done["n"] == 0):
        if t_err is None and logs(m0, logging.ERROR, "Сторож[" + KEY):
            t_err = time.monotonic()
        await asyncio.sleep(0.05)
    errs = logs(m0, logging.ERROR, "Сторож[" + KEY)
    txt = errs[0] if errs else ""
    check(bool(errs) and "нет продвижения" in txt and t_err is not None
          and t_err - t0 <= S.watchdog_stall_sec + 2 * S.watchdog_tick_sec + 0.5,
          f"Д: движок завис через {t_hang - t0:.1f} с после старта (срыв по command_timeout 2 с), "
          f"ERROR сторожа — через {(t_err or 0) - t0:.1f} с (stall 3 с + тик 0,5 с)")
    check("Pool.release" in txt and ("wait_for" in txt or "shield" in txt or "ждёт:" in txt),
          "Д: в стеке ERROR видно Pool.release и ожидание")
    for line in txt.splitlines()[:24]:
        note("│ " + line[:150])
    h = m.engine_health(KEY)
    check(first._task.done() and h["restarts"] == 1 and done["n"] >= 1,
          f"Д: перезапуск №1, новый движок (через помощника) отработал цикл: циклов {done['n']}")
    await m.stop_all()
    old_pool.terminate()
    await remote.close_pool(new_pool, "стенд Д", timeout=2)


async def scen_e(pdsn: str) -> None:
    print("Е. Слот у неизвестного пути: соединение помощника у «сироты» движка", flush=True)
    pool = await remote.create_pool(pdsn, min_size=0, max_size=1, label="стенд Е",
                                    extra_server_settings=RO, ssl=False)
    async with remote.connection(pool, label="стенд Е") as c:
        await c.fetchval("SELECT 1")
    q0 = qsize(pool)
    hold = asyncio.Event()
    real_stale = remote.stale_conn_sec
    remote.stale_conn_sec = lambda: 2.0

    def make(kind: str):
        e = eng.OnlinePollEngine("6003790403", "pcc", 2, "E", cfg=None, poll_interval_sec=0.2)

        async def orphan():
            async with remote.connection(pool, label="стенд Е") as c:
                await c.fetchval("SELECT 1")
                await hold.wait()                    # держит слот и не реагирует

        async def cycle():
            if kind == "hold":
                e._orphan = asyncio.create_task(orphan(), name="orphan")   # наследует метку
                await asyncio.Event().wait()         # без пульса
            else:
                e._open_ok = True

        async def initialize(start_date, allow_gap_fill=False):
            return None

        e._poll_cycle = cycle
        e.initialize = initialize
        return e

    async def build(obs):
        return make("ok")

    m = mgr.OnlineManager()
    m._build_engine = build
    first = make("hold")
    m._wanted.add(KEY)
    m._launch(first)
    await asyncio.sleep(0.5)
    q_hold = qsize(pool)
    m0 = len(CAP.recs)
    m._start_watchdog()
    end = time.monotonic() + 20
    while time.monotonic() < end and m.engine_health(KEY)["restarts"] < 1:
        await asyncio.sleep(0.1)
    await asyncio.sleep(0.3)
    check(q_hold == 0 and qsize(pool) == q0 == 1,
          f"Е: слот занят сиротой ({q_hold}) → после перезапуска освобождён ({qsize(pool)})")
    # Обрывает уборщик тика (Р9, ERROR — любой владелец) или сторож при перезапуске
    # (WARNING — соединения этой машины); оба — по возрасту > stale_conn_sec
    by_tick = logs(m0, logging.ERROR, "— оборвано")
    by_restart = logs(m0, logging.WARNING, "оборвано соединений source")
    check(bool(by_tick or by_restart),
          f"Е: соединение сироты оборвано — {'уборщиком тика' if by_tick else 'при перезапуске'}: "
          f"{(by_tick or by_restart)[0][:110] if (by_tick or by_restart) else '—'}")
    remote.stale_conn_sec = real_stale
    hold.set()
    await m.stop_all()
    await remote.close_pool(pool, "стенд Е", timeout=2)


async def scen_zh(proxy: BlackHoleProxy, pdsn: str) -> None:
    print("Ж. Первое соединение нового пула: в init нет запросов (ревью W1)", flush=True)
    # Было: init делал SHOW statement_timeout на первом соединении — дыра на нём
    # (и на служебном соединении отмены) вешала acquire без срока, слот занят
    proxy.marks = [MARK, b"SHOW statement_timeout"]
    proxy.hole_on_silence = True
    sil0 = proxy.silenced
    m0 = len(CAP.recs)
    pool = await remote.create_pool(pdsn, min_size=0, max_size=1, label="стенд Ж",
                                    extra_server_settings=RO, ssl=False)

    async def first() -> int:
        async with remote.connection(pool, label="стенд Ж") as c:
            return await c.fetchval("SELECT 1")

    t0 = time.monotonic()
    t = asyncio.create_task(first(), name="first_conn")
    done, _ = await asyncio.wait({t}, timeout=10)
    dt = time.monotonic() - t0
    if not done:
        note("висит: " + " → ".join(x.split(" ", 1)[-1] for x in remote.task_stack(t)))
        t.cancel()
        await asyncio.wait({t}, timeout=3)
    check(bool(done) and not t.cancelled() and t.exception() is None and t.result() == 1
          and proxy.silenced == sil0 and dt <= 3,
          f"Ж: первая выдача нового пула при «дыре» на SHOW — ответ за {dt:.1f} с, init "
          f"запросов не шлёт (замолчало соединений: {proxy.silenced - sil0})")
    info = logs(m0, logging.INFO, "стенд Ж:")
    check(bool(info) and f"statement_timeout {S.source_statement_timeout}s" in info[0],
          f"Ж: INFO пула — statement_timeout по параметру запуска: {info[0] if info else '—'}")
    await remote.close_pool(pool, "стенд Ж", timeout=2)

    # Первый же запрос первого соединения уходит в дыру (и отмена — тоже):
    # TimeoutError за срок запроса, слот свободен сразу
    proxy.marks = [MARK]
    proxy.hole = False
    pool2 = await remote.create_pool(pdsn, min_size=0, max_size=1, label="стенд Ж2",
                                     extra_server_settings=RO, ssl=False)
    t0 = time.monotonic()
    err = None
    try:
        async with remote.connection(pool2, label="стенд Ж2") as c:
            await c.fetch("SELECT 1 /*bh*/")
    except BaseException as e:      # noqa: BLE001
        err = e
    dt = time.monotonic() - t0
    check(isinstance(err, TimeoutError) and dt <= 3 and qsize(pool2) == 1,
          f"Ж: первый запрос нового пула в «дыре» — {type(err).__name__} за {dt:.1f} с, слот свободен")
    proxy.hole = False
    proxy.hole_on_silence = False
    async with remote.connection(pool2, label="стенд Ж2") as c:
        check(await c.fetchval("SELECT 1") == 1, "Ж: после «дыры» тот же пул отвечает")
    await remote.close_pool(pool2, "стенд Ж2", timeout=2)


async def main(dsn: str) -> None:
    u = urlparse(dsn)
    proxy = BlackHoleProxy(u.hostname, u.port or 5432)
    await proxy.start()
    pdsn = _with_host(dsn, "127.0.0.1", proxy.port)
    print(f"Прокси 127.0.0.1:{proxy.port} → {u.hostname}:{u.port or 5432} (DSN не печатается)", flush=True)
    # Короткие сроки стенда
    S.source_query_timeout = 2
    S.source_acquire_timeout = 3
    S.source_release_timeout = 2
    S.source_connect_timeout = 5
    S.source_statement_timeout = 1
    S.watchdog_stall_sec = 3
    S.watchdog_tick_sec = 0.5
    S.watchdog_cancel_wait_sec = 2
    S.watchdog_cycle_warn_sec = 60
    # Порог без продвижения в бою не меньше 3 × опрос + 60 с — стенду он мешает
    mgr.OnlineManager._stall_limit = staticmethod(lambda e: S.watchdog_stall_sec)
    try:
        await scen_a_g(proxy, pdsn)
        await scen_b_v(proxy, pdsn, dsn)
        await scen_d(proxy, pdsn)
        await scen_e(pdsn)
        await scen_zh(proxy, pdsn)
        note(f"прокси: замолчало по маркеру {proxy.silenced}, принято «дырой» {proxy.swallowed}")
    finally:
        await proxy.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--dsn-file", help="файл с DSN тестовой PostgreSQL (только чтение)")
    args = ap.parse_args()
    if not args.dsn_file:
        print("Стенд пропущен (нет --dsn-file)")
        sys.exit(0)
    _dsn = Path(args.dsn_file).read_text(encoding="utf-8").strip()
    faulthandler.dump_traceback_later(300, exit=True)
    t_all = time.monotonic()
    try:
        asyncio.run(main(_dsn))
    except Exception as _e:      # noqa: BLE001
        import traceback
        check(False, f"стенд упал: {_e!r}\n{traceback.format_exc()}")
    print(f"Стенд: {time.monotonic() - t_all:.0f} с")
    if _errors:
        print(f"FAIL — {len(_errors)} расхождений:")
        for x in _errors:
            print(f"  • {x}")
        sys.exit(1)
    print("Все проверки пройдены.")
