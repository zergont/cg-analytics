# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""Проверки v5.1.34 «Решения владельца 01.11» (без БД и сети; проект
docs/plans/design_v5132_tails.md, раздел «Вопросы владельцу», В1–В3).

- В1 (вариант В = Б + В). Пуски внутри обрыва связи: строка фактов по счётчикам
  панели (попытки 40069, успешные 40074, наработка 40070) в первом сегменте после
  обрыва — ДЭС №3 «Обрыв связи 25.10 00:11:03 – 26.10 18:55:59 (42 ч 45 мин): по
  счётчикам 2 пуска (…), наработка 40 ч 06 мин; когда был последний пуск — не
  видно»; счётчики не менялись — строка без «последнего пуска» (с 30 мин),
  короткий обрыв без смены — без строки; у машины нет счётчиков пусков — только
  наработка, без «не меньше»; нет счётчиков вовсе — как раньше. Сменился счётчик
  пусков — время в режиме от первых данных после обрыва «не меньше»: статус
  «Работа ≥ … (последний пуск — в обрыве 25–26.10)», отчёт «с начала состояния (с
  26.10 18:56, пуск в обрыве)», «Срабатываний с 26.10 18:56 (пуск в обрыве
  связи)», признаки головы run_origin_unknown / origin_from, цепочка с предком до
  обрыва рвётся; обрыв, начавшийся внутри сегмента «Работа» и кончившийся в
  следующем (сутки без заглушки), — то же; смена режима после обрыва — факты в
  закрытом сменой режима сегменте; рестарт — признак из forward_fill_json; запрос
  показаний до обрыва — только на закрытии, один, в цикле открытого окна — нет.
- В2 (вариант В). «Общая авария» с кодом (панельный CAUTION): уровень статуса
  «предупреждение», строка «🟡 панель: Общая авария — код 1453 «…» с 24.10
  16:00», alarm_text статуса — из неё, panel_severity «норма» — гейт на одну
  Общую аварию не запускается, хеши составов и подписи разборов гейта прежние;
  без Общей аварии и при настоящем WARNING / SHUTDOWN панели — как раньше;
  календарь сегментов без частей сводки не перекрашивается.
- В3 (вариант Б). Окно без своих строк (обрыв после реза): тревоги не
  закрываются, CLOSED не пишется; возврат связи — без повторного OPENED; окно
  со строками — как раньше; рестарт внутри обрыва — тревога из журнала живая.

Запуск:  py -3 scripts/test_v5134.py
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analytics import labels as L  # noqa: E402
from analytics import serializer as SER  # noqa: E402
from analytics.config import AnalyticsConfig  # noqa: E402
from analytics.fault_ref import FaultRef  # noqa: E402
from online import engine as eng  # noqa: E402
from online import status_assembler as SA  # noqa: E402

_errors: list[str] = []
MSK = ZoneInfo("Europe/Moscow")
U = timezone.utc
NBSP = " "


def check(cond: bool, msg: str) -> None:
    if not cond:
        _errors.append(msg)


class _Catch(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.recs: list[logging.LogRecord] = []

    def emit(self, record):
        self.recs.append(record)


LOG = _Catch()
for _n in ("analytics", "online", "corpus"):
    logging.getLogger(_n).addHandler(LOG)
    logging.getLogger(_n).setLevel(logging.DEBUG)
    logging.getLogger(_n).propagate = False


def msgs(level: int, text: str, since: int = 0) -> list[str]:
    return [r.getMessage() for r in LOG.recs[since:] if r.levelno == level and text in r.getMessage()]


KB = ROOT / "knowledge_base"
CFG3 = AnalyticsConfig.from_pair(KB, "pcc3300", "cummins_qsx15")      # ДЭС №3
FR3 = FaultRef(search_paths=[KB / "engines" / "cummins_qsx15", KB / "controllers" / "pcc3300"])
ATT, SUC, HRS, HB = 40069, 40074, 40070, 40290


def M(d, h, m, s=0, mo=10):
    """Время МСК → UTC."""
    return datetime(2026, mo, d, h, m, s, tzinfo=MSK).astimezone(U)


def S(t, sec):
    return t + timedelta(seconds=sec)


def iso(t):
    return t.isoformat()


# Обрыв ДЭС №3 по data_gaps (620): 25.10 00:11:03 → 26.10 18:55:59 МСК
G0, G1 = M(25, 0, 11, 3), M(26, 18, 55, 59)
BEFORE = {ATT: (M(25, 0, 0, 32), 961.0), SUC: (M(25, 0, 1, 44), 951.0), HRS: (M(25, 0, 8, 11), 56038140.0)}
AFTER = {ATT: (G1, 963.0), SUC: (G1, 953.0), HRS: (G1, 56182500.0)}
_ROLE = {ATT: "START_ATTEMPTS_TOTAL", SUC: "START_SUCCESS_TOTAL", HRS: "ENGINE_HOURS"}


def by_role(d):
    return {_ROLE[a]: v for a, v in d.items()}


TXT_3 = ("Обрыв связи 25.10 00:11:03 – 26.10 18:55:59 (42 ч 45 мин): по счётчикам 2 пуска (попыток "
         "961 → 963, успешных 951 → 953), наработка 40 ч 06 мин; когда был последний пуск — не видно")

# ══ В1-1. Факты обрыва и строка (чистые функции) ═════════════════════════════
_f = SER.outage_counter_facts(G0, G1, by_role(BEFORE), by_role(AFTER))
check(_f and _f["starts_changed"] is True and _f["n_success"] == 2 and _f["n_attempts"] == 2
      and _f["worked_sec"] == 144360.0, f"В1-1а: факты ДЭС №3 25–26.10: {_f}")
check(SER.outage_facts_text(_f, MSK) == TXT_3, f"В1-1а: строка как в решении: {SER.outage_facts_text(_f, MSK)}")

_g0, _g1 = M(29, 6, 2, 8), M(29, 12, 11, 51)
_f2 = SER.outage_counter_facts(_g0, _g1, {"START_ATTEMPTS_TOTAL": (S(_g0, -300), 963.0),
                                          "START_SUCCESS_TOTAL": (S(_g0, -300), 953.0),
                                          "ENGINE_HOURS": (S(_g0, -100), 56500000.0)},
                               {"START_ATTEMPTS_TOTAL": (_g1, 963.0), "START_SUCCESS_TOTAL": (_g1, 953.0),
                                "ENGINE_HOURS": (_g1, 56500000.0 + 22140)})
check(_f2["starts_changed"] is False and SER.outage_facts_text(_f2, MSK)
      == "Обрыв связи 29.10 06:02:08–12:11:51 (6 ч 10 мин): по счётчикам пусков не было (попыток 963, "
         "успешных 953), наработка 6 ч 09 мин",
      f"В1-1б: счётчики не менялись — без «последнего пуска»: {SER.outage_facts_text(_f2, MSK)}")
_f3 = SER.outage_counter_facts(_g0, _g1, {"START_ATTEMPTS_TOTAL": (_g0, 961.0), "START_SUCCESS_TOTAL": (_g0, 951.0)},
                               {"START_ATTEMPTS_TOTAL": (_g1, 962.0), "START_SUCCESS_TOTAL": (_g1, 951.0)})
check(_f3["starts_changed"] is True and SER.outage_facts_text(_f3, MSK)
      == "Обрыв связи 29.10 06:02:08–12:11:51 (6 ч 10 мин): по счётчикам 1 попытка пуска без успешного "
         "(попыток 961 → 962, успешных 951); когда была последняя попытка — не видно",
      f"В1-1в: только попытка — без успешного пуска: {SER.outage_facts_text(_f3, MSK)}")
_f4 = SER.outage_counter_facts(_g0, _g1, {"ENGINE_HOURS": (_g0, 1000.0)}, {"ENGINE_HOURS": (_g1, 1000.0 + 22140)})
check(_f4["starts_changed"] is None and SER.outage_facts_text(_f4, MSK)
      == "Обрыв связи 29.10 06:02:08–12:11:51 (6 ч 10 мин): наработка по счётчику 6 ч 09 мин; счётчика "
         "пусков у машины нет — были ли пуски, не видно",
      f"В1-1г: счётчиков пусков у машины нет — только наработка: {SER.outage_facts_text(_f4, MSK)}")
check(SER.outage_counter_facts(_g0, _g1, {}, {}) is None and SER.outage_facts_text(None) is None,
      "В1-1д: нет ни одной пары показаний — фактов нет")

# Обрывы, кончившиеся в сегменте: [t0, t1), смежные записи склеены, синтетика — нет
_sp = eng._outage_spans([
    {"gap_start": M(24, 5, 48, 15), "gap_end": M(24, 11, 48, 9)},
    {"gap_start": M(24, 11, 48, 9), "gap_end": M(24, 12, 38, 58)},
    {"gap_start": M(24, 13, 0), "gap_end": None},
    {"gap_start": M(24, 9, 0), "gap_end": M(24, 9, 30), "start_unknown": True},
    {"gap_start": M(24, 20, 0), "gap_end": M(25, 9, 0)},
], M(24, 9, 0), M(25, 9, 0))
check(_sp == [(M(24, 5, 48, 15), M(24, 12, 38, 58))],
      f"В1-1е: склейка 615+616, без синтетики и без конца на t1: {_sp}")
check(eng._outage_spans([{"gap_start": M(24, 20, 0), "gap_end": M(25, 9, 0)}], M(25, 9, 0), M(26, 9, 0))
      == [(M(24, 20, 0), M(25, 9, 0))], "В1-1е: конец ровно на начале сегмента — первые данные в нём")
_ro = {"run_origin_unknown": True, "origin_from": iso(G1), "gap_from": iso(G0), "gap_to": iso(G1), "run_state": 3}
check(SER.run_origin_days(_ro, MSK) == "25–26.10"
      and SER.run_origin_days(dict(_ro, gap_from=iso(M(26, 1, 0))), MSK) == "26.10"
      and SER.run_origin_days(dict(_ro, gap_from=iso(M(30, 22, 0, mo=9))), MSK) == "30.09–26.10"
      and SER.fmt_dm_round(G1, MSK) == "26.10 18:56",
      "В1-1ж: дни обрыва «25–26.10», «26.10», «30.09–26.10»; начало «26.10 18:56»")
check(SER.run_origin_valid(_ro, 3) and not SER.run_origin_valid(_ro, 0)
      and not SER.run_origin_valid(dict(_ro, origin_from=None)) and not SER.run_origin_valid(None),
      "В1-1з: признак годен только своего режима и с origin_from")

# ══ В1-2. Движок: _outage_facts (запрос показаний до обрыва — один, на закрытии) ═
import analytics.source as src  # noqa: E402
from db import remote  # noqa: E402

_LVB: list = []


def _lvb_with(table):
    async def fn(sn, et, p, addrs, ts_before, max_age):
        _LVB.append((tuple(sorted(addrs)), ts_before))
        return {a: {"ts": table[a][0], "value": table[a][1]} for a in addrs
                if a in table and table[a][0] < ts_before and table[a][0] >= ts_before - timedelta(seconds=max_age)}
    return fn


_SAVE_LVB = src.get_last_values_before


def mk_engine(cfg=CFG3):
    return eng.OnlinePollEngine("1126246373", "pcc", 1, "x", cfg, poll_interval_sec=30, daily_hour=9, tz=MSK)


def rows_of(points):
    out: dict = {}
    for a, t, v in points:
        out.setdefault(_ROLE[a], []).append((t, v))
    for v in out.values():
        v.sort()
    return out


try:
    src.get_last_values_before = _lvb_with(BEFORE)
    _e = mk_engine()
    _rows = rows_of([(ATT, G1, 963.0), (SUC, G1, 953.0), (HRS, G1, 56182500.0), (HRS, S(G1, 180), 56182680.0)])
    _LVB.clear()
    _facts, _org = asyncio.run(_e._outage_facts(M(26, 9, 0), M(27, 9, 0), [{"gap_start": G0, "gap_end": G1}],
                                                _rows, M(26, 8, 55), 3))
    check(len(_LVB) == 1 and _LVB[0][1] == S(G0, 1) and len(_facts) == 1 and _facts[0]["text"] == TXT_3,
          f"В1-2а: обрыв начался до истории окна — один запрос до его начала, строка ДЭС №3: {_LVB} {_facts}")
    check(_org == {"run_origin_unknown": True, "origin_from": iso(G1), "gap_from": iso(G0), "gap_to": iso(G1),
                   "run_state": 3}, f"В1-2а: признак начала режима — первые данные после обрыва: {_org}")

    # Обрыв внутри истории окна, счётчики не менялись: строка (≥ 30 мин), без запроса и признака
    _a, _b = M(26, 12, 0), M(26, 18, 0)
    _rows2 = rows_of([(ATT, S(_a, -600), 963.0), (SUC, S(_a, -600), 953.0), (HRS, S(_a, -60), 1000.0),
                      (ATT, _b, 963.0), (SUC, _b, 953.0), (HRS, _b, 1000.0 + 21600)])
    _LVB.clear()
    _f2, _o2 = asyncio.run(_e._outage_facts(M(26, 9, 0), M(27, 9, 0), [{"gap_start": _a, "gap_end": _b}],
                                            _rows2, M(26, 8, 55), 3))
    check(not _LVB and len(_f2) == 1 and _o2 is None and "пусков не было" in _f2[0]["text"],
          f"В1-2б: счётчики не менялись — строка без признака и без запроса: {_LVB} {_f2} {_o2}")
    # Короткий обрыв без смены — строки нет; со сменой — есть и признак (при любой длине)
    _c, _d = M(26, 21, 43, 59), M(26, 21, 47, 17)
    _rows3 = rows_of([(ATT, S(_c, -60), 963.0), (SUC, S(_c, -60), 953.0), (ATT, _d, 963.0), (SUC, _d, 953.0)])
    _f3, _o3 = asyncio.run(_e._outage_facts(M(26, 9, 0), M(27, 9, 0), [{"gap_start": _c, "gap_end": _d}],
                                            _rows3, M(26, 8, 55), 3))
    check(_f3 == [] and _o3 is None, f"В1-2в: обрыв 3 мин без смены счётчиков — без строки: {_f3}")
    _rows4 = rows_of([(ATT, S(_c, -60), 963.0), (SUC, S(_c, -60), 953.0), (ATT, _d, 964.0), (SUC, _d, 954.0)])
    _f4, _o4 = asyncio.run(_e._outage_facts(M(26, 9, 0), M(27, 9, 0), [{"gap_start": _c, "gap_end": _d}],
                                            _rows4, M(26, 8, 55), 3))
    check(len(_f4) == 1 and "1 пуск (попыток 963 → 964" in _f4[0]["text"] and _o4
          and _o4["origin_from"] == iso(_d), f"В1-2г: обрыв 3 мин с пуском — строка и признак: {_f4} {_o4}")
    # Переходный режим — строка без признака (цепочки у него нет)
    _f5, _o5 = asyncio.run(_e._outage_facts(M(26, 9, 0), M(27, 9, 0), [{"gap_start": _c, "gap_end": _d}],
                                            _rows4, M(26, 8, 55), 5))
    check(len(_f5) == 1 and _o5 is None, f"В1-2д: режим 5 — без признака: {_o5}")

    # Машина без счётчиков пусков (роль не заведена) — только наработка, без признака
    class _NoStarts:
        def __init__(self, cfg, drop):
            self._c, self._drop = cfg, set(drop)

        def role_to_addr(self, role):
            return None if role in self._drop else self._c.role_to_addr(role)

        def __getattr__(self, n):
            return getattr(self._c, n)

    _e6 = mk_engine(_NoStarts(CFG3, {"START_ATTEMPTS_TOTAL", "START_SUCCESS_TOTAL"}))
    _LVB.clear()
    _f6, _o6 = asyncio.run(_e6._outage_facts(M(26, 9, 0), M(27, 9, 0), [{"gap_start": G0, "gap_end": G1}],
                                             _rows, M(26, 8, 55), 3))
    check(len(_f6) == 1 and _f6[0]["starts_changed"] is None and "счётчика пусков у машины нет" in _f6[0]["text"]
          and _o6 is None and _LVB and _LVB[0][0] == (HRS,),
          f"В1-2е: нет счётчиков пусков — только наработка, «не меньше» не ставится: {_f6} {_o6} {_LVB}")
    _e7 = mk_engine(_NoStarts(CFG3, set(SER.OUTAGE_COUNTER_ROLES)))
    _LVB.clear()
    _f7 = asyncio.run(_e7._outage_facts(M(26, 9, 0), M(27, 9, 0), [{"gap_start": G0, "gap_end": G1}],
                                        _rows, M(26, 8, 55), 3))
    check(_f7 == ([], None) and not _LVB, f"В1-2ж: счётчиков у машины нет вовсе — как раньше, без запроса: {_f7}")

    # Сбой запроса — без фактов, предупреждение
    async def _boom(*a, **k):
        raise RuntimeError("source down")
    src.get_last_values_before = _boom
    _k0 = len(LOG.recs)
    # Новый движок: у прежнего показания до обрыва уже в снимке (запрос из В1-2а)
    _f8 = asyncio.run(mk_engine()._outage_facts(M(26, 9, 0), M(27, 9, 0), [{"gap_start": G0, "gap_end": G1}],
                                       _rows, M(26, 8, 55), 3))
    check(_f8 == ([], None) and msgs(logging.WARNING, "факты обрыва связи", _k0),
          f"В1-2з: сбой запроса — сегмент без фактов, предупреждение: {_f8}")
finally:
    src.get_last_values_before = _SAVE_LVB


# ══ В1-3. Полный путь: суточный рез #36137, следующий цикл, статус, рестарт ═══
from online import db as _odb_real  # noqa: E402


class FakeDB:
    """online.db в памяти: записи сегментов, журнал тревог, счётчики эпизодов."""

    def __init__(self):
        self.closed: list[dict] = []
        self.open_row: dict | None = None
        self.journal: list[dict] = []
        self.active: list[dict] = []
        self.counts: dict = {}
        self.last_closed: dict | None = None
        self.eid = 100
        self.calls: list[str] = []

    def episode_key(self, *a, **k):
        return _odb_real.episode_key(*a, **k)

    async def insert_closed_segment(self, data):
        self.closed.append(dict(data))
        return 36137 + len(self.closed) - 1

    async def upsert_open_segment(self, data):
        self.open_row = dict(data)
        return 36162

    async def pop_open_segment(self, *a):
        r, self.open_row = self.open_row, None
        return r

    async def get_open_segment(self, *a):
        return self.open_row

    async def count_episodes_batch(self, *a, **k):
        self.calls.append("count_episodes_batch")
        return {k2: dict(v) for k2, v in self.counts.items()}

    async def get_episodes_overlapping(self, *a, **k):
        return []

    async def get_open_episodes(self, *a, **k):
        return []

    async def existing_episode_onsets(self, *a, **k):
        return set()

    async def find_episode_by_onset(self, *a, **k):
        return None

    async def open_episode(self, *a, **k):
        self.eid += 1
        return self.eid

    async def insert_closed_episode(self, *a, **k):
        self.eid += 1
        return self.eid

    async def insert_alert_events(self, sn, et, p, events):
        self.journal.extend(dict(e) for e in events)

    async def get_active_alerts(self, *a):
        return [dict(x) for x in self.active]

    async def get_last_closed_segment(self, *a):
        return self.last_closed

    async def get_transition_chain(self, *a, **k):
        return []

    def __getattr__(self, name):
        async def fn(*a, **k):
            self.calls.append(name)
            return None
        return fn


_ENUM = [{"addr": 40010, "state_start": M(24, 15, 59, 40), "state_end": None, "value": 2},
         {"addr": 40011, "state_start": M(24, 15, 59, 40), "state_end": None, "value": 3},
         {"addr": 40012, "state_start": M(24, 16, 0, 53), "state_end": None, "value": 1453},
         {"addr": 40013, "state_start": M(24, 16, 0, 53), "state_end": None, "value": 1}]
CA = {"addr": 40408, "bit": 11, "fault_start": M(24, 16, 0, 53), "fault_end": None, "severity": None,
      "fault_name": "CommonAlarm", "fault_name_ru": "Общая авария"}
CA_KEY = "CONTROLLER_FAULT|40408|11"


def pings(a, b, step=30, extra=None):
    out, t = [], a
    while t < b:
        out.append({"addr": HB, "ts": t, "value": 1.0})
        t += timedelta(seconds=step)
    return out + list(extra or [])


def counters(t, att, suc, hrs):
    return [{"addr": ATT, "ts": t, "value": att}, {"addr": SUC, "ts": t, "value": suc},
            {"addr": HRS, "ts": t, "value": hrs}]


class World:
    """Источник телеметрии в памяти: история, ленты, разрывы, показания до обрыва."""

    def __init__(self, hist, gaps, before=None, enum=None, faults=None):
        self.hist = sorted(hist, key=lambda r: r["ts"])
        self.gaps = gaps
        self.before = before or {}
        self.enum = enum if enum is not None else _ENUM
        self.faults = faults if faults is not None else [CA]
        self.lvb: list = []

    async def history(self, sn, et, p, a, b, wl, meta=None):
        out = [dict(r) for r in self.hist if a <= r["ts"] < b]
        if meta is not None:
            meta["max_ts"] = max((r["ts"] for r in out), default=None)
        return out

    async def enum_periods(self, *a, **k):
        return [dict(r) for r in self.enum]

    async def fault_periods(self, *a, **k):
        return [dict(r) for r in self.faults]

    async def data_gaps(self, sn, et, p, a, b):
        # now — момент цикла: запись data_gaps появляется с первым пакетом после разрыва
        now = getattr(self, "now", None)
        return [dict(g) for g in self.gaps if g["gap_start"] < b and (g["gap_end"] is None or g["gap_end"] > a)
                and (now is None or (g["gap_end"] is not None and g["gap_end"] <= now))]

    async def timeline(self, *a, **k):
        return [dict(r) for r in self.enum], [dict(r) for r in self.faults]

    async def lvb_fn(self, sn, et, p, addrs, ts_before, max_age):
        self.lvb.append((tuple(sorted(addrs)), ts_before))
        return {a: {"ts": self.before[a][0], "value": self.before[a][1]} for a in addrs
                if a in self.before and self.before[a][0] < ts_before}


async def _none(*a, **k):
    return None


async def _ttb(func, **k):
    return func()


async def _goc(*aws):
    return list(await asyncio.gather(*aws))


import online.verifier as _ver  # noqa: E402

_SAVE = {"db": eng.online_db, "ls": eng._load_seed, "ttb": remote.to_thread_beating,
         "goc": remote.gather_or_cancel, "enq": eng._enqueue_segment, "fv": _ver.fire_verify}
_SRC_NAMES = ("get_whitelist_history_chunked", "get_whitelist_history", "get_enum_periods",
              "get_fault_periods", "get_data_gaps", "get_timeline_periods", "get_last_values_before")
_SAVE_SRC = {n: getattr(src, n) for n in _SRC_NAMES}


def install(world: World, db: FakeDB) -> None:
    eng.online_db = db
    src.get_whitelist_history_chunked = world.history
    src.get_whitelist_history = world.history
    src.get_enum_periods = world.enum_periods
    src.get_fault_periods = world.fault_periods
    src.get_data_gaps = world.data_gaps
    src.get_timeline_periods = world.timeline
    src.get_last_values_before = world.lvb_fn
    eng._load_seed = _none
    remote.to_thread_beating = _ttb
    remote.gather_or_cancel = _goc
    eng._enqueue_segment = lambda *a, **k: None
    _ver.fire_verify = lambda *a, **k: None


def restore() -> None:
    eng.online_db = _SAVE["db"]
    eng._load_seed = _SAVE["ls"]
    remote.to_thread_beating = _SAVE["ttb"]
    remote.gather_or_cancel = _SAVE["goc"]
    eng._enqueue_segment = _SAVE["enq"]
    _ver.fire_verify = _SAVE["fv"]
    for _n2, _f0 in _SAVE_SRC.items():
        setattr(src, _n2, _f0)


def seg_row(data: dict) -> dict:
    """Открытая строка, как её читает статус (поля upsert_open_segment)."""
    return {"run_state": data["run_state"], "t_start": data["t_start"],
            "characteristics_json": data.get("characteristics_json"),
            "current_values_json": data.get("current_values_json"),
            "active_detections_json": data.get("active_detections_json"),
            "coking_risk_json": data.get("coking_risk_json")}


_CNT = {"count_window": 3, "dur_window": 2193009.0, "blind_window": 293027.0, "count_since": 0, "dur_since": 0.0}
CUT26, CUT27 = M(26, 9, 0), M(27, 9, 0)

# История #36137: связи нет до 18:55:59, затем пинги и счётчики (963 / 953)
_H37 = pings(G1, S(CUT27, 900), extra=counters(G1, 963.0, 953.0, 56182500.0)
             + [{"addr": HRS, "ts": S(G1, 180 * k), "value": 56182500.0 + 180 * k} for k in range(1, 300)]
             + [{"addr": ATT, "ts": S(G1, 900 * k), "value": 963.0} for k in range(1, 60)]
             + [{"addr": SUC, "ts": S(G1, 900 * k), "value": 953.0} for k in range(1, 60)])
_GAPS37 = [{"gap_start": G0, "gap_end": G1}, {"gap_start": M(26, 21, 43, 59), "gap_end": M(26, 21, 47, 17)}]

try:
    _w = World(_H37, _GAPS37, BEFORE)
    _db = FakeDB()
    _db.counts = {CA_KEY: dict(_CNT)}
    install(_w, _db)
    _e = mk_engine()
    _e.cursor_ts = CUT26
    _e.continued_from_id = None        # после заглушки #36125 цепочка рвётся (v4.9.57)
    _e.inherited_run_state_sec = {}
    _e._prev_seg_hint = eng._make_seg_hint({"run_state": 3, "t_start": M(25, 9, 0), "t_end": CUT26,
                                            "characteristics_json": {}})      # заглушка #36125
    _e._last_open_run_state = 3
    asyncio.run(_e._close_window(CUT26, CUT27, "DAILY_BOUNDARY", lookahead_to=CUT27, live_to=CUT27))
    _c37 = _db.closed[-1] if _db.closed else {}
    _ch = _c37.get("characteristics_json") or {}
    _ff = _c37.get("forward_fill_json") or {}
    _rmd = _c37.get("report_md") or ""
    _smd = _c37.get("report_summary_md") or ""
    _sj = _c37.get("report_summary_json") or {}
    check(len(_w.lvb) == 1 and _w.lvb[0][1] == S(G0, 1),
          f"В1-3а: на закрытии — один запрос показаний до обрыва: {_w.lvb}")
    check(_ch.get("run_origin_unknown") is True and _ch.get("origin_from") == iso(G1)
          and _ch.get("origin_gap") == {"from": iso(G0), "to": iso(G1)} and _c37.get("continued_from") is None,
          f"В1-3а: признаки головы цепочки run_origin_unknown / origin_from: {[_ch.get(k) for k in ('run_origin_unknown', 'origin_from', 'origin_gap')]}")
    _sec = (CUT27 - G1).total_seconds()
    check(_ff.get("_run_state_sec") == {"3": _sec} and (_ff.get("_run_origin") or {}).get("origin_from") == iso(G1),
          f"В1-3б: время режима по цепочке — от первых данных после обрыва ({_sec}): {_ff.get('_run_state_sec')}")
    check(f"- ℹ️ {TXT_3}" in _rmd, "В1-3в: строка фактов в полном отчёте #36137")
    check("с начала состояния (с 26.10 18:56, пуск в обрыве) | не меньше 14 ч 04 мин |"
          in _rmd, "В1-3в: «с начала состояния (с 26.10 18:56, пуск в обрыве)» в отчёте головы: "
          + str([ln for ln in _rmd.splitlines() if "начала состояния" in ln]))
    check(f"- ℹ️ {TXT_3}" in _smd and [x.get("text") for x in _sj.get("outage_facts") or []] == [TXT_3],
          f"В1-3г: строка фактов в сводке и в её частях: {_sj.get('outage_facts')}")
    check("Срабатываний с 26.10 18:56 (пуск в обрыве связи): **0**" in _rmd,
          "В1-3д: «Срабатываний с пуска» — с первых данных после обрыва: "
          + str([ln for ln in _rmd.splitlines() if "Срабатываний с" in ln]))
    check(_e.inherited_run_origin == {"run_origin_unknown": True, "origin_from": iso(G1), "gap_from": iso(G0),
                                      "gap_to": iso(G1), "run_state": 3}
          and _e.inherited_run_state_sec == {3: _sec} and _e.continued_from_id == 36137,
          f"В1-3е: признак и время режима идут по цепочке: {_e.inherited_run_origin} {_e.inherited_run_state_sec}")
    check(len([m for m in msgs(logging.INFO, "обрыв связи по счётчикам панели") if "начало режима не видно" in m]) >= 1,
          "В1-3е: факты — в журнале сервиса (INFO)")

    # Следующий цикл открытого окна: без запросов, «не меньше» в статусе
    _w.lvb.clear()
    asyncio.run(_e._update_open_window(CUT27, S(CUT27, 600)))
    _op = _db.open_row or {}
    _och = _op.get("characteristics_json") or {}
    check(not _w.lvb, f"В1-3ж: цикл открытого окна — без запроса показаний до обрыва: {_w.lvb}")
    check((_och.get("_run_origin") or {}).get("origin_from") == iso(G1) and _op.get("continued_from") == 36137
          and abs(_och["_total_run_state_sec"][3] - (_sec + 600)) < 1,
          f"В1-3ж: открытая строка — признак и время от 18:55:59: {_och.get('_run_origin')} {_och.get('_total_run_state_sec')}")
    _st = SA.build_structural_status(seg_row(_op), FR3, MSK)
    _txt = SA.format_status_text(_st)
    check(_txt.startswith(f"Работа ≥ 14 ч 14 мин (последний пуск{NBSP}— в обрыве 25–26.10) — "),
          f"В1-3з: статус «Работа ≥ … (последний пуск — в обрыве 25–26.10)»: {_txt!r}")
    # UI режет хвост по первому « — » после подписи режима — скобка не режет
    _tail = _txt[_txt.index(" — ", len("Работа")) + 3:]
    check(_tail.startswith("🟡 панель: Общая авария"), f"В1-3з: хвост строки для UI: {_tail!r}")
    check(_st["time_in_mode_at_least"] is True and _st["run_origin"]["origin_from"] == iso(G1),
          f"В1-3з: признак «не меньше» в структуре статуса: {_st.get('run_origin')}")
    _cf = [d for d in _op.get("active_detections_json") or [] if d.get("scenario") == "CONTROLLER_FAULT"]
    check(_cf and _cf[0]["values"].get("startup_since") == iso(G1) and _cf[0]["values"].get("startup_origin_unknown"),
          f"В1-3и: счётчик «с пуска» открытой строки — с 18:55:59, с пометкой: {[d.get('values') for d in _cf]}")
    _prompt = SA.build_warning_prompt(_st)
    check("Время в режиме: не меньше 14 ч 14 мин (последний пуск — в обрыве 25–26.10)" in _prompt,
          "В1-3к: вход гейта — «не меньше»: " + str([ln for ln in _prompt.splitlines() if "Время в режиме" in ln]))

    # Рестарт после реза: признак — из forward_fill_json последнего закрытого
    _db.last_closed = dict(_c37, id=36137, t_end=CUT27, cause_close="DAILY_BOUNDARY")
    _e2 = mk_engine()
    asyncio.run(_e2.initialize(CUT27))
    check(_e2.inherited_run_origin == _e.inherited_run_origin and _e2.inherited_run_state_sec == {3: _sec}
          and _e2.continued_from_id == 36137, f"В1-3л: рестарт — признак поднят: {_e2.inherited_run_origin}")
    _db.open_row = None
    asyncio.run(_e2._update_open_window(CUT27, S(CUT27, 600)))
    _txt2 = SA.format_status_text(SA.build_structural_status(seg_row(_db.open_row or {}), FR3, MSK))
    check(_txt2.startswith(f"Работа ≥ 14 ч 14 мин (последний пуск{NBSP}— в обрыве 25–26.10)"),
          f"В1-3л: после рестарта статус тот же: {_txt2!r}")
    # Следующий суточный рез: цепочка несёт признак, «с начала состояния» — от 18:56
    _e2._open_history_cache = None
    asyncio.run(_e2._close_window(CUT27, M(28, 9, 0), "DAILY_BOUNDARY", lookahead_to=M(28, 9, 0),
                                  live_to=M(28, 9, 0)))
    _c62 = _db.closed[-1]
    check(_c62.get("continued_from") == 36137 and not (_c62.get("characteristics_json") or {}).get("run_origin_unknown")
          and ((_c62.get("forward_fill_json") or {}).get("_run_origin") or {}).get("origin_from") == iso(G1)
          and "с начала состояния (с 26.10 18:56, пуск в обрыве) | не меньше 38 ч 04 мин" in (_c62.get("report_md") or ""),
          "В1-3м: следующие сутки — продолжение цепочки, признак по ff, «не меньше 38 ч 04 мин»: "
          + str([ln for ln in (_c62.get("report_md") or "").splitlines() if "начала состояния" in ln]))

    # ── Счётчики пусков не менялись — как раньше (строка фактов есть, «не меньше» нет) ──
    _w = World(_H37, _GAPS37, {ATT: (M(25, 0, 0, 32), 963.0), SUC: (M(25, 0, 1, 44), 953.0),
                               HRS: (M(25, 0, 8, 11), 56182500.0 - 153896)})
    _db = FakeDB()
    _db.counts = {CA_KEY: dict(_CNT)}
    install(_w, _db)
    _e = mk_engine()
    _e.cursor_ts, _e.continued_from_id, _e._last_open_run_state = CUT26, None, 3
    asyncio.run(_e._close_window(CUT26, CUT27, "DAILY_BOUNDARY", lookahead_to=CUT27, live_to=CUT27))
    _c = _db.closed[-1]
    _ch = _c.get("characteristics_json") or {}
    check(not _ch.get("run_origin_unknown") and "_run_origin" not in (_c.get("forward_fill_json") or {})
          and (_c.get("forward_fill_json") or {}).get("_run_state_sec") == {"3": 86400.0}
          and "по счётчикам пусков не было (попыток 963, успешных 953)" in (_c.get("report_summary_md") or "")
          and "с начала состояния" not in (_c.get("report_md") or "")
          and "Срабатываний с пуска: **0**" in (_c.get("report_md") or "") and _e.inherited_run_origin is None,
          f"В1-3н: счётчики не менялись — время от начала сегмента, как раньше, строка фактов есть: "
          f"{(_c.get('forward_fill_json') or {}).get('_run_state_sec')}")

    # ── У машины нет счётчиков в данных — как раньше, без строки и без запроса ──
    _w = World([r for r in _H37 if r["addr"] == HB], _GAPS37, {})
    _db = FakeDB()
    install(_w, _db)
    _e = mk_engine()
    _e.cursor_ts, _e.continued_from_id, _e._last_open_run_state = CUT26, None, 3
    asyncio.run(_e._close_window(CUT26, CUT27, "DAILY_BOUNDARY", lookahead_to=CUT27, live_to=CUT27))
    _c = _db.closed[-1]
    check(not _w.lvb and "Обрыв связи" not in (_c.get("report_summary_md") or "")
          and not (_c.get("report_summary_json") or {}).get("outage_facts")
          and (_c.get("forward_fill_json") or {}).get("_run_state_sec") == {"3": 86400.0},
          f"В1-3о: счётчиков в данных нет — как раньше: {_w.lvb}")

    # ── Обрыв начался внутри «Работы» предыдущих суток и кончился в этих (без заглушки) ──
    _ga, _gb = M(27, 6, 2, 8), M(27, 12, 11, 51)
    _h = (pings(S(CUT27, -300), _ga, extra=counters(S(_ga, -60), 961.0, 951.0, 5000000.0))
          + pings(_gb, M(28, 9, 0), extra=counters(_gb, 962.0, 952.0, 5000000.0 + 20000)))
    _w = World(_h, [{"gap_start": _ga, "gap_end": _gb}],
               {ATT: (S(_ga, -60), 961.0), SUC: (S(_ga, -60), 951.0), HRS: (S(_ga, -60), 5000000.0)})
    _db = FakeDB()
    install(_w, _db)
    _e = mk_engine()
    _e.cursor_ts, _e.continued_from_id, _e._last_open_run_state = CUT27, 36137, 3
    _e.inherited_run_state_sec = {3: 86400.0}
    _e._prev_seg_hint = eng._make_seg_hint({"run_state": 3, "t_start": CUT26, "t_end": CUT27,
                                            "characteristics_json": {}})
    asyncio.run(_e._close_window(CUT27, M(28, 9, 0), "DAILY_BOUNDARY", lookahead_to=M(28, 9, 0), live_to=M(28, 9, 0)))
    _c = _db.closed[-1]
    _ch = _c.get("characteristics_json") or {}
    _sec2 = (M(28, 9, 0) - _gb).total_seconds()
    check(len(_w.lvb) == 1 and _ch.get("origin_from") == iso(_gb) and _c.get("continued_from") is None
          and (_c.get("forward_fill_json") or {}).get("_run_state_sec") == {"3": _sec2}
          and "по счётчикам 1 пуск (попыток 961 → 962, успешных 951 → 952)" in (_c.get("report_summary_md") or "")
          and "с начала состояния (с 27.10 12:12, пуск в обрыве)" in (_c.get("report_md") or ""),
          f"В1-3п: обрыв из предыдущих суток — пуск в нём рвёт цепочку, время от 12:11:51: "
          f"{_c.get('continued_from')} {(_c.get('forward_fill_json') or {}).get('_run_state_sec')} {_w.lvb} "
          f"{[ln for ln in (_c.get('report_summary_md') or '').splitlines() if 'Обрыв' in ln]} "
          f"{[ln for ln in (_c.get('report_md') or '').splitlines() if 'начала состояния' in ln]}")
    # Тот же обрыв без пуска — цепочка продолжается, время — от пуска до обрыва, как раньше
    _h2 = [dict(r, value=961.0) if r["addr"] == ATT else dict(r, value=951.0) if r["addr"] == SUC else r for r in _h]
    _w = World(_h2, [{"gap_start": _ga, "gap_end": _gb}],
               {ATT: (S(_ga, -60), 961.0), SUC: (S(_ga, -60), 951.0), HRS: (S(_ga, -60), 5000000.0)})
    _db = FakeDB()
    install(_w, _db)
    _e = mk_engine()
    _e.cursor_ts, _e.continued_from_id, _e._last_open_run_state = CUT27, 36137, 3
    _e.inherited_run_state_sec = {3: 86400.0}
    _e._prev_seg_hint = eng._make_seg_hint({"run_state": 3, "t_start": CUT26, "t_end": CUT27,
                                            "characteristics_json": {}})
    asyncio.run(_e._close_window(CUT27, M(28, 9, 0), "DAILY_BOUNDARY", lookahead_to=M(28, 9, 0), live_to=M(28, 9, 0)))
    _c = _db.closed[-1]
    check(_c.get("continued_from") == 36137 and not (_c.get("characteristics_json") or {}).get("run_origin_unknown")
          and (_c.get("forward_fill_json") or {}).get("_run_state_sec") == {"3": 172800.0}
          and "пусков не было" in (_c.get("report_summary_md") or ""),
          f"В1-3р: без пуска в обрыве — цепочка и время как раньше: {_c.get('continued_from')} "
          f"{(_c.get('forward_fill_json') or {}).get('_run_state_sec')}")

    # ── Смена режима после обрыва: факты — в сегменте, закрытом сменой режима ──
    _en = [dict(r) for r in _ENUM if r["addr"] != 40011] + [
        {"addr": 40011, "state_start": M(24, 15, 59, 40), "state_end": M(26, 19, 30), "value": 3},
        {"addr": 40011, "state_start": M(26, 19, 30), "state_end": None, "value": 0}]
    _w = World(pings(G1, M(26, 20, 0), extra=counters(G1, 963.0, 953.0, 56182500.0)), _GAPS37[:1], BEFORE,
               enum=_en)
    _db = FakeDB()
    install(_w, _db)
    _e = mk_engine()
    _e.cursor_ts, _e.continued_from_id, _e._last_open_run_state = CUT26, None, 3
    asyncio.run(_e._update_open_window(CUT26, M(26, 20, 0)))
    _rs = [c for c in _db.closed if c.get("run_state") == 3]
    _rc = (_rs[-1].get("characteristics_json") or {}) if _rs else {}
    check(_rs and _rc.get("origin_from") == iso(G1) and TXT_3 in (_rs[-1].get("report_summary_md") or "")
          and len(_w.lvb) == 1 and _e.inherited_run_origin is None
          and "_run_origin" not in ((_db.open_row or {}).get("characteristics_json") or {}),
          f"В1-3с: смена режима — факты в закрытой «Работе», новый режим без признака: "
          f"{[c.get('run_state') for c in _db.closed]} {_w.lvb}")
finally:
    restore()

check("keep_missing=_own_rows is False" in inspect.getsource(eng.OnlinePollEngine._update_open_window),
      "В3: условие у вызова _diff_alerts — окно без своих строк")

# ══ В2. Статус при висящей «Общей аварии» с кодом ═══════════════════════════
CA_DET = {"scenario": "CONTROLLER_FAULT", "severity": "CAUTION", "t_detected": iso(M(24, 16, 0, 53)),
          "source": "CONTROLLER_FAULT", "fault_codes": [1453], "ref_codes": [],
          "trigger": "Общая авария при несброшенном коде 1453 «Отказ отключения автомата генераторной установки»",
          "related_roles": ["FAULT_MASK_8", "LAST_FAULT_CODE"],
          "description_key": "CONTROLLER_FAULT.common_alarm_unconfirmed",
          "values": {"addr": 40408, "bit": 11, "fault_name": "Общая авария", "last_fault_code": 1453,
                     "last_fault_type": 1, "code_name": "Отказ отключения автомата генераторной установки",
                     "fault_start": iso(M(24, 16, 0, 53)), "fault_end": None,
                     "last_fault_code_since": iso(M(24, 16, 0, 53))}}
I2_DET = {"scenario": "NEGATIVE_SEQUENCE", "severity": "CAUTION", "t_detected": iso(M(31, 10, 0)),
          "trigger": "Ток обратной последовательности: 12 %", "fault_codes": [], "values": {}}
WARN_DET = {"scenario": "CONTROLLER_FAULT", "severity": "WARNING", "t_detected": iso(M(31, 10, 0)),
            "fault_codes": [1248], "values": {"addr": 40406, "bit": 2, "fault_name": "Предупреждение двигателя",
                                              "fault_start": iso(M(31, 10, 0))}}
SHUT_DET = {"scenario": "CONTROLLER_FAULT", "severity": "SHUTDOWN", "t_detected": iso(M(31, 10, 0)),
            "fault_codes": [], "values": {"addr": 40405, "bit": 15, "fault_name": "Неисправность при останове",
                                          "fault_start": iso(M(31, 10, 0))}}
CA_TXT = "Общая авария — код 1453 «Отказ отключения автомата генераторной установки» с 24.10 16:00"


def row(dets, rs=3, sec=3600.0, supp=None):
    return {"run_state": rs, "t_start": M(31, 9, 0),
            "characteristics_json": {"_total_run_state_sec": {str(rs): sec}},
            "active_detections_json": dets, "current_values_json": {"values": {}},
            "gate_suppressed_hash": supp}


check(SA.compute_severity_level([CA_DET]) == "предупреждение" and SA.compute_panel_severity([CA_DET]) == "норма"
      and SA.compute_severity_level([CA_DET], panel_caution=False) == "норма"
      and SA.compute_severity_level([]) == "норма",
      "В2-1: CAUTION панели — «предупреждение», источник панели «норма»; календарь без частей — как раньше")
_s1 = SA.build_structural_status(row([CA_DET]), FR3, MSK)
_t1 = SA.format_status_text(_s1)
check(_s1["severity_level"] == "предупреждение" and _s1["panel_severity"] == "норма"
      and _s1["analytics_severity"] == "норма" and _t1 == f"Работа 1 ч 00 мин — 🟡 панель: {CA_TXT}.",
      f"В2-2: уровень и строка статуса: {_s1['severity_level']} / {_s1['panel_severity']} / {_t1!r}")
check(SA.status_alarm_text(_s1) == CA_TXT and SA.extract_alarm_text(_s1) is None,
      f"В2-3: alarm_text статуса — из панельного CAUTION; подпись разборов гейта — как раньше: "
      f"{SA.status_alarm_text(_s1)!r} / {SA.extract_alarm_text(_s1)!r}")
# Хеши составов гейта: строка CAUTION в них не входит
_s1b = {**_s1, "panel_alarms": [{k: v for k, v in a.items() if k != "caution_text"} for a in _s1["panel_alarms"]]}
check(SA.compute_fault_hash(_s1) == SA.compute_fault_hash(_s1b)
      and SA.compute_analytics_hash([CA_DET, I2_DET]) == SA.compute_analytics_hash([I2_DET]),
      "В2-4: хеши составов гейта прежние")
# Аналитика рядом — обе части в строке; главная тревога (решение Р2): код висит с
# прошлых суток — первой аналитика, в конце «+ код 1453 не сброшен»
_s2 = SA.build_structural_status(row([CA_DET, I2_DET]), FR3, MSK)
_t2 = SA.format_status_text(_s2)
check(_t2 == f"Работа 1 ч 00 мин — 🟡 панель: {CA_TXT} | 🟡 аналитика: Ток обратной последовательности."
      and SA.status_alarm_text(_s2) == "Ток обратной последовательности + код 1453 не сброшен"
      and SA.extract_alarm_text(_s2) == "Ток обратной последовательности",
      f"В2-5: панель и аналитика: {_t2!r} / {SA.status_alarm_text(_s2)!r}")
# Код свежий (появился в этом сегменте) — первой Общая авария
_ca_fresh = dict(CA_DET, t_detected=iso(M(31, 9, 30)),
                 values=dict(CA_DET["values"], fault_start=iso(M(31, 9, 30)), last_fault_code_since=iso(M(31, 9, 30))))
_s2f = SA.build_structural_status(row([_ca_fresh, I2_DET]), FR3, MSK)
check(SA.status_alarm_text(_s2f) == "Общая авария — код 1453 «Отказ отключения автомата генераторной установки» "
      "с 31.10 09:30 + ещё 1", f"В2-5а: свежий код — первым (Р2): {SA.status_alarm_text(_s2f)!r}")
# Подпись 🟠/🔴 — тревога панели наибольшего ранга, а не первая (ревью В2, п. 5)
_s4b = SA.build_structural_status(row([CA_DET, WARN_DET]), FR3, MSK)
check("🟠 панель: " in SA.format_status_text(_s4b) and "Общая авария" not in SA.format_status_text(_s4b),
      f"В2-5б: CAUTION первым в списке — подпись по WARNING: {SA.format_status_text(_s4b)!r}")
# Без Общей аварии — как раньше
_s3 = SA.build_structural_status(row([]), FR3, MSK)
check(_s3["severity_level"] == "норма" and SA.format_status_text(_s3) == "Работа 1 ч 00 мин — параметры в норме."
      and SA.status_alarm_text(_s3) is None, f"В2-6: без Общей аварии — как раньше: {SA.format_status_text(_s3)!r}")
# Настоящий WARNING / SHUTDOWN панели — как раньше, строки CAUTION нет
_s4 = SA.build_structural_status(row([CA_DET, WARN_DET]), FR3, MSK)
_s5 = SA.build_structural_status(row([CA_DET, SHUT_DET]), FR3, MSK)
check(_s4["severity_level"] == "внимание" and "🟠 панель:" in SA.format_status_text(_s4)
      and "🟡 панель" not in SA.format_status_text(_s4)
      and _s5["severity_level"] == "авария" and "🔴 панель:" in SA.format_status_text(_s5)
      and "🟡 панель" not in SA.format_status_text(_s5)
      and SA.status_alarm_text(_s4) == SA.extract_alarm_text(_s4),
      f"В2-7: WARNING / SHUTDOWN панели — как раньше: {SA.format_status_text(_s4)!r} / {SA.format_status_text(_s5)!r}")
# Снятая гейтом аналитика при Общей аварии: «проверено ИИ» и жёлтая панель
_h_i2 = SA.compute_analytics_hash([I2_DET])
_s6 = SA.build_structural_status(row([CA_DET, dict(I2_DET, scenario="COOLING_FAILURE", trigger="ОЖ 96 °C")],
                                     supp=SA.compute_analytics_hash([dict(I2_DET, scenario="COOLING_FAILURE")])),
                                 FR3, MSK)
_t6 = SA.format_status_text(_s6)
check(_s6["analytics_suppressed"] and _s6["severity_level"] == "предупреждение"
      and "🟡 панель: Общая авария" in _t6 and "✓ аналитика" in _t6,
      f"В2-8: снятая аналитика — «проверено ИИ», уровень по панели: {_s6['severity_level']} {_t6!r}")

# Гейт на одну Общую аварию не запускается; при аналитике — как раньше
import online.manager as _mgr  # noqa: E402
from db import analytics as _dba  # noqa: E402


def _eng_stub():
    now = datetime.now(U)
    return NS(router_sn="SN", equip_type="pcc", panel_id=1, _fault_ref=FR3, tz=MSK,
              last_data_ts=now - timedelta(seconds=10), data_read_to=now - timedelta(seconds=5),
              data_holes=[], live_ok_at=now, live_ok_real=True, beat_mono=time.monotonic(),
              poll_interval_sec=30, started_at=now - timedelta(hours=1))


async def _tick(dets):
    from online import db as odb
    st: dict = {}

    async def gos(*a):
        return dict(row(dets), id=36195, warning_analyses=[], gate_log=[])

    async def upd(sn, et, p, status_text, status_struct=None):
        st["text"], st["struct"] = status_text, status_struct

    async def setting(key, default=None):
        return default

    async def nothing(*a, **k):
        return 0
    real = (odb.get_open_segment, odb.update_open_segment_status, odb.clear_episodes_gate_suppressed,
            _dba.get_app_setting)
    odb.get_open_segment, odb.update_open_segment_status = gos, upd
    odb.clear_episodes_gate_suppressed, _dba.get_app_setting = nothing, setting
    try:
        m = _mgr.OnlineManager()
        m._engines = {"SN/pcc/1": _eng_stub()}
        await m._tick_status_lines()
        return m, st
    finally:
        (odb.get_open_segment, odb.update_open_segment_status, odb.clear_episodes_gate_suppressed,
         _dba.get_app_setting) = real


_m, _st = asyncio.run(_tick([CA_DET]))
check(not _m._warning_tracker and not _m._gate_inflight
      and (_st.get("struct") or {}).get("alarm_text") == CA_TXT and "🟡 панель: Общая авария" in (_st.get("text") or ""),
      f"В2-9: тик — статус с Общей аварией, гейт не запускается: {_m._warning_tracker} {_st}")
_m, _st = asyncio.run(_tick([CA_DET, I2_DET]))
check(list(_m._warning_tracker) == ["SN/pcc/1"], f"В2-10: с аналитикой — отсчёт стабилизации гейта, как раньше: "
                                                  f"{_m._warning_tracker}")

# ══ В3. Окно без своих строк — тревоги не закрываются ═══════════════════════
LP = {"scenario": "LIMIT_PROXIMITY", "severity": "CAUTION", "t_detected": iso(M(29, 3, 51, 29)),
      "trigger": "Температура масла: 101 °C", "fault_codes": [], "ref_codes": [144],
      "values": {"role": "OIL_TEMP", "shutdown_limit": 110.0}}
LP_KEY = eng._alert_key(LP)
check(LP_KEY == "LIMIT_PROXIMITY|OIL_TEMP", f"В3: ключ близости с ролью (v5.1.31): {LP_KEY}")
_act, _ev = eng._diff_alerts({LP_KEY: LP}, [], ts=M(29, 9, 0, 31), segment_id=None, keep_missing=True)
check(_act == [LP] and _ev == [], f"В3-1: окно без своих строк — тревога активна, CLOSED нет: {_ev}")
_act, _ev = eng._diff_alerts({LP_KEY: LP}, [], ts=M(29, 9, 0, 31), segment_id=None)
check(_act == [] and [e["event_type"] for e in _ev] == ["CLOSED"], "В3-2: окно со строками — CLOSED, как раньше")
_act, _ev = eng._diff_alerts({LP_KEY: LP}, [dict(LP, values=dict(LP["values"], live={"x": 1}))],
                             ts=M(29, 12, 12, 17), segment_id=None)
check(len(_act) == 1 and _ev == [], f"В3-3: возврат связи — без повторного OPENED: {_ev}")
_new = dict(I2_DET)
_act, _ev = eng._diff_alerts({LP_KEY: LP}, [_new], ts=M(29, 9, 0, 31), segment_id=None, keep_missing=True)
check(len(_act) == 2 and [e["event_type"] for e in _ev] == ["OPENED"],
      "В3-4: новая тревога в таком окне — OPENED, как раньше")

try:
    # Полный путь: рез 29.10 09:00 в обрыве (06:02:08–12:11:51), строк в окне нет
    _w = World([], [], {}, enum=_ENUM[:2], faults=[])
    _db = FakeDB()
    install(_w, _db)
    _e = mk_engine()
    _e.cursor_ts, _e._last_open_run_state = M(29, 9, 0), 3
    _e._active_alerts = {LP_KEY: dict(LP)}
    for _off in (31, 62, 93):
        asyncio.run(_e._update_open_window(M(29, 9, 0), S(M(29, 9, 0), _off)))
    check(not _db.journal and set(_e._active_alerts) == {LP_KEY}
          and [d.get("scenario") for d in (_db.open_row or {}).get("active_detections_json") or []] == ["LIMIT_PROXIMITY"],
          f"В3-5: рез в обрыве — CLOSED нет, тревога в живом статусе: {_db.journal} {list(_e._active_alerts)}")
    # Окно со своими строками без тревоги — CLOSED, как раньше
    _w.hist = pings(S(M(29, 9, 0), -300), S(M(29, 9, 0), 200))
    asyncio.run(_e._update_open_window(M(29, 9, 0), S(M(29, 9, 0), 124)))
    check([(j["scenario"], j["event_type"]) for j in _db.journal] == [("LIMIT_PROXIMITY", "CLOSED")]
          and not _e._active_alerts, f"В3-6: строки есть — закрытие, как раньше: {_db.journal}")

    # Рестарт внутри обрыва: OPENED в журнале без CLOSED → тревога живая, окно пустое — не закрывается
    _w = World([], [], {}, enum=_ENUM[:2], faults=[])
    _db = FakeDB()
    _db.active = [{"scenario": "LIMIT_PROXIMITY", "severity": "CAUTION", "trigger": LP["trigger"],
                   "values": dict(LP["values"]), "segment_id": 36192}]
    install(_w, _db)
    _e = mk_engine()
    _e.cursor_ts, _e._last_open_run_state = M(29, 9, 0), 3
    asyncio.run(_e._restore_live_state())
    check(set(_e._active_alerts) == {LP_KEY}, f"В3-7: рестарт — тревога из журнала: {list(_e._active_alerts)}")
    asyncio.run(_e._update_open_window(M(29, 9, 0), M(29, 10, 0)))
    check(not _db.journal and set(_e._active_alerts) == {LP_KEY},
          f"В3-7: рестарт внутри обрыва — тревога не закрыта: {_db.journal}")
finally:
    restore()


# ══ В1-4. Ревью v5.1.34 и решение Р1: живое решение, привязка, окно показаний ══
import json as _json  # noqa: E402

# Тексты: «< 1 мин», короткий обрыв без «не видно», счётчики есть — показаний нет,
# убывший счётчик — «сбит», без признака
_ga = M(28, 10, 0)
_fq = SER.outage_counter_facts(_ga, S(_ga, 20), {"START_ATTEMPTS_TOTAL": (_ga, 1558.0), "START_SUCCESS_TOTAL": (_ga, 1550.0)},
                               {"START_ATTEMPTS_TOTAL": (S(_ga, 20), 1559.0), "START_SUCCESS_TOTAL": (S(_ga, 20), 1551.0)})
check(_fq["starts_changed"] is True and _fq["origin_possible"] is False
      and SER.outage_facts_text(_fq, MSK) == "Обрыв связи 28.10 10:00:00–10:00:20 (< 1 мин): по счётчикам 1 пуск "
                                             "(попыток 1558 → 1559, успешных 1550 → 1551)",
      f"В1-4а: обрыв 20 с — «< 1 мин», пуск виден в данных: без «не видно» и без признака: "
      f"{SER.outage_facts_text(_fq, MSK)!r}")
_fn = SER.outage_counter_facts(_ga, S(_ga, 3600), {"ENGINE_HOURS": (_ga, 1000.0)},
                               {"ENGINE_HOURS": (S(_ga, 3600), 4600.0)}, start_roles=True)
check(_fn["starts_changed"] is None and "показаний счётчика пусков до обрыва нет" in SER.outage_facts_text(_fn, MSK),
      f"В1-4б: счётчики пусков у машины есть, показаний нет: {SER.outage_facts_text(_fn, MSK)!r}")
_fz = SER.outage_counter_facts(_ga, S(_ga, 3600), {"START_ATTEMPTS_TOTAL": (_ga, 963.0), "START_SUCCESS_TOTAL": (_ga, 953.0)},
                               {"START_ATTEMPTS_TOTAL": (S(_ga, 3600), 961.0), "START_SUCCESS_TOTAL": (S(_ga, 3600), 953.0)})
check(_fz["starts_changed"] is False and _fz["counter_glitch"] is True
      and "счётчик пусков сбит (попыток 963 → 961, успешных 953) — пуски по нему не оценить" in SER.outage_facts_text(_fz, MSK),
      f"В1-4в: счётчик убыл (кадр застывших часов 24.10) — «сбит», без признака: {SER.outage_facts_text(_fz, MSK)!r}")
_f0 = SER.outage_counter_facts(_ga, S(_ga, 3600), {"START_ATTEMPTS_TOTAL": (_ga, 961.0)},
                               {"START_ATTEMPTS_TOTAL": (S(_ga, 3600), 0.0)})
check(_f0["starts_changed"] is False and _f0["counter_glitch"], "В1-4в: нулевой кадр — не пуск")


def _run_live(name, enum, hist, gaps, cycles, before=None, cursor=CUT26, rs0=0, mem=None, db=None, eng0=None):
    """Циклы открытого окна; World.now — момент цикла (видимость data_gaps)."""
    w = World(hist, gaps, before or {}, enum=enum, faults=[])
    db = db or FakeDB()
    install(w, db)
    try:
        e = eng0 or mk_engine()
        if eng0 is None:
            e.cursor_ts, e.continued_from_id, e._last_open_run_state = cursor, None, rs0
        if mem is not None:
            e._counter_mem = dict(mem)
        snaps = []
        for t in cycles:
            w.now = t
            asyncio.run(e._update_open_window(e.cursor_ts, t))
            oc = (db.open_row or {}).get("characteristics_json") or {}
            snaps.append((t, oc.get("_run_origin"), (db.open_row or {}).get("report_summary_md") or ""))
        return e, db, w, snaps
    finally:
        restore()


_base = [{"addr": 40010, "state_start": M(24, 15, 59, 40), "state_end": None, "value": 2}]
_gs, _ge = M(26, 12, 0), M(26, 12, 3)
try:
    # 1) Стоп → обрыв 3 мин → «Работа» с 40011=3 на 1 с позже конца разрыва (ДЭС №3 29.09):
    #    обрыв — «Работе», у Стопа ни строки, ни признака (ревью п. 1)
    _en1 = _base + [{"addr": 40011, "state_start": M(26, 8, 0), "state_end": S(_ge, 1), "value": 0},
                    {"addr": 40011, "state_start": S(_ge, 1), "state_end": None, "value": 3}]
    _h1 = (pings(M(26, 8, 55), _gs) + pings(_ge, M(26, 12, 20))
           + counters(M(26, 11, 50), 961.0, 951.0, 1000.0) + counters(S(_ge, 2), 962.0, 952.0, 1000.0))
    _e1, _db1, _w1, _sn1 = _run_live("1", _en1, _h1, [{"gap_start": _gs, "gap_end": _ge}], [M(26, 12, 20)],
                                     cursor=M(26, 8, 0))
    _st1 = [c for c in _db1.closed if c.get("run_state") == 0]
    _sc1 = (_st1[-1].get("characteristics_json") or {}) if _st1 else {}
    check(_st1 and not _sc1.get("origin_from") and "Обрыв связи" not in (_st1[-1].get("report_summary_md") or "")
          and (_sn1[-1][1] or {}).get("origin_from") == iso(_ge) and "Обрыв связи 26.10 12:00:00–12:03:00" in _sn1[-1][2]
          and not _w1.lvb,
          f"В1-4г: режим на 1 с позже конца разрыва — обрыв и признак у «Работы», у Стопа — нет: "
          f"Стоп {_sc1.get('origin_from')} / открытая {(_sn1[-1][1] or {}).get('origin_from')}")

    # 2) Стоп → обрыв 3 мин → Стоп, видимый пуск через 5 мин; счётчик пришёл после пуска (ДГУ):
    #    ложного «1 пуск» и «останов в обрыве» нет (ревью п. 2)
    _st = S(_ge, 300)
    _en2 = _base + [{"addr": 40011, "state_start": M(26, 8, 0), "state_end": _st, "value": 0},
                    {"addr": 40011, "state_start": _st, "state_end": S(_st, 20), "value": 1},
                    {"addr": 40011, "state_start": S(_st, 20), "state_end": S(_st, 60), "value": 2},
                    {"addr": 40011, "state_start": S(_st, 60), "state_end": None, "value": 3}]
    _h2 = (pings(M(26, 8, 55), _gs) + pings(_ge, M(26, 12, 20))
           + counters(M(26, 11, 50), 961.0, 951.0, 1000.0) + counters(S(_st, 25), 962.0, 952.0, 1000.0))
    _e2, _db2, _w2, _sn2 = _run_live("2", _en2, _h2, [{"gap_start": _gs, "gap_end": _ge}], [M(26, 12, 20)],
                                     cursor=M(26, 8, 0))
    check(not any((c.get("characteristics_json") or {}).get("origin_from") for c in _db2.closed)
          and not any("1 пуск" in (c.get("report_summary_md") or "") for c in _db2.closed)
          and not _sn2[-1][1] and all(c.get("continued_from") is None or True for c in _db2.closed),
          f"В1-4д: видимый пуск после обрыва — показание после него не «после обрыва»: "
          f"{[(c.get('run_state'), (c.get('characteristics_json') or {}).get('origin_from')) for c in _db2.closed]}")

    # 3) Стоп → обрыв → Стоп, первый кадр счётчиков — нули: не пуск (ревью п. 3)
    _en3 = _base + [{"addr": 40011, "state_start": M(26, 8, 0), "state_end": None, "value": 0}]
    _h3 = (pings(M(26, 8, 55), _gs) + pings(_ge, M(26, 12, 40))
           + counters(M(26, 11, 50), 961.0, 951.0, 1000.0) + counters(S(_ge, 1), 0.0, 0.0, 1000.0)
           + counters(S(_ge, 30), 961.0, 951.0, 1000.0))
    _e3, _db3, _w3, _sn3 = _run_live("3", _en3, _h3, [{"gap_start": _gs, "gap_end": _ge}], [M(26, 12, 40)],
                                     cursor=M(26, 8, 0))
    check(not _sn3[-1][1], f"В1-4е: нулевой кадр после переподключения — без признака: {_sn3[-1][1]}")

    # 4) «Мигание»: два разрыва по 3 мин через 2 мин без показаний счётчика пусков — одна строка
    _en4 = _base + [{"addr": 40011, "state_start": M(26, 8, 0), "state_end": None, "value": 3}]
    _a1, _b1, _a2, _b2 = M(26, 12, 0), M(26, 12, 3), M(26, 12, 5), M(26, 12, 8)
    _h4 = (pings(M(26, 8, 55), _a1) + pings(_b1, _a2) + pings(_b2, M(26, 13, 0))
           + counters(M(26, 11, 50), 961.0, 951.0, 1000.0) + counters(S(_b2, 5), 962.0, 952.0, 1500.0))
    _e4, _db4, _w4, _sn4 = _run_live("4", _en4, _h4, [{"gap_start": _a1, "gap_end": _b1},
                                                      {"gap_start": _a2, "gap_end": _b2}], [M(26, 13, 0)],
                                     cursor=M(26, 8, 0), rs0=3)
    check(_sn4[-1][2].count("Обрыв связи") == 1 and "Обрыв связи 26.10 12:00:00–12:08:00 (8 мин)" in _sn4[-1][2]
          and (_sn4[-1][1] or {}).get("origin_from") == iso(_b2),
          f"В1-4ж: мигание связи — один обрыв 12:00–12:08: "
          f"{[ln for ln in _sn4[-1][2].splitlines() if 'Обрыв' in ln]}")

    # 5) Решение Р1: машина работала, память движка до обрыва; при возврате связи —
    #    признак, «≥» и строка фактов в открытом сегменте сразу, без запросов
    _en5 = _base + [{"addr": 40011, "state_start": M(24, 15, 59, 40), "state_end": None, "value": 3}]
    _gs5, _ge5 = M(26, 12, 0), M(26, 14, 0)
    _h5 = (pings(S(CUT26, -300), _gs5, extra=counters(M(26, 11, 50), 961.0, 951.0, 1000.0))
           + pings(_ge5, M(26, 16, 0), extra=counters(_ge5, 963.0, 953.0, 1000.0 + 6000)))
    _cyc5 = [M(26, 11, 55), M(26, 13, 0), S(_ge5, 30), S(_ge5, 120), S(_ge5, 900)]
    _e5, _db5, _w5, _sn5 = _run_live("5", _en5, _h5, [{"gap_start": _gs5, "gap_end": _ge5}], _cyc5,
                                     cursor=CUT26, rs0=3)
    check(not _sn5[0][1] and not _sn5[1][1] and not _sn5[2][1]
          and (_sn5[3][1] or {}).get("origin_from") == iso(_ge5) and not _w5.lvb,
          f"В1-4з: Р1 — признак сразу после возврата связи (+2 мин, после допуска смены режима), "
          f"без запросов: {[(t.strftime('%H:%M:%S'), bool(r)) for t, r, _ in _sn5]} lvb={_w5.lvb}")
    _op5 = _db5.open_row or {}
    _st5 = SA.build_structural_status(seg_row(_op5), FR3, MSK)
    check(SA.format_status_text(_st5).startswith(f"Работа ≥ 15 мин (последний пуск{NBSP}— в обрыве 26.10)")
          and _op5.get("continued_from") is None
          and "ℹ️ Обрыв связи 26.10 12:00:00–14:00:00 (2 ч 00 мин): по счётчикам 2 пуска" in (_op5.get("report_summary_md") or ""),
          f"В1-4з: статус «≥» от первых данных и строка фактов в открытом сегменте: "
          f"{SA.format_status_text(_st5)!r}")
    # Рестарт после решения: снимок до обрыва — из характеристик открытой строки, решение то же, без запроса
    _och5 = (_op5.get("characteristics_json") or {})
    check(_och5.get("_outage_before") and _och5.get("_outage_carry"),
          f"В1-4и: снимок и разрывы — в открытой строке: {list(_och5)}")
    _e5b = mk_engine()
    _e5b.cursor_ts, _e5b.continued_from_id, _e5b._last_open_run_state = CUT26, None, 3
    _db5.open_row = dict(_op5, t_start=CUT26)
    install(World(_h5, [{"gap_start": _gs5, "gap_end": _ge5}], {}, enum=_en5, faults=[]), _db5)
    try:
        asyncio.run(_e5b._restore_live_state())
    finally:
        restore()
    _hcut = [r for r in _h5 if r["ts"] >= M(26, 11, 50)]     # история окна после рестарта без строк до обрыва
    _e5c, _db5c, _w5c, _sn5c = _run_live("5b", _en5, _hcut, [{"gap_start": _gs5, "gap_end": _ge5}],
                                         [S(_ge5, 960)], db=_db5, eng0=_e5b)
    check((_sn5c[-1][1] or {}).get("origin_from") == iso(_ge5) and not _w5c.lvb,
          f"В1-4и: рестарт после решения — признак тот же, без запроса: {_sn5c[-1][1]} lvb={_w5c.lvb}")
    # Без памяти (рестарт внутри обрыва) — в открытом окне решения нет, на закрытии — один запрос
    _hpost = [r for r in _h5 if r["ts"] >= _ge5]
    _e5d, _db5d, _w5d, _sn5d = _run_live("5c", _en5, _hpost, [{"gap_start": _gs5, "gap_end": _ge5}],
                                         [S(_ge5, 120), S(_ge5, 900)], before=BEFORE | {
                                             ATT: (M(26, 11, 50), 961.0), SUC: (M(26, 11, 50), 951.0),
                                             HRS: (M(26, 11, 50), 1000.0)}, cursor=M(26, 13, 59), rs0=3)
    check(not _sn5d[-1][1] and not _w5d.lvb,
          f"В1-4к: памяти нет (рестарт внутри обрыва) — в открытом окне без признака и запроса: {_sn5d[-1][1]}")
    _w5d2 = World(_hpost, [{"gap_start": _gs5, "gap_end": _ge5}],
                  {ATT: (M(26, 11, 50), 961.0), SUC: (M(26, 11, 50), 951.0), HRS: (M(26, 11, 50), 1000.0)},
                  enum=_en5, faults=[])
    install(_w5d2, _db5d)
    try:
        asyncio.run(_e5d._close_window(M(26, 13, 59), CUT27, "DAILY_BOUNDARY", lookahead_to=CUT27, live_to=CUT27))
    finally:
        restore()
    _c5d = _db5d.closed[-1]
    check(len(_w5d2.lvb) == 1 and (_c5d.get("characteristics_json") or {}).get("origin_from") == iso(_ge5),
          f"В1-4к: запасной путь — на закрытии один запрос и признак: {_w5d2.lvb}")

    # 6) Выключатель KB (ревью п. 8)
    class _Off:
        def __init__(self, c):
            self._c = c

        def seg(self, sec, key, default=None):
            if sec == "no_data_estimate" and key == "outage_facts_enabled":
                return False
            return self._c.seg(sec, key, default=default)

        def __getattr__(self, n):
            return getattr(self._c, n)
    _eo = mk_engine(_Off(CFG3))
    check(asyncio.run(_eo._outage_facts(M(26, 9, 0), M(27, 9, 0), [{"gap_start": G0, "gap_end": G1}],
                                        rows_of([(ATT, G1, 963.0)]), M(26, 8, 55), 3)) == ([], None),
          "В1-4л: outage_facts_enabled: false — без фактов и признака")
finally:
    restore()

# ══ В1-5. Обрыв кончился за минуты до суточного реза, показания счётчиков пусков —
# после 09:00 (у ДГУ — через 5 мин после переподключения): перенос через рез ═══
_ga5, _gb5 = M(27, 8, 20), M(27, 8, 57)
_en55 = [{"addr": 40010, "state_start": M(24, 15, 59, 40), "state_end": None, "value": 2},
         {"addr": 40011, "state_start": M(24, 15, 59, 40), "state_end": None, "value": 3}]
_pre5 = []
_t = S(CUT26, -240)
while _t < _ga5:
    _pre5 += counters(_t, 961.0, 951.0, 5_000_000.0 + (_t - CUT26).total_seconds())
    _t += timedelta(minutes=15)
_post5 = ([{"addr": HRS, "ts": S(_gb5, 60), "value": 5_000_000.0 + 86_400.0}]
          + [r for k in range(0, 12) for r in counters(S(M(27, 9, 3), 900 * k), 963.0, 953.0,
                                                       5_000_000.0 + 86_400.0 + 360 + 900 * k)])
_H55 = pings(S(CUT26, -300), _ga5, extra=_pre5) + pings(_gb5, M(28, 9, 5), extra=_post5)
_G55 = [{"gap_start": _ga5, "gap_end": _gb5}]
_TXT55 = ("Обрыв связи 27.10 08:20:00–08:57:00 (37 мин): по счётчикам 2 пуска (попыток 961 → 963, "
          "успешных 951 → 953)")
try:
    _w55 = World(_H55, _G55, {}, enum=_en55, faults=[])
    _db55 = FakeDB()
    install(_w55, _db55)
    _e55 = mk_engine()
    _e55.cursor_ts, _e55.continued_from_id, _e55._last_open_run_state = CUT26, None, 3
    for _t55 in (M(27, 8, 10), S(_gb5, 90), S(_gb5, 150)):
        _w55.now = _t55
        asyncio.run(_e55._update_open_window(CUT26, _t55))
    _w55.now = S(CUT27, 5)
    asyncio.run(_e55._close_window(CUT26, CUT27, "DAILY_BOUNDARY", lookahead_to=CUT27, live_to=CUT27))
    _cA = _db55.closed[-1]
    _chA = _cA.get("characteristics_json") or {}
    check("Обрыв связи" not in (_cA.get("report_summary_md") or "") and "Обрыв связи" not in (_cA.get("report_md") or "")
          and not _chA.get("origin_from") and (_chA.get("outage_deferred") or [{}])[0].get("gap_end") == iso(_gb5)
          and (_chA.get("outage_deferred") or [{}])[0].get("before")
          and (_e55._outage_carry or {}).get("t0") == iso(CUT27) and not _w55.lvb,
          f"В1-5а: на резе обрыв с показаниями пусков за резом не решается — переносится, строки нет: "
          f"{_chA.get('outage_deferred')} carry={_e55._outage_carry} lvb={_w55.lvb}")
    # Продолжение: решение по показаниям после реза, окно «после» — от конца обрыва
    _sn55 = []
    for _t55 in (S(CUT27, 60), S(CUT27, 270)):
        _w55.now = _t55
        asyncio.run(_e55._update_open_window(CUT27, _t55))
        _oc = (_db55.open_row or {}).get("characteristics_json") or {}
        _sn55.append((_oc.get("_run_origin"), (_db55.open_row or {}).get("report_summary_md") or ""))
    _st55 = SA.build_structural_status(seg_row(_db55.open_row or {}), FR3, MSK)
    check(not _sn55[0][0] and (_sn55[1][0] or {}).get("origin_from") == iso(_gb5) and _TXT55 in _sn55[1][1]
          and SA.format_status_text(_st55).startswith(f"Работа ≥ 7 мин (последний пуск{NBSP}— в обрыве 27.10)")
          and (_db55.open_row or {}).get("continued_from") is None and not _w55.lvb,
          f"В1-5б: продолжение решает сразу, как пришли показания (09:03), без запросов: "
          f"{[bool(x[0]) for x in _sn55]} {SA.format_status_text(_st55)!r} lvb={_w55.lvb}")
    # Закрытие продолжения — строка фактов один раз (у предка её нет), признак, без запроса
    _w55.now = S(M(28, 9, 0), 5)
    asyncio.run(_e55._close_window(CUT27, M(28, 9, 0), "DAILY_BOUNDARY", lookahead_to=M(28, 9, 0),
                                   live_to=M(28, 9, 0)))
    _cB = _db55.closed[-1]
    _chB = _cB.get("characteristics_json") or {}
    _all_lines = sum((c.get("report_summary_md") or "").count("Обрыв связи 27.10 08:20") for c in _db55.closed)
    check(_chB.get("origin_from") == iso(_gb5) and _all_lines == 1 and _cB.get("continued_from") is None
          and (_cB.get("forward_fill_json") or {}).get("_run_state_sec") == {"3": (M(28, 9, 0) - _gb5).total_seconds()}
          and "с начала состояния (с 27.10 08:57, пуск в обрыве)" in (_cB.get("report_md") or "")
          and "(между ними обрыв связи с пусками 27.10 — начало режима не видно)" in (_cB.get("report_md") or "")
          and not _w55.lvb,
          f"В1-5в: закрытие продолжения — одна строка на два сегмента, признак, время от 08:57: "
          f"строк {_all_lines} {_chB.get('origin_from')} {(_cB.get('forward_fill_json') or {}).get('_run_state_sec')}")

    # Рестарт между резом и решением: перенос — из характеристик закрытого предка, без запроса
    _db56 = FakeDB()
    _db56.last_closed = dict(_cA, id=36137, t_end=CUT27, cause_close="DAILY_BOUNDARY")
    _w56 = World(_H55, _G55, {}, enum=_en55, faults=[])
    install(_w56, _db56)
    _e56 = mk_engine()
    asyncio.run(_e56.initialize(CUT27))
    _w56.now = S(CUT27, 270)
    asyncio.run(_e56._update_open_window(CUT27, S(CUT27, 270)))
    _oc56 = (_db56.open_row or {}).get("characteristics_json") or {}
    check((_oc56.get("_run_origin") or {}).get("origin_from") == iso(_gb5)
          and _TXT55 in ((_db56.open_row or {}).get("report_summary_md") or "") and not _w56.lvb,
          f"В1-5г: рестарт между резом и решением — без потери и без запроса: {_oc56.get('_run_origin')} lvb={_w56.lvb}")
    # Показания пусков пришли до реза — решает сам сегмент, переноса нет
    _post5b = [dict(r, ts=S(_gb5, 60)) if r["addr"] in (ATT, SUC) and r["ts"] == M(27, 9, 3) else r for r in _post5]
    _w57 = World(pings(S(CUT26, -300), _ga5, extra=_pre5) + pings(_gb5, M(27, 10, 0), extra=_post5b), _G55, {},
                 enum=_en55, faults=[])
    _db57 = FakeDB()
    install(_w57, _db57)
    _e57 = mk_engine()
    _e57.cursor_ts, _e57.continued_from_id, _e57._last_open_run_state = CUT26, None, 3
    _w57.now = S(CUT27, 5)
    asyncio.run(_e57._close_window(CUT26, CUT27, "DAILY_BOUNDARY", lookahead_to=CUT27, live_to=CUT27))
    _cA7 = _db57.closed[-1]
    check(_TXT55 in (_cA7.get("report_summary_md") or "")
          and (_cA7.get("characteristics_json") or {}).get("origin_from") == iso(_gb5)
          and not (_cA7.get("characteristics_json") or {}).get("outage_deferred")
          and (getattr(_e57, "_outage_carry", None) or {}).get("t0") != iso(CUT27),
          f"В1-5д: показания пусков до реза — решает сегмент, где кончился обрыв, переноса нет: "
          f"{(_cA7.get('characteristics_json') or {}).get('outage_deferred')}")
finally:
    restore()

# ══ В1-6. Перепроверка v5.1.34 ═══════════════════════════════════════════════
# note-б: убыль только наработки — «наработка сбита», признак по росту пусков ставится
_fh = SER.outage_counter_facts(_ga, S(_ga, 3600),
                               {"START_ATTEMPTS_TOTAL": (_ga, 1558.0), "START_SUCCESS_TOTAL": (_ga, 1550.0),
                                "ENGINE_HOURS": (_ga, 5000.0 * 3600)},
                               {"START_ATTEMPTS_TOTAL": (S(_ga, 3600), 1559.0), "START_SUCCESS_TOTAL": (S(_ga, 3600), 1551.0),
                                "ENGINE_HOURS": (S(_ga, 3600), 100.0 * 3600)}, start_roles=True)
check(_fh["starts_changed"] is True and not _fh["counter_glitch"]
      and (f"по счётчикам 1 пуск (попыток 1558 → 1559, успешных 1550 → 1551), наработка сбита "
           f"({L.engine_hours_text(5000)} → 100 ч)") in SER.outage_facts_text(_fh, MSK), f"В1-6а: сбита только наработка: {SER.outage_facts_text(_fh, MSK)!r}")

# п. 1: подсказка предка после рестарта — с причиной закрытия
check(eng._make_seg_hint({"run_state": 0, "cause_close": "RUN_STATE_CHANGE", "characteristics_json": {}}).cause_close
      == "RUN_STATE_CHANGE", "В1-6б: подсказка предка из БД — с cause_close")
_en61 = _base + [{"addr": 40011, "state_start": M(26, 8, 0), "state_end": S(_ge, 1), "value": 0},
                 {"addr": 40011, "state_start": S(_ge, 1), "state_end": None, "value": 3}]
_h61 = (pings(M(26, 8, 55), _gs) + pings(_ge, M(26, 14, 0))
        + counters(M(26, 11, 50), 961.0, 951.0, 1000.0) + counters(S(_ge, 2), 962.0, 952.0, 1000.0))
try:
    _w61 = World(_h61, [{"gap_start": _gs, "gap_end": _ge}], {}, enum=_en61, faults=[])
    _db61 = FakeDB()
    install(_w61, _db61)
    _e61 = mk_engine()
    _e61.cursor_ts, _e61.continued_from_id, _e61._last_open_run_state = CUT26, None, 0
    asyncio.run(_e61._update_open_window(CUT26, M(26, 12, 20)))
    _db61.last_closed = dict(_db61.closed[-1], id=36140)
    _e62 = mk_engine()
    asyncio.run(_e62.initialize(CUT26))
    asyncio.run(_e62._update_open_window(_e62.cursor_ts, M(26, 12, 30)))
    _oc62 = (_db61.open_row or {}).get("characteristics_json") or {}
    _n0 = len(_db61.closed)
    asyncio.run(_e62._close_window(_e62.cursor_ts, CUT27, "DAILY_BOUNDARY", lookahead_to=CUT27, live_to=CUT27))
    _c62b = _db61.closed[-1]
    check((_oc62.get("_run_origin") or {}).get("origin_from") == iso(_ge)
          and (_c62b.get("characteristics_json") or {}).get("origin_from") == iso(_ge)
          and "Обрыв связи 26.10 12:00:00–12:03:00" in (_c62b.get("report_summary_md") or "") and not _w61.lvb,
          f"В1-6в: рестарт после смены режима на 1 с позже конца обрыва — «≥» и факты не теряются: "
          f"{_oc62.get('_run_origin')} / {(_c62b.get('characteristics_json') or {}).get('origin_from')}")
finally:
    restore()

# п. 2: показание пусков после обрыва — максимум окна: сбитый кадр решение не переключает
_MEM62 = {"START_ATTEMPTS_TOTAL": (M(25, 0, 0, 32), 961.0), "START_SUCCESS_TOTAL": (M(25, 0, 1, 44), 951.0),
          "ENGINE_HOURS": (M(25, 0, 8, 11), 56038140.0)}


def _live62(extra, offs):
    w = World(pings(G1, S(G1, 3000), extra=extra), [{"gap_start": G0, "gap_end": G1}], dict(BEFORE))
    db = FakeDB()
    install(w, db)
    out = []
    try:
        e = mk_engine()
        e.cursor_ts, e.continued_from_id, e._last_open_run_state = CUT26, None, 3
        e._counter_mem = dict(_MEM62)
        for off in offs:
            w.now = S(G1, off)
            asyncio.run(e._update_open_window(CUT26, S(G1, off)))
            oc = (db.open_row or {}).get("characteristics_json") or {}
            out.append((bool(oc.get("_run_origin")), "сбит" in ((db.open_row or {}).get("report_summary_md") or "")))
        return out, w.lvb
    finally:
        restore()


_rb, _lb = _live62(counters(S(G1, 1), 963.0, 953.0, 56182500.0) + counters(S(G1, 110), 0.0, 0.0, 56182500.0)
                   + counters(S(G1, 140), 963.0, 953.0, 56182640.0), [95, 125, 155, 300, 1250])
_rc, _lc = _live62(counters(S(G1, 1), 963.0, 953.0, 56182500.0) + counters(S(G1, 1190), 0.0, 0.0, 56183600.0)
                   + counters(S(G1, 1300), 963.0, 953.0, 56183700.0), [95, 600, 1195, 1250, 1400])
check(all(r == (True, False) for r in _rb) and all(r == (True, False) for r in _rc) and not _lb and not _lc,
      f"В1-6г: нулевой кадр внутри и последним в окне «после» — «≥» не мигает, «сбит» нет: {_rb} / {_rc}")

# п. 3: строки после обрыва приняты раньше записи data_gaps — снимок берёт показание до обрыва
_e63 = mk_engine()
_e63._note_counters(counters(M(25, 0, 0, 32), 961.0, 951.0, 56038140.0))
_e63._note_counters(counters(S(G1, 1), 963.0, 953.0, 56182500.0))      # память уже после обрыва
_e63._note_outage_before([{"gap_start": G0, "gap_end": G1}])
_sv63 = (_e63._outage_before.get(G0.isoformat()) or {}).get("vals") or {}
check((_sv63.get("START_ATTEMPTS_TOTAL") or [None, None])[1] == 961.0
      and (_sv63.get("ENGINE_HOURS") or [None, None])[1] == 56038140.0,
      f"В1-6д: снимок до обрыва при памяти, обновлённой строками после него: {_sv63}")

# п. 4: перенос через рез, следующие сутки — заглушка без связи: отложенное несёт дальше
_e64 = mk_engine()
_e64._defer_outage_to(CUT27, [{"gap_start": iso(_ga5), "gap_end": iso(_gb5),
                               "before": {"START_ATTEMPTS_TOTAL": [iso(S(_ga5, -60)), 961.0]},
                               "after": {"ENGINE_HOURS": [iso(S(_gb5, 60)), 1000.0]}}])
_d64 = _e64._deferred_from_carry(CUT27)
_e64._defer_outage_to(M(28, 9, 0), _d64)
check(_d64 and _d64[0].get("before") and _d64[0].get("after")
      and [g.get("forced") for g in _e64._outage_carry_for(M(28, 9, 0))] == [True],
      f"В1-6е: заглушка после переноса — отложенное со снимками до и после — следующим суткам: {_d64}")
_cw = inspect.getsource(eng.OnlinePollEngine._close_window)
_uw = inspect.getsource(eng.OnlinePollEngine._update_open_window)
check("self._deferred_from_carry(seg.t_start)" in _cw and "self._deferred_from_carry(seg.t_start)" in _uw,
      "В1-6е: заглушка на суточном пути и при смене режима несёт отложенное дальше")
# note-а: показание наработки «после» до реза — в переносе
check('"after": {r: [v[0].isoformat(), v[1]] for r, v in after.items() if v}' in inspect.getsource(
    eng.OnlinePollEngine._outage_facts), "В1-6ж: перенос хранит показания после обрыва до реза")

# Ревью п. 9: «связь вперёд» не возвращает предка голове с пуском в обрыве
import online.db as _odbm  # noqa: E402
check("characteristics_json->'run_origin_unknown' = 'true'::jsonb" in inspect.getsource(_odbm.insert_closed_segment),
      "В1-4м: insert_closed_segment не связывает голову с пуском в обрыве с предком")
# Ревью п. 11: «Предыдущее состояние» у головы с пуском в обрыве (#36137 после заглушки)
check(any("[суточный рез] (между ними обрыв связи с пусками 25–26.10 — начало режима не видно)" in x
          for x in _rmd.splitlines()),
      f"В1-4н: «Предыдущее состояние» с пометкой об обрыве с пусками: "
      f"{[x for x in _rmd.splitlines() if 'Предыдущее' in x]}")
check(not any("между ними обрыв" in x for x in (_c62.get("report_md") or "").splitlines()),
      "В1-4н: у продолжения цепочки (#36162) пометки нет — обрыв у предка")

# Ревью В3: коды тревоги — в журнал и обратно при рестарте, хеш состава сходится
_LP2 = dict(LP, ref_codes=[144], fault_codes=[])
_act, _ev = eng._diff_alerts({}, [_LP2], ts=M(29, 3, 51, 41), segment_id=None)
check(_ev and (_ev[0].get("alert") or {}).get("ref_codes") == [144], f"В3-8: коды тревоги в событии журнала: {_ev}")
_vj = _odbm._journal_values(_ev[0])


class _Conn:
    async def fetch(self, *a, **k):
        return [{"scenario": "LIMIT_PROXIMITY", "event_type": "OPENED", "ts": M(29, 3, 51, 41), "severity": "CAUTION",
                 "trigger_text": LP["trigger"], "values_json": _json.dumps(_vj, ensure_ascii=False, default=str),
                 "segment_id": None}]

    async def close(self):
        return None


async def _cn():
    return _Conn()
_save_cn = _odbm._connect
_odbm._connect = _cn
try:
    _rest = asyncio.run(_odbm.get_active_alerts("1126246373", "pcc", 1))
finally:
    _odbm._connect = _save_cn
check(_rest and _rest[0].get("ref_codes") == [144] and "_alert" not in _rest[0]["values"]
      and SA.compute_analytics_hash(_rest) == SA.compute_analytics_hash([_LP2]),
      f"В3-8: тревога после рестарта — с кодами, хеш состава тот же: {_rest}")

# Ревью В2, п. 4: /api/online/status — аварийный стоп «авария», как в /api/machines
_rt_src = (ROOT / "web" / "routes.py").read_text(encoding="utf-8")   # fastapi в тестах нет
check('_ch_os.get("stop_kind") == "EMERGENCY"' in _rt_src,
      "В2-11: /api/online/status — аварийный стоп переопределяет уровень")


if _errors:
    print(f"FAIL — {len(_errors)} расхождений:")
    for x in _errors:
        print(f"  • {x}")
    sys.exit(1)
print("Все проверки пройдены.")
