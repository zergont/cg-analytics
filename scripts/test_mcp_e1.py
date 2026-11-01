"""v5.2.0 «Сервер телеметрии для моделей» (MCP, этап Э1; проект
docs/plans/design_mcp_telemetry.md, решения Р0–Р16). Чистые функции без БД.

Раздел 1 — карта установок (db/units.build_tree): правило по умолчанию «тот же
роутер и номер панели», явная привязка и явное «не привязан», установка без строки
units (id None), соседи только активные, непривязанные датчики под объектом, имя
из units поверх реестра, свежесть наблюдения.
Раздел 2 — время: parse_ts (ISO, ДД.ММ.ГГГГ, ДД.ММ ЧЧ:ММ, ЧЧ:ММ, «сейчас», пояс
сервиса), parse_window (от/до, момент ± минут, умолчание, ошибки), fmt_num.
Раздел 3 — ряды (telemetry_mcp.series): удержание до следующей записи, разрыв
связи — пусто и без переноса через него, ячейка (мин / среднее ПО ВРЕМЕНИ / макс),
сырой режим при малом числе меток, укрупнение с круглым шагом, сводка.
Раздел 4 — лента: классификация (режим / действие персонала / состояние /
неисправность), схлопывание дребезга (≥3 переключений чаще 60 с — одна строка с
числом повторов; 2 — не схлопывать), разбор видов.
Раздел 5 — KB: разбор имён параметров (псевдоним, роль, русское имя, адрес,
описание, каталог), неоднозначность — ошибка со списком, предел числа параметров,
номинал из thresholds, ключевые роли.
Раздел 6 — конверт: метки [Tn] по задаче, строки очистки/разрывов/усечения,
окно одного дня.
Раздел 7 — отчёт и акт: разделы по заголовкам, ключи разделов, текст акта из
incident_json, первая строка сводки.
Раздел 8 — сервер и конфиг: 12 инструментов, имена аргументов, умолчания лимитов,
разрешения по источнику.
Раздел 9 — run_tool: ToolError → «ОШИБКА», срок → частичный ответ с пометкой,
усечение по размеру, источник не разрешён.
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


def U(h: int, m: int = 0, s: int = 0, day: int = 13) -> datetime:
    return datetime(2026, 10, day, h, m, s, tzinfo=timezone.utc)


# ── 1. Карта установок ────────────────────────────────────────────────────────
from db.units import build_tree, flat_units

REG = [
    {"router_sn": "R1", "equip_type": "pcc", "panel_id": 1, "name": "ДГУ №1", "manufacturer": "Cummins",
     "model": "KTA50", "engine_sn": "1345988", "controller_id": "pcc3300", "engine_id": "cummins_kta50", "active": True},
    {"router_sn": "R1", "equip_type": "pcc", "panel_id": 2, "name": "ДГУ №2", "controller_id": "pcc3300",
     "engine_id": "cummins_kta50", "active": True},
    {"router_sn": "R1", "equip_type": "yfm8", "panel_id": 1, "name": "Масло", "active": True},
    {"router_sn": "R1", "equip_type": "yfm8", "panel_id": 7, "name": "Датчик без панели", "active": True},
    {"router_sn": "R2", "equip_type": "pcc", "panel_id": 1, "name": "ДЭС №3", "kb_path": "cummins_kta50_pcc3300", "active": True},
]
UNITS = [{"id": 10, "router_sn": "R1", "panel_type": "pcc", "panel_id": 1, "name": "ДГУ №1 (Сининда)", "active": True, "notes": None},
         {"id": 11, "router_sn": "R1", "panel_type": "pcc", "panel_id": 2, "name": None, "active": False, "notes": "выкл"}]
tree = build_tree(REG, UNITS, [], object_names={"R1": "Сининда"},
                  freshness={("R2", "pcc", 1): U(12, 0, 3)})
objs = {o["name"]: o for o in tree["objects"]}
check(set(objs) == {"Сининда", "R2"}, f"1: объекты по имени и серийнику: {set(objs)}")
sin = objs["Сининда"]
u1, u2 = sin["units"]
check(u1["id"] == 10 and u1["name"] == "ДГУ №1 (Сининда)", "1: имя из units поверх реестра")
check(u2["id"] == 11 and u2["name"] == "ДГУ №2" and not u2["active"], "1: имя из реестра, активность из units")
check([s["name"] for s in u1["sensors"]] == ["Масло"] and u1["sensors"][0]["attached"] == "default",
      "1: датчик того же роутера и panel_id — по умолчанию к панели")
check([s["name"] for s in sin["unattached"]] == ["Датчик без панели"], "1: датчик без панели — не привязан")
check(u1["neighbors"] == [] and u2["neighbors"] == ["ДГУ №1 (Сининда)"],
      f"1: соседи — только активные: {u1['neighbors']} / {u2['neighbors']}")
des = objs["R2"]["units"][0]
check(des["id"] is None and des["name"] == "ДЭС №3" and not des["materialized"], "1: установка без строки units — id None")
check(des["panel"]["binding"] == "cummins_kta50_pcc3300" and u1["panel"]["binding"] == "pcc3300 × cummins_kta50",
      "1: привязка KB — пара или legacy")
check(des["panel"]["last_data_ts"] == U(12, 0, 3).isoformat(), "1: свежесть наблюдения в панели")
# явные решения: датчик 1 → к ДГУ №2, датчик 7 → явно не привязан
tree2 = build_tree(REG, UNITS, [
    {"router_sn": "R1", "equip_type": "yfm8", "panel_id": 1, "unit_id": 11, "role": "масло", "name": None},
    {"router_sn": "R1", "equip_type": "yfm8", "panel_id": 7, "unit_id": None, "role": None, "name": "Вибро"},
], object_names={"R1": "Сининда"})
sin2 = {o["name"]: o for o in tree2["objects"]}["Сининда"]
check([s["name"] for s in sin2["units"][1]["sensors"]] == ["Масло"] and sin2["units"][1]["sensors"][0]["role"] == "масло"
      and sin2["units"][1]["sensors"][0]["attached"] == "explicit", "1: явная привязка к другой установке")
check(sin2["units"][0]["sensors"] == [] and [s["name"] for s in sin2["unattached"]] == ["Вибро"],
      "1: явное «не привязан» с именем владельца")
check(len(flat_units(tree2)) == 3, "1: flat_units")

# ── 2. Время ─────────────────────────────────────────────────────────────────
from telemetry_mcp.common import ToolError, fmt_num, parse_ts, parse_window
from config import get_tz

tz = get_tz()
ref = datetime(2026, 10, 13, 15, 0, tzinfo=timezone.utc)
check(parse_ts("13.10.2026 12:00").astimezone(tz).hour == 12, "2: ДД.ММ.ГГГГ ЧЧ:ММ — пояс сервиса")
check(parse_ts("2026-10-13T12:00:00+00:00") == U(12), "2: ISO с поясом")
check(parse_ts("2026-10-13 12:00:30").astimezone(tz).second == 30, "2: ISO без пояса — пояс сервиса")
check(parse_ts("13.10 12:05", ref=ref).astimezone(tz).strftime("%d.%m %H:%M") == "13.10 12:05", "2: ДД.ММ ЧЧ:ММ — год из ref")
check(parse_ts("09:30", ref=ref).astimezone(tz).strftime("%H:%M") == "09:30", "2: ЧЧ:ММ — сегодня")
check(parse_ts("сейчас", ref=ref) == ref and parse_ts("") is None and parse_ts(None) is None, "2: «сейчас», пусто")
try:
    parse_ts("вчера вечером")
    check(False, "2: непонятное время — ошибка")
except ToolError as e:
    check("Не понял время" in str(e), "2: текст ошибки времени")
t0, t1 = parse_window("13.10.2026 12:00", "13.10.2026 13:30")
check((t1 - t0) == timedelta(minutes=90), "2: окно от/до")
t0, t1 = parse_window(None, None, "13.10.2026 12:00", 15)
check((t1 - t0) == timedelta(minutes=30) and t0.astimezone(tz).minute == 45, "2: момент ± минут")
t0, t1 = parse_window(None, None, None, None, default_hours=3)
check(abs((t1 - t0) - timedelta(hours=3)) < timedelta(seconds=2), "2: умолчание — последние N часов")
for bad in (("13.10.2026 13:00", "13.10.2026 12:00"),):
    try:
        parse_window(*bad)
        check(False, "2: начало позже конца — ошибка")
    except ToolError:
        pass
try:
    parse_window("01.09.2026", "13.10.2026", max_days=31)
    check(False, "2: окно длиннее предела — ошибка")
except ToolError as e:
    check("trend" in str(e), "2: подсказка про trend")
check(fmt_num(412.0) == "412" and fmt_num(50.04) == "50.0" and fmt_num(1234.6) == "1235" and fmt_num(None) == "—",
      f"2: fmt_num: {fmt_num(412.0)} {fmt_num(50.04)} {fmt_num(1234.6)}")
check(fmt_num(243, step=8) == "240" and fmt_num(249, step=8) == "248", "2: fmt_num по шагу датчика")

# ── 3. Ряды ──────────────────────────────────────────────────────────────────
from telemetry_mcp.series import Stepper, held_points, nice_step, resample

rows = [{"addr": 40034, "ts": U(12, 0, 0), "value": 400}, {"addr": 40034, "ts": U(12, 0, 30), "value": 500},
        {"addr": 40034, "ts": U(12, 2, 0), "value": None}, {"addr": 40003, "ts": U(12, 0, 0), "value": 50.0},
        {"addr": 40034, "ts": U(12, 3, 0), "value": 100, "is_carried_forward": True},
        {"addr": 40034, "ts": U(12, 5, 0), "value": 600}]
pts = held_points(rows, 40034)
check([v for _, v in pts] == [400, 500, 600], "3: точки регистра без None и удержанных движком")
st = Stepper(pts)
check(st.value_at(U(11, 59)) is None and st.value_at(U(12, 0, 10)) == 400 and st.value_at(U(12, 4)) == 500
      and st.value_at(U(13)) == 600, "3: удержание до следующей записи")
gap = [(U(12, 1), U(12, 4))]
stg = Stepper(pts, gap)
check(stg.value_at(U(12, 2)) is None, "3: внутри разрыва — пусто")
check(stg.value_at(U(12, 4, 30)) is None and stg.value_at(U(12, 5)) == 600, "3: значение до разрыва через разрыв не переносится")
mn, av, mx = st.bucket(U(12, 0, 0), U(12, 1, 0))
check(mn == 400 and mx == 500 and abs(av - 450) < 1e-9, f"3: ячейка мин/ср.по времени/макс: {mn} {av} {mx}")
mn, av, mx = st.bucket(U(12, 0, 20), U(12, 0, 40))
check(abs(av - 450) < 1e-9 and mn == 400, "3: среднее по времени с удержанным значением на входе ячейки")
check(st.bucket(U(11), U(11, 30)) is None, "3: ячейка до первой записи — неизвестно")
rs = resample({"P": pts}, U(12), U(12, 10), 150)
check(rs.mode == "raw" and [t for t in rs.times] == [U(12), U(12, 0, 30), U(12, 5)] and rs.cells["P"] == [400, 500, 600],
      "3: сырой режим — метки записей")
check(rs.summary["P"]["min"][1] == 400 and rs.summary["P"]["max"][1] == 600 and rs.n_raw["P"] == 3, "3: сводка")
many = [(U(12) + timedelta(seconds=i * 10), float(i)) for i in range(360)]   # час, 360 точек
rs2 = resample({"X": many}, U(12), U(13), 60)
check(rs2.mode == "bucket" and rs2.step_sec == 60 and len(rs2.times) == 60, f"3: укрупнение до круглого шага: {rs2.step_sec} {len(rs2.times)}")
c0 = rs2.cells["X"][0]
check(c0 is not None and c0[0] == 0 and c0[2] == 5 and abs(c0[1] - 2.5) < 1e-9, f"3: ячейка укрупнения: {c0}")
check(nice_step(7) == 10 and nice_step(61) == 120 and nice_step(100000) == 172800, "3: круглый шаг")

# ── 4. Лента ─────────────────────────────────────────────────────────────────
from telemetry_mcp.tools_timeline import _kinds, classify, debounce

check(classify({"kind": "state", "addr": 40011, "role": "RUN_STATE"}, None) == "mode", "4: 40011 — режим")
check(classify({"kind": "state", "addr": 40010, "role": "SWITCH_POS"}, None) == "personnel", "4: переключатель — персонал")
check(classify({"kind": "state", "addr": 40599, "role": "RUN_COMMAND"}, None) == "personnel", "4: команда — персонал")
check(classify({"kind": "state", "addr": 40569, "role": "FAULT_RESET"}, None) == "personnel", "4: сброс — персонал")
check(classify({"kind": "state", "addr": 43562, "role": "GENSET_CB_POS"}, None) == "personnel", "4: автомат — персонал")
check(classify({"kind": "state", "addr": 40013, "role": "LAST_FAULT_TYPE"}, None) == "state", "4: прочее — состояние")
check(classify({"kind": "fault", "addr": 40400, "bit": 1}, None) == "fault", "4: фронт — неисправность")
flap = [{"ts": U(12, 3, 0) + timedelta(seconds=7 * i), "end": None, "kind": "state", "addr": 40012, "name": "Код",
         "value": i % 2, "label": str(i % 2)} for i in range(37)]
other = [{"ts": U(12, 0), "end": None, "kind": "state", "addr": 40011, "name": "Режим", "value": 3, "label": "Работа"},
         {"ts": U(12, 9), "end": None, "kind": "state", "addr": 40011, "name": "Режим", "value": 0, "label": "Стоп"}]
out = debounce(flap + other, 60, 3)
check(len(out) == 3, f"4: 37 переключений — одна строка, 2 режима остаются: {len(out)}")
d = [e for e in out if e.get("debounced")][0]
check(d["debounced"]["n"] == 37 and d["ts"] == U(12, 3, 0) and d["label"] == "0", "4: число повторов, первое время, последняя метка")
two = debounce(flap[:2] + other, 60, 3)
check(len(two) == 4 and not any(e.get("debounced") for e in two), "4: два переключения не схлопываются")
check(_kinds("режим, неисправности") == {"mode", "fault"} and _kinds(None) == {"mode", "state", "personnel", "fault", "link"}
      and _kinds("действия персонала, связь") == {"personnel", "link"}, "4: разбор видов")

# ── 5. KB ─────────────────────────────────────────────────────────────────────
from telemetry_mcp.kb import key_roles, nominal_for, resolve_params


class FakeCfg:
    register_map = {
        40034: {"role": "ACTIVE_POWER_TOTAL", "kind": "analog", "unit": "kW", "description": "Суммарная активная мощность", "trip_snapshot": True},
        40003: {"role": "FREQUENCY", "kind": "analog", "unit": "Hz", "description": "Частота генератора"},
        40040: {"role": "RPM", "kind": "analog", "unit": "rpm", "description": "Обороты двигателя"},
        40027: {"role": "CURRENT_L1", "kind": "analog", "unit": "A", "description": "Ток фазы L1"},
        40011: {"role": "RUN_STATE", "kind": "enum", "unit": "enum", "description": "Режим"},
        40045: {"role": "OIL_PRESS", "kind": "analog", "unit": "kPa", "description": "Давление масла"},
        40046: {"role": "OIL_TEMP", "kind": "analog", "unit": "°C", "description": "Температура масла"},
    }
    thresholds = {"frequency": {"passport": {"nominal_hz": 50.0}}, "engine_speed": {"controller": {"rated_rpm": 1500}},
                  "oil_pressure": {"passport": {"rated_min_kpa": 310}}}
    trip_snapshot_roles = ["ACTIVE_POWER_TOTAL"]

    def role_to_addr(self, role):
        for a, m in self.register_map.items():
            if m.get("role") == role:
                return a
        return None

    def sensor_step(self, role):
        return (8.0, "default") if role == "OIL_PRESS" else (None, "none")


CAT = {40034: {"name_ru": "Мощность общая", "name": "Genset Total kW", "unit": "kW", "kind": "analog", "states": None},
       40100: {"name_ru": "Температура воды на выходе", "name": "Coolant Out Temp", "unit": "°C", "kind": "analog", "states": None},
       40101: {"name_ru": "Температура воды на входе", "name": "Coolant In Temp", "unit": "°C", "kind": "analog", "states": None}}
cfg = FakeCfg()
p = resolve_params(cfg, CAT, ["мощность", "Гц", "RPM", "ток L1", "40045", "давление масла", "Обороты двигателя"])
check([x.addr for x in p] == [40034, 40003, 40040, 40027, 40045], f"5: псевдонимы, роль, адрес, русское имя, дубли убраны: {[x.addr for x in p]}")
check(p[0].label == "Активная мощность" and p[0].unit == "кВт" and p[4].step == 8.0 and p[4].unit == "кПа",
      f"5: подпись, единица по-русски, шаг датчика: {p[0].label} {p[0].unit} {p[4].step}")
check(resolve_params(cfg, CAT, ["воды на выходе"])[0].addr == 40100 and resolve_params(cfg, CAT, ["воды на выходе"])[0].source == "каталог",
      "5: подстрока по каталогу, когда в KB нет")
try:
    resolve_params(cfg, CAT, ["температура воды"])
    check(False, "5: неоднозначная подстрока — ошибка")
except ToolError as e:
    check("40100" in str(e) and "40101" in str(e), "5: список кандидатов в ошибке")
try:
    resolve_params(cfg, CAT, ["мощность", "частота", "обороты"], max_params=2)
    check(False, "5: предел параметров — ошибка")
except ToolError as e:
    check("разбейте" in str(e), "5: текст предела")
try:
    resolve_params(cfg, CAT, ["нейтрино"])
    check(False, "5: неизвестный параметр — ошибка")
except ToolError as e:
    check("не найден" in str(e), "5: текст неизвестного параметра")
check(nominal_for(cfg, "FREQUENCY") == (50.0, "frequency.passport.nominal_hz") and nominal_for(cfg, "RPM")[0] == 1500
      and nominal_for(cfg, "OIL_PRESS")[0] == 310 and nominal_for(cfg, "CURRENT_L1") is None, "5: номиналы из thresholds")
kr = key_roles(cfg)
check(kr[0] == "ACTIVE_POWER_TOTAL" and "RPM" in kr and "FREQUENCY" in kr and "OIL_PRESS" in kr and len(kr) == len(set(kr))
      and "COOLANT_TEMP" not in kr, f"5: ключевые роли только из карты, без повторов: {kr}")

# ── 6. Конверт ───────────────────────────────────────────────────────────────
from telemetry_mcp.envelope import Envelope, fmt_window, next_label, reset_labels

reset_labels()
check(next_label("A") == "T1" and next_label("A") == "T2" and next_label("B") == "T1" and next_label(None) == "T1",
      "6: метки по задаче")
env = Envelope(label="T3", tool="ряд", unit="ДЭС №3 (установка 7)", window_from=U(9), window_to=U(10, 30),
               layer="очищенный слой", units={"мощность": "кВт"}, dropped={"future": 2, "frozen": 0, "dups": 1},
               gaps=[(U(9, 41, 10), U(9, 43, 55))], truncation=["укрупнено до 45 с"], notes=["запись по изменению"])
txt = env.text()
check(txt.startswith("[T3] ДЭС №3 (установка 7) · ряд · 13.10.2026 "), f"6: шапка конверта: {txt.splitlines()[0]}")
check("МСК" in txt.splitlines()[0] and "слой: очищенный слой" in txt, "6: пояс и слой")
check("единицы: мощность кВт" in txt and "отброшено 3 строк (метка в будущем 2, дубли 1)" in txt, f"6: единицы и очистка: {txt}")
check("разрывы связи в окне" in txt and "(2 мин 45 с)" in txt, "6: разрывы с длительностью")
check("усечение: укрупнено до 45 с" in txt and "примечания: запись по изменению" in txt, "6: усечение и примечания")
e2 = Envelope(label="T1", tool="x", unit="y")
check("очистка" not in e2.text(), "6: без окна строка очистки не печатается")
e3 = Envelope(label="T1", tool="x", unit="y", window_from=U(9), window_to=U(10))
check("строк не отброшено, разрывов связи в окне нет" in e3.text(), "6: пустая очистка при окне")
check(fmt_window(U(9), U(10, 30)).count(".10.2026") == 1, "6: один день — дата один раз")
check(fmt_window(U(9), U(9)).startswith("момент 13.10.2026 "), f"6: момент: {fmt_window(U(9), U(9))}")
check(env.to_dict()["gaps"][0]["from"] == U(9, 41, 10).isoformat(), "6: структура конверта")

# ── 7. Отчёт и акт ───────────────────────────────────────────────────────────
from telemetry_mcp.tools_segments import _act_text, _first_line, _section_key, _split_sections

md = "# ДЭС №3 — Работа 13.10\nшапка текст\n\n## Сводка\nстрока сводки\n\n## Подсегменты\n| a |\n\n## Замечания к работе\nнет\n"
secs = _split_sections(md)
check([s[0] for s in secs] == ["ДЭС №3 — Работа 13.10", "Сводка", "Подсегменты", "Замечания к работе"], f"7: разделы: {[s[0] for s in secs]}")
check(_section_key("## Замечания к работе") == "notes" and _section_key("сводка") == "summary" and _section_key("Подсегменты") == "subs",
      "7: ключи разделов")
check(_first_line("\n## 🟡 Замечания — 2\nтекст") == "🟡 Замечания — 2", "7: первая строка сводки")
act = {"id": 35941, "t_start": U(6, 28, 53), "t_end": None, "incident_json": {
    "stop_ts": U(6, 28, 53).isoformat(), "stop_kind": "EMERGENCY", "character": "immediate", "failure": "work",
    "window": {"from": U(3, 45, 32).isoformat(), "to": U(6, 30).isoformat(), "anchor": U(3, 45, 32).isoformat()},
    "fall": {"label": "Работа", "t_start": U(6, 28, 33).isoformat(), "t_end": U(6, 28, 53).isoformat()},
    "standing": [{"name": "Не в автоматическом режиме", "since": U(21, 37, 52, day=17).isoformat(), "age_sec": 2451061.0}],
    "summary": [{"bit": 11, "addr": 40408, "kind": "fault", "name": "Общая авария", "count": 2,
                 "first": U(6, 26, 23).isoformat(), "last": U(6, 28, 53).isoformat()},
                {"name": "Готов к нагрузке", "count": 1, "first": U(6, 26, 3).isoformat(), "last": U(6, 26, 3).isoformat()}],
    "chronology": [{"ts": U(6, 28, 42).isoformat(), "kind": "state", "name": "Режим", "label": "Работа"},
                   {"ts": U(6, 28, 53).isoformat(), "kind": "fault", "name": "Разнос", "severity": "shutdown"}]},
    "warning_analyses": [{"t": U(7).isoformat(), "kind": "incident", "md": "Совет: причина — заброс оборотов."}]}
t = _act_text(act, 10, full=True)
check("Акт #35941" in t and "вид EMERGENCY" in t and "характер: immediate" in t and "отказ: работы" in t, f"7: шапка акта: {t.splitlines()[0]}")
check("окно акта:" in t and "сегмент падения: Работа" in t and "стоящие неисправности на момент стопа: Не в автоматическом режиме (с 18.10" in t
      and "680 ч" in t, f"7: окно, падение, стоящие: {t}")
check("Общая авария ×2 (09:26:23–09:28:53)" in t and "Готов к нагрузке ×1 (09:26:03)" in t and "лента акта: событий 2" in t
      and "[shutdown]" in t, f"7: сводка и лента: {t}")
act2 = {**act, "incident_json": {**act["incident_json"], "character": {"character": "immediate", "confidence": "high",
        "signals": {"run_state_before": 3, "run_command_path": "direct_estop", "passed_cooldown": False}}}}
t2 = _act_text(act2, 3, full=False)
check("характер: мгновенный (аварийный), уверенность высокая [до стопа — Работа; путь команды direct_estop; без охлаждения]" in t2,
      f"7: характер словарём: {t2.splitlines()[0]}")
check("заключение совета ИИ" in t and "заброс оборотов" in t, "7: заключение совета")
check("заключения совета ИИ по акту нет" in _act_text({**act, "warning_analyses": []}, 5, full=True), "7: без заключения")

# ── 8. Сервер и конфиг ───────────────────────────────────────────────────────
from telemetry_mcp.config import McpSettings, DEFAULT_LIMITS
from telemetry_mcp.server import build_server

st = McpSettings(sources={"default": "*", "hermes": ["equipment_tree", "state_at"], "closed": []})
check(st.allowed(None, "series") and st.allowed("claude-code", "series") and st.allowed("hermes", "state_at")
      and not st.allowed("hermes", "series") and not st.allowed("closed", "equipment_tree"), "8: разрешения по источнику")
check(st.limit("series_max_points") == 150 and st.limit("call_timeout_sec") == 20 and DEFAULT_LIMITS["result_max_tokens"] == 8000,
      "8: умолчания лимитов")
srv = build_server(st)
tools = asyncio.run(srv.list_tools())
names = sorted(t.name for t in tools)
check(names == sorted(["equipment_tree", "register_catalog", "series", "state_at", "timeline", "segments", "segment_report",
                       "episodes", "incident_acts", "trend", "kb_passport", "fault_code"]), f"8: 12 инструментов: {names}")
by = {t.name: t for t in tools}
props = {n: set((by[n].input_schema or {}).get("properties", {}).keys()) for n in by}
check(props["series"] == {"unit", "params", "t_from", "t_to", "at", "minutes", "points", "with_neighbors"}, f"8: аргументы series: {props['series']}")
check("ctx" not in props["series"] and props["segment_report"] == {"segment_id", "sections"}, "8: контекст не аргумент")
check(all("Установка" in (by[n].input_schema["properties"]["unit"].get("description") or "") for n in ("series", "timeline", "state_at")),
      "8: описания аргументов по-русски")
check(all(by[n].description and "." in by[n].description for n in by), "8: описания инструментов")

# ── 9. run_tool ──────────────────────────────────────────────────────────────
from telemetry_mcp import common
from telemetry_mcp.common import run_tool

common.SETTINGS = McpSettings(journal=False, limits={**DEFAULT_LIMITS, "call_timeout_sec": 0.2, "result_max_tokens": 50},
                              sources={"default": "*", "closed": []})


class Ctx:
    def __init__(self, headers=None):
        self.headers = headers or {}


async def ok_body(env):
    env.rows = 1
    return "тело"


async def err_body(env):
    raise ToolError("укажите установку")


async def slow_body(env):
    await asyncio.sleep(1)
    return "поздно"


async def big_body(env):
    return "\n".join(f"строка {i} " + "x" * 30 for i in range(40))


r = asyncio.run(run_tool("series", Ctx({"X-Task-Id": "job-1"}), {"unit": "ДЭС №3"}, ok_body))
check(r.startswith("[T") and "\n\nтело" in r, f"9: нормальный ответ — конверт + тело: {r[:60]}")
r = asyncio.run(run_tool("series", Ctx(), {}, err_body))
check(r == "ОШИБКА: укажите установку", f"9: ToolError → текст: {r}")
r = asyncio.run(run_tool("series", Ctx(), {}, slow_body))
check("не успел за 0.2 с" in r and "поздно" not in r, f"9: срок — частичный ответ с пометкой: {r}")
r = asyncio.run(run_tool("series", Ctx(), {}, big_body))
check("[обрезано]" in r and "результат обрезан" in r, "9: усечение по размеру")
r = asyncio.run(run_tool("series", Ctx({"x-source": "closed"}), {}, ok_body))
check(r.startswith("ОШИБКА") and "не разрешён" in r, "9: источник без прав")

if _errors:
    print(f"FAIL — {len(_errors)} расхождений:")
    for x in _errors:
        print(f"  • {x}")
    sys.exit(1)
print("Все проверки пройдены.")
