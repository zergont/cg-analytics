# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""v5.1.31 «Близость по параметру»: сверка на реальных данных (проект
docs/plans/design_v5123_plus.md, §2.4, решения В-26.1, В-26.2, В-26.3, В-30.3).
ТОЛЬКО ЧТЕНИЕ — соединения с default_transaction_read_only (проверка SHOW),
запись не производится (UPDATE проверяется только EXPLAIN без ANALYZE), LLM и
шлюза нет. Время — МСК.

«Стало» — перепрогон окон кодом этого репозитория (E._run_segment, как
перечитывается закрытое окно), «было» — тем же скриптом в подпроцессе на коде
до правки (--old-root: каталог с git archive HEAD), «в БД» — живые эпизоды
alarm_episodes. Эпизод по участкам — кластер (values.live), кластеры соседних
окон ближе N = 600 с склеиваются; у близости — по ключу «LIMIT_PROXIMITY|роль»
(старый код — один ключ на три роли).

Разделы:
- sql: SQL v5.1.31 на реальной БД — роль строк близости (JSON / по уставке),
  existing_episode_onsets, count_episodes_batch и get_active_alerts по трём
  машинам (старый и новый запрос журнала тревог), find_episode_by_onset с
  ролью, set_episodes_gate_suppressed (EXPLAIN и SELECT того же отбора);
- lp: ДЭС №3 — перегрев 07.09 и 16.09 (масло и ОЖ раздельно, видов +1),
  #35872 (13.10 14:59:49 – 14.10 09:00: «2 эп., 35 мин 51 с, замечаний 5»),
  28–29.10 (масло: ровные 99 °C больше не близость, с 100 °C);
- nl: NO_LOAD у «Работы» со стоянки (В-30.3) — ДГУ №1 29.09 16:13:22–16:39:42
  и 27.09, ДГУ №2 12.10 13:44:39–14:00:59: эпизод с момента превышения 900 с
  от начала окна;
- n60: 60 дней 18.08–17.10 цепочкой (30 дней — 17.09–17.10): близость по ролям
  и NO_LOAD по машинам; окна дней с кривыми метками времени (30.09–03.10,
  05–09.10) в итог не идут.

Запуск:
    py -3 scripts/check_v5131_data.py --dsn-file .dsn --old-root <git archive HEAD>
    py -3 scripts/check_v5131_data.py --dsn-file .dsn --old-root <…> sql lp nl
Без --old-root «было» — только живые эпизоды БД. Код возврата 1 — расхождения.
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

# Внутренний режим --emit-root: тот же разбор окон кодом другого каталога
# (старый код — «было»); каталог в sys.path до импорта пакетов
ROOT = Path(__file__).resolve().parent.parent
_EMIT_ROOT: Path | None = None
if "--emit-root" in sys.argv:
    _EMIT_ROOT = Path(sys.argv[sys.argv.index("--emit-root") + 1]).resolve()
    ROOT = _EMIT_ROOT
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True

import asyncpg  # noqa: E402

from online import engine as E  # noqa: E402

logging.basicConfig(level=logging.ERROR)
MSK = ZoneInfo("Europe/Moscow")
U = timezone.utc
RO = {"default_transaction_read_only": "on"}
MACH = {"ДГУ №1": ("6003790403", "pcc", 1), "ДГУ №2": ("6003790403", "pcc", 2),
        "ДЭС №3": ("1126246373", "pcc", 1)}
_ENG = {"6003790403": "cummins_kta50", "1126246373": "cummins_qsx15"}
LP, NLK = "LIMIT_PROXIMITY", "NO_LOAD_TOO_LONG"
KEYS = (LP, NLK)
ROLE_RU = {"OIL_TEMP": "масло", "COOLANT_TEMP": "ОЖ", "OIL_PRESS": "давление"}
GLUE_SEC = 600.0
# Кривые метки времени (застывшие часы роутера ДЭС №3) — в итоги не идут
BAD_DAYS = {(9, 30), (10, 1), (10, 2), (10, 3), (10, 5), (10, 6), (10, 7), (10, 8), (10, 9), (10, 24)}
_fails: list[str] = []
_DSN: str | None = None
_OLD_ROOT: str | None = None


def check(cond: bool, msg: str) -> None:
    print(("  OK   " if cond else "  FAIL ") + msg)
    if not cond:
        _fails.append(msg)


def D(day: int, mo: int = 10) -> datetime:
    """Суточный рез 09:00 МСК."""
    return datetime(2026, mo, day, 9, 0, tzinfo=MSK).astimezone(U)


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


def dur(sec: float | None) -> str:
    if sec is None:
        return "—"
    sec = int(round(sec))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    if h:
        return f"{h} ч {m:02d} мин"
    return f"{m} мин {s:02d} с" if m else f"{s} с"


def _cfg(sn: str):
    from analytics.config import AnalyticsConfig
    return AnalyticsConfig.from_pair(ROOT / "knowledge_base", "pcc3300", _ENG.get(sn, "cummins_kta50"))


def _bad_day(t: datetime) -> bool:
    m = t.astimezone(MSK)
    return (m.month, m.day) in BAD_DAYS


# ── Разбор окон (общий для «стало» и подпроцесса «было») ─────────────────────

def _final_coking(segs) -> Any:
    for s in reversed(segs or []):
        for sub in reversed(s.subsegments or []):
            ra = getattr(sub, "risk_accumulators", None)
            if ra is not None and getattr(ra, "coking_risk", None) is not None:
                return ra.coking_risk
    return None


def _det_key(d) -> str:
    """Ключ тревоги детекции кодом ROOT: v5.1.31 — с ролью у близости, старый — сценарий."""
    try:
        from analytics.live import scenario_key
        return scenario_key(d.to_dict())
    except ImportError:
        return d.scenario


def _win_dets(segs) -> list[dict]:
    """Детекции окна (кроме панели и нагара): ключ, роль, подсегмент, кластеры values.live."""
    out = []
    for s in segs or []:
        for sub in s.subsegments or []:
            for d in sub.detections or []:
                if d.scenario in ("CONTROLLER_FAULT", "COKING_RISK"):
                    continue
                v = d.values or {}
                lv = v.get("live") if isinstance(v.get("live"), dict) else None
                out.append({
                    "sc": d.scenario, "key": _det_key(d), "role": v.get("role"),
                    "s": sub.t_start, "e": sub.t_end, "t_det": d.t_detected, "trig": d.trigger,
                    "transient": bool(getattr(sub, "transient", False)),
                    "v": {k: v.get(k) for k in ("value", "shutdown_limit", "noload_run_sec", "run_start",
                                                "carry_sec") if k in v},
                    "cl": ((lv or {}).get("clusters") or []) if d.scenario in KEYS else [],
                })
    return out


async def run_windows(key: str, cuts: list[datetime], chain: bool = True) -> list[dict]:
    """Окна [cuts[i], cuts[i+1]) кодом ROOT: детекции, время, нагар в конце окна.
    chain — аккумулятор нагара окна идёт в следующее (перенос «без нагрузки»)."""
    from analytics.contract import CokingRisk
    sn, et, p = MACH[key]
    cfg = _cfg(sn)
    cr = CokingRisk()
    out = []
    for a, b in zip(cuts, cuts[1:]):
        t0 = time.perf_counter()
        segs, _inc, _g, _c = await E._run_segment(sn, et, p, "", a, b, cfg,
                                                  cr if chain else CokingRisk())
        out.append({"a": a.isoformat(), "b": b.isoformat(), "sec": round(time.perf_counter() - t0, 2),
                    "dets": _win_dets(segs),
                    "segs": [{"s": s.t_start, "e": s.t_end, "rs": s.run_state} for s in segs]})
        fc = _final_coking(segs)
        if chain and fc is not None:
            import copy
            cr = copy.deepcopy(fc)
    return out


def episodes(wins: list[dict], key: str, lo: datetime | None = None, hi: datetime | None = None) -> list[dict]:
    """Эпизоды ключа по окнам: уникальные кластеры детекций с этим ключом (склейка
    соседних ближе N). У старого кода ключ близости — сценарий, роли кластера — roles."""
    iv: list[dict] = []
    seen: set = set()
    for w in wins:
        if lo is not None and _u(w["a"]) < lo:
            continue
        if hi is not None and _u(w["a"]) >= hi:
            continue
        for d in w["dets"]:
            if d["key"] != key and not (d["sc"] == key and d["key"] == d["sc"]):
                continue
            for c in d["cl"]:
                k = (c["start"], c.get("end"))
                if k in seen:
                    continue
                seen.add(k)
                iv.append({"s": _u(c["start"]), "e": _u(c["end"]), "viol": c.get("viol_sec"),
                           "vmax": c.get("vmax"), "roles": sorted((c.get("roles") or {}).keys()),
                           "run_sec": c.get("run_sec"), "run_start": c.get("run_start"),
                           "carry": c.get("carry_sec")})
    iv.sort(key=lambda x: (x["s"], x["e"]))
    out: list[dict] = []
    for x in iv:
        if out and ((x["s"] - out[-1]["e"]).total_seconds() <= GLUE_SEC or x["s"] <= out[-1]["e"]):
            y = out[-1]
            y["e"] = max(y["e"], x["e"])
            y["viol"] = (y.get("viol") or 0.0) + (x.get("viol") or 0.0)
            y["vmax"] = max(y.get("vmax") or 0.0, x.get("vmax") or 0.0)
            y["roles"] = sorted(set(y["roles"]) | set(x["roles"]))
            y["parts"] = y.get("parts", 1) + 1
            continue
        out.append(dict(x))
    return out


def ep_line(e: dict) -> str:
    bits = []
    if e.get("viol") is not None and e.get("run_sec") is None:
        bits.append(f"чистое {dur(e['viol'])}")
    if e.get("vmax") is not None and e.get("run_sec") is None:
        bits.append(f"макс. {e['vmax']:.2f}")
    if e.get("roles"):
        bits.append("роли " + "+".join(ROLE_RU.get(r, r) for r in e["roles"]))
    if e.get("run_sec") is not None:
        bits.append(f"отрезок {dur(e['run_sec'])} с {f(e.get('run_start'))}"
                    + (f", перенос {e['carry']:.0f} с" if e.get("carry") else ""))
    return f"{f(e['s'])} – {f(e['e'])}, {dur((e['e'] - e['s']).total_seconds())}" + (
        " (" + ", ".join(bits) + ")" if bits else "")


def kinds(wins: list[dict], lo: datetime | None = None, hi: datetime | None = None) -> list[str]:
    """Виды аналитики окна (ключи детекций рабочих подсегментов; без разовых событий)."""
    out = set()
    for w in wins:
        for d in w["dets"]:
            if d["transient"] or d["sc"] in ("LOAD_STEP", "COOLDOWN_VIOLATION", "WARMUP_VIOLATION"):
                continue
            if lo is not None and _u(d["e"]) <= lo:
                continue
            if hi is not None and _u(d["s"]) >= hi:
                continue
            out.add(d["key"])
    return sorted(out)


async def _old_windows(key: str, cuts: list[datetime], chain: bool = True) -> list[dict] | None:
    """«Было» — тот же разбор кодом --old-root в подпроцессе (JSON)."""
    if not _OLD_ROOT:
        return None
    args = [sys.executable, "-X", "utf8", str(Path(__file__).resolve()), "--emit-root", _OLD_ROOT,
            "--dsn-file", os.environ["_CHK5131_DSNFILE"], "--emit-key", key,
            "--emit-cuts", ",".join(c.isoformat() for c in cuts)]
    if not chain:
        args.append("--emit-nochain")
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE)
    so, se = await proc.communicate()
    if proc.returncode != 0:
        print("  (старый код не отработал:", se.decode("utf-8", "replace")[-400:], ")")
        return None
    line = [x for x in so.decode("utf-8", "replace").splitlines() if x.startswith("JSON ")][-1]
    return json.loads(line[5:])


async def both(key: str, cuts: list[datetime], chain: bool = True):
    new_task = asyncio.create_task(run_windows(key, cuts, chain))
    old_task = asyncio.create_task(_old_windows(key, cuts, chain))
    return await new_task, await old_task


async def db_eps(conn, key: str, a: datetime, b: datetime, scen: tuple = KEYS) -> list[dict]:
    sn, et, p = MACH[key]
    rows = await conn.fetch(
        "SELECT id, scenario, t_open, t_close, active_sec, open_values_json FROM alarm_episodes "
        "WHERE router_sn=$1 AND equip_type=$2 AND panel_id=$3 AND scenario = ANY($4::text[]) "
        "AND t_open < $6 AND (t_close IS NULL OR t_close > $5) ORDER BY t_open",
        sn, et, p, list(scen), a, b)
    return [dict(r) for r in rows]


def _tot(eps: list[dict]) -> float:
    return sum((x["e"] - x["s"]).total_seconds() for x in eps)


def print_lp(title: str, new: list[dict], old: list[dict] | None, lo=None, hi=None) -> dict:
    """Близость: было — один ключ, стало — по ролям. → {ключ: эпизоды стало}."""
    print(f"\n— {title}: близость к уставке останова")
    if old is not None:
        eo = episodes(old, LP, lo, hi)
        print(f"    было   {len(eo)} эп., {dur(_tot(eo))} (один ключ на три роли)")
        for e in eo[:12]:
            print("      " + ep_line(e))
        if len(eo) > 12:
            print(f"      … ещё {len(eo) - 12}")
    res = {}
    for k in sorted({d["key"] for w in new for d in w["dets"] if d["sc"] == LP}):
        en = episodes(new, k, lo, hi)
        res[k] = en
        print(f"    стало  {k}: {len(en)} эп., {dur(_tot(en))}")
        for e in en[:12]:
            print("      " + ep_line(e))
        if len(en) > 12:
            print(f"      … ещё {len(en) - 12}")
    if not res:
        print("    стало  эпизодов нет")
    if old is not None:
        print(f"    виды аналитики: было {kinds(old, lo, hi)}")
    print(f"    виды аналитики: стало {kinds(new, lo, hi)}")
    return res


# ── Разделы ──────────────────────────────────────────────────────────────────

async def sql(conn):
    print("\n══ SQL v5.1.31 на реальной БД (только чтение) ══")
    from analytics.live import limit_role
    from online import db as odb
    role_sql = odb._lp_role_sql()
    rows = await conn.fetch(f"SELECT id, router_sn, panel_id, open_values_json, {role_sql} AS lp_role "
                            "FROM alarm_episodes WHERE scenario = 'LIMIT_PROXIMITY'")
    mism, by = [], {}
    for r in rows:
        ov = r["open_values_json"]
        ov = json.loads(ov) if isinstance(ov, str) else (ov or {})
        py = limit_role({k: ov.get(k) for k in ("role", "shutdown_limit")})
        if py != r["lp_role"]:
            mism.append((r["id"], py, r["lp_role"]))
        by[(r["router_sn"], r["panel_id"], r["lp_role"], "JSON" if ov.get("role") else "уставка")] = \
            by.get((r["router_sn"], r["panel_id"], r["lp_role"], "JSON" if ov.get("role") else "уставка"), 0) + 1
    print(f"  строк близости: {len(rows)}; роль SQL / Python: {sorted(by.items(), key=str)}")
    check(not mism, f"SQL и Python дают одну роль у всех строк близости (расхождений {len(mism)}: {mism[:5]})")
    nulls = [k for k in by if k[2] is None]
    print(f"  без роли (ни JSON, ни уставки): {[(k, by[k]) for k in nulls]}")
    # Ключи онсетов и счётчики по ролям — новые запросы на реальных данных
    for key, (sn, et, p) in MACH.items():
        on = await conn.fetch("SELECT t_open FROM alarm_episodes WHERE router_sn=$1 AND equip_type=$2 "
                              "AND panel_id=$3 AND scenario='LIMIT_PROXIMITY' ORDER BY t_open DESC LIMIT 5",
                              sn, et, p)
        if on:
            got = await _with_conn(odb.existing_episode_onsets, sn, et, p, [r["t_open"] for r in on])
            print(f"  {key}: existing_episode_onsets (5 последних онсетов близости) → "
                  f"{sorted({k for k, _ in got if k.startswith(LP)})}")
            check(all(k.startswith(LP + "|") for k, _ in got if k.startswith(LP)),
                  f"{key}: онсеты близости — ключи с ролью")
        cnt = await _with_conn(odb.count_episodes_batch, sn, et, p, [LP, NLK], 30, None, None)
        print(f"  {key}: count_episodes_batch за 30 дней → " + json.dumps(
            {k: (v["count_window"], round(v["dur_window"] / 3600, 2)) for k, v in sorted(cnt.items())},
            ensure_ascii=False))
        # Журнал тревог: прежний запрос (DISTINCT ON без роли) и новый
        old_q = await conn.fetch("""
            SELECT DISTINCT ON (scenario, values_json->>'addr', values_json->>'bit')
                scenario, event_type, ts, values_json FROM alert_journal
            WHERE router_sn = $1 AND equip_type = $2 AND panel_id = $3
            ORDER BY scenario, values_json->>'addr', values_json->>'bit', ts DESC""", sn, et, p)
        new_a = await _with_conn(odb.get_active_alerts, sn, et, p)
        old_a = [r for r in old_q if r["event_type"] != "CLOSED"]
        _keys_new = sorted(E._alert_key(a) for a in new_a)
        _keys_old = sorted(r["scenario"] for r in old_a)
        print(f"  {key}: журнал тревог — висят по прежнему запросу {_keys_old}, по новому {_keys_new}")
        _lp_new = [k for k in _keys_new if k.startswith(LP)]
        # Ревью v5.1.31 (B): где по прежнему запросу близость не висела, не висит и по новому
        # (до v5.1.31 OPENED и CLOSED одной тревоги несли значения разных ролей — фантом)
        if LP in _keys_old:
            check(len(_lp_new) == 1 and "|" in _lp_new[0],
                  f"{key}: журнал — висевшая близость восстанавливается одной ролью ({_lp_new})")
        else:
            check(not _lp_new, f"{key}: журнал — близость не висела и не висит (новый запрос: {_lp_new})")
    # find_episode_by_onset с ролью — на открытом/последнем эпизоде ДЭС №3
    sn, et, p = MACH["ДЭС №3"]
    last = await conn.fetchrow(f"SELECT id, t_open, {role_sql} AS r FROM alarm_episodes WHERE router_sn=$1 "
                               "AND equip_type=$2 AND panel_id=$3 AND scenario='LIMIT_PROXIMITY' "
                               "ORDER BY t_open DESC LIMIT 1", sn, et, p)
    if last:
        hit = await _with_conn(odb.find_episode_by_onset, sn, et, p, scenario=LP, addr=None, bit=None,
                               t_open=last["t_open"], role=last["r"])
        miss = await _with_conn(odb.find_episode_by_onset, sn, et, p, scenario=LP, addr=None, bit=None,
                                t_open=last["t_open"], role="COOLANT_TEMP" if last["r"] != "COOLANT_TEMP"
                                else "OIL_TEMP")
        anyr = await _with_conn(odb.find_episode_by_onset, sn, et, p, scenario=LP, addr=None, bit=None,
                                t_open=last["t_open"])
        print(f"  find_episode_by_onset #{last['id']} ({last['r']}, {f(last['t_open'])}): своя роль → "
              f"{hit and hit['id']}, чужая → {miss and miss['id']}, без роли → {anyr and anyr['id']}")
        check(hit and hit["id"] == last["id"] and miss is None and anyr and anyr["id"] == last["id"],
              "find_episode_by_onset: своя роль находит, чужая — нет, без роли — как раньше")
    # set_episodes_gate_suppressed: план UPDATE (EXPLAIN без ANALYZE — не выполняется) и тот же отбор
    upd = (f"UPDATE alarm_episodes SET gate_suppressed = TRUE, updated_at = now() "
           f"WHERE router_sn = $1 AND equip_type = $2 AND panel_id = $3 AND t_close IS NULL "
           f"AND {odb._key_match_sql('$4')}")
    async with conn.transaction(readonly=True):
        plan = await conn.fetch("EXPLAIN " + upd, sn, et, p, [LP + "|OIL_TEMP"])
    print(f"  EXPLAIN UPDATE (только план): {plan[0][0][:80]}…")
    for keys in ([LP + "|OIL_TEMP"], [LP + "|COOLANT_TEMP"], [LP]):
        n = await conn.fetch(
            "SELECT id, " + role_sql + " AS r FROM alarm_episodes WHERE router_sn = $1 AND equip_type = $2 "
            "AND panel_id = $3 AND t_open > now() - interval '30 days' AND " + odb._key_match_sql("$4"),
            sn, et, p, keys)
        print(f"  отбор по ключам {keys} (эпизоды ДЭС №3 за 30 дней): {len(n)} строк, роли "
              f"{sorted({r['r'] for r in n}, key=str)}")
        if keys == [LP + "|COOLANT_TEMP"]:
            check(all(r["r"] == "COOLANT_TEMP" for r in n), "отбор «близость ОЖ» не берёт строки масла")
        if keys == [LP + "|OIL_TEMP"]:
            check(all(r["r"] == "OIL_TEMP" for r in n), "отбор «близость масла» — только строки масла")


async def _with_conn(fn, *a, **k):
    """Функция online.db на соединении только для чтения (подмена _connect)."""
    from online import db as odb
    orig = odb._connect

    async def _c():
        c = await asyncpg.connect(_DSN, server_settings=RO)
        assert await c.fetchval("SHOW default_transaction_read_only") == "on"
        return c
    odb._connect = _c
    try:
        return await fn(*a, **k)
    finally:
        odb._connect = orig


async def lp(conn):
    print("\n══ Близость к уставке по ролям (В-26.1) и допуск 0,01 (В-26.3) — ДЭС №3 ══")
    # Перегрев 07.09 и 16.09
    for d0 in (7, 16):
        a, b = D(d0, 9), D(d0 + 1, 9)
        new, old = await both("ДЭС №3", [a, b], chain=False)
        res = print_lp(f"ДЭС №3 {d0:02d}.09 (перегрев)", new, old)
        k_old, k_new = kinds(old or []), kinds(new)
        check(LP + "|COOLANT_TEMP" in res and LP + "|OIL_TEMP" in res,
              f"ДЭС №3 {d0:02d}.09: масло и ОЖ — раздельно")
        if old is not None:
            check(len(k_new) == len(k_old) + 1, f"ДЭС №3 {d0:02d}.09: видов аналитики +1 "
                                                f"({len(k_old)} → {len(k_new)})")
    # #35872: 13.10 14:59:49 – 14.10 09:00
    lo, hi = T(13, 14, 59, 49), D(14)
    new, old = await both("ДЭС №3", [D(13), D(14)], chain=False)
    res = print_lp("ДЭС №3 #35872 (13.10 14:59:49 – 14.10 09:00)", new, old, None, None)
    en = [x for x in res.get(LP + "|OIL_TEMP", []) if x["s"] >= lo and x["s"] < hi]
    rn = await conn.fetchval("SELECT (report_summary_json->'verdict'->>'remarks_n')::int FROM auto_segments "
                             "WHERE id = 35872")
    eo = [x for x in episodes(old or [], LP) if x["s"] >= lo and x["s"] < hi]
    print(f"    #35872: близость масла в сегменте — было {len(eo)} эп., {dur(_tot(eo))}; стало {len(en)} эп., "
          f"{dur(_tot(en))}; замечаний в БД (до v5.1.31) {rn}")
    for x in en:
        print("      " + ep_line(x))
    _kn = [k for k in kinds(new, lo, hi)]
    _ko = [k for k in kinds(old or [], lo, hi)]
    print(f"    виды в сегменте: было {_ko}, стало {_kn}")
    check(len(en) == 2 and abs(_tot(en) - (35 * 60 + 51)) <= 2,
          f"#35872: 2 эп., 35 мин 51 с (стало {len(en)} эп., {dur(_tot(en))})")
    check(len(_kn) == len(_ko), f"#35872: видов аналитики столько же ({len(_ko)} → {len(_kn)}), замечаний — {rn}")
    # 28–29.10: масло 99 vs 100
    new, old = await both("ДЭС №3", [D(28), D(29), D(30)], chain=False)
    res = print_lp("ДЭС №3 28–29.10 (масло 99 → с 100 °C)", new, old)
    eo = episodes(old or [], LP)
    en = res.get(LP + "|OIL_TEMP", [])
    vmax_low = [x for x in en if (x.get("vmax") or 0) < 100.0 - 0.001]
    check(not vmax_low, f"28–29.10: ни одного эпизода с максимумом ниже 100 °C ({len(vmax_low)})")
    if old is not None:
        check(_tot(en) < _tot(eo), f"28–29.10: время близости меньше ({dur(_tot(eo))} → {dur(_tot(en))})")


async def nl(conn):
    print("\n══ NO_LOAD_TOO_LONG у «Работы» со стоянки (В-30.3) ══")
    cases = (("ДГУ №1", [D(28, 9), D(29, 9), D(29, 9) + timedelta(hours=15)], T(29, 16, 13, 22, 9),
              T(29, 16, 39, 42, 9), "ДГУ №1 29.09 16:13:22–16:39:42"),
             ("ДГУ №2", [D(11), D(12), D(13)], T(12, 13, 44, 39), T(12, 14, 0, 59),
              "ДГУ №2 12.10 13:44:39–14:00:59"),
             ("ДГУ №1", [D(26, 9), D(27, 9), D(28, 9)], None, None, "ДГУ №1 27.09"))
    for key, cuts, w0, w1, title in cases:
        new, old = await both(key, cuts)
        lo = cuts[-2]
        eo = episodes(old or [], NLK, lo)
        en = episodes(new, NLK, lo)
        print(f"\n— {title}: без нагрузки")
        db = await db_eps(conn, key, cuts[-2], cuts[-1], (NLK,))
        for r in db:
            print(f"    в БД   #{r['id']}: {f(r['t_open'])} – {f(r['t_close'])}")
        print(f"    было   {len(eo)} эп., {dur(_tot(eo))}")
        for x in eo:
            print("      " + ep_line(x))
        print(f"    стало  {len(en)} эп., {dur(_tot(en))}")
        for x in en:
            print("      " + ep_line(x))
        _rw = [s for w in new for s in w["segs"] if s["rs"] == 3 and _u(s["s"]) >= lo]
        print("    «Работы» окна: " + "; ".join(f"{f(s['s'])}–{f(s['e'])}" for s in _rw))
        if w0 is not None:
            # Начало окна участков — рабочая часть «Работы» (после разгона до номинала,
            # секунды после смены режима): отрезок с него, нарушение — +900 с
            hit = [x for x in en if x.get("run_start")
                   and 0 <= (_u(x["run_start"]) - w0).total_seconds() <= 60
                   and abs((x["s"] - (_u(x["run_start"]) + timedelta(seconds=900))).total_seconds()) <= 2]
            check(bool(hit) and abs((hit[0]["e"] - w1).total_seconds()) <= 15,
                  f"{title}: эпизод с начала рабочей части + 900 с до конца «Работы» "
                  f"({'; '.join(ep_line(x) for x in hit) or 'нет'})")
            check(not [x for x in eo if x["s"] < w1 and x["e"] > w0],
                  f"{title}: старый код эпизода не давал")


async def n60(conn):
    print("\n══ 60 дней 18.08–17.10 цепочкой (30 дней — 17.09–17.10); дни с кривыми метками — без них ══")
    cuts = [D(18, 8) + timedelta(days=i) for i in range(61)]
    cut30 = D(17, 9)
    for key in MACH:
        new, old = await both(key, cuts)
        new_ok = [w for w in new if not _bad_day(_u(w["a"]))]
        old_ok = [w for w in (old or []) if not _bad_day(_u(w["a"]))]
        secs = [w["sec"] for w in new]
        osecs = [w["sec"] for w in old or []]
        print(f"\n  {key}: окон {len(new)} (в итоге {len(new_ok)}), перепрогон новым кодом {sum(secs):.0f} с"
              + (f", старым — {sum(osecs):.0f} с" if osecs else ""))
        lp_keys = sorted({d["key"] for w in new_ok for d in w["dets"] if d["sc"] == LP})
        for lab, lo in (("60 дн", cuts[0]), ("30 дн", cut30)):
            eo = episodes(old_ok, LP, lo)
            print(f"    близость {lab}: было {len(eo)} эп. / {dur(_tot(eo))}; стало " + ", ".join(
                f"{k.split('|')[-1]} {len(episodes(new_ok, k, lo))} эп. / {dur(_tot(episodes(new_ok, k, lo)))}"
                for k in lp_keys))
            en_nl, eo_nl = episodes(new_ok, NLK, lo), episodes(old_ok, NLK, lo)
            print(f"    без нагрузки {lab}: было {len(eo_nl)} эп. / {dur(_tot(eo_nl))}; стало {len(en_nl)} эп. / "
                  f"{dur(_tot(en_nl))}")
        # «Работы» не через рез, у которых отрезок без нагрузки начат с начала окна (В-30.3)
        en_nl = episodes(new_ok, NLK)
        eo_nl = episodes(old_ok, NLK)
        new_only = [x for x in en_nl if not any(o["s"] < x["e"] and o["e"] > x["s"] for o in eo_nl)]
        print(f"    без нагрузки: новых эпизодов (старый не видел) {len(new_only)}")
        for x in new_only[:15]:
            print("      " + ep_line(x))


SECTIONS = [("sql", sql), ("lp", lp), ("nl", nl), ("n60", n60)]


# ── Внутренний режим: окна кодом другого каталога, JSON в stdout ─────────────

async def _emit(args) -> int:
    from analytics import source as _src
    dsn = Path(args.dsn_file).read_text(encoding="utf-8").strip()
    _src._pool = await asyncpg.create_pool(dsn, min_size=1, max_size=3, command_timeout=600,
                                           server_settings=RO)
    try:
        async with _src._pool.acquire() as c2:
            assert await c2.fetchval("SHOW default_transaction_read_only") == "on"
        cuts = [_u(x) for x in args.emit_cuts.split(",")]
        wins = await run_windows(args.emit_key, cuts, chain=not args.emit_nochain)
    finally:
        await _src._pool.close()
    print("JSON " + json.dumps(wins, ensure_ascii=False, default=str))
    return 0


async def main() -> int:
    global _DSN, _OLD_ROOT
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn-file", help="DSN одной БД (источник и аналитика) — иначе config.yml")
    ap.add_argument("--old-root", help="каталог кода до правки (git archive HEAD) — «было»")
    ap.add_argument("--emit-root", help=argparse.SUPPRESS)
    ap.add_argument("--emit-key", help=argparse.SUPPRESS)
    ap.add_argument("--emit-cuts", help=argparse.SUPPRESS)
    ap.add_argument("--emit-nochain", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("sections", nargs="*", help="разделы: sql lp nl n60; по умолчанию все")
    args = ap.parse_args()
    if args.emit_root:
        return await _emit(args)
    if args.dsn_file:
        dsn = Path(args.dsn_file).read_text(encoding="utf-8").strip()
        src_dsn = dsn
        os.environ["_CHK5131_DSNFILE"] = str(Path(args.dsn_file).resolve())
    else:
        from config import settings
        dsn, src_dsn = settings.analytics_db_url, settings.source_db_url
        if args.old_root:
            print("--old-root требует --dsn-file")
            return 2
    _DSN = dsn
    _OLD_ROOT = str(Path(args.old_root).resolve()) if args.old_root else None
    print("Сверка v5.1.31 на реальных данных. Соединение только для чтения, запись НЕ производится.")
    from analytics import source as _src
    conn = await asyncpg.connect(dsn, server_settings=RO)
    _src._pool = await asyncpg.create_pool(src_dsn, min_size=1, max_size=3, command_timeout=600,
                                           server_settings=RO)
    run_all = not args.sections
    try:
        assert await conn.fetchval("SHOW default_transaction_read_only") == "on"
        async with _src._pool.acquire() as c2:
            assert await c2.fetchval("SHOW default_transaction_read_only") == "on"
        for name, fn in SECTIONS:
            if run_all or name in args.sections:
                if conn.is_closed():
                    conn = await asyncpg.connect(dsn, server_settings=RO)
                    assert await conn.fetchval("SHOW default_transaction_read_only") == "on"
                await fn(conn)
    finally:
        await conn.close()
        await _src._pool.close()
        _src._pool = None
    if _fails:
        print(f"\nРАСХОЖДЕНИЙ: {len(_fails)}")
        for x in _fails:
            print(f"  • {x}")
        return 1
    print("\nВсе ожидания плана сходятся.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
