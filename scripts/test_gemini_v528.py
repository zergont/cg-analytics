"""v5.2.8 «Провайдер Gemini» (решение владельца 13.11.2026: четвёртый облачный провайдер к
Claude, OpenAI и DeepSeek). Gemini идёт через точку OpenAI-формата Google
(…/v1beta/openai/chat/completions) общим клиентом. Без сети: HTTP — httpx.MockTransport.

Раздел 1 — реестр: тип, адрес по умолчанию (вместе с путём версии), адрес без пути
дополняется, облачный провайдер, признаки «инструменты» и «зрение» по умолчанию, лимит
контекста; прежние провайдеры не задеты.
Раздел 2 — запрос: адрес без /v1, ключ, температура не шлётся, уровни рассуждения, расход в
потоке; у остальных провайдеров адрес прежний.
Раздел 3 — список моделей и «Проверить»: адрес, приставка «models/» снимается.
Раздел 4 — вызов через общий клиент и журнал: ответ, строка llm_calls, расход и стоимость.
Раздел 5 — форма настроек: провайдер в списке, умолчания признаков.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

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

from llm import client as llm_client
from llm import registry, usage

ROWS: list[dict] = []


async def sink(row: dict):
    ROWS.append(dict(row))
    return len(ROWS)


usage.set_sink(sink)


def run(coro):
    return asyncio.run(coro)


GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai"

# ── 1. Реестр ────────────────────────────────────────────────────────────────
print("1. Реестр: провайдер Gemini")

e = registry.normalize_entry({"name": "Gemini", "provider": "gemini", "model": "gemini-x", "api_key": "g-key",
                              "price_in": 1.5, "price_out": 9, "price_cache_read": 0.15})
check(e["provider"] == "gemini" and e["type"] == "llm" and e["id"] == "gemini", "запись Gemini — тип llm")
check(e["base_url"] == GEMINI_URL == registry.GEMINI_DEFAULT_URL, f"адрес по умолчанию: {e['base_url']}")
check(e["tools"] is True and e["vision"] is True, "инструменты и зрение включены по умолчанию")
check(e["max_ctx_tokens"] == registry.DEFAULT_MAX_CTX["gemini"] == 1_000_000, "лимит контекста по умолчанию")
check("gemini" in registry.CLOUD_PROVIDERS and "gemini" in registry.ENTRY_PROVIDERS, "облачный провайдер")
for raw, want in (("https://generativelanguage.googleapis.com", GEMINI_URL),
                  ("https://generativelanguage.googleapis.com/", GEMINI_URL),
                  (GEMINI_URL + "/", GEMINI_URL),
                  ("http://relay.local:8443", "http://relay.local:8443/v1beta/openai"),
                  ("http://relay.local:8443/custom/openai", "http://relay.local:8443/custom/openai")):
    got = registry.normalize_entry({"name": "g", "provider": "gemini", "model": "m", "base_url": raw})["base_url"]
    check(got == want, f"адрес {raw!r} → {got!r}")
check(registry.normalize_entry({"name": "g", "provider": "gemini", "model": "m", "vision": False})["vision"] is False,
      "зрение у Gemini можно снять")
old = {p: registry.normalize_entry({"name": p, "provider": p, "model": "m"}) for p in registry.ENTRY_PROVIDERS}
check(old["openai"]["base_url"] == "https://api.openai.com" and old["openai"]["vision"] is False
      and old["anthropic"]["vision"] is True and old["deepseek"]["vision"] is False
      and old["lmstudio"]["vision"] is False,
      "прежние провайдеры: адреса и признаки не изменились")
# v5.2.10: у записей шлюза зрение включено по умолчанию; явный отказ сохраняется
check(old["gorynych"]["vision"] is True, "шлюз: зрение по умолчанию включено")
check(registry.normalize_entry({"name": "gw", "provider": "gorynych", "model": "fast", "vision": False})["vision"] is False,
      "шлюз: снятый флажок сохраняется")

# ── 2. Запрос ────────────────────────────────────────────────────────────────
print("2. Запрос к Gemini")

url, payload, headers = llm_client._build_request("сис", "вопрос", None, True, e)
check(url == GEMINI_URL + "/chat/completions", f"адрес запроса без /v1: {url}")
check(headers.get("Authorization") == "Bearer g-key", "ключ в заголовке")
check("temperature" not in payload, "температура не шлётся")
check(payload.get("stream_options") == {"include_usage": True}, "расход в потоке запрошен")
check("reasoning_effort" not in payload, "рассуждение по умолчанию — параметр не шлётся")
check(payload["model"] == "gemini-x" and payload["messages"][0] == {"role": "system", "content": "сис"},
      "модель и системный промпт без примесей")
check(not any(k in payload for k in ("priority", "notes", "fallback", "thinking", "chat_template_kwargs")),
      "полей других провайдеров нет")
for level, want in (("off", "low"), ("low", "low"), ("medium", "medium"), ("high", "high"), ("xhigh", "high")):
    _, p, _ = llm_client._build_request("сис", "вопрос", None, False, e, reasoning=level)
    check(p.get("reasoning_effort") == want, f"рассуждение {level} → {want}: {p.get('reasoning_effort')}")
    check("/no_think" not in p["messages"][0]["content"] and "stream_options" not in p, f"{level}: без примесей")

for prov, base, want in (("openai", "https://api.openai.com", "https://api.openai.com/v1/chat/completions"),
                         ("deepseek", "https://api.deepseek.com", "https://api.deepseek.com/v1/chat/completions"),
                         ("gorynych", "http://gw:4000/src/analytics", "http://gw:4000/src/analytics/v1/chat/completions"),
                         ("lmstudio", "http://h:1234", "http://h:1234/v1/chat/completions")):
    cfg = registry.normalize_entry({"name": prov, "provider": prov, "model": "m", "base_url": base})
    check(llm_client._build_request("s", "u", None, False, cfg)[0] == want, f"{prov}: адрес прежний")
cfg = registry.normalize_entry({"name": "o", "provider": "ollama", "model": "m", "base_url": "http://h:11434"})
check(llm_client._build_request("s", "u", None, False, cfg)[0] == "http://h:11434/api/chat", "ollama: адрес прежний")

# ── 3. Список моделей ────────────────────────────────────────────────────────
print("3. Список моделей и «Проверить»")

SEEN: list[httpx.Request] = []
REPLY: dict = {}


def handler(request: httpx.Request) -> httpx.Response:
    SEEN.append(request)
    if request.url.path.endswith("/models"):
        return httpx.Response(200, json={"data": [{"id": "models/gemini-x"}, {"id": "models/gemini-y"}, {"id": "plain"}]})
    if request.url.path.endswith("/chat/completions"):
        return httpx.Response(200, json=REPLY)
    return httpx.Response(404)


_tr = llm_client._TRANSPORT
llm_client._TRANSPORT = httpx.MockTransport(handler)
try:
    models = run(llm_client.list_server_models("gemini", GEMINI_URL, "g-key", proxy="http://p:1"))
    check(models == ["gemini-x", "gemini-y", "plain"], f"приставка «models/» снята: {models}")
    check(str(SEEN[-1].url) == GEMINI_URL + "/models" and SEEN[-1].headers.get("authorization") == "Bearer g-key",
          f"адрес списка моделей: {SEEN[-1].url}")
    ping = run(llm_client.ping_entry(e))
    check(ping["ok"] and "моделей: 3" in ping["detail"], f"«Проверить» находит модель записи: {ping}")
    run(llm_client.list_server_models("openai", "https://api.openai.com", "k"))
    check(str(SEEN[-1].url) == "https://api.openai.com/v1/models", "OpenAI: адрес списка прежний")

    # ── 4. Вызов и журнал ────────────────────────────────────────────────────
    print("4. Вызов через общий клиент, журнал и стоимость")
    REPLY.update({"model": "gemini-x", "choices": [{"message": {"role": "assistant", "content": "Ответ Gemini."},
                                                    "finish_reason": "stop"}],
                  "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 100_000,
                            "prompt_tokens_details": {"cached_tokens": 400_000},
                            "completion_tokens_details": {"reasoning_tokens": 60_000}}})
    ROWS.clear(); SEEN.clear()
    text = run(llm_client.chat("сис", "вопрос", entry=e, stream=False))
    check(text == "Ответ Gemini.", f"ответ: {text!r}")
    check(str(SEEN[-1].url) == GEMINI_URL + "/chat/completions", "вызов ушёл на точку Gemini")
    body = json.loads(SEEN[-1].content)
    check(body["stream"] is False and "temperature" not in body, "тело запроса")
    check(len(ROWS) == 1 and ROWS[0]["provider"] == "gemini" and ROWS[0]["entry_id"] == "gemini"
          and ROWS[0]["status"] == "ok", f"строка журнала: {ROWS[:1]}")
    r = ROWS[0]
    check(r["tokens_in"] == 600_000 and r["tokens_cache_read"] == 400_000 and r["tokens_out"] == 100_000
          and r["tokens_reasoning"] == 60_000, f"расход: вход без кэша отдельно: {r}")
    want = 0.6 * 1.5 + 0.4 * 0.15 + 0.1 * 9
    check(r["cost_usd"] is not None and abs(r["cost_usd"] - want) < 1e-6, f"стоимость по ценам записи: {r['cost_usd']} (ждали {want})")
finally:
    llm_client._TRANSPORT = _tr

# ── 5. Форма настроек ────────────────────────────────────────────────────────
print("5. Форма настроек")

with open(os.path.join(ROOT, "web", "templates", "settings.html"), encoding="utf-8") as f:
    tpl = f.read()
check('<option value="gemini">Gemini (Google)</option>' in tpl, "провайдер в списке формы")
check("gemini: 1000000" in tpl and 'LLM_REG_VISION_DEFAULT = ["anthropic", "gemini", "gorynych"]' in tpl
      and '"gemini", "gorynych"]' in tpl, "умолчания формы: лимит, зрение, инструменты")
check("generativelanguage.googleapis.com/v1beta/openai" in tpl, "адрес по умолчанию в форме")
with open(os.path.join(ROOT, "web", "routes.py"), encoding="utf-8") as f:
    check('if provider == "gemini":' in f.read(), "«Модели сервера» знают адрес Gemini")

usage.set_sink(None)
print()
if _errors:
    print(f"Не пройдено проверок: {len(_errors)}")
    for msg in _errors:
        print("  -", msg)
    sys.exit(1)
print("Все проверки пройдены.")
