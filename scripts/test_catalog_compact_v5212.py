# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Краткий перечень в register_catalog (v5.2.12; проверка деплоя v5.2.11 14.11.2026,
docs/plans/design_tool_rules_v2.md, разд. 8.5–8.6).

Дефект: перечень опрашиваемых регистров без слова поиска не помещался в предел
результата сервера (8000 токенов по оценке 1,7 знака на токен) — у ДЭС №3 в ответе
оставалось 106 строк из 137, хвост с заданием частоты 40595 и номиналами пропадал.

Проверяется:
1. Без слова поиска маски неисправностей идут одной строкой (диапазоны адресов, какие
   без записей изменения, какие не опрашиваются), состояния не расшифровываются.
2. Поиск по слову или адресу даёт полную строку: состояния, число битов маски, дата.
3. Перечень из 137 регистров с настоящими именами из базы знаний помещается в предел
   результата с запасом.
4. Запасной режим (перечень по записям) и режим без сведений об опросе.

Запуск: python scripts/test_catalog_compact_v5212.py
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telemetry_mcp import availability as av                  # noqa: E402
from telemetry_mcp import tools_equipment as te               # noqa: E402
from telemetry_mcp.availability import Polled                 # noqa: E402
from telemetry_mcp.config import DEFAULT_LIMITS               # noqa: E402

UTC = timezone.utc
T_NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
FAILS = 0


def check(cond, msg):
    global FAILS
    if not cond:
        FAILS += 1
    print(("  ✓ " if cond else "  ✗ ") + msg)


# ── 1. Маски одной строкой, состояния без расшифровки ────────────────────────
print("1. Краткий перечень")

check(te._ranges([40400, 40401, 40402, 40405, 40407, 40408]) == "40400–40402, 40405, 40407, 40408", "диапазоны адресов")
check(te._ranges([]) == "" and te._ranges([5, 3, 4, 3]) == "3–5", "пустой список, порядок и повторы")

MASKS = list(range(40400, 40429))
rm = {40068: {"role": "RPM", "kind": "analog", "unit": "rpm"},
      40011: {"role": "RUN_STATE", "kind": "enum", "labels_ru": {"0": "Стоп", "3": "Работа"}}}
rm.update({a: {"role": f"FAULT_MASK_{a - 40400}", "kind": "fault_bitmap"} for a in MASKS})
cat = {40068: {"name_ru": "Обороты двигателя", "kind": "analog", "unit": "rpm"},
       40011: {"name_ru": "Состояние установки", "kind": "enum",
               "states": {"labels_ru": {"0": "Останов", "1": "Пуск", "2": "Прогрев", "3": "Работа"}}},
       40557: {"name_ru": "Состояние стартера", "kind": "enum", "states": {"labels_ru": {"0": "выкл", "1": "вкл"}}}}
cat.update({a: {"name_ru": f"16-битная маска текущих активных неисправностей №{a - 40399}", "kind": "fault_bitmap",
                "states": {str(b): f"бит {b}" for b in range(14)}} for a in MASKS})
# опрашиваются маски 40400–40415, 40420, 40422–40428; 40402 и 40404–40406 ни разу не менялись
on = [a for a in MASKS if a not in (40416, 40417, 40418, 40419, 40421)]
polled = av.PolledMap({40068: Polled(T_NOW), 40011: Polled(T_NOW)})
polled.update({a: Polled(T_NOW, a in (40402, 40404, 40405, 40406), 0.0 if a in (40402, 40404, 40405, 40406) else None)
               for a in on})

v = te.build_catalog(rm, cat, polled, None, False, tz=UTC)
L = v.text.split("\n")
check(L[0].startswith("Опрашивается регистров: 26 из 32 в карте панели.") and "Перечень краткий" in L[0]
      and "по слову или адресу (query)" in L[0], f"шапка с подсказкой: {L[0]}")
check(L[1].startswith("Маски неисправностей — регистров 24, одной строкой: опрашиваются 40400–40415, 40420, 40422–40428"),
      f"маски одной строкой: {L[1]}")
check("из них значение не менялось (0) — 40402, 40404–40406" in L[1], "маски без записей изменения названы со значением")
check("fault_code (addr и bit)" in L[1] and "по её адресу или по слову «маска»" in L[1], "подсказки: расшифровка бита и полные строки")
check("не опрашиваются" not in L[1], "по умолчанию в строке масок только опрашиваемые")
check(L[2] == "адрес | имя | ед. | вид | роль | опрос", f"заголовок краткого перечня: {L[2]}")
check([ln.split(" | ")[0] for ln in L[3:]] == ["40011", "40068"], "строк масок в таблице нет")
check(L[3] == "40011 | Состояние установки |  | состояние | роль RUN_STATE | посл. запись 14.11.2026"
      or (L[3].startswith("40011 | ") and "=" not in L[3] and L[3].endswith("| посл. запись 14.11.2026")),
      f"состояния не расшифрованы: {L[3]}")
check(v.total == 26, f"счёт регистров включает маски: {v.total}")
check("Колонка «опрос» заполнена" not in v.text, "в коротком списке дата остаётся в каждой строке")

# общая дата последней записи — в шапку, в строках остаётся только то, что отличается
T_OLD = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)
many_cat = {a: {"name_ru": f"Параметр {a}", "kind": "analog"} for a in range(40100, 40112)}
many = av.PolledMap({a: Polled(T_NOW) for a in range(40100, 40110)})
many[40110] = Polled(T_OLD)
many[40111] = Polled(T_NOW, True, 5.0)
v = te.build_catalog({}, many_cat, many, None, False, tz=UTC)
L = v.text.split("\n")
check("Колонка «опрос» заполнена только там, где отличается: у остальных регистров — посл. запись 14.11.2026." in L[0],
      f"общая дата вынесена в шапку: {L[0][-110:]}")
check(L[1] == "адрес | имя | ед. | вид | роль | опрос" and L[2] == "40100 | Параметр 40100 |  | аналог",
      f"строка с общей датой — без колонки опроса: {L[2]}")
check(L[12].endswith("| посл. запись 02.10.2026") and L[13].endswith("| значение не менялось (5)"),
      f"отличающиеся строки колонку сохраняют: {L[12]} / {L[13]}")
v = te.build_catalog({}, many_cat, many, None, True, tz=UTC)
check(all(not ln.endswith("| не опрашивается") for ln in v.text.split("\n")[2:12]) and v.total == 12, "вся карта: опрашиваемые без пометки")
many_cat[40999] = {"name_ru": "Параметр вне опроса", "kind": "analog"}
v = te.build_catalog({}, many_cat, many, None, True, tz=UTC)
check(v.text.split("\n")[-1] == "40999 | Параметр вне опроса |  | аналог | не опрашивается", "вся карта: регистр вне опроса помечен в строке")
v = te.build_catalog({}, many_cat, many, "параметр", False, tz=UTC)
check("Колонка «опрос» заполнена" not in v.text and v.text.split("\n")[2].endswith("| посл. запись 14.11.2026") and v.total == 12,
      "в поиске дата остаётся в каждой строке, сколько бы строк ни нашлось")
few = av.PolledMap({a: Polled(T_NOW if a % 2 else T_OLD) for a in range(40100, 40107)})
v = te.build_catalog({}, many_cat, few, None, False, tz=UTC)
check("Колонка «опрос» заполнена" not in v.text, "меньше восьми строк с общей датой — в шапку не выносится")

# вся карта панели: неопрашиваемые маски названы отдельно
v = te.build_catalog(rm, cat, polled, None, True, tz=UTC)
L = v.text.split("\n")
check("не опрашиваются 40416–40419, 40421" in L[1], f"вся карта: маски вне опроса — {L[1]}")
check(any(ln.startswith("40557 | ") and ln.endswith("| не опрашивается") for ln in L), "вся карта: регистр вне опроса помечен")
check(v.total == 32, "вся карта: счёт")

# предел строк считает и маски
v = te.build_catalog(rm, cat, polled, None, True, tz=UTC, cap=2)
check(v.truncation == ["показано 31 из 32 регистров — сузьте поиском"] and len(v.text.split("\n")) == 5,
      f"предел строк: {v.truncation}")

# ── 2. Поиск — полная строка ─────────────────────────────────────────────────
print("2. Поиск по слову и адресу")

v = te.build_catalog(rm, cat, polled, "маска", False, tz=UTC)
L = v.text.split("\n")
check("Перечень краткий" not in v.text and "одной строкой" not in v.text, "в поиске подсказок краткого перечня нет")
check(L[1] == "адрес | имя | ед. | вид | роль · состояния | опрос", "заголовок поиска — с состояниями")
check(v.total == 24 and L[2].startswith("40400 | ") and "· битов 14" in L[2] and L[2].endswith("| посл. запись 14.11.2026"),
      f"маски строками, с числом битов и датой: {L[2]}")
check(any(ln.startswith("40402 | ") and ln.endswith("| значение не менялось (0)") for ln in L), "маска без записей изменения — своей строкой")
v = te.build_catalog(rm, cat, polled, "40011", False, tz=UTC)
check("0=Стоп" in v.text and "3=Работа" in v.text and "1=Пуск" in v.text, f"поиск по адресу расшифровывает состояния: {v.text.splitlines()[-1]}")
line, _hay, _name = te._catalog_row(40011, rm, cat)
check("0=Стоп" in line, "строка по умолчанию — полная (прежний вызов без brief)")
check("=" not in te._catalog_row(40011, rm, cat, brief=True)[0], "brief — без состояний")

# ── 3. Размер перечня на настоящих именах ────────────────────────────────────
print("3. Перечень из 137 регистров помещается в предел результата")

from analytics import binding                                 # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CFG = binding.build_config(ROOT / "knowledge_base", controller_id="pcc3300", engine_id="cummins_qsx15")
RM = CFG.register_map
real = sorted(RM)
# карта панели: имена и состояния — из базы знаний, плюс регистры без роли с длинным именем
CAT = {a: {"name_ru": (RM[a].get("description") or f"Регистр {a}"), "kind": RM[a].get("kind") or "analog",
           "unit": RM[a].get("unit") or "",
           "states": ({"labels_ru": {str(i): f"Состояние номер {i} с длинным названием" for i in range(12)}}
                      if RM[a].get("kind") == "enum" else ({str(b): "x" for b in range(16)}
                                                           if RM[a].get("kind") == "fault_bitmap" else None))}
       for a in real}
extra = [a for a in range(43000, 43400) if a not in CAT][:max(0, 137 - len(real))]
for a in extra:
    CAT[a] = {"name_ru": "Регистр без роли в базе знаний с именем обычной длины, примерно шестьдесят знаков",
              "kind": "enum" if a % 3 == 0 else "analog", "unit": "kPa",
              "states": {"labels_ru": {str(i): f"Состояние номер {i} с длинным названием" for i in range(12)}}}
addrs137 = (real + extra)[:137]
check(len(addrs137) == 137, f"в выборке 137 регистров (из базы знаний {len(real)})")
P137 = av.PolledMap({a: Polled(T_NOW, RM.get(a, {}).get("kind") == "fault_bitmap" and a % 2 == 0,
                               0.0 if RM.get(a, {}).get("kind") == "fault_bitmap" and a % 2 == 0 else None)
                     for a in addrs137})
v = te.build_catalog(RM, CAT, P137, None, False, tz=UTC)
limit_chars = int(DEFAULT_LIMITS["result_max_tokens"] * 1.7)
n_masks = sum(1 for a in addrs137 if RM.get(a, {}).get("kind") == "fault_bitmap")
rows = [ln for ln in v.text.split("\n") if ln[:5].isdigit()]
check(v.total == 137 and len(rows) == 137 - n_masks and n_masks >= 20, f"все регистры учтены: строк {len(rows)}, масок {n_masks}")
# выборка нарочно тяжелее настоящей (56 регистров с именем в 80 знаков): на сервере перечень
# ДЭС №3 из 137 регистров занимает около 10,1 тыс. знаков
check(len(v.text) < limit_chars * 0.85, f"перечень {len(v.text)} зн. — запас к пределу ({limit_chars} зн.) не меньше 15 %")
check("Колонка «опрос» заполнена только там, где отличается" in v.text.splitlines()[0], "общая дата вынесена в шапку")
full = sum(len(te._catalog_row(a, RM, CAT)[0]) + 27 for a in addrs137)
check(full > limit_chars, f"прежний вид того же перечня предел превышал: {full} зн.")
check(not v.truncation, "усечения по числу строк нет")

# ── 4. Запасной режим и режим без сведений об опросе ─────────────────────────
print("4. Запасной режим и каталог без сведений об опросе")

soft = av.PolledMap({40068: Polled(T_NOW), 40400: Polled(T_NOW), 40401: Polled(T_NOW), 40402: Polled(T_NOW)})
soft.exact = False
v = te.build_catalog(rm, cat, soft, None, True, tz=UTC)
L = v.text.split("\n")
check(L[0].startswith("Регистров с записями: 4 из 32") and "Перечень краткий" in L[0], f"запасной режим, шапка: {L[0][:80]}")
check("с записями 40400–40402" in L[1] and "записей нет 40403–40428" in L[1] and "не опрашива" not in L[1],
      f"запасной режим: маски без слова «не опрашивается» — {L[1]}")
v = te.build_catalog(rm, cat, None, None, False)
L = v.text.split("\n")
check(L[0].startswith("Перечень краткий") and L[1].startswith("Маски неисправностей — регистров 29, одной строкой: 40400–40428."),
      f"без сведений об опросе: {L[:2]}")
check(L[2] == "адрес | имя | ед. | вид | роль" and v.total == 31, "без сведений об опросе: заголовок без колонки опроса, счёт")
v = te.build_catalog({}, {}, av.PolledMap(), None, False)
check("Перечень краткий" not in v.text and v.total == 0, "пустой каталог — без подсказки краткого перечня")

src = (Path(__file__).resolve().parent.parent / "telemetry_mcp" / "tools_equipment.py").read_text(encoding="utf-8")
check("Без слова поиска перечень краткий" in src and "маски неисправностей одной строкой" in src, "описание инструмента говорит о кратком перечне")

print()
if FAILS:
    print(f"ПРОВАЛЕНО проверок: {FAILS}")
    sys.exit(1)
print("Все проверки пройдены")
