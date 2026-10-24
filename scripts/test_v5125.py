# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""v5.1.25 «Честные цифры и тексты» (план docs/plans/design_v5125.md, решения
владельца В-0.1′, В-0.1″, В-25.1…В-25.4 и разбор ДЭС №3 24.10.2026 —
docs/plans/audit_des3_2026-10-24.md, решения А и Б).

Этап 1 — аналитика без движка (§2 плана):
- 1.1 П1/П2 (confirm_exit): бит готовности, вставший раньше стопа перед
  попыткой, не подтверждает; бит при показаниях оборотов/частоты ниже порогов
  не подтверждает (ready_ignored); бит без показаний — как раньше; пуск 13.10
  13:12:26 и пуск, где бит раньше первого отсчёта выше порога (критика M6), —
  момент прежний; допуск кадра на голове окна (SHOULD 8); живой край — «ждём
  подтверждения»;
- 1.2 синтетика ДЭС №3 24.10 через segment(): «Стоп» 12:38:59–12:41:05 и
  плашка «Неудачный пуск · Стоп» с 12:41:05, отсчётов внутри нет, профиля
  нет; попытка 13:34:03 — «Неудачный пуск», а не «Отменённый»;
- 1.3 П3 (start_profile): fire без перехода порогов — None, с переходом — как
  раньше;
- 1.4 подписи (решение А): cranked None → «Неудачный пуск», факты «прокрутка
  по данным не видна»; отсчёты внутри без вращения — «Отменённый»; старая
  запись — как раньше;
- 1.5 признаки характера по-русски: новые голоса, перевод 6 старых строк
  (критика M2), латиницы нет, вход совета, реконструкция корпуса, копия акта
  в API (web/routes._incident_ru);
- 1.6 «Обычно» (В-25.3): короткая работа — «—» и пояснение, по реальным
  отсчётам (SHOULD 4), долгая — как раньше, части без n — как раньше,
  открытый сегмент — «Работа идёт …»;
- 1.7 «(идёт)» (В-25.3): открытый / суточный рез / смена режима; движок
  передаёт причину и конец явно (критика M3);
- 1.8 LOAD_STEP (В-25.1): восстановление от минимума (test_v13), шаг опроса
  и «не более N с», пары мощности ближе 1 с, онсет — первая пара выше порога
  (критика M7), INFO без эпизода и группы, CAUTION — точечный эпизод, два
  события — два эпизода, ключ KB в обоих слоях, «Производные метрики»;
- 1.9 П10, чистая часть (движок подключает на этапе 2): оценка работы за
  обрыв по счётчику наработки на числах #36065 («до ~12:05–12:08», 6 ч 18
  мин), ΔH = 0, весь перерыв, остановился до сегмента, сменились счётчики
  пусков, нет H1; строка заглушки «Последние данные до обрыва»; ключи KB
  no_data_estimate (шаг — в слое контроллера, SHOULD 5).

Этап 2 — движок (§3 плана, подмена online.db и счёт запросов):
- 2.1 P2-3: _known_ends и _drop_debounce_tails на данных #35944 — период
  «Сброса нагрузки» 1 мин 51 с, просадки 70 с; живой на конец, снятие раньше
  t_open и позже w1 — открыт; два фронта бита, второй не снят (M5); фронт,
  прошедший между циклами; сводка открытого сегмента — снятый в дебаунсе снятым;
- 2.2 count_episodes_batch(ends=…): SQL с unnest и LEFT JOIN по открытым,
  без ends — пустые массивы; эмуляция агрегата — 111/120 и 70/81; оба пути
  закрытия и открытое окно передают ends; посев снятого фронта до снятия;
- 2.3 П4: снятие позже конца окна — живая, _live_new_episodes согласован,
  запрет снятия «сигнал панели» (SHOULD 3);
- 2.4 П5: ДЭС №3 24.10 — кандидаты 422 и 197, один запрос и 2 вставки, затем
  0 запросов; рестарт — 1 запрос и 0 вставок; живой того же онсета — пропуск;
  Сводка открытого с «(код 422)» 26 с и «(код 197)» 10 с; сбой вставки без
  _open_failed; порядок в цикле; после закрытия дебаунсом — 0 запросов (SHOULD 1);
- 2.5 П6: детекция (код, имя, начало кода без обрезки — M4, #36068 через
  segment()), имя новой и старой (14901) записи, смена 1453 → 1248 тем же
  UPDATE, переоткрытие с другим кодом, пометка только в #36067, 10 записей;
- 2.6 П7: контекст перекрытия, «superseded» / «condition_cleared», суффикс
  «перекрыта предупреждением (аварией) панели», «Висит также» вне вердикта и
  без дубля, M8;
- 2.7 П8: перерыв от последних данных — «6 ч 51 мин»;
- 2.8 П10: последние данные по разрывам (M1), оценка по наработке и счётчику
  попыток пуска одним запросом, нет H1, сбой; обе ветки заглушки;
- 2.9 сторож v5.1.24: сбой новых шагов не ставит флаги цикла, удалённое —
  через remote;
- 2.10 В-25.4: журнал тревог — служебный.

Этап 3 — значок акта, сервер (§4.1 плана, решение В-25.2):
- 3.1 analysis_badge: ready (и при терминальной отметке), pending (< 72 ч),
  failed (терминальная отметка / 72 ч / старый без заказа), none; сроки — из
  констант; причина failed (SHOULD 6); момент останова акта;
- 3.2 get_segments_for_calendar: без актов — один запрос; с актами — два
  (unnest, окно ±2 сут, ключи incident_hash); без incident_state (движок,
  HTML-календарь) — один; сбой второго — строки без состояния;
  get_incident_analysis_states на своём соединении;
- 3.3 API: incident_analysis / incident_analysis_reason в /segments и
  /segment/{id}, has_incident на месте, null при непрочитанном состоянии;
  описание в docs/ui_api_guide.md.

Ревью v5.1.25 — подтверждённые замечания:
- 4.1 Q1: «до ~» и «не работал» — только если на обрыве работал и счётчик
  пусков есть и не менялся; стоял на обрыве / KTA50 без счётчика пусков —
  только наработка (hours_only), функция и путь движка;
- 4.2 Q2/Q7: продолжение работы через суточный рез — «С 09:00 — 40 с
  (1 отсчёт)», не «Работа идёт» и не «Короткая работа»;
- 4.3 Q9: часы работы с 6 отсчётами — «Мало отсчётов за работу»;
- 4.4 Q10: «на панели сейчас» — только у открытого сегмента;
- 4.5 Q4: «superseded» через 62–70 с от начала окна — в Сводке через
  настоящий путь (_filter_window_episodes, _build_summary_md_for);
- 4.6 Q3: панельный эпизод — по фронту (ДЭС №3 13.10, 1438): второй фронт в
  дебаунсе первого — разрез по снятию своего, контекст аварии — новому;
  первый промах — снятие своего фронта; снятие на конце сегмента — конец;
- 4.7 Q5: пуск прошёл без связи — пометка, без «масла перед пуском» и
  момента автомата.

Без БД, LLM и сети (online.db подменяется).

Запуск:  PYTHONIOENCODING=utf-8 py -3 scripts/test_v5125.py
"""
from __future__ import annotations

import ast
import asyncio
import copy
import inspect
import logging
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import analytics.segmenter as _sg  # noqa: E402
import online.db as _odb  # noqa: E402
import online.engine as eng  # noqa: E402
from analytics import classifier as clf  # noqa: E402
from analytics import labels  # noqa: E402
from analytics.config import AnalyticsConfig  # noqa: E402
from analytics.contract import (  # noqa: E402
    EVENT_SCENARIOS, Detection, DerivedMetrics, RiskAccumulators, Segment, Subsegment,
)
from analytics.detectors import _detect_load_step  # noqa: E402
from analytics.metrics import (  # noqa: E402
    _freq_transient_metrics, _max_speed, _poll_step_sec, compute_derived_metrics, recovery_bound_text,
)
from analytics.segmenter import confirm_exit, confirm_params, segment, start_profile  # noqa: E402
from analytics.serializer import (  # noqa: E402
    _event_groups, _ongoing_label, _violation_line, build_summary_parts, render_summary_md, to_markdown,
)

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
for _name in ("analytics", "online", "corpus"):
    logging.getLogger(_name).addHandler(LOG)
    logging.getLogger(_name).propagate = False


def logged(text: str) -> bool:
    return any(text in r.getMessage() for r in LOG.records)


KB = ROOT / "knowledge_base"
CFG = AnalyticsConfig.from_pair(KB, "pcc3300", "cummins_kta50")
CFG3 = AnalyticsConfig.from_pair(KB, "pcc3300", "cummins_qsx15")
RPM, FREQ, HB, PWR = 40068, 40044, 40290, CFG3.role_to_addr("ACTIVE_POWER_TOTAL")
SW, RC = CFG3.role_to_addr("SWITCH_POS"), CFG3.role_to_addr("RUN_COMMAND")
_LAT3 = re.compile(r"[A-Za-z]{3,}")


def M(d: int, h: int, m: int, s: int = 0, mo: int = 10) -> datetime:
    """Метка МСК → UTC."""
    return datetime(2026, mo, d, h, m, s, tzinfo=MSK).astimezone(U)


def S(t: datetime, sec: float) -> datetime:
    return t + timedelta(seconds=sec)


def h(addr, ts, v):
    return {"addr": addr, "ts": ts, "value": v}


def rs(v, a, b=None):
    return {"addr": 40011, "value": v, "state_start": a, "state_end": b}


def en(addr, v, a, b=None, label=None):
    return {"addr": addr, "value": v, "state_start": a, "state_end": b, "label": label}


def by_addr(rows):
    out: dict[int, list[dict]] = {}
    for r in sorted(rows, key=lambda r: r["ts"]):
        out.setdefault(r["addr"], []).append(r)
    return out


def lab(s):
    return labels.label_from_chars(s.to_dict()) or {0: "Стоп", 2: "Прогрев", 3: "Работа"}.get(s.run_state)


def hm(t) -> str:
    return datetime.fromisoformat(t).astimezone(MSK).strftime("%H:%M:%S") if t else "—"


# Бит «Готов к нагрузке» ДЭС №3: с 13.10 14:59:49 до первого кадра масок после связи 24.10
READY_BIT = {"addr": 40408, "bit": 7, "severity": None, "fault_start": M(13, 14, 59, 49),
             "fault_end": M(24, 12, 41, 50), "fault_name_ru": "Готов к нагрузке",
             "fault_name": "Готов к нагрузке"}

# ══ 1.1. П1/П2: confirm_exit ═══════════════════════════════════════════════
PRM = confirm_params(CFG3)
A0, A1 = M(24, 12, 41, 5), M(24, 12, 41, 13)
STOP0 = M(24, 12, 38, 59)
READY_OLD = [(M(13, 14, 59, 49), M(24, 12, 41, 50))]

st, at, fx = confirm_exit(A0, A1, [], [], READY_OLD, [], PRM, ready_since=STOP0)
check(st == "no_data" and at is None and fx["ready"] is False and fx.get("ready_ignored") == "stale",
      f"1.1а: бит с 13.10 старше стопа 12:38:59 — не засчитан (П1): {st} {fx}")
st, at, fx = confirm_exit(A0, A1, [(M(24, 12, 41, 4), 0.0)], [], READY_OLD, [], PRM)
check(st == "unconfirmed" and fx["ready"] is False and fx.get("ready_ignored") == "no_rotation",
      f"1.1б: удержанные обороты 0 и бит — не подтверждает (П2): {st} {fx}")
st, at, fx = confirm_exit(A0, A1, [(M(24, 12, 41, 4), 0.0)], [], READY_OLD, [], PRM, ready_since=STOP0)
check(st == "unconfirmed" and fx.get("ready_ignored") in ("stale", "no_rotation") and not fx["ready"],
      f"1.1б: оба правила — не подтверждает: {st} {fx}")
st, at, fx = confirm_exit(A0, A1, [], [], [(S(A0, 3), S(A0, 60))], [], PRM, ready_since=STOP0)
check(st == "confirmed" and at == S(A0, 3) and fx["ready"] is True and "ready_ignored" not in fx,
      f"1.1в: бит без единого показания оборотов и частоты — подтверждает, как раньше: {st} {at} {fx}")
st, at, fx = confirm_exit(A0, A1, [], [], [(S(A0, 3), S(A0, 60))], [], PRM)
check(st == "confirmed" and at == S(A0, 3), "1.1в: без ready_since — как раньше")
# Пуск 13.10 13:12:26 (test_transition_confirm_v5116: бит со сменой режима, 1498 об/мин в 13:12:25)
B0 = M(13, 13, 12, 26)
_rpm13 = [(S(B0, -1), 1498.0), (S(B0, 8), 1497.0)]
st, at, fx = confirm_exit(B0, M(13, 13, 13, 2), _rpm13, [(B0, 49.8)], [(B0, M(13, 13, 13, 2))], [], PRM,
                          ready_since=M(13, 10, 45, 26))
check(st == "confirmed" and at == B0 and fx["ready"] is True,
      f"1.1г: пуск 13.10 13:12:26 — подтверждён, момент прежний: {st} {at} {fx}")
# Критика M6: бит в кадре смены режима, первый отсчёт выше порога через 9 с —
# бит засчитывается (порог перешли), момент — бит, а не отсчёт
C0 = M(24, 13, 40, 11)
_rpm40 = [(S(C0, -40), 0.0), (S(C0, 9), 1561.0)]
st, at, fx = confirm_exit(C0, S(C0, 600), _rpm40, [], [(C0, S(C0, 3600))], [], PRM,
                          ready_since=M(24, 13, 35, 5))
st0, at0, _ = confirm_exit(C0, S(C0, 600), _rpm40, [], [(C0, S(C0, 3600))], [], PRM)
check(st == st0 == "confirmed" and at == at0 == C0 and fx["ready"] is True,
      f"1.1д: бит раньше первого отсчёта выше порога — момент не сдвинулся (M6): {at} / {at0}")
# Голова окна: стоп до окна — начало цепочки минус кадр опроса (SHOULD 8)
_P = [rs(0, M(24, 12, 0), M(24, 12, 40)), rs(1, M(24, 12, 40), M(24, 12, 41, 5)), rs(3, A0, A1)]
check(_sg._ready_since(_P, 2) == M(24, 12, 0) and _sg._ready_since([rs(3, A0, A1)], 0) == S(A0, -1.0),
      f"1.1е: начало стопа (задержка пуска пропускается) / голова окна с допуском кадра: "
      f"{_sg._ready_since(_P, 2)} {_sg._ready_since([rs(3, A0, A1)], 0)}")
st, at, fx = confirm_exit(A0, A1, [], [], [(S(A0, -0.4), S(A0, 60))], [], PRM, ready_since=S(A0, -1.0))
check(st == "confirmed" and at == A0, f"1.1е: бит на 0,4 с раньше смены режима — тот же кадр, засчитан: {st} {at}")
# Цепочка без бита (циклическая прокрутка 16.10, 189 об/мин ниже 250) — как раньше
_rpm16 = [(S(A0, -10), 0.0), (S(A0, 3), 171.0), (S(A0, 9), 189.0)]
check(confirm_exit(A0, S(A0, 20), _rpm16, [], [], [], PRM, ready_since=STOP0)[0]
      == confirm_exit(A0, S(A0, 20), _rpm16, [], [], [], PRM)[0] == "unconfirmed",
      "1.1ж: без бита готовности П1/П2 ничего не меняют")

# Живой край: цепочка идёт, стопа после нет — «ждём подтверждения»
_EN_LIVE = [rs(0, STOP0, A0), rs(3, A0)]
_H24 = [h(RPM, STOP0, 0.0), h(RPM, M(24, 12, 40, 1), 0.0), h(RPM, M(24, 12, 41, 4), 0.0),
        h(RPM, M(24, 12, 42, 7), 0.0), h(FREQ, STOP0, 0.0)]
_live = segment(_EN_LIVE, [r for r in _H24 if r["ts"] < M(24, 12, 41, 12)], [READY_BIT], [], CFG3,
                "SN", "pcc", 1, "E", STOP0, M(24, 12, 41, 12))
check([lab(s) for s in _live] == ["Стоп", "Работа — ждём подтверждения"] and _live[-1].attempt_pending,
      f"1.1з: живой край — «ждём подтверждения», а не «Работа»: {[lab(s) for s in _live]}")

# ══ 1.2. Синтетика ДЭС №3 24.10 через segment() ════════════════════════════
_EN24 = [rs(0, STOP0, A0), rs(3, A0, A1), rs(0, A1)]
_s24 = segment(_EN24, _H24, [READY_BIT], [], CFG3, "SN", "pcc", 1, "E", STOP0, M(24, 12, 50))
_got = [(hm(s.t_start), hm(s.t_end), lab(s)) for s in _s24]
check(_got == [("12:38:59", "12:41:05", "Стоп"), ("12:41:05", "12:50:00", "Неудачный пуск · Стоп")],
      f"1.2а: «Стоп» 12:38:59–12:41:05 и плашка «Неудачный пуск · Стоп» с 12:41:05: {_got}")
_a24 = _s24[-1].attempt if _s24 and _s24[-1].attempt else {}
check(_a24.get("rpm_in") == 0 and "cranked" in _a24 and _a24["cranked"] is None
      and _a24["facts"]["ready"] is False,
      f"1.2б: отсчётов оборотов внутри нет — cranked None, «Готов к нагрузке» не засчитан: {_a24}")
_f24 = labels.attempt_facts_ru(_a24, MSK) or ""
check(_f24.startswith("попытка 8 с, прокрутка по данным не видна (отсчётов внутри попытки нет)"),
      f"1.2в: факты: {_f24}")
check(all(getattr(s, "start_profile", None) is None for s in _s24) and "Пуск:" not in
      to_markdown(_s24, "SN", "pcc", 1, STOP0, M(24, 12, 50), tz=MSK),
      "1.2г: профиля пуска и строки «Пуск» нет")
# Попытка 13:34:03 (#36072): отсчёты 13:33:34 и 13:34:37, внутри 13:34:03–13:34:10 нет
D0, D1 = M(24, 13, 34, 3), M(24, 13, 34, 10)
_EN34 = [rs(0, M(24, 12, 41, 13), D0), rs(3, D0, D1), rs(0, D1),
         en(SW, 2, M(24, 13, 33, 50), D1, "Ручной"), en(SW, 0, D1, None, "Выкл"),
         en(RC, 0, M(24, 13, 33, 43), None, "нет команды (0)")]
_H34 = [h(RPM, M(24, 13, 33, 34), 0.0), h(RPM, M(24, 13, 34, 37), 0.0), h(FREQ, M(24, 13, 30), 0.0)]
_s34 = segment(_EN34, _H34, [], [], CFG3, "SN", "pcc", 1, "E", M(24, 13, 30), M(24, 13, 36))
_p34 = [s for s in _s34 if s.attempt]
_f34 = labels.attempt_facts_ru(_p34[0].attempt, MSK) if _p34 else ""
check(_p34 and lab(_p34[0]) == "Неудачный пуск · Стоп"
      and _f34.startswith("попытка 7 с, прокрутка по данным не видна (отсчётов внутри попытки нет)")
      and "без прокрутки" not in _f34,
      f"1.2д: 13:34:03 — «Неудачный пуск · Стоп» (было «Отменённый», ноль из 13:33:34): "
      f"{_p34 and lab(_p34[0])} / {_f34}")

# ══ 1.3. П3: профиль пуска без перехода порогов не строится ═══════════════
_c = M(24, 13, 40, 11)
_b0 = by_addr([h(RPM, S(_c, -5), 0.0), h(RPM, S(_c, 20), 0.0), h(FREQ, S(_c, -5), 0.0)])
check(start_profile(_b0, [], CFG3, _c, _c, S(_c, 60)) is None
      and start_profile(_b0, [], CFG3, _c, None, S(_c, 60)) is None,
      "1.3а: «схватил» только битом (fire есть), обороты 0 — профиля нет")
_b1 = by_addr([h(RPM, S(_c, -5), 0.0), h(RPM, S(_c, 5), 600.0), h(RPM, S(_c, 9), 1500.0),
               h(FREQ, S(_c, 9), 50.0)])
_pf = start_profile(_b1, [], CFG3, _c, S(_c, 2), S(_c, 60))
_pf0 = start_profile(_b1, [], CFG3, _c, None, S(_c, 60))
check(_pf is not None and _pf["fire_at"] == S(_c, 2).isoformat() and _pf["rated_at"] == S(_c, 9).isoformat()
      and _pf0 is not None and _pf0["fire_at"] == S(_c, 5).isoformat(),
      f"1.3б: обороты перешли порог — как раньше (fire переданный / первый отсчёт > 250): "
      f"{_pf and _pf['fire_at']} {_pf0 and _pf0['fire_at']}")

# ══ 1.4. Подписи попытки (решение А) ═══════════════════════════════════════
_an = {"cranked": None, "facts": {"rpm_max": 0}, "outcome": "SIMPLE", "codes": []}
check(labels.attempt_cranked(_an) is None and labels.attempt_label(_an) == "Неудачный пуск · Стоп",
      "1.4а: cranked None с rpm_max 0 — None, «Неудачный пуск · Стоп» (не проваливается в rpm_max)")
_ai = {"cranked": None, "rpm_in": 0, "facts": {"rpm_max": 0.0}, "outcome": "SIMPLE", "codes": [],
       "duration_sec": 7.0}
check(labels.attempt_facts_ru(_ai) == "попытка 7 с, прокрутка по данным не видна (отсчётов внутри попытки нет)",
      f"1.4б: факты без отсчётов внутри: {labels.attempt_facts_ru(_ai)}")
_af = {"cranked": False, "rpm_in": 1, "facts": {"rpm_max": 0.0}, "outcome": "SIMPLE", "codes": [],
       "duration_sec": 9.0}
check(labels.attempt_label(_af) == "Отменённый пуск · Стоп"
      and labels.attempt_facts_ru(_af) == "попытка 9 с без прокрутки (обороты 0)",
      "1.4в: отсчёты внутри без вращения, без кода — «Отменённый пуск · Стоп», как раньше")
_au = {"cranked": None, "facts": {}, "outcome": "SIMPLE", "codes": [], "duration_sec": 7.0}
check(labels.attempt_facts_ru(_au) == "попытка 7 с, обороты неизвестны",
      "1.4г: записи без rpm_in и без оборотов — прежнее «обороты неизвестны»")
_old = {"path": [3], "codes": [], "facts": {"ready": False, "rpm_max": 0.0, "freq_max": 0.0},
        "outcome": "SIMPLE", "from_state": 3, "duration_sec": 7.0}
check(labels.attempt_label(_old) == "Отменённый пуск · Стоп"
      and labels.attempt_cranked(dict(_old, facts={"rpm_max": 189.0})) is True,
      "1.4д: старая запись без cranked (#36072 в БД) — по rpm_max, как раньше")
_v23 = {"cranked": True, "facts": {"rpm_max": 189.0}, "outcome": "SIMPLE", "codes": []}
check(labels.attempt_label(_v23) == "Неудачный пуск · Стоп", "1.4е: запись v5.1.23 с cranked — как была")

# ══ 1.5. Признаки характера по-русски ══════════════════════════════════════
_NEW = {clf.VOTE_NO_COOLDOWN: "без охлаждения перед остановом (режим 40011 не проходил 4/5)",
        clf.VOTE_COOLDOWN: "через охлаждение (режим 40011 прошёл 4/5)",
        clf.VOTE_RC_VIA_STOP: "команда работы 40599 через «Стоп»",
        clf.VOTE_RC_DIRECT: "команда «Работа» снята сразу (3→0), без «Стоп»",
        clf.VOTE_FT_SHUTDOWN: "тип неисправности 40013 — «Останов» (4)",
        clf.VOTE_FT_SHUTDOWN_COOLDOWN: "тип неисправности 40013 — «Останов с охлаждением» (3)"}
check(all(k == v for k, v in _NEW.items()), f"1.5а: голоса по плану: {[k for k, v in _NEW.items() if k != v]}")
# Все 6 старых строк из БД (8 актов за 45 дней, критика M2)
_OLD6 = ["RUN_STATE без cooldown 4/5", "RUN_STATE через cooldown", "RunCommand через «Стоп»",
         "RunCommand прямой EmergencyStop", "40013=Shutdown", "40013=ShutdownWithCooldown"]
_RU6 = [clf.VOTE_NO_COOLDOWN, clf.VOTE_COOLDOWN, clf.VOTE_RC_VIA_STOP, clf.VOTE_RC_DIRECT,
        clf.VOTE_FT_SHUTDOWN, clf.VOTE_FT_SHUTDOWN_COOLDOWN]
check([clf.vote_ru(v) for v in _OLD6] == _RU6, f"1.5б: старые голоса при чтении: {[clf.vote_ru(v) for v in _OLD6]}")
check([clf.vote_ru(v) for v in _RU6] == _RU6, "1.5б: новые — как есть")


def _per(addr, pts):
    out = []
    for i, (t, v) in enumerate(pts):
        out.append({"addr": addr, "value": v, "state_start": t,
                    "state_end": pts[i + 1][0] if i + 1 < len(pts) else None})
    return out


_st = M(16, 9, 28, 53)
_votes = []
for _ep in (
        _per(40011, [(S(_st, -20), 3), (_st, 0)]) + _per(40599, [(S(_st, -21), 3), (_st, 0)])
        + _per(40013, [(S(_st, -1), 4)]),                                           # немедленный
        _per(40011, [(S(_st, -400), 3), (S(_st, -300), 5), (_st, 0)])
        + _per(40599, [(S(_st, -400), 3), (S(_st, -301), 1), (_st, 0)]) + _per(40013, [(S(_st, -1), 3)]),
        _per(40011, [(S(_st, -20), 3), (_st, 0)])):                                 # без 40599/40013
    _ch = clf.classify_stop_character(_ep, _st)
    _votes += _ch["immediate_votes"] + _ch["controlled_votes"]
check(_votes and not [v for v in _votes if _LAT3.search(v)] and clf.VOTE_FT_SHUTDOWN in _votes
      and clf.VOTE_RC_VIA_STOP in _votes and clf.VOTE_COOLDOWN in _votes,
      f"1.5в: нет латиницы ≥ 3 букв подряд в голосах классификатора: {_votes}")
LOG.records.clear()
check(clf.vote_ru("Unknown vote X") == "Unknown vote X" and logged("Голос характера без перевода"),
      "1.5г: неизвестный голос с латиницей — как есть и WARNING")
check(clf.character_text({"character": "immediate", "confidence": "high"}) == "немедленный (уверенность высокая)"
      and clf.character_text({"character": "controlled", "confidence": "low"})
      == "контролируемый (уверенность низкая)"
      and clf.character_text({"character": "unknown", "confidence": "low"}) == "неясен (уверенность низкая)",
      "1.5д: CHARACTER_RU / CONFIDENCE_RU")
# Старый акт #35941 (как в БД)
_INC = {"stop_ts": M(16, 9, 28, 53).isoformat(), "window": {},
        "character": {"character": "immediate", "confidence": "high", "is_incident": True,
                      "signals": {"passed_cooldown": False, "fault_type_40013": 4,
                                  "run_command_path": "direct_estop", "run_state_before": 3,
                                  "is_fall_from_work": True},
                      "immediate_votes": ["RUN_STATE без cooldown 4/5", "RunCommand прямой EmergencyStop",
                                          "40013=Shutdown"], "controlled_votes": []},
        "chronology": []}
_INC0 = copy.deepcopy(_INC)
from online.incident_gate import build_incident_prompt  # noqa: E402

_pr = build_incident_prompt(_INC, None, [], {}, tz=MSK)
check("Характер по детерминированному гейту: немедленный (уверенность высокая)" in _pr
      and ("Сигналы: " + "; ".join([clf.VOTE_NO_COOLDOWN, clf.VOTE_RC_DIRECT, clf.VOTE_FT_SHUTDOWN])) in _pr
      and "RUN_STATE" not in _pr and "EmergencyStop" not in _pr and "immediate" not in _pr,
      f"1.5е: вход совета — по-русски: {[x for x in _pr.splitlines() if 'Характер' in x or 'Сигналы' in x]}")
from corpus.preprocessor import _format_incident  # noqa: E402

_fi = _format_incident(_INC)
check("**Характер:** немедленный (уверенность высокая); падение из работы: да" in _fi
      and "**Сигналы:** охлаждение пройдено — нет; команда работы 40599 — снята сразу; "
          "режим 40011 перед остановом — 3; тип 40013 — 4" in _fi
      and f"**За немедленный:** {clf.VOTE_NO_COOLDOWN}; {clf.VOTE_RC_DIRECT}; {clf.VOTE_FT_SHUTDOWN}" in _fi
      and not _LAT3.search(_fi.split("**Лента")[0]),
      f"1.5ж: реконструкция корпуса — по-русски: {_fi.splitlines()[:6]}")
# API: копия акта с голосами по-русски (функция из web/routes.py — без fastapi)
_rsrc = (ROOT / "web" / "routes.py").read_text(encoding="utf-8")
_fn = next(n for n in ast.parse(_rsrc).body if isinstance(n, ast.FunctionDef) and n.name == "_incident_ru")
_ns: dict = {"Any": Any, "logger": logging.getLogger("online.test_routes")}
exec(compile(ast.Module(body=[_fn], type_ignores=[]), "routes._incident_ru", "exec"), _ns)
_api = _ns["_incident_ru"](_INC)
check(_api["character"]["immediate_votes"] == [clf.VOTE_NO_COOLDOWN, clf.VOTE_RC_DIRECT, clf.VOTE_FT_SHUTDOWN]
      and _api["character"]["character"] == "immediate" and _api["character"]["signals"] == _INC["character"]["signals"]
      and _api["chronology"] == [] and _INC == _INC0
      and _ns["_incident_ru"](None) is None and _ns["_incident_ru"]({"x": 1}) == {"x": 1},
      f"1.5з: API — копия с переводом, коды и исходный акт не тронуты: {_api['character']}")
check('_inc = _parse_json(seg.get("incident_json"), None, ctx="incident_json")' in _rsrc
      and '"incident_json": _incident_ru(_inc)' in _rsrc,
      "1.5з: /api/segment/{id} отдаёт incident_json через _incident_ru (акт разобран один раз — "
      "он же для значка, этап 3)")

# ══ 1.6. «Обычно» — короткая работа (В-25.3) ═══════════════════════════════
W0 = M(16, 9, 29, 33)


def SUBK(a, b, chars, transient=False, dets=()):
    s = Subsegment(id=f"s.{a}", parent_segment_id="s", t_start=S(W0, a).isoformat(),
                   t_end=S(W0, b).isoformat(), duration_sec=b - a, load_zone="NORMAL",
                   cause_open="ZONE_CHANGE", cause_close=None, characteristics=chars,
                   derived_metrics=DerivedMetrics(), risk_accumulators=RiskAccumulators(),
                   detections=list(dets), data_quality=1.0, data_gaps=[])
    s.transient = transient
    return s


def SEGK(subs, rs_=3, cause_close="RUN_STATE_CHANGE", t_end=None):
    return Segment(id="s", router_sn="R", equip_type="pcc", panel_id=1, engine_sn="", run_state=rs_,
                   run_state_label="Работа", t_start=subs[0].t_start,
                   t_end=subs[-1].t_end if t_end is None else t_end,
                   duration_sec=sum(s.duration_sec for s in subs), engine_hours_start=None,
                   cause_open="RUN_STATE_CHANGE", cause_close=cause_close, preamble_included=False,
                   data_quality=1.0, subsegments=subs, sequence_checks=[], events=[])


def CH(unit, med, mn, mx, vs, ve, n, real=None):
    d = {"unit": unit, "median": med, "min": mn, "max": mx, "value_start": vs, "value_end": ve,
         "sample_count": n}
    if real is not None:
        d["real_count"] = real
    return d


# Аналог #35929: 20 с, 2 реальных отсчёта (строк окна с forward-fill больше)
_k29 = SEGK([SUBK(0, 20, {"RPM": CH("rpm", 1471.5, 1247, 1696, 1696, 1247, 3, 2),
                          "FREQUENCY": CH("Hz", 49.15, 41.5, 56.8, 56.8, 41.5, 3, 2)})])
_pk = build_summary_parts([_k29], [], tz=MSK, trip_roles=["RPM", "FREQUENCY"])
_km = _pk["key_metrics"]
check(_km.get("short") == {"sec": 20.0, "n": 2, "rpm": [1696, 1247], "freq": [56.8, 41.5], "open": False,
                           "by": "time", "since": None, "since_text": None}
      and [r.get("n") for r in _km["rows"]] == [2, 2],
      f"1.6а: короткая работа в частях: {_km.get('short')}")
_md29 = render_summary_md(_pk)
check("| Обороты двигателя | — | 1247 | 1696 | об/мин |" in _md29
      and "| Частота генератора | — | 41,5 | 56,8 | Гц |" in _md29
      and "Короткая работа (20 с, 2 отсчёта): обороты 1696 → 1247 об/мин, частота 56,8 → 41,5 Гц" in _md29
      and "1471" not in _md29,
      f"1.6а: «—» во всех строках и пояснение: {[x for x in _md29.splitlines() if '|' in x or 'Корот' in x]}")
# Работа 3 ч — как раньше
_k3h = SEGK([SUBK(0, 10800, {"RPM": CH("rpm", 1500.0, 1490, 1510, 1500, 1501, 1080, 1080),
                             "FREQUENCY": CH("Hz", 50.0, 49.8, 50.2, 50.0, 50.0, 1080, 1080)})])
_p3h = build_summary_parts([_k3h], [], tz=MSK, trip_roles=["RPM", "FREQUENCY"])
_md3h = render_summary_md(_p3h)
check("short" not in _p3h["key_metrics"] and "| Обороты двигателя | 1500 | 1490 | 1510 | об/мин |" in _md3h
      and "Короткая работа" not in _md3h,
      f"1.6б: работа 3 ч — «Обычно» медианой: {[x for x in _md3h.splitlines() if 'Обороты' in x]}")
# Части без n (сохранённые до v5.1.25) — как раньше
_pold = copy.deepcopy(_p3h)
for _r in _pold["key_metrics"]["rows"]:
    for _k in ("n", "start", "end"):
        _r.pop(_k, None)
check(render_summary_md(_pold) == _md3h, "1.6в: части без n — печать прежняя")
# Долгая работа, но отсчётов оборотов < 10 — коротко по отсчётам
_k5 = SEGK([SUBK(0, 300, {"RPM": CH("rpm", 1500.0, 1490, 1510, 1490, 1510, 30, 5)})])
check((build_summary_parts([_k5], [], tz=MSK, trip_roles=["RPM"])["key_metrics"].get("short") or {}).get("n") == 5,
      "1.6г: 5 мин, но 5 реальных отсчётов оборотов (ff — 30) — «—» (по реальным, SHOULD 4)")
# Характеристики без счёта отсчётов — только по времени (test_summary_v5120, 3д)
_kn = SEGK([SUBK(0, 7200, {"RPM": {"unit": "rpm", "median": 1500, "min": 1487, "max": 1515}})])
check("short" not in build_summary_parts([_kn], [], tz=MSK, trip_roles=["RPM"])["key_metrics"],
      "1.6д: без sample_count/real_count — по времени, 2 ч не короткая")
# Открытый сегмент, первые 100 с — «—» и «Работа идёт»
_ko = SEGK([SUBK(0, 100, {"RPM": CH("rpm", 1500.0, 1495, 1503, 1495, 1500, 11, 11),
                          "FREQUENCY": CH("Hz", 50.0, 49.9, 50.1, 49.9, 50.0, 11, 11)})], cause_close=None)
_po = build_summary_parts([_ko], [], tz=MSK, trip_roles=["RPM", "FREQUENCY"], is_open=True)
_mdo = render_summary_md(_po)
check("| Обороты двигателя | — |" in _mdo
      and "Работа идёт 1 мин 40 с (11 отсчётов): обороты 1495 → 1500 об/мин, частота 49,9 → 50 Гц" in _mdo,
      f"1.6е: открытый сегмент 100 с — «—» и «Работа идёт …»: {[x for x in _mdo.splitlines() if 'Работа' in x]}")
check("is_open=not closing" in inspect.getsource(eng.OnlinePollEngine._build_summary_md_for),
      "1.6е: движок передаёт is_open в сводку открытого сегмента")

# ══ 1.7. «(идёт)» → по причине закрытия (В-25.3, критика M3) ═══════════════
_t_cl = M(25, 9, 0)
_ent = [(1, {"values": {"live": {"clusters": [{"start": S(_t_cl, -120).isoformat(), "end": _t_cl.isoformat(),
                                               "ongoing": True, "viol_sec": 120.0}]}}})]


def _vl(close):
    return _violation_line(_ent, MSK, close) or ""


check("(идёт)" in _vl(None) and "(идёт)" in _vl((None, _t_cl, 3))
      and "(продолжается после 09:00)" in _vl(("DAILY_BOUNDARY", _t_cl, 3))
      and "(до конца работы)" in _vl(("RUN_STATE_CHANGE", _t_cl, 3))
      and "(до конца режима)" in _vl(("FAULT_CLEARED", _t_cl, 0))
      and "(идёт)" not in _vl(("DAILY_BOUNDARY", _t_cl, 3)),
      f"1.7а: подписи: {_vl(None)} | {_vl(('DAILY_BOUNDARY', _t_cl, 3))} | {_vl(('RUN_STATE_CHANGE', _t_cl, 3))}")
check(_ongoing_label(("DAILY_BOUNDARY", None, 3)) == " (продолжается после суточного реза)",
      "1.7а: суточный рез без конца — без «None»")
_det_live = Detection(scenario="LIMIT_PROXIMITY", severity="CAUTION", t_detected=S(_t_cl, -120).isoformat(),
                      source="METRIC_RULE", trigger="t", related_roles=[], fault_codes=[], description_key="k",
                      values={"live": {"clusters": [{"start": S(_t_cl, -120).isoformat(), "end": _t_cl.isoformat(),
                                                      "ongoing": True, "viol_sec": 120.0}]}})
W0 = S(_t_cl, -600)
_sv = SEGK([SUBK(0, 600, {}, dets=[_det_live])], cause_close=None, t_end=None)
_md_open = to_markdown([_sv], "SN", "pcc", 1, W0, _t_cl, tz=MSK)
_md_cut = to_markdown([_sv], "SN", "pcc", 1, W0, _t_cl, tz=MSK, last_close=("DAILY_BOUNDARY", _t_cl))
_md_rs = to_markdown([_sv], "SN", "pcc", 1, W0, _t_cl, tz=MSK, last_close=("RUN_STATE_CHANGE", _t_cl))
check("(идёт)" in _md_open and "(продолжается после 09:00)" in _md_cut and "(идёт)" not in _md_cut
      and "(до конца работы)" in _md_rs,
      "1.7б: to_markdown — без last_close по сегменту, с ним — по фактической причине")
_esrc = inspect.getsource(eng)
check("last_close=(seg_cause_close, seg_t_end)" in _esrc
      and 'last_close=(seg.cause_close or "RUN_STATE_CHANGE", seg_t_end_rs)' in _esrc,
      "1.7в: движок передаёт причину и конец в обоих путях закрытия (M3)")

# ══ 1.8. LOAD_STEP (В-25.1) ════════════════════════════════════════════════
t0 = datetime(2024, 1, 1, 12, 0, 0, tzinfo=U)
_fs = [(t0, 50.0), (S(t0, 1), 48.5), (S(t0, 2), 47.5), (S(t0, 3), 49.0), (S(t0, 5), 49.8)]
check(_freq_transient_metrics(_fs, 50.0, 0.5, t0) == (5.0, None, 3.0),
      f"1.8а: восстановление от минимума — 3,0 с (test_v13, тест 1): {_freq_transient_metrics(_fs, 50.0, 0.5, t0)}")
_d = _detect_load_step(DerivedMetrics(dP_dt_max=60.0, freq_dip_pct=5.0, freq_recovery_sec=3.0), t0, S(t0, 60), CFG3)
check(len(_d) == 1 and _d[0].severity == "INFO" and not _d[0].values["violations"]
      and _d[0].values["recovery_checked"] is True,
      f"1.8б: 3,0 с при норме G3 3 с — нарушения нет: {_d and _d[0].values}")
_rt = recovery_bound_text(10.0, 10.0, "G3")
check(_rt == "не более 20 с (шаг опроса 10 с, норма G3 не проверяется)", f"1.8в: подпись: {_rt}")
_d = _detect_load_step(DerivedMetrics(dP_dt_max=60.0, freq_dip_pct=3.0, freq_recovery_sec=10.0,
                                      freq_poll_step_sec=10.0, freq_recovery_text=_rt), t0, S(t0, 60), CFG3)
check(len(_d) == 1 and _d[0].severity == "INFO" and not _d[0].values["violations"]
      and _d[0].values["recovery_checked"] is False and _d[0].values["poll_step_sec"] == 10.0
      and _d[0].trigger == "скорость изменения мощности 60 кВт/с; провал частоты 3,0 %; восстановление "
                           "не более 20 с (шаг опроса 10 с, норма G3 не проверяется) — в пределах G3",
      f"1.8в: шаг 10 с > нормы 3 с — восстановление не сравнивается: {_d and _d[0].trigger}")
_d = _detect_load_step(DerivedMetrics(dP_dt_max=60.0, freq_dip_pct=8.0, freq_recovery_sec=10.0,
                                      freq_poll_step_sec=10.0, freq_recovery_text=_rt), t0, S(t0, 60), CFG3)
check(len(_d) == 1 and _d[0].severity == "CAUTION" and _d[0].values["violations"] == ["freq_dip=8.0% > G3:7.0%"]
      and _d[0].trigger == "скорость изменения мощности 60 кВт/с; провал частоты 8,0 % больше нормы G3 7 %; "
                           "восстановление не более 20 с (шаг опроса 10 с, норма G3 не проверяется)",
      f"1.8г: провал 8 % — CAUTION, ключи violations прежние: {_d and (_d[0].trigger, _d[0].values['violations'])}")
_d = _detect_load_step(DerivedMetrics(dP_dt_max=60.0, freq_dip_pct=3.0, freq_recovery_sec=5.0,
                                      freq_poll_step_sec=2.0), t0, S(t0, 60), CFG3)
check(len(_d) == 1 and _d[0].severity == "CAUTION" and _d[0].values["recovery_checked"] is True
      and "восстановление 5 с больше нормы G3 3 с" in _d[0].trigger,
      f"1.8г: шаг 2 с ≤ нормы — восстановление сравнивается: {_d and _d[0].trigger}")
# Пара мощности через 0,12 с (пульс db-writer) — не в счёт
_ps = [(t0, 100.0), (S(t0, 0.12), 160.0), (S(t0, 10), 170.0)]
check(_max_speed(_ps, [], 30, 3, absolute=True) == 500.0
      and abs(_max_speed(_ps, [], 30, 3, absolute=True, min_dt=1.0) - 10 / 9.88) < 1e-6,
      "1.8д: пара мощности через 0,12 с отброшена")
# Сквозной расчёт: пинг раз в 10 с, наброс 100 → 700 кВт за 10 с, провал до 46 Гц
A = M(31, 13, 27, 0, mo=8)


def _ls_rows(extra_p=(), n=31):
    rows = [h(HB, S(A, 10 * k), float(k)) for k in range(n)]
    rows += [h(RPM, S(A, 10 * k + 1), 1500.0) for k in range(n)]
    rows += [h(PWR, S(A, s), v) for s, v in ((0, 100.0), (10, 100.0), (20, 700.0), (20.12, 760.0),
                                              (30, 720.0), (40, 720.0)) + tuple(extra_p)]
    rows += [h(FREQ, S(A, s), v) for s, v in ((0, 50.0), (20, 48.0), (30, 46.0), (40, 49.9), (50, 50.0))]
    return by_addr(rows)


_dm = compute_derived_metrics(_ls_rows(), A, S(A, 300), [], CFG3)
check(_dm.dP_dt_max == 60.0 and _dm.dP_dt_max_at == S(A, 20).isoformat()
      and _dm.dP_dt_onset_at == S(A, 20).isoformat() and _dm.freq_poll_step_sec == 10.0
      and _dm.freq_recovery_sec == 10.0
      and _dm.freq_recovery_text == "не более 20 с (шаг опроса 10 с, норма G3 не проверяется)",
      f"1.8е: dP/dt без пары 0,12 с, шаг опроса по пингу, подпись: {_dm.dP_dt_max} {_dm.dP_dt_max_at} "
      f"{_dm.freq_poll_step_sec} {_dm.freq_recovery_text}")
check(_poll_step_sec(_ls_rows(), A, S(A, 300), CFG3) == 10.0
      and _poll_step_sec(by_addr([h(HB, A, 0.0)]), A, S(A, 300), CFG3) is None,
      "1.8е: шаг опроса — медиана интервалов пакетов; один пакет — None")
_dl = _detect_load_step(_dm, A, S(A, 300), CFG3)
check(len(_dl) == 1 and _dl[0].t_detected == S(A, 20).isoformat() and _dl[0].severity == "CAUTION"
      and _dl[0].values["dP_dt_max_at"] == S(A, 20).isoformat(),
      f"1.8е: t_detected — онсет события (первая пара выше порога): {_dl and _dl[0].t_detected}")
# Критика M7: подсегмент растёт, позже пара круче — онсет прежний, момент наибольшей сдвинулся
_dm2 = compute_derived_metrics(_ls_rows(extra_p=((200, 720.0), (210, 1720.0)), n=31), A, S(A, 300), [], CFG3)
_dl2 = _detect_load_step(_dm2, A, S(A, 300), CFG3)
check(_dm2.dP_dt_onset_at == S(A, 20).isoformat() and _dm2.dP_dt_max_at == S(A, 210).isoformat()
      and _dl2 and _dl2[0].t_detected == S(A, 20).isoformat()
      and "(наибольшая в " in _dl2[0].trigger,
      f"1.8ж: рост подсегмента — онсет 20 с, наибольшая 210 с (M7): {_dm2.dP_dt_onset_at} {_dm2.dP_dt_max_at} "
      f"{_dl2 and _dl2[0].trigger}")
# «Производные метрики»: восстановление подписью, моментов нет
_sub_dm = SUBK(0, 300, {})
_sub_dm.derived_metrics = _dm
W0 = A
_md_dm = to_markdown([SEGK([_sub_dm])], "SN", "pcc", 1, A, S(A, 300), tz=MSK)
check("| `freq_recovery_sec` | не более 20 с (шаг опроса 10 с, норма G3 не проверяется) |" in _md_dm
      and "dP_dt_max_at" not in _md_dm and "freq_recovery_text" not in _md_dm and "dP_dt_onset_at" not in _md_dm
      and "| `freq_poll_step_sec` | 10.0 |" in _md_dm,
      f"1.8з: «Производные метрики»: {[x for x in _md_dm.splitlines() if 'freq_' in x or 'dP_' in x]}")
# Разовое событие: в EVENT_SCENARIOS, ключ KB в обоих слоях
check("LOAD_STEP" in EVENT_SCENARIOS, "1.8и: LOAD_STEP — разовое событие")
_mono = AnalyticsConfig(KB / "equipment" / "cummins_kta50_pcc3300")
check(CFG.det("LOAD_STEP", "min_pair_dt_sec") == 1 and _mono.det("LOAD_STEP", "min_pair_dt_sec") == 1
      and CFG3.det("LOAD_STEP", "min_pair_dt_sec") == 1
      and "min_pair_dt_sec: 1" in (KB / "_defaults" / "analytics" / "detectors.yaml").read_text(encoding="utf-8")
      and "min_pair_dt_sec: 1" in (KB / "equipment" / "cummins_kta50_pcc3300" / "analytics" / "detectors.yaml"
                                   ).read_text(encoding="utf-8"),
      "1.8и: LOAD_STEP.min_pair_dt_sec = 1 в _defaults и в монолите")


def LS(sev, t, trig="скорость изменения мощности 60 кВт/с; провал частоты 8,0 % больше нормы G3 7 %"):
    return Detection(scenario="LOAD_STEP", severity=sev, t_detected=t.isoformat(), source="METRIC_RULE",
                     trigger=trig, related_roles=[], fault_codes=[], description_key="LOAD_STEP.x",
                     values={"violations": []})


class SeedDB:
    """Подмена online.db для посева: запись запросов и вставок, без БД."""

    def __init__(self, onsets=()):
        self.onsets = set(onsets)
        self.ins: list[dict] = []
        self.asked: list[list] = []

    async def existing_episode_onsets(self, sn, et, pid, onsets):
        self.asked.append(list(onsets))
        return set(self.onsets)

    async def get_episodes_overlapping(self, *a, **k):
        return []

    async def insert_closed_episode(self, *a, **k):
        self.ins.append(k)
        return len(self.ins)

    async def count_episodes_batch(self, *a, **k):
        return {}


_SEED_NAMES = ("existing_episode_onsets", "get_episodes_overlapping", "insert_closed_episode",
               "count_episodes_batch")
_orig_seed = {n: getattr(_odb, n) for n in _SEED_NAMES}


def _with_db(db, coro_fn):
    for n in _SEED_NAMES:
        setattr(eng.online_db, n, getattr(db, n))
    try:
        return asyncio.run(coro_fn())
    finally:
        for n, f in _orig_seed.items():
            setattr(eng.online_db, n, f)


W0 = A
E1, E2 = S(A, 20), S(A, 1820)
_seg_info = SEGK([SUBK(0, 1800, {}, dets=[LS("INFO", E1)])])
_seg_cau = SEGK([SUBK(0, 1800, {}, dets=[LS("CAUTION", E1)])])
_seg_two = SEGK([SUBK(0, 1800, {}, dets=[LS("CAUTION", E1)]), SUBK(1800, 3600, {}, dets=[LS("CAUTION", E2)])])


def _collect(segx, db):
    return _with_db(db, lambda: eng._collect_and_enrich_detections(
        segx, "SN", "pcc", 1, _sg._tz(datetime.fromisoformat(segx.t_end)), CFG3, A,
        open_keys=set(), seeded_out=set(), gaps=[], live_episodes={}))


def _mk_engine():
    e = eng.OnlinePollEngine.__new__(eng.OnlinePollEngine)
    e.cfg = CFG3
    e.router_sn, e.equip_type, e.panel_id = "SN", "pcc", 1
    e._events_seeded = set()
    return e


_db = SeedDB()
_collect(_seg_info, _db)
check(not _db.ins, f"1.8к: INFO при закрытии — 0 вставок: {_db.ins}")
_db = SeedDB()
_eo = _mk_engine()
_with_db(_db, lambda: _eo._seed_open_events(_seg_info, S(A, 600)))
check(not _db.ins and not _db.asked, "1.8к: INFO в живом цикле — ни запроса, ни вставки")
check(_event_groups([_seg_info], [], A, S(A, 3600), MSK) == []
      and _event_groups([_seg_info], [{"scenario": "LOAD_STEP", "severity": "INFO", "t_open": E1.isoformat()}],
                        A, S(A, 3600), MSK) == [],
      "1.8к: INFO — ни группы, ни запасного эпизода")
_pinfo = build_summary_parts([_seg_info], [], tz=MSK, trip_roles=[])
check(not _pinfo["remarks"] and _pinfo["verdict"]["level"] == "НОРМА",
      f"1.8к: INFO — без вида в «ЗАМЕЧАНИЯ» и цвета: {_pinfo['verdict']}")
_db = SeedDB()
_collect(_seg_cau, _db)
check(len(_db.ins) == 1 and _db.ins[0]["close_reason"] == "transition_event"
      and _db.ins[0]["t_open"] == _db.ins[0]["t_close"] == E1 and _db.ins[0]["active_sec"] == 0.0,
      f"1.8л: CAUTION при закрытии — точечный эпизод transition_event: {_db.ins}")
_pcau = build_summary_parts([_seg_cau], [], tz=MSK, trip_roles=[])
_mdc = render_summary_md(_pcau)
check(_pcau["remarks"] and _pcau["remarks"][0]["scenario"] == "LOAD_STEP" and "событие" in _mdc,
      f"1.8л: замечание «событие …»: {[x for x in _mdc.splitlines() if 'событ' in x]}")
_db = SeedDB()
_eo = _mk_engine()
_with_db(_db, lambda: _eo._seed_open_events(_seg_cau, S(A, 600)))
_with_db(_db, lambda: _eo._seed_open_events(_seg_cau, S(A, 900)))
check(len(_db.ins) == 1 and len(_db.asked) == 1, f"1.8л: живой цикл — одна вставка на онсет: {len(_db.ins)}")
_db = SeedDB()
_collect(_seg_two, _db)
check(sorted(k["t_open"] for k in _db.ins) == [E1, E2],
      f"1.8м: два события в одном сегменте — два эпизода: {[k['t_open'] for k in _db.ins]}")
_db = SeedDB(onsets={("LOAD_STEP", E1)})
_collect(_seg_two, _db)
check([k["t_open"] for k in _db.ins] == [E2], "1.8м: засеянный онсет не дублируется")

# ══ 1.9. П10, чистая часть: оценка работы за обрыв по счётчику наработки ═══
from analytics.serializer import (  # noqa: E402
    build_no_data_report, no_data_estimate_params, no_data_run_estimate, no_data_summary_parts,
)

# #36065: H0 05:47:11 = 55 985 400 с, H1 12:38:58 = 56 008 080 с, сегмент 09:00–12:38:59
H0, H1 = (M(24, 5, 47, 11), 55985400.0), (M(24, 12, 38, 58), 56008080.0)
# На обрыве работал (3), счётчик пусков есть и не менялся (ревью Q1: иначе — только наработка)
_est = no_data_run_estimate(H0, H1, 180, M(24, 9, 0), starts_changed=False, run_state=3)
check(_est and _est["kind"] == "until" and _est["worked_sec"] == 22680.0
      and _est["end_from"] == M(24, 12, 5, 11).isoformat() and _est["end_to"] == M(24, 12, 8, 11).isoformat(),
      f"1.9а: #36065 — работал до 12:05:11–12:08:11: {_est}")
_t = labels.no_data_hours_text(_est, MSK)
check(_t == "Наработка за перерыв по счётчику — 6 ч 18 мин: двигатель работал до ~12:05–12:08 "
           "(счётчик идёт шагом 3 мин).", f"1.9а: текст: {_t}")
check(no_data_run_estimate(H0, (H1[0], H0[1]), 180, M(24, 9, 0))["kind"] == "idle"
      and labels.no_data_hours_text(no_data_run_estimate(H0, (H1[0], H0[1]), 180, M(24, 9, 0)), MSK)
      == "По счётчику наработки за перерыв двигатель не работал.",
      "1.9б: ΔH = 0 — «не работал»")
_wh = no_data_run_estimate(H0, (H1[0], H0[1] + (H1[0] - H0[0]).total_seconds() - 60), 180, M(24, 9, 0))
check(_wh["kind"] == "whole" and "работал весь перерыв" in labels.no_data_hours_text(_wh, MSK),
      f"1.9в: конец не раньше H1 − шаг — «весь перерыв»: {_wh}")
_bf = no_data_run_estimate(H0, (H1[0], H0[1] + 3600), 180, M(24, 9, 0), starts_changed=False, run_state=3)
check(_bf["kind"] == "before"
      and labels.no_data_hours_text(_bf, MSK) == "В этот период не работал (по счётчику наработки "
                                                 "остановился ~24.10 06:47–06:50).",
      f"1.9г: конец раньше начала сегмента: {labels.no_data_hours_text(_bf, MSK)}")
_sc = no_data_run_estimate(H0, H1, 180, M(24, 9, 0), starts_changed=True)
check(_sc["kind"] == "starts" and _sc["end_from"] is None and "до ~" not in labels.no_data_hours_text(_sc, MSK)
      and "были остановы и пуски" in labels.no_data_hours_text(_sc, MSK),
      "1.9д: счётчики пусков сменились — без «до ~» (SHOULD 5)")
check(no_data_run_estimate(H0, None, 180, M(24, 9, 0)) is None and no_data_run_estimate(None, H1, 180, M(24, 9, 0))
      is None and labels.no_data_hours_text(None) is None, "1.9е: нет H0/H1 — оценки нет")
_rmd, _smd = build_no_data_report("SN", "pcc", 1, M(24, 9, 0), M(24, 12, 38, 59), tz=MSK,
                                  last_data_ts=M(24, 5, 48, 15), run_estimate=_est)
check("Последние данные до обрыва: 2026-10-24 05:48:15" in _smd and "получены" not in _smd,
      f"1.9ж: строка «Последние данные до обрыва»: {_smd}")
check("до ~12:05–12:08" in _smd and "до ~12:05–12:08" in _rmd and "## ⚠ Нет связи" in _smd,
      "1.9ж: оценка — в обеих частях заглушки")
_r0, _s0 = build_no_data_report("SN", "pcc", 1, M(24, 9, 0), M(24, 12, 38, 59), tz=MSK)
check("Данных за период нет." in _s0 and "Наработка" not in _s0, "1.9ж: без оценки и времени — как раньше")
_ndp = no_data_summary_parts(_est)
check(_ndp["run_estimate"] == _est and _ndp["verdict"] is None and no_data_summary_parts()["run_estimate"] is None,
      "1.9з: run_estimate в частях для API, вердикта нет")
_mono = AnalyticsConfig(KB / "equipment" / "cummins_kta50_pcc3300")
check(no_data_estimate_params(CFG) == no_data_estimate_params(CFG3) == no_data_estimate_params(_mono)
      == {"enabled": True, "step_sec": 180.0, "lookback_sec": 172800.0}
      and "engine_hours_step_sec" not in (KB / "_defaults" / "analytics" / "segmentation.yaml").read_text(
          encoding="utf-8").split("no_data_estimate:")[1]
      and "engine_hours_step_sec: 180" in (KB / "controllers" / "pcc3300" / "analytics" / "segmentation.yaml"
                                           ).read_text(encoding="utf-8"),
      "1.9и: ключи no_data_estimate — шаг в слое контроллера, остальное в _defaults, всё в монолите")

# ══════════════════════════ Этап 2 — движок (§3 плана) ══════════════════════
from online.status_assembler import suppression_bans  # noqa: E402

CA_KEY = "CONTROLLER_FAULT|40408|11"
LD_KEY = "CONTROLLER_FAULT|40408|6"


def PF(addr, bit, sev, fs, fe=None, name="Неисправность", extra=None):
    """Детекция панели — как _detect_controller_faults."""
    v = {"addr": addr, "bit": bit, "fault_name": name, "bit_name": name,
         "fault_start": fs.isoformat(), "fault_end": fe.isoformat() if fe else None,
         "raw_severity": (sev or "").lower()}
    v.update(extra or {})
    return Detection(scenario="CONTROLLER_FAULT", severity=sev, t_detected=fs.isoformat(),
                     source="CONTROLLER_FAULT", trigger=f"{name} (addr={addr}, bit={bit})",
                     related_roles=[], fault_codes=[], description_key="x", values=v)


def AD(sc, t):
    return Detection(scenario=sc, severity="CAUTION", t_detected=t.isoformat(), source="METRIC_RULE",
                     trigger=sc, related_roles=[], fault_codes=[], description_key="x", values={})


def SUB2(a, b, dets=(), chars=None):
    return Subsegment(id=f"x.{a.isoformat()}", parent_segment_id="x", t_start=a.isoformat(),
                      t_end=b.isoformat(), duration_sec=(b - a).total_seconds(), load_zone="NORMAL",
                      cause_open="ZONE_CHANGE", cause_close=None, characteristics=dict(chars or {}),
                      derived_metrics=DerivedMetrics(), risk_accumulators=RiskAccumulators(),
                      detections=list(dets), data_quality=1.0, data_gaps=[])


def SEG2(subs, rs_=3, cause_close="RUN_STATE_CHANGE", events=()):
    return Segment(id="x", router_sn="R", equip_type="pcc", panel_id=1, engine_sn="", run_state=rs_,
                   run_state_label="Работа", t_start=subs[0].t_start, t_end=subs[-1].t_end,
                   duration_sec=sum(s.duration_sec for s in subs), engine_hours_start=None,
                   cause_open="RUN_STATE_CHANGE", cause_close=cause_close, preamble_included=False,
                   data_quality=1.0, subsegments=subs, sequence_checks=[], events=list(events))


class EpDB:
    """Подмена online.db для этапа 2: запросы и записи — в списки, без БД."""

    def __init__(self, onsets=(), eps=(), prev=None, fail_insert=False):
        self.onsets, self.eps, self.prev = set(onsets), list(eps), prev
        self.fail_insert = fail_insert
        self.asked: list[list] = []
        self.ins: list[dict] = []
        self.calls: list[tuple] = []
        self.counts: list[dict] = []

    async def existing_episode_onsets(self, sn, et, pid, onsets):
        self.asked.append(list(onsets))
        return set(self.onsets)

    async def insert_closed_episode(self, *a, **k):
        if self.fail_insert:
            raise RuntimeError("нет БД")
        self.ins.append(k)
        return len(self.ins)

    async def get_episodes_overlapping(self, *a, **k):
        return [dict(e) for e in self.eps]

    async def count_episodes_batch(self, *a, **k):
        self.counts.append(k)
        return {}

    async def find_episode_by_onset(self, *a, **k):
        self.calls.append(("find", k))
        return self.prev

    async def open_episode(self, *a, **k):
        self.calls.append(("open", k))
        return 501

    async def reopen_episode(self, ep_id, **k):
        self.calls.append(("reopen", ep_id, k))

    async def update_episode(self, ep_id, **k):
        self.calls.append(("update", ep_id, k))

    async def close_episode(self, ep_id, t_close, reason, **k):
        self.calls.append(("close", ep_id, t_close, reason, k))


_EP_NAMES = ("existing_episode_onsets", "insert_closed_episode", "get_episodes_overlapping",
             "count_episodes_batch", "find_episode_by_onset", "open_episode", "reopen_episode",
             "update_episode", "close_episode")
_orig_ep = {n: getattr(_odb, n) for n in _EP_NAMES}


def _with_db2(db, coro_fn):
    for n in _EP_NAMES:
        setattr(eng.online_db, n, getattr(db, n))
    try:
        return asyncio.run(coro_fn())
    finally:
        for n, f in _orig_ep.items():
            setattr(eng.online_db, n, f)


def _mk_engine2(cfg=CFG3, episodes=None):
    e = eng.OnlinePollEngine.__new__(eng.OnlinePollEngine)
    e.cfg, e.tz, e.poll_interval_sec = cfg, MSK, 30
    e.router_sn, e.equip_type, e.panel_id = "SN", "pcc", 1
    e._events_seeded, e._panel_seeded, e._panel_seeded_seg = set(), set(), None
    e._episodes = dict(episodes or {})
    e._episode_accrual_ts, e._last_open_seg_id, e._supp_recheck = None, 7, None
    e._prev_seg_hint = None
    return e


# ══ 2.1. P2-3: известный конец открытого эпизода (#35944 ДГУ №1 16.10) ═════
G0, G1 = M(16, 9, 29, 33), M(16, 9, 32, 33)   # окно: «2 мин 51 с» = эпизод до конца окна
_d44 = SEG2([SUB2(G0, G1, [PF(40408, 6, "WARNING", M(16, 9, 29, 42), M(16, 9, 31, 33), "Сброс нагрузки"),
                          AD("RPM_UNDERSPEED", M(16, 9, 30, 2))])])
_mem44 = {LD_KEY: {"id": 14929, "severity": "WARNING", "miss": 2, "first_miss_ts": M(16, 9, 31, 33),
                   "t_open": M(16, 9, 29, 42), "exact": False, "until": None},
          "RPM_UNDERSPEED": {"id": 14930, "severity": "CAUTION", "miss": 1, "first_miss_ts": M(16, 9, 31, 12),
                             "t_open": M(16, 9, 30, 2), "exact": True, "until": M(16, 9, 31, 12),
                             "until_kind": "end"}}
_eps44 = [{"id": 14929, "scenario": "CONTROLLER_FAULT", "source": "panel", "severity": "WARNING", "addr": 40408,
           "bit": 6, "t_open": M(16, 9, 29, 42), "t_close": None, "active_sec": 86.0, "blind_sec": 0.0,
           "gate_suppressed": False, "open_values_json": {"fault_name": "Сброс нагрузки"}},
          {"id": 14930, "scenario": "RPM_UNDERSPEED", "source": "analytics", "severity": "CAUTION",
           "t_open": M(16, 9, 30, 2), "t_close": None, "active_sec": 127.0, "blind_sec": 0.0,
           "gate_suppressed": False}]
_kn44 = eng._known_ends([_d44], _mem44, set(), G1)
check(_kn44 == {LD_KEY: {"id": 14929, "end": M(16, 9, 31, 33), "exact": False},
                "RPM_UNDERSPEED": {"id": 14930, "end": M(16, 9, 31, 12), "exact": True}},
      f"2.1а: известные концы #35944 — снятие 09:31:33 и конец просадки 09:31:12: {_kn44}")
_dt44 = eng._drop_debounce_tails(copy.deepcopy(_eps44), [_d44], _mem44, set(), w1=G1)
_by44 = {e["id"]: e for e in _dt44}
check(_by44[14929]["t_close"] == M(16, 9, 31, 33) and _by44[14929]["active_sec"] == 111.0
      and _by44[14930]["t_close"] == M(16, 9, 31, 12) and _by44[14930]["active_sec"] == 70.0
      and _eps44[0]["t_close"] is None,
      f"2.1а: копии для сводки — 111 с и 70 с, строки не тронуты: {[(e['id'], e['active_sec']) for e in _dt44]}")


def _line(md, name):
    return next((x for x in md.splitlines() if f"**{name}" in x), "")


_md44 = render_summary_md(build_summary_parts([_d44], _dt44, tz=MSK, trip_roles=[], t_from=G0, t_to=G1))
_l44 = _line(_md44, "Сброс нагрузки")
check("воздействие за период 1 мин 51 с" in _l44 and "активен на конец" not in _l44,
      f"2.1а: «Сброс нагрузки» — 1 мин 51 с, не «на конец»: {_l44}")
_md44o = render_summary_md(build_summary_parts([_d44], [_eps44[0]], tz=MSK, trip_roles=[], t_from=G0, t_to=G1))
check("воздействие за период 2 мин 51 с" in _line(_md44o, "Сброс нагрузки"),
      f"2.1а: было — 2 мин 51 с до конца окна: {_line(_md44o, 'Сброс нагрузки')}")
check(LD_KEY not in eng._known_ends([_d44], _mem44, {LD_KEY}, G1),
      "2.1б: живой на конец (live_keys) — остаётся открытым")
check(LD_KEY not in eng._known_ends([_d44], {LD_KEY: dict(_mem44[LD_KEY], t_open=M(16, 9, 32, 0))}, set(), G1),
      "2.1в: снятие раньше начала эпизода — прежний период маски, не конец")
check(LD_KEY not in eng._known_ends([_d44], _mem44, set(), M(16, 9, 31, 0)),
      "2.1г: снятие позже w1 — открыт")
_d44b = SEG2([SUB2(G0, G1, [PF(40408, 6, "WARNING", M(16, 9, 29, 42), M(16, 9, 31, 33), "Сброс нагрузки"),
                           PF(40408, 6, "WARNING", M(16, 9, 31, 50), None, "Сброс нагрузки")])])
# Ревью v5.1.25 (Q3): эпизод панели — по фронту; второй висящий фронт — свой
# эпизод, конец первого — его снятие. Висит свой фронт — конца нет (M5)
check(eng._known_ends([_d44b], _mem44, set(), G1)[LD_KEY]["end"] == M(16, 9, 31, 33)
      and LD_KEY not in eng._known_ends([_d44b], {LD_KEY: dict(_mem44[LD_KEY], front=M(16, 9, 31, 50))},
                                        set(), G1),
      "2.1д: два фронта бита, второй не снят — конец первого по его снятию (Q3); висит свой — конца нет (M5)")
_d44c = SEG2([SUB2(G0, G1, [PF(40408, 6, "WARNING", M(16, 9, 29, 42), M(16, 9, 31, 33), "Сброс нагрузки"),
                           PF(40408, 6, "WARNING", M(16, 9, 32, 0), M(16, 9, 32, 10), "Сброс нагрузки")])])
check(eng._known_ends([_d44c], _mem44, set(), G1)[LD_KEY]["end"] == M(16, 9, 31, 33)
      and eng._known_ends([_d44c], {LD_KEY: dict(_mem44[LD_KEY], front=M(16, 9, 32, 0))}, set(),
                          G1)[LD_KEY]["end"] == M(16, 9, 32, 10),
      "2.1д: фронт, прошедший между циклами, — свой (посев); живой второй фронт — конец по нему")
# Сводка открытого сегмента: снятый в дебаунсе — снятым
_e21 = _mk_engine2(CFG, _mem44)
_db21 = EpDB(eps=_eps44)
_md21, _p21 = _with_db2(_db21, lambda: _e21._build_summary_md_for([_d44], G0, G1, live_keys=set()))
check(_md21 and "воздействие за период 1 мин 51 с" in _line(_md21, "Сброс нагрузки")
      and "активен на конец" not in _line(_md21, "Сброс нагрузки"),
      f"2.1е: открытая сводка — снятый в дебаунсе снятым: {_line(_md21 or '', 'Сброс нагрузки')}")
_md21b, _ = _with_db2(_db21, lambda: _e21._build_summary_md_for([_d44], G0, G1, live_keys={LD_KEY}))
check("воздействие за период 2 мин 51 с" in _line(_md21b or "", "Сброс нагрузки"),
      f"2.1е: живой в цикле — открытым до конца окна, как раньше: {_line(_md21b or '', 'Сброс нагрузки')}")
_usrc = inspect.getsource(eng.OnlinePollEngine._update_open_window)
check("live_keys=_open_live_keys" in _usrc and "known_ends=_kn_open" in _usrc,
      "2.1е: открытое окно передаёт живой состав сводке и известные концы счётчикам")

# ══ 2.2. count_episodes_batch(ends=…) ══════════════════════════════════════


class _FC2:
    def __init__(self):
        self.q: list[tuple] = []

    async def fetch(self, sql, *args):
        self.q.append((sql, args))
        return []

    async def fetchrow(self, sql, *args):
        self.q.append((sql, args))
        return None

    async def execute(self, sql, *args):
        self.q.append((sql, args))
        return "UPDATE 1"

    async def close(self):
        pass


_fc2 = _FC2()
_orig_connect = _odb._connect


async def _fc2_connect():
    return _fc2


_odb._connect = _fc2_connect
try:
    asyncio.run(_odb.count_episodes_batch("SN", "pcc", 1, ["CONTROLLER_FAULT"], 30, G0,
                                          ends={14929: (M(16, 9, 31, 33), False), 14930: (M(16, 9, 31, 12), True)}))
    asyncio.run(_odb.count_episodes_batch("SN", "pcc", 1, ["CONTROLLER_FAULT"], 30, G0))
    (_sq, _a1), (_, _a2) = _fc2.q
    check("unnest($8::bigint[], $9::timestamptz[], $10::bool[])" in _sq
          and "LEFT JOIN u ON u.id = a.id AND a.t_close IS NULL" in _sq
          and "$7::timestamptz IS NULL OR source IS DISTINCT FROM 'analytics'" in _sq,
          "2.2а: SQL — unnest и LEFT JOIN по открытым строкам, фильтр аналитики прежний")
    check(_a1[7] == [14929, 14930] and _a1[8] == [M(16, 9, 31, 33), M(16, 9, 31, 12)] and _a1[9] == [False, True]
          and _a2[7] == [] and _a2[8] == [] and _a2[9] == [],
          f"2.2б: массивы id→конец; без ends — пустые: {_a1[7:]}, {_a2[7:]}")
finally:
    _odb._connect = _orig_connect


def _emu_counts(rows, ends, since):
    """Агрегат count_episodes_batch в Python по тем же правилам (окно 30 дней — все строки)."""
    per: dict[tuple, list] = {}
    for r in rows:
        u = ends.get(r["id"]) if r["t_close"] is None else None
        a, b = r["active_sec"], r["blind_sec"]
        if u is not None:
            span = max(0.0, (u[0] - r["t_open"]).total_seconds())
            a, b = (span, min(b, span)) if u[1] else (max(a, span), b)
        k = (r["scenario"], r["addr"], r["bit"], r["t_open"])
        cur = per.setdefault(k, [0.0, 0.0])
        cur[0], cur[1] = max(cur[0], a), max(cur[1], b)
    out: dict[str, list] = {}
    for (sc, ad, bt, t0), (a, _b) in per.items():
        key = _odb.episode_key(sc, ad, bt)
        o = out.setdefault(key, [0.0, 0.0])
        o[0] += a
        if t0 >= since:
            o[1] += a
    return out


_rows44 = [
    {"id": 14929, "scenario": "CONTROLLER_FAULT", "addr": 40408, "bit": 6, "t_open": M(16, 9, 29, 42),
     "t_close": None, "active_sec": 86.0, "blind_sec": 0.0},
    {"id": 14926, "scenario": "CONTROLLER_FAULT", "addr": 40408, "bit": 6, "t_open": M(16, 9, 10, 0),
     "t_close": M(16, 9, 10, 9), "active_sec": 9.0, "blind_sec": 0.0},
    {"id": 14930, "scenario": "RPM_UNDERSPEED", "addr": None, "bit": None, "t_open": M(16, 9, 30, 2),
     "t_close": None, "active_sec": 127.0, "blind_sec": 0.0},
    {"id": 14925, "scenario": "RPM_UNDERSPEED", "addr": None, "bit": None, "t_open": M(16, 9, 9, 50),
     "t_close": M(16, 9, 10, 1), "active_sec": 11.0, "blind_sec": 0.0}]
_ends44 = eng._known_ends_arg(_kn44)
_c44 = _emu_counts(_rows44, _ends44, G0)
_c44o = _emu_counts(_rows44, {}, G0)
check(_c44[LD_KEY] == [120.0, 111.0] and _c44["RPM_UNDERSPEED"] == [81.0, 70.0]
      and _c44o[LD_KEY] == [95.0, 86.0] and _c44o["RPM_UNDERSPEED"] == [138.0, 127.0],
      f"2.2в: #35944 — «Сброс нагрузки» 111 с пуска / 120 за 30 дней, просадка 70 / 81 (было 86/95, 127/138): "
      f"{_c44}, {_c44o}")
check(_ends44 == {14929: (M(16, 9, 31, 33), False), 14930: (M(16, 9, 31, 12), True)},
      f"2.2г: аргумент ends из карты известных концов: {_ends44}")
_db22 = EpDB(onsets={(LD_KEY, M(16, 9, 29, 42))})
_with_db2(_db22, lambda: eng._collect_and_enrich_detections(
    _d44, "SN", "pcc", 1, G1, CFG, G0, open_keys={LD_KEY, "RPM_UNDERSPEED"}, seeded_out=set(), gaps=[],
    live_episodes=_mem44, known_ends=_kn44))
_db22b = EpDB()
_with_db2(_db22b, lambda: eng._enrich_open_seg_detections(_d44, "SN", "pcc", 1, CFG, G0, known_ends=_kn44))
_db22c = EpDB()
_with_db2(_db22c, lambda: eng._enrich_open_seg_detections(_d44, "SN", "pcc", 1, CFG, G0))
check(_db22.counts and _db22.counts[0].get("ends") == _ends44 and _db22b.counts[0].get("ends") == _ends44
      and "ends" not in _db22c.counts[0],
      "2.2г: закрытие и открытое окно передают ends счётчикам; без известных концов — как раньше")
_csrc = inspect.getsource(eng.OnlinePollEngine._close_window)
check("known_ends=_kn_seg" in _csrc and "known_ends=_kn_rs" in _usrc
      and "_live_keys if is_last else set()" in _csrc and "_live_keys if ci == len(closed_segs) - 1 else set()" in _usrc,
      "2.2д: оба пути закрытия считают концы сегмента (живые — только у последнего)")
# Посев при закрытии: снятие позже конца сегмента — тоже снятие
_d22 = SEG2([SUB2(G0, M(16, 9, 31, 0), [PF(40408, 6, "WARNING", M(16, 9, 29, 42), M(16, 9, 31, 33), "Сброс")])])
_db22d = EpDB()
_with_db2(_db22d, lambda: eng._collect_and_enrich_detections(
    _d22, "SN", "pcc", 1, M(16, 9, 31, 0), CFG, G0, open_keys=set(), seeded_out=set(), gaps=[], live_episodes={}))
check(len(_db22d.ins) == 1 and _db22d.ins[0]["t_close"] == M(16, 9, 31, 33) and _db22d.ins[0]["active_sec"] == 111.0,
      f"2.2е: снятый фронт сеется до снятия 09:31:33, хотя сегмент кончился в 09:31:00: "
      f"{[(k['t_close'], k['active_sec']) for k in _db22d.ins]}")

# ══ 2.3. П4: снятие позже окна — неисправность ещё висит ════════════════════
_T4 = M(24, 12, 46, 50)
_s4 = SEG2([SUB2(M(24, 12, 41, 13), _T4, [PF(40403, 15, "WARNING", M(24, 12, 46, 30), M(24, 12, 46, 56))])],
           rs_=0, cause_close=None)
_s4b = SEG2([SUB2(M(24, 12, 41, 13), _T4, [PF(40403, 15, "WARNING", M(24, 12, 46, 30), M(24, 12, 46, 45))])],
            rs_=0, cause_close=None)
_ad4 = eng._extract_open_segment_data(_s4)[1]
_ad4b = eng._extract_open_segment_data(_s4b)[1]
check([eng._alert_key(d) for d in _ad4] == ["CONTROLLER_FAULT|40403|15"] and _ad4b == [],
      f"2.3а: fault_end позже конца окна — живая, не позже — нет: {len(_ad4)}, {len(_ad4b)}")
check(len(eng._live_new_episodes([_s4], [], {"CONTROLLER_FAULT|40403|15"}, _T4)) == 1
      and eng._live_new_episodes([_s4b], [], {"CONTROLLER_FAULT|40403|15"}, _T4) == [],
      "2.3б: _live_new_episodes согласован — синтетика по фронту, живому на конец")
check("panel" in suppression_bans({}, _ad4) and "panel" not in suppression_bans({}, _ad4b),
      "2.3в: маска, снятая после конца сегмента, — запрет снятия «сигнал панели» (критика SHOULD 3)")

# ══ 2.4. П5: фронты панели, снятые внутри открытого сегмента ═══════════════
_N422 = "Ошибка данных об уровне охлаждающей жидкости"
_N197 = "Низкий уровень охлаждающей жидкости"
C422 = PF(40403, 15, "WARNING", M(24, 12, 46, 30), M(24, 12, 46, 56), "Уровень ОЖ: ошибка данных",
          {"panel_code": 422, "code_name": _N422})
C197 = PF(40401, 1, "WARNING", M(24, 12, 46, 46), M(24, 12, 46, 56), "Уровень ОЖ низкий",
          {"panel_code": 197, "code_name": _N197})
K422, K197 = "CONTROLLER_FAULT|40403|15", "CONTROLLER_FAULT|40401|1"


def _open24(t_to, dets=(C422, C197)):
    return SEG2([SUB2(M(24, 12, 41, 13), t_to, [d for d in dets if datetime.fromisoformat(d.t_detected) < t_to])],
                rs_=0, cause_close=None)


_o50, _o47 = _open24(M(24, 12, 46, 50)), _open24(M(24, 12, 47, 0))
check(eng._open_panel_front_candidates(_o50, M(24, 12, 46, 50), {}, set()) == []
      and K422 in {eng._alert_key(d) for d in eng._extract_open_segment_data(_o50)[1]},
      "2.4а: окно до 12:46:50 — кандидатов нет, 422 живая (П4)")
check({k for k, _t, _d in eng._open_panel_front_candidates(_o47, M(24, 12, 47), {}, set())} == {K422, K197},
      "2.4а: окно до 12:47:00 — кандидаты 422 и 197")
_e24 = _mk_engine2()
_db24 = EpDB()
_n1 = _with_db2(_db24, lambda: _e24._seed_open_panel_fronts(_o47, M(24, 12, 47), []))
_ins24 = sorted(_db24.ins, key=lambda k: k["t_open"])
check(_n1 == 2 and len(_db24.asked) == 1 and [(k["t_open"], k["t_close"], k["active_sec"]) for k in _ins24]
      == [(M(24, 12, 46, 30), M(24, 12, 46, 56), 26.0), (M(24, 12, 46, 46), M(24, 12, 46, 56), 10.0)]
      and all(k["scenario"] == "CONTROLLER_FAULT" and k["source"] == "panel"
              and k["close_reason"] == "condition_cleared" for k in _ins24),
      f"2.4б: цикл 1 — один existing_episode_onsets и 2 вставки: {len(_db24.asked)}, "
      f"{[(k['t_open'], k['active_sec']) for k in _ins24]}")
_with_db2(_db24, lambda: _e24._seed_open_panel_fronts(_o47, M(24, 12, 47, 30), []))
check(len(_db24.asked) == 1 and len(_db24.ins) == 2, "2.4в: цикл 2 — ни одного запроса")
_e24r = _mk_engine2()
_db24r = EpDB(onsets={(K422, M(24, 12, 46, 30)), (K197, M(24, 12, 46, 46))})
_with_db2(_db24r, lambda: _e24r._seed_open_panel_fronts(_o47, M(24, 12, 47), []))
_with_db2(_db24r, lambda: _e24r._seed_open_panel_fronts(_o47, M(24, 12, 47, 30), []))
check(len(_db24r.asked) == 1 and not _db24r.ins, "2.4г: рестарт — один запрос, 0 вставок, дальше без запросов")
_e24l = _mk_engine2(episodes={K422: {"id": 15017, "t_open": M(24, 12, 46, 30), "miss": 1}})
_db24l = EpDB()
_with_db2(_db24l, lambda: _e24l._seed_open_panel_fronts(_o47, M(24, 12, 47), []))
check([k["t_open"] for k in _db24l.ins] == [M(24, 12, 46, 46)],
      "2.4д: живой эпизод того же онсета (422) — пропуск, его закроет _process_episodes")
# Сводка открытого сегмента — обе строки (как полный отчёт)
_rows24 = [dict(k, id=i + 1, t_close=k["t_close"], open_values_json=k["open_values"], gate_suppressed=False)
           for i, k in enumerate(_ins24)]
_md24, _ = _with_db2(EpDB(eps=_rows24),
                     lambda: _mk_engine2()._build_summary_md_for([_o47], M(24, 12, 41, 13), M(24, 12, 47),
                                                                 live_keys=set()))
check(f"🟠 **{_N422} (код 422)** [панель]: 1 эпизод, воздействие за период 26 с" in (_md24 or "")
      and f"🟠 **{_N197} (код 197)** [панель]: 1 эпизод, воздействие за период 10 с" in (_md24 or ""),
      f"2.4е: Сводка открытого — «(код 422)» 26 с и «(код 197)» 10 с: "
      f"{[x for x in (_md24 or '').splitlines() if 'код' in x]}")
# Сбой вставки — без исключения, без флагов цикла; фронт — на повтор
_e24f = _mk_engine2()
_db24f = EpDB(fail_insert=True)
_nf = _with_db2(_db24f, lambda: _e24f._seed_open_panel_fronts(_o47, M(24, 12, 47), []))
check(_nf == 0 and not _e24f._open_failed and not _e24f._close_failed and not _e24f._panel_seeded,
      "2.4ж: сбой вставки не ставит _open_failed, фронты остаются кандидатами")
_i_pe = _usrc.index("await self._process_episodes(")
_i_ev = _usrc.index("await self._seed_open_events(")
_i_pf = _usrc.index("await self._seed_open_panel_fronts(open_seg, ts_to_utc, gaps)")
_i_en = _usrc.index("await _enrich_open_seg_detections(")
_tail = _usrc[_i_pf:_i_en]
check(_i_pe < _i_ev < _i_pf < _i_en and "посев снятых фронтов панели" in _tail and "_open_failed" not in _tail,
      "2.4з: порядок — после _process_episodes и событий, до счётчиков; сбой — WARNING без _open_failed")
# SHOULD 1: цикл после закрытия дебаунсом — 0 запросов
_e24d = _mk_engine2(episodes={K422: {"id": 15017, "severity": "WARNING", "miss": 0, "first_miss_ts": None,
                                     "t_open": M(24, 12, 46, 30), "exact": False, "until": None}})
_e24d._episode_accrual_ts = M(24, 12, 46, 50)
_db24d = EpDB()
for _k in range(3):
    _with_db2(_db24d, lambda: _e24d._process_episodes(
        [], S(M(24, 12, 47), 30 * _k), [], cleared_at=eng._panel_cleared_at([_o47])))
_o47_422 = _open24(M(24, 12, 48), dets=(C422,))
_with_db2(_db24d, lambda: _e24d._seed_open_panel_fronts(_o47_422, M(24, 12, 48), []))
check(any(c[0] == "close" and c[3] == "condition_cleared" and c[2] == M(24, 12, 46, 56) for c in _db24d.calls)
      and not _db24d.asked and not _db24d.ins,
      f"2.4и: закрыт дебаунсом по fault_end — фронт отмечен, посев без запросов (SHOULD 1): {_db24d.asked}")

# ══ 2.5. П6: имя Общей аварии по коду и «код сменился» ═════════════════════
_EN6 = [rs(0, M(24, 12, 38, 59), M(24, 12, 41, 5)), rs(3, M(24, 12, 41, 5), M(24, 12, 41, 13)),
        rs(0, M(24, 12, 41, 13), None),
        en(40012, 1453, M(13, 15, 1), M(24, 12, 38, 59)), en(40012, 1248, M(24, 12, 38, 59), None),
        en(40013, 4, M(13, 15, 1), None)]
_FP6 = [{"addr": 40408, "bit": 11, "severity": None, "fault_start": M(13, 15, 1), "fault_end": None,
         "fault_name_ru": "Общая авария", "fault_name": "Common Alarm"},
        {"addr": 40406, "bit": 2, "severity": "warning", "fault_start": M(24, 12, 41, 50), "fault_end": None,
         "fault_name_ru": "Предупреждение двигателя", "fault_name": "Engine Warning"}]
_H6 = [h(RPM, M(24, 12, 41, 4), 0.0), h(RPM, M(24, 12, 42, 7), 0.0)]
_s6 = segment(_EN6, _H6, _FP6, [], CFG3, "SN", "pcc", 1, "E", M(24, 12, 38, 59), M(24, 12, 41, 40))


def _ca_det(s):
    return next((d for sub in s.subsegments for d in sub.detections
                 if d.scenario == "CONTROLLER_FAULT" and d.values.get("bit") == 11), None)


_ca67, _ca68 = (_ca_det(_s6[0]), _ca_det(_s6[1])) if len(_s6) >= 2 else (None, None)
_N1248, _N1453 = "Предупреждение двигателя — общее", "Отказ отключения автомата генераторной установки"
check(_ca67 is not None and _ca67.values.get("last_fault_code") == 1248 and _ca67.values.get("code_name") == _N1248
      and _ca67.values.get("last_fault_code_since") == M(24, 12, 38, 59).isoformat()
      and _ca67.values.get("fault_name") == "Общая авария"
      and _ca67.trigger == f"Общая авария при несброшенном коде 1248 «{_N1248}»",
      f"2.5а: детекция #36067 — код 1248, имя, начало кода: {_ca67 and _ca67.values} / {_ca67 and _ca67.trigger}")
check(_ca68 is not None and _ca68.values.get("last_fault_code_since") == M(24, 12, 38, 59).isoformat(),
      f"2.5а: #36068 через segment() — начало кода настоящее, без обрезки по окну (M4): "
      f"{_ca68 and _ca68.values.get('last_fault_code_since')}")
check(labels.panel_fault_name(_ca67.values if _ca67 else {}) == f"Общая авария: {_N1248} (код 1248)"
      and labels.panel_fault_name({"fault_name": "CommonAlarm (Общая авария)", "last_fault_code": 1453},
                                  CFG3.fault_code_name) == f"Общая авария: {_N1453} (код 1453)"
      and labels.panel_fault_name({"fault_name": "CommonAlarm (Общая авария)", "last_fault_code": 1453})
      == "Общая авария (код 1453)"
      and labels.panel_fault_name({"fault_name": "CommonAlarm (Общая авария)"}) == "CommonAlarm (Общая авария)",
      "2.5б: имя — новая запись и старая (14901) при чтении; без кода — как раньше")
# Смена кода 1453 → 1248 под тем же фронтом: тем же UPDATE, память
_ca67d = _ca67.to_dict() if _ca67 else {}
_e25 = _mk_engine2(episodes={CA_KEY: {"id": 14901, "severity": "CAUTION", "miss": 0, "first_miss_ts": None,
                                      "t_open": M(13, 15, 1), "exact": False, "until": None,
                                      "code": 1453, "code_changes": []}})
_e25._episode_accrual_ts = M(24, 12, 40, 30)
_db25 = EpDB()
_with_db2(_db25, lambda: _e25._process_episodes([_ca67d], M(24, 12, 41, 0), []))
_up25 = [c for c in _db25.calls if c[0] == "update"]
_vm25 = (_up25[0][2].get("values_merge") or {}) if _up25 else {}
check(len(_up25) == 1 and _vm25.get("last_fault_code") == 1248 and _vm25.get("code_name") == _N1248
      and _vm25.get("code_changes") == [{"from": 1453, "to": 1248, "at": M(24, 12, 38, 59).isoformat()}]
      and _e25._episodes[CA_KEY]["code"] == 1248 and len(_db25.calls) == 1,
      f"2.5в: смена кода — values_merge в том же update_episode, новый эпизод не заводится: {_db25.calls}")
_with_db2(_db25, lambda: _e25._process_episodes([_ca67d], M(24, 12, 41, 30), []))
check("values_merge" not in [c for c in _db25.calls if c[0] == "update"][-1][2],
      "2.5в: тот же код — без слияния")
check(eng._code_change({"code": 1453, "code_changes": [{"from": i, "to": i + 1} for i in range(10)]},
                       {"last_fault_code": 1248})["code_changes"][-1]["to"] == 1248
      and len(eng._code_change({"code": 1453, "code_changes": [{"from": i, "to": i + 1} for i in range(10)]},
                               {"last_fault_code": 1248})["code_changes"]) == 10
      and eng._code_change({"code": 1453}, {"last_fault_code": 0}) is None
      and eng._code_change({"code": None}, {"last_fault_code": 1248}) is None,
      "2.5г: не больше 10 записей; нули (мигающий 40012) и неизвестный прежний код — не смена")
# Переоткрытие с другим кодом (громкий снят, Общая авария с кодом осталась)
_ctx25 = {"ca_key": CA_KEY, "ca_active": True, "loud": None}
_e25r = _mk_engine2()
_e25r._episode_accrual_ts = M(24, 13, 30)
_db25r = EpDB(prev={"id": 14901, "t_close": M(24, 12, 42, 15), "severity": "CAUTION",
                    "last_fault_code": "1453", "code_changes": None})
_with_db2(_db25r, lambda: _e25r._process_episodes([_ca67d], M(24, 13, 31), [], panel_ctx=_ctx25))
_rp25 = [c for c in _db25r.calls if c[0] == "reopen"]
_rvm = (_rp25[0][2].get("values_merge") or {}) if _rp25 else {}
check(_rp25 and _rvm.get("last_fault_code") == 1248 and "superseded_by" in _rvm and _rvm["superseded_by"] is None
      and _rvm.get("code_changes") == [{"from": 1453, "to": 1248, "at": M(24, 12, 38, 59).isoformat()}]
      and _e25r._episodes[CA_KEY]["code"] == 1248 and (CA_KEY, M(13, 15, 1)) in _e25r._panel_seeded,
      f"2.5д: переоткрытие с другим кодом — «код сменился» и снятие отметки «перекрыта» тем же UPDATE: {_rp25}")
# Пометка — только в сегменте, где код сменился (#36067 да, #36068 нет)
_ep14901 = {"id": 14901, "scenario": "CONTROLLER_FAULT", "source": "panel", "severity": "CAUTION", "addr": 40408,
            "bit": 11, "t_open": M(13, 15, 1), "t_close": None, "active_sec": 9e5, "blind_sec": 0.0,
            "gate_suppressed": False,
            "open_values_json": {"fault_name": "CommonAlarm (Общая авария)", "last_fault_code": 1453}}
_p67 = build_summary_parts([_s6[0]], [_ep14901], tz=MSK, trip_roles=[], t_from=M(24, 12, 38, 59),
                           t_to=M(24, 12, 41, 5), code_name=CFG3.fault_code_name)
_l67 = _line(render_summary_md(_p67), "Общая авария")
check(_l67.startswith(f"- 🟡 **Общая авария: {_N1248} (код 1248)** [панель]: 1 эпизод, воздействие за период 2 мин 6 с")
      and "— код сменился 1453 → 1248" in _l67,
      f"2.5е: #36067 — имя по коду и «код сменился 1453 → 1248»: {_l67}")
_p68 = build_summary_parts([_s6[1]], [dict(_ep14901, open_values_json=dict(
    _ep14901["open_values_json"], last_fault_code=1248,
    code_changes=[{"from": 1453, "to": 1248, "at": M(24, 12, 38, 59).isoformat()}]))],
    tz=MSK, trip_roles=[], t_from=M(24, 12, 41, 5), t_to=M(24, 12, 41, 40), code_name=CFG3.fault_code_name)
_l68 = _line(render_summary_md(_p68), "Общая авария")
check(f"**Общая авария: {_N1248} (код 1248)**" in _l68 and "код сменился" not in _l68,
      f"2.5е: #36068 — то же имя без пометки: {_l68}")
_p67m = build_summary_parts([_s6[0]], [dict(_ep14901, open_values_json=_p68 and dict(
    _ep14901["open_values_json"], last_fault_code=1248,
    code_changes=[{"from": 1453, "to": 1248, "at": M(24, 12, 38, 59).isoformat()}]))],
    tz=MSK, trip_roles=[], t_from=M(24, 12, 38, 59), t_to=M(24, 12, 41, 5), code_name=CFG3.fault_code_name)
check("— код сменился 1453 → 1248" in _line(render_summary_md(_p67m), "Общая авария"),
      "2.5е: после слияния в БД — прежний код из code_changes")

# ══ 2.6. П7: «перекрыта предупреждением панели» и «Висит также» ════════════
_loud = PF(40406, 2, "WARNING", M(24, 12, 41, 50), None, "Предупреждение двигателя").to_dict()
_ctx = eng._supersede_ctx(CFG3, _FP6, [_loud], M(24, 12, 42, 15))
check(_ctx == {"ca_key": CA_KEY, "ca_active": True,
               "loud": {"name": "Предупреждение двигателя", "at": M(24, 12, 41, 50).isoformat(),
                        "severity": "WARNING"}},
      f"2.6а: контекст перекрытия из загруженного: {_ctx}")
_mem26 = {CA_KEY: {"id": 14901, "severity": "CAUTION", "miss": 2, "first_miss_ts": M(24, 12, 42, 15),
                   "t_open": M(13, 15, 1), "exact": False, "until": None, "code": 1248}}
_e26 = _mk_engine2(episodes=copy.deepcopy(_mem26))
_e26._episode_accrual_ts = M(24, 12, 42, 30)
_db26 = EpDB()
_with_db2(_db26, lambda: _e26._process_episodes([_loud], M(24, 12, 43), [], panel_ctx=_ctx))
_cl26 = [c for c in _db26.calls if c[0] == "close"]
check(_cl26 and _cl26[0][1] == 14901 and _cl26[0][2] == M(24, 12, 42, 15) and _cl26[0][3] == "superseded"
      and _cl26[0][4].get("values_merge") == {"superseded_by": _ctx["loud"]} and CA_KEY not in _e26._episodes,
      f"2.6б: Общая авария закрыта «superseded» при громком бите и висящем бите: {_cl26}")
_e26b = _mk_engine2(episodes=copy.deepcopy(_mem26))
_e26b._episode_accrual_ts = M(24, 12, 42, 30)
_db26b = EpDB()
_with_db2(_db26b, lambda: _e26b._process_episodes([_loud], M(24, 12, 43), [],
                                                   panel_ctx=dict(_ctx, ca_active=False)))
_cl26b = [c for c in _db26b.calls if c[0] == "close"]
check(_cl26b and _cl26b[0][3] == "condition_cleared" and _cl26b[0][4] == {},
      f"2.6в: бит Общей аварии снят — «condition_cleared», как раньше: {_cl26b}")
# Сводка: суффикс «перекрыта …»
_ev27 = [{"type": "FAULT", "t": M(13, 15, 1).isoformat(), "fault_end": None, "addr": 40408, "bit": 11,
          "severity": None},
         {"type": "FAULT", "t": M(24, 12, 41, 50).isoformat(), "fault_end": None, "addr": 40406, "bit": 2,
          "severity": "warning"}]
_lfc = {"LAST_FAULT_CODE": {"value_end": 1248.0, "kind": "state"}}
_s26 = SEG2([SUB2(M(24, 12, 41, 13), M(24, 12, 41, 50),
                  [_ca_det(_s6[1]) or AD("X", M(24, 12, 41, 13))]),
             SUB2(M(24, 12, 41, 50), M(24, 12, 47),
                  [PF(40406, 2, "WARNING", M(24, 12, 41, 50), None, "Предупреждение двигателя")], _lfc)],
            rs_=0, events=_ev27)
_ep_sup = dict(_ep14901, t_close=M(24, 12, 42, 15), close_reason="superseded",
               open_values_json={"fault_name": "CommonAlarm (Общая авария)", "last_fault_code": 1248,
                                 "superseded_by": _ctx["loud"]})
_ep_loud = {"id": 15015, "scenario": "CONTROLLER_FAULT", "source": "panel", "severity": "WARNING", "addr": 40406,
            "bit": 2, "t_open": M(24, 12, 41, 50), "t_close": None, "active_sec": 310.0, "blind_sec": 0.0,
            "gate_suppressed": False, "open_values_json": {"fault_name": "Предупреждение двигателя"}}
_p26 = build_summary_parts([_s26], [_ep_sup, _ep_loud], tz=MSK, trip_roles=[], t_from=M(24, 12, 41, 13),
                           t_to=M(24, 12, 47), code_name=CFG3.fault_code_name, common_alarm=(40408, 11, 15.0))
_md26 = render_summary_md(_p26)
check("— перекрыта предупреждением панели «Предупреждение двигателя» с 12:41:50, Общая авария не снята"
      in _line(_md26, "Общая авария"),
      f"2.6г: суффикс «перекрыта предупреждением панели … с 12:41:50»: {_line(_md26, 'Общая авария')}")
_sh = dict(_ctx["loud"], severity="SHUTDOWN", name="Аварийный останов")
_p26s = build_summary_parts([_s26], [dict(_ep_sup, open_values_json=dict(_ep_sup["open_values_json"],
                                                                          superseded_by=_sh)), _ep_loud],
                            tz=MSK, trip_roles=[], t_from=M(24, 12, 41, 13), t_to=M(24, 12, 47))
check("— перекрыта аварией панели «Аварийный останов» с 12:41:50" in _line(render_summary_md(_p26s), "Общая авария"),
      "2.6г: громкий — авария: «перекрыта аварией панели» (SHOULD 7)")
check(_p26.get("common_alarm_hold") is None, "2.6д: строка «Висит также» не дублирует перекрытую в этом окне")
# «Висит также» — в следующем сегменте, пока бит висит под громким
_s27 = SEG2([SUB2(M(24, 13, 0), M(24, 13, 10),
                  [PF(40406, 2, "WARNING", M(24, 12, 41, 50), None, "Предупреждение двигателя")], _lfc)],
            rs_=0, events=_ev27)
_p27 = build_summary_parts([_s27], [_ep_loud], tz=MSK, trip_roles=[], t_from=M(24, 13, 0), t_to=M(24, 13, 10),
                           code_name=CFG3.fault_code_name, common_alarm=(40408, 11, 15.0))
_p27n = build_summary_parts([_s27], [_ep_loud], tz=MSK, trip_roles=[], t_from=M(24, 13, 0), t_to=M(24, 13, 10),
                            code_name=CFG3.fault_code_name)
_md27 = render_summary_md(_p27)
# Закрытый сегмент — код на конец периода, не «сейчас» (ревью v5.1.25, Q10; открытый — 4.6)
check((_p27.get("common_alarm_hold") or {}).get("text")
      == f"Висит также Общая авария с 13.10 15:01 (на конец периода на панели код 1248 «{_N1248}»)"
      and f"- ℹ️ Висит также Общая авария с 13.10 15:01 (на конец периода на панели код 1248 «{_N1248}»)"
      in _md27
      and _p27["verdict"] == _p27n["verdict"] and _p27["end_level"] == _p27n["end_level"],
      f"2.6е: «Висит также» — вне счёта замечаний и вердикта: {_p27.get('common_alarm_hold')} / {_p27['verdict']}")
# Критика M8: Общая авария встала вместе с громким (16.10) — строки нет
_ev28 = [dict(_ev27[0], t=M(24, 12, 41, 45).isoformat()), _ev27[1]]
_s28 = SEG2([SUB2(M(24, 13, 0), M(24, 13, 10),
                  [PF(40406, 2, "WARNING", M(24, 12, 41, 50), None, "Предупреждение двигателя")], _lfc)],
            rs_=0, events=_ev28)
check(build_summary_parts([_s28], [_ep_loud], tz=MSK, trip_roles=[], t_from=M(24, 13, 0), t_to=M(24, 13, 10),
                          common_alarm=(40408, 11, 15.0)).get("common_alarm_hold") is None,
      "2.6ж: фронт Общей аварии в пределах 15 с от громкого, своего эпизода нет — строки нет (M8)")
check(build_summary_parts([_s28], [_ep_loud, dict(_ep14901, t_open=M(24, 12, 41, 45))], tz=MSK, trip_roles=[],
                          t_from=M(24, 13, 0), t_to=M(24, 13, 10),
                          common_alarm=(40408, 11, 15.0)).get("common_alarm_hold") is not None,
      "2.6ж: у фронта свой открытый эпизод — строка есть")
_e28 = _mk_engine2()
_db28 = EpDB(eps=[_ep_loud])
_md28, _ = _with_db2(_db28, lambda: _e28._build_summary_md_for([_s27], M(24, 13, 0), M(24, 13, 10),
                                                                live_keys={"CONTROLLER_FAULT|40406|2"}))
check("ℹ️ Висит также Общая авария" in (_md28 or ""), "2.6з: движок передаёт бит Общей аварии из KB в сводку")

# ══ 2.7. П8: перерыв в журнале сервиса — от последних данных ═══════════════
import db.analytics as _dba  # noqa: E402
import online.manager as _mgr  # noqa: E402


class _Info(logging.Handler):
    def __init__(self):
        super().__init__(logging.INFO)
        self.msgs: list[str] = []

    def emit(self, record):
        self.msgs.append(record.getMessage())


_inf = _Info()
_lm = logging.getLogger("online.manager")
_lm.addHandler(_inf)
_lm_level = _lm.level
_lm.setLevel(logging.INFO)
_NOW = {"t": M(24, 7, 48, 48)}


class _FakeDT(datetime):
    @classmethod
    def now(cls, tz=None):
        return _NOW["t"]


async def _stale90(*a, **k):
    return "90"


async def _none_seg(*a, **k):
    return None


_o_dt, _o_gas, _o_gos = _mgr.datetime, _dba.get_app_setting, _odb.get_open_segment
_mgr.datetime, _dba.get_app_setting, _odb.get_open_segment = _FakeDT, _stale90, _none_seg
try:
    _m = _mgr.OnlineManager.__new__(_mgr.OnlineManager)
    _eng27 = type("E", (), {"last_data_ts": M(24, 5, 48, 15), "router_sn": "1126246373",
                            "equip_type": "pcc", "panel_id": 1})()
    _m._engines = {"K": _eng27}
    _m._stale_since, _m._stale_last_data = {}, {}
    _m._engine_progress_text = lambda *a, **k: "—"
    asyncio.run(_m._tick_status_lines())
    _eng27.last_data_ts = M(24, 11, 48, 9)      # поздние строки медленной группы (12:38:28)
    _NOW["t"] = M(24, 12, 38, 30)
    asyncio.run(_m._tick_status_lines())
    _eng27.last_data_ts = M(24, 12, 39, 7)
    _NOW["t"] = M(24, 12, 39, 44)
    asyncio.run(_m._tick_status_lines())
finally:
    _mgr.datetime, _dba.get_app_setting, _odb.get_open_segment = _o_dt, _o_gas, _o_gos
    _lm.removeHandler(_inf)
    _lm.setLevel(_lm_level)
check(any("данные снова свежие (перерыв 6 ч 51 мин)" in x for x in _inf.msgs) and not _m._stale_last_data,
      f"2.7: перерыв — от последних данных 05:48:15, а не от входа 07:48:48: "
      f"{[x for x in _inf.msgs if 'свежие' in x]}")

# ══ 2.8. П10: заглушка без связи — последние данные и оценка по наработке ══
_e28n = _mk_engine2()
_e28n.last_data_ts = M(24, 11, 48, 9)        # поднят поздними строками (критика M1)
_g65 = [{"gap_start": M(24, 5, 48, 15), "gap_end": M(24, 11, 48, 9)},
        {"gap_start": M(24, 11, 48, 9), "gap_end": M(24, 12, 38, 58)}]
check(_e28n._no_data_last_before(M(24, 9, 0), _g65) == M(24, 5, 48, 15)
      and _e28n._no_data_last_before(M(24, 12, 0), _g65) == M(24, 11, 48, 9)
      and _e28n._no_data_last_before(M(24, 9, 0), [], [h(HB, M(24, 8, 59), 1.0)]) == M(24, 8, 59)
      and _e28n._no_data_last_before(M(24, 9, 0), []) is None,
      "2.8а: последние данные до обрыва — начало разрыва на начале сегмента (05:48:15), не last_data_ts")
from analytics import source as _srcm  # noqa: E402

_EH, _ST = CFG3.role_to_addr("ENGINE_HOURS"), CFG3.role_to_addr("START_ATTEMPTS_TOTAL")
_hist65 = [h(_EH, M(24, 12, 38, 58), 56008080.0), h(_ST, M(24, 12, 38, 58), 412.0), h(RPM, M(24, 12, 38, 58), 0.0)]
_asked10: list = []


async def _lvb(sn, et, pid, addrs, ts_before, max_age):
    _asked10.append((list(addrs), ts_before, max_age))
    return {_EH: {"ts": M(24, 5, 47, 11), "value": 55985400.0}, _ST: {"ts": M(24, 5, 40), "value": 412.0}}


async def _lvb_fail(*a, **k):
    raise RuntimeError("нет связи с источником")


_o_lvb = _srcm.get_last_values_before
_srcm.get_last_values_before = _lvb
try:
    # #36065: на обрыве 05:48 работал — режим сегмента без связи 3 (ревью Q1)
    _est10 = asyncio.run(_e28n._no_data_estimate(M(24, 9, 0), M(24, 12, 38, 59), _hist65, M(24, 5, 48, 15),
                                                 run_state=3))
    _est10s = asyncio.run(_e28n._no_data_estimate(M(24, 9, 0), M(24, 12, 38, 59),
                                                  _hist65[:1] + [h(_ST, M(24, 12, 38, 58), 414.0)],
                                                  M(24, 5, 48, 15), run_state=3))
    _n_ask = len(_asked10)
    _est10n = asyncio.run(_e28n._no_data_estimate(M(24, 9, 0), M(24, 12, 38, 59), [h(RPM, M(24, 12, 38, 58), 0.0)],
                                                  M(24, 5, 48, 15)))
    _srcm.get_last_values_before = _lvb_fail
    LOG.records.clear()
    _est10f = asyncio.run(_e28n._no_data_estimate(M(24, 9, 0), M(24, 12, 38, 59), _hist65, M(24, 5, 48, 15)))
finally:
    _srcm.get_last_values_before = _o_lvb
check(_est10 and _est10["kind"] == "until" and _est10["end_from"] == M(24, 12, 5, 11).isoformat()
      and _est10["worked_sec"] == 22680.0 and _est10["starts_changed"] is False
      and _asked10[0][0] == [_EH, _ST] and _asked10[0][1] == S(M(24, 5, 48, 15), 1)
      and _asked10[0][2] == 172800.0,
      f"2.8б: #36065 — один запрос до обрыва (наработка и попытки пуска), «до ~12:05–12:08»: {_est10}, {_asked10[:1]}")
check("до ~12:05–12:08" in (labels.no_data_hours_text(_est10, MSK) or "")
      and "6 ч 18 мин" in (labels.no_data_hours_text(_est10, MSK) or ""),
      f"2.8б: текст: {labels.no_data_hours_text(_est10, MSK)}")
check(_est10s and _est10s["kind"] == "starts", "2.8в: счётчик попыток пуска сменился — «были остановы и пуски»")
check(_est10n is None and len(_asked10) == _n_ask, "2.8г: нет H1 в history — оценки нет, запроса нет")
check(_est10f is None and logged("оценка работы за обрыв по счётчику наработки не получена"),
      "2.8д: сбой запроса — строка без оценки, WARNING")
check("last_data_ts=self.last_data_ts" not in _csrc and "last_data_ts=self.last_data_ts" not in _usrc
      and "last_data_ts=_nd_last, run_estimate=_nd_est" in _usrc and "last_data_ts=_nd_last," in _csrc,
      "2.8е: обе ветки заглушки — последние данные по разрывам (M1), оценка — на пути смены режима")
_rmd10, _smd10 = build_no_data_report("SN", "pcc", 1, M(24, 9, 0), M(24, 12, 38, 59), tz=MSK,
                                      last_data_ts=_e28n._no_data_last_before(M(24, 9, 0), _g65),
                                      run_estimate=_est10)
check("Последние данные до обрыва: 2026-10-24 05:48:15" in _smd10 and "до ~12:05–12:08" in _smd10,
      f"2.8е: заглушка #36065: {_smd10}")

# ══ 2.9. Сторож v5.1.24: новые шаги не ставят флаги цикла ══════════════════
_e29 = _mk_engine2()
_with_db2(EpDB(fail_insert=True), lambda: _e29._seed_open_panel_fronts(_o47, M(24, 12, 47), []))
_srcm.get_last_values_before = _lvb_fail
try:
    asyncio.run(_e29._no_data_estimate(M(24, 9, 0), M(24, 12, 38, 59), _hist65, M(24, 5, 48, 15)))
finally:
    _srcm.get_last_values_before = _o_lvb
check(not _e29._open_failed and not _e29._close_failed and _e29.last_cycle_error is None,
      "2.9а: сбой посева и оценки — без _open_failed / _close_failed / ошибки цикла")
_ssrc = inspect.getsource(eng.OnlinePollEngine._seed_open_panel_fronts)
_nsrc = inspect.getsource(eng.OnlinePollEngine._no_data_estimate)
check("_src." not in _ssrc and "online_db." in _ssrc and "_src.get_last_values_before" in _nsrc
      and "remote.connection" in inspect.getsource(_srcm.get_last_values_before)
      and "beat_mono" not in _ssrc + _nsrc and "live_ok_at" not in _ssrc + _nsrc,
      "2.9б: посев — только БД аналитики; удалённый запрос оценки — через remote (продлевает пульс)")

# ══ 2.10. В-25.4: журнал тревог — служебный ════════════════════════════════
check(all("служебный" in (f.__doc__ or "") and "alarm_episodes" in (f.__doc__ or "")
          and "segment_id не поддерживается" in (f.__doc__ or "")
          for f in (_odb.insert_alert_events, _odb.get_active_alerts)),
      "2.10: docstring insert_alert_events и get_active_alerts — журнал служебный, источник — alarm_episodes")

# ══════════════════════ Этап 3 — значок акта, сервер (§4.1 плана) ═══════════
import online.incident_gate as _ig  # noqa: E402

# ══ 3.1. analysis_badge (решение В-25.2) ═══════════════════════════════════
_NOW3 = M(24, 18, 0)
_ST16 = M(16, 9, 26, 23)                         # #35931 ДГУ №1 16.10
_ST14 = datetime(2026, 9, 14, 7, 34, 53, tzinfo=U)   # #35282 — без заказа
check(_ig.BADGE_GIVE_UP_SEC == _ig.INCIDENT_MAX_ORDERS * _ig.INCIDENT_DEADLINE_SEC == 72 * 3600
      and not re.search(r"\b(72|259200|86400)\b", inspect.getsource(_ig.analysis_badge).split('"""')[2]),
      "3.1а: срок «без разбора» — 3 заказа × 24 ч из констант, не числом")
_b = _ig.analysis_badge
check(_b({"analysed": True, "ordered": 1, "terminal": None}, _ST16, _NOW3) == "ready"
      and _b({"analysed": True, "ordered": 0, "terminal": None}, _ST14, _NOW3) == "ready"
      and _b({"analysed": True, "ordered": 2, "terminal": "failed"}, _ST16, _NOW3) == "ready",
      "3.1б: ready — разбор есть (хоть на соседней строке, хоть после отметки «failed»)")
check(_b({"analysed": False, "ordered": 1, "terminal": None}, S(_NOW3, -3600), _NOW3) == "pending"
      and _b({"analysed": False, "ordered": 0, "terminal": None}, S(_NOW3, -71.9 * 3600), _NOW3) == "pending"
      and _b({"analysed": False, "ordered": 3, "terminal": None}, None, _NOW3) == "pending",
      "3.1в: pending — меньше 72 ч и без терминальной отметки (и без момента останова)")
check(all(_b({"analysed": False, "ordered": 1, "terminal": t}, S(_NOW3, -600), _NOW3) == "failed"
          for t in _ig.INC_TERMINAL)
      and _b({"analysed": False, "ordered": 2, "terminal": None}, S(_NOW3, -72 * 3600), _NOW3) == "failed"
      and _b({"analysed": False, "ordered": 0, "terminal": None}, _ST14, _NOW3) == "failed"
      and _b({"analysed": False, "ordered": 0, "terminal": "ordered"}, S(_NOW3, -60), _NOW3) == "pending",
      "3.1г: failed — терминальная отметка, 72 ч от аварии, старый акт без заказа; «ordered» — не терминальная")
check(_b(None, _ST16, _NOW3) == "none" and _b("x", _ST16, _NOW3) == "none",
      "3.1д: без состояния (нет акта) — none")
_r = _ig.analysis_failed_reason
check(_r({"analysed": False, "ordered": 0, "terminal": None}) == "not_ordered"
      and _r({"analysed": False, "ordered": 2, "terminal": None}) == "no_answer"
      and _r({"analysed": False, "ordered": 3, "terminal": "failed"}) == "no_answer"
      and _r({"analysed": False, "ordered": 1, "terminal": "removed"}) == "removed"
      and _r({"analysed": False, "ordered": 1, "terminal": "save_failed"}) == "save_failed"
      and _r({"analysed": True, "ordered": 1, "terminal": None}) is None and _r(None) is None,
      "3.1е: причина failed — «не заказывался» у акта без заказа, а не «совет не ответил» (SHOULD 6)")
_a = _ig.act_stop_ts
check(_a("2026-10-16T06:26:23+00:00", M(1, 0, 0)) == _ST16
      and _a("2026-10-16T09:26:23+03:00", None) == _ST16
      and _a(None, _ST16) == _ST16 and _a("мусор", _ST16) == _ST16
      and _a(None, "2026-10-16T06:26:23+00:00") == _ST16
      and _a(None, datetime(2026, 10, 16, 6, 26, 23)) == _ST16 and _a(None, None) is None
      and _ig.incident_hash(_a("2026-10-16T09:26:23+03:00", None)) == "incident:2026-10-16T06:26:23+00:00",
      "3.1ж: момент останова акта — stop_ts (в UTC), иначе начало строки; ключ — как у заказа")

# ══ 3.2. get_segments_for_calendar: состояние разбора одним запросом ═══════


class _FC3:
    """Календарь: первый fetch — строки сегментов, следующие — состояние разбора."""

    def __init__(self, seg_rows, states=(), fail_second=False):
        self.seg_rows, self.states, self.fail_second = seg_rows, list(states), fail_second
        self.q: list[tuple] = []
        self.closed = 0

    async def fetch(self, sql, *args):
        self.q.append((sql, args))
        if len(self.q) == 1:
            return [dict(r) for r in self.seg_rows]
        if self.fail_second:
            raise RuntimeError("сбой второго запроса")
        return [dict(r) for r in self.states]

    async def close(self):
        self.closed += 1


def _cal_row(i, t, inc=False, stop=None):
    return {"id": i, "t_start": t, "t_end": S(t, 60), "run_state": 0, "has_incident": inc,
            "incident_stop_ts": stop}


async def _cal(fc, **kw):
    async def _c():
        return fc
    _odb._connect = _c
    try:
        return await _odb.get_segments_for_calendar("SN", "pcc", 1, ts_from=M(1, 0, 0), ts_to=M(31, 0, 0), **kw)
    finally:
        _odb._connect = _orig_connect


_rows_plain = [_cal_row(1, M(16, 8, 0)), _cal_row(2, M(16, 9, 0))]
_fc3a = _FC3(_rows_plain)
_out3a = asyncio.run(_cal(_fc3a, incident_state=True))
check(len(_fc3a.q) == 1 and all("_incident_state" not in r for r in _out3a) and _fc3a.closed == 1,
      f"3.2а: без актов — один запрос: {len(_fc3a.q)}")
_sql3 = _fc3a.q[0][0]
check("CASE WHEN incident_json IS NOT NULL" in _sql3 and "THEN incident_json->>'stop_ts' END AS incident_stop_ts" in _sql3
      and "(incident_json IS NOT NULL) AS has_incident" in _sql3,
      "3.2б: момент останова — в той же выборке, JSONB разворачивается только у строк с актом")
_rows_inc = [_cal_row(35931, _ST16, True, "2026-10-16T06:26:23+00:00"), _cal_row(35932, M(16, 9, 27)),
             _cal_row(35282, _ST14, True, None)]
_states3 = [{"id": 35931, "analysed": True, "ordered": 1, "terminal": None},
            {"id": 35282, "analysed": False, "ordered": 0, "terminal": None}]
_fc3b = _FC3(_rows_inc, _states3)
_out3b = asyncio.run(_cal(_fc3b, incident_state=True))
_sq2, _a2 = _fc3b.q[1] if len(_fc3b.q) > 1 else ("", ())
check(len(_fc3b.q) == 2 and _fc3b.closed == 1, f"3.2в: с актами — два запроса на одном соединении: {len(_fc3b.q)}")
check("unnest($4::bigint[], $5::timestamptz[], $6::text[])" in _sq2
      and "a.router_sn = $1 AND a.equip_type = $2 AND a.panel_id = $3" in _sq2
      and "q.stop_ts - interval '2 days'" in _sq2 and "q.stop_ts + interval '2 days'" in _sq2
      and "jsonb_build_object('fault_hash', q.fh)" in _sq2
      and "e->>'kind' = 'incident' AND e->>'fault_hash' = s.fh" in _sq2
      and "('failed', 'removed', 'save_failed')" in _sq2,
      "3.2г: второй запрос — по машине в окне ±2 сут (разбор на соседней строке), отметки и терминальные решения "
      "как у get_incident_analysis_state")
check(set(_ig.INC_TERMINAL) == {"failed", "removed", "save_failed"},
      "3.2г: терминальные решения в SQL совпадают с INC_TERMINAL")
check(_a2[:3] == ("SN", "pcc", 1) and list(_a2[3]) == [35931, 35282] and list(_a2[4]) == [_ST16, _ST14]
      and list(_a2[5]) == [_ig.incident_hash(_ST16), _ig.incident_hash(_ST14)]
      and _a2[5][1] == "incident:2026-09-14T07:34:53+00:00",
      f"3.2д: входы — id, момент останова (stop_ts, иначе начало строки), ключ incident_hash: {_a2[3:]}")
_st3 = {r["id"]: r.get("_incident_state") for r in _out3b}
check(_st3 == {35931: {"analysed": True, "ordered": 1, "terminal": None}, 35932: None,
               35282: {"analysed": False, "ordered": 0, "terminal": None}},
      f"3.2е: состояние легло в строки с актом: {_st3}")
_fc3c = _FC3(_rows_inc, _states3)
asyncio.run(_cal(_fc3c))
check(len(_fc3c.q) == 1, f"3.2ж: без incident_state (движок, HTML-календарь) — один запрос и при актах: {len(_fc3c.q)}")
_esrc = inspect.getsource(eng)
_rsrc3 = (ROOT / "web" / "routes.py").read_text(encoding="utf-8")
check("get_segments_for_calendar(" in _esrc and "incident_state" not in _esrc
      and "odb.get_segments_for_calendar(router_sn, equip_type, panel_id)\n" in _rsrc3,
      "3.2ж: движок (контекст гейта) и HTML-календарь второй запрос не делают")
LOG.records.clear()
_fc3d = _FC3(_rows_inc, fail_second=True)
_out3d = asyncio.run(_cal(_fc3d, incident_state=True))
check(len(_out3d) == 3 and all("_incident_state" not in r for r in _out3d) and _fc3d.closed == 1
      and logged("состояние разбора актов не прочитано"),
      "3.2з: сбой второго запроса — календарь без состояния (значок по has_incident), WARNING в журнал")
_fc3e = _FC3([], _states3)
_fc3e.q.append(("занято", ()))                   # первый fetch уже был — отдаёт состояние


async def _own():
    async def _c():
        return _fc3e
    _odb._connect = _c
    try:
        return (await _odb.get_incident_analysis_states("SN", "pcc", 1, [(35931, _ST16, "k1"), (35282, _ST14, "k2")]),
                await _odb.get_incident_analysis_states("SN", "pcc", 1, []))
    finally:
        _odb._connect = _orig_connect

_own3, _empty3 = asyncio.run(_own())
check(_own3 == {35931: {"analysed": True, "ordered": 1, "terminal": None},
                35282: {"analysed": False, "ordered": 0, "terminal": None}}
      and _empty3 == {} and len(_fc3e.q) == 2 and _fc3e.closed == 1,
      f"3.2и: get_incident_analysis_states без соединения — своё и закрывает; пусто — без запроса: {_own3}")

# ══ 3.3. API: incident_analysis в /segments и /segment/{id} ════════════════
_fn3 = next(n for n in ast.parse(_rsrc3).body
            if isinstance(n, ast.FunctionDef) and n.name == "_incident_analysis_fields")
_ns3: dict = {"Any": Any, "logger": logging.getLogger("online.test_routes")}
exec(compile(ast.Module(body=[_fn3], type_ignores=[]), "routes._incident_analysis_fields", "exec"), _ns3)
_iaf = _ns3["_incident_analysis_fields"]
_RD = {"analysed": True, "ordered": 1, "terminal": None}
_NO = {"analysed": False, "ordered": 0, "terminal": None}
check(_iaf(False, None, None, _ST16, _NOW3) == {"incident_analysis": "none", "incident_analysis_reason": None}
      and _iaf(True, _RD, "2026-10-16T06:26:23+00:00", _ST16, _NOW3)
      == {"incident_analysis": "ready", "incident_analysis_reason": None}
      and _iaf(True, _NO, "2026-09-14T07:34:53+00:00", _ST14, _NOW3)
      == {"incident_analysis": "failed", "incident_analysis_reason": "not_ordered"}
      and _iaf(True, {"analysed": False, "ordered": 1, "terminal": None}, None, S(_NOW3, -1800), _NOW3)
      == {"incident_analysis": "pending", "incident_analysis_reason": None}
      and _iaf(True, {"analysed": False, "ordered": 2, "terminal": "failed"}, None, _ST16, _NOW3)
      == {"incident_analysis": "failed", "incident_analysis_reason": "no_answer"},
      "3.3а: ready / failed (причина) / pending / none")
check(_iaf(True, None, None, _ST16, _NOW3) == {"incident_analysis": None, "incident_analysis_reason": None},
      "3.3б: состояние не прочитано — null (UI рисует по has_incident, как раньше)")
_seg_src = _rsrc3.split("async def api_machine_segments", 1)[1].split("\n@router", 1)[0]
_det_src = _rsrc3.split("async def api_segment_detail", 1)[1].split("\n@router", 1)[0]
check("incident_state=True" in _seg_src and '"has_incident":  bool(seg.get("has_incident"))' in _seg_src
      and "**_incident_analysis_fields(bool(seg.get(\"has_incident\")), seg.get(\"_incident_state\")" in _seg_src
      and "seg.get(\"incident_stop_ts\")" in _seg_src,
      "3.3в: /segments — второй запрос календаря, поле рядом с has_incident")
check("odb.get_incident_analysis_state(" in _det_src and "incident_hash(_inc_stop), _inc_stop" in _det_src
      and "if isinstance(_inc, dict):" in _det_src and "**_incident_analysis_fields(isinstance(_inc, dict), _inc_state" in _det_src
      and _det_src.count("get_incident_analysis_state") == 1,
      "3.3г: /segment/{id} — один запрос только при акте, то же поле")
_guide = (ROOT / "docs" / "ui_api_guide.md").read_text(encoding="utf-8")
check(all(x in _guide for x in ('"incident_analysis"', '"has_incident"', "incident_analysis_reason",
                                "акт без разбора совета", "Разбор совета готовится", "not_ordered",
                                "Журнал тревог (`alert_journal`, решение В-25.4) — служебный")),
      "3.3д: docs/ui_api_guide.md — поле, правило значка, тексты блока акта, журнал тревог служебный")

# ══════════════════════ Ревью v5.1.25 — подтверждённые замечания ═══════════

# ══ 4.1. Q1: «до ~» и «не работал» — только при подтверждённой непрерывности ══
# ДГУ №1: стояла на обрыве 05:48, пущена в 10:00 (случай «нет данных счётчика пусков» —
# starts_changed None; сам счётчик 40069 с v5.1.25 есть у всех PCC3300),
# связь в 12:38; сегмент 09:00–12:38, наработка за перерыв 2 ч 38 мин
_H0k, _H1k = (M(24, 5, 47, 11), 30000000.0), (M(24, 12, 38, 0), 30000000.0 + 9480)
_st41 = no_data_run_estimate(_H0k, _H1k, 180, M(24, 9, 0), starts_changed=None, run_state=0)
_ns41 = no_data_run_estimate(_H0k, _H1k, 180, M(24, 9, 0), starts_changed=None, run_state=3)
_t41s, _t41n = labels.no_data_hours_text(_st41, MSK), labels.no_data_hours_text(_ns41, MSK)
check(_st41["kind"] == "hours_only" and _st41["reason"] == "stood" and _st41["end_from"] is None
      and _t41s == "Наработка за перерыв по счётчику — 2 ч 38 мин; на последних данных до обрыва двигатель "
                   "не работал — когда он работал, по счётчику не оценить.",
      f"4.1а: стоял на обрыве — только наработка, без «не работал (остановился ~08:25)»: {_st41} / {_t41s}")
check(_ns41["kind"] == "hours_only" and _ns41["reason"] == "no_starts" and "до ~" not in _t41n
      and "не работал (" not in _t41n and "счётчика пусков у машины нет" in _t41n,
      f"4.1б: KTA50 без счётчика пусков — только наработка: {_t41n}")
check(no_data_run_estimate(_H0k, _H1k, 180, M(24, 9, 0), starts_changed=False, run_state=0)["kind"] == "hours_only"
      and no_data_run_estimate(H0, H1, 180, M(24, 9, 0), starts_changed=False, run_state=3)["kind"] == "until"
      and no_data_run_estimate(H0, (H1[0], H0[1] + (H1[0] - H0[0]).total_seconds() - 60), 180,
                               M(24, 9, 0))["kind"] == "whole"
      and no_data_run_estimate(H0, (H1[0], H0[1]), 180, M(24, 9, 0), run_state=0)["kind"] == "idle",
      "4.1в: стоял, счётчик есть — только наработка; работал и счётчик не менялся — «до ~»; весь перерыв и "
      "ΔH = 0 — без сверки, как раньше")
# Путь движка: KTA50 (CFG) — 40069 в слое контроллера с v5.1.25; запрос H0 берёт и его.
# Значения счётчика в H0 нет — starts_changed None (только наработка)
_e41 = _mk_engine2(CFG)
_hk = [h(CFG.role_to_addr("ENGINE_HOURS"), M(24, 12, 38, 0), _H1k[1])]


async def _lvb41(sn, et, pid, addrs, ts_before, max_age):
    _asked41.append(list(addrs))
    return {CFG.role_to_addr("ENGINE_HOURS"): {"ts": _H0k[0], "value": _H0k[1]}}


_asked41: list = []
_srcm.get_last_values_before = _lvb41
try:
    _e41s = asyncio.run(_e41._no_data_estimate(M(24, 9, 0), M(24, 12, 38, 0), _hk, M(24, 5, 48, 15), run_state=0))
    _e41r = asyncio.run(_e41._no_data_estimate(M(24, 9, 0), M(24, 12, 38, 0), _hk, M(24, 5, 48, 15), run_state=3))
finally:
    _srcm.get_last_values_before = _o_lvb
check(CFG.role_to_addr("START_ATTEMPTS_TOTAL") == 40069 and _asked41 == [[40070, 40069], [40070, 40069]]
      and _e41s["kind"] == "hours_only" and _e41s["reason"] == "stood" and _e41s["starts_changed"] is None
      and _e41r["kind"] == "hours_only" and _e41r["reason"] == "no_starts",
      f"4.1г: движок, ДГУ №1 (KTA50) — стоял / работал без данных счётчика пусков — только наработка: {_e41s} / {_e41r}")
# 4.1е: счётчик пусков есть и не менялся, машина на обрыве работала — «до ~» и у KTA50
_hk2 = _hk + [h(40069, M(24, 12, 38, 0), 1558.0)]


async def _lvb41e(sn, et, pid, addrs, ts_before, max_age):
    return {40070: {"ts": _H0k[0], "value": _H0k[1]}, 40069: {"ts": _H0k[0], "value": 1558.0}}


_srcm.get_last_values_before = _lvb41e
try:
    _e41u = asyncio.run(_e41._no_data_estimate(M(24, 9, 0), M(24, 12, 38, 0), _hk2, M(24, 5, 48, 15), run_state=3))
finally:
    _srcm.get_last_values_before = _o_lvb
check(_e41u is not None and _e41u["starts_changed"] is False and _e41u["kind"] in ("until", "before", "whole"),
      f"4.1е: KTA50, счётчик пусков 40069 не менялся, на обрыве работала — оценка момента останова: {_e41u}")
check(re.search(r"_no_data_estimate\([^)]*run_state=seg\.run_state\)", _usrc, re.S) is not None,
      "4.1д: заглушка смены режима передаёт режим сегмента без связи (режим на последних данных)")

# ══ 4.2. Q2/Q7: продолжение работы через суточный рез — не «короткая» и не «идёт 40 с» ══
_kc = SEG2([SUB2(M(24, 9, 0), M(24, 9, 0, 40), (), {"RPM": CH("rpm", 1501.0, 1501, 1501, 1501, 1501, 4, 1),
                                                     "FREQUENCY": CH("Hz", 50.0, 50, 50, 50, 50, 4, 1)})],
           cause_close=None)
_kc.cause_open = "REPORT_START"
_pc = build_summary_parts([_kc], [], tz=MSK, trip_roles=["RPM", "FREQUENCY"], is_open=True)
_mdc = render_summary_md(_pc)
check("| Обороты двигателя | — |" in _mdc
      and "С 09:00 — 40 с (1 отсчёт): обороты 1501 → 1501 об/мин, частота 50 → 50 Гц" in _mdc
      and "Работа идёт" not in _mdc and "Короткая работа" not in _mdc
      and _pc["key_metrics"]["short"]["since"] == M(24, 9, 0).isoformat(),
      f"4.2а: открытый сегмент-продолжение после 09:00 — «С 09:00 — 40 с (1 отсчёт)», «—» остаётся: "
      f"{[x for x in _mdc.splitlines() if 'С 09' in x or 'Работа' in x]}")
_kc2 = SEG2([SUB2(M(24, 9, 0), M(24, 9, 1), (), {"RPM": CH("rpm", 1500.0, 1498, 1501, 1498, 1500, 6, 6)})])
_kc2.cause_open = "REPORT_START"
_mdc2 = render_summary_md(build_summary_parts([_kc2], [], tz=MSK, trip_roles=["RPM"]))
check("С 09:00 — 1 мин (6 отсчётов): обороты 1498 → 1500 об/мин" in _mdc2 and "Короткая работа" not in _mdc2,
      f"4.2б: продолжение, остановленное в 09:01, — не «Короткая работа (1 мин)»: "
      f"{[x for x in _mdc2.splitlines() if 'С 09' in x or 'Корот' in x]}")
check("Работа идёт 1 мин 40 с" in _mdo and "Короткая работа (20 с, 2 отсчёта)" in _md29,
      "4.2в: новый пуск — «Работа идёт» и «Короткая работа», как раньше")

# ══ 4.3. Q9: часы работы с обрывом связи и 6 отсчётами — не «Короткая работа» ══
_k9 = SEGK([SUBK(0, 11520, {"RPM": CH("rpm", 1498.0, 1497, 1499, 1497, 1499, 1152, 6),
                            "FREQUENCY": CH("Hz", 49.9, 49.9, 50.0, 49.9, 50.0, 1152, 6)})])
_p9 = build_summary_parts([_k9], [], tz=MSK, trip_roles=["RPM", "FREQUENCY"])
_md9 = render_summary_md(_p9)
check(_p9["key_metrics"]["short"]["by"] == "samples" and "| Обороты двигателя | — |" in _md9
      and "Мало отсчётов за работу (3 ч 12 мин, 6 отсчётов): обороты 1497 → 1499 об/мин" in _md9
      and "Короткая работа" not in _md9 and "за 3 ч 12 мин работы" in _md9,
      f"4.3: долгая работа, мало отсчётов — «Мало отсчётов за работу», «—» остаётся: "
      f"{[x for x in _md9.splitlines() if 'отсч' in x]}")

# ══ 4.4. Q10: «на панели сейчас» — только у открытого сегмента ═════════════
_p44o = build_summary_parts([_s27], [_ep_loud], tz=MSK, trip_roles=[], t_from=M(24, 13, 0), t_to=M(24, 13, 10),
                            code_name=CFG3.fault_code_name, common_alarm=(40408, 11, 15.0), is_open=True)
check((_p44o.get("common_alarm_hold") or {}).get("text")
      == f"Висит также Общая авария с 13.10 15:01 (на панели сейчас код 1248 «{_N1248}»)"
      and "на панели сейчас" in (_md28 or ""),
      f"4.4: открытый — «на панели сейчас код 1248»; закрытый — «на конец периода» (2.6е): "
      f"{_p44o.get('common_alarm_hold')}")

# ══ 4.5. Q4: «superseded» в первые 120 с окна — не хвост дебаунса ══════════
_ep_cc = dict(_ep_sup, id=14902, close_reason="condition_cleared")
_fw = eng._filter_window_episodes([_ep_sup, _ep_cc, _ep_loud], M(24, 12, 41, 5), M(24, 13, 34, 3), tail_sec=120)
check([e["id"] for e in _fw] == [14901, 15015],
      f"4.5а: перекрытая через 70 с от начала окна — в окне, снятая так же — хвост: {[e['id'] for e in _fw]}")
# Настоящий путь движка: _build_summary_md_for (фильтр 120 с, закрытие и открытое окно)
_e45 = _mk_engine2()
_db45 = EpDB(eps=[_ep_sup, _ep_loud])
_md45c, _ = _with_db2(_db45, lambda: _e45._build_summary_md_for(
    [_s26], M(24, 12, 41, 13), M(24, 12, 47), closing=True, live_keys={"CONTROLLER_FAULT|40406|2"}))
_md45o, _ = _with_db2(_db45, lambda: _e45._build_summary_md_for(
    [_s26], M(24, 12, 41, 13), M(24, 12, 47), live_keys={"CONTROLLER_FAULT|40406|2"}))
_SUPX = "— перекрыта предупреждением панели «Предупреждение двигателя» с 12:41:50, Общая авария не снята"
check(_SUPX in _line(_md45c or "", "Общая авария") and _SUPX in _line(_md45o or "", "Общая авария"),
      f"4.5б: #36070 через _build_summary_md_for (закрыта через 62 с) — суффикс «перекрыта …» в закрытой и "
      f"открытой Сводке: {_line(_md45c or '', 'Общая авария')}")

# ══ 4.6. Q3: панельный эпизод — по фронту (ДЭС №3 13.10, 1438) ══════════════
K38 = "CONTROLLER_FAULT|40407|8"
_A13, _A13e, _B13 = M(13, 10, 44, 13), M(13, 10, 44, 31), M(13, 10, 45, 26)
_trip46: list = []


async def _trip46_rec(ep_id, t):
    _trip46.append((ep_id, t))


def _mk46(miss, first_miss):
    e = _mk_engine2(episodes={K38: {"id": 14895, "severity": "SHUTDOWN", "miss": miss, "first_miss_ts": first_miss,
                                    "t_open": _A13, "exact": False, "until": None, "until_kind": None,
                                    "code": None, "code_changes": [], "front": _A13}})
    e._episode_accrual_ts = M(13, 10, 45, 0)
    e._attach_trip_context = _trip46_rec
    return e


_dB = PF(40407, 8, "SHUTDOWN", _B13, None, "Отказ прокрутки стартером").to_dict()
_fe46 = {K38: {_A13: _A13e, _B13: None}}
# а) первый фронт в дебаунсе, пришёл второй
_e46 = _mk46(1, _A13e)
_db46 = EpDB()
_with_db2(_db46, lambda: _e46._process_episodes([_dB], M(13, 10, 45, 30), [], front_ends=_fe46))
_c46 = [c for c in _db46.calls if c[0] in ("close", "open", "update")]
check([c[0] for c in _c46] == ["close", "open"] and _c46[0][1:4] == (14895, _A13e, "condition_cleared")
      and _c46[1][1]["t_open"] == _B13 and _e46._episodes[K38]["id"] == 501
      and _e46._episodes[K38]["t_open"] == _B13 and (K38, _A13) in _e46._panel_seeded
      and _trip46 == [(501, _B13)],
      f"4.6а: второй фронт в дебаунсе первого — первый закрыт по своему снятию 10:44:31, второй — свой "
      f"эпизод (контекст аварии — ему): {_c46}, {_trip46}")
# б) в одном цикле: свой фронт снят, новый висит (промаха ещё не было)
_e46b = _mk46(0, None)
_db46b = EpDB()
_with_db2(_db46b, lambda: _e46b._process_episodes([_dB], M(13, 10, 45, 30), [], front_ends=_fe46))
check([c[0] for c in _db46b.calls if c[0] in ("close", "open", "update")] == ["close", "open"]
      and next(c for c in _db46b.calls if c[0] == "close")[2] == _A13e,
      "4.6б: снятие своего фронта и новый фронт в одном цикле — разрез по снятию своего")
# в) снятие своего фронта не известно и промаха не было (рестарт со старой строкой) — как раньше
_e46c = _mk46(0, None)
_db46c = EpDB()
_with_db2(_db46c, lambda: _e46c._process_episodes([_dB], M(13, 10, 45, 30), [], front_ends={}))
check([c[0] for c in _db46c.calls if c[0] in ("close", "open", "update")] == ["update"]
      and _e46c._episodes[K38]["front"] == _B13,
      "4.6в: свой фронт в окне не виден, промаха нет — продолжение, как до v5.1.25")
# г) тот же фронт снова (Общая авария под громким битом) — продолжение
_e46d = _mk46(1, _A13e)
_db46d = EpDB()
_dA = PF(40407, 8, "SHUTDOWN", _A13, None, "Отказ прокрутки стартером").to_dict()
_with_db2(_db46d, lambda: _e46d._process_episodes([_dA], M(13, 10, 45, 30), [], front_ends=_fe46))
check([c[0] for c in _db46d.calls if c[0] in ("close", "open", "update")] == ["update"],
      "4.6г: тот же фронт — продолжение эпизода")
# д) первый промах — снятие своего фронта, а не самое позднее по биту (SHOULD 2)
_e46e = _mk46(0, None)
_e46e._episode_accrual_ts = M(13, 10, 44, 30)
_C13, _C13e = M(13, 10, 44, 40), M(13, 10, 44, 50)
_with_db2(EpDB(), lambda: _e46e._process_episodes(
    [], M(13, 10, 45, 0), [], cleared_at={K38: _C13e}, front_ends={K38: {_A13: _A13e, _C13: _C13e}}))
check(_e46e._episodes[K38]["first_miss_ts"] == _A13e,
      f"4.6д: к промаху снят и следующий фронт — конец по своему 10:44:31, не 10:44:50: "
      f"{_e46e._episodes[K38]['first_miss_ts']}")
# е) карта фронтов и вызов в цикле
_fe46s = eng._panel_front_ends([SEG2([SUB2(M(13, 10, 44, 4), M(13, 10, 44, 31),
                                           [PF(40407, 8, "SHUTDOWN", _A13, _A13e, "x")]),
                                      SUB2(M(13, 10, 45, 7), M(13, 10, 45, 30),
                                           [PF(40407, 8, "SHUTDOWN", _B13, None, "x")])])])
check(_fe46s == _fe46 and "front_ends=_panel_front_ends(segments)" in _usrc,
      f"4.6е: фронты окна по биту; открытое окно передаёт их в _process_episodes: {_fe46s}")
# ж) сегмент, закрытый снятием (10:44:04–10:44:31), — снятие на его конце: не «активен на конец»
_s46 = SEG2([SUB2(M(13, 10, 44, 4), M(13, 10, 44, 31), [PF(40407, 8, "SHUTDOWN", _A13, _A13e, "x")])],
            rs_=0, cause_close="SHUTDOWN_CLEARED")
_kn46 = eng._known_ends([_s46], {K38: {"id": 14895, "t_open": _A13, "front": _A13}}, set(), M(13, 10, 44, 31))
check(_kn46.get(K38) == {"id": 14895, "end": _A13e, "exact": False},
      f"4.6ж: снятие ровно на конце сегмента — известный конец 10:44:31 (как _cleared_by): {_kn46}")

# ══ 4.7. Q5: пуск прошёл без связи — без «масла перед пуском» и момента автомата ══
_c47 = M(30, 20, 33, 17, mo=9)
_OIL3, _CB3 = CFG3.role_to_addr("OIL_TEMP"), CFG3.role_to_addr("GENSET_CB_POS")
_b47 = by_addr([h(RPM, M(30, 15, 33, 23, mo=9), 0.0), h(_OIL3, M(30, 15, 33, 23, mo=9), 87.0),
                h(RPM, _c47, 1496.0), h(FREQ, _c47, 49.9), h(_OIL3, _c47, 93.0)])
_en47 = [en(_CB3, 1, _c47)]
_g47 = [{"gap_start": M(30, 15, 31, 56, mo=9), "gap_end": _c47}]
_sp47 = start_profile(_b47, _en47, CFG3, _c47, _c47, S(_c47, 10), gaps=_g47)
_sp47n = start_profile(_b47, _en47, CFG3, _c47, _c47, S(_c47, 10), gaps=[])
_t47 = labels.start_profile_text(_sp47, MSK) or ""
check(_sp47 is not None and _sp47.get("unobserved") is True and _sp47["oil_temp"] is None
      and _t47.startswith("пуск прошёл без связи — на номинале с первого отсчёта после обрыва (20:33:17)")
      and "автомат уже замкнут" in _t47 and "масло перед пуском" not in _t47 and "замкнут 20:33:17" not in _t47,
      f"4.7а: ДЭС №3 30.09 20:33:17 — пометка, без масла и момента автомата: {_t47}")
check(_sp47n is not None and not _sp47n.get("unobserved") and _sp47n["oil_temp"] == 93.0
      and "масло перед пуском 93 °C" in (labels.start_profile_text(_sp47n, MSK) or ""),
      "4.7б: без разрыва на прокрутке — профиль как раньше")
_b47c = by_addr([h(RPM, S(_c47, -5), 0.0), h(RPM, S(_c47, 5), 600.0), h(RPM, S(_c47, 9), 1500.0)])
check(not (start_profile(_b47c, [], CFG3, _c47, None, S(_c47, 60), gaps=_g47) or {}).get("unobserved"),
      "4.7в: разрыв кончился на прокрутке, но переход порога виден по отсчётам — обычный профиль")

if _errors:
    print(f"FAIL — {len(_errors)} расхождений:")
    for x in _errors:
        print(f"  • {x}")
    sys.exit(1)
print("Все проверки пройдены.")
