# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""Разбор аварии на совете шлюза: учёт задач, срок, повтор, восстановление (v5.1.11).

Совет (smart + xhigh) идёт десятки минут, в очереди batch — дольше. До v5.1.11
разбор был голым create_task: дубль при СТОП/ПУСК с перечиткой, ни одного
следа неудачи в БД, после рестарта посреди совета авария оставалась без разбора.

Запуск:  py -3 scripts/test_incident_inflight.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import llm.client as client  # noqa: E402
import llm.router as router  # noqa: E402
import online.db as odb  # noqa: E402
import online.incident_gate as ig  # noqa: E402

_errors: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        _errors.append(msg)


ig.INCIDENT_RETRY_AFTER_SEC = 0
ig._MARK_RETRY_SEC = 0
ig.INCIDENT_SAVE_RETRY_SEC = 0.01
ig.INCIDENT_SAVE_WINDOW_SEC = 3          # 4 попытки записи
STOP = datetime(2026, 10, 12, 10, 57, 2, tzinfo=timezone.utc)
FH = f"incident:{STOP.isoformat()}"
INC = {"stop_ts": STOP.isoformat(), "character": {"character": "IMMEDIATE"}, "window": {}}

# ── Фальшивая БД: журнал гейта и разборы по машине ───────────────────────────
DB: dict = {}


def reset_db(**kw) -> None:
    DB.clear()
    DB.update({"events": [], "saved": [], "save_fail": 0, "save_calls": 0, "acts": [],
               "analysed": set(), "mark_fail": False})
    DB.update(kw)


async def _state(sn, eq, panel, fh, stop_ts):
    evs = [e for e in DB["events"] if e["fault_hash"] == fh and e["key"] == (sn, eq, panel)]
    term = [e["decision"] for e in evs if e["decision"] in ig.INC_TERMINAL]
    return {"analysed": (sn, eq, panel, fh) in DB["analysed"] or any(
                s["fault_hash"] == fh and s["key"] == (sn, eq, panel) for s in DB["saved"]),
            "ordered": sum(1 for e in evs if e["decision"] == ig.INC_ORDERED),
            "terminal": term[-1] if term else None}


async def _append(sn, eq, panel, event, segment_id=None, ts=None):
    if DB["mark_fail"]:
        return False
    DB["events"].append({**event, "key": (sn, eq, panel)})
    return True


async def _save(sn, eq, panel, analysis_md, fault_hash, alarm_text=None, segment_id=None, ts=None):
    DB["save_calls"] += 1
    if DB["save_fail"] > 0:
        DB["save_fail"] -= 1
        return False
    DB["saved"].append({"fault_hash": fault_hash, "md": analysis_md, "key": (sn, eq, panel),
                        "t": ts})
    return True


async def _seg(seg_id):
    return None


async def _between(*a, **k):
    return []


async def _acts(since):
    return [a for a in DB["acts"] if a["t_start"] >= since]


odb.get_incident_analysis_state = _state
odb.append_segment_gate_event = _append
odb.save_segment_warning = _save
odb.get_segment_by_id = _seg
odb.get_segments_between = _between
odb.get_incident_acts_since = _acts


async def _no_context(*a, **k):
    return {}


ig._context = _no_context      # оборудование и состояние на момент останова — без БД (v5.1.14)

# ── Фальшивая модель ─────────────────────────────────────────────────────────
ASK: dict = {}


def set_answers(*answers) -> None:
    """Каждый элемент: строка — ответ; исключение — бросить; ('sleep', сек) — висеть."""
    ASK.clear()
    ASK.update({"queue": list(answers), "calls": 0})


async def _ask(prompt, meta):
    ASK["calls"] += 1
    a = ASK["queue"].pop(0) if ASK["queue"] else "Разбор"
    if isinstance(a, tuple) and a[0] == "sleep":
        meta["task_id"] = f"gw-task-{ASK['calls']}"   # как _note_gateway_task при приёме
        await asyncio.sleep(a[1])
        return "поздно"
    if isinstance(a, BaseException):
        raise a
    return a


_REAL_ASK = ig._ask
ig._ask = _ask


def run_one(**kw) -> str:
    async def go():
        return await ig._run_incident("SN", "pcc", 1, INC, STOP, 35778)
    return asyncio.run(go())


def decisions() -> list[str]:
    return [e["decision"] for e in DB["events"]]


# 1. Успех с первого раза: отметка «заказан» с ts останова внутри, разбор записан
reset_db(); set_answers("Разбор аварии")
check(run_one() == "ok" and ASK["calls"] == 1, f"1: ok: {ASK}")
check(decisions() == ["ordered"] and DB["events"][0]["ts"] == STOP.isoformat()
      and DB["events"][0]["kind"] == "incident" and DB["events"][0]["attempt"] == 1,
      f"1б: отметка заказа: {DB['events']}")
check(DB["saved"] and DB["saved"][0]["fault_hash"] == FH and DB["saved"][0]["t"] == STOP,
      "1в: разбор записан с ключом и временем останова")

# 2. Уже разобрана — совет не заказываем
reset_db(analysed={("SN", "pcc", 1, FH)}); set_answers("x")
check(run_one() == "analysed" and ASK["calls"] == 0 and decisions() == [],
      "2: разобранная авария не заказывается")

# 3. Срок: нет ответа → один перезаказ → «не получен» с причиной
reset_db(); set_answers(("sleep", 5), ("sleep", 5))
ig.INCIDENT_DEADLINE_SEC = 0.05
out = run_one()
ig.INCIDENT_DEADLINE_SEC = 24 * 3600
check(out == "failed" and ASK["calls"] == 2, f"3: срок → 2 попытки: {out}, {ASK['calls']}")
check(decisions() == ["ordered", "ordered", "failed"] and "нет ответа" in DB["events"][-1]["reason"],
      f"3б: терминальная отметка с причиной: {DB['events']}")
check(DB["events"][-1]["ts"] == STOP.isoformat(), "3в: у терминальной отметки ts останова")
check(DB["events"][-1].get("task_id") == "gw-task-2" and "gw-task-2" in DB["events"][-1]["reason"],
      f"3г: task_id осиротевшего совета — в отметке: {DB['events'][-1]}")
check(DB["events"][1].get("prev_task_id") == "gw-task-1",
      f"3д: перезаказ помнит задачу первой попытки: {DB['events'][1]}")

# 4. Снято оператором шлюза — без повтора
reset_db(); set_answers(client.LLMError(200, "stopped", "снято оператором", task_id="t9",
                                        gateway=True))
check(run_one() == "removed" and ASK["calls"] == 1 and decisions() == ["ordered", "removed"],
      f"4: снятие — без повтора: {decisions()}")
check(DB["events"][-1].get("task_id") == "t9", f"4б: task_id из ошибки шлюза: {DB['events'][-1]}")

# 5. Сбой → один повтор → успех
reset_db(); set_answers(client.LLMError(503, "backend_unavailable", "нет головы", gateway=True),
                        "Разбор со второго раза")
check(run_one() == "ok" and ASK["calls"] == 2 and decisions() == ["ordered", "ordered"],
      f"5: повтор после сбоя: {decisions()}")
check(DB["events"][1]["attempt"] == 2, "5б: номер заказа растёт")

# 6. Пустой ответ — сбой, а не разбор
reset_db(); set_answers(ig.EmptyIncidentAnswer("пусто"), ig.EmptyIncidentAnswer("пусто"))
check(run_one() == "failed" and not DB["saved"], "6: пустой ответ не записан")

# 7. Сегмента нет — повтор только записи, без второго совета
reset_db(save_fail=2); set_answers("Разбор")
check(run_one() == "ok" and ASK["calls"] == 1 and DB["save_calls"] == 3,
      f"7: повтор записи: calls={ASK['calls']} save={DB['save_calls']}")
reset_db(save_fail=99); set_answers("Разбор")
check(run_one() == "save_failed" and ASK["calls"] == 1 and decisions() == ["ordered", "save_failed"],
      f"7б: запись не удалась — отметка, совет один: {decisions()}")

# 8. Исчерпаны заказы (рестарты) — «не получен» без вопроса модели
reset_db(); set_answers("x")
DB["events"] = [{"kind": "incident", "fault_hash": FH, "decision": "ordered", "key": ("SN", "pcc", 1)}
                for _ in range(3)]
check(run_one() == "failed" and ASK["calls"] == 0 and decisions()[-1] == "failed",
      f"8: после трёх заказов — failed: {decisions()}")
reset_db(); set_answers("x")
DB["events"] = [{"kind": "incident", "fault_hash": FH, "decision": "ordered", "key": ("SN", "pcc", 1)}
                for _ in range(2)]
run_one()
check(ASK["calls"] == 1 and DB["events"][-1]["attempt"] == 3, "8б: третий заказ разрешён")
reset_db(); set_answers("x")
DB["events"] = [{"kind": "incident", "fault_hash": FH, "decision": "removed", "key": ("SN", "pcc", 1)}]
check(run_one() == "removed" and ASK["calls"] == 0, "8в: терминальная отметка — не заказываем")


# 9. Дубль заказа отбрасывается; снятие при остановке — без терминальной отметки
async def t_inflight():
    reset_db(); set_answers(("sleep", 30), ("sleep", 30))
    ok1 = ig.order_incident_analysis("SN", "pcc", 1, INC, STOP, 35778)
    ok2 = ig.order_incident_analysis("SN", "pcc", 1, INC, STOP.astimezone(timezone(timedelta(hours=3))))
    other = ig.order_incident_analysis("SN", "pcc", 2, INC, STOP)
    await asyncio.sleep(0.05)
    n_before = ig.inflight_count()
    n = await ig.cancel_all(timeout=2)
    after = ig.order_incident_analysis("SN", "pcc", 3, INC, STOP)
    return ok1, ok2, other, n_before, n, after


ok1, ok2, other, n_before, n, after = asyncio.run(t_inflight())
check(ok1 and not ok2 and other, f"9: дубль того же останова отброшен: {ok1, ok2, other}")
check(n_before == 2 and n == 2 and ig.inflight_count() == 0 and not ig._inflight,
      f"9б: cancel_all снял задачи: {n_before}, {n}")
check(not after, "9в: после остановки новые заказы не принимаются")
check(all(d == "ordered" for d in decisions()),
      f"9г: снятие при остановке — без терминальной отметки: {decisions()}")
ig._closing = False

# 10. Восстановление: 48 ч, разобранные и закрытые пропускаются, не больше 3 за старт
now = datetime.now(timezone.utc)


def act(i, h, panel=1):
    return {"id": 100 + i, "router_sn": f"SN{i}", "equip_type": "pcc", "panel_id": panel,
            "t_start": now - timedelta(hours=h),
            "incident_json": json.dumps({"stop_ts": "x", "window": {}})}


async def t_recover():
    reset_db(); set_answers(*["ok"] * 10)
    DB["acts"] = [act(0, 1), act(1, 2), act(2, 3), act(3, 4), act(4, 5), act(5, 60)]
    h0 = f"incident:{DB['acts'][0]['t_start'].isoformat()}"
    h1 = f"incident:{DB['acts'][1]['t_start'].isoformat()}"
    DB["analysed"].add(("SN0", "pcc", 1, h0))
    DB["events"].append({"kind": "incident", "fault_hash": h1, "decision": "failed",
                         "key": ("SN1", "pcc", 1)})
    n = await ig.recover_pending(delay=0)
    keys = sorted(k[0] for k in ig._inflight)
    await asyncio.gather(*ig._inflight.values())
    return n, keys


n, keys = asyncio.run(t_recover())
check(n == 3 and keys == ["SN2", "SN3", "SN4"], f"10: восстановлено 3, по порядку свежести: {n}, {keys}")
check(sorted(s["key"][0] for s in DB["saved"]) == ["SN2", "SN3", "SN4"],
      "10б: восстановленные разборы записаны")

# 11. Отметка переживает нарезку открытой строки при закрытии
from online.engine import _slice_gate_state  # noqa: E402
mark = {"kind": "incident", "fault_hash": FH, "decision": "failed", "ts": STOP.isoformat()}
sl = _slice_gate_state({"analyses": [], "gate_log": [mark]},
                       STOP - timedelta(minutes=1), STOP + timedelta(hours=1))
check(sl.get("gate_log") == [mark], f"11: отметка уехала в стоп-сегмент: {sl}")
sl2 = _slice_gate_state({"analyses": [], "gate_log": [mark]}, STOP - timedelta(hours=5),
                        STOP - timedelta(minutes=1))
check(not sl2.get("gate_log"), "11б: в сегмент работы до останова не попадает")

# 12. Вопрос модели: строка таблицы, свой промпт без verdict, подпись
seen: list = []


async def _chat(system, user, *, depth=None, meta=None, **k):
    seen.append(("chat", system, depth))
    meta["gorynych"] = {"backend": "smart", "reasoning_level": "xhigh"}   # итог шлюза
    return "## Вероятная причина\nТекст"


async def _claude(prompt, model):
    seen.append(("claude", model))
    return "## Вероятная причина\nТекст Claude"


client.chat = _chat
ig._ask_claude = _claude
ig._ask = _REAL_ASK
router.apply_depth("incident", "smart", "xhigh", "batch", provider="llm")
txt = asyncio.run(ig._ask("АКТ", {}))
check(seen[-1][0] == "chat" and seen[-1][2] == "incident"
      and seen[-1][1] == router.get_prompt("incident") and "verdict" not in seen[-1][1],
      f"12: шлюз — свой промпт аварии, depth=incident: {seen[-1][0]}, {seen[-1][2]}")
check(txt.startswith("## Вероятная причина") and "smart" in txt, f"12б: подпись головы: {txt[-80:]!r}")
router.apply_depth("incident", "smart", "xhigh", "batch", provider="api", claude_model="claude-opus-5-5")
asyncio.run(ig._ask("АКТ", {}))
check(seen[-1] == ("claude", "claude-opus-5-5"), f"12в: Claude — модель строки: {seen[-1]}")
check("дизель-генераторной" in ig.build_incident_prompt(INC) and
      "газопоршнев" not in ig.build_incident_prompt(INC), "12г: тип установки в промпте")

# 13. Снятие сервиса во время повторов записи — текст в журнал сервиса, отметки нет
import logging  # noqa: E402

ig._ask = _ask
logged: list[str] = []


class _H(logging.Handler):
    def emit(self, rec):
        logged.append(rec.getMessage())


ig.logger.addHandler(_H())
ig.INCIDENT_SAVE_RETRY_SEC, ig.INCIDENT_SAVE_WINDOW_SEC = 5, 600


async def t_cancel_save():
    reset_db(save_fail=99); set_answers("ТЕКСТ СОВЕТА")
    ig.order_incident_analysis("SN", "pcc", 1, INC, STOP, 35778)
    await asyncio.sleep(0.1)
    await ig.cancel_all(timeout=2)


asyncio.run(t_cancel_save())
ig._closing = False
check(any("ТЕКСТ СОВЕТА" in m and "остановка сервиса" in m for m in logged),
      "13: текст снятого во время записи разбора — в журнале сервиса")
check(decisions() == ["ordered"], f"13б: без терминальной отметки — восстановление закажет: {decisions()}")


# 14. Запись в строку, удалённую между поиском и UPDATE, — не «успех»
class _Conn:
    def __init__(self, status):
        self.status = status

    async def fetchrow(self, *a):
        return {"id": 1}

    async def execute(self, *a):
        return self.status

    async def close(self):
        pass


import importlib  # noqa: E402
real_db = importlib.reload(odb)
for st, want in (("UPDATE 0", False), ("UPDATE 1", True)):
    async def _c(st=st):
        return _Conn(st)
    real_db._connect = _c
    got1 = asyncio.run(real_db.save_segment_warning("SN", "pcc", 1, "md", FH, segment_id=1, ts=STOP))
    got2 = asyncio.run(real_db.append_segment_gate_event("SN", "pcc", 1, {"x": 1}, segment_id=1, ts=STOP))
    check(got1 is want and got2 is want, f"14: {st} → {want}: {got1}, {got2}")

if _errors:
    print(f"FAIL — {len(_errors)} расхождений:")
    for x in _errors:
        print(f"  • {x}")
    sys.exit(1)
print("Все проверки пройдены.")
