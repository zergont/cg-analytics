# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""Таблица «Кто и как разбирает» (v5.1.11).

До v5.1.11 провайдера гейта и аварии выбирала карточка «по уровню», модель
шлюза — градация, а основного исполнителя заключений — голова цепочки: три
настройки на одно решение. 12.10 разбор аварии ушёл в Claude вопреки градации.

Запуск:  py -3 scripts/test_ai_routes_v5111.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import corpus.settings as corpus_settings  # noqa: E402
import corpus.worker as worker  # noqa: E402
import llm.client as client  # noqa: E402
import llm.registry as registry  # noqa: E402
import llm.router as router  # noqa: E402

_errors: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        _errors.append(msg)


# Снимок app_settings 12.10 (ai_settings_out.txt)
SNAP_DEPTH = json.dumps({
    "seg_norma": {"model": "fast", "reasoning": "xhigh", "priority": "normal"},
    "seg_caution": {"model": "fast", "reasoning": "xhigh", "priority": "normal"},
    "seg_warning": {"model": "smart", "reasoning": "high", "priority": "normal"},
    "seg_shutdown": {"model": "smart", "reasoning": "high", "priority": "normal"},
    "gate_caution": {"model": "fast", "reasoning": "low", "priority": "interactive"},
    "gate_warning": {"model": "smart", "reasoning": "high", "priority": "interactive"},
    "incident": {"model": "smart", "reasoning": "xhigh", "priority": "batch"},
    "humanize": {"model": "fast", "reasoning": "xhigh", "priority": "batch"},
    "manual": {"model": "fast", "reasoning": "default", "priority": "interactive"},
})
GGUF = "/home/folist/models/Qwen3.8-27B-Q8_0.gguf"
SNAP_LEGACY = {
    "ai_warning_level_предупреждение_provider": "llm",
    "ai_warning_level_предупреждение_model": GGUF,
    "ai_warning_level_внимание_provider": "llm",
    "ai_warning_level_внимание_model": GGUF,
    "ai_task_human_auto_provider": "llm",
    "ai_task_seg_auto_provider": "llm",
    "seg_chain_head_provider": "llm",           # голова цепочки — запись шлюза
}
CARD_MODEL = "claude-sonnet-5-5"
corpus_settings.get_claude_settings = lambda: {"model": CARD_MODEL, "max_tokens": 1000,
                                               "proxy": "", "max_tool_calls": 3}

# ── 1. Миграция на снимке 12.10 ──────────────────────────────────────────────
new, changes = router.migrate_routes(SNAP_DEPTH, SNAP_LEGACY)
check(new is not None and len(changes) == 9, f"1: перенесены все 9 видов: {changes}")
d = json.loads(new)
check(all(d[k]["provider"] == "llm" for k in router.DEPTH_KINDS),
      f"1б: всё на шлюзе: {[(k, d[k]['provider']) for k in d]}")
check(all(d[k]["claude_model"] == "" for k in router.DEPTH_KINDS),
      "1в: путь к gguf в модель Claude не перенесён")
check(d["incident"] == {"model": "smart", "reasoning": "xhigh", "priority": "batch",
                        "provider": "llm", "claude_model": ""},
      f"1г: авария — совет шлюза, batch: {d['incident']}")
again, ch2 = router.migrate_routes(new, SNAP_LEGACY)
check(again is None and ch2 == [], f"1д: повторный прогон ничего не меняет: {ch2}")

# Авария на Claude в старых ключах — всё равно шлюз (решение 12.10)
legacy_api = dict(SNAP_LEGACY, **{
    "ai_warning_level_предупреждение_provider": "api",
    "ai_warning_level_предупреждение_model": "claude-haiku-5-5",
    "seg_chain_head_provider": "api"})
d2 = json.loads(router.migrate_routes(SNAP_DEPTH, legacy_api)[0])
check(d2["gate_caution"]["provider"] == "api" and d2["gate_caution"]["claude_model"] == "claude-haiku-5-5",
      f"1е: гейт аналитики на Claude с моделью уровня: {d2['gate_caution']}")
check(d2["gate_warning"]["provider"] == "llm" and d2["gate_warning"]["claude_model"] == "",
      "1ж: гейт панели остался на шлюзе, gguf отброшен")
check(all(d2[k]["provider"] == "api" for k in ("seg_norma", "seg_caution", "seg_warning", "seg_shutdown")),
      "1з: заключения — по голове цепочки (Claude)")
check(d2["incident"]["provider"] == "llm" and d2["manual"]["provider"] == "llm",
      "1и: авария и ручные — шлюз всегда")

# Строка с провайдером не трогается; битый JSON — ключ не пишем
part = json.loads(SNAP_DEPTH)
part["incident"]["provider"] = "api"
part["incident"]["claude_model"] = "claude-opus-5-5"
d3 = json.loads(router.migrate_routes(json.dumps(part), SNAP_LEGACY)[0])
check(d3["incident"]["provider"] == "api" and d3["incident"]["claude_model"] == "claude-opus-5-5",
      "1к: уже перенесённая строка не трогается")
check(router.migrate_routes("{битый", SNAP_LEGACY)[0] is None, "1л: битый JSON — None")
check(router.migrate_routes("[1,2]", SNAP_LEGACY)[0] is None, "1м: не словарь — None")
d4 = json.loads(router.migrate_routes("", SNAP_LEGACY)[0])
check(set(d4) == set(router.DEPTH_KINDS) and d4["incident"]["provider"] == "llm",
      "1н: пустой ключ — строки по умолчанию с провайдером")

# ── 2. Загрузка/сохранение строки ────────────────────────────────────────────
router.load_depth(new)
check(router.get_route("incident")["provider"] == "llm"
      and router.get_route("incident")["reasoning"] == "xhigh", "2: строка аварии загружена")
router.load_depth(router.serialize_depth())
check(json.loads(router.serialize_depth()) == json.loads(new), "2б: круговая загрузка без потерь")
router.load_depth(SNAP_DEPTH)          # старый JSON без провайдера (откат и обратно)
check(router.get_route("gate_caution")["provider"] == "llm", "2в: без поля provider — шлюз")
router.load_depth(json.dumps({"humanize": {"model": "fast", "reasoning": "low", "priority": "batch",
                                           "provider": "мусор"}}))
check(router.get_route("humanize")["provider"] == "llm", "2г: неверный провайдер — шлюз")
router.load_depth(new)
router.apply_depth("gate_warning", "smart", "high", "interactive", provider="api",
                   claude_model="claude-opus-5-5")
router.apply_depth("gate_warning", "fast", "low", "interactive")
r = router.get_route("gate_warning")
check(r["provider"] == "api" and r["claude_model"] == "claude-opus-5-5" and r["model"] == "fast",
      f"2д: без provider/claude_model — прежние: {r}")

# ── 3. Подписи и модель Claude ───────────────────────────────────────────────
router.load_depth(new)
client.apply_llm_settings("http://lms:1234", "qwen/qwen3.8-27b", 0.1, 65000, provider="lmstudio")
check(router.route_label("incident") == "LLM (lmstudio): qwen/qwen3.8-27b",
      f"3-: общее подключение не шлюз — подпись по нему: {router.route_label('incident')}")
client.apply_llm_settings("http://192.168.0.79:4000/src/analytics", "fast", 1.0, 65000,
                          provider="gorynych")
check(router.route_label("incident") == "Горыныч: smart (xhigh)", router.route_label("incident"))
check(router.route_label("manual") == "Горыныч: fast", router.route_label("manual"))
router.apply_depth("humanize", "fast", "xhigh", "batch", provider="api", claude_model="")
check(router.claude_model_for("humanize") == CARD_MODEL
      and router.route_label("humanize") == f"Claude: {CARD_MODEL}",
      "3: пустая модель Claude — из карточки")
router.apply_depth("humanize", "fast", "xhigh", "batch", provider="api", claude_model="claude-haiku-5-5")
check(router.claude_model_for("humanize") == "claude-haiku-5-5", "3б: модель строки")
router.load_depth(new)

# ── 4. Гейт на шлюзе: модель/рассуждение/приоритет строки, без пути к gguf ─────
GW_CFG = {"provider": "gorynych", "base_url": "http://gw/src/analytics", "model": "fast"}
m, rs, pr = client.resolve_depth(GW_CFG, "gate_caution", GGUF, None, None)
check((m, rs, pr) == ("fast", "low", "interactive"), f"4: гейт аналитики — строка: {(m, rs, pr)}")
m, rs, pr = client.resolve_depth(GW_CFG, "incident", None, None, None)
check((m, rs, pr) == ("smart", "xhigh", "batch"), f"4б: авария — совет: {(m, rs, pr)}")

# ── 5. Заключения: основной из строки, запасные без совпадающих ──────────────
REG = [
    {"id": "model", "name": "Горыныч (шлюз)", "provider": "gorynych", "type": "llm",
     "base_url": "http://192.168.0.79:4000/src/analytics", "model": "fast", "max_ctx_tokens": 100000},
    {"id": "local-default", "name": "LM Studio", "provider": "lmstudio", "type": "llm",
     "base_url": "http://192.168.0.111:1234", "model": "qwen", "max_ctx_tokens": 100000},
    {"id": "claude-api", "name": "Claude API", "provider": "anthropic", "type": "api",
     "base_url": "", "model": "claude-sonnet-5", "max_ctx_tokens": 200000},
]
registry.load_registry(json.dumps(REG))
router.apply_chain("seg_auto", ["model", "local-default", "claude-api"])
router.apply_chain("seg_manual", [])
client.apply_llm_settings("http://192.168.0.79:4000/src/analytics/v1", "fast", 1.0, 65000,
                          stream=False, provider="gorynych")

ids = [e["id"] for e in worker._route_entries("seg_auto", "seg_caution")]
check(ids == ["route:llm", "local-default", "claude-api"], f"5: Горыныч основной: {ids}")
head = worker._route_entries("seg_auto", "seg_caution")[0]
check(head["max_ctx_tokens"] == registry.DEFAULT_MAX_CTX["gorynych"] and head["stream"] is True
      and head.get("_global"), f"5б: основной шлюз — окно шлюза, поток: {head}")
router.apply_depth("seg_shutdown", "smart", "high", "normal", provider="api",
                   claude_model="claude-opus-5-5")
ents = worker._route_entries("seg_auto", "seg_shutdown")
check([e["id"] for e in ents] == ["route:api", "model", "local-default"],
      f"5в: Claude основной: {[e['id'] for e in ents]}")
check(ents[0]["model"] == "claude-opus-5-5", "5г: модель основного Claude — из строки")
check([e["id"] for e in worker._route_entries("seg_manual", "manual")] == ["route:llm"],
      "5д: ручной без цепочки — только основной")

# Сбой основного уводит к запасным; модель передаётся только основному Claude
calls: list[tuple] = []


async def _chat(system, user, *, entry=None, stream=None, depth=None, meta=None, **k):
    calls.append(("chat", entry["id"] if entry else "_cfg", depth))
    if entry is None:
        raise client.LLMError(503, "backend_unavailable", "нет головы", gateway=True)
    return "## Коротко\nЗапасной ответ"


async def _agent(seg_row, kb_path, system_prompt=None, model=None):
    calls.append(("agent", model))
    return {"conclusion_md": "Claude", "verdict": "норма", "alarm_level": "НОРМА",
            "claude_model": model or CARD_MODEL, "tokens_used": 1, "tool_calls_count": 0,
            "loops_count": 1, "generation_time_sec": 0.1, "debug_json": {}, "error": None}


client.chat = _chat
# corpus.agent тянет anthropic — подменяем модуль целиком (воркер импортирует лениво)
import types  # noqa: E402
sys.modules["corpus.agent"] = types.SimpleNamespace(analyse_segment=_agent)
worker._model_input = lambda row: "ВХОД"
worker._verdict_fields = lambda row: ("норма", "НОРМА")

calls.clear()
res = asyncio.run(worker._analyse_segment_chain({"id": 1}, None, "SYS",
                                                worker._route_entries("seg_auto", "seg_caution"),
                                                depth="seg_caution"))
check(calls == [("chat", "_cfg", "seg_caution"), ("chat", "local-default", "seg_caution")],
      f"5е: основной упал → запасной LM Studio: {calls}")
check(res["error"] is None and [t["action"] for t in res["debug_json"]["routing"]] == ["error", "ok"],
      f"5ж: след маршрута: {res['debug_json'].get('routing')}")

calls.clear()
asyncio.run(worker._analyse_segment_chain({"id": 2}, None, "SYS",
                                          worker._route_entries("seg_auto", "seg_shutdown"),
                                          depth="seg_shutdown"))
check(calls == [("agent", "claude-opus-5-5")], f"5з: основной Claude — модель строки: {calls}")

calls.clear()
asyncio.run(worker._analyse_segment_chain({"id": 3}, None, "SYS",
                                          [worker._route_head("seg_caution"),
                                           registry.get_entry("claude-api")],
                                          depth="seg_caution"))
check(calls == [("chat", "_cfg", "seg_caution"), ("agent", None)],
      f"5и: запасной claude-api — модель карточки, не записи реестра: {calls}")

# Основной без запасных: отказ — ошибка, а не пустое заключение
calls.clear()
res = asyncio.run(worker._analyse_segment_chain({"id": 4}, None, "SYS",
                                                [worker._route_head("manual")], depth="manual"))
check(res["error"] and not res["conclusion_md"], f"5к: отказ без запасных — ошибка: {res['error']}")

# ── 6. Пересказ: провайдер строки ────────────────────────────────────────────
import corpus.humanizer as hum  # noqa: E402
seen_h: list[tuple] = []


async def _h_api(system_prompt, user_msg, model=None):
    seen_h.append(("api", model))
    return "Пересказ"


async def _h_llm(system_prompt, user_msg, meta=None):
    seen_h.append(("llm",))
    return "Пересказ"


hum._humanize_api = _h_api
hum._humanize_llm = _h_llm
hum_input = "## Блок 2\nТекст заключения\n"
router.load_depth(new)
router.apply_depth("humanize", "fast", "xhigh", "batch", provider="api", claude_model="claude-haiku-5-5")
_orig_extract = getattr(hum, "_extract_block2", None)
if _orig_extract is not None:
    hum._extract_block2 = lambda md: "Текст"
asyncio.run(hum.humanize(hum_input))
router.apply_depth("humanize", "fast", "xhigh", "batch", provider="llm")
asyncio.run(hum.humanize(hum_input))
check(seen_h == [("api", "claude-haiku-5-5"), ("llm",)], f"6: пересказ по строке: {seen_h}")

# ── 7. Форма настроек: устаревшая вкладка ничего не сбрасывает ───────────────
# (web/routes.py только вызывает apply_route_form / apply_prompts_form и пишет итог)
router.load_depth(new)
router.apply_depth("gate_caution", "fast", "low", "interactive", provider="api",
                   claude_model="claude-haiku-5-5")
# Форма до деплоя: только depth_* (без route_*)
stale = {f"depth_{k}_{f}": router.get_depth(k)[f] for k in router.DEPTH_KINDS
         for f in ("model", "reasoning", "priority")}
stale["depth_incident_reasoning"] = "high"
router.apply_route_form(stale)
g = router.get_route("gate_caution")
check(g["provider"] == "api" and g["claude_model"] == "claude-haiku-5-5",
      f"7: старая форма не сбросила провайдера/модель Claude: {g}")
check(router.get_route("incident")["reasoning"] == "high", "7б: поле из старой формы применено")
check(json.loads(router.serialize_depth())["gate_caution"]["provider"] == "api",
      "7в: в БД — с провайдером")
# Новая форма: переключить, ручные — всегда шлюз; пустая форма — ничего не трогает
router.apply_route_form({"route_gate_caution_provider": "llm",
                         "route_gate_caution_claude_model": "",
                         "route_manual_provider": "api"})
check(router.get_route("gate_caution")["provider"] == "llm"
      and router.get_route("gate_caution")["claude_model"] == ""
      and router.get_route("gate_caution")["model"] == "fast",
      f"7г: переключение на шлюз, параметры шлюза целы: {router.get_route('gate_caution')}")
check(router.get_route("manual")["provider"] == "llm", "7д: ручные — всегда шлюз")
check(router.get_route("incident")["reasoning"] == "high", "7е: пустая форма не трогает строки")

# Промпты: поля нет — текущий; пустое — по умолчанию
router.apply_task("incident", "llm", "МОЙ ПРОМПТ АВАРИИ")
acc = router.apply_prompts_form({"ai_seg_auto_prompt": "НОВЫЙ"})
check(router.get_prompt("incident") == "МОЙ ПРОМПТ АВАРИИ" and "incident" not in acc,
      "7ж: нет поля промпта аварии — промпт не тронут")
check(router.get_prompt("seg_auto") == "НОВЫЙ" and acc == {"seg_auto": "НОВЫЙ"},
      f"7з: принят только присланный промпт: {acc}")
router.apply_prompts_form({"ai_incident_prompt": "  "})
check(router.get_prompt("incident") == router.get_default_prompt("incident")
      and "verdict" not in router.get_prompt("incident"),
      "7и: пустой промпт — по умолчанию, без verdict")
check(router.get_all()["incident"]["prompt"] == router.get_default_prompt("incident"),
      "7к: в форме виден действующий промпт")

# ── 8. «Запасные»: якорь шлюза форма не снимает и не переставляет ────────────
gw = lambda e: e in ("model",)  # noqa: E731
check(router.merge_fallbacks(["model", "claude-api"], ["claude-api"], gw) == ["model", "claude-api"],
      "8: карточка без шлюза — якорь на месте")
check(router.merge_fallbacks(["model", "claude-api"], [], gw) == ["model"],
      "8б: запасные убраны — якорь остался")
check(router.merge_fallbacks(["model", "claude-api"], ["claude-api", "model", "x", "x"], gw)
      == ["model", "claude-api", "x"], "8в: устаревшая вкладка не переставляет якорь, повторы убраны")
check(router.merge_fallbacks(["claude-api"], ["model", "claude-api"], gw) == ["claude-api"],
      "8г: якоря не было — форма его не добавляет")

# ── 9. Клиент: принятый шлюзом совет после обрыва не пересылается ────────────
import httpx  # noqa: E402

posts: list[int] = []


class _Broken(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b": ping\n\n"
        raise httpx.ReadError("обрыв")


def _handler(request):
    posts.append(1)
    return httpx.Response(200, headers={"content-type": "text/event-stream",
                                        "x-gorynych-task-id": "gw-77"}, stream=_Broken())


import importlib  # noqa: E402
real_client = importlib.reload(client)          # chat подменён выше — берём настоящий
real_client._TRANSPORT = httpx.MockTransport(_handler)
real_client._retry_delay = lambda *a, **k: 0
real_client.apply_llm_settings("http://gw/src/analytics", "fast", 0.1, 1000, provider="gorynych")
meta9: dict = {}
try:
    asyncio.run(real_client.chat("SYS", "АКТ", depth="incident", meta=meta9, resend_accepted=False))
    check(False, "9: обрыв должен дойти до вызывающего")
except httpx.ReadError:
    pass
check(len(posts) == 1 and meta9.get("task_id") == "gw-77",
      f"9б: один POST, task_id сохранён: {len(posts)}, {meta9}")
posts.clear()
try:
    asyncio.run(real_client.chat("SYS", "отчёт", depth="seg_norma"))
except httpx.ReadError:
    pass
check(len(posts) == real_client._RETRY_ATTEMPTS, f"9в: остальные вызовы — с повтором как раньше: {len(posts)}")


def _stop_handler(request):
    posts.append(1)
    return httpx.Response(503, json={"error": {"code": "gateway_stopping", "message": "рестарт"}},
                          headers={"x-gorynych-task-id": "gw-78"})


posts.clear()
real_client._TRANSPORT = httpx.MockTransport(_stop_handler)
try:
    asyncio.run(real_client.chat("SYS", "АКТ", depth="incident", resend_accepted=False))
except Exception:
    pass
check(len(posts) == real_client._RETRY_ATTEMPTS, f"9г: до приёма задачи (503) повтор остаётся: {len(posts)}")
real_client._TRANSPORT = None

if _errors:
    print(f"FAIL — {len(_errors)} расхождений:")
    for x in _errors:
        print(f"  • {x}")
    sys.exit(1)
print("Все проверки пройдены.")
