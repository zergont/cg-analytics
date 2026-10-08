# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""Тест детекторов перекоса фазных токов и тока нейтрали (v4.9.97).

Повод — 29.09.2026, Сининда: перед сгоранием повышающего трансформатора фаза
L3 упала до ~10% от L1/L2, ток нейтрали вырос до ~1440 А, а I₂ остался
2–4% номинала — детектор обратной последовательности (и заводская защита
PCC3300) промолчали, автомат отключился через 2,5 часа.

Пороги из норм (KB): перекос ≥20% I_ном — ПТЭД 1993 п. 4.1.8; ток нейтрали
≥25% I_ном — ГОСТ 11677-85 п. 3.9.6, Stamford AGN 017; I₂ ≥6% — до
паспортного предела генератора 8% (Stamford AGN 016, IEC 60034-1).

Запуск:  py -3 scripts/test_phase_imbalance.py
"""
from __future__ import annotations

import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analytics.config import AnalyticsConfig  # noqa: E402
from analytics.contract import DerivedMetrics  # noqa: E402
from analytics.detectors import (  # noqa: E402
    _detect_negative_sequence, _detect_neutral_current, _detect_phase_current_spread,
)
from analytics.metrics import (  # noqa: E402
    _compute_neutral_current, _longest_run_at_or_above, _sustained_max, compute_derived_metrics,
)
from analytics.serializer import SCENARIO_RU  # noqa: E402
from corpus.preprocessor import _group_worst  # noqa: E402

_errors: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        _errors.append(msg)


KB = Path(__file__).resolve().parent.parent / "knowledge_base"
CFG = AnalyticsConfig.from_pair(KB, "pcc3300", "cummins_kta50")
T0 = datetime(2026, 9, 29, 12, 59, tzinfo=timezone.utc)

# Медианы ДГУ №1, 29.09 12:59–13:10 UTC (подсегмент «нормальная нагрузка»)
TRIP = {"I": (932.5, 905.0, 93.5), "P": (157, 206, 11), "Q": (140, -13, -18)}
# Обычная работа ДГУ №1 (27.09): фазы ровные
NORM = {"I": (646.0, 645.0, 710.0), "P": (136, 135, 152), "Q": (60, 58, 70)}


def by_addr(pattern: dict, seconds: int, step: int = 10,
            once: tuple[int, ...] = (), every: dict | None = None,
            no_packets: tuple[int, int] | None = None) -> dict:
    """Строки, как их пишет роутер: по пакетам связи (пинг 40290 каждые step с).

    once  — номера фаз (1..3), чьи ток, P и Q пришли один раз в начале
            (мёртвая фаза: значение не меняется — роутер его не повторяет);
    every — {роль: период, с} для редко присылаемых регистров;
    no_packets — (с, по) секунд без пакетов вовсе: потеря связи.
    """
    every = every or {}
    roles = {"CURRENT_L": "I", "ACTIVE_POWER_L": "P", "REACTIVE_POWER_L": "Q"}
    alive = [s for s in range(0, seconds, step)
             if not (no_packets and no_packets[0] <= s < no_packets[1])]
    out: dict[int, list[dict]] = {}
    for prefix, key in roles.items():
        for k in range(3):
            role = f"{prefix}{k + 1}"
            period = every.get(role, step)
            secs = [0] if (k + 1) in once else [s for s in alive if s % period == 0]
            out[CFG.role_to_addr(role)] = [
                {"ts": T0 + timedelta(seconds=s), "value": pattern[key][k],
                 "is_carried_forward": False} for s in secs]
    hb = int(CFG.seg("data_quality", "heartbeat_addr"))
    out[hb] = [{"ts": T0 + timedelta(seconds=s), "value": float(s), "is_carried_forward": False}
               for s in alive]
    kw = CFG.role_to_addr("RATING_KW")
    out[kw] = [{"ts": T0, "value": 1000.0, "is_carried_forward": False}]
    return out


# ── 1. Ток нейтрали ──────────────────────────────────────────────────────────
n = _compute_neutral_current([100, 100, 100], [60, 60, 60], [30, 30, 30])
check(n is not None and n < 1e-6, f"1a: симметричная система дала ток нейтрали {n}")
n = _compute_neutral_current(list(TRIP["I"]), list(TRIP["P"]), list(TRIP["Q"]))
check(n is not None and 1400 < n < 1480, f"1б: картина 29.09 — ожидали ≈1440 А, получили {n}")
n = _compute_neutral_current([500, 500, 0.0], [100, 100, 0], [50, 50, 0])
check(n is not None and n > 400, f"1в: нулевая фаза — законный провал, а не пропуск: {n}")
check(_compute_neutral_current([500, 500, 500], [100, 0, 100], [50, 0, 50]) is None,
      "1г: ток есть, а угла взять не из чего — замер должен пропускаться")

# ── 2. Непрерывное превышение ────────────────────────────────────────────────
S = [(T0 + timedelta(seconds=10 * i), v) for i, v in enumerate([5, 25, 30, 22, 10, 40, 41])]
check(_longest_run_at_or_above(S, 20, 90) == 30, "2a: длина непрерывного участка ≥ порога")
check(_longest_run_at_or_above(S, 50, 90) is None, "2б: выше порога нет — None")
gap = [(T0, 30), (T0 + timedelta(seconds=10), 30), (T0 + timedelta(seconds=300), 30),
       (T0 + timedelta(seconds=310), 30)]
check(_longest_run_at_or_above(gap, 20, 90) == 10, "2в: дыра в данных рвёт непрерывность")

# Устойчивый максимум: разовый пакет на скачке нагрузки (одна фаза новая,
# две старые — у ДЭС №3 так получалось 61%) в максимум не попадает
spike = [(T0 + timedelta(seconds=10 * i), v) for i, v in enumerate([3, 4, 61, 5, 4])]
check(_sustained_max(spike, 90) == 5, f"2г: разовый всплеск попал в максимум: {_sustained_max(spike, 90)}")
held = [(T0 + timedelta(seconds=10 * i), v) for i, v in enumerate([3, 40, 45, 5])]
check(_sustained_max(held, 90) == 40, "2д: устойчивое превышение потеряно")

# ── 3. Метрики подсегмента ───────────────────────────────────────────────────
dm = compute_derived_metrics(by_addr(TRIP, 600), T0, T0 + timedelta(seconds=600), [], CFG)
check(dm.phase_spread_pct_max is not None and 45 < dm.phase_spread_pct_max < 48,
      f"3a: перекос 29.09 ≈46% I_ном, получили {dm.phase_spread_pct_max}")
check(dm.neutral_current_pct_max is not None and 78 < dm.neutral_current_pct_max < 82,
      f"3б: нейтраль 29.09 ≈80% I_ном, получили {dm.neutral_current_pct_max}")
check((dm.phase_spread_run_sec or 0) >= 580 and (dm.neutral_current_run_sec or 0) >= 580,
      f"3в: превышение на весь подсегмент: {dm.phase_spread_run_sec}/{dm.neutral_current_run_sec}")
check(dm.neg_seq_i2_pct_max is not None and dm.neg_seq_i2_pct_max < 6,
      f"3г: I₂ на картине 29.09 должен остаться ниже 6% — иначе тест не о том: {dm.neg_seq_i2_pct_max}")

dn = compute_derived_metrics(by_addr(NORM, 600), T0, T0 + timedelta(seconds=600), [], CFG)
check((dn.phase_spread_pct_max or 0) < 5 and (dn.neutral_current_pct_max or 0) < 10,
      f"3д: обычная работа — {dn.phase_spread_pct_max}% / {dn.neutral_current_pct_max}%")
check(dn.phase_spread_run_sec is None and dn.neutral_current_run_sec is None,
      "3е: в норме превышений быть не должно")

# Мёртвая фаза: ток, P и Q фазы L3 = 0 пришли один раз — роутер не повторяет
# неизменное значение. Остальные фазы — каждый пакет. Должно держаться.
DEAD = {"I": (930.0, 905.0, 0.0), "P": (157, 206, 0), "Q": (140, -13, 0)}
dd = compute_derived_metrics(by_addr(DEAD, 600, once=(3,)), T0, T0 + timedelta(seconds=600), [], CFG)
check((dd.phase_spread_run_sec or 0) >= 580 and (dd.neutral_current_run_sec or 0) >= 580,
      f"3ж: мёртвая фаза, присланная один раз, потеряна: {dd.phase_spread_run_sec}/{dd.neutral_current_run_sec}")

# Пофазная Q приходит раз в 200 с (так бывает в проде) — непрерывность не рвётся
dq = compute_derived_metrics(
    by_addr(TRIP, 600, every={f"REACTIVE_POWER_L{k}": 200 for k in (1, 2, 3)}),
    T0, T0 + timedelta(seconds=600), [], CFG)
check((dq.neutral_current_run_sec or 0) >= 580,
      f"3з: редкая Q порвала непрерывность нейтрали: {dq.neutral_current_run_sec}")

# Потеря связи на 5 минут посередине: удержание сбрасывается, непрерывность рвётся
dl = compute_derived_metrics(by_addr(DEAD, 900, once=(3,), no_packets=(200, 500)),
                             T0, T0 + timedelta(seconds=900), [], CFG)
check(dl.phase_spread_run_sec is not None and dl.phase_spread_run_sec < 300,
      f"3и: значение пережило потерю связи: {dl.phase_spread_run_sec}")
# То же через data_gaps (дыра записана внешним сервисом), пакеты при этом есть
gap = [{"gap_start": T0 + timedelta(seconds=200), "gap_end": T0 + timedelta(seconds=210)}]
dg = compute_derived_metrics(by_addr(DEAD, 600, once=(3,)), T0, T0 + timedelta(seconds=600), gap, CFG)
check(dg.phase_spread_run_sec is not None and dg.phase_spread_run_sec < 300,
      f"3к: удержание прошло через дыру data_gaps: {dg.phase_spread_run_sec}")

# ── 4. Детекторы ─────────────────────────────────────────────────────────────
det = _detect_phase_current_spread(dm, T0, CFG) + _detect_neutral_current(dm, T0, CFG)
names = {d.scenario for d in det}
check(names == {"PHASE_CURRENT_SPREAD", "NEUTRAL_CURRENT"}, f"4a: сработали {names}")
for d in det:
    check(d.severity == "CAUTION", f"4б: {d.scenario} — тяжесть {d.severity}, ожидали CAUTION")
    check(d.values.get("norm_ref") and d.values["norm_ref"] in d.trigger,
          f"4в: {d.scenario} — в тексте нет ссылки на норму: {d.trigger}")
spread = next((d for d in det if d.scenario == "PHASE_CURRENT_SPREAD"), None)
check(spread is not None and "ПТЭД 1993" in spread.trigger, "4г: перекос без ссылки на ПТЭД")
neutral = next((d for d in det if d.scenario == "NEUTRAL_CURRENT"), None)
check(neutral is not None and "ГОСТ 11677-85" in neutral.trigger, "4д: нейтраль без ссылки на ГОСТ")

check(not _detect_phase_current_spread(dn, T0, CFG) and not _detect_neutral_current(dn, T0, CFG),
      "4е: обычная работа дала срабатывание")
short = DerivedMetrics(phase_spread_pct_max=23.0, phase_spread_run_sec=17.0,
                       neutral_current_pct_max=31.0, neutral_current_run_sec=17.0)
check(not _detect_phase_current_spread(short, T0, CFG) and not _detect_neutral_current(short, T0, CFG),
      "4ж: всплеск 17 с (как у ДЭС №3) не должен срабатывать")

# ── 5. I₂: порог 6% из KB в обеих привязках, ссылка на паспорт генератора ────
for name, cfg in (("пара", CFG), ("legacy", AnalyticsConfig(KB / "equipment" / "cummins_kta50_pcc3300"))):
    check(float(cfg.det("NEGATIVE_SEQUENCE", "i2_proximity_warning_pct")) == 6.0,
          f"5a/{name}: порог I₂ не 6%")
    check(float(cfg.det("PHASE_CURRENT_SPREAD", "spread_warning_pct")) == 20.0
          and float(cfg.det("NEUTRAL_CURRENT", "neutral_warning_pct")) == 25.0,
          f"5б/{name}: пороги перекоса/нейтрали")
ns = _detect_negative_sequence(DerivedMetrics(neg_seq_i2_pct_max=7.0, neg_seq_i2_duration_sec=120.0), T0, CFG)
check(len(ns) == 1 and "Stamford" in ns[0].trigger, f"5в: I₂=7% должен сработать со ссылкой: {ns}")
check(not _detect_negative_sequence(DerivedMetrics(neg_seq_i2_pct_max=5.0, neg_seq_i2_duration_sec=120.0), T0, CFG),
      "5г: I₂=5% не должен срабатывать")

# ── 6. Названия и сводка корпуса ─────────────────────────────────────────────
check(SCENARIO_RU.get("PHASE_CURRENT_SPREAD") == "Перекос фазных токов"
      and SCENARIO_RU.get("NEUTRAL_CURRENT") == "Ток нейтрали", "6a: нет русских названий")
w = _group_worst("NEUTRAL_CURRENT", [d.values for d in det if d.scenario == "NEUTRAL_CURRENT"])
check(w and "при норме ≤25%" in w and "ГОСТ 11677-85" in w, f"6б: сводка корпуса: {w!r}")

if _errors:
    print(f"FAIL — {len(_errors)} расхождений:")
    for e in _errors:
        print(f"  • {e}")
    sys.exit(1)
print("Все проверки пройдены.")
