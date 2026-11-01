"""v5.1.33: месяц календаря API — по операционным суткам, а не по полуночи UTC.

Сининда, сутки 31.10: разгрузка, охлаждение и стоп 01.11 04:06–04:29 МСК и
последний закрытый сегмент суток (04:29–09:00) не попадали в ответ за октябрь
(t_start после 01.11 00:00 UTC) и приходили в ответ за ноябрь с op_day 31.10 —
в UI их не было видно. Помощники берутся из web/routes.py разбором AST
(fastapi в локальном окружении не нужен); проверка эндпоинта — по исходнику.

Запуск: py -3 scripts/test_calendar_month_v5133.py
"""
from __future__ import annotations

import ast
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "web" / "routes.py").read_text(encoding="utf-8")
TREE = ast.parse(SRC)
FAIL: list[str] = []


def check(name: str, cond: bool, info: str = "") -> None:
    print(("OK   " if cond else "FAIL ") + name + (f" — {info}" if info and not cond else ""))
    if not cond:
        FAIL.append(name)


def _func(name: str):
    for n in TREE.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    return None


ns: dict = {}
for fname in ("_op_day", "_calendar_month_bounds"):
    node = _func(fname)
    check(f"0. помощник {fname} есть на уровне модуля", node is not None)
    if node is not None:
        exec(compile(ast.Module(body=[node], type_ignores=[]), "routes", "exec"), ns)

U = timezone.utc
MSK = ZoneInfo("Europe/Moscow")
bounds = ns["_calendar_month_bounds"]
op_day = ns["_op_day"]

# 1. Границы месяца
f, t, d0, d1 = bounds(2026, 10, MSK, 9)
check("1а. октябрь с 01.10 09:00 МСК", f == datetime(2026, 10, 1, 6, tzinfo=U), str(f))
check("1б. октябрь до 01.11 09:00 МСК", t == datetime(2026, 11, 1, 6, tzinfo=U), str(t))
check("1в. op_day октября [2026-10-01, 2026-11-01)", (d0, d1) == ("2026-10-01", "2026-11-01"))
f, t, d0, d1 = bounds(2026, 12, MSK, 9)
check("1г. декабрь — до 01.01 следующего года", t == datetime(2027, 1, 1, 6, tzinfo=U) and d1 == "2027-01-01")
f, t, d0, d1 = bounds(2026, 11, ZoneInfo("UTC"), 0)
check("1д. пояс UTC и рез в полночь — календарный месяц", f == datetime(2026, 11, 1, tzinfo=U)
      and t == datetime(2026, 12, 1, tzinfo=U))

# 2. op_day на стыке — как раскладывает UI
check("2а. 01.11 01:29 UTC (04:29 МСК) — сутки 31.10", op_day(datetime(2026, 11, 1, 1, 29, 7, tzinfo=U), MSK, 9) == "2026-10-31")
check("2б. 01.11 06:00 UTC (09:00 МСК) — сутки 01.11", op_day(datetime(2026, 11, 1, 6, tzinfo=U), MSK, 9) == "2026-11-01")
check("2в. 31.10 06:00 UTC (09:00 МСК) — сутки 31.10", op_day(datetime(2026, 10, 31, 6, tzinfo=U), MSK, 9) == "2026-10-31")
check("2г. без t_start — None", op_day(None, MSK, 9) is None)

# 3. Отбор сегментов Сининды (ДГУ №1, реальные метки) окнами октября и ноября
SEGS = [  # (id, t_start, t_end)
    (36198, datetime(2026, 10, 30, 6, tzinfo=U), datetime(2026, 10, 31, 6, tzinfo=U)),
    (36209, datetime(2026, 10, 31, 6, tzinfo=U), datetime(2026, 11, 1, 1, 6, 58, tzinfo=U)),
    (36211, datetime(2026, 11, 1, 1, 6, 58, tzinfo=U), datetime(2026, 11, 1, 1, 9, 28, tzinfo=U)),
    (36216, datetime(2026, 11, 1, 1, 11, 8, tzinfo=U), datetime(2026, 11, 1, 1, 29, 7, tzinfo=U)),
    (36219, datetime(2026, 11, 1, 1, 29, 7, tzinfo=U), datetime(2026, 11, 1, 6, tzinfo=U)),
    (36218, datetime(2026, 11, 1, 6, tzinfo=U), None),
]


def pick(year: int, month: int) -> list[int]:
    f, t, d0, d1 = bounds(year, month, MSK, 9)
    out = []
    for sid, s, e in SEGS:   # то же пересечение, что get_segments_for_calendar
        if not ((e is None or e >= f) and s <= t):
            continue
        od = op_day(s, MSK, 9)
        if d0 <= od < d1:
            out.append(sid)
    return out


oct_ids, nov_ids = pick(2026, 10), pick(2026, 11)
check("3а. в октябре — остановы и последний закрытый сегмент суток 31.10",
      {36209, 36211, 36216, 36219} <= set(oct_ids), str(oct_ids))
check("3б. в октябре нет сегмента суток 01.11", 36218 not in oct_ids, str(oct_ids))
check("3в. в ноябре нет сегментов суток 31.10", not ({36209, 36211, 36216, 36219} & set(nov_ids)), str(nov_ids))
check("3г. в ноябре — открытый сегмент суток 01.11", nov_ids == [36218], str(nov_ids))

# 4. Эндпоинт пользуется помощниками и отбрасывает чужие сутки
ep = _func("api_machine_segments")
ep_src = ast.get_source_segment(SRC, ep) if ep else ""
check("4а. эндпоинт считает окно через _calendar_month_bounds", "_calendar_month_bounds(" in ep_src)
check("4б. op_day — через _op_day", "_op_day(" in ep_src)
check("4в. сегменты чужих суток пропускаются", "day_from <= op_day < day_to" in ep_src and "continue" in ep_src)
check("4г. полуночи UTC в эндпоинте больше нет", "datetime(year, month, 1, tzinfo=timezone.utc)" not in ep_src)

print(f"\nпровалов: {len(FAIL)}")
sys.exit(1 if FAIL else 0)
