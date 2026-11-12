"""v5.2.5 «График на сервере телеметрии» (MCP, этап Э2а; docs/plans/design_mcp_e2.md
разд. 9, решения Р17 и Р21). Чистые функции без БД.

Раздел 1 — панели (telemetry_mcp.chart.panels_from): группировка колонок сбора по
параметру в порядке запроса, своя установка первой, соседи следом, номинал панели,
признак «данные есть».
Раздел 2 — рендер (render_png): PNG шириной 1200 px, высота по числу панелей;
сырой режим (числа) и укрупнённый (мин/ср/макс с полосой), пустые ячейки, разрывы
связи, сосед, номинал рядом с данными и далеко от них, многодневное окно; размер
файла в разумных пределах; панель без записей не роняет рендер.
Раздел 3 — сводка (tools_series.summary_lines) по собранному ряду: те же строки,
что печатает series; колонка без записей.
Раздел 4 — сервер: инструмент chart в списке, аргументы, без схемы структурного
вывода у всех инструментов (иначе текст уходит дважды), версия из файла VERSION,
лимиты графика в конфиге.
Раздел 5 — ответ с картинкой: список [текст, Image] превращается SDK в текст и
ImageContent (PNG, base64); вложение учитывается в конверте.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_errors: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        _errors.append(msg)
        print("  ✗", msg)


def U(h: int, m: int = 0, s: int = 0, day: int = 16) -> datetime:
    return datetime(2026, 10, day, h, m, s, tzinfo=timezone.utc)


from config import get_tz
from telemetry_mcp.chart import Line, Panel, has_data, panels_from, png_size, render_png

TZ = get_tz()

# ── 1. Панели ────────────────────────────────────────────────────────────────
cols = [
    {"key": "Частота генератора", "unit_name": "ДГУ №1", "neighbor": False, "param": "Частота генератора", "unit": "Гц", "role": "FREQUENCY"},
    {"key": "Обороты двигателя", "unit_name": "ДГУ №1", "neighbor": False, "param": "Обороты двигателя", "unit": "об/мин", "role": "RPM"},
    {"key": "ДГУ №2: Частота генератора", "unit_name": "ДГУ №2", "neighbor": True, "param": "Частота генератора", "unit": "Гц", "role": "FREQUENCY"},
    {"key": "ДГУ №2: Обороты двигателя", "unit_name": "ДГУ №2", "neighbor": True, "param": "Обороты двигателя", "unit": "об/мин", "role": "RPM"},
]
cells = {"Частота генератора": [50.0, 56.8, None], "Обороты двигателя": [1500, 1696, 76],
         "ДГУ №2: Частота генератора": [50.0, 50.1, 50.0], "ДГУ №2: Обороты двигателя": [1500, 1501, 1500]}
panels = panels_from(cols, cells, {"Частота генератора": 50.0})
check([p.label for p in panels] == ["Частота генератора", "Обороты двигателя"], f"1: панели по параметрам в порядке запроса: {[p.label for p in panels]}")
check([ln.name for ln in panels[0].lines] == ["ДГУ №1", "ДГУ №2"] and [ln.neighbor for ln in panels[0].lines] == [False, True],
      "1: своя установка первой, сосед следом")
check(panels[0].nominal == 50.0 and panels[1].nominal is None and panels[0].unit == "Гц", "1: номинал и единица панели")
check(has_data(panels) and not has_data(panels_from(cols, {k: [None, None] for k in cells})), "1: признак «данные есть»")
check(not has_data(panels_from(cols, {})), "1: колонок без ячеек — данных нет")
# сосед, пришедший раньше своей установки, всё равно встаёт вторым
rev = panels_from([cols[2], cols[0]], cells)
check([ln.neighbor for ln in rev[0].lines] == [False, True], "1: порядок линий не зависит от порядка колонок")

# ── 2. Рендер ────────────────────────────────────────────────────────────────
times = [U(6, 20) + timedelta(seconds=10 * i) for i in range(90)]
raw = Panel("Частота генератора", "Гц", [
    Line("ДГУ №1", False, [None] * 30 + [56.8, 41.5, 0.0] + [0.0] * 20 + [48.1 + (i % 5) for i in range(37)]),
    Line("ДГУ №2", True, [50.0 + 0.1 * (i % 2) for i in range(90)])], nominal=50.0)
rpm = Panel("Обороты двигателя", "об/мин", [Line("ДГУ №1", False, [0] * 30 + [1696, 1247, 76] + [0] * 20 + [1500] * 37)], nominal=1500.0)
png = render_png([raw, rpm], times, U(6, 20), U(6, 35), title="ДГУ №1 · 16.10.2026 09:20–09:35 МСК",
                 gaps=[(U(6, 22), U(6, 23, 30))], tz=TZ)
w, h = png_size(png)
check(png[:8] == b"\x89PNG\r\n\x1a\n" and w == 1200, f"2: PNG шириной 1200: {w}×{h}")
check(500 <= h <= 600, f"2: высота для двух панелей (0,9 + 2×2,3 дюйма): {h}")
check(15_000 < len(png) < 400_000, f"2: размер файла в разумных пределах: {len(png)}")
one = render_png([rpm], times, U(6, 20), U(6, 35), title="одна панель", tz=TZ)
check(png_size(one)[1] < h and png_size(one)[0] == 1200, f"2: одна панель ниже двух: {png_size(one)}")
# укрупнённый режим: (мин, ср, макс) с полосой, многодневное окно, пустые корзины
bt = [U(0, day=10) + timedelta(hours=12 * i) for i in range(36)]
bcells = [None, None] + [(49.7 + 0.01 * i, 50.0, 50.3 + 0.2 * (i % 3)) for i in range(30)] + [None, (0.0, 30.0, 56.8), (50.0, 50.0, 50.0), None]
bucket = Panel("Частота генератора", "Гц", [Line("ДГУ №1", False, bcells)], nominal=50.0)
far = Panel("Давление масла", "кПа", [Line("ДГУ №1", False, [(400.0, 420.0, 440.0)] * 36)], nominal=5.0)
png2 = render_png([bucket, far], bt, U(0, day=10), U(0, day=28), title="18 суток", tz=TZ, multi_day=True,
                  gaps=[(U(13, day=10), U(14, day=10)), (U(3, day=27), None)])
check(png_size(png2)[0] == 1200 and len(png2) > 15_000, f"2: укрупнённый режим с полосой, открытый разрыв: {png_size(png2)} {len(png2)}")
# панель без записей и линия из одних пустых — рендер не падает
empty = Panel("Ток в нейтральном проводнике генератора", "А", [Line("ДГУ №1", False, [None] * 90)])
png3 = render_png([empty, rpm], times, U(6, 20), U(6, 35), title="пустая панель", tz=TZ)
check(png_size(png3)[0] == 1200, "2: панель без записей не роняет рендер")
png4 = render_png([Panel("X", "", [Line("A", False, [])])], [], U(6, 20), U(6, 35), title="нет сетки", tz=TZ)
check(png4[:4] == b"\x89PNG", "2: пустая сетка времени не роняет рендер")
try:
    png_size("не картинка".encode("utf-8"))
    check(False, "2: png_size на мусоре — ошибка")
except ValueError:
    pass

# ── 3. Сводка по собранному ряду ─────────────────────────────────────────────
from telemetry_mcp.series import resample
from telemetry_mcp.tools_series import Gathered, summary_lines

pts = {"Частота генератора": [(U(6, 26, 3), 56.8), (U(6, 26, 12), 41.5), (U(6, 26, 23), 0.0)], "Пусто": []}
rs = resample(pts, U(6, 20), U(6, 35), 150)
g = Gathered(rs=rs, keys=list(pts), steps={"Частота генератора": None}, cols=[], prefs=[], t0=U(6, 20), t1=U(6, 35), long_window=False)
sl = summary_lines(g)
check(sl[0] == "сводка:" and "первое 56.8 (09:26:03)" in sl[1] and "мин 0 (09:26:23)" in sl[1] and "макс 56.8 (09:26:03)" in sl[1]
      and "записей 3" in sl[1], f"3: строка сводки: {sl[1]}")
check(sl[2] == "  Пусто: записей нет", f"3: колонка без записей: {sl[2]}")

# ── 4. Сервер ────────────────────────────────────────────────────────────────
import logging

logging.disable(logging.CRITICAL)
from telemetry_mcp.config import DEFAULT_LIMITS, McpSettings
from telemetry_mcp.server import INSTRUCTIONS, build_server, service_version

srv = build_server(McpSettings(journal=False))
tools = {t.name: t for t in asyncio.run(srv.list_tools())}
check("chart" in tools and len(tools) == 13, f"4: chart в списке, всего 13: {sorted(tools)}")
check(set(tools["chart"].input_schema["properties"]) == {"unit", "params", "t_from", "t_to", "at", "minutes", "with_neighbors"},
      f"4: аргументы chart: {set(tools['chart'].input_schema['properties'])}")
check(tools["chart"].input_schema.get("required") == ["unit", "params"], f"4: обязательные аргументы: {tools['chart'].input_schema.get('required')}")
check("только из сводки" in tools["chart"].description, "4: описание: числа — из сводки")
check(all(getattr(t, "output_schema", None) is None for t in tools.values()),
      f"4: без схемы структурного вывода: {[n for n, t in tools.items() if getattr(t, 'output_schema', None)]}")
_ver = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "VERSION"), encoding="utf-8").read().strip()
check(service_version() == _ver, f"4: версия сервиса из файла VERSION: {service_version()} / {_ver}")
check(DEFAULT_LIMITS["chart_max_params"] == 4 and DEFAULT_LIMITS["chart_max_points"] == 600, "4: лимиты графика")
check("chart" in INSTRUCTIONS and "из сводки" in INSTRUCTIONS, "4: правило про график в инструкциях сервера")

# ── 5. Ответ с картинкой ─────────────────────────────────────────────────────
from mcp.server.mcpserver import Image
from mcp.server.mcpserver.utilities.func_metadata import _convert_to_content

blocks = _convert_to_content(["[T1] текст конверта\n\nсводка:", Image(data=png, format="png")])
check(len(blocks) == 2 and blocks[0].type == "text" and blocks[1].type == "image", f"5: текст и картинка: {[b.type for b in blocks]}")
import base64

mime = getattr(blocks[1], "mime_type", None) or getattr(blocks[1], "mimeType", None)
check(mime == "image/png" and base64.b64decode(blocks[1].data) == png, f"5: PNG в base64 без потерь: {mime}")
from telemetry_mcp.envelope import Envelope

e = Envelope(label="T1", tool="chart", unit="ДГУ №1")
e.attachment_bytes = len(png)
check(e.attachment_bytes == len(png) and "attachment" not in e.text(), "5: вложение учтено в конверте, в текст не печатается")

# ── 6. Значение на входе окна (посев) ────────────────────────────────────────
# Запись идёт по изменению: до первой записи в окне значение держится с момента до окна.
# Без посева ряд и график выглядели пустыми до первой записи (частота ДГУ №1 16.10 до 09:26).
from telemetry_mcp.series import from_buckets, seed_allowed

T0, T1 = U(6, 20), U(6, 35)
check(seed_allowed(U(3, 44), T0, []), "6: запись до окна без разрывов — держим")
check(not seed_allowed(U(3, 44), T0, [(U(5), U(5, 10))]), "6: разрыв связи между записью и окном — не держим")
check(seed_allowed(U(5, 30), T0, [(U(5), U(5, 10))]), "6: разрыв раньше записи — держим")
check(not seed_allowed(U(5, 30), T0, [(U(6), None)]), "6: открытый разрыв до окна — не держим")
check(not seed_allowed(U(6, 19), T0, [(U(6, 19, 30), U(6, 22))]), "6: разрыв, накрывающий начало окна, — не держим")
check(not seed_allowed(T0, T0, []) and not seed_allowed(U(6, 25), T0, []), "6: запись не раньше окна — не посев")
check(seed_allowed(U(3, 44), T0, [(U(6, 22), U(6, 23))]), "6: разрыв внутри окна посеву не мешает")

seed_pt = (T0 - timedelta(microseconds=1), 0.0)
rs6 = resample({"Частота": [seed_pt, (U(6, 26, 3), 56.8), (U(6, 26, 12), 41.5)], "Обороты": [(U(6, 20, 12), 0.0), (U(6, 26, 3), 1696.0)]},
               T0, T1, 150)
check(rs6.times[0] == T0 and rs6.cells["Частота"][0] == 0.0, f"6: сетка начинается с начала окна, на входе — посев: {rs6.times[0]} {rs6.cells['Частота'][:2]}")
check(rs6.cells["Обороты"][0] is None and rs6.cells["Обороты"][1] == 0.0, "6: у ряда без посева вход окна пуст")
check(rs6.summary["Частота"]["first"] == (U(6, 26, 3), 56.8) and rs6.n_raw["Частота"] == 2 and rs6.summary["Частота"]["n"] == 2,
      f"6: посев не входит в сводку и счёт записей: {rs6.summary['Частота']['first']} {rs6.n_raw['Частота']}")
rs6g = resample({"Частота": [seed_pt, (U(6, 26, 3), 56.8)]}, T0, T1, 150, gaps=[(U(6, 19), U(6, 21))])
check(rs6g.times[0] != T0 or rs6g.cells["Частота"][0] is None, "6: начало окна в разрыве связи — посев не показывается")
rs6n = resample({"Обороты": [(U(6, 20, 12), 0.0)]}, T0, T1, 150)
check(rs6n.times[0] == U(6, 20, 12), "6: без посева сетка — как раньше, с первой записи")

B6 = [{"b": U(6, 22), "vmin": 5.0, "vmax": 7.0, "avg": 6.0, "n": 2, "vfirst": 5.0, "vlast": 7.0, "t_first": U(6, 22, 5), "t_last": U(6, 22, 40)}]
fb6 = from_buckets({"P": B6}, T0, U(6, 24), 60, seed={"P": 3.0})
check(fb6.cells["P"][0] == (3.0, 3.0, 3.0) and fb6.cells["P"][1] == (3.0, 3.0, 3.0), f"6: корзины до первой записи держат посев: {fb6.cells['P'][:2]}")
check(fb6.cells["P"][2] == (3.0, 6.0, 7.0), f"6: минимум корзины учитывает значение на входе: {fb6.cells['P'][2]}")
check(fb6.summary["P"]["first"] == (U(6, 22, 5), 5.0) and fb6.summary["P"]["n"] == 2, "6: сводка корзин — только записи")
fb6g = from_buckets({"P": B6}, T0, U(6, 24), 60, gaps=[(U(6, 20), U(6, 21))], seed={"P": 3.0})
check(fb6g.cells["P"][0] is None and fb6g.cells["P"][1] is None, f"6: разрыв обрывает удержание посева: {fb6g.cells['P'][:2]}")
check(from_buckets({"P": B6}, T0, U(6, 24), 60).cells["P"][0] is None, "6: без посева вход окна пуст, как раньше")
check(DEFAULT_LIMITS["series_seed_max_age_hours"] == 6, "6: предел возраста посева — 6 ч")

# прогрев рендера не падает
from telemetry_mcp.chart import warm_up

warm_up()

if _errors:
    print(f"FAIL — {len(_errors)} расхождений:")
    for x in _errors:
        print(f"  • {x}")
    sys.exit(1)
print("Все проверки пройдены.")
