# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""Тест отбора живых тревог на резе сегмента (v4.9.72).

`_live_alert_keys` отвечает на вопрос «переживёт ли тревога закрытие окна».
Если да — сеять по ней закрытый «эфемерный» эпизод нельзя: движок заведёт по
ней живой, и одна непрерывная тревога превратится в два эпизода. Память
движка на это не отвечает: она отстаёт на цикл, а тревога, поднявшая смену
RUN_STATE, появляется в том же цикле, в котором сегмент и режется.

Запуск:  py -3 scripts/test_live_alert_keys.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from online.engine import _live_alert_keys  # noqa: E402

_errors: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        _errors.append(msg)


class _Det:
    def __init__(self, scenario: str, addr=None, bit=None, fault_end=None):
        self._d = {
            "scenario": scenario,
            "severity": "SHUTDOWN",
            "values": {"addr": addr, "bit": bit, "fault_end": fault_end},
        }

    def to_dict(self) -> dict:
        return dict(self._d)


def sub(dets: list[_Det]) -> SimpleNamespace:
    return SimpleNamespace(
        t_start="2026-09-16T21:00:00+00:00",
        t_end="2026-09-16T21:35:50+00:00",
        detections=dets,
        characteristics={},
        risk_accumulators=SimpleNamespace(
            coking_risk=SimpleNamespace(to_dict=lambda: {})
        ),
    )


def seg(subs: list) -> SimpleNamespace:
    return SimpleNamespace(subsegments=subs, run_state=0, run_state_label="Стоп")


# 1. Висящая маска панели (fault_end пуст) — живая, сеять закрытый эпизод нельзя
keys = _live_alert_keys(seg([sub([_Det("CONTROLLER_FAULT", 40404, 3)])]))
check(keys == {"CONTROLLER_FAULT|40404|3"}, f"1: ожидали ключ маски, получили {keys}")

# 2. Снятая маска (fault_end проставлен) — уже не живая, её сеять как раз надо
keys = _live_alert_keys(seg([sub([
    _Det("CONTROLLER_FAULT", 40404, 3, fault_end="2026-09-16T21:37:52+00:00"),
])]))
check(keys == set(), f"2: снятая маска попала в живые: {keys}")

# 3. Разные биты — разные тревоги, ключи не схлопываются
keys = _live_alert_keys(seg([sub([
    _Det("CONTROLLER_FAULT", 40404, 3),
    _Det("CONTROLLER_FAULT", 40404, 7),
])]))
check(keys == {"CONTROLLER_FAULT|40404|3", "CONTROLLER_FAULT|40404|7"},
      f"3: биты схлопнулись: {keys}")

# 4. Аналитика ключуется сценарием
keys = _live_alert_keys(seg([sub([_Det("RPM_UNDERSPEED")])]))
check(keys == {"RPM_UNDERSPEED"}, f"4: ожидали ключ сценария, получили {keys}")

# 5. Берём только ПОСЛЕДНИЙ подсегмент: тревога из раннего уже не активна
keys = _live_alert_keys(seg([
    sub([_Det("CONTROLLER_FAULT", 40404, 3)]),
    sub([_Det("CONTROLLER_FAULT", 40428, 1)]),
]))
check(keys == {"CONTROLLER_FAULT|40428|1"},
      f"5: подтянулась тревога из раннего подсегмента: {keys}")

# 6. Сегмент без подсегментов не роняет цикл закрытия
check(_live_alert_keys(seg([])) == set(), "6: пустой сегмент дал непустой набор")

if _errors:
    print("ПРОВАЛЕНО:")
    for e in _errors:
        print("  -", e)
    sys.exit(1)
print("Все проверки пройдены.")
