# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Учёт обращений к моделям — таблица `llm_calls` (v5.2.6, MCP Э2б шаг 1).

Решения владельца (docs/plans/design_mcp_e2.md): Р5 — считать токены; Р25 — пишут
ВСЕ обращения к моделям (гейт, заключения, пересказ, разбор аварии, очистка языка,
ручные запросы), не только цикл с инструментами; Р26 — одна таблица на этот план и
на план цепочки (plan_chain_reports_v5.md: «Таблица llm_calls»); Р22 — стоимость по
ценам записи реестра, фиксируется в момент вызова.

Одна строка — один вызов модели. Расход нормализуется по провайдерам к четырём
числам: вход без кэша, выход, чтение кэша, запись кэша (+ рассуждения как доля
выхода). Чего провайдер не отдал — NULL, не 0.

Запись делается ПОСЛЕ ответа и ответ не ломает: любая ошибка журнала — одно
предупреждение в журнал процесса; вставка — со сроком INSERT_TIMEOUT_SEC.

Кто и что разбирает, вызывающий сообщает через call_context(...) — контекст
наследуется вложенными вызовами (разбор аварии → очистка языка) и задачами asyncio.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Awaitable, Callable, Iterator

logger = logging.getLogger(__name__)

# Срок вставки строки журнала: БД занята — вызов модели не ждёт дольше
INSERT_TIMEOUT_SEC = 3.0

_ctx: ContextVar[dict | None] = ContextVar("llm_call_ctx", default=None)
# Подмена приёмника для тестов: async (row) -> id; в работе — None (БД аналитики)
_sink: Callable[[dict], Awaitable[int | None]] | None = None
_warned = False
# Журнал в БД пишет только работающий сервис (main.py включает при старте): тесты и
# скрипты вне сервиса в боевую таблицу не пишут — им приёмник задаётся set_sink()
_enabled = False

# Вид разбора (строка «Кто и как разбирает») → вид задачи журнала
_TASK_BY_DEPTH = {"incident": "incident", "humanize": "humanize", "manual": "manual", "question": "question"}

COLUMNS = (
    "task_kind", "depth", "task_ref", "run_id", "iteration", "entry_id", "provider", "model", "backend",
    "reasoning", "priority", "prompt_version", "system_chars", "input_chars", "input_parts", "output_chars",
    "tokens_in", "tokens_out", "tokens_cache_read", "tokens_cache_write", "tokens_reasoning",
    "tool_calls", "tool_result_chars", "duration_ms", "status", "error", "gateway_task_id",
    "gateway_job_id", "verdict", "cost_usd",
)


def enable(on: bool = True) -> None:
    """Включить запись в БД аналитики (делает сервис при старте)."""
    global _enabled
    _enabled = bool(on)


def set_sink(fn: Callable[[dict], Awaitable[int | None]] | None) -> None:
    """Приёмник строк вместо БД (тесты, прогоны вне сервиса); None — БД аналитики."""
    global _sink
    _sink = fn


# ── Контекст вызова ──────────────────────────────────────────────────────────

@contextmanager
def call_context(**fields: Any) -> Iterator[dict]:
    """Сообщить журналу, что за вызовы идут внутри блока. Поля: task (gate | seg |
    humanize | incident | question | lang_check | manual | playground), ref (сегмент,
    акт, вопрос), depth, prompt_version, run_id, iteration, input_parts. Вложенный блок
    дополняет и перекрывает внешний. Возвращает словарь контекста: после вызова в нём
    `last_call_id` — для annotate()."""
    cur = dict(_ctx.get() or {})
    cur.pop("last_call_id", None)
    cur.update({k: v for k, v in fields.items() if v is not None})
    token = _ctx.set(cur)
    try:
        yield cur
    finally:
        _ctx.reset(token)


def current() -> dict:
    return dict(_ctx.get() or {})


def task_for(depth: str | None) -> str | None:
    """Вид задачи журнала по виду разбора: seg_* → seg, gate_* → gate, …"""
    if not depth:
        return None
    if depth.startswith("seg_"):
        return "seg"
    if depth.startswith("gate_"):
        return "gate"
    return _TASK_BY_DEPTH.get(depth)


# ── Нормализация расхода и стоимость ─────────────────────────────────────────

def _g(obj: Any, key: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _int(v: Any) -> int | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def normalize_usage(provider: str | None, usage: Any) -> dict[str, int | None]:
    """Расход ответа провайдера → {tokens_in (вход БЕЗ кэша), tokens_out,
    tokens_cache_read, tokens_cache_write, tokens_reasoning}. usage — словарь
    (OpenAI-формат, шлюз, Ollama) или объект (Anthropic SDK)."""
    out: dict[str, int | None] = {"tokens_in": None, "tokens_out": None, "tokens_cache_read": None,
                                  "tokens_cache_write": None, "tokens_reasoning": None}
    if usage is None:
        return out
    if provider == "anthropic" or _g(usage, "input_tokens") is not None:
        out["tokens_in"] = _int(_g(usage, "input_tokens"))
        out["tokens_out"] = _int(_g(usage, "output_tokens"))
        out["tokens_cache_read"] = _int(_g(usage, "cache_read_input_tokens"))
        out["tokens_cache_write"] = _int(_g(usage, "cache_creation_input_tokens"))
        return out
    prompt = _int(_g(usage, "prompt_tokens"))
    out["tokens_out"] = _int(_g(usage, "completion_tokens"))
    # DeepSeek: попадание и промах кэша отдельными полями
    hit, miss = _int(_g(usage, "prompt_cache_hit_tokens")), _int(_g(usage, "prompt_cache_miss_tokens"))
    cached = hit if hit is not None else _int(_g(_g(usage, "prompt_tokens_details"), "cached_tokens"))
    if miss is not None:
        out["tokens_in"] = miss
    elif prompt is not None and cached is not None:
        out["tokens_in"] = max(0, prompt - cached)
    else:
        out["tokens_in"] = prompt
    out["tokens_cache_read"] = cached
    out["tokens_reasoning"] = _int(_g(_g(usage, "completion_tokens_details"), "reasoning_tokens"))
    if out["tokens_reasoning"] is None:
        out["tokens_reasoning"] = _int(_g(usage, "reasoning_tokens"))
    return out


def _price(entry: dict | None, key: str) -> float | None:
    if not entry:
        return None
    v = entry.get(key)
    if v in (None, ""):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f >= 0 else None


def cost_usd(entry: dict | None, u: dict[str, int | None]) -> float | None:
    """Стоимость вызова по ценам записи реестра (USD за 1 млн токенов; Р22). Цен нет
    (шлюз, локальные, не заполнено) — None: стоимость не показывается. Цена кэша не
    задана — токены кэша считаются по цене входа (оценка сверху)."""
    p_in, p_out = _price(entry, "price_in"), _price(entry, "price_out")
    if p_in is None and p_out is None:
        return None
    p_in, p_out = p_in or 0.0, p_out or 0.0
    p_cr = _price(entry, "price_cache_read")
    p_cw = _price(entry, "price_cache_write")
    total = ((u.get("tokens_in") or 0) * p_in + (u.get("tokens_out") or 0) * p_out
             + (u.get("tokens_cache_read") or 0) * (p_cr if p_cr is not None else p_in)
             + (u.get("tokens_cache_write") or 0) * (p_cw if p_cw is not None else p_in))
    return round(total / 1_000_000.0, 6)


# ── Запись ───────────────────────────────────────────────────────────────────

async def _insert(row: dict) -> int | None:
    from db.pool import acquire_analytics
    cols = [c for c in COLUMNS if row.get(c) is not None]
    if not cols:
        return None
    vals = [json.dumps(row[c], ensure_ascii=False, default=str) if c == "input_parts" else row[c] for c in cols]
    ph = ", ".join(f"${i + 1}::jsonb" if c == "input_parts" else f"${i + 1}" for i, c in enumerate(cols))
    conn = await acquire_analytics()
    try:
        return await conn.fetchval(
            f"INSERT INTO llm_calls ({', '.join(cols)}) VALUES ({ph}) RETURNING id", *vals)
    finally:
        await conn.close()


async def record(
    *, provider: str | None, model: str | None, entry: dict | None = None,
    task: str | None = None, depth: str | None = None, backend: str | None = None,
    reasoning: str | None = None, priority: str | None = None,
    system_chars: int | None = None, input_chars: int | None = None, output_chars: int | None = None,
    usage: Any = None, duration_ms: int | None = None, status: str = "ok", error: str | None = None,
    gateway_task_id: str | None = None, gateway_job_id: str | None = None,
    tool_calls: int | None = None, tool_result_chars: int | None = None,
) -> int | None:
    """Записать один вызов модели. Никогда не бросает (кроме отмены задачи):
    возвращает id строки или None. Поля задачи — из call_context."""
    global _warned
    try:
        ctx = _ctx.get() or {}
        depth = depth or ctx.get("depth")
        u = normalize_usage(provider, usage)
        row: dict[str, Any] = {
            "task_kind": task or ctx.get("task") or task_for(depth) or "other",
            "depth": depth,
            "task_ref": (str(ctx["ref"]) if ctx.get("ref") is not None else None),
            "run_id": ctx.get("run_id"), "iteration": ctx.get("iteration"),
            "entry_id": (entry or {}).get("id"),
            "provider": provider, "model": model, "backend": backend,
            "reasoning": reasoning, "priority": priority,
            "prompt_version": ctx.get("prompt_version"),
            "system_chars": system_chars, "input_chars": input_chars,
            "input_parts": ctx.get("input_parts"), "output_chars": output_chars,
            **u,
            "tool_calls": tool_calls, "tool_result_chars": tool_result_chars,
            "duration_ms": duration_ms, "status": status,
            "error": (str(error)[:1000] if error else None),
            "gateway_task_id": gateway_task_id, "gateway_job_id": gateway_job_id,
            "cost_usd": cost_usd(entry, u),
        }
        if _sink is not None:
            rid = await _sink(row)
        elif _enabled:
            rid = await asyncio.wait_for(_insert(row), INSERT_TIMEOUT_SEC)
        else:
            rid = None
        cur = _ctx.get()
        if isinstance(cur, dict):
            cur["last_call_id"] = rid
            cur["last_cost_usd"] = row["cost_usd"]
        return rid
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — журнал не ломает вызов модели
        if not _warned:
            _warned = True
            logger.warning("Журнал llm_calls недоступен — вызовы моделей не записываются "
                           "(таблицу создаёт cg-analytics при старте)", exc_info=True)
        return None


async def annotate(ctx_or_id: Any, **fields: Any) -> None:
    """Дописать в строку вызова то, что известно после разбора ответа: verdict
    (вердикт гейта), status, error. ctx_or_id — словарь из call_context или id."""
    rid = ctx_or_id.get("last_call_id") if isinstance(ctx_or_id, dict) else ctx_or_id
    allowed = {k: v for k, v in fields.items() if k in ("verdict", "status", "error") and v is not None}
    if rid is None or not allowed:
        return
    try:
        if _sink is not None:
            await _sink({"_annotate": rid, **allowed})
            return
        if not _enabled:
            return
        from db.pool import acquire_analytics

        async def _upd() -> None:
            conn = await acquire_analytics()
            try:
                sets = ", ".join(f"{k} = ${i + 2}" for i, k in enumerate(allowed))
                await conn.execute(f"UPDATE llm_calls SET {sets} WHERE id = $1", int(rid),
                                   *[str(v)[:1000] for v in allowed.values()])
            finally:
                await conn.close()
        await asyncio.wait_for(_upd(), INSERT_TIMEOUT_SEC)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.debug("llm_calls: пометка строки %s не записана", rid, exc_info=True)


# ── Claude API: обёртка вызова ───────────────────────────────────────────────

def anthropic_entry() -> dict | None:
    """Запись реестра для Claude (цены): первая с provider anthropic; нет — None."""
    try:
        from llm.registry import get_entries
        for e in get_entries():
            if e.get("provider") == "anthropic":
                return e
    except Exception:  # noqa: BLE001
        pass
    return None


def _text_len(content: Any) -> int:
    if isinstance(content, str):
        return len(content)
    n = 0
    if isinstance(content, (list, tuple)):
        for b in content:
            if isinstance(b, dict):
                n += len(str(b.get("text") or "")) + _text_len(b.get("content"))
            else:
                n += len(str(getattr(b, "text", "") or ""))
    return n


async def anthropic_create(client: Any, *, depth: str | None = None, task: str | None = None,
                           entry: dict | None = None, **kwargs: Any) -> Any:
    """`client.messages.create(**kwargs)` с записью в журнал (успех и ошибка).
    depth/task — если не заданы контекстом. Поведение вызова не меняется."""
    t0 = time.monotonic()
    system = kwargs.get("system")
    sys_chars = _text_len(system) if system is not None else None
    in_chars = (sys_chars or 0) + sum(_text_len(m.get("content")) for m in kwargs.get("messages") or []
                                      if isinstance(m, dict))
    ent = entry if entry is not None else anthropic_entry()
    try:
        resp = await client.messages.create(**kwargs)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        await record(provider="anthropic", model=kwargs.get("model"), entry=ent, task=task, depth=depth,
                     system_chars=sys_chars, input_chars=in_chars,
                     duration_ms=int((time.monotonic() - t0) * 1000), status="error", error=repr(exc)[:300])
        raise
    content = getattr(resp, "content", None) or []
    out_chars = sum(len(str(getattr(b, "text", "") or "")) for b in content)
    n_tools = sum(1 for b in content if getattr(b, "type", "") == "tool_use")
    await record(provider="anthropic", model=getattr(resp, "model", None) or kwargs.get("model"), entry=ent,
                 task=task, depth=depth, system_chars=sys_chars, input_chars=in_chars, output_chars=out_chars,
                 usage=getattr(resp, "usage", None), duration_ms=int((time.monotonic() - t0) * 1000),
                 status="ok" if (out_chars or n_tools) else "empty", tool_calls=n_tools or None)
    return resp
