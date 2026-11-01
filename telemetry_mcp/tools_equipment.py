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

from typing import Annotated, Any

from mcp.server.mcpserver import Context
from pydantic import Field

from analytics.labels import ROLE_LABELS, fmt_local, unit_ru

from .catalog import get_catalog, enum_text, bit_info
from .common import ToolError, UNITS, resolve_unit, run_tool, settings
from .envelope import Envelope
from .kb import kb_for, resolve_params

_ARG_UNIT = Annotated[str, Field(description="Установка: имя («ДЭС №3», «ДГУ №1») или id из equipment_tree")]


def register(server: Any) -> None:

    @server.tool(name="equipment_tree",
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
                    lines.append(f"  • {u['name']}{ident}{act} · панель {p['equip_type']}/{p['panel_id']} · "
                                 f"{eng}{sn} · {kb} · {last_s}")
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

    @server.tool(name="register_catalog",
                 description="Каталог регистров установки: адрес, русское имя, единица, вид "
                             "(аналог/состояние/биты), роль в KB, расшифровка состояний. "
                             "Поиск по слову сужает список.")
    async def register_catalog(
        ctx: Context,
        unit: _ARG_UNIT,
        query: Annotated[str | None, Field(description="Слово для поиска по имени/роли (пусто — все регистры KB)")] = None,
        all_registers: Annotated[bool, Field(description="true — весь каталог панели, не только регистры KB")] = False,
    ) -> str:
        async def body(env: Envelope) -> str:
            u = await resolve_unit(unit)
            env.unit, env.unit_id = u.title, u.id
            cfg, _ref, note = await kb_for(u)
            cat = await get_catalog(u.equip_type)
            env.layer = "каталог регистров телеметрии + mapping KB"
            if note:
                env.notes.append(note)
            rm = cfg.register_map if cfg is not None else {}
            addrs = sorted(set(rm) | (set(cat) if all_registers or not rm else set()))
            q = (query or "").strip().lower().replace("ё", "е")
            rows: list[str] = []
            for a in addrs:
                m, c = rm.get(a, {}), cat.get(a, {})
                role = m.get("role") or ""
                name = ROLE_LABELS.get(role) or m.get("description") or c.get("name_ru") or c.get("name") or ""
                kind = m.get("kind") or c.get("kind") or ""
                unit_s = unit_ru(m.get("unit") or c.get("unit") or "")
                hay = " ".join([str(a), name, role, str(c.get("name") or ""), str(m.get("description") or "")]).lower()
                if q and q not in hay.replace("ё", "е"):
                    continue
                states = ""
                st = c.get("states")
                if kind == "enum" and isinstance(st, dict):
                    lab = st.get("labels_ru") or st.get("labels") or {}
                    kbl = m.get("labels_ru") if isinstance(m.get("labels_ru"), dict) else {}
                    merged = {**{str(k): v for k, v in lab.items()}, **{str(k): v for k, v in kbl.items()}}
                    states = " · " + ", ".join(f"{k}={v}" for k, v in list(merged.items())[:8])
                elif kind == "fault_bitmap" and isinstance(st, dict):
                    states = f" · битов {len(st)}"
                kind_ru = {"analog": "аналог", "enum": "состояние", "fault_bitmap": "биты неисправностей"}.get(kind, kind)
                rows.append(f"{a} | {name} | {unit_s} | {kind_ru}" + (f" | роль {role}" if role else "") + states)
            cap = 150
            env.rows = len(rows)
            if len(rows) > cap:
                env.truncation.append(f"показано {cap} из {len(rows)} регистров — сузьте поиском")
                rows = rows[:cap]
            if not rows:
                return "Ничего не найдено." + (" Попробуйте all_registers=true." if not all_registers else "")
            return "адрес | имя | ед. | вид | роль · состояния\n" + "\n".join(rows)
        return await run_tool("register_catalog", ctx, {"unit": unit, "query": query, "all_registers": all_registers},
                              body)

    @server.tool(name="kb_passport",
                 description="Паспорт и пороги из базы знаний установки: паспортные значения РЭ, уставки "
                             "контроллера, настраиваемые пороги, зоны нагрузки, шаг датчиков, ссылки на "
                             "источники. Параметр сужает до своего раздела.")
    async def kb_passport(
        ctx: Context,
        unit: _ARG_UNIT,
        param: Annotated[str | None, Field(description="Параметр (давление масла, температура ОЖ, частота …); пусто — все разделы")] = None,
    ) -> str:
        async def body(env: Envelope) -> str:
            u = await resolve_unit(unit)
            env.unit, env.unit_id = u.title, u.id
            cfg, _ref, note = await kb_for(u)
            env.layer = "knowledge_base (thresholds, zones, detectors)"
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
            if param:
                q = param.strip().lower().replace("ё", "е")
                try:
                    cat = await get_catalog(u.equip_type)
                    role = resolve_params(cfg, cat, [param])[0].role
                except ToolError:
                    role = None
                keys = {k for k in sections if _section_matches(k, q, role)}
                det_sections = [k for k in dets if isinstance(dets.get(k), dict) and _section_matches(k, q, role)]
                if not keys and not det_sections:
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

    @server.tool(name="fault_code",
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
