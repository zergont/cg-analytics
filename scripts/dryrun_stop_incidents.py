# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""Сухой прогон аварийной ветки СТОПа на боевой истории (v4.9.77).

Отвечает на вопрос «что получится, когда авария пройдёт через новую модель»,
не дожидаясь аварии и ничего не записывая. Для каждого стоп-периода,
помеченного EMERGENCY, строит ровно то, что построил бы движок при закрытии
сегмента: акт «Следователя» и его ленту. Для простых стопов — хронологию
стоянки. Печатает результат и проверяет инварианты:

  * у каждого закрытого EMERGENCY акт строится (иначе авария осталась бы
    без разбора — это был исходный дефект);
  * лента акта не пуста;
  * голова аварии закрывается причиной SHUTDOWN_CLEARED, то есть рез встал
    по снятию маски, а не по краю окна;
  * на этой причине сработает строка MTTR «Авария снята».

Флаг stop_kind не читается: классификация вызывается напрямую, поэтому видно
и те машины, где новая модель ещё не включена.

Записи в БД НЕ ПРОИЗВОДИТСЯ — только чтение.

Запуск:
  py -3 scripts/dryrun_stop_incidents.py --days 30
  py -3 scripts/dryrun_stop_incidents.py --router 1126246373 --panel 1 --days 45
  py -3 scripts/dryrun_stop_incidents.py --from 2026-09-16 --to 2026-09-18 -v
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncpg  # noqa: E402

from analytics import binding, source as asrc  # noqa: E402
from analytics.classifier import build_stop_incident  # noqa: E402
from analytics.reconstructor import build_segment_chronology  # noqa: E402
from analytics.segmenter import _classify_stop_periods  # noqa: E402
from config import settings  # noqa: E402
from db import analytics as db_analytics  # noqa: E402
from online import db as online_db  # noqa: E402

_CHARACTER_RU = {
    "immediate": "немедленный",
    "controlled": "через охлаждение",
    "unknown": "не определён",
}


def _fmt(ts) -> str:
    if ts is None:
        return "открыт"
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)
    return ts.strftime("%d.%m %H:%M:%S")


def _dur(a: datetime, b: datetime | None, tt: datetime) -> str:
    secs = int(((b or tt) - a).total_seconds())
    if secs < 60:
        return f"{secs}с"
    if secs < 3600:
        return f"{secs // 60}м {secs % 60}с"
    return f"{secs // 3600}ч {(secs % 3600) // 60}м"


def _event_line(e: dict, fault_ref) -> str:
    ts = _fmt(e.get("ts"))
    if e.get("kind") == "fault":
        name = e.get("name") or f"бит {e.get('addr')}/{e.get('bit')}"
        sev = e.get("severity") or "?"
        tail = f", снято в {_fmt(e['end'])}" if e.get("end") else ", ещё активна"
        code = ""
        if fault_ref is not None:
            try:
                rec = fault_ref.lookup_by_name(e.get("name") or "", e.get("severity"))
            except Exception:
                rec = None
            if rec and rec.get("code"):
                code = f" [код {rec['code']}]"
        return f"      {ts}  * {name}{code}  ({sev}{tail})"
    what = e.get("name") or f"регистр {e.get('addr')}"
    val = e.get("label") or e.get("value")
    return f"      {ts}  > {what}: {val}"


def _render_events(events: list[dict], fault_ref, verbose: bool,
                   around: datetime | None = None, limit: int = 12) -> list[str]:
    """Строки ленты. При усечении показываем ОКРЕСТНОСТЬ момента останова.

    Наивные первые N бесполезны: у длинной ленты это вся преамбула, а сам
    останов и маска, сделавшая сегмент аварийным, оказываются за обрезом.
    """
    if verbose or len(events) <= limit:
        return [_event_line(e, fault_ref) for e in events]
    if around is None:
        return ([_event_line(e, fault_ref) for e in events[:limit]]
                + [f"      ... ещё {len(events) - limit} (полностью — ключ -v)"])

    def _ts(e):
        raw = e.get("ts")
        return datetime.fromisoformat(raw) if isinstance(raw, str) else raw

    before = [e for e in events if _ts(e) and _ts(e) < around]
    after = [e for e in events if not _ts(e) or _ts(e) >= around]
    head = before[-3:] if len(before) > 3 else before
    tail = after[: max(0, limit - len(head))]
    out: list[str] = []
    if len(before) > len(head):
        out.append(f"      ... {len(before) - len(head)} до останова "
                   f"(полностью — ключ -v)")
    out += [_event_line(e, fault_ref) for e in head + tail]
    rest = len(after) - len(tail)
    if rest > 0:
        out.append(f"      ... ещё {rest} после (полностью — ключ -v)")
    return out


async def _current_rows(conn, sn: str, et: str, pid: int,
                        tf: datetime, tt: datetime) -> list[dict]:
    rows = await conn.fetch(
        """
        SELECT id, t_start, t_end, run_state, cause_close,
               characteristics_json->>'stop_kind' AS stop_kind,
               incident_json IS NOT NULL AS has_incident
        FROM auto_segments
        WHERE router_sn=$1 AND equip_type=$2 AND panel_id=$3
          AND t_start < $5 AND (t_end IS NULL OR t_end > $4)
        ORDER BY t_start
        """, sn, et, pid, tf, tt)
    return [dict(r) for r in rows]


async def _run_machine(conn, obs: dict, tf: datetime, tt: datetime,
                       verbose: bool) -> dict:
    sn, et, pid = obs["router_sn"], obs["equip_type"], obs["panel_id"]
    label = f"{sn}/{et}/{pid}"
    stat: dict = {"emergency": 0, "acts": 0, "no_act": 0, "empty_chrono": 0,
                  "cut_ok": 0, "simple_with_log": 0, "problems": []}

    bnd = await db_analytics.get_equipment_binding(sn, et, pid) or {}
    try:
        cfg = binding.build_config(
            settings.knowledge_base_path,
            controller_id=bnd.get("controller_id"),
            engine_id=bnd.get("engine_id"),
            kb_path=bnd.get("kb_path"),
        )
    except Exception as exc:
        print(f"{label}: привязка конфига не собралась ({exc}) — пропуск")
        return stat
    try:
        fault_ref = binding.build_fault_ref(
            settings.knowledge_base_path,
            controller_id=bnd.get("controller_id"),
            engine_id=bnd.get("engine_id"),
            kb_path=bnd.get("kb_path"),
        )
    except Exception:
        fault_ref = None

    enum_periods = await asrc.get_enum_periods(
        sn, et, pid, tf, tt, addrs=asrc.enum_read_addrs(cfg))
    fault_periods = await asrc.get_fault_periods(
        sn, et, pid, tf, tt, fault_addrs=cfg.whitelist_fault)
    rs_periods = sorted(
        (p for p in enum_periods if p["addr"] == 40011),
        key=lambda p: p["state_start"])

    parts = _classify_stop_periods(rs_periods, fault_periods, cfg, tt)
    stops = [p for p in parts if int(p["value"]) == 0]
    emergencies = [p for p in stops if p.get("stop_kind") == "EMERGENCY"]

    cur = await _current_rows(conn, sn, et, pid, tf, tt)
    cur_acts = sum(1 for r in cur if r["has_incident"])

    print(f"\n=== {label} ({obs.get('name') or '-'}) ===")
    print(f"  сейчас в БД: сегментов {len(cur)}, из них с актом {cur_acts}")
    print(f"  найдено аварийных стопов: {len(emergencies)}")

    for p in emergencies:
        stat["emergency"] += 1
        t_from = p["state_start"]
        t_to = p.get("state_end")
        cc = p.get("cause_close")
        print(f"  --- АВАРИЙНЫЙ СТОП {_fmt(t_from)} - {_fmt(t_to)}  "
              f"({_dur(t_from, t_to, tt)}), close={cc or '-'}")

        if t_to is None:
            print("      ещё не закрыт — акт построится при закрытии")
            continue
        if cc == "SHUTDOWN_CLEARED":
            stat["cut_ok"] += 1
            print("      рез по снятию маски, MTTR даст «Авария снята»")
        else:
            print(f"      ВНИМАНИЕ: рез не по снятию маски (close={cc or '-'})")

        inc = build_stop_incident(
            enum_periods, fault_periods, t_from, t_to, cfg,
            stop_kind="EMERGENCY",
        )
        if inc is None:
            stat["no_act"] += 1
            stat["problems"].append(f"{label} {_fmt(t_from)}: акт НЕ построен")
            print("      ДЕФЕКТ: акт не построен")
            continue
        stat["acts"] += 1

        ch = inc.get("character") or {}
        votes = (ch.get("immediate_votes") or []) + (ch.get("controlled_votes") or [])
        print(f"      характер: "
              f"{_CHARACTER_RU.get(ch.get('character'), ch.get('character'))}"
              f" (уверенность {ch.get('confidence')})")
        if votes:
            print(f"      признаки: {'; '.join(votes)}")

        events = inc.get("chronology") or []
        if not events:
            stat["empty_chrono"] += 1
            stat["problems"].append(f"{label} {_fmt(t_from)}: лента акта пуста")
            print("      ДЕФЕКТ: лента акта пуста")
            continue
        print(f"      событий в ленте: {len(events)}")
        for line in _render_events(events, fault_ref, verbose, around=t_from):
            print(line)

    # Хронология стоянки: показываем самые насыщенные простые стопы
    simple = [p for p in stops
              if p.get("stop_kind") != "EMERGENCY" and p.get("state_end")]
    logs = []
    for p in simple:
        ch = build_segment_chronology(
            enum_periods, fault_periods, p["state_start"], p["state_end"], cfg)
        if ch and ch.get("chronology"):
            logs.append((p, ch))
    stat["simple_with_log"] = len(logs)
    print(f"  простых стопов с непустой хронологией: {len(logs)} из {len(simple)}")
    for p, ch in sorted(logs, key=lambda x: -len(x[1]["chronology"]))[:2]:
        events = ch["chronology"]
        print(f"  --- СТОЯНКА {_fmt(p['state_start'])} - {_fmt(p['state_end'])}  "
              f"({_dur(p['state_start'], p['state_end'], tt)}), событий {len(events)}")
        for line in _render_events(events, fault_ref, verbose, limit=10):
            print(line)

    return stat


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--router")
    ap.add_argument("--panel", type=int)
    ap.add_argument("--from", dest="tf")
    ap.add_argument("--to", dest="tt")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="печатать ленты целиком")
    args = ap.parse_args()

    if args.tf and args.tt:
        tf = datetime.fromisoformat(args.tf).replace(tzinfo=timezone.utc)
        tt = datetime.fromisoformat(args.tt).replace(tzinfo=timezone.utc)
    else:
        tt = datetime.now(timezone.utc)
        tf = tt - timedelta(days=args.days)

    print(f"Окно: {_fmt(tf)} - {_fmt(tt)} (UTC). Запись НЕ производится.")

    await asrc.init_source_pool()
    conn = await asyncpg.connect(settings.analytics_db_url)
    try:
        observations = await online_db.list_observations()
        if args.router:
            observations = [o for o in observations
                            if o["router_sn"] == args.router
                            and (args.panel is None or o["panel_id"] == args.panel)]

        total = {"emergency": 0, "acts": 0, "no_act": 0, "empty_chrono": 0,
                 "cut_ok": 0, "simple_with_log": 0}
        problems: list[str] = []
        for obs in observations:
            st = await _run_machine(conn, obs, tf, tt, args.verbose)
            for k in total:
                total[k] += st[k]
            problems += st["problems"]

        print(f"\n{'=' * 60}\nИТОГО по {len(observations)} машинам:")
        print(f"  аварийных стопов найдено:      {total['emergency']}")
        print(f"  из них рез по снятию маски:    {total['cut_ok']}")
        print(f"  актов построено:               {total['acts']}")
        print(f"  простых стоянок с хронологией: {total['simple_with_log']}")
        if problems:
            print(f"\n  ПРОБЛЕМЫ ({len(problems)}):")
            for pr in problems:
                print(f"    - {pr}")
            return 1
        print("\n  Инварианты выполнены: у каждого закрытого аварийного стопа "
              "есть акт с непустой лентой.")
    finally:
        await conn.close()
        await asrc.close_source_pool()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
