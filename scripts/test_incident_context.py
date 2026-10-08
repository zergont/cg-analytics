# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""Тест догрузки контекста акта в онлайн-движке (v4.9.94).

`_load_incident_context` расширяет окно акта назад до последнего нормального
останова. С v4.9.79 по v4.9.93 модуль-источник в ней не был импортирован:
каждый вызов падал на NameError, исключение глушилось, и акт строился на
обычном окне движка — без преамбулы, то есть без того самого цикла от
нормального останова, ради которого функция написана.

Второе: поиск якоря обязан читать тип последней неисправности (40013), а не
один RUN_STATE. Иначе прошлая авария без бита в масках (1452) сходит за
нормальный останов, и акт обрезает серию попыток на ней.

Источник подменён фейком — БД не нужна.

Запуск:  py -3 scripts/test_incident_context.py
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analytics import source as asrc  # noqa: E402
from analytics.config import AnalyticsConfig  # noqa: E402
from online.engine import _ACT_LOAD_MARGIN_SEC, _load_incident_context  # noqa: E402

_errors: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        _errors.append(msg)


def T(h: int, m: int, s: int = 0) -> datetime:
    return datetime(2026, 9, 17, h, m, s, tzinfo=timezone.utc)


def rs(value: int, start: datetime, end: datetime | None) -> dict:
    return {"addr": 40011, "value": value, "state_start": start,
            "state_end": end, "label": f"RS={value}"}


def ft(value: int, start: datetime, end: datetime | None) -> dict:
    return {"addr": 40013, "value": value, "state_start": start,
            "state_end": end, "label": f"FT={value}"}


# Вечер 17.09 на ДГУ №1: штатный останов, затем серия 1452 без бита в масках
NORMAL_STOP = T(18, 0)
E1, RESET_E1, RESTART = T(20, 28, 37), T(20, 32, 13), T(20, 36, 17)
E2 = T(20, 43, 32)
ENUM = [
    rs(3, T(17, 0), NORMAL_STOP),
    rs(0, NORMAL_STOP, T(20, 0)),
    rs(3, T(20, 0), E1),
    rs(0, E1, RESTART),
    rs(3, RESTART, E2),
    rs(0, E2, None),
    ft(0, T(17, 0), E1),
    ft(4, E1, RESET_E1),
    ft(0, RESET_E1, E2),
    ft(4, E2, None),
    {"addr": 40012, "value": 1452, "state_start": E1, "state_end": None,
     "label": "1452"},
]

calls: list[dict] = []


async def fake_enum(router_sn, equip_type, panel_id, ts_from, ts_to, addrs=None):
    calls.append({"ts_from": ts_from, "addrs": list(addrs or [])})
    return [p for p in ENUM
            if p["addr"] in (addrs or [])
            and p["state_start"] < ts_to
            and (p["state_end"] is None or p["state_end"] > ts_from)]


fault_calls: list[datetime] = []


async def fake_faults(router_sn, equip_type, panel_id, ts_from, ts_to, fault_addrs=None):
    fault_calls.append(ts_from)
    return []    # у 1452 нет бита в масках


def main() -> int:
    cfg = AnalyticsConfig.from_pair(Path("knowledge_base"), "pcc3300", "cummins_kta50")
    asrc.get_enum_periods = fake_enum
    asrc.get_fault_periods = fake_faults

    ctx = asyncio.run(_load_incident_context(
        "6003790403", "pcc3300", 1, E2, E2 + timedelta(minutes=5), cfg,
    ))

    # 1. Функция вообще отрабатывает (до v4.9.94 — NameError → None)
    check(ctx is not None, "1: догрузка контекста вернула None — источник не импортирован?")

    # 2. Поиск якоря читает 40013 вместе с RUN_STATE
    check(bool(calls) and 40013 in calls[0]["addrs"] and 40011 in calls[0]["addrs"],
          f"2: поиск якоря без 40013: {calls[0]['addrs'] if calls else 'вызовов нет'}")

    # 3. Якорь — штатный останов, а не прошлая 1452 и не её хвост
    if len(calls) >= 2:
        want = NORMAL_STOP - timedelta(seconds=_ACT_LOAD_MARGIN_SEC)
        check(calls[1]["ts_from"] == want,
              f"3: полная загрузка от {calls[1]['ts_from']}, ожидали {want} "
              f"(якорь на штатном останове)")
    else:
        check(False, "3: полной загрузки от якоря не было")

    # 4. В полную загрузку попала вся серия — первая 1452 и её код
    if ctx:
        ep, _ = ctx
        check(any(p["addr"] == 40013 and p["state_start"] == E1 for p in ep),
              "4: первая авария серии не попала в контекст акта")

    # 5. Обычный случай: якорь внутри горизонта — маски не перечитываются
    check(len(fault_calls) == 1,
          f"5: маски читались {len(fault_calls)} раз(а), ожидали один")

    # 6. Якорь раньше горизонта (резерв простоял больше месяца, пустили,
    #    упал): стоп-период начался до горизонта и закончился внутри. Маски
    #    перечитываются от того же места, что и enum, — покрытие совпадает.
    global ENUM
    trip = T(20, 43, 32)
    long_stop = trip - timedelta(days=60)
    ENUM = [
        rs(3, long_stop - timedelta(hours=1), long_stop),
        rs(0, long_stop, trip - timedelta(hours=1)),
        rs(3, trip - timedelta(hours=1), trip),
        rs(0, trip, None),
        ft(0, long_stop - timedelta(hours=1), trip),
        ft(4, trip, None),
    ]
    calls.clear()
    fault_calls.clear()
    ctx = asyncio.run(_load_incident_context(
        "6003790403", "pcc3300", 1, trip, trip + timedelta(minutes=5), cfg,
    ))
    want = long_stop - timedelta(seconds=_ACT_LOAD_MARGIN_SEC)
    check(ctx is not None, "6a: догрузка не отработала")
    check(len(calls) >= 2 and calls[1]["ts_from"] == want,
          f"6б: enum от {calls[1]['ts_from'] if len(calls) >= 2 else '—'}, ожидали {want}")
    check(len(fault_calls) == 2 and fault_calls[1] == want,
          f"6в: маски не перечитаны от якоря: {fault_calls}")

    # 7. Статус движка несёт вид стопа и считает аварию, которую видно
    #    только по 40013 (детекций нет), аварией — по обоим гейт
    #    предупреждений её пропускает (v4.9.95)
    from online.status_assembler import build_structural_status
    st = build_structural_status({"run_state": 0,
                                  "characteristics_json": {"stop_kind": "EMERGENCY"},
                                  "active_detections_json": []})
    check(st.get("stop_kind") == "EMERGENCY" and st.get("panel_severity") == "авария",
          f"7: статус аварии без детекций: {st.get('stop_kind')!r}/{st.get('panel_severity')!r}")

    if _errors:
        print(f"FAIL — {len(_errors)} расхождений:")
        for e in _errors:
            print(f"  • {e}")
        return 1
    print("Все проверки пройдены.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
