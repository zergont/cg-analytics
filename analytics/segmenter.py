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
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

from .config import AnalyticsConfig
from .contract import RiskAccumulators, Segment, Subsegment
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

    # Интервалы несброшенного кода 40012 (step-функция по замерам)
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
    """Качество связи [0.0, 1.0] = 1 - (суммарное время gap / длительность окна).

    Единственный источник истины — таблица data_gaps. Каждый gap обрезается
    по границам окна; незакрытый gap (gap_end IS NULL) считается до t_end.
    """
    t0 = _tz(t_start)
    t1 = _tz(t_end)
    duration_sec = (t1 - t0).total_seconds()
    if duration_sec <= 0:
        return 1.0

    gap_sec = 0.0
    for g in gaps:
        gs = max(_tz(g["gap_start"]), t0)
        ge_raw = g.get("gap_end")
        ge = min(_tz(ge_raw) if ge_raw else t1, t1)
        if ge > gs:
            gap_sec += (ge - gs).total_seconds()

    return round(max(0.0, 1.0 - gap_sec / duration_sec), 3)


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
) -> tuple[Subsegment, RiskAccumulators]:
    """Создать один подсегмент (для нейтральных состояний RUN_STATE ≠ 3)."""
    subseg_id = f"{seg_id}.{idx}"
    char_t_start = preamble_t_start if (idx == 1 and preamble_t_start) else t_start

    ts_index = _build_ts_index(seg_by_addr)
    sub_chars = _slice_by_addr(seg_by_addr, ts_index, char_t_start, t_end)
    sub_win   = _slice_by_addr(seg_by_addr, ts_index, t_start, t_end)

    chars   = compute_characteristics(sub_chars, char_t_start, t_end, cfg)
    derived = compute_derived_metrics(sub_win, t_start, t_end, gaps, cfg)
    new_acc = update_accumulators(accumulators, sub_win, t_start, t_end, load_zone, run_state, cfg, gaps)

    faults_in = [
        fp for fp in fault_periods
        if _tz(fp["fault_start"]) < _tz(t_end)
        and (_tz(fp["fault_end"]) if fp.get("fault_end") else _tz(t_end)) > _tz(t_start)
    ]
    detections = run_all_detectors(
        chars, derived, new_acc, faults_in,
        load_zone, run_state, t_start, t_end, prev_zone, cfg,
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
) -> tuple[list[Subsegment], RiskAccumulators]:
    """L2-сегментация внутри RUN_STATE=3: ZoneTracker + fault-события."""
    t_start_tz = _tz(seg_start)
    t_end_tz = _tz(seg_end)

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
        intervals.append((prev_ts, t_end_tz, prev_zone, prev_cause_open, None))

    if not intervals:
        intervals = [(t_start_tz, t_end_tz, initial_zone, "SEG_START", None)]

    # Создаём подсегменты
    subsegments: list[Subsegment] = []
    accumulators = prev_accumulators
    prev_sub_zone: str | None = None

    ts_index = _build_ts_index(seg_by_addr)

    for idx, (t_sub_start, t_sub_end, zone, cause_open, cause_close) in enumerate(intervals):
        subseg_id = f"{seg_id}.{idx + 1}"
        char_t_start = (preamble_t_start if idx == 0 and preamble_t_start else t_sub_start)

        sub_chars = _slice_by_addr(seg_by_addr, ts_index, char_t_start, t_sub_end)
        sub_win   = _slice_by_addr(seg_by_addr, ts_index, t_sub_start, t_sub_end)

        chars        = compute_characteristics(sub_chars, char_t_start, t_sub_end, cfg)
        derived      = compute_derived_metrics(sub_win, t_sub_start, t_sub_end, gaps, cfg)
        accumulators = update_accumulators(accumulators, sub_win, t_sub_start, t_sub_end, zone, run_state, cfg, gaps)

        faults_in_sub = [
            fp for fp in fault_periods_in_seg
            if _tz(fp["fault_start"]) < t_sub_end
            and (_tz(fp["fault_end"]) if fp.get("fault_end") else t_sub_end) > t_sub_start
        ]
        detections = run_all_detectors(
            chars, derived, accumulators, faults_in_sub,
            zone, run_state, t_sub_start, t_sub_end, prev_sub_zone, cfg,
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

    return subsegments, accumulators


# ── Sequence checks ───────────────────────────────────────────────────────────

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
        empty = [s.id for s in subsegments if s.data_quality == 0.0]
        checks.append({
            "check": "subseg_data_coverage",
            "passed": len(empty) == 0,
            "details": f"empty_subsegments={empty}" if empty else "ok",
        })

    # Проверка: нет активных критических fault в конце сегмента
    shutdown_faults = [
        fp for fp in fault_periods
        if fp.get("fault_end") is None and fp.get("severity") in ("SHUTDOWN",)
    ]
    checks.append({
        "check": "no_active_shutdown_fault",
        "passed": len(shutdown_faults) == 0,
        "details": (
            f"active_shutdown_faults={[fp.get('fault_name') for fp in shutdown_faults]}"
            if shutdown_faults else "ok"
        ),
    })

    return checks


# ── Межсегментные проверки ────────────────────────────────────────────────────

def _check_stop_profile(
    stop_seg: Segment,
    all_by_addr: dict[int, list[dict]],
    cfg: AnalyticsConfig,
    Detection: type,
) -> None:
    """Диагностика профиля останова: обороты должны упасть до нуля за T_stop_max.

    Если RPM не достигли порога rpm_stopped_threshold за T_stop_max секунд —
    подозрение на отказ клапана отсечки топлива (пожароопасно!).
    """
    from datetime import datetime as _DT

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

    # RPM-ряд в сегменте останова (только реальные измерения)
    rpm_rows = sorted(
        (
            (_tz(r["ts"]), float(r["value"]))
            for r in all_by_addr.get(rpm_addr, [])
            if t0 <= _tz(r["ts"]) < t1
            and r.get("value") is not None
            and not r.get("is_carried_forward", False)
        ),
        key=lambda x: x[0],
    )

    if not rpm_rows:
        return  # нет данных RPM — не можем проверить

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
        # RPM так и не упали — клапан отсечки подозревается
        rpm_min = min(v for _, v in rpm_rows)
        stop_seg.sequence_checks.append({
            "check": "rpm_stop_profile",
            "passed": False,
            "stop_time_sec": None,
            "rpm_min_observed": rpm_min,
            "seg_duration_sec": seg_dur,
            "T_stop_max_sec": T_stop_max,
            "details": (
                f"НАРУШЕНИЕ: RPM не достигли {rpm_thr:.0f} rpm за {seg_dur:.0f}с "
                f"(минимум {rpm_min:.0f} rpm) — подозрение на отказ клапана отсечки!"
            ),
        })
        if stop_seg.subsegments:
            stop_seg.subsegments[0].detections.append(Detection(
                scenario="STOP_PROFILE",
                severity=cfg.det("STOP_PROFILE", "severity_default", default="WARNING"),
                t_detected=stop_seg.t_start,
                source="METRIC_RULE",
                trigger=(
                    f"RPM не достигли {rpm_thr:.0f} rpm за {seg_dur:.0f}с "
                    f"(норматив {T_stop_max:.0f}с) — клапан отсечки топлива?"
                ),
                related_roles=["RPM"],
                fault_codes=[],
                description_key="STOP_PROFILE.rpm_not_zero_fuel_shutoff_suspect",
                values={
                    "rpm_min_observed": rpm_min,
                    "seg_duration_sec": seg_dur,
                    "T_stop_max_sec": T_stop_max,
                    "rpm_stopped_threshold": rpm_thr,
                },
            ))


def _inter_segment_checks(
    segments: list[Segment],
    all_by_addr: dict[int, list[dict]],
    cfg: AnalyticsConfig,
) -> None:
    """Второй проход по готовому списку сегментов: проверяет переходы между ними.

    Мутирует sequence_checks и detections сегментов/подсегментов на месте.
    Вызывается после основного цикла в segment(), перед возвратом результата.
    all_by_addr нужен для STOP_PROFILE (доступ к сырому RPM-ряду останова).
    """
    from .contract import Detection

    warmup_en = bool(cfg.det("WARMUP_VIOLATION", "enabled", default=True))
    cooldown_en = bool(cfg.det("COOLDOWN_VIOLATION", "enabled", default=True))
    stop_profile_en = bool(cfg.det("STOP_PROFILE", "enabled", default=True))

    for i, seg in enumerate(segments):
        prev_segs = segments[:i]

        # WARMUP_CHECK: Running-сегмент проверяет предшествующий прогрев
        if seg.run_state == 3 and warmup_en:
            _check_warmup_transition(seg, prev_segs, cfg, Detection)

        # COOLDOWN_CHECK: Stop-сегмент проверяет наличие охлаждения
        if seg.run_state == 0 and cooldown_en:
            _check_cooldown_transition(seg, prev_segs, cfg, Detection)

        # STOP_PROFILE: диагностика профиля останова (клапан отсечки топлива)
        if seg.run_state == 0 and stop_profile_en:
            _check_stop_profile(seg, all_by_addr, cfg, Detection)


def _check_warmup_transition(
    running_seg: Segment,
    prev_segs: list[Segment],
    cfg: AnalyticsConfig,
    Detection: type,
) -> None:
    """Проверить наличие и длительность прогрева перед running-сегментом.

    Добавляет cold_start_context и warmup_duration в sequence_checks.
    При нарушении добавляет Detection WARMUP_VIOLATION в первый подсегмент.
    """
    min_warmup = float(cfg.det("WARMUP_VIOLATION", "min_warmup_sec", default=180.0))
    hot_warmup = float(cfg.det("WARMUP_VIOLATION", "hot_start_warmup_sec", default=0.0))
    cold_thr = float(cfg.det("WARMUP_VIOLATION", "cold_start_coolant_c", default=21.0))

    # Найти последний предшествующий RUN_STATE=2 (Warmup)
    prev_warmup: Segment | None = next(
        (s for s in reversed(prev_segs) if s.run_state == 2), None
    )

    if prev_warmup is None:
        # Прогрев не обнаружен в запрошенном периоде — не можем оценить
        running_seg.sequence_checks.append({
            "check": "warmup_presence",
            "passed": True,
            "cold_start": None,
            "details": "RUN_STATE=2 (Warmup) не обнаружен в периоде анализа",
        })
        return

    # Определить cold_start по Т ОЖ в начале warmup-сегмента
    coolant_start: float | None = None
    if prev_warmup.subsegments:
        cool_char = prev_warmup.subsegments[0].characteristics.get("COOLANT_TEMP", {})
        coolant_start = cool_char.get("value_start")

    cold_start = (coolant_start is not None and coolant_start < cold_thr)

    # Записать контекст пуска в sequence_checks running-сегмента
    running_seg.sequence_checks.append({
        "check": "cold_start_context",
        "passed": True,
        "cold_start": cold_start,
        "coolant_at_start_c": coolant_start,
        "details": (
            f"cold_start={cold_start}, Т ОЖ на пуске={coolant_start:.1f}°C"
            if coolant_start is not None
            else "cold_start=неизвестно (нет данных Т ОЖ)"
        ),
    })

    # Проверить длительность прогрева с учётом типа пуска
    required = min_warmup if cold_start else hot_warmup
    warmup_dur = prev_warmup.duration_sec
    passed = warmup_dur >= required

    running_seg.sequence_checks.append({
        "check": "warmup_duration",
        "passed": passed,
        "warmup_duration_sec": warmup_dur,
        "required_sec": required,
        "cold_start": cold_start,
        "details": (
            f"ok: прогрев {warmup_dur:.0f}с >= {required:.0f}с"
            if passed
            else (
                f"НАРУШЕНИЕ: прогрев {warmup_dur:.0f}с < {required:.0f}с "
                f"(cold_start={cold_start}, Т ОЖ={coolant_start}°C)"
            )
        ),
    })

    if not passed and running_seg.subsegments:
        trigger = (
            f"warmup_duration={warmup_dur:.0f}с < {required:.0f}с при "
            f"{'холодном' if cold_start else 'горячем'} пуске"
            + (f" (Т ОЖ={coolant_start:.1f}°C)" if coolant_start is not None else "")
        )
        running_seg.subsegments[0].detections.append(Detection(
            scenario="WARMUP_VIOLATION",
            severity=cfg.det("WARMUP_VIOLATION", "severity_default", default="WARNING"),
            t_detected=running_seg.t_start,
            source="PASSPORT_THRESHOLD",
            trigger=trigger,
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


def _check_cooldown_transition(
    stop_seg: Segment,
    prev_segs: list[Segment],
    cfg: AnalyticsConfig,
    Detection: type,
) -> None:
    """Проверить наличие охлаждения после работы под нагрузкой (Addendum v1.4, п.4.2).

    ИСПРАВЛЕННАЯ логика: горячий останов без охлаждения → ВСЕГДА нарушение.
    Причина останова НЕ подавляет тревогу — аварийная УСИЛИВАЕТ (риск заклинивания!).

    severity:
    - останов с ELEVATED/OVERLOAD → WARNING (риск заклинивания/коробления турбины)
    - останов с NORMAL/LOW → CAUTION
    Аварийный останов (SHUTDOWN-fault в сегменте) добавляет контекст в trigger.
    """
    required_after = cfg.det("COOLDOWN_VIOLATION", "required_after_zone", default="ELEVATED")
    min_cooldown = float(cfg.det("COOLDOWN_VIOLATION", "min_cooldown_sec", default=0.0))

    # Найти последний running-сегмент (RUN_STATE=3)
    last_running: Segment | None = None
    last_running_idx: int = -1
    for j, s in enumerate(prev_segs):
        if s.run_state == 3:
            last_running = s
            last_running_idx = j

    if last_running is None:
        return  # нечего проверять

    # Была ли нагрузка (любая, не только ELEVATED)?
    elevated_zones = {"ELEVATED", "OVERLOAD"}
    had_elevated = any(sub.load_zone in elevated_zones for sub in last_running.subsegments)
    had_any_load = any(
        sub.load_zone not in ("NA",)
        for sub in last_running.subsegments
    )

    if not had_any_load:
        return  # не было нагрузки — охлаждение не требуется

    # Была ли фаза охлаждения (RUN_STATE 4/5) между running и stop?
    segs_between = prev_segs[last_running_idx + 1:]
    cooldown_dur = sum(s.duration_sec for s in segs_between if s.run_state in (4, 5))
    had_cooldown_phase = cooldown_dur > 0

    if min_cooldown <= 0:
        passed = had_cooldown_phase
    else:
        passed = cooldown_dur >= min_cooldown

    stop_seg.sequence_checks.append({
        "check": "cooldown_after_elevated",
        "passed": passed,
        "cooldown_duration_sec": cooldown_dur,
        "required_sec": min_cooldown,
        "had_elevated": had_elevated,
        "had_cooldown_phase": had_cooldown_phase,
        "details": (
            f"ok: охлаждение {cooldown_dur:.0f}с"
            if passed
            else (
                f"НАРУШЕНИЕ: нет фазы охлаждения RUN_STATE=4/5"
                if not had_cooldown_phase
                else f"НАРУШЕНИЕ: cooldown={cooldown_dur:.0f}с < {min_cooldown:.0f}с"
            )
        ),
    })

    if not passed and stop_seg.subsegments:
        # Severity: WARNING если была высокая нагрузка (риск турбины), иначе CAUTION
        severity = "WARNING" if had_elevated else "CAUTION"

        # Контекст аварийного останова (SHUTDOWN-fault в сегменте останова)
        has_shutdown_fault = any(
            e.get("type") == "FAULT" and e.get("severity") in ("shutdown", "SHUTDOWN")
            for e in stop_seg.events
        )
        emergency_prefix = "АВАРИЙНЫЙ ОСТАНОВ — " if has_shutdown_fault else ""

        stop_seg.subsegments[0].detections.append(Detection(
            scenario="COOLDOWN_VIOLATION",
            severity=severity,
            t_detected=stop_seg.t_start,
            source="PASSPORT_THRESHOLD",
            trigger=(
                f"{emergency_prefix}Останов без охлаждения "
                f"({'ELEVATED/OVERLOAD' if had_elevated else 'под нагрузкой'}) "
                f"— риск {'заклинивания двигателя!' if had_elevated else 'теплового удара'}"
            ),
            related_roles=["COOLANT_TEMP", "RPM"],
            fault_codes=[611],
            description_key="COOLDOWN_VIOLATION.no_cooldown_after_elevated",
            values={
                "cooldown_duration_sec": cooldown_dur,
                "required_sec": min_cooldown,
                "had_elevated": had_elevated,
                "emergency_stop": has_shutdown_fault,
            },
        ))


# ── Главная функция ───────────────────────────────────────────────────────────

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

    # Индексируем историю по адресу
    all_by_addr = _build_by_addr(history)

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
    run_state_periods = _split_stop_on_fault_cleared(
        run_state_periods, fault_periods, all_by_addr, cfg, tt,
    )

    # Рез СТОП-периодов по пересечению уровня в АВАРИЮ: SHUTDOWN-фронт на стоящей
    # машине (E-Stop и т.п.) выделяется в отдельный красный сегмент, не хороня
    # сегмент-причину. Границы детерминированы из history.
    run_state_periods = _split_stop_on_shutdown(
        run_state_periods, fault_periods, cfg, tt,
    )

    segments: list[Segment] = []
    accumulators = RiskAccumulators()
    if initial_coking_risk is not None:
        accumulators.coking_risk = _copy.deepcopy(initial_coking_risk)

    for period in run_state_periods:
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
        cause_close: str | None = period.get("cause_close") or (
            None if p_end_raw is None or _tz(p_end_raw) >= tt
            else "RUN_STATE_CHANGE"
        )

        # L2-подсегменты
        if run_state == 3:
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
            )
            subsegments = [subseg]

        # Sequence checks
        seq_checks = _sequence_checks(run_state, subsegments, fault_periods_in_seg, cfg)

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
        )
        segments.append(seg)

    # Второй проход: межсегментные проверки (warmup / cooldown / cold_start / stop_profile)
    _inter_segment_checks(segments, all_by_addr, cfg)

    return segments
