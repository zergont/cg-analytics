# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""Тест догрузки контекста акта и данных лент в онлайн-движке (v4.9.94–96).

`_load_incident_context` расширяет окно акта назад до последнего нормального
останова. С v4.9.79 по v4.9.93 модуль-источник в ней не был импортирован:
каждый вызов падал на NameError, исключение глушилось, и акт строился на
обычном окне движка — без преамбулы, то есть без того самого цикла от
нормального останова, ради которого функция написана.

Поиск якоря обязан читать тип последней неисправности (40013), а не один
RUN_STATE. Иначе прошлая авария без бита в масках (1452) сходит за
нормальный останов, и акт обрезает серию попыток на ней.

С v4.9.96 лента — всё, что прислала панель (каталог регистров БД, не KB):
регистры, которых нет в KB, и информационные битовые события. Классификация
(якорь, вердикт) при этом по-прежнему по маскам KB: информационный бит с
тяжестью из каталога не должен делать штатный останов аварийным.

Источник подменён фейком — БД не нужна.

Запуск:  py -3 scripts/test_incident_context.py
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analytics import source as asrc  # noqa: E402
from analytics.classifier import build_stop_incident  # noqa: E402
from analytics.config import AnalyticsConfig  # noqa: E402
from analytics.reconstructor import build_chronology  # noqa: E402
from online.engine import (  # noqa: E402
    _ACT_LOAD_MARGIN_SEC, _load_incident_context, _timeline_for,
)

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


# Регистр, которого нет в KB: в ленту он должен попасть с именем из каталога
NOISY = 40700
INFO_ADDR = 40450          # информационные битовые события (40440+), не маски

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
    {"addr": NOISY, "value": 1, "state_start": T(20, 27), "state_end": None,
     "label": "Вкл", "name_ru": "Вентилятор радиатора"},
]
FAULTS = [
    # Информационный бит, которому каталог приписал «shutdown», — поднят ровно
    # на штатном останове. Попади он в классификацию, штатный останов стал бы
    # аварийным, и якорь уехал бы в фолбэк.
    {"addr": INFO_ADDR, "bit": 2, "fault_start": NORMAL_STOP,
     "fault_end": NORMAL_STOP + timedelta(minutes=1), "severity": "shutdown",
     "fault_name_ru": "Событие: аварийный останов (индикатор)"},
    # Информационный бит, висящий на момент второй аварии
    {"addr": INFO_ADDR, "bit": 5, "fault_start": T(20, 40), "fault_end": None,
     "severity": None, "fault_name_ru": "Не в автомате"},
]

calls: list[dict] = []
fault_calls: list[dict] = []


async def fake_catalog(equip_type):
    return sorted({40010, 40011, 40012, 40013, NOISY, INFO_ADDR}
                  | set(range(40400, 40429)))


async def fake_enum(router_sn, equip_type, panel_id, ts_from, ts_to, addrs=None):
    calls.append({"ts_from": ts_from, "addrs": list(addrs or [])})
    return [p for p in ENUM
            if p["addr"] in (addrs or [])
            and p["state_start"] < ts_to
            and (p["state_end"] is None or p["state_end"] > ts_from)]


async def fake_faults(router_sn, equip_type, panel_id, ts_from, ts_to, fault_addrs=None):
    fault_calls.append({"ts_from": ts_from, "addrs": set(fault_addrs or [])})
    return [f for f in FAULTS
            if f["addr"] in (fault_addrs or ())
            and f["fault_start"] < ts_to
            and (f["fault_end"] is None or f["fault_end"] > ts_from)]


def main() -> int:
    global ENUM
    cfg = AnalyticsConfig.from_pair(Path("knowledge_base"), "pcc3300", "cummins_kta50")
    asrc.get_enum_periods = fake_enum
    asrc.get_fault_periods = fake_faults
    asrc.get_catalog_addrs = fake_catalog
    check(INFO_ADDR not in cfg.whitelist_fault and NOISY not in cfg.register_map,
          "0: тестовые адреса оказались в KB — тест ничего не проверит")

    ctx = asyncio.run(_load_incident_context(
        "6003790403", "pcc3300", 1, E2, E2 + timedelta(minutes=5), cfg,
    ))

    # 1. Функция вообще отрабатывает (до v4.9.94 — NameError → None)
    check(ctx is not None and len(ctx) == 3,
          "1: догрузка контекста вернула не тройку — источник не импортирован?")

    # 2. Поиск якоря читает 40013 вместе с RUN_STATE
    check(bool(calls) and 40013 in calls[0]["addrs"] and 40011 in calls[0]["addrs"],
          f"2: поиск якоря без 40013: {calls[0]['addrs'] if calls else 'вызовов нет'}")

    # 3. Якорь — штатный останов, а не прошлая 1452 и не её хвост; и не
    #    сдвинут информационным битом с тяжестью «shutdown» из каталога
    want = NORMAL_STOP - timedelta(seconds=_ACT_LOAD_MARGIN_SEC)
    check(len(calls) >= 2 and calls[1]["ts_from"] == want,
          f"3: полная загрузка от {calls[1]['ts_from'] if len(calls) >= 2 else '—'}, "
          f"ожидали {want} (якорь на штатном останове)")

    if ctx and len(ctx) == 3:
        ep, fp, tlf = ctx
        # 4. Вся серия в контексте — и регистр, которого нет в KB
        check(any(p["addr"] == 40013 and p["state_start"] == E1 for p in ep),
              "4a: первая авария серии не попала в контекст акта")
        check(any(p["addr"] == NOISY for p in ep),
              "4б: регистр не из KB не попал в ленту — лента всё ещё по KB")
        # 5. Классификация по маскам KB, лента — по всем битовым регистрам
        check(not any(f["addr"] == INFO_ADDR for f in fp),
              "5a: информационное событие попало в маски классификации")
        check(sum(1 for f in tlf if f["addr"] == INFO_ADDR) == 2,
              "5б: информационные события не попали в ленту")
        check(len(fault_calls) == 2 and INFO_ADDR in fault_calls[1]["addrs"],
              f"5в: лента читала маски не по каталогу: {fault_calls}")

        # 6. Акт: окно от штатного останова, событие и «висело» — из ленты
        inc = build_stop_incident(ep, fp, E2, None, cfg, stop_kind="EMERGENCY",
                                  timeline_faults=tlf)
        check(inc is not None and inc["window"]["from"] == NORMAL_STOP.isoformat(),
              f"6a: окно акта {inc and inc['window']['from']}, ожидали {NORMAL_STOP}")
        if inc:
            names = {e.get("name") for e in inc["chronology"]}
            check("Вентилятор радиатора" in names,
                  f"6б: имя регистра не из KB не взято из каталога: {names}")
            check("Не в автомате" in {s["name"] for s in inc["standing"]},
                  "6в: висящее информационное событие не попало в срез «висело»")
            sev = {g["name"]: g for g in inc["summary"]}
            check("Событие: аварийный останов (индикатор)" in sev,
                  "6г: информационное событие пропало из свода")

    # 7. Якорь раньше горизонта: маски для классификации — из ленты от якоря,
    #    без третьего запроса; информационные события в них не попадают
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
    check(ctx is not None, "7a: догрузка не отработала")
    check(len(calls) >= 2 and calls[1]["ts_from"] == want,
          f"7б: лента от {calls[1]['ts_from'] if len(calls) >= 2 else '—'}, ожидали {want}")
    check(len(fault_calls) == 2 and fault_calls[1]["ts_from"] == want,
          f"7в: маски не взяты от якоря: {[c['ts_from'] for c in fault_calls]}")
    if ctx:
        check(not any(f["addr"] == INFO_ADDR for f in ctx[1]),
              "7г: информационное событие попало в маски классификации")

    # 8. Данные лент окна: у работающей машины не догружаются, у стоянки — да
    calls.clear()
    fault_calls.clear()
    kb_e, kb_f = [rs(3, T(20, 0), None)], []
    got = asyncio.run(_timeline_for(
        [SimpleNamespace(run_state=3)], "r", "pcc3300", 1, T(20, 0), T(21, 0),
        cfg, kb_e, kb_f))
    check(got == (kb_e, kb_f) and not calls and not fault_calls,
          "8a: у работающей машины лента догружалась")
    got = asyncio.run(_timeline_for(
        [SimpleNamespace(run_state=3), SimpleNamespace(run_state=0)],
        "r", "pcc3300", 1, T(20, 0), T(21, 0), cfg, kb_e, kb_f))
    check(len(calls) == 1 and NOISY in calls[0]["addrs"],
          "8б: у стоянки лента не догружена по каталогу")

    # 9. Имя регистра не из KB — из каталога; из KB — по-прежнему из KB
    ch = build_chronology(
        [{"addr": NOISY, "value": 1, "state_start": T(20, 0), "state_end": None,
          "label": "Вкл", "name_ru": "Вентилятор радиатора"},
         {"addr": 40011, "value": 0, "state_start": T(20, 1), "state_end": None,
          "label": "Стоп", "name_ru": "Run State"}], [], cfg)
    by = {e["addr"]: e["name"] for e in ch}
    check(by.get(NOISY) == "Вентилятор радиатора", f"9a: {by.get(NOISY)!r}")
    check(by.get(40011) == cfg.register_map[40011].get("description"),
          f"9б: имя регистра из KB заменено каталогом: {by.get(40011)!r}")

    # 10. Статус движка несёт вид стопа и считает аварию, которую видно
    #     только по 40013 (детекций нет), аварией — по обоим гейт
    #     предупреждений её пропускает (v4.9.95)
    from online.status_assembler import build_structural_status
    st = build_structural_status({"run_state": 0,
                                  "characteristics_json": {"stop_kind": "EMERGENCY"},
                                  "active_detections_json": []})
    check(st.get("stop_kind") == "EMERGENCY" and st.get("panel_severity") == "авария",
          f"10: статус аварии без детекций: {st.get('stop_kind')!r}/{st.get('panel_severity')!r}")

    # 11. Промпт разбора: свод без обрезки и расчётные параметры цикла
    from online.incident_gate import build_incident_prompt
    summary = [{"kind": "state", "name": f"Вид {i}", "count": 1,
                "first": T(20, 0).isoformat(), "last": T(20, 0).isoformat()}
               for i in range(30)]
    seg_run = {"run_state": 3, "t_start": T(20, 0), "t_end": E1,
               "characteristics_json": {"subsegments": [
                   {"characteristics": {"COOLANT_TEMP": {"min": 70, "max": 88,
                                                         "value_end": 86, "unit": "°C"}}},
                   {"characteristics": {"COOLANT_TEMP": {"min": 80, "max": 91.5,
                                                         "value_end": 90, "unit": "°C"}}},
               ]}}
    # Стоянка с остывшей ОЖ и код неисправности в характеристиках: ни то,
    # ни другое не должно попасть в параметры работы
    seg_stop = {"run_state": 0, "t_start": NORMAL_STOP, "t_end": T(20, 0),
                "characteristics_json": (
                    '{"stop_kind": "SIMPLE", "subsegments": [{"characteristics": '
                    '{"COOLANT_TEMP": {"min": 15, "max": 40, "value_end": 20, "unit": "°C"},'
                    ' "LAST_FAULT_CODE": {"min": 0, "max": 1452, "value_end": 1452}}}]}')}
    seg_prev_trip = {"run_state": 0, "t_start": E1, "t_end": RESTART,
                     "characteristics_json": {"stop_kind": "EMERGENCY", "subsegments": [
                         {"characteristics": {"COOLANT_TEMP": {
                             "min": 85, "max": 104, "value_end": 95, "unit": "°C"}}}]}}
    seg_start = {"run_state": 1, "t_start": RESTART, "t_end": E2,
                 "characteristics_json": {"subsegments": [{"characteristics": {
                     "COOLANT_TEMP": {"min": 60, "max": 62, "value_end": 61, "unit": "°C"}}}]}}
    summary.append({"kind": "fault", "name": "Датчик цилиндра 4", "count": 2,
                    "severity": None, "first": T(20, 1).isoformat(),
                    "last": T(20, 2).isoformat()})
    pr = build_incident_prompt(
        {"stop_ts": E2.isoformat(), "summary": summary, "events_total": 32},
        None, [seg_stop, seg_run, seg_prev_trip, seg_start])
    check("Вид 29" in pr and "ещё" not in pr, "11a: свод видов обрезан")
    check("Датчик цилиндра 4 [событие]" in pr,
          "11a2: бит без тяжести подписан не «событие»")
    check("Ход цикла" in pr and "Работа:" in pr and "Стоп (аварийный):" in pr,
          "11б: нет хода цикла или вида стопа")
    check("Параметры за цикл — Работа" in pr and "COOLANT_TEMP: 70 / 91.5 °C" in pr,
          f"11в: параметры работы смешаны со стоянкой: {pr[-900:]!r}")
    check("COOLANT_TEMP: 15 / 40 °C" in pr and "COOLANT_TEMP: 85 / 104 °C" in pr,
          "11г: стоянки не выделены своим режимом")
    check("LAST_FAULT_CODE" not in pr, "11д: код неисправности выдан за параметр")
    check("Сегмент, из которого машина остановилась — Задержка пуска" in pr
          and "COOLANT_TEMP: 60 / 62 / 61 °C" in pr,
          "11е: не показан сегмент, из которого машина остановилась")
    check("Последний рабочий сегмент цикла — Работа" in pr
          and "COOLANT_TEMP: 70 / 91.5 / 90 °C" in pr,
          "11ж: не показан последний рабочий сегмент")

    # 12. Лента — надмножество KB даже при неполном каталоге; вход нарезки
    #     режется из неё без второго чтения
    async def tiny_catalog(equip_type):
        return [NOISY]
    asrc.get_catalog_addrs = tiny_catalog
    calls.clear()
    fault_calls.clear()
    asyncio.run(asrc.get_timeline_periods("r", "pcc3300", 1, T(17, 0), T(21, 0), cfg))
    kb_e = set(asrc.enum_read_addrs(cfg))
    check(calls and kb_e <= set(calls[0]["addrs"]) and NOISY in calls[0]["addrs"],
          "12a: лента не покрывает регистры KB при неполном каталоге")
    check(fault_calls and set(cfg.whitelist_fault) <= fault_calls[0]["addrs"],
          "12б: лента не покрывает маски KB при неполном каталоге")
    ENUM = [rs(0, T(20, 0), None), {"addr": NOISY, "value": 1, "state_start": T(20, 1),
                                    "state_end": None, "label": "Вкл"}]
    e_kb, f_kb = asrc.kb_slice(ENUM, FAULTS + [{"addr": 40404, "bit": 3,
                                                "fault_start": T(20, 0),
                                                "fault_end": None}], cfg)
    check([p["addr"] for p in e_kb] == [40011] and [f["addr"] for f in f_kb] == [40404],
          f"12в: kb_slice: {[p['addr'] for p in e_kb]} / {[f['addr'] for f in f_kb]}")

    if _errors:
        print(f"FAIL — {len(_errors)} расхождений:")
        for e in _errors:
            print(f"  • {e}")
        return 1
    print("Все проверки пройдены.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
