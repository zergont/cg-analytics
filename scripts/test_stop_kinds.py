# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""Тест классификации СТОП-периодов на два вида (v4.9.68).

Проверяет `_classify_stop_periods`: стоп аварийный, если в момент его начала
активна маска аварийной тяжести; рез ровно один — по снятию последней такой
маски; авария посреди простого стопа вид не меняет и не режет.

Сценарии A и B воспроизводят реальные цепочки с прода 16–17.09.2026:
  A — авария по температуре ОЖ, машина 1126246373 (сегменты 35342/35344);
  B — кнопка E-Stop посреди стоянки, где старая пара сплиттеров давала пять
      кусков, включая вырожденный в четыре секунды.

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
    """Минимальный конфиг: нужна только шкала severity масок."""

    _MAP = {"shutdown": "SHUTDOWN", "shutdown_cooldown": "SHUTDOWN",
            "derate": "WARNING", "warning": "WARNING", "none": "INFO"}

    def bitmap_severity(self, raw: str | None) -> str:
        return self._MAP.get(raw or "none", "WARNING")


CFG = _Cfg()
TT = T(23, 0)


def rs(value: int, start: datetime, end: datetime | None = None) -> dict:
    return {"addr": 40011, "value": value, "state_start": start, "state_end": end}


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

    if _errors:
        print(f"FAIL — {len(_errors)} расхождений:")
        for e in _errors:
            print(f"  • {e}")
        return 1
    print("OK — восемь сценариев классификации стопа пройдены")
    return 0


if __name__ == "__main__":
    sys.exit(main())
