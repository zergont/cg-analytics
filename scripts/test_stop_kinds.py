# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""Тест классификации СТОП-периодов на два вида (v4.9.68).

Проверяет `_classify_stop_periods`: стоп аварийный, если в момент его начала
панель держит аварию — маской аварийной тяжести или типом последней
неисправности 40013 (v4.9.94); рез ровно один — по последнему снятию; авария
посреди простого стопа вид не меняет и не режет.

Сценарии A и B воспроизводят реальные цепочки с прода 16–17.09.2026:
  A — авария по температуре ОЖ, машина 1126246373 (сегменты 35342/35344);
  B — кнопка E-Stop посреди стоянки, где старая пара сплиттеров давала пять
      кусков, включая вырожденный в четыре секунды.
Сценарии I–R — второй признак: аварии с кодом 1452 на ДГУ №1, у которого
нет бита в масках (шесть пропусков из семнадцати за 45 суток), и тайминги
снятия маски / 40013 с прода.

Запуск:  py -3 scripts/test_stop_kinds.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analytics.segmenter import _classify_stop_periods  # noqa: E402

_errors: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        _errors.append(msg)


def T(h: int, m: int, s: int = 0) -> datetime:
    return datetime(2026, 9, 16, h, m, s, tzinfo=timezone.utc)


class _Cfg:
    """Минимальный конфиг: шкала severity масок и карта регистров.

    declare=False — контроллер не объявил аварийные значения типа
    неисправности: решают одни маски.
    """

    _MAP = {"shutdown": "SHUTDOWN", "shutdown_cooldown": "SHUTDOWN",
            "derate": "WARNING", "warning": "WARNING", "none": "INFO"}

    def __init__(self, declare: bool = True) -> None:
        meta = {"role": "LAST_FAULT_TYPE"}
        if declare:
            meta["shutdown_states"] = [3, 4]
        self.register_map = {40011: {"role": "RUN_STATE"}, 40013: meta}

    def bitmap_severity(self, raw: str | None) -> str:
        return self._MAP.get(raw or "none", "WARNING")

    def role_to_addr(self, role: str) -> int | None:
        return next((a for a, m in self.register_map.items() if m.get("role") == role), None)


CFG = _Cfg()
CFG_NO_DECL = _Cfg(declare=False)
TT = T(23, 0)


def rs(value: int, start: datetime, end: datetime | None = None) -> dict:
    return {"addr": 40011, "value": value, "state_start": start, "state_end": end}


def ft(value: int, start: datetime, end: datetime | None = None) -> dict:
    """Период 40013 — тип последней неисправности."""
    return {"addr": 40013, "value": value, "state_start": start, "state_end": end}


def classify(rs_periods: list[dict], faults: list[dict], types: list[dict],
             cfg=CFG) -> list[dict]:
    """Как в segment(): периоды RUN_STATE + все enum-периоды окна."""
    return _classify_stop_periods(rs_periods, faults, cfg, TT, rs_periods + types)


def fp(severity: str, start: datetime, end: datetime | None = None) -> dict:
    return {"severity": severity, "fault_start": start, "fault_end": end}


def stops(periods: list[dict]) -> list[dict]:
    return [p for p in periods if p["value"] == 0]


def main() -> int:
    # A. Авария в момент останова, снята посреди стопа → голова + хвост
    out = _classify_stop_periods(
        [rs(3, T(9, 0), T(10, 2, 20)),
         rs(0, T(10, 2, 20), T(10, 13, 43)),
         rs(3, T(10, 13, 43))],
        [fp("shutdown", T(10, 2, 20), T(10, 12, 50)),
         fp("warning", T(10, 2, 20))],          # горячий останов, залипший
        CFG, TT,
    )
    st = stops(out)
    check(len(st) == 2, f"A: кусков стопа {len(st)}, ожидалось 2")
    if len(st) == 2:
        check(st[0]["stop_kind"] == "EMERGENCY", "A: голова не EMERGENCY")
        check(st[0]["state_end"] == T(10, 12, 50), "A: рез не в момент снятия аварии")
        check(st[0]["cause_close"] == "SHUTDOWN_CLEARED", "A: причина закрытия головы")
        check(st[1]["stop_kind"] == "SIMPLE", "A: хвост не SIMPLE")
        check(st[1]["cause_open"] == "SHUTDOWN_CLEARED", "A: причина открытия хвоста")
        check(st[1]["state_end"] == T(10, 13, 43), "A: хвост не дотянут до пуска")
    check(len(out) == 4, f"A: всего периодов {len(out)}, ожидалось 4 (работа+2+работа)")

    # B. Кнопка посреди стоянки: вид не меняется, реза нет
    out = _classify_stop_periods(
        [rs(3, T(20, 40), T(20, 50, 52)),
         rs(0, T(20, 50, 52), T(21, 30, 45)),
         rs(3, T(21, 30, 45))],
        [fp("shutdown", T(21, 4, 38), T(21, 5, 39))],
        CFG, TT,
    )
    st = stops(out)
    check(len(st) == 1, f"B: кусков стопа {len(st)}, ожидался 1")
    if st:
        check(st[0]["stop_kind"] == "SIMPLE", "B: стоп стал аварийным от кнопки в середине")
        check(st[0]["state_end"] == T(21, 30, 45), "B: стоп разрезан")

    # C. Аварию не сняли до пуска → реза нет, весь стоп аварийный
    out = _classify_stop_periods(
        [rs(0, T(10, 0), T(10, 30))], [fp("shutdown", T(10, 0), T(10, 40))], CFG, TT)
    check(len(out) == 1 and out[0]["stop_kind"] == "EMERGENCY", "C: ожидался один аварийный кусок")
    check(out and out[0]["state_end"] == T(10, 30), "C: конец стопа сдвинут")

    # D. Открытый аварийный стоп не закрывается искусственно
    out = _classify_stop_periods([rs(0, T(10, 0))], [fp("shutdown", T(10, 0))], CFG, TT)
    check(len(out) == 1 and out[0]["state_end"] is None, "D: открытый стоп получил конец")
    check(out and out[0]["stop_kind"] == "EMERGENCY", "D: открытый стоп не аварийный")

    # E. Перекрывающиеся аварии → рез по снятию ПОСЛЕДНЕЙ
    out = _classify_stop_periods(
        [rs(0, T(10, 0), T(11, 0))],
        [fp("shutdown", T(10, 0), T(10, 20)), fp("shutdown", T(10, 15), T(10, 40))],
        CFG, TT,
    )
    st = stops(out)
    check(len(st) == 2 and st[0]["state_end"] == T(10, 40),
          "E: рез не по снятию последней аварии")

    # F. Предупреждение аварией не делает
    out = _classify_stop_periods(
        [rs(0, T(10, 0), T(11, 0))], [fp("warning", T(10, 0), T(10, 40))], CFG, TT)
    check(len(out) == 1 and out[0]["stop_kind"] == "SIMPLE", "F: warning сделал стоп аварийным")

    # G. Машина, стоящая в аварии с июля (ДГУ №2) — один аварийный кусок
    old = datetime(2026, 7, 6, 7, 47, 48, tzinfo=timezone.utc)
    out = _classify_stop_periods([rs(0, old)], [fp("shutdown", old)], CFG, TT)
    check(len(out) == 1 and out[0]["stop_kind"] == "EMERGENCY", "G: залипшая авария не распознана")

    # H. Не-стоповые периоды проходят насквозь без вида
    out = _classify_stop_periods([rs(3, T(10, 0), T(11, 0))], [fp("shutdown", T(10, 0))], CFG, TT)
    check(len(out) == 1 and out[0].get("stop_kind") is None, "H: рабочий период получил вид стопа")

    # ── Второй признак: тип последней неисправности (40013) ──────────────
    # Прод, ДГУ №1, 17.09 20:28:37: код 1452 «Отказ включения автомата»
    # роняет машину в Shutdown, а бита в масках у него нет. Сброс — через
    # 216 с, пуск — через 460 с.
    S1452 = T(20, 28, 37)
    RS_1452 = [rs(3, T(20, 0), S1452), rs(0, S1452, T(20, 36, 17)), rs(3, T(20, 36, 17))]

    # I. Авария без бита в масках: голова по 40013, рез на сбросе
    out = classify(RS_1452, [],
                   [ft(1, T(19, 0), S1452), ft(4, S1452, T(20, 32, 13)), ft(0, T(20, 32, 13))])
    st = stops(out)
    check(len(st) == 2, f"I: кусков стопа {len(st)}, ожидалось 2")
    if len(st) == 2:
        check(st[0]["stop_kind"] == "EMERGENCY", "I: авария без бита в масках не распознана")
        check(st[0]["state_end"] == T(20, 32, 13), "I: рез не на сбросе 40013")
        check(st[0]["cause_close"] == "SHUTDOWN_CLEARED", "I: причина закрытия головы")
        check(st[1]["stop_kind"] == "SIMPLE" and st[1]["cause_open"] == "SHUTDOWN_CLEARED",
              "I: хвост не простой стоп после снятия")

    # J. Без объявления в карте регистров 40013 не читается — прежнее поведение
    out = classify(RS_1452, [], [ft(4, S1452, T(20, 32, 13))], cfg=CFG_NO_DECL)
    st = stops(out)
    check(len(st) == 1 and st[0]["stop_kind"] == "SIMPLE",
          "J: 40013 сработал без объявления shutdown_states")

    # K. Без enum-периодов (старый вызов) решают одни маски
    out = _classify_stop_periods(RS_1452, [], CFG, TT)
    check(len(stops(out)) == 1 and stops(out)[0]["stop_kind"] == "SIMPLE",
          "K: вызов без enum_periods изменил поведение")

    # L. Маска держится дольше панели (14.09 09:21:39: 40013 снят на 33 с,
    #    маска — на 57 с): рез по тому, что снято ПОСЛЕДНИМ
    S = T(9, 21, 39)
    out = classify([rs(3, T(9, 0), S), rs(0, S, T(9, 22, 40)), rs(3, T(9, 22, 40))],
                   [fp("shutdown", S, T(9, 22, 36))],
                   [ft(4, S, T(9, 22, 12)), ft(0, T(9, 22, 12))])
    st = stops(out)
    check(len(st) == 2 and st[0]["state_end"] == T(9, 22, 36),
          "L: рез не по последнему снятию (маска позже 40013)")

    # M. Обратный порядок (ДГУ №1 до 19.09: маска на 61 с, 40013 на 65 с,
    #    пуск в том же кадре, что сброс) — реза нет, весь стоп аварийный
    S = T(7, 14, 8)
    out = classify([rs(3, T(7, 0), S), rs(0, S, T(7, 15, 13)), rs(3, T(7, 15, 13))],
                   [fp("shutdown", S, T(7, 15, 9))],
                   [ft(4, S, T(7, 15, 13)), ft(1, T(7, 15, 13))])
    st = stops(out)
    check(len(st) == 1 and st[0]["stop_kind"] == "EMERGENCY"
          and st[0]["state_end"] == T(7, 15, 13),
          "M: сброс в кадре пуска должен давать один аварийный кусок")

    # N. Warning в 40013 аварией не делает
    out = classify(RS_1452, [], [ft(1, S1452, T(20, 32, 13))])
    check(stops(out)[0]["stop_kind"] == "SIMPLE", "N: Warning в 40013 сделал стоп аварийным")

    # O. Shutdown посреди простого стопа (кнопка на стоящей машине) — вид
    #    не меняет и не режет, как и маска в сценарии B
    out = classify([rs(0, T(10, 0), T(11, 0))], [], [ft(4, T(10, 20), T(10, 30))])
    st = stops(out)
    check(len(st) == 1 and st[0]["stop_kind"] == "SIMPLE",
          "O: 40013 посреди простого стопа сменил вид или разрезал его")

    # P. Shutdown with Cooldown → Shutdown подряд: интервалы смыкаются,
    #    рез по концу второго
    out = classify([rs(0, T(10, 0), T(11, 0))], [],
                   [ft(3, T(10, 0), T(10, 5)), ft(4, T(10, 5), T(10, 12)), ft(0, T(10, 12))])
    st = stops(out)
    check(len(st) == 2 and st[0]["state_end"] == T(10, 12),
          "P: смежные 3→4 не слились в один интервал")

    # Q. Незакрытый Shutdown держит открытый стоп аварийным
    out = classify([rs(0, T(10, 0))], [], [ft(4, T(10, 0))])
    check(len(out) == 1 and out[0]["stop_kind"] == "EMERGENCY" and out[0]["state_end"] is None,
          "Q: открытый стоп с незакрытым 40013=4 не аварийный")

    # R. Значение строкой (как может прийти из БД) — тоже распознаётся
    out = classify(RS_1452, [], [{**ft(4, S1452, T(20, 32, 13)), "value": "4"}])
    check(stops(out)[0]["stop_kind"] == "EMERGENCY", "R: 40013='4' строкой не распознан")

    # S. Сквозь segment() на настоящих конфигах обеих привязок: 40013 должен
    #    дойти до нарезки из enum-периодов окна, а объявление — из карты
    #    регистров. Без этого сценария потеря аргумента в segment() молча
    #    вернула бы одни маски, и все тесты выше остались бы зелёными.
    from analytics.config import AnalyticsConfig
    from analytics.segmenter import segment

    kb = Path(__file__).resolve().parent.parent / "knowledge_base"
    enum_1452 = RS_1452 + [ft(1, T(19, 0), S1452), ft(4, S1452, T(20, 32, 13)),
                           ft(0, T(20, 32, 13))]
    for name, cfg_real in (
        ("пара", AnalyticsConfig.from_pair(kb, "pcc3300", "cummins_kta50")),
        ("legacy", AnalyticsConfig(kb / "equipment" / "cummins_kta50_pcc3300")),
    ):
        segs = segment(enum_1452, [], [], [], cfg_real, "r", "pcc3300", 1, "e",
                       T(19, 30), T(21, 0))
        st = [(s.stop_kind, s.t_start, s.t_end, s.cause_close)
              for s in segs if s.run_state == 0]
        want = [("EMERGENCY", S1452.isoformat(), T(20, 32, 13).isoformat(),
                 "SHUTDOWN_CLEARED"),
                ("SIMPLE", T(20, 32, 13).isoformat(), T(20, 36, 17).isoformat())]
        got = [x[:4] if x[0] == "EMERGENCY" else x[:3] for x in st]
        check(got == want, f"S/{name}: segment() нарезал {st}, ожидали {want}")

    if _errors:
        print(f"FAIL — {len(_errors)} расхождений:")
        for e in _errors:
            print(f"  • {e}")
        return 1
    print("OK — девятнадцать сценариев классификации стопа пройдены")
    return 0


if __name__ == "__main__":
    sys.exit(main())
