# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Удалённая БД телеметрии (source): ни одного ожидания без срока (v5.1.24).

17.10 ~13:40 запрос движка ДГУ №2 сорвался по command_timeout на деградировавшем
VPN. asyncpg вернул соединение в пул и ждал подтверждения отмены без срока
(release без timeout → ожидание cancel_waiter / ответа служебного соединения
отмены) — движок молчал до 22.10 09:25, а слот пула (3 на весь парк) был
занят навсегда. Здесь всё, что ходит к удалённой БД:

- connection(pool): ожидание слота — со сроком; после успешного запроса
  соединение возвращается со сроком; при ЛЮБОЙ ошибке или отмене внутри блока
  оно обрывается (terminate), а не возвращается — ждать отмену некому;
- create_pool / connect: command_timeout, statement_timeout сессии (параметр
  запуска — переживает RESET ALL при возврате в пул), TCP keepalive и
  TCP_USER_TIMEOUT на сокет (Linux);
- пульс движка: каждый завершённый удалённый вызов продлевает пульс задачи-
  владельца (метка в ContextVar) — сторож отличает долгую догонку от зависания;
- gather_or_cancel: первая ошибка отменяет соседей — их слоты свободны сразу;
- to_thread_beating: нарезка в потоке с пульсом; отмена дожидается конца потока
  (поток не прерывается — второй на ту же работу не запускаем);
- close_pool: закрытие со сроком, затем обрыв;
- task_stack: где висит задача (цепочка await, а не один верхний кадр).
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
import socket
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Callable

import asyncpg

from config import settings

logger = logging.getLogger(__name__)


# ── Владелец (движок) и пульс ─────────────────────────────────────────────────

@dataclass(eq=False)
class EngineTag:
    """Метка движка: кладётся в run(), её наследуют дочерние задачи (gather, shield)."""
    key: str
    touch: Callable[[], None]
    # Завершённых удалённых вызовов (блоков connection) — продвижение для потолка
    # сторожа: медленная, но живая догонка курсор не двигает, а вызовы завершает
    # (ревью v5.1.24, W3)
    calls: int = 0


_owner: contextvars.ContextVar["EngineTag | None"] = contextvars.ContextVar(
    "remote_owner", default=None)


def bind_owner(tag: EngineTag | None) -> contextvars.Token:
    return _owner.set(tag)


def reset_owner(token: contextvars.Token) -> None:
    try:
        _owner.reset(token)
    except (ValueError, RuntimeError):
        # Токен из другого контекста (отмена посреди run) — метка умрёт с задачей
        _owner.set(None)


def current_owner() -> EngineTag | None:
    return _owner.get()


def touch_owner() -> None:
    """Продлить пульс движка, которому принадлежит текущая задача (если есть)."""
    tag = _owner.get()
    if tag is not None:
        try:
            tag.touch()
        except Exception:
            pass


def _call_done() -> None:
    """Удалённый вызов завершён (успех, ошибка, отказ подключения): счётчик
    владельца + пульс. Поток нарезки и локальная БД счётчик не двигают."""
    tag = _owner.get()
    if tag is not None:
        tag.calls += 1
    touch_owner()


def detached_context() -> contextvars.Context:
    """Контекст без метки движка — для фоновых задач, которые движок только
    заказывает (разбор аварии, очередь заключений, верификация): их ожидание
    не должно продлевать пульс движка и не должно попадать в его стек."""
    ctx = contextvars.copy_context()
    ctx.run(_owner.set, None)
    return ctx


def tasks_of(tag: EngineTag | None) -> list[asyncio.Task]:
    """Живые задачи с меткой этого движка (Python 3.12+: Task.get_context)."""
    if tag is None:
        return []
    out = []
    for t in asyncio.all_tasks():
        get_ctx = getattr(t, "get_context", None)
        if get_ctx is None:
            continue
        try:
            if get_ctx().get(_owner) is tag and not t.done():
                out.append(t)
        except Exception:
            continue
    return out


# ── Таймаут ожидания слота ────────────────────────────────────────────────────

class PoolBusy(TimeoutError):
    """Нет свободного соединения (или нет подключения) за срок — это не таймаут
    запроса: подменять его текстом «уменьшите интервал» нельзя."""


# ── Учёт выданных соединений (диагностика сторожа и уборка) ───────────────────

_held: dict[int, dict] = {}


def _held_add(conn, pool, label: str) -> dict:
    entry = {
        "conn":  conn,
        "pool":  pool,
        "label": label,
        "owner": _owner.get(),
        "since": time.monotonic(),
        "task":  asyncio.current_task(),
    }
    _held[id(entry)] = entry
    return entry


def _held_drop(entry: dict) -> None:
    _held.pop(id(entry), None)


def _owner_match(entry: dict, owner: Any) -> bool:
    if owner is None:
        return True
    tag = entry.get("owner")
    if isinstance(owner, EngineTag):
        return tag is owner
    return tag is not None and tag.key == owner


def held(owner: Any = None) -> list[dict]:
    """Соединения внутри блоков connection(): владелец, возраст, метка пула.

    owner — EngineTag (по идентичности), ключ машины или None (все)."""
    now = time.monotonic()
    out = []
    for e in list(_held.values()):
        if not _owner_match(e, owner):
            continue
        t = e.get("task")
        out.append({
            "owner":   e["owner"].key if e.get("owner") is not None else None,
            "label":   e["label"],
            "age_sec": round(now - e["since"], 1),
            "task":    t.get_name() if t is not None else None,
        })
    return out


def stale_conn_sec() -> float:
    """Старше этого соединение не может быть ни в законном запросе, ни в
    законном возврате: ожидание слота + запрос + возврат + 60 с (= 310 с)."""
    return (settings.source_acquire_timeout + settings.source_query_timeout
            + settings.source_release_timeout + 60)


def terminate_stale(owner: Any, older_than: float) -> list[dict]:
    """Оборвать соединения владельца, выданные раньше older_than секунд назад.

    Учёт снимается ДО возврата в пул, поэтому здесь только соединения внутри
    тела блока — законного release у них нет (ловушка asyncpg: release по
    истечении бюджета обрывает текущее соединение держателя, а держатель уже
    мог достаться другому). Возвращает описания оборванных."""
    now = time.monotonic()
    out = []
    for e in list(_held.values()):
        if not _owner_match(e, owner) or now - e["since"] <= older_than:
            continue
        _held_drop(e)
        _terminate_quietly(e["conn"])
        out.append({"owner": e["owner"].key if e.get("owner") is not None else None,
                    "label": e["label"], "age_sec": round(now - e["since"], 1)})
    return out


def _terminate_quietly(conn) -> None:
    """Оборвать соединение; уже отсоединённый прокси (соединение умерло само
    или возвращено) — не ошибка."""
    try:
        conn.terminate()
    except Exception:
        pass


# ── Сокет и сессия ────────────────────────────────────────────────────────────

_warned: set[str] = set()


def _warn_once(key: str, msg: str, *args, exc_info: bool = False) -> None:
    if key in _warned:
        return
    _warned.add(key)
    logger.warning(msg, *args, exc_info=exc_info)


def server_settings() -> dict[str, str]:
    """Параметры запуска сессии удалённой БД.

    statement_timeout — именно параметром запуска: SET в init стирается первым
    же RESET ALL при возврате соединения в пул (asyncpg, проба критика 23.10)."""
    st = int(settings.source_statement_timeout or 0)
    return {"statement_timeout": str(st * 1000)} if st > 0 else {}


def apply_keepalive(con, *, label: str = "source") -> dict | None:
    """TCP keepalive (+ TCP_USER_TIMEOUT на Linux) на сокет соединения.

    Возвращает применённое {имя: значение} или None (сокета нет). Нет
    констант (не Linux) — ставится то, что есть. Ошибки не фатальны."""
    tr = getattr(con, "_transport", None)          # asyncpg: приватный атрибут
    sock = None
    if tr is not None:
        try:
            sock = tr.get_extra_info("socket")
        except Exception:
            sock = None
    if sock is None or not hasattr(sock, "setsockopt"):
        _warn_once(f"nosock:{label}", "%s: keepalive не установлен — нет доступа к сокету "
                   "соединения", label)
        return None
    applied: dict[str, int] = {}
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        applied["SO_KEEPALIVE"] = 1
    except Exception:
        _warn_once(f"keepalive:{label}", "%s: SO_KEEPALIVE не установлен", label, exc_info=True)
        return None
    for name, val in (("TCP_KEEPIDLE", settings.source_keepalive_idle),
                      ("TCP_KEEPINTVL", settings.source_keepalive_interval),
                      ("TCP_KEEPCNT", settings.source_keepalive_count)):
        opt = getattr(socket, name, None)
        if not val or opt is None:
            continue
        try:
            sock.setsockopt(socket.IPPROTO_TCP, opt, int(val))
            applied[name] = int(val)
        except Exception:
            _warn_once(f"{name}:{label}", "%s: %s не установлен", label, name, exc_info=True)
    ut = settings.source_tcp_user_timeout
    opt = getattr(socket, "TCP_USER_TIMEOUT", None)
    if ut and opt is not None:
        try:
            sock.setsockopt(socket.IPPROTO_TCP, opt, int(ut) * 1000)
            applied["TCP_USER_TIMEOUT"] = int(ut)
        except Exception:
            _warn_once(f"TCP_USER_TIMEOUT:{label}", "%s: TCP_USER_TIMEOUT не установлен", label,
                       exc_info=True)
    return applied


def describe_applied(applied: dict | None, stmt: str | None) -> str:
    """«keepalive 60/10×6 с, TCP_USER_TIMEOUT 120 с, statement_timeout 110s» —
    сокет по фактическому результату setsockopt, а не по конфигу; statement_timeout
    — параметр запуска сессии, принятый сервером при подключении."""
    parts = []
    if applied is None:
        parts.append("keepalive НЕ установлен (нет сокета)")
    else:
        idle, intvl, cnt = (applied.get("TCP_KEEPIDLE"), applied.get("TCP_KEEPINTVL"),
                            applied.get("TCP_KEEPCNT"))
        if idle or intvl or cnt:
            parts.append(f"keepalive {idle or '—'}/{intvl or '—'}×{cnt or '—'} с")
        else:
            parts.append("keepalive (интервалы ОС)")
        parts.append(f"TCP_USER_TIMEOUT {applied['TCP_USER_TIMEOUT']} с"
                     if "TCP_USER_TIMEOUT" in applied else "TCP_USER_TIMEOUT недоступен (не Linux)")
    parts.append(f"statement_timeout {stmt}" if stmt else "statement_timeout не задан")
    return ", ".join(parts)


def _stmt_text(ss: dict[str, str] | None) -> str | None:
    """statement_timeout параметра запуска (мс) → «110s», как его печатает SHOW."""
    raw = (ss or {}).get("statement_timeout")
    try:
        ms = int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        return str(raw)
    if ms <= 0:
        return None
    return f"{ms // 1000}s" if ms % 1000 == 0 else f"{ms}ms"


def _make_init(label: str, ss: dict[str, str] | None = None):
    """init-колбэк пула: keepalive на каждое новое соединение; один раз на пул —
    INFO с тем, что удалось применить.

    Ни одного запроса в init (ревью v5.1.24, W1): SHOW statement_timeout на первом
    соединении шёл внутри acquire — срыв или отмена init уводили asyncpg в close()
    без срока (ожидание отмены через мёртвый VPN), соединение ещё не в учёте, и
    уборщик его не видел. statement_timeout — параметр запуска: соединение
    установлено — значит, сервер его принял (недопустимое значение он отвергает
    при подключении), так что в INFO — то, что передано."""
    state = {"reported": False}
    stmt = _stmt_text(server_settings() if ss is None else ss)

    async def _init(con) -> None:
        applied = apply_keepalive(con, label=label)
        if state["reported"]:
            return
        state["reported"] = True
        logger.info("%s: %s", label, describe_applied(applied, stmt))

    return _init


async def create_pool(dsn: str, *, min_size: int, max_size: int, label: str,
                      extra_server_settings: dict[str, str] | None = None,
                      **connect_kwargs: Any) -> asyncpg.Pool:
    """Пул к удалённой БД: command_timeout, срок подключения, statement_timeout
    сессии, keepalive на сокет. min_size=0 — старт сервиса от VPN не зависит.

    extra_server_settings / connect_kwargs — для стенда (только чтение, ssl)."""
    ss = {**server_settings(), **(extra_server_settings or {})}
    return await asyncpg.create_pool(
        dsn,
        min_size=min_size,
        max_size=max_size,
        command_timeout=settings.source_query_timeout,
        timeout=settings.source_connect_timeout,
        server_settings=ss or None,
        init=_make_init(label, ss),
        **connect_kwargs,
    )


async def connect(dsn: str, *, label: str = "source", remote: bool = True):
    """Одиночное соединение (db/source.py, «Синхронизация реестра»): те же сроки;
    keepalive и statement_timeout — только к удалённой БД."""
    kw: dict[str, Any] = {"timeout": settings.source_connect_timeout,
                          "command_timeout": settings.source_query_timeout}
    if remote and server_settings():
        kw["server_settings"] = server_settings()
    con = await asyncpg.connect(dsn, **kw)
    if remote:
        apply_keepalive(con, label=label)
    return con


async def close_connection(con) -> None:
    """Закрыть одиночное соединение со сроком; не вышло — оборвать."""
    if con is None:
        return
    try:
        await asyncio.wait_for(con.close(timeout=settings.source_release_timeout),
                               settings.source_release_timeout + 1)
    except asyncio.CancelledError:
        _terminate_quietly(con)
        raise
    except Exception as e:
        _terminate_quietly(con)
        logger.debug("Соединение не закрылось штатно — оборвано (%s)", type(e).__name__)


def _acquire_timeout(pool) -> float:
    """Срок ожидания слота: source_acquire_timeout, но не меньше command_timeout
    пула — диагностические скрипты (check_v512x_data.py) ставят свой пул с
    command_timeout 600 с, и 600-секундные запросы в 3 слотах не должны давать
    PoolBusy (критика v5.1.24, Р17)."""
    acq = float(settings.source_acquire_timeout)
    try:
        ct = (getattr(pool, "_connect_kwargs", None) or {}).get("command_timeout")
        if ct:
            acq = max(acq, float(ct))
    except Exception:
        pass
    return acq


@asynccontextmanager
async def connection(pool: asyncpg.Pool, *, label: str = "source"):
    """Соединение пула удалённой БД — все ожидания со сроком.

    Ошибка или отмена внутри блока → соединение ОБРЫВАЕТСЯ: протокол мог
    остаться в «отмене», а все три бесконечных ожидания asyncpg сидят именно
    там (17–22.10, ДГУ №2). Слот освобождается сразу (_release_on_close).
    Успех → возврат со сроком source_release_timeout (сброс сессии ≈ 1 RTT);
    не уложился — asyncpg сам обрывает, строки вызывающему всё равно отдаются.
    """
    acq = _acquire_timeout(pool)
    t0 = time.monotonic()
    try:
        conn = await pool.acquire(timeout=acq)
    except asyncio.TimeoutError:
        _call_done()
        waited = time.monotonic() - t0
        if waited < acq - 1:
            # Сам слот был свободен — не уложилось подключение (VPN лежит)
            raise PoolBusy(f"нет подключения к {label} за {waited:.0f} с") from None
        raise PoolBusy(f"пул {label} занят {acq:.0f} с") from None
    except BaseException:
        _call_done()                # отказ подключения — тоже завершённый вызов
        raise
    touch_owner()
    entry = _held_add(conn, pool, label)
    try:
        yield conn
    except BaseException:
        # Таймаут, обрыв, отмена задачи: состояние соединения неизвестно, и оно,
        # возможно, ждёт подтверждения отмены, которого через мёртвый VPN не будет.
        # Не возвращаем — обрываем: слот свободен сразу, ждать нечего
        _held_drop(entry)
        _terminate_quietly(conn)
        raise
    else:
        _held_drop(entry)
        try:
            await pool.release(conn, timeout=settings.source_release_timeout)
        except asyncio.CancelledError:
            # Возврат под shield досчитает сам (≤ source_release_timeout); обрывать
            # посреди возврата нельзя — держатель уже мог достаться другому
            raise
        except asyncpg.InterfaceError as e:
            # Пул закрыт (остановка сервиса) — соединение оборвано закрытием
            logger.debug("%s: соединение не возвращено в пул: %s", label, e)
        except Exception as e:
            logger.warning("%s: соединение не вернулось в пул за %.0f с — оборвано (%s)",
                           label, settings.source_release_timeout, type(e).__name__)
    finally:
        _call_done()


# ── Параллельные запросы ──────────────────────────────────────────────────────

async def _cancel_all(tasks: list[asyncio.Future]) -> None:
    pending = [t for t in tasks if not t.done()]
    for t in pending:
        t.cancel()
    if pending:
        await asyncio.wait(pending, timeout=settings.watchdog_cancel_wait_sec)
    for t in tasks:
        if t.done() and not t.cancelled():
            try:
                t.exception()       # отметить как прочитанное — без «never retrieved»
            except BaseException:
                pass


async def gather_or_cancel(*aws):
    """asyncio.gather, но первая ошибка отменяет соседей и наружу уходит
    ИСХОДНОЕ исключение (не группа). Без этого соседи держат слоты пула до
    своего command_timeout. Внешняя отмена — отменить детей и дождаться их
    (со сроком), затем отмена дальше."""
    tasks = [asyncio.ensure_future(a) for a in aws]
    if not tasks:
        return []
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
    except BaseException:
        await _cancel_all(tasks)
        raise
    failed = next((t for t in tasks
                   if t.done() and not t.cancelled() and t.exception() is not None), None)
    if failed is not None:
        await _cancel_all(tasks)
        raise failed.exception()
    if any(t.cancelled() for t in tasks):
        # Ребёнка отменили снаружи — как gather: отмена наружу
        await _cancel_all(tasks)
        raise asyncio.CancelledError()
    return [t.result() for t in tasks]


# Живые потоки to_thread_beating: id → {owner, label, since}
_threads: dict[int, dict] = {}


def threads(owner: Any = None) -> list[dict]:
    """Потоки to_thread_beating, которые ещё считают: владелец, возраст, метка.

    owner — EngineTag (по идентичности), ключ машины или None (все)."""
    now = time.monotonic()
    return [{"owner": e["owner"].key if e.get("owner") is not None else None,
             "label": e["label"], "age_sec": round(now - e["since"], 1)}
            for e in list(_threads.values()) if _owner_match(e, owner)]


async def to_thread_beating(func: Callable[[], Any], *, interval: float = 30.0,
                            label: str = "нарезка") -> Any:
    """asyncio.to_thread с пульсом владельца, пока поток жив (CPU-часть
    нарезки длинного окна не считается зависанием).

    Отмена поток не бросает (ревью v5.1.24, W6): поток не прерывается, а задача,
    вышедшая по отмене сразу, отдавала машину новому движку — тот запускал ту же
    нарезку вторым потоком, и перезапуски копили потоки (GIL, пул исполнителя
    общий для всех машин). Отменённая задача дожидается конца потока и только
    потом завершается отменой: для менеджера это обычный зомби — новый движок
    только после конца потока."""
    entry = {"owner": _owner.get(), "label": label, "since": time.monotonic()}
    _threads[id(entry)] = entry
    fut = asyncio.ensure_future(asyncio.to_thread(func))
    try:
        while True:
            done, _ = await asyncio.wait({fut}, timeout=interval)
            if done:
                return fut.result()
            touch_owner()
    except asyncio.CancelledError:
        # Пульс больше не продлеваем: движок снимают, задача — зомби до конца потока
        while not fut.done():
            try:
                await asyncio.wait({fut})
            except asyncio.CancelledError:
                continue
        if not fut.cancelled():
            try:
                fut.exception()     # результат выброшен — без «never retrieved»
            except BaseException:
                pass
        raise
    finally:
        _threads.pop(id(entry), None)


# ── Закрытие и остановка со сроком ────────────────────────────────────────────

def _busy_count(pool) -> int:
    try:
        return sum(1 for h in getattr(pool, "_holders", []) if getattr(h, "_in_use", None) is not None)
    except Exception:
        return -1


async def close_pool(pool: asyncpg.Pool | None, label: str, timeout: float | None = None) -> bool:
    """Закрыть пул за ограниченное время; не закрылся — оборвать соединения.

    Отменённый Pool.close() сам зовёт terminate() (asyncpg). True — закрыт штатно."""
    if pool is None:
        return True
    timeout = settings.pool_close_timeout if timeout is None else timeout
    now = time.monotonic()
    owners = [{"owner": e["owner"].key if e.get("owner") is not None else None,
               "task": e["task"].get_name() if e.get("task") is not None else None,
               "age_sec": now - e["since"]}
              for e in list(_held.values()) if e.get("pool") is pool]
    try:
        await asyncio.wait_for(pool.close(), timeout)
        logger.info("Пул %s закрыт", label)
        return True
    except BaseException as e:
        busy = _busy_count(pool)
        try:
            pool.terminate()
        except Exception:
            pass
        if isinstance(e, asyncio.TimeoutError):
            who = ", ".join(f"{h['owner'] or h['task'] or '?'} ({h['age_sec']:.0f} с)" for h in owners)
            logger.warning("Пул %s не закрылся за %.0f с — соединения оборваны (в работе было %s%s)",
                           label, timeout, busy if busy >= 0 else "?",
                           f": {who}" if who else "")
            return False
        if isinstance(e, asyncio.CancelledError):
            raise
        logger.warning("Пул %s: ошибка закрытия — соединения оборваны", label, exc_info=True)
        return False


async def cancel_and_wait(task: asyncio.Task | None, timeout: float) -> bool:
    """Отменить задачу и подождать её конца не дольше timeout. True — завершилась."""
    if task is None or task.done():
        return True
    task.cancel()
    await asyncio.wait({task}, timeout=timeout)
    if task.done():
        if not task.cancelled():
            try:
                task.exception()
            except BaseException:
                pass
        return True
    return False


async def wait_bounded(aw, timeout: float, label: str) -> bool:
    """Дождаться остановки компонента не дольше timeout (остановка сервиса)."""
    try:
        await asyncio.wait_for(aw, timeout)
        return True
    except asyncio.TimeoutError:
        logger.warning("Остановка: %s не уложилась в %.0f с — дальше без неё", label, timeout)
        return False
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("Остановка: ошибка остановки %s", label, exc_info=True)
        return False


# ── Где висит задача ──────────────────────────────────────────────────────────

def _short_path(fn: str) -> str:
    parts = fn.replace("\\", "/").split("/")
    return "/".join(parts[-2:]) if len(parts) > 1 else fn


def _short_repr(obj: Any, n: int = 160) -> str:
    try:
        r = repr(obj)
    except Exception:
        r = f"<{type(obj).__qualname__}>"
    return r if len(r) <= n else r[: n - 1] + "…"


def _hidden_inner(obj: Any, prev_frame: Any) -> Any:
    """Звено, за которым цепочка await не видна через cr_await (ревью v5.1.24, W2):

    - async_generator_asend / athrow — шаг @asynccontextmanager (connection():
      __aenter__ ждёт pool.acquire, __aexit__ — pool.release); у них нет ни
      кадра, ни ag_await — дальше идём в сам генератор (self.gen кадра
      __aenter__ / __aexit__);
    - coroutine_wrapper — результат coro.__await__() (PoolAcquireContext asyncpg:
      `await pool.acquire()`); корутина за ним — среди его ссылок.
    None — скрытого звена нет."""
    tname = type(obj).__name__
    if tname in ("async_generator_asend", "async_generator_athrow") and prev_frame is not None:
        try:
            gen = getattr(prev_frame.f_locals.get("self"), "gen", None)
        except Exception:
            gen = None
        return gen if getattr(gen, "ag_frame", None) is not None else None
    if tname == "coroutine_wrapper":
        import gc
        for r in gc.get_referents(obj):
            if hasattr(r, "cr_await"):
                return r
    return None


def coro_chain(coro: Any, limit: int = 40) -> list[str]:
    """Цепочка await от корутины вглубь: «файл:строка функция» каждого кадра.

    Task.get_stack() у приостановленной корутины отдаёт ОДИН верхний кадр —
    по нему не видно, где висим (проба критика 23.10: ['run']). Звено без
    кадра (Cython-корутина asyncpg, Future-итератор) — по имени/repr; шаг
    @asynccontextmanager и coroutine_wrapper — насквозь (_hidden_inner)."""
    out: list[str] = []
    seen: set[int] = set()
    prev_frame = None
    while coro is not None and len(out) < limit and id(coro) not in seen:
        seen.add(id(coro))
        inner = _hidden_inner(coro, prev_frame)
        if inner is not None and id(inner) not in seen:
            coro = inner
            continue
        frame = None
        for attr in ("cr_frame", "gi_frame", "ag_frame"):
            frame = getattr(coro, attr, None)
            if frame is not None:
                break
        name = getattr(coro, "__qualname__", None) or type(coro).__qualname__
        if frame is not None:
            out.append(f"{_short_path(frame.f_code.co_filename)}:{frame.f_lineno} {name}")
        else:
            out.append(f"{name} [{_short_repr(coro, 100)}]")
        prev_frame = frame
        nxt = None
        for attr in ("cr_await", "gi_yieldfrom", "ag_await"):
            nxt = getattr(coro, attr, None)
            if nxt is not None:
                break
        coro = nxt
    return out


def task_stack(task: asyncio.Task, limit: int = 40) -> list[str]:
    """Цепочка await задачи + то, чего она ждёт (Future/Task/gather)."""
    try:
        lines = coro_chain(task.get_coro(), limit=limit)
    except Exception as e:
        lines = [f"(цепочка не снята: {type(e).__name__})"]
    waiter = getattr(task, "_fut_waiter", None)
    if waiter is not None:
        lines.append(f"ждёт: {_short_repr(waiter)}")
    return lines


def format_task_stacks(tasks: list[asyncio.Task], limit: int = 40) -> str:
    """Многострочный дамп задач для записи ERROR сторожа."""
    blocks = []
    for t in tasks:
        head = f"  ● задача {t.get_name()}"
        body = "\n".join(f"      {x}" for x in task_stack(t, limit=limit))
        blocks.append(f"{head}\n{body}" if body else head)
    return "\n".join(blocks) if blocks else "  (задач с меткой движка нет)"


__all__ = [
    "EngineTag", "PoolBusy", "bind_owner", "reset_owner", "current_owner", "touch_owner",
    "detached_context", "tasks_of", "held", "terminate_stale", "stale_conn_sec",
    "apply_keepalive", "server_settings", "create_pool", "connect", "close_connection",
    "connection", "gather_or_cancel", "to_thread_beating", "close_pool", "cancel_and_wait",
    "wait_bounded", "coro_chain", "task_stack", "format_task_stacks", "threads",
]
