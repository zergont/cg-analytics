# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Механизм промптов (v5.2.1; docs/plans/design_v520_prompts.md §4, решения
владельца В1–В15 от 01.11.2026).

Системный промпт каждой точки вызова ИИ собирает одна функция system_for:
текст владельца из настроек (или умолчание кода) + блок «Правила системы» —
безусловные правила кода (В1) — + приписка точки вызова (маркер вердикта гейта
на шлюзе, последней). Условные правила, привязанные к фактам, живут во входе,
как раньше (шапка заключения, «cancel ДОПУСТИМ/НЕДОСТУПНА»).

- Правило, текст которого уже есть у владельца, в блок не дописывается
  (meta.skipped); противоречия текста владельца правилам только помечаются
  (журнал на старте, пометка в форме) — код текст владельца не правит.
- prompt_version — отпечаток действующего system (sha256 после нормализации
  CRLF), версия блока правил и версия формата входа: пишется в debug_json
  разбора, gate_log, warning_analyses.extra (В12, В13).
- Разовая миграция сохранённых промптов по маркеру ai_prompts_version (§4.5,
  В2/В5/В6/В7): резервная копия, «равно умолчанию» → «следовать коду» (""),
  построчные замены только при точном совпадении. Маркер — после успеха.
- Форма «Системные промпты» сохраняет только отличие от умолчания, защищена от
  вкладки, открытой до выката (В8).
- История действующих текстов — app_settings.ai_prompt_history (≤ 50, В14),
  выключатель блока — ai_prompt_rules_enabled (по умолчанию true, В15).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

# Версия блока «Правила системы» (текст и состав правил). Правка текста или
# состава правил — поднять
PROMPT_RULES_VERSION = "5.2.1"
# Версия формы «Системные промпты»: вкладка с другой (или без неё) не сохраняет
PROMPTS_FORM_VERSION = "5.2.1"
# Маркер разовой миграции сохранённых промптов (по образцу ai_routes_version)
PROMPTS_MIGRATION_VERSION = "5.2.1"
MIGRATION_KEY = "ai_prompts_version"
RULES_ENABLED_KEY = "ai_prompt_rules_enabled"
HISTORY_KEY = "ai_prompt_history"
HISTORY_MAX = 50
# Резервная копия исходного текста владельца до миграции v5.2.1
BACKUP_SUFFIX = "_backup_5_2_1"
# След миграции: когда прошла и когда владелец сохранял тексты до неё (ревью
# v5.2.1, п. 17: дата строки промпта после миграции — дата миграции)
MIGRATION_INFO_KEY = "ai_prompts_migration_5_2_1"

RULES_HEADER = "ПРАВИЛА СИСТЕМЫ (действуют всегда, в настройках не меняются):"

SEG_TASKS: frozenset[str] = frozenset({"seg_auto", "seg_manual"})
GATE_TASKS: frozenset[str] = frozenset({"warning_claude"})


@dataclass(frozen=True)
class Rule:
    """Безусловное правило блока. text — может содержать {tz} (пояс подписей)."""
    id: str
    text: str
    tasks: frozenset[str]
    source: str
    # Фразы текста владельца, противоречащие правилу (сравнение — _fold)
    contradicts: tuple[str, ...] = ()


# Проект текстов — §4.2; R-NAME/R-EQUIP — по картине объекта во входе (В11 в
# новой постановке 01.11), R-HOURS — с «не повторяй» (В4 (а)), R-STOP — только
# заключения и гейт (В9 (а)), R-LIMIT — без уставок панели во входе (В10 (а))
RULES: tuple[Rule, ...] = (
    Rule("R-STOP",
         "Замечания аналитики — уровень «предупреждение», не выше. По ним не предписывай "
         "останов двигателя («выполнить останов», «немедленно остановить», «остановить ДГУ»): "
         "останов решает защита панели. Пиши, что проверить и за чем следить. При сигналах "
         "панели WARNING/SHUTDOWN — действия по коду панели из входа.",
         SEG_TASKS | GATE_TASKS, "audit1410.9.1; Д1 (≤ CAUTION, v5.1.7); В9 (а)"),
    Rule("R-LIMIT",
         "Пороги называй только те, что есть во входе, и тем видом, каким они там названы. "
         "Паспорт двигателя — минимум нормальной работы, а не уставка защиты. Уставок панели, "
         "которых нет во входе, не называй.",
         SEG_TASKS | GATE_TASKS, "audit1410.9.1; audit1410 §5; В10 (а)",
         contradicts=("10% строже",)),
    Rule("R-NAME",
         "Машину называй так, как в блоке «Картина объекта» входа (строка «Эта установка»). "
         "Серийный номер роутера — не имя машины.",
         SEG_TASKS | GATE_TASKS, "audit1410.9.4; В11 (а)"),
    Rule("R-EQUIP",
         "Марку двигателя, панели и датчиков и соседние установки бери только из блока "
         "«Картина объекта» входа; чего там нет — не называй.",
         SEG_TASKS | GATE_TASKS, "AI-019, Д26, plan5.Ч3; В11 (а)",
         contradicts=("cummins kta50", "cummins qsx15", "cummins с панелью",
                      "с панелью управления pcc3300")),
    Rule("R-HOURS",
         "Наработку двигателя бери только из строки «Наработка» входа; часы у событий журнала "
         "и счётчик пусков — не наработка. В тексте её не повторяй — строку с наработкой "
         "ставит система.",
         SEG_TASKS, "AI-030, audit1410.9.5, v5122.Наработка; В4 (а)",
         contradicts=("укажи при какой наработке",)),
    Rule("R-TZ",
         "Время пиши по местному времени ({tz}), как во входе, а не по UTC.",
         SEG_TASKS | GATE_TASKS, "Д19, v5122.P1-6"),
)

# Противоречия текста владельца не правилам блока, а входу кода (шапка
# заключения) и решениям кода — тоже только помечаются
INPUT_CONFLICTS: dict[str, tuple[tuple[str, str], ...]] = {
    "seg_auto": (("шапка входа «не пересматривай вердикт»", "поставь собственный диагноз"),
                 ("шапка входа «не пересматривай вердикт»", "и поставить собственный диагноз")),
    "seg_manual": (("шапка входа «не пересматривай вердикт»", "поставь собственный диагноз"),
                   ("шапка входа «не пересматривай вердикт»", "и поставить собственный диагноз")),
    "warning_claude": (("гейт при аварии панели не зовётся (В6)", "особый случай — аварийный останов"),),
}

# Известные и принятые противоречия — в журнал INFO, не WARNING (решение по
# ревью v5.2.1: seg_manual не трогаем до удаления кнопки «Анализ», В3)
ACCEPTED_CONFLICTS: frozenset[tuple[str, str]] = frozenset({
    ("seg_manual", "поставь собственный диагноз"),
    ("seg_manual", "и поставить собственный диагноз"),
})

# Источник версии формата входа по задаче (лениво: модули входа тяжёлые)
_INPUT_VERSION_SRC: dict[str, tuple[str, str]] = {
    "seg_auto": ("corpus.preprocessor", "CONCLUSION_INPUT_VERSION"),
    "seg_manual": ("corpus.preprocessor", "CONCLUSION_INPUT_VERSION"),
    "warning_claude": ("online.status_assembler", "GATE_INPUT_VERSION"),
    "incident": ("online.incident_gate", "INCIDENT_INPUT_VERSION"),
}

# ── Состояние процесса ───────────────────────────────────────────────────────

_rules_enabled: bool = True
# Когда сохранён текст владельца (app_settings.updated_at); нет — None
_saved_at: dict[str, datetime | None] = {}
# След миграции 5.2.1 (MIGRATION_INFO_KEY): {at, tasks: {задача: {owner_saved_at, sha}}}
_migration_info: dict[str, Any] = {}


def set_migration_info(raw: Any) -> None:
    """След миграции из app_settings (JSON); битый — пусто."""
    global _migration_info
    try:
        v = json.loads(raw) if isinstance(raw, str) and raw else (raw or {})
    except (TypeError, ValueError):
        v = {}
    _migration_info = v if isinstance(v, dict) else {}


def rules_enabled() -> bool:
    return _rules_enabled


def set_rules_enabled(enabled: bool) -> None:
    """Выключатель блока правил (В15): выключение снимает блок без отката кода."""
    global _rules_enabled
    _rules_enabled = bool(enabled)


def note_saved(task: str, ts: datetime | None) -> None:
    _saved_at[task] = ts


def saved_at(task: str) -> datetime | None:
    return _saved_at.get(task)


# ── Нормализация ─────────────────────────────────────────────────────────────

def normalize(text: Any) -> str:
    """CRLF → LF, пробелы в концах строк, края. Все тексты из textarea — с CRLF."""
    if not text:
        return ""
    t = str(text).replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in t.split("\n")).strip()


def _fold(text: str) -> str:
    """Для поиска фраз: пробелы схлопнуты, регистр и «ё» не важны, «10 %» = «10%»."""
    t = re.sub(r"\s+", " ", (text or "").replace("\r", " ")).strip().lower().replace("ё", "е")
    return re.sub(r"\s+%", "%", t)


def fingerprint(text: str) -> str:
    """Отпечаток system — 10 знаков sha256 нормализованного текста."""
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()[:10]


def _tz_label() -> str:
    try:
        from analytics.labels import tz_label
        return tz_label()
    except Exception:
        return "МСК"


def rule_text(rule: Rule) -> str:
    return rule.text.replace("{tz}", _tz_label())


def rules_for(task: str) -> list[Rule]:
    return [r for r in RULES if task in r.tasks]


# ── Сборка system ────────────────────────────────────────────────────────────

def _router():
    from llm import router
    return router


def owner_text(task: str) -> str:
    """Текст владельца из настроек; "" — задача следует коду (AI-002). Сохранённый
    текст, равный умолчанию кода (нынешнему или прошлому) после нормализации, —
    тоже «следовать коду» (ревью v5.2.1, п. 20: откат на v5.2.0 и возврат, форма
    старой версии)."""
    own = normalize(_router().get_owner_prompt(task))
    return "" if own and own in known_defaults(task) else own


def shown_text(task: str) -> str:
    """Текст, который форма показывает в поле задачи (действующий без блока)."""
    return base_text(task)[0]


def default_text(task: str) -> str:
    return normalize(_router().get_default_prompt(task))


def base_text(task: str) -> tuple[str, str]:
    """(действующий текст без блока, источник owner|code)."""
    own = owner_text(task)
    return (own, "owner") if own else (default_text(task), "code")


def rules_block(task: str, base: str | None = None, *,
                force: bool = False) -> tuple[str, list[str], list[str]]:
    """(текст блока, применённые id, пропущенные id). Правило, текст которого уже
    есть в тексте владельца, не дописывается. Выключатель — пустой блок; force —
    блок как был бы при включённом (показ в форме серым)."""
    if not (_rules_enabled or force):
        return "", [], []
    if base is None:
        base, _ = base_text(task)
    folded = _fold(base)
    lines: list[str] = []
    applied: list[str] = []
    skipped: list[str] = []
    for r in rules_for(task):
        txt = rule_text(r)
        if _fold(txt) in folded:
            skipped.append(r.id)
            continue
        applied.append(r.id)
        lines.append(f"- {txt}")
    if not lines:
        return "", applied, skipped
    return RULES_HEADER + "\n" + "\n".join(lines), applied, skipped


def conflicts_in(task: str, text: str) -> list[dict[str, str]]:
    """Противоречия текста правилам блока задачи и входу кода (только пометка)."""
    folded = _fold(text)
    out: list[dict[str, str]] = []
    for r in rules_for(task):
        for p in r.contradicts:
            if _fold(p) in folded:
                out.append({"rule": r.id, "phrase": p})
    for what, p in INPUT_CONFLICTS.get(task, ()):
        if _fold(p) in folded:
            out.append({"rule": what, "phrase": p})
    return out


def input_version(task: str) -> str | None:
    src = _INPUT_VERSION_SRC.get(task)
    if not src:
        return None
    try:
        import importlib
        return getattr(importlib.import_module(src[0]), src[1], None)
    except Exception:
        return None


def version_string(meta: dict[str, Any]) -> str:
    """«warning_claude·owner·3fa2c1d9e0·r5.2.1·in5.2.1» — короткая строка версии."""
    return (f"{meta.get('task')}·{meta.get('src')}·{meta.get('sha')}·r{meta.get('rules')}"
            f"·in{meta.get('input') or '-'}")


def system_for(task: str, *, suffix: str = "") -> tuple[str, dict[str, Any]]:
    """Системный промпт точки вызова и его след.

    text = текст владельца (или умолчание кода) + «\\n\\n» + блок «Правила
    системы» + suffix (у гейта на шлюзе — маркер вердикта, последним).
    meta = {task, src, saved_at, sha, rules, rule_ids, skipped, conflicts, input,
    version} — в debug_json / gate_log / warning_analyses.extra (В12, В13)."""
    base, src = base_text(task)
    block, applied, skipped = rules_block(task, base)
    text = base + (f"\n\n{block}" if block else "") + (suffix or "")
    ts = _saved_at.get(task) if src == "owner" else None
    meta: dict[str, Any] = {
        "task": task,
        "src": src,
        "saved_at": ts.isoformat() if isinstance(ts, datetime) else None,
        "sha": fingerprint(text),
        "rules": PROMPT_RULES_VERSION if _rules_enabled else "off",
        "rule_ids": applied,
        "skipped": skipped,
        "conflicts": conflicts_in(task, base),
        "input": input_version(task),
    }
    meta["version"] = version_string(meta)
    return text, meta


def prompt_version(task: str, *, suffix: str = "") -> str | None:
    """Строка версии system задачи (gate_log, отметки совета); сбой — None."""
    try:
        return system_for(task, suffix=suffix)[1]["version"]
    except Exception:
        logger.warning("Промпт %s: версия не определена", task, exc_info=True)
        return None


# ── Показ: /settings и журнал старта ─────────────────────────────────────────

def _local(ts: datetime | None) -> str | None:
    """«03.10.2026 18:21 МСК» — местное время с поясом."""
    if not isinstance(ts, datetime):
        return None
    try:
        from analytics.labels import _local_tz
        z = _local_tz()
        t = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        return f"{(t.astimezone(z) if z else t).strftime('%d.%m.%Y %H:%M')} {_tz_label()}"
    except Exception:
        return ts.isoformat()


def status_text(meta: dict[str, Any]) -> str:
    """«текст владельца от 03.10.2026 18:21 МСК · отпечаток 3fa2c1d9e0 · правила
    системы 5.2.1 (6)» / «умолчание кода 5.2.1 · …»."""
    if meta.get("src") == "owner":
        ts = meta.get("saved_at")
        when = _local_iso(ts)
        head = "текст владельца" + (f" от {when}" if when else "")
        mig = ((_migration_info.get("tasks") or {}).get(meta.get("task")) or {})
        if mig.get("sha") and mig["sha"] == fingerprint(owner_text(meta.get("task") or "")):
            # Текст — результат миграции (владелец с тех пор не сохранял): дата строки
            # — дата миграции, а владелец сохранял раньше (ревью v5.2.1, п. 17)
            own = _local_iso(mig.get("owner_saved_at"))
            head = (f"текст владельца после миграции {PROMPTS_MIGRATION_VERSION} от "
                    f"{_local_iso(mig.get('migrated_at') or _migration_info.get('at')) or when or '?'}"
                    + (f"; сохранён владельцем {own}" if own else ""))
    else:
        head = "умолчание кода"
    n = len(meta.get("rule_ids") or [])
    if meta.get("rules") == "off":
        rules = "правила системы выключены"
    elif n or meta.get("skipped"):
        rules = f"правила системы {meta.get('rules')} ({n})"
        if meta.get("skipped"):
            rules += f", уже в тексте: {', '.join(meta['skipped'])}"
    else:
        rules = "правил системы для задачи нет"
    return f"{head} · отпечаток {meta.get('sha')} · {rules}"


def _local_iso(ts: Any) -> str | None:
    if not ts:
        return None
    try:
        return _local(ts if isinstance(ts, datetime) else datetime.fromisoformat(str(ts)))
    except ValueError:
        return str(ts)


def conflicts_text(conflicts: list[dict[str, str]]) -> str:
    return "; ".join(f"противоречит {c['rule']}: «{c['phrase']}»" for c in conflicts)


def conflicts_accepted(task: str, conflicts: list[dict[str, str]]) -> bool:
    """Все противоречия известны и приняты (журнал INFO, а не WARNING)."""
    return bool(conflicts) and all((task, c.get("phrase")) in ACCEPTED_CONFLICTS for c in conflicts)


def _log_conflicts(task: str, line: str, conflicts: list[dict[str, str]]) -> None:
    if not conflicts:
        logger.info("%s", line)
    elif conflicts_accepted(task, conflicts):
        logger.info("%s · %s (известно, принято до удаления кнопки «Анализ»)", line,
                    conflicts_text(conflicts))
    else:
        logger.warning("%s · %s", line, conflicts_text(conflicts))


def describe(task: str, *, backup: Any = None,
             suffixes: dict[str, str] | None = None) -> dict[str, Any]:
    """Карточка задачи для формы: источник, отпечаток, блок правил (только
    чтение; выключен — серым, ревью п. 19), противоречия, умолчание кода,
    отпечаток показанного текста (защита от перезаписи из другой вкладки, п. 15),
    кнопка «Вернуть копию» — если копия есть и отличается от текущего текста (п. 18).
    backup — текст резервной копии (None — копии нет; True — есть, текст не дан)."""
    text, meta = system_for(task)
    base, _ = base_text(task)
    block, ids, _ = rules_block(task, base, force=True)
    has_backup = backup is not None and backup is not False
    if isinstance(backup, str):
        has_backup = form_value(task, backup) != owner_text(task)
    out = {
        "task": task, "meta": meta, "status": status_text(meta),
        "conflicts_text": conflicts_text(meta["conflicts"]),
        "conflicts_accepted": conflicts_accepted(task, meta["conflicts"]),
        "block": block, "block_ids": ids, "rules_on": _rules_enabled,
        "default": default_text(task), "backup": has_backup,
        "shown": shown_text(task), "shown_sha": fingerprint(shown_text(task)),
        "variants": [],
    }
    for label, sfx in (suffixes or {}).items():
        out["variants"].append({"label": label, "sha": system_for(task, suffix=sfx)[1]["sha"]})
    return out


def log_startup(tasks, suffixes: dict[str, dict[str, str]] | None = None) -> None:
    """Строка журнала на старте по каждой задаче; противоречия — WARNING."""
    for task in tasks:
        try:
            _, meta = system_for(task)
            extra = ""
            for label, sfx in ((suffixes or {}).get(task) or {}).items():
                extra += f" · {label}: {system_for(task, suffix=sfx)[1]['sha']}"
            line = f"Промпт {task}: {status_text(meta)}{extra} · {meta['version']}"
            _log_conflicts(task, line, meta["conflicts"])
        except Exception:
            logger.warning("Промпт %s: строка журнала не собрана", task, exc_info=True)


# ── История действующих текстов (В14) ────────────────────────────────────────

def history_add(raw_json: str | None, items: list[tuple[str, str, str]],
                now: datetime | None = None) -> str | None:
    """{sha: {task, text, first_seen, last_seen}} + items [(sha, task, text)];
    не больше HISTORY_MAX (свежие по last_seen). None — ничего не изменилось."""
    now = now or datetime.now(timezone.utc)
    try:
        hist = json.loads(raw_json) if raw_json else {}
    except (TypeError, ValueError):
        logger.warning("История промптов: битый JSON — начинаю заново")
        hist = {}
    if not isinstance(hist, dict):
        hist = {}
    changed = False
    stamp = now.isoformat()
    for sha, task, text in items:
        rec = hist.get(sha)
        if isinstance(rec, dict):
            if rec.get("last_seen") != stamp:
                rec["last_seen"] = stamp
                changed = True
            continue
        hist[sha] = {"task": task, "text": text, "first_seen": stamp, "last_seen": stamp}
        changed = True
    if len(hist) > HISTORY_MAX:
        keep = sorted(hist.items(), key=lambda kv: str((kv[1] or {}).get("last_seen") or ""),
                      reverse=True)[:HISTORY_MAX]
        hist = dict(keep)
        changed = True
    return json.dumps(hist, ensure_ascii=False) if changed else None


def history_items(tasks, suffixes: dict[str, dict[str, str]] | None = None) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    for task in tasks:
        variants = {"": ""}
        variants.update((suffixes or {}).get(task) or {})
        for sfx in variants.values():
            text, meta = system_for(task, suffix=sfx)
            out.append((meta["sha"], task, text))
    return out


async def record_history(get: Callable[..., Awaitable[Any]], put: Callable[..., Awaitable[Any]],
                         tasks, suffixes: dict[str, dict[str, str]] | None = None) -> bool:
    """Записать действующие тексты в историю; True — запись была."""
    new = history_add(await get(HISTORY_KEY, ""), history_items(tasks, suffixes))
    if new is None:
        return False
    await put(HISTORY_KEY, new)
    return True


# ── Разовая миграция сохранённых промптов (§4.5) ─────────────────────────────

# Таблица замен, согласованная владельцем 01.11 (В2 (а)+(б)+(в), В3 (а), В5 (а),
# В6 (а)): (было — строки подряд, станет — строки; пусто — удалить, решение).
# Меняются только целые строки при точном совпадении после нормализации
_SEG_LINE1_OLD = ("Ты — технический аналитик дизельных генераторных установок Cummins с панелью "
                  "управления PCC3300.",)
_SEG_LINE1_NEW = ("Ты — технический аналитик дизель-генераторных установок.",)
_SEG_HOURS_OLD = ("[1–2 строки: что происходило по сути. Обязательно укажи при какой наработке "
                  "мотора формируется ответ.]",)
_SEG_HOURS_NEW = ("[1–2 строки: что происходило по сути.]",)
_SEG_CAUSES_OLD = ("Если в отчёте есть раздел «Справочник кодов неисправностей» — используй эти "
                   "данные как достоверную базу для объяснения. Не выдумывай описания кодов сам. "
                   "ОБЯЗАТЕЛЬНО отмечай наиболее вероятные причины неисправности из этих данных.",)
_SEG_CAUSES_NEW = ("Если в отчёте есть раздел «Справочник кодов неисправностей» — используй эти "
                   "данные как достоверную базу для объяснения. Не выдумывай описания кодов сам. "
                   "Для кодов, выставленных панелью, называй вероятные причины по справочнику; "
                   "о причине аварийного останова не суди — её разбирает совет.",)
GATE_STRICT_OLD = ("Уставки аналитического блока намеренно ~10% строже порогов панели управления "
                   "(политика раннего предупреждения). Аналитика регистрирует отклонения без "
                   "понимания контекста, поэтому её предупреждения требуют проверки на адекватность.")
GATE_STRICT_NEW = ("Пороги аналитики — паспорт двигателя, близость к уставкам предупреждения и "
                   "останова панели и правила режимов. Аналитика регистрирует отклонения без "
                   "понимания контекста, поэтому её предупреждения требуют проверки на адекватность.")
_GATE_SPECIAL_OLD = (
    "ОСОБЫЙ СЛУЧАЙ — АВАРИЙНЫЙ ОСТАНОВ:",
    "Если в данных присутствует блок «Контекст аварийного останова», машина уже остановлена "
    "панелью управления — оценка реальности угрозы не нужна (вердикт всегда pass). Твоя задача "
    "смещается на реконструкцию происшествия. Начни ответ с раздела «Условия перед аварией»: в "
    "каком режиме и с какой нагрузкой работала машина до отключения, как вели себя ключевые "
    "параметры за последние минуты (плавный дрейф или скачок), какие тревоги висели до аварии и "
    "как они связаны с сработавшим кодом. Затем — вероятная причина и что проверить перед "
    "повторным пуском.",
)

_SEG_REPLACEMENTS = (
    (_SEG_LINE1_OLD, _SEG_LINE1_NEW, "В2 (а): марка — из картины объекта (R-EQUIP)"),
    (_SEG_HOURS_OLD, _SEG_HOURS_NEW, "В2 (б), В4: наработку ставит код (R-HOURS)"),
    (_SEG_CAUSES_OLD, _SEG_CAUSES_NEW, "В2 (в): причины — по справочнику, об аварии не судить"),
)
REPLACEMENTS: dict[str, tuple[tuple[tuple[str, ...], tuple[str, ...], str], ...]] = {
    "seg_auto": _SEG_REPLACEMENTS,
    "seg_manual": _SEG_REPLACEMENTS,             # В3 (а): иначе seg_manual не трогаем
    "warning_claude": (
        ((GATE_STRICT_OLD,), (GATE_STRICT_NEW,), "В5 (а): «~10% строже» — неверная посылка"),
        (_GATE_SPECIAL_OLD, (), "В6 (а): абзац «ОСОБЫЙ СЛУЧАЙ» мёртв — гейт при аварии не зовётся"),
    ),
}


def known_defaults(task: str) -> list[str]:
    """Умолчания кода задачи — нынешнее и прошлые (до v5.2.1), нормализованные:
    сохранённая копия, совпавшая с любым из них, — «следовать коду» (AI-002, В7)."""
    r = _router()
    cur = normalize(r.get_default_prompt(task))
    out = [cur]
    if task in SEG_TASKS:
        out.append(cur.replace(_SEG_LINE1_NEW[0], "Ты — технический аналитик дизельного "
                               "генераторного агрегата (ДГА) Cummins KTA50 с панелью управления PCC3300."))
    if task in GATE_TASKS:
        v5134 = cur.replace(GATE_STRICT_NEW, GATE_STRICT_OLD)
        out.append(v5134)
        # до v5.1.12 — старые фразы о панели
        out.append(v5134.replace(r.GATE_PANEL_RULE_NEW, r.GATE_PANEL_RULE_OLD)
                   .replace(r.GATE_PANEL_PASS_NEW, r.GATE_PANEL_PASS_OLD))
    return [normalize(x) for x in dict.fromkeys(out)]


def _replace_lines(lines: list[str], old: tuple[str, ...], new: tuple[str, ...]) -> bool:
    n = len(old)
    for i in range(len(lines) - n + 1):
        if tuple(lines[i:i + n]) == old:
            lines[i:i + n] = list(new)
            return True
    return False


def migrate_prompt_text(task: str, raw: str | None) -> tuple[str | None, list[str]]:
    """Новый текст сохранённого промпта задачи или None — менять нечего.

    Совпал с умолчанием кода (нынешним или прошлым) после нормализации — "" (В7);
    иначе — построчные замены по таблице (только целые строки при точном
    совпадении; строки нет — текст владельца не трогается, пометка в журнал).
    Только нормализация (CRLF) без замен — не пишем: дата владельца остаётся."""
    notes: list[str] = []
    norm = normalize(raw)
    if not norm:
        return None, ["пусто — уже следует коду"]
    if norm in known_defaults(task):
        return "", ["равен умолчанию кода — «следовать коду»"]
    lines = norm.split("\n")
    done = 0
    for old, new, why in REPLACEMENTS.get(task, ()):
        if _replace_lines(lines, old, new):
            done += 1
            notes.append(f"заменено ({why}): «{old[0][:60]}…»" if new else
                         f"удалено ({why}): «{old[0][:60]}…»")
        else:
            notes.append(f"строки нет — не тронуто ({why}): «{old[0][:60]}…»")
    if not done:
        return None, notes
    # После удаления абзаца не оставляем двойных пустых строк
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    if out in known_defaults(task):
        # После замен текст совпал с умолчанием кода — следовать коду (ревью п. 16)
        return "", notes + ["после замен равен умолчанию кода — «следовать коду»"]
    return out, notes


async def run_prompts_migration(get: Callable[..., Awaitable[Any]],
                                put: Callable[..., Awaitable[Any]],
                                tasks, *, apply: Callable[[str, str], None] | None = None,
                                owner_saved_at: Callable[[str], Any] | None = None
                                ) -> dict[str, Any]:
    """Разовая миграция (§4.5): для каждой задачи с ключом — резервная копия
    (только если её ещё нет), затем новый текст по migrate_prompt_text. Маркер
    ставится последним, только когда все задачи прошли; исключение — наружу
    (вызывающий пишет WARNING, повтор на следующем старте). Маркер уже стоит —
    ничего не делаем.

    get(key, default) / put(key, value) — app_settings; apply(task, text) —
    применить в роутере; owner_saved_at(task) — когда владелец сохранял текст
    (до миграции) — в след MIGRATION_INFO_KEY для /settings.
    Итог: {done, skipped, changes, notes}."""
    rep: dict[str, Any] = {"done": False, "skipped": False, "changes": {}, "notes": {}}
    if (await get(MIGRATION_KEY, "")) == PROMPTS_MIGRATION_VERSION:
        rep.update(done=True, skipped=True)
        return rep
    now_iso = datetime.now(timezone.utc).isoformat()
    # След — поверх загруженного (сбой посередине и повтор его не теряют, ревью п. 3)
    prev_info = _migration_info if isinstance(_migration_info, dict) else {}
    info: dict[str, Any] = {"at": prev_info.get("at") or now_iso,
                            "tasks": dict(prev_info.get("tasks") or {})}
    for task in tasks:
        key = f"ai_task_{task}_prompt"
        raw = await get(key, None)
        if raw is None:
            rep["notes"][task] = ["ключа нет — следует коду"]
            continue
        bkey = key + BACKUP_SUFFIX
        if (await get(bkey, None)) is None:
            await put(bkey, raw)
        new, notes = migrate_prompt_text(task, raw)
        rep["notes"][task] = notes
        if new is None:
            continue
        ts = owner_saved_at(task) if owner_saved_at else None
        old_rec = info["tasks"].get(task) or {}
        info["tasks"][task] = {
            # дата владельца — первая записанная: повтор после сбоя её не подменяет
            "owner_saved_at": old_rec.get("owner_saved_at")
            or (ts.isoformat() if isinstance(ts, datetime) else ts),
            "sha": fingerprint(new) if new else None,
            "migrated_at": now_iso,
        }
        # След задачи — ДО её нового текста: при сбое между ними дата владельца
        # остаётся в следе, а текст владельца — в БД (повтор доведёт)
        await put(MIGRATION_INFO_KEY, json.dumps(info, ensure_ascii=False))
        set_migration_info(info)
        await put(key, new)
        rep["changes"][task] = new
        if apply is not None:
            apply(task, new)
    await put(MIGRATION_KEY, PROMPTS_MIGRATION_VERSION)
    rep["done"] = True
    return rep


# ── Форма «Системные промпты» (§4.6, В8) ────────────────────────────────────

# Скрытые поля формы: отпечаток показанного текста задачи (ревью v5.2.1, п. 15) и
# признак «выключатель был в форме» (п. 19)
SHOWN_FIELD = "ai_{task}_shown"
RULES_PRESENT_FIELD = "ai_prompt_rules_present"


def form_value(task: str, posted: Any) -> str:
    """Значение поля формы для записи: нормализовано (CRLF), совпало с умолчанием
    кода — нынешним или прошлым, тот же набор, что у owner_text, — "" (следовать
    коду, не морозим; ревью v5.2.1, п. 1)."""
    v = normalize(posted)
    if not v or v in known_defaults(task):
        return ""
    return v


def past_default(task: str, posted: Any) -> bool:
    """Текст совпал с ПРОШЛЫМ умолчанием кода (не нынешним): задача будет следовать
    коду — владельцу сообщаем (журнал INFO и плашка формы)."""
    v = normalize(posted)
    return bool(v) and v != default_text(task) and v in known_defaults(task)


def _diff_short(old: str, new: str, limit: int = 12) -> str:
    import difflib
    d = [x for x in difflib.unified_diff(old.split("\n"), new.split("\n"), lineterm="", n=0)
         if not x.startswith(("---", "+++", "@@"))]
    return "\n".join(x[:160] for x in d[:limit]) + (f"\n… ещё {len(d) - limit} строк" if len(d) > limit else "")


async def save_prompts_form(form, *, get: Callable[..., Awaitable[Any]],
                            put: Callable[..., Awaitable[Any]],
                            suffixes: dict[str, dict[str, str]] | None = None) -> dict[str, Any]:
    """Сохранение формы. Итог {stale, saved, conflicts, rules_enabled}.

    - Вкладка без поля prompts_version или со старой версией — ничего не пишет;
      что она пыталась поменять — в журнал (WARNING, ревью п. 14).
    - Поля нет — задачу не трогаем. Текст, совпавший с показанным во вкладке
      (скрытое поле ai_<задача>_shown — отпечаток), не пишется: «Сохранить» не
      откатывает правку, сделанную после открытия страницы (другая вкладка,
      «Вернуть копию»; п. 15). Изменённый во вкладке, а в БД с тех пор
      поменявшийся, — отказ по полю (conflicts), правка — в журнал.
    - Равное умолчанию — "" (не морозим). Выключатель блока — только если
      поле было в форме (п. 19)."""
    r = _router()
    ver = form.get("prompts_version")
    if ver != PROMPTS_FORM_VERSION:
        changed = []
        for task in r.TASKS:
            v = form.get(f"ai_{task}_prompt")
            if v is not None and normalize(v) != shown_text(task):
                changed.append(task)
                logger.warning("Системные промпты: старая вкладка — текст вкладки %s отличается от "
                               "действующего (не сохранён):\n%s",
                               task, _diff_short(shown_text(task), normalize(v)))
        logger.warning("Системные промпты: форма версии %r (нужна %s) — вкладка открыта до "
                       "обновления; ничего не сохранено (текст вкладки отличается у: %s), обновите "
                       "страницу", ver, PROMPTS_FORM_VERSION, ", ".join(changed) or "ни у одной задачи")
        return {"stale": True, "saved": {}, "conflicts": [], "defaulted": [],
                "rules_enabled": _rules_enabled}
    now = datetime.now(timezone.utc)
    saved: dict[str, str] = {}
    conflicts: list[str] = []
    defaulted: list[str] = []
    for task in r.TASKS:
        v = form.get(f"ai_{task}_prompt")
        if v is None:
            continue
        posted = normalize(v)
        shown = form.get(SHOWN_FIELD.format(task=task))
        if shown is None:
            # Отпечатка нет (карточка не собралась) — сравнение с действующим: равно —
            # не меняли, иначе — запись, без ложного «конфликта» (ревью п. 2)
            if posted == shown_text(task):
                continue
        elif fingerprint(posted) == shown:
            continue                      # в этой вкладке не меняли
        elif fingerprint(shown_text(task)) != shown:
            conflicts.append(task)
            logger.warning("Промпт %s: изменился после открытия страницы (другая вкладка или "
                           "«Вернуть копию») — правка не сохранена:\n%s",
                           task, _diff_short(shown_text(task), posted))
            continue
        new = form_value(task, posted)
        if past_default(task, posted):
            defaulted.append(task)
            logger.info("Промпт %s: текст совпал с прошлым умолчанием кода — задача следует коду", task)
        # Не изменилось по смыслу — не пишем; но сохранённая копия умолчания
        # (owner_text её не видит, п. 20) при «следовать коду» сбрасывается в ""
        if new == owner_text(task) and not (not new and normalize(r.get_owner_prompt(task))):
            continue
        await put(f"ai_task_{task}_prompt", new)
        r.apply_task(task, r.get_provider(task), new)
        note_saved(task, now if new else None)
        saved[task] = new
        conf = conflicts_in(task, new) if new else []
        if conf:
            _log_conflicts(task, f"Промпт {task} сохранён", conf)
    toggled = False
    if form.get(RULES_PRESENT_FIELD) is not None:
        en = form.get(RULES_ENABLED_KEY) is not None
        toggled = en != _rules_enabled
        if toggled:
            await put(RULES_ENABLED_KEY, "true" if en else "false")
            set_rules_enabled(en)
            logger.warning("Блок «Правила системы» %s",
                           "включён" if en else "ВЫКЛЮЧЕН (ключ ai_prompt_rules_enabled)")
    if saved or toggled:
        try:
            await record_history(get, put, r.TASKS, suffixes)
        except Exception:
            logger.warning("История промптов не записана", exc_info=True)
    return {"stale": False, "saved": saved, "conflicts": conflicts, "defaulted": defaulted,
            "rules_enabled": _rules_enabled}


async def restore_backup(task: str, form, *, get: Callable[..., Awaitable[Any]],
                         put: Callable[..., Awaitable[Any]],
                         suffixes: dict[str, dict[str, str]] | None = None) -> dict[str, Any]:
    """«Вернуть копию 5.2.1»: текст владельца до миграции из резервного ключа.
    Старая вкладка (без версии формы) — ничего не делает ({} — ложь). Копия,
    совпавшая с прошлым умолчанием кода, — «следовать коду» (defaulted, ревью п. 1).
    Действующий текст — в историю (п. 18). Итог {ok, defaulted}."""
    r = _router()
    if form.get("prompts_version") != PROMPTS_FORM_VERSION or task not in r.TASKS:
        logger.warning("Возврат копии промпта %s: форма версии %r — не выполнено",
                       task, form.get("prompts_version"))
        return {}
    raw = await get(f"ai_task_{task}_prompt{BACKUP_SUFFIX}", None)
    if raw is None:
        return {}
    new = form_value(task, raw)
    defaulted = past_default(task, raw)
    if defaulted:
        logger.info("Промпт %s: копия совпала с прошлым умолчанием кода — задача следует коду", task)
    await put(f"ai_task_{task}_prompt", new)
    r.apply_task(task, r.get_provider(task), new)
    note_saved(task, datetime.now(timezone.utc) if new else None)
    conf = conflicts_in(task, new) if new else []
    logger.warning("Промпт %s: возвращена копия до миграции 5.2.1%s", task,
                   (" · " + conflicts_text(conf)) if conf else "")
    try:
        await record_history(get, put, r.TASKS, suffixes)
    except Exception:
        logger.warning("История промптов не записана", exc_info=True)
    return {"ok": True, "defaulted": defaulted}
