# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""Сухой прогон новой модели СТОПа на боевых данных. ТОЛЬКО ЧТЕНИЕ.

Сравнивает нарезку, которая лежит в auto_segments прямо сейчас, с той, что
даст `_classify_stop_periods` на тех же исходных периодах. Ничего не пишет:
открывает source-пул на чтение и читает auto_segments отдельным соединением.

Отвечает на вопрос «что изменится в календаре» ДО того, как оно изменится.

Запуск на сервере:
    .venv/bin/python scripts/dryrun_stop_kinds.py                 # весь парк, 30 суток
    .venv/bin/python scripts/dryrun_stop_kinds.py --days 2        # короче окно
    .venv/bin/python scripts/dryrun_stop_kinds.py \
        --router 1126246373 --panel 1 \
        --from "2026-09-16 09:30" --to "2026-09-16 10:30"         # одна цепочка подробно
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
from analytics.segmenter import _classify_stop_periods  # noqa: E402
from config import settings  # noqa: E402
from db import analytics as db_analytics  # noqa: E402
from online import db as online_db  # noqa: E402

_RS = {0: "Стоп", 1: "Задержка пуска", 2: "Прогрев", 3: "Работа",
       4: "Разгрузка", 5: "Охлаждение", 6: "Переход на ХХ"}


def _fmt(ts: datetime | None) -> str:
    return ts.strftime("%d.%m %H:%M:%S") if ts else "открыт"


def _dur(a: datetime, b: datetime | None, tt: datetime) -> str:
    secs = int(((b or tt) - a).total_seconds())
    if secs < 60:
        return f"{secs}с"
    if secs < 3600:
        return f"{secs // 60}м {secs % 60}с"
    return f"{secs // 3600}ч {(secs % 3600) // 60}м"


async def _current_segments(conn, sn: str, et: str, pid: int,
                            tf: datetime, tt: datetime) -> list[dict]:
    rows = await conn.fetch(
        """
        SELECT t_start, t_end, run_state, cause_close,
               characteristics_json->>'cause_open' AS cause_open
        FROM auto_segments
        WHERE router_sn=$1 AND equip_type=$2 AND panel_id=$3
          AND t_start < $5 AND (t_end IS NULL OR t_end > $4)
        ORDER BY t_start
        """, sn, et, pid, tf, tt)
    return [dict(r) for r in rows]


async def _run_machine(conn, obs: dict, tf: datetime, tt: datetime,
                       verbose: bool) -> tuple[int, int, int]:
    sn, et, pid = obs["router_sn"], obs["equip_type"], obs["panel_id"]
    label = f"{sn}/{et}/{pid}"

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
        return 0, 0, 0

    enum_periods = await asrc.get_enum_periods(
        sn, et, pid, tf, tt, addrs=asrc.enum_read_addrs(cfg))
    fault_periods = await asrc.get_fault_periods(
        sn, et, pid, tf, tt, fault_addrs=cfg.whitelist_fault)

    rs_periods = sorted(
        (p for p in enum_periods if p["addr"] == 40011),
        key=lambda p: p["state_start"])
    new = _classify_stop_periods(rs_periods, fault_periods, cfg, tt)
    new_stops = [p for p in new if int(p["value"]) == 0]
    emergency = [p for p in new_stops if p.get("stop_kind") == "EMERGENCY"]

    cur = await _current_segments(conn, sn, et, pid, tf, tt)
    cur_stops = [r for r in cur if r["run_state"] == 0]

    print(f"\n=== {label} ({obs.get('name') or '—'}) ===")
    print(f"  сейчас в БД:  стоп-сегментов {len(cur_stops)}  (всего {len(cur)})")
    print(f"  станет:       стоп-кусков   {len(new_stops)}  "
          f"(из них аварийных {len(emergency)})")

    short = [r for r in cur_stops
             if r["t_end"] and (r["t_end"] - r["t_start"]).total_seconds() < 60]
    if short:
        print(f"  вырожденных сейчас (<60с): {len(short)} — исчезнут")

    if verbose:
        print("  --- сейчас ---")
        for r in cur_stops:
            print(f"    {_fmt(r['t_start'])} – {_fmt(r['t_end'])}  "
                  f"{_dur(r['t_start'], r['t_end'], tt):>9}  "
                  f"{_RS.get(r['run_state'], r['run_state'])}  "
                  f"open={r['cause_open'] or '—'} close={r['cause_close'] or '—'}")
        print("  --- станет ---")
        for p in new_stops:
            kind = "АВАРИЙНЫЙ СТОП" if p.get("stop_kind") == "EMERGENCY" else "Стоп"
            print(f"    {_fmt(p['state_start'])} – {_fmt(p.get('state_end'))}  "
                  f"{_dur(p['state_start'], p.get('state_end'), tt):>9}  "
                  f"{kind}  close={p.get('cause_close') or '—'}")

    return len(cur_stops), len(new_stops), len(emergency)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--router")
    ap.add_argument("--panel", type=int)
    ap.add_argument("--from", dest="tf")
    ap.add_argument("--to", dest="tt")
    args = ap.parse_args()

    if args.tf and args.tt:
        tf = datetime.fromisoformat(args.tf).replace(tzinfo=timezone.utc)
        tt = datetime.fromisoformat(args.tt).replace(tzinfo=timezone.utc)
    else:
        tt = datetime.now(timezone.utc)
        tf = tt - timedelta(days=args.days)

    print(f"Окно: {_fmt(tf)} — {_fmt(tt)} (UTC). Запись НЕ производится.")

    await asrc.init_source_pool()
    conn = await asyncpg.connect(settings.analytics_db_url)
    try:
        observations = await online_db.list_observations()
        if args.router:
            observations = [o for o in observations
                            if o["router_sn"] == args.router
                            and (args.panel is None or o["panel_id"] == args.panel)]
        verbose = bool(args.router) or len(observations) == 1

        tot_cur = tot_new = tot_emg = 0
        for obs in observations:
            c, n, e = await _run_machine(conn, obs, tf, tt, verbose)
            tot_cur += c
            tot_new += n
            tot_emg += e

        print(f"\n{'=' * 52}\nИТОГО по {len(observations)} машинам:")
        print(f"  стоп-сегментов сейчас: {tot_cur}")
        print(f"  станет:                {tot_new}   "
              f"(разница {tot_new - tot_cur:+d})")
        print(f"  из них аварийных:      {tot_emg}")
    finally:
        await conn.close()
        await asrc.close_source_pool()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
