# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Сегментатор: L1 по смене RUN_STATE (40011), L2 по зонам нагрузки.

Ключевые алгоритмы:
- ZoneTracker: асимметричный гистерезис + подтверждение N_stab + ретроактивная граница
- Преамбула: последние preamble.slices аналоговых строк до смены RUN_STATE включаются
  в расчёт характеристик первого подсегмента
- Fault-события режут подсегмент немедленно (без N_stab)
- Аккумуляторы рисков сквозные — не сбрасываются между сегментами

Входные данные (уже загружены runner-ом из БД):
  enum_periods   — из enum_history (addr=40011 RUN_STATE, addr=40010 SWITCH_POS)
  history        — из history_rich  (аналоговые whitelist-регистры)
  fault_periods  — из fault_history (addr 40400–40428)
  gaps           — из data_gaps
"""
from __future__ import annotations

import bisect
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

from .config import AnalyticsConfig
from .contract import Characteristic, RiskAccumulators, Segment, Subsegment
from .forward_fill import apply_forward_fill
from .metrics import compute_characteristics, compute_derived_metrics
from .accumulators import update_accumulators
from .detectors import run_all_detectors


# ── Вспомогательные ──────────────────────────────────────────────────────────

def _tz(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _fv(row: dict) -> float | None:
    v = row.get("value")
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _filter_by_window(
    by_addr: dict[int, list[dict]],
    t_start: datetime,
    t_end: datetime,
) -> dict[int, list[dict]]:
    """Вернуть срез by_addr только с записями в [t_start, t_end)."""
    t0, t1 = _tz(t_start), _tz(t_end)
    return {
        addr: [r for r in rows if t0 <= _tz(r["ts"]) < t1]
        for addr, rows in by_addr.items()
    }


def _filter_gaps(
    gaps: list[dict],
    t_start: datetime,
    t_end: datetime,
) -> list[dict]:
    t0, t1 = _tz(t_start), _tz(t_end)
    result = []
    for g in gaps:
        gs = _tz(g["gap_start"])
        ge_raw = g.get("gap_end")
        ge = _tz(ge_raw) if ge_raw else t1
        if gs < t1 and ge > t0:
            result.append(g)
    return result


def _clip_gap_intervals(
    gaps: list[dict],
    t_start: datetime,
    t_end: datetime,
) -> list[dict]:
    """Интервалы дыр связи, обрезанные по [t_start, t_end), для отчёта.

    Возвращает список {"start": iso, "end": iso, "duration_sec": float},
    отсортированный по времени. Незакрытый gap (gap_end IS NULL) тянется до t_end.
    """
    t0, t1 = _tz(t_start), _tz(t_end)
    out: list[dict] = []
    for g in gaps:
        gs = max(_tz(g["gap_start"]), t0)
        ge_raw = g.get("gap_end")
        ge = min(_tz(ge_raw) if ge_raw else t1, t1)
        if ge > gs:
            out.append({
                "start": gs.isoformat(),
                "end": ge.isoformat(),
                "duration_sec": (ge - gs).total_seconds(),
            })
    out.sort(key=lambda x: x["start"])
    return out


def _merge_intervals(ivals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    """Объединить пересекающиеся интервалы (вход не обязан быть отсортирован)."""
    if not ivals:
        return []
    ivals = sorted(ivals)
    out = [ivals[0]]
    for s, e in ivals[1:]:
        if s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def _split_stop_on_fault_cleared(
    run_state_periods: list[dict],
    fault_periods: list[dict],
    all_by_addr: dict[int, list[dict]],
    cfg,
    tt: datetime,
    enum_periods: list[dict] | None = None,
) -> list[dict]:
    """Разрезать СТОП-периоды (RUN_STATE=0) по устранению неисправностей.

    «Грязно» = активен не-INFO бит масок ИЛИ несброшенный код в регистре
    LAST_FAULT_CODE (40012, обнуляется только кнопкой сброса после устранения).
    Граница ставится на момент первого чистого замера, если чистота
    продержалась ≥ stable_clean_sec (защита от «сброс → через минуту кнопка»:
    вернувшиеся коды отменяют рез). Первый кусок закрывается FAULT_CLEARED.

    Правило детерминировано из history — перечитка воспроизводит границы.
    """
    if not bool(cfg.seg("fault_cleared", "enabled", default=True)):
        return run_state_periods
    stable_sec = float(cfg.seg("fault_cleared", "stable_clean_sec", default=300))
    lfc_addr = int(cfg.seg("fault_cleared", "last_fault_code_addr", default=40012))
    tt = _tz(tt)

    # Не-INFO периоды битов масок (severity по bitmap KB)
    dirty_bits: list[tuple[datetime, datetime]] = []
    for fp in fault_periods:
        if cfg.bitmap_severity(fp.get("severity") or "none") == "INFO":
            continue
        fs = _tz(fp["fault_start"])
        fe = _tz(fp["fault_end"]) if fp.get("fault_end") else tt
        if fe > fs:
            dirty_bits.append((fs, fe))

    # Интервалы несброшенного кода 40012 — по периодам состояния (v5.1.1;
    # раньше step-функция по «замерам», которых в окне могло не быть)
    lfc_periods = [p for p in (enum_periods or []) if p.get("addr") == lfc_addr]
    if lfc_periods:
        for p in lfc_periods:
            try:
                is_dirty = p.get("value") is not None and float(p["value"]) != 0
            except (TypeError, ValueError):
                is_dirty = False
            if is_dirty:
                s = _tz(p["state_start"])
                e = _tz(p["state_end"]) if p.get("state_end") else tt
                if e > s:
                    dirty_bits.append((s, e))
    else:
        lfc_rows = all_by_addr.get(lfc_addr) or []
        prev_ts, prev_dirty = None, False
        for r in lfc_rows:
            v = _fv(r)
            is_dirty = v is not None and v != 0
            ts = _tz(r["ts"])
            if prev_dirty and prev_ts is not None:
                dirty_bits.append((prev_ts, ts))
            prev_ts, prev_dirty = ts, is_dirty
        if prev_dirty and prev_ts is not None and tt > prev_ts:
            dirty_bits.append((prev_ts, tt))

    dirty = _merge_intervals(dirty_bits)
    if not dirty:
        return run_state_periods

    out: list[dict] = []
    for period in run_state_periods:
        if int(period["value"]) != 0:
            out.append(period)
            continue
        p_start = _tz(period["state_start"])
        p_end_raw = period.get("state_end")
        p_end = _tz(p_end_raw) if p_end_raw else tt

        # Грязные интервалы, обрезанные по периоду
        in_period = [
            (max(s, p_start), min(e, p_end))
            for s, e in dirty
            if s < p_end and e > p_start
        ]
        # Границы: конец грязи, после которого чисто ≥ stable_sec
        boundaries: list[datetime] = []
        for i, (_, e) in enumerate(in_period):
            nxt = in_period[i + 1][0] if i + 1 < len(in_period) else p_end
            if e < p_end and (nxt - e).total_seconds() >= stable_sec:
                boundaries.append(e)

        if not boundaries:
            out.append(period)
            continue

        cuts = [p_start] + boundaries + [p_end]
        for i in range(len(cuts) - 1):
            part = dict(period)
            part["state_start"] = cuts[i]
            is_last = i == len(cuts) - 2
            part["state_end"] = p_end_raw if is_last else cuts[i + 1]
            if i > 0:
                part["cause_open"] = "FAULT_CLEARED"
            if not is_last:
                part["cause_close"] = "FAULT_CLEARED"
            out.append(part)
    return out


def _split_stop_on_shutdown(
    run_state_periods: list[dict],
    fault_periods: list[dict],
    cfg,
    tt: datetime,
) -> list[dict]:
    """Разрезать СТОП-периоды (RUN_STATE=0) по пересечению уровня в АВАРИЮ.

    SHUTDOWN-severity фронт (E-Stop и т.п.) на УЖЕ стоящей машине не должен красить
    весь стоп-сегмент: режем на входе в аварию и на возврате, чтобы красный
    аварийный интервал стал отдельным сегментом, а сегмент-причина сохранил свой
    уровень и разбор (инцидент 17.07: E-Stop в 17:10 хоронил горячий останов 16:33).
    Severity падает естественно из состава фолтов суб-сегмента — резать достаточно.

    Границы — строго ВНУТРИ периода: если авария активна с его начала, реза нет
    (пересечения «снизу вверх» не было). cause_close реза = RUN_STATE_CHANGE
    (безопасно для CHECK и распознаётся путями персиста); метка реза — в cause_open.
    Правило детерминировано из history — перечитка воспроизводит границы.
    """
    if not bool(cfg.seg("shutdown_split", "enabled", default=True)):
        return run_state_periods
    tt = _tz(tt)

    shutdown_iv: list[tuple[datetime, datetime]] = []
    for fp in fault_periods:
        if cfg.bitmap_severity(fp.get("severity") or "none") != "SHUTDOWN":
            continue
        fs = _tz(fp["fault_start"])
        fe = _tz(fp["fault_end"]) if fp.get("fault_end") else tt
        if fe > fs:
            shutdown_iv.append((fs, fe))
    shutdown_iv = _merge_intervals(shutdown_iv)
    if not shutdown_iv:
        return run_state_periods

    out: list[dict] = []
    for period in run_state_periods:
        if int(period["value"]) != 0:
            out.append(period)
            continue
        p_start = _tz(period["state_start"])
        p_end_raw = period.get("state_end")
        p_end = _tz(p_end_raw) if p_end_raw else tt

        # Границы = входы/выходы SHUTDOWN-интервалов, строго внутри периода
        bounds: set[datetime] = set()
        for s, e in shutdown_iv:
            if p_start < s < p_end:
                bounds.add(s)
            if p_start < e < p_end:
                bounds.add(e)
        if not bounds:
            out.append(period)
            continue

        cuts = [p_start] + sorted(bounds) + [p_end]
        for i in range(len(cuts) - 1):
            part = dict(period)
            part["state_start"] = cuts[i]
            is_last = i == len(cuts) - 2
            part["state_end"] = p_end_raw if is_last else cuts[i + 1]
            in_red = any(s <= cuts[i] < e for s, e in shutdown_iv)
            if i > 0:
                part["cause_open"] = "SHUTDOWN_ON_STOPPED" if in_red else "SHUTDOWN_STOPPED_CLEARED"
            if not is_last:
                part["cause_close"] = "RUN_STATE_CHANGE"
            out.append(part)
    return out


def fault_type_shutdown(cfg) -> tuple[int | None, frozenset[int]]:
    """Регистр типа последней неисправности и его значения аварийной тяжести.

    Кодировка своя у каждого контроллера, поэтому объявляет её он сам — в
    карте регистров, ключом `shutdown_states` у роли LAST_FAULT_TYPE. Нет
    объявления — источник выключен, аварию определяют только маски.
    """
    try:
        addr = cfg.role_to_addr("LAST_FAULT_TYPE")
        states = (cfg.register_map.get(addr) or {}).get("shutdown_states") if addr else None
        if not addr or not states:
            return None, frozenset()
        return addr, frozenset(int(v) for v in states)
    except (AttributeError, TypeError, ValueError):
        return None, frozenset()


def shutdown_intervals(
    fault_periods: list[dict], cfg, tt: datetime,
    enum_periods: list[dict] | None = None,
) -> list[tuple[datetime, datetime]]:
    """Слитые интервалы, в которых панель держала аварию.

    Два источника, оба — реакция самой панели:
      - маски аварийной тяжести;
      - тип последней неисправности (PCC3300: 40013 = Shutdown / Shutdown
        with Cooldown).
    Второй нужен потому, что не у каждого кода есть бит в масках. 1452 «Отказ
    включения автомата» роняет машину в Shutdown, но бита не имеет: за 45 суток
    так прошли мимо шесть аварий ДГУ №1 из семнадцати. Тип читается в одном
    кадре опроса с RUN_STATE — в момент останова он уже на месте, — и уходит
    ровно на сбросе: на всех семнадцати совпал со снятием масок с точностью до
    кадра и ни разу не пережил пуск.

    Слияние обязательно: авария, поднявшаяся поверх ещё не снятой, должна
    продлевать покрытие, иначе рез уедет к снятию первой, а не последней.
    Источники сливаются так же — рез идёт по тому, что снялось позже.
    Незакрытый интервал считается активным до tt.

    Общая для сегментатора (вид стоп-периода и место реза) и «Следователя»
    (какая из прошлых стоянок была нормальной) — шкала тяжести должна быть
    одна, иначе якорь и нарезка разойдутся.
    """
    tt = _tz(tt)
    out: list[tuple[datetime, datetime]] = []
    for fp in fault_periods:
        if cfg.bitmap_severity(fp.get("severity") or "none") != "SHUTDOWN":
            continue
        fs = _tz(fp["fault_start"])
        fe = _tz(fp["fault_end"]) if fp.get("fault_end") else tt
        if fe > fs:
            out.append((fs, fe))

    ft_addr, ft_states = fault_type_shutdown(cfg)
    if ft_addr is not None:
        for p in enum_periods or []:
            if p.get("addr") != ft_addr:
                continue
            try:
                if int(p.get("value")) not in ft_states:
                    continue
            except (TypeError, ValueError):
                continue
            s = _tz(p["state_start"])
            e = _tz(p["state_end"]) if p.get("state_end") else tt
            if e > s:
                out.append((s, e))
    return _merge_intervals(out)


def _classify_stop_periods(
    run_state_periods: list[dict],
    fault_periods: list[dict],
    cfg,
    tt: datetime,
    enum_periods: list[dict] | None = None,
) -> list[dict]:
    """Разделить СТОП-периоды на два вида и поставить единственный рез.

    Стоп считается аварийным, если в момент его начала панель держит аварию:
    активна маска аварийной тяжести или тип последней неисправности — Shutdown
    (см. shutdown_intervals). Залипшей с прошлого раза такая авария быть не
    может: с ней машину не пустить, значит она и есть причина этого останова —
    искать её в прошлом не нужно.

    Рез ровно один: там, где панель сняла аварию последней. Голова —
    АВАРИЙНЫЙ СТОП, хвост — простой СТОП «готов к пуску». Не сняли до пуска —
    реза нет, весь стоп аварийный. Авария, возникшая ПОСРЕДИ простого стопа
    (кнопка на стоящей машине), вид не меняет и не режет: её видно цветом
    живого сегмента и в хронологии. Так инцидент 17.07 решается без вырезания
    красного куска.

    enum_periods — все enum-периоды окна (нужен тип последней неисправности);
    без них решают одни маски.

    Заменяет пару _split_stop_on_fault_cleared / _split_stop_on_shutdown: те
    резали по любой смене «грязно/чисто» и по входу в аварию и давали до пяти
    кусков на один стоп, включая вырожденные в секунды.
    """
    shutdown_iv = shutdown_intervals(fault_periods, cfg, tt, enum_periods)

    out: list[dict] = []
    for period in run_state_periods:
        if int(period["value"]) != 0:
            out.append(period)
            continue

        p_start = _tz(period["state_start"])
        p_end_raw = period.get("state_end")
        p_end = _tz(p_end_raw) if p_end_raw else _tz(tt)

        covering = next(((s, e) for s, e in shutdown_iv if s <= p_start < e), None)
        if covering is None:
            part = dict(period)
            part["stop_kind"] = "SIMPLE"
            out.append(part)
            continue

        t_clear = covering[1]
        if t_clear >= p_end:
            part = dict(period)
            part["stop_kind"] = "EMERGENCY"
            out.append(part)
            continue

        head = dict(period)
        head["stop_kind"] = "EMERGENCY"
        head["state_end"] = t_clear
        head["cause_close"] = "SHUTDOWN_CLEARED"
        out.append(head)

        tail = dict(period)
        tail["stop_kind"] = "SIMPLE"
        tail["state_start"] = t_clear
        tail["state_end"] = p_end_raw
        tail["cause_open"] = "SHUTDOWN_CLEARED"
        out.append(tail)

    return out


def _state_periods(enum_periods: list[dict], cfg: AnalyticsConfig) -> dict[int, list[dict]]:
    """Периоды регистров-состояний, идущих в характеристики подсегмента.

    Это `kind: enum` с `state_characteristic: true` в карте (PCC3300: код и тип
    последней неисправности 40012/40013). По адресу, отсортированы по началу.
    """
    addrs = {a for a, m in cfg.register_map.items()
             if m.get("kind") == "enum" and m.get("state_characteristic")}
    out: dict[int, list[dict]] = {}
    for p in enum_periods or []:
        a = p.get("addr")
        if a not in addrs or p.get("value") is None:
            continue
        # «Нет данных» декодера (u16 NA = 65535) пишется в enum_history как
        # значение — это не код неисправности, состояние в этот период неизвестно
        na = cfg.register_map[a].get("na_values") or [65535]
        try:
            if int(p["value"]) in {int(x) for x in na}:
                continue
        except (TypeError, ValueError):
            continue
        out.setdefault(a, []).append(p)
    for lst in out.values():
        lst.sort(key=lambda p: _tz(p["state_start"]))
    return out


def _state_characteristics(
    state_periods: dict[int, list[dict]], t_start: datetime, t_end: datetime,
    cfg: AnalyticsConfig,
) -> dict[str, Any]:
    """Характеристики состояний за [t_start, t_end) — из периодов enum_history.

    Состояние держится, пока его не сменят: период, начавшийся днями раньше и
    не закрытый, покрывает любое окно. Поэтому значение известно в каждом
    подсегменте — в отличие от замера, который forward-fill переносит не
    дольше 90 с. До v5.1.1 код 40012 подменялся «замером» в начале окна и жил
    только в первом подсегменте, а при суточном закрытии терялся целиком.

    Форма — как у замера (value_start / value_end / min / max), потребители
    читают те же поля; median = значение на конец, slope — нет.
    """
    t0, t1 = _tz(t_start), _tz(t_end)
    result: dict[str, Any] = {}
    for addr, periods in state_periods.items():
        vals: list[float] = []
        for p in periods:
            s = _tz(p["state_start"])
            e = _tz(p["state_end"]) if p.get("state_end") else None
            if s >= t1 or (e is not None and e <= t0):
                continue
            try:
                vals.append(float(p["value"]))
            except (TypeError, ValueError):
                continue
        if not vals:
            continue
        meta = cfg.register_map.get(addr, {})
        role = meta.get("role", f"addr_{addr}")
        char = Characteristic(
            role=role, unit=meta.get("unit") or "", sample_count=len(vals),
            median=vals[-1], mad=None, min=min(vals), max=max(vals),
            value_start=vals[0], value_end=vals[-1], slope=None,
        ).to_dict()
        char["kind"] = "state"
        result[role] = char
    return result


def _build_ts_index(by_addr: dict[int, list[dict]]) -> dict[int, list[datetime]]:
    """Tz-нормализованный индекс меток времени для bisect-срезов."""
    return {addr: [_tz(r["ts"]) for r in rows] for addr, rows in by_addr.items()}


def _slice_by_addr(
    by_addr: dict[int, list[dict]],
    ts_index: dict[int, list[datetime]],
    t_start: datetime,
    t_end: datetime,
) -> dict[int, list[dict]]:
    """Нарезать by_addr по [t_start, t_end) через бинарный поиск. O(A·log n)."""
    t0, t1 = _tz(t_start), _tz(t_end)
    result: dict[int, list[dict]] = {}
    for addr, rows in by_addr.items():
        tss = ts_index[addr]
        lo = bisect.bisect_left(tss, t0)
        hi = bisect.bisect_left(tss, t1)
        if lo < hi:
            result[addr] = rows[lo:hi]
    return result


# ── Зонная классификация ──────────────────────────────────────────────────────

def _classify_zone_initial(load_pct: float, cfg: AnalyticsConfig) -> str:
    """Классифицировать зону нагрузки без гистерезиса (для первого чтения)."""
    zones = cfg.zone_boundaries()
    for name in ("OVERLOAD", "ELEVATED", "NORMAL", "LOW"):
        if name not in zones:
            continue
        z_min, z_max = zones[name]
        if z_min <= load_pct < z_max:
            return name
    return "NA"


def _classify_with_hysteresis(
    load_pct: float,
    current_zone: str,
    cfg: AnalyticsConfig,
) -> str:
    """Классифицировать с учётом асимметричного гистерезиса (ТЗ: «вход по границе, выход по границе-5%»).

    Гистерезис применяется только на НИЖНЕЙ границе зоны (при снижении нагрузки):
    - выход вниз: load_pct < z_min - hyst  (нужно упасть на hyst% ниже входного порога)
    - выход вверх: load_pct >= z_max        (стандартная граница, без расширения)

    Это предотвращает быстрые переключения при снижении нагрузки (типовой режим),
    но не задерживает вход в более нагруженную зону.
    """
    hyst = cfg.hysteresis_pct()
    zones = cfg.zone_boundaries()

    if current_zone in zones:
        z_min, z_max = zones[current_zone]
        eff_min = z_min - hyst   # нижняя граница с гистерезисом
        eff_max = z_max           # верхняя граница — стандартная (без расширения)
        if eff_min <= load_pct < eff_max:
            return current_zone

    return _classify_zone_initial(load_pct, cfg)


# ── ZoneTracker ───────────────────────────────────────────────────────────────

class ZoneTracker:
    """Отслеживает зону нагрузки с гистерезисом и подтверждением N_stab.

    Алгоритм:
    - N_stab последовательных чтений вне текущей зоны (с учётом гистерезиса)
      → подтверждение смены зоны
    - Граница размещается ретроактивно: в момент первого пересечения
    - Если до подтверждения читается другая зона или возврат — кандидат сбрасывается
    """

    def __init__(self, initial_zone: str, n_stab: int, cfg: AnalyticsConfig) -> None:
        self.current_zone = initial_zone
        self._n_stab = n_stab
        self._cfg = cfg
        self._candidate: str | None = None
        self._candidate_count: int = 0
        self._first_crossing_ts: datetime | None = None

    def update(
        self, load_pct: float, ts: datetime
    ) -> tuple[str, datetime] | None:
        """Обработать одно чтение LOAD_PCT.

        Возвращает (new_zone, retroactive_boundary_ts) если зона подтверждена,
        иначе None.
        """
        classified = _classify_with_hysteresis(load_pct, self.current_zone, self._cfg)

        if classified == self.current_zone:
            self._candidate = None
            self._candidate_count = 0
            self._first_crossing_ts = None
            return None

        if classified == self._candidate:
            self._candidate_count += 1
        else:
            self._candidate = classified
            self._candidate_count = 1
            self._first_crossing_ts = _tz(ts)

        if self._candidate_count >= self._n_stab:
            confirmed = self._candidate
            boundary_ts = self._first_crossing_ts
            self.current_zone = confirmed
            self._candidate = None
            self._candidate_count = 0
            self._first_crossing_ts = None
            return (confirmed, boundary_ts)

        return None


# ── Данные ───────────────────────────────────────────────────────────────────

def _build_by_addr(history: list[dict]) -> dict[int, list[dict]]:
    """Сгруппировать строки history по addr (сортировка по ts уже выполнена в БД)."""
    by_addr: dict[int, list[dict]] = {}
    for row in history:
        by_addr.setdefault(row["addr"], []).append(row)
    return by_addr


def _collect_preamble(
    by_addr: dict[int, list[dict]],
    seg_start: datetime,
    preamble_slices: int,
    cfg: AnalyticsConfig,
) -> tuple[dict[int, list[dict]], datetime | None]:
    """Собрать последние preamble_slices строк каждого аналогового адреса до seg_start.

    Возвращает (preamble_by_addr, effective_t_start) где effective_t_start —
    самая ранняя метка времени среди всех преамбульных строк (или None).
    """
    t0 = _tz(seg_start)
    preamble: dict[int, list[dict]] = {}
    earliest: datetime | None = None

    for addr, rows in by_addr.items():
        if cfg.register_map.get(addr, {}).get("kind") != "analog":
            continue
        before = [r for r in rows if _tz(r["ts"]) < t0]
        tail = before[-preamble_slices:]
        if tail:
            preamble[addr] = tail
            ts_first = _tz(tail[0]["ts"])
            if earliest is None or ts_first < earliest:
                earliest = ts_first

    return preamble, earliest


def _apply_scale(by_addr: dict[int, list[dict]], cfg: AnalyticsConfig) -> dict[int, list[dict]]:
    """Перевод в единицы карты (v5.1.6: наработка 40070 в БД — секунды, в карте — ч).

    Строки КОПИРУЮТСЯ: history — кэш открытого окна движка, мутация
    масштабировала бы его заново на каждом цикле.
    """
    for addr, meta in cfg.register_map.items():
        k = meta.get("_scale")
        if k and addr in by_addr:
            by_addr[addr] = [{**r, "value": float(r["value"]) * k if r.get("value") is not None else None}
                             for r in by_addr[addr]]
    return by_addr


def _get_engine_hours(
    by_addr: dict[int, list[dict]],
    t_start: datetime,
    cfg: AnalyticsConfig,
) -> float | None:
    """Первое значение ENGINE_HOURS вблизи t_start (или None)."""
    addr = cfg.role_to_addr("ENGINE_HOURS")
    if addr is None or addr not in by_addr:
        return None
    t0 = _tz(t_start)
    rows = by_addr[addr]
    for r in rows:
        if _tz(r["ts"]) >= t0:
            return _fv(r)
    return None


def _compute_data_quality(
    by_addr: dict[int, list[dict]],
    t_start: datetime,
    t_end: datetime,
    gaps: list[dict],
    cfg: AnalyticsConfig,
) -> float:
    """Качество связи [0.0, 1.0] = 1 - (время без связи / длительность окна).

    Единственный источник истины — таблица data_gaps. Каждый gap обрезается
    по границам окна; незакрытый gap (gap_end IS NULL) считается до t_end.
    Время без связи — по ОБЪЕДИНЕНИЮ интервалов: разрывы перекрываются (внешний
    сервис пишет несколько на одно окно, движок добавляет синтетический по
    max(history.ts)), и сумма длин занижала качество вплоть до 0 — а при 0
    сегмент считается «без связи» (заглушка, без ИИ). ДЭС №3 09.10: 0,40 при
    реальных ≥0,55, подсегмент с данными помечен 0%.
    """
    t0 = _tz(t_start)
    t1 = _tz(t_end)
    duration_sec = (t1 - t0).total_seconds()
    if duration_sec <= 0:
        return 1.0

    spans = []
    for g in gaps:
        gs = max(_tz(g["gap_start"]), t0)
        ge_raw = g.get("gap_end")
        ge = min(_tz(ge_raw) if ge_raw else t1, t1)
        if ge > gs:
            spans.append((gs, ge))
    gap_sec = union_seconds(spans)

    return round(max(0.0, 1.0 - gap_sec / duration_sec), 3)


def union_seconds(spans) -> float:
    """Длина объединения интервалов (start, end) в секундах — без двойного счёта
    перекрытий."""
    total = 0.0
    cur_s = cur_e = None
    for s, e in sorted(spans):
        if cur_e is None or s > cur_e:
            if cur_e is not None:
                total += (cur_e - cur_s).total_seconds()
            cur_s, cur_e = s, e
        elif e > cur_e:
            cur_e = e
    if cur_e is not None:
        total += (cur_e - cur_s).total_seconds()
    return total


# ── Построение подсегментов ───────────────────────────────────────────────────

def _build_single_subsegment(
    seg_id: str,
    idx: int,
    seg_by_addr: dict[int, list[dict]],
    preamble_t_start: datetime | None,
    t_start: datetime,
    t_end: datetime,
    load_zone: str,
    cause_open: str,
    cause_close: str | None,
    run_state: int,
    gaps: list[dict],
    accumulators: RiskAccumulators,
    prev_zone: str | None,
    cfg: AnalyticsConfig,
    fault_periods: list[dict],
    state_periods: dict[int, list[dict]] | None = None,
    detect_run_state: int | None = None,
    transient: bool = False,
    onset_faults: list[dict] | None = None,
) -> tuple[Subsegment, RiskAccumulators]:
    """Создать один подсегмент (для нейтральных состояний RUN_STATE ≠ 3).

    detect_run_state — режим, по которому включаются детекторы, если он не
    совпадает с режимом накопителей: у переходного куска «Работы» (разгон,
    сброс оборотов) детекторы работы молчат, панель — нет (v5.1.16).
    """
    subseg_id = f"{seg_id}.{idx}"
    char_t_start = preamble_t_start if (idx == 1 and preamble_t_start) else t_start

    ts_index = _build_ts_index(seg_by_addr)
    sub_chars = _slice_by_addr(seg_by_addr, ts_index, char_t_start, t_end)
    sub_win   = _slice_by_addr(seg_by_addr, ts_index, t_start, t_end)

    chars   = compute_characteristics(sub_chars, char_t_start, t_end, cfg)
    chars.update(_state_characteristics(state_periods or {}, t_start, t_end, cfg))
    derived = compute_derived_metrics(sub_win, t_start, t_end, gaps, cfg)
    new_acc = update_accumulators(accumulators, sub_win, t_start, t_end, load_zone, run_state, cfg, gaps)

    faults_in = [
        fp for fp in fault_periods
        if _tz(fp["fault_start"]) < _tz(t_end)
        and (_tz(fp["fault_end"]) if fp.get("fault_end") else _tz(t_end)) > _tz(t_start)
    ]
    detections = run_all_detectors(
        chars, derived, new_acc, faults_in,
        load_zone, run_state if detect_run_state is None else detect_run_state,
        t_start, t_end, prev_zone, cfg, state_periods,
        onset_faults=onset_faults if onset_faults is not None else fault_periods,
    )

    dq = _compute_data_quality(seg_by_addr, t_start, t_end, gaps, cfg)

    subseg = Subsegment(
        id=subseg_id,
        parent_segment_id=seg_id,
        t_start=_tz(t_start).isoformat(),
        t_end=_tz(t_end).isoformat(),
        duration_sec=(_tz(t_end) - _tz(t_start)).total_seconds(),
        load_zone=load_zone,
        cause_open=cause_open,
        cause_close=cause_close,
        characteristics=chars,
        derived_metrics=derived,
        risk_accumulators=new_acc,
        detections=detections,
        data_quality=dq,
        data_gaps=_clip_gap_intervals(gaps, t_start, t_end),
        transient=transient,
    )
    return subseg, new_acc


def _build_subsegments_for_running(
    seg_id: str,
    seg_by_addr: dict[int, list[dict]],
    preamble_t_start: datetime | None,
    seg_start: datetime,
    seg_end: datetime,
    fault_periods_in_seg: list[dict],
    gaps: list[dict],
    prev_accumulators: RiskAccumulators,
    run_state: int,
    cfg: AnalyticsConfig,
    state_periods: dict[int, list[dict]] | None = None,
    steady: tuple[datetime | None, datetime | None] | None = None,
    onset_faults: list[dict] | None = None,
) -> tuple[list[Subsegment], RiskAccumulators]:
    """L2-сегментация внутри RUN_STATE=3: ZoneTracker + fault-события.

    steady — рабочая часть сегмента (v5.1.16, _steady_window): до неё разгон с
    нулей пуска, после — сброс оборотов перед охлаждением. Эти куски —
    отдельные переходные подсегменты без детекторов работы (панель видна).
    (None, None) — рабочих оборотов не было, весь сегмент переходный;
    None — делить нечего (как до v5.1.16).
    """
    seg_t0 = _tz(seg_start)
    seg_t1 = _tz(seg_end)
    head_end, tail_start = seg_t0, seg_t1
    if steady is not None:
        s_from, s_to = steady
        if s_from is None:
            head_end = seg_t1
        else:
            head_end = max(seg_t0, min(_tz(s_from), seg_t1))
            tail_start = max(head_end, min(_tz(s_to) if s_to else seg_t1, seg_t1))

    pre_subs: list[Subsegment] = []
    accumulators = prev_accumulators
    if steady is not None and head_end <= seg_t0 and preamble_t_start is not None:
        # Преамбула со стоянки в рабочую часть не идёт: обороты в ней ниже
        # рабочих — это нули перед пуском (пробный пуск 13.10: «обороты 0»,
        # «давление масла 0» у работы, вышедшей на 1498 за секунду до смены)
        _prm = confirm_params(cfg)
        _rpm_a = cfg.role_to_addr("RPM")
        if any(_tz(r["ts"]) < seg_t0 and (_fv(r) or 0.0) < _prm["rated_rpm"]
               for r in (seg_by_addr.get(_rpm_a) or []) if _fv(r) is not None):
            preamble_t_start = None
    if head_end > seg_t0:
        sub, accumulators = _build_single_subsegment(
            seg_id=seg_id, idx=1, seg_by_addr=seg_by_addr,
            preamble_t_start=preamble_t_start, t_start=seg_t0, t_end=head_end,
            load_zone="NA", cause_open="SEG_START",
            cause_close="RPM_RATED" if head_end < seg_t1 else None,
            run_state=run_state, gaps=_filter_gaps(gaps, seg_t0, head_end),
            accumulators=accumulators, prev_zone=None, cfg=cfg,
            fault_periods=fault_periods_in_seg, state_periods=state_periods,
            detect_run_state=0, transient=True, onset_faults=onset_faults,
        )
        pre_subs.append(sub)
        if head_end >= seg_t1:
            return pre_subs, accumulators
        preamble_t_start = None   # преамбула — у разгона, не у рабочей части

    t_start_tz = head_end
    t_end_tz = tail_start

    n_stab = int(cfg.seg("boundary_confirmation", "n_stab", default=3))
    load_addr = cfg.role_to_addr("LOAD_PCT")

    # Временной ряд LOAD_PCT в окне сегмента
    load_rows: list[dict] = []
    if load_addr and load_addr in seg_by_addr:
        load_rows = [
            r for r in seg_by_addr[load_addr]
            if t_start_tz <= _tz(r["ts"]) < t_end_tz and r.get("value") is not None
        ]

    # Начальная зона (без гистерезиса)
    initial_zone = "NA"
    if load_rows:
        v = _fv(load_rows[0])
        if v is not None:
            initial_zone = _classify_zone_initial(v, cfg)

    tracker = ZoneTracker(initial_zone, n_stab, cfg)

    # Проход по LOAD_PCT — собираем ретроактивные зональные границы
    # zone_events: (retroactive_boundary_ts, new_zone) — отсортированы по boundary_ts
    zone_events: list[tuple[datetime, str]] = []
    for row in load_rows:
        ts = _tz(row["ts"])
        v = _fv(row)
        if v is None:
            continue
        result = tracker.update(v, ts)
        if result:
            new_zone, boundary_ts = result
            zone_events.append((boundary_ts, new_zone))

    # Определить зону на произвольный момент времени
    # (учитывает все ретроактивные границы)
    def zone_at(ts: datetime) -> str:
        z = initial_zone
        for evt_ts, new_z in zone_events:
            if evt_ts <= ts:
                z = new_z
            else:
                break
        return z

    # Fault-события — немедленные границы (без N_stab).
    # INFO-фолты (статусные биты без severity) подсегменты НЕ режут.
    fault_splits: list[tuple[datetime, str, str]] = []
    for fp in fault_periods_in_seg:
        mapped_sev = cfg.bitmap_severity(fp.get("severity"))
        if mapped_sev == "INFO":
            continue
        ft = _tz(fp["fault_start"])
        if t_start_tz < ft < t_end_tz:
            fault_splits.append((ft, "FAULT_START", zone_at(ft)))

    # Объединяем все точки разбивки: (ts, cause, zone_after_boundary)
    split_points: list[tuple[datetime, str, str]] = []
    for bt, new_zone in zone_events:
        split_points.append((bt, "LOAD_ZONE_CHANGE", new_zone))
    for ft, cause, zone in fault_splits:
        split_points.append((ft, cause, zone))

    split_points.sort(key=lambda x: x[0])

    # Строим интервалы подсегментов из split_points
    intervals: list[tuple[datetime, datetime, str, str, str | None]] = []
    prev_ts = t_start_tz
    prev_zone = initial_zone
    prev_cause_open = "SEG_START"

    for split_ts, cause, new_zone in split_points:
        if split_ts <= prev_ts:
            # Ретроактивная граница до/вровень с началом — только обновляем зону
            prev_zone = new_zone
            continue
        if split_ts >= t_end_tz:
            break
        intervals.append((prev_ts, split_ts, prev_zone, prev_cause_open, cause))
        prev_ts = split_ts
        prev_zone = new_zone
        prev_cause_open = cause

    # Финальный интервал
    if prev_ts < t_end_tz:
        intervals.append((prev_ts, t_end_tz, prev_zone, prev_cause_open,
                          "RPM_RUNDOWN" if tail_start < seg_t1 else None))

    if not intervals:
        intervals = [(t_start_tz, t_end_tz, initial_zone, "SEG_START", None)]
    if pre_subs:
        # Рабочая часть открывается выходом на рабочие обороты
        s0, e0, z0, _, c0 = intervals[0]
        intervals[0] = (s0, e0, z0, "RPM_RATED", c0)

    # Создаём подсегменты
    subsegments: list[Subsegment] = list(pre_subs)
    prev_sub_zone: str | None = None

    ts_index = _build_ts_index(seg_by_addr)

    for idx, (t_sub_start, t_sub_end, zone, cause_open, cause_close) in enumerate(intervals):
        subseg_id = f"{seg_id}.{idx + 1 + len(pre_subs)}"
        char_t_start = (preamble_t_start if idx == 0 and preamble_t_start else t_sub_start)

        sub_chars = _slice_by_addr(seg_by_addr, ts_index, char_t_start, t_sub_end)
        sub_win   = _slice_by_addr(seg_by_addr, ts_index, t_sub_start, t_sub_end)

        chars        = compute_characteristics(sub_chars, char_t_start, t_sub_end, cfg)
        chars.update(_state_characteristics(state_periods or {}, t_sub_start, t_sub_end, cfg))
        derived      = compute_derived_metrics(sub_win, t_sub_start, t_sub_end, gaps, cfg)
        accumulators = update_accumulators(accumulators, sub_win, t_sub_start, t_sub_end, zone, run_state, cfg, gaps)

        faults_in_sub = [
            fp for fp in fault_periods_in_seg
            if _tz(fp["fault_start"]) < t_sub_end
            and (_tz(fp["fault_end"]) if fp.get("fault_end") else t_sub_end) > t_sub_start
        ]
        detections = run_all_detectors(
            chars, derived, accumulators, faults_in_sub,
            zone, run_state, t_sub_start, t_sub_end, prev_sub_zone, cfg, state_periods,
            onset_faults=onset_faults if onset_faults is not None else fault_periods_in_seg,
        )

        sub_gaps = _filter_gaps(gaps, t_sub_start, t_sub_end)
        dq = _compute_data_quality(seg_by_addr, t_sub_start, t_sub_end, sub_gaps, cfg)

        subseg = Subsegment(
            id=subseg_id,
            parent_segment_id=seg_id,
            t_start=t_sub_start.isoformat(),
            t_end=t_sub_end.isoformat(),
            duration_sec=(t_sub_end - t_sub_start).total_seconds(),
            load_zone=zone,
            cause_open=cause_open,
            cause_close=cause_close,
            characteristics=chars,
            derived_metrics=derived,
            risk_accumulators=accumulators,
            detections=detections,
            data_quality=dq,
            data_gaps=_clip_gap_intervals(sub_gaps, t_sub_start, t_sub_end),
        )
        subsegments.append(subseg)
        prev_sub_zone = zone

    if tail_start < seg_t1:
        sub, accumulators = _build_single_subsegment(
            seg_id=seg_id, idx=len(subsegments) + 1, seg_by_addr=seg_by_addr,
            preamble_t_start=None, t_start=tail_start, t_end=seg_t1,
            load_zone="NA", cause_open="RPM_RUNDOWN", cause_close=None,
            run_state=run_state, gaps=_filter_gaps(gaps, tail_start, seg_t1),
            accumulators=accumulators, prev_zone=prev_sub_zone, cfg=cfg,
            fault_periods=fault_periods_in_seg, state_periods=state_periods,
            detect_run_state=0, transient=True, onset_faults=onset_faults,
        )
        subsegments.append(sub)

    return subsegments, accumulators


# ── Sequence checks ───────────────────────────────────────────────────────────

# ── Одиночный ноль источника (v5.1.17) ──────────────────────────────────────
# Декодер иногда пишет «нулевой кадр»: обороты и/или давление масла ровно 0
# одной точкой посреди работы, иногда вместе с током и мощностью. ДГУ №1 12.10
# 14:09:17 МСК: обороты 1499 → 0 → 1501 за 9 с при частоте 50 Гц; ДЭС №3 13.10
# 14:59:56: давление масла 300 → 0 → 316 при 1515 об/мин. За 30 дней — 18 таких
# точек на трёх машинах; каждая давала «обороты 0», «давление масла 0 у порога»
# и ложное падение давления масла. Решение владельца 13.10.2026: такой ноль —
# сбой источника, в аналитику не идёт. Настоящее падение так не спрячется:
# показание пишется по изменению, и настоящий ноль держится — следующее
# показание тоже не рабочее или приходит позже минуты, а панель меняет режим.

def _zero_glitch_params(cfg) -> dict[str, Any]:
    def g(key: str, default: Any) -> Any:
        try:
            v = cfg.seg("zero_glitch", key, default=default)
        except Exception:
            v = default
        return default if v is None else v

    roles: dict[str, float] = {}
    for role, thr in (g("roles", {"RPM": 1000, "OIL_PRESS": 100}) or {}).items():
        try:
            roles[str(role)] = float(thr)
        except (TypeError, ValueError):
            continue
    return {
        "enabled": bool(g("enabled", True)),
        "roles": roles,
        "max_gap_sec": float(g("max_gap_sec", 60.0)),
        "frame_tol_sec": float(g("frame_tol_sec", 1.0)),
        "stop_guard_sec": float(g("stop_guard_sec", 15.0)),
    }


def _drop_zero_glitches(
    by_addr: dict[int, list[dict]], cfg, tt: datetime,
    stops: list[tuple[datetime, datetime]] | None = None,
) -> tuple[dict[int, list[dict]], list[tuple[datetime, str]]]:
    """Убрать одиночные нули источника. (новый by_addr, [(момент, роль)]).

    Ноль роли из списка (обороты, давление масла) — сбой, если показания до и
    после него рабочие (выше порога роли) и между ними не больше max_gap_sec.
    Ноль на краю данных, после которого показаний ещё нет, — пока сбой: живое
    окно иначе завело бы эпизод «обороты 0» до следующего показания; если ноль
    настоящий, следующее показание (тоже ноль или «пульс» через минуту) его
    подтвердит. Остальные аналоговые нули того же кадра (ток, мощность) уходят
    вместе с ним — это один и тот же сбойный кадр.

    stops — периоды стопа по 40011: ноль внутри стопа или за stop_guard_sec до
    его начала — настоящий останов (панель сменила режим), его не трогаем.
    """
    prm = _zero_glitch_params(cfg)
    if not prm["enabled"] or not prm["roles"]:
        return by_addr, []
    tt = _tz(tt)
    gap = prm["max_gap_sec"]
    guard = prm["stop_guard_sec"]

    def _at_stop(ts: datetime) -> bool:
        return any(s - timedelta(seconds=guard) <= ts < e for s, e in stops or [])

    drop_ids: set[int] = set()
    found: list[tuple[datetime, str]] = []
    for role, thr in prm["roles"].items():
        addr = cfg.role_to_addr(role)
        rows = by_addr.get(addr) if addr else None
        if not rows:
            continue
        real = [r for r in rows if _fv(r) is not None]
        for k, r in enumerate(real):
            if _fv(r) != 0:
                continue
            prev = real[k - 1] if k > 0 else None
            if prev is None or (_fv(prev) or 0) <= thr:
                continue
            ts = _tz(r["ts"])
            if _at_stop(ts):
                continue
            nxt = real[k + 1] if k + 1 < len(real) else None
            if nxt is None:
                glitch = (tt - ts).total_seconds() < gap
            else:
                glitch = ((_fv(nxt) or 0) > thr
                          and (_tz(nxt["ts"]) - _tz(prev["ts"])).total_seconds() <= gap)
            if glitch:
                drop_ids.add(id(r))
                found.append((ts, role))
    if not found:
        return by_addr, []
    tol = prm["frame_tol_sec"]
    frames = sorted(t for t, _ in found)
    out: dict[int, list[dict]] = {}
    for addr, rows in by_addr.items():
        analog = cfg.register_map.get(addr, {}).get("kind") == "analog"
        keep = []
        for r in rows:
            if id(r) in drop_ids:
                continue
            if analog and _fv(r) == 0:
                ts = _tz(r["ts"])
                if any(abs((ts - t).total_seconds()) <= tol for t in frames):
                    continue
            keep.append(r)
        out[addr] = keep
    found.sort(key=lambda x: x[0])
    return out, found


def _glitch_check(glitches: list[tuple[datetime, str]], t0: datetime, t1: datetime) -> dict | None:
    """Проверка сегмента: сколько одиночных нулей источника отброшено и когда."""
    inside = [(t, r) for t, r in glitches if t0 <= t < t1]
    if not inside:
        return None
    ru = {"RPM": "обороты", "OIL_PRESS": "давление масла"}
    shown = ", ".join(f"{ru.get(r, r)} {t.strftime('%H:%M:%S')} UTC" for t, r in inside[:5])
    more = f" и ещё {len(inside) - 5}" if len(inside) > 5 else ""
    return {
        "check": "source_zero_glitch",
        "passed": True,
        "count": len(inside),
        "details": f"отброшены одиночные нули источника (сбой декодера): {shown}{more}",
    }


def _sequence_checks(
    run_state: int,
    subsegments: list[Subsegment],
    fault_periods: list[dict],
    cfg: AnalyticsConfig,
) -> list[dict[str, Any]]:
    """Внутрисегментные проверки (данные, активные fault).

    Межсегментные проверки (warmup/cooldown) выполняются отдельным проходом
    в _inter_segment_checks() после построения всего списка сегментов.
    """
    checks: list[dict[str, Any]] = []

    if run_state == 3:
        # Проверка: все подсегменты имеют данные
        empty_idx = [i for i, s in enumerate(subsegments) if s.data_quality == 0.0]
        details = "ok"
        if empty_idx:
            def _hms(iso: str) -> str:
                from datetime import datetime as _DT
                return _DT.fromisoformat(iso).strftime("%H:%M:%S")

            # Группируем подряд идущие пустые подсегменты в непрерывные интервалы
            runs: list[list[int]] = []
            for i in empty_idx:
                if runs and i == runs[-1][-1] + 1:
                    runs[-1].append(i)
                else:
                    runs.append([i])

            # Окна нулевой длины (подсегмент 06:00:00–06:00:00 на резе) — не обрыв
            runs = [r for r in runs if subsegments[r[-1]].t_end != subsegments[r[0]].t_start]
            ranges = [
                f"{_hms(subsegments[run[0]].t_start)}–{_hms(subsegments[run[-1]].t_end)}"
                for run in runs
            ]
            details = ("отсутствие связи/данных: " + ", ".join(ranges)) if ranges else "ok"
        else:
            runs = []

        checks.append({
            "check": "subseg_data_coverage",
            "passed": not runs,
            "details": details,        # UTC-текст — для старых читателей
            # Окна структурно: сводка и отчёт печатают их в местном времени
            "ranges": [{"start": subsegments[r[0]].t_start, "end": subsegments[r[-1]].t_end}
                       for r in runs],
        })

    # Проверка: нет активных аварийных масок на конце сегмента. Наблюдение, на
    # вердикт не влияет — аварию несёт сама панель. Сырые уровни масок в нижнем
    # регистре ('shutdown') — до v5.1.7 сравнение с "SHUTDOWN" не срабатывало никогда
    _seg_end = _tz(datetime.fromisoformat(subsegments[-1].t_end)) \
        if subsegments and subsegments[-1].t_end else None

    def _active_at_end(fp: dict) -> bool:
        if _seg_end is None:
            return fp.get("fault_end") is None
        fs, fe = fp.get("fault_start"), fp.get("fault_end")
        fs = _tz(datetime.fromisoformat(fs)) if isinstance(fs, str) else (_tz(fs) if fs else None)
        fe = _tz(datetime.fromisoformat(fe)) if isinstance(fe, str) else (_tz(fe) if fe else None)
        # Снятая ровно на конце (рез SHUTDOWN_CLEARED) — уже не активна
        return (fs is None or fs <= _seg_end) and (fe is None or fe > _seg_end)

    shutdown_faults = [
        fp for fp in fault_periods
        if cfg.bitmap_severity(fp.get("severity") or "") == "SHUTDOWN" and _active_at_end(fp)
    ]
    checks.append({
        "check": "no_active_shutdown_fault",
        "passed": len(shutdown_faults) == 0,
        "details": (
            "на конце сегмента активна аварийная маска панели: "
            + ", ".join(fp.get("fault_name_ru") or fp.get("fault_name") or "?"
                        for fp in shutdown_faults)
            if shutdown_faults else "ok"
        ),
    })

    return checks


# ── Межсегментные проверки ────────────────────────────────────────────────────

def _check_stop_profile(
    stop_seg: Segment,
    all_by_addr: dict[int, list[dict]],
    cfg: AnalyticsConfig,
) -> None:
    """Профиль останова: за сколько обороты упали до rpm_stopped_threshold.

    Только наблюдение (проверка rpm_stop_profile), без детекции и на вердикт не
    влияет: отказ останова — авария самой панели, аналитика её не дублирует
    (решение владельца 11.10.2026). У PCC3300 это код 1245 «Неисправность при
    останове двигателя» (SHUTDOWN): контроллер снял питание с клапана отсечки, а
    вращение продолжается — маска 40405/15 (ДЭС №3 16.09: оба раза вместе с
    40012 = 1245). Бит 40407/3 FailtoShutdown — другое: код 1416, режим Battle Short.
    До v5.1.7 здесь была детекция STOP_PROFILE уровня WARNING.
    """
    from datetime import datetime as _DT
    from datetime import timedelta as _TD

    # Только голова стопа — сегмент открыт сменой режима. На суточном резе
    # (REPORT_START) и резах по аварии машина уже стоит, и «время останова»
    # выходило временем до первого замера RPM (35761: «замедленный 37 с»;
    # 35641: ложный STOP_PROFILE WARNING на 6 ч 45 мин) — v5.1.6
    if stop_seg.cause_open != "RUN_STATE_CHANGE":
        return

    rpm_addr = cfg.role_to_addr("RPM")
    if rpm_addr is None:
        return

    t0 = _tz(_DT.fromisoformat(stop_seg.t_start))
    t_end_raw = stop_seg.t_end
    if t_end_raw is None:
        return
    t1 = _tz(_DT.fromisoformat(t_end_raw))

    T_stop_max = float(cfg.seg("transitions", "T_stop_max_sec", default=30.0))
    rpm_thr = float(cfg.seg("transitions", "rpm_stopped_threshold", default=50.0))
    # «Не упали» — только после горизонта с запасом на медленный останов (самый
    # медленный штатный за 60 дней — 74 с): иначе открытое окно на 30–60-й
    # секунде давало ложное «НАРУШЕНИЕ» (эпизоды 13516, 14768, 14786)
    T_violation = float(cfg.seg("transitions", "T_stop_violation_sec", default=90.0))

    # RPM-ряд (только реальные измерения)
    rpm_all = sorted(
        (
            (_tz(r["ts"]), float(r["value"]))
            for r in all_by_addr.get(rpm_addr, [])
            if r.get("value") is not None and not r.get("is_carried_forward", False)
        ),
        key=lambda x: x[0],
    )
    # Голова, обрезанная резом стопа (авария снята, суточная граница), — ряд и после
    # реза: короткая голова иначе не получала вердикта никогда (отказ отсечки с
    # E-Stop в первые 30 с). При смене режима (пуск) — только свой сегмент
    t_eval = t1
    if stop_seg.cause_close not in (None, "RUN_STATE_CHANGE"):
        t_eval = max(t1, t0 + _TD(seconds=T_violation))
    rpm_rows = [(ts, v) for ts, v in rpm_all if t0 <= ts < t_eval]
    if t_eval > t1:
        # Первый замер на горизонте (не позже минуты за ним — не через обрыв связи)
        nxt = next(((ts, v) for ts, v in rpm_all if ts >= t_eval), None)
        if nxt and nxt[0] <= t_eval + _TD(seconds=60):
            rpm_rows.append(nxt)

    if not rpm_rows:
        return  # нет данных RPM — не можем проверить

    # «Из работы»: двигатель вращался в момент смены режима — последний замер до
    # t0 (история грузится с запасом), иначе первый в окне. Не вращался — проверять нечего
    before = [v for ts, v in rpm_all if t0 - _TD(seconds=300) <= ts < t0]
    if (before[-1] if before else rpm_rows[0][1]) < rpm_thr:
        return

    seg_dur = (t1 - t0).total_seconds()

    # Найти первый момент, когда RPM упали ниже порога
    stop_time: float | None = None
    for ts, v in rpm_rows:
        if v < rpm_thr:
            stop_time = (ts - t0).total_seconds()
            break

    if stop_time is not None:
        # RPM упали — проверяем время
        if stop_time <= T_stop_max:
            stop_seg.sequence_checks.append({
                "check": "rpm_stop_profile",
                "passed": True,
                "stop_time_sec": stop_time,
                "T_stop_max_sec": T_stop_max,
                "details": f"ok: RPM упали до порога за {stop_time:.0f}с <= {T_stop_max:.0f}с",
            })
        else:
            # Медленный останов — просто информируем, не WARNING
            stop_seg.sequence_checks.append({
                "check": "rpm_stop_profile",
                "passed": True,
                "stop_time_sec": stop_time,
                "T_stop_max_sec": T_stop_max,
                "details": f"замедленный останов: RPM упали за {stop_time:.0f}с > {T_stop_max:.0f}с",
            })
    else:
        # Судить «не остановился» можно, только когда норматив истёк ПО ДАННЫМ: в
        # первом цикле открытого стопа окно короче (35604: 709→545→120 за 20 с —
        # «НАРУШЕНИЕ», а остановился за 31 с) — v5.1.6
        if (rpm_rows[-1][0] - t0).total_seconds() < T_violation:
            return
        # RPM так и не упали — наблюдение; аварию «отказ останова» (1245) выставляет панель
        rpm_min = min(v for _, v in rpm_rows)
        obs = (rpm_rows[-1][0] - t0).total_seconds()
        stop_seg.sequence_checks.append({
            "check": "rpm_stop_profile",
            "passed": False,
            "stop_time_sec": None,
            "rpm_min_observed": rpm_min,
            "seg_duration_sec": seg_dur,
            "T_stop_max_sec": T_stop_max,
            "details": (
                f"обороты не упали ниже {rpm_thr:.0f} об/мин за {obs:.0f} с после перехода "
                f"в «Стоп» (минимум {rpm_min:.0f} об/мин); на вердикт не влияет — "
                f"отказ останова выставляет панель (код 1245)"
            ),
        })


def _inter_segment_checks(
    segments: list[Segment],
    all_by_addr: dict[int, list[dict]],
    cfg: AnalyticsConfig,
    state_periods: dict[int, list[dict]] | None = None,
    transition_ctx: list | None = None,
    ts_from: datetime | None = None,
    load_seed: dict | None = None,
    gaps: list[dict] | None = None,
) -> None:
    """Второй проход по готовому списку сегментов: проверяет переходы между ними.

    Мутирует sequence_checks и detections сегментов/подсегментов на месте.
    Вызывается после основного цикла в segment(), перед возвратом результата.
    all_by_addr нужен для профиля останова и нагрузки перед остановом (сырые ряды
    с преамбулой до окна). transition_ctx — закрытые сегменты перед окном
    (analytics.transitions, v5.1.10): без них голова окна не видела, из чего
    пришла, и нарушение перехода жило один цикл.
    """
    from .contract import Detection
    from .transitions import ctx_for_window, link_from_segment

    warmup_en = bool(cfg.det("WARMUP_VIOLATION", "enabled", default=True))
    cooldown_en = bool(cfg.det("COOLDOWN_VIOLATION", "enabled", default=True))
    stop_profile_en = bool(cfg.det("STOP_PROFILE", "enabled", default=True))
    tol = float(cfg.seg("transitions", "joint_tol_sec", default=2.0))

    ctx = ctx_for_window(transition_ctx, ts_from, tol) if transition_ctx else []
    window_links = [link_from_segment(s) for s in segments]

    for i, seg in enumerate(segments):
        prev_links = ctx + [lk for lk in window_links[:i] if lk is not None]

        # Прогрев перед работой — у головы работы. Неподтверждённый выход из
        # стопа ещё не пуск: оценивать нечего (v5.1.16)
        if seg.run_state == 3 and warmup_en and not getattr(seg, "attempt_pending", False):
            _check_warmup_transition(seg, prev_links, cfg, Detection)

        # Охлаждение перед остановом — у головы стопа после работы
        if seg.run_state == 0 and cooldown_en:
            _check_cooldown_transition(seg, prev_links, cfg, Detection, all_by_addr,
                                       load_seed=load_seed, gaps=gaps)

        # Профиль останова — наблюдение, без детекции (v5.1.7)
        if seg.run_state == 0 and stop_profile_en:
            _check_stop_profile(seg, all_by_addr, cfg)

        # Коды панели у добавленных детекций (COOLDOWN 611 и т.п.) — только выставленные
        if seg.subsegments:
            from .detectors import confirm_panel_codes, panel_codes_in
            lfc = (state_periods or {}).get(cfg.role_to_addr("LAST_FAULT_CODE")) or []
            for sub in seg.subsegments:
                confirm_panel_codes(sub.detections, panel_codes_in(lfc, sub.t_start, sub.t_end))


def _as_links(prev) -> list:
    """Звенья из списка Segment (старые вызовы) или уже звеньев."""
    from .transitions import Link, link_from_segment
    out = []
    for x in prev or []:
        lk = x if isinstance(x, Link) else link_from_segment(x)
        if lk is not None:
            out.append(lk)
    return out


def _check_warmup_transition(
    running_seg: Segment,
    prev: list,
    cfg: AnalyticsConfig,
    Detection: type,
) -> None:
    """Прогрев перед работой: только у головы работы (смена режима), по цепочке
    от ближайшего стопа (v5.1.10).

    Раньше брался «последний RS=2 в окне»: второй пуск в окне суток заимствовал
    прогрев первого («прогрев 300 с ok»), а у продолжений после суточного реза
    писалось «RUN_STATE=2 не обнаружен». Пуск без фазы прогрева — норма парка
    (настройка панели): тёплый — запись без замечания; холодный (ОЖ ниже порога)
    — первый сезон наблюдением (решение 12.10). Короткий прогрев при холодном
    пуске — разовое событие WARMUP_VIOLATION.
    """
    from .transitions import chain_before

    if running_seg.cause_open != "RUN_STATE_CHANGE":
        return
    min_warmup = float(cfg.det("WARMUP_VIOLATION", "min_warmup_sec", default=180.0))
    hot_warmup = float(cfg.det("WARMUP_VIOLATION", "hot_start_warmup_sec", default=0.0))
    cold_thr = float(cfg.det("WARMUP_VIOLATION", "cold_start_coolant_c", default=21.0))
    tol = float(cfg.seg("transitions", "joint_tol_sec", default=2.0))
    max_links = int(cfg.seg("transitions", "chain_max_links", default=8))

    ch = chain_before(_as_links(prev), running_seg.t_start, tol_sec=tol,
                      max_links=max_links, allow_start_sequence=True)
    if ch is None or (not ch.synthetic_anchor and ch.anchor.run_state != 0):
        return  # нет пуска (работа после работы, цепочка рвётся) — нечего оценивать
    seq = ((ch.anchor,) if ch.synthetic_anchor else ()) + ch.between
    warm = [lk for lk in seq if lk.run_state == 2]

    coolant_start = warm[0].coolant_start if warm else None
    if coolant_start is None and running_seg.subsegments:
        coolant_start = running_seg.subsegments[0].characteristics.get(
            "COOLANT_TEMP", {}).get("value_start")
    cold_start = coolant_start is not None and coolant_start < cold_thr

    running_seg.sequence_checks.append({
        "check": "cold_start_context",
        "passed": True,
        "cold_start": cold_start,
        "coolant_at_start_c": coolant_start,
        "details": (
            f"{'холодный' if cold_start else 'тёплый'} пуск, Т ОЖ {coolant_start:.0f} °C"
            if coolant_start is not None else "Т ОЖ на пуске неизвестна"
        ),
    })

    if not warm:
        running_seg.sequence_checks.append({
            "check": "warmup_presence",
            "passed": True,
            "cold_start": cold_start,
            "details": "пуск без фазы прогрева",
        })
        if cold_start:
            # Первый сезон — наблюдение без замечания (решение 12.10)
            running_seg.sequence_checks.append({
                "check": "cold_start_no_warmup",
                "passed": False,
                "coolant_at_start_c": coolant_start,
                "details": (f"холодный пуск без фазы прогрева: Т ОЖ {coolant_start:.0f} °C "
                            f"(ниже {cold_thr:.0f} °C) — наблюдение"),
            })
        return

    required = min_warmup if cold_start else hot_warmup
    warmup_dur = sum(lk.duration_sec for lk in warm)
    passed = warmup_dur >= required
    running_seg.sequence_checks.append({
        "check": "warmup_duration",
        "passed": passed,
        "warmup_duration_sec": warmup_dur,
        "required_sec": required,
        "cold_start": cold_start,
        "details": (
            f"ok: прогрев {warmup_dur:.0f} с ≥ {required:.0f} с"
            if passed else
            f"НАРУШЕНИЕ: прогрев {warmup_dur:.0f} с < {required:.0f} с при "
            f"{'холодном' if cold_start else 'тёплом'} пуске"
            + (f" (Т ОЖ {coolant_start:.0f} °C)" if coolant_start is not None else "")
        ),
    })

    if not passed and running_seg.subsegments:
        running_seg.subsegments[0].detections.append(Detection(
            scenario="WARMUP_VIOLATION",
            severity=cfg.det("WARMUP_VIOLATION", "severity_default", default="CAUTION"),
            t_detected=running_seg.t_start,
            source="PASSPORT_THRESHOLD",
            trigger=(
                f"Пуск с прогревом {warmup_dur:.0f} с при норме {required:.0f} с "
                f"({'холодный' if cold_start else 'тёплый'} пуск"
                + (f", Т ОЖ {coolant_start:.0f} °C" if coolant_start is not None else "") + ")"
            ),
            related_roles=["COOLANT_TEMP"],
            fault_codes=[],
            description_key="WARMUP_VIOLATION.insufficient_warmup",
            values={
                "warmup_duration_sec": warmup_dur,
                "required_sec": required,
                "cold_start": cold_start,
                "coolant_at_start_c": coolant_start,
                "cold_threshold_c": cold_thr,
            },
        ))


def _cooled_since_load(
    stop_t0: datetime,
    all_by_addr: dict[int, list[dict]] | None,
    cfg: AnalyticsConfig,
    thr: float,
    load_seed: dict | None = None,
    gaps: list[dict] | None = None,
) -> tuple[float | None, float | None, bool]:
    """Сколько секунд перед остановом двигатель был без нагрузки ≥ thr (ряд LOAD_PCT).

    Ряд пишется по изменению (при постоянном значении — «пульс» ~15 мин):
    значение держится до следующего замера. Берутся все реальные замеры до
    останова плюс load_seed — последний замер перед окном истории (у головы
    окна преамбула всего 300 с). Возвращает (секунды, последняя нагрузка ≥ thr,
    обрыв связи): обрыв — если на отрезке, от которого зависит вывод (от
    последнего замера ≥ thr или от первого известного значения до останова),
    была дыра — тогда что было в ней, неизвестно. (None, None, False) — ряда нет.
    """
    addr = cfg.role_to_addr("LOAD_PCT")
    if addr is None:
        return None, None, False
    pts = sorted(
        (_tz(r["ts"]), float(r["value"])) for r in (all_by_addr or {}).get(addr, [])
        if r.get("value") is not None and not r.get("is_carried_forward", False)
        and _tz(r["ts"]) < stop_t0
    )
    if load_seed and load_seed.get("ts") is not None and load_seed.get("value") is not None:
        st = _tz(load_seed["ts"]) if not isinstance(load_seed["ts"], str) \
            else _tz(datetime.fromisoformat(load_seed["ts"]))
        if st < stop_t0 and (not pts or st < pts[0][0]):
            pts.insert(0, (st, float(load_seed["value"])))
    if not pts:
        return None, None, False
    hi = [k for k, (_, v) in enumerate(pts) if v >= thr]
    if hi:
        k = hi[-1]
        end_hi = pts[k + 1][0] if k + 1 < len(pts) else stop_t0
        cooled, last, span0 = (stop_t0 - end_hi).total_seconds(), pts[k][1], pts[k][0]
    else:
        cooled, last, span0 = (stop_t0 - pts[0][0]).total_seconds(), None, pts[0][0]
    blind = False
    for g in gaps or []:
        gs = g.get("gap_start")
        ge = g.get("gap_end")
        gs = _tz(datetime.fromisoformat(gs)) if isinstance(gs, str) else (_tz(gs) if gs else None)
        ge = _tz(datetime.fromisoformat(ge)) if isinstance(ge, str) else (_tz(ge) if ge else None)
        if gs is not None and gs < stop_t0 and (ge is None or ge > span0):
            blind = True
            break
    return cooled, last, blind


def _check_cooldown_transition(
    stop_seg: Segment,
    prev: list,
    cfg: AnalyticsConfig,
    Detection: type,
    all_by_addr: dict[int, list[dict]] | None = None,
    load_seed: dict | None = None,
    gaps: list[dict] | None = None,
) -> None:
    """Охлаждение перед остановом (v5.1.10, решения владельца 12.10).

    Нарушение — если от последней нагрузки ≥ load_pct_threshold (30 %: ниже —
    недогруз) до останова прошло меньше min_cooldown_sec (3 мин, Cummins 3–5).
    Режим докрутки без нагрузки не важен: фаза охлаждения 4/5, холостой ход,
    снятая нагрузка на номинале — так же считает и панель (43800 Rated Cooldown
    Time — время на номинале при нагрузке < 10 %). Часто вместо штатного
    расхолаживания просто снимают нагрузку — это охлаждение.

    Только у головы стопа после работы (цепочка до работы через переходные
    режимы); работа без нагрузки ≥ порога — охлаждать нечего. Нет данных о
    нагрузке — «не оценено» (наблюдение, без замечания).
    """
    from .transitions import chain_before

    if stop_seg.cause_open != "RUN_STATE_CHANGE":
        return
    thr = float(cfg.det("COOLDOWN_VIOLATION", "load_pct_threshold", default=30.0))
    min_cool = float(cfg.det("COOLDOWN_VIOLATION", "min_cooldown_sec", default=180.0))
    tol = float(cfg.seg("transitions", "joint_tol_sec", default=2.0))
    max_links = int(cfg.seg("transitions", "chain_max_links", default=8))

    ch = chain_before(_as_links(prev), stop_seg.t_start, tol_sec=tol, max_links=max_links)
    if ch is None or ch.anchor.run_state != 3:
        return  # не после работы (или цепочка рвётся): охлаждать нечего / не видно

    t0 = _tz(datetime.fromisoformat(stop_seg.t_start))
    phase_sec = sum(lk.duration_sec for lk in ch.between if lk.run_state in (4, 5))
    path = "→".join(str(lk.run_state) for lk in (ch.anchor,) + ch.between) + "→0"
    loads = [lk.load_pct_max for lk in ch.run_pieces if lk.load_pct_max is not None]
    run_max = max(loads) if loads else None

    has_shutdown_fault = any(
        e.get("type") == "FAULT"
        and e.get("severity") in ("shutdown", "SHUTDOWN", "shutdown_cooldown")
        for e in stop_seg.events
    )

    base = {
        "check": "cooldown_after_elevated",
        "required_sec": min_cool,
        "load_threshold_pct": thr,
        "phase_sec": phase_sec,
        "path": path,
        "run_load_max_pct": run_max,
    }
    if run_max is not None and run_max < thr:
        stop_seg.sequence_checks.append(dict(
            base, passed=True, cooled_sec=None,
            details=(f"работа без нагрузки ≥ {thr:.0f} % (макс. {run_max:.0f} %) — "
                     f"охлаждение не требуется"),
        ))
        return

    _gaps = list(gaps or []) + list((load_seed or {}).get("gaps") or [])
    cooled, last_load, blind = _cooled_since_load(t0, all_by_addr, cfg, thr, load_seed, _gaps)
    if (cooled is None or blind) and phase_sec >= min_cool:
        cooled, blind = phase_sec, False   # фаза охлаждения видна целиком
    if cooled is None or blind:
        stop_seg.sequence_checks.append({
            "check": "cooldown_not_evaluated",
            "passed": False,
            "path": path,
            "details": ("охлаждение перед остановом не оценено — "
                        + ("обрыв связи перед остановом" if blind
                           else "нет данных о нагрузке перед остановом")),
        })
        return

    passed = cooled >= min_cool
    stop_seg.sequence_checks.append(dict(
        base, passed=passed, cooled_sec=cooled,
        details=(
            f"ok: без нагрузки ≥ {thr:.0f} % перед остановом {cooled:.0f} с ≥ {min_cool:.0f} с"
            if passed else
            f"НАРУШЕНИЕ: останов через {cooled:.0f} с после работы под нагрузкой"
            + (f" {last_load:.0f} %" if last_load is not None else "")
            + f" — охлаждение меньше {min_cool:.0f} с"
        ),
    ))

    if not passed and stop_seg.subsegments:
        prefix = "АВАРИЙНЫЙ ОСТАНОВ — " if has_shutdown_fault else ""
        stop_seg.subsegments[0].detections.append(Detection(
            scenario="COOLDOWN_VIOLATION",
            severity=cfg.det("COOLDOWN_VIOLATION", "severity_default", default="CAUTION"),
            t_detected=stop_seg.t_start,
            source="PASSPORT_THRESHOLD",
            trigger=(
                (f"{prefix}Останов прямо под нагрузкой"
                 + (f" {last_load:.0f} %" if last_load is not None else "")
                 + " — без охлаждения" if cooled < 1 else
                 f"{prefix}Останов через {cooled:.0f} с после работы под нагрузкой"
                 + (f" {last_load:.0f} %" if last_load is not None else "")
                 + f" — охлаждение меньше {min_cool / 60:.0f} мин")
                + ", риск для турбины"
            ),
            related_roles=["LOAD_PCT", "RPM"],
            fault_codes=[611],
            description_key="COOLDOWN_VIOLATION.no_cooldown_after_load",
            values={
                "cooled_sec": cooled,
                "required_sec": min_cool,
                "load_threshold_pct": thr,
                "last_load_pct": last_load,
                "phase_sec": phase_sec,
                "path": path,
                "emergency_stop": has_shutdown_fault,
            },
        ))


# ── Подтверждение выхода из стопа (v5.1.16) ─────────────────────────────────
# Регистр 40011 — команда и последовательность панели, а не то, что физически
# с машиной: по команде пуска панель сразу пишет «Прогрев» или «Номинал», хотя
# коленвал ещё не провернулся. ДЭС №3 13.10.2026: четыре попытки за 5 минут
# (обороты 0, трижды 1438 «Отказ прокрутки») стали «Прогревом» и тремя
# «Работами» с ложными «давление масла 0» и «обороты 0»; ДГУ №2 20.09 —
# четыре зелёных «Прогрева» при прокрутке стартером ~100 об/мин.
#
# Решения владельца 13.10.2026 (docs/plans/design_v5116_transition_confirm.md):
#   - выход из стопа (1/2/3 после 0) подтверждается фактом — любым из трёх
#     независимых: обороты выше стартера (> 250), частота генератора, бит
#     «Готов к нагрузке»; механика, электрика и панель страхуют друг друга;
#   - не подтвердился и вернулся в стоп — ОДНА плашка с устойчивым состоянием
#     исхода: «Неудачная работа · Авария» / «Неудачная работа · Стоп»;
#     правило «один сегмент — одно состояние» не нарушено: изменённое
#     состояние не подтвердилось, показываем устойчивое;
#   - охлаждение (4/5) не трогаем: машина, вставшая при входе в него, упала
#     с работы — это ведёт логика аварийного стопа.

def confirm_params(cfg) -> dict[str, Any]:
    """Пороги подтверждения (KB: segmentation.yaml → transition_confirm).

    Рабочие обороты — тот же порог, что у паспортной проверки масла
    (OIL_DILUTION.passport_min_rpm): один источник.
    """
    def g(key: str, default: Any) -> Any:
        try:
            v = cfg.seg("transition_confirm", key, default=default)
        except Exception:
            v = default
        return default if v is None else v

    bits: set[tuple[int, int]] = set()
    for b in g("ready_bits", []) or []:
        try:
            bits.add((int(b["addr"]), int(b["bit"])) if isinstance(b, dict)
                     else (int(b[0]), int(b[1])))
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    try:
        rated = cfg.det("OIL_DILUTION", "passport_min_rpm", default=1400.0)
    except Exception:
        rated = 1400.0
    return {
        "enabled": bool(g("enabled", True)),
        "rpm_min": float(g("rpm_min", 250.0)),
        "freq_min": float(g("freq_min_hz", 5.0)),
        "ready_bits": frozenset(bits),
        "carry_sec": float(g("carry_sec", 120.0)),
        "rated_rpm": float(g("rated_rpm_min", rated if rated is not None else 1400.0)),
        "rated_freq": float(g("rated_freq_min_hz", 46.5)),
        "rundown_max_sec": float(g("rundown_max_sec", 30.0)),
        "no_data_share": float(g("no_data_share", 0.5)),
        "anchor_check_sec": float(g("anchor_check_sec", 900.0)),
    }


def value_series(by_addr: dict[int, list[dict]], addr: int | None) -> list[tuple[datetime, float]]:
    """Реальные показания регистра (ts, value) по времени — без forward-fill."""
    if not addr:
        return []
    out = []
    for r in by_addr.get(addr) or []:
        if r.get("is_carried_forward"):
            continue
        v = _fv(r)
        if v is not None:
            out.append((_tz(r["ts"]), v))
    out.sort(key=lambda x: x[0])
    return out


def ready_intervals(fault_periods: list[dict], prm: dict, tt: datetime) -> list[tuple[datetime, datetime]]:
    """Периоды бита «Готов к нагрузке» (адреса — у контроллера в KB)."""
    if not prm["ready_bits"]:
        return []
    out = []
    for fp in fault_periods or []:
        try:
            key = (int(fp.get("addr")), int(fp.get("bit")))
        except (TypeError, ValueError):
            continue
        if key not in prm["ready_bits"]:
            continue
        s = _tz(fp["fault_start"])
        e = _tz(fp["fault_end"]) if fp.get("fault_end") else _tz(tt)
        if e > s:
            out.append((s, e))
    return _merge_intervals(out)


def confirm_exit(
    t0: datetime, t1: datetime,
    rpm: list[tuple[datetime, float]], freq: list[tuple[datetime, float]],
    ready: list[tuple[datetime, datetime]], gaps: list[dict] | None, prm: dict,
) -> tuple[str, datetime | None, dict[str, Any]]:
    """Подтвердился ли режим на [t0, t1): ('confirmed', момент, факты),
    ('unconfirmed', None, факты) или ('no_data', None, факты).

    Показание пишется по изменению: если в коротком отрезке строк нет, значение
    держится с последнего показания до t0 (не старше carry_sec). Без единого
    показания оборотов и частоты, или когда отрезок больше чем наполовину без
    связи, исход не объявляется (решение 13.10: «нет данных» — не неудача).
    """
    t0, t1 = _tz(t0), _tz(t1)
    facts: dict[str, Any] = {"rpm_max": None, "freq_max": None, "ready": False}
    hits: list[datetime] = []
    evidence = False
    for series, thr, key in ((rpm, prm["rpm_min"], "rpm_max"),
                             (freq, prm["freq_min"], "freq_max")):
        vals: list[float] = []
        carried = None
        first_hit = None
        for ts, v in series:
            if ts < t0:
                carried = v if (t0 - ts).total_seconds() <= prm["carry_sec"] else None
                continue
            if ts >= t1:
                break
            vals.append(v)
            if first_hit is None and v > thr:
                first_hit = ts
        if carried is not None:
            vals.append(carried)
            if carried > thr:
                first_hit = t0
        if vals:
            evidence = True
            facts[key] = max(vals)
        if first_hit is not None:
            hits.append(first_hit)
    for s, e in ready:
        if s < t1 and e > t0:
            facts["ready"] = True
            hits.append(max(s, t0))
    if hits:
        return "confirmed", min(hits), facts
    dur = (t1 - t0).total_seconds()
    spans = []
    for g in gaps or []:
        gs = max(_tz(g["gap_start"]), t0)
        ge_raw = g.get("gap_end")
        ge = min(_tz(ge_raw) if ge_raw else t1, t1)
        if ge > gs:
            spans.append((gs, ge))
    blind = union_seconds(spans)
    if not evidence or (dur > 0 and blind / dur >= prm["no_data_share"]):
        return "no_data", None, facts
    return "unconfirmed", None, facts


def _attempt_codes(code_periods: list[dict], t0: datetime, t_fall: datetime,
                   lag_sec: float) -> list[int]:
    """Коды 40012, которые панель выставила на попытке и в лаг после падения.

    Не агрегат плашки: та тянется весь стоп после попытки, и в неё попадали бы
    и код, висевший до попытки (1248 после сброса), и кнопка через час.
    """
    out: list[int] = []
    hi = t_fall + timedelta(seconds=lag_sec)
    for p in code_periods or []:
        s = _tz(p["state_start"])
        if s < t0 or s > hi:
            continue
        try:
            v = int(p.get("value") or 0)
        except (TypeError, ValueError):
            continue
        if v > 0 and v != 65535 and v not in out:
            out.append(v)
    return out


def _attempt_record(chain: list[dict], stop: dict, res: list[tuple],
                    code_periods: list[dict] | None = None,
                    lag_sec: float = 60.0) -> dict[str, Any]:
    """Что было на неудачной попытке — для подписи, отчёта и акта."""
    path = [int(c["value"]) for c in chain]
    t0 = _tz(chain[0]["state_start"])
    t_fall = _tz(stop["state_start"])

    def _mx(key):
        vals = [r[2].get(key) for r in res if r[2].get(key) is not None]
        return max(vals) if vals else None

    return {
        "from_state": 3 if 3 in path else (2 if 2 in path else path[-1]),
        "path": path,
        "t_start": t0.isoformat(),
        "t_fall": t_fall.isoformat(),
        "outcome": "EMERGENCY" if stop.get("stop_kind") == "EMERGENCY" else "SIMPLE",
        "duration_sec": max(0.0, (t_fall - t0).total_seconds()),
        "facts": {
            "rpm_max": _mx("rpm_max"),
            "freq_max": _mx("freq_max"),
            "ready": any(r[2].get("ready") for r in res),
        },
        "codes": _attempt_codes(code_periods or [], t0, t_fall, lag_sec),
    }


def _confirm_attempts(
    periods: list[dict],
    all_by_addr: dict[int, list[dict]],
    fault_periods: list[dict],
    gaps: list[dict] | None,
    cfg,
    tt: datetime,
    prev_value: int | None = None,
    confirm_to: datetime | None = None,
    code_periods: list[dict] | None = None,
    carry_glue: bool = False,
) -> list[dict]:
    """Выходы из стопа без подтверждения — в плашку стопа-исхода.

    Цепочка — подряд идущие нестоповые периоды после стопа (prev_value — режим
    перед окном, если цепочка начинается на его краю). Задержка пуска (1) в
    цепочку не входит и остаётся своим сегментом, как до v5.1.16: это таймер, а
    не вращение, и на ней работает START_FAILURE; выход из стопа считается и
    через неё. Подтвердился хоть один — неподтверждённое перед ним клеится к
    нему (короткий кусок прогрева в ручном пуске — часть пуска). Не
    подтвердился ни один:
      - за цепочкой стоп — один период стопа с началом на попытке, видом стопа
        и его резом, плюс attempt;
      - цепочка ещё идёт — ОДИН открытый период от её начала с attempt_pending:
        ни одно звено не закрывается, пока исход не решён, иначе живой движок
        закрыл бы первое звено навсегда («ждём подтверждения») и потерял бы,
        что следующее — выход из стопа (ревью v5.1.16); работы нет, суточный
        рез переносится;
      - нет данных — оставить как есть, неудачу не объявлять.
    confirm_to — докуда смотреть за край окна, решая исход (суточный рез:
    попытка за минуту до 09:00 решается после него); сегменты режутся по tt.
    carry_glue — прошлое окно закрылось «Работой», склеенной с куском прогрева,
    который продолжается за рез: здесь он снова клеится к своей «Работе»
    (иначе в начале суток вылезал «Прогрев» между двумя «Работами»).
    Вызывается после _classify_stop_periods: вид стопа решается по началу
    стопа (там панель держит аварию), а не по началу попытки.
    """
    prm = confirm_params(cfg)
    if not prm["enabled"] or not periods:
        return periods
    tt = _tz(tt)
    t_res = max(tt, _tz(confirm_to)) if confirm_to else tt
    rpm = value_series(all_by_addr, cfg.role_to_addr("RPM"))
    freq = value_series(all_by_addr, cfg.role_to_addr("FREQUENCY"))
    ready = ready_intervals(fault_periods, prm, t_res)
    try:
        lag = float(cfg.seg("stop_kind", "stabilization_sec", default=60) or 60)
    except Exception:
        lag = 60.0

    out: list[dict] = []
    i, n = 0, len(periods)
    last = prev_value          # режим до текущего, без задержки пуска
    if carry_glue and n and int(periods[0]["value"]) not in (0, 1, 3):
        k = 0
        while k < n and int(periods[k]["value"]) not in (0, 1, 3):
            k += 1
        if k < n and int(periods[k]["value"]) == 3:
            head = dict(periods[k])
            head["state_start"] = periods[0]["state_start"]
            head.pop("cause_open", None)
            head["glued_from"] = [int(c["value"]) for c in periods[:k]]
            out.append(head)
            last = 3
            i = k + 1
    while i < n:
        p = periods[i]
        v = int(p["value"])
        if v == 1 or v == 0 or last != 0:
            out.append(p)
            if v != 1:
                last = v
            i += 1
            continue
        j = i
        while j < n and int(periods[j]["value"]) not in (0, 1):
            j += 1
        chain = periods[i:j]
        res = []
        for c in chain:
            c0 = _tz(c["state_start"])
            c1 = min(_tz(c["state_end"]), t_res) if c.get("state_end") else t_res
            res.append(confirm_exit(c0, c1, rpm, freq, ready, gaps, prm))

        first = next((k for k, r in enumerate(res) if r[0] == "confirmed"), None)
        if first is not None:
            head = dict(chain[first])
            head["confirmed_at"] = res[first][1]
            if first > 0:
                head["state_start"] = chain[0]["state_start"]
                if chain[0].get("cause_open"):
                    head["cause_open"] = chain[0]["cause_open"]
                else:
                    head.pop("cause_open", None)
                head["glued_from"] = [int(c["value"]) for c in chain[:first]]
            out.append(head)
            out.extend(chain[first + 1:])
            last = int(chain[-1]["value"])
        elif any(r[0] == "no_data" for r in res):
            out.extend(chain)
            last = int(chain[-1]["value"])
        elif j < n and int(periods[j]["value"]) == 1:
            # За цепочкой снова задержка пуска — не выход и не возврат в стоп
            out.extend(chain)
            last = int(chain[-1]["value"])
        elif j >= n or _tz(periods[j]["state_start"]) >= t_res:
            # Исход не решён: стоп ещё не наступил. Одно открытое звено
            pend = dict(chain[-1])
            pend["state_start"] = chain[0]["state_start"]
            if chain[0].get("cause_open"):
                pend["cause_open"] = chain[0]["cause_open"]
            else:
                pend.pop("cause_open", None)
            pend["attempt_pending"] = True
            pend["pending_path"] = [int(c["value"]) for c in chain]
            out.append(pend)
            last = int(chain[-1]["value"])
        else:
            stop = periods[j]
            plate = dict(stop)
            plate["state_start"] = chain[0]["state_start"]
            if chain[0].get("cause_open"):
                plate["cause_open"] = chain[0]["cause_open"]
            else:
                plate.pop("cause_open", None)
            plate["attempt"] = _attempt_record(chain, stop, res, code_periods, lag)
            out.append(plate)
            last = 0
            j += 1
        i = j
    return out


def _steady_window(
    all_by_addr: dict[int, list[dict]], cfg, seg_start: datetime, seg_end: datetime,
    next_value: int | None, pending: bool,
) -> tuple[datetime | None, datetime | None] | None:
    """Рабочая часть «Работы»: от выхода на рабочие обороты до их сброса перед
    охлаждением (v5.1.16).

    При ручном пуске панель пишет «Номинал» по команде, и в первый подсегмент
    работы попадали нули до выхода на обороты: за 30 дней 29 ложных «давление
    масла 0 / обороты 0» на 16 пусках (ДЭС №3 29.09 — 15 ч). В конце — точка
    750 об/мин за секунду до перехода на холостой ход давала «просадку».
    Рабочие обороты — обороты ≥ рабочего порога ИЛИ частота ≥ рабочей (если
    датчик оборотов отвалится, электрика страхует). Сброс в конце отрезается,
    только перед охлаждением 4/5/6 и только если он короче rundown_max_sec:
    падение в стоп — не переход, его аналитика видит.

    None — показаний нет совсем (делить не по чему, как до v5.1.16);
    (None, None) — рабочих оборотов не было: весь сегмент переходный.
    """
    prm = confirm_params(cfg)
    if not prm["enabled"]:
        return None
    t0, t1 = _tz(seg_start), _tz(seg_end)
    if pending:
        return None, None
    rpm = value_series(all_by_addr, cfg.role_to_addr("RPM"))
    freq = value_series(all_by_addr, cfg.role_to_addr("FREQUENCY"))
    events: list[tuple[datetime, str, float]] = []
    state: dict[str, float | None] = {"rpm": None, "freq": None}
    for name, series in (("rpm", rpm), ("freq", freq)):
        for ts, v in series:
            if ts < t0:
                if (t0 - ts).total_seconds() <= prm["carry_sec"]:
                    state[name] = v
                continue
            if ts >= t1:
                break
            events.append((ts, name, v))
    if state["rpm"] is None and state["freq"] is None and not events:
        return None
    events.sort(key=lambda e: e[0])

    def _ok() -> bool:
        return ((state["rpm"] is not None and state["rpm"] >= prm["rated_rpm"])
                or (state["freq"] is not None and state["freq"] >= prm["rated_freq"]))

    steady_from = t0 if _ok() else None
    last_ok_end: datetime | None = None   # момент, когда рабочие обороты ушли
    for ts, name, v in events:
        was = _ok()
        state[name] = v
        now = _ok()
        if now and steady_from is None:
            steady_from = ts
        if was and not now:
            last_ok_end = ts
        elif now:
            last_ok_end = None
    if steady_from is None:
        return None, None
    steady_to = t1
    if (next_value in (4, 5, 6) and last_ok_end is not None
            and (t1 - last_ok_end).total_seconds() <= prm["rundown_max_sec"]):
        steady_to = last_ok_end
    if steady_to <= steady_from:
        return None, None
    return steady_from, steady_to


def segment(
    enum_periods: list[dict[str, Any]],
    history: list[dict[str, Any]],
    fault_periods: list[dict[str, Any]],
    gaps: list[dict[str, Any]],
    cfg: AnalyticsConfig,
    router_sn: str,
    equip_type: str,
    panel_id: int,
    engine_sn: str,
    ts_from: datetime,
    ts_to: datetime,
    initial_coking_risk=None,  # CokingRisk | None — для онлайн-режима (п. 5.1 ТЗ)
    transition_ctx: list | None = None,  # звенья перед окном (analytics.transitions, v5.1.10)
    load_seed: dict | None = None,  # последний замер нагрузки перед окном + обрывы до окна
    confirm_to: datetime | None = None,  # докуда решать исход попытки за краем окна (v5.1.16)
) -> list[Segment]:
    """Главная точка входа: преобразует сырые данные в список сегментов.

    Входные данные должны быть загружены runner-ом из БД через analytics.source.
    Обработка строго каузальная (от старых к новым).

    initial_coking_risk: если передан — инициализирует coking_risk аккумулятор
    начальным состоянием из предыдущего закрытого сегмента (ТЗ Этап 1.5, раздел 5.1).
    """
    import copy as _copy
    tf = _tz(ts_from)
    tt = _tz(ts_to)

    # Индексируем историю по адресу; единицы — как в карте (наработка: с → ч)
    all_by_addr = _apply_scale(_build_by_addr(history), cfg)
    # Одиночные нули источника — до всего остального (v5.1.17); ноль у
    # настоящего останова (40011 → 0) — не сбой
    _stops = [(_tz(p["state_start"]), _tz(p["state_end"]) if p.get("state_end") else tt)
              for p in enum_periods if p.get("addr") == 40011 and int(p.get("value") or 0) == 0]
    all_by_addr, _zero_glitches = _drop_zero_glitches(all_by_addr, cfg, tt, _stops)
    # Регистры-состояния для характеристик подсегментов (код неисправности)
    state_periods = _state_periods(enum_periods, cfg)

    # Параметры конфигурации
    preamble_slices = int(cfg.seg("preamble", "slices", default=3))

    # Периоды RUN_STATE (addr=40011), отсортированные по state_start
    run_state_periods = sorted(
        [p for p in enum_periods if p["addr"] == 40011],
        key=lambda p: _tz(p["state_start"]),
    )

    # Рез СТОП-периодов по устранению неисправностей: «СТОП (авария)» →
    # FAULT_CLEARED → «СТОП (готов к пуску)». Живой движок и перечитка
    # получают одинаковые границы — правило детерминировано из history.
    # Новая модель (v4.9.68): один рез вместо двух сплиттеров, у стопа два вида.
    # Пока флаг выключен — поведение байт-в-байт прежнее.
    if bool(cfg.seg("stop_kind", "enabled", default=False)):
        # Горизонт — тот же, что у исхода попытки (confirm_to): открытая
        # авария, поднявшаяся на падении за суточным резом, иначе обрезалась
        # бы по tt, и голова плашки вышла бы «Стоп», а перечитка — «Авария»
        # (ревью v5.1.16). Сегменты всё равно режутся по tt
        run_state_periods = _classify_stop_periods(
            run_state_periods, fault_periods, cfg,
            max(tt, _tz(confirm_to)) if confirm_to else tt, enum_periods,
        )
    else:
        run_state_periods = _split_stop_on_fault_cleared(
            run_state_periods, fault_periods, all_by_addr, cfg, tt, enum_periods,
        )
        run_state_periods = _split_stop_on_shutdown(
            run_state_periods, fault_periods, cfg, tt,
        )

    # Выход из стопа подтверждается фактом (v5.1.16). Режим перед окном — из
    # контекста переходов: окно живого движка начинается ровно на попытке
    _prev_rs: int | None = None
    if transition_ctx:
        from .transitions import ctx_for_window
        _ctx = ctx_for_window(transition_ctx, tf,
                              float(cfg.seg("transitions", "joint_tol_sec", default=2.0)))
        # Задержка пуска выход из стопа не прерывает: режим до неё
        for _lk in reversed(_ctx or []):
            if _lk.run_state is not None and int(_lk.run_state) != 1:
                _prev_rs = int(_lk.run_state)
                break
    # Прошлое окно закрыто «Работой», а первый период окна начался до него и
    # не работа: это хвост куска, склеенного с ней за суточным резом
    _carry = bool(
        _prev_rs == 3 and run_state_periods
        and _tz(run_state_periods[0]["state_start"]) < tf
        and int(run_state_periods[0]["value"]) not in (0, 1, 3))
    run_state_periods = _confirm_attempts(
        run_state_periods, all_by_addr, fault_periods, gaps, cfg, tt, _prev_rs,
        confirm_to=confirm_to,
        code_periods=state_periods.get(cfg.role_to_addr("LAST_FAULT_CODE")) or [],
        carry_glue=_carry,
    )

    segments: list[Segment] = []
    accumulators = RiskAccumulators()
    if initial_coking_risk is not None:
        accumulators.coking_risk = _copy.deepcopy(initial_coking_risk)

    for k_period, period in enumerate(run_state_periods):
        p_start = _tz(period["state_start"])
        p_end_raw = period.get("state_end")
        p_end = _tz(p_end_raw) if p_end_raw else tt

        # Фильтруем к диапазону запроса
        seg_start = max(p_start, tf)
        seg_end = min(p_end, tt)
        if seg_end <= seg_start:
            continue

        run_state: int = int(period["value"])
        run_state_label: str = period.get("label") or str(run_state)
        attempt: dict[str, Any] | None = period.get("attempt")
        attempt_pending = bool(period.get("attempt_pending"))
        if attempt:
            from .labels import attempt_label
            run_state_label = attempt_label(attempt)
        elif attempt_pending:
            from .labels import pending_label
            run_state_label = pending_label(run_state)
        # Риск нагара на начало сегмента — для прироста/погашения в сводке
        coking_start = accumulators.coking_risk.to_dict()

        # Формируем строки by_addr для сегмента (включая преамбулу)
        preamble_by_addr, preamble_t_start = _collect_preamble(
            all_by_addr, seg_start, preamble_slices, cfg
        )
        seg_by_addr: dict[int, list[dict]] = {}
        for addr in set(all_by_addr.keys()) | set(preamble_by_addr.keys()):
            pre_rows = preamble_by_addr.get(addr, [])
            seg_rows = [
                r for r in all_by_addr.get(addr, [])
                if _tz(seg_start) <= _tz(r["ts"]) < _tz(seg_end)
            ]
            if pre_rows or seg_rows:
                seg_by_addr[addr] = pre_rows + seg_rows

        # Forward-fill: заполняем отсутствующие значения в пакетах по пинг-регистру.
        # Строки с is_carried_forward=True используются только для агрегатов;
        # velocity-метрики их игнорируют.
        seg_gaps = _filter_gaps(gaps, seg_start, seg_end)
        seg_by_addr = apply_forward_fill(seg_by_addr, cfg, seg_start, seg_end, seg_gaps)

        # Fault-периоды, пересекающиеся с сегментом
        fault_periods_in_seg = [
            fp for fp in fault_periods
            if _tz(fp["fault_start"]) < _tz(seg_end)
            and (_tz(fp["fault_end"]) if fp.get("fault_end") else _tz(seg_end)) > _tz(seg_start)
        ]

        # Идентификатор сегмента — детерминированный
        seg_id = (
            f"{router_sn}_p{panel_id}"
            f"_s{run_state}"
            f"_{_tz(seg_start).strftime('%Y%m%dT%H%M%SZ')}"
        )

        # Мото-часы на начало сегмента
        engine_hours_start = _get_engine_hours(seg_by_addr, seg_start, cfg)

        # Причины открытия/закрытия (FAULT_CLEARED проставляет сплит СТОП-периодов)
        cause_open = period.get("cause_open") or (
            "REPORT_START" if p_start < tf else "RUN_STATE_CHANGE"
        )
        stop_kind: str | None = period.get("stop_kind")
        # Причина реза действует, только если сам рез попал в окно. Иначе при
        # доборе после простоя сервиса довграничная половина получала бы
        # «Авария снята» на суточной границе, хотя маску сняли часами позже.
        _cut_inside = p_end_raw is not None and _tz(p_end_raw) < tt
        cause_close: str | None = (
            (period.get("cause_close") or "RUN_STATE_CHANGE") if _cut_inside else None
        )

        # L2-подсегменты
        if run_state == 3:
            _nxt = (int(run_state_periods[k_period + 1]["value"])
                    if k_period + 1 < len(run_state_periods) and _cut_inside else None)
            _steady = _steady_window(all_by_addr, cfg, seg_start, seg_end, _nxt,
                                     attempt_pending)
            subsegments, accumulators = _build_subsegments_for_running(
                seg_id=seg_id,
                seg_by_addr=seg_by_addr,
                preamble_t_start=preamble_t_start,
                seg_start=seg_start,
                seg_end=seg_end,
                fault_periods_in_seg=fault_periods_in_seg,
                gaps=seg_gaps,
                prev_accumulators=accumulators,
                run_state=run_state,
                cfg=cfg,
                state_periods=state_periods,
                steady=_steady,
                onset_faults=fault_periods,
            )
        else:
            subseg, accumulators = _build_single_subsegment(
                seg_id=seg_id,
                idx=1,
                seg_by_addr=seg_by_addr,
                preamble_t_start=preamble_t_start,
                t_start=seg_start,
                t_end=seg_end,
                load_zone="NA",
                cause_open=cause_open,
                cause_close=cause_close,
                run_state=run_state,
                gaps=seg_gaps,
                accumulators=accumulators,
                prev_zone=None,
                cfg=cfg,
                fault_periods=fault_periods_in_seg,
                state_periods=state_periods,
                onset_faults=fault_periods,
            )
            subsegments = [subseg]

        # Sequence checks
        seq_checks = _sequence_checks(run_state, subsegments, fault_periods_in_seg, cfg)
        _gc = _glitch_check(_zero_glitches, _tz(seg_start), _tz(seg_end))
        if _gc:
            seq_checks.append(_gc)

        # Fault-события сегмента (для events[])
        events: list[dict[str, Any]] = []
        for fp in fault_periods_in_seg:
            fe = fp.get("fault_end")
            events.append({
                "type": "FAULT",
                "t": _tz(fp["fault_start"]).isoformat(),
                # None → неисправность не закрылась (активна на момент анализа)
                "fault_end": _tz(fe).isoformat() if fe else None,
                "addr": fp.get("addr"),
                "bit": fp.get("bit"),
                "name_ru": fp.get("fault_name_ru"),
                "name": fp.get("fault_name"),
                "severity": fp.get("severity"),
                "duration_sec": fp.get("duration_sec"),
            })

        # Качество данных сегмента (взвешенное среднее по подсегментам)
        total_dur = sum(s.duration_sec for s in subsegments)
        if total_dur > 0:
            seg_dq = sum(s.data_quality * s.duration_sec for s in subsegments) / total_dur
        else:
            seg_dq = 1.0

        seg = Segment(
            id=seg_id,
            router_sn=router_sn,
            equip_type=equip_type,
            panel_id=panel_id,
            engine_sn=engine_sn,
            run_state=run_state,
            run_state_label=run_state_label,
            t_start=_tz(seg_start).isoformat(),
            t_end=_tz(seg_end).isoformat(),
            duration_sec=(_tz(seg_end) - _tz(seg_start)).total_seconds(),
            engine_hours_start=engine_hours_start,
            cause_open=cause_open,
            cause_close=cause_close,
            preamble_included=preamble_t_start is not None,
            data_quality=round(seg_dq, 3),
            subsegments=subsegments,
            sequence_checks=seq_checks,
            events=events,
            stop_kind=stop_kind,
            coking_start=coking_start,
            attempt=attempt,
            attempt_pending=attempt_pending,
        )
        segments.append(seg)

    # Второй проход: межсегментные проверки (warmup / cooldown / cold_start / stop_profile)
    _inter_segment_checks(segments, all_by_addr, cfg, state_periods,
                          transition_ctx=transition_ctx, ts_from=tf, load_seed=load_seed,
                          gaps=gaps)

    return segments
