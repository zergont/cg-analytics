"""v5.2.7 «Реестр, строка таблицы и расход ИИ» (MCP Э2б, шаг 2; docs/plans/design_mcp_e2.md,
решения Р12, Р19, Р21, Р22, Р23, разд. 6). Без БД и без сети: HTTP — httpx.MockTransport.

Раздел 1 — реестр: провайдер openai (тип, адрес по умолчанию, хвост /v1), цены (число,
запятая, пусто, мусор, минус), признаки «инструменты» и «зрение» (умолчания по
провайдеру, явный отказ сохраняется), прокси; сохранение и загрузка; запись старого
формата получает умолчания; стоимость считается по ценам записи.
Раздел 2 — общий клиент: запрос к OpenAI (адрес, ключ, без температуры, уровни
рассуждения, расход в потоке), список моделей; прокси записи доходит до клиента.
Раздел 3 — строка таблицы: новые поля и их умолчания, подключение и инструменты только у
разбора аварии, пределы бюджетов, «не передано — оставить», форма (флажок с отметкой
присутствия, старая вкладка ничего не сбрасывает), сохранение и загрузка, старый ключ.
Раздел 4 — помощники строки: запись подключения (только облачная и существующая),
модель вызова, бюджеты (строка → config.yml → код), подпись.
Раздел 5 — расход: тексты запросов, граница хранения, итоги (NULL не становится нулём),
доля кэша.
Раздел 6 — схема, планировщик, шаблоны: таблица суточных итогов без CHECK, задание
свёртки, поля формы реестра, карточка расхода подключена.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone

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
from llm import registry, router, usage
from db import llm_usage


def run(coro):
    return asyncio.run(coro)


def read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


# ── 1. Реестр ────────────────────────────────────────────────────────────────
print("1. Реестр: OpenAI, цены, признаки, прокси")

e = registry.normalize_entry({"name": "GPT", "provider": "openai", "model": "gpt-x",
                              "api_key": " k ", "price_in": "1,25", "price_out": 10,
                              "price_cache_read": "", "price_cache_write": None,
                              "proxy": "  http://proxy:3128  "})
check(e["provider"] == "openai" and e["type"] == "llm", "openai — тип llm")
check(e["base_url"] == registry.OPENAI_DEFAULT_URL, "openai: адрес по умолчанию")
check(e["price_in"] == 1.25 and e["price_out"] == 10.0, "цены: запятая и число")
check(e["price_cache_read"] is None and e["price_cache_write"] is None, "пустая цена — None")
check(e["proxy"] == "http://proxy:3128", "прокси без пробелов")
check(e["tools"] is True and e["vision"] is False, "openai: инструменты да, зрение нет по умолчанию")
check(e["max_ctx_tokens"] == registry.DEFAULT_MAX_CTX["openai"], "openai: лимит контекста по умолчанию")

e2 = registry.normalize_entry({"name": "GPT2", "provider": "openai", "model": "m",
                               "base_url": "https://api.openai.com/v1/"})
check(e2["base_url"] == "https://api.openai.com", "openai: хвост /v1 убран")

for bad in ("abc", "-1", -0.5, True, [], "1e"):
    check(registry.normalize_entry({"name": "x", "provider": "deepseek", "model": "m",
                                    "price_in": bad})["price_in"] is None,
          f"цена {bad!r} — None")
check(registry.normalize_entry({"name": "x", "provider": "deepseek", "model": "m",
                                "price_in": 0})["price_in"] == 0.0, "цена 0 — это 0, не None")

defaults = {p: registry.normalize_entry({"name": p, "provider": p, "model": "m"})
            for p in registry.ENTRY_PROVIDERS}
check(all(defaults[p]["tools"] for p in (*registry.CLOUD_PROVIDERS, "gorynych")),
      "инструменты по умолчанию: облако и шлюз")
check(not any(defaults[p]["tools"] for p in ("ollama", "lmstudio", "llamacpp")),
      "инструменты по умолчанию: локальные — нет")
# v5.2.8: к Claude добавился Gemini (четвёртый облачный провайдер, тоже со зрением)
check(defaults["anthropic"]["vision"] and not any(
    defaults[p]["vision"] for p in registry.ENTRY_PROVIDERS if p not in registry.VISION_BY_DEFAULT),
      "зрение по умолчанию — только у провайдеров из VISION_BY_DEFAULT")
check({"anthropic", "openai", "deepseek"} <= set(registry.CLOUD_PROVIDERS), "облачные провайдеры")

off = registry.normalize_entry({"name": "c", "provider": "anthropic", "model": "m",
                                "tools": False, "vision": False})
check(off["tools"] is False and off["vision"] is False, "явный отказ от признаков сохраняется")
on = registry.normalize_entry({"name": "d", "provider": "deepseek", "model": "m",
                               "vision": "on", "tools": "false"})
check(on["vision"] is True and on["tools"] is False, "признаки строкой из формы")

# сохранение и загрузка
_saved = registry.serialize()
try:
    registry.load_registry(json.dumps([
        {"id": "old", "name": "Старая", "provider": "deepseek", "model": "deepseek-chat",
         "base_url": "https://api.deepseek.com", "api_key": "k"},
    ]))
    old = registry.get_entry("old")
    check(old is not None and old["price_in"] is None and old["tools"] is True
          and old["vision"] is False and old["proxy"] == "",
          "запись старого формата получает умолчания")
    registry.upsert_entry({"id": "gpt", "name": "GPT", "provider": "openai", "model": "gpt-x",
                           "api_key": "k", "price_in": 2, "price_out": 8,
                           "price_cache_read": 0.2, "vision": True, "proxy": "http://p:1"})
    registry.upsert_entry({"id": "claude", "name": "Claude", "provider": "anthropic",
                           "model": "claude-x", "price_in": 3, "price_out": 15})
    registry.load_registry(registry.serialize())
    g = registry.get_entry("gpt")
    check(g["price_in"] == 2.0 and g["price_out"] == 8.0 and g["price_cache_read"] == 0.2
          and g["price_cache_write"] is None and g["vision"] is True and g["proxy"] == "http://p:1",
          "цены, зрение и прокси переживают сохранение")

    # стоимость по ценам записи
    u = usage.normalize_usage("openai", {"prompt_tokens": 1_000_000, "completion_tokens": 500_000,
                                         "prompt_tokens_details": {"cached_tokens": 400_000}})
    cost = usage.cost_usd(g, u)
    # вход без кэша 600 тыс. × 2 + кэш 400 тыс. × 0,2 + выход 500 тыс. × 8
    check(cost is not None and abs(cost - (0.6 * 2 + 0.4 * 0.2 + 0.5 * 8)) < 1e-6,
          f"стоимость по ценам записи: {cost}")
    check(usage.cost_usd(old, u) is None, "без цен стоимости нет")

    # ── 3–4. Строка таблицы (нужен реестр с облачными записями) ──────────────
    print("3. Строка таблицы: подключение, инструменты, бюджеты")
    _depth_saved = router.serialize_depth()
    try:
        d = router.get_depth("incident")
        for k in ("api_entry", "tools", *router.BUDGET_KEYS):
            check(k in d, f"поле {k} есть в строке")
        check(router.API_ENTRY_KINDS == {"incident"} and router.TOOL_KINDS == {"incident"},
              "подключение и инструменты — только у разбора аварии")

        router.apply_depth("incident", "smart", "xhigh", "batch", provider="api",
                           claude_model="", api_entry="gpt", tools="telemetry",
                           budgets={"budget_calls": "9", "budget_tokens": "", "budget_minutes": 15})
        d = router.get_depth("incident")
        check(d["api_entry"] == "gpt" and d["tools"] == "telemetry", "подключение и инструменты записаны")
        check(d["budget_calls"] == 9 and d["budget_tokens"] is None and d["budget_minutes"] == 15,
              "бюджеты: число, пусто — умолчание")

        router.apply_depth("incident", "smart", "xhigh", "batch")
        d = router.get_depth("incident")
        check(d["api_entry"] == "gpt" and d["tools"] == "telemetry" and d["budget_calls"] == 9,
              "не передано — остаётся прежним")

        router.apply_depth("incident", "smart", "xhigh", "batch", budgets={"budget_calls": 500})
        check(router.get_depth("incident")["budget_calls"] == 60, "бюджет вызовов — не больше 60")
        router.apply_depth("incident", "smart", "xhigh", "batch",
                           budgets={"budget_tokens": 5, "budget_minutes": 0})
        d = router.get_depth("incident")
        check(d["budget_tokens"] == 10_000 and d["budget_minutes"] == 1, "бюджеты — не меньше нижней границы")
        check(d["budget_calls"] == 60, "непереданный бюджет не трогается")
        router.apply_depth("incident", "smart", "xhigh", "batch", tools="всё подряд")
        check(router.get_depth("incident")["tools"] == "", "неизвестный набор инструментов — выключено")

        for kind in router.DEPTH_KINDS:
            if kind == "incident":
                continue
            cur = router.get_depth(kind)
            router.apply_depth(kind, cur["model"], cur["reasoning"], cur["priority"],
                               api_entry="gpt", tools="telemetry", budgets={"budget_calls": 5})
            got = router.get_depth(kind)
            check(got["api_entry"] == "" and got["tools"] == "" and got["budget_calls"] is None,
                  f"{kind}: чужое подключение и инструменты не принимаются")
            check(router.api_entry_for(kind) is None and not router.tools_enabled(kind),
                  f"{kind}: помощники отвечают «нет»")

        # форма
        router.apply_depth("incident", "smart", "xhigh", "batch", provider="api",
                           api_entry="", tools="", budgets={k: "" for k in router.BUDGET_KEYS})
        router.apply_route_form({
            "route_incident_provider": "api", "route_incident_claude_model": "",
            "depth_incident_model": "smart", "depth_incident_reasoning": "xhigh",
            "depth_incident_priority": "batch",
            "route_incident_api_entry": "gpt",
            "route_incident_tools_present": "1", "route_incident_tools": "on",
            "route_incident_budget_calls": "7", "route_incident_budget_tokens": "120000",
            "route_incident_budget_minutes": "",
        })
        d = router.get_depth("incident")
        check(d["api_entry"] == "gpt" and d["tools"] == "telemetry" and d["budget_calls"] == 7
              and d["budget_tokens"] == 120_000 and d["budget_minutes"] is None,
              f"форма строки разбора аварии: {d}")

        # старая вкладка: новых полей нет вовсе
        router.apply_route_form({
            "route_incident_provider": "api", "route_incident_claude_model": "",
            "depth_incident_model": "smart", "depth_incident_reasoning": "xhigh",
            "depth_incident_priority": "batch",
        })
        d = router.get_depth("incident")
        check(d["api_entry"] == "gpt" and d["tools"] == "telemetry" and d["budget_calls"] == 7,
              "вкладка, открытая до обновления, новых полей не сбрасывает")

        # флажок снят: отметка присутствия есть, самого флажка нет
        router.apply_route_form({
            "route_incident_provider": "api", "depth_incident_model": "smart",
            "depth_incident_reasoning": "xhigh", "depth_incident_priority": "batch",
            "route_incident_tools_present": "1",
        })
        check(router.get_depth("incident")["tools"] == "", "снятый флажок выключает инструменты")

        # сохранение и загрузка
        router.apply_depth("incident", "smart", "xhigh", "batch", tools="telemetry")
        dumped = router.serialize_depth()
        router.apply_depth("incident", "fast", "default", "normal", provider="llm",
                           api_entry="", tools="", budgets={k: "" for k in router.BUDGET_KEYS})
        router.load_depth(dumped)
        d = router.get_depth("incident")
        check(d["api_entry"] == "gpt" and d["tools"] == "telemetry" and d["budget_calls"] == 7
              and d["provider"] == "api", "строка переживает сохранение и загрузку")

        # ключ старого формата (без новых полей) — умолчания, провайдер цел
        legacy = {k: {kk: vv for kk, vv in v.items()
                      if kk in ("model", "reasoning", "priority", "provider", "claude_model")}
                  for k, v in json.loads(dumped).items()}
        router.load_depth(json.dumps(legacy))
        d = router.get_depth("incident")
        check(d["api_entry"] == "" and d["tools"] == "" and d["budget_calls"] is None
              and d["provider"] == "api", "ключ старого формата: новые поля — умолчания")

        print("4. Помощники строки: подключение, модель, бюджеты, подпись")
        router.apply_depth("incident", "smart", "xhigh", "batch", provider="api",
                           claude_model="", api_entry="gpt", tools="telemetry",
                           budgets={"budget_calls": 7, "budget_tokens": "", "budget_minutes": ""})
        ent = router.api_entry_for("incident")
        check(ent is not None and ent["id"] == "gpt", "подключение строки — запись реестра")
        check(router.api_model_for("incident") == "gpt-x", "модель — из записи реестра")
        check(router.tools_enabled("incident"), "инструменты включены")
        check("GPT" in router.route_label("incident") and "gpt-x" in router.route_label("incident"),
              f"подпись строки называет подключение: {router.route_label('incident')}")
        router.apply_depth("incident", "smart", "xhigh", "batch", claude_model="gpt-y")
        check(router.api_model_for("incident") == "gpt-y", "поле модели строки — переопределение")

        router.apply_depth("incident", "smart", "xhigh", "batch", claude_model="", api_entry="old")
        check(router.api_entry_for("incident")["id"] == "old", "DeepSeek — облачное подключение")
        router.apply_depth("incident", "smart", "xhigh", "batch", api_entry="нет-такой")
        check(router.api_entry_for("incident") is None, "удалённая запись — как без подключения")
        check(router.api_model_for("incident") == router.claude_model_for("incident"),
              "без подключения модель — как у Claude раньше")
        check(router.route_label("incident").startswith("Claude: "), "без подключения подпись — Claude")
        registry.upsert_entry({"id": "local", "name": "Локальная", "provider": "lmstudio",
                               "model": "q", "base_url": "http://h:1234"})
        router.apply_depth("incident", "smart", "xhigh", "batch", api_entry="local")
        check(router.api_entry_for("incident") is None, "локальная запись облачным подключением не считается")
        router.apply_depth("incident", "smart", "xhigh", "batch", api_entry="claude")
        check(router.api_entry_for("incident")["provider"] == "anthropic"
              and router.api_model_for("incident") == "claude-x"
              and router.route_label("incident") == "Claude: claude-x",
              "запись Claude: модель записи, подпись Claude")

        # бюджеты: строка → config.yml → код
        import telemetry_mcp.config as mcp_config
        _load = mcp_config.load

        class _Cfg:
            host_budgets: dict = {}

        try:
            mcp_config.load = lambda: _Cfg()
            b = router.budgets_for("incident")
            check(b == {"calls": 7, "tokens": 200_000, "minutes": 10}, f"бюджеты: строка поверх кода: {b}")
            check(router.budgets_for("incident", gateway=True) == {"calls": 7, "tokens": 150_000, "minutes": 40},
                  "бюджеты шлюза — свои умолчания")
            _Cfg.host_budgets = {"cloud": {"tokens": 300_000, "minutes": 5}, "gateway": {"calls": 4}}
            b = router.budgets_for("incident")
            check(b == {"calls": 7, "tokens": 300_000, "minutes": 5}, f"config.yml перекрывает код: {b}")
            router.apply_depth("incident", "smart", "xhigh", "batch", budgets={"budget_calls": ""})
            check(router.budgets_for("incident", gateway=True)["calls"] == 4, "бюджет шлюза из config.yml")

            def _broken():
                raise RuntimeError("нет конфига")
            mcp_config.load = _broken
            check(router.budget_defaults() == router.BUDGET_DEFAULTS["cloud"],
                  "сбой чтения конфига — умолчания кода")
        finally:
            mcp_config.load = _load
        cfg = mcp_config.load()
        check(cfg.client_url.startswith("http") and cfg.client_url.endswith("/mcp"),
              f"адрес сервера телеметрии для хоста: {cfg.client_url}")
    finally:
        router.load_depth(_depth_saved)
finally:
    registry.load_registry(_saved)


# ── 2. Общий клиент ──────────────────────────────────────────────────────────
print("2. Общий клиент: OpenAI")

cfg = registry.normalize_entry({"name": "GPT", "provider": "openai", "model": "gpt-x",
                                "api_key": "sk-test", "temperature": 0.4, "proxy": "http://p:1"})
url, payload, headers = llm_client._build_request("сис", "вопрос", None, True, cfg)
check(url == "https://api.openai.com/v1/chat/completions", f"адрес запроса: {url}")
check(headers.get("Authorization") == "Bearer sk-test", "ключ в заголовке")
check("temperature" not in payload, "температура OpenAI не шлётся")
check(payload.get("stream_options") == {"include_usage": True}, "расход в потоке запрошен")
check("reasoning_effort" not in payload and "chat_template_kwargs" not in payload,
      "рассуждение по умолчанию — ничего не шлём")
check(payload["messages"][0]["content"] == "сис", "системный промпт без примесей")
check("priority" not in payload and "notes" not in payload, "полей шлюза нет")

for level, want in (("off", None), ("low", "low"), ("medium", "medium"), ("high", "high"), ("xhigh", "high")):
    _, p, _ = llm_client._build_request("сис", "вопрос", None, False, cfg, reasoning=level)
    check(p.get("reasoning_effort") == want, f"рассуждение {level} → {want}: {p.get('reasoning_effort')}")
    check("chat_template_kwargs" not in p and "thinking" not in p, f"{level}: без полей других провайдеров")
    check("/no_think" not in p["messages"][0]["content"], f"{level}: без /no_think")
    check("stream_options" not in p, "без потока расход не запрашивается отдельно")

SEEN: list[httpx.Request] = []


def handler(request: httpx.Request) -> httpx.Response:
    SEEN.append(request)
    if request.url.path.endswith("/v1/models"):
        return httpx.Response(200, json={"data": [{"id": "gpt-x"}, {"id": "gpt-y"}]})
    return httpx.Response(404)


_tr = llm_client._TRANSPORT
llm_client._TRANSPORT = httpx.MockTransport(handler)
try:
    models = run(llm_client.list_server_models("openai", "https://api.openai.com", "sk-test",
                                               proxy="http://p:1"))
    check(models == ["gpt-x", "gpt-y"], f"список моделей OpenAI: {models}")
    check(str(SEEN[-1].url) == "https://api.openai.com/v1/models"
          and SEEN[-1].headers.get("authorization") == "Bearer sk-test", "запрос списка моделей")
    ping = run(llm_client.ping_entry(cfg))
    check(ping["ok"] and "моделей: 2" in ping["detail"], f"проверка подключения: {ping}")
finally:
    llm_client._TRANSPORT = _tr

_made: list[dict] = []
_real = httpx.AsyncClient


class _Spy(_real):
    def __init__(self, *a, **kw):
        _made.append(dict(kw))
        super().__init__(*a, **{k: v for k, v in kw.items() if k != "proxy"})


httpx.AsyncClient = _Spy
try:
    async def _open():
        async with llm_client._http_client("http://p:1"):
            pass
        async with llm_client._http_client():
            pass
        async with llm_client._http_client(None):
            pass
    run(_open())
finally:
    httpx.AsyncClient = _real
check(_made[0].get("proxy") == "http://p:1", "прокси записи доходит до клиента")
check("proxy" not in _made[1] and "proxy" not in _made[2], "без прокси — напрямую")


# ── 5. Расход ────────────────────────────────────────────────────────────────
print("5. Расход: запросы, хранение, итоги")

sql = llm_usage.rollup_sql()
check("DELETE FROM llm_calls WHERE ts < $1 RETURNING *" in sql, "свёртка удаляет старше границы")
check("INSERT INTO llm_calls_daily" in sql and "ON CONFLICT (day, task_kind, provider, model)" in sql,
      "свёртка дописывает суточные итоги")
check(sql.count("$") == 2 and "$2::text" in sql, "свёртка: два параметра, пояс текстом")
for c in llm_usage.SUM_COLUMNS:
    check(f"sum({c}) AS {c}" in sql and f"{c} = CASE WHEN d.{c} IS NULL" in sql,
          f"свёртка: колонка {c} суммируется и сливается без потери NULL")
check(sql.count("(") == sql.count(")"), "свёртка: скобки сходятся")

for name, q, n in (("по дням", llm_usage.by_day_sql(), 3), ("по задачам", llm_usage.by_task_sql(), 2)):
    params = set(re.findall(r"\$(\d)", q))
    check(params == {str(i) for i in range(1, n + 1)}, f"сводка {name}: параметры {sorted(params)}")
    check("UNION ALL" in q and "llm_calls_daily" in q, f"сводка {name}: журнал и суточные итоги")
    check(q.count("(") == q.count(")"), f"сводка {name}: скобки сходятся")
check("FROM mcp_calls" in llm_usage.BY_TOOL_SQL and "avg(bytes)" in llm_usage.BY_TOOL_SQL,
      "сводка по инструментам — из журнала сервера телеметрии")

cut = llm_usage.cutoff_for(90, now=datetime(2026, 11, 12, 21, 30, tzinfo=timezone.utc))
local = cut.astimezone(llm_usage._tz())
check((local.hour, local.minute, local.second) == (0, 0, 0), f"граница хранения — начало суток: {local}")
now_local = datetime(2026, 11, 12, 21, 30, tzinfo=timezone.utc).astimezone(llm_usage._tz())
check((now_local.date() - local.date()).days == 90, "граница хранения — 90 суток назад")
check(llm_usage.KEEP_DAYS == 90, "построчно хранится 90 суток")

rows = [
    {"calls": 3, "errors": 1, "priced": 2, "tokens_in": 100, "tokens_out": 50,
     "tokens_cache_read": 300, "tokens_cache_write": None, "tokens_reasoning": None,
     "tool_calls": None, "duration_ms": 900, "cost_usd": 0.5},
    {"calls": 2, "errors": 0, "priced": 0, "tokens_in": None, "tokens_out": 20,
     "tokens_cache_read": None, "tokens_cache_write": None, "tokens_reasoning": None,
     "tool_calls": None, "duration_ms": 100, "cost_usd": None},
]
t = llm_usage.totals(rows)
check(t["calls"] == 5 and t["errors"] == 1 and t["priced"] == 2, "итог: вызовы, сбои, оценённые")
check(t["tokens_in"] == 100 and t["tokens_out"] == 70 and t["cost_usd"] == 0.5, "итог: суммы")
check(t["tokens_reasoning"] is None and t["tool_calls"] is None and t["tokens_cache_write"] is None,
      "итог: чего никто не отдал — None, не 0")
check(t["cache_share"] == 0.75, f"доля кэша: 300 из 400 — {t['cache_share']}")
check(llm_usage.totals([])["tokens_in"] is None and llm_usage.totals([])["calls"] == 0, "пустой период")
check(llm_usage.cache_share({"tokens_in": 10, "tokens_cache_read": None}) is None,
      "провайдер кэш не отдаёт — доли нет")
check(llm_usage.cache_share({"tokens_in": 0, "tokens_cache_read": 0}) is None, "входа нет — доли нет")
check(llm_usage.cache_share({"tokens_in": 100, "tokens_cache_read": 100, "tokens_cache_write": 200}) == 0.25,
      "доля кэша считается от всего входа")


# ── 6. Схема, планировщик, шаблоны ───────────────────────────────────────────
print("6. Схема, планировщик, шаблоны")

schema = read("db/online_schema.sql")
m = re.search(r"CREATE TABLE IF NOT EXISTS llm_calls_daily \((.*?)\n\);", schema, re.S)
check(m is not None, "таблица llm_calls_daily есть в схеме")
if m:
    ddl = m.group(1)
    for c in ("day", "task_kind", "provider", "model", "calls", "errors", "priced", *llm_usage.SUM_COLUMNS):
        check(re.search(rf"^\s*{c}\s", ddl, re.M) is not None, f"llm_calls_daily: колонка {c}")
    check("PRIMARY KEY (day, task_kind, provider, model)" in ddl, "llm_calls_daily: ключ для слияния")
    check(re.search(r"\bCHECK\s*\(", ddl) is None, "llm_calls_daily: без CHECK")

sched = read("scheduler.py")
check("llm_calls_rollup" in sched and "_rollup_llm_calls" in sched, "задание свёртки в планировщике")
check("usage_routes" in read("main.py"), "API расхода подключён к приложению")

tpl = read("web/templates/settings.html")
for field in ('name="price_in"', 'name="price_out"', 'name="price_cache_read"', 'name="price_cache_write"',
              'name="tools"', 'name="vision"', 'name="proxy"', 'name="caps_present"',
              '<option value="openai">'):
    check(field in tpl, f"форма реестра: {field}")
check('{% include "settings_usage.html" %}' in tpl, "карточка расхода подключена к настройкам")
card = read("web/templates/settings_usage.html")
check("/api/llm-usage" in card and "aiUsageLoad" in card, "карточка расхода читает API")
check("innerHTML" not in card, "карточка расхода не вставляет разметку из данных")

# шаблоны разбираются
from jinja2 import Environment, FileSystemLoader

env = Environment(loader=FileSystemLoader(os.path.join(ROOT, "web", "templates")))
for name in ("settings.html", "settings_usage.html"):
    try:
        env.parse(env.loader.get_source(env, name)[0])
    except Exception as exc:  # noqa: BLE001
        check(False, f"шаблон {name} не разбирается: {exc!r}")

# обработчик формы реестра: старая вкладка не сбрасывает цены и признаки
routes = read("web/routes.py")
check('form.get("caps_present") is None and existing' in routes,
      "сохранение записи: без отметки формы цены и признаки остаются прежними")

print()
if _errors:
    print(f"Не пройдено проверок: {len(_errors)}")
    for msg in _errors:
        print("  -", msg)
    sys.exit(1)
print("Все проверки пройдены.")
