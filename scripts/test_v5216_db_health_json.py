"""v5.2.16: /api/db/health отдаёт JSON, а не 500.

Повод — аудит закрытия сегментов 12–19.11.2026: в журнале cg-analytics ответ
/api/db/health — 500 «Object of type datetime is not JSON serializable»
(sync_state[].last_sync_at — TIMESTAMPTZ, asyncpg отдаёт datetime). Страница /db
раз в 15 с вызывает этот адрес, поэтому её цифры не обновлялись после открытия.

Раздел 1 — маршрут со статистикой, в которой есть datetime: ответ 200, дата —
ISO 8601 с поясом, остальные поля без изменений.
Раздел 2 — страница /db разбирает дату через new Date(...); VERSION.

Запуск:  .venv/Scripts/python.exe scripts/test_v5216_db_health_json.py
(без fastapi в окружении раздел 1 пропускается с пометкой)
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_errors: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        _errors.append(msg)


# ══ 1. Маршрут ══════════════════════════════════════════════════════════════
_STATS = {
    "db_size_bytes": 123456789,
    "db_size_pretty": "117.7 МБ",
    "tables": [{"name": "history", "size_bytes": 1000, "size_pretty": "1000 bytes", "row_count": 10}],
    "sync_state": [{"router_sn": "R1", "equip_type": "pcc3300", "panel_id": 1,
                    "last_sync_at": datetime(2026, 11, 19, 6, 0, 0, tzinfo=timezone.utc),
                    "lag_sec": 12, "device_name": "ДГУ №1"}],
    "daily_rows": 4321,
    "daily_mb": 1.5,
    "monthly_mb": 45,
}

try:
    import fastapi  # noqa: F401
    _have_fastapi = True
except ImportError:
    _have_fastapi = False
    print("раздел 1 пропущен: в окружении нет fastapi (запускать из .venv)")

if _have_fastapi:
    import db.analytics as dba  # noqa: E402
    import web.routes as routes  # noqa: E402
    from fastapi.responses import JSONResponse  # noqa: E402

    # 1а. Старый путь падал — проверка, что тест ловит именно этот дефект
    try:
        JSONResponse(dict(_STATS))
        check(False, "1а: JSONResponse без кодировщика не упал на datetime — тест не воспроизводит дефект")
    except TypeError:
        pass

    async def _fake_stats() -> dict:
        return {**_STATS, "sync_state": [dict(s) for s in _STATS["sync_state"]]}

    _orig = dba.get_db_health_stats
    dba.get_db_health_stats = _fake_stats
    try:
        resp = asyncio.run(routes.api_db_health())
    except Exception as exc:  # noqa: BLE001
        resp = None
        check(False, f"1б: маршрут упал: {exc!r}")
    finally:
        dba.get_db_health_stats = _orig

    if resp is not None:
        check(resp.status_code == 200, f"1б: статус {resp.status_code}, ждали 200")
        body = json.loads(resp.body)
        s = (body.get("sync_state") or [{}])[0]
        check(s.get("last_sync_at") == "2026-11-19T06:00:00+00:00",
              f"1в: last_sync_at {s.get('last_sync_at')!r}, ждали ISO 8601 с поясом")
        check(s.get("lag_sec") == 12 and s.get("device_name") == "ДГУ №1",
              "1г: поля строки синхронизации не изменились")
        check(body.get("daily_rows") == 4321 and body.get("daily_mb") == 1.5
              and body.get("monthly_mb") == 45 and body.get("db_size_pretty") == "117.7 МБ",
              "1д: сводные поля не изменились")
        check(body.get("source_mode") in ("external", "local"), "1е: source_mode в ответе")

# ══ 2. Страница и версия ════════════════════════════════════════════════════
_tpl = (ROOT / "web" / "templates" / "db_health.html").read_text(encoding="utf-8")
check('fetch("/api/db/health")' in _tpl and "new Date(s.last_sync_at)" in _tpl,
      "2а: страница /db берёт /api/db/health и разбирает дату через new Date")
_rt = (ROOT / "web" / "routes.py").read_text(encoding="utf-8")
check("return JSONResponse(jsonable_encoder(stats))" in _rt, "2б: маршрут отдаёт через jsonable_encoder")
check(tuple(int(x) for x in (ROOT / "VERSION").read_text(encoding="utf-8").strip().split(".")) >= (5, 2, 16),
      "2в: VERSION не ниже 5.2.16")

if _errors:
    print(f"FAIL — {len(_errors)} расхождений:")
    for x in _errors:
        print(f"  • {x}")
    sys.exit(1)
print("Все проверки пройдены.")
