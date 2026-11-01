# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Инструменты 6–9: сегменты аналитики, отчёт сегмента, эпизоды тревог, акты
аварийных остановов (design_mcp_telemetry.md, разд. 5.2, форма 6.6)."""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Annotated, Any

from mcp.server.mcpserver import Context
from pydantic import Field

from analytics.labels import fmt_duration, fmt_local
from analytics.serializer import RUN_STATE_RU

from .common import ToolError, est_tokens, fmt_num, fmt_t, parse_window, resolve_unit, run_tool, settings
from .envelope import Envelope
from .queries import act_by_id, acts_in_window, segment_with_report, segments_in_window

_ARG_UNIT = Annotated[str, Field(description="Установка: имя или id")]


def _det_names(cj: dict[str, Any] | None) -> list[str]:
    names: list[str] = []
    for sub in (cj or {}).get("subsegments") or []:
        for d in sub.get("detections") or []:
            if isinstance(d, dict):
                n = d.get("scenario") or d.get("description_key")
                if n and n not in names:
                    names.append(str(n))
    return names


def _first_line(md: str | None) -> str | None:
    if not md:
        return None
    for ln in str(md).splitlines():
        s = ln.strip().lstrip("#*- ").strip()
        if s:
            return s[:200]
    return None


def register(server: Any) -> None:

    @server.tool(name="segments",
                 description="Сегменты аналитики установки за окно: номер, режим, границы, длительность, "
                             "причина закрытия, качество данных, подсегменты, замечания, есть ли акт и "
                             "разборы ИИ. Номер сегмента нужен для segment_report.")
    async def segments(
        ctx: Context,
        unit: _ARG_UNIT,
        t_from: Annotated[str | None, Field(description="Начало окна (МСК); пусто — 7 суток назад")] = None,
        t_to: Annotated[str | None, Field(description="Конец окна; пусто — сейчас")] = None,
    ) -> str:
        async def body(env: Envelope) -> str:
            u = await resolve_unit(unit)
            env.unit, env.unit_id = u.title, u.id
            t0, t1 = parse_window(t_from, t_to, None, None, default_hours=24 * 7, max_days=120)
            env.window_from, env.window_to = t0, t1
            env.layer = "БД аналитики (auto_segments)"
            rows = await segments_in_window(*u.triple, t0, t1)
            env.rows = len(rows)
            day = True
            lines: list[str] = []
            for s in rows:
                cj = s.get("characteristics_json") or {}
                rs = s.get("run_state")
                mode = RUN_STATE_RU.get(rs, str(rs))
                if cj.get("stop_kind") == "EMERGENCY":
                    mode += " (аварийный)"
                if isinstance(cj.get("attempt"), dict):
                    mode += " · неудачная попытка пуска"
                t_end = s.get("t_end")
                span = f"{fmt_t(s['t_start'], day=day)}–{fmt_t(t_end, day=day) if t_end else 'открыт'}"
                dur = fmt_duration(((t_end or datetime.now(s['t_start'].tzinfo)) - s["t_start"]).total_seconds())
                parts = [f"#{s['id']} · {mode} · {span} ({dur})"]
                if s.get("cause_close"):
                    parts.append(f"закрыт: {s['cause_close']}")
                dq = cj.get("data_quality")
                if dq is not None:
                    parts.append(f"качество данных {float(dq):.2f}")
                subs = cj.get("subsegments") or []
                if subs:
                    parts.append(f"подсегментов {len(subs)}")
                dets = _det_names(cj)
                if dets:
                    parts.append("замечания: " + ", ".join(dets[:6]) + (" …" if len(dets) > 6 else ""))
                summ = _first_line(s.get("report_summary_md"))
                if summ:
                    parts.append(f"сводка: {summ}")
                if s.get("has_act"):
                    parts.append("акт аварийного останова: да")
                if s.get("n_analyses"):
                    parts.append(f"разборов ИИ: {s['n_analyses']}")
                lines.append(" · ".join(parts))
            if not lines:
                return "Сегментов в окне нет (аналитика по установке не велась или окно пустое)."
            return "\n".join(lines)
        return await run_tool("segments", ctx, {"unit": unit, "t_from": t_from, "t_to": t_to}, body)

    @server.tool(name="segment_report",
                 description="Отчёт сегмента аналитики по разделам: шапка, сводка, подсегменты, замечания, "
                             "заключение ИИ; «всё» — целиком (большие сутки сжимаются). Нужен номер из segments.")
    async def segment_report(
        ctx: Context,
        segment_id: Annotated[int, Field(description="Номер сегмента (#id из segments)")],
        sections: Annotated[str | None, Field(description="Разделы через запятую: шапка, сводка, подсегменты, замечания, заключение, всё. По умолчанию шапка и сводка")] = None,
    ) -> str:
        async def body(env: Envelope) -> str:
            row = await segment_with_report(int(segment_id))
            if row is None:
                raise ToolError(f"Сегмент #{segment_id} не найден.")
            env.unit = f"сегмент #{segment_id}"
            env.window_from, env.window_to = row.get("t_start"), row.get("t_end")
            env.layer = "БД аналитики (report_md)"
            report = row.get("report_md") or ""
            if not report:
                return "У сегмента нет отчёта (открытый сегмент или отчёт ещё не построен)."
            blocks = _split_sections(report)
            names = [b[0] for b in blocks]
            env.notes.append("разделы отчёта: " + "; ".join(names[:12]))
            want = [w.strip().lower() for w in (sections or "шапка, сводка").replace(";", ",").split(",") if w.strip()]
            if any(w in ("всё", "все", "all") for w in want):
                text = report
                limit = int(settings().limit("result_max_tokens"))
                if est_tokens(text) > limit:
                    try:
                        from corpus.compact import LEVELS, compact_input
                        for level in LEVELS:
                            c = compact_input(report, row, level)
                            if c is None:
                                break
                            text = c
                            env.truncation.append(f"отчёт сжат по правилам заключения (ступень {level})")
                            if est_tokens(text) <= limit:
                                break
                    except Exception:  # noqa: BLE001
                        pass
                env.rows = len(text.splitlines())
                return text
            chosen: list[str] = []
            for w in want:
                hit = [b for b in blocks if _section_key(b[0]) == _section_key(w) or w in b[0].lower()]
                if not hit and w in ("шапка", "head"):
                    hit = blocks[:1]
                for b in hit:
                    if b[1] not in chosen:
                        chosen.append(b[1])
            if not chosen:
                raise ToolError("Таких разделов нет. Есть: " + "; ".join(names))
            text = "\n\n".join(chosen)
            env.rows = len(text.splitlines())
            return text
        return await run_tool("segment_report", ctx, {"segment_id": segment_id, "sections": sections}, body)

    @server.tool(name="episodes",
                 description="Эпизоды тревог аналитики за окно: сценарий, серьёзность, начало и конец, "
                             "воздействие (active_sec) и слепая доля без связи (blind_sec), источник "
                             "(панель/аналитика), снятие гейтом, значения на открытии.")
    async def episodes(
        ctx: Context,
        unit: _ARG_UNIT,
        t_from: Annotated[str | None, Field(description="Начало окна (МСК); пусто — 7 суток назад")] = None,
        t_to: Annotated[str | None, Field(description="Конец окна; пусто — сейчас")] = None,
        scenario: Annotated[str | None, Field(description="Сценарий (подстрока), например RPM_UNDERSPEED, NEUTRAL_CURRENT")] = None,
    ) -> str:
        async def body(env: Envelope) -> str:
            u = await resolve_unit(unit)
            env.unit, env.unit_id = u.title, u.id
            t0, t1 = parse_window(t_from, t_to, None, None, default_hours=24 * 7, max_days=120)
            env.window_from, env.window_to = t0, t1
            env.layer = "БД аналитики (alarm_episodes)"
            from online.db import get_episodes_overlapping
            eps = await get_episodes_overlapping(*u.triple, t0, t1)
            if scenario:
                q = scenario.strip().upper()
                eps = [e for e in eps if q in str(e.get("scenario") or "").upper()]
            env.rows = len(eps)
            lines: list[str] = []
            for e in eps[:300]:
                t_close = e.get("t_close")
                parts = [f"#{e['id']} · {e.get('scenario')} [{e.get('severity') or '—'}] · "
                         f"{fmt_t(e['t_open'], day=True)}–{fmt_t(t_close, day=True) if t_close else 'открыт'}"]
                parts.append(f"воздействие {fmt_duration(float(e.get('active_sec') or 0))}"
                             + (f", без связи {fmt_duration(float(e['blind_sec']))}" if e.get("blind_sec") else ""))
                parts.append("источник: " + ("панель" if e.get("source") == "panel" else "аналитика"))
                if e.get("close_reason"):
                    parts.append(f"закрыт: {e['close_reason']}")
                if e.get("gate_suppressed"):
                    parts.append("снят гейтом ИИ")
                ov = e.get("open_values_json")
                if isinstance(ov, str):
                    import json as _json
                    try:
                        ov = _json.loads(ov)
                    except ValueError:
                        ov = None
                if isinstance(ov, dict):
                    kv = [f"{k}={fmt_num(v) if isinstance(v, (int, float)) else v}" for k, v in list(ov.items())[:6]
                          if not isinstance(v, (dict, list))]
                    if kv:
                        parts.append("значения: " + ", ".join(kv))
                lines.append(" · ".join(parts))
            if len(eps) > 300:
                env.truncation.append(f"показано 300 из {len(eps)} эпизодов — сузьте окно или сценарий")
            if not lines:
                return "Эпизодов в окне нет."
            return "\n".join(lines)
        return await run_tool("episodes", ctx, {"unit": unit, "t_from": t_from, "t_to": t_to, "scenario": scenario},
                              body)

    @server.tool(name="incident_acts",
                 description="Акты аварийных остановов установки за окно или акт по номеру сегмента: момент "
                             "и вид стопа, характер (мгновенный/управляемый), отказ пуска или работы, окно "
                             "акта, стоящие неисправности, сводка «Следователя», лента событий, заключение "
                             "совета ИИ, если было.")
    async def incident_acts(
        ctx: Context,
        unit: Annotated[str | None, Field(description="Установка: имя или id (не нужна при act_id)")] = None,
        t_from: Annotated[str | None, Field(description="Начало окна (МСК); пусто — 30 суток назад")] = None,
        t_to: Annotated[str | None, Field(description="Конец окна; пусто — сейчас")] = None,
        act_id: Annotated[int | None, Field(description="Номер сегмента стопа с актом — подробный акт")] = None,
        events_limit: Annotated[int, Field(description="Сколько событий ленты акта показать (по умолчанию 25)")] = 25,
    ) -> str:
        async def body(env: Envelope) -> str:
            u = await resolve_unit(unit) if unit else None
            if u is not None:
                env.unit, env.unit_id = u.title, u.id
            env.layer = "БД аналитики (incident_json, warning_analyses)"
            if act_id is not None:
                row = await act_by_id(int(act_id))
                if row is None:
                    raise ToolError(f"Акта у сегмента #{act_id} нет.")
                env.unit = u.title if u else f"сегмент #{act_id}"
                env.rows = 1
                return _act_text(row, int(events_limit), full=True)
            if u is None:
                raise ToolError("Укажите установку или act_id.")
            t0, t1 = parse_window(t_from, t_to, None, None, default_hours=24 * 30, max_days=400)
            env.window_from, env.window_to = t0, t1
            rows = await acts_in_window(*u.triple, t0, t1)
            env.rows = len(rows)
            if not rows:
                return "Актов аварийных остановов в окне нет."
            return "\n\n".join(_act_text(r, min(int(events_limit), 8), full=False) for r in rows)
        return await run_tool("incident_acts", ctx, {"unit": unit, "t_from": t_from, "t_to": t_to, "act_id": act_id,
                                                     "events_limit": events_limit}, body)


def _act_text(row: dict[str, Any], events_limit: int, *, full: bool) -> str:
    inc = row.get("incident_json") or {}
    lines: list[str] = []
    stop = _dt(inc.get("stop_ts")) or row.get("t_start")
    ch = inc.get("character")
    lines.append(f"Акт #{row['id']} · стоп {fmt_t(stop, day=True)} · вид {inc.get('stop_kind') or '—'}"
                 + (f" · характер: {ch}" if ch else "")
                 + (f" · отказ: {'пуска' if inc.get('failure') == 'start' else 'работы'}" if inc.get("failure") else ""))
    if isinstance(ch, dict):
        lines[-1] = lines[-1].replace(f"характер: {ch}", "характер: " + _character_text(ch))
    win = inc.get("window") or {}
    if win:
        lines.append(f"окно акта: {fmt_t(_dt(win.get('from')), day=True)}–{fmt_t(_dt(win.get('to')), day=True)}"
                     + (f", якорь {fmt_t(_dt(win.get('anchor')), day=True)}" if win.get("anchor") else "")
                     + (f" ({win.get('baseline')})" if win.get("baseline") else ""))
    fall = inc.get("fall")
    if isinstance(fall, dict):
        lines.append(f"сегмент падения: {fall.get('label') or fall.get('run_state')} "
                     f"{fmt_t(_dt(fall.get('t_start')), day=True)}–{fmt_t(_dt(fall.get('t_end')), day=True)}"
                     + (" (попытка пуска)" if fall.get("attempt") else ""))
    standing = inc.get("standing")
    if isinstance(standing, list) and standing:
        lines.append("стоящие неисправности на момент стопа: " + "; ".join(_standing_text(s) for s in standing[:8]))
    summ = inc.get("summary")
    if summ:
        lines.append("сводка «Следователя» (что повторялось в окне): " + _summary_text(summ, 12 if full else 5))
    att = inc.get("attempt")
    if isinstance(att, dict):
        lines.append("попытка пуска: " + _plain(att, 300))
    ev = inc.get("chronology") or []
    if ev:
        lines.append(f"лента акта: событий {len(ev)}" + (f", первые {events_limit}:" if len(ev) > events_limit else ":"))
        from .kb import role_label_ru
        for e in ev[:events_limit]:
            ts = _dt(e.get("ts"))
            what = role_label_ru(e.get("role")) or e.get("name") or ""
            lab = e.get("label") if e.get("label") not in (None, "") else e.get("value")
            sev = f" [{e['severity']}]" if e.get("severity") else ""
            lines.append(f"  {fmt_t(ts, day=True)} · {'неисправность' if e.get('kind') == 'fault' else 'состояние'} · "
                         f"{what}" + (f" → {lab}" if e.get("kind") != "fault" and lab not in (None, "") else "") + sev)
    conclusions = [w for w in (row.get("warning_analyses") or []) if isinstance(w, dict) and
                   (w.get("kind") == "incident" or "incident" in str(w.get("fault_hash") or ""))]
    if conclusions:
        last = conclusions[-1]
        md = str(last.get("md") or "")
        cap = 2500 if full else 500
        lines.append(f"заключение совета ИИ ({fmt_t(_dt(last.get('t')), day=True)}): "
                     + (md[:cap] + (" …" if len(md) > cap else "")))
    elif full:
        lines.append("заключения совета ИИ по акту нет")
    return "\n".join(lines)


def _character_text(ch: dict[str, Any]) -> str:
    """{"character": "immediate", "confidence": "high", "signals": {…}} → текст."""
    kind = {"immediate": "мгновенный (аварийный)", "controlled": "управляемый"}.get(str(ch.get("character")), str(ch.get("character")))
    conf = {"high": "высокая", "medium": "средняя", "low": "низкая"}.get(str(ch.get("confidence")), None)
    sig = ch.get("signals") or {}
    bits = []
    rs = sig.get("run_state_before")
    if rs is not None:
        bits.append(f"до стопа — {RUN_STATE_RU.get(rs, rs)}")
    if sig.get("run_command_path"):
        bits.append(f"путь команды {sig['run_command_path']}")
    if sig.get("fault_type_40013") is not None:
        bits.append(f"тип 40013 = {sig['fault_type_40013']}")
    if sig.get("passed_cooldown") is not None:
        bits.append("с охлаждением" if sig["passed_cooldown"] else "без охлаждения")
    out = kind + (f", уверенность {conf}" if conf else "")
    return out + (" [" + "; ".join(bits) + "]" if bits else "")


def _standing_text(s: Any) -> str:
    if not isinstance(s, dict):
        return str(s)
    name = s.get("name") or s.get("fault_name_ru") or s.get("fault_name") or f"{s.get('addr')}/{s.get('bit')}"
    age = s.get("age_sec")
    since = _dt(s.get("since"))
    extra = []
    if since:
        extra.append(f"с {fmt_t(since, day=True)}")
    if age:
        extra.append(fmt_duration(float(age)))
    return name + (" (" + ", ".join(extra) + ")" if extra else "")


def _summary_text(summ: Any, limit: int) -> str:
    """Список повторов {name, count, first, last, kind} → «имя ×N (с…по…)»."""
    if isinstance(summ, str):
        return _plain(summ, 1200)
    if isinstance(summ, dict):
        summ = [summ]
    if not isinstance(summ, list):
        return _plain(summ, 600)
    items = []
    for it in summ[:limit]:
        if not isinstance(it, dict):
            items.append(str(it))
            continue
        name = it.get("name") or f"{it.get('addr')}/{it.get('bit')}"
        n = it.get("count")
        a, b = _dt(it.get("first")), _dt(it.get("last"))
        span = ""
        if a and b and a != b:
            span = f" ({fmt_t(a)}–{fmt_t(b)})"
        elif a:
            span = f" ({fmt_t(a)})"
        items.append(f"{name}" + (f" ×{n}" if n else "") + span)
    more = f"; ещё {len(summ) - limit}" if len(summ) > limit else ""
    return "; ".join(items) + more


def _dt(v: Any) -> datetime | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v))
    except ValueError:
        return None


def _plain(v: Any, cap: int) -> str:
    if isinstance(v, str):
        s = v
    elif isinstance(v, dict):
        s = "; ".join(f"{k}: {_plain(x, cap)}" for k, x in v.items() if x not in (None, "", [], {}))
    elif isinstance(v, list):
        s = "; ".join(_plain(x, cap) for x in v)
    else:
        s = str(v)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:cap] + (" …" if len(s) > cap else "")


_SECTION_ALIASES = {
    "шапка": "head", "head": "head", "сводка": "summary", "summary": "summary",
    "подсегмент": "subs", "подсегменты": "subs", "замечания": "notes", "замечание": "notes",
    "заключение": "conclusion", "заключение ии": "conclusion", "ход цикла": "cycle",
}


def _section_key(name: str) -> str:
    n = name.strip().lower().lstrip("#").strip()
    for k, v in _SECTION_ALIASES.items():
        if n.startswith(k) or k in n:
            return v
    return n


def _split_sections(md: str) -> list[tuple[str, str]]:
    """[(заголовок, текст раздела)] по строкам «## …»/«# …»; текст до первого
    заголовка — «шапка»."""
    out: list[tuple[str, str]] = []
    cur_name = "шапка"
    cur: list[str] = []
    for ln in md.splitlines():
        m = re.match(r"^\s*#{1,3}\s+(.+?)\s*$", ln)
        if m:
            if cur or out == []:
                out.append((cur_name, "\n".join(cur).strip()))
            cur_name, cur = m.group(1).strip(), [ln]
        else:
            cur.append(ln)
    out.append((cur_name, "\n".join(cur).strip()))
    return [(n, t) for n, t in out if t]
