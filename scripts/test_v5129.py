"""Проверки v5.1.29 «Сжатый вход заключения» (без БД и сети).

- 1. Обычный вход (влезает в предел шлюза) — без сжатия, как раньше (В-29.1).
- 2. Раздутый вход — сжат кодом, ступень 4: шапка целиком, агрегаты по зонам,
  почасовая лента, полные таблицы первого, последнего и подсегментов, где
  впервые появился пункт «Обнаружений», строка на подсегмент; общие замечания —
  один раз; строка «Вход сжат: …» для диспетчера (В-29.3).
- 3. Ступени (В-29.2): не влезает ступень 4 — 3, затем 2; не влезает и 2 —
  самая короткая уходит в цепочку и получает быстрый skip_size.
- 4. Отчёт не совпадает с characteristics_json — сжатия нет.
- 5. Цепочка: шлюзу — сжатый вход; модели, в предел которой полный влезает
  (Claude API, 200 тыс.), — полный; строка сжатия — перед подписью модели.
- 6. Почасовая лента: время режется по часам, все часы подряд, без данных
  отдельно, смена месяца — по времени.
- 7. Полных таблиц не больше _MAX_FULL, даже если новый пункт в каждом
  подсегменте (ревью: старые отчёты — 80 из 80).
- 8. Метка «**Подсегментов:**» в разборах гейта до отчёта шапку не режет.
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

import llm.client as client  # noqa: E402
from corpus import compact, worker  # noqa: E402
from llm import registry  # noqa: E402

_errors: list[str] = []
U = timezone.utc


def check(cond: bool, msg: str) -> None:
    if not cond:
        _errors.append(msg)


def run(coro):
    return asyncio.run(coro)


def seg_row(n: int, block_chars: int = 3000, start=datetime(2026, 10, 28, 6, 0, tzinfo=U),
            drop_block: bool = False, panel_each: bool = False, na_first: bool = False,
            gate_md: str = "") -> dict:
    """Сегмент «Работа» из n подсегментов по 3 мин, чередование зон.
    Замечания: Общая авария во всех, близость масла — с 3-го, I₂ — в 40-м;
    panel_each — у каждого подсегмента свой фронт панели (новый пункт свёртки)."""
    subs, blocks = [], []
    t = start
    for k in range(1, n + 1):
        zone = "NORMAL" if k % 2 else "LOW"
        dur = 180.0
        gaps = []
        if na_first and k == 1:
            zone, dur = "NA", 3 * 3600.0
            gaps = [{"start": t.isoformat(), "end": (t + timedelta(seconds=dur)).isoformat(), "duration_sec": dur}]
        dets = [{"scenario": "CONTROLLER_FAULT", "t_detected": "2026-10-24T13:00:53+00:00",
                 "values": {"fault_name": "Общая авария", "addr": 40408, "bit": 11,
                            "fault_start": ("2026-10-28T%02d:%02d:00+00:00" % divmod(k, 60)) if panel_each
                            else "2026-10-24T13:00:53+00:00"},
                 "trigger": "Общая авария"}]
        if k >= 3:
            dets.append({"scenario": "LIMIT_PROXIMITY", "t_detected": t.isoformat(),
                         "description_key": "LIMIT_PROXIMITY.oil_temp_near_limit",
                         "trigger": "Температура масла: 100.00 в пределах 10% от порога 110.0"})
        if k == 40:
            dets.append({"scenario": "NEGATIVE_SEQUENCE", "t_detected": (t + timedelta(seconds=30)).isoformat(),
                         "trigger": "I2"})
        subs.append({"id": f"x.{k}", "t_start": t.isoformat(), "t_end": (t + timedelta(seconds=dur)).isoformat(),
                     "duration_sec": dur, "load_zone": zone, "detections": dets, "data_gaps": gaps,
                     "characteristics": {} if zone == "NA" else {
                         "LOAD_PCT": {"unit": "%", "median": 32.0 if zone == "NORMAL" else 24.0,
                                      "min": 20.0, "max": 45.0},
                         "OIL_TEMP": {"unit": "°C", "median": 99.0, "min": 97.0, "max": 101.0},
                         "ECM_LINK_STATE": {"unit": "", "median": 1.0, "min": 1.0, "max": 1.0},
                         "ENGINE_HOURS": {"unit": "h", "median": 15650.0 + k, "min": 15650.0, "max": 15700.0},
                         "RATING_KW": {"unit": "kW", "median": 440.0, "min": 440.0, "max": 440.0},
                         "HEARTBEAT": {"unit": "", "median": 1e8, "min": 1e8, "max": 1e8 + 10},
                     }})
        if not (drop_block and k == n):
            blocks.append(f"#### Подсегмент 1.{k} — зона\n\n| | |\n|-|-|\n" + "x" * block_chars + "\n\n")
        t += timedelta(seconds=dur)
    report = ("# Отчёт\n\n## Сводка\n\nОбнаружения: близость масла, Общая авария\n\n## Сегменты\n\n"
              "### Сегмент 1 — Работа\n\n" + (f"**Подсегментов:** {n}\n\n" if n > 1 else "") + "".join(blocks))
    row = {"id": 900000 + n, "run_state": 3, "report_md": report,
           "characteristics_json": json.dumps({"subsegments": subs})}
    if gate_md:
        row["warning_analysis_md"] = gate_md
    return row


SYS = "S" * 6000


def blocks_in(txt: str) -> list[str]:
    return [ln.split(" — ")[0].replace("#### Подсегмент ", "") for ln in txt.splitlines()
            if ln.startswith("#### Подсегмент ")]


# ══ 1. Обычный вход — без сжатия ═════════════════════════════════════════════
r10 = seg_row(10)
check(worker._est_prompt_tokens(SYS, worker._model_input(r10)) <= worker._GW_MAX_PROMPT_TOKENS,
      "1: маленький вход влезает — сжимать нечего")

# ══ 2. Раздутый вход — ступень 4 ═════════════════════════════════════════════
r141 = seg_row(141)
full = worker._model_input(r141)
est = worker._est_prompt_tokens(SYS, full)
check(est > worker._GW_MAX_PROMPT_TOKENS, f"2а: 141 подсегмент по 3 тыс. — не влезает: {est}")
txt, est2, info = worker._compact_model_input(r141, SYS, full)
check(info["level"] == 4 and info["subsegments"] == 141 and est2 <= worker._GW_MAX_PROMPT_TOKENS
      and len(txt) < len(full) / 5, f"2б: сжат до ступени 4 и влезает: {info} {est2}")
check(txt.startswith(full[:full.find("**Подсегментов:**")]), "2в: шапка (вердикт, сводка) — целиком")
_lines = txt.split("**Все подсегменты по строке:**")[1]
check("Во всех или почти во всех подсегментах: Общая авария, Приближение к уставке" in txt
      and "Общая авария" not in _lines and "Температура масла" not in _lines
      and [ln.split(" | ")[0] for ln in _lines.splitlines() if "Ток обратной" in ln or "NEGATIVE" in ln]
      == ["| 1.40"],
      "2г: общие замечания (≥80 % подсегментов) — один раз; в строках — только редкое (I₂ в 1.40)")
check(blocks_in(txt) == ["1.1", "1.3", "1.40", "1.141"],
      f"2д: полностью — первый, первое появление пункта (1.3 масло, 1.40 I₂), последний: {blocks_in(txt)}")
_agg = txt.split("**Подсегментов:**")[1].split("**Почасовая лента")[0]
check("Наработка" not in _agg and "RATING_KW" not in _agg and "| Нагрузка |" in _agg
      and "| Температура масла |" in _agg and "ECM_LINK_STATE" in _agg,
      "2е: в агрегатах нет счётчиков и паспорта; постоянный измеряемый показатель — есть")
check("Малая нагрузка: 70 подсегментов" in txt and "Нормальная нагрузка: 71 подсегмент," in txt
      and "; Малая нагрузка" in txt, "2ж: зоны с числом подсегментов, через «;»")
check(compact.compact_note(info) == "*Вход сжат: 141 подсегмент сведён по зонам нагрузки; полные таблицы ИИ "
      "видел только у первого, последнего и там, где появились замечания.*",
      f"2з: строка для диспетчера: {compact.compact_note(info)}")

# ══ 3. Ступени ═══════════════════════════════════════════════════════════════
_lv = {lv: len(compact.compact_input(full, r141, lv)) for lv in (4, 3, 2)}
check(_lv[4] > _lv[3] > _lv[2], f"3а: ступени убывают: {_lv}")
_saved = worker._GW_MAX_PROMPT_TOKENS
try:
    worker._GW_MAX_PROMPT_TOKENS = worker._est_prompt_tokens(SYS, "x" * (_lv[3] + 10))
    i3 = worker._compact_model_input(r141, SYS, full)[2]
    worker._GW_MAX_PROMPT_TOKENS = worker._est_prompt_tokens(SYS, "x" * (_lv[2] + 10))
    i2 = worker._compact_model_input(r141, SYS, full)[2]
    worker._GW_MAX_PROMPT_TOKENS = 100
    _, e0, i0 = worker._compact_model_input(r141, SYS, full)
finally:
    worker._GW_MAX_PROMPT_TOKENS = _saved
check(i3["level"] == 3 and i2["level"] == 2 and i0["level"] == 2 and e0 > 100,
      f"3б: не влезла 4 — 3, затем 2; не влезла и 2 — она же, дальше skip_size: {i3} {i2} {i0}")
check("таблицы подсегментов ИИ не видел" in compact.compact_note(i2), "3в: строка диспетчеру на ступени 2")

# ══ 4. Отчёт не совпадает с подсегментами — без сжатия ═══════════════════════
rbad = seg_row(141, drop_block=True)
check(worker._compact_model_input(rbad, SYS, worker._model_input(rbad)) is None,
      "4: блоков меньше, чем подсегментов, — сжатия нет")

# ══ 5. Цепочка ═══════════════════════════════════════════════════════════════
GW = registry.normalize_entry({"id": "gw", "name": "Горыныч", "provider": "gorynych", "model": "fast"})
API = {"id": "api", "name": "Claude API", "type": "api", "provider": "anthropic", "model": "m",
       "max_ctx_tokens": 200000, "_route": True}
registry.load_registry(json.dumps([GW]))
_sent: list[str] = []
_mode = {"fail": False}


def handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    _sent.append(body["messages"][-1]["content"])
    if _mode["fail"]:
        return httpx.Response(200, content=("data: " + json.dumps(
            {"error": {"message": "нет головы", "code": "backend_unavailable", "task_id": "t-9"}}) + "\n\n").encode(),
            headers={"content-type": "text/event-stream"})
    lines = ["data: " + json.dumps({"choices": [{"delta": {"content": "## Сводка\nзаключение\n\n| a | b |\n|---|---|\n| 1 | 2 |"}}]}),
             "data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}],
                                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}),
             "data: [DONE]"]
    return httpx.Response(200, content=("\n\n".join(lines) + "\n\n").encode(),
                          headers={"content-type": "text/event-stream", "x-gorynych-task-id": "v-1"})

client._TRANSPORT = httpx.MockTransport(handler)
res = run(worker._analyse_segment_chain(r141, None, SYS, [GW]))
check(res["debug_json"]["routing"] == [{"entry": "gw", "action": "ok"}] and _sent == [txt],
      f"5а: шлюз получил сжатый вход: {res['debug_json']['routing']}")
cm = res["conclusion_md"]
_note = compact.compact_note(info)
_sig = cm.rfind("\n---\n*Модель:")
check(_note in cm and res["debug_json"]["input_compact"]["level"] == 4
      and (_sig < 0 or cm.find(_note) < _sig) and f"| 1 | 2 |\n\n{_note}" in cm,
      f"5б: строка сжатия — отдельным абзацем, перед подписью модели: {cm[-260:]!r}")

_got: dict = {}


async def _fake_analyse(segment_row, kb_path, system_prompt=None, model=None, user_input=None):
    _got["user_input"] = user_input
    return {"conclusion_md": "## Сводка\napi", "error": None, "debug_json": {}}

_orig = sys.modules.get("corpus.agent")
sys.modules["corpus.agent"] = types.SimpleNamespace(analyse_segment=_fake_analyse)
try:
    # Шлюз отказал — Claude API: полный вход влезает в его 200 тыс., сжатия нет
    _mode["fail"] = True
    _sent.clear()
    r170 = seg_row(170, block_chars=1300)
    _est170 = worker._est_prompt_tokens(SYS, worker._model_input(r170))
    res2 = run(worker._analyse_segment_chain(r170, None, SYS, [GW, API]))
    _api_in = _got.get("user_input", "нет вызова")
    # Полный не влезает и в Claude — ему тоже сжатый
    _got.clear()
    res3 = run(worker._analyse_segment_chain(seg_row(260, block_chars=2000), None, SYS, [API]))
finally:
    _mode["fail"] = False
    client._TRANSPORT = None
    if _orig is None:
        sys.modules.pop("corpus.agent", None)
    else:
        sys.modules["corpus.agent"] = _orig
check(worker._GW_MAX_PROMPT_TOKENS < _est170 <= 200000, f"5в: подбор примера 150–200 тыс.: {_est170}")
check(len(_sent) == 1 and len(_sent[0]) < 60000 and _api_in is None
      and _note.split(":")[0] not in res2["conclusion_md"]
      and [x["action"] for x in res2["debug_json"]["routing"]] == ["error", "ok"],
      f"5г: шлюзу — сжатый, Claude API — полный (собирает сам), без строки сжатия: {res2['debug_json']['routing']}")
check(isinstance(_got.get("user_input"), str) and "Вход сжат" in res3["conclusion_md"],
      "5д: полный не влезает и в Claude API — ему сжатый, со строкой для диспетчера")

# ══ 6. Почасовая лента ═══════════════════════════════════════════════════════
rm = seg_row(141, start=datetime(2026, 10, 31, 19, 0, tzinfo=U), na_first=True)
tm = compact.compact_input(worker._model_input(rm), rm, 2)
_rows = [ln for ln in tm.split("**Почасовая лента")[1].splitlines() if ln.startswith("| ") and ":00 |" in ln]
_hours = [ln.split(" | ")[0].lstrip("| ") for ln in _rows]
check(_hours[:4] == ["31.10 22:00", "31.10 23:00", "01.11 00:00", "01.11 01:00"],
      f"6а: все часы подряд через смену месяца: {_hours[:5]}")
check(_rows[1].endswith("| 0 | 0 % | 100 % |") and _rows[0].split(" | ")[1] == "1",
      f"6б: час без начала подсегмента есть, «без данных» по зоне Н/Д: {_rows[:2]}")
check("Данных нет (без связи)." in tm, "6в: зона Н/Д — «данных нет», а не пустая таблица")
_t4 = compact.compact_input(worker._model_input(rm), rm, 4)
check("⚠ без связи 3 ч 00 мин" in _t4.split("**Все подсегменты по строке:**")[1],
      "6г: в строке подсегмента — сколько без связи")

# ══ 7. Предел полных таблиц ══════════════════════════════════════════════════
rp = seg_row(80, panel_each=True)
tp = compact.compact_input(worker._model_input(rp), rp, 4)
check(len(blocks_in(tp)) == compact._MAX_FULL and blocks_in(tp)[0] == "1.1" and blocks_in(tp)[-1] == "1.80"
      and "ещё 72 подсегмента с новыми замечаниями" in tp,
      f"7: новый пункт в каждом подсегменте — полностью не больше {compact._MAX_FULL}: {blocks_in(tp)}")

# ══ 8. Метка в разборах гейта не режет шапку ═════════════════════════════════
rg = seg_row(141, gate_md="Разбор гейта: **Подсегментов:** 2 (из ответа модели)")
fg = worker._model_input(rg)
tg = compact.compact_input(fg, rg, 2)
check(fg.find("**Подсегментов:**") < fg.find("## Сводка") and "## Сводка" in tg
      and tg.startswith(fg[:fg.find("## Сводка")]),
      "8: метка в разборе гейта до отчёта — шапка и Сводка целиком")

if _errors:
    print(f"FAIL — {len(_errors)} расхождений:")
    for x in _errors:
        print(f"  • {x}")
    sys.exit(1)
print("Все проверки пройдены.")
