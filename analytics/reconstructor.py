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
    return {
        "ts":       _tz(p["state_start"]),
        "end":      _tz(p.get("state_end")),
        "kind":     "state",
        "addr":     addr,
        "bit":      None,
        "role":     meta.get("role"),
        "name":     meta.get("description") or meta.get("role") or f"reg_{addr}",
        "value":    p.get("value"),
        "label":    p.get("label"),
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


def serialize_chronology(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Лента → JSON-совместимый вид (datetime → ISO) для хранения в incident_json."""
    out = []
    for e in events:
        r = dict(e)
        r["ts"] = e["ts"].isoformat() if e.get("ts") else None
        r["end"] = e["end"].isoformat() if e.get("end") else None
        out.append(r)
    return out
