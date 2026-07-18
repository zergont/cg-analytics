# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Классификатор причины останова («Следователь»).

Первый кусок — ХАРАКТЕР-ГЕЙТ: на переходе работа→останов решает, штатный это
останов или падение в не-штат. От этого зависит ветка хранения:
  - controlled (штатный, через охлаждение) → обычная схема сегмента;
  - immediate  (немедленный/горячий)       → incident_json + черновик акта.

Решение — по трём независимым сигналам (детерминированно из enum-периодов):
  1. RUN_STATE (40011): прошёл ли cooldown 4/5 перед остановом;
  2. RunCommand (40599): прямой Работа→EmergencyStop vs через фазу «Стоп»;
  3. тип неисправности (40013): Shutdown(4) vs ShutdownWithCooldown(3).
Согласие сигналов даёт уверенность; конфликт помечается (флаг неоднозначности).

Пороги/логика — из наблюдаемых состояний; RUN_STATE 4/5 = штатное охлаждение
(operation_rules: 3–5 мин на холостом перед остановом). Полный классификатор
(кто/нарушение/последствия) — отдельным шагом.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

# enum-значения (PCC3300)
_RS_COOLDOWN = (4, 5)          # RUN_STATE: Cooldown/StopDelay, CooldownatIdle
_RS_STOP = 0                   # RUN_STATE: Stop
_RC_EMERGENCY_STOP = 0         # 40599: EmergencyStop
_RC_STOP = 1                   # 40599: Stop
_FT_SHUTDOWN = 4               # 40013: Shutdown (немедленный)
_FT_SHUTDOWN_COOLDOWN = 3      # 40013: ShutdownwithCooldown (контролируемый)

_ADDR_RUN_STATE = 40011
_ADDR_RUN_COMMAND = 40599
_ADDR_FAULT_TYPE = 40013


def _tz(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _periods_for(enum_periods: list[dict[str, Any]], addr: int) -> list[dict[str, Any]]:
    return sorted(
        (p for p in enum_periods if p["addr"] == addr),
        key=lambda p: _tz(p["state_start"]),
    )


def _value_at(periods: list[dict[str, Any]], ts: datetime) -> Any:
    """Значение периода, покрывающего ts (state_start <= ts < state_end|active)."""
    ts = _tz(ts)
    hit = None
    for p in periods:
        s = _tz(p["state_start"])
        e = _tz(p["state_end"]) if p.get("state_end") else None
        if s <= ts and (e is None or ts < e):
            hit = p.get("value")
    return hit


def find_work_to_stop(enum_periods: list[dict[str, Any]]) -> list[datetime]:
    """Моменты перехода RUN_STATE из не-стопа в стоп (0). Кандидаты на разбор."""
    rs = _periods_for(enum_periods, _ADDR_RUN_STATE)
    out: list[datetime] = []
    for prev, cur in zip(rs, rs[1:]):
        if prev.get("value") != _RS_STOP and cur.get("value") == _RS_STOP:
            out.append(_tz(cur["state_start"]))
    return out


def classify_stop_character(
    enum_periods: list[dict[str, Any]],
    stop_ts: datetime,
    cfg: Any = None,
    lookback_sec: int = 600,
) -> dict[str, Any]:
    """Характер останова в момент stop_ts: immediate / controlled / unknown.

    immediate (падение в не-штат) → is_incident=True (нужен incident_json + акт).
    """
    stop_ts = _tz(stop_ts)
    win_from = stop_ts - timedelta(seconds=lookback_sec)
    rs = _periods_for(enum_periods, _ADDR_RUN_STATE)
    rc = _periods_for(enum_periods, _ADDR_RUN_COMMAND)
    ft = _periods_for(enum_periods, _ADDR_FAULT_TYPE)

    # 1) RUN_STATE: было ли охлаждение 4/5 в окне до останова
    passed_cooldown = any(
        win_from <= _tz(p["state_start"]) < stop_ts and p.get("value") in _RS_COOLDOWN
        for p in rs
    )
    rs_before = _value_at(rs, stop_ts - timedelta(seconds=1))

    # 2) RunCommand: через «Стоп» или прямой EmergencyStop
    rc_at = _value_at(rc, stop_ts)
    had_stop_cmd = any(
        win_from <= _tz(p["state_start"]) <= stop_ts and p.get("value") == _RC_STOP
        for p in rc
    )
    if had_stop_cmd:
        rc_path = "via_stop"
    elif rc_at == _RC_EMERGENCY_STOP:
        rc_path = "direct_estop"
    else:
        rc_path = "unknown"

    # 3) Тип последней неисправности на останове
    fault_type = _value_at(ft, stop_ts)

    immediate: list[str] = []
    controlled: list[str] = []
    if not passed_cooldown:
        immediate.append("RUN_STATE без cooldown 4/5")
    else:
        controlled.append("RUN_STATE через cooldown")
    if rc_path == "direct_estop":
        immediate.append("RunCommand прямой EmergencyStop")
    elif rc_path == "via_stop":
        controlled.append("RunCommand через «Стоп»")
    if fault_type == _FT_SHUTDOWN:
        immediate.append("40013=Shutdown")
    elif fault_type == _FT_SHUTDOWN_COOLDOWN:
        controlled.append("40013=ShutdownWithCooldown")

    if immediate and not controlled:
        character, confidence = "immediate", "high"
    elif controlled and not immediate:
        character, confidence = "controlled", "high"
    elif immediate and controlled:
        character = "immediate" if len(immediate) >= len(controlled) else "controlled"
        confidence = "low"  # конфликт сигналов — флаг неоднозначности
    else:
        character, confidence = "unknown", "low"

    # Падение именно ИЗ РАБОТЫ: перед остановом машина не стояла. Отсекает
    # под-сегменты реза stopped→stopped (rs_before=0), которые иначе ложно
    # попали бы в immediate по «нет cooldown + резалка».
    is_fall_from_work = rs_before is not None and rs_before != _RS_STOP

    return {
        "character": character,
        "is_incident": character == "immediate" and is_fall_from_work,
        "confidence": confidence,
        "signals": {
            "passed_cooldown": passed_cooldown,
            "run_state_before": rs_before,
            "is_fall_from_work": is_fall_from_work,
            "run_command_path": rc_path,
            "fault_type_40013": fault_type,
        },
        "immediate_votes": immediate,
        "controlled_votes": controlled,
    }


_INVESTIGATOR_VERSION = "1.0"


def build_stop_incident(
    enum_periods: list[dict[str, Any]],
    fault_periods: list[dict[str, Any]],
    stop_ts: datetime,
    t_end: datetime | None = None,
    cfg: Any = None,
    preamble_sec: int = 300,
) -> dict[str, Any] | None:
    """incident_json для падения работа→не-штат; иначе None.

    Вызывается при закрытии стоп-сегмента (stop_ts = его начало). Если характер
    не immediate или это не падение из работы — None (обычная схема, без тяжёлого
    артефакта). Иначе: вердикт характера + лента [stop-preamble, t_end] (onset).
    Возвращает JSON-совместимый dict (datetime → ISO) для хранения в JSONB.
    """
    verdict = classify_stop_character(enum_periods, stop_ts, cfg)
    if not verdict["is_incident"]:
        return None

    from .reconstructor import build_chronology, serialize_chronology

    stop_ts = _tz(stop_ts)
    win_from = stop_ts - timedelta(seconds=preamble_sec)
    win_to = _tz(t_end) if t_end is not None else None
    chrono = build_chronology(
        enum_periods, fault_periods, cfg, window_from=win_from, window_to=win_to
    )
    return {
        "kind": "stop_incident",
        "stop_ts": stop_ts.isoformat(),
        "character": verdict,
        "chronology": serialize_chronology(chrono),
        "investigator_version": _INVESTIGATOR_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
