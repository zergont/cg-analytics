"""MCP Э2б, шаг 3: хост цикла с инструментами (docs/plans/design_mcp_e2.md, разд. 2–4, 7, 10).
Без БД и без облака: модели — подставные, сервер телеметрии — заглушка на случайном порту.

Раздел 1 — схемы инструментов для Claude и OpenAI-формата; ключ дедупа; конверт [Tn] и блок
«## Данные»; подпись с расходом.
Раздел 2 — цикл с подставным адаптером: вызовы → результаты → итог; дедуп; бюджеты (вызовы,
объём контекста, минуты) и пометка предела; отказ вызовам после предела; обрезанный ответ;
неизвестный инструмент и неразобранные аргументы; три сбоя сети подряд; модель без зрения;
пустой итог — ошибка; строки журнала с номером прогона, итерации и размером результатов.
Раздел 3 — MCP-клиент против сервера-заглушки: список, вызов с номером прогона, картинка,
ошибка инструмента, недоступный сервер, проба для настроек.
Раздел 4 — адаптер Claude: кэш промпта, неизменные инструменты и system, история только
дополняется (ответ целиком), картинка в результате инструмента, уровень рассуждения, отказ
модели, обрезка по длине.
Раздел 5 — адаптер OpenAI-формата: запрос (tools, tool_choice), роли сообщений, рассуждение
DeepSeek возвращается в историю, картинка следующим сообщением, повтор при 503, ошибка 400,
аргументы не JSON, поля шлюза.
Раздел 6 — правила промпта для инструментов и подключение цикла к разбору аварии: прежний
путь без подключения и инструментов, цикл с подключением, недоступный сервер (разбор без
инструментов с пометкой), подсказка имени установки, сводка прогона в записи разбора.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_errors: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        _errors.append(msg)
        print("  ✗", msg)


import logging

logging.disable(logging.CRITICAL)

import httpx

from llm import agent_loop, mcp_client, prompts, registry, router, usage
from llm import client as llm_client
from llm.adapters import make_adapter
from llm.adapters.claude import ClaudeAdapter
from llm.adapters.openai_chat import OpenAIChatAdapter
from llm.agent_loop import LoopError, ToolCall, Turn
from llm.mcp_client import McpUnavailable, TelemetrySession, ToolResult, ToolSpec

ROWS: list[dict] = []


async def sink(row: dict):
    ROWS.append(dict(row))
    return len(ROWS)


usage.set_sink(sink)


def run(coro):
    return asyncio.run(coro)


SPECS = [
    ToolSpec("chart", "График", {"type": "object", "properties": {"unit": {"type": "string"}}}),
    ToolSpec("series", "Ряд параметра", {"type": "object", "properties": {"unit": {"type": "string"},
                                                                         "params": {"type": "string"}},
                                         "required": ["unit", "params"]}),
    ToolSpec("timeline", "Лента событий", {"type": "object", "properties": {"unit": {"type": "string"}}}),
]

# ── 1. Схемы, дедуп, конверт ─────────────────────────────────────────────────
print("1. Схемы инструментов, ключ дедупа, конверт и блок «## Данные»")

a = mcp_client.to_anthropic(SPECS)
check(a[1] == {"name": "series", "description": "Ряд параметра", "input_schema": SPECS[1].input_schema},
      "схема для Claude — как у сервера")
o = mcp_client.to_openai(SPECS)
check(o[1] == {"type": "function", "function": {"name": "series", "description": "Ряд параметра",
                                                "parameters": SPECS[1].input_schema}},
      "схема для OpenAI-формата")

k1 = agent_loop.call_key("series", {"unit": "ДГУ №1", "params": "частота", "t_to": None})
k2 = agent_loop.call_key("series", {"params": " частота ", "unit": "ДГУ  №1", "t_to": ""})
check(k1 == k2, "дедуп: порядок ключей, пробелы и пустые значения не важны")
check(k1 != agent_loop.call_key("series", {"unit": "ДГУ №1", "params": "обороты"}), "дедуп: другие аргументы — другой ключ")
check(k1 != agent_loop.call_key("trend", {"unit": "ДГУ №1", "params": "частота"}), "дедуп: другой инструмент — другой ключ")

ENV = ("[T3] ДГУ №1 · series · 16.10.2026 09:20–09:35 МСК · слой: история\n"
       "единицы: частота Гц\nусечение: показано 200 из 950 строк\n\nвремя | значение")
label, head = agent_loop.envelope_head(ENV)
check(label == "T3" and head == "ДГУ №1 · series · 16.10.2026 09:20–09:35 МСК", f"конверт: {label!r}, {head!r}")
check(agent_loop.envelope_head("ОШИБКА: установка не найдена") == (None, ""), "ошибка — без метки")
check(agent_loop.truncation_note(ENV) == "показано 200 из 950 строк"
      and agent_loop.truncation_note("[T1] x · y\nочистка: нет") is None, "пометка усечения — словами сервера")
check(agent_loop.truncation_note("[T1] x\nусечение: " + "а" * 200).endswith("…"), "длинная пометка обрезается")

block = agent_loop.data_block([
    {"kind": "call", "name": "series", "label": "T3", "head": head, "truncated": "укрупнено до шага 10 с", "images": 0},
    {"kind": "call", "name": "chart", "label": "T4", "head": "ДГУ №1 · chart · окно", "images": 1},
    {"kind": "dedup", "name": "series", "label": "T3"},
    {"kind": "call", "name": "timeline", "label": None, "is_error": True},
    {"kind": "refused", "name": "trend"},
])
check(block == "- [T3] ДГУ №1 · series · 16.10.2026 09:20–09:35 МСК — укрупнено до шага 10 с\n"
               "- [T4] ДГУ №1 · chart · окно — с графиком", f"блок «Данные»: {block!r}")
check(agent_loop.strip_data_block("Вывод [T1].\n\n## Данные\n- [T1] что-то\n- [T2] ещё") == "Вывод [T1].",
      "блок «Данные» модели снимается")
check(agent_loop.strip_data_block("## Вывод\nДанные верны.\n\n### Причина\nтекст") == "## Вывод\nДанные верны.\n\n### Причина\nтекст",
      "текст без блока не трогается")
check(agent_loop.fmt_cost(0.314) == "$0,31" and agent_loop.fmt_cost(0.0123) == "$0,012", "стоимость в подписи")
check(agent_loop.fmt_thousands(48_400) == "48 тыс." and agent_loop.fmt_thousands(640) == "640", "тысячи токенов")


# ── 2. Цикл с подставным адаптером ───────────────────────────────────────────
print("2. Цикл: вызовы, дедуп, бюджеты, отказы, сбои сети")


class FakeAdapter:
    provider = "fake"
    model = "fake-1"

    def __init__(self, script, vision=True, ctx_step=1000):
        self.script = list(script)      # ход: текст | список вызовов | функция(адаптер) → Turn
        self.vision = vision
        self.ctx = 0
        self.ctx_step = ctx_step
        self.steps: list[dict] = []
        self.results: list[list[tuple[ToolCall, ToolResult]]] = []
        self.notes: list[str] = []
        self.tools: list[ToolSpec] = []
        self.started: tuple[str, str] | None = None
        self.closed = False

    def start(self, system, user, tools):
        self.started, self.tools = (system, user), list(tools)

    async def step(self, *, final, timeout):
        self.steps.append({"final": final, "timeout": timeout, "ctx": usage.current()})
        self.ctx += self.ctx_step
        item = self.script.pop(0) if self.script else "итог по умолчанию"
        u = {"tokens_in": self.ctx, "tokens_out": 100, "tokens_cache_read": None,
             "tokens_cache_write": None, "tokens_reasoning": None}
        await usage.record(provider="fake", model="fake-1", usage=None, status="ok")
        if callable(item):
            t = item(self)
            t.usage = t.usage or u
            return t
        if isinstance(item, str):
            return Turn(text=item, usage=u, model=self.model)
        calls = [ToolCall(id=f"c{len(self.steps)}_{i}", name=n, args=a) for i, (n, a) in enumerate(item)]
        return Turn(text="", calls=calls, usage=u, stop="tools", model=self.model)

    def add_results(self, results, note):
        self.results.append(list(results))
        if note:
            self.notes.append(note)

    def add_note(self, note):
        self.notes.append(note)

    async def aclose(self):
        self.closed = True


class FakeSession:
    def __init__(self, tools=None, fail=0, slow=0.0):
        self.tools = list(tools if tools is not None else SPECS)
        self.calls: list[tuple[str, dict]] = []
        self.fail = fail              # столько первых вызовов — сбой сети
        self.n = 0

    async def list_tools(self):
        return sorted(self.tools, key=lambda t: t.name)

    async def call(self, name, args):
        self.calls.append((name, dict(args)))
        if len(self.calls) <= self.fail:
            return ToolResult(text=mcp_client.NETWORK_ERROR_TEXT, is_error=True, network_error=True)
        if args.get("unit") == "нет такой":
            return ToolResult(text="ОШИБКА: установка «нет такой» не найдена", is_error=True)
        self.n += 1
        imgs = [("image/png", "QUJD")] if name == "chart" else []
        return ToolResult(text=f"[T{self.n}] {args.get('unit', '?')} · {name} · окно · слой: история\n"
                               f"строка данных {self.n}", images=imgs)


U = {"unit": "ДГУ №1"}

# 2.1. Обычный прогон
ROWS.clear()
ad = FakeAdapter([[("timeline", U)], [("series", {**U, "params": "частота"}), ("chart", U)],
                  "Причина — провал частоты [T2].\n\n## Данные\n- [T9] выдумано"])
ss = FakeSession()
r = run(agent_loop.run(adapter=ad, system="СИС", user="бриф", session=ss,
                       budget={"calls": 12, "tokens": 200_000, "minutes": 10}, run_id="run-test1"))
check(ad.started == ("СИС", "бриф") and [t.name for t in ad.tools] == ["chart", "series", "timeline"],
      "адаптер получил system, бриф и инструменты по алфавиту")
check(r.iterations == 3 and r.tool_calls == 3 and r.stop_reason == "done", f"три шага, три вызова: {r.summary()}")
check(r.answer == "Причина — провал частоты [T2]." and "[T9]" not in r.text, "блок «Данные» модели заменён блоком хоста")
check(r.text.endswith("## Данные\n- [T1] ДГУ №1 · timeline · окно\n- [T2] ДГУ №1 · series · окно\n"
                      "- [T3] ДГУ №1 · chart · окно — с графиком"), f"блок хоста: {r.text!r}")
check(len(ad.results) == 2 and len(ad.results[1]) == 2, "результаты шага возвращаются одной пачкой")
check(ad.notes == [], "предела нет — пометок нет")
check(r.context_tokens == 3100 and r.usage["tokens_in"] == 6000 and r.usage["tokens_out"] == 300,
      f"объём контекста — последний запрос, суммы — по итерациям: {r.context_tokens}, {r.usage}")
check([s["ctx"].get("iteration") for s in ad.steps] == [1, 2, 3]
      and all(s["ctx"].get("run_id") == "run-test1" for s in ad.steps), "номер прогона и итерации в контексте журнала")
check([row.get("tool_result_chars") for row in ROWS] == [None, len(ss_text := "[T1] ДГУ №1 · timeline · окно · слой: история\nстрока данных 1"),
                                                         ROWS[2].get("tool_result_chars")]
      and ROWS[2]["tool_result_chars"] > ROWS[1]["tool_result_chars"],
      f"размер результатов инструментов — в строке следующего шага: {[row.get('tool_result_chars') for row in ROWS]}")
check(all(row["run_id"] == "run-test1" for row in ROWS) and [row["iteration"] for row in ROWS] == [1, 2, 3],
      "строки журнала: прогон и итерация")
check(r.signature_tail() == "инструментов 3 · контекст 3 тыс. ток.", f"подпись: {r.signature_tail()!r}")
check(r.cost_usd is None, "без цен стоимости нет")

# 2.2. Дедуп
ad = FakeAdapter([[("timeline", U)], [("timeline", {"unit": " ДГУ №1 "}), ("series", {**U, "params": "частота"})], "итог"])
ss = FakeSession()
r = run(agent_loop.run(adapter=ad, system="s", user="u", session=ss))
check(len(ss.calls) == 2 and r.tool_calls == 2, f"повтор к серверу не ушёл: {ss.calls}")
dup = ad.results[1][0][1]
check("уже выполнен" in dup.text and "[T1]" in dup.text and not dup.is_error, f"повтор — ссылка на метку: {dup.text!r}")
check(r.summary()["dedup"] == 1 and r.data_block.count("\n") == 1, "повтор в журнале прогона, в блоке «Данные» — один раз")

# 2.3. Ошибочный вызов не запоминается как выполненный
ad = FakeAdapter([[("timeline", {"unit": "нет такой"})], [("timeline", {"unit": "нет такой"})], "итог"])
ss = FakeSession()
r = run(agent_loop.run(adapter=ad, system="s", user="u", session=ss))
check(len(ss.calls) == 2 and r.data_block == "" and r.text == "итог", "ошибка не дедупится и в «Данные» не идёт")

# 2.4. Бюджет вызовов
ad = FakeAdapter([[("timeline", U), ("series", {**U, "params": "a"}), ("series", {**U, "params": "b"})],
                  [("series", {**U, "params": "c"})], "итог по собранному"])
ss = FakeSession()
r = run(agent_loop.run(adapter=ad, system="s", user="u", session=ss, budget={"calls": 2}))
check(len(ss.calls) == 2, f"к серверу ушло не больше бюджета: {len(ss.calls)}")
check(ad.results[0][2][1].is_error and "предел разбора" in ad.results[0][2][1].text, "третий вызов шага — отказ")
check(len(ad.notes) >= 1 and "вызовы инструментов" in ad.notes[0] and "итоговый" in ad.notes[0], f"пометка предела: {ad.notes}")
check(ad.steps[1]["final"] is True and ad.steps[0]["final"] is False, "после предела шаг помечен итоговым")
check(ad.results[1][0][1].is_error and len(ss.calls) == 2, "вызов после предела не исполняется")
check(r.stop_reason == "limit_calls" and r.text.startswith("итог по собранному"), f"причина остановки: {r.stop_reason}")
check("остановлен по пределу: вызовы инструментов" in r.signature_tail(), f"предел в подписи: {r.signature_tail()}")
check([t.name for t in ad.tools] == ["chart", "series", "timeline"], "список инструментов после предела не меняется")

# 2.5. Модель продолжает звать инструменты после предела — прогон не зацикливается
ad = FakeAdapter([[("timeline", U)]] + [[("series", {**U, "params": str(i)})] for i in range(10)])
ss = FakeSession()
try:
    run(agent_loop.run(adapter=ad, system="s", user="u", session=ss, budget={"calls": 1}))
    check(False, "пустой итог после предела — ошибка")
except LoopError as exc:
    check("не дала итогового ответа" in str(exc), f"текст ошибки: {exc}")
check(len(ad.steps) == 1 + agent_loop.MAX_FINAL_NUDGES + 1 and len(ss.calls) == 1,
      f"шагов после предела — не больше {agent_loop.MAX_FINAL_NUDGES + 1}: {len(ad.steps)}")

# 2.6. Объём контекста
ad = FakeAdapter([[("timeline", U)], [("series", {**U, "params": "a"})], "итог"], ctx_step=60_000)
ss = FakeSession()
r = run(agent_loop.run(adapter=ad, system="s", user="u", session=ss, budget={"tokens": 100_000}))
check(r.stop_reason == "limit_tokens" and len(ss.calls) == 2 and "объём контекста" in ad.notes[0],
      f"предел объёма контекста: {r.stop_reason}, вызовов {len(ss.calls)}")
ad = FakeAdapter([[("timeline", U)], "итог"], ctx_step=60_000)
r = run(agent_loop.run(adapter=ad, system="s", user="u", session=FakeSession(), budget={"tokens": 100_000}))
check(r.stop_reason == "done", "объём ниже предела — без остановки")

# 2.7. Минуты
_mono = agent_loop.time.monotonic
_clock = {"t": 1000.0}
agent_loop.time = types.SimpleNamespace(monotonic=lambda: _clock["t"])
try:
    def _slow(adapter):
        _clock["t"] += 400.0
        return Turn(text="", calls=[ToolCall("x1", "timeline", U)], stop="tools")
    ad = FakeAdapter([_slow, [("series", {**U, "params": "a"})], "итог после времени"])
    ss = FakeSession()
    r = run(agent_loop.run(adapter=ad, system="s", user="u", session=ss, budget={"minutes": 5}))
    check(r.stop_reason == "limit_minutes" and len(ss.calls) == 1, f"предел времени: {r.stop_reason}, вызовов {len(ss.calls)}")
    check(ad.steps[0]["timeout"] == 300.0 + agent_loop.FINAL_GRACE_SEC, f"срок шага — остаток бюджета и запас: {ad.steps[0]['timeout']}")
    check(ad.steps[1]["final"] and ad.steps[1]["timeout"] == agent_loop.FINAL_GRACE_SEC, "итоговый шаг — под сроком запаса")

    # время вышло до очередного шага (долгий вызов инструмента) — пометка отдельным сообщением
    class SlowSession(FakeSession):
        async def call(self, name, args):
            _clock["t"] += 700.0
            return await super().call(name, args)
    ad = FakeAdapter([[("timeline", U)], "итог"])
    r = run(agent_loop.run(adapter=ad, system="s", user="u", session=SlowSession(), budget={"minutes": 10}))
    check(r.stop_reason == "limit_minutes" and "время" in ad.notes[0], f"время вышло на инструменте: {r.stop_reason}")
finally:
    agent_loop.time = types.SimpleNamespace(monotonic=_mono)
    import time as _time
    agent_loop.time = _time

# 2.8. Неизвестный инструмент, неразобранные аргументы, обрезанный ответ
def _bad(adapter):
    return Turn(text="", calls=[ToolCall("b1", "series", {}, bad_args="не JSON: x"),
                                ToolCall("b2", "sql", {"q": "select"})], stop="tools")
def _cut(adapter):
    return Turn(text="", calls=[ToolCall("b3", "timeline", {})], stop="length")
ad = FakeAdapter([_bad, _cut, "итог"])
ss = FakeSession()
r = run(agent_loop.run(adapter=ad, system="s", user="u", session=ss))
check(len(ss.calls) == 0, "негодные вызовы к серверу не уходят")
check("не разобраны" in ad.results[0][0][1].text and "инструмента «sql» нет" in ad.results[0][1][1].text,
      "модель получает причину отказа")
check("обрезан" in ad.results[1][0][1].text and ad.notes == [], "обрезанный ответ — отказ без объявления предела")
check(r.stop_reason == "done" and r.text == "итог", "после негодных вызовов разбор продолжается")

# 2.9. Сбои сети
ad = FakeAdapter([[("timeline", U)], [("timeline", U)], [("timeline", U), ("series", {**U, "params": "a"})],
                  "итог без данных"])
ss = FakeSession(fail=99)
r = run(agent_loop.run(adapter=ad, system="s", user="u", session=ss))
check(len(ss.calls) == 3 and r.stop_reason == "no_server", f"три сбоя подряд — инструменты остановлены: {len(ss.calls)}, {r.stop_reason}")
check("сервер телеметрии недоступен" in ad.notes[0], "пометка о недоступном сервере")
ad = FakeAdapter([[("timeline", U)], [("timeline", U)], "итог"])
ss = FakeSession(fail=1)
r = run(agent_loop.run(adapter=ad, system="s", user="u", session=ss))
check(len(ss.calls) == 2 and r.stop_reason == "done" and r.tool_calls == 2, "один сбой — повтор разрешён, счёт сбоев сброшен")

# 2.10. Без инструментов и без зрения
ad = FakeAdapter(["ответ одним запросом\n\n## Данные\n- сам написал"])
r = run(agent_loop.run(adapter=ad, system="s", user="u", session=None, tools_note="инструменты недоступны"))
check(ad.tools == [] and r.iterations == 1 and r.text.endswith("- сам написал"), "без инструментов — один запрос, текст не трогается")
check(r.signature_tail().startswith("инструменты недоступны"), f"пометка в подписи: {r.signature_tail()}")
ad = FakeAdapter(["итог"], vision=False)
r = run(agent_loop.run(adapter=ad, system="s", user="u", session=FakeSession()))
check([t.name for t in ad.tools] == ["series", "timeline"], "модели без зрения график не предлагается")


class DeadSession:
    async def list_tools(self):
        raise McpUnavailable("нет связи")


ad = FakeAdapter(["итог"])
r = run(agent_loop.run(adapter=ad, system="s", user="u", session=DeadSession()))
check(ad.tools == [] and r.tools_note == "инструменты недоступны" and r.text == "итог",
      "сервер недоступен на входе — прогон без инструментов")

# 2.11. Стоимость из журнала
_orig_record = usage.record
registry_entry = {"id": "x", "price_in": 2.0, "price_out": 10.0}


class PricedAdapter(FakeAdapter):
    async def step(self, *, final, timeout):
        t = await super().step(final=final, timeout=timeout)
        await usage.record(provider="openai", model="m", entry=registry_entry,
                           usage={"prompt_tokens": 1_000_000, "completion_tokens": 100_000})
        return t


ad = PricedAdapter([[("timeline", U)], "итог"])
r = run(agent_loop.run(adapter=ad, system="s", user="u", session=FakeSession()))
check(r.cost_usd == 6.0 and r.priced_steps == 2 and "$6,00" in r.signature_tail(),
      f"стоимость прогона — сумма шагов: {r.cost_usd}, {r.signature_tail()}")


# ── 3. MCP-клиент против сервера-заглушки ────────────────────────────────────
print("3. MCP-клиент: сервер-заглушка")

import uvicorn
from mcp.server.mcpserver import Context, Image, MCPServer
from mcp.server.transport_security import TransportSecuritySettings

SEEN_META: list[dict] = []
PNG = bytes.fromhex("89504e470d0a1a0a") + b"stub"


def stub_app():
    server = MCPServer(name="stub", instructions="заглушка")

    @server.tool(name="timeline", structured_output=False, description="Лента событий")
    async def timeline(ctx: Context, unit: str, at: str | None = None) -> str:
        meta = getattr(ctx, "meta", None)
        if meta is None:
            rc = getattr(ctx, "request_context", None)
            meta = getattr(rc, "meta", None) if rc is not None else None
        headers = {str(k).lower(): v for k, v in dict(getattr(ctx, "headers", None) or {}).items()}
        SEEN_META.append({"meta_task": (meta.get("task_id") if hasattr(meta, "get") else getattr(meta, "task_id", None))
                          if meta is not None else None,
                          "x_task": headers.get("x-task-id"), "x_source": headers.get("x-source")})
        if unit == "нет":
            return "ОШИБКА: установка не найдена"
        return f"[T1] {unit} · timeline · окно · слой: события\nстрока"

    @server.tool(name="chart", structured_output=False, description="График")
    async def chart(unit: str):
        return [f"[T2] {unit} · chart · окно · слой: история\nсводка", Image(data=PNG, format="png")]

    return server.streamable_http_app(
        streamable_http_path="/mcp", json_response=True, stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        host="127.0.0.1")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def with_stub(fn):
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(stub_app(), host="127.0.0.1", port=port, log_level="critical"))
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        return await fn(f"http://127.0.0.1:{port}/mcp")
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)


async def _client_case(url):
    out = {}
    async with TelemetrySession("run-stub", url=url) as s:
        out["tools"] = await s.list_tools()
        out["ok"] = await s.call("timeline", {"unit": "ДГУ №1"})
        out["err"] = await s.call("timeline", {"unit": "нет"})
        out["img"] = await s.call("chart", {"unit": "ДГУ №1"})
        out["unknown"] = await s.call("нет_такого", {})
    out["probe"] = await mcp_client.probe(url)
    # полный прогон цикла через настоящего клиента
    ad = FakeAdapter([[("timeline", {"unit": "ДЭС №3"})], [("chart", {"unit": "ДЭС №3"})], "итог [T1]"])
    async with TelemetrySession("run-loop", url=url) as s:
        out["run"] = await agent_loop.run(adapter=ad, system="s", user="u", session=s, run_id="run-loop")
        out["adapter"] = ad
    return out


res = run(with_stub(_client_case))
check([t.name for t in res["tools"]] == ["chart", "timeline"], f"список инструментов: {[t.name for t in res['tools']]}")
check(res["tools"][1].description == "Лента событий" and "unit" in res["tools"][1].input_schema.get("properties", {}),
      "описание и схема аргументов")
check("ctx" not in res["tools"][1].input_schema.get("properties", {}), "служебный аргумент в схему не попал")
check(res["ok"].text.startswith("[T1] ДГУ №1 · timeline") and not res["ok"].is_error, f"вызов: {res['ok'].text!r}")
check(SEEN_META and SEEN_META[0]["x_task"] == "run-stub" and SEEN_META[0]["x_source"] == "analytics",
      f"номер прогона и источник — заголовками: {SEEN_META[:1]}")
check(SEEN_META[0]["meta_task"] == "run-stub", f"номер прогона — в _meta вызова: {SEEN_META[0]}")
check(res["err"].is_error and not res["err"].network_error, "ошибка инструмента — не сбой сети")
check(len(res["img"].images) == 1 and res["img"].images[0][0] == "image/png", "картинка графика получена")
import base64
check(base64.b64decode(res["img"].images[0][1]) == PNG, "картинка — base64 без искажений")
check(res["unknown"].is_error, "неизвестный инструмент — ошибка, не исключение")
check(res["probe"]["ok"] and res["probe"]["tools"] == 2, f"проба для настроек: {res['probe']}")
check(res["run"].tool_calls == 2 and "- [T1] ДЭС №3 · timeline · окно" in res["run"].text
      and "с графиком" in res["run"].text, f"прогон через клиента: {res['run'].text!r}")
check(len(res["adapter"].results[1][0][1].images) == 1, "картинка дошла до адаптера")


async def _dead():
    port = free_port()
    try:
        async with TelemetrySession("run-x", url=f"http://127.0.0.1:{port}/mcp"):
            return "вошёл"
    except McpUnavailable as exc:
        return exc


d = run(_dead())
check(isinstance(d, McpUnavailable), f"недоступный сервер — McpUnavailable: {d!r}")
p = run(mcp_client.probe(f"http://127.0.0.1:{free_port()}/mcp"))
check(p["ok"] is False and p["tools"] == 0, f"проба недоступного сервера: {p}")


# ── 4. Адаптер Claude ────────────────────────────────────────────────────────
print("4. Адаптер Claude")


def blk(**kw):
    return types.SimpleNamespace(**kw)


class FakeAnthropic:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests: list[dict] = []
        self.timeouts: list[float] = []
        self.messages = self
        self.closed = False

    def with_options(self, timeout=None):
        self.timeouts.append(timeout)
        return self

    async def create(self, **kwargs):
        # снимок запроса: история потом дополняется на месте
        self.requests.append(json.loads(json.dumps(kwargs, default=lambda o: getattr(o, "__dict__", str(o)))))
        return self.replies.pop(0)

    async def close(self):
        self.closed = True


def reply(content, stop="end_turn", usage_=None):
    return blk(content=content, stop_reason=stop, model="claude-test",
               usage=usage_ or blk(input_tokens=10, output_tokens=5, cache_read_input_tokens=900,
                                   cache_creation_input_tokens=100), stop_details=None)


think = blk(type="thinking", thinking="", signature="sig")
fa = FakeAnthropic([
    reply([think, blk(type="tool_use", id="tu1", name="chart", input={"unit": "ДГУ №1"}),
           blk(type="tool_use", id="tu2", name="timeline", input={"unit": "нет такой"})], stop="tool_use"),
    reply([blk(type="text", text="Итог [T1].")]),
])
ROWS.clear()
ca = ClaudeAdapter(model="claude-test", client=fa,
                   entry={"id": "cl", "provider": "anthropic", "reasoning": "high", "vision": True,
                          "price_in": 2.0, "price_out": 10.0, "price_cache_read": 0.1, "price_cache_write": 2.5})
r = run(agent_loop.run(adapter=ca, system="СИСТЕМА", user="бриф", session=FakeSession(),
                       budget={"minutes": 10}, run_id="run-claude"))
q1, q2 = fa.requests
check(q1["system"] == [{"type": "text", "text": "СИСТЕМА", "cache_control": {"type": "ephemeral"}}],
      "system — блоком с меткой кэша")
check(q1["cache_control"] == {"type": "ephemeral"} and q2["cache_control"] == {"type": "ephemeral"},
      "автоматическая метка кэша на хвосте разговора")
check(q1["tools"] == q2["tools"] == mcp_client.to_anthropic(sorted(SPECS, key=lambda t: t.name)),
      "инструменты одинаковы на всех шагах")
check(q1["system"] == q2["system"] and "tool_choice" not in q1 and "tool_choice" not in q2,
      "system не меняется, выбор инструмента не навязывается")
check(q1["thinking"] == {"type": "adaptive"} and q1["output_config"] == {"effort": "high"},
      f"уровень рассуждения записи: {q1.get('thinking')}, {q1.get('output_config')}")
check(q1["messages"] == [{"role": "user", "content": "бриф"}], "первый запрос — бриф")
check(q2["messages"][:1] == q1["messages"] and len(q2["messages"]) == 3, "история только дополняется")
asst = q2["messages"][1]
check(asst["role"] == "assistant" and [b["type"] for b in asst["content"]] == ["thinking", "tool_use", "tool_use"],
      f"ответ модели в истории целиком, с блоком рассуждений: {[b['type'] for b in asst['content']]}")
tr = q2["messages"][2]["content"]
check(tr[0]["type"] == "tool_result" and tr[0]["tool_use_id"] == "tu1"
      and tr[0]["content"][1] == {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"}},
      "картинка — внутри результата инструмента")
check(tr[1]["tool_use_id"] == "tu2" and tr[1].get("is_error") is True and "is_error" not in tr[0],
      "ошибка инструмента помечена")
check(len(tr) == 2, "без предела — без пометки")
check(r.text.startswith("Итог [T1].") and r.model == "claude-test", "итог и модель")
check(r.usage["tokens_cache_read"] == 1800 and r.context_tokens == 1015, f"расход: {r.usage}, контекст {r.context_tokens}")
want = round(2 * ((10 * 2 + 5 * 10 + 900 * 0.1 + 100 * 2.5) / 1e6), 6)
check(r.cost_usd == want and r.priced_steps == 2, f"стоимость по ценам записи: {r.cost_usd} (ждали {want})")
check([row["provider"] for row in ROWS] == ["anthropic", "anthropic"] and ROWS[0]["tool_calls"] == 2
      and ROWS[1]["tool_result_chars"] and ROWS[0]["entry_id"] == "cl", "строки журнала шагов Claude")
check(fa.timeouts and abs(fa.timeouts[0] - (600.0 + agent_loop.FINAL_GRACE_SEC)) < 1.0, f"срок запроса: {fa.timeouts[:1]}")

# пометка предела — текстом после результатов; уровень «по умолчанию» — без параметров
fa = FakeAnthropic([
    reply([blk(type="tool_use", id="t1", name="timeline", input={"unit": "ДГУ №1"})], stop="tool_use"),
    reply([blk(type="text", text="итог")]),
])
ca = ClaudeAdapter(model="claude-test", client=fa)
r = run(agent_loop.run(adapter=ca, system="s", user="u", session=FakeSession(), budget={"calls": 1}))
last = fa.requests[1]["messages"][2]["content"]
check(last[-1]["type"] == "text" and "Предел разбора" in last[-1]["text"] and last[0]["type"] == "tool_result",
      "пометка предела — текстовым блоком после результатов")
check("thinking" not in fa.requests[0] and "output_config" not in fa.requests[0], "уровень по умолчанию — параметры не шлются")
check(fa.requests[1]["tools"] == fa.requests[0]["tools"], "после предела инструменты в запросе те же")
check(ca.vision is True and ClaudeAdapter(model="m", client=fa, entry={"provider": "anthropic", "vision": False}).vision is False,
      "зрение: у Claude по умолчанию есть, запись может снять")
check(ClaudeAdapter(model="m", client=fa, entry={"provider": "anthropic", "reasoning": "off"}).effort == "low"
      and ClaudeAdapter(model="m", client=fa, entry={"provider": "anthropic", "reasoning": "xhigh"}).effort == "xhigh",
      "«выкл» — низший уровень, xhigh — как есть")

fa = FakeAnthropic([blk(content=[], stop_reason="refusal", model="m", usage=None, stop_details=blk(category="cyber"))])
try:
    run(agent_loop.run(adapter=ClaudeAdapter(model="m", client=fa), system="s", user="u"))
    check(False, "отказ модели — ошибка прогона")
except LoopError as exc:
    check("отказалась" in str(exc) and "cyber" in str(exc), f"отказ: {exc}")

fa = FakeAnthropic([reply([blk(type="tool_use", id="t1", name="timeline", input={})], stop="max_tokens"),
                    reply([blk(type="text", text="итог")])])
ca = ClaudeAdapter(model="m", client=fa)
ss = FakeSession()
r = run(agent_loop.run(adapter=ca, system="s", user="u", session=ss))
check(len(ss.calls) == 0 and r.text == "итог", "вызов из обрезанного ответа не исполняется")


# ── 5. Адаптер OpenAI-формата ────────────────────────────────────────────────
print("5. Адаптер OpenAI-формата")

REQS: list[dict] = []
PLAN: list = []


def handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    REQS.append({"url": str(request.url), "auth": request.headers.get("authorization"), "body": body})
    item = PLAN.pop(0)
    if isinstance(item, int):
        return httpx.Response(item, json={"error": {"message": "перегрузка" if item == 503 else "плохой запрос"}})
    return httpx.Response(200, json=item, headers=item.pop("_headers", {}))


def oa_reply(content=None, calls=None, finish=None, usage_=None, **extra):
    msg = {"role": "assistant", "content": content, **extra}
    if calls:
        msg["tool_calls"] = [{"id": i, "type": "function", "function": {"name": n, "arguments": a}}
                             for i, n, a in calls]
    return {"model": "m-served", "choices": [{"message": msg, "finish_reason": finish or ("tool_calls" if calls else "stop")}],
            "usage": usage_ or {"prompt_tokens": 1000, "completion_tokens": 50}}


_tr = llm_client._TRANSPORT
llm_client._TRANSPORT = httpx.MockTransport(handler)
_delays = OpenAIChatAdapter.__module__
import llm.adapters.openai_chat as oc
_saved_delays = oc._RETRY_DELAYS
oc._RETRY_DELAYS = (0.0, 0.0)
try:
    # 5.1. OpenAI со зрением
    gpt = registry.normalize_entry({"id": "gpt", "name": "GPT", "provider": "openai", "model": "gpt-x",
                                    "api_key": "sk-test", "vision": True, "reasoning": "medium",
                                    "price_in": 2, "price_out": 8})
    PLAN[:] = [503, oa_reply(calls=[("c1", "chart", json.dumps({"unit": "ДГУ №1"})), ("c2", "series", "{не json")]),
               oa_reply(content="Итог [T1].", usage_={"prompt_tokens": 3000, "completion_tokens": 200,
                                                      "prompt_tokens_details": {"cached_tokens": 1000}})]
    REQS.clear(); ROWS.clear()
    # формат chat/completions у OpenAI остаётся рабочим (старые модели); цикл по записи
    # openai берёт Responses API — раздел 5.7
    oa = OpenAIChatAdapter(entry=gpt, depth="incident")
    check(oa.vision and oa.provider == "openai", "адаптер chat/completions для записи OpenAI")
    r = run(agent_loop.run(adapter=oa, system="СИС", user="бриф", session=FakeSession(), budget={"calls": 1}))
    check(len(REQS) == 3 and REQS[0]["body"] == REQS[1]["body"], "при 503 запрос повторён тем же телом")
    b1, b2 = REQS[1]["body"], REQS[2]["body"]
    check(REQS[1]["url"] == "https://api.openai.com/v1/chat/completions" and REQS[1]["auth"] == "Bearer sk-test", "адрес и ключ")
    check(b1["tools"] == mcp_client.to_openai(sorted(SPECS, key=lambda t: t.name)) and b1["tool_choice"] == "auto",
          "инструменты и свободный выбор")
    check(b1["stream"] is False and "temperature" not in b1 and b1["reasoning_effort"] == "medium",
          "без потока, без температуры, уровень рассуждения записи")
    check(b1["messages"] == [{"role": "system", "content": "СИС"}, {"role": "user", "content": "бриф"}], "первый запрос")
    m = b2["messages"]
    check(m[2]["role"] == "assistant" and len(m[2]["tool_calls"]) == 2 and "reasoning_content" not in m[2],
          "сообщение модели в истории — с вызовами")
    check(m[3] == {"role": "tool", "tool_call_id": "c1", "content": m[3]["content"]} and m[3]["content"].startswith("[T1]"),
          "результат — сообщением tool")
    check(m[4]["role"] == "tool" and m[4]["tool_call_id"] == "c2" and "не разобраны" in m[4]["content"],
          "аргументы не JSON — отказ результатом")
    check(m[5]["role"] == "user" and m[5]["content"][1]["image_url"]["url"] == "data:image/png;base64,QUJD",
          "картинка — следующим сообщением за результатами")
    check(m[6] == {"role": "user", "content": m[6]["content"]} and "Предел разбора" in m[6]["content"], "пометка предела")
    check(b2["tool_choice"] == "auto" and b2["tools"] == b1["tools"],
          "после предела первый итоговый шаг — с прежним tool_choice (кэш цел), инструменты те же")
    check(r.text.startswith("Итог [T1].") and r.stop_reason == "limit_calls", "итог")
    check(r.usage["tokens_in"] == 3000 and r.usage["tokens_cache_read"] == 1000 and r.context_tokens == 3200,
          f"расход OpenAI: вход без кэша отдельно: {r.usage}, контекст {r.context_tokens}")
    check(abs(r.cost_usd - ((1000 * 2 + 50 * 8) + (2000 * 2 + 1000 * 2 + 200 * 8)) / 1e6) < 1e-9, f"стоимость: {r.cost_usd}")
    check([row["status"] for row in ROWS] == ["ok", "ok"] and ROWS[0]["tool_calls"] == 2 and ROWS[0]["model"] == "m-served",
          f"журнал шагов: {[(row['status'], row['tool_calls']) for row in ROWS]}")

    # 5.2. DeepSeek: рассуждение возвращается в историю, зрения нет
    ds = registry.normalize_entry({"id": "ds", "name": "DeepSeek", "provider": "deepseek", "model": "deepseek-x",
                                   "base_url": "https://api.deepseek.com", "api_key": "k", "reasoning": "xhigh"})
    PLAN[:] = [oa_reply(content="", calls=[("d1", "timeline", '{"unit": "ДГУ №1"}')], reasoning_content="думаю"),
               oa_reply(content="Итог.", reasoning_content="ещё думаю",
                        usage_={"prompt_tokens": 500, "completion_tokens": 40, "prompt_cache_hit_tokens": 300,
                                "prompt_cache_miss_tokens": 200})]
    REQS.clear()
    da = make_adapter(ds, depth="incident")
    ss = FakeSession()
    r = run(agent_loop.run(adapter=da, system="s", user="u", session=ss))
    b1, b2 = REQS[0]["body"], REQS[1]["body"]
    check([t["function"]["name"] for t in b1["tools"]] == ["series", "timeline"], "без зрения график не предлагается")
    check(b1["thinking"] == {"type": "enabled"} and b1["reasoning_effort"] == "max", f"рассуждение DeepSeek: {b1.get('reasoning_effort')}")
    check(b2["messages"][2].get("reasoning_content") == "думаю", "рассуждение DeepSeek возвращается в историю")
    check(r.usage["tokens_cache_read"] == 300 and r.text.startswith("Итог."), f"расход DeepSeek: {r.usage}")

    # 5.3. Ошибка 400 — сразу, без повтора; строка журнала с ошибкой
    PLAN[:] = [400]
    REQS.clear(); ROWS.clear()
    try:
        run(agent_loop.run(adapter=OpenAIChatAdapter(entry=gpt), system="s", user="u"))
        check(False, "400 — ошибка прогона")
    except LoopError as exc:
        check("HTTP 400" in str(exc) and "sk-test" not in str(exc), f"ошибка без ключа: {exc}")
    check(len(REQS) == 1 and ROWS and ROWS[-1]["status"] == "error", "400 не повторяется, в журнале — ошибка")

    # 5.4. Перегрузка дольше повторов
    PLAN[:] = [503, 503, 503]
    try:
        run(agent_loop.run(adapter=OpenAIChatAdapter(entry=gpt), system="s", user="u"))
        check(False, "503 трижды — ошибка")
    except LoopError as exc:
        check("503" in str(exc), f"ошибка перегрузки: {exc}")

    # 5.5. Прямой режим шлюза: поля шлюза, голова в подписи
    gw = registry.normalize_entry({"id": "gw", "name": "Горыныч", "provider": "gorynych", "model": "flashnext",
                                   "base_url": "http://gw:4000/src/analytics"})
    PLAN[:] = [{**oa_reply(content="Итог шлюза."), "gorynych": {"backend": "flashnext", "task_id": "gw-7"},
                "_headers": {"x-gorynych-task-id": "gw-7"}}]
    REQS.clear(); ROWS.clear()
    ga = make_adapter(gw, model="flashnext", depth="incident", priority="batch")
    r = run(agent_loop.run(adapter=ga, system="s", user="u", session=FakeSession()))
    b = REQS[0]["body"]
    check(REQS[0]["url"] == "http://gw:4000/src/analytics/v1/chat/completions" and b["priority"] == "batch"
          and b["notes"] is False and b["model"] == "flashnext" and "tools" in b, f"запрос к шлюзу: {sorted(b)}")
    check(r.model == "flashnext" and ROWS[-1]["backend"] == "flashnext" and ROWS[-1]["gateway_task_id"] == "gw-7",
          "голова и номер задачи шлюза — в журнале")
    check(r.cost_usd is None, "у шлюза стоимости нет")

    # 5.5б. Модель и после пометки просит инструмент — только тогда tool_choice none
    PLAN[:] = [oa_reply(calls=[("k1", "timeline", json.dumps({"unit": "ДГУ №1"}))]),
               oa_reply(calls=[("k2", "series", json.dumps({"unit": "ДГУ №1", "params": "a"}))]),
               oa_reply(content="Итог после отказа.")]
    REQS.clear()
    ss = FakeSession()
    r = run(agent_loop.run(adapter=OpenAIChatAdapter(entry=gpt), system="s", user="u", session=ss, budget={"calls": 1}))
    check([q["body"]["tool_choice"] for q in REQS] == ["auto", "auto", "none"] and len(ss.calls) == 1
          and r.text.startswith("Итог после отказа."), f"tool_choice по шагам: {[q['body']['tool_choice'] for q in REQS]}")

    # 5.7. OpenAI в цикле — Responses API: вызовы и рассуждения списком output, история целиком
    def rs_reply(output, usage_=None, **extra):
        return {"model": "gpt-served", "status": "completed", "output": output,
                "usage": usage_ or {"input_tokens": 1000, "input_tokens_details": {"cached_tokens": 0},
                                    "output_tokens": 50, "output_tokens_details": {"reasoning_tokens": 30}}, **extra}

    reasoning_item = {"id": "rs_1", "type": "reasoning", "summary": [], "encrypted_content": "ENC-1"}
    fc1 = {"id": "fc_1", "type": "function_call", "call_id": "call_1", "name": "chart", "arguments": json.dumps({"unit": "ДГУ №1"})}
    fc2 = {"id": "fc_2", "type": "function_call", "call_id": "call_2", "name": "series", "arguments": "{не json"}
    msg_item = {"id": "msg_1", "type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": "Итог Responses [T1]."}]}
    PLAN[:] = [503, rs_reply([reasoning_item, fc1, fc2]),
               rs_reply([msg_item], usage_={"input_tokens": 3000, "input_tokens_details": {"cached_tokens": 1000},
                                            "output_tokens": 200, "output_tokens_details": {"reasoning_tokens": 120}})]
    REQS.clear(); ROWS.clear()
    from llm.adapters.openai_responses import OpenAIResponsesAdapter
    ra = make_adapter(gpt, depth="incident")
    check(isinstance(ra, OpenAIResponsesAdapter) and ra.vision, "запись OpenAI в цикле — адаптер Responses API")
    r = run(agent_loop.run(adapter=ra, system="СИС", user="бриф", session=FakeSession(), budget={"calls": 1}))
    check(len(REQS) == 3 and REQS[0]["body"] == REQS[1]["body"], "Responses: при 503 запрос повторён")
    b1, b2 = REQS[1]["body"], REQS[2]["body"]
    check(REQS[1]["url"] == "https://api.openai.com/v1/responses" and REQS[1]["auth"] == "Bearer sk-test", f"адрес Responses: {REQS[1]['url']}")
    check(b1["instructions"] == "СИС" and b1["input"] == [{"role": "user", "content": "бриф"}] and b1["store"] is False,
          "системный промпт — instructions, разговор — input, без хранения на стороне OpenAI")
    check(b1["tools"][1] == {"type": "function", "name": "series", "description": "Ряд параметра",
                             "parameters": SPECS[1].input_schema} and b1["tool_choice"] == "auto",
          "инструменты в плоском виде Responses")
    check(b1["reasoning"] == {"effort": "medium"} and "temperature" not in b1 and "messages" not in b1,
          "уровень рассуждения записи, полей chat/completions нет")
    inp = b2["input"]
    check(inp[1] == reasoning_item and inp[2] == fc1 and inp[3] == fc2,
          "ответ модели в истории целиком: рассуждение (зашифрованное) вместе с вызовами")
    check(inp[4] == {"type": "function_call_output", "call_id": "call_1", "output": inp[4]["output"]}
          and inp[4]["output"].startswith("[T1]"), "результат — function_call_output с call_id")
    check(inp[5]["call_id"] == "call_2" and "не разобраны" in inp[5]["output"], "аргументы не JSON — отказ результатом")
    check(inp[6]["role"] == "user" and inp[6]["content"][1] == {"type": "input_image", "image_url": "data:image/png;base64,QUJD"},
          "картинка — следующим сообщением частью input_image")
    check(inp[7]["role"] == "user" and "Предел разбора" in inp[7]["content"] and b2["tools"] == b1["tools"]
          and b2["tool_choice"] == "auto", "пометка предела, инструменты и tool_choice прежние")
    check(r.text.startswith("Итог Responses [T1].") and r.model == "gpt-served", "итог и модель")
    check(r.usage == {"tokens_in": 3000, "tokens_out": 250, "tokens_cache_read": 1000, "tokens_cache_write": None,
                      "tokens_reasoning": 150} and r.context_tokens == 3200,
          f"расход Responses: вход без кэша отдельно, рассуждения внутри выхода: {r.usage}")
    check(abs(r.cost_usd - ((1000 * 2 + 50 * 8) + (2000 * 2 + 1000 * 2 + 200 * 8)) / 1e6) < 1e-9, f"стоимость Responses: {r.cost_usd}")
    check([row["status"] for row in ROWS] == ["ok", "ok"] and ROWS[0]["tool_calls"] == 2
          and ROWS[1]["tool_result_chars"], "журнал шагов Responses")

    PLAN[:] = [rs_reply([reasoning_item], status="incomplete", incomplete_details={"reason": "max_output_tokens"}),
               rs_reply([msg_item])]
    try:
        run(agent_loop.run(adapter=make_adapter(gpt), system="s", user="u"))
        check(False, "обрезанный ответ без текста — ошибка прогона")
    except LoopError as exc:
        check("не дала итогового ответа" in str(exc), f"обрезанный ответ: {exc}")
    PLAN[:] = [rs_reply([{"type": "message", "content": [{"type": "refusal", "refusal": "не могу"}]}])]
    try:
        run(agent_loop.run(adapter=make_adapter(gpt), system="s", user="u"))
        check(False, "отказ модели — ошибка прогона")
    except LoopError as exc:
        check("отказалась" in str(exc), f"отказ Responses: {exc}")
    PLAN[:] = [400]
    ROWS.clear()
    try:
        run(agent_loop.run(adapter=make_adapter(gpt), system="s", user="u"))
        check(False, "400 — ошибка прогона")
    except LoopError as exc:
        check("HTTP 400" in str(exc) and "sk-test" not in str(exc) and ROWS[-1]["status"] == "error", f"400 Responses: {exc}")

    # 5.6. Gemini: точка OpenAI-формата Google, упрощённые схемы, подписи рассуждений в истории
    gm = registry.normalize_entry({"id": "gem", "name": "Gemini", "provider": "gemini", "model": "gemini-x",
                                   "api_key": "g-key", "reasoning": "high", "price_in": 1, "price_out": 5})
    sig_call = {"id": "g1", "type": "function", "function": {"name": "chart", "arguments": json.dumps({"unit": "ДГУ №1"})},
                "extra_content": {"google": {"thought_signature": "SIG-1"}}}
    first = oa_reply(content=None)
    first["choices"][0]["message"].update({"tool_calls": [sig_call], "extra_content": {"google": {"thought_signature": "SIG-M"}}})
    first["choices"][0]["finish_reason"] = "tool_calls"
    PLAN[:] = [first, oa_reply(content="Итог Gemini [T1].")]
    REQS.clear(); ROWS.clear()
    gma = make_adapter(gm, depth="incident")
    check(isinstance(gma, OpenAIChatAdapter) and gma.vision, "Gemini — адаптер OpenAI-формата, зрение по умолчанию")
    r = run(agent_loop.run(adapter=gma, system="СИС", user="бриф", session=FakeSession()))
    b1, b2 = REQS[0]["body"], REQS[1]["body"]
    check(REQS[0]["url"] == "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
          and REQS[0]["auth"] == "Bearer g-key", f"адрес и ключ Gemini: {REQS[0]['url']}")
    check("temperature" not in b1 and b1["reasoning_effort"] == "high" and b1["tool_choice"] == "auto",
          "без температуры, уровень рассуждения записи")
    check([t["function"]["name"] for t in b1["tools"]] == ["chart", "series", "timeline"], "со зрением график предлагается")
    echoed = b2["messages"][2]
    check(echoed["role"] == "assistant" and echoed["tool_calls"][0]["extra_content"] == {"google": {"thought_signature": "SIG-1"}}
          and echoed.get("extra_content") == {"google": {"thought_signature": "SIG-M"}},
          f"подписи рассуждений Gemini возвращаются в историю: {sorted(echoed)}")
    check(b2["messages"][3]["role"] == "tool" and b2["messages"][4]["role"] == "user"
          and b2["messages"][4]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,"),
          "результат и картинка следующим сообщением")
    check(r.text.startswith("Итог Gemini [T1].") and ROWS[-1]["provider"] == "gemini" and r.cost_usd is not None,
          f"итог, журнал и стоимость Gemini: {r.cost_usd}")
finally:
    llm_client._TRANSPORT = _tr
    oc._RETRY_DELAYS = _saved_delays

# Упрощение схем для Gemini: «значение или null» → значение, без title и умолчаний
_schema = {"type": "object", "title": "seriesArguments", "required": ["unit"], "properties": {
    "unit": {"title": "Unit", "type": "string", "description": "Установка"},
    "t_from": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None, "title": "T From", "description": "Начало окна"},
    "minutes": {"anyOf": [{"type": "integer"}, {"type": "null"}], "default": None, "title": "Minutes"},
    "title": {"type": "string", "title": "Title", "description": "параметр с именем title"},
    "mix": {"anyOf": [{"type": "string"}, {"type": "integer"}, {"type": "null"}], "default": 5},
    "flag": {"type": "boolean", "default": False, "title": "Flag"}}}
_simple = mcp_client.simplify_schema(_schema)
check(_simple == {"type": "object", "required": ["unit"], "properties": {
    "unit": {"type": "string", "description": "Установка"},
    "t_from": {"type": "string", "description": "Начало окна"},
    "minutes": {"type": "integer"},
    "title": {"type": "string", "description": "параметр с именем title"},
    "mix": {"anyOf": [{"type": "string"}, {"type": "integer"}]},
    "flag": {"type": "boolean"}}}, f"упрощённая схема: {_simple}")
check(_schema["properties"]["t_from"].get("default", "нет") is None and "title" in _schema, "исходная схема не изменена")
_spec = [ToolSpec("series", "Ряд", _schema)]
check(mcp_client.to_openai(_spec)[0]["function"]["parameters"] is _schema
      and mcp_client.to_openai(_spec, simplify=True)[0]["function"]["parameters"] == _simple,
      "упрощение — только по просьбе (Gemini), остальным схема как у сервера")

try:
    make_adapter({"provider": "ollama"})
    check(False, "Ollama в цикле не поддерживается")
except ValueError:
    pass
check(isinstance(make_adapter(None, model="claude-x"), ClaudeAdapter), "без записи — Claude")


# ── 6. Правила промпта и разбор аварии ───────────────────────────────────────
print("6. Правила промпта для инструментов и разбор аварии через цикл")

t0, m0 = prompts.system_for("incident")
t1, m1 = prompts.system_for("incident", tools=True)
check(t1.startswith(t0) and len(t1) > len(t0) and prompts.RULES_HEADER in t1 and prompts.RULES_HEADER not in t0,
      "правила инструментов — только в прогоне с инструментами")
check(m0["rules"] == prompts.PROMPT_RULES_VERSION and m1["rules"] == f"{prompts.PROMPT_RULES_VERSION}+t{prompts.TOOLS_RULES_VERSION}"
      and m0["version"] != m1["version"] and m1["tools"] is True, f"версии: {m0['version']} / {m1['version']}")
check(all(i.startswith("R-T-") for i in m1["rule_ids"]) and len(m1["rule_ids"]) == len(prompts.TOOL_RULES), f"состав правил: {m1['rule_ids']}")
for task in ("seg_auto", "seg_manual", "warning_claude"):
    _a, _b = prompts.system_for(task), prompts.system_for(task, tools=True)
    check(_a[0] == _b[0] and _a[1]["version"] == _b[1]["version"], f"{task}: правила инструментов не подмешиваются")
check("«## Данные» в конце не пиши" in t1 and "timeline" in t1 and "[Tn]" in t1, "ключевые правила в тексте")
check(not any(r.tools_only for r in prompts.rules_for("incident")) and
      sum(r.tools_only for r in prompts.rules_for("incident", True)) == len(prompts.TOOL_RULES), "rules_for с инструментами и без")

from online import incident_gate as ig

check(ig._unit_name("Картина объекта (карта установок):\nОбъект: КС\nЭта установка: ДГУ №1 инв. 2569 — двигатель Cummins KTA50.\n")
      == "ДГУ №1 инв. 2569", "имя установки из картины объекта")
check(ig._unit_name("Эта установка: имя не задано — двигатель X") is None and ig._unit_name("нет картины") is None,
      "имени нет — подсказки нет")

_reg_saved, _depth_saved = registry.serialize(), router.serialize_depth()
_ask_claude, _session_cls, _make, _clean = ig._ask_claude, mcp_client.TelemetrySession, None, None
import llm.adapters as adapters_pkg
import llm.lang_check as lang_check

_make = adapters_pkg.make_adapter
_clean = lang_check.clean_text
CALLS: list = []


async def fake_claude(prompt, model, system=None):
    CALLS.append(("claude", model, system))
    return "старый путь"


async def no_clean(text, prompt, **kw):
    return text, None


class StubSession:
    alive = True

    def __init__(self, run_id, **kw):
        self.run_id = run_id

    async def __aenter__(self):
        if not StubSession.alive:
            raise McpUnavailable("нет связи")
        self._s = FakeSession()
        return self

    async def __aexit__(self, *a):
        CALLS.append(("closed", self.run_id))

    async def list_tools(self):
        return await self._s.list_tools()

    async def call(self, name, args):
        return await self._s.call(name, args)


LAST: dict = {}


def fake_make(entry, **kw):
    ad = FakeAdapter([[("timeline", {"unit": "ДГУ №1 инв. 2569"})], "Разбор [T1]."] if LAST.get("tools", True)
                     else ["Разбор без инструментов."],
                     vision=bool((entry or {}).get("vision", entry is None)))
    ad.model = kw.get("model") or "?"
    LAST.update(adapter=ad, entry=entry, kw=kw)
    return ad


PROMPT = ("Картина объекта (карта установок):\nЭта установка: ДГУ №1 инв. 2569 — двигатель Cummins KTA50.\n\n"
          "Акт аварийного останова …")
try:
    ig._ask_claude = fake_claude
    mcp_client.TelemetrySession = StubSession
    adapters_pkg.make_adapter = fake_make
    lang_check.clean_text = no_clean
    registry.load_registry(json.dumps([
        {"id": "gpt", "name": "GPT", "provider": "openai", "model": "gpt-x", "api_key": "k", "vision": True},
        {"id": "ds", "name": "DeepSeek", "provider": "deepseek", "model": "ds-x", "api_key": "k", "tools": False},
    ]))

    # 6.0. В облако вход уходит без серийного номера роутера (Р11)
    check(ig.scrub_private("# Отчёт — ДГУ (`8640X123456` / панель 1)\nтройка 8640X123456/pcc/1", ("8640X123456",))
          == "# Отчёт — ДГУ (`(скрыто)` / панель 1)\nтройка (скрыто)/pcc/1", "серийник роутера вычищается")
    check(ig.scrub_private("текст 1234", ("1234", "", None)) == "текст 1234", "короткие и пустые значения не трогаются")

    # 6.1. Ни подключения, ни инструментов — прежний одиночный запрос Claude
    router.apply_depth("incident", "smart", "xhigh", "batch", provider="api", claude_model="claude-old",
                       api_entry="", tools="", budgets={k: "" for k in router.BUDGET_KEYS})
    meta: dict = {}
    text = run(ig._ask(PROMPT, meta, system="СИС"))
    check(CALLS[-1] == ("claude", "claude-old", "СИС") and "loop" not in meta and text.startswith("старый путь"),
          f"прежний путь не тронут: {CALLS[-1]}")

    # 6.2. Подключение OpenAI + инструменты
    router.apply_depth("incident", "smart", "xhigh", "batch", api_entry="gpt", tools="telemetry",
                       claude_model="", budgets={"budget_calls": 5})
    CALLS.clear(); LAST.clear(); meta = {}
    text = run(ig._ask(PROMPT, meta, system="СИС"))
    ad = LAST["adapter"]
    check(LAST["entry"]["id"] == "gpt" and LAST["kw"]["model"] == "gpt-x" and LAST["kw"]["depth"] == "incident",
          f"адаптер — по подключению строки: {LAST['kw']}")
    check(ad.started[0] == prompts.system_for("incident", tools=True)[0], "system — с правилами инструментов")
    check(ad.started[1].startswith(PROMPT) and ad.started[1].endswith("установка этого акта — «ДГУ №1 инв. 2569»."),
          "бриф не изменён, имя установки — подсказкой в конце")
    check("## Данные\n- [T1] ДГУ №1 инв. 2569 · timeline · окно" in text, f"блок «Данные» в разборе: {text!r}")
    check("Модель: gpt-x · инструментов 1" in text or not router.format_ai_signature("x"),
          f"подпись с расходом: {text[-120:]!r}")
    check(meta["loop"]["tool_calls"] == 1 and meta["loop"]["run_id"].startswith("run-") and meta["loop"]["stop_reason"] == "done",
          f"сводка прогона для записи разбора: {meta.get('loop')}")
    check(meta["prompt"] == prompts.system_for("incident", tools=True)[1]["version"], "версия промпта — с отметкой инструментов")
    check(ad.closed and ("closed", meta["loop"]["run_id"]) in CALLS, "адаптер и подключение закрыты")
    check(not any(c[0] == "claude" for c in CALLS), "старый путь не вызывался")
    LAST.clear(); meta = {}
    run(ig._ask(PROMPT + "\nотчёт `8640X123456` / панель 1", meta, system="СИС", private=("8640X123456",)))
    check("8640X123456" not in LAST["adapter"].started[1] and "(скрыто)" in LAST["adapter"].started[1],
          "бриф цикла — без серийника роутера")

    # 6.2б. Очистка языка переписывает только текст разбора: блок «Данные» хоста добавляется после неё
    async def rewriting(text, prompt, **kw):
        CALLS.append(("clean", text))
        return "ОЧИЩЕННЫЙ ТЕКСТ.", {"found": ["x"], "cleaned": True, "accepted": True}
    lang_check.clean_text = rewriting
    LAST.clear(); CALLS.clear(); meta = {}
    text = run(ig._ask(PROMPT, meta, system="СИС"))
    cleaned_input = next(c[1] for c in CALLS if c[0] == "clean")
    check("## Данные" not in cleaned_input and cleaned_input == "Разбор [T1].", f"под очистку идёт только текст разбора: {cleaned_input!r}")
    check(text.startswith("ОЧИЩЕННЫЙ ТЕКСТ.\n\n## Данные\n- [T1] ДГУ №1 инв. 2569 · timeline · окно")
          and meta.get("lang_check", {}).get("accepted") is True, f"блок «Данные» цел после очистки: {text[:120]!r}")
    lang_check.clean_text = no_clean

    # 6.3. Сервер телеметрии недоступен — разбор без инструментов, с пометкой
    StubSession.alive = False
    LAST.clear(); LAST["tools"] = False; meta = {}
    text = run(ig._ask(PROMPT, meta, system="СИС"))
    ad = LAST["adapter"]
    check(ad.tools == [] and ad.started == ("СИС", PROMPT), "без сервера — прежний system и бриф без подсказки")
    check(meta["loop"]["tools_note"] == "инструменты недоступны" and "prompt" not in meta
          and text.startswith("Разбор без инструментов."), f"пометка: {meta['loop'].get('tools_note')}")
    StubSession.alive = True

    # 6.4. Подключение не умеет инструменты
    router.apply_depth("incident", "smart", "xhigh", "batch", api_entry="ds")
    LAST.clear(); LAST["tools"] = False; CALLS.clear(); meta = {}
    text = run(ig._ask(PROMPT, meta, system="СИС"))
    check(LAST["adapter"].tools == [] and meta["loop"]["tools_note"] == "подключение не умеет инструменты"
          and not any(c[0] == "closed" for c in CALLS), "запись без признака инструментов — сервер не трогаем")

    # 6.5. Подключение без инструментов строки — один запрос через адаптер
    router.apply_depth("incident", "smart", "xhigh", "batch", api_entry="gpt", tools="")
    LAST.clear(); LAST["tools"] = False; meta = {}
    text = run(ig._ask(PROMPT, meta, system="СИС"))
    check(LAST["entry"]["id"] == "gpt" and LAST["adapter"].tools == [] and meta["loop"]["tools_note"] is None
          and meta["loop"]["iterations"] == 1, "подключение из реестра без инструментов — один запрос")

    # 6.6. Строка на шлюзе цикл не трогает
    check(ig._route()[0] == "api", "строка на облаке")
finally:
    ig._ask_claude = _ask_claude
    mcp_client.TelemetrySession = _session_cls
    adapters_pkg.make_adapter = _make
    lang_check.clean_text = _clean
    registry.load_registry(_reg_saved)
    router.load_depth(_depth_saved)

usage.set_sink(None)
print()
if _errors:
    print(f"Не пройдено проверок: {len(_errors)}")
    for msg in _errors:
        print("  -", msg)
    sys.exit(1)
print("Все проверки пройдены.")
