# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Forward-fill аналоговых регистров по пакетам опроса (ТЗ Addendum v1.2, пункт 1).

Политика report-by-exception: роутер отправляет регистр только при изменении
значения сверх допуска. Отсутствие в пакете ≠ ноль — значение не изменилось.

Алгоритм:
  - За пакетные якоря берутся таймштампы регистра-пинга (heartbeat_addr, 40290),
    который присутствует в КАЖДОМ пакете опроса.
  - Для каждого аналогового регистра: если в пакете нет реального чтения —
    подставляем последнее известное значение (is_carried_forward=True).
  - Синтетические строки НЕ хранятся в БД; создаются в памяти только на время
    расчёта агрегатов (median/min/max).
  - Скорости (V-метрики, slope) считаются ТОЛЬКО по реальным измерениям
    (is_carried_forward=False) — это предотвращает артефакты «ступеньки».
  - Память forward-fill сбрасывается при gap или устаревании (> heartbeat × mult).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .config import AnalyticsConfig


def _tz(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _fv(r: dict) -> float | None:
    v = r.get("value")
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _is_in_gap(ts: datetime, gaps: list[dict]) -> bool:
    for g in gaps:
        gs = _tz(g["gap_start"])
        ge_raw = g.get("gap_end")
        ge = _tz(ge_raw) if ge_raw else ts + timedelta(seconds=1)
        if gs <= ts < ge:
            return True
    return False


def apply_forward_fill(
    by_addr: dict[int, list[dict]],
    cfg: AnalyticsConfig,
    t_start: datetime,
    t_end: datetime,
    gaps: list[dict],
) -> dict[int, list[dict]]:
    """Применить forward-fill к аналоговым регистрам по таймштампам пинг-регистра.

    Возвращает НОВЫЙ словарь by_addr. Каждая строка обогащена полем
    ``is_carried_forward`` (bool). Строки с is_carried_forward=True
    используются только для агрегатных метрик и должны игнорироваться
    при расчёте скоростей (V-метрик).

    Если heartbeat_addr не задан или его данных нет в by_addr — возвращает
    исходные данные с is_carried_forward=False на каждой строке (деградирует
    к прежнему поведению без ложных нулей от forward-fill).
    """
    t0 = _tz(t_start)
    t1 = _tz(t_end)

    heartbeat_sec = float(cfg.seg("data_quality", "heartbeat_nominal_sec", default=30))
    max_mult = float(cfg.seg("data_quality", "heartbeat_max_multiplier", default=3))
    max_age_sec = heartbeat_sec * max_mult
    tol_sec = heartbeat_sec / 2  # ±15 сек вокруг таймштампа пакета

    hb_addr_raw = cfg.seg("data_quality", "heartbeat_addr", default=None)
    hb_addr: int | None = None
    if hb_addr_raw is not None:
        try:
            hb_addr = int(hb_addr_raw)
        except (TypeError, ValueError):
            hb_addr = None

    # Если нет пинга — только расставляем флаги is_carried_forward=False
    if hb_addr is None or hb_addr not in by_addr:
        return _mark_all_real(by_addr)

    # Таймштампы пакетов из пинг-регистра в [t0, t1)
    packet_ts_list: list[datetime] = sorted(
        _tz(r["ts"])
        for r in by_addr[hb_addr]
        if t0 <= _tz(r["ts"]) < t1
    )
    if not packet_ts_list:
        return _mark_all_real(by_addr)

    # Аналоговые роли (кроме quality_only, которые только для data_quality)
    analog_addrs: set[int] = {
        addr for addr, meta in cfg.register_map.items()
        if meta.get("kind") == "analog" and not meta.get("quality_only")
    }

    result: dict[int, list[dict]] = {}

    for addr in analog_addrs:
        all_rows = sorted(
            (r for r in by_addr.get(addr, []) if r.get("value") is not None),
            key=lambda r: _tz(r["ts"]),
        )

        # Инициализация: последнее известное значение из ПРЕДШЕСТВУЮЩИХ данных
        # (включая преамбулу, т.е. строки с ts < t0)
        lk_val: float | None = None
        lk_ts: datetime | None = None
        for r in all_rows:
            if _tz(r["ts"]) < t0:
                v = _fv(r)
                if v is not None:
                    lk_val = v
                    lk_ts = _tz(r["ts"])
            else:
                break

        # Реальные строки в периоде, отсортированные по ts
        period_rows: list[dict] = [
            r for r in all_rows if t0 <= _tz(r["ts"]) < t1
        ]

        # Для быстрого поиска: индекс текущей позиции в period_rows
        real_idx = 0
        output_rows: list[dict] = []

        for pkt_ts in packet_ts_list:
            # 1. «Промотать» все реальные строки, поступившие ≤ pkt_ts
            while real_idx < len(period_rows):
                r_ts = _tz(period_rows[real_idx]["ts"])
                if r_ts <= pkt_ts + timedelta(seconds=tol_sec):
                    r2 = dict(period_rows[real_idx])
                    r2["is_carried_forward"] = False
                    output_rows.append(r2)
                    v = _fv(period_rows[real_idx])
                    if v is not None:
                        lk_val = v
                        lk_ts = r_ts
                    real_idx += 1
                else:
                    break

            # 2. Была ли реальная строка В ЭТОМ пакете (вблизи pkt_ts)?
            real_at_packet = any(
                abs((_tz(r["ts"]) - pkt_ts).total_seconds()) <= tol_sec
                for r in output_rows[-real_idx:]  # смотрим только свежедобавленные
            )
            # Упрощённая проверка: последняя добавленная строка — в пакете?
            real_at_packet = bool(
                output_rows
                and not output_rows[-1]["is_carried_forward"]
                and abs((_tz(output_rows[-1]["ts"]) - pkt_ts).total_seconds()) <= tol_sec
            )

            # 3. Если реального нет — подставить последнее известное
            if not real_at_packet and lk_val is not None and lk_ts is not None:
                age = (pkt_ts - lk_ts).total_seconds()
                if 0 < age <= max_age_sec and not _is_in_gap(pkt_ts, gaps):
                    output_rows.append({
                        "ts": pkt_ts,
                        "addr": addr,
                        "value": lk_val,
                        "raw": None,
                        "name_ru": None,
                        "unit": None,
                        "is_carried_forward": True,
                    })

        # Добавить оставшиеся реальные строки после последнего пакета
        while real_idx < len(period_rows):
            r2 = dict(period_rows[real_idx])
            r2["is_carried_forward"] = False
            output_rows.append(r2)
            real_idx += 1

        output_rows.sort(key=lambda r: _tz(r["ts"]))
        result[addr] = output_rows

    # Адреса не из analog_addrs (enum, fault, heartbeat) — без изменений,
    # просто расставляем флаг is_carried_forward=False
    for addr, rows in by_addr.items():
        if addr not in result:
            result[addr] = [{**r, "is_carried_forward": False} for r in rows]

    return result


def _mark_all_real(by_addr: dict[int, list[dict]]) -> dict[int, list[dict]]:
    """Расставить is_carried_forward=False на все строки без изменения данных."""
    return {
        addr: [{**r, "is_carried_forward": False} for r in rows]
        for addr, rows in by_addr.items()
    }
