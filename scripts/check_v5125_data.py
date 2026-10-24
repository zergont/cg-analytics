# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""v5.1.25 «Честные цифры и тексты»: сверка на реальных данных (план
docs/plans/design_v5125.md, §6). ТОЛЬКО ЧТЕНИЕ — соединения с
default_transaction_read_only (проверка SHOW), online.db._connect подменён на
соединение только для чтения, запись не производится, LLM и шлюза нет.

Этап 1 (§2 плана):
- att: перепрогон ДЭС №3 24.10 12:30–14:25 МСК кодом движка (_run_segment):
  «Стоп» 12:38:59–12:41:05; «Неудачный пуск · Стоп» с 12:41:05, 13:34:03,
  13:35:05 (до 13:40:11); «Работа» 13:40:11 с профилем как у #36076; плашки
  14:05:49 и 14:11:50 — «Неудачный пуск · Стоп»; у всех пяти rpm_in == 0 и
  факты «… прокрутка по данным не видна (отсчётов внутри попытки нет) …»
  (строки целиком); профиля у плашек нет;
- att30: все выходы из стопа за 30 дней по 24.10 (критика SHOULD 11) — до
  (confirm_exit v5.1.24) и после (П1/П2): подтверждение снято ровно у одного
  выхода — ДЭС №3 24.10 12:41:05; у остальных «Работ» confirmed_at и профиль
  прежние;
- lbl: подписи хранимых плашек за 30 дней при чтении: #35935/#35937 —
  «Неудачный пуск · Стоп»; #36072/#36074/#36080/#36082 — «Отменённый пуск ·
  Стоп» (записи без rpm_in не перестраиваются, §7);
- clf: акты за 45 дней — голоса при чтении через путь API (_incident_ru из
  web/routes.py), вход совета и реконструкцию корпуса: английских голосов нет,
  каждая старая строка переведена (критика M2);
- km: закрытые «Работы» 17.09–17.10 с таблицей «Ключевые показатели»;
  короткие перепрогоняются кодом движка, число считается и печатается
  (критика SHOULD 4; 24.10 — 4 из 36: #35782, #35865, #35900, #35929; у
  #35850/#35854/#35859 таблицы больше нет — работа переходная); #35929 —
  «Короткая работа (20 с, 2 отсчёта): обороты 1696 → 1247 об/мин, частота
  56,8 → 41,5 Гц»;
- vio: «(идёт)» через _close_window (критика M3) с подменой записи: #35929 —
  «(до конца работы)», суточный рез ДЭС №3 22–23.10 (#36040) — «(продолжается
  после 09:00)»;
- ls: LOAD_STEP — 9 онсетов CAUTION 18.08–17.10 из alarm_episodes и кандидаты
  по SQL (соседние отсчёты мощности с Δt ≥ 1 с и |ΔP|/Δt > 50 кВт/с) за
  25.08–24.10: перепрогон — 0 CAUTION; INFO — только 31.08 и 02.09 (ДГУ №1);
  онсет — первая пара выше порога, «не более N с (шаг опроса M с, норма G3
  не проверяется)»; M — по пакетам подсегмента (в августе у ДГУ №1 ~32 с, план
  «9–10 с» — замер 24.10: печатается шаг за последние 6 ч).

Этап 2 (§3 плана):
- p23: 30 дней — период панели в сохранённой Сводке против эпизода в БД
  («было» — ровно 3 завышения: #35944 +60, #35831 +11, #35845 +17 с);
  перепрогон трёх сегментов с состоянием эпизодов на момент закрытия и
  известными концами — 0; #35944 — «Сброс нагрузки» 1 мин 51 с, счётчики на
  момент закрытия 111/120 и 70/81; count_episodes_batch(ends=…) на БД;
- p5: ДЭС №3 24.10 — окно до 12:46:50: 422 живая, кандидатов нет; до 12:47:00 —
  кандидаты 422 и 197, existing_episode_onsets находит 15017/15018 (вставок 0),
  Сводка открытого — «(код 422)» 26 с и «(код 197)» 10 с; наложения эпизодов
  одного бита за 30 дней (SHOULD 2);
- ca: #36064 — «Общая авария: Отказ отключения автомата … (код 1453)»; #36067 —
  код 1248 и «код сменился 1453 → 1248» (до и после слияния в БД); #36068 — без
  пометки (M4); смена кода в живом цикле тем же UPDATE; закрытие 14901 в цикле
  12:42:15 — «superseded»; суффикс «перекрыта …» в #36070; «Висит также» за 30
  дней по сохранённым данным (M8); P3 — segment_id_open NULL;
- p10: #36065 с порядком поступления строк (received_at, created_at разрывов):
  последние данные до обрыва 05:48:15 (не 11:48:09 из памяти — M1), оценка по
  40070 «до ~12:05–12:08», 6 ч 18 мин, попытки пуска 40069 не менялись.

Этап 3 (§4.1 плана, решение В-25.2):
- badge: значок акта — акты за 45 дней по одному (get_incident_analysis_state,
  путь /api/segment/{id}): 6 ready, 2 failed (#35282, #35492 — «не
  заказывался»), 0 pending; календари сентября и октября трёх машин путём
  /segments (get_segments_for_calendar(incident_state=True)): значки строк с
  актом совпадают с поштучными, прочие строки — none; второй запрос
  календаря — только при актах; время запросов печатается.

Ревью v5.1.25 (подтверждённые замечания):
- ca: Сводка #36070 — настоящим путём (_filter_window_episodes с 120 с, затем
  _drop_debounce_tails): «superseded» через 62 с от начала окна в Сводке (Q4);
- p10: #36065 — режим на обрыве 3 из БД; #35641 (29.09, стоял на обрыве 22.09)
  — с 40069 «были остановы и пуски», без роли счётчика пусков (как KTA50) —
  только наработка, без «не работал (остановился ~…)» (Q1);
- rv: Q3 — покадровый прогон панели (детектор, _process_episodes, посев П5)
  на периодах из БД: ДЭС №3 13.10 1438 и 16.09 код 197 — эпизод на каждый
  фронт без наложений во всех фазах опроса; Q5 — пуски 30.09 20:33:17 и
  01.10 20:08:18 в разрыве связи — пометка без масла и момента автомата;
  Q2/Q7 — открытая Сводка ДГУ №1/№2 24.10 после 09:00 — «С 09:00 — …»;
  Q8 — у #35282/#35492 разборов совета нет (UI — причина и разбор гейта).

Запуск:
    py -3 scripts/check_v5125_data.py --dsn-file .dsn            # все разделы
    py -3 scripts/check_v5125_data.py --dsn-file .dsn att lbl     # выбранные
Код возврата 1 — есть расхождения.
"""
from __future__ import annotations

import argparse
import ast
import asyncio
import json
import logging
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import asyncpg  # noqa: E402

import analytics.segmenter as _sg  # noqa: E402
from analytics.labels import fmt_local  # noqa: E402
from online import engine as E  # noqa: E402

MSK = ZoneInfo("Europe/Moscow")
U = timezone.utc
RO = {"default_transaction_read_only": "on"}
MACH = {"ДГУ №1": ("6003790403", "pcc", 1), "ДГУ №2": ("6003790403", "pcc", 2),
        "ДЭС №3": ("1126246373", "pcc", 1)}
_ENG = {"6003790403": "cummins_kta50", "1126246373": "cummins_qsx15"}
_LAT3 = re.compile(r"[A-Za-z]{3,}")
_fails: list[str] = []
_DSN: str | None = None


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


def _cfg(sn: str):
    from analytics.config import AnalyticsConfig
    return AnalyticsConfig.from_pair(ROOT / "knowledge_base", "pcc3300", _ENG.get(sn, "cummins_kta50"))


async def _replay(key: str, a: datetime, b: datetime, cfg=None):
    """Окно кодом движка (_run_segment): сегменты и акты, только чтение источника."""
    from analytics.contract import CokingRisk
    sn, et, p = MACH[key]
    cfg = cfg or _cfg(sn)
    segs, inc, _g, _c = await E._run_segment(sn, et, p, "", a, b, cfg, CokingRisk())
    return segs, inc, cfg


def _lab(s) -> str:
    from analytics.labels import label_from_chars
    from analytics.serializer import RUN_STATE_RU
    return label_from_chars(s.to_dict()) or RUN_STATE_RU.get(s.run_state, str(s.run_state))


async def _ro_connect():
    """online.db._connect на время сверки: отдельное соединение только для чтения."""
    return await asyncpg.connect(_DSN, server_settings=RO)


# ── Подтверждение выхода до v5.1.25 (v5.1.24, для «было» в att30) ─────────────

def _confirm_exit_v5124(t0, t1, rpm, freq, ready, gaps, prm, ready_since=None):
    """confirm_exit v5.1.24 без П1/П2: бит засчитывается при любом пересечении."""
    t0, t1 = _sg._tz(t0), _sg._tz(t1)
    facts: dict[str, Any] = {"rpm_max": None, "freq_max": None, "ready": False}
    hits: list[datetime] = []
    evidence = False
    for series, thr, key in ((rpm, prm["rpm_min"], "rpm_max"), (freq, prm["freq_min"], "freq_max")):
        vals: list[float] = []
        carried = None
        first_hit = None
        for ts, v in series:
            if ts < t0:
                carried = v if (t0 - ts).total_seconds() <= prm["carry_sec"] else None
                continue
            if ts >= t1:
                break
            vals.append(v)
            if first_hit is None and v > thr:
                first_hit = ts
        if carried is not None:
            vals.append(carried)
            if carried > thr:
                first_hit = t0
        if vals:
            evidence = True
            facts[key] = max(vals)
        if first_hit is not None:
            hits.append(first_hit)
    for s, e in ready:
        if s < t1 and e > t0:
            facts["ready"] = True
            hits.append(max(s, t0))
    if hits:
        return "confirmed", min(hits), facts
    dur = (t1 - t0).total_seconds()
    spans = []
    for g in gaps or []:
        gs = max(_sg._tz(g["gap_start"]), t0)
        ge_raw = g.get("gap_end")
        ge = min(_sg._tz(ge_raw) if ge_raw else t1, t1)
        if ge > gs:
            spans.append((gs, ge))
    blind = _sg.union_seconds(spans)
    if not evidence or (dur > 0 and blind / dur >= prm["no_data_share"]):
        return "no_data", None, facts
    return "unconfirmed", None, facts


# ══ att: ДЭС №3 24.10 12:30–14:25 ═══════════════════════════════════════════

async def att(conn):
    print("\n== att: перепрогон ДЭС №3 24.10 12:30–14:25 МСК (П1/П2, П3, П-А) ==")
    from analytics.labels import attempt_facts_ru
    segs, _inc, _cfg3 = await _replay("ДЭС №3", M(24, 12, 30), M(24, 14, 25))
    rows = []
    for s in segs:
        lab = _lab(s)
        sp = getattr(s, "start_profile", None)
        att_ = s.attempt if isinstance(s.attempt, dict) else None
        print(f"  {f(s.t_start)}–{f(s.t_end)}: {lab}"
              + (f"; профиль: номинал {sp.get('rated_sec')} с, обороты {sp.get('rpm')}" if sp else "")
              + (f"; rpm_in {att_.get('rpm_in')}, cranked {att_.get('cranked')}" if att_ else ""))
        if att_:
            print(f"      факты: {attempt_facts_ru(att_, MSK, codes=True)}")
        rows.append((_u(s.t_start), _u(s.t_end) if s.t_end else None, lab, sp, att_))
    by = {r[0]: r for r in rows}
    st = by.get(M(24, 12, 38, 59))
    check(st is not None and st[2] == "Стоп" and st[1] == M(24, 12, 41, 5), "«Стоп» 12:38:59–12:41:05")
    want = {M(24, 12, 41, 5): M(24, 13, 34, 3), M(24, 13, 34, 3): M(24, 13, 35, 5),
            M(24, 13, 35, 5): M(24, 13, 40, 11), M(24, 14, 5, 49): None, M(24, 14, 11, 50): None}
    for t, te in want.items():
        r = by.get(t)
        facts = attempt_facts_ru(r[4], MSK) if r and r[4] else ""
        check(r is not None and r[2] == "Неудачный пуск · Стоп" and (te is None or r[1] == te)
              and r[4].get("rpm_in") == 0 and r[4].get("cranked") is None
              and "прокрутка по данным не видна (отсчётов внутри попытки нет)" in facts and r[3] is None,
              f"{f(t)}: «Неудачный пуск · Стоп»" + (f" до {f(te)}" if te else "")
              + f", отсчётов внутри нет, профиля нет: {r and (r[2], f(r[1]))} / {facts}")
    w = by.get(M(24, 13, 40, 11))
    ref = await conn.fetchval("SELECT characteristics_json->'start_profile' FROM auto_segments WHERE id = 36076")
    ref = _j(ref, {}) or {}
    sp = (w[3] if w else None) or {}
    print(f"  #36076 в БД: номинал {ref.get('rated_sec')} с, обороты {ref.get('rpm')}; перепрогон: "
          f"{sp.get('rated_sec')} с, {sp.get('rpm')}")
    check(w is not None and w[2] == "Работа" and sp and sp.get("rated_sec") == ref.get("rated_sec")
          and sp.get("rpm") == ref.get("rpm") and sp.get("crank_at") == ref.get("crank_at"),
          "«Работа» 13:40:11 с профилем как у #36076 (П2 не сдвинул подтверждение)")


# ══ att30: выходы из стопа за 30 дней — до и после П1/П2 ═══════════════════

_ATT30_FROM, _ATT30_TO = M(24, 0, 0, mo=9), M(25, 0, 0)


async def _exits(conn, sn, et, p, a, b) -> list[datetime]:
    per = [dict(r) for r in await conn.fetch("""
        SELECT value, state_start FROM enum_history
        WHERE router_sn=$1 AND equip_type=$2 AND panel_id=$3 AND addr=40011
          AND state_start < $5 AND (state_end IS NULL OR state_end > $4)
        ORDER BY state_start""", sn, et, p, a - timedelta(days=2), b)]
    out, prev = [], None
    for r in per:
        v = int(r["value"])
        if v in (1, 2, 3) and prev == 0 and a <= _u(r["state_start"]) < b:
            out.append(_u(r["state_start"]))
        prev = v
    return out


def _exit_view(segs, t: datetime) -> tuple:
    """Что получилось на выходе t: (вид, confirmed_at, ключ профиля)."""
    for s in segs:
        s0 = _u(s.t_start)
        s1 = _u(s.t_end) if s.t_end else None
        if abs((s0 - t).total_seconds()) <= 2 or (s0 <= t and (s1 is None or t < s1) and s.attempt):
            kind = ("plate" if s.attempt else "pending" if s.attempt_pending
                    else "work" if s.run_state == 3 else f"rs{s.run_state}")
            sp = getattr(s, "start_profile", None)
            # confirmed_at живёт в периоде нарезки, у сегмента его нет: «схватил»
            # профиля — это он (fire = confirmed_at, segmenter._start_origin)
            conf = sp.get("fire_at") if isinstance(sp, dict) else None
            pk = (tuple((k, json.dumps(sp.get(k), default=str)) for k in
                        ("crank_at", "fire_at", "rated_at", "rated_sec", "rpm", "freq", "t_to", "end_by",
                         "unstable")) if isinstance(sp, dict) else None)
            return kind, conf, pk, _lab(s)
    return ("none", None, None, None)


async def att30(conn):
    print(f"\n== att30: все выходы из стопа {f(_ATT30_FROM)}–{f(_ATT30_TO)} — до (v5.1.24) и после П1/П2 ==")
    t_to = min(_ATT30_TO, datetime.now(U))
    diffs: list[tuple] = []
    n_exits = n_works = 0
    for key, (sn, et, p) in MACH.items():
        cfg = _cfg(sn)
        exits = await _exits(conn, sn, et, p, _ATT30_FROM, t_to)
        n_exits += len(exits)
        wins: list[list[datetime]] = []
        for s in exits:
            a, b = s - timedelta(minutes=10), min(s + timedelta(minutes=40), t_to)
            if wins and a <= wins[-1][1]:
                wins[-1][1] = max(wins[-1][1], b)
            else:
                wins.append([a, b])
        t0 = time.monotonic()
        for a, b in wins:
            new, _i, _c = await _replay(key, a, b, cfg)
            orig = _sg.confirm_exit
            _sg.confirm_exit = _confirm_exit_v5124
            try:
                old, _i2, _c2 = await _replay(key, a, b, cfg)
            finally:
                _sg.confirm_exit = orig
            for s in exits:
                if not (a <= s < b):
                    continue
                vn, vo = _exit_view(new, s), _exit_view(old, s)
                if vn[0] == "work":
                    n_works += 1
                if vn[:3] != vo[:3]:
                    diffs.append((key, s, vo, vn))
        print(f"  {key}: выходов {len(exits)}, окон {len(wins)} ({time.monotonic() - t0:.0f} с)")
    print(f"  всего выходов {n_exits}, «Работ» после {n_works}; расхождений до/после: {len(diffs)}")
    for key, s, vo, vn in diffs:
        print(f"    {key} {f(s)}: было {vo[0]} «{vo[3]}» (подтверждён {f(vo[1])}), "
              f"стало {vn[0]} «{vn[3]}» (подтверждён {f(vn[1])})"
              + ("; профиль изменился" if vo[2] != vn[2] and vo[0] == vn[0] == "work" else ""))
    check([(k, s) for k, s, _o, _n in diffs] == [("ДЭС №3", M(24, 12, 41, 5))],
          "подтверждение снято ровно у одного выхода — ДЭС №3 24.10 12:41:05; у остальных «Работ» "
          "confirmed_at и профиль прежние")


# ══ lbl: подписи хранимых плашек при чтении ════════════════════════════════

async def lbl(conn):
    print("\n== lbl: подписи хранимых плашек за 30 дней при чтении (решение А, записи не перестраиваются) ==")
    from analytics.labels import attempt_facts_ru, label_from_chars
    rows = await conn.fetch("""
        SELECT id, router_sn, equip_type, panel_id, t_start, t_end, characteristics_json ch
        FROM auto_segments
        WHERE t_start >= $1 AND jsonb_typeof(characteristics_json->'attempt') = 'object'
        ORDER BY t_start""", _ATT30_FROM)
    names = {v: k for k, v in MACH.items()}
    got: dict[int, str] = {}
    for r in rows:
        ch = _j(r["ch"], {})
        a = ch.get("attempt") or {}
        got[r["id"]] = label_from_chars(ch)
        print(f"  #{r['id']} {names.get((r['router_sn'], r['equip_type'], r['panel_id']), '?')} "
              f"{f(r['t_start'])}: «{got[r['id']]}» (cranked {a.get('cranked', '—')}, rpm_in "
              f"{a.get('rpm_in', '—')}): {attempt_facts_ru(a, MSK)}")
    want = {35935: "Неудачный пуск · Стоп", 35937: "Неудачный пуск · Стоп",
            36072: "Отменённый пуск · Стоп", 36074: "Отменённый пуск · Стоп",
            36080: "Отменённый пуск · Стоп", 36082: "Отменённый пуск · Стоп"}
    for sid, w in want.items():
        check(got.get(sid) == w, f"#{sid}: «{w}» при чтении ({got.get(sid)})")


# ══ clf: голоса характера в актах ══════════════════════════════════════════

def _routes_fn(name: str):
    src = (ROOT / "web" / "routes.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == name)
    ns: dict = {"Any": Any, "logger": logging.getLogger("online.check_routes")}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), f"routes.{name}", "exec"), ns)
    return ns[name]


async def clf(conn):
    print("\n== clf: акты за 45 дней — голоса характера при чтении (API, вход совета, корпус) ==")
    from analytics.classifier import _VOTES_OLD_RU
    from corpus.preprocessor import _format_incident
    from online.incident_gate import build_incident_prompt
    inc_ru = _routes_fn("_incident_ru")
    rows = await conn.fetch("""
        SELECT id, router_sn, panel_id, t_start, incident_json FROM auto_segments
        WHERE incident_json IS NOT NULL AND t_start > now() - interval '45 days' ORDER BY t_start""")
    print(f"  актов: {len(rows)}")
    bad = []
    old_seen: set[str] = set()
    for r in rows:
        inc = _j(r["incident_json"], {})
        ch = inc.get("character") or {}
        raw = (ch.get("immediate_votes") or []) + (ch.get("controlled_votes") or [])
        old_seen |= {v for v in raw if v in _VOTES_OLD_RU}
        api = inc_ru(inc)
        ach = api.get("character") or {}
        votes = (ach.get("immediate_votes") or []) + (ach.get("controlled_votes") or [])
        pr = build_incident_prompt(inc, None, [], {}, tz=MSK)
        pr_ch = [x for x in pr.splitlines() if x.startswith(("Характер по", "Сигналы:"))]
        fi = _format_incident(inc)
        fi_ch = [x for x in fi.splitlines() if x.startswith(("**Характер", "**Сигналы", "**За "))]
        print(f"  #{r['id']} {f(r['t_start'])}: {raw} → {votes}")
        for x in pr_ch + fi_ch:
            print(f"      {x}")
        if any(_LAT3.search(v) for v in votes) or any(_LAT3.search(x) for x in pr_ch + fi_ch):
            bad.append(r["id"])
    print(f"  старых строк в актах: {sorted(old_seen)}")
    check(rows and not bad, f"английских голосов и подписей характера нет ни в API, ни во входе совета, "
                            f"ни в корпусе: {bad}")
    check(old_seen and old_seen <= set(_VOTES_OLD_RU), "каждая старая строка из актов есть в словаре перевода")


# ══ km: «Обычно» короткой работы ═══════════════════════════════════════════

_KM_FROM, _KM_TO = M(17, 0, 0, mo=9), M(17, 0, 0)


async def km(conn):
    print(f"\n== km: «Ключевые показатели» закрытых «Работ» {f(_KM_FROM)}–{f(_KM_TO)} (В-25.3) ==")
    from analytics.serializer import build_summary_parts, render_summary_md
    rows = await conn.fetch("""
        SELECT id, router_sn, equip_type, panel_id, t_start, t_end, report_summary_json->'key_metrics' km
        FROM auto_segments
        WHERE run_state = 3 AND t_end IS NOT NULL AND t_start >= $1 AND t_start < $2
          AND jsonb_typeof(report_summary_json->'key_metrics') = 'object'
        ORDER BY t_start""", _KM_FROM, _KM_TO)
    names = {v: k for k, v in MACH.items()}
    print(f"  «Работ» с таблицей: {len(rows)}")
    short: dict[int, str] = {}
    for r in rows:
        k = _j(r["km"], {})
        dur = (_u(r["t_end"]) - _u(r["t_start"])).total_seconds()
        if dur > 7200 and float(k.get("work_raw_sec") or 0) >= 120:
            continue        # долгая работа: отсчётов оборотов заведомо больше 10
        key = names.get((r["router_sn"], r["equip_type"], r["panel_id"]))
        segs, _i, cfg = await _replay(key, _u(r["t_start"]), _u(r["t_end"]))
        works = [s for s in segs if s.run_state == 3 and not getattr(s, "attempt_pending", False)]
        if not works:
            print(f"  #{r['id']} {key} {f(r['t_start'])}: в перепрогоне «Работы» нет")
            continue
        parts = build_summary_parts(works, [], tz=MSK, trip_roles=cfg.trip_snapshot_roles)
        kmp = parts.get("key_metrics") or {}
        md = render_summary_md(parts)
        line = next((x for x in md.splitlines() if x.startswith(("Короткая работа", "Работа идёт"))), None)
        print(f"  #{r['id']} {key} {f(r['t_start'])} ({dur:.0f} с, в БД «Обычно» оборотов "
              f"{next((x.get('median') for x in k.get('rows') or [] if x.get('role') == 'RPM'), '—')}): "
              + (line or ("не короткая" if kmp else
                          "таблицы нет — вся работа переходная (с v5.1.16) или стала плашкой")))
        if kmp.get("short"):
            short[r["id"]] = line or ""
    print(f"  коротких: {len(short)} из {len(rows)}: {sorted(short)}")
    check(short.get(35929) == "Короткая работа (20 с, 2 отсчёта): обороты 1696 → 1247 об/мин, "
                              "частота 56,8 → 41,5 Гц",
          f"#35929 — точный текст пояснения: {short.get(35929)}")
    check(all(x.startswith("Короткая работа (") for x in short.values()),
          "у всех коротких — пояснение под таблицей")


# ══ vio: «(идёт)» через _close_window ══════════════════════════════════════

class _CW:
    """Подмена записи online.db в _close_window: строки только запоминаются."""

    def __init__(self):
        self.inserted: list[dict] = []

    async def pop_open_segment(self, *a, **k):
        return None

    async def upsert_open_segment(self, data):
        return -1

    async def insert_closed_segment(self, data):
        self.inserted.append(data)
        return -len(self.inserted)

    async def retarget_episode_segments(self, *a, **k):
        return 0

    async def insert_detection_events(self, *a, **k):
        return None

    async def append_segment_gate_state(self, *a, **k):
        return None

    async def set_episodes_gate_src_segment(self, *a, **k):
        return 0


_CW_NAMES = ("pop_open_segment", "upsert_open_segment", "insert_closed_segment", "retarget_episode_segments",
             "insert_detection_events", "append_segment_gate_state", "set_episodes_gate_src_segment")


async def _close_window_ro(key: str, t_from: datetime, t_to: datetime, reason: str) -> list[dict]:
    """_close_window движка на реальных данных: запись в БД подменена, посев и
    сводка выключены (вне раздела), источник — только чтение."""
    from analytics.contract import CokingRisk
    import online.verifier as _ver
    sn, et, p = MACH[key]
    cw = _CW()
    orig = {n: getattr(E.online_db, n) for n in _CW_NAMES}
    o_collect, o_fv = E._collect_and_enrich_detections, _ver.fire_verify

    async def _empty(*a, **k):
        return []

    async def _noop(*a, **k):
        return None

    async def _no_summary(*a, **k):
        return None, None

    for n in _CW_NAMES:
        setattr(E.online_db, n, getattr(cw, n))
    E._collect_and_enrich_detections = _empty
    _ver.fire_verify = lambda *a, **k: None
    try:
        e = E.OnlinePollEngine.__new__(E.OnlinePollEngine)
        e.cfg, e.tz = _cfg(sn), MSK
        e.router_sn, e.equip_type, e.panel_id, e.engine_sn = sn, et, p, ""
        e.inherited_coking_risk, e.inherited_run_state_sec = CokingRisk(), {}
        e.continued_from_id, e._prev_seg_hint, e._transition_ctx = None, None, []
        e._episodes, e._act_done_for, e.poll_interval_sec = {}, None, 30
        e.cursor_ts, e.last_processed_to, e.forward_fill_memory = t_from, None, None
        e._open_history_cache = e._open_history_cache_ts = None
        e._active_alerts, e._fault_ref = {}, None
        e.last_data_ts = None
        e._open_load_seed = _noop
        e._build_summary_md_for = _no_summary
        e._order_incident_analysis = lambda *a, **k: None
        e._refine_until = lambda segs: None
        await e._close_window(t_from, t_to, reason, lookahead_to=t_to, live_to=t_to)
    finally:
        for n, fn in orig.items():
            setattr(E.online_db, n, fn)
        E._collect_and_enrich_detections, _ver.fire_verify = o_collect, o_fv
    return cw.inserted


def _vio_lines(md: str | None) -> list[str]:
    return [x for x in (md or "").splitlines() if x.strip().startswith("- Нарушение")]


async def vio(conn):
    print("\n== vio: подпись идущего нарушения через _close_window (критика M3) ==")
    ins = await _close_window_ro("ДГУ №1", M(16, 9, 25, 52), M(16, 9, 27), "DAILY_BOUNDARY")
    w = next((d for d in ins if _u(d["t_start"]) == M(16, 9, 25, 52)), None)
    lines = _vio_lines(w and w.get("report_md"))
    print(f"  #35929 (перепрогон 09:25:52–09:27): сегментов {len(ins)}, причина {w and w.get('cause_close')}")
    for x in lines:
        print(f"    {x.strip()}")
    check(w is not None and any("(до конца работы)" in x for x in lines) and not any("(идёт)" in x for x in lines),
          "#35929: «(до конца работы)» вместо «(идёт)»")
    ins = await _close_window_ro("ДЭС №3", M(22, 9, 0), M(23, 9, 0), "DAILY_BOUNDARY")
    last = ins[-1] if ins else None
    lines = _vio_lines(last and last.get("report_md"))
    print(f"  суточный рез ДЭС №3 22.10 09:00 – 23.10 09:00 (#36040): сегментов {len(ins)}, "
          f"последний {last and f(last['t_start'])}–{last and f(last['t_end'])} {last and last.get('cause_close')}")
    for x in lines:
        print(f"    {x.strip()[:220]}")
    check(last is not None and last.get("cause_close") == "DAILY_BOUNDARY"
          and any("(продолжается после 09:00)" in x for x in lines) and not any("(идёт)" in x for x in lines),
          "суточный рез — «(продолжается после 09:00)»")


# ══ ls: LOAD_STEP ══════════════════════════════════════════════════════════

_LS_INFO = {("ДГУ №1", "31.08"), ("ДГУ №1", "02.09")}


async def _seg_window(conn, sn, et, p, t: datetime) -> tuple[datetime, datetime] | None:
    r = await conn.fetchrow("""
        SELECT t_start, t_end FROM auto_segments
        WHERE router_sn=$1 AND equip_type=$2 AND panel_id=$3 AND run_state = 3
          AND t_start <= $4 AND (t_end IS NULL OR t_end > $4) ORDER BY t_start DESC LIMIT 1""", sn, et, p, t)
    return (_u(r["t_start"]), _u(r["t_end"])) if r and r["t_end"] else None


async def ls(conn):
    print("\n== ls: LOAD_STEP — 9 онсетов CAUTION 18.08–17.10 и кандидаты по SQL 25.08–24.10 (В-25.1) ==")
    names = {v: k for k, v in MACH.items()}
    eps = await conn.fetch("""
        SELECT DISTINCT router_sn, equip_type, panel_id, t_open FROM alarm_episodes
        WHERE scenario = 'LOAD_STEP' AND severity = 'CAUTION' AND t_open >= $1 AND t_open < $2
        ORDER BY t_open""", M(18, 0, 0, mo=8), M(18, 0, 0))
    print(f"  онсетов CAUTION в alarm_episodes: {len(eps)} — "
          f"{[(names.get((r['router_sn'], r['equip_type'], r['panel_id'])), f(r['t_open'])) for r in eps]}")
    check(len(eps) == 9, "9 онсетов LOAD_STEP CAUTION за 60 дней")
    wins: dict[tuple, tuple] = {}
    for r in eps:
        key = names.get((r["router_sn"], r["equip_type"], r["panel_id"]))
        w = await _seg_window(conn, r["router_sn"], r["equip_type"], r["panel_id"], _u(r["t_open"]))
        if w:
            wins[(key, w)] = ("эпизод", _u(r["t_open"]))
    # Кандидаты по SQL: соседние отсчёты мощности с Δt ≥ 1 с и |ΔP|/Δt > 50 кВт/с при 40011 = 3
    for key, (sn, et, p) in MACH.items():
        cfg = _cfg(sn)
        addr = cfg.role_to_addr("ACTIVE_POWER_TOTAL")
        cand = await conn.fetch("""
            WITH x AS (
                SELECT ts, value, lag(ts) OVER w pts, lag(value) OVER w pv FROM history_rich
                WHERE router_sn=$1 AND equip_type=$2 AND panel_id=$3 AND addr=$4 AND ts >= $5 AND ts < $6
                WINDOW w AS (ORDER BY ts))
            SELECT ts, value, pts, pv, abs(value - pv) / EXTRACT(EPOCH FROM ts - pts) spd FROM x
            WHERE pts IS NOT NULL AND EXTRACT(EPOCH FROM ts - pts) >= 1
              AND abs(value - pv) / EXTRACT(EPOCH FROM ts - pts) > 50
            ORDER BY ts""", sn, et, p, addr, M(25, 0, 0, mo=8), min(M(25, 0, 0), datetime.now(U)))
        print(f"  {key}: кандидатов по SQL {len(cand)}")
        for c in cand:
            w = await _seg_window(conn, sn, et, p, _u(c["ts"]))
            if w is None:
                continue
            wins.setdefault((key, w), ("SQL", _u(c["ts"])))
            print(f"    {f(c['pts'])} → {f(c['ts'])}: {float(c['pv']):.0f} → {float(c['value']):.0f} кВт, "
                  f"{float(c['spd']):.0f} кВт/с; «Работа» {f(w[0])}–{f(w[1])}")
    caution, info = [], []
    for (key, (a, b)), (src, t) in sorted(wins.items(), key=lambda x: x[0][1][0]):
        segs, _i, cfg = await _replay(key, a, b)
        for s in segs:
            for sub in s.subsegments:
                for d in sub.detections:
                    if d.scenario != "LOAD_STEP":
                        continue
                    dm = sub.derived_metrics
                    print(f"  {key} {f(a)}–{f(b)} ({src} {f(t)}): {d.severity} онсет {f(d.t_detected)}, "
                          f"наибольшая {f(dm.dP_dt_max_at)}; {d.trigger}")
                    print(f"      «Производные»: freq_recovery_sec {dm.freq_recovery_text or dm.freq_recovery_sec}, "
                          f"шаг опроса {dm.freq_poll_step_sec}")
                    (caution if d.severity == "CAUTION" else info).append((key, d, dm))
    check(not caution, f"перепрогон: CAUTION LOAD_STEP — 0 ({[(k, f(d.t_detected)) for k, d, _ in caution]})")
    got_info = {(k, fmt_local(d.t_detected, MSK, day=True)[:5]) for k, d, _ in info}
    check(got_info == _LS_INFO, f"INFO — только 31.08 и 02.09 (ДГУ №1): {sorted(got_info)}")
    check(all(d.t_detected == dm.dP_dt_onset_at for _k, d, dm in info + caution),
          "t_detected — онсет (первая пара выше порога dP/dt)")
    # Шаг опроса — по пакетам самого подсегмента: в августе–сентябре у ДГУ №1
    # опрос был ~32 с, «9–10 с» плана — замер 24.10
    check(all((dm.freq_recovery_text or "").startswith("не более ") and "норма G3 не проверяется"
              in dm.freq_recovery_text and dm.freq_poll_step_sec is not None and 3 < dm.freq_poll_step_sec <= 90
              for _k, _d, dm in info if dm.freq_recovery_sec is not None),
          f"«Производные»: «не более N с (шаг опроса M с, норма G3 не проверяется)», шаг M по пакетам "
          f"подсегмента: {[dm.freq_poll_step_sec for _k, _d, dm in info]}")
    # Шаг опроса сейчас (план: ДЭС №3 — 9 с, ДГУ — 10 с): последние сутки, пакеты пинга
    from analytics.metrics import _poll_step_sec
    for key, (sn, et, p) in MACH.items():
        cfg = _cfg(sn)
        hb = cfg.seg("data_quality", "heartbeat_addr", default=None)
        t1 = datetime.now(U)
        rows = [dict(r) for r in await conn.fetch("""
            SELECT addr, ts, value FROM history_rich WHERE router_sn=$1 AND equip_type=$2 AND panel_id=$3
              AND addr=$4 AND ts >= $5 AND ts < $6 ORDER BY ts""", sn, et, p, int(hb), t1 - timedelta(hours=6), t1)]
        st = _poll_step_sec({int(hb): rows}, t1 - timedelta(hours=6), t1, cfg) if rows else None
        print(f"  шаг опроса {key} за последние 6 ч: {st} с (строк пинга {len(rows)})")


# ══════════════════════════ Этап 2 — движок ═════════════════════════════════

CA_KEY = "CONTROLLER_FAULT|40408|11"


async def _ep_rows(conn, sn, et, p, a: datetime, b: datetime, scenario: str | None = None) -> list[dict]:
    """Эпизоды, пересекающие [a, b) — как online.db.get_episodes_overlapping."""
    rows = await conn.fetch("""
        SELECT * FROM alarm_episodes
        WHERE router_sn = $1 AND equip_type = $2 AND panel_id = $3 AND t_open < $5
          AND (t_close IS NULL OR t_close > $4 OR (close_reason = 'transition_event' AND t_open >= $4))
          AND ($6::text IS NULL OR scenario = $6)
        ORDER BY t_open""", sn, et, p, a, b, scenario)
    out = []
    for r in rows:
        d = dict(r)
        d["open_values_json"] = _j(d.get("open_values_json"), {})
        out.append(d)
    return out


def _iv_union(iv: list[tuple]) -> float:
    iv = sorted((a, b) for a, b in iv if b > a)
    tot, cur = 0.0, None
    for a, b in iv:
        if cur is None or a > cur[1]:
            if cur:
                tot += (cur[1] - cur[0]).total_seconds()
            cur = [a, b]
        else:
            cur[1] = max(cur[1], b)
    if cur:
        tot += (cur[1] - cur[0]).total_seconds()
    return tot


def _panel_expected(eps: list[dict], name: str, w0: datetime, w1: datetime, cn) -> float | None:
    """Период панели в окне по эпизодам БД с этим именем (имя по коду, имя бита или
    fault_name — как в сохранённой сводке разных версий). None — эпизода нет."""
    from analytics.labels import panel_fault_name
    iv, hit = [], False
    for e in eps:
        if e.get("source") != "panel":
            continue
        ov = e.get("open_values_json") or {}
        names = {panel_fault_name(ov, cn), panel_fault_name(ov), ov.get("fault_name")}
        if name not in names:
            continue
        hit = True
        a = max(_u(e["t_open"]), w0)
        b = min(_u(e["t_close"]) if e.get("t_close") else w1, w1)
        iv.append((a, b))
    return _iv_union(iv) if hit else None


def _closing_mem(eps: list[dict], seg_end: datetime, tail_sec: float = 150.0) -> tuple[list[dict], dict]:
    """Состояние на момент закрытия сегмента по строкам БД: строка, закрытая позже
    его конца (продолжение) или за последние tail_sec до него (дебаунс — 3 цикла
    после промаха; закрывается позже цикла закрытия сегмента), тогда была
    открытой — копия с t_close None; память движка — id, начало, точный ли (с
    until = конец строки). Закрытая в дебаунсе раньше — тот же конец: известный
    конец его же и даст."""
    rows, mem = [], {}
    for e in eps:
        r = dict(e)
        tc = _u(e.get("t_close"))
        if tc is None or tc > seg_end - timedelta(seconds=tail_sec):
            r["t_close"] = None
            k = E._episode_key(r)
            ov = r.get("open_values_json") or {}
            exact = r.get("scenario") != "CONTROLLER_FAULT" and isinstance(ov.get("live"), dict)
            mem[k] = {"id": r["id"], "severity": r.get("severity"), "miss": 1, "first_miss_ts": None,
                      "t_open": _u(r["t_open"]), "exact": exact, "until": tc if exact else None,
                      "until_kind": "end" if exact else None}
        rows.append(r)
    return rows, mem


# ══ p23: известный конец открытого эпизода в сводке и счётчиках ═════════════

_P23_EXPECT = {35944: 60, 35831: 11, 35845: 17}


async def p23(conn):
    print("\n== p23: период панели в Сводке против эпизода в БД (30 дней) и известный конец (P2-3) ==")
    from analytics.serializer import build_summary_parts, render_summary_md
    names = {v: k for k, v in MACH.items()}
    t1 = M(24, 0, 0)
    t0 = t1 - timedelta(days=30)
    over: dict[int, float] = {}
    n_groups = 0
    for key, (sn, et, p) in MACH.items():
        cfg = _cfg(sn)
        segs = await conn.fetch("""
            SELECT id, t_start, t_end, report_summary_json FROM auto_segments
            WHERE router_sn=$1 AND equip_type=$2 AND panel_id=$3 AND t_end IS NOT NULL
              AND t_start >= $4 AND t_start < $5 AND report_summary_json IS NOT NULL
            ORDER BY t_start""", sn, et, p, t0, t1)
        eps_all = await _ep_rows(conn, sn, et, p, t0 - timedelta(days=30), t1 + timedelta(days=2),
                                 "CONTROLLER_FAULT")
        for s in segs:
            w0, w1 = _u(s["t_start"]), _u(s["t_end"])
            parts = _j(s["report_summary_json"], {})
            eps = [e for e in eps_all if _u(e["t_open"]) < w1
                   and (e.get("t_close") is None or _u(e["t_close"]) > w0)]
            for g in parts.get("remarks") or []:
                if g.get("source") != "panel" or "period_sec" not in g or g.get("kind"):
                    continue
                exp = _panel_expected(eps, g.get("name"), w0, w1, cfg.fault_code_name)
                if exp is None:
                    continue
                n_groups += 1
                d = float(g["period_sec"]) - exp
                if d >= 1:
                    over[s["id"]] = max(over.get(s["id"], 0.0), d)
                    print(f"  было: #{s['id']} {key} {f(w0)}–{f(w1)} «{g.get('name')}»: в Сводке "
                          f"{g['period_sec']:.0f} с, по эпизоду {exp:.0f} с (+{d:.0f} с)")
    print(f"  панельных строк сверено: {n_groups}; завышений: {len(over)}")
    check({k: round(v) for k, v in over.items()} == _P23_EXPECT,
          f"«было» — ровно 3 завышения (+60/+11/+17 с): {({k: round(v) for k, v in over.items()})}")
    # «Станет»: перепрогон трёх сегментов, состояние эпизодов на момент закрытия
    left: dict[int, float] = {}
    for sid in sorted(_P23_EXPECT):
        r = await conn.fetchrow("SELECT router_sn, equip_type, panel_id, t_start, t_end FROM auto_segments "
                                "WHERE id=$1", sid)
        key = names.get((r["router_sn"], r["equip_type"], r["panel_id"]))
        w0, w1 = _u(r["t_start"]), _u(r["t_end"])
        # Окно с минутой за концом: живой состав — у остающегося открытым (путь
        # смены режима движка), не у самого закрываемого
        segs, _i, cfg = await _replay(key, w0, w1 + timedelta(seconds=60))
        i0 = next((i for i, s in enumerate(segs) if _u(s.t_start) == w0), 0)
        seg = segs[i0] if segs else None
        if seg is None:
            check(False, f"#{sid}: перепрогон без сегмента")
            continue
        eps_db = await _ep_rows(conn, r["router_sn"], r["equip_type"], r["panel_id"], w0, w1)
        rows, mem = _closing_mem(eps_db, w1)
        live = E._live_alert_keys(segs[i0 + 1] if i0 + 1 < len(segs) else seg)
        kn = E._known_ends([seg], mem, live, w1)
        eps2 = E._drop_debounce_tails(rows, [seg], mem, live, w1=w1)
        syn = E._live_new_episodes([seg], eps2, live, w1)
        eps2 = E._drop_same_onset(eps2, syn) + syn
        parts = build_summary_parts([seg], episodes=eps2, tz=MSK, trip_roles=cfg.trip_snapshot_roles,
                                    t_from=w0, t_to=w1, code_name=cfg.fault_code_name)
        md = render_summary_md(parts)
        worst = 0.0
        for g in parts.get("remarks") or []:
            if g.get("source") != "panel" or "period_sec" not in g or g.get("kind"):
                continue
            exp = _panel_expected(eps_db, g.get("name"), w0, w1, cfg.fault_code_name)
            if exp is not None:
                worst = max(worst, float(g["period_sec"]) - exp)
        left[sid] = worst
        print(f"  станет: #{sid} {key} {f(w0)}–{f(w1)}: известные концы "
              f"{ {k: (v['id'], f(v['end']), v['exact']) for k, v in kn.items()} }")
        for x in md.splitlines():
            if x.startswith("- ") and "[панель]" in x:
                print(f"      {x[:200]}")
        if sid == 35944:
            ln = next((x for x in md.splitlines() if "**Сброс нагрузки" in x
                       or "Load Dump" in x or "(код 1464)" in x), "")
            check("воздействие за период 1 мин 51 с" in ln and "активен на конец" not in ln,
                  f"#35944: «Сброс нагрузки» — 1 мин 51 с: {ln}")
    check(all(v < 1 for v in left.values()), f"«станет» — завышений 0: {({k: round(v) for k, v in left.items()})}")
    # Счётчики #35944 на момент закрытия: строки БД ≤ конца сегмента, 14929/14930 — открытые
    sn, et, p = MACH["ДГУ №1"]
    w0, w1 = M(16, 9, 29, 33), M(16, 9, 32, 33)
    ch = _j(await conn.fetchval("SELECT characteristics_json FROM auto_segments WHERE id=35944"), {})
    stored = {}
    for sub in ch.get("subsegments") or []:
        for d in sub.get("detections") or []:
            v = d.get("values") or {}
            stored[E._alert_key(d)] = (v.get("startup_duration_sec"), v.get("history_duration_30d_sec"),
                                       v.get("startup_since"))
    since = _u((stored.get("CONTROLLER_FAULT|40408|6") or (None, None, None))[2]) or w0
    _an_since = E.analytics_counts_since(_cfg(sn))      # аналитика — с даты выкладки v5.1.21 (В5)
    rows = await conn.fetch("""
        SELECT id, scenario, addr, bit, t_open, t_close, active_sec, blind_sec FROM alarm_episodes
        WHERE router_sn=$1 AND equip_type=$2 AND panel_id=$3 AND scenario = ANY($4::text[])
          AND t_open > $5 AND t_open < $6
          AND ($7::timestamptz IS NULL OR source IS DISTINCT FROM 'analytics' OR t_open >= $7)""",
                            sn, et, p, ["CONTROLLER_FAULT", "RPM_UNDERSPEED"], w1 - timedelta(days=30), w1,
                            _an_since)
    per: dict[tuple, float] = {}
    ends = {14929: (M(16, 9, 31, 33), False), 14930: (M(16, 9, 31, 12), True)}
    for x in rows:
        a = float(x["active_sec"] or 0)
        if x["id"] in ends:
            # Открытые на момент закрытия: у точного — конец − начало; у панели —
            # не меньше начисленного (тогда начислено меньше снятия − начала)
            a = max(0.0, (ends[x["id"]][0] - _u(x["t_open"])).total_seconds())
        k = (x["scenario"], x["addr"], x["bit"], _u(x["t_open"]))
        per[k] = max(per.get(k, 0.0), a)
    agg: dict[str, list] = {}
    for (sc, ad, bt, t), a in per.items():
        o = agg.setdefault(E.online_db.episode_key(sc, ad, bt), [0.0, 0.0])
        o[1] += a
        if t >= since:
            o[0] += a
    ld, ru = agg.get("CONTROLLER_FAULT|40408|6"), agg.get("RPM_UNDERSPEED")
    print(f"  счётчики #35944 (с пуска {f(since)}): было в БД — «Сброс нагрузки» "
          f"{stored.get('CONTROLLER_FAULT|40408|6', (None, None))[:2]}, просадка "
          f"{stored.get('RPM_UNDERSPEED', (None, None))[:2]}; станет — {ld}, {ru}")
    check(ld and [round(ld[0]), round(ld[1])] == [111, 120] and ru and [round(ru[0]), round(ru[1])] == [70, 81],
          f"#35944: счётчики «Сброса нагрузки» 111/120 и просадки 70/81: {ld}, {ru}")
    # SQL на БД: массивы ends выполняются; закрытые строки ends не меняют
    c0 = await E.online_db.count_episodes_batch(sn, et, p, ["CONTROLLER_FAULT", "RPM_UNDERSPEED"], 30, since)
    c1 = await E.online_db.count_episodes_batch(sn, et, p, ["CONTROLLER_FAULT", "RPM_UNDERSPEED"], 30, since,
                                                ends=ends)
    check(c0 == c1 and c0, "count_episodes_batch(ends=…) на БД: запрос выполняется, закрытые строки ends не трогают")


# ══ p5: короткие неисправности панели в открытом сегменте (ДЭС №3 24.10) ═══

class _RecDB:
    """Подмена записи online.db: вставки только запоминаются, чтение — настоящее (RO)."""

    def __init__(self):
        self.ins: list[dict] = []
        self.calls: list[tuple] = []

    async def insert_closed_episode(self, *a, **k):
        self.ins.append(k)
        return -len(self.ins)

    async def open_episode(self, *a, **k):
        self.calls.append(("open", k))
        return -100

    async def reopen_episode(self, ep_id, **k):
        self.calls.append(("reopen", ep_id, k))

    async def update_episode(self, ep_id, **k):
        self.calls.append(("update", ep_id, k))

    async def close_episode(self, ep_id, t_close, reason, **k):
        self.calls.append(("close", ep_id, t_close, reason, k))

    async def find_episode_by_onset(self, *a, **k):
        return None


_REC_NAMES = ("insert_closed_episode", "open_episode", "reopen_episode", "update_episode", "close_episode",
              "find_episode_by_onset")


async def _with_rec(rec, coro_fn):
    orig = {n: getattr(E.online_db, n) for n in _REC_NAMES}
    for n in _REC_NAMES:
        setattr(E.online_db, n, getattr(rec, n))
    try:
        return await coro_fn()
    finally:
        for n, fn in orig.items():
            setattr(E.online_db, n, fn)


def _engine_ro(key: str, episodes: dict | None = None):
    sn, et, p = MACH[key]
    e = E.OnlinePollEngine.__new__(E.OnlinePollEngine)
    e.cfg, e.tz, e.poll_interval_sec = _cfg(sn), MSK, 30
    e.router_sn, e.equip_type, e.panel_id = sn, et, p
    e._events_seeded, e._panel_seeded, e._panel_seeded_seg = set(), set(), None
    e._episodes = dict(episodes or {})
    e._episode_accrual_ts, e._last_open_seg_id, e._supp_recheck, e._prev_seg_hint = None, None, None, None
    return e


async def p5(conn):
    print("\n== p5: ДЭС №3 24.10 — окна 12:41:13–12:46:50 и 12:41:13–12:47:00, без записи (П4, П5) ==")
    from analytics.serializer import build_summary_parts, render_summary_md
    K422, K197 = "CONTROLLER_FAULT|40403|15", "CONTROLLER_FAULT|40401|1"
    a = M(24, 12, 41, 13)
    segs, _i, cfg = await _replay("ДЭС №3", a, M(24, 12, 46, 50))
    live = {E._alert_key(d) for d in E._extract_open_segment_data(segs[-1])[1]}
    cand = E._open_panel_front_candidates(segs[-1], M(24, 12, 46, 50), {}, set())
    print(f"  окно до 12:46:50: живые {sorted(live)}; кандидаты {[(k, f(t)) for k, t, _d in cand]}")
    check(K422 in live and not cand, "окно до 12:46:50 — 422 живая (П4), кандидатов нет")
    segs, _i, cfg = await _replay("ДЭС №3", a, M(24, 12, 47, 0))
    op = segs[-1]
    cand = E._open_panel_front_candidates(op, M(24, 12, 47, 0), {}, set())
    print(f"  окно до 12:47:00: кандидаты {[(k, f(t), f(d.values.get('fault_end'))) for k, t, d in cand]}")
    check({k for k, _t, _d in cand} == {K422, K197}, "окно до 12:47:00 — кандидаты 422 и 197")
    # Посев: existing_episode_onsets — настоящий (RO), вставки только запоминаются
    e = _engine_ro("ДЭС №3")
    rec = _RecDB()
    n = await _with_rec(rec, lambda: e._seed_open_panel_fronts(op, M(24, 12, 47, 0), []))
    have = await E.online_db.existing_episode_onsets(*MACH["ДЭС №3"], [t for _k, t, _d in cand])
    print(f"  existing_episode_onsets: {sorted((k, f(t)) for k, t in have)}; вставок {n}")
    check(n == 0 and {(K422, M(24, 12, 46, 30)), (K197, M(24, 12, 46, 46))} <= have,
          "в проде эпизоды 15017/15018 уже есть — посев их находит, вставок 0")
    # Сводка открытого сегмента — с эпизодами, как их засеял бы П5
    rows = []
    for k, t, d in cand:
        v = d.values or {}
        rows.append({"id": -1, "scenario": "CONTROLLER_FAULT", "source": "panel", "severity": d.severity,
                     "addr": v.get("addr"), "bit": v.get("bit"), "t_open": t, "t_close": _u(v.get("fault_end")),
                     "active_sec": (_u(v.get("fault_end")) - t).total_seconds(), "blind_sec": 0.0,
                     "gate_suppressed": False, "open_values_json": v, "close_reason": "condition_cleared"})
    other = [r for r in await _ep_rows(conn, *MACH["ДЭС №3"], a, M(24, 12, 47, 0))
             if E._episode_key(r) not in (K422, K197)]
    rows_o, _mem = _closing_mem(other, M(24, 12, 47, 0))
    parts = build_summary_parts([op], episodes=rows_o + rows, tz=MSK, trip_roles=cfg.trip_snapshot_roles,
                                t_from=a, t_to=M(24, 12, 47, 0), code_name=cfg.fault_code_name, is_open=True)
    md = render_summary_md(parts)
    for x in md.splitlines():
        if x.startswith("- "):
            print(f"      {x[:200]}")
    check(any("(код 422)** [панель]: 1 эпизод, воздействие за период 26 с" in x for x in md.splitlines())
          and any("(код 197)** [панель]: 1 эпизод, воздействие за период 10 с" in x for x in md.splitlines()),
          "Сводка открытого — «(код 422)» 26 с и «(код 197)» 10 с")
    # SHOULD 2: наложения эпизодов одного бита за 30 дней (разные фронты внахлёст)
    ov = await conn.fetchval("""
        SELECT count(*) FROM alarm_episodes a JOIN alarm_episodes b
          ON a.router_sn = b.router_sn AND a.equip_type = b.equip_type AND a.panel_id = b.panel_id
         AND a.scenario = 'CONTROLLER_FAULT' AND b.scenario = 'CONTROLLER_FAULT'
         AND a.addr = b.addr AND a.bit = b.bit AND a.id < b.id AND a.t_open <> b.t_open
         AND a.t_open < COALESCE(b.t_close, now()) AND b.t_open < COALESCE(a.t_close, now())
        WHERE a.t_open > now() - interval '30 days'""")
    print(f"  наложений эпизодов одного бита за 30 дней (мигающий бит, SHOULD 2): {ov}")


# ══ ca: Общая авария — имя по коду, «код сменился», «перекрыта» ════════════

async def ca(conn):
    print("\n== ca: Общая авария ДЭС №3 — #36064, #36067, #36068, #36070 и 14901 (П6, П7) ==")
    from analytics.serializer import build_summary_parts, render_summary_md
    from analytics import source as _src
    sn, et, p = MACH["ДЭС №3"]
    cfg = _cfg(sn)
    n1248, n1453 = cfg.fault_code_name(1248), cfg.fault_code_name(1453)
    e14901 = dict(await conn.fetchrow("SELECT * FROM alarm_episodes WHERE id = 14901"))
    e14901["open_values_json"] = _j(e14901.get("open_values_json"), {})
    merged = dict(e14901["open_values_json"], last_fault_code=1248, code_name=n1248,
                  code_changes=[{"from": 1453, "to": 1248, "at": M(24, 12, 38, 59).isoformat()}])

    async def _summary(sid, ov=None, close=None):
        r = await conn.fetchrow("SELECT t_start, t_end FROM auto_segments WHERE id=$1", sid)
        w0, w1 = _u(r["t_start"]), _u(r["t_end"])
        segs, _i, _c = await _replay("ДЭС №3", w0, w1)
        seg = next((s for s in segs if _u(s.t_start) == w0), None) or segs[0]
        eps_db = await _ep_rows(conn, sn, et, p, w0, w1)
        rows, mem = _closing_mem(eps_db, w1)
        for x in rows:
            if x["id"] == 14901:
                if ov is not None:
                    x["open_values_json"] = ov
                if close is not None:
                    x["t_close"], x["close_reason"] = close
                    mem.pop(CA_KEY, None)
        live = E._live_alert_keys(seg)
        # Настоящий путь сводки (_build_summary_md_for, ревью v5.1.25 Q4): сначала
        # фильтр хвостов дебаунса (дебаунс + цикл ≈ 120 с), затем хвосты памяти
        _deb = int(cfg.det("ALARM_EPISODES", "close_debounce_cycles", default=3) or 3)
        rows = E._filter_window_episodes(rows, w0, w1, tail_sec=(_deb + 1) * 30)
        rows = E._drop_debounce_tails(rows, [seg], mem, live, w1=w1)
        parts = build_summary_parts([seg], episodes=rows, tz=MSK, trip_roles=cfg.trip_snapshot_roles,
                                    t_from=w0, t_to=w1, code_name=cfg.fault_code_name,
                                    common_alarm=(40408, 11, 15.0))
        md = render_summary_md(parts)
        det = next((d for sub in seg.subsegments for d in sub.detections
                    if d.scenario == "CONTROLLER_FAULT" and d.values.get("bit") == 11
                    and d.values.get("addr") == 40408), None)
        ln = next((x for x in md.splitlines() if "**Общая авария" in x), "")
        print(f"  #{sid} {f(w0)}–{f(w1)}: детекция {det and det.values.get('last_fault_code')} "
              f"с {det and f(det.values.get('last_fault_code_since'))}")
        print(f"      {ln[:260]}")
        return ln, parts, seg

    ln, _p, seg64 = await _summary(36064)
    check(f"**Общая авария: {n1453} (код 1453)**" in ln and "код сменился" not in ln,
          f"#36064 — «Общая авария: {n1453} (код 1453)»")
    ln, _p, _s = await _summary(36067)
    check(f"**Общая авария: {n1248} (код 1248)** [панель]: 1 эпизод, воздействие за период 2 мин 6 с" in ln
          and "— код сменился 1453 → 1248" in ln,
          "#36067 — имя по коду 1248 и «код сменился 1453 → 1248» (до слияния в БД)")
    ln, _p, _s = await _summary(36067, ov=merged)
    check("— код сменился 1453 → 1248" in ln, "#36067 — и после слияния (прежний код из code_changes)")
    ln, _p, _s = await _summary(36068, ov=merged)
    check(f"**Общая авария: {n1248} (код 1248)**" in ln and "код сменился" not in ln,
          "#36068 — то же имя без пометки (критика M4)")
    # Смена кода в живом цикле: память эпизода 14901 с кодом 1453, детекция 1248
    segs, _i, _c = await _replay("ДЭС №3", M(24, 12, 38, 59), M(24, 12, 39, 30))
    dets = E._extract_open_segment_data(segs[-1])[1]
    e = _engine_ro("ДЭС №3", {CA_KEY: {"id": 14901, "severity": "CAUTION", "miss": 0, "first_miss_ts": None,
                                        "t_open": _u(e14901["t_open"]), "exact": False, "until": None,
                                        "code": 1453, "code_changes": []}})
    e._episode_accrual_ts = M(24, 12, 39, 0)
    rec = _RecDB()
    await _with_rec(rec, lambda: e._process_episodes(dets, M(24, 12, 39, 30), [], panel_ctx=None))
    up = [c for c in rec.calls if c[0] == "update" and c[1] == 14901]
    vm = (up[0][2].get("values_merge") or {}) if up else {}
    print(f"  _code_change 14901: {vm.get('code_changes')}")
    check(vm.get("last_fault_code") == 1248 and (vm.get("code_changes") or [{}])[-1].get("from") == 1453,
          "живой цикл: «код сменился 1453 → 1248» тем же UPDATE эпизода 14901, нового эпизода нет")
    # Закрытие 14901 в цикле 12:42:15: громкий 40406/2 с 12:41:50, бит Общей аварии висит
    t_cl = M(24, 12, 42, 15)
    segs, _i, _c = await _replay("ДЭС №3", M(24, 12, 41, 13), t_cl)
    dets = E._extract_open_segment_data(segs[-1])[1]
    fps = await _src.get_fault_periods(sn, et, p, M(24, 12, 41, 13), t_cl, fault_addrs=cfg.whitelist_fault)
    ctx = E._supersede_ctx(cfg, fps, dets, t_cl)
    print(f"  контекст перекрытия на {f(t_cl)}: {ctx}")
    e = _engine_ro("ДЭС №3", {CA_KEY: {"id": 14901, "severity": "CAUTION", "miss": 2, "first_miss_ts": t_cl,
                                        "t_open": _u(e14901["t_open"]), "exact": False, "until": None,
                                        "code": 1248}})
    e._episode_accrual_ts = M(24, 12, 41, 45)
    rec = _RecDB()
    await _with_rec(rec, lambda: e._process_episodes(dets, t_cl, [], panel_ctx=ctx))
    cl = [c for c in rec.calls if c[0] == "close" and c[1] == 14901]
    print(f"  закрытие 14901: {cl and (cl[0][3], cl[0][4])}")
    check(cl and cl[0][3] == "superseded"
          and ((cl[0][4].get("values_merge") or {}).get("superseded_by") or {}).get("at")
          == M(24, 12, 41, 50).isoformat(),
          "14901 → «superseded», перекрыт «Предупреждением двигателя» с 12:41:50")
    sup = dict(merged, superseded_by=(ctx or {}).get("loud"))
    ln, parts70, _s = await _summary(36070, ov=sup, close=(t_cl, "superseded"))
    check("перекрыта предупреждением панели «Предупреждение двигателя» с 12:41:50, Общая авария не снята" in ln,
          "#36070 — суффикс «перекрыта предупреждением панели … с 12:41:50»")
    # Критика M8: «Висит также» за 30 дней по сохранённым данным: громкий фронт,
    # поднявший Общую аварию вместе с собой (16.10), строки не даёт; строка — только
    # где фронт Общей аварии старше громкого больше 15 с или у него свой эпизод
    t1 = M(24, 23, 59)
    hold, rejected = [], []
    for key, (s_, e_, p_) in MACH.items():
        rows = await conn.fetch("""
            SELECT id, t_start, t_end, characteristics_json, report_summary_json FROM auto_segments
            WHERE router_sn=$1 AND equip_type=$2 AND panel_id=$3 AND t_end IS NOT NULL
              AND t_start >= $4 AND t_start < $5 ORDER BY t_start""", s_, e_, p_, t1 - timedelta(days=30), t1)
        cas = await _ep_rows(conn, s_, e_, p_, t1 - timedelta(days=60), t1, "CONTROLLER_FAULT")
        cas = [x for x in cas if x.get("addr") == 40408 and x.get("bit") == 11]
        for r in rows:
            ch, sp = _j(r["characteristics_json"], {}), _j(r["report_summary_json"], {})
            w1 = _u(r["t_end"])
            evs = [x for x in ch.get("events") or [] if x.get("type") == "FAULT"]
            hang = lambda x: x.get("fault_end") is None or _u(x["fault_end"]) >= w1  # noqa: E731
            ca_t = min((_u(x["t"]) for x in evs if x.get("addr") == 40408 and x.get("bit") == 11 and hang(x)),
                       default=None)
            if ca_t is None:
                continue
            subs = ch.get("subsegments") or []
            lfc = ((subs[-1].get("characteristics") or {}).get("LAST_FAULT_CODE") or {}) if subs else {}
            if not float(lfc.get("value_end") or 0) > 0:
                continue
            rem = sp.get("remarks") or []
            loud = [g for g in rem if g.get("source") == "panel" and g.get("at_end")
                    and g.get("severity") in ("WARNING", "SHUTDOWN")]
            own = [g for g in rem if g.get("source") == "panel" and g.get("at_end")
                   and ("Общая авария" in (g.get("name") or "") or "CommonAlarm" in (g.get("name") or ""))]
            if not loud or own:
                continue
            loud_t = min((_u(x["t"]) for x in evs if str(x.get("severity") or "").lower()
                          in ("warning", "shutdown", "shutdown_cooldown") and hang(x)
                          and not (x.get("addr") == 40408 and x.get("bit") == 11)), default=None)
            older = loud_t is not None and (loud_t - ca_t).total_seconds() > 15
            has_ep = any(_u(x["t_open"]) == ca_t and (x.get("t_close") is None or _u(x["t_close"]) > w1)
                         for x in cas)
            if older or has_ep:
                hold.append((key, r["id"], f(r["t_start"]), f(ca_t), loud_t and f(loud_t)))
            else:
                rejected.append((key, r["id"], f(r["t_start"]), f(ca_t), loud_t and f(loud_t),
                                 abs((loud_t - ca_t).total_seconds()) if loud_t else None))
    print(f"  «Висит также» по сохранённым сегментам за 30 дней (сегмент, начало, фронт Общей аварии, "
          f"громкий): {hold}")
    print(f"  отклонено правилом M8 (Общая авария встала вместе с громким): {rejected}")
    check(not any(x[2].startswith("16.10") for x in hold) and rejected
          and all(x[5] is None or x[5] <= 15 for x in rejected),
          "громкий фронт, поднявший Общую аварию вместе с собой (16.10, 12.10), строки «Висит также» не даёт (M8)")
    # План ждал «вне 24.10 — 0»: по сохранённым данным строка встала бы и в сегментах,
    # где Общая авария висела задолго до громкого (ДЭС №3 13.10) — это и есть случай П7
    print(f"  вне 24.10: {[x for x in hold if not x[2].startswith('24.10')]} — Общая авария старше громкого "
          f"на часы/сутки (план ждал 0 — отступление)")
    # P3: segment_id_open = NULL за 30 дней
    nn = await conn.fetch("""
        SELECT id, router_sn, panel_id, scenario, addr, bit, t_open FROM alarm_episodes
        WHERE segment_id_open IS NULL AND t_open > now() - interval '30 days' ORDER BY id""")
    s14931 = await conn.fetchval("SELECT segment_id_open FROM alarm_episodes WHERE id = 14931")
    print(f"  segment_id_open NULL за 30 дней: {[(r['id'], r['addr'], r['bit'], f(r['t_open'])) for r in nn]}; "
          f"14931 → #{s14931}")
    check(s14931 == 35955 and {r["id"] for r in nn} <= {14802, 14815, 14833},
          "P3: 14931 перецелен на #35955; NULL — только 14802/14815/14833 (сбой часов, не путь закрытия)")


# ══ p10: заглушка #36065 — последние данные до обрыва и оценка по наработке ═

async def p10(conn):
    print("\n== p10: #36065 ДЭС №3 24.10 09:00–12:38:59 — порядок поступления строк, 40070 из БД (П10) ==")
    from analytics import source as _src
    from analytics.labels import no_data_hours_text
    from analytics.serializer import build_no_data_report
    sn, et, p = MACH["ДЭС №3"]
    cfg = _cfg(sn)
    t0, t1 = M(24, 9, 0), M(24, 12, 38, 59)
    t_cyc = M(24, 12, 39, 10)                    # цикл закрытия #36065 (строки 12:38:58/59 получены в 12:39:06)
    rows = [dict(r) for r in await conn.fetch("""
        SELECT addr, ts, value, received_at FROM history_rich
        WHERE router_sn=$1 AND equip_type=$2 AND panel_id=$3 AND addr = ANY($4::int[])
          AND ts >= $5 AND ts <= $6 AND received_at <= $7 ORDER BY ts""",
                                              sn, et, p, list(cfg.whitelist_analog), t0 - timedelta(seconds=300),
                                              t_cyc, t_cyc)]
    hist_before = [r for r in rows if _u(r["received_at"]) <= M(24, 12, 38, 40)]
    ld_old = max((_u(r["ts"]) for r in hist_before), default=None)
    gaps = [dict(g) for g in await conn.fetch("""
        SELECT gap_start, gap_end FROM data_gaps WHERE router_sn=$1 AND equip_type=$2 AND panel_id=$3
          AND gap_start < $5 AND (gap_end IS NULL OR gap_end > $4) AND created_at <= $6 ORDER BY gap_start""",
                                              sn, et, p, t0, t_cyc, t_cyc)]
    e = _engine_ro("ДЭС №3")
    e.last_data_ts = ld_old
    last = e._no_data_last_before(t0, gaps, rows)
    print(f"  last_data_ts движка к циклу закрытия (строки, полученные до 12:38:40): {f(ld_old)}; "
          f"разрывы {[(f(g['gap_start']), f(g['gap_end'])) for g in gaps]}")
    check(last == M(24, 5, 48, 15) and ld_old == M(24, 11, 48, 9),
          f"последние данные до обрыва — 05:48:15 по разрыву, а не {f(ld_old)} из памяти (критика M1): {f(last)}")
    # Режим на обрыве — удержанный режим сегмента без связи (ревью v5.1.25, Q1)
    rs65 = await conn.fetchval("SELECT run_state FROM auto_segments WHERE id = 36065")
    print(f"  режим #36065 (удержан с обрыва): {rs65}")
    check(rs65 == 3, "#36065: на обрыве 05:48 работал (run_state 3) — «до ~» допустимо")
    est = await e._no_data_estimate(t0, t1, rows, last, run_state=rs65)
    txt = no_data_hours_text(est, MSK)
    print(f"  оценка: {est}")
    print(f"  текст: {txt}")
    check(est and est["kind"] == "until" and est["end_from"] == M(24, 12, 5, 11).isoformat()
          and est["end_to"] == M(24, 12, 8, 11).isoformat() and est["worked_sec"] == 22680.0
          and est["starts_changed"] is False,
          "оценка по 40070: до ~12:05–12:08, наработка 6 ч 18 мин, попытки пуска 40069 не менялись")
    check(txt and "6 ч 18 мин" in txt and "до ~12:05–12:08" in txt, "текст оценки")
    _rmd, smd = build_no_data_report(sn, et, p, t0, t1, tz=MSK, last_data_ts=last, run_estimate=est)
    print(f"  заглушка: {smd.strip()}")
    check("Последние данные до обрыва: 2026-10-24 05:48:15" in smd, "строка «Последние данные до обрыва: 05:48:15»")
    # Ревью v5.1.25 (Q1): #35641 ДЭС №3 29.09 — на обрыве 22.09 стоял, на возврате
    # 29.09 15:44:49 уже в работе. С 40069 — «были остановы и пуски». С v5.1.25
    # 40069 в слое контроллера: у KTA50 (тот же PCC3300) результат тот же
    r41 = await conn.fetchrow("SELECT t_start, t_end, run_state, cause_close FROM auto_segments WHERE id = 35641")
    a41, b41 = _u(r41["t_start"]), _u(r41["t_end"])
    rows41 = [dict(r) for r in await conn.fetch("""
        SELECT addr, ts, value FROM history_rich
        WHERE router_sn=$1 AND equip_type=$2 AND panel_id=$3 AND addr = ANY($4::int[])
          AND ts >= $5 AND ts <= $6 ORDER BY ts""", sn, et, p, list(cfg.whitelist_analog),
                                            a41 - timedelta(seconds=300), b41 + timedelta(seconds=10))]
    gaps41 = [dict(g) for g in await conn.fetch("""
        SELECT gap_start, gap_end FROM data_gaps WHERE router_sn=$1 AND equip_type=$2 AND panel_id=$3
          AND gap_start < $5 AND (gap_end IS NULL OR gap_end > $4) ORDER BY gap_start""", sn, et, p, a41, b41)]
    e41 = _engine_ro("ДЭС №3")
    last41 = e41._no_data_last_before(a41, gaps41, rows41)
    est41 = await e41._no_data_estimate(a41, b41, rows41, last41, run_state=r41["run_state"])
    e41k = _engine_ro("ДЭС №3")
    e41k.cfg = _cfg("6003790403")                 # KTA50: 40069 — из слоя контроллера
    est41k = await e41k._no_data_estimate(a41, b41, rows41, last41, run_state=r41["run_state"])
    t41, t41k = no_data_hours_text(est41, MSK), no_data_hours_text(est41k, MSK)
    print(f"  #35641 {f(a41)}–{f(b41)} режим {r41['run_state']} {r41['cause_close']}, последние данные {f(last41)}")
    print(f"    QSX15: {est41 and est41.get('kind')} — {t41}")
    print(f"    KTA50: {est41k and est41k.get('kind')} — {t41k}")
    check(r41["run_state"] == 0 and est41 and est41["kind"] == "starts" and est41k and est41k["kind"] == "starts"
          and all("не работал (" not in (t or "") and "до ~" not in (t or "") for t in (t41, t41k)),
          "#35641: стоял на обрыве — без «В этот период не работал (остановился ~22.09 10:44)» (Q1)")


# ══ badge: значок акта по готовности разбора совета (этап 3) ═══════════════

async def badge(conn):
    print("\n== badge: значок акта — акты за 45 дней и календари сентября–октября трёх машин (В-25.2) ==")
    import online.db as _odb
    from online.incident_gate import act_stop_ts, incident_hash
    iaf = _routes_fn("_incident_analysis_fields")
    now = datetime.now(U)
    acts = await conn.fetch("""
        SELECT id, router_sn, equip_type, panel_id, t_start, incident_json->>'stop_ts' AS st
        FROM auto_segments
        WHERE incident_json IS NOT NULL AND t_start > now() - interval '45 days' ORDER BY t_start""")
    one: dict[int, dict] = {}
    one_state: dict[int, dict] = {}
    for r in acts:
        st = act_stop_ts(r["st"], r["t_start"])
        t = time.perf_counter()
        s1 = await _odb.get_incident_analysis_state(r["router_sn"], r["equip_type"], r["panel_id"],
                                                    incident_hash(st), st)
        dt = (time.perf_counter() - t) * 1000
        one_state[r["id"]] = s1
        one[r["id"]] = iaf(True, s1, r["st"], r["t_start"], now)
        print(f"  #{r['id']} {f(r['t_start'])} (останов {f(st)}): {s1} → {one[r['id']]} [{dt:.0f} мс с соединением]")
    by = {}
    for i, b in one.items():
        by.setdefault(b["incident_analysis"], set()).add(i)
    print(f"  итог по одному: { {k: sorted(v) for k, v in by.items()} }")
    check(len(acts) == 8 and len(by.get("ready", ())) == 6 and by.get("failed") == {35282, 35492}
          and not by.get("pending") and set(by) <= {"ready", "failed"},
          "акты за 45 дней: 6 ready, 2 failed (#35282, #35492), 0 pending")
    check(all(one[i]["incident_analysis_reason"] == "not_ordered" and one_state[i]["ordered"] == 0
              for i in by.get("failed", ())),
          "у #35282/#35492 причина — «не заказывался» (ordered = 0), не «совет не ответил» (SHOULD 6)")
    # Календари путём /segments: второй запрос — только при актах
    seen: set[int] = set()
    bad: list = []
    for key, (sn, et, p) in MACH.items():
        for mo in (9, 10):
            ts_from, ts_to = datetime(2026, mo, 1, tzinfo=U), datetime(2026, mo + 1, 1, tzinfo=U)
            t = time.perf_counter()
            plain = await _odb.get_segments_for_calendar(sn, et, p, ts_from=ts_from, ts_to=ts_to, limit=500)
            t_plain = (time.perf_counter() - t) * 1000
            t = time.perf_counter()
            segs = await _odb.get_segments_for_calendar(sn, et, p, ts_from=ts_from, ts_to=ts_to, limit=500,
                                                        incident_state=True)
            t_full = (time.perf_counter() - t) * 1000
            inc_rows = [s for s in segs if s.get("has_incident")]
            t2 = None
            if inc_rows:
                q = [(s["id"], act_stop_ts(s.get("incident_stop_ts"), s["t_start"]), None) for s in inc_rows]
                q = [(i, st, incident_hash(st)) for i, st, _ in q]
                t = time.perf_counter()
                batch = await _odb.get_incident_analysis_states(sn, et, p, q, conn=conn)
                t2 = (time.perf_counter() - t) * 1000
                for s in inc_rows:
                    if batch.get(s["id"]) != s.get("_incident_state"):
                        bad.append((s["id"], "повтор второго запроса", batch.get(s["id"]), s.get("_incident_state")))
            vals: dict[str, int] = {}
            for s in segs:
                b = iaf(bool(s.get("has_incident")), s.get("_incident_state"), s.get("incident_stop_ts"),
                        s.get("t_start"), now)
                vals[b["incident_analysis"] or "null"] = vals.get(b["incident_analysis"] or "null", 0) + 1
                if s.get("has_incident"):
                    seen.add(s["id"])
                    if s["id"] in one and b != one[s["id"]]:
                        bad.append((s["id"], b, one[s["id"]]))
                    if s["id"] in one_state and s.get("_incident_state") != one_state[s["id"]]:
                        bad.append((s["id"], s.get("_incident_state"), one_state[s["id"]]))
                elif b != {"incident_analysis": "none", "incident_analysis_reason": None}:
                    bad.append((s["id"], b))
            if len(plain) != len(segs) or [s["id"] for s in plain] != [s["id"] for s in segs]:
                bad.append((key, mo, "состав календаря с incident_state другой"))
            print(f"  {key} {mo:02d}.2026: строк {len(segs)}, актов {len(inc_rows)}, значки {vals}; "
                  f"календарь {t_plain:.0f} мс без состояния / {t_full:.0f} мс с ним (с соединением)"
                  + (f", второй запрос отдельно {t2:.0f} мс" if t2 is not None else ", второго запроса нет"))
    check(not bad, f"календарь: значки строк с актом — как по одному (то же состояние), прочие — none: {bad[:5]}")
    check(set(one) <= seen, f"все акты за 45 дней попали в календари: нет {sorted(set(one) - seen)}")


# ══ rv: подтверждённые замечания ревью v5.1.25 на реальных данных ══════════

class _MemDB:
    """Эпизоды в памяти для покадрового прогона панели: БД не трогается."""

    def __init__(self):
        self.eps: list[dict] = []
        self.nid = 0

    def _row(self, i):
        return next(e for e in self.eps if e["id"] == i)

    async def find_episode_by_onset(self, sn, et, p, *, scenario, addr, bit, t_open):
        rs = [e for e in self.eps if (e["scenario"], e["addr"], e["bit"], e["t_open"]) == (scenario, addr, bit, t_open)]
        if not rs:
            return None
        e = max(rs, key=lambda x: x["id"])
        return {"id": e["id"], "t_close": e["t_close"], "severity": e["severity"],
                "last_fault_code": None, "code_changes": None}

    async def open_episode(self, sn, et, p, *, scenario, severity, t_open, addr=None, bit=None,
                           active_sec=0.0, **k):
        self.nid += 1
        self.eps.append({"id": self.nid, "scenario": scenario, "severity": severity, "addr": addr, "bit": bit,
                         "t_open": t_open, "t_close": None, "active_sec": float(active_sec or 0), "how": "open"})
        return self.nid

    async def insert_closed_episode(self, sn, et, p, *, scenario, severity, t_open, t_close, active_sec,
                                    addr=None, bit=None, **k):
        self.nid += 1
        self.eps.append({"id": self.nid, "scenario": scenario, "severity": severity, "addr": addr, "bit": bit,
                         "t_open": t_open, "t_close": t_close, "active_sec": float(active_sec), "how": "seed"})
        return self.nid

    async def reopen_episode(self, i, *, now_ts, **k):
        e = self._row(i)
        e["t_close"] = None
        e["active_sec"] = max(e["active_sec"], (now_ts - e["t_open"]).total_seconds())

    async def update_episode(self, i, *, active_sec_add=0.0, **k):
        e = self._row(i)
        if e["t_close"] is None:
            e["active_sec"] += active_sec_add

    async def close_episode(self, i, t_close, reason, **k):
        e = self._row(i)
        if e["t_close"] is None:
            e["t_close"] = t_close
            e["active_sec"] = max(e["active_sec"], (t_close - e["t_open"]).total_seconds())

    async def existing_episode_onsets(self, sn, et, p, onsets):
        s = set(onsets)
        return {(f"CONTROLLER_FAULT|{e['addr']}|{e['bit']}", e["t_open"]) for e in self.eps if e["t_open"] in s}


_MEM_NAMES = ("find_episode_by_onset", "open_episode", "insert_closed_episode", "reopen_episode",
              "update_episode", "close_episode", "existing_episode_onsets")


async def _panel_minisim(key: str, addr: int, bit: int, w0: datetime, w1: datetime, step: int = 30,
                         lag: int = 30) -> tuple[list[dict], list[tuple]]:
    """Панельные эпизоды бита покадрово (шаг опроса step, окно отстаёт на lag —
    «как видно в момент now»): периоды неисправности из БД, детектор панели,
    _process_episodes и посев снятых фронтов П5 одного открытого сегмента с w0.
    Запись — в памяти (_MemDB), контекст аварии выключен."""
    from types import SimpleNamespace as NS
    from analytics import source as _src
    from analytics.detectors import _detect_controller_faults
    sn, et, p = MACH[key]
    cfg = _cfg(sn)
    fps = [fp for fp in await _src.get_fault_periods(sn, et, p, w0, w1 + timedelta(hours=1),
                                                     fault_addrs=cfg.whitelist_fault)
           if fp.get("addr") == addr and fp.get("bit") == bit]
    fronts = sorted((_u(fp["fault_start"]), _u(fp.get("fault_end"))) for fp in fps)
    db = _MemDB()
    e = _engine_ro(key)

    async def _no_ctx(*a, **k):
        return None

    e._attach_trip_context = _no_ctx
    e._episode_accrual_ts = w0
    orig = {n: getattr(E.online_db, n) for n in _MEM_NAMES}
    for n in _MEM_NAMES:
        setattr(E.online_db, n, getattr(db, n))
    try:
        T = w0 + timedelta(seconds=step)
        while T <= w1:
            now = T + timedelta(seconds=lag)
            vis = []
            for fp in fps:
                fs, fe = _u(fp["fault_start"]), _u(fp.get("fault_end"))
                if fs > now:
                    continue
                r = dict(fp)
                if fe is not None and fe > now:
                    r["fault_end"] = None
                vis.append(r)
            dets = _detect_controller_faults(vis, {}, w0, T, cfg)
            seg = NS(t_start=w0.isoformat(),
                     subsegments=[NS(detections=dets, t_start=w0.isoformat(), t_end=T.isoformat())])
            curr = [d.to_dict() for d in dets if not E._cleared_by(d.to_dict(), T)]
            await e._process_episodes(curr, T, [], cleared_at=E._panel_cleared_at([seg]),
                                      front_ends=E._panel_front_ends([seg]))
            await e._seed_open_panel_fronts(seg, T, [])
            T += timedelta(seconds=step)
    finally:
        for n, fn in orig.items():
            setattr(E.online_db, n, fn)
    return sorted(db.eps, key=lambda x: x["t_open"]), fronts


def _overlaps(eps: list[dict]) -> list[tuple]:
    out = []
    for i, a in enumerate(eps):
        for b in eps[i + 1:]:
            ac = a["t_close"] or datetime.max.replace(tzinfo=U)
            bc = b["t_close"] or datetime.max.replace(tzinfo=U)
            if a["t_open"] < bc and b["t_open"] < ac:
                out.append((a["id"], b["id"]))
    return out


async def rv(conn):
    print("\n== rv: подтверждённые замечания ревью v5.1.25 (Q3, Q5, Q2/Q7, Q8) ==")
    from analytics.labels import start_profile_text
    from analytics.serializer import build_summary_parts, render_summary_md
    # Q3: панельный эпизод — по фронту. Покадровый прогон, фазы опроса 0…25 с
    for key, addr, bit, w0, w1, name in (
            ("ДЭС №3", 40407, 8, M(13, 10, 43, 0), M(13, 13, 15, 0), "13.10 1438"),
            ("ДЭС №3", 40401, 1, M(16, 16, 27, 0, mo=9), M(16, 16, 47, 0, mo=9), "16.09 код 197")):
        res = []
        for ph in (0, 5, 10, 15, 20, 25):
            eps, fronts = await _panel_minisim(key, addr, bit, w0 + timedelta(seconds=ph), w1)
            fr_in = [(fs, fe) for fs, fe in fronts if fs >= w0 and fe is not None and fe <= w1 - timedelta(minutes=3)]
            got = [(x["t_open"], x["t_close"]) for x in eps]
            ok = got == fr_in and not _overlaps(eps)
            res.append(ok)
            if ph == 0 or not ok:
                print(f"  {name}, фаза {ph} с: фронтов {len(fr_in)} ({sum((b - a).total_seconds() for a, b in fr_in):.0f} с), "
                      f"эпизодов {len(eps)} ({sum((x['t_close'] - x['t_open']).total_seconds() for x in eps if x['t_close']):.0f} с), "
                      f"наложений {len(_overlaps(eps))}")
                for x in eps:
                    print(f"      #{x['id']} {f(x['t_open'])}–{f(x['t_close'])} [{x['how']}]")
        check(all(res), f"Q3 {name}: эпизод на каждый фронт [начало, снятие], без наложений — во всех 6 фазах опроса")
    # Q5: пуск прошёл без связи — ДЭС №3 30.09 20:33:17 и 01.10 20:08:18
    for a, b, tc, name in ((M(30, 15, 0, mo=9), M(30, 21, 0, mo=9), M(30, 20, 33, 17, mo=9), "30.09"),
                           (M(1, 14, 0), M(1, 20, 30), M(1, 20, 8, 18), "01.10")):
        segs, _i, _c = await _replay("ДЭС №3", a, b)
        s = next((x for x in segs if x.run_state == 3 and _u(x.t_start) == tc), None)
        sp = getattr(s, "start_profile", None) if s is not None else None
        txt = start_profile_text(sp, MSK) if isinstance(sp, dict) else None
        print(f"  {name} «Работа» {s and f(s.t_start)}: Пуск: {txt}")
        check(isinstance(sp, dict) and sp.get("unobserved") is True and sp.get("oil_temp") is None
              and txt and txt.startswith("пуск прошёл без связи") and "масло перед пуском" not in txt
              and "автомат уже замкнут" in txt,
              f"Q5 {name}: пуск в разрыве связи — пометка, без «масла перед пуском» и момента автомата")
    # Q2/Q7: продолжение работы через рез 24.10 09:00 — открытая Сводка в первые циклы
    for key in ("ДГУ №1", "ДГУ №2"):
        sn, et, p = MACH[key]
        cfg = _cfg(sn)
        lines = []
        for sec in (30, 60, 90):
            segs, _i, _c = await _replay(key, M(24, 9, 0), M(24, 9, 0) + timedelta(seconds=sec))
            seg = segs[-1]
            md = render_summary_md(build_summary_parts([seg], [], tz=MSK, trip_roles=cfg.trip_snapshot_roles,
                                                       t_from=M(24, 9, 0), is_open=True))
            ln = next((x for x in md.splitlines() if x.startswith(("С 09:00", "Работа идёт", "Короткая", "Мало"))), "")
            lines.append((seg.run_state, seg.cause_open, ln))
        print(f"  {key} 24.10 после реза: {[(r, c, ln[:70]) for r, c, ln in lines]}")
        check(all(r == 3 and c == "REPORT_START" and (not ln or ln.startswith("С 09:00 — ")) for r, c, ln in lines)
              and any(ln for _r, _c, ln in lines),
              f"Q2/Q7 {key}: продолжение после 09:00 — «С 09:00 — N (M отсчётов)», не «Работа идёт N с»")
    # Q8: у #35492 разбор только старым гейтом (не incident:…) — UI покажет его
    # отдельно от разбора совета, и «Разбор совета не заказывался» не спорит с ним
    for sid in (35282, 35492):
        wa = _j(await conn.fetchval("SELECT warning_analyses FROM auto_segments WHERE id = $1", sid), []) or []
        hs = [(x.get("fault_hash") or "") for x in wa if isinstance(x, dict) and x.get("md")]
        council = [h for h in hs if h.startswith("incident:")]
        print(f"  #{sid}: разборы {hs}, из них совета {council}")
        check(not council, f"Q8 #{sid}: разбора совета нет — в блоке акта причина «не заказывался»; "
                           f"прочие разборы ({len(hs)}) — отдельно как разбор гейта")


SECTIONS = (("att", att), ("att30", att30), ("lbl", lbl), ("clf", clf), ("km", km), ("vio", vio), ("ls", ls),
            ("p23", p23), ("p5", p5), ("ca", ca), ("p10", p10), ("badge", badge), ("rv", rv))


async def main() -> int:
    global _DSN
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn-file", help="DSN одной БД (источник и аналитика) — иначе config.yml")
    ap.add_argument("sections", nargs="*",
                    help="разделы: att att30 lbl clf km vio ls (этап 1), p23 p5 ca p10 (этап 2), "
                         "badge (этап 3), rv (ревью); по умолчанию все")
    args = ap.parse_args()
    if args.dsn_file:
        dsn = Path(args.dsn_file).read_text(encoding="utf-8").strip()
        src_dsn = dsn
    else:
        from config import settings
        dsn, src_dsn = settings.analytics_db_url, settings.source_db_url
    _DSN = dsn
    print("Сверка v5.1.25 на реальных данных. Соединение только для чтения, запись НЕ производится.")
    from analytics import source as _src
    import online.db as _odb
    conn = await asyncpg.connect(dsn, server_settings=RO)
    _src._pool = await asyncpg.create_pool(src_dsn, min_size=1, max_size=3, command_timeout=600,
                                           server_settings=RO)
    _orig_connect = _odb._connect
    _odb._connect = _ro_connect
    run_all = not args.sections
    try:
        assert await conn.fetchval("SHOW default_transaction_read_only") == "on"
        async with _src._pool.acquire() as c2:
            assert await c2.fetchval("SHOW default_transaction_read_only") == "on"
        for name, fn in SECTIONS:
            if run_all or name in args.sections:
                await fn(conn)
    finally:
        _odb._connect = _orig_connect
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
