"""v5.2.6 «Журнал обращений к моделям» (MCP Э2б, шаг 1; docs/plans/design_mcp_e2.md,
решения Р5, Р22, Р25, Р26). Без БД и без сети: приёмник строк — set_sink, HTTP —
httpx.MockTransport.

Раздел 1 — нормализация расхода (llm.usage.normalize_usage): Anthropic (объект и
словарь, кэш чтение/запись), OpenAI (кэш внутри prompt_tokens, рассуждения), DeepSeek
(попадание/промах кэша), шлюз и Ollama (без кэша), пусто — все None, не 0.
Раздел 2 — стоимость (cost_usd): цены записи реестра USD за 1 млн; цен нет — None;
цена кэша не задана — по цене входа; мусор в ценах.
Раздел 3 — контекст вызова: task_for по виду разбора, вложенность и наследование
call_context, наследование задачей asyncio, last_call_id.
Раздел 4 — запись (record): состав строки, вид задачи по контексту/виду/умолчанию;
без приёмника и без включения сервисом в БД не пишет; отказ приёмника вызов не
ломает; annotate дописывает вердикт.
Раздел 5 — Claude API (anthropic_create): успех, вызов инструмента, пустой ответ,
ошибка (строка с error и проброс), параметры доходят до SDK без изменений.
Раздел 6 — общий клиент (chat/chat_stream): поток шлюза (расход и голова из
последнего куска, task_id из заголовка), ответ без потока, пустой ответ, ошибка 400
(строка error с кодом шлюза), поток Ollama, meta вызывающего заполняется как раньше,
stream_options у DeepSeek/OpenAI.
Раздел 7 — схема: таблица llm_calls в online_schema.sql, без CHECK, все колонки
модуля есть в DDL.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_errors: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        _errors.append(msg)
        print("  ✗", msg)


import logging

logging.disable(logging.CRITICAL)

import httpx

from llm import usage
from llm import client as llm_client

ROWS: list[dict] = []


async def sink(row: dict):
    ROWS.append(dict(row))
    return len(ROWS)


def run(coro):
    return asyncio.run(coro)


# ── 1. Нормализация расхода ──────────────────────────────────────────────────
class AU:
    input_tokens = 1200
    output_tokens = 340
    cache_read_input_tokens = 9000
    cache_creation_input_tokens = 500


n = usage.normalize_usage("anthropic", AU())
check(n == {"tokens_in": 1200, "tokens_out": 340, "tokens_cache_read": 9000, "tokens_cache_write": 500, "tokens_reasoning": None},
      f"1: Anthropic объектом: {n}")
n = usage.normalize_usage("anthropic", {"input_tokens": 10, "output_tokens": 5})
check(n["tokens_in"] == 10 and n["tokens_out"] == 5 and n["tokens_cache_read"] is None and n["tokens_cache_write"] is None,
      f"1: Anthropic без кэша — None, не 0: {n}")
n = usage.normalize_usage("openai", {"prompt_tokens": 5000, "completion_tokens": 800,
                                     "prompt_tokens_details": {"cached_tokens": 4096},
                                     "completion_tokens_details": {"reasoning_tokens": 512}})
check(n == {"tokens_in": 904, "tokens_out": 800, "tokens_cache_read": 4096, "tokens_cache_write": None, "tokens_reasoning": 512},
      f"1: OpenAI — вход без кэша, рассуждения: {n}")
n = usage.normalize_usage("deepseek", {"prompt_tokens": 3000, "completion_tokens": 200,
                                       "prompt_cache_hit_tokens": 2560, "prompt_cache_miss_tokens": 440,
                                       "completion_tokens_details": {"reasoning_tokens": 90}})
check(n == {"tokens_in": 440, "tokens_out": 200, "tokens_cache_read": 2560, "tokens_cache_write": None, "tokens_reasoning": 90},
      f"1: DeepSeek — промах/попадание кэша: {n}")
n = usage.normalize_usage("gorynych", {"prompt_tokens": 42000, "completion_tokens": 1500})
check(n["tokens_in"] == 42000 and n["tokens_out"] == 1500 and n["tokens_cache_read"] is None, f"1: шлюз без кэша: {n}")
n = usage.normalize_usage("ollama", None)
check(all(v is None for v in n.values()), f"1: расхода нет — все None: {n}")
n = usage.normalize_usage("lmstudio", {"prompt_tokens": "x", "completion_tokens": True})
check(n["tokens_in"] is None and n["tokens_out"] is None, f"1: мусор — None: {n}")

# ── 2. Стоимость ─────────────────────────────────────────────────────────────
E = {"id": "claude-api", "price_in": 3, "price_out": 15, "price_cache_read": 0.3, "price_cache_write": 3.75}
u = {"tokens_in": 1200, "tokens_out": 340, "tokens_cache_read": 9000, "tokens_cache_write": 500}
c = usage.cost_usd(E, u)
check(abs(c - (1200 * 3 + 340 * 15 + 9000 * 0.3 + 500 * 3.75) / 1e6) < 1e-9, f"2: стоимость по четырём ценам: {c}")
check(usage.cost_usd(None, u) is None and usage.cost_usd({"id": "gw"}, u) is None and usage.cost_usd({"price_in": ""}, u) is None,
      "2: цен нет — None")
c2 = usage.cost_usd({"price_in": 2, "price_out": 8}, u)
check(abs(c2 - (1200 * 2 + 340 * 8 + 9000 * 2 + 500 * 2) / 1e6) < 1e-9, f"2: цена кэша не задана — по цене входа: {c2}")
check(usage.cost_usd({"price_in": "abc", "price_out": -1}, u) is None, "2: мусор в ценах — None")
check(usage.cost_usd({"price_in": 0, "price_out": 0}, u) == 0.0, "2: нулевые цены — стоимость 0, а не None")
check(usage.cost_usd({"price_out": 10}, {"tokens_in": None, "tokens_out": 1000}) == 0.01, "2: только цена выхода, расход с None")

# ── 3. Контекст ──────────────────────────────────────────────────────────────
check(usage.task_for("seg_norma") == "seg" and usage.task_for("gate_warning") == "gate" and usage.task_for("incident") == "incident"
      and usage.task_for("humanize") == "humanize" and usage.task_for("manual") == "manual" and usage.task_for(None) is None
      and usage.task_for("что-то") is None, "3: вид задачи по виду разбора")
check(usage.current() == {}, "3: вне блока контекст пуст")
with usage.call_context(task="incident", ref=35941, prompt_version="pv1") as cx:
    check(usage.current()["task"] == "incident" and usage.current()["ref"] == 35941, "3: контекст внутри блока")
    with usage.call_context(task="lang_check", depth=None) as cx2:
        cur = usage.current()
        check(cur["task"] == "lang_check" and cur["ref"] == 35941 and cur["prompt_version"] == "pv1",
              f"3: вложенный блок перекрывает задачу и наследует остальное: {cur}")
    check(usage.current()["task"] == "incident", "3: после вложенного блока — прежний контекст")
check(usage.current() == {}, "3: после блока контекст пуст")


async def _task_inherits():
    with usage.call_context(task="gate", ref="R/pcc/1"):
        return await asyncio.wait_for(asyncio.sleep(0, result=usage.current().get("task")), 1), \
            await asyncio.create_task(_read_task())


async def _read_task():
    return usage.current().get("ref")


check(run(_task_inherits()) == ("gate", "R/pcc/1"), "3: задача asyncio наследует контекст")

# ── 4. Запись ────────────────────────────────────────────────────────────────
usage.set_sink(sink)
ROWS.clear()


async def _rec():
    with usage.call_context(task="seg", ref=36001, depth="seg_caution", prompt_version="v7") as cx:
        rid = await usage.record(provider="gorynych", model="fast", entry={"id": "gw"}, backend="q27-5090",
                                 reasoning="high", priority="normal", system_chars=900, input_chars=41000,
                                 output_chars=2100, usage={"prompt_tokens": 24000, "completion_tokens": 700},
                                 duration_ms=5400, gateway_task_id="t-77")
        return rid, dict(cx)


rid, cx = run(_rec())
r = ROWS[-1]
check(rid == 1 and cx.get("last_call_id") == 1, f"4: id строки и last_call_id в контексте: {rid} {cx.get('last_call_id')}")
check(r["task_kind"] == "seg" and r["depth"] == "seg_caution" and r["task_ref"] == "36001" and r["prompt_version"] == "v7",
      f"4: задача, вид, ссылка строкой, версия промпта: {r['task_kind']} {r['depth']} {r['task_ref']}")
check(r["entry_id"] == "gw" and r["backend"] == "q27-5090" and r["tokens_in"] == 24000 and r["tokens_out"] == 700
      and r["gateway_task_id"] == "t-77" and r["cost_usd"] is None and r["status"] == "ok", "4: поля вызова, без цен стоимость None")
check(set(r) <= set(usage.COLUMNS), f"4: в строке только колонки таблицы: {set(r) - set(usage.COLUMNS)}")
run(usage.record(provider="gorynych", model="fast", depth="gate_caution"))
check(ROWS[-1]["task_kind"] == "gate", "4: без контекста задача — по виду разбора")
run(usage.record(provider="lmstudio", model="x"))
check(ROWS[-1]["task_kind"] == "other" and ROWS[-1]["depth"] is None, "4: ни контекста, ни вида — other")
run(usage.record(provider="anthropic", model="m", entry=E, usage=AU(), error="x" * 3000, status="error"))
check(len(ROWS[-1]["error"]) == 1000 and ROWS[-1]["cost_usd"] is not None, "4: ошибка обрезана, стоимость по ценам записи")

# без приёмника и без включения сервисом — в БД не ходит
usage.set_sink(None)
_called = []


async def _boom(row):
    _called.append(row)
    raise AssertionError("в БД ходить нельзя")


_orig_insert = usage._insert
usage._insert = _boom
usage.enable(False)
check(run(usage.record(provider="anthropic", model="m")) is None and not _called, "4: журнал не включён — БД не трогаем")
run(usage.annotate(5, verdict="cancel"))
check(not _called, "4: annotate без включения — БД не трогаем")
# включён, БД падает — вызов не ломается
usage.enable(True)
check(run(usage.record(provider="anthropic", model="m")) is None and len(_called) == 1, "4: отказ БД — None, без исключения")
usage.enable(False)
usage._insert = _orig_insert


async def _bad_sink(row):
    raise RuntimeError("приёмник упал")


usage.set_sink(_bad_sink)
check(run(usage.record(provider="anthropic", model="m")) is None, "4: отказ приёмника вызов не ломает")
usage.set_sink(sink)
ROWS.clear()
run(usage.annotate({"last_call_id": 12}, verdict="cancel", лишнее="x"))
check(ROWS == [{"_annotate": 12, "verdict": "cancel"}], f"4: annotate — только разрешённые поля: {ROWS}")
ROWS.clear()
run(usage.annotate({}, verdict="pass"))
run(usage.annotate(7))
check(ROWS == [], "4: annotate без id или без полей — ничего")


# ── 5. Claude API ────────────────────────────────────────────────────────────
class Blk:
    def __init__(self, type_, text=None, name=None):
        self.type, self.text, self.name = type_, text, name


class Resp:
    def __init__(self, content, model="claude-x"):
        self.content, self.model, self.usage, self.stop_reason = content, model, AU(), "end_turn"


class FakeMessages:
    def __init__(self, result):
        self.result, self.kwargs = result, None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeClient:
    def __init__(self, result):
        self.messages = FakeMessages(result)


ROWS.clear()
fc = FakeClient(Resp([Blk("text", "Разбор: причина — заброс оборотов.")]))
resp = run(usage.anthropic_create(fc, depth="incident", model="claude-x", max_tokens=100, system="Ты инженер.",
                                  messages=[{"role": "user", "content": "Акт: …"}]))
r = ROWS[-1]
check(resp is fc.messages.result and fc.messages.kwargs == {"model": "claude-x", "max_tokens": 100, "system": "Ты инженер.",
                                                             "messages": [{"role": "user", "content": "Акт: …"}]},
      "5: параметры доходят до SDK без изменений, ответ возвращается как есть")
check(r["provider"] == "anthropic" and r["task_kind"] == "incident" and r["depth"] == "incident" and r["model"] == "claude-x"
      and r["tokens_in"] == 1200 and r["tokens_cache_read"] == 9000 and r["status"] == "ok"
      and r["system_chars"] == len("Ты инженер.") and r["input_chars"] == len("Ты инженер.") + len("Акт: …")
      and r["output_chars"] == len("Разбор: причина — заброс оборотов."), f"5: строка успешного вызова: {r}")
run(usage.anthropic_create(FakeClient(Resp([Blk("text", "анализ"), Blk("tool_use", name="verdict")])), depth="gate_caution",
                           model="m", messages=[{"role": "user", "content": [{"type": "text", "text": "вход"}]}]))
check(ROWS[-1]["tool_calls"] == 1 and ROWS[-1]["task_kind"] == "gate" and ROWS[-1]["input_chars"] == 4, f"5: вызов инструмента, вход списком блоков: {ROWS[-1]['tool_calls']} {ROWS[-1]['input_chars']}")
run(usage.anthropic_create(FakeClient(Resp([])), task="playground", model="m", messages=[]))
check(ROWS[-1]["status"] == "empty" and ROWS[-1]["task_kind"] == "playground", "5: пустой ответ — status empty; задача задана явно")
try:
    run(usage.anthropic_create(FakeClient(RuntimeError("529 overloaded")), depth="humanize", model="m", messages=[]))
    check(False, "5: ошибка SDK пробрасывается")
except RuntimeError as e:
    check("529" in str(e) and ROWS[-1]["status"] == "error" and "529" in ROWS[-1]["error"] and ROWS[-1]["task_kind"] == "humanize",
          f"5: строка ошибки: {ROWS[-1]}")


async def _ctx_claude():
    with usage.call_context(task="seg", ref=36010, depth="seg_warning"):
        await usage.anthropic_create(FakeClient(Resp([Blk("text", "ок")])), model="m", messages=[])


run(_ctx_claude())
check(ROWS[-1]["task_kind"] == "seg" and ROWS[-1]["depth"] == "seg_warning" and ROWS[-1]["task_ref"] == "36010", "5: вид и сегмент из контекста")

# ── 6. Общий клиент ──────────────────────────────────────────────────────────
SSE = ("data: " + json.dumps({"choices": [{"delta": {"content": "При"}}]}) + "\n\n"
       ": ping\n\n"
       "data: " + json.dumps({"choices": [{"delta": {"content": "чина ясна."}}]}) + "\n\n"
       "data: " + json.dumps({"choices": [{"delta": {}}], "usage": {"prompt_tokens": 42000, "completion_tokens": 1500},
                               "gorynych": {"backend": "flashnext", "reasoning_level": "xhigh", "task_id": "task-9", "duration_ms": 61000}}) + "\n\n"
       "data: [DONE]\n\n")
SEEN: list[dict] = []


def handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content or b"{}")
    SEEN.append({"url": str(request.url), "body": body})
    mode = handler.mode
    if mode == "gw_stream":
        return httpx.Response(200, headers={"content-type": "text/event-stream", "x-gorynych-task-id": "task-9"}, content=SSE.encode())
    if mode == "json":
        return httpx.Response(200, json={"choices": [{"message": {"content": "Ответ без потока."}}], "model": "qwen",
                                         "usage": {"prompt_tokens": 800, "completion_tokens": 60}})
    if mode == "empty":
        return httpx.Response(200, json={"choices": [{"message": {"content": ""}}], "usage": {"prompt_tokens": 800, "completion_tokens": 0}})
    if mode == "err400":
        return httpx.Response(400, json={"error": {"message": "вход не влез", "code": "context_length_exceeded", "task_id": "task-4"}})
    if mode == "ollama":
        lines = [json.dumps({"message": {"content": "Привет"}}), json.dumps({"message": {"content": "!"}}),
                 json.dumps({"done": True, "prompt_eval_count": 55, "eval_count": 7})]
        return httpx.Response(200, content=("\n".join(lines) + "\n").encode())
    return httpx.Response(500, json={"error": "?"})


handler.mode = "gw_stream"
llm_client._TRANSPORT = httpx.MockTransport(handler)

# поток шлюза
llm_client.apply_llm_settings("http://gw.test/src/analytics", "fast", 0.1, 16384, True, "gorynych")
ROWS.clear()
meta: dict = {}


async def _gw():
    with usage.call_context(task="incident", ref=35941):
        return await llm_client.chat("Системный промпт", "Вход разбора", depth="incident", meta=meta)


text = run(_gw())
r = ROWS[-1]
check(text == "Причина ясна." and len(ROWS) == 1, f"6: ответ шлюза собран, одна строка журнала: {text!r} {len(ROWS)}")
check(r["provider"] == "gorynych" and r["backend"] == "flashnext" and r["reasoning"] == "xhigh" and r["gateway_task_id"] == "task-9"
      and r["tokens_in"] == 42000 and r["tokens_out"] == 1500 and r["task_kind"] == "incident" and r["task_ref"] == "35941"
      and r["status"] == "ok" and r["output_chars"] == len("Причина ясна.") and r["system_chars"] == len("Системный промпт")
      and r["input_chars"] == len("Системный промпт") + len("Вход разбора") and r["duration_ms"] is not None,
      f"6: строка вызова шлюза: {r}")
check(meta.get("gorynych", {}).get("backend") == "flashnext" and meta.get("task_id") == "task-9" and meta.get("usage", {}).get("prompt_tokens") == 42000,
      f"6: meta вызывающего заполнена как раньше: {meta}")
check(SEEN[-1]["body"].get("priority") and "stream_options" not in SEEN[-1]["body"], "6: шлюзу stream_options не шлём")
# без meta — тоже записывается
ROWS.clear()
run(llm_client.chat("s", "u", depth="humanize"))
check(len(ROWS) == 1 and ROWS[0]["task_kind"] == "humanize" and ROWS[0]["tokens_in"] == 42000, "6: без meta вызывающего расход всё равно в журнале")

# без потока (прямой сервер)
llm_client.apply_llm_settings("http://lm.test", "qwen", 0.1, 16384, False, "lmstudio")
handler.mode = "json"
ROWS.clear()
check(run(llm_client.chat("s", "u")) == "Ответ без потока.", "6: ответ без потока")
check(ROWS[-1]["provider"] == "lmstudio" and ROWS[-1]["tokens_in"] == 800 and ROWS[-1]["tokens_out"] == 60 and ROWS[-1]["backend"] is None
      and ROWS[-1]["task_kind"] == "other" and ROWS[-1]["status"] == "ok", f"6: строка прямого сервера: {ROWS[-1]}")
handler.mode = "empty"
check(run(llm_client.chat("s", "u")) == "" and ROWS[-1]["status"] == "empty" and ROWS[-1]["output_chars"] == 0, "6: пустой ответ — status empty")
handler.mode = "err400"
llm_client.apply_llm_settings("http://gw.test/src/analytics", "fast", 0.1, 16384, True, "gorynych")
ROWS.clear()
try:
    run(llm_client.chat("s", "u", depth="seg_norma"))
    check(False, "6: ошибка 400 пробрасывается")
except llm_client.LLMError as e:
    check(e.code == "context_length_exceeded" and len(ROWS) == 1 and ROWS[0]["status"] == "error"
          and "context_length_exceeded" in ROWS[0]["error"] and ROWS[0]["task_kind"] == "seg" and ROWS[0]["gateway_task_id"] is None,
          f"6: строка ошибки шлюза: {ROWS}")
# Ollama
llm_client.apply_llm_settings("http://ol.test", "qwen2.5:14b", 0.1, 16384, True, "ollama")
handler.mode = "ollama"
ROWS.clear()
check(run(llm_client.chat("s", "u", stream=True)) == "Привет!" and ROWS[-1]["tokens_in"] == 55 and ROWS[-1]["tokens_out"] == 7
      and ROWS[-1]["provider"] == "ollama", f"6: поток Ollama — расход из последней строки: {ROWS[-1]}")
# stream_options — только там, где расход в потоке отдают по просьбе
for prov, want in (("deepseek", True), ("openai", True), ("gorynych", False), ("lmstudio", False)):
    cfg = {"provider": prov, "base_url": "http://x", "model": "m", "temperature": 0.1, "num_ctx": 1, "api_key": "k"}
    _url, payload, _h = llm_client._build_request("s", "u", None, True, cfg)
    check(("stream_options" in payload) == want, f"6: stream_options у {prov}: {payload.get('stream_options')}")
_url, payload, _h = llm_client._build_request("s", "u", None, False, {"provider": "deepseek", "base_url": "http://x", "model": "m",
                                                                      "temperature": 0.1, "num_ctx": 1})
check("stream_options" not in payload, "6: без потока stream_options не нужен")
llm_client._TRANSPORT = None
usage.set_sink(None)

# ── 7. Схема ─────────────────────────────────────────────────────────────────
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sql = open(os.path.join(root, "db", "online_schema.sql"), encoding="utf-8").read()
m = re.search(r"CREATE TABLE IF NOT EXISTS llm_calls \((.*?)\n\);", sql, re.S)
check(m is not None, "7: таблица llm_calls в online_schema.sql")
ddl = m.group(1) if m else ""
check(not re.search(r"\bCHECK\s*\(", ddl, re.I), "7: без CHECK (файл играется при каждом старте)")
missing = [c for c in usage.COLUMNS if not re.search(rf"^\s+{c}\s", ddl, re.M)]
check(not missing, f"7: все колонки модуля есть в DDL: {missing}")
check("input_text" in ddl and "idx_llm_calls_ts" in sql and "idx_llm_calls_run" in sql, "7: колонка полного входа (пока не пишется) и индексы")
main_src = open(os.path.join(root, "main.py"), encoding="utf-8").read()
check("_llm_usage.enable()" in main_src, "7: сервис включает журнал при старте")

if _errors:
    print(f"FAIL — {len(_errors)} расхождений:")
    for x in _errors:
        print(f"  • {x}")
    sys.exit(1)
print("Все проверки пройдены.")
