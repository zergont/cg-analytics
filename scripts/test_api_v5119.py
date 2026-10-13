# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""API для UI v4.28.23 (v5.1.19, решения владельца 10.10 и 13.10.2026).

- кто написал заключение: «ai» — модель, «system» — факты без ИИ (плашка
  неудачного пуска v5.1.16); значок ИИ в календаре — только у модели;
- «в очереди» видно сразу при постановке, а не с началом работы воркера;
- признаки попытки пуска для цвета плашки.

Запуск:  py -3 scripts/test_api_v5119.py
"""
from __future__ import annotations

import ast
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analytics.labels import SYSTEM_MODEL, conclusion_by  # noqa: E402

_errors: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        _errors.append(msg)


# 1. Кто написал заключение
check(conclusion_by(SYSTEM_MODEL) == "system" and conclusion_by("Горыныч: q27-5090 (xhigh)") == "ai"
      and conclusion_by("claude-sonnet-5") == "ai" and conclusion_by(None) is None,
      "1: ai / system / нет")

# 2. Воркер пишет плашке ту же модель, что распознаёт API
import corpus.worker as cw  # noqa: E402
from analytics.segmenter import segment  # noqa: E402,F401
fc = cw._attempt_conclusion({"characteristics_json": {
    "attempt": {"from_state": 3, "outcome": "SIMPLE", "t_start": "2026-10-13T07:40:28+00:00",
                "t_fall": "2026-10-13T07:40:37+00:00", "codes": [], "facts": {"rpm_max": 0.0}},
    "subsegments": []}})
check(fc and conclusion_by(fc["claude_model"]) == "system", f"2: плашка — system: {fc and fc['claude_model']}")

# 3. «В очереди» при постановке — не перетирает готовое заключение
import corpus.db as cdb  # noqa: E402
SQL: list[str] = []


class _Conn:
    async def execute(self, sql, *a):
        SQL.append(" ".join(sql.split()))
        return "INSERT 0 1"

    async def close(self):
        pass


async def _connect():
    return _Conn()


cdb._connect = _connect
asyncio.run(cdb.mark_queued(7))
check(SQL and "'queued'" in SQL[0] and "ON CONFLICT (auto_segment_id) DO NOTHING" in SQL[0],
      f"3: вставка без перезаписи: {SQL}")

# 4. Движок ставит «в очереди» при обычной постановке, перезаказ — как раньше
import online.engine as eng  # noqa: E402
import db.analytics as dba  # noqa: E402
CALLS: list = []


class _W:
    def enqueue(self, seg_id, prio, again=False):
        CALLS.append(("enqueue", seg_id, again))


async def _setting(key, default=None):
    return "true"


async def _mq(seg_id):
    CALLS.append(("mark_queued", seg_id))


async def _ss(seg_id, status, error=None):
    CALLS.append(("set_status", seg_id, status))


cw.get_worker = lambda: _W()
dba.get_app_setting = _setting
cdb.mark_queued = _mq
cdb.set_status = _ss


async def _run(again):
    eng._enqueue_segment(11, again=again)
    for _ in range(5):
        await asyncio.sleep(0)

CALLS.clear()
asyncio.run(_run(False))
check(CALLS[:2] == [("mark_queued", 11), ("enqueue", 11, False)], f"4: обычная постановка: {CALLS}")
CALLS.clear()
asyncio.run(_run(True))
check(CALLS[:2] == [("set_status", 11, "queued"), ("enqueue", 11, True)], f"4б: перезаказ: {CALLS}")

# 5. API: признаки попытки и кто написал — в /segments, /segment, /machines
src = (ROOT / "web" / "routes.py").read_text(encoding="utf-8")
tree = ast.parse(src)
fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_attempt_fields")
ns: dict = {}
exec(compile(ast.Module(body=[fn], type_ignores=[]), "routes._attempt_fields", "exec"), ns)
af = ns["_attempt_fields"]
check(af({"attempt": {"outcome": "EMERGENCY", "from_state": 3, "t_fall": "x"}})
      == {"attempt": {"outcome": "EMERGENCY", "from_state": 3}, "attempt_pending": False}
      and af({"attempt_pending": True}) == {"attempt": None, "attempt_pending": True}
      and af(None) == {"attempt": None, "attempt_pending": False}, "5: признаки попытки")
check(src.count("**_attempt_fields(_chars)") == 2 and src.count("**_attempt_fields(_m_chars)") == 1,
      "5б: признаки — в календаре, карточке и /machines")
check('"conclusion_by": conclusion_by(ai_status.get("claude_model"))' in src
      and '"by":            conclusion_by(rec.get("claude_model"))' in src
      and 'conclusion_by(ai_status.get("claude_model")) == "ai"' in src,
      "5в: has_claude — только у модели; conclusion_by / analysis.by в выдаче")
check("claude_model" in (ROOT / "corpus" / "db.py").read_text(encoding="utf-8").split(
      "async def get_analyses_for_segments", 1)[1].split("async def", 1)[0],
      "5г: модель в статусах календаря")

if _errors:
    print(f"FAIL — {len(_errors)} расхождений:")
    for x in _errors:
        print(f"  • {x}")
    sys.exit(1)
print("Все проверки пройдены.")
