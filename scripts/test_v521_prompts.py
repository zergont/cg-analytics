# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""v5.2.1 «Механизм промптов» (docs/plans/design_v520_prompts.md §4, решения
владельца В1–В16 от 01.11.2026).

Проверяется без моделей (chat и anthropic подменены, при вызове пишут, что им
дали): одна сборка system на всех точках вызова (блок «Правила системы» ровно
один раз, правило из текста владельца не дублируется, выключатель снимает блок),
разовая миграция сохранённых промптов на настоящих текстах из БД (копия, CRLF,
«равно умолчанию» → "", построчные замены только при точном совпадении, маркер
только после успеха, повтор ничего не меняет), форма (только отличие, CRLF,
старая вкладка, совет не замораживается), prompt_version (меняется от текста,
правил и формата входа; пишется в debug_json, gate_log, warning_analyses.extra,
отметку совета), картина объекта (Сининда — две установки и соседи, ДЭС №3 —
без соседей, датчик привязан / не привязан / молчит, без серийных номеров),
наработка кодом (есть счётчик / нет), R-STOP только на своих путях, хеши
составов гейта и GATE_RULES_VERSION не изменились, эталон формата входа.

Запуск:  py -3 scripts/test_v521_prompts.py
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# anthropic в тестовом окружении может не стоять — подменяем модулем, который
# пишет вызов и отвечает заготовкой (сеть не нужна)
API_CALLS: list[dict] = []


class _Resp:
    def __init__(self, kw):
        self.content = [types.SimpleNamespace(type="text", text="## Сводка\nОтвет Claude")]
        if kw.get("tools"):
            self.content.append(types.SimpleNamespace(type="tool_use", name="verdict", id="t1",
                                                      input={"decision": "pass", "reason": "угроза"}))
        self.usage = types.SimpleNamespace(input_tokens=10, output_tokens=5)
        self.stop_reason = "end_turn"


class _Messages:
    async def create(self, **kw):
        API_CALLS.append(kw)
        return _Resp(kw)


class _FakeAnthropic:
    def __init__(self, **kw):
        self.messages = _Messages()


_fake_mod = types.ModuleType("anthropic")
_fake_mod.AsyncAnthropic = _FakeAnthropic
sys.modules["anthropic"] = _fake_mod

import corpus.settings as cs  # noqa: E402
import llm.client as client  # noqa: E402
import llm.prompts as prompts  # noqa: E402
import llm.router as router  # noqa: E402
import online.status_assembler as sa  # noqa: E402

cs.apply_claude_settings(model="claude-sonnet-5-5", max_tool_calls=3, max_tokens=1000, proxy="")
import config as _config  # noqa: E402
_config.set_tz("Europe/Moscow")        # подписи «МСК» — эталон входа не зависит от config.yml

_errors: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        _errors.append(msg)


def run(coro):
    return asyncio.run(coro)


def reset_router() -> None:
    for t in router.TASKS:
        router.apply_task(t, router.get_provider(t), "")
        prompts.note_saved(t, None)
    prompts.set_rules_enabled(True)


H = prompts.RULES_HEADER
SEG_RULES = ["R-STOP", "R-LIMIT", "R-NAME", "R-EQUIP", "R-HOURS", "R-TZ"]
GATE_RULES = ["R-STOP", "R-LIMIT", "R-NAME", "R-EQUIP", "R-TZ"]
from online.manager import _LLM_VERDICT_SUFFIX  # noqa: E402

# Настоящие тексты из БД (app_settings на 01.11, с CRLF, как их сохранила форма)
SAVED_SEG_AUTO = "\r\n".join([
    "Ты — технический аналитик дизельных генераторных установок Cummins с панелью управления PCC3300.",
    "", "КЛЮЧЕВОЙ ПРИНЦИП:",
    "Вердикт, уровень тревоги и список обнаружений ВЫНЕСЕНЫ детерминированным блоком аналитики.",
    "Твоя задача — ОБЪЯСНИТЬ зафиксированную картину, а не ставить собственный диагноз.",
    "НЕ пересматривай вердикт блока. НЕ ищи дополнительных проблем сверх обнаружений блока.",
    "Остановка двигателя, переходы на холостой ход, падение оборотов/давления/частоты при останове — "
    "ШТАТНЫЕ процессы. Если блок их не отметил — это не неисправность.",
    "Учитывай хронологию событий, у каждой неисправности есть время появления", "",
    "Если в отчёте есть раздел «Справочник кодов неисправностей» — используй эти данные как достоверную "
    "базу для объяснения. Не выдумывай описания кодов сам. ОБЯЗАТЕЛЬНО отмечай наиболее вероятные "
    "причины неисправности из этих данных. ",
    "", "ФОРМАТ ОТВЕТА — строго следуй структуре:", "", "## Сводка",
    "[1–2 строки: что происходило по сути. Обязательно укажи при какой наработке мотора формируется ответ.]",
    "", "## Разбор", "[Причина и контекст по обнаружениям блока.",
    "На штатном сегменте без отклонений: \"Отклонений не выявлено. Все параметры в норме.\"]", "",
    "## Рекомендации", "[Конкретные действия при отклонении.",
    "На штатном сегменте: \"Плановый контроль по регламенту ТО.\" или оставь пустым.]"])
SAVED_SEG_MANUAL = "\r\n".join([
    "Ты — технический аналитик дизельных генераторных установок Cummins с панелью управления PCC3300.",
    "", "КЛЮЧЕВОЙ ПРИНЦИП:",
    "Вердикт, уровень тревоги и список обнаружений ВЫНЕСЕНЫ детерминированным блоком аналитики.",
    "Твоя задача — ОБЪЯСНИТЬ зафиксированную картину, и поставить собственный диагноз.",
    "Критично оцени работы установки.",
    "Если в отчёте есть раздел «Справочник кодов неисправностей» — используй эти данные как достоверную "
    "базу для объяснения. Не выдумывай описания кодов сам. ОБЯЗАТЕЛЬНО отмечай наиболее вероятные "
    "причины неисправности из этих данных. ",
    "", "ФОРМАТ ОТВЕТА — строго следуй структуре:", "", "## Сводка",
    "[1–2 строки: что происходило по сути. Обязательно укажи при какой наработке мотора формируется ответ.]",
    "", "## Разбор", "[Причина и контекст по обнаружениям блока.",
    "На штатном сегменте без отклонений: \"Отклонений не выявлено. Все параметры в норме.\"]", "",
    "## Рекомендации", "[Конкретные действия при отклонении.",
    "На штатном сегменте: \"Плановый контроль по регламенту ТО.\" или оставь пустым.]"])
SAVED_GATE = "\r\n".join([
    "Ты — эксперт по дизель-генераторным установкам и фильтр ложных срабатываний.", "", "КОНТЕКСТ:",
    prompts.GATE_STRICT_OLD, "", "ТВОЯ ЗАДАЧА:",
    "1. Оцени реальность угрозы: запас до фактических порогов панели, режим работы, динамику ключевых "
    "параметров, типичность картины для данного режима.",
    "2. Дай краткий технический анализ: что зафиксировано, возможные причины, на что обратить внимание "
    "оператору. Ответ на русском языке.",
    "3. ОБЯЗАТЕЛЬНО заверши вызовом инструмента verdict:",
    "   - decision=\"cancel\" — срабатывание аналитики не отражает реальной угрозы (ложное/преждевременное); "
    "допустимо ТОЛЬКО когда в данных написано «cancel ДОПУСТИМ». «Общая авария» с несброшенным кодом, "
    "висевшая на начало сегмента, — сведения, снятие она не запрещает.",
    "   - decision=\"pass\" — угроза реальна или сомнения остаются; при сигналах панели WARNING/SHUTDOWN или "
    "«Общей аварии», появившейся в этом сегменте (в данных «отмена НЕДОСТУПНА»), — всегда pass.",
    "В reason укажи краткое обоснование решения (1-2 предложения).",
    "Сомневаешься — выбирай pass: пропустить лишнее предупреждение дешевле, чем скрыть реальную проблему.",
    "ОСОБЫЙ СЛУЧАЙ — АВАРИЙНЫЙ ОСТАНОВ:",
    "Если в данных присутствует блок «Контекст аварийного останова», машина уже остановлена панелью "
    "управления — оценка реальности угрозы не нужна (вердикт всегда pass). Твоя задача смещается на "
    "реконструкцию происшествия. Начни ответ с раздела «Условия перед аварией»: в каком режиме и с какой "
    "нагрузкой работала машина до отключения, как вели себя ключевые параметры за последние минуты "
    "(плавный дрейф или скачок), какие тревоги висели до аварии и как они связаны с сработавшим кодом. "
    "Затем — вероятная причина и что проверить перед повторным пуском. ",
    "Учитывай хронологию событий."])
SAVED_HUMAN = router.get_default_prompt("human_auto").replace("\n", "\r\n")   # = умолчанию с CRLF
SAVED_ANALYZE = router.get_default_prompt("analyze_page").replace("\n", "\r\n")
DB_PROMPTS = {"seg_auto": SAVED_SEG_AUTO, "seg_manual": SAVED_SEG_MANUAL, "warning_claude": SAVED_GATE,
              "human_auto": SAVED_HUMAN, "human_manual": SAVED_HUMAN, "analyze_page": SAVED_ANALYZE}

# ══ 1. Сборка system ═════════════════════════════════════════════════════════
reset_router()
for t in router.TASKS:
    txt, m = prompts.system_for(t)
    want = SEG_RULES if t in ("seg_auto", "seg_manual") else GATE_RULES if t == "warning_claude" else []
    check(m["rule_ids"] == want, f"1: правила {t}: {m['rule_ids']}")
    check(txt.count(H) == (1 if want else 0), f"1а: блок у {t} ровно {'один раз' if want else 'нет'}")
    check(txt.startswith(prompts.default_text(t)) and m["src"] == "code", f"1б: {t} — умолчание кода впереди")
    check(not m["conflicts"], f"1в: умолчание кода {t} без противоречий: {m['conflicts']}")
    if want:
        check(txt.rstrip().endswith(prompts.rule_text(prompts.RULES[-1])) and txt.index(H) > len(
            prompts.default_text(t)), f"1г: блок {t} — в конце system")
check("R-HOURS" not in prompts.system_for("warning_claude")[1]["rule_ids"], "1д: R-HOURS — только заключения")
# Гейт на шлюзе: маркер вердикта последним, после блока
g_llm, gm = prompts.system_for("warning_claude", suffix=_LLM_VERDICT_SUFFIX)
check(g_llm.endswith(_LLM_VERDICT_SUFFIX) and g_llm.count(H) == 1
      and g_llm.index(H) < g_llm.index("ВЕРДИКТ: cancel"), "1е: шлюз — блок, затем маркер вердикта")
check(gm["sha"] != prompts.system_for("warning_claude")[1]["sha"], "1ж: отпечаток с припиской — свой")
# Правило уже в тексте владельца — не дописывается
tz_rule = prompts.rule_text(next(r for r in prompts.RULES if r.id == "R-TZ"))
router.apply_task("seg_auto", "api", "Мой текст.\n- " + tz_rule.upper().replace(" ", "  "))
txt, m = prompts.system_for("seg_auto")
check("R-TZ" in m["skipped"] and "R-TZ" not in m["rule_ids"] and txt.count(H) == 1
      and tz_rule not in txt.split(H)[1], f"2: правило из текста владельца не повторено: {m}")
check(m["src"] == "owner" and txt.startswith("Мой текст."), "2а: текст владельца впереди")
# Выключатель снимает блок
prompts.set_rules_enabled(False)
txt, m = prompts.system_for("seg_auto")
check(H not in txt and m["rules"] == "off" and "·roff·" in m["version"] and not m["rule_ids"],
      f"2б: выключатель снимает блок: {m['version']}")
prompts.set_rules_enabled(True)
# CRLF: отпечаток и system не зависят от переводов строк
router.apply_task("seg_auto", "api", SAVED_SEG_AUTO)
crlf_txt, crlf_m = prompts.system_for("seg_auto")
router.apply_task("seg_auto", "api", SAVED_SEG_AUTO.replace("\r\n", "\n"))
lf_txt, lf_m = prompts.system_for("seg_auto")
check(crlf_m["sha"] == lf_m["sha"] and "\r" not in crlf_txt, "2в: CRLF не меняет отпечаток, в system — LF")
check(crlf_m["sha"] == prompts.system_for("seg_auto")[1]["sha"], "2г: отпечаток стабилен")
# Противоречия текста владельца — только пометка
ids = {c["rule"] for c in crlf_m["conflicts"]}
check({"R-HOURS", "R-EQUIP"} <= ids and crlf_txt.startswith(prompts.normalize(SAVED_SEG_AUTO)),
      f"2д: «укажи наработку», «Cummins с панелью» помечены, текст не вырезан: {crlf_m['conflicts']}")
router.apply_task("warning_claude", "api", SAVED_GATE)
gconf = {c["phrase"] for c in prompts.system_for("warning_claude")[1]["conflicts"]}
check("10% строже" in gconf and "особый случай — аварийный останов" in gconf, f"2е: гейт: {gconf}")
router.apply_task("seg_manual", "api", SAVED_SEG_MANUAL)
check(any("собственный диагноз" in c["phrase"] for c in prompts.system_for("seg_manual")[1]["conflicts"]),
      "2ж: seg_manual «поставить собственный диагноз» — помечен (противоречит шапке входа)")
reset_router()

# ══ 3. Все точки вызова берут system из system_for ══════════════════════════
CALL_SITES = ["corpus/worker.py", "corpus/agent.py", "corpus/humanizer.py", "online/manager.py",
              "online/incident_gate.py", "llm/client.py"]
for f in CALL_SITES:
    src = (ROOT / f).read_text(encoding="utf-8")
    check("get_prompt(" not in src and "system_for(" in src, f"3: {f} — system_for, без get_prompt(")
router.apply_task("seg_auto", "api", SAVED_SEG_AUTO)
router.apply_task("warning_claude", "api", SAVED_GATE)
SEEN: list[tuple[str, str]] = []


async def _chat(system, user, **k):
    SEEN.append(("chat", system))
    if k.get("meta") is not None:
        k["meta"].clear()           # как настоящий клиент: meta чистится на каждой попытке
    return "## Сводка\nОтвет шлюза"


async def _chat_stream(system, user, **k):
    SEEN.append(("stream", system))
    yield "т"


client.chat = _chat
client.chat_stream = _chat_stream
import corpus.worker as worker  # noqa: E402
import corpus.agent as agent  # noqa: E402
import corpus.humanizer as hum  # noqa: E402
import online.manager as mgr  # noqa: E402
import online.incident_gate as ig  # noqa: E402

worker._lang_clean = lambda text, src, depth: _ret((text, None))


async def _ret_coro(v):
    return v


def _ret(v):
    return _ret_coro(v)


# 3а. Воркер: system цепочки — system_for(task), след — debug_json.prompt
import corpus.db as cdb  # noqa: E402
import db.analytics as dban  # noqa: E402
W: dict = {"up": [], "prompt": None}
ROW0 = {"id": 7, "router_sn": "SN", "equip_type": "pcc", "panel_id": 1, "t_end": datetime.now(timezone.utc),
        "run_state": 3, "report_md": "отчёт", "characteristics_json": {}}


async def _w_none(*a, **k):
    return None


async def _w_row(sid):
    return dict(ROW0)


async def _w_up(sid, data):
    W["up"].append(data)


async def _w_chain(seg_row, kb, prompt, entries, depth=None):
    W["prompt"] = prompt
    W["pic"] = seg_row.get("object_picture")
    return {"verdict": "норма", "alarm_level": "НОРМА", "conclusion_md": "ИИ", "error": None,
            "debug_json": {"routing": []}, "claude_model": "ИИ"}


async def _w_setting(key, default=None):
    return "false"
_orig_w = (cdb.set_status, cdb.get_segment_row, cdb.upsert_analysis, cdb.get_equipment_kb_path,
           worker._analyse_segment_chain, dban.get_app_setting)
cdb.set_status, cdb.get_segment_row, cdb.upsert_analysis, cdb.get_equipment_kb_path = (
    _w_none, _w_row, _w_up, _w_none)
worker._analyse_segment_chain, dban.get_app_setting = _w_chain, _w_setting
try:
    for task in ("seg_auto", "seg_manual"):
        W["up"].clear()
        run(worker._process_segment(7, force=task == "seg_manual", task_id=task))
        want, wm = prompts.system_for(task)
        dj = (W["up"][-1] if W["up"] else {}).get("debug_json") or {}
        check(W["prompt"] == want and W["prompt"].count(H) == 1, f"3а: воркер {task} — system_for")
        check(dj.get("prompt", {}).get("version") == wm["version"] and dj.get("routing") == [],
              f"3б: debug_json.prompt у {task}: {dj.get('prompt')}")
        # Менеджера нет — запасной блок картины (правила не ссылаются в пустоту), признак False
        check(dj.get("picture") is False and (W["pic"] or "").startswith("Картина объекта: карта установок не загружена")
              and "не называй" in W["pic"], f"3б2: запасной блок картины и picture=false: {W['pic']!r}")
finally:
    (cdb.set_status, cdb.get_segment_row, cdb.upsert_analysis, cdb.get_equipment_kb_path,
     worker._analyse_segment_chain, dban.get_app_setting) = _orig_w

# 3в. Цепочка: шлюз получает переданный system; Claude-агент — тот же
SEEN.clear()
API_CALLS.clear()
sys_seg = prompts.system_for("seg_auto")[0]
gw_entry = {"id": "model", "name": "Горыныч", "type": "llm", "provider": "gorynych", "model": "fast",
            "max_ctx_tokens": 100000, "stream": False}
api_entry = {"id": "route:api", "name": "Claude API", "type": "api", "provider": "anthropic",
             "model": "claude-x", "max_ctx_tokens": 200000, "_route": True}
run(worker._analyse_segment_chain(dict(ROW0), None, sys_seg, [gw_entry], depth="seg_norma"))
check(SEEN and SEEN[-1] == ("chat", sys_seg), "3в: цепочка → шлюз: system_for")
res = run(worker._analyse_segment_chain(dict(ROW0), None, sys_seg, [api_entry], depth="seg_norma"))
check(API_CALLS and API_CALLS[-1]["system"] == sys_seg and not res.get("error"),
      f"3г: цепочка → Claude-агент: system_for ({res.get('error')})")
# 3д. Агент без system_prompt — сам берёт system_for("seg_auto")
API_CALLS.clear()
run(agent.analyse_segment(dict(ROW0), None))
check(API_CALLS and API_CALLS[-1]["system"] == sys_seg, "3д: агент без system_prompt — system_for")
# 3е. Пересказ: шлюз и Claude API
hmeta: dict = {}
for prov in ("llm", "api"):
    SEEN.clear()
    API_CALLS.clear()
    router.apply_depth("humanize", "fast", "default", "batch", provider=prov)
    hm: dict = {}
    run(hum.humanize("Блок 1\n## Сводка\nТекст", meta=hm))
    got = SEEN[-1][1] if prov == "llm" and SEEN else (API_CALLS[-1]["system"] if API_CALLS else None)
    check(got == prompts.system_for("human_auto")[0] and H not in (got or "")
          and hm.get("prompt", {}).get("task") == "human_auto",
          f"3е: пересказ ({prov}) — system_for, блок пуст, след в meta: {hm.get('prompt')}")
router.apply_depth("humanize", "fast", "default", "batch", provider="llm")
# 3ж. Гейт: Claude API (вердикт инструментом, без приписки) и шлюз (приписка последней)
API_CALLS.clear()
run(mgr._run_warning_gate_api("вход", "claude-x", cs.get_claude_settings()))
check(API_CALLS and API_CALLS[-1]["system"] == prompts.system_for("warning_claude")[0]
      and not API_CALLS[-1]["system"].endswith(_LLM_VERDICT_SUFFIX), "3ж: гейт Claude API — system_for")
SEEN.clear()
run(mgr._run_warning_gate_llm("вход", None, depth="gate_caution", meta={}))
check(SEEN and SEEN[-1][1] == prompts.system_for("warning_claude", suffix=_LLM_VERDICT_SUFFIX)[0],
      "3з: гейт шлюз — system_for с маркером вердикта")
# 3и. Совет: шлюз и Claude API (блок пуст)
SEEN.clear()
router.apply_depth("incident", "smart", "xhigh", "batch", provider="llm")
_lc_orig = None
import llm.lang_check as lc  # noqa: E402


async def _no_clean(text, src, **k):
    return text, None
_lc_orig, lc.clean_text = lc.clean_text, _no_clean
im: dict = {}
run(ig._ask("АКТ", im))
check(SEEN and SEEN[-1][1] == prompts.system_for("incident")[0] and H not in SEEN[-1][1],
      "3и: совет шлюз — system_for")
API_CALLS.clear()
run(ig._ask_claude("АКТ", "claude-x"))
check(API_CALLS and API_CALLS[-1]["system"] == prompts.system_for("incident")[0], "3к: совет Claude — system_for")
# 3л. «Ручной анализ»
SEEN.clear()


async def _drain():
    return [t async for t in client.stream_analysis("пакет")]
run(_drain())
check(SEEN and SEEN[-1] == ("stream", prompts.system_for("analyze_page")[0]), "3л: «Ручной анализ» — system_for")

# ══ 4. R-STOP только на своих путях (В9) ═════════════════════════════════════
stop_txt = prompts.rule_text(prompts.RULES[0])
for t in router.TASKS:
    has = stop_txt in prompts.system_for(t)[0]
    check(has == (t in ("seg_auto", "seg_manual", "warning_claude")), f"4: R-STOP у {t}: {has}")
check("действия по коду панели из входа" in stop_txt, "4а: при сигналах панели — по коду панели")
reset_router()

# ══ 5. Миграция сохранённых промптов (§4.5) ══════════════════════════════════
new, notes = prompts.migrate_prompt_text("seg_auto", SAVED_SEG_AUTO)
check(new is not None and "\r" not in new, "5: seg_auto изменён, LF")
check(new.split("\n")[0] == "Ты — технический аналитик дизель-генераторных установок.", "5а: строка 1")
check("Обязательно укажи при какой наработке" not in new and "[1–2 строки: что происходило по сути.]" in new,
      "5б: требование наработки убрано")
check("ОБЯЗАТЕЛЬНО отмечай" not in new and "о причине аварийного останова не суди — её разбирает совет." in new,
      "5в: причины — по справочнику")
check("Учитывай хронологию событий, у каждой неисправности есть время появления" in new,
      "5г: хронологию не трогаем (В2)")
check(len(new.split("\n")) == len(SAVED_SEG_AUTO.split("\r\n")) and sum(
    1 for a, b in zip(new.split("\n"), prompts.normalize(SAVED_SEG_AUTO).split("\n")) if a != b) == 3,
      "5д: изменены ровно 3 строки")
check(not {c["rule"] for c in prompts.conflicts_in("seg_auto", new)} & {"R-HOURS", "R-EQUIP"},
      f"5е: после миграции противоречий правилам нет: {prompts.conflicts_in('seg_auto', new)}")
newm, _ = prompts.migrate_prompt_text("seg_manual", SAVED_SEG_MANUAL)
check(newm is not None and "и поставить собственный диагноз" in newm and "Критично оцени" in newm
      and "Обязательно укажи" not in newm and newm.startswith("Ты — технический аналитик дизель-генераторных"),
      "5ж: seg_manual — три замены, остальное не тронуто (В3)")
newg, gnotes = prompts.migrate_prompt_text("warning_claude", SAVED_GATE)
check(newg is not None and "~10%" not in newg and prompts.GATE_STRICT_NEW in newg, "5з: «~10% строже» заменено")
check("ОСОБЫЙ СЛУЧАЙ" not in newg and "реконструкцию происшествия" not in newg
      and newg.endswith("чем скрыть реальную проблему.\nУчитывай хронологию событий."),
      f"5и: абзац «ОСОБЫЙ СЛУЧАЙ» удалён, хронология на месте: {newg[-120:]!r}")
check(prompts.migrate_prompt_text("human_auto", SAVED_HUMAN)[0] == ""
      and prompts.migrate_prompt_text("analyze_page", SAVED_ANALYZE)[0] == "", "5к: равно умолчанию (CRLF) → \"\"")
# Строка владельца изменена — не трогаем
edited = SAVED_SEG_AUTO.replace("Обязательно укажи при какой наработке мотора",
                                "Обязательно укажи при какой наработке ДВИГАТЕЛЯ")
e_new, e_notes = prompts.migrate_prompt_text("seg_auto", edited)
check(e_new is not None and "Обязательно укажи при какой наработке ДВИГАТЕЛЯ" in e_new
      and any("строки нет" in n for n in e_notes), "5л: изменённая владельцем строка не тронута, в журнал")
only = SAVED_GATE.replace(prompts.GATE_STRICT_OLD, "Мой контекст.").replace("ОСОБЫЙ СЛУЧАЙ", "Особо")
check(prompts.migrate_prompt_text("warning_claude", only)[0] is None, "5м: нечего менять — None (дата цела)")
check(prompts.migrate_prompt_text("seg_auto", "")[0] is None, "5н: пусто — уже следует коду")
# Старое умолчание кода (до v5.2.1) — тоже «следовать коду»
old_seg = router.get_default_prompt("seg_auto").replace(
    "Ты — технический аналитик дизель-генераторных установок.",
    "Ты — технический аналитик дизельного генераторного агрегата (ДГА) Cummins KTA50 с панелью управления PCC3300.")
check(prompts.migrate_prompt_text("seg_auto", old_seg.replace("\n", "\r\n"))[0] == "", "5о: прошлое умолчание → \"\"")
# п. 16: после замен текст совпал с умолчанием — тоже «следовать коду»
_dflt = router.get_default_prompt("seg_auto")
_near = _dflt.replace("Ты — технический аналитик дизель-генераторных установок.",
                      "Ты — технический аналитик дизельных генераторных установок Cummins с панелью управления PCC3300.")
_mn, _mnotes = prompts.migrate_prompt_text("seg_auto", _near.replace("\n", "\r\n"))
check(_mn == "" and any("после замен равен умолчанию" in x for x in _mnotes), f"5о2: после замен = умолчанию: {_mn!r}")

# Разовый проход по app_settings в памяти: копия, маркер последним, повтор — ничего
class Store:
    def __init__(self, data, fail_on=None):
        self.d, self.puts, self.fail_on = dict(data), [], fail_on

    async def get(self, key, default=None):
        return self.d.get(key, default)

    async def put(self, key, value):
        if self.fail_on and key == self.fail_on:
            raise RuntimeError("БД недоступна")
        self.puts.append(key)
        self.d[key] = value


st = Store({f"ai_task_{t}_prompt": v for t, v in DB_PROMPTS.items()})
applied: dict = {}
rep = run(prompts.run_prompts_migration(st.get, st.put, list(router.TASKS),
                                        apply=lambda t, v: applied.__setitem__(t, v)))
check(rep["done"] and not rep["skipped"] and st.puts[-1] == prompts.MIGRATION_KEY
      and st.d[prompts.MIGRATION_KEY] == prompts.PROMPTS_MIGRATION_VERSION, "5п: маркер — последним")
check(all(st.d[f"ai_task_{t}_prompt{prompts.BACKUP_SUFFIX}"] == v for t, v in DB_PROMPTS.items())
      and f"ai_task_incident_prompt{prompts.BACKUP_SUFFIX}" not in st.d, "5р: копии исходников (с CRLF)")
check(st.d["ai_task_human_auto_prompt"] == "" and st.d["ai_task_seg_auto_prompt"] == new
      and st.d["ai_task_warning_claude_prompt"] == newg and "ai_task_incident_prompt" not in st.d,
      "5с: значения по таблице, совета нет — не заводим")
check(set(applied) == {"seg_auto", "seg_manual", "warning_claude", "human_auto", "human_manual", "analyze_page"},
      f"5т: применено в роутере: {sorted(applied)}")
n_puts = len(st.puts)
rep2 = run(prompts.run_prompts_migration(st.get, st.put, list(router.TASKS)))
check(rep2["skipped"] and len(st.puts) == n_puts, "5у: повтор с маркером — ни одной записи")
# Повтор без маркера (как после сбоя) тоже ничего не меняет и копий не перезаписывает
st.d.pop(prompts.MIGRATION_KEY)
before = dict(st.d)
st.puts.clear()
rep3 = run(prompts.run_prompts_migration(st.get, st.put, list(router.TASKS)))
check(rep3["done"] and not rep3["changes"] and st.puts == [prompts.MIGRATION_KEY]
      and all(st.d[k] == v for k, v in before.items()), f"5ф: второй проход — без изменений: {st.puts}")
# Сбой посреди — маркера нет, следующий старт доводит; копия — исходник
st2 = Store({f"ai_task_{t}_prompt": v for t, v in DB_PROMPTS.items()}, fail_on="ai_task_warning_claude_prompt")
try:
    run(prompts.run_prompts_migration(st2.get, st2.put, list(router.TASKS)))
    failed = False
except RuntimeError:
    failed = True
check(failed and prompts.MIGRATION_KEY not in st2.d, "5х: сбой — маркер не ставится")
st2.fail_on = None
run(prompts.run_prompts_migration(st2.get, st2.put, list(router.TASKS)))
check(st2.d.get(prompts.MIGRATION_KEY) == prompts.PROMPTS_MIGRATION_VERSION
      and st2.d["ai_task_warning_claude_prompt"] == newg
      and st2.d[f"ai_task_seg_auto_prompt{prompts.BACKUP_SUFFIX}"] == SAVED_SEG_AUTO, "5ц: повтор после сбоя доводит")
reset_router()

# ══ 6. Форма (В8; ревью п. 14, 15, 18–20) ════════════════════════════════════
def page_form(rules_on: bool = True) -> dict:
    """Форма, как её отрисовала страница и прислал браузер (CRLF в textarea)."""
    f = {"prompts_version": prompts.PROMPTS_FORM_VERSION, prompts.RULES_PRESENT_FIELD: "1"}
    if rules_on:
        f[prompts.RULES_ENABLED_KEY] = "on"
    for t in router.TASKS:
        d = prompts.describe(t)
        f[f"ai_{t}_prompt"] = d["shown"].replace("\n", "\r\n")
        f[prompts.SHOWN_FIELD.format(task=t)] = d["shown_sha"]
    return f


fs = Store({f"ai_task_{t}_prompt": v for t, v in DB_PROMPTS.items()})
for t, v in DB_PROMPTS.items():
    router.apply_task(t, router.get_provider(t), v)
# п. 20: сохранённая копия умолчания (CRLF) — «следовать коду» и до миграции
check(prompts.owner_text("human_auto") == "" and prompts.system_for("human_auto")[1]["src"] == "code"
      and prompts.owner_text("seg_auto") == prompts.normalize(SAVED_SEG_AUTO), "6: копия умолчания — следует коду")
# Вкладка, открытая до выката: все поля с текстами, без версии формы, с правкой
stale_form = {f"ai_{t}_prompt": router.get_all()[t]["prompt"] for t in router.TASKS}
stale_form["ai_seg_auto_prompt"] += "\r\nПРАВКА"
r0 = run(prompts.save_prompts_form(stale_form, get=fs.get, put=fs.put))
check(r0["stale"] and not fs.puts, "6а: старая вкладка — ничего не записано")
r0b = run(prompts.save_prompts_form({**stale_form, "prompts_version": "5.1.34"}, get=fs.get, put=fs.put))
check(r0b["stale"] and not fs.puts, "6б: чужая версия формы — ничего не записано")
# Новая форма без правок — ни одной записи (дата владельца цела, совет не морозится)
r1 = run(prompts.save_prompts_form(page_form(), get=fs.get, put=fs.put))
check(not r1["stale"] and not r1["saved"] and not r1["conflicts"] and not fs.puts,
      f"6в: «Сохранить» без правок — ни одной записи: {fs.puts}")
check("ai_task_incident_prompt" not in fs.d, "6г: первое «Сохранить» не морозит промпт совета")
# Правка: CRLF → LF, применено, дата, история
f2 = page_form()
f2["ai_seg_auto_prompt"] = "Новый текст\r\nвладельца  \r\n"
run(prompts.save_prompts_form(f2, get=fs.get, put=fs.put))
check(fs.d["ai_task_seg_auto_prompt"] == "Новый текст\nвладельца" and router.get_owner_prompt("seg_auto")
      == "Новый текст\nвладельца" and prompts.saved_at("seg_auto") is not None, "6д: CRLF → LF, применено, дата")
check(prompts.HISTORY_KEY in fs.d, "6е: при сохранении — запись в историю")
# Равное умолчанию — "" (следовать коду): владелец вставил текст умолчания
fs.puts.clear()
f3 = page_form()
f3["ai_warning_claude_prompt"] = router.get_default_prompt("warning_claude").replace("\n", "\r\n")
run(prompts.save_prompts_form(f3, get=fs.get, put=fs.put))
check(fs.d["ai_task_warning_claude_prompt"] == "" and fs.puts.count("ai_task_warning_claude_prompt") == 1
      and not [k for k in fs.puts if k.startswith("ai_task_") and k != "ai_task_warning_claude_prompt"],
      f"6ж: вставлено умолчание — \"\", остальное не тронуто: {fs.puts}")
# п. 15: вкладка A открыта, во вкладке B — «Вернуть копию»; A сохраняет без правки
# поля — копия не откатывается; A правит поле — отказ по полю
fs.d[f"ai_task_seg_auto_prompt{prompts.BACKUP_SUFFIX}"] = SAVED_SEG_AUTO
tab_a = page_form()
check(not run(prompts.restore_backup("seg_auto", {}, get=fs.get, put=fs.put)), "6з: копия без версии формы — нет")
fs.puts.clear()
check(run(prompts.restore_backup("seg_auto", {"prompts_version": prompts.PROMPTS_FORM_VERSION},
                                 get=fs.get, put=fs.put))
      and router.get_owner_prompt("seg_auto") == prompts.normalize(SAVED_SEG_AUTO)
      and prompts.HISTORY_KEY in fs.puts, "6и: копия возвращена, история записана (п. 18)")
fs.puts.clear()
ra = run(prompts.save_prompts_form(tab_a, get=fs.get, put=fs.put))
check(not ra["saved"] and not fs.puts and router.get_owner_prompt("seg_auto") == prompts.normalize(SAVED_SEG_AUTO),
      f"6к: старая вкладка A без правок не откатывает копию: {fs.puts}")
tab_a["ai_seg_auto_prompt"] = "Правка во вкладке A"
ra2 = run(prompts.save_prompts_form(tab_a, get=fs.get, put=fs.put))
check(ra2["conflicts"] == ["seg_auto"] and not fs.puts, f"6л: правка поверх изменившегося — отказ: {ra2}")
# п. 18: кнопка «Вернуть копию» скрыта, если копия равна текущему тексту
check(prompts.describe("seg_auto", backup=SAVED_SEG_AUTO)["backup"] is False
      and prompts.describe("human_auto", backup=SAVED_HUMAN)["backup"] is False
      and prompts.describe("seg_manual", backup="другой текст")["backup"] is True, "6м: кнопка — только при отличии")
# п. 19: выключатель — только если поле было в форме; выключенный блок виден серым
fs.puts.clear()
nf = page_form(rules_on=False)
nf.pop(prompts.RULES_PRESENT_FIELD)
run(prompts.save_prompts_form(nf, get=fs.get, put=fs.put))
check(prompts.rules_enabled() and prompts.RULES_ENABLED_KEY not in fs.puts, "6н: без признака поля — выключатель цел")
run(prompts.save_prompts_form(page_form(rules_on=False), get=fs.get, put=fs.put))
d_off = prompts.describe("seg_auto")
check(fs.d.get(prompts.RULES_ENABLED_KEY) == "false" and not prompts.rules_enabled()
      and d_off["block"].startswith(H) and d_off["rules_on"] is False
      and H not in prompts.system_for("seg_auto")[0], "6о: чекбокс снят — блок выключен, в форме виден серым")
prompts.set_rules_enabled(True)
# Старый apply_prompts_form (роутер, только для тестов) — те же правила нормализации
router.apply_prompts_form({"ai_human_auto_prompt": SAVED_HUMAN})
check(router.get_owner_prompt("human_auto") == "", "6п: равное умолчанию — следовать коду")
# п. 17: дата после миграции — «после миграции 5.2.1 от …; сохранён владельцем …»
reset_router()
mst = Store({"ai_task_seg_auto_prompt": SAVED_SEG_AUTO})
router.apply_task("seg_auto", "api", SAVED_SEG_AUTO)
prompts.note_saved("seg_auto", datetime(2026, 10, 3, 15, 21, tzinfo=timezone.utc))
run(prompts.run_prompts_migration(mst.get, mst.put, ["seg_auto"],
                                  apply=lambda t, v: (router.apply_task(t, "api", v),
                                                      prompts.note_saved(t, datetime.now(timezone.utc))),
                                  owner_saved_at=prompts.saved_at))
st_txt = prompts.status_text(prompts.system_for("seg_auto")[1])
check(st_txt.startswith("текст владельца после миграции 5.2.1 от ") and "сохранён владельцем 03.10.2026 18:21 МСК" in st_txt
      and prompts.MIGRATION_INFO_KEY in mst.d, f"6р: дата миграции и дата владельца: {st_txt}")
router.apply_task("seg_auto", "api", "Владелец поправил после миграции")
check("после миграции" not in prompts.status_text(prompts.system_for("seg_auto")[1]), "6с: после правки — обычная дата")
check("умолчание кода ·" in prompts.status_text(prompts.system_for("incident")[1]), "6т: умолчание кода без версии (п. 21)")
prompts.set_migration_info({})
reset_router()

# ══ 7. prompt_version ════════════════════════════════════════════════════════
import corpus.preprocessor as prep  # noqa: E402
v0 = prompts.system_for("seg_auto")[1]["version"]
check(re.fullmatch(r"seg_auto·code·[0-9a-f]{10}·r5\.2\.1·in5\.2\.1", v0), f"7: формат версии: {v0}")
router.apply_task("seg_auto", "api", "Другой текст")
check(prompts.system_for("seg_auto")[1]["version"] != v0, "7а: смена текста — новая версия")
reset_router()
_r0 = prompts.RULES[0]
prompts.RULES = (prompts.Rule(_r0.id, _r0.text + " Ещё.", _r0.tasks, _r0.source),) + prompts.RULES[1:]
v_rules = prompts.system_for("seg_auto")[1]
prompts.RULES = (_r0,) + prompts.RULES[1:]
check(v_rules["version"] != v0, "7б: смена текста правил — новая версия")
_old_rv = prompts.PROMPT_RULES_VERSION
prompts.PROMPT_RULES_VERSION = "9.9.9"
check("·r9.9.9·" in prompts.system_for("seg_auto")[1]["version"], "7в: версия правил — в строке")
prompts.PROMPT_RULES_VERSION = _old_rv
_old_iv = prep.CONCLUSION_INPUT_VERSION
prep.CONCLUSION_INPUT_VERSION = "9.9.9"
v_in = prompts.system_for("seg_auto")[1]["version"]
prep.CONCLUSION_INPUT_VERSION = _old_iv
check(v_in != v0 and v_in.endswith("·in9.9.9"), f"7г: смена формата входа — новая версия: {v_in}")
check(prompts.system_for("warning_claude")[1]["input"] == sa.GATE_INPUT_VERSION
      and prompts.system_for("incident")[1]["input"] == ig.INCIDENT_INPUT_VERSION, "7д: версии входа гейта и совета")
# В журнал гейта и в запись разбора
import online.db as odb  # noqa: E402
T = datetime(2026, 11, 1, 6, 0, tzinfo=timezone.utc)
LP = {"scenario": "LIMIT_PROXIMITY", "severity": "CAUTION", "fault_codes": [], "ref_codes": [214],
      "related_roles": ["OIL_TEMP"], "trigger": "Температура масла: 100 в пределах 10% от порога 110", "values": {}}
STRUCT = sa.build_structural_status({"id": 1, "run_state": 3, "t_start": T, "characteristics_json": {},
                                     "current_values_json": {"values": {}}, "active_detections_json": [LP]})
G: dict = {"events": [], "saved": [], "prompt": None}


async def _g_open(*a):
    return {"id": 1, "t_start": T, "warning_analyses": []}


async def _g_empty(*a, **k):
    return []


async def _g_counts(*a, **k):
    return {}


async def _g_true(*a, **k):
    return True


async def _g_event(*a, event=None, **k):
    G["events"].append(event)
    return True


async def _g_save(*a, extra=None, **k):
    G["saved"].append(extra)
    return True


async def _g_mark(*a, **k):
    return []


async def _g_late(*a, **k):
    return False, False


async def _g_llm(prompt, model, depth=None, meta=None):
    G["prompt"] = prompt
    return "Анализ.", "pass", "угроза", 10, 5
_G_NAMES = ("get_open_segment", "get_run_state_origin_ts", "count_episodes_batch", "get_open_episodes",
            "get_episodes_overlapping", "set_segment_gate_suppression", "append_segment_gate_event",
            "save_segment_warning", "set_episodes_gate_suppressed", "resolve_segment_id",
            "apply_late_gate_suppression")
_orig_g = {n: getattr(odb, n) for n in _G_NAMES}
for n, f in zip(_G_NAMES, (_g_open, _w_none, _g_counts, _g_empty, _g_empty, _g_true, _g_event, _g_save,
                           _g_mark, _w_none, _g_late)):
    setattr(odb, n, f)
_orig_llm = mgr._run_warning_gate_llm
mgr._run_warning_gate_llm = _g_llm
router.apply_depth("gate_caution", "fast", "low", "interactive", provider="llm")
PIC = "Картина объекта (карта установок):\nОбъект: Тест."
try:
    run(mgr._analyze_warning_claude("SN", "pcc", 1, dict(STRUCT), "fh1", object_picture=PIC))
    want_v = prompts.system_for("warning_claude", suffix=_LLM_VERDICT_SUFFIX)[1]["version"]
    ge = G["events"][-1] if G["events"] else {}
    check(ge.get("prompt") == want_v and ge.get("rules") == sa.GATE_RULES_VERSION,
          f"7е: gate_log — версия промпта: {ge.get('prompt')}")
    check(G["saved"] and (G["saved"][-1] or {}).get("prompt") == want_v, "7ж: warning_analyses.extra.prompt")
    check(ge.get("picture") is True and (G["saved"][-1] or {}).get("picture") is True,
          "7ж2: признак картины объекта — в gate_log и в записи разбора (п. 8)")
    gp = G["prompt"] or ""
    check(gp.startswith("Выполни анализ неисправности дизель-генераторной установки.\n\n" + PIC + "\n\nРежим работы:"),
          f"7з: картина объекта — вторым во входе гейта: {gp[:160]!r}")
finally:
    for n, f in _orig_g.items():
        setattr(odb, n, f)
    mgr._run_warning_gate_llm = _orig_llm
# Совет: версия — в отметку заказа и в запись разбора
I: dict = {"events": [], "saved": []}


async def _i_state(*a, **k):
    return {"analysed": False, "terminal": None, "ordered": 0}


async def _i_append(sn, eq, panel, event, segment_id=None, ts=None):
    I["events"].append(event)
    return True


async def _i_save(*a, extra=None, **k):
    I["saved"].append(extra)
    return True


async def _i_material(*a, **k):
    return "АКТ"
_orig_i = (odb.get_incident_analysis_state, odb.append_segment_gate_event, odb.save_segment_warning,
           ig._material)
odb.get_incident_analysis_state, odb.append_segment_gate_event, odb.save_segment_warning = (
    _i_state, _i_append, _i_save)
ig._material = _i_material
try:
    run(ig._run_incident("SN", "pcc", 1, {"stop_ts": T.isoformat()}, T, 5))
    iv = prompts.system_for("incident")[1]["version"]
    ordered = [e for e in I["events"] if e.get("decision") == "ordered"]
    check(ordered and ordered[0].get("prompt") == iv and ordered[0].get("picture") is False,
          f"7и: отметка заказа совета — версия и признак картины: {ordered}")
    # Заглушка шлюза чистит meta, как настоящий клиент (ревью п. 13): версия всё равно в записи
    check(I["saved"] and (I["saved"][-1] or {}).get("prompt") == iv and (I["saved"][-1] or {}).get("picture") is False,
          f"7к: разбор совета — extra.prompt при очищенной meta: {I['saved']}")
finally:
    (odb.get_incident_analysis_state, odb.append_segment_gate_event, odb.save_segment_warning,
     ig._material) = _orig_i
    lc.clean_text = _lc_orig
# История по отпечатку
hist = prompts.history_add(None, [("a" * 10, "seg_auto", "текст")], datetime(2026, 11, 1, tzinfo=timezone.utc))
check(prompts.history_add(hist, [("a" * 10, "seg_auto", "текст")], datetime(2026, 11, 1, tzinfo=timezone.utc)) is None,
      "7л: тот же отпечаток в тот же момент — без записи")
many = None
for i in range(60):
    many = prompts.history_add(many, [(f"{i:010d}", "seg_auto", str(i))],
                               datetime(2026, 11, 1, tzinfo=timezone.utc) + timedelta(minutes=i)) or many
hm_ = json.loads(many)
check(len(hm_) == prompts.HISTORY_MAX and f"{59:010d}" in hm_ and f"{0:010d}" not in hm_, "7м: история ≤ 50, свежие")

# ══ 8. Картина объекта (В11; ревью п. 1, 3–12) ═══════════════════════════════
from db.units import build_tree  # noqa: E402
import llm.object_picture as op  # noqa: E402
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
REG = [
    {"router_sn": "6003790403", "equip_type": "pcc", "panel_id": 1, "name": "ДГУ №1 инв. 2569",
     "manufacturer": "Cummins", "model": "KTA50", "engine_sn": "25428546", "controller_id": "pcc3300",
     "engine_id": "cummins_kta50", "kb_path": "cummins_kta50_pcc3300", "active": True},
    {"router_sn": "6003790403", "equip_type": "pcc", "panel_id": 2, "name": "ДГУ №2 инв. 2568",
     "manufacturer": "Cummins", "model": "KTA50", "engine_sn": "25428547", "controller_id": "pcc3300",
     "engine_id": "cummins_kta50", "kb_path": "cummins_kta50_pcc3300", "active": True},
    {"router_sn": "6003790403", "equip_type": "yts61", "panel_id": 1, "name": "YTS-61",
     "manufacturer": "Yateks", "model": "YTS-61", "engine_sn": None, "controller_id": None,
     "engine_id": None, "kb_path": None, "active": True},
    {"router_sn": "6003790403", "equip_type": "yfm8", "panel_id": 9, "name": "Вибро",
     "manufacturer": None, "model": None, "engine_sn": None, "controller_id": None,
     "engine_id": None, "kb_path": None, "active": True},
    {"router_sn": "1126246373", "equip_type": "pcc", "panel_id": 1, "name": "ДЭС №3",
     "manufacturer": "Cummins", "model": "QSX15", "engine_sn": "41377550", "controller_id": "pcc3300",
     "engine_id": "cummins_qsx15", "kb_path": None, "active": True},
]
SENS = [{"router_sn": "6003790403", "equip_type": "yts61", "panel_id": 1, "unit_id": None, "role": "качество масла",
         "name": None}]
# Установки заведены (имени владельца нет — имя реестра), датчик масла явно привязан к ДГУ №1
UNITS = [{"id": 1, "router_sn": "6003790403", "name": None, "panel_type": "pcc", "panel_id": 1, "notes": None, "active": True},
         {"id": 2, "router_sn": "6003790403", "name": None, "panel_type": "pcc", "panel_id": 2, "notes": None, "active": True},
         {"id": 3, "router_sn": "1126246373", "name": None, "panel_type": "pcc", "panel_id": 1, "notes": None, "active": True}]
SENS_ATT = [dict(SENS[0], unit_id=1)]
FRESH = {("6003790403", "pcc", 1): NOW, ("6003790403", "pcc", 2): NOW - timedelta(minutes=5),
         ("1126246373", "pcc", 1): NOW}
OBS = {k: "running" for k in FRESH}
SEEN_TM = {("6003790403", "yts61", 1): NOW - timedelta(hours=2), ("6003790403", "yfm8", 9): NOW - timedelta(minutes=1)}
NAMES = {"6003790403": "Сининда-1", "1126246373": "Новопетровское"}


def ctx_of(reg=REG, units=UNITS, sens=SENS_ATT, names=NAMES, fresh=FRESH, obs=OBS, seen=SEEN_TM):
    return {"tree": op.annotate(build_tree(reg, units, sens, object_names=names or {}, freshness=fresh,
                                           observation_status=obs), seen=seen, now=NOW),
            "names_ok": names is not None}


ctx = ctx_of()
p1 = op.render(ctx, "6003790403", "pcc", 1, now=NOW, live=True)
lines1 = p1.split("\n")
check(lines1[0] == op.PICTURE_HEADER
      and lines1[1] == "Объект: Сининда-1. Установки объекта (в одной сети): ДГУ №1 инв. 2569, ДГУ №2 инв. 2568."
      and lines1[2] == "Эта установка: ДГУ №1 инв. 2569 — двигатель Cummins KTA50.", f"8: Сининда:\n{p1}")
check(lines1[3] == "Источники данных: панель Cummins PowerCommand PCC3300 (двигатель, генератор, режимы, "
                   "коды неисправностей); датчик YTS-61 (качество масла) — данных нет.",
      f"8а: панель из KB, датчик привязан, по телеметрии молчит 2 ч: {lines1[3]!r}")
check(lines1[4] == "Соседи: ДГУ №2 инв. 2568 — Cummins KTA50, панель PCC3300.", f"8б: соседи: {lines1[4]!r}")
check(lines1[5] == "Датчики объекта без привязки к установке: датчик Вибро.", f"8в: не привязан, на связи: {lines1[5]!r}")
p1c = op.render(ctx, "6003790403", "pcc", 1, now=NOW)       # заключение / совет: не живая свежесть
check("датчик YTS-61 (качество масла) — сейчас нет данных." in p1c and "— данных нет" not in p1c,
      f"8а2: в заключении — «сейчас нет данных», не «данных нет» (п. 6):\n{p1c}")
# Свежесть датчика не известна (телеметрия не ответила) — «данных нет» не пишем (п. 1)
p_unk = op.render(ctx_of(seen=None), "6003790403", "pcc", 1, now=NOW, live=True)
check("; датчик YTS-61 (качество масла)." in p_unk and "Вибро." in p_unk, f"8а3: свежесть не известна:\n{p_unk}")
# Правило по умолчанию (тот же роутер и панель): датчик — у ДГУ №1, без описания
p_def = op.render(ctx_of(units=[], sens=[]), "6003790403", "pcc", 1, now=NOW, live=True)
check("; датчик YTS-61 — данных нет." in p_def, f"8а4: датчик по правилу умолчания, без описания:\n{p_def}")
# Описание датчика — из слоя KB его привязки (раньше роли карты)
import time as _time  # noqa: E402
op._labels_cache[("sensor", "yts61")] = (_time.monotonic(), {"measures": "вязкость и диэлектрика масла"})
REG_KB = [dict(r, controller_id="yts61") if r["equip_type"] == "yts61" else r for r in REG]
p_kb = op.render(ctx_of(reg=REG_KB), "6003790403", "pcc", 1, now=NOW, live=True)
check("датчик YTS-61 (вязкость и диэлектрика масла) — данных нет" in p_kb, f"8а5: описание датчика из KB:\n{p_kb}")
p2 = op.render(ctx, "6003790403", "pcc", 2, now=NOW, live=True)
check("Эта установка: ДГУ №2 инв. 2568" in p2 and "Соседи: ДГУ №1 инв. 2569 — Cummins KTA50, панель PCC3300, "
      "датчики: YTS-61." in p2, f"8г: у ДГУ №2 сосед — ДГУ №1 со своим датчиком:\n{p2}")
p3 = op.render(ctx, "1126246373", "pcc", 1, now=NOW, live=True)
check("Объект: Новопетровское. На объекте одна установка — соседей нет." in p3 and "Соседи:" not in p3
      and "двигатель Cummins QSX15" in p3, f"8д: ДЭС №3 — без соседей:\n{p3}")
for p in (p1, p2, p3):
    check(not any(x in p for x in ("6003790403", "1126246373", "25428546", "25428547", "41377550", "pcc/", "yts61/")),
          f"8е: без серийных номеров роутера и двигателя, без троек:\n{p}")
# Сосед: молчит (живая свежесть движков), «данных нет» — при панели, до датчиков (п. 5);
# остановлен (СТОП) — «наблюдение остановлено»; без наблюдения — «наблюдение не ведётся» (п. 6)
p4 = op.render(ctx, "6003790403", "pcc", 2, now=NOW, live=True,
               freshness={("6003790403", "pcc", 1): NOW - timedelta(hours=3)})
check("ДГУ №1 инв. 2569 — Cummins KTA50, панель PCC3300 — данных нет, датчики: YTS-61." in p4,
      f"8ж1: сосед молчит — пометка у панели:\n{p4}")
p4s = op.render(ctx_of(obs={**OBS, ("6003790403", "pcc", 2): "stopped"}), "6003790403", "pcc", 1, now=NOW, live=True)
p4n = op.render(ctx_of(obs={k: v for k, v in OBS.items() if k != ("6003790403", "pcc", 2)}),
                "6003790403", "pcc", 1, now=NOW, live=True)
check("панель PCC3300 — наблюдение остановлено." in p4s and "панель PCC3300 — наблюдение не ведётся." in p4n,
      f"8ж2: сосед после СТОП / без наблюдения:\n{p4s}\n{p4n}")
# Явное «не привязан» и выключенная установка; имени объекта нет; имена не прочитаны
units3 = [dict(UNITS[0]), dict(UNITS[1], active=False)]
p5 = op.render(ctx_of(units=units3, sens=SENS, names={}), "6003790403", "pcc", 1, now=NOW, live=True)
check(p5.split("\n")[1] == "Объект: имя не задано. На объекте одна установка — соседей нет."
      and "Источники данных: панель Cummins PowerCommand PCC3300 (двигатель, генератор, режимы, коды неисправностей)."
      in p5 and "без привязки к установке: датчик YTS-61 (качество масла) — данных нет; датчик Вибро." in p5,
      f"8з: объект без имени, сосед выключен, датчик явно не привязан:\n{p5}")
p5b = op.render(ctx_of(names=None), "6003790403", "pcc", 1, now=NOW, live=True)
check(p5b.split("\n")[1].startswith("Объект: имя объекта не прочитано."), f"8з2: телеметрия не ответила (п. 9):\n{p5b}")
check(op.render(ctx, "6003790403", "pcc", 7) is None and op.render(None, "x", "pcc", 1) is None,
      "8и: установки нет в карте — None")
# п. 3, 9, 10: одна очистка имён — серийники роутера и двигателей всех панелей объекта и тройки
REG_SN = [dict(REG[0], name="ДГУ №1 s/n 25428546"), dict(REG[1], name=None),
          dict(REG[2], name="YTS-61 6003790403"), dict(REG[3], name="Вибро (25428547)")]
p6 = op.render(ctx_of(reg=REG_SN, units=[], sens=[], names={"6003790403": "Сининда-1 (6003790403)"}),
               "6003790403", "pcc", 1, now=NOW, live=True)
check("Объект: Сининда-1. Установки объекта (в одной сети): ДГУ №1, без имени (панель 2)." in p6
      and "Эта установка: ДГУ №1 — " in p6 and "датчик YTS-61 — данных нет" in p6 and "датчик Вибро." in p6
      and "Соседи: без имени (панель 2) — " in p6
      and not any(x in p6 for x in ("6003790403", "25428546", "25428547", "s/n", "pcc/")),
      f"8к: серийники вырезаны, безымянная — «без имени (панель N)»:\n{p6}")
check(op.clean_name("ДГУ №1 s/n 25428546", {"25428546"}, set()) == "ДГУ №1"
      and op.clean_name("pcc/1", set(), {"pcc/1"}) == "", "8к2: clean_name")
# п. 4: карты нет — запасной блок по движку; без движка — «не называй»
fb_eng = types.SimpleNamespace(equip_name="ДЭС №3", equip_engine="Cummins QSX15",
                               cfg=types.SimpleNamespace(controller_label="Cummins PowerCommand PCC3300",
                                                         engine_label=None))
fb = op.engine_fallback(types.SimpleNamespace(_engines={"1126246373|pcc|1": fb_eng}), "1126246373", "pcc", 1)
check(fb == "Картина объекта: карта установок не загружена.\nЭта установка: ДЭС №3 — двигатель Cummins QSX15, "
           "панель Cummins PowerCommand PCC3300." and not op.is_full(fb) and op.is_full(p1)
      and "не называй" in op.engine_fallback(None, "x", "pcc", 1), f"8л: запасной блок: {fb!r}")

# 8м. Боевой путь (п. 1, 7, 11): менеджер сервиса грузит карту (реестр, установки, датчики,
# наблюдения — БД аналитики; имена и свежесть датчиков — телеметрия), гейт берёт её из
# памяти с живой свежестью движков, заключение — через picture_for
import online.db as odb  # noqa: E402
import db.units as dbu  # noqa: E402


async def _m_reg():
    return [dict(r) for r in REG]


async def _m_units():
    return [dict(u) for u in UNITS]


async def _m_sens():
    return [dict(s) for s in SENS_ATT]


async def _m_obs():
    return [{"router_sn": k[0], "equip_type": k[1], "panel_id": k[2], "last_data_ts": v, "status": "running"}
            for k, v in FRESH.items()]


async def _m_names():
    return dict(NAMES)


async def _m_seen():
    return {("6003790403", "yts61", 1): datetime.now(timezone.utc) - timedelta(hours=2),
            ("6003790403", "yfm8", 9): datetime.now(timezone.utc) - timedelta(minutes=1)}
_orig_m = (dban.get_equipment_registry, dbu.list_units, dbu.list_unit_sensors, odb.list_observations,
           op._object_names, op._telemetry_last_seen, mgr._manager)
dban.get_equipment_registry, dbu.list_units, dbu.list_unit_sensors, odb.list_observations = (
    _m_reg, _m_units, _m_sens, _m_obs)
op._object_names, op._telemetry_last_seen = _m_names, _m_seen
try:
    m = mgr.OnlineManager()
    mgr._manager = m
    now_ = datetime.now(timezone.utc)
    m._engines = {"6003790403|pcc|1": types.SimpleNamespace(router_sn="6003790403", equip_type="pcc", panel_id=1,
                                                            cfg=None, last_data_ts=now_),
                  "6003790403|pcc|2": types.SimpleNamespace(router_sn="6003790403", equip_type="pcc", panel_id=2,
                                                            cfg=None, last_data_ts=now_ - timedelta(hours=1))}
    check(run(m.refresh_object_pictures()) and m._picture_ctx is not None, "8м: карта загружена менеджером")
    g_pic = m.object_picture_cached("6003790403", "pcc", 1)
    check("датчик YTS-61 (качество масла) — данных нет" in g_pic and "датчик Вибро." in g_pic
          and "ДГУ №2 инв. 2568 — Cummins KTA50, панель PCC3300 — данных нет." in g_pic,
          f"8м2: гейт — свежесть датчиков из телеметрии, соседа — из движка:\n{g_pic}")
    c_pic, c_full = run(op.picture_for("6003790403", "pcc", 1))
    check(c_full and "датчик YTS-61 (качество масла) — сейчас нет данных" in c_pic
          and "панель PCC3300 — сейчас нет данных." in c_pic, f"8м3: заключение — «сейчас нет данных»:\n{c_pic}")
    # Телеметрия не ответила — имя «не прочитано», свежесть датчиков не пишется, срок кэша короткий
    async def _none_async():
        return None
    op._object_names, op._telemetry_last_seen = _none_async, _none_async
    run(m.refresh_object_pictures())
    t_pic = m.object_picture_cached("6003790403", "pcc", 1)
    check("имя объекта не прочитано" in t_pic and "датчик YTS-61 (качество масла);" not in t_pic
          and "датчик YTS-61 (качество масла)." in t_pic.replace(";", ".") and m._picture_ctx["ttl"] == 60.0,
          f"8м4: без телеметрии:\n{t_pic}")
    # п. 7: загрузка, начатая раньше, но закончившая позже, не затирает свежую
    _real_load = op.load_context
    _order = iter([0.05, 0.0])

    async def _slow_load(cfgs=None):
        d = next(_order)
        tag = "old" if d else "new"
        await asyncio.sleep(d)
        return {"tree": {"objects": []}, "at": _time.monotonic(), "tag": tag}
    op.load_context = _slow_load

    async def _race():
        await asyncio.gather(m.refresh_object_pictures(), m.refresh_object_pictures())
    run(_race())
    op.load_context = _real_load
    check(m._picture_ctx.get("tag") == "new", f"8м5: устаревшая загрузка отброшена: {m._picture_ctx.get('tag')}")
    # Нет картины в кэше — гейт получает запасной блок по движку
    m._picture_ctx = {"tree": {"objects": []}, "at": _time.monotonic(), "ttl": 600}
    m._engines["6003790403|pcc|1"].equip_name = "ДГУ №1 инв. 2569"
    m._engines["6003790403|pcc|1"].equip_engine = "Cummins KTA50"
    check(m.object_picture_cached("6003790403", "pcc", 1).startswith(
        "Картина объекта: карта установок не загружена.\nЭта установка: ДГУ №1 инв. 2569 — двигатель Cummins KTA50"),
        "8м6: гейт без карты — запасной блок по движку")
finally:
    (dban.get_equipment_registry, dbu.list_units, dbu.list_unit_sensors, odb.list_observations,
     op._object_names, op._telemetry_last_seen, mgr._manager) = _orig_m
# п. 11: загрузка карты — не в каждой сборке движка, а после цикла старта и на ПУСК
_msrc = (ROOT / "online" / "manager.py").read_text(encoding="utf-8")
_be = _msrc[_msrc.index("async def _build_engine"):_msrc.index("def _engine_cfgs")]
check("schedule_picture_refresh" not in _be and _msrc.count("self.schedule_picture_refresh()") >= 2,
      "8н: карта не грузится в _build_engine")
# Описание из KB: ключ есть в слое контроллера и в монолите; без ключа — по ролям карты
from analytics.config import AnalyticsConfig  # noqa: E402
KB = ROOT / "knowledge_base"
c_pair = AnalyticsConfig.from_pair(KB, "pcc3300", "cummins_qsx15")
c_mono = AnalyticsConfig(KB / "equipment" / "cummins_kta50_pcc3300")
check(c_pair.controller_measures == c_mono.controller_measures == "двигатель, генератор, режимы, коды неисправностей"
      and c_pair.controller_short == c_mono.controller_short == "PCC3300", "8к: описание панели — из KB, оба слоя")
c_pair.mapping["controller"] = {"name": "X"}
check(c_pair.controller_measures == "двигатель, генератор, режимы, коды неисправностей"
      and c_pair.controller_short == "X", "8л: рабочая копия без ключа — по ролям карты регистров")
# Картина — в начале входа заключения (и в сжатом входе), совета — вместо блока реестра
row = {"id": 1, "run_state": 3, "report_md": "# Отчёт", "characteristics_json": {}, "object_picture": p1}
inp = prep.build_claude_input(row)
check(inp.startswith(p1 + "\n\n[ВЕРДИКТ БЛОКА АНАЛИТИКИ — НЕ ПЕРЕСМАТРИВАТЬ]"), f"8м: заключение — картина первой: {inp[:80]!r}")
row.pop("object_picture")
check(prep.build_claude_input(row).startswith("[ВЕРДИКТ"), "8н: картины нет — вход как раньше")
from corpus import compact  # noqa: E402
_subs, _blocks = [], []
for k in range(1, 4):
    _t = T + timedelta(minutes=3 * k)
    _subs.append({"id": f"x.{k}", "t_start": _t.isoformat(), "t_end": (_t + timedelta(minutes=3)).isoformat(),
                  "duration_sec": 180.0, "load_zone": "NORMAL", "detections": [], "data_gaps": [],
                  "characteristics": {"LOAD_PCT": {"unit": "%", "median": 30.0, "min": 20.0, "max": 40.0}}})
    _blocks.append(f"#### Подсегмент 1.{k} — зона\n\n| | |\n|-|-|\n" + "x" * 3000 + "\n\n")
crow = {"id": 2, "run_state": 3, "characteristics_json": json.dumps({"subsegments": _subs}), "object_picture": p1,
        "report_md": "# Отчёт\n\n## Сегменты\n\n### Сегмент 1 — Работа\n\n**Подсегментов:** 3\n\n"
                     + "".join(_blocks)}
cfull = prep.build_claude_input(crow)
ccomp = compact.compact_input(cfull, crow, 3)
check(ccomp is not None and len(ccomp) < len(cfull) and ccomp.startswith(p1 + "\n\n[ВЕРДИКТ"),
      "8о: сжатый вход сохраняет картину объекта первой")
INC = {"stop_ts": T.isoformat(), "character": {"character": "immediate"}, "window": {}}
ctx_eq = {"equipment": {"name": "ДГУ №1 инв. 2569", "engine": "Cummins KTA50", "engine_sn": "25428546",
                        "controller": "Cummins PowerCommand PCC3300"}}
ip_old = ig.build_incident_prompt(INC, None, [], ctx_eq)
ip_new = ig.build_incident_prompt(INC, None, [], ctx_eq, object_picture=p1)
check("Оборудование (реестр):" in ip_old and "  - Двигатель: Cummins KTA50" in ip_old and "25428546" not in ip_old
      and "s/n" not in ip_old, "8п: без картины — блок реестра тем же составом, без s/n (ревью п. 2)")
check(p1 in ip_new and "Оборудование (реестр):" not in ip_new and "25428546" not in ip_new
      and ip_new.index(p1) < ip_new.index("Останов:"), "8р: совет — картина вместо блока реестра, без s/n")

# ══ 9. Наработка кодом (В4) ══════════════════════════════════════════════════
H_ROW = {"id": 3, "router_sn": "SN", "equip_type": "pcc", "panel_id": 1, "report_md": "отчёт", "run_state": 3,
         "characteristics_json": {"engine_hours_end": 13643.4}, "analytics_version": "5.2.0"}
LINE = "*Наработка двигателя на конец периода: 13 643 ч (счётчик панели).*"   # пробел — неразрывный
check(worker._hours_line(H_ROW) == LINE and worker._hours_line(ROW0) is None, "9: строка есть / счётчика нет — нет")
check(worker.with_hours(H_ROW, LINE + "\n\n## Сводка\nтекст") == LINE + "\n\n## Сводка\nтекст",
      "9а0: строка уже стоит — второй раз не ставится (п. 22)")
res = run(worker._analyse_segment_chain(dict(H_ROW), None, sys_seg, [gw_entry], depth="seg_norma"))
check(res["conclusion_md"].startswith(LINE + "\n\n## Сводка\nОтвет шлюза"), f"9а: шлюз — строка перед текстом: "
      f"{res['conclusion_md'][:120]!r}")
check(LINE not in hum._extract_block2(res["conclusion_md"]), "9б: пересказ строку не получает")
res = run(worker._analyse_segment_chain(dict(ROW0), None, sys_seg, [gw_entry], depth="seg_norma"))
check(res["conclusion_md"].startswith("## Сводка") and "Наработка" not in res["conclusion_md"], "9в: нет счётчика — нет строки")
res = run(worker._analyse_segment_chain(dict(H_ROW), None, sys_seg, [api_entry], depth="seg_norma"))
cm = res["conclusion_md"]
check("### 🤖 Детерминированная аналитика" in cm and cm.index("### 🤖") < cm.index(LINE) < cm.index("## Сводка\nОтвет Claude"),
      f"9г: Claude — строка после Блока 1, перед текстом модели: {cm[:400]!r}")
check("Наработка двигателя на конец периода: 13 643 ч (счётчик панели)." in prep.build_claude_input(H_ROW),
      "9д: факт во входе остался")

# ══ 10. Хеши составов гейта и GATE_RULES_VERSION не изменились ══════════════
AN = [{"scenario": "LIMIT_PROXIMITY", "severity": "CAUTION", "ref_codes": [144], "fault_codes": [], "param_role": "COOLANT_TEMP"},
      {"scenario": "OIL_PRESSURE_LOW", "severity": "CAUTION", "fault_codes": [], "limit": {"kind": "passport", "hard": True}},
      {"scenario": "RPM_UNDERSPEED", "severity": "CAUTION", "fault_codes": []}]
PN = [{"scenario": "CONTROLLER_FAULT", "severity": "WARNING", "fault_codes": [1448]}]
# Эталон — посчитан кодом v5.2.0 (HEAD 79135a4) на тех же составах
check(sa.compute_analytics_hash(AN) == "75b09537f578"
      and sa.compute_fault_hash({"panel_alarms": PN, "analytics_alarms": AN}) == "f1e845d1d4d0"
      and sa.compute_fault_hash({"panel_alarms": [], "analytics_alarms": AN[:1]}) == "e01b8b148c13",
      "10: хеши составов гейта — как в v5.2.0")
check(sa.GATE_RULES_VERSION == "5.1.15", f"10а: GATE_RULES_VERSION не поднят: {sa.GATE_RULES_VERSION}")
s_copy = json.dumps(STRUCT, sort_keys=True, default=str)
sa.build_warning_prompt(STRUCT, object_picture=p1)
check(json.dumps(STRUCT, sort_keys=True, default=str) == s_copy
      and sa.compute_fault_hash(STRUCT) == sa.compute_fault_hash(json.loads(s_copy)),
      "10б: картина во входе не трогает состав и хеш")

# ══ 11. Эталон формата входа: вход изменился — подними *_INPUT_VERSION ═══════
# При правке сборщика входа: поднять версию и записать новый отпечаток сюда
INPUT_REF = {
    "conclusion": ("5.2.1", "CONCLUSION_INPUT_VERSION"),
    "gate": ("5.2.1", "GATE_INPUT_VERSION"),
    "incident": ("5.2.1", "INCIDENT_INPUT_VERSION"),
}
REF_SHA = {"conclusion": "e051e3f40d", "gate": "bb5adfd47b", "incident": "210cc45298"}
FIX_PIC = "Картина объекта (карта установок):\nОбъект: Тест. На объекте одна установка — соседей нет."
fx_conc = prep.build_claude_input({"id": 1, "run_state": 3, "report_md": "# Отчёт", "characteristics_json": {},
                                   "object_picture": FIX_PIC})
fx_struct = {"mode_label": "Работа", "run_state": 3, "load_pct": 41.0, "time_in_mode_sec": 3725,
             "severity_level": "предупреждение", "panel_severity": "норма", "analytics_severity": "предупреждение",
             "panel_alarms": [], "params": [],
             "analytics_alarms": [{"scenario": "LIMIT_PROXIMITY", "severity": "CAUTION", "fault_codes": [],
                                   "description": "Близость к уставке", "trigger": LP["trigger"]}]}
fx_gate = sa.build_warning_prompt(fx_struct, object_picture=FIX_PIC)
fx_inc = ig.build_incident_prompt({"stop_ts": T.isoformat(), "character": {"character": "immediate"}, "window": {}},
                                  None, [], {}, tz=timezone.utc, object_picture=FIX_PIC)
cur = {"conclusion": prep.CONCLUSION_INPUT_VERSION, "gate": sa.GATE_INPUT_VERSION,
       "incident": ig.INCIDENT_INPUT_VERSION}
for name, txt in (("conclusion", fx_conc), ("gate", fx_gate), ("incident", fx_inc)):
    sha = hashlib.sha256(txt.encode("utf-8")).hexdigest()[:10]
    ver, const = INPUT_REF[name]
    if sha != REF_SHA[name] and cur[name] == ver:
        _errors.append(f"11: вход «{name}» изменился ({sha} ≠ {REF_SHA[name]}), а {const} = {ver} — "
                       f"поднимите версию и запишите отпечаток в REF_SHA")
    elif sha == REF_SHA[name] and cur[name] != ver:
        _errors.append(f"11а: {const} = {cur[name]}, а эталон входа «{name}» прежний — обновите INPUT_REF")

# ══ 12. Перепроверка ревью v5.2.1 ════════════════════════════════════════════
# А: молчание — на момент загрузки карты; кэш старше SILENT_AFTER_SEC — без выводов
_ctxA = ctx_of(seen={("6003790403", "yts61", 1): NOW - timedelta(minutes=2)})
_ctxA["at"] = _time.monotonic()
check("датчик YTS-61 (качество масла)." in op.render(_ctxA, "6003790403", "pcc", 1, now=NOW + timedelta(hours=2),
                                                     live=True),
      "12А: датчик был на связи при загрузке — через 2 ч гейт не пишет «данных нет» по метке карты")
_ctxA2 = ctx_of()                                   # yts61 молчит 2 ч на момент загрузки
_ctxA2["at"] = _time.monotonic() - op.SILENT_AFTER_SEC - 60
_pA2 = op.render(_ctxA2, "6003790403", "pcc", 1, now=NOW, live=True)
check("датчик YTS-61 (качество масла)." in _pA2 and "данных нет" not in _pA2.split("\n")[3],
      f"12А2: кэш старше {op.SILENT_AFTER_SEC} с — выводов о свежести по меткам карты нет:\n{_pA2}")
_pA3 = op.render(_ctxA2, "6003790403", "pcc", 2, now=NOW, live=True,
                 freshness={("6003790403", "pcc", 1): NOW - timedelta(hours=2)})
check("ДГУ №1 инв. 2569 — Cummins KTA50, панель PCC3300 — данных нет" in _pA3,
      "12А3: живая свежесть движка — и при старом кэше")
# Б: телеметрия висит — карта грузится со сроком, без имён и свежести
_orig_b = (dban.get_equipment_registry, dbu.list_units, dbu.list_unit_sensors, odb.list_observations,
           op._object_names, op._telemetry_last_seen, op.TELEMETRY_TIMEOUT_SEC)


async def _hang():
    await asyncio.sleep(30)


dban.get_equipment_registry, dbu.list_units, dbu.list_unit_sensors, odb.list_observations = (
    _m_reg, _m_units, _m_sens, _m_obs)
op._object_names, op._telemetry_last_seen, op.TELEMETRY_TIMEOUT_SEC = _hang, _hang, 0.2
try:
    _t0 = _time.monotonic()
    _cB = run(op.load_context())
    _dt = _time.monotonic() - _t0
    check(_dt < 1.0 and _cB["names_ok"] is False and _cB["ttl"] == 60.0,
          f"12Б: зависшая телеметрия — карта за {_dt:.2f} с (одновременно, со сроком), имена не прочитаны")
finally:
    (dban.get_equipment_registry, dbu.list_units, dbu.list_unit_sensors, odb.list_observations,
     op._object_names, op._telemetry_last_seen, op.TELEMETRY_TIMEOUT_SEC) = _orig_b
# В: запасной блок и блок реестра совета — имя без серийников
_eB = types.SimpleNamespace(equip_name="ДГУ s/n 25428546", equip_engine=None, engine_sn="25428546",
                            cfg=types.SimpleNamespace(engine_label="Cummins KTA50",
                                                      controller_label="Cummins PowerCommand PCC3300"))
_fbB = op.engine_fallback(types.SimpleNamespace(_engines={"6003790403|pcc|1": _eB}), "6003790403", "pcc", 1)
check("Эта установка: ДГУ — двигатель Cummins KTA50" in _fbB and "25428546" not in _fbB
      and "6003790403" not in op.fallback_text("ДГУ 6003790403", serials=("6003790403",)), f"12В: {_fbB!r}")
_ipB = ig.build_incident_prompt(INC, None, [], {"equipment": {"name": "ДГУ №1 (6003790403)", "engine": "Cummins KTA50",
                                                              "engine_sn": "25428546", "router_sn": "6003790403",
                                                              "controller": "PCC3300"}})
check("  - Установка: ДГУ №1\n" in _ipB and "6003790403" not in _ipB, "12В2: блок реестра совета — имя очищено")
# Г: границы серийников и троек, «№» и «#» на краях
_S, _T = {"6003790403", "25428546"}, {"pcc/1", "pcc/2"}
for _src, _want in (("№3 ДЭС", "№3 ДЭС"), ("#2", "#2"), ("ДГУ №12 (pcc/12)", "ДГУ №12 (pcc/12)"),
                    ("Котельная 160037904031", "Котельная 160037904031"), ("ДГУ s/n 25428546", "ДГУ"),
                    ("ДГУ-2 (pcc/2)", "ДГУ-2"), ("Сининда-1 (6003790403)", "Сининда-1"),
                    ("ДГУ №1 инв. 2569", "ДГУ №1 инв. 2569")):
    check(op.clean_name(_src, _S, _T) == _want, f"12Г: clean_name({_src!r}) = {op.clean_name(_src, _S, _T)!r}")
# Д: СТОП — карта перечитывается (как ПУСК)
_ms = (ROOT / "online" / "manager.py").read_text(encoding="utf-8")
_sm = _ms[_ms.index("async def stop_machine"):_ms.index("async def stop_all")]
check("self.schedule_picture_refresh()" in _sm, "12Д: СТОП перечитывает картину объекта")

# 1: прошлое умолчание из формы и из копии — «следовать коду» с пометкой; кнопка — по тому же правилу
reset_router()
_fs1 = Store({})
_old_gate = prompts.known_defaults("warning_claude")[1]
_f1 = page_form()
_f1["ai_warning_claude_prompt"] = _old_gate.replace("\n", "\r\n")
_r1 = run(prompts.save_prompts_form(_f1, get=_fs1.get, put=_fs1.put))
check(_r1["defaulted"] == ["warning_claude"] and not _r1["saved"] and "ai_task_warning_claude_prompt" not in _fs1.d
      and prompts.system_for("warning_claude")[1]["src"] == "code", f"12-1: вставлено прошлое умолчание: {_r1}")
_fs1.d[f"ai_task_warning_claude_prompt{prompts.BACKUP_SUFFIX}"] = _old_gate
_rb = run(prompts.restore_backup("warning_claude", {"prompts_version": prompts.PROMPTS_FORM_VERSION},
                                 get=_fs1.get, put=_fs1.put))
check(_rb.get("defaulted") is True and _fs1.d.get("ai_task_warning_claude_prompt") == ""
      and prompts.describe("warning_claude", backup=_old_gate)["backup"] is False,
      f"12-1б: копия = прошлое умолчание — следовать коду, кнопка скрыта: {_rb}")
# 2: поля отпечатка нет — сравнение с действующим: без правки ничего, с правкой — запись
_f2 = {k: v for k, v in page_form().items() if not k.endswith("_shown")}
_fs1.puts.clear()
_r2 = run(prompts.save_prompts_form(_f2, get=_fs1.get, put=_fs1.put))
check(not _r2["conflicts"] and not _r2["saved"] and not [k for k in _fs1.puts if k.startswith("ai_task_")],
      f"12-2: без отпечатка, без правок — ни конфликта, ни записи: {_r2}")
_f2["ai_seg_auto_prompt"] = "Свой текст"
_r2b = run(prompts.save_prompts_form(_f2, get=_fs1.get, put=_fs1.put))
check(_r2b["saved"] == {"seg_auto": "Свой текст"} and not _r2b["conflicts"], f"12-2б: без отпечатка, с правкой: {_r2b}")
# 3: сбой посередине — след миграции и дата владельца не теряются
reset_router()
prompts.set_migration_info({})
_m3 = Store({"ai_task_seg_auto_prompt": SAVED_SEG_AUTO, "ai_task_warning_claude_prompt": SAVED_GATE},
            fail_on="ai_task_warning_claude_prompt")
for _t in ("seg_auto", "warning_claude"):
    router.apply_task(_t, "api", _m3.d[f"ai_task_{_t}_prompt"])
prompts.note_saved("seg_auto", datetime(2026, 10, 3, 15, 21, tzinfo=timezone.utc))
prompts.note_saved("warning_claude", datetime(2026, 10, 12, 20, 23, tzinfo=timezone.utc))


def _ap3(t, v):
    router.apply_task(t, "api", v)
    prompts.note_saved(t, datetime.now(timezone.utc))
try:
    run(prompts.run_prompts_migration(_m3.get, _m3.put, ["seg_auto", "warning_claude"], apply=_ap3,
                                      owner_saved_at=prompts.saved_at))
except RuntimeError:
    pass
_tr = json.loads(_m3.d[prompts.MIGRATION_INFO_KEY])
check(set(_tr["tasks"]) == {"seg_auto", "warning_claude"} and prompts.MIGRATION_KEY not in _m3.d
      and _m3.d["ai_task_warning_claude_prompt"] == SAVED_GATE,
      f"12-3: след задачи пишется ДО текста — сбой на тексте гейта: след есть, текст цел: {list(_tr['tasks'])}")
# Повтор (новый старт): след подгружен, дата владельца — первая записанная
_m3.fail_on = None
prompts.set_migration_info(_m3.d[prompts.MIGRATION_INFO_KEY])
prompts.note_saved("seg_auto", datetime(2026, 11, 2, 6, 0, tzinfo=timezone.utc))      # дата строки — миграции
run(prompts.run_prompts_migration(_m3.get, _m3.put, ["seg_auto", "warning_claude"], apply=_ap3,
                                  owner_saved_at=prompts.saved_at))
for _t, _d in (("seg_auto", "03.10.2026 18:21 МСК"), ("warning_claude", "12.10.2026 23:23 МСК")):
    _st3 = prompts.status_text(prompts.system_for(_t)[1])
    check("после миграции 5.2.1" in _st3 and f"сохранён владельцем {_d}" in _st3, f"12-3б: {_t}: {_st3}")
prompts.set_migration_info({})
reset_router()
# 4: system и версия совета — вместе на каждой попытке
I4: dict = {"events": [], "systems": []}


async def _i4_state(*a, **k):
    return {"analysed": False, "terminal": None, "ordered": sum(1 for e in I4["events"] if e.get("decision") == "ordered")}


async def _i4_append(sn, eq, panel, event, segment_id=None, ts=None):
    I4["events"].append(event)
    return True


async def _i4_ask(prompt, meta, system=None):
    I4["systems"].append(system)
    if len(I4["systems"]) == 1:
        router.apply_task("incident", "llm", "Новый промпт совета после первой попытки")   # правка между попытками
        raise RuntimeError("шлюз упал")
    return "## Коротко\nтекст"
_orig_i4 = (odb.get_incident_analysis_state, odb.append_segment_gate_event, odb.save_segment_warning, ig._material,
            ig._ask, ig.INCIDENT_RETRY_AFTER_SEC)
odb.get_incident_analysis_state, odb.append_segment_gate_event, odb.save_segment_warning = (
    _i4_state, _i4_append, _i_save)
ig._material, ig._ask, ig.INCIDENT_RETRY_AFTER_SEC = _i_material, _i4_ask, 0
try:
    I["saved"].clear()
    _v_before = prompts.system_for("incident")[1]["version"]
    run(ig._run_incident("SN", "pcc", 1, {"stop_ts": T.isoformat()}, T, 5))
    _ord = [e for e in I4["events"] if e.get("decision") == "ordered"]
    _v_after = prompts.system_for("incident")[1]["version"]
    check(len(_ord) == 2 and _ord[0]["prompt"] == _v_before and _ord[1]["prompt"] == _v_after != _v_before
          and I4["systems"][1] == prompts.system_for("incident")[0]
          and (I["saved"][-1] or {}).get("prompt") == _v_after,
          f"12-4: повтор после правки — новый system и его версия: {[e.get('prompt') for e in _ord]}")
finally:
    (odb.get_incident_analysis_state, odb.append_segment_gate_event, odb.save_segment_warning, ig._material,
     ig._ask, ig.INCIDENT_RETRY_AFTER_SEC) = _orig_i4
    reset_router()

if _errors:
    print("FAIL:")
    for e in _errors:
        print("  -", e)
    sys.exit(1)
print("OK: v5.2.1 — механизм промптов (сборка system, миграция, форма, версия, картина объекта, наработка)")
