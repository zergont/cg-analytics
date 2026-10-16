# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""Вход разбора аварии v5.1.14: оборудование, числовой код, состояние на момент останова.

Совет 12.10 без марки контроллера и числового кода искал по Datakom/DeepSea, с
ними — нашёл руководство PowerCommand 3.3. Команда и положение автомата не
менялись весь пуск и в ленту акта не попали, хотя для причины это главное.

Запуск:  py -3 scripts/test_incident_input_v5114.py
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import online.incident_gate as ig  # noqa: E402
from analytics.config import AnalyticsConfig  # noqa: E402
from llm.router import TASKS  # noqa: E402

_errors: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        _errors.append(msg)


KB = ROOT / "knowledge_base"
STOP = datetime(2026, 10, 12, 10, 57, 2, tzinfo=timezone.utc)

# 1. KB: имя контроллера и регистры «состояние на момент останова»
cfg = AnalyticsConfig.from_pair(KB, "pcc3300", "cummins_kta50")
check(cfg.controller_label == "Cummins PowerCommand PCC3300", f"1: имя контроллера: {cfg.controller_label}")
check([a for a, _ in cfg.incident_state_registers] == [43560, 43562],
      f"1б: команда и положение автомата: {cfg.incident_state_registers}")

# 2. Числовые коды из ленты акта
INC = {"stop_ts": STOP.isoformat(), "character": {"character": "immediate", "confidence": "high"},
       "window": {"from": "2026-09-29T13:41:13+00:00"},
       "chronology": [
           {"ts": STOP.isoformat(), "kind": "state", "addr": 40012, "role": "LAST_FAULT_CODE", "value": 1452,
            "label": "Отказ включения автомата генераторной установки (ПРЕДУПРЕЖДЕНИЕ)", "name": "Код"},
           {"ts": STOP.isoformat(), "kind": "state", "addr": 40012, "role": "LAST_FAULT_CODE", "value": 1452,
            "label": "дубль", "name": "Код"},
           {"ts": STOP.isoformat(), "kind": "state", "addr": 40012, "role": "LAST_FAULT_CODE", "value": 0,
            "label": "нет", "name": "Код"}]}
line = ig._fault_codes_line(INC)
check(line == "Коды неисправностей панели в акте (40012): 1452 — Отказ включения автомата генераторной "
              "установки (ПРЕДУПРЕЖДЕНИЕ)", f"2: коды: {line}")
check(ig._fault_codes_line({"chronology": []}) is None, "2б: кодов нет — строки нет")

# 3. Вход: оборудование, коды, состояние (давнее — с датой, сегодняшнее — время)
ctx = {"equipment": {"name": "ДГУ №1 инв. 2569", "engine": "Cummins KTA50", "engine_sn": "25428546",
                     "controller": "Cummins PowerCommand PCC3300"},
       "states": [{"addr": 43560, "name": "Команда на включение", "label": "Не активно",
                   "since": "2026-09-29T13:10:22+00:00"},
                  {"addr": 43562, "name": "Положение", "label": "Разомкнут",
                   "since": "2026-10-12T10:43:12+00:00"}]}
p = ig.build_incident_prompt(INC, None, [], ctx)
check("Двигатель: Cummins KTA50, s/n 25428546" in p and "Cummins PowerCommand PCC3300" in p,
      "3: оборудование во входе")
check("1452 — Отказ включения" in p, "3б: числовой код во входе")
# v5.1.22 (P1-6): время во входе — МСК; давнее состояние — с датой
check("43560 Команда на включение = Не активно (с 29.09 16:10:22)" in p
      and "43562 Положение = Разомкнут (с 13:43:12)" in p and "UTC" not in p
      and "Время во входе — МСК." in p, f"3в: состояние на момент останова: {p}")
check(p.index("Оборудование") < p.index("Останов:"), "3г: оборудование — в начале")
p0 = ig.build_incident_prompt(INC, None, [])
check("Оборудование" not in p0 and "Состояние на момент" not in p0, "3д: без контекста — как раньше")


# 4. Контекст: реестр + KB + enum_history; сбой — fail-open
async def _eq(r, e, p):
    return {"name": "ДГУ №1 инв. 2569", "manufacturer": "Cummins", "model": "KTA50", "engine_sn": "25428546",
            "controller_id": "pcc3300", "engine_id": "cummins_kta50", "kb_path": None}


async def _periods(r, e, p, t0, t1, addrs=None):
    assert t1 == STOP and (STOP - t0).total_seconds() == 1, (t0, t1)
    return [{"addr": 43560, "state_start": datetime(2026, 9, 29, 13, 10, 22, tzinfo=timezone.utc),
             "value": 0, "label": "Не активно"},
            {"addr": 43562, "state_start": datetime(2026, 9, 29, 13, 10, 22, tzinfo=timezone.utc),
             "value": 0, "label": "Разомкнут"}]


import db.analytics as dba  # noqa: E402
import analytics.source as src  # noqa: E402
from config import settings as app_settings  # noqa: E402
dba.get_equipment = _eq
src.get_enum_periods = _periods
app_settings.knowledge_base_path = KB
c = asyncio.run(ig._context("6003790403", "pcc", 1, STOP))
check(c.get("equipment", {}).get("controller") == "Cummins PowerCommand PCC3300"
      and c["equipment"]["engine"] == "Cummins KTA50", f"4: оборудование: {c.get('equipment')}")
check([(s["addr"], s["label"]) for s in c.get("states", [])] == [(43560, "Не активно"), (43562, "Разомкнут")],
      f"4б: состояние: {c.get('states')}")


async def _boom(*a, **k):
    raise RuntimeError("БД недоступна")


dba.get_equipment = _boom
check(asyncio.run(ig._context("SN", "pcc", 1, STOP)) == {}, "4в: сбой реестра — без блока, не падаем")

# 4г. KB с «controller: X» строкой (правка в веб-морде) — имя, а не падение
cfg2 = AnalyticsConfig.from_pair(KB, "pcc3300", "cummins_kta50")
cfg2.mapping["controller"] = "PCC3300"
check(cfg2.controller_label == "PCC3300", f"4г: controller строкой: {cfg2.controller_label}")
cfg2.mapping["controller"] = {"name": ""}
check(cfg2.controller_label is None, "4д: пустое имя — None")


# 4е. «Нет данных» декодера (65535) — словами
async def _periods_na(r, e, p, t0, t1, addrs=None):
    return [{"addr": 43562, "state_start": STOP, "value": 65535, "label": "65535"}]


dba.get_equipment = _eq
src.get_enum_periods = _periods_na
c = asyncio.run(ig._context("6003790403", "pcc", 1, STOP))
check(c.get("states") and c["states"][0]["label"] == "нет данных", f"4е: NA: {c.get('states')}")

# 5. Системный промпт: «Коротко», внешние источники — ориентир, без verdict
sp = TASKS["incident"][2]
check("## Коротко" in sp and sp.index("## Коротко") < sp.index("## Условия перед аварией"),
      "5: «Коротко» — первым разделом")
check("[Wn]" in sp and "не данные этой машины" in sp and "verdict" not in sp, "5б: правило внешних источников")

if _errors:
    print(f"FAIL — {len(_errors)} расхождений:")
    for x in _errors:
        print(f"  • {x}")
    sys.exit(1)
print("Все проверки пройдены.")
