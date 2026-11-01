"""Проба сервера телеметрии (MCP) без Claude Code: список инструментов, дерево
оборудования, затем вызовы по установке или по эталонному кейсу проекта
(docs/plans/design_mcp_telemetry.md, разд. 11).

  python scripts/mcp_probe.py                      — инструменты и дерево
  python scripts/mcp_probe.py --unit "ДЭС №3"      — снимок, лента и ряд за последние часы
  python scripts/mcp_probe.py --case sininda|des3|dgu1
  python scripts/mcp_probe.py --call series --args '{"unit":"ДЭС №3","params":"мощность"}'

Сервер: --url (по умолчанию http://127.0.0.1:8095/mcp), источник — заголовок X-Source.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

from mcp.client.session import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

CASES: dict[str, list[tuple[str, dict]]] = {
    # Сининда 29.09.2026: что было видно у обеих машин за три часа до отключения ДГУ №1 в 13:10
    "sininda": [
        ("equipment_tree", {"unit": "ДГУ №1"}),
        ("timeline", {"unit": "ДГУ №1", "t_from": "29.09.2026 10:00", "t_to": "29.09.2026 13:15", "with_neighbors": True}),
        ("series", {"unit": "ДГУ №1", "params": "ток L1, ток L2, ток L3, мощность", "t_from": "29.09.2026 10:00",
                    "t_to": "29.09.2026 13:15", "points": 60, "with_neighbors": True}),
        ("kb_passport", {"unit": "ДГУ №1", "param": "ток"}),
    ],
    # ДЭС №3 13.10.2026: был ли пуск в 13:11 успешным
    "des3": [
        ("timeline", {"unit": "ДЭС №3", "t_from": "13.10.2026 12:30", "t_to": "13.10.2026 13:30",
                      "kinds": "режим, действия, неисправности"}),
        ("series", {"unit": "ДЭС №3", "params": "обороты, частота, АКБ", "t_from": "13.10.2026 13:05",
                    "t_to": "13.10.2026 13:20", "points": 100}),
        ("state_at", {"unit": "ДЭС №3", "at": "13.10.2026 13:12"}),
        ("segments", {"unit": "ДЭС №3", "t_from": "13.10.2026 12:00", "t_to": "13.10.2026 16:00"}),
    ],
    # ДГУ №1 16.10.2026: серия аварий 1448/234, синхронизатор 30…55 Гц у обеих ДГУ с 15.10
    "dgu1": [
        ("incident_acts", {"unit": "ДГУ №1", "t_from": "16.10.2026 00:00", "t_to": "16.10.2026 23:59"}),
        ("episodes", {"unit": "ДГУ №1", "t_from": "16.10.2026 09:00", "t_to": "16.10.2026 10:30"}),
        ("series", {"unit": "ДГУ №1", "params": "частота, задание частоты, обороты", "t_from": "16.10.2026 09:20",
                    "t_to": "16.10.2026 09:35", "points": 120, "with_neighbors": True}),
        ("trend", {"unit": "ДГУ №1", "params": "частота, мощность", "t_from": "10.10.2026", "t_to": "17.10.2026"}),
        ("fault_code", {"unit": "ДГУ №1", "code": 1448}),
        ("fault_code", {"unit": "ДГУ №1", "code": 234}),
    ],
}


def _text(result) -> str:
    parts = []
    for c in getattr(result, "content", []) or []:
        t = getattr(c, "text", None)
        if t:
            parts.append(t)
    return "\n".join(parts) if parts else str(result)


async def run(url: str, source: str, calls: list[tuple[str, dict]], limit: int, task_id: str) -> int:
    http = create_mcp_http_client(headers={"X-Source": source, "X-Task-Id": task_id})
    failures = 0
    async with streamable_http_client(url, http_client=http) as streams:
        read = getattr(streams, "read_stream", None) or streams[0]
        write = getattr(streams, "write_stream", None) or streams[1]
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print(f"инструментов: {len(tools.tools)} — " + ", ".join(t.name for t in tools.tools))
            for name, args in calls:
                t0 = time.monotonic()
                try:
                    res = await session.call_tool(name, args)
                    text = _text(res)
                    err = bool(getattr(res, "is_error", False)) or text.startswith("ОШИБКА")
                except Exception as e:  # noqa: BLE001
                    text, err = f"исключение: {type(e).__name__}: {e}", True
                dt = time.monotonic() - t0
                failures += int(err)
                head = f"\n=== {name} {json.dumps(args, ensure_ascii=False)} · {dt:.1f} с · {len(text)} симв."
                print(head + (" · ОШИБКА" if err else ""))
                print(text[:limit] + (" …" if len(text) > limit else ""))
    return failures


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8095/mcp")
    ap.add_argument("--source", default="probe")
    ap.add_argument("--unit")
    ap.add_argument("--case", choices=sorted(CASES))
    ap.add_argument("--call")
    ap.add_argument("--args", default="{}")
    ap.add_argument("--limit", type=int, default=3000, help="сколько символов ответа печатать")
    a = ap.parse_args()
    calls: list[tuple[str, dict]] = []
    if a.call:
        calls.append((a.call, json.loads(a.args)))
    elif a.case:
        calls.extend(CASES[a.case])
    elif a.unit:
        calls.extend([
            ("state_at", {"unit": a.unit}),
            ("timeline", {"unit": a.unit}),
            ("series", {"unit": a.unit, "params": "мощность, частота, обороты", "points": 40}),
        ])
    else:
        calls.append(("equipment_tree", {}))
    task = f"probe-{int(time.time())}"
    failures = asyncio.run(run(a.url, a.source, calls, a.limit, task))
    print(f"\nитого: вызовов {len(calls)}, с ошибкой {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
