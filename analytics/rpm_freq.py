# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Сверка оборотов с частотой генератора (v5.1.21, решения владельца 15.10.2026).

ДГУ №1 15.10 04:42–09:24 МСК: 12 одиночных отсчётов оборотов 1417–1479 при
частоте 49,9–50,1 Гц, через 9–11 с каждый раз 1495–1501. Это сбой датчика
оборотов, а не провал: двигатель на 1417 об/мин дал бы генератору 47,2 Гц.
Каждый такой отсчёт заводил «Просадку оборотов» на весь подсегмент (24 ч).

Частота опроса отсчёта оборотов в момент t — набор значений регистра частоты:
- удержанное на t (последняя реальная строка не позже t). Держится, пока идут
  пакеты; сбрасывается на дыре data_gaps и на паузе пакетов длиннее 90 с,
  потолок freq_hold_max_sec;
- плюс все строки частоты после t до следующего отсчёта оборотов, не дальше
  t + poll_window_max_sec.
Не «та же метка»: строка частоты часто приходит позже строки оборотов — у
ДЭС №3 на 1 с в 8–15 % опросов, у ДГУ №1 17–18.09 на 4–8 с. С «той же
меткой» фильтр вырезал бы у ДЭС №3 четыре настоящих провала (16–17.09).

Здесь две вещи, обе без зависимостей от сегментатора и движка:
- is_glitch — условие фильтра одиночного сбоя (1а, segmenter._drop_rpm_glitches);
- is_dip / dip_marks — критерий провала v* (1б, В1): провал, только если ниже
  порога и обороты, и частота опроса: max(обороты, k·f_min) < порога,
  k = rated_rpm / freq_nominal_hz. Без частоты (нет, вне 40–55 Гц, 0) — по
  оборотам, как раньше: обороты 0 при 0 Гц и падение в стоп — провал (v5.1.16).
  Ключ dip_needs_freq: false — правило B: провал по оборотам после фильтра 1а.
- overspeed_params / is_over / over_marks — заброс оборотов (v5.1.23, п.2, В2′):
  отсчёт в заборе, только если выше порога (1575 об/мин при уставке разноса
  1725) и обороты, и k·f_max опроса; без частоты (нет, вне 40–65 Гц) заброса
  нет — по одним оборотам не ловим.
"""

from __future__ import annotations

import bisect
from datetime import datetime, timedelta, timezone
from typing import Any

# Строки одного пакета расходятся с пингом на секунду (ДЭС №3: обороты в :43,
# частота в :44) — пауза пакетов считается от первой строки после неё
_PACKET_TOL_SEC = 2.0
_FAR = datetime.max.replace(tzinfo=timezone.utc)

# Порог заброса — доля уставки разноса ниже неё, % (RPM_OVERSPEED.proximity_pct,
# решение В2′ 17.10.2026): 150/1725 → 1575 об/мин, 91,3 % уставки. Умолчание
# кода равно KB, чтобы опечатка ключа не вернула прежние 1552,5 (10 %)
OVERSPEED_PROXIMITY_PCT = 8.69565


def _tz(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _as_dt(v: Any) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, str):
        try:
            return _tz(datetime.fromisoformat(v))
        except ValueError:
            return None
    return _tz(v) if isinstance(v, datetime) else None


def _fv(row: dict) -> float | None:
    v = row.get("value")
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def rpm_freq_params(cfg) -> dict[str, Any]:
    """Параметры сверки: segmentation.yaml → rpm_freq_check, порог и номинал —
    из детекторов (RPM_UNDERSPEED.threshold_rpm / rated_rpm, LOAD_STEP.freq_nominal_hz)."""
    def g(key: str, default: Any) -> Any:
        try:
            v = cfg.seg("rpm_freq_check", key, default=default)
        except Exception:
            v = default
        return default if v is None else v

    def d(scen: str, key: str, default: float) -> float:
        try:
            v = cfg.det(scen, key, default=default)
            return float(default if v is None else v)
        except Exception:
            return float(default)

    try:
        lo, hi = (float(x) for x in g("freq_valid_hz", [40.0, 55.0]))
    except (TypeError, ValueError):
        lo, hi = 40.0, 55.0
    try:
        hb = float(cfg.seg("data_quality", "heartbeat_nominal_sec", default=30) or 30)
        mult = float(cfg.seg("data_quality", "heartbeat_max_multiplier", default=3) or 3)
        hb_addr = cfg.seg("data_quality", "heartbeat_addr", default=None)
    except Exception:
        hb, mult, hb_addr = 30.0, 3.0, None
    rated = d("RPM_UNDERSPEED", "rated_rpm", 1500.0)
    f_nom = d("LOAD_STEP", "freq_nominal_hz", 50.0)
    return {
        "enabled": bool(g("enabled", True)),
        "dip_needs_freq": bool(g("dip_needs_freq", True)),
        "threshold_rpm": d("RPM_UNDERSPEED", "threshold_rpm", 1485.0),
        "k": rated / f_nom if f_nom > 0 else 30.0,
        "tolerance_rpm": float(g("tolerance_rpm", 10.0)),
        "freq_hold_max_sec": float(g("freq_hold_max_sec", 1800.0)),
        "freq_lo": lo,
        "freq_hi": hi,
        "poll_window_max_sec": float(g("poll_window_max_sec", 60.0)),
        "next_max_gap_sec": float(g("next_max_gap_sec", 60.0)),
        "min_rpm": float(g("min_rpm", 1000.0)),
        "remark_min_count": int(g("remark_min_count", 3)),
        "packet_max_gap_sec": hb * mult,
        "heartbeat_addr": int(hb_addr) if hb_addr is not None else None,
    }


class FreqPoll:
    """Частота опроса отсчёта оборотов: удержанное значение + строки до следующего отсчёта."""

    def __init__(
        self,
        rows: list[tuple[datetime, float]],
        gaps: list[dict] | None,
        packets: list[datetime] | None,
        prm: dict[str, Any],
        seed: tuple[datetime, float] | None = None,
    ):
        pts = sorted((_tz(t), float(v)) for t, v in rows)
        if seed is not None:
            st = _as_dt(seed[0])
            if st is not None and seed[1] is not None and (not pts or st < pts[0][0]):
                pts.insert(0, (st, float(seed[1])))
        self._ts = [t for t, _ in pts]
        self._v = [v for _, v in pts]
        self._gaps: list[tuple[datetime, datetime]] = []
        for gp in gaps or []:
            gs = _as_dt(gp.get("gap_start"))
            if gs is None:
                continue
            self._gaps.append((gs, _as_dt(gp.get("gap_end")) or _FAR))
        pk = sorted(_tz(t) for t in packets or [])
        mx = prm["packet_max_gap_sec"]
        self._pause_ends = [b for a, b in zip(pk, pk[1:]) if (b - a).total_seconds() > mx]
        self._prm = prm

    def held(self, t: datetime) -> tuple[datetime, float] | None:
        """Удержанное значение на t: последняя строка не позже t, если связь не рвалась."""
        i = bisect.bisect_right(self._ts, t) - 1
        if i < 0:
            return None
        ft = self._ts[i]
        if (t - ft).total_seconds() > self._prm["freq_hold_max_sec"]:
            return None
        if any(gs < t and ge > ft for gs, ge in self._gaps):
            return None
        tol = timedelta(seconds=_PACKET_TOL_SEC)
        j = bisect.bisect_right(self._pause_ends, ft + tol)
        if j < len(self._pause_ends) and self._pause_ends[j] <= t + tol:
            return None
        return ft, self._v[i]

    def window(self, t: datetime, t_next: datetime | None = None) -> list[float]:
        """Значения частоты опроса отсчёта в t (t_next — следующий отсчёт оборотов)."""
        out: list[float] = []
        h = self.held(t)
        if h is not None:
            out.append(h[1])
        end = t + timedelta(seconds=self._prm["poll_window_max_sec"])
        if t_next is not None and t_next < end:
            end = t_next
        j = bisect.bisect_right(self._ts, t)
        while j < len(self._ts) and self._ts[j] < end:
            out.append(self._v[j])
            j += 1
        return out


def _real(rows: list[dict] | None) -> list[tuple[datetime, float]]:
    out = []
    for r in rows or []:
        if r.get("is_carried_forward"):
            continue
        v = _fv(r)
        if v is not None:
            out.append((_tz(r["ts"]), v))
    out.sort(key=lambda x: x[0])
    return out


def packet_times(by_addr: dict[int, list[dict]], prm: dict[str, Any]) -> list[datetime]:
    """Моменты пакетов связи: пинг (heartbeat_addr), без него — моменты любых
    реальных строк. Общие для частоты опроса и участков (analytics/live.py)."""
    hb = prm.get("heartbeat_addr")
    packets = sorted({t for t, _ in _real(by_addr.get(hb))}) if hb is not None else []
    if not packets:
        packets = sorted({_tz(r["ts"]) for rows in by_addr.values() for r in rows
                          if not r.get("is_carried_forward")})
    return packets


def build_poll(
    by_addr: dict[int, list[dict]], cfg, prm: dict[str, Any],
    held_freq: tuple[datetime, float] | None = None,
    gaps: list[dict] | None = None,
    packets: list[datetime] | None = None,
) -> FreqPoll | None:
    """Частота опроса по строкам окна. Пакеты — пинг (heartbeat_addr), без него —
    моменты любых строк. None — регистра частоты в карте нет."""
    f_addr = cfg.role_to_addr("FREQUENCY")
    if f_addr is None:
        return None
    if packets is None:
        packets = packet_times(by_addr, prm)
    return FreqPoll(_real(by_addr.get(f_addr)), gaps, packets, prm, held_freq)


def is_glitch(
    rpm: float, fvals: list[float], nxt: tuple[datetime, float] | None,
    t: datetime, prm: dict[str, Any],
) -> bool:
    """1а: одиночный сбой датчика — обороты ниже порога, а вся частота опроса
    рабочая (k·f ≥ порог + допуск) и следующий отсчёт в норме не позже минуты.

    Нули не трогаем (min_rpm, решение 13.10 по фильтру нулей): выбег, холостой
    ход и падения в стоп ниже min_rpm. Отсчёт без следующего (край данных) не
    вырезается — решает v*."""
    thr = prm["threshold_rpm"]
    if not (prm["min_rpm"] < rpm < thr) or not fvals or nxt is None:
        return False
    if nxt[1] < thr or (nxt[0] - t).total_seconds() > prm["next_max_gap_sec"]:
        return False
    need = thr + prm["tolerance_rpm"]
    return all(prm["freq_lo"] <= f <= prm["freq_hi"] and prm["k"] * f >= need for f in fvals)


def freq_ok(fmin: float | None, prm: dict[str, Any]) -> bool:
    """Частоте можно верить: есть, не 0 и в пределах 40–55 Гц."""
    return fmin is not None and fmin > 0 and prm["freq_lo"] <= fmin <= prm["freq_hi"]


def is_dip(rpm: float, fvals: list[float], prm: dict[str, Any]) -> bool:
    """1б: отсчёт в провале. v* — max(обороты, k·f_min) < порога; без частоты и
    при dip_needs_freq: false (правило B) — обороты < порога."""
    thr = prm["threshold_rpm"]
    if rpm >= thr:
        return False
    if prm["dip_needs_freq"]:
        fmin = min(fvals) if fvals else None
        if freq_ok(fmin, prm):
            return max(rpm, prm["k"] * fmin) < thr
    return True


def dip_marks(
    rpm_rows: list[tuple[datetime, float]],
    poll: FreqPoll | None,
    prm: dict[str, Any],
    tt: datetime | None = None,
) -> list[tuple[datetime, float, float | None, bool | None]]:
    """Провал по каждому отсчёту оборотов: [(момент, обороты, f_min опроса, в провале)].

    f_min — только у отсчётов ниже порога (выше порога провала нет при любой
    частоте, окно опроса не считается). «В провале» None — отсчёт пока не учитывать: правило B на живом краю
    (последний отсчёт ниже порога без следующего, с него не прошло
    next_max_gap_sec) — иначе сбой заводил бы эпизод, а следующий цикл,
    вырезав его фильтром 1а, отзывал. tt — живой край открытого окна; у
    закрытого (рез, смена режима) None: будущих строк не будет, отсчёт без
    следующего решается по себе (ревью v5.1.21, analytics-3). Отсчёты —
    реальные, после фильтра 1а.
    """
    out: list[tuple[datetime, float, float | None, bool | None]] = []
    rows = sorted(((_tz(t), float(v)) for t, v in rpm_rows), key=lambda x: x[0])
    for k, (t, v) in enumerate(rows):
        nxt_t = rows[k + 1][0] if k + 1 < len(rows) else None
        fvals = poll.window(t, nxt_t) if poll is not None and v < prm["threshold_rpm"] else []
        fmin = min(fvals) if fvals else None
        bad: bool | None = is_dip(v, fvals, prm)
        if (bad and not prm["dip_needs_freq"] and nxt_t is None and tt is not None
                and (_tz(tt) - t).total_seconds() < prm["next_max_gap_sec"]):
            bad = None
        out.append((t, v, fmin, bad))
    return out


def overspeed_params(cfg, base: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Параметры заброса оборотов (RPM_OVERSPEED, v5.1.23, решение В2′ 17.10.2026).

    Порог — уставка разноса панели (thresholds двигателя rpm.controller.
    overspeed_rpm, код 234) минус proximity_pct: у KTA50 и QSX15 1725 − 150 =
    1575 об/мин (proximity_pct 8,69565 = 150/1725, 91,3 % уставки; В2′ — пересмотр
    В2 «на 10 % строже» = 1552,5 по данным 60 дней). Сравнение строгое: 1575 —
    ещё не заброс (порог 1575,0000375 при 8,69565). Частоте опроса верим в
    freq_valid_hz — шире, чем у провала: 1696 об/мин 16.10 шли при 56,8 Гц.
    Опрос и k — от сверки v* (base, по умолчанию rpm_freq_params): FreqPoll
    берёт из словаря удержание и окно опроса. None — уставки разноса в KB нет:
    порог не выдумываем, ключа и детекции нет."""
    try:
        ovs = cfg.thr("rpm", "controller", "overspeed_rpm", default=None)
        if ovs is None:
            ovs = cfg.thr("frequency", "controller", "overspeed_rpm", default=None)
        ovs = float(ovs) if ovs is not None else None
    except (TypeError, ValueError):
        ovs = None
    if ovs is None or ovs <= 0:
        return None

    def d(key: str, default: Any) -> Any:
        try:
            v = cfg.det("RPM_OVERSPEED", key, default=default)
        except Exception:
            v = default
        return default if v is None else v

    # Умолчание = KB (OVERSPEED_PROXIMITY_PCT, В2′): без ключа — тоже 1575
    try:
        pct = float(d("proximity_pct", OVERSPEED_PROXIMITY_PCT))
    except (TypeError, ValueError):
        pct = OVERSPEED_PROXIMITY_PCT
    try:
        lo, hi = (float(x) for x in d("freq_valid_hz", [40.0, 65.0]))
    except (TypeError, ValueError):
        lo, hi = 40.0, 65.0
    try:
        code = int(d("fault_code", 234))
    except (TypeError, ValueError):
        code = 234
    return dict(base if base is not None else rpm_freq_params(cfg),
                threshold_rpm=ovs * (1 - pct / 100), overspeed_rpm=ovs, proximity_pct=pct,
                freq_lo=lo, freq_hi=hi, fault_code=code)


def over_fmax(fvals: list[float], prm: dict[str, Any]) -> float | None:
    """Наибольшая частота опроса, которой можно верить (в freq_valid_hz заброса).
    0 Гц стоянки и 66 Гц сбоя отбрасываются, остальное решает максимум."""
    ok = [f for f in fvals if prm["freq_lo"] <= f <= prm["freq_hi"]]
    return max(ok) if ok else None


def is_over(rpm: float, fvals: list[float], prm: dict[str, Any]) -> bool:
    """Отсчёт в заборе: обороты > порога И k·f_max опроса > порога (строго)."""
    thr = prm["threshold_rpm"]
    if rpm <= thr:
        return False
    fmax = over_fmax(fvals, prm)
    return fmax is not None and prm["k"] * fmax > thr


def over_marks(
    rpm_rows: list[tuple[datetime, float]],
    poll: FreqPoll | None,
    prm: dict[str, Any],
) -> list[tuple[datetime, float, float | None, bool]]:
    """Заброс по каждому отсчёту оборотов: [(момент, обороты, f_max опроса, в заборе)].

    prm — overspeed_params. f_max — только у отсчётов выше порога (ниже
    заброса нет при любой частоте, окно опроса не считается). Без опроса
    (регистра частоты нет) заброса нет. Частота опроса — та же, что у провала:
    удержанное значение и строки до следующего отсчёта оборотов (строка
    частоты приходит на 1–8 с позже — ДГУ №2 15.10 12:42:19: 1606 об/мин,
    удержано 0 Гц, через секунду 53,6). Добавочные строки только поднимают
    максимум: отсчёт, подтверждённый на одном цикле, на следующем не отзывается."""
    out: list[tuple[datetime, float, float | None, bool]] = []
    rows = sorted(((_tz(t), float(v)) for t, v in rpm_rows), key=lambda x: x[0])
    thr = prm["threshold_rpm"]
    for k, (t, v) in enumerate(rows):
        if v <= thr or poll is None:
            out.append((t, v, None, False))
            continue
        nxt_t = rows[k + 1][0] if k + 1 < len(rows) else None
        fvals = poll.window(t, nxt_t)
        out.append((t, v, over_fmax(fvals, prm), is_over(v, fvals, prm)))
    return out


def glitch_details(samples: list[dict], tz=None) -> str:
    """Текст проверки source_rpm_glitch: время местное (МСК), не UTC, как у нулей."""
    from .labels import fmt_number
    if tz is None:
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo("Europe/Moscow")
        except Exception:
            tz = timezone(timedelta(hours=3))
    shown = []
    for s in samples[:5]:
        t = _as_dt(s.get("t"))
        if t is None:
            continue
        hz = s.get("hz")
        shown.append(f"{t.astimezone(tz).strftime('%H:%M:%S')} ({fmt_number(s.get('rpm'), 0)}"
                     + (f" при {fmt_number(hz, 1)} Гц)" if hz is not None else ")"))
    more = f" и ещё {len(samples) - 5}" if len(samples) > 5 else ""
    return ("отброшены отсчёты оборотов, не подтверждённые частотой генератора "
            f"(сбой датчика), всего {len(samples)}: {', '.join(shown)}{more}")
