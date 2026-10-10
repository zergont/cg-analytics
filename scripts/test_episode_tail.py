# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""Снятая тревога не попадает в следующий сегмент; время снятия — по панели (v5.0.3).

06.10 ДЭС №3: кнопка аварийного останова снята и машина пошла в прогрев одним
пакетом (09:04:26). Эпизод закрывается после 3 чистых циклов, поэтому сводка
9-секундного прогрева получила «АВАРИЮ»; t_close встал на горизонт обработки
(09:04:07), раньше реального снятия.

Запуск:  py -3 scripts/test_episode_tail.py
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import online.engine as eng  # noqa: E402

_errors: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        _errors.append(msg)


def T(s: str) -> datetime:
    return datetime.fromisoformat(f"2026-10-06T{s}+00:00")


class Det:
    def __init__(self, scenario, values=None):
        self.scenario, self.values = scenario, values or {}

    def to_dict(self):
        return {"scenario": self.scenario, "values": self.values}


class Sub:
    def __init__(self, dets):
        self.detections = dets


class Seg:
    def __init__(self, subs):
        self.subsegments = subs


ESTOP = {"addr": 40407, "bit": 5}
estop_cleared = Det("CONTROLLER_FAULT", {**ESTOP, "fault_end": "2026-10-06T09:04:26+00:00"})
stop_seg = Seg([Sub([estop_cleared])])                    # стоп: кнопка висела до 09:04:26
warmup_seg = Seg([Sub([])])                               # прогрев 09:04:26–09:04:35: кнопки нет

# 1. Время снятия панели
ca = eng._panel_cleared_at([stop_seg, warmup_seg])
check(ca.get("CONTROLLER_FAULT|40407|5") == T("09:04:26"), f"1: время снятия по fault_end: {ca}")

# 2. Хвост дебаунса в сводке закрываемого прогрева
ep_estop = {"scenario": "CONTROLLER_FAULT", "addr": 40407, "bit": 5, "t_open": T("04:00:00") - timedelta(hours=13),
            "t_close": None, "severity": "SHUTDOWN"}
kept = eng._filter_window_episodes([ep_estop], T("09:04:26"), T("09:04:35"), tail_sec=120)
check(kept == [ep_estop], "2: без правки фильтр окна оставляет открытый эпизод (воспроизведение бага)")
check(eng._drop_debounce_tails(kept, [warmup_seg], {}) == [], "2б: прогрев — кнопка снята до него, в сводку не идёт")
check(eng._drop_debounce_tails([ep_estop], [stop_seg], {}) == [ep_estop], "2в: стоп — кнопка висела, остаётся")

ep_an = {"scenario": "OIL_DILUTION", "t_open": T("08:00:00"), "t_close": None}
check(eng._drop_debounce_tails([ep_an], [warmup_seg], {}) == [ep_an],
      "2г: аналитика без промаха остаётся (короткий сегмент мог не дать детекцию)")
check(eng._drop_debounce_tails([ep_an], [warmup_seg], {"OIL_DILUTION": {"miss": 1}}) == [],
      "2д: аналитика с промахом без детекции в сегменте — хвост")
ep_closed = {**ep_estop, "t_close": T("09:04:26")}
check(eng._drop_debounce_tails([ep_closed], [warmup_seg], {}) == [ep_closed], "2е: закрытые эпизоды не трогаем")


# 3. _process_episodes: t_close = fault_end
class Cfg:
    def det(self, *a, default=None):
        return default


calls: dict[str, list] = {"close": [], "update": []}


async def _close(ep_id, t_close, reason):
    calls["close"].append((ep_id, t_close))


async def _update(*a, **k):
    calls["update"].append(a)


eng.online_db.close_episode = _close
eng.online_db.update_episode = _update

e = eng.OnlinePollEngine.__new__(eng.OnlinePollEngine)
e.cfg = Cfg()
e.router_sn, e.equip_type, e.panel_id = "T", "pcc", 1
e._episodes = {"CONTROLLER_FAULT|40407|5": {"id": 77, "severity": "SHUTDOWN", "miss": 0, "first_miss_ts": None}}
e._episode_accrual_ts = T("09:03:37")


async def run_cycles():
    await e._process_episodes([], T("09:04:07"), [], cleared_at=ca)        # первый промах
    await e._process_episodes([], T("09:04:37"), [], cleared_at={})
    await e._process_episodes([], T("09:05:07"), [], cleared_at={})


asyncio.run(run_cycles())
check(calls["close"] == [(77, T("09:04:26"))], f"3: эпизод закрыт временем снятия панели: {calls['close']}")

e._episodes = {"OIL_DILUTION": {"id": 78, "severity": "CAUTION", "miss": 0, "first_miss_ts": None}}
calls["close"].clear()
e._episode_accrual_ts = T("09:03:37")
asyncio.run(run_cycles())
check(calls["close"] == [(78, T("09:04:07"))], f"3б: без времени панели — горизонт обработки, как раньше: {calls['close']}")

if _errors:
    print(f"FAIL — {len(_errors)} расхождений:")
    for x in _errors:
        print(f"  • {x}")
    sys.exit(1)
print("Все проверки пройдены.")
