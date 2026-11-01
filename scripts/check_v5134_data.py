# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""v5.1.34 «Решения владельца 01.11»: сверка на реальных данных (проект
docs/plans/design_v5132_tails.md, «Вопросы владельцу», В1–В3). ТОЛЬКО ЧТЕНИЕ —
соединения с default_transaction_read_only (проверка SHOW); все записи online.db
(сегменты, эпизоды, журнал тревог, статус) — в памяти, LLM и шлюза нет. Время — МСК.

«Стало» — кодом этого репозитория, «было» — тем же скриптом в подпроцессе на коде
до правки (--old-root: каталог с git archive HEAD).

Разделы:
- w1: ДЭС №3 — суточный рез 27.10 09:00 (#36137: обрыв 25.10 00:11:03 – 26.10
  18:55:59, заглушка #36125 перед ним) и 28.10 09:00 (#36162), между ними — цикл
  открытого окна 27.10 09:00–09:10: строка фактов по счётчикам, «с начала
  состояния», «Срабатываний с …», признаки головы, время режима по цепочке,
  статус, число запросов показаний до обрыва; итог на сегодня — статус открытой строки с
  признаком цепочки (время режима — от первых данных после обрыва);
- w2: статус ДЭС №3 сейчас (открытая строка) — уровень, строка, alarm_text, уровень API,
  хеши составов и решение «запускать ли гейт»; то же без аналитики (одна Общая
  авария);
- w3: журнал тревог за 30 дней — CLOSED в окне без своих строк (обрыв после
  реза или старта окна) и пары CLOSED/OPENED, которые уходят; реплей 29.10 —
  циклы 09:00:31… и 12:12:17… старым и новым кодом.

Дни с кривыми метками (24.10, 30.09–03.10, 05–09.10) не перерасчитываются.

Запуск:
    py -3 scripts/check_v5134_data.py --dsn-file .dsn --old-root <git archive HEAD> [w1 w2 w3]
Код возврата 1 — расхождения с ожиданиями проекта.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
if "--emit-root" in sys.argv:
    ROOT = Path(sys.argv[sys.argv.index("--emit-root") + 1]).resolve()
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True

import asyncpg  # noqa: E402

from online import db as odb  # noqa: E402
from online import engine as E  # noqa: E402
from online import status_assembler as SA  # noqa: E402

logging.basicConfig(level=logging.ERROR)
MSK = ZoneInfo("Europe/Moscow")
U = timezone.utc
RO = {"default_transaction_read_only": "on"}
SN, ET, P = "1126246373", "pcc", 1
_fails: list[str] = []
_OLD_ROOT: str | None = None
_DSN: str = ""
NEW = "--emit-root" not in sys.argv


def check(cond: bool, msg: str) -> None:
    print(("  OK   " if cond else "  FAIL ") + msg)
    if not cond:
        _fails.append(msg)


def T(day: int, h: int, mi: int, s: int = 0, mo: int = 10) -> datetime:
    return datetime(2026, mo, day, h, mi, s, tzinfo=MSK).astimezone(U)


def _u(t: Any) -> datetime | None:
    if t is None:
        return None
    if isinstance(t, str):
        t = datetime.fromisoformat(t)
    return t if t.tzinfo else t.replace(tzinfo=U)


def f(t: Any) -> str:
    t = _u(t)
    return t.astimezone(MSK).strftime("%d.%m %H:%M:%S") if t else "—"


def _j(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return None
    return v


def _cfg():
    from analytics.config import AnalyticsConfig
    return AnalyticsConfig.from_pair(ROOT / "knowledge_base", "pcc3300", "cummins_qsx15")


def _fref():
    from analytics.fault_ref import FaultRef
    kb = ROOT / "knowledge_base"
    return FaultRef(search_paths=[kb / "engines" / "cummins_qsx15", kb / "controllers" / "pcc3300"])


# ── online.db: чтение — из БД (только чтение), запись — в память ─────────────

class Mem:
    def __init__(self):
        self.closed: list[dict] = []
        self.open_row: dict | None = None
        self.journal: list[dict] = []
        self.eid = 9_000_000
        self.status: dict | None = None
        self.last_closed: dict | None = None


MEM = Mem()


async def _ro_connect():
    return await asyncpg.connect(_DSN, server_settings=RO)


def install_db() -> None:
    odb._connect = _ro_connect

    async def insert_closed_segment(data):
        MEM.closed.append(json.loads(json.dumps(data, default=str)))
        return 36137 + len(MEM.closed) - 1

    async def upsert_open_segment(data):
        MEM.open_row = json.loads(json.dumps(data, default=str))
        return 36195

    async def pop_open_segment(*a):
        r, MEM.open_row = MEM.open_row, None
        return r

    async def get_open_segment(*a):
        return MEM.open_row

    async def eid(*a, **k):
        MEM.eid += 1
        return MEM.eid

    async def alerts(sn, et, p, events):
        MEM.journal.extend(json.loads(json.dumps(events, default=str)))

    async def get_last_closed_segment(*a):
        return MEM.last_closed

    async def nothing(*a, **k):
        return None

    async def zero(*a, **k):
        return 0

    for n in ("update_observation_last_data_ts", "retarget_episode_segments", "insert_detection_events",
              "append_segment_gate_state", "set_episodes_gate_src_segment", "reopen_episode", "update_episode",
              "close_episode", "set_episode_context", "clear_open_gate_suppression_hash",
              "set_episodes_gate_suppressed", "apply_late_run_trip", "append_segment_gate_event"):
        setattr(odb, n, nothing)
    odb.clear_episodes_gate_suppressed = zero
    odb.insert_closed_segment = insert_closed_segment
    odb.upsert_open_segment = upsert_open_segment
    odb.pop_open_segment = pop_open_segment
    odb.get_open_segment = get_open_segment
    odb.open_episode = eid
    odb.insert_closed_episode = eid
    odb.insert_alert_events = alerts
    odb.get_last_closed_segment = get_last_closed_segment
    E._enqueue_segment = lambda *a, **k: None
    import online.verifier as ver
    ver.fire_verify = lambda *a, **k: None


LVB: list = []
# Запросы показаний счётчиков панели до обрыва (В1); удержанные значения окна
# (_load_seed, v5.1.21) идут тем же get_last_values_before по другим регистрам
_COUNTERS = {40069, 40070, 40074}


def count_lvb() -> None:
    from analytics import source as src
    f0 = src.get_last_values_before

    async def w(*a, **k):
        if set(a[3]) <= _COUNTERS:
            LVB.append((sorted(a[3]), a[4]))
        return await f0(*a, **k)
    src.get_last_values_before = w


def _engine():
    return E.OnlinePollEngine(SN, ET, P, "", _cfg(), poll_interval_sec=30, daily_hour=9, tz=MSK,
                              fault_ref=_fref())


def _lines(md: str | None, *keys: str) -> list[str]:
    return [x for x in (md or "").splitlines() if any(k in x for k in keys)]


def _seg_row(data: dict) -> dict:
    return {k: data.get(k) for k in ("run_state", "t_start", "characteristics_json", "current_values_json",
                                     "active_detections_json", "coking_risk_json", "gate_suppressed_hash")}


# ── Задачи (общие для «стало» и подпроцесса «было») ─────────────────────────

async def task_w1() -> dict:
    """#36137 (рез 27.10 09:00), открытое окно 27.10 09:00–09:10, #36162 (рез 28.10 09:00)."""
    LVB.clear()
    e = _engine()
    e.cursor_ts, e.continued_from_id, e.inherited_run_state_sec = T(26, 9, 0), None, {}
    e._last_open_run_state = 3
    out: dict = {}
    t0 = time.perf_counter()
    await e._close_window(T(26, 9, 0), T(27, 9, 0), "DAILY_BOUNDARY", lookahead_to=T(27, 9, 0),
                          live_to=T(27, 9, 0))
    c = MEM.closed[-1]
    ch, ff = c.get("characteristics_json") or {}, c.get("forward_fill_json") or {}
    out["c37"] = {"sec": round(time.perf_counter() - t0, 1), "continued_from": c.get("continued_from"),
                  "flags": {k: ch.get(k) for k in ("run_origin_unknown", "origin_from", "origin_gap")},
                  "ff_rs": ff.get("_run_state_sec"), "ff_ro": ff.get("_run_origin"), "lvb": len(LVB),
                  "rep": _lines(c.get("report_md"), "Обрыв связи", "начала состояния", "Срабатываний с",
                                "за период |"),
                  "sum": _lines(c.get("report_summary_md"), "Обрыв связи", "отсутствие связи",
                                "Ключевые показатели", "## "),
                  "facts": (_j(c.get("report_summary_json")) or {}).get("outage_facts")}
    out["eng37"] = {"ro": getattr(e, "inherited_run_origin", None),
                    "rs": {str(k): v for k, v in (e.inherited_run_state_sec or {}).items()},
                    "cf": e.continued_from_id}
    LVB.clear()
    t0 = time.perf_counter()
    await e._update_open_window(T(27, 9, 0), T(27, 9, 10))
    op = MEM.open_row or {}
    st = SA.build_structural_status(_seg_row(op), _fref(), MSK)
    out["open"] = {"sec": round(time.perf_counter() - t0, 1), "lvb": len(LVB),
                   "total": (op.get("characteristics_json") or {}).get("_total_run_state_sec"),
                   "ro": (op.get("characteristics_json") or {}).get("_run_origin"),
                   "status": SA.format_status_text(st), "level": st.get("severity_level"),
                   "panel": st.get("panel_severity"),
                   "since": [(d.get("values") or {}).get("startup_since") for d in op.get("active_detections_json") or []
                             if d.get("scenario") == "CONTROLLER_FAULT"]}
    LVB.clear()
    e._open_history_cache = None
    t0 = time.perf_counter()
    await e._close_window(T(27, 9, 0), T(28, 9, 0), "DAILY_BOUNDARY", lookahead_to=T(28, 9, 0),
                          live_to=T(28, 9, 0))
    c = MEM.closed[-1]
    ff = c.get("forward_fill_json") or {}
    out["c62"] = {"sec": round(time.perf_counter() - t0, 1), "continued_from": c.get("continued_from"),
                  "ff_rs": ff.get("_run_state_sec"), "ff_ro": ff.get("_run_origin"), "lvb": len(LVB),
                  "rep": _lines(c.get("report_md"), "начала состояния", "Срабатываний с"),
                  "sum": _lines(c.get("report_summary_md"), "Обрыв связи")}
    return out


async def task_w2(conn=None) -> dict:
    """Статус открытой строки сейчас: как его соберёт тик статуса (старым / новым кодом)."""
    c2 = await _ro_connect()
    try:
        r = await c2.fetchrow("SELECT * FROM auto_segments WHERE router_sn=$1 AND equip_type=$2 AND panel_id=$3 "
                              "AND t_end IS NULL", SN, ET, P)
    finally:
        await c2.close()
    row = dict(r)
    for k in ("characteristics_json", "active_detections_json", "current_values_json", "coking_risk_json"):
        row[k] = _j(row.get(k))
    out = {"id": row["id"], "stored_text": row.get("status_text"),
           "stored_struct": _j(row.get("status_struct_json"))}

    def one(rw):
        st = SA.build_structural_status(rw, _fref(), MSK)
        alarm = (SA.status_alarm_text(st) if hasattr(SA, "status_alarm_text") else SA.extract_alarm_text(st))
        sev = st["severity_level"]
        gate = not (sev == "норма" or ((not st.get("analytics_alarms") or st.get("analytics_suppressed"))
                                       and st.get("panel_severity") == "норма"))
        from web.segment_view import _parse_json  # noqa: F401  (тот же разбор, что у API)
        from online.status_assembler import _without_state, compute_severity_level, level_dets, suppression_active
        dets = _without_state(rw.get("active_detections_json") or [])
        api = compute_severity_level(level_dets(dets, suppression_active(rw, dets)))
        return {"text": SA.format_status_text(st), "level": sev, "panel": st.get("panel_severity"),
                "analytics": st.get("analytics_severity"), "alarm": alarm,
                "gate_alarm": SA.extract_alarm_text(st), "api_level": api, "gate_runs": gate,
                "fault_hash": SA.compute_fault_hash(st),
                "an_hash": SA.compute_analytics_hash(rw.get("active_detections_json") or []),
                "time": st.get("time_in_mode_sec")}
    out["now"] = one(row)
    only_ca = dict(row, active_detections_json=[d for d in row["active_detections_json"] or []
                                                if d.get("scenario") == "CONTROLLER_FAULT"])
    out["only_ca"] = one(only_ca)
    if NEW:
        # Цепочка с признаком (v5.1.34, В1): время режима — от первых данных после обрыва
        g0, g1 = T(25, 0, 11, 3), T(26, 18, 55, 59)
        ch = dict(row["characteristics_json"] or {})
        tot = dict(ch.get("_total_run_state_sec") or {})
        tot["3"] = float(tot.get("3") or 0) - (g1 - T(26, 9, 0)).total_seconds()
        ch["_total_run_state_sec"] = tot
        ch["_run_origin"] = {"run_origin_unknown": True, "origin_from": g1.isoformat(),
                             "gap_from": g0.isoformat(), "gap_to": g1.isoformat(), "run_state": 3}
        out["chain"] = one(dict(row, characteristics_json=ch))
    return out


async def task_w3() -> dict:
    """Реплей 29.10: тревога LIMIT_PROXIMITY|OIL_TEMP жива с 03:51:41, рез 09:00 в обрыве
    06:02:08–12:11:51; циклы открытого окна до и после возврата связи."""
    e = _engine()
    e.cursor_ts, e._last_open_run_state = T(29, 9, 0), 3
    # Живые тревоги на 09:00 — по журналу, как get_active_alerts на тот момент
    c2 = await _ro_connect()
    try:
        rows = await c2.fetch("""
            SELECT DISTINCT ON (scenario, values_json->>'addr', values_json->>'bit', values_json->>'role')
                   scenario, event_type, severity, trigger_text, values_json
            FROM alert_journal WHERE router_sn=$1 AND equip_type=$2 AND panel_id=$3 AND ts < $4
            ORDER BY scenario, values_json->>'addr', values_json->>'bit', values_json->>'role', ts DESC""",
                              SN, ET, P, T(29, 9, 0))
    finally:
        await c2.close()
    act = [{"scenario": r["scenario"], "severity": r["severity"], "trigger": r["trigger_text"],
            "values": _j(r["values_json"]) or {}} for r in rows if r["event_type"] != "CLOSED"]
    e._active_alerts = {E._alert_key(a, fallback=False): a for a in act}
    cycles = [T(29, 9, 0, 31), T(29, 9, 1, 1), T(29, 9, 1, 31), T(29, 12, 12, 17), T(29, 12, 12, 47),
              T(29, 12, 13, 17)]
    out = {"cycles": []}
    for t in cycles:
        n0 = len(MEM.journal)
        t0 = time.perf_counter()
        await e._update_open_window(T(29, 9, 0), t)
        out["cycles"].append({"t": t.isoformat(), "sec": round(time.perf_counter() - t0, 1),
                              "events": [(j["scenario"], j["event_type"]) for j in MEM.journal[n0:]],
                              "active": sorted(e._active_alerts)})
    return out


async def run_task(name: str) -> Any:
    install_db()
    count_lvb()
    return await {"w1": task_w1, "w2": task_w2, "w3": task_w3}[name]()


async def _old(name: str) -> Any:
    if not _OLD_ROOT:
        return None
    args = [sys.executable, "-X", "utf8", str(Path(__file__).resolve()), "--emit-root", _OLD_ROOT,
            "--dsn-file", os.environ["_CHK5134_DSNFILE"], "--emit-task", name]
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE)
    so, se = await proc.communicate()
    if proc.returncode != 0:
        print("  (старый код не отработал:", se.decode("utf-8", "replace")[-800:], ")")
        return None
    line = [x for x in so.decode("utf-8", "replace").splitlines() if x.startswith("JSON ")][-1]
    return json.loads(line[5:])


async def both(name: str):
    t0 = time.perf_counter()
    ot = asyncio.create_task(_old(name))
    new = json.loads(json.dumps(await run_task(name), default=str))
    old = await ot
    print(f"    (задача {name}: {time.perf_counter() - t0:.1f} с)")
    return new, old


# ── Разделы ──────────────────────────────────────────────────────────────────

TXT_3 = ("Обрыв связи 25.10 00:11:03 – 26.10 18:55:59 (42 ч 45 мин): по счётчикам 2 пуска (попыток "
         "961 → 963, успешных 951 → 953), наработка 40 ч 06 мин; когда был последний пуск — не видно")


async def w1(conn):
    print("\n══ В1: пуски в обрыве 25.10 00:11:03 – 26.10 18:55:59 (ДЭС №3, #36137 → #36162) ══")
    new, old = await both("w1")
    for lab, d in (("было ", old), ("стало", new)):
        if d is None:
            print(f"    {lab}: (нет)")
            continue
        c = d["c37"]
        print(f"    {lab} #36137 (рез 27.10 09:00, {c['sec']} с): continued_from={c['continued_from']}, "
              f"признаки {c['flags']}, ff._run_state_sec={c['ff_rs']}, запросов до обрыва {c['lvb']}")
        for x in c["rep"]:
            print(f"           отчёт:  {x}")
        for x in c["sum"]:
            print(f"           сводка: {x}")
        o = d["open"]
        print(f"    {lab} цикл 27.10 09:10 ({o['sec']} с, запросов до обрыва {o['lvb']}): "
              f"время {o['total']}, «с пуска» с {o['since']}")
        print(f"           статус: {o['status']!r} ({o['level']}, панель {o['panel']})")
        c = d["c62"]
        print(f"    {lab} #36162 (рез 28.10 09:00, {c['sec']} с): continued_from={c['continued_from']}, "
              f"ff._run_state_sec={c['ff_rs']}, ff._run_origin={'есть' if c['ff_ro'] else 'нет'}, "
              f"запросов до обрыва {c['lvb']}")
        for x in c["rep"] + c["sum"]:
            print(f"           {x}")
    c, o, c2 = new["c37"], new["open"], new["c62"]
    g1 = T(26, 18, 55, 59)
    check(c["lvb"] == 1 and c["flags"].get("run_origin_unknown") is True
          and c["flags"].get("origin_from") == g1.isoformat() and c["continued_from"] is None,
          "#36137: признаки головы run_origin_unknown / origin_from, один запрос показаний до обрыва на закрытии")
    check(any(TXT_3 in x for x in c["sum"]) and any(TXT_3 in x for x in c["rep"]),
          "#36137: строка фактов по счётчикам — в сводке и в отчёте, как в решении")
    check(any("с начала состояния (с 26.10 18:56, пуск в обрыве) | не меньше 14 ч 04 мин" in x for x in c["rep"]),
          "#36137: «с начала состояния (с 26.10 18:56, пуск в обрыве)»")
    check(c["ff_rs"] == {"3": (T(27, 9, 0) - g1).total_seconds()},
          "#36137: время режима по цепочке — от 18:55:59 (14 ч 04 мин 01 с), а не 24 ч")
    check(o["lvb"] == 0 and o["status"].startswith("Работа ≥ 14 ч 14 мин (последний пуск — в обрыве 25–26.10)")
          and o["since"] and all(s == g1.isoformat() for s in o["since"]),
          "открытое окно 27.10: статус «Работа ≥ …», «с пуска» — с 18:55:59, без запросов")
    check(c2["continued_from"] == 36137 and c2["lvb"] == 0
          and any("с начала состояния (с 26.10 18:56, пуск в обрыве) | не меньше 38 ч 04 мин" in x for x in c2["rep"]),
          "#36162: цепочка продолжается, «не меньше 38 ч 04 мин», без запроса")
    if old:
        check(not any("Обрыв связи" in x for x in old["c37"]["sum"])
              and old["c37"]["ff_rs"] == {"3": 86400.0}
              and any("с начала состояния (с 26.10 09:00)" in x for x in old["c62"]["rep"]),
              "было: строки фактов нет, время режима от 26.10 09:00")


async def w2(conn):
    print("\n══ В2: статус ДЭС №3 при висящей Общей аварии 1453 (с 24.10 16:00) ══")
    new, old = await both("w2")
    print(f"    в БД сейчас (#{new['id']}): {new['stored_text']!r}, alarm_text "
          f"{(new['stored_struct'] or {}).get('alarm_text')!r}")
    for key, title in (("now", "состав как есть"), ("only_ca", "только Общая авария")):
        for lab, d in (("было ", old), ("стало", new)):
            if d is None:
                continue
            x = d[key]
            print(f"    {lab} [{title}]: уровень {x['level']} (панель {x['panel']}, аналитика {x['analytics']}, "
                  f"API {x['api_level']}), гейт {'запускается' if x['gate_runs'] else 'нет'}, "
                  f"хеши {x['fault_hash']}/{x['an_hash']}")
            print(f"           строка: {x['text']!r}")
            print(f"           alarm_text статуса: {x['alarm']!r}; подпись разбора гейта: {x['gate_alarm']!r}")
    if "chain" in new:
        x = new["chain"]
        print(f"    стало [с признаком цепочки В1]: {x['text']!r}")
    n = new["now"]
    check(n["level"] == "предупреждение" and n["panel"] == "норма" and n["api_level"] == "предупреждение"
          and "🟡 панель: Общая авария — код 1453 «Отказ отключения автомата генераторной установки» с 24.10 16:00"
          in n["text"], "сейчас: «предупреждение» и строка с кодом 1453 и датой; источник панели — «норма»")
    check(new["only_ca"]["gate_runs"] is False and new["only_ca"]["level"] == "предупреждение",
          "одна Общая авария: уровень «предупреждение», гейт не запускается")
    if old:
        for key in ("now", "only_ca"):
            check(old[key]["fault_hash"] == new[key]["fault_hash"] and old[key]["an_hash"] == new[key]["an_hash"]
                  and old[key]["gate_runs"] == new[key]["gate_runs"] and old[key]["gate_alarm"] == new[key]["gate_alarm"],
                  f"[{key}]: хеши составов, запуск гейта и подпись разбора — как раньше")
    if "chain" in new:
        check(new["chain"]["text"].startswith("Работа ≥ ") and "(последний пуск — в обрыве 25–26.10)"
              in new["chain"]["text"], "с признаком цепочки: «Работа ≥ … (последний пуск — в обрыве 25–26.10)»")


async def w3(conn):
    print("\n══ В3: тревоги в окне без своих строк ══")
    since = datetime.now(U) - timedelta(days=30)
    rows = await conn.fetch("""
        SELECT id, router_sn, equip_type, panel_id, scenario, event_type, ts, values_json
        FROM alert_journal WHERE ts >= $1 ORDER BY router_sn, equip_type, panel_id, ts""", since)
    names = {(r["router_sn"], r["equip_type"], r["panel_id"]): r["name"] for r in await conn.fetch(
        "SELECT router_sn, equip_type, panel_id, name FROM equipment_registry")}

    def key(r):
        v = _j(r["values_json"]) or {}
        return (r["router_sn"], r["equip_type"], r["panel_id"], r["scenario"], v.get("addr"), v.get("bit"),
                v.get("role") if r["scenario"] == "LIMIT_PROXIMITY" else None)
    closed = [r for r in rows if r["event_type"] == "CLOSED"]
    blind: list = []
    for r in closed:
        seg = await conn.fetchrow("""SELECT t_start FROM auto_segments WHERE router_sn=$1 AND equip_type=$2
            AND panel_id=$3 AND t_start <= $4 AND (t_end IS NULL OR t_end > $4) ORDER BY t_start DESC LIMIT 1""",
                                  r["router_sn"], r["equip_type"], r["panel_id"], r["ts"])
        if seg is None:
            continue
        own = await conn.fetchval("""SELECT 1 FROM history_rich WHERE router_sn=$1 AND equip_type=$2
            AND panel_id=$3 AND ts >= $4 AND ts < $5 AND received_at <= $5 LIMIT 1""",
                                  r["router_sn"], r["equip_type"], r["panel_id"], seg["t_start"], r["ts"])
        if not own:
            # Возврат данных: первая своя строка окна, принятая после события
            back = await conn.fetchval("""SELECT min(received_at) FROM history_rich WHERE router_sn=$1
                AND equip_type=$2 AND panel_id=$3 AND ts >= $4 AND received_at > $5
                AND received_at < $5 + interval '3 days'""",
                                       r["router_sn"], r["equip_type"], r["panel_id"], seg["t_start"], r["ts"])
            blind.append((r, back))
    _bad = {(9, 30), (10, 1), (10, 2), (10, 3), (10, 5), (10, 6), (10, 7), (10, 8), (10, 9), (10, 24)}

    def bad(t):
        m = _u(t).astimezone(MSK)
        return (m.month, m.day) in _bad
    # Пара уходит, если тревога вернулась в первые циклы после возврата данных (≤ 3 мин):
    # она продолжается без CLOSED / OPENED. Иначе CLOSED лишь переезжает на первый
    # цикл со своими строками, а OPENED (если был) остаётся
    pairs = 0
    moved = 0
    detail = []
    for r, back in blind:
        k = key(r)
        nxt = next((x for x in rows if key(x) == k and x["ts"] > r["ts"]), None)
        if (nxt is not None and nxt["event_type"] == "OPENED" and back is not None
                and nxt["ts"] <= back + timedelta(seconds=180)):
            pairs += 1
            detail.append((names.get(k[:3], k[0]), r["scenario"], k[6], f(r["ts"]), f(nxt["ts"]),
                           bad(r["ts"]) or bad(nxt["ts"]), f(back)))
        else:
            moved += 1
            detail.append((names.get(k[:3], k[0]), r["scenario"], k[6], f(r["ts"]), None, bad(r["ts"]), f(back),
                           f(nxt["ts"]) if nxt is not None and nxt["event_type"] == "OPENED" else None))
    n_o = sum(1 for r in rows if r["event_type"] == "OPENED")
    n_bad = sum(1 for x in detail if x[5])
    print(f"    за 30 дней: событий {len(rows)} (OPENED {n_o}, CLOSED {len(closed)}); CLOSED в окне без своих "
          f"строк — {len(blind)}: пар CLOSED/OPENED уходит {pairs}, CLOSED переносится на возврат связи {moved}"
          f" (из них на днях с кривыми метками {n_bad})")
    for x in detail:
        print(f"      {x[0]}: {x[1]}{'|' + x[2] if x[2] else ''} CLOSED {x[3]}, данные с {x[6]}"
              + (f" / OPENED {x[4]} — пара уходит" if x[4] else
                 " — CLOSED переезжает на первый цикл со строками"
                 + (f" (OPENED {x[7]} остаётся)" if len(x) > 7 and x[7] else ""))
              + (" [кривые метки]" if x[5] else ""))
    check(any(x[1] == "LIMIT_PROXIMITY" and x[3] == "29.10 09:00:31" and x[4] for x in detail),
          "пара 29.10 CLOSED 09:00:31 / OPENED 12:12:17 — в числе уходящих")
    print("\n  — реплей 29.10 (тревога жива с 03:51:41, рез 09:00 в обрыве)")
    new, old = await both("w3")
    for lab, d in (("было ", old), ("стало", new)):
        if d is None:
            continue
        for c in d["cycles"]:
            print(f"    {lab} {f(c['t'])} ({c['sec']} с): события {c['events'] or '—'}, активны {c['active']}")
    # Масло после возврата связи — 99,0007 °C: с допуском v5.1.31 это уже не близость к
    # уставке (с 100 °C), поэтому нынешний детектор тревогу на 12:12 не поднимает —
    # CLOSED переезжает с реза на возврат связи, OPENED не будет
    cut_new = [tuple(e) for c in new["cycles"][:3] for e in c["events"]]
    ret_new = [tuple(e) for c in new["cycles"][3:] for e in c["events"]]
    check(not cut_new and all("LIMIT_PROXIMITY|OIL_TEMP" in c["active"] for c in new["cycles"][:3])
          and ("LIMIT_PROXIMITY", "OPENED") not in ret_new,
          "стало: в окне без своих строк — ни CLOSED, ни OPENED, тревога жива; при возврате связи — без OPENED")
    if old:
        cut_old = [tuple(e) for c in old["cycles"][:3] for e in c["events"]]
        check(cut_old == [("LIMIT_PROXIMITY", "CLOSED")], f"было: CLOSED на резе в обрыве ({cut_old})")


# ── Внутренний режим: задача кодом другого каталога, JSON в stdout ───────────

async def _emit(args) -> int:
    global _DSN
    from analytics import source as _src
    _DSN = Path(args.dsn_file).read_text(encoding="utf-8").strip()
    _src._pool = await asyncpg.create_pool(_DSN, min_size=1, max_size=3, command_timeout=900,
                                           server_settings=RO)
    try:
        async with _src._pool.acquire() as c2:
            assert await c2.fetchval("SHOW default_transaction_read_only") == "on"
        res = await run_task(args.emit_task)
    finally:
        await _src._pool.close()
    print("JSON " + json.dumps(res, ensure_ascii=False, default=str))
    return 0


async def main() -> int:
    global _OLD_ROOT, _DSN
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn-file", required=True, help="DSN одной БД (источник и аналитика)")
    ap.add_argument("--old-root", help="каталог кода до правки (git archive HEAD) — «было»")
    ap.add_argument("--emit-root", help=argparse.SUPPRESS)
    ap.add_argument("--emit-task", help=argparse.SUPPRESS)
    ap.add_argument("sections", nargs="*", help="разделы: w1 w2 w3; по умолчанию все")
    args = ap.parse_args()
    if args.emit_root:
        return await _emit(args)
    _DSN = Path(args.dsn_file).read_text(encoding="utf-8").strip()
    os.environ["_CHK5134_DSNFILE"] = str(Path(args.dsn_file).resolve())
    _OLD_ROOT = str(Path(args.old_root).resolve()) if args.old_root else None
    print("Сверка v5.1.34 на реальных данных. Соединение только для чтения, запись НЕ производится.")
    from analytics import source as _src
    conn = await asyncpg.connect(_DSN, server_settings=RO)
    _src._pool = await asyncpg.create_pool(_DSN, min_size=1, max_size=3, command_timeout=900,
                                           server_settings=RO)
    try:
        assert await conn.fetchval("SHOW default_transaction_read_only") == "on"
        async with _src._pool.acquire() as c2:
            assert await c2.fetchval("SHOW default_transaction_read_only") == "on"
        # Новый запрос начала режима (v5.1.34) — на живой строке, только чтение
        if NEW:
            odb._connect = _ro_connect
            # Открытая строка ДЭС №3 — какая сейчас (на суточном резе id меняется)
            _oid = await conn.fetchval("SELECT id FROM auto_segments WHERE router_sn=$1 AND equip_type=$2 "
                                       "AND panel_id=$3 AND t_end IS NULL", SN, ET, P)
            _o = await odb.get_run_state_origin_ts(_oid)
            print(f"    get_run_state_origin_ts({_oid}) = {f(_o)} (признака у цепочки в БД ещё нет — голова #36137)")
            check(_u(_o) == T(26, 9, 0), "новый запрос начала режима на живой цепочке — как раньше, 26.10 09:00")
            # Условие «связи вперёд» insert_closed_segment (ревью п. 9) — на живой таблице, чтением
            _n = await conn.fetchval("""SELECT count(*) FROM auto_segments WHERE t_end IS NOT NULL
                AND continued_from IS NULL
                AND NOT COALESCE(characteristics_json->'run_origin_unknown' = 'true'::jsonb, false)""")
            check(_n is not None and _n > 0, f"условие «связи вперёд» разбирается БД ({_n} строк без предка)")
        run_all = not args.sections
        if run_all or "w1" in args.sections:
            await w1(conn)
        if run_all or "w2" in args.sections:
            await w2(conn)
        if run_all or "w3" in args.sections:
            await w3(conn)
    finally:
        await conn.close()
        await _src._pool.close()
        _src._pool = None
    if _fails:
        print(f"\nРАСХОЖДЕНИЙ: {len(_fails)}")
        for x in _fails:
            print(f"  • {x}")
        return 1
    print("\nВсе ожидания проекта сходятся.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
