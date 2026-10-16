# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""v5.1.21: сверка на реальных окнах (раздел 9 плана). ТОЛЬКО ЧТЕНИЕ.

Окна #35876, #35890 (ДГУ №1) и #35872, #35891 (ДЭС №3). Данные — тем же кодом
движка, что на проде (_run_segment: _load_data, _load_seed, синтетический хвост,
segment()); шаг датчика давления — по истории 30 дней, как _refresh_sensor_steps.
Закрытие окна — _collect_and_enrich_detections с подменой записи: вставки
эпизодов только запоминаются, в БД ничего не пишется. Живой цикл моделируется
так, как он шёл бы на проде: по кластерам, идущим на резе, эпизоды открыты
(в памяти и в БД — с until). Сводка — та же цепочка, что _build_summary_md_for
(closing): фильтр окна, хвосты дебаунса, синтетика, build_summary_parts; эпизоды
панели — из alarm_episodes (чтение).

Сверяются цифры раздела 1 docs/plans/design_v5121_episodes.md: у #35890 нет
RPM_UNDERSPEED и 10 строк сбоя датчика, провал 19:47:50 у #35891, близость и
паспорт у #35872 (ЗАМЕЧАНИЯ — 3 вида / 7 эпизодов, воздействие аналитики 1 ч 24 мин).

Фикстур в репозитории нет — 5 МБ строк на сегмент; синтетические аналоги —
раздел 9 scripts/test_episodes_v5121.py.

Запуск (соединения открываются с default_transaction_read_only):
    .venv/bin/python scripts/check_v5121_data.py                 # все окна, БД из config.yml
    py -3 scripts/check_v5121_data.py --dsn-file .dsn 35890 35872  # одна БД по DSN, выбранные окна
Код возврата 1 — есть расхождения.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import asyncpg  # noqa: E402

from analytics import source as _src  # noqa: E402
from analytics.config import AnalyticsConfig  # noqa: E402
from analytics.contract import CokingRisk  # noqa: E402
from analytics.metrics import estimate_sensor_step  # noqa: E402
from analytics.serializer import _append_detections, build_summary_parts  # noqa: E402
from corpus.preprocessor import _fmt_detections_hierarchy  # noqa: E402
from online import db as online_db  # noqa: E402
from online import engine as E  # noqa: E402

MSK = ZoneInfo("Europe/Moscow")
U = timezone.utc
KB = ROOT / "knowledge_base"
RO = {"default_transaction_read_only": "on"}

# Машины: (router_sn, equip_type, panel_id, двигатель KB)
MACH = {"ДГУ №1": ("6003790403", "pcc", 1, "cummins_kta50"),
        "ДЭС №3": ("1126246373", "pcc", 1, "cummins_qsx15")}


def M(d: int, h: int, mi: int, s: int = 0) -> datetime:
    """Метка МСК октября 2026 → UTC."""
    return datetime(2026, 10, d, h, mi, s, tzinfo=MSK).astimezone(U)


WINDOWS = {
    "35876": ("ДГУ №1", M(13, 9, 0), M(14, 9, 0)),
    "35890": ("ДГУ №1", M(14, 9, 0), M(15, 9, 0)),
    "35872": ("ДЭС №3", M(13, 14, 59, 49), M(14, 9, 0)),
    "35891": ("ДЭС №3", M(14, 9, 0), M(15, 9, 0)),
}

_fails: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  OK   " if cond else "  FAIL ") + msg)
    if not cond:
        _fails.append(msg)


def hm(ts) -> str:
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)
    return ts.astimezone(MSK).strftime("%H:%M:%S")


def dur(sec: float) -> str:
    from analytics.labels import fmt_duration
    return fmt_duration(sec)


class _NoWrite:
    """Подмена записи online.db на время посева: вставки только запоминаются."""

    def __init__(self, ov: list[dict]):
        self.ov, self.ins = ov, []

    async def existing_episode_onsets(self, *a, **k):
        return set()

    async def get_episodes_overlapping(self, *a, **k):
        return list(self.ov)

    async def insert_closed_episode(self, *a, **k):
        self.ins.append(k)
        return -len(self.ins)

    async def count_episodes_batch(self, *a, **k):
        return {}


_NAMES = ("existing_episode_onsets", "get_episodes_overlapping", "insert_closed_episode",
          "count_episodes_batch")


async def _sensor_step(cfg, sn, et, p) -> None:
    addr = cfg.role_to_addr("OIL_PRESS")
    if not addr:
        return
    hist = await _src.get_value_histogram(sn, et, p, addr, datetime.now(U) - timedelta(days=30))
    step = estimate_sensor_step(hist)
    if step is not None:
        cfg.set_sensor_step("OIL_PRESS", step, "history")


async def _panel_eps(conn, sn, et, p, a, b) -> list[dict]:
    rows = await conn.fetch("""
        SELECT * FROM alarm_episodes
        WHERE router_sn = $1 AND equip_type = $2 AND panel_id = $3 AND source = 'panel'
          AND t_open < $5 AND (t_close IS NULL OR t_close > $4)
        ORDER BY t_open
    """, sn, et, p, a, b)
    return [dict(r) for r in rows]


async def _close_and_summary(seg, is_last: bool, t_to: datetime, gaps, cfg, sn, et, p, panel):
    """Посев закрытия и сводка сегмента — как _close_window, без записи в БД."""
    s0 = E._as_utc(seg.t_start)
    s1 = t_to if is_last else E._as_utc(seg.t_end)
    clusters = E._segment_clusters(seg)
    # Живой цикл вёл эпизоды по кластерам, идущим на резе: открыты, until — край окна
    mem: dict[str, dict] = {}
    open_rows: list[dict] = []
    for (k, s), c in sorted(clusters.items(), key=lambda x: x[0][1]):
        if not (is_last and c["ongoing"]):
            continue
        rid = 900000 + len(open_rows)
        open_rows.append({"id": rid, "scenario": c["scenario"], "source": "analytics", "severity": c["severity"],
                          "gate_suppressed": False, "t_open": s, "t_close": None,
                          "active_sec": (c["end"] - s).total_seconds(), "blind_sec": c["blind_sec"],
                          "open_values_json": {"live": {"until": c["end"].isoformat()}}})
        mem[k] = {"id": rid, "exact": True, "until": c["end"], "t_open": s, "miss": 0,
                  "first_miss_ts": None, "severity": c["severity"]}
    fake = _NoWrite(open_rows)
    orig = {n: getattr(online_db, n) for n in _NAMES}
    for n in _NAMES:
        setattr(online_db, n, getattr(fake, n))
    try:
        panel_keys = {E._episode_key(e) for e in panel if e.get("t_close") is None}
        await E._collect_and_enrich_detections(
            seg, sn, et, p, s1, cfg, s0, open_keys=set(mem) | panel_keys, seeded_out=set(), gaps=gaps,
            live_episodes=mem, carry_end=E._seed_carry(is_last and seg.run_state == 3, t_to,
                                                       t_to - timedelta(seconds=30), 30))
    finally:
        for n, f in orig.items():
            setattr(online_db, n, f)
    seeded = [{"source": "analytics", "scenario": k["scenario"], "severity": k["severity"],
               "gate_suppressed": False, "t_open": k["t_open"], "t_close": k["t_close"],
               "active_sec": k["active_sec"], "blind_sec": k["blind_sec"],
               "open_values_json": k["open_values"]} for k in fake.ins]
    eps = E._filter_window_episodes(seeded + open_rows + [dict(e) for e in panel], s0, s1, tail_sec=120)
    live_keys = E._live_alert_keys(seg) if is_last else set()
    eps = E._drop_debounce_tails(eps, [seg], mem, live_keys)
    syn = E._live_new_episodes([seg], eps, live_keys, s1)
    eps = E._drop_same_onset(eps, syn) + syn
    parts = build_summary_parts([seg], episodes=eps, tz=MSK, trip_roles=cfg.trip_snapshot_roles,
                                t_from=s0, t_to=s1)
    return clusters, fake.ins, parts


def _print_clusters(clusters) -> None:
    by: dict[str, list] = {}
    for (k, _), c in sorted(clusters.items(), key=lambda x: x[0][1]):
        by.setdefault(k, []).append(c)
    for k, cl in by.items():
        per = sum((c["end"] - c["start"]).total_seconds() for c in cl)
        print(f"    {k}: {len(cl)} кл., воздействие {dur(per)}")
        for c in cl:
            lv = (c["values"].get("live") or {}).get("clusters") or [{}]
            roles = ",".join(sorted((lv[0].get("roles") or {}).keys()))
            print(f"      {hm(c['start'])}–{hm(c['end'])}{' идёт' if c['ongoing'] else ''}"
                  f"  {dur((c['end'] - c['start']).total_seconds())}  чистое {dur(lv[0].get('viol_sec') or 0)}"
                  + (f"  роли {roles}" if roles else ""))


def _iv(clusters, key) -> list[tuple[str, str, bool]]:
    return [(hm(c["start"]), hm(c["end"]), c["ongoing"])
            for (k, _), c in sorted(clusters.items(), key=lambda x: x[0][1]) if k == key]


def _grp(parts, sc) -> dict:
    r = [g for g in parts["remarks"] if g.get("scenario") == sc]
    return r[0] if r else {}


def _dets(segs, sc):
    return [d for s in segs for sub in s.subsegments for d in sub.detections if d.scenario == sc]


def _rpm_min(segs):
    v = [sub.characteristics.get("RPM", {}).get("min") for s in segs for sub in s.subsegments
         if isinstance(sub.characteristics.get("RPM"), dict)]
    v = [x for x in v if x is not None]
    return min(v) if v else None


def _glitch(segs) -> list[dict]:
    return [c for s in segs for c in (s.sequence_checks or []) if c.get("check") == "source_rpm_glitch"]


async def run(key: str, conn) -> None:
    name, a, b = WINDOWS[key]
    sn, et, p, engine = MACH[name]
    cfg = AnalyticsConfig.from_pair(KB, "pcc3300", engine)
    await _sensor_step(cfg, sn, et, p)
    segs, _inc, gaps, _chr = await E._run_segment(sn, et, p, "", a, b, cfg, CokingRisk())
    panel = await _panel_eps(conn, sn, et, p, a, b)
    print(f"\n{'#' * 78}\n#{key} {name} {a.astimezone(MSK):%d.%m %H:%M:%S} – {b.astimezone(MSK):%d.%m %H:%M} МСК; "
          f"сегментов {len(segs)}, data_gaps {len(gaps)}, эпизодов панели {len(panel)}")
    all_cl: dict = {}
    summaries = []
    reports: list[str] = []
    blocks: list[str] = []
    for i, seg in enumerate(segs):
        is_last = i == len(segs) - 1
        cl, ins, parts = await _close_and_summary(seg, is_last, b, gaps, cfg, sn, et, p, panel)
        all_cl.update(cl)
        summaries.append((seg, parts))
        print(f"  сегмент {seg.t_start[:19]} RS={seg.run_state} ({seg.cause_open}): кластеры")
        _print_clusters(cl)
        print(f"    посев: {[(k['scenario'], hm(k['t_open']), hm(k['t_close'])) for k in ins]}")
        v = parts["verdict"]
        print(f"    сводка: {v['title']}")
        for g in parts["remarks"]:
            print(f"      - {g.get('name')}: {g.get('count')} эп., период {dur(g.get('period_sec', 0) or 0)}"
                  f"{', на конец' if g.get('at_end') else ''}"
                  f"{', не подтв. ' + str(g.get('unconfirmed')) if g.get('unconfirmed') else ''}")
        for c in parts["failed_checks"]:
            print(f"      ❗ {c['details'][:160]}")
        print("    отчёт, строки «Нарушение»:")
        lines: list[str] = []
        _append_detections(lines, seg, 1, str, tz=MSK)
        for j, ln in enumerate(lines):
            if "Нарушение" in ln:
                head = next((x for x in reversed(lines[:j]) if x.startswith("- ")), "")
                print(f"      {head[2:70]}\n        {ln.strip()}")
                reports.append(ln)
        print("    блок 1 заключения:")
        blk = _fmt_detections_hierarchy(seg.to_dict())
        blocks.append(blk)
        for ln in blk.splitlines():
            print("      " + ln)
    run_seg, run_parts = next(((s, pp) for s, pp in summaries if s.run_state == 3), summaries[-1])
    gl = _glitch(segs)
    print("  сверка с планом:")
    if key == "35876":
        check(not _dets(segs, "RPM_UNDERSPEED"), "нет RPM_UNDERSPEED")
        check(any(hm(x["t"]) == "03:34:53" for c in gl for x in c.get("samples") or []),
              f"03:34:53 вырезан как сбой: {[hm(x['t']) for c in gl for x in c.get('samples') or []]}")
        check(_rpm_min(segs) == 1483.0, f"минимум оборотов в таблице 1483: {_rpm_min(segs)}")
        check(run_parts["verdict"]["level"] == "НОРМА" and not _grp(run_parts, "RPM_UNDERSPEED"),
              f"НОРМА, просадки в замечаниях нет: {run_parts['verdict']['title']}")
    elif key == "35890":
        want = ["04:42:32", "04:53:52", "04:55:12", "04:56:32", "04:57:03", "04:58:03", "05:53:22",
                "08:34:13", "08:44:02", "08:49:22"]
        got = [hm(x["t"]) for c in gl for x in c.get("samples") or []]
        check(not _dets(segs, "RPM_UNDERSPEED"), "нет RPM_UNDERSPEED (жалоба: «24 ч» от одиночных отсчётов)")
        check(got == want and gl and gl[0].get("passed") is False and gl[0].get("count") == 10,
              f"10 строк сбоя, проверка не пройдена: {got}")
        check(_rpm_min(segs) == 1484.0, f"минимум оборотов в таблице 1484 (при 49,7 Гц): {_rpm_min(segs)}")
        check(run_parts["verdict"]["level"] == "НОРМА"
              and any(c.get("check") == "source_rpm_glitch" for c in run_parts["failed_checks"]),
              f"НОРМА и строка «сбой датчика» в замечаниях: {run_parts['verdict']['title']}")
    elif key == "35872":
        lp = _iv(all_cl, "LIMIT_PROXIMITY")
        check(lp == [("17:43:10", "17:45:52", False), ("17:56:58", "17:57:16", False),
                     ("18:09:08", "18:12:25", False), ("07:44:32", "09:00:00", True)],
              f"близость — 4 кластера (пауза 666 с > N): {lp}")
        roles = {r for (k, _), c in all_cl.items() if k == "LIMIT_PROXIMITY"
                 for r in ((c["values"]["live"]["clusters"][0].get("roles")) or {})}
        check(roles == {"OIL_TEMP"}, f"только температура масла (ноль давления отсечён полом): {roles}")
        od = _iv(all_cl, "OIL_DILUTION")
        check(od == [("08:07:38", "08:09:08", False), ("08:58:47", "09:00:00", True)],
              f"паспорт — 2 кластера: {od}")
        check(all("live" in (d.values or {}) for d in _dets(segs, "OIL_DILUTION")),
              "комбинированной ветки OIL_DILUTION нет")
        g_lp, g_od = _grp(run_parts, "LIMIT_PROXIMITY"), _grp(run_parts, "OIL_DILUTION")
        # В2а (16.10): заголовок — виды замечаний; эпизодов 7 (1 + 4 + 2)
        check(run_parts["verdict"]["remarks_n"] == 3 and run_parts["verdict"].get("remarks_episodes_n") == 7,
              f"ЗАМЕЧАНИЯ — 3 вида / 7 эпизодов: {run_parts['verdict']['title']}")
        check(g_lp.get("count") == 4 and dur(g_lp.get("period_sec") or 0) == "1 ч 21 мин" and g_lp.get("at_end"),
              f"близость — 4 эп., 1 ч 21 мин, активна на конец: {g_lp.get('count')}, "
              f"{dur(g_lp.get('period_sec') or 0)}")
        check(g_od.get("count") == 2 and dur(g_od.get("period_sec") or 0) == "2 мин 43 с",
              f"паспорт — 2 эп., 2 мин 43 с: {g_od.get('count')}, {dur(g_od.get('period_sec') or 0)}")
        tot = (g_lp.get("period_sec") or 0) + (g_od.get("period_sec") or 0)
        check(dur(tot) == "1 ч 23 мин" or dur(tot) == "1 ч 24 мин",
              f"воздействие аналитики ≈ 1 ч 24 мин: {dur(tot)}")
        check(any("Нарушение (4 эпизода): 13.10 17:43:10–17:45:52" in r and "(чистое время 1 ч 09 мин)" in r
                  for r in reports), "отчёт: «Нарушение (4 эпизода) … (чистое время 1 ч 09 мин)»")
        check(any("Температура масла у уставки останова панели — 4 срабатывания, суммарно 1 ч 09 мин" in b
                  for b in blocks), "блок 1: близость масла — 4 срабатывания, суммарно 1 ч 09 мин")
    elif key == "35891":
        rpm = _iv(all_cl, "RPM_UNDERSPEED")
        check(rpm == [("19:47:50", "19:47:59", False)], f"провал 19:47:50, 9 с: {rpm}")
        d = _dets(segs, "RPM_UNDERSPEED")
        check(bool(d) and all(x.values.get("rpm_min") == 1483 for x in d), f"1483 об/мин: {[x.trigger for x in d]}")
        lp = [c for (k, _), c in all_cl.items() if k == "LIMIT_PROXIMITY"]
        per = sum((c["end"] - c["start"]).total_seconds() for c in lp)
        check(len(lp) == 10 and dur(per).startswith("4 ч 04 мин"), f"близость — 10 кластеров, 4 ч 04 мин: "
              f"{len(lp)}, {dur(per)}")
        od = _iv(all_cl, "OIL_DILUTION")
        check(("09:00:00", "09:00:08", False) in od and ("09:22:11", "09:30:08", False) in od,
              f"паспорт: перенос 09:00:00–09:00:08 и 09:22:11–09:30:08: {od}")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn-file", help="DSN одной БД (источник и аналитика) — иначе config.yml")
    ap.add_argument("windows", nargs="*", help=f"окна: {', '.join(WINDOWS)} (по умолчанию все)")
    args = ap.parse_args()
    if args.dsn_file:
        dsn = Path(args.dsn_file).read_text(encoding="utf-8").strip()
        src_dsn = an_dsn = dsn
    else:
        from config import settings
        src_dsn, an_dsn = settings.source_db_url, settings.analytics_db_url
    print("Сверка v5.1.21 на реальных окнах. Соединения только для чтения, запись НЕ производится.")
    _src._pool = await asyncpg.create_pool(src_dsn, min_size=1, max_size=3, command_timeout=600,
                                           server_settings=RO)
    conn = await asyncpg.connect(an_dsn, server_settings=RO)
    try:
        for key in (args.windows or list(WINDOWS)):
            await run(key, conn)
    finally:
        await conn.close()
        await _src._pool.close()
        _src._pool = None
    if _fails:
        print(f"\nРАСХОЖДЕНИЙ: {len(_fails)}")
        for f in _fails:
            print(f"  • {f}")
        return 1
    print("\nВсе цифры раздела 1 плана сходятся.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
