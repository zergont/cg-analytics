"""v5.2.15: подпись попытки пуска без кода и аварии — «Прерванный пуск · Стоп»
(решение владельца 16.11.2026, вариант «а»).

Повод — ДГУ №1 05.11.2026 10:15–10:21 МСК: три попытки прокрутили (11, 71 и
21 с, до 188–191 об/мин) и прервали ключом и командой, без кода панели; в
календаре — «Неудачный пуск · Стоп» рядом с вердиктом «🟢 НОРМА». Четвёртая
попытка (10:18:38) — код 359 «Отказ пуска», аварийный останов: она «Неудачный
пуск · Авария». Ключ ушёл в «Выкл» и на ней (10:19:58, в одном кадре с кодом),
поэтому подпись решает не ключ, а код и авария.

Раздел 1 — подписи четырёх попыток 05.11 по записям в БД.
Раздел 2 — правило: код или авария — «Неудачный»; без них нет прокрутки —
«Отменённый»; прокрутка была или не видна (решение А) — «Прерванный».
Раздел 3 — потребители: календарь (label_from_chars), заключение-факты,
строка отчёта, сервер телеметрии, версии входа, KB в обоих слоях.

Запуск:  py -3 scripts/test_v5215_attempt_label.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analytics.labels import attempt_label, label_from_chars  # noqa: E402

_errors: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        _errors.append(msg)


def att(**kw):
    base = {"from_state": 3, "path": [3], "outcome": "SIMPLE", "codes": [], "cranked": True,
            "rpm_in": 3, "facts": {"rpm_max": 190.0, "freq_max": 0.0, "ready": False}}
    base.update(kw)
    return base


# ══ 1. ДГУ №1 05.11.2026 — четыре попытки ═══════════════════════════════════
A36299 = att(crank_sec=11.0, facts={"rpm_max": 188.0, "freq_max": 0.0, "ready": False},
             switch={"value": 0, "label": "Выкл", "at": "2026-11-05T07:15:38+00:00"},
             run_command={"value": 1, "label": "Стоп", "at": "2026-11-05T07:15:37+00:00"})
A36301 = att(crank_sec=71.0, facts={"rpm_max": 191.0, "freq_max": 0.0, "ready": False},
             switch={"value": 0, "label": "Выкл", "at": "2026-11-05T07:16:58+00:00"})
A36303 = att(crank_sec=69.0, outcome="EMERGENCY", codes=[359],
             facts={"rpm_max": 214.0, "freq_max": 0.0, "ready": False},
             switch={"value": 0, "label": "Выкл", "at": "2026-11-05T07:19:58+00:00"})
A36305 = att(crank_sec=21.0, facts={"rpm_max": 173.0, "freq_max": 0.0, "ready": False},
             switch={"value": 0, "label": "Выкл", "at": "2026-11-05T07:20:58+00:00"})
for sid, a, want in ((36299, A36299, "Прерванный пуск · Стоп"), (36301, A36301, "Прерванный пуск · Стоп"),
                     (36303, A36303, "Неудачный пуск · Авария"), (36305, A36305, "Прерванный пуск · Стоп")):
    check(attempt_label(a) == want, f"1: #{sid} — {attempt_label(a)!r}, ждали {want!r}")

# ══ 2. Правило ══════════════════════════════════════════════════════════════
check(attempt_label(att(codes=[1438])) == "Неудачный пуск · Стоп",
      "2а: код панели без аварии — «Неудачный пуск · Стоп»")
check(attempt_label(att(cranked=False, outcome="EMERGENCY", codes=[1438])) == "Неудачный пуск · Авария",
      "2б: авария без прокрутки (1438 «Отказ прокрутки») — «Неудачный пуск · Авария»")
check(attempt_label(att(cranked=False, outcome="EMERGENCY", codes=[])) == "Неудачный пуск · Авария",
      "2в: авария без кода — «Неудачный»")
check(attempt_label(att(cranked=False, facts={"rpm_max": 0.0})) == "Отменённый пуск · Стоп",
      "2г: отсчёты без вращения, без кода и аварии — «Отменённый пуск · Стоп»")
check(attempt_label(att(cranked=None, rpm_in=0, facts={"rpm_max": 0.0})) == "Прерванный пуск · Стоп",
      "2д: прокрутка по данным не видна (решение А) — не «Отменённый», а «Прерванный»")
# Старые записи без cranked и без codes (до v5.1.23): по rpm_max
_old = {"path": [3], "outcome": "SIMPLE", "from_state": 3, "facts": {"rpm_max": 189.0}}
check(attempt_label(_old) == "Прерванный пуск · Стоп"
      and attempt_label(dict(_old, facts={"rpm_max": 0.0})) == "Отменённый пуск · Стоп"
      and attempt_label(dict(_old, facts={})) == "Прерванный пуск · Стоп"
      and attempt_label(dict(_old, outcome="EMERGENCY")) == "Неудачный пуск · Авария",
      "2е: старые записи — прокрутка/нет/неизвестно/авария")
check(attempt_label(None) is None and attempt_label("x") is None, "2ж: не попытка — None")
# Ключ подпись не решает: «Выкл» в кадре падения без кода — по-прежнему по прокрутке
check(attempt_label(att(switch={"value": 0, "label": "Выкл", "at": None}, cranked=False,
                        facts={"rpm_max": 0.0})) == "Отменённый пуск · Стоп",
      "2з: ключ «Выкл» без прокрутки — «Отменённый»")

# ══ 3. Потребители ══════════════════════════════════════════════════════════
# 3а. Календарь и статус — label_from_chars (web/routes._seg_label, status_assembler)
check(label_from_chars({"attempt": A36301}) == "Прерванный пуск · Стоп"
      and label_from_chars({"attempt": A36303}) == "Неудачный пуск · Авария",
      "3а: календарь и строка статуса — новая подпись")

# 3б. Заключение-факты без ИИ: подпись и текст без «неудачной попытки»
from corpus import worker  # noqa: E402
_c = worker._attempt_conclusion({"id": 36301, "run_state": 0,
                                 "characteristics_json": json.dumps({"attempt": dict(A36301, t_fall="2026-11-05T07:16:58+00:00")})})
_md = (_c or {}).get("report_md") or (_c or {}).get("md") or json.dumps(_c, ensure_ascii=False)
check(_c is not None and "Прерванный пуск · Стоп: выход из стопа не подтвердился" in _md
      and "для попытки пуска не пишется" in _md and "неудачн" not in _md.lower(),
      f"3б: заключение-факты: {_md[:400]}")

# 3в. Строка отчёта сегмента — «Попытка выхода из стопа», подпись — в заголовке
_ser = (ROOT / "analytics" / "serializer.py").read_text(encoding="utf-8")
check("**Попытка выхода из стопа:**" in _ser and "**Неудачная попытка выхода из стопа:**" not in _ser,
      "3в: строка отчёта без «Неудачная попытка»")

# 3г. Сервер телеметрии (segments): подпись попытки той же функцией
_mcp = (ROOT / "telemetry_mcp" / "tools_segments.py").read_text(encoding="utf-8")
check("неудачная попытка пуска" not in _mcp and 'attempt_label(cj["attempt"])' in _mcp,
      "3г: сервер телеметрии — подпись из attempt_label")
_lab = attempt_label(A36301)
check("Стоп · " + _lab.split(" · ")[0].lower() == "Стоп · прерванный пуск", "3г: строка режима сервера телеметрии")

# 3д. Версии входа заключения, гейта и совета — 5.2.15; правила гейта прежние
import corpus.preprocessor as prep  # noqa: E402
import online.incident_gate as ig  # noqa: E402
import online.status_assembler as sa  # noqa: E402
check(prep.CONCLUSION_INPUT_VERSION == sa.GATE_INPUT_VERSION == ig.INCIDENT_INPUT_VERSION == "5.2.15"
      and sa.GATE_RULES_VERSION == "5.1.15",
      f"3д: версии входа {prep.CONCLUSION_INPUT_VERSION}/{sa.GATE_INPUT_VERSION}/{ig.INCIDENT_INPUT_VERSION}, "
      f"правила гейта {sa.GATE_RULES_VERSION}")

# 3е. KB: комментарий подписи в обоих слоях, порог прокрутки прежний
import yaml  # noqa: E402
for _p in ("knowledge_base/_defaults/analytics/segmentation.yaml",
           "knowledge_base/equipment/cummins_kta50_pcc3300/analytics/segmentation.yaml"):
    _t = (ROOT / _p).read_text(encoding="utf-8")
    _y = yaml.safe_load(_t) or {}
    check("«Прерванный пуск · Стоп»" in _t and (_y.get("transition_confirm") or {}).get("crank_rpm_min") == 50,
          f"3е: {_p} — подпись в комментарии и crank_rpm_min 50")

# 3ж. VERSION
check((ROOT / "VERSION").read_text(encoding="utf-8").strip() == "5.2.15", "3ж: VERSION = 5.2.15")

if _errors:
    print(f"FAIL — {len(_errors)} расхождений:")
    for x in _errors:
        print(f"  • {x}")
    sys.exit(1)
print("Все проверки пройдены.")
