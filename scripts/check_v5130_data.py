# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""v5.1.30 «По событию»: сверка на реальных данных (проект
docs/plans/design_v5123_plus.md, §2.3, решения В-27.1, В-27.2, В-30.1).
ТОЛЬКО ЧТЕНИЕ — соединения с default_transaction_read_only (проверка SHOW),
запись не производится, LLM и шлюза нет. Время — МСК.

«Стало» — перепрогон суточных окон кодом этого репозитория (E._run_segment,
как перечитывается закрытое окно), «было» — тем же скриптом в подпроцессе на
коде до правки (--old-root: каталог с git archive HEAD), «в БД» — живые
эпизоды alarm_episodes. Эпизод по участкам — кластер (values.live), кластеры
соседних окон ближе N = 600 с склеиваются (продолжение через рез); без
участков (старый код) — подряд идущие подсегменты с детекцией.

Разделы:
- i2: I₂ — ДГУ №1 12.10 (14886: стало 23:15:23–23:41:12, чистое 6 мин 49 с,
  непрерывно 89 с, запрет снятия до 23:51); ДЭС №3 09.10 (14830), ДГУ №2
  27.09, ДЭС №3 07.10 10:18–10:25 — эпизода нет; живые случаи ДГУ №1 15037
  (26.10) и 15061 (29.10) — что даёт новое правило; ДЭС №3 30–31.10 —
  справочно (эпизоды 15067–15079);
- ph: 29.09 (Сининда) — ДГУ №1 нейтраль 13:47:22–16:10:22, перекос
  13:52:52–16:10:22; ДГУ №2 13:52:39–15:58:29;
- nl: NO_LOAD 12.10 — ДГУ №1 три эпизода (14:26:53–14:39:02,
  15:03:13–15:18:23, 17:00:43–18:17:02), ДГУ №2 — три, 1 ч 42 мин; ДЭС №3
  30.09 — с 09:14:49 по 15:28:29 с переносом через рез, 01.10 — два эпизода
  (51 мин и 3 ч 52 мин) — окна цепочкой с аккумулятором нагара предыдущего;
- days: дни задания (ДГУ №1 16.10, ДГУ №2 15.10, ДЭС №3 13–16.10) — ни I₂,
  ни перекоса, ни нейтрали, ни NO_LOAD ни в старом, ни в новом;
- n60: 60 дней 18.08–17.10 цепочкой (30 дней — 17.09–17.10 из того же
  прогона): NO_LOAD по машинам, сколько нарушений старый код терял, I₂;
- b30: В-30.1 — эпизоды аналитики, закрытые в первые 3 мин после суточного
  реза при разрыве связи на 09:00 (по БД, 60 дней).

Запуск:
    py -3 scripts/check_v5130_data.py --dsn-file .dsn --old-root <git archive HEAD>
    py -3 scripts/check_v5130_data.py --dsn-file .dsn --old-root <…> i2 nl
Без --old-root «было» — только живые эпизоды БД. Код возврата 1 — расхождения.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
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
KEYS = ("NEGATIVE_SEQUENCE", "PHASE_CURRENT_SPREAD", "NEUTRAL_CURRENT", "NO_LOAD_TOO_LONG")
NAME = {"NEGATIVE_SEQUENCE": "I₂", "PHASE_CURRENT_SPREAD": "перекос", "NEUTRAL_CURRENT": "нейтраль",
        "NO_LOAD_TOO_LONG": "без нагрузки"}
GLUE_SEC = 600.0
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


# ── Разбор окон (общий для «стало» и подпроцесса «было») ─────────────────────

def _final_coking(segs) -> Any:
    for s in reversed(segs or []):
        for sub in reversed(s.subsegments or []):
            ra = getattr(sub, "risk_accumulators", None)
            if ra is not None and getattr(ra, "coking_risk", None) is not None:
                return ra.coking_risk
    return None


def _win_dets(segs) -> list[dict]:
    """Детекции ключей v5.1.30 окна: подсегмент, текст, кластеры values.live."""
    out = []
    for s in segs or []:
        for sub in s.subsegments or []:
            for d in sub.detections or []:
                if d.scenario not in KEYS:
                    continue
                v = d.values or {}
                lv = v.get("live") if isinstance(v.get("live"), dict) else None
                out.append({
                    "sc": d.scenario, "s": sub.t_start, "e": sub.t_end, "t_det": d.t_detected,
                    "trig": d.trigger,
                    "v": {k: v.get(k) for k in ("i2_pct_max", "duration_sec", "pct_max", "run_sec",
                                                "noload_run_sec", "run_start", "carry_sec") if k in v},
                    "cl": (lv or {}).get("clusters") or [],
                })
    return out


async def run_windows(key: str, cuts: list[datetime], chain: bool = True,
                      detail: bool = False) -> list[dict]:
    """Окна [cuts[i], cuts[i+1]) кодом ROOT: детекции ключей, время, нагар в
    конце окна. chain — аккумулятор нагара окна идёт в следующее (перенос
    «без нагрузки» через рез, как inherited_coking_risk движка)."""
    from analytics.contract import CokingRisk
    sn, et, p = MACH[key]
    cfg = _cfg(sn)
    cr = CokingRisk()
    out = []
    for a, b in zip(cuts, cuts[1:]):
        t0 = time.perf_counter()
        segs, _inc, _g, _c = await E._run_segment(sn, et, p, "", a, b, cfg,
                                                  cr if chain else CokingRisk())
        w = {"a": a.isoformat(), "b": b.isoformat(), "sec": round(time.perf_counter() - t0, 2),
             "dets": _win_dets(segs)}
        if detail:
            w["_segs"] = segs
            w["_cfg"] = cfg
        out.append(w)
        fc = _final_coking(segs)
        if chain and fc is not None:
            import copy
            cr = copy.deepcopy(fc)
    return out


def episodes(wins: list[dict], sc: str) -> list[dict]:
    """Эпизоды ключа по окнам: кластеры (склейка соседних ближе N) или, без
    участков, подряд идущие подсегменты с детекцией."""
    iv: list[dict] = []
    seen: set = set()
    for w in wins:
        for d in w["dets"]:
            if d["sc"] != sc:
                continue
            if d["cl"]:
                for c in d["cl"]:
                    k = (c["start"], c.get("end"))
                    if k in seen:
                        continue
                    seen.add(k)
                    iv.append({"s": _u(c["start"]), "e": _u(c["end"]), "viol": c.get("viol_sec"),
                               "longest": c.get("longest_sec"), "vmax": c.get("vmax"),
                               "smax": c.get("smax"), "spans": c.get("spans"),
                               "run_sec": c.get("run_sec"), "run_start": c.get("run_start"),
                               "carry": c.get("carry_sec"), "live": True})
            else:
                iv.append({"s": _u(d["s"]), "e": _u(d["e"]), "v": d["v"], "live": False})
    iv.sort(key=lambda x: (x["s"], x["e"]))
    out: list[dict] = []
    for x in iv:
        if out:
            y = out[-1]
            gap = (x["s"] - y["e"]).total_seconds()
            if (x["live"] and y["live"] and gap <= GLUE_SEC) or (not x["live"] and gap <= 1.0) \
                    or x["s"] <= y["e"]:
                if x["e"] > y["e"]:
                    y["e"] = x["e"]
                y["parts"] = y.get("parts", 1) + 1
                for k in ("viol", "longest", "vmax", "smax", "run_sec"):
                    if x.get(k) is not None:
                        y[k] = max(y.get(k) or 0.0, x[k]) if k != "viol" else (y.get(k) or 0.0) + x[k]
                continue
        out.append(dict(x))
    return out


def ep_line(e: dict) -> str:
    sec = (e["e"] - e["s"]).total_seconds()
    tail = ""
    if e.get("live"):
        bits = []
        if e.get("viol") is not None:
            bits.append(f"чистое {dur(e['viol'])}")
        if e.get("longest") is not None:
            bits.append(f"непрерывно до {e['longest']:.0f} с")
        if e.get("vmax") is not None and e.get("run_sec") is None:
            bits.append(f"макс. {e['vmax']:.2f} (устойч. {e.get('smax') or 0:.2f})")
        if e.get("spans"):
            bits.append(f"участков {e['spans']}")
        if e.get("run_sec") is not None:
            bits.append(f"отрезок {dur(e['run_sec'])} с {f(e.get('run_start'))}"
                        + (f", перенос {e['carry']:.0f} с" if e.get("carry") else ""))
        tail = " (" + ", ".join(bits) + ")" if bits else ""
    else:
        v = e.get("v") or {}
        if v:
            tail = " " + json.dumps({k: (round(x, 1) if isinstance(x, float) else x)
                                     for k, x in v.items()}, ensure_ascii=False)
    return f"{f(e['s'])} – {f(e['e'])}, {dur(sec)}{tail}"


def _serial(wins: list[dict]) -> list[dict]:
    return [{k: v for k, v in w.items() if not k.startswith("_")} for w in wins]


async def _old_windows(key: str, cuts: list[datetime], chain: bool = True) -> list[dict] | None:
    """«Было» — тот же разбор кодом --old-root в подпроцессе (JSON)."""
    if not _OLD_ROOT:
        return None
    args = [sys.executable, "-X", "utf8", str(Path(__file__).resolve()), "--emit-root", _OLD_ROOT,
            "--dsn-file", os.environ["_CHK5130_DSNFILE"], "--emit-key", key,
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


async def both(key: str, cuts: list[datetime], chain: bool = True, detail: bool = False):
    new_task = asyncio.create_task(run_windows(key, cuts, chain, detail))
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


def print_case(title: str, sc: str, new: list[dict], old: list[dict] | None, db: list[dict]) -> tuple:
    print(f"\n— {title}: {NAME[sc]}")
    dbx = [r for r in db if r["scenario"] == sc]
    for r in dbx:
        print(f"    в БД   #{r['id']}: {f(r['t_open'])} – {f(r['t_close'])}, "
              f"{dur((_u(r['t_close']) - _u(r['t_open'])).total_seconds()) if r['t_close'] else 'идёт'}")
    eo = episodes(old, sc) if old is not None else None
    en = episodes(new, sc)
    if eo is not None:
        if not eo:
            print("    было   эпизодов нет")
        for e in eo:
            print("    было   " + ep_line(e))
    if not en:
        print("    стало  эпизодов нет")
    for e in en:
        print("    стало  " + ep_line(e))
    return en, eo


def _has(en: list[dict], s: datetime, e: datetime, tol: float = 1.0) -> bool:
    return any(abs((x["s"] - s).total_seconds()) <= tol and abs((x["e"] - e).total_seconds()) <= tol
               for x in en)


# ── Разделы ──────────────────────────────────────────────────────────────────

async def i2(conn):
    print("\n══ I₂ (NEGATIVE_SEQUENCE): «непрерывно ≥ 60 с», эпизод — кластер (В-27.1) ══")
    from analytics.live import detection_live
    # ДГУ №1 12.10 — 14886
    new, old = await both("ДГУ №1", [D(12), D(13)], chain=False, detail=True)
    en, _ = print_case("ДГУ №1 12.10 (14886)", "NEGATIVE_SEQUENCE", new, old,
                       await db_eps(conn, "ДГУ №1", D(12), D(13)))
    check(len(en) == 1 and _has(en, T(12, 23, 15, 23), T(12, 23, 41, 12)),
          "ДГУ №1 12.10: один эпизод 23:15:23–23:41:12")
    if en:
        check(abs((en[0].get("viol") or 0) - 409) <= 1 and abs((en[0].get("longest") or 0) - 89) <= 1,
              f"ДГУ №1 12.10: чистое 6 мин 49 с, непрерывно 89 с ({en[0].get('viol')}, {en[0].get('longest')})")
        check(abs((en[0].get("vmax") or 0) - 6.97) < 0.01,
              f"ДГУ №1 12.10: максимум ряда 6,97 % (vmax {en[0].get('vmax')}, устойчивый {en[0].get('smax')})")
    # Запрет снятия: детекция жива, пока кластер идёт или кончился ≤ 600 с назад
    det = None
    for s in new[0]["_segs"]:
        for sub in s.subsegments:
            for d in sub.detections:
                if d.scenario == "NEGATIVE_SEQUENCE":
                    det = d
    if det is not None:
        a1, a2 = detection_live(det, T(12, 23, 51, 0)), detection_live(det, T(12, 23, 51, 30))
        print(f"    запрет снятия (детекция жива): 23:51:00 — {a1}, 23:51:30 — {a2}")
        check(a1 is True and a2 is False, "ДГУ №1 12.10: запрет снятия I₂ держится до 23:51:12")
    # Эпизода нет
    for key, d0, title in (("ДЭС №3", 8, "ДЭС №3 09.10 (14830, «117 с» одной точкой)"),
                           ("ДЭС №3", 7, "ДЭС №3 07.10 10:18–10:25 (53 с непрерывно)")):
        new, old = await both(key, [D(d0), D(d0 + 1)], chain=False)
        en, _ = print_case(title, "NEGATIVE_SEQUENCE", new, old, await db_eps(conn, key, D(d0), D(d0 + 1)))
        await raw_runs(key, D(d0), D(d0 + 1))
        check(not en, f"{title}: эпизода нет")
    new, old = await both("ДГУ №2", [D(27, 9), D(28, 9)], chain=False)
    en, eo = print_case("ДГУ №2 27.09 («780 с» одним переходным кадром)", "NEGATIVE_SEQUENCE", new, old,
                        await db_eps(conn, "ДГУ №2", D(27, 9), D(28, 9)))
    await raw_runs("ДГУ №2", D(27, 9), D(28, 9))
    check(not en, "ДГУ №2 27.09: эпизода нет")
    # Живые случаи ДГУ №1: 15037 (26.10), 15061 (29.10)
    for d0, eid in ((26, 15037), (29, 15061)):
        new, old = await both("ДГУ №1", [D(d0), D(d0 + 1)], chain=False)
        en, eo = print_case(f"ДГУ №1 {d0}.10 (живой {eid})", "NEGATIVE_SEQUENCE", new, old,
                            await db_eps(conn, "ДГУ №1", D(d0), D(d0 + 1)))
        await raw_runs("ДГУ №1", D(d0), D(d0 + 1))
        _clusters_note(new, "NEGATIVE_SEQUENCE")
        check(all((x["e"] - x["s"]).total_seconds() < 6 * 3600 for x in en),
              f"ДГУ №1 {d0}.10 ({eid}): эпизод не тянется до реза")
    # ДЭС №3 30–31.10 — справочно
    new, old = await both("ДЭС №3", [D(30), D(31)], chain=False)
    print_case("ДЭС №3 30.10 (справочно: живые 15067–15079)", "NEGATIVE_SEQUENCE", new, old,
               await db_eps(conn, "ДЭС №3", D(30), D(31)))


def _clusters_note(wins: list[dict], sc: str) -> None:
    """Тексты детекций нового кода (справка)."""
    for w in wins:
        for d in w["dets"]:
            if d["sc"] == sc:
                print(f"      детекция {f(d['s'])}–{f(d['e'])}: {d['trig']}")


async def raw_runs(key: str, a: datetime, b: datetime, role: str = "I2", top: int = 3) -> None:
    """Справка: самые длинные участки ряда фаз ≥ порога до квалификации (почему
    эпизода нет или он короткий) — выход live._phase_spans при перепрогоне."""
    import analytics.live as _lv
    got: list[dict] = []
    orig = _lv._phase_spans

    def rec(*a_, **k_):
        res = orig(*a_, **k_)
        if (a_[6] if len(a_) > 6 else k_.get("role")) == role:
            got.extend(res)
        return res
    _lv._phase_spans = rec
    try:
        await run_windows(key, [a, b], chain=False)
    finally:
        _lv._phase_spans = orig
    sp = sorted(got, key=lambda s: (s["_e"] - s["_s"]).total_seconds(), reverse=True)
    tot = sum((s["_e"] - s["_s"]).total_seconds() for s in got)
    vals = [p[1] for s in got for p in s["_pts"] if p[1] is not None]
    print(f"      участков ряда ≥ порога: {len(got)}, сумма {tot:.0f} с, макс. {max(vals) if vals else 0:.2f} %; "
          "самые длинные: " + ", ".join(f"{f(s['_s'])} {(s['_e'] - s['_s']).total_seconds():.0f} с"
                                         for s in sp[:top]))


async def ph(conn):
    print("\n══ 29.09 (Сининда): перекос фаз и ток нейтрали по кластерам ══")
    # У ДГУ №2 в проекте одна строка «13:52:39–15:58:29» — это нейтраль;
    # перекос дошёл до 20 % на пакет позже — допуск начала 30 с
    exp = {("ДГУ №1", "NEUTRAL_CURRENT"): (T(29, 13, 47, 22, 9), T(29, 16, 10, 22, 9), 1.0),
           ("ДГУ №1", "PHASE_CURRENT_SPREAD"): (T(29, 13, 52, 52, 9), T(29, 16, 10, 22, 9), 1.0),
           ("ДГУ №2", "NEUTRAL_CURRENT"): (T(29, 13, 52, 39, 9), T(29, 15, 58, 29, 9), 1.0),
           ("ДГУ №2", "PHASE_CURRENT_SPREAD"): (T(29, 13, 52, 39, 9), T(29, 15, 58, 29, 9), 30.0)}
    for key in ("ДГУ №1", "ДГУ №2"):
        new, old = await both(key, [D(29, 9), D(30, 9)], chain=False)
        db = await db_eps(conn, key, D(29, 9), D(30, 9))
        for sc in ("NEUTRAL_CURRENT", "PHASE_CURRENT_SPREAD", "NEGATIVE_SEQUENCE"):
            en, _ = print_case(f"{key} 29.09", sc, new, old, db)
            if (key, sc) in exp:
                s, e, tol = exp[(key, sc)]
                check(len(en) == 1 and abs((en[0]["s"] - s).total_seconds()) <= tol
                      and abs((en[0]["e"] - e).total_seconds()) <= 1.0,
                      f"{key} 29.09 {NAME[sc]}: {f(s)}–{f(e)} (стало: "
                      f"{'; '.join(f(x['s']) + '–' + f(x['e']) for x in en) or 'нет'})")


async def nl(conn):
    print("\n══ NO_LOAD_TOO_LONG: отрезки, начало — момент превышения 900 с (В-27.2) ══")
    for key, exp in (("ДГУ №1", [(T(12, 14, 26, 53), T(12, 14, 39, 2)), (T(12, 15, 3, 13), T(12, 15, 18, 23)),
                                 (T(12, 17, 0, 43), T(12, 18, 17, 2))]),
                     ("ДГУ №2", None)):
        new, old = await both(key, [D(11), D(12), D(13)])
        en, eo = print_case(f"{key} 12.10", "NO_LOAD_TOO_LONG", new, old, await db_eps(conn, key, D(12), D(13)))
        en = [x for x in en if x["s"] >= D(12)]
        tot = sum((x["e"] - x["s"]).total_seconds() for x in en)
        print(f"    стало итого: {len(en)} эп., {dur(tot)}")
        if exp:
            check(len(en) == 3 and all(_has(en, s, e) for s, e in exp),
                  f"{key} 12.10: три эпизода 14:26:53–14:39:02, 15:03:13–15:18:23, 17:00:43–18:17:02")
        else:
            check(len(en) == 3 and abs(tot - 102 * 60) <= 60, f"{key} 12.10: три эпизода, 1 ч 42 мин ({dur(tot)})")
    # ДЭС №3 30.09 и 01.10 — цепочкой с 29.09 (перенос через рез по аккумулятору)
    cuts = [D(29, 9), D(30, 9), D(1), D(2)]
    new, old = await both("ДЭС №3", cuts)
    en, eo = print_case("ДЭС №3 29.09–02.10 (цепочкой)", "NO_LOAD_TOO_LONG", new, old,
                        await db_eps(conn, "ДЭС №3", D(29, 9), D(2)))
    e30 = [x for x in en if D(30, 9) <= x["s"] < D(1)]
    e01 = [x for x in en if D(1) <= x["s"] < D(2)]
    check(len(e30) == 1 and abs((e30[0]["s"] - T(30, 9, 14, 49, 9)).total_seconds()) <= 2
          and abs((e30[0]["e"] - T(30, 15, 28, 29, 9)).total_seconds()) <= 2,
          f"ДЭС №3 30.09: с 09:14:49 по 15:28:29 с переносом "
          f"({'; '.join(f(x['s']) + '–' + f(x['e']) for x in e30) or 'нет'})")
    d01 = sorted(round((x["e"] - x["s"]).total_seconds() / 60) for x in e01)
    check(len(e01) == 2 and abs(d01[0] - 51) <= 1 and abs(d01[1] - 232) <= 1,
          f"ДЭС №3 01.10: два эпизода, 51 мин и 3 ч 52 мин ({d01} мин)")


async def days(conn):
    print("\n══ Дни задания: ни I₂, ни перекоса, ни нейтрали, ни NO_LOAD ══")
    for key, cuts in (("ДГУ №1", [D(16), D(17)]), ("ДГУ №2", [D(15), D(16)]),
                      ("ДЭС №3", [D(13), D(14), D(15), D(16), D(17)])):
        new, old = await both(key, cuts)
        n_new = {sc: len(episodes(new, sc)) for sc in KEYS}
        n_old = {sc: len(episodes(old, sc)) for sc in KEYS} if old is not None else None
        print(f"  {key} {f(cuts[0])} – {f(cuts[-1])}: стало {n_new}, было {n_old}")
        check(not any(n_new.values()) and (n_old is None or not any(n_old.values())),
              f"{key}: эпизодов этих правил нет ни в старом, ни в новом")


async def n60(conn):
    print("\n══ 60 дней 18.08–17.10 цепочкой (30 дней — 17.09–17.10) ══")
    cuts = [D(18, 8) + timedelta(days=i) for i in range(61)]
    cut30 = D(17, 9)
    tot_lost = tot_viol = 0
    for key in MACH:
        new, old = await both(key, cuts)
        secs = [w["sec"] for w in new]
        osecs = [w["sec"] for w in old or []]
        print(f"\n  {key}: окон {len(new)}, перепрогон новым кодом {sum(secs):.0f} с "
              f"(в среднем {sum(secs) / max(1, len(secs)):.1f} с на сутки)"
              + (f", старым — {sum(osecs):.0f} с ({sum(osecs) / max(1, len(osecs)):.1f} с)" if osecs else ""))
        for sc in KEYS:
            en = episodes(new, sc)
            eo = episodes(old, sc) if old is not None else []
            for lab, lo in (("60 дн", cuts[0]), ("30 дн", cut30)):
                en_ = [x for x in en if x["s"] >= lo]
                eo_ = [x for x in eo if x["s"] >= lo]
                print(f"    {NAME[sc]:13s} {lab}: было {len(eo_)} эп. / "
                      f"{dur(sum((x['e'] - x['s']).total_seconds() for x in eo_))}; стало {len(en_)} эп. / "
                      f"{dur(sum((x['e'] - x['s']).total_seconds() for x in en_))}")
            if sc == "NO_LOAD_TOO_LONG":
                # Нарушения (участки в окнах), которых старый код не видел ни в одном подсегменте
                viol = [(x["s"], x["e"]) for x in en]
                old_iv = [(x["s"], x["e"]) for x in eo]
                lost = [v for v in viol if not any(o[0] < v[1] and o[1] > v[0] for o in old_iv)]
                tot_lost += len(lost)
                tot_viol += len(viol)
                print(f"    без нагрузки: нарушений {len(viol)}, старый код не видел {len(lost)}")
            if sc == "NEGATIVE_SEQUENCE":
                for x in en:
                    print("      I₂ стало " + ep_line(x))
                for x in eo:
                    print("      I₂ было  " + ep_line(x))
    print(f"\n  Без нагрузки за 60 дней по трём машинам: нарушений {tot_viol}, старый код терял {tot_lost}")


async def b30(conn):
    """Неточный эпизод (без values.live), закрытый «вслепую», получает t_close
    = первый цикл после реза (09:00:01…09:03): это и ищем — с 16.10 (v5.1.21:
    эпизоды по событию и мост через рез; до неё движок закрывал всё на 09:00
    сам). Точный эпизод закрывается по концу кластера (t_close — раньше реза),
    по БД его не отличить от посева закрытого окна (та же запись на резе) —
    его случаи показывает симуляция (test_events_v5130, раздел 6)."""
    print("\n══ В-30.1: неточные эпизоды аналитики, закрытые в первые 3 мин после реза "
          "при разрыве связи на 09:00 (с 16.10) ══")
    rows = await conn.fetch(
        "SELECT e.id, e.router_sn, e.panel_id, e.scenario, e.t_open, e.t_close "
        "FROM alarm_episodes e WHERE e.source = 'analytics' AND e.t_close >= $1 "
        "AND e.close_reason = 'condition_cleared' AND e.t_open < e.t_close "
        "AND ((e.t_close AT TIME ZONE 'Europe/Moscow')::time > '09:00:00' "
        "     AND (e.t_close AT TIME ZONE 'Europe/Moscow')::time <= '09:03:00') "
        "ORDER BY e.t_close", D(16))
    hits = []
    for r in rows:
        cut = _u(r["t_close"]).astimezone(MSK).replace(hour=9, minute=0, second=0, microsecond=0)
        g = await conn.fetchval(
            "SELECT count(*) FROM data_gaps WHERE router_sn=$1 AND panel_id=$2 "
            "AND gap_start <= $3 AND (gap_end IS NULL OR gap_end >= $4)",
            r["router_sn"], r["panel_id"], cut.astimezone(U) - timedelta(seconds=60),
            cut.astimezone(U) + timedelta(seconds=120))
        print(f"  #{r['id']} {r['scenario']} {f(r['t_open'])} → {f(r['t_close'])}"
              + (" — разрыв связи на резе" if g else " — связь на резе была"))
        if g:
            hits.append(r)
    print(f"  закрытых в 09:00:01–09:03 с 16.10: {len(rows)}, из них при разрыве связи на резе — "
          f"{len(hits)}")


SECTIONS = [("i2", i2), ("ph", ph), ("nl", nl), ("days", days), ("n60", n60), ("b30", b30)]


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
    print("JSON " + json.dumps(_serial(wins), ensure_ascii=False, default=str))
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
    ap.add_argument("sections", nargs="*", help="разделы: i2 ph nl days n60 b30; по умолчанию все")
    args = ap.parse_args()
    if args.emit_root:
        return await _emit(args)
    if args.dsn_file:
        dsn = Path(args.dsn_file).read_text(encoding="utf-8").strip()
        src_dsn = dsn
        os.environ["_CHK5130_DSNFILE"] = str(Path(args.dsn_file).resolve())
    else:
        from config import settings
        dsn, src_dsn = settings.analytics_db_url, settings.source_db_url
        if args.old_root:
            print("--old-root требует --dsn-file")
            return 2
    _DSN = dsn
    _OLD_ROOT = str(Path(args.old_root).resolve()) if args.old_root else None
    print("Сверка v5.1.30 на реальных данных. Соединение только для чтения, запись НЕ производится.")
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
                # Долгий раздел n60 (час перепрогона) — соединение успевает закрыться
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
