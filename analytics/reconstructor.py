# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Реконструктор действий («Следователь», Фаза 2).

Сливает три потока телеметрии в единую упорядоченную по времени ленту событий,
из которой классификатор выводит причину останова, а рендер — черновик акта:

  1. enum-журнал      — смены состояний командных/исполнительных регистров
                        (ключ 40010, RUN_STATE 40011, RunCommand 40599, топл.
                        соленоид, возбуждение, GensetCB, Сброс, коды 40012/40013…)
  2. fault-фронты     — фронты битов масок неисправностей (severity из KB)
  3. аналог + пороги  — тренды параметров с аннотацией пересечения порогов РЭ
                        (добавляется отдельным шагом; см. annotate_analog_crossings)

Детерминированно: лента воспроизводима из history — перечитка даёт то же самое.

v5.1.22 (P1-4, решение владельца В2): третий поток — лента аналоговых отсчётов
сегмента падения (build_analog_trace) снимается из уже загруженной истории окна
при закрытии этого сегмента, без повторного чтения; печать для совета и факты
для заключения стопа — format_analog_trace_ru / fall_facts_ru (время местное).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _tz(ts: datetime | None) -> datetime | None:
    if ts is None:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _state_event(p: dict[str, Any], reg: dict[int, dict]) -> dict[str, Any]:
    """enum-период → событие ленты (смена состояния регистра)."""
    addr = p["addr"]
    meta = reg.get(addr, {})
    # Имя — из KB, если регистр там описан; иначе из каталога БД телеметрии:
    # лента несёт всё, что прислала панель, а не только объявленное в KB.
    # Метка значения — labels_ru KB поверх каталога (v5.1.23, решение В5:
    # 40599 = 0 — «нет команды (0)», а не «EmergencyStop» каталога)
    from .labels import enum_label
    return {
        "ts":       _tz(p["state_start"]),
        "end":      _tz(p.get("state_end")),
        "kind":     "state",
        "addr":     addr,
        "bit":      None,
        "role":     meta.get("role"),
        "name":     (meta.get("description") or p.get("name_ru")
                     or meta.get("role") or f"reg_{addr}"),
        "value":    p.get("value"),
        "label":    enum_label(meta, p.get("value"), p.get("label"), fallback_value=False),
        "severity": None,
    }


def _fault_event(f: dict[str, Any]) -> dict[str, Any]:
    """fault-период → событие ленты (фронт неисправности)."""
    addr, bit = f["addr"], f.get("bit")
    return {
        "ts":       _tz(f["fault_start"]),
        "end":      _tz(f.get("fault_end")),
        "kind":     "fault",
        "addr":     addr,
        "bit":      bit,
        "role":     None,
        "name":     f.get("fault_name_ru") or f.get("fault_name") or f"fault_{addr}/{bit}",
        "value":    None,
        "label":    None,
        "severity": f.get("severity"),
    }


def build_chronology(
    enum_periods: list[dict[str, Any]],
    fault_periods: list[dict[str, Any]],
    cfg: Any = None,
    window_from: datetime | None = None,
    window_to: datetime | None = None,
) -> list[dict[str, Any]]:
    """Слить enum-журнал и fault-фронты в одну ленту, отсортированную по началу.

    Каждое событие: {ts, end, kind (state|fault), addr, bit, role, name, value,
    label, severity}. cfg (AnalyticsConfig) нужен только для имён ролей регистров;
    без него имена берутся из label/адреса. Порядок стабилен: при равном ts
    сначала смены состояний (state), затем фронты (fault) — состояние-причина
    предшествует своему следствию-фолту при одном замере.

    window_from/window_to — фильтр по НАЧАЛУ события (onset-in-window): в ленту
    попадают только переходы, СЛУЧИВШИЕСЯ в окне, а не фоновые состояния, которые
    активны с прошлого. Без окна — все переданные периоды.
    """
    reg = getattr(cfg, "register_map", {}) if cfg is not None else {}
    events: list[dict[str, Any]] = []
    events.extend(_state_event(p, reg) for p in enum_periods)
    events.extend(_fault_event(f) for f in fault_periods)

    wf = _tz(window_from)
    wt = _tz(window_to)
    if wf is not None or wt is not None:
        events = [
            e for e in events
            if e["ts"] is not None
            and (wf is None or e["ts"] >= wf)
            and (wt is None or e["ts"] < wt)
        ]

    _kind_rank = {"state": 0, "fault": 1}
    _far = datetime.max.replace(tzinfo=timezone.utc)
    events.sort(key=lambda e: (e["ts"] or _far, _kind_rank.get(e["kind"], 9)))
    return events


_SEGMENT_CHRONOLOGY_VERSION = "1.0"


def build_segment_chronology(
    enum_periods: list[dict[str, Any]],
    fault_periods: list[dict[str, Any]],
    t_from: datetime,
    t_to: datetime,
    cfg: Any = None,
) -> dict[str, Any] | None:
    """Лента событий панели за окно сегмента; None — если ничего не случилось.

    Для стоп-сегмента это ответ на вопрос «кто что нажимал и сбрасывал, пока
    машина стояла»: в режиме 0 панель копит сообщения, не меняя режим, и без
    ленты стоянка выглядит однородной.

    Окно строгое, без преамбулы: событие принадлежит сегменту, во время
    которого произошло. Поэтому маска, висевшая ещё до начала стоянки, в
    ленту не попадёт — она видна в тяжести сегмента и в его детекциях, а
    здесь нас интересует именно то, что происходило внутри.
    """
    chrono = build_chronology(
        enum_periods, fault_periods, cfg, window_from=t_from, window_to=t_to
    )
    if not chrono:
        return None
    return {
        "kind": "segment_chronology",
        "t_from": _tz(t_from).isoformat() if t_from else None,
        "t_to": _tz(t_to).isoformat() if t_to else None,
        "chronology": serialize_chronology(chrono),
        "version": _SEGMENT_CHRONOLOGY_VERSION,
    }


def serialize_chronology(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Лента → JSON-совместимый вид (datetime → ISO) для хранения в incident_json."""
    out = []
    for e in events:
        r = dict(e)
        r["ts"] = e["ts"].isoformat() if e.get("ts") else None
        r["end"] = e["end"].isoformat() if e.get("end") else None
        out.append(r)
    return out


# ── Лента аналоговых отсчётов акта (v5.1.22, P1-4, решение В2) ───────────────
#
# Совет по #35941 писал «параметры этого сегмента в акте отсутствуют» и «898 в
# начале разгона» (на деле 1294 было в 09:28:42, а 898 — в 09:28:52): в акте были
# только состояния и фронты, аналог — склейкой мин/макс по режимам за весь цикл.
# Лента — отсчёты сегмента падения по секундам опроса, как их записала панель:
# ряд пишется по изменению (и «пульсом»), значение держится до следующей записи,
# пики между опросами не видны. Снимается из истории окна, уже загруженной для
# нарезки, — отдельного чтения аналога нет (решение 08.10, v4.9.96; В2 16.10).

TRACE_VERSION = 1
# Предохранитель хранения: окно ленты ≤ before + after (~63 опроса), но опрос
# бывает чаще — ранние строки сверх предела уходят во «вход»
_TRACE_STORE_MAX = 200
# Короткие подписи колонок и имена в фактах; неизвестная роль — кодом.
# Задание частоты регулятора (40595, v5.1.23) — колонка «задание» за частотой
_TRACE_HEAD = {"RPM": "об/мин", "FREQUENCY": "Гц", "FREQ_REFERENCE": "задание",
               "ACTIVE_POWER_TOTAL": "P,кВт", "CURRENT_AVG": "I,А", "VOLTAGE_LL_AVG": "U,В",
               "OIL_PRESS": "Pм,кПа", "BATTERY_V": "АКБ,В", "COOLANT_TEMP": "ОЖ,°C", "OIL_TEMP": "Tм,°C"}
_TRACE_NAME = {"RPM": "обороты", "FREQUENCY": "частота", "FREQ_REFERENCE": "задание",
               "ACTIVE_POWER_TOTAL": "P", "CURRENT_AVG": "I", "VOLTAGE_LL_AVG": "U",
               "OIL_PRESS": "давление масла", "BATTERY_V": "АКБ", "COOLANT_TEMP": "ОЖ",
               "OIL_TEMP": "температура масла"}
# Положение автомата — в фактах заключения (остальные состояния — только в печати)
_CB_ROLES = ("GENSET_CB_POS",)
# Короткие имена состояний в печати ленты; прочие — описание из KB
_STATE_NAME = {"GENSET_CB_POS": "Автомат"}


def trace_params(cfg: Any) -> dict[str, int]:
    """Лимиты ленты из KB (stop_kind.act_trace_*); в коде те же значения по
    умолчанию — рабочая копия KB на сервере перекрывает эталон целиком."""
    def _int(key: str, default: int) -> int:
        try:
            v = cfg.seg("stop_kind", key, default=default)
            return int(default if v is None else v)
        except Exception:
            return default
    return {"before": _int("act_trace_before_sec", 600),
            "after": _int("act_trace_after_sec", 30),
            "max_rows": _int("act_trace_max_rows", 40)}


def _num(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _dt(x: Any) -> datetime | None:
    if isinstance(x, str):
        try:
            x = datetime.fromisoformat(x)
        except ValueError:
            return None
    return _tz(x) if isinstance(x, datetime) else None


def build_analog_trace(
    by_addr: dict[int, list[dict[str, Any]]],
    enum_periods: list[dict[str, Any]],
    cfg: Any,
    t_from: datetime,
    stop_ts: datetime,
    t_to: datetime,
    segment: dict[str, Any] | None = None,
    held: dict[str, tuple[datetime, float]] | None = None,
) -> dict[str, Any] | None:
    """Лента отсчётов [t_from, t_to] вокруг останова stop_ts; None — колонок или
    данных нет (акт строится и без неё).

    by_addr — история окна по адресу в единицах карты (строки
    is_carried_forward пропускаются); колонки — cfg.incident_trace_registers:
    аналог — отсчёты, сгруппированные по секунде опроса (в строке только новые
    записи), enum (положение автомата) — периоды, пересекающие ленту, с
    настоящим началом. Значение на входе — последняя запись до t_from в окне,
    иначе удержанное перед окном (held — посев движка). segment — {run_state,
    label, t_start, t_end} сегмента падения для подписи. JSON-совместимый dict.
    """
    regs = list(getattr(cfg, "incident_trace_registers", None) or [])
    if not regs:
        return None
    t0, stop, t1 = _dt(t_from), _dt(stop_ts), _dt(t_to)
    if t0 is None or stop is None or t1 is None or t1 < t0:
        return None
    cols: list[dict[str, Any]] = []
    entry: dict[str, dict[str, Any]] = {}
    groups: dict[datetime, dict[str, float]] = {}
    for reg in regs:
        if reg.get("kind") == "enum":
            continue
        role = reg["role"]
        cols.append({"role": role, "addr": reg["addr"], "unit": reg.get("unit") or ""})
        last: tuple[float, datetime] | None = None
        for r in by_addr.get(reg["addr"]) or []:
            if r.get("is_carried_forward"):
                continue
            v, ts = _num(r.get("value")), _dt(r.get("ts"))
            if v is None or ts is None:
                continue
            if ts < t0:
                last = (v, ts)
                continue
            if ts > t1:
                break
            # Округление — только шум float (56.800000000000004), не точность
            groups.setdefault(ts.replace(microsecond=0), {})[role] = round(v, 3)
        if last is None and held and role in held:
            h_ts = _dt(held[role][0])
            if h_ts is not None and h_ts < t0 and _num(held[role][1]) is not None:
                last = (float(held[role][1]), h_ts)
        if last is not None:
            entry[role] = {"v": round(last[0], 3), "t": last[1].isoformat()}

    states: list[dict[str, Any]] = []
    for reg in regs:
        if reg.get("kind") != "enum":
            continue
        pers = []
        for p in enum_periods or []:
            if p.get("addr") != reg["addr"] or p.get("state_start") is None:
                continue
            s, e = _dt(p["state_start"]), _dt(p.get("state_end"))
            if s is None or s > t1 or (e is not None and e <= t0):
                continue
            lab = p.get("label")
            pers.append({"t": s.isoformat(), "v": p.get("value"),
                         "label": lab if lab not in (None, "") else p.get("value")})
        pers.sort(key=lambda x: x["t"])
        if pers:
            states.append({"role": reg["role"], "addr": reg["addr"],
                           "name": reg.get("description") or reg["role"], "periods": pers})

    if not groups and not entry and not states:
        return None
    rows = [{"t": ts.isoformat(), "v": groups[ts]} for ts in sorted(groups)]
    if len(rows) > _TRACE_STORE_MAX:
        # Ранние сверх предела — во «вход»: печать всё равно свернула бы их
        for row in rows[:len(rows) - _TRACE_STORE_MAX]:
            for role, v in row["v"].items():
                entry[role] = {"v": v, "t": row["t"]}
        rows = rows[len(rows) - _TRACE_STORE_MAX:]
        t0 = _dt(rows[0]["t"])
    # Шаг опроса — медиана интервалов между строками («опрос ~10 с» в шапке)
    times = [_dt(r["t"]) for r in rows]
    gaps = sorted((b - a).total_seconds() for a, b in zip(times, times[1:]) if b > a)
    step = int(round(gaps[len(gaps) // 2])) if gaps else None
    return {
        "v": TRACE_VERSION,
        "t_from": t0.isoformat(), "t_to": t1.isoformat(), "stop": stop.isoformat(),
        "segment": segment, "step_sec": step,
        "max_rows": trace_params(cfg)["max_rows"],
        "cols": cols, "entry": entry, "rows": rows, "states": states,
    }


def _hms(ts: Any, tz=None, ref: Any = None) -> str:
    """Время ленты в местном поясе; другие сутки, чем у ref, — с датой."""
    from .labels import fmt_local
    t = fmt_local(ts, tz)
    if t is None:
        return "—"
    if ref is not None:
        d1, d2 = fmt_local(ts, tz, day=True), fmt_local(ref, tz, day=True)
        if d1 and d2 and d1[:5] != d2[:5]:
            return d1
    return t


def _hms_day(ts: Any, tz=None) -> str:
    from .labels import fmt_local
    return fmt_local(ts, tz, day=True) or "—"


def _fmtv(v: Any) -> str:
    from .labels import fmt_number
    x = _num(v)
    if x is None:
        return "—"
    return fmt_number(x, 0 if abs(x) >= 100 else None)


def _trace_series(trace: dict[str, Any]) -> tuple[list[dict], dict[str, list[float]]]:
    """(колонки, все значения колонки — вход и записи) для поиска неизменных."""
    cols = [c for c in trace.get("cols") or [] if isinstance(c, dict) and c.get("role")]
    vals: dict[str, list[float]] = {c["role"]: [] for c in cols}
    for role, e in (trace.get("entry") or {}).items():
        if role in vals and _num((e or {}).get("v")) is not None:
            vals[role].append(float(e["v"]))
    for row in trace.get("rows") or []:
        for role, v in (row.get("v") or {}).items():
            if role in vals and _num(v) is not None:
                vals[role].append(float(v))
    return cols, vals


def _split_const(cols: list[dict], vals: dict[str, list[float]]) -> tuple[list[dict], list[dict]]:
    """(неменявшиеся за ленту, менявшиеся); колонки без значений — ни там, ни там."""
    const, var = [], []
    for c in cols:
        vs = vals.get(c["role"]) or []
        if vs:
            (const if len({_fmtv(v) for v in vs}) == 1 else var).append(c)
    return const, var


def _state_text(st: dict[str, Any], tz=None, ref: Any = None) -> str:
    """«Разомкнут с 16.10 03:41:02; Замкнут с 09:26:10» — первое с датой."""
    pers = st.get("periods") or []
    return "; ".join(f"{p.get('label')} с {_hms(p.get('t'), tz, ref) if i else _hms_day(p.get('t'), tz)}"
                     for i, p in enumerate(pers))


def format_analog_trace_ru(trace: dict[str, Any] | None, tz=None,
                           max_rows: int | None = None) -> list[str]:
    """Печать ленты для входа совета (v5.1.22, P1-4): шапка с оговорками опроса,
    «Без изменений за ленту: …» для неменявшихся колонок, положение автомата,
    таблица по секундам опроса («·» — новой записи нет, значение держится),
    одна строка-разделитель на момент останова, ранние строки сверх max_rows —
    одной строкой «мин…макс». Время — местное (P1-6). Пусто — ленты нет
    (акты до v5.1.22)."""
    from .labels import tz_label, unit_ru
    if not isinstance(trace, dict) or not (trace.get("rows") or trace.get("entry")
                                           or trace.get("states")):
        return []
    stop = trace.get("stop")
    stop_dt = _dt(stop)
    lim = max(2, int(max_rows or trace.get("max_rows") or 40))
    cols, vals = _trace_series(trace)
    const, var = _split_const(cols, vals)
    out = [f"Лента отсчётов сегмента падения ({tz_label(tz)}; опрос ~{trace.get('step_sec') or 10} с; "
           f"ряд пишется по изменению — значение держится до следующей записи, «·» — новой записи "
           f"нет; пики между опросами не видны): {_hms(trace.get('t_from'), tz, stop)}–"
           f"{_hms(trace.get('t_to'), tz, stop)}, останов {_hms(stop, tz)}"]
    if const:
        out.append("  Без изменений за ленту: " + "; ".join(
            f"{_TRACE_NAME.get(c['role'], c['role'])} {_fmtv(vals[c['role']][0])} "
            f"{unit_ru(c.get('unit'))}".rstrip() for c in const))
    for st in trace.get("states") or []:
        out.append(f"  {_STATE_NAME.get(st.get('role'), st.get('name'))} ({st.get('addr')}): "
                   f"{_state_text(st, tz, stop)}")
    if not var:
        return out
    entry = trace.get("entry") or {}
    printable = [r for r in trace.get("rows") or []
                 if any(c["role"] in (r.get("v") or {}) for c in var)]
    out += ["```", "  время     " + " ".join(f"{_TRACE_HEAD.get(c['role'], c['role']):>7}" for c in var)]
    cur = {c["role"]: _num((entry.get(c["role"]) or {}).get("v")) for c in var}
    if any(v is not None for v in cur.values()):
        out.append(f"  {_hms(trace.get('t_from'), tz, stop):8}  "
                   + " ".join(f"{_fmtv(cur[c['role']]):>7}" for c in var) + "  ← на входе")
    if len(printable) > lim:
        fold, printable = printable[:len(printable) - lim + 1], printable[len(printable) - lim + 1:]
        rng = []
        for c in var:
            vs = [float(r["v"][c["role"]]) for r in fold if c["role"] in r["v"]]
            if vs:
                rng.append(f"{_TRACE_HEAD.get(c['role'], c['role'])} {_fmtv(min(vs))}…{_fmtv(max(vs))}")
                cur[c["role"]] = vs[-1]
        out.append(f"  {_hms(fold[0]['t'], tz, stop)}–{_hms(fold[-1]['t'], tz, stop)} "
                   f"(свёрнуто строк: {len(fold)}): " + "; ".join(rng))
    sep_done = False
    for r in printable:
        if not sep_done and stop_dt is not None and _dt(r["t"]) >= stop_dt:
            out.append(f"  ── {_hms(stop, tz)} останов ──")
            sep_done = True
        cells = []
        for c in var:
            role = c["role"]
            if role in r["v"]:
                cur[role] = float(r["v"][role])
                cells.append(f"{_fmtv(cur[role]):>7}")
            else:
                cells.append(f"{(_fmtv(cur[role]) + '·') if cur[role] is not None else '—':>7}")
        out.append(f"  {_hms(r['t'], tz, stop):8}  " + " ".join(cells))
    if not sep_done:
        out.append(f"  ── {_hms(stop, tz)} останов ──")
    out.append("```")
    return out


def fall_facts_ru(trace: dict[str, Any] | None, tz=None, n_before: int = 3) -> str | None:
    """Факты до останова одной строкой — для заключения аварийного стопа
    (v5.1.22, P1-5, решение В1) и шапки входа модели.

    Нагрузка и ток не менялись за ленту — «нагрузка 0 кВт и ток 0 А весь
    интервал ленты», иначе последняя запись до останова с её временем. Автомат —
    состояние на момент останова и с какого времени. Обороты и частота —
    последние n_before отсчётов до останова и отсчёт кадра останова (первая
    запись позже кадра — после звена «останов», «через N с после останова»): печатается
    только свежая запись, удержанное значение — «(частота 42,6 Гц держится с
    09:28:42)», а не как замер (критика прототипа #35941). Задание частоты
    регулятора (v5.1.23) — в той же цепочке с подписью: «1696 об/мин / 56,8 Гц /
    задание 30,6 Гц → … задание 55 Гц»; не менялось за ленту — в цепочку не
    идёт (оно в строке «Без изменений» ленты). None — ленты нет.
    """
    from .labels import unit_ru
    stop = _dt((trace or {}).get("stop")) if isinstance(trace, dict) else None
    if stop is None:
        return None
    cols, vals = _trace_series(trace)
    by_role = {c["role"]: c for c in cols}
    const_roles = {c["role"] for c in _split_const(cols, vals)[0]}
    entry = trace.get("entry") or {}
    rows = trace.get("rows") or []
    parts: list[str] = []

    def _val(role: str, v: float) -> str:
        return f"{_fmtv(v)} {unit_ru((by_role.get(role) or {}).get('unit'))}".rstrip()

    def _last_before(role: str) -> tuple[float, str] | None:
        e = entry.get(role)
        best = (float(e["v"]), e.get("t")) if e and _num(e.get("v")) is not None else None
        for r in rows:
            if _dt(r["t"]) >= stop:
                break
            if role in (r.get("v") or {}):
                best = (float(r["v"][role]), r["t"])
        return best

    load: list[tuple[bool, str]] = []
    for role, word in (("ACTIVE_POWER_TOTAL", "нагрузка"), ("CURRENT_AVG", "ток")):
        if role not in by_role or not vals.get(role):
            continue
        if role in const_roles:
            load.append((True, f"{word} {_val(role, vals[role][0])}"))
        else:
            lb = _last_before(role)
            if lb:
                load.append((False, f"{word} до останова {_val(role, lb[0])} "
                                    f"(запись {_hms(lb[1], tz, stop)})"))
    if load:
        parts.append(" и ".join(t for _, t in load) + " весь интервал ленты"
                     if all(k for k, _ in load) else "; ".join(t for _, t in load))

    for st in trace.get("states") or []:
        if st.get("role") not in _CB_ROLES:
            continue
        at = [p for p in st.get("periods") or [] if (_dt(p.get("t")) or stop) <= stop]
        if not at:
            continue
        cur = at[-1]
        lab = str(cur.get("label")).lower()
        if len(at) > 1:
            parts.append(f"автомат: {str(at[-2].get('label')).lower()} → {lab} в {_hms(cur['t'], tz, stop)}")
        else:
            parts.append(f"автомат {lab} с {_hms_day(cur['t'], tz)}")

    rf = [r for r in ("RPM", "FREQUENCY") if r in by_role]
    # Задание — только если менялось: неизменное держалось бы скобкой у каждого звена
    if "FREQ_REFERENCE" in by_role and vals.get("FREQ_REFERENCE") and "FREQ_REFERENCE" not in const_roles:
        rf.append("FREQ_REFERENCE")
    if rf:
        held: dict[str, tuple[float, str]] = {}
        for role in rf:
            e = entry.get(role)
            if e and _num(e.get("v")) is not None:
                held[role] = (float(e["v"]), e.get("t"))
        seq: list[tuple[dict, dict[str, tuple[float, str]]]] = []
        for r in rows:
            if any(role in (r.get("v") or {}) for role in rf):
                seq.append((r, dict(held)))
                for role in rf:
                    if role in r["v"]:
                        held[role] = (float(r["v"][role]), r["t"])
        before = [x for x in seq if _dt(x[0]["t"]) < stop][-max(1, n_before):]
        after = next((x for x in seq if _dt(x[0]["t"]) >= stop), None)

        def _row(r: dict, h: dict[str, tuple[float, str]], mark: str = "") -> str:
            # Обороты и частота — по единице, задание — с подписью (тоже Гц)
            fresh = [("задание " if role == "FREQ_REFERENCE" else "") + _val(role, float(r["v"][role]))
                     for role in rf if role in r["v"]]
            kept = [f"{_TRACE_NAME.get(role, role)} {_val(role, h[role][0])} "
                    f"{'держатся' if role == 'RPM' else 'держится'} с {_hms(h[role][1], tz, stop)}"
                    for role in rf if role not in r["v"] and role in h]
            txt = _hms(r["t"], tz, stop) + mark + " " + " / ".join(fresh)
            return txt + (f" ({'; '.join(kept)})" if kept else "")

        chain = [_row(r, h) for r, h in before]
        if after is None:
            chain.append(f"{_hms(stop, tz)} останов")
        else:
            # v5.1.22, ревью incident-7 / shadow-1: «кадр останова» — только
            # отсчёт того же кадра опроса (≤ 1 с, _SAME_FRAME_SEC нарезки). Первая
            # запись через 6–8 с (ДЭС №3 13.10: останов 10:40:11, обороты 0 —
            # 10:40:19) — после разделителя останова и со сдвигом: в момент
            # останова её не было, держалось прежнее
            _gap = (_dt(after[0]["t"]) - stop).total_seconds()
            if _gap <= 1.0:
                chain.append(_row(after[0], after[1], " (кадр останова)"))
            else:
                chain.append(f"{_hms(stop, tz)} останов")
                chain.append(_row(after[0], after[1],
                                  f" (через {int(round(_gap))} с после останова)"))
        if before or after is not None:
            what = "обороты, частота и задание" if "FREQ_REFERENCE" in rf else "обороты и частота"
            parts.append(f"{what} по отсчётам: " + " → ".join(chain))

    if not parts:
        return None
    seg = trace.get("segment") or {}
    lead = "До останова"
    if seg.get("label") and seg.get("t_start"):
        lead += (f" — {seg['label']} {_hms(seg['t_start'], tz, stop)}–"
                 f"{_hms(seg.get('t_end') or trace['stop'], tz, stop)}")
    return f"{lead}: " + "; ".join(parts) + "."
