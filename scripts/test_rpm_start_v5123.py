# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""v5.1.23 «Пуск и обороты» (план docs/plans/design_v5123_plus.md, раздел 2.1,
решения владельца В1–В6 17.10.2026). Разделы — по номерам плана §2.1 «Тесты».

Раздел 1 — задание частоты 40595 (FREQ_REFERENCE) в загрузке:
- 1а: адрес в whitelist у обеих пар и монолита, роль, единица, домен,
  подпись «Задание частоты (регулятор)»;
- 1б: порядок колонок ленты акта (сразу за частотой, порядок 2.5), те же
  колонки по умолчанию из кода, лента и факты #35929 (16.10 09:26: задание
  30,6 → 55 Гц), неизменное задание — только в «Без изменений»;
- 1в: паритет монолита и слоёв (_defaults + controllers/pcc3300 +
  engines/cummins_kta50): все файлы конфига, регистры без domain, ключи v5.1.23;
- 1г: в стоп-сегменте отчёта задание не печатается, в работе — печатается;
- 1д: подробные блоки акта и вход гейта получают задание сами (_chars_of,
  _all_params, группа «Двигатель»).
Раздел 2 — метки заброса оборотов (п.2, В2′): порог 1575 = уставка разноса из
thresholds двигателя − 150 (proximity_pct 8,69565 из KB, 91,3 % уставки; не
константа), обороты и 30·f_max опроса строго выше порога, частоте верим в
40–65 Гц; да — 1696/56,8, 1584/52,9, 1606 при опросе [0; 53,6], 1576/52,6;
нет — 1575/52,6 (ровно порог), 1574/52,5, 1560/52,0 и 1553/51,8 (ДЭС №3
августа), 1551/51,6, 1538/50,9, 1542/50,5, без частоты, 66 Гц, сбой датчика
вверх при 50 Гц, 30·52,5 = 1575; умолчание кода = KB.
Раздел 3 — эпизод RPM_OVERSPEED: ДГУ №2 15.10 12:42 МСК (реальные отсчёты) при
пороге KB 1575 (12:42:19–12:42:29, один участок) и для сравнения при прежнем
1552,5 (12:42:19–12:43:09, два участка); ДГУ №1
16.10 09:26:03 (#35929); CAUTION через cap_severity; ref_codes [234], код в
fault_codes только от панели; без участков (None, сбой ключа, ключа нет)
детекции нет; сбой ключа и детектора изолирован; тексты (имя, худшее значение,
запас до уставки разноса).
Раздел 4 — профиль пуска (п.3, В1 вариант «б»): пограничные пуски из
rpm/hunt.out (ряды сняты с БД и сжаты — синтетика без БД): 27.09 17:24, 15.10
13:00, 20.09 16:34 (прогрев 5 мин), 25.09 13:48, 15.10 05:42 (52,2 Гц на
прокрутке — неустойчив), холодные 12.10 13:43 и 29.09 06:08, 15.10 13:28; и
реальные неустойчивые ДГУ №1 16.10 09:25:52 / 09:28:33 / 09:29:33 / 09:57:55,
ДГУ №2 15.10 12:42; границы правил (51,5 / 48,5 / 30 с / 2 смены знака),
конец окна (номинал + 5 мин, автомат, режим, выбег), не пуск (рез, работа
после охлаждения, попытка), прогрев в контексте живого движка, открытое окно,
выключатель KB, изоляция сбоя, срезы без разбора всей истории.
Раздел 5 — гейт (п.4): start_unstable — один помощник в uncancellable_reasons и
suppression_bans; профиль в characteristics_json открытой строки и в статусе
(start), начало первого кластера у тревог статуса (onset), строка «Пуск» во
входе гейта, _inherit_allowed и _ban_row; провал под нагрузкой после окна и
заброс вне пуска (В3) снимать можно, заброс на пуске — нельзя; устойчивый пуск
— можно; старая строка без профиля; сбой помощника изолирован.
Раздел 6 — тексты: пометка «на пуске» в сводке (и в шапке заключения),
заголовок «ЗАМЕЧАНИЯ — N» — заброс добавляет вид, раскачка нет; строка «Пуск»
в отчёте у каждого пуска (у неустойчивого — с причинами) и в акте после блока
сегмента падения.
Раздел 7 — подпись плашки попытки (п.5, решение В4): порог прокрутки из KB
(transition_confirm.crank_rpm_min: 50); реальные плашки ДГУ №1 16.10 09:27
(#35935, #35937) — «Неудачный пуск · Стоп», факты только наблюдаемые
(прокрутка, АКБ, 40010 и 40599 в кадре падения), целевые тексты плана; матрица
attempt_label: режим панели × прокрутка × сигналы × исход × коды; старая
запись без новых полей — подпись по rpm_max, факты прежние; подпись при чтении
(календарь, статус); граница порога и KB; без прокрутки с кодом 1438 и аварией
— «Неудачный пуск · Авария», коды в фактах по запросу (без повтора в
заключении плашки); «Отменённый пуск · Стоп»; изоляция сбоя наблюдаемого.
Раздел 8 — имена кодов и 40599 (п.6, решения В5 и В7): имя кода из
справочника KB и «(код N)» в сводке и в отчёте (имя бита — values.bit_name,
fault_name и ключи эпизодов те же; эпизод до v5.1.23 — через справочник
движка или детекцию фронта); 1464 «Load Dump — …» в обеих копиях и в статусе;
1992 «Crankshaft Sensor High»; описание и метка 40599 = 0 «нет команды (0)»
поверх каталога; голос характера «команда «Работа» снята сразу (3→0), без
«Стоп»».
Раздел 9 — изоляция ключей segment_spans (п.7): сбой одного ключа — None в
словаре, его детектор по подсегменту, RPM_UNDERSPEED, LIMIT_PROXIMITY и паспорт
остаются по участкам; запись в лог; через segment() то же; LIVE_KEYS удалена.
Раздел 10 — выключатель комбинированной ветки OIL_DILUTION (п.8, В6 = А):
в KB выключена, без ключа — прежнее поведение, паспорт работает.

Исправления по ревью v5.1.23 (пункты в своих разделах):
- 1е (P3): вход гейта у стоящей машины — без задания частоты;
- 2а, 4а (R12): ключи KB RPM_OVERSPEED и start_profile действительно читаются
  (смена значения в KB меняет параметр; набор ключей KB = набор ключей кода);
- 4з (R7): живой путь — задание и масло на начало прокрутки из посева перед
  окном (прогрев 0→2→3 длиннее преамбулы 300 с), FREQ_REFERENCE в посеве;
- 4и (R14): номинал в кадре смены режима — «на номинале с первого отсчёта»
  с оценкой сверху, а не «за 0 с»;
- 5а (R13, P5): время в причине запрета start_unstable — с «МСК» (вход гейта и
  «Факты (система, не ИИ)»);
- 5д (R1): суточный рез посреди окна неустойчивого пуска — профиль предка
  (start_profile_prev) с окном без обрезки, начало эпизода (мост через рез),
  запрет в обоих местах, строка «Пуск» предка во входе гейта;
- 5е, 6е (R4): запрет, появившийся после применённого снятия, — тик снимает
  пометку «снято», гейт не метит, сводка и поздний вердикт снятие не применяют;
- 6ж (R9): «из них на пуске N: …», когда в окне пуска начались не все эпизоды;
- 8ж (R10, P1, P2): имя по коду в Блоке 1 заключения, пакетной сводке и
  бегущей строке UI.

Юнит-тесты синтетические, без БД; проверки на реальной БД —
scripts/check_v5123_data.py.

Запуск:  py -3 scripts/test_rpm_start_v5123.py
"""
from __future__ import annotations

import copy
import inspect
import logging
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import analytics.live as _lv  # noqa: E402
from analytics.config import AnalyticsConfig  # noqa: E402
from analytics.contract import DerivedMetrics, RiskAccumulators  # noqa: E402
from analytics.detectors import _combined_oil_detection, _passport_detection, run_all_detectors  # noqa: E402
from analytics.labels import ROLE_LABELS, role_label  # noqa: E402
from analytics.live import segment_spans  # noqa: E402
from analytics.reconstructor import build_analog_trace, fall_facts_ru, format_analog_trace_ru  # noqa: E402
from analytics.segmenter import segment  # noqa: E402
from analytics.serializer import to_markdown  # noqa: E402

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
CFG = AnalyticsConfig.from_pair(KB, "pcc3300", "cummins_kta50")     # ДГУ №1 / №2
CFG3 = AnalyticsConfig.from_pair(KB, "pcc3300", "cummins_qsx15")    # ДЭС №3
MONO = AnalyticsConfig(KB / "equipment" / "cummins_kta50_pcc3300")  # legacy-монолит
RPM, FREQ, FREF, HB = 40068, 40044, 40595, 40290
OILT, COOL3, PRESS3 = (CFG3.role_to_addr(r) for r in ("OIL_TEMP", "COOLANT_TEMP", "OIL_PRESS"))


def T(h, m, s=0, d=15):
    return datetime(2026, 10, d, h, m, s, tzinfo=U)


def M(d, h, m, s=0):
    """Время МСК (как в разборах) → UTC."""
    return datetime(2026, 10, d, h, m, s, tzinfo=MSK).astimezone(U)


def S(t, sec):
    return t + timedelta(seconds=sec)


def h(addr, ts, v):
    return {"addr": addr, "ts": ts, "value": v}


def rs(v, a, b=None):
    return {"addr": 40011, "value": v, "state_start": a, "state_end": b}


def iso(t):
    return t.isoformat()


def pings(a, b, step=10):
    """Пакеты связи (пинг 40290) каждые step с."""
    out, t, k = [], a, 0
    while t < b:
        out.append(h(HB, t, float(k)))
        t += timedelta(seconds=step)
        k += 1
    return out


def by_addr(rows):
    out: dict[int, list[dict]] = {}
    for r in sorted(rows, key=lambda r: r["ts"]):
        out.setdefault(r["addr"], []).append(r)
    return out


def _boom(*a, **k):
    raise RuntimeError("сбой ключа (тест)")


L0 = T(6, 0)            # 09:00 МСК
L1 = S(L0, 1800)


def work_rows(a=L0, b=L1, extra=(), pre=300, rpm=1500.0):
    """Работа: пинг, обороты 1500/1501 и 50 Гц каждые 10 с; extra — поверх
    (строка extra с той же меткой и адресом заменяет фоновую)."""
    ex = {(r["addr"], r["ts"]) for r in extra}
    rows = pings(S(a, -pre), b)
    t, k = S(a, -pre), 0
    while t < b:
        for addr, v in ((RPM, rpm + (k % 2)), (FREQ, 50.0)):
            if (addr, t) not in ex:
                rows.append(h(addr, t, v))
        t += timedelta(seconds=10)
        k += 1
    return sorted(rows + list(extra), key=lambda r: r["ts"])


def seg3(rows, enum, cfg=CFG3, a=L0, b=L1):
    return segment(enum, rows, [], [], cfg, "SN", "pcc", 1, "E", a, b)


def dets(segs, scen):
    return [(sub, d) for s in segs for sub in s.subsegments for d in sub.detections
            if d.scenario == scen and not sub.transient]


# ══ 1. Задание частоты 40595 в загрузке (п.1) ═══════════════════════════════

# ── 1а. Whitelist, роль, единица, домен, подпись ──
for _nm, _c in (("KTA50", CFG), ("QSX15", CFG3), ("монолит", MONO)):
    _m = _c.register_map.get(FREF) or {}
    check(FREF in _c.whitelist_analog and _c.role_to_addr("FREQ_REFERENCE") == FREF,
          f"1а: {_nm}: 40595 в whitelist аналога, роль FREQ_REFERENCE")
    check(_m.get("kind") == "analog" and _m.get("unit") == "Hz" and float(_m.get("incident_trace")) == 2.5,
          f"1а: {_nm}: analog, Гц, incident_trace 2.5: {_m}")
    check(FREF not in _c.whitelist_enum and FREF not in _c.whitelist_fault, f"1а: {_nm}: только аналог")
check((CFG.register_map.get(FREF) or {}).get("domain") == "CONTROLLER", "1а: домен CONTROLLER (шов H3)")
check(ROLE_LABELS.get("FREQ_REFERENCE") == "Задание частоты (регулятор)"
      and role_label("FREQ_REFERENCE") == "Задание частоты (регулятор)", "1а: подпись роли")
# Горячий цикл, закрытие и пакетный прогон берут whitelist из карты — адрес идёт сам
import analytics.runner as _runner  # noqa: E402
import online.engine as _eng  # noqa: E402
_src_eng = inspect.getsource(_eng)
check(_src_eng.count("whitelist_analog") >= 3 and "whitelist_analog" in inspect.getsource(_runner),
      "1а: загрузка истории — по cfg.whitelist_analog (движок и пакетный прогон)")

# ── 1б. Порядок колонок ленты акта ──
_TRACE_ORDER = ["RPM", "FREQUENCY", "FREQ_REFERENCE", "ACTIVE_POWER_TOTAL", "CURRENT_AVG",
                "VOLTAGE_LL_AVG", "OIL_PRESS", "BATTERY_V", "GENSET_CB_POS"]
for _nm, _c in (("KTA50", CFG), ("QSX15", CFG3), ("монолит", MONO)):
    check([r["role"] for r in _c.incident_trace_registers] == _TRACE_ORDER,
          f"1б: {_nm}: задание — сразу за частотой: {[r['role'] for r in _c.incident_trace_registers]}")
# Рабочая копия mapping.yaml без пометок — те же колонки по умолчанию из кода
_nt = AnalyticsConfig.from_pair(KB, "pcc3300", "cummins_kta50")
for _m in _nt._register_map.values():
    _m.pop("incident_trace", None)
check([r["role"] for r in _nt.incident_trace_registers] == _TRACE_ORDER,
      f"1б: без пометок — те же колонки из кода: {[r['role'] for r in _nt.incident_trace_registers]}")

# Лента и факты #35929 (ДГУ №1 16.10, останов 09:26:23 МСК) — значения из БД
STOP29 = M(16, 9, 26, 23)
_rows29 = [h(RPM, M(16, 9, 25, 12), 0.0), h(FREQ, M(16, 9, 17, 3), 0.0), h(FREF, M(16, 9, 19, 23), 0.0),
           h(RPM, M(16, 9, 26, 3), 1696.0), h(FREQ, M(16, 9, 26, 3), 56.8), h(FREF, M(16, 9, 26, 3), 30.633),
           h(RPM, M(16, 9, 26, 12), 1247.0), h(FREQ, M(16, 9, 26, 12), 41.5), h(FREF, M(16, 9, 26, 12), 55.0),
           h(RPM, M(16, 9, 26, 23), 76.0), h(FREQ, M(16, 9, 26, 23), 0.0), h(FREF, M(16, 9, 26, 23), 0.0),
           h(RPM, M(16, 9, 26, 32), 0.0)]
tr29 = build_analog_trace(by_addr(_rows29), [], CFG, M(16, 9, 25, 52), STOP29, M(16, 9, 26, 53))
check(tr29 and [c["role"] for c in tr29["cols"]][:3] == ["RPM", "FREQUENCY", "FREQ_REFERENCE"]
      and tr29["entry"].get("FREQ_REFERENCE") == {"v": 0.0, "t": iso(M(16, 9, 19, 23))},
      f"1б: лента — колонка задания за частотой, вход — запись до окна: {tr29 and tr29['cols'][:3]}")
_tl29 = format_analog_trace_ru(tr29, MSK)
_head = next((ln for ln in _tl29 if ln.startswith("  время")), "")
_r03 = next((ln for ln in _tl29 if ln.startswith("  09:26:03")), "")
check(_head.split()[1:4] == ["об/мин", "Гц", "задание"] and _r03.split()[1:4] == ["1696", "56,8", "30,6"],
      f"1б: печать — колонка «задание» за «Гц»: {_head!r} / {_r03!r}")
_ff29 = fall_facts_ru(tr29, MSK) or ""
check("обороты, частота и задание по отсчётам: 09:26:03 1696 об/мин / 56,8 Гц / задание 30,6 Гц → "
      "09:26:12 1247 об/мин / 41,5 Гц / задание 55 Гц → "
      "09:26:23 (кадр останова) 76 об/мин / 0 Гц / задание 0 Гц" in _ff29,
      f"1б: факты аварии — задание 30,6 → 55 Гц в цепочке оборотов: {_ff29}")
# Задание не менялось за ленту (ДЭС №3: 49,9) — «Без изменений», в факты не идёт
_rows_c = [h(RPM, M(16, 9, 25, 12), 1500.0), h(FREQ, M(16, 9, 25, 12), 50.0), h(FREF, M(16, 9, 19, 23), 49.9),
           h(RPM, M(16, 9, 26, 3), 1480.0), h(FREQ, M(16, 9, 26, 3), 49.3),
           h(RPM, M(16, 9, 26, 23), 900.0), h(FREQ, M(16, 9, 26, 23), 30.0)]
tr_c = build_analog_trace(by_addr(_rows_c), [], CFG, M(16, 9, 25, 52), STOP29, M(16, 9, 26, 53))
_tlc = format_analog_trace_ru(tr_c, MSK)
_ffc = fall_facts_ru(tr_c, MSK) or ""
check(any(ln.startswith("  Без изменений за ленту:") and "задание 49,9 Гц" in ln for ln in _tlc)
      and not any(ln.startswith("  время") and "задание" in ln for ln in _tlc),
      f"1б: неизменное задание — в строке «Без изменений», не колонкой: {_tlc}")
check("обороты и частота по отсчётам: 09:26:03 1480 об/мин / 49,3 Гц" in _ffc and "задание" not in _ffc,
      f"1б: неизменное задание в факты не идёт (не скобкой у каждого звена): {_ffc}")
# Старая лента (акт v5.1.22, колонки без задания) — печать и факты прежние
tr_old = copy.deepcopy(tr29)
tr_old["cols"] = [c for c in tr_old["cols"] if c["role"] != "FREQ_REFERENCE"]
tr_old["entry"].pop("FREQ_REFERENCE", None)
for _r in tr_old["rows"]:
    _r["v"].pop("FREQ_REFERENCE", None)
_ffo = fall_facts_ru(tr_old, MSK) or ""
check("обороты и частота по отсчётам: 09:26:03 1696 об/мин / 56,8 Гц → 09:26:12 1247 об/мин / 41,5 Гц → "
      "09:26:23 (кадр останова) 76 об/мин / 0 Гц" in _ffo and "задание" not in "\n".join(format_analog_trace_ru(tr_old, MSK)),
      f"1б: акт без колонки задания — прежний вид: {_ffo}")

# ── 1в. Паритет монолита и слоёв ──
for _attr in ("thresholds", "zones", "segmentation", "detectors", "fault_matrix"):
    check(getattr(MONO, _attr) == getattr(CFG, _attr), f"1в: {_attr}: монолит ≠ слои")
check(MONO.whitelist_analog == CFG.whitelist_analog and MONO.whitelist_enum == CFG.whitelist_enum
      and MONO.whitelist_fault == CFG.whitelist_fault, "1в: whitelist монолита ≠ слоёв")
_reg_l = {a: {k: v for k, v in m.items() if k != "domain"} for a, m in CFG.register_map.items()}
check(MONO.register_map == _reg_l,
      f"1в: регистры монолита ≠ слоёв (без domain): "
      f"{sorted(set(MONO.register_map) ^ set(_reg_l)) or [a for a in _reg_l if MONO.register_map.get(a) != _reg_l[a]]}")
check(MONO.controller_label == CFG.controller_label == "Cummins PowerCommand PCC3300", "1в: имя контроллера")
check(MONO.seg("fault_cleared", "stable_clean_sec") == 300
      and MONO.seg("stop_kind", "act_trace_max_rows") == 40 and MONO.seg("stop_kind", "fault_reset_window_sec") == 60,
      "1в: ключи сегментации v5.1.22 в монолите")
check(MONO.det("OIL_DILUTION", "combined_enabled") is False and CFG.det("OIL_DILUTION", "combined_enabled") is False
      and CFG3.det("OIL_DILUTION", "combined_enabled") is False, "1в: выключатель OIL_DILUTION — и в монолите")

# ── 1г. Отчёт: в стоп-сегменте задание не печатается ──
_ser_rows = pings(S(L0, -300), L1)
_t, _k = S(L0, -300), 0
while _t < L1:
    _stop = _t < S(L0, 900)
    _ser_rows += [h(RPM, _t, 0.0 if _stop else 1500.0 + _k % 2), h(FREQ, _t, 0.0 if _stop else 50.0),
                  h(FREF, _t, 30.0 if _stop else 50.0)]
    _t += timedelta(seconds=10)
    _k += 1
_ser = seg3(sorted(_ser_rows, key=lambda r: r["ts"]), [rs(0, S(L0, -3600), S(L0, 900)), rs(3, S(L0, 900))],
            cfg=CFG)
_md = to_markdown(_ser, "SN", "pcc", 1, L0, L1, tz=MSK)
_blocks = _md.split("### Сегмент ")
_stop_b = next((b for b in _blocks if "(RUN_STATE=0)" in b.split("\n")[0]), "")
_work_b = next((b for b in _blocks if "(RUN_STATE=3)" in b.split("\n")[0]), "")
check([s.run_state for s in _ser] == [0, 3]
      and all("FREQ_REFERENCE" in (sub.characteristics or {}) for s in _ser for sub in s.subsegments),
      f"1г: задание — в характеристиках обоих сегментов: {[s.run_state for s in _ser]}")
check(_stop_b and "| RPM |" in _stop_b and "FREQ_REFERENCE" not in _stop_b,
      "1г: стоп-сегмент — без строки задания (остальные роли печатаются)")
check(_work_b and "| FREQ_REFERENCE | Hz |" in _work_b, "1г: работа — задание в таблице характеристик")

# ── 1д. Акт и гейт получают задание без правок ──
from online.incident_gate import _NOT_PARAMS, _chars_of  # noqa: E402
from online.status_assembler import _all_params, _param_group  # noqa: E402
_ch = _chars_of({"characteristics_json": {"subsegments": [
    {"characteristics": {"FREQ_REFERENCE": {"min": 30.633, "max": 55.0, "value_end": 0.0, "unit": "Hz"}}}]}})
check("FREQ_REFERENCE" not in _NOT_PARAMS and _ch.get("FREQ_REFERENCE", {}).get("max") == 55.0,
      f"1д: подробные блоки акта — задание из характеристик сегмента: {_ch}")
_pp = _all_params({"FREQ_REFERENCE": {"value": 50.0, "unit": "Hz", "min": 49.9, "max": 50.2}})
check(_param_group("FREQ_REFERENCE") == "Двигатель" and _pp
      and _pp[0]["role"] == "FREQ_REFERENCE" and _pp[0]["group"] == "Двигатель" and _pp[0]["value"] == 50.0,
      f"1д: вход гейта — параметр в группе «Двигатель»: {_pp}")

# ── 1е. Вход гейта у стоящей машины — без задания частоты (как отчёт стоп-сегмента) ──
from online.status_assembler import build_structural_status as _bss1, build_warning_prompt as _bwp1  # noqa: E402


def _row1e(run_state):
    return {"id": 1, "run_state": run_state, "t_start": L0, "characteristics_json": {},
            "current_values_json": {"values": {"FREQ_REFERENCE": {"value": 30.0, "unit": "Hz"},
                                               "OIL_TEMP": {"value": 40.0, "unit": "C"}}},
            "active_detections_json": [{"scenario": "CONTROLLER_FAULT", "severity": "CAUTION",
                                        "trigger": "Общая авария (addr=40401, bit=0)",
                                        "values": {"addr": 40401, "bit": 0, "fault_name": "Общая авария"}}]}


_st1e0, _st1e3 = _bss1(_row1e(0), None), _bss1(_row1e(3), None)
check([p["role"] for p in _st1e0["params"]] == ["OIL_TEMP"]
      and {"FREQ_REFERENCE", "OIL_TEMP"} == {p["role"] for p in _st1e3["params"]}
      and "FREQ_REFERENCE" not in _bwp1(_st1e0) and "FREQ_REFERENCE: 30 Hz" in _bwp1(_st1e3),
      f"1е: стоп — задание частоты во вход гейта не идёт (на ходу — идёт): "
      f"{[p['role'] for p in _st1e0['params']]} / {[p['role'] for p in _st1e3['params']]}")
from analytics.serializer import _STOP_HIDDEN_ROLES  # noqa: E402
from online.status_assembler import _STOP_HIDDEN_PARAMS  # noqa: E402
check(_STOP_HIDDEN_PARAMS == _STOP_HIDDEN_ROLES and _all_params({"FREQ_REFERENCE": {"value": 30.0}}) != []
      and _all_params({"FREQ_REFERENCE": {"value": 30.0}}, skip=_STOP_HIDDEN_PARAMS) == [],
      "1е: те же роли, что скрыты в отчёте стоп-сегмента; без skip — параметр есть (как раньше)")

# ══ 2. Метки заброса оборотов (п.2, В2) ═════════════════════════════════════
from analytics.contract import cap_severity  # noqa: E402
from analytics.detectors import _detect_rpm_overspeed  # noqa: E402
from analytics.rpm_freq import build_poll, is_over, over_marks, overspeed_params  # noqa: E402
from analytics.serializer import SCENARIO_RU, _det_worst  # noqa: E402
from corpus.preprocessor import _fmt_detections_hierarchy, _group_worst  # noqa: E402


def cfg_thr(base, ovs):
    """Копия конфига с другой уставкой разноса (… — ключа нет вовсе)."""
    c = copy.copy(base)
    c.thresholds = copy.deepcopy(base.thresholds)
    for sec in ("rpm", "frequency"):
        ctl = c.thresholds.get(sec, {}).get("controller", {})
        ctl.pop("overspeed_rpm", None)
        if ovs is not ...:
            ctl["overspeed_rpm"] = ovs
    return c


def cfg_det(base, scen, **kw):
    """Копия конфига с правкой секции детектора."""
    c = copy.copy(base)
    c.detectors = dict(base.detectors, **{scen: dict(base.detectors.get(scen) or {}, **kw)})
    return c


# ── 2а. Порог — от уставки разноса двигателя (уставка − 150, В2′), частота 40–65 Гц ──
from analytics.labels import fmt_number  # noqa: E402
from analytics.rpm_freq import OVERSPEED_PROXIMITY_PCT  # noqa: E402

for _nm, _c in (("KTA50", CFG), ("QSX15", CFG3), ("монолит", MONO)):
    _op = overspeed_params(_c)
    _ovs = _c.thr("rpm", "controller", "overspeed_rpm")
    check(_op is not None and _ovs == 1725 and _op["overspeed_rpm"] == _ovs
          and abs(_op["threshold_rpm"] - (_ovs - 150)) < 1e-3 and 1575.0 < _op["threshold_rpm"] < 1575.001
          and abs(_op["threshold_rpm"] - _ovs * (1 - 8.69565 / 100)) < 1e-9
          and (_op["freq_lo"], _op["freq_hi"]) == (40.0, 65.0) and _op["k"] == 30.0 and _op["fault_code"] == 234,
          f"2а: {_nm}: порог 1575 = уставка {_ovs} − 150 (строго «выше»: 1575 — не заброс), частота 40–65 Гц, "
          f"k 30, код 234: {_op and _op['threshold_rpm']}")
    check(_c.det("RPM_OVERSPEED", "severity_default") == "CAUTION"
          and _c.det("RPM_OVERSPEED", "proximity_pct") == 8.69565,
          f"2а: {_nm}: секция KB RPM_OVERSPEED (proximity_pct 8,69565 = 150/1725, В2′): "
          f"{_c.det('RPM_OVERSPEED', 'proximity_pct')}")
    check(fmt_number(_op["threshold_rpm"], 1) == "1575" and fmt_number(100 - _op["proximity_pct"], 1) == "91,3",
          f"2а: {_nm}: в текстах — «1575» и «91,3 %», не «1575,0» / «1574,9»: "
          f"{fmt_number(_op['threshold_rpm'], 1)}, {fmt_number(100 - _op['proximity_pct'], 1)}")
check(abs(overspeed_params(cfg_thr(CFG, 1750))["threshold_rpm"] - 1597.826125) < 1e-6
      and abs(overspeed_params(cfg_thr(CFG3, 1800))["threshold_rpm"] - 1643.4783) < 1e-6,
      f"2а: порог считается от уставки двигателя, не константа (1750 → 1597,8, 1800 → 1643,5): "
      f"{overspeed_params(cfg_thr(CFG, 1750))['threshold_rpm']}, {overspeed_params(cfg_thr(CFG3, 1800))['threshold_rpm']}")
check(overspeed_params(cfg_thr(CFG, ...)) is None, "2а: уставки разноса в KB нет — параметров нет (порог не выдумываем)")
# Умолчание кода = KB (В2′): ключа нет (опечатка) — тот же порог 1575, не прежние 1552,5
_c_nokey = copy.copy(CFG)
_c_nokey.detectors = dict(CFG.detectors, RPM_OVERSPEED={k: v for k, v in CFG.detectors["RPM_OVERSPEED"].items()
                                                        if k != "proximity_pct"})
_o_nokey = overspeed_params(_c_nokey)
check(OVERSPEED_PROXIMITY_PCT == CFG.det("RPM_OVERSPEED", "proximity_pct")
      and _o_nokey["proximity_pct"] == OVERSPEED_PROXIMITY_PCT
      and _o_nokey["threshold_rpm"] == overspeed_params(CFG)["threshold_rpm"]
      and overspeed_params(cfg_det(CFG, "RPM_OVERSPEED", proximity_pct="?"))["threshold_rpm"]
      == overspeed_params(CFG)["threshold_rpm"],
      f"2а: умолчание кода = KB: без ключа proximity_pct и при мусоре — порог 1575: {_o_nokey['threshold_rpm']}")
# Ревью R12: умолчания в коде равны KB — ключи проверяются сменой значения в KB
_o5 = overspeed_params(cfg_det(CFG, "RPM_OVERSPEED", proximity_pct=5))
_of = overspeed_params(cfg_det(CFG, "RPM_OVERSPEED", freq_valid_hz=[40, 60]))
_oc = overspeed_params(cfg_det(CFG, "RPM_OVERSPEED", fault_code=1992))
check(abs(_o5["threshold_rpm"] - 1638.75) < 1e-9 and _o5["proximity_pct"] == 5
      and (_of["freq_lo"], _of["freq_hi"]) == (40.0, 60.0)
      and not is_over(1600.0, [62.0], _of) and is_over(1600.0, [62.0], overspeed_params(CFG))
      and _oc["fault_code"] == 1992,
      f"2а: ключи KB RPM_OVERSPEED читаются: proximity_pct 5 → 1638,75; freq_valid_hz [40, 60] — 62 Гц "
      f"не подтверждает; fault_code 1992: {_o5['threshold_rpm']}, {(_of['freq_lo'], _of['freq_hi'])}, {_oc['fault_code']}")
_kb_os = {k for k in (CFG.detectors.get("RPM_OVERSPEED") or {}) if not k.startswith("_")}
_code_os = set(re.findall(r'\bd\("(\w+)"', inspect.getsource(overspeed_params))) | {"enabled", "severity_default"}
check(_kb_os == _code_os, f"2а: ключи секции RPM_OVERSPEED KB = ключи, которые читает код: {sorted(_kb_os ^ _code_os)}")
# Частота провала (v*) осталась 40–55 Гц — у заброса своя граница
from analytics.rpm_freq import rpm_freq_params  # noqa: E402
check((rpm_freq_params(CFG)["freq_lo"], rpm_freq_params(CFG)["freq_hi"]) == (40.0, 55.0),
      "2а: граница частоты провала не тронута (40–55 Гц)")


def marks2(rpm_pts, freq_pts, cfg=CFG, a=L0):
    """Метки заброса: отсчёты [(с от a, об/мин)], частота [(с, Гц)], пинг раз в 10 с →
    {с: (обороты, f_max, в заборе)}."""
    rows = (pings(S(a, -300), S(a, 600)) + [h(RPM, S(a, s), v) for s, v in rpm_pts]
            + [h(FREQ, S(a, s), v) for s, v in freq_pts])
    ba = by_addr(rows)
    op = overspeed_params(cfg)
    poll = build_poll(ba, cfg, op)
    rr = [(r["ts"], r["value"]) for r in ba.get(RPM, [])]
    return {round((t - a).total_seconds()): (v, f, bad) for t, v, f, bad in over_marks(rr, poll, op)}


# ── 2б. Да: реальные забросы 30 дней ──
_m = marks2([(0, 1696.0), (9, 1247.0)], [(0, 56.8), (9, 41.5)])
check(_m[0] == (1696.0, 56.8, True) and _m[9][2] is False, f"2б: 1696 при 56,8 Гц (ДГУ №1 16.10 09:26:03) — заброс: {_m}")
_m = marks2([(0, 95.0), (10, 1584.0), (20, 1512.0)], [(10, 52.9), (20, 51.5)])
check(_m[10] == (1584.0, 52.9, True) and not _m[20][2], f"2б: 1584 при 52,9 Гц (ДГУ №1 16.10 09:29:42) — заброс: {_m}")
# ДГУ №2 15.10 12:42:19: удержано 0 Гц стоянки, строка 53,6 через секунду
_m = marks2([(0, 197.0), (10, 1606.0), (20, 1551.0)], [(-200, 0.0), (11, 53.6), (20, 51.6)])
_p2 = build_poll(by_addr(pings(S(L0, -300), S(L0, 600)) + [h(FREQ, S(L0, -200), 0.0), h(FREQ, S(L0, 11), 53.6)]),
                 CFG, overspeed_params(CFG))
check(_p2.window(S(L0, 10), S(L0, 20)) == [0.0, 53.6] and _m[10] == (1606.0, 53.6, True),
      f"2б: 1606 при опросе [0; 53,6] — 0 Гц отброшен, максимум 53,6 подтверждает: {_m}")
_m = marks2([(0, 1576.0)], [(0, 52.6)])
check(_m[0] == (1576.0, 52.6, True), f"2б: 1576 при 52,6 Гц — заброс (новая граница, В2′): {_m}")

# ── 2в. Нет ──
for _rpm_v, _hz, _why in ((1575.0, 52.6, "1575/52,6 — ровно порог, строго «выше»"),
                          (1574.0, 52.5, "1574/52,5 — ниже порога"),
                          (1560.0, 52.0, "1560/52,0 (типичный скачок ДЭС №3 августа) — при 1575 не заброс"),
                          (1553.0, 51.8, "1553/51,8 (ДЭС №3 августа, был бы заброс при 1552,5)"),
                          (1551.0, 51.6, "1551/51,6 (ДГУ №2 12:42:29) — обороты не выше порога"),
                          (1538.0, 50.9, "1538/50,9 (сброс 367 кВт 29.09)"),
                          (1542.0, 50.5, "1542/50,5 (ДЭС №3 17.09)"),
                          (1696.0, 50.0, "сбой датчика вверх: 30·50 = 1500 ниже порога"),
                          (1600.0, 52.5, "30·52,5 = 1575 — строго «выше»"),
                          (1980.0, 66.0, "66 Гц — частоте не верим")):
    _m = marks2([(0, _rpm_v)], [(0, _hz)])
    check(_m[0][2] is False, f"2в: {_why} — не заброс: {_m[0]}")
check(marks2([(0, 1600.0)], [(0, 52.51)])[0][2] is True, "2в: 30·52,51 = 1575,3 > 1575 — заброс (граница)")
_m = marks2([(0, 1560.0), (10, 1553.0)], [(0, 52.0), (10, 51.8)], cfg=cfg_det(CFG, "RPM_OVERSPEED", proximity_pct=10))
check(_m[0][2] is True and _m[10][2] is True,
      f"2в: (для сравнения) при прежнем пороге 1552,5 те же 1560/52,0 и 1553/51,8 — заброс: {_m}")
check(marks2([(0, 1980.0)], [(0, 66.0)])[0][1] is None, "2в: 66 Гц — в f_max не идёт")
_m = marks2([(0, 1696.0), (10, 1696.0)], [])
check(not any(x[2] for x in _m.values()) and all(x[1] is None for x in _m.values()),
      f"2в: без частоты заброса нет (по одним оборотам не ловим): {_m}")
_op = overspeed_params(CFG)
check(over_marks([(L0, 1696.0)], None, _op) == [(L0, 1696.0, None, False)],
      "2в: регистра частоты нет (опроса нет) — заброса нет")
check(is_over(1696.0, [0.0, 66.0, 56.8], _op) and not is_over(1696.0, [0.0, 66.0], _op),
      "2в: f_max — по частотам в 40–65 Гц")
check(not is_over(1575.0, [56.0], _op) and is_over(1575.1, [56.0], _op) and is_over(1576.0, [52.6], _op)
      and not is_over(1696.0, [], _op),
      "2в: is_over — обороты строго выше порога 1575, без частоты — нет")
# Строка частоты пришла позже: на живом краю отсчёт ещё не подтверждён, со строкой — подтверждён
_m_edge = marks2([(0, 1606.0)], [(-200, 0.0)])
_m_full = marks2([(0, 1606.0)], [(-200, 0.0), (1, 53.6)])
check(_m_edge[0][2] is False and _m_full[0][2] is True,
      "2в: подтверждение только добавляется (строки опроса поднимают максимум), не отзывается")

# ══ 3. Эпизод RPM_OVERSPEED (п.2) ═══════════════════════════════════════════
# ДГУ №2 15.10 12:42 МСК — отсчёты из БД (history_rich): 40068 обороты,
# 40044 частота, 40595 задание; 40011: 0 → 3 (12:42:09) → 6 (12:43:29) → 5
_D2 = [  # (ч, мин, с, обороты, частота, задание)
    (12, 42, 9, 197.0, None, 50.0), (12, 42, 19, 1606.0, None, 46.7), (12, 42, 20, None, 53.6, None),
    (12, 42, 29, 1551.0, 51.6, 30.0), (12, 42, 39, None, None, 42.266), (12, 42, 40, 1310.0, 44.0, None),
    (12, 42, 49, 1569.0, 52.3, 46.5), (12, 42, 59, 1563.0, None, 48.2), (12, 43, 0, None, 52.1, None),
    (12, 43, 9, 1450.0, 48.7, 49.466), (12, 43, 19, None, None, 49.3), (12, 43, 20, 1538.0, 51.4, None),
    (12, 43, 29, 1500.0, 50.4, 50.166), (12, 43, 39, 913.0, None, 30.0), (12, 43, 40, None, 0.0, None),
    (12, 43, 49, 898.0, None, None), (12, 44, 0, 901.0, None, None), (12, 44, 9, 897.0, None, None),
    (12, 44, 20, 901.0, None, None), (12, 44, 29, 900.0, None, None),
]


def M15(hh, mm, ss=0):
    return M(15, hh, mm, ss)


_rows3 = pings(M15(12, 36, 9), M15(12, 45, 0)) + [h(FREQ, M15(12, 38, 0), 0.0), h(RPM, M15(12, 41, 29), 0.0)]
for _hh, _mm, _ss, _r, _f, _z in _D2:
    for _a, _v in ((RPM, _r), (FREQ, _f), (FREF, _z)):
        if _v is not None:
            _rows3.append(h(_a, M15(_hh, _mm, _ss), _v))
_rows3.sort(key=lambda r: r["ts"])
_ENUM3 = [rs(0, M15(10, 38), M15(12, 42, 9)), rs(3, M15(12, 42, 9), M15(12, 43, 29)),
          rs(6, M15(12, 43, 29), M15(12, 43, 40)), rs(5, M15(12, 43, 40), M15(12, 45, 9))]


def ospd(segs):
    return [d for _, d in dets(segs, "RPM_OVERSPEED")]


def clusters_of(dd):
    out = {}
    for d in dd:
        for c in (d.values.get("live") or {}).get("clusters") or []:
            out[c["start"]] = c
    return [out[k] for k in sorted(out)]


# ── 3а. Порог KB 1575 (В2′): один эпизод 12:42:19–12:42:29, один участок ──
# 1569/52,3 (12:42:49) и 1563/52,1 (12:42:59) — ниже 1575: второго участка нет
_segs3 = seg3(_rows3, _ENUM3, cfg=CFG, a=M15(12, 40), b=M15(12, 45))
_o3 = ospd(_segs3)
_cl3 = clusters_of(_o3)
check(len(_cl3) == 1 and _cl3[0]["start"] == iso(M15(12, 42, 19)) and _cl3[0]["end"] == iso(M15(12, 42, 29))
      and _cl3[0]["spans"] == 1 and _cl3[0]["vmax"] == 1606.0 and not _cl3[0]["ongoing"],
      f"3а: ДГУ №2 15.10: один кластер 12:42:19–12:42:29 МСК, один участок, макс. 1606: {_cl3}")
check(_o3 and all(d.t_detected == iso(M15(12, 42, 19)) for d in _o3),
      f"3а: начало эпизода — первое нарушение 12:42:19: {[d.t_detected for d in _o3]}")
_d3 = max(_o3, key=lambda d: d.values["rpm_max"]) if _o3 else None
check(_d3 is not None and _d3.values["rpm_max"] == 1606.0 and _d3.values["freq_hz_max"] == 53.6
      and _d3.values["freq_rpm_max"] == 1608.0 and _d3.values["threshold_rpm"] == 1575.0
      and _d3.values["proximity_pct"] == 8.69565 and _d3.values["front_count"] == 1
      and _d3.values["overspeed_rpm"] == 1725.0 and _d3.values["margin_rpm"] == 119.0,
      f"3а: поля детекции (rpm_max, freq_rpm_max, threshold, overspeed, margin): {_d3 and _d3.values}")
check(_d3 is not None and _d3.severity == "CAUTION" and _d3.fault_codes == [] and _d3.ref_codes == [234]
      and _d3.source == "METRIC_RULE" and _d3.related_roles == ["RPM", "FREQUENCY"],
      f"3а: CAUTION, код 234 справочный (панель его не выставляла): {_d3 and (_d3.severity, _d3.fault_codes, _d3.ref_codes)}")
check(_d3 is not None and _d3.trigger == ("Обороты макс. 1606 об/мин (53,6 Гц, по частоте ≈ 1608) — "
                                          "выше порога 1575 (91,3 % уставки разноса 1725), до уставки осталось 119, "
                                          "фронтов: 1"),
      f"3а: текст срабатывания: {_d3 and _d3.trigger}")
check(dets(_segs3, "RPM_UNDERSPEED"), "3а: просадка 1310 при 44 Гц того же пуска — по-прежнему есть")
check(all(not sub.transient for s in _segs3 for sub in s.subsegments
          for d in sub.detections if d.scenario == "RPM_OVERSPEED"),
      "3а: заброс — только в рабочих подсегментах (не в разгоне)")

# ── 3б. (для сравнения) Прежний порог 1552,5 (proximity_pct 10): 12:42:19–12:43:09, два участка ──
_o3b = ospd(seg3(_rows3, _ENUM3, cfg=cfg_det(CFG, "RPM_OVERSPEED", proximity_pct=10), a=M15(12, 40), b=M15(12, 45)))
_cl3b = clusters_of(_o3b)
check(len(_cl3b) == 1 and _cl3b[0]["start"] == iso(M15(12, 42, 19)) and _cl3b[0]["end"] == iso(M15(12, 43, 9))
      and _cl3b[0]["spans"] == 2,
      f"3б: при 1552,5 — эпизод 12:42:19–12:43:09, два участка (1569, 1563 — заброс): {_cl3b}")
_d3b = max(_o3b, key=lambda d: d.values["rpm_max"]) if _o3b else None
check(_d3b is not None and "выше порога 1552,5 (90 % уставки разноса 1725)" in _d3b.trigger,
      f"3б: текст при 1552,5 — «1552,5 (90 % …)»: {_d3b and _d3b.trigger}")

# ── 3в. ДГУ №1 16.10 #35929: 1696/56,8 с 09:26:03 по 09:26:12, до уставки 29 ──
_rows29s = sorted(pings(M(16, 9, 20, 3), M(16, 9, 27, 0)) + _rows29, key=lambda r: r["ts"])
_segs29 = seg3(_rows29s, [rs(0, M(16, 3, 45, 32), M(16, 9, 25, 52)), rs(3, M(16, 9, 25, 52), STOP29),
                          rs(0, STOP29, M(16, 9, 27, 23))], cfg=CFG, a=M(16, 9, 20), b=M(16, 9, 27))
_o29 = ospd(_segs29)
_cl29 = clusters_of(_o29)
check(len(_cl29) == 1 and _cl29[0]["start"] == iso(M(16, 9, 26, 3)) and _cl29[0]["end"] == iso(M(16, 9, 26, 12))
      and _o29[0].values["rpm_max"] == 1696.0 and _o29[0].values["freq_hz_max"] == 56.8
      and _o29[0].values["margin_rpm"] == 29.0,
      f"3в: #35929 — заброс 1696 (56,8 Гц) 09:26:03–09:26:12, до уставки 29: "
      f"{_cl29} {_o29 and _o29[0].values.get('margin_rpm')}")

# ── 3г. CAUTION через cap_severity; код 234 — в fault_codes только от панели ──
check(cap_severity("RPM_OVERSPEED", "SHUTDOWN") == "CAUTION" and cap_severity("RPM_OVERSPEED", "WARNING") == "CAUTION",
      "3г: cap_severity — аналитика не выше CAUTION")
_sp3 = segment_spans(by_addr(_rows3), None, CFG, M15(12, 42, 19), M15(12, 43, 29), [], {}, False)
_A3, _B3 = M15(12, 42, 19), M15(12, 43, 29)
CH3 = {"RPM": {"min": 1310.0, "max": 1606.0, "median": 1551.0}}


def run3(live, cfg=CFG, run_state=3, sp=None, chars=CH3):
    return [d for d in run_all_detectors(chars, DerivedMetrics(), RiskAccumulators(), [], "NA", run_state,
                                         _A3, _B3, None, cfg, state_periods=sp, live_spans=live)
            if d.scenario == "RPM_OVERSPEED"]


_hi = run3(_sp3, cfg=cfg_det(CFG, "RPM_OVERSPEED", severity_default="SHUTDOWN"))
check(len(_hi) == 1 and _hi[0].severity == "CAUTION", f"3г: KB с SHUTDOWN — детекция всё равно CAUTION: {_hi}")
_lfc = CFG.role_to_addr("LAST_FAULT_CODE")
_pan = run3(_sp3, sp={_lfc: [{"value": 234, "state_start": M15(12, 43, 0), "state_end": None}]})
check(len(_pan) == 1 and _pan[0].fault_codes == [234] and _pan[0].ref_codes == [234],
      f"3г: панель выставила 234 в подсегменте — код в fault_codes: {[(d.fault_codes, d.ref_codes) for d in _pan]}")

# ── 3д. Без участков детекции нет ──
check(run3(None) == [], "3д: live_spans=None (расчёт без участков) — заброса нет (по максимуму подсегмента не ловим)")
check(run3(dict(_sp3, RPM_OVERSPEED=None)) == [], "3д: сбой ключа (None) — заброса нет")
check(run3({k: v for k, v in _sp3.items() if k != "RPM_OVERSPEED"}) == [], "3д: ключа нет — заброса нет")
check(run3(_sp3, run_state=1) == [] and run3(_sp3, cfg=cfg_det(CFG, "RPM_OVERSPEED", enabled=False)) == [],
      "3д: не RUN_STATE=3 или детектор выключен в KB — заброса нет")
check(len(run3(_sp3)) == 1, "3д: (для сравнения) по участкам — есть")
check("RPM_OVERSPEED" not in segment_spans(by_addr(_rows3), None, cfg_thr(CFG, ...), _A3, _B3, [], {}, False),
      "3д: уставки разноса в KB нет — ключа участков нет")
# Подсегмент, который участок заброса не пересекает (после 12:43:09), — без детекции
check(_detect_rpm_overspeed(M15(12, 43, 10), CFG, live=_sp3.get("RPM_OVERSPEED"), seg_end=_B3) == [],
      "3д: подсегмент без участка заброса — детекции нет")

# ── 3е. Изоляция: сбой ключа заброса и его детектора ──
_sp_rows = by_addr(_rows3)
_base3 = segment_spans(_sp_rows, None, CFG, _A3, _B3, [], {}, False)
check(_base3.get("RPM_OVERSPEED") and _base3.get("RPM_UNDERSPEED"), "3е: без сбоев — участки заброса и просадки")
for _fn in ("_over_points", "over_marks", "overspeed_params"):
    _orig = getattr(_lv, _fn)
    setattr(_lv, _fn, _boom)
    LOG.records.clear()
    try:
        _r = segment_spans(_sp_rows, None, CFG, _A3, _B3, [], {}, False)
        check("RPM_OVERSPEED" in _r and _r["RPM_OVERSPEED"] is None
              and _r.get("RPM_UNDERSPEED") == _base3["RPM_UNDERSPEED"] and logged("Участки ключа RPM_OVERSPEED"),
              f"3е: сбой {_fn} — ключ заброса None, просадка цела, запись в лог")
    except Exception as e:  # noqa: BLE001
        check(False, f"3е: сбой {_fn} уронил segment_spans: {e!r}")
    finally:
        setattr(_lv, _fn, _orig)
import analytics.detectors as _dt  # noqa: E402
_orig = _dt._detect_rpm_overspeed
_dt._detect_rpm_overspeed = _boom
LOG.records.clear()
try:
    _all3 = run_all_detectors(CH3, DerivedMetrics(), RiskAccumulators(), [], "NA", 3, _A3, _B3, None, CFG,
                              live_spans=_sp3)
    check(not any(d.scenario == "RPM_OVERSPEED" for d in _all3)
          and any(d.scenario == "RPM_UNDERSPEED" and "live" in d.values for d in _all3)
          and logged("Заброс оборотов"),
          "3е: сбой детектора заброса — просадка по участкам, запись в лог, подсегмент не падает")
except Exception as e:  # noqa: BLE001
    check(False, f"3е: сбой детектора заброса уронил run_all_detectors: {e!r}")
finally:
    _dt._detect_rpm_overspeed = _orig

# ── 3ж. Тексты: имя, худшее значение, запас до уставки разноса ──
check(SCENARIO_RU.get("RPM_OVERSPEED") == "Заброс оборотов", "3ж: имя сценария «Заброс оборотов»")
_w = _det_worst([(1, {"scenario": "RPM_OVERSPEED", "values": {"rpm_max": 1584.0}}),
                 (2, {"scenario": "RPM_OVERSPEED", "values": {"rpm_max": 1696.0}}),
                 (3, {"scenario": "RPM_OVERSPEED", "values": {"rpm_max": 1569.0}})])
check(_w is not None and _w[0] == 2, f"3ж: худшее — максимум оборотов: {_w}")
check(_group_worst("RPM_OVERSPEED", [{"rpm_max": 1584.0, "overspeed_rpm": 1725.0},
                                     {"rpm_max": 1696.0, "overspeed_rpm": 1725.0}])
      == "макс. 1696 об/мин — до уставки разноса 1725 осталось 29",
      f"3ж: худшее за период — запас до уставки разноса: "
      f"{_group_worst('RPM_OVERSPEED', [{'rpm_max': 1696.0, 'overspeed_rpm': 1725.0}])}")
check(_group_worst("RPM_OVERSPEED", [{"rpm_max": 1740.0, "overspeed_rpm": 1725.0}])
      == "макс. 1740 об/мин — выше уставки разноса 1725 на 15", "3ж: выше уставки — «выше … на N»")
_hier = _fmt_detections_hierarchy({"subsegments": [{"duration_sec": 60, "detections": [_d3.to_dict()]}]}) if _d3 else ""
check("Заброс оборотов" in _hier and "макс. 1606 об/мин — до уставки разноса 1725 осталось 119" in _hier,
      f"3ж: иерархия обнаружений (заключение) — имя и запас: {_hier!r}")
_md3 = to_markdown(_segs3, "SN", "pcc", 2, M15(12, 40), M15(12, 45), tz=MSK)
check("**Заброс оборотов** (CAUTION)" in _md3 and "Обороты макс. 1606 об/мин" in _md3
      and "Нарушение: 15.10 12:42:19–12:42:29" in _md3 and "1575,0" not in _md3 and "1552,5" not in _md3,
      "3ж: отчёт — пункт «Заброс оборотов», текст и строка нарушения МСК")

# ══ 4. Профиль пуска (п.3, В1 — вариант «б») ════════════════════════════════
import json  # noqa: E402

import analytics.labels as _lbl  # noqa: E402
import analytics.segmenter as _sg  # noqa: E402
from analytics.labels import START_SCENARIOS, start_note_text, start_profile_text, start_window  # noqa: E402
from analytics.segmenter import start_profile, start_profile_params  # noqa: E402
from analytics.transitions import Link  # noqa: E402

KW, CB = CFG.role_to_addr("ACTIVE_POWER_TOTAL"), CFG.role_to_addr("GENSET_CB_POS")
OIL1 = CFG.role_to_addr("OIL_TEMP")


def MM(mo, d, hh, mm, ss=0):
    """Время МСК любого месяца → UTC."""
    return datetime(2026, mo, d, hh, mm, ss, tzinfo=MSK).astimezone(U)


def start_rows(t0, pts, pre=(), tail=None, b=600, a=-600):
    """Строки пуска: pts — [(с от t0, обороты, Гц, задание, кВт)] (None — строки
    нет), pre — [(с, адрес, значение)], tail — (с, обороты, Гц): дальше каждые
    10 с до конца окна, кроме меток, уже заданных в pts."""
    rows = pings(S(t0, a - 300), S(t0, b))
    rows += [h(ad, S(t0, s), v) for s, ad, v in pre]
    have = set()
    for s, r, f, z, k in pts:
        for ad, v in ((RPM, r), (FREQ, f), (FREF, z), (KW, k)):
            if v is not None:
                rows.append(h(ad, S(t0, s), v))
                have.add((ad, s))
    if tail is not None:
        s, r, f = tail
        k = 0
        while s < b:
            for ad, v in ((RPM, r + k % 2), (FREQ, f)):
                if (ad, s) not in have:
                    rows.append(h(ad, S(t0, s), v))
            s += 10
            k += 1
    return sorted(rows, key=lambda r: r["ts"])


def start_case(t0, pts, rs_seq, cb_at=None, pre=(), tail=None, cfg=CFG, a=-600, b=600,
               live_edge=False, ctx=None, hist_from=None, seed=None, carry=None):
    """Пуск в окне [t0 + a, t0 + b): rs_seq — [(с от t0, 40011)], cb_at — замыкание
    автомата 43562 (с от t0). hist_from — история окна только с t0 + hist_from
    (преамбула живого движка, ревью R7), seed — посев удержанных значений перед
    окном (load_seed), carry — профиль пуска предка через суточный рез (R1)."""
    enum = [rs(v, S(t0, s), S(t0, rs_seq[i + 1][0]) if i + 1 < len(rs_seq) else None)
            for i, (s, v) in enumerate(rs_seq)]
    if cb_at is not None:
        enum += [{"addr": CB, "value": 0, "state_start": S(t0, -7200), "state_end": S(t0, cb_at)},
                 {"addr": CB, "value": 1, "state_start": S(t0, cb_at), "state_end": None}]
    rows = start_rows(t0, pts, pre, tail, b, a)
    if hist_from is not None:
        rows = [r for r in rows if r["ts"] >= S(t0, hist_from)]
    return segment(enum, rows, [], [], cfg, "SN", "pcc", 1, "E",
                   S(t0, a), S(t0, b), transition_ctx=ctx, live_edge=live_edge, load_seed=seed,
                   start_carry=carry)


def prof_of(segs):
    """Профиль пуска окна: один — словарь, нет — None, несколько — список."""
    pp = [s.start_profile for s in segs if s.start_profile]
    return pp[0] if len(pp) == 1 else (pp or None)


def why(sp):
    return set((sp or {}).get("reasons") or []) if isinstance(sp, dict) else {"нет профиля"}


def cfg_seg(base, sec, **kw):
    """Копия конфига с правкой секции сегментации."""
    c = copy.copy(base)
    c.segmentation = dict(base.segmentation, **{sec: dict(base.segmentation.get(sec) or {}, **kw)})
    return c


# ── 4а. Пороги из KB: секция start_profile, номинал и «схватил» — общие ──
_spp = start_profile_params(CFG)
check(_spp["after_rated_sec"] == 300 and _spp["rundown_rpm"] == 1000 and _spp["fref_low_hz"] == 48.5
      and _spp["fref_high_hz"] == 51.5 and _spp["unstable_swing_sec"] == 30 and _spp["unstable_flips"] == 2
      and _spp["swing_out_hz"] == 1.0 and _spp["swing_in_hz"] == 0.5 and _spp["swing_rpm_tol"] == 30,
      f"4а: пороги профиля из KB (В1): {_spp}")
check(_spp["rpm_min"] == 250 and _spp["freq_min"] == 5 and _spp["rated_rpm"] == 1400
      and _spp["rated_freq"] == 46.5 and _spp["nominal_rpm"] == 1500 and _spp["nominal_hz"] == 50,
      "4а: «схватил» и выход на номинал — пороги transition_confirm, номинал — RPM_UNDERSPEED / LOAD_STEP")
check(start_profile_params(CFG3) == _spp and start_profile_params(MONO) == _spp,
      "4а: пороги одинаковы у KTA50, QSX15 и монолита (секция в _defaults и в монолите)")
# Ревью R12: умолчания в коде равны значениям KB — что ключ читается, видно только
# по смене значения в KB; и набор ключей секции KB = набор ключей, которые читает код
_kb_sp = {k: v for k, v in (CFG.segmentation.get("start_profile") or {}).items() if not k.startswith("_")}
_code_sp = set(re.findall(r'\bg\("(\w+)"', inspect.getsource(start_profile_params)))
check(set(_kb_sp) == _code_sp, f"4а: ключи секции start_profile KB = ключи, которые читает код: "
      f"лишние в KB {sorted(set(_kb_sp) - _code_sp)}, нет в KB {sorted(_code_sp - set(_kb_sp))}")
_unread = []
for _k, _v in _kb_sp.items():
    _new = (not _v) if isinstance(_v, bool) else (_v + 1 if isinstance(_v, int) else _v + 7.25)
    _got = start_profile_params(cfg_seg(CFG, "start_profile", **{_k: _new})).get(_k)
    if _got != _new:
        _unread.append((_k, _new, _got))
check(_kb_sp and not _unread, f"4а: каждый ключ start_profile KB меняет параметр профиля: {_unread}")

# ── 4б. Пограничные пуски hunt.out — устойчивы (кроме 15.10 05:42 по «б») ──
# ДГУ №2 27.09 17:24:10 — одиночный отсчёт 1426 / 47,7 Гц, 9 с
T_A = MM(9, 27, 17, 24, 10)
_pA = prof_of(start_case(T_A, [
    (-1, None, None, 50.0, None), (0, 1426.0, 47.7, None, None), (9, 1499.0, 50.0, None, None),
    (20, 1501.0, None, None, None), (23, None, None, None, 0.0), (29, 1499.0, None, None, None),
    (40, None, 49.9, None, None), (49, 1500.0, 50.1, None, None), (60, None, 50.0, None, None),
    (69, 1499.0, None, None, None), (79, None, None, 50.033, None), (80, 1502.0, 50.1, None, None),
    (89, 1498.0, 50.0, 49.966, None), (100, 1500.0, None, None, None), (109, None, 50.1, 50.0, None),
    (119, None, None, 49.966, None), (120, None, 50.0, None, None), (129, None, None, 50.0, None),
    (149, 1499.0, 49.9, 50.033, None), (160, None, 50.0, None, None), (169, 1500.0, 49.9, 50.166, 1.0)],
    [(-500, 0), (0, 3)], cb_at=169, pre=[(-11, RPM, 0.0), (-291, FREQ, 0.0), (-240, FREF, 0.0), (-240, OIL1, 66.0)],
    tail=(180, 1499.0, 50.0)))
check(isinstance(_pA, dict) and _pA["rated_sec"] == 0.0 and _pA["swing_sec"] == 9.0 and _pA["flips"] == 0
      and _pA["end_by"] == "breaker" and not _pA["unstable"] and _pA["fref"]["range"] == [49.966, 50.033]
      and _pA["kw_max"] == 0.0 and _pA["oil_temp"] == 66.0,
      f"4б: 27.09 17:24 — раскачка 9 с без смен знака, автомат 17:26:59 — устойчив: {_pA}")
# ДГУ №2 15.10 13:00:20 — частота 49,0–51,1 Гц, одна смена знака, задание 49,5–50,6
T_B = M(15, 13, 0, 20)
_pB = prof_of(start_case(T_B, [
    (-1, None, None, 0.0, 0.0), (9, 1472.0, 49.5, 49.5, 0.0), (19, 1526.0, None, 50.633, None),
    (20, None, 51.1, None, None), (29, 1488.0, 49.0, 50.3, None), (39, 1499.0, None, 49.866, None),
    (40, None, 50.0, None, None), (49, 1500.0, 50.1, 49.966, None), (59, 1499.0, None, None, None),
    (60, None, 50.0, None, None), (69, 1501.0, None, 50.033, None), (79, 1500.0, None, 50.0, None),
    (89, 1499.0, None, 49.966, None), (99, 1500.0, None, 50.0, None), (109, 1498.0, 49.9, 49.933, 0.0),
    (189, None, None, 50.066, 344.0)],
    [(-500, 0), (0, 3)], cb_at=189, pre=[(-51, RPM, 0.0), (-100, FREQ, 0.0), (-300, OIL1, 43.0)],
    tail=(119, 1500.0, 50.0)))
check(isinstance(_pB, dict) and _pB["rated_sec"] == 9.0 and _pB["swing_sec"] == 0.0 and _pB["flips"] == 1
      and not _pB["unstable"] and _pB["fref"]["range"] == [49.5, 50.633] and _pB["end_by"] == "breaker",
      f"4б: 15.10 13:00 — одна смена знака, 51,1 Гц при 1526 (в пределах ±30) — не раскачка, устойчив: {_pB}")
# ДГУ №2 20.09 16:34:50 — прогрев 5 мин на 900 об/мин, задание 45,9 на рампе до номинала
T_C = MM(9, 20, 16, 34, 50)
_ptsC = [(-1, None, None, 30.0, None), (0, 926.0, None, None, None)] + [
    (s, 900.0 + (s // 10) % 3, None, None, None) for s in range(9, 299, 10)] + [
    (299, None, None, 45.9, None), (300, 1390.0, 46.2, None, None), (309, 1499.0, 50.0, 50.0, None),
    (320, 1500.0, None, None, None), (329, None, 50.1, None, None), (340, None, 50.0, None, None),
    (349, 1499.0, None, None, None), (360, None, 49.9, None, None), (369, None, 50.0, None, None),
    (380, 1500.0, None, None, None), (399, None, None, 0.0, None), (400, 535.0, 0.0, None, None),
    (409, 0.0, None, None, None)]
_RS_C = [(-400, 0), (0, 2), (300, 3), (389, 6), (400, 0)]
_PRE_C = [(-71, RPM, 0.0), (-182, FREQ, 0.0), (-122, FREF, 0.0), (-191, OIL1, 15.0)]
_segsC = start_case(T_C, _ptsC, _RS_C, pre=_PRE_C)
_pC = prof_of(_segsC)
check(isinstance(_pC, dict) and _pC["crank_at"] == iso(T_C) and _pC["rated_sec"] == 309.0
      and _pC["end_by"] == "state_end" and not _pC["unstable"] and _pC["fref"]["range"] == [45.9, 50.0]
      and _pC["fref"]["low"] is None and _pC["fref"]["seq"] == [30.0, 45.9, 50.0],
      f"4б: 20.09 16:34 — «Работа» за прогревом: прокрутка с начала прогрева, номинал за 309 с, "
      f"45,9 Гц на рампе до номинала — не «ниже 48,5 после номинала»: {_pC}")
check([s.run_state for s in _segsC if s.start_profile] == [3],
      "4б: профиль — только у «Работы» (у прогрева нет)")
# ДГУ №1 25.09 13:48:33 — синхронизация, задание 50,73 на прокрутке
T_D = MM(9, 25, 13, 48, 33)
_pD = prof_of(start_case(T_D, [
    (-23, 0.0, None, None, 0.0), (0, 1493.0, 50.0, 50.733, None), (9, 1500.0, None, 50.0, None),
    (20, 1501.0, None, 49.966, None), (29, None, None, 50.0, None), (40, 1502.0, None, None, None),
    (49, 1500.0, None, None, None), (60, None, None, 49.966, 349.0), (69, 1498.0, 49.9, None, None)],
    [(-500, 0), (0, 3)], cb_at=60, pre=[(-300, FREQ, 0.0), (-294, FREF, 0.0), (-191, OIL1, 64.0)],
    tail=(80, 1500.0, 50.0)))
check(isinstance(_pD, dict) and _pD["rated_sec"] == 0.0 and not _pD["unstable"]
      and _pD["fref"]["range"] == [49.966, 50.733] and _pD["fref"]["high"] is None,
      f"4б: 25.09 13:48 — задание 50,73 Гц (≤ 51,5) — устойчив: {_pD}")
# ДГУ №2 15.10 05:42:20 — задание 52,2 Гц на прокрутке: по «б» неустойчив (первый предвестник)
T_E = M(15, 5, 42, 20)
_ptsE = [(-1, None, None, 52.2, None), (0, 1481.0, 49.6, None, 0.0), (9, 1483.0, 49.5, 49.933, None),
         (20, 1498.0, 49.9, None, None), (29, 1500.0, 50.0, 49.966, None), (39, None, None, 50.0, None),
         (40, 1502.0, 50.1, None, None), (49, 1501.0, 50.0, None, 319.0)]
_PRE_E = [(-71, FREF, 0.0), (-31, RPM, 0.0), (-131, FREQ, 0.0), (-211, OIL1, 58.0)]
_pE = prof_of(start_case(T_E, _ptsE, [(-500, 0), (0, 3)], cb_at=49, pre=_PRE_E, tail=(60, 1500.0, 50.0)))
check(isinstance(_pE, dict) and _pE["unstable"] and why(_pE) == {"fref_high"}
      and _pE["fref"]["high"] == {"v": 52.2, "t": iso(T_E), "stage": "crank", "rpm": None}
      and _pE["swing_sec"] == 0.0 and _pE["flips"] == 0,
      f"4б: 15.10 05:42 — задание 52,2 Гц на начало прокрутки — неустойчив (вариант «б»): {_pE}")
check("неустойчивый: на прокрутке задание 52,2 Гц" in (start_profile_text(_pE, MSK) or ""),
      f"4б: 15.10 05:42 — строка «Пуск»: {start_profile_text(_pE, MSK)}")
# По варианту «а» (выше 51,5 — только после «схватил») был бы устойчив: 52,2 — до него
_pEa = prof_of(start_case(T_E, [(-1, None, None, 50.0, None)] + _ptsE[1:], [(-500, 0), (0, 3)], cb_at=49,
                          pre=_PRE_E, tail=(60, 1500.0, 50.0)))
check(isinstance(_pEa, dict) and not _pEa["unstable"],
      "4б: (для сравнения) без 52,2 на прокрутке — тот же пуск устойчив")
# Холодный ДГУ №1 12.10 13:43:13 — масло 17 °C, окно до номинал + 5 мин
T_F = M(12, 13, 43, 13)
_segsF = start_case(T_F, [(-27, 0.0, None, None, 0.0), (-1, None, None, 50.0, None), (0, 1153.0, 38.5, None, None),
                          (9, 1501.0, 50.0, None, None)],
                    [(-500, 0), (0, 3)], pre=[(-207, OIL1, 17.0), (-387, FREQ, 0.0)], tail=(20, 1500.0, 50.0))
_pF = prof_of(_segsF)
check(isinstance(_pF, dict) and _pF["rated_sec"] == 9.0 and _pF["end_by"] == "rated_300"
      and _pF["t_to"] == iso(S(T_F, 309)) and _pF["oil_temp"] == 17.0 and not _pF["unstable"]
      and _pF["breaker_closed_at"] is None,
      f"4б: 12.10 13:43 (холодный, масло 17 °C) — окно до номинал + 5 мин, устойчив: {_pF}")
# Холодный ДГУ №1 29.09 06:08:03 — масло 16 °C, окно до конца режима (задание 43,1 на переходе — вне окна)
T_G = MM(9, 29, 6, 8, 3)
_pG = prof_of(start_case(T_G, [
    (-46, 0.0, None, None, None), (0, 149.0, None, 50.0, None), (9, 1500.0, 50.0, None, None),
    (20, 1501.0, 50.1, None, None), (29, 1499.0, 50.0, None, None), (129, None, 50.1, None, None),
    (149, 1287.0, 43.0, 43.1, None), (160, 892.0, 0.0, 30.0, 0.0), (169, 905.0, None, None, None),
    (240, 681.0, None, 0.0, None), (249, 0.0, None, None, None)],
    [(-500, 0), (0, 3), (149, 5), (240, 0)], pre=[(-211, OIL1, 16.0), (-196, FREQ, 0.0), (-196, FREF, 0.0)]))
check(isinstance(_pG, dict) and _pG["end_by"] == "state_end" and _pG["t_to"] == iso(S(T_G, 149))
      and _pG["fref"]["range"] == [50.0, 50.0] and _pG["oil_temp"] == 16.0 and not _pG["unstable"],
      f"4б: 29.09 06:08 (холодный, масло 16 °C) — окно до конца «Работы», устойчив: {_pG}")
# ДГУ №1 15.10 13:28:53 — 1275 об/мин за секунду до «Работы»: «схватил» на начале режима
T_H = M(15, 13, 28, 53)
_pH = prof_of(start_case(T_H, [
    (-21, 0.0, None, None, None), (-1, 1275.0, 42.5, 50.0, 0.0), (9, 1501.0, 50.0, 50.033, None),
    (19, 1499.0, None, 50.0, None), (29, 1498.0, None, 49.966, None), (39, 1499.0, None, 50.0, 378.0)],
    [(-500, 0), (0, 3)], cb_at=39, pre=[(-201, OIL1, 68.0), (-361, FREQ, 0.0), (-280, FREF, 0.0)],
    tail=(49, 1500.0, 50.0)))
check(isinstance(_pH, dict) and _pH["fire_at"] == iso(T_H) and _pH["rated_sec"] == 9.0
      and _pH["end_by"] == "breaker" and not _pH["unstable"],
      f"4б: 15.10 13:28 — «схватил» на начале режима, номинал за 9 с, автомат 13:29:32, устойчив: {_pH}")

# ── 4в. Реальные неустойчивые пуски (ряды из БД) ──
# ДГУ №2 15.10 12:42 (раздел 3): заброс 1606, раскачка ≈70 с, 4 смены знака, задание до 42,3
_p3 = prof_of(_segs3)
check(isinstance(_p3, dict) and why(_p3) == {"overspeed", "swing", "flips", "fref_low"}
      and _p3["swing_sec"] == 69.0 and _p3["flips"] == 4 and _p3["rated_sec"] == 10.0
      and _p3["overspeed"]["rpm"] == 1606.0 and _p3["overspeed"]["hz"] == 53.6
      and _p3["fref"]["low"] == {"v": 42.266, "t": iso(M15(12, 42, 39)), "rpm": 1310.0}
      and _p3["fref"]["range"] == [42.266, 50.0] and _p3["fref"]["range_rated"] == [42.266, 49.466]
      and _p3["end_by"] == "state_end" and _p3["t_to"] == iso(M15(12, 43, 29)),
      f"4в: ДГУ №2 15.10 12:42 — заброс, раскачка 69 с, 4 смены знака, задание до 42,3 после номинала: {_p3}")
# ДГУ №1 16.10 09:25:52 (#35929, раздел 3): заброс 1696 и задание 30,6 → 55 Гц
_p29 = prof_of(_segs29)
check(isinstance(_p29, dict) and why(_p29) == {"overspeed", "fref_high"}
      and _p29["fref"]["seq"] == [30.633, 55.0] and _p29["fref"]["high"]["v"] == 55.0
      and _p29["fref"]["high"]["stage"] == "rated" and _p29["fref"]["high"]["rpm"] == 1247.0
      and _p29["swing_sec"] == 20.0 and _p29["flips"] == 1,
      f"4в: #35929 — заброс 1696 и задание 55 Гц при 1247 об/мин: {_p29}")
check("задание 30,6 → 55 Гц" in (start_profile_text(_p29, MSK) or ""),
      f"4в: #35929 — «задание 30,6 → 55 Гц» в строке «Пуск»: {start_profile_text(_p29, MSK)}")
# ДГУ №1 16.10 09:28:33 (#35939): до номинала не вышел, на разгоне задание 55 Гц при 1294
T_L = M(16, 9, 28, 33)
_segsL = start_case(T_L, [(-21, 0.0, None, None, None), (-1, 203.0, None, 50.0, 0.0), (9, 1294.0, 42.6, 55.0, 0.0),
                          (19, 898.0, None, 0.0, None), (20, None, 0.0, None, None), (29, 260.0, None, None, None),
                          (39, 0.0, None, None, None)],
                    [(-31, 0), (0, 3), (20, 0)], pre=[(-60, FREQ, 0.0), (-31, FREF, 0.0), (-221, OIL1, 22.0)],
                    a=-50, b=120)
_pL = prof_of(_segsL)
check(isinstance(_pL, dict) and why(_pL) == {"fref_high"} and _pL["rated_at"] is None and _pL["rated_sec"] is None
      and _pL["fref"]["high"] == {"v": 55.0, "t": iso(S(T_L, 9)), "stage": "ramp", "rpm": 1294.0}
      and _pL["end_by"] == "state_end",
      f"4в: #35939 — на разгоне задание 55 Гц при 1294 об/мин: {_pL}")
_stL = start_profile_text(_pL, MSK) or ""
check(_stL.startswith("до номинала не вышел;") and _stL.endswith("— неустойчивый: на разгоне задание 55 Гц при 1294 об/мин"),
      f"4в: #35939 — строка «Пуск»: {_stL}")
# ДГУ №1 16.10 09:29:33 (#35944): заброс 1584, раскачка 1 мин 50 с, задание 48,6–50,7 Гц
T_J = M(16, 9, 29, 33)
_ptsJ = [(-31, 260.0, None, None, None), (-21, 0.0, None, None, None), (-1, 95.0, None, None, None),
         (9, 1584.0, 52.9, 49.9, 0.0), (19, 1512.0, 51.5, 50.366, None), (29, 1451.0, 48.5, 48.566, None),
         (39, 1526.0, 50.3, 48.966, None), (49, 1489.0, 49.7, 49.033, None), (59, 1514.0, 51.5, 50.733, None),
         (69, 1439.0, 48.1, 49.733, 0.0), (79, 1499.0, 51.3, 50.566, None), (89, 1466.0, 49.0, 49.533, None),
         (99, 1540.0, 51.3, 50.5, None), (109, 1525.0, 50.9, 50.1, None), (119, 1500.0, 50.0, 50.0, None),
         (129, 1498.0, None, 49.966, None), (139, 1499.0, None, None, None), (140, None, None, None, 0.0),
         (159, None, 50.1, None, 0.0), (169, None, 50.0, 50.0, None), (179, 1067.0, 35.3, 33.566, None),
         (189, 898.0, 0.0, 30.0, None), (199, 897.0, None, None, None), (209, 900.0, None, None, None),
         (269, 688.0, None, 0.0, 0.0), (279, 561.0, None, None, None)]
_segsJ = start_case(T_J, _ptsJ, [(-40, 0), (0, 3), (180, 5), (269, 0)],
                    pre=[(-40, FREQ, 0.0), (-41, FREF, 0.0), (-280, OIL1, 25.0)], a=-60, b=330)
_pJ = prof_of(_segsJ)
check(isinstance(_pJ, dict) and why(_pJ) == {"overspeed", "swing", "flips"} and _pJ["swing_sec"] == 110.0
      and _pJ["flips"] == 6 and _pJ["end_by"] == "rundown" and _pJ["t_to"] == iso(S(T_J, 179))
      and _pJ["fref"]["range"] == [48.566, 50.733] and _pJ["overspeed"]["rpm"] == 1584.0
      and _pJ["rpm"] == [1439.0, 1584.0] and _pJ["freq"] == [48.1, 52.9] and _pJ["oil_temp"] == 25.0,
      f"4в: #35944 — заброс 1584, раскачка 1 мин 50 с, 6 смен знака, окно до сброса оборотов 09:32:32: {_pJ}")
check(all(p["overspeed"]["threshold_rpm"] == 1575.0 for p in (_p3, _p29, _pJ)),
      f"4в: порог заброса в профилях 15.10 и 16.10 — 1575 (В2′, без хвоста дроби): "
      f"{[p['overspeed']['threshold_rpm'] for p in (_p3, _p29, _pJ)]}")
check(start_note_text(_pJ, MSK) == "на пуске: раскачка 1 мин 50 с, 6 смен знака, задание 48,6–50,7 Гц, "
                                   "автомат разомкнут, 0 кВт, пуск неустойчивый",
      f"4в: #35944 — пометка «на пуске»: {start_note_text(_pJ, MSK)}")
# ДГУ №1 16.10 09:57:55 (#35949): раскачка 40 с, 2 смены знака, задание 49,4–50,5 Гц
T_M = M(16, 9, 57, 55)
_ptsM = [(-23, 0.0, None, None, 0.0), (-1, 242.0, None, None, None), (7, 1498.0, 50.0, 49.666, 0.0),
         (17, 1465.0, 48.3, 49.433, None), (27, 1540.0, 51.3, 50.533, None), (37, 1523.0, 50.4, 49.9, None),
         (47, 1464.0, 48.7, 50.033, None), (57, 1499.0, 50.0, 49.933, None), (67, None, None, 49.9, None),
         (77, 1500.0, None, 50.0, 0.0), (87, 1501.0, 50.1, 50.033, None), (97, 1500.0, 50.0, 50.0, None),
         (107, 1499.0, None, 49.966, None), (117, 1498.0, None, None, None), (127, 1499.0, None, 49.933, None),
         (137, 1501.0, 50.1, 50.033, 381.0)]
_PRE_M = [(-201, OIL1, 37.0), (-480, FREQ, 0.0), (-503, FREF, 0.0)]
_segsM = start_case(T_M, _ptsM, [(-500, 0), (0, 3)], cb_at=137, pre=_PRE_M, tail=(147, 1500.0, 50.0))
_pM = prof_of(_segsM)
check(isinstance(_pM, dict) and why(_pM) == {"swing", "flips"} and _pM["swing_sec"] == 40.0 and _pM["flips"] == 2
      and _pM["fref"]["range"] == [49.433, 50.533] and _pM["end_by"] == "breaker" and _pM["rated_sec"] == 7.0,
      f"4в: #35949 — раскачка 40 с, 2 смены знака, задание 49,4–50,5 Гц: {_pM}")

# ── 4г. Границы правил варианта «б» ──
T_Q = M(14, 10, 0, 0)
_BASE_Q = [(-27, 0.0, None, None, 0.0), (0, 1153.0, 38.5, None, None), (9, 1501.0, 50.0, None, None)]


def q(extra=(), pre=(), rs_seq=((-500, 0), (0, 3)), **kw):
    """Чистый пуск 14.10 10:00 (номинал за 9 с) с поправками; профиль."""
    pts = {(s): (s, r, f, z, k) for s, r, f, z, k in _BASE_Q}
    for p in extra:
        pts[p[0]] = p
    return prof_of(start_case(T_Q, sorted(pts.values()), list(rs_seq),
                              pre=[(-200, OIL1, 40.0), (-300, FREQ, 0.0)] + list(pre),
                              tail=(20, 1500.0, 50.0), **kw))


_q0 = q()
check(isinstance(_q0, dict) and not _q0["unstable"] and _q0["reasons"] == [], f"4г: чистый пуск — устойчив: {_q0}")
check(not q(pre=[(-1, FREF, 51.5)])["unstable"] and why(q(pre=[(-1, FREF, 51.6)])) == {"fref_high"},
      "4г: задание на прокрутке 51,5 — не выше порога, 51,6 — неустойчив")
_qr = q(extra=[(5, None, None, 51.6, None)], pre=[(-1, FREF, 50.0)])
check(why(_qr) == {"fref_high"} and _qr["fref"]["high"]["stage"] == "ramp",
      f"4г: 51,6 между «схватил» и номиналом — «на разгоне»: {_qr and _qr['fref']}")
check(not q(extra=[(40, None, None, 48.5, None)], pre=[(-1, FREF, 50.0)])["unstable"]
      and why(q(extra=[(40, None, None, 48.4, None)], pre=[(-1, FREF, 50.0)])) == {"fref_low"},
      "4г: задание после номинала 48,5 — не ниже порога, 48,4 — неустойчив")
check(not q(extra=[(5, None, None, 48.4, None), (9, 1501.0, 50.0, 50.0, None)], pre=[(-1, FREF, 30.0)])["unstable"],
      "4г: 48,4 на разгоне (до номинала) — не «после номинала» (20.09 16:34)")
check(not q(extra=[(40, None, None, 30.0, None)], pre=[(-1, FREF, 50.0)])["unstable"],
      "4г: задание 30 Гц (холостой ход) — ниже 40, не в счёт")
# Раскачка: выход за ±1 Гц, подтверждённый оборотами того же знака дальше ±30; конец — вход в ±0,5
_out = [(30, 1460.0, 48.8, None, None), (40, 1460.0, 48.8, None, None), (50, 1460.0, 48.8, None, None)]
_q29 = q(extra=_out + [(59, 1500.0, 50.0, None, None)])
_q30 = q(extra=_out + [(60, 1500.0, 50.0, None, None)])
check(_q29["swing_sec"] == 29.0 and not _q29["unstable"] and _q30["swing_sec"] == 30.0 and why(_q30) == {"swing"},
      f"4г: раскачка 29 с — устойчив, 30 с — неустойчив: {_q29['swing_sec']} / {_q30['swing_sec']}")
_qn = q(extra=[(30, 1490.0, 48.8, None, None), (40, 1540.0, 48.8, None, None), (50, 1500.0, 50.0, None, None)])
check(_qn["swing_sec"] == 0.0, f"4г: частота без оборотов того же знака дальше ±30 — не раскачка: {_qn['swing_sec']}")
_qf1 = q(extra=[(30, None, 50.6, None, None), (40, None, 49.4, None, None)])
_qf2 = q(extra=[(30, None, 50.6, None, None), (40, None, 49.4, None, None), (50, None, 50.6, None, None)])
check(_qf1["flips"] == 1 and not _qf1["unstable"] and _qf2["flips"] == 2 and why(_qf2) == {"flips"}
      and _qf2["swing_sec"] == 0.0,
      f"4г: одна смена знака — устойчив, две — неустойчив (±0,6 Гц — не раскачка): {_qf1['flips']} / {_qf2['flips']}")
# Окно: после номинал + 5 мин, замыкания автомата и выбега раскачка не считается
_late = [(330, 1460.0, 48.6, None, None), (340, 1540.0, 51.4, None, None), (350, 1460.0, 48.6, None, None),
         (360, 1500.0, 50.0, None, None)]
_qw = q(extra=_late)
check(_qw["end_by"] == "rated_300" and _qw["flips"] == 0 and not _qw["unstable"],
      f"4г: раскачка позже номинал + 5 мин — вне окна: {_qw['end_by']}, {_qw['flips']}")
_qb = q(extra=[(30 + s, *x[1:]) for s, x in ((0, _late[0]), (10, _late[1]), (20, _late[2]), (30, _late[3]))]
        + [(25, None, None, None, 120.0)], cb_at=25)
check(_qb["end_by"] == "breaker" and _qb["breaker_closed_at"] == iso(S(T_Q, 25)) and _qb["flips"] == 0
      and not _qb["unstable"] and "автомат замкнут 10:00:25" in start_profile_text(_qb, MSK),
      f"4г: после замыкания автомата раскачка — вне окна (это уже нагрузка): {_qb['end_by']}, {_qb['flips']}")
_qd = q(extra=[(30, 900.0, 30.0, None, None), (40, 1460.0, 48.6, None, None), (50, 1540.0, 51.4, None, None),
               (60, 1460.0, 48.6, None, None)])
check(_qd["end_by"] == "rundown" and _qd["t_to"] == iso(S(T_Q, 30)) and _qd["flips"] == 0,
      f"4г: первый отсчёт ниже 1000 после номинала — конец окна: {_qd['end_by']}, {_qd['t_to']}")
_qk = q(extra=[(15, None, None, None, 0.0), (35, None, None, None, 345.0)])
check(_qk["kw_max"] == 345.0 and "до 345 кВт" in start_profile_text(_qk, MSK),
      f"4г: нагрузка в окне — «до 345 кВт»: {_qk['kw_max']}")

# ── 4д. Не пуск: суточный рез, работа после охлаждения, попытка, выключатель KB ──
check(q(rs_seq=((-2000, 0), (-1000, 3))) is None,
      "4д: «Работа» началась до окна (суточный рез, REPORT_START) — профиля нет, окно через рез не дописывается")
check(q(rs_seq=((-500, 3), (-200, 5), (0, 3))) is None,
      "4д: «Работа» после охлаждения без стопа (5 → 3) — не пуск, профиля нет")
_qcut = q(b=100)
check(isinstance(_qcut, dict) and _qcut["end_by"] == "state_end" and _qcut["t_to"] == iso(S(T_Q, 100))
      and not _qcut["open"],
      f"4д: пуск за 100 с до суточного реза — окно до реза, не «идёт»: {_qcut and _qcut['end_by']}")
_segsAt = start_case(T_Q, [(-27, 0.0, None, None, 0.0), (0, 171.0, None, 50.0, 0.0), (9, 189.0, None, None, None),
                           (20, 0.0, None, 0.0, None)], [(-500, 0), (0, 3), (20, 0)],
                     pre=[(-300, FREQ, 0.0), (-200, OIL1, 40.0)], b=300)
check(prof_of(_segsAt) is None and any(s.attempt for s in _segsAt),
      "4д: неудачная попытка (прокрутка до 189 об/мин) — плашка, профиля нет")
check(q(cfg=cfg_seg(CFG, "start_profile", enabled=False)) is None, "4д: start_profile.enabled: false — профиля нет")
check(q(cfg=cfg_seg(CFG, "start_profile", unstable_swing_sec=20))["unstable"] is False
      and why(q(extra=_out + [(55, 1500.0, 50.0, None, None)], cfg=cfg_seg(CFG, "start_profile", unstable_swing_sec=20)))
      == {"swing"}, "4д: порог раскачки — из KB (20 с: раскачка 25 с — неустойчив)")

# ── 4е. Живой движок: прогрев закрыт прошлым окном (контекст), окно идёт ──
_ctxC = [Link(0, S(T_C, -400), T_C), Link(2, T_C, S(T_C, 300))]
_pCl = prof_of(start_case(T_C, _ptsC, [(300, 3), (389, 6), (400, 0)], pre=_PRE_C, a=300, b=600, ctx=_ctxC))
check(isinstance(_pCl, dict) and _pCl["crank_at"] == iso(T_C) and _pCl["rated_sec"] == 309.0
      and _pCl["fref"]["range"] == [45.9, 50.0] and not _pCl["unstable"],
      f"4е: окно с начала «Работы», прогрев в контексте — прокрутка с начала прогрева: {_pCl}")
check(prof_of(start_case(T_C, _ptsC, [(300, 3), (389, 6), (400, 0)], pre=_PRE_C, a=300, b=600)) is None,
      "4е: без контекста (не знаем, что было до окна) — не пуск, профиля нет")
_pO = prof_of(start_case(T_E, _ptsE[:4], [(-500, 0), (0, 3)], pre=_PRE_E, a=-600, b=25, live_edge=True))
check(isinstance(_pO, dict) and _pO["open"] and start_window(_pO) == (iso(T_E), None)
      and "(окно пуска ещё идёт)" in start_profile_text(_pO, MSK) and _pO["unstable"],
      f"4е: живой край, окно пуска идёт — open, конец окна не задан: {_pO}")
_pOc = prof_of(start_case(T_E, _ptsE, [(-500, 0), (0, 3)], cb_at=49, pre=_PRE_E, tail=(60, 1500.0, 50.0),
                          b=120, live_edge=True))
check(isinstance(_pOc, dict) and not _pOc["open"] and start_window(_pOc) == (iso(T_E), iso(S(T_E, 49))),
      f"4е: живой край, но окно пуска кончилось (автомат) — конец задан: {_pOc and _pOc['end_by']}")

# ── 4ж. Изоляция, хранение, срезы ──
_orig = _sg.start_profile
_sg.start_profile = _boom
LOG.records.clear()
try:
    _segsX = start_case(T_J, _ptsJ, [(-40, 0), (0, 3), (180, 5), (269, 0)],
                        pre=[(-40, FREQ, 0.0), (-41, FREF, 0.0), (-280, OIL1, 25.0)], a=-60, b=330)
    check(prof_of(_segsX) is None and any(s.run_state == 3 and s.subsegments for s in _segsX)
          and dets(_segsX, "RPM_OVERSPEED") and logged("Профиль пуска сегмента"),
          "4ж: сбой профиля — сегменты и детекции строятся, профиля нет, запись в лог")
except Exception as e:  # noqa: BLE001
    check(False, f"4ж: сбой профиля уронил segment(): {e!r}")
finally:
    _sg.start_profile = _orig
import analytics.rpm_freq as _rf  # noqa: E402
_orig = _rf.overspeed_params
_rf.overspeed_params = _boom
LOG.records.clear()
try:
    _pX = start_profile(_sg._build_by_addr(start_rows(T_J, _ptsJ, a=-60, b=330)),
                        [rs(0, S(T_J, -40), T_J), rs(3, T_J, S(T_J, 180))], CFG, T_J, None, S(T_J, 180))
    check(isinstance(_pX, dict) and _pX["overspeed"] is None and why(_pX) == {"swing", "flips"}
          and logged("Заброс оборотов в профиле пуска не оценён"),
          f"4ж: сбой оценки заброса — профиль без заброса, раскачка на месте: {_pX and _pX['reasons']}")
finally:
    _rf.overspeed_params = _orig
# Срезы по метке: профиль не разбирает всю историю окна (value_series не зовётся)
_orig = _sg.value_series
_sg.value_series = _boom
try:
    _pV = start_profile(_sg._build_by_addr(start_rows(T_J, _ptsJ, a=-60, b=330)),
                        [rs(0, S(T_J, -40), T_J), rs(3, T_J, S(T_J, 180))], CFG, T_J, None, S(T_J, 180),
                        steady_to=S(T_J, 179))
    check(isinstance(_pV, dict) and _pV["swing_sec"] == 110.0 and _pV["flips"] == 6,
          "4ж: профиль считается срезами по метке (без value_series по всему окну)")
except Exception as e:  # noqa: BLE001
    check(False, f"4ж: профиль разбирает всю историю окна: {e!r}")
finally:
    _sg.value_series = _orig
_wseg = next(s for s in _segs3 if s.start_profile)
check(json.loads(json.dumps(_wseg.to_dict()))["start_profile"] == _wseg.start_profile,
      "4ж: профиль — в Segment.to_dict() (characteristics_json), сериализуется в JSON")
check(all("start_profile" not in s.to_dict() for s in _segs3 if not s.start_profile),
      "4ж: не пуск — ключа start_profile в characteristics_json нет (как у строк до v5.1.23)")

# ── 4з. Ревью R7: живой движок — задание и масло на начало прокрутки из посева ──
# ДГУ №2 20.09 16:34:50: прогрев ровно 5 мин, окно «Работы» с 16:39:50, история —
# с преамбулой 300 с (с 16:34:50); задание 30,0 в 16:34:49 и масло 15 °C в
# 16:31:39 — только в посеве перед окном (engine._load_seed). В окне масло 16 °C
_PRE_C7 = _PRE_C + [(100, OIL1, 16.0)]
_seedC = {"held": {"FREQ_REFERENCE": {"ts": S(T_C, -1), "value": 30.0},
                   "OIL_TEMP": {"ts": S(T_C, -191), "value": 15.0},
                   "RPM": {"ts": S(T_C, -71), "value": 0.0}, "FREQUENCY": {"ts": S(T_C, -182), "value": 0.0}},
          "held_gaps": []}
_pCs = prof_of(start_case(T_C, _ptsC, [(300, 3), (389, 6), (400, 0)], pre=_PRE_C7, a=300, b=600, ctx=_ctxC,
                          hist_from=0, seed=_seedC))
_pCn = prof_of(start_case(T_C, _ptsC, [(300, 3), (389, 6), (400, 0)], pre=_PRE_C7, a=300, b=600, ctx=_ctxC,
                          hist_from=0))
_pCb = prof_of(start_case(T_C, _ptsC, _RS_C, pre=_PRE_C7))
check(isinstance(_pCs, dict) and _pCs["fref"]["seq"] == [30.0, 45.9, 50.0] and _pCs["oil_temp"] == 15.0
      and _pCs["fref"]["seq"] == _pCb["fref"]["seq"] and _pCs["oil_temp"] == _pCb["oil_temp"]
      and _pCs["fref"]["range"] == _pCb["fref"]["range"] and _pCs["unstable"] == _pCb["unstable"],
      f"4з: живой путь с посевом — задание 30 → 45,9 → 50 Гц и масло 15 °C, как в пакетном окне: "
      f"{_pCs and (_pCs['fref']['seq'], _pCs['oil_temp'])} / {(_pCb['fref']['seq'], _pCb['oil_temp'])}")
check(isinstance(_pCn, dict) and _pCn["fref"]["seq"] == [45.9, 50.0] and _pCn["oil_temp"] == 16.0,
      f"4з: (для сравнения) без посева значения на прокрутку не видно — так было до правки: "
      f"{_pCn and (_pCn['fref']['seq'], _pCn['oil_temp'])}")
check("задание 30 → 45,9 → 50 Гц" in (start_profile_text(_pCs, MSK) or "")
      and "масло перед пуском 15 °C" in (start_profile_text(_pCs, MSK) or ""),
      f"4з: строка «Пуск» живого пути: {start_profile_text(_pCs, MSK)}")
# Задание выше 51,5 на прокрутке, видное только в посеве, — признак fref_high (В1)
_seedC52 = copy.deepcopy(_seedC)
_seedC52["held"]["FREQ_REFERENCE"]["value"] = 52.2
_pC52 = prof_of(start_case(T_C, _ptsC, [(300, 3), (389, 6), (400, 0)], pre=_PRE_C7, a=300, b=600, ctx=_ctxC,
                           hist_from=0, seed=_seedC52))
check(isinstance(_pC52, dict) and _pC52["unstable"] and why(_pC52) == {"fref_high"}
      and _pC52["fref"]["high"]["stage"] == "crank",
      f"4з: 52,2 Гц на прокрутке в посеве — неустойчив и в живом пути: {_pC52 and _pC52['reasons']}")
check("FREQ_REFERENCE" in _eng._HELD_ROLES, "4з: задание частоты — в удержанных значениях посева движка")
# _value_at: строка окна главнее посева; посев — только не позже момента и не старше срока
_va = _sg._value_at
_ba7 = _sg._build_by_addr([h(FREF, S(T_Q, -5), 49.0)])
check(_va({}, FREF, T_Q, 30, (S(T_Q, -1), 52.2)) == (S(T_Q, -1), 52.2)
      and _va(_ba7, FREF, T_Q, 30, (S(T_Q, -1), 52.2)) == (S(T_Q, -5), 49.0)
      and _va({}, FREF, T_Q, 30, (S(T_Q, 1), 52.2)) is None
      and _va({}, FREF, T_Q, 30, (S(T_Q, -31), 52.2)) is None
      and _va({}, FREF, T_Q, None, (S(T_Q, -3600), 52.2)) == (S(T_Q, -3600), 52.2)
      and _va({}, FREF, T_Q, 30, None) is None,
      "4з: _value_at — строка окна, иначе посев не позже момента и не старше срока")

# ── 4и. Ревью R14: номинал в кадре смены режима — «на номинале с первого отсчёта» ──
for _nm, _pp14, _bnd in (("15.10 05:42", _pE, 31.0), ("27.09 17:24", _pA, 11.0), ("25.09 13:48", _pD, 23.0)):
    _t14 = start_profile_text(_pp14, MSK) or ""
    check(_pp14["rated_sec"] == 0.0 and _pp14["rated_first"] is True and _pp14["rated_bound_sec"] == _bnd
          and _t14.startswith(f"на номинале с первого отсчёта (разгон между отсчётами, не дольше "
                              f"{_lbl.fmt_duration(_bnd)});") and "за 0 с" not in _t14,
          f"4и: {_nm} — номинал в кадре смены режима, оценка сверху {_bnd} с: {_t14}")
check(_pB["rated_first"] is False and (start_profile_text(_pB, MSK) or "").startswith("выход на номинал за 9 с;")
      and _pF["rated_first"] is False and _p3["rated_first"] is False,
      "4и: разгон за 9–10 с (номинал в следующем кадре) — «выход на номинал за N с», как было")
_old14 = {k: v for k, v in _pE.items() if k not in ("rated_first", "rated_bound_sec")}
check((start_profile_text(_old14, MSK) or "").startswith("на номинале с первого отсчёта;")
      and (start_profile_text(dict(_old14, rated_sec=9.0), MSK) or "").startswith("выход на номинал за 9 с;")
      and (start_profile_text(dict(_pE, rated_bound_sec=None), MSK) or "").startswith("на номинале с первого отсчёта;"),
      "4и: профиль без признака (rated_sec < 1) и без оценки — «на номинале с первого отсчёта»")

# ══ 5. Гейт: запрет снятия start_unstable (п.4, В1 и В3) ═════════════════════
import online.manager as _mgr  # noqa: E402
import online.status_assembler as _sa  # noqa: E402
from online.engine import _ban_row, _extract_open_segment_data, _make_seg_hint  # noqa: E402
from online.status_assembler import (GATE_RULES_VERSION, build_structural_status, build_warning_prompt,  # noqa: E402
                                     compute_analytics_hash, first_cluster_start, gate_can_cancel, gate_dets,
                                     start_unstable_reason, suppression_active, suppression_bans,
                                     uncancellable_reasons)

# Открытая строка ДГУ №2 15.10 12:42 на живом краю 12:43:20 (окно пуска идёт)
_rows3o = [r for r in _rows3 if r["ts"] < M15(12, 43, 20)]
_segs3o = segment([rs(0, M15(10, 38), M15(12, 42, 9)), rs(3, M15(12, 42, 9), None)], _rows3o, [], [], CFG,
                  "SN", "pcc", 2, "E", M15(12, 40), M15(12, 43, 20), live_edge=True)
_open = _segs3o[-1]
_cv, _dets_o = _extract_open_segment_data(_open)


def open_row(seg, cv, dd, chars_extra=None, h=None):
    """Строка открытого сегмента, как её пишет движок (upsert_open_segment)."""
    ch = {**seg.to_dict(), "_total_run_state_sec": {seg.run_state: seg.duration_sec}}
    ch.update(chars_extra or {})
    return {"id": 1, "run_state": seg.run_state, "t_start": seg.t_start, "characteristics_json": json.dumps(ch),
            "current_values_json": cv, "active_detections_json": dd, "coking_risk_json": None,
            "gate_suppressed_hash": h}


_row_o = open_row(_open, _cv, _dets_o)
_sp_o = _open.start_profile
check(isinstance(_sp_o, dict) and _sp_o["open"] and _sp_o["unstable"]
      and json.loads(_row_o["characteristics_json"]).get("start_profile") == _sp_o,
      f"5а: профиль пуска — в characteristics_json открытой строки (окно идёт): {_sp_o and _sp_o.get('end_by')}")
check("**open_seg.to_dict()" in _src_eng and "start_profile=sprof" in _src_eng,
      "5а: движок пишет open_seg.to_dict() в открытую строку и восстанавливает профиль в подсказке предка")
_st = build_structural_status(_row_o, None, MSK)
check(_st["start"] == _sp_o, "5а: в статус кладётся start из start_profile")
_al = {a["scenario"]: a for a in _st["analytics_alarms"]}
check({"RPM_UNDERSPEED", "RPM_OVERSPEED"} <= set(_al)
      and _al["RPM_OVERSPEED"]["onset"] == iso(M15(12, 42, 19))
      and all(_al[sc]["onset"] == first_cluster_start(next(d for d in _dets_o if d["scenario"] == sc)).isoformat()
              for sc in ("RPM_UNDERSPEED", "RPM_OVERSPEED")),
      f"5а: тревоги статуса получают начало первого кластера (values.live): "
      f"{[(sc, a.get('onset')) for sc, a in _al.items()]}")
check(all("onset" not in a for a in _st["analytics_alarms"] if a["scenario"] not in START_SCENARIOS),
      "5а: onset — только у просадки и заброса")
_rs_o = uncancellable_reasons(_st)
check([r["code"] for r in _rs_o] == ["start_unstable"]
      and _rs_o[0]["text"].startswith("просадка или заброс оборотов на неустойчивом пуске 12:42:09 МСК (заброс 1606"),
      f"5а: uncancellable_reasons — start_unstable (время с поясом, ревью R13): {_rs_o}")
_gf5 = _sa.gate_facts_md(_st, "cancel", False, _rs_o)
check("вердикт модели «снять» — не применён: просадка или заброс оборотов на неустойчивом пуске 12:42:09 МСК ("
      in _gf5, f"5а: «Факты (система, не ИИ)» — время запрета с «МСК» (v5.1.22 P1-6, ревью R13): {_gf5}")
check(not gate_can_cancel(_st), "5а: гейт не вправе снять состав (gate_can_cancel)")
check(suppression_bans(_row_o, _dets_o) == ["start_unstable"],
      f"5а: suppression_bans — start_unstable: {suppression_bans(_row_o, _dets_o)}")
_h_o = compute_analytics_hash(gate_dets(_sa._without_state(_dets_o)))
_row_h = dict(_row_o, gate_suppressed_hash=_h_o)
check(not suppression_active(_row_h, _dets_o) and _sa.is_analytics_suppressed(_row_h, _dets_o),
      "5а: снятие с совпавшим хешем не действует (запрет) — статус, наследование, сводка")
check(_mgr._inherit_allowed(_row_o, _h_o) == (False, ["start_unstable"]),
      f"5а: manager._inherit_allowed — запрет start_unstable: {_mgr._inherit_allowed(_row_o, _h_o)}")
_pr = build_warning_prompt(_st)
check("Пуск 12:42:09 МСК: выход на номинал за 10 с;" in _pr
      and "Гейт: отмена НЕДОСТУПНА — просадка или заброс оборотов на неустойчивом пуске 12:42:09 МСК" in _pr,
      f"5а: вход гейта — строка «Пуск ЧЧ:ММ:СС» и «отмена НЕДОСТУПНА»: "
      f"{[ln for ln in _pr.splitlines() if ln.startswith(('Пуск', 'Гейт'))]}")
check(GATE_RULES_VERSION == "5.1.15", "5а: GATE_RULES_VERSION не поднят (запрет — для новых пусков)")
check("start_unstable" not in (ROOT / "llm" / "router.py").read_text(encoding="utf-8"),
      "5а: системный промпт гейта (llm/router.py) не тронут")

# ── 5б. Один помощник в обоих местах ──
_calls = []
_orig = _sa.start_unstable_reason


def _rec(profile, items):
    _calls.append((profile, [i.get("scenario") for i in items or []]))
    return _orig(profile, items)


_sa.start_unstable_reason = _rec
try:
    _r1 = [r["code"] for r in _sa.uncancellable_reasons(_st)]
    _r2 = _sa.suppression_bans(_row_o, _dets_o)
finally:
    _sa.start_unstable_reason = _orig
check(len(_calls) == 2 and _calls[0][0] == _calls[1][0] == _sp_o and "start_unstable" in _r1 and "start_unstable" in _r2,
      f"5б: uncancellable_reasons и suppression_bans зовут один помощник с тем же профилем: {len(_calls)}")
for _fn in (_sa.uncancellable_reasons, _sa.suppression_bans):
    _s = inspect.getsource(_fn)
    check("start_unstable_reason(" in _s and "RPM_UNDERSPEED" not in _s and "START_UNSTABLE_SCENARIOS" not in _s,
          f"5б: {_fn.__name__} — без своей проверки сценариев и окна (только помощник)")
check("suppression_bans" in inspect.getsource(_mgr._inherit_allowed)
      and "suppression_bans(" in inspect.getsource(_eng.OnlinePollEngine._inherit_gate_suppression)
      and "suppression_active(_ban_row(" in inspect.getsource(_eng._gate_suppressed_scenarios),
      "5б: наследование движка, _inherit_allowed и сводка закрытия — через suppression_bans / suppression_active")
# Закрываемый сегмент: запрет — по профилю самого сегмента, подсказка предка его хранит
_br = _ban_row({"t_start": _open.t_start, "characteristics_json": json.dumps({"stop_kind": None})}, _open, _h_o)
check(_br["characteristics_json"]["start_profile"] == _sp_o and suppression_bans(_br, _dets_o) == ["start_unstable"],
      "5б: _ban_row — профиль закрываемого сегмента (запрет и на конце сегмента)")
_br0 = _ban_row(_row_o, next(s for s in _segs3o if s.run_state == 0), _h_o)
check(_br0["characteristics_json"]["start_profile"] is None,
      "5б: _ban_row стоп-сегмента — профиль открытой строки не переносится")
_hint = _make_seg_hint({"run_state": 3, "t_start": _open.t_start, "t_end": None,
                        "characteristics_json": json.dumps(_open.to_dict())})
check(_hint.start_profile == _sp_o and _make_seg_hint({"run_state": 3, "characteristics_json": "{}"}).start_profile is None,
      "5б: подсказка предка (рестарт) восстанавливает профиль; у старой строки — None")

# ── 5в. Заброс на пуске — нельзя; заброс вне пуска и провал под нагрузкой после окна — можно ──
_ov = [d for d in _dets_o if d["scenario"] == "RPM_OVERSPEED"]
_un = [d for d in _dets_o if d["scenario"] == "RPM_UNDERSPEED"]
check(_ov and start_unstable_reason(_sp_o, _ov) and _un and start_unstable_reason(_sp_o, _un),
      "5в: заброс на пуске снимать нельзя (и просадку)")
_spc = _p3   # то же окно, закрытое: 12:42:09–12:43:29


def det_at(sc, t):
    """Детекция с первым кластером в t (values.live)."""
    return {"scenario": sc, "severity": "CAUTION", "t_detected": iso(t),
            "values": {"live": {"clusters": [{"start": iso(t), "end": iso(S(t, 30)), "ongoing": False}],
                                "onset": iso(t)}}}


check(start_unstable_reason(_spc, [det_at("RPM_OVERSPEED", M15(13, 30))]) is None,
      "5в: заброс вне окна пуска (сброс нагрузки) — снимать можно (В3)")
check(start_unstable_reason(_spc, [det_at("RPM_UNDERSPEED", M15(12, 43, 29))]) is None
      and start_unstable_reason(_spc, [det_at("RPM_UNDERSPEED", M15(12, 43, 28))]) is not None,
      "5в: окно пуска — [прокрутка, конец окна): первый кластер на конце окна и позже — не пуск")
check(start_unstable_reason(_spc, [det_at("RPM_UNDERSPEED", M15(12, 42, 8))]) is None,
      "5в: кластер до начала прокрутки — не пуск")
check(start_unstable_reason(_spc, [det_at("OIL_DILUTION", M15(12, 42, 30))]) is None,
      "5в: прочие замечания — запрета нет")
check(start_unstable_reason(_sp_o, [det_at("RPM_UNDERSPEED", M15(14, 0))]) is not None,
      "5в: окно пуска ещё идёт (open) — любой кластер после прокрутки — на пуске")
# Провал под нагрузкой после окна пуска (реальный пуск #35949 + провал через 15 мин)
_ptsML = _ptsM + [(s, 1500.0 + (s // 10) % 2, 50.0, None, 400.0 if s == 147 else None) for s in range(147, 1047, 10)] \
    + [(1047, 1418.0, 47.3, None, None)] + [(s, 1500.0, 50.0, None, None) for s in range(1057, 1200, 10)]
_segsML = start_case(T_M, _ptsML, [(-500, 0), (0, 3)], cb_at=137, pre=_PRE_M, b=1100, live_edge=True)
_openML = _segsML[-1]
_cvML, _dML = _extract_open_segment_data(_openML)
_rowML = open_row(_openML, _cvML, _dML)
_dUn = next((d for d in _dML if d["scenario"] == "RPM_UNDERSPEED"), {})
_clUn = ((_dUn.get("values") or {}).get("live") or {}).get("clusters") or []
check(_openML.start_profile and _openML.start_profile["unstable"] and len(_clUn) == 2
      and _clUn[0]["start"] == iso(S(T_M, 17)) and first_cluster_start(_dUn) == S(T_M, 1047),
      f"5в: #35949 + провал 1418 / 47,3 Гц под нагрузкой через 15 мин — в живом составе эпизод с 10:15:22 "
      f"(кластер пуска 09:58:12 в той же детекции остыл): {[c.get('start') for c in _clUn]}")
check(suppression_bans(_rowML, _dML) == [] and "start_unstable" not in
      [r["code"] for r in uncancellable_reasons(build_structural_status(_rowML, None, MSK))],
      "5в: провал под нагрузкой после окна неустойчивого пуска — снимать можно")
# Устойчивый пуск с провалом на пуске (27.09 17:24: 1426 / 47,7 Гц) — снимать можно
_segsAo = start_case(T_A, [(-1, None, None, 50.0, None), (0, 1426.0, 47.7, None, None), (9, 1499.0, 50.0, None, None)],
                     [(-500, 0), (0, 3)], pre=[(-11, RPM, 0.0), (-291, FREQ, 0.0), (-240, OIL1, 66.0)],
                     tail=(20, 1500.0, 50.0), b=60, live_edge=True)
_cvA, _dA = _extract_open_segment_data(_segsAo[-1])
check(_segsAo[-1].start_profile and not _segsAo[-1].start_profile["unstable"]
      and any(d["scenario"] == "RPM_UNDERSPEED" for d in _dA)
      and suppression_bans(open_row(_segsAo[-1], _cvA, _dA), _dA) == [],
      "5в: провал на устойчивом пуске (27.09 17:24) — снимать можно")

# ── 5г. Старая строка без профиля; сбой помощника изолирован ──
_ch_old = _open.to_dict()
_ch_old.pop("start_profile", None)
_row_old = dict(_row_o, characteristics_json=json.dumps(_ch_old))
_st_old = build_structural_status(_row_old, None, MSK)
check(_st_old["start"] is None and uncancellable_reasons(_st_old) == [] and suppression_bans(_row_old, _dets_o) == []
      and "\nПуск " not in build_warning_prompt(_st_old),
      "5г: строка до v5.1.23 (без профиля) — запрета нет, строки «Пуск» во входе гейта нет")
_sa.start_unstable_reason = _boom
LOG.records.clear()
try:
    _rb1 = uncancellable_reasons(dict(_st, panel_severity="внимание"))
    _rb2 = suppression_bans(_row_o, _dets_o)
    check([r["code"] for r in _rb1] == ["panel"] and _rb2 == []
          and logged("Запрет снятия на неустойчивом пуске не проверен"),
          f"5г: сбой помощника — прочие запреты на месте, запись в лог: {_rb1}, {_rb2}")
except Exception as e:  # noqa: BLE001
    check(False, f"5г: сбой помощника уронил проверку запретов: {e!r}")
finally:
    _sa.start_unstable_reason = _orig

# ── 5д. Ревью R1: суточный рез посреди окна неустойчивого пуска ──
# ДГУ №2 15.10 12:42 (реальные отсчёты), рез — в 12:42:45, посреди раскачки:
# у строки после реза своего профиля нет (окно через рез не дописывается), а
# эпизоды пуска идут мостом — запрет снятия переходит профилем предка
from analytics.transitions import link_from_segment, push_ctx  # noqa: E402
from online.status_assembler import start_profile_of, start_unstable_scenarios  # noqa: E402

_CUT = M15(12, 42, 45)
_segs_c1 = segment(_ENUM3, _rows3, [], [], CFG, "SN", "pcc", 2, "E", M15(12, 40), _CUT)   # закрытие резом
_w_c1 = next(s for s in _segs_c1 if s.run_state == 3)
_sp_c1 = _w_c1.start_profile
check(isinstance(_sp_c1, dict) and _sp_c1["end_by"] == "state_end" and _sp_c1["t_to"] == iso(_CUT)
      and _sp_c1["unstable"], f"5д: до реза — окно пуска обрезано резом 12:42:45: {_sp_c1 and _sp_c1['t_to']}")
_ctx_c = []
for _s in _segs_c1:
    _ctx_c = push_ctx(_ctx_c, link_from_segment(_s))
# Окно после реза — как у движка: история с преамбулой 300 с, «Работа» идёт
_rows_c2 = [r for r in _rows3 if S(_CUT, -300) <= r["ts"] < M15(12, 43, 20)]
_enum_c2 = [rs(3, M15(12, 42, 9), None)]


def seg_c2(carry, rows=_rows_c2, enum=_enum_c2, b=M15(12, 43, 20), live=True):
    return segment(enum, rows, [], [], CFG, "SN", "pcc", 2, "E", _CUT, b, transition_ctx=_ctx_c,
                   live_edge=live, start_carry=carry)


check(_eng._start_carry(_w_c1) == _sp_c1 and _eng._start_carry(next(s for s in _segs_c1 if s.run_state == 0)) is None
      and _eng._start_carry(None) is None
      and _eng._start_carry(_make_seg_hint({"run_state": 3, "characteristics_json": json.dumps(_w_c1.to_dict())}))
      == _sp_c1, "5д: _start_carry — профиль предка-«Работы» (Segment и подсказка после рестарта), иначе None")
_segs_c2 = seg_c2(_eng._start_carry(_w_c1))
_open_c2 = _segs_c2[-1]
_spp = _open_c2.start_profile_prev
check(_open_c2.run_state == 3 and _open_c2.start_profile is None and isinstance(_spp, dict) and _spp.get("carried")
      and _spp["cut_at"] == iso(_CUT) and _spp["crank_at"] == _sp_c1["crank_at"] and _spp["unstable"]
      and _spp["reasons"] == _sp_c1["reasons"] and _spp["open"] and start_window(_spp) == (_sp_c1["crank_at"], None),
      f"5д: после реза — своего профиля нет, профиль предка с окном без обрезки (идёт): {_spp}")
check("start_profile_prev" in _open_c2.to_dict() and "start_profile" not in _open_c2.to_dict()
      and not any(ln.startswith("- **Пуск:**") for ln in
                  to_markdown([_open_c2], "SN", "pcc", 2, _CUT, M15(12, 43, 20), tz=MSK).splitlines()),
      "5д: профиль предка — в characteristics_json (start_profile_prev), строки «Пуск» в отчёте нет")
_cv_c2, _d_c2 = _extract_open_segment_data(_open_c2)
_row_c2 = open_row(_open_c2, _cv_c2, _d_c2)
_st_c2 = build_structural_status(_row_c2, None, MSK)
_h_c2 = compute_analytics_hash(gate_dets(_sa._without_state(_d_c2)))
_pr_c2 = build_warning_prompt(_st_c2)
# При пороге 1575 (В2′) заброс 12:42:19–12:42:29 кончился до реза — после реза под
# запретом мостом идёт просадка (при 1552,5 был и второй участок заброса 12:42:49–12:43:09)
check("RPM_UNDERSPEED" in {d["scenario"] for d in _d_c2} and "RPM_OVERSPEED" not in {d["scenario"] for d in _d_c2}
      and _st_c2["start"] == _spp and "start_unstable" in [r["code"] for r in uncancellable_reasons(_st_c2)]
      and not gate_can_cancel(_st_c2) and suppression_bans(_row_c2, _d_c2) == ["start_unstable"]
      and _mgr._inherit_allowed(_row_c2, _h_c2) == (False, ["start_unstable"])
      and not suppression_active(dict(_row_c2, gate_suppressed_hash=_h_c2), _d_c2),
      f"5д: строка после реза — запрет start_unstable в обоих местах и в наследовании: "
      f"{[r['code'] for r in uncancellable_reasons(_st_c2)]}, {suppression_bans(_row_c2, _d_c2)}")
check("Пуск 12:42:09 МСК (до суточного реза 12:42:45): " in _pr_c2 and "вердикт cancel ДОПУСТИМ" not in _pr_c2
      and "Гейт: отмена НЕДОСТУПНА — просадка или заброс оборотов на неустойчивом пуске 12:42:09 МСК" in _pr_c2,
      f"5д: вход гейта строки после реза — строка «Пуск» предка и «отмена НЕДОСТУПНА»: "
      f"{[ln for ln in _pr_c2.splitlines() if ln.startswith(('Пуск', 'Гейт'))]}")
# Без переноса (до правки) — запрета не было: так и терялся
_open_n2 = seg_c2(None)[-1]
_cvn, _dn2 = _extract_open_segment_data(_open_n2)
check(_open_n2.start_profile is None and _open_n2.start_profile_prev is None
      and suppression_bans(open_row(_open_n2, _cvn, _dn2), _dn2) == [],
      "5д: (для сравнения) без профиля предка строка после реза запрета не знает")
# (для сравнения) Прежний порог 1552,5 (proximity_pct 10): второй участок заброса
# 12:42:49–12:43:09 — после реза; путь «заброс на неустойчивом пуске после реза → запрет»
_CFG10 = cfg_det(CFG, "RPM_OVERSPEED", proximity_pct=10)
_segs_c1x = segment(_ENUM3, _rows3, [], [], _CFG10, "SN", "pcc", 2, "E", M15(12, 40), _CUT)
_ctx_cx = []
for _s in _segs_c1x:
    _ctx_cx = push_ctx(_ctx_cx, link_from_segment(_s))
_open_c2x = segment(_enum_c2, _rows_c2, [], [], _CFG10, "SN", "pcc", 2, "E", _CUT, M15(12, 43, 20),
                    transition_ctx=_ctx_cx, live_edge=True,
                    start_carry=_eng._start_carry(next(s for s in _segs_c1x if s.run_state == 3)))[-1]
_cv_c2x, _d_c2x = _extract_open_segment_data(_open_c2x)
_row_c2x = open_row(_open_c2x, _cv_c2x, _d_c2x)
_st_c2x = build_structural_status(_row_c2x, None, MSK)
check({"RPM_OVERSPEED", "RPM_UNDERSPEED"} <= {d["scenario"] for d in _d_c2x}
      and isinstance(_open_c2x.start_profile_prev, dict) and _open_c2x.start_profile_prev["unstable"]
      and sorted(start_unstable_scenarios(_open_c2x.start_profile_prev, _d_c2x)) == ["RPM_OVERSPEED", "RPM_UNDERSPEED"]
      and "start_unstable" in [r["code"] for r in uncancellable_reasons(_st_c2x)]
      and suppression_bans(_row_c2x, _d_c2x) == ["start_unstable"],
      f"5д: (для сравнения, 1552,5) заброс после реза на неустойчивом пуске — тоже под запретом: "
      f"{sorted(d['scenario'] for d in _d_c2x)}, {suppression_bans(_row_c2x, _d_c2x)}")
_br_c2 =_ban_row({"t_start": _open_c2.t_start, "characteristics_json": "{}"}, _open_c2, _h_c2)
check(_br_c2["characteristics_json"]["start_profile_prev"] == _spp and suppression_bans(_br_c2, _d_c2) == ["start_unstable"],
      "5д: _ban_row закрываемой строки после реза — профиль предка (запрет и на конце сегмента)")
# Закрытое окно после реза: конец окна пуска — без обрезки, как у профиля без реза
_spp_cl = seg_c2(_sp_c1, rows=[r for r in _rows3 if r["ts"] >= S(_CUT, -300)],
                 enum=[rs(3, M15(12, 42, 9), M15(12, 43, 29)), rs(6, M15(12, 43, 29), M15(12, 43, 40)),
                       rs(5, M15(12, 43, 40), M15(12, 45, 9))], b=M15(12, 45), live=False)[0].start_profile_prev
check(isinstance(_spp_cl, dict) and _spp_cl["t_to"] == _p3["t_to"] == iso(M15(12, 43, 29))
      and _spp_cl["end_by"] == _p3["end_by"] and not _spp_cl["open"],
      f"5д: окно пуска предка — до 12:43:29 (конец «Работы»), как без реза: {_spp_cl and _spp_cl['t_to']}")
# Начало эпизода, а не кластера нового окна: эпизод с 12:42:40 идёт мостом, кластер
# окна после реза начался позже конца окна пуска
_dl = det_at("RPM_UNDERSPEED", M15(12, 44, 0))
check(start_unstable_reason(_spp_cl, [_dl]) is None, "5д: кластер окна после конца окна пуска — сам по себе не пуск")
_eng._mark_episode_onsets([_dl], {"RPM_UNDERSPEED": {"t_open": M15(12, 42, 40)}})
check(_dl.get("onset") == iso(M15(12, 42, 40)) and start_unstable_reason(_spp_cl, [_dl]) is not None
      and first_cluster_start(_dl) == M15(12, 42, 40),
      f"5д: эпизод памяти движка начался в окне пуска (мост через рез) — запрет: {_dl.get('onset')}")
_dsame = det_at("RPM_UNDERSPEED", M15(14, 0))
_dpanel = {"scenario": "CONTROLLER_FAULT", "values": {"addr": 1, "bit": 1}}
_eng._mark_episode_onsets([_dsame, _dpanel], {"RPM_UNDERSPEED": {"t_open": M15(14, 0)},
                                              "CONTROLLER_FAULT|1|1": {"t_open": M15(12, 0)}})
check("onset" not in _dsame and "onset" not in _dpanel and start_unstable_reason(_spp_cl, [_dsame]) is None,
      "5д: эпизод начался с кластером (новый провал под нагрузкой) и прочие — без поля onset, запрета нет")
# Перепроверка R1: быстрый повторный пуск после стопа (16.10: стоп 40 с между
# #35939 и #35944) — эпизод прежней «Работы» продолжился свежестью в новом пуске,
# его начало раньше прокрутки нового пуска. Запрет нового неустойчивого пуска — по
# кластеру окна: начало эпизода движок ставит только «Работе», несущей профиль
# предка через рез, и не раньше её прокрутки
_dq = det_at("RPM_UNDERSPEED", M15(12, 42, 30))
_eng._mark_episode_onsets([_dq], {"RPM_UNDERSPEED": {"t_open": M15(12, 40, 50)}},
                          since=_eng._carry_onset_floor(_spc))
_dq2 = det_at("RPM_UNDERSPEED", M15(12, 44, 0))
_eng._mark_episode_onsets([_dq2], {"RPM_UNDERSPEED": {"t_open": M15(12, 42, 40)}},
                          since=_eng._carry_onset_floor(_spp_cl))
check("onset" not in _dq and start_unstable_reason(_spc, [_dq]) is not None
      and _dq2.get("onset") == iso(M15(12, 42, 40)) and start_unstable_reason(_spp_cl, [_dq2]) is not None
      and _eng._carry_onset_floor(None) is None and _eng._carry_onset_floor({"crank_at": None}) is None
      and _eng._carry_onset_floor(_spp_cl) == M15(12, 42, 7),
      f"5д: эпизод прежней «Работы» через стоп — начало не переносится, запрет нового пуска на месте; "
      f"мост через рез — переносится: {_dq.get('onset')}, {_dq2.get('onset')}")
_src_pe = inspect.getsource(_eng.OnlinePollEngine._process_episodes)
_src_uw = inspect.getsource(_eng.OnlinePollEngine._update_open_window)
check("_carry_onset_floor(start_carry)" in _src_pe
      and '_carry_onset_floor(getattr(open_seg, "start_profile_prev", None))' in _src_uw
      and 'start_carry=getattr(open_seg, "start_profile_prev", None)' in _src_uw,
      "5д: движок отмечает начало эпизода только у «Работы» с профилем предка (оба места)")
check("start_carry=_start_carry(self._prev_seg_hint)" in _src_eng and "start_carry=_start_carry(prev_hint)" in _src_eng
      and inspect.getsource(_eng.OnlinePollEngine._process_episodes).count("_mark_episode_onsets(") == 1
      and inspect.getsource(_eng.OnlinePollEngine._update_open_window).count("_mark_episode_onsets(") == 1,
      "5д: движок — профиль предка в открытом окне и на резе, начало эпизода у тревог строки")
# Окно пуска предка кончилось до реза (автомат), эпизод пуска идёт мостом через рез
_sML1 = prof_of(start_case(T_M, _ptsML, [(-500, 0), (0, 3)], cb_at=137, pre=_PRE_M, b=400))
_sML2 = start_case(T_M, _ptsML, [(-500, 0), (0, 3)], cb_at=137, pre=_PRE_M, a=400, b=1100, live_edge=True,
                   hist_from=100, carry=_sML1)[-1].start_profile_prev
check(isinstance(_sML2, dict) and _sML2["t_to"] == _sML1["t_to"] == iso(S(T_M, 137)) and _sML2["end_by"] == "breaker"
      and not _sML2["open"] and start_unstable_reason(_sML2, [det_at("RPM_UNDERSPEED", S(T_M, 1047))]) is None,
      f"5д: окно пуска кончилось до реза — конец прежний, провал под нагрузкой после окна снимать можно: "
      f"{_sML2 and (_sML2['t_to'], _sML2['end_by'])}")
check(seg_c2({**_sp_c1, "crank_at": iso(M15(12, 42, 30))})[-1].start_profile_prev is None
      and seg_c2({"crank_at": None})[-1].start_profile_prev is None,
      "5д: профиль не этой «Работы» (прокрутка позже её начала) или битый — не переносится")
# Строка «Пуск» предка во входе гейта — только пока окно идёт или действует запрет
_al_late = [dict(a, onset=iso(M15(12, 44, 0))) if a["scenario"] in START_SCENARIOS else a
            for a in _st_c2["analytics_alarms"]]
_pr_in = build_warning_prompt(dict(_st_c2, start=_spp_cl))
_pr_late = build_warning_prompt(dict(_st_c2, start=_spp_cl, analytics_alarms=_al_late))
check("Пуск 12:42:09 МСК (до суточного реза 12:42:45): " in _pr_in and "\nПуск " not in _pr_late
      and "вердикт cancel ДОПУСТИМ" in _pr_late,
      "5д: строка «Пуск» предка — пока действует запрет; эпизоды после окна (пуск вчера) — без неё")
check(start_profile_of({"start_profile": _sp_o, "start_profile_prev": _spp}) == _sp_o
      and start_profile_of({"start_profile_prev": _spp}) == _spp and start_profile_of({}) is None
      and start_profile_of(None) is None, "5д: свой профиль строки главнее профиля предка")

# ── 5е. Ревью R4: запрет, появившийся после применённого снятия ──
check(set(start_unstable_scenarios(_sp_o, _dets_o)) == {"RPM_UNDERSPEED", "RPM_OVERSPEED"}
      and start_unstable_scenarios(dict(_sp_o, unstable=False), _dets_o) == []
      and start_unstable_scenarios(_spc, [det_at("RPM_UNDERSPEED", M15(13, 30))]) == [],
      "5е: сценарии под запретом — тот же расчёт, что у запрета (окно, неустойчивость)")
# Статус: пометку «снято» снимает тик планировщика (как у паспорта, v5.1.12)
import asyncio  # noqa: E402
import types  # noqa: E402

import db.analytics as _dba  # noqa: E402
import online.db as _odb  # noqa: E402

_cleared: list = []


async def _fake_clear(sn, et, pid, scenarios):
    _cleared.append(sorted(scenarios))
    return 1


async def _fake_noop(*a, **k):
    return True


async def _fake_setting(key, default=None):
    return default


def tick5(row):
    _cur_row = {"r": row}

    async def _get_open(sn, et, pid):
        return _cur_row["r"]

    saved = {n: getattr(_odb, n) for n in ("get_open_segment", "update_open_segment_status",
                                           "clear_episodes_gate_suppressed")}
    saved_set = _dba.get_app_setting
    _odb.get_open_segment, _odb.update_open_segment_status = _get_open, _fake_noop
    _odb.clear_episodes_gate_suppressed, _dba.get_app_setting = _fake_clear, _fake_setting
    try:
        m = _mgr.OnlineManager()
        m._engines = {"k": types.SimpleNamespace(router_sn="SN", equip_type="pcc", panel_id=2, last_data_ts=None,
                                                 _fault_ref=None, tz=MSK)}
        asyncio.run(m._tick_status_lines())
    finally:
        for n, f in saved.items():
            setattr(_odb, n, f)
        _dba.get_app_setting = saved_set


_cleared.clear()
tick5(_row_o)
check(_cleared == [["RPM_OVERSPEED", "RPM_UNDERSPEED"]],
      f"5е: неустойчивый пуск — тик снимает пометку «снято» с просадки и заброса в окне пуска: {_cleared}")
_cleared.clear()
tick5(_rowML)
check(_cleared == [], f"5е: провал под нагрузкой после окна — пометку не трогаем: {_cleared}")
_cleared.clear()
tick5(_row_c2)
check(_cleared == [["RPM_UNDERSPEED"]],
      f"5е: и в строке после суточного реза (R1; заброс при 1575 кончился до реза): {_cleared}")
_cleared.clear()
tick5(_row_c2x)
check(_cleared == [["RPM_OVERSPEED", "RPM_UNDERSPEED"]],
      f"5е: (для сравнения, 1552,5) после реза — снимает пометку и с заброса: {_cleared}")
_src_gate = inspect.getsource(_mgr._analyze_warning_claude)
check("start_unstable_scenarios(" in _src_gate and "_cancel_sc = [sc for sc in _cancel_sc if sc not in _su_now]"
      in _src_gate and "get_segment_by_id(_landed)" in _src_gate,
      "5е: гейт — сценарии, ставшие под запретом, пока шёл разбор, не метит (свежая строка или поздний сегмент)")
# Сводка и поздний вердикт — раздел 6е

# ══ 6. Тексты: «на пуске», заголовок, строка «Пуск» в отчёте и акте ═════════
from analytics.serializer import build_summary_parts, render_summary_md  # noqa: E402
from corpus.preprocessor import _remarks_from_summary  # noqa: E402
from online.incident_gate import _format_cycle_params  # noqa: E402

_w3 = next(s for s in _segs3 if s.start_profile)
_t_ov = first_cluster_start(next(d.to_dict() for _, d in dets(_segs3, "RPM_OVERSPEED")))
_t_un = first_cluster_start(next(d.to_dict() for _, d in dets(_segs3, "RPM_UNDERSPEED")))


def ep6(sc, t0, t1=None, **kw):
    d = {"source": "analytics", "scenario": sc, "severity": "CAUTION", "gate_suppressed": False,
         "t_open": t0, "t_close": t1, "active_sec": (t1 - t0).total_seconds() if t1 else 30.0, "blind_sec": 0.0}
    d.update(kw)
    return d


def remarks6(eps, segs=None):
    p = build_summary_parts(segs or [_w3], eps, tz=MSK)
    return p, {g.get("scenario"): g for g in p["remarks"]}


_E_UN = ep6("RPM_UNDERSPEED", _t_un, M15(12, 43, 9))
_E_OV = ep6("RPM_OVERSPEED", _t_ov, M15(12, 43, 9))
_p6, _g6 = remarks6([_E_UN, _E_OV])
_note = "на пуске: раскачка 1 мин 9 с, 4 смены знака, задание 42,3–50 Гц, автомат разомкнут, пуск неустойчивый"
check(_g6["RPM_UNDERSPEED"].get("start_note") == _note and _g6["RPM_OVERSPEED"].get("start_note") == _note
      and _g6["RPM_UNDERSPEED"].get("start_unstable") is True,
      f"6а: пометка «на пуске» у просадки и заброса, начавшихся в окне пуска: {_g6['RPM_UNDERSPEED'].get('start_note')}")
_md6 = render_summary_md(_p6)
check("**Просадка оборотов** [аналитика]: 1 эпизод, воздействие за период" in _md6
      and f" — {_note}" in _md6 and _md6.count(_note) == 2,
      f"6а: сводка — пометка в строках замечаний: {[ln for ln in _md6.splitlines() if 'на пуске' in ln]}")
check("на пуске: раскачка 1 мин 9 с" in (_remarks_from_summary({"report_summary_json": _p6}) or ""),
      "6а: шапка заключения (обнаружения) — с пометкой «на пуске»")
# ── 6б. Заголовок: заброс добавляет вид, раскачка — нет ──
_pu, _gu = remarks6([_E_UN])
check(_p6["verdict"]["remarks_n"] == 2 and _pu["verdict"]["remarks_n"] == 1
      and set(_g6) == {"RPM_UNDERSPEED", "RPM_OVERSPEED"} and set(_gu) == {"RPM_UNDERSPEED"},
      f"6б: «ЗАМЕЧАНИЯ — N»: просадка с раскачкой — 1 вид, с забросом — 2 "
      f"({_pu['verdict']['remarks_n']} / {_p6['verdict']['remarks_n']})")
check(not any("неустойчив" in (g.get("name") or "") for g in _p6["remarks"]),
      "6б: отдельного замечания «неустойчивый пуск» нет")
# ── 6в. Без пометки: после окна, панель, прочие сценарии, сегмент без профиля ──
_, _g6c = remarks6([ep6("RPM_UNDERSPEED", M15(12, 43, 29), M15(12, 43, 40)),
                    ep6("OIL_DILUTION", M15(12, 42, 30), M15(12, 43, 0)),
                    ep6("CONTROLLER_FAULT", M15(12, 42, 30), None, source="panel", addr=40401, bit=13,
                        severity="WARNING")])
check(all(not g.get("start_note") for g in _g6c.values()),
      f"6в: эпизод после окна пуска, прочие сценарии и панель — без пометки: "
      f"{[(k, g.get('start_note')) for k, g in _g6c.items()]}")
_w3old = copy.copy(_w3)
_w3old.start_profile = None
_, _g6o = remarks6([_E_UN], [_w3old])
check(not _g6o["RPM_UNDERSPEED"].get("start_note"), "6в: сегмент без профиля (до v5.1.23) — без пометки")
_orig = _lbl.start_note_text
_lbl.start_note_text = _boom
LOG.records.clear()
try:
    _pb, _gb = remarks6([_E_UN, _E_OV])
    check(_pb["verdict"]["remarks_n"] == 2 and not _gb["RPM_UNDERSPEED"].get("start_note")
          and logged("Пометка «на пуске» в сводке не поставлена"),
          "6в: сбой пометки — сводка строится без неё, запись в лог")
except Exception as e:  # noqa: BLE001
    check(False, f"6в: сбой пометки уронил сводку: {e!r}")
finally:
    _lbl.start_note_text = _orig
# ── 6г. Строка «Пуск» в отчёте — у каждого пуска, у неустойчивого с причинами ──
_md3p = to_markdown(_segs3, "SN", "pcc", 2, M15(12, 40), M15(12, 45), tz=MSK)
_pl = [ln for ln in _md3p.splitlines() if ln.startswith("- **Пуск:**")]
check(_pl == ["- **Пуск:** выход на номинал за 10 с; обороты 1310–1606 об/мин, частота 44–53,6 Гц; "
              "раскачка 1 мин 9 с, 4 смены знака; задание 42,3–50 Гц, после номинала 42,3–49,5 Гц; "
              "автомат разомкнут — неустойчивый: заброс 1606 об/мин (53,6 Гц); раскачка 1 мин 9 с, "
              "4 смены знака; задание до 42,3 Гц после выхода на номинал (обороты 1310)"],
      f"6г: отчёт ДГУ №2 15.10 12:42 — одна строка «Пуск» (у «Работы»), с причинами: {_pl}")
_mdF = to_markdown(_segsF, "SN", "pcc", 1, S(T_F, -600), S(T_F, 600), tz=MSK)
_plF = [ln for ln in _mdF.splitlines() if ln.startswith("- **Пуск:**")]
check(_plF == ["- **Пуск:** выход на номинал за 9 с; обороты 1500–1501 об/мин, частота 50 Гц; задание 50 Гц; "
               "автомат разомкнут; масло перед пуском 17 °C"],
      f"6г: устойчивый пуск — строка «Пуск» без пометки: {_plF}")
# ── 6д. Акт: строка «Пуск» после блока сегмента падения (#35941 ← #35939) ──
_wL = next(s for s in _segsL if s.start_profile)
_cyc = [{"run_state": 0, "t_start": iso(S(T_L, -31)), "t_end": iso(T_L),
         "characteristics_json": {"stop_kind": None}},
        {"run_state": 3, "t_start": _wL.t_start, "t_end": _wL.t_end, "characteristics_json": _wL.to_dict()},
        {"run_state": 0, "t_start": _wL.t_end, "t_end": None, "characteristics_json": {"stop_kind": "EMERGENCY"}}]
_act = _format_cycle_params(_cyc, _wL.t_end, None, tz=MSK)
_ia = next((i for i, ln in enumerate(_act) if ln.startswith("Сегмент, из которого машина остановилась")), None)
_ip = next((i for i, ln in enumerate(_act) if ln.startswith("Пуск 09:28:33: ")), None)
check(_ia is not None and _ip is not None and _ip > _ia
      and _act[_ip].endswith("— неустойчивый: на разгоне задание 55 Гц при 1294 об/мин"),
      f"6д: акт — строка «Пуск» после блока сегмента падения: {_act[_ip] if _ip is not None else _act}")
_cyc_old = [dict(c) for c in _cyc]
_cyc_old[1] = dict(_cyc[1], characteristics_json={k: v for k, v in _wL.to_dict().items() if k != "start_profile"})
check(not any(ln.startswith("Пуск ") for ln in _format_cycle_params(_cyc_old, _wL.t_end, None, tz=MSK)),
      "6д: сегмент падения до v5.1.23 (без профиля) — акт прежний")

# ── 6е. Ревью R4: сводка и поздний вердикт — снятие эпизода в окне неустойчивого пуска не действует ──
from analytics.serializer import apply_gate_suppression  # noqa: E402

# Сводка: пометка «снято» на эпизоде в окне неустойчивого пуска не действует
_src5 = {"ts": iso(M15(12, 43, 0)), "segment_id": None}
_p5s, _g5s = remarks6([ep6("RPM_UNDERSPEED", _t_un, M15(12, 43, 9), gate_suppressed=True, gate_suppressed_src=_src5),
                       ep6("RPM_OVERSPEED", _t_ov, M15(12, 43, 9), gate_suppressed=True, gate_suppressed_src=_src5)])
check(_g5s["RPM_UNDERSPEED"]["suppressed"] == 0 and _g5s["RPM_OVERSPEED"]["suppressed"] == 0
      and _p5s["verdict"]["level"] == "CAUTION" and "снято" not in render_summary_md(_p5s),
      f"6е: #35900 — снятие, применённое до неустойчивости, в сводке не действует: {_p5s['verdict']}")
_w3st = copy.copy(_w3)
_w3st.start_profile = dict(_w3.start_profile, unstable=False, reasons=[])
_p5u, _g5u = remarks6([ep6("RPM_UNDERSPEED", _t_un, M15(12, 43, 9), gate_suppressed=True, gate_suppressed_src=_src5)],
                      [_w3st])
check(_g5u["RPM_UNDERSPEED"]["suppressed"] == 1 and _p5u["verdict"]["level"] == "НОРМА",
      "6е: устойчивый пуск — снятие действует, как было")
# Поздний вердикт гейта (строку закрыли, пока шёл разбор): эпизод в окне не снимается
_p5o, _g5o = remarks6([ep6("RPM_UNDERSPEED", _t_un, None), ep6("RPM_OVERSPEED", _t_ov, None)])
_late5 = apply_gate_suppression(_p5o, ["RPM_UNDERSPEED", "RPM_OVERSPEED"], until=M15(12, 44),
                                src={"ts": iso(M15(12, 44))})
check(_g5o["RPM_UNDERSPEED"].get("start_ban_t") == [iso(_t_un)] and _late5 == _p5o,
      f"6е: поздний вердикт — эпизоды в окне неустойчивого пуска не снимаются: {_late5['verdict']}")
_p5x = copy.deepcopy(_p5o)
for _g in _p5x["remarks"]:
    _g.pop("start_ban_t", None)
check(apply_gate_suppression(_p5x, ["RPM_UNDERSPEED", "RPM_OVERSPEED"], until=M15(12, 44))["verdict"]["level"]
      == "НОРМА", "6е: (части без запрета — как раньше: снимается)")
_g5m = {"name": "Просадка оборотов", "source": "analytics", "scenario": "RPM_UNDERSPEED", "severity": "CAUTION",
        "count": 2, "active_sec": 60.0, "blind_sec": 0.0, "suppressed": 0, "unconfirmed": 0, "open": 2,
        "open_live": 2, "open_live_t": [iso(_t_un), iso(M15(12, 50))], "start_ban_t": [iso(_t_un)], "at_end": True}
_p5m = apply_gate_suppression({"version": 3, "remarks": [_g5m], "failed_checks": [], "emergency": False,
                               "verdict": {"level": "CAUTION"}}, ["RPM_UNDERSPEED"], until=None)
check(_p5m["remarks"][0]["suppressed"] == 1 and _p5m["remarks"][0]["open_live_t"] == [iso(_t_un)]
      and _p5m["verdict"]["level"] == "CAUTION",
      f"6е: в группе снимается только эпизод после окна пуска: {_p5m['remarks'][0]}")
# Строка после суточного реза (R1): профиль предка — и для пометки, и для запрета
_p5c, _g5c = remarks6([ep6("RPM_UNDERSPEED", _t_un, None, gate_suppressed=True, gate_suppressed_src=_src5)],
                      [_open_c2])
check(_g5c["RPM_UNDERSPEED"]["suppressed"] == 0 and (_g5c["RPM_UNDERSPEED"].get("start_note") or "").startswith(
      "на пуске: ") and _g5c["RPM_UNDERSPEED"].get("start_unstable") is True,
      f"6е: строка после реза — эпизод пуска с пометкой «на пуске», снятие не действует: {_g5c['RPM_UNDERSPEED']}")
import analytics.serializer as _ser_mod  # noqa: E402
_orig = _ser_mod._start_windows
_ser_mod._start_windows = _boom
LOG.records.clear()
try:
    _p5b, _g5b = remarks6([ep6("RPM_UNDERSPEED", _t_un, M15(12, 43, 9), gate_suppressed=True,
                               gate_suppressed_src=_src5)])
    check(_g5b["RPM_UNDERSPEED"]["suppressed"] == 1
          and logged("Запрет снятия на неустойчивом пуске в сводке не проверен"),
          "6е: сбой проверки запрета в сводке — сводка как раньше, запись в лог")
except Exception as e:  # noqa: BLE001
    check(False, f"6е: сбой проверки запрета уронил сводку: {e!r}")
finally:
    _ser_mod._start_windows = _orig

# ── 6ж. Ревью R9: в окне пуска начались не все эпизоды группы — «из них на пуске N» ──
_E9 = [ep6("RPM_UNDERSPEED", _t_un, M15(12, 43, 9)),
       ep6("RPM_UNDERSPEED", M15(12, 43, 40), M15(12, 43, 50), gate_suppressed=True,
           gate_suppressed_src={"ts": iso(M15(12, 44, 0)), "segment_id": None}),
       ep6("RPM_UNDERSPEED", M15(12, 44, 10), M15(12, 44, 20))]
_p9, _g9 = remarks6(_E9)
_g9u = _g9["RPM_UNDERSPEED"]
_note9 = "из них на пуске 1: " + _note[len("на пуске: "):]
check(_g9u["count"] == 3 and _g9u.get("start_n") == 1 and _g9u.get("start_note") == _note9
      and _g9u.get("start_unstable") is True and _g9u.get("start_ban_t") == [iso(_t_un)] and _g9u["suppressed"] == 1,
      f"6ж: 1 эпизод из 3 в окне пуска — «{_note9}»: {_g9u.get('start_note')}, start_n {_g9u.get('start_n')}")
_md9 = render_summary_md(_p9)
_hd9 = _remarks_from_summary({"report_summary_json": _p9}) or ""
check(f" — {_note9}" in _md9 and _note not in _md9 and "из них на пуске 1:" in _hd9,
      f"6ж: сводка и шапка заключения — «из них на пуске 1»: "
      f"{[ln for ln in _md9.splitlines() if 'пуске' in ln]}")
check(_g6["RPM_UNDERSPEED"].get("start_n") == 1 and _g6["RPM_UNDERSPEED"].get("start_note") == _note,
      "6ж: все эпизоды группы в окне пуска — пометка прежняя («на пуске: …»)")

# ══ 7. Подпись плашки попытки (п.5, решение В4) ═════════════════════════════
import ast  # noqa: E402

from analytics.labels import (CRANK_RPM_MIN, attempt_cranked, attempt_facts_ru, attempt_label,  # noqa: E402
                              label_from_chars)
from analytics.segmenter import confirm_params  # noqa: E402

BAT, SW, RC, LFC, LFT = (CFG.role_to_addr(r) for r in ("BATTERY_V", "SWITCH_POS", "RUN_COMMAND",
                                                         "LAST_FAULT_CODE", "LAST_FAULT_TYPE"))

# ── 7а. Порог прокрутки — ключ KB, одинаковый у пар и монолита ──
check(confirm_params(CFG)["crank_rpm_min"] == 50 and confirm_params(CFG3)["crank_rpm_min"] == 50
      and confirm_params(MONO)["crank_rpm_min"] == 50 and CRANK_RPM_MIN == 50
      and CFG.seg("transition_confirm", "crank_rpm_min") == 50
      and MONO.seg("transition_confirm", "crank_rpm_min") == 50,
      "7а: transition_confirm.crank_rpm_min = 50 в _defaults и в монолите")
check((BAT, SW, RC) == (40061, 40010, 40599), f"7а: АКБ, переключатель и команда — из карты: {(BAT, SW, RC)}")


def per7(addr, seq, labels=None):
    """enum-периоды подряд: seq — [(момент, значение)], метка — как у каталога."""
    return [{"addr": addr, "value": v, "state_start": t,
             "state_end": seq[i + 1][0] if i + 1 < len(seq) else None,
             "label": (labels or {}).get(v)} for i, (t, v) in enumerate(seq)]


# Реальные ДГУ №1 16.10 (МСК): плашки 09:27:23 (#35935) и 09:27:55 (#35937)
_SWL = {0: "Выкл", 1: "Авто", 2: "Ручной"}
_RCL = {0: "EmergencyStop", 1: "Стоп", 2: "Останов по Load Demand", 3: "Работа"}   # 0 — каталог до 17.10
_EN7 = (per7(40011, [(M(16, 9, 26, 23), 0), (M(16, 9, 27, 23), 3), (M(16, 9, 27, 43), 0),
                     (M(16, 9, 27, 55), 3), (M(16, 9, 28, 2), 0)])
        + per7(SW, [(M(16, 9, 26, 43), 0), (M(16, 9, 27, 23), 2), (M(16, 9, 27, 43), 0),
                    (M(16, 9, 27, 55), 2), (M(16, 9, 28, 2), 0)], _SWL)
        + per7(RC, [(M(16, 9, 26, 23), 0), (M(16, 9, 27, 23), 3), (M(16, 9, 28, 2), 1),
                    (M(16, 9, 28, 12), 0)], _RCL)
        + per7(LFC, [(M(16, 9, 26, 43), 0)], {0: "Нет"}))
_H7 = sorted([h(RPM, M(16, 9, 26, 32), 0.0), h(RPM, M(16, 9, 27, 23), 171.0), h(RPM, M(16, 9, 27, 32), 189.0),
              h(RPM, M(16, 9, 27, 43), 0.0), h(RPM, M(16, 9, 27, 52), 187.0), h(RPM, M(16, 9, 28, 2), 107.0),
              h(RPM, M(16, 9, 28, 12), 0.0), h(FREQ, M(16, 9, 26, 23), 0.0)]
             + [h(BAT, M(16, 9, 27, s), v) for s, v in (
                 (3, 25.5), (12, 25.6), (23, 21.5), (32, 22.0), (43, 24.9), (52, 22.0))]
             + [h(BAT, M(16, 9, 28, s), v) for s, v in ((2, 24.7), (12, 25.0), (22, 25.1))],
             key=lambda r: r["ts"])


def seg7(enum=_EN7, hist=_H7, cfg=CFG, faults=(), a=M(16, 9, 26, 30), b=M(16, 9, 28, 30)):
    return segment(list(enum), list(hist), list(faults), [], cfg, "SN", "pcc", 1, "E", a, b)


def plates(segs):
    return [s for s in segs if s.attempt]


_pl7 = plates(seg7())
_a35, _a37 = (_pl7[0].attempt, _pl7[1].attempt) if len(_pl7) == 2 else ({}, {})
check([label_from_chars(s.to_dict()) for s in _pl7] == ["Неудачный пуск · Стоп"] * 2,
      f"7б: #35935 и #35937 — «Неудачный пуск · Стоп» (было «Неудачная работа · Стоп»): "
      f"{[label_from_chars(s.to_dict()) for s in _pl7]}")
_f35, _f37 = attempt_facts_ru(_a35, MSK), attempt_facts_ru(_a37, MSK)
check(_f35 == "прокрутка 20 с до 189 об/мин без вспышки, АКБ до 21,5 В, переключатель «Выкл» 09:27:43, "
              "команда работы — «Работа»",
      f"7б: #35935 — целевой текст плана: {_f35}")
check(_f37 == "прокрутка 10 с до 187 об/мин без вспышки, АКБ до 22,0 В, переключатель «Выкл» и "
              "команда работы «Стоп» 09:28:02",
      f"7б: #35937 — «прокрутка 10 с до 187 об/мин, «Выкл» и команда «Стоп» 09:28:02»: {_f37}")
check(_a35.get("cranked") is True and _a35.get("crank_from") == iso(M(16, 9, 27, 23)) and _a35.get("crank_sec") == 20.0
      and _a35.get("battery_min_v") == 21.5
      and _a35.get("switch") == {"value": 0, "label": "Выкл", "at": iso(M(16, 9, 27, 43))}
      and _a35.get("run_command") == {"value": 3, "label": "Работа", "at": None} and _a35.get("codes") == [],
      f"7б: #35935 — поля записи: {_a35}")
check(_a37.get("crank_from") == iso(M(16, 9, 27, 52)) and _a37.get("crank_sec") == 10.0
      and _a37.get("battery_min_v") == 22.0 and _a37.get("t_start") == iso(M(16, 9, 27, 55))
      and _a37.get("run_command") == {"value": 1, "label": "Стоп", "at": iso(M(16, 9, 28, 2))},
      f"7б: #35937 — прокрутка с удержанного отсчёта 09:27:52 (187 об/мин) до падения: {_a37}")
check(not any(w in (_f35 + _f37).lower() for w in ("оператор", "пауз", "цикл", "прервал", "отмен")),
      "7б: в фактах только наблюдаемое — без «прервал оператор» / «пауза цикла»")
# Код следующей попытки в лаге после падения — не код этой плашки: 234 разноса
# в 09:28:53 (пуск 09:28:33) — через 51 с после падения #35937 (перепрогон брал его)
_EN7x = ([p for p in _EN7 if p["addr"] not in (40011, LFC)]
         + per7(40011, [(M(16, 9, 26, 23), 0), (M(16, 9, 27, 23), 3), (M(16, 9, 27, 43), 0), (M(16, 9, 27, 55), 3),
                        (M(16, 9, 28, 2), 0), (M(16, 9, 28, 33), 3), (M(16, 9, 28, 53), 0)])
         + per7(LFC, [(M(16, 9, 26, 43), 0), (M(16, 9, 28, 53), 234)], {0: "Нет", 234: "Разнос (ОСТАНОВ)"}))
_H7x = sorted(_H7 + [h(RPM, M(16, 9, 28, 32), 203.0), h(RPM, M(16, 9, 28, 42), 1294.0),
                     h(RPM, M(16, 9, 28, 52), 898.0), h(FREQ, M(16, 9, 28, 42), 42.6)], key=lambda r: r["ts"])
_pl7x = plates(seg7(_EN7x, _H7x, b=M(16, 9, 29, 10)))
check(len(_pl7x) == 2 and _pl7x[1].attempt["codes"] == [] and "код" not in attempt_facts_ru(_pl7x[1].attempt, MSK,
                                                                                            codes=True),
      f"7б: #35937 — код 234 следующей попытки (09:28:53) не её: {[s.attempt['codes'] for s in _pl7x]}")
# 40599 = 0 в кадре падения: метка KB поверх каталога «EmergencyStop»
_EN7z = [p for p in _EN7 if p["addr"] != RC] + per7(RC, [(M(16, 9, 26, 23), 0), (M(16, 9, 27, 23), 3),
                                                         (M(16, 9, 27, 43), 0)], _RCL)
_a35z = plates(seg7(_EN7z))[0].attempt
check(_a35z["run_command"] == {"value": 0, "label": "нет команды (0)", "at": iso(M(16, 9, 27, 43))}
      and "переключатель «Выкл» и команда работы «нет команды (0)» 09:27:43" in attempt_facts_ru(_a35z, MSK),
      f"7б: 40599 = 0 — «нет команды (0)» из KB, а не «EmergencyStop» каталога: {_a35z['run_command']}")
# Нет 40010/40599 в окне или NA — сигналов в фактах нет
_EN7n = [p for p in _EN7 if p["addr"] not in (SW, RC)] + per7(SW, [(M(16, 9, 27, 0), 65535)])
_a35n = plates(seg7(_EN7n))[0].attempt
check(_a35n["switch"] is None and _a35n["run_command"] is None
      and attempt_facts_ru(_a35n, MSK) == "прокрутка 20 с до 189 об/мин без вспышки, АКБ до 21,5 В",
      f"7б: нет 40599, 40010 = NA — сигналов нет: {attempt_facts_ru(_a35n, MSK)}")

# ── 7в. Матрица attempt_label: режим × прокрутка × сигналы × исход × коды ──
_SIG7 = [{}, {"switch": {"value": 0, "label": "Выкл", "at": iso(M(16, 9, 27, 43))},
              "run_command": {"value": 1, "label": "Стоп", "at": iso(M(16, 9, 27, 43))}},
         {"switch": {"value": 2, "label": "Ручной", "at": None},
          "run_command": {"value": 3, "label": "Работа", "at": None}}]
_bad7, _n7 = [], 0
for _fs in (1, 2, 3):
    for _ck, _rpm in (("да", 189.0), ("да", 50.5), ("нет", 50.0), ("нет", 0.0), ("?", None)):
        for _sig in _SIG7:
            for _oc in ("SIMPLE", "EMERGENCY"):
                for _codes in ([], [1438]):
                    for _new in (True, False):      # запись v5.1.23 / до неё (без cranked)
                        _att = {"from_state": _fs, "path": [_fs], "outcome": _oc, "codes": _codes,
                                "facts": {"rpm_max": _rpm, "freq_max": 0.0, "ready": False}, **_sig}
                        if _new:
                            _att["cranked"] = (_rpm > 50) if _rpm is not None else None
                        _want = ("Отменённый пуск" if _ck == "нет" and not _codes and _oc == "SIMPLE"
                                 else "Неудачный пуск") + (" · Авария" if _oc == "EMERGENCY" else " · Стоп")
                        _n7 += 1
                        if attempt_label(_att) != _want:
                            _bad7.append((_fs, _rpm, bool(_sig), _oc, _codes, _new, attempt_label(_att), _want))
check(_n7 == 360 and not _bad7, f"7в: матрица подписей ({_n7} сочетаний) — расхождения: {_bad7[:5]}")
check(attempt_label(None) is None and attempt_label({"outcome": "SIMPLE"}) == "Неудачный пуск · Стоп",
      "7в: не попытка — None; без оборотов — «Неудачный пуск» (отмены не видно)")
# Старая запись (#35935 в БД, v5.1.22): подпись по rpm_max, факты прежние
_OLD35 = {"path": [3], "codes": [], "facts": {"ready": False, "rpm_max": 189.0, "freq_max": 0.0},
          "t_fall": "2026-10-16T06:27:43+00:00", "outcome": "SIMPLE", "t_start": "2026-10-16T06:27:23+00:00",
          "from_state": 3, "duration_sec": 20.0}
check(attempt_cranked(_OLD35) is True and attempt_label(_OLD35) == "Неудачный пуск · Стоп"
      and attempt_facts_ru(_OLD35) == "попытка 20 с: обороты до 189 об/мин, частоты нет, «Готов к нагрузке» не выставлен",
      f"7в: старая запись — подпись по rpm_max, факты как раньше: {attempt_label(_OLD35)} / {attempt_facts_ru(_OLD35)}")
check(attempt_label(dict(_OLD35, facts={"rpm_max": 0.0})) == "Отменённый пуск · Стоп"
      and attempt_label(dict(_OLD35, facts={})) == "Неудачный пуск · Стоп"
      and attempt_label(dict(_OLD35, facts={"rpm_max": 0.0}, codes=[1438], outcome="EMERGENCY"))
      == "Неудачный пуск · Авария", "7в: старая запись без прокрутки / без оборотов / с кодом и аварией")

# ── 7г. Подпись при чтении: календарь (web/routes._seg_label), статус ──
from analytics.serializer import RUN_STATE_RU  # noqa: E402

_rsrc = (ROOT / "web" / "routes.py").read_text(encoding="utf-8")
_fn = next(n for n in ast.parse(_rsrc).body if isinstance(n, ast.FunctionDef) and n.name == "_seg_label")
_ns7: dict = {"_STOP_KIND_LABELS": {}, "_RUN_STATE_LABELS": RUN_STATE_RU}
exec(compile(ast.Module(body=[_fn], type_ignores=[]), "routes._seg_label", "exec"), _ns7)
_CH35 = {"attempt": _OLD35, "stop_kind": "SIMPLE"}
check(_ns7["_seg_label"](0, "SIMPLE", _CH35) == "Неудачный пуск · Стоп",
      f"7г: календарь — подпись закрытой #35935 пересчитана при чтении: {_ns7['_seg_label'](0, 'SIMPLE', _CH35)}")
_st7 = build_structural_status({"id": 1, "run_state": 0, "t_start": M(16, 9, 27, 23), "characteristics_json": _CH35,
                                "current_values_json": {"values": {}}, "active_detections_json": []}, None)
check(_st7.get("mode_label") == "Неудачный пуск · Стоп", f"7г: строка статуса: {_st7.get('mode_label')}")

# ── 7д. Граница порога и ключ KB ──
_H7b = [r for r in _H7 if not (r["addr"] == RPM and M(16, 9, 27, 23) <= r["ts"] <= M(16, 9, 27, 32))]
for _v, _lab in ((50.0, "Отменённый пуск · Стоп"), (51.0, "Неудачный пуск · Стоп")):
    _pp7 = plates(seg7(hist=sorted(_H7b + [h(RPM, M(16, 9, 27, 32), _v)], key=lambda r: r["ts"]),
                       b=M(16, 9, 27, 50)))
    check(len(_pp7) == 1 and label_from_chars(_pp7[0].to_dict()) == _lab
          and _pp7[0].attempt["cranked"] is (_v > 50),
          f"7д: прокрутка {_v} об/мин — «{_lab}» (порог строго выше 50): "
          f"{[label_from_chars(s.to_dict()) for s in _pp7]}")
_pk7 = plates(seg7(cfg=cfg_seg(CFG, "transition_confirm", crank_rpm_min=200)))
check([label_from_chars(s.to_dict()) for s in _pk7] == ["Отменённый пуск · Стоп"] * 2
      and "попытка 20 с без прокрутки (обороты до 189 об/мин)" in attempt_facts_ru(_pk7[0].attempt, MSK),
      f"7д: порог из KB (200) — 189 об/мин не прокрутка: {[label_from_chars(s.to_dict()) for s in _pk7]}")

# ── 7е. Без прокрутки, код 1438, авария (как ДЭС №3 13.10) — «Неудачный пуск · Авария» ──
from corpus.worker import _attempt_conclusion  # noqa: E402

_T7E = M(13, 10, 40, 1)
_EN7e = (per7(40011, [(S(_T7E, -500), 0), (_T7E, 3), (S(_T7E, 10), 0)])
         + per7(LFT, [(S(_T7E, -500), 0), (S(_T7E, 10), 4)])
         + per7(LFC, [(S(_T7E, -500), 0), (S(_T7E, 10), 1438)], {0: "Нет", 1438: "Отказ прокрутки стартером (ОСТАНОВ)"}))
_F7e = [{"addr": 40407, "bit": 8, "severity": "shutdown", "fault_start": S(_T7E, 10), "fault_end": None,
         "fault_name_ru": "Отказ прокрутки стартером", "fault_name": "Отказ прокрутки стартером"}]
_H7e = [h(RPM, S(_T7E, -30), 0.0), h(RPM, S(_T7E, 5), 0.0), h(RPM, S(_T7E, 20), 0.0)]
_pe7 = plates(seg7(_EN7e, _H7e, CFG3, _F7e, S(_T7E, -60), S(_T7E, 120)))
_ae7 = _pe7[0].attempt if _pe7 else {}
check(_pe7 and label_from_chars(_pe7[0].to_dict()) == "Неудачный пуск · Авария" and _ae7.get("cranked") is False
      and _ae7.get("codes") == [1438] and _ae7.get("code_names") == {"1438": "Отказ прокрутки стартером"},
      f"7е: без прокрутки, 1438, авария — «Неудачный пуск · Авария»: {_pe7 and label_from_chars(_pe7[0].to_dict())} {_ae7}")
_fe7 = attempt_facts_ru(_ae7, MSK)
check(_fe7 == "попытка 10 с без прокрутки (обороты 0)"
      and attempt_facts_ru(_ae7, MSK, codes=True) == "попытка 10 с без прокрутки (обороты 0), "
                                                     "код 1438 «Отказ прокрутки стартером»",
      f"7е: факты — коды только по запросу: {_fe7} / {attempt_facts_ru(_ae7, MSK, codes=True)}")
_c7 = _attempt_conclusion({"characteristics_json": _pe7[0].to_dict()}) if _pe7 else None
check(_c7 and _c7["humanized_md"].count("1438") == 1
      and _c7["humanized_md"].startswith("Неудачный пуск · Авария: выход из стопа не подтвердился — "
                                         "попытка 10 с без прокрутки (обороты 0). Останов"),
      f"7е: заключение плашки (worker не правился) — код один раз: {_c7 and _c7['humanized_md']}")
_sum7 = build_summary_parts(_pe7, [], tz=MSK) if _pe7 else {}
check((_sum7.get("attempt") or {}) == {"label": "Неудачный пуск · Авария",
                                       "facts": "попытка 10 с без прокрутки (обороты 0), код 1438 «Отказ прокрутки стартером»"},
      f"7е: сводка — подпись и факты с кодом: {_sum7.get('attempt')}")
_md7 = to_markdown(_pe7, "SN", "pcc", 1, S(_T7E, -60), S(_T7E, 120), tz=MSK)
check("- **Неудачная попытка выхода из стопа:** попытка 10 с без прокрутки (обороты 0), код 1438 "
      "«Отказ прокрутки стартером»; останов" in _md7, "7е: отчёт — строка попытки с кодом")
# Без кода и без аварии — «Отменённый пуск · Стоп»
_EN7o = per7(40011, [(S(_T7E, -500), 0), (_T7E, 3), (S(_T7E, 9), 0)])
_po7 = plates(seg7(_EN7o, _H7e, CFG3, (), S(_T7E, -60), S(_T7E, 120)))
check(_po7 and label_from_chars(_po7[0].to_dict()) == "Отменённый пуск · Стоп"
      and attempt_facts_ru(_po7[0].attempt, MSK) == "попытка 9 с без прокрутки (обороты 0)",
      f"7е: без прокрутки, кода и аварии — «Отменённый пуск · Стоп»: "
      f"{_po7 and (label_from_chars(_po7[0].to_dict()), attempt_facts_ru(_po7[0].attempt, MSK))}")

# ── 7ж. Изоляция: сбой наблюдаемого — плашка как до v5.1.23, подпись по rpm_max ──
_orig = _sg._attempt_observed
_sg._attempt_observed = _boom
LOG.records.clear()
try:
    _px7 = plates(seg7())
    check(len(_px7) == 2 and "cranked" not in _px7[0].attempt
          and label_from_chars(_px7[0].to_dict()) == "Неудачный пуск · Стоп"
          and attempt_facts_ru(_px7[0].attempt, MSK).startswith("попытка 20 с: обороты до 189 об/мин")
          and logged("Наблюдаемое на попытке"),
          f"7ж: сбой наблюдаемого — запись прежняя, подпись по rpm_max, лог: {_px7 and _px7[0].attempt}")
except Exception as e:  # noqa: BLE001
    check(False, f"7ж: сбой наблюдаемого уронил segment(): {e!r}")
finally:
    _sg._attempt_observed = _orig

# ══ 8. Имена кодов и 40599 (п.6, решения В5 и В7) ═══════════════════════════
from analytics.classifier import classify_stop_character  # noqa: E402
from analytics.detectors import _detect_controller_faults  # noqa: E402
from analytics.labels import enum_label, panel_fault_name  # noqa: E402
from analytics.reconstructor import _state_event, build_chronology  # noqa: E402
from analytics.serializer import _det_name  # noqa: E402
from online.status_assembler import format_status_text  # noqa: E402

_N234 = "Разнос двигателя — превышение частоты вращения"
_N1464 = "Load Dump — сигнал разгрузки при перегрузке или пониженной частоте"

# ── 8а. Имя кода из справочника KB ──
check(CFG.fault_code_name(234) == _N234 and CFG3.fault_code_name(234) == _N234
      and MONO.fault_code_name(234) == _N234 and CFG.fault_code_name(1464) == _N1464
      and MONO.fault_code_name(1464) == _N1464,
      f"8а: имена 234 и 1464 из справочника (обе копии): {CFG.fault_code_name(234)} / {MONO.fault_code_name(1464)}")
check(CFG.fault_code_name(99999) is None and CFG.fault_code_name(1982) is None and CFG.fault_code_name("x") is None,
      "8а: кода нет или нет перевода (ru = en) — None, тогда имя бита")


def fp8(addr, bit, sev, a, name):
    return {"addr": addr, "bit": bit, "severity": sev, "fault_start": a, "fault_end": None,
            "fault_name_ru": name, "fault_name": name, "duration_sec": None}


# ── 8б. Детекция панели: код привязан — имя кода и «(код N)»; имя бита — bit_name ──
_t8 = M(16, 9, 28, 53)
_f8 = fp8(40401, 13, "shutdown", _t8, "Скорость/положение КВ: выше нормы")
_d8 = _detect_controller_faults([_f8], {}, S(_t8, -5), S(_t8, 60), CFG,
                                {LFC: per7(LFC, [(S(_t8, -300), 0), (_t8, 234)])}, onset_faults=[_f8])
_v8 = _d8[0].values if _d8 else {}
check(_d8 and _d8[0].fault_codes == [234] and _v8.get("panel_code") == 234 and _v8.get("code_name") == _N234
      and _v8.get("bit_name") == "Скорость/положение КВ: выше нормы"
      and _v8.get("fault_name") == "Скорость/положение КВ: выше нормы"
      and _d8[0].description_key == "CONTROLLER_FAULT.addr_40401_bit_13",
      f"8б: #35941 — код 234 привязан, имя кода в values.code_name, имя бита — bit_name и fault_name: {_v8}")
check(_det_name(_d8[0].to_dict()) == f"{_N234} (код 234)",
      f"8б: отчёт — «{_N234} (код 234)»: {_det_name(_d8[0].to_dict())}")
_d8n = _detect_controller_faults([_f8], {}, S(_t8, -5), S(_t8, 60), CFG, {LFC: per7(LFC, [(S(_t8, -300), 0)])},
                                 onset_faults=[_f8])
check(_d8n and "panel_code" not in _d8n[0].values
      and _det_name(_d8n[0].to_dict()) == "Скорость/положение КВ: выше нормы",
      "8б: код не привязан — имя бита, без «(код …)»")
_old8 = {"scenario": "CONTROLLER_FAULT", "values": {"addr": 40408, "bit": 6, "fault_name": "Сброс нагрузки",
                                                    "panel_code": 1464}}
check(_det_name(_old8) == "Сброс нагрузки" and panel_fault_name(_old8["values"]) == "Сброс нагрузки"
      and panel_fault_name(_old8["values"], CFG.fault_code_name) == f"{_N1464} (код 1464)"
      and panel_fault_name({"fault_name": "X", "bit_name": "X", "panel_code": 99999}) == "X (код 99999)",
      "8б: детекция до v5.1.23 — имя как раньше; со справочником — по коду; кода нет в справочнике — "
      "имя бита и «(код N)»")
_orig = _lbl.panel_fault_name
_lbl.panel_fault_name = _boom
try:
    check(_det_name(_d8[0].to_dict()) == "Скорость/положение КВ: выше нормы",
          "8б: сбой имени по коду — имя бита, отчёт строится")
finally:
    _lbl.panel_fault_name = _orig

# ── 8в. Сводка: имя замечания по коду, группа та же (addr, bit) ──
def ep8(addr, bit, t0, ov, sev="WARNING"):
    return {"source": "panel", "scenario": "CONTROLLER_FAULT", "severity": sev, "addr": addr, "bit": bit,
            "gate_suppressed": False, "t_open": t0, "t_close": S(t0, 30), "active_sec": 30.0, "blind_sec": 0.0,
            "open_values_json": json.dumps(ov, ensure_ascii=False)}


def names8(eps, segs=None, **kw):
    p = build_summary_parts(segs or [_w3], eps, tz=MSK, **kw)
    return [(g.get("name"), g.get("count")) for g in p["remarks"] if g.get("source") == "panel"]


_t44 = M(16, 9, 29, 42)
_ov_old = {"addr": 40408, "bit": 6, "fault_name": "Сброс нагрузки", "panel_code": 1464}   # #14929 в БД
check(names8([ep8(40408, 6, _t44, _ov_old)]) == [("Сброс нагрузки", 1)],
      f"8в: эпизод до v5.1.23 без справочника — имя как раньше: {names8([ep8(40408, 6, _t44, _ov_old)])}")
check(names8([ep8(40408, 6, _t44, _ov_old)], code_name=CFG.fault_code_name) == [(f"{_N1464} (код 1464)", 1)],
      f"8в: #35944 — «{_N1464} (код 1464)» (справочник движка)")
_ov_new = dict(_v8, fault_start=iso(_t8))
check(names8([ep8(40401, 13, _t8, _ov_new, "SHUTDOWN")]) == [(f"{_N234} (код 234)", 1)],
      f"8в: #35941 — эпизод v5.1.23 — имя кода из open_values: {names8([ep8(40401, 13, _t8, _ov_new, 'SHUTDOWN')])}")
check(names8([ep8(40401, 13, _t8, _ov_new, "SHUTDOWN"),
              ep8(40401, 13, S(_t8, 600), {"fault_name": "Скорость/положение КВ: выше нормы"}, "SHUTDOWN")])
      == [(f"{_N234} (код 234)", 2)], "8в: два фронта бита (с кодом и без) — одна группа (addr, bit не менялись)")
# Эпизод без кода в open_values (синтетический / код встал циклом позже) — код у детекции того же фронта
_w8 = copy.deepcopy(_w3)
_w8.subsegments[0].detections.append(_d8[0])
check(names8([ep8(40401, 13, _t8, {"fault_name": "Скорость/положение КВ: выше нормы"}, "SHUTDOWN")], [_w8])
      == [(f"{_N234} (код 234)", 1)]
      and names8([ep8(40401, 13, S(_t8, 5), {"fault_name": "Скорость/положение КВ: выше нормы"}, "SHUTDOWN")],
                 [_w8]) == [("Скорость/положение КВ: выше нормы", 1)],
      "8в: эпизод без кода — код и имя у детекции того же фронта (addr, bit, начало), чужой фронт — нет")
check("_front_code" in inspect.getsource(build_summary_parts)
      and "code_name=getattr(self.cfg, \"fault_code_name\", None)" in (ROOT / "online" / "engine.py").read_text(
          encoding="utf-8"), "8в: движок передаёт справочник имён кодов в сводку")

# ── 8г. 1464 и 1992 в справочнике (обе копии), строка статуса ──
_fc_l = (KB / "controllers" / "pcc3300" / "pcc3300_fault_codes.json").read_bytes()
_fc_m = (KB / "equipment" / "cummins_kta50_pcc3300" / "pcc3300_fault_codes.json").read_bytes()
_idx8 = {e["code"]: e for e in json.loads(_fc_l.decode("utf-8"))["fault_codes"]}
check(_fc_l == _fc_m, "8г: копии справочника кодов (слой и монолит) совпадают")
check(_idx8[1464]["description"]["ru"] == _N1464 and _idx8[1464]["description"]["en"] == "Load Dump Fault"
      and _idx8[1464]["severity"] == "wa", f"8г: 1464 — «{_N1464}»: {_idx8[1464]['description']}")
_e1992 = _idx8[1992]
check(_e1992["description"] == {"en": "Crankshaft Sensor High",
                                "ru": "Разнос по датчику коленвала — обороты выше уставки"}
      and _e1992["severity"] == "sh" and not _e1992["documentation"]["causes"]
      and not _e1992["documentation"]["steps"] and "картер" not in json.dumps(_e1992["documentation"]["causes"]
                                                                            + _e1992["documentation"]["steps"])
      and "0900-0670" in _e1992["documentation"]["source"] and "Modbus" in _e1992["documentation"]["source"]
      and "external" not in _e1992["documentation"]["source"] and _e1992["documentation"]["related"] == "234",
      f"8г: 1992 (В7) — «Crankshaft Sensor High», причины про картер убраны, источник 0900-0670 и Modbus 3.3: {_e1992}")
_row8 = {"id": 1, "run_state": 3, "t_start": _t44, "characteristics_json": {}, "current_values_json": {"values": {}},
         "active_detections_json": [{"scenario": "CONTROLLER_FAULT", "severity": "WARNING", "fault_codes": [1464],
                                     "trigger": "Сброс нагрузки (addr=40408, bit=6)",
                                     "values": {"addr": 40408, "bit": 6, "fault_name": "Сброс нагрузки"}}]}
_txt8 = format_status_text(build_structural_status(_row8, CFG._fault_ref()))
check(f"код 1464 «{_N1464}»" in _txt8, f"8г: статус — «код 1464 «{_N1464}»»: {_txt8}")

# ── 8д. 40599: описание и метка KB поверх каталога (лента акта) ──
_D40599 = "Команда работы панели (0 — нет команды, 1 — Стоп, 2 — останов по Load Demand, 3 — Работа)"
check(all((c.register_map.get(40599) or {}).get("description") == _D40599
          and (c.register_map.get(40599) or {}).get("labels_ru") == {0: "нет команды (0)"} for c in (CFG, CFG3, MONO)),
      "8д: 40599 — описание и labels_ru {0: «нет команды (0)»} у обеих пар и в монолите")
_reg8 = CFG.register_map
_ev = {v: _state_event({"addr": 40599, "value": v, "state_start": _t8, "state_end": None, "label": lab}, _reg8)
       for v, lab in ((0, "EmergencyStop"), (1, "Стоп"), (3, None))}
check(_ev[0]["label"] == "нет команды (0)" and _ev[0]["name"] == _D40599 and _ev[1]["label"] == "Стоп"
      and _ev[3]["label"] is None,
      f"8д: лента — 0 «нет команды (0)» поверх каталога, 1 — из каталога, без метки — как раньше: "
      f"{[(v, e['label']) for v, e in _ev.items()]}")
check(_state_event({"addr": 40010, "value": 0, "state_start": _t8, "label": "Выкл"}, _reg8)["label"] == "Выкл",
      "8д: регистр без labels_ru в KB — метка каталога")
_chr8 = build_chronology(per7(40599, [(_t8, 3), (S(_t8, 9), 0)], _RCL), [], CFG)
check([e["label"] for e in _chr8] == ["Работа", "нет команды (0)"], f"8д: build_chronology: {[e['label'] for e in _chr8]}")
check(enum_label({"labels_ru": {"0": "a"}}, 0, "b") == "a" and enum_label({}, 0, "b") == "b"
      and enum_label({}, 0, None) == "0" and enum_label({}, 0, None, fallback_value=False) is None
      and enum_label(None, None, None) is None, "8д: порядок меток: KB → каталог → значение")

# ── 8е. Голос характера по 40599 ──
_t8s = M(16, 9, 28, 53)
_ch8 = classify_stop_character(per7(40011, [(S(_t8s, -20), 3), (_t8s, 0)]) + per7(40599, [(S(_t8s, -21), 3), (_t8s, 0)]),
                               _t8s)
_ch8b = classify_stop_character(per7(40011, [(S(_t8s, -20), 3), (_t8s, 0)])
                                + per7(40599, [(S(_t8s, -21), 3), (S(_t8s, -2), 1), (_t8s, 0)]), _t8s)
check("команда «Работа» снята сразу (3→0), без «Стоп»" in _ch8["immediate_votes"]
      and not any("EmergencyStop" in v for v in _ch8["immediate_votes"] + _ch8["controlled_votes"])
      and _ch8["signals"]["run_command_path"] == "direct_estop" and _ch8["character"] == "immediate",
      f"8е: #35941 — голос «команда «Работа» снята сразу (3→0), без «Стоп»»: {_ch8['immediate_votes']}")
check("RunCommand через «Стоп»" in _ch8b["controlled_votes"]
      and not any("снята сразу" in v for v in _ch8b["immediate_votes"]),
      f"8е: операторский 3→1→0 — голос «через «Стоп»»: {_ch8b['controlled_votes']}")

# ── 8ж. Имя по коду в Блоке 1 заключения (R10), пакетной сводке (P1) и бегущей строке UI (P2) ──
def hier8(*dd):
    return _fmt_detections_hierarchy(json.dumps({"subsegments": [
        {"duration_sec": 20.0, "detections": [d.to_dict() if hasattr(d, "to_dict") else d for d in dd]}]},
        ensure_ascii=False, default=str))


_h8 = hier8(_d8[0])
_l8 = next((ln for ln in _h8.splitlines() if "срабатыван" in ln), "")
check(f"{_N234} — 1 срабатывание (код 234)" in _l8 and _l8.count("(код 234)") == 1
      and "Скорость/положение КВ" not in _h8,
      f"8ж: Блок 1 заключения — имя кода и «(код N)» один раз, как в сводке (ревью R10): {_l8}")
_l8n = next((ln for ln in hier8(_d8n[0]).splitlines() if "срабатыван" in ln), "")
check("Скорость/положение КВ: выше нормы — 1 срабатывание" in _l8n and "(код" not in _l8n,
      f"8ж: Блок 1 — код не привязан — имя бита, как было: {_l8n}")
_l8b = [ln for ln in hier8(_d8[0], _d8n[0]).splitlines() if "срабатыван" in ln]
check(len(_l8b) == 1 and f"{_N234} — 2 срабатывания" in _l8b[0] and _l8b[0].endswith("(код 234)")
      and _l8b[0].count("(код 234)") == 1,
      f"8ж: Блок 1 — детекции одного бита с кодом и без — одна группа: {_l8b}")
from analytics.serializer import build_summary_md  # noqa: E402
_eo8 = [ep8(40408, 6, _t44, _ov_old)]
check(f"{_N1464} (код 1464)" in build_summary_md([_w3], _eo8, MSK, code_name=CFG.fault_code_name)
      and "Сброс нагрузки" in build_summary_md([_w3], _eo8, MSK),
      "8ж: пакетная сводка (build_summary_md) — справочник кодов, как у движка (P1)")
# web/routes без fastapi не импортируется — функция берётся из исходника (как в 7г)
_fn8 = next(n for n in ast.parse(_rsrc).body if isinstance(n, ast.FunctionDef) and n.name == "_alarm_name")
_ns8: dict = {"_parse_json": lambda v, *a, **k: json.loads(v) if isinstance(v, str) else v,
              "logger": logging.getLogger("web")}
exec(compile(ast.Module(body=[_fn8], type_ignores=[]), "routes._alarm_name", "exec"), _ns8)
_an8 = _ns8["_alarm_name"]
_orig = _mgr.get_manager
_mgr.get_manager = lambda: types.SimpleNamespace(_engines={"k": types.SimpleNamespace(cfg=CFG)})
try:
    _an = {
        "new": _an8({"source": "panel", "open_values_json": json.dumps(_ov_new, ensure_ascii=False)}),
        "old": _an8({"source": "panel", "open_values_json": json.dumps(_ov_old, ensure_ascii=False)}, "k"),
        "old_noeng": _an8({"source": "panel", "open_values_json": json.dumps(_ov_old)}, "x"),
        "nocode": _an8({"source": "panel", "open_values_json": json.dumps(
            {"fault_name": "Скорость/положение КВ: выше нормы"})}, "k"),
        "analytics": _an8({"source": "analytics", "scenario": "RPM_UNDERSPEED",
                           "open_values_json": json.dumps({"live": {}})}, "k"),
    }
finally:
    _mgr.get_manager = _orig
check(_an == {"new": f"{_N234} (код 234)", "old": f"{_N1464} (код 1464)", "old_noeng": "Сброс нагрузки",
              "nocode": "Скорость/положение КВ: выше нормы", "analytics": None}
      and '"name":            _alarm_name(e, key),' in _rsrc,
      f"8ж: бегущая строка UI (active_alarms[].name) — по коду, как в карточке; без кода — как было (P2): {_an}")

# ══ 9. Изоляция ключей segment_spans (п.7) ══════════════════════════════════
check(not hasattr(_lv, "LIVE_KEYS"), "9: неиспользуемая константа LIVE_KEYS удалена")

# Окно, где срабатывают все четыре ключа (ДЭС №3): масло 99,0007 у уставки
# [100, 400), ОЖ 94 у HCT [300, 500), паспорт 236 < 242 [560, 640), провал
# оборотов 1418 при 47,3 Гц в 900
_ROWS9 = work_rows(extra=[
    h(OILT, S(L0, 0), 95.0), h(OILT, S(L0, 100), 99.0007), h(OILT, S(L0, 400), 97.0),
    h(COOL3, S(L0, 0), 85.0), h(COOL3, S(L0, 300), 94.0), h(COOL3, S(L0, 500), 85.0),
    h(PRESS3, S(L0, 0), 250.0), h(PRESS3, S(L0, 560), 236.0), h(PRESS3, S(L0, 640), 250.0),
    h(RPM, S(L0, 900), 1418.0), h(FREQ, S(L0, 900), 47.3),
])
_KEYS = ("RPM_UNDERSPEED", "LIMIT_PROXIMITY", "COOLING_FAILURE", "OIL_DILUTION")


def spans9(rows=_ROWS9, cfg=CFG3):
    return segment_spans(by_addr(rows), None, cfg, L0, L1, [], {}, False, horizon=None)


base9 = spans9()
check(all(base9.get(k) for k in _KEYS),
      f"9а: без сбоев — участки всех четырёх ключей: {[(k, len(base9.get(k) or [])) for k in _KEYS]}")
# Ключ заброса (этап 2): его сбой не гасит четыре прежних ключа
_orig = _lv._over_points
_lv._over_points = _boom
try:
    _r9 = spans9()
    check(_r9.get("RPM_OVERSPEED", "нет") is None and all(_r9.get(k) == base9[k] for k in _KEYS),
          "9а: сбой ключа заброса не гасит просадку, близость, ОЖ и паспорт")
finally:
    _lv._over_points = _orig

# (имя функции модуля live, ключ, который она ломает)
_BREAKS = (("_rpm_points", "RPM_UNDERSPEED"), ("limit_checks", "LIMIT_PROXIMITY"),
           ("cooling_proximity", "COOLING_FAILURE"), ("_passport_spans", "OIL_DILUTION"))
for _fn, _key in _BREAKS:
    _orig = getattr(_lv, _fn)
    setattr(_lv, _fn, _boom)
    LOG.records.clear()
    try:
        _r = spans9()
        check(_key in _r and _r[_key] is None, f"9а: сбой {_fn} — ключ {_key} = None: {_r.get(_key, 'нет ключа')}")
        check(all(_r.get(k) == base9[k] for k in _KEYS if k != _key),
              f"9а: сбой {_key} не гасит соседей: {[(k, bool(_r.get(k))) for k in _KEYS]}")
        check(logged(f"Участки ключа {_key}"), f"9а: сбой {_key} — запись в лог")
    except Exception as e:  # noqa: BLE001
        check(False, f"9а: сбой {_fn} уронил segment_spans: {e!r}")
    finally:
        setattr(_lv, _fn, _orig)

# ── 9б. Детекторы: ключ None — по подсегменту, соседи — по участкам ──
DM9 = DerivedMetrics(oil_press_passport_kpa=242, oil_press_step_kpa=4, oil_press_step_source="default")
CH9 = {"RPM": {"min": 1418.0, "max": 1501.0, "median": 1500.0},
       "OIL_TEMP": {"min": 95.0, "max": 99.5, "median": 97.0},
       "COOLANT_TEMP": {"min": 85.0, "max": 94.0, "median": 85.0, "value_start": 85.0, "value_end": 85.0},
       "OIL_PRESS": {"min": 236.0, "max": 250.0, "median": 250.0}}


def run9(live, derived=DM9, cfg=CFG3, chars=CH9, a=L0, b=L1):
    return {d.scenario: d for d in run_all_detectors(chars, derived, RiskAccumulators(), [], "NORMAL", 3,
                                                       a, b, None, cfg, live_spans=live)}


d_all = run9(base9)
check(all(k in d_all and "live" in d_all[k].values for k in _KEYS),
      f"9б: без сбоев — все четыре по участкам: {[(k, k in d_all and 'live' in d_all[k].values) for k in _KEYS]}")
d_rpm = run9(dict(base9, RPM_UNDERSPEED=None))
check("RPM_UNDERSPEED" in d_rpm and "live" not in d_rpm["RPM_UNDERSPEED"].values
      and d_rpm["RPM_UNDERSPEED"].t_detected == iso(L0) and "RPM мин. 1418" in d_rpm["RPM_UNDERSPEED"].trigger,
      f"9б: сбой просадки — по подсегменту (min 1418), начало подсегмента: "
      f"{d_rpm.get('RPM_UNDERSPEED') and d_rpm['RPM_UNDERSPEED'].trigger}")
check(all(k in d_rpm and "live" in d_rpm[k].values for k in ("LIMIT_PROXIMITY", "COOLING_FAILURE", "OIL_DILUTION")),
      "9б: сбой просадки — близость, ОЖ и паспорт по участкам")
d_lp = run9(dict(base9, LIMIT_PROXIMITY=None))
check("LIMIT_PROXIMITY" in d_lp and "live" not in d_lp["LIMIT_PROXIMITY"].values
      and all(k in d_lp and "live" in d_lp[k].values for k in ("RPM_UNDERSPEED", "OIL_DILUTION")),
      "9б: сбой близости — она по подсегменту, просадка и паспорт по участкам")
DM9_OLD = DerivedMetrics(oil_press_passport_kpa=242, oil_press_step_kpa=4, oil_press_step_source="default",
                         oil_press_below_passport_run_sec=80, oil_press_below_passport_min_kpa=236,
                         oil_press_hard_run_sec=80, oil_press_hard_min_kpa=236,
                         oil_press_passport_until=iso(S(L0, 640)), oil_press_passport_ongoing=False)
d_pp = run9(dict(base9, OIL_DILUTION=None), derived=DM9_OLD)
check("OIL_DILUTION" in d_pp and d_pp["OIL_DILUTION"].source == "PASSPORT_THRESHOLD"
      and "live" not in d_pp["OIL_DILUTION"].values and d_pp["OIL_DILUTION"].t_detected == iso(L0)
      and all(k in d_pp and "live" in d_pp[k].values for k in ("RPM_UNDERSPEED", "LIMIT_PROXIMITY")),
      "9б: сбой паспорта — паспорт по DerivedMetrics, просадка и близость по участкам")
# Ключа нет (не сбой, а нет участков) — детекции нет, как в v5.1.21
d_none = run9({k: v for k, v in base9.items() if k != "RPM_UNDERSPEED"})
check("RPM_UNDERSPEED" not in d_none, "9б: ключа нет — детекции нет (не путать со сбоем)")

# ── 9в. Через segment(): сбой одного ключа не гасит остальные ──
RS_REP = [rs(3, T(0, 0))]       # режим начался до окна — REPORT_START
for _fn, _key in _BREAKS:
    _orig = getattr(_lv, _fn)
    setattr(_lv, _fn, _boom)
    LOG.records.clear()
    try:
        _segs = seg3(_ROWS9, RS_REP)
        _by = {k: dets(_segs, k) for k in _KEYS}
        _live_ok = {k: bool(_by[k]) and all("live" in d.values for _, d in _by[k]) for k in _KEYS}
        _old_ok = bool(_by[_key]) and all("live" not in d.values for _, d in _by[_key])
        _need = [k for k in ("RPM_UNDERSPEED", "LIMIT_PROXIMITY", "OIL_DILUTION") if k != _key]
        check(all(_live_ok[k] for k in _need),
              f"9в: сбой {_key} — {', '.join(_need)} по участкам: {_live_ok}")
        check(_old_ok or (_key == "COOLING_FAILURE" and not _by[_key]),
              f"9в: сбой {_key} — его детектор по подсегменту: "
              f"{[(d.t_detected, 'live' in d.values) for _, d in _by[_key]]}")
        check(logged(f"Участки ключа {_key}") and not logged("Участки нарушений сегмента"),
              f"9в: сбой {_key} — свой лог, общая страховка сегментатора не сработала")
    except Exception as e:  # noqa: BLE001
        check(False, f"9в: сбой {_fn} уронил segment(): {e!r}")
    finally:
        setattr(_lv, _fn, _orig)
# Общая страховка сегментатора осталась: сбой всего расчёта — все по подсегменту
import analytics.segmenter as _sg  # noqa: E402
_orig_ss = _sg.segment_spans
_sg.segment_spans = _boom
LOG.records.clear()
try:
    _segs = seg3(_ROWS9, RS_REP)
    _rpm_all = dets(_segs, "RPM_UNDERSPEED")
    check(_rpm_all and all("live" not in d.values for _, d in _rpm_all) and logged("Участки нарушений сегмента"),
          "9в: сбой всего расчёта участков — страховка сегментатора, по подсегменту")
except Exception as e:  # noqa: BLE001
    check(False, f"9в: сбой всего расчёта уронил segment(): {e!r}")
finally:
    _sg.segment_spans = _orig_ss

# ══ 10. Выключатель комбинированной OIL_DILUTION (п.8, В6 = А) ═══════════════
# Падение 100 кПа и рост 12 °C; минимум выше пола обоих двигателей (KTA50 241,3, QSX15 172)
CH_COMB = {"OIL_PRESS": {"min": 300.0, "median": 400.0}, "OIL_TEMP": {"max": 104.0, "median": 92.0}}


def cfg_comb(base, value):
    """Копия конфига с combined_enabled = value; ... — ключа нет вовсе."""
    c = copy.copy(base)
    od = {k: v for k, v in base.detectors["OIL_DILUTION"].items() if k != "combined_enabled"}
    if value is not ...:
        od["combined_enabled"] = value
    c.detectors = dict(base.detectors, OIL_DILUTION=od)
    return c


for _nm, _c in (("KTA50", CFG), ("QSX15", CFG3), ("монолит", MONO)):
    check(_combined_oil_detection(CH_COMB, L0, _c) is None, f"10а: {_nm}: в KB ветка выключена")
for _nm, _c in (("ключ true", cfg_comb(CFG, True)), ("ключа нет", cfg_comb(CFG, ...))):
    _d = _combined_oil_detection(CH_COMB, L0, _c)
    check(_d is not None and _d.description_key.endswith("combined_pressure_temp_anomaly"),
          f"10а: {_nm} — ветка работает как до v5.1.23")


def oil10(cfg, derived=DerivedMetrics(), live=None):
    return [d for d in run_all_detectors(CH_COMB, derived, RiskAccumulators(), [], "NORMAL", 3, L0, S(L0, 300),
                                         None, cfg, live_spans=live) if d.scenario == "OIL_DILUTION"]


check(not oil10(CFG) and not oil10(CFG3), "10б: KB — комбинированной детекции нет (прогрев, нулевой кадр)")
check(len(oil10(cfg_comb(CFG, True))) == 1, "10б: (для сравнения) с включённой веткой — была бы")
# Паспорт — работает при выключенной ветке: по DerivedMetrics и по участкам
DM_PP = DerivedMetrics(oil_press_passport_kpa=242, oil_press_step_kpa=8, oil_press_step_source="history",
                       oil_press_below_passport_run_sec=90, oil_press_below_passport_min_kpa=240,
                       oil_press_instep_min_kpa=240, oil_press_passport_until=iso(S(L0, 200)),
                       oil_press_passport_ongoing=False, oil_press_last_min_kpa=240)
_p_old = oil10(CFG3, DM_PP)
check(len(_p_old) == 1 and _p_old[0].source == "PASSPORT_THRESHOLD"
      and _p_old[0].description_key == "OIL_DILUTION.below_passport_min",
      f"10в: паспорт по DerivedMetrics при выключенной ветке: {[d.description_key for d in _p_old]}")
_p_live = [d for d in run_all_detectors(CH9, DM9, RiskAccumulators(), [], "NORMAL", 3, L0, L1, None, CFG3,
                                        live_spans=base9) if d.scenario == "OIL_DILUTION"]
check(len(_p_live) == 1 and _p_live[0].source == "PASSPORT_THRESHOLD" and "live" in _p_live[0].values
      and _p_live[0].t_detected == iso(S(L0, 560)),
      f"10в: паспорт по участкам при выключенной ветке: {[(d.source, d.t_detected) for d in _p_live]}")
check(_passport_detection(DM_PP, L0, CFG3) is not None, "10в: _passport_detection не зависит от выключателя")

if _errors:
    print(f"FAIL — {len(_errors)} расхождений:")
    for x in _errors:
        print(f"  • {x}")
    sys.exit(1)
print("Все проверки пройдены.")
