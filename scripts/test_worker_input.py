# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""Вход заключения — один для всех провайдеров (v5.1.2).

До v5.1.2 ветка llm цепочки и одиночная LLM слали модели голый report_md:
шапка единого вердикта, разборы гейта и хронология доходили только до
claude-api. В корпус писалось verdict='LLM', alarm_level=NULL.

Запуск:  py -3 scripts/test_worker_input.py
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import corpus.preprocessor as prep  # noqa: E402
import corpus.worker as worker  # noqa: E402
import llm.client as client  # noqa: E402
from analytics.serializer import build_summary_parts  # noqa: E402

_errors: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        _errors.append(msg)


T0 = datetime(2026, 10, 10, 6, 0, tzinfo=timezone.utc)
seg = NS(run_state=3, stop_kind=None, cause_close="DAILY_BOUNDARY", sequence_checks=[],
         t_start=T0.isoformat(), t_end=(T0 + timedelta(hours=24)).isoformat(),
         subsegments=[NS(characteristics={}, risk_accumulators=NS(coking_risk=NS(risk_level="GREEN")))])
eps = [{"source": "analytics", "severity": "CAUTION", "scenario": "OIL_DILUTION", "gate_suppressed": True,
        "t_open": T0 + timedelta(hours=1), "t_close": None, "active_sec": 3600.0, "blind_sec": 0.0},
       {"source": "analytics", "severity": "CAUTION", "scenario": "COOLING_FAILURE", "gate_suppressed": False,
        "t_open": T0 + timedelta(hours=2), "t_close": T0 + timedelta(hours=3), "active_sec": 3600.0,
        "blind_sec": 0.0}]
ROW = {"id": 35765, "run_state": 3, "report_md": "# Аналитический отчёт\nТЕЛО ОТЧЁТА",
       "characteristics_json": {}, "report_summary_json": build_summary_parts([seg], eps),
       "warning_analyses": [{"t": T0.isoformat(), "fault_hash": "b73f", "alarm_text": "Давление масла",
                             "md": "Ложное срабатывание: 248 кПа штатно."}]}

seen: list[str] = []


async def _chat(system_prompt, user, **k):
    seen.append(user)
    return "## Коротко\nОтвет"


client.chat = _chat

# 1. Цепочка, запись шлюза (type=llm)
entry = {"id": "model", "name": "Горыныч", "type": "llm", "provider": "gorynych", "model": "fast",
         "max_ctx_tokens": 100000, "stream": False}
res = asyncio.run(worker._analyse_segment_chain(ROW, None, "СИСТЕМНЫЙ", [entry], depth="seg_caution"))
inp = seen[-1]
check(inp.startswith("[ВЕРДИКТ БЛОКА АНАЛИТИКИ — НЕ ПЕРЕСМАТРИВАТЬ]") and inp.endswith("ТЕЛО ОТЧЁТА"),
      f"1: шлюз получает шапку + отчёт: {inp[:80]!r}…{inp[-20:]!r}")
check("Ложное срабатывание" in inp, "1б: разборы гейта во входе")
check("снято ИИ" in inp and "Уровень тревоги: CAUTION" in inp, f"1в: шапка из частей сводки: {inp[:400]!r}")
check(res["verdict"] == "требует внимания" and res["alarm_level"] == "CAUTION",
      f"1г: вердикт в корпус, не «LLM»: {res['verdict']}/{res['alarm_level']}")
dbg = res["debug_json"]
check(dbg["input_length"] == len(inp) and dbg["est_prompt_tokens"] == int((len("СИСТЕМНЫЙ") + len(inp)) / 1.7) + 4000,
      f"1д: размер — по полному входу: {dbg}")

# 2. Одиночная LLM
res = asyncio.run(worker._analyse_segment_llm(ROW, "СИСТЕМНЫЙ", depth="seg_caution"))
check(seen[-1] == inp and res["alarm_level"] == "CAUTION", "2: одиночная LLM — тот же вход и вердикт")

# 3. Вход не собрался — отчёт, а не падение
_orig = prep.build_claude_input


def _boom(row):
    raise ValueError("битые части")


prep.build_claude_input = _boom
asyncio.run(worker._analyse_segment_chain(ROW, None, "СИСТЕМНЫЙ", [entry]))
check(seen[-1] == ROW["report_md"], "3: запасной вход — отчёт")
prep.build_claude_input = _orig

if _errors:
    print(f"FAIL — {len(_errors)} расхождений:")
    for x in _errors:
        print(f"  • {x}")
    sys.exit(1)
print("Все проверки пройдены.")
