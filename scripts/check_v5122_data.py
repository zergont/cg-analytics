# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""v5.1.22: сверка на реальных данных (план docs/plans/design_v5122_incidents.md,
раздел 5 «Прогоны по БД»). ТОЛЬКО ЧТЕНИЕ — соединение с
default_transaction_read_only, запись не производится.

Раздел P1-1 — снятие гейтом относится к своим эпизодам. По сегментам, журналам
гейта и эпизодам из БД цепочка проигрывается по новым правилам:
- своё решение (разбор моделью / повтор) снимает эпизоды, открытые на момент
  разбора; новый эпизод в той же строке наследует, если состав тот же и
  запретов нет (suppression_bans: сигнал панели, свежая «Общая авария»,
  аварийный стоп); источник — gate_decision_source по журналу строки;
- на границе сегментов хеш переходит по engine._gate_carry (тот же код, что
  в движке): снятый эпизод идёт на границе, стопа между решением и границей нет,
  новая строка не стоп; новый эпизод в строке с перенесённым снятием не
  наследует;
- состав — набор сценариев аналитики, живых в момент (по эпизодам БД; хеш
  с ref_codes из старых записей журнала не восстановить).
Сверяются ожидания раздела 1 плана: ДГУ №1 16.10 (#35944 — 14930, #35949 —
14931 без снятия), ДГУ №1 14–15.10 (14916 переносится через рез с carry и
подписью «по разбору от 14.10 03:36 (#35876)», через стоп 15.10 13:04 — нет),
ДЭС №3 #35891 (9 эпизодов близости остаются снятыми — 1453 висит с начала
строки, источник — разбор 09:01:59), ДЭС №3 15.10 (14920 — свой разбор),
ДГУ №2 15.10 (#35900, #35926 — свои разборы). Плюс разница «было снято —
станет живым» за 30 дней и пробный (без записи) разовый перенос открытых строк.

Этап 2 (P1-2, P1-3, шапка заключения):
- p12: код 40012 на закрытии тем же помощником нарезки по границам сегментов
  БД — #35931, #35941, ДЭС №3 #35848/#35855/#35861 («сброшен»), #35869
  («снят в момент пуска»), #35872 (висит — прежний текст); за 30 дней все
  SHUTDOWN_CLEARED/FAULT_CLEARED и все блоки «Несброшенная» (запись ровно там,
  где код снят в t_end — сверка с независимым расчётом);
- p13: отметка обрыва аварией за 60 дней на парах «не стоп → стоп» — ровно
  перед аварийными стопами (не плашками), #35929/#35939 есть, #35944 нет;
- header: перепрогон окон ДГУ №1 16.10 09:20–09:40 и ДЭС №3 13.10 10:30–15:10
  кодом движка (_run_segment) — шапка заключения (наработка 13 643 / 15 297 ч,
  «Авария снята в …», код, запрет «штатно»), в отчётах нет «Несброшенной».

Этап 3 (P1-4, P1-5, P1-6), перепрогон окон кодом движка (_run_segment —
акты с контекстом от якоря, как в движке), к шлюзу не обращаемся:
- p14: акт #35931/#35941 — лента отсчётов и сегмент падения, факты до останова
  («держится с» для удержанного), новый вход совета по сегментам цикла из БД:
  размер не больше прежнего (15 437 / 20 979), МСК без UTC, 713 кВт до
  разделителя «03:45:32 последний штатный останов», у #35929/#35939 свои строки;
- p15: заключение аварийного стопа фактами (#35931, #35941), ДЭС №3 13.10 —
  плашки отказов пуска (МСК, лента «обороты 0») и старые строки БД (акт 1.0 —
  факты без ленты).

Запуск:
    py -3 scripts/check_v5122_data.py --dsn-file .dsn            # все разделы
    py -3 scripts/check_v5122_data.py --dsn-file .dsn p12 header # выбранные
Код возврата 1 — есть расхождения.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import asyncpg  # noqa: E402

from analytics.labels import fault_code_close_text, fmt_local, gate_supp_label  # noqa: E402
from online import engine as E  # noqa: E402
from online import status_assembler as sa  # noqa: E402

MSK = ZoneInfo("Europe/Moscow")
U = timezone.utc
RO = {"default_transaction_read_only": "on"}
MACH = {"ДГУ №1": ("6003790403", "pcc", 1), "ДГУ №2": ("6003790403", "pcc", 2),
        "ДЭС №3": ("1126246373", "pcc", 1)}
_fails: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  OK   " if cond else "  FAIL ") + msg)
    if not cond:
        _fails.append(msg)


def M(d: int, h: int, mi: int, s: int = 0) -> datetime:
    return datetime(2026, 10, d, h, mi, s, tzinfo=MSK).astimezone(U)


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


async def load(conn, key, t0, t1):
    sn, et, pid = MACH[key]
    segs = [dict(r) for r in await conn.fetch("""
        SELECT id, t_start, t_end, run_state, cause_close, gate_suppressed_hash, gate_log,
               characteristics_json->>'stop_kind' AS stop_kind
        FROM auto_segments WHERE router_sn=$1 AND equip_type=$2 AND panel_id=$3
          AND t_start < $5 AND (t_end IS NULL OR t_end > $4) ORDER BY t_start""", sn, et, pid, t0, t1)]
    eps = [dict(r) for r in await conn.fetch("""
        SELECT id, scenario, source, severity, t_open, t_close, close_reason, gate_suppressed, addr, bit
        FROM alarm_episodes WHERE router_sn=$1 AND equip_type=$2 AND panel_id=$3
          AND t_open < $5 AND (t_close IS NULL OR t_close > $4) ORDER BY t_open""", sn, et, pid, t0, t1)]
    for s in segs:
        s["gate_log"] = [e for e in _j(s["gate_log"], []) if isinstance(e, dict)]
    return segs, eps


def _open_at(e, t, tol: float = 0.0):
    """Эпизод открыт в момент t. tol — допуск на дебаунс закрытия: t_close в БД
    — конец нарушения, а строка закрывается после трёх чистых циклов, так что
    вердикт, пришедший через 1–2 мин, ещё застаёт её открытой."""
    return _u(e["t_open"]) <= t and (e["t_close"] is None
                                     or _u(e["t_close"]) > t - timedelta(seconds=tol))


# Допуск дебаунса закрытия эпизода (3 цикла по 30 с + отставание обработки)
_DEBOUNCE_TOL = 180.0
# Мост эпизода через суточный рез (v5.1.21): до него эпизод на резе закрывался,
# а на 09:00:00 открывался новый того же сценария (14904 → 14916) — в нынешнем
# движке это один эпизод. В проигрыше такой новый — продолжение перенесённого
_BRIDGE_SEC = 600.0
# Не тревоги гейта: риск нагара (состояние, v5.1.3) и режимные замечания (v5.1.12)
_NOT_GATE = {"COKING_RISK", "NO_LOAD_TOO_LONG", "IDLE_TOO_LONG"}


def simulate(segs, eps):
    """Проигрыш цепочки по правилам v5.1.22. Возвращает {id эпизода: (вид, источник)}
    и журнал переносов [(граница, сегмент, перенесено ли, id)]."""
    an = [e for e in eps if e["source"] == "analytics" and e.get("close_reason") != "transition_event"
          and e["scenario"] not in _NOT_GATE]
    panel = [e for e in eps if e["source"] == "panel"]
    supp: dict[int, tuple] = {}
    carries: list[tuple] = []
    row = {"key": None, "own": False, "log": [], "src": None}
    for i, s in enumerate(segs):
        t0, t1 = _u(s["t_start"]), _u(s["t_end"]) or datetime.now(U)
        own_recs = sorted([(_u(e.get("ts")), e) for e in s["gate_log"]
                           if sa.gate_entry_kind(e) in sa.GATE_OWN_KINDS and sa._applied_cancel(e)],
                          key=lambda x: x[0])
        events = [("dec", t, e) for t, e in own_recs if t is not None and t0 <= t < t1]
        events += [("ep", _u(e["t_open"]), e) for e in an if t0 <= _u(e["t_open"]) < t1]
        events.sort(key=lambda x: (x[1], 0 if x[0] == "ep" else 1))
        log_so_far: list[dict] = []
        for kind, t, x in events:
            if kind == "dec":
                sc = frozenset(a.get("scenario") for a in x.get("alarms") or []
                               if a.get("scenario") not in ("CONTROLLER_FAULT", None))
                log_so_far.append(x)
                src = sa.gate_decision_source(log_so_far, "K") or {"ts": x.get("ts")}
                # Своё решение: эпизоды состава, открытые на момент разбора
                ids = [e["id"] for e in an if e["scenario"] in sc and _open_at(e, t, _DEBOUNCE_TOL)]
                for i_ in ids:
                    supp.setdefault(i_, (sa.gate_entry_kind(x), {"ts": src.get("ts"), "segment_id": None}))
                row = {"key": sc, "own": True, "log": [{"ts": x.get("ts"), "kind": "llm", "decision": "cancel",
                                                        "decision_applied": True, "suppressed_hash": "K",
                                                        "episodes": ids}],
                       "src": {"ts": src.get("ts"), "segment_id": None}}
                continue
            e = x
            if not row["key"] or e["id"] in supp:
                continue
            comp = frozenset(a["scenario"] for a in an if _open_at(a, t))
            if comp != row["key"] or e["scenario"] not in row["key"]:
                continue
            if not row["own"]:
                # Перенесённое снятие: новый эпизод не наследует — хеш снимается
                if _u(e["t_open"]) >= t0:
                    row["key"] = None
                continue
            # Запреты на момент открытия
            dets = [{"scenario": "CONTROLLER_FAULT", "severity": p["severity"], "fault_codes": [],
                     "values": {"fault_start": _u(p["t_open"]).isoformat()}}
                    for p in panel if _open_at(p, t)]
            bans = sa.suppression_bans({"t_start": t0, "characteristics_json": {"stop_kind": s["stop_kind"]}},
                                       dets)
            if bans:
                # Повтор, когда запрет снимется, а эпизод ещё идёт
                ends = [_u(p["t_close"]) for p in panel if _open_at(p, t) and p["t_close"] is not None]
                t_free = max(ends) if ends and len(ends) == len([p for p in panel if _open_at(p, t)]) else None
                if t_free is None or not _open_at(e, t_free) or "emergency" in bans:
                    supp[e["id"]] = ("refused:" + ",".join(bans), None)
                    continue
                supp[e["id"]] = ("inherited(после запрета)", row["src"])
                continue
            supp[e["id"]] = ("inherited", row["src"])
        # Граница с следующим сегментом
        if i + 1 < len(segs) and s["t_end"] is not None:
            nxt = segs[i + 1]
            b = _u(s["t_end"])
            if _u(nxt["t_start"]) != b or not row["key"]:
                row = {"key": None, "own": False, "log": [], "src": None}
                continue
            mem = {e["scenario"]: {"id": e["id"]} for e in an if _open_at(e, b - timedelta(seconds=1))
                   and _open_at(e, b)}
            live = set(mem)
            ids_supp = [i_ for i_ in supp if not str(supp[i_][0]).startswith("refused")]
            gs = {"suppressed_hash": "K", "gate_log": [dict(r, episodes=sorted(set(r.get("episodes") or [])
                                                                                | set(ids_supp)))
                                                        for r in row["log"]]
                  if row["own"] else row["log"]}
            if row["own"] and gs["gate_log"]:
                gs["gate_log"][-1]["ts"] = row["src"]["ts"]
            cr = E._gate_carry(gs, [(NS(run_state=s["run_state"]), s["id"], _u(s["t_start"]), b)], mem,
                               live, live if nxt["run_state"] != s["run_state"] else None,
                               nxt["run_state"], b, [])
            carried = bool(cr and cr.get("record"))
            carries.append((b, s["id"], nxt["id"], carried, cr["ids"] if cr else []))
            if carried:
                src = cr["record"]["source"]
                _ids = list(cr["ids"])
                # Мост v5.1.21: новый эпизод того же сценария ровно на резе — продолжение
                if s.get("cause_close") == "DAILY_BOUNDARY":
                    for x in [e for e in an if e["id"] in cr["ids"]]:
                        if x["t_close"] is None or _u(x["t_close"]) > b + timedelta(seconds=_BRIDGE_SEC):
                            continue
                        _ids += [y["id"] for y in an if y["scenario"] == x["scenario"] and y["id"] != x["id"]
                                 and abs((_u(y["t_open"]) - b).total_seconds()) <= 1.0]
                for i_ in _ids:
                    k0, s0 = supp.get(i_, ("carry", None))
                    supp[i_] = (k0, {"ts": (s0 or src)["ts"], "segment_id": src.get("segment_id") or s["id"]})
                row = {"key": row["key"], "own": False,
                       "log": [dict(cr["record"], episodes=sorted(set(_ids)))],
                       "src": {"ts": src["ts"], "segment_id": src.get("segment_id") or s["id"]}}
            else:
                row = {"key": None, "own": False, "log": [], "src": None}
    return supp, carries


def label(supp, ep_id, seg_t0, seg_t1):
    k, src = supp.get(ep_id, (None, None))
    if not k or str(k).startswith("refused"):
        return None
    if not src:
        return "снято ИИ"
    t = _u(src["ts"])
    return gate_supp_label({**src, "own": seg_t0 <= t < seg_t1}, MSK)


def seg_of(segs, t):
    for s in segs:
        if _u(s["t_start"]) <= t and (s["t_end"] is None or t < _u(s["t_end"])):
            return s
    return None


async def p11(conn):
    print("\n== P1-1: снятие гейтом по своим эпизодам ==")
    # ДГУ №1 13–16.10
    segs, eps = await load(conn, "ДГУ №1", M(13, 9, 0), M(16, 12, 0))
    supp, carries = simulate(segs, eps)
    _rs = {s["id"]: s["run_state"] for s in segs}
    print("  Переносы ДГУ №1 (граница МСК, из → в (режим), перенесено, эпизоды):")
    for b, a, n, c, ids in carries:
        if c or ids or M(15, 13, 0) <= b <= M(15, 13, 10):
            print(f"    {f(b)}  #{a} ({_rs.get(a)}) → #{n} ({_rs.get(n)})  {'перенесено' if c else 'нет'} {ids}")
    s90 = next((s for s in segs if s["id"] == 35890), None)
    k16 = supp.get(14916)
    check(k16 is not None and k16[1] and k16[1]["segment_id"] == 35876
          and label(supp, 14916, _u(s90["t_start"]), _u(s90["t_end"])) == "снято по разбору от 14.10 03:36 (#35876)",
          f"ДГУ №1 14916 — перенос через рез, подпись #35890: "
          f"{label(supp, 14916, _u(s90['t_start']), _u(s90['t_end'])) if s90 else None} ({k16})")
    _b15 = [c for c in carries if c[0] == M(15, 9, 0)]
    check(_b15 and _b15[0][3] and 14916 in _b15[0][4], f"ДГУ №1 15.10 09:00 — 14916 идёт, перенос: {_b15}")
    # 15.10 13:04:43 Работа → смена режима (без стопа: перенос по В5, если эпизод ещё
    # идёт), 13:07:32 — стоп: дальше снятие не переходит
    _b1304 = [c for c in carries if M(15, 13, 0) <= c[0] <= M(15, 13, 10)]
    check(_b1304 and not any(c[3] and _rs.get(c[2]) == 0 for c in _b1304)
          and not any(c[3] for c in carries if M(15, 13, 5) <= c[0] <= M(16, 12, 0)),
          f"ДГУ №1 15.10 13:04–13:07 — через стоп снятие не переходит: {_b1304}")
    for eid, seg_id in ((14930, 35944), (14931, 35949)):
        e = next((x for x in eps if x["id"] == eid), None)
        check(e is not None and e["id"] not in supp,
              f"ДГУ №1 {eid} (#{seg_id}) — без снятия (в БД сейчас: "
              f"{'снято' if e and e['gate_suppressed'] else 'не снято'}): {supp.get(eid)}")
    _stops = [c for c in carries if c[3] and any(
        s["id"] == c[2] and s["run_state"] == 0 for s in segs)]
    check(not _stops, f"ДГУ №1 13–16.10 — ни одного переноса в стоп: {_stops}")

    # ДЭС №3 13–15.10
    segs, eps = await load(conn, "ДЭС №3", M(13, 9, 0), M(15, 12, 0))
    supp, carries = simulate(segs, eps)
    s91 = next((s for s in segs if s["id"] == 35891), None)
    lp91 = [e for e in eps if e["scenario"] == "LIMIT_PROXIMITY" and s91
            and _u(s91["t_start"]) <= _u(e["t_open"]) < _u(s91["t_end"])]
    _inh = [e["id"] for e in lp91 if supp.get(e["id"], ("",))[0] == "inherited"]
    _db = [e["id"] for e in lp91 if e["gate_suppressed"]]
    _lab = {label(supp, i, _u(s91["t_start"]), _u(s91["t_end"])) for i in _inh} if s91 else set()
    print(f"  ДЭС №3 #35891: близость — {len(lp91)} эпизодов, в БД снято {len(_db)}, станет — "
          f"своё {len([e for e in lp91 if supp.get(e['id'], ('',))[0] == 'llm'])}, "
          f"наследование {len(_inh)}, подписи {_lab}")
    check(len(_inh) == 9 and _lab == {"снято ИИ (разбор 09:01)"},
          f"ДЭС №3 #35891 — 9 наследований близости, источник — разбор 09:01:59: {len(_inh)}, {_lab}")
    check(not [e["id"] for e in lp91 if str(supp.get(e["id"], ("",))[0]).startswith("refused")],
          "ДЭС №3 #35891 — 1453 с начала строки не запрет")
    check(14920 not in supp, f"ДЭС №3 15.10 — 14920 без перенесённого снятия (свой разбор): {supp.get(14920)}")
    _b = [c for c in carries if c[0] == M(15, 9, 0)]
    check(_b and not _b[0][3], f"ДЭС №3 15.10 09:00 — 14919 кончился в 08:36:42, хеш не переходит: {_b}")

    # ДГУ №2 15.10
    segs, eps = await load(conn, "ДГУ №2", M(15, 5, 0), M(16, 12, 0))
    supp, carries = simulate(segs, eps)
    for eid, seg_id in ((14921, 35900), (14922, 35926)):
        check(eid not in supp, f"ДГУ №2 {eid} (#{seg_id}) — без перенесённого снятия, свой разбор: "
                               f"{supp.get(eid)}")
    check(not [c for c in carries if c[3] and any(s["id"] == c[2] and s["run_state"] == 0 for s in segs)],
          "ДГУ №2 15.10 — ни одного переноса в стоп")


async def diff30(conn):
    print("\n== P1-1: 30 дней — было снято, станет живым ==")
    t0, t1 = M(17, 0, 0) - timedelta(days=30), datetime.now(U)
    total_old = total_new = 0
    all_lost: set[int] = set()
    for key in MACH:
        segs, eps = await load(conn, key, t0, t1)
        supp, carries = simulate(segs, eps)
        # Журнал гейта до 17.09 не сохранялся; риск нагара и режимные замечания — не состав гейта
        an = [e for e in eps if e["source"] == "analytics" and _u(e["t_open"]) >= t0
              and e["scenario"] not in _NOT_GATE and e.get("close_reason") != "transition_event"]
        old = {e["id"] for e in an if e["gate_suppressed"]}
        new = {i for i, (k, _s) in supp.items() if not str(k).startswith("refused")}
        lost = sorted(old - new)
        gained = sorted((new - old) & {e["id"] for e in an})
        all_lost |= set(lost)
        total_old += len(old)
        total_new += len(new & {e["id"] for e in an})
        print(f"  {key}: снято было {len(old)}, станет {len(new & {e['id'] for e in an})}; "
              f"станет живым {lost}; станет снятым {gained}")
        for i in lost:
            e = next(x for x in an if x["id"] == i)
            s = seg_of(segs, _u(e["t_open"]))
            print(f"     {i} {e['scenario']} {f(e['t_open'])} — #{s['id'] if s else '?'}")
        print(f"     переносов через рез/смену режима: {sum(1 for c in carries if c[3])}, "
              f"в стоп: {sum(1 for c in carries if c[3] and any(s['id'] == c[2] and s['run_state'] == 0 for s in segs))}")
    print(f"  Итого снятых эпизодов: было {total_old}, станет {total_new}")
    # Раздел 1 плана: через стоп и пуск — 14880, 14930, 14931 (ДГУ №1), 14921, 14922 (ДГУ №2);
    # новый эпизод после реза — 14920 (ДЭС №3). Своих решений и продолжений через рез это не трогает
    _want = {14880, 14920, 14921, 14922, 14930, 14931}
    check({i for i in all_lost if i <= 14931} == _want,
          f"30 дней: живыми станут ровно снятые переносом через стоп и новый эпизод после реза: "
          f"{sorted(i for i in all_lost if i <= 14931)}")


async def migration_preview(conn):
    print("\n== P1-1: разовый перенос (пробный, без записи) ==")
    rows = await conn.fetch("""SELECT id, router_sn, equip_type, panel_id, t_start, gate_suppressed_hash, gate_log
                               FROM auto_segments WHERE t_end IS NULL ORDER BY id""")
    n_rows, ep_ids = [], []
    for r in rows:
        log = [e for e in _j(r["gate_log"], []) if isinstance(e, dict)]
        h = r["gate_suppressed_hash"]
        if h and sa.gate_decision_source(log, h) is None:
            n_rows.append(r["id"])
        has_own = any(sa.gate_entry_kind(e) in sa.GATE_OWN_KINDS and sa._applied_cancel(e) for e in log)
        if not has_own:
            eps = await conn.fetch("""SELECT id FROM alarm_episodes WHERE router_sn=$1 AND equip_type=$2
                                      AND panel_id=$3 AND gate_suppressed AND t_open >= $4 ORDER BY id""",
                                   r["router_sn"], r["equip_type"], r["panel_id"], r["t_start"])
            ep_ids += [e["id"] for e in eps]
        print(f"  #{r['id']} {r['router_sn']}/{r['panel_id']} с {f(r['t_start'])}: хеш {h or '—'}, "
              f"своё решение {'есть' if has_own else 'нет'}")
    print(f"  Хеш будет сброшен у строк {n_rows}; снятие обнулится у эпизодов {ep_ids}")
    check(14931 in ep_ids or not any(r["id"] == 35949 for r in rows),
          f"разовый перенос — 14931 теряет снятие (#35949 открыта): {ep_ids}")


# ══ Этап 2: P1-2 код 40012 на закрытии, P1-3 обрыв аварией, шапка заключения ══

_ENG = {"6003790403": "cummins_kta50", "1126246373": "cummins_qsx15"}
_CFGS: dict = {}


def _cfg(sn: str):
    eng = _ENG.get(sn, "cummins_kta50")
    if eng not in _CFGS:
        from analytics.config import AnalyticsConfig
        _CFGS[eng] = AnalyticsConfig.from_pair(ROOT / "knowledge_base", "pcc3300", eng)
    return _CFGS[eng]


async def _fcc_db(r) -> tuple[dict | None, str | None]:
    """Код 40012 на закрытии по границам сегмента БД — тем же помощником нарезки."""
    from analytics import source as _src
    from analytics.segmenter import _state_periods, fault_code_close
    cfg = _cfg(r["router_sn"])
    t0, t1 = _u(r["t_start"]), _u(r["t_end"])
    enum = await _src.get_enum_periods(r["router_sn"], r["equip_type"], r["panel_id"], t0,
                                       t1 + timedelta(seconds=60), addrs=_src.enum_read_addrs(cfg))
    rs = [p for p in enum if p["addr"] == 40011 and abs((_u(p["state_start"]) - t1).total_seconds()) <= 1]
    sp = _state_periods(enum, cfg).get(cfg.role_to_addr("LAST_FAULT_CODE")) or []
    # v5.1.22, ревью verdict-2: режим сегмента — у нестопового кода, вытесненного
    # другим, сброс после кадра снятия не учитывается
    _ch = r.get("characteristics_json") if hasattr(r, "get") else r["characteristics_json"]
    if isinstance(_ch, str):
        try:
            _ch = json.loads(_ch)
        except ValueError:
            _ch = None
    rec = fault_code_close(sp, enum, cfg, t0, t1, next_state=int(rs[0]["value"]) if rs else None,
                           run_state=r["run_state"],
                           tripped=bool(isinstance(_ch, dict) and _ch.get("closed_by_trip")))
    return rec, fault_code_close_text(rec, MSK)


async def p12(conn):
    print("\n== P1-2: код 40012 на закрытии сегмента (сброшен / снят / сменился) ==")
    want = {
        35931: "Код 1448 сброшен: сброс неисправностей 09:26:32 (40569), код снят 09:26:43; "
               "переключатель «Выкл» (40010) в том же кадре",
        35941: "Код 234 сброшен: сброс неисправностей 09:29:22 (40569), код снят 09:29:22; "
               "переключатель «Выкл» (40010) в том же кадре",
        35848: "Код 1438 сброшен: сброс неисправностей 10:40:19 (40569), код снят 10:40:20; "
               "переключатель «Выкл» (40010) в том же кадре",
        35855: "Код 1438 сброшен: переключатель «Выкл» (40010), код снят 10:44:31",
        35861: "Код 1438 сброшен: переключатель «Выкл» (40010), код снят 13:11:45; сразу после сброса в "
               "регистре код 1248 «Предупреждение двигателя — общее» (предупреждение) — он относится к "
               "следующему сегменту",
        35869: "Код 1248 снят в 14:59:49, в момент пуска",
        35872: None,      # 1453 висит с 15:01:38 — «Несброшенная», как раньше
        35929: None, 35939: None,   # в «Работе» код встаёт ровно в t_end — записи нет
    }
    for sid, exp in want.items():
        r = await conn.fetchrow("SELECT * FROM auto_segments WHERE id=$1", sid)
        if r["cause_close"] == "DAILY_BOUNDARY" or r["t_end"] is None:
            rec, txt = None, None       # рез по краю окна — запись не делается
        else:
            rec, txt = await _fcc_db(r)
        check(txt == exp, f"#{sid} ({f(r['t_start'])}–{f(r['t_end'])}, {r['cause_close']}): {txt}")

    print("  30 дней — все SHUTDOWN_CLEARED / FAULT_CLEARED:")
    rows = await conn.fetch("""SELECT * FROM auto_segments WHERE cause_close IN ('SHUTDOWN_CLEARED','FAULT_CLEARED')
                               AND t_start > now() - interval '30 days' ORDER BY t_start""")
    hang = 0
    for r in rows:
        rec, txt = await _fcc_db(r)
        old = "Несброшенная неисправность" in (r["report_md"] or "")
        hang += txt is None
        print(f"    #{r['id']} {r['router_sn'][-4:]}/{r['panel_id']} {f(r['t_end'])} {r['cause_close']}: "
              f"{'было «Несброшенная» → ' if old else ''}{txt or 'код висит за концом / нет данных'}")
    check(rows and hang == 0, f"30 дней: у всех {len(rows)} сегментов, закрытых снятием аварии, — «сброшен/снят», "
                              f"висящих {hang}")

    print("  30 дней — все блоки «Несброшенная» в отчётах:")
    rows = await conn.fetch("""SELECT * FROM auto_segments WHERE report_md LIKE '%Несброшенная неисправность%'
                               AND t_start > now() - interval '30 days' AND t_end IS NOT NULL ORDER BY t_start""")
    from analytics import source as _src
    stats = {"сброшен": 0, "снят": 0, "сменился": 0, "висит": 0, "рез по краю": 0}
    mism = []
    for r in rows:
        if r["cause_close"] == "DAILY_BOUNDARY":
            stats["рез по краю"] += 1
            continue
        rec, txt = await _fcc_db(r)
        # Независимо: последний период 40012, начавшийся до конца, кончился ровно в t_end
        t1 = _u(r["t_end"])
        enum = await _src.get_enum_periods(r["router_sn"], r["equip_type"], r["panel_id"], _u(r["t_start"]),
                                           t1 + timedelta(seconds=60), addrs=[40012])
        last = [p for p in enum if _u(p["state_start"]) < t1 and p["value"] not in (65535,)]
        last = last[-1] if last else None
        ended = bool(last and int(last["value"]) > 0 and last["state_end"] is not None
                     and abs((_u(last["state_end"]) - t1).total_seconds()) <= 1)
        if ended != (rec is not None):
            mism.append(r["id"])
        if rec is None:
            stats["висит"] += 1
        elif rec.get("next_code"):
            stats["сменился"] += 1
        elif rec.get("reset"):
            stats["сброшен"] += 1
        else:
            stats["снят"] += 1
    print(f"    всего {len(rows)}: {stats}")
    check(not mism, f"«Несброшенная» 30 дней: запись есть ровно там, где код снят в t_end (расхождения {mism})")


async def p13(conn):
    print("\n== P1-3: отметка «… прервана аварийным остановом» ==")
    from analytics import source as _src
    from analytics.segmenter import _state_periods, _trip_mark, shutdown_intervals
    rows = await conn.fetch("""
        SELECT s.id sid, s.t_start s0, s.characteristics_json->>'stop_kind' sk,
               s.characteristics_json->'attempt' att, p.id pid, p.run_state prs, p.t_start p0, p.t_end p1,
               s.router_sn, s.equip_type, s.panel_id
        FROM auto_segments s JOIN auto_segments p
          ON p.router_sn = s.router_sn AND p.equip_type = s.equip_type AND p.panel_id = s.panel_id
         AND p.t_end = s.t_start AND p.run_state <> 0
        WHERE s.run_state = 0 AND s.t_start > now() - interval '60 days' ORDER BY s.t_start""")
    marked, wrong = [], []
    by_mode: dict[int, int] = {}
    for r in rows:
        cfg = _cfg(r["router_sn"])
        t0, t1 = _u(r["p0"]), _u(r["p1"])
        enum = await _src.get_enum_periods(r["router_sn"], r["equip_type"], r["panel_id"], t0,
                                           t1 + timedelta(seconds=120), addrs=_src.enum_read_addrs(cfg))
        faults = await _src.get_fault_periods(r["router_sn"], r["equip_type"], r["panel_id"], t0,
                                              t1 + timedelta(seconds=120), fault_addrs=cfg.whitelist_fault)
        iv = shutdown_intervals(faults, cfg, t1 + timedelta(seconds=120), enum)
        codes = _state_periods(enum, cfg).get(cfg.role_to_addr("LAST_FAULT_CODE")) or []
        att = _j(r["att"], None)
        # v5.1.22, ревью verdict-5: отметка — только перед аварийным стопом, а вид
        # стопа — по нынешней классификации (авария держится в момент начала
        # стопа, как _classify_stop_periods), не по записи БД: у строк до модели
        # двух видов стопа его нет, у #35651/#35653 (1452 без бита) — устаревший SIMPLE
        sk_now = "EMERGENCY" if any(s <= t1 < e for s, e in iv) else "SIMPLE"
        nxt = {"value": 0, "state_start": t1, "stop_kind": sk_now, "attempt": att}
        tr = _trip_mark(int(r["prs"]), t0, t1, iv, nxt, set(), codes, faults, cfg)
        if tr:
            from analytics.labels import run_trip_name, run_trip_text
            marked.append(r["pid"])
            print(f"    #{r['pid']} {run_trip_name(r['prs'])}: {fmt_local(tr['t'], MSK)} — {run_trip_text(tr)} "
                  f"(следом #{r['sid']} {r['sk']})")
        # Независимо: стоп в БД аварийный или 40013 ∈ {3, 4} встал в момент падения
        # (±15 с) — у сегментов до модели двух видов стопа (v4.9.68) вида нет, у
        # #35653 (1452 без бита) — SIMPLE, хотя 40013 = 4
        fall_type = any(p["addr"] == 40013 and p["value"] in (3, 4)
                        and abs((_u(p["state_start"]) - t1).total_seconds()) <= 15 for p in enum)
        if bool(tr) != (((r["sk"] == "EMERGENCY") or sk_now == "EMERGENCY" or fall_type) and not att):
            wrong.append((r["pid"], r["sk"], sk_now, bool(att), fall_type, bool(tr)))
        if tr:
            by_mode[int(r["prs"])] = by_mode.get(int(r["prs"]), 0) + 1
    print(f"  60 дней: пар «не стоп → стоп» {len(rows)}, отмечено {len(marked)}, по режимам {by_mode}")
    check(not wrong, f"60 дней: отметка ровно у сегментов перед аварийным стопом / типом 40013 Shutdown "
                     f"на падении (не плашкой): расхождения {wrong}")
    # Раздел P1-3 плана (вопрос 3): 23 падения в аварию из нестоповых режимов за 60 дней —
    # 19 из «Работы», 2 из «Прогрева», 1 из «Охлаждения», 1 из «Перехода на ХХ»
    check(by_mode == {3: 19, 2: 2, 5: 1, 6: 1}, f"60 дней: 19/2/1/1 по режимам, как в плане: {by_mode}")
    check(35929 in marked and 35939 in marked and 35944 not in marked,
          f"16.10: отметка у #35929 и #35939, у #35944 — нет")
    n3 = await conn.fetchval("""SELECT count(*) FROM enum_history WHERE addr = 40013 AND value = 3
                                AND state_start > now() - interval '120 days'""")
    print(f"  40013 = 3 (останов с охлаждением) за 120 дней: {n3}")


async def _replay(name, sn, et, p, eng, a, b):
    from analytics import source as _src  # noqa: F401
    from analytics.config import AnalyticsConfig
    from analytics.contract import CokingRisk
    cfg = AnalyticsConfig.from_pair(ROOT / "knowledge_base", "pcc3300", eng)
    segs, _inc, _gaps, _chr = await E._run_segment(sn, et, p, "", a, b, cfg, CokingRisk())
    return segs


async def header(conn):
    print("\n== Шапка входа заключения (P1-2, P1-3, P1-5): перепрогон окон ==")
    from analytics.serializer import build_summary_parts, render_summary_md, to_markdown
    from analytics.fault_ref import FaultRef
    from corpus.preprocessor import conclusion_facts, extract_verdict_alarm
    fr = FaultRef(str(ROOT / "knowledge_base" / "controllers" / "pcc3300"))
    windows = [("ДГУ №1", "6003790403", "pcc", 1, "cummins_kta50", M(16, 9, 20), M(16, 9, 40),
                {35929: M(16, 9, 26, 23), 35931: M(16, 9, 26, 43), 35939: M(16, 9, 28, 53),
                 35941: M(16, 9, 29, 22), 35944: M(16, 9, 32, 33)}),
               ("ДЭС №3", "1126246373", "pcc", 1, "cummins_qsx15", M(13, 10, 30), M(13, 15, 10),
                {35848: M(13, 10, 40, 20), 35855: M(13, 10, 44, 31), 35861: M(13, 13, 11, 45),
                 35869: M(13, 14, 59, 49)})]
    for name, sn, et, p, eng, a, b, ids in windows:
        segs = await _replay(name, sn, et, p, eng, a, b)
        print(f"  {name} {f(a)}–{f(b)}: сегментов {len(segs)}")
        by_end = {_u(s.t_end): s for s in segs if s.t_end}
        for sid, te in ids.items():
            s = by_end.get(te)
            if s is None:
                check(False, f"#{sid}: в перепрогоне нет сегмента с концом {f(te)}")
                continue
            parts = build_summary_parts([s], [], tz=MSK)
            row = {"run_state": s.run_state, "characteristics_json": s.to_dict(), "report_summary_json": parts,
                   "t_end": _u(s.t_end), "cause_close": s.cause_close}
            facts = conclusion_facts(row)
            rep = to_markdown([s], sn, et, p, a, b, tz=MSK, fault_ref=fr)
            print(f"    #{sid} → {s.run_state_label} {f(s.t_start)}–{f(s.t_end)}: "
                  f"{parts['verdict']['title']}")
            for x in facts:
                print(f"       · {x}")
            hours = "13 643 ч" if sn == "6003790403" else "15 297 ч"
            check(any(hours in x for x in facts), f"#{sid}: наработка {hours} в шапке")
            check("Несброшенная" not in rep and "не сброшен кнопкой" not in rep,
                  f"#{sid}: в отчёте нет «Несброшенной» и «не сброшен кнопкой»")
            if sid in (35929, 35939):
                check(any("Он не штатный: не пиши «штатный», «все параметры в норме»" in x for x in facts)
                      and extract_verdict_alarm(row)[1] == "CAUTION"
                      and "Работа прервана аварийным остановом" in render_summary_md(parts),
                      f"#{sid}: оборван аварией — CAUTION, запрет «штатно» в шапке")
            if sid == 35931:
                check(any(x.startswith("Авария снята в 09:26:43 (через 20 с)") for x in facts)
                      and any(x.startswith("Код 1448 сброшен: сброс неисправностей 09:26:32 (40569), "
                                           "код снят 09:26:43") for x in facts), "#35931: снятие и код в шапке")
            if sid == 35941:
                check(any(x.startswith("Авария снята в 09:29:22 (через 29 с)") for x in facts)
                      and any(x.startswith("Код 234 сброшен") for x in facts), "#35941: снятие и код в шапке")
            if sid == 35944:
                check(s.closed_by_trip is None and not any("аварийным остановом" in x for x in facts),
                      "#35944: конец — охлаждение без аварии, отметки нет")
            if sid == 35861:
                check(any("сразу после сброса в регистре код 1248" in x for x in facts),
                      "#35861: 1438 сброшен, следом 1248 — следующий сегмент")
            if sid == 35869:
                check(any(x.startswith("Код 1248 снят в 14:59:49, в момент пуска") for x in facts),
                      "#35869: 1248 снят в момент пуска, без «сброшен»")
        if name == "ДЭС №3":
            s72 = next((s for s in segs if _u(s.t_start) == M(13, 14, 59, 49)), None)
            rep = to_markdown([s72], sn, et, p, a, b, tz=MSK, fault_ref=fr) if s72 else ""
            check(s72 is not None and "**⚠ Несброшенная неисправность:** код `1453`" in rep,
                  "#35872: 1453 висит с 15:01:38 — «Несброшенная» остаётся")



# ── Этап 3: акт и вход совета (P1-4), заключение фактами (P1-5), МСК (P1-6) ──

# Действующий вход совета до v5.1.22, символов (без блока оборудования и
# состояний; scratchpad dgu1_1610/prompt_359xx.txt, собран по БД 16.10)
_OLD_PROMPT = {35931: 15437, 35941: 20979}
_REPLAY: dict = {}


async def _replay_window(key, a, b):
    """Окно кодом движка (_run_segment): сегменты, акты (с контекстом от якоря —
    те же чтения источника, что у движка), только чтение."""
    if (key, a) in _REPLAY:
        return _REPLAY[(key, a)]
    from analytics.config import AnalyticsConfig
    from analytics.contract import CokingRisk
    sn, et, p = MACH[key]
    cfg = AnalyticsConfig.from_pair(ROOT / "knowledge_base", "pcc3300", _ENG.get(sn, "cummins_kta50"))
    segs, inc, _g, _c = await E._run_segment(sn, et, p, "", a, b, cfg, CokingRisk())
    _REPLAY[(key, a)] = (segs, inc, cfg)
    return _REPLAY[(key, a)]


async def _cycle(conn, sn, et, p, inc):
    """Сегменты цикла из БД — как incident_gate._material (get_segments_between)."""
    wf = datetime.fromisoformat(inc["window"]["from"])
    st = datetime.fromisoformat(inc["stop_ts"])
    return [dict(r) for r in await conn.fetch("""
        SELECT id, run_state, t_start, t_end, characteristics_json FROM auto_segments
        WHERE router_sn=$1 AND equip_type=$2 AND panel_id=$3 AND t_end IS NOT NULL
          AND t_end > $4 AND t_start < $5 ORDER BY t_start""", sn, et, p, wf, st)]


async def _ctx(conn, sn, et, p, stop):
    """Контекст совета (incident_gate._context): реестр — чтением этой же БД."""
    import db.analytics as dba
    from config import settings as app_settings
    from online import incident_gate as ig

    async def _eq(r, e, pid):
        row = await conn.fetchrow("""SELECT router_sn, equip_type, panel_id, name, manufacturer, model,
                                            engine_sn, kb_path, controller_id, engine_id
                                     FROM equipment_registry WHERE router_sn=$1 AND equip_type=$2
                                       AND panel_id=$3""", r, e, pid)
        return dict(row) if row else None
    _orig = dba.get_equipment
    dba.get_equipment = _eq
    app_settings.knowledge_base_path = ROOT / "knowledge_base"
    try:
        return await ig._context(sn, et, p, stop)
    finally:
        dba.get_equipment = _orig


async def p14(conn):
    print("\n== P1-4 / P1-6: акт с лентой отсчётов и новый вход совета (#35931, #35941; без шлюза) ==")
    from analytics.reconstructor import fall_facts_ru
    from online.incident_gate import build_incident_prompt
    segs, inc, cfg = await _replay_window("ДГУ №1", M(16, 9, 20), M(16, 9, 40))
    sn, et, p = MACH["ДГУ №1"]
    # v5.1.23 (п.1): в ленте акта колонка «задание» (40595) — оно в фактах
    # рядом с оборотами и частотой
    want_ff = {
        35931: ("09:26:03 1696 об/мин / 56,8 Гц / задание 30,6 Гц → 09:26:12 1247 об/мин / 41,5 Гц / "
                "задание 55 Гц → 09:26:23 (кадр останова) 76 об/мин / 0 Гц / задание 0 Гц"),
        35941: ("09:28:42 1294 об/мин / 42,6 Гц / задание 55 Гц → 09:28:52 898 об/мин / задание 0 Гц "
                "(частота 42,6 Гц держится с 09:28:42) → 09:28:53 (кадр останова) 0 Гц (обороты 898 об/мин "
                "держатся с 09:28:52; задание 0 Гц держится с 09:28:52)"),
    }
    for sid, stop in ((35931, M(16, 9, 26, 23)), (35941, M(16, 9, 28, 53))):
        act = inc.get(stop.isoformat())
        if act is None:
            check(False, f"#{sid}: акт в перепрогоне не построен")
            continue
        tr = act.get("trace") or {}
        ff = fall_facts_ru(tr, MSK)
        print(f"  #{sid}: акт {act.get('investigator_version')}, сегмент падения {act.get('fall')}")
        print(f"       лента {f(tr.get('t_from'))}–{f(tr.get('t_to'))}: строк {len(tr.get('rows') or [])}, "
              f"шаг ~{tr.get('step_sec')} с")
        print(f"       {ff}")
        check(act.get("investigator_version") == "1.1" and tr and (act.get("fall") or {}).get("label") == "Работа",
              f"#{sid}: в акте лента отсчётов и сегмент падения «Работа»")
        check(ff is not None and want_ff[sid] in ff and "нагрузка 0 кВт и ток 0 А весь интервал ленты" in ff
              and "автомат разомкнут с 16.10 03:41:02" in ff, f"#{sid}: факты до останова по ленте")
        row = dict(await conn.fetchrow("SELECT * FROM auto_segments WHERE id=$1", sid))
        cyc = await _cycle(conn, sn, et, p, act)
        names = {}
        try:
            from analytics.fault_ref import FaultRef
            fr = FaultRef(str(ROOT / "knowledge_base" / "controllers" / "pcc3300"))
            names = {c: ((fr.lookup(c) or {}).get("description") or {}).get("ru") for c in (1448, 234)}
        except Exception:
            pass
        bare = build_incident_prompt(act, row, cyc, {"code_name": lambda c: names.get(int(c))}, tz=MSK)
        ctx = await _ctx(conn, sn, et, p, stop)
        full = build_incident_prompt(act, row, cyc, ctx, tz=MSK)
        print(f"       вход совета: {len(bare)} симв. без блока оборудования (было {_OLD_PROMPT[sid]}), "
              f"с оборудованием и состоянием {len(full)}")
        _P14[sid] = full
        cyc_part = full.split("Ход цикла", 1)[-1].split("\n\n", 1)[0].splitlines()
        sep = next((i for i, ln in enumerate(cyc_part) if "последний штатный останов" in ln), None)
        check(len(bare) <= _OLD_PROMPT[sid], f"#{sid}: вход не длиннее прежнего ({len(bare)} ≤ {_OLD_PROMPT[sid]})")
        check("Время во входе — МСК." in full and "UTC" not in full and "+00:00" not in full
              and "Параметры за цикл" not in full and "Лента отсчётов сегмента падения" in full
              and "Время в ответе указывай по МСК" in full,
              f"#{sid}: вход — МСК, ход цикла по сегментам, лента отсчётов")
        check(sep is not None and any("713 кВт" in ln for ln in cyc_part[:sep])
              and not any("713" in ln for ln in cyc_part[sep:])
              and "── 03:45:32 последний штатный останов" in cyc_part[sep],
              f"#{sid}: 713 кВт работы до штатного останова — до разделителя 03:45:32")
        if sid == 35941:
            check(sum(1 for ln in cyc_part if ln.startswith("  - Работа: 09:2")) == 2
                  and "1448 «Пониженная частота»; сброшена через 20 с" in full,
                  "#35941: у #35929 и #35939 свои строки, предыдущая авария серии — фактом")
        check(full.count("останов ──") == 1, f"#{sid}: одна строка-разделитель останова в ленте")


_P14: dict = {}


async def p15(conn):
    print("\n== P1-5: заключение аварийного стопа фактами (#35931, #35941, ДЭС №3 13.10) ==")
    from analytics.serializer import build_summary_parts
    from corpus.worker import _attempt_conclusion, _shutdown_conclusion
    segs, inc, cfg = await _replay_window("ДГУ №1", M(16, 9, 20), M(16, 9, 40))
    sn, et, p = MACH["ДГУ №1"]
    by_start = {_u(s.t_start): s for s in segs}
    for sid, stop in ((35931, M(16, 9, 26, 23)), (35941, M(16, 9, 28, 53))):
        s = by_start.get(stop)
        act = inc.get(stop.isoformat())
        if s is None or act is None:
            check(False, f"#{sid}: в перепрогоне нет стопа / акта")
            continue
        row = dict(await conn.fetchrow("SELECT * FROM auto_segments WHERE id=$1", sid))
        row.update({"characteristics_json": s.to_dict(), "incident_json": act,
                    "report_summary_json": build_summary_parts([s], [], tz=MSK), "gate_log": []})
        cyc = await _cycle(conn, sn, et, p, act)
        sc = _shutdown_conclusion(row, cyc, tz=MSK)
        md = (sc or {}).get("conclusion_md") or ""
        print(f"  #{sid}:")
        for ln in md.splitlines():
            if ln.strip():
                print(f"     | {ln}")
        frags = {35931: ["Останов: 16.10 09:26:23 МСК, код панели 1448", "«Работа» 09:25:52–09:26:23, через 31 с после пуска",
                         "Авария снята в 09:26:43 (через 20 с)", "Код 1448 сброшен: сброс неисправностей 09:26:32",
                         "нагрузка 0 кВт и ток 0 А", "автомат разомкнут с 16.10 03:41:02", "13 643 ч",
                         "Предыдущих аварий после штатного останова нет"],
                 35941: ["Останов: 16.10 09:28:53 МСК, код панели 234", "через 20 с после пуска",
                         "Авария снята в 09:29:22 (через 29 с)", "Код 234 сброшен", "898 об/мин (частота 42,6 Гц держится",
                         "16.10 09:26:23 — аварийный стоп (падение с работы); код: 1448", "13 643 ч"]}[sid]
        miss = [x for x in frags if x not in md]
        check(sc is not None and sc["claude_model"] == "Система (без ИИ)" and not miss
              and "UTC" not in md and "40599" not in md and "под нагрузкой" not in md,
              f"#{sid}: заключение фактами — нет фрагментов {miss}")

    # ДЭС №3 13.10: отказы пуска — плашки (факты v5.1.16, теперь МСК) и их лента;
    # старые строки БД (аварийные стопы, акт 1.0 без ленты) — факты без строки ленты
    segs3, inc3, cfg3 = await _replay_window("ДЭС №3", M(13, 10, 30), M(13, 15, 10))
    from analytics.reconstructor import fall_facts_ru, format_analog_trace_ru
    plates = [s for s in segs3 if s.attempt and s.attempt.get("outcome") == "EMERGENCY"]
    print(f"  ДЭС №3 13.10: плашек с аварией {len(plates)}, актов {len(inc3)}")
    for s in plates:
        fc = _attempt_conclusion({"characteristics_json": s.to_dict()})
        act = inc3.get(s.t_start) or {}
        tl = format_analog_trace_ru(act.get("trace"), MSK)
        print(f"     {s.run_state_label} {f(s.t_start)}: {fc and fc['humanized_md']}")
        for ln in tl[:3]:
            print(f"        {ln}")
        print(f"        {fall_facts_ru(act.get('trace'), MSK)}")
        check(fc and " МСК" in fc["humanized_md"] and "+00:00" not in fc["conclusion_md"],
              f"ДЭС №3 {f(s.t_start)}: плашка — время останова МСК")
        check(act.get("trace") and "обороты 0 об/мин" in "\n".join(tl),
              f"ДЭС №3 {f(s.t_start)}: лента попытки — обороты 0 (стартер не провернул)")
    check(len(plates) == 3, f"ДЭС №3 13.10: три отказа пуска с аварией ({len(plates)})")
    for sid in (35848, 35855, 35861):
        row = dict(await conn.fetchrow("SELECT * FROM auto_segments WHERE id=$1", sid))
        inc_db = _j(row["incident_json"], {})
        cyc = await _cycle(conn, row["router_sn"], row["equip_type"], row["panel_id"], inc_db)
        sc = _shutdown_conclusion(row, cyc, tz=MSK)
        print(f"  #{sid} (строка БД, акт {inc_db.get('investigator_version')}): {sc and sc['humanized_md']}")
        check(sc is not None and "ленты отсчётов в акте нет" in sc["conclusion_md"]
              and "1438" in sc["conclusion_md"] and "UTC" not in sc["conclusion_md"],
              f"#{sid}: старый акт — факты без ленты, код 1438, МСК")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn-file", help="DSN одной БД (источник и аналитика) — иначе config.yml")
    ap.add_argument("sections", nargs="*",
                    help="разделы: p11 diff30 migration (этап 1), p12 p13 header (этап 2), "
                         "p14 p15 (этап 3); по умолчанию все")
    args = ap.parse_args()
    if args.dsn_file:
        dsn = Path(args.dsn_file).read_text(encoding="utf-8").strip()
        src_dsn = dsn
    else:
        from config import settings
        dsn, src_dsn = settings.analytics_db_url, settings.source_db_url
    print("Сверка v5.1.22 на реальных данных. Соединение только для чтения, запись НЕ производится.")
    from analytics import source as _src
    conn = await asyncpg.connect(dsn, server_settings=RO)
    _src._pool = await asyncpg.create_pool(src_dsn, min_size=1, max_size=3, command_timeout=600,
                                           server_settings=RO)
    run_all = not args.sections
    try:
        assert await conn.fetchval("SHOW default_transaction_read_only") == "on"
        for name, fn in (("p11", p11), ("diff30", diff30), ("migration", migration_preview),
                         ("p12", p12), ("p13", p13), ("header", header), ("p14", p14), ("p15", p15)):
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
