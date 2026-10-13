# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""Закрытие окна без тихих потерь записей гейта (v5.1.13, вариант А).

Раньше закрытие читало открытую строку и отдельным запросом удаляло её: разбор,
записанный между шагами, получал «UPDATE 1» и пропадал со строкой. Теперь снимок
и удаление — один запрос (DELETE … RETURNING), «не записано» — у всех писателей.

Запуск:  py -3 scripts/test_close_race_v5113.py
"""
from __future__ import annotations

import asyncio
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from online.status_assembler import GATE_RULES_VERSION as RULES  # noqa: E402

import online.db as odb  # noqa: E402

_errors: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        _errors.append(msg)


T = datetime(2026, 10, 13, 6, 0, tzinfo=timezone.utc)


class Conn:
    def __init__(self, row=None, status="UPDATE 1"):
        self.row, self.status, self.sql = row, status, []

    async def fetchrow(self, sql, *a):
        self.sql.append(sql)
        return self.row

    async def execute(self, sql, *a):
        self.sql.append(sql)
        return self.status

    async def close(self):
        pass


def use(conn):
    async def _c():
        return conn
    odb._connect = _c


# 1. Снимок и удаление — один запрос
c = Conn(row={"id": 7, "gate_log": "[]", "t_end": None})
use(c)
got = asyncio.run(odb.pop_open_segment("SN", "pcc", 1))
sql = " ".join(c.sql[0].split())
check(got == {"id": 7, "gate_log": "[]", "t_end": None}, f"1: снимок строки: {got}")
check(len(c.sql) == 1 and sql.startswith("DELETE FROM auto_segments") and "RETURNING *" in sql
      and "t_end IS NULL" in sql, f"1б: один запрос DELETE … RETURNING: {sql}")
use(Conn(row=None))
check(asyncio.run(odb.pop_open_segment("SN", "pcc", 1)) is None, "1в: строки нет — None")

# 2. Вердикт «снять» в удалённую строку — «не записано»
for st, want in (("UPDATE 0", False), ("UPDATE 1", True)):
    use(Conn(row={"id": 1}, status=st))
    got = asyncio.run(odb.set_segment_gate_suppression("SN", "pcc", 1, "h", segment_id=1, ts=T))
    check(got is want, f"2: {st} → {want}: {got}")

# 3. В движке не осталось шаблона «прочитать, потом удалить»
src = (ROOT / "online" / "engine.py").read_text(encoding="utf-8")
check("delete_open_segment" not in src and src.count("online_db.pop_open_segment(") == 2,
      "3: оба пути закрытия снимают строку одним запросом")
check(not re.search(r"def delete_open_segment", (ROOT / "online" / "db.py").read_text(encoding="utf-8")),
      "3б: старой функции удаления без снимка нет")

# 4. Гейт: снятие не записалось ни в одну строку — не применено, эпизоды не помечены
import online.manager as mgr  # noqa: E402
import online.status_assembler as sa  # noqa: E402
import corpus.settings as cs  # noqa: E402
import llm.router as router  # noqa: E402

LP = {"scenario": "LIMIT_PROXIMITY", "severity": "CAUTION", "fault_codes": [], "ref_codes": [214],
      "related_roles": ["OIL_TEMP"], "trigger": "Температура масла: 100 в пределах 10% от порога 110",
      "values": {}}
STRUCT = sa.build_structural_status({"id": 1, "run_state": 3, "t_start": T, "characteristics_json": {},
                                     "current_values_json": {"values": {}}, "active_detections_json": [LP]})
LOG: dict = {}


def reset(supp_ok: bool) -> None:
    LOG.clear()
    LOG.update({"events": [], "saved": [], "marked": [], "supp_ok": supp_ok})


async def _open(*a):
    return {"id": 1, "t_start": T, "warning_analyses": []}


async def _none(*a, **k):
    return None


async def _empty(*a, **k):
    return []


async def _counts(*a, **k):
    return {}


async def _supp(*a, **k):
    return LOG["supp_ok"]


async def _event(*a, event=None, **k):
    LOG["events"].append(event)
    return True


async def _save(*a, analysis_md=None, extra=None, **k):
    LOG["saved"].append((analysis_md, extra))
    return True


async def _mark(r, e, p, scenarios):
    LOG["marked"].append(list(scenarios))


async def _late(*a, **k):
    return False, False


async def _llm(prompt, model, depth=None, meta=None):
    return "Анализ: ложное срабатывание датчика.", "cancel", "данные", 10, 5


odb.get_open_segment = _open
odb.get_run_state_origin_ts = _none
odb.count_episodes_batch = _counts
odb.get_open_episodes = _empty
odb.get_episodes_overlapping = _empty
odb.set_segment_gate_suppression = _supp
odb.append_segment_gate_event = _event
odb.save_segment_warning = _save
odb.set_episodes_gate_suppressed = _mark
odb.resolve_segment_id = _none
odb.apply_late_gate_suppression = _late
mgr._run_warning_gate_llm = _llm
mgr._GATE_SAVE_RETRY_SEC = 0
cs.get_claude_settings = lambda: {"model": "claude-sonnet-5-5", "max_tokens": 1000, "proxy": ""}
router.apply_depth("gate_caution", "fast", "low", "interactive", provider="llm")

reset(supp_ok=False)
out = asyncio.run(mgr._analyze_warning_claude("SN", "pcc", 1, dict(STRUCT), "h1"))
ev = LOG["events"][-1]
md, extra = LOG["saved"][-1]
check(out == mgr.GATE_FAILED and ev["decision"] == "cancel" and ev["decision_applied"] is False
      and ev["rejected_by"] == ["not_written"], f"4: не записано — не применено, повтор: {out}, {ev}")
check(not LOG["marked"] and extra["applied"] is False and "снятие не записано" in md,
      f"4б: эпизоды не помечены, разбор не врёт: {LOG['marked']}, {md[:200]}")

reset(supp_ok=True)
out = asyncio.run(mgr._analyze_warning_claude("SN", "pcc", 1, dict(STRUCT), "h2"))
ev = LOG["events"][-1]
check(out == mgr.GATE_OK and ev["decision_applied"] is True and LOG["marked"] == [["LIMIT_PROXIMITY"]]
      and "применён (на момент разбора)" in LOG["saved"][-1][0], f"4в: записано — применено: {out}, {ev}")

# 4г. Разбор с незаписанным снятием — не «разобран»: состав спросят снова после паузы
check(mgr._analyzed_hashes({"warning_analyses": [
    {"fault_hash": "h1", "rules": RULES, "applied": False, "not_written": True},
    {"fault_hash": "h2", "rules": RULES}]}) == {"h2"}, "4г: not_written не считается разобранным")
reset(supp_ok=False)
asyncio.run(mgr._analyze_warning_claude("SN", "pcc", 1, dict(STRUCT), "h3"))
check(LOG["saved"][-1][1].get("not_written") is True, "4д: отметка not_written в записи разбора")

# 5. Перенос в новую строку: отметка «перенесено» — только если журнал ляжет не в неё
CALLS = {"n": 0}


async def _open_changing(*a):
    CALLS["n"] += 1
    return {"id": 1 if CALLS["n"] == 1 else 2, "t_start": T, "warning_analyses": []}


odb.get_open_segment = _open_changing
for landed, want_carried in ((2, 0), (1, 1)):
    async def _resolve(*a, landed=landed):
        return landed
    odb.resolve_segment_id = _resolve
    CALLS["n"] = 0
    reset(supp_ok=True)
    asyncio.run(mgr._analyze_warning_claude("SN", "pcc", 1, dict(STRUCT), "h4"))
    carried = [e for e in LOG["events"] if e.get("provider") == "carried"]
    check(len(carried) == want_carried,
          f"5: журнал ляжет в строку {landed} — «перенесено» {want_carried}: {len(carried)}")

if _errors:
    print(f"FAIL — {len(_errors)} расхождений:")
    for x in _errors:
        print(f"  • {x}")
    sys.exit(1)
print("Все проверки пройдены.")
