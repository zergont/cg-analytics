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


def prev_stop_end(
    enum_periods: list[dict[str, Any]], stop_ts: datetime
) -> datetime | None:
    """Конец предыдущей стоянки перед stop_ts; None — предыдущей не было.

    Пол окна ВЕРДИКТА, и только его. Вердикт отвечает на вопрос «как упала
    именно эта попытка — из работы или через расхолаживание», поэтому смотреть
    дальше конца прошлой стоянки ему нечего: чужое расхолаживание в окне
    превращает немедленное падение в контролируемое. На серии 16.09 без этого
    пола падение 13:31:08 читалось как «через охлаждение».

    Окно ЛЕНТЫ акта этим полом не ограничено — у него другой вопрос и свой
    якорь, см. find_baseline_anchor.
    """
    stop_ts = _tz(stop_ts)
    ends = [
        _tz(p["state_end"])
        for p in _periods_for(enum_periods, _ADDR_RUN_STATE)
        if p.get("value") == _RS_STOP and p.get("state_end")
        and _tz(p["state_end"]) <= stop_ts
    ]
    return max(ends) if ends else None


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
    fault_periods: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Характер останова в момент stop_ts: immediate / controlled / unknown.

    immediate (падение в не-штат) → is_incident=True (нужен incident_json + акт).
    """
    stop_ts = _tz(stop_ts)
    win_from = stop_ts - timedelta(seconds=lookback_sec)
    # Не заглядывать в предыдущую стоянку: иначе в серии попыток пуска гейт
    # видит чужое расхолаживание и объявляет останов контролируемым
    _floor = prev_stop_end(enum_periods, stop_ts)
    if _floor is not None and _floor > win_from:
        win_from = _floor
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


# Докуда искать нормальный останов. Не семантическая граница, а предохранитель:
# машина, которая давно не останавливалась штатно, не должна тянуть в акт всю
# свою историю. Не нашли за это время — идём в фолбэк.
# Докуда искать эталон. Не смысловая граница, а предохранитель от запроса в
# начало времён: длительность сама по себе ничего не портит — наоборот, месяц
# работы без штатного останова это диагноз, а не шум. Размер акта ограничивает
# не время, а порог по числу событий.
BASELINE_SEARCH_SEC = 30 * 24 * 3600
# Фолбэк, когда эталона в горизонте нет: три последних стоп-периода, но окно
# не короче суток (берём более раннюю границу из двух).
BASELINE_FALLBACK_PERIODS = 3
BASELINE_FALLBACK_MIN_SEC = 24 * 3600
# Потолок ленты акта. Упирается в контекстное окно запроса к ИИ, а не в
# здравый смысл: сколько событий модель осилит, столько и кладём.
MAX_ACT_EVENTS = 300


def find_baseline_anchor(
    enum_periods: list[dict[str, Any]],
    fault_periods: list[dict[str, Any]],
    stop_ts: datetime,
    cfg: Any = None,
    *,
    fallback_periods: int = BASELINE_FALLBACK_PERIODS,
    fallback_min_sec: int = BASELINE_FALLBACK_MIN_SEC,
) -> tuple[datetime, str]:
    """Начало последнего НОРМАЛЬНОГО останова перед stop_ts. (момент, причина).

    Цикл оборудования — работа, стоп, и так по кругу. Если прошлый стоп прошёл
    штатно, значит на тот момент с машиной было всё в порядке; всё, что
    случилось после, — возможные предвестники этой аварии. Поэтому окно акта
    отсчитывается отсюда, а не от числа минут: предупреждение способно
    опередить аварию на часы, а длительность сама по себе не мешает — она
    диагностична.

    Нормальный останов — стоп-сегмент, который НЕ аварийный И открылся НЕ
    снятием аварии. Второе условие существенно: после аварии машину не пустят,
    пока не сбросят ошибки, и как только сбросили, аварийный стоп становится
    простым. Но этот простой стоп — состояние восстановления, хвост той же
    аварии (`cause_open = SHUTDOWN_CLEARED`), а не самостоятельный останов.
    Считать его эталоном значит обрезать серию попыток на первом же звене,
    тогда как следующая авария должна вбирать все предыдущие.

    Расхолаживания в условии НЕТ: останов без него бывает штатным, а
    аварийность определяется маской в момент падения, и только ею.

    Причина — 'normal_stop' либо 'fallback'. Фолбэк (эталона в горизонте нет)
    — три последних стоп-периода, но не короче суток. Отсутствие эталона само
    по себе диагноз, поэтому причина едет в акт, а не молчит.
    """
    from .segmenter import _classify_stop_periods

    stop_ts = _tz(stop_ts)
    rs = _periods_for(enum_periods, _ADDR_RUN_STATE)
    try:
        parts = _classify_stop_periods(rs, fault_periods or [], cfg, stop_ts)
    except Exception:
        parts = rs

    best: datetime | None = None
    stop_starts: list[datetime] = []
    for p in parts:
        if int(p.get("value", -1)) != _RS_STOP:
            continue
        p_start = _tz(p["state_start"])
        if p_start >= stop_ts:
            break
        stop_starts.append(p_start)
        if (p.get("stop_kind") != "EMERGENCY"
                and p.get("cause_open") != "SHUTDOWN_CLEARED"):
            best = p_start
    if best is not None:
        return best, "normal_stop"

    by_periods = (
        stop_starts[-fallback_periods] if len(stop_starts) >= fallback_periods
        else (stop_starts[0] if stop_starts else stop_ts)
    )
    by_time = stop_ts - timedelta(seconds=fallback_min_sec)
    return min(by_periods, by_time), "fallback"


def cap_act_events(
    events: list[dict[str, Any]], stop_ts: datetime, max_events: int,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Обрезать ленту до порога. Возвращает (лента, сведения об обрезке).

    Режем не по времени — по числу событий: длительность цикла диагностична
    сама по себе, а вот контекстное окно запроса к ИИ конечно. Порядок
    жертв: сперва самые ранние смены состояний, фронты неисправностей держим
    до последнего — они существо дела. Всё, что после останова, не трогаем.
    Обрезка не молчит: сколько и до какого момента, едет в акт.
    """
    if not max_events or len(events) <= max_events:
        return events, None

    def _ts(e):
        raw = e.get("ts")
        return datetime.fromisoformat(raw) if isinstance(raw, str) else raw

    stop_ts = _tz(stop_ts)
    after = [e for e in events if _ts(e) is None or _ts(e) >= stop_ts]
    before = [e for e in events if e not in after]
    budget = max(0, max_events - len(after))
    faults = [e for e in before if e.get("kind") == "fault"]
    states = [e for e in before if e.get("kind") != "fault"]
    if len(faults) >= budget:
        kept_before = faults[-budget:] if budget else []
    else:
        kept_before = faults + states[-(budget - len(faults)):] if budget else []
    kept_before.sort(key=lambda e: (_ts(e) or stop_ts))
    kept = kept_before + after
    dropped = len(events) - len(kept)
    edge = _ts(kept_before[0]) if kept_before else stop_ts
    return kept, {
        "dropped": dropped,
        "kept_from": edge.isoformat() if edge else None,
        "reason": "предел ленты акта",
    }


def standing_faults(
    fault_periods: list[dict[str, Any]], stop_ts: datetime,
) -> list[dict[str, Any]]:
    """Маски, активные В МОМЕНТ останова, со временем их появления.

    Не лента, а срез состояния. Нужен потому, что лента фильтруется по НАЧАЛУ
    события: маска, поднявшаяся задолго до окна и всё ещё висящая, не попадёт
    в неё никогда — даже если она и есть причина. В проде такие живут неделями
    (40408/11 на ДГУ №1 висела двенадцать суток), и в акте им место одной
    строкой «висит с такого-то», а не пятью сутками событий.
    """
    stop_ts = _tz(stop_ts)
    out: list[dict[str, Any]] = []
    for fp in fault_periods or []:
        fs = _tz(fp["fault_start"])
        fe = _tz(fp["fault_end"]) if fp.get("fault_end") else None
        if fs > stop_ts or (fe is not None and fe <= stop_ts):
            continue
        out.append({
            "name": fp.get("fault_name_ru") or fp.get("fault_name")
                    or f'fault_{fp.get("addr")}/{fp.get("bit")}',
            "severity": fp.get("severity"),
            "addr": fp.get("addr"),
            "bit": fp.get("bit"),
            "since": fs.isoformat(),
            "age_sec": max(0.0, (stop_ts - fs).total_seconds()),
            "cleared_at": fe.isoformat() if fe else None,
        })
    out.sort(key=lambda e: -e["age_sec"])
    return out


_INVESTIGATOR_VERSION = "1.0"


def build_stop_incident(
    enum_periods: list[dict[str, Any]],
    fault_periods: list[dict[str, Any]],
    stop_ts: datetime,
    t_end: datetime | None = None,
    cfg: Any = None,
    preamble_sec: int = 300,   # не используется: начало окна даёт якорь
    stop_kind: str | None = None,
    stabilization_sec: int = 60,
    max_events: int = MAX_ACT_EVENTS,
) -> dict[str, Any] | None:
    """incident_json для аварийного стоп-сегмента; иначе None.

    Вызывается при закрытии стоп-сегмента (stop_ts = его начало). Когда строим —
    вердикт характера + лента [stop-preamble, t_end]. Возвращает JSON-совместимый
    dict (datetime → ISO) для хранения в JSONB.

    stop_kind — вид стоп-сегмента новой модели ('EMERGENCY' / 'SIMPLE' / None).
    Когда он передан, решает именно он: признак аварии — активная маска панели в
    момент останова, а не вердикт характер-гейта. Гейт упирается в оконный дефект
    (период RUN_STATE=3 выпадает из окна пересчёта, rs_before=None →
    is_fall_from_work=False), из-за чего за 30 дней построился один акт на восемь
    аварий. Вердикт при этом считается как и раньше и едет в артефакт описанием
    характера останова.
    None — старая модель (флаг stop_kind выключен): решает is_incident, как раньше.
    """
    verdict = classify_stop_character(
        enum_periods, stop_ts, cfg, fault_periods=fault_periods
    )
    if stop_kind is not None:
        if stop_kind != "EMERGENCY":
            return None
    elif not verdict["is_incident"]:
        return None

    from .reconstructor import build_chronology, serialize_chronology

    stop_ts = _tz(stop_ts)

    # Начало окна — последний нормальный останов: всё, что случилось после
    # него, может быть предвестником этой аварии. Число минут тут не годится:
    # предупреждение способно опередить аварию на часы.
    win_from, baseline_reason = find_baseline_anchor(
        enum_periods, fault_periods, stop_ts, cfg
    )
    # Пола по предыдущей аварии тут НЕТ сознательно: если между падениями
    # не было штатного останова, значит это серия попыток, и следующий акт
    # должен вбирать предыдущие — они одна история и так уйдут в разбор ИИ.
    # Был штатный останов — якорь сам встанет на нём.
    # Конец — не позднее лага стабилизации: сопутствующие неисправности
    # (горячий останов, рост температуры) приходят уже после самого останова
    # и относятся к этой же аварии. Дальше конца сегмента не идём.
    win_to = _tz(t_end) if t_end is not None else None
    if stabilization_sec:
        lag_to = stop_ts + timedelta(seconds=stabilization_sec)
        win_to = lag_to if win_to is None else min(win_to, lag_to)

    chrono = build_chronology(
        enum_periods, fault_periods, cfg, window_from=win_from, window_to=win_to
    )
    events, truncated = cap_act_events(
        serialize_chronology(chrono), stop_ts, max_events
    )
    return {
        "kind": "stop_incident",
        "stop_ts": stop_ts.isoformat(),
        "stop_kind": stop_kind,
        "character": verdict,
        # Что висело на момент падения — маска могла подняться задолго до окна
        "standing": standing_faults(fault_periods, stop_ts),
        "window": {
            "from": win_from.isoformat(),
            "to": win_to.isoformat() if win_to else None,
            "baseline": baseline_reason,
        },
        "chronology": events,
        "truncated": truncated,
        "investigator_version": _INVESTIGATOR_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
