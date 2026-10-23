# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""v5.1.24 «Надзор за движками» (план docs/plans/design_v5124_watchdog.md,
решение владельца 23.10.2026 «делай всё», критика — раздел 12).

17.10 ~13:40 движок ДГУ №2 завис в asyncpg (после срыва запроса соединение
возвращалось в пул и ждало подтверждения отмены без срока) и молчал до 22.10
09:25. Проверяется без БД, на подменах:

1. db/remote.connection: сроки ожидания слота и возврата; при ошибке / отмене —
   обрыв, а не возврат; учёт выданных и владелец из ContextVar (наследуется
   gather); пульс владельца; terminate_stale только своих и старых; PoolBusy —
   «пул занят» и «нет подключения»; сбой возврата после успеха не роняет строки.
2. Keepalive и сессия: набор setsockopt (Linux), без констант TCP_*, без
   сокета — одно WARNING; настоящий сокет — SO_KEEPALIVE = 1; statement_timeout
   — параметром запуска (К1: SET в init стирается RESET ALL), INFO по факту.
3. gather_or_cancel: порядок, отмена соседей, исходное исключение, внешняя отмена.
4. Учёт цикла движка: длительности, ошибки подряд, WARNING долгого цикла, пульс,
   live_ok_at только по открытому окну (К6: N+1-цикл его не двигает), stop()
   со сроком.
5. Сторож: стек на 2+ уровня ниже run (К2), перезапуск только этой машины,
   лимит и подъём после окна, зомби, колбэк старой задачи, неожиданная отмена
   и ошибка задачи, СТОП во время зависания, замок ПУСК, К3 (ПУСК между тиком
   и замком), упавший старт, enabled=false, эпоха гейта и поставщик движка
   (Р11), is_running, К4 (срок под замком), К5 (ПУСК при зомби, остановка
   посреди подъёма, _launch при остановке), К8 и Р6 (долгий цикл, потолок).
6. API: health_fields и порог «не обновляется» (К7), поля маршрутов.
7. Остановка: close_pool с «вечным» close(), stop_all — сторож раньше движков,
   движки параллельно.
8. history_sync: тик со сроком, command_timeout пула.
9. Конфиг: умолчания §2.2, config.example.yml, statement_timeout поправляется.
10. Статус-тик: вход в «устарело» — одно WARNING, выход — одно INFO, запросов не больше.
11. Регрессия: у удалённых пулов нет выдачи в обход db/remote.

Ревью кода 24.10 (W1–W12): init пула без запросов (W1); стек сквозь
@asynccontextmanager и coroutine_wrapper (W2); потолок считает продвижением
завершённые вызовы source и не снимает живой движок при лимите (W3); упавший
_build_engine при старте — под надзором (W4); ERROR потолка при выключенном
стороже — раз на эпизод (W5); нарезка в потоке — без перезапуска, отмена ждёт
поток (W6); analytics_stale при недостоверном data_stale (W7); /online — догонка
только при свежем пульсе (W8); needs_start, restart_limit для dead,
engine_auto_restart (W9); install.sh (W10); шаблон конфига (W11); план (W12).

Стенд «чёрная дыра» с настоящей БД — scripts/check_v5124_hang.py.

Запуск:  py -3 scripts/test_watchdog_v5124.py
"""
from __future__ import annotations

import ast
import asyncio
import faulthandler
import inspect
import logging
import socket
import sys
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import asyncpg  # noqa: E402
import yaml  # noqa: E402

import config as cfgmod  # noqa: E402
from config import settings as S  # noqa: E402
from db import remote  # noqa: E402
import db.analytics as dba  # noqa: E402
import online.db as odb  # noqa: E402
import online.engine as eng  # noqa: E402
import online.manager as mgr  # noqa: E402

_errors: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        _errors.append(msg)


# Страховка: тест про зависания сам зависнуть не должен — дамп стеков и выход
faulthandler.dump_traceback_later(240, exit=True)

# Тест без БД: любое обращение к настоящей аналитической БД (config.yml указывает
# на рабочий сервер) — сразу ошибка и отметка; в конце проверяется, что их не было
import db.pool as _dbpool  # noqa: E402
_DB_HITS: list[str] = []


async def _no_real_db(*a, **k):
    import traceback as _tb
    _DB_HITS.append("".join(_tb.format_stack(limit=6)))
    raise RuntimeError("тест: обращение к настоящей БД запрещено")

_dbpool.acquire_analytics = _no_real_db


# ── Журнал: всё — в память, без вывода ────────────────────────────────────────

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
_root.setLevel(logging.DEBUG)
logging.getLogger("asyncio").setLevel(logging.WARNING)


def mark() -> int:
    return len(CAP.recs)


def logs(since: int = 0, level: int | None = None, sub: str | None = None) -> list[str]:
    out = []
    for r in CAP.recs[since:]:
        if level is not None and r.levelno != level:
            continue
        msg = r.getMessage()
        if sub is not None and sub not in msg:
            continue
        out.append(msg)
    return out


def recs(since: int = 0, level: int | None = None, sub: str | None = None) -> list:
    return [r for r in CAP.recs[since:]
            if (level is None or r.levelno == level) and (sub is None or sub in r.getMessage())]


async def until(cond, timeout: float = 5.0, step: float = 0.02) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(step)
    return bool(cond())


# Короткие сроки для тестов (умолчания проверяются в разделе 9 отдельно)
DEFAULTS = {k: getattr(S, k) for k in dir(S) if k.startswith(("watchdog_", "source_", "pool_",
                                                                 "history_sync_"))}
check(DEFAULTS["source_acquire_timeout"] == 120 and DEFAULTS["source_release_timeout"] == 10
      and DEFAULTS["watchdog_stall_sec"] == 600 and DEFAULTS["watchdog_cancel_wait_sec"] == 15,
      f"0: умолчания на месте до подмены: {DEFAULTS}")


# ══ 1. db/remote.connection ══════════════════════════════════════════════════

class FakeProxy:
    def __init__(self, pool) -> None:
        self.pool = pool
        self.terminated = 0
        self.detached = False

    def terminate(self) -> None:
        if self.detached:
            raise asyncpg.InterfaceError("cannot call Connection.terminate(): connection has "
                                         "been released back to the pool")
        self.terminated += 1

    async def fetch(self, q):
        return [q]


class FakePool:
    def __init__(self, acquire_delay: float = 0.0, acquire_exc=None, release_exc=None) -> None:
        self.acq: list = []
        self.rel: list = []
        self.acquire_delay = acquire_delay
        self.acquire_exc = acquire_exc
        self.release_exc = release_exc
        self.last: FakeProxy | None = None

    async def acquire(self, timeout=None):
        self.acq.append(timeout)
        if self.acquire_delay:
            await asyncio.wait_for(asyncio.sleep(self.acquire_delay), timeout)
        if self.acquire_exc is not None:
            raise self.acquire_exc
        self.last = FakeProxy(self)
        return self.last

    async def release(self, conn, timeout=None):
        self.rel.append(timeout)
        if self.release_exc is not None:
            raise self.release_exc


BEATS: list[str] = []


async def sec1() -> None:
    # (а) успех
    p = FakePool()
    async with remote.connection(p) as c:
        rows = await c.fetch("SELECT 1")
    check(p.acq == [120.0] and p.rel == [10.0] and c.terminated == 0 and rows == ["SELECT 1"],
          f"1а: acquire(120) и release(10): {p.acq} {p.rel}")

    # (б) TimeoutError в блоке → обрыв, возврата нет, исключение то же
    p = FakePool()
    err = TimeoutError("command_timeout")
    got = None
    try:
        async with remote.connection(p) as c:
            raise err
    except TimeoutError as e:
        got = e
    check(got is err and p.last.terminated == 1 and p.rel == [],
          f"1б: таймаут — обрыв без возврата, исключение то же: {got!r} rel={p.rel}")

    # (в) отмена задачи внутри блока → обрыв и CancelledError
    p = FakePool()
    started = asyncio.Event()

    async def body():
        async with remote.connection(p):
            started.set()
            await asyncio.sleep(3600)

    t = asyncio.create_task(body())
    await started.wait()
    t.cancel()
    try:
        await t
        cancelled = False
    except asyncio.CancelledError:
        cancelled = True
    check(cancelled and p.last.terminated == 1 and p.rel == [], "1в: отмена — обрыв и CancelledError")

    # (г) учёт: запись внутри блока, владелец из ContextVar наследуется gather
    BEATS.clear()
    tag = remote.EngineTag("SN|pcc|2", lambda: BEATS.append("beat"))
    tok = remote.bind_owner(tag)
    try:
        inside: list = []

        async def child():
            async with remote.connection(FakePool(), label="source-БД"):
                inside.append(remote.held(tag))
                inside.append(remote.held("SN|pcc|2"))
                inside.append(remote.held("другая"))

        await remote.gather_or_cancel(child())
        check(len(inside[0]) == 1 and inside[0][0]["owner"] == "SN|pcc|2"
              and inside[0][0]["label"] == "source-БД" and len(inside[1]) == 1 and inside[2] == [],
              f"1г: запись с владельцем из ContextVar (дочерняя задача gather): {inside}")
        check(remote.held(tag) == [], "1г: после блока записи нет")
        # (д) пульс: на выдаче и на выходе (успех и ошибка)
        check(BEATS == ["beat", "beat"], f"1д: пульс на выдаче и на выходе: {BEATS}")
        BEATS.clear()
        try:
            async with remote.connection(FakePool()):
                raise ValueError("x")
        except ValueError:
            pass
        check(BEATS == ["beat", "beat"], f"1д: пульс и при ошибке: {BEATS}")
        BEATS.clear()
        try:
            async with remote.connection(FakePool(acquire_exc=OSError("refused"))):
                pass
        except OSError:
            pass
        check(BEATS == ["beat"], f"1д: отказ подключения — тоже пульс: {BEATS}")
    finally:
        remote.reset_owner(tok)

    # (е) terminate_stale — только свои и старше порога
    tag_a = remote.EngineTag("A", lambda: None)
    tag_b = remote.EngineTag("B", lambda: None)
    hold = asyncio.Event()
    conns: dict = {}

    async def holder(tag, name):
        tok2 = remote.bind_owner(tag)
        try:
            async with remote.connection(FakePool()) as c:
                conns[name] = c
                await hold.wait()
        except Exception:
            pass
        finally:
            remote.reset_owner(tok2)

    ta = asyncio.create_task(holder(tag_a, "a"))
    tb = asyncio.create_task(holder(tag_b, "b"))
    await until(lambda: len(conns) == 2)
    for e in remote._held.values():
        if e["owner"] is tag_a:
            e["since"] -= 1000
    n_young = remote.terminate_stale(tag_a, 2000)
    n_a = remote.terminate_stale(tag_a, 500)
    check(n_young == [] and len(n_a) == 1 and n_a[0]["owner"] == "A"
          and conns["a"].terminated == 1 and conns["b"].terminated == 0,
          f"1е: terminate_stale — только свои старше порога: {n_young} {n_a}")
    check(len(remote.held(tag_b)) == 1 and remote.held(tag_a) == [], "1е: учёт: снята только своя")
    hold.set()
    await asyncio.gather(ta, tb)

    # (ж) обрыв у отсоединённого прокси не роняет
    px = FakeProxy(None)
    px.detached = True
    remote._terminate_quietly(px)
    check(True, "1ж")

    # PoolBusy: слот не освободился за срок / нет подключения
    S.source_acquire_timeout = 0.3
    try:
        try:
            async with remote.connection(FakePool(acquire_delay=5)):
                pass
            busy = None
        except remote.PoolBusy as e:
            busy = e
        check(busy is not None and isinstance(busy, TimeoutError) and "занят" in str(busy),
              f"1з: пул занят за срок — PoolBusy(TimeoutError): {busy!r}")
        S.source_acquire_timeout = 30
        try:
            async with remote.connection(FakePool(acquire_exc=TimeoutError()), label="source-БД"):
                pass
            busy = None
        except remote.PoolBusy as e:
            busy = e
        check(busy is not None and "нет подключения к source-БД" in str(busy),
              f"1з: таймаут подключения — «нет подключения», не «пул занят» (Р1): {busy!r}")
    finally:
        S.source_acquire_timeout = DEFAULTS["source_acquire_timeout"]

    # Р2: возврат после успешного запроса не уложился — строки всё равно отданы
    m0 = mark()
    p = FakePool(release_exc=TimeoutError())
    async with remote.connection(p, label="source-БД") as c:
        rows = await c.fetch("q")
    check(rows == ["q"] and logs(m0, logging.WARNING, "не вернулось в пул"),
          f"1и: сбой возврата — WARNING, строки отданы: {logs(m0)}")
    p = FakePool(release_exc=asyncpg.InterfaceError("pool is closed"))
    async with remote.connection(p) as c:
        rows = await c.fetch("q")
    check(rows == ["q"], "1и: закрытый пул при возврате — не ошибка")

    # Р17: пул диагностического скрипта с command_timeout 600 — слот ждём не меньше
    check(remote._acquire_timeout(NS(_connect_kwargs={"command_timeout": 600})) == 600
          and remote._acquire_timeout(NS(_connect_kwargs={"command_timeout": 120})) == 120
          and remote._acquire_timeout(FakePool()) == 120,
          "1к: срок слота = max(source_acquire_timeout, command_timeout пула)")


asyncio.run(sec1())


# ── Ревью W2: цепочка await сквозь @asynccontextmanager и coroutine_wrapper ──

class _AwaitCtx:
    """Как PoolAcquireContext asyncpg: `await ctx` → coroutine_wrapper."""

    def __init__(self, coro) -> None:
        self.coro = coro

    def __await__(self):
        return self.coro.__await__()


async def _w2_deep(ev):
    await _w2_deepest(ev)


async def _w2_deepest(ev):
    await ev.wait()


class DeepPool:
    """Пул, у которого acquire или release висит тремя уровнями ниже."""

    def __init__(self, hang_in: str) -> None:
        self.hang_in = hang_in
        self.ev = asyncio.Event()

    def acquire(self, timeout=None):
        return _AwaitCtx(self._acquire_deep())

    async def _acquire_deep(self):
        if self.hang_in == "acquire":
            await _w2_deep(self.ev)
        return FakeProxy(self)

    async def release(self, conn, timeout=None):
        if self.hang_in == "release":
            await _w2_deep(self.ev)


async def sec1_review() -> None:
    for stage in ("acquire", "release"):
        p = DeepPool(stage)

        async def user():
            async with remote.connection(p, label="source-БД"):
                pass

        t = asyncio.create_task(user())
        await asyncio.sleep(0.02)
        st = remote.task_stack(t)
        txt = "\n".join(st)
        own = [x for x in st if "remote.py:" in x and x.endswith(" connection")]
        check(own and "_w2_deepest" in txt and "async_generator_asend [" not in txt
              and (stage != "acquire" or "_acquire_deep" in txt)
              and (stage != "release" or "DeepPool.release" in txt),
              f"W2: висит в {stage} внутри remote.connection — в стеке строка помощника и "
              f"всё ниже (сквозь asynccontextmanager{' и coroutine_wrapper' if stage == 'acquire' else ''}):"
              f"\n{txt}")
        p.ev.set()
        await asyncio.wait_for(t, 2)

    # W3: завершённые удалённые вызовы — счётчик владельца (успех и отказ), поток — нет
    tag = remote.EngineTag("C|pcc|1", lambda: None)
    tok = remote.bind_owner(tag)
    try:
        async with remote.connection(FakePool()):
            pass
        check(tag.calls == 1, f"W3: блок connection — вызов +1: {tag.calls}")
        try:
            async with remote.connection(FakePool(acquire_exc=OSError("refused"))):
                pass
        except OSError:
            pass
        r = await remote.to_thread_beating(lambda: 7)
        check(tag.calls == 2 and r == 7, f"W3: отказ подключения +1, поток нарезки — нет: {tag.calls}")
    finally:
        remote.reset_owner(tok)

    # W6: отмена to_thread_beating дожидается конца потока (задача — зомби до него)
    import threading
    tag6 = remote.EngineTag("T6|pcc|1", lambda: None)
    started = threading.Event()

    def work():
        started.set()
        time.sleep(0.6)
        return 42

    async def body():
        tok6 = remote.bind_owner(tag6)
        try:
            return await remote.to_thread_beating(work, interval=0.05)
        finally:
            remote.reset_owner(tok6)

    t = asyncio.create_task(body())
    await until(started.is_set, 2)
    th = remote.threads(tag6)
    check(len(th) == 1 and th[0]["owner"] == "T6|pcc|1" and th[0]["label"] == "нарезка"
          and remote.threads("T6|pcc|1") == th and remote.threads("другая") == [],
          f"W6: поток нарезки в учёте владельца: {th}")
    t.cancel()
    await asyncio.sleep(0.15)
    alive1 = not t.done()
    t.cancel()                                  # повторная отмена — тоже ждёт поток
    await asyncio.sleep(0.05)
    alive2 = not t.done()
    await asyncio.wait({t}, timeout=2)
    check(alive1 and alive2 and t.done() and t.cancelled() and remote.threads(tag6) == [],
          f"W6: отмена дожидается потока (не бросает его), затем CancelledError: "
          f"{alive1} {alive2} {t.done()}")

asyncio.run(sec1_review())


# ══ 2. Keepalive, сессия ═════════════════════════════════════════════════════

class FakeSock:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def setsockopt(self, level, opt, val) -> None:
        self.calls.append((level, opt, val))


def fake_con(sock, stmt="110s", fail_show=False):
    async def fetchval(q):
        if fail_show:
            raise RuntimeError("нет прав")
        return stmt

    async def execute(q, *a):
        EXECUTED.append(q)

    return NS(_transport=NS(get_extra_info=lambda name: sock if name == "socket" else None),
              fetchval=fetchval, execute=execute)


EXECUTED: list = []
LINUX = NS(SOL_SOCKET=1, SO_KEEPALIVE=9, IPPROTO_TCP=6, TCP_KEEPIDLE=4, TCP_KEEPINTVL=5,
           TCP_KEEPCNT=6, TCP_USER_TIMEOUT=18)
_real_socket = remote.socket
remote.socket = LINUX
try:
    fs = FakeSock()
    ap = remote.apply_keepalive(fake_con(fs))
    check(fs.calls == [(1, 9, 1), (6, 4, 60), (6, 5, 10), (6, 6, 6), (6, 18, 120000)]
          and ap == {"SO_KEEPALIVE": 1, "TCP_KEEPIDLE": 60, "TCP_KEEPINTVL": 10, "TCP_KEEPCNT": 6,
                     "TCP_USER_TIMEOUT": 120},
          f"2: Linux — keepalive 60/10×6 и TCP_USER_TIMEOUT 120000 мс: {fs.calls}")
    check(remote.describe_applied(ap, "110s")
          == "keepalive 60/10×6 с, TCP_USER_TIMEOUT 120 с, statement_timeout 110s",
          f"2: INFO по факту: {remote.describe_applied(ap, '110s')}")
    remote.socket = NS(SOL_SOCKET=1, SO_KEEPALIVE=9, IPPROTO_TCP=6)
    fs = FakeSock()
    ap = remote.apply_keepalive(fake_con(fs))
    check(fs.calls == [(1, 9, 1)] and ap == {"SO_KEEPALIVE": 1}
          and "TCP_USER_TIMEOUT недоступен (не Linux)" in remote.describe_applied(ap, None),
          f"2: без констант TCP_* — только SO_KEEPALIVE: {fs.calls}")
    # init-колбэк: keepalive на каждое соединение, INFO — один раз на пул, без SET
    remote.socket = LINUX
    EXECUTED.clear()
    m0 = mark()
    init = remote._make_init("source-БД")
    fs1, fs2 = FakeSock(), FakeSock()
    asyncio.run(init(fake_con(fs1)))
    asyncio.run(init(fake_con(fs2)))
    info = logs(m0, logging.INFO, "source-БД:")
    check(len(info) == 1 and "keepalive 60/10×6 с, TCP_USER_TIMEOUT 120 с, statement_timeout 110s"
          in info[0] and fs1.calls and fs2.calls and EXECUTED == [],
          f"2: init — keepalive каждому, INFO один раз, SET не выполняется (К1): {info} {EXECUTED}")
    # W1 (ревью): в init ни одного запроса — SHOW на первом соединении шёл внутри
    # acquire, и его срыв уводил asyncpg в close() без срока. Соединение, у
    # которого любой запрос висит вечно, init проходит сразу
    m0 = mark()
    _shows: list = []

    async def _eternal(q, *a, **k):
        _shows.append(q)
        await asyncio.Event().wait()

    _hang_con = fake_con(FakeSock())
    _hang_con.fetchval = _hang_con.fetch = _hang_con.execute = _eternal

    async def _init_hang():
        await asyncio.wait_for(remote._make_init("lbl2")(_hang_con), 1)

    _t_w1 = time.monotonic()
    asyncio.run(_init_hang())
    check(not _shows and time.monotonic() - _t_w1 < 0.5
          and any("statement_timeout 110s" in x for x in logs(m0, logging.INFO, "lbl2")),
          f"2: W1 — init без запросов к соединению (вечный SHOW не задевает выдачу): {_shows}")
    m0 = mark()
    asyncio.run(remote._make_init("lbl3", {"statement_timeout": "1000",
                                           "default_transaction_read_only": "on"})(fake_con(FakeSock())))
    asyncio.run(remote._make_init("lbl4", {})(fake_con(FakeSock())))
    check(any("statement_timeout 1s" in x for x in logs(m0, logging.INFO, "lbl3"))
          and any("statement_timeout не задан" in x for x in logs(m0, logging.INFO, "lbl4"))
          and remote._stmt_text({"statement_timeout": "1500"}) == "1500ms",
          f"2: W1 — в INFO statement_timeout параметра запуска пула: {logs(m0, logging.INFO)}")
finally:
    remote.socket = _real_socket

m0 = mark()
remote._warned.discard("nosock:poolX")
check(remote.apply_keepalive(NS(), label="poolX") is None
      and remote.apply_keepalive(NS(_transport=None), label="poolX") is None
      and len(logs(m0, logging.WARNING, "poolX: keepalive не установлен")) == 1,
      "2: нет сокета — None и одно WARNING на пул")

_rs = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    remote.apply_keepalive(fake_con(_rs), label="real")
    check(_rs.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) != 0,
          "2: настоящий сокет ОС — SO_KEEPALIVE включён")
finally:
    _rs.close()

check(remote.server_settings() == {"statement_timeout": "110000"},
      f"2: statement_timeout — параметр запуска сессии: {remote.server_settings()}")
S.source_statement_timeout = 0
check(remote.server_settings() == {}, "2: 0 — не ставить")
S.source_statement_timeout = DEFAULTS["source_statement_timeout"]

_CP: list[dict] = []
_real_create_pool = asyncpg.create_pool


async def _fake_create_pool(dsn, **kw):
    _CP.append(kw)
    return NS(dsn=dsn)

asyncpg.create_pool = _fake_create_pool
try:
    asyncio.run(remote.create_pool("postgresql://x/y", min_size=0, max_size=3, label="source-БД"))
finally:
    asyncpg.create_pool = _real_create_pool
check(_CP and _CP[0]["command_timeout"] == 120 and _CP[0]["min_size"] == 0
      and _CP[0]["timeout"] == 30 and _CP[0]["server_settings"] == {"statement_timeout": "110000"}
      and callable(_CP[0]["init"]),
      f"2: create_pool — command_timeout, срок подключения, statement_timeout, init: {_CP}")
_m2 = mark()
asyncio.run(_CP[0]["init"](fake_con(FakeSock())))
check(any("statement_timeout 110s" in x for x in logs(_m2, logging.INFO, "source-БД:")),
      f"2: init пула — INFO по его параметрам запуска (W1): {logs(_m2, logging.INFO)}")
_src_txt = (ROOT / "analytics" / "source.py").read_text(encoding="utf-8")
check("min_size=0" in _src_txt and "remote.create_pool(" in _src_txt
      and "SET statement_timeout" not in (ROOT / "db" / "remote.py").read_text(encoding="utf-8"),
      "2: пул source — min_size=0 через remote.create_pool; SET в init нет (К1)")


# ══ 3. gather_or_cancel ══════════════════════════════════════════════════════

async def sec3() -> None:
    async def val(v, d):
        await asyncio.sleep(d)
        return v

    r = await remote.gather_or_cancel(val(1, 0.03), val(2, 0.01), val(3, 0.0))
    check(r == [1, 2, 3], f"3: порядок результатов: {r}")

    flags: list[str] = []

    async def slow(name):
        try:
            await asyncio.sleep(3600)
        finally:
            flags.append(name)

    async def boom():
        await asyncio.sleep(0.01)
        raise KeyError("исходное")

    try:
        await remote.gather_or_cancel(slow("a"), boom(), slow("b"))
        got = None
    except BaseException as e:      # noqa: BLE001
        got = e
    check(isinstance(got, KeyError) and sorted(flags) == ["a", "b"],
          f"3: ошибка одного — соседи отменены, наружу исходное (не группа): {got!r} {flags}")

    flags.clear()
    outer = asyncio.create_task(remote.gather_or_cancel(slow("x"), slow("y")))
    await asyncio.sleep(0.02)
    outer.cancel()
    try:
        await outer
        oc = False
    except asyncio.CancelledError:
        oc = True
    check(oc and sorted(flags) == ["x", "y"], f"3: внешняя отмена отменяет и дожидается детей: {flags}")

asyncio.run(sec3())


# ══ 4. Учёт цикла движка ═════════════════════════════════════════════════════

S.watchdog_cancel_wait_sec = 0.2
S.watchdog_cycle_warn_sec = 0.05


def new_engine(key="SN|pcc|1", poll=0.01):
    r, e, p = key.split("|")
    return eng.OnlinePollEngine(r, e, int(p), "E", cfg=None, poll_interval_sec=poll)


async def sec4() -> None:
    e = new_engine()
    seq = ["ok", "raise", "nplus1", "open_failed", "slow", "ok"]
    seen_beats: list[float] = []

    async def cycle():
        seen_beats.append(e.beat_mono)
        kind = seq[e.cycles] if e.cycles < len(seq) else "stop"
        if kind == "ok":
            e._open_ok = True
        elif kind == "raise":
            raise RuntimeError("сбой source")
        elif kind == "open_failed":
            e._open_failed = True
            e._note_cycle_error(TimeoutError("open"))
        elif kind == "slow":
            e._open_ok = True
            await asyncio.sleep(0.08)
        elif kind == "stop":
            e._running = False
        HIST.append((kind, e.live_ok_at, e.cycles_failed_streak))

    HIST: list = []
    e._poll_cycle = cycle
    t0 = time.monotonic()
    m0 = mark()
    e._task = asyncio.create_task(e.run())
    await until(lambda: e.cycles >= len(seq) + 1, 5)
    e._running = False
    await asyncio.wait_for(e._task, 2)
    st = e.started_at
    check(e.cycles >= len(seq) and e.max_cycle_sec >= 0.08 and e.last_cycle_sec is not None,
          f"4: учёт длительности: cycles={e.cycles} max={e.max_cycle_sec}")
    check(all(b >= t0 for b in seen_beats), "4: пульс в начале цикла")
    check(e.beat_mono >= t0 and time.monotonic() - e.beat_mono < 1, "4: пульс в конце цикла")
    check(logs(m0, logging.WARNING, "цикл шёл"), f"4: долгий цикл — WARNING: {logs(m0, logging.WARNING)}")
    check(logs(m0, logging.ERROR, "ошибка цикла"), "4: ошибка цикла — в журнал")
    check(e.cycles_failed_streak == 0 and e.last_cycle_error is None,
          f"4: успешный цикл обнуляет счёт ошибок: {e.cycles_failed_streak} {e.last_cycle_error}")
    check(e.live_ok_at > st, "4: живой анализ обновлён")

    # Строки по циклам: N+1 не двигает live_ok_at; ошибка открытого окна — счёт +1
    # (HIST пишется до учёта цикла, поэтому смотрим через пару циклов)
    e2 = new_engine()
    lk: list = []

    async def c2():
        k = e2.cycles
        lk.append((e2.live_ok_at, e2.cycles_failed_streak, e2.last_cycle_error))
        if k == 0:
            e2._open_ok = True
        elif k == 1:
            pass                               # N+1 / нечего обрабатывать
        elif k == 2:
            e2._open_failed = True
            e2._note_cycle_error(TimeoutError("open"))
        elif k == 3:
            raise RuntimeError("цикл")
        else:
            e2._running = False

    e2._poll_cycle = c2
    e2._task = asyncio.create_task(e2.run())
    await asyncio.wait_for(e2._task, 3)
    # lk[i] — состояние ДО учёта цикла i, т. е. после цикла i-1
    after0, after1, after2, after3 = lk[1], lk[2], lk[3], lk[4]
    check(after0[0] > e2.started_at and after0[1] == 0, f"4: цикл с открытым окном — live_ok_at: {lk}")
    check(after1[0] == after0[0] and after1[1] == 0,
          f"4: цикл без открытого окна (N+1) live_ok_at не двигает, но успешен (К6): {lk}")
    check(after2[0] == after0[0] and after2[1] == 1 and "open" in (after2[2] or ""),
          f"4: открытое окно упало — счёт ошибок, live_ok_at стоит: {lk}")
    check(after3[1] == 2 and "RuntimeError" in (after3[2] or ""), f"4: исключение цикла — счёт +1: {lk}")

    # Настоящий _poll_cycle: первый цикл — N+1, live_ok_at = started_at (К6)
    e3 = new_engine()

    async def _no_steps(now):
        return None

    e3._refresh_sensor_steps = _no_steps
    await e3._poll_cycle()
    check(e3.prev_poll_ts is not None and not e3._open_ok and e3.live_ok_at == e3.started_at,
          "4: настоящий N+1-цикл — открытое окно не обработано, live_ok_at = старт")

    # stop() на задаче, глотающей отмену, — возврат за cancel_wait_sec
    e4 = new_engine()

    async def swallow():
        end = time.monotonic() + 0.8
        while time.monotonic() < end:
            try:
                await asyncio.sleep(max(0.0, end - time.monotonic()))
            except asyncio.CancelledError:
                continue

    e4._task = asyncio.create_task(swallow())
    await asyncio.sleep(0)
    t1 = time.monotonic()
    m0 = mark()
    await e4.stop()
    dt = time.monotonic() - t1
    check(dt < 0.6 and not e4._task.done() and logs(m0, logging.WARNING, "не завершилась"),
          f"4: stop() со сроком ({dt:.2f} с) и WARNING")
    await asyncio.wait({e4._task}, timeout=2)          # уборка: задача доживает свой срок

asyncio.run(sec4())


# ══ 5. Сторож ════════════════════════════════════════════════════════════════

S.watchdog_tick_sec = 0.05
S.watchdog_stall_sec = 0.5
S.watchdog_cancel_wait_sec = 0.2
S.watchdog_max_restarts = 3
S.watchdog_restart_window_sec = 60
S.watchdog_cycle_warn_sec = 0.3
S.watchdog_enabled = True

# Формула порога без продвижения — на настоящих числах (до подмены)
check(mgr.OnlineManager._stall_limit(NS(poll_interval_sec=30)) == 600
      and mgr.OnlineManager._stall_limit(NS(poll_interval_sec=300)) == 960 or S.watchdog_stall_sec != 600,
      "5: порог = max(stall_sec, 3 × опрос + 60)")
_sv = S.watchdog_stall_sec
S.watchdog_stall_sec = 600
check(mgr.OnlineManager._stall_limit(NS(poll_interval_sec=30)) == 600
      and mgr.OnlineManager._stall_limit(NS(poll_interval_sec=300)) == 960,
      "5: порог = max(stall_sec, 3 × опрос + 60)")
S.watchdog_stall_sec = _sv
mgr.OnlineManager._stall_limit = staticmethod(lambda e: S.watchdog_stall_sec)
mgr._RETRY_BACKOFF_SEC = (0.1, 0.2, 0.3)


async def _middle(ev):
    await _hang_here(ev)


async def _hang_here(ev):
    await ev.wait()


KEY = "6003790403|pcc|2"
STATUS: dict[str, str] = {}
BUILT: list = []
PLAN: list[str] = []          # поведение следующих построенных движков
INIT_BLOCK: dict = {}
BUILD_FAIL: dict[str, int] = {}   # сколько раз _build_engine машины упадёт (W4)
THREAD_SEC = 2.0                  # длительность «нарезки» в потоке (W6)


def make_engine(key: str, kind: str):
    e = new_engine(key, poll=0.02)
    hang = asyncio.Event()
    e._hang_ev = hang
    e._kind = kind

    async def cycle():
        if kind == "ok":
            e._open_ok = True
        elif kind == "idle":
            pass                        # цикл без открытого окна (N+1)
        elif kind == "hang":
            await _middle(hang)
        elif kind == "zombie":
            end = time.monotonic() + 1.0
            while time.monotonic() < end:
                try:
                    await asyncio.sleep(max(0.0, end - time.monotonic()))
                except asyncio.CancelledError:
                    continue
            e._running = False
        elif kind == "pulse":
            while True:                 # пульс есть, курсор стоит
                e._touch_beat()
                await asyncio.sleep(0.03)
        elif kind == "slowremote":
            while True:                 # курсор стоит, а source отвечает (медленно) — W3
                async with remote.connection(FakePool(), label="source-БД"):
                    await asyncio.sleep(0.1)
        elif kind == "thread":
            # Долгая нарезка в потоке: пульс только от потока — W6
            await remote.to_thread_beating(lambda: time.sleep(THREAD_SEC), interval=0.03)
            e._open_ok = True

    async def initialize(start_date, allow_gap_fill=False):
        ev = INIT_BLOCK.get(key)
        if ev is not None:
            await ev.wait()
        if INIT_BLOCK.get(key + ":fail"):
            INIT_BLOCK[key + ":fail"] -= 1
            raise RuntimeError("локальная БД не готова")

    e._poll_cycle = cycle
    e.initialize = initialize
    return e


async def fake_build(obs):
    key = f"{obs['router_sn']}|{obs['equip_type']}|{obs['panel_id']}"
    if BUILD_FAIL.get(key):
        BUILD_FAIL[key] -= 1
        raise asyncpg.ConnectionDoesNotExistError("кластер PostgreSQL перезапускается")
    kind = PLAN.pop(0) if PLAN else "ok"
    e = make_engine(key, kind)
    BUILT.append(e)
    return e


async def fake_get_obs(r, e, p):
    key = f"{r}|{e}|{p}"
    st = STATUS.get(key)
    if st is None:
        return None
    return {"router_sn": r, "equip_type": e, "panel_id": p, "status": st,
            "start_date": datetime(2026, 10, 1, tzinfo=timezone.utc), "poll_interval_sec": 30}


async def _noop(*a, **k):
    return None

odb.get_observation = fake_get_obs
odb.close_open_as_operator_stop = _noop
odb.set_observation_status = _noop
odb.upsert_observation = _noop
odb.get_last_closed_segment = _noop


def new_manager() -> mgr.OnlineManager:
    m = mgr.OnlineManager()
    m._build_engine = fake_build
    return m


def launch(m, key, kind):
    e = make_engine(key, kind)
    m._wanted.add(key)
    m._launch(e)
    return e


async def stop_m(m):
    await m.stop_all()


async def sec5a() -> None:
    """Зависание → стек, перезапуск только этой машины, перенос live_ok_at."""
    STATUS.clear(); BUILT.clear(); PLAN.clear()
    m = new_manager()
    STATUS[KEY] = "running"
    STATUS["OTHER|pcc|1"] = "running"
    PLAN.append("idle")                     # новый движок ещё не обновил живой анализ
    old = launch(m, KEY, "hang")
    other = launch(m, "OTHER|pcc|1", "ok")
    old.live_ok_at = datetime(2026, 10, 17, 10, 39, tzinfo=timezone.utc)
    m0 = mark()
    m._start_watchdog()
    ok = await until(lambda: m._engines.get(KEY) not in (None, old), 5)
    err = [r for r in recs(m0, logging.ERROR) if "Сторож[" + KEY in r.getMessage()]
    txt = err[0].getMessage() if err else ""
    check(ok and err, f"5а: зависший движок перезапущен с ERROR: {logs(m0, logging.ERROR)}")
    check("_hang_here" in txt and "_middle" in txt and "run" in txt and "нет продвижения" in txt,
          f"5а: в ERROR — цепочка await до _hang_here (К2), а не один кадр:\n{txt}")
    check(old._task.done() and old._task.cancelled(), "5а: старая задача отменена")
    new = m._engines.get(KEY)
    check(new is not None and new.live_ok_at == old.live_ok_at,
          f"5а: live_ok_at перенесён: {getattr(new, 'live_ok_at', None)}")
    check(m._engines.get("OTHER|pcc|1") is other and not other._task.done(),
          "5а: соседняя машина не тронута")
    h = m.engine_health(KEY)
    check(h["restarts"] == 1 and h["last_restart"]["reason"].startswith("нет продвижения")
          and h["state"] == "running", f"5а: engine_restarts = 1: {h}")
    check(logs(m0, logging.WARNING, "движок перезапущен"), "5а: WARNING «движок перезапущен, №1»")
    check(m.is_running(*KEY.rsplit("|", 2)[:2], 2), "5а: is_running")
    await stop_m(m)


async def sec5b() -> None:
    """Лимит: 4-е зависание за окно — ERROR, отмена, нового движка нет; после окна — подъём."""
    STATUS.clear(); BUILT.clear(); PLAN.clear()
    S.watchdog_restart_window_sec = 60
    m = new_manager()
    STATUS[KEY] = "running"
    PLAN.extend(["hang", "hang", "hang", "ok"])
    first = launch(m, KEY, "hang")
    m0 = mark()
    m._start_watchdog()
    ok = await until(lambda: m._down.get(KEY, ("",))[0] == "limit", 12)
    check(ok, f"5б: лимит исчерпан: {m._down} {m._restart_info}")
    check(m._restart_info[KEY]["count"] == 3 and KEY not in m._engines and len(BUILT) == 3,
          f"5б: три перезапуска, четвёртого движка нет: {m._restart_info} built={len(BUILT)}")
    lim = logs(m0, logging.ERROR, "больше не перезапускаю")
    check(len(lim) == 1 and "_hang_here" in lim[0], f"5б: ERROR о лимите со стеком — один: {lim}")
    check(all(b._task.done() for b in BUILT) and first._task.done(), "5б: зависшие задачи отменены")
    h = m.engine_health(KEY)
    check(h["state"] == "restart_limit", f"5б: состояние restart_limit: {h['state']}")
    await asyncio.sleep(0.3)
    check(KEY not in m._engines, "5б: пока лимит — не поднимаем")
    # Окно освободилось — тик поднимает машину
    S.watchdog_restart_window_sec = 0.2
    ok = await until(lambda: KEY in m._engines, 3)
    check(ok and m._engines[KEY]._kind == "ok" and m._restart_info[KEY]["count"] == 4,
          f"5б: после окна — подъём: {m._restart_info}")
    S.watchdog_restart_window_sec = 60
    await stop_m(m)


async def sec5c() -> None:
    """Зомби: новый движок не стартует, пока старая задача жива; ПУСК тоже (К5)."""
    STATUS.clear(); BUILT.clear(); PLAN.clear()
    m = new_manager()
    STATUS[KEY] = "running"
    z = launch(m, KEY, "zombie")
    z.beat_mono -= 100                      # давно без пульса
    m0 = mark()
    m._start_watchdog()
    ok = await until(lambda: KEY in m._zombies, 3)
    check(ok and KEY not in m._engines and logs(m0, logging.ERROR, "не завершилась"),
          f"5в: зомби зарегистрирован, нового движка нет: {logs(m0, logging.ERROR)}")
    check(m.engine_health(KEY)["state"] == "zombie", "5в: состояние zombie")
    try:
        await m.start_machine(*KEY.rsplit("|", 2)[:2], 2, datetime(2026, 10, 1, tzinfo=timezone.utc))
        refused = None
    except RuntimeError as e:
        refused = e
    check(refused is not None and "не завершился" in str(refused) and KEY not in m._engines,
          f"5в: ПУСК при живом зомби — ошибка оператору: {refused!r}")
    ok = await until(lambda: KEY in m._engines, 4)
    check(ok and z._task.done() and m._engines[KEY] is not z and KEY not in m._zombies,
          "5в: после конца старой задачи — новый движок")
    await stop_m(m)


async def sec5d() -> None:
    """Колбэк старой задачи не снимает новый движок; неожиданная отмена и ошибка."""
    STATUS.clear(); BUILT.clear(); PLAN.clear()
    m = new_manager()
    STATUS[KEY] = "running"
    a = launch(m, KEY, "ok")
    b0 = make_engine(KEY, "ok")
    check(m._launch(b0) is False and m._engines.get(KEY) is a,
          "5г: второй движок на машину при живой задаче не запускается")
    a._running = False
    old_task = a._task
    old_task.cancel()
    await asyncio.wait({old_task})
    b = make_engine(KEY, "ok")
    check(m._launch(b) is True, "5г: после конца прежней задачи — запуск")
    m._on_task_done(KEY, old_task, a)       # запоздалый колбэк старой задачи
    await asyncio.sleep(0.05)
    check(m._engines.get(KEY) is b and m._tasks.get(KEY) is b._task,
          "5г: колбэк старой задачи новый движок не снял")
    # Неожиданная отмена → WARNING, сторож поднимает
    m0 = mark()
    m._start_watchdog()
    b._task.cancel()
    ok = await until(lambda: m._engines.get(KEY) not in (None, b), 3)
    check(logs(m0, logging.WARNING, "отменена неожиданно") and ok,
          f"5д: неожиданная отмена — WARNING и подъём: {logs(m0, logging.WARNING)}")
    # Исключение задачи → ERROR с exc_info, подъём
    c = m._engines[KEY]
    m._wanted.discard(KEY)                 # на время подмены сторож машину не трогает
    await m._stop_engine(KEY)              # снять, чтобы запустить «падающий» движок
    check(c._task.done(), "5д: прежний движок снят")

    async def boom():
        await asyncio.sleep(0.01)
        raise RuntimeError("упал run")

    d = make_engine(KEY, "ok")
    d.run = boom
    d._running = True                       # run подменён — флаг ставим сами
    m0 = mark()
    m._launch(d)
    m._wanted.add(KEY)
    ok = await until(lambda: m._engines.get(KEY) not in (None, d), 3)
    er = [r for r in recs(m0, logging.ERROR) if "завершилась с ошибкой" in r.getMessage()]
    check(er and er[0].exc_info is not None and ok,
          f"5д: ошибка задачи — ERROR со стеком и подъём: {logs(m0, logging.ERROR)}")
    check(m._restart_info[KEY]["count"] == 2 and "ошибкой" in m._restart_info[KEY]["last_reason"],
          f"5д: перезапуск после ошибки задачи учтён: {m._restart_info.get(KEY)}")
    await stop_m(m)


async def sec5e() -> None:
    """СТОП во время зависания — подъёма нет; ПУСК под замком — сторож пропускает; К3."""
    STATUS.clear(); BUILT.clear(); PLAN.clear()
    m = new_manager()
    STATUS[KEY] = "running"
    h1 = launch(m, KEY, "hang")
    m._start_watchdog()
    STATUS[KEY] = "stopped"
    await m.stop_machine(*KEY.rsplit("|", 2)[:2], 2)
    await asyncio.sleep(1.0)
    check(KEY not in m._wanted and KEY not in m._engines and h1._task.done() and not BUILT,
          f"5е: СТОП во время зависания — ключ снят, подъёма нет: built={len(BUILT)}")
    check(not m.is_running(*KEY.rsplit("|", 2)[:2], 2), "5е: после СТОП is_running = false")

    # Замок машины держит ПУСК — сторож ключ пропускает, пока замок взят
    STATUS[KEY] = "running"
    h2 = launch(m, KEY, "hang")
    lock = m._key_lock(KEY)
    await lock.acquire()
    m._lock_since[KEY] = (time.monotonic(), "ПУСК")
    await asyncio.sleep(0.9)
    check(m._engines.get(KEY) is h2 and not h2._task.done() and not BUILT,
          "5е: ПУСК под замком — сторож пропускает ключ")
    # К3: тик решил «завис», а ПУСК успел взять замок и поставить свежий движок
    m._spawn_restart(KEY, "stall", "нет продвижения 999 с", h2, h2._task)
    await asyncio.sleep(0.05)
    await m._stop_engine(KEY)
    fresh = make_engine(KEY, "ok")
    m._launch(fresh)
    m._lock_since.pop(KEY, None)
    lock.release()
    await until(lambda: not m._restart_tasks, 2)
    check(m._engines.get(KEY) is fresh and not fresh._task.done(),
          "5е: К3 — перезапуск под замком перепроверил причину, свежий движок не тронут")
    await stop_m(m)


async def sec5f() -> None:
    """Старт, упавший при start_all_running, поднимается сторожем (с паузой)."""
    STATUS.clear(); BUILT.clear(); PLAN.clear()
    m = new_manager()
    STATUS[KEY] = "running"

    async def lst():
        return [{"router_sn": "6003790403", "equip_type": "pcc", "panel_id": 2, "status": "running",
                 "start_date": datetime(2026, 10, 1, tzinfo=timezone.utc), "poll_interval_sec": 30}]

    class _NoSync:
        def __init__(self, interval_sec=30):
            pass

        def start(self):
            pass

        async def stop(self):
            pass

    async def _no_sched():
        await asyncio.sleep(3600)

    import online.history_sync as hs
    real_lst, real_hs = odb.list_observations, hs.HistorySyncWorker
    odb.list_observations, hs.HistorySyncWorker = lst, _NoSync
    m._run_status_scheduler = _no_sched
    INIT_BLOCK[KEY + ":fail"] = 2               # старт и первый повтор падают
    try:
        m0 = mark()
        await m.start_all_running()
        check(KEY in m._wanted and KEY not in m._engines and m._down[KEY][0] == "failed",
              f"5ж: упавший старт — ключ под надзором: {m._down}")
        check(m.engine_health(KEY)["state"] == "not_started", "5ж: состояние not_started")
        ok = await until(lambda: KEY in m._engines, 4)
        check(ok and logs(m0, logging.ERROR, "подъём движка не удался")
              and logs(m0, logging.WARNING, "после неудачного старта"),
              f"5ж: сторож поднял машину после повторов: {logs(m0, logging.WARNING)}")
        check(m._restart_info.get(KEY) is None, "5ж: повтор старта не считается перезапуском")
        check(logs(m0, logging.INFO, "Сторож движков: запущен"), "5ж: «Сторож движков: запущен»")
    finally:
        odb.list_observations, hs.HistorySyncWorker = real_lst, real_hs
        INIT_BLOCK.pop(KEY + ":fail", None)
    await stop_m(m)

    # W4 (ревью): упал сам _build_engine (локальная БД перезапускается вместе с
    # сервисом, как 22–23.10) — ключ всё равно под надзором, сторож поднимает
    STATUS.clear(); BUILT.clear(); PLAN.clear()
    m = new_manager()
    STATUS[KEY] = "running"
    odb.list_observations, hs.HistorySyncWorker = lst, _NoSync
    m._run_status_scheduler = _no_sched
    BUILD_FAIL[KEY] = 1
    try:
        m0 = mark()
        await m.start_all_running()
        check(KEY in m._wanted and KEY not in m._engines and m._down[KEY][0] == "failed"
              and "ConnectionDoesNotExistError" in m._down[KEY][1],
              f"5ж: W4 — упал _build_engine при старте — ключ под надзором: {m._down}")
        check(m.engine_health(KEY)["state"] == "not_started"
              and m.is_running(*KEY.rsplit("|", 2)[:2], 2),
              "5ж: W4 — not_started, is_running (удаление сегментов без СТОП не проскочит)")
        ok = await until(lambda: KEY in m._engines, 4)
        check(ok and logs(m0, logging.WARNING, "после неудачного старта") and len(BUILT) == 1,
              f"5ж: W4 — сторож поднял машину после упавшего _build_engine: built={len(BUILT)}")
    finally:
        odb.list_observations, hs.HistorySyncWorker = real_lst, real_hs
        BUILD_FAIL.pop(KEY, None)
    await stop_m(m)

    # Нет привязки KB при старте — машина вне надзора, «нужен ПУСК» (W9)
    STATUS.clear(); BUILT.clear(); PLAN.clear()
    m = new_manager()

    async def _no_binding(obs):
        return None

    m._build_engine = _no_binding
    odb.list_observations, hs.HistorySyncWorker = lst, _NoSync
    m._run_status_scheduler = _no_sched
    try:
        await m.start_all_running()
        f = mgr.health_fields(m, KEY, "running", datetime.now(timezone.utc), 90)
        check(KEY not in m._wanted and f["engine_state"] == "needs_start" and f["engine_stalled"]
              and f["analytics_stale"] and f["engine_auto_restart"] is False
              and "нужен ПУСК" in (f["engine_down_reason"] or ""),
              f"5ж: W9 — нет привязки KB: needs_start, сторож не обещан: {f}")
    finally:
        odb.list_observations, hs.HistorySyncWorker = real_lst, real_hs
    await stop_m(m)


async def sec5g() -> None:
    """enabled=false — одно ERROR со стеком, без отмены."""
    STATUS.clear(); BUILT.clear(); PLAN.clear()
    S.watchdog_enabled = False
    try:
        m = new_manager()
        STATUS[KEY] = "running"
        h = launch(m, KEY, "hang")
        m0 = mark()
        m._start_watchdog()
        await asyncio.sleep(1.2)
        errs = [x for x in logs(m0, logging.ERROR) if "сторож выключен" in x]
        check(len(errs) == 1 and "_hang_here" in errs[0] and not h._task.done()
              and m._engines.get(KEY) is h and not BUILT,
              f"5з: enabled=false — одно ERROR со стеком, движок не тронут: {len(errs)}")
        check(m.engine_health(KEY)["state"] == "stalled", "5з: состояние stalled")
        check(logs(m0, logging.INFO, "только запись в журнал"), "5з: старт сторожа — «выключен»")
    finally:
        S.watchdog_enabled = True
    await stop_m(m)


async def sec5h() -> None:
    """Эпоха гейта: перезапуск сторожем — итог засчитан; СТОП/ПУСК — нет (Р11)."""
    STATUS.clear(); BUILT.clear(); PLAN.clear()
    m = new_manager()
    STATUS[KEY] = "running"
    e1 = launch(m, KEY, "hang")
    seen: dict = {}
    gate_ev = asyncio.Event()

    async def fake_gate(sn, et, pid, struct, fault_hash, **k):
        seen["engine"] = k.get("engine")
        await gate_ev.wait()
        return mgr.GATE_OK

    async def open_seg(sn, et, pid):
        return {"id": 77}

    real_gate, real_open = mgr._analyze_warning_claude, odb.get_open_segment
    mgr._analyze_warning_claude, odb.get_open_segment = fake_gate, open_seg
    try:
        ikey = (KEY, "предупреждение")
        t = asyncio.create_task(m._run_gate(KEY, ikey, e1, {}, "H1"))
        m._gate_inflight[ikey] = ("H1", datetime.now(timezone.utc), t)
        await asyncio.sleep(0.02)
        prov = seen.get("engine")
        check(callable(prov) and prov() is e1, "5и: поставщик движка — текущий движок")
        m._start_watchdog()
        ok = await until(lambda: m._engines.get(KEY) not in (None, e1), 4)
        check(ok and m._gate_inflight.get(ikey, (None, None, None))[2] is t,
              "5и: запись «разбор в работе» переживает перезапуск сторожем — второй не уйдёт")
        e2 = m._engines.get(KEY)
        check(prov() is e2, "5и: после перезапуска поставщик отдаёт новый движок (Р11)")
        gate_ev.set()
        await asyncio.wait_for(t, 2)
        check(m._gate_done.get(KEY, {}).get("H1") == 77,
              f"5и: разбор после перезапуска сторожем засчитан: {m._gate_done}")
        # СТОП/ПУСК — эпоха меняется, итог не пишется
        gate_ev.clear()
        t2 = asyncio.create_task(m._run_gate(KEY, ikey, e2, {}, "H2"))
        m._gate_inflight[ikey] = ("H2", datetime.now(timezone.utc), t2)
        await asyncio.sleep(0.02)
        prov2 = seen["engine"]
        await m.start_machine(*KEY.rsplit("|", 2)[:2], 2, datetime(2026, 10, 1, tzinfo=timezone.utc))
        check("H1" not in m._gate_done.get(KEY, {}) and not m._gate_inflight,
              "5и: ПУСК сбросил память гейта и «в работе»")
        check(prov2() is None, "5и: после ПУСК поставщик старого разбора — None")
        gate_ev.set()
        await asyncio.wait_for(t2, 2)
        check("H2" not in m._gate_done.get(KEY, {}), "5и: разбор, вернувшийся после ПУСК, итог не пишет")
    finally:
        mgr._analyze_warning_claude, odb.get_open_segment = real_gate, real_open
    await stop_m(m)


async def sec5i() -> None:
    """Сброс памяти при ПУСК без движка (К5); is_running в окне перезапуска; К4."""
    STATUS.clear(); BUILT.clear(); PLAN.clear()
    m = new_manager()
    STATUS[KEY] = "running"
    m._gate_done[KEY] = {"X": 1}
    m._wanted.add(KEY)
    ep0 = m._mem_epoch.get(KEY, 0)
    check(m.is_running(*KEY.rsplit("|", 2)[:2], 2), "5к: is_running в окне перезапуска (движка нет)")
    await m.start_machine(*KEY.rsplit("|", 2)[:2], 2, datetime(2026, 10, 1, tzinfo=timezone.utc))
    check(m._mem_epoch.get(KEY, 0) == ep0 + 1 and KEY not in m._gate_done and KEY in m._engines
          and KEY in m._wanted, "5к: ПУСК без движка — эпоха +1, память гейта сброшена, движок есть")
    # Р8: ПУСК, упавший посередине, — ключ не под надзором, ошибка оператору
    INIT_BLOCK[KEY + ":fail"] = 1
    try:
        await m.start_machine(*KEY.rsplit("|", 2)[:2], 2, datetime(2026, 10, 1, tzinfo=timezone.utc))
        perr = None
    except RuntimeError as e:
        perr = e
    check(perr is not None and KEY not in m._wanted and KEY not in m._engines,
          f"5к: ПУСК упал — ошибка оператору, сторож молча не поднимает (Р8): {perr!r}")
    # W9: API не обещает «сторож перезапускает» — машина вне надзора, нужен ПУСК
    f = mgr.health_fields(m, KEY, "running", datetime.now(timezone.utc), 90)
    check(f["engine_state"] == "needs_start" and f["engine_stalled"] and f["analytics_stale"]
          and f["engine_auto_restart"] is False
          and (f["engine_down_reason"] or "").startswith("ПУСК не выполнен: RuntimeError"),
          f"5к: W9 — ПУСК упал: needs_start с причиной, без обещания сторожа: {f}")
    INIT_BLOCK.pop(KEY + ":fail", None)

    # К4: участок под замком со сроком
    S.watchdog_stall_sec = 0.3
    m0 = mark()
    try:
        async with m._key_section(KEY, "ПУСК"):
            await asyncio.sleep(5)
        k4 = None
    except RuntimeError as e:
        k4 = e
    check(k4 is not None and "не уложился" in str(k4) and not m._key_lock(KEY).locked()
          and logs(m0, logging.ERROR, "не уложился"),
          f"5к: К4 — зависание под замком прерывается по сроку, замок свободен: {k4!r}")
    # Тик: замок держится дольше stall_sec — ERROR
    m._wanted.add(KEY)
    lock = m._key_lock(KEY)
    await lock.acquire()
    m._lock_since[KEY] = (time.monotonic() - 10, "СТОП")
    m0 = mark()
    m._watch_tick()
    check(logs(m0, logging.ERROR, "замок машины держит «СТОП»"), "5к: тик — ERROR о долгом замке")
    lock.release()
    m._lock_since.pop(KEY, None)
    S.watchdog_stall_sec = 0.5
    await stop_m(m)


async def sec5j() -> None:
    """К5: остановка посреди подъёма; _launch при остановке; stop_all — сторож первым,
    движки параллельно."""
    STATUS.clear(); BUILT.clear(); PLAN.clear()
    m = new_manager()
    STATUS[KEY] = "running"
    launch(m, KEY, "hang")
    INIT_BLOCK[KEY] = asyncio.Event()           # подъём застрянет в initialize
    m._start_watchdog()
    ok = await until(lambda: KEY not in m._engines and BUILT, 4)
    check(ok and m._restart_tasks, "5л: перезапуск идёт (ждёт initialize)")
    await m.stop_all()
    INIT_BLOCK[KEY].set()
    await asyncio.sleep(0.1)
    check(KEY not in m._engines and not m._restart_tasks and m._watchdog_task is None,
          "5л: остановка посреди подъёма — движок не запущен, перезапуск снят (К5)")
    INIT_BLOCK.pop(KEY, None)
    check(m._launch(make_engine(KEY, "ok")) is False and KEY not in m._engines,
          "5л: _launch при остановке отказывает")

    # stop_all: три движка, глотающие отмену, — параллельно
    m2 = new_manager()
    for i in range(3):
        launch(m2, f"Z{i}|pcc|1", "zombie")
    await asyncio.sleep(0.05)
    t0 = time.monotonic()
    m0 = mark()
    await m2.stop_all()
    dt = time.monotonic() - t0
    check(dt < 2 * S.watchdog_cancel_wait_sec + 0.4,
          f"5л: stop_all — движки параллельно, срок ≤ 2 × cancel_wait + ε: {dt:.2f} с")
    check(logs(m0, logging.INFO, "Онлайн-движки остановлены за"), "5л: INFO «Онлайн-движки остановлены за N с»")
    await asyncio.sleep(1.1)                    # зомби дожили свой срок


async def sec5k() -> None:
    """К8: долгий идущий цикл — WARNING раз на цикл; Р6: пульс есть, курсор стоит — перезапуск."""
    STATUS.clear(); BUILT.clear(); PLAN.clear()
    m = new_manager()
    STATUS[KEY] = "running"
    p = launch(m, KEY, "pulse")
    m0 = mark()
    m._start_watchdog()
    ok = await until(lambda: m._engines.get(KEY) not in (None, p), 5)
    w = logs(m0, logging.WARNING, "]: цикл идёт")
    check(len(w) == 1, f"5м: долгий цикл, пока идёт, — одно WARNING (К8): {w}")
    er = logs(m0, logging.ERROR, "ни курсора, ни ответов source")
    check(ok and er and p._task.done(),
          f"5м: пульс есть, а ни курсора, ни ответов source — перезапуск (Р6): {er}")
    await stop_m(m)


async def sec5_review_w3() -> None:
    """W3: медленная, но живая работа (source отвечает, курсор стоит) потолком не
    снимается; при исчерпанном лимите живой движок не снимается — одно ERROR."""
    STATUS.clear(); BUILT.clear(); PLAN.clear()
    m = new_manager()
    STATUS[KEY] = "running"
    s = launch(m, KEY, "slowremote")
    m0 = mark()
    m._start_watchdog()
    await asyncio.sleep(1.6)                    # > 3 × stall_sec, курсор не двигался
    check(m._engines.get(KEY) is s and not s._task.done() and not BUILT
          and not logs(m0, logging.ERROR) and s._tag.calls >= 8,
          f"W3: source отвечает (вызовов {s._tag.calls}) — потолок не снимает живой цикл: "
          f"{logs(m0, logging.ERROR)}")
    await stop_m(m)

    STATUS.clear(); BUILT.clear(); PLAN.clear()
    m = new_manager()
    STATUS[KEY] = "running"
    p = launch(m, KEY, "pulse")
    from collections import deque
    m._restart_times[KEY] = deque([time.monotonic()] * S.watchdog_max_restarts)
    m0 = mark()
    m._start_watchdog()
    await asyncio.sleep(1.5)
    er = logs(m0, logging.ERROR, "живой движок (пульс есть) не снимаю")
    h = m.engine_health(KEY)
    check(len(er) == 1 and m._engines.get(KEY) is p and not p._task.done() and not BUILT
          and h["state"] == "stalled" and h["auto_restart"] is False,
          f"W3: потолок при исчерпанном лимите — одно ERROR, живой движок не снят: "
          f"{len(er)} {h['state']} built={len(BUILT)}")
    check("ни курсора, ни ответов source" in (er[0] if er else "") and "Задачи движка:" in (er[0] if er else ""),
          "W3: в ERROR — причина и стеки задач движка")
    await stop_m(m)


async def sec5_review_w5() -> None:
    """W5: сторож выключен, потолок (пульс свежий) — ERROR со стеками один раз на
    эпизод, а не на каждом тике."""
    STATUS.clear(); BUILT.clear(); PLAN.clear()
    S.watchdog_enabled = False
    try:
        m = new_manager()
        STATUS[KEY] = "running"
        p = launch(m, KEY, "pulse")
        m0 = mark()
        m._start_watchdog()
        await asyncio.sleep(1.6)                # ~20 тиков после порога
        errs = [x for x in logs(m0, logging.ERROR) if "сторож выключен" in x]
        check(len(errs) == 1 and "ни курсора" in errs[0] and m._engines.get(KEY) is p
              and not p._task.done() and not BUILT,
              f"W5: сторож выключен, потолок — одно ERROR на эпизод: {len(errs)}")
        h = m.engine_health(KEY)
        check(h["state"] == "stalled" and h["auto_restart"] is False,
              f"W5/W9: состояние stalled, сторож не перезапустит: {h['state']} {h['auto_restart']}")
    finally:
        S.watchdog_enabled = True
    await stop_m(m)


async def sec5_review_w6() -> None:
    """W6: долгая нарезка в потоке (пульс только от потока) — ERROR один раз, без
    перезапуска; снятие движка ждёт конец потока (зомби), второго потока нет."""
    STATUS.clear(); BUILT.clear(); PLAN.clear()
    m = new_manager()
    STATUS[KEY] = "running"
    t0 = time.monotonic()
    e = launch(m, KEY, "thread")
    m0 = mark()
    m._start_watchdog()
    ok = await until(lambda: logs(m0, logging.ERROR, "нарезка в потоке"), 1.5)
    await asyncio.sleep(0.3)
    er = logs(m0, logging.ERROR, "нарезка в потоке")
    check(ok and len(er) == 1 and "поток не прервать" in er[0] and "поток нарезки считает" in er[0]
          and m._engines.get(KEY) is e and not BUILT and len(remote.threads(KEY)) == 1,
          f"W6: пульс только от потока — одно ERROR, перезапуска нет, поток один: {er}")
    h = m.engine_health(KEY)
    check(h["state"] == "stalled" and h["auto_restart"] is False,
          f"W6: состояние stalled, сторож сознательно не трогает: {h}")
    # СТОП посреди нарезки: задача ждёт поток — зомби, новый движок только после него
    m._wanted.discard(KEY)
    gone = await m._stop_engine(KEY)
    check(gone is False and KEY in m._zombies and not e._task.done() and len(remote.threads(KEY)) == 1,
          f"W6: снятие посреди нарезки — зомби до конца потока: {gone}")
    m._wanted.add(KEY)
    m0 = mark()
    await asyncio.sleep(0.15)
    m._logged.pop((KEY, "zombie"), None)
    m._watch_tick()
    check(logs(m0, logging.ERROR, "ждёт поток нарезки"), f"W6: тик — зомби ждёт поток: {logs(m0, logging.ERROR)}")
    ok = await until(lambda: e._task.done(), THREAD_SEC + 1)
    check(ok and e._task.cancelled() and remote.threads(KEY) == []
          and time.monotonic() - t0 >= THREAD_SEC - 0.1,
          "W6: задача завершилась отменой только после конца потока")
    ok = await until(lambda: KEY in m._engines and m._engines[KEY] is not e, 3)
    check(ok and len(BUILT) == 1, f"W6: после конца потока — новый движок (один): built={len(BUILT)}")
    await stop_m(m)
    await asyncio.sleep(0.05)


async def sec5_long_catchup() -> None:
    """Нормальная долгая догонка (пульс и курсор двигаются) — не трогается."""
    STATUS.clear(); BUILT.clear(); PLAN.clear()
    m = new_manager()
    STATUS[KEY] = "running"
    e = make_engine(KEY, "ok")

    async def catchup():
        base = datetime(2026, 9, 1, tzinfo=timezone.utc)
        for i in range(60):                    # «закрытие окон»: пульс и курсор
            await asyncio.sleep(0.03)
            e.cursor_ts = base + timedelta(days=i)
            e.last_processed_to = e.cursor_ts
            e._touch_beat()
        e._open_ok = True

    e._poll_cycle = catchup
    m._wanted.add(KEY)
    m._launch(e)
    m0 = mark()
    m._start_watchdog()
    await asyncio.sleep(1.6)
    check(m._engines.get(KEY) is e and not BUILT and not logs(m0, logging.ERROR),
          f"5н: законная догонка дольше stall_sec с пульсом — не трогается: {logs(m0, logging.ERROR)}")
    await stop_m(m)


for _fn in (sec5a, sec5b, sec5c, sec5d, sec5e, sec5f, sec5g, sec5h, sec5i, sec5j, sec5k,
            sec5_long_catchup, sec5_review_w3, sec5_review_w5, sec5_review_w6):
    try:
        asyncio.run(asyncio.wait_for(_fn(), 30))
    except Exception as _e:      # noqa: BLE001
        import traceback
        check(False, f"{_fn.__name__}: исключение {_e!r}\n{traceback.format_exc()}")


# К2: стек задачи — вся цепочка, звено без кадра — по имени
async def sec_stack() -> None:
    ev = asyncio.Event()

    class NoFrame:                               # как Cython-корутина asyncpg
        __qualname__ = "Protocol._wait_for_cancellation"

        def __await__(self):
            return ev.wait().__await__()

    async def run_():
        await _middle(ev)

    t = asyncio.create_task(run_())
    await asyncio.sleep(0.01)
    st = remote.task_stack(t)
    check(len(t.get_stack()) == 1 and any("_hang_here" in x for x in st)
          and any("_middle" in x for x in st) and any("ждёт:" in x for x in st),
          f"К2: get_stack — один кадр, task_stack — вся цепочка: {st}")
    chain = remote.coro_chain(NS(cr_frame=None, cr_await=None, __qualname__="X"))
    check(chain and chain[0].startswith("X ["), f"К2: звено без кадра — по имени: {chain}")
    ev.set()
    await t

asyncio.run(sec_stack())


# ══ 6. API ═══════════════════════════════════════════════════════════════════

S.watchdog_cycle_warn_sec = 120
S.watchdog_stall_sec = 600
mgr.OnlineManager._stall_limit = staticmethod(lambda e: max(S.watchdog_stall_sec,
                                                            3 * float(e.poll_interval_sec) + 60))
now = datetime.now(timezone.utc)
check(mgr.analytics_stale_threshold(90, 30) == 180 and mgr.analytics_stale_threshold(300, 30) == 300,
      "6: порог «не обновляется» = max(data_stale, опрос + cycle_warn + 30) = 180 с (К7)")


def _api_engine(lag_sec):
    e = eng.OnlinePollEngine("SN", "pcc", 1, "E", cfg=None, poll_interval_sec=30)
    e.live_ok_at = now - timedelta(seconds=lag_sec)
    return e


m = mgr.OnlineManager()
m._engines["SN|pcc|1"] = _api_engine(31)
f = mgr.health_fields(m, "SN|pcc|1", "running", now, 90)
check(f["analytics_stale"] is False and f["analytics_lag_sec"] < 90 and f["engine_stalled"] is False
      and f["engine_state"] == "running" and f["engine_restarts"] == 0 and f["last_engine_restart"] is None
      and f["analytics_updated_at"] == m._engines["SN|pcc|1"].live_ok_at.isoformat(),
      f"6: свежий движок: {f}")
m._engines["SN|pcc|1"] = _api_engine(150)
f = mgr.health_fields(m, "SN|pcc|1", "running", now, 90)
check(f["analytics_stale"] is False, f"6: рез 09:00 — лаг 150 с ещё не «не обновляется» (К7): {f}")
m._engines["SN|pcc|1"] = _api_engine(200)
f = mgr.health_fields(m, "SN|pcc|1", "running", now, 90)
check(f["analytics_stale"] is True and f["analytics_lag_sec"] == 200, f"6: лаг > порога: {f}")
f = mgr.health_fields(m, "SN|pcc|1", "stopped", now, 90)
check(f["analytics_stale"] is False and f["analytics_lag_sec"] is None and f["analytics_updated_at"] is None
      and f["engine_state"] == "stopped" and f["engine_stalled"] is False, f"6: stopped: {f}")
f = mgr.health_fields(m, "NONE|pcc|1", "running", now, 90)
check(f["analytics_stale"] is True and f["engine_stalled"] is True and f["engine_state"] == "needs_start"
      and f["analytics_lag_sec"] is None and f["engine_auto_restart"] is False
      and "нужен ПУСК" in (f["engine_down_reason"] or ""),
      f"6: running без движка и вне надзора — needs_start (W9): {f}")
f = mgr.health_fields(m, "SN|pcc|1", "running", now, 90)
check(f["engine_auto_restart"] is False, "6: движок вне _wanted — сторож его не поднимет")
m._wanted.add("SN|pcc|1")
check(mgr.health_fields(m, "SN|pcc|1", "running", now, 90)["engine_auto_restart"] is True,
      "6: под надзором и сторож включён — engine_auto_restart")
S.watchdog_enabled = False
check(mgr.health_fields(m, "SN|pcc|1", "running", now, 90)["engine_auto_restart"] is False,
      "6: сторож выключен — engine_auto_restart = false (W9)")
S.watchdog_enabled = True
m._wanted.discard("SN|pcc|1")

# W7: data_stale недостоверен — analytics_stale раньше порога 180 с
m._engines["SN|pcc|1"] = _api_engine(120)
m._engines["SN|pcc|1"].live_ok_real = True
f = mgr.health_fields(m, "SN|pcc|1", "running", now, 90, data_stale=True)
check(f["analytics_stale"] is True,
      f"6: W7 — начало простоя: лаг 120 с > порога телеметрии 90 с при data_stale — «не обновляется»: {f}")
check(mgr.health_fields(m, "SN|pcc|1", "running", now, 90)["analytics_stale"] is False,
      "6: W7 — тот же лаг при свежих данных — ещё не «не обновляется» (порог 180 с, К7)")
m._engines["SN|pcc|1"] = _api_engine(40)
m._engines["SN|pcc|1"].live_ok_real = True
f = mgr.health_fields(m, "SN|pcc|1", "running", now, 90, data_stale=True)
check(f["analytics_stale"] is False,
      f"6: W7 — аналитика свежая, отстала сама телеметрия (Р19) — «Нет связи», как раньше: {f}")
m._engines["SN|pcc|1"] = _api_engine(20)              # live_ok_at = старт / ПУСК
f = mgr.health_fields(m, "SN|pcc|1", "running", now, 90, data_stale=True)
check(f["analytics_stale"] is True,
      f"6: W7 — после рестарта / ПУСК живой анализ ещё не обновлён, data_stale — «не обновляется»: {f}")
m._engines["SN|pcc|1"] = _api_engine(31)
m._last_live_ok.pop("P|pcc|1", None)
m._wanted.add("P|pcc|1")
m._last_live_ok["P|pcc|1"] = now - timedelta(seconds=5)
m._live_real["P|pcc|1"] = False                     # окно ПУСК: метка — момент ПУСК
check(mgr.health_fields(m, "P|pcc|1", "running", now, 90, data_stale=True)["analytics_stale"] is True
      and mgr.health_fields(m, "P|pcc|1", "running", now, 90)["analytics_stale"] is False,
      "6: W7 — окно ПУСК при data_stale — «не обновляется», при свежих данных — не мигает")
m._wanted.discard("P|pcc|1")
# Перенос признака «настоящее обновление» через _remember_live_ok (перезапуск сторожем)
_old = _api_engine(50)
_old.live_ok_real = True
m._remember_live_ok("R|pcc|1", _old)
check(m._live_real.get("R|pcc|1") is True and m._last_live_ok["R|pcc|1"] == _old.live_ok_at,
      "6: W7 — признак настоящего обновления переносится вместе с live_ok_at")

# W9: задача умерла / зомби завершился при исчерпанном лимите — restart_limit, не restarting
from collections import deque as _dq
m._wanted.add("D|pcc|1")
m._down["D|pcc|1"] = ("dead", "задача движка отменена неожиданно")
m._restart_times["D|pcc|1"] = _dq([time.monotonic()] * S.watchdog_max_restarts)
check(m.engine_health("D|pcc|1")["state"] == "restart_limit",
      f"6: W9 — dead при исчерпанном лимите: {m.engine_health('D|pcc|1')['state']}")
m._restart_times["D|pcc|1"].clear()
check(m.engine_health("D|pcc|1")["state"] == "restarting", "6: W9 — dead при свободном лимите — restarting")
m._wanted.discard("D|pcc|1")
# Окно перезапуска / ПУСК: движка нет, лаг — от прежнего движка (К7, правка 2)
m._wanted.add("W|pcc|1")
m._last_live_ok["W|pcc|1"] = now - timedelta(seconds=40)
f = mgr.health_fields(m, "W|pcc|1", "running", now, 90)
check(f["analytics_stale"] is False and f["engine_state"] == "restarting" and f["engine_stalled"] is False
      and f["analytics_lag_sec"] == 40, f"6: окно перезапуска — не мигает: {f}")
m._restart_info["SN|pcc|1"] = {"count": 2, "last_ts": now, "last_reason": "нет продвижения 612 с"}
f = mgr.health_fields(m, "SN|pcc|1", "running", now, 90, diagnostics=True)
check(f["engine_restarts"] == 2 and f["last_engine_restart"] == {"ts": now.isoformat(),
                                                                  "reason": "нет продвижения 612 с"},
      f"6: перезапуски из памяти: {f}")
check(all(k in f for k in ("engine_beat_age_sec", "cycle_running_sec", "last_cycle_sec", "max_cycle_sec",
                           "cycles_failed_streak", "last_cycle_error", "analytics_stale_threshold_sec")),
      f"6: диагностика /api/online/status: {sorted(f)}")
f = mgr.health_fields(None, "SN|pcc|1", "running", now, 90)
check(f["analytics_stale"] is True and f["engine_state"] == "not_started", "6: менеджера нет — stale")
m._down["L|pcc|1"] = ("limit", "лимит")
m._wanted.add("L|pcc|1")
m._last_live_ok["L|pcc|1"] = now
f = mgr.health_fields(m, "L|pcc|1", "running", now, 90)
check(f["engine_state"] == "restart_limit" and f["engine_stalled"] and f["analytics_stale"],
      f"6: лимит перезапусков: {f}")

# Маршруты (fastapi локально нет — разбор исходника, как test_api_v5119)
_rt = (ROOT / "web" / "routes.py").read_text(encoding="utf-8")
_tree = ast.parse(_rt)
_fn = next(n for n in _tree.body if isinstance(n, ast.FunctionDef) and n.name == "_engine_health_fields")
_ns: dict = {"logger": logging.getLogger("routes")}
exec(compile(ast.Module(body=[_fn], type_ignores=[]), "routes._engine_health_fields", "exec"), _ns)
_ehf = _ns["_engine_health_fields"]
r1 = _ehf(m, "SN|pcc|1", {"status": "running", "poll_interval_sec": 30}, now, 90)
r2 = _ehf(m, "SN|pcc|1", {"status": "running"}, now, 90, diagnostics=True)
_api = {"analytics_updated_at", "analytics_lag_sec", "analytics_stale", "engine_stalled",
        "engine_restarts", "last_engine_restart", "engine_state", "engine_auto_restart",
        "engine_down_reason"}
check(set(r1) == _api and _api < set(r2) and "last_cycle_error" in r2,
      f"6: поля /api/machines и /api/online/status: {sorted(r1)} / {sorted(r2)}")
check(_ehf(None, "x", {}, "не дата", 90) is not None, "6: сбой расчёта не роняет ответ")
m._engines["SN|pcc|1"] = _api_engine(120)
m._engines["SN|pcc|1"].live_ok_real = True
check(_ehf(m, "SN|pcc|1", {"status": "running"}, now, 90, data_stale=True)["analytics_stale"] is True
      and _ehf(m, "SN|pcc|1", {"status": "running"}, now, 90)["analytics_stale"] is False,
      "6: W7 — маршрут передаёт data_stale в health_fields")
_m_src = _rt.split("async def api_machines", 1)[1].split("@router", 1)[0]
_s_src = _rt.split("async def api_online_status", 1)[1].split("@router", 1)[0]
check("**_engine_health_fields(_mgr, key, obs, _now, _stale_sec, data_stale=data_stale)" in _m_src
      and "_engine_health_fields(mgr, key, obs, now_utc, _stale_sec, diagnostics=True,\n"
          "                                        data_stale=data_stale)" in _s_src
      and "**_health" in _s_src, "6: оба маршрута отдают поля надзора (и data_stale в расчёт — W7)")
check(_m_src.count("(_now - _last_data).total_seconds() > _stale_sec") == 1
      and _s_src.count("(now_utc - _last_data).total_seconds() > _stale_sec") == 1,
      "6: data_stale считается как раньше")
_html = (ROOT / "web" / "templates" / "online_monitor.html").read_text(encoding="utf-8")
check("Аналитика не обновляется" in _html and "Движок завис — перезапуск сторожем" in _html,
      "6: страница /online — «Аналитика не обновляется» и «Движок завис»")
check("item.engine_auto_restart ? '⚠ Движок завис — перезапуск сторожем'" in _html
      and "нужен ПУСК" in _html,
      "6: W9 — /online обещает перезапуск только при engine_auto_restart, needs_start — «нужен ПУСК»")
_cu = _html.split("const catchingUp", 1)[1].split(";", 1)[0]
check("beatFresh" in _cu and "item.engine_beat_age_sec < 120" in _html,
      "6: W8 — «Исторические данные» только при свежем пульсе (зависание — «не обновляется»)")
_guide = (ROOT / "docs" / "ui_api_guide.md").read_text(encoding="utf-8")
check(all(k in _guide for k in _api) and "Аналитика не обновляется ≠ нет связи" in _guide
      and "180" in _guide and "needs_start" in _guide,
      "6: docs/ui_api_guide.md описывает поля и порог")


# ══ 7. Остановка: пулы ═══════════════════════════════════════════════════════

class EternalPool:
    def __init__(self) -> None:
        self.terminated = 0
        self._holders: list = []

    async def close(self):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            self.terminate()
            raise

    def terminate(self):
        self.terminated += 1


class QuickPool(EternalPool):
    async def close(self):
        return None


async def sec7() -> None:
    p = EternalPool()
    m0 = mark()
    t0 = time.monotonic()
    ok = await remote.close_pool(p, "source-БД", timeout=0.2)
    dt = time.monotonic() - t0
    check(ok is False and dt < 0.6 and p.terminated >= 1
          and logs(m0, logging.WARNING, "Пул source-БД не закрылся за 0 с — соединения оборваны"),
          f"7: «вечный» close() — возврат за срок ({dt:.2f} с), обрыв, WARNING: {logs(m0)}")
    m0 = mark()
    ok = await remote.close_pool(QuickPool(), "аналитической БД", timeout=0.2)
    check(ok is True and logs(m0, logging.INFO, "Пул аналитической БД закрыт"), "7: штатное закрытие — INFO")
    check(await remote.close_pool(None, "x") is True, "7: пула нет — ничего")
    t = asyncio.create_task(asyncio.sleep(3600))
    check(await remote.cancel_and_wait(t, 0.2) is True and t.cancelled(), "7: cancel_and_wait")
    m0 = mark()
    check(await remote.wait_bounded(asyncio.sleep(5), 0.1, "воркер") is False
          and logs(m0, logging.WARNING, "воркер не уложилась"), "7: wait_bounded со сроком")

asyncio.run(sec7())
_main = (ROOT / "main.py").read_text(encoding="utf-8")
check("timeout_graceful_shutdown=10" in _main and "close_source_pool(), close_pools()" in _main
      and "остановлен (за" in _main, "7: main — срок uvicorn, пулы параллельно, «остановлен за N с»")


# ══ 8. history_sync ══════════════════════════════════════════════════════════

async def sec8() -> None:
    import online.history_sync as hs
    S.history_sync_tick_timeout = 0.2
    S.watchdog_cancel_wait_sec = 0.2
    w = hs.HistorySyncWorker(interval_sec=0.05)
    calls: list = []

    async def slow_tick():
        calls.append(time.monotonic())
        if len(calls) == 1:
            await asyncio.sleep(5)

    w._tick = slow_tick
    m0 = mark()
    w.start()
    ok = await until(lambda: len(calls) >= 2, 3)
    await w.stop()
    check(ok and logs(m0, logging.WARNING, "тик синхронизации не уложился"),
          f"8: тик дольше срока — WARNING, цикл продолжается: {logs(m0, logging.WARNING)}")
    S.history_sync_tick_timeout = DEFAULTS["history_sync_tick_timeout"]

    import db.pool as dbp
    _CP.clear()
    asyncpg.create_pool = _fake_create_pool
    try:
        dbp._sync_source_pool = None
        await dbp.sync_source_pool()
    finally:
        asyncpg.create_pool = _real_create_pool
        dbp._sync_source_pool = None
    check(_CP and _CP[0]["command_timeout"] == S.source_query_timeout and _CP[0]["min_size"] == 0
          and _CP[0]["max_size"] == 4, f"8: пул history_sync — command_timeout и min_size=0: {_CP}")
    check("remote.connection(" in inspect.getsource(hs._sync_device), "8: _sync_device — через помощника")

asyncio.run(sec8())


# ══ 9. Конфиг ════════════════════════════════════════════════════════════════

_min = {"databases": {"source": "s", "analytics": "a"}, "anthropic": {"api_key": "k"}}
d = cfgmod.Settings(_min)
check((d.source_acquire_timeout, d.source_connect_timeout, d.source_release_timeout,
       d.source_statement_timeout, d.source_keepalive_idle, d.source_keepalive_interval,
       d.source_keepalive_count, d.source_tcp_user_timeout, d.pool_close_timeout)
      == (120, 30, 10, 110, 60, 10, 6, 120, 10)
      and (d.watchdog_enabled, d.watchdog_tick_sec, d.watchdog_stall_sec, d.watchdog_cancel_wait_sec,
           d.watchdog_max_restarts, d.watchdog_restart_window_sec, d.watchdog_cycle_warn_sec,
           d.history_sync_tick_timeout) == (True, 30, 600, 15, 3, 3600, 120, 300)
      and not d.source_statement_timeout_fixed,
      "9: умолчания без новых секций (§2.2)")
_ex = yaml.safe_load((ROOT / "config.example.yml").read_text(encoding="utf-8"))
e9 = cfgmod.Settings(_ex)
check(all(getattr(e9, k) == getattr(d, k) for k in (
    "source_acquire_timeout", "source_connect_timeout", "source_release_timeout",
    "source_statement_timeout", "source_keepalive_idle", "source_keepalive_interval",
    "source_keepalive_count", "source_tcp_user_timeout", "pool_close_timeout", "watchdog_enabled",
    "watchdog_tick_sec", "watchdog_stall_sec", "watchdog_cancel_wait_sec", "watchdog_max_restarts",
    "watchdog_restart_window_sec", "watchdog_cycle_warn_sec", "history_sync_tick_timeout")),
      "9: config.example.yml совпадает с умолчаниями")
f9 = cfgmod.Settings({"databases": {"source": "s", "analytics": "a", "source_query_timeout": 60,
                                    "source_statement_timeout": 90}, "anthropic": {"api_key": "k"}})
check(f9.source_statement_timeout == 50 and f9.source_statement_timeout_fixed
      and f9.source_acquire_timeout == 60, "9: statement_timeout ≥ query_timeout — поправлен вниз")
check("source_statement_timeout_fixed" in _main, "9: main пишет WARNING о поправке")
# W11: копия шаблона с поднятым source_query_timeout — вычисляемые сроки следуют за ним
_ex11 = yaml.safe_load((ROOT / "config.example.yml").read_text(encoding="utf-8"))
_ex11["databases"]["source_query_timeout"] = 300
e11 = cfgmod.Settings(_ex11)
check(e11.source_statement_timeout == 290 and e11.source_acquire_timeout == 300
      and "source_statement_timeout" not in _ex11["databases"]
      and "source_acquire_timeout" not in _ex11["databases"],
      f"9: W11 — шаблон не замораживает вычисляемые сроки: statement {e11.source_statement_timeout}, "
      f"acquire {e11.source_acquire_timeout}")


# ══ 10. Статус-тик ═══════════════════════════════════════════════════════════

async def sec10() -> None:
    calls: list = []

    async def gos(sn, et, pid):
        calls.append(sn)
        return None

    async def setting(key, default=None):
        return default

    real = (odb.get_open_segment, dba.get_app_setting)
    odb.get_open_segment, dba.get_app_setting = gos, setting
    try:
        m10 = mgr.OnlineManager()
        e = NS(router_sn="SN", equip_type="pcc", panel_id=1, _fault_ref=None, tz=timezone.utc,
               last_data_ts=datetime.now(timezone.utc) - timedelta(seconds=500),
               live_ok_at=datetime.now(timezone.utc) - timedelta(seconds=95),
               beat_mono=time.monotonic(), poll_interval_sec=30)
        m10._engines = {"SN|pcc|1": e}
        m0 = mark()
        for _ in range(3):
            await m10._tick_status_lines()
        w = logs(m0, logging.WARNING, "данные устарели")
        check(len(w) == 1 and "статус и гейт заморожены" in w[0] and "движок: продвигается" in w[0]
              and "аналитика обновлялась" in w[0] and calls == [],
              f"10: вход в «устарело» — одно WARNING, запросов нет: {w} {calls}")
        e.last_data_ts = datetime.now(timezone.utc)
        await m10._tick_status_lines()
        await m10._tick_status_lines()
        i = logs(m0, logging.INFO, "данные снова свежие")
        check(len(i) == 1 and "перерыв" in i[0] and len(calls) == 2,
              f"10: выход — одно INFO, запросы как раньше: {i} {calls}")
    finally:
        odb.get_open_segment, dba.get_app_setting = real

asyncio.run(sec10())


# ══ 11. Регрессия: выдача удалённых пулов — только через db/remote ═══════════

for _p in ("analytics/source.py", "online/history_sync.py", "online/engine.py", "db/source.py",
           "analytics/runner.py"):
    _t = (ROOT / _p).read_text(encoding="utf-8")
    check(".acquire()" not in _t and "acquire_sync_source" not in _t
          and "asyncpg.connect(" not in _t and "asyncpg.create_pool(" not in _t,
          f"11: {_p} — нет выдачи / подключения к source в обход db/remote")
_pool_txt = (ROOT / "db" / "pool.py").read_text(encoding="utf-8")
check(_pool_txt.count(".acquire()") == 1 and "_analytics_pool.acquire()" in _pool_txt
      and "acquire_sync_source" not in _pool_txt, "11: db/pool — выдача только локального пула")
_eng_txt = (ROOT / "online" / "engine.py").read_text(encoding="utf-8")
check("asyncio.gather(" not in _eng_txt.split("async def _load_data", 1)[1].split("\nasync def ", 1)[0]
      and _eng_txt.count("remote.gather_or_cancel(") == 6
      and "asyncio.to_thread(" not in _eng_txt and _eng_txt.count("remote.to_thread_beating(") == 2,
      "11: движок — gather_or_cancel (6 мест) и нарезка с пульсом (2 места)")
for _p, _needle in (("online/incident_gate.py", "context=detached_context()"),
                    ("online/verifier.py", "context=detached_context()"),
                    ("online/engine.py", "context=remote.detached_context()")):
    check(_needle in (ROOT / _p).read_text(encoding="utf-8"), f"11: {_p} — фоновая задача без метки (Р4)")
_svc = (ROOT / "cg-analytics.service").read_text(encoding="utf-8")
_inst = (ROOT / "install.sh").read_text(encoding="utf-8")
check("After=network.target postgresql.service" in _svc and "10-postgresql.conf" in _svc
      and "pg_lsclusters" in _inst and "cg-analytics.service.d/10-postgresql.conf" in _inst
      and "Wants=%s" in _inst, "11: юнит и install.sh — порядок к кластеру PostgreSQL drop-in'ом")
check("IFS='|' read -r AN_HOST AN_PORT" in _inst and "parse_qs(u.query)" in _inst
      and 'if [[ "$AN_LOC" == "?" ]]' in _inst and "awk '{print $1}'" not in _inst.split("AN_LOC", 1)[1][:600],
      "11: W10 — install.sh: хост|порт без схлопывания пробела, сокет из ?host=, сбой python — не «локальная»")
_plan = (ROOT / "docs" / "plans" / "design_v5124_watchdog.md").read_text(encoding="utf-8")
check("до 24.10 01:30 UTC = 04:30 МСК" in _plan and "НЕ «только что»" in _plan,
      "11: W12 — план: срок override таймера до 01:30 UTC и проверка LAST")

# Пульс владельца из контекста: detached_context без метки
_t9 = remote.EngineTag("T", lambda: None)
_tok = remote.bind_owner(_t9)
check(remote.detached_context().run(remote.current_owner) is None and remote.current_owner() is _t9,
      "11: detached_context — без метки движка")
remote.reset_owner(_tok)
check(mgr._fmt_dur(45) == "45 с" and mgr._fmt_dur(660) == "11 мин" and mgr._fmt_dur(7500) == "2 ч 05 мин"
      and mgr._fmt_dur(4 * 86400 + 20 * 3600) == "4 сут 20 ч", "11: длительности журнала")

for k, v in DEFAULTS.items():
    setattr(S, k, v)

check(not _DB_HITS, f"Тест обращался к настоящей БД ({len(_DB_HITS)}):\n" + "\n".join(_DB_HITS[:2]))

if _errors:
    print(f"FAIL — {len(_errors)} расхождений:")
    for x in _errors:
        print(f"  • {x}")
    sys.exit(1)
print("Все проверки пройдены.")
