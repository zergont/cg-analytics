# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Хост цикла «вопрос → вызов инструмента → ответ» (MCP Э2б;
docs/plans/design_mcp_e2.md, разд. 2 и 4.1).

Один цикл на всех провайдеров: разговор с моделью ведёт адаптер (Claude —
llm/adapters/claude.py, OpenAI-формат — llm/adapters/openai_chat.py), инструменты
исполняет сервер телеметрии через llm/mcp_client.py. Цикл отвечает за то, что от
провайдера не зависит:

- бюджеты прогона (Р12, Р23): вызовы инструментов, объём контекста, минуты. По
  исчерпании модель получает пометку «отвечай по собранному»; список инструментов
  в запросе при этом НЕ меняется (у Claude правка списка обнуляет кэш и ломает
  блоки рассуждений прошлых шагов) — запрошенные после предела вызовы получают
  отказ результатом;
- дедуп: повтор вызова с теми же аргументами к серверу не уходит, модель получает
  ссылку на метку прежнего результата (сам результат уже есть в разговоре);
- блок «## Данные»: собирается из первых строк результатов (конверт сервера:
  метка [Tn], установка, инструмент, окно), а не моделью;
- учёт: каждая итерация — строка llm_calls с номером прогона и итерации.

Fail-open: нет сервера телеметрии — прогон идёт без инструментов, с пометкой.
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from llm.mcp_client import NETWORK_ERROR_TEXT, McpUnavailable, ToolResult, ToolSpec

logger = logging.getLogger(__name__)

# Умолчания бюджета (облако) — на случай вызова без строки таблицы
DEFAULT_BUDGET = {"calls": 12, "tokens": 200_000, "minutes": 10}
# После предела модели даётся время на итоговый ответ
FINAL_GRACE_SEC = 180.0
# Сколько раз подряд модель может запросить инструмент после предела
MAX_FINAL_NUDGES = 2
# Столько сбоев сети подряд — инструменты до конца прогона не исполняются
NET_FAILS_TO_STOP = 3
# Оценка объёма ещё не отправленных результатов (точный объём даёт расход ответа)
EST_CHARS_PER_TOKEN = 3
EST_IMAGE_TOKENS = 1500
# Инструменты только для моделей со зрением (Р21)
VISION_ONLY_TOOLS = frozenset({"chart"})

WHY_CALLS = "вызовы инструментов"
WHY_TOKENS = "объём контекста"
WHY_MINUTES = "время"
WHY_SERVER = "сервер телеметрии недоступен"
_STOP_CODE = {WHY_CALLS: "limit_calls", WHY_TOKENS: "limit_tokens",
              WHY_MINUTES: "limit_minutes", WHY_SERVER: "no_server"}

LIMIT_NOTE = ("Предел разбора достигнут ({why}). Инструменты больше не вызывай: напиши итоговый "
              "ответ по уже полученным данным; чего не хватило — назови прямо.")
DUP_TEXT = ("Повтор: этот вызов с теми же аргументами уже выполнен в этом разборе — результат "
            "выше{label}. Повторно не выполняется.")
REFUSED_TEXT = "Инструмент не вызван: {why}."

TOKEN_KEYS = ("tokens_in", "tokens_out", "tokens_cache_read", "tokens_cache_write", "tokens_reasoning")


class LoopError(Exception):
    """Прогон не дал ответа (модель отказалась, не ответила в срок, пустой итог)."""


@dataclass
class ToolCall:
    """Вызов инструмента, запрошенный моделью. bad_args — аргументы не разобраны."""
    id: str
    name: str
    args: dict[str, Any]
    bad_args: str | None = None


@dataclass
class Turn:
    """Один ответ модели: текст и/или вызовы инструментов. usage — нормализованный
    расход (llm.usage.normalize_usage). stop: end | tools | length."""
    text: str
    calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, int | None] = field(default_factory=dict)
    stop: str = "end"
    model: str | None = None


class Adapter(Protocol):
    """Разговор с моделью одного провайдера. Разговор только дополняется."""
    provider: str
    model: str
    vision: bool

    def start(self, system: str, user: str, tools: list[ToolSpec]) -> None: ...

    async def step(self, *, final: bool, timeout: float) -> Turn: ...

    def add_results(self, results: list[tuple[ToolCall, ToolResult]], note: str | None) -> None: ...

    def add_note(self, note: str) -> None: ...

    async def aclose(self) -> None: ...


@dataclass
class RunResult:
    text: str                      # ответ + блок «## Данные»
    answer: str                    # ответ модели без блока
    data_block: str
    stop_reason: str               # done | limit_calls | limit_tokens | limit_minutes | no_server
    stop_why: str | None
    iterations: int
    calls: list[dict[str, Any]]    # журнал вызовов инструментов прогона
    tools_offered: list[str]
    tools_note: str | None         # почему инструментов нет, хотя строка их включила
    usage: dict[str, int | None]   # суммы по итерациям
    context_tokens: int | None     # объём контекста прогона (Р23)
    cost_usd: float | None
    priced_steps: int
    duration_sec: float
    run_id: str
    provider: str
    model: str | None

    @property
    def tool_calls(self) -> int:
        """Сколько вызовов дошло до сервера телеметрии."""
        return sum(1 for c in self.calls if c["kind"] == "call")

    def summary(self) -> dict[str, Any]:
        """Сводка прогона для записи разбора (extra) и debug."""
        return {
            "run_id": self.run_id, "provider": self.provider, "model": self.model,
            "iterations": self.iterations, "tool_calls": self.tool_calls,
            "dedup": sum(1 for c in self.calls if c["kind"] == "dedup"),
            "tokens": dict(self.usage), "context_tokens": self.context_tokens,
            "cost_usd": self.cost_usd, "stop_reason": self.stop_reason,
            "tools": self.tools_offered, "tools_note": self.tools_note,
            "duration_sec": round(self.duration_sec, 1),
        }

    def signature_tail(self) -> str:
        """Хвост подписи модели: «инструментов 7 · контекст 48 тыс. ток. · $0,31»."""
        parts: list[str] = []
        if self.tools_note:
            parts.append(self.tools_note)
        elif self.tools_offered:
            parts.append(f"инструментов {self.tool_calls}")
        if self.context_tokens:
            parts.append(f"контекст {fmt_thousands(self.context_tokens)} ток.")
        if self.cost_usd is not None and self.priced_steps == self.iterations:
            parts.append(fmt_cost(self.cost_usd))
        if self.stop_why:
            parts.append(f"остановлен по пределу: {self.stop_why}")
        return " · ".join(parts)


def fmt_thousands(n: int) -> str:
    return f"{max(1, round(n / 1000))} тыс." if n >= 1000 else str(int(n))


def fmt_cost(v: float) -> str:
    return "$" + (f"{v:.2f}" if v >= 0.1 else f"{v:.3f}").replace(".", ",")


# ── Конверт сервера и блок «## Данные» ───────────────────────────────────────

_LABEL_RE = re.compile(r"^\[(T\d+)\]\s*(.*)$")
_DATA_HEAD_RE = re.compile(r"\n#{1,4}[ \t]*Данные[ \t]*:?[ \t]*\n.*\Z", re.S)


def envelope_head(text: str) -> tuple[str | None, str]:
    """(метка, «установка · инструмент · окно») из первой строки результата."""
    first = (text or "").split("\n", 1)[0].strip()
    m = _LABEL_RE.match(first)
    if not m:
        return None, ""
    rest = m.group(2)
    cut = rest.find(" · слой:")
    return m.group(1), (rest[:cut] if cut >= 0 else rest).strip()


def truncation_note(text: str, limit: int = 90) -> str | None:
    """Что сервер сделал с объёмом ответа (строка конверта «усечение: …»): усёк
    строки или укрупнил шаг. None — ответ полный."""
    for line in (text or "").splitlines()[:8]:
        if line.startswith("усечение:"):
            note = line[len("усечение:"):].strip()
            return (note[:limit].rstrip() + "…") if len(note) > limit else (note or "усечено")
    return None


def data_block(calls: list[dict[str, Any]]) -> str:
    """Строки блока «## Данные»: по одной на состоявшийся вызов с меткой."""
    lines = []
    for c in calls:
        if c["kind"] != "call" or c.get("is_error") or not c.get("label"):
            continue
        line = f"- [{c['label']}] {c.get('head') or c['name']}"
        marks = [m for m in (c.get("truncated"), "с графиком" if c.get("images") else None) if m]
        lines.append(line + (f" — {', '.join(marks)}" if marks else ""))
    return "\n".join(lines)


def strip_data_block(text: str) -> str:
    """Убрать блок «## Данные», написанный самой моделью, — его ставит хост."""
    return _DATA_HEAD_RE.sub("", "\n" + (text or "")).strip()


def call_key(name: str, args: dict[str, Any]) -> str:
    """Ключ дедупа: инструмент и аргументы в устойчивом виде (порядок ключей и
    пробелы по краям строк не важны; пустые значения не считаются)."""
    def norm(v: Any) -> Any:
        if isinstance(v, str):
            return " ".join(v.split())
        if isinstance(v, dict):
            return {k: norm(x) for k, x in sorted(v.items()) if x not in (None, "", [], {})}
        if isinstance(v, (list, tuple)):
            return [norm(x) for x in v]
        return v
    return name + " " + json.dumps(norm(args or {}), ensure_ascii=False, sort_keys=True)


def _context_tokens(u: dict[str, int | None]) -> int | None:
    vals = [u.get(k) for k in ("tokens_in", "tokens_cache_read", "tokens_cache_write", "tokens_out")]
    return sum(v for v in vals if v) if any(v is not None for v in vals) else None


# ── Прогон ───────────────────────────────────────────────────────────────────

def new_run_id() -> str:
    return "run-" + uuid.uuid4().hex[:12]


async def offered_tools(session: Any, *, vision: bool) -> list[ToolSpec]:
    """Инструменты сервера для модели: без графика, если модель не видит картинок."""
    tools = await session.list_tools()
    return [t for t in tools if vision or t.name not in VISION_ONLY_TOOLS]


async def run(*, adapter: Adapter, system: str, user: str, session: Any = None,
              tools: list[ToolSpec] | None = None, budget: dict[str, Any] | None = None,
              run_id: str | None = None, tools_note: str | None = None) -> RunResult:
    """Один прогон. session — открытое подключение к серверу телеметрии (None — без
    инструментов); tools — уже полученный список (иначе берётся у session);
    tools_note — пометка в подпись, почему инструментов нет. Адаптер закрывается
    вызывающим."""
    from llm import usage as _usage

    run_id = run_id or new_run_id()
    b = {**DEFAULT_BUDGET, **{k: v for k, v in (budget or {}).items() if v}}
    t0 = time.monotonic()
    deadline = t0 + float(b["minutes"]) * 60.0

    if session is not None and tools is None:
        try:
            tools = await offered_tools(session, vision=bool(adapter.vision))
        except McpUnavailable as exc:
            logger.warning("Цикл %s: сервер телеметрии недоступен (%s) — прогон без инструментов", run_id, exc)
            tools, session, tools_note = [], None, "инструменты недоступны"
    tools = list(tools or []) if session is not None else []
    offered = {t.name for t in tools}
    adapter.start(system, user, tools)

    seen: dict[str, str] = {}
    log: list[dict[str, Any]] = []
    totals: dict[str, int | None] = dict.fromkeys(TOKEN_KEYS)
    cost_total, priced_steps = 0.0, 0
    ctx_tokens: int | None = None
    final_why: str | None = None
    executed = nudges = net_fails = it = 0
    pending_chars: int | None = None
    answer, model = "", None

    while True:
        it += 1
        now = time.monotonic()
        if tools and final_why is None and now >= deadline:
            final_why = WHY_MINUTES
            adapter.add_note(LIMIT_NOTE.format(why=final_why))
        timeout = FINAL_GRACE_SEC if final_why is not None else max(60.0, deadline - now) + FINAL_GRACE_SEC
        with _usage.call_context(run_id=run_id, iteration=it, tool_result_chars=pending_chars) as cx:
            turn = await adapter.step(final=final_why is not None, timeout=timeout)
        pending_chars = None
        model = turn.model or model
        for k in TOKEN_KEYS:
            v = turn.usage.get(k)
            if v is not None:
                totals[k] = (totals[k] or 0) + int(v)
        step_cost = cx.get("last_cost_usd")
        if step_cost is not None:
            cost_total += float(step_cost)
            priced_steps += 1
        ctx_tokens = _context_tokens(turn.usage) or ctx_tokens

        if not turn.calls:
            answer = turn.text
            break

        if final_why is not None or turn.stop == "length":
            # Предел уже объявлен (или ответ обрезан на середине вызова): вызовы не исполняются
            nudges += 1
            if nudges > MAX_FINAL_NUDGES:
                answer = turn.text
                break
            why = f"предел разбора ({final_why})" if final_why is not None else \
                "ответ обрезан пределом длины, вызов не разобран — повтори короче"
            results = [(c, ToolResult(text=REFUSED_TEXT.format(why=why), is_error=True)) for c in turn.calls]
            for c, _ in results:
                log.append({"kind": "refused", "name": c.name, "args": c.args, "iteration": it})
            adapter.add_results(results, LIMIT_NOTE.format(why=final_why) if final_why is not None else None)
            pending_chars = sum(r.chars for _, r in results)
            continue

        results = []
        for c in turn.calls:
            rec: dict[str, Any] = {"name": c.name, "args": c.args, "iteration": it}
            key = call_key(c.name, c.args)
            if c.bad_args:
                res = ToolResult(text=f"ОШИБКА: аргументы вызова не разобраны ({c.bad_args})", is_error=True)
                rec["kind"] = "bad_args"
            elif c.name not in offered:
                res = ToolResult(text=f"ОШИБКА: инструмента «{c.name}» нет. Доступны: "
                                      f"{', '.join(sorted(offered))}", is_error=True)
                rec["kind"] = "unknown"
            elif key in seen:
                lbl = seen[key]
                res = ToolResult(text=DUP_TEXT.format(label=f", метка [{lbl}]" if lbl else ""))
                rec.update(kind="dedup", label=lbl)
            elif executed >= int(b["calls"]):
                res = ToolResult(text=REFUSED_TEXT.format(why=f"предел разбора ({WHY_CALLS})"), is_error=True)
                rec["kind"] = "refused"
            elif net_fails >= NET_FAILS_TO_STOP:
                res = ToolResult(text=NETWORK_ERROR_TEXT, is_error=True, network_error=True)
                rec["kind"] = "no_server"
            else:
                t1 = time.monotonic()
                res = await session.call(c.name, c.args)
                executed += 1
                net_fails = net_fails + 1 if res.network_error else 0
                if not adapter.vision:
                    res.images = []
                label, head = envelope_head(res.text)
                rec.update(kind="call", label=label, head=head, is_error=res.is_error,
                           chars=res.chars, images=len(res.images), truncated=truncation_note(res.text),
                           duration_ms=int((time.monotonic() - t1) * 1000))
                if not res.is_error:
                    seen[key] = label or ""
            log.append(rec)
            results.append((c, res))

        est = sum(r.chars for _, r in results) // EST_CHARS_PER_TOKEN \
            + EST_IMAGE_TOKENS * sum(len(r.images) for _, r in results)
        if net_fails >= NET_FAILS_TO_STOP:
            final_why = WHY_SERVER
        elif executed >= int(b["calls"]):
            final_why = WHY_CALLS
        elif ctx_tokens is not None and ctx_tokens + est >= int(b["tokens"]):
            final_why = WHY_TOKENS
        elif time.monotonic() >= deadline:
            final_why = WHY_MINUTES
        adapter.add_results(results, LIMIT_NOTE.format(why=final_why) if final_why is not None else None)
        pending_chars = sum(r.chars for _, r in results)

    answer = (answer or "").strip()
    if not answer:
        raise LoopError("модель не дала итогового ответа"
                        + (f" (предел: {final_why})" if final_why else ""))
    block = data_block(log)
    if block:
        answer = strip_data_block(answer)
    text = answer + (f"\n\n## Данные\n{block}" if block else "")
    result = RunResult(
        text=text, answer=answer, data_block=block,
        stop_reason=_STOP_CODE.get(final_why or "", "done"), stop_why=final_why,
        iterations=it, calls=log, tools_offered=sorted(offered), tools_note=tools_note,
        usage=totals, context_tokens=ctx_tokens,
        cost_usd=round(cost_total, 6) if priced_steps else None, priced_steps=priced_steps,
        duration_sec=time.monotonic() - t0, run_id=run_id,
        provider=adapter.provider, model=model or adapter.model,
    )
    logger.info("Цикл %s: %s/%s — итераций %d, вызовов инструментов %d (повторов %d), контекст %s ток., "
                "стоимость %s, остановка: %s, %.0f с", run_id, result.provider, result.model, it,
                result.tool_calls, sum(1 for c in log if c["kind"] == "dedup"),
                ctx_tokens if ctx_tokens is not None else "—",
                fmt_cost(result.cost_usd) if result.cost_usd is not None else "—",
                result.stop_reason, result.duration_sec)
    return result
