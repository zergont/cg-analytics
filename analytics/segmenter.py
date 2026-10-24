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
from .live import held_before, segment_spans


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
    shutdown_iv: list[tuple[datetime, datetime]] | None = None,
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

    shutdown_iv — уже посчитанные интервалы (v5.1.22: segment() считает их один
    раз — ими же отмечается сегмент, оборванный аварией, _trip_mark).
    """
    if shutdown_iv is None:
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
    cause_open: str | None = None,
    held: dict[str, Any] | None = None,
    live_edge: bool = False,
) -> tuple[list[Subsegment], RiskAccumulators]:
    """L2-сегментация внутри RUN_STATE=3: ZoneTracker + fault-события.

    live_edge — сегмент кончается на живом краю открытого окна: только там
    правило B ждёт следующего отсчёта оборотов (v5.1.21, ревью analytics-3).

    steady — рабочая часть сегмента (v5.1.16, _steady_window): до неё разгон с
    нулей пуска, после — сброс оборотов перед охлаждением. Эти куски —
    отдельные переходные подсегменты без детекторов работы (панель видна).
    (None, None) — рабочих оборотов не было, весь сегмент переходный;
    None — делить нечего (как до v5.1.16).

    Участки нарушений (v5.1.21, П2, analytics/live.py) считаются один раз на
    рабочую часть [head_end, tail_start) до цикла подсегментов и идут в
    детекторы рабочих подсегментов; переходные куски их не получают. held —
    удержанные значения на начало сегмента (live.held_before): частота —
    опросу оборотов, остальное — переносу через начало окна, только при
    продолжении режима (cause_open REPORT_START без разгона). Сбой расчёта —
    предупреждение и прежнее поведение (live_spans=None); сбой одного ключа —
    по подсегменту только его детектор (v5.1.23, п.7: ключ — None в словаре).
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

    # Участки и кластеры нарушений рабочей части — один раз на сегмент (v5.1.21).
    # None, а не {}: пустой словарь молча погасил бы детекторы участков.
    # Горизонт правила B — только живой край открытого окна; у закрытого (рез,
    # смена режима) будущих строк не будет (ревью v5.1.21, analytics-3).
    # Ключи изолированы внутри segment_spans (v5.1.23); этот try — страховка
    live_spans: dict[str, list[dict] | None] | None = None
    try:
        live_spans = segment_spans(
            seg_by_addr, None, cfg, head_end, tail_start, gaps, held or {},
            carry=(cause_open == "REPORT_START" and head_end == seg_t0),
            horizon=seg_t1 if live_edge else None,
        )
    except Exception:
        logger.warning("Участки нарушений сегмента %s не посчитаны — детекторы по подсегменту",
                       seg_id, exc_info=True)

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
        _det_args = (chars, derived, accumulators, faults_in_sub,
                     zone, run_state, t_sub_start, t_sub_end, prev_sub_zone, cfg, state_periods)
        _onset = onset_faults if onset_faults is not None else fault_periods_in_seg
        try:
            detections = run_all_detectors(*_det_args, onset_faults=_onset, live_spans=live_spans)
        except Exception:
            if live_spans is None:
                raise
            # Ветки детекторов по участкам (v5.1.21) — тоже под защитой: при сбое
            # подсегмент считается по-старому, цикл открытого окна не падает
            logger.warning("Детекторы по участкам подсегмента %s не отработали — по подсегменту",
                           subseg_id, exc_info=True)
            detections = run_all_detectors(*_det_args, onset_faults=_onset, live_spans=None)

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


# ── Сбой датчика оборотов: сверка с частотой генератора (v5.1.21) ───────────
# ДГУ №1 15.10 04:42–09:24 МСК: 12 одиночных отсчётов 1417–1479 об/мин при
# 49,9–50,1 Гц, следом каждый раз 1495–1501 — сбой датчика, а не провал. Каждый
# заводил «Просадку оборотов» на весь подсегмент. Решения владельца 15.10.2026
# (В1, В4): такой отсчёт вырезается из аналитики и перечисляется в отчёте;
# правило и частота опроса — analytics/rpm_freq.py. Фильтр нулей не трогаем:
# нули и всё ниже min_rpm (выбег, холостой ход, падение в стоп) — не сюда.


def _held_from_seed(load_seed: dict | None, cfg) -> dict[str, tuple[datetime, float]]:
    """Удержанные значения на голове окна из load_seed["held"] (v5.1.21, П10):
    {роль: (метка строки, значение в единицах карты)}. Пусто — посева нет
    (перечитка, тесты, старый движок)."""
    out: dict[str, tuple[datetime, float]] = {}
    for role, h in ((load_seed or {}).get("held") or {}).items():
        try:
            ts = h["ts"]
            ts = _tz(datetime.fromisoformat(ts)) if isinstance(ts, str) else _tz(ts)
            v = float(h["value"])
            addr = cfg.role_to_addr(role)
            k = cfg.register_map.get(addr, {}).get("_scale") if addr is not None else None
            out[str(role)] = (ts, v * k if k else v)
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _drop_rpm_glitches(
    by_addr: dict[int, list[dict]], cfg, enum_periods: list[dict] | None,
    held_freq: tuple[datetime, float] | None = None,
    gaps: list[dict] | None = None,
) -> tuple[dict[int, list[dict]], list[tuple[datetime, float, float | None]]]:
    """Вырезать одиночные сбои датчика оборотов. (новый by_addr, [(момент, обороты, f_min)]).

    Отсчёт оборотов — сбой (v5.1.21, 1а), если всё сразу:
    - min_rpm < обороты < порога (нули не трогаем — решение 13.10);
    - в этот момент RUN_STATE = 3;
    - вся частота опроса рабочая: 40–55 Гц и k·f ≥ порог + допуск (f ≥ 49,9);
    - следующий отсчёт не ниже порога и не позже next_max_gap_sec.
    Уходит только строка оборотов — частота и мощность того же опроса остаются.
    held_freq — удержанная частота перед окном истории (П10), gaps — дыры окна
    вместе с дырами от удержанной строки до окна.
    """
    from .rpm_freq import build_poll, is_glitch, rpm_freq_params
    prm = rpm_freq_params(cfg)
    if not prm["enabled"]:
        return by_addr, []
    rpm_addr = cfg.role_to_addr("RPM")
    rows = by_addr.get(rpm_addr) if rpm_addr is not None else None
    if not rows:
        return by_addr, []
    real = [r for r in rows if _fv(r) is not None and not r.get("is_carried_forward")]
    if not any(prm["min_rpm"] < _fv(r) < prm["threshold_rpm"] for r in real):
        return by_addr, []
    run3 = [(_tz(p["state_start"]), _tz(p["state_end"]) if p.get("state_end") else None)
            for p in enum_periods or []
            if p.get("addr") == 40011 and p.get("value") is not None and int(p["value"]) == 3]
    if not run3:
        return by_addr, []
    poll = build_poll(by_addr, cfg, prm, held_freq, gaps)
    if poll is None:
        return by_addr, []

    drop_ids: set[int] = set()
    found: list[tuple[datetime, float, float | None]] = []
    for k, r in enumerate(real):
        v = _fv(r)
        if not (prm["min_rpm"] < v < prm["threshold_rpm"]):
            continue
        ts = _tz(r["ts"])
        if not any(s <= ts and (e is None or ts < e) for s, e in run3):
            continue
        nxt = real[k + 1] if k + 1 < len(real) else None
        nxt_p = (_tz(nxt["ts"]), _fv(nxt)) if nxt is not None else None
        fvals = poll.window(ts, nxt_p[0] if nxt_p else None)
        if is_glitch(v, fvals, nxt_p, ts, prm):
            drop_ids.add(id(r))
            found.append((ts, v, min(fvals)))
    if not found:
        return by_addr, []
    out = dict(by_addr)
    out[rpm_addr] = [r for r in rows if id(r) not in drop_ids]
    return out, found


def _rpm_glitch_check(
    glitches: list[tuple[datetime, float, float | None]], t0: datetime, t1: datetime, cfg,
) -> dict | None:
    """Проверка сегмента source_rpm_glitch: сколько отсчётов оборотов вырезано как сбой.

    Время в тексте — МСК (у _glitch_check — UTC, этот недочёт не копируем);
    моменты лежат и в samples, отчёт перепечатывает их в своём поясе. При
    remark_min_count и больше за сегмент — passed=False (В4): строка в
    «Замечаниях», объекту на заметку; на вердикт не влияет (_DATA_QUALITY_CHECKS)."""
    from .rpm_freq import glitch_details, rpm_freq_params
    inside = [g for g in glitches if t0 <= g[0] < t1]
    if not inside:
        return None
    samples = [{"t": t.isoformat(), "rpm": v, "hz": f} for t, v, f in inside]
    return {
        "check": "source_rpm_glitch",
        "passed": len(inside) < rpm_freq_params(cfg)["remark_min_count"],
        "count": len(inside),
        "samples": samples,
        "details": glitch_details(samples),
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
# v5.1.23 (решение В4, пересмотр 13.10): подпись плашки — по прокрутке, а не
# по режиму панели: «Неудачный пуск · Стоп/Авария» / «Отменённый пуск · Стоп»
# (labels.attempt_label); в записи — наблюдаемое: прокрутка, АКБ, 40010 и
# 40599 в кадре падения (_attempt_observed).
# v5.1.25 (разбор ДЭС №3 24.10): бит готовности, переживший стоп, и бит при
# оборотах/частоте ниже порогов выход не подтверждают (confirm_exit, П1/П2);
# попытка без отсчётов оборотов внутри — cranked None, «Неудачный пуск»
# (_attempt_record, решение А); профиль пуска без перехода порогов не
# строится (start_profile, П3).

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
        # Прокрутка стартером на плашке попытки (v5.1.23, решение В4)
        "crank_rpm_min": float(g("crank_rpm_min", 50.0)),
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
    ready_since: datetime | None = None,
) -> tuple[str, datetime | None, dict[str, Any]]:
    """Подтвердился ли режим на [t0, t1): ('confirmed', момент, факты),
    ('unconfirmed', None, факты) или ('no_data', None, факты).

    Показание пишется по изменению: если в коротком отрезке строк нет, значение
    держится с последнего показания до t0 (не старше carry_sec). Без единого
    показания оборотов и частоты, или когда отрезок больше чем наполовину без
    связи, исход не объявляется (решение 13.10: «нет данных» — не неудача).

    v5.1.25 (разбор ДЭС №3 24.10, П1/П2): бит «Готов к нагрузке» держался с
    13.10 через стоп при 0 об/мин и ложно подтвердил попытку 12:41:05 (#36068
    «Работа» 8 с вместо плашки). Бит засчитывается, только если:
    - его период начался не раньше ready_since (П1: начало стопа перед
      цепочкой — бит, вставший раньше, пережил стоп); None — без проверки;
    - у оборотов и частоты нет ни одного показания (включая удержанное) или
      хоть одно перешло порог (П2): показания есть, а порогов не перешли — бит
      противоречит данным. С переходом порога бит засчитывается как раньше —
      момент подтверждения не сдвигается (критика M6).
    Отброшенный бит — facts["ready_ignored"]: "stale" (П1) / "no_rotation"
    (П2); facts["ready"] тогда False — «Готов к нагрузке был» про старое
    значение в фактах плашки не печатается.
    """
    t0, t1 = _tz(t0), _tz(t1)
    facts: dict[str, Any] = {"rpm_max": None, "freq_max": None, "ready": False}
    hits: list[datetime] = []
    evidence = False
    crossed = False
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
            crossed = True
    _since = _tz(ready_since) if ready_since is not None else None
    for s, e in ready:
        if not (s < t1 and e > t0):
            continue
        if _since is not None and s < _since:
            # П1: бит встал раньше стопа перед попыткой — пережил стоп
            facts.setdefault("ready_ignored", "stale")
            continue
        if evidence and not crossed:
            # П2: обороты или частота есть, но порогов не перешли
            facts["ready_ignored"] = "no_rotation"
            continue
        facts["ready"] = True
        facts.pop("ready_ignored", None)
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
                   lag_sec: float, until: datetime | None = None) -> list[int]:
    """Коды 40012, которые панель выставила на попытке и в лаг после падения.

    Не агрегат плашки: та тянется весь стоп после попытки, и в неё попадали бы
    и код, висевший до попытки (1248 после сброса), и кнопка через час.
    until — конец стопа после попытки (следующий выход из стопа): код, вставший
    уже на следующей попытке, не её (v5.1.23: перепрогон #35937 брал 234 разноса
    09:28:53 следующей попытки — 51 с после падения 09:28:02, в пределах лага;
    коды теперь в фактах и в подписи плашки).
    """
    out: list[int] = []
    hi = t_fall + timedelta(seconds=lag_sec)
    for p in code_periods or []:
        s = _tz(p["state_start"])
        if s < t0 or s > hi or (until is not None and s >= until):
            continue
        try:
            v = int(p.get("value") or 0)
        except (TypeError, ValueError):
            continue
        if v > 0 and v != 65535 and v not in out:
            out.append(v)
    return out


def _enum_at_fall(enum_periods: list[dict] | None, addr: int | None, t_fall: datetime,
                  cfg) -> dict[str, Any] | None:
    """Значение enum-регистра в кадре падения попытки: {value, label, at}; at —
    момент смены, если регистр сменился в этом кадре (± _SAME_FRAME_SEC), иначе
    None — значение держалось. None — регистра нет, значение неизвестно или NA."""
    if addr is None:
        return None
    hi = t_fall + timedelta(seconds=_SAME_FRAME_SEC)
    cur = None
    for p in enum_periods or []:
        if p.get("addr") != addr or p.get("state_start") is None:
            continue
        s = _tz(p["state_start"])
        if s <= hi and (cur is None or s >= _tz(cur["state_start"])):
            cur = p
    if cur is None:
        return None
    if cur.get("state_end") is not None and \
            _tz(cur["state_end"]) < t_fall - timedelta(seconds=_SAME_FRAME_SEC):
        return None                     # период кончился до падения — значение не знаем
    v = _enum_value(cur)
    if v is None or v in _na_values(cfg, addr):
        return None
    from .labels import enum_label
    s = _tz(cur["state_start"])
    return {
        "value": v,
        "label": enum_label(cfg.register_map.get(addr), v, cur.get("label")),
        "at": s.isoformat() if abs((s - t_fall).total_seconds()) <= _SAME_FRAME_SEC else None,
    }


def _attempt_observed(
    t0: datetime, t_fall: datetime, rpm: list[tuple[datetime, float]],
    all_by_addr: dict[int, list[dict]], enum_periods: list[dict] | None,
    code_periods: list[dict] | None, codes: list[int], cfg, prm: dict,
) -> dict[str, Any]:
    """Наблюдаемое на неудачной попытке (v5.1.23, решение В4) — для фактов без
    выводов: длительность прокрутки — с первого отсчёта оборотов выше
    crank_rpm_min (и удержанного перед попыткой, как в confirm_exit: #35937 —
    187 об/мин в 09:27:52 при «Работе» с 09:27:55) до падения; минимум АКБ за
    прокрутку (без неё — за попытку); переключатель 40010 и команда работы
    40599 в кадре падения; подписи кодов попытки из каталога. Всё из уже
    загруженных строк и enum-периодов окна — запросов нет.
    """
    crank_min = prm["crank_rpm_min"]
    crank_from: datetime | None = None
    carried = None
    # Отсчёты оборотов внутри попытки, без удержанного (v5.1.25, решение А):
    # нет ни одного — прокрутка по данным не видна
    rpm_in = 0
    for ts, v in rpm:
        if ts < t0:
            carried = (ts, v) if (t0 - ts).total_seconds() <= prm["carry_sec"] else None
            continue
        if ts >= t_fall:
            break
        rpm_in += 1
        if crank_from is None and v > crank_min:
            crank_from = ts
    if carried is not None and carried[1] > crank_min:
        crank_from = carried[0]
    lo = crank_from or t0
    bat = [v for ts, v in value_series(all_by_addr, cfg.role_to_addr("BATTERY_V"))
           if lo <= ts <= t_fall]
    from .labels import split_code_label
    names: dict[str, str] = {}
    for p in code_periods or []:
        v = _enum_value(p)
        if v in codes and str(v) not in names:
            nm = split_code_label(p.get("label"))[0]
            if nm:
                names[str(v)] = nm
    return {
        "rpm_in": rpm_in,
        "crank_from": crank_from.isoformat() if crank_from else None,
        "crank_sec": max(0.0, (t_fall - crank_from).total_seconds()) if crank_from else None,
        "battery_min_v": min(bat) if bat else None,
        "switch": _enum_at_fall(enum_periods, cfg.role_to_addr("SWITCH_POS"), t_fall, cfg),
        "run_command": _enum_at_fall(enum_periods, cfg.role_to_addr("RUN_COMMAND"), t_fall, cfg),
        "code_names": names,
    }


def _attempt_record(chain: list[dict], stop: dict, res: list[tuple],
                    code_periods: list[dict] | None = None,
                    lag_sec: float = 60.0, observe: dict | None = None) -> dict[str, Any]:
    """Что было на неудачной попытке — для подписи, отчёта и акта.

    observe — что нужно для наблюдаемого (v5.1.23): {rpm, all_by_addr,
    enum_periods, cfg, prm}; без него или при сбое — запись как до v5.1.23
    (подпись тогда по rpm_max фактов)."""
    path = [int(c["value"]) for c in chain]
    t0 = _tz(chain[0]["state_start"])
    t_fall = _tz(stop["state_start"])

    def _mx(key):
        vals = [r[2].get(key) for r in res if r[2].get(key) is not None]
        return max(vals) if vals else None

    rec = {
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
        "codes": _attempt_codes(code_periods or [], t0, t_fall, lag_sec,
                                _tz(stop["state_end"]) if stop.get("state_end") else None),
    }
    if observe:
        try:
            obs = _attempt_observed(
                t0, t_fall, observe["rpm"], observe["all_by_addr"], observe.get("enum_periods"),
                code_periods, rec["codes"], observe["cfg"], observe["prm"])
            # Прокрутка — rpm_max попытки (с удержанным, как в confirm_exit:
            # #35937) выше crank_rpm_min (план v5.1.23 §2.1 п.5). Не выше — «без
            # прокрутки», только если внутри попытки есть отсчёты (v5.1.25,
            # решение А): ноль из отсчёта ДО попытки (ДЭС №3 24.10 13:34, 13:35)
            # отмены не доказывает. Оборотов нет — None: что пуск отменили, не видно
            _rm = rec["facts"]["rpm_max"]
            if isinstance(_rm, (int, float)) and float(_rm) > observe["prm"]["crank_rpm_min"]:
                cranked = True
            elif obs.get("rpm_in") == 0:
                cranked = None
            elif isinstance(_rm, (int, float)):
                cranked = False
            else:
                cranked = None
            rec.update({"cranked": cranked, **obs})
        except Exception:
            logger.warning("Наблюдаемое на попытке %s не собрано — запись без него",
                           t0.isoformat(), exc_info=True)
    return rec


# ── Код 40012 на закрытии и обрыв сегмента аварией (v5.1.22, P1-2, P1-3) ────

# Кадр опроса enum-регистров: подъём «Сброса неисправностей» и снятие кода
# приходят в одном пакете, но порядок строк в нём не гарантирован — допуск в
# один кадр после снятия (критика v5.1.22, п. 2). Свойство опроса, а не порог
# аналитики, поэтому не в KB
_RESET_FRAME_SEC = 10.0
# Метки одного кадра: снятие кода и смена переключателя в одном пакете
_SAME_FRAME_SEC = 1.0


def _na_values(cfg, addr: int | None) -> set[int]:
    try:
        return {int(x) for x in ((cfg.register_map.get(addr) or {}).get("na_values") or [65535])}
    except (TypeError, ValueError, AttributeError):
        return {65535}


def _enum_value(p: dict) -> int | None:
    try:
        return int(p.get("value"))
    except (TypeError, ValueError):
        return None


def fault_code_close(
    code_periods: list[dict], enum_periods: list[dict], cfg,
    seg_start: datetime, seg_end: datetime, next_state: int | None = None,
    run_state: int | None = None, tripped: bool = False,
) -> dict[str, Any] | None:
    """Что стало с кодом 40012, державшимся до конца сегмента (v5.1.22, P1-2).

    Отчёт брал value_end последнего подсегмента. Окно полуоткрытое [t_start,
    t_end), а аварийный стоп режется ровно по снятию аварии, — код, снятый в
    t_end, считался висящим: «⚠ Несброшенная неисправность… код не сброшен
    кнопкой» в 28 сегментах из 81 за 30 дней, и заключения советовали «не
    сбрасывать» уже после пуска. Вызывается только для реза внутри окна
    (_cut_inside): рез по краю окна будущего не знает.

    Смотрит период кода, на котором окно кончилось:
      - код держался за конец сегмента (или не снят) — None, текст прежний;
      - снят не позже конца — что пришло следом: 0 («снят») или другой код
        («сменился», 11 из 28: #35861 1438 → 1248), поэтому вывод «сброшен» из
        cause_close невозможен;
      - признак сброса: подъём «Сброса неисправностей» (роль FAULT_RESET,
        PCC3300 40569) не раньше stop_kind.fault_reset_window_sec до снятия и
        не позже кадра после, но не раньше появления кода; переход переключателя
        (SWITCH_POS, 40010) в «Выкл» в кадре снятия. #35869: 40569 поднимался
        за 1 ч 47 мин до снятия 1248 — это «снят», не «сброшен».
    Код из state_periods (NA 65535 там отброшен); следом NA — неизвестно, None.
    40569 и 40010 — kind: enum, уже в enum_periods нарезки: запросов нет.

    v5.1.22, ревью verdict-2: нестоповый сегмент (run_state ≠ 0), оборванный
    аварией (tripped) или чей код вытеснен другим (следом не 0), — подъёмы
    40569 и «Выкл» 40010 позже кадра снятия не учитываются: это сброс уже
    нового кода, сделанный в стопе (ДЭС №3 16.09: 146 вытеснен 151 в 16:26:11,
    сброс 16:26:19 приписывался 146 — «сброшен» с обратным порядком времени).
    Подъёмы и до, и после — последний не позже кадра снятия.
    """
    t0, t1 = _tz(seg_start), _tz(seg_end)
    last = None
    for p in code_periods or []:
        s = _tz(p["state_start"])
        e = _tz(p["state_end"]) if p.get("state_end") else None
        if s >= t1 or (e is not None and e <= t0):
            continue
        if last is None or s >= _tz(last["state_start"]):
            last = p
    if last is None:
        return None
    code = _enum_value(last)
    if not code or code <= 0 or last.get("state_end") is None:
        return None
    t_clear = _tz(last["state_end"])
    if t_clear > t1:
        return None                     # висит за концом сегмента — «несброшенная»
    t_onset = _tz(last["state_start"])
    code_addr = last.get("addr")

    # Что пришло следом — сырой период того же регистра (NA тоже виден)
    nxt = None
    for p in enum_periods or []:
        if p.get("addr") == code_addr and p.get("state_start") is not None \
                and abs((_tz(p["state_start"]) - t_clear).total_seconds()) <= _SAME_FRAME_SEC:
            nxt = p
            break
    nv = _enum_value(nxt) if nxt is not None else None
    if nv is None or nv in _na_values(cfg, code_addr):
        return None                     # следом «нет данных» — состояние неизвестно

    try:
        window = float(cfg.seg("stop_kind", "fault_reset_window_sec", default=60) or 60)
    except (TypeError, ValueError):
        window = 60.0
    lo = max(t_clear - timedelta(seconds=window), t_onset)
    # Нестоповый сегмент, оборванный аварией, или код вытеснен другим — после
    # кадра снятия сбрасывают уже новый код (ревью verdict-2)
    _strict = run_state not in (None, 0) and (tripped or nv != 0)
    after_sec = _SAME_FRAME_SEC if _strict else _RESET_FRAME_SEC
    hi = t_clear + timedelta(seconds=after_sec)
    reset_addr = cfg.role_to_addr("FAULT_RESET")
    sw_addr = cfg.role_to_addr("SWITCH_POS")
    reset_at: datetime | None = None
    switch_at: datetime | None = None
    switch_label: str | None = None
    for p in enum_periods or []:
        a, v = p.get("addr"), _enum_value(p)
        if v is None or p.get("state_start") is None:
            continue
        s = _tz(p["state_start"])
        # Код вытеснен аварией (_strict): сброс и «Выкл» в кадре вытеснения
        # относятся к новой аварии — порядок внутри кадра опроса не различить
        # (v5.1.22, перепроверка: «Код 1451 сброшен: «Выкл»…; сразу после — 1446»)
        if _strict and s >= t_clear:
            continue
        if reset_addr is not None and a == reset_addr and v != 0 \
                and v not in _na_values(cfg, a) and lo <= s <= hi:
            if reset_at is None or s > reset_at:
                reset_at = s
        elif sw_addr is not None and a == sw_addr and v == 0 \
                and -_RESET_FRAME_SEC <= (s - t_clear).total_seconds() <= after_sec:
            if switch_at is None or abs((s - t_clear).total_seconds()) < abs((switch_at - t_clear).total_seconds()):
                switch_at = s
                lab = p.get("label")
                switch_label = str(lab) if lab and not str(lab).isdigit() else None

    from .labels import split_code_label
    name, kind = split_code_label(last.get("label"))
    n_name, n_kind = split_code_label(nxt.get("label")) if nv else (None, None)
    return {
        "code": code, "name": name, "kind": kind,
        "t_onset": t_onset.isoformat(), "t_clear": t_clear.isoformat(),
        "reset": reset_at is not None or switch_at is not None,
        "reset_at": reset_at.isoformat() if reset_at else None,
        "switch_off_at": switch_at.isoformat() if switch_at else None,
        "switch_label": switch_label,
        "next_code": nv, "next_name": n_name, "next_kind": n_kind,
        "at_end": abs((t_clear - t1).total_seconds()) <= _SAME_FRAME_SEC,
        "next_state": next_state,
    }


def trip_record(
    t_moment: datetime, code_periods: list[dict], fault_periods: list[dict], cfg,
) -> dict[str, Any]:
    """Что панель записала в момент аварийного останова (v5.1.22, P1-3): коды
    40012, вставшие в пределах CONTROLLER_FAULT.code_onset_tol_sec от момента
    (как у детектора панели), с метками каталога, и маски аварийной тяжести.

    Общая для нарезки (отметка сегмента, оборванного аварией) и поздней
    доработки сводки движком (online.engine: следующий сегмент стал аварийным
    после закрытия) — подпись должна быть одна.
    """
    t = _tz(t_moment)
    try:
        tol = float(cfg.det("CONTROLLER_FAULT", "code_onset_tol_sec", default=15.0) or 15.0)
    except (TypeError, ValueError, AttributeError):
        tol = 15.0
    from .labels import split_code_label
    codes: list[dict[str, Any]] = []
    for p in code_periods or []:
        if p.get("state_start") is None:
            continue
        v = _enum_value(p)
        if not v or v <= 0 or v in _na_values(cfg, p.get("addr")):
            continue
        if abs((_tz(p["state_start"]) - t).total_seconds()) > tol:
            continue
        if any(c["code"] == v for c in codes):
            continue
        name, kind = split_code_label(p.get("label"))
        codes.append({"code": v, "name": name, "kind": kind})
    masks: list[str] = []
    for fp in fault_periods or []:
        if cfg.bitmap_severity(fp.get("severity") or "none") != "SHUTDOWN":
            continue
        if abs((_tz(fp["fault_start"]) - t).total_seconds()) > tol:
            continue
        nm = fp.get("fault_name_ru") or fp.get("fault_name")
        if nm and nm not in masks:
            masks.append(nm)
    return {"t": t.isoformat(), "codes": codes, "masks": masks}


def _trip_mark(
    run_state: int, seg_start: datetime, seg_end: datetime,
    shutdown_iv: list[tuple[datetime, datetime]], next_period: dict | None,
    used: set, code_periods: list[dict], fault_periods: list[dict], cfg,
) -> dict[str, Any] | None:
    """Отметка нестопового сегмента, оборванного аварийным остановом (v5.1.22, P1-3).

    #35939 (16.10, «Работа» 20 с, затем 234) получила «🟢 НОРМА» и заключение
    «все параметры в норме»: разгон до рабочих оборотов — переходная часть без
    детекторов, а о следующем аварийном стопе вход не знал. Отметка ставится,
    если на конце сегмента началась авария панели (интервал shutdown_intervals:
    маска аварийной тяжести или 40013 = Shutdown / Shutdown with Cooldown;
    начало ∈ (t_start, t_end] и не дальше code_onset_tol_sec от конца, за концом
    — с тем же допуском: маска приходит кадром позже 40011) или следом идёт
    аварийный стоп.
    Одна авария — одна отметка (used): «Работа» → «Охлаждение» при 40013 = 3
    отмечает только «Работу», управляемое охлаждение не оборвано. Плашки
    попыток (стоп-сегменты со своей подписью «· Авария») не отмечаются, и
    сегмент перед плашкой её аварией тоже.

    v5.1.22, ревью:
    - verdict-5: следом ПРОСТОЙ стоп — отметки нет. Допуск за концом (маска
      кадром позже 40011) у отметки, а вид стопа строгий (авария держится в
      момент начала стопа): «прервана аварийным остановом» стояла рядом с
      простым стопом без акта и совета. Поздняя доработка движком ждёт, пока
      стоп станет аварийным;
    - verdict-4: в записи — начало интервала аварии (iv); used сеется из
      звеньев контекста (отметки сегментов, закрытых прошлыми вызовами), и
      правило «одна авария — одна отметка» не зависит от разбиения на вызовы
      (живой движок закрывает «Работу» и «Охлаждение» разными циклами).
    """
    if run_state == 0:
        return None
    if (next_period is not None and _enum_value(next_period) == 0
            and not next_period.get("attempt")
            and next_period.get("stop_kind") != "EMERGENCY"):
        return None
    t0, t1 = _tz(seg_start), _tz(seg_end)
    try:
        tol = float(cfg.det("CONTROLLER_FAULT", "code_onset_tol_sec", default=15.0) or 15.0)
    except (TypeError, ValueError, AttributeError):
        tol = 15.0
    # Начало аварии — у конца сегмента: допуск за концом — на запаздывание маски
    # после смены 40011, до конца — на обратный порядок строк в кадре (за 60
    # дней 23 из 23 — одним кадром). Маска аварийной тяжести посреди долгой
    # работы без останова сегмент не обрывает. Следом плашка неудачной попытки —
    # её авария случилась на попытке, а не здесь (задержка пуска → прогрев 10 с
    # → 1438): допуска за концом нет
    lo = max(t0, t1 - timedelta(seconds=tol))
    hi = t1 if (next_period or {}).get("attempt") else t1 + timedelta(seconds=tol)

    def _taken(s: datetime) -> bool:
        # Начало из прошлого вызова — из ISO звена контекста: сверка в кадр
        return any(abs((s - u).total_seconds()) <= _SAME_FRAME_SEC for u in used)

    onset = next((s for s, _e in shutdown_iv if lo < s <= hi and not _taken(s)), None)
    nxt_emerg = bool(
        next_period is not None and _enum_value(next_period) == 0
        and next_period.get("stop_kind") == "EMERGENCY" and not next_period.get("attempt")
        and abs((_tz(next_period["state_start"]) - t1).total_seconds()) <= _SAME_FRAME_SEC)
    cover = None
    if onset is None and nxt_emerg:
        # Аварийность стопа — от интервала, начавшегося раньше: если его уже
        # отдали предыдущему сегменту («Работа» при 40013 = 3), «Охлаждение»
        # перед стопом не оборвано
        t_next = _tz(next_period["state_start"])
        cover = next((s for s, e in shutdown_iv if s <= t_next < e), None)
        if cover is not None and _taken(cover):
            return None
        if cover is not None:
            used.add(cover)
    elif onset is None:
        return None
    if onset is not None:
        used.add(onset)
    moment = min(onset, t1) if onset is not None else t1
    rec = trip_record(moment, code_periods, fault_periods, cfg)
    rec["run_state"] = int(run_state)
    rec["next_stop_kind"] = (next_period or {}).get("stop_kind")
    rec["by"] = "onset" if onset is not None else "next_emergency"
    _iv = onset if onset is not None else cover
    if _iv is not None:
        rec["iv"] = _iv.isoformat()       # начало интервала аварии (ревью verdict-4)
    return rec


def _fall_trace(
    by_addr: dict[int, list[dict]], enum_periods: list[dict], cfg,
    seg_start: datetime, seg_end: datetime, stop: datetime, data_to: datetime,
    run_state: Any, label: str | None, held: dict[str, tuple[datetime, float]] | None,
) -> dict[str, Any] | None:
    """Лента отсчётов сегмента падения (v5.1.22, P1-4, решение В2): от
    max(начала сегмента, останов − act_trace_before_sec) до останов +
    act_trace_after_sec, но не дальше истории окна. Из уже загруженной истории —
    без чтения; акт следующего стопа берёт её отсюда (engine._build_act)."""
    from .reconstructor import build_analog_trace, trace_params
    prm = trace_params(cfg)
    stop = _tz(stop)
    t_from = max(_tz(seg_start), stop - timedelta(seconds=prm["before"]))
    t_to = min(stop + timedelta(seconds=prm["after"]), _tz(data_to))
    if t_to < t_from:
        t_to = stop
    try:
        rs = int(run_state) if run_state is not None else None
    except (TypeError, ValueError):
        rs = None
    if not label:
        # Подпись режима — как в отчёте («Работа»), а не метка каталога панели
        # («Номин. частота/напряжение»)
        from .serializer import RUN_STATE_RU
        label = RUN_STATE_RU.get(rs, str(rs) if rs is not None else None)
    return build_analog_trace(
        by_addr, enum_periods, cfg, t_from, stop, t_to,
        segment={"run_state": rs, "label": label, "t_start": _tz(seg_start).isoformat(),
                 "t_end": _tz(seg_end).isoformat()},
        held=held)


def _engine_hours_at(all_by_addr: dict[int, list[dict]], cfg, t: datetime,
                     held: dict[str, tuple[datetime, float]] | None) -> float | None:
    """Наработка по счётчику на момент t (v5.1.22): последняя строка ENGINE_HOURS
    до t в окне истории (единицы карты — часы, _apply_scale), иначе удержанное
    значение посева перед окном (engine._load_seed). Счётчик пишется по
    изменению и «пульсом» раз в 15 мин: у сегментов короче пульса строки нет, и
    модель выдумывала наработку — «680 ч» (длительность бита «Не в автомате»)
    при 13 643 ч у ДГУ №1, «955 моточасов» (счётчик пусков) у ДЭС №3."""
    addr = cfg.role_to_addr("ENGINE_HOURS")
    t = _tz(t)
    if addr is not None:
        best = None
        for r in all_by_addr.get(addr) or []:
            if _tz(r["ts"]) >= t:
                break
            if r.get("value") is not None and not r.get("is_carried_forward"):
                best = r
        if best is not None:
            return _fv(best)
    h = (held or {}).get("ENGINE_HOURS")
    if h is not None and _tz(h[0]) < t:
        return float(h[1])
    return None


def _ready_since(periods: list[dict], i: int) -> datetime:
    """С какого момента бит «Готов к нагрузке» засчитывается цепочке, которая
    начинается periods[i] (v5.1.25, П1, разбор ДЭС №3 24.10).

    Есть стоп перед цепочкой — его начало: задержка пуска (1) пропускается, как
    у last в _confirm_attempts. Стоп до окна (цепочка на голове окна) — начало
    цепочки: бит, вставший раньше попытки, когда машина известно стояла, —
    старый. Допуск в кадр опроса: бит и 40011 приходят пакетами, и метка бита
    бывает на доли секунды раньше смены режима (критика v5.1.25, SHOULD 8).
    По истории ДЭС №3 бит гас до перехода 40011 в стоп в 32 циклах из 33, а у
    настоящего пуска встаёт на номинале — через секунды после прокрутки."""
    k = i - 1
    while k >= 0 and int(periods[k]["value"]) == 1:
        k -= 1
    if k >= 0 and int(periods[k]["value"]) == 0:
        return _tz(periods[k]["state_start"])
    return _tz(periods[i]["state_start"]) - timedelta(seconds=_SAME_FRAME_SEC)


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
    enum_periods: list[dict] | None = None,
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
    enum_periods — переключатель 40010 и команда 40599 в кадре падения
    попытки (v5.1.23, решение В4), только для записи плашки.
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
        try:
            ready_since = _ready_since(periods, i)
        except Exception:
            logger.warning("Начало стопа перед попыткой не найдено — бит готовности без П1",
                           exc_info=True)
            ready_since = None
        res = []
        for c in chain:
            c0 = _tz(c["state_start"])
            c1 = min(_tz(c["state_end"]), t_res) if c.get("state_end") else t_res
            res.append(confirm_exit(c0, c1, rpm, freq, ready, gaps, prm, ready_since=ready_since))

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
            plate["attempt"] = _attempt_record(
                chain, stop, res, code_periods, lag,
                observe={"rpm": rpm, "all_by_addr": all_by_addr, "enum_periods": enum_periods,
                         "cfg": cfg, "prm": prm})
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


# ── Профиль пуска (v5.1.23, п.3, решение владельца В1 17.10.2026) ──────────────
# ДГУ №1 16.10: заброс 1696 об/мин при задании 30,6 → 55 Гц, через три минуты —
# раскачка ±1,5 Гц почти две минуты; ДГУ №2 15.10 12:42 — заброс 1606 и провал
# 1310 при задании до 42,3 Гц, а гейт снял эпизод как переходный процесс. Пуск
# без нагрузки, автомат разомкнут — аналитика работы видела только «просадку».
# Профиль — факты окна пуска одной записью: строка «Пуск» в отчёте, пометка
# «на пуске» в сводке и акте, запрет снятия гейтом (status_assembler,
# start_unstable). Отдельного замечания нет: цвет и число видов он не меняет.

def _joint_tol(cfg) -> float:
    try:
        return float(cfg.seg("transitions", "joint_tol_sec", default=2.0) or 2.0)
    except Exception:
        return 2.0


def start_profile_params(cfg) -> dict[str, Any]:
    """Пороги профиля пуска (KB: segmentation.yaml → start_profile). «Схватил» и
    выход на номинал — пороги подтверждения выхода из стопа (confirm_params),
    номинал — как у сверки оборотов с частотой (RPM_UNDERSPEED.rated_rpm,
    LOAD_STEP.freq_nominal_hz): один источник."""
    def g(key: str, default: Any) -> Any:
        try:
            v = cfg.seg("start_profile", key, default=default)
        except Exception:
            v = default
        return default if v is None else v

    def d(scen: str, key: str, default: float) -> float:
        try:
            v = cfg.det(scen, key, default=default)
            return float(default if v is None else v)
        except Exception:
            return float(default)

    cp = confirm_params(cfg)
    return {
        "enabled": bool(g("enabled", True)),
        "after_rated_sec": float(g("after_rated_sec", 300.0)),
        "rundown_rpm": float(g("rundown_rpm", 1000.0)),
        "swing_out_hz": float(g("swing_out_hz", 1.0)),
        "swing_in_hz": float(g("swing_in_hz", 0.5)),
        "swing_rpm_tol": float(g("swing_rpm_tol", 30.0)),
        "swing_pair_sec": float(g("swing_pair_sec", 10.0)),
        "unstable_swing_sec": float(g("unstable_swing_sec", 30.0)),
        "unstable_flips": int(g("unstable_flips", 2)),
        "fref_low_hz": float(g("fref_low_hz", 48.5)),
        "fref_high_hz": float(g("fref_high_hz", 51.5)),
        "fref_valid_min_hz": float(g("fref_valid_min_hz", 40.0)),
        "fref_hold_max_sec": float(g("fref_hold_max_sec", 1800.0)),
        # Один кадр опроса — стык переходов (номинал в кадре смены режима, ревью R14)
        "frame_tol_sec": _joint_tol(cfg),
        "rpm_min": cp["rpm_min"],
        "freq_min": cp["freq_min"],
        "carry_sec": cp["carry_sec"],
        "rated_rpm": cp["rated_rpm"],
        "rated_freq": cp["rated_freq"],
        "rundown_max_sec": cp["rundown_max_sec"],
        "nominal_rpm": d("RPM_UNDERSPEED", "rated_rpm", 1500.0),
        "nominal_hz": d("LOAD_STEP", "freq_nominal_hz", 50.0),
    }


def _start_origin(
    periods: list[dict], k: int, ctx_links: list | None, tf: datetime, tol: float = 2.0,
) -> tuple[datetime, datetime | None] | None:
    """(начало прокрутки, «схватил») пуска, которым начинается «Работа»
    periods[k]; None — «Работа» не пуск.

    Пуск — выход из стопа, подтверждённый фактом (_confirm_attempts):
    - сама «Работа» подтвердила выход (confirmed_at; со склеенным куском
      прогрева — начало прокрутки у склейки);
    - «Работа» сразу за прогревом (ХХ), которым машина вышла из стопа: ДГУ №2
      20.09 16:34 — 5 мин на 900 об/мин, затем номинал. Прогрев ищется в
      периодах окна, а на голове окна — в звеньях контекста (живой движок
      открывает окно на начале «Работы», прогрев уже закрыт); «схватил» тогда
      ищется по оборотам с начала прогрева (преамбула окна).
    Режим, начавшийся до окна (суточный рез), — не пуск: окно пуска через рез
    не дописывается."""
    p = periods[k]
    if p.get("attempt") or p.get("attempt_pending"):
        return None
    p0 = _tz(p["state_start"])
    if p0 < tf - timedelta(seconds=tol):
        return None
    if p.get("confirmed_at") is not None:
        return p0, _tz(p["confirmed_at"])
    chain: list[tuple[int | None, datetime, datetime | None, Any]] = []
    for q in reversed(periods[:k]):
        chain.append((int(q["value"]), _tz(q["state_start"]),
                      _tz(q["state_end"]) if q.get("state_end") else None, q.get("confirmed_at")))
    for lk in reversed(ctx_links or []):
        chain.append((lk.run_state, _tz(lk.t_start), _tz(lk.t_end), None))
    crank: datetime | None = None
    fire: datetime | None = None
    t_next = p0
    for v, a, b, conf in chain:
        if b is None or abs((b - t_next).total_seconds()) > tol:
            return None
        if v == 0:
            return (crank, fire) if crank is not None else None
        if v == 2:
            crank, fire = a, (_tz(conf) if conf is not None else None)
        elif v != 1:
            return None
        t_next = a
    return None


def _row_at(rows: list[dict], t: datetime) -> int:
    """Индекс первой строки не раньше t. Строки адреса в окне идут по метке
    (сортирует БД; на этом же стоят срезы _slice_by_addr)."""
    return bisect.bisect_left(rows, _tz(t), key=lambda r: _tz(r["ts"]))


def _iter_values(by_addr: dict[int, list[dict]], addr: int | None, a: datetime,
                 b: datetime | None = None):
    """Реальные показания регистра (ts, value) в [a, b) по порядку — без
    forward-fill. Срез по метке: профиль пуска не разбирает всю историю окна
    (живой движок пересчитывает его каждый цикл по окну до суток)."""
    rows = (by_addr.get(addr) or []) if addr else []
    j = len(rows) if b is None else _row_at(rows, b)
    for k in range(_row_at(rows, a), j):
        r = rows[k]
        if r.get("is_carried_forward"):
            continue
        v = _fv(r)
        if v is not None:
            yield _tz(r["ts"]), v


def _last_value(by_addr: dict[int, list[dict]], addr: int | None, t: datetime,
                max_age: float | None = None) -> tuple[datetime, float] | None:
    """Последнее реальное показание не позже t (не старше max_age)."""
    rows = (by_addr.get(addr) or []) if addr else []
    t = _tz(t)
    k = bisect.bisect_right(rows, t, key=lambda r: _tz(r["ts"])) - 1
    while k >= 0:
        r = rows[k]
        v = None if r.get("is_carried_forward") else _fv(r)
        if v is not None:
            ts = _tz(r["ts"])
            if max_age is not None and (t - ts).total_seconds() > max_age:
                return None
            return ts, v
        k -= 1
    return None


def _value_at(by_addr: dict[int, list[dict]], addr: int | None, t: datetime,
              max_age: float | None = None,
              held: tuple[datetime, float] | None = None) -> tuple[datetime, float] | None:
    """Значение в силе на момент t: последнее реальное показание не позже t (не
    старше max_age); строк до t в окне истории нет — удержанное перед окном
    (посев движка, held), если оно не позже t и не старше max_age.

    Ревью v5.1.23 (R7): живой движок открывает окно «Работы» на её начале, а
    история — с преамбулой 300 с; у цепочки 0→2→3 прокрутка — начало прогрева,
    и задание 30 Гц, записанное за секунду до неё, в окно не попадало."""
    rows = (by_addr.get(addr) or []) if addr else []
    t = _tz(t)
    k = bisect.bisect_right(rows, t, key=lambda r: _tz(r["ts"])) - 1
    while k >= 0:
        r = rows[k]
        v = None if r.get("is_carried_forward") else _fv(r)
        if v is not None:
            ts = _tz(r["ts"])
            if max_age is not None and (t - ts).total_seconds() > max_age:
                return None
            return ts, v
        k -= 1
    if held is not None:
        try:
            ts, v = _tz(held[0]), float(held[1])
        except (TypeError, ValueError, IndexError):
            return None
        if ts <= t and (max_age is None or (t - ts).total_seconds() <= max_age):
            return ts, v
    return None


class _Series:
    """Показания регистра по времени (value_series) с поиском по метке."""

    def __init__(self, series: list[tuple[datetime, float]]):
        self.s = series
        self.ts = [t for t, _ in series]

    @classmethod
    def window(cls, by_addr: dict[int, list[dict]], addr: int | None,
               a: datetime, b: datetime | None = None) -> "_Series":
        """Показания в [a, b) — срез истории окна, а не вся она."""
        return cls(list(_iter_values(by_addr, addr, a, b)))

    def near(self, t: datetime, within: float) -> float | None:
        """Ближайшее по времени показание в пределах ± within секунд."""
        best: tuple[float, float] | None = None
        i = bisect.bisect_left(self.ts, t - timedelta(seconds=within))
        while i < len(self.s) and self.ts[i] <= t + timedelta(seconds=within):
            dt = abs((self.ts[i] - t).total_seconds())
            if best is None or dt < best[0]:
                best = (dt, self.s[i][1])
            i += 1
        return best[1] if best else None

    def span(self, a: datetime, b: datetime) -> list[tuple[datetime, float]]:
        """Показания в [a, b)."""
        return self.s[bisect.bisect_left(self.ts, a):bisect.bisect_left(self.ts, b)]


# Оценка сверху времени разгона, который прошёл между отсчётами (ревью R14):
# последний отсчёт до прокрутки дальше минуты — «пульс» регистра, не опрос
_RATED_BOUND_MAX_SEC = 60.0


def _start_window_end(
    all_by_addr: dict[int, list[dict]], enum_periods: list[dict], cfg, prm: dict[str, Any],
    crank: datetime, fire: datetime, rated: datetime | None, seg_end: datetime,
    steady_to: datetime | None = None, low_from: datetime | None = None,
) -> tuple[datetime, str, datetime | None]:
    """Конец окна пуска — самое раннее из: выход на номинал (нет — «схватил»)
    + after_rated_sec; замыкание автомата (GENSET_CB_POS = 1) после начала
    прокрутки; конец режима seg_end; первый отсчёт оборотов ниже rundown_rpm
    после номинала; сброс оборотов перед охлаждением (steady_to). low_from —
    откуда искать выбег (продолжение после суточного реза: строки окна).
    → (конец окна, причина, момент замыкания автомата или None)."""
    rpm_a = cfg.role_to_addr("RPM")
    base = rated or fire
    t_cap = base + timedelta(seconds=prm["after_rated_sec"])
    ends: list[tuple[datetime, str]] = [(t_cap, "rated_300"), (seg_end, "state_end")]
    cb_addr = cfg.role_to_addr("GENSET_CB_POS")
    cb_closed: datetime | None = None
    if cb_addr is not None:
        for p in sorted((p for p in enum_periods or [] if p.get("addr") == cb_addr
                         and p.get("state_start") is not None),
                        key=lambda p: _tz(p["state_start"])):
            if _enum_value(p) == 1 and _tz(p["state_start"]) >= crank:
                cb_closed = _tz(p["state_start"])
                break
    if cb_closed is not None:
        ends.append((cb_closed, "breaker"))
    if rated is not None:
        # Позже номинал + 5 мин выбег конец окна уже не решает
        _a = rated if low_from is None else max(rated, _tz(low_from))
        low = next((t for t, v in _iter_values(all_by_addr, rpm_a, _a, min(seg_end, t_cap))
                    if t > rated and v < prm["rundown_rpm"]), None)
        if low is not None:
            ends.append((low, "rundown"))
        if steady_to is not None and _tz(steady_to) > rated:
            ends.append((_tz(steady_to), "rundown"))
    t_to, end_by = min(ends, key=lambda e: e[0])
    return max(t_to, fire), end_by, cb_closed


def _iso_dt(v: Any) -> datetime | None:
    if v is None:
        return None
    try:
        return _tz(v if isinstance(v, datetime) else datetime.fromisoformat(str(v)))
    except (TypeError, ValueError):
        return None


def start_profile_carry(
    prev: dict[str, Any] | None,
    all_by_addr: dict[int, list[dict]],
    enum_periods: list[dict],
    cfg,
    p0: datetime,
    tf: datetime,
    seg_end: datetime,
    open_end: bool = False,
    steady_to: datetime | None = None,
) -> dict[str, Any] | None:
    """Профиль пуска «Работы»-предка у «Работы», продолжающейся через суточный
    рез (ревью v5.1.23, R1). None — профиля нет или он не этой «Работы».

    Окно пуска через рез не дописывается (план §2.1): профиль своей строки у
    продолжения не считается, строки «Пуск» в отчёте нет. Но эпизод, начавшийся
    в окне неустойчивого пуска до 09:00, после реза идёт мостом, и гейт новой
    строки без профиля вправе был бы его снять — это запрет (В1, В3). Профиль
    предка переносится как есть (факты окна до реза, unstable и причины);
    окно, обрезанное резом (end_by state_end на начале окна), — с концом без
    обрезки: номинал (нет — «схватил») + 5 мин, автомат, конец режима, выбег —
    по строкам этого окна. Из уже загруженной истории окна, без чтения.
    p0 — начало периода «Работы» (до окна), tf — начало окна (рез)."""
    if not isinstance(prev, dict):
        return None
    tol = _joint_tol(cfg)
    crank, t_prev = _iso_dt(prev.get("crank_at")), _iso_dt(prev.get("t_to"))
    p0, tf, seg_end = _tz(p0), _tz(tf), _tz(seg_end)
    # Профиль этой «Работы»: прокрутка не позже её начала, окно — не раньше
    if crank is None or t_prev is None or crank > p0 + timedelta(seconds=tol) \
            or t_prev < p0 - timedelta(seconds=tol):
        return None
    out = dict(prev, carried=True, cut_at=tf.isoformat(), open=False)
    if prev.get("end_by") == "state_end" and abs((t_prev - tf).total_seconds()) <= tol:
        prm = start_profile_params(cfg)
        fire = _iso_dt(prev.get("fire_at")) or crank
        rated = _iso_dt(prev.get("rated_at"))
        if rated is None:
            # До реза номинала не было — ищем в строках этого окна
            for addr, thr in ((cfg.role_to_addr("RPM"), prm["rated_rpm"]),
                              (cfg.role_to_addr("FREQUENCY"), prm["rated_freq"])):
                hit = next((t for t, v in _iter_values(all_by_addr, addr, tf, rated or seg_end)
                            if v >= thr), None)
                if hit is not None and (rated is None or hit < rated):
                    rated = hit
        t_to, end_by, cb_closed = _start_window_end(all_by_addr, enum_periods, cfg, prm, crank,
                                                    fire, rated, seg_end, steady_to, low_from=tf)
        out.update(t_to=t_to.isoformat(), end_by=end_by,
                   open=bool(open_end and end_by == "state_end"))
        if cb_closed is not None and cb_closed <= t_to and not prev.get("breaker_closed_at"):
            out["breaker_closed_at"] = cb_closed.isoformat()
    return out


def _r3(v: float | None) -> float | None:
    """Показание для записи в JSON: без хвоста двоичной дроби (56,800000000000004)."""
    return round(float(v), 3) if v is not None else None


def _rng(vals: list[float]) -> list[float] | None:
    return [_r3(min(vals)), _r3(max(vals))] if vals else None


def start_profile(
    all_by_addr: dict[int, list[dict]],
    enum_periods: list[dict],
    cfg,
    crank: datetime,
    fire: datetime | None,
    seg_end: datetime,
    open_end: bool = False,
    steady_to: datetime | None = None,
    held: dict[str, tuple[datetime, float]] | None = None,
    gaps: list[dict] | None = None,
) -> dict[str, Any] | None:
    """Профиль пуска (v5.1.23, п.3, решение В1, вариант «б»).

    Окно — от «схватил» (fire; нет — первый отсчёт оборотов > rpm_min или
    частоты > freq_min с начала прокрутки) до самого раннего из: выход на
    номинал + after_rated_sec; замыкание автомата (GENSET_CB_POS = 1); конец
    режима (seg_end — и суточный рез: окно через него не дописывается); первый
    отсчёт оборотов ниже rundown_rpm после номинала; сброс оборотов перед
    охлаждением (steady_to, _steady_window: 16.10 09:32:32 — 1067 об/мин / 35 Гц
    за секунду до «Охлаждения» удлинял раскачку на минуту).
    Поля: выход на номинал за N с от начала прокрутки; обороты и частота
    мин–макс от номинала (не вышел — от «схватил»); раскачка — от первого
    выхода частоты за номинал ± swing_out_hz, подтверждённого оборотами того же
    знака дальше swing_rpm_tol, до входа в номинал ± swing_in_hz после
    последнего такого выхода; смены знака (выше / ниже номинал ± swing_in_hz);
    задание частоты — с начала прокрутки (значение на её начало и строки окна):
    последовательность, размах значений ≥ fref_valid_min_hz (range) и отдельно
    по строкам после номинала (range_rated), наибольшее с прокрутки и
    наименьшее после номинала; заброс — метки RPM_OVERSPEED в окне
    (rpm_freq.over_marks); автомат, кВт, масло на начало.
    Неустойчив (вариант «б»): заброс, раскачка ≥ unstable_swing_sec, смен
    знака ≥ unstable_flips, задание ниже fref_low_hz после номинала или выше
    fref_high_hz с начала прокрутки. open_end — режим идёт (живой край окна):
    окно пуска ещё растёт, профиль пересчитывается каждый цикл.
    Из уже загруженной истории окна, срезами по метке — без чтения и без
    разбора всего окна. None — выключено в KB, оборотов и частоты нет или они
    порогов не перешли (v5.1.25, П3: «схватил» по одному биту готовности —
    профиля нет; ДЭС №3 24.10 12:41:05 — «до номинала не вышел» при 0 об/мин).
    unobserved (ревью v5.1.25, Q5) — пуск прошёл без связи: прокрутка на конце
    разрыва data_gaps, первый отсчёт после него уже выше порога; масла перед
    пуском нет, автомат в тексте — без момента (labels.start_profile_text)."""
    prm = start_profile_params(cfg)
    if not prm["enabled"]:
        return None
    crank, seg_end = _tz(crank), _tz(seg_end)
    rpm_a, frq_a = cfg.role_to_addr("RPM"), cfg.role_to_addr("FREQUENCY")
    _hd = held or {}
    # Поиск «схватил» и номинала — до первого попадания, без разбора всего окна.
    # Значение на начало прокрутки — и удержанное перед окном (ревью R7: окно
    # живого движка начинается с «Работы», прогрев — до преамбулы истории).
    # Переход порогов ищется всегда, и при переданном fire (confirmed_at): тот
    # мог дать один бит готовности (П3)
    hits = []
    for addr, thr, role in ((rpm_a, prm["rpm_min"], "RPM"), (frq_a, prm["freq_min"], "FREQUENCY")):
        c = _value_at(all_by_addr, addr, crank, prm["carry_sec"], _hd.get(role))
        hit = (crank if c is not None and c[1] > thr else
               next((t for t, v in _iter_values(all_by_addr, addr, crank, seg_end) if v > thr), None))
        if hit is not None:
            hits.append(hit)
            if fire is not None:
                break                   # fire известен — достаточно, что порог перешли
    if not hits:
        return None
    fire = fire or min(hits)
    fire = max(_tz(fire), crank)
    # Пуск без связи (ревью v5.1.25, Q5): прокрутка — на конце разрыва
    # data_gaps (в пределах кадра опроса), а первый же отсчёт после него уже
    # выше порога — самого пуска не видели (ДЭС №3 30.09: разрыв 15:31:56–
    # 20:33:17, в 20:33:17 сразу 1496 об/мин, автомат замкнут, масло 93 °C).
    # Профиль остаётся (строка «Пуск» у каждого пуска, В1), но с пометкой, без
    # «масла перед пуском» и момента автомата — это уже работающая машина
    unobserved = False
    try:
        if crank in hits:
            _ft = prm["frame_tol_sec"]
            for g in gaps or []:
                gs, ge = _iso_dt(g.get("gap_start")), _iso_dt(g.get("gap_end"))
                if (gs is not None and ge is not None and gs < crank
                        and abs((ge - crank).total_seconds()) <= _ft):
                    unobserved = True
                    break
    except Exception:
        logger.warning("Пуск в разрыве связи не проверен — профиль как раньше", exc_info=True)
        unobserved = False

    # Выход на номинал: обороты ≥ рабочих или частота ≥ рабочей (как у рабочей
    # части «Работы», _steady_window)
    rated: datetime | None = None
    for addr, thr in ((rpm_a, prm["rated_rpm"]), (frq_a, prm["rated_freq"])):
        hit = next((t for t, v in _iter_values(all_by_addr, addr, fire, rated or seg_end) if v >= thr), None)
        if hit is not None and (rated is None or hit < rated):
            rated = hit
    base = rated or fire

    # Конец окна — самое раннее из номинал + 5 мин, автомата, конца режима, выбега
    t_to, end_by, cb_closed = _start_window_end(all_by_addr, enum_periods, cfg, prm, crank, fire,
                                                rated, seg_end, steady_to)

    # Номинал в одном кадре опроса со сменой режима (ревью R14): разгон прошёл
    # между отсчётами — «за 0 с» была бы ложной точностью. Оценка сверху — от
    # последнего отсчёта оборотов или частоты до прокрутки (не дальше минуты)
    rated_first, rated_bound = False, None
    if rated is not None and (rated - crank).total_seconds() <= prm["frame_tol_sec"] and not any(
            True for addr in (rpm_a, frq_a) for _ in _iter_values(all_by_addr, addr, crank, rated)):
        rated_first = True
        _before = [x for x in (_value_at(all_by_addr, a, crank - timedelta(microseconds=1), None, _hd.get(r))
                               for a, r in ((rpm_a, "RPM"), (frq_a, "FREQUENCY"))) if x is not None]
        if _before:
            _b = (rated - max(ts for ts, _ in _before)).total_seconds()
            if 0 < _b <= _RATED_BOUND_MAX_SEC:
                rated_bound = round(_b, 1)

    # Ряды окна пуска: обороты с запасом на поиск ближайшего (± swing_pair_sec)
    _pair = timedelta(seconds=prm["swing_pair_sec"])
    rpm = _Series.window(all_by_addr, rpm_a, crank - _pair, t_to + _pair)
    freq = _Series.window(all_by_addr, frq_a, base, t_to)
    rpm_w, freq_w = rpm.span(base, t_to), freq.s
    out: dict[str, Any] = {
        "v": 1,
        "crank_at": crank.isoformat(),
        "fire_at": fire.isoformat(),
        "rated_at": rated.isoformat() if rated else None,
        "rated_sec": round((rated - crank).total_seconds(), 1) if rated else None,
        # Номинал — в кадре смены режима: разгон не виден (ревью R14)
        "rated_first": rated_first,
        "rated_bound_sec": rated_bound,
        "t_to": t_to.isoformat(),
        "end_by": end_by,
        "open": bool(open_end and end_by == "state_end"),
        "rpm": _rng([v for _, v in rpm_w]),
        "freq": _rng([v for _, v in freq_w]),
        "swing_sec": 0.0, "swing_from": None, "swing_to": None, "flips": 0,
    }

    # Раскачка и смены знака — только после выхода на номинал (разгон — не раскачка)
    if rated is not None:
        f0, r0 = prm["nominal_hz"], prm["nominal_rpm"]
        outs: list[datetime] = []
        side = flips = 0
        for t, v in freq_w:
            dev = v - f0
            if abs(dev) > prm["swing_out_hz"]:
                r = rpm.near(t, prm["swing_pair_sec"])
                if r is not None and abs(r - r0) > prm["swing_rpm_tol"] and (dev > 0) == (r > r0):
                    outs.append(t)
            sd = 1 if dev > prm["swing_in_hz"] else (-1 if dev < -prm["swing_in_hz"] else 0)
            if sd and side and sd != side:
                flips += 1
            if sd:
                side = sd
        out["flips"] = flips
        if outs:
            back = next((t for t, v in freq_w if t > outs[-1] and abs(v - f0) <= prm["swing_in_hz"]), t_to)
            out.update(swing_sec=round((back - outs[0]).total_seconds(), 1),
                       swing_from=outs[0].isoformat(), swing_to=back.isoformat())

    # Задание частоты: значение на начало прокрутки и строки окна
    fr_a = cfg.role_to_addr("FREQ_REFERENCE")
    if fr_a is not None:
        # (момент, значение, в силе на начало прокрутки)
        pts: list[tuple[datetime, float, bool]] = []
        h0 = _value_at(all_by_addr, fr_a, crank, prm["fref_hold_max_sec"], _hd.get("FREQ_REFERENCE"))
        if h0 is not None:
            pts.append((crank, h0[1], True))
        pts += [(t, v, False) for t, v in _iter_values(all_by_addr, fr_a, crank, t_to) if t > crank]
        if pts:
            seq: list[float] = []
            for _, v, _h in pts:
                if v > 0 and (not seq or abs(seq[-1] - v) > 1e-6):
                    seq.append(_r3(v))
            valid = [x for x in pts if x[1] >= prm["fref_valid_min_hz"]]
            # Размах — полный с начала прокрутки и отдельно по строкам после номинала
            fref: dict[str, Any] = {"seq": seq[:8], "seq_n": len(seq),
                                    "range": _rng([v for _, v, _h in valid]),
                                    "range_rated": None, "high": None, "low": None}
            if valid:
                th, vh, held0 = max(valid, key=lambda x: x[1])
                if vh > prm["fref_high_hz"]:
                    # На прокрутке — значение на её начало или строка до «схватил»
                    stage = ("crank" if held0 or th < fire else
                             "ramp" if rated is None or th < rated else "rated")
                    fref["high"] = {"v": _r3(vh), "t": th.isoformat(), "stage": stage,
                                    "rpm": None if stage == "crank" else rpm.near(th, prm["swing_pair_sec"])}
            if rated is not None:
                after = [(t, v) for t, v, _h in valid if t >= rated and not _h]
                fref["range_rated"] = _rng([v for _, v in after])
                if after:
                    tl, vl = min(after, key=lambda x: x[1])
                    if vl < prm["fref_low_hz"]:
                        fref["low"] = {"v": _r3(vl), "t": tl.isoformat(),
                                       "rpm": rpm.near(tl, prm["swing_pair_sec"])}
            out["fref"] = fref

    # Заброс — те же метки, что у RPM_OVERSPEED (обороты и k·f_max опроса)
    out["overspeed"] = None
    try:
        from .rpm_freq import build_poll, over_marks, overspeed_params
        oprm = overspeed_params(cfg)
        if oprm is not None:
            rr = rpm.span(fire, t_to)
            if any(v > oprm["threshold_rpm"] for _, v in rr):
                # Следующий отсчёт — граница окна опроса последнего
                _nx = next(_iter_values(all_by_addr, rpm_a, t_to), None)
                nxt = [_nx] if _nx is not None else []
                poll = build_poll(all_by_addr, cfg, oprm, (held or {}).get("FREQUENCY"), gaps)
                bad = [(t, v, fm) for t, v, fm, b in over_marks(rr + nxt, poll, oprm)
                       if b and t < t_to]
                if bad:
                    t, v, fm = max(bad, key=lambda x: x[1])
                    out["overspeed"] = {"rpm": _r3(v), "hz": _r3(fm), "t": t.isoformat(),
                                        "threshold_rpm": _r3(oprm["threshold_rpm"])}  # 1575,0000375 → 1575
    except Exception:
        logger.warning("Заброс оборотов в профиле пуска не оценён", exc_info=True)

    # Автомат, нагрузка, масло
    out["breaker_closed_at"] = cb_closed.isoformat() if cb_closed is not None and cb_closed <= t_to else None
    kw = [v for _, v in _iter_values(all_by_addr, cfg.role_to_addr("ACTIVE_POWER_TOTAL"), fire, t_to)]
    out["kw_max"] = _r3(max(kw)) if kw else None
    # Масло на начало прокрутки: последняя строка до неё (строк до неё в окне
    # нет — удержанное перед окном, если оно не позже прокрутки, ревью R7),
    # иначе первая в окне, иначе удержанное перед окном (посев)
    oil_a = cfg.role_to_addr("OIL_TEMP")
    o = (_value_at(all_by_addr, oil_a, crank, None, _hd.get("OIL_TEMP"))
         or next(_iter_values(all_by_addr, oil_a, crank, t_to), None))
    if o is None and _hd.get("OIL_TEMP") is not None:
        o = _hd["OIL_TEMP"]
    out["oil_temp"] = round(o[1], 1) if o is not None else None
    if unobserved:
        # Пуск прошёл без связи (Q5): масло и автомат — уже работающей машины
        out.update(unobserved=True, oil_temp=None)

    # Неустойчивый пуск — вариант «б» (В1)
    reasons: list[str] = []
    if out["overspeed"]:
        reasons.append("overspeed")
    if out["swing_sec"] >= prm["unstable_swing_sec"]:
        reasons.append("swing")
    if out["flips"] >= prm["unstable_flips"]:
        reasons.append("flips")
    fz = out.get("fref") or {}
    if fz.get("low"):
        reasons.append("fref_low")
    if fz.get("high"):
        reasons.append("fref_high")
    out["unstable"] = bool(reasons)
    out["reasons"] = reasons
    out["limits"] = {k: prm[k] for k in ("unstable_swing_sec", "unstable_flips",
                                         "fref_low_hz", "fref_high_hz")}
    return out


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
    load_seed: dict | None = None,  # последний замер нагрузки перед окном + обрывы до окна;
                                    # held/held_gaps — удержанные значения (v5.1.21, П10)
    confirm_to: datetime | None = None,  # докуда решать исход попытки за краем окна (v5.1.16)
    live_edge: bool = False,  # ts_to — живой край открытого окна (v5.1.21, правило B)
    start_carry: dict | None = None,  # профиль пуска «Работы»-предка, закрытой
                                      # суточным резом (ревью v5.1.23, R1)
) -> list[Segment]:
    """Главная точка входа: преобразует сырые данные в список сегментов.

    Входные данные должны быть загружены runner-ом из БД через analytics.source.
    Обработка строго каузальная (от старых к новым).

    initial_coking_risk: если передан — инициализирует coking_risk аккумулятор
    начальным состоянием из предыдущего закрытого сегмента (ТЗ Этап 1.5, раздел 5.1).

    live_edge: окно открыто (живой цикл движка), ts_to — край данных: последний
    отсчёт оборотов без следующего правило B (dip_needs_freq: false) пока не
    учитывает. Закрытое окно (рез, перечитка) — False: будущих строк не будет
    (ревью v5.1.21, analytics-3).

    start_carry: профиль пуска «Работы», закрытой суточным резом перед окном
    (подсказка предка движка). «Работа», продолжающаяся с него на голове окна,
    получает его в start_profile_prev — для запрета снятия гейтом эпизода,
    начавшегося в окне неустойчивого пуска до реза (ревью v5.1.23, R1).
    """
    import copy as _copy
    tf = _tz(ts_from)
    tt = _tz(ts_to)

    # Индексируем историю по адресу; единицы — как в карте (наработка: с → ч)
    all_by_addr = _apply_scale(_build_by_addr(history), cfg)
    # Лента отсчётов акта (v5.1.22, P1-4) — записи панели как есть: фильтры
    # сбоев ниже возвращают новый словарь, этот остаётся сырым
    _trace_by_addr = all_by_addr
    # Одиночные нули источника — до всего остального (v5.1.17); ноль у
    # настоящего останова (40011 → 0) — не сбой
    _stops = [(_tz(p["state_start"]), _tz(p["state_end"]) if p.get("state_end") else tt)
              for p in enum_periods if p.get("addr") == 40011 and int(p.get("value") or 0) == 0]
    all_by_addr, _zero_glitches = _drop_zero_glitches(all_by_addr, cfg, tt, _stops)
    # Удержанные значения перед окном истории (v5.1.21, П10): частота — сверке
    # оборотов, остальное — переносу участков через начало окна
    _held = _held_from_seed(load_seed, cfg)
    _held_gaps = list(gaps or []) + list((load_seed or {}).get("held_gaps") or [])
    # Сбой датчика оборотов, не подтверждённый частотой генератора (v5.1.21, 1а).
    # Сбой фильтра не должен ронять анализ окна — тогда данные как есть
    try:
        all_by_addr, _rpm_glitches = _drop_rpm_glitches(
            all_by_addr, cfg, enum_periods, _held.get("FREQUENCY"), _held_gaps)
    except Exception:
        logger.warning("Сверка оборотов с частотой не выполнена", exc_info=True)
        _rpm_glitches = []
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
    _stop_kind_on = bool(cfg.seg("stop_kind", "enabled", default=False))
    # Интервалы аварии панели — один расчёт на вид стопа и отметку сегмента,
    # оборванного аварией (v5.1.22, P1-3)
    _shutdown_iv: list[tuple[datetime, datetime]] = []
    if _stop_kind_on:
        # Горизонт — тот же, что у исхода попытки (confirm_to): открытая
        # авария, поднявшаяся на падении за суточным резом, иначе обрезалась
        # бы по tt, и голова плашки вышла бы «Стоп», а перечитка — «Авария»
        # (ревью v5.1.16). Сегменты всё равно режутся по tt
        _horizon = max(tt, _tz(confirm_to)) if confirm_to else tt
        _shutdown_iv = shutdown_intervals(fault_periods, cfg, _horizon, enum_periods)
        run_state_periods = _classify_stop_periods(
            run_state_periods, fault_periods, cfg, _horizon, enum_periods,
            shutdown_iv=_shutdown_iv,
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
    # Звенья перед окном — и прогрев, которым машина вышла из стопа перед
    # «Работой» на голове окна (профиль пуска, v5.1.23)
    _ctx_links: list = []
    if transition_ctx:
        from .transitions import ctx_for_window
        _ctx = ctx_for_window(transition_ctx, tf,
                              float(cfg.seg("transitions", "joint_tol_sec", default=2.0)))
        _ctx_links = list(_ctx or [])
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
        enum_periods=enum_periods,
    )

    segments: list[Segment] = []
    accumulators = RiskAccumulators()
    if initial_coking_risk is not None:
        accumulators.coking_risk = _copy.deepcopy(initial_coking_risk)
    # Код 40012 (без NA) — для записи «код на закрытии» и отметки обрыва (v5.1.22)
    _code_periods = state_periods.get(cfg.role_to_addr("LAST_FAULT_CODE")) or []
    # Начала аварий, уже отданных сегменту: одна авария — одна отметка. v5.1.22,
    # ревью verdict-4: и отданных сегментам, закрытым прошлыми вызовами (звенья
    # контекста, стыкующиеся с окном): живой движок закрывает «Работу» и
    # «Охлаждение» разными циклами, а отметка должна быть одна
    _trip_used: set = set()
    if transition_ctx:
        try:
            from .transitions import ctx_for_window as _cfw
            for _lk in _cfw(transition_ctx, tf,
                            float(cfg.seg("transitions", "joint_tol_sec", default=2.0))):
                _iv0 = getattr(_lk, "trip_iv", None)
                if _iv0 is not None:
                    _trip_used.add(_tz(_iv0))
        except Exception:
            logger.warning("Отметки обрыва аварией прошлых сегментов не прочитаны", exc_info=True)
    # Докуда есть история (лента отсчётов акта: «останов + after, сколько есть»)
    _data_to = max(tt, _tz(confirm_to)) if confirm_to else tt

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

        # Мото-часы на начало сегмента. v5.1.22: строки счётчика в сегменте нет
        # (короче пульса 15 мин) — последняя до начала в окне, иначе удержанная
        # перед окном; и на конец — для строки шапки заключения
        engine_hours_start = _get_engine_hours(seg_by_addr, seg_start, cfg)
        engine_hours_end: float | None = None
        try:
            if engine_hours_start is None:
                engine_hours_start = _engine_hours_at(all_by_addr, cfg, seg_start, _held)
            engine_hours_end = _engine_hours_at(all_by_addr, cfg, seg_end, _held)
            if engine_hours_end is None:
                engine_hours_end = engine_hours_start
        except Exception:
            logger.warning("Наработка сегмента не определена", exc_info=True)

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
        _next_period = (run_state_periods[k_period + 1]
                        if k_period + 1 < len(run_state_periods) and _cut_inside else None)

        # Код 40012 на закрытии (v5.1.22, P1-2): снят ли на резе — «сброшен /
        # снят / сменился» вместо ложной «Несброшенной». Только рез внутри окна
        fault_code_close_rec: dict[str, Any] | None = None
        closed_by_trip: dict[str, Any] | None = None
        if _cut_inside:
            # Сегмент, оборванный аварийным остановом (v5.1.22, P1-3, решение В3):
            # любой режим, кроме стопа, — только при модели двух видов стопа.
            # До кода на закрытии: у оборванного сегмента сброс после кадра
            # снятия — уже нового кода (ревью verdict-2)
            if _stop_kind_on and run_state != 0 and not attempt:
                try:
                    closed_by_trip = _trip_mark(
                        run_state, seg_start, seg_end, _shutdown_iv, _next_period,
                        _trip_used, _code_periods, fault_periods, cfg)
                except Exception:
                    logger.warning("Отметка обрыва сегмента аварией не определена", exc_info=True)
            try:
                fault_code_close_rec = fault_code_close(
                    _code_periods, enum_periods, cfg, seg_start, seg_end,
                    next_state=_enum_value(_next_period) if _next_period is not None else None,
                    run_state=run_state, tripped=closed_by_trip is not None)
            except Exception:
                logger.warning("Код 40012 на закрытии сегмента не определён", exc_info=True)
            # Лента отсчётов сегмента падения для акта следующего стопа
            # (v5.1.22, P1-4, В2): история окна уже в памяти — без чтения
            if closed_by_trip is not None:
                try:
                    _tr = _fall_trace(_trace_by_addr, enum_periods, cfg, seg_start, seg_end,
                                      seg_end, _data_to, run_state, None, _held)
                    if _tr is not None:
                        closed_by_trip["trace"] = _tr
                except Exception:
                    logger.warning("Лента отсчётов сегмента падения не снята", exc_info=True)
        # Плашка неудачной попытки с аварией — сама сегмент падения (v5.1.22, P1-4)
        fall_trace: dict[str, Any] | None = None
        if (_stop_kind_on and attempt and attempt.get("outcome") == "EMERGENCY"
                and attempt.get("t_fall")):
            try:
                _a0 = _tz(datetime.fromisoformat(attempt.get("t_start"))) if attempt.get("t_start") \
                    else _tz(seg_start)
                _af = _tz(datetime.fromisoformat(attempt["t_fall"]))
                fall_trace = _fall_trace(_trace_by_addr, enum_periods, cfg, _a0, _af, _af, _data_to,
                                         attempt.get("from_state"), run_state_label, _held)
            except Exception:
                logger.warning("Лента отсчётов попытки не снята", exc_info=True)

        # L2-подсегменты
        start_prof: dict[str, Any] | None = None
        start_prev: dict[str, Any] | None = None
        if run_state == 3:
            _nxt = int(_next_period["value"]) if _next_period is not None else None
            _steady = _steady_window(all_by_addr, cfg, seg_start, seg_end, _nxt,
                                     attempt_pending)
            # Профиль пуска (v5.1.23, п.3): «Работа» с подтверждённым выходом из
            # стопа. Из истории окна, без чтения; сбой — без профиля, нарезка идёт
            if not attempt_pending:
                try:
                    _org = _start_origin(run_state_periods, k_period, _ctx_links, tf,
                                         float(cfg.seg("transitions", "joint_tol_sec", default=2.0)))
                    _st_to = (_steady[1] if isinstance(_steady, tuple) and _steady[1] is not None
                              and _steady[1] < _tz(seg_end) else None)
                    if _org is not None:
                        start_prof = start_profile(
                            all_by_addr, enum_periods, cfg, _org[0], _org[1], seg_end,
                            open_end=live_edge and p_end_raw is None, steady_to=_st_to,
                            held=_held, gaps=_held_gaps)
                    elif start_carry is not None and p_start < tf:
                        # «Работа» идёт через суточный рез: профиль предка — для
                        # запрета снятия (ревью R1); окно пуска не дописывается
                        start_prev = start_profile_carry(
                            start_carry, all_by_addr, enum_periods, cfg, p_start, tf, seg_end,
                            open_end=live_edge and p_end_raw is None, steady_to=_st_to)
                except Exception:
                    logger.warning("Профиль пуска сегмента %s не посчитан", seg_start, exc_info=True)
            # Удержанные значения на начало сегмента (v5.1.21): последняя строка
            # до него в окне истории, иначе посев перед окном (П10). Сбой — без них
            try:
                _seg_held = held_before(all_by_addr, cfg, seg_start, _held, _held_gaps,
                                        with_passport=cause_open == "REPORT_START")
            except Exception:
                logger.warning("Удержанные значения на начало сегмента не определены", exc_info=True)
                _seg_held = {}
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
                cause_open=cause_open,
                held=_seg_held,
                # Живой край — только у сегмента, идущего до края открытого окна
                live_edge=live_edge and not _cut_inside,
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
        _rc = _rpm_glitch_check(_rpm_glitches, _tz(seg_start), _tz(seg_end), cfg)
        if _rc:
            seq_checks.append(_rc)

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
            fault_code_close=fault_code_close_rec,
            closed_by_trip=closed_by_trip,
            engine_hours_end=engine_hours_end,
            fall_trace=fall_trace,
            start_profile=start_prof,
            start_profile_prev=start_prev,
        )
        segments.append(seg)

    # Второй проход: межсегментные проверки (warmup / cooldown / cold_start / stop_profile)
    _inter_segment_checks(segments, all_by_addr, cfg, state_periods,
                          transition_ctx=transition_ctx, ts_from=tf, load_seed=load_seed,
                          gaps=gaps)

    return segments
