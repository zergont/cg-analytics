"""Приёмка MCP Э2б: разбор акта аварии с инструментами сервера телеметрии — ВНЕ сервиса
(решение Р24 design_mcp_e2.md). В БД ничего не пишет: акт и настройки только читает,
строки журнала llm_calls собирает в память, разбор кладёт в файлы каталога --out.

    python scripts/incident_tools_probe.py --act 35941 --mcp-url http://192.168.0.194:8095/mcp
    python scripts/incident_tools_probe.py --act 35941 --entry deepseek --out exports/e2b
    python scripts/incident_tools_probe.py --act 35941 --entry gorynych --model flashnext --priority batch
    python scripts/incident_tools_probe.py --keys-file .claude/e2b_keys.yml --check-keys
    python scripts/incident_tools_probe.py --act 35941 --entry openai --keys-file .claude/e2b_keys.yml

--entry — id записи реестра моделей (ключ, прокси и цены — её); без него — Claude с
ключом config.yml и моделью карточки «Claude API». Бриф — тот же, что собирает сервис
(акт, ход цикла, контекст, картина объекта). Очистка языка (второй вызов модели в
сервисе) здесь не выполняется.

--keys-file — локальный файл с ключами провайдеров (YAML: провайдер → api_key, model,
proxy, vision, цены): записи живут только в памяти прогона, в реестр сервиса и в БД не
попадают, значения ключей не печатаются. --check-keys — проверить ключи бесплатным
запросом списка моделей, без вызова модели.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import logging

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

ROWS: list[dict] = []


async def _sink(row: dict):
    ROWS.append(dict(row))
    return len(ROWS)


FILE_PROVIDERS = ("openai", "deepseek", "gemini", "anthropic")
_FILE_URLS = {"deepseek": "https://api.deepseek.com"}


def entries_from_file(path: str, claude_proxy: str = "") -> dict[str, dict]:
    """Записи реестра в памяти из файла ключей; раздел без ключа пропускается."""
    import yaml

    from llm import registry
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    out: dict[str, dict] = {}
    for provider, sec in raw.items():
        if provider not in FILE_PROVIDERS or not isinstance(sec, dict):
            continue
        key = str(sec.get("api_key") or "").strip()
        if not key:
            continue
        proxy = str(sec.get("proxy") or "").strip()
        if proxy.lower() == "claude":
            proxy = claude_proxy
        extra = {k: sec[k] for k in (*registry.PRICE_KEYS, "vision", "tools", "reasoning", "base_url")
                 if sec.get(k) not in (None, "")}
        extra.setdefault("base_url", _FILE_URLS.get(provider, ""))
        out[provider] = registry.normalize_entry({
            "id": provider, "name": provider, "provider": provider,
            "model": str(sec.get("model") or "").strip(), "api_key": key, "proxy": proxy, **extra})
    return out


async def check_keys(entries: dict[str, dict]) -> int:
    """Проверить ключи запросом списка моделей (токены не тратятся). Ключи не печатаются."""
    import httpx

    from llm.client import list_server_models
    if not entries:
        print("в файле нет ни одного раздела с ключом")
        return 1
    bad = 0
    for pid, e in entries.items():
        if e["provider"] == "anthropic":
            print(f"{pid}: список моделей у Claude не запрашивается — ключ проверит первый прогон")
            continue
        via = "через прокси" if e.get("proxy") else "напрямую"
        try:
            models = await list_server_models(e["provider"], e["base_url"], e["api_key"],
                                              proxy=e.get("proxy") or None)
        except httpx.HTTPStatusError as exc:
            bad += 1
            print(f"{pid}: отказ {exc.response.status_code} ({via}): {exc.response.text[:200]!r}")
            continue
        except Exception as exc:  # noqa: BLE001
            bad += 1
            print(f"{pid}: нет связи ({via}): {type(exc).__name__}")
            continue
        model = e.get("model") or ""
        state = "не задана" if not model else ("есть в списке" if model in models else "НЕТ в списке")
        prices = "заданы" if e.get("price_in") is not None else "не заданы"
        print(f"{pid}: ключ принят ({via}), моделей {len(models)}; модель «{model or '—'}» {state}; "
              f"зрение: {'да' if e.get('vision') else 'нет'}; цены {prices}")
        if not model or model not in models:
            print("   доступные модели:", ", ".join(models[:40]))
            bad += 1 if model else 0
    return 1 if bad else 0


async def _load_settings() -> None:
    """То, что сервис читает из app_settings при старте и что нужно разбору."""
    from corpus.settings import apply_claude_settings, get_claude_settings
    from db.analytics import get_app_setting
    from llm import prompts, registry
    from llm.router import TASKS, apply_task

    registry.load_registry(await get_app_setting(registry.REGISTRY_SETTING_KEY, ""))
    d = get_claude_settings()
    apply_claude_settings(
        model=await get_app_setting("claude_model", d["model"]),
        max_tool_calls=int(await get_app_setting("claude_max_tool_calls", str(d["max_tool_calls"]))),
        max_tokens=int(await get_app_setting("claude_max_tokens", str(d["max_tokens"]))),
        proxy=await get_app_setting("claude_proxy", d["proxy"]),
    )
    _label, def_provider, _def_prompt = TASKS["incident"]
    apply_task("incident", await get_app_setting("ai_task_incident_provider", def_provider),
               await get_app_setting("ai_task_incident_prompt", "") or "")
    prompts.set_rules_enabled(await get_app_setting(prompts.RULES_ENABLED_KEY, "true") != "false")


async def _brief(act_id: int) -> tuple[str, dict, str]:
    """Бриф разбора акта, как его собирает сервис; (текст, строка сегмента, серийник роутера)."""
    import llm.object_picture as op
    from online import db as online_db
    from online import incident_gate as ig

    seg = await online_db.get_segment_by_id(act_id)
    if not seg:
        raise SystemExit(f"сегмент #{act_id} не найден")
    inc = seg.get("incident_json")
    if isinstance(inc, str):
        inc = json.loads(inc)
    if not inc:
        raise SystemExit(f"у сегмента #{act_id} нет акта аварии")
    stop_ts = ig.act_stop_ts(inc.get("stop_ts"), seg.get("t_start"))
    if stop_ts is None:
        raise SystemExit("у акта нет момента останова")

    # Вне сервиса менеджера нет: картину объекта строим той же функцией по карте установок
    async def picture_for(router_sn, equip_type, panel_id):
        try:
            ctx = await op.load_context()
            text = op.render(ctx, router_sn, equip_type, int(panel_id), live=False)
            if text:
                return text, True
        except Exception:
            logging.getLogger("probe").warning("картина объекта не собрана", exc_info=True)
        return op.fallback_text(), False

    op.picture_for = picture_for
    text = await ig._material(seg["router_sn"], seg["equip_type"], int(seg["panel_id"]), inc,
                              ig._utc(stop_ts), act_id)
    return text, seg, str(seg["router_sn"])


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--act", type=int, default=0, help="id сегмента с актом аварии")
    ap.add_argument("--keys-file", default="", help="локальный файл с ключами провайдеров (YAML); в БД не пишется")
    ap.add_argument("--check-keys", action="store_true", help="только проверить ключи из --keys-file, модель не вызывать")
    ap.add_argument("--prices", default="", help="цены записи для стоимости в отчёте, USD за 1 млн: вход,выход[,чтение кэша[,запись кэша]]")
    ap.add_argument("--entry", default="", help="id записи реестра моделей; пусто — Claude (config.yml)")
    ap.add_argument("--model", default="", help="модель вместо модели записи / карточки Claude")
    ap.add_argument("--priority", default="batch", help="приоритет очереди шлюза")
    ap.add_argument("--mcp-url", default="", help="адрес сервера телеметрии (умолчание — telemetry_mcp.client_url)")
    ap.add_argument("--no-tools", action="store_true", help="разбор одним запросом, без инструментов")
    ap.add_argument("--no-proxy", action="store_true", help="Claude без прокси карточки (с рабочей машины)")
    ap.add_argument("--calls", type=int, default=0)
    ap.add_argument("--tokens", type=int, default=0)
    ap.add_argument("--minutes", type=int, default=0)
    ap.add_argument("--out", default="", help="каталог для брифа, ответа и сводки прогона")
    ap.add_argument("--brief-only", action="store_true", help="только собрать бриф, модель не вызывать")
    a = ap.parse_args()

    from llm import agent_loop, registry, usage
    from llm.adapters import make_adapter
    from llm.mcp_client import McpUnavailable, TelemetrySession
    from llm.prompts import system_for
    from llm.router import BUDGET_DEFAULTS

    usage.set_sink(_sink)          # журнал — в память, не в БД
    # Пул исходной БД телеметрии (только чтение): состояние на момент останова и имена объектов
    from analytics.source import init_source_pool
    await init_source_pool()
    await _load_settings()
    file_entries: dict[str, dict] = {}
    if a.keys_file:
        from corpus.settings import get_claude_settings
        file_entries = entries_from_file(a.keys_file, get_claude_settings().get("proxy") or "")
        print("записи из файла ключей:", ", ".join(file_entries) or "нет")
    if a.check_keys:
        return await check_keys(file_entries)
    if not a.act:
        raise SystemExit("нужен --act: id сегмента с актом аварии")
    brief, seg, router_sn = await _brief(a.act)
    from online.incident_gate import scrub_private
    brief = scrub_private(brief, (router_sn,))      # как делает сервис на облачном пути (Р11)
    if router_sn and len(router_sn) >= 5 and router_sn in brief:
        where = [ln for ln in brief.splitlines() if router_sn in ln]
        print("ВНИМАНИЕ: в брифе есть серийный номер роутера — в облако такой вход не отправляю. Строки:")
        for ln in where[:5]:
            print("   ", ln.replace(router_sn, "<серийник роутера>")[:200])
        return 2
    print(f"акт #{a.act}: {seg['equip_type']}/{seg['panel_id']}, бриф {len(brief)} симв.")
    out_dir = a.out and os.path.join(a.out, f"act{a.act}_{a.entry or 'claude'}")
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "brief.md"), "w", encoding="utf-8") as f:
            f.write(brief)
    if a.brief_only:
        print(brief)
        return 0

    entry = None
    if a.entry:
        entry = file_entries.get(a.entry) or registry.get_entry(a.entry)
        if not entry:
            ids = ", ".join([*file_entries, *(e["id"] for e in registry.get_entries())])
            raise SystemExit(f"записи «{a.entry}» в реестре нет; есть: {ids}")
    if a.prices and entry:
        vals = [v.strip() for v in a.prices.split(",")]
        entry = registry.normalize_entry({**entry, **dict(zip(registry.PRICE_KEYS, vals))})
    if a.no_proxy:
        from corpus.settings import apply_claude_settings, get_claude_settings
        c = get_claude_settings()
        apply_claude_settings(c["model"], c["max_tool_calls"], c["max_tokens"], "")
        if entry:
            entry = {**entry, "proxy": ""}
    gateway = bool(entry) and entry.get("provider") == "gorynych"
    budget = dict(BUDGET_DEFAULTS["gateway" if gateway else "cloud"])
    for k in ("calls", "tokens", "minutes"):
        if getattr(a, k):
            budget[k] = getattr(a, k)

    run_id = agent_loop.new_run_id()
    adapter = make_adapter(entry, model=a.model or (entry or {}).get("model") or "", depth="incident",
                           priority=a.priority)
    t0 = time.monotonic()
    note = None
    try:
        session = tools = None
        if not a.no_tools:
            try:
                session = TelemetrySession(run_id, **({"url": a.mcp_url} if a.mcp_url else {}))
                await session.__aenter__()
                tools = await agent_loop.offered_tools(session, vision=bool(adapter.vision))
            except McpUnavailable as exc:
                print(f"сервер телеметрии недоступен: {exc} — прогон без инструментов")
                session, tools, note = None, None, "инструменты недоступны"
        user = brief
        if session is not None and tools:
            system, pm = system_for("incident", tools=True)
            from online.incident_gate import _unit_name
            name = _unit_name(brief)
            if name:
                user = f"{brief}\n\nДля инструментов телеметрии: установка этого акта — «{name}»."
            print(f"инструментов: {len(tools)} ({', '.join(t.name for t in tools)}); установка: {name or '—'}")
        else:
            system, pm = system_for("incident")
        print(f"прогон {run_id}: {adapter.provider}/{adapter.model or '(модель карточки)'}, "
              f"бюджет {budget}, промпт {pm['version']}")
        with usage.call_context(task="incident", depth="incident", prompt_version=pm["version"], ref=a.act):
            res = await agent_loop.run(adapter=adapter, system=system, user=user, session=session,
                                       tools=tools, budget=budget, run_id=run_id, tools_note=note)
    finally:
        await adapter.aclose()

    print(f"\n=== вызовы инструментов ({res.tool_calls}) ===")
    for c in res.calls:
        extra = f"[{c.get('label') or '—'}] {c.get('chars', 0)} симв." if c["kind"] == "call" else c["kind"]
        if c.get("is_error"):
            extra += " ОШИБКА"
        print(f"  ит.{c['iteration']} {c['name']} {json.dumps(c['args'], ensure_ascii=False)} → {extra}")
    print("\n=== журнал вызовов модели ===")
    for r in ROWS:
        print(f"  ит.{r.get('iteration')} {r.get('model')}: вход {r.get('tokens_in')}, кэш чт. {r.get('tokens_cache_read')}, "
              f"кэш зап. {r.get('tokens_cache_write')}, выход {r.get('tokens_out')}, рассужд. {r.get('tokens_reasoning')}, "
              f"инстр. {r.get('tool_calls')}, {r.get('duration_ms')} мс, {r.get('status')}, ${r.get('cost_usd')}")
    summary = res.summary()
    print("\n=== сводка ===")
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    print(f"подпись: Модель: {res.model} · {res.signature_tail()} · всего {time.monotonic() - t0:.0f} с")
    print("\n=== разбор ===")
    print(res.text)
    if out_dir:
        with open(os.path.join(out_dir, "answer.md"), "w", encoding="utf-8") as f:
            f.write(res.text + f"\n\n---\n*Модель: {res.model} · {res.signature_tail()}*\n")
        with open(os.path.join(out_dir, "run.json"), "w", encoding="utf-8") as f:
            json.dump({"summary": summary, "calls": res.calls, "llm_calls": ROWS}, f, ensure_ascii=False,
                      indent=1, default=str)
        print(f"\nфайлы: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
