# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""v5.1.21: эпизоды аналитики по событию и сверка оборотов с частотой
(план docs/plans/design_v5121_episodes.md, решения владельца В1–В7 15.10.2026).

Раздел 1 — сверка оборотов с частотой генератора (П1) и удержанные значения
на голове окна (П10):
- 1а: одиночный сбой датчика оборотов (ДГУ №1 15.10 04:42–09:24 МСК — 12
  отсчётов 1417–1479 при 49,9–50,1 Гц, следом 1495–1501) вырезается, настоящие
  провалы и падения в стоп — нет; частота «опроса» ловит строку, пришедшую на
  1–8 с позже оборотов;
- 1б: провал v* — ниже порога и обороты, и частота; правило B ключом;
- 1в / В4: проверка source_rpm_glitch с временем МСК, от 3 за сегмент —
  строка в «Замечаниях» без влияния на вердикт;
- П10: _load_seed отдаёт удержанные значения всегда, одним запросом DISTINCT ON.

Раздел 2 — участки (П2, analytics/live.py): удержание при идущих пакетах,
пауза пакетов кончает участок, дыра data_gaps — нет (слепая доля), пол
давления, удержанные значения на начало сегмента, перенос только при
REPORT_START, переходный кусок без участков, паспорт через границу зоны и
через суточный рез (40 + 40 с).
Раздел 3 — кластеры: N = 600 с видимого времени, пауза 666 с — два кластера,
близость — объединение ролей с участками по ролям.
Раздел 4 — детекторы (П3): детекция по участку подсегмента, t_detected —
начало кластера, values.live; live_spans=None — прежний вывод; паспорт из
кластера; комбинированная ветка с полом; просадка по v*; сбой участков.
Раздел 5 — свежесть (П4): detection_live (дыра внутри N не в счёт), взгляд
назад через смену зоны, давний кластер выпадает, двухступенчатая свежесть
паспорта и псевдоним _passport_stale, заглушки без data_gaps/t_end,
«активен на конец» в сводке той же функцией.
Раздел 6 — движок (П5, П8) с подменой БД: открытие от начала кластера,
продолжение, разрез с точным закрытием по until, смешанный ключ, мост на
резе (и его отсутствие), пересчёт слепой доли, карта концов аналитики,
восстановление exact/until из open_values_json, SQL update/close_episode.
Раздел 7 — посев при закрытии и рез (П6, П7): эпизод на кластер, покрытие
онсетом и пересечением (один запрос на сегмент), открытый точный эпизод — до
until + N, два сегмента окна, перенос на резе (и его нет при догонке),
смешанный ключ, синтетика с живого начала, _drop_same_onset только для панели,
хвост дебаунса по until, перенос 60 с в сводке нового дня, мост в открытой
сводке, until при переоткрытии.
Раздел 8 — сводка, отчёт, заключение (П9): подтверждение по кластеру,
открытый эпизод до конца нарушения, строка «Нарушение: …», блок 1 по ролям.
Раздел 9 — синтетические аналоги регресса (#35890, #35891, #35872) через
segment() → посев → сводку; реальные окна — scripts/check_v5121_data.py.
Раздел 10 — счётчики переходного месяца (П11, В5).
Раздел 11 — ревью v5.1.21, сценарии ревьюеров без БД: пауза пакетов без
data_gaps внутри нарушения (until — горизонт идущего кластера, конец — по
карте окна; разрез кластера после паузы), рестарт с простоем > N (until из
JSON уточняется по кластерам окна; простой через 09:00 — _refine_until при
закрытии), догонка через рез (касание 09:00 — не покрытие), правило B в
закрытом окне (живого края нет), 1–2 сбоя датчика оборотов — строкой ℹ️ в
полном отчёте.

Запуск:  py -3 scripts/test_episodes_v5121.py
"""
from __future__ import annotations

import asyncio
import copy
import inspect
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import analytics.rpm_freq as _rf  # noqa: E402
import analytics.segmenter as _sg  # noqa: E402
import analytics.source as _src  # noqa: E402
import online.engine as eng  # noqa: E402
from analytics.config import AnalyticsConfig  # noqa: E402
from analytics.contract import DerivedMetrics, RiskAccumulators, Segment, Subsegment  # noqa: E402
from analytics.rpm_freq import build_poll, dip_marks, is_dip, rpm_freq_params  # noqa: E402
from analytics.segmenter import _drop_rpm_glitches, segment  # noqa: E402
from analytics.serializer import (  # noqa: E402
    _DATA_QUALITY_CHECKS, _check_details, build_summary_parts, render_summary_md,
)
from corpus.preprocessor import _remarks_from_summary  # noqa: E402

_errors: list[str] = []
MSK = ZoneInfo("Europe/Moscow")
U = timezone.utc


def check(cond: bool, msg: str) -> None:
    if not cond:
        _errors.append(msg)


class _LogCatch(logging.Handler):
    """Предупреждения модулей — в список, а не в stderr теста."""

    def __init__(self):
        super().__init__(logging.WARNING)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


LOG = _LogCatch()
for _name in ("analytics", "online"):
    logging.getLogger(_name).addHandler(LOG)
    logging.getLogger(_name).propagate = False


def logged(text: str) -> bool:
    return any(text in r.getMessage() for r in LOG.records)


KB = ROOT / "knowledge_base"
CFG = AnalyticsConfig.from_pair(KB, "pcc3300", "cummins_kta50")
CFG3 = AnalyticsConfig.from_pair(KB, "pcc3300", "cummins_qsx15")
RPM, FREQ, HB, OILT, LOAD = 40068, 40044, 40290, 40063, CFG.role_to_addr("LOAD_PCT")


def T(h, m, s=0, d=15):
    return datetime(2026, 10, d, h, m, s, tzinfo=U)


def S(t, sec):
    return t + timedelta(seconds=sec)


def h(addr, ts, v):
    return {"addr": addr, "ts": ts, "value": v}


def rs(v, a, b=None):
    return {"addr": 40011, "value": v, "state_start": a, "state_end": b}


def pings(a, b, step=10, skip=None):
    """Пакеты связи (пинг 40290) каждые step с; skip — (от, до) без пакетов."""
    out, t, k = [], a, 0
    while t < b:
        if not (skip and skip[0] < t < skip[1]):
            out.append(h(HB, t, float(k)))
        t += timedelta(seconds=step)
        k += 1
    return out


def by_addr(rows):
    out: dict[int, list[dict]] = {}
    for r in sorted(rows, key=lambda r: r["ts"]):
        out.setdefault(r["addr"], []).append(r)
    return out


t1 = T(1, 42, 32)   # 04:42:32 МСК — первый сбой ДГУ №1 15.10
RS3 = [rs(3, T(0, 0))]
P = pings(T(1, 30), T(2, 0))


def drop(rows, enum=None, held=None, gaps=None, cfg=CFG):
    return _drop_rpm_glitches(by_addr(rows + P), cfg, RS3 if enum is None else enum, held, gaps)


def marks(rows, held=None, gaps=None, tt=None, cfg=CFG, prm=None):
    b = by_addr(rows + P)
    prm = prm or rpm_freq_params(cfg)
    poll = build_poll(b, cfg, prm, held, gaps)
    real = [(r["ts"], float(r["value"])) for r in b.get(RPM, [])]
    return {t: (v, f, bad) for t, v, f, bad in dip_marks(real, poll, prm, tt)}


def cut_at(res, t):
    return any(g[0] == t for g in res[1])


def kept(res, t):
    return any(r["ts"] == t for r in res[0].get(RPM, []))


# ══ 1. Сверка оборотов с частотой (П1, этап 1) ═════════════════════════════

# ── 1а. 1479 при 50,1 со следующим 1499 — сбой, вырезан ──
R1 = [h(RPM, S(t1, -10), 1500.0), h(FREQ, S(t1, -10), 50.0), h(RPM, t1, 1479.0), h(FREQ, t1, 50.1),
      h(RPM, S(t1, 10), 1499.0)]
r1 = drop(R1)
check(cut_at(r1, t1) and not kept(r1, t1), f"1а: сбой вырезан: {r1[1]}")
check(r1[1] and r1[1][0][1] == 1479.0 and r1[1][0][2] == 50.1,
      f"1а: в находке обороты и f_min опроса (удержанное на t — строка той же метки): {r1[1]}")
check(len(r1[0][FREQ]) == 2, "1а: строки частоты остаются")
check(not marks(R1)[t1][2], "1а: v* — сбой и не провал")

# ── 1б. 1484 при 49,7 — не вырезан и не провал (30·49,7 = 1491) ──
R2 = [h(RPM, S(t1, -10), 1500.0), h(RPM, t1, 1484.0), h(FREQ, t1, 49.7), h(RPM, S(t1, 10), 1500.0)]
r2 = drop(R2)
check(not r2[1] and kept(r2, t1), f"1б: 1484 при 49,7 не вырезан: {r2[1]}")
check(marks(R2)[t1][2] is False, f"1б: 1484 при 49,7 не провал: {marks(R2)[t1]}")

# ── 1в. 1418 при 47,7 — провал ──
R3 = [h(RPM, S(t1, -10), 1500.0), h(RPM, t1, 1418.0), h(FREQ, t1, 47.7), h(RPM, S(t1, 10), 1500.0)]
check(not drop(R3)[1], "1в: 1418 при 47,7 не вырезан")
check(marks(R3)[t1][2] is True, f"1в: 1418 при 47,7 — провал: {marks(R3)[t1]}")

# ── 1г. ДЭС №3 16.09 15:20:37: 1443, удержанная 49,9, строка 47,5 на +1 с ──
R4 = [h(FREQ, S(t1, -300), 49.9), h(RPM, S(t1, -9), 1500.0), h(RPM, t1, 1443.0), h(FREQ, S(t1, 1), 47.5),
      h(RPM, S(t1, 9), 1500.0), h(FREQ, S(t1, 10), 50.0)]
r4 = drop(R4, cfg=CFG3)
check(not r4[1] and kept(r4, t1), f"1г: запоздавшая частота — настоящий провал не вырезан: {r4[1]}")
m4 = marks(R4, cfg=CFG3)[t1]
check(m4[2] is True and m4[1] == 47.5, f"1г: провал по частоте опроса: {m4}")

# ── 1д. ДГУ №1 17.09 23:50:31: 749, удержанная 50,1, 0 Гц на +4 с ──
R5 = [h(FREQ, S(t1, -200), 50.1), h(RPM, S(t1, -30), 1500.0), h(RPM, t1, 749.0), h(FREQ, S(t1, 4), 0.0),
      h(RPM, S(t1, 30), 0.0)]
check(not drop(R5)[1], "1д: 749 при удержанной 50,1 не вырезан")
check(marks(R5)[t1][2] is True, f"1д: 749 при 0 Гц на +4 с — провал: {marks(R5)[t1]}")

# ── 1е. Выбег 494 при удержанной 50 Гц со следующим 0; холостой ход 913 при 50,4 ──
R6 = [h(FREQ, S(t1, -100), 50.0), h(RPM, S(t1, -10), 1500.0), h(RPM, t1, 494.0), h(RPM, S(t1, 10), 0.0)]
r6 = drop(R6)
check(not r6[1] and kept(r6, t1), "1е: выбег 494 не вырезан")
R7 = [h(RPM, S(t1, -10), 1500.0), h(RPM, t1, 913.0), h(FREQ, t1, 50.4), h(RPM, S(t1, 10), 1500.0)]
check(not drop(R7)[1], "1е: холостой ход 913 при 50,4 не вырезан")

# ── 1ж. Обороты 0 при 50 Гц — 1а не трогает, v* не провал, строка остаётся; 0 при 0 Гц — провал ──
R8 = [h(RPM, S(t1, -10), 1499.0), h(FREQ, S(t1, -10), 50.0), h(RPM, t1, 0.0), h(RPM, S(t1, 9), 1501.0)]
r8 = drop(R8)
check(not r8[1] and kept(r8, t1), "1ж: нулевой кадр 1а не трогает (решение 13.10)")
check(marks(R8)[t1][2] is False, f"1ж: обороты 0 при 50 Гц — не провал: {marks(R8)[t1]}")
R9 = [h(RPM, S(t1, -10), 1500.0), h(FREQ, S(t1, -5), 0.0), h(RPM, t1, 0.0)]
check(marks(R9)[t1][2] is True, f"1ж: обороты 0 при 0 Гц — провал: {marks(R9)[t1]}")

# ── 1з. RUN_STATE ≠ 3 — не вырезан ──
check(not drop(R1, enum=[rs(4, T(0, 0))])[1], "1з: охлаждение (4) — не вырезан")
check(not drop(R1, enum=[rs(3, T(0, 0), S(t1, -1)), rs(5, S(t1, -1))])[1], "1з: работа кончилась до отсчёта")

# ── 1и. Частоте не верим — сверки нет ──
base = [h(RPM, S(t1, -10), 1500.0), h(RPM, t1, 1479.0), h(RPM, S(t1, 10), 1499.0)]
# за дырой data_gaps
gap = [{"gap_start": S(t1, -80), "gap_end": S(t1, -40)}]
Rg = base + [h(FREQ, S(t1, -100), 50.1)]
check(not drop(Rg, gaps=gap)[1], "1и: частота за дырой связи — не вырезан")
check(drop(Rg)[1], "1и: та же частота без дыры — вырезан")
check(marks(Rg, gaps=gap)[t1][2] is True, "1и: за дырой провал по оборотам")
# старше 1800 с
check(not drop(base + [h(FREQ, S(t1, -1900), 50.1)])[1], "1и: удержанная частота старше 1800 с — не вырезан")
# пауза пакетов > 90 с без data_gaps
_P0 = P
P = pings(T(1, 30), T(2, 0), skip=(S(t1, -150), S(t1, -10)))
check(not drop(Rg)[1], "1и: пауза пакетов > 90 с — удержание сброшено, не вырезан")
P = _P0
# вне 40–55 Гц, 0, нет вовсе
check(not drop(base + [h(FREQ, t1, 55.5)])[1], "1и: частота 55,5 — не вырезан")
check(marks(base + [h(FREQ, t1, 55.5)])[t1][2] is True, "1и: частота вне 40–55 — провал по оборотам")
check(not drop(base + [h(FREQ, t1, 0.0)])[1], "1и: частота 0 — не вырезан")
check(not drop(base)[1], "1и: частоты нет — не вырезан")
check(marks(base)[t1][2] is True, "1и: частоты нет — провал по оборотам")
# одно значение опроса ниже 49,9 — не вырезан
check(not drop(base + [h(FREQ, S(t1, -20), 50.1), h(FREQ, S(t1, 3), 49.8)])[1],
      "1и: хоть одно значение опроса < 49,9 — не вырезан")
# следующий отсчёт позже минуты или ниже порога
check(not drop([h(RPM, t1, 1479.0), h(FREQ, t1, 50.1), h(RPM, S(t1, 70), 1499.0)])[1],
      "1и: следующий позже 60 с — не вырезан")
check(not drop([h(RPM, t1, 1479.0), h(FREQ, t1, 50.1), h(RPM, S(t1, 10), 1470.0)])[1],
      "1и: следующий ниже порога — не вырезан")

# ── 1к. Последний отсчёт без следующего — не вырезан (решает v*) ──
R10 = [h(RPM, S(t1, -10), 1500.0), h(RPM, t1, 1479.0), h(FREQ, t1, 50.1)]
check(not drop(R10)[1], "1к: край данных — не вырезан")
check(marks(R10, tt=S(t1, 5))[t1][2] is False, "1к: v* — не провал и на живом краю")

# ── 1л. Правило B (dip_needs_freq: false): по оборотам, живой край ждёт следующего ──
prmB = dict(rpm_freq_params(CFG), dip_needs_freq=False)
check(marks(R2, prm=prmB)[t1][2] is True, "1л: правило B — 1484 при 49,7 провал")
check(marks(R10, tt=S(t1, 30), prm=prmB)[t1][2] is None, "1л: правило B — край ждёт следующего отсчёта")
check(marks(R10, tt=S(t1, 61), prm=prmB)[t1][2] is True, "1л: правило B — через 60 с отсчёт учитывается")
check(marks(R3, tt=S(t1, 11), prm=prmB)[t1][2] is True, "1л: правило B — отсчёт со следующим не ждёт")
cfgB = copy.deepcopy(CFG)
cfgB.segmentation["rpm_freq_check"]["dip_needs_freq"] = False
check(rpm_freq_params(cfgB)["dip_needs_freq"] is False and rpm_freq_params(CFG)["dip_needs_freq"] is True,
      "1л: ключ dip_needs_freq читается из KB")
check(is_dip(1484.0, [49.7], prmB) and not is_dip(1484.0, [49.7], rpm_freq_params(CFG)),
      "1л: is_dip — v* и правило B")

# ── 1м. Выключатель 1а — свой, от zero_glitch не зависит ──
cfgOff = copy.deepcopy(CFG)
cfgOff.segmentation["rpm_freq_check"]["enabled"] = False
check(not drop(R1, cfg=cfgOff)[1], "1м: rpm_freq_check.enabled: false — фильтр выключен")
check(CFG.seg("zero_glitch", "enabled") is False and drop(R1)[1], "1м: фильтр нулей выключен, 1а работает")

# ── 1н. KB ──
_k = CFG.seg("rpm_freq_check")
check(_k.get("enabled") is True and _k.get("tolerance_rpm") == 10 and _k.get("freq_hold_max_sec") == 1800
      and _k.get("freq_valid_hz") == [40, 55] and _k.get("poll_window_max_sec") == 60
      and _k.get("next_max_gap_sec") == 60 and _k.get("min_rpm") == 1000
      and _k.get("dip_needs_freq") is True and _k.get("remark_min_count") == 3, f"1н: блок KB: {_k}")
_p = rpm_freq_params(CFG)
check(_p["k"] == 30.0 and _p["threshold_rpm"] == 1485.0 and _p["packet_max_gap_sec"] == 90.0,
      f"1н: k = 1500/50, порог 1485, пауза пакетов 90 с: {_p}")

# ── 1о. Через segment(): проверка source_rpm_glitch в МСК, минимум без сбоя, нет просадки ──
W0, W1 = T(1, 30), T(2, 0)


def seg_rows(glitches, extra=()):
    rows = [h(FREQ, W0, 50.0), h(OILT, W0, 80.0)] + pings(W0, W1)
    t = W0
    while t < W1:
        rows.append(h(RPM, t, 1500.0 if (t.second // 10) % 2 else 1499.0))
        t += timedelta(seconds=10)
    gl = {g: v for g, v in glitches}
    rows = [r for r in rows if not (r["addr"] == RPM and r["ts"] in gl)]
    rows += [h(RPM, g, v) for g, v in glitches] + list(extra)
    return sorted(rows, key=lambda r: r["ts"])


def run(rows, seed=None, cfg=CFG):
    return segment([rs(3, T(0, 0))], rows, [], [], cfg, "SN", "pcc", 1, "E", W0, W1, load_seed=seed)


def rpm_min(segs):
    v = [sub.characteristics.get("RPM", {}).get("min") for s in segs for sub in s.subsegments]
    v = [x for x in v if x is not None]
    return min(v) if v else None


def rg_check(segs):
    return [c for s in segs for c in s.sequence_checks if c.get("check") == "source_rpm_glitch"]


G3 = [(t1, 1479.0), (S(t1, 680), 1417.0), (S(t1, 760), 1460.0)]
s3 = run(seg_rows(G3))
c3 = rg_check(s3)
check(c3 and c3[0]["count"] == 3 and c3[0]["passed"] is False, f"1о: 3 сбоя за сегмент — passed=False (В4): {c3}")
check(c3 and "04:42:32 (1479 при 50 Гц)" in c3[0]["details"] and "UTC" not in c3[0]["details"]
      and "сбой датчика" in c3[0]["details"], f"1о: время МСК, обороты и частота: {c3 and c3[0]['details']}")
check(c3 and c3[0]["samples"][0]["t"] == t1.isoformat(), "1о: моменты сбоев — в samples")
check(rpm_min(s3) == 1499.0, f"1о: минимум оборотов без сбоев: {rpm_min(s3)}")
check(not [d for s in s3 for sub in s.subsegments for d in sub.detections if d.scenario == "RPM_UNDERSPEED"],
      "1о: сбои датчика просадки не дают")
s2_ = run(seg_rows(G3[:2]))
c2_ = rg_check(s2_)
check(c2_ and c2_[0]["count"] == 2 and c2_[0]["passed"] is True, f"1о: 2 сбоя — passed=True: {c2_}")
# Настоящий провал (частота подтверждает) остаётся
s4 = run(seg_rows([(t1, 1418.0)], extra=[h(FREQ, S(t1, 1), 47.3), h(FREQ, S(t1, 12), 50.0)]))
check(not rg_check(s4) and rpm_min(s4) == 1418.0, f"1о: настоящий провал остаётся: {rpm_min(s4)}")
# Сбой фильтра не роняет анализ окна
_orig = _sg._drop_rpm_glitches


def _boom(*a, **k):
    raise RuntimeError("тест")


_sg._drop_rpm_glitches = _boom
try:
    s5 = run(seg_rows(G3))
    check(s5 and rpm_min(s5) == 1417.0 and not rg_check(s5), "1о: сбой фильтра — данные как есть, без падения")
    check(logged("Сверка оборотов с частотой не выполнена"), "1о: сбой фильтра — запись в лог")
except Exception as e:  # noqa: BLE001
    check(False, f"1о: сбой фильтра уронил segment(): {e!r}")
finally:
    _sg._drop_rpm_glitches = _orig

# ── 1п. Удержанная частота перед окном (П10) доходит до фильтра ──
# Строк частоты в окне истории нет: без посева сверки нет, с посевом — сбой вырезан
rows_nf = [r for r in seg_rows([(S(W0, 25), 1479.0)]) if r["addr"] != FREQ]
check(not rg_check(run(rows_nf)), "1п: без удержанной частоты сбой не вырезан")
seed = {"held": {"FREQUENCY": {"ts": S(W0, -600), "value": 50.0}}, "held_gaps": []}
check(rg_check(run(rows_nf, seed=seed)), "1п: удержанная частота из load_seed — сбой вырезан")
seed_gap = {"held": {"FREQUENCY": {"ts": S(W0, -600), "value": 50.0}},
            "held_gaps": [{"gap_start": S(W0, -500), "gap_end": S(W0, -400)}]}
check(not rg_check(run(rows_nf, seed=seed_gap)), "1п: дыра между удержанной строкой и окном — сверки нет")
check(_sg._held_from_seed({"held": {"FREQUENCY": {"ts": S(W0, -60).isoformat(), "value": "50.1"}}}, CFG)
      == {"FREQUENCY": (S(W0, -60), 50.1)}, "1п: посев из строк ISO")
check(_sg._held_from_seed(None, CFG) == {} and _sg._held_from_seed({"ts": W0, "value": 1.0}, CFG) == {},
      "1п: без held — пусто (старый посев нагрузки)")

# ── 1р. Сводка и вход модели (В4): строка в «Замечаниях», на вердикт не влияет ──
check("source_rpm_glitch" in _DATA_QUALITY_CHECKS, "1р: проверка в _DATA_QUALITY_CHECKS")
_c = dict(c3[0]) if c3 else {}
_seg = Segment(id="s", router_sn="R", equip_type="pcc", panel_id=1, engine_sn="", run_state=3,
               run_state_label="Работа", t_start=W0.isoformat(), t_end=W1.isoformat(), duration_sec=1800,
               engine_hours_start=None, cause_open="REPORT_START", cause_close=None, preamble_included=False,
               data_quality=1.0, sequence_checks=[_c], events=[],
               subsegments=[Subsegment(id="s.1", parent_segment_id="s", t_start=W0.isoformat(),
                                       t_end=W1.isoformat(), duration_sec=1800, load_zone="NORMAL",
                                       cause_open="SEG_START", cause_close=None, characteristics={},
                                       derived_metrics=DerivedMetrics(), risk_accumulators=RiskAccumulators(),
                                       detections=[], data_quality=1.0)])
parts = build_summary_parts([_seg], [], tz=MSK, trip_roles=[])
check(parts["verdict"]["level"] == "НОРМА" and parts["verdict"]["remarks_n"] == 0,
      f"1р: вердикт НОРМА: {parts['verdict']}")
check(any(c.get("check") == "source_rpm_glitch" and "04:42:32" in c.get("details", "")
          for c in parts["failed_checks"]), f"1р: строка в замечаниях, МСК: {parts['failed_checks']}")
md = render_summary_md(parts)
check("❗ отброшены отсчёты оборотов" in md, f"1р: строка в сводке:\n{md}")
rem = _remarks_from_summary({"report_summary_json": parts}) or ""
check("[качество данных, на вердикт не влияет]" in rem, f"1р: вход модели: {rem}")
check("01:42:32" in _check_details(_c, U) and "04:42:32" in _check_details(_c, MSK),
      "1р: отчёт перепечатывает моменты в своём поясе")

# ══ П10. Удержанные значения на голове окна (_load_seed) ═══════════════════

TF = T(6, 0)
TH = TF - timedelta(seconds=300)
_calls: list[tuple] = []


def _fake_src(rows: dict, gaps: list):
    async def lvb(sn, et, pid, addrs, ts_before, max_age):
        _calls.append(("values", tuple(sorted(addrs)), ts_before, max_age))
        return {a: r for a, r in rows.items() if a in addrs}

    async def gd(sn, et, pid, a, b):
        _calls.append(("gaps", a, b))
        return [g for g in gaps if g["gap_start"] < b and (g["gap_end"] is None or g["gap_end"] > a)]
    return lvb, gd


_orig_src = (_src.get_last_values_before, _src.get_data_gaps)


def load_seed(rows, gaps=(), cfg=CFG):
    _calls.clear()
    _src.get_last_values_before, _src.get_data_gaps = _fake_src(rows, list(gaps))
    try:
        return asyncio.run(eng._load_seed("SN", "pcc", 1, TF, cfg))
    finally:
        _src.get_last_values_before, _src.get_data_gaps = _orig_src


# Нагрузки нет — удержанные значения всё равно отдаются (раньше None)
sd = load_seed({FREQ: {"ts": S(TH, -700), "value": 50.1}, RPM: {"ts": S(TH, -60), "value": 1500.0},
                OILT: {"ts": S(TH, -1700), "value": 97.0}})
check(sd and set(sd["held"]) == {"FREQUENCY", "RPM", "OIL_TEMP"} and "ts" not in sd,
      f"П10: без нагрузки — held есть, ключей нагрузки нет: {sd}")
check(sd and sd["held"]["FREQUENCY"] == {"ts": S(TH, -700), "value": 50.1}, "П10: значение и метка частоты")
check([c[0] for c in _calls] == ["values", "gaps"], f"П10: один запрос значений и один дыр: {_calls}")
check(_calls and set(_calls[0][1]) >= {RPM, FREQ, OILT, LOAD, CFG.role_to_addr("COOLANT_TEMP"),
                                       CFG.role_to_addr("OIL_PRESS")}
      and _calls[0][2] == TH and _calls[0][3] == 1800.0, f"П10: адреса, граница и возраст: {_calls[:1]}")
check(len(_calls) > 1 and _calls[1][1] == S(TH, -1700) and _calls[1][2] == TF,
      "П10: дыры — от самой старой строки до окна")
# Значение за дырой отбрасывается, нагрузка — с дырами от своей строки
sd2 = load_seed({FREQ: {"ts": S(TH, -900), "value": 50.0}, RPM: {"ts": S(TH, -60), "value": 1500.0},
                 LOAD: {"ts": S(TH, -1000), "value": 70.0}},
                gaps=[{"gap_start": S(TH, -800), "gap_end": S(TH, -700)}])
check(sd2 and "FREQUENCY" not in sd2["held"] and "RPM" in sd2["held"], f"П10: частота за дырой отброшена: {sd2}")
check(sd2 and sd2.get("ts") == S(TH, -1000) and sd2.get("value") == 70.0 and len(sd2.get("gaps") or []) == 1
      and len(sd2.get("held_gaps") or []) == 1, f"П10: нагрузка с дырой, как раньше: {sd2}")
# Старше 1800 с — не удержанное
sd3 = load_seed({FREQ: {"ts": S(TH, -1801), "value": 50.0}})
check(sd3 is not None and sd3["held"] == {}, f"П10: старше 1800 с — нет: {sd3}")
check(load_seed({}) is None, "П10: строк нет — None")


async def _raise(*a, **k):
    raise RuntimeError("тест")


_src.get_last_values_before = _raise
try:
    check(asyncio.run(eng._load_seed("SN", "pcc", 1, TF, CFG)) is None, "П10: ошибка БД — None, без падения")
    check(logged("Удержанные значения перед окном не прочитаны"), "П10: ошибка БД — запись в лог")
finally:
    _src.get_last_values_before = _orig_src[0]

# Кэш: один запрос на курсор; рез берёт посев из того же кэша
_n = {"n": 0}
_orig_ls = eng._load_seed


async def _count_seed(*a, **k):
    _n["n"] += 1
    return {"held": {}, "held_gaps": []}


eng._load_seed = _count_seed
try:
    fake = SimpleNamespace(router_sn="SN", equip_type="pcc", panel_id=1, cfg=CFG, _load_seed_cache=None)
    for _ in range(3):
        asyncio.run(eng.OnlinePollEngine._open_load_seed(fake, TF))
    asyncio.run(eng.OnlinePollEngine._open_load_seed(fake, TF + timedelta(days=1)))
    check(_n["n"] == 2, f"П10: кэш на курсор: {_n['n']} запросов")
finally:
    eng._load_seed = _orig_ls
_cw = inspect.getsource(eng.OnlinePollEngine._close_window)
check("_open_load_seed(t_from)" in _cw and _cw.count("load_seed=_seed") == 2,
      "П10: рез — посев из кэша курсора в обоих прогонах")
check("load_seed" in inspect.signature(eng._run_segment).parameters, "П10: _run_segment принимает посев")
check(_rf.glitch_details([{"t": t1.isoformat(), "rpm": 1479.0, "hz": 50.1}]).endswith("04:42:32 (1479 при 50,1 Гц)"),
      "1в: текст по умолчанию — МСК")

# ══ 2. Участки (П2, этап 2: analytics/live.py) ══════════════════════════════
import analytics.live as _lv  # noqa: E402
from analytics.detectors import (  # noqa: E402
    PASSPORT_RECENT_SEC, _combined_oil_detection, _passport_detection, run_all_detectors,
)
from analytics.live import LIVE_RECENT_SEC, held_before, segment_spans  # noqa: E402

COOL3, PRESS3, LOAD3 = (CFG3.role_to_addr(r) for r in ("COOLANT_TEMP", "OIL_PRESS", "LOAD_PCT"))
L0 = T(6, 0)            # 09:00 МСК — суточный рез
L1 = S(L0, 1800)
PL = pings(S(L0, -300), S(L1, 60))


def sp(rows, t0=L0, t1=L1, gaps=None, held=None, carry=False, cfg=CFG3, horizon=None):
    return segment_spans(by_addr(rows), None, cfg, t0, t1, gaps or [], held or {}, carry, horizon=horizon)


def iso(t):
    return t.isoformat()


def cl(res, key="LIMIT_PROXIMITY"):
    return res.get(key) or []


check(LIVE_RECENT_SEC == 600 and PASSPORT_RECENT_SEC == LIVE_RECENT_SEC,
      "2: N = 600 с (В2), PASSPORT_RECENT_SEC — псевдоним")

# ── 2а. Удержание 99,0007 без новых строк 20 мин при идущих пакетах — участок идёт ──
r = sp(PL + [h(OILT, S(L0, 60), 97.0), h(OILT, S(L0, 600), 99.0007)])
c = cl(r)
check(len(c) == 1 and c[0]["start"] == iso(S(L0, 600)) and c[0]["end"] == iso(L1) and c[0]["ongoing"]
      and c[0]["blind_sec"] == 0 and set(c[0]["roles"]) == {"OIL_TEMP"} and not c[0]["carried"],
      f"2а: удержание при идущих пакетах — участок идёт до края окна: {c}")
check(c and c[0]["viol_sec"] == 1200 and c[0]["vmax"] == 99.0007, f"2а: чистое время и значение: {c}")

# ── 2б. Пауза пакетов > 90 с без data_gaps — конец на последнем пакете, слепой доли нет ──
Pp = pings(S(L0, -300), S(L1, 60), skip=(S(L0, 900), S(L0, 1100)))
c = cl(sp(Pp + [h(OILT, S(L0, 600), 99.0007)]))
check(len(c) == 1 and c[0]["end"] == iso(S(L0, 900)) and not c[0]["ongoing"] and c[0]["blind_sec"] == 0
      and c[0]["viol_sec"] == 300, f"2б: пауза — конец на последнем пакете, слепой доли нет: {c}")
# После паузы свежая строка — новый участок (пауза 200 с < N — тот же кластер)
c = cl(sp(Pp + [h(OILT, S(L0, 600), 99.0007), h(OILT, S(L0, 1101), 99.0007)]))
check(len(c) == 1 and c[0]["spans"] == 2 and c[0]["viol_sec"] == 300 + 699 and c[0]["ongoing"],
      f"2б: после паузы — новый участок того же кластера: {c}")

# ── 2в. Дыра data_gaps участок не рвёт: слепая доля, конец — первый пакет после дыры ──
G = [{"gap_start": S(L0, 900), "gap_end": S(L0, 1100)}]
c = cl(sp(Pp + [h(OILT, S(L0, 600), 99.0007)], gaps=G))
check(len(c) == 1 and c[0]["end"] == iso(S(L0, 1100)) and c[0]["blind_sec"] == 200 and not c[0]["ongoing"]
      and c[0]["viol_sec"] == 500, f"2в: дыра — до первого пакета после неё, слепо 200 с: {c}")
c = cl(sp(Pp + [h(OILT, S(L0, 600), 99.0007), h(OILT, S(L0, 1101), 99.5)], gaps=G))
check(len(c) == 1 and c[0]["spans"] == 1 and c[0]["ongoing"] and c[0]["blind_sec"] == 200,
      f"2в: в первом пакете после дыры — нарушение: участок продолжается: {c}")
c = cl(sp(Pp + [h(OILT, S(L0, 600), 99.0007), h(OILT, S(L0, 1101), 97.0)], gaps=G))
check(len(c) == 1 and c[0]["end"] == iso(S(L0, 1101)), f"2в: после дыры — норма: конец на ней: {c}")
# Открытая дыра (синтетический хвост открытого окна) — нарушение «идёт», хвост — слепая доля
Pt = pings(S(L0, -300), S(L0, 1000))
c = cl(sp(Pt + [h(OILT, S(L0, 600), 99.0007)], gaps=[{"gap_start": S(L0, 990), "gap_end": None}]))
check(len(c) == 1 and c[0]["ongoing"] and c[0]["end"] == iso(L1) and c[0]["blind_sec"] == 810,
      f"2в: обрыв связи на нарушении — идёт, слепая доля — хвост: {c}")
c = cl(sp(Pt + [h(OILT, S(L0, 600), 99.0007)]))
check(len(c) == 1 and not c[0]["ongoing"] and c[0]["end"] == iso(S(L0, 990)),
      f"2в: пакеты кончились без data_gaps — конец на последнем пакете: {c}")

# ── 2г. Пол давления (В3): ниже уставки останова — не замер ──
r = sp(PL + [h(PRESS3, S(L0, 60), 300.0), h(PRESS3, S(L0, 120), 0.0), h(PRESS3, S(L0, 130), 316.0)])
check(not cl(r), f"2г: нулевой кадр давления (ДЭС №3 13.10 14:59:56) — не близость: {cl(r)}")
c = cl(sp(PL + [h(PRESS3, S(L0, 60), 300.0), h(PRESS3, S(L0, 600), 185.0), h(PRESS3, S(L0, 700), 300.0)]))
check(len(c) == 1 and set(c[0]["roles"]) == {"OIL_PRESS"} and c[0]["start"] == iso(S(L0, 600))
      and c[0]["end"] == iso(S(L0, 700)) and c[0]["vmin"] == 185.0,
      f"2г: 185 < 172·1,1 — близость давления: {c}")
c = cl(sp(PL + [h(PRESS3, S(L0, 600), 185.0), h(PRESS3, S(L0, 650), 0.0), h(PRESS3, S(L0, 700), 300.0)]))
check(len(c) == 1 and c[0]["end"] == iso(S(L0, 700)), f"2г: ноль внутри участка пропускается: {c}")

# ── 2д. Удержанные значения на начало сегмента (held_before) ──
hb_rows = by_addr(PL + [h(OILT, S(L0, -100), 99.5), h(RPM, S(L0, -50), 1500.0)])
hd = held_before(hb_rows, CFG3, L0, {"COOLANT_TEMP": (S(L0, -900), 95.0)}, [])
check(hd.get("OIL_TEMP") == (S(L0, -100), 99.5) and hd.get("RPM") == (S(L0, -50), 1500.0)
      and hd.get("COOLANT_TEMP") == (S(L0, -900), 95.0), f"2д: строка окна истории, иначе посев: {hd}")
hd = held_before(hb_rows, CFG3, L0, None, [{"gap_start": S(L0, -80), "gap_end": S(L0, -60)}])
check("OIL_TEMP" not in hd and "RPM" in hd, f"2д: за дырой значение неизвестно: {hd}")
hd = held_before(by_addr(pings(S(L0, -300), S(L0, -250)) + pings(S(L0, -100), L0)
                         + [h(OILT, S(L0, -280), 99.5)]), CFG3, L0)
check("OIL_TEMP" not in hd, f"2д: пауза пакетов после строки — значение неизвестно: {hd}")


# ── 2е. Через segment(): перенос при REPORT_START, без переноса при смене режима ──
def work_rows(a=L0, b=L1, extra=(), pre=300, rpm=1500.0):
    rows = pings(S(a, -pre), b)
    t, k = S(a, -pre), 0
    while t < b:
        rows += [h(RPM, t, rpm + (k % 2)), h(FREQ, t, 50.0)]
        t += timedelta(seconds=10)
        k += 1
    return sorted(rows + list(extra), key=lambda r: r["ts"])


def seg3(rows, enum, seed=None, gaps=None, a=L0, b=L1, cfg=CFG3):
    return segment(enum, rows, [], gaps or [], cfg, "SN", "pcc", 1, "E", a, b, load_seed=seed)


def dets(segs, scen, work_only=True):
    return [(sub, d) for s in segs for sub in s.subsegments for d in sub.detections
            if d.scenario == scen and not (work_only and sub.transient)]


SPANS_CALLS: list = []
_orig_spans = _sg.segment_spans


def _rec_spans(*a, **k):
    res = _orig_spans(*a, **k)
    SPANS_CALLS.append((a, k, res))
    return res


_sg.segment_spans = _rec_spans
RS_REP = [rs(3, T(0, 0))]                       # режим начался до окна — REPORT_START
RS_CHG = [rs(0, T(0, 0), L0), rs(3, L0)]        # смена режима на начале окна
seed_ot = {"held": {"OIL_TEMP": {"ts": S(L0, -600), "value": 99.0007}}, "held_gaps": []}
SPANS_CALLS.clear()
s_rep = seg3(work_rows(extra=[h(OILT, S(L0, 300), 97.0)]), RS_REP, seed=seed_ot)
d_rep = dets(s_rep, "LIMIT_PROXIMITY")
check(SPANS_CALLS and SPANS_CALLS[-1][1].get("carry") is True, "2е: REPORT_START без разгона — перенос")
check(len(d_rep) == 1 and d_rep[0][1].t_detected == iso(L0)
      and d_rep[0][1].values["live"]["clusters"][0]["carried"]
      and d_rep[0][1].values["live"]["clusters"][0]["end"] == iso(S(L0, 300)),
      f"2е: удержанные 99,0007 переносятся с 09:00 до первой строки в норме: "
      f"{[(d.t_detected, d.values.get('live')) for _, d in d_rep]}")
SPANS_CALLS.clear()
s_chg = seg3(work_rows(extra=[h(OILT, S(L0, 300), 97.0)]), RS_CHG, seed=seed_ot)
check(SPANS_CALLS and SPANS_CALLS[-1][1].get("carry") is False and not dets(s_chg, "LIMIT_PROXIMITY"),
      f"2е: смена режима — переноса нет: {[d.trigger for _, d in dets(s_chg, 'LIMIT_PROXIMITY')]}")

# ── 2ж. Строка до окна за дырой связи детекции не даёт (раньше — копия forward-fill) ──
gap_pre = [{"gap_start": S(L0, -25), "gap_end": S(L0, -5)}]
rows_pre = [r for r in work_rows(extra=[h(OILT, S(L0, -30), 99.5), h(OILT, S(L0, 40), 95.0)])
            if not (r["addr"] == HB and S(L0, -25) < r["ts"] < S(L0, -5))]
check(not dets(seg3(rows_pre, RS_REP, gaps=gap_pre), "LIMIT_PROXIMITY"),
      "2ж: значение до окна за дырой — ни переноса, ни детекции")
_sg.segment_spans = _boom
try:
    check(dets(seg3(rows_pre, RS_REP, gaps=gap_pre), "LIMIT_PROXIMITY"),
          "2ж: (для сравнения) по подсегменту строка до окна давала детекцию")
finally:
    _sg.segment_spans = _rec_spans

# ── 2з. Переходный кусок (разгон) участков не получает ──
run_up = [h(RPM, S(L0, -300), 0.0), h(FREQ, S(L0, -300), 0.0), h(RPM, S(L0, 5), 300.0),
          h(RPM, S(L0, 20), 900.0), h(OILT, S(L0, 10), 99.5), h(FREQ, S(L0, 40), 50.0)]
rows_up = sorted(pings(S(L0, -300), L1) + run_up
                 + [h(RPM, S(L0, 40 + 10 * k), 1500.0 + k % 2) for k in range(170)], key=lambda r: r["ts"])
SPANS_CALLS.clear()
s_up = seg3(rows_up, RS_CHG)
tr = [sub for s in s_up for sub in s.subsegments if sub.transient]
check(tr and SPANS_CALLS and SPANS_CALLS[-1][0][3] == S(L0, 40) and SPANS_CALLS[-1][1].get("carry") is False,
      f"2з: окно участков — с выхода на обороты: {SPANS_CALLS and SPANS_CALLS[-1][0][3]}")
check(not any("live" in (d.values or {}) for sub in tr for d in sub.detections),
      "2з: переходный кусок — без участков")
check(not dets(s_up, "LIMIT_PROXIMITY", work_only=False) and not dets(s_up, "RPM_UNDERSPEED", work_only=False),
      "2з: разгон и строка в нём — ни близости, ни просадки")

# ── 2и. Паспорт не короче 60 с через границу зоны — детекция в обоих подсегментах, без падения ──
def zone_rows(press, load_from=600):
    ex = [h(LOAD3, S(L0, 10 * k), 50.0 if 10 * k < load_from else 80.0) for k in range(180)]
    ex += [h(PRESS3, t, v) for t, v in press]
    return work_rows(extra=ex)


z_rows = zone_rows([(S(L0, 0), 250.0), (S(L0, 560), 236.0), (S(L0, 640), 250.0)])
try:
    s_z = seg3(z_rows, RS_REP)
    d_z = dets(s_z, "OIL_DILUTION")
    subs_z = [sub for s in s_z for sub in s.subsegments if not sub.transient]
    check(len(subs_z) >= 2 and len(d_z) == 2 and len({d.t_detected for _, d in d_z}) == 1
          and d_z[0][1].t_detected == iso(S(L0, 560)),
          f"2и: участок 80 с через границу зоны — в обоих подсегментах, начало одно: "
          f"{[(sub.t_start, d.t_detected) for sub, d in d_z]}")
    lim = d_z[0][1].values["limit"] if d_z else {}
    check(lim.get("hard") is True and lim.get("value") == 236.0 and lim.get("run_sec") == 80.0
          and lim.get("until") == iso(S(L0, 640)) and lim.get("ongoing") is False
          and lim.get("hard_until") == iso(S(L0, 640)) and lim.get("hard_ongoing") is False,
          f"2и: поля паспорта — из кластера, «жёсткая» часть: {lim}")
    check(d_z and d_z[1][1].values["live"]["first"] == iso(S(L0, 600))
          and d_z[0][1].values["live"]["onset"] == iso(S(L0, 560)),
          f"2и: first — первое нарушение в подсегменте, onset — начало кластера: {d_z and d_z[1][1].values['live']}")
except Exception as e:  # noqa: BLE001
    check(False, f"2и: паспорт через границу зоны уронил segment(): {e!r}")
_sg.segment_spans = _boom
try:
    check(not dets(seg3(z_rows, RS_REP), "OIL_DILUTION"),
          "2и: (для сравнения) по подсегменту по 40 с — выдержку не набирал ни один")
finally:
    _sg.segment_spans = _rec_spans

# ── 2к. Перенесённый паспорт 40 + 40 с квалифицируется (от удержанной строки) ──
rows_40 = work_rows(extra=[h(PRESS3, S(L0, -200), 250.0), h(PRESS3, S(L0, -40), 236.0),
                           h(PRESS3, S(L0, 40), 250.0)])
d40 = dets(seg3(rows_40, RS_REP), "OIL_DILUTION")
c40 = d40[0][1].values["live"]["clusters"][0] if d40 else {}
check(len(d40) == 1 and d40[0][1].t_detected == iso(L0) and c40.get("carried") and c40.get("run_sec") == 80.0
      and c40.get("end") == iso(S(L0, 40)),
      f"2к: 40 с до реза + 40 с после — участок с 09:00, выдержка набрана: {c40}")
check(not dets(seg3(rows_40, RS_CHG), "OIL_DILUTION"), "2к: при смене режима — 40 с, выдержки нет")
# Удержанная строка — повтор того же значения: начало участка — по ряду окна истории
rows_rep = work_rows(extra=[h(PRESS3, S(L0, -200), 250.0), h(PRESS3, S(L0, -70), 236.0),
                            h(PRESS3, S(L0, -10), 236.0), h(PRESS3, S(L0, 20), 250.0)])
d_rp = dets(seg3(rows_rep, RS_REP), "OIL_DILUTION")
check(len(d_rp) == 1 and d_rp[0][1].values["live"]["clusters"][0]["run_sec"] == 90.0,
      f"2к: повтор 236 в 08:59:50 — длительность от 08:58:50 (70 + 20 с): "
      f"{d_rp and d_rp[0][1].values['live']['clusters'][0]}")

# ══ 3. Кластеры ═════════════════════════════════════════════════════════════
# ── 3а. Пауза 666 с между участками — два кластера (ДЭС №3 13.10 17:45:52 → 17:56:58) ──
def two(gap_s, gaps=None, pk=PL):
    return cl(sp(pk + [h(OILT, S(L0, 100), 99.0007), h(OILT, S(L0, 160), 97.0),
                       h(OILT, S(L0, 160 + gap_s), 99.0007), h(OILT, S(L0, 200 + gap_s), 97.0)], gaps=gaps))


check(len(two(666)) == 2, f"3а: пауза 666 с > N — два кластера: {two(666)}")
check(len(two(600)) == 1 and two(600)[0]["spans"] == 2 and two(600)[0]["viol_sec"] == 100,
      f"3а: 600 с — один кластер из двух участков: {two(600)}")
# ── 3б. N — по видимому времени: дыра между участками не считается ──
Pg = pings(S(L0, -300), S(L1, 60), skip=(S(L0, 300), S(L0, 500)))
c = two(700, gaps=[{"gap_start": S(L0, 300), "gap_end": S(L0, 500)}], pk=Pg)
check(len(c) == 1 and c[0]["blind_sec"] == 200, f"3б: 700 с, из них 200 без связи — один кластер: {c}")
check(len(two(700, pk=Pg)) == 2, "3б: те же 700 с без data_gaps — два кластера")
# ── 3в. Близость — объединение ролей, участки по ролям внутри кластера ──
r = sp(PL + [h(OILT, S(L0, 100), 99.0007), h(OILT, S(L0, 400), 97.0),
             h(COOL3, S(L0, 50), 85.0), h(COOL3, S(L0, 300), 94.0), h(COOL3, S(L0, 500), 85.0)])
c = cl(r)
check(len(c) == 1 and c[0]["start"] == iso(S(L0, 100)) and c[0]["end"] == iso(S(L0, 500))
      and c[0]["viol_sec"] == 400 and c[0]["roles"]["OIL_TEMP"]["viol_sec"] == 300
      and c[0]["roles"]["COOLANT_TEMP"]["viol_sec"] == 200 and c[0]["roles"]["COOLANT_TEMP"]["vmax"] == 94.0,
      f"3в: один кластер на три роли, участки по ролям: {c}")
cf = cl(r, "COOLING_FAILURE")
check(len(cf) == 1 and cf[0]["start"] == iso(S(L0, 300)) and cf[0]["end"] == iso(S(L0, 500)),
      f"3в: COOLING_FAILURE — свой ключ (≥ 0,9·HCT): {cf}")
check(all(not k.startswith("_") for k in _lv.public(c[0])) and "_spans" in c[0],
      "3в: служебные поля — только в памяти")

# ══ 4. Детекторы (П3) ═══════════════════════════════════════════════════════
DM0 = DerivedMetrics()


def run_det(chars, sub_a, sub_b, live, derived=DM0, cfg=CFG3):
    return run_all_detectors(chars, derived, RiskAccumulators(), [], "NORMAL", 3, sub_a, sub_b, None, cfg,
                             live_spans=live)


# ── 4а. Участок пересекает подсегмент — детекция есть; не пересекает — нет ──
live_lp = sp(PL + [h(OILT, S(L0, 100), 99.0007), h(OILT, S(L0, 400), 97.0)])
chars_hot = {"OIL_TEMP": {"min": 95.0, "max": 99.5, "median": 97.0}}
dA = [d for d in run_det(chars_hot, S(L0, 300), S(L0, 600), live_lp) if d.scenario == "LIMIT_PROXIMITY"]
dB = [d for d in run_det(chars_hot, S(L0, 400), S(L0, 600), live_lp) if d.scenario == "LIMIT_PROXIMITY"]
check(len(dA) == 1 and dA[0].t_detected == iso(S(L0, 100)) and dA[0].values["role"] == "OIL_TEMP"
      and dA[0].values["value"] == 99.0007 and dA[0].trigger.startswith("Температура масла: 99.00"),
      f"4а: участок в подсегменте — детекция, начало кластера, значение по отсчётам: {[d.values for d in dA]}")
check(not dB, f"4а: участок кончился на начале подсегмента — детекции нет (max 99,5 в характеристиках): {dB}")
lvA = dA[0].values["live"] if dA else {}
check(set(lvA) == {"clusters", "onset", "first", "until", "ongoing"} and lvA["first"] == iso(S(L0, 300))
      and lvA["onset"] == iso(S(L0, 100)) and lvA["until"] == iso(S(L0, 400)) and lvA["ongoing"] is False,
      f"4а: values.live: {lvA}")
import json  # noqa: E402
try:
    json.dumps(dA[0].to_dict() if dA else {})
except TypeError as e:
    check(False, f"4а: детекция с live не сериализуется: {e}")
# Ключа нет — детекции нет (по характеристикам была бы)
check(not [d for d in run_det(chars_hot, S(L0, 300), S(L0, 600), {}) if d.scenario == "LIMIT_PROXIMITY"],
      "4а: словарь без ключа — детекции нет")

# ── 4б. live_spans=None — прежний вывод ──
d_old = run_det(dict(chars_hot, RPM={"min": 1473, "max": 1510, "median": 1497},
                     COOLANT_TEMP={"min": 85, "max": 90, "median": 86, "value_start": 85, "value_end": 86}),
                S(L0, 300), S(L0, 600), None)
by_sc = {d.scenario: d for d in d_old}
check(set(by_sc) >= {"LIMIT_PROXIMITY", "RPM_UNDERSPEED", "COOLING_FAILURE"}
      and all(d.t_detected == iso(S(L0, 300)) and "live" not in d.values for d in d_old),
      f"4б: None — по характеристикам, начало подсегмента, без live: {[(d.scenario, d.t_detected) for d in d_old]}")
check(set(by_sc["LIMIT_PROXIMITY"].values) == {"value", "shutdown_limit", "proximity_pct"}
      and by_sc["RPM_UNDERSPEED"].trigger == "RPM мин. 1473 об/мин — на 12 ниже порога 1485 "
      "(размах 37 об/мин от макс. 1510), фронтов: 1" and "freq_rpm_min" not in by_sc["RPM_UNDERSPEED"].values,
      f"4б: прежние поля и текст: {by_sc['RPM_UNDERSPEED'].trigger}")
dm_pp = DerivedMetrics(oil_press_passport_kpa=242, oil_press_step_kpa=8, oil_press_step_source="history",
                       oil_press_below_passport_run_sec=90, oil_press_below_passport_min_kpa=240,
                       oil_press_instep_min_kpa=240, oil_press_passport_until=iso(S(L0, 500)),
                       oil_press_passport_ongoing=False, oil_press_last_min_kpa=240)
pp_old = _passport_detection(dm_pp, S(L0, 300), CFG3)
check(pp_old and pp_old.t_detected == iso(S(L0, 300)) and "live" not in pp_old.values
      and pp_old.values["limit"]["until"] == iso(S(L0, 500)), "4б: паспорт без участков — по DerivedMetrics")

# ── 4в. Паспорт по участкам: поля из кластера, без нужного поля — детекции нет (без падения) ──
live_pp = sp(PL + [h(RPM, S(L0, 10 * k), 1500.0 + k % 2) for k in range(180)]
             + [h(PRESS3, S(L0, 0), 250.0), h(PRESS3, S(L0, 100), 240.0), h(PRESS3, S(L0, 200), 250.0)],
             cfg=CFG3)
ppc = cl(live_pp, "OIL_DILUTION")
check(len(ppc) == 1 and ppc[0]["hard"] is False and ppc[0]["vmin"] == 240.0 and ppc[0]["run_sec"] == 100.0
      and "hard_until" not in ppc[0], f"4в: 240 при паспорте 242 и шаге 4 — в пределах шага: {ppc}")
dm_lim = DerivedMetrics(oil_press_passport_kpa=242, oil_press_step_kpa=4, oil_press_step_source="default")
pp_new = _passport_detection(dm_lim, S(L0, 150), CFG3, live=live_pp["OIL_DILUTION"], seg_end=S(L0, 300))
check(pp_new and pp_new.t_detected == iso(S(L0, 100)) and pp_new.values["limit"]["hard"] is False
      and pp_new.values["limit"]["value"] == 240.0 and pp_new.values["limit"]["until"] == iso(S(L0, 200))
      and pp_new.values["limit"]["hard_until"] is None and "в пределах шага" in pp_new.trigger,
      f"4в: из кластера при пустых полях подсегмента: {pp_new and pp_new.values}")
check(_passport_detection(DerivedMetrics(), S(L0, 150), CFG3, live=live_pp["OIL_DILUTION"],
                          seg_end=S(L0, 300)) is None, "4в: паспортного предела нет — детекции нет, без падения")
check(_passport_detection(dm_lim, S(L0, 250), CFG3, live=live_pp["OIL_DILUTION"], seg_end=S(L0, 300)) is None,
      "4в: участок не пересекает подсегмент — детекции нет")

# ── 4г. Комбинированная ветка OIL_DILUTION — без участков, с полом давления (В3) ──
# v5.1.23 (В6 = А): в KB ветка выключена (combined_enabled: false) — логику
# пола проверяем на копии конфига с включённой веткой; выключатель —
# scripts/test_rpm_start_v5123.py, раздел 10
CFG3_COMB = copy.copy(CFG3)
CFG3_COMB.detectors = dict(CFG3.detectors,
                           OIL_DILUTION=dict(CFG3.detectors["OIL_DILUTION"], combined_enabled=True))
ch_zero = {"OIL_PRESS": {"min": 0.0, "median": 300.0}, "OIL_TEMP": {"max": 104.0, "median": 92.0}}
ch_real = {"OIL_PRESS": {"min": 200.0, "median": 300.0}, "OIL_TEMP": {"max": 104.0, "median": 92.0}}
check(_combined_oil_detection(ch_zero, L0, CFG3_COMB) is None,
      "4г: минимум ниже уставки останова — ветка молчит (14902)")
check(_combined_oil_detection(ch_real, L0, CFG3_COMB) is not None, "4г: настоящее падение давления — ветка работает")
d_comb = [d for d in run_det(ch_real, L0, S(L0, 300), {}, cfg=CFG3_COMB) if d.scenario == "OIL_DILUTION"]
check(len(d_comb) == 1 and d_comb[0].description_key.endswith("combined_pressure_temp_anomaly")
      and "live" not in d_comb[0].values, f"4г: комбинированная ветка при участках — без live: {d_comb}")

# ── 4д. Просадка оборотов по v* через segment(): сбой — нет, провал — rpm_min, по частоте, фронты ──
dip_rows = work_rows()
for r_ in dip_rows:
    if r_["ts"] == S(L0, 300) and r_["addr"] in (RPM, FREQ):
        r_["value"] = 1418.0 if r_["addr"] == RPM else 47.3
s_dip = seg3(dip_rows, RS_REP)
d_dip = dets(s_dip, "RPM_UNDERSPEED")
v_dip = d_dip[0][1].values if d_dip else {}
check(len(d_dip) == 1 and d_dip[0][1].t_detected == iso(S(L0, 300)) and v_dip.get("rpm_min") == 1418.0
      and v_dip.get("freq_rpm_min") == 1419.0 and v_dip.get("front_count") == 1
      and "RPM мин. 1418 об/мин (по частоте ≈ 1419) — на 67 ниже порога 1485" in d_dip[0][1].trigger,
      f"4д: провал 1418 при 47,3 Гц: {d_dip and d_dip[0][1].trigger}")
check(v_dip.get("live", {}).get("clusters", [{}])[0].get("end") == iso(S(L0, 310)),
      f"4д: участок провала — до первого отсчёта в норме: {v_dip.get('live')}")
gl_rows = work_rows()
for r_ in gl_rows:
    if r_["addr"] == RPM and r_["ts"] in (S(L0, 300), S(L0, 600)):
        r_["value"] = 1479.0 if r_["ts"] == S(L0, 300) else 1484.0
    if r_["addr"] == FREQ and r_["ts"] == S(L0, 600):
        r_["value"] = 49.7
s_gl = seg3(gl_rows, RS_REP)
check(not dets(s_gl, "RPM_UNDERSPEED"), "4д: сбой (вырезан) и 1484 при 49,7 Гц — просадки нет (жалоба #35890)")
# Два провала в одном подсегменте — два участка: фронтов 2
two_rows = work_rows(extra=[h(FREQ, S(L0, 301), 47.0), h(FREQ, S(L0, 311), 50.0),
                            h(FREQ, S(L0, 501), 47.5), h(FREQ, S(L0, 511), 50.0)])
for r_ in two_rows:
    if r_["addr"] == RPM and r_["ts"] in (S(L0, 300), S(L0, 500)):
        r_["value"] = 1410.0 if r_["ts"] == S(L0, 300) else 1425.0
d_two = dets(seg3(two_rows, RS_REP), "RPM_UNDERSPEED")
check(len(d_two) == 1 and d_two[0][1].values["front_count"] == 2 and d_two[0][1].values["rpm_min"] == 1410.0
      and d_two[0][1].values["freq_rpm_min"] == 1410.0 and len(d_two[0][1].values["live"]["clusters"]) == 1,
      f"4д: два провала в 200 с — один кластер, фронтов 2: {d_two and d_two[0][1].values}")
# Правило B на живом краю: последний отсчёт ниже порога без следующего — пока не учитывается
prmB_cfg = copy.deepcopy(CFG3)
prmB_cfg.segmentation["rpm_freq_check"]["dip_needs_freq"] = False
edge = PL + [h(RPM, S(L0, 10 * k), 1500.0 + k % 2) for k in range(100)] + [h(RPM, S(L0, 1000), 1480.0)]
check(not cl(sp(edge, cfg=prmB_cfg, horizon=S(L0, 1030)), "RPM_UNDERSPEED"),
      "4д: правило B — край окна ждёт следующего отсчёта")
check(cl(sp(edge, cfg=prmB_cfg, horizon=S(L0, 1061)), "RPM_UNDERSPEED"),
      "4д: правило B — через 60 с отсчёт учитывается")

# ── 4ж. COOLING_FAILURE: близость — по участкам, прогноз прогрева — без них ──
d_cf = [d for d in run_det({"COOLANT_TEMP": {"max": 94.0, "value_start": 86, "value_end": 85}},
                           S(L0, 400), S(L0, 700), r) if d.scenario == "COOLING_FAILURE"]
check(len(d_cf) == 1 and d_cf[0].t_detected == iso(S(L0, 300)) and d_cf[0].values["coolant_temp_max"] == 94.0
      and d_cf[0].values["live"]["clusters"][0]["end"] == iso(S(L0, 500)),
      f"4ж: близость ОЖ — удержанное 94 в подсегменте, начало кластера: {[d.values for d in d_cf]}")
warm = {"COOLANT_TEMP": {"max": 89.0, "value_start": 70.0, "value_end": 84.0}}
d_w = [d for d in run_all_detectors(warm, DerivedMetrics(coolant_asymptote_c=97.0), RiskAccumulators(), [],
                                    "NORMAL", 3, S(L0, 600), S(L0, 1200), None, CFG3, live_spans=r)
       if d.scenario == "COOLING_FAILURE"]
check(len(d_w) == 1 and d_w[0].description_key.endswith("warmup_asymptote_above_norm")
      and "live" not in d_w[0].values,
      f"4ж: участка нет — максимум 89 не близость, прогноз прогрева как раньше: {[d.description_key for d in d_w]}")

# ── 4е. Сбой расчёта участков — прежнее поведение и запись в лог ──
_sg.segment_spans = _boom
try:
    s_fail = seg3(work_rows(extra=[h(OILT, S(L0, 100), 99.5), h(OILT, S(L0, 400), 97.0)]), RS_REP)
    d_fail = dets(s_fail, "LIMIT_PROXIMITY")
    check(d_fail and all(d.t_detected == sub.t_start and "live" not in d.values for sub, d in d_fail),
          "4е: сбой участков — детекторы по подсегменту (live_spans=None)")
    check(logged("Участки нарушений сегмента"), "4е: сбой участков — запись в лог")
except Exception as e:  # noqa: BLE001
    check(False, f"4е: сбой участков уронил segment(): {e!r}")
finally:
    _sg.segment_spans = _orig_spans
# Сбой в ветке детектора по участкам — подсегмент по-старому, без падения
import analytics.detectors as _dt  # noqa: E402
_orig_hits = _dt.sub_hits
_dt.sub_hits = _boom
try:
    s_f2 = seg3(work_rows(extra=[h(OILT, S(L0, 100), 99.5), h(OILT, S(L0, 400), 97.0)]), RS_REP)
    d_f2 = dets(s_f2, "LIMIT_PROXIMITY")
    check(d_f2 and all("live" not in d.values for _, d in d_f2)
          and logged("Детекторы по участкам подсегмента"), "4е: сбой ветки детектора — по подсегменту, в лог")
except Exception as e:  # noqa: BLE001
    check(False, f"4е: сбой ветки детектора уронил segment(): {e!r}")
finally:
    _dt.sub_hits = _orig_hits

# ══ 5. Свежесть и живой состав (П4, этап 3) ═════════════════════════════════
from analytics.contract import Detection  # noqa: E402
from analytics.live import alive_detections, detection_live  # noqa: E402

F0 = T(6, 0, d=16)      # 16.10 09:00 МСК


def lv_block(start, end, ongoing=False, more=()):
    cls = [{"start": a.isoformat(), "end": b.isoformat(), "ongoing": og} for a, b, og in more]
    cls.append({"start": start.isoformat(), "end": end.isoformat(), "ongoing": ongoing})
    return {"clusters": cls, "onset": start.isoformat(), "first": start.isoformat(),
            "until": end.isoformat(), "ongoing": ongoing}


def ldet(sc, start, end, ongoing=False, extra=None, more=()):
    v = {"live": lv_block(start, end, ongoing, more)}
    v.update(extra or {})
    return {"scenario": sc, "severity": "CAUTION", "t_detected": start.isoformat(), "trigger": "t", "values": v}


def DET(sc, start=None, end=None, ongoing=False, values=None):
    v = dict(values or {})
    if start is not None:
        v["live"] = lv_block(start, end, ongoing)
    return Detection(scenario=sc, severity="CAUTION", t_detected=(start or F0).isoformat(), source="METRIC_RULE",
                     trigger="t", related_roles=[], fault_codes=[], description_key="k", values=v)


def SUB(a, b, dets, gaps=()):
    return Subsegment(id=f"s.{a}", parent_segment_id="s", t_start=S(F0, a).isoformat(),
                      t_end=S(F0, b).isoformat(), duration_sec=b - a, load_zone="NORMAL",
                      cause_open="ZONE_CHANGE", cause_close=None, characteristics={},
                      derived_metrics=DerivedMetrics(), risk_accumulators=RiskAccumulators(),
                      detections=dets, data_quality=1.0,
                      data_gaps=[{"start": S(F0, x).isoformat(), "end": S(F0, y).isoformat(),
                                  "duration_sec": y - x} for x, y in gaps])


def SEG(subs, rs=3):
    return Segment(id="s", router_sn="R", equip_type="pcc", panel_id=1, engine_sn="", run_state=rs,
                   run_state_label="Работа", t_start=subs[0].t_start, t_end=subs[-1].t_end,
                   duration_sec=0, engine_hours_start=None, cause_open="REPORT_START", cause_close=None,
                   preamble_included=False, data_quality=1.0, subsegments=subs, sequence_checks=[], events=[])


def scen(dets):
    return sorted(d.get("scenario") for d in dets)


# ── 5а. detection_live: идёт / кончился не больше N видимого времени назад ──
H = S(F0, 2000)
check(detection_live({"scenario": "X", "values": {}}, H, []) is None, "5а: без live — None (прежняя логика)")
check(detection_live(ldet("LIMIT_PROXIMITY", F0, S(F0, 100), ongoing=True), H, []) is True, "5а: идёт — живая")
check(detection_live(ldet("LIMIT_PROXIMITY", F0, S(H, -600)), H, []) is True, "5а: кончился 600 с назад — живая")
check(detection_live(ldet("LIMIT_PROXIMITY", F0, S(H, -601)), H, []) is False, "5а: 601 с назад — не живая")
g_dg = [{"gap_start": S(H, -500), "gap_end": S(H, -300)}]
check(detection_live(ldet("LIMIT_PROXIMITY", F0, S(H, -700)), H, g_dg) is True,
      "5а: дыра внутри N не считается (700 − 200 = 500 с видимого)")
check(detection_live(ldet("LIMIT_PROXIMITY", F0, S(H, -700)), H,
                     [{"start": S(H, -500).isoformat(), "end": S(H, -300).isoformat()}]) is True,
      "5а: дыры подсегмента (start/end) — так же")
check(detection_live(ldet("LIMIT_PROXIMITY", F0, S(H, -700)), H,
                     [{"gap_start": S(H, -650), "gap_end": None}]) is True,
      "5а: открытая дыра (синтетический хвост) — до горизонта")
check(detection_live(DET("RPM_UNDERSPEED", F0, S(H, -700)), H, []) is False, "5а: Detection — так же, как dict")
_d_nountil = ldet("LIMIT_PROXIMITY", F0, S(H, -100))
del _d_nountil["values"]["live"]["until"], _d_nountil["values"]["live"]["ongoing"]
check(detection_live(_d_nountil, H, []) is True, "5а: без until/ongoing — по последнему кластеру")

# ── 5б. Взгляд назад через смену зоны; давний кластер выпадает ──
# Кластер близости 100–290 с в первом подсегменте, во втором и третьем детекции нет
_lp = DET("LIMIT_PROXIMITY", S(F0, 100), S(F0, 290), values={"role": "OIL_TEMP"})
seg_lb = SEG([SUB(0, 300, [_lp]), SUB(300, 600, []), SUB(600, 700, [DET("NO_LOAD_TOO_LONG")])])
_, act = eng._extract_open_segment_data(seg_lb)
check(scen(act) == ["LIMIT_PROXIMITY", "NO_LOAD_TOO_LONG"]
      and [d for d in act if d["scenario"] == "LIMIT_PROXIMITY"][0]["t_detected"] == S(F0, 100).isoformat(),
      f"5б: смена зоны не обрывает — ключ взглядом назад, начало кластера: {act}")
seg_old = SEG([SUB(0, 300, [_lp]), SUB(300, 600, []), SUB(600, 1000, [])])
check(scen(eng._extract_open_segment_data(seg_old)[1]) == [],
      "5б: кончился 710 с назад — выпал (прежде по последнему подсегменту — тоже нет)")
seg_gap = SEG([SUB(0, 300, [_lp]), SUB(300, 600, [], gaps=[(400, 600)]), SUB(600, 1000, [], gaps=[(600, 700)])])
check(scen(eng._extract_open_segment_data(seg_gap)[1]) == ["LIMIT_PROXIMITY"],
      "5б: за обрыв связи не устаревает (710 − 300 = 410 с видимого)")
# Детекция последнего подсегмента с давним кластером — не живая (прежде висела бы до конца подсегмента)
seg_last = SEG([SUB(0, 1800, [DET("RPM_UNDERSPEED", S(F0, 100), S(F0, 109))])])
check(scen(eng._extract_open_segment_data(seg_last)[1]) == [],
      "5б: провал 9 с в начале 30-минутного подсегмента — не висит до его конца")
# Детекции без live — как раньше: только последний подсегмент, целиком
seg_plain = SEG([SUB(0, 300, [DET("NO_LOAD_TOO_LONG")]), SUB(300, 1800, [DET("IDLE_TOO_LONG")])])
check(scen(eng._extract_open_segment_data(seg_plain)[1]) == ["IDLE_TOO_LONG"],
      "5б: без live — по последнему подсегменту, как раньше")
# Самый поздний подсегмент со сценарием решает: в нём кластер старый — дальше назад не смотрим
_lp_new = DET("LIMIT_PROXIMITY", S(F0, 1300), S(F0, 1350))
seg_two = SEG([SUB(0, 300, [_lp]), SUB(1200, 1400, [_lp_new]), SUB(1400, 1900, [])])
_a2 = eng._extract_open_segment_data(seg_two)[1]
check(scen(_a2) == ["LIMIT_PROXIMITY"] and _a2[0]["t_detected"] == S(F0, 1300).isoformat(),
      f"5б: из самого позднего подсегмента: {_a2}")

# ── 5в. Двухступенчатая свежесть паспорта; псевдоним _passport_stale ──
def pdet(hard_until_s, end_s=None, ongoing=True):
    lim = {"kind": "passport", "limit": 242, "unit": "кПа", "value": 232, "run_sec": 300, "step": 8,
           "hard": True, "engine": "Cummins QSX15", "until": S(F0, end_s or 2000).isoformat(),
           "ongoing": ongoing, "last_value": 240, "hard_until": S(F0, hard_until_s).isoformat(),
           "hard_ongoing": False}
    return ldet("OIL_DILUTION", S(F0, 0), S(F0, end_s or 2000), ongoing=ongoing,
                extra={"limit": lim, "oil_press_min": 232})


_pd = pdet(1300)
eng._passport_live_hardness(_pd, S(F0, 2000), [])
check(_pd["values"]["limit"]["hard"] is False and _pd["values"]["limit"]["value"] == 240,
      "5в: «нарушение» 700 с назад — живая копия «в пределах шага»")
_pd = pdet(1300)
eng._passport_live_hardness(_pd, S(F0, 2000), [(S(F0, 1400), S(F0, 1600))])
check(_pd["values"]["limit"]["hard"] is True, "5в: 700 с, из них 200 без связи — «нарушение» свежее")
_pd_old = pdet(1300)
del _pd_old["values"]["live"]
eng._passport_live_hardness(_pd_old, S(F0, 2000), [(S(F0, 1400), S(F0, 1600))])
check(_pd_old["values"]["limit"]["hard"] is False, "5в: без live — астрономически, как в v5.1.12")
check(eng._passport_stale(pdet(100, end_s=1300, ongoing=False), S(F0, 2000)) is True
      and eng._passport_stale(pdet(100, end_s=1300, ongoing=False), S(F0, 2000),
                              [(S(F0, 1400), S(F0, 1600))]) is False
      and eng._passport_stale(pdet(100, end_s=2000, ongoing=True), S(F0, 2000)) is False,
      "5в: _passport_stale — обёртка над detection_live (видимое время)")
seg_pp = SEG([SUB(0, 1400, [DET("OIL_DILUTION", S(F0, 0), S(F0, 1300),
                                values=pdet(100, end_s=1300, ongoing=False)["values"])]),
              SUB(1400, 2000, [], gaps=[(1400, 1600)])])
check(scen(eng._extract_open_segment_data(seg_pp)[1]) == ["OIL_DILUTION"],
      "5в: паспорт — взглядом назад, за обрыв не устарел")

# ── 5г. Заглушки подсегментов без data_gaps и t_end ──
_stub = SimpleNamespace(t_start=S(F0, 0).isoformat(), detections=[DET("LIMIT_PROXIMITY", S(F0, 0), S(F0, 50))],
                        characteristics={}, risk_accumulators=RiskAccumulators())
try:
    _cv, _a = eng._extract_open_segment_data(SimpleNamespace(subsegments=[_stub], run_state=3, run_state_label="Р"))
    check(scen(_a) == ["LIMIT_PROXIMITY"], f"5г: заглушка без t_end — горизонт по t_start: {_a}")
    check(scen(alive_detections([SimpleNamespace(detections=[DET("X")])])) == ["X"], "5г: заглушка без полей")
except Exception as e:  # noqa: BLE001
    check(False, f"5г: заглушка уронила живой состав: {e!r}")
# Сбой расчёта свежести — прежний состав (последний подсегмент) и запись в лог
import analytics.live as _live_mod  # noqa: E402
_orig_alive = _live_mod.alive_detections
_live_mod.alive_detections = _boom
try:
    check(scen(eng._extract_open_segment_data(seg_lb)[1]) == ["NO_LOAD_TOO_LONG"]
          and logged("Живой состав по участкам не собран"), "5г: сбой свежести — по последнему подсегменту, в лог")
finally:
    _live_mod.alive_detections = _orig_alive


# ── 5д. «Активен на конец» в сводке — та же функция ──
def at_end(segm, sc="LIMIT_PROXIMITY", t_open=S(F0, 100)):
    e = {"source": "analytics", "scenario": sc, "severity": "CAUTION", "gate_suppressed": False,
         "t_open": t_open, "t_close": None, "active_sec": 600.0, "blind_sec": 0.0}
    p = build_summary_parts([segm], episodes=[e], tz=MSK, t_from=_sg._tz(datetime.fromisoformat(segm.t_start)),
                            t_to=_sg._tz(datetime.fromisoformat(segm.t_end)))
    r = [g for g in p["remarks"] if g.get("scenario") == sc]
    return bool(r and r[0].get("at_end")), p.get("end_level")


check(at_end(seg_lb) == (True, "CAUTION"), f"5д: живое взглядом назад — активен на конец: {at_end(seg_lb)}")
check(at_end(seg_old) == (False, "НОРМА"), f"5д: давний кластер — не активен: {at_end(seg_old)}")
_live_mod.alive_detections = _boom
try:
    check(at_end(seg_lb)[0] is False, "5д: сбой свежести — по последнему подсегменту, как раньше")
finally:
    _live_mod.alive_detections = _orig_alive

# ══ 6. Движок: эпизоды по событию (П5, П8) ══════════════════════════════════
import online.db as _odb  # noqa: E402


class FakeDB:
    """Подмена online.db: запись вызовов, без БД."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.next_id = 500
        self.onset = None
        self.open_rows: list[dict] = []

    async def open_episode(self, *a, **k):
        self.next_id += 1
        self.calls.append(("open", self.next_id, k))
        return self.next_id

    async def find_episode_by_onset(self, *a, **k):
        return self.onset

    async def reopen_episode(self, ep_id, **k):
        self.calls.append(("reopen", ep_id, k))

    async def update_episode(self, ep_id, **k):
        self.calls.append(("update", ep_id, k))

    async def close_episode(self, ep_id, t_close, reason, **k):
        self.calls.append(("close", ep_id, t_close, reason, k))

    async def get_open_segment(self, *a, **k):
        return None

    async def set_episodes_gate_suppressed(self, *a, **k):
        pass

    async def get_open_episodes(self, *a, **k):
        return list(self.open_rows)

    async def get_active_alerts(self, *a, **k):
        return []

    def of(self, kind):
        return [c for c in self.calls if c[0] == kind]


_DB_NAMES = ("open_episode", "find_episode_by_onset", "reopen_episode", "update_episode", "close_episode",
             "get_open_segment", "set_episodes_gate_suppressed", "get_open_episodes", "get_active_alerts")
_orig_db = {n: getattr(_odb, n) for n in _DB_NAMES}
_orig_gd = _src.get_data_gaps


async def _noop(*a, **k):
    return None


def mk_engine(db):
    for n in _DB_NAMES:
        setattr(eng.online_db, n, getattr(db, n))
    e = eng.OnlinePollEngine.__new__(eng.OnlinePollEngine)
    e.cfg = CFG3
    e.router_sn, e.equip_type, e.panel_id = "SN", "pcc", 1
    e._episodes, e._episode_accrual_ts, e._last_open_seg_id = {}, None, 5
    e._supp_recheck, e._prev_seg_hint, e.cursor_ts = None, None, None
    e._attach_trip_context = _noop
    return e


def cyc(e, dets, now, gaps=(), gf=F0, rs=3, ca=None):
    if e._episode_accrual_ts is None:
        e._episode_accrual_ts = S(now, -30)
    asyncio.run(e._process_episodes(list(dets), now, list(gaps), cleared_at=ca, gaps_from=gf, run_state=rs))


LP = "LIMIT_PROXIMITY"


async def _gd_empty(*a, **k):
    return []


class _BadDet:
    def to_dict(self):
        raise RuntimeError("тест")


_src.get_data_gaps = _gd_empty      # разрывы до окна (голова эпизода) — без БД
try:
    # ── 6а. Открытие от начала кластера S ──
    db = FakeDB()
    e = mk_engine(db)
    cyc(e, [ldet(LP, S(F0, 100), S(F0, 600), ongoing=True)], S(F0, 600))
    op = db.of("open")
    ep = e._episodes.get(LP) or {}
    check(len(op) == 1 and op[0][2]["t_open"] == S(F0, 100) and op[0][2]["active_sec"] == 500.0
          and op[0][2]["open_values"]["live"]["until"] == S(F0, 600).isoformat(),
          f"6а: открыт с начала кластера, until = горизонт в open_values: {op}")
    check(ep.get("exact") is True and ep.get("until") == S(F0, 600) and ep.get("t_open") == S(F0, 100),
          f"6а: память эпизода: {ep}")

    # ── 6б. Продолжение: тот же кластер кончился — until = его конец, тем же UPDATE ──
    cyc(e, [ldet(LP, S(F0, 100), S(F0, 610))], S(F0, 630))
    up = db.of("update")
    check(len(up) == 1 and up[0][2].get("until") == S(F0, 610) and e._episodes[LP]["until"] == S(F0, 610),
          f"6б: until — конец кластера, пишется в update_episode: {up}")
    # Новый кластер в пределах N — продолжение
    cyc(e, [ldet(LP, S(F0, 1100), S(F0, 1200), ongoing=True, more=[(S(F0, 100), S(F0, 610), False)])], S(F0, 1200))
    check(not db.of("close") and len(db.of("open")) == 1 and e._episodes[LP]["until"] == S(F0, 1200),
          f"6б: кластер через 490 с — тот же эпизод: {db.calls[-1]}")

    # ── 6в. Разрез: новый кластер дальше until + N ──
    e._episodes[LP]["until"] = S(F0, 1210)
    cyc(e, [ldet(LP, S(F0, 1900), S(F0, 1950), ongoing=True)], S(F0, 1950),
        gaps=[{"gap_start": S(F0, 150), "gap_end": S(F0, 250)}])
    cl_ = db.of("close")
    op = db.of("open")
    check(len(cl_) == 1 and cl_[0][2] == S(F0, 1210) and cl_[0][4] == {"exact": True, "blind_sec": 100.0},
          f"6в: прежний закрыт точно по until, слепая доля — дыры внутри: {cl_}")
    check(len(op) == 2 and op[1][2]["t_open"] == S(F0, 1900) and e._episodes[LP]["t_open"] == S(F0, 1900),
          f"6в: новый — с начала своего кластера: {op[-1:]}")
    # Те же 690 с, из них 200 без связи — продолжение
    db2 = FakeDB()
    e2 = mk_engine(db2)
    e2._episodes = {LP: {"id": 7, "severity": "CAUTION", "miss": 0, "first_miss_ts": None,
                         "t_open": S(F0, 100), "until": S(F0, 1210), "exact": True}}
    cyc(e2, [ldet(LP, S(F0, 1900), S(F0, 1950), ongoing=True)], S(F0, 1950),
        gaps=[{"gap_start": S(F0, 1300), "gap_end": S(F0, 1500)}])
    check(not db2.of("close") and not db2.of("open") and db2.of("update"),
          "6в: N по видимому времени — дыра между кластерами не в счёт")

    # ── 6г. Смешанный ключ: детекция без live у точного эпизода — until = горизонт ──
    db3 = FakeDB()
    e3 = mk_engine(db3)
    e3._episodes = {"OIL_DILUTION": {"id": 8, "severity": "CAUTION", "miss": 0, "first_miss_ts": None,
                                     "t_open": S(F0, 100), "until": S(F0, 300), "exact": True}}
    comb = {"scenario": "OIL_DILUTION", "severity": "CAUTION", "t_detected": S(F0, 600).isoformat(), "values": {}}
    cyc(e3, [comb], S(F0, 900))
    check(db3.of("update") and db3.of("update")[0][2].get("until") == S(F0, 900),
          f"6г: комбинированная ветка — until = горизонт: {db3.calls}")
    cyc(e3, [ldet("OIL_DILUTION", S(F0, 100), S(F0, 400)), comb], S(F0, 930))
    check(e3._episodes["OIL_DILUTION"]["until"] == S(F0, 930), "6г: паспорт + комбинированная — горизонт")
    # Близость: детекции по ролям — until по самому позднему кластеру, порядок не важен
    db4 = FakeDB()
    e4 = mk_engine(db4)
    e4._episodes = {LP: {"id": 9, "severity": "CAUTION", "miss": 0, "first_miss_ts": None,
                         "t_open": S(F0, 100), "until": S(F0, 800), "exact": True}}
    cyc(e4, [ldet(LP, S(F0, 700), S(F0, 1000), ongoing=True), ldet(LP, S(F0, 100), S(F0, 300))], S(F0, 1000))
    check(e4._episodes[LP]["until"] == S(F0, 1000) and not db4.of("close"),
          f"6г: роль со старым кластером последней — until не уезжает назад: {e4._episodes[LP]}")
    # Тот же кластер больше не идёт, его конец раньше прежнего горизонта: until был горизонтом
    # идущего кластера — теперь его фактический конец, и вниз (ревью v5.1.21, engine-1: иначе
    # горизонт синтетического хвоста паузы пакетов без data_gaps оставался в until — пауза
    # шла в воздействие, кластер после неё склеивался с прежним). Тем же UPDATE
    cyc(e4, [ldet(LP, S(F0, 700), S(F0, 980))], S(F0, 1030))
    check(e4._episodes[LP]["until"] == S(F0, 980) and e4._episodes[LP]["until_kind"] == "end"
          and db4.of("update")[-1][2]["until"] == S(F0, 980),
          f"6г: тот же кластер кончился — until = его конец, и вниз: {e4._episodes[LP]}")
    # Фактический конец уже известен — поздние строки того же кластера until назад не двигают
    cyc(e4, [ldet(LP, S(F0, 700), S(F0, 970))], S(F0, 1060))
    check(e4._episodes[LP]["until"] == S(F0, 980) and db4.of("update")[-1][2]["until"] == S(F0, 980),
          f"6г: конец известен — поздние строки until не уводят назад: {e4._episodes[LP]}")
    # Горизонт детекции без live (смешанный ключ) — не горизонт кластера: назад не уезжает
    cyc(e3, [ldet("OIL_DILUTION", S(F0, 100), S(F0, 400))], S(F0, 960))
    check(e3._episodes["OIL_DILUTION"]["until"] == S(F0, 930)
          and e3._episodes["OIL_DILUTION"]["until_kind"] == "plain",
          f"6г: горизонт комбинированной ветки — until не назад: {e3._episodes['OIL_DILUTION']}")
    # Старая строка (без exact) — до горизонта по-старому, без until
    db5 = FakeDB()
    e5 = mk_engine(db5)
    e5._episodes = {LP: {"id": 10, "severity": "CAUTION", "miss": 0, "first_miss_ts": None, "t_open": S(F0, 0)}}
    cyc(e5, [ldet(LP, S(F0, 1900), S(F0, 1950))], S(F0, 1950))
    check(db5.of("update") and "until" not in db5.of("update")[0][2] and not db5.of("close"),
          f"6г: старая строка без live — без until и разреза: {db5.calls}")

    # ── 6д. Точное закрытие после дебаунса; first_miss_ts = max(until, карта) ──
    db6 = FakeDB()
    e6 = mk_engine(db6)
    e6._episodes = {LP: {"id": 11, "severity": "CAUTION", "miss": 0, "first_miss_ts": None,
                         "t_open": S(F0, 100), "until": S(F0, 400), "exact": True}}
    for k in range(3):
        cyc(e6, [], S(F0, 1100 + 30 * k), gaps=[{"gap_start": S(F0, 200), "gap_end": S(F0, 250)}])
    cl_ = db6.of("close")
    check(len(cl_) == 1 and cl_[0][1] == 11 and cl_[0][2] == S(F0, 400) and cl_[0][4]["exact"] is True
          and cl_[0][4]["blind_sec"] == 50.0 and LP not in e6._episodes,
          f"6д: 3 промаха — закрыт по until, а не горизонтом: {cl_}")
    db7 = FakeDB()
    e7 = mk_engine(db7)
    e7._episodes = {LP: {"id": 12, "severity": "CAUTION", "miss": 0, "first_miss_ts": None,
                         "t_open": S(F0, 100), "until": S(F0, 400), "exact": True}}
    for k in range(3):
        cyc(e7, [], S(F0, 1100 + 30 * k), ca={LP: S(F0, 450)} if k == 0 else {})
    check(db7.of("close") and db7.of("close")[0][2] == S(F0, 450), f"6д: карта позже until — по карте: {db7.calls}")
    db8 = FakeDB()
    e8 = mk_engine(db8)
    e8._episodes = {LP: {"id": 13, "severity": "CAUTION", "miss": 0, "first_miss_ts": None,
                         "t_open": S(F0, 100), "until": None, "exact": True}}
    for k in range(3):
        cyc(e8, [], S(F0, 1100 + 30 * k), ca={LP: S(F0, 50)} if k == 0 else {})
    check(db8.of("close") and db8.of("close")[0][2] == S(F0, 1100),
          f"6д: ни until, ни карты (карта раньше t_open) — горизонт первого промаха: {db8.calls}")

    # ── 6е. Мост на резе / рестарте ──
    def bridge_run(until_s, rs=3, gf=F0, prev_rs=3, cycles=((60, None),), gaps=()):
        db_ = FakeDB()
        e_ = mk_engine(db_)
        e_._prev_seg_hint = SimpleNamespace(run_state=prev_rs) if prev_rs is not None else None
        e_._episodes = {LP: {"id": 20, "severity": "CAUTION", "miss": 0, "first_miss_ts": None,
                             "t_open": S(F0, -3600), "until": S(F0, until_s), "exact": True}}
        for now_s, dets_ in cycles:
            cyc(e_, dets_ or [], S(F0, now_s), gaps=gaps, gf=gf, rs=rs)
        return db_, e_

    db_, e_ = bridge_run(-60, cycles=[(30, None), (60, None), (90, None), (120, None)])
    check(not db_.of("close") and e_._episodes[LP]["miss"] == 0,
          f"6е: рез — новое окно кластер не видит, с until < N: промаха нет: {e_._episodes[LP]}")
    db_, e_ = bridge_run(-60, cycles=[(30, None), (500, None), (530, None), (560, None), (590, None),
                                      (620, None)])
    cl_ = db_.of("close")
    check(len(cl_) == 1 and cl_[0][2] == S(F0, -60) and cl_[0][4]["exact"],
          f"6е: мост кончился через N — закрыт по until, без времени моста: {cl_}")
    db_, e_ = bridge_run(-60, cycles=[(30, None), (60, [ldet(LP, F0, S(F0, 60), ongoing=True)])])
    check(not db_.of("close") and not db_.of("open") and e_._episodes[LP]["until"] == S(F0, 60),
          "6е: повтор в пределах моста — продолжение того же эпизода")
    db_, e_ = bridge_run(-60, cycles=[(500, None), (530, None), (560, None)],
                         gaps=[{"gap_start": S(F0, 0), "gap_end": S(F0, 400)}])
    check(not db_.of("close") and e_._episodes[LP]["miss"] == 0, "6е: мост по видимому времени (обрыв не в счёт)")
    db_, e_ = bridge_run(0, cycles=[(30, None), (60, None), (90, None), (120, None)])
    check(not db_.of("close"), "6е: кластер шёл на самом резе (until = начало окна) — мост тоже")
    for label, kw in (("отзыв внутри окна", {"until_s": 10}), ("RUN_STATE ≠ 3", {"until_s": -60, "rs": 0}),
                      ("после смены режима", {"until_s": -60, "prev_rs": 0})):
        db_, e_ = bridge_run(cycles=[(30, None), (60, None), (90, None)], **kw)
        check(len(db_.of("close")) == 1, f"6е: без моста — {label}: {db_.calls}")

    # ── 6ж. Слепая доля головы до окна — догрузкой разрывов; сбой — накопленная ──
    async def _gd_head(sn, et, pid, a, b):
        return [{"gap_start": S(F0, -1000), "gap_end": S(F0, -700)}]

    _src.get_data_gaps = _gd_head
    db_, e_ = bridge_run(-60, cycles=[(560, None), (590, None), (620, None)])
    check(db_.of("close") and db_.of("close")[0][4]["blind_sec"] == 300.0,
          f"6ж: голова эпизода до окна — разрывы догружены: {db_.of('close')}")
    _src.get_data_gaps = _raise
    db_, e_ = bridge_run(-60, cycles=[(560, None), (590, None), (620, None)])
    check(db_.of("close") and db_.of("close")[0][4]["blind_sec"] is None
          and logged("разрывы до окна не догружены"),
          f"6ж: разрывы не догружены — накопленная доля (None): {db_.of('close')}")
    _src.get_data_gaps = _gd_empty

    # ── 6з. Карта концов аналитики ──
    seg_closed = SEG([SUB(0, 600, [DET(LP, S(F0, 100), S(F0, 600), ongoing=True)])])
    seg_closed.cause_close = "RUN_STATE_CHANGE"
    check(eng._analytics_cleared_at([seg_closed]) == {LP: S(F0, 600)},
          "6з: идущий кластер закрытого сегмента кончается краем его окна")
    seg_m = SEG([SUB(0, 600, [DET(LP, S(F0, 100), S(F0, 200))]), SUB(600, 1200, [DET(LP, S(F0, 700), S(F0, 750))]),
                 SUB(1200, 6000, [DET(LP, S(F0, 5000), S(F0, 5100))])])
    mem = {LP: {"t_open": S(F0, 700), "until": S(F0, 720), "exact": True}}
    check(eng._analytics_cleared_at([seg_m], mem) == {LP: S(F0, 750)},
          f"6з: кластер с началом не раньше t_open, цепочкой — кластер после простоя не берётся: "
          f"{eng._analytics_cleared_at([seg_m], mem)}")
    check(eng._analytics_cleared_at([seg_m]) == {LP: S(F0, 5100)}, "6з: без памяти — самый поздний конец")
    check(eng._analytics_cleared_at([seg_m], {LP: {"t_open": S(F0, 700), "until": S(F0, 720)}}) == {},
          "6з: не точный эпизод — карты нет")
    check(eng._analytics_cleared_at([SimpleNamespace(subsegments=[SimpleNamespace(detections=[_BadDet()])])],
                                    mem) == {} and logged("Карта концов нарушений аналитики не собрана"),
          "6з: сбой — пустая карта и запись в лог")
    _uw = inspect.getsource(eng.OnlinePollEngine._update_open_window)
    check("_analytics_cleared_at(segments, self._episodes)" in _uw and "run_state=open_seg.run_state" in _uw
          and "**_panel_cleared_at(segments)" in _uw, "6з: карта сливается со снятием панели, режим — для моста")
    check(inspect.getsource(eng._panel_cleared_at).count("CONTROLLER_FAULT") >= 1,
          "6з: _panel_cleared_at не тронута (только панель)")

    # ── 6и. Восстановление после рестарта ──
    import json as _json  # noqa: E402
    dbr = FakeDB()
    er = mk_engine(dbr)
    dbr.open_rows = [
        {"id": 31, "scenario": LP, "addr": None, "bit": None, "severity": "CAUTION", "t_open": S(F0, -7200),
         "open_values_json": _json.dumps({"live": lv_block(S(F0, -7200), S(F0, -5000))})},
        {"id": 32, "scenario": "RPM_UNDERSPEED", "addr": None, "bit": None, "severity": "CAUTION",
         "t_open": S(F0, -100), "open_values_json": _json.dumps({"live": {"onset": S(F0, -100).isoformat()}})},
        {"id": 33, "scenario": "CONTROLLER_FAULT", "addr": 40407, "bit": 5, "severity": "SHUTDOWN",
         "t_open": S(F0, -100), "open_values_json": _json.dumps({"fault_name": "x"})},
        {"id": 34, "scenario": "OIL_DILUTION", "addr": None, "bit": None, "severity": "CAUTION",
         "t_open": S(F0, -100), "open_values_json": {"limit": {"kind": "passport"}}},
    ]
    asyncio.run(er._restore_live_state())
    m = er._episodes
    check(m[LP]["exact"] and m[LP]["until"] == S(F0, -5000)
          and m["RPM_UNDERSPEED"]["exact"] and m["RPM_UNDERSPEED"]["until"] is None
          and not m["CONTROLLER_FAULT|40407|5"]["exact"] and not m["OIL_DILUTION"]["exact"],
          f"6и: exact и until — из open_values_json: {m}")
    # until не записан — первая же детекция ключа считается продолжением
    cyc(er, [ldet("RPM_UNDERSPEED", S(F0, 900), S(F0, 909))], S(F0, 930))
    check(not dbr.of("close") and not dbr.of("open") and er._episodes["RPM_UNDERSPEED"]["until"] == S(F0, 909),
          f"6и: until неизвестен — продолжение: {dbr.calls}")
    # Долгий простой: until давний, моста нет — закрытие точно по until, без времени простоя
    er._prev_seg_hint = SimpleNamespace(run_state=3)
    for k in range(3):
        cyc(er, [ldet("RPM_UNDERSPEED", S(F0, 900), S(F0, 909))], S(F0, 960 + 30 * k))
    cl_ = [c for c in dbr.of("close") if c[1] == 31]
    check(len(cl_) == 1 and cl_[0][2] == S(F0, -5000) and cl_[0][4]["exact"],
          f"6и: после простоя — закрыт по until (−5000 с), а не по горизонту: {cl_}")

    # ── 6к. Старые строки: закрытие по-старому, позиционно (совместимость подмен) ──
    async def _close3(ep_id, t_close, reason):
        _c3.append((ep_id, t_close))

    _c3: list = []
    db9 = FakeDB()
    e9 = mk_engine(db9)
    eng.online_db.close_episode = _close3
    e9._episodes = {LP: {"id": 40, "severity": "CAUTION", "miss": 0, "first_miss_ts": None, "t_open": S(F0, 0)}}
    for k in range(3):
        cyc(e9, [], S(F0, 600 + 30 * k), ca={LP: S(F0, 300)} if k == 0 else {})
    check(_c3 == [(40, S(F0, 300))], f"6к: старая строка — close_episode(id, t, reason), как раньше: {_c3}")

    # ── 6м. Сбой разбора кластеров — эпизоды по-старому, цикл не падает ──
    _orig_lbk = eng._live_by_key
    eng._live_by_key = _boom
    try:
        db10 = FakeDB()
        e10 = mk_engine(db10)
        cyc(e10, [ldet(LP, S(F0, 100), S(F0, 600), ongoing=True)], S(F0, 600))
        check(db10.of("open") and db10.of("open")[0][2]["t_open"] == S(F0, 100)
              and e10._episodes[LP]["exact"] is False and logged("кластеры детекций не разобраны"),
              f"6м: сбой — открытие по t_detected, не точный, запись в лог: {db10.calls}")
    finally:
        eng._live_by_key = _orig_lbk
finally:
    for n, f in _orig_db.items():
        setattr(eng.online_db, n, f)
    _src.get_data_gaps = _orig_gd


# ── 6л. SQL: until тем же UPDATE, только открытой строки; точное закрытие ──
class _FakeConn:
    def __init__(self):
        self.q: list[tuple] = []

    async def execute(self, sql, *args):
        self.q.append((sql, args))
        return "UPDATE 1"

    async def close(self):
        pass


_fc = _FakeConn()
_orig_conn = _odb._connect


async def _fake_connect():
    return _fc


_odb._connect = _fake_connect
try:
    asyncio.run(_odb.update_episode(5, active_sec_add=30.0, until=S(F0, 610)))
    asyncio.run(_odb.update_episode(6, active_sec_add=30.0))
    (sql1, a1), (_, a2) = _fc.q
    check(len(_fc.q) == 2 and "t_close IS NULL" in sql1 and "jsonb_set(open_values_json, '{live,until}'" in sql1
          and a1[4] == S(F0, 610).isoformat() and a2[4] is None,
          f"6л: until — jsonb_set в том же UPDATE, по закрытой строке ничего: {a1}, {a2}")
    _fc.q.clear()
    asyncio.run(_odb.close_episode(5, S(F0, 610), "condition_cleared", exact=True, blind_sec=12.5))
    asyncio.run(_odb.close_episode(6, S(F0, 610), "condition_cleared"))
    (sqle, ae), (sqlo, ao) = _fc.q
    check("GREATEST(active_sec" not in sqle and "LEAST(COALESCE($4::double precision, blind_sec)" in sqle
          and "t_close IS NULL" in sqle and ae == (5, S(F0, 610), "condition_cleared", 12.5),
          f"6л: точное закрытие — active_sec = t_close − t_open, blind_sec не больше: {ae}")
    check("GREATEST(" in sqlo and "active_sec, EXTRACT" in sqlo and "LEAST" not in sqlo,
          "6л: без exact — прежний GREATEST")
    check(ao == (6, S(F0, 610), "condition_cleared"), f"6л: без exact — прежние аргументы: {ao}")
finally:
    _odb._connect = _orig_conn


# ══ 7. Посев при закрытии и суточный рез (П6, П7, этап 4) ═══════════════════
import json as _json  # noqa: E402

from analytics.serializer import _append_detections, _violation_line  # noqa: E402
from corpus.preprocessor import _fmt_detections_hierarchy  # noqa: E402


def LDET(sc, cls, values=None, t_detected=None):
    """Detection с участками: cls — [(начало, конец[, идёт[, чистое[, роли]]])]."""
    v = dict(values or {})
    out = []
    for c in cls:
        d = {"start": c[0].isoformat(), "end": c[1].isoformat(), "ongoing": bool(c[2]) if len(c) > 2 else False,
             "viol_sec": c[3] if len(c) > 3 else (c[1] - c[0]).total_seconds(), "blind_sec": 0.0, "spans": 1}
        if len(c) > 4:
            d["roles"] = c[4]
        out.append(d)
    v["live"] = {"clusters": out, "onset": out[-1]["start"], "first": out[0]["start"],
                 "until": out[-1]["end"], "ongoing": out[-1]["ongoing"]}
    return Detection(scenario=sc, severity="CAUTION", t_detected=(t_detected or cls[0][0]).isoformat(),
                     source="METRIC_RULE", trigger="t", related_roles=[], fault_codes=[], description_key="k",
                     values=v)


class SeedDB:
    """Подмена online.db для посева: запись вставок, без БД."""

    def __init__(self, onsets=(), ov=()):
        self.onsets, self.ov = set(onsets), list(ov)
        self.ins: list[dict] = []
        self.ov_calls: list[tuple] = []
        self.asked: list[list] = []
        self.cnt: list[dict] = []

    async def existing_episode_onsets(self, sn, et, pid, onsets):
        self.asked.append(list(onsets))
        return set(self.onsets)

    async def get_episodes_overlapping(self, sn, et, pid, a, b):
        self.ov_calls.append((a, b))
        return list(self.ov)

    async def insert_closed_episode(self, *a, **k):
        self.ins.append(k)
        return len(self.ins)

    async def count_episodes_batch(self, *a, **k):
        self.cnt.append(k)
        return {}


_SEED_NAMES = ("existing_episode_onsets", "get_episodes_overlapping", "insert_closed_episode",
               "count_episodes_batch")
_orig_seed = {n: getattr(_odb, n) for n in _SEED_NAMES}


def collect(segx, db, open_keys=None, seeded=None, gaps=(), mem=None, carry=False, cfg=CFG3):
    for n in _SEED_NAMES:
        setattr(eng.online_db, n, getattr(db, n))
    try:
        return asyncio.run(eng._collect_and_enrich_detections(
            segx, "SN", "pcc", 1, _sg._tz(datetime.fromisoformat(segx.t_end)), cfg, F0,
            open_keys=set() if open_keys is None else open_keys, seeded_out=seeded, gaps=list(gaps),
            live_episodes=mem or {}, carry_end=carry))
    finally:
        for n, f in _orig_seed.items():
            setattr(eng.online_db, n, f)


def ins_iv(db):
    return [(k["t_open"], k["t_close"], k["active_sec"]) for k in db.ins]


C1, C2, C3 = (S(F0, 100), S(F0, 200)), (S(F0, 1700), S(F0, 1900)), (S(F0, 3000), S(F0, 3300))
ROLE_OT = {"role": "OIL_TEMP"}
seg7 = SEG([SUB(0, 1800, [LDET(LP, [C1, C2], ROLE_OT)]),
            SUB(1800, 3600, [LDET(LP, [(C2[0], S(F0, 1910)), C3], ROLE_OT)])])

# ── 7а. 3 кластера → 3 эпизода; один запрос пересечений на сегмент ──
db7 = SeedDB()
so7: set = set()
collect(seg7, db7, seeded=so7, gaps=[{"gap_start": S(F0, 3100), "gap_end": S(F0, 3150)}])
check(ins_iv(db7) == [(C1[0], C1[1], 100.0), (C2[0], S(F0, 1910), 210.0), (C3[0], C3[1], 300.0)],
      f"7а: по эпизоду на кластер, конец — самый поздний: {ins_iv(db7)}")
check([k["blind_sec"] for k in db7.ins] == [0.0, 0.0, 50.0] and all(k["source"] == "analytics" for k in db7.ins),
      f"7а: слепая доля — дыры внутри кластера: {[k['blind_sec'] for k in db7.ins]}")
check(all(len(k["open_values"]["live"]["clusters"]) == 1
          and k["open_values"]["live"]["onset"] == k["t_open"].isoformat() for k in db7.ins),
      "7а: open_values — live только своего кластера")
check(db7.ov_calls == [(S(F0, -1), S(F0, 3600))], f"7а: один запрос пересечений на окно сегмента: {db7.ov_calls}")
check(so7 == {(LP, C1[0]), (LP, C2[0]), (LP, C3[0])}, f"7а: засеянное — пары (ключ, начало): {so7}")
check(set(db7.asked[0]) >= {C1[0], C2[0], C3[0]}, "7а: онсеты кластеров — в existing_episode_onsets")

# ── 7б. Покрыт онсетом или пересечением — не сеется ──
db7 = SeedDB(onsets={(LP, C1[0])},
             ov=[{"id": 1, "scenario": LP, "t_open": S(F0, 1705), "t_close": S(F0, 1900)},
                 {"id": 2, "scenario": "RPM_UNDERSPEED", "t_open": S(F0, 2900), "t_close": S(F0, 3400)}])
collect(seg7, db7)
check(ins_iv(db7) == [(C3[0], C3[1], 300.0)],
      f"7б: тот же онсет и пересечение (начало сдвинуто поздними строками) — не сеются; чужой сценарий не мешает: "
      f"{ins_iv(db7)}")

# ── 7в. Открытый точный эпизод покрывает только [t_open, until + N] (догонка) ──
open_ex = {"id": 70, "scenario": LP, "t_open": S(F0, 100), "t_close": None,
           "open_values_json": _json.dumps({"live": {"until": S(F0, 200).isoformat()}})}
db7 = SeedDB(ov=[open_ex])
collect(seg7, db7)
check(ins_iv(db7) == [(C2[0], S(F0, 1910), 210.0), (C3[0], C3[1], 300.0)],
      f"7в: открытый эпизод до until + N, остальные кластеры сеются: {ins_iv(db7)}")
db7 = SeedDB(ov=[open_ex])
collect(seg7, db7, mem={LP: {"id": 70, "exact": True, "until": S(F0, 1500), "t_open": S(F0, 100)}})
check(ins_iv(db7) == [(C3[0], C3[1], 300.0)], f"7в: until памяти свежее БД (1700 − 1500 ≤ N): {ins_iv(db7)}")
db7 = SeedDB(ov=[open_ex])
collect(seg7, db7, mem={LP: {"id": 70, "exact": True, "until": S(F0, 1500)}},
        gaps=[{"gap_start": S(F0, 1950), "gap_end": S(F0, 2950)}])
check(not db7.ins, f"7в: N — по видимому времени (дыра 1000 с не в счёт): {ins_iv(db7)}")
db7 = SeedDB(ov=[{"id": 71, "scenario": LP, "t_open": S(F0, 0), "t_close": None, "open_values_json": {}}])
collect(seg7, db7)
check(not db7.ins, "7в: открытая старая строка без live — до бесконечности, как раньше")

# ── 7г. Два закрываемых сегмента одного окна — второй получает свои кластеры ──
C4 = (S(F0, 4000), S(F0, 4100))
segA = SEG([SUB(0, 1800, [LDET(LP, [C1]), DET("NO_LOAD_TOO_LONG", values={})])])
segB = SEG([SUB(3900, 5000, [LDET(LP, [C4]), DET("NO_LOAD_TOO_LONG", values={})])])
acc: set = set()
dbA, dbB = SeedDB(), SeedDB()
collect(segA, dbA, open_keys=set() | acc, seeded=acc)
collect(segB, dbB, open_keys=set() | acc, seeded=acc)
check([k["scenario"] for k in dbA.ins] == [LP, "NO_LOAD_TOO_LONG"] and ins_iv(dbB)[:1] == [(C4[0], C4[1], 100.0)]
      and [k["scenario"] for k in dbB.ins] == [LP],
      f"7г: второй сегмент — свой кластер; детекция без live — прежнее правило ключа: "
      f"{[k['scenario'] for k in dbA.ins]}, {[k['scenario'] for k in dbB.ins]}")
check(acc == {(LP, C1[0]), "NO_LOAD_TOO_LONG", (LP, C4[0])}, f"7г: накопитель окна: {acc}")

# ── 7д. Живой на конец: идёт и продолжится переносом — не сеется; без переноса — сеется ──
C_on = (S(F0, 3300), S(F0, 3600), True)
seg_on = SEG([SUB(0, 3600, [LDET(LP, [C1, C_on])])])
db7 = SeedDB()
collect(seg_on, db7, carry=True)
check(ins_iv(db7) == [(C1[0], C1[1], 100.0)],
      f"7д: без эпизода и идёт на резе «Работы» — продолжит перенос: {ins_iv(db7)}")
db7 = SeedDB()
collect(seg_on, db7, carry=False)
check(ins_iv(db7) == [(C1[0], C1[1], 100.0), (C_on[0], C_on[1], 300.0)],
      f"7д: смена режима или догонка — идущий кластер сеется до края окна: {ins_iv(db7)}")
seg_al = SEG([SUB(0, 3600, [LDET(LP, [(S(F0, 3300), S(F0, 3500))])])])
db7 = SeedDB()
collect(seg_al, db7, carry=True)
check(ins_iv(db7) == [(S(F0, 3300), S(F0, 3500), 200.0)],
      f"7д: живой на конец, но кончился — без эпизода в памяти сеется: {ins_iv(db7)}")
# Перенос — только при живом цикле до самого реза; догонка (отстал больше трёх циклов) — сеять
check(eng._seed_carry(True, F0, S(F0, -30), 30) is True
      and eng._seed_carry(True, F0, S(F0, -91), 30) is False
      and eng._seed_carry(False, F0, S(F0, -30), 30) is False
      and eng._seed_carry(True, F0, None, 30) is False,
      "7д: перенос — только суточный рез «Работы» при живом цикле до самого реза")
check("live_to=_live_to" in inspect.getsource(eng.OnlinePollEngine._poll_cycle)
      and "carry_end=_carry_end" in inspect.getsource(eng.OnlinePollEngine._close_window),
      "7д: _poll_cycle → _close_window(live_to) → посев")

# ── 7е. Смешанный ключ: кластер — по кластеру, детекция без live — по-старому ──
seg_mix = SEG([SUB(0, 1800, [LDET("OIL_DILUTION", [(S(F0, 100), S(F0, 190))])]),
               SUB(1800, 3600, [DET("OIL_DILUTION", values={})])])
db7 = SeedDB()
collect(seg_mix, db7)
check(ins_iv(db7) == [(S(F0, 100), S(F0, 190), 90.0), (F0, S(F0, 3600), 3600.0)] and len(db7.ov_calls) == 2,
      f"7е: кластер паспорта + комбинированная ветка прежним путём: {ins_iv(db7)}, запросов {len(db7.ov_calls)}")
_bad_seg = SEG([SUB(0, 1800, [LDET(LP, [C1])])])
_orig_sc = eng._segment_clusters
eng._segment_clusters = _boom
try:
    db7 = SeedDB()
    collect(_bad_seg, db7)
    check(ins_iv(db7) == [(C1[0], S(F0, 1800), 1700.0)] and logged("Кластеры сегмента для посева не собраны"),
          f"7е: сбой кластеров — посев по-старому (до конца подсегмента) и запись в лог: {ins_iv(db7)}")
finally:
    eng._segment_clusters = _orig_sc

# ── 7ж. Счётчики — с analytics_counts_since (В5) ──
CFG3_SINCE = copy.copy(CFG3)
CFG3_SINCE.detectors = dict(CFG3.detectors, ALARM_EPISODES={"analytics_counts_since": "2026-10-16"})
# Пустой ключ — отдельной копией: в KB с выкладки стоит дата (В5)
CFG3_EMPTY = copy.copy(CFG3)
CFG3_EMPTY.detectors = dict(CFG3.detectors, ALARM_EPISODES={"analytics_counts_since": None})
db7 = SeedDB()
collect(seg7, db7, cfg=CFG3_EMPTY)
check(db7.cnt and db7.cnt[-1].get("analytics_since") is None, "7ж: ключ пуст — без фильтра")
db7 = SeedDB()
collect(seg7, db7, cfg=CFG3_SINCE)
check(db7.cnt and db7.cnt[-1].get("analytics_since") == datetime(2026, 10, 15, 21, 0, tzinfo=U),
      f"7ж: дата выкладки — полночь МСК: {db7.cnt}")
for n in _SEED_NAMES:
    setattr(eng.online_db, n, getattr(db7, n))
try:
    asyncio.run(eng._enrich_open_seg_detections(seg7, "SN", "pcc", 1, CFG3_SINCE, F0))
    check(db7.cnt[-1].get("analytics_since") == datetime(2026, 10, 15, 21, 0, tzinfo=U),
          "7ж: открытый сегмент — тот же фильтр")
finally:
    for n, f in _orig_seed.items():
        setattr(eng.online_db, n, f)

# ── 7з. Синтетика сводки: начало — начало живого кластера (взгляд назад) ──
seg_syn = SEG([SUB(0, 1800, [LDET(LP, [C1])]), SUB(1800, 3000, [LDET(LP, [(S(F0, 2500), S(F0, 2700))])]),
               SUB(3000, 3200, [])])
syn = eng._live_new_episodes([seg_syn], [], {LP}, S(F0, 3200))
check(len(syn) == 1 and syn[0]["t_open"] == S(F0, 2500) and syn[0]["active_sec"] == 700.0,
      f"7з: начало синтетики — начало живого кластера, а не первая детекция: {syn}")
_live_mod.alive_detections = _boom
try:
    syn_b = eng._live_new_episodes([seg_syn], [], {LP}, S(F0, 3200))
    check(syn_b and syn_b[0]["t_open"] == C1[0] and logged("Начало живых кластеров для сводки не найдено"),
          f"7з: сбой — по t_detected, как раньше, и в лог: {syn_b}")
finally:
    _live_mod.alive_detections = _orig_alive

# ── 7и. _drop_same_onset — только панель; у аналитики строки одного онсета склеивает сводка ──
closed_same = {"scenario": LP, "source": "analytics", "severity": "CAUTION", "t_open": S(F0, 2500),
               "t_close": S(F0, 2700), "active_sec": 200.0, "blind_sec": 15.0, "gate_suppressed": False}
kept_ = eng._drop_same_onset([closed_same], syn)
check(kept_ == [closed_same], "7и: аналитическая строка того же онсета не убирается")
p7 = build_summary_parts([seg_syn], kept_ + syn, tz=MSK, t_from=F0, t_to=S(F0, 3200))
g7 = [g for g in p7["remarks"] if g.get("scenario") == LP]
check(g7 and g7[0]["count"] == 1 and g7[0]["at_end"] and g7[0].get("period_sec") == 200.0
      and g7[0]["blind_sec"] == 15.0,
      f"7и: одна строка онсета, живая на конец, воздействие — до конца кластера: {g7}")

# ── 7к. Хвост дебаунса: точный эпизод, не живой на конец, — копией до конца нарушения ──
ep_ex = {"id": 80, "scenario": LP, "source": "analytics", "t_open": S(F0, 100), "t_close": None}
seg_dt = SEG([SUB(0, 3600, [LDET(LP, [(S(F0, 100), S(F0, 1000))])])])
mem_dt = {LP: {"id": 80, "exact": True, "until": S(F0, 900), "t_open": S(F0, 100), "miss": 0}}
out7 = eng._drop_debounce_tails([ep_ex], [seg_dt], mem_dt, set())
check(out7 == [dict(ep_ex, t_close=S(F0, 1000))] and ep_ex["t_close"] is None,
      f"7к: t_close = max(until, конец по карте сегмента), БД и строка не тронуты: {out7}")
check(eng._drop_debounce_tails([ep_ex], [seg_dt], mem_dt, {LP}) == [ep_ex], "7к: живой на конец — остаётся открытым")
out7 = eng._drop_debounce_tails([ep_ex], [SEG([SUB(0, 3600, [])])], mem_dt, set())
check(out7 == [dict(ep_ex, t_close=S(F0, 900))], f"7к: детекции нет — по until памяти (мост): {out7}")
ep_old = {"id": 81, "scenario": LP, "source": "analytics", "t_open": S(F0, -900), "t_close": None}
check(eng._drop_debounce_tails([ep_old], [SEG([SUB(0, 3600, [])])],
                               {LP: {"id": 81, "exact": True, "until": S(F0, -100), "t_open": S(F0, -900)}},
                               set()) == [], "7к: кончился до сегмента — хвост, в сводку не идёт")
check(eng._drop_debounce_tails([ep_ex], [SEG([SUB(0, 3600, [])])], {LP: {"miss": 1}}) == []
      and eng._drop_debounce_tails([ep_ex], [SEG([SUB(0, 3600, [])])], {LP: {"miss": 0}}) == [ep_ex],
      "7к: не точный эпизод — прежнее правило (промах без детекции — хвост)")

# ── 7л. _filter_window_episodes: перенос 60 с после 09:00 остаётся в сводке нового дня ──
ep_carry = {"scenario": LP, "t_open": S(F0, -3600), "t_close": S(F0, 60),
            "open_values_json": _json.dumps({"live": {"until": S(F0, 60).isoformat()}})}
check(eng._filter_window_episodes([ep_carry], F0, S(F0, 3600), 120) == [ep_carry],
      "7л: эпизод с участками — любое пересечение с окном")
check(eng._filter_window_episodes([dict(ep_carry, open_values_json={"live": {}})], F0, S(F0, 3600), 120),
      "7л: open_values словарём — так же")
check(eng._filter_window_episodes([dict(ep_carry, open_values_json={})], F0, S(F0, 3600), 120) == [],
      "7л: старая строка — прежний порог хвоста дебаунса")

# ── 7м. Сводка открытого сегмента: эпизод, держащийся мостом, в окне не показывается ──
ep_br = {"id": 90, "scenario": LP, "source": "analytics", "severity": "CAUTION", "t_open": S(F0, -3600),
         "t_close": None, "active_sec": 3600.0, "blind_sec": 0.0, "gate_suppressed": False,
         "open_values_json": _json.dumps({"live": {"until": S(F0, -60).isoformat()}})}
check(eng._bridge_only(ep_br, {LP: {"id": 90, "exact": True, "until": S(F0, -60)}}, F0)
      and not eng._bridge_only(ep_br, {LP: {"id": 90, "exact": True, "until": S(F0, 30)}}, F0)
      and not eng._bridge_only(ep_br, {LP: {"id": 90, "until": S(F0, -60)}}, F0),
      "7м: мост — точный, until не позже начала окна")


async def _ov_br(*a, **k):
    return [dict(ep_br)]


_orig_ov = _odb.get_episodes_overlapping
eng.online_db.get_episodes_overlapping = _ov_br
try:
    e_s = mk_engine(FakeDB())
    for n, f in _orig_db.items():
        setattr(eng.online_db, n, f)
    eng.online_db.get_episodes_overlapping = _ov_br
    e_s.poll_interval_sec, e_s.tz = 30, MSK
    e_s._episodes = {LP: {"id": 90, "exact": True, "until": S(F0, -60), "t_open": S(F0, -3600)}}
    _, p_br = asyncio.run(e_s._build_summary_md_for([SEG([SUB(0, 600, [])])], F0, S(F0, 600)))
    check(p_br and not [g for g in p_br["remarks"] if g.get("scenario") == LP],
          f"7м: мост — в сводке открытого окна нет: {p_br and p_br['remarks']}")
    e_s._episodes[LP]["until"] = S(F0, 30)
    _, p_br = asyncio.run(e_s._build_summary_md_for([SEG([SUB(0, 600, [LDET(LP, [(F0, S(F0, 30))])])])],
                                                    F0, S(F0, 600)))
    g_br = [g for g in p_br["remarks"] if g.get("scenario") == LP] if p_br else []
    check(g_br and g_br[0]["unconfirmed"] == 0 and g_br[0].get("period_sec") == 30.0 and g_br[0]["before"],
          f"7м: повтор после реза — эпизод в окне, до конца нарушения: {g_br}")
finally:
    eng.online_db.get_episodes_overlapping = _orig_ov

# ── 7н. Переоткрытие по онсету пишет until тем же UPDATE ──
_src.get_data_gaps = _gd_empty
try:
    dbo = FakeDB()
    dbo.onset = {"id": 77, "t_close": S(F0, 50), "severity": "CAUTION"}
    e_o = mk_engine(dbo)
    cyc(e_o, [ldet(LP, S(F0, 0), S(F0, 100))], S(F0, 130))
    rp = dbo.of("reopen")
    check(rp and rp[0][2].get("until") == S(F0, 100) and e_o._episodes[LP]["until"] == S(F0, 100),
          f"7н: reopen_episode получает until кластера: {rp}")
finally:
    for n, f in _orig_db.items():
        setattr(eng.online_db, n, f)
    _src.get_data_gaps = _orig_gd
_fc = _FakeConn()
_odb._connect = _fake_connect
try:
    asyncio.run(_odb.reopen_episode(5, now_ts=S(F0, 130), until=S(F0, 100)))
    asyncio.run(_odb.reopen_episode(6, now_ts=S(F0, 130)))
    (sqlr, ar), (_, ar2) = _fc.q
    check("jsonb_set(open_values_json, '{live,until}'" in sqlr and ar[4] == S(F0, 100).isoformat() and ar2[4] is None,
          f"7н: SQL переоткрытия — until в open_values_json, без него строка как есть: {ar}, {ar2}")
finally:
    _odb._connect = _orig_conn

# ══ 8. Сводка, отчёт, заключение (П9) ═══════════════════════════════════════
RPM_SC = "RPM_UNDERSPEED"


def aep(sc, a, b, opened=False):
    return {"source": "analytics", "scenario": sc, "severity": "CAUTION", "gate_suppressed": False,
            "t_open": S(F0, a), "t_close": None if opened else S(F0, b), "active_sec": float(b - a),
            "blind_sec": 0.0}


def grp(parts, sc):
    r = [g for g in parts["remarks"] if g.get("scenario") == sc]
    return r[0] if r else {}


# ── 8а. Подтверждение — по кластеру своего сценария или подсегменту детекции без live ──
seg8 = SEG([SUB(0, 1800, [LDET(RPM_SC, [(S(F0, 100), S(F0, 109))])]), SUB(1800, 3600, [DET("OIL_DILUTION")])])
p8 = build_summary_parts([seg8], [aep(RPM_SC, 100, 109), aep(RPM_SC, 1000, 1200), aep("OIL_DILUTION", 2000, 2100),
                                  aep("OIL_DILUTION", 500, 600)], tz=MSK, t_from=F0, t_to=S(F0, 3600))
check(grp(p8, RPM_SC).get("count") == 2 and grp(p8, RPM_SC).get("unconfirmed") == 1
      and grp(p8, RPM_SC).get("period_sec") == 9.0,
      f"8а: в подсегменте, но вне кластера — не подтверждён (прежде подсегмент подтверждал): {grp(p8, RPM_SC)}")
check(grp(p8, "OIL_DILUTION").get("unconfirmed") == 1 and grp(p8, "OIL_DILUTION").get("period_sec") == 100.0,
      f"8а: смешанный сценарий — подсегментом детекции без live: {grp(p8, 'OIL_DILUTION')}")
p8o = build_summary_parts([SEG([SUB(0, 1800, [DET(RPM_SC)])])], [aep(RPM_SC, 1000, 1200)], tz=MSK,
                          t_from=F0, t_to=S(F0, 1800))
check(grp(p8o, RPM_SC).get("unconfirmed") == 0, "8а: сегмент без участков — как раньше, подсегментом")

# ── 8б. Открытый точный эпизод — до конца кластера; идущий — до конца окна ──
seg8b = SEG([SUB(0, 3600, [LDET(LP, [(S(F0, 100), S(F0, 400))])])])
p8b = build_summary_parts([seg8b], [aep(LP, 100, 0, opened=True)], tz=MSK, t_from=F0, t_to=S(F0, 3600))
check(grp(p8b, LP).get("period_sec") == 300.0 and grp(p8b, LP).get("open") == 1,
      f"8б: кластер кончился — воздействие до его конца: {grp(p8b, LP)}")
seg8c = SEG([SUB(0, 3600, [LDET(LP, [(S(F0, 100), S(F0, 3600), True)])])])
check(grp(build_summary_parts([seg8c], [aep(LP, 100, 0, opened=True)], tz=MSK, t_from=F0, t_to=S(F0, 3600)),
          LP).get("period_sec") == 3500.0, "8б: идёт — до конца окна")
seg8d = SEG([SUB(0, 1800, [LDET("OIL_DILUTION", [(S(F0, 100), S(F0, 400))])]),
             SUB(1800, 2400, [DET("OIL_DILUTION")]), SUB(2400, 3600, [])])
check(grp(build_summary_parts([seg8d], [aep("OIL_DILUTION", 100, 0, opened=True)], tz=MSK, t_from=F0,
                              t_to=S(F0, 3600)), "OIL_DILUTION").get("period_sec") == 2300.0,
      "8б: смешанный — до конца подсегмента детекции без live")
check(grp(build_summary_parts([SEG([SUB(0, 3600, [DET(LP)])])], [aep(LP, 100, 0, opened=True)], tz=MSK,
                              t_from=F0, t_to=S(F0, 3600)), LP).get("period_sec") == 3500.0,
      "8б: без участков — до конца окна, как раньше")

# ── 8в. Отчёт: строка «Нарушение: …» по уникальным кластерам, чистое время своей роли ──
R1 = {"OIL_TEMP": {"viol_sec": 60.0}, "OIL_PRESS": {"viol_sec": 90.0}}
R2 = {"OIL_TEMP": {"viol_sec": 210.0}}
seg8v = SEG([SUB(0, 1800, [LDET(LP, [(S(F0, 100), S(F0, 200), False, 90.0, R1),
                                     (S(F0, 1700), S(F0, 1900), True, 210.0, R2)], ROLE_OT)]),
             SUB(1800, 3600, [LDET(LP, [(S(F0, 1700), S(F0, 1910), True, 210.0, R2)], ROLE_OT)])])
ln8: list[str] = []
_append_detections(ln8, seg8v, 1, lambda x: str(x), tz=MSK)
viol8 = [x for x in ln8 if "Нарушение" in x]
check(viol8 == ["  - Нарушение (2 эпизода): 16.10 09:01:40–09:03:20, 16.10 09:28:20–09:31:50 (идёт) "
                "(чистое время 4 мин 30 с)"], f"8в: строка нарушения: {viol8}")
ln8o: list[str] = []
_append_detections(ln8o, SEG([SUB(0, 1800, [DET(LP)])]), 1, lambda x: str(x), tz=MSK)
check(not [x for x in ln8o if "Нарушение" in x], "8в: без участков — строки нет")
many = [(S(F0, 1000 * k), S(F0, 1000 * k + 10)) for k in range(8)]
vl = _violation_line([(1, LDET(RPM_SC, many).to_dict())], MSK)
check(vl and "(8 эпизодов)" in vl and "…" in vl and vl.count("–") == 5 and "(чистое время 1 мин 20 с)" in vl,
      f"8в: много кластеров — первые три, «…», последние два: {vl}")

# ── 8г. Блок 1 заключения: число — кластеры, «суммарно» — участки своей роли ──
def chars_of(segx):
    return {"subsegments": [{"duration_sec": sub.duration_sec,
                             "detections": [dict(d.to_dict(), trigger="Температура масла: 99,5 °C")
                                            for d in sub.detections]} for sub in segx.subsegments]}


blk = _fmt_detections_hierarchy(_json.dumps(chars_of(seg8v)))
check("Температура масла у уставки останова панели — 2 срабатывания, суммарно 4 мин" in blk,
      f"8г: 2 кластера, суммарно — участки роли масла (60 + 210 с): {blk}")
blk1 = _fmt_detections_hierarchy({"subsegments": [
    {"duration_sec": 1800, "detections": [LDET(RPM_SC, [(S(F0, 100), S(F0, 109))],
                                               {"rpm_min": 1483, "threshold_rpm": 1485}).to_dict()]},
    {"duration_sec": 1800, "detections": []}]})
check("Просадка оборотов" in blk1 and "1 срабатывание, длительность 9 с" in blk1,
      f"8г: один кластер — его чистое время, а не подсегмент: {blk1}")
blk_old = _fmt_detections_hierarchy({"subsegments": [
    {"duration_sec": 1800, "detections": [DET(RPM_SC, values={"rpm_min": 1450}).to_dict()] * 2},
    {"duration_sec": 1800, "detections": [DET(RPM_SC, values={"rpm_min": 1450}).to_dict()]}]})
check("3 срабатывания, суммарно 1 ч 00 мин" in blk_old, f"8г: старые сегменты — как было: {blk_old}")

# ══ 9. Регресс на данных — синтетические аналоги ═════════════════════════════
# Реальные окна #35876, #35890, #35872, #35891 — 5 МБ строк на сегмент, фикстуры
# не кладём: проверка цифр раздела 1 плана — scripts/check_v5121_data.py (БД
# только чтение). Здесь — те же картины на синтетике через весь путь:
# segment() → посев при закрытии → сводка.

# ── 9а. Аналог #35890 (ДГУ №1): 10 сбоев датчика и 1484 при 49,7 Гц — ни просадки, ни эпизода ──
WA0, WA1 = T(1, 30), T(6, 0)
GL10 = [(T(1, 42, 32), 1479.0), (T(1, 53, 52), 1440.0), (T(1, 55, 12), 1460.0), (T(1, 56, 32), 1417.0),
        (T(1, 57, 3), 1470.0), (T(1, 58, 3), 1475.0), (T(2, 53, 22), 1455.0), (T(5, 34, 13), 1465.0),
        (T(5, 44, 2), 1478.0), (T(5, 49, 22), 1450.0)]
rows9 = pings(WA0, WA1)
_t = WA0
while _t < WA1:
    rows9.append(h(RPM, _t, 1500.0 if (_t.second // 10) % 2 else 1499.0))
    if _t.second == 0 and _t.minute % 5 == 0:
        rows9.append(h(FREQ, _t, 50.0))
    _t += timedelta(seconds=10)
rows9 += [h(RPM, t, v) for t, v in GL10]
rows9 += [h(RPM, T(1, 47, 3), 1484.0), h(FREQ, T(1, 47, 3), 49.7), h(FREQ, T(1, 47, 13), 50.0)]
rows9.sort(key=lambda r: r["ts"])
s9a = segment([rs(3, T(0, 0))], rows9, [], [], CFG, "SN", "pcc", 1, "E", WA0, WA1)
c9a = rg_check(s9a)
check(c9a and c9a[0]["count"] == 10 and c9a[0]["passed"] is False, f"9а: 10 сбоев — проверка не пройдена: {c9a}")
check(not dets(s9a, RPM_SC, work_only=False) and rpm_min(s9a) == 1484.0,
      f"9а: нет RPM_UNDERSPEED, минимум 1484 (при 49,7 Гц): {rpm_min(s9a)}")
db9 = SeedDB()
collect(s9a[0], db9, cfg=CFG)
p9a = build_summary_parts(s9a, [], tz=MSK, t_from=WA0, t_to=WA1)
check(not db9.ins and p9a["verdict"]["level"] == "НОРМА"
      and [c["check"] for c in p9a["failed_checks"]] == ["source_rpm_glitch"],
      f"9а: эпизодов нет, НОРМА, строка качества данных: {p9a['verdict']}, {p9a['failed_checks']}")

# ── 9б. Аналог #35891 (ДЭС №3): провал 1483 при 49,4 Гц — 9 с, а не до конца подсегмента ──
rows9b = [r for r in work_rows(b=S(L0, 1800)) if not (r["addr"] in (RPM, FREQ) and r["ts"] == S(L0, 600))]
rows9b += [h(RPM, S(L0, 600), 1483.0), h(FREQ, S(L0, 600), 49.4), h(RPM, S(L0, 609), 1502.0)]
s9b = seg3(sorted(rows9b, key=lambda r: r["ts"]), RS_REP)
d9b = dets(s9b, RPM_SC)
check(len(d9b) == 1 and d9b[0][1].t_detected == iso(S(L0, 600))
      and "по частоте ≈ 1482" in d9b[0][1].trigger,
      f"9б: провал — по отсчётам, начало 09:10:00: {[(d.t_detected, d.trigger) for _, d in d9b]}")
db9 = SeedDB()
collect(s9b[0], db9)
check(ins_iv(db9) == [(S(L0, 600), S(L0, 609), 9.0)], f"9б: эпизод 9 с: {ins_iv(db9)}")

# ── 9в. Аналог #35872 (ДЭС №3): близость — 4 эпизода (пауза 666 с), паспорт — 2, пол давления ──
L2 = S(L0, 7200)
ot9 = [(0, 97.0), (600, 99.5), (762, 97.0), (1428, 99.5), (1446, 97.0), (2172, 99.5), (2369, 97.0), (6000, 99.5)]
pr9 = [(0, 250.0), (30, 0.0), (40, 250.0), (4000, 236.0), (4090, 250.0), (7110, 236.0)]
rows9c = work_rows(b=L2, extra=[h(OILT, S(L0, a), v) for a, v in ot9] + [h(PRESS3, S(L0, a), v) for a, v in pr9])
s9c = seg3(rows9c, RS_REP, b=L2)
seg9c = s9c[-1]
# «Общая авария» висит весь сегмент (как 1453 у ДЭС №3) — детекция панели в данных
CA = {"addr": 40408, "bit": 11, "fault_name": "CommonAlarm (Общая авария)"}
seg9c.subsegments[-1].detections.append(
    Detection(scenario="CONTROLLER_FAULT", severity="CAUTION", t_detected=S(L0, -3600).isoformat(),
              source="CONTROLLER_FAULT", trigger="CommonAlarm (Общая авария) (addr=40408, bit=11)",
              related_roles=[], fault_codes=[1453], description_key="k", values=dict(CA)))
cl9 = eng._segment_clusters(seg9c)
lp9 = sorted((c["start"], c["end"], c["ongoing"]) for (k, _), c in cl9.items() if k == LP)
check(lp9 == [(S(L0, 600), S(L0, 762), False), (S(L0, 1428), S(L0, 1446), False),
              (S(L0, 2172), S(L0, 2369), False), (S(L0, 6000), L2, True)],
      f"9в: 4 кластера близости — пауза 666 с больше N: {lp9}")
check(all(set((d.values or {}).get("live", {}).get("clusters", [{}])[0].get("roles", {})) == {"OIL_TEMP"}
          for _, d in dets(s9c, LP)) and dets(s9c, LP), "9в: только температура масла — ноль давления отсечён полом")
od9 = sorted((c["start"], c["end"]) for (k, _), c in cl9.items() if k == "OIL_DILUTION")
check(od9 == [(S(L0, 4000), S(L0, 4090)), (S(L0, 7110), L2)]
      and all("live" in (d.values or {}) for _, d in dets(s9c, "OIL_DILUTION")),
      f"9в: паспорт — 2 кластера, комбинированной ветки нет: {od9}")
PANEL9 = "CONTROLLER_FAULT|40408|11"
SEED9 = [(S(L0, 600), S(L0, 762), 162.0), (S(L0, 1428), S(L0, 1446), 18.0),
         (S(L0, 2172), S(L0, 2369), 197.0), (S(L0, 4000), S(L0, 4090), 90.0)]
lk9 = eng._live_alert_keys(seg9c)
check(lk9 >= {LP, "OIL_DILUTION", PANEL9}, f"9в: живые на конец: {lk9}")


def ep_row(k):
    return {"source": "analytics", "scenario": k["scenario"], "severity": k["severity"], "gate_suppressed": False,
            "t_open": k["t_open"], "t_close": k["t_close"], "active_sec": k["active_sec"],
            "blind_sec": k["blind_sec"], "open_values_json": k["open_values"]}


PANEL_EP9 = {"source": "panel", "scenario": "CONTROLLER_FAULT", "addr": 40408, "bit": 11, "severity": "CAUTION",
             "gate_suppressed": False, "t_open": S(L0, -3600), "t_close": None, "active_sec": 3600.0,
             "blind_sec": 0.0, "open_values_json": {"fault_name": "CommonAlarm (Общая авария)"}}


def summary9(eps, mem):
    eps = eng._drop_debounce_tails(eps, [seg9c], mem, lk9)
    syn = eng._live_new_episodes([seg9c], eps, lk9, L2)
    return build_summary_parts([seg9c], eng._drop_same_onset(eps, syn) + syn, tz=MSK, t_from=L0, t_to=L2), syn


def check9(p, tag):
    # В2а (16.10): заголовок — виды замечаний (панель, близость, паспорт), эпизодов 7 (1 + 4 + 2)
    check(p["verdict"]["remarks_n"] == 3 and p["verdict"]["remarks_episodes_n"] == 7
          and p["verdict"]["title"].endswith("ЗАМЕЧАНИЯ К РАБОТЕ — 3"),
          f"9в ({tag}): ЗАМЕЧАНИЯ — 3 вида, 7 эпизодов: {p['verdict']}")
    check(grp(p, LP).get("count") == 4 and grp(p, LP).get("period_sec") == 162 + 18 + 197 + 1200
          and grp(p, LP).get("at_end"), f"9в ({tag}): близость — 4 эп., до конца нарушений: {grp(p, LP)}")
    check(grp(p, "OIL_DILUTION").get("count") == 2 and grp(p, "OIL_DILUTION").get("period_sec") == 180.0,
          f"9в ({tag}): паспорт — 2 эп., 3 мин: {grp(p, 'OIL_DILUTION')}")


# Живой цикл вёл эпизоды по идущим кластерам (как на проде): они в памяти и в БД открыты
OPEN9 = [{"id": 101, "scenario": LP, "source": "analytics", "severity": "CAUTION", "gate_suppressed": False,
          "t_open": S(L0, 6000), "t_close": None, "active_sec": 1170.0, "blind_sec": 0.0,
          "open_values_json": _json.dumps({"live": {"until": S(L2, -30).isoformat()}})},
         {"id": 102, "scenario": "OIL_DILUTION", "source": "analytics", "severity": "CAUTION", "gate_suppressed": False,
          "t_open": S(L0, 7110), "t_close": None, "active_sec": 60.0, "blind_sec": 0.0,
          "open_values_json": _json.dumps({"live": {"until": S(L2, -30).isoformat()}})}]
MEM9 = {LP: {"id": 101, "exact": True, "until": S(L2, -30), "t_open": S(L0, 6000), "miss": 0},
        "OIL_DILUTION": {"id": 102, "exact": True, "until": S(L2, -30), "t_open": S(L0, 7110), "miss": 0}}
db9 = SeedDB(ov=OPEN9)
collect(seg9c, db9, carry=True, mem=MEM9, open_keys={PANEL9, LP, "OIL_DILUTION"})
check(ins_iv(db9) == SEED9, f"9в (память): посев — кончившиеся кластеры, идущие ведёт живой эпизод: {ins_iv(db9)}")
p9c, syn9 = summary9([ep_row(k) for k in db9.ins] + [dict(e) for e in OPEN9] + [PANEL_EP9], MEM9)
check(not syn9, "9в (память): синтетика не нужна — эпизоды открыты")
check9(p9c, "память")
# По идущим кластерам эпизодов нет (живой цикл не успел): перенос продолжит, в сводке — синтетика
db9 = SeedDB()
collect(seg9c, db9, carry=True, open_keys={PANEL9})
check(ins_iv(db9) == SEED9, f"9в (без памяти): посев без идущих на резе: {ins_iv(db9)}")
p9c, syn9 = summary9([ep_row(k) for k in db9.ins] + [PANEL_EP9], {})
check(sorted(s["t_open"] for s in syn9) == [S(L0, 6000), S(L0, 7110)],
      f"9в (без памяти): синтетика с начала своих кластеров: {[s['t_open'] for s in syn9]}")
check9(p9c, "без памяти")

# ══ 10. Счётчики переходного месяца (П11, В5) ═══════════════════════════════
import online.manager as _mgr  # noqa: E402


def cfg_since(v):
    c = copy.copy(CFG3)
    c.detectors = dict(CFG3.detectors, ALARM_EPISODES={"analytics_counts_since": v})
    return c


# ── 10а. Ключ: пусто — без фильтра; дата — полночь МСК; время с поясом; кривое — None и лог ──
from datetime import date as _date  # noqa: E402

check(CFG3_EMPTY.det("ALARM_EPISODES", "analytics_counts_since", default="нет") is None
      and eng.analytics_counts_since(CFG3_EMPTY) is None, "10а: ключ пуст — фильтра нет")
# В KB — дата выкладки (В5, снять после 15.11): одна и та же в стеке и legacy-монолите
_legacy = AnalyticsConfig(KB / "equipment" / "cummins_kta50_pcc3300")
_kb_since = eng.analytics_counts_since(CFG3)
check(_kb_since is not None and eng.analytics_counts_since(_legacy) == _kb_since,
      f"10а: ключ в KB и legacy-монолите совпадает: {_kb_since} / {eng.analytics_counts_since(_legacy)}")
_m16 = datetime(2026, 10, 15, 21, 0, tzinfo=U)
check(eng.analytics_counts_since(cfg_since("2026-10-16")) == _m16
      and eng.analytics_counts_since(cfg_since(_date(2026, 10, 16))) == _m16
      and eng.analytics_counts_since(cfg_since("2026-10-16T14:00:00+03:00")) == datetime(2026, 10, 16, 11, 0, tzinfo=U)
      and eng.analytics_counts_since(cfg_since(datetime(2026, 10, 16, 11, 0, tzinfo=U))) == datetime(2026, 10, 16, 11, 0, tzinfo=U)
      and eng.analytics_counts_since(cfg_since("  ")) is None,
      "10а: дата, дата YAML, ISO с поясом, datetime, пробелы")
check(eng.analytics_counts_since(cfg_since("16.10.2026")) is None
      and logged("не прочитан — счётчики аналитики без фильтра"), "10а: нечитаемо — без фильтра и в лог")
eng.analytics_counts_since(cfg_since("16.10.2026"))
check(sum("не прочитан — счётчики" in r.getMessage() for r in LOG.records) == 1,
      "10а: предупреждение — раз на значение (зовётся каждый цикл)")


# ── 10б. SQL count_episodes_batch: аналитика с даты, панель — без фильтра ──
class _FetchConn(_FakeConn):
    async def fetch(self, sql, *args):
        self.q.append((sql, args))
        return []


_fq = _FetchConn()


async def _fetch_connect():
    return _fq


_odb._connect = _fetch_connect
try:
    asyncio.run(_odb.count_episodes_batch("SN", "pcc", 1, [LP], 30, None, analytics_since=_m16))
    asyncio.run(_odb.count_episodes_batch("SN", "pcc", 1, [LP], 30, None))
    (sqlc, ac), (_, ac2) = _fq.q
    check("$7::timestamptz IS NULL OR source IS DISTINCT FROM 'analytics'" in sqlc and "OR t_open >= $7" in sqlc
          and ac[6] == _m16 and ac2[6] is None, f"10б: фильтр только для source='analytics': {ac}, {ac2}")
finally:
    _odb._connect = _orig_conn

# ── 10в. Гейт: счётчики с той же даты ──
check(_mgr._counts_since_of(SimpleNamespace(cfg=cfg_since("2026-10-16"))) == _m16
      and _mgr._counts_since_of(SimpleNamespace()) is None, "10в: дата машины для гейта; без cfg — None")
check("analytics_since" in inspect.signature(_mgr._analyze_warning_claude).parameters
      and "analytics_since=analytics_since" in inspect.getsource(_mgr._analyze_warning_claude)
      and '"analytics_since": _since' in inspect.getsource(_mgr.OnlineManager._run_gate),
      "10в: _run_gate → _analyze_warning_claude → count_episodes_batch")

# ── 10г. Напоминание снять ключ: старше 35 дней — предупреждение при старте ──
LOG.records.clear()
check(not eng.warn_stale_counts_since(cfg_since("2026-10-16"), now=_m16 + timedelta(days=35))
      and not logged("ключу больше 35 дней"), "10г: 35 дней — без предупреждения")
check(eng.warn_stale_counts_since(cfg_since("2026-10-16"), now=_m16 + timedelta(days=35, seconds=1))
      and logged("ключу больше 35 дней"), "10г: больше 35 дней — предупреждение")
check(not eng.warn_stale_counts_since(CFG3), "10г: ключ пуст — молчит")
check("warn_stale_counts_since" in inspect.getsource(_mgr.OnlineManager.start_all_running),
      "10г: проверка — при старте сервиса")

# ══ 11. Ревью v5.1.21: подтверждённые дефекты (сценарии ревьюеров, без БД) ══════
from analytics.serializer import to_markdown  # noqa: E402

OILT3 = CFG3.role_to_addr("OIL_TEMP")
RS_DAY = [rs(3, T(0, 0))]


def pause_rows(oil, pause=(990, 2010), end=2400, a=L0):
    """Пакеты (пинг, обороты, частота) каждые 10 с от a − 300 до a + end, в паузе пакетов
    нет и строки data_gaps нет; oil — [(с от a, °C)] строки температуры масла."""
    rows_, t_, k_ = [], S(a, -300), 0
    while t_ < S(a, end):
        s_ = (t_ - a).total_seconds()
        if not (pause and pause[0] < s_ < pause[1]):
            rows_ += [h(HB, t_, float(k_)), h(RPM, t_, 1500.0 + k_ % 2), h(FREQ, t_, 50.0)]
        t_ += timedelta(seconds=10)
        k_ += 1
    rows_ += [h(OILT3, S(a, x), v) for x, v in oil]
    return sorted(rows_, key=lambda r: r["ts"])


def live_run(e_, rows_, secs, a=L0, trace=None):
    """Живой цикл движка: segment() открытого окна от a на каждом горизонте (синтетический
    хвост — _synth_connectivity_gap), состав → _process_episodes с картой концов окна."""
    for x in secs:
        hz = S(a, x)
        hist = [r for r in rows_ if r["ts"] <= hz]
        gps = [g for g in [eng._synth_connectivity_gap(hist, a, hz, CFG3)] if g]
        segs_ = segment(RS_DAY, hist, [], list(gps), CFG3, "SN", "pcc", 1, "E", a, hz,
                        load_seed=None, live_edge=True)
        _, dets_ = eng._extract_open_segment_data(segs_[-1])
        if e_._episode_accrual_ts is None:
            e_._episode_accrual_ts = S(hz, -30)
        asyncio.run(e_._process_episodes(dets_, hz, gps,
                                         cleared_at=eng._analytics_cleared_at(segs_, e_._episodes),
                                         gaps_from=a, run_state=segs_[-1].run_state))
        if trace is not None:
            m_ = e_._episodes.get(LP)
            trace[x] = (dict(m_) if m_ else None, bool(gps))


class MemSeedDB:
    """Посев с памятью: вставки складываются, пересечения — тем же предикатом, что db.py."""

    def __init__(self):
        self.rows: list[dict] = []

    async def existing_episode_onsets(self, sn, et, pid, onsets):
        return {(r["scenario"], r["t_open"]) for r in self.rows if r["t_open"] in onsets}

    async def get_episodes_overlapping(self, sn, et, pid, a, b):
        return [dict(r) for r in self.rows if r["t_open"] < b and (r["t_close"] is None or r["t_close"] > a)]

    async def insert_closed_episode(self, *a, **k):
        self.rows.append({"scenario": k["scenario"], "t_open": k["t_open"], "t_close": k["t_close"],
                          "active_sec": k["active_sec"], "open_values_json": k["open_values"]})
        return len(self.rows)

    async def count_episodes_batch(self, *a, **k):
        return {}


_src.get_data_gaps = _gd_empty
try:
    # ── 11а. engine-1 / shadow-1: пауза пакетов без data_gaps внутри нарушения ──
    # (rv/eng/r1_pause_until.py): 99,5 °C с +600 с, пакетов нет +990…+2010 с, после — норма.
    # Итоговый участок кончается на последнем пакете перед паузой (+990); было: закрыт по
    # горизонту синтетического хвоста +2010 — act 1410 с вместо 390, blind 0
    HZ11 = range(630, 2400, 30)
    db_p = FakeDB()
    e_p = mk_engine(db_p)
    tr_p: dict = {}
    live_run(e_p, pause_rows([(60, 97.0), (600, 99.5), (2010, 97.0)]), HZ11, trace=tr_p)
    m_in = tr_p[2010][0] or {}
    check(tr_p[2010][1] and m_in.get("until") == S(L0, 2010) and m_in.get("until_kind") == "horizon",
          f"11а: в паузе — синтетический хвост, until = горизонт идущего кластера: {m_in}")
    m_out = tr_p[2040][0] or {}
    check(m_out.get("until") == S(L0, 990) and m_out.get("until_kind") == "end",
          f"11а: пакеты вернулись без data_gaps — until памяти = конец участка (+990), вниз: {m_out}")
    cl_p, op_p = db_p.of("close"), db_p.of("open")
    check(len(op_p) == 1 and op_p[0][2]["t_open"] == S(L0, 600) and len(cl_p) == 1
          and cl_p[0][2] == S(L0, 990) and cl_p[0][4] == {"exact": True, "blind_sec": 0.0},
          f"11а: закрыт по последнему пакету перед паузой — act 390 с, время паузы не воздействие: "
          f"{op_p}, {cl_p}")
    # (rv/eng/r1b_pause_glue.py): после паузы нарушение продолжается — пауза > N, два кластера
    # по данным; было: один эпизод (onset после паузы ≤ until горизонта — «продолжение»)
    db_g = FakeDB()
    e_g = mk_engine(db_g)
    live_run(e_g, pause_rows([(60, 97.0), (600, 99.5), (2010, 99.5), (2200, 97.0)]), HZ11)
    cl_g, op_g = db_g.of("close"), db_g.of("open")
    check(len(cl_g) == 1 and cl_g[0][2] == S(L0, 990) and cl_g[0][4]["exact"]
          and [o[2]["t_open"] for o in op_g] == [S(L0, 600), S(L0, 2010)],
          f"11а: кластер после паузы > N — разрез: прежний по +990, новый с +2010: {op_g}, {cl_g}")
    # Контроль: та же норма без паузы — закрытие по концу кластера (+2010), как было
    # (свежесть N после конца, затем три промаха)
    db_c = FakeDB()
    e_c = mk_engine(db_c)
    live_run(e_c, pause_rows([(60, 97.0), (600, 99.5), (2010, 97.0)], pause=None, end=2800),
             range(630, 2760, 30))
    check(db_c.of("close") and db_c.of("close")[0][2] == S(L0, 2010),
          f"11а: без паузы — конец кластера +2010: {db_c.of('close')}")
    # _exact_end и _episode_continues — карта окна решает, until памяти — запасной
    _ep_h = {"t_open": S(F0, 100), "until": S(F0, 1000), "until_kind": "horizon"}
    check(eng._exact_end(_ep_h, S(F0, 980)) == S(F0, 980)
          and eng._exact_end(_ep_h, None) == S(F0, 1000)
          and eng._exact_end(_ep_h, S(F0, 50)) == S(F0, 1000)
          and eng._exact_end(dict(_ep_h, until_kind="plain"), S(F0, 980)) == S(F0, 1000)
          and eng._exact_end(dict(_ep_h, until=None), S(F0, 980)) == S(F0, 980),
          "11а: _exact_end — карта, без неё until; горизонт детекции без live — позднейшее")
    check(not eng.OnlinePollEngine._episode_continues(_ep_h, S(F0, 1590), [], 600.0, end=S(F0, 980))
          and eng.OnlinePollEngine._episode_continues(_ep_h, S(F0, 1500), [], 600.0, end=S(F0, 980))
          and eng.OnlinePollEngine._episode_continues(_ep_h, S(F0, 1590), [], 600.0)
          and eng.OnlinePollEngine._episode_continues(dict(_ep_h, until=None), S(F0, 9000), [], 600.0,
                                                      end=S(F0, 980)),
          "11а: продолжение — от конца по карте; until неизвестен — продолжение (П5)")
    # Карта: цепочка от кластера эпизода (начало в t_open), а не от until памяти
    seg_pz = SEG([SUB(0, 3000, [DET(LP, S(F0, 100), S(F0, 400)), DET(LP, S(F0, 1500), S(F0, 1600))])])
    _mem_pz = {LP: {"t_open": S(F0, 100), "until": S(F0, 1450), "exact": True}}
    check(eng._analytics_cleared_at([seg_pz], _mem_pz) == {LP: S(F0, 400)},
          f"11а: карта — от кластера t_open, кластер дальше N не цепляется, даже если начался до until: "
          f"{eng._analytics_cleared_at([seg_pz], _mem_pz)}")

    # ── 11б. shadow-2: рестарт с простоем > N — until из JSON уточняется по кластерам окна ──
    # (rv/skp2_restart.py): C1 +600…+2400, C2 +4200…+6000, сервис лежит +900…+4500. Было: разрез
    # по until JSON (+870) — act 270 с вместо 1800, хвост C1 потерян
    def restart_run(oil, down=(900, 4500), end_s=4620):
        rows_ = pause_rows(oil, pause=None, end=end_s + 60)
        db1 = FakeDB()
        e1 = mk_engine(db1)
        live_run(e1, rows_, range(630, down[0], 30))
        _u_json = db1.of("update")[-1][2]["until"]
        _op = db1.of("open")[0]
        db2 = FakeDB()
        db2.open_rows = [{"id": _op[1], "scenario": LP, "addr": None, "bit": None, "severity": "CAUTION",
                          "t_open": _op[2]["t_open"],
                          "open_values_json": _json.dumps(dict(_op[2]["open_values"], live=dict(
                              _op[2]["open_values"]["live"], until=_u_json.isoformat())), default=str)}]
        e2 = mk_engine(db2)
        asyncio.run(e2._restore_live_state())
        m_r = dict(e2._episodes.get(LP) or {})
        live_run(e2, rows_, range(down[1], end_s, 30))
        return _u_json, m_r, db2

    u_js, m_rs, db_r = restart_run([(60, 97.0), (600, 99.5), (2400, 97.0), (4200, 99.5)])
    check(u_js == S(L0, 870) and m_rs.get("until") == S(L0, 870) and m_rs.get("until_kind") == "horizon",
          f"11б: восстановлен until из JSON (+870, последний цикл до простоя): {u_js}, {m_rs}")
    cl_r, op_r = db_r.of("close"), db_r.of("open")
    check(len(cl_r) == 1 and cl_r[0][1] == 501 and cl_r[0][2] == S(L0, 2400) and cl_r[0][4]["exact"]
          and [o[2]["t_open"] for o in op_r] == [S(L0, 4200)],
          f"11б: первый цикл — конец C1 по карте окна (+2400), разрез, C2 — новый эпизод: {op_r}, {cl_r}")
    # Кластер кончился до простоя — закрытие по нему, без времени простоя (как в плане)
    u_js2, _, db_r2 = restart_run([(60, 97.0), (600, 99.5), (800, 97.0), (4200, 99.5)])
    check(u_js2 == S(L0, 800) and db_r2.of("close") and db_r2.of("close")[0][2] == S(L0, 800),
          f"11б: C1 кончился до простоя — закрыт по +800: {db_r2.of('close')}")
    # Без C2 (ключ не жив) — промах, закрытие по концу C1 (+2400), как было
    _, _, db_r3 = restart_run([(60, 97.0), (600, 99.5), (2400, 97.0)], end_s=4650)
    check(db_r3.of("close") and db_r3.of("close")[0][2] == S(L0, 2400) and not db_r3.of("open"),
          f"11б: ключ не жив — закрыт по концу C1: {db_r3.of('close')}")

    # Простой через 09:00 (rv/shadow_drop.py rsA): восстановленный until 08:44:19, кластер 07:44:32
    # идёт на резе. Закрытие окна уточняет until по своим кластерам (_refine_until) до 09:00 —
    # перенос с 09:00 в новом окне продолжает эпизод. Было: разрез, 07:44:32–08:44:19 и
    # 09:00:00–…, 15 мин 41 с потеряно
    db_x = FakeDB()
    e_x = mk_engine(db_x)
    e_x._episodes = {LP: {"id": 31, "severity": "CAUTION", "miss": 0, "first_miss_ts": None,
                          "t_open": S(F0, -4528), "until": S(F0, -941), "until_kind": "horizon",
                          "exact": True}}
    seg_cut = SEG([SUB(-7200, 0, [LDET(LP, [(S(F0, -4528), F0, True)], ROLE_OT)])])
    e_x._refine_until([seg_cut])
    check(e_x._episodes[LP]["until"] == F0 and e_x._episodes[LP]["until_kind"] == "end",
          f"11б: закрываемое окно — until = край окна (кластер шёл на резе): {e_x._episodes[LP]}")
    db_sd = SeedDB(ov=[{"id": 31, "scenario": LP, "t_open": S(F0, -4528), "t_close": None,
                        "open_values_json": _json.dumps({"live": {"until": S(F0, -941).isoformat()}})}])
    collect(seg_cut, db_sd, mem=e_x._episodes)
    check(not db_sd.ins, f"11б: кластер на резе покрыт открытым эпизодом — не сеется: {ins_iv(db_sd)}")
    for n in _DB_NAMES:
        setattr(eng.online_db, n, getattr(db_x, n))
    cyc(e_x, [ldet(LP, F0, S(F0, 1200), ongoing=True)], S(F0, 1200), gf=F0)
    check(not db_x.of("close") and not db_x.of("open") and e_x._episodes[LP]["until"] == S(F0, 1200),
          f"11б: перенос с 09:00 — продолжение того же эпизода: {db_x.calls}")
    # Горизонт смешанного ключа (детекция без live) по карте не уточняется
    e_x._episodes[LP].update(until=S(F0, 1300), until_kind="plain")
    e_x._refine_until([SEG([SUB(0, 1800, [DET(LP, F0, S(F0, 1200))])])])
    check(e_x._episodes[LP]["until"] == S(F0, 1300), "11б: «plain» — until не уточняется картой")
    _cw, _uw = (inspect.getsource(eng.OnlinePollEngine._close_window),
                inspect.getsource(eng.OnlinePollEngine._update_open_window))
    check(_cw.index("self._refine_until(segments)") < _cw.index("_collect_and_enrich_detections(")
          and _uw.index("self._refine_until(segments)") < _uw.index("_collect_and_enrich_detections("),
          "11б: уточнение — до посева и сводки закрываемых сегментов (рез и смена режима)")
    # Посев: until памяти решает и вниз (в БД мог остаться горизонт паузы)
    db_m = SeedDB(ov=[dict(open_ex, open_values_json=_json.dumps({"live": {"until": S(F0, 1500).isoformat()}}))])
    collect(seg7, db_m, mem={LP: {"id": 70, "exact": True, "until": S(F0, 200), "t_open": S(F0, 100)}})
    check(ins_iv(db_m) == [(C2[0], S(F0, 1910), 210.0), (C3[0], C3[1], 300.0)],
          f"11б: открытый эпизод покрывает [t_open, until памяти + N], а не until БД: {ins_iv(db_m)}")

    # ── 11в. engine-2: догонка через рез — касание 09:00 не покрытие ──
    # (rv/eng/r2_catchup_boundary.py): нарушение 08:00–10:00 МСК, эпизода в памяти нет; окно 1
    # сеет [08:00, 09:00], окно 2 — перенесённый [09:00, 10:00]. Было: засеяно 3600 с из 7200
    db_mem = MemSeedDB()
    day1 = SEG([SUB(-86400, 0, [LDET(LP, [(S(F0, -3600), F0, True)], ROLE_OT)])])
    day2 = SEG([SUB(0, 86400, [LDET(LP, [(F0, S(F0, 3600), False)], ROLE_OT)])])
    for dseg in (day1, day2):
        collect(dseg, db_mem, carry=eng._seed_carry(True, datetime.fromisoformat(dseg.t_end), None, 30))
    check([(r["t_open"], r["t_close"], r["active_sec"]) for r in db_mem.rows]
          == [(S(F0, -3600), F0, 3600.0), (F0, S(F0, 3600), 3600.0)],
          f"11в: продолжение после реза засеяно — 7200 с: {db_mem.rows}")
    # Настоящее пересечение по-прежнему покрывает
    db_mem2 = MemSeedDB()
    db_mem2.rows.append({"scenario": LP, "t_open": S(F0, -3600), "t_close": S(F0, 10), "active_sec": 3610.0,
                         "open_values_json": {}})
    collect(day2, db_mem2)
    check(len(db_mem2.rows) == 1, f"11в: пересечение (09:00 < 09:00:10) — покрыт, не сеется: {db_mem2.rows}")
finally:
    for n, f in _orig_db.items():
        setattr(eng.online_db, n, f)
    _src.get_data_gaps = _orig_gd

# ── 11г. analytics-3: правило B в закрытом окне — последний отсчёт провала не теряется ──
# (rv/x3_ruleB_cut.py): 1470 при 47 Гц в 08:59:40, следующий отсчёт — после 09:00. Было: в
# закрытом окне горизонт = рез, отсчёт «ждал следующего» и выпадал; начало уезжало на 09:00
X0, XC = T(5, 0), T(6, 0)
x_rows = (pings(X0, S(XC, 3600)) + [h(FREQ, X0, 50.0)]
          + [h(RPM, S(X0, 10 * k), 1500.0 + k % 2) for k in range(356)]
          + [h(RPM, S(XC, -20), 1470.0), h(FREQ, S(XC, -19), 47.0)]
          + [h(RPM, S(XC, 5), 1500.0), h(FREQ, S(XC, 6), 50.0)])
x_closed = by_addr([r for r in x_rows if r["ts"] < XC])
c_cl = (segment_spans(x_closed, None, cfgB, X0, XC, [], {}, False, horizon=None).get("RPM_UNDERSPEED") or [])
check(len(c_cl) == 1 and c_cl[0]["start"] == iso(S(XC, -20)) and c_cl[0]["ongoing"],
      f"11г: закрытое окно (горизонта нет) — отсчёт без следующего решается по себе: {c_cl}")
check(not segment_spans(x_closed, None, cfgB, X0, XC, [], {}, False, horizon=XC).get("RPM_UNDERSPEED"),
      "11г: живой край открытого окна — по-прежнему ждёт следующего отсчёта")
x_cl_rows = [r for r in x_rows if r["ts"] < XC]
d_cls = dets(segment(RS_DAY, x_cl_rows, [], [], cfgB, "SN", "pcc", 1, "E", X0, XC), "RPM_UNDERSPEED")
check(d_cls and d_cls[-1][1].values["live"]["clusters"][-1]["start"] == iso(S(XC, -20)),
      f"11г: segment() закрытого окна (рез) — провал 08:59:40 в своём сегменте: "
      f"{[d.values.get('live') for _, d in d_cls]}")
check(not dets(segment(RS_DAY, x_cl_rows, [], [], cfgB, "SN", "pcc", 1, "E", X0, XC, live_edge=True),
               "RPM_UNDERSPEED"), "11г: segment(live_edge=True) — открытое окно ждёт следующего")
check("live_edge=True" in inspect.getsource(eng.OnlinePollEngine._update_open_window)
      and "live_edge" not in inspect.getsource(eng._run_segment),
      "11г: живой край — только у открытого окна движка; рез (_run_segment) — закрытое")

# ── 11д. compat-1: 1–2 сбоя датчика оборотов — наблюдением в полном отчёте, в сводке — нет ──
# (rv/compat/r9_glitch1.py, #35876): проверка passed=True молчала, минимум оборотов в таблице
# менялся без объяснения
md2 = to_markdown(s2_, "SN", "pcc", 1, W0, W1, tz=MSK)
check("ℹ️ `source_rpm_glitch`" in md2 and "⚠️ `source_rpm_glitch`" not in md2
      and "**Наблюдения по последовательности**" in md2 and "сбой датчика" in md2 and "04:42:32" in md2,
      f"11д: 2 сбоя — строка ℹ️ в отчёте (МСК): {[x for x in md2.splitlines() if 'glitch' in x]}")
p2 = build_summary_parts(s2_, [], tz=MSK, trip_roles=[])
check(not [c for c in p2["failed_checks"] if c.get("check") == "source_rpm_glitch"]
      and "отброшены отсчёты" not in render_summary_md(p2) and p2["verdict"]["level"] == "НОРМА",
      "11д: в сводке — только от 3 сбоев (В4)")
md3 = to_markdown(s3, "SN", "pcc", 1, W0, W1, tz=MSK)
check("⚠️ `source_rpm_glitch`" in md3 and "ℹ️ `source_rpm_glitch`" not in md3,
      "11д: 3 сбоя — как было, предупреждением")

if _errors:
    print(f"FAIL — {len(_errors)} расхождений:")
    for x in _errors:
        print(f"  • {x}")
    sys.exit(1)
print("Все проверки пройдены.")
