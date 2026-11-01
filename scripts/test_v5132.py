# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""Проверки v5.1.32 «Хвосты проверки v5.1.25» (без БД и сети; проект
docs/plans/design_v5132_tails.md, живая проверка v5.1.25 30.10, defects[5–9, 11, 13]).

- 1. Тексты (defects[11]): НОРМА без замечаний — «### К сведению» и ℹ️ (#36117:
  сбой датчика оборотов шёл с ❗ под «Замечания»), при замечаниях — как раньше;
  подпись пояса «МСК», без пояса — «UTC»; заглушка — короткие метки и имя машины;
  уровень риска нагара по-русски, без нулевых слагаемых; имя машины из реестра в
  заголовке (ДЭС №3 — не «ДГУ») и без имени — только номер; менеджер ставит имя
  из get_equipment при сборке движка, движок передаёт его во все отчёты.
- 2. Пуск (defects[7], [8]): разгон 1406 / 1428 первым отсчётом — не просадка,
  раскачка — от выхода на порог; провал после выхода на порог (26.10 1548 → 1466,
  16.10 1696 → 1247) остаётся; порог за окно пуска не набран — просадка от первого
  отсчёта (по концу окна; пока окно идёт — ждём); ключ KB выключен — как до
  v5.1.32; выбег 1251 перед «Охлаждением» отрезан по частоте кадра смены режима
  (± 2 с), перед Стопом и при рабочей частоте кадра — нет; строка «Пуск».
- 3. Хвост без связи (defects[9]): разрыв до конца сегмента — tail в проверке
  покрытия, строка ❗, заголовок «за N работы (ещё … без связи до 09:00)», строка
  полного отчёта; короткий (< 90 с), внутренний и пустой хвостовой подсегмент.
- 4. Свежесть (defects[5], [6]): по прочитанному (data_read_to) — пауза 50 с не
  «устарели», разрыв 191 с — «устарели» и перерыв по дыре «3 мин 11 с: 18:07:28 →
  18:10:39»; после старта — одно INFO без WARNING и без перерыва; движок не читает
  > 180 с — «аналитика не обновляется»; дыра в строках движка; data_stale API.
- 5. Очередь пересказа (defects[13]): пустое заключение (#35030) не отбирается.
- 6. Ревью v5.1.32: предел разгона runup_max_sec (медленный 1410→1486 за 190 с —
  просадка и «неустойчивый», как v5.1.31; на живом краю ждём не дольше предела),
  пуск за секунды до реза (разгон «ещё идёт» на резе, выход на порог — после
  реза), окно, закрытое автоматом до порога, — просадка; частота первого отсчёта
  — из его кадра; подписи порога (по частоте, «ещё не набрал», «до замыкания
  автомата», «в первые 30 с»); «Короткая работа» без хвоста; несколько хвостов —
  «на концах сегментов»; экранирование имени; W7 по границе чтения; лаг тика — от
  live_ok_at; срок «ещё не читали»; дыры короче порога не в перерыве; даты в
  перерыве через сутки; возраст данных в «устарели»; дыра закрытого окна;
  заключение из пробелов; имя у живого движка из настроек.

Запуск:  py -3 scripts/test_v5132.py
"""
from __future__ import annotations

import asyncio
import copy
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
from analytics import segmenter as SG  # noqa: E402
from analytics import serializer as SER  # noqa: E402
from analytics.config import AnalyticsConfig  # noqa: E402
from analytics.contract import DerivedMetrics, RiskAccumulators, Segment, Subsegment  # noqa: E402
from analytics.segmenter import segment  # noqa: E402

_errors: list[str] = []
MSK = ZoneInfo("Europe/Moscow")
U = timezone.utc


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
CFG = AnalyticsConfig.from_pair(KB, "pcc3300", "cummins_kta50")     # ДГУ №1 / №2
RPM, FREQ, HB = 40068, 40044, 40290


def M(d, h, m, s=0):
    """Время МСК → UTC."""
    return datetime(2026, 10, d, h, m, s, tzinfo=MSK).astimezone(U)


def S(t, sec):
    return t + timedelta(seconds=sec)


def h(addr, ts, v):
    return {"addr": addr, "ts": ts, "value": v}


def rs(v, a, b=None):
    return {"addr": 40011, "value": v, "state_start": a, "state_end": b}


def iso(t):
    return t.isoformat()


def pings(a, b, step=10):
    out, t, k = [], a, 0
    while t < b:
        out.append(h(HB, t, float(k)))
        t += timedelta(seconds=step)
        k += 1
    return out


def cfg_det(base, scen, **kw):
    c = copy.copy(base)
    c.detectors = dict(base.detectors, **{scen: dict(base.detectors.get(scen) or {}, **kw)})
    return c


def sub(t0, t1, dq=1.0, gaps=(), transient=False, idx=1, ra=None):
    return Subsegment(id=f"s.{idx}", parent_segment_id="s", t_start=iso(t0), t_end=iso(t1),
                      duration_sec=(t1 - t0).total_seconds(), load_zone="NORMAL", cause_open="SEG_START",
                      cause_close=None, characteristics={}, derived_metrics=DerivedMetrics(),
                      risk_accumulators=ra or RiskAccumulators(), detections=[], data_quality=dq,
                      data_gaps=[{"start": iso(a), "end": iso(b), "duration_sec": (b - a).total_seconds()}
                                 for a, b in gaps],
                      transient=transient)


def seg_of(subs, checks=(), rs_=3, cause_open="REPORT_START", cause_close="DAILY_BOUNDARY"):
    t0, t1 = subs[0].t_start, subs[-1].t_end
    return Segment(id="s", router_sn="R", equip_type="pcc", panel_id=1, engine_sn="", run_state=rs_,
                   run_state_label="Работа", t_start=t0, t_end=t1,
                   duration_sec=(datetime.fromisoformat(t1) - datetime.fromisoformat(t0)).total_seconds(),
                   engine_hours_start=None, cause_open=cause_open, cause_close=cause_close,
                   preamble_included=False, data_quality=1.0, sequence_checks=list(checks), events=[],
                   subsegments=list(subs))


# ══ 1. Тексты (defects[11]) ══════════════════════════════════════════════════

# ── 1а. НОРМА без замечаний — «К сведению» и ℹ️ (#36117) ──
_glitch = {"check": "source_rpm_glitch", "passed": False,
           "details": "отброшены отсчёты оборотов, не подтверждённые частотой генератора (сбой датчика), всего 5"}
_W0, _W1 = M(25, 9, 0), M(26, 9, 0)
_sN = seg_of([sub(_W0, _W1)], [_glitch])
_pN = SER.build_summary_parts([_sN], [], tz=MSK, trip_roles=[])
_mdN = SER.render_summary_md(_pN)
check(_pN["verdict"]["level"] == "НОРМА" and "Замечаний к работе нет." in _mdN
      and "### К сведению\n- ℹ️ отброшены отсчёты оборотов" in _mdN and "### Замечания" not in _mdN
      and "❗" not in _mdN, f"1а: НОРМА со сбоем датчика — «К сведению» и ℹ️:\n{_mdN}")
_eps = [{"id": 1, "scenario": "RPM_UNDERSPEED", "source": "analytics", "severity": "CAUTION",
         "t_open": iso(M(25, 12, 0)), "t_close": iso(M(25, 12, 0, 10)), "active_sec": 10.0, "blind_sec": 0.0,
         "gate_suppressed": False, "open_values_json": {}}]
# Живой панельный CAUTION («Общая авария» + 40012) — замечание к работе (решение 11.10, 1а)
_epP = [{"source": "panel", "severity": "CAUTION", "gate_suppressed": False, "scenario": "CONTROLLER_FAULT",
         "addr": 40408, "bit": 11, "t_open": iso(M(25, 10, 0)), "t_close": None, "active_sec": 3600.0,
         "blind_sec": 0.0, "open_values_json": {"fault_name": "Общая авария"}}]
_pC = SER.build_summary_parts([_sN], _epP, tz=MSK, trip_roles=[], t_from=_W0, t_to=_W1)
_mdC = SER.render_summary_md(_pC)
check(_pC["verdict"]["level"] == "CAUTION" and "### Замечания" in _mdC and "- ❗ отброшены отсчёты оборотов" in _mdC
      and "К сведению" not in _mdC, f"1а: при замечаниях — «Замечания» и ❗, как раньше:\n{_mdC}")
_pU = SER.build_summary_parts([_sN], _eps, tz=MSK, trip_roles=[], t_from=_W0, t_to=_W1)
_mdU = SER.render_summary_md(_pU)
check(_pU["verdict"]["level"] == "НОРМА" and "### Замечания" in _mdU and "- ❗ отброшены отсчёты оборотов" in _mdU,
      f"1а: НОРМА с неподтверждённым эпизодом — замечание в блоке, «Замечания», как раньше:\n{_mdU}")
_pS = SER.build_summary_parts([_sN], [dict(_eps[0], gate_suppressed=True)], tz=MSK, trip_roles=[],
                              t_from=_W0, t_to=_W1)
_mdS = SER.render_summary_md(_pS)
check(_pS["verdict"]["level"] == "НОРМА" and "### Замечания" in _mdS and "К сведению" not in _mdS,
      f"1а: НОРМА со снятым замечанием — «Замечания», как раньше:\n{_mdS}")

# ── 1б. Подпись пояса ──
check(SER._fmt_ts("2026-10-29T06:00:00+00:00", MSK) == "2026-10-29 09:00:00 МСК",
      f"1б: МСК: {SER._fmt_ts('2026-10-29T06:00:00+00:00', MSK)}")
check(SER._fmt_ts("2026-10-29T06:00:00+00:00", None) == "2026-10-29 06:00:00 UTC",
      f"1б: без пояса — UTC (время в UTC): {SER._fmt_ts('2026-10-29T06:00:00+00:00', None)}")
check(SER._fmt_ts("2026-10-29T06:00:00+00:00", ZoneInfo("Asia/Yakutsk"))
      == "2026-10-29 15:00:00 UTC+9", "1б: другой пояс — «UTC+N»")

# ── 1в. Заглушка: короткие метки, «МСК», имя машины (#36125) ──
_rm, _sm = SER.build_no_data_report("1126246373", "pcc", 1, M(25, 9, 0), M(26, 9, 0), tz=MSK,
                                    last_data_ts=M(25, 0, 11, 3), equip_name="ДЭС №3")
check(_sm == ("## ⚠ Нет связи\n\nСвязь с оборудованием отсутствовала весь период (25.10 09:00 — 26.10 09:00 МСК). "
              "Последние данные до обрыва: 25.10 00:11:03 МСК.\n"), f"1в: сводка заглушки:\n{_sm}")
check(_rm.startswith("# Аналитический отчёт — ДЭС №3 (`1126246373` / панель 1)\n")
      and "**Период анализа:** 2026-10-25 09:00:00 МСК — 2026-10-26 09:00:00 МСК" in _rm
      and "Последние данные до обрыва: 2026-10-25 00:11:03 МСК." in _rm and "Europe/" not in _rm + _sm
      and "ДГУ" not in _rm, f"1в: отчёт заглушки:\n{_rm}")
_rm0, _sm0 = SER.build_no_data_report("SN", "pcc", 2, M(24, 9, 0), M(24, 12, 38, 59), tz=MSK)
check(_rm0.startswith("# Аналитический отчёт — `SN` / панель 2\n")
      and "(24.10 09:00 — 24.10 12:38:59 МСК). Данных за период нет." in _sm0,
      f"1в: без имени — только номер; конец не на минуте — с секундами:\n{_rm0}\n{_sm0}")

# ── 1г. Риск нагара: уровень по-русски, без нулевых слагаемых ──
def _risk_md(level, low=9240.0, idle=0.0, cold=0.0, tlevel="GREEN", el=0.0):
    ra = RiskAccumulators()
    ra.coking_risk.risk_level, ra.coking_risk.low_load_zone_sec = level, low
    ra.coking_risk.idle_low_rpm_sec, ra.coking_risk.coolant_below_60_sec = idle, cold
    ra.thermal_risk.risk_level, ra.thermal_risk.elevated_zone_sec = tlevel, el
    s = seg_of([sub(_W0, S(_W0, 1800), idx=1), sub(S(_W0, 1800), S(_W0, 3600), idx=2, ra=ra)])
    return SER.to_markdown([s], "1126246373", "pcc", 1, _W0, S(_W0, 3600), "x", tz=MSK, equip_name="ДЭС №3")


_r1 = _risk_md("YELLOW")
check("| Риск нагара | 🟡 повышенный | накоплено с прожига: малая нагрузка 2 ч 34 мин |" in _r1
      and "YELLOW" not in _r1 and "холостой ход 0 с" not in _r1 and "ОЖ<60" not in _r1,
      f"1г: «🟡 повышенный», нулевые слагаемые не печатаются: "
      f"{[x for x in _r1.splitlines() if 'Риск нагара' in x]}")
_r2 = _risk_md("RED", low=0.0, idle=600.0, cold=75.0, tlevel="YELLOW", el=120.0)
check("| Риск нагара | 🔴 высокий | накоплено с прожига: холостой ход 10 мин, холодная ОЖ (< 60 °C) 1 мин 15 с |"
      in _r2 and "| Тепловой | 🟡 повышенный | повышенная нагрузка: 2 мин |" in _r2 and "ELEVATED" not in _r2,
      f"1г: «🔴 высокий», тепловой по-русски: {[x for x in _r2.splitlines() if x.startswith('| Риск') or x.startswith('| Тепл')]}")
_r3 = _risk_md("YELLOW", low=0.0)
check("| Риск нагара | 🟡 повышенный | — |" in _r3, "1г: все слагаемые нулевые — «—»")

# ── 1д. Имя машины в заголовке; фолбэк ──
check(SER.report_title("1126246373", 1, "ДЭС №3") == "# Аналитический отчёт — ДЭС №3 (`1126246373` / панель 1)"
      and SER.report_title("6003790403", 1, "ДГУ №1 инв. 2569")
      == "# Аналитический отчёт — ДГУ №1 инв. 2569 (`6003790403` / панель 1)"
      and SER.report_title("1126246373", 1, None) == "# Аналитический отчёт — `1126246373` / панель 1"
      and SER.report_title("1126246373", 1, "  ") == "# Аналитический отчёт — `1126246373` / панель 1",
      "1д: заголовок — имя из реестра, без имени — только номер и панель")
check(_r1.startswith("# Аналитический отчёт — ДЭС №3 (`1126246373` / панель 1)\n")
      and SER.to_markdown([seg_of([sub(_W0, S(_W0, 60))])], "SN", "pcc", 1, _W0, S(_W0, 60), "x", tz=MSK)
      .startswith("# Аналитический отчёт — `SN` / панель 1\n"), "1д: to_markdown с именем и без")
# Метки разбора текста отчёта не задеты: сжатие входа ищет «**Подсегментов:**» и «#### Подсегмент»
from corpus import compact as _cmp  # noqa: E402
check(_cmp._MARK == "**Подсегментов:**" and "Аналитический отчёт" not in inspect.getsource(_cmp),
      "1д: corpus/compact.py не разбирает заголовок отчёта")

# ── 1е. Имя из реестра: менеджер при сборке движка, движок — во все отчёты ──
import online.manager as _mgr  # noqa: E402
from analytics import binding as _bnd  # noqa: E402
from db import analytics as _dba  # noqa: E402


async def _build(name):
    async def _gb(*a, **k):
        return {"controller_id": "pcc3300", "engine_id": "cummins_qsx15", "kb_path": None}

    async def _ge(*a, **k):
        return {"name": name, "engine_sn": "41377550"}

    async def _gs(key, default=None):
        return default
    real = (_dba.get_equipment_binding, _dba.get_equipment, _dba.get_app_setting)
    _dba.get_equipment_binding, _dba.get_equipment, _dba.get_app_setting = _gb, _ge, _gs
    try:
        m = _mgr.OnlineManager()
        return await m._build_engine({"router_sn": "1126246373", "equip_type": "pcc", "panel_id": 1,
                                      "poll_interval_sec": 30})
    finally:
        _dba.get_equipment_binding, _dba.get_equipment, _dba.get_app_setting = real


_e1 = asyncio.run(_build("ДЭС №3"))
_e0 = asyncio.run(_build(None))
check(_e1 is not None and _e1.equip_name == "ДЭС №3" and _e1.engine_sn == "41377550" and _e0.equip_name is None,
      f"1е: имя машины из get_equipment — в движке: {getattr(_e1, 'equip_name', '?')!r}, без имени — None")
_src_eng = (ROOT / "online" / "engine.py").read_text(encoding="utf-8")
check(_src_eng.count('equip_name=getattr(self, "equip_name", None)') == 5,
      "1е: движок передаёт имя во все отчёты (суточный рез, заглушки двух путей, смена режима, открытое окно)")
check("equip_name=eq.get(\"name\")" in (ROOT / "scheduler.py").read_text(encoding="utf-8")
      and "equip_name=eq.get(\"name\")" in (ROOT / "web" / "routes.py").read_text(encoding="utf-8")
      and "equip_name=equip_name" in (ROOT / "analytics" / "runner.py").read_text(encoding="utf-8"),
      "1е: пакетный прогон и ручной отчёт — имя из той же строки реестра")


# ══ 2. Пуск (defects[7], [8]) ════════════════════════════════════════════════

def start_case(t0, pts, rs_seq, cfg=CFG, a=-300, b=600, live_edge=False, tail=None, pre_rpm=0.0):
    """pts — [(с от t0, обороты, Гц)] (None — строки нет); rs_seq — [(с от t0, 40011)];
    tail — (с, обороты, Гц): дальше каждые 10 с до конца окна."""
    enum = [rs(v, S(t0, s), S(t0, rs_seq[i + 1][0]) if i + 1 < len(rs_seq) else None)
            for i, (s, v) in enumerate(rs_seq)]
    rows = pings(S(t0, a - 300), S(t0, b))
    rows += [h(RPM, S(t0, -40), pre_rpm), h(FREQ, S(t0, -200), 0.0)]
    have = set()
    for s, r, fq in pts:
        for ad, v in ((RPM, r), (FREQ, fq)):
            if v is not None:
                rows.append(h(ad, S(t0, s), v))
                have.add((ad, s))
    if tail is not None:
        s, r, fq = tail
        while s < b:
            for ad, v in ((RPM, r), (FREQ, fq)):
                if (ad, s) not in have:
                    rows.append(h(ad, S(t0, s), v))
            s += 10
    rows.sort(key=lambda r: r["ts"])
    return segment(enum, rows, [], [], cfg, "SN", "pcc", 2, "E", S(t0, a), S(t0, b), live_edge=live_edge)


def under(segs):
    out = []
    for s in segs:
        for x in s.subsegments:
            if x.transient:
                continue
            for d in x.detections:
                if d.scenario == "RPM_UNDERSPEED":
                    for c in ((d.values or {}).get("live") or {}).get("clusters") or []:
                        out.append((c["start"], c.get("vmin")))
    return sorted(set(out))


def work(segs):
    return next(s for s in segs if s.run_state == 3)


# ── 2а. ДГУ №2 27.10 12:02:48 (#36146): 1406 / 46,6 Гц первым отсчётом, выбег 1251 ──
T1 = M(27, 12, 2, 48)
_P1 = [(0, 1406.0, 46.6), (10, 1498.0, None), (11, None, 50.0), (20, 1500.0, None), (50, 1499.0, None),
       (60, 1500.0, None), (70, 1251.0, None), (71, None, 41.6), (80, 891.0, None), (81, None, 0.0),
       (92, 901.0, None), (100, 897.0, None)]
_RS1 = [(-600, 0), (0, 3), (71, 5)]
_s1 = start_case(T1, _P1, _RS1, b=150)
_w1 = work(_s1)
_sp1 = _w1.start_profile or {}
check(under(_s1) == [], f"2а: разгон 1406 первым отсчётом — не просадка (было 15041): {under(_s1)}")
check(_sp1.get("runup_to") == iso(S(T1, 10)) and _sp1.get("first_rpm") == 1406.0 and _sp1.get("first_freq") == 46.6
      and _sp1.get("swing_sec") == 0.0 and _sp1.get("rated_first") is True,
      f"2а: выход на порог 12:02:58, раскачки нет (было 11 с): {_sp1}")
check(_sp1.get("rpm") == [1406.0, 1500.0] and _sp1.get("end_by") == "rundown" and _sp1.get("t_to") == iso(S(T1, 70)),
      f"2б: выбег 1251 отрезан по частоте 41,6 кадра смены режима: {_sp1.get('rpm')} {_sp1.get('end_by')} "
      f"{_sp1.get('t_to')}")
_km1 = SER._key_metrics_part([_w1], ["RPM", "FREQUENCY"], tz=MSK)
_st1 = SER._km_short_text(_km1["short"]) if _km1 and _km1.get("short") else None
check(_st1 == "Короткая работа (1 мин 10 с, 5 отсчётов): обороты 1406 → 1500 об/мин, частота 46,6 → 50 Гц"
      and next(r for r in _km1["rows"] if r["role"] == "RPM")["min"] == 1406.0,
      f"2б: «Короткая работа» без выбега (было 1406 → 1251, 6 отсчётов): {_st1}")
_t1 = L.start_profile_text(_sp1, MSK) or ""
check(_t1.startswith("на рабочих оборотах с первого отсчёта (1406 об/мин, 46,6 Гц; разгон между отсчётами, "
                     "не дольше 40 с), 1485 об/мин и выше — с 12:02:58; обороты 1406–1500 об/мин"),
      f"2в: строка «Пуск» — не «на номинале»: {_t1}")
check("раскачка" not in _t1, "2в: в строке «Пуск» нет раскачки от отсчёта разгона")

# ── 2г. Второй пуск 12:06:48 (#36152): 1428 / 47,8 → 1498 ──
T2 = M(27, 12, 6, 48)
_s2 = start_case(T2, [(0, 1428.0, 47.8), (11, 1498.0, 50.0)], [(-200, 0), (0, 3)], b=200, tail=(20, 1500.0, 50.0))
check(under(_s2) == [], f"2г: разгон 1428 — не просадка (было 15042, снимал LLM-гейт): {under(_s2)}")

# ── 2д. ДГУ №1 26.10 11:11:33: 1548 / 51,5 → 1466 / 48,7 — провал после порога остаётся ──
T3 = M(26, 11, 11, 33)
_s3 = start_case(T3, [(9, 1548.0, 51.5), (19, 1466.0, 48.7), (29, 1444.0, 48.5), (39, 1475.0, 49.0),
                      (49, 1535.0, 51.2), (59, 1500.0, 50.0)], [(-600, 0), (0, 3)], b=300,
                 tail=(69, 1500.0, 50.0))
check(under(_s3) and under(_s3)[0][0] == iso(S(T3, 19)), f"2д: провал 1466 после 1548 — остаётся: {under(_s3)}")
check((work(_s3).start_profile or {}).get("runup_to") == iso(S(T3, 9)), "2д: выход на порог — первый же 1548")

# ── 2е. ДГУ №1 16.10 09:25:52: заброс 1696, затем 1247 и Стоп — провал остаётся, выбег не режется ──
T4 = M(16, 9, 25, 52)
_s4 = start_case(T4, [(11, 1696.0, 56.8), (20, 1247.0, 41.5), (31, 76.0, 0.0)], [(-600, 0), (0, 3), (31, 0)],
                 b=120)
check(any(t == iso(S(T4, 20)) for t, _ in under(_s4)), f"2е: провал 1247 после заброса — остаётся: {under(_s4)}")

# ── 2ж. Порог за окно пуска не набран — просадка от первого отсчёта ──
T5 = M(27, 14, 0, 0)
_slow = [(s, 1420.0 + (s // 10) % 3, 47.4) for s in range(0, 400, 10)]
_s5 = start_case(T5, _slow, [(-600, 0), (0, 3)], b=420)
_u5 = under(_s5)
check(_u5 and _u5[0][0] == iso(T5) and (work(_s5).start_profile or {}).get("runup_to") is None,
      f"2ж: 1420–1422 об/мин без выхода на порог за окно пуска — просадка с первого отсчёта: {_u5}")
# Живой край: ждём порога не дольше runup_max_sec (30 с) от рабочих оборотов (ревью А1)
_s5o = start_case(T5, [p for p in _slow if p[0] < 25], [(-600, 0), (0, 3)], b=25, live_edge=True)
_sp5o = work(_s5o).start_profile or {}
check(_sp5o.get("open") is True and _sp5o.get("runup_pending") is True and under(_s5o) == []
      and "1485 об/мин ещё не набрал" in (L.start_profile_text(_sp5o, MSK) or ""),
      f"2ж: окно пуска идёт, предел не вышел — порога ждём, просадку не заводим: {under(_s5o)}")
_s5p = start_case(T5, [p for p in _slow if p[0] < 60], [(-600, 0), (0, 3)], b=60, live_edge=True)
_sp5p = work(_s5p).start_profile or {}
check(_sp5p.get("runup_pending") is False and under(_s5p) and under(_s5p)[0][0] == iso(T5),
      f"2ж: предел 30 с вышел, порога нет — просадка с первого отсчёта и на живом краю: {under(_s5p)}")

# ── 2з. Ключ KB выключен — как до v5.1.32 ──
_CFG_OFF = cfg_det(CFG, "RPM_UNDERSPEED", start_runup_exclude=False)
_s1off = start_case(T1, _P1, _RS1, b=150, cfg=_CFG_OFF)
_sp1off = work(_s1off).start_profile or {}
check(under(_s1off) == [(iso(T1), 1406.0)] and "runup_to" not in _sp1off and _sp1off.get("swing_sec") == 11.0
      and (L.start_profile_text(_sp1off, MSK) or "").startswith("на номинале с первого отсчёта"),
      f"2з: start_runup_exclude: false — просадка 1406, раскачка 11 с, «на номинале»: {under(_s1off)} {_sp1off}")
check(SG._runup_exclude(CFG) is True and SG._runup_exclude(_CFG_OFF) is False, "2з: ключ читается из KB")
_mono = AnalyticsConfig(KB / "equipment" / "cummins_kta50_pcc3300")
_qsx = AnalyticsConfig.from_pair(KB, "pcc3300", "cummins_qsx15")
check(_mono.det("RPM_UNDERSPEED", "start_runup_exclude") is True
      and _qsx.det("RPM_UNDERSPEED", "start_runup_exclude") is True,
      "2з: ключ в обоих слоях KB (_defaults и legacy-монолит)")

# ── 2и. Кадр смены режима: частота кадра рабочая — не режем; без частоты в кадре — не режем ──
_P1b = [p if p[0] != 71 else (71, None, 50.0) for p in _P1]
_spb = work(start_case(T1, _P1b, _RS1, b=150)).start_profile or {}
check(_spb.get("end_by") == "state_end" and _spb.get("rpm") == [1251.0, 1500.0],
      f"2и: частота кадра смены 50 Гц — сброса нет, как раньше: {_spb.get('end_by')} {_spb.get('rpm')}")
_P1c = [p for p in _P1 if p[0] != 71]
_spc = work(start_case(T1, _P1c, _RS1, b=150)).start_profile or {}
check(_spc.get("end_by") == "state_end", f"2и: отсчёта частоты в кадре нет — как раньше: {_spc.get('end_by')}")
_P1d = [p if p[0] != 71 else (74, None, 41.6) for p in _P1]
_spd = work(start_case(T1, _P1d, _RS1, b=150)).start_profile or {}
check(_spd.get("end_by") == "state_end", f"2и: частота дальше кадра (3 с) — не кадр смены: {_spd.get('end_by')}")


# ══ 3. Хвост без связи (defects[9]) ══════════════════════════════════════════

C0, C1 = M(28, 9, 0), M(29, 9, 0)
G0 = M(29, 6, 2, 8)
_subs = [sub(C0, M(29, 3, 44, 17), idx=1), sub(M(29, 3, 44, 17), C1, dq=0.437, gaps=[(G0, C1)], idx=2)]
_tail = SG._tail_gap(_subs, CFG)
check(_tail == {"start": iso(G0), "end": iso(C1), "sec": 10672.0, "work_sec": 10672.0},
      f"3а: хвост 06:02:08–09:00 (#36178): {_tail}")
_chk = [c for c in SG._sequence_checks(3, _subs, [], CFG) if c["check"] == "subseg_data_coverage"][0]
check(not _chk["passed"] and _chk["ranges"] == [{"start": iso(G0), "end": iso(C1)}] and _chk["tail"] == _tail
      and _chk["details"] == "отсутствие связи/данных: 03:02:08–06:00:00",
      f"3а: проверка покрытия — окно хвоста (было passed, строки ❗ нет): {_chk}")
_s36178 = seg_of(_subs, [_chk])
_s36178.subsegments[0].characteristics = {"RPM": {"median": 1500.0, "min": 1488.0, "max": 1508.0, "unit": "rpm",
                                                  "value_start": 1500.0, "value_end": 1500.0, "n": 8000}}
_eps2 = [{"id": 2, "scenario": "LIMIT_PROXIMITY", "source": "analytics", "severity": "CAUTION",
          "t_open": iso(M(28, 12, 0)), "t_close": iso(M(28, 13, 0)), "active_sec": 3600.0, "blind_sec": 0.0,
          "gate_suppressed": False, "open_values_json": {}}]
_p3 = SER.build_summary_parts([_s36178], _eps2, tz=MSK, trip_roles=["RPM"], t_from=C0, t_to=C1)
_md3 = SER.render_summary_md(_p3)
check("- ❗ отсутствие связи/данных: 29.10 06:02:08–09:00:00" in _md3
      and "### Ключевые показатели за 21 ч 02 мин работы (ещё 2 ч 57 мин без связи до 09:00)" in _md3,
      f"3б: строка ❗ и заголовок «(ещё … без связи до 09:00)» (было «за 24 ч 00 мин работы»):\n{_md3}")
_km3 = _p3["key_metrics"]
check(_km3["work_raw_sec"] == 86400.0 and _km3["tail_blind_sec"] == 10672.0 and _km3["tail_end"] == iso(C1)
      and _km3["tail_end_text"] == "09:00", f"3б: поля key_metrics (work_raw_sec не меняется): {_km3}")
_rep3 = SER.to_markdown([_s36178], "1126246373", "pcc", 1, C0, C1, "x", tz=MSK, equip_name="ДЭС №3")
check("| Время под нагрузкой (RUN_STATE=3) за период | 24 ч 00 мин (из них 2 ч 57 мин в конце без связи) |"
      in _rep3, "3в: строка полного отчёта («в конце» — и у открытого сегмента, ревью)")
# Короткий хвост (< 90 с) — нет; внутренний разрыв — нет; пустой хвостовой подсегмент — одно окно с начала разрыва
_short = [sub(C0, M(29, 8, 58), idx=1), sub(M(29, 8, 58), C1, dq=0.4, gaps=[(M(29, 8, 58, 40), C1)], idx=2)]
check(SG._tail_gap(_short, CFG) is None, "3г: хвост 80 с — короче льготы опроса, не хвост")
_inner = [sub(C0, M(29, 3, 0), gaps=[(M(28, 20, 0), M(28, 21, 0))], idx=1, dq=0.9), sub(M(29, 3, 0), C1, idx=2)]
check(SG._tail_gap(_inner, CFG) is None
      and [c for c in SG._sequence_checks(3, _inner, [], CFG) if c["check"] == "subseg_data_coverage"][0]["passed"],
      "3г: внутренний разрыв — не хвост, как раньше")
_emp = [sub(C0, M(29, 5, 0), idx=1, dq=0.8, gaps=[(M(29, 4, 30), M(29, 5, 0))]),
        sub(M(29, 5, 0), C1, idx=2, dq=0.0, gaps=[(M(29, 5, 0), C1)])]
_che = [c for c in SG._sequence_checks(3, _emp, [], CFG) if c["check"] == "subseg_data_coverage"][0]
check(_che["ranges"] == [{"start": iso(M(29, 4, 30)), "end": iso(C1)}] and _che["tail"]["sec"] == 16200.0,
      f"3г: пустой хвостовой подсегмент — одно окно от начала разрыва: {_che['ranges']}")
_tr = [sub(C0, M(29, 8, 0), idx=1), sub(M(29, 8, 0), C1, idx=2, dq=0.0, transient=True, gaps=[(M(29, 8, 0), C1)])]
check(SG._tail_gap(_tr, CFG)["work_sec"] == 0.0, "3г: хвост в переходном подсегменте — работу не уменьшает")
# Через сегментатор: синтетический разрыв идущего обрыва (gap_end None) до конца окна
_rows = pings(S(C0, -300), M(28, 11, 0))
_t = S(C0, -300)
while _t < M(28, 11, 0):
    _rows += [h(RPM, _t, 1500.0), h(FREQ, _t, 50.0)]
    _t = S(_t, 10)
_segT = segment([rs(3, M(28, 7, 0), None)], sorted(_rows, key=lambda r: r["ts"]), [],
                [{"gap_start": M(28, 11, 0), "gap_end": None}], CFG, "SN", "pcc", 1, "E", C0, M(28, 12, 0))
_cT = [c for c in work(_segT).sequence_checks if c["check"] == "subseg_data_coverage"][0]
check(_cT.get("tail") and _cT["tail"]["start"] == iso(M(28, 11, 0)) and _cT["tail"]["end"] == iso(M(28, 12, 0)),
      f"3д: сегментатор — хвост по синтетическому разрыву: {_cT}")


# ══ 4. Свежесть (defects[5], [6]) ════════════════════════════════════════════

NOW = datetime.now(U)


def eng(last, read_to, holes=(), poll=30):
    return NS(router_sn="SN", equip_type="pcc", panel_id=1, _fault_ref=None, tz=MSK,
              last_data_ts=last, data_read_to=read_to, data_holes=list(holes),
              live_ok_at=NOW, beat_mono=time.monotonic(), poll_interval_sec=poll)


# ── 4а. Состояния ──
df = _mgr.data_freshness
check(df(eng(S(NOW, -82), S(NOW, -30)), NOW, 90)[0] == "fresh",
      "4а: пауза 50 с (данные на 52 с старше границы чтения) — свежо (было «устарели 92 с»)")
check(df(eng(S(NOW, -221), S(NOW, -30)), NOW, 90) == ("stale", 191.0), "4а: разрыв 191 с — «устарели»")
check(df(eng(S(NOW, -40), None), NOW, 90) == ("unread", None), "4а: после старта не читали — «неизвестно»")
check(df(eng(None, None), NOW, 90)[0] == "fresh" and df(eng(None, S(NOW, -30)), NOW, 90)[0] == "fresh",
      "4а: данных ещё не было вовсе — как раньше, не «устарели»")
check(df(eng(S(NOW, -200), S(NOW, -190)), NOW, 90) == ("lag", 190.0)
      and df(eng(S(NOW, -170), S(NOW, -160)), NOW, 90) == ("hold", 160.0)
      and df(eng(S(NOW, -110), S(NOW, -100)), NOW, 90)[0] == "fresh",
      "4а: движок не читает > 180 с — «аналитика не обновляется»; окно не читалось 160 с — W7, "
      "статус ждёт молча (перепроверка, п. 3); 100 с — свежо")

# ── 4б. Перерыв по дыре в строках ──
_h = [(M(27, 18, 7, 28), M(27, 18, 10, 39))]
check(_mgr.hole_text(eng(M(27, 18, 11, 9), None, _h), M(27, 18, 7, 28), M(27, 18, 11, 31))
      == "3 мин 11 с: 18:07:28 → 18:10:39", "4б: перерыв 3 мин 11 с (было «4 мин» до тика)")
check(_mgr.hole_text(eng(M(27, 18, 11, 9), None, []), M(27, 18, 7, 28), M(27, 18, 11, 31))
      == "не больше 3 мин 41 с", "4б: дыры нет — оценка сверху до последней строки")
check(_mgr.hole_text(eng(M(27, 18, 11, 9), None, [(M(27, 17, 0), M(27, 17, 5))]), M(27, 18, 7, 28),
                     M(27, 18, 11, 31)).startswith("не больше"), "4б: дыра до обрыва — не его")

# ── 4в. Дыра в строках движка (без запросов) ──
from online.engine import OnlinePollEngine  # noqa: E402
_oe = OnlinePollEngine.__new__(OnlinePollEngine)
_oe.router_sn, _oe.equip_type, _oe.panel_id = "SN", "pcc", 1
_oe.last_data_ts = M(27, 18, 7, 28)
_hist = [{"ts": M(27, 18, 7, 18)}, {"ts": M(27, 18, 7, 28)}, {"ts": M(27, 18, 10, 39)}, {"ts": M(27, 18, 10, 49)}]
_oe._note_data_hole(_hist, M(27, 18, 10, 49))
check(list(_oe.data_holes) == [(M(27, 18, 7, 28), M(27, 18, 10, 39))], f"4в: дыра 18:07:28 → 18:10:39: {list(getattr(_oe, 'data_holes', []))}")
_oe.data_holes.clear()
_oe.last_data_ts = M(27, 18, 10, 39)
_oe._note_data_hole(_hist, M(27, 18, 10, 49))
check(not _oe.data_holes, "4в: шаг 10 с — не дыра")
check("self.data_read_to = ts_to_utc" in _src_eng and "self._note_data_hole(history, _seen_ts)" in _src_eng,
      "4в: граница чтения и дыра — в цикле открытого окна по уже прочитанной истории")


# ── 4г. Статус-тик: одно INFO после старта, «аналитика не обновляется», перерыв по дыре ──
async def _tick_case() -> None:
    from online import db as odb
    calls: list = []

    async def gos(sn, et, pid):
        calls.append(sn)
        return None

    async def setting(key, default=None):
        return default
    real = (odb.get_open_segment, _dba.get_app_setting)
    odb.get_open_segment, _dba.get_app_setting = gos, setting
    try:
        m = _mgr.OnlineManager()
        e = eng(datetime.now(U) - timedelta(seconds=110), None)
        m._engines = {"SN|pcc|1": e}
        m._engine_progress_text = lambda *a, **k: "—"
        k0 = len(LOG.recs)
        for _ in range(3):
            await m._tick_status_lines()
        check(len(msgs(logging.INFO, "свежесть данных после запуска ещё не прочитана", k0)) == 1
              and not msgs(logging.WARNING, "данные устарели", k0) and calls == [],
              "4г: после старта — одно INFO, без «устарели» (было «устарели 112–126 с») и без запросов")
        e.data_read_to = datetime.now(U) - timedelta(seconds=30)
        e.last_data_ts = datetime.now(U) - timedelta(seconds=40)
        await m._tick_status_lines()
        check(len(calls) == 1 and not m._stale_since, "4г: первое чтение — свежо, статус как раньше")
        e.data_read_to = datetime.now(U) - timedelta(seconds=200)
        k1 = len(LOG.recs)
        await m._tick_status_lines()
        await m._tick_status_lines()
        check(len(msgs(logging.WARNING, "аналитика не обновляется", k1)) == 1
              and not msgs(logging.WARNING, "данные устарели", k1) and len(calls) == 1,
              "4г: движок не читает 200 с — «аналитика не обновляется» одно WARNING, статус заморожен")
        _t0 = datetime.now(U)
        e.data_read_to, e.last_data_ts = _t0 - timedelta(seconds=20), _t0 - timedelta(seconds=221)
        k2 = len(LOG.recs)
        await m._tick_status_lines()
        check(len(msgs(logging.INFO, "аналитика снова обновляется", k2)) == 1
              and len(msgs(logging.WARNING, "данные устарели 201 с", k2)) == 1 and len(calls) == 1,
              f"4г: строки отстают от границы чтения 201 с — «устарели»: {[r.getMessage() for r in LOG.recs[k2:]]}")
        _lb = e.last_data_ts
        e.data_holes = [(_lb, _lb + timedelta(seconds=191))]
        e.last_data_ts, e.data_read_to = _lb + timedelta(seconds=200), _lb + timedelta(seconds=205)
        await m._tick_status_lines()
        _fr = msgs(logging.INFO, "данные снова свежие", k2)
        check(len(_fr) == 1 and "(перерыв 3 мин 11 с: " in _fr[0] and len(calls) == 2,
              f"4г: перерыв — по дыре в строках: {_fr}")
    finally:
        odb.get_open_segment, _dba.get_app_setting = real


asyncio.run(_tick_case())

# ── 4д. data_stale API ──
_m4 = NS(_engines={"K": eng(S(NOW, -82), S(NOW, -30))})
check(_mgr.data_stale_for(_m4, "K", S(NOW, -82), NOW, 90) is False,
      "4д: пауза 50 с — data_stale «ложь» (было «истина» по now − last_data_ts)")
_m4._engines["K"] = eng(S(NOW, -221), S(NOW, -30))
check(_mgr.data_stale_for(_m4, "K", S(NOW, -221), NOW, 90) is True, "4д: разрыв 191 с — «истина»")
_m4._engines["K"] = eng(S(NOW, -110), None)
check(_mgr.data_stale_for(_m4, "K", S(NOW, -110), NOW, 90) is True
      and _mgr.data_stale_for(None, "K", S(NOW, -60), NOW, 90) is False
      and _mgr.data_stale_for(None, "K", None, NOW, 90) is True,
      "4д: движок не читал / нет движка — прежняя формула от now")
_rt = (ROOT / "web" / "routes.py").read_text(encoding="utf-8")
check(_rt.count("data_stale = _data_stale(") == 2 and "from online.manager import data_stale_for" in _rt,
      "4д: оба маршрута считают data_stale помощником")


# ══ 5. Очередь пересказа (defects[13]) ═══════════════════════════════════════

async def _unhum() -> str:
    from corpus import db as cdb
    seen: dict = {}

    class _C:
        async def fetch(self, sql, *a):
            seen["sql"] = sql
            return [{"auto_segment_id": 36200}]

        async def close(self):
            pass

    async def _conn():
        return _C()
    real = cdb._connect
    cdb._connect = _conn
    try:
        ids = await cdb.get_unhumanized_segments()
    finally:
        cdb._connect = real
    return seen.get("sql", "") if ids == [36200] else ""


_sql = asyncio.run(_unhum())
check("conclusion_md ~ '\\S'" in _sql and "conclusion_md IS NOT NULL" in _sql,
      f"5: пустое заключение (#35030: '' вместо NULL) и из одних пробелов не отбирается: {_sql}")
_qsrc = (ROOT / "corpus" / "qwen_worker.py").read_text(encoding="utf-8")
_wsrc = (ROOT / "corpus" / "worker.py").read_text(encoding="utf-8")
check("if not conclusion_md.strip():" in _qsrc and "not str(humanized).strip()" in _qsrc
      and "if not content or not str(content).strip():" in _wsrc,
      "5: заключение и ответ модели из одних пробелов — пустые (ревью O8)")


# ══ 6. Ревью v5.1.32 ═════════════════════════════════════════════════════════

T6 = M(27, 14, 0, 0)
R6 = [(-600, 0), (0, 3)]

# ── А1. Медленный разгон 1410→1486 за 190 с — разгона нет: просадка, раскачка, «неустойчивый» ──
_slow6 = [(s, 1410.0 + s * 0.4, 47.0 + s * 0.013) for s in range(0, 200, 10)]
_sA1 = start_case(T6, _slow6, R6, b=400, tail=(200, 1500.0, 50.0))
_spA1 = work(_sA1).start_profile or {}
check(under(_sA1) and under(_sA1)[0][0] == iso(T6) and _spA1.get("runup_to") is None
      and _spA1.get("unstable") is True and "swing" in (_spA1.get("reasons") or [])
      and "1485 об/мин в первые 30 с не набрал" in (L.start_profile_text(_spA1, MSK) or ""),
      f"А1: порог не взят за 30 с — просадка с первого отсчёта, раскачка, «неустойчивый» (запрет снятия): "
      f"{under(_sA1)} {_spA1.get('unstable')} {_spA1.get('reasons')}")
check(SG.start_profile_params(CFG)["runup_max_sec"] == 30.0
      and _mono.det("RPM_UNDERSPEED", "runup_max_sec") == 30
      and _qsx.det("RPM_UNDERSPEED", "runup_max_sec") == 30
      and CFG.det("RPM_UNDERSPEED", "_source") == "CONTROLLER + OWNER",
      "А1: ключ runup_max_sec — в обоих слоях KB, происхождение блока — контроллер и решение владельца")
_sA1b = start_case(T6, _slow6, R6, b=400, tail=(200, 1500.0, 50.0),
                   cfg=cfg_det(CFG, "RPM_UNDERSPEED", runup_max_sec=300))
check(not under(_sA1b) and (work(_sA1b).start_profile or {}).get("runup_to") == iso(S(T6, 190)),
      "А1: предел из KB читается (300 с — тот же разгон уже разгон)")

# ── А2. Пуск за 10 с до суточного реза: разгон «ещё идёт» на резе, порог — после реза ──
TC6 = M(27, 9, 0, 0)
_e6 = [rs(0, S(TC6, -1200), S(TC6, -10)), rs(3, S(TC6, -10), None)]
_r6 = pings(S(TC6, -1500), S(TC6, 300)) + [h(RPM, S(TC6, -40), 0.0), h(FREQ, S(TC6, -200), 0.0)]
for _s, _rv, _fv in [(-10, 1406.0, 46.6), (0, 1440.0, 47.9), (10, 1498.0, 50.0)]:
    _r6 += [h(RPM, S(TC6, _s), _rv), h(FREQ, S(TC6, _s), _fv)]
for _s in range(20, 300, 10):
    _r6 += [h(RPM, S(TC6, _s), 1500.0), h(FREQ, S(TC6, _s), 50.0)]
_r6.sort(key=lambda r: r["ts"])
_pre6 = segment(_e6, _r6, [], [], CFG, "SN", "pcc", 2, "E", S(TC6, -1200), TC6)
_wpre = work(_pre6)
check(under(_pre6) == [] and (_wpre.start_profile or {}).get("runup_pending") is True
      and "1485 об/мин ещё не набрал" in (L.start_profile_text(_wpre.start_profile, MSK) or ""),
      f"А2: до реза разгон «ещё идёт» — просадки нет (было: 1406 в 08:59:50): {under(_pre6)}")
from online.engine import _start_carry  # noqa: E402
_post6 = segment(_e6, _r6, [], [], CFG, "SN", "pcc", 2, "E", TC6, S(TC6, 300), start_carry=_start_carry(_wpre))
_wpost = work(_post6)
check(under(_post6) == [] and (_wpost.start_profile_prev or {}).get("runup_to") == iso(S(TC6, 10)),
      f"А2: после реза порог 1498 в 09:00:10 по строкам окна — 1440 и перенос 1406 не просадка: "
      f"{under(_post6)} {(_wpost.start_profile_prev or {}).get('runup_to')}")

# ── А3. Окно закрыто автоматом до выхода на порог — просадка (машина под нагрузкой ниже 1485) ──
_CB = CFG.role_to_addr("GENSET_CB_POS")
_sA3 = start_case(T6, [(0, 1406.0, 46.6), (10, 1460.0, 48.6), (20, 1498.0, 50.0)], R6, b=300,
                  tail=(30, 1500.0, 50.0))
if _CB is not None:
    _enA3 = [rs(0, S(T6, -600), T6), rs(3, T6, None),
             {"addr": _CB, "value": 1, "state_start": S(T6, 5), "state_end": None}]
    _rows3 = pings(S(T6, -600), S(T6, 300)) + [h(RPM, S(T6, -40), 0.0), h(FREQ, S(T6, -200), 0.0)]
    for _s, _rv, _fv in [(0, 1406.0, 46.6), (10, 1460.0, 48.6), (20, 1498.0, 50.0)] +             [(x, 1500.0, 50.0) for x in range(30, 300, 10)]:
        _rows3 += [h(RPM, S(T6, _s), _rv), h(FREQ, S(T6, _s), _fv)]
    _rows3.sort(key=lambda r: r["ts"])
    _sA3 = segment(_enA3, _rows3, [], [], CFG, "SN", "pcc", 2, "E", S(T6, -300), S(T6, 300))
    _spA3 = work(_sA3).start_profile or {}
    check(under(_sA3) and under(_sA3)[0][0] == iso(T6) and _spA3.get("end_by") == "breaker"
          and "1485 об/мин до замыкания автомата не набрал" in (L.start_profile_text(_spA3, MSK) or ""),
          f"А3: автомат замкнут до порога — просадка с первого отсчёта, как раньше: {under(_sA3)}")

# ── А4. Частота первого отсчёта — только из его кадра ──
_sA4 = start_case(T6, [(0, 1406.0, None), (10, 1498.0, None), (11, None, 50.0)], R6, b=200,
                  tail=(20, 1500.0, 50.0))
_spA4 = work(_sA4).start_profile or {}
check(_spA4.get("first_freq") is None
      and (L.start_profile_text(_spA4, MSK) or "").startswith(
          "на рабочих оборотах с первого отсчёта (1406 об/мин; разгон между отсчётами"),
      f"А4: 50 Гц пришли после порога — не «1406 об/мин, 50 Гц»: {L.start_profile_text(_spA4, MSK)}")

# ── А5. Подписи порога ──
_sA5 = start_case(T6, [(0, 1406.0, 46.6), (2, None, 49.6), (10, 1300.0, 43.3), (20, 1498.0, 50.0)], R6, b=300,
                  tail=(30, 1500.0, 50.0))
_tA5 = L.start_profile_text(work(_sA5).start_profile, MSK) or ""
check("49,5 Гц и выше — с 14:00:02" in _tA5 and under(_sA5) and under(_sA5)[0][0] == iso(S(T6, 10)),
      f"А5: порог по частоте — «49,5 Гц и выше», провал 1300 после него — просадка: {_tA5}")
_spb = {"crank_at": iso(T6), "rated_at": iso(T6), "rated_sec": 0.0, "rated_first": True, "rated_bound_sec": 9.0,
        "t_to": iso(S(T6, 20)), "end_by": "rundown", "runup_rpm": 1485.0, "runup_hz": 49.5,
        "runup_max_sec": 30.0, "runup_to": None, "first_rpm": 1421.0, "first_freq": 47.4,
        "rpm": [1421.0, 1440.0], "freq": [47.4, 48.0], "swing_sec": 0.0, "flips": 0}
_tb = L.start_profile_text(_spb, MSK) or ""
check(_tb.startswith("на рабочих оборотах с первого отсчёта (1421 об/мин, 47,4 Гц; разгон между отсчётами, "
                     "не дольше 9 с), 1485 об/мин до конца окна пуска не набрал;")
      and "на номинале" not in _tb, f"А5: порог не взят, окно по выбегу — не «на номинале» (ДГУ №2 12.10): {_tb}")
_old_sp = {k: v for k, v in _spb.items() if not k.startswith("runup") and not k.startswith("first")}
check((L.start_profile_text(_old_sp, MSK) or "").startswith("на номинале с первого отсчёта"),
      "А5: профиль до v5.1.32 — текст прежний")

# ── А6. «Короткая работа» — по работе без хвоста без связи ──
_t60 = M(28, 8, 0)
_r60 = pings(S(_t60, -900), S(_t60, 80)) + [h(RPM, S(_t60, -40), 0.0), h(FREQ, S(_t60, -200), 0.0)]
for _s in range(0, 80, 10):
    _r60 += [h(RPM, S(_t60, _s), 1500.0), h(FREQ, S(_t60, _s), 50.0)]
_r60.sort(key=lambda r: r["ts"])
_s60 = segment([rs(0, S(_t60, -900), _t60), rs(3, _t60, None)], _r60, [],
               [{"gap_start": S(_t60, 70), "gap_end": None}], CFG, "SN", "pcc", 1, "E", S(_t60, -900), M(28, 9, 0))
_md60 = SER.render_summary_md(SER.build_summary_parts([work(_s60)], [], tz=MSK, trip_roles=["RPM", "FREQUENCY"],
                                                      t_from=_t60, t_to=M(28, 9, 0)))
check("### Ключевые показатели за 1 мин 10 с работы (ещё 58 мин 50 с без связи до 09:00)" in _md60
      and "Короткая работа (1 мин 10 с, 8 отсчётов)" in _md60 and "Мало отсчётов" not in _md60,
      f"А6: «Короткая работа (1 мин 10 с…)», а не «Мало отсчётов за работу (1 ч 00 мин…)»:\n{_md60}")

# ── А8. Несколько хвостов — «на концах сегментов», без «до HH:MM» ──
def _tail_seg(t0, t1, g0):
    _sb = [sub(t0, t1, dq=0.5, gaps=[(g0, t1)])]
    _ck = [c for c in SG._sequence_checks(3, _sb, [], CFG) if c["check"] == "subseg_data_coverage"]
    _sg = seg_of(_sb, _ck)
    _sg.subsegments[0].characteristics = {"RPM": {"median": 1500.0, "min": 1490.0, "max": 1505.0, "unit": "rpm",
                                                  "value_start": 1500.0, "value_end": 1500.0, "n": 500}}
    return _sg


_two = [_tail_seg(M(28, 9, 0), M(28, 12, 0), M(28, 11, 0)), _tail_seg(M(28, 13, 0), M(28, 16, 0), M(28, 15, 30))]
_p8 = SER.build_summary_parts(_two, [], tz=MSK, trip_roles=["RPM"], t_from=M(28, 9, 0), t_to=M(28, 16, 0))
_md8 = SER.render_summary_md(_p8)
check(_p8["key_metrics"]["tail_n"] == 2 and "tail_end_text" not in _p8["key_metrics"]
      and "за 4 ч 30 мин работы (ещё 1 ч 30 мин без связи на концах сегментов)" in _md8,
      f"А8: два хвоста — «на концах сегментов»: {[x for x in _md8.splitlines() if 'Ключевые' in x]}")

# ── Имя машины: разметка Markdown экранируется ──
check(SER.report_title("SN", 1, "ДГУ_1 *резерв* `x`\nвторая") ==
      "# Аналитический отчёт — ДГУ\\_1 \\*резерв\\* \\`x\\` вторая (`SN` / панель 1)"
      and SER.report_title("SN", 1, "ДЭС №3") == "# Аналитический отчёт — ДЭС №3 (`SN` / панель 1)",
      f"имя: экранирование и перевод строки: {SER.report_title('SN', 1, 'ДГУ_1 *резерв* `x`' + chr(10) + 'вторая')}")

# ── O1. W7 по границе чтения: движок не читал дольше порога телеметрии и опроса ──
_NOWo = datetime.now(U)


def _api_eng(read_ago, live_ago=31.0):
    e = OnlinePollEngine("SN", "pcc", 1, "E", cfg=None, poll_interval_sec=30)
    e.live_ok_at, e.live_ok_real = _NOWo - timedelta(seconds=live_ago), True
    e.data_read_to = _NOWo - timedelta(seconds=read_ago) if read_ago is not None else None
    e.last_data_ts = _NOWo - timedelta(seconds=(read_ago or 0) + 10)
    return e


_mO = _mgr.OnlineManager()
_mO._engines["SN|pcc|1"] = _api_eng(130)
_fO = _mgr.health_fields(_mO, "SN|pcc|1", "running", _NOWo, 90, data_stale=False)
_mO._engines["SN|pcc|1"] = _api_eng(60)
_fO2 = _mgr.health_fields(_mO, "SN|pcc|1", "running", _NOWo, 90, data_stale=False)
check(_fO["analytics_stale"] is True and _fO2["analytics_stale"] is False,
      f"O1: не читал 130 с (> 90 + 30) — analytics_stale, data_stale замер (было: ждали лага 180 с); 60 с — нет: "
      f"{_fO['analytics_stale']} {_fO2['analytics_stale']}")
_guide = (ROOT / "docs" / "ui_api_guide.md").read_text(encoding="utf-8")
check("замирает на последнем чтении" in _guide and "Движок не читает — `data_stale = false`" not in _guide,
      "O1: ui_api_guide — data_stale замирает, W7 по границе чтения")

# ── O2. Лаг тика — от live_ok_at (как у API), запасной — граница чтения ──
_eo2 = NS(last_data_ts=_NOWo - timedelta(seconds=40), data_read_to=_NOWo - timedelta(seconds=30),
          live_ok_at=_NOWo - timedelta(seconds=200), live_ok_real=True)
check(df(_eo2, _NOWo, 90)[0] == "lag" and df(NS(**dict(vars(_eo2), live_ok_real=False)), _NOWo, 90)[0] == "fresh",
      "O2: окно упало после чтения истории (live_ok_at 200 с назад) — «аналитика не обновляется»; "
      "старт без живого обновления — по границе чтения")


# ── O3. Срок «ещё не читали»; O7. возраст данных и «простой»; O4/O6 — перерыв ──
async def _tick_review() -> None:
    from online import db as odb
    calls: list = []

    async def gos(sn, et, pid):
        calls.append(sn)
        return None

    async def setting(key, default=None):
        return default
    real = (odb.get_open_segment, _dba.get_app_setting)
    odb.get_open_segment, _dba.get_app_setting = gos, setting
    try:
        m = _mgr.OnlineManager()
        _n0 = datetime.now(U)
        e = eng(_n0 - timedelta(seconds=300), None)
        e.started_at = _n0 - timedelta(seconds=200)
        m._engines = {"SN|pcc|1": e}
        m._engine_progress_text = lambda *a, **k: "—"
        k0 = len(LOG.recs)
        await m._tick_status_lines()
        await m._tick_status_lines()
        check(len(msgs(logging.WARNING, "аналитика не обновляется", k0)) == 1
              and "с запуска окно не прочитано" in msgs(logging.WARNING, "аналитика не обновляется", k0)[0]
              and not msgs(logging.INFO, "ещё не прочитана", k0) and calls == [],
              f"O3: не читали 200 с с запуска — WARNING «аналитика не обновляется», статус заморожен: "
              f"{[r.getMessage() for r in LOG.recs[k0:]]}")
        _n1 = datetime.now(U)
        e.data_read_to, e.last_data_ts = _n1 - timedelta(seconds=20), _n1 - timedelta(seconds=231)
        e.live_ok_at, e.live_ok_real = _n1 - timedelta(seconds=5), True
        k1 = len(LOG.recs)
        await m._tick_status_lines()
        _w = msgs(logging.WARNING, "данные устарели", k1)
        _i = msgs(logging.INFO, "аналитика снова обновляется (простой", k1)
        check(len(_i) == 1 and len(_w) == 1 and "данные устарели 211 с (последние данные 231 с назад)" in _w[0],
              f"O3/O7: выход из простоя — INFO с простоем от запуска; «устарели» — с возрастом данных: {_w} {_i}")
        _lb = e.last_data_ts
        # Обрыв 3 мин 11 с и пауза 40 с после возврата (sim_holes.py) — перерыв 3 мин 11 с
        e.data_holes = [(_lb, _lb + timedelta(seconds=191)), (_lb + timedelta(seconds=201), _lb + timedelta(seconds=241))]
        e.last_data_ts, e.data_read_to = _lb + timedelta(seconds=241), _lb + timedelta(seconds=245)
        await m._tick_status_lines()
        _fr = msgs(logging.INFO, "данные снова свежие", k1)
        check(len(_fr) == 1 and "(перерыв 3 мин 11 с: " in _fr[0],
              f"O4: пауза 40 с после возврата связи перерыв не удлиняет (было «4 мин 1 с»): {_fr}")
    finally:
        odb.get_open_segment, _dba.get_app_setting = real


asyncio.run(_tick_review())
check(_mgr.hole_text(eng(M(26, 18, 56, 9), None, [(M(25, 0, 11, 3), M(26, 18, 55, 59))]), M(25, 0, 11, 3),
                     M(26, 18, 57, 0)) == "42 ч 44 мин: 25.10 00:11:03 → 26.10 18:55:59",
      "O6: перерыв через сутки — с датами")
check(_mgr.hole_text(eng(M(24, 12, 39, 7), None, [(M(24, 5, 48, 15), M(24, 11, 48, 9)),
                                                   (M(24, 11, 48, 9), M(24, 12, 38, 59))]),
                     M(24, 5, 48, 15), M(24, 12, 39, 44)) == "6 ч 50 мин: 05:48:15 → 12:38:59",
      "O4: поздняя строка внутри обрыва (#36065) перерыв не делит")

# ── O5. Дыра, кончившаяся в закрытом окне ──
_oe5 = OnlinePollEngine.__new__(OnlinePollEngine)
_oe5.router_sn, _oe5.equip_type, _oe5.panel_id = "SN", "pcc", 1
_oe5._note_hole_pair(M(30, 8, 50, 0), M(30, 8, 58, 0))
_oe5._note_hole_pair(M(30, 8, 58, 0), M(30, 8, 58, 10))
check(list(_oe5.data_holes) == [(M(30, 8, 50, 0), M(30, 8, 58, 0))]
      and '_win_last: dict = {"after": getattr(self, "last_data_ts", None)}' in _src_eng
      and 'last_ts_out["holes"] = holes_between(' in _src_eng
      and 'for _ha, _hb in _win_last.get("holes") or []:' in _src_eng,
      "O5: закрытое окно отдаёт дыры после памяти свежести — пишутся без запроса")

# ══ 7. Перепроверка ревью v5.1.32 ════════════════════════════════════════════

# ── п. 4. Все промежутки, а не только первый: одиночная строка внутри обрыва ──
from online.engine import holes_between  # noqa: E402
_oe7 = OnlinePollEngine.__new__(OnlinePollEngine)
_oe7.router_sn, _oe7.equip_type, _oe7.panel_id = "SN", "pcc", 1
_oe7.last_data_ts = M(27, 18, 7, 28)
_oe7._note_data_hole([{"ts": M(27, 18, 7, 28)}, {"ts": M(27, 18, 10, 10)}, {"ts": M(27, 18, 10, 39)},
                      {"ts": M(27, 18, 10, 49)}], M(27, 18, 10, 49))
check(list(_oe7.data_holes) == [(M(27, 18, 7, 28), M(27, 18, 10, 10)), (M(27, 18, 10, 10), M(27, 18, 10, 39))]
      and _mgr.hole_text(eng(M(27, 18, 10, 49), None, _oe7.data_holes), M(27, 18, 7, 28), M(27, 18, 11, 40))
      == "3 мин 11 с: 18:07:28 → 18:10:39",
      f"п. 4: строка 18:10:10 внутри обрыва — перерыв 3 мин 11 с (было «2 мин 42 с»): {list(_oe7.data_holes)}")
_h7 = holes_between(M(30, 8, 0), [M(30, 8, 20)] + [S(M(30, 8, 58), 10 * i) for i in range(12)], 20.0)
check(_h7 == [(M(30, 8, 0), M(30, 8, 20)), (M(30, 8, 20), M(30, 8, 58))]
      and _mgr.hole_text(eng(M(30, 9, 30), None, _h7), M(30, 8, 0), M(30, 9, 31)) == "58 мин: 08:00:00 → 08:58:00",
      f"п. 4: закрытое окно, кадр 08:20 внутри обрыва — 58 мин (было «20 мин»): {_h7}")

# ── п. 1, 2. Срок «ещё не читали» — от старта текущего движка, с законной догонкой ──
async def _tick_p12() -> None:
    from online import db as odb
    calls: list = []

    async def gos(sn, et, pid):
        calls.append(sn)
        return None

    async def setting(key, default=None):
        return default
    real = (odb.get_open_segment, _dba.get_app_setting)
    odb.get_open_segment, _dba.get_app_setting = gos, setting
    try:
        m = _mgr.OnlineManager()
        m._engine_progress_text = lambda *a, **k: "—"
        _n = datetime.now(U)
        e1 = eng(_n - timedelta(seconds=40), None)
        e1.started_at = _n - timedelta(seconds=20)
        m._engines = {"SN|pcc|1": e1}
        await m._tick_status_lines()
        m._unread_since["SN|pcc|1"] -= timedelta(hours=2)        # СТОП в обход менеджера, 2 ч
        e2 = eng(_n - timedelta(seconds=40), None)
        e2.started_at = datetime.now(U) - timedelta(seconds=5)
        m._engines = {"SN|pcc|1": e2}
        k0 = len(LOG.recs)
        await m._tick_status_lines()
        check(not msgs(logging.WARNING, "аналитика не обновляется", k0),
              "п. 1: ПУСК через 2 ч — срок от старта нового движка, без «не обновляется 7220 с»")
        m._lag_since["SN|pcc|1"] = _n
        m._stale_since["SN|pcc|1"] = _n

        async def _stop():
            return None
        e2.stop = _stop
        await m._stop_engine("SN|pcc|1")
        check(not m._unread_since and not m._lag_since and not m._stale_since and not m._unread_logged,
              "п. 1: СТОП чистит память статус-тика по машине")
        # п. 2: рестарт посреди догонки — к порогу 180 с добавляется законная догонка
        e3 = eng(_n - timedelta(seconds=300), None)
        e3.started_at, e3.cursor_ts = datetime.now(U) - timedelta(seconds=250), datetime.now(U) - timedelta(days=1)
        m._engines = {"SN|pcc|1": e3}
        k1 = len(LOG.recs)
        await m._tick_status_lines()
        check(not msgs(logging.WARNING, "аналитика не обновляется", k1)
              and _mgr._catchup_estimate_sec(e3) >= 120.0,
              "п. 2: не читали 250 с при догонке через сутки (порог 180 + 120) — ещё не «не обновляется»")
        # п. 3: окно не читалось 125 с — статус и гейт ждут молча, как UI
        e4 = eng(datetime.now(U) - timedelta(seconds=130), datetime.now(U) - timedelta(seconds=125))
        m._engines = {"SN|pcc|1": e4}
        calls.clear()
        k2 = len(LOG.recs)
        await m._tick_status_lines()
        check(calls == [] and not [r for r in LOG.recs[k2:] if r.levelno >= logging.INFO],
              "п. 3: W7 (125 с без чтения) — тик не пишет статус и не пускает гейт, без WARNING")
    finally:
        odb.get_open_segment, _dba.get_app_setting = real


asyncio.run(_tick_p12())
_src_mgr = (ROOT / "online" / "manager.py").read_text(encoding="utf-8")
check(_src_mgr.count("self._forget_status_state(key)") >= 3
      and _src_mgr.count("self._forget_status_state(key, full=True)") == 1,
      "п. 1: память тика чистится везде, где движок снимается; «устарело» и простой — только при СТОП")

# ── п. 5. Обрыв связи сразу после первого отсчёта разгона ──
T7 = M(27, 15, 0, 0)


def _gap_case(b, pts, gaps, live):
    enum = [rs(0, S(T7, -600), T7), rs(3, T7, None)]
    rows = pings(S(T7, -600), S(T7, 1)) + pings(S(T7, 600), S(T7, b)) + [h(RPM, S(T7, -40), 0.0),
                                                                          h(FREQ, S(T7, -200), 0.0)]
    for _s, _rv, _fv in pts:
        if S(T7, _s) < S(T7, b):
            rows += [h(RPM, S(T7, _s), _rv), h(FREQ, S(T7, _s), _fv)]
    rows.sort(key=lambda r: r["ts"])
    return segment(enum, rows, [], gaps, CFG, "SN", "pcc", 2, "E", S(T7, -300), S(T7, b), live_edge=live)


for _b in (45, 120):
    _sg7 = _gap_case(_b, [(0, 1406.0, 46.6)], [{"gap_start": T7, "gap_end": None}] if _b > 90 else [], True)
    _sp7 = work(_sg7).start_profile or {}
    check(under(_sg7) == [] and _sp7.get("runup_pending") is True and _sp7.get("runup_wait") is True,
          f"п. 5: связь потеряна после 1406, живой край +{_b} с — ждём первого отсчёта после разрыва: {under(_sg7)}")
_pts7 = [(0, 1406.0, 46.6)] + [(s, 1500.0, 50.0) for s in range(600, 900, 10)]
_sg7c = _gap_case(900, _pts7, [{"gap_start": T7, "gap_end": S(T7, 600)}], False)
_sp7c = work(_sg7c).start_profile or {}
check(under(_sg7c) == [] and _sp7c.get("runup_to") == iso(S(T7, 600)) and _sp7c.get("runup_unobserved") is True
      and "выход на 1485 об/мин прошёл без связи (первый отсчёт после разрыва — 15:10:00)"
      in (L.start_profile_text(_sp7c, MSK) or ""),
      f"п. 5: связь вернулась на 1500 — разгон без связи, просадки нет (было: 1406 с 15:00:00): {under(_sg7c)}")
_pts7d = [(0, 1406.0, 46.6)] + [(s, 1450.0, 48.3) for s in range(600, 900, 10)]
_sg7d = _gap_case(900, _pts7d, [{"gap_start": T7, "gap_end": S(T7, 600)}], False)
check(under(_sg7d) and under(_sg7d)[0][0] == iso(T7) and (work(_sg7d).start_profile or {}).get("runup_to") is None,
      f"п. 5: связь вернулась на 1450 — порог не набран, просадка от первого отсчёта: {under(_sg7d)}")

# ── п. 6 и предел включительно: пока ждём — без раскачки; живой край ровно на пределе ──
_sp6 = (work(_pre6).start_profile or {})
# С финальной проверки (п. 3) раскачка и не считается дальше последнего отсчёта (было 10 с);
# подпись проверяется на профиле с раскачкой (п. 6)
check("раскачка" not in (L.start_profile_text(_sp6, MSK) or "")
      and "раскачка" not in (L.start_profile_text(dict(_sp6, swing_sec=10.0, flips=1), MSK) or ""),
      f"п. 6: пока ждём порога — раскачка от отсчёта разгона не печатается: {L.start_profile_text(_sp6, MSK)}")
_pB2 = [(0, 1406.0, 46.6), (10, 1460.0, 48.6), (20, 1480.0, 49.3), (30, 1490.0, 49.7)]
_fl = []
for _b in (29, 30, 31):
    _sB = start_case(T6, [p for p in _pB2 if p[0] < _b], R6, b=_b, live_edge=True)
    _fl.append(under(_sB))
check(_fl == [[], [], []], f"предел включительно: живой край +29/+30/+31 — без эпизода на 30-й и отзыва (было на +30): {_fl}")

# ══ 8. Финальная проверка ═════════════════════════════════════════════════════

# ── п. 1. Перезапуск сторожем во время обрыва — «устарело» и простой переживают его ──
async def _tick_wd() -> None:
    from online import db as odb

    async def gos(sn, et, pid):
        return None

    async def setting(key, default=None):
        return default
    real = (odb.get_open_segment, _dba.get_app_setting)
    odb.get_open_segment, _dba.get_app_setting = gos, setting
    try:
        m = _mgr.OnlineManager()
        m._engine_progress_text = lambda *a, **k: "—"
        _n = datetime.now(U)
        e1 = eng(_n - timedelta(seconds=400), _n - timedelta(seconds=30))
        e1.started_at = _n - timedelta(hours=1)
        m._engines = {"SN|pcc|1": e1}
        k0 = len(LOG.recs)
        await m._tick_status_lines()
        m._engines.pop("SN|pcc|1")
        m._forget_status_state("SN|pcc|1")                 # сторож снимает движок
        e2 = eng(_n - timedelta(seconds=400), None)
        e2.started_at = _n
        m._engines = {"SN|pcc|1": e2}
        await m._tick_status_lines()
        e2.data_read_to, e2.last_data_ts = _n - timedelta(seconds=20), _n - timedelta(seconds=25)
        e2.data_holes = [(_n - timedelta(seconds=400), _n - timedelta(seconds=60))]
        await m._tick_status_lines()
        check(len(msgs(logging.WARNING, "данные устарели", k0)) == 1
              and len(msgs(logging.INFO, "данные снова свежие (перерыв 5 мин 40 с", k0)) == 1,
              f"п. 1: перезапуск сторожем в обрыве — одно «устарели» и «снова свежие (перерыв …)»: "
              f"{[r.getMessage() for r in LOG.recs[k0:] if r.levelno >= logging.INFO]}")
    finally:
        odb.get_open_segment, _dba.get_app_setting = real


asyncio.run(_tick_wd())

# ── п. 2. Обрыв не делят поздняя пачка строк и живой островок, который тик не застал ──
check(_mgr.hole_text(eng(M(24, 12, 39, 7), None, [(M(24, 5, 48, 15), M(24, 6, 0, 0)), (M(24, 6, 5, 0), M(24, 12, 38, 59))]),
                     M(24, 5, 48, 15), M(24, 12, 39, 44)) == "6 ч 50 мин: 05:48:15 → 12:38:59",
      "п. 2: поздняя пачка 06:00–06:05 внутри обрыва — 6 ч 50 мин (было «11 мин 45 с»)")
check(_mgr.hole_text(eng(M(27, 18, 36), None, [(M(27, 18, 29), M(27, 18, 32, 38)), (M(27, 18, 33, 3), M(27, 18, 35, 48))]),
                     M(27, 18, 29), M(27, 18, 36, 30)) == "6 мин 48 с: 18:29:00 → 18:35:48",
      "п. 2: живой островок 25 с между двумя обрывами — оба (было только первый)")
check(_mgr.hole_text(eng(M(28, 0, 2, 10), None, [(M(27, 22, 55, 43), M(28, 0, 1, 14)), (M(28, 0, 1, 15), M(28, 0, 1, 57))]),
                     M(27, 22, 55, 43), M(28, 0, 2, 30)) == "1 ч 05 мин: 27.10 22:55:43 → 28.10 00:01:14",
      "п. 2: пауза 42 с после кадра возврата — не обрыв (было «1 ч 06 мин … 00:01:57»)")

# ── п. 3. Пока ждём порог при обрыве — без раскачки по удержанному отсчёту ──
for _b in (100, 3600):
    _sg8 = _gap_case(_b, [(0, 1406.0, 46.6)], [{"gap_start": T7, "gap_end": None}], True)
    _sp8 = work(_sg8).start_profile or {}
    check(_sp8.get("unstable") is False and _sp8.get("swing_sec") == 0.0 and under(_sg8) == [],
          f"п. 3: обрыв после 1406 / 46,6 Гц, живой край +{_b} с — не «неустойчивый» (было: раскачка "
          f"{'1 мин 40 с' if _b == 100 else '5 мин'}): {_sp8.get('unstable')} {_sp8.get('swing_sec')}")
_en8 = [rs(0, S(T7, -600), T7), rs(3, T7, S(T7, 600)), rs(0, S(T7, 600), None)]
_r8 = pings(S(T7, -600), S(T7, 1)) + pings(S(T7, 600), S(T7, 700)) + [h(RPM, S(T7, -40), 0.0), h(FREQ, S(T7, -200), 0.0),
                                                                      h(RPM, T7, 1406.0), h(FREQ, T7, 46.6)]
_r8 += [h(RPM, S(T7, _s), 0.0) for _s in range(600, 700, 10)] + [h(FREQ, S(T7, 600), 0.0)]
_r8.sort(key=lambda r: r["ts"])
_sg8c = segment(_en8, _r8, [], [{"gap_start": T7, "gap_end": S(T7, 600)}], CFG, "SN", "pcc", 2, "E",
                S(T7, -300), S(T7, 700))
_sp8c = work(_sg8c).start_profile or {}
check(under(_sg8c) == [] and _sp8c.get("runup_unseen") is True and _sp8c.get("unstable") is False
      and "выход на 1485 об/мин не наблюдали — связь пропала до предела" in (L.start_profile_text(_sp8c, MSK) or ""),
      f"п. 3: связь вернулась вместе со Стопом — разгон не наблюдали, 1406 не просадка: {under(_sg8c)} "
      f"{L.start_profile_text(_sp8c, MSK)}")

# ── п. 4. Разрыв — по пакетам и data_gaps, а не по тишине регистров ──
_en9 = [rs(0, S(T6, -600), T6), rs(3, T6, None)]
_r9 = pings(S(T6, -900), S(T6, 400)) + [h(RPM, S(T6, -40), 0.0), h(FREQ, S(T6, -200), 0.0), h(RPM, T6, 1406.0),
                                         h(FREQ, T6, 46.6)]
for _s in range(120, 400, 10):
    _r9 += [h(RPM, S(T6, _s), 1500.0), h(FREQ, S(T6, _s), 50.0)]
_r9.sort(key=lambda r: r["ts"])
_sg9 = segment(_en9, _r9, [], [], CFG, "SN", "pcc", 2, "E", S(T6, -300), S(T6, 400))
_sp9 = work(_sg9).start_profile or {}
check(under(_sg9) and under(_sg9)[0][0] == iso(T6) and not _sp9.get("runup_unobserved")
      and "прошёл без связи" not in (L.start_profile_text(_sp9, MSK) or ""),
      f"п. 4: пинги идут, обороты и частота молчат 120 с (1406 удержано) — просадка, а не «без связи»: "
      f"{under(_sg9)} {_sp9.get('runup_unobserved')}")

# ── O9. Имя машины из настроек — у живого движка ──
check("_eng.equip_name = name or None" in _rt, "O9: /settings/equipment/update обновляет имя у живого движка")



if _errors:
    print(f"FAIL — {len(_errors)} расхождений:")
    for x in _errors:
        print(f"  • {x}")
    sys.exit(1)
print("Все проверки пройдены.")
