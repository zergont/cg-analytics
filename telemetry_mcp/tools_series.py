# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Инструменты 3, 4, 10: ряд параметров, снимок состояния, долгие тренды
(design_mcp_telemetry.md, разд. 5.2, формы 6.2, 6.3, 6.5)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from mcp.server.mcpserver import Context
from pydantic import Field

from analytics import source as _src
from analytics.labels import ROLE_LABELS, fmt_duration, fmt_local, unit_ru
from analytics.serializer import RUN_STATE_RU

from .catalog import get_catalog, enum_text
from .common import (ToolError, UnitRef, fmt_num, fmt_t, neighbors_of, now_utc, parse_ts,
                     parse_window, resolve_unit, run_tool, settings)
from .envelope import Envelope
from .history import bucketed_history, gap_pairs, load_history, retention_cutoff, retention_note
from .kb import ParamRef, kb_for, key_roles, nominal_for, param_from_addr, resolve_params
from .series import bucket_times, from_buckets, held_points, nice_step, resample

_ARG_UNIT = Annotated[str, Field(description="Установка: имя («ДЭС №3») или id из equipment_tree")]
_ARG_FROM = Annotated[str | None, Field(description="Начало окна, пояс сервиса (МСК): «13.10.2026 12:00», «2026-10-13 12:00:00»")]
_ARG_TO = Annotated[str | None, Field(description="Конец окна; пусто — сейчас")]
_ARG_AT = Annotated[str | None, Field(description="Вместо от/до: момент, вокруг которого окно ± minutes")]
_ARG_MIN = Annotated[int | None, Field(description="Полуширина окна вокруг at, минут (по умолчанию 30)")]


def _split(s: str | None) -> list[str]:
    if not s:
        return []
    return [x.strip() for x in str(s).replace(";", ",").split(",") if x.strip()]


def _multi_day(t0: datetime, t1: datetime) -> bool:
    return (fmt_local(t0, day=True) or "")[:5] != (fmt_local(t1 - timedelta(seconds=1), day=True) or "")[:5]


async def _series_for(unit: UnitRef, cfg: Any, prefs: list[ParamRef], t0: datetime, t1: datetime,
                      ) -> tuple[dict[str, list], dict[str, int], list[tuple[datetime, datetime | None]]]:
    addrs = frozenset(p.addr for p in prefs)
    gaps = gap_pairs(await _src.get_data_gaps(*unit.triple, t0, t1))
    rows, st = await load_history(*unit.triple, t0, t1, addrs)
    series = {p.label: held_points(rows, p.addr) for p in prefs}
    return series, st, gaps


def register(server: Any) -> None:

    @server.tool(name="series",
                 description="Ряд параметров установки за окно: значения удержаны до следующей записи "
                             "(запись по изменению), при укрупнении шага — мин / среднее по времени / макс. "
                             "Сводка по колонкам. with_neighbors=true добавляет те же параметры соседних "
                             "установок объекта на той же сетке (обе машины в одном ответе).")
    async def series(
        ctx: Context,
        unit: _ARG_UNIT,
        params: Annotated[str, Field(description="Параметры через запятую: мощность, частота, обороты, ток L1, "
                                                 "давление масла, температура ОЖ, напряжение, АКБ … или адреса регистров (до 8)")],
        t_from: _ARG_FROM = None,
        t_to: _ARG_TO = None,
        at: _ARG_AT = None,
        minutes: _ARG_MIN = None,
        points: Annotated[int | None, Field(description="Сколько точек по времени (по умолчанию и не больше 150)")] = None,
        with_neighbors: Annotated[bool, Field(description="Добавить колонки соседних установок объекта")] = False,
    ) -> str:
        st = settings()

        async def body(env: Envelope) -> str:
            u = await resolve_unit(unit)
            env.unit, env.unit_id = u.title, u.id
            cfg, _ref, note = await kb_for(u)
            if note:
                env.notes.append(note)
            cat = await get_catalog(u.equip_type)
            prefs = resolve_params(cfg, cat, _split(params), max_params=int(st.limit("series_max_params")))
            t0, t1 = parse_window(t_from, t_to, at, minutes, default_hours=2,
                                  max_days=float(st.limit("series_max_window_days")))
            env.window_from, env.window_to = t0, t1
            env.layer = "очищенный слой: защита меток, удержание до следующей записи"
            n_pts = min(int(points or st.limit("series_max_points")), int(st.limit("series_max_points")))
            raw_hours = float(st.limit("series_raw_window_hours"))
            long_window = (t1 - t0) > timedelta(hours=raw_hours)
            step_sec = nice_step((t1 - t0).total_seconds() / max(1, n_pts)) if long_window else 0

            # Колонки: своя установка и соседи (те же роли у соседа; тот же адрес при том же типе панели)
            columns: list[tuple[UnitRef, Any, list[ParamRef]]] = [(u, cfg, prefs)]
            if with_neighbors:
                for nb in await neighbors_of(u):
                    ncfg, _r, _n = await kb_for(nb)
                    ncat = await get_catalog(nb.equip_type)
                    nprefs: list[ParamRef] = []
                    for p in prefs:
                        a = ncfg.role_to_addr(p.role) if (ncfg is not None and p.role) else None
                        if a is None and nb.equip_type == u.equip_type:
                            a = p.addr
                        if a is None:
                            continue
                        try:
                            np_ = param_from_addr(ncfg, ncat, a)
                        except ToolError:
                            continue
                        np_.label = f"{nb.name}: {p.label}"
                        nprefs.append(np_)
                    if nprefs:
                        columns.append((nb, ncfg, nprefs))

            steps: dict[str, float | None] = {}
            series_all: dict[str, Any] = {}
            gaps: list = []
            for cu, _ccfg, cprefs in columns:
                for p in cprefs:
                    env.units[p.label] = p.unit
                    steps[p.label] = p.step
                if long_window:
                    # Укрупнение на стороне БД (v5.2.3): сырые строки за недели не вычитать за срок вызова;
                    # series_all[метка] → укрупнённый набор этой установки (Resampled)
                    cgaps = gap_pairs(await _src.get_data_gaps(*cu.triple, t0, t1))
                    agg = await bucketed_history(*cu.triple, t0, t1, frozenset(p.addr for p in cprefs), step_sec)
                    part = from_buckets({p.label: agg.get(p.addr, []) for p in cprefs}, t0, t1, step_sec, cgaps)
                    for p in cprefs:
                        series_all[p.label] = part
                    if cu is u:
                        gaps = cgaps
                    elif cgaps:
                        env.notes.append(f"у {cu.name} разрывы связи: " + "; ".join(
                            f"{fmt_t(a, day=True)}–{fmt_t(b, day=True) if b else '…'}" for a, b in cgaps[:4]))
                else:
                    ser, drops, cgaps = await _series_for(cu, _ccfg, cprefs, t0, t1)
                    for k, v in drops.items():
                        env.dropped[k] = env.dropped.get(k, 0) + v
                    if cu is u:
                        gaps = cgaps
                    elif cgaps:
                        env.notes.append(f"у {cu.name} разрывы связи: " + "; ".join(
                            f"{fmt_t(a)}–{fmt_t(b) if b else '…'}" for a, b in cgaps[:4]))
                    series_all.update(ser)
            env.gaps = gaps

            if long_window:
                # Склейка укрупнённых рядов всех колонок на одной сетке корзин
                times = bucket_times(t0, t1, step_sec)
                cells = {k: series_all[k].cells.get(k, [None] * len(times)) for k in series_all}
                summary = {k: series_all[k].summary.get(k, {}) for k in series_all}
                n_raw = {k: series_all[k].n_raw.get(k, 0) for k in series_all}
                from .series import Resampled
                rs = Resampled("bucket", step_sec, times, cells, summary, n_raw)
                env.layer = "укрупнение в БД по корзинам (среднее по времени удержания); защита меток — только от меток в будущем"
                env.notes.append(f"окно длиннее {raw_hours:g} ч — укрупнено в БД до шага {fmt_duration(step_sec)}; "
                                 "кадры застывших часов здесь не отсеиваются")
            else:
                rs = resample(series_all, t0, t1, n_pts, gaps)
            env.rows = len(rs.times)
            if rs.mode == "bucket":
                env.truncation.append(f"укрупнено до шага {fmt_duration(rs.step_sec)} ({len(rs.times)} точек; "
                                      "в ячейке мин/ср.по времени/макс)")
            if not any(rs.summary.get(k) for k in rs.summary):
                rn = await retention_note(t0, t1)
                if rn:
                    env.notes.append(rn)
            env.notes.append("запись по изменению — значение держится до следующей записи; в разрыве связи — пусто")
            for p in prefs:
                nom = nominal_for(cfg, p.role)
                if nom:
                    env.notes.append(f"{p.label}: номинал {fmt_num(nom[0])} {p.unit} ({nom[1]})")
            keys = list(series_all.keys())
            day = _multi_day(t0, t1)
            # Сводка — ПЕРЕД таблицей: при усечении по размеру теряются строки, а не итоги (приёмка Э1)
            lines = ["сводка:"]
            for k in keys:
                s = rs.summary.get(k) or {}
                if not s:
                    lines.append(f"  {k}: записей нет")
                    continue
                lines.append(f"  {k}: первое {fmt_num(s['first'][1], steps.get(k))} ({fmt_t(s['first'][0], day=day)}), "
                             f"последнее {fmt_num(s['last'][1], steps.get(k))} ({fmt_t(s['last'][0], day=day)}), "
                             f"мин {fmt_num(s['min'][1], steps.get(k))} ({fmt_t(s['min'][0], day=day)}), "
                             f"макс {fmt_num(s['max'][1], steps.get(k))} ({fmt_t(s['max'][0], day=day)}), "
                             f"записей {s['n']}")
            lines.append("")
            lines.append("время | " + " | ".join(keys))
            for i, t in enumerate(rs.times):
                row_cells = []
                for k in keys:
                    c = rs.cells[k][i] if i < len(rs.cells.get(k, [])) else None
                    if c is None:
                        row_cells.append("—")
                    elif isinstance(c, tuple):
                        mn, av, mx = c
                        row_cells.append(fmt_num(av, steps.get(k)) if abs(mx - mn) < 1e-9
                                         else f"{fmt_num(mn, steps.get(k))}/{fmt_num(av, steps.get(k))}/{fmt_num(mx, steps.get(k))}")
                    else:
                        row_cells.append(fmt_num(c, steps.get(k)))
                lines.append(fmt_t(t, day=day) + " | " + " | ".join(row_cells))
            if not rs.times:
                lines.append("(в окне нет ни одной записи выбранных регистров)")
            return "\n".join(lines)
        return await run_tool("series", ctx, {"unit": unit, "params": params, "t_from": t_from, "t_to": t_to,
                                              "at": at, "minutes": minutes, "points": points,
                                              "with_neighbors": with_neighbors}, body)

    @server.tool(name="state_at",
                 description="Снимок состояния установки на момент: значение, единица и ВОЗРАСТ значения "
                             "(когда менялось в последний раз), режим и состояния панели с расшифровкой, "
                             "активные неисправности с серьёзностью. По умолчанию ключевые параметры.")
    async def state_at(
        ctx: Context,
        unit: _ARG_UNIT,
        at: Annotated[str | None, Field(description="Момент (пояс сервиса); пусто — сейчас")] = None,
        params: Annotated[str | None, Field(description="«ключевые» (по умолчанию), «все» или список параметров через запятую")] = None,
    ) -> str:
        st = settings()

        async def body(env: Envelope) -> str:
            u = await resolve_unit(unit)
            env.unit, env.unit_id = u.title, u.id
            cfg, _ref, note = await kb_for(u)
            if note:
                env.notes.append(note)
            cat = await get_catalog(u.equip_type)
            t = parse_ts(at) or now_utc()
            max_age = timedelta(days=float(st.limit("state_max_age_days")))
            env.window_from = env.window_to = t
            env.layer = "последние записи до момента (защита меток) + периоды состояний и неисправностей"
            env.notes.append(f"последнее значение ищется не дальше {max_age.days} сут до момента")
            cut = await retention_cutoff()
            if cut is not None and t < cut:
                env.notes.append(f"момент старше срока хранения истории в телеметрии (записи с {cut:%d.%m.%Y}): "
                                 "аналоговых значений нет, только состояния и неисправности")
            sel = (params or "ключевые").strip().lower()
            rm = cfg.register_map if cfg is not None else {}
            if sel in ("ключевые", "key", ""):
                roles = key_roles(cfg) if cfg is not None else []
                analog = [cfg.role_to_addr(r) for r in roles]
                analog = [a for a in analog if a is not None and rm.get(a, {}).get("kind", "analog") == "analog"]
                enum_addrs = sorted(set(_src.enum_read_addrs(cfg))) if cfg is not None else []
            elif sel in ("все", "all"):
                analog = sorted(a for a, m in rm.items() if m.get("kind") == "analog")
                if not analog:
                    analog = sorted(a for a, c in cat.items() if c.get("kind") == "analog")
                enum_addrs = sorted(set(a for a, c in cat.items() if c.get("kind") == "enum")
                                    | set(_src.enum_read_addrs(cfg) if cfg is not None else []))
                cap = int(st.limit("snapshot_max_registers"))
                if len(analog) + len(enum_addrs) > cap:
                    env.truncation.append(f"регистров больше {cap} — показаны первые, укажите список")
                    analog = analog[:cap]
            else:
                prefs = resolve_params(cfg, cat, _split(params))
                analog = [p.addr for p in prefs if p.kind == "analog"]
                enum_addrs = [p.addr for p in prefs if p.kind == "enum"]
            out: list[str] = []
            gaps = gap_pairs(await _src.get_data_gaps(*u.triple, t - timedelta(hours=6), t + timedelta(seconds=1)))
            for a, b in gaps:
                if a <= t and (b is None or t < b):
                    env.notes.append(f"момент внутри разрыва связи {fmt_t(a)}–{fmt_t(b) if b else '…'}: "
                                     "значения — последние до обрыва")
            if analog:
                vals = await _src.get_last_values_before(*u.triple, list(analog), t, max_age.total_seconds())
                for a in analog:
                    p = param_from_addr(cfg, cat, a)
                    v = vals.get(a)
                    if not v:
                        out.append(f"{p.label}: нет записи за {max_age.days} сут до момента")
                        continue
                    age = (t - v["ts"]).total_seconds()
                    out.append(f"{p.label}: {fmt_num(v['value'], p.step)} {p.unit} · возраст {fmt_duration(age)} "
                               f"(с {fmt_t(v['ts'], day=True)})")
            if enum_addrs:
                periods = await _src.get_enum_periods(*u.triple, t - max_age, t + timedelta(seconds=1),
                                                      addrs=sorted(enum_addrs))
                latest: dict[int, dict] = {}
                for p in periods:
                    if p["state_start"] <= t and (p.get("state_end") is None or t < p["state_end"]):
                        cur = latest.get(p["addr"])
                        if cur is None or p["state_start"] > cur["state_start"]:
                            latest[p["addr"]] = p
                from .kb import role_label_ru
                for a in enum_addrs:
                    p = latest.get(a)
                    m = rm.get(a, {})
                    name = role_label_ru(m.get("role")) or m.get("description") or (cat.get(a) or {}).get("name_ru") or f"регистр {a}"
                    if p is None:
                        out.append(f"{name}: состояние на момент неизвестно")
                        continue
                    from analytics.labels import enum_label
                    lab = enum_label(m, p.get("value"), p.get("label")) or str(p.get("value"))
                    if a == cfg.role_to_addr("RUN_STATE") if cfg is not None else False:
                        lab = RUN_STATE_RU.get(int(p["value"]), lab) if p.get("value") is not None else lab
                    out.append(f"{name}: {lab} · с {fmt_t(p['state_start'], day=True)} "
                               f"({fmt_duration((t - p['state_start']).total_seconds())})")
            faults = await _src.get_fault_periods(*u.triple, t - max_age, t + timedelta(seconds=1))
            act = [f for f in faults if f["fault_start"] <= t and (f.get("fault_end") is None or t < f["fault_end"])]
            if act:
                out.append("активные неисправности:")
                for f in sorted(act, key=lambda x: x["fault_start"]):
                    fname = f.get("fault_name_ru") or f.get("fault_name") or f"{f['addr']}/{f['bit']}"
                    out.append(f"  • {fname} [{f.get('severity') or '—'}] с {fmt_t(f['fault_start'], day=True)} "
                               f"({fmt_duration((t - f['fault_start']).total_seconds())})")
            else:
                out.append("активных неисправностей на момент нет")
            env.rows = len(out)
            return "\n".join(out)
        return await run_tool("state_at", ctx, {"unit": unit, "at": at, "params": params}, body)

    @server.tool(name="trend",
                 description="Долгие тренды (дни…месяцы) из характеристик сегментов аналитики: по суткам "
                             "или по сегментам работы — медиана по времени, минимум, максимум параметра, "
                             "часы работы. Запасной путь без аналитики — часовые агрегаты «по отсчётам».")
    async def trend(
        ctx: Context,
        unit: _ARG_UNIT,
        params: Annotated[str, Field(description="Параметры через запятую (до 4)")],
        t_from: _ARG_FROM = None,
        t_to: _ARG_TO = None,
        step: Annotated[str, Field(description="«сутки» (по умолчанию) или «сегмент»")] = "сутки",
    ) -> str:
        st = settings()

        async def body(env: Envelope) -> str:
            u = await resolve_unit(unit)
            env.unit, env.unit_id = u.title, u.id
            cfg, _ref, note = await kb_for(u)
            if note:
                env.notes.append(note)
            cat = await get_catalog(u.equip_type)
            prefs = resolve_params(cfg, cat, _split(params), max_params=4)
            t0, t1 = parse_window(t_from, t_to, None, None, default_hours=24 * 30,
                                  max_days=float(st.limit("trend_max_window_days")))
            env.window_from, env.window_to = t0, t1
            from .queries import segments_in_window, hourly_aggregates
            segs = [s for s in await segments_in_window(*u.triple, t0, t1, limit=5000)
                    if s.get("t_end") is not None and (s.get("run_state") or 0) != 0]
            for p in prefs:
                env.units[p.label] = p.unit
            by_seg = (step or "").strip().lower().startswith("сегм")
            rows: dict[str, dict[str, Any]] = {}
            tz = _tz()
            for s in segs:
                cj = s.get("characteristics_json") or {}
                subs = cj.get("subsegments") or []
                key = f"#{s['id']} {RUN_STATE_RU.get(s.get('run_state'), '')} {fmt_local(s['t_start'], day=True)}" if by_seg \
                    else s["t_start"].astimezone(tz).strftime("%d.%m.%Y")
                r = rows.setdefault(key, {"w": 0.0, "p": {p.label: {"mw": 0.0, "w": 0.0, "min": None, "max": None} for p in prefs}})
                for sub in subs:
                    w = float(sub.get("duration_sec") or 0)
                    r["w"] += w
                    chars = sub.get("characteristics") or {}
                    for p in prefs:
                        c = chars.get(p.role or "") if p.role else None
                        if not isinstance(c, dict) or c.get("median") is None:
                            continue
                        acc = r["p"][p.label]
                        try:
                            med = float(c["median"])
                        except (TypeError, ValueError):
                            continue
                        acc["mw"] += w * med
                        acc["w"] += w
                        for k, f in (("min", min), ("max", max)):
                            if c.get(k) is not None:
                                acc[k] = c[k] if acc[k] is None else f(acc[k], c[k])
            # Регистры вне KB (нет роли → нет характеристик аналитики): по суткам из часовых
            # агрегатов телеметрии, среднее ПО ОТСЧЁТАМ — с пометкой (приёмка Э1: 40030)
            by_samples = [p for p in prefs if not p.role]
            ok_samples: list[ParamRef] = []
            if by_samples and not by_seg:
                for p in by_samples:
                    try:
                        agg_rows = await hourly_aggregates(*u.triple, p.addr, t0, t1)
                    except Exception:  # noqa: BLE001 — на реплике без TimescaleDB агрегатов нет
                        env.notes.append(f"{p.label}: регистр вне KB, а часовых агрегатов в источнике нет "
                                         "(history_1hour) — возьмите series по дням")
                        continue
                    ok_samples.append(p)
                    for a in agg_rows:
                        key = a["bucket"].astimezone(tz).strftime("%d.%m.%Y")
                        r = rows.setdefault(key, {"w": 0.0, "p": {q.label: {"mw": 0.0, "w": 0.0, "min": None, "max": None} for q in prefs}})
                        acc = r["p"][p.label]
                        nn = int(a.get("sample_count") or 0)
                        if a.get("avg_value") is not None and nn:
                            acc["mw"] += float(a["avg_value"]) * nn
                            acc["w"] += nn
                        for k, f in (("min", min), ("max", max)):
                            v = a.get(f"{k}_value")
                            if v is not None:
                                acc[k] = float(v) if acc[k] is None else f(acc[k], float(v))
                if ok_samples:
                    env.notes.append("колонки " + ", ".join(p.label for p in ok_samples)
                                     + " — регистры вне KB: по часовым агрегатам телеметрии, среднее ПО ОТСЧЁТАМ, не по времени")
            elif by_samples:
                env.notes.append("колонки " + ", ".join(p.label for p in by_samples)
                                 + " — регистры вне KB: в шаге «сегмент» недоступны (нет характеристик аналитики), возьмите шаг «сутки»")
            if rows and not by_seg:
                rows = dict(sorted(rows.items(), key=lambda kv: datetime.strptime(kv[0], "%d.%m.%Y")))
            if rows:
                env.layer = "характеристики сегментов аналитики (медианы по времени, с очисткой)"
                cap = int(st.limit("trend_max_points"))
                keys = list(rows.keys())
                if len(keys) > cap:
                    env.truncation.append(f"точек больше {cap} — показаны последние {cap}")
                    keys = keys[-cap:]
                env.rows = len(keys)
                lines = ["период | часы работы | " + " | ".join(f"{p.label} (медиана/мин/макс)" for p in prefs)]
                for k in keys:
                    r = rows[k]
                    cells = []
                    for p in prefs:
                        a = r["p"][p.label]
                        if a["w"] <= 0:
                            cells.append("—")
                        else:
                            cells.append(f"{fmt_num(a['mw'] / a['w'], p.step)}/{fmt_num(a['min'], p.step)}/{fmt_num(a['max'], p.step)}")
                    lines.append(f"{k} | {r['w'] / 3600:.1f} | " + " | ".join(cells))
                return "\n".join(lines)
            # запасной путь — часовые агрегаты писателя, по отсчётам
            env.layer = "часовые агрегаты телеметрии (history_1hour) — ПО ОТСЧЁТАМ, не по времени"
            env.notes.append("сегментов аналитики за окно нет; среднее по отсчётам смещено при записи по изменению")
            lines = ["час | " + " | ".join(f"{p.label} (ср./мин/макс, n)" for p in prefs)]
            table: dict[datetime, list[str]] = {}
            for p in prefs:
                agg = await hourly_aggregates(*u.triple, p.addr, t0, t1)
                for a in agg:
                    table.setdefault(a["bucket"], ["—"] * len(prefs))[prefs.index(p)] = \
                        f"{fmt_num(a['avg_value'])}/{fmt_num(a['min_value'])}/{fmt_num(a['max_value'])}, {a['sample_count']}"
            keys = sorted(table)
            cap = int(st.limit("trend_max_points"))
            if len(keys) > cap:
                env.truncation.append(f"часов больше {cap} — показаны последние {cap}")
                keys = keys[-cap:]
            env.rows = len(keys)
            for k in keys:
                lines.append(fmt_t(k, day=True) + " | " + " | ".join(table[k]))
            if not keys:
                lines.append("(агрегатов за окно нет)")
            return "\n".join(lines)
        return await run_tool("trend", ctx, {"unit": unit, "params": params, "t_from": t_from, "t_to": t_to,
                                             "step": step}, body)


def _tz():
    from config import get_tz
    return get_tz()
