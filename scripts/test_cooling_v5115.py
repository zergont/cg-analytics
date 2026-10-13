# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""Отказ охлаждения v5.1.15 (вариант Г): тренда нет, близость к уставке — всегда.

Датчик ОЖ шагает по 1 °C и пишет по изменению: наклон по подсегменту считался по
2–3 показаниям, качание термостата 84→85 за 80 с давало «устойчивый тренд за
469 с». Близость к уставке HCT молчала при высоком наклоне, у ДГУ №2 (77–79 °C)
не проверялась вовсе, на коротких подсегментах пропускалась.

Запуск:  py -3 scripts/test_cooling_v5115.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analytics.config import AnalyticsConfig  # noqa: E402
from analytics.contract import DerivedMetrics  # noqa: E402
from analytics.detectors import _detect_cooling_failure  # noqa: E402

_errors: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        _errors.append(msg)


KB = ROOT / "knowledge_base"
QSX = AnalyticsConfig.from_pair(KB, "pcc3300", "cummins_qsx15")
KTA = AnalyticsConfig.from_pair(KB, "pcc3300", "cummins_kta50")
T0 = datetime(2026, 10, 11, 11, 7, 37, tzinfo=timezone.utc)


def run(cfg, start, end, mx, slope=None, sec=600, asym=None, rs=3):
    chars = {"COOLANT_TEMP": {"value_start": start, "value_end": end, "max": mx, "slope": slope}}
    return _detect_cooling_failure(chars, DerivedMetrics(coolant_asymptote_c=asym), rs, T0,
                                   T0 + timedelta(seconds=sec), cfg)


def keys(dets):
    return [d.description_key for d in dets]


# 1. «Устойчивый тренд за 469 с» (35771): качание 84→85 — не тревога
check(not run(QSX, 84, 85, 85, slope=0.0125, sec=469), "1: качание термостата ДЭС №3 — не тревога")
check(not run(KTA, 80, 81, 81, slope=0.0345, sec=900), "1б: качание 80↔81 ДГУ №1 — не тревога")

# 2. Высокий наклон и 90 °C — близость (раньше молчала: ветка тренда)
d = run(QSX, 83, 90, 90, slope=0.013, sec=1200)
check(keys(d) == ["COOLING_FAILURE.approaching_hct_limit"] and "ОЖ дошла до 90 °C" in d[0].trigger
      and d[0].values["coolant_temp_max"] == 90, f"2: близость при высоком наклоне: {keys(d)} {d and d[0].trigger}")
check(all("тренд" not in x.trigger for x in d), "2б: текста «тренд» нет")

# 3. ДГУ №2: рабочая полоса 77–79 — близость проверяется (раньше вход «≥ 80 на начало»)
check(keys(run(KTA, 78, 89, 89, slope=0.004)) == ["COOLING_FAILURE.approaching_hct_limit"],
      "3: близость у машины с низкой рабочей полосой")

# 4. Короткий подсегмент (4 мин 39 с) с 90 °C — близость (14.08 09:14)
check(keys(run(QSX, 88, 90, 90, slope=0.01, sec=279)) == ["COOLING_FAILURE.approaching_hct_limit"],
      "4: уровень — факт и на коротком подсегменте")

# 5. Граница: 88 — нет, 89 — да (уставка 97,8, 10 % → 88,02)
check(not run(QSX, 86, 88, 88) and run(QSX, 86, 89, 89), "5: порог близости 88,02 °C")

# 6. Прогрев: вход в рабочую полосу с асимптотой выше нормы; при 89+ — близость, не прогрев
check(keys(run(KTA, 72, 82, 82, asym=98.0, sec=1800)) == ["COOLING_FAILURE.warmup_asymptote_above_norm"],
      "6: прогрев с асимптотой выше нормы — как раньше")
check(not run(KTA, 72, 82, 82, asym=98.0, sec=240), "6б: прогрев на коротком подсегменте — нет")
check(keys(run(KTA, 70, 90, 90, asym=98.0, sec=1800)) == ["COOLING_FAILURE.approaching_hct_limit"],
      "6в: в прогреве дошла до 90 — близость (раньше прогрев обрывал проверку)")

# 7. Не в работе — молчим; тренда нет никогда
check(not run(QSX, 83, 95, 95, rs=0), "7: не RUN — нет")
check(not run(QSX, 82, 88, 88, slope=0.05, sec=3600), "7б: любой наклон без уровня — нет")

# 8. KB: ключей тренда больше нет
for c, name in ((QSX, "QSX15"), (KTA, "KTA50")):
    cf = c.detectors.get("COOLING_FAILURE") or {}
    check(not any(k.startswith("cooling_slope") for k in cf), f"8: {name} — без ключей наклона: {list(cf)}")

# 9. Ревью: старое снятие ложного тренда не глушит близость — новая версия правил
import online.status_assembler as sa  # noqa: E402
check(sa.GATE_RULES_VERSION == "5.1.15", f"9: версия правил гейта: {sa.GATE_RULES_VERSION}")
main_src = (ROOT / "main.py").read_text(encoding="utf-8")
check('get_app_setting("gate_rules_version", "") != _GRV' in main_src,
      "9б: разовый сброс снятий при смене версии — по константе")

# 10. Засев при закрытии: дубль к живому эпизоду (онсет на подсегмент раньше) не
#     создаётся; конец засеянного — последний подсегмент с детекцией, не конец сегмента
import asyncio  # noqa: E402
from types import SimpleNamespace as NS  # noqa: E402
from analytics.contract import Detection  # noqa: E402
import online.db as odb  # noqa: E402
import online.engine as eng  # noqa: E402

S0 = datetime(2026, 10, 13, 6, 0, tzinfo=timezone.utc)


def det(t):
    return Detection(scenario="COOLING_FAILURE", severity="CAUTION", t_detected=t.isoformat(),
                     source="PASSPORT_THRESHOLD", trigger="ОЖ дошла до 89 °C", related_roles=["COOLANT_TEMP"],
                     fault_codes=[144], description_key="COOLING_FAILURE.approaching_hct_limit",
                     values={"coolant_temp_max": 89})


B0, C0, C1, D1, E1 = (S0 + timedelta(minutes=m) for m in (0, 10, 13, 20, 60))
SEG = NS(run_state=3, subsegments=[
    NS(t_end=C0.isoformat(), detections=[]),                 # B: 88 — без детекции в итоговом
    NS(t_end=C1.isoformat(), detections=[det(C0)]),          # C: 89
    NS(t_end=D1.isoformat(), detections=[]),                 # D: 86
    NS(t_end=E1.isoformat(), detections=[det(D1)]),          # E: снова 89
])
INS: list = []
OV: dict = {"eps": []}


async def _onsets(*a, **k):
    return set()


async def _ov(r, e, p, t0, t1):
    OV["asked"] = (t0, t1)
    return OV["eps"]


async def _ins(*a, **k):
    INS.append(k)


async def _cnt(*a, **k):
    return {}


odb.existing_episode_onsets = _onsets
odb.get_episodes_overlapping = _ov
odb.insert_closed_episode = _ins
odb.count_episodes_batch = _cnt
SEG_END = S0 + timedelta(hours=24)

OV["eps"] = [{"scenario": "COOLING_FAILURE", "t_open": B0, "t_close": C0 + timedelta(minutes=2)}]
asyncio.run(eng._collect_and_enrich_detections(SEG, "SN", "pcc", 1, SEG_END, QSX, S0, open_keys=set()))
check(not INS and OV.get("asked") == (C0, C1),
      f"10: живой эпизод с онсетом b0 пересекает первый участок — дубль не засеян: {INS}, {OV.get('asked')}")
OV["eps"] = []
asyncio.run(eng._collect_and_enrich_detections(SEG, "SN", "pcc", 1, SEG_END, QSX, S0, open_keys=set()))
check(len(INS) == 1 and INS[0]["t_open"] == C0 and INS[0]["t_close"] == E1 and INS[0]["active_sec"] == 50 * 60,
      f"10б: засеянный — до последнего подсегмента с детекцией, не до конца суток: {INS}")

if _errors:
    print(f"FAIL — {len(_errors)} расхождений:")
    for x in _errors:
        print(f"  • {x}")
    sys.exit(1)
print("Все проверки пройдены.")
