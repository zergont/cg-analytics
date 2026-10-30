"""Проверки v5.1.28 «Предохранитель цепочки» (без сети: шлюз подменён).

- 1. Предел входа шлюза: оценка выше _GW_MAX_PROMPT_TOKENS — запрос не уходит,
  след «skip_size» с пределом 150 000, а не паспортом окна 262 144 (сутки
  ДЭС №3 28.10: ~262 тыс. по оценке прошли под паспортом с запасом 67 токенов).
  У записей не шлюза предел прежний — паспорт.
- 2. Срок ответа шлюза: поток без ответа дольше _GW_DEADLINE_SEC — ошибка
  «шлюз не ответил за N мин» с task_id, цепочка уходит к следующей модели.
- 3. Обычный ответ шлюза в срок — как раньше.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

import llm.client as client  # noqa: E402
from corpus import worker  # noqa: E402
from llm import registry  # noqa: E402

_errors: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        _errors.append(msg)


def run(coro):
    return asyncio.run(coro)


GW = registry.normalize_entry({"id": "gw", "name": "Горыныч", "provider": "gorynych", "model": "fast"})
LMS = registry.normalize_entry({"id": "lms", "name": "LM Studio", "provider": "lmstudio",
                                "base_url": "http://lms:1234", "model": "qwen",
                                "max_ctx_tokens": 1_000_000, "stream": False})
registry.load_registry(json.dumps([GW, LMS]))
check(GW["max_ctx_tokens"] == 262144 and worker._entry_ctx_limit(GW) == 150_000
      and worker._entry_ctx_limit(LMS) == 1_000_000,
      f"0: предел шлюза 150 000 при паспорте 262 144, у других — паспорт: {worker._entry_ctx_limit(GW)}")

_hits: list[str] = []


def _final(text: str) -> bytes:
    """Ответ шлюза потоком: кусок текста и финал с метаданными."""
    lines = [
        "data: " + json.dumps({"choices": [{"delta": {"content": text}}]}),
        "data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}],
                               "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}),
        "data: [DONE]",
    ]
    return ("\n\n".join(lines) + "\n\n").encode()


def _lms_ok() -> httpx.Response:
    return httpx.Response(200, content=json.dumps({"choices": [{"message": {"content": "заключение lms"}}]}).encode())


# ══ 1. Предел входа шлюза ════════════════════════════════════════════════════
def handler_size(request: httpx.Request) -> httpx.Response:
    _hits.append(request.url.host)
    return _lms_ok()

client._TRANSPORT = httpx.MockTransport(handler_size)
_big = "т" * int((worker._GW_MAX_PROMPT_TOKENS + 5_000) * worker._EST_CHARS_PER_TOKEN)
res = run(worker._analyse_segment_chain({"id": 36178, "report_md": _big}, None, "SYS", [GW, LMS]))
tr = res["debug_json"]["routing"]
check(tr[0]["entry"] == "gw" and tr[0]["action"] == "skip_size" and "лимита 150000" in tr[0]["detail"],
      f"1а: шлюз пропущен по пределу 150 000 без запроса: {tr}")
check(tr[1] == {"entry": "lms", "action": "ok"} and res["conclusion_md"].startswith("заключение lms")
      and "gw" not in _hits and _hits == ["lms"],
      f"1б: запрос ушёл только следующей модели: {tr} {_hits}")
_hits.clear()
_mid = "т" * int((worker._GW_MAX_PROMPT_TOKENS - 10_000) * worker._EST_CHARS_PER_TOKEN)
res = run(worker._analyse_segment_chain({"id": 2, "report_md": _mid}, None, "SYS", [LMS]))
check(res["debug_json"]["routing"] == [{"entry": "lms", "action": "ok"}],
      "1в: у записи не шлюза предел прежний")

# ══ 2. Срок ответа шлюза ═════════════════════════════════════════════════════
class _Hang(httpx.AsyncByteStream):
    """Поток шлюза: задача принята, дальше только ожидание (как пинги без ответа)."""

    async def __aiter__(self):
        await asyncio.sleep(30)
        yield b""


def handler_hang(request: httpx.Request) -> httpx.Response:
    _hits.append(request.url.host)
    if request.url.host == "lms":
        return _lms_ok()
    return httpx.Response(200, stream=_Hang(), headers={"content-type": "text/event-stream",
                                                         "x-gorynych-task-id": "v-hang-1"})

_hits.clear()
_saved_deadline = worker._GW_DEADLINE_SEC
worker._GW_DEADLINE_SEC = 0.3
client._TRANSPORT = httpx.MockTransport(handler_hang)
try:
    res = run(worker._analyse_segment_chain({"id": 3, "report_md": "отчёт"}, None, "SYS", [GW, LMS]))
finally:
    worker._GW_DEADLINE_SEC = _saved_deadline
tr = res["debug_json"]["routing"]
check(tr[0]["entry"] == "gw" and tr[0]["action"] == "error" and "не ответил" in tr[0]["detail"]
      and "v-hang-1" in tr[0]["detail"],
      f"2а: срок истёк — ошибка с номером задачи шлюза: {tr}")
check(tr[1] == {"entry": "lms", "action": "ok"} and res["conclusion_md"].startswith("заключение lms"),
      f"2б: цепочка ушла к следующей модели: {tr}")

# ══ 3. Ответ шлюза в срок ════════════════════════════════════════════════════
def handler_ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=_final("## Сводка\nвсё штатно"),
                          headers={"content-type": "text/event-stream", "x-gorynych-task-id": "v-ok-1"})

client._TRANSPORT = httpx.MockTransport(handler_ok)
res = run(worker._analyse_segment_chain({"id": 4, "report_md": "отчёт"}, None, "SYS", [GW]))
check(res["debug_json"]["routing"] == [{"entry": "gw", "action": "ok"}]
      and res["conclusion_md"].startswith("## Сводка"),
      f"3: ответ шлюза в срок — как раньше: {res['debug_json']['routing']} {res['conclusion_md'][:40]!r}")
client._TRANSPORT = None

if _errors:
    print(f"FAIL — {len(_errors)} расхождений:")
    for x in _errors:
        print(f"  • {x}")
    sys.exit(1)
print("Все проверки пройдены.")
