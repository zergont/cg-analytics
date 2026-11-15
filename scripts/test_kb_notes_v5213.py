# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""v5.2.13 «Пометки базы знаний к регистрам» (решение владельца 14.11.2026;
docs/plans/design_kb_register_notes.md). Без БД и сети.

Запись регистра в mapping.yaml несёт `note_ru` (текст для модели) и `note_source`
(документ, раздел, страница). Сервер телеметрии показывает пометку модели; пометка без
источника не показывается.

Раздел 1 — разбор полей записи (telemetry_mcp.kb_notes): пометка с источником, без
источника, пустая, многострочный YAML-блок; одно предупреждение на адрес.
Раздел 2 — строки каталога: пометка под строкой своего регистра в коротком списке,
подсказка в примечаниях в длинном, граница длины, списки без пометок не меняются.
Раздел 3 — блоки паспорта KB: по адресам и все подряд, имя регистра в заголовке.
Раздел 4 — база знаний: у каждой `note_ru` есть `note_source`; пометка регистра 40595
доходит до конфига обеих привязок (KTA50 и QSX15), говорит о составе задания и его
ограничении и не содержит сведений о конкретных установках.
Раздел 5 — каталог сервера (build_catalog) на настоящей базе знаний: пометка под строкой
40595 при поиске по слову и по адресу, счёт регистров и строка не меняются, полный каталог
без текста пометок с подсказкой в примечаниях, неопрашиваемый регистр.
Раздел 6 — инструменты через сервер (зависимости подменены): register_catalog и
kb_passport по параметру, по адресу без раздела порогов, без параметра; параметры без
пометки отвечают как раньше; описания инструментов.

Запуск:  py -3 scripts/test_kb_notes_v5213.py
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import yaml  # noqa: E402

from analytics import binding  # noqa: E402
from telemetry_mcp import kb_notes as kn  # noqa: E402

_errors: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        _errors.append(msg)
        print("  ✗", msg)


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


_cap = _Capture()
kn.logger.addHandler(_cap)
kn.logger.propagate = False

NOTE = {"role": "FREQ_REFERENCE", "description": "Итоговое задание частоты регулятора",
        "note_ru": "Сумма четырёх слагаемых.\n  Ограничена 60–110 %  базовой частоты.\n",
        "note_source": "0900-0670,\n  разд. 6.5.1, стр. 106\n"}
NO_SRC = {"role": "RPM", "note_ru": "Толкование без источника"}
PLAIN = {"role": "FREQUENCY", "description": "Частота генератора"}

# ── 1. Разбор полей записи ───────────────────────────────────────────────────
print("1. Пометка записи регистра")

check(kn.register_note(NOTE, 40595) == ("Сумма четырёх слагаемых. Ограничена 60–110 % базовой частоты.",
                                        "0900-0670, разд. 6.5.1, стр. 106"),
      f"пометка с источником, YAML-блок одной строкой: {kn.register_note(NOTE, 40595)}")
check(kn.register_note(PLAIN, 40044) is None and kn.register_note(None, 1) is None and kn.register_note({}, 1) is None,
      "нет пометки — None")
check(kn.register_note({"note_ru": "   ", "note_source": "док."}, 2) is None, "пустой текст — пометки нет")
check(kn.register_note({"note_ru": 5, "note_source": "док."}, 3) is None, "не строка — пометки нет")
check(not _cap.messages, f"предупреждений быть не должно: {_cap.messages}")

check(kn.register_note(NO_SRC, 40068) is None, "пометка без источника не показывается")
check(kn.register_note({"note_ru": "текст", "note_source": "  "}, 40068) is None, "пустой источник — как без источника")
kn.register_note(NO_SRC, 40068)
check(len(_cap.messages) == 1 and "40068" in _cap.messages[0] and "note_source" in _cap.messages[0],
      f"одно предупреждение на адрес: {_cap.messages}")
kn.register_note(NO_SRC, 40069)
check(len(_cap.messages) == 2, "другой адрес — своё предупреждение")

check(kn.note_lines(NOTE, 40595) == ["    пометка: Сумма четырёх слагаемых. Ограничена 60–110 % базовой частоты.",
                                     "    источник: 0900-0670, разд. 6.5.1, стр. 106"], "две строки под регистром")
check(kn.note_lines(PLAIN, 40044) == [] and kn.note_lines(NO_SRC, 40068) == [], "без пометки и без источника — строк нет")

# ── 2. Строки каталога ───────────────────────────────────────────────────────
print("2. Каталог регистров")

rm = {40044: PLAIN, 40595: NOTE, 40068: NO_SRC}
rows = ["40044 | Частота генератора | Гц | аналог", "40068 | Обороты | об/мин | аналог",
        "40595 | Задание частоты (регулятор) | Гц | аналог | посл. запись 14.11.2026"]
addrs = [40044, 40068, 40595]
notes: list[str] = []
out = kn.rows_with_notes(rows, addrs, rm, notes)
check(out[:3] == rows[:3] and len(out) == 5, f"строки регистров на месте, добавлены две: {len(out)}")
check(out[3].startswith("    пометка: Сумма четырёх") and out[4].startswith("    источник: 0900-0670"),
      "пометка и источник — сразу под строкой 40595")
check(notes == [], "в коротком списке подсказка не нужна")
check(not any("Толкование" in ln for ln in out), "пометка без источника в каталог не попадает")

mid = kn.rows_with_notes(["40595 | …", "40044 | …"], [40595, 40044], rm, [])
check(mid[0] == "40595 | …" and mid[1].startswith("    пометка:") and mid[3] == "40044 | …",
      "пометка идёт за своим регистром, а не в конце списка")

no_notes: list[str] = []
same = kn.rows_with_notes(rows[:2], addrs[:2], rm, no_notes)
check(same == rows[:2] and no_notes == [], "список без пометок не меняется")
check(kn.rows_with_notes([], [], rm, []) == [], "пустой список")

big_rm = {a: PLAIN for a in range(40001, 40031)}
big_rm[40010] = NOTE
big_rm[40020] = NOTE
big_addrs = sorted(big_rm)
big_rows = [f"{a} | регистр" for a in big_addrs]
hint: list[str] = []
out = kn.rows_with_notes(big_rows, big_addrs, big_rm, hint)
check(out == big_rows, "в длинном списке строки не меняются")
check(len(hint) == 1 and "40010, 40020" in hint[0] and "пометка базы знаний" in hint[0] and "query" in hint[0],
      f"подсказка с адресами в примечаниях: {hint}")

edge_addrs = big_addrs[5:5 + kn.NOTE_ROWS_MAX]          # ровно предел, 40010 внутри
edge_rows = [f"{a} | регистр" for a in edge_addrs]
e_notes: list[str] = []
out = kn.rows_with_notes(edge_rows, edge_addrs, big_rm, e_notes)
check(len(out) == kn.NOTE_ROWS_MAX + 2 and e_notes == [], "ровно предел строк — пометка печатается")
over_addrs = big_addrs[5:6 + kn.NOTE_ROWS_MAX]
o_notes: list[str] = []
out = kn.rows_with_notes([f"{a} | регистр" for a in over_addrs], over_addrs, big_rm, o_notes)
check(len(out) == kn.NOTE_ROWS_MAX + 1 and len(o_notes) == 1, "на одну строку больше предела — только подсказка")

cut_notes: list[str] = []
out = kn.rows_with_notes(big_rows[:12], big_addrs, big_rm, cut_notes)      # список усечён, адресов больше
check("40010" in cut_notes[0] and "40020" not in cut_notes[0], "подсказка — только по показанным строкам")

many_rm = {a: NOTE for a in range(40001, 40021)}
m_notes: list[str] = []
kn.rows_with_notes([f"{a} | r" for a in sorted(many_rm)], sorted(many_rm), many_rm, m_notes)
check(m_notes[0].count("400") == 12 and " …" in m_notes[0], f"в подсказке не больше 12 адресов: {m_notes}")

# ── 3. Блоки паспорта KB ─────────────────────────────────────────────────────
print("3. Паспорт KB")

blk = kn.passport_notes(rm, None, [40595])
check(blk == ["\n[регистр 40595 · Задание частоты (регулятор)]",
              "  пометка: Сумма четырёх слагаемых. Ограничена 60–110 % базовой частоты.",
              "  источник: 0900-0670, разд. 6.5.1, стр. 106"], f"блок регистра с пометкой: {blk}")
check(kn.passport_notes(rm, None, [40044]) == [] and kn.passport_notes(rm, None, [40068]) == [],
      "регистр без пометки и пометка без источника — блока нет")
check(kn.passport_notes(rm, None, [40595, 40595]) == blk, "повтор адреса — один блок")
check(kn.passport_notes(rm, None, [49999]) == [], "адреса нет в KB — блока нет")
check(kn.passport_notes(rm) == blk, "без адресов — все регистры с пометкой")
no_role = {40700: {"description": "Регистр без роли", "note_ru": "Текст", "note_source": "док., стр. 1"}}
check(kn.passport_notes(no_role)[0] == "\n[регистр 40700 · Регистр без роли]", "имя без роли — описание KB")
check(kn.passport_notes({40701: {"note_ru": "Текст", "note_source": "док."}},
                        {40701: {"name_ru": "Имя каталога"}})[0] == "\n[регистр 40701 · Имя каталога]",
      "имени в KB нет — имя из каталога")

# ── 4. База знаний ───────────────────────────────────────────────────────────
print("4. База знаний")

KB = Path("knowledge_base")
n_notes = 0
for path in sorted(KB.rglob("mapping.yaml")):
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    for addr, meta in (data.get("registers") or {}).items():
        if not isinstance(meta, dict):
            continue
        if "note_source" in meta and not str(meta.get("note_ru") or "").strip():
            check(False, f"{path}: у регистра {addr} источник есть, а пометки нет")
        if str(meta.get("note_ru") or "").strip():
            n_notes += 1
            check(bool(str(meta.get("note_source") or "").strip()),
                  f"{path}: у регистра {addr} пометка note_ru без note_source")
check(n_notes >= 1, "в базе знаний есть хотя бы одна пометка")

_n_warn = len(_cap.messages)
for engine in ("cummins_kta50", "cummins_qsx15"):
    cfg = binding.build_config(KB, controller_id="pcc3300", engine_id=engine)
    note = kn.register_note(cfg.register_map.get(40595), 40595)
    check(note is not None, f"pcc3300 × {engine}: пометка 40595 дошла до конфига")
    if note is None:
        continue
    text, src = note
    check("\n" not in text and "  " not in text, f"{engine}: текст одной строкой")
    for must in ("четырёх слагаемых", "Frequency Adjust", "Speed Bias", "60–110 %", "30…55 Гц",
                 "синхронизатор", "статизм", "1465", "43553", "43584", "43558"):
        check(must in text, f"{engine}: в пометке нет «{must}»")
    for must in ("0900-0670", "6.5.1", "стр. 106", "Выпуск 22", "Modbus 3.3.pdf"):
        check(must in src, f"{engine}: в источнике нет «{must}»")
    # Слой контроллера общий для всех PCC3300: сведений об установках и случаях в пометке нет
    for banned in ("ДГУ", "ДЭС", "Сининд", "Новопетровск", "16.10", "15.10", "2026"):
        check(banned not in text, f"{engine}: в пометке контроллера есть «{banned}» — это сведение об установке")
    check(cfg.register_map[40595].get("role") == "FREQ_REFERENCE" and cfg.role_to_addr("FREQ_REFERENCE") == 40595,
          f"{engine}: роль регистра не задета")
check(len(_cap.messages) == _n_warn, f"в базе знаний нет пометок без источника: {_cap.messages[_n_warn:]}")

# ── 5. build_catalog с настоящей базой знаний ────────────────────────────────
print("5. Каталог сервера телеметрии (build_catalog)")

import asyncio  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

from telemetry_mcp import tools_equipment as te  # noqa: E402
from telemetry_mcp.availability import Polled  # noqa: E402
from telemetry_mcp.common import UnitRef  # noqa: E402
from telemetry_mcp.config import McpSettings  # noqa: E402
from telemetry_mcp.server import build_server  # noqa: E402

UTC = timezone.utc
T_NOW = datetime(2026, 11, 14, 5, 0, tzinfo=UTC)
CFG = binding.build_config(KB, controller_id="pcc3300", engine_id="cummins_kta50")
RM = CFG.register_map
CAT = {40595: {"name": "Final Frequency Reference", "unit": "Hz", "kind": "analog"},
       40044: {"name": "Genset Frequency OP", "unit": "Hz", "kind": "analog"},
       43553: {"name": "Synchronizer Status", "name_ru": "Состояние синхронизатора", "kind": "enum"}}
POLLED = {a: Polled(T_NOW) for a in RM}

v = te.build_catalog(RM, CAT, POLLED, "задание", False, tz=UTC)
ln = v.text.split("\n")
check(v.total == 1, f"счёт регистров пометку не считает: {v.total}")
check(ln[2].startswith("40595 | Задание частоты (регулятор) | Гц | аналог | роль FREQ_REFERENCE")
      and ln[2].endswith("| посл. запись 14.11.2026"), f"строка регистра прежняя: {ln[2]}")
check(len(ln) == 5 and ln[3].startswith("    пометка: Задание скорости регулятора")
      and "30…55 Гц" in ln[3] and "синхронизатор" in ln[3], "пометка — под строкой 40595")
check(ln[4].startswith("    источник: 0900-0670_I10_201204.pdf") and "стр. 106" in ln[4], "источник — следующей строкой")
check(not any("пометка базы знаний" in n for n in v.notes), "в коротком списке подсказки нет")

v = te.build_catalog(RM, CAT, POLLED, "40595", False, tz=UTC)
check(v.total == 1 and "    пометка: " in v.text, "поиск по адресу — та же пометка")

v = te.build_catalog(RM, CAT, POLLED, "частот", False, tz=UTC)
ln = v.text.split("\n")
i595 = next(i for i, x in enumerate(ln) if x.startswith("40595 |"))
check(v.total == 3 and ln[i595 + 1].startswith("    пометка:") and sum(x.startswith("    пометка:") for x in ln) == 1,
      f"три регистра по слову (частота, обороты, задание): пометка только у 40595 и сразу под ним ({v.total})")

v = te.build_catalog(RM, CAT, POLLED, None, False, tz=UTC)
check(v.total == len(RM) and "    пометка:" not in v.text and "    источник:" not in v.text,
      "полный каталог — без текста пометок")
check(any("пометка базы знаний" in n and "40595" in n for n in v.notes), f"полный каталог — подсказка с адресом: {v.notes}")
RM_PLAIN = {a: {k: x for k, x in m.items() if k not in ("note_ru", "note_source")} for a, m in RM.items()}
v0 = te.build_catalog(RM_PLAIN, CAT, POLLED, None, False, tz=UTC)
check(v.text == v0.text and v.total == v0.total, "текст полного каталога от пометок не меняется")
check([n for n in v.notes if "пометка базы знаний" not in n] == v0.notes, "прочие примечания каталога прежние")
v0 = te.build_catalog(RM_PLAIN, CAT, POLLED, "задание", False, tz=UTC)
check("пометка" not in v0.text and len(v0.text.split("\n")) == 3, "без полей пометки строка 40595 — как до v5.2.13")

v = te.build_catalog(RM, CAT, None, "задание", False, tz=UTC)
check(v.total == 1 and "    пометка: " in v.text, "доступность не определена — пометка всё равно печатается")

v = te.build_catalog(RM, CAT, {40044: Polled(T_NOW)}, "задание", False, tz=UTC)
check(v.total == 0 and "пометка:" not in v.text, "регистра нет среди опрашиваемых — пометки в ответе нет")
v = te.build_catalog(RM, CAT, {40044: Polled(T_NOW)}, "задание", True, tz=UTC)
check(v.total == 1 and "| не опрашивается" in v.text and "    пометка: " in v.text,
      "вся карта панели: у неопрашиваемого регистра пометка есть")

# ── 6. Инструменты сервера: register_catalog и kb_passport ───────────────────
print("6. Инструменты сервера телеметрии")

_real = (te.resolve_unit, te.kb_for, te.get_catalog, te.polled_registers)
UNIT = UnitRef(id=2, name="ДГУ №1", object="Объект", router_sn="R1", equip_type="pcc", panel_id=1, active=True)


async def _f_unit(name):
    return UNIT


async def _f_kb(unit):
    return CFG, None, None


async def _f_cat(equip_type):
    return CAT


async def _f_polled(unit, catalog=None):
    return POLLED


def _text(res) -> str:
    return "\n".join(getattr(c, "text", "") for c in res.content)


logging.disable(logging.CRITICAL)
try:
    te.resolve_unit, te.kb_for, te.get_catalog, te.polled_registers = _f_unit, _f_kb, _f_cat, _f_polled
    srv = build_server(McpSettings(journal=False))
    tools = {t.name: t for t in asyncio.run(srv.list_tools())}
    check("пометка базы знаний" in tools["register_catalog"].description, "описание каталога говорит о пометках")
    check("пометки к регистрам" in tools["kb_passport"].description, "описание паспорта говорит о пометках")
    check(len(tools) == 13, f"инструментов по-прежнему 13: {len(tools)}")

    def call(tool: str, **args) -> str:
        return _text(asyncio.run(srv.call_tool(tool, args)))

    out = call("register_catalog", unit="ДГУ №1", query="задание")
    check(out.startswith("[T") and "\n    пометка: Задание скорости регулятора" in out and "\n    источник: 0900-0670" in out,
          f"register_catalog «задание»: пометка в ответе: {out[:200]}")
    out = call("register_catalog", unit="ДГУ №1")
    check("\n    пометка:" not in out and "примечания:" in out and "есть пометка базы знаний у регистров: 40595" in out,
          "register_catalog без запроса: подсказка в конверте, текста пометки нет")

    out = call("kb_passport", unit="ДГУ №1", param="задание частоты")
    check("[регистр 40595 · Задание частоты (регулятор)]" in out and "  пометка: Задание скорости" in out
          and "  источник: 0900-0670" in out, f"kb_passport «задание частоты»: блок регистра: {out[-300:]}")
    check("[frequency]" in out and out.index("[frequency]") < out.index("[регистр 40595"),
          "блок пометки — после разделов порогов")
    out = call("kb_passport", unit="ДГУ №1", param="40595")
    check("[регистр 40595" in out and not out.startswith("ОШИБКА"),
          f"по адресу без раздела порогов — пометка, а не отказ: {out[:160]}")
    out = call("kb_passport", unit="ДГУ №1", param="частота")
    check("[frequency]" in out and "[регистр 40595" not in out, "«частота» — другой регистр, пометки 40595 нет")
    out = call("kb_passport", unit="ДГУ №1", param="давление масла")
    check("[регистр" not in out and not out.startswith("ОШИБКА"), "параметр без пометки — ответ прежний")
    out = call("kb_passport", unit="ДГУ №1", param="43553")
    check(out.startswith("ОШИБКА: Раздел порогов для «43553» не найден"), f"нет ни порогов, ни пометки — отказ, как раньше: {out[:120]}")
    out = call("kb_passport", unit="ДГУ №1")
    check(out.count("[регистр ") == 1 and "[регистр 40595 · Задание частоты (регулятор)]" in out,
          "весь паспорт — все пометки базы знаний (сейчас одна)")
    check("[зоны нагрузки" in out and out.index("[регистр 40595") < out.index("[зоны нагрузки"),
          "весь паспорт: пометки перед зонами и шагом датчиков, разделы порогов на месте")
finally:
    te.resolve_unit, te.kb_for, te.get_catalog, te.polled_registers = _real
    logging.disable(logging.NOTSET)

print()
if _errors:
    print(f"Не пройдено проверок: {len(_errors)}")
    for msg in _errors:
        print("  -", msg)
    sys.exit(1)
print("Все проверки пройдены.")
