# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""Гейт предупреждений: один разбор в работе, без повторных вызовов (v4.9.100).

09.10 совет шлюза думал над разбором 25–40 мин, а отметка «разобрано» ставится
только готовым разбором — планировщик каждые 2 мин слал тот же состав заново
(13 одинаковых советов за 24 мин). Проверяется на подставных БД и разборе:
  1. пока разбор в работе — повторов нет, сколько бы тиков ни прошло;
  2. ответ пришёл после закрытия сегмента (отметка легла в закрытую строку) —
     строка, открытая на момент ответа, состав не повторяет; следующий сегмент
     разбирает его заново, как и раньше;
  3. после ошибки тот же состав — только через паузу, и пауза у каждого состава
     своя (чередование A↔B её не сбивает);
  4. новый состав того же уровня ждёт ответа; другой уровень идёт параллельно;
  5. задачу сняли на шлюзе — повторно не шлём до смены состава;
  6. разбор не вернулся за срок — снят, повторно не шлём;
  7. СТОП/ПУСК (перезапуск движка) сбрасывает память гейта по машине; разбор,
     бывший в работе, по возвращении её не трогает и новую машину не держит;
  8. подпись шлюза без головы (итог совета) — по заказанной модели;
  9. снято оператором — «снято»; шлюз перезапускался (даже исчерпав повторы)
     и прочие ошибки — «ошибка», с паузой и повтором.

Запуск:  py -3 scripts/test_gate_inflight.py
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db.analytics as dba  # noqa: E402
import online.db as odb  # noqa: E402
import online.manager as mgr  # noqa: E402
import online.status_assembler as sa  # noqa: E402

_errors: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        _errors.append(msg)


class Clock:
    t = datetime(2026, 10, 9, 11, 0, tzinfo=timezone.utc)


class FakeDT(datetime):
    @classmethod
    def now(cls, tz=None):
        return Clock.t


mgr.datetime = FakeDT

SEG = {"id": 1, "analyzed": None}
STATE = {"hash": "A", "level": "внимание"}
CALLS: list[str] = []
PENDING: list[tuple[asyncio.Event, dict]] = []


async def _get_app_setting(key, default=None):
    return default


async def _get_open_segment(sn, et, pid):
    return {"id": SEG["id"], "warning_analyzed_hash": SEG["analyzed"]}


async def _noop(*a, **k):
    return True


dba.get_app_setting = _get_app_setting
odb.get_open_segment = _get_open_segment
odb.update_open_segment_status = _noop
sa.build_structural_status = lambda seg, ref, tz: {"severity_level": STATE["level"],
                                                   "panel_severity": "норма"}
sa.compute_fault_hash = lambda s: STATE["hash"]
sa.format_status_text = lambda s: "статус"
sa.extract_alarm_text = lambda s: "тревога"


async def _fake_gate(sn, et, pid, struct, fault_hash):
    """«Совет думает», пока тест не отпустит; исход задаёт тест."""
    CALLS.append(fault_hash)
    ev, res = asyncio.Event(), {"outcome": mgr.GATE_OK, "mark_db": True}
    PENDING.append((ev, res))
    await ev.wait()
    if res["outcome"] == mgr.GATE_OK and res["mark_db"]:
        SEG["analyzed"] = fault_hash
    return res["outcome"]


mgr._analyze_warning_claude = _fake_gate


class Eng:
    router_sn, equip_type, panel_id = "1126246373", "pcc", 1
    last_data_ts = None
    _fault_ref = None
    tz = timezone.utc

    async def stop(self):
        pass


async def tick(m, minutes: float = 1.0, n: int = 1) -> None:
    for _ in range(n):
        Clock.t += timedelta(minutes=minutes)
        await m._tick_status_lines()
        await asyncio.sleep(0)


async def release(outcome=None, mark_db=True, idx=0) -> None:
    ev, res = PENDING.pop(idx)
    if outcome:
        res["outcome"] = outcome
    res["mark_db"] = mark_db
    ev.set()
    for _ in range(5):
        await asyncio.sleep(0)


async def main() -> None:
    m = mgr.OnlineManager()
    m._engines = {"k": Eng()}

    # 1. Долгий разбор: 30 тиков — один вызов
    await tick(m, n=30)
    check(CALLS == ["A"], f"1: пока разбор в работе, повторов нет: {CALLS}")
    # 2. Пока шёл разбор, сегмент сменился: отметка легла в закрытую строку 1,
    # открыта уже строка 2
    SEG["id"] = 2
    await release(mark_db=False)
    check(not m._gate_inflight, "1б: по ответу — не в работе")
    await tick(m, n=5)
    check(CALLS == ["A"], f"2: строка, открытая на момент ответа, состав не повторяет: {CALLS}")
    SEG["id"] = 3                                          # следующий сегмент
    await tick(m, n=3)
    check(CALLS == ["A", "A"], f"2б: следующий сегмент разбирает состав заново: {CALLS}")
    await release()

    # 3. Ошибка → пауза 15 мин
    STATE["hash"] = "B"
    await tick(m, n=3)
    check(CALLS[-1] == "B", f"3: новый состав ушёл: {CALLS}")
    await release(mgr.GATE_FAILED)
    await tick(m, n=10)
    check(CALLS.count("B") == 1, f"3б: после ошибки тот же состав не раньше паузы: {CALLS}")
    await tick(m, n=6)
    check(CALLS.count("B") == 2, f"3в: после паузы — повтор: {CALLS}")
    await release(mgr.GATE_FAILED)                         # B снова упал
    STATE["hash"] = "B2"
    await tick(m, n=3)
    await release(mgr.GATE_FAILED)                         # и B2 упал
    STATE["hash"] = "B"
    await tick(m, n=4)
    check(CALLS.count("B") == 2, f"3г: пауза у каждого состава своя (B2 её не сбил): {CALLS}")
    await tick(m, n=15)
    check(CALLS.count("B") == 3, f"3д: после паузы B уходит: {CALLS}")
    await release()

    # 4. Тот же уровень ждёт; другой уровень — параллельно
    STATE["hash"] = "C"
    await tick(m, n=3)
    STATE["hash"] = "D"
    await tick(m, n=10)
    check(CALLS[-1] == "C" and "D" not in CALLS, f"4: D того же уровня ждёт C: {CALLS}")
    STATE["hash"], STATE["level"] = "E", "предупреждение"
    await tick(m, n=3)
    check(CALLS[-1] == "E", f"4б: другой уровень идёт параллельно: {CALLS}")
    await release(idx=1)                                   # E
    STATE["hash"], STATE["level"] = "D", "внимание"
    await release(idx=0)                                   # C
    await tick(m, n=2)
    check(CALLS[-1] == "D" and CALLS.count("D") == 1, f"4в: после ответа по C — D: {CALLS}")

    # 5. Сняли на шлюзе — повторно не шлём
    await release(mgr.GATE_REMOVED)
    await tick(m, n=40)
    check(CALLS.count("D") == 1, f"5: снятый на шлюзе состав не повторяется: {CALLS}")

    # 6. Не вернулся за срок — снят, не повторяется
    mgr._GATE_TIMEOUT_SEC = 0.05
    STATE["hash"] = "F"
    await tick(m, n=3)
    task = next(iter(m._gate_inflight.values()))[2]
    await asyncio.sleep(0.2)
    PENDING.clear()
    check(task.done() and not m._gate_inflight, "6: разбор снят по сроку")
    await tick(m, n=30)
    check(CALLS.count("F") == 1, f"6б: после снятия по сроку повтора нет: {CALLS}")
    mgr._GATE_TIMEOUT_SEC = 3 * 3600

    # 7. Перезапуск движка (СТОП/ПУСК) посреди разбора
    STATE["hash"] = "G"
    await tick(m, n=3)
    check(CALLS[-1] == "G", f"7: G ушёл: {CALLS}")
    await m._stop_engine("k")
    check("k" not in m._gate_done and "k" not in m._gate_failed and not m._gate_inflight,
          "7б: память гейта и «в работе» сброшены")
    m._engines = {"k": Eng()}
    SEG["analyzed"], SEG["id"] = None, 10
    await release(mark_db=False)                           # старый разбор вернулся
    check("G" not in m._gate_done.get("k", {}), "7в: старый ответ память новой машины не тронул")
    await tick(m, n=3)
    check(CALLS.count("G") == 2, f"7г: перестроенный сегмент разбирает состав: {CALLS}")
    await release()
    STATE["hash"] = "F"
    await tick(m, n=3)
    check(CALLS.count("F") == 2, f"7д: после перезапуска F снова разбирается: {CALLS}")
    await release()

    # 8. Подпись шлюза
    from llm.client import gateway_label
    check(gateway_label({"gorynych": {"task_id": "v-1"}, "requested": {"model": "smart", "reasoning": "xhigh"}},
                        "/home/x.gguf") == "smart (xhigh)", "8: итог совета — по заказанной модели")
    check(gateway_label({"gorynych": {"backend": "q27-5090", "reasoning_level": "none"},
                         "requested": {"model": "fast", "reasoning": "low"}}, "x") == "q27-5090 (none)",
          "8б: голова названа — подпись по голове")
    check(gateway_label({}, "/home/x.gguf") == "/home/x.gguf", "8в: без шлюза — как раньше")

    # 9. Итог по ошибке
    from llm.client import LLMError
    o = mgr._gate_outcome_for_error
    check(o(LLMError(200, "stopped", "x", reason="operator", gateway=True)) == mgr.GATE_REMOVED, "9: оператор — снято")
    check(o(LLMError(409, "council_cancelled", "x", gateway=True)) == mgr.GATE_REMOVED, "9б: совет отменён — снято")
    check(o(LLMError(200, "stopped", "x", reason="shutdown", gateway=True)) == mgr.GATE_FAILED,
          "9в: перезапуск шлюза — ошибка, не снято")
    check(o(LLMError(503, "gateway_stopping", "x", gateway=True)) == mgr.GATE_FAILED, "9г: остановка шлюза — ошибка")
    check(o(LLMError(503, "stopped", "x", reason="operator")) == mgr.GATE_FAILED, "9д: не шлюз — ошибка")
    check(o(RuntimeError("x")) == mgr.GATE_FAILED, "9е: прочее — ошибка")


asyncio.run(main())
if _errors:
    print(f"FAIL — {len(_errors)} расхождений:")
    for e in _errors:
        print(f"  • {e}")
    sys.exit(1)
print("Все проверки пройдены.")
