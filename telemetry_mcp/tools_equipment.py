# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Инструменты 1, 2, 11, 12: оборудование и соседи, каталог регистров, паспорт
KB, справочник кодов (design_mcp_telemetry.md, разд. 5.2)."""
from __future__ import annotations

import asyncio
from collections import Counter
from types import SimpleNamespace
from typing import Annotated, Any, NamedTuple

from mcp.server.mcpserver import Context
from pydantic import Field

from analytics.labels import ROLE_LABELS, fmt_local, unit_ru

from config import get_tz

from .availability import Polled, fmt_value, is_exact, last_text, polled_registers
from .catalog import get_catalog, enum_text, bit_info
from .common import ToolError, UNITS, resolve_unit, run_tool, settings
from .envelope import Envelope
from .kb import kb_for, resolve_params
from .kb_notes import passport_notes, rows_with_notes

_ARG_UNIT = Annotated[str, Field(description="Установка: имя («ДЭС №3», «ДГУ №1») или id из equipment_tree")]


CATALOG_CAP = 150
# Краткий перечень: с какого числа строк общая дата последней записи выносится в шапку
COMMON_MARK_MIN = 8
# Сколько ждать перечни опроса в дереве оборудования: не успели — строка без счёта,
# запросы доходят в фоне и наполняют кэш
TREE_POLLED_WAIT_SEC = 6.0


class CatalogView(NamedTuple):
    text: str
    total: int
    truncation: list[str]
    notes: list[str]


def _norm_q(s: Any) -> str:
    return str(s or "").strip().lower().replace("ё", "е")


def _catalog_row(a: int, rm: dict[int, Any], cat: dict[int, Any], brief: bool = False) -> tuple[str, str, str]:
    """(строка каталога без пометки об опросе, текст для поиска, имя). brief — строка
    краткого перечня (v5.2.12): состояния не расшифровываются."""
    m, c = rm.get(a, {}), cat.get(a, {})
    role = m.get("role") or ""
    name = ROLE_LABELS.get(role) or m.get("description") or c.get("name_ru") or c.get("name") or ""
    kind = m.get("kind") or c.get("kind") or ""
    unit_s = unit_ru(m.get("unit") or c.get("unit") or "")
    hay = _norm_q(" ".join([str(a), name, role, str(c.get("name") or ""), str(m.get("description") or "")]))
    states = ""
    st = c.get("states")
    if kind == "enum" and isinstance(st, dict):
        lab = st.get("labels_ru") or st.get("labels") or {}
        kbl = m.get("labels_ru") if isinstance(m.get("labels_ru"), dict) else {}
        merged = {**{str(k): v for k, v in lab.items()}, **{str(k): v for k, v in kbl.items()}}
        states = "" if brief else " · " + ", ".join(f"{k}={v}" for k, v in list(merged.items())[:8])
    elif kind == "fault_bitmap" and isinstance(st, dict):
        states = f" · битов {len(st)}"
    kind_ru = {"analog": "аналог", "enum": "состояние", "fault_bitmap": "биты неисправностей"}.get(kind, kind)
    line = f"{a} | {name} | {unit_s} | {kind_ru}" + (f" | роль {role}" if role else "") + states
    return line, hay, name


def _kind(a: int, rm: dict[int, Any], cat: dict[int, Any]) -> str:
    return str((rm.get(a) or {}).get("kind") or (cat.get(a) or {}).get("kind") or "")


def _ranges(addrs: list[int]) -> str:
    """Адреса списком с диапазонами: «40400–40415, 40420, 40422–40428»."""
    xs = sorted(set(addrs))
    out: list[str] = []
    i = 0
    while i < len(xs):
        j = i
        while j + 1 < len(xs) and xs[j + 1] == xs[j] + 1:
            j += 1
        if j - i >= 2:
            out.append(f"{xs[i]}–{xs[j]}")
        else:
            out.extend(str(x) for x in xs[i:j + 1])
        i = j + 1
    return ", ".join(out)


def _mask_summary(masks: list[int], polled: dict[int, Polled] | None, exact: bool) -> str:
    """Маски неисправностей одной строкой — для краткого перечня (v5.2.12)."""
    parts: list[str] = []
    if polled is None:
        parts.append(_ranges(masks))
    else:
        on = [a for a in masks if a in polled]
        gone = [a for a in masks if a not in polled]
        if on:
            parts.append(("опрашиваются " if exact else "с записями ") + _ranges(on))
        held: dict[str, list[int]] = {}
        for a in on:
            p = polled[a]
            if p.held and exact:
                held.setdefault(fmt_value(p.value) if p.value is not None else "", []).append(a)
        for val, aa in held.items():
            parts.append("из них значение не менялось" + (f" ({val})" if val else "") + " — " + _ranges(aa))
        if gone:
            parts.append(("не опрашиваются " if exact else "записей нет ") + _ranges(gone))
    return (f"Маски неисправностей — регистров {len(masks)}, одной строкой: " + "; ".join(parts)
            + ". Что стоит за битом — fault_code (addr и bit); строка маски с числом битов и датой — "
              "запросом по её адресу или по слову «маска».")


def build_catalog(rm: dict[int, Any], cat: dict[int, Any], polled: dict[int, Polled] | None,
                  query: str | None, all_registers: bool, *, tz: Any = None,
                  cap: int = CATALOG_CAP) -> CatalogView:
    """Текст каталога регистров. polled — перечень опрашиваемых (availability); None —
    доступность не определена, каталог как до v5.2.11: регистры KB, по просьбе — вся
    карта панели, без пометок."""
    q = _norm_q(query)
    known = sorted(set(rm) | set(cat))
    notes: list[str] = []
    trunc: list[str] = []
    head = ""
    # exact — перечень из таблицы последних значений (полный); иначе выведен из записей:
    # регистр без записей либо не опрашивается, либо ни разу не менялся
    exact = is_exact(polled)
    off = "не опрашивается" if exact else "записей нет"
    if polled is None:
        addrs = sorted(set(rm) | (set(cat) if all_registers or not rm else set()))
        notes.append("доступность регистров определить не удалось — каталог без пометок об опросе")
    else:
        n_polled = sum(1 for a in known if a in polled)
        n_map = len(cat) or len(known)       # карта панели — каталог телеметрии (как в дереве оборудования)
        if exact:
            head = f"Опрашивается регистров: {n_polled} из {n_map} в карте панели. " + (
                "Показана вся карта панели: у регистров без данных пометка «не опрашивается»."
                if all_registers else "Показаны опрашиваемые: только по ним есть данные.")
        else:
            head = (f"Регистров с записями: {n_polled} из {n_map} в карте панели (перечень выведен из "
                    "записей: регистр, который опрашивается, но ни разу не менялся, в него не попадает). ") + (
                "Показана вся карта панели: у регистров без записей пометка «записей нет»."
                if all_registers else "Показаны регистры с записями.")
        addrs = known if all_registers else [a for a in known if a in polled]
        no_data = [a for a in sorted(rm) if a not in polled and (rm.get(a) or {}).get("role")]
        if no_data and exact:
            notes.append("регистры с ролью KB, которых нет в опросе: " + ", ".join(
                f"{a} ({rm[a]['role']})" for a in no_data[:12]) + (" …" if len(no_data) > 12 else ""))
    # Краткий перечень (v5.2.12): без слова поиска список длинный (113–137 регистров) и не
    # помещался в предел результата — хвост обрезался. Маски неисправностей идут одной
    # строкой, состояния не расшифровываются; полная строка регистра — по слову или адресу
    compact = not q
    masks: list[int] = []
    marks: list[str] = []          # пометка об опросе каждой строки — парой к rows
    rows: list[str] = []
    row_addrs: list[int] = []      # адрес каждой строки — для пометок базы знаний
    for a in addrs:
        line, hay, _name = _catalog_row(a, rm, cat, brief=compact)
        if q and q not in hay:
            continue
        if compact and _kind(a, rm, cat) == "fault_bitmap":
            masks.append(a)
            continue
        if polled is not None:
            marks.append(last_text(polled[a], tz, exact) if a in polled else off)
            line += " | " + marks[-1]
        rows.append(line)
        row_addrs.append(a)
    total = len(rows) + len(masks)
    if compact and total:
        head += (" " if head else "") + ("Перечень краткий: состояния не расшифрованы — полная строка регистра "
                                         "по слову или адресу (query).")
    if compact and marks:
        # Одна и та же «посл. запись ДД.ММ.ГГГГ» у большинства строк — в шапку, один раз
        common, n_common = (Counter(m for m in marks if m.startswith("посл. запись")).most_common(1) or [("", 0)])[0]
        if n_common >= max(COMMON_MARK_MIN, len(rows) // 2):
            tail = " | " + common
            rows = [r[:-len(tail)] if m == common else r for r, m in zip(rows, marks)]
            head += (f" Колонка «опрос» заполнена только там, где отличается: у остальных регистров — {common}.")
    if masks:
        head += "\n" + _mask_summary(masks, polled, exact)
    if not total:
        if polled is not None and not all_registers and q:
            others = []
            for a in known:
                if a in polled:
                    continue
                _line, hay, name = _catalog_row(a, rm, cat)
                if q in hay:
                    others.append(f"{a} {name}".strip())
            if others:
                text = ((f"Среди опрашиваемых регистров по запросу «{query}» ничего нет. В карте панели такие "
                         f"регистры есть ({len(others)}), но роутер их не опрашивает — данных по ним нет: ")
                        if exact else
                        (f"Среди регистров с записями по запросу «{query}» ничего нет. В карте панели такие "
                         f"регистры есть ({len(others)}), но записей по ним нет: ")) \
                    + "; ".join(others[:8]) + (" …" if len(others) > 8 else "")
            else:
                text = f"По запросу «{query}» ничего нет ни среди опрашиваемых регистров, ни в карте панели."
        elif polled is None and not all_registers:
            text = "Ничего не найдено. Попробуйте all_registers=true."
        else:
            text = "Ничего не найдено."
        return CatalogView((head + "\n" if head else "") + text, 0, trunc, notes)
    if len(rows) > cap:
        trunc.append(f"показано {cap + len(masks)} из {total} регистров — сузьте поиском")
        rows = rows[:cap]
    # Пометки базы знаний к регистрам (v5.2.13, kb_notes): в коротком списке — под строкой
    # регистра, с источником; в длинном — подсказка в примечаниях, у каких адресов они есть
    rows = rows_with_notes(rows, row_addrs, rm, notes)
    header = ("адрес | имя | ед. | вид | роль" if compact else "адрес | имя | ед. | вид | роль · состояния") \
        + (" | опрос" if polled is not None else "")
    return CatalogView("\n".join(([head] if head else []) + [header] + rows), total, trunc, notes)


async def _tree_polled(objects: list[dict[str, Any]]) -> dict[tuple, tuple[int, int]]:
    """{(роутер, тип, панель): (опрашивается, в карте панели)} для дерева оборудования.
    Ждём недолго: что не успело — без счёта в этот раз, запросы наполняют кэш в фоне."""
    refs: dict[tuple, Any] = {}
    for o in objects:
        for u in o.get("units", []):
            p = u["panel"]
            key = (o["router_sn"], p["equip_type"], int(p["panel_id"]))
            refs[key] = SimpleNamespace(triple=key, equip_type=p["equip_type"])
    if not refs:
        return {}
    tasks = {key: asyncio.ensure_future(polled_registers(ref)) for key, ref in refs.items()}
    await asyncio.wait(tasks.values(), timeout=TREE_POLLED_WAIT_SEC)
    out: dict[tuple, tuple[int, int]] = {}
    for key, t in tasks.items():
        if not t.done() or t.cancelled() or t.exception() is not None or t.result() is None:
            continue
        cat = await get_catalog(key[1])
        out[key] = (len(t.result()), len(cat))
    return out


def register(server: Any) -> None:

    @server.tool(structured_output=False, name="equipment_tree",
                 description="Оборудование и соседи: дерево объект → установки → панель и датчики. "
                             "С чего начинать: даёт имена и id установок для остальных инструментов, "
                             "соседей по объекту, привязку KB и когда были последние данные.")
    async def equipment_tree(
        ctx: Context,
        unit: Annotated[str | None, Field(description="Показать только объект этой установки (имя или id); пусто — всё")] = None,
    ) -> str:
        async def body(env: Envelope) -> str:
            tree = await UNITS.tree()
            objects = tree.get("objects", [])
            if unit:
                u = await resolve_unit(unit)
                objects = [o for o in objects if o["router_sn"] == u.router_sn]
                env.unit = u.title
            env.layer = "карта установок (аналитика) + реестр оборудования"
            lines: list[str] = []
            n_units = 0
            polled_n = await _tree_polled(objects)
            for o in objects:
                lines.append(f"Объект «{o['name']}»:")
                for u in o.get("units", []):
                    n_units += 1
                    p = u["panel"]
                    eng = " ".join(x for x in (p.get("manufacturer"), p.get("model")) if x) or "производитель/модель не заполнены"
                    sn = f", двигатель {p['engine_sn']}" if p.get("engine_sn") else ""
                    kb = f"KB: {p['binding']}" if p.get("binding") else "KB: привязка не задана"
                    last = p.get("last_data_ts")
                    last_s = f"последние данные {fmt_local(last, day=True)}" if last else "данных движка ещё нет"
                    act = "" if u.get("active") else " · выключена"
                    ident = f" [id {u['id']}]" if u.get("id") is not None else " [обращаться по имени]"
                    pn = polled_n.get((o["router_sn"], p["equip_type"], int(p["panel_id"])))
                    polled_s = f" · опрашивается регистров: {pn[0]} из {pn[1]}" if pn else ""
                    lines.append(f"  • {u['name']}{ident}{act} · панель {p['equip_type']}/{p['panel_id']} · "
                                 f"{eng}{sn} · {kb} · {last_s}{polled_s}")
                    sens = u.get("sensors") or []
                    if sens:
                        lines.append("     датчики: " + "; ".join(
                            f"{s['name']} ({s['equip_type']}/{s['panel_id']}" + (f", {s['role']}" if s.get("role") else "") + ")"
                            for s in sens))
                    if u.get("neighbors"):
                        lines.append("     соседи по объекту: " + ", ".join(u["neighbors"]))
                for s in o.get("unattached", []):
                    lines.append(f"  · не привязан к установке: {s['name']} ({s['equip_type']}/{s['panel_id']})")
            env.rows = n_units
            if not lines:
                return "Карта установок пуста: в реестре аналитики нет панелей (заполняется в настройках cg-analytics)."
            return "\n".join(lines)
        return await run_tool("equipment_tree", ctx, {"unit": unit}, body)

    @server.tool(structured_output=False, name="register_catalog",
                 description="Каталог регистров установки. По умолчанию — только регистры, которые опрашивает "
                             "роутер (по ним есть данные): адрес, русское имя, единица, вид, роль в KB, дата "
                             "последней записи. Без слова поиска перечень краткий: состояния не расшифрованы, "
                             "маски неисправностей одной строкой. Поиск по слову или адресу сужает список и "
                             "даёт полную строку регистра. all_registers=true — вся карта "
                             "панели: у регистров без данных пометка «не опрашивается», запрашивать их бесполезно. "
                             "В коротком списке (поиск по слову или адресу) под строкой регистра — пометка базы "
                             "знаний с источником, если она есть: из чего складывается значение и чем ограничено.")
    async def register_catalog(
        ctx: Context,
        unit: _ARG_UNIT,
        query: Annotated[str | None, Field(description="Слово для поиска по имени, роли или адресу (пусто — все опрашиваемые)")] = None,
        all_registers: Annotated[bool, Field(description="true — вся карта панели, включая регистры, которые роутер не опрашивает")] = False,
    ) -> str:
        async def body(env: Envelope) -> str:
            u = await resolve_unit(unit)
            env.unit, env.unit_id = u.title, u.id
            cfg, _ref, note = await kb_for(u)
            cat = await get_catalog(u.equip_type)
            env.layer = "каталог регистров телеметрии + mapping KB + записи установки"
            if note:
                env.notes.append(note)
            rm = cfg.register_map if cfg is not None else {}
            view = build_catalog(rm, cat, await polled_registers(u, cat), query, all_registers, tz=get_tz())
            env.rows = view.total
            env.truncation.extend(view.truncation)
            env.notes.extend(view.notes)
            return view.text
        return await run_tool("register_catalog", ctx, {"unit": unit, "query": query, "all_registers": all_registers},
                              body)

    @server.tool(structured_output=False, name="kb_passport",
                 description="Паспорт и пороги из базы знаний установки: паспортные значения РЭ, уставки "
                             "контроллера, настраиваемые пороги, зоны нагрузки, шаг датчиков, ссылки на "
                             "источники, пометки к регистрам (состав и ограничения значения, с источником). "
                             "Параметр сужает до своего раздела.")
    async def kb_passport(
        ctx: Context,
        unit: _ARG_UNIT,
        param: Annotated[str | None, Field(description="Параметр (давление масла, температура ОЖ, частота …); пусто — все разделы")] = None,
    ) -> str:
        async def body(env: Envelope) -> str:
            u = await resolve_unit(unit)
            env.unit, env.unit_id = u.title, u.id
            cfg, _ref, note = await kb_for(u)
            env.layer = "knowledge_base (thresholds, zones, detectors, пометки к регистрам)"
            if cfg is None:
                raise ToolError(note or "KB для установки не собрана")
            lines: list[str] = []
            eng, ctl = cfg.engine_label, cfg.controller_label
            lines.append("Профиль: " + " · ".join(x for x in (f"двигатель {eng}" if eng else None,
                                                             f"контроллер {ctl}" if ctl else None) if x))
            thr = cfg.thresholds or {}
            dets = cfg.detectors or {}
            sections = list(thr.keys())
            det_sections: list[str] = []
            # Пометки базы знаний к регистрам (v5.2.13, kb_notes): с параметром — пометка его
            # регистра, без параметра — все; у параметра без разделов порогов пометка — не отказ
            cat = None
            note_addrs: list[int] | None = None
            if param:
                q = param.strip().lower().replace("ё", "е")
                try:
                    cat = await get_catalog(u.equip_type)
                    pref = resolve_params(cfg, cat, [param])[0]
                    role, note_addrs = pref.role, [pref.addr]
                except ToolError:
                    role, note_addrs = None, []
                keys = {k for k in sections if _section_matches(k, q, role)}
                det_sections = [k for k in dets if isinstance(dets.get(k), dict) and _section_matches(k, q, role)]
                if not keys and not det_sections and not passport_notes(cfg.register_map, cat, note_addrs):
                    raise ToolError(f"Раздел порогов для «{param}» не найден. Разделы thresholds: "
                                    + ", ".join(sections) + "; детекторы: " + ", ".join(sorted(dets)))
                sections = [k for k in sections if k in keys]
            for k in sections:
                node = thr.get(k)
                if not isinstance(node, dict):
                    continue
                lines.append(f"\n[{k}]")
                lines.extend(_dump(node, "  "))
            # Пороги детекторов (detectors.yaml): несимметрия, нейтраль, недогруз и т. п. —
            # с norm_ref (ссылка на документ), которые владелец ценит
            for k in det_sections:
                lines.append(f"\n[детектор {k}]")
                lines.extend(_dump(dets[k], "  "))
            lines.extend(passport_notes(cfg.register_map, cat, note_addrs))
            if not param:
                try:
                    zb = cfg.zone_boundaries()
                    lines.append("\n[зоны нагрузки, % номинала] " + "; ".join(
                        f"{z}: {a:g}–{b:g}" for z, (a, b) in zb.items()))
                except Exception:  # noqa: BLE001
                    pass
            steps = []
            for role in sorted({m.get("role") for m in cfg.register_map.values() if m.get("role")}):
                st, src = cfg.sensor_step(role)
                if st:
                    steps.append(f"{ROLE_LABELS.get(role, role)} {st:g} ({'по истории' if src == 'history' else 'умолчание KB'})")
            if steps and not param:
                lines.append("\n[шаг датчиков] " + "; ".join(steps))
            env.rows = len(lines)
            return "\n".join(lines)
        return await run_tool("kb_passport", ctx, {"unit": unit, "param": param}, body)

    @server.tool(structured_output=False, name="fault_code",
                 description="Справочник кодов панели: что говорит панель про код неисправности или бит "
                             "маски (имя, серьёзность, описание, причины и шаги из KB). «Как в мануале» — "
                             "это не здесь, а у Библиотекаря.")
    async def fault_code(
        ctx: Context,
        unit: _ARG_UNIT,
        code: Annotated[int | None, Field(description="Код неисправности панели (например 1448, 234, 415)")] = None,
        addr: Annotated[int | None, Field(description="Адрес регистра маски (40400…40428), вместе с bit")] = None,
        bit: Annotated[int | None, Field(description="Номер бита маски 0…15, вместе с addr")] = None,
    ) -> str:
        async def body(env: Envelope) -> str:
            u = await resolve_unit(unit)
            env.unit, env.unit_id = u.title, u.id
            cfg, ref, note = await kb_for(u)
            env.layer = "справочник кодов KB (fault_ref) + каталог битов"
            if note:
                env.notes.append(note)
            out: list[str] = []
            if code is not None:
                rec = ref.lookup(int(code)) if ref else None
                name = cfg.fault_code_name(code) if cfg is not None else None
                if rec is None and not name:
                    return f"Код {code} в справочнике KB не найден."
                out.append(f"Код {code}: {name or rec.get('name_ru') or rec.get('name') or ''}")
                if rec:
                    md = ref.format_for_report(int(code)) if ref else None
                    if md:
                        out.append(md.strip())
                    else:
                        if rec.get("severity"):
                            out.append(f"серьёзность: {rec['severity']}")
                        if rec.get("ru") or rec.get("en"):
                            out.append(str(rec.get("ru") or rec.get("en")))
            elif addr is not None and bit is not None:
                cat = await get_catalog(u.equip_type)
                info = bit_info((cat.get(int(addr)) or {}).get("states"), int(bit))
                if not info:
                    return f"Бит {bit} регистра {addr} в каталоге не описан."
                out.append(f"Регистр {addr} бит {bit}: {info.get('name_ru') or info.get('name')} "
                           f"[{info.get('severity') or '—'}]")
                if ref:
                    rec = ref.lookup_by_name(info.get("name") or "", info.get("severity"))
                    if rec and rec.get("code") is not None:
                        out.append(f"соответствует коду {rec['code']}: " + (ref.format_for_report(int(rec["code"])) or ""))
            else:
                raise ToolError("Укажите code или пару addr и bit.")
            env.rows = 1
            return "\n".join(out)
        return await run_tool("fault_code", ctx, {"unit": unit, "code": code, "addr": addr, "bit": bit}, body)


def _section_matches(key: str, q: str, role: str | None) -> bool:
    k = key.lower()
    tokens = {
        "OIL_PRESS": ("oil_press", "oil_dilution"), "OIL_TEMP": ("oil_temp",), "COOLANT_TEMP": ("coolant", "cooling"),
        "BATTERY_V": ("battery",), "FREQUENCY": ("freq",), "RPM": ("rpm", "speed"),
        "ACTIVE_POWER_TOTAL": ("power", "load"), "LOAD_PCT": ("load",), "VOLTAGE_LL_AVG": ("volt",),
        "CURRENT_AVG": ("current", "neg_seq", "negative", "neutral", "phase"),
        "CURRENT_L1": ("current", "phase", "neutral", "neg_seq"), "CURRENT_L2": ("current", "phase", "neutral", "neg_seq"),
        "CURRENT_L3": ("current", "phase", "neutral", "neg_seq"), "FUEL_RATE": ("fuel",),
        "BOOST_PRESSURE": ("boost", "intake"), "INTAKE_MANIFOLD_TEMP": ("intake",),
    }
    if role and any(t in k for t in tokens.get(role, ())):
        return True
    words = [w for w in q.replace("_", " ").split() if len(w) > 1]
    ru = {"масл": ("oil",), "давлен": ("press",), "температ": ("temp",), "ож": ("coolant",), "охлажд": ("coolant", "cooling"),
          "част": ("freq",), "оборот": ("rpm", "speed"), "акб": ("battery",), "батар": ("battery",),
          "мощн": ("power",), "нагруз": ("load",), "напряж": ("volt",), "ток": ("current", "neutral", "neg_seq", "phase"),
          "нейтрал": ("neutral",), "несимм": ("neg_seq", "phase", "neutral"), "перекос": ("phase", "neg_seq"),
          "топлив": ("fuel",), "наддув": ("boost",), "нагар": ("coking", "wet"), "закокс": ("coking", "wet")}
    for w in words:
        for pre, ens in ru.items():
            if w.startswith(pre) and any(en in k for en in ens):
                return True
        if len(w) > 2 and w in k:
            return True
    return False


def _dump(node: dict[str, Any], indent: str) -> list[str]:
    out: list[str] = []
    for k, v in node.items():
        if k.startswith("_"):
            if k == "_source":
                out.append(f"{indent}источник: {v}")
            continue
        if isinstance(v, dict):
            out.append(f"{indent}{k}:")
            out.extend(_dump(v, indent + "  "))
        else:
            out.append(f"{indent}{k}: {v}")
    return [x for x in out if x]
