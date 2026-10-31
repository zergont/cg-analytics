# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""v5.1.31 «Близость по параметру» (docs/plans/design_v5123_plus.md, §2.4;
решения владельца В-26.1, В-26.2, В-26.3, В-30.3 от 31.10.2026). Без БД, без
шлюза и LLM.

1. Роль и ключ (В-26.1, В-26.2): роль из values.role, у строки без роли — по
   уставке (110 — масло, 103,9 — ОЖ, 172 и 241,3 — давление), по коду, по
   префиксу триггера; ключ «LIMIT_PROXIMITY|роль» одинаковый у детекции
   (_alert_key), строки (_episode_key, db.episode_key) и в SQL (_lp_role_sql);
   ключ без роли покрывает все роли (key_covers).
2. Допуск сравнения (В-26.3): 99,000712 — не нарушение, 100,00072 — нарушение;
   и в участках, и в детекторе без участков; compare_tol в обоих слоях KB.
3. Кластеры по ролям: две роли сразу — два кластера, две детекции, обе живые
   (взгляд назад по ключу), два ключа живых тревог.
4. Движок: два эпизода; переход на деплое — открытый эпизод со старым ключом
   продолжается как «LIMIT_PROXIMITY|роль» без пары «закрыт/открыт»; журнал
   тревог без роли — не «вечная» тревога (DISTINCT ON с ролью по уставке).
5. Счётчики по роли: count_episodes_batch (GROUP BY роли, ключ результата),
   обогащение детекций, счётчики гейта в manager.
6. Гейт (L1): снятая близость ОЖ не переносится на масло — в
   _gate_suppressed_scenarios, _live_new_episodes, _gate_carry, _carry_scope,
   _inherit_gate_suppression (и запись), db.set_episodes_gate_suppressed,
   apply_gate_suppression, manager (пометка, запись, отказанные); хеш состава с
   одной ролью прежний; запись до v5.1.31 без роли — все роли.
7. Сводка: вид на параметр «Приближение к уставке останова панели (Температура
   масла)», «ЗАМЕЧАНИЯ — 2» при двух ролях, «активен на конец» и подтверждение
   по своей роли, строка без роли — по уставке.
8. API (param_role), статус и промпт гейта.
9. NO_LOAD_TOO_LONG у «Работы» не через рез (В-30.3): удержанная нагрузка ниже
   порога — отрезок с начала окна; не ниже порога или нет — с первой строки;
   перенос через рез — как в v5.1.30; дыра связи и пауза пакетов рвут.

Запуск:  py -3 scripts/test_lp_roles_v5131.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import analytics.live as _lv  # noqa: E402
import online.db as odb  # noqa: E402
import online.engine as eng  # noqa: E402
import online.status_assembler as sa  # noqa: E402
from analytics import source as _src  # noqa: E402
from analytics.config import AnalyticsConfig  # noqa: E402
from analytics.contract import (  # noqa: E402
    CokingRisk, Detection, DerivedMetrics, RiskAccumulators, Segment, Subsegment,
)
from analytics.detectors import run_all_detectors  # noqa: E402
from analytics.live import (  # noqa: E402
    LIMIT_COMPARE_TOL, alive_detections, key_covers, limit_bad, limit_checks, limit_role, scenario_key,
    segment_spans,
)
from analytics.segmenter import segment  # noqa: E402
from analytics.serializer import apply_gate_suppression, build_summary_parts  # noqa: E402

_errors: list[str] = []
U = timezone.utc
MSK = ZoneInfo("Europe/Moscow")


def check(cond: bool, msg: str) -> None:
    if not cond:
        _errors.append(msg)


for _name in ("analytics", "online"):
    logging.getLogger(_name).addHandler(logging.NullHandler())
    logging.getLogger(_name).propagate = False

KB = ROOT / "knowledge_base"
CFG = AnalyticsConfig.from_pair(KB, "pcc3300", "cummins_kta50")     # ДГУ №1 / №2
CFG3 = AnalyticsConfig.from_pair(KB, "pcc3300", "cummins_qsx15")    # ДЭС №3
RPM, FREQ, HB, OILT = 40068, 40044, 40290, 40063
COOL3, PRESS3 = CFG3.role_to_addr("COOLANT_TEMP"), CFG3.role_to_addr("OIL_PRESS")
LOAD = CFG.role_to_addr("LOAD_PCT")
LP = "LIMIT_PROXIMITY"
LPO, LPC, LPP = "LIMIT_PROXIMITY|OIL_TEMP", "LIMIT_PROXIMITY|COOLANT_TEMP", "LIMIT_PROXIMITY|OIL_PRESS"


def T(h, m, s=0, d=7, mo=9):
    return datetime(2026, mo, d, h, m, s, tzinfo=U)


def S(t, sec):
    return t + timedelta(seconds=sec)


def h(addr, ts, v):
    return {"addr": addr, "ts": ts, "value": v}


def rs(v, a, b=None):
    return {"addr": 40011, "value": v, "state_start": a, "state_end": b}


def iso(t):
    return t.isoformat()


def pings(a, b, step=10, skip=None):
    out, t, k = [], a, 0
    while t < b:
        if not (skip and skip[0] < t < skip[1]):
            out.append(h(HB, t, float(k)))
        t += timedelta(seconds=step)
        k += 1
    return out


def by_addr(rows):
    out: dict[int, list[dict]] = {}
    for r in sorted(rows, key=lambda r: r["ts"]):
        out.setdefault(r["addr"], []).append(r)
    return out


L0 = T(6, 0)          # 07.09 09:00 МСК (перегрев ДЭС №3 04–16.09)
L1 = S(L0, 1800)
PL = pings(S(L0, -300), S(L1, 60))


def sp(rows, t0=L0, t1=L1, gaps=None, held=None, carry=False, cfg=CFG3, **k):
    return segment_spans(by_addr(rows), None, cfg, t0, t1, gaps or [], held or {}, carry, **k)


# ══ 1. Роль и ключ (В-26.1, В-26.2) ═══════════════════════════════════════════
check(limit_role({"role": "COOLANT_TEMP", "shutdown_limit": 110.0}) == "COOLANT_TEMP",
      "1а: values.role главнее уставки")
check([limit_role({"shutdown_limit": x}) for x in (110.0, 103.9, 241.3, 172.0, 172)]
      == ["OIL_TEMP", "COOLANT_TEMP", "OIL_PRESS", "OIL_PRESS", "OIL_PRESS"],
      "1а: роль по уставке — 110 масло, 103,9 ОЖ, 172 и 241,3 давление")
# Ревью v5.1.31 (C): как SQL — уставка только числом, роль только непустой строкой
check(limit_role({"shutdown_limit": "110.0"}) is None and limit_role({"role": 5, "shutdown_limit": 110.0})
      == "OIL_TEMP" and limit_role({"role": "", "shutdown_limit": 103.9}) == "COOLANT_TEMP",
      "1а: уставка строкой — не уставка; роль не строкой или пустая — по уставке (как в SQL)")
check(eng._episode_key({"scenario": LP, "trigger": "Температура ОЖ: 95", "open_values_json": {}}) == LP
      and eng._alert_key({"scenario": LP, "values": {}, "trigger": "Температура ОЖ: 95"}) == LPC
      and eng._alert_key({"scenario": LP, "values": {}, "trigger": "Температура ОЖ: 95"}, fallback=False) == LP
      and scenario_key({"scenario": LP, "ref_codes": [151]}) == LPC,
      "1а: запасные ветки (триггер, код) — не у строк БД и записей журнала; у тревоги статуса — да")
check(limit_role({"shutdown_limit": 120.0}) is None and limit_role({}) is None
      and limit_role({"shutdown_limit": True}) is None, "1а: чужая уставка, пусто — роли нет")
check(limit_role({}, codes=[151]) == "COOLANT_TEMP"
      and limit_role({}, trigger="Давление масла: 250.00 в пределах 10% от порога 241.3") == "OIL_PRESS",
      "1а: по справочному коду и по префиксу триггера")
_dO = {"scenario": LP, "values": {"role": "OIL_TEMP", "shutdown_limit": 110.0}}
_dC = {"scenario": LP, "values": {"role": "COOLANT_TEMP", "shutdown_limit": 103.9}}
check(eng._alert_key(_dO) == LPO and eng._alert_key(_dC) == LPC and scenario_key(_dO) == LPO,
      "1б: ключ детекции — «LIMIT_PROXIMITY|роль»")
check(eng._alert_key({"scenario": LP, "values": {"shutdown_limit": 103.9}}) == LPC
      and eng._alert_key({"scenario": LP, "values": {}}) == LP,
      "1б: детекция (запись журнала) без роли — по уставке; роли не определить — сценарий")
check(eng._episode_key({"scenario": LP, "open_values_json": json.dumps({"role": "OIL_TEMP"})}) == LPO
      and eng._episode_key({"scenario": LP, "open_values_json": {"shutdown_limit": 241.3}}) == LPP
      and eng._episode_key({"scenario": LP, "param_role": "COOLANT_TEMP", "open_values_json": None}) == LPC
      and eng._episode_key({"scenario": "RPM_UNDERSPEED", "open_values_json": {"role": "X"}}) == "RPM_UNDERSPEED"
      and eng._episode_key({"scenario": "CONTROLLER_FAULT", "addr": 1, "bit": 2}) == "CONTROLLER_FAULT|1|2",
      "1б: ключ строки — роль JSON (строкой и словарём), по уставке, синтетика param_role; прочие — как было")
check(odb.episode_key(LP, None, None, "OIL_TEMP") == LPO and odb.episode_key(LP, None, None) == LP
      and odb.episode_key("RPM_UNDERSPEED", None, None, "OIL_TEMP") == "RPM_UNDERSPEED"
      and odb.episode_key("CONTROLLER_FAULT", 40408, 11) == "CONTROLLER_FAULT|40408|11",
      "1б: db.episode_key — роль у близости, прочие как было")
check(key_covers(LP, LPO) and key_covers(LPO, LPO) and not key_covers(LPC, LPO) and not key_covers(LPO, LP)
      and not key_covers("RPM_UNDERSPEED", LPO) and key_covers("RPM_UNDERSPEED", "RPM_UNDERSPEED"),
      "1в: ключ без роли покрывает все роли, роль — только свою")
_sql = odb._lp_role_sql()
check(all(x in _sql for x in ("110.0", "103.9", "241.3", "172.0",
                              "jsonb_typeof(open_values_json->'role') = 'string' THEN "
                              "NULLIF(open_values_json->>'role', '')",
                              "jsonb_typeof(open_values_json->'shutdown_limit') = 'number'",
                              "scenario = 'LIMIT_PROXIMITY'"))
      and "values_json->>'role'" in odb._lp_role_sql("values_json"),
      f"1г: SQL роли — JSON, у старых строк по уставке (безопасное приведение): {_sql}")
check("scenario = ANY($4::text[])" in odb._key_match_sql("$4")
      and "(scenario || '|' || COALESCE(" in odb._key_match_sql("$4"),
      "1г: SQL подбора по ключам — сценарий целиком или ключ с ролью")

# ══ 2. Допуск сравнения (В-26.3) ══════════════════════════════════════════════
_chk = {c["role"]: c for c in limit_checks(CFG3)}
check(not limit_bad(_chk["OIL_TEMP"], 99.000712) and limit_bad(_chk["OIL_TEMP"], 100.00072)
      and not limit_bad(_chk["OIL_TEMP"], 99.0) and limit_bad(_chk["OIL_TEMP"], 99.02),
      "2а: масло — 99,000712 не нарушение, 100,00072 нарушение (порог 99 + 0,01)")
_np = _chk["OIL_PRESS"]["near"]      # у QSX15 (ДЭС №3) уставка 172 кПа → 189,2
check(not limit_bad(_chk["COOLANT_TEMP"], 93.51 + 0.005) and limit_bad(_chk["COOLANT_TEMP"], 93.53)
      and limit_bad(_chk["OIL_PRESS"], _np - 0.02) and not limit_bad(_chk["OIL_PRESS"], _np - 0.005)
      and abs(_np - 172.0 * 1.1) < 1e-9,
      "2а: ОЖ и давление — с тем же допуском (давление — «< порог − допуск»)")
check(all(c["tol"] == 0.01 for c in _chk.values()) and LIMIT_COMPARE_TOL == 0.01,
      "2а: допуск 0,01 из KB, умолчание 0,01")
for _p in ("_defaults/analytics/detectors.yaml", "equipment/cummins_kta50_pcc3300/analytics/detectors.yaml"):
    _txt = (KB / _p).read_text(encoding="utf-8")
    _blk = _txt[_txt.index("\nLIMIT_PROXIMITY:"):]
    _blk = _blk[:_blk.index("\n# ───", 1)]
    check("compare_tol: 0.01" in _blk and "В-26.3" in _blk, f"2г: compare_tol 0.01 в {_p}")


class _NoTol:
    """Конфиг без ключа compare_tol — умолчание кода."""

    def __init__(self, base):
        self._b = base

    def det(self, sc, key, default=None):
        if sc == LP and key == "compare_tol":
            return default
        return self._b.det(sc, key, default=default)

    def __getattr__(self, n):
        return getattr(self._b, n)


check(all(c["tol"] == LIMIT_COMPARE_TOL for c in limit_checks(_NoTol(CFG3))), "2г: нет ключа — умолчание 0,01")
_r99 = PL + [h(OILT, S(L0, 100), 99.000712), h(OILT, S(L0, 900), 97.0)]
_r100 = PL + [h(OILT, S(L0, 100), 100.00072), h(OILT, S(L0, 900), 97.0)]
check(LP not in sp(_r99), f"2б: участки — 99,000712 нарушения не дают: {sp(_r99).get(LP)}")
_c100 = sp(_r100).get(LP) or []
check(len(_c100) == 1 and _c100[0]["role"] == "OIL_TEMP" and _c100[0]["viol_sec"] == 800,
      f"2б: 100,00072 — участок 800 с: {_c100}")


def run_det(chars, a, b, live, cfg=CFG3):
    return [d for d in run_all_detectors(chars, DerivedMetrics(), RiskAccumulators(), [], "NORMAL", 3, a, b,
                                         None, cfg, live_spans=live) if d.scenario == LP]


_d99 = run_det({"OIL_TEMP": {"min": 95.0, "max": 99.000712, "median": 97.0}}, L0, L1, None)
_d100 = run_det({"OIL_TEMP": {"min": 95.0, "max": 100.00072, "median": 97.0}}, L0, L1, None)
check(not _d99 and len(_d100) == 1 and _d100[0].values["role"] == "OIL_TEMP"
      and _d100[0].values["shutdown_limit"] == 110.0 and "live" not in _d100[0].values,
      f"2в: детектор без участков — через limit_bad с допуском, роль в values: {[d.values for d in _d100]}")

# ══ 3. Кластеры, детекции и живой состав по ролям ═════════════════════════════
# Перегрев ДЭС №3 07.09: масло у уставки с +100 до +700, ОЖ — с +300 до +1500
_r2 = PL + [h(OILT, S(L0, 100), 100.5), h(OILT, S(L0, 700), 97.0),
            h(COOL3, S(L0, 0), 90.0), h(COOL3, S(L0, 300), 95.0), h(COOL3, S(L0, 1500), 92.0)]
_c2 = sp(_r2).get(LP) or []
check(len(_c2) == 2 and [c["role"] for c in _c2] == ["OIL_TEMP", "COOLANT_TEMP"]
      and [c["start"] for c in _c2] == [iso(S(L0, 100)), iso(S(L0, 300))]
      and [c["viol_sec"] for c in _c2] == [600, 1200]
      and [set(c["roles"]) for c in _c2] == [{"OIL_TEMP"}, {"COOLANT_TEMP"}],
      f"3а: две роли сразу — по кластеру на роль (до v5.1.31 — один на обе): {_c2}")
_live2 = sp(_r2)
_d2 = run_det({}, S(L0, 600), S(L0, 1200), _live2)
_by = {d.values["role"]: d for d in _d2}
check(set(_by) == {"OIL_TEMP", "COOLANT_TEMP"} and _by["OIL_TEMP"].t_detected == iso(S(L0, 100))
      and _by["COOLANT_TEMP"].t_detected == iso(S(L0, 300))
      and _by["COOLANT_TEMP"].values["live"]["onset"] == iso(S(L0, 300))
      and all(len(d.values["live"]["clusters"]) == 1 for d in _d2),
      f"3б: две детекции, начало — кластер своей роли: {[(d.values['role'], d.t_detected) for d in _d2]}")


def DET(role, start, end, ongoing=False, extra=None):
    lim = {"OIL_TEMP": 110.0, "COOLANT_TEMP": 103.9, "OIL_PRESS": 241.3}[role]
    v = {"value": 100.5, "shutdown_limit": lim, "proximity_pct": 10.0, "role": role,
         "live": {"clusters": [{"start": iso(start), "end": iso(end), "ongoing": ongoing, "role": role,
                                "roles": {role: {"viol_sec": (end - start).total_seconds()}},
                                "viol_sec": (end - start).total_seconds()}],
                  "onset": iso(start), "first": iso(start), "until": iso(end), "ongoing": ongoing}}
    v.update(extra or {})
    lab = _lv.LIMIT_LABELS[role]
    return Detection(scenario=LP, severity="CAUTION", t_detected=iso(start), source="PASSPORT_THRESHOLD",
                     trigger=f"{lab}: 100.50 в пределах 10% от порога {lim:.1f}", related_roles=[role],
                     fault_codes=[], description_key=f"LIMIT_PROXIMITY.{role.lower()}_near_limit", values=v,
                     ref_codes=[{"OIL_TEMP": 214, "COOLANT_TEMP": 151, "OIL_PRESS": 415}[role]])


def SUB(a, b, dets, gaps=()):
    return Subsegment(id=f"s.{a}", parent_segment_id="s", t_start=S(L0, a).isoformat(),
                      t_end=S(L0, b).isoformat(), duration_sec=b - a, load_zone="NORMAL",
                      cause_open="ZONE_CHANGE", cause_close=None, characteristics={},
                      derived_metrics=DerivedMetrics(), risk_accumulators=RiskAccumulators(),
                      detections=dets, data_quality=1.0,
                      data_gaps=[{"start": S(L0, x).isoformat(), "end": S(L0, y).isoformat(),
                                  "duration_sec": y - x} for x, y in gaps])


def SEG(subs, rs_=3):
    return Segment(id="s", router_sn="R", equip_type="pcc", panel_id=1, engine_sn="", run_state=rs_,
                   run_state_label="Работа", t_start=subs[0].t_start, t_end=subs[-1].t_end,
                   duration_sec=0, engine_hours_start=None, cause_open="REPORT_START", cause_close=None,
                   preamble_included=False, data_quality=1.0, subsegments=subs, sequence_checks=[], events=[])


# ОЖ живая в прежнем подсегменте (смена зоны), масло — в последнем: до v5.1.31 «уже
# найден» по сценарию прятал ОЖ (перегрев 04–16.09: близость ОЖ скрыта близостью масла)
_seg2 = SEG([SUB(0, 600, [DET("COOLANT_TEMP", S(L0, 300), S(L0, 1500), True)]),
             SUB(600, 1500, [DET("OIL_TEMP", S(L0, 1000), S(L0, 1500), True)])])
_al = alive_detections(_seg2.subsegments)
check(sorted(scenario_key(d) for d in _al) == [LPC, LPO],
      f"3в: живой состав — обе роли (взгляд назад по ключу): {[scenario_key(d) for d in _al]}")
_, _act = eng._extract_open_segment_data(_seg2)
check(sorted(eng._alert_key(d) for d in _act) == [LPC, LPO] and eng._live_alert_keys(_seg2) == {LPC, LPO},
      f"3в: живые тревоги движка — два ключа: {[eng._alert_key(d) for d in _act]}")
# Через segment(): две роли — детекции обеих в рабочих подсегментах, отдельные кластеры
_rows_seg = list(PL)
_t, _k = S(L0, -300), 0
while _t < L1:
    _rows_seg += [h(RPM, _t, 1500.0 + _k % 2), h(FREQ, _t, 50.0)]
    _t, _k = S(_t, 10), _k + 1
_rows_seg += [h(OILT, S(L0, 100), 100.5), h(OILT, S(L0, 700), 97.0),
              h(COOL3, S(L0, 0), 90.0), h(COOL3, S(L0, 300), 95.0), h(COOL3, S(L0, 1500), 92.0)]
_sg = segment([rs(3, T(0, 0))], sorted(_rows_seg, key=lambda r: r["ts"]), [], [], CFG3, "SN", "pcc", 1, "E",
              L0, L1)
_seg_dets = [d for s in _sg for sub in s.subsegments for d in sub.detections if d.scenario == LP]
check({d.values.get("role") for d in _seg_dets} == {"OIL_TEMP", "COOLANT_TEMP"}
      and {eng._alert_key(d.to_dict()) for d in _seg_dets} == {LPO, LPC},
      f"3г: segment() — детекции обеих ролей: {[(d.values.get('role'), d.t_detected) for d in _seg_dets]}")
_cl_sg = {k: s for k, s in eng._segment_clusters(_sg[-1]) if k.startswith(LP)}
check(sorted(_cl_sg) == [LPC, LPO]
      and {k: iso(s) for k, s in _cl_sg.items()} == {LPO: iso(S(L0, 100)), LPC: iso(S(L0, 300))},
      f"3г: кластеры сегмента для посева — по ключу роли: {sorted(_cl_sg)}")

# ══ 4. Движок: два эпизода, переход на деплое, журнал тревог ══════════════════


class FakeDB:
    """Подмена online.db: запись вызовов, без БД."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.next_id = 700
        self.open_rows: list[dict] = []
        self.alerts: list[dict] = []

    async def open_episode(self, *a, **k):
        self.next_id += 1
        self.calls.append(("open", self.next_id, k))
        return self.next_id

    async def find_episode_by_onset(self, *a, **k):
        self.calls.append(("find", k.get("scenario"), k.get("role")))
        return None

    async def reopen_episode(self, ep_id, **k):
        self.calls.append(("reopen", ep_id, k))

    async def update_episode(self, ep_id, **k):
        self.calls.append(("update", ep_id, k))

    async def close_episode(self, ep_id, t_close, reason, **k):
        self.calls.append(("close", ep_id, t_close, reason, k))

    async def get_open_segment(self, *a, **k):
        return None

    async def set_episodes_gate_suppressed(self, *a, **k):
        self.calls.append(("mark", a, k))
        return []

    async def get_open_episodes(self, *a, **k):
        return list(self.open_rows)

    async def get_active_alerts(self, *a, **k):
        return list(self.alerts)

    def of(self, kind):
        return [c for c in self.calls if c[0] == kind]


_DB_NAMES = ("open_episode", "find_episode_by_onset", "reopen_episode", "update_episode", "close_episode",
             "get_open_segment", "set_episodes_gate_suppressed", "get_open_episodes", "get_active_alerts")
_orig_db = {n: getattr(odb, n) for n in _DB_NAMES}
_orig_gd = _src.get_data_gaps


async def _noop(*a, **k):
    return None


async def _gd_empty(*a, **k):
    return []


def mk_engine(db):
    for n in _DB_NAMES:
        setattr(eng.online_db, n, getattr(db, n))
    e = eng.OnlinePollEngine.__new__(eng.OnlinePollEngine)
    e.cfg = CFG3
    e.router_sn, e.equip_type, e.panel_id = "SN", "pcc", 1
    e._episodes, e._episode_accrual_ts, e._last_open_seg_id = {}, None, 5
    e._supp_recheck, e._prev_seg_hint, e.cursor_ts = None, None, None
    e._supp_refused, e._supp_refused_sig = {}, None
    e._active_alerts = {}
    e._attach_trip_context = _noop
    return e


def cyc(e, dets, now, gaps=(), gf=L0, rs_=3, ca=None):
    if e._episode_accrual_ts is None:
        e._episode_accrual_ts = S(now, -30)
    asyncio.run(e._process_episodes([d if isinstance(d, dict) else d.to_dict() for d in dets], now,
                                    list(gaps), cleared_at=ca, gaps_from=gf, run_state=rs_))


_src.get_data_gaps = _gd_empty
try:
    # ── 4а. Две роли одновременно — два эпизода, онсет ищется своей роли ──
    db = FakeDB()
    e = mk_engine(db)
    _now = S(L0, 1200)
    cyc(e, [DET("OIL_TEMP", S(L0, 100), _now, True), DET("COOLANT_TEMP", S(L0, 300), _now, True)], _now)
    _op = db.of("open")
    check(len(_op) == 2 and sorted(e._episodes) == [LPC, LPO]
          and {o[2]["open_values"]["role"]: o[2]["t_open"] for o in _op}
          == {"OIL_TEMP": S(L0, 100), "COOLANT_TEMP": S(L0, 300)}
          and sorted(f[2] for f in db.of("find")) == ["COOLANT_TEMP", "OIL_TEMP"],
          f"4а: два эпизода по ролям (до v5.1.31 — один): {db.calls}")
    # Следующий цикл: ОЖ кончилась, масло идёт — эпизод масла продолжается, ОЖ — в дебаунсе
    db.calls.clear()
    _now2 = S(_now, 30)
    cyc(e, [DET("OIL_TEMP", S(L0, 100), _now2, True)], _now2)
    check(not db.of("open") and not db.of("close") and [u[1] for u in db.of("update")] == [e._episodes[LPO]["id"]]
          and e._episodes[LPC]["miss"] == 1,
          f"4а: масло продолжается, ОЖ — промах своего эпизода: {db.calls}")

    # ── 4б. Переход на деплое: открытый эпизод со старым ключом «LIMIT_PROXIMITY» ──
    for _tag, _ov in (("роль в JSON", {"role": "OIL_TEMP", "shutdown_limit": 110.0}),
                      ("без роли, уставка 110", {"shutdown_limit": 110.0})):
        db = FakeDB()
        db.open_rows = [{"id": 14990, "scenario": LP, "addr": None, "bit": None, "severity": "CAUTION",
                         "t_open": S(L0, 50),
                         "open_values_json": json.dumps(dict(_ov, live={"until": iso(S(L0, 1170))}))}]
        db.alerts = [{"scenario": LP, "severity": "CAUTION", "trigger": "Температура масла: 100.50",
                      "values": dict(_ov), "segment_id": None}]
        e = mk_engine(db)
        asyncio.run(e._restore_live_state())
        check(list(e._episodes) == [LPO] and e._episodes[LPO]["id"] == 14990 and e._episodes[LPO]["exact"]
              and list(e._active_alerts) == [LPO],
              f"4б ({_tag}): восстановлен под ключом с ролью: {e._episodes}, {list(e._active_alerts)}")
        e._episode_accrual_ts = S(L0, 1170)
        # Кластер масла по новой нарезке начался позже прежнего (объединённого) начала
        cyc(e, [DET("OIL_TEMP", S(L0, 100), S(L0, 1200), True)], S(L0, 1200))
        check(not db.of("open") and not db.of("close") and [u[1] for u in db.of("update")] == [14990],
              f"4б ({_tag}): продолжается та же строка — без пары «закрыт/открыт»: {db.calls}")
        _new, _journal = eng._diff_alerts(e._active_alerts, [DET("OIL_TEMP", S(L0, 100), S(L0, 1200),
                                                                   True).to_dict()], S(L0, 1200), None)
        check(not _journal, f"4б ({_tag}): журнал тревог — без OPENED/CLOSED на переходе: {_journal}")

    # ── 4в. Журнал без роли не даёт «вечной» тревоги ──
    db = FakeDB()
    db.alerts = [{"scenario": LP, "severity": "CAUTION", "trigger": None,
                  "values": {"value": 100.5, "shutdown_limit": 110.0}, "segment_id": None}]
    e = mk_engine(db)
    asyncio.run(e._restore_live_state())
    _new, _journal = eng._diff_alerts(e._active_alerts, [], S(L0, 600), None)
    check(list(e._active_alerts) == [LPO] and [(j["event_type"], j["scenario"]) for j in _journal]
          == [("CLOSED", LP)] and _journal[0]["values"].get("shutdown_limit") == 110.0,
          f"4в: запись без роли — ключ по уставке, закрывается своей записью CLOSED: {_journal}")
finally:
    for _n, _f in _orig_db.items():
        setattr(eng.online_db, _n, _f)
    _src.get_data_gaps = _orig_gd


class Conn:
    """Подмена соединения asyncpg: запись SQL и параметров, ответ — заданные строки."""

    def __init__(self, rows=None):
        self.rows, self.sql, self.args = rows or [], [], []

    async def fetch(self, sql, *a):
        self.sql.append(sql)
        self.args.append(a)
        return self.rows

    async def fetchrow(self, sql, *a):
        self.sql.append(sql)
        self.args.append(a)
        return self.rows[0] if self.rows else None

    async def execute(self, sql, *a):
        self.sql.append(sql)
        self.args.append(a)
        return "UPDATE 0"

    async def close(self):
        pass


_orig_connect = odb._connect


def use(conn):
    async def _c():
        return conn
    odb._connect = _c


try:
    # ── 4г. get_active_alerts: DISTINCT ON с ролью (у записи без роли — по уставке) ──
    c = Conn(rows=[])
    use(c)
    asyncio.run(odb.get_active_alerts("SN", "pcc", 1))
    _q = " ".join(c.sql[0].split())
    _re = " ".join(odb._lp_role_sql("values_json").split())
    check(_q.count(_re) == 2 and _q.index("DISTINCT ON") < _q.index(_re) < _q.index("FROM j")
          and _q.rindex(_re) > _q.index("ORDER BY"),
          f"4г: DISTINCT ON и ORDER BY — с ролью записи журнала: {_q[:400]}")
    # Ревью v5.1.31 (B): записи близости без роли в JSON (кроме CLOSED) раньше последнего
    # CLOSED близости машины не берутся — пара разных ролей до v5.1.31 фантома не даёт
    _has = " ".join(odb._lp_has_role_sql("values_json").split())
    check("max(ts) FILTER (WHERE scenario = 'LIMIT_PROXIMITY' AND event_type = 'CLOSED') OVER () AS lp_closed"
          in _q and f"WHERE NOT (scenario = 'LIMIT_PROXIMITY' AND event_type <> 'CLOSED' AND NOT {_has} "
          "AND lp_closed IS NOT NULL AND ts < lp_closed)" in _q,
          f"4г: журнал — старые записи близости без роли отсекаются последним CLOSED: {_q}")
    # Семантика DISTINCT ON той же ролью: OPENED без роли (уставка 110) и позже CLOSED с ролью —
    # одна группа, последнее событие CLOSED → активных нет. По одной values->>'role' было бы
    # две группы, OPENED без роли висел бы вечно
    _jr = [{"event_type": "OPENED", "ts": S(L0, 0), "values": {"shutdown_limit": 110.0}},
           {"event_type": "CLOSED", "ts": S(L0, 600), "values": {"role": "OIL_TEMP", "shutdown_limit": 110.0}}]

    def _distinct(rows, key):
        last: dict = {}
        for r in sorted(rows, key=lambda r: r["ts"]):
            last[key(r)] = r
        return [r for r in last.values() if r["event_type"] != "CLOSED"]

    check(_distinct(_jr, lambda r: limit_role(r["values"])) == []
          and len(_distinct(_jr, lambda r: r["values"].get("role"))) == 1,
          "4г: та же роль по уставке — без вечной тревоги (по одной роли JSON была бы)")

    def _active(rows):
        """Эмуляция get_active_alerts v5.1.31: отсев старых записей без роли, DISTINCT ON по роли."""
        lpc = max((r["ts"] for r in rows if r["event_type"] == "CLOSED"), default=None)
        keep = [r for r in rows if not (r["event_type"] != "CLOSED" and not r["values"].get("role")
                                        and lpc is not None and r["ts"] < lpc)]
        return _distinct(keep, lambda r: limit_role(r["values"]))

    # ДГУ №1 06.07: OPENED с уставкой 103,9 (ОЖ), CLOSED той же тревоги с уставкой 110 (масло)
    _pair = [{"event_type": "OPENED", "ts": S(L0, 0), "values": {"shutdown_limit": 103.9}},
             {"event_type": "CLOSED", "ts": S(L0, 520), "values": {"shutdown_limit": 110.0}}]
    check(len(_distinct(_pair, lambda r: limit_role(r["values"]))) == 1 and _active(_pair) == [],
          "4г: пара разных ролей до v5.1.31 — по одной группировке по роли фантом ОЖ, с отсевом — нет")
    _live_dep = [{"event_type": "CLOSED", "ts": S(L0, 0), "values": {"role": "OIL_TEMP", "shutdown_limit": 110.0}},
                 {"event_type": "OPENED", "ts": S(L0, 600), "values": {"shutdown_limit": 110.0}}]
    check(len(_active(_live_dep)) == 1 and _active(_jr) == [],
          "4г: запись без роли позже всех CLOSED (живая на выкладке) — висит; OPENED без роли → CLOSED с ролью — нет")

    # ── 4д. existing_episode_onsets / find_episode_by_onset — ключ и отбор по роли ──
    c = Conn(rows=[{"scenario": LP, "addr": None, "bit": None, "t_open": S(L0, 100), "lp_role": "OIL_TEMP"},
                   {"scenario": LP, "addr": None, "bit": None, "t_open": S(L0, 300), "lp_role": "COOLANT_TEMP"},
                   {"scenario": "RPM_UNDERSPEED", "addr": None, "bit": None, "t_open": S(L0, 5), "lp_role": None}])
    use(c)
    _on = asyncio.run(odb.existing_episode_onsets("SN", "pcc", 1, [S(L0, 100)]))
    check(_on == {(LPO, S(L0, 100)), (LPC, S(L0, 300)), ("RPM_UNDERSPEED", S(L0, 5))}
          and "AS lp_role" in c.sql[0], f"4д: онсеты — ключи с ролью: {_on}")
    c = Conn(rows=[])
    use(c)
    asyncio.run(odb.find_episode_by_onset("SN", "pcc", 1, scenario=LP, addr=None, bit=None,
                                          t_open=S(L0, 100), role="COOLANT_TEMP"))
    check(c.args[0][-1] == "COOLANT_TEMP", f"4д: find_episode_by_onset — параметр роли: {c.args}")
    check("$8::text IS NULL OR" in c.sql[0] and odb._lp_role_sql() in c.sql[0],
          "4д: отбор по роли строки (JSON или уставка), без роли — как раньше")

    # ── 5а. count_episodes_batch: GROUP BY роли, ключ результата — с ролью ──
    _cnt = {"count_window": 3, "dur_window": 600.0, "blind_window": 0.0, "count_since": 1, "dur_since": 60.0}
    c = Conn(rows=[dict(_cnt, scenario=LP, addr=None, bit=None, lp_role="OIL_TEMP"),
                   dict(_cnt, scenario=LP, addr=None, bit=None, lp_role="COOLANT_TEMP", count_window=1),
                   dict(_cnt, scenario="OIL_DILUTION", addr=None, bit=None, lp_role=None)])
    use(c)
    _res = asyncio.run(odb.count_episodes_batch("SN", "pcc", 1, [LP, "OIL_DILUTION"], 30, None))
    _q = " ".join(c.sql[0].split())
    check(set(_res) == {LPO, LPC, "OIL_DILUTION"} and _res[LPC]["count_window"] == 1
          and _res[LPO]["count_window"] == 3
          and "GROUP BY scenario, addr, bit, lp_role, t_open" in _q
          and "GROUP BY scenario, addr, bit, lp_role" in _q and c.args[0][3] == [LP, "OIL_DILUTION"],
          f"5а: счётчики по роли, WHERE — по сценарию: {_res}")

    # ── 6е. db.set_episodes_gate_suppressed / clear_episodes_gate_suppressed — ключи с ролью ──
    c = Conn(rows=[])
    use(c)
    asyncio.run(odb.set_episodes_gate_suppressed("SN", "pcc", 1, [LPC, "NO_LOAD_TOO_LONG"], ids=[5]))
    _q = " ".join(c.sql[0].split())
    check(c.args[0][3] == [LPC] and odb._key_match_sql("$4") in c.sql[0] and "t_close IS NULL" in _q,
          f"6е: пометка — ключ роли ОЖ (режимные замечания отсечены по сценарию ключа): {c.args}")
    c = Conn(rows=[])
    use(c)
    asyncio.run(odb.clear_episodes_gate_suppressed("SN", "pcc", 1, ["OIL_DILUTION"]))
    check(odb._key_match_sql("$4") in c.sql[0], "6е: снятие пометки — тем же подбором по ключам")
finally:
    odb._connect = _orig_connect

# ── 5б. Обогащение детекций открытого сегмента — счётчики своей роли ──
_cnt_calls: list = []


async def _cnt_fake(*a, **k):
    _cnt_calls.append(a[3])
    return {LPO: {"count_window": 7, "dur_window": 3600.0, "blind_window": 0.0, "count_since": 2,
                  "dur_since": 600.0},
            LPC: {"count_window": 1, "dur_window": 120.0, "blind_window": 0.0, "count_since": 1,
                  "dur_since": 120.0}}


_orig_cnt = odb.count_episodes_batch
odb.count_episodes_batch = _cnt_fake
try:
    _seg5 = SEG([SUB(0, 1500, [DET("OIL_TEMP", S(L0, 100), S(L0, 1500), True),
                               DET("COOLANT_TEMP", S(L0, 300), S(L0, 1500), True)])])
    asyncio.run(eng._enrich_open_seg_detections(_seg5, "SN", "pcc", 1, CFG3, L0))
    _v5 = {d.values["role"]: d.values for d in _seg5.subsegments[0].detections}
    check(_cnt_calls == [[LP]] and _v5["OIL_TEMP"]["history_count_30d"] == 7
          and _v5["COOLANT_TEMP"]["history_count_30d"] == 1 and _v5["COOLANT_TEMP"]["startup_count"] == 1,
          f"5б: у каждой роли свои «за 30 дней» и «с пуска»: {_cnt_calls}, {_v5}")
finally:
    odb.count_episodes_batch = _orig_cnt

# ══ 6. Гейт (L1): снятая близость ОЖ не переносится на масло ══════════════════
_dOd, _dCd = DET("OIL_TEMP", S(L0, 100), S(L0, 1500), True).to_dict(), \
    DET("COOLANT_TEMP", S(L0, 300), S(L0, 1500), True).to_dict()
H_C = sa.compute_analytics_hash([_dCd])
H_OC = sa.compute_analytics_hash([_dOd, _dCd])
# Хеш состава с одной ролью — прежний: (сценарий, справочные коды), роль в нём не участвует
check(H_C == sa.compute_analytics_hash([{"scenario": LP, "ref_codes": [151]}]) and H_C != H_OC
      and sa.compute_analytics_hash([_dOd]) != H_C,
      "6: хеш состава с одной ролью не изменился (действующие снятия в силе); роли различаются кодом")

# ── 6а. _gate_suppressed_scenarios — ключи с ролью ──
_own = [{"ts": iso(S(L0, 400)), "kind": "llm", "decision": "cancel", "decision_applied": True,
         "suppressed_hash": H_C, "fault_hash": "fC", "episodes": [812]}]
_segC = SEG([SUB(0, 1500, [DET("COOLANT_TEMP", S(L0, 300), S(L0, 1500), True)])])
_gs = {"suppressed_hash": H_C, "gate_log": _own}
_row6 = {"id": 9, "t_start": L0, "gate_suppressed_hash": H_C, "gate_log": json.dumps(_own),
         "characteristics_json": {}}
check(eng._gate_suppressed_scenarios(_gs, _segC, _row6) == {LPC},
      f"6а: снятый состав сводки — ключ ОЖ: {eng._gate_suppressed_scenarios(_gs, _segC, _row6)}")
_segOC = SEG([SUB(0, 1500, [DET("OIL_TEMP", S(L0, 100), S(L0, 1500), True),
                            DET("COOLANT_TEMP", S(L0, 300), S(L0, 1500), True)])])
check(eng._gate_suppressed_scenarios(_gs, _segOC, _row6) == set(),
      "6а: к ОЖ добавилось масло — состав другой, снятия в сводке нет")

# ── 6б. _live_new_episodes — пометка только своей роли ──
_syn = eng._live_new_episodes([_segOC], [], {LPO, LPC}, S(L0, 1500), {LPC},
                              {"ts": iso(S(L0, 400)), "segment_id": None, "kind": "inherited"})
_sb = {s["param_role"]: s for s in _syn}
check(set(_sb) == {"OIL_TEMP", "COOLANT_TEMP"} and _sb["COOLANT_TEMP"]["gate_suppressed"] is True
      and _sb["OIL_TEMP"]["gate_suppressed"] is False and _sb["OIL_TEMP"]["gate_suppressed_src"] is None
      and eng._episode_key(_sb["OIL_TEMP"]) == LPO and eng._episode_key(_sb["COOLANT_TEMP"]) == LPC
      and _sb["OIL_TEMP"]["t_open"] == S(L0, 100),
      f"6б: синтетика — снятой помечена только ОЖ, у каждой своя роль: {_syn}")
# Открытая строка без роли (до v5.1.31) покрывает все роли — синтетика не нужна
check(not eng._live_new_episodes([_segOC], [{"scenario": LP, "t_open": S(L0, 50), "t_close": None,
                                             "open_values_json": {}}], {LPO, LPC}, S(L0, 1500)),
      "6б: открытая строка без роли — синтетики нет (как до v5.1.31)")

# ── 6в. _gate_carry — synthetic ключами, состав на конце (журнал до v5.1.22) по ключу ──
_eps6 = {LPO: {"id": 811, "t_open": S(L0, 100)}, LPC: {"id": 812, "t_open": S(L0, 300)}}
_closed6 = [(_segC, 35990, L0, S(L0, 1500))]
_cr = eng._gate_carry(_gs, _closed6, _eps6, {LPO, LPC}, {LPO, LPC}, 3, S(L0, 1500),
                      dets_end=[_dCd], supp_sc={LPC})
check(_cr and _cr["record"]["synthetic"] == [LPC], f"6в: synthetic — ключ ОЖ: {_cr and _cr['record']}")
_gs_old = {"suppressed_hash": H_C, "gate_log": [dict(_own[0], episodes=None)]}
for _e in _gs_old["gate_log"]:
    _e.pop("episodes")
_cr2 = eng._gate_carry(_gs_old, _closed6, _eps6, {LPO, LPC}, {LPO, LPC}, 3, S(L0, 1500), dets_end=[_dCd])
check(_cr2 and _cr2["ids"] == [812], f"6в: журнал до v5.1.22 — снятые по ключу состава на конце (ОЖ): {_cr2}")

# ── 6г. _carry_scope — ключи; запись до v5.1.31 без роли — все роли ──
_carry_rec = {"ts": iso(S(L0, 1800)), "kind": "carry", "decision": "cancel", "decision_applied": True,
              "suppressed_hash": H_OC, "episodes": [], "synthetic": [LPC]}
_late_rec = {"ts": iso(S(L0, 1801)), "kind": "late", "decision": "cancel", "decision_applied": True,
             "suppressed_hash": H_OC, "episodes": [], "source": {"ts": iso(S(L0, 1700))},
             "alarms": [{"scenario": LP, "param_role": "COOLANT_TEMP"}]}
_ids, _synth, _late = eng._carry_scope([_carry_rec, _late_rec], H_OC)
check(_synth == {LPC} and set(_late) == {LPC}, f"6г: перенос и поздний вердикт — ключ ОЖ: {_synth}, {_late}")
_ids0, _synth0, _late0 = eng._carry_scope([dict(_carry_rec, synthetic=[LP]),
                                           dict(_late_rec, alarms=[{"scenario": LP}])], H_OC)
check(_synth0 == {LP} and set(_late0) == {LP}, f"6г: запись до v5.1.31 — сценарием: {_synth0}, {_late0}")


# ── 6д. _inherit_gate_suppression — перенос, своё решение, запись ──
class GateDB:
    def __init__(self, row):
        self.row, self.calls = row, []

    async def get_open_segment(self, *a, **k):
        return self.row

    async def set_episodes_gate_suppressed(self, r, e, p, scenarios, ids=None, src=None, **k):
        self.calls.append(("mark", list(scenarios), list(ids or []), dict(src or {})))
        return list(ids or [])

    async def append_segment_gate_event(self, *a, event=None, **k):
        self.calls.append(("event", event))
        return True

    async def clear_open_gate_suppression_hash(self, r, e, p, hh):
        self.calls.append(("clear_hash", hh))
        return True

    def of(self, kind):
        return [c for c in self.calls if c[0] == kind]


_G_NAMES = ("get_open_segment", "set_episodes_gate_suppressed", "append_segment_gate_event",
            "clear_open_gate_suppression_hash")
_orig_g = {n: getattr(odb, n) for n in _G_NAMES}


def gate_engine(gdb, mem):
    for n in _G_NAMES:
        setattr(eng.online_db, n, getattr(gdb, n))
    e = eng.OnlinePollEngine.__new__(eng.OnlinePollEngine)
    e.cfg = CFG3
    e.router_sn, e.equip_type, e.panel_id = "SN", "pcc", 1
    e._episodes, e.cursor_ts = mem, S(L0, 1800)
    e._supp_refused, e._supp_refused_sig = {}, None
    return e


R6 = S(L0, 1800)      # начало новой строки (суточный рез)
try:
    # Перенос: закрытая карточка сняла только ОЖ (synthetic), обе роли продолжаются через рез
    for _tag, _log in (("synthetic", [_carry_rec]), ("поздний вердикт", [dict(_late_rec, ts=iso(R6))])):
        gdb = GateDB({"id": 36000, "t_start": R6, "gate_suppressed_hash": H_OC, "gate_log": json.dumps(_log),
                      "characteristics_json": {}})
        e6 = gate_engine(gdb, {LPO: {"id": 821}, LPC: {"id": 822}})
        asyncio.run(e6._inherit_gate_suppression([(LPO, 821, S(L0, 100)), (LPC, 822, S(L0, 300))],
                                                 [_dOd, _dCd], ts=S(R6, 30)))
        _mk = gdb.of("mark")
        check(len(_mk) == 1 and _mk[0][1] == [LPC] and _mk[0][2] == [822] and _mk[0][3]["kind"] == "carry"
              and gdb.of("clear_hash") == [("clear_hash", H_OC)],
              f"6д ({_tag}): перенос — только ОЖ; масло не наследует, хеш снят (разберёт гейт): {gdb.calls}")
        _ev = gdb.of("event")
        check(_ev and _ev[0][1]["alarms"] == [{"scenario": LP, "param_role": "COOLANT_TEMP"}]
              and _ev[0][1]["episodes"] == [822],
              f"6д ({_tag}): запись carry — сценарий и роль ОЖ: {_ev}")
    # Запись до v5.1.31 (сценарий без роли) — все роли, как раньше
    gdb = GateDB({"id": 36000, "t_start": R6, "gate_suppressed_hash": H_OC,
                  "gate_log": json.dumps([dict(_carry_rec, synthetic=[LP])]), "characteristics_json": {}})
    e6 = gate_engine(gdb, {LPO: {"id": 821}, LPC: {"id": 822}})
    asyncio.run(e6._inherit_gate_suppression([(LPO, 821, S(L0, 100)), (LPC, 822, S(L0, 300))],
                                             [_dOd, _dCd], ts=S(R6, 30)))
    check(gdb.of("mark") and sorted(gdb.of("mark")[0][2]) == [821, 822] and not gdb.of("clear_hash"),
          f"6д: запись переноса до v5.1.31 без роли — обе роли (прежнее поведение): {gdb.calls}")
    # Своё решение строки по составу {ОЖ}: в составе масло и ОЖ — хеш другой, снятия нет
    gdb = GateDB({"id": 36001, "t_start": L0, "gate_suppressed_hash": H_C, "gate_log": json.dumps(_own),
                  "characteristics_json": {}})
    e6 = gate_engine(gdb, {LPO: {"id": 831}})
    asyncio.run(e6._inherit_gate_suppression([(LPO, 831, S(L0, 1000))], [_dOd, _dCd], ts=S(L0, 1000)))
    check(not gdb.of("mark") and not gdb.of("event"),
          f"6д: снятие ОЖ не наследуется новым эпизодом масла (состав сменился): {gdb.calls}")
    # Тот же состав {ОЖ}, новый эпизод ОЖ — наследует, запись с ролью
    e6 = gate_engine(gdb, {LPC: {"id": 832}})
    asyncio.run(e6._inherit_gate_suppression([(LPC, 832, S(L0, 1000))], [_dCd], ts=S(L0, 1000)))
    _mk = gdb.of("mark")
    check(len(_mk) == 1 and _mk[0][1] == [LPC] and _mk[0][2] == [832] and _mk[0][3]["kind"] == "inherited"
          and gdb.of("event")[0][1]["alarms"] == [{"scenario": LP, "param_role": "COOLANT_TEMP"}],
          f"6д: тот же состав — новый эпизод ОЖ наследует снятие, запись с ролью: {gdb.calls}")
finally:
    for _n, _f in _orig_g.items():
        setattr(eng.online_db, _n, _f)

# ── 6ж. apply_gate_suppression — поздний вердикт снимает только свою роль ──
EPS7 = [{"source": "analytics", "scenario": LP, "severity": "CAUTION", "gate_suppressed": False,
         "t_open": S(L0, 100), "t_close": None, "active_sec": 1400.0, "blind_sec": 0.0, "id": 901,
         "open_values_json": json.dumps({"role": "OIL_TEMP", "shutdown_limit": 110.0})},
        {"source": "analytics", "scenario": LP, "severity": "CAUTION", "gate_suppressed": False,
         "t_open": S(L0, 300), "t_close": None, "active_sec": 1200.0, "blind_sec": 0.0, "id": 902,
         "open_values_json": {"shutdown_limit": 103.9}}]
_p7 = build_summary_parts([_segOC], episodes=EPS7, tz=MSK, t_from=L0, t_to=S(L0, 1500))
_g7 = {g.get("param_role"): g for g in _p7["remarks"]}
_late7 = apply_gate_suppression(_p7, [LPC], until=S(L0, 1500), src={"ts": iso(S(L0, 1500))})
_lg7 = {g.get("param_role"): g for g in _late7["remarks"]}
check(_lg7["COOLANT_TEMP"]["suppressed"] == 1 and _lg7["OIL_TEMP"]["suppressed"] == 0
      and _late7["verdict"]["remarks_n"] == 1,
      f"6ж: поздний вердикт по ОЖ — масло остаётся замечанием: {[(g['name'], g['suppressed']) for g in _late7['remarks']]}")
_late7b = apply_gate_suppression(_p7, [LP], until=S(L0, 1500))
check(all(g["suppressed"] == 1 for g in _late7b["remarks"]), "6ж: сценарий без роли — все роли (как раньше)")
_old_parts = json.loads(json.dumps(_p7))
for _g in _old_parts["remarks"]:
    _g.pop("key", None)
    _g.pop("param_role", None)
_late7c = apply_gate_suppression(_old_parts, [LPC], until=S(L0, 1500))
check(all(g["suppressed"] == 1 for g in _late7c["remarks"]),
      "6ж: части до v5.1.31 (без key) — по сценарию ключа, как раньше")

# ── 6з. manager: запись без модели, отказанные — по ключу ──
import online.manager as mgr  # noqa: E402
_ev6 = mgr._gate_mark_event("inherited", S(L0, 600), "f", H_C, {"ts": iso(S(L0, 400))}, [5], [LPC, "OIL_DILUTION"],
                            "r")
check(_ev6["alarms"] == [{"scenario": LP, "param_role": "COOLANT_TEMP"}, {"scenario": "OIL_DILUTION"}],
      f"6з: запись журнала — сценарий и роль: {_ev6['alarms']}")
_eng6 = SimpleNamespace(_episodes={LPO: {"id": 841, "t_open": S(L0, 700)}, LPC: {"id": 842, "t_open": S(L0, 700)}},
                        _supp_refused={}, _supp_refused_sig=("x",))
check(mgr._refuse_after(_eng6, [LPC], S(L0, 600)) == [842] and list(_eng6._supp_refused) == [842],
      "6з: отказанные наследованию — только эпизод ОЖ")

# ── 5в / 6и. Гейт в manager: счётчики по роли, пометка ключами ролей ──
import corpus.settings as cs  # noqa: E402
import llm.router as router  # noqa: E402

_ST = sa.build_structural_status({"id": 1, "run_state": 3, "t_start": L0, "characteristics_json": {},
                                  "current_values_json": {"values": {}},
                                  "active_detections_json": [_dOd, _dCd]})
_aa = {a["param_role"]: a for a in _ST["analytics_alarms"]}
check(set(_aa) == {"OIL_TEMP", "COOLANT_TEMP"} and _aa["COOLANT_TEMP"]["description"]
      == "Температура ОЖ у уставки останова панели",
      f"8: статус — у тревоги близости param_role и описание параметра: {_ST['analytics_alarms']}")
LOGM: dict = {"marked": [], "cnt": [], "events": [], "prompt": []}


async def _m_open(*a):
    return {"id": 1, "t_start": L0, "warning_analyses": []}


async def _m_none(*a, **k):
    return None


async def _m_empty(*a, **k):
    return []


async def _m_cnt(*a, **k):
    LOGM["cnt"].append(list(a[3]))
    return {LPO: {"count_window": 5, "dur_window": 600.0, "blind_window": 0.0, "count_since": 0, "dur_since": 0.0},
            LPC: {"count_window": 2, "dur_window": 60.0, "blind_window": 0.0, "count_since": 0, "dur_since": 0.0}}


async def _m_true(*a, **k):
    return True


async def _m_event(*a, event=None, **k):
    LOGM["events"].append(event)
    return True


async def _m_mark(r, e, p, scenarios, **k):
    if k.get("after") is not None:
        return []
    LOGM["marked"].append(list(scenarios))
    return [11]


async def _m_late(*a, **k):
    return False, False


async def _m_llm(prompt, model, depth=None, meta=None):
    LOGM["prompt"].append(prompt)
    return "Анализ: ложное срабатывание датчика.", "cancel", "данные", 10, 5


_M_NAMES = ("get_open_segment", "get_run_state_origin_ts", "count_episodes_batch", "get_open_episodes",
            "get_episodes_overlapping", "set_segment_gate_suppression", "append_segment_gate_event",
            "save_segment_warning", "set_episodes_gate_suppressed", "resolve_segment_id",
            "apply_late_gate_suppression")
_orig_m = {n: getattr(odb, n) for n in _M_NAMES}
_orig_llm, _orig_cs = mgr._run_warning_gate_llm, cs.get_claude_settings
try:
    odb.get_open_segment, odb.get_run_state_origin_ts = _m_open, _m_none
    odb.count_episodes_batch, odb.get_open_episodes, odb.get_episodes_overlapping = _m_cnt, _m_empty, _m_empty
    odb.set_segment_gate_suppression, odb.append_segment_gate_event = _m_true, _m_event
    odb.save_segment_warning, odb.set_episodes_gate_suppressed = _m_true, _m_mark
    odb.resolve_segment_id, odb.apply_late_gate_suppression = _m_none, _m_late
    mgr._run_warning_gate_llm = _m_llm
    mgr._GATE_SAVE_RETRY_SEC = 0
    cs.get_claude_settings = lambda: {"model": "claude-sonnet-5-5", "max_tokens": 1000, "proxy": ""}
    router.apply_depth("gate_caution", "fast", "low", "interactive", provider="llm")
    _out = asyncio.run(mgr._analyze_warning_claude("SN", "pcc", 1, json.loads(json.dumps(_ST, default=str)), "h"))
    _pr = LOGM["prompt"][0] if LOGM["prompt"] else ""
    check(LOGM["cnt"] == [[LP]] and "за 30 дней: 5" in _pr and "за 30 дней: 2" in _pr,
          f"5в: счётчики гейта — запрос по сценарию, у тревог свои по роли: {LOGM['cnt']}")
    check(LOGM["marked"] and sorted(LOGM["marked"][0]) == [LPC, LPO],
          f"6и: пометка — ключи обеих ролей состава: {LOGM['marked']}")
    _gev = [e for e in LOGM["events"] if e.get("kind") == "llm"]
    check(_gev and sorted(a.get("param_role") for a in _gev[0]["alarms"]) == ["COOLANT_TEMP", "OIL_TEMP"],
          f"6и: запись разбора — роль у тревог близости: {_gev and _gev[0]['alarms']}")
finally:
    for _n, _f in _orig_m.items():
        setattr(odb, _n, _f)
    mgr._run_warning_gate_llm, cs.get_claude_settings = _orig_llm, _orig_cs

# ══ 7. Сводка: вид на параметр ════════════════════════════════════════════════
_names = sorted(g["name"] for g in _p7["remarks"])
check(_names == ["Приближение к уставке останова панели (Температура ОЖ)",
                 "Приближение к уставке останова панели (Температура масла)"]
      and _p7["verdict"]["remarks_n"] == 2 and _p7["verdict"]["title"].endswith("ЗАМЕЧАНИЯ К РАБОТЕ — 2"),
      f"7а: два вида с параметром, «ЗАМЕЧАНИЯ — 2»: {_names}, {_p7['verdict']}")
check(_g7["OIL_TEMP"]["key"] == LPO and _g7["COOLANT_TEMP"]["key"] == LPC
      and all(g["at_end"] for g in _p7["remarks"]),
      f"7а: ключ и роль у группы (строка без роли — по уставке 103,9), обе на конец: {_g7}")
check(_g7["OIL_TEMP"]["period_sec"] == 1400 and _g7["COOLANT_TEMP"]["period_sec"] == 1200,
      f"7а: воздействие — до конца кластера своей роли: {[(g['name'], g.get('period_sec')) for g in _p7['remarks']]}")
# Активна на конец только ОЖ — масло кончилось давно, «активен на конец» по своей роли
_segE = SEG([SUB(0, 1500, [DET("OIL_TEMP", S(L0, 100), S(L0, 200)),
                           DET("COOLANT_TEMP", S(L0, 300), S(L0, 1500), True)])])
_eps_e = [dict(EPS7[0], t_close=S(L0, 200)), EPS7[1]]
_pe = build_summary_parts([_segE], episodes=_eps_e, tz=MSK, t_from=L0, t_to=S(L0, 1500))
_ge = {g.get("param_role"): g for g in _pe["remarks"]}
check(_ge["COOLANT_TEMP"]["at_end"] and not _ge["OIL_TEMP"]["at_end"],
      f"7б: «активен на конец» — по своей роли: {[(g['name'], g['at_end']) for g in _pe['remarks']]}")
# Подтверждение — по кластеру своей роли: эпизод масла без кластера масла в сегменте — не подтверждён
_segOnlyC = SEG([SUB(0, 1500, [DET("COOLANT_TEMP", S(L0, 300), S(L0, 1500), True)])])
_pu = build_summary_parts([_segOnlyC], episodes=[dict(EPS7[0], t_close=S(L0, 600)), EPS7[1]], tz=MSK,
                          t_from=L0, t_to=S(L0, 1500))
_gu = {g.get("param_role"): g for g in _pu["remarks"]}
check(_gu["OIL_TEMP"]["unconfirmed"] == 1 and _gu["COOLANT_TEMP"]["unconfirmed"] == 0,
      f"7в: подтверждение — кластером своей роли (ОЖ масло не подтверждает): {_gu}")

# ══ 8. API, статус и промпт гейта ═════════════════════════════════════════════
# web.routes без fastapi (локально его нет) — функция из исходника, как в test_api_v5119
import ast  # noqa: E402

_rsrc = (ROOT / "web" / "routes.py").read_text(encoding="utf-8")
_fn = next(n for n in ast.parse(_rsrc).body if isinstance(n, ast.FunctionDef) and n.name == "_param_role")
_ns: dict = {"logger": logging.getLogger("web")}
exec(compile(ast.Module(body=[_fn], type_ignores=[]), "routes._param_role", "exec"), _ns)
_param_role = _ns["_param_role"]
check('"param_role":      _param_role(e),' in _rsrc, "8: param_role — в active_alarms /api/machines")
check(_param_role({"scenario": LP, "open_values_json": json.dumps({"role": "OIL_TEMP"})}) == "OIL_TEMP"
      and _param_role({"scenario": LP, "open_values_json": {"shutdown_limit": 103.9}}) == "COOLANT_TEMP"
      and _param_role({"scenario": "RPM_UNDERSPEED", "open_values_json": {"role": "RPM"}}) is None,
      "8: API param_role — из JSON, у старых строк по уставке, у прочих — null")
_pr8 = sa.build_warning_prompt(_ST, episode_timeline=[
    {"scenario": LP, "severity": "CAUTION", "t_open": S(L0, 100), "t_close": None, "active_sec": 60,
     "open_values_json": {"role": "COOLANT_TEMP"}}],
    trip_context={"open_alarms": [{"scenario": LP, "param_role": "OIL_TEMP", "severity": "CAUTION",
                                   "active_sec": 60}]})
check("LIMIT_PROXIMITY (Температура ОЖ)" in _pr8 and "Висела тревога: LIMIT_PROXIMITY (Температура масла)" in _pr8,
      "8: хронология и контекст аварии во входе гейта — с параметром близости")

# ══ 9. NO_LOAD_TOO_LONG у «Работы» не через рез (В-30.3) ═════════════════════
N0 = datetime(2026, 9, 29, 13, 13, 22, tzinfo=U)     # ДГУ №1 29.09 16:13:22 МСК — пуск
N1 = S(N0, 1580)                                     # 16:39:42


def nl_rows(load_from: float | None, load_val: float = 50.0, a=N0, b=N1, skip=None):
    """Пакеты каждые 10 с; строки нагрузки нет до load_from (регистр пишется при изменении)."""
    rows = pings(S(a, -300), b, skip=skip)
    if load_from is not None:
        rows.append(h(LOAD, S(a, load_from), load_val))
    return rows


NLK = "NO_LOAD_TOO_LONG"


def nl(rows, held=None, carry=False, gaps=None, a=N0, b=N1, **k):
    return sp(rows, t0=a, t1=b, gaps=gaps, held=held, carry=carry, cfg=CFG, **k).get(NLK) or []


_held0 = {"LOAD_PCT": (S(N0, -400), 0.0)}
# 9а. Со стоянки: удержанная 0 — отрезок с t0, нарушение с t0 + 900 (раньше — нет эпизода)
_c9a = nl(nl_rows(1500), held=_held0)
check(len(_c9a) == 1 and _c9a[0]["start"] == iso(S(N0, 900)) and _c9a[0]["end"] == iso(S(N0, 1500))
      and _c9a[0]["run_start"] == iso(N0) and _c9a[0]["carry_sec"] == 0.0 and not _c9a[0]["carried"]
      and _c9a[0]["run_sec"] == 1500.0,
      f"9а: «Работа» со стоянки, удержанная 0 — с начала окна, нарушение с +900 с: {_c9a}")
check(not nl(nl_rows(1500)), "9а: удержанной нет — с первой строки, как раньше (строка 50 % — отрезка нет)")
# Строка нагрузки на разгоне (до t0) — удержанное на t0 значение именно она
_c9a2 = nl(nl_rows(1500) + [h(LOAD, S(N0, -60), 0.0)], held={"LOAD_PCT": (S(N0, -400), 40.0)},
           seg_start=S(N0, -120))
check(len(_c9a2) == 1 and _c9a2[0]["start"] == iso(S(N0, 900)),
      f"9а: строка разгона (после начала сегмента, до t0) позже удержанной — она решает: {_c9a2}")
# Ревью v5.1.31 (E): строка до начала сегмента (преамбула) — не в счёт, решает held_before
check(not nl(nl_rows(1500) + [h(LOAD, S(N0, -60), 0.0)], held={"LOAD_PCT": (S(N0, -400), 40.0)},
             seg_start=S(N0, -30))
      and not nl(nl_rows(1500) + [h(LOAD, S(N0, -60), 0.0)], seg_start=N0),
      "9а: строка преамбулы до начала сегмента удержанное не решает (дыра до сегмента за ней не видна)")
# Идущий на краю окна: открытое окно
_c9a3 = nl(nl_rows(None), held=_held0, b=S(N0, 1200))
check(len(_c9a3) == 1 and _c9a3[0]["start"] == iso(S(N0, 900)) and _c9a3[0]["ongoing"],
      f"9а: без единой строки нагрузки — идёт с +900 с до края окна: {_c9a3}")
# 9б. Удержанная не ниже порога — с первой строки (как сейчас)
check(not nl(nl_rows(1500), held={"LOAD_PCT": (S(N0, -400), 30.0)}),
      "9б: удержанная 30 % — отсчёт с первой строки, нарушения нет")
check(not nl(nl_rows(1500, 0.0), held={"LOAD_PCT": (S(N0, -400), 10.0)}),
      "9б: удержанная ровно на пороге (10 %) — не «ниже», отрезок с первой строки (+1500, 80 с) — нет")
# 9в. Перенос через рез (carry) — как в v5.1.30: с переносом аккумулятора
_c9c = nl(nl_rows(1500), held=_held0, carry=True, noload_carry_sec=600.0)
check(len(_c9c) == 1 and _c9c[0]["start"] == iso(S(N0, 300)) and _c9c[0]["carry_sec"] == 600.0
      and _c9c[0]["run_start"] == iso(S(N0, -600)),
      f"9в: перенос через рез — по аккумулятору (900 − 600 = +300 с): {_c9c}")
# 9г. Дыра связи рвёт отрезок; время без связи не считается
_g9 = [{"gap_start": S(N0, 600), "gap_end": S(N0, 700)}]
check(not nl(nl_rows(1500, skip=(S(N0, 600), S(N0, 700))), held=_held0, gaps=_g9),
      "9г: дыра связи на +600 — отрезок рвётся (после дыры значение неизвестно до свежей строки)")
check(not nl(nl_rows(1500), held=_held0, gaps=[{"gap_start": S(N0, -200), "gap_end": S(N0, -100)}]),
      "9г: дыра между удержанной строкой и началом окна — удержанное неизвестно, с первой строки")
check(not nl(nl_rows(1500, skip=(S(N0, -250), S(N0, -100))), held=_held0),
      "9г: пауза пакетов > 90 с до начала окна — удержанное неизвестно")
check(not nl(nl_rows(1500, skip=(S(N0, 400), S(N0, 520))), held=_held0),
      "9г: пауза пакетов 120 с внутри — отрезок рвётся")
# 9д. Через segment(): «Работа» после стоянки (смена режима на начале окна)
_rows9 = nl_rows(1500)
_t, _k = S(N0, -300), 0
while _t < N1:
    _rows9 += [h(RPM, _t, 1500.0 + _k % 2), h(FREQ, _t, 50.0)]
    _t, _k = S(_t, 10), _k + 1
_rows9.append(h(LOAD, S(N0, -200), 0.0))
_s9 = segment([rs(0, S(N0, -7200), N0), rs(3, N0)], sorted(_rows9, key=lambda r: r["ts"]), [], [], CFG,
              "SN", "pcc", 1, "E", N0, N1)
_d9 = [(sub, d) for s in _s9 for sub in s.subsegments for d in sub.detections
       if d.scenario == NLK and not sub.transient]
check(_d9 and {d.t_detected for _, d in _d9} == {iso(S(N0, 900))}
      and _d9[0][1].values["live"]["clusters"][0]["run_start"] == iso(N0),
      f"9д: segment() — «Работа» со стоянки: эпизод с момента превышения 900 с от начала окна: "
      f"{[(d.t_detected, d.values.get('live', {}).get('clusters')) for _, d in _d9]}")
_s9r = segment([rs(3, S(N0, -7200))], sorted(_rows9, key=lambda r: r["ts"]), [], [], CFG,
               "SN", "pcc", 1, "E", N0, N1)
_d9r = [d for s in _s9r for sub in s.subsegments for d in sub.detections if d.scenario == NLK]
_s9r2 = segment([rs(3, S(N0, -7200))], sorted(_rows9, key=lambda r: r["ts"]), [], [], CFG,
                "SN", "pcc", 1, "E", N0, N1, initial_coking_risk=CokingRisk(noload_run_sec=600.0))
_d9r2 = [d for s in _s9r2 for sub in s.subsegments for d in sub.detections if d.scenario == NLK]
check({d.t_detected for d in _d9r} == {iso(S(N0, 900))} and {d.t_detected for d in _d9r2} == {iso(S(N0, 300))},
      f"9д: суточный рез (перенос) — путь v5.1.30 без изменений: удержанная 0 и аккумулятор 0 / 600 с — "
      f"+900 / +300 с: {[d.t_detected for d in _d9r]}, {[d.t_detected for d in _d9r2]}")
# Сбой расчёта удержанного — предупреждение, отрезок с первой строки
_orig_sh = _lv._start_held


def _boom(*a, **k):
    raise RuntimeError("тест")


_lv._start_held = _boom
try:
    check(not nl(nl_rows(1500), held=_held0), "9е: сбой удержанного на начало — как раньше, без падения")
finally:
    _lv._start_held = _orig_sh

# ── 9ж. Ревью v5.1.31 (A): «Работа» со стоянки идёт через суточный рез — один эпизод ──
# Пуск 08:40 МСК, удержанная нагрузка 0, строк LOAD_PCT до реза нет: аккумулятор нагара
# досчитывает кусок до первой строки — перенос через рез согласован с участками
CUT9 = datetime(2026, 9, 29, 6, 0, tzinfo=U)                 # 29.09 09:00 МСК
P9, W09, W29 = S(CUT9, -1200), S(CUT9, -3 * 3600), S(CUT9, 3600)
_r9j = pings(W09, W29)
_t, _k = P9, 0
while _t < W29:
    _r9j += [h(RPM, _t, 1500.0 + _k % 2), h(FREQ, _t, 50.0)]
    _t, _k = S(_t, 10), _k + 1
_r9j = sorted(_r9j + [h(LOAD, S(W09, 60), 0.0)], key=lambda r: r["ts"])
_en9 = [rs(0, W09, P9), rs(3, P9)]
_s9j1 = segment(_en9, [r for r in _r9j if r["ts"] < CUT9], [], [], CFG, "SN", "pcc", 1, "E", W09, CUT9)
_acc9 = _s9j1[-1].subsegments[-1].risk_accumulators.coking_risk
_s9j2 = segment(_en9, _r9j, [], [], CFG, "SN", "pcc", 1, "E", CUT9, W29, initial_coking_risk=_acc9)


def _nlc(segs):
    out = []
    for s_ in segs:
        for sub in s_.subsegments:
            for d in sub.detections:
                if d.scenario == NLK and not sub.transient:
                    out += [c for c in d.values["live"]["clusters"] if c not in out]
    return out


_c91, _c92 = _nlc(_s9j1), _nlc(_s9j2)
check(len(_c91) == 1 and _c91[0]["start"] == iso(S(P9, 900)) and _c91[0]["end"] == iso(CUT9)
      and abs(_acc9.noload_run_sec - 1200.0) < 1e-6,
      f"9ж: окно до реза — нарушение с пуска + 900 с, аккумулятор на резе 1200 с: {_c91}, {_acc9.noload_run_sec}")
check(len(_c92) == 1 and _c92[0]["start"] == iso(CUT9) and _c92[0]["carried"]
      and _c92[0]["run_start"] == iso(P9) and _c92[0]["carry_sec"] == 1200.0,
      f"9ж: после реза — продолжение с 09:00, перенос, начало отрезка 08:40 (было: с 09:15, пара "
      f"«закрыт/открыт»): {_c92}")
# Тот же сценарий через подсегментный путь детектора (без участков) — по аккумулятору тоже нарушение
_sub9 = [sub for s_ in _s9j1 for sub in s_.subsegments if not sub.transient][-1]
_d9acc = [d for d in run_all_detectors(_sub9.characteristics, _sub9.derived_metrics, _sub9.risk_accumulators,
                                       [], _sub9.load_zone, 3, datetime.fromisoformat(_sub9.t_start),
                                       datetime.fromisoformat(_sub9.t_end), None, CFG, live_spans=None)
          if d.scenario == NLK]
check(_d9acc and _d9acc[0].values["noload_run_sec"] == 1200.0,
      f"9ж: детектор по подсегменту (аккумулятор) согласован с участками: {[d.values for d in _d9acc]}")
# Метрики нагара от досчёта не меняются — только счётчик «без нагрузки»
from analytics.accumulators import update_accumulators  # noqa: E402
_sw = by_addr([r for r in _r9j if P9 <= r["ts"] < CUT9])
_a0 = update_accumulators(RiskAccumulators(), _sw, P9, CUT9, "LOW", 3, CFG, [])
_a1 = update_accumulators(RiskAccumulators(), _sw, P9, CUT9, "LOW", 3, CFG, [], noload_head="start")
_d0, _d1 = _a0.coking_risk.to_dict(), _a1.coking_risk.to_dict()
_diff = sorted(k for k in set(_d0) | set(_d1) if _d0.get(k) != _d1.get(k))
check(_diff == ["noload_run_sec"] and _a1.coking_risk.noload_run_sec == 1200.0
      and _a0.thermal_risk.to_dict() == _a1.thermal_risk.to_dict(),
      f"9ж: досчёт меняет только noload_run_sec (нагар, прожиг, тепло — как были): {_diff}")
# Дыра связи до первой строки — досчёта нет; продолжение ("cont") копит, а не сбрасывает
_a2 = update_accumulators(RiskAccumulators(), _sw, P9, CUT9, "LOW", 3, CFG,
                          [{"gap_start": S(P9, 300), "gap_end": S(P9, 360)}], noload_head="start")
_a3 = update_accumulators(_a1, by_addr([]), CUT9, S(CUT9, 600), "LOW", 3, CFG, [], noload_head="cont")
check(_a2.coking_risk.noload_run_sec == 0.0 and _a3.coking_risk.noload_run_sec == 1800.0,
      f"9ж: дыра — без досчёта; продолжение — копится: {_a2.coking_risk.noload_run_sec}, "
      f"{_a3.coking_risk.noload_run_sec}")

# ── 9з. Ревью v5.1.31 (перепроверка A): кусок до первой строки — не дальше конца отрезка по
# участкам (дыра, пауза пакетов); через два реза без строк — непрерывно ──
P9z = S(CUT9, -1500)                                          # пуск 08:35 МСК


def _two(rows, faults=(), gaps=(), n_win=2):
    """Окна [W0, рез), [рез, рез + 1 ч) (n_win=3 — ещё сутки до второго реза и час после)."""
    rows = sorted(rows, key=lambda r: r["ts"])
    per = [rs(0, W09, P9z), rs(3, P9z)]
    cuts = [W09, CUT9, S(CUT9, 3600)] if n_win == 2 else [W09, CUT9, S(CUT9, 86400), S(CUT9, 86400 + 3600)]
    out, acc = [], None
    for a, b in zip(cuts, cuts[1:]):
        sg = segment(per, [r for r in rows if r["ts"] < b], list(faults), list(gaps), CFG, "SN", "pcc", 1, "E",
                     a, b, **({"initial_coking_risk": acc} if acc is not None else {}))
        out.append((_nlc(sg), sg[-1].subsegments[-1].risk_accumulators.coking_risk))
        acc = out[-1][1]
    return out


def _pk(a, b, skip=None):
    """Пакеты каждые 10 с (пинг; с пуска — обороты и частота); skip — (от, до) без пакетов."""
    rr, t, k = [], a, 0
    while t < b:
        if not (skip and skip[0] < t < skip[1]):
            rr.append(h(HB, t, float(k)))
            if t >= P9z:
                rr += [h(RPM, t, 1500.0 + k % 2), h(FREQ, t, 50.0)]
        t, k = S(t, 10), k + 1
    return rr


_flt = [{"addr": 40401, "bit": 0, "fault_start": S(P9z, 600), "fault_end": S(P9z, 620), "severity": "warning",
         "value": 1}]
# P3: разрез подсегмента неисправностью на +600, дыра data_gaps +700…+800, строка 2 % на +1000
_w = _two(_pk(W09, S(CUT9, 3600), (S(P9z, 700), S(P9z, 800))) + [h(LOAD, S(W09, 60), 0.0),
                                                               h(LOAD, S(P9z, 1000), 2.0)],
          faults=_flt, gaps=[{"gap_start": S(P9z, 700), "gap_end": S(P9z, 800)}])
check(abs(_w[0][1].noload_run_sec - 500.0) < 1e-6 and len(_w[1][0]) == 1
      and _w[1][0][0]["start"] == iso(S(CUT9, 400)) and _w[1][0][0]["run_start"] == iso(S(P9z, 1000)),
      f"9з P3: дыра в подсегменте «cont» — кусок до неё не копится, счёт со строки после дыры "
      f"(акк. 500 с, эпизод с 09:06:40, отрезок с 08:51:40): {_w[0][1].noload_run_sec}, {_w[1][0]}")
# P4: пауза пакетов +300…+500 без data_gaps, строк нет — участки рвутся, аккумулятор тоже
_w = _two(_pk(W09, S(CUT9, 3600), (S(P9z, 300), S(P9z, 500))) + [h(LOAD, S(W09, 60), 0.0)])
check(_w[0][1].noload_run_sec == 0.0 and not _w[0][0] and not _w[1][0],
      f"9з P4: пауза пакетов без data_gaps — отрезок кончился, перенос 0, эпизода нет: "
      f"{_w[0][1].noload_run_sec}, {_w[1][0]}")
# Два реза подряд без строк нагрузки (> 24 ч): перенос копится и во втором окне — один эпизод
_w = _two(_pk(W09, S(CUT9, 86400 + 3600)) + [h(LOAD, S(W09, 60), 0.0)], n_win=3)
check(abs(_w[0][1].noload_run_sec - 1500.0) < 1e-6 and abs(_w[1][1].noload_run_sec - 87900.0) < 1e-6
      and [len(x[0]) for x in _w] == [1, 1, 1]
      and _w[2][0][0]["start"] == iso(S(CUT9, 86400)) and _w[2][0][0]["carried"]
      and _w[2][0][0]["run_start"] == iso(P9z),
      f"9з: два реза без строк — перенос и во втором окне (1500 → 87 900 с), эпизод с 09:00 второго дня, "
      f"отрезок с 08:35: {[(x[0], x[1].noload_run_sec) for x in _w]}")

# ── 10. Ревью v5.1.31 (D, G): худшее у давления — минимум; роль без подписи кодом не печатается ──
from analytics.serializer import _det_worst  # noqa: E402
from corpus.preprocessor import _group_worst  # noqa: E402
_pe = [(1, {"scenario": LP, "values": {"value": 185.0, "role": "OIL_PRESS", "shutdown_limit": 172.0}}),
       (2, {"scenario": LP, "values": {"value": 180.0, "role": "OIL_PRESS", "shutdown_limit": 172.0}})]
_te = [(1, {"scenario": LP, "values": {"value": 100.5, "role": "OIL_TEMP", "shutdown_limit": 110.0}}),
       (2, {"scenario": LP, "values": {"value": 101.0, "role": "OIL_TEMP", "shutdown_limit": 110.0}})]
check(_det_worst(_pe)[0] == 2 and _det_worst(_te)[0] == 2
      and _group_worst(LP, [v for _, d in _pe for v in [d["values"]]]) == "мин. 180 при пороге 172"
      and _group_worst(LP, [v for _, d in _te for v in [d["values"]]]) == "макс. 101 при пороге 110",
      "10: давление — худшее минимум («мин.»), температуры — максимум")
_pg = build_summary_parts([_segOC], episodes=[dict(EPS7[0], open_values_json={"role": "FOO"})], tz=MSK,
                          t_from=L0, t_to=S(L0, 1500))
check([g["name"] for g in _pg["remarks"]] == ["Приближение к уставке останова панели"],
      f"10: роль без подписи — имя без параметра, не кодом: {[g['name'] for g in _pg['remarks']]}")

if _errors:
    print(f"FAIL — {len(_errors)} расхождений:")
    for e_ in _errors:
        print("  •", e_)
    sys.exit(1)
print("Все проверки пройдены.")
