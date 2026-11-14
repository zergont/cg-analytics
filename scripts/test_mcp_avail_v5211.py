"""v5.2.11 «Доступность регистров и правила промпта v2» (решение владельца 14.11.2026;
docs/plans/design_tool_rules_v2.md, разд. 7). Без БД: запросы подменены.

Опрос регистров ограничен настройкой роутера, опроса по требованию нет. Карта панели —
1353 регистра, с данными на установку — 88–137. Сервер телеметрии показывает моделям
перечень опрашиваемых и прямо отвечает про регистры вне опроса.

Раздел 1 — перечень опрашиваемых (telemetry_mcp.availability): основной источник —
таблица последних значений исходной БД (полный перечень; регистр без записей изменения
помечается «значение держится»), запасной — записи реплики (history, периоды состояний,
события неисправностей; неполон, формулировки осторожнее); кэш с обновлением в фоне,
отказы источников, разделение параметров, тексты пометок.
Раздел 2 — каталог регистров (build_catalog): по умолчанию только опрашиваемые, поиск по
ним, подсказка, когда слово есть только в карте панели, вся карта с пометками, роли KB без
данных, счёт в шапке, усечение, поведение при неизвестной доступности.
Раздел 3 — дерево оборудования: счёт опрашиваемых регистров; сервер: описание
инструмента, подсказки, срок кэша.
Раздел 4 — правила промпта, версия 2: состав и порядок, тексты, без подсказки про поиск
по всей карте, версия набора; задачи без инструментов не задеты.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_errors: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        _errors.append(msg)
        print("  ✗", msg)


import logging

logging.disable(logging.CRITICAL)

from telemetry_mcp import availability as av
from telemetry_mcp import tools_equipment as te
from telemetry_mcp.availability import Polled


def run(coro):
    return asyncio.run(coro)


UTC = timezone.utc
T_NOW = datetime(2026, 11, 14, 5, 0, tzinfo=UTC)
T_MAY = datetime(2026, 5, 25, 9, 0, tzinfo=UTC)
T_OCT = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)

# ── 1. Перечень опрашиваемых ─────────────────────────────────────────────────
print("1. Перечень опрашиваемых регистров")

parts = {
    "history": [{"addr": 40068, "t": T_NOW, "open": False}, {"addr": 40011, "t": T_NOW, "open": False}],
    "enum_history": [{"addr": 40011, "t": T_OCT, "open": True},      # есть и в history — берётся её время
                     {"addr": 40564, "t": T_MAY, "open": True},      # не менялся с начала хранения
                     {"addr": 40570, "t": T_OCT, "open": False}],
    "fault_history": [{"addr": 40401, "t": T_OCT, "open": False}],
}
m = av.merge(parts)
check(set(m) == {40068, 40011, 40564, 40570, 40401}, f"объединение трёх таблиц: {sorted(m)}")
check(m[40011] == Polled(T_NOW, False), "есть запись в history — её время, не период состояния")
check(m[40564] == Polled(T_MAY, True), "состояние без записей изменения — «держится с»")
check(m[40570] == Polled(T_OCT, False) and m[40401] == Polled(T_OCT, False), "закрытый период и событие неисправности")
check(av.merge({}) == {}, "нет данных — пусто")
check(av.last_text(m[40068], UTC) == "посл. запись 14.11.2026"
      and av.last_text(m[40564], UTC, exact=False) == "без изменений с 25.05.2026",
      "тексты даты последней записи (перечень по записям)")

# основной источник — таблица последних значений: перечень полный
latest = [{"addr": 40068, "ts": T_NOW, "value": 1500.0}, {"addr": 40011, "ts": T_NOW, "value": 3.0},
          {"addr": 40402, "ts": T_NOW, "value": 0.0},        # маска без единой неисправности: записей изменения нет
          {"addr": 40564, "ts": T_NOW, "value": 0.0}]
cm = av.combine(latest, m)
check(av.is_exact(cm) and set(cm) == {40068, 40011, 40402, 40564}, f"перечень из последних значений — полный: {sorted(cm)}")
check(cm[40402] == Polled(T_NOW, True, 0.0), "регистр в опросе без записей изменения — «значение держится» со значением")
check(cm[40068] == Polled(T_NOW, False, None) and cm[40564].held is False, "регистр с записями — обычный")
check(40570 not in cm and 40401 not in cm, "перечень — по таблице последних значений, а не по объединению с записями")
cr = av.combine(None, m)
check(av.is_exact(cr) is False and set(cr) == set(m), "нет таблицы последних значений — перечень по записям, неполный")
check(av.combine(None, None) is None, "нет ни одного источника — доступность не определена")
only_latest = av.combine(latest, None)
check(av.is_exact(only_latest) and not any(x.held for x in only_latest.values()), "записи не прочитаны — пометок «держится» нет")
check(av.is_exact(None) is True and av.is_exact({40068: Polled(T_NOW)}) is True, "обычный словарь считается полным перечнем")
check(av.last_text(cm[40402], UTC) == "значение не менялось (0)" and av.last_text(Polled(T_NOW, True, None), UTC) == "значение не менялось",
      "текст для регистра без записей изменения")
check(av.fmt_value(0.0) == "0" and av.fmt_value(49.866) == "49,866" and av.fmt_value(None) == "", "значение в пометке")
check("FROM latest_state" in av._SQL_LATEST and "router_sn = $1" in av._SQL_LATEST, "запрос последних значений по установке")
av.set_source_dsn("   ")
check(run(av._latest(("R1", "pcc", 1))) is None, "исходная БД не задана — основной источник пропускается")

sql = av._SQL.format(tab="history", col="ts", open="false")
check("CROSS JOIN LATERAL" in sql and "ORDER BY ts DESC LIMIT 1" in sql and "unnest($4::int[])" in sql,
      "запрос — по записи на адрес карты панели, по индексу")
check([s[0] for s in av._SOURCES] == ["history", "enum_history", "fault_history"], "три источника записей")

P = lambda addr, label="x": SimpleNamespace(addr=addr, label=label)
prefs = [P(40068, "Обороты"), P(40557, "Состояние стартера"), P(40564, "Команда останова")]
kept, missing = av.split_polled(prefs, m)
check([p.addr for p in kept] == [40068, 40564] and [p.addr for p in missing] == [40557], "опрашиваемые и нет")
check(av.split_polled(prefs, None) == (prefs, []), "доступность не определена — все считаются опрашиваемыми")
txt = av.unpolled_text(missing)
check(txt == "роутером не опрашивается, данных нет: 40557 «Состояние стартера» (перечень опрашиваемых — register_catalog)",
      f"пометка о регистре вне опроса: {txt}")
check(av.unpolled_text(missing, who="ДГУ №2").startswith("у ДГУ №2 роутером не опрашивается"), "пометка у соседа")
check("register_catalog" in av.NOTHING_POLLED and "не опрашивается" in av.NOTHING_POLLED, "ответ, когда не опрашивается ничего")
soft = av.unpolled_text(missing, exact=False)
check("записей нет" in soft and "не опрашивается или его значение не менялось" in soft and "роутером не опрашивается, данных нет" not in soft,
      f"перечень по записям — осторожная формулировка: {soft}")
check("не опрашиваются или их значения не менялись" in av.nothing_text(False) and av.nothing_text(True) == av.NOTHING_POLLED,
      "ответ «ничего нет» в двух режимах")
ht = av.held_text([P(40402, "Маска неисправностей №3"), P(40068, "Обороты")], cm)
check(ht is not None and "40402 «Маска неисправностей №3» = 0" in ht and "40068" not in ht and "значение держится" in ht,
      f"пометка о регистре без записей изменения: {ht}")
check(av.held_text([P(40068, "Обороты")], cm) is None and av.held_text([P(40564)], cr) is None and av.held_text([P(1)], None) is None,
      "нет таких регистров или перечень по записям — пометки нет")

# кэш и обновление в фоне
CALLS: list[tuple] = []
STATE = {"fail": False, "data": {40068: Polled(T_NOW)}}


async def fake_fetch(triple, addrs):
    CALLS.append((triple, len(addrs)))
    if STATE["fail"]:
        raise RuntimeError("БД недоступна")
    return av.PolledMap(STATE["data"])


_real_fetch, _real_ttl = av._fetch, av._ttl_sec
av._fetch = fake_fetch
TTL = {"v": 1800.0}
av._ttl_sec = lambda: TTL["v"]
unit = SimpleNamespace(triple=("R1", "pcc", 1), equip_type="pcc")
cat = {40068: {}, 40557: {}, 40564: {}}
try:
    async def _cache_case():
        av.reset_cache()
        a = await av.polled_registers(unit, cat)
        b = await av.polled_registers(unit, cat)
        out = {"first": a, "second_same": a is b, "calls_after_two": len(CALLS), "addrs": CALLS[0][1]}
        # срок вышел: отдаётся прежний перечень, обновление идёт в фоне
        TTL["v"] = 0.0
        STATE["data"] = {40068: Polled(T_NOW), 40564: Polled(T_MAY, True)}
        c = await av.polled_registers(unit, cat)
        out["stale_returned"] = c is a
        await asyncio.sleep(0.05)
        TTL["v"] = 1800.0
        d = await av.polled_registers(unit, cat)
        out["refreshed"] = sorted(d)
        out["calls_total"] = len(CALLS)
        # отказ при обновлении: остаётся прежний перечень
        STATE["fail"] = True
        TTL["v"] = 0.0
        e = await av.polled_registers(unit, cat)
        await asyncio.sleep(0.05)
        f = await av.polled_registers(unit, cat)
        out["after_fail"] = sorted(f) == sorted(d) and e is d
        # отказ без кэша — None
        av.reset_cache()
        out["no_cache_fail"] = await av.polled_registers(unit, cat)
        STATE["fail"] = False
        TTL["v"] = 1800.0
        av.reset_cache()
        out["empty_catalog"] = await av.polled_registers(unit, {})
        return out

    r = run(_cache_case())
    check(r["first"] == {40068: Polled(T_NOW)} and r["second_same"] and r["calls_after_two"] == 1 and r["addrs"] == 3,
          f"второй вызов — из кэша, запрос по адресам карты: {r}")
    check(r["stale_returned"], "после срока сразу отдаётся прежний перечень")
    check(r["refreshed"] == [40068, 40564] and r["calls_total"] == 2, f"перечень обновлён в фоне: {r['refreshed']}")
    check(r["after_fail"], "отказ при обновлении — прежний перечень остаётся")
    check(r["no_cache_fail"] is None, "отказ без кэша — доступность не определена")
    check(r["empty_catalog"] is None, "нет карты панели — доступность не определена")
finally:
    av._fetch, av._ttl_sec = _real_fetch, _real_ttl
    av.reset_cache()

# отказ вспомогательной таблицы не мешает, отказ history — ошибка
_real_query = av._query


async def q_enum_fails(triple, addrs, tab, col, opened):
    if tab == "enum_history":
        raise RuntimeError("нет таблицы")
    return [{"addr": 40068, "t": T_NOW, "open": False}] if tab == "history" else []


async def q_history_fails(triple, addrs, tab, col, opened):
    if tab == "history":
        raise RuntimeError("срок запроса")
    return []


_real_latest = av._latest


async def latest_ok(triple):
    return [{"addr": 40068, "ts": T_NOW, "value": 1500.0}, {"addr": 40402, "ts": T_NOW, "value": 0.0}]


async def latest_fails(triple):
    raise OSError("исходная БД недоступна")


async def latest_none(triple):
    return None


try:
    av._latest = latest_none
    av._query = q_enum_fails
    got = run(av._fetch(("R1", "pcc", 1), [40068]))
    check(got == {40068: Polled(T_NOW)} and av.is_exact(got) is False, "без исходной БД и таблицы состояний — перечень по history, неполный")
    av._query = q_history_fails
    check(run(av._fetch(("R1", "pcc", 1), [40068])) is None, "нет ни последних значений, ни записей — не определено")
    av._latest = latest_ok
    got = run(av._fetch(("R1", "pcc", 1), [40068]))
    check(av.is_exact(got) and set(got) == {40068, 40402} and not got[40402].held,
          "записи не прочитаны, последние значения есть — перечень полный, без пометок «держится»")
    av._query = q_enum_fails
    got = run(av._fetch(("R1", "pcc", 1), [40068]))
    check(av.is_exact(got) and got[40402] == Polled(T_NOW, True, 0.0) and got[40068].held is False,
          "оба источника: маска без записей изменения помечена «держится»")
    av._latest = latest_fails
    got = run(av._fetch(("R1", "pcc", 1), [40068]))
    check(av.is_exact(got) is False and set(got) == {40068}, "исходная БД недоступна — запасной перечень по записям")
finally:
    av._query, av._latest = _real_query, _real_latest

# ── 2. Каталог регистров ─────────────────────────────────────────────────────
print("2. Каталог регистров")

rm = {40068: {"role": "RPM", "kind": "analog", "unit": "rpm"},
      40595: {"role": "FREQ_REFERENCE", "kind": "analog", "unit": "Hz"},
      40999: {"role": "FUEL_RATE", "kind": "analog"}}
catalog = {40068: {"name_ru": "Обороты двигателя", "kind": "analog", "unit": "rpm"},
           40595: {"name_ru": "Итоговое задание частоты", "kind": "analog", "unit": "Hz"},
           40557: {"name_ru": "Состояние стартера", "kind": "enum", "states": {"labels_ru": {"0": "выкл", "1": "вкл"}}},
           43538: {"name_ru": "Команда стартера", "kind": "enum"},
           40564: {"name_ru": "Команда аварийного останова", "kind": "enum"},
           40999: {"name_ru": "Расход топлива", "kind": "analog"}}
polled = {40068: Polled(T_NOW), 40595: Polled(T_NOW), 40564: Polled(T_MAY, True)}

v = te.build_catalog(rm, catalog, polled, None, False, tz=UTC)
lines = v.text.split("\n")
check(lines[0].startswith("Опрашивается регистров: 3 из 6 в карте панели.") and "Показаны опрашиваемые" in lines[0],
      f"шапка со счётом: {lines[0]}")
check(lines[1] == "адрес | имя | ед. | вид | роль | опрос", "заголовок с колонкой опроса (краткий перечень, v5.2.12)")
check(v.total == 3 and [ln.split(" | ")[0] for ln in lines[2:]] == ["40068", "40564", "40595"], "по умолчанию — только опрашиваемые")
check(lines[2].endswith("| посл. запись 14.11.2026") and lines[3].endswith("| значение не менялось"), "дата последней записи в строке")
check(any("40999 (FUEL_RATE)" in n for n in v.notes), f"роль KB без данных — в пометках: {v.notes}")
check("40557" not in v.text and "43538" not in v.text, "регистров вне опроса в списке нет")

v = te.build_catalog(rm, catalog, polled, "задание", False, tz=UTC)
check(v.total == 1 and "40595 | Задание частоты (регулятор)" in v.text, "поиск по слову — среди опрашиваемых, без всей карты")
v = te.build_catalog(rm, catalog, polled, "ЗадаНие", False, tz=UTC)
check(v.total == 1, "поиск не зависит от регистра букв")

v = te.build_catalog(rm, catalog, polled, "стартер", False, tz=UTC)
check(v.total == 0 and "Среди опрашиваемых регистров по запросу «стартер» ничего нет" in v.text
      and "роутер их не опрашивает" in v.text and "40557 Состояние стартера" in v.text and "43538 Команда стартера" in v.text
      and "(2)" in v.text, f"слово есть только в карте панели — сказано прямо, с адресами: {v.text}")
v = te.build_catalog(rm, catalog, polled, "нетакого", False, tz=UTC)
check("ни среди опрашиваемых регистров, ни в карте панели" in v.text, "слова нет нигде")

v = te.build_catalog(rm, catalog, polled, None, True, tz=UTC)
rows = {ln.split(" | ")[0]: ln for ln in v.text.split("\n")[2:]}
check(v.total == 6 and "Показана вся карта панели" in v.text, "вся карта панели по просьбе")
check(rows["40557"].endswith("| не опрашивается") and rows["40999"].endswith("| не опрашивается")
      and rows["40068"].endswith("посл. запись 14.11.2026"), "в полной карте у регистров без данных пометка")

big_cat = {40000 + i: {"name_ru": f"Регистр {i}", "kind": "analog"} for i in range(400)}
big_polled = {a: Polled(T_NOW) for a in list(big_cat)[:200]}
v = te.build_catalog({}, big_cat, big_polled, None, False, tz=UTC)
check(v.total == 200 and len(v.text.split("\n")) == 2 + te.CATALOG_CAP and v.truncation
      and "показано 150 из 200" in v.truncation[0], f"усечение длинного списка: {v.truncation}")

v = te.build_catalog(rm, catalog, None, None, False)
check(v.total == 3 and "опрос" not in v.text.split("\n")[0] and "не опрашивается" not in v.text
      and any("определить не удалось" in n for n in v.notes), "доступность не определена — каталог как раньше, с пометкой")
check(te.build_catalog(rm, catalog, None, None, True).total == 6, "…и вся карта по просьбе, без пометок")
check("all_registers=true" in te.build_catalog(rm, catalog, None, "стартер", False).text, "…и прежняя подсказка при пустом поиске")
check(te.build_catalog(rm, catalog, {}, None, False).text.startswith("Опрашивается регистров: 0 из 6"), "пустой перечень — счёт ноль")

# перечень по записям (исходная БД недоступна): осторожные слова
soft_map = av.PolledMap({40068: Polled(T_NOW), 40564: Polled(T_MAY, True)})
soft_map.exact = False
v = te.build_catalog(rm, catalog, soft_map, None, True, tz=UTC)
rows = {ln.split(" | ")[0]: ln for ln in v.text.splitlines()[2:]}
check(v.text.startswith("Регистров с записями: 2 из 6") and "ни разу не менялся" in v.text.splitlines()[0],
      f"шапка в запасном режиме: {v.text.splitlines()[0]}")
check(rows["40557"].endswith("| записей нет") and rows["40564"].endswith("| без изменений с 25.05.2026")
      and "не опрашивается" not in "".join(rows.values()), "в запасном режиме — «записей нет», а не «не опрашивается»")
check(not any("нет в опросе" in n for n in v.notes), "в запасном режиме роли KB без записей не объявляются неопрашиваемыми")
v = te.build_catalog(rm, catalog, soft_map, "стартер", False, tz=UTC)
check("Среди регистров с записями" in v.text and "записей по ним нет" in v.text and "роутер их не опрашивает" not in v.text,
      "пустой поиск в запасном режиме")

# ── 3. Дерево оборудования и сервер ──────────────────────────────────────────
print("3. Дерево оборудования, описание инструмента, подсказки сервера")

_real_polled, _real_get_catalog = te.polled_registers, te.get_catalog


async def fake_polled(ref, catalog=None):
    if ref.triple[2] == 3:
        await asyncio.sleep(10)          # не успевает к сроку
    if ref.triple[2] == 2:
        return None                      # доступность не определена
    return {40068: Polled(T_NOW), 40595: Polled(T_NOW)}


async def fake_catalog(equip_type):
    return catalog


objects = [{"router_sn": "R1", "units": [{"panel": {"equip_type": "pcc", "panel_id": 1}},
                                         {"panel": {"equip_type": "pcc", "panel_id": 2}},
                                         {"panel": {"equip_type": "pcc", "panel_id": 3}}]}]
_wait = te.TREE_POLLED_WAIT_SEC
try:
    te.polled_registers, te.get_catalog = fake_polled, fake_catalog
    te.TREE_POLLED_WAIT_SEC = 0.2

    async def _tree_case():
        res = await te._tree_polled(objects)
        for t in asyncio.all_tasks() - {asyncio.current_task()}:
            t.cancel()
        return res

    res = run(_tree_case())
    check(res == {("R1", "pcc", 1): (2, 6)}, f"счёт только у установки с известным перечнем: {res}")
    check(run(te._tree_polled([])) == {}, "пустое дерево")
finally:
    te.polled_registers, te.get_catalog, te.TREE_POLLED_WAIT_SEC = _real_polled, _real_get_catalog, _wait

from telemetry_mcp.config import DEFAULT_LIMITS, McpSettings
from telemetry_mcp.server import INSTRUCTIONS, build_server

check(DEFAULT_LIMITS.get("availability_ttl_min") == 30, "срок кэша перечня — 30 минут")
check("опрашивает роутер" in INSTRUCTIONS and "register_catalog" in INSTRUCTIONS and "недостающий сигнал" in INSTRUCTIONS,
      "подсказки сервера говорят про перечень опрашиваемых")
srv = build_server(McpSettings())
tools = {t.name: t for t in run(srv.list_tools())}
desc = tools["register_catalog"].description
check("опрашивает" in desc and "не опрашивается" in desc and "all_registers=true" in desc, f"описание каталога: {desc[:120]}")
check(len(tools) == 13, f"инструментов по-прежнему 13: {len(tools)}")

for rel in ("telemetry_mcp/tools_series.py", "telemetry_mcp/tools_chart.py"):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        src = f.read()
    check("split_polled(prefs, polled)" in src and "nothing_text(is_exact(polled))" in src and "held_text(prefs, polled)" in src,
          f"{rel}: параметры делятся на опрашиваемые и нет, формулировка по режиму")
with open(os.path.join(ROOT, "telemetry_mcp/tools_series.py"), encoding="utf-8") as f:
    src = f.read()
check(src.count("split_polled(") >= 3 and "unpolled_text(gone, who=nb.name, exact=is_exact(npolled))" in src,
      "ряд, срез и тренд проверяют опрос; у соседа — свой перечень")

# ── 4. Правила промпта, версия 2 ─────────────────────────────────────────────
print("4. Правила промпта для инструментов, версия 2")

from llm import prompts

ids = [r.id for r in prompts.TOOL_RULES]
check(ids == ["R-T-DATA", "R-T-AVAIL", "R-T-UNIT", "R-T-ORDER", "R-T-HISTORY", "R-T-SETPOINT",
              "R-T-ENVELOPE", "R-T-HOLD", "R-T-REF", "R-T-ONCE"], f"состав и порядок правил: {ids}")
check(prompts.TOOLS_RULES_VERSION == "2", "версия набора правил — 2")
text, meta = prompts.system_for("incident", tools=True)
check(meta["rules"] == f"{prompts.PROMPT_RULES_VERSION}+t2" and meta["rule_ids"] == ids, f"версия промпта: {meta['version']}")
by_id = {r.id: prompts.rule_text(r) for r in prompts.TOOL_RULES}
check("которые опрашивает роутер этой установки" in by_id["R-T-AVAIL"] and "register_catalog" in by_id["R-T-AVAIL"]
      and "какого регистра недостаёт" in by_id["R-T-AVAIL"], "правило про доступные регистры")
check("с прошлыми пусками" in by_id["R-T-HISTORY"] and "segments за 7–14 суток" in by_id["R-T-HISTORY"]
      and "последний удачный" in by_id["R-T-HISTORY"], "правило про предысторию")
check("запрос «задание»" in by_id["R-T-SETPOINT"] and "в контуре управления" in by_id["R-T-SETPOINT"], "правило про задание регулятора")
check("за одну-две недели" in by_id["R-T-ONCE"] and "шире суток без нужды не запрашивай" in by_id["R-T-ONCE"],
      "правило про окна: списки за одну-две недели можно")
check(not any("all_registers" in t for t in by_id.values()), "подсказки про поиск по всей карте панели в правилах нет")
check(all(r.tools_only and r.tasks == prompts.TOOL_TASKS for r in prompts.TOOL_RULES), "правила — только для прогона с инструментами")
t0, m0 = prompts.system_for("incident")
check(prompts.RULES_HEADER not in t0 and m0["rules"] == prompts.PROMPT_RULES_VERSION, "без инструментов правила в промпт не входят")
for task in ("seg_auto", "warning_claude"):
    a, b = prompts.system_for(task), prompts.system_for(task, tools=True)
    check(a[0] == b[0] and a[1]["version"] == b[1]["version"], f"{task}: не задет")
check(len(prompts.RULES) == 6 and prompts.RULES[-1].id == "R-TZ", "основной набор правил не менялся")

print()
if _errors:
    print(f"Не пройдено проверок: {len(_errors)}")
    for msg in _errors:
        print("  -", msg)
    sys.exit(1)
print("Все проверки пройдены.")
