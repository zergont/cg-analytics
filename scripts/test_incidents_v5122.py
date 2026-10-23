# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""v5.1.22 «Честность при авариях» (план docs/plans/design_v5122_incidents.md,
решения владельца В1–В8 16.10.2026).

Раздел 1 — P1-1: снятие гейтом относится к своим эпизодам и не переходит по
составу в новые карточки (решения В5, В6):
- 1а: предикат suppression_active и запреты suppression_bans — сигнал панели
  WARNING/SHUTDOWN, свежая «Общая авария» (фронт в строке, смена кода),
  аварийный стоп; 1453, висящий с начала строки (ДЭС №3 #35891), — не запрет;
  статус (build_structural_status) тем же предикатом;
- 1б: журнал гейта — вид записи (llm/api/repeat/inherited/carry/late; старые
  provider=carried → late), источник снятия (повтор #35891 → разбор 09:01:59),
  основание повтора _cancelled_hashes — только llm/api/repeat;
- 1в: наследование эпизодом (движок, подмена БД): по id, запись inherited с
  источником; отказ по панели — id в памяти, повтор без запроса только при
  смене условий; перенесённое снятие новый эпизод не наследует (хеш строки
  снимается), продолжение через границу — carry; 1453 с начала строки — снятие
  действует; паспорт — нет;
- 1г: правило переноса _gate_carry — эпизод идёт на границе, стопа между
  решением и границей нет (все закрываемые сегменты), новая строка не стоп;
  смена режима — пересечение (В5); своё решение после границы — хеш остаётся;
- 1д: суточный рез целиком (_close_window с подменой нарезки и БД): ДГУ №1
  14–15.10 (14916 продолжается — carry), ДЭС №3 15.10 (14919 кончился до
  09:00 — хеша нет), догонка Работа → Стоп → Работа — хеша нет, рез пропущен
  в переходном режиме — строка та же;
- 1е: путь смены режима — пересечение по последнему закрытому и остающемуся;
- 1ж: гейт — своё решение по эпизодам, открытым на момент разбора, открытые
  во время разбора в той же строке наследуют (inherited), повтор с источником;
- 1з: подписи в сводке, примечании вердикта и входе заключения; поздний
  вердикт по границе (apply_gate_suppression until/src); части v4;
- 1и: разовый перенос reset_carried_gate_suppression и ключ gate_carry_version;
- 1к: API — _active_dets тем же предикатом, счётчики «Гейт ИИ обработал …»
  только по разборам моделью; 1л: схема.

Раздел 2 — P1-2 код 40012 на закрытии, P1-3 сегмент, оборванный аварией,
общая шапка входа заключения (решения В3, В7); серия ДГУ №1 16.10 09:25–09:30
нарезкой segment() на сырых периодах, как в БД:
- 2а: «сброшен» — подъём 40569 за 11 с и 40010 «Выкл» в кадре снятия (#35931),
  в одном кадре (#35941); «сменился» 1438 → 1248 (#35861, сброс
  переключателем); «снят в момент пуска» — 40569 за 1 ч 47 мин (#35869); код
  висит за концом — записи нет; допуск в кадр после снятия; подъём до кода —
  не сброс; NA 65535; «Работа», где код встаёт в t_end, — записи нет; ключ KB;
- 2б: отметка «… прервана аварийным остановом»: #35929/#35939 с кодом и
  меткой, открытая «Работа» — нет, стоп — нет, простой стоп — нет, «Прогрев
  прерван», 3 → 5 → стоп при 40013 = 3 — только «Работа», маска кадром позже
  40011, stop_kind выключен, задержка пуска перед плашкой с аварией — нет;
- 2в: отчёт (без «Несброшенной», «Справочник кода», причина закрытия), висящий
  код — прежний текст; часть fault_code и строка «⏱ Авария снята»; ЗАМЕЧАНИЯ —
  2 у #35929, НОРМА → ЗАМЕЧАНИЯ — 1 у #35939, карточка CAUTION; поздняя отмена
  гейтом отметку не снимает; add_run_trip (поздняя доработка) — та же строка;
- 2г: шапка заключения — наработка, «Авария снята в … (через N с)», одна
  строка о коде, #35861, запрет «штатно» и seg_caution у #35939, поздняя
  отметка в шапке, «нет в данных — не указывай»; блок 1 пути Claude API;
- 2д: наработка — строка до начала в окне, посев перед окном (с → ч), на конец;
- 2е: поздняя отметка движком: ожидание после закрытия в простой стоп, запись
  apply_late_run_trip одна, повтор ничего не меняет, перезаказ заключения.

Раздел 3 — P1-4 акт и вход совета, P1-5 заключение аварийного стопа фактами
(решение В1), P1-6 время МСК, чужой язык (В4); серия 16.10 с отсчётами панели:
- 3а: лента отсчётов снимается нарезкой из истории окна (В2): окно от начала
  сегмента падения до останова + 30 с, колонки KB, вход, секунда опроса, автомат
  из enum; печать — «Без изменений», «·», один разделитель останова, свёртка
  сверх 40 строк; посев перед окном; плашка — Segment.fall_trace;
- 3б: факты до останова — удержанное значение «держится с», а не замер;
- 3в: общий _build_act на трёх путях (прогон окна, открытый стоп, после рестарта
  по подсказке из БД, смена режима), плашка, нет отметки — предупреждение;
- 3г: вход совета — ход цикла по сегментам (713 кВт до разделителя, P 0 у
  сегмента падения, части через рез, лимит 30 строк, своя плашка исключена),
  МСК без UTC, лента за сегментом падения, фон; перенос промпта совета;
- 3д: заключение фактами — тексты #35931/#35941, когда ИИ (кнопка, строка,
  без акта, совет failed), «system» только у seg_shutdown, разовая установка
  строки, перезаказ при сбое совета;
- 3е: МСК — плашка #35935, входы заключения и гейта, шапка;
- 3ж: язык — фрагменты, один проход очистки, приём / отказ (числа, заголовки,
  остаток), подключение к заключению (оба пути), пересказу, совету, гейту.

Раздел 4 — ревью v5.1.22, подтверждённые находки (сценарии ревьюеров):
- 4а gate-1: эпизод, открытый во время разбора, при сигнале панели / смене
  состава снятие не наследует, id — движку в отказанные, повтор после запрета;
- 4б gate-2 / incident-2: журнал разбора — до прохода языка: в окне очистки
  перенос идёт, новый эпизод наследует, хеш не снимается;
- 4в gate-3: догонка Работа → Стоп → Работа — синтетика послестоповой «Работы»
  не снята; 4г gate-4: перенос снимает только снятое закрытой карточкой;
- 4д verdict-1: наработка до v5.1.6 — секунды → часы; 4е verdict-2: код на
  закрытии оборванного сегмента — без сброса после кадра снятия, «сменился»;
  4ж verdict-3: «сняла аварийную маску» — только при маске; 4з verdict-4: одна
  отметка на аварию между вызовами нарезки; 4и verdict-5: перед простым стопом
  отметки нет; 4к verdict-6: поздняя дописка перезаказывает заключение;
- 4л–4н incident-1/3/8: strip_cjk, ссылки [W…], разметка ответа очистки;
  4о incident-4: остановка во время очистки — исходник совета сохранён;
  4п incident-5: перезаказ по устаревшему id; 4р incident-7 / shadow-1: «кадр
  останова» только в кадре; 4с: снятые до v5.1.22 — отдельной подписью.

Запуск:  py -3 scripts/test_incidents_v5122.py
"""
from __future__ import annotations

import ast
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import online.db as odb  # noqa: E402
import online.engine as eng  # noqa: E402
import online.manager as mgr  # noqa: E402
import online.status_assembler as sa  # noqa: E402
from analytics.contract import (  # noqa: E402
    CokingRisk, DerivedMetrics, Detection, RiskAccumulators, Segment, Subsegment,
)
from analytics.labels import fmt_local, gate_supp_label, tz_label  # noqa: E402
from analytics.serializer import (  # noqa: E402
    SUMMARY_PARTS_VERSION, apply_gate_suppression, build_summary_parts, render_summary_md,
)

_errors: list[str] = []
MSK = ZoneInfo("Europe/Moscow")
U = timezone.utc
RULES = sa.GATE_RULES_VERSION


def check(cond: bool, msg: str) -> None:
    if not cond:
        _errors.append(msg)


def M(d: int, h: int, mi: int, s: int = 0) -> datetime:
    """Метка МСК октября 2026 → UTC."""
    return datetime(2026, 10, d, h, mi, s, tzinfo=MSK).astimezone(U)


def iso(t: datetime) -> str:
    return t.isoformat()


# ══ 1. P1-1: снятие гейтом относится к своим эпизодам ═════════════════════════

LP = {"scenario": "LIMIT_PROXIMITY", "severity": "CAUTION", "fault_codes": [], "ref_codes": [214],
      "trigger": "Температура масла: 100 в пределах 10% от порога 110", "values": {}}
RPM = {"scenario": "RPM_UNDERSPEED", "severity": "CAUTION", "fault_codes": [], "ref_codes": [1448],
       "trigger": "Обороты", "values": {}}
H_LP = sa.compute_analytics_hash([LP])
H_RPM = sa.compute_analytics_hash([RPM])


def panel(sev, code, start, addr=40408, bit=6):
    return {"scenario": "CONTROLLER_FAULT", "severity": sev, "fault_codes": [code],
            "values": {"addr": addr, "bit": bit, "fault_start": iso(start)}}


def row_with(h, t0, chars=None, log=None, rid=5):
    return {"id": rid, "gate_suppressed_hash": h, "t_start": t0,
            "characteristics_json": chars or {}, "gate_log": log or []}


def chars_code0(code):
    return {"subsegments": [{"characteristics": {"LAST_FAULT_CODE": {"value_start": code}}}]}


# ── 1а. Предикат и запреты ──
R0 = M(14, 9, 0)
row = row_with(H_LP, R0)
check(sa.suppression_active(row, [LP]) and sa.suppression_bans(row, [LP]) == [],
      "1а: состав совпал, запретов нет — снятие действует")
check(not sa.suppression_active(row, [LP, panel("WARNING", 1464, M(14, 9, 10))])
      and sa.suppression_bans(row, [LP, panel("WARNING", 1464, M(14, 9, 10))]) == ["panel"],
      "1а: сигнал панели WARNING — не действует")
check(not sa.suppression_active(row, [LP, panel("SHUTDOWN", 1448, M(14, 9, 10), 40407, 3)]),
      "1а: SHUTDOWN — не действует")
check(sa.suppression_bans(row, [LP, panel("CAUTION", 1453, M(14, 9, 5), 40405, 1)]) == ["panel_fresh"]
      and not sa.suppression_active(row, [LP, panel("CAUTION", 1453, M(14, 9, 5), 40405, 1)]),
      "1а: «Общая авария» с фронтом в строке — свежая, не действует")
_r1453 = row_with(H_LP, R0, chars_code0(1453))
check(sa.suppression_active(_r1453, [LP, panel("CAUTION", 1453, M(13, 15, 0), 40405, 1)]),
      "1а: 1453 висит с начала строки (ДЭС №3 #35891) — снятие действует")
check(not sa.suppression_active(row_with(H_LP, R0, chars_code0(1438)),
                                [LP, panel("CAUTION", 1453, M(13, 15, 0), 40405, 1)]),
      "1а: код сменился с начала сегмента — свежая, не действует")
check(sa.suppression_bans(row_with(H_LP, R0, {"stop_kind": "EMERGENCY"}), [LP]) == ["emergency"],
      "1а: аварийный стоп — не действует")
check(not sa.suppression_active(row_with(H_LP, R0), [LP, {"scenario": "NEGATIVE_SEQUENCE"}]),
      "1а: несимметрия — как раньше (is_analytics_suppressed)")
check(sa.is_analytics_suppressed(row, [LP, panel("WARNING", 1464, M(14, 9, 10))]),
      "1а: is_analytics_suppressed (закрытые сегменты) не меняется")


def status(dets, chars=None):
    return sa.build_structural_status({"id": 5, "run_state": 3, "t_start": R0,
                                       "characteristics_json": chars or {},
                                       "current_values_json": {"values": {}},
                                       "active_detections_json": dets,
                                       "gate_suppressed_hash": H_LP})


check(status([LP])["analytics_suppressed"] is True, "1а: статус — снято")
_sw = status([LP, panel("WARNING", 1464, M(14, 9, 10))])
check(_sw["analytics_suppressed"] is False and _sw["severity_level"] == "внимание",
      f"1а: статус при сигнале панели — аналитика не снята: {_sw['analytics_suppressed']}")
_sc = status([LP, panel("CAUTION", 1453, M(13, 15, 0), 40405, 1)], chars_code0(1453))
check(_sc["analytics_suppressed"] is True and _sc["panel_alarms"][0]["fresh"] is False,
      "1а: статус при 1453 с начала строки — снято, «Общая авария» не свежая")
_sf = status([LP, panel("CAUTION", 1453, M(14, 9, 5), 40405, 1)])
check(_sf["analytics_suppressed"] is False and _sf["panel_alarms"][0]["fresh"] is True,
      "1а: статус при свежей «Общей аварии» — не снято, fresh тем же расчётом")

# ── 1б. Журнал гейта: вид, источник, основание повтора ──
check(sa.gate_entry_kind({"provider": "carried"}) == "late"
      and sa.gate_entry_kind({"provider": "repeat"}) == "repeat"
      and sa.gate_entry_kind({"provider": "api"}) == "api"
      and sa.gate_entry_kind({"provider": "llm"}) == "llm"
      and sa.gate_entry_kind({}) == "llm"
      and sa.gate_entry_kind({"kind": "incident"}) == "incident"
      and sa.gate_entry_kind({"kind": "carry", "provider": "carry"}) == "carry",
      "1б: вид записи; старый provider=carried — поздний вердикт (late)")
# Журнал ДЭС №3 #35891 (БД, 14.10): разбор 09:01:59 (LP), 09:26:00 (LP+OD), повтор 09:41:00 (LP)
LOG_35891 = [
    {"ts": "2026-10-14T06:01:59.698684+00:00", "provider": "llm", "decision": "cancel",
     "decision_applied": True, "fault_hash": "a36ac7f457e4", "rules": RULES},
    {"ts": "2026-10-14T06:26:00.258929+00:00", "provider": "llm", "decision": "cancel",
     "decision_applied": True, "fault_hash": "ed469fb1bf9a", "rules": RULES},
    {"ts": "2026-10-14T06:41:00.650789+00:00", "provider": "repeat", "decision": "cancel",
     "decision_applied": True, "fault_hash": "a36ac7f457e4", "rules": RULES},
]
_s91 = sa.gate_decision_source(LOG_35891, H_LP)
check(_s91 is not None and _s91["ts"] == "2026-10-14T06:01:59.698684+00:00" and _s91["kind"] == "repeat",
      f"1б: #35891 — источник повтора 09:41 — разбор 09:01:59 (журнал до v5.1.22): {_s91}")
check(gate_supp_label({**_s91, "own": True}, MSK) == "снято ИИ (разбор 09:01)",
      f"1б: подпись #35891: {gate_supp_label({**_s91, 'own': True}, MSK)}")
check(sa.gate_hash_source(LOG_35891, "a36ac7f457e4")["ts"] == "2026-10-14T06:01:59.698684+00:00",
      "1б: источник повтора по составу — первый разбор моделью")
_new = [{"ts": "a", "kind": "llm", "decision": "cancel", "decision_applied": True,
         "suppressed_hash": "H1", "fault_hash": "f1"},
        {"ts": "b", "kind": "llm", "decision": "cancel", "decision_applied": True,
         "suppressed_hash": "H2", "fault_hash": "f2"}]
check(sa.gate_decision_source(_new, "H1")["ts"] == "a" and sa.gate_decision_source(_new, "H3") is None,
      "1б: новые записи — по suppressed_hash; чужого хеша — нет основания")
_carry_log = [{"ts": "c", "kind": "carry", "decision": "cancel", "decision_applied": True,
               "suppressed_hash": "H1", "fault_hash": "f1",
               "source": {"ts": "2026-10-14T00:36:51+00:00", "segment_id": 35876}, "episodes": [14916]}]
check(sa.gate_decision_source(_carry_log, "H1") is None
      and sa.gate_decision_source(_carry_log, "H1", sa.GATE_CARRY_KINDS)["segment_id"] == 35876,
      "1б: перенос — не своё решение строки; источник переноса — исходный разбор")
check(sa.gate_record_episodes(_carry_log, "H1") == {14916} and sa.gate_record_episodes(LOG_35891, H_LP) is None,
      "1б: снятые эпизоды — из записей; журнал до v5.1.22 — None")
_mixed = [dict(e, rules=RULES) for e in (
    {"fault_hash": "L", "decision": "cancel", "decision_applied": True, "kind": "llm"},
    {"fault_hash": "A", "decision": "cancel", "decision_applied": True, "kind": "api"},
    {"fault_hash": "R", "decision": "cancel", "decision_applied": True, "kind": "repeat"},
    {"fault_hash": "I", "decision": "cancel", "decision_applied": True, "kind": "inherited"},
    {"fault_hash": "C", "decision": "cancel", "decision_applied": True, "kind": "carry"},
    {"fault_hash": "T", "decision": "cancel", "decision_applied": True, "kind": "late"},
    {"fault_hash": "O", "decision": "cancel", "decision_applied": True, "provider": "carried"},
    {"fault_hash": "P", "decision": "cancel", "decision_applied": True, "provider": "llm"})]
check(mgr._cancelled_hashes({"gate_log": _mixed}) == {"L", "A", "R", "P"},
      f"1б: основание повтора — только llm/api/repeat: {mgr._cancelled_hashes({'gate_log': _mixed})}")


# ── 1в. Наследование эпизодом (движок, подмена БД) ──
class FakeDB:
    def __init__(self):
        self.row = None
        self.calls: list[tuple] = []
        self.next_id = 100

    async def get_open_segment(self, *a, **k):
        self.calls.append(("get",))
        return self.row

    async def set_episodes_gate_suppressed(self, r, e, p, scenarios, ids=None, src=None, **k):
        self.calls.append(("mark", list(scenarios), list(ids or []), dict(src or {})))
        return list(ids or [])

    async def append_segment_gate_event(self, *a, event=None, segment_id=None, ts=None, **k):
        self.calls.append(("event", event, segment_id))
        return True

    async def clear_open_gate_suppression_hash(self, r, e, p, h):
        self.calls.append(("clear_hash", h))
        return True

    async def open_episode(self, *a, **k):
        self.next_id += 1
        self.calls.append(("open", self.next_id))
        return self.next_id

    async def find_episode_by_onset(self, *a, **k):
        return None

    async def update_episode(self, *a, **k):
        pass

    async def close_episode(self, *a, **k):
        pass

    def of(self, kind):
        return [c for c in self.calls if c[0] == kind]


_DB_NAMES = ("get_open_segment", "set_episodes_gate_suppressed", "append_segment_gate_event",
             "clear_open_gate_suppression_hash", "open_episode", "find_episode_by_onset",
             "update_episode", "close_episode")
_orig_db = {n: getattr(odb, n) for n in _DB_NAMES}


class Cfg:
    def det(self, *a, default=None):
        return default


async def _noop(*a, **k):
    return None


def mk_engine(db):
    for n in _DB_NAMES:
        setattr(eng.online_db, n, getattr(db, n))
    e = eng.OnlinePollEngine.__new__(eng.OnlinePollEngine)
    e.cfg = Cfg()
    e.router_sn, e.equip_type, e.panel_id = "SN", "pcc", 1
    e._episodes, e._episode_accrual_ts, e._last_open_seg_id = {}, None, 5
    e._supp_recheck, e._prev_seg_hint, e.cursor_ts = None, None, R0
    e._supp_refused, e._supp_refused_sig = {}, None
    e._attach_trip_context = _noop
    return e


OWN_LOG = [{"ts": iso(M(14, 9, 59)), "kind": "llm", "decision": "cancel", "decision_applied": True,
            "suppressed_hash": H_LP, "fault_hash": "fLP", "episodes": [90]}]
db = FakeDB()
e = mk_engine(db)
db.row = row_with(H_LP, R0, log=OWN_LOG)
e._episodes = {"LIMIT_PROXIMITY": {"id": 99}}
asyncio.run(e._inherit_gate_suppression([("LIMIT_PROXIMITY", 99, M(14, 10, 5))], [LP], ts=M(14, 10, 6)))
_mk, _ev = db.of("mark"), db.of("event")
check(len(_mk) == 1 and _mk[0][2] == [99] and _mk[0][3].get("kind") == "inherited"
      and _mk[0][3].get("ts") == iso(M(14, 9, 59)),
      f"1в: своё решение строки — помечен только новый id, вид inherited, источник — разбор: {_mk}")
check(len(_ev) == 1 and _ev[0][1]["kind"] == "inherited" and _ev[0][1]["episodes"] == [99]
      and _ev[0][1]["fault_hash"] == "fLP" and _ev[0][1]["suppressed_hash"] == H_LP
      and _ev[0][1]["source"]["ts"] == iso(M(14, 9, 59)) and _ev[0][1]["ts"] == iso(M(14, 10, 6)),
      f"1в: запись inherited со ссылкой на исходный разбор, fault_hash — его: {_ev}")

# Отказ по панели: ни пометки, ни записи, id в памяти
db.calls.clear()
_W = panel("WARNING", 1464, M(14, 10, 0))
e._episodes = {"LIMIT_PROXIMITY": {"id": 100}}
asyncio.run(e._inherit_gate_suppression([("LIMIT_PROXIMITY", 100, M(14, 10, 2))], [LP, _W]))
check(not db.of("mark") and not db.of("event") and 100 in e._supp_refused,
      f"1в: сигнал панели — ни пометки, ни записи, id в памяти: {db.calls}, {e._supp_refused}")
# Пока сигнал панели горит — повтора нет (и запроса строки тоже)
check(e._refused_retry_due([LP, _W]) is False, "1в: WARNING горит — повтор не нужен")
# Сигнал ушёл, эпизод идёт — повтор наследования в цикле эпизодов
db.calls.clear()
e._episode_accrual_ts = M(14, 10, 30)
e._active_alerts = {}
e._episodes = {"LIMIT_PROXIMITY": {"id": 100, "severity": "CAUTION", "miss": 0, "first_miss_ts": None,
                                   "t_open": M(14, 10, 2), "exact": False}}
asyncio.run(e._process_episodes([LP], M(14, 11, 0), []))
_mk = db.of("mark")
check(len(_mk) == 1 and _mk[0][2] == [100] and not e._supp_refused,
      f"1в: запрет снят — наследование повторено для отказанного id: {db.calls}")
# Тот же состав и та же строка после повтора — без запроса
db.calls.clear()
e._supp_refused = {101: ("LIMIT_PROXIMITY", 101, M(14, 11, 2))}
e._supp_refused_sig = e._retry_sig([LP])
e._episodes = {"LIMIT_PROXIMITY": {"id": 101, "severity": "CAUTION", "miss": 0, "first_miss_ts": None,
                                   "t_open": M(14, 11, 2), "exact": False}}
asyncio.run(e._process_episodes([LP], M(14, 11, 30), []))
check(not db.of("get"), f"1в: условия не менялись — повтор без запроса не идёт: {db.calls}")
# Отказанный эпизод закончился — забыт без запроса
db.calls.clear()
e._supp_refused = {102: ("LIMIT_PROXIMITY", 102, M(14, 11, 2))}
e._supp_refused_sig = None
e._episodes = {}
asyncio.run(e._process_episodes([], M(14, 12, 0), []))
check(not e._supp_refused and not db.of("get"), "1в: отказанный эпизод кончился — забыт, запроса нет")

# Свежая «Общая авария» — отказ; пока она та же — без запроса; ушла — повтор
db.calls.clear()
_C = panel("CAUTION", 1453, M(14, 9, 30), 40405, 1)
e._supp_refused, e._supp_refused_sig = {}, None
e._episodes = {"LIMIT_PROXIMITY": {"id": 103}}
asyncio.run(e._inherit_gate_suppression([("LIMIT_PROXIMITY", 103, M(14, 9, 40))], [LP, _C]))
check(103 in e._supp_refused and not db.of("mark"), "1в: свежая «Общая авария» — отказ")
check(e._refused_retry_due([LP, _C]) is False and e._refused_retry_due([LP]) is True,
      "1в: та же «Общая авария» — повтора нет; ушла — повтор")

# 1453 с начала строки (ДЭС №3 #35891) — снятие наследуется
db.calls.clear()
db.row = row_with(H_LP, R0, chars_code0(1453), log=LOG_35891)
e._supp_refused = {}
e._episodes = {"LIMIT_PROXIMITY": {"id": 104}}
asyncio.run(e._inherit_gate_suppression([("LIMIT_PROXIMITY", 104, M(14, 10, 8))],
                                        [LP, panel("CAUTION", 1453, M(13, 15, 0), 40405, 1)]))
_mk = db.of("mark")
check(len(_mk) == 1 and _mk[0][2] == [104] and _mk[0][3]["ts"] == "2026-10-14T06:01:59.698684+00:00",
      f"1в: #35891 — 1453 с начала строки не запрет; источник — разбор 09:01:59: {_mk}")

# Нарушение паспорта — не наследуется
db.calls.clear()
_LPh = dict(LP, values={"limit": {"kind": "passport", "hard": True}})
db.row = row_with(sa.compute_analytics_hash([_LPh]), R0, log=[dict(OWN_LOG[0], suppressed_hash=sa.compute_analytics_hash([_LPh]))])
asyncio.run(e._inherit_gate_suppression([("LIMIT_PROXIMITY", 105, M(14, 10, 8))], [_LPh]))
check(not db.of("mark"), "1в: нарушение паспорта — снятие не наследуется")

# Перенесённое снятие: новый эпизод в строке не наследует — хеш снимается
CARRY_ROW_LOG = [{"ts": iso(M(15, 9, 0)), "kind": "carry", "decision": "cancel", "decision_applied": True,
                  "suppressed_hash": H_RPM, "fault_hash": "10d98a7ad8c7",
                  "source": {"ts": "2026-10-14T00:36:51+00:00", "segment_id": 35876}, "episodes": [14916],
                  # v5.1.22, ревью gate-4: закрытая карточка сняла новую тревогу RPM (синтетика)
                  "synthetic": ["RPM_UNDERSPEED"]}]
db.calls.clear()
db.row = row_with(H_RPM, M(15, 9, 0), log=CARRY_ROW_LOG)
e._episodes = {"RPM_UNDERSPEED": {"id": 106}}
asyncio.run(e._inherit_gate_suppression([("RPM_UNDERSPEED", 106, M(15, 11, 0))], [RPM]))
check(not db.of("mark") and db.of("clear_hash") == [("clear_hash", H_RPM)],
      f"1в: новый эпизод при перенесённом снятии — не наследует, хеш снят (разберёт гейт): {db.calls}")
# Продолжение через границу (тревога появилась до реза) — снятие переносом, запись carry
db.calls.clear()
asyncio.run(e._inherit_gate_suppression([("RPM_UNDERSPEED", 107, M(15, 8, 59, 30))], [RPM]))
_mk, _ev = db.of("mark"), db.of("event")
check(len(_mk) == 1 and _mk[0][2] == [107] and _mk[0][3]["kind"] == "carry"
      and _mk[0][3]["segment_id"] == 35876 and not db.of("clear_hash")
      and _ev and _ev[0][1]["kind"] == "carry",
      f"1в: продолжение через рез — перенос, источник #35876: {db.calls}")
# Чужой хеш без журнала (перенос до v5.1.22) — новый эпизод не наследует
db.calls.clear()
db.row = row_with(H_RPM, M(16, 9, 57, 55), log=[])
asyncio.run(e._inherit_gate_suppression([("RPM_UNDERSPEED", 108, M(16, 9, 58, 12))], [RPM]))
check(not db.of("mark") and db.of("clear_hash"),
      "1в: #35949 — хеш от разбора 14.10 без своего решения: 14931 не наследует, хеш снят")
# Строки нет — False (проверка откладывается, id сохраняются)
db.calls.clear()
db.row = None
check(asyncio.run(e._inherit_gate_suppression([("RPM_UNDERSPEED", 109, M(16, 10, 0))], [RPM])) is False,
      "1в: строки нет — проверка откладывается")
for n, f in _orig_db.items():
    setattr(eng.online_db, n, f)


# ── 1г. Правило переноса _gate_carry ──
def mkdet(sc, start, end, ongoing):
    return Detection(scenario=sc, severity="CAUTION", t_detected=iso(start), source="METRIC_RULE",
                     trigger="t", related_roles=[], fault_codes=[], description_key="k",
                     values={"live": {"clusters": [{"start": iso(start), "end": iso(end), "ongoing": ongoing}],
                                      "onset": iso(start), "first": iso(start), "until": iso(end),
                                      "ongoing": ongoing}},
                     ref_codes=[1448] if sc == "RPM_UNDERSPEED" else [214])


def mksub(a: datetime, b: datetime, dets):
    return Subsegment(id=f"s.{a.timestamp()}", parent_segment_id="s", t_start=iso(a), t_end=iso(b),
                      duration_sec=(b - a).total_seconds(), load_zone="NORMAL", cause_open="ZONE_CHANGE",
                      cause_close=None, characteristics={}, derived_metrics=DerivedMetrics(),
                      risk_accumulators=RiskAccumulators(), detections=dets, data_quality=1.0, data_gaps=[])


def mkseg(a: datetime, b: datetime, rs=3, dets=(), stop_kind=None, cause_close=None):
    s = Segment(id="s", router_sn="R", equip_type="pcc", panel_id=1, engine_sn="", run_state=rs,
                run_state_label={0: "Стоп", 3: "Работа", 5: "Охлаждение"}.get(rs, "?"),
                t_start=iso(a), t_end=iso(b), duration_sec=(b - a).total_seconds(),
                engine_hours_start=None, cause_open="REPORT_START", cause_close=cause_close,
                preamble_included=False, data_quality=1.0, subsegments=[mksub(a, b, list(dets))],
                sequence_checks=[], events=[])
    s.stop_kind = stop_kind
    return s


B = M(15, 9, 0)               # суточный рез ДГУ №1
DEC = M(14, 3, 36, 51)        # разбор 14.10 03:36:51 (#35876)
GS = {"suppressed_hash": H_RPM, "analyses": [],
      "gate_log": [{"ts": iso(DEC), "kind": "llm", "decision": "cancel", "decision_applied": True,
                    "suppressed_hash": H_RPM, "fault_hash": "10d98a7ad8c7", "episodes": [14916]}]}
EPS = {"RPM_UNDERSPEED": {"id": 14916}}
work = mkseg(M(14, 9, 0), B, 3, [mkdet("RPM_UNDERSPEED", M(14, 9, 0), B, True)])
_dend = eng._extract_open_segment_data(work)[1]
cr = eng._gate_carry(GS, [(work, 35890, M(14, 9, 0), B)], EPS, {"RPM_UNDERSPEED"}, None, 3, B, _dend)
check(cr is not None and cr["hash"] == H_RPM and cr["ids"] == [14916] and cr["segment_id"] is None
      and cr["record"]["kind"] == "carry" and cr["record"]["ts"] == iso(B)
      and cr["record"]["source"] == {"ts": iso(DEC), "segment_id": None}
      and cr["record"]["fault_hash"] == "10d98a7ad8c7" and cr["record"]["suppressed_hash"] == H_RPM,
      f"1г: эпизод идёт на резе — перенос с записью carry: {cr}")
# Решение в закрываемом окне — номер его закрытого сегмента в источнике
_dec2 = M(14, 15, 0)
_gs2 = dict(GS, gate_log=[dict(GS["gate_log"][0], ts=iso(_dec2))])
cr = eng._gate_carry(_gs2, [(work, 35890, M(14, 9, 0), B)], EPS, {"RPM_UNDERSPEED"}, None, 3, B, _dend)
check(cr and cr["segment_id"] == 35890 and cr["record"]["source"]["segment_id"] == 35890,
      f"1г: решение в закрываемом сегменте — его номер в источнике: {cr}")
check(eng._gate_carry(GS, [(work, 35890, M(14, 9, 0), B)], EPS, set(), None, 3, B, _dend) is None,
      "1г: снятый эпизод на границе не идёт — хеш не переходит")
check(eng._gate_carry(GS, [(work, 35890, M(14, 9, 0), B)], {"RPM_UNDERSPEED": {"id": 1}},
                      {"RPM_UNDERSPEED"}, None, 3, B, _dend) is None,
      "1г: идёт другой эпизод того же сценария (не снятый) — хеш не переходит")
_stop = mkseg(M(16, 9, 26, 23), M(16, 9, 29, 22), 0, stop_kind="EMERGENCY")
_w1 = mkseg(M(16, 9, 0), M(16, 9, 26, 23), 3)
_w2 = mkseg(M(16, 9, 29, 33), M(16, 9, 32, 33), 3, [mkdet("RPM_UNDERSPEED", M(16, 9, 30, 2), M(16, 9, 32, 33), True)])
_gs3 = dict(GS, gate_log=[dict(GS["gate_log"][0], ts=iso(M(16, 9, 10)))])
check(eng._gate_carry(_gs3, [(_w1, 1, M(16, 9, 0), M(16, 9, 26, 23)), (_stop, 2, M(16, 9, 26, 23), M(16, 9, 29, 22)),
                             (_w2, 3, M(16, 9, 29, 33), M(16, 9, 32, 33))],
                      {"RPM_UNDERSPEED": {"id": 14916}}, {"RPM_UNDERSPEED"}, {"RPM_UNDERSPEED"}, 3,
                      M(16, 9, 32, 33), []) is None,
      "1г: догонка Работа → Стоп → Работа — стоп между решением и границей, хеш не переходит")
check(eng._gate_carry(GS, [(work, 35890, M(14, 9, 0), B)], EPS, {"RPM_UNDERSPEED"}, None, 0, B, _dend) is None,
      "1г: новая строка — стоп: хеш не переходит")
_stop_before = mkseg(M(14, 2, 0), M(14, 3, 0), 0)
check(eng._gate_carry(GS, [(_stop_before, 7, M(14, 2, 0), M(14, 3, 0)), (work, 35890, M(14, 9, 0), B)], EPS,
                      {"RPM_UNDERSPEED"}, None, 3, B, _dend) is not None,
      "1г: стоп до решения — не помеха")
# Смена режима без стопа (В5): эпизод идёт и на конце закрытого, и в остающемся
check(eng._gate_carry(GS, [(work, 35890, M(14, 9, 0), B)], EPS, {"RPM_UNDERSPEED"}, {"RPM_UNDERSPEED"}, 5, B,
                      _dend) is not None
      and eng._gate_carry(GS, [(work, 35890, M(14, 9, 0), B)], EPS, {"RPM_UNDERSPEED"}, set(), 5, B,
                          _dend) is None,
      "1г: Работа → Охлаждение — переносится, только если эпизод идёт и в новом режиме (В5)")
# Своё решение уже после границы (хвост) — хеш остаётся без записи переноса
_gs4 = dict(GS, gate_log=[dict(GS["gate_log"][0], ts=iso(B + timedelta(seconds=20)), episodes=[200])])
cr = eng._gate_carry(_gs4, [(work, 35890, M(14, 9, 0), B)], {}, set(), None, 3, B, [])
check(cr == {"hash": H_RPM, "record": None, "ids": [], "segment_id": None},
      f"1г: решение после границы — своё у новой строки, хеш остаётся: {cr}")
# Перенесённое раньше снятие переносится дальше со своим источником
_gs5 = {"suppressed_hash": H_RPM, "gate_log": CARRY_ROW_LOG}
cr = eng._gate_carry(_gs5, [(work, 35907, M(15, 9, 0), M(16, 9, 0))], EPS, {"RPM_UNDERSPEED"}, None, 3,
                     M(16, 9, 0), _dend)
check(cr and cr["segment_id"] == 35876 and cr["record"]["source"]["ts"] == "2026-10-14T00:36:51+00:00",
      f"1г: цепочка резов — источник исходного разбора: {cr}")
# Журнал до v5.1.22 (без списков эпизодов) — по составу на конце
_gs6 = {"suppressed_hash": H_RPM, "gate_log": [{"ts": iso(DEC), "provider": "llm", "decision": "cancel",
                                                 "decision_applied": True, "fault_hash": "10d98a7ad8c7"}]}
check(eng._gate_carry(_gs6, [(work, 35890, M(14, 9, 0), B)], EPS, {"RPM_UNDERSPEED"}, None, 3, B, _dend)
      is not None
      and eng._gate_carry(_gs6, [(work, 35890, M(14, 9, 0), B)], EPS, {"RPM_UNDERSPEED"}, None, 3, B, [LP])
      is None, "1г: журнал до v5.1.22 — по составу на конце")
check(eng._gate_carry({"suppressed_hash": H_RPM, "gate_log": []}, [(work, 35890, M(14, 9, 0), B)], EPS,
                      {"RPM_UNDERSPEED"}, None, 3, B, _dend) is None,
      "1г: хеш без основания (чужой, пустой журнал) — не переходит")


# ── 1д. Суточный рез целиком: _close_window с подменой нарезки и БД ──
class CWDB:
    def __init__(self, open_row):
        self.open_row = open_row
        self.inserted: list = []
        self.appended: list = []
        self.src_seg: list = []
        self.next = 35900

    async def pop_open_segment(self, *a, **k):
        r, self.open_row = self.open_row, None
        return r

    async def upsert_open_segment(self, data):
        return 777

    async def insert_closed_segment(self, data):
        self.next += 1
        self.inserted.append((self.next, data))
        return self.next

    async def retarget_episode_segments(self, *a, **k):
        return 0

    async def insert_detection_events(self, *a, **k):
        return None

    async def append_segment_gate_state(self, seg_id, state):
        self.appended.append((seg_id, state))

    async def set_episodes_gate_src_segment(self, ids, sid):
        self.src_seg.append((list(ids), sid))
        return len(ids)


_CW_NAMES = ("pop_open_segment", "upsert_open_segment", "insert_closed_segment",
             "retarget_episode_segments", "insert_detection_events", "append_segment_gate_state",
             "set_episodes_gate_src_segment")
_orig_cw = {n: getattr(odb, n) for n in _CW_NAMES}
_orig_run, _orig_collect = eng._run_segment, eng._collect_and_enrich_detections
import online.verifier as _verifier  # noqa: E402
_orig_fv = _verifier.fire_verify


async def _empty_list(*a, **k):
    return []


async def _no_summary(*a, **k):
    return None, None


def close_window(segments, open_row, episodes, t_from, t_to):
    cw = CWDB(open_row)
    for n in _CW_NAMES:
        setattr(eng.online_db, n, getattr(cw, n))

    async def _run(*a, **k):
        return segments, {}, [], {}
    eng._run_segment = _run
    eng._collect_and_enrich_detections = _empty_list
    _verifier.fire_verify = lambda *a, **k: None
    e2 = eng.OnlinePollEngine.__new__(eng.OnlinePollEngine)
    e2.cfg, e2.tz = Cfg(), MSK
    e2.router_sn, e2.equip_type, e2.panel_id, e2.engine_sn = "SN", "pcc", 1, ""
    e2.inherited_coking_risk, e2.inherited_run_state_sec = CokingRisk(), {}
    e2.continued_from_id, e2._prev_seg_hint, e2._transition_ctx = None, None, []
    e2._episodes, e2._act_done_for, e2.poll_interval_sec = episodes, None, 30
    e2.cursor_ts, e2.last_processed_to, e2.forward_fill_memory = t_from, None, None
    e2._open_history_cache = e2._open_history_cache_ts = None
    e2._active_alerts, e2._fault_ref = {}, None
    e2._open_load_seed = _noop
    e2._build_summary_md_for = _no_summary
    e2._refine_until = lambda segs: None
    asyncio.run(e2._close_window(t_from, t_to, "DAILY_BOUNDARY", lookahead_to=t_to, live_to=t_to))
    return cw, e2


def open_row(log, t0, h=H_RPM):
    return {"id": 35890, "t_start": t0, "gate_suppressed_hash": h, "gate_log": json.dumps(log),
            "warning_analyses": "[]", "characteristics_json": {}}


# ДГУ №1 14–15.10: 14916 продолжается через рез — снятие переходит с записью carry
cw, _ = close_window([work], open_row(GS["gate_log"], M(14, 9, 0)), dict(EPS), M(14, 9, 0), B)
_st = dict(cw.appended[0][1]) if cw.appended else {}
check(cw.appended and cw.appended[0][0] == 777 and _st.get("gate_suppressed_hash") == H_RPM
      and [g.get("kind") for g in _st.get("gate_log") or []] == ["carry"]
      and _st["gate_log"][0]["episodes"] == [14916]
      and _st["gate_log"][0]["source"] == {"ts": iso(DEC), "segment_id": None},
      f"1д: ДГУ №1 14–15.10 — 14916 продолжается, перенос с carry: {cw.appended}")
# Решение 14.10 03:36 лежит в закрываемом окне (#35876 тогда — открытая строка) — номер в источник
cw, _ = close_window([mkseg(M(13, 9, 0), M(14, 9, 0), 3, [mkdet("RPM_UNDERSPEED", M(14, 3, 30), M(14, 9, 0), True)])],
                     open_row(GS["gate_log"], M(13, 9, 0)), dict(EPS), M(13, 9, 0), M(14, 9, 0))
_st = dict(cw.appended[0][1]) if cw.appended else {}
check(_st.get("gate_suppressed_hash") == H_RPM and _st["gate_log"][-1]["source"]["segment_id"] == 35901
      and cw.src_seg == [([14916], 35901)],
      f"1д: решение в закрываемом сегменте — его номер в записи и в источнике эпизода: {cw.appended}, {cw.src_seg}")
# ДЭС №3 15.10: близость кончилась в 08:36:42 — на резе не идёт, хеша нет (14920 разберёт гейт)
_lp_end = mkseg(M(14, 9, 0), B, 3, [mkdet("LIMIT_PROXIMITY", M(15, 8, 9, 8), M(15, 8, 36, 42), False)])
_gs_lp = [{"ts": iso(M(15, 8, 10)), "kind": "llm", "decision": "cancel", "decision_applied": True,
           "suppressed_hash": H_LP, "fault_hash": "a36ac7f457e4", "episodes": [14919]}]
cw, _ = close_window([_lp_end], open_row(_gs_lp, M(14, 9, 0), H_LP),
                     {"LIMIT_PROXIMITY": {"id": 14919}}, M(14, 9, 0), B)
_st = dict(cw.appended[0][1]) if cw.appended else {}
check(not _st.get("gate_suppressed_hash") and not _st.get("gate_log"),
      f"1д: ДЭС №3 15.10 — 14919 кончился до 09:00, хеш не переходит, хвоста нет: {cw.appended}")
# Догонка после рестарта: Работа (решение) → Стоп → Работа — хеша нет
_wa = mkseg(M(16, 9, 0), M(16, 9, 26, 23), 3, [], cause_close="RUN_STATE_CHANGE")
_sb = mkseg(M(16, 9, 26, 23), M(16, 9, 29, 22), 0, stop_kind="EMERGENCY", cause_close="SHUTDOWN_CLEARED")
_wc = mkseg(M(16, 9, 29, 33), M(17, 9, 0), 3, [mkdet("RPM_UNDERSPEED", M(16, 9, 30, 2), M(17, 9, 0), True)])
cw, _ = close_window([_wa, _sb, _wc], open_row(_gs3["gate_log"], M(16, 9, 0)),
                     {"RPM_UNDERSPEED": {"id": 14916}}, M(16, 9, 0), M(17, 9, 0))
_st = dict(cw.appended[0][1]) if cw.appended else {}
check(len(cw.inserted) == 3 and not _st.get("gate_suppressed_hash"),
      f"1д: догонка Работа → Стоп → Работа — хеш не переходит: {cw.appended}")
# Своё решение после границы (разбор вернулся с меткой после 09:00) — хеш остаётся
_tail_log = [dict(GS["gate_log"][0], ts=iso(B + timedelta(seconds=40)), episodes=[300])]
cw, _ = close_window([work], open_row(_tail_log, M(14, 9, 0)), {}, M(14, 9, 0), B)
_st = dict(cw.appended[0][1]) if cw.appended else {}
check(_st.get("gate_suppressed_hash") == H_RPM and [g.get("kind") for g in _st.get("gate_log")] == ["llm"],
      f"1д: решение после границы — хвост и хеш в новой строке: {cw.appended}")
# Рез пропущен в переходном режиме (ни один сегмент не закрыт) — строка та же, хеш остаётся
_warm = mkseg(M(14, 8, 59), B, 2, [])
cw, _ = close_window([_warm], open_row(GS["gate_log"], M(14, 8, 59)), {}, M(14, 8, 59), B)
_st = dict(cw.appended[0][1]) if cw.appended else {}
check(not cw.inserted and _st.get("gate_suppressed_hash") == H_RPM,
      f"1д: рез пропущен — та же строка, хеш остаётся: {cw.appended}")
for n, f in _orig_cw.items():
    setattr(eng.online_db, n, f)
eng._run_segment, eng._collect_and_enrich_detections = _orig_run, _orig_collect
_verifier.fire_verify = _orig_fv

# ── 1е. Путь смены режима: пересечение по последнему закрытому и остающемуся ──
import inspect  # noqa: E402
_uw = inspect.getsource(eng.OnlinePollEngine._update_open_window)
_cw = inspect.getsource(eng.OnlinePollEngine._close_window)
check("_gate_suppressed_scenarios(_rs_gate_state, closed_segs[-1], _rs_open_row," in _uw
      and "closed=closed_segs)" in _uw
      and "live_end=_live_alert_keys(_rs_last), live_open=_live_keys" in _uw
      and "open_run_state=open_seg.run_state" in _uw and "_rs_closed_info" in _uw,
      "1е: смена режима — новые тревоги по последнему закрытому, перенос — пересечение (В5)")
check("_gate_carry(" in _cw and "_closed_info" in _cw
      and '_tail["gate_suppressed_hash"] = _gate_state["suppressed_hash"]' in _cw,
      "1е: суточный рез — правило переноса по всем закрытым")
check("ts=t_to)" in _uw.split("Отложенная проверка снятия гейтом")[1][:400],
      "1е: отложенная проверка — с моментом цикла")


# ── 1ж. Гейт: своё решение, наследование во время разбора, повтор с источником ──
import corpus.settings as cs  # noqa: E402
import llm.router as router  # noqa: E402

G: dict = {}
_GATE_TS = {"t": None}


async def _g_open(*a):
    return {"id": 1, "t_start": M(16, 9, 0), "run_state": 3, "warning_analyses": [], "gate_log": [],
            "active_detections_json": [LP]}


async def _g_none(*a, **k):
    return None


async def _g_empty(*a, **k):
    return []


async def _g_counts(*a, **k):
    return {}


async def _g_true(*a, **k):
    return True


async def _g_event(*a, event=None, segment_id=None, **k):
    G["events"].append((event, segment_id))
    return True


async def _g_save(*a, **k):
    return True


async def _g_mark(r, e, p, scenarios, **k):
    G["marks"].append(k)
    if k.get("until") is not None:
        _GATE_TS["t"] = k["until"]
        return [11]
    return [12] if k.get("after") is not None else []


async def _g_llm(prompt, model, depth=None, meta=None):
    return "Анализ.", "cancel", "ложное", 10, 5


_G_NAMES = ("get_open_segment", "get_run_state_origin_ts", "count_episodes_batch", "get_open_episodes",
            "get_episodes_overlapping", "set_segment_gate_suppression", "append_segment_gate_event",
            "save_segment_warning", "set_episodes_gate_suppressed", "resolve_segment_id")
_orig_g = {n: getattr(odb, n) for n in _G_NAMES}
for n, f in zip(_G_NAMES, (_g_open, _g_none, _g_counts, _g_empty, _g_empty, _g_true, _g_event, _g_save,
                           _g_mark, _g_none)):
    setattr(odb, n, f)
_orig_llm = mgr._run_warning_gate_llm
mgr._run_warning_gate_llm = _g_llm
mgr._GATE_SAVE_RETRY_SEC = 0
_orig_cs = cs.get_claude_settings
cs.get_claude_settings = lambda: {"model": "claude-sonnet-5-5", "max_tokens": 1000, "proxy": ""}
router.apply_depth("gate_caution", "fast", "low", "interactive", provider="llm")
_STRUCT = sa.build_structural_status({"id": 1, "run_state": 3, "t_start": M(16, 9, 0), "characteristics_json": {},
                                      "current_values_json": {"values": {}}, "active_detections_json": [LP]})
G.update({"events": [], "marks": []})
_out = asyncio.run(mgr._analyze_warning_claude("SN", "pcc", 1, dict(_STRUCT), "fh1"))
_m_until = [m for m in G["marks"] if m.get("until") is not None]
_m_after = [m for m in G["marks"] if m.get("after") is not None]
check(_out == mgr.GATE_OK and len(_m_until) == 1 and _m_until[0]["src"]["kind"] == "llm"
      and _m_until[0]["src"]["segment_id"] is None and _m_until[0]["src"]["ts"] == _GATE_TS["t"].isoformat(),
      f"1ж: своё решение — эпизоды, открытые на момент разбора, источник — разбор: {G['marks']}")
_inh = [ev for ev, _sid in G["events"] if ev.get("kind") == "inherited"]
_dec = [ev for ev, _sid in G["events"] if ev.get("kind") == "llm"]
check(len(_m_after) == 1 and _m_after[0]["src"]["kind"] == "inherited" and len(_inh) == 1
      and _inh[0]["episodes"] == [12] and _inh[0]["source"]["ts"] == _GATE_TS["t"].isoformat(),
      f"1ж: открытые в этой строке во время разбора — наследуют, запись inherited: {G['events']}")
check(len(_dec) == 1 and _dec[0]["suppressed_hash"] == H_LP and _dec[0]["episodes"] == [11]
      and _dec[0]["provider"] == "llm",
      f"1ж: запись разбора — вид llm, хеш снятия, снятые эпизоды: {_dec}")
for n, f in _orig_g.items():
    setattr(odb, n, f)
mgr._run_warning_gate_llm = _orig_llm
cs.get_claude_settings = _orig_cs

# Повтор #35891: запись repeat с источником — разбор 09:01:59
G.update({"events": [], "marks": []})
for n, f in (("set_segment_gate_suppression", _g_true), ("set_episodes_gate_suppressed", _g_mark),
             ("append_segment_gate_event", _g_event)):
    setattr(mgr.online_db, n, f)


async def _mark_ids(r, e, p, scenarios, **k):
    G["marks"].append(k)
    return [14910, 14911]
mgr.online_db.set_episodes_gate_suppressed = _mark_ids
_row91 = {"id": 35891, "gate_suppressed_hash": "ed469fb1bf9a-состав", "gate_log": json.dumps(LOG_35891)}
_st91 = {"analytics_alarms": [LP], "panel_alarms": [], "panel_severity": "норма",
         "analytics_suppressed": False, "severity_level": "предупреждение"}
asyncio.run(mgr.OnlineManager._reapply_cancel(None, NS(router_sn="R", equip_type="pcc", panel_id=1),
                                              _row91, _st91, "a36ac7f457e4"))
_rp = [ev for ev, _sid in G["events"] if ev.get("kind") == "repeat"]
check(len(_rp) == 1 and _rp[0]["source"]["ts"] == "2026-10-14T06:01:59.698684+00:00"
      and _rp[0]["suppressed_hash"] == H_LP and _rp[0]["episodes"] == [14910, 14911]
      and G["marks"][0]["src"] == {"ts": "2026-10-14T06:01:59.698684+00:00", "segment_id": None,
                                   "kind": "repeat"},
      f"1ж: повтор #35891 — источник 09:01:59, хеш, эпизоды: {G['events']}, {G['marks']}")
for n, f in _orig_g.items():
    setattr(odb, n, f)


# ── 1з. Подписи в сводке, примечании вердикта и входе заключения ──
check(SUMMARY_PARTS_VERSION == 4, "1з: версия частей сводки 4 (одна правка на всю версию)")
check(fmt_local("2026-10-14T00:36:51+00:00", MSK) == "03:36:51"
      and fmt_local("2026-10-14T00:36:51+00:00", MSK, day=True, seconds=False) == "14.10 03:36"
      and tz_label(MSK) == "МСК", "1з: местное время подписей")
T0 = M(16, 9, 57, 55)


def sseg():
    sub = NS(characteristics={}, risk_accumulators=NS(coking_risk=NS(risk_level="GREEN")))
    return NS(run_state=3, stop_kind=None, cause_close=None, sequence_checks=[],
              t_start=iso(T0), t_end=iso(T0 + timedelta(hours=1)), cause_open="RUN_STATE_CHANGE",
              subsegments=[sub])


def sep(t_open, supp=False, src=None, closed=False, sc="RPM_UNDERSPEED"):
    return {"source": "analytics", "scenario": sc, "severity": "CAUTION", "gate_suppressed": supp,
            "gate_suppressed_src": json.dumps(src) if src else None,
            "t_open": t_open, "t_close": t_open + timedelta(seconds=40) if closed else None,
            "active_sec": 40.0, "blind_sec": 0.0}


_own_src = {"ts": iso(M(16, 9, 59, 10)), "segment_id": None, "kind": "llm"}
p = build_summary_parts([sseg()], [sep(M(16, 9, 58, 12), True, _own_src, closed=True)], tz=MSK,
                        t_from=T0, t_to=T0 + timedelta(hours=1))
md = render_summary_md(p)
check(p["remarks"][0]["supp_label"] == "снято ИИ (разбор 09:59)" and "— *снято ИИ (разбор 09:59)*" in md
      and "Предупреждения аналитики (1) сняты ИИ (разбор 09:59)." in md,
      f"1з: решение в этой карточке — «снято ИИ (разбор 09:59)»: {md}")
_far = {"ts": "2026-10-14T00:36:51+00:00", "segment_id": 35876, "kind": "carry"}
p = build_summary_parts([sseg()], [sep(M(16, 9, 0), True, _far)], tz=MSK, t_from=T0,
                        t_to=T0 + timedelta(hours=1))
md = render_summary_md(p)
check("— *снято по разбору от 14.10 03:36 (#35876)*" in md
      and "Предупреждения аналитики (1) сняты по разбору от 14.10 03:36 (#35876)." in md,
      f"1з: решение раньше — «снято по разбору от 14.10 03:36 (#35876)»: {md}")
p = build_summary_parts([sseg()], [sep(M(16, 9, 58, 12), True, _own_src, closed=True),
                                   sep(M(16, 10, 20), False)], tz=MSK, t_from=T0, t_to=T0 + timedelta(hours=1))
md = render_summary_md(p)
check("снято ИИ (разбор 09:59): 1 из 2" in md, f"1з: частичное снятие с подписью: {md}")
p = build_summary_parts([sseg()], [sep(M(16, 9, 58, 12), True, None, closed=True)], tz=MSK,
                        t_from=T0, t_to=T0 + timedelta(hours=1))
check("— *снято ИИ*" in render_summary_md(p) and "supp_label" not in p["remarks"][0],
      "1з: без источника (старые эпизоды) — «снято ИИ»")
_old = json.loads(json.dumps(p))
_old["version"] = 3
check("— *снято ИИ*" in render_summary_md(_old), "1з: старые части — «снято ИИ»")
# Вход заключения — та же подпись
from corpus.preprocessor import _remarks_from_summary  # noqa: E402
p = build_summary_parts([sseg()], [sep(M(16, 9, 0), True, _far)], tz=MSK, t_from=T0,
                        t_to=T0 + timedelta(hours=1))
_rm = _remarks_from_summary({"report_summary_json": p})
check("снято по разбору от 14.10 03:36 (#35876)" in _rm and "снято ИИ" not in _rm,
      f"1з: вход заключения — не «снято ИИ» про чужой разбор: {_rm}")
# Поздний вердикт по границе: снимается эпизод, начавшийся не позже разбора
p = build_summary_parts([sseg()], [sep(M(16, 10, 0), False), sep(M(16, 10, 30), False)], tz=MSK,
                        t_from=T0, t_to=T0 + timedelta(hours=1))
p["remarks"][0]["at_end"] = True
_g0 = p["remarks"][0]
check(_g0["open_live"] == 2 and len(_g0["open_live_t"]) == 2, f"1з: открытые с началом: {_g0}")
p2 = apply_gate_suppression(p, ["RPM_UNDERSPEED"], until=M(16, 10, 10),
                            src={"ts": iso(M(16, 10, 10)), "segment_id": 35949})
g2 = p2["remarks"][0]
check(g2["suppressed"] == 1 and g2["open_live"] == 1 and g2["at_end"] is True
      and g2["supp_label"] == "снято ИИ (разбор 10:10)" and p2["verdict"]["level"] == "CAUTION",
      f"1з: поздний вердикт — только начавшийся до разбора, подпись: {g2}, {p2['verdict']}")
_legacy = json.loads(json.dumps(p))
for g in _legacy["remarks"]:
    g.pop("open_live_t", None)
p3 = apply_gate_suppression(_legacy, ["RPM_UNDERSPEED"], until=M(16, 10, 10))
check(p3["remarks"][0]["suppressed"] == 2 and p3["remarks"][0]["open_live"] == 0,
      "1з: части без начала эпизодов — все открытые, как раньше")
# Синтетика закрываемого сегмента — подпись своего решения
_syn_src = eng._gate_own_src({"suppressed_hash": H_LP, "gate_log": OWN_LOG})
check(_syn_src == {"ts": iso(M(14, 9, 59)), "segment_id": None, "kind": "inherited"}
      and eng._gate_own_src({"suppressed_hash": H_RPM, "gate_log": CARRY_ROW_LOG}) is None,
      "1з: источник синтетики — своё решение строки, не перенос")


# ── 1и. Разовый перенос при старте ──
class MigConn:
    def __init__(self, rows, eps):
        self.rows, self.eps = rows, eps

    async def fetch(self, sql, *a):
        return [dict(r) for r in self.rows if r["t_end"] is None]

    async def execute(self, sql, *a):
        if "UPDATE auto_segments" in sql:
            n = 0
            for r in self.rows:
                if r["id"] == a[0] and r["gate_suppressed_hash"] == a[1]:
                    r["gate_suppressed_hash"] = None
                    n += 1
            return f"UPDATE {n}"
        n = 0
        for x in self.eps:
            if ((x["router_sn"], x["equip_type"], x["panel_id"]) == a[:3] and x["gate_suppressed"]
                    and x["t_open"] >= a[3]):
                x["gate_suppressed"], x["gate_suppressed_src"] = False, None
                n += 1
        return f"UPDATE {n}"

    def transaction(self):
        class _Tx:
            async def __aenter__(s):
                return s

            async def __aexit__(s, *exc):
                return False
        return _Tx()

    async def close(self):
        return None


_rows = [
    {"id": 35949, "router_sn": "6003790403", "equip_type": "pcc", "panel_id": 1, "t_start": M(16, 9, 57, 55),
     "t_end": None, "gate_suppressed_hash": "10d98a7ad8c7", "gate_log": "[]"},
    {"id": 35923, "router_sn": "1126246373", "equip_type": "pcc", "panel_id": 1, "t_start": M(16, 9, 0),
     "t_end": None, "gate_suppressed_hash": "71bf001698d8", "gate_log": None},
    {"id": 35999, "router_sn": "X", "equip_type": "pcc", "panel_id": 1, "t_start": M(16, 9, 0),
     "t_end": None, "gate_suppressed_hash": "own", "gate_log": json.dumps(
         [{"ts": iso(M(16, 9, 30)), "provider": "llm", "decision": "cancel", "decision_applied": True,
           "fault_hash": "f"}])},
]
_eps = [
    {"id": 14931, "router_sn": "6003790403", "equip_type": "pcc", "panel_id": 1, "t_open": M(16, 9, 58, 12),
     "gate_suppressed": True, "gate_suppressed_src": None},
    {"id": 14916, "router_sn": "6003790403", "equip_type": "pcc", "panel_id": 1, "t_open": M(14, 9, 0),
     "gate_suppressed": True, "gate_suppressed_src": None},
    {"id": 14990, "router_sn": "X", "equip_type": "pcc", "panel_id": 1, "t_open": M(16, 9, 31),
     "gate_suppressed": True, "gate_suppressed_src": None},
]
_mc = MigConn(_rows, _eps)
_orig_connect = odb._connect


async def _mconn():
    return _mc
odb._connect = _mconn
_res1 = asyncio.run(odb.reset_carried_gate_suppression())
check(_res1 == (2, 1) and _rows[0]["gate_suppressed_hash"] is None and _rows[1]["gate_suppressed_hash"] is None
      and _rows[2]["gate_suppressed_hash"] == "own",
      f"1и: чужие хеши открытых строк сброшены, строка со своим «снять» не тронута: {_res1}")
check(not _eps[0]["gate_suppressed"] and _eps[1]["gate_suppressed"] and _eps[2]["gate_suppressed"],
      "1и: 14931 (закрыт, в строке без своего решения) — снятие обнулено; 14916 (раньше строки) и "
      "эпизод строки со своим решением — нет")
check(asyncio.run(odb.reset_carried_gate_suppression()) == (0, 0), "1и: повторный запуск ничего не меняет")
odb._connect = _orig_connect
_main = (ROOT / "main.py").read_text(encoding="utf-8")
_blk = _main.split('get_app_setting("gate_carry_version", "") != "5.1.22"')[1].split("except Exception")[0]
check("reset_carried_gate_suppression" in _blk
      and _blk.index("_reset_carried()") < _blk.index('set_app_setting("gate_carry_version", "5.1.22")'),
      "1и: ключ gate_carry_version ставится только после успешного переноса")
check('GATE_RULES_VERSION = "5.1.15"' in (ROOT / "online" / "status_assembler.py").read_text(encoding="utf-8"),
      "1и: GATE_RULES_VERSION не поднят")

# ── 1к. API: _active_dets тем же предикатом, счётчики только по разборам моделью ──
_rsrc = (ROOT / "web" / "routes.py").read_text(encoding="utf-8")
_tree = ast.parse(_rsrc)
_fn = next(n for n in _tree.body if isinstance(n, ast.FunctionDef) and n.name == "_active_dets")
from web.segment_view import _parse_json as _pj  # noqa: E402
_ns: dict = {"_parse_json": _pj}
exec(compile(ast.Module(body=[_fn], type_ignores=[]), "routes._active_dets", "exec"), _ns)
_ad = _ns["_active_dets"]
_seg_open = {"active_detections_json": json.dumps([LP]), "gate_suppressed_hash": H_LP, "t_start": R0,
             "characteristics_json": {}}
check(_ad(_seg_open)[1] is True
      and _ad(dict(_seg_open, active_detections_json=json.dumps([LP, panel("WARNING", 1464, M(14, 9, 10))])))[1]
      is False, "1к: API — снятие не действует при сигнале панели (тот же предикат)")
check("gate_entry_kind(e) in GATE_MODEL_KINDS" in _rsrc, "1к: счётчики гейта — только разборы моделью")
_cnt_log = [{"kind": "incident"}, {"provider": "llm", "decision_applied": True},
            {"provider": "api", "decision_applied": False}, {"provider": "repeat", "decision_applied": True},
            {"provider": "carried", "decision_applied": True}, {"kind": "inherited", "decision_applied": True},
            {"kind": "carry", "decision_applied": True}, {"kind": "late", "decision_applied": True}]
_gl = [x for x in _cnt_log if sa.gate_entry_kind(x) in sa.GATE_MODEL_KINDS]
check(len(_gl) == 2 and sum(1 for x in _gl if x.get("decision_applied")) == 1,
      "1к: «Гейт ИИ обработал» — 2 разбора, снято 1 (без повтора, наследования, переноса)")

# ── 1л. Схема ──
_sql = (ROOT / "db" / "online_schema.sql").read_text(encoding="utf-8")
_stmt = _sql.split("ADD COLUMN IF NOT EXISTS gate_suppressed_src")[0].rsplit("ALTER TABLE", 1)[1]
check("ADD COLUMN IF NOT EXISTS gate_suppressed_src JSONB;" in _sql and "alarm_episodes" in _stmt
      and "CHECK" not in _sql.split("gate_suppressed_src JSONB;")[0].rsplit("ALTER TABLE", 1)[1],
      "1л: alarm_episodes.gate_suppressed_src JSONB — ADD COLUMN IF NOT EXISTS, без CHECK")


# ══ 2. P1-2 код 40012 на закрытии, P1-3 обрыв аварией, шапка заключения ══════

from dataclasses import replace as dc_replace  # noqa: E402

from analytics import binding  # noqa: E402
from analytics.labels import (  # noqa: E402
    engine_hours_text, fault_code_close_text, run_trip_name, run_trip_text, split_code_label,
)
from analytics.segmenter import (  # noqa: E402
    _engine_hours_at, _state_periods, _trip_mark, fault_code_close, segment, shutdown_intervals,
)
from analytics.serializer import add_run_trip, to_markdown  # noqa: E402
from analytics.fault_ref import FaultRef  # noqa: E402
from corpus.preprocessor import (  # noqa: E402
    build_claude_input, conclusion_facts, extract_verdict_alarm,
)
from llm.router import depth_for_segment  # noqa: E402
from web.segment_view import severity_from_parts  # noqa: E402

KB_CFG = binding.build_config(ROOT / "knowledge_base", controller_id="pcc3300", engine_id="cummins_kta50")
FREF = FaultRef(str(ROOT / "knowledge_base" / "controllers" / "pcc3300"))
_LAB12 = {0: "Нет", 1448: "Пониженная частота (ОСТАНОВ)",
          234: "Разнос двигателя — превышение частоты вращения (ОСТАНОВ)",
          1438: "Отказ прокрутки стартером (ОСТАНОВ)", 1248: "Предупреждение двигателя — общее (ПРЕДУПРЕЖДЕНИЕ)",
          1453: "Отказ отключения автомата генераторной установки (ПРЕДУПРЕЖДЕНИЕ)", 65535: "65535"}
_LAB = {40010: {0: "Выкл", 1: "Авто", 2: "Ручной"}, 40569: {0: "Не активно", 1: "Активно"},
        40013: {0: "Нет", 1: "Предупреждение", 3: "Останов с охлаждением", 4: "Останов (авария)"},
        40012: _LAB12}


def epers(addr, *spans):
    """Периоды одного регистра: (значение, начало[, конец]) подряд; конец — начало
    следующего, у последнего — открыт."""
    out = []
    for i, sp in enumerate(spans):
        v, s = sp[0], sp[1]
        e = sp[2] if len(sp) > 2 else (spans[i + 1][1] if i + 1 < len(spans) else None)
        out.append({"addr": addr, "value": v, "state_start": s, "state_end": e,
                    "label": _LAB.get(addr, {}).get(v, str(v))})
    return out


def mask(addr, bit, s, e, name, sev="shutdown"):
    return {"addr": addr, "bit": bit, "fault_start": s, "fault_end": e, "severity": sev,
            "fault_name_ru": name, "fault_name": name}


# Серия ДГУ №1 16.10 09:25–09:30 (сырые периоды — как в БД, проверено чтением)
W0, W1 = M(16, 9, 20), M(16, 9, 40)
ENUM_1610 = (
    epers(40011, (0, W0 - timedelta(hours=5)), (3, M(16, 9, 25, 52)), (0, M(16, 9, 26, 23)),
          (3, M(16, 9, 28, 33)), (0, M(16, 9, 28, 53)), (3, M(16, 9, 29, 33)))
    + epers(40012, (0, M(12, 16, 44, 23)), (1448, M(16, 9, 26, 23)), (0, M(16, 9, 26, 43)),
            (234, M(16, 9, 28, 53)), (0, M(16, 9, 29, 22)))
    + epers(40013, (0, M(12, 16, 44, 23)), (4, M(16, 9, 26, 23)), (0, M(16, 9, 26, 43)),
            (4, M(16, 9, 28, 53)), (0, M(16, 9, 29, 22)))
    + epers(40569, (0, M(1, 16, 39, 42)), (1, M(16, 9, 26, 32)), (0, M(16, 9, 26, 43)),
            (1, M(16, 9, 29, 22)), (0, M(16, 9, 29, 32)))
    + epers(40010, (0, M(16, 3, 45, 32)), (2, M(16, 9, 25, 52)), (0, M(16, 9, 26, 43)),
            (2, M(16, 9, 28, 33)), (0, M(16, 9, 29, 22)), (2, M(16, 9, 29, 33))))
FAULTS_1610 = [mask(40408, 1, M(16, 9, 26, 23), M(16, 9, 26, 43), "Пониженная частота"),
               mask(40401, 13, M(16, 9, 28, 53), M(16, 9, 29, 22), "Скорость/положение КВ: выше нормы")]
EH_ADDR = KB_CFG.role_to_addr("ENGINE_HOURS")
EH_SEC = 13642.6 * 3600      # 40070 в БД — секунды


def hist_runs(runs, w0=W0, w1=W1, eh=None):
    """Обороты/частота: 1500/50 в «Работе», 0 в стопе — раз в 10 с; eh — [(метка,
    секунды наработки)]."""
    rows = []
    t = w0
    while t < w1:
        on = any(a <= t < b for a, b in runs)
        rows.append({"addr": 40068, "ts": t, "value": 1500.0 if on else 0.0})
        rows.append({"addr": 40044, "ts": t, "value": 50.0 if on else 0.0})
        t += timedelta(seconds=10)
    for ts, v in eh or []:
        rows.append({"addr": EH_ADDR, "ts": ts, "value": v})
    rows.sort(key=lambda r: r["ts"])
    return rows


RUNS_1610 = [(M(16, 9, 25, 52), M(16, 9, 26, 23)), (M(16, 9, 28, 33), M(16, 9, 28, 53)),
             (M(16, 9, 29, 33), W1)]


def seg_run(enum, faults, hist, w0=W0, w1=W1, cfg=KB_CFG, seed=None):
    return segment(enum_periods=enum, history=hist, fault_periods=faults, gaps=[], cfg=cfg,
                   router_sn="T", equip_type="pcc", panel_id=1, engine_sn="", ts_from=w0, ts_to=w1,
                   load_seed=seed)


def by_start(segs, t):
    return next((s for s in segs if datetime.fromisoformat(s.t_start) == t), None)


SEGS = seg_run(ENUM_1610, FAULTS_1610, hist_runs(RUNS_1610, eh=[(M(16, 9, 21), EH_SEC)]))
s29, s31 = by_start(SEGS, M(16, 9, 25, 52)), by_start(SEGS, M(16, 9, 26, 23))
s39, s41 = by_start(SEGS, M(16, 9, 28, 33)), by_start(SEGS, M(16, 9, 28, 53))
s44 = by_start(SEGS, M(16, 9, 29, 33))
check(all(x is not None for x in (s29, s31, s39, s41, s44))
      and s31.stop_kind == "EMERGENCY" and s41.stop_kind == "EMERGENCY"
      and s31.cause_close == "SHUTDOWN_CLEARED" and s29.run_state == 3,
      f"2: нарезка серии 16.10: {[(s.run_state, s.t_start[11:19], s.stop_kind, s.cause_close) for s in SEGS]}")

# ── 2а. Код 40012 на закрытии (P1-2, В7) ──
f31 = s31.fault_code_close or {}
check(f31.get("code") == 1448 and f31.get("name") == "Пониженная частота" and f31.get("reset") is True
      and f31.get("reset_at") == iso(M(16, 9, 26, 32)) and f31.get("switch_off_at") == iso(M(16, 9, 26, 43))
      and f31.get("next_code") == 0 and f31.get("at_end") is True and f31.get("switch_label") == "Выкл",
      f"2а: #35931 — 1448 сброшен: сброс 09:26:32, снят 09:26:43, «Выкл» в кадре: {f31}")
t31 = fault_code_close_text(f31, MSK)
check(t31 == "Код 1448 сброшен: сброс неисправностей 09:26:32 (40569), код снят 09:26:43; "
             "переключатель «Выкл» (40010) в том же кадре", f"2а: текст #35931 (В7): {t31}")
check(fault_code_close_text(f31, MSK, regs=False) == "Код 1448 сброшен: сброс неисправностей 09:26:32, "
      "код снят 09:26:43; переключатель «Выкл» в том же кадре", "2а: без регистров — строка сводки")
f41 = s41.fault_code_close or {}
check(f41.get("code") == 234 and f41.get("reset_at") == iso(M(16, 9, 29, 22))
      and fault_code_close_text(f41, MSK).startswith(
          "Код 234 сброшен: сброс неисправностей 09:29:22 (40569), код снят 09:29:22"),
      f"2а: #35941 — 234 сброшен, сброс и «Выкл» в кадре снятия: {fault_code_close_text(f41, MSK)}")
check("кнопк" not in t31 and "несброш" not in t31.lower(), "2а: без «кнопки» и «несброшенной» (В7)")
check(s29.fault_code_close is None and s39.fault_code_close is None,
      "2а: «Работа», где код встаёт ровно в t_end, — записи нет")
check(KB_CFG.seg("stop_kind", "fault_reset_window_sec") == 60, "2а: окно сброса — ключ KB 60 с")


def fcc(enum, t0, t1, nxt=None):
    sp = _state_periods(enum, KB_CFG).get(40012) or []
    return fault_code_close(sp, enum, KB_CFG, t0, t1, next_state=nxt)


D0, D1 = M(13, 10, 45, 7), M(13, 13, 11, 45)       # ДЭС №3 #35861
E61 = (epers(40012, (0, M(13, 10, 44, 31)), (1438, M(13, 10, 45, 26)), (1248, D1), (0, M(13, 14, 59, 49)))
       + epers(40569, (0, M(13, 10, 42, 52)), (1, M(13, 13, 12, 7)), (0, M(13, 13, 12, 16)))
       + epers(40010, (2, M(13, 10, 45, 7)), (0, D1), (2, M(13, 13, 12, 26))))
r = fcc(E61, D0, D1, 0) or {}
check(r.get("code") == 1438 and r.get("next_code") == 1248 and r.get("reset_at") is None
      and r.get("switch_off_at") == iso(D1) and r.get("next_kind") == "предупреждение",
      f"2а: #35861 — 1438 → 1248, сброс переключателем (40569 через 22 с — не в кадре): {r}")
t = fault_code_close_text(r, MSK)
check(t == "Код 1438 сброшен: переключатель «Выкл» (40010), код снят 13:11:45; сразу после сброса в регистре "
          "код 1248 «Предупреждение двигателя — общее» (предупреждение) — он относится к следующему сегменту",
      f"2а: текст #35861: {t}")
# #35869: простой стоп 13:14:59–14:59:49, 1248 снят в момент пуска, 40569 — за 1 ч 47 мин
E69 = (epers(40012, (1248, D1), (0, M(13, 14, 59, 49)), (1453, M(13, 15, 1, 38)))
       + epers(40569, (1, M(13, 13, 12, 7)), (0, M(13, 13, 12, 16)))
       + epers(40010, (0, M(13, 13, 14, 59)), (2, M(13, 14, 59, 49))))
r = fcc(E69, M(13, 13, 14, 59), M(13, 14, 59, 49), 3) or {}
check(r.get("reset") is False and fault_code_close_text(r, MSK) == "Код 1248 снят в 14:59:49, в момент пуска",
      f"2а: #35869 — «снят», без «сброс» (40569 за 1 ч 47 мин): {fault_code_close_text(r, MSK)}")
# #35872: 1453 висит за концом — записи нет, отчёт прежний
check(fcc(E69, M(13, 14, 59, 49), M(14, 9, 0)) is None, "2а: код висит за концом — записи нет")
# Допуск в один кадр после снятия; подъём до появления кода — не сброс этого кода
_c0, _c1 = M(16, 9, 0), M(16, 9, 1)
_base = epers(40012, (0, _c0 - timedelta(minutes=5)), (1448, _c0), (0, _c1))
r = fcc(_base + epers(40569, (0, _c0 - timedelta(hours=1)), (1, _c1 + timedelta(seconds=5))), _c0 - timedelta(minutes=1), _c1)
check(r and r["reset_at"] == iso(_c1 + timedelta(seconds=5)), f"2а: 40569 через 5 с после снятия — сброс: {r}")
r = fcc(_base + epers(40569, (0, _c0 - timedelta(hours=1)), (1, _c1 + timedelta(seconds=15))), _c0 - timedelta(minutes=1), _c1)
check(r and r["reset"] is False, f"2а: 40569 через 15 с — уже не этот кадр, «снят»: {r}")
r = fcc(_base + epers(40569, (0, _c0 - timedelta(hours=1)), (1, _c0 - timedelta(seconds=20)), (0, _c0 - timedelta(seconds=10))),
        _c0 - timedelta(minutes=1), _c1)
check(r and r["reset"] is False, f"2а: 40569 до появления кода — не сброс этого кода: {r}")
# NA: код NA — не код; следом NA — состояние неизвестно, записи нет
_na = epers(40012, (0, _c0 - timedelta(minutes=5)), (1448, _c0), (65535, _c1))
check(fcc(_na, _c0 - timedelta(minutes=1), _c1 + timedelta(seconds=30)) is None,
      "2а: следом NA 65535 — записи нет (отчёт прежний)")
check(fcc(epers(40012, (65535, _c0), (0, _c1)), _c0 - timedelta(minutes=1), _c1) is None,
      "2а: NA вместо кода — записи нет")
check(split_code_label("Пониженная частота (ОСТАНОВ)") == ("Пониженная частота", "останов")
      and split_code_label("1448") == (None, None), "2а: метка каталога — имя и тип")

# ── 2б. Обрыв аварией (P1-3, В3) ──
tr29, tr39 = s29.closed_by_trip or {}, s39.closed_by_trip or {}
check(tr29.get("t") == iso(M(16, 9, 26, 23)) and tr29.get("codes") == [
          {"code": 1448, "name": "Пониженная частота", "kind": "останов"}]
      and tr29.get("masks") == ["Пониженная частота"] and tr29.get("run_state") == 3
      and tr29.get("next_stop_kind") == "EMERGENCY",
      f"2б: #35929 — «Работа» оборвана в 09:26:23 кодом 1448: {tr29}")
check(tr39.get("t") == iso(M(16, 9, 28, 53)) and [c["code"] for c in tr39.get("codes") or []] == [234],
      f"2б: #35939 — код 234 в 09:28:53: {tr39}")
check(s44.closed_by_trip is None, "2б: открытая «Работа» у края окна (#35944) — отметки нет")
check(all(s.closed_by_trip is None for s in SEGS if s.run_state == 0), "2б: стоп-сегменты не отмечаются")
check(run_trip_name(3) == "Работа прервана аварийным остановом" and run_trip_name(2) == "Прогрев прерван аварийным остановом"
      and run_trip_name(5) == "Охлаждение на х.х. прервано аварийным остановом"
      and run_trip_text(tr39) == "код 234 «Разнос двигателя — превышение частоты вращения»",
      "2б: имя по режиму и текст кода")
# 3 → простой стоп — нет
_e_simple = (epers(40011, (3, W0 - timedelta(hours=1)), (0, M(16, 9, 30))) + epers(40012, (0, W0 - timedelta(days=1)))
             + epers(40013, (0, W0 - timedelta(days=1))))
_ss = seg_run(_e_simple, [], hist_runs([(W0 - timedelta(hours=1), M(16, 9, 30))]))
check(all(s.closed_by_trip is None for s in _ss) and _ss[0].cause_close == "RUN_STATE_CHANGE",
      "2б: «Работа» → простой стоп — отметки нет")
# 2 → аварийный стоп — «Прогрев прерван»
_e_warm = (epers(40011, (0, W0 - timedelta(hours=1)), (2, M(16, 9, 25)), (0, M(16, 9, 26)))
           + epers(40012, (0, W0 - timedelta(days=1)), (151, M(16, 9, 26)), (0, M(16, 9, 27)))
           + epers(40013, (0, W0 - timedelta(days=1)), (4, M(16, 9, 26)), (0, M(16, 9, 27))))
_sw = seg_run(_e_warm, [], hist_runs([(M(16, 9, 25), M(16, 9, 26))]))
_w = by_start(_sw, M(16, 9, 25))
check(_w is not None and _w.run_state == 2 and (_w.closed_by_trip or {}).get("codes", [{}])[0].get("code") == 151,
      f"2б: «Прогрев» → аварийный стоп — отметка: {[(s.run_state, s.closed_by_trip) for s in _sw]}")
# 3 → 5 при 40013 = 3 (останов с охлаждением) — отметка только у «Работы»
_e_cd = (epers(40011, (3, W0 - timedelta(hours=1)), (5, M(16, 9, 25)), (0, M(16, 9, 28)))
         + epers(40012, (0, W0 - timedelta(days=1)), (2224, M(16, 9, 25)), (0, M(16, 9, 30)))
         + epers(40013, (0, W0 - timedelta(days=1)), (3, M(16, 9, 25)), (0, M(16, 9, 30))))
_sc = seg_run(_e_cd, [], hist_runs([(W0 - timedelta(hours=1), M(16, 9, 28))]))
_r3, _r5 = by_start(_sc, W0), by_start(_sc, M(16, 9, 25))
_r0 = by_start(_sc, M(16, 9, 28))
check(_r3 is not None and _r3.closed_by_trip and _r5 is not None and _r5.closed_by_trip is None
      and _r0 is not None and _r0.stop_kind == "EMERGENCY",
      f"2б: 3 → 5 → стоп при 40013 = 3 — отметка только у «Работы»: "
      f"{[(s.run_state, bool(s.closed_by_trip), s.stop_kind) for s in _sc]}")
# Маска кадром позже 40011 — момент — смена режима
_e_lag = (epers(40011, (3, W0 - timedelta(hours=1)), (0, M(16, 9, 30))) + epers(40012, (0, W0 - timedelta(days=1)))
          + epers(40013, (0, W0 - timedelta(days=1))))
_sl = seg_run(_e_lag, [mask(40401, 13, M(16, 9, 30, 2), M(16, 9, 31), "Скорость/положение КВ: выше нормы")],
              hist_runs([(W0 - timedelta(hours=1), M(16, 9, 30))]))
_sl0 = by_start(_sl, M(16, 9, 30))
check(_sl[0].closed_by_trip is None and _sl0 is not None and _sl0.stop_kind == "SIMPLE",
      f"2б: маска через 2 с после 40011 — стоп простой, отметки нет (вид стопа и отметка согласованы, "
      f"ревью verdict-5): {_sl[0].closed_by_trip}, {_sl0 and _sl0.stop_kind}")
# Маска аварийной тяжести посреди работы, останова по ней нет — не обрыв
_sm = seg_run(_e_simple, [mask(40401, 13, M(16, 9, 25), M(16, 9, 25, 30), "Скорость/положение КВ: выше нормы")],
              hist_runs([(W0 - timedelta(hours=1), M(16, 9, 30))]))
check(all(s.closed_by_trip is None for s in _sm), "2б: маска посреди работы без останова — отметки нет")
# stop_kind выключен — нет
_cfg_off = binding.build_config(ROOT / "knowledge_base", controller_id="pcc3300", engine_id="cummins_kta50")
_cfg_off.segmentation["stop_kind"] = dict(_cfg_off.segmentation.get("stop_kind") or {}, enabled=False)
_soff = seg_run(ENUM_1610, FAULTS_1610, hist_runs(RUNS_1610), cfg=_cfg_off)
check(all(s.closed_by_trip is None for s in _soff), "2б: stop_kind.enabled = false — отметки нет")
# Задержка пуска перед плашкой неудачной попытки с аварией — не отмечается (у плашки своя подпись)
_e_pl = (epers(40011, (0, W0 - timedelta(hours=1)), (1, M(16, 9, 25)), (2, M(16, 9, 25, 10)), (0, M(16, 9, 25, 20)))
         + epers(40012, (0, W0 - timedelta(days=1)), (1438, M(16, 9, 25, 20)), (0, M(16, 9, 26)))
         + epers(40013, (0, W0 - timedelta(days=1)), (4, M(16, 9, 25, 20)), (0, M(16, 9, 26))))
_sp = seg_run(_e_pl, [], hist_runs([]))
check(any(s.attempt for s in _sp) and all(s.closed_by_trip is None for s in _sp),
      f"2б: задержка пуска → плашка с аварией — отметок нет: "
      f"{[(s.run_state, s.run_state_label, bool(s.closed_by_trip)) for s in _sp]}")

# ── 2в. Отчёт и сводка ──
rep31 = to_markdown([s31], "R", "pcc", 1, W0, W1, tz=MSK, fault_ref=FREF)
check("Несброшенная" not in rep31 and "не сброшен кнопкой" not in rep31
      and "**Код неисправности на закрытии (регистр 40012) (тип: Немедленный останов (Shutdown)):** "
          "Код 1448 сброшен: сброс неисправностей 09:26:32 (40569), код снят 09:26:43" in rep31
      and "**Справочник кода:**" in rep31 and "**Код 1448**" in rep31,
      f"2в: отчёт #35931 — «сброшен», справочник кода, без «Несброшенной»")
rep29 = to_markdown([s29], "R", "pcc", 1, W0, W1, tz=MSK, fault_ref=FREF)
check("**Причина закрытия:** RUN_STATE_CHANGE — аварийный останов панели в 2026-10-16 09:26:23 Europe/Moscow, "
      "код 1448 «Пониженная частота»" in rep29, "2в: отчёт #35929 — причина закрытия с остановом")
# Висящий код — прежний текст
_hang = seg_run(epers(40011, (3, W0 - timedelta(hours=1))) + epers(40012, (1453, W0 - timedelta(hours=2)))
                + epers(40013, (1, W0 - timedelta(hours=2))), [], hist_runs([(W0 - timedelta(hours=1), W1)]))
_rh = to_markdown(_hang, "R", "pcc", 1, W0, W1, tz=MSK, fault_ref=FREF)
check("**⚠ Несброшенная неисправность:** код `1453`" in _rh and _hang[0].fault_code_close is None,
      "2в: код висит на конце — «Несброшенная», как раньше")
p31 = build_summary_parts([s31], [], tz=MSK)
md31 = render_summary_md(p31)
check(p31["fault_code"]["code"] == 1448 and p31["fault_code"]["text"].startswith("Код 1448 сброшен")
      and "⏱ **Авария снята** — время до устранения: **20 с** (от первого кода до сброса). Код 1448 сброшен: "
          "сброс неисправностей 09:26:32, код снят 09:26:43" in md31,
      f"2в: часть fault_code и строка «⏱ Авария снята»: {md31}")
_p69 = build_summary_parts([dc_replace(s31, stop_kind="SIMPLE", cause_close="RUN_STATE_CHANGE")], [], tz=MSK)
check(_p69["mttr"] is None and "Код 1448" not in render_summary_md(_p69),
      "2в: у простого стопа код в сводку не выносится (только с «⏱ Авария снята»)")
_rpm_ep = {"source": "analytics", "scenario": "RPM_UNDERSPEED", "severity": "CAUTION", "gate_suppressed": False,
           "t_open": M(16, 9, 26, 12), "t_close": M(16, 9, 26, 23), "active_sec": 11.0, "blind_sec": 0.0}
# Просадка 09:26:12–09:26:23 — детекция в сегменте (иначе эпизод «не подтверждён»)
_s29d = dc_replace(s29, subsegments=[dc_replace(sub, detections=list(sub.detections) + (
    [mkdet("RPM_UNDERSPEED", M(16, 9, 26, 12), M(16, 9, 26, 23), False)] if k == len(s29.subsegments) - 1
    else [])) for k, sub in enumerate(s29.subsegments)])
p29 = build_summary_parts([_s29d], [_rpm_ep], tz=MSK, t_from=M(16, 9, 25, 52), t_to=M(16, 9, 26, 23))
md29 = render_summary_md(p29)
_g = [g for g in p29["remarks"] if g.get("scenario") == "RUN_TRIP"]
check(p29["verdict"]["title"] == "🟡 ЗАМЕЧАНИЯ К РАБОТЕ — 2" and len(_g) == 1
      and "- 🟡 **Работа прервана аварийным остановом** [панель]: 09:26:23 — код 1448 «Пониженная частота»" in md29,
      f"2в: #35929 — ЗАМЕЧАНИЯ — 2: просадка и обрыв аварией: {md29}")
p39 = build_summary_parts([s39], [], tz=MSK)
check(p39["verdict"]["level"] == "CAUTION" and p39["verdict"]["title"] == "🟡 ЗАМЕЧАНИЯ К РАБОТЕ — 1"
      and p39["end_level"] == "НОРМА" and severity_from_parts(p39, None) == "CAUTION",
      f"2в: #35939 — «НОРМА» → ЗАМЕЧАНИЯ — 1, карточка жёлтая: {p39['verdict']}")
# Открытый эпизод (поздний вердикт снимает открытые) — отметка обрыва при этом остаётся
_p29o = build_summary_parts([_s29d], [dict(_rpm_ep, t_close=None)], tz=MSK, t_from=M(16, 9, 25, 52),
                            t_to=M(16, 9, 26, 23))
_ps = apply_gate_suppression(_p29o, ["RPM_UNDERSPEED"])
check(any(g.get("scenario") == "RUN_TRIP" for g in _ps["remarks"])
      and _ps["verdict"]["title"] == "🟡 ЗАМЕЧАНИЯ К РАБОТЕ — 1",
      f"2в: поздняя отмена гейтом просадки — отметка остаётся: {_ps['verdict']}")
# Поздняя доработка: сегмент закрыт до аварийности стопа
_p_plain = build_summary_parts([dc_replace(s39, closed_by_trip=None)], [], tz=MSK)
check(_p_plain["verdict"]["level"] == "НОРМА", "2в: без отметки — НОРМА (как было у #35939)")
_p_late = add_run_trip(_p_plain, s39.closed_by_trip, MSK)
check(_p_late["verdict"]["title"] == "🟡 ЗАМЕЧАНИЯ К РАБОТЕ — 1" and _p_late["has_remarks"]
      and render_summary_md(_p_late) == render_summary_md(p39)
      and add_run_trip(_p_late, s39.closed_by_trip, MSK) is _p_late
      and add_run_trip({"no_data": True}, s39.closed_by_trip) == {"no_data": True},
      "2в: add_run_trip — та же строка, что у нарезки; повтор ничего не меняет")
_old3 = json.loads(json.dumps(p31))
_old3["version"] = 3
_old3.pop("fault_code")
check("Код 1448" not in render_summary_md(_old3) and "⏱ **Авария снята**" in render_summary_md(_old3),
      "2в: старые части без fault_code — строка «⏱» прежняя")

# ── 2г. Шапка входа заключения ──


def srow(seg, parts, **kw):
    return {"run_state": seg.run_state, "characteristics_json": seg.to_dict(), "report_md": "отчёт",
            "report_summary_json": parts, "t_end": datetime.fromisoformat(seg.t_end),
            "cause_close": seg.cause_close, **kw}


inp31 = build_claude_input(srow(s31, p31))
head31 = inp31.split("---")[0]
check("Авария снята в 09:26:43 (через 20 с)" in head31
      and "Код 1448 сброшен: сброс неисправностей 09:26:32 (40569), код снят 09:26:43" in head31
      and "на конец сегмента код 1448 не висит. Не называй его несброшенным" in head31
      and head31.count("Код 1448") == 1 and "Наработка двигателя на конец периода: 13 643 ч" in head31,
      f"2г: шапка #35931 — снятие по маске, одна строка о коде, наработка: {head31}")
_r61 = fcc(E61, D0, D1, 0)
_s61 = dc_replace(s31, fault_code_close=_r61, t_start=iso(D0), t_end=iso(D1))
_p61 = build_summary_parts([_s61], [], tz=MSK)
_f61 = conclusion_facts(srow(_s61, _p61))
check(any("сразу после сброса в регистре код 1248" in x and "не висит" in x for x in _f61)
      and not any("несброшен" in x.lower().replace("не называй его несброшенным", "") for x in _f61),
      f"2г: #35861 — сброшен, следом 1248 — следующий сегмент: {_f61}")
inp39 = build_claude_input(srow(s39, p39))
head39 = inp39.split("---")[0]
check("Сегмент закончился аварийным остановом панели (код 234 «Разнос двигателя — превышение частоты вращения» "
      "в 09:28:53)" in head39 and "не пиши «штатный», «все параметры в норме», «плановый контроль»" in head39
      and "Работа прервана аварийным остановом [CAUTION, панель, событие 09:28:53" in head39
      and "Уровень тревоги: CAUTION" in head39,
      f"2г: шапка #35939 — запрет «штатно», отметка панели: {head39}")
check(extract_verdict_alarm(srow(s39, p39)) == ("требует внимания", "CAUTION")
      and depth_for_segment("CAUTION") == "seg_caution",
      "2г: #35939 — вердикт CAUTION, разбор seg_caution вместо seg_norma")
# Поздняя отметка тоже доходит до шапки (из части сводки, не из characteristics)
_h_late = conclusion_facts(srow(dc_replace(s39, closed_by_trip=None), _p_late))
check(any("закончился аварийным остановом" in x for x in _h_late), f"2г: поздняя отметка — в шапке: {_h_late}")
_no_eh = {"run_state": 3, "characteristics_json": {"engine_hours_start": None, "subsegments": []},
          "report_summary_json": None}
# v5.1.22 (этап 3, P1-6): первой строкой шапки — «Время во входе — МСК.»
check(conclusion_facts(_no_eh) == ["Время во входе — МСК.", "Наработка двигателя: нет в данных — не указывай её; часы у событий журнала — "
                                   "длительность события, счётчик пусков — не наработка."],
      "2г: наработки нет — запрет выдумывать (часы событий, счётчик пусков)")
check(engine_hours_text(15296.95) == "15 297 ч", "2г: ДЭС №3 — 15 297 ч")
# Сегмент до v5.1.22 (#35872: 24 ч работы) — на конец по подсегментам, а не на начало
_old72 = {"run_state": 3, "report_summary_json": None, "characteristics_json": {
    "engine_hours_start": 15296.95, "subsegments": [
        {"characteristics": {"ENGINE_HOURS": {"value_end": 15313.05}}},
        {"characteristics": {"ENGINE_HOURS": {"value_end": 15314.95}}}, {"characteristics": {}}]}}
check(conclusion_facts(_old72)[1] == "Наработка двигателя на конец периода: 15 315 ч (счётчик панели).",
      f"2г: старый сегмент — наработка на конец по подсегментам: {conclusion_facts(_old72)}")
# Блок 1 агентского пути — те же строки
# agent.py импортирует anthropic — функцию берём из исходника (как routes._active_dets в 1к)
_atree = ast.parse((ROOT / "corpus" / "agent.py").read_text(encoding="utf-8"))
_afn = next(n for n in _atree.body if isinstance(n, ast.FunctionDef) and n.name == "_build_conclusion")
_ans: dict = {}
exec(compile(ast.Module(body=[_afn], type_ignores=[]), "agent._build_conclusion", "exec"), _ans)
_build_conclusion = _ans["_build_conclusion"]
_b1 = _build_conclusion(srow(s31, p31, t_end=datetime.fromisoformat(s31.t_end)), "авария", "SHUTDOWN", "", "m")
check("| Код 40012 | Код 1448 сброшен: сброс неисправностей 09:26:32, код снят 09:26:43" in _b1,
      f"2г: блок 1 (Claude API) — строка кода 40012: {_b1[:600]}")
_b1t = _build_conclusion(srow(s39, p39), "требует внимания", "CAUTION", "", "m")
check("| Конец сегмента | аварийный останов панели 09:28:53, код 234" in _b1t, "2г: блок 1 — конец сегмента")

# ── 2д. Наработка на начало и конец ──
check("ENGINE_HOURS" in eng._HELD_ROLES, "2д: счётчик наработки — в удержанных значениях посева")
check(s29.engine_hours_start == 13642.6 and s29.engine_hours_end == 13642.6,
      f"2д: строки в сегменте нет — последняя до начала в окне: {s29.engine_hours_start}")
_seed = {"held": {"ENGINE_HOURS": {"ts": W0 - timedelta(minutes=10), "value": EH_SEC}}}
_sseed = seg_run(ENUM_1610, FAULTS_1610, hist_runs(RUNS_1610), seed=_seed)
_s29b = by_start(_sseed, M(16, 9, 25, 52))
check(_s29b.engine_hours_start == 13642.6 and _s29b.engine_hours_end == 13642.6,
      f"2д: строк в окне нет — удержанное перед окном (секунды → часы): {_s29b.engine_hours_start}")
_snone = seg_run(ENUM_1610, FAULTS_1610, hist_runs(RUNS_1610))
check(all(s.engine_hours_start is None and s.engine_hours_end is None for s in _snone),
      "2д: ни строк, ни посева — None")
_eh_hist = hist_runs(RUNS_1610, eh=[(M(16, 9, 21), EH_SEC), (M(16, 9, 31), EH_SEC + 90),
                                     (M(16, 9, 38), EH_SEC + 180)])
_s_eh = by_start(seg_run(ENUM_1610, FAULTS_1610, _eh_hist), M(16, 9, 29, 33))
check(abs(_s_eh.engine_hours_end - 13642.65) < 1e-6 and abs(_s_eh.engine_hours_start - 13642.625) < 1e-6,
      f"2д: на начало — первая строка в сегменте (как раньше), на конец — последняя до конца: "
      f"{_s_eh.engine_hours_start} → {_s_eh.engine_hours_end}")
check(_engine_hours_at({}, KB_CFG, W0, None) is None, "2д: пусто — None")

# ── 2е. Поздняя отметка обрыва движком (аварийность стопа пришла после закрытия) ──


class TripDB:
    def __init__(self, parts):
        self.parts, self.updates = parts, []

    async def fetchval(self, sql, *a):
        return json.dumps(self.parts)

    async def execute(self, sql, *a):
        self.updates.append(a)
        self.parts = json.loads(a[1])

    async def close(self):
        return None


_tdb = TripDB(_p_plain)
_orig_connect = odb._connect


async def _tconnect():
    return _tdb
odb._connect = _tconnect
try:
    ch, lv = asyncio.run(odb.apply_late_run_trip(35939, s39.closed_by_trip, MSK))
    ch2, lv2 = asyncio.run(odb.apply_late_run_trip(35939, s39.closed_by_trip, MSK))
finally:
    odb._connect = _orig_connect
check(ch and lv and not ch2 and not lv2 and len(_tdb.updates) == 1
      and "Работа прервана аварийным остановом" in _tdb.updates[0][2],
      f"2е: apply_late_run_trip — одна запись, вердикт сменился, повтор ничего не меняет: {ch, lv, ch2}")

_late_calls: list = []
_enq: list = []


async def _late_db(seg_id, trip, tz=None):
    _late_calls.append((seg_id, trip))
    return True, True
_orig_late, _orig_enq = eng.online_db.apply_late_run_trip, eng._enqueue_segment
eng.online_db.apply_late_run_trip = _late_db
eng._enqueue_segment = lambda sid, again=False: _enq.append((sid, again))
try:
    e3 = eng.OnlinePollEngine.__new__(eng.OnlinePollEngine)
    e3.cfg, e3.tz, e3.router_sn, e3.equip_type, e3.panel_id = KB_CFG, MSK, "R", "pcc", 1
    # Работа закрылась, а стоп ещё простой: аварийность не пришла
    _seg39 = dc_replace(s39, closed_by_trip=None)
    _stop_simple = NS(run_state=0, stop_kind="SIMPLE", attempt=None, t_start=iso(M(16, 9, 28, 53)))
    e3._watch_trip([(_seg39, 35939, M(16, 9, 28, 33), M(16, 9, 28, 53))], [_seg39, _stop_simple])
    check(e3._trip_watch == {"id": 35939, "t_start": M(16, 9, 28, 33), "t_end": M(16, 9, 28, 53), "run_state": 3},
          f"2е: закрыта сменой режима в простой стоп — ждём аварийности: {e3._trip_watch}")
    # Следующий цикл: окно с 09:28:53, данные ещё без аварии — ждём дальше
    _e_win = [p for p in ENUM_1610 if p["state_end"] is None or p["state_end"] > M(16, 9, 28, 53)]
    _e_noshut = [dict(p, value=0) if p["addr"] in (40012, 40013) and p["state_start"] == M(16, 9, 28, 53) else p
                 for p in _e_win]
    asyncio.run(e3._late_run_trip([_stop_simple], _e_noshut, [], M(16, 9, 29, 10)))
    check(e3._trip_watch is not None and not _late_calls, "2е: аварийности ещё нет — ждём, без записи")
    _stop_em = NS(run_state=0, stop_kind="EMERGENCY", attempt=None, t_start=iso(M(16, 9, 28, 53)))
    asyncio.run(e3._late_run_trip([_stop_em], _e_win, [FAULTS_1610[1]], M(16, 9, 29, 20)))
    check(len(_late_calls) == 1 and _late_calls[0][0] == 35939
          and _late_calls[0][1]["codes"] == tr39["codes"] and _late_calls[0][1]["t"] == tr39["t"]
          and _enq == [(35939, True)] and e3._trip_watch is None,
          f"2е: стоп стал аварийным — отметка дописана той же подписью, заключение перезаказано: {_late_calls}")
    # Уже отмечен нарезкой / стоп аварийный сразу / стоп сменился — не ждём
    e3._watch_trip([(s39, 35939, M(16, 9, 28, 33), M(16, 9, 28, 53))], [s39, _stop_simple])
    _w1 = e3._trip_watch
    e3._watch_trip([(_seg39, 35939, M(16, 9, 28, 33), M(16, 9, 28, 53))], [_seg39, _stop_em])
    _w2 = e3._trip_watch
    e3._trip_watch = {"id": 1, "t_start": M(16, 9, 28, 33), "t_end": M(16, 9, 28, 53), "run_state": 3}
    asyncio.run(e3._late_run_trip([NS(run_state=3, stop_kind=None, attempt=None, t_start=iso(M(16, 9, 29, 33)))],
                                  _e_win, [], M(16, 9, 30)))
    check(_w1 is None and _w2 is None and e3._trip_watch is None and len(_late_calls) == 1,
          "2е: отмечен нарезкой, стоп уже аварийный, окно ушло — не ждём и не пишем")
finally:
    eng.online_db.apply_late_run_trip, eng._enqueue_segment = _orig_late, _orig_enq
_src_eng = (ROOT / "online" / "engine.py").read_text(encoding="utf-8")
check("await self._late_run_trip(segments, enum_periods, fault_periods, ts_to_utc)" in _src_eng
      and "self._watch_trip(_rs_closed_info, segments)" in _src_eng,
      "2е: путь смены режима — ожидание и поздняя отметка подключены")



# ══ 3. P1-4 акт и вход совета, P1-5 заключение аварийного стопа, P1-6 МСК, В4 язык ══

import logging  # noqa: E402

import corpus.db as cdb  # noqa: E402
import corpus.worker as cw  # noqa: E402
import llm.client as lclient  # noqa: E402
import llm.lang_check as lc  # noqa: E402
import online.incident_gate as ig  # noqa: E402
from analytics import classifier as clf  # noqa: E402
from analytics.labels import SYSTEM_MODEL, conclusion_by  # noqa: E402
from analytics.reconstructor import (  # noqa: E402
    build_analog_trace, fall_facts_ru, format_analog_trace_ru, trace_params,
)
from corpus.preprocessor import (  # noqa: E402
    _format_chronology, _format_gate_analyses, _format_incident,
)

# Отсчёты ДГУ №1 16.10 вокруг #35931 и #35941 (значения — как в БД, проверено
# чтением; P и I — нули, автомат разомкнут с 03:41:02)
_T_ROWS = [
    (40068, M(16, 9, 25, 12), 0.0), (40044, M(16, 9, 17, 3), 0.0), (40034, M(16, 9, 25, 23), 0.0),
    (40029, M(16, 9, 25, 23), 0.0), (40025, M(16, 9, 19, 23), 0.0), (40062, M(16, 9, 25, 12), 0.0),
    (40061, M(16, 9, 25, 32), 27.1),
    # #35929 «Работа» 09:25:52–09:26:23
    (40068, M(16, 9, 26, 3), 1696.0), (40044, M(16, 9, 26, 3), 56.8),
    (40034, M(16, 9, 26, 3) + timedelta(milliseconds=400), 0.0), (40029, M(16, 9, 26, 3), 0.0),
    (40025, M(16, 9, 26, 3), 397.0), (40062, M(16, 9, 26, 3), 624.0), (40061, M(16, 9, 26, 3), 25.1),
    (40068, M(16, 9, 26, 12), 1247.0), (40044, M(16, 9, 26, 12), 41.5), (40025, M(16, 9, 26, 12), 398.0),
    (40062, M(16, 9, 26, 12), 648.0), (40061, M(16, 9, 26, 12), 25.0),
    (40068, M(16, 9, 26, 23), 76.0), (40044, M(16, 9, 26, 23), 0.0), (40025, M(16, 9, 26, 23), 0.0),
    (40062, M(16, 9, 26, 23), 120.0), (40061, M(16, 9, 26, 23), 25.2),
    (40068, M(16, 9, 26, 32), 0.0), (40062, M(16, 9, 26, 32), 0.0), (40061, M(16, 9, 26, 32), 25.3),
    # две плашки 09:27:23 / 09:27:55 — без вращения выше прокрутки
    (40068, M(16, 9, 27, 30), 189.0), (40068, M(16, 9, 27, 45), 0.0),
    (40068, M(16, 9, 28, 0), 203.0), (40068, M(16, 9, 28, 20), 0.0),
    # #35939 «Работа» 09:28:33–09:28:53
    (40068, M(16, 9, 28, 30), 203.0), (40062, M(16, 9, 28, 30), 128.0), (40061, M(16, 9, 28, 30), 22.3),
    (40068, M(16, 9, 28, 42), 1294.0), (40044, M(16, 9, 28, 42), 42.6), (40025, M(16, 9, 28, 42), 432.0),
    (40062, M(16, 9, 28, 42), 652.0), (40061, M(16, 9, 28, 42), 24.8), (40034, M(16, 9, 28, 42), 0.0),
    (40029, M(16, 9, 28, 42), 0.0),
    (40068, M(16, 9, 28, 52), 898.0), (40062, M(16, 9, 28, 52), 560.0), (40061, M(16, 9, 28, 52), 25.0),
    (40044, M(16, 9, 28, 53), 0.0), (40025, M(16, 9, 28, 53), 55.0),
    (40068, M(16, 9, 29, 2), 260.0), (40025, M(16, 9, 29, 2), 0.0), (40062, M(16, 9, 29, 2), 203.0),
    (40068, M(16, 9, 29, 12), 0.0), (40062, M(16, 9, 29, 12), 0.0),
    # после пуска 09:29:33
    (40068, M(16, 9, 29, 40), 1500.0), (40044, M(16, 9, 29, 40), 50.0),
    (EH_ADDR, M(16, 9, 21), EH_SEC),
]
HIST_T = sorted(({"addr": a, "ts": t, "value": v} for a, t, v in _T_ROWS), key=lambda r: r["ts"])
CB_1610 = epers(43562, (0, M(16, 3, 41, 2)))
for _p in CB_1610:
    _p["label"] = "Разомкнут"
SEGS3 = seg_run(ENUM_1610 + CB_1610, FAULTS_1610, HIST_T)
t29, t31 = by_start(SEGS3, M(16, 9, 25, 52)), by_start(SEGS3, M(16, 9, 26, 23))
t39, t41 = by_start(SEGS3, M(16, 9, 28, 33)), by_start(SEGS3, M(16, 9, 28, 53))
check(all(x is not None for x in (t29, t31, t39, t41)) and t29.closed_by_trip and t39.closed_by_trip,
      f"3: нарезка серии 16.10 с отсчётами: {[(s.run_state, s.t_start[11:19], bool(s.closed_by_trip)) for s in SEGS3]}")

# ── 3а. Лента отсчётов: снимается нарезкой из истории окна (В2), без запросов ──
tr29 = (t29.closed_by_trip or {}).get("trace") or {}
tr39 = (t39.closed_by_trip or {}).get("trace") or {}
check(tr29.get("t_from") == iso(M(16, 9, 25, 52)) and tr29.get("stop") == iso(M(16, 9, 26, 23))
      and tr29.get("t_to") == iso(M(16, 9, 26, 53)) and tr29.get("max_rows") == 40
      and (tr29.get("segment") or {}).get("label") == "Работа",
      f"3а: окно ленты #35929 — от начала сегмента до останова + 30 с: {[tr29.get(k) for k in ('t_from', 't_to', 'segment')]}")
# v5.1.23: + задание частоты 40595 сразу за частотой (incident_trace: 2.5)
check([c["role"] for c in tr29.get("cols") or []] == ["RPM", "FREQUENCY", "FREQ_REFERENCE", "ACTIVE_POWER_TOTAL",
                                                     "CURRENT_AVG", "VOLTAGE_LL_AVG", "OIL_PRESS", "BATTERY_V"],
      f"3а: колонки — пометка incident_trace KB по порядку: {tr29.get('cols')}")
check(tr29.get("entry", {}).get("RPM") == {"v": 0.0, "t": iso(M(16, 9, 25, 12))}
      and tr29.get("entry", {}).get("FREQUENCY", {}).get("t") == iso(M(16, 9, 17, 3)),
      f"3а: значение на входе — последняя запись до начала ленты: {tr29.get('entry')}")
_r03 = next((r for r in tr29.get("rows") or [] if r["t"] == iso(M(16, 9, 26, 3))), None)
check(_r03 is not None and _r03["v"].get("ACTIVE_POWER_TOTAL") == 0.0 and _r03["v"]["RPM"] == 1696.0
      and len([r for r in tr29["rows"] if r["t"].startswith("2026-10-16T06:26:03")]) == 1,
      f"3а: записи одной секунды опроса — одна строка (40034 через 0,4 с): {_r03}")
check((tr29.get("states") or [{}])[0].get("periods") == [{"t": iso(M(16, 3, 41, 2)), "v": 0, "label": "Разомкнут"}],
      f"3а: положение автомата — период из enum, с настоящим началом: {tr29.get('states')}")
check("trace" not in (t31.to_dict().get("closed_by_trip") or {}) and t31.closed_by_trip is None
      and "trace" in (t29.to_dict().get("closed_by_trip") or {}),
      "3а: лента — в отметке обрыва сегмента падения (characteristics_json), у стопа её нет")
_tl29 = format_analog_trace_ru(tr29, MSK)
_tl29s = "\n".join(_tl29)
check(_tl29[0].startswith("Лента отсчётов сегмента падения (МСК; опрос ~")
      and "пики между опросами не видны): 09:25:52–09:26:53, останов 09:26:23" in _tl29[0]
      and "  Без изменений за ленту: P 0 кВт; I 0 А" in _tl29
      and "  Автомат (43562): Разомкнут с 16.10 03:41:02" in _tl29
      and _tl29s.count("останов ──") == 1 and "  ── 09:26:23 останов ──" in _tl29,
      f"3а: печать — шапка, «Без изменений», автомат, один разделитель останова:\n{_tl29s}")
_rows_txt = [ln for ln in _tl29 if ln.startswith("  09:")]
check(any(ln.startswith("  09:25:52") and ln.endswith("← на входе") for ln in _rows_txt)
      and any(ln.startswith("  09:26:32") and "0·" in ln for ln in _rows_txt)
      and _tl29.index("  ── 09:26:23 останов ──") < next(i for i, ln in enumerate(_tl29) if ln.startswith("  09:26:23")),
      f"3а: вход, «·» — значение держится, разделитель перед кадром останова: {_rows_txt}")
_tl39 = format_analog_trace_ru(tr39, MSK)
check(sum(1 for ln in _tl39 if "останов ──" in ln) == 1 and "  ── 09:28:53 останов ──" in _tl39,
      f"3а: #35941 — одна строка-разделитель (09:28:52 и 09:28:53 — разные кадры): {_tl39}")
# Удержанное перед окном (посев) — значение на входе, если в окне строки нет
_hl = build_analog_trace({}, [], KB_CFG, M(16, 9, 25, 52), M(16, 9, 26, 23), M(16, 9, 26, 53),
                         held={"RPM": (M(16, 9, 10), 0.0)})
check(_hl and _hl["entry"] == {"RPM": {"v": 0.0, "t": iso(M(16, 9, 10))}} and _hl["rows"] == [],
      f"3а: посев перед окном — вход ленты: {_hl}")
check(build_analog_trace({}, [], KB_CFG, M(16, 9, 25, 52), M(16, 9, 26, 23), M(16, 9, 26, 53)) is None
      and format_analog_trace_ru(None) == [] and fall_facts_ru(None) is None,
      "3а: истории нет — ленты нет (акт строится без неё)")
check(trace_params(KB_CFG) == {"before": 600, "after": 30, "max_rows": 40}
      and trace_params(NS(seg=lambda *a, default=None: default)) == {"before": 600, "after": 30, "max_rows": 40},
      "3а: лимиты KB stop_kind.act_trace_* и те же умолчания в коде")
check([r["addr"] for r in KB_CFG.incident_trace_registers]
      == [40068, 40044, 40595, 40034, 40029, 40025, 40062, 40061, 43562], "3а: пометка incident_trace в mapping.yaml")
_cfg_nt = binding.build_config(ROOT / "knowledge_base", controller_id="pcc3300", engine_id="cummins_kta50")
for _m in _cfg_nt._register_map.values():
    _m.pop("incident_trace", None)
check([r["addr"] for r in _cfg_nt.incident_trace_registers] == [r["addr"] for r in KB_CFG.incident_trace_registers],
      "3а: рабочая копия mapping.yaml без пометок — те же колонки по умолчанию из кода")
# Лимиты: «Работа» 20 мин с записью оборотов раз в 10 с — окно 600 с, печать 40 строк, ранние свёрнуты
_long = [{"addr": 40068, "ts": M(16, 9, 0) + timedelta(seconds=10 * k), "value": 1500.0 + (k % 3)} for k in range(121)]
_lt = build_analog_trace({40068: _long}, [], KB_CFG, M(16, 9, 10), M(16, 9, 20), M(16, 9, 20, 30))
_ltp = format_analog_trace_ru(_lt, MSK)
_body = [ln for ln in _ltp if ln.startswith("  09:")]
check(_lt["entry"]["RPM"]["t"] == iso(M(16, 9, 9, 50)) and len(_lt["rows"]) == 61
      and any("(свёрнуто строк: 22)" in ln and "об/мин 1500…1502" in ln for ln in _ltp)
      and len([ln for ln in _body if "свёрнуто" not in ln and "← на входе" not in ln]) == 39,
      f"3а: свёртка ранних строк сверх 40: {len(_lt['rows'])} строк, печать {len(_body)}")
# Плашка неудачной попытки с аварией — сама сегмент падения: лента в Segment.fall_trace
_e_pl3 = (epers(40011, (0, W0 - timedelta(hours=1)), (2, M(16, 9, 25)), (0, M(16, 9, 25, 20)))
          + epers(40012, (0, W0 - timedelta(days=1)), (1438, M(16, 9, 25, 20)), (0, M(16, 9, 26)))
          + epers(40013, (0, W0 - timedelta(days=1)), (4, M(16, 9, 25, 20)), (0, M(16, 9, 26))))
_h_pl = [{"addr": 40068, "ts": M(16, 9, 24) + timedelta(seconds=10 * k), "value": 0.0} for k in range(12)]
_spl = seg_run(_e_pl3, [], _h_pl)
_plate = next((s for s in _spl if s.attempt), None)
check(_plate is not None and _plate.attempt.get("outcome") == "EMERGENCY" and _plate.fall_trace
      and _plate.fall_trace["t_from"] == iso(M(16, 9, 25)) and _plate.fall_trace["stop"] == iso(M(16, 9, 25, 20))
      and "fall_trace" not in _plate.to_dict()
      and "Без изменений за ленту: обороты 0 об/мин" in "\n".join(format_analog_trace_ru(_plate.fall_trace, MSK)),
      f"3а: плашка с аварией — лента попытки («обороты 0»: стартер не провернул), в характеристики не пишется: "
      f"{_plate and _plate.fall_trace}")

# ── 3б. Факты до останова (для заключения): удержанное — «держится с» ──
ff29 = fall_facts_ru(tr29, MSK)
check(ff29 == "До останова — Работа 09:25:52–09:26:23: нагрузка 0 кВт и ток 0 А весь интервал ленты; "
              "автомат разомкнут с 16.10 03:41:02; обороты и частота по отсчётам: 09:26:03 1696 об/мин / "
              "56,8 Гц → 09:26:12 1247 об/мин / 41,5 Гц → 09:26:23 (кадр останова) 76 об/мин / 0 Гц.",
      f"3б: #35931 — факты ленты: {ff29}")
ff39 = fall_facts_ru(tr39, MSK)
check(ff39 is not None and "09:28:52 898 об/мин (частота 42,6 Гц держится с 09:28:42)" in ff39
      and "09:28:53 (кадр останова) 0 Гц (обороты 898 об/мин держатся с 09:28:52)" in ff39
      and "09:28:52 898 об/мин / 42,6" not in ff39,
      f"3б: #35941 — удержанная частота не выдаётся за замер: {ff39}")
_tr_load = json.loads(json.dumps(tr29))
_tr_load["rows"][0]["v"]["ACTIVE_POWER_TOTAL"] = 512.0
_tr_load["states"][0]["periods"].append({"t": iso(M(16, 9, 26, 10)), "v": 1, "label": "Замкнут"})
_ffl = fall_facts_ru(_tr_load, MSK)
check("нагрузка до останова 512 кВт (запись 09:26:03); ток 0 А" in _ffl
      and "автомат: разомкнут → замкнут в 09:26:10" in _ffl, f"3б: нагрузка менялась, автомат переключался: {_ffl}")

# ── 3в. Акт: общий построитель на всех трёх путях, лента и сегмент падения ──
_orig_lic = eng._load_incident_context


async def _lic_none(*a, **k):
    return None
eng._load_incident_context = _lic_none
_logged: list[str] = []


class _LH(logging.Handler):
    def emit(self, rec):
        _logged.append(rec.getMessage())


eng.logger.addHandler(_LH())
_ba = dict(tl_enum=ENUM_1610 + CB_1610, fault_periods=FAULTS_1610, tl_faults=FAULTS_1610, skip_stops=set())
try:
    # _run_segment (суточный рез, догонка): сегмент падения — в окне
    act31 = asyncio.run(eng._build_act("R", "pcc", 1, t31, datetime.fromisoformat(t31.t_end), W1, KB_CFG,
                                       segments=SEGS3, key="t", **_ba))
    # открытый стоп по лагу: окно начинается со стопа, сегмент падения — подсказка предка
    act31o = asyncio.run(eng._build_act("R", "pcc", 1, t31, None, W1, KB_CFG, segments=[t31], prev=t29,
                                        key="t", **_ba))
    # после рестарта: подсказка из строки БД
    _hint = eng._make_seg_hint({"run_state": 3, "t_start": M(16, 9, 25, 52), "t_end": M(16, 9, 26, 23),
                                "characteristics_json": json.dumps(t29.to_dict())})
    act31r = asyncio.run(eng._build_act("R", "pcc", 1, t31, None, W1, KB_CFG, segments=[t31], prev=_hint,
                                        key="t", **_ba))
    # закрытие сменой режима: закрываемые сегменты окна
    act41 = asyncio.run(eng._build_act("R", "pcc", 1, t41, datetime.fromisoformat(t41.t_end), W1, KB_CFG,
                                       segments=[t39, t41], prev=None, key="t", **_ba))
    # отметки обрыва нет (аварийность пришла позже) — предупреждение, акт без ленты
    _logged.clear()
    act_nt = asyncio.run(eng._build_act("R", "pcc", 1, t31, None, W1, KB_CFG, segments=[t31],
                                        prev=dc_replace(t29, closed_by_trip=None), key="t", **_ba))
    _warn_nt = list(_logged)
    act_pl = asyncio.run(eng._build_act("R", "pcc", 1, _plate, None, M(16, 9, 30), KB_CFG,
                                        tl_enum=_e_pl3, fault_periods=[], tl_faults=[], skip_stops=set(),
                                        segments=_spl, key="t"))
finally:
    eng._load_incident_context = _orig_lic
check(act31 and act31["trace"] == tr29 and act31["investigator_version"] == "1.1"
      and act31["fall"] == {"run_state": 3, "label": "Работа", "t_start": iso(M(16, 9, 25, 52)),
                            "t_end": iso(M(16, 9, 26, 23)), "chain_start": iso(M(16, 9, 25, 52))},
      f"3в: акт (_run_segment) — лента и сегмент падения: {act31 and act31.get('fall')}")
check(act31o and act31o.get("trace") == tr29 and act31r and act31r.get("trace") == tr29
      and _hint.closed_by_trip and _hint.t_end == iso(M(16, 9, 26, 23)),
      "3в: открытый стоп по лагу и после рестарта (подсказка из БД) — та же лента")
check(act41 and act41.get("trace") == tr39 and act41["fall"]["chain_start"] == iso(M(16, 9, 28, 33)),
      f"3в: закрытие сменой режима — лента #35939, пуск 09:28:33: {act41 and act41.get('fall')}")
check(act_nt and "trace" not in act_nt and act_nt.get("fall", {}).get("label") == "Работа"
      and any("нет отметки обрыва аварией" in x for x in _warn_nt),
      f"3в: у сегмента падения нет отметки — акт без ленты, предупреждение в журнал: {_warn_nt}")
check(act_pl and act_pl.get("trace") == _plate.fall_trace and act_pl["fall"].get("attempt") is True
      and act_pl["failure"] == "start", f"3в: плашка — лента попытки: {act_pl and act_pl.get('fall')}")
_old_act = clf.build_stop_incident(ENUM_1610, FAULTS_1610, M(16, 9, 26, 23), M(16, 9, 26, 43), KB_CFG,
                                   stop_kind="EMERGENCY")
check(_old_act and "trace" not in _old_act and "fall" not in _old_act,
      "3в: без ленты и сведений — ключей в акте нет (как у актов 1.0)")
_eng_src = (ROOT / "online" / "engine.py").read_text(encoding="utf-8")
check(_eng_src.count("await _build_act(") == 3 and _eng_src.count("build_stop_incident(") == 1
      and "prev_hint=self._prev_seg_hint" in _eng_src,
      "3в: три пути (суточный рез/догонка, смена режима, открытый стоп) — один построитель")

# ── 3г. Вход совета: ход цикла по сегментам, МСК, лента ──


def _row(sg_or_dict, **kw):
    if isinstance(sg_or_dict, dict):
        d = dict(sg_or_dict)
    else:
        d = {"run_state": sg_or_dict.run_state, "t_start": datetime.fromisoformat(sg_or_dict.t_start),
             "t_end": datetime.fromisoformat(sg_or_dict.t_end), "characteristics_json": sg_or_dict.to_dict()}
    d.update(kw)
    return d


def _chars(**roles):
    return {"subsegments": [{"characteristics": {r: dict(v) for r, v in roles.items()}}]}


_W15 = {"run_state": 3, "t_start": M(15, 13, 28, 53), "t_end": M(16, 3, 41, 3), "characteristics_json": {
    "cause_close": "RUN_STATE_CHANGE", **_chars(
        ACTIVE_POWER_TOTAL={"min": 0, "max": 713, "value_end": 0, "unit": "kW"},
        CURRENT_AVG={"min": 0, "max": 1324, "value_end": 0, "unit": "A"},
        RPM={"min": 1496, "max": 1504, "value_end": 1500, "unit": "rpm"},
        HEARTBEAT={"min": 1, "max": 2, "value_end": 2, "unit": ""},
        RATING_KW={"min": 1000, "max": 1000, "value_end": 1000, "unit": "kW"})}}
_UNL = {"run_state": 4, "t_start": M(16, 3, 41, 3), "t_end": M(16, 3, 44, 3),
        "characteristics_json": _chars(ACTIVE_POWER_TOTAL={"min": 0, "max": 0, "value_end": 0, "unit": "kW"})}
_CDN = {"run_state": 5, "t_start": M(16, 3, 44, 3), "t_end": M(16, 3, 45, 32), "characteristics_json": {}}
_ST1 = {"run_state": 0, "t_start": M(16, 3, 45, 32), "t_end": M(16, 9, 0),
        "characteristics_json": {"stop_kind": "SIMPLE", "cause_close": "DAILY_BOUNDARY", **_chars(
            COOLANT_TEMP={"min": 20, "max": 80, "value_end": 20, "unit": "°C"})}}
_ST2 = {"run_state": 0, "t_start": M(16, 9, 0), "t_end": M(16, 9, 25, 52),
        "characteristics_json": {"stop_kind": "SIMPLE", "cause_close": "RUN_STATE_CHANGE"}}
CYC31 = [_W15, _UNL, _CDN, _ST1, _ST2, _row(t29)]
INC31 = clf.build_stop_incident(ENUM_1610 + CB_1610, FAULTS_1610, M(16, 9, 26, 23), M(16, 9, 26, 43), KB_CFG,
                                stop_kind="EMERGENCY", anchor=(M(16, 3, 45, 32), "normal_stop"),
                                min_window_sec=12 * 3600, trace=tr29, fall=act31["fall"])
pr31 = ig.build_incident_prompt(INC31, None, CYC31, {"code_name": lambda c: {1448: "Пониженная частота"}.get(c)},
                                tz=MSK)
_cyc = pr31.split("Ход цикла", 1)[-1].split("\n\n", 1)[0].splitlines()
_sep_i = next((i for i, ln in enumerate(_cyc) if "последний штатный останов" in ln), None)
check(_sep_i is not None and _cyc[_sep_i] == "  ── 03:45:32 последний штатный останов; дальше — возможные предвестники ──"
      and any("713 кВт" in ln for ln in _cyc[:_sep_i]) and not any("713" in ln for ln in _cyc[_sep_i:]),
      f"3г: 713 кВт работы до штатного останова — до разделителя: {_cyc}")
check("Параметры за цикл" not in pr31 and any(ln.startswith("  - Работа: 09:25:52 — 09:26:23 (31 с): P 0 кВт; I 0 А;")
                                               for ln in _cyc),
      f"3г: склейки по режимам нет, у сегмента падения P 0 кВт своей строкой: {_cyc}")
check(any(ln == "  - Стоп: 03:45:32 — 09:25:52 (5 ч 40 мин), 2 части через суточный рез" for ln in _cyc)
      and any(ln.startswith("  - Работа: 15.10 13:28:53 — 16.10 03:41:03 (14 ч 12 мин): P 0…713 кВт") for ln in _cyc),
      f"3г: части стопа через суточный рез — одной строкой, давние сутки — с датой: {_cyc}")
check("Время во входе — МСК." in pr31 and "+00:00" not in pr31 and "UTC" not in pr31
      and "Останов: 16.10 09:26:23" in pr31 and "Последний штатный останов: 16.10 03:45:32" in pr31
      and "Лента вокруг останова (МСК):" in pr31 and "Время в ответе указывай по МСК" in pr31,
      "3г: время во входе — МСК, без «+00:00» и «UTC»")
check("HEARTBEAT" not in pr31 and "RATING_KW" not in pr31, "3г: пульс и номиналы — не параметры")
_i_fall = pr31.find("Сегмент, из которого машина остановилась — Работа")
_i_tl = pr31.find("Лента отсчётов сегмента падения")
_i_bg = pr31.find("Последняя работа до штатного останова (фон) — Работа, 15.10 13:28:53")
check(0 < _i_fall < _i_tl < _i_bg and "ACTIVE_POWER_TOTAL: 0 / 713 / 0 kW" in pr31[_i_bg:],
      "3г: подробно — сегмент падения, сразу лента отсчётов, затем фон (последняя работа до штатного останова)")
# Серия: #35941 — у #35929 и #35939 свои строки, предыдущая авария 1448 фактом
_r31 = _row(t31)
CYC41 = CYC31 + [_r31, _row(t39)]
INC41 = clf.build_stop_incident(ENUM_1610 + CB_1610, FAULTS_1610, M(16, 9, 28, 53), M(16, 9, 29, 22), KB_CFG,
                                stop_kind="EMERGENCY", anchor=(M(16, 3, 45, 32), "normal_stop"),
                                min_window_sec=12 * 3600, trace=tr39, fall=act41["fall"])
pr41 = ig.build_incident_prompt(INC41, None, CYC41, {"code_name": lambda c: {1448: "Пониженная частота"}.get(c)},
                                tz=MSK)
_cyc41 = pr41.split("Ход цикла", 1)[-1].split("\n\n", 1)[0].splitlines()
check(sum(1 for ln in _cyc41 if ln.startswith("  - Работа: 09:2")) == 2
      and any(ln.startswith("  - Стоп (аварийный): 09:26:23 — 09:26:43 (20 с)") for ln in _cyc41)
      and "  - 16.10 09:26:23 — аварийный стоп (падение с работы); код: 1448 «Пониженная частота»; "
          "сброшена через 20 с" in pr41,
      f"3г: #35941 — свои строки #35929/#35939, серия фактами: {_cyc41}")
# Своя плашка исключена, прошлые — своими строками; лимит 30 строк
_many = [{"run_state": 3 if k % 2 else 0, "t_start": M(16, 1, 0) + timedelta(minutes=5 * k),
          "t_end": M(16, 1, 0) + timedelta(minutes=5 * k + 5), "characteristics_json": {}} for k in range(40)]
_pm = ig.build_incident_prompt({"stop_ts": iso(M(16, 9, 0)), "window": {}}, None, _many, tz=MSK)
_cm = _pm.split("Ход цикла", 1)[-1].split("\n\n", 1)[0].splitlines()[1:]
check(len(_cm) == 30 and _cm[0].startswith("  - … ранее 11 строк: 01:00:00 — 01:55:00"),
      f"3г: не больше 30 строк хода цикла, ранние свёрнуты: {len(_cm)} {_cm[:2]}")
_own = dict(_row(_plate), t_start=datetime.fromisoformat(_plate.t_start))
_pp = ig.build_incident_prompt(act_pl, None, [_own], tz=MSK)
# v5.1.23 (В4): подпись плашки — «Неудачный пуск · Авария» (была «Неудачный прогрев · …»)
check("(эта попытка)" in _pp and not any(ln.startswith(("  - Неудачный пуск · Авария:",
                                                        "  - Неудачный прогрев · Авария:"))
                                         for ln in _pp.splitlines()),
      "3г: своя плашка в ход цикла не входит — только строкой «эта попытка»")
# Старый акт (без ленты) — вход строится, ленты нет
_p_old = ig.build_incident_prompt(_old_act, None, CYC31, tz=MSK)
check("Лента отсчётов" not in _p_old and "Ход цикла" in _p_old, "3г: акт до v5.1.22 — вход без ленты")

# Промпт совета: правила v5.1.22 и перенос сохранённой копии
_inc_p = router.get_default_prompt("incident")
_old_p = _inc_p.replace("\n" + router.INCIDENT_RULES_V5122, "")
check(router.INCIDENT_RULES_V5122 in _inc_p and router.migrate_incident_prompt(_old_p) == _inc_p
      and router.migrate_incident_prompt(_inc_p) is None and router.migrate_incident_prompt("свой промпт") is None
      and router.migrate_incident_prompt("") is None,
      "3г: промпт совета — МСК и лента; перенос сохранённой копии один раз, свой текст не трогается")

# ── 3д. Заключение аварийного стопа — фактами без ИИ (В1) ──


def _srow(seg, inc, parts=None, sid=35931, **kw):
    return {"id": sid, "router_sn": "SN", "equip_type": "pcc", "panel_id": 1, "run_state": seg.run_state,
            "t_start": datetime.fromisoformat(seg.t_start), "t_end": datetime.fromisoformat(seg.t_end),
            "characteristics_json": seg.to_dict(), "incident_json": inc, "gate_log": [],
            "report_summary_json": parts if parts is not None else build_summary_parts([seg], [], tz=MSK),
            "cause_close": seg.cause_close, **kw}


SR31 = _srow(t31, INC31)
sc31 = cw._shutdown_conclusion(SR31, CYC31[:-1], tz=MSK)
_md = (sc31 or {}).get("conclusion_md") or ""
check(sc31 and sc31["claude_model"] == SYSTEM_MODEL and conclusion_by(sc31["claude_model"]) == "system"
      and sc31["alarm_level"] == "SHUTDOWN" and sc31["debug_json"]["entry"] == "shutdown_facts",
      f"3д: поля записи — как у плашек: {sc31 and {k: sc31[k] for k in ('claude_model', 'alarm_level', 'debug_json')}}")
for _frag in ("Останов: 16.10 09:26:23 МСК, код панели 1448 «Пониженная частота» (останов).",
              "Из какого режима: «Работа» 09:25:52–09:26:23, через 31 с после пуска (выход из стопа в 09:25:52).",
              "Авария снята в 09:26:43 (через 20 с)",
              "Код 1448 сброшен: сброс неисправностей 09:26:32 (40569), код снят 09:26:43",
              "нагрузка 0 кВт и ток 0 А весь интервал ленты; автомат разомкнут с 16.10 03:41:02",
              "09:26:03 1696 об/мин / 56,8 Гц → 09:26:12 1247 об/мин / 41,5 Гц",
              "Предыдущих аварий после штатного останова нет.",
              "Наработка двигателя на конец периода: 13\xa0643 ч (счётчик панели).",
              "Справочник кода", "Причину разбирает совет ИИ по акту аварии"):
    check(_frag in _md, f"3д: #35931 — в заключении «{_frag}»:\n{_md}")
check("40599" not in _md and "EmergencyStop" not in _md and "UTC" not in _md and "冷" not in _md,
      "3д: без голоса 40599 и без UTC")
check((sc31 or {}).get("humanized_md", "").startswith("Аварийный останов 16.10 09:26:23 МСК: код панели 1448"),
      f"3д: краткая сводка вместо пересказа: {sc31 and sc31['humanized_md']}")
SR41 = _srow(t41, INC41, sid=35941)
sc41 = cw._shutdown_conclusion(SR41, CYC41, tz=MSK)
check(sc41 and "через 20 с после пуска" in sc41["conclusion_md"]
      and "  - 16.10 09:26:23 — аварийный стоп (падение с работы); код: 1448 «Пониженная частота»; "
          "сброшена через 20 с" in sc41["conclusion_md"]
      and "(частота 42,6 Гц держится с 09:28:42)" in sc41["conclusion_md"],
      f"3д: #35941 — пуск 09:28:33, предыдущая авария серии фактом: {sc41 and sc41['conclusion_md']}")
check(cw._shutdown_conclusion(dict(SR31, incident_json=None)) is None, "3д: акта нет — заключение пишет ИИ")
_old_sr = _srow(t31, _old_act)
_old_sc = cw._shutdown_conclusion(_old_sr, None, tz=MSK)
check(_old_sc and "ленты отсчётов в акте нет" in _old_sc["conclusion_md"]
      and "Предыдущих аварий" not in _old_sc["conclusion_md"],
      "3д: старый акт без ленты — факты без строки «До останова», серия неизвестна — без строки")

# Когда факты, а когда ИИ: строка таблицы, вид разбора, акт, отметка совета, ручной запуск
_FAIL_LOG = [{"kind": "incident", "fault_hash": ig.incident_hash(M(16, 9, 26, 23)), "decision": "failed"}]
_orig_cdb = {n: getattr(cdb, n) for n in ("set_status", "get_segment_row", "upsert_analysis",
                                          "get_equipment_kb_path")}
_orig_chain, _orig_gsb = cw._analyse_segment_chain, odb.get_segments_between
import db.analytics as dban  # noqa: E402
_orig_gas = dban.get_app_setting
W: dict = {}


async def _w_row(sid):
    return W["row"]


async def _w_up(sid, data):
    W["up"].append(data)


async def _w_none(*a, **k):
    return None


async def _w_chain(seg_row, kb, prompt, entries, depth=None):
    W["chain"].append((depth, [e["id"] for e in entries]))
    return {"verdict": "авария", "alarm_level": "SHUTDOWN", "conclusion_md": "ИИ", "error": None,
            "debug_json": {}, "claude_model": "ИИ"}


async def _w_segs(*a, **k):
    W["segs"] += 1
    return CYC31[:-1]


async def _w_setting(key, default=None):
    return "false"
cdb.set_status, cdb.get_segment_row, cdb.upsert_analysis, cdb.get_equipment_kb_path = (
    _w_none, _w_row, _w_up, _w_none)
cw._analyse_segment_chain, odb.get_segments_between, dban.get_app_setting = _w_chain, _w_segs, _w_setting


def _run_ps(row, force=False, provider="system"):
    W.update({"row": row, "up": [], "chain": [], "segs": 0})
    router.apply_depth("seg_shutdown", "fast", "default", "normal", provider=provider)
    asyncio.run(cw._process_segment(35931, force=force, task_id="seg_manual" if force else "seg_auto"))
    return W["up"][-1] if W["up"] else None, list(W["chain"])


try:
    _u1, _c1 = _run_ps(SR31)
    _u2, _c2 = _run_ps(SR31, force=True)
    _u3, _c3 = _run_ps(SR31, provider="llm")
    _u4, _c4 = _run_ps(dict(SR31, incident_json=None))
    _u5, _c5 = _run_ps(dict(SR31, gate_log=json.dumps(_FAIL_LOG)))
    _u6, _c6 = _run_ps(_srow(t29, None, sid=35929))
finally:
    for n, f in _orig_cdb.items():
        setattr(cdb, n, f)
    cw._analyse_segment_chain, odb.get_segments_between, dban.get_app_setting = _orig_chain, _orig_gsb, _orig_gas
    router.apply_depth("seg_shutdown", "fast", "default", "normal", provider="llm")
check(_u1 and _u1["claude_model"] == SYSTEM_MODEL and _u1["status"] == "done" and not _c1 and W is not None,
      f"3д: «Система (без ИИ)» и акт есть — факты, модель не зовётся: {_c1}")
check(_c2 == [("manual", _c2[0][1])] if _c2 else False, f"3д: кнопка «Анализ» — ИИ: {_c2}")
check(_c3 and _c3[0][0] == "seg_shutdown" and _c4 and _c5,
      f"3д: строка таблицы — ИИ / акта нет / совет не получен (failed) — пишет ИИ: {_c3}, {_c4}, {_c5}")
check(_c6 and _c6[0][0] == "seg_caution", "3д: «Работа», оборванная аварией (CAUTION), — заключение ИИ как раньше")
check(_c4 and _c4[0][1][0] == "route:llm", f"3д: без акта при строке «Система» — явно шлюз строки: {_c4}")

# Таблица: «Система (без ИИ)» только у строки аварийного стопа
router.apply_depth("gate_caution", "fast", "low", "interactive", provider="system")
router.apply_depth("seg_shutdown", "fast", "default", "normal", provider="system")
check(router.get_route("gate_caution")["provider"] == "llm" and router.get_route("seg_shutdown")["provider"] == "system"
      and router.route_label("seg_shutdown") == SYSTEM_MODEL,
      "3д: «system» в строке гейта отклонён (иначе ушёл бы в Claude API), у seg_shutdown — принят")
router.apply_route_form({"route_incident_provider": "system", "route_seg_shutdown_provider": "system"})
check(router.get_route("incident")["provider"] == "llm" and router.get_route("seg_shutdown")["provider"] == "system",
      "3д: форма настроек — «system» только у строки аварийного стопа")
_mj, _mn = router.migrate_shutdown_route(json.dumps({"seg_shutdown": {"model": "smart", "reasoning": "high",
                                                                      "priority": "normal", "provider": "llm"}}))
_mj2, _mn2 = router.migrate_shutdown_route(_mj)
router.load_depth(_mj)
check(json.loads(_mj)["seg_shutdown"] == {"model": "smart", "reasoning": "high", "priority": "normal",
                                          "provider": "system"}
      and _mj2 is None and _mn2.startswith("уже") and router.migrate_shutdown_route("{битый")[0] is None
      and router.get_route("seg_shutdown")["provider"] == "system" and router.get_route("seg_shutdown")["model"] == "smart",
      f"3д: разовая установка строки по ключу — модель строки сохраняется, повтор ничего не меняет: {_mn}")
router.load_depth("")
_main_src = (ROOT / "main.py").read_text(encoding="utf-8")
check('"ai_shutdown_route_version"' in _main_src and "_mig_shutdown(" in _main_src
      and _main_src.index("_mig_shutdown(") < _main_src.index("_load_depth(await get_app_setting(_DEPTH_KEY"),
      "3д: main.py — установка до загрузки таблицы, ключ ai_shutdown_route_version")
_tpl = (ROOT / "web" / "templates" / "settings.html").read_text(encoding="utf-8")
check("{% if d.kind == 'seg_shutdown' %}" in _tpl and 'value="system"' in _tpl,
      "3д: страница настроек — пункт «Система (без ИИ)» только у строки аварийного стопа")
# Совет не получен — заключение перезаказано у ИИ (только при строке «Система»)
_enq: list = []
_orig_eq, _orig_rs, _orig_gbi = eng._enqueue_segment, odb.resolve_segment_id, odb.get_segment_by_id
_SEG_ROWS = {35931: {"id": 35931, "t_end": M(16, 9, 26, 43)}, 35999: {"id": 35999, "t_end": None}}


async def _rs35931(r, e, p, sid, ts):
    return sid if sid in _SEG_ROWS else 35931


async def _gbi(sid):
    return _SEG_ROWS.get(sid)
eng._enqueue_segment = lambda sid, again=False: _enq.append((sid, again))
odb.resolve_segment_id, odb.get_segment_by_id = _rs35931, _gbi
try:
    router.apply_depth("seg_shutdown", "fast", "default", "normal", provider="system")
    asyncio.run(ig._reorder_conclusion("SN", "pcc", 1, M(16, 9, 26, 23), None))
    asyncio.run(ig._reorder_conclusion("SN", "pcc", 1, M(16, 9, 26, 23), 35999))   # стоп ещё открыт
    router.apply_depth("seg_shutdown", "fast", "default", "normal", provider="llm")
    asyncio.run(ig._reorder_conclusion("SN", "pcc", 1, M(16, 9, 26, 23), 35931))
finally:
    eng._enqueue_segment, odb.resolve_segment_id, odb.get_segment_by_id = _orig_eq, _orig_rs, _orig_gbi
check(_enq == [(35931, True)],
      f"3д: совет failed — перезаказ закрытого стопа (again); открытый стоп и строка ИИ — нет: {_enq}")
check(cw._council_failed(dict(SR31, gate_log=_FAIL_LOG), INC31)
      and not cw._council_failed(dict(SR31, gate_log=[dict(_FAIL_LOG[0], decision="ordered")]), INC31)
      and not cw._council_failed(dict(SR31, gate_log=[dict(_FAIL_LOG[0], fault_hash="incident:x")]), INC31),
      "3д: «совет не получен» — терминальная отметка этой аварии")

# ── 3е. Время МСК: плашки, входы заключения и гейта (P1-6) ──
_fc35 = cw._attempt_conclusion({"characteristics_json": {"attempt": {
    "from_state": 3, "outcome": "SIMPLE", "t_start": "2026-10-16T06:27:23+00:00",
    "t_fall": "2026-10-16T06:27:43+00:00", "codes": [], "facts": {"rpm_max": 189.0}}, "subsegments": []}})
check(_fc35 and "Останов 09:27:43 МСК" in _fc35["humanized_md"] and "+00:00" not in _fc35["conclusion_md"]
      and " МСК*" in _fc35["conclusion_md"] and "UTC" not in _fc35["conclusion_md"],
      f"3е: плашка #35935 — «Останов 09:27:43 МСК»: {_fc35 and _fc35['humanized_md']}")
_fi = _format_incident(INC31)
check("**Лента действий (время МСК;" in _fi and "\n09:26:23  " in _fi and "06:26:23" not in _fi,
      "3е: акт во входе заключения — МСК")
_fch = _format_chronology({"chronology": [{"ts": iso(M(16, 9, 26, 32)), "kind": "state", "addr": 40569,
                                           "name": "Сброс", "label": "Активно"}]})
check("Время — МСК." in _fch and "09:26:32  состояние 40569" in _fch, f"3е: хронология стоянки — МСК: {_fch}")
_fga = _format_gate_analyses({"warning_analyses": [{"t": "2026-10-13T12:03:25+00:00", "md": "разбор",
                                                    "alarm_text": "Общая авария"}]})
check("### 13.10 15:03:25 МСК — Общая авария" in _fga, f"3е: заголовок разбора гейта — МСК (#35872): {_fga}")
_wp = sa.build_warning_prompt(dict(_STRUCT), prev_analyses=[{"t": "2026-10-14T06:01:59+00:00", "md": "m",
                                                             "alarm_text": "a"}],
                              episode_timeline=[{"t_open": M(16, 9, 58, 12), "t_close": M(16, 9, 58, 52),
                                                 "scenario": "RPM_UNDERSPEED", "severity": "CAUTION",
                                                 "active_sec": 40}])
check("--- Разбор 14.10 09:01 МСК — a ---" in _wp and "09:58:12 → 09:58:52" in _wp
      and "Хронология тревог сегмента (время МСК):" in _wp and "UTC" not in _wp,
      "3е: вход гейта — прошлые разборы и хронология тревог по МСК")
_pl = sa._panel_line({"panel_severity": "норма", "panel_alarms": [
    {"severity": "CAUTION", "fault_codes": [1453], "since": "2026-10-13T12:01:38+00:00", "fresh": True}]})
check("несброшенный код 1453, с 13.10 15:01 МСК" in _pl, f"3е: «Факты (система, не ИИ)» — МСК: {_pl}")
_h31 = conclusion_facts(SR31)
check(_h31[0] == "Время во входе — МСК." and any(x.startswith("До останова — Работа 09:25:52–09:26:23") for x in _h31),
      f"3е: шапка заключения ИИ — МСК первой строкой, «До останова» из ленты (п. 5): {_h31}")

# ── 3ж. Чужой язык (В4): один проход «очистка / перевод», без повторов и Claude API ──
_SRC = "Режим fault simulation. Панель: Hot Stop, Fault Log."
_BAD = ("## Сводка\nДГУ прошла цикл: стоп →冷却 → перезапуск в 09:25:52. Код 1448 remains active.\n"
        "Режим fault simulation и Hot Stop, Shutdown with Cooldown, fail-open, `raw text here`.\n"
        "## Разбор\nЧастота 41,5 Гц.")
check(lc.foreign_fragments(_BAD, _SRC) == ["冷却", "remains active"]
      and lc.foreign_fragments("Цитата: the governor fails to respond [W2]", "", allow_ref_quotes=True) == []
      and lc.foreign_fragments("Цитата: the governor fails to respond [W2]", "") == ["the governor fails to respond"]
      and lc.foreign_fragments("Всё по-русски, Hot Stop и RUN_STATE=3.", "") == [],
      f"3ж: иероглифы и английские фразы; цитаты входа, термины с заглавной, код, ссылки [Wn] — нет: "
      f"{lc.foreign_fragments(_BAD, _SRC)}")
_GOOD = _BAD.replace("冷却", "охлаждение").replace("remains active", "остаётся активным")
LCALLS: list = []
_orig_ask = lc._ask_cleanup


def _fake_cleanup(answer):
    async def _f(text, priority, meta):
        LCALLS.append((text, priority))
        meta["gorynych"] = {"backend": "fast", "reasoning_level": None}
        if isinstance(answer, Exception):
            raise answer
        return answer
    return _f


try:
    lc._ask_cleanup = _fake_cleanup(_GOOD)
    LCALLS.clear()
    _t1, _i1 = asyncio.run(lc.clean_text(_BAD, _SRC, priority="normal"))
    _n1 = len(LCALLS)
    _t0, _i0 = asyncio.run(lc.clean_text(_GOOD, _SRC))
    _n0 = len(LCALLS) - _n1
    lc._ask_cleanup = _fake_cleanup(_GOOD.replace("1448", "1488"))
    _t2, _i2 = asyncio.run(lc.clean_text(_BAD, _SRC))
    lc._ask_cleanup = _fake_cleanup(_GOOD.replace("## Разбор\n", ""))
    _t3, _i3 = asyncio.run(lc.clean_text(_BAD, _SRC))
    lc._ask_cleanup = _fake_cleanup(RuntimeError("шлюз недоступен"))
    _t4, _i4 = asyncio.run(lc.clean_text(_BAD, _SRC))
    lc._ask_cleanup = _fake_cleanup(_BAD.replace("冷却", "охлаждение"))
    _t5, _i5 = asyncio.run(lc.clean_text(_BAD, _SRC))
finally:
    lc._ask_cleanup = _orig_ask
check(_t1 == _GOOD and _i1 == {"found": ["冷却", "remains active"], "cleaned": True, "accepted": True, "model": "fast"}
      and _n1 == 1 and LCALLS[0] == (_BAD, "normal") and _t0 == _GOOD and _i0 is None and _n0 == 0,
      f"3ж: принято — перевод; один запрос, на вход только текст; чужого языка нет — запроса нет: {_i1}")
check(_t2 == lc.strip_cjk(_BAD) and _i2["accepted"] is False and "потеряны числа: 1448" in _i2["reason"]
      and _i2["stripped"] is True and "冷" not in _t2,
      f"3ж: число не сохранено — исходник с вычищенными иероглифами: {_i2}")
check(_i3["accepted"] is False and _i3["reason"].startswith("потерян заголовок"), f"3ж: заголовок потерян: {_i3}")
check(_t4 == lc.strip_cjk(_BAD) and _i4["cleaned"] is False and "шлюз недоступен" in _i4["error"],
      f"3ж: сбой очистки — исходник без иероглифов: {_i4}")
check(_i5["accepted"] is False and "remains active" in _i5["reason"], f"3ж: английская фраза осталась — не принято: {_i5}")
check(lc.get_config() == {"model": "fast", "reasoning": "off"} and "ai_lang_cleanup_model" in _main_src,
      "3ж: быстрая модель шлюза по умолчанию, настройка ai_lang_cleanup_model")

# Подключено к заключению (оба пути), пересказу, гейту и совету; источник — их вход
_BAD2 = ("## Сводка" + chr(10) + "ДГУ прошла цикл: стоп →冷却 → перезапуск в 09:25:52. "
         "Код 1448 remains active." + chr(10) + "## Разбор" + chr(10) + "Частота 41,5 Гц.")
_GOOD2 = _BAD2.replace("冷却", "охлаждение").replace("remains active", "остаётся активным")
import types  # noqa: E402

import corpus.humanizer as chum  # noqa: E402
_orig_chat, _orig_hllm = lclient.chat, chum._humanize_llm
# Путь Claude API: модуль агента подменён (anthropic в тестовом окружении не нужен)
_orig_agent_mod = sys.modules.get("corpus.agent")
cagent = types.ModuleType("corpus.agent")


async def _chat_bad(system, user, **k):
    return _BAD2


async def _agent_bad(seg_row, kb, system_prompt=None, model=None):
    return {"conclusion_md": _BAD2, "error": None, "debug_json": {"x": 1}, "verdict": "авария",
            "alarm_level": "SHUTDOWN"}


async def _hum_bad(system, user, meta=None):
    return _BAD2


_E_LLM = {"id": "route:llm", "name": "Горыныч", "type": "llm", "provider": "gorynych", "model": "fast",
          "max_ctx_tokens": 10 ** 6, "_global": True}
_E_API = {"id": "route:api", "name": "Claude API", "type": "api", "provider": "anthropic", "model": "m",
          "max_ctx_tokens": 10 ** 6, "_route": True}
try:
    lc._ask_cleanup = _fake_cleanup(_GOOD2)
    cagent.analyse_segment = _agent_bad
    sys.modules["corpus.agent"] = cagent
    lclient.chat, chum._humanize_llm = _chat_bad, _hum_bad
    _rl = asyncio.run(cw._analyse_segment_chain(SR31, None, "SYS", [_E_LLM], depth="seg_shutdown"))
    _ra = asyncio.run(cw._analyse_segment_chain(SR31, None, "SYS", [_E_API], depth="seg_shutdown"))
    router.apply_depth("humanize", "fast", "default", "batch", provider="llm")
    _hm: dict = {}
    _hu = asyncio.run(chum.humanize("## Сводка\nтекст заключения 1448 41,5 09:25:52", meta=_hm))
    router.apply_depth("incident", "smart", "xhigh", "normal", provider="llm")
    _im: dict = {}
    _it = asyncio.run(ig._ask("АКТ", _im))
finally:
    lc._ask_cleanup = _orig_ask
    lclient.chat, chum._humanize_llm = _orig_chat, _orig_hllm
    if _orig_agent_mod is not None:
        sys.modules["corpus.agent"] = _orig_agent_mod
    else:
        sys.modules.pop("corpus.agent", None)
check(_rl["conclusion_md"].startswith(_GOOD2) and _rl["debug_json"]["lang_check"]["accepted"] is True,
      f"3ж: заключение (шлюз) — очищено до подписи, отметка в debug_json: {_rl['debug_json'].get('lang_check')}")
check(_ra["conclusion_md"] == _GOOD2 and _ra["debug_json"]["lang_check"]["accepted"] and _ra["debug_json"]["x"] == 1,
      "3ж: заключение (Claude API) — тот же проход, без смены модели")
check(_hu.startswith(_GOOD2) and _hm.get("lang_check", {}).get("accepted") is True,
      f"3ж: пересказ — очищен, отметка в meta: {_hm}")
check(_it.startswith(_GOOD2) and _im.get("lang_check", {}).get("accepted") is True,
      f"3ж: ответ совета — очищен, без перезаказа совета: {_im}")
_sv: list = []
_orig_ssw = odb.save_segment_warning


async def _ssw(*a, **k):
    _sv.append(k)
    return True
odb.save_segment_warning = _ssw
try:
    asyncio.run(ig._save("SN", "pcc", 1, M(16, 9, 26, 23), 35931, "текст", extra={"lang_check": {"x": 1}}))
    asyncio.run(ig._save("SN", "pcc", 1, M(16, 9, 26, 23), 35931, "текст"))
finally:
    odb.save_segment_warning = _orig_ssw
check(_sv[0].get("extra") == {"lang_check": {"x": 1}} and "extra" not in _sv[1],
      "3ж: отметка совета — в записи разбора (warning_analyses)")
# Гейт: та же обвязка, что в 1ж
for n, f in zip(_G_NAMES, (_g_open, _g_none, _g_counts, _g_empty, _g_empty, _g_true, _g_event, _g_save,
                           _g_mark, _g_none)):
    setattr(odb, n, f)
_gsaved: list = []


async def _g_save2(*a, **k):
    _gsaved.append(k)
    return True


async def _g_llm_bad(prompt, model, depth=None, meta=None):
    return _BAD2, "pass", "угроза", 10, 5
odb.save_segment_warning = _g_save2
mgr._run_warning_gate_llm = _g_llm_bad
cs.get_claude_settings = lambda: {"model": "claude-sonnet-5-5", "max_tokens": 1000, "proxy": ""}
G.update({"events": [], "marks": []})
try:
    lc._ask_cleanup = _fake_cleanup(_GOOD2)
    asyncio.run(mgr._analyze_warning_claude("SN", "pcc", 1, dict(_STRUCT), "fh2"))
finally:
    lc._ask_cleanup = _orig_ask
    for n, f in _orig_g.items():
        setattr(odb, n, f)
    mgr._run_warning_gate_llm = _orig_llm
    cs.get_claude_settings = _orig_cs
_gev = [ev for ev, _sid in G["events"] if ev.get("fault_hash") == "fh2"]
check(_gsaved and "冷" not in _gsaved[0]["analysis_md"] and "охлаждение" in _gsaved[0]["analysis_md"]
      and _gsaved[0]["extra"].get("lang_check", {}).get("accepted") is True
      and _gev and "lang_check" not in _gev[0],
      f"3ж: разбор гейта — очищен, отметка в записи разбора (журнал пишется до прохода языка, "
      f"ревью gate-2): {_gsaved and _gsaved[0].get('extra')}")



# ══ 4. Ревью v5.1.22: подтверждённые находки (сценарии ревьюеров) ═════════════

from analytics.transitions import link_from_row, link_from_segment  # noqa: E402
from analytics.serializer import supp_remark_text  # noqa: E402
from corpus.preprocessor import _engine_hours_of  # noqa: E402


class GStore:
    """Открытая строка и эпизоды машины в памяти (харнесс ревьюера g_h1): гейт
    и движок пишут в одну и ту же строку."""

    def __init__(self, row, eps):
        self.row, self.eps, self.calls = row, eps, []

    async def get_open_segment(self, *a, **k):
        return dict(self.row, gate_log=list(self.row["gate_log"])) if self.row else None

    async def get_run_state_origin_ts(self, *a, **k):
        return None

    async def count_episodes_batch(self, *a, **k):
        return {}

    async def get_open_episodes(self, *a, **k):
        return []

    async def get_episodes_overlapping(self, *a, **k):
        return []

    async def set_segment_gate_suppression(self, r, e, p, suppressed_hash=None, segment_id=None, ts=None):
        self.calls.append(("set_hash", suppressed_hash))
        self.row["gate_suppressed_hash"] = suppressed_hash
        return True

    async def append_segment_gate_event(self, *a, event=None, segment_id=None, ts=None, **k):
        self.calls.append(("event", event.get("kind")))
        self.row["gate_log"].append(event)
        return True

    async def save_segment_warning(self, *a, **k):
        self.calls.append(("save", k.get("extra")))
        return True

    async def set_episodes_gate_suppressed(self, r, e, p, scenarios, ids=None, until=None, after=None, src=None):
        out = []
        for x in self.eps:
            if x["t_close"] is not None or x["scenario"] not in scenarios:
                continue
            if ids is not None and x["id"] not in ids:
                continue
            if until is not None and not x["t_open"] <= until:
                continue
            if after is not None and not x["t_open"] > after:
                continue
            x["gate_suppressed"] = True
            x["src"] = x.get("src") or src
            out.append(x["id"])
        self.calls.append(("mark", "until" if until else "after" if after else "ids", out,
                           (src or {}).get("kind")))
        return out

    async def resolve_segment_id(self, *a, **k):
        return self.row["id"] if self.row else None

    async def get_segment_by_id(self, *a, **k):
        return None

    async def clear_open_gate_suppression_hash(self, r, e, p, h):
        ok = self.row is not None and self.row.get("gate_suppressed_hash") == h
        if ok:
            self.row["gate_suppressed_hash"] = None
        self.calls.append(("clear_hash", ok))
        return ok

    async def apply_late_gate_suppression(self, *a, **k):
        return False, False

    def of(self, kind):
        return [c for c in self.calls if c[0] == kind]


_GS_NAMES = ("get_open_segment", "get_run_state_origin_ts", "count_episodes_batch", "get_open_episodes",
             "get_episodes_overlapping", "set_segment_gate_suppression", "append_segment_gate_event",
             "save_segment_warning", "set_episodes_gate_suppressed", "resolve_segment_id",
             "get_segment_by_id", "clear_open_gate_suppression_hash", "apply_late_gate_suppression")
_orig_gs = {n: getattr(odb, n) for n in _GS_NAMES}
_orig_llm4, _orig_ask4, _orig_cs4 = mgr._run_warning_gate_llm, lc._ask_cleanup, cs.get_claude_settings


def gs_bind(store):
    for n in _GS_NAMES:
        setattr(odb, n, getattr(store, n))


def gs_engine(eps_mem):
    e = eng.OnlinePollEngine.__new__(eng.OnlinePollEngine)
    e.cfg = Cfg()
    e.router_sn, e.equip_type, e.panel_id = "SN", "pcc", 1
    e._episodes, e._episode_accrual_ts, e._last_open_seg_id = eps_mem, None, 1
    e._supp_recheck, e._prev_seg_hint, e.cursor_ts = None, None, M(16, 9, 0)
    e._supp_refused, e._supp_refused_sig = {}, ("подпись прошлой попытки",)
    e._attach_trip_context = _noop
    return e


cs.get_claude_settings = lambda: {"model": "claude-sonnet-5-5", "max_tokens": 1000, "proxy": ""}
router.apply_depth("gate_caution", "fast", "low", "interactive", provider="llm")
mgr._GATE_SAVE_RETRY_SEC = 0
_ST_LP = sa.build_structural_status({"id": 1, "run_state": 3, "t_start": M(16, 9, 0), "characteristics_json": {},
                                     "current_values_json": {"values": {}}, "active_detections_json": [LP]})
_W1464 = panel("WARNING", 1464, M(16, 9, 29, 42))
try:
    # ── 4а. gate-1: эпизод, открытый во время разбора, при сигнале панели снятие
    # не наследует (#35944: просадка 09:30:02–09:31:12 внутри 1464); id — движку ──
    for _tag, _dets_during in (("сигнал панели 1464", [LP, _W1464]), ("состав сменился", [LP, RPM])):
        _st = GStore({"id": 1, "t_start": M(16, 9, 0), "run_state": 3, "gate_suppressed_hash": None,
                      "gate_log": [], "warning_analyses": [], "characteristics_json": {},
                      "active_detections_json": [LP]},
                     [{"id": 4001, "scenario": "LIMIT_PROXIMITY", "t_open": M(16, 9, 10), "t_close": None,
                       "gate_suppressed": False}])
        gs_bind(_st)
        _eg = gs_engine({"LIMIT_PROXIMITY": {"id": 4001, "t_open": M(16, 9, 10)}})

        async def _llm_4a(prompt, model, depth=None, meta=None, _st=_st, _eg=_eg, _d=_dets_during):
            # Во время разбора: прежний эпизод кончился, пришёл сигнал (или сменился
            # состав), открылся новый эпизод того же сценария
            _st.eps[0]["t_close"] = datetime.now(U)
            _t2 = datetime.now(U) + timedelta(seconds=1)
            _st.eps.append({"id": 4002, "scenario": "LIMIT_PROXIMITY", "t_open": _t2, "t_close": None,
                            "gate_suppressed": False})
            _eg._episodes = {"LIMIT_PROXIMITY": {"id": 4002, "t_open": _t2}}
            _st.row["active_detections_json"] = list(_d)
            return "Анализ: близость к уставке ложная.", "cancel", "ложное", 10, 5
        mgr._run_warning_gate_llm = _llm_4a
        _res = asyncio.run(mgr._analyze_warning_claude("SN", "pcc", 1, dict(_ST_LP), "fh4a", engine=_eg))
        _after = [c for c in _st.of("mark") if c[1] == "after"]
        check(_res == mgr.GATE_OK and not _after and "inherited" not in [c[1] for c in _st.of("event")]
              and _st.eps[1]["gate_suppressed"] is False
              and 4002 in _eg._supp_refused and _eg._supp_refused_sig is None
              and _st.row["gate_suppressed_hash"] == H_LP,
              f"4а: gate-1 ({_tag}) — эпизод, открытый во время разбора, не помечен, записи inherited нет, "
              f"id в отказанных движка: {_st.calls}, {_eg._supp_refused}")
    # Запрет снят, эпизод идёт — повтор наследования по общему правилу движка
    check(_eg._refused_retry_due([LP, _W1464]) is False and _eg._refused_retry_due([LP]) is True,
          "4а: gate-1 — пока горит WARNING, повтора нет; ушёл — повтор (подпись сброшена)")
    _st.row["active_detections_json"] = [LP]
    _st.calls.clear()
    asyncio.run(_eg._inherit_gate_suppression([], [LP], ts=datetime.now(U)))
    check([c for c in _st.of("mark") if c[2] == [4002] and c[3] == "inherited"]
          and _st.of("event") == [("event", "inherited")] and not _eg._supp_refused
          and _st.eps[1]["src"]["ts"] == next(e for e in _st.row["gate_log"] if e.get("kind") == "llm")["ts"],
          f"4а: gate-1 — после снятия запрета эпизод наследует снятие (inherited, источник — разбор): {_st.calls}")

    # ── 4б. gate-2 / incident-2: журнал разбора — до прохода языка; в окне очистки
    # движок видит своё решение строки (перенос идёт, новый эпизод наследует) ──
    _st = GStore({"id": 2, "t_start": M(16, 9, 0), "run_state": 3, "gate_suppressed_hash": None,
                  "gate_log": [], "warning_analyses": [], "characteristics_json": {},
                  "active_detections_json": [LP]},
                 [{"id": 4101, "scenario": "LIMIT_PROXIMITY", "t_open": M(16, 9, 10), "t_close": None,
                   "gate_suppressed": False}])
    gs_bind(_st)
    _SNAP: dict = {}

    async def _llm_4b(prompt, model, depth=None, meta=None):
        return "## Разбор\nБлизость к уставке remains active, ложная.", "cancel", "ложное", 10, 5

    async def _cleanup_4b(text, priority, meta):
        _row = await odb.get_open_segment("SN", "pcc", 1)
        _SNAP["log"] = [(e.get("kind"), e.get("suppressed_hash"), e.get("episodes")) for e in _row["gate_log"]]
        _bnd = datetime.now(U)
        _sg = mkseg(M(16, 9, 0), _bnd, 3, [mkdet("LIMIT_PROXIMITY", M(16, 9, 10), _bnd, True)])
        _SNAP["carry"] = eng._gate_carry({"suppressed_hash": _row["gate_suppressed_hash"],
                                          "gate_log": _row["gate_log"]},
                                         [(_sg, 35950, M(16, 9, 0), _bnd)], {"LIMIT_PROXIMITY": {"id": 4101}},
                                         {"LIMIT_PROXIMITY"}, {"LIMIT_PROXIMITY"}, 5, _bnd,
                                         eng._extract_open_segment_data(_sg)[1])
        # Новый эпизод того же состава в этой строке, пока идёт очистка
        _st.eps.append({"id": 4102, "scenario": "LIMIT_PROXIMITY", "t_open": datetime.now(U), "t_close": None,
                        "gate_suppressed": False})
        _e = gs_engine({"LIMIT_PROXIMITY": {"id": 4102}})
        await _e._inherit_gate_suppression([("LIMIT_PROXIMITY", 4102, datetime.now(U))], [LP], ts=datetime.now(U))
        _SNAP["hash_after_engine"] = _st.row["gate_suppressed_hash"]
        return text.replace("remains active", "сохраняется")
    mgr._run_warning_gate_llm = _llm_4b
    lc._ask_cleanup = _cleanup_4b
    _res = asyncio.run(mgr._analyze_warning_claude("SN", "pcc", 1, dict(_ST_LP), "fh4b"))
    _kinds = [c[0] if c[0] != "event" else f"event:{c[1]}" for c in _st.calls]
    check(_SNAP.get("log") and _SNAP["log"][0] == ("llm", H_LP, [4101]),
          f"4б: gate-2 — в окне очистки своё решение уже в журнале: {_SNAP.get('log')}")
    check(_SNAP.get("carry") is not None and _SNAP["carry"]["hash"] == H_LP,
          f"4б: gate-2 — рез/смена режима в окне очистки переносит снятие: {_SNAP.get('carry')}")
    check(_SNAP.get("hash_after_engine") == H_LP and not _st.of("clear_hash")
          and _st.eps[-1]["gate_suppressed"] is True and (_st.eps[-1]["src"] or {}).get("kind") == "inherited",
          f"4б: incident-2 — новый эпизод в окне очистки наследует, хеш строки не снят: {_st.calls}")
    _sv4 = _st.of("save")
    check(_kinds.index("event:llm") < _kinds.index("save") and _sv4
          and (_sv4[0][1] or {}).get("lang_check", {}).get("accepted") is True
          and "lang_check" not in next(e for e in _st.row["gate_log"] if e.get("kind") == "llm"),
          f"4б: журнал разбора — до прохода языка и сохранения; отметка языка — в записи разбора: {_kinds}")
finally:
    for n, f in _orig_gs.items():
        setattr(odb, n, f)
    mgr._run_warning_gate_llm, lc._ask_cleanup, cs.get_claude_settings = _orig_llm4, _orig_ask4, _orig_cs4

# ── 4в. gate-3: догонка [Работа (разбор 08:10) → Стоп → Работа] — синтетика
# послестоповой «Работы» снятой не помечается (тот же стоп-фильтр, что у переноса) ──
_DEC3 = M(16, 8, 10)
_LOG3 = [{"ts": iso(_DEC3), "kind": "llm", "decision": "cancel", "decision_applied": True,
          "suppressed_hash": H_RPM, "fault_hash": "f1", "episodes": [500]}]
_GS3 = {"suppressed_hash": H_RPM, "gate_log": _LOG3}
_w31 = mkseg(M(16, 8, 0), M(16, 8, 30), 3, [mkdet("RPM_UNDERSPEED", M(16, 8, 5), M(16, 8, 20), False)],
             cause_close="RUN_STATE_CHANGE")
_st3 = mkseg(M(16, 8, 30), M(16, 8, 40), 0, stop_kind="EMERGENCY", cause_close="SHUTDOWN_CLEARED")
_w32 = mkseg(M(16, 8, 41), M(16, 9, 0), 3, [mkdet("RPM_UNDERSPEED", M(16, 8, 58, 30), M(16, 9, 0), True)])
_row3 = open_row(_LOG3, M(16, 8, 0))
check(eng._gate_suppressed_scenarios(_GS3, _w32, _row3, closed=[_w31, _st3, _w32]) == set()
      and eng._gate_suppressed_scenarios(_GS3, _w32, _row3) == {"RPM_UNDERSPEED"},
      "4в: gate-3 — стоп между решением и сегментом: синтетика не снята (без списка закрываемых — как раньше)")
_st0 = mkseg(M(16, 7, 0), M(16, 7, 30), 0, stop_kind="SIMPLE")
_w33 = mkseg(M(16, 7, 30), M(16, 9, 0), 3, [mkdet("RPM_UNDERSPEED", M(16, 8, 58, 30), M(16, 9, 0), True)])
check(eng._gate_suppressed_scenarios(_GS3, _w33, open_row(_LOG3, M(16, 7, 0)), closed=[_st0, _w33])
      == {"RPM_UNDERSPEED"},
      "4в: gate-3 — стоп до решения не помеха")
_st_after = mkseg(M(16, 8, 30), M(16, 9, 0), 0, stop_kind="SIMPLE")
check(eng._gate_suppressed_scenarios(_GS3, _st_after, _row3, closed=[_w31, _st_after]) == set(),
      "4в: gate-3 — сам сегмент — стоп, начавшийся после решения: не снимается")
_SUMM3: list = []


async def _summ3(segs, t0, t1, closing=False, live_keys=None, suppressed_sc=None, supp_src=None):
    _SUMM3.append((segs[0].run_state, sorted(suppressed_sc or []), supp_src))
    return None, None
_no_summary_orig = _no_summary
_no_summary = _summ3
try:
    cwr, _ = close_window([_w31, _st3, _w32], _row3, {}, M(16, 8, 0), M(16, 9, 0))
finally:
    _no_summary = _no_summary_orig
    for n, f in _orig_cw.items():
        setattr(eng.online_db, n, f)
    eng._run_segment, eng._collect_and_enrich_detections = _orig_run, _orig_collect
    _verifier.fire_verify = _orig_fv
_st3c = dict(cwr.appended[0][1]) if cwr.appended else {}
check(_SUMM3 and all(s[1] == [] for s in _SUMM3) and not _st3c.get("gate_suppressed_hash"),
      f"4в: gate-3 — _close_window при догонке: в сводках снятых нет, хеш не переходит: {_SUMM3}")

# ── 4г. gate-4: перенос снимает только то, что закрытая карточка сняла ──
H_AB = sa.compute_analytics_hash([RPM, LP])
_cr4 = eng._gate_carry({"suppressed_hash": H_RPM, "gate_log": GS["gate_log"]}, [(work, 35890, M(14, 9, 0), B)],
                       EPS, {"RPM_UNDERSPEED"}, None, 3, B, _dend, supp_sc={"LIMIT_PROXIMITY"})
check(_cr4 and _cr4["record"]["synthetic"] == ["LIMIT_PROXIMITY"],
      f"4г: gate-4 — в записи carry — тревоги, снятые в сводке закрытой карточки: {_cr4 and _cr4['record']}")
_CARRY_B = [{"ts": iso(M(15, 9, 0)), "kind": "carry", "decision": "cancel", "decision_applied": True,
             "suppressed_hash": H_AB, "fault_hash": "fAB",
             "source": {"ts": iso(M(14, 20, 0)), "segment_id": 35880}, "episodes": [601, 602]}]
# Строка B: только перенос; LP снова за цикл до реза — в сводке B живая
_segB = mkseg(M(15, 9, 0), M(16, 9, 0), 3, [mkdet("RPM_UNDERSPEED", M(14, 20, 0), M(16, 9, 0), True),
                                            mkdet("LIMIT_PROXIMITY", M(16, 8, 59, 40), M(16, 9, 0), True)])
_rowB = open_row(_CARRY_B, M(15, 9, 0), H_AB)
check(eng._gate_suppressed_scenarios({"suppressed_hash": H_AB, "gate_log": _CARRY_B}, _segB, _rowB) == set(),
      "4г: строка B с перенесённым снятием — новые тревоги в её сводке живые")
try:
    cwr, _ = close_window([_segB], _rowB, {"RPM_UNDERSPEED": {"id": 601}}, M(15, 9, 0), M(16, 9, 0))
finally:
    for n, f in _orig_cw.items():
        setattr(eng.online_db, n, f)
    eng._run_segment, eng._collect_and_enrich_detections = _orig_run, _orig_collect
    _verifier.fire_verify = _orig_fv
_tailC = dict(cwr.appended[0][1]) if cwr.appended else {}
_recC = [g for g in _tailC.get("gate_log") or [] if g.get("kind") == "carry"]
check(_tailC.get("gate_suppressed_hash") == H_AB and _recC and _recC[-1]["episodes"] == [601]
      and _recC[-1]["synthetic"] == [],
      f"4г: в строку C — перенос по 601, синтетики снятой нет: {_tailC}")
db4 = FakeDB()
e4 = mk_engine(db4)
try:
    db4.row = row_with(H_AB, M(16, 9, 0), log=_tailC.get("gate_log") or [], rid=35930)
    e4._episodes = {"RPM_UNDERSPEED": {"id": 601}, "LIMIT_PROXIMITY": {"id": 603}}
    asyncio.run(e4._inherit_gate_suppression([("LIMIT_PROXIMITY", 603, M(16, 8, 59, 40))], [RPM, LP],
                                             ts=M(16, 9, 0, 30)))
    check(not db4.of("mark") and db4.of("clear_hash") == [("clear_hash", H_AB)],
          f"4г: gate-4 — LP 603 (живая в карточке B) перенесённым снятием не снимается, хеш снят — разберёт "
          f"гейт: {db4.calls}")
    # Та же тревога, снятая в сводке B (синтетика своего решения), — переносится
    db4.calls.clear()
    db4.row = row_with(H_AB, M(16, 9, 0), log=[dict(_recC[-1], synthetic=["LIMIT_PROXIMITY"])], rid=35930)
    asyncio.run(e4._inherit_gate_suppression([("LIMIT_PROXIMITY", 603, M(16, 8, 59, 40))], [RPM, LP],
                                             ts=M(16, 9, 0, 30)))
    _mk4 = db4.of("mark")
    check(len(_mk4) == 1 and _mk4[0][2] == [603] and _mk4[0][3]["kind"] == "carry" and not db4.of("clear_hash"),
          f"4г: снятая в закрытой карточке тревога — продолжение переносом: {db4.calls}")
    # Поздний вердикт (late): снимал начавшиеся не позже момента разбора
    _LATE = [{"ts": iso(M(16, 9, 1)), "kind": "late", "decision": "cancel", "decision_applied": True,
              "suppressed_hash": H_AB, "fault_hash": "fAB", "episodes": [],
              "source": {"ts": iso(M(16, 8, 59, 50)), "segment_id": 35929},
              "alarms": [{"scenario": "LIMIT_PROXIMITY"}, {"scenario": "RPM_UNDERSPEED"}]}]
    db4.calls.clear()
    db4.row = row_with(H_AB, M(16, 9, 0), log=_LATE, rid=35930)
    asyncio.run(e4._inherit_gate_suppression([("LIMIT_PROXIMITY", 603, M(16, 8, 59, 40))], [RPM, LP]))
    _ok_late = db4.of("mark") and db4.of("mark")[0][2] == [603]
    db4.calls.clear()
    asyncio.run(e4._inherit_gate_suppression([("LIMIT_PROXIMITY", 604, M(16, 8, 59, 55))], [RPM, LP]))
    check(_ok_late and not db4.of("mark") and db4.of("clear_hash"),
          f"4г: поздний вердикт — начавшийся до разбора переносится, после — нет: {db4.calls}")
finally:
    for n, f in _orig_db.items():
        setattr(eng.online_db, n, f)

# ── 4д. verdict-1: наработка сегментов до v5.1.6 — секунды, не часы ──
_ch510 = {"engine_hours_start": 54812520.0, "subsegments": [
    {"characteristics": {"ENGINE_HOURS": {"value_end": 54850000.0}}},
    {"characteristics": {"ENGINE_HOURS": {"value_end": 54898740.0}}}]}
check(engine_hours_text(_engine_hours_of(_ch510, "5.1.0")) == "15 250 ч"
      and engine_hours_text(_engine_hours_of(_ch510)) == "15 250 ч"
      and engine_hours_text(_engine_hours_of({"engine_hours_start": 48809700.0, "subsegments": []}, "5.1.0"))
      == "13 558 ч",
      f"4д: verdict-1 — #35765 (5.1.0): секунды → часы, а не «54 898 740 ч»: "
      f"{_engine_hours_of(_ch510, '5.1.0')}")
check(engine_hours_text(_engine_hours_of({"engine_hours_start": 500000.0, "subsegments": []}, "4.9.62"))
      == "139 ч"
      and engine_hours_text(_engine_hours_of({"engine_hours_start": 11121.2, "subsegments": []}, "5.1.8"))
      == "11 121 ч"
      and _engine_hours_of({"engine_hours_start": 9.9e9, "subsegments": []}, "5.1.0") is None,
      "4д: verdict-1 — молодой двигатель до 5.1.6 (секунды < 10^6) по версии; 5.1.8 — часы; "
      "неправдоподобное и после пересчёта — «нет в данных»")
_f510 = conclusion_facts({"run_state": 3, "characteristics_json": _ch510, "report_summary_json": None,
                          "analytics_version": "5.1.0"})
check(_f510[1] == "Наработка двигателя на конец периода: 15 250 ч (счётчик панели)."
      and not any("54 898" in x or "54\xa0898" in x for x in _f510),
      f"4д: verdict-1 — шапка заключения v5.1.0: {_f510}")
_sc510 = cw._shutdown_conclusion(dict(SR31, characteristics_json=dict(t31.to_dict(), engine_hours_end=None,
                                                                      engine_hours_start=54812520.0,
                                                                      subsegments=[]),
                                      analytics_version="5.1.0"), None, tz=MSK)
check(_sc510 and "Наработка двигателя на конец периода: 15\xa0226 ч" in _sc510["conclusion_md"]
      or (_sc510 and "15 226 ч" in _sc510["conclusion_md"]),
      f"4д: verdict-1 — заключение-факты (worker) тем же пересчётом: {_sc510 and _sc510['conclusion_md'][-400:]}")
check("analytics_version" in (ROOT / "corpus" / "db.py").read_text(encoding="utf-8").split(
    "async def get_segment_row")[1].split("WHERE id = $1")[0],
      "4д: verdict-1 — строка воркера несёт analytics_version")

# ── 4е. verdict-2: код на закрытии сегмента, оборванного аварией ──
_A0, _A1 = M(16, 13, 37, 8), M(16, 16, 26, 11)          # ДЭС №3 16.09: 146 вытеснен 151
_EA = (epers(40012, (0, _A0 - timedelta(hours=1)), (146, M(16, 15, 0)), (151, _A1), (0, M(16, 16, 28, 52)))
       + epers(40569, (0, _A0 - timedelta(hours=1)), (1, M(16, 16, 26, 19)), (0, M(16, 16, 26, 29)))
       + epers(40010, (2, _A0 - timedelta(hours=1)), (0, M(16, 16, 26, 20))))
_spA = _state_periods(_EA, KB_CFG).get(40012) or []
_ra = fault_code_close(_spA, _EA, KB_CFG, _A0, _A1, next_state=0, run_state=3, tripped=True) or {}
check(_ra.get("reset") is False and _ra.get("reset_at") is None and _ra.get("switch_off_at") is None
      and _ra.get("next_code") == 151
      and fault_code_close_text(_ra, MSK) == "Код 146 сменился в 16:26:11 на код 151 — он относится к "
                                             "следующему сегменту",
      f"4е: verdict-2 — сброс 16:26:19 (уже в стопе, при 151) старому коду не приписан, «сменился»: "
      f"{_ra}, {fault_code_close_text(_ra, MSK)}")
_rs = fault_code_close(_spA, _EA, KB_CFG, _A0, _A1, next_state=0) or {}
check(_rs.get("reset_at") == iso(M(16, 16, 26, 19)),
      "4е: стоп-сегмент (run_state не задан) — допуск в кадр после снятия прежний")
_B0, _B1 = M(16, 16, 30, 41), M(16, 16, 31, 8)          # 197: подъёмы 16:30:58 и 16:31:16
_EB = (epers(40012, (0, _B0 - timedelta(hours=1)), (197, M(16, 16, 30, 50)), (151, _B1), (0, M(16, 16, 33)))
       + epers(40569, (0, _B0 - timedelta(hours=1)), (1, M(16, 16, 30, 58)), (0, M(16, 16, 31, 4)),
               (1, M(16, 16, 31, 16)), (0, M(16, 16, 31, 20)))
       + epers(40010, (2, _B0 - timedelta(hours=1)), (0, M(16, 16, 31, 17))))
_rb = fault_code_close(_state_periods(_EB, KB_CFG).get(40012) or [], _EB, KB_CFG, _B0, _B1, next_state=0,
                       run_state=3, tripped=True) or {}
check(_rb.get("reset_at") == iso(M(16, 16, 30, 58)) and _rb.get("switch_off_at") is None,
      f"4е: verdict-2 — подъёмы до и после снятия: берётся последний не позже кадра снятия, «Выкл» после — "
      f"нет: {_rb}")
# Нарезка целиком: отметка обрыва — до кода на закрытии
_EAS = (epers(40011, (3, _A0), (0, _A1)) + _EA
        + epers(40013, (0, _A0 - timedelta(hours=1)), (4, _A1), (0, M(16, 16, 28, 52))))
_sA = seg_run(_EAS, [], hist_runs([(_A0, _A1)], M(16, 16, 0), M(16, 16, 40)), M(16, 16, 0), M(16, 16, 40))
_sA3 = next((s for s in _sA if s.run_state == 3), None)
check(_sA3 is not None and _sA3.closed_by_trip and (_sA3.fault_code_close or {}).get("reset") is False
      and (_sA3.fault_code_close or {}).get("next_code") == 151,
      f"4е: verdict-2 — нарезка: «Работа», оборванная 151, без ложного «сброшен»: "
      f"{_sA3 and (_sA3.fault_code_close, bool(_sA3.closed_by_trip))}")

# ── 4ж. verdict-3: «сняла аварийную маску» — только если маска была ──
_e52 = (epers(40011, (3, W0 - timedelta(hours=1)), (0, M(16, 9, 30)))
        + epers(40012, (0, W0 - timedelta(days=1)), (1452, M(16, 9, 30)), (0, M(16, 9, 32)))
        + epers(40013, (0, W0 - timedelta(days=1)), (4, M(16, 9, 30)), (0, M(16, 9, 32))))
_s52 = seg_run(_e52, [], hist_runs([(W0 - timedelta(hours=1), M(16, 9, 30))]))
_t52 = by_start(_s52, M(16, 9, 30))
_p52 = build_summary_parts([_t52], [], tz=MSK)
_f52 = conclusion_facts(srow(_t52, _p52))
check(_t52.stop_kind == "EMERGENCY" and _p52["mttr"]["by"] == "state"
      and any("Авария снята в 09:32:00 (через 2 мин) — панель сняла аварию (аварийной маски не было" in x
              for x in _f52)
      and not any("аварийную маску" in x for x in _f52),
      f"4ж: verdict-3 — 1452 без маски: формулировка по 40013: {_p52['mttr']}, {_f52}")
check(p31["mttr"]["by"] == "mask"
      and any("панель сняла аварийную маску" in x for x in conclusion_facts(srow(s31, p31))),
      f"4ж: verdict-3 — #35931 (маска 40408) — «сняла аварийную маску»: {p31['mttr']}")
check("панель сняла аварийную маску" in sc31["conclusion_md"]
      and "панель сняла аварию." in (cw._shutdown_conclusion(dict(SR31, report_summary_json=dict(
          SR31["report_summary_json"], mttr={k: v for k, v in SR31["report_summary_json"]["mttr"].items()
                                             if k != "by"})), None, tz=MSK) or {}).get("conclusion_md", ""),
      "4ж: verdict-3 — заключение-факты: маска — «аварийную маску»; части без источника — нейтрально")

# ── 4з. verdict-4: одна авария — одна отметка и в живом движке ──
_E4 = (epers(40011, (3, W0 - timedelta(hours=1)), (5, M(16, 9, 25)), (0, M(16, 9, 28)))
       + epers(40012, (0, W0 - timedelta(days=1)), (2224, M(16, 9, 25)), (0, M(16, 9, 30)))
       + epers(40013, (0, W0 - timedelta(days=1)), (3, M(16, 9, 25)), (0, M(16, 9, 30))))
_H4 = hist_runs([(W0 - timedelta(hours=1), M(16, 9, 28))])


def _clip(enum, w0, w1):
    return [p for p in enum if (p["state_end"] is None or p["state_end"] > w0) and p["state_start"] < w1]


def _seg_ctx(enum, w0, w1, ctx=None):
    return segment(enum_periods=_clip(enum, w0, w1), history=[r for r in _H4 if r["ts"] < w1], fault_periods=[],
                   gaps=[], cfg=KB_CFG, router_sn="T", equip_type="pcc", panel_id=1, engine_sn="",
                   ts_from=w0, ts_to=w1, transition_ctx=ctx)


_c1 = _seg_ctx(_E4, W0, M(16, 9, 25, 30))
_r1 = next(s for s in _c1 if s.run_state == 3)
_c2 = _seg_ctx(_E4, M(16, 9, 25), M(16, 9, 28, 30), [link_from_segment(_r1)])
_r5 = next(s for s in _c2 if s.run_state == 5)
_c2n = _seg_ctx(_E4, M(16, 9, 25), M(16, 9, 28, 30))
check(_r1.closed_by_trip and _r1.closed_by_trip.get("iv") == iso(M(16, 9, 25)) and _r5.closed_by_trip is None
      and next(s for s in _c2n if s.run_state == 5).closed_by_trip is not None,
      f"4з: verdict-4 — окна живого движка: «Работа» отмечена (iv 09:25), «Охлаждение» в следующем вызове "
      f"(звено контекста с отметкой) — нет; без контекста была вторая: {_r1.closed_by_trip}, {_r5.closed_by_trip}")
_lr = link_from_row({"run_state": 3, "t_start": iso(W0), "t_end": iso(M(16, 9, 25)),
                     "trip_iv": iso(M(16, 9, 25))})
_c2r = _seg_ctx(_E4, M(16, 9, 25), M(16, 9, 28, 30), [_lr])
check(_lr.trip_iv == M(16, 9, 25) and next(s for s in _c2r if s.run_state == 5).closed_by_trip is None
      and "closed_by_trip,iv" in (ROOT / "online" / "db.py").read_text(encoding="utf-8"),
      "4з: verdict-4 — после рестарта звено из БД (проекция closed_by_trip.iv) — та же одна отметка")

# ── 4и. verdict-5: перед ПРОСТЫМ стопом отметки нет (и поздняя — ждёт аварийности) ──
_nx_simple = {"value": 0, "state_start": M(16, 9, 30), "stop_kind": "SIMPLE", "attempt": None}
_iv5 = [(M(16, 9, 30, 2), M(16, 9, 31))]
check(_trip_mark(3, W0, M(16, 9, 30), _iv5, _nx_simple, set(), [], [], KB_CFG) is None
      and _trip_mark(3, W0, M(16, 9, 30), _iv5, dict(_nx_simple, stop_kind="EMERGENCY"), set(), [], [], KB_CFG),
      "4и: verdict-5 — маска кадром позже 40011 при простом стопе — отметки нет; при аварийном — есть")
_late5: list = []
_enq5: list = []


async def _late_db5(seg_id, trip, tz=None):
    _late5.append((seg_id, trip))
    return True, False          # уровень прежний (CAUTION)
_orig_late5, _orig_enq5 = eng.online_db.apply_late_run_trip, eng._enqueue_segment
eng.online_db.apply_late_run_trip = _late_db5
eng._enqueue_segment = lambda sid, again=False: _enq5.append((sid, again))
try:
    e5 = eng.OnlinePollEngine.__new__(eng.OnlinePollEngine)
    e5.cfg, e5.tz, e5.router_sn, e5.equip_type, e5.panel_id = KB_CFG, MSK, "R", "pcc", 1
    e5._trip_watch = {"id": 35950, "t_start": W0, "t_end": M(16, 9, 30), "run_state": 3}
    _E5w = (epers(40011, (3, W0 - timedelta(hours=1)), (0, M(16, 9, 30))) + epers(40012, (0, W0 - timedelta(days=1)))
            + epers(40013, (0, W0 - timedelta(days=1))))
    _F5 = [mask(40401, 13, M(16, 9, 30, 2), M(16, 9, 31), "Скорость/положение КВ: выше нормы")]
    _hs = NS(run_state=0, stop_kind="SIMPLE", attempt=None, t_start=iso(M(16, 9, 30)))
    asyncio.run(e5._late_run_trip([_hs], _E5w, _F5, M(16, 9, 30, 31)))
    check(not _late5 and e5._trip_watch is not None,
          f"4и: verdict-5 — поздняя отметка при простом стопе не пишется, ожидание идёт: {_late5}")
    # ── 4к. verdict-6: поздняя дописка перезаказывает заключение и без смены уровня ──
    _he = NS(run_state=0, stop_kind="EMERGENCY", attempt=None, t_start=iso(M(16, 9, 30)))
    asyncio.run(e5._late_run_trip([_he], _E5w, _F5, M(16, 9, 30, 41)))
    check(len(_late5) == 1 and _enq5 == [(35950, True)] and e5._trip_watch is None,
          f"4к: verdict-6 — стоп стал аварийным: отметка дописана, уровень прежний — заключение всё равно "
          f"перезаказано: {_late5}, {_enq5}")
finally:
    eng.online_db.apply_late_run_trip, eng._enqueue_segment = _orig_late5, _orig_enq5

# ── 4л. incident-1: strip_cjk не трогает отступы и пробелы вне вырезанного ──
_TXT1 = ("## Факты\n1. Пуск в 09:26\n   - обороты 1696: remains active\n   - частота 56,8 Гц\n\n"
         "```\nT    P    I\n09   0    0\n```\nКонец строки с переносом  \nследующая\n")
_orig_ask41 = lc._ask_cleanup


async def _fail41(text, priority, meta):
    raise RuntimeError("gw 500")
lc._ask_cleanup = _fail41
try:
    _o41, _i41 = asyncio.run(lc.clean_text(_TXT1, ""))
finally:
    lc._ask_cleanup = _orig_ask41
check(lc.strip_cjk(_TXT1) == _TXT1 and _o41 == _TXT1 and _i41["stripped"] is False,
      f"4л: incident-1 — иероглифов нет: текст как есть (вложенность, ```-блок, «  \\n»), stripped=False: {_i41}")
check(lc.strip_cjk("1. Пуск\n   - обороты 冷却 1696\n   - стоп →冷却 → пуск\n    冷却 отступ\nрежим冷却после")
      == "1. Пуск\n   - обороты 1696\n   - стоп → → пуск\n    отступ\nрежим после",
      f"4л: incident-1 — вырез только иероглифов, отступы целы: "
      f"{lc.strip_cjk(chr(10).join(['1. Пуск', '   - обороты 冷却 1696']))!r}")

# ── 4м. incident-3: ссылки «[W10, по памяти]», «[W1, W2]» — цитата руководства ──
_q = "Перечень — «dead or weak battery or a failed starter»"
check(all(lc.foreign_fragments(f"{_q} {r}", "", True) == []
          for r in ("[W10]", "[W10, по памяти, не проверено]", "[W1, W2]", "[W1][W2]"))
      and lc.foreign_fragments(f"{_q} [W10, по памяти]", "") != [],
      "4м: incident-3 — ссылки с пояснением и списком у совета законны; без allow_ref_quotes — чужой язык")

# ── 4н. incident-8: разметка ответа очистки ──
_orig8 = ("## Коротко\nОбороты просели до 1310 об/мин, remains active до 09:31.\n\n"
          "## Факты\n- Частота 49,4 Гц\n  - нагрузка 0 кВт\n")
_good8 = _orig8.replace("remains active", "держится")
check(lc.accept_cleaned(_orig8, _good8) == (True, "")
      and lc.accept_cleaned(_orig8, "```markdown\n" + _orig8 + "```")[0] is False
      and lc.accept_cleaned(_orig8, "```markdown\n" + _good8 + "```")[0] is False
      and lc.accept_cleaned(_orig8, _good8.replace("- ", ""))[0] is False
      and lc.accept_cleaned(_orig8, _good8.replace("## Факты", "Факты"))[0] is False,
      f"4н: incident-8 — обёртка ``` (и с непереведённым внутри), снятые маркеры списков/заголовков — отказ: "
      f"{lc.accept_cleaned(_orig8, '```markdown' + chr(10) + _orig8 + '```')}")
_orig_ask8 = lc._ask_cleanup


async def _fenced8(text, priority, meta):
    return "```markdown\n" + _good8 + "```"
lc._ask_cleanup = _fenced8
try:
    _o8, _i8 = asyncio.run(lc.clean_text(_orig8, ""))
finally:
    lc._ask_cleanup = _orig_ask8
check(_o8 == _good8.strip() and _i8["accepted"] is True,
      f"4н: incident-8 — одна внешняя обёртка ``` снимается, перевод принят: {_i8}, {_o8!r}")

# ── 4о. incident-4: остановка сервиса во время очистки ответа совета ──
_SAVED4: list = []
_LOG4: list = []
_MARKS4: list = []


async def _chat4(system, user, **k):
    return "## Коротко\nПричина — cold start двигателя, код 234.\n"


async def _slow4(text, priority, meta):
    await asyncio.sleep(30)
    return text


async def _mat4(*a, **k):
    return "АКТ"


async def _state4(*a, **k):
    return {"analysed": False, "terminal": None, "ordered": 0}


async def _mark4(*a, **k):
    _MARKS4.append(a[5])
    return True


async def _ssw4(*a, **k):
    _SAVED4.append(k)
    return True


class _H4(logging.Handler):
    def emit(self, rec):
        _LOG4.append(rec.getMessage())


_orig4 = (lclient.chat, lc._ask_cleanup, ig._material, ig._state, ig._mark, odb.save_segment_warning)
_h4 = _H4()
logging.getLogger("online.incident_gate").addHandler(_h4)
router.apply_depth("incident", "smart", "xhigh", "normal", provider="llm")


async def _run4():
    t = asyncio.create_task(ig._run_incident("SN", "pcc", 1, {}, M(16, 9, 28, 53), 35941))
    await asyncio.sleep(0.3)
    t.cancel()
    try:
        await t
    except asyncio.CancelledError:
        pass
try:
    lclient.chat, lc._ask_cleanup = _chat4, _slow4
    ig._material, ig._state, ig._mark, odb.save_segment_warning = _mat4, _state4, _mark4, _ssw4
    asyncio.run(_run4())
finally:
    lclient.chat, lc._ask_cleanup, ig._material, ig._state, ig._mark, odb.save_segment_warning = _orig4
    logging.getLogger("online.incident_gate").removeHandler(_h4)
check(len(_SAVED4) == 1 and "cold start" in _SAVED4[0]["analysis_md"]
      and _SAVED4[0]["fault_hash"] == ig.incident_hash(M(16, 9, 28, 53))
      and "остановка сервиса" in (_SAVED4[0].get("extra") or {}).get("lang_check", {}).get("error", "")
      and any("cold start" in m and "сохранён" in m for m in _LOG4),
      f"4о: incident-4 — ответ совета сохранён исходником (одна запись) и в журнале сервиса: "
      f"{len(_SAVED4)}, {_MARKS4}, {[m[:120] for m in _LOG4]}")

# ── 4п. incident-5: перезаказ заключения по устаревшему id сегмента ──
_ROWS5 = {200: {"id": 200, "t_end": M(16, 9, 29, 22)}}
_enq55: list = []


async def _gbi5(i):
    return _ROWS5.get(i)


async def _rs5(r, e, p, sid, ts):
    return sid if sid in _ROWS5 else 200
_orig5 = (eng._enqueue_segment, odb.resolve_segment_id, odb.get_segment_by_id)
eng._enqueue_segment = lambda sid, again=False: _enq55.append((sid, again))
odb.resolve_segment_id, odb.get_segment_by_id = _rs5, _gbi5
try:
    router.apply_depth("seg_shutdown", "fast", "default", "normal", provider="system")
    asyncio.run(ig._reorder_conclusion("SN", "pcc", 1, M(16, 9, 28, 53), 150))
finally:
    eng._enqueue_segment, odb.resolve_segment_id, odb.get_segment_by_id = _orig5
    router.apply_depth("seg_shutdown", "fast", "default", "normal", provider="llm")
check(_enq55 == [(200, True)],
      f"4п: incident-5 — id открытой стоп-строки удалён циклом закрытия: сегмент по моменту останова: {_enq55}")

# ── 4р. incident-7 / shadow-1: «кадр останова» — только отсчёт того же кадра ──
_stop7 = datetime(2026, 10, 13, 7, 40, 11, tzinfo=U)
_tr7 = {"v": 1, "t_from": iso(_stop7 - timedelta(seconds=60)), "t_to": iso(_stop7 + timedelta(seconds=30)),
        "stop": iso(_stop7), "step_sec": 10, "max_rows": 40,
        "segment": {"run_state": 2, "label": "Неудачный прогрев · Авария", "t_start": iso(_stop7 - timedelta(seconds=10)),
                    "t_end": iso(_stop7)},
        "cols": [{"role": "RPM", "addr": 40068, "unit": "rpm"}, {"role": "FREQUENCY", "addr": 40044, "unit": "Hz"},
                 {"role": "OIL_PRESS", "addr": 40062, "unit": "kPa"}],
        "entry": {"RPM": {"v": 0, "t": iso(_stop7 - timedelta(seconds=600))},
                  "FREQUENCY": {"v": 0, "t": iso(_stop7 - timedelta(seconds=600))}},
        "rows": [{"t": iso(_stop7), "v": {"OIL_PRESS": 0}},
                 {"t": iso(_stop7 + timedelta(seconds=8)), "v": {"RPM": 0}}],
        "states": []}
_ff7 = fall_facts_ru(_tr7, MSK) or ""
check("(кадр останова)" not in _ff7
      and "10:40:11 останов → 10:40:19 (через 8 с после останова) 0 об/мин (частота 0 Гц держится с 10:30:11)" in _ff7,
      f"4р: incident-7 / shadow-1 — ДЭС №3 13.10: обороты 0 через 8 с — после разделителя, не «кадр останова»: {_ff7}")
_tr7b = json.loads(json.dumps(_tr7))
_tr7b["entry"] = {"RPM": {"v": 1500, "t": iso(_stop7 - timedelta(seconds=90))},
                  "FREQUENCY": {"v": 50, "t": iso(_stop7 - timedelta(seconds=90))}}
_tr7b["rows"] = [{"t": iso(_stop7 - timedelta(seconds=20)), "v": {"RPM": 900, "FREQUENCY": 30}},
                 {"t": iso(_stop7 + timedelta(seconds=25)), "v": {"RPM": 0, "FREQUENCY": 0}}]
_ff7b = fall_facts_ru(_tr7b, MSK) or ""
check("10:39:51 900 об/мин / 30 Гц → 10:40:11 останов → 10:40:36 (через 25 с после останова) 0 об/мин / 0 Гц"
      in _ff7b and "кадр останова" not in _ff7b,
      f"4р: incident-7 — сценарий ревьюера (25 с): удержанные 900 в момент останова не выдаются за 0: {_ff7b}")
check("09:26:23 (кадр останова) 76 об/мин / 0 Гц" in (ff29 or ""),
      "4р: отсчёт в кадре останова — «кадр останова», как раньше")

# ── 4с. Переходный артефакт (теневой прогон): снятые до v5.1.22 без источника ──
_src52 = {"ts": iso(M(16, 8, 52)), "segment_id": None, "kind": "llm"}
_TA = M(16, 8, 0)


def _sseg8():
    sub = NS(characteristics={}, risk_accumulators=NS(coking_risk=NS(risk_level="GREEN")))
    return NS(run_state=3, stop_kind=None, cause_close=None, sequence_checks=[],
              t_start=iso(_TA), t_end=iso(_TA + timedelta(hours=1)), cause_open="RUN_STATE_CHANGE",
              subsegments=[sub])


_eps8 = [sep(M(16, 8, 10), True, None, closed=True), sep(M(16, 8, 20), True, None, closed=True),
         sep(M(16, 8, 50), True, _src52, closed=True), sep(M(16, 8, 55), False, None, closed=True)]
_p8 = build_summary_parts([_sseg8()], _eps8, tz=MSK, t_from=_TA, t_to=_TA + timedelta(hours=1))
_md8 = render_summary_md(_p8)
check(_p8["remarks"][0].get("supp_nosrc") == 2
      and "снято ИИ (разбор 08:52): 1 из 4; снято ИИ: 2 из 4" in _md8
      and "снято ИИ (разбор 08:52): 3 из 4" not in _md8,
      f"4с: снятые до v5.1.22 — отдельной подписью «снято ИИ», не новому разбору: {_md8}")
_p8b = build_summary_parts([_sseg8()], _eps8[:3], tz=MSK, t_from=_TA, t_to=_TA + timedelta(hours=1))
_md8b = render_summary_md(_p8b)
check("— *снято ИИ (разбор 08:52): 1; снято ИИ: 2*" in _md8b
      and "Предупреждения аналитики (3) сняты ИИ." in _md8b,
      f"4с: все сняты — две подписи; примечание вердикта общее: {_md8b}")
from corpus.preprocessor import _remarks_from_summary as _rfs  # noqa: E402
check("снято ИИ (разбор 08:52): 1 из 4; снято ИИ: 2 из 4" in _rfs({"report_summary_json": _p8}),
      "4с: вход заключения — та же раздельная подпись")
# Поздний вердикт поверх старого снятия без источника
_p8c = build_summary_parts([_sseg8()], [sep(M(16, 8, 10), True, None, closed=True), sep(M(16, 8, 40), False)],
                           tz=MSK, t_from=_TA, t_to=_TA + timedelta(hours=1))
_p8d = apply_gate_suppression(_p8c, ["RPM_UNDERSPEED"], until=M(16, 8, 45),
                              src={"ts": iso(M(16, 8, 45)), "segment_id": 35949})
check(_p8d["remarks"][0].get("supp_nosrc") == 1
      and supp_remark_text(_p8d["remarks"][0], 2) == "снято ИИ (разбор 08:45): 1; снято ИИ: 1",
      f"4с: поздний вердикт не присваивает себе снятое раньше без источника: {_p8d['remarks'][0]}")



# ── 5. Прогон совета 16.10 (#35941): состояние кода — снимок на момент останова ──
import inspect as _insp5  # noqa: E402
from llm import router as _rt5  # noqa: E402
import online.incident_gate as _ig5  # noqa: E402

check("снимок на момент останова" in _rt5._INCIDENT_PROMPT
      and "Не утверждай, сброшен ли код сейчас" in _rt5._INCIDENT_PROMPT,
      "5а: правило снимка кода в промпте совета")
check("снимок на момент останова" in _rt5.INCIDENT_RULES_V5122,
      "5б: правило — и в переносе сохранённого промпта")
_src5 = _insp5.getsource(_ig5)
check("Отчёт аналитики по сегменту (снимок на момент останова" in _src5
      and "**⚠ Код неисправности на момент останова:**" in _src5,
      "5в: отчёт стоп-сегмента во входе совета подписан как снимок")

if _errors:
    print(f"FAIL — {len(_errors)} расхождений:")
    for x in _errors:
        print(f"  • {x}")
    sys.exit(1)
print("Все проверки пройдены.")
