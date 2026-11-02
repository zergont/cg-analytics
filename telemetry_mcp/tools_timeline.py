# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Инструмент 5: лента событий (design_mcp_telemetry.md, разд. 5.2, форма 6.4).

Один отсортированный список: смены режима, состояния, действия персонала,
фронты неисправностей, связь (разрывы и события писателя), по флагу — разметка
аналитики. Дребезг одного регистра схлопывается в строку с числом повторов.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Any

from mcp.server.mcpserver import Context
from pydantic import Field

from analytics import source as _src
from analytics.labels import fmt_duration, fmt_local
from analytics.reconstructor import build_chronology
from analytics.serializer import RUN_STATE_RU

from .common import UnitRef, fmt_t, neighbors_of, parse_window, resolve_unit, run_tool, settings
from .envelope import Envelope
from .history import gap_pairs
from .kb import kb_for

# Регистры действий персонала (PCC): переключатель, команда пуска/останова, сброс
# неисправностей, выключатель генератора — и роли с теми же смыслами.
PERSONNEL_ADDRS = {40010, 40580, 40599, 40569, 43562, 43560, 40710}
PERSONNEL_ROLE_TOKENS = ("SWITCH", "COMMAND", "RESET", "CB_", "_CB", "BREAKER")
RUN_STATE_ADDRS = {40011, 43500}

KIND_RU = {"mode": "режим", "state": "состояние", "personnel": "действие персонала",
           "fault": "неисправность", "link": "связь", "analytics": "аналитика"}


def classify(ev: dict[str, Any], cfg: Any) -> str:
    if ev.get("kind") == "fault":
        return "fault"
    addr, role = ev.get("addr"), (ev.get("role") or "")
    if addr in RUN_STATE_ADDRS or role in ("RUN_STATE", "GENSET_RUN_STATE"):
        return "mode"
    if addr in PERSONNEL_ADDRS or any(t in role for t in PERSONNEL_ROLE_TOKENS):
        return "personnel"
    return "state"


def _reset_release(e: dict[str, Any]) -> bool:
    """Возврат маркера сброса неисправностей в «не активно» — не действие, шум."""
    if (e.get("role") or "") != "FAULT_RESET":
        return False
    try:
        return int(e.get("value")) == 0
    except (TypeError, ValueError):
        return str(e.get("label") or "").lower().startswith("не актив")


def debounce(events: list[dict[str, Any]], window_sec: float, min_count: int) -> list[dict[str, Any]]:
    """Серии переключений одного регистра чаще window_sec и не короче min_count
    — одна строка «мигал N раз за M» с первым/последним временем и последней меткой."""
    by_addr: dict[Any, list[dict[str, Any]]] = {}
    for e in events:
        if e["kind"] in ("state", "mode", "personnel"):
            by_addr.setdefault(e.get("addr"), []).append(e)
        else:
            by_addr.setdefault(("fault", e.get("addr"), e.get("bit")), []).append(e)
    out: list[dict[str, Any]] = []
    for lst in by_addr.values():
        lst.sort(key=lambda x: x["ts"])
        i = 0
        while i < len(lst):
            j = i
            while j + 1 < len(lst) and (lst[j + 1]["ts"] - lst[j]["ts"]).total_seconds() <= window_sec:
                j += 1
            n = j - i + 1
            if n >= min_count:
                first, last = lst[i], lst[j]
                merged = dict(last)
                merged["ts"] = first["ts"]
                merged["debounced"] = {"n": n, "from": first["ts"], "to": last["ts"]}
                out.append(merged)
            else:
                out.extend(lst[i:j + 1])
            i = j + 1
    out.sort(key=lambda x: (x["ts"], 0 if x["kind"] == "mode" else 1))
    return out


def _fmt_event(e: dict[str, Any], day: bool, prefix: str = "", t0: datetime | None = None) -> str:
    kind = e["kind"]
    # Событие, начавшееся до окна (эпизод, период), печатается с датой — иначе оно
    # встаёт «впереди 09:00» без объяснения (приёмка Э1)
    if t0 is not None and e.get("ts") is not None and e["ts"] < t0:
        day = True
    t = fmt_t(e["ts"], day=day)
    dur = ""
    if e.get("end") and e.get("ts"):
        dur = f" · {fmt_duration((e['end'] - e['ts']).total_seconds())}"
    elif kind in ("state", "mode", "personnel", "fault") and e.get("end") is None and not e.get("debounced"):
        dur = " · продолжается"
    db = e.get("debounced")
    if db:
        span = fmt_duration((db["to"] - db["from"]).total_seconds())
        what = f"{e.get('name')}: мигал {db['n']} раз за {span} ({fmt_t(db['from'])}–{fmt_t(db['to'])}), " \
               f"последнее — {e.get('label') or e.get('value')}"
        return f"{t} · {prefix}{KIND_RU[kind]} · {what}"
    if kind == "fault":
        sev = f" [{e['severity']}]" if e.get("severity") else ""
        return f"{t} · {prefix}неисправность · {e.get('name')}{sev}{dur}"
    if kind == "link":
        return f"{t} · {prefix}связь · {e.get('name')}{dur}"
    if kind == "analytics":
        return f"{t} · {prefix}аналитика · {e.get('name')}{dur}"
    lab = e.get("label") if e.get("label") not in (None, "") else e.get("value")
    from .kb import role_label_ru
    name = role_label_ru(e.get("role")) or e.get("name")
    return f"{t} · {prefix}{KIND_RU[kind]} · {name} → {lab}{dur}"


async def _unit_events(u: UnitRef, t0: datetime, t1: datetime, kinds: set[str], env: Envelope | None,
                       st: Any) -> list[dict[str, Any]]:
    cfg, _ref, note = await kb_for(u)
    if env is not None and note:
        env.notes.append(note)
    enum_periods, fault_periods = await _src.get_timeline_periods(*u.triple, t0, t1, cfg)
    events = build_chronology(enum_periods, fault_periods, cfg, t0, t1)
    for e in events:
        e["kind"] = classify(e, cfg)
        if e["kind"] == "mode" and e.get("value") is not None:
            try:
                e["label"] = RUN_STATE_RU.get(int(e["value"]), e.get("label"))
            except (TypeError, ValueError):
                pass
    events = [e for e in events if e["kind"] in kinds and not _reset_release(e)]
    if "link" in kinds:
        gaps = gap_pairs(await _src.get_data_gaps(*u.triple, t0, t1))
        if env is not None:
            env.gaps = gaps
        for a, b in gaps:
            events.append({"ts": a, "end": b, "kind": "link", "name": "обрыв связи"
                           + ("" if b else " (продолжается)")})
        try:
            from db.source import get_events_range
            for ev in await get_events_range(*u.triple, t0, t1):
                et = str(ev.get("event_type") or "")
                if "online" in et or "offline" in et or "stale" in et:
                    events.append({"ts": ev["ts"], "end": None, "kind": "link",
                                   "name": f"{et}" + (f": {ev['description']}" if ev.get("description") else "")})
        except Exception:  # noqa: BLE001
            if env is not None:
                env.notes.append("события связи писателя (events) не прочитаны")
    events = debounce(events, float(st.limit("debounce_sec")), int(st.limit("debounce_min_count")))
    return events


def register(server: Any) -> None:

    @server.tool(name="timeline",
                 description="Лента событий установки за окно одним списком: смены режима, состояния, "
                             "действия персонала (переключатель, команды, сброс, автомат), фронты "
                             "неисправностей с серьёзностью, связь (разрывы). Дребезг схлопнут с числом "
                             "повторов. with_analytics=true добавляет границы сегментов и эпизоды.")
    async def timeline(
        ctx: Context,
        unit: Annotated[str, Field(description="Установка: имя или id")],
        t_from: Annotated[str | None, Field(description="Начало окна (МСК)")] = None,
        t_to: Annotated[str | None, Field(description="Конец окна; пусто — сейчас")] = None,
        at: Annotated[str | None, Field(description="Вместо от/до: момент ± minutes")] = None,
        minutes: Annotated[int | None, Field(description="Полуширина окна вокруг at, минут (по умолчанию 30)")] = None,
        kinds: Annotated[str | None, Field(description="Виды через запятую: режим, состояние, действия, неисправности, связь; пусто — все")] = None,
        with_analytics: Annotated[bool, Field(description="Добавить разметку аналитики: сегменты и эпизоды")] = False,
        with_neighbors: Annotated[bool, Field(description="Добавить режимы и неисправности соседних установок")] = False,
    ) -> str:
        st = settings()

        async def body(env: Envelope) -> str:
            u = await resolve_unit(unit)
            env.unit, env.unit_id = u.title, u.id
            t0, t1 = parse_window(t_from, t_to, at, minutes, default_hours=2, max_days=31)
            env.window_from, env.window_to = t0, t1
            env.layer = "периоды состояний и неисправностей (защита меток), разрывы связи"
            want = _kinds(kinds)
            events = await _unit_events(u, t0, t1, want, env, st)
            lines_ev: list[tuple[datetime, str]] = []
            day = (fmt_local(t0, day=True) or "")[:5] != (fmt_local(t1 - timedelta(seconds=1), day=True) or "")[:5]
            for e in events:
                lines_ev.append((e["ts"], _fmt_event(e, day, t0=t0)))
            from .history import retention_cutoff
            cut = await retention_cutoff()
            if cut is not None and t0 < cut:
                env.notes.append(f"аналоговой истории раньше {cut:%d.%m.%Y} в телеметрии уже нет (срок хранения): "
                                 "ряды и снимки за это время недоступны, лента состояний хранится дольше")
            if with_analytics:
                from .queries import segments_in_window
                from online.db import get_episodes_overlapping
                for s in await segments_in_window(*u.triple, t0, t1):
                    name = f"сегмент #{s['id']} «{RUN_STATE_RU.get(s.get('run_state'), s.get('run_state'))}»"
                    if s.get("t_end"):
                        name += f" до {fmt_t(s['t_end'], day=day)}"
                    else:
                        name += " (открыт)"
                    if s.get("cause_close"):
                        name += f", закрыт: {s['cause_close']}"
                    lines_ev.append((s["t_start"], _fmt_event({"ts": s["t_start"], "end": None, "kind": "analytics",
                                                                "name": name}, day, t0=t0)))
                for ep in await get_episodes_overlapping(*u.triple, t0, t1):
                    name = f"эпизод #{ep['id']} {ep.get('scenario')} [{ep.get('severity') or '—'}]"
                    if ep.get("gate_suppressed"):
                        name += " · снят гейтом"
                    lines_ev.append((ep["t_open"], _fmt_event({"ts": ep["t_open"], "end": ep.get("t_close"),
                                                                "kind": "analytics", "name": name}, day, t0=t0)))
            if with_neighbors:
                for nb in await neighbors_of(u):
                    nev = await _unit_events(nb, t0, t1, {"mode", "fault", "personnel"}, None, st)
                    for e in nev:
                        lines_ev.append((e["ts"], _fmt_event(e, day, prefix=f"[{nb.name}] ", t0=t0)))
            lines_ev.sort(key=lambda x: x[0])
            cap = int(st.limit("timeline_max_rows"))
            env.rows = len(lines_ev)
            if len(lines_ev) > cap:
                env.truncation.append(f"показано {cap} из {len(lines_ev)} событий — сузьте окно или виды")
                lines_ev = lines_ev[:cap]
            if not lines_ev:
                return "Событий в окне нет."
            return "\n".join(s for _, s in lines_ev)
        return await run_tool("timeline", ctx, {"unit": unit, "t_from": t_from, "t_to": t_to, "at": at,
                                                "minutes": minutes, "kinds": kinds, "with_analytics": with_analytics,
                                                "with_neighbors": with_neighbors}, body)


def _kinds(s: str | None) -> set[str]:
    if not s:
        return {"mode", "state", "personnel", "fault", "link"}
    out: set[str] = set()
    for w in str(s).replace(";", ",").split(","):
        w = w.strip().lower()
        if not w:
            continue
        if w.startswith("реж"):
            out.add("mode")
        elif w.startswith("сост"):
            out.add("state")
        elif w.startswith("дейст") or w.startswith("перс"):
            out.add("personnel")
        elif w.startswith("неиспр") or w.startswith("авар") or w.startswith("fault"):
            out.add("fault")
        elif w.startswith("связ") or w.startswith("разр"):
            out.add("link")
    return out or {"mode", "state", "personnel", "fault", "link"}
