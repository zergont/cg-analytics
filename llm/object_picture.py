# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Картина объекта во входе моделей (v5.2.1, решение владельца В11 в новой
постановке 01.11): заключение, гейт и совет получают в начале входа

    Картина объекта (карта установок):
    Объект: Сининда-1. Установки объекта (в одной сети): ДГУ №1 инв. 2569, ДГУ №2 инв. 2568.
    Эта установка: ДГУ №1 инв. 2569 — двигатель Cummins KTA50.
    Источники данных: панель Cummins PowerCommand PCC3300 (двигатель, генератор, режимы, коды неисправностей).
    Соседи: ДГУ №2 инв. 2568 — Cummins KTA50, панель PCC3300.

по карте установок (db/units.py, v5.2.0): соседи — активные установки того же
объекта, датчики — привязанные к установке («не привязаны» — под объектом). Что
меряет источник — кратко из KB (controller.measures слоя панели / датчика).
Источник привязан, но молчит больше 30 мин — «данных нет» (гейт, живая
свежесть) или «сейчас нет данных» (заключение и совет: период уже прошёл).
Свежесть панелей — память движков и online_observations, датчиков — телеметрия
(equipment.last_seen_at); не известна — не пишется. Сосед без наблюдения —
«наблюдение не ведётся», после СТОП — «наблюдение остановлено».
Без серийных номеров двигателей и роутера, без троек и адресов — как политика
MCP Р11 (clean_name — одна очистка для объекта, установки, соседа и датчика).
Модели правила R-NAME/R-EQUIP (llm/prompts.py): машину и марку — только по этому
блоку; карты нет — запасной блок «Картина объекта: карта установок не
загружена» с тем, что знает движок (fallback_text).

Кэш — у онлайн-менеджера (одна загрузка карты на старте, на ПУСК, после правки
карты или реестра): гейт берёт картину из кэша без запросов в горячем цикле,
заключение и совет — тоже из кэша, при пустом или старом кэше — одна загрузка
на задачу.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

PICTURE_HEADER = "Картина объекта (карта установок):"
# Источник без данных дольше этого — «данных нет» (обрыв связи минутами — норма)
SILENT_AFTER_SEC = 30 * 60
# Срок кэша карты у менеджера: старше — заключение и совет перечитывают (одна
# загрузка на задачу), гейт — заказывает фоновое обновление и берёт что есть
CONTEXT_TTL_SEC = 10 * 60
# Срок одного чтения телеметрии (имена объектов, свежесть датчиков) при загрузке
# карты: реплика висит — карта грузится без них (ревью v5.2.1, Б)
TELEMETRY_TIMEOUT_SEC = 8.0

Triple = tuple[str, str, int]

# Подписи слоёв KB по привязке: KB правят редко, движки перечитывают её на ПУСК
_LABELS_TTL_SEC = 3600.0
_labels_cache: dict[tuple, tuple[float, dict[str, Any]]] = {}


def _aware(ts: Any) -> datetime | None:
    if ts is None:
        return None
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts)
        except ValueError:
            return None
    if not isinstance(ts, datetime):
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _silent(ts: Any, now: datetime) -> bool:
    t = _aware(ts)
    return t is None or (now - t).total_seconds() > SILENT_AFTER_SEC


# ── Подписи из KB ────────────────────────────────────────────────────────────

def _cfg_labels(cfg) -> dict[str, Any]:
    return {"label": cfg.controller_label, "short": cfg.controller_short,
            "measures": cfg.controller_measures}


def panel_labels(panel: dict[str, Any], cfg=None) -> dict[str, Any]:
    """{label, short, measures, engine} панели установки: подписи контроллера —
    из конфига движка (если есть) или из KB по привязке реестра; двигатель —
    «производитель модель» из реестра (как у совета, v5.1.14). Сбой — без подписей."""
    out: dict[str, Any] = {
        "engine": " ".join(str(x) for x in (panel.get("manufacturer"), panel.get("model")) if x) or None,
    }
    try:
        if cfg is not None:
            out.update(_cfg_labels(cfg))
            return out
        key = (panel.get("controller_id"), panel.get("engine_id"), panel.get("kb_path"))
        if not ((key[0] and key[1]) or key[2]):
            return out
        hit = _labels_cache.get(key)
        if hit and time.monotonic() - hit[0] < _LABELS_TTL_SEC:
            out.update(hit[1])
            return out
        from analytics import binding
        from config import settings
        lab = _cfg_labels(binding.build_config(settings.knowledge_base_path, controller_id=key[0],
                                               engine_id=key[1], kb_path=key[2]))
        _labels_cache[key] = (time.monotonic(), lab)
        out.update(lab)
    except Exception:
        logger.warning("Картина объекта: подписи панели из KB не собраны", exc_info=True)
    return out


def sensor_measures(sensor: dict[str, Any]) -> str | None:
    """Что меряет датчик: controller.measures слоя KB его привязки
    (controllers/<controller_id>/analytics/mapping.yaml), иначе роль из карты
    установок (решение владельца о датчике); нет — None."""
    cid = sensor.get("controller_id")
    if cid:
        key = ("sensor", cid)
        hit = _labels_cache.get(key)
        if hit and time.monotonic() - hit[0] < _LABELS_TTL_SEC:
            m = hit[1].get("measures")
        else:
            m = None
            try:
                import yaml
                from config import kb_read, settings
                p = kb_read(settings.knowledge_base_path / "controllers" / str(cid) / "analytics"
                            / "mapping.yaml")
                if p.exists():
                    with p.open(encoding="utf-8") as f:
                        c = (yaml.safe_load(f) or {}).get("controller")
                    if isinstance(c, dict) and c.get("measures"):
                        m = str(c["measures"]).strip() or None
            except Exception:
                logger.warning("Картина объекта: описание датчика %s из KB не прочитано", cid,
                               exc_info=True)
            _labels_cache[key] = (time.monotonic(), {"measures": m})
        if m:
            return m
    role = (sensor.get("role") or "").strip()
    return role or None


# ── Загрузка карты ───────────────────────────────────────────────────────────

def _panel_types() -> frozenset[str]:
    try:
        from telemetry_mcp.config import load
        return load().panel_types
    except Exception:
        from db.units import DEFAULT_PANEL_TYPES
        return DEFAULT_PANEL_TYPES


# Имена объектов: кэш только удачного чтения (ревью v5.2.1, п. 9: холодный старт
# без БД телеметрии не замораживает «имя не прочитано» на 10 мин)
_NAMES_TTL_SEC = 600.0
_names_cache: tuple[float, dict[str, str]] | None = None


async def _object_names() -> dict[str, str] | None:
    """{router_sn: имя объекта} из таблицы objects телеметрии (тем же пулом, что
    telemetry_mcp.catalog и /api/units); сбой — None (имя «не прочитано»)."""
    global _names_cache
    now = time.monotonic()
    if _names_cache and now - _names_cache[0] < _NAMES_TTL_SEC:
        return _names_cache[1]
    try:
        from analytics import source as _src
        from db import remote
        async with remote.connection(_src._get_pool()) as conn:
            rows = await conn.fetch("SELECT router_sn, name FROM objects")
        names = {r["router_sn"]: (r["name"] or "").strip() for r in rows if (r["name"] or "").strip()}
        _names_cache = (now, names)
        return names
    except Exception:
        logger.warning("Картина объекта: имена объектов из телеметрии не прочитаны", exc_info=True)
        return None


async def _telemetry_last_seen() -> dict[Triple, Any] | None:
    """Свежесть оборудования по телеметрии — equipment.last_seen_at, один запрос
    к реплике на загрузку карты (ревью v5.2.1, п. 1): у датчиков нет движка
    аналитики и строки online_observations. Сбой — None (свежесть не известна)."""
    try:
        from db.source import get_active_equipment
        return {(r["router_sn"], r["equip_type"], int(r["panel_id"])): r.get("last_seen_at")
                for r in await get_active_equipment()}
    except Exception:
        logger.warning("Картина объекта: свежесть датчиков из телеметрии не прочитана", exc_info=True)
        return None


def _unit_key(u: dict[str, Any]) -> Triple:
    p = u.get("panel") or {}
    return (u.get("router_sn"), p.get("equip_type"), int(p.get("panel_id")))


def _sensor_key(s: dict[str, Any], router_sn: str) -> Triple:
    return (s.get("router_sn") or router_sn, s.get("equip_type"), int(s.get("panel_id")))


def annotate(tree: dict[str, Any], cfgs: dict[Triple, Any] | None = None,
             seen: dict[Triple, Any] | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Подписи KB и свежесть датчиков в дерево (на месте): у установки _pic, у
    датчика _measures и _fresh_known. Свежесть датчика: строка наблюдения
    движка, иначе телеметрия (seen); нет ни того, ни другого — не известна, и
    «данных нет» не пишется. Молчание по меткам карты — _silent — считается на
    момент загрузки (now), а не на момент вывода (ревью v5.2.1, А)."""
    now = now or datetime.now(timezone.utc)

    def _sensor(s: dict[str, Any], rsn: str) -> None:
        s["_measures"] = sensor_measures(s)
        if s.get("observation") is not None:
            s["_fresh_known"] = True
        elif seen is not None:
            last = seen.get(_sensor_key(s, rsn))
            s["last_data_ts"] = last.isoformat() if hasattr(last, "isoformat") else last
            s["_fresh_known"] = True
        else:
            s["_fresh_known"] = False
        s["_silent"] = _silent(s.get("last_data_ts"), now)
    for o in tree.get("objects") or []:
        for u in o.get("units") or []:
            u["_pic"] = panel_labels(u.get("panel") or {}, (cfgs or {}).get(_unit_key(u)))
            p = u.get("panel") or {}
            p["_silent"] = _silent(p.get("last_data_ts"), now)
            for s in u.get("sensors") or []:
                _sensor(s, o.get("router_sn"))
        for s in o.get("unattached") or []:
            _sensor(s, o.get("router_sn"))
    return tree


async def load_context(cfgs: dict[Triple, Any] | None = None) -> dict[str, Any]:
    """Карта установок с подписями KB — одна загрузка (реестр, установки, датчики,
    наблюдения; из телеметрии — имена объектов и свежесть датчиков). cfgs —
    конфиги работающих движков по тройке (подписи панели без чтения KB).
    Телеметрия не ответила — срок кэша короткий: повтор при следующем вызове."""
    from db.units import get_tree

    async def _bounded(coro, what: str):
        try:
            return await asyncio.wait_for(coro, TELEMETRY_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            logger.warning("Картина объекта: %s из телеметрии не прочитаны за %.0f с — без них",
                           what, TELEMETRY_TIMEOUT_SEC)
            return None
    # Имена и свежесть — одновременно и со сроком (ревью v5.2.1, Б)
    names, seen = await asyncio.gather(_bounded(_object_names(), "имена объектов"),
                                       _bounded(_telemetry_last_seen(), "свежесть датчиков"))
    tree = await get_tree(panel_types=_panel_types(), object_names=names or {})
    ok = names is not None and seen is not None
    return {"tree": annotate(tree, cfgs, seen, datetime.now(timezone.utc)), "at": time.monotonic(),
            "names_ok": names is not None, "ttl": CONTEXT_TTL_SEC if ok else 60.0}


# ── Текст ────────────────────────────────────────────────────────────────────

_SN_PREFIX = r"(?:\b(?:s/?n|sn|esn|с/н|сер(?:ийный)?\.?\s*(?:номер|№)?|зав(?:одской)?\.?\s*(?:номер|№)?)\s*[:#№]?\s*)?"


def object_serials(obj: dict[str, Any]) -> tuple[set[str], set[str]]:
    """(серийные номера, тройки) объекта: роутер и двигатели всех его панелей;
    тройки — «тип/панель» установок и датчиков."""
    serials = {str(obj.get("router_sn") or "")}
    triples: set[str] = set()
    for u in obj.get("units") or []:
        p = u.get("panel") or {}
        serials.add(str(p.get("engine_sn") or ""))
        triples.add(f"{p.get('equip_type')}/{p.get('panel_id')}")
        for s in u.get("sensors") or []:
            triples.add(f"{s.get('equip_type')}/{s.get('panel_id')}")
    for s in obj.get("unattached") or []:
        triples.add(f"{s.get('equip_type')}/{s.get('panel_id')}")
    return {x for x in serials if len(x.strip()) >= 5}, {t for t in triples if "None" not in t}


def clean_name(name: Any, serials: set[str], triples: set[str]) -> str:
    """Имя без серийных номеров роутера и двигателей объекта и без троек (одна
    функция для объекта, установки, соседа и датчика, ревью v5.2.1, п. 3):
    «ДГУ №1 s/n 25428546» → «ДГУ №1», «Сининда-1 (6003790403)» → «Сининда-1».
    Пусто — "" (вызывающий ставит запасную подпись)."""
    s = str(name or "")
    # Только целые: серийник внутри более длинного числа и «pcc/1» внутри
    # «pcc/12» не трогаем (ревью v5.2.1, Г)
    for sn in sorted(serials, key=len, reverse=True):
        s = re.sub(_SN_PREFIX + r"(?<![\w/])" + re.escape(sn.strip()) + r"(?![\w/])", " ", s,
                   flags=re.IGNORECASE)
    for t in sorted(triples, key=len, reverse=True):
        s = re.sub(r"(?<![\w/])" + re.escape(t) + r"(?![\w/])", " ", s)
    s = re.sub(r"\(\s*\)|\[\s*\]", " ", s)
    # «№» и «#» на краях — часть имени («№3 ДЭС»), не снимаем
    s = re.sub(r"\s+", " ", s).strip(" -–—,.:;")
    return s


def _uname(u: dict[str, Any], serials: set[str], triples: set[str]) -> str:
    n = clean_name(u.get("name"), serials, triples)
    return n or f"без имени (панель {(u.get('panel') or {}).get('panel_id')})"


def _fresh_note(ts: Any, now: datetime, live: bool, silent: bool | None = None) -> str | None:
    """Пометка о молчании источника. silent — готовый вывод по меткам карты (на
    момент её загрузки); None — считать по ts и now (живая память движков)."""
    if not (_silent(ts, now) if silent is None else silent):
        return None
    # Живая свежесть — гейту; в заключении и совете период прошёл: «сейчас»
    return "данных нет" if live else "сейчас нет данных"


def _map_silent(d: dict[str, Any], now: datetime, map_ok: bool) -> bool | None:
    """Молчание по метке карты: на момент загрузки карты; кэш старше
    SILENT_AFTER_SEC — выводов о свежести по ней не делаем (False). Ревью А."""
    if not map_ok:
        return False
    if "_silent" in d:
        return bool(d["_silent"])
    return _silent(d.get("last_data_ts"), now)


def _sensor_name(s: dict[str, Any], serials: set[str], triples: set[str]) -> str:
    """Имя датчика: из карты / реестра (очищено); запасное имя дерева — код типа
    («yts61») — не имя: модель из реестра, иначе «без имени»."""
    raw = s.get("name")
    if raw and raw == s.get("equip_type"):
        raw = None
    return (clean_name(raw, serials, triples) or clean_name(s.get("model"), serials, triples)
            or "без имени")


def _sensor_text(s: dict[str, Any], now: datetime, live: bool, serials: set[str],
                 triples: set[str], map_ok: bool = True) -> str:
    name = _sensor_name(s, serials, triples)
    m = s.get("_measures") if "_measures" in s else sensor_measures(s)
    t = f"датчик {name}" + (f" ({m})" if m else "")
    note = (_fresh_note(None, now, live, silent=_map_silent(s, now, map_ok))
            if s.get("_fresh_known") else None)
    return t + (f" — {note}" if note else "")


def _neighbor_note(u: dict[str, Any], fresh: dict[Triple, Any], now: datetime, live: bool,
                   map_ok: bool = True) -> str | None:
    p = u.get("panel") or {}
    obs = p.get("observation")
    key = _unit_key(u)
    if key in fresh:                       # работающий движок (память менеджера)
        return _fresh_note(fresh[key], now, live)
    if obs is None:
        return "наблюдение не ведётся"
    if obs != "running":
        return "наблюдение остановлено"
    return _fresh_note(None, now, live, silent=_map_silent(p, now, map_ok))


def render(ctx: dict[str, Any] | None, router_sn: str, equip_type: str, panel_id: int, *,
           now: datetime | None = None,
           freshness: dict[Triple, Any] | None = None,
           live: bool = False) -> str | None:
    """Блок «Картина объекта» для установки; None — установки нет в карте.

    freshness — свежесть панелей работающих движков по тройке (память
    менеджера, без запросов); live — живая свежесть (гейт): «данных нет»; в
    заключении и совете — «сейчас нет данных» (период уже прошёл)."""
    if not ctx:
        return None
    tree = ctx.get("tree") or {}
    obj = next((o for o in tree.get("objects") or [] if o.get("router_sn") == router_sn), None)
    if obj is None:
        return None
    me = next((u for u in obj.get("units") or []
               if _unit_key(u) == (router_sn, equip_type, int(panel_id))), None)
    if me is None:
        return None
    now = now or datetime.now(timezone.utc)
    fresh = freshness or {}
    serials, triples = object_serials(obj)
    # Метки карты действуют, пока кэш моложе SILENT_AFTER_SEC (ревью А)
    at = ctx.get("at")
    map_ok = at is None or time.monotonic() - float(at) <= SILENT_AFTER_SEC
    units = [u for u in obj.get("units") or [] if u is me or u.get("active")]
    others = [u for u in units if u is not me]

    if ctx.get("names_ok", True) is False:
        head = "Объект: имя объекта не прочитано."
    else:
        oname = clean_name(obj.get("name"), serials, triples)
        head = f"Объект: {oname}." if oname else "Объект: имя не задано."
    lines = [PICTURE_HEADER]
    if others:
        lines.append(f"{head} Установки объекта (в одной сети): "
                     f"{', '.join(_uname(u, serials, triples) for u in units)}.")
    else:
        lines.append(f"{head} На объекте одна установка — соседей нет.")

    pic = me.get("_pic") if "_pic" in me else panel_labels(me.get("panel") or {})
    eng = pic.get("engine")
    lines.append(f"Эта установка: {_uname(me, serials, triples)} — "
                 + (f"двигатель {eng}." if eng else "двигатель в реестре не задан."))
    panel = "панель " + (pic.get("label") or "(марка в KB не задана)")
    if pic.get("measures"):
        panel += f" ({pic['measures']})"
    lines.append("Источники данных: " + "; ".join(
        [panel] + [_sensor_text(s, now, live, serials, triples, map_ok)
                   for s in me.get("sensors") or []]) + ".")

    if others:
        nb: list[str] = []
        for u in others:
            p = u.get("_pic") if "_pic" in u else panel_labels(u.get("panel") or {})
            t = (f"{_uname(u, serials, triples)} — {p.get('engine') or 'двигатель в реестре не задан'}, "
                 f"панель {p.get('short') or p.get('label') or 'не задана'}")
            note = _neighbor_note(u, fresh, now, live, map_ok)
            if note:
                t += f" — {note}"
            sens = [_sensor_name(s, serials, triples) for s in u.get("sensors") or []]
            if sens:
                t += ", датчики: " + ", ".join(sens)
            nb.append(t)
        lines.append("Соседи: " + "; ".join(nb) + ".")
    un = obj.get("unattached") or []
    if un:
        lines.append("Датчики объекта без привязки к установке: "
                     + "; ".join(_sensor_text(s, now, live, serials, triples, map_ok) for s in un) + ".")
    return "\n".join(lines)


def is_full(text: str | None) -> bool:
    """Картина по карте установок (а не запасная строка) — признак picture."""
    return bool(text) and str(text).startswith(PICTURE_HEADER)


def fallback_text(name: str | None = None, engine: str | None = None,
                  controller: str | None = None, serials: Any = ()) -> str:
    """Карты нет (не загрузилась, сбой, нет менеджера) — минимальный блок из памяти
    движка, чтобы правила R-NAME/R-EQUIP не ссылались в пустоту (ревью v5.2.1, п. 4).
    Имя — через clean_name (серийники роутера и двигателя, ревью В)."""
    head = f"{PICTURE_FALLBACK_HEADER} карта установок не загружена."
    name = clean_name(name, {str(x) for x in serials if x and len(str(x).strip()) >= 5}, set()) or None
    if not (name or engine or controller):
        return head + " Имя и марку машины не называй."
    t = f"Эта установка: {name or 'имя не задано'} — " + (f"двигатель {engine}" if engine
                                                          else "двигатель не известен")
    return head + "\n" + t + (f", панель {controller}" if controller else "") + "."


PICTURE_FALLBACK_HEADER = "Картина объекта:"


async def picture_for(router_sn: str, equip_type: str, panel_id: int) -> tuple[str, bool]:
    """(текст, полная ли картина) для заключения и совета: из кэша онлайн-менеджера;
    кэш пуст или старый — одна загрузка карты на задачу. Карты нет (нет менеджера,
    сбой) — запасной блок из памяти движка, признак False. Свежесть — не живая."""
    mgr = None
    try:
        from online.manager import current_manager
        mgr = current_manager()
        if mgr is not None:
            text = await mgr.object_picture(router_sn, equip_type, int(panel_id), reload_stale=True)
            if text:
                return text, True
    except Exception:
        logger.warning("Картина объекта для %s/%s/%s не собрана — запасной блок",
                       router_sn, equip_type, panel_id, exc_info=True)
    return engine_fallback(mgr, router_sn, equip_type, panel_id), False


def engine_fallback(mgr, router_sn: str, equip_type: str, panel_id: int) -> str:
    """Запасной блок по движку машины в памяти менеджера (имя, двигатель, панель)."""
    try:
        eng = (getattr(mgr, "_engines", None) or {}).get(f"{router_sn}|{equip_type}|{panel_id}")
        if eng is not None:
            cfg = getattr(eng, "cfg", None)
            return fallback_text(getattr(eng, "equip_name", None),
                                 getattr(eng, "equip_engine", None) or getattr(cfg, "engine_label", None),
                                 getattr(cfg, "controller_label", None),
                                 serials=(router_sn, getattr(eng, "engine_sn", None)))
    except Exception:
        logger.debug("Картина объекта: запасной блок по движку не собран", exc_info=True)
    return fallback_text()
