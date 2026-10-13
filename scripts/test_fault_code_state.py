# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""Коды неисправности 40012/40013 — состояние, а не замер (v5.1.1).

До v5.1.1 код был `kind: analog`: периоды enum_history подменялись «замером»
в начале окна, forward-fill переносил его не дольше 90 с, и код жил только в
первом подсегменте. «Общая авария» гасла на смене зоны нагрузки, эпизод
закрывался промахом и на каждом резе заводился заново от того же фронта (у
ДЭС №3 8 строк на одну неисправность). Суточное закрытие прижимало «замер» к
ts_from−300 с — в преамбулу — и теряло код целиком.

Запуск:  py -3 scripts/test_fault_code_state.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from online.status_assembler import GATE_RULES_VERSION as RULES  # noqa: E402

from analytics import binding  # noqa: E402
from analytics.segmenter import (  # noqa: E402
    _split_stop_on_fault_cleared, _state_characteristics, _state_periods, segment,
)
import online.engine as eng  # noqa: E402
from online.manager import _analyzed_hashes  # noqa: E402

_errors: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        _errors.append(msg)


cfg = binding.build_config(ROOT / "knowledge_base", controller_id="pcc3300", engine_id="cummins_kta50")
T0 = datetime(2026, 10, 10, 6, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(hours=2)
FRONT = datetime(2026, 10, 6, 9, 6, 5, tzinfo=timezone.utc)

# 1. Карта: коды — enum, из аналога не читаются
check(cfg.register_map[40012].get("kind") == "enum" and cfg.register_map[40013].get("kind") == "enum",
      "1: 40012/40013 — enum")
check(40012 not in cfg.whitelist_analog and 40012 in cfg.whitelist_enum, "1б: в списке enum, не в аналоге")


def per(addr, value, start, end=None):
    return {"addr": addr, "value": value, "state_start": start, "state_end": end, "label": str(value)}


# 2. Характеристика из периодов: период с 09.10 покрывает любое окно
enum = [per(40011, 3, T0 - timedelta(days=1)), per(40012, 1453, FRONT + timedelta(days=3)),
        per(40013, 1, FRONT + timedelta(days=3)), per(40012, 611, FRONT, FRONT + timedelta(days=3))]
sp = _state_periods(enum, cfg)
check(set(sp) == {40012, 40013}, f"2: состояния — только коды: {set(sp)}")
ch = _state_characteristics(sp, T0 + timedelta(minutes=50), T0 + timedelta(minutes=51), cfg)
check(ch.get("LAST_FAULT_CODE", {}).get("value_end") == 1453 and ch["LAST_FAULT_CODE"].get("kind") == "state",
      f"2б: код в любом подсегменте: {ch.get('LAST_FAULT_CODE')}")
check(ch.get("LAST_FAULT_TYPE", {}).get("value_end") == 1, "2в: тип тоже")
reset = enum + [per(40012, 0, T0 + timedelta(minutes=30))]
reset[1] = per(40012, 1453, FRONT + timedelta(days=3), T0 + timedelta(minutes=30))
ch = _state_characteristics(_state_periods(reset, cfg), T0, T0 + timedelta(hours=1), cfg)["LAST_FAULT_CODE"]
check(ch["value_start"] == 1453 and ch["value_end"] == 0, f"2г: сброс внутри окна — на конец 0: {ch}")

# 3. Сегментатор: работа с зонами нагрузки — код и «Общая авария» во ВСЕХ подсегментах
load_addr = cfg.role_to_addr("LOAD_PCT")
hist = []
t = T0
while t < T1:
    m = (t - T0).total_seconds() / 60
    load = 20.0 if m < 40 or m >= 80 else 70.0
    hist.append({"addr": 40290, "ts": t, "value": 1.0})
    hist.append({"addr": load_addr, "ts": t, "value": load})
    t += timedelta(seconds=30)
ca = {"addr": 40408, "bit": 11, "fault_start": FRONT, "fault_end": None, "severity": None,
      "fault_name": "CommonAlarm", "fault_name_ru": "Общая авария"}


def run(enum_periods):
    return segment(enum_periods=enum_periods, history=hist, fault_periods=[ca], gaps=[], cfg=cfg,
                   router_sn="T", equip_type="pcc", panel_id=1, engine_sn="", ts_from=T0, ts_to=T1)


segs = run(enum)
subs = [s for seg in segs for s in seg.subsegments]
check(len(subs) >= 3, f"3: ожидались смены зоны нагрузки: {len(subs)} подсегм.")
for i, s in enumerate(subs):
    lfc = (s.characteristics.get("LAST_FAULT_CODE") or {}).get("value_end")
    cf = [d for d in s.detections if d.scenario == "CONTROLLER_FAULT" and d.values.get("addr") == 40408]
    check(lfc == 1453, f"3б: код в подсегменте {i} ({s.load_zone}): {lfc}")
    check(len(cf) == 1 and cf[0].t_detected == FRONT.isoformat(),
          f"3в: «Общая авария + код» в подсегменте {i}, онсет — фронт: {[d.t_detected for d in cf]}")

# 4. Сброс кнопкой посреди окна — после него ни кода, ни CAUTION
segs = run(reset)
last = segs[-1].subsegments[-1]
check((last.characteristics.get("LAST_FAULT_CODE") or {}).get("value_end") == 0
      and not any(d.scenario == "CONTROLLER_FAULT" for d in last.detections),
      "4: после сброса код 0, детекции нет")

# 5. Легаси-рез FAULT_CLEARED — код по периодам, а не по «замерам»
stop = [{"addr": 40011, "value": 0, "state_start": T0, "state_end": None}]
lfc_per = [per(40012, 1453, FRONT, T0 + timedelta(minutes=20)), per(40012, 0, T0 + timedelta(minutes=20))]
parts = _split_stop_on_fault_cleared(stop, [], {}, cfg, T1, lfc_per)
check(len(parts) == 2 and parts[0]["state_end"] == T0 + timedelta(minutes=20)
      and parts[1].get("cause_open") == "FAULT_CLEARED", f"5: рез по сбросу кода: {parts}")

# 6. Движок: тот же онсет — переоткрыть, а не плодить
calls: dict[str, list] = {"open": [], "reopen": []}


async def _find(*a, **k):
    return {"id": 14850, "t_close": T0 + timedelta(minutes=10), "severity": "CAUTION"}


async def _open(*a, **k):
    calls["open"].append(k)
    return 99


async def _reopen(ep_id, **k):
    calls["reopen"].append((ep_id, k))


async def _noop(*a, **k):
    return None


class Cfg:
    def det(self, *a, default=None):
        return default


eng.online_db.find_episode_by_onset = _find
eng.online_db.open_episode = _open
eng.online_db.reopen_episode = _reopen
eng.online_db.update_episode = _noop
eng.online_db.close_episode = _noop
eng.online_db.get_open_segment = _noop
e = eng.OnlinePollEngine.__new__(eng.OnlinePollEngine)
e.cfg = Cfg()
e.router_sn, e.equip_type, e.panel_id = "T", "pcc", 1
e._episodes, e._last_open_seg_id, e._supp_recheck = {}, 5, None
e._episode_accrual_ts = T0 + timedelta(minutes=29)
e._attach_trip_context = _noop
det = {"scenario": "CONTROLLER_FAULT", "severity": "CAUTION", "t_detected": FRONT.isoformat(),
       "values": {"addr": 40408, "bit": 11, "fault_name": "CommonAlarm (Общая авария)"}}
asyncio.run(e._process_episodes([det], T0 + timedelta(minutes=30), []))
check(calls["open"] == [] and len(calls["reopen"]) == 1 and calls["reopen"][0][0] == 14850,
      f"6: переоткрыт 14850, новой строки нет: {calls}")
check(calls["reopen"][0][1]["now_ts"] == T0 + timedelta(minutes=30),
      f"6б: воздействие — от фронта до конца окна (GREATEST в SQL): {calls['reopen']}")
check(e._episodes.get("CONTROLLER_FAULT|40408|11", {}).get("id") == 14850, "6в: память движка — тот же id")


async def _none(*a, **k):
    return None


eng.online_db.find_episode_by_onset = _none
e._episodes = {}
asyncio.run(e._process_episodes([dict(det, t_detected=(T0 + timedelta(minutes=5)).isoformat())],
                                T0 + timedelta(minutes=31), []))
check(len(calls["open"]) == 1, "6г: новый онсет — новый эпизод")

# 6д. Снятие прежнего периода той же маски (раньше фронта) — не конец эпизода
async def _close(ep_id, t_close, reason):
    calls.setdefault("close", []).append((ep_id, t_close))


eng.online_db.close_episode = _close
e._episodes = {"CONTROLLER_FAULT|40408|11": {"id": 7, "severity": "CAUTION", "miss": 0,
                                            "first_miss_ts": None, "t_open": T0 + timedelta(minutes=10)}}
e._episode_accrual_ts = T0 + timedelta(minutes=40)
for k in range(3):
    asyncio.run(e._process_episodes([], T0 + timedelta(minutes=41 + k), [],
                                    cleared_at={"CONTROLLER_FAULT|40408|11": T0}))
check(calls.get("close") == [(7, T0 + timedelta(minutes=41))],
      f"6д: снятие раньше фронта игнорируется, t_close — по промаху: {calls.get('close')}")

# 6е. Сводка на стыке реза: закрытая строка того же онсета заменяется синтетикой
from types import SimpleNamespace as NS  # noqa: E402


class D:
    def to_dict(self):
        return {"scenario": "CONTROLLER_FAULT", "severity": "CAUTION", "t_detected": FRONT.isoformat(),
                "values": {"addr": 40408, "bit": 11, "fault_name": "CommonAlarm (Общая авария)"}}


segx = NS(subsegments=[NS(detections=[D()])])
closed = {"scenario": "CONTROLLER_FAULT", "addr": 40408, "bit": 11, "t_open": FRONT,
          "t_close": T0 + timedelta(hours=1), "blind_sec": 3600.0, "active_sec": 1.0}
other = dict(closed, t_open=FRONT - timedelta(days=1))
syn = eng._live_new_episodes([segx], [closed, other], {"CONTROLLER_FAULT|40408|11"}, T1)
merged = eng._drop_same_onset([closed, other], syn) + syn
check(len(syn) == 1 and syn[0]["blind_sec"] == 3600.0 and len(merged) == 2 and closed not in merged
      and other in merged, f"6е: одна строка онсета, прежняя вспышка осталась: {merged}")

# 6ж. «Нет данных» декодера (65535) — не код неисправности
na = [per(40012, 1453, FRONT, T0 + timedelta(minutes=30)), per(40012, 65535, T0 + timedelta(minutes=30))]
ch = _state_characteristics(_state_periods(na, cfg), T0 + timedelta(minutes=40), T0 + timedelta(minutes=50), cfg)
check("LAST_FAULT_CODE" not in ch, f"6ж: в периоде NA кода нет: {ch.get('LAST_FAULT_CODE')}")

# 7. Гейт после перезапуска: разобранные составы из записей строки
check(_analyzed_hashes({"warning_analyses": json.dumps([{"fault_hash": "b73f", "rules": RULES},
                                                       {"fault_hash": "5cf2", "rules": RULES}])}) == {"b73f", "5cf2"},
      "7: хеши из JSON-строки")
check(_analyzed_hashes({"warning_analyses": [{"fault_hash": "d6e4", "rules": RULES}, {"md": "x"}]}) == {"d6e4"},
      "7б: из списка")
# v5.1.12: разбор по старым правилам гейта — не «разобрано», состав разберут заново
check(_analyzed_hashes({"warning_analyses": [{"fault_hash": "old1"}, {"fault_hash": "new1", "rules": RULES}]})
      == {"new1"}, "7в: разбор без rules — по старым правилам, не считается")
check(_analyzed_hashes({"warning_analyses": None}) == set() and _analyzed_hashes({"warning_analyses": "битый"}) == set(),
      "7в: пусто / битый JSON")

if _errors:
    print(f"FAIL — {len(_errors)} расхождений:")
    for x in _errors:
        print(f"  • {x}")
    sys.exit(1)
print("Все проверки пройдены.")
