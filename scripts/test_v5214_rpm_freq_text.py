# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""v5.2.14: честная подача расхождения оборотов и частоты, без вердикта
«сбой датчика» (решение владельца 16.11.2026, вариант «а»).

Повод — Сининда-1, 14.11.2026 17:05:07: ДГУ №1 и ДГУ №2 в параллели на общей
шине, частота у обеих 49,4 Гц (k·f = 1482 об/мин), датчик оборотов ДГУ №1 —
1344. Эпизод записан с rpm_min 1344, drop_pct 11,1 и drop_rpm 167 (размах от
максимума, а не глубина), гейт прочёл «11 % просадки», а «(по частоте ≈ 1482)» —
как «1482 Гц».

Раздел 1 — кейс Сининды через segment(): v_min 1482, «на 3 ниже порога»,
признак расхождения, частота в Гц только у частоты, без «сбой датчика».
Раздел 2 — без расхождения (1470 при 49,0 Гц → v_min 1470).
Раздел 3 — без частоты: по участкам и по подсегменту (live=None) — по оборотам.
Раздел 4 — dip_view: v_min и расходящийся отсчёт — разные отсчёты, правило B,
допуск сверки; v_min кластера (analytics/live.py).
Раздел 5 — строка проверки source_rpm_glitch без «сбой датчика».
Раздел 6 — потребители: худшее в отчёте и во входе заключения, сервер
телеметрии (episodes), хеши составов гейта и GATE_RULES_VERSION прежние.
Раздел 7 — ключ ALARM_EPISODES.analytics_counts_since снят в обоих слоях KB.
Раздел 8 — заброс оборотов в той же подаче: v_max = max min(обороты, k·f_max),
«на N выше порога», датчик выше частоты — расхождение; потребители.
Раздел 9 — строка без повтора названия: в отчёте, во входе гейта («Просадка
оборотов: До 1482 об/мин …») и заключения; разборы текста по «:» не задеты.
Ревью v5.2.14: расхождение — сам расходящийся отсчёт (rpm_freq_mismatch_rpm/_hz),
а не крайнее показание датчика; запас до уставки разноса — по датчику; частота
в отсчёте v_min/v_max (v_min_hz/v_max_hz); у датчика единица, оговорка в
скобках; «по частоте генератора» — только при расхождении в этом отсчёте;
версии входа гейта и совета — 5.2.14; напоминание о ключе счётчиков удалено.

Запуск:  py -3 scripts/test_v5214_rpm_freq_text.py
"""
from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from analytics.config import AnalyticsConfig  # noqa: E402
from analytics.contract import DerivedMetrics  # noqa: E402
from analytics.detectors import _detect_rpm_underspeed  # noqa: E402
from analytics.live import segment_spans  # noqa: E402
from analytics.rpm_freq import (  # noqa: E402
    dip_view, glitch_details, rpm_freq_mismatch, rpm_freq_params, v_star,
)
from analytics.segmenter import segment  # noqa: E402
from analytics.serializer import _det_worst  # noqa: E402

_errors: list[str] = []
U = timezone.utc
MSK = ZoneInfo("Europe/Moscow")


def check(cond: bool, msg: str) -> None:
    if not cond:
        _errors.append(msg)


KB = ROOT / "knowledge_base"
CFG = AnalyticsConfig.from_pair(KB, "pcc3300", "cummins_kta50")
RPM, FREQ, HB = CFG.role_to_addr("RPM"), CFG.role_to_addr("FREQUENCY"), 40290
PRM = rpm_freq_params(CFG)
L0 = datetime(2026, 11, 14, 13, 50, tzinfo=U)      # 16:50 МСК
L1 = L0 + timedelta(seconds=1800)
DIP = L0 + timedelta(seconds=907)                   # 17:05:07 МСК


def S(t, sec):
    return t + timedelta(seconds=sec)


def h(addr, ts, v):
    return {"addr": addr, "ts": ts, "value": v}


def rows_with(dip_rpm: float, dip_hz: float | None, freq: bool = True):
    """Работа 1500/1501 об/мин при 50 Гц каждые 10 с, пинг каждые 10 с; в 17:05:07 —
    отсчёт dip_rpm (частота dip_hz в той же метке), через 10 с снова 1500."""
    rows, t, k = [], S(L0, -300), 0
    while t < L1:
        rows.append(h(HB, t, float(k)))
        rows.append(h(RPM, t, 1500.0 + (k % 2)))
        if freq:
            rows.append(h(FREQ, t, 50.0))
        t += timedelta(seconds=10)
        k += 1
    rows += [h(HB, DIP, 9999.0), h(RPM, DIP, dip_rpm)]
    if freq and dip_hz is not None:
        rows.append(h(FREQ, DIP, dip_hz))
    return sorted(rows, key=lambda r: r["ts"])


def run_seg(rows):
    return segment([{"addr": 40011, "value": 3, "state_start": S(L0, -7200), "state_end": None}],
                   rows, [], [], CFG, "SN", "pcc", 1, "E", L0, L1)


def dets(segs):
    return [d for s in segs for sub in s.subsegments for d in sub.detections
            if d.scenario == "RPM_UNDERSPEED" and not sub.transient]


def hz_numbers(text: str) -> list[float]:
    """Все числа перед «Гц» в тексте."""
    return [float(x.replace(",", ".")) for x in re.findall(r"(\d+(?:,\d+)?)\s*Гц", text)]


# ══ 1. Сининда-1 14.11 17:05:07: 1344 об/мин при 49,4 Гц ═════════════════════
segs1 = run_seg(rows_with(1344.0, 49.4))
d1 = dets(segs1)
v1 = d1[0].values if d1 else {}
t1 = d1[0].trigger if d1 else ""
check(len(d1) == 1, f"1: одна детекция просадки: {[d.trigger for d in d1]}")
check(v1.get("rpm_min") == 1344.0 and v1.get("freq_rpm_min") == 1482.0 and v1.get("freq_hz_min") == 49.4
      and v1.get("v_min") == 1482.0, f"1: датчик 1344, по частоте 1482 (49,4 Гц), v_min 1482: {v1}")
check(v1.get("below_threshold_rpm") == 3.0 and v1.get("drop_rpm") == 18.0 and v1.get("drop_pct") == 1.2,
      f"1: глубина по v_min — на 3 ниже порога, 18 об/мин (1,2 %) ниже номинала: {v1}")
check(v1.get("spread_rpm") == 157.0 and v1.get("rpm_max") == 1501.0,
      f"1: размах от максимума — отдельным полем spread_rpm: {v1}")
check(v1.get("rpm_freq_mismatch") is True and v1.get("rpm_freq_mismatch_count") == 1
      and v1.get("rpm_freq_mismatch_rpm") == 1344.0 and v1.get("rpm_freq_mismatch_hz") == 49.4
      and v1.get("v_min_hz") == 49.4,
      f"1: признак расхождения, число и сам отсчёт, частота в отсчёте v_min: {v1}")
check(t1 == ("До 1482 об/мин по частоте генератора 49,4 Гц (на 3 ниже порога 1485), "
             "фронтов: 1; датчик оборотов в том же опросе — 1344 об/мин, с частотой не сходится "
             "(обороты и частота могли быть прочитаны в разные моменты опроса)"),
      f"1: строка срабатывания: {t1}")
check(hz_numbers(t1) == [49.4] and "1482 об/мин" in t1 and "сбой датчика" not in t1
      and "размах" not in t1 and "≈" not in t1,
      f"1: «Гц» только у частоты, у оборотов — «об/мин», без «сбой датчика» и размаха: {t1}")
_cl1 = (v1.get("live") or {}).get("clusters") or [{}]
check(_cl1[0].get("vmin") == 1344.0 and _cl1[0].get("v_min") == 1482.0,
      f"1: кластер — датчик (vmin) и подтверждённая глубина (v_min): {_cl1}")
check(d1 and d1[0].severity == "CAUTION", "1: аналитика не выше CAUTION")

# ══ 2. Без расхождения: 1470 при 49,0 Гц ═════════════════════════════════════
d2 = dets(run_seg(rows_with(1470.0, 49.0)))
v2 = d2[0].values if d2 else {}
t2 = d2[0].trigger if d2 else ""
check(len(d2) == 1 and v2.get("v_min") == 1470.0 and v2.get("rpm_min") == 1470.0
      and v2.get("freq_rpm_min") == 1470.0 and v2.get("freq_hz_min") == 49.0
      and v2.get("below_threshold_rpm") == 15.0 and v2.get("drop_rpm") == 30.0 and v2.get("drop_pct") == 2.0,
      f"2: v_min 1470, на 15 ниже порога, 30 (2 %) ниже номинала: {v2}")
check(v2.get("rpm_freq_mismatch") is False and v2.get("v_min_hz") == 49.0
      and not ({"rpm_freq_mismatch_count", "rpm_freq_mismatch_rpm", "rpm_freq_mismatch_hz"} & set(v2)),
      f"2: расхождения нет: {v2}")
check(t2 == "До 1470 об/мин при частоте генератора 49 Гц (на 15 ниже порога 1485), фронтов: 1",
      f"2: коротко, без датчика и размаха: {t2}")

# ══ 3. Без частоты — по оборотам, как раньше ═════════════════════════════════
d3 = dets(run_seg(rows_with(1473.0, None, freq=False)))
v3 = d3[0].values if d3 else {}
t3 = d3[0].trigger if d3 else ""
check(len(d3) == 1 and v3.get("v_min") == 1473.0 and v3.get("rpm_min") == 1473.0
      and v3.get("freq_rpm_min") is None and v3.get("freq_hz_min") is None
      and v3.get("rpm_freq_mismatch") is None and v3.get("below_threshold_rpm") == 12.0,
      f"3: частоты нет — v_min = обороты, сверять не с чем (None): {v3}")
check(t3 == "До 1473 об/мин по датчику оборотов (на 12 ниже порога 1485), фронтов: 1",
      f"3: строка без частоты: {t3}")
# По подсегменту (live=None, участков нет): прежний путь, без полей частоты
d3b = _detect_rpm_underspeed({"RPM": {"min": 1473, "max": 1510, "median": 1497}}, DerivedMetrics(), L0, CFG)
v3b = d3b[0].values if d3b else {}
check(d3b and d3b[0].trigger == "До 1473 об/мин по датчику оборотов (на 12 ниже порога 1485), "
      "фронтов: 1", f"3б: live=None — по датчику: {d3b and d3b[0].trigger}")
check(v3b.get("v_min") == 1473.0 and v3b.get("below_threshold_rpm") == 12.0 and v3b.get("spread_rpm") == 37
      and v3b.get("drop_rpm") == 27.0 and v3b.get("drop_pct") == 1.8
      and not ({"freq_rpm_min", "freq_hz_min", "rpm_freq_mismatch", "live"} & set(v3b)),
      f"3б: глубина 27 (1,8 %), размах 37 отдельно, полей частоты нет: {v3b}")
check(not _detect_rpm_underspeed({"RPM": {"min": 1490, "max": 1510}}, DerivedMetrics(), L0, CFG),
      "3б: выше порога — детекции нет")

# ══ 4. dip_view, v*, допуск ══════════════════════════════════════════════════
check(v_star(1344.0, 1482.0, PRM) == 1482.0 and v_star(1473.0, None, PRM) == 1473.0
      and v_star(1480.0, 1500.0, dict(PRM, dip_needs_freq=False)) == 1480.0,
      "4: v* = max(обороты, k·f); без частоты и при правиле B — обороты")
check(PRM["tolerance_rpm"] == 10.0 and rpm_freq_mismatch(1471.0, 1482.0, PRM)
      and not rpm_freq_mismatch(1472.0, 1482.0, PRM) and not rpm_freq_mismatch(1500.0, 1482.0, PRM)
      and not rpm_freq_mismatch(1344.0, None, PRM),
      "4: расхождение — датчик ниже k·f больше допуска сверки (10), только вниз, без частоты — нет")
# v_min и самый низкий расходящийся отсчёт — разные отсчёты
pts = [(S(DIP, 0), 1344.0, 1482.0), (S(DIP, 30), 1480.0, 1470.0)]
dv = dip_view(pts, PRM)
check(dv["v_min"] == 1480.0 and dv["v_rpm"] == 1480.0 and dv["v_hz"] == 49.0 and not dv["v_by_freq"]
      and dv["mismatch"] is True and dv["mismatch_count"] == 1 and dv["mm_rpm"] == 1344.0
      and dv["mm_hz"] == 49.4 and dv["mm_same"] is False and dv["rpm_min"] == 1344.0
      and dv["freq_rpm_min"] == 1470.0 and dv["freq_hz_min"] == 49.0, f"4а: разные отсчёты: {dv}")
from analytics.detectors import _underspeed_trigger  # noqa: E402
t4 = _underspeed_trigger(dv["v_min"], 1485 - dv["v_min"], 1485.0, 2, dv)
check(t4 == ("До 1480 об/мин при частоте генератора 49 Гц (на 5 ниже порога 1485), "
             "фронтов: 2; датчик оборотов в провале — до 1344 об/мин при частоте генератора 49,4 Гц, "
             "с частотой не сходится (обороты и частота могли быть прочитаны в разные моменты опроса)")
      and hz_numbers(t4) == [49.0, 49.4], f"4а: строка — датчик со своей частотой: {t4}")
# Несколько расходящихся отсчётов — их число
dv2 = dip_view([(S(DIP, 0), 1344.0, 1482.0), (S(DIP, 10), 1400.0, 1482.0)], PRM)
t42 = _underspeed_trigger(dv2["v_min"], 1485 - dv2["v_min"], 1485.0, 1, dv2)
check(dv2["mismatch_count"] == 2 and dv2["mm_same"] and t42.endswith(
      "с частотой не сходится (отсчётов: 2; обороты и частота могли быть прочитаны в разные моменты опроса)"),
      f"4б: два расходящихся отсчёта: {t42}")
# Правило B: глубина — по датчику, расхождение — без повтора числа
PRM_B = dict(PRM, dip_needs_freq=False)
dvb = dip_view([(S(DIP, 0), 1480.0, 1500.0)], PRM_B)
tb = _underspeed_trigger(dvb["v_min"], 1485 - dvb["v_min"], 1485.0, 1, dvb)
check(dvb["v_min"] == 1480.0 and dvb["mismatch"] and dvb["mm_same"]
      and tb == ("До 1480 об/мин при частоте генератора 50 Гц (на 5 ниже порога 1485), "
                 "фронтов: 1; датчик оборотов с частотой не сходится (обороты и частота могли быть "
                 "прочитаны в разные моменты опроса)"), f"4в: правило B: {dvb} / {tb}")
# В пределах допуска — «при частоте», хотя значение дала частота (ревью v5.2.14, п. 5)
dvt = dip_view([(S(DIP, 0), 1418.0, 1419.0)], PRM)
check(dvt["v_min"] == 1419.0 and not dvt["v_by_freq"] and dvt["mismatch"] is False
      and _underspeed_trigger(1419.0, 66.0, 1485.0, 1, dvt)
      == "До 1419 об/мин при частоте генератора 47,3 Гц (на 66 ниже порога 1485), фронтов: 1",
      f"4в: в пределах допуска — «при частоте генератора»: {dvt}")
# Ревью v5.2.14, проба A: расходящийся отсчёт 1300 при 49,4 Гц, крайний датчик 1100 — без частоты
dA = dets(run_seg(rows_with(1300.0, 49.4) + [h(HB, S(DIP, 10), 9998.0), h(RPM, S(DIP, 10), 1100.0),
                                              h(FREQ, S(DIP, 10), 36.0)]))
vA = dA[0].values if dA else {}
check(dA and vA.get("rpm_min") == 1100.0 and vA.get("v_min") == 1100.0 and vA.get("rpm_freq_mismatch_rpm") == 1300.0
      and vA.get("rpm_freq_mismatch_hz") == 49.4
      and dA[0].trigger.endswith("датчик оборотов в провале — до 1300 об/мин при частоте генератора 49,4 Гц, "
                                 "с частотой не сходится (обороты и частота могли быть прочитаны в разные "
                                 "моменты опроса)"),
      f"4г: расхождение — сам отсчёт 1300 при 49,4 Гц, а не крайний 1100: {vA} / {dA and dA[0].trigger}")
check(dip_view([], PRM) is None, "4г: отсчётов нет — None")
# v_min кластера считается в segment_spans
by_addr: dict[int, list[dict]] = {}
for r in rows_with(1344.0, 49.4):
    by_addr.setdefault(r["addr"], []).append(r)
spn = segment_spans(by_addr, None, CFG, L0, L1, [], {}, False)
cl = spn.get("RPM_UNDERSPEED") or []
check(len(cl) == 1 and cl[0]["vmin"] == 1344.0 and cl[0]["v_min"] == 1482.0,
      f"4д: кластер — vmin датчика и v_min: {[(c.get('vmin'), c.get('v_min')) for c in cl]}")

# ══ 5. Строка проверки source_rpm_glitch ════════════════════════════════════
gd = glitch_details([{"t": "2026-10-15T01:42:32+00:00", "rpm": 1479.0, "hz": 50.0},
                     {"t": "2026-10-15T01:53:52+00:00", "rpm": 1440.0, "hz": 49.9}], MSK)
check(gd == ("отброшены из расчёта отсчёты оборотов, не сходящиеся с частотой генератора (не доказательство "
             "сбоя датчика: обороты и частота могут быть прочитаны в разные моменты опроса), всего 2: "
             "04:42:32 (1479 при 50 Гц), 04:53:52 (1440 при 49,9 Гц)"), f"5: текст проверки: {gd}")
check("сбой датчика" not in gd and "(сбой датчика)" not in gd, f"5: без вердикта «сбой датчика»: {gd}")
# Тексты отчёта и входа модели в коде — без «сбой датчика» (комментарии — можно)
_txt_bad = []
for _f in ("analytics/rpm_freq.py", "analytics/detectors.py", "analytics/segmenter.py", "analytics/serializer.py",
           "analytics/runner.py", "corpus/preprocessor.py", "telemetry_mcp/tools_segments.py"):
    for _n, _line in enumerate((ROOT / _f).read_text(encoding="utf-8").splitlines(), 1):
        _code = _line.split("#", 1)[0]
        if _code.strip().startswith('"""'):
            continue     # докстринг — комментарий кода
        if re.search(r"[\"'][^\"']*сбо[йяи] датчика", _code) and "доказательство" not in _code:
            _txt_bad.append(f"{_f}:{_n}")
check(not _txt_bad, f"5: строки кода с «сбой датчика» в тексте: {_txt_bad}")

# ══ 6. Потребители ═══════════════════════════════════════════════════════════
# 6а. Худшее в отчёте — по v_min; у старых записей — по rpm_min
_ents = [(1, {"scenario": "RPM_UNDERSPEED", "values": {"rpm_min": 1344.0, "v_min": 1482.0}}),
         (2, {"scenario": "RPM_UNDERSPEED", "values": {"rpm_min": 1470.0, "v_min": 1470.0}}),
         (3, {"scenario": "RPM_UNDERSPEED", "values": {"rpm_min": 1475.0}})]
w = _det_worst(_ents)
check(w and w[0] == 2, f"6а: худшее — по подтверждённой глубине, а не по датчику 1344: {w}")
w_old = _det_worst([(1, {"scenario": "RPM_UNDERSPEED", "values": {"rpm_min": 1480}}),
                    (2, {"scenario": "RPM_UNDERSPEED", "values": {"rpm_min": 1460}})])
check(w_old and w_old[0] == 2, f"6а: записи до v5.2.14 — по rpm_min: {w_old}")
# 6б. Вход заключения (иерархия обнаружений)
from corpus.preprocessor import CONCLUSION_INPUT_VERSION, _group_worst  # noqa: E402
gw = _group_worst("RPM_UNDERSPEED", [v1])
check(gw == ("мин. 1482 об/мин — на 3 ниже порога 1485; датчик оборотов 1344 об/мин при 49,4 Гц — с частотой генератора "
             "не сходится (могли быть прочитаны в разные моменты опроса)"), f"6б: строка заключения: {gw}")
check(_group_worst("RPM_UNDERSPEED", [v2]) == "мин. 1470 об/мин — на 15 ниже порога 1485"
      and _group_worst("RPM_UNDERSPEED", [{"rpm_min": 1473, "threshold_rpm": 1485}])
      == "мин. 1473 об/мин — на 12 ниже порога 1485",
      "6б: без расхождения и старая запись — как раньше")
def _ver(v):
    return tuple(int(x) for x in str(v).split("."))


# не ниже 5.2.14: следующие версии поднимают её дальше (5.2.15 — подпись попытки)
check(_ver(CONCLUSION_INPUT_VERSION) >= (5, 2, 14), f"6б: версия входа заключения поднята: {CONCLUSION_INPUT_VERSION}")
# 6в. Сервер телеметрии, episodes: новая запись — глубина и сверка; старая — без drop_* (= размах)
from telemetry_mcp.tools_segments import _episode_values  # noqa: E402
ev_new = _episode_values("RPM_UNDERSPEED", v1)
check(ev_new == ["v_min=1482", "v_min_hz=49.4", "below_threshold_rpm=3", "rpm_min=1344", "rpm_freq_mismatch=да",
                 "rpm_freq_mismatch_rpm=1344", "rpm_freq_mismatch_hz=49.4", "spread_rpm=157"],
      f"6в: новая запись — частота в отсчёте v_min и сам расходящийся отсчёт: {ev_new}")
check(_episode_values("RPM_UNDERSPEED", v2) == ["v_min=1470", "v_min_hz=49", "below_threshold_rpm=15",
                                                "rpm_min=1470", "rpm_freq_mismatch=нет", "spread_rpm=31"],
      f"6в: без расхождения: {_episode_values('RPM_UNDERSPEED', v2)}")
_old = {"live": {"clusters": []}, "rpm_max": 1511.0, "rpm_med": 1500.0, "rpm_min": 1344.0, "drop_pct": 11.1,
        "drop_rpm": 167.0, "rated_rpm": 1500.0, "spread_rpm": 167.0, "front_count": 1, "freq_rpm_min": 1482.0,
        "threshold_rpm": 1485.0, "below_threshold_rpm": 141.0}
ev_old = _episode_values("RPM_UNDERSPEED", _old)
check(ev_old == ["rpm_min=1344", "freq_rpm_min=1482", "threshold_rpm=1485", "spread_rpm=167"]
      and not any(x.startswith("drop_") for x in ev_old), f"6в: запись #15433 (до v5.2.14): {ev_old}")
ev_lp = _episode_values("LIMIT_PROXIMITY", {"value": 99.5, "shutdown_limit": 110.0, "role": "OIL_TEMP",
                                            "proximity_pct": 10, "limit": {"kind": "setpoint"}})
check(ev_lp == ["value=99.5", "shutdown_limit=110", "role=OIL_TEMP", "proximity_pct=10"],
      f"6в: прочие сценарии — как раньше (первые шесть): {ev_lp}")
# 6г. Хеши составов гейта: значения в них не входят, эталон v5.2.0 прежний
import online.status_assembler as sa  # noqa: E402
AN = [{"scenario": "LIMIT_PROXIMITY", "severity": "CAUTION", "ref_codes": [144], "fault_codes": [],
       "param_role": "COOLANT_TEMP"},
      {"scenario": "OIL_PRESSURE_LOW", "severity": "CAUTION", "fault_codes": [], "limit": {"kind": "passport",
                                                                                          "hard": True}},
      {"scenario": "RPM_UNDERSPEED", "severity": "CAUTION", "fault_codes": []}]
AN_V = [dict(a) for a in AN]
AN_V[2] = dict(AN_V[2], values=dict(v1), trigger=t1)
PN = [{"scenario": "CONTROLLER_FAULT", "severity": "WARNING", "fault_codes": [1448]}]
check(sa.compute_analytics_hash(AN) == sa.compute_analytics_hash(AN_V) == "75b09537f578"
      and sa.compute_fault_hash({"panel_alarms": PN, "analytics_alarms": AN_V}) == "f1e845d1d4d0",
      "6г: хеши составов гейта прежние, новые поля и текст в них не входят")
import online.incident_gate as ig  # noqa: E402
check(sa.GATE_RULES_VERSION == "5.1.15" and _ver(sa.GATE_INPUT_VERSION) >= (5, 2, 14)
      and _ver(ig.INCIDENT_INPUT_VERSION) >= (5, 2, 14),
      f"6г: GATE_RULES_VERSION прежний; формат входа гейта и совета — 5.2.14 (строки срабатывания): "
      f"{sa.GATE_RULES_VERSION}, {sa.GATE_INPUT_VERSION}, {ig.INCIDENT_INPUT_VERSION}")
d1_rc = (d1[0].ref_codes, d1[0].fault_codes) if d1 else None
check(d1_rc == ([1448], []) and d1 and d1[0].description_key == "RPM_UNDERSPEED.below_threshold",
      f"6г: справочный код (ref_codes) и ключ описания прежние: {d1_rc}")

# ══ 7. Ключ переходного месяца снят (В5: «после 15.11 снять») ══════════════
import online.engine as eng  # noqa: E402

_legacy = AnalyticsConfig(KB / "equipment" / "cummins_kta50_pcc3300")
CFG_Q = AnalyticsConfig.from_pair(KB, "pcc3300", "cummins_qsx15")
check(eng.analytics_counts_since(CFG) is None and eng.analytics_counts_since(CFG_Q) is None
      and eng.analytics_counts_since(_legacy) is None, "7: счётчики аналитики без фильтра по дате")
check(not hasattr(eng, "warn_stale_counts_since"), "7: напоминание снять ключ удалено вместе с ключом")
for _p in (KB / "_defaults" / "analytics" / "detectors.yaml",
           KB / "equipment" / "cummins_kta50_pcc3300" / "analytics" / "detectors.yaml"):
    _y = yaml.safe_load(_p.read_text(encoding="utf-8")) or {}
    _raw = _p.read_text(encoding="utf-8")
    check("analytics_counts_since" not in (_y.get("ALARM_EPISODES") or {}) and "переходный месяц" not in _raw
          and "2026-10-16" not in _raw, f"7: {_p.relative_to(ROOT)} — ключа и комментария нет")

# ══ 8. Заброс оборотов — та же подача ════════════════════════════════════════
from analytics.detectors import _detect_rpm_overspeed  # noqa: E402
from analytics.rpm_freq import over_view, overspeed_params, v_over  # noqa: E402

OPRM = overspeed_params(CFG)


def ospd(segs):
    return [d for s in segs for sub in s.subsegments for d in sub.detections
            if d.scenario == "RPM_OVERSPEED" and not sub.transient]


# 8а. ДГУ №2 15.10 12:42:19: 1606 об/мин при 53,6 Гц — датчик и частота сходятся
o1 = ospd(run_seg(rows_with(1606.0, 53.6)))
ov1 = o1[0].values if o1 else {}
to1 = o1[0].trigger if o1 else ""
check(len(o1) == 1 and ov1.get("rpm_max") == 1606.0 and ov1.get("freq_rpm_max") == 1608.0
      and ov1.get("freq_hz_max") == 53.6 and ov1.get("v_max") == 1606.0 and ov1.get("above_threshold_rpm") == 31.0
      and ov1.get("margin_rpm") == 119.0 and ov1.get("rpm_freq_mismatch") is False and ov1.get("v_max_hz") == 53.6
      and "rpm_freq_mismatch_count" not in ov1, f"8а: v_max = min(1606, 1608), на 31 выше порога: {ov1}")
check(to1 == ("До 1606 об/мин при частоте генератора 53,6 Гц (на 31 выше порога 1575 — 91,3 % уставки "
              "разноса 1725; до уставки осталось 119), фронтов: 1") and hz_numbers(to1) == [53.6]
      and "≈" not in to1 and "сбой датчика" not in to1, f"8а: строка заброса — коротко: {to1}")
_ocl = (ov1.get("live") or {}).get("clusters") or [{}]
check(_ocl[0].get("vmax") == 1606.0 and _ocl[0].get("v_max") == 1606.0, f"8а: кластер — vmax и v_max: {_ocl}")
check(o1 and o1[0].severity == "CAUTION" and o1[0].ref_codes == [234], "8а: CAUTION, код 234 справочный")
# 8б. Датчик выше частоты: 1700 при 53,6 Гц — высота по частоте, датчик рядом
o2 = ospd(run_seg(rows_with(1700.0, 53.6)))
ov2 = o2[0].values if o2 else {}
to2 = o2[0].trigger if o2 else ""
check(len(o2) == 1 and ov2.get("rpm_max") == 1700.0 and ov2.get("v_max") == 1608.0 and ov2.get("v_max_hz") == 53.6
      and ov2.get("above_threshold_rpm") == 33.0 and ov2.get("margin_rpm") == 25.0
      and ov2.get("rpm_freq_mismatch") is True and ov2.get("rpm_freq_mismatch_count") == 1
      and ov2.get("rpm_freq_mismatch_rpm") == 1700.0 and ov2.get("rpm_freq_mismatch_hz") == 53.6,
      f"8б: v_max 1608 по частоте, запас до уставки — по датчику (25), расхождение: {ov2}")
check(to2 == ("До 1608 об/мин по частоте генератора 53,6 Гц (на 33 выше порога 1575 — 91,3 % уставки "
              "разноса 1725; до уставки осталось 25 по датчику оборотов), фронтов: 1; датчик оборотов в том же "
              "опросе — 1700 об/мин, с частотой не сходится (обороты и частота могли быть прочитаны в разные "
              "моменты опроса)")
      and hz_numbers(to2) == [53.6], f"8б: строка заброса с расхождением: {to2}")
# 8в. v_over, расхождение вверх, over_view
check(v_over(1606.0, 1608.0) == 1606.0 and v_over(1700.0, 1608.0) == 1608.0 and v_over(1606.0, None) == 1606.0,
      "8в: v_over = min(обороты, k·f_max)")
check(rpm_freq_mismatch(1619.0, 1608.0, OPRM, over=True) and not rpm_freq_mismatch(1618.0, 1608.0, OPRM, over=True)
      and not rpm_freq_mismatch(1500.0, 1608.0, OPRM, over=True), "8в: у заброса расхождение — только вверх")
ovw = over_view([(S(DIP, 0), 1700.0, 1608.0), (S(DIP, 10), 1620.0, 1611.0)], OPRM)
# 1620 при k·f 1611 — в пределах допуска: «при частоте», не «по частоте» (ревью v5.2.14, п. 5)
check(ovw["v_max"] == 1611.0 and ovw["v_rpm"] == 1620.0 and not ovw["v_by_freq"] and ovw["mm_rpm"] == 1700.0
      and ovw["mm_same"] is False and ovw["mismatch_count"] == 1 and ovw["rpm_max"] == 1700.0
      and ovw["freq_rpm_max"] == 1611.0, f"8в: over_view — v_max и расходящийся отсчёт разные: {ovw}")
check(_detect_rpm_overspeed(L0, CFG, live=None, seg_end=L1) == [], "8в: без участков заброса нет (как было)")
# 8г. Потребители
_ow = _det_worst([(1, {"scenario": "RPM_OVERSPEED", "values": {"rpm_max": 1700.0, "v_max": 1608.0}}),
                  (2, {"scenario": "RPM_OVERSPEED", "values": {"rpm_max": 1650.0, "v_max": 1650.0}}),
                  (3, {"scenario": "RPM_OVERSPEED", "values": {"rpm_max": 1640.0}})])
check(_ow and _ow[0] == 2, f"8г: худшее заброса — по v_max: {_ow}")
check(_group_worst("RPM_OVERSPEED", [ov2]) == ("макс. 1608 об/мин — до уставки разноса 1725 осталось 25 по "
                                               "датчику оборотов; датчик оборотов 1700 об/мин при 53,6 Гц — с "
                                               "частотой генератора не сходится (могли быть прочитаны в разные "
                                               "моменты опроса)")
      and _group_worst("RPM_OVERSPEED", [{"rpm_max": 1696.0, "overspeed_rpm": 1725.0}])
      == "макс. 1696 об/мин — до уставки разноса 1725 осталось 29",
      f"8г: вход заключения: {_group_worst('RPM_OVERSPEED', [ov2])}")
ev_o = _episode_values("RPM_OVERSPEED", ov2)
check(ev_o == ["v_max=1608", "v_max_hz=53.6", "above_threshold_rpm=33", "rpm_max=1700", "rpm_freq_mismatch=да",
               "rpm_freq_mismatch_rpm=1700", "rpm_freq_mismatch_hz=53.6", "margin_rpm=25"],
      f"8г: episodes, новая запись: {ev_o}")
# Ревью v5.2.14, проба B: 1700 при 56,8 Гц сходится, расходится 1650 при 53,6 Гц — его и называем
oB = ospd(run_seg(rows_with(1700.0, 56.8) + [h(HB, S(DIP, 10), 9998.0), h(RPM, S(DIP, 10), 1650.0),
                                             h(FREQ, S(DIP, 10), 53.6)]))
vB = oB[0].values if oB else {}
check(oB and vB.get("v_max") == 1700.0 and vB.get("margin_rpm") == 25.0 and vB.get("rpm_freq_mismatch_rpm") == 1650.0
      and vB.get("rpm_freq_mismatch_hz") == 53.6
      and _group_worst("RPM_OVERSPEED", [vB]).endswith("датчик оборотов 1650 об/мин при 53,6 Гц — с частотой "
                                                       "генератора не сходится (могли быть прочитаны в разные "
                                                       "моменты опроса)"),
      f"8г: расходящийся отсчёт заброса — 1650 при 53,6 Гц, а не максимум 1700: {vB} / "
      f"{_group_worst('RPM_OVERSPEED', [vB])}")
check(_group_worst("RPM_OVERSPEED", [{"v_max": True}]) is None
      and _group_worst("RPM_UNDERSPEED", [{"v_min": False, "rpm_min": None}]) is None,
      "8г: булево значение — не число")
_oold = {"live": {}, "rpm_max": 1606.0, "margin_rpm": 119.0, "freq_hz_max": 53.6, "front_count": 1,
         "overspeed_rpm": 1725.0, "freq_rpm_max": 1608.0, "threshold_rpm": 1575.0}
check(_episode_values("RPM_OVERSPEED", _oold) == ["rpm_max=1606", "margin_rpm=119", "freq_hz_max=53.6",
                                                  "front_count=1", "overspeed_rpm=1725"],
      f"8г: episodes, запись до v5.2.14 — как раньше: {_episode_values('RPM_OVERSPEED', _oold)}")
AN_O = AN + [{"scenario": "RPM_OVERSPEED", "severity": "CAUTION", "fault_codes": [], "ref_codes": [234]}]
AN_OV = AN_V + [dict(AN_O[-1], values=dict(ov2), trigger=to2)]
check(sa.compute_analytics_hash(AN_O) == sa.compute_analytics_hash(AN_OV),
      "8г: хеш состава гейта не зависит от значений и строки заброса")

# ══ 9. Строка без повтора названия: отчёт, вход гейта и заключения ══════════
from analytics.live import limit_role  # noqa: E402
from analytics.serializer import SCENARIO_RU, _det_name, to_markdown  # noqa: E402
from corpus.preprocessor import _fmt_detections_hierarchy  # noqa: E402

check(all(not t.startswith(("Просадка", "Заброс", "RPM", "Обороты")) for t in (t1, t2, t3, to1, to2)),
      "9: название сценария в строке не повторяется")
md1 = to_markdown(segs1, "SN", "pcc", 1, L0, L1, tz=MSK)
check("**Просадка оборотов** (CAUTION)" in md1
      and "  - До 1482 об/мин по частоте генератора 49,4 Гц (на 3 ниже порога 1485)" in md1,
      f"9а: отчёт — пункт «Просадка оборотов», под ним строка: "
      f"{[x for x in md1.splitlines() if 'До 1482' in x or 'Просадка' in x]}")
_struct = {"mode_label": "Работа", "run_state": 3, "load_pct": 41.0, "time_in_mode_sec": 3725,
           "severity_level": "предупреждение", "panel_severity": "норма", "analytics_severity": "предупреждение",
           "panel_alarms": [], "params": [],
           "analytics_alarms": [
               {"scenario": "RPM_UNDERSPEED", "severity": "CAUTION", "fault_codes": [],
                "description": SCENARIO_RU["RPM_UNDERSPEED"], "trigger": t1, "history_count_30d": 7,
                "history_duration_30d_sec": 600},
               {"scenario": "RPM_OVERSPEED", "severity": "CAUTION", "fault_codes": [],
                "description": SCENARIO_RU["RPM_OVERSPEED"], "trigger": to1}]}
gp = sa.build_warning_prompt(_struct)
check("A1 [CAUTION] Просадка оборотов: До 1482 об/мин по частоте генератора 49,4 Гц (на 3 ниже порога 1485)" in gp
      and "A2 [CAUTION] Заброс оборотов: До 1606 об/мин при частоте генератора 53,6 Гц" in gp
      and "датчик оборотов в том же опросе — 1344 об/мин, с частотой не сходится (обороты и частота могли быть "
      "прочитаны в разные моменты опроса) — за 30 дней: 7 (10 мин суммарно)" in gp,
      f"9б: вход гейта — оговорка в скобках, счётчик после неё: "
      f"{[x for x in gp.splitlines() if x.strip().startswith('A')]}")
hier = _fmt_detections_hierarchy({"subsegments": [{"duration_sec": 60, "detections": [d1[0].to_dict(),
                                                                                     o1[0].to_dict()]}]})
check("Просадка оборотов — 1 срабатывание" in hier and "мин. 1482 об/мин — на 3 ниже порога 1485" in hier
      and "Заброс оборотов — 1 срабатывание" in hier
      and "макс. 1606 об/мин — до уставки разноса 1725 осталось 119" in hier, f"9в: вход заключения: {hier}")
# Разборы текста по «:» — только у близости к уставке и панели: строка оборотов их не задевает
check(_det_name({"scenario": "RPM_UNDERSPEED", "trigger": t1}) == "Просадка оборотов"
      and _det_name({"scenario": "RPM_OVERSPEED", "trigger": to2}) == "Заброс оборотов"
      and limit_role(d1[0].values, t1, []) is None and limit_role(o1[0].values, to1, []) is None,
      "9г: имя группы и роль близости по строке оборотов не берутся")

if _errors:
    print(f"FAIL — {len(_errors)} расхождений:")
    for x in _errors:
        print(f"  • {x}")
    sys.exit(1)
print("Все проверки пройдены.")
