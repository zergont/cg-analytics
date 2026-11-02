# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Общее для инструментов: установки, окна времени, запуск вызова с конвертом,
журналом, сроком и усечением.

Модель оперирует именем установки или её id (разд. 4.3 проекта); тройки и
серийники наружу не выдаются (Р11). Время во входе и в ответах — пояс сервиса
(МСК), дата — в конверте.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from config import get_tz
from analytics.labels import fmt_local

from .config import McpSettings
from .envelope import Envelope, next_label

logger = logging.getLogger(__name__)

SETTINGS: McpSettings | None = None   # ставит server.build_app


def settings() -> McpSettings:
    global SETTINGS
    if SETTINGS is None:
        from .config import load
        SETTINGS = load()
    return SETTINGS


class ToolError(Exception):
    """Ошибка, которую видит модель текстом («ОШИБКА: …») — не исключение хоста."""


# ── Установки ────────────────────────────────────────────────────────────────

@dataclass
class UnitRef:
    id: int | None
    name: str
    object: str
    router_sn: str
    equip_type: str
    panel_id: int
    active: bool
    panel: dict[str, Any] = field(default_factory=dict)
    sensors: list[dict[str, Any]] = field(default_factory=list)
    neighbor_names: list[str] = field(default_factory=list)

    @property
    def triple(self) -> tuple[str, str, int]:
        return self.router_sn, self.equip_type, self.panel_id

    @property
    def title(self) -> str:
        return f"{self.name} (установка {self.id})" if self.id is not None else self.name

    @property
    def binding(self) -> dict[str, Any]:
        return {"controller_id": self.panel.get("controller_id"),
                "engine_id": self.panel.get("engine_id"), "kb_path": self.panel.get("kb_path")}


def _unit_from_tree(u: dict[str, Any]) -> UnitRef:
    p = u.get("panel") or {}
    return UnitRef(
        id=u.get("id"), name=u["name"], object=u.get("object") or u.get("router_sn") or "",
        router_sn=u["router_sn"], equip_type=p.get("equip_type", ""), panel_id=int(p.get("panel_id", 0)),
        active=bool(u.get("active", True)), panel=p, sensors=list(u.get("sensors") or []),
        neighbor_names=list(u.get("neighbors") or []),
    )


class UnitsCache:
    """Дерево установок с обновлением раз в ttl секунд."""

    def __init__(self, ttl_sec: float = 60.0) -> None:
        self.ttl = ttl_sec
        self._tree: dict[str, Any] | None = None
        self._at = 0.0
        self._lock = asyncio.Lock()

    async def tree(self, force: bool = False) -> dict[str, Any]:
        if not force and self._tree is not None and time.monotonic() - self._at < self.ttl:
            return self._tree
        async with self._lock:
            if force or self._tree is None or time.monotonic() - self._at >= self.ttl:
                from db.units import get_tree
                from .catalog import get_object_names
                names = await get_object_names()
                self._tree = await get_tree(panel_types=settings().panel_types, object_names=names)
                self._at = time.monotonic()
        return self._tree

    async def units(self) -> list[UnitRef]:
        from db.units import flat_units
        return [_unit_from_tree(u) for u in flat_units(await self.tree())]


UNITS = UnitsCache()

_NORM_RE = re.compile(r"[\s\-_.«»\"']+")


def _norm(s: str) -> str:
    s = s.lower().replace("№", "n").replace("no.", "n").replace("ё", "е")
    return _NORM_RE.sub("", s)


async def resolve_unit(text: str | int | None) -> UnitRef:
    """Установка по id («7», «установка 7») или имени («ДЭС №3», «дэс 3»).
    Неоднозначно — ошибка со списком кандидатов; не найдено — список установок."""
    units = await UNITS.units()
    if text is None or str(text).strip() == "":
        raise ToolError("Укажите установку: " + _list_units(units))
    s = str(text).strip()
    m = re.fullmatch(r"(?:установка\s*)?#?(\d+)", s, flags=re.I)
    if m:
        uid = int(m.group(1))
        for u in units:
            if u.id == uid:
                return u
    ns = _norm(s)
    exact = [u for u in units if _norm(u.name) == ns]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise ToolError("Несколько установок с таким именем: " + _list_units(exact))
    part = [u for u in units if ns and (ns in _norm(u.name) or ns in _norm(u.object + u.name))]
    if len(part) == 1:
        return part[0]
    if len(part) > 1:
        raise ToolError(f"«{s}» подходит нескольким установкам, уточните: " + _list_units(part))
    raise ToolError(f"Установка «{s}» не найдена. Есть: " + _list_units(units))


def _list_units(units: list[UnitRef]) -> str:
    items = [f"{u.name} [id {u.id}]" if u.id is not None else u.name for u in units[:30]]
    more = f" и ещё {len(units) - 30}" if len(units) > 30 else ""
    return "; ".join(items) + more if items else "ни одной установки в карте"


async def neighbors_of(unit: UnitRef) -> list[UnitRef]:
    """Соседи — другие активные установки того же объекта (роутера)."""
    return [u for u in await UNITS.units()
            if u.router_sn == unit.router_sn and u.active and u.triple != unit.triple]


# ── Время ────────────────────────────────────────────────────────────────────

_DT_FORMATS = (
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
    "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%Y-%m-%d", "%d.%m.%Y",
)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(s: str | None, *, ref: datetime | None = None) -> datetime | None:
    """Метка времени из текста модели → aware datetime (UTC). Без пояса — пояс
    сервиса. Понимает ISO, «ДД.ММ.ГГГГ ЧЧ:ММ[:СС]», «ДД.ММ ЧЧ:ММ» (текущий год),
    «ЧЧ:ММ» (сегодня), «сейчас». None/пусто → None."""
    if s is None:
        return None
    t = str(s).strip()
    if not t:
        return None
    tz = get_tz()
    ref_local = (ref or now_utc()).astimezone(tz)
    if t.lower() in ("сейчас", "now", "теперь"):
        return ref or now_utc()
    try:
        dt = datetime.fromisoformat(t)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        return dt.astimezone(timezone.utc)
    except ValueError:
        pass
    for fmt in _DT_FORMATS:
        try:
            dt = datetime.strptime(t, fmt)
            return dt.replace(tzinfo=tz).astimezone(timezone.utc)
        except ValueError:
            continue
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?", t)
    if m:
        d, mo, h, mi, se = (int(x) if x else 0 for x in m.groups())
        dt = datetime(ref_local.year, mo, d, h, mi, se, tzinfo=tz)
        return dt.astimezone(timezone.utc)
    m = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", t)
    if m:
        h, mi, se = (int(x) if x else 0 for x in m.groups())
        dt = ref_local.replace(hour=h, minute=mi, second=se, microsecond=0)
        return dt.astimezone(timezone.utc)
    raise ToolError(f"Не понял время «{t}». Формат: ДД.ММ.ГГГГ ЧЧ:ММ[:СС] (пояс сервиса) или ISO.")


def parse_window(from_: str | None, to: str | None, at: str | None = None,
                 minutes: int | None = None, *, default_hours: float = 2.0,
                 max_days: float | None = None) -> tuple[datetime, datetime]:
    """Окно [от, до): явное от/до, либо момент ± минут (по умолчанию 30), либо
    последние default_hours до «сейчас». max_days — предел длины (ошибка)."""
    now = now_utc()
    if at:
        c = parse_ts(at)
        m = int(minutes) if minutes else 30
        t0, t1 = c - timedelta(minutes=m), c + timedelta(minutes=m)
    else:
        t1 = parse_ts(to) or now
        t0 = parse_ts(from_) or (t1 - timedelta(hours=default_hours))
    if t0 >= t1:
        raise ToolError("Начало окна не раньше конца: проверьте «от» и «до».")
    if max_days is not None and (t1 - t0) > timedelta(days=max_days):
        raise ToolError(f"Окно длиннее {max_days:g} сут. Сузьте окно или возьмите другой инструмент "
                        "(долгие тренды — trend).")
    return t0, t1


def fmt_t(ts: datetime | None, day: bool = False) -> str:
    return fmt_local(ts, day=day) or "—"


def est_tokens(text: str) -> int:
    return int(len(text) / float(settings().limit("chars_per_token") or 1.7))


def fmt_num(v: Any, step: float | None = None) -> str:
    """Число для таблицы: целое — как есть; с шагом датчика — кратно шагу; иначе
    1 знак до 100 и 0 знаков дальше (ряды и так огрублены записью по изменению)."""
    if v is None:
        return "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if step:
        f = round(f / step) * step
    if abs(f - round(f)) < 1e-9:
        return str(int(round(f)))
    return f"{f:.1f}" if abs(f) < 100 else str(int(round(f)))


# ── Запуск вызова: конверт, срок, усечение, журнал ───────────────────────────

def ctx_task_and_source(ctx: Any) -> tuple[str | None, str | None]:
    """Сквозной task_id (из _meta вызова или заголовка) и источник (заголовок)."""
    st = settings()
    task: str | None = None
    source: str | None = None
    try:
        meta = getattr(ctx, "meta", None)
        if meta is None:
            rc = getattr(ctx, "request_context", None)
            meta = getattr(rc, "meta", None) if rc is not None else None
        if meta is not None:
            for k in ("task_id", "taskId", "task"):
                v = meta.get(k) if hasattr(meta, "get") else getattr(meta, k, None)
                if v:
                    task = str(v)
                    break
            v = meta.get("source") if hasattr(meta, "get") else getattr(meta, "source", None)
            if v:
                source = str(v)
    except Exception:
        pass
    try:
        headers = getattr(ctx, "headers", None) or {}
        hl = {str(k).lower(): v for k, v in dict(headers).items()}
        task = task or hl.get(st.task_header.lower())
        source = source or hl.get(st.source_header.lower())
    except Exception:
        pass
    return task, source


async def run_tool(
    tool: str, ctx: Any, args: dict[str, Any],
    body: Callable[[Envelope], Awaitable[str]],
    *, unit: UnitRef | None = None,
) -> str:
    """Выполнить тело инструмента под сроком с конвертом, усечением по размеру
    и записью в журнал. Любая ошибка — текст «ОШИБКА: …» (модель видит причину
    и может поправить аргументы), в журнале status=error."""
    st = settings()
    task_id, source = ctx_task_and_source(ctx)
    if not st.allowed(source, tool):
        return f"ОШИБКА: инструмент {tool} не разрешён источнику «{source or 'default'}»."
    env = Envelope(label="", tool=tool, unit=unit.title if unit else "—",
                   unit_id=unit.id if unit else None)
    t0 = time.monotonic()
    status, error, text = "ok", None, ""
    try:
        timeout = float(st.limit("call_timeout_sec") or 20)
        try:
            out = await asyncio.wait_for(body(env), timeout=timeout)
        except asyncio.TimeoutError:
            status = "timeout"
            env.truncation.append(f"не успел за {timeout:g} с — сузьте окно или число параметров")
            out = ""
        out, cut = _cap_result(out, int(st.limit("result_max_tokens") or 8000))
        if cut:
            env.truncation.append(cut)
        # Метка [Tn] — только у состоявшегося ответа: ошибка номер не съедает (приёмка Э1)
        env.label = next_label(task_id)
        text = env.text() + ("\n\n" + out if out else "")
    except ToolError as e:
        status, error = "error", str(e)
        text = f"ОШИБКА: {e}"
    except Exception as e:  # noqa: BLE001 — модели нужен текст, хосту — журнал
        status, error = "error", f"{type(e).__name__}: {e}"
        logger.warning("telemetry_mcp: %s упал", tool, exc_info=True)
        text = f"ОШИБКА: внутренний сбой инструмента {tool}: {type(e).__name__}: {e}"
    dur_ms = int((time.monotonic() - t0) * 1000)
    if st.journal:
        try:
            from .journal import log_call
            await log_call(
                tool=tool, task_id=task_id, source=source, unit_id=env.unit_id,
                args=_safe_args(args), window_from=env.window_from, window_to=env.window_to,
                rows=env.rows, nbytes=len(text.encode("utf-8")),
                truncated="; ".join(env.truncation) or None,
                dropped={**{k: v for k, v in env.dropped.items() if v},
                         **({"gaps": len(env.gaps)} if env.gaps else {})} or None,
                duration_ms=dur_ms, status=status, error=error,
            )
        except Exception:
            logger.debug("журнал вызова не записан", exc_info=True)
    return text


def _safe_args(args: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for k, v in (args or {}).items():
        if v is None or k.startswith("_"):
            continue
        out[k] = v if isinstance(v, (int, float, bool, str)) else json.loads(json.dumps(v, default=str))
    return out


def _cap_result(text: str, max_tokens: int) -> tuple[str, str | None]:
    if not text:
        return text, None
    n = est_tokens(text)
    if n <= max_tokens:
        return text, None
    keep = int(len(text) * max_tokens / n)
    cut = text[:keep]
    nl = cut.rfind("\n")
    if nl > keep * 0.6:
        cut = cut[:nl]
    return cut + "\n… [обрезано]", f"результат обрезан до ~{max_tokens} ток. (было ~{n}) — сузьте окно или набор"
