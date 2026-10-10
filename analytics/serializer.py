# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Сериализация результатов аналитики в JSON и Markdown.

JSON — полный машиночитаемый контракт.
Markdown — структурированный отчёт для человека и LLM (Этап 2).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .contract import Segment, Subsegment


# ── Вспомогательные ──────────────────────────────────────────────────────────

_SEVERITY_EMOJI = {
    "SHUTDOWN": "🔴",
    "WARNING":  "🟠",
    "CAUTION":  "🟡",
    "INFO":     "🔵",
}

# Человеческие названия сценариев аналитики (соответствуют detectors.yaml).
# Канонический словарь — corpus/preprocessor импортирует отсюда.
SCENARIO_RU: dict[str, str] = {
    "LOAD_STEP":          "Резкий наброс/сброс нагрузки",
    "NEGATIVE_SEQUENCE":  "Ток обратной последовательности",
    "PHASE_CURRENT_SPREAD": "Перекос фазных токов",
    "NEUTRAL_CURRENT":    "Ток нейтрали",
    "COOLING_FAILURE":    "Отклонения температуры охлаждающей жидкости",
    "OIL_DILUTION":       "Пониженное давление масла",
    "COKING_RISK":        "Длительная работа на малой нагрузке (риск нагара)",
    "WARMUP_VIOLATION":   "Нарушение прогрева",
    "COOLDOWN_VIOLATION": "Нарушение охлаждения перед остановом",
    "START_FAILURE":      "Проблема пуска",
    "THERMAL_HIGHLOAD":   "Перегрев под высокой нагрузкой",
    "LIMIT_PROXIMITY":    "Приближение к паспортному порогу",
    "STOP_PROFILE":       "Отклонение профиля останова",
    "RPM_UNDERSPEED":     "Просадка оборотов",
    "DETECTION_COUNTER":  "Повторные срабатывания за период",
    "CONTROLLER_FAULT":   "Неисправность по данным контроллера",
}

_SEVERITY_LABEL = {
    "SHUTDOWN": "АВАРИЯ (SHUTDOWN)",
    "WARNING":  "АВАРИЯ (WARNING)",
    "CAUTION":  "ПРЕДУПРЕЖДЕНИЕ АНАЛИТИКИ",
    "INFO":     "INFO",
}

_ZONE_RU = {
    "LOW": "Малая нагрузка",
    "NORMAL": "Нормальная нагрузка",
    "ELEVATED": "Повышенная нагрузка",
    "OVERLOAD": "Перегрузка",
    "NA": "Н/Д",
}

RUN_STATE_RU: dict[int, str] = {
    0: "Стоп",
    1: "Задержка пуска",
    2: "Прогрев",
    3: "Работа",
    4: "Разгрузка",
    5: "Охлаждение на х.х.",
    6: "Переход на х.х.",
}

_RISK_EMOJI = {"GREEN": "🟢", "YELLOW": "🟡", "RED": "🔴"}

# Тип последней неисправности (регистр 40013, enum PCC3300)
_FAULT_TYPE_RU: dict[int, str] = {
    0: "Нет",
    1: "Предупреждение (Warning)",
    2: "Снижение мощности (Derate)",
    3: "Останов с охлаждением (Shutdown with Cooldown)",
    4: "Немедленный останов (Shutdown)",
}


def _fmt_duration(sec: float) -> str:
    """Форматировать длительность в «Xч Yм Zс» (без нулевых компонентов)."""
    s = int(sec)
    h = s // 3600
    m = (s % 3600) // 60
    s = s % 60
    parts = []
    if h:
        parts.append(f"{h}ч")
    if m:
        parts.append(f"{m}м")
    if s or not parts:
        parts.append(f"{s}с")
    return " ".join(parts)


def _fmt_ts(iso: str | None, tz=None) -> str:
    """Форматировать ISO-метку в читаемую строку с учётом часового пояса."""
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo:
            target = tz if tz is not None else timezone.utc
            dt = dt.astimezone(target)
            label = getattr(tz, "key", "UTC") if tz is not None else "UTC"
            return dt.strftime(f"%Y-%m-%d %H:%M:%S {label}")
        return iso
    except ValueError:
        return iso


def _make_fmt_ts(tz):
    """Вернуть замыкание _fmt_ts с захваченным часовым поясом."""
    def _f(iso: str | None) -> str:
        return _fmt_ts(iso, tz)
    return _f


def _as_dict(d: Any) -> dict:
    """Convert Detection dataclass to dict if needed."""
    return d.to_dict() if hasattr(d, "to_dict") else d


def _max_severity(detections: list) -> str | None:
    order = ["SHUTDOWN", "WARNING", "CAUTION", "INFO"]
    found = {_as_dict(d)["severity"] for d in detections}
    for sev in order:
        if sev in found:
            return sev
    return None


# ── JSON ─────────────────────────────────────────────────────────────────────

def to_json(
    segments: list[Segment],
    router_sn: str,
    equip_type: str,
    panel_id: int,
    ts_from: datetime,
    ts_to: datetime,
    analytics_version: str = "2.0.0",
    indent: int | None = 2,
) -> str:
    """Сериализовать полный аналитический контракт в JSON-строку."""
    payload = {
        "analytics_version": analytics_version,
        "router_sn": router_sn,
        "equip_type": equip_type,
        "panel_id": panel_id,
        "ts_from": ts_from.isoformat(),
        "ts_to": ts_to.isoformat(),
        "segments_count": len(segments),
        "segments": [s.to_dict() for s in segments],
    }
    return json.dumps(payload, ensure_ascii=False, indent=indent, default=str)


# ── Markdown ──────────────────────────────────────────────────────────────────

_SEV_RANK_MD = {"SHUTDOWN": 4, "WARNING": 3, "CAUTION": 2, "INFO": 1}
_SRC_RU = {"panel": "панель", "analytics": "аналитика"}


SUMMARY_PARTS_VERSION = 1

# Уровень в частях сводки — те же значения, что alarm_level корпуса (догма v4.9.32)
_LEVEL_RANK = {"НОРМА": 0, "CAUTION": 1, "WARNING": 2, "SHUTDOWN": 3}


def build_summary_parts(
    segments: list[Segment],
    episodes: list[dict[str, Any]] | None = None,
    tz=None,
    trip_roles: list[str] | None = None,
) -> dict[str, Any]:
    """Сводка сегмента частями — единый источник вердикта (v5.1.0).

    Из частей печатается report_summary_md (render_summary_md) и их же читают
    цепочка ИИ (глубина разбора, шапка «не пересматривать», вердикт корпуса),
    календарь и карточка UI — раньше вердикт считался в трёх местах по-разному.

    Вердикт детерминированный (эпизоды + sequence-проверки), мнение ИИ —
    отдельными панелями. Эпизоды, отменённые гейтом, в вердикт не входят,
    но в замечаниях показываются с пометкой.

    Ключи: verdict {level, title, note, remarks_n, suppressed_n};
    end_level — что висит на конец окна (живое, не снятое ИИ): по нему красится
    стоп-сегмент в календаре (v4.9.73); mttr {kind, label, sec} | None;
    remarks — замечания по причинам, с фазами before / appeared / open;
    failed_checks; key_metrics {rows} | None; coking {level} | None; no_data.
    """
    episodes = episodes or []
    failed_checks = [
        c for s in segments for c in (getattr(s, "sequence_checks", None) or [])
        if isinstance(c, dict) and not c.get("passed", True)
    ]
    # Аварийный стоп — авария панели, даже если эпизода SHUTDOWN нет: 1452
    # «Отказ включения автомата» роняет панель в Shutdown без бита в масках,
    # и видно это только по типу последней неисправности (40013), который
    # решает вид стопа, но в детекцию не идёт.
    emergency = [s for s in segments if getattr(s, "stop_kind", None) == "EMERGENCY"]

    # Сегмент закрыт по устранению неисправностей → время до устранения (MTTR):
    # от первого фронта панельного кода до момента чистоты (границы сегмента).
    # SHUTDOWN_CLEARED — тот же рез в новой модели СТОПа: там граница ставится
    # по снятию аварийной маски, а не по полной чистоте (v4.9.74)
    mttr_part = None
    _cc = getattr(segments[-1], "cause_close", None) if segments else None
    if _cc in ("FAULT_CLEARED", "SHUTDOWN_CLEARED"):
        # Якорь зависит от того, ЧТО именно устранили. На резе по снятию
        # аварийной маски строка называется «Авария снята» — значит и мерить
        # надо от аварии, а не от предупреждения, случившегося раньше.
        # Наблюдалось 20.09: строка показывала 16м19с от предупреждения по
        # температуре ОЖ, тогда как авария (кнопка) длилась 15м39с.
        _anchor_sev = (("SHUTDOWN",) if _cc == "SHUTDOWN_CLEARED"
                       else ("SHUTDOWN", "WARNING"))
        panel_eps = [
            e for e in episodes
            if e.get("source") == "panel"
            and e.get("severity") in _anchor_sev
            and e.get("t_open")
        ]
        t_first = min((e["t_open"] for e in panel_eps), default=None)
        # Эпизода нет (авария без бита в масках) — якорь в начале аварийного
        # стопа: 40013 входит в Shutdown в одном кадре с остановом. Только у
        # головы, открытой самим остановом; продолжение после суточного реза
        # (REPORT_START) начала аварии не знает, и врать временем не стоит.
        if t_first is None and _cc == "SHUTDOWN_CLEARED":
            _heads = [s.t_start for s in emergency
                      if getattr(s, "cause_open", None) == "RUN_STATE_CHANGE"]
            if _heads:
                try:
                    t_first = datetime.fromisoformat(min(_heads))
                except (ValueError, TypeError):
                    t_first = None
        t_end_iso = getattr(segments[-1], "t_end", None)
        if t_first is not None and t_end_iso:
            try:
                t_end = datetime.fromisoformat(t_end_iso)
                if t_end.tzinfo is None:
                    t_end = t_end.replace(tzinfo=timezone.utc)
                if t_first.tzinfo is None:
                    t_first = t_first.replace(tzinfo=timezone.utc)
                mttr = (t_end - t_first).total_seconds()
                if mttr > 0:
                    mttr_part = {
                        "kind": _cc,
                        "label": ("Авария снята" if _cc == "SHUTDOWN_CLEARED"
                                  else "Неисправности устранены"),
                        "sec": mttr,
                    }
            except (ValueError, TypeError):
                pass

    # ── Замечания: агрегируем эпизоды по причине (поэпизодный список — в полном отчёте) ──
    seg_start = None
    if segments:
        try:
            seg_start = datetime.fromisoformat(segments[0].t_start)
            if seg_start.tzinfo is None:
                seg_start = seg_start.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError, AttributeError):
            seg_start = None

    def _group_key(e: dict):
        if e.get("source") == "panel":
            return ("panel", e.get("addr"), e.get("bit"))
        return ("analytics", e.get("scenario"))

    def _group_name(e: dict) -> str:
        if e.get("source") == "panel":
            vals = e.get("open_values_json")
            if isinstance(vals, str):
                try:
                    vals = json.loads(vals)
                except Exception:
                    vals = {}
            name = (vals or {}).get("fault_name")
            return name or f"Неисправность панели (addr={e.get('addr')}, bit={e.get('bit')})"
        sc = e.get("scenario") or "?"
        return SCENARIO_RU.get(sc, sc)

    # Что висит на конец окна — по данным последнего подсегмента, как раньше
    # красился простой стоп (v4.9.73, _seg_active_dets): висит маска, у которой
    # в последнем подсегменте есть детекция без снятого фронта (fault_end) —
    # снятая панелью уже не висит, хотя эпизод ещё в дебаунсе закрытия, а
    # поднятая снова после снятия висит; аналитика висит, только если её
    # детекция есть в последнем подсегменте
    last_sub = None
    if segments and getattr(segments[-1], "subsegments", None):
        last_sub = segments[-1].subsegments[-1]
    last_dets = getattr(last_sub, "detections", None) if last_sub is not None else None
    panel_live: set[tuple] | None = None
    analytics_at_end: set[str] | None = None
    if last_dets is not None:
        panel_live, analytics_at_end = set(), set()
        for d in last_dets:
            dd = d.to_dict() if hasattr(d, "to_dict") else d
            if dd.get("scenario") == "CONTROLLER_FAULT":
                v = dd.get("values") or {}
                if not v.get("fault_end"):
                    panel_live.add((v.get("addr"), v.get("bit")))
            else:
                analytics_at_end.add(dd.get("scenario"))

    groups: dict[tuple, dict] = {}
    for e in episodes:
        g = groups.setdefault(_group_key(e), {
            "name": _group_name(e), "source": e.get("source"),
            "scenario": e.get("scenario"),
            "sev_rank": 0, "severity": e.get("severity"),
            "count": 0, "active_sec": 0.0, "blind_sec": 0.0,
            "suppressed": 0, "open": 0, "open_live": 0, "before": False, "appeared": False,
            "at_end": False,
        })
        g["count"] += 1
        g["active_sec"] += e.get("active_sec") or 0
        g["blind_sec"] += e.get("blind_sec") or 0
        rank = _SEV_RANK_MD.get(e.get("severity") or "", 0)
        if rank > g["sev_rank"]:
            g["sev_rank"], g["severity"] = rank, e.get("severity")
        if e.get("gate_suppressed"):
            g["suppressed"] += 1
        if not e.get("t_close"):
            g["open"] += 1
            if not e.get("gate_suppressed"):
                # Открытые и не снятые — их и снимет поздний вердикт гейта
                # (set_episodes_gate_suppressed метит только открытые эпизоды)
                g["open_live"] += 1
                if e.get("source") == "panel":
                    hangs = panel_live is None or (e.get("addr"), e.get("bit")) in panel_live
                else:
                    hangs = analytics_at_end is None or e.get("scenario") in analytics_at_end
                if hangs:
                    g["at_end"] = True
        # Фаза относительно сегмента: было до начала / появилось в нём
        t_open = e.get("t_open")
        if isinstance(t_open, str):
            try:
                t_open = datetime.fromisoformat(t_open)
            except ValueError:
                t_open = None
        if isinstance(t_open, datetime) and seg_start is not None:
            if t_open.tzinfo is None:
                t_open = t_open.replace(tzinfo=timezone.utc)
            if t_open < seg_start:
                g["before"] = True
            else:
                g["appeared"] = True
    remarks = sorted(groups.values(), key=lambda g: (-g["sev_rank"], -g["active_sec"]))
    for g in remarks:
        g.pop("sev_rank", None)

    checks = []
    for c in failed_checks:
        det = c.get("details") or c.get("detail") or ""
        checks.append({"check": c.get("check"), "details": det})

    verdict, end_level = _verdict_from_remarks(remarks, checks, bool(emergency))

    # ── Ключевые показатели: trip_snapshot-роли последнего рабочего подсегмента ──
    key_metrics = None
    coking = None
    work_seg = next(
        (s for s in reversed(segments)
         if getattr(s, "run_state", None) == 3 and s.subsegments),
        None,
    )
    if work_seg and trip_roles:
        sub = work_seg.subsegments[-1]
        rows = [
            {"role": role, "median": sub.characteristics[role].get("median"),
             "min": sub.characteristics[role].get("min"),
             "max": sub.characteristics[role].get("max"),
             "unit": sub.characteristics[role].get("unit", "")}
            for role in trip_roles
            if isinstance(sub.characteristics.get(role), dict)
        ]
        if rows:
            key_metrics = {"rows": rows}
        coking = {"level": sub.risk_accumulators.coking_risk.risk_level}

    return {
        "version": SUMMARY_PARTS_VERSION,
        "verdict": verdict,
        "end_level": end_level,
        "mttr": mttr_part,
        "remarks": remarks,
        "has_remarks": bool(episodes or failed_checks),
        "failed_checks": checks,
        "emergency": bool(emergency),
        "key_metrics": key_metrics,
        "coking": coking,
        "no_data": False,
    }


# Проверки последовательности, которые говорят о качестве данных, а не о
# работе машины: замечанием к работе не считаются (решение 11.10, 2а) — строкой
# в «Замечаниях» остаются, на вердикт не влияют
_DATA_QUALITY_CHECKS = frozenset({"subseg_data_coverage"})


def _verdict_from_remarks(
    remarks: list[dict], checks: list[dict], emergency: bool,
) -> tuple[dict, str]:
    """Вердикт периода и уровень на конец окна — из групп замечаний.

    Из групп, а не из эпизодов, чтобы вердикт можно было пересчитать, когда
    поздняя отмена гейтом помечает замечание снятым (apply_gate_suppression).
    Живой панельный CAUTION («Общая авария» + несброшенный 40012) — замечание
    к работе (решение 11.10, 1а): раньше заголовок писал «НОРМА — замечаний нет»
    при 🟡 строке ниже.
    """
    def live(g):
        return g["count"] - g.get("suppressed", 0)

    panel = [g for g in remarks if g.get("source") == "panel" and live(g) > 0]
    shutdown = any(g.get("severity") == "SHUTDOWN" for g in panel)
    warning = any(g.get("severity") == "WARNING" for g in panel)
    n = (sum(live(g) for g in remarks if g.get("source") == "analytics")
         + sum(live(g) for g in panel if g.get("severity") not in ("SHUTDOWN", "WARNING"))
         + sum(1 for c in checks if c.get("check") not in _DATA_QUALITY_CHECKS))
    suppressed_n = sum(g.get("suppressed", 0) for g in remarks)

    if shutdown or emergency:
        verdict = {"level": "SHUTDOWN", "title": "🔴 АВАРИЯ — аварийный останов панели", "note": None}
    elif warning:
        verdict = {"level": "WARNING", "title": "🟠 ВНИМАНИЕ — предупреждение панели управления",
                   "note": None}
    elif n:
        verdict = {"level": "CAUTION", "title": f"🟡 ЗАМЕЧАНИЯ К РАБОТЕ — {n}", "note": None}
    elif suppressed_n:
        verdict = {"level": "НОРМА", "title": "🟢 НОРМА",
                   "note": f"Предупреждения аналитики ({suppressed_n}) сняты ИИ."}
    else:
        verdict = {"level": "НОРМА", "title": "🟢 НОРМА", "note": "Замечаний к работе нет."}
    verdict["remarks_n"] = n
    verdict["suppressed_n"] = suppressed_n

    end_level = "НОРМА"
    for g in remarks:
        if not g.get("at_end"):
            continue
        if g.get("source") == "panel":
            lvl = g.get("severity") if g.get("severity") in ("SHUTDOWN", "WARNING") else "CAUTION"
        else:
            lvl = "CAUTION"
        if _LEVEL_RANK[lvl] > _LEVEL_RANK[end_level]:
            end_level = lvl
    if emergency:
        end_level = "SHUTDOWN"
    return verdict, end_level


def apply_gate_suppression(parts: dict[str, Any], scenarios) -> dict[str, Any]:
    """Пометить снятыми аналитические замечания этих сценариев и пересчитать вердикт.

    Для поздней отмены гейтом: разбор вернулся после закрытия сегмента, его
    вердикт лёг в закрытую строку, а части там заморожены при закрытии.
    Несимметрию гейт не снимает (online.status_assembler.UNCANCELLABLE_SCENARIOS).
    """
    from online.status_assembler import UNCANCELLABLE_SCENARIOS
    sc = {x for x in scenarios if x not in UNCANCELLABLE_SCENARIOS}
    if not sc or not parts or parts.get("no_data"):
        return parts
    out = json.loads(json.dumps(parts))
    remarks = out.get("remarks") or []
    # Без отметок «на конец» уровень конца из групп не восстановить — прежний
    keep_end = not all("at_end" in g for g in remarks)
    for g in remarks:
        if g.get("source") == "analytics" and g.get("scenario") in sc:
            # Вердикт гейта касается открытых эпизодов: закрывшиеся раньше в
            # этом же сегменте остаются замечаниями, как и при живой пометке
            n = g.get("open_live")
            if n is None:
                n = g["count"] - g.get("suppressed", 0)
            g["suppressed"] = g.get("suppressed", 0) + n
            g["open_live"] = 0
            g["at_end"] = False
    out["verdict"], end_level = _verdict_from_remarks(
        remarks, out.get("failed_checks") or [], bool(out.get("emergency")))
    if not keep_end:
        out["end_level"] = end_level
    return out


def render_summary_md(parts: dict[str, Any]) -> str:
    """report_summary_md из частей сводки (build_summary_parts)."""
    lines: list[str] = []
    a = lines.append
    v = parts.get("verdict") or {}
    a(f"## {v.get('title', '')}")
    if v.get("note"):
        a(v["note"])

    m = parts.get("mttr")
    if m:
        a("")
        a(f"⏱ **{m['label']}** — время до устранения: "
          f"**{_fmt_duration(m['sec'])}** (от первого кода до сброса)")

    if parts.get("has_remarks"):
        a("")
        a("### Замечания")
        _eps_ru = lambda n: ("эпизод" if n % 10 == 1 and n % 100 != 11 else
                             "эпизода" if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14) else
                             "эпизодов")
        for g in parts.get("remarks") or []:
            emoji = _SEVERITY_EMOJI.get(g["severity"], "")
            src_ru = _SRC_RU.get(g["source"], g["source"] or "")
            line = (f"- {emoji} **{g['name']}** [{src_ru}]: {g['count']} {_eps_ru(g['count'])}, "
                    f"воздействие {_fmt_duration(g['active_sec'])}")
            # Слепая доля — обязательная оговорка: внутри неё мог быть сброс
            if g["blind_sec"] >= 1:
                line += (f" (из них {_fmt_duration(g['blind_sec'])} без связи — "
                         f"снятие могло произойти раньше)")
            if g["suppressed"]:
                line += (" — *снято ИИ*" if g["suppressed"] == g["count"]
                         else f" — *снято ИИ: {g['suppressed']} из {g['count']}*")
            if g["open"]:
                line += " — **активен на конец периода**"
            a(line)
        for c in parts.get("failed_checks") or []:
            det = c.get("details") if isinstance(c, dict) else c
            a(f"- ❗ {det}" if det else "- ❗ проверка не пройдена")

    km = parts.get("key_metrics")
    if km and km.get("rows"):
        a("")
        a("### Ключевые показатели")
        a("| Параметр | Медиана | Мин | Макс | Ед. |")
        a("|----------|--------:|----:|----:|-----|")
        for r in km["rows"]:
            a(f"| {r['role']} | {_fmt_val(r.get('median'))} | {_fmt_val(r.get('min'))} "
              f"| {_fmt_val(r.get('max'))} | {r.get('unit', '')} |")
    if parts.get("coking"):
        a("")
        a(f"Закоксовка: **{parts['coking']['level']}**")

    return "\n".join(lines).strip() + "\n"


def build_summary_md(
    segments: list[Segment],
    episodes: list[dict[str, Any]] | None = None,
    tz=None,
    trip_roles: list[str] | None = None,
) -> str:
    """Верхняя часть отчёта (report_summary_md): вердикт → замечания → показатели.

    Полные таблицы остаются в report_md — UI сворачивает его как «Технические
    данные». Отдельное поле вместо HTML <details>: react-markdown в
    UI-telemetry вырезает сырой HTML. Собирается из частей (build_summary_parts).
    """
    return render_summary_md(build_summary_parts(segments, episodes, tz, trip_roles))


def no_data_summary_parts() -> dict[str, Any]:
    """Части сводки сегмента без связи: вердикта нет, ИИ не нужен."""
    return {
        "version": SUMMARY_PARTS_VERSION, "verdict": None, "end_level": None,
        "mttr": None, "remarks": [], "has_remarks": False, "failed_checks": [],
        "emergency": False, "key_metrics": None, "coking": None, "no_data": True,
    }


def build_no_data_report(
    router_sn: str,
    equip_type: str,
    panel_id: int,
    t_start: datetime,
    t_end: datetime,
    tz=None,
    last_data_ts: "datetime | None" = None,
) -> tuple[str, str]:
    """Заглушка отчёта для сегмента без единой строки телеметрии (data_quality == 0).

    Возвращает (report_md, report_summary_md). Штатный to_markdown для таких
    сегментов не вызывается: метрики там строятся из преамбулы/forward-fill,
    т.е. из данных ДО обрыва связи, и читаются как актуальный анализ.
    """
    fmt_ts = _make_fmt_ts(tz)
    period = f"{fmt_ts(t_start.isoformat())} — {fmt_ts(t_end.isoformat())}"
    last_line = (
        f"Последние данные получены: {fmt_ts(last_data_ts.isoformat())}."
        if last_data_ts is not None
        else "Данных за период нет."
    )

    report_md = "\n".join([
        f"# Аналитический отчёт — ДГУ `{router_sn}` / панель {panel_id}",
        "",
        f"**Период анализа:** {period}",
        f"**Тип оборудования:** {equip_type}",
        "",
        "## ⚠ Связь с оборудованием отсутствовала весь период",
        "",
        "Телеметрия за период не поступала (качество данных 0%), анализ невозможен.",
        last_line,
        "",
    ])
    summary_md = (
        f"## ⚠ Нет связи\n\nСвязь с оборудованием отсутствовала весь период "
        f"({period}). {last_line}\n"
    )
    return report_md, summary_md


def to_markdown(
    segments: list[Segment],
    router_sn: str,
    equip_type: str,
    panel_id: int,
    ts_from: datetime,
    ts_to: datetime,
    analytics_version: str = "2.0.0",
    tz=None,
    prev_seg=None,
    fault_ref=None,
    inherited_run_state_sec: "dict[int, float] | None" = None,
) -> str:
    """Сформировать Markdown-отчёт.

    tz — объект часового пояса (например, из config.get_tz()); None → UTC.
    inherited_run_state_sec — накопленное время (сек) в каждом RS от предыдущих
    сегментов суточной цепочки (continued_from). Добавляется к RS=3 времени в сводке.
    """
    fmt_ts = _make_fmt_ts(tz)

    lines: list[str] = []
    a = lines.append

    # ── Заголовок ──
    a(f"# Аналитический отчёт — ДГУ `{router_sn}` / панель {panel_id}")
    a(f"")
    a(f"**Период анализа:** {fmt_ts(ts_from.isoformat())} — {fmt_ts(ts_to.isoformat())}")
    a(f"**Тип оборудования:** {equip_type}")
    a(f"**Версия аналитики:** {analytics_version}")
    a(f"")

    # ── Сводка ──
    all_detections: list[dict] = []
    total_running_sec = float((inherited_run_state_sec or {}).get(3, 0.0))
    total_stopped_sec = float((inherited_run_state_sec or {}).get(0, 0.0))
    total_elevated_sec = 0.0
    seg_dqs: list[float] = []

    for seg in segments:
        seg_dqs.append(seg.data_quality)
        if seg.run_state == 3:
            total_running_sec += seg.duration_sec
        elif seg.run_state == 0:
            total_stopped_sec += seg.duration_sec
        for sub in seg.subsegments:
            all_detections.extend(_as_dict(d) for d in sub.detections)
            tr = sub.risk_accumulators.thermal_risk
            total_elevated_sec += tr.elevated_zone_sec

    by_severity: dict[str, int] = {}
    for d in all_detections:
        by_severity[d["severity"]] = by_severity.get(d["severity"], 0) + 1

    avg_dq = sum(seg_dqs) / len(seg_dqs) if seg_dqs else 1.0
    max_sev = _max_severity(all_detections)

    a("## Сводка")
    a("")
    a(f"| Параметр | Значение |")
    a(f"|----------|----------|")
    a(f"| Сегментов | {len(segments)} |")
    a(f"| Время под нагрузкой (RUN_STATE=3) | {_fmt_duration(total_running_sec)} |")
    a(f"| Время в останове (RUN_STATE=0) | {_fmt_duration(total_stopped_sec)} |")
    a(f"| Время в зоне повышенной нагрузки | {_fmt_duration(total_elevated_sec)} |")
    a(f"| Всего обнаружений | {len(all_detections)} |")
    for sev in ["SHUTDOWN", "WARNING", "CAUTION", "INFO"]:
        cnt = by_severity.get(sev, 0)
        if cnt:
            a(f"| — {_SEVERITY_EMOJI.get(sev, '')} {_SEVERITY_LABEL.get(sev, sev)} | {cnt} |")
    a(f"| Качество данных (среднее) | {avg_dq:.1%} |")
    if max_sev:
        a(f"| Максимальный уровень тревоги | {_SEVERITY_EMOJI.get(max_sev, '')} {_SEVERITY_LABEL.get(max_sev, max_sev)} |")
    a("")

    # ── Быстрый список тревог ──
    alarm_detections = [d for d in all_detections if d["severity"] in ("SHUTDOWN", "WARNING")]
    if alarm_detections:
        a("## Тревоги")
        a("")
        for d in alarm_detections:
            emoji = _SEVERITY_EMOJI.get(d["severity"], "")
            ts_str = fmt_ts(d.get("t_detected"))
            a(f"- {emoji} **{d['scenario']}** @ {ts_str}: {d['trigger']}")
        a("")

    # ── Детали по сегментам ──
    a("## Сегменты")
    a("")

    for seg_idx, seg in enumerate(segments, 1):
        ps = segments[seg_idx - 2] if seg_idx >= 2 else prev_seg
        _append_segment(lines, seg, seg_idx, fmt_ts, prev_seg=ps, fault_ref=fault_ref)

    return "\n".join(lines)


def _append_segment(
    lines: list[str],
    seg: Segment,
    idx: int,
    fmt_ts,
    prev_seg: "Segment | None" = None,
    fault_ref=None,
) -> None:
    a = lines.append
    state_label = (
        seg.run_state_label
        or RUN_STATE_RU.get(seg.run_state, f"RUN_STATE={seg.run_state}")
    )
    dq_str = f"{seg.data_quality:.0%}"
    dur_str = _fmt_duration(seg.duration_sec)
    hours_str = (
        f" | Мото-часы: {seg.engine_hours_start:.0f} с ({seg.engine_hours_start / 3600:.0f} ч)"
        if seg.engine_hours_start is not None else ""
    )

    a(f"### Сегмент {idx} — {state_label} (RUN_STATE={seg.run_state})")
    a("")
    a(f"- **Начало:** {fmt_ts(seg.t_start)}")
    a(f"- **Конец:** {fmt_ts(seg.t_end)}")
    a(f"- **Длительность:** {dur_str}{hours_str}")
    a(f"- **Качество данных:** {dq_str}")
    # Предыдущее состояние
    if seg.cause_open == "RUN_STATE_CHANGE":
        if prev_seg is not None:
            prev_label = (
                prev_seg.run_state_label
                or RUN_STATE_RU.get(prev_seg.run_state, f"RUN_STATE={prev_seg.run_state}")
            )
            a(f"- **Предыдущее состояние:** ← {prev_label} (RUN_STATE={prev_seg.run_state})")
        else:
            a(f"- **Предыдущее состояние:** ← неизвестно (вне окна анализа)")
    elif seg.cause_open == "REPORT_START":
        if prev_seg is not None:
            prev_label = (
                prev_seg.run_state_label
                or RUN_STATE_RU.get(prev_seg.run_state, f"RUN_STATE={prev_seg.run_state}")
            )
            a(f"- **Предыдущее состояние:** ← {prev_label} (RUN_STATE={prev_seg.run_state}) [суточный рез]")
        else:
            a(f"- **Предыдущее состояние:** ← начало окна анализа")
    a(f"- **Причина открытия:** {seg.cause_open}")
    if seg.cause_close:
        a(f"- **Причина закрытия:** {seg.cause_close}")
    if seg.preamble_included:
        a(f"- **Преамбула включена:** да")
    a("")

    # Fault-события
    if seg.events:
        a("**События журнала:**")
        a("")
        for ev in seg.events:
            sev = ev.get("severity") or "?"
            name = ev.get("name_ru") or ev.get("name") or "Unknown"
            t = fmt_ts(ev.get("t"))
            dur = ev.get("duration_sec")
            dur_s = f" ({_fmt_duration(dur)})" if dur else ""
            a(f"- {_SEVERITY_EMOJI.get(sev, '')} `{name}` @ {t}{dur_s}")
        a("")

    # Активные неисправности + несброшенный код 40012 + коды из обнаружений
    if fault_ref:
        seen_codes: set[int] = set()

        # А. Активные на закрытие сегмента биты (warning и выше) из битмапов.
        # Сортировка по серьёзности; расшифровка из справочника — по имени бита
        # (у битов нет поля code, сопоставление по EN-имени покрывает часть).
        # INFO-биты (статусные) и закрывшиеся внутри окна остаются в «Событиях журнала».
        _raw_rank = {"shutdown": 0, "shutdown_cooldown": 0, "derate": 1, "warning": 1}
        active_bits: list[tuple[int, dict]] = []
        for ev in seg.events:
            if ev.get("type") != "FAULT":
                continue
            rank = _raw_rank.get((ev.get("severity") or "").lower())
            if rank is None:
                continue
            fe = ev.get("fault_end")
            if fe is not None and seg.t_end is not None and fe < seg.t_end:
                continue
            active_bits.append((rank, ev))
        active_bits.sort(key=lambda x: (x[0], x[1].get("t") or ""))

        if active_bits:
            a("**⚠ Активные неисправности (не сброшены на закрытие сегмента):**")
            a("")
            for rank, ev in active_bits:
                sev_scale = "SHUTDOWN" if rank == 0 else "WARNING"
                name_ru = ev.get("name_ru") or ev.get("name") or "?"
                a(f"- {_SEVERITY_EMOJI[sev_scale]} `{name_ru}` "
                  f"({ev.get('addr')}/{ev.get('bit')}) — активна с {fmt_ts(ev.get('t'))}")
                entry = fault_ref.lookup_by_name(ev.get("name") or "", ev.get("severity"))
                if entry and entry.get("code") is not None:
                    code = int(entry["code"])
                    if code not in seen_codes:
                        seen_codes.add(code)
                        desc = fault_ref.format_for_report(code)
                        if desc:
                            for line in desc.split("\n"):
                                a(f"  {line}")
            a("")

        # Б. Несброшенный код 40012 — смотрим В ПОСЛЕДНЮЮ очередь: в большинстве
        # случаев он дублирует активные битмапы (расшифрованы выше); только
        # несущественная ошибка (не отражённая в масках) живёт здесь одна.
        # Берём value_end, не median — если сброс нажали внутри окна, конец чист.
        # Регистр latched: сбрасывается только кнопкой после устранения причины.
        unacked_code = 0
        unacked_type: int | None = None
        for sub in reversed(seg.subsegments):
            lfc = sub.characteristics.get("LAST_FAULT_CODE") or {}
            raw_val = lfc.get("value_end")
            if raw_val is None:
                continue
            try:
                unacked_code = int(raw_val)
            except (ValueError, TypeError):
                unacked_code = 0
            lft = sub.characteristics.get("LAST_FAULT_TYPE") or {}
            try:
                unacked_type = int(lft["value_end"]) if lft.get("value_end") is not None else None
            except (ValueError, TypeError):
                unacked_type = None
            break

        if unacked_code > 0:
            if unacked_code in seen_codes:
                a(f"**Последний код неисправности (регистр 40012):** `{unacked_code}` — "
                  f"дублирует активную неисправность выше.")
                a("")
            else:
                seen_codes.add(unacked_code)
                type_str = ""
                if unacked_type is not None:
                    type_str = f" — тип: {_FAULT_TYPE_RU.get(unacked_type, f'код типа {unacked_type}')}"
                a(f"**⚠ Несброшенная неисправность:** код `{unacked_code}` (регистр 40012){type_str}")
                a("")
                a("Код не сброшен кнопкой сброса на панели — неисправность считается не устранённой.")
                a("")
                desc = fault_ref.format_for_report(unacked_code)
                if desc:
                    for line in desc.split("\n"):
                        a(line)
                    a("")

        # В. fault_codes из обнаружений
        codes_to_show: list[int] = []
        for sub in seg.subsegments:
            for _d in sub.detections:
                d = _as_dict(_d)
                for code in d.get("fault_codes") or []:
                    try:
                        c = int(code)
                        if c > 0 and c not in seen_codes:
                            codes_to_show.append(c)
                            seen_codes.add(c)
                    except (ValueError, TypeError):
                        pass

        if codes_to_show:
            a("**Справочник кодов неисправностей:**")
            a("")
            any_found = False
            for code in codes_to_show:
                desc = fault_ref.format_for_report(code)
                if desc:
                    for line in desc.split("\n"):
                        a(line)
                    a("")
                    any_found = True
            if not any_found:
                # Все коды отсутствуют в справочнике — удаляем пустой заголовок
                lines.pop()
                lines.pop()

    # Sequence checks
    failed_checks = [c for c in seg.sequence_checks if not c.get("passed")]
    if failed_checks:
        a("**Предупреждения по последовательности:**")
        a("")
        for c in failed_checks:
            a(f"- ⚠️ `{c['check']}`: {c.get('details', '')}")
        a("")

    # Подсегменты
    if len(seg.subsegments) > 1:
        a(f"**Подсегментов:** {len(seg.subsegments)}")
        a("")

    for sub_idx, sub in enumerate(seg.subsegments, 1):
        _append_subsegment(lines, sub, idx, sub_idx, fmt_ts, short=(len(seg.subsegments) == 1))


def _append_subsegment(
    lines: list[str],
    sub: Subsegment,
    seg_idx: int,
    sub_idx: int,
    fmt_ts,
    short: bool = False,
) -> None:
    a = lines.append
    zone_ru = _ZONE_RU.get(sub.load_zone, sub.load_zone)
    dur_str = _fmt_duration(sub.duration_sec)

    if not short:
        a(f"#### Подсегмент {seg_idx}.{sub_idx} — {zone_ru}")
        a("")
        a(f"| | |")
        a(f"|-|-|")
        a(f"| Начало | {fmt_ts(sub.t_start)} |")
        a(f"| Конец | {fmt_ts(sub.t_end)} |")
        a(f"| Длительность | {dur_str} |")
        a(f"| Качество данных | {sub.data_quality:.0%} |")
        a(f"| Причина открытия | {sub.cause_open} |")
        if sub.cause_close:
            a(f"| Причина закрытия | {sub.cause_close} |")
        a("")

    # Интервалы потери связи (слепые зоны) — где именно данных не было
    gaps = getattr(sub, "data_gaps", None) or []
    if gaps:
        # Суммарно — по объединению: разрывы перекрываются, сумма длин бывала
        # больше самого подсегмента (ДЭС №3: 7 ч 19 мин в подсегменте 5 ч 11 мин)
        from analytics.segmenter import union_seconds
        _spans = []
        for g in gaps:
            try:
                _spans.append((datetime.fromisoformat(g["start"]), datetime.fromisoformat(g["end"])))
            except (KeyError, TypeError, ValueError):
                pass
        total = (union_seconds(_spans) if len(_spans) == len(gaps)
                 else sum(g.get("duration_sec", 0) for g in gaps))
        a(f"**⚠ Потеря связи ({len(gaps)}, суммарно {_fmt_duration(total)}):**")
        a("")
        for g in gaps:
            a(f"- {fmt_ts(g.get('start'))} → {fmt_ts(g.get('end'))} "
              f"({_fmt_duration(g.get('duration_sec', 0))})")
        a("")

    # Характеристики
    if sub.characteristics:
        a("**Характеристики:**")
        a("")
        a("| Роль | Ед. | Медиана | Мин | Макс | Тренд/с |")
        a("|------|-----|---------|-----|------|---------|")
        for role, ch in sub.characteristics.items():
            med = _fmt_val(ch.get("median"))
            mn = _fmt_val(ch.get("min"))
            mx = _fmt_val(ch.get("max"))
            slope = _fmt_val(ch.get("slope"))
            unit = ch.get("unit", "")
            a(f"| {role} | {unit} | {med} | {mn} | {mx} | {slope} |")
        a("")

    # Derived metrics (только ненулевые)
    dm = sub.derived_metrics.to_dict()
    dm_nz = {k: v for k, v in dm.items() if v is not None}
    if dm_nz:
        a("**Производные метрики:**")
        a("")
        a("| Метрика | Значение |")
        a("|---------|----------|")
        for k, v in dm_nz.items():
            a(f"| `{k}` | {v} |")
        a("")

    # Риски
    cr = sub.risk_accumulators.coking_risk.to_dict()
    tr = sub.risk_accumulators.thermal_risk.to_dict()
    cr_lvl = cr["risk_level"]
    tr_lvl = tr["risk_level"]
    if cr_lvl != "GREEN" or tr_lvl != "GREEN":
        a("**Риски:**")
        a("")
        a(f"| Риск | Уровень | Детали |")
        a(f"|------|---------|--------|")
        if cr_lvl != "GREEN":
            details = (
                f"простой: {_fmt_duration(cr['idle_low_rpm_sec'])}, "
                f"ОЖ<60°C: {_fmt_duration(cr['coolant_below_60_sec'])}, "
                f"LOW зона: {_fmt_duration(cr['low_load_zone_sec'])}"
            )
            a(f"| Закоксование | {_RISK_EMOJI.get(cr_lvl, '')} {cr_lvl} | {details} |")
        if tr_lvl != "GREEN":
            details = f"ELEVATED зона: {_fmt_duration(tr['elevated_zone_sec'])}"
            a(f"| Тепловой | {_RISK_EMOJI.get(tr_lvl, '')} {tr_lvl} | {details} |")
        a("")

    # Обнаружения
    if sub.detections:
        a("**Обнаружения:**")
        a("")
        for _d in sub.detections:
            d = _as_dict(_d)
            emoji = _SEVERITY_EMOJI.get(d["severity"], "")
            a(f"- {emoji} **{d['scenario']}** ({d['severity']}): {d['trigger']}")
            a(f"  - Источник: `{d['source']}`")
            if d.get("fault_codes"):
                a(f"  - Коды: {d['fault_codes']}")
            count_30d = (d.get("values") or {}).get("history_count_30d")
            if count_30d is not None:
                dur_30d = (d.get("values") or {}).get("history_duration_30d_sec")
                blind_30d = (d.get("values") or {}).get("history_blind_30d_sec")
                suffix = f" (суммарно {_fmt_duration(dur_30d)})" if dur_30d else ""
                if dur_30d and blind_30d:
                    suffix = (f" (суммарно {_fmt_duration(dur_30d)}, из них "
                              f"{_fmt_duration(blind_30d)} без связи)")
                a(f"  - Срабатываний этого типа за 30 дней: **{count_30d}**{suffix}")
            startup_count = (d.get("values") or {}).get("startup_count")
            if startup_count is not None:
                dur_startup = (d.get("values") or {}).get("startup_duration_sec")
                suffix = f" (суммарно {_fmt_duration(dur_startup)})" if dur_startup else ""
                a(f"  - Срабатываний с пуска: **{startup_count}**{suffix}")
        a("")
    elif not short:
        a("*Обнаружений нет.*")
        a("")


def _fmt_val(v: Any, decimals: int = 2) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):.{decimals}f}"
    except (TypeError, ValueError):
        return str(v)


# ── Сводная статистика (для БД) ───────────────────────────────────────────────

def build_run_summary(segments: list[Segment]) -> dict[str, Any]:
    """Вычислить сводные числа для записи в analysis_runs."""
    all_det: list[dict] = []
    dqs: list[float] = []
    for seg in segments:
        dqs.append(seg.data_quality)
        for sub in seg.subsegments:
            all_det.extend(_as_dict(d) for d in sub.detections)

    max_sev = _max_severity(all_det)
    avg_dq = round(sum(dqs) / len(dqs), 3) if dqs else 1.0

    return {
        "segments_count": len(segments),
        "detections_count": len(all_det),
        "max_severity": max_sev,
        "data_quality_avg": avg_dq,
    }
