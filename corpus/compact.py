"""Сжатый вход заключения для раздутых суток (v5.1.29, решения В-29.1–В-29.3).

ДЭС №3 28–29.10: 141 и 125 подсегментов нагрузки (циклическая нагрузка через
границу зон), вход ИИ ~437 тыс. символов — 99 % это таблицы подсегментов по
~3 тыс. символов. Шлюз держал такой вход 30 мин и отказывал (v5.1.28 ставит
предел). Здесь вход сжимается кодом, без модели: шапка (вердикт, акт, разборы
гейта, хронология, Сводка с обнаружениями) — целиком, подсегменты — сводкой.

Ступени (В-29.2), от полной к самой короткой:
  4 — агрегаты по зонам + почасовая лента + полные таблицы подсегментов, где
      впервые появилось замечание (не больше _MAX_FULL), первого и последнего
      + строка на подсегмент;
  3 — то же без строк подсегментов;
  2 — только агрегаты по зонам и почасовая лента.
Сжимает вызывающий (corpus/worker) для тех моделей цепочки, у которых полный
вход не влезает в предел (В-29.1); диспетчер видит строку под заключением
(В-29.3).
"""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

LEVELS = (4, 3, 2)

_MARK = "**Подсегментов:**"
_BLOCK = re.compile(r"^#### Подсегмент (\d+\.\d+) ", re.M)
# Замечание в такой доле подсегментов — общее (висящая Общая авария): построчно
# не помечается, а называется один раз
_CHRONIC_SHARE = 0.8
# Полных таблиц подсегментов не больше стольких (с первым и последним): у
# старых отчётов и у части детекторов метка обнаружения своя в каждом
# подсегменте — без предела «полностью» попадали все (#35193: 80 из 80)
_MAX_FULL = 8
# Служебные показатели, счётчики и паспорт — медиана по зоне у них ничего не
# значит (время включения контроллера, наработка, расход с начала, пуски)
_SKIP_ROLES = {"HEARTBEAT", "ENGINE_HOURS", "FUEL_TOTAL", "START_ATTEMPTS_TOTAL",
               "START_SUCCESS_TOTAL", "RATING_KW", "RATING_KVA"}


def subsegments(segment_row: dict) -> list[dict]:
    """Подсегменты из characteristics_json (dict или строка JSON)."""
    cj = segment_row.get("characteristics_json")
    if isinstance(cj, str):
        try:
            cj = json.loads(cj)
        except ValueError:
            return []
    return list((cj or {}).get("subsegments") or []) if isinstance(cj, dict) else []


def _utc(s: Any) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(s))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _num(v: Any) -> str:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return "—"
    return f"{x:.2f}".rstrip("0").rstrip(".") if abs(x) < 1e6 else f"{x:.0f}"


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def _zone_ru(z: str) -> str:
    from analytics.serializer import _ZONE_RU
    return _ZONE_RU.get(z, z)


def _subs_word(n: int) -> str:
    from analytics.serializer import _plural
    return f"{n} {_plural(n, 'подсегмент', 'подсегмента', 'подсегментов')}"


def _det_names(sub: dict) -> list[str]:
    from analytics.serializer import _det_name
    return sorted({_det_name(d) for d in (sub.get("detections") or []) if isinstance(d, dict)})


def _gaps(sub: dict) -> list[tuple[datetime, datetime]]:
    """Разрывы связи подсегмента; зона «Н/Д» без разрывов — весь подсегмент."""
    out = []
    for g in sub.get("data_gaps") or []:
        a, b = _utc((g or {}).get("start")), _utc((g or {}).get("end"))
        if a and b and b > a:
            out.append((a, b))
    if not out and sub.get("load_zone") == "NA":
        a, b = _utc(sub.get("t_start")), _utc(sub.get("t_end"))
        if a and b and b > a:
            out.append((a, b))
    return out


def _zone_table(subs: list[dict], zone: str) -> list[str]:
    """Показатели зоны: медиана — средняя медиан подсегментов по времени,
    минимум и максимум — по всем подсегментам зоны."""
    from analytics.labels import ROLE_LABELS
    acc: dict[str, dict] = {}
    for s in subs:
        if s.get("load_zone") != zone:
            continue
        w = float(s.get("duration_sec") or 0)
        for role, c in (s.get("characteristics") or {}).items():
            if (role in _SKIP_ROLES or not isinstance(c, dict) or c.get("kind") == "state"
                    or c.get("median") is None):
                continue
            try:
                med = float(c["median"])
            except (TypeError, ValueError):
                continue
            r = acc.setdefault(role, {"w": 0.0, "mw": 0.0, "min": None, "max": None,
                                      "unit": c.get("unit") or ""})
            r["w"] += w
            r["mw"] += w * med
            for k, f in (("min", min), ("max", max)):
                if c.get(k) is not None:
                    r[k] = c[k] if r[k] is None else f(r[k], c[k])
    rows = [f"| {ROLE_LABELS.get(role, role)} | {r['unit']} | {_num(r['mw'] / r['w'])} | "
            f"{_num(r['min'])} | {_num(r['max'])} |" for role, r in acc.items() if r["w"] > 0]
    if not rows:
        return ["Данных нет (без связи)."]
    return ["| Показатель | Ед. | Медиана (средняя по времени) | Мин | Макс |",
            "|---|---|---|---|---|"] + rows


def _hourly(subs: list[dict], tz) -> list[str]:
    """Почасовая лента: время подсегментов разрезано по часам, все часы подряд,
    доля малой нагрузки и доля без данных — от времени часа в сегменте."""
    hours: dict[datetime, list[float]] = {}   # начато, всего, «Малая», без данных

    def spread(a: datetime, b: datetime, k: int) -> None:
        while a < b:
            h = a.astimezone(tz).replace(minute=0, second=0, microsecond=0)
            nxt = min(b, (h + timedelta(hours=1)).astimezone(timezone.utc))
            hours.setdefault(h, [0, 0.0, 0.0, 0.0])[k] += (nxt - a).total_seconds()
            a = nxt

    for s in subs:
        a, b = _utc(s.get("t_start")), _utc(s.get("t_end"))
        if not (a and b and b > a):
            continue
        hours.setdefault(a.astimezone(tz).replace(minute=0, second=0, microsecond=0),
                         [0, 0.0, 0.0, 0.0])[0] += 1
        spread(a, b, 1)
        if s.get("load_zone") == "LOW":
            spread(a, b, 2)
        for ga, gb in _gaps(s):
            spread(max(a, ga), min(b, gb), 3)
    if not hours:
        return []
    out = ["| Час | Начато подсегментов | Малая нагрузка | Без данных |", "|---|---|---|---|"]
    h, last = min(hours), max(hours)
    while h <= last:
        n, tot, low, nd = hours.get(h, [0, 0.0, 0.0, 0.0])
        pct = (lambda x: f"{100 * x / tot:.0f} %") if tot else (lambda x: "—")
        out.append(f"| {h.strftime('%d.%m %H:00')} | {n:.0f} | {pct(low)} | {pct(nd)} |")
        h = (h + timedelta(hours=1)).astimezone(tz).replace(minute=0, second=0, microsecond=0)
    return out


def _blocks(report_part: str) -> dict[str, str]:
    """Полные таблицы подсегментов отчёта по номеру «1.N»."""
    ms = list(_BLOCK.finditer(report_part))
    return {m.group(1): report_part[m.start():(ms[i + 1].start() if i + 1 < len(ms) else len(report_part))]
            for i, m in enumerate(ms)}


def _pick_full(subs: list[dict]) -> tuple[list[int], int]:
    """Номера подсегментов для полных таблиц: первый, последний и те, где
    впервые появился пункт свёртки «Обнаружений» (_det_group_key), — не больше
    _MAX_FULL. Второе — сколько таких не поместилось."""
    from analytics.serializer import _det_group_key
    n = len(subs)
    seen: set = set()
    firsts: list[int] = []
    for k, s in enumerate(subs):
        for d in (s.get("detections") or []):
            if not isinstance(d, dict):
                continue
            try:
                key = _det_group_key(d)
            except Exception:
                key = (d.get("scenario"),)
            if key not in seen:
                seen.add(key)
                if k not in firsts:
                    firsts.append(k)
    ends = {0, n - 1}
    middle = [k for k in firsts if k not in ends]
    room = max(0, _MAX_FULL - len(ends))
    return sorted(ends | set(middle[:room])), max(0, len(middle) - room)


def compact_input(full_input: str, segment_row: dict, level: int) -> str | None:
    """Сжатый вход заданной ступени; None — сжать нечем (нет подсегментов или
    отчёт не совпадает с characteristics_json) — вызывающий оставит полный."""
    from analytics.labels import _local_tz, fmt_duration, fmt_local, tz_label

    # Метка — только в самом отчёте (он идёт последним): в разборах гейта и
    # хронологии перед ним её быть не должно, но шапку резать по ним нельзя
    report = segment_row.get("report_md") or ""
    start = len(full_input) - len(report) if report and full_input.endswith(report) else 0
    i = full_input.find(_MARK, start)
    subs = subsegments(segment_row)
    if i < 0 or len(subs) < 2:
        return None
    head, tail = full_input[:i], full_input[i:]
    blocks = _blocks(tail)
    nums = list(blocks)
    if len(nums) != len(subs):
        return None
    n = len(subs)
    tz = _local_tz() or timezone.utc
    tzl = tz_label()

    names = [_det_names(s) for s in subs]
    cnt = Counter(x for ns in names for x in ns)
    chronic = sorted(k for k, v in cnt.items() if v >= _CHRONIC_SHARE * n)
    pick, over = _pick_full(subs)

    zt: Counter = Counter()
    zn: Counter = Counter()
    for s in subs:
        zt[s.get("load_zone")] += float(s.get("duration_sec") or 0)
        zn[s.get("load_zone")] += 1

    parts = ["вход сжат кодом: таблицы подсегментов сведены по зонам нагрузки (медиана — средняя "
             "по времени, минимум и максимум — по всем подсегментам зоны), почасовая лента"]
    if level >= 3:
        parts.append("полные таблицы — только у первого, последнего и подсегментов, где впервые "
                     "появилось замечание" + (f" (ещё {_subs_word(over)} с новыми замечаниями — "
                                               "без полной таблицы)" if over else ""))
    if level >= 4:
        parts.append("по строке на каждый подсегмент")
    a = [f"{_MARK} {n} — " + "; ".join(f"{_zone_ru(z)}: {_subs_word(zn[z])}, {fmt_duration(zt[z])}"
                                       for z in zt) + ".",
         "",
         "*" + _cap("; ".join(parts)) + ". Остальные таблицы есть в отчёте сегмента — "
         "о подсегментах, которых здесь нет, выводов не делай.*",
         ""]
    if chronic:
        a += [f"Во всех или почти во всех подсегментах: {', '.join(chronic)}.", ""]
    for z in zt:
        a += [f"**{_zone_ru(z)} ({_subs_word(zn[z])}, {fmt_duration(zt[z])}):**", ""] + _zone_table(subs, z) + [""]
    hl = _hourly(subs, tz)
    if hl:
        a += [f"**Почасовая лента ({tzl}):**", ""] + hl + [""]

    if level >= 3:
        a += ["**Полностью — первый, последний и подсегменты, где впервые появилось замечание:**", ""]
        a += [blocks[nums[k]].rstrip("\n") + "\n" for k in pick]

    if level >= 4:
        a += ["**Все подсегменты по строке:**", "",
              f"| № | Начало ({tzl}) | Длительность | Зона | Нагрузка, медиана (мин–макс), % | Замечания |",
              "|---|---|---|---|---|---|"]
        for k, s in enumerate(subs):
            c = (s.get("characteristics") or {}).get("LOAD_PCT") or {}
            marks = [x for x in names[k] if x not in chronic]
            gs = sum((gb - ga).total_seconds() for ga, gb in _gaps(s))
            if gs > 0:
                marks.append(f"⚠ без связи {fmt_duration(gs)}")
            a.append(f"| {nums[k]} | {fmt_local(s.get('t_start'), tz, day=True) or '—'} | "
                     f"{fmt_duration(s.get('duration_sec'))} | {_zone_ru(s.get('load_zone'))} | "
                     f"{_num(c.get('median'))} ({_num(c.get('min'))}–{_num(c.get('max'))}) | {', '.join(marks)} |")
        a.append("")
    return head + "\n".join(a)


def compact_note(info: dict) -> str:
    """Строка под заключением для диспетчера (В-29.3) — пишет код, не модель."""
    from analytics.serializer import _plural
    n = int(info.get("subsegments") or 0)
    tail = ("полные таблицы ИИ видел только у первого, последнего и там, где появились замечания"
            if int(info.get("level") or 0) >= 3 else "таблицы подсегментов ИИ не видел")
    return (f"*Вход сжат: {n} {_plural(n, 'подсегмент сведён', 'подсегмента сведены', 'подсегментов сведены')} "
            f"по зонам нагрузки; {tail}.*")


def with_note(conclusion: str, note: str) -> str:
    """Строка сжатия — отдельным абзацем перед подписью модели («---» и
    «*Модель:»): страница .194 обрезает всё после подписи, а без пустой строки
    markdown склеивает её с последним абзацем, пунктом списка или таблицей."""
    body = (conclusion or "").rstrip("\n")
    idx = body.rfind("\n---\n*Модель:")
    if idx >= 0:
        return body[:idx].rstrip("\n") + "\n\n" + note + "\n" + body[idx:] + "\n"
    return body + "\n\n" + note + "\n"
