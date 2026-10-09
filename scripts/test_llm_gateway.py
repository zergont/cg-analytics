# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""Тест клиента моделей и шлюза «Горыныч» на подставном транспорте (v4.9.98).

Живой шлюз не трогаем: он делит видеокарты с советом, ботом и скриптами.
Проверяется:
  - тело запроса к шлюзу: модель/рассуждение/приоритет по градации разбора,
    notes=false, fallback=true, только reasoning_effort, всегда поток;
  - прямые серверы (LM Studio и др.) — поведение прежнее, градация не действует;
  - ошибки шлюза с кодом: событие внутри потока и тело HTTP-ответа →
    LLMError; повтор только при остановке шлюза (stopped/gateway_stopping);
  - метаданные ответа (какая голова ответила, usage) — в подпись и отладку;
  - цепочка разбора: пустой ответ — ошибка и переход к следующей модели;
  - реестр: провайдер gorynych, адрес по умолчанию, лимит параллельности.

Запуск:  py -3 scripts/test_llm_gateway.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import llm.client as client  # noqa: E402
from llm import registry, router  # noqa: E402

_errors: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        _errors.append(msg)


client._RETRY_BASE_DELAY_SEC = 0.0
client._GATEWAY_RETRY_DELAY_SEC = 0.0

GW = registry.normalize_entry({"id": "gw", "name": "Горыныч", "provider": "gorynych",
                               "model": "flashnext", "reasoning": "xhigh"})
LMS = registry.normalize_entry({"id": "lms", "name": "LM Studio", "provider": "lmstudio",
                                "base_url": "http://lms:1234", "model": "qwen/qwen3.8-27b",
                                "reasoning": "off", "stream": False})


class Server:
    """Подставной сервер: по очереди отдаёт заготовленные ответы, пишет запросы."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append({"url": str(request.url), "body": json.loads(request.content or b"{}")})
        status, body, headers = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        return httpx.Response(status, content=body.encode() if isinstance(body, str) else body,
                              headers=headers or {})

    def install(self):
        client._TRANSPORT = httpx.MockTransport(self.handler)
        return self


def sse(*events: dict, ping: bool = True) -> tuple[int, str, dict]:
    lines = [": ping", ""] if ping else []
    for e in events:
        lines += [f"data: {json.dumps(e, ensure_ascii=False)}", ""]
    lines += ["data: [DONE]", ""]
    return 200, "\n".join(lines), {"content-type": "text/event-stream", "x-gorynych-task-id": "t-42"}


def chunk(text: str) -> dict:
    return {"choices": [{"delta": {"content": text}}]}


GORYNYCH_FINAL = {"choices": [{"delta": {}}], "usage": {"prompt_tokens": 900, "completion_tokens": 120,
                                                        "total_tokens": 1020},
                  "gorynych": {"task_id": "t-42", "backend": "flashnext", "reasoning_level": "xhigh",
                               "note": None, "duration_ms": 31000}}


def run(coro):
    return asyncio.run(coro)


# ── 1. Тело запроса к шлюзу по градации ──────────────────────────────────────
router.apply_depth("seg_shutdown", "flashnext", "xhigh", "normal")
router.apply_depth("seg_norma", "fast", "default", "batch")
srv = Server(sse(chunk("Разбор."), GORYNYCH_FINAL)).install()
meta: dict = {}
txt = run(client.chat("SYS", "отчёт", entry=GW, stream=False, depth="seg_shutdown", meta=meta))
b = srv.requests[0]["body"]
check(txt == "Разбор.", f"1a: текст ответа {txt!r}")
check(srv.requests[0]["url"] == "http://192.168.0.79:4000/src/analytics/v1/chat/completions",
      f"1б: адрес {srv.requests[0]['url']}")
check(b["model"] == "flashnext" and b["reasoning_effort"] == "xhigh" and b["priority"] == "normal",
      f"1в: модель/рассуждение/приоритет из градации: {b}")
check(b["stream"] is True, "1г: к шлюзу всегда поток, даже если вызывающий просил без потока")
check(b.get("notes") is False and b.get("fallback") is True, "1д: notes=false и fallback=true")
check("chat_template_kwargs" not in b and "/no_think" not in b["messages"][0]["content"],
      "1е: шлюзу — только reasoning_effort, без chat_template_kwargs и /no_think")
check("temperature" not in b, "1ж: температуру шлюзу не шлём — выбирает он")
check(meta.get("gorynych", {}).get("backend") == "flashnext" and meta.get("usage", {}).get("total_tokens") == 1020,
      f"1з: метаданные ответа не собраны: {meta}")
check(client.gateway_label(meta, "fast") == "flashnext (xhigh)", "1и: подпись по реально ответившей голове")
check(meta.get("task_id") == "t-42", f"1й: task_id шлюза — в meta сразу при приёме: {meta.get('task_id')}")
check(meta.get("requested") == {"model": "flashnext", "reasoning": "xhigh"}, f"1й2: заказ в meta: {meta.get('requested')}")

srv = Server(sse(chunk("ok"))).install()
run(client.chat("SYS", "отчёт", entry=GW, depth="seg_norma"))
b = srv.requests[0]["body"]
check(b["model"] == "fast" and "reasoning_effort" not in b and b["priority"] == "batch",
      f"1к: «по умолчанию» — рассуждение не передаём, решает шлюз: {b}")
check("temperature" not in b, "1к: и при «по умолчанию» температуру выбирает шлюз")

router.apply_depth("humanize", "", "off", "background")
srv = Server(sse(chunk("ok"))).install()
run(client.chat("SYS", "текст", entry=GW, depth="humanize", model="/home/x/Qwen.gguf"))
b = srv.requests[0]["body"]
check(b["model"] == "flashnext", f"1л: пустая модель в градации — модель подключения, не вызывающего: {b['model']}")
check(b["reasoning_effort"] == "none" and "temperature" not in b and b["priority"] == "background",
      f"1м: выкл → none, температуру и без рассуждений выбирает шлюз: {b}")
router.load_depth("")   # вернуть профили по умолчанию

# ── 2. Прямой сервер: градация не действует, поведение прежнее ───────────────
srv = Server((200, json.dumps({"choices": [{"message": {"content": "<think>x</think>ответ"}}]}), None)).install()
txt = run(client.chat("SYS", "отчёт", entry=LMS, depth="seg_shutdown", priority="interactive"))
b = srv.requests[0]["body"]
check(txt == "ответ", f"2a: ответ LM Studio {txt!r}")
check(b["model"] == "qwen/qwen3.8-27b" and b["stream"] is False, "2б: модель и режим записи LM Studio")
check("priority" not in b and "notes" not in b and "fallback" not in b,
      "2в: поля шлюза не должны уходить другим провайдерам")
check(b.get("temperature") == 0.1, "2в: прямому серверу температура уходит, как раньше")
check(b.get("reasoning_effort") == "none" and b.get("chat_template_kwargs") == {"enable_thinking": False}
      and b["messages"][0]["content"].endswith("/no_think"), "2г: выключатель рассуждений LM Studio прежний")

# ── 3. Ошибки шлюза ──────────────────────────────────────────────────────────
srv = Server(sse({"error": {"message": "нет живых голов", "code": "backend_unavailable",
                            "task_id": "t-1"}})).install()
try:
    run(client.chat("SYS", "отчёт", entry=GW))
    check(False, "3a: ошибка внутри потока не превратилась в исключение")
except client.LLMError as e:
    check(e.code == "backend_unavailable" and e.task_id == "t-1" and e.status == 200,
          f"3a: код/task_id ошибки внутри потока: {e}")
check(len(srv.requests) == 1, f"3б: backend_unavailable не повторяют, было {len(srv.requests)} попыток")

srv = Server(sse({"error": {"message": "остановлен", "code": "stopped", "reason": "shutdown"}}),
             sse(chunk("после рестарта"))).install()
txt = run(client.chat("SYS", "отчёт", entry=GW))
check(txt == "после рестарта" and len(srv.requests) == 2, f"3в: stopped повторяется: {txt!r}, {len(srv.requests)}")

# Задачу снял оператор шлюза — не повторять: повтор поставил бы её в очередь снова
for reason in ("operator", "client_disconnected"):
    srv = Server(sse({"error": {"message": "снята", "code": "stopped", "reason": reason, "task_id": "t-5"}}),
                 sse(chunk("не должно быть"))).install()
    try:
        run(client.chat("SYS", "отчёт", entry=GW))
        check(False, f"3в2: снятие ({reason}) не дало исключения")
    except client.LLMError as e:
        check(e.code == "stopped" and e.reason == reason, f"3в2: {e}")
    check(len(srv.requests) == 1, f"3в3: снятие оператором ({reason}) не повторяется: {len(srv.requests)}")
srv = Server(sse({"error": {"message": "отменён", "code": "council_cancelled"}}), sse(chunk("x"))).install()
try:
    run(client.chat("SYS", "отчёт", entry=GW))
except client.LLMError:
    pass
check(len(srv.requests) == 1, "3в4: council_cancelled не повторяется")

srv = Server((503, json.dumps({"error": {"message": "занято", "code": "backend_unavailable",
                                         "task_id": "t-9"}}), {"content-type": "application/json"})).install()
try:
    run(client.chat("SYS", "отчёт", entry=GW))
    check(False, "3г: HTTP 503 с телом шлюза не дал исключения")
except client.LLMError as e:
    check(e.status == 503 and e.code == "backend_unavailable" and e.task_id == "t-9",
          f"3г: код из тела HTTP-ответа (поток): {e}")
check(len(srv.requests) == 1, "3д: 503 backend_unavailable не повторяют")

srv = Server((400, json.dumps({"error": {"message": "не влезло", "code": "context_length_exceeded",
                                         "tokens_estimated": 300000, "ctx": 262144}}),
              {"content-type": "application/json"})).install()
try:
    run(client.chat("SYS", "отчёт", entry=GW))
    check(False, "3е: context_length_exceeded не дал исключения")
except client.LLMError as e:
    check(e.status == 400 and e.code == "context_length_exceeded", f"3е: {e}")

srv = Server((500, "Internal Server Error", None), (200, json.dumps(
    {"choices": [{"message": {"content": "ok"}}]}), None)).install()
txt = run(client.chat("SYS", "отчёт", entry=LMS))
check(txt == "ok" and len(srv.requests) == 2, "3ж: обычный 500 прямого сервера повторяется, как раньше")

# llama-server: тело ошибки с кодом-числом — для прямого сервера решает статус
srv = Server((503, json.dumps({"error": {"code": 503, "message": "Loading model",
                                         "type": "unavailable_error"}}), {"content-type": "application/json"}),
             (200, json.dumps({"choices": [{"message": {"content": "ok"}}]}), None)).install()
txt = run(client.chat("SYS", "отчёт", entry=LMS))
check(txt == "ok" and len(srv.requests) == 2,
      f"3з: 503 прямого сервера с кодом в теле повторяется: {txt!r}, {len(srv.requests)}")
check(not client.retriable_llm_error(client.LLMError(503, "backend_unavailable", "x", gateway=True))
      and client.retriable_llm_error(client.LLMError(503, "backend_unavailable", "x")),
      "3и: политика по коду — только для ошибок шлюза")

# После остановки шлюза длинная пауза держится и на отказе соединения
_delays: list[float] = []
_real_sleep = client.asyncio.sleep


async def _fake_sleep(sec: float) -> None:
    _delays.append(sec)


client.asyncio.sleep = _fake_sleep
client._RETRY_BASE_DELAY_SEC, client._GATEWAY_RETRY_DELAY_SEC = 2.0, 15.0
_calls = {"n": 0}


def _restarting(request: httpx.Request) -> httpx.Response:
    _calls["n"] += 1
    if _calls["n"] == 1:
        status, body, headers = sse({"error": {"message": "остановлен", "code": "gateway_stopping"}})
        return httpx.Response(status, content=body.encode(), headers=headers)
    if _calls["n"] == 2:
        raise httpx.ConnectError("connection refused", request=request)
    status, body, headers = sse(chunk("поднялся"))
    return httpx.Response(status, content=body.encode(), headers=headers)


client._TRANSPORT = httpx.MockTransport(_restarting)
txt = run(client.chat("SYS", "отчёт", entry=GW))
check(txt == "поднялся" and _delays == [15.0, 30.0],
      f"3к: пауза шлюза держится после остановки: {txt!r}, паузы {_delays}")

_delays.clear()
_calls["n"] = 1          # сразу отказ соединения, без остановки шлюза
client._TRANSPORT = httpx.MockTransport(_restarting)
run(client.chat("SYS", "отчёт", entry=GW))
check(_delays == [2.0], f"3л: обычный отказ соединения — короткая пауза: {_delays}")
client.asyncio.sleep = _real_sleep
client._RETRY_BASE_DELAY_SEC, client._GATEWAY_RETRY_DELAY_SEC = 0.0, 0.0

srv = Server((302, "", {"location": "http://elsewhere/"})).install()
try:
    run(client.chat("SYS", "отчёт", entry=LMS))
    check(False, "3м: ответ 3xx принят за успех")
except Exception as e:
    check(not isinstance(e, AssertionError), f"3м: {e!r}")

# ── 4. Реестр и общие настройки ──────────────────────────────────────────────
e = registry.normalize_entry({"name": "gw", "provider": "gorynych", "model": "fast",
                              "base_url": "http://gw:4000/src/analytics/v1/", "max_concurrent": 8,
                              "stream": False})
check(e["base_url"] == "http://gw:4000/src/analytics", f"4a: хвост /v1 не срезан: {e['base_url']}")
check(e["max_concurrent"] == 3 and e["stream"] is True and e["max_ctx_tokens"] == 262144,
      f"4б: потолок 3, поток всегда, окно шлюза: {e}")
check(registry.normalize_entry({"name": "g", "provider": "gorynych"})["max_concurrent"] == 2
      and GW["base_url"] == registry.GORYNYCH_DEFAULT_URL, "4в: умолчания шлюза")

saved = client.get_llm_settings()
client.apply_llm_settings("http://gw:4000/src/analytics/v1", "fast", 0.1, 16384, stream=False,
                          provider="gorynych")
srv = Server(sse(chunk("гейт"), GORYNYCH_FINAL)).install()
from online.manager import _run_warning_gate_llm  # noqa: E402
gm: dict = {}
analysis, decision, reason, t_in, t_out = run(
    _run_warning_gate_llm("промпт", "/home/folist/models/Qwen3.8-27B-Q8_0.gguf",
                          depth=router.depth_for_gate("предупреждение"), meta=gm))
b = srv.requests[0]["body"]
check(srv.requests[0]["url"] == "http://gw:4000/src/analytics/v1/chat/completions", "4г: адрес шлюза из общих настроек")
check(b["model"] == "fast" and b["priority"] == "interactive" and b["stream"] is True,
      f"4д: гейт — модель градации, а не путь к gguf из маршрута уровня: {b}")
check(t_in == 900 and t_out == 120 and gm.get("gorynych"), "4е: токены гейта из usage")
client.apply_llm_settings(saved["base_url"], saved["model"], saved["temperature"], saved["num_ctx"],
                          stream=saved["stream"], provider=saved["provider"])

# ── 5. Цепочка разбора сегмента ──────────────────────────────────────────────
from corpus import worker  # noqa: E402

registry.load_registry(json.dumps([GW, LMS]))


def routed(request: httpx.Request) -> httpx.Response:
    if "gorynych" in request.url.path or "src/analytics" in request.url.path:
        return httpx.Response(200, content=sse(chunk(""))[1].encode(),
                              headers={"content-type": "text/event-stream"})
    return httpx.Response(200, content=json.dumps({"choices": [{"message": {"content": "заключение"}}]}).encode())


client._TRANSPORT = httpx.MockTransport(routed)
res = run(worker._analyse_segment_chain({"id": 1, "report_md": "отчёт"}, None, "SYS", [GW, LMS],
                                        depth="seg_norma"))
trace = res["debug_json"]["routing"]
check(trace[0]["entry"] == "gw" and trace[0]["action"] == "error", f"5a: пустой ответ шлюза — ошибка: {trace}")
check(trace[1] == {"entry": "lms", "action": "ok"} and res["conclusion_md"].startswith("заключение"),
      f"5б: цепочка ушла к следующей модели: {trace}")

srv = Server(sse(chunk("## Сводка\nвсё штатно"), GORYNYCH_FINAL)).install()
res = run(worker._analyse_segment_chain({"id": 2, "report_md": "отчёт"}, None, "SYS", [GW],
                                        depth="seg_shutdown"))
check(res["claude_model"] == "Горыныч: flashnext (xhigh)" and res["tokens_used"] == 1020,
      f"5в: подпись и токены по реально ответившей голове: {res['claude_model']}, {res['tokens_used']}")
check(res["debug_json"].get("gorynych", {}).get("task_id") == "t-42" and res["debug_json"]["depth"] == "seg_shutdown",
      "5г: task_id шлюза и вид разбора — в debug_json")
check("*Модель: flashnext (xhigh)" in res["conclusion_md"], "5д: префикс подписи «*Модель:» сохранён")

srv = Server(sse({"error": {"message": "x", "code": "backend_unavailable", "task_id": "t-7"}})).install()
res = run(worker._analyse_segment_chain({"id": 3, "report_md": "отчёт"}, None, "SYS", [GW]))
rec = res["debug_json"]["routing"][0]
check(rec.get("code") == "backend_unavailable" and rec.get("task_id") == "t-7",
      f"5е: код и task_id ошибки шлюза — отдельными полями следа: {rec}")

# ── 6. Вид разбора по сегменту, профили ──────────────────────────────────────
check(router.depth_for_segment("НОРМА") == "seg_norma" and router.depth_for_segment("SHUTDOWN") == "seg_shutdown"
      and router.depth_for_segment(None) == "seg_caution", "6a: уровень тревоги → вид разбора")
check(worker._segment_depth({"characteristics_json": {"stop_kind": "EMERGENCY"}}, "seg_auto") == "seg_shutdown",
      "6б: аварийный стоп — разбор аварии")
check(worker._segment_depth({"characteristics_json": {}}, "seg_manual") == "manual", "6в: ручной запуск")
router.load_depth(json.dumps({"seg_norma": {"model": "gemma", "reasoning": "nonsense", "priority": "urgent"}}))
p = router.get_depth("seg_norma")
check(p == {"model": "gemma", "reasoning": "default", "priority": "batch"},
      f"6г: неизвестные значения из настроек заменяются умолчаниями: {p}")
check(router.get_depth("gate_caution")["model"] == "fast", "6д: виды без настроек — по умолчанию fast")
check(all(v["model"] == "fast" for k, v in json.loads(router.serialize_depth()).items() if k != "seg_norma"),
      "6е: по умолчанию всем fast")

client._TRANSPORT = None
if _errors:
    print(f"FAIL — {len(_errors)} расхождений:")
    for e in _errors:
        print(f"  • {e}")
    sys.exit(1)
print("Все проверки пройдены.")
