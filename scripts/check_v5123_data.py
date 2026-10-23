# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""v5.1.23 «Пуск и обороты»: сверка на реальных данных (план
docs/plans/design_v5123_plus.md, раздел 2.1). ТОЛЬКО ЧТЕНИЕ — соединение с
default_transaction_read_only, запись не производится, к шлюзу не обращаемся.

Этап 1 (п.1, п.7, п.8):
- p1: задание частоты 40595 есть в history_rich у всех трёх машин за последние
  сутки; масштаб — уже Гц (value = raw × 0,001, как в register_map.jsonl KB:
  u32, multiplier 0.001, Hz), на номинале ~50, на стоянке 0; прирост строк
  whitelist (план: ДГУ №1 +1,8 %, ДГУ №2 +1,6 %, ДЭС №3 +0,09 %) — справочно;
- act: перепрогон окна ДГУ №1 16.10 09:20–09:40 кодом движка (_run_segment):
  в ленте актов #35931/#35941 колонка задания сразу за частотой (30,6 → 55 Гц
  перед разносом, 50 → 55 → 0 у #35941), факты аварии с заданием; в отчёте
  стоп-сегментов задания нет, у работы есть; размер входа совета — справочно;
- p7: в перепрогонах ни один ключ участков не упал (нет записи «Участки
  ключа …» в логе) — изоляция не меняет вывод на реальных окнах;
- p8: открытых эпизодов комбинированной OIL_DILUTION нет (условие выкладки);
  перепрогон окон её живых эпизодов за 60 дней — с KB v5.1.23 детекций 0, с
  включённой веткой — сколько было бы (справочно).

Этап 2 (п.2, заброс оборотов RPM_OVERSPEED):
- p2: за 30 дней (17.09–17.10) все отсчёты оборотов выше порога (решение В2′:
  1575 = уставка разноса из KB двигателя 1725 − 150, 91,3 %; proximity_pct
  8,69565) у трёх машин — 3 (при прежних 1552,5 было 5); окна их режимов
  перепрогоняются кодом движка (_run_segment), кластеры RPM_OVERSPEED — эпизоды:
  ровно 3 (ДГУ №2 15.10 12:42:19–12:42:29 1606/53,6, один участок; ДГУ №1
  16.10 09:26:03–09:26:12 1696/56,8; ДГУ №1 16.10 09:29:42–09:29:52 1584/52,9 —
  пуск 09:29:33), других нет (ложных 0); все CAUTION, код 234 справочный; текст
  «выше порога 1575 (91,3 % уставки разноса 1725)»; ближайшие отсчёты не выше
  порога — справочно. Детекции вне этих окон быть не может: без отсчёта выше
  порога заброса нет.
- p2x: справочно к В2′ — 60 дней (18.08–17.10): при 1575 у ДЭС №3 2 эпизода
  на скачках нагрузки (02.09 13:17:57 и 05.09 09:28:56 МСК, оба 1584), у ДГУ —
  те же 3; перепрогон тех же окон при прежних 1552,5 — у ДЭС №3 36 эпизодов
  (план В2′), каждый эпизод при 1575 лежит в эпизоде при 1552,5.

Этап 3 (п.3–4, профиль пуска и запрет снятия start_unstable):
- p3: все выходы из стопа за 30 дней (как в прототипе hunt.py: 40011 из 0 в
  1/2/3) перепрогоняются кодом движка (окно пуск −10 … +40 мин); у каждой
  «Работы» с выхода из стопа есть профиль; неустойчивых по варианту «б»
  (В1) ровно 6 — ДГУ №1 16.10 09:25:52, 09:28:33, 09:29:33, 09:57:55, ДГУ №2
  15.10 12:42:09 и 05:42:20 — с причинами по плану; пограничные пуски плана
  устойчивы; запрет снятия гейтом — у #35929, #35944, #35949 и #35900.
  Ревью R7: у цепочек 0→2→3 — ещё живой путь движка (окно с начала «Работы»,
  прогрев в контексте, преамбула 300 с и посев): задание и масло на начало
  прокрутки, «схватил» и неустойчивость — как в пакетном окне.

Этап 4 (п.5–6, подпись плашки попытки и имена кодов):
- p5: все плашки неудачных попыток за 30 дней в auto_segments — подпись «до»
  (v5.1.22, по режиму панели) и «после» (В4, по прокрутке) при чтении:
  меняется ровно у #35935 и #35937 («Неудачная работа · Стоп» → «Неудачный
  пуск · Стоп»), их сохранённые заключения не переписываются; перепрогон
  коротких выходов из стопа (цепочка до 10 мин) кодом движка — подписи и
  факты такой же плашки после деплоя (#35935/#35937 — тексты плана);
- p6: панельные эпизоды с привязанным кодом за 30 дней — имя «до» (бит
  каталога) и «после» (имя кода KB и «(код N)»): #35941 — 234, #35944 —
  1464; перепрогон ДГУ №1 16.10 09:20–09:40: имена в отчёте, в ленте акта
  #35941 40599 = 0 — «нет команды (0)», голос характера «команда «Работа»
  снята сразу (3→0), без «Стоп»»; каталог БД телеметрии — справочно.

Запуск:
    py -3 scripts/check_v5123_data.py --dsn-file .dsn            # все разделы
    py -3 scripts/check_v5123_data.py --dsn-file .dsn p1 act     # выбранные
Код возврата 1 — есть расхождения.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import asyncpg  # noqa: E402

from analytics.labels import fmt_local  # noqa: E402
from online import engine as E  # noqa: E402

MSK = ZoneInfo("Europe/Moscow")
U = timezone.utc
RO = {"default_transaction_read_only": "on"}
MACH = {"ДГУ №1": ("6003790403", "pcc", 1), "ДГУ №2": ("6003790403", "pcc", 2),
        "ДЭС №3": ("1126246373", "pcc", 1)}
_ENG = {"6003790403": "cummins_kta50", "1126246373": "cummins_qsx15"}
FREF = 40595
_fails: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  OK   " if cond else "  FAIL ") + msg)
    if not cond:
        _fails.append(msg)


def M(d: int, h: int, mi: int, s: int = 0, mo: int = 10) -> datetime:
    return datetime(2026, mo, d, h, mi, s, tzinfo=MSK).astimezone(U)


def _u(t):
    if t is None:
        return None
    if isinstance(t, str):
        t = datetime.fromisoformat(t)
    return t if t.tzinfo else t.replace(tzinfo=U)


def _j(v, default):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return default
    return v if v is not None else default


def f(t) -> str:
    return fmt_local(t, MSK, day=True) or "—"


class _LogCatch(logging.Handler):
    """Предупреждения аналитики во время перепрогонов — для раздела p7."""

    def __init__(self):
        super().__init__(logging.WARNING)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


LOG = _LogCatch()
logging.getLogger("analytics").addHandler(LOG)
_REPLAYS: list[str] = []


def _cfg(sn: str):
    from analytics.config import AnalyticsConfig
    return AnalyticsConfig.from_pair(ROOT / "knowledge_base", "pcc3300", _ENG.get(sn, "cummins_kta50"))


async def _replay(key: str, a: datetime, b: datetime, cfg=None):
    """Окно кодом движка (_run_segment): сегменты и акты, только чтение источника."""
    from analytics.contract import CokingRisk
    sn, et, p = MACH[key]
    cfg = cfg or _cfg(sn)
    segs, inc, _g, _c = await E._run_segment(sn, et, p, "", a, b, cfg, CokingRisk())
    _REPLAYS.append(f"{key} {f(a)}–{f(b)}")
    return segs, inc, cfg


# ── p1: 40595 в history_rich, масштаб ───────────────────────────────────────

async def p1(conn):
    print("\n== p1: задание частоты 40595 в history_rich (последние сутки) ==")
    cat = {}
    for ln in (ROOT / "knowledge_base" / "controllers" / "pcc3300" / "register_map.jsonl").read_text(
            encoding="utf-8").splitlines():
        if ln.strip():
            r = json.loads(ln)
            if r.get("addr") == FREF:
                cat = r
                break
    check(cat.get("multiplier") == 0.001 and cat.get("unit") == "Hz" and cat.get("data_type") == "u32",
          f"каталог KB: 40595 u32 × 0,001, Гц ({cat.get('name')})")
    cfg = _cfg("6003790403")
    t1 = datetime.now(U)
    t0 = t1 - timedelta(days=1)
    wl = sorted(cfg.whitelist_analog - {FREF})
    for key, (sn, et, p) in MACH.items():
        r = await conn.fetchrow("""
            SELECT count(*) n, count(*) FILTER (WHERE abs(value - raw * 0.001) > 1e-6) bad_scale,
                   max(unit) unit, max(name_ru) nm, max(value) vmax,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY value) FILTER (WHERE value > 1) med_run,
                   count(*) FILTER (WHERE value = 0) n_zero
            FROM history_rich WHERE router_sn=$1 AND equip_type=$2 AND panel_id=$3 AND addr=$4
              AND ts >= $5 AND ts < $6""", sn, et, p, FREF, t0, t1)
        n_wl = await conn.fetchval("""
            SELECT count(*) FROM history_rich WHERE router_sn=$1 AND equip_type=$2 AND panel_id=$3
              AND addr = ANY($4::int[]) AND ts >= $5 AND ts < $6""", sn, et, p, wl, t0, t1)
        med = float(r["med_run"]) if r["med_run"] is not None else None
        print(f"  {key}: строк {r['n']}, ед. {r['unit']!r} «{r['nm']}», медиана при работе {med}, "
              f"макс {r['vmax'] and round(float(r['vmax']), 3)}, нулей {r['n_zero']}; "
              f"прирост whitelist +{100.0 * r['n'] / max(n_wl, 1):.2f} % ({n_wl} строк без 40595)")
        check(r["n"] > 0, f"{key}: 40595 есть в history_rich за сутки")
        check(r["bad_scale"] == 0 and r["unit"] == "Hz", f"{key}: value = raw × 0,001, единица Hz — уже Гц")
        check(med is not None and 49.0 <= med <= 51.0 and float(r["vmax"]) <= 65.0,
              f"{key}: на номинале ~50 Гц (медиана ненулевых {med})")


# ── act: перепрогон ДГУ №1 16.10 — лента и факты с заданием ─────────────────

_OLD_PROMPT = {35931: 15437, 35941: 20979}   # вход совета до v5.1.22, символов (check_v5122)


async def _cycle(conn, sn, et, p, inc):
    """Сегменты цикла из БД — как incident_gate._material (как в check_v5122)."""
    wf = datetime.fromisoformat(inc["window"]["from"])
    st = datetime.fromisoformat(inc["stop_ts"])
    return [dict(r) for r in await conn.fetch("""
        SELECT id, run_state, t_start, t_end, characteristics_json FROM auto_segments
        WHERE router_sn=$1 AND equip_type=$2 AND panel_id=$3 AND t_end IS NOT NULL
          AND t_end > $4 AND t_start < $5 ORDER BY t_start""", sn, et, p, wf, st)]


async def act(conn):
    print("\n== act: ДГУ №1 16.10 09:20–09:40 — колонка задания в ленте акта, факты, отчёт ==")
    from analytics.reconstructor import fall_facts_ru, format_analog_trace_ru
    from analytics.serializer import to_markdown
    segs, inc, cfg = await _replay("ДГУ №1", M(16, 9, 20), M(16, 9, 40))
    sn, et, p = MACH["ДГУ №1"]
    want = {35931: ("задание 30,6 Гц", "задание 55 Гц"), 35941: ("задание 55 Гц",)}
    for sid, stop in ((35931, M(16, 9, 26, 23)), (35941, M(16, 9, 28, 53))):
        a = inc.get(stop.isoformat())
        if a is None:
            check(False, f"#{sid}: акт в перепрогоне не построен")
            continue
        tr = a.get("trace") or {}
        roles = [c.get("role") for c in tr.get("cols") or []]
        vals = [r["v"]["FREQ_REFERENCE"] for r in tr.get("rows") or [] if "FREQ_REFERENCE" in r.get("v", {})]
        ent = (tr.get("entry") or {}).get("FREQ_REFERENCE")
        ff = fall_facts_ru(tr, MSK) or ""
        print(f"  #{sid}: колонки {roles}")
        print(f"       задание: вход {ent}, записи {vals}")
        for ln in format_analog_trace_ru(tr, MSK):
            print(f"       | {ln}")
        print(f"       факты: {ff}")
        check(roles[:3] == ["RPM", "FREQUENCY", "FREQ_REFERENCE"], f"#{sid}: колонка задания сразу за частотой")
        check(all(w in ff for w in want[sid]), f"#{sid}: факты аварии с заданием {want[sid]}")
        if sid == 35931:
            check(vals[:2] == [30.633, 55.0], f"#35931: задание 30,6 → 55 Гц перед разносом: {vals}")
        if sid == 35941:
            seq = ([ent["v"]] if ent else []) + vals
            check(50.0 in seq and 55.0 in seq and seq[-1] == 0.0
                  and seq.index(50.0) < seq.index(55.0) < len(seq) - 1,
                  f"#35941: задание 50 → 55 → 0: {seq}")
        try:
            from online.incident_gate import build_incident_prompt
            row = dict(await conn.fetchrow("SELECT * FROM auto_segments WHERE id=$1", sid))
            cyc = await _cycle(conn, sn, et, p, a)
            bare = build_incident_prompt(a, row, cyc, {}, tz=MSK)
            # Тот же вход без колонки задания — цена 40595 во входе совета
            a0 = copy.deepcopy(a)
            t0r = a0.get("trace") or {}
            t0r["cols"] = [c for c in t0r.get("cols") or [] if c.get("role") != "FREQ_REFERENCE"]
            (t0r.get("entry") or {}).pop("FREQ_REFERENCE", None)
            for r_ in t0r.get("rows") or []:
                r_.get("v", {}).pop("FREQ_REFERENCE", None)
            bare0 = build_incident_prompt(a0, row, cyc, {}, tz=MSK)
            print(f"       вход совета без блока оборудования: {len(bare)} симв., без колонки задания "
                  f"{len(bare0)} (+{len(bare) - len(bare0)}); до v5.1.22 было {_OLD_PROMPT[sid]}")
            check(len(bare) <= _OLD_PROMPT[sid],
                  f"#{sid}: вход совета с заданием не длиннее входа до v5.1.22 ({len(bare)} ≤ {_OLD_PROMPT[sid]})")
        except Exception as e:  # noqa: BLE001
            check(False, f"#{sid}: вход совета не собран: {e!r}")
    stops = [s for s in segs if s.run_state == 0]
    works = [s for s in segs if s.run_state == 3]
    md_s = "\n".join(to_markdown([s], sn, et, p, M(16, 9, 20), M(16, 9, 40), tz=MSK) for s in stops)
    md_w = "\n".join(to_markdown([s], sn, et, p, M(16, 9, 20), M(16, 9, 40), tz=MSK) for s in works)
    have_s = sum(1 for s in stops for sub in s.subsegments if "FREQ_REFERENCE" in (sub.characteristics or {}))
    print(f"  стоп-сегментов {len(stops)} (с заданием в характеристиках {have_s}), работ {len(works)}")
    check(stops and "FREQ_REFERENCE" not in md_s, "отчёт: в стоп-сегментах строки задания нет")
    check(works and "| FREQ_REFERENCE | Hz |" in md_w, "отчёт: у работы задание в таблице характеристик")


# ── p7: изоляция ключей — на реальных окнах ключи не падают ─────────────────

async def p7(conn):
    print("\n== p7: ключи участков на реальных окнах (ДГУ №1 16.10, ДГУ №2 15.10, ДЭС №3 13.10) ==")
    for key, a, b in (("ДГУ №1", M(16, 9, 20), M(16, 10, 30)), ("ДГУ №2", M(15, 12, 30), M(15, 13, 40)),
                      ("ДЭС №3", M(13, 10, 30), M(13, 15, 10))):
        segs, _inc, _cfg3 = await _replay(key, a, b)
        n_live = sum(1 for s in segs for sub in s.subsegments for d in sub.detections
                     if "live" in (d.values or {}))
        print(f"  {key} {f(a)}–{f(b)}: сегментов {len(segs)}, детекций по участкам {n_live}")
    bad = [r.getMessage() for r in LOG.records
           if r.getMessage().startswith(("Участки ключа", "Участки нарушений сегмента"))]
    for x in bad[:10]:
        print(f"    лог: {x}")
    check(not bad, f"во всех перепрогонах ({len(_REPLAYS)}) ни один ключ участков не упал")


# ── p8: комбинированная OIL_DILUTION — условие выкладки и перепрогон ────────

async def p8(conn):
    print("\n== p8: комбинированная ветка OIL_DILUTION (В6 = А) ==")
    rows = [dict(r) for r in await conn.fetch("""
        SELECT id, router_sn, equip_type, panel_id, t_open, t_close FROM alarm_episodes
        WHERE scenario = 'OIL_DILUTION' AND open_values_json ? 'oil_press_drop_kpa'
          AND t_open > now() - interval '60 days' ORDER BY t_open""")]
    n_open = sum(1 for r in rows if r["t_close"] is None)
    print(f"  живых эпизодов комбинированной ветки за 60 дней: {len(rows)} (план: 6), открытых: {n_open}")
    check(n_open == 0, "открытых эпизодов комбинированной OIL_DILUTION нет — выкладка без закрытия по свежести")
    key_of = {v: k for k, v in MACH.items()}
    n_kb = n_on = 0
    for r in rows:
        key = key_of.get((r["router_sn"], r["equip_type"], r["panel_id"]))
        if key is None:
            continue
        t = _u(r["t_open"])
        a, b = t - timedelta(minutes=10), t + timedelta(minutes=30)
        segs, _inc, cfg = await _replay(key, a, b)
        cfg_on = copy.copy(cfg)
        cfg_on.detectors = dict(cfg.detectors, OIL_DILUTION=dict(cfg.detectors["OIL_DILUTION"],
                                                                 combined_enabled=True))
        segs_on, _i2, _c2 = await _replay(key, a, b, cfg_on)

        def _comb(ss):
            return sum(1 for s in ss for sub in s.subsegments for d in sub.detections
                       if d.scenario == "OIL_DILUTION"
                       and str(d.description_key).endswith("combined_pressure_temp_anomaly"))
        k_kb, k_on = _comb(segs), _comb(segs_on)
        n_kb += k_kb
        n_on += k_on
        print(f"  эпизод {r['id']} {key} {f(t)}: KB v5.1.23 — {k_kb}, с включённой веткой — {k_on}")
    check(n_kb == 0, f"перепрогон окон живых эпизодов: комбинированных детекций с KB v5.1.23 — {n_kb}")
    print(f"  справочно: с включённой веткой в тех же окнах было бы {n_on} (часть уже гасит пол давления v5.1.21)")


# ── p2: заброс оборотов за 30 дней — 3 эпизода, ложных 0 (порог 1575, В2′) ──

_P2_FROM, _P2_TO = M(17, 0, 0, mo=9), M(18, 0, 0)
# (машина, начало кластера) → (обороты, Гц, конец кластера, участков) — план §2.1, проект «Обороты
# и пуск», порог 1575 (В2′): ДГУ №2 15.10 — один участок до 12:42:29 (при 1552,5 было до 12:43:09)
_P2_WANT = {("ДГУ №1", M(16, 9, 26, 3)): (1696.0, 56.8, M(16, 9, 26, 12), 1),
            ("ДГУ №1", M(16, 9, 29, 42)): (1584.0, 52.9, M(16, 9, 29, 52), 1),
            ("ДГУ №2", M(15, 12, 42, 19)): (1606.0, 53.6, M(15, 12, 42, 29), 1)}
_P2_OVER_N = 3          # отсчётов оборотов выше 1575 за 30 дней: 1696, 1584, 1606 (при 1552,5 было 5)
# 60 дней (18.08–17.10, справочно к В2′): у ДЭС №3 при 1575 — 2 эпизода на скачках нагрузки
_P2X_FROM = M(18, 0, 0, mo=8)
_P2X_DES3 = {("ДЭС №3", M(2, 13, 17, 57, mo=9)): (1584.0, None, None, None),
             ("ДЭС №3", M(5, 9, 28, 56, mo=9)): (1584.0, None, None, None)}
_P2X_OLD_DES3 = 36      # при прежних 1552,5 у ДЭС №3 (режим 19.08–16.09) — 36 одиночных эпизодов


def _cfg_prox(cfg, prox):
    """Копия конфига с другим RPM_OVERSPEED.proximity_pct (сравнение с прежним порогом)."""
    if prox is None:
        return cfg
    c = copy.copy(cfg)
    c.detectors = dict(cfg.detectors, RPM_OVERSPEED=dict(cfg.detectors.get("RPM_OVERSPEED") or {},
                                                         proximity_pct=prox))
    return c


async def _os_scan(conn, frm: datetime, to: datetime, prox=None, verbose: bool = True):
    """Отсчёты оборотов выше порога заброса за [frm, to) и эпизоды RPM_OVERSPEED
    в перепрогонах окон их режимов кодом движка. prox — proximity_pct вместо KB.
    → ({машина: отсчётов выше}, {(машина, начало кластера): {...}}, {машина: порог})."""
    from analytics.labels import fmt_number
    from analytics.rpm_freq import overspeed_params
    t_to = min(to, datetime.now(U))
    n_over: dict[str, int] = {}
    thrs: dict[str, float] = {}
    found: dict[tuple, dict] = {}
    for key, (sn, et, p) in MACH.items():
        cfg = _cfg_prox(_cfg(sn), prox)
        op = overspeed_params(cfg)
        thr = op["threshold_rpm"]
        thrs[key] = thr
        rpm_a = cfg.role_to_addr("RPM")
        over = [dict(r) for r in await conn.fetch("""
            SELECT ts, value FROM history_rich WHERE router_sn=$1 AND equip_type=$2 AND panel_id=$3
              AND addr=$4 AND ts >= $5 AND ts < $6 AND value > $7 ORDER BY ts""",
            sn, et, p, rpm_a, frm, t_to, thr)]
        n_over[key] = len(over)
        if verbose:
            near = [dict(r) for r in await conn.fetch("""
                SELECT ts, value FROM history_rich WHERE router_sn=$1 AND equip_type=$2 AND panel_id=$3
                  AND addr=$4 AND ts >= $5 AND ts < $6 AND value > $7 AND value <= $8
                ORDER BY value DESC LIMIT 3""", sn, et, p, rpm_a, frm, t_to, thr - 25.0, thr)]
            print(f"  {key}: порог {fmt_number(thr, 1)} ({fmt_number(100 - op['proximity_pct'], 1)} % уставки "
                  f"{op['overspeed_rpm']:.0f}), отсчётов выше — {len(over)}; ближайшие не выше: "
                  + (", ".join(f"{f(r['ts'])} {float(r['value']):.0f}" for r in near) or "нет"))
        # Окна режимов отсчётов выше порога (40011 на момент отсчёта), слитые
        wins: list[list[datetime]] = []
        for r in over:
            t = _u(r["ts"])
            per = await conn.fetchrow("""
                SELECT value, state_start, state_end FROM enum_history
                WHERE router_sn=$1 AND equip_type=$2 AND panel_id=$3 AND addr=40011
                  AND state_start <= $4 AND (state_end IS NULL OR state_end > $4)
                  AND state_start > $4 - interval '40 days'
                ORDER BY state_start DESC LIMIT 1""", sn, et, p, t)
            ps = _u(per["state_start"]) if per else t
            pe = _u(per["state_end"]) if per and per["state_end"] else t + timedelta(hours=1)
            if verbose:
                print(f"    {f(t)} {float(r['value']):.0f} об/мин, режим {per and per['value']} "
                      f"[{f(ps)} – {f(pe) if per and per['state_end'] else '…'}]")
            a = max(ps - timedelta(minutes=10), t - timedelta(hours=2))
            b = min(pe + timedelta(minutes=5), t + timedelta(hours=2), t_to)
            if wins and a <= wins[-1][1]:
                wins[-1][1] = max(wins[-1][1], b)
            else:
                wins.append([a, b])
        for a, b in wins:
            segs, _inc, _c = await _replay(key, a, b, cfg)
            for s in segs:
                for sub in s.subsegments:
                    for d in sub.detections:
                        if d.scenario != "RPM_OVERSPEED":
                            continue
                        for c in (d.values.get("live") or {}).get("clusters") or []:
                            k = (key, _u(c["start"]))
                            cur = found.setdefault(k, {"end": c["end"], "vmax": c.get("vmax"), "spans": c.get("spans"),
                                                       "dets": []})
                            if _u(c["end"]) > _u(cur["end"]):
                                cur.update(end=c["end"], spans=c.get("spans"))
                            cur["dets"].append(d)
    return n_over, found, thrs


def _os_print(found: dict) -> None:
    for (key, st), c in sorted(found.items(), key=lambda x: x[0][1]):
        d = max(c["dets"], key=lambda x: x.values.get("rpm_max") or 0)
        v = d.values
        print(f"    {key} {f(st)} – {f(_u(c['end']))}: макс. {v.get('rpm_max'):.0f} об/мин "
              f"({v.get('freq_hz_max')} Гц), участков {c['spans']}, до уставки {v.get('margin_rpm')}, "
              f"{d.severity}, коды {d.fault_codes} / справочно {d.ref_codes}")
        print(f"      {d.trigger}")


def _os_check(found: dict, want: dict, what: str) -> None:
    got = set(found)
    check(got == set(want),
          f"{what}: эпизоды заброса — ровно {len(want)}, ложных 0: лишние "
          f"{sorted((k, f(t)) for k, t in got - set(want))}, нет {sorted((k, f(t)) for k, t in set(want) - got)}")
    for k, (rpm, hz, end, spans) in want.items():
        c = found.get(k)
        if c is None:
            continue
        d = max(c["dets"], key=lambda x: x.values.get("rpm_max") or 0)
        check(d.values.get("rpm_max") == rpm and (hz is None or d.values.get("freq_hz_max") == hz),
              f"{what}: {k[0]} {f(k[1])}: {rpm:.0f} об/мин" + (f" при {hz} Гц" if hz is not None else "")
              + f" ({d.values.get('rpm_max')}/{d.values.get('freq_hz_max')})")
        if end is not None:
            check(_u(c["end"]) == end and c["spans"] == spans,
                  f"{what}: {k[0]} эпизод {f(k[1])}–{fmt_local(end, MSK)}, участков {spans} "
                  f"({fmt_local(_u(c['end']), MSK)}, {c['spans']})")
        check(all(x.severity == "CAUTION" and 234 in (x.ref_codes or []) and 234 not in (x.fault_codes or [])
                  for x in c["dets"]),
              f"{what}: {k[0]} {f(k[1])}: CAUTION, код 234 справочный (панель не выставляла)")


async def p2(conn):
    print("\n== p2: заброс оборотов RPM_OVERSPEED за 30 дней (17.09–17.10), порог 1575 (В2′) ==")
    n_over, found, thrs = await _os_scan(conn, _P2_FROM, _P2_TO)
    check(all(1575.0 < t < 1575.001 for t in thrs.values()),
          f"порог из KB у всех трёх машин — 1575 (строго «выше»): {sorted(set(thrs.values()))}")
    tot = sum(n_over.values())
    print(f"  всего отсчётов выше порога: {tot} {n_over} (ожидание: {_P2_OVER_N}; при 1552,5 было 5)")
    check(tot == _P2_OVER_N, f"отсчётов оборотов выше 1575 за 30 дней — {tot}, ожидание {_P2_OVER_N}")
    print(f"  эпизодов (кластеров) RPM_OVERSPEED в перепрогонах: {len(found)}")
    _os_print(found)
    _os_check(found, _P2_WANT, "30 дней")
    w2 = found.get(("ДГУ №2", M(15, 12, 42, 19)))
    if w2:
        d = max(w2["dets"], key=lambda x: x.values.get("rpm_max") or 0)
        check("выше порога 1575 (91,3 % уставки разноса 1725), до уставки осталось 119" in d.trigger,
              f"ДГУ №2 15.10: текст «выше порога 1575 (91,3 % уставки разноса 1725), до уставки осталось 119»: "
              f"{d.trigger}")


async def p2x(conn):
    print("\n== p2x: заброс оборотов за 60 дней (18.08–17.10), справочно к В2′: 1575 против 1552,5 ==")
    n_over, found, _t = await _os_scan(conn, _P2X_FROM, _P2_TO)
    print(f"  при 1575: отсчётов выше {n_over}; эпизодов {len(found)}")
    _os_print(found)
    _os_check(found, {**_P2_WANT, **_P2X_DES3}, "60 дней, 1575")
    print("  для сравнения — прежний порог 1552,5 (proximity_pct 10), перепрогон тех же 60 дней:")
    n_old, f_old, _t = await _os_scan(conn, _P2X_FROM, _P2_TO, prox=10, verbose=False)
    by_key: dict[str, list] = {}
    for (key, st), c in sorted(f_old.items(), key=lambda x: x[0][1]):
        by_key.setdefault(key, []).append((st, c))
    for key in MACH:
        eps = by_key.get(key, [])
        rmax = [max(x.values.get("rpm_max") or 0 for x in c["dets"]) for _st, c in eps]
        print(f"    {key}: отсчётов выше 1552,5 — {n_old.get(key)}, эпизодов {len(eps)}"
              + (f" ({min(rmax):.0f}–{max(rmax):.0f} об/мин, {f(eps[0][0])} … {f(eps[-1][0])})" if eps else ""))
    des3 = by_key.get("ДЭС №3", [])
    print(f"  ДЭС №3 при 1552,5: {len(des3)} эпизодов (план В2′: {_P2X_OLD_DES3}); при 1575: "
          f"{sum(1 for k in found if k[0] == 'ДЭС №3')} (план: 2)")
    check(len(des3) == _P2X_OLD_DES3,
          f"60 дней, 1552,5 (сравнение): у ДЭС №3 {len(des3)} эпизодов, по плану В2′ {_P2X_OLD_DES3}")
    lost = [(k, f(st)) for (k, st) in found
            if not any(k2 == k and st2 <= st <= _u(c2["end"]) for (k2, st2), c2 in f_old.items())]
    check(not lost, f"60 дней: каждый эпизод при 1575 лежит в эпизоде при 1552,5 (порог только поднят): вне — {lost}")


# ── p3: профиль пуска на всех подтверждённых пусках 30 дней ─────────────────

# Неустойчивые по варианту «б» (В1): ДГУ №1 16.10 ×4, ДГУ №2 15.10 12:42 и 05:42
_P3_UNSTABLE = {("ДГУ №1", M(16, 9, 25, 52)), ("ДГУ №1", M(16, 9, 28, 33)), ("ДГУ №1", M(16, 9, 29, 33)),
                ("ДГУ №1", M(16, 9, 57, 55)), ("ДГУ №2", M(15, 12, 42, 9)), ("ДГУ №2", M(15, 5, 42, 20))}


def _hz1(v) -> str:
    return "—" if v is None else f"{float(v):.2f}".rstrip("0").rstrip(".")


async def p3(conn):
    print("\n== p3: профиль пуска на подтверждённых пусках за 30 дней (17.09–17.10), В1 вариант «б» ==")
    from analytics.labels import start_note_text, start_profile_text
    t_to = min(_P2_TO, datetime.now(U))
    found: dict[tuple, dict] = {}
    n_starts = 0
    no_prof: list[str] = []
    for key, (sn, et, p) in MACH.items():
        cfg = _cfg(sn)
        per = [dict(r) for r in await conn.fetch("""
            SELECT value, state_start, state_end FROM enum_history
            WHERE router_sn=$1 AND equip_type=$2 AND panel_id=$3 AND addr=40011
              AND state_start < $5 AND (state_end IS NULL OR state_end > $4)
            ORDER BY state_start""", sn, et, p, _P2_FROM - timedelta(days=2), t_to)]
        # Пуски — как в прототипе hunt.py: выход 40011 из 0 в 1/2/3
        starts, prev = [], None
        for r in per:
            v = int(r["value"])
            if v in (1, 2, 3) and prev in (0, None) and _u(r["state_start"]) >= _P2_FROM:
                starts.append(_u(r["state_start"]))
            prev = v
        n_starts += len(starts)
        # Окна перепрогона: пуск −10 мин … +40 мин, близкие — одним окном
        wins: list[list[datetime]] = []
        for s in starts:
            a, b = s - timedelta(minutes=10), min(s + timedelta(minutes=40), t_to)
            if wins and a <= wins[-1][1]:
                wins[-1][1] = max(wins[-1][1], b)
            else:
                wins.append([a, b])
        profs: list[tuple[datetime, Any, dict]] = []
        works: list[Any] = []
        for a, b in wins:
            segs, _inc, _c = await _replay(key, a, b, cfg)
            for sg in segs:
                sp = getattr(sg, "start_profile", None)
                if isinstance(sp, dict):
                    profs.append((_u(sp["crank_at"]), sg, sp))
                elif sg.run_state == 3 and not getattr(sg, "attempt_pending", False):
                    works.append(sg)
        profs.sort(key=lambda x: x[0])
        for i, s in enumerate(starts):
            nxt = starts[i + 1] if i + 1 < len(starts) else t_to
            hit = next(((c, sg, sp) for c, sg, sp in profs if s - timedelta(seconds=2) <= c < nxt), None)
            if hit is None:
                # Без профиля — только если «Работы» с этого выхода нет (попытка
                # без «схватил», прогрев без «Работы»)
                w = [sg for sg in works if s <= _u(sg.t_start) < nxt and sg.cause_open != "REPORT_START"]
                no_prof.append((key, s, w))
                continue
            found[(key, s)] = {"seg": hit[1], "sp": hit[2]}
    print(f"  выходов из стопа (как в hunt.py): {n_starts}; с профилем пуска: {len(found)}; без профиля: {len(no_prof)}")
    for key, s, w in no_prof:
        print(f"    без профиля: {key} {f(s)}" + (f" — «Работа» {[f(sg.t_start) for sg in w]}" if w else
                                                  " — попытка без «схватил» или прогрев без «Работы»"))
    check(not any(w for _k, _s, w in no_prof), "у каждой «Работы» с выхода из стопа есть профиль пуска")
    # Сверка с прототипом: hunt.out снят 17.10 около 07:20 МСК — 53 пуска со «схватил»
    _hunt_to = M(17, 7, 20)
    n_hunt = sum(1 for (k, s) in found if s < _hunt_to)
    print(f"  справочно: пусков с профилем до {f(_hunt_to)} (период hunt.out) — {n_hunt}; "
          f"позже — {sorted((k, f(s)) for (k, s) in found if s >= _hunt_to)}")
    unstable = set()
    for (key, s), d in sorted(found.items(), key=lambda x: (x[0][0], x[0][1])):
        sp = d["sp"]
        fr = sp.get("fref") or {}
        rng = fr.get("range") or [None, None]
        print(f"  {key} {f(s)} #{'' if not sp.get('unstable') else 'НЕУСТ'} "
              f"номинал {sp.get('rated_sec')} с, окно до {fmt_local(sp.get('t_to'), MSK)} ({sp.get('end_by')}), "
              f"об {sp.get('rpm')}, Гц {sp.get('freq')}, раскачка {sp.get('swing_sec'):.0f} с, смен {sp.get('flips')}, "
              f"задание {_hz1(rng[0])}…{_hz1(rng[1])} (посл. {fr.get('seq')}), "
              f"заброс {(sp.get('overspeed') or {}).get('rpm')}, масло {sp.get('oil_temp')}, "
              f"причины {sp.get('reasons')}")
        if sp.get("unstable"):
            unstable.add((key, s))
            print(f"      Пуск: {start_profile_text(sp, MSK)}")
            print(f"      сводка: {start_note_text(sp, MSK)}")
    print(f"  неустойчивых: {len(unstable)}")
    check(unstable == _P3_UNSTABLE,
          f"неустойчивые пуски по варианту «б» — ровно 6 (ДГУ №1 16.10 ×4, ДГУ №2 15.10 12:42 и 05:42): "
          f"лишние {sorted((k, f(t)) for k, t in unstable - _P3_UNSTABLE)}, "
          f"нет {sorted((k, f(t)) for k, t in _P3_UNSTABLE - unstable)}")
    # Опорные значения плана §2.1 и проекта «Обороты и пуск»
    want = {("ДГУ №1", M(16, 9, 29, 33)): {"overspeed", "swing", "flips"},
            ("ДГУ №1", M(16, 9, 57, 55)): {"swing", "flips"},
            ("ДГУ №2", M(15, 12, 42, 9)): {"overspeed", "swing", "flips", "fref_low"},
            ("ДГУ №2", M(15, 5, 42, 20)): {"fref_high"},
            ("ДГУ №1", M(16, 9, 25, 52)): {"overspeed", "fref_high"},
            ("ДГУ №1", M(16, 9, 28, 33)): {"fref_high"}}
    for k, rs in want.items():
        d = found.get(k)
        got = set((d or {}).get("sp", {}).get("reasons") or [])
        check(d is not None and got == rs, f"{k[0]} {f(k[1])}: причины {sorted(rs)} ({sorted(got)})")
    d = found.get(("ДГУ №1", M(16, 9, 29, 33)))
    if d:
        sp = d["sp"]
        check(sp.get("swing_sec") == 110.0 and sp.get("flips") == 6
              and (sp.get("fref") or {}).get("range") == [48.566, 50.733],
              f"ДГУ №1 16.10 09:29:33: раскачка 1 мин 50 с, 6 смен знака (седьмую у hunt.py даёт сброс "
              f"оборотов 09:32:32 перед охлаждением), задание 48,6–50,7 Гц: "
              f"{sp.get('swing_sec')} с, {sp.get('flips')}, {(sp.get('fref') or {}).get('range')}")
    d = found.get(("ДГУ №1", M(16, 9, 57, 55)))
    if d:
        sp = d["sp"]
        check(sp.get("swing_sec") == 40.0 and sp.get("flips") == 2
              and (sp.get("fref") or {}).get("range") == [49.433, 50.533],
              f"ДГУ №1 16.10 09:57:55: раскачка 40 с, 2 смены знака, задание 49,4–50,5 Гц: "
              f"{sp.get('swing_sec')} с, {sp.get('flips')}, {(sp.get('fref') or {}).get('range')}")
    d = found.get(("ДГУ №1", M(16, 9, 28, 33)))
    if d:
        hi = (d["sp"].get("fref") or {}).get("high") or {}
        check(hi.get("v") == 55.0 and hi.get("stage") == "ramp" and hi.get("rpm") == 1294.0,
              f"ДГУ №1 16.10 09:28:33 (#35939): на разгоне задание 55 Гц при 1294 об/мин: {hi}")
    if d:
        # Акт #35941: строка «Пуск» сразу за блоком сегмента падения (#35939)
        from online.incident_gate import _format_cycle_params
        sg = d["seg"]
        cyc = [{"run_state": 3, "t_start": sg.t_start, "t_end": sg.t_end, "characteristics_json": sg.to_dict()},
               {"run_state": 0, "t_start": sg.t_end, "t_end": None, "characteristics_json": {"stop_kind": "EMERGENCY"}}]
        act_l = [ln for ln in _format_cycle_params(cyc, sg.t_end, None, tz=MSK) if ln.startswith("Пуск ")]
        print(f"  акт #35941: {act_l}")
        check(len(act_l) == 1 and act_l[0].startswith("Пуск 09:28:33: до номинала не вышел")
              and act_l[0].endswith("неустойчивый: на разгоне задание 55 Гц при 1294 об/мин"),
              "акт #35941 — строка «Пуск» сегмента падения #35939")
    d = found.get(("ДГУ №2", M(15, 5, 42, 20)))
    if d:
        hi = (d["sp"].get("fref") or {}).get("high") or {}
        check(hi.get("v") == 52.2 and hi.get("stage") == "crank",
              f"ДГУ №2 15.10 05:42: задание 52,2 Гц на прокрутке: {hi}")
    # Запрет снятия гейтом (п.4): просадка или заброс, первый кластер которых в
    # окне неустойчивого пуска, — тем же помощником, что у статуса и гейта
    from online.status_assembler import start_unstable_reason
    banned, allowed = set(), []
    for (key, s), d in found.items():
        dd = [x.to_dict() for sub in d["seg"].subsegments for x in sub.detections
              if x.scenario in ("RPM_UNDERSPEED", "RPM_OVERSPEED")]
        if not dd:
            continue
        r = start_unstable_reason(d["sp"], dd)
        if r:
            banned.add((key, s))
            print(f"  запрет снятия {key} {f(s)}: {r['text']}")
        else:
            allowed.append(f"{key} {f(s)} ({', '.join(sorted({x['scenario'] for x in dd}))})")
    print(f"  просадка/заброс с профилем без запрета (вне окна или пуск устойчив): {allowed or 'нет'}")
    _want_ban = {("ДГУ №1", M(16, 9, 25, 52)), ("ДГУ №1", M(16, 9, 29, 33)), ("ДГУ №1", M(16, 9, 57, 55)),
                 ("ДГУ №2", M(15, 12, 42, 9))}
    check(banned == _want_ban,
          f"запрет start_unstable — #35929, #35944, #35949 (ДГУ №1 16.10) и #35900 (ДГУ №2 15.10 12:42): "
          f"лишние {sorted((k, f(t)) for k, t in banned - _want_ban)}, нет {sorted((k, f(t)) for k, t in _want_ban - banned)}")
    # Живой путь для цепочек 0→2→3 (ревью R7): движок открывает окно «Работы» на её
    # начале, прогрев — в контексте закрытых сегментов, история — с преамбулой
    # 300 с и посевом удержанных значений (с v5.1.23 — и задания частоты).
    # Значения на начало прокрутки должны совпасть с пакетным окном
    from analytics.contract import CokingRisk
    from analytics.transitions import link_from_segment, push_ctx
    n_chain = 0
    for (key, s), d in sorted(found.items(), key=lambda x: (x[0][0], x[0][1])):
        sp, sg = d["sp"], d["seg"]
        w0 = _u(sg.t_start)
        if _u(sp["crank_at"]) >= w0 - timedelta(seconds=2) or sg.cause_open == "REPORT_START":
            continue
        n_chain += 1
        sn, et, p = MACH[key]
        cfg = _cfg(sn)
        head, _i, _c = await _replay(key, _u(sp["crank_at"]) - timedelta(minutes=10), w0, cfg)
        ctx: list = []
        for x in head:
            ctx = push_ctx(ctx, link_from_segment(x))
        segs2, _i2, _g2, _c2 = await E._run_segment(sn, et, p, "", w0, min(w0 + timedelta(minutes=40), t_to),
                                                     cfg, CokingRisk(), transition_ctx=ctx)
        _REPLAYS.append(f"{key} {f(w0)} (живой путь)")
        lp = next((x.start_profile for x in segs2 if isinstance(getattr(x, "start_profile", None), dict)), None)
        _cmp = ("crank_at", "fire_at", "oil_temp", "unstable", "reasons")
        print(f"  живой путь {key} {f(s)} (прогрев {f(sp['crank_at'])} → «Работа» {f(w0)}): "
              f"задание {(lp or {}).get('fref', {}).get('seq')} / пакет {(sp.get('fref') or {}).get('seq')}, "
              f"масло {(lp or {}).get('oil_temp')} / {sp.get('oil_temp')}, "
              f"схватил {fmt_local((lp or {}).get('fire_at'), MSK)} / {fmt_local(sp.get('fire_at'), MSK)}")
        check(lp is not None and (lp.get("fref") or {}).get("seq") == (sp.get("fref") or {}).get("seq")
              and all(lp.get(k) == sp.get(k) for k in _cmp),
              f"живой путь {key} {f(s)}: задание и масло на начало прокрутки, «схватил», неустойчивость — "
              f"как в пакетном окне")
    print(f"  цепочек 0→2→3 с прогревом в контексте: {n_chain}")
    check(n_chain >= 1, "есть пуски 0→2→3 для сверки живого пути")
    # Пограничные пуски плана (§2.1, тесты п.4) — устойчивы
    for key, s in (("ДГУ №2", M(27, 17, 24, 10, mo=9)), ("ДГУ №1", M(27, 17, 24, 42, mo=9)),
                   ("ДГУ №2", M(15, 13, 0, 20)), ("ДГУ №2", M(20, 16, 34, 50, mo=9)),
                   ("ДГУ №1", M(25, 13, 48, 33, mo=9)), ("ДГУ №1", M(12, 13, 43, 13)),
                   ("ДГУ №1", M(29, 6, 8, 3, mo=9)), ("ДГУ №1", M(15, 13, 28, 53))):
        d = found.get((key, s))
        check(d is not None and not d["sp"].get("unstable"),
              f"пограничный пуск {key} {f(s)} — профиль есть, устойчив: "
              f"{d and (d['sp'].get('swing_sec'), d['sp'].get('flips'), (d['sp'].get('fref') or {}).get('range'))}")


# ── p5: подписи плашек попыток за 30 дней — до и после (п.5, В4) ────────────

def _label_v5122(att: dict) -> str | None:
    """Подпись плашки до v5.1.23 (по режиму панели, решение 13.10) — для «было»."""
    if not isinstance(att, dict):
        return None
    try:
        fs = int(att.get("from_state") or 3)
    except (TypeError, ValueError):
        fs = 3
    em = att.get("outcome") == "EMERGENCY"
    if fs == 1:
        head = "Неудачный пуск" if em else "Отменённый пуск"
    else:
        head = {2: "Неудачный прогрев", 3: "Неудачная работа"}.get(fs, "Неудачный пуск")
    return f"{head} · {'Авария' if em else 'Стоп'}"


# Плашки в БД, у которых подпись меняется (план §2.1, «Что увидит владелец»)
_P5_CHANGE = {35935: ("Неудачная работа · Стоп", "Неудачный пуск · Стоп"),
              35937: ("Неудачная работа · Стоп", "Неудачный пуск · Стоп")}
# Факты тех же плашек при новой нарезке (перепрогон) — по началу попытки
_P5_FACTS = {M(16, 9, 27, 23): "прокрутка 20 с до 189 об/мин без вспышки, АКБ до 21,5 В, переключатель «Выкл» "
                               "09:27:43, команда работы — «Работа»",
             M(16, 9, 27, 55): "прокрутка 10 с до 187 об/мин без вспышки, АКБ до 22,0 В, переключатель «Выкл» и "
                               "команда работы «Стоп» 09:28:02"}


async def p5(conn):
    print("\n== p5: подписи плашек неудачных попыток за 30 дней (17.09–17.10) — до (v5.1.22) и после (В4) ==")
    from analytics.labels import attempt_facts_ru, attempt_label, label_from_chars
    t_to = min(_P2_TO, datetime.now(U))
    rows = await conn.fetch("""
        SELECT id, router_sn, equip_type, panel_id, t_start, t_end, characteristics_json ch
        FROM auto_segments
        WHERE t_start >= $1 AND t_start < $2 AND jsonb_typeof(characteristics_json->'attempt') = 'object'
        ORDER BY t_start""", _P2_FROM, t_to)
    names = {v: k for k, v in MACH.items()}
    changed: dict[int, tuple] = {}
    print(f"  плашек в БД: {len(rows)}")
    for r in rows:
        ch = _j(r["ch"], {})
        att = ch.get("attempt") or {}
        was, now = _label_v5122(att), label_from_chars(ch)
        if was != now:
            changed[r["id"]] = (was, now)
        print(f"  #{r['id']} {names.get((r['router_sn'], r['equip_type'], r['panel_id']), '?')} "
              f"{f(r['t_start'])}–{f(r['t_end'])}: «{was}» → «{now}»; факты записи (до v5.1.23, как были): "
              f"{attempt_facts_ru(att, MSK)}")
    check(changed == _P5_CHANGE,
          f"подпись при чтении меняется ровно у #35935 и #35937 («Неудачная работа · Стоп» → «Неудачный пуск · "
          f"Стоп»): {changed}")
    # Сохранённые заключения плашек не переписываются (решение В4)
    try:
        sa = await conn.fetch("""SELECT auto_segment_id, claude_model, humanized_md FROM segment_analyses
                                 WHERE auto_segment_id = ANY($1::bigint[])""", list(_P5_CHANGE))
        for x in sa:
            print(f"  заключение #{x['auto_segment_id']} ({x['claude_model']}), не переписывается: "
                  f"{(x['humanized_md'] or '')[:160]}")
        check(all("Неудачная работа" in (x["humanized_md"] or "") for x in sa),
              "сохранённые заключения #35935/#35937 остаются со словами «Неудачная работа» (не переписываем)")
    except Exception as e:  # noqa: BLE001
        print(f"  заключения не прочитаны: {e!r}")
    # Как будет у такой же плашки после деплоя: перепрогон окон коротких выходов
    # из стопа (цепочка не нулевых режимов до 10 мин) кодом движка
    seen: list[tuple] = []
    for key, (sn, et, p) in MACH.items():
        cfg = _cfg(sn)
        per = [dict(r) for r in await conn.fetch("""
            SELECT value, state_start, state_end FROM enum_history
            WHERE router_sn=$1 AND equip_type=$2 AND panel_id=$3 AND addr=40011
              AND state_start < $5 AND (state_end IS NULL OR state_end > $4)
            ORDER BY state_start""", sn, et, p, _P2_FROM - timedelta(days=1), t_to)]
        cands, i = [], 0
        while i < len(per):
            v = int(per[i]["value"])
            if v != 0 and i > 0 and int(per[i - 1]["value"]) == 0 and _u(per[i]["state_start"]) >= _P2_FROM:
                j = i
                while j < len(per) and int(per[j]["value"]) != 0:
                    j += 1
                if j < len(per) and (_u(per[j]["state_start"]) - _u(per[i]["state_start"])).total_seconds() <= 600:
                    cands.append((_u(per[i]["state_start"]), _u(per[j]["state_start"])))
                i = j
                continue
            i += 1
        wins: list[list[datetime]] = []
        for s, e in cands:
            a, b = s - timedelta(minutes=5), min(e + timedelta(minutes=5), t_to)
            if wins and a <= wins[-1][1]:
                wins[-1][1] = max(wins[-1][1], b)
            else:
                wins.append([a, b])
        for a, b in wins:
            segs, _inc, _c = await _replay(key, a, b, cfg)
            for sg in segs:
                att = getattr(sg, "attempt", None)
                if isinstance(att, dict) and (key, att.get("t_start")) not in {(k, x[0]) for k, x in seen}:
                    seen.append((key, (att["t_start"], _label_v5122(att), attempt_label(att),
                                       attempt_facts_ru(att, MSK, codes=True))))
    print(f"  перепрогон коротких выходов из стопа: плашек {len(seen)}")
    for key, (ts, was, now, facts) in sorted(seen, key=lambda x: x[1][0]):
        print(f"    {key} {f(ts)}: «{was}» → «{now}»: {facts}")
    # Ожидание (историческая серия): ДГУ №2 20.09 — прокрутка ~100 об/мин без
    # вспышки ×4; ДЭС №3 13.10 — обороты 0, трижды 1438 с аварией и одна отмена
    # ключом «Ручной» → «Выкл» без кода; ДГУ №1 16.10 — #35935, #35937
    want_r = {("ДГУ №2", M(20, 15, 51, 50, mo=9)): "Неудачный пуск · Стоп",
              ("ДГУ №2", M(20, 15, 52, 10, mo=9)): "Неудачный пуск · Стоп",
              ("ДГУ №2", M(20, 15, 54, 20, mo=9)): "Неудачный пуск · Стоп",
              ("ДГУ №2", M(20, 15, 54, 49, mo=9)): "Неудачный пуск · Стоп",
              ("ДЭС №3", M(13, 10, 40, 1)): "Неудачный пуск · Авария",
              ("ДЭС №3", M(13, 10, 40, 28)): "Отменённый пуск · Стоп",
              ("ДЭС №3", M(13, 10, 44, 4)): "Неудачный пуск · Авария",
              ("ДЭС №3", M(13, 10, 45, 7)): "Неудачный пуск · Авария",
              ("ДГУ №1", M(16, 9, 27, 23)): "Неудачный пуск · Стоп",
              ("ДГУ №1", M(16, 9, 27, 55)): "Неудачный пуск · Стоп"}
    got_r = {(k, _u(ts)): now for k, (ts, was, now, facts) in seen}
    check(got_r == want_r, f"плашки перепрогона — подписи по В4: лишние/другие "
                           f"{sorted((k, f(t), v) for (k, t), v in got_r.items() if want_r.get((k, t)) != v)}, "
                           f"нет {sorted((k, f(t)) for (k, t) in want_r if (k, t) not in got_r)}")
    by_t = {_u(ts): (now, facts) for key, (ts, was, now, facts) in seen if key == "ДГУ №1"}
    for t0, want in _P5_FACTS.items():
        got = by_t.get(t0)
        check(got is not None and got == ("Неудачный пуск · Стоп", want),
              f"перепрогон ДГУ №1 {f(t0)}: «Неудачный пуск · Стоп», факты плана — {got}")
    # «Отменённый пуск» не может спорить с фактами: ни прокрутки, ни кода
    bad = [(k, f(ts), now, facts) for k, (ts, was, now, facts) in seen
           if any(w in facts for w in ("оператор", "пауз", "цикл"))
           or (now.startswith("Отменённый") and ("код " in facts or facts.startswith("прокрутка")))]
    check(not bad, f"в фактах только наблюдаемое; «Отменённый пуск» — без прокрутки и без кода: {bad}")


# ── p6: имена кодов, 40599 и голос характера (п.6, В5, В7) ──────────────────

async def p6(conn):
    print("\n== p6: имена панельных замечаний по коду, 40599 = 0 «нет команды (0)», голос характера ==")
    from analytics.labels import panel_fault_name
    from analytics.serializer import _det_name
    t_to = min(_P2_TO, datetime.now(U))
    cfg = _cfg("6003790403")
    rows = await conn.fetch("""
        SELECT id, router_sn, panel_id, addr, bit, severity, t_open, segment_id_open, open_values_json ov
        FROM alarm_episodes
        WHERE source = 'panel' AND t_open >= $1 AND t_open < $2 ORDER BY t_open""", _P2_FROM, t_to)
    coded = []
    for r in rows:
        ov = _j(r["ov"], {})
        if ov.get("panel_code") is None:
            continue
        new = panel_fault_name(ov, _cfg(r["router_sn"]).fault_code_name)
        coded.append((r["id"], r["segment_id_open"], r["addr"], r["bit"], ov.get("fault_name"), new))
        print(f"  эпизод #{r['id']} (сегмент #{r['segment_id_open']}) {r['addr']}/{r['bit']} {f(r['t_open'])}: "
              f"«{ov.get('fault_name')}» → «{new}»")
    print(f"  панельных эпизодов за 30 дней: {len(rows)}, с привязанным кодом: {len(coded)}")
    want = {(40401, 13): "Разнос двигателя — превышение частоты вращения (код 234)",
            (40408, 6): "Load Dump — сигнал разгрузки при перегрузке или пониженной частоте (код 1464)"}
    check({(a, b): n for _i, _s, a, b, _o, n in coded} == want,
          f"имена по коду: #35941 — «{want[(40401, 13)]}», #35944 — «{want[(40408, 6)]}»")
    # Перепрогон ДГУ №1 16.10 09:20–09:40: детекции панели, лента и голос акта #35941
    segs, inc, _c = await _replay("ДГУ №1", M(16, 9, 20), M(16, 9, 40), cfg)
    dn = sorted({_det_name(d.to_dict()) for s in segs for sub in s.subsegments for d in sub.detections
                 if d.scenario == "CONTROLLER_FAULT" and (d.values or {}).get("panel_code") is not None})
    print(f"  перепрогон: имена панели с кодом в отчёте: {dn}")
    check(want[(40401, 13)] in dn and want[(40408, 6)] in dn, "отчёт перепрогона — имена кодов 234 и 1464")
    a41 = inc.get(M(16, 9, 28, 53).isoformat()) or {}
    ev = [e for e in a41.get("chronology") or [] if e.get("addr") == 40599]
    print(f"  акт #35941, 40599 в ленте: {[(fmt_local(e.get('ts'), MSK), e.get('value'), e.get('label')) for e in ev]}")
    if ev:
        print(f"       имя регистра: {ev[0].get('name')}")
    check(any(e.get("value") == 0 and e.get("label") == "нет команды (0)" for e in ev)
          and not any("EmergencyStop" in str(e.get("label")) for e in ev),
          "акт #35941: 40599 = 0 подписан «нет команды (0)»")
    votes = (a41.get("character") or {}).get("immediate_votes") or []
    print(f"  голос характера #35941: {votes}")
    check("команда «Работа» снята сразу (3→0), без «Стоп»" in votes and not any("EmergencyStop" in v for v in votes),
          "голос характера #35941 — «команда «Работа» снята сразу (3→0), без «Стоп»»")
    # Справочно: каталог БД телеметрии (задача декодеру, В5/В7)
    for a, b in ((40401, 13), (40411, 10), (40408, 6)):
        x = await conn.fetchrow("""SELECT states_json->$2::text sj FROM register_catalog
                                   WHERE equip_type='pcc' AND addr=$1""", a, str(b))
        print(f"  каталог {a}/{b}: {x and _j(x['sj'], {}).get('name_ru')}")
    x = await conn.fetchrow("SELECT states_json FROM register_catalog WHERE equip_type='pcc' AND addr=40599")
    print(f"  каталог 40599 labels_ru: {x and (_j(x['states_json'], {}).get('labels_ru'))}")


SECTIONS = (("p1", p1), ("act", act), ("p7", p7), ("p8", p8), ("p2", p2), ("p2x", p2x), ("p3", p3), ("p5", p5),
            ("p6", p6))


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn-file", help="DSN одной БД (источник и аналитика) — иначе config.yml")
    ap.add_argument("sections", nargs="*",
                    help="разделы: p1 act p7 p8 (этап 1), p2 p2x (этап 2), p3 (этап 3), p5 p6 (этап 4); "
                         "по умолчанию все")
    args = ap.parse_args()
    if args.dsn_file:
        dsn = Path(args.dsn_file).read_text(encoding="utf-8").strip()
        src_dsn = dsn
    else:
        from config import settings
        dsn, src_dsn = settings.analytics_db_url, settings.source_db_url
    print("Сверка v5.1.23 на реальных данных. Соединение только для чтения, запись НЕ производится.")
    from analytics import source as _src
    conn = await asyncpg.connect(dsn, server_settings=RO)
    _src._pool = await asyncpg.create_pool(src_dsn, min_size=1, max_size=3, command_timeout=600,
                                           server_settings=RO)
    run_all = not args.sections
    try:
        assert await conn.fetchval("SHOW default_transaction_read_only") == "on"
        for name, fn in SECTIONS:
            if run_all or name in args.sections:
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
