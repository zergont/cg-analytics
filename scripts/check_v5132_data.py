# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""v5.1.32 «Хвосты проверки v5.1.25»: сверка на реальных данных (проект
docs/plans/design_v5132_tails.md). ТОЛЬКО ЧТЕНИЕ — соединения с
default_transaction_read_only (проверка SHOW), запись не производится, LLM и
шлюза нет. Время — МСК.

«Стало» — перепрогон окон кодом этого репозитория (E._run_segment, как
перечитывается закрытое окно), «было» — тем же скриптом в подпроцессе на коде до
правки (--old-root: каталог с git archive HEAD).

Разделы:
- sql: очередь пересказа (п. 5) — прежний и новый отбор get_unhumanized_segments;
- start: пуски (п. 2) — ДГУ №2 27.10 12:02:48 и 12:06:48 (#36146, #36152: 1406 и
  1428 первым отсчётом, выбег 1251 при «Охлаждении» 12:03:59), ДГУ №1 26.10
  11:11:33 (#36133: провал 1466 после 1548 — остаётся), ДГУ №1 16.10 09:25:52
  (провал 1247 после заброса 1696 — остаётся);
- tail: хвост без связи (п. 3) — ДЭС №3 #36178 (28→29.10) и #36196 (30→31.10):
  строка ❗, заголовок «за N работы (ещё … без связи до 09:00)», строка полного
  отчёта; там же тексты п. 1 (имя машины, «МСК», уровень риска нагара);
- stale: журнал «данные устарели» 24.10–01.11 (п. 4, --journal: выгрузка
  journalctl с этими строками) — сколько входов станет при свежести по
  прочитанному (чтение окна каждые 30 с по правилу N+1, строки — по received_at;
  худший сдвиг фазы чтения), перерыв по дыре в строках.

Дни с кривыми метками (24.10, 30.09–03.10, 05–09.10) не перерасчитываются.

Запуск:
    py -3 scripts/check_v5132_data.py --dsn-file .dsn --old-root <git archive HEAD> \\
        --journal <stale_journal.txt> [sql start tail stale]
Код возврата 1 — расхождения с ожиданиями проекта.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
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

from online import engine as E  # noqa: E402

logging.basicConfig(level=logging.ERROR)
MSK = ZoneInfo("Europe/Moscow")
U = timezone.utc
RO = {"default_transaction_read_only": "on"}
MACH = {"ДГУ №1": ("6003790403", "pcc", 1), "ДГУ №2": ("6003790403", "pcc", 2),
        "ДЭС №3": ("1126246373", "pcc", 1)}
KEY2NAME = {"6003790403|pcc|1": "ДГУ №1", "6003790403|pcc|2": "ДГУ №2", "1126246373|pcc|1": "ДЭС №3"}
_ENG = {"6003790403": "cummins_kta50", "1126246373": "cummins_qsx15"}
BAD_DAYS = {(9, 30), (10, 1), (10, 2), (10, 3), (10, 5), (10, 6), (10, 7), (10, 8), (10, 9), (10, 24)}
_fails: list[str] = []
_OLD_ROOT: str | None = None


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


def _cfg(sn: str):
    from analytics.config import AnalyticsConfig
    return AnalyticsConfig.from_pair(ROOT / "knowledge_base", "pcc3300", _ENG.get(sn, "cummins_kta50"))


def _bad(t: datetime) -> bool:
    m = t.astimezone(MSK)
    return (m.month, m.day) in BAD_DAYS


# ── Разбор окна (общий для «стало» и подпроцесса «было») ─────────────────────

async def win_facts(key: str, a: datetime, b: datetime, task: str) -> list[dict]:
    """Сегменты окна кодом ROOT: пуск (детекции просадки, профиль, короткая
    работа) или хвост (покрытие, заголовок показателей, строка отчёта, тексты)."""
    from analytics.contract import CokingRisk
    from analytics import serializer as ser
    sn, et, p = MACH[key]
    cfg = _cfg(sn)
    segs, _inc, _g, _c = await E._run_segment(sn, et, p, "", a, b, cfg, CokingRisk())
    out = []
    roles = list(getattr(cfg, "trip_snapshot_roles", None) or [])
    for s in segs:
        if s.run_state != 3:
            continue
        d: dict[str, Any] = {"s": s.t_start, "e": s.t_end, "cause_close": s.cause_close}
        if task == "start":
            un = []
            for sub in s.subsegments:
                for det in sub.detections or []:
                    if det.scenario != "RPM_UNDERSPEED" or sub.transient:
                        continue
                    lv = (det.values or {}).get("live") or {}
                    for c in lv.get("clusters") or []:
                        un.append({"start": c.get("start"), "end": c.get("end"), "vmin": c.get("vmin")})
            uniq = {(x["start"], x["end"]): x for x in un}
            d["under"] = sorted(uniq.values(), key=lambda x: x["start"])
            sp = s.start_profile or {}
            d["sp"] = {k: sp.get(k) for k in ("crank_at", "rated_at", "rated_first", "runup_to", "first_rpm",
                                              "first_freq", "rpm", "freq", "swing_sec", "swing_from", "end_by",
                                              "t_to", "unstable")} if sp else None
            try:
                from analytics.labels import start_profile_text
                d["sp_text"] = start_profile_text(sp, MSK) if sp else None
            except Exception as ex:   # noqa: BLE001
                d["sp_text"] = repr(ex)
            km = ser._key_metrics_part([s], roles, tz=MSK)
            d["short"] = ser._km_short_text(km["short"]) if km and isinstance(km.get("short"), dict) else None
            rpm = next((r for r in (km or {}).get("rows") or [] if r.get("role") == "RPM"), None)
            d["rpm_row"] = {k: rpm.get(k) for k in ("min", "max", "start", "end", "n")} if rpm else None
            d["work_raw"] = (km or {}).get("work_raw_sec")
            d["subs"] = [(x.t_start, x.t_end, bool(x.transient)) for x in s.subsegments]
        else:
            cov = next((c for c in s.sequence_checks or [] if c.get("check") == "subseg_data_coverage"), {})
            d["cov"] = {"passed": cov.get("passed"), "ranges": cov.get("ranges"), "tail": cov.get("tail")}
            parts = ser.build_summary_parts([s], [], tz=MSK, trip_roles=roles, t_from=a, t_to=b)
            md = ser.render_summary_md(parts)
            d["km_head"] = next((x for x in md.splitlines() if x.startswith("### Ключевые показатели")), None)
            d["cov_line"] = [x for x in md.splitlines() if "отсутствие связи" in x]
            d["section"] = [x for x in md.splitlines() if x in ("### Замечания", "### К сведению")]
            km = parts.get("key_metrics") or {}
            d["km"] = {k: km.get(k) for k in ("work_raw_sec", "tail_blind_sec", "tail_end", "tail_end_text")}
            try:
                rep = ser.to_markdown([s], sn, et, p, a, b, "x", tz=MSK, equip_name="ДЭС №3")
            except TypeError:
                rep = ser.to_markdown([s], sn, et, p, a, b, "x", tz=MSK)
            d["rep_head"] = rep.splitlines()[0]
            d["rep_rs3"] = next((x for x in rep.splitlines() if x.startswith("| Время под нагрузкой (RUN_STATE=3) за период")), None)
            d["rep_counts"] = {w: rep.count(w) for w in ("Europe/Moscow", "МСК", "🟡 YELLOW", "🔴 RED",
                                                          "холостой ход 0 с", "ОЖ<60°C 0 с", "🟡 повышенный",
                                                          "🔴 высокий")}
        out.append(d)
    return out


async def _old(key: str, a: datetime, b: datetime, task: str) -> list[dict] | None:
    if not _OLD_ROOT:
        return None
    args = [sys.executable, "-X", "utf8", str(Path(__file__).resolve()), "--emit-root", _OLD_ROOT,
            "--dsn-file", os.environ["_CHK5132_DSNFILE"], "--emit-key", key, "--emit-task", task,
            "--emit-cuts", f"{a.isoformat()},{b.isoformat()}"]
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE)
    so, se = await proc.communicate()
    if proc.returncode != 0:
        print("  (старый код не отработал:", se.decode("utf-8", "replace")[-600:], ")")
        return None
    line = [x for x in so.decode("utf-8", "replace").splitlines() if x.startswith("JSON ")][-1]
    return json.loads(line[5:])


async def both(key, a, b, task):
    t0 = time.perf_counter()
    nt = asyncio.create_task(win_facts(key, a, b, task))
    ot = asyncio.create_task(_old(key, a, b, task))
    new, old = await nt, await ot
    print(f"    (окно {f(a)} – {f(b)}: {time.perf_counter() - t0:.1f} с)")
    return new, old


# ── Разделы ──────────────────────────────────────────────────────────────────

async def sql(conn):
    print("\n══ п. 5: очередь пересказа (get_unhumanized_segments) ══")
    old_q = ("SELECT auto_segment_id FROM segment_analyses WHERE status = 'done' AND conclusion_md IS NOT NULL "
             "AND (humanized_md IS NULL OR humanized_md = '')")
    import inspect
    from corpus import db as cdb
    src = inspect.getsource(cdb.get_unhumanized_segments)
    new_q = old_q.replace("AND (humanized_md", r"AND conclusion_md ~ '\S' AND (humanized_md")
    ws = await conn.fetchval(r"SELECT E'\n \t' ~ '\S'")
    o = [r[0] for r in await conn.fetch(old_q)]
    n = [r[0] for r in await conn.fetch(new_q)]
    print(f"    было  {len(o)}: {o[:10]}")
    print(f"    стало {len(n)}: {n[:10]}")
    check(r"conclusion_md ~ '\\S'" in src and ws is False,
          "код: фильтр пустого заключения (и из одних пробелов) в get_unhumanized_segments")
    check(35030 in o and 35030 not in n, "#35030 (conclusion_md = '') — больше не в очереди")


def _cl(u: list[dict]) -> str:
    return ", ".join(f"{f(x['start'])[6:]}–{f(x['end'])[6:]} ({x.get('vmin')})" for x in u) or "нет"


def _print_start(title, new, old):
    print(f"\n— {title}")
    for lab, ww in (("было ", old), ("стало", new)):
        if ww is None:
            print(f"    {lab}: (нет)")
            continue
        for d in ww:
            sp = d.get("sp") or {}
            print(f"    {lab} «Работа» {f(d['s'])} – {f(d['e'])}: просадка {_cl(d['under'])}")
            if sp:
                print(f"           профиль: обороты {sp.get('rpm')}, частота {sp.get('freq')}, раскачка "
                      f"{sp.get('swing_sec')} с, конец окна {f(sp.get('t_to'))} ({sp.get('end_by')}), "
                      f"выход на порог {f(sp.get('runup_to'))}")
                print(f"           «Пуск»: {d.get('sp_text')}")
            if d.get("short"):
                print(f"           {d['short']}")
            if d.get("rpm_row"):
                print(f"           обороты в таблице: {d['rpm_row']}, работа {d.get('work_raw')} с")


async def start(conn):
    print("\n══ п. 2: пуски — разгон до порога, выбег по кадру смены режима ══")
    # ДГУ №2 27.10: два пуска (#36146 12:02:48, #36152 12:06:48)
    n, o = await both("ДГУ №2", T(27, 12, 0), T(27, 13, 0), "start")
    _print_start("ДГУ №2 27.10 12:00–13:00 (#36146, #36152)", n, o)
    w1 = next((d for d in n if _u(d["s"]) == T(27, 12, 2, 48)), None)
    w2 = next((d for d in n if _u(d["s"]) == T(27, 12, 6, 48)), None)
    o1 = next((d for d in (o or []) if _u(d["s"]) == T(27, 12, 2, 48)), None)
    if o is not None:
        check(o1 is not None and any(_u(x["start"]) == T(27, 12, 2, 48) for x in o1["under"]),
              "было: 15041 — просадка 1406 в 12:02:48")
    check(w1 is not None and not w1["under"], "#36146: просадки на отсчёте разгона 1406 нет")
    check(w2 is not None and not w2["under"], "#36152: просадки на отсчёте разгона 1428 нет")
    if w1:
        sp = w1["sp"] or {}
        check(sp.get("rpm") == [1406.0, 1500.0] and sp.get("end_by") == "rundown"
              and _u(sp.get("t_to")) == T(27, 12, 3, 58), f"#36146: профиль без выбега 1251: {sp}")
        check(sp.get("swing_sec") == 0.0 and _u(sp.get("runup_to")) == T(27, 12, 2, 58),
              "#36146: раскачки нет, выход на порог 12:02:58")
        check((w1.get("short") or "").startswith("Короткая работа (1 мин 10 с, 5 отсчётов): обороты 1406 → 1500"),
              f"#36146: короткая работа без выбега: {w1.get('short')}")
        check((w1.get("rpm_row") or {}).get("min") == 1406.0, "#36146: минимум оборотов 1406, не 1251")
    # ДГУ №1 26.10: провал после выхода на порог — остаётся
    n, o = await both("ДГУ №1", T(26, 11, 5), T(26, 11, 30), "start")
    _print_start("ДГУ №1 26.10 11:05–11:30 (#36133, эпизод 15033)", n, o)
    check(any(any(_u(x["start"]) == T(26, 11, 11, 52) for x in d["under"]) for d in n),
          "ДГУ №1 26.10: провал 1466 в 11:11:52 после 1548 — остаётся")
    # ДГУ №1 16.10: провал после заброса — остаётся
    n, o = await both("ДГУ №1", T(16, 9, 20), T(16, 9, 35), "start")
    _print_start("ДГУ №1 16.10 09:20–09:35 (эпизод 14925)", n, o)
    check(any(any(_u(x["start"]) == T(16, 9, 26, 12) for x in d["under"]) for d in n),
          "ДГУ №1 16.10: провал 1247 в 09:26:12 после заброса 1696 — остаётся")


def _print_tail(title, new, old):
    print(f"\n— {title}")
    for lab, ww in (("было ", old), ("стало", new)):
        if ww is None:
            print(f"    {lab}: (нет)")
            continue
        for d in ww:
            print(f"    {lab} «Работа» {f(d['s'])} – {f(d['e'])}")
            print(f"           покрытие: passed={d['cov']['passed']}, хвост {d['cov'].get('tail')}")
            print(f"           сводка: {d['section']} {d['cov_line']}")
            print(f"           {d['km_head']}")
            print(f"           отчёт: {d['rep_head']}")
            print(f"           отчёт: {d['rep_rs3']}")
            print(f"           тексты: {d['rep_counts']}")


async def tail(conn):
    print("\n══ п. 3 и п. 1: хвост без связи на резе, тексты отчёта ══")
    for sid, a, b, gs, head, work in (
            (36178, T(28, 9, 0), T(29, 9, 0), T(29, 6, 2, 8),
             "за 21 ч 02 мин работы (ещё 2 ч 57 мин без связи до 09:00)", "29.10 06:02:08–09:00:00"),
            (36196, T(30, 9, 0), T(31, 9, 0), T(31, 4, 49, 4),
             "за 19 ч 49 мин работы (ещё 4 ч 10 мин без связи до 09:00)", "31.10 04:49:04–09:00:00")):
        n, o = await both("ДЭС №3", a, b, "tail")
        _print_tail(f"ДЭС №3 #{sid} {f(a)} – {f(b)}", n, o)
        d = n[-1] if n else {}
        t = (d.get("cov") or {}).get("tail") or {}
        check(_u(t.get("start")) == gs and _u(t.get("end")) == b, f"#{sid}: хвост {f(gs)} – {f(b)}: {t}")
        check(any(work in x for x in d.get("cov_line") or []), f"#{sid}: строка ❗ «{work}»")
        check(head in (d.get("km_head") or ""), f"#{sid}: заголовок «{head}»")
        check("в конце без связи)" in (d.get("rep_rs3") or ""), f"#{sid}: строка полного отчёта")
        if o:
            check("ещё" not in (o[-1].get("km_head") or "") and not o[-1]["cov_line"],
                  f"#{sid} было: «за 24 ч 00 мин работы», строки ❗ нет")
        rc = d.get("rep_counts") or {}
        check(d.get("rep_head") == "# Аналитический отчёт — ДЭС №3 (`1126246373` / панель 1)",
              f"#{sid}: заголовок с именем машины")
        check(rc.get("Europe/Moscow") == 0 and rc.get("МСК", 0) > 0, f"#{sid}: «МСК» вместо «Europe/Moscow»")
        check(rc.get("🟡 YELLOW") == 0 and rc.get("🔴 RED") == 0 and rc.get("холостой ход 0 с") == 0
              and rc.get("ОЖ<60°C 0 с") == 0, f"#{sid}: уровни по-русски, без нулевых слагаемых")


# ── Журнал «данные устарели»: свежесть по прочитанному ──────────────────────

def _journal(path: str) -> tuple[list[dict], list[datetime]]:
    ev, starts = [], []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        t = datetime.fromisoformat(line.split()[0])
        if "Started server process" in line:
            starts.append(t)
            continue
        m = re.search(r"StatusScheduler\[([^\]]+)\]: данные устарели (\d+) с", line)
        if m:
            ev.append({"kind": "stale", "t": t, "key": m.group(1), "age": int(m.group(2))})
            continue
        m = re.search(r"StatusScheduler\[([^\]]+)\]: данные снова свежие \(перерыв ([^)]+)\)", line)
        if m:
            ev.append({"kind": "fresh", "t": t, "key": m.group(1), "txt": m.group(2)})
    return ev, starts


def _sim(rows: list[tuple[datetime, datetime]], a: datetime, b: datetime, phase: float,
         stale: float = 90.0) -> tuple[float, tuple | None]:
    """Чтения окна каждые 30 с (фаза phase): граница чтения — время прошлого цикла
    (N+1), видны строки с ts < границы и received_at ≤ момента чтения. → (наибольшее
    отставание L = граница − last_data_ts, дыра (до, после) при выходе из «устарели»)."""
    last, mx, hole, was = None, 0.0, None, False
    k = a + timedelta(seconds=phase)
    prev = k - timedelta(seconds=30)
    while k <= b:
        vis = [ts for ts, rc in rows if ts < prev and rc <= k]
        new_last = max(vis, default=last)
        if last is not None and new_last is not None and new_last > last:
            first = min((ts for ts in vis if ts > last), default=None)
            if was and first is not None:
                hole = (last, first)
        last = new_last
        if last is not None:
            L = (prev - last).total_seconds()
            mx = max(mx, L)
            if L > stale:
                was = True
            elif was:
                was = False
        prev, k = k, k + timedelta(seconds=30)
    return mx, hole


async def stale(conn, journal: str | None):
    print("\n══ п. 4: журнал «данные устарели» 24.10–01.11 — свежесть по прочитанному ══")
    if not journal or not Path(journal).exists():
        check(False, "выгрузка журнала (--journal) не найдена")
        return
    ev, starts = _journal(journal)
    gaps = await conn.fetch("SELECT router_sn, panel_id, gap_start, gap_end FROM data_gaps "
                            "WHERE gap_start >= '2026-10-24'")
    stales = [e for e in ev if e["kind"] == "stale"]
    rows_out = []
    for e in stales:
        sn, _, p = e["key"].split("|")
        p = int(p)
        last = e["t"] - timedelta(seconds=e["age"])
        g = [x for x in gaps if x["router_sn"] == sn and x["panel_id"] == p
             and x["gap_start"] <= e["t"] + timedelta(seconds=60)
             and (x["gap_end"] or e["t"]) >= last - timedelta(seconds=60)]
        lt = e["t"].astimezone(MSK)
        cat = ("data_gaps" if g else "рез" if (lt.hour == 9 and lt.minute <= 6)
               else "рестарт" if any(0 <= (e["t"] - s).total_seconds() <= 300 for s in starts) else "пауза")
        fr = next((x for x in ev if x["kind"] == "fresh" and x["key"] == e["key"] and x["t"] > e["t"]), None)
        # Окно — от выхода из прошлого «устарели» этой машины (прошлый разрыв — свой
        # вход) до выхода из этого (следующий — тоже свой)
        pf = max((x["t"] for x in ev if x["kind"] == "fresh" and x["key"] == e["key"] and x["t"] < e["t"]),
                 default=None)
        a = last - timedelta(seconds=150)
        if pf is not None and pf > a:
            a = pf
        b = (fr["t"] + timedelta(seconds=30)) if fr else e["t"] + timedelta(seconds=600)
        hist = await conn.fetch(
            "SELECT ts, min(received_at) rc FROM history WHERE router_sn=$1 AND panel_id=$2 "
            "AND ts >= $3 AND ts < $4 GROUP BY ts ORDER BY ts", sn, p, a - timedelta(seconds=300), b)
        rr = [(r["ts"], r["rc"] or r["ts"]) for r in hist]
        sims = [_sim(rr, a, b, ph) for ph in range(0, 30, 5)]
        mx = max(s[0] for s in sims)
        holes = [s[1] for s in sims if s[1] is not None]
        gl = max(((((x["gap_end"] or e["t"]) - x["gap_start"]).total_seconds()) for x in g), default=None)
        rows_out.append({"cat": cat, "e": e, "mx": mx, "hole": holes[0] if holes else None, "fresh": fr, "gap": gl})
    n_old = len(rows_out)
    n_new = sum(1 for r in rows_out if r["mx"] > 90)
    by_cat: dict[str, list[int]] = {}
    for r in rows_out:
        c = by_cat.setdefault(r["cat"], [0, 0])
        c[0] += 1
        c[1] += r["mx"] > 90
    print(f"    входов было {n_old}, станет {n_new} (худшая фаза чтения)")
    for c, (a1, b1) in sorted(by_cat.items()):
        print(f"      {c:10s}: было {a1:3d} → станет {b1:3d}")
    print("    примеры (было → станет):")
    for r in rows_out:
        e = r["e"]
        tag = "останется" if r["mx"] > 90 else "уйдёт"
        h = r["hole"]
        ht = f"перерыв {int((h[1] - h[0]).total_seconds())} с: {f(h[0])[6:]} → {f(h[1])[6:]}" if h else "—"
        if r["cat"] != "data_gaps" or (r["gap"] or 0) < 139 or e["t"].astimezone(MSK).day in (27,) and e["t"].astimezone(MSK).hour == 18 and e["t"].astimezone(MSK).minute < 12:
            print(f"      {r['cat']:9s} {KEY2NAME.get(e['key'], e['key'])} {f(e['t'])}: «устарели {e['age']} с», "
                  f"«перерыв {r['fresh']['txt'] if r['fresh'] else '?'}» (разрыв {r['gap']}) → L max "
                  f"{r['mx']:.0f} с — {tag}; {ht}")
    remain_small = [r for r in rows_out if r["mx"] > 90 and r["cat"] != "data_gaps"]
    check(not remain_small, f"ложные входы без data_gaps не остаются: {[(r['cat'], f(r['e']['t'])) for r in remain_small]}")
    gap_ok = all((r["mx"] > 90) == ((r["gap"] or 0) >= 139) for r in rows_out if r["cat"] == "data_gaps")
    check(gap_ok, "входы при data_gaps: остаются ровно разрывы ≥ 139 с (63–81 с — короче порога)")
    print(f"    итог: {n_old} → {n_new}")


# ── Внутренний режим: окно кодом другого каталога, JSON в stdout ─────────────

async def _emit(args) -> int:
    from analytics import source as _src
    dsn = Path(args.dsn_file).read_text(encoding="utf-8").strip()
    _src._pool = await asyncpg.create_pool(dsn, min_size=1, max_size=3, command_timeout=600,
                                           server_settings=RO)
    try:
        async with _src._pool.acquire() as c2:
            assert await c2.fetchval("SHOW default_transaction_read_only") == "on"
        a, b = (_u(x) for x in args.emit_cuts.split(","))
        res = await win_facts(args.emit_key, a, b, args.emit_task)
    finally:
        await _src._pool.close()
    print("JSON " + json.dumps(res, ensure_ascii=False, default=str))
    return 0


async def main() -> int:
    global _OLD_ROOT
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn-file", required=True, help="DSN одной БД (источник и аналитика)")
    ap.add_argument("--old-root", help="каталог кода до правки (git archive HEAD) — «было»")
    ap.add_argument("--journal", help="выгрузка journalctl со строками «данные устарели» / «снова свежие»")
    ap.add_argument("--emit-root", help=argparse.SUPPRESS)
    ap.add_argument("--emit-key", help=argparse.SUPPRESS)
    ap.add_argument("--emit-task", help=argparse.SUPPRESS)
    ap.add_argument("--emit-cuts", help=argparse.SUPPRESS)
    ap.add_argument("sections", nargs="*", help="разделы: sql start tail stale; по умолчанию все")
    args = ap.parse_args()
    if args.emit_root:
        return await _emit(args)
    dsn = Path(args.dsn_file).read_text(encoding="utf-8").strip()
    os.environ["_CHK5132_DSNFILE"] = str(Path(args.dsn_file).resolve())
    _OLD_ROOT = str(Path(args.old_root).resolve()) if args.old_root else None
    print("Сверка v5.1.32 на реальных данных. Соединение только для чтения, запись НЕ производится.")
    from analytics import source as _src
    conn = await asyncpg.connect(dsn, server_settings=RO)
    _src._pool = await asyncpg.create_pool(dsn, min_size=1, max_size=3, command_timeout=600,
                                           server_settings=RO)
    try:
        assert await conn.fetchval("SHOW default_transaction_read_only") == "on"
        async with _src._pool.acquire() as c2:
            assert await c2.fetchval("SHOW default_transaction_read_only") == "on"
        run_all = not args.sections
        if run_all or "sql" in args.sections:
            await sql(conn)
        if run_all or "start" in args.sections:
            await start(conn)
        if run_all or "tail" in args.sections:
            await tail(conn)
        if run_all or "stale" in args.sections:
            await stale(conn, args.journal)
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
