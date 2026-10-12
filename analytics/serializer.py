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
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from .contract import Segment, Subsegment, cap_severity


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
    "COKING_RISK":        "Риск нагара (недогруз до прожига)",
    "IDLE_TOO_LONG":      "Длительный холостой ход",
    "NO_LOAD_TOO_LONG":   "Работа без нагрузки дольше нормы",
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
# Сырые уровни масок панели в журнале (fault_bitmap_map)
_RAW_SEV_EMOJI = {"shutdown": "🔴", "shutdown_cooldown": "🔴", "warning": "🟠", "derate": "🟠"}
_SRC_RU = {"panel": "панель", "analytics": "аналитика"}


SUMMARY_PARTS_VERSION = 2   # v5.1.6: воздействие за период / с начала, показатели за сегмент

# Уровень в частях сводки — те же значения, что alarm_level корпуса (догма v4.9.32)
_LEVEL_RANK = {"НОРМА": 0, "CAUTION": 1, "WARNING": 2, "SHUTDOWN": 3}


_COKING_LEVEL_RU = {"GREEN": "низкий", "YELLOW": "повышенный", "RED": "высокий"}


def _fmt_span_dm(t_open, t_close, tz=None) -> str:
    """«11.10 20:55–21:01» (через сутки — с датой конца), открытый — «с 11.10 20:55»."""
    a = _dt(t_open)
    b = _dt(t_close)
    if a is None:
        return "?"
    if tz is not None:
        a = a.astimezone(tz)
        b = b.astimezone(tz) if b is not None else None
    if b is None:
        return "с " + a.strftime("%d.%m %H:%M")
    end = b.strftime("%H:%M") if b.date() == a.date() else b.strftime("%d.%m %H:%M")
    return f"{a.strftime('%d.%m %H:%M')}–{end}"


def _fmt_dm(iso: str | None, tz=None) -> str | None:
    """«06.07» / «29.09 16:06» в местном поясе."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if tz is not None:
        dt = dt.astimezone(tz)
    return dt.strftime("%d.%m %H:%M")


def _coking_part(segments: list, tz=None) -> dict[str, Any] | None:
    """Риск нагара за период — накопленное состояние, а не событие (v5.1.3).

    На начало — Segment.coking_start, на конец — накопитель последнего
    подсегмента. Прирост и погашение за период — по монотонным счётчикам.
    Замечание — только где долг вырос и уровень не низкий, и не в стопе: на
    стоящей машине риск не меняется, его показывают «перед пуском» (решение
    11.10.2026: вердикт стопа «Норма» + отдельная метка).
    """
    end = None
    for s in reversed(segments or []):
        if getattr(s, "subsegments", None):
            cr = getattr(getattr(s.subsegments[-1], "risk_accumulators", None), "coking_risk", None)
            end = cr.to_dict() if hasattr(cr, "to_dict") else None
            break
    if end is None:
        return None
    start = getattr(segments[0], "coking_start", None) or None
    added = paid = None
    if isinstance(start, dict):
        added = max(0.0, float(end.get("debt_added_total_sec") or 0)
                    - float(start.get("debt_added_total_sec") or 0))
        paid = max(0.0, float(end.get("debt_paid_total_sec") or 0)
                   - float(start.get("debt_paid_total_sec") or 0))
    stop = all(getattr(s, "run_state", None) == 0 for s in segments)
    working = all(getattr(s, "run_state", None) == 3 for s in segments)
    level = end.get("risk_level") or "GREEN"
    # Чистый рост долга за период: начислено минус погашено. Валовое начисление
    # давало замечание и там, где прожиг долг уменьшил (09.10: −16 ч, CAUTION)
    net = (added - paid) if added is not None and paid is not None else None
    level_start = (start or {}).get("risk_level")
    # «Сейчас» для календаря — конец периода (у открытого — последний подсегмент)
    now_iso = getattr(segments[-1], "t_end", None)
    if not now_iso:
        for s in reversed(segments):
            if getattr(s, "subsegments", None):
                now_iso = s.subsegments[-1].t_end
                break
    purged_at = None
    lp = end.get("last_purge_ts")
    if lp and lp != (start or {}).get("last_purge_ts"):
        purged_at = lp
    return {
        "level": level,
        "level_start": level_start,
        "low_load_sec": float(end.get("low_load_zone_sec") or 0),
        "idle_sec": float(end.get("idle_low_rpm_sec") or 0),
        "cold_sec": float(end.get("coolant_below_60_sec") or 0),
        "debt_sec": float(end.get("debt_sec") or 0),
        "added_sec": added,
        "paid_sec": paid,
        "last_purge_ts": lp,
        "last_accum_ts": end.get("last_accum_ts"),
        "purged_at": purged_at,
        "purge_left_low_min": end.get("purge_left_low_min"),
        "purge_left_high_min": end.get("purge_left_high_min"),
        "purge_low_pct": end.get("purge_low_pct"),
        "purge_high_pct": end.get("purge_high_pct"),
        "stop": stop,
        "before_start": stop and level != "GREEN",
        "net_sec": net,
        # Замечание — только рабочий сегмент (решение 1), где риск вырос; в
        # прогреве/охлаждении за х.х. отвечает IDLE_TOO_LONG, накопление — строкой
        "remark": working and net is not None and net >= 60 and level != "GREEN",
        "since_text": _fmt_dm(lp, tz)[:5] if lp else None,
        "accum_until_text": _fmt_dm(end.get("last_accum_ts"), tz),
        "purged_at_text": _fmt_dm(purged_at, tz),
        # По Cummins (v5.1.5): эскалация и календарь прогонов
        "service_needed": bool(end.get("service_needed")),
        "calendar": _coking_calendar(end, now_iso, tz),
    }


def _days_between(a_iso: str | None, b_iso: str | None) -> float | None:
    try:
        a = datetime.fromisoformat(a_iso)
        b = datetime.fromisoformat(b_iso)
    except (TypeError, ValueError):
        return None
    if a.tzinfo is None:
        a = a.replace(tzinfo=timezone.utc)
    if b.tzinfo is None:
        b = b.replace(tzinfo=timezone.utc)
    return (b - a).total_seconds() / 86400.0


def _coking_calendar(cr: dict, now_iso: str | None, tz=None) -> dict[str, Any] | None:
    """Календарь прогонов по Cummins (T-030 §7.5.3/§7.5.5, A035B516): ежемесячный
    (≥30 мин при ≥30 %) и годовой (прогон T-030). Напоминание по ТО, на вердикт
    не влияет. Учёт — с v5.1.5, историю не пересчитываем: «нет сведений с DD.MM».
    """
    since = cr.get("calendar_since_ts")
    if not now_iso or not since:
        return None
    out: dict[str, Any] = {"since_text": (_fmt_dm(since, tz) or "")[:5]}
    for key, ts_key, period in (("monthly", "last_monthly_run_ts", 30),
                                ("annual", "last_annual_run_ts", 365)):
        last = cr.get(ts_key)
        days = _days_between(last or since, now_iso)
        out[key] = {
            "last_ts": last,
            "last_text": (_fmt_dm(last, tz) or "")[:5] if last else None,
            "days": round(days, 1) if days is not None else None,
            "period_days": period,
            "overdue": days is not None and days > period,
        }
    return out


def _fmt_hm(sec: float) -> str:
    """«4 ч 48 мин» — накопленное время риска без секунд."""
    m = int(round((sec or 0) / 60.0))
    h, m = divmod(m, 60)
    return f"{h} ч {m} мин" if h else f"{m} мин"


def _coking_composition(ck: dict) -> str:
    parts = []
    if ck.get("low_load_sec", 0) >= 60:
        parts.append(f"малая нагрузка {_fmt_hm(ck['low_load_sec'])}")
    if ck.get("idle_sec", 0) >= 60:
        parts.append(f"холостой ход {_fmt_hm(ck['idle_sec'])}")
    if ck.get("cold_sec", 0) >= 60:
        parts.append(f"холодная ОЖ {_fmt_hm(ck['cold_sec'])}")
    return ", ".join(parts) or "менее минуты"


_SERVICE_PURGE = ("сервисный прожиг по Cummins T-030 — не меньше 3 ч при нагрузке ≥30 %, "
                  "из них не меньше 1 ч при ≥75 %")
_SERVICE_ADVICE = ("Долг больше 32 ч — нужен сервисный прожиг (штатное лечение Cummins, "
                   "годовой прогон T-030): до набора нагрузки осмотреть выпуск, стыки и турбину на подтёки "
                   "масла и топлива, слить конденсат из выпуска; набирать нагрузку ступенями, "
                   "при нехватке — нагрузочным стендом: не меньше 3 ч, из них не меньше 1 ч "
                   "около номинала; взять пробу масла на топливо (ориентир Cummins — 5 %).")


def _coking_left(ck: dict) -> str | None:
    if ck.get("service_needed"):
        return _SERVICE_PURGE
    lo, hi = ck.get("purge_left_low_min"), ck.get("purge_left_high_min")
    plo, phi = ck.get("purge_low_pct"), ck.get("purge_high_pct")
    if not lo or not plo:
        return None
    s = f"~{lo:.0f} мин при нагрузке ≥{plo:.0f} %"
    if hi and phi:
        s += f" или ~{hi:.0f} мин при ≥{phi:.0f} %"
    return s


def _coking_accum_text(ck: dict) -> str:
    """«накоплен при работе с последнего прожига 06.07 по 29.09 16:06: …»."""
    since = (f"с последнего прожига {ck['since_text']}" if ck.get("since_text")
             else "с начала наблюдения")
    until = f" по {ck['accum_until_text']}" if ck.get("accum_until_text") else ""
    return f"накоплен при работе {since}{until}: {_coking_composition(ck)}"


def coking_state_text(ck: dict | None) -> str | None:
    """Риск нагара одной строкой — для шапки заключения и консоли (перенесённое состояние)."""
    if not ck or ck.get("level") in (None, "GREEN"):
        return None
    lvl = _COKING_LEVEL_RU.get(ck["level"], ck["level"])
    if "debt_sec" not in ck:   # части до v5.1.3: накопителей нет — без выдуманных подробностей
        return f"уровень {lvl} на конец периода"
    txt = f"уровень {lvl}; {_coking_accum_text(ck)}"
    if ck.get("stop"):
        txt += "; в этом сегменте установка стояла — риск не изменился (накоплен при прошлой работе)"
    elif (ck.get("net_sec") or 0) >= 60:
        txt += f"; за период долг до прожига вырос на ~{_fmt_hm(ck['net_sec'])} (в пересчёте на малую нагрузку)"
    if ck.get("service_needed"):
        txt += "; долг больше 32 ч — нужен сервисный прожиг"
    left = _coking_left(ck)
    if left:
        txt += f"; до сброса — {left}"
    return txt


def coking_calendar_text(ck: dict | None, only_overdue: bool = False) -> str | None:
    """Прогоны по Cummins одной строкой (напоминание по ТО, без влияния на вердикт)."""
    cal = (ck or {}).get("calendar")
    if not cal:
        return None
    parts = []
    for key, what in (("monthly", "ежемесячный (≥30 мин при ≥30 %)"),
                      ("annual", "годовой (≥3 ч, из них ≥1 ч при ≥75 %)")):
        m = cal.get(key) or {}
        if only_overdue and not m.get("overdue"):
            continue
        if m.get("last_text"):
            s = f"{what} — {m['last_text']}"
        else:
            s = f"{what} — нет сведений с {cal.get('since_text')}"
        if m.get("overdue"):
            s += f" ⚠ просрочен: {m['days']:.0f} дн. при норме {m['period_days']}"
        parts.append(s)
    return ("Прогоны по Cummins: " + "; ".join(parts)) if parts else None


def _dt(v) -> datetime | None:
    if isinstance(v, str):
        try:
            v = datetime.fromisoformat(v)
        except ValueError:
            return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    return None


def _merge_iv(iv: list[tuple]) -> list[tuple]:
    out: list[list] = []
    for s, e in sorted(x for x in iv if x[1] > x[0]):
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


def _iv_len(iv: list[tuple]) -> float:
    return sum((e - s).total_seconds() for s, e in iv)


def _iv_cross(a: list[tuple], b: list[tuple]) -> float:
    tot = 0.0
    for s1, e1 in a:
        for s2, e2 in b:
            lo, hi = max(s1, s2), min(e1, e2)
            if hi > lo:
                tot += (hi - lo).total_seconds()
    return tot


def _weighted_median(pairs: list[tuple]) -> float | None:
    """Нижняя взвешенная медиана [(значение, вес)]."""
    pts = sorted((float(v), float(w)) for v, w in pairs if v is not None and w and w > 0)
    if not pts:
        return None
    half, acc = sum(w for _, w in pts) / 2, 0.0
    for v, w in pts:
        acc += w
        if acc >= half:
            return v
    return pts[-1][0]


def _key_metrics_part(segments: list, trip_roles: list[str] | None) -> dict[str, Any] | None:
    """Ключевые показатели за весь период работы (v5.1.6; раньше — последний
    подсегмент: у ДЭС №3 «36,7 %» при сутках в основном на малой нагрузке).

    Медиана — по медианам подсегментов, взвешенная по времени с данными
    (длительность × качество); мин/макс — точные за период.
    """
    subs = [(sub, float(getattr(sub, "duration_sec", 0) or 0)
             * float(sub.data_quality if getattr(sub, "data_quality", None) is not None else 1.0))
            for s in segments if getattr(s, "run_state", None) == 3
            for sub in (getattr(s, "subsegments", None) or [])]
    subs = [(sub, w) for sub, w in subs if w > 0]
    if not subs or not trip_roles:
        return None
    rows = []
    for role in trip_roles:
        meds, mins, maxs, unit = [], [], [], ""
        for sub, w in subs:
            ch = (getattr(sub, "characteristics", None) or {}).get(role)
            if not isinstance(ch, dict) or ch.get("kind") == "state":
                continue
            meds.append((ch.get("median"), w))
            unit = ch.get("unit") or unit
            if ch.get("min") is not None:
                mins.append(ch["min"])
            if ch.get("max") is not None:
                maxs.append(ch["max"])
        if meds or mins:
            rows.append({"role": role, "median": _weighted_median(meds),
                         "min": min(mins, default=None), "max": max(maxs, default=None),
                         "unit": unit})
    if not rows:
        return None
    return {"rows": rows, "basis": "period", "work_sec": sum(w for _, w in subs),
            "subsegments": len(subs)}


def build_summary_parts(
    segments: list[Segment],
    episodes: list[dict[str, Any]] | None = None,
    tz=None,
    trip_roles: list[str] | None = None,
    t_from: datetime | None = None,
    t_to: datetime | None = None,
) -> dict[str, Any]:
    """Сводка сегмента частями — единый источник вердикта (v5.1.0).

    Из частей печатается report_summary_md (render_summary_md) и их же читают
    цепочка ИИ (глубина разбора, шапка «не пересматривать», вердикт корпуса),
    календарь и карточка UI — раньше вердикт считался в трёх местах по-разному.

    Вердикт детерминированный (эпизоды + sequence-проверки), мнение ИИ —
    отдельными панелями. Эпизоды, отменённые гейтом, в вердикт не входят,
    но в замечаниях показываются с пометкой.

    Ключи: verdict {level, title, note, remarks_n, suppressed_n, unconfirmed_n};
    end_level — что висит на конец окна (живое, не снятое ИИ): по нему красится
    стоп-сегмент в календаре (v4.9.73); mttr {kind, label, sec} | None;
    remarks — замечания по причинам, с фазами before / appeared / open;
    failed_checks; key_metrics {rows} | None; coking {level} | None; no_data.
    """
    # Риск нагара — состояние, у него своя часть (старые эпизоды до v5.1.3 не в счёт)
    from .contract import STATE_SCENARIOS
    # Старые эпизоды аналитики с WARNING (до v5.1.7) читаются по догме — CAUTION
    from .contract import EVENT_CHECKS, EVENT_SCENARIOS
    episodes = [dict(e, severity=cap_severity(e.get("scenario"), e.get("severity")))
                for e in (episodes or []) if e.get("scenario") not in STATE_SCENARIOS]
    # Разовые события перехода — отдельно: замечание строится из детекции сегмента,
    # эпизод (точечный) — только запасной источник (v5.1.10)
    event_eps = [e for e in episodes if e.get("scenario") in EVENT_SCENARIOS]
    episodes = [e for e in episodes if e.get("scenario") not in EVENT_SCENARIOS]
    failed_checks = [
        c for s in segments for c in (getattr(s, "sequence_checks", None) or [])
        if isinstance(c, dict) and not c.get("passed", True)
        and c.get("check") not in _HIDDEN_CHECKS
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

    # Окно сводки: воздействие и слепая доля — за период и отдельно «с начала»
    # (v5.1.6, решение A: «481 ч» эпизода в суточном сегменте ДГУ №2)
    w0 = _dt(t_from) or seg_start
    w1 = _dt(t_to) or (_dt(getattr(segments[-1], "t_end", None)) if segments else None)
    gap_iv = _merge_iv([(_dt(g.get("start")), _dt(g.get("end")))
                        for s in segments for sub in (getattr(s, "subsegments", None) or [])
                        for g in (getattr(sub, "data_gaps", None) or [])
                        if _dt(g.get("start")) and _dt(g.get("end"))])

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
                # Снятие позже конца окна (перепрогон, добор, запаздывание закрытия)
                # — на конец периода тревога ещё висела
                _fe, _end = _dt(v.get("fault_end")), _dt(getattr(last_sub, "t_end", None))
                if not _fe or (_end is not None and _fe >= _end):
                    panel_live.add((v.get("addr"), v.get("bit")))
            else:
                analytics_at_end.add(dd.get("scenario"))

    # Строки одного фронта — один эпизод (дубли до v5.1.1: «Общая авария» 8 строк на
    # один фронт; заголовок «ЗАМЕЧАНИЯ — n» считал строки, список — фронты)
    _by_onset: dict[tuple, dict] = {}
    for e in episodes:
        k = (_group_key(e), str(e.get("t_open")))
        cur = _by_onset.get(k)
        if cur is None:
            _by_onset[k] = dict(e)
            continue
        if not e.get("t_close") or not cur.get("t_close"):
            # открытая строка главнее: её состояние (снятие ИИ) действует сейчас
            if not e.get("t_close"):
                cur["gate_suppressed"] = e.get("gate_suppressed")
            cur["t_close"] = None
        else:
            cur["t_close"] = max(_dt(cur["t_close"]) or _dt(e["t_close"]), _dt(e["t_close"]) or _dt(cur["t_close"]))
        cur["active_sec"] = max(float(cur.get("active_sec") or 0), float(e.get("active_sec") or 0))
        cur["blind_sec"] = max(float(cur.get("blind_sec") or 0), float(e.get("blind_sec") or 0))
        if _SEV_RANK_MD.get(e.get("severity") or "", 0) > _SEV_RANK_MD.get(cur.get("severity") or "", 0):
            cur["severity"] = e.get("severity")
    episodes = list(_by_onset.values())

    # Подтверждение итоговым расчётом (решение 11.10, а): эпизод аналитики рождается
    # в живой обработке на подсегменте, который итоговая нарезка сегмента считает
    # иначе (COOLING_FAILURE, фаза 2: подсегмент только что перешёл порог 300 с) —
    # в детекциях сегмента его нет, заключение его не видит (гейт — как успел). Такой
    # — «по живым данным, в итоговом расчёте не подтверждено»: в замечаниях с пометкой,
    # в вердикт и счёт замечаний не идёт (ДЭС №3: 93 эпизода за 30 дней, 35765).
    # Проверки перехода (охлаждение, прогрев) не сверяются: их ставит межсегментная
    # проверка, а итоговый расчёт сегмента начинается с его головы и предыдущего
    # сегмента не видит — отсутствие детекции там ничего не опровергает (ревью v5.1.8:
    # 4 из 5 настоящих «без охлаждения» за 30 дней выпали бы из вердикта)
    _subs = [sub for s in segments for sub in (getattr(s, "subsegments", None) or [])]
    _sub_scs = [
        (_dt(getattr(sub, "t_start", None)), _dt(getattr(sub, "t_end", None)),
         {_as_dict(d).get("scenario") for d in (getattr(sub, "detections", None) or [])})
        for sub in _subs
    ]
    # Сверять не с чем — подсегменты без сведений о детекциях (не из сегментатора)
    _can_confirm = any(getattr(sub, "detections", None) is not None for sub in _subs)

    def _confirmed(e: dict) -> bool:
        if (not _can_confirm or e.get("source") == "panel"
                or e.get("scenario") == "CONTROLLER_FAULT"):
            return True
        a0, a1 = _dt(e.get("t_open")), _dt(e.get("t_close"))
        if w0 is not None and (a0 is None or a0 < w0):
            a0 = w0
        if w1 is not None and (a1 is None or a1 > w1):
            a1 = w1
        sc = e.get("scenario")
        return any(
            sc in scs
            and (a1 is None or s0 is None or s0 <= a1)
            and (a0 is None or s1 is None or s1 >= a0)
            for s0, s1, scs in _sub_scs
        )

    groups: dict[tuple, dict] = {}
    for e in episodes:
        g = groups.setdefault(_group_key(e), {
            "name": _group_name(e), "source": e.get("source"),
            "scenario": e.get("scenario"),
            "sev_rank": 0, "severity": e.get("severity"),
            "count": 0, "active_sec": 0.0, "blind_sec": 0.0,
            "suppressed": 0, "unconfirmed": 0, "unconfirmed_at": [], "open": 0, "open_live": 0,
            "before": False, "appeared": False, "at_end": False, "_iv": [], "_on": {},
        })
        g["count"] += 1
        # Снятое ИИ и неподтверждённое не суммируются: снятие главнее
        _unconf = not e.get("gate_suppressed") and not _confirmed(e)
        if _unconf:
            g["unconfirmed"] += 1
            # Время — обязательно: в отчёте такого эпизода нет, и без времени модель
            # приписала пометку единственной видимой детекции (35771, 12.10)
            g["unconfirmed_at"].append(_fmt_span_dm(e.get("t_open"), e.get("t_close"), tz))
        _to, _tc = _dt(e.get("t_open")), _dt(e.get("t_close"))
        if w0 is not None and w1 is not None and _to is not None:
            _end = min(_tc or w1, w1)
            g["_iv"].append((max(_to, w0), _end))
            on = g["_on"].setdefault(_to, {"end": _end, "blind": 0.0})
            on["end"] = max(on["end"], _end)
            on["blind"] = max(on["blind"], float(e.get("blind_sec") or 0))
        g["active_sec"] += e.get("active_sec") or 0
        g["blind_sec"] += e.get("blind_sec") or 0
        rank = _SEV_RANK_MD.get(e.get("severity") or "", 0)
        if rank > g["sev_rank"]:
            g["sev_rank"], g["severity"] = rank, e.get("severity")
        if e.get("gate_suppressed"):
            g["suppressed"] += 1
        if not e.get("t_close"):
            g["open"] += 1
            if not e.get("gate_suppressed") and not _unconf:
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
    for g in groups.values():
        iv, ons = _merge_iv(g.pop("_iv")), g.pop("_on")
        if w0 is None or w1 is None or not ons:
            continue
        g["period_sec"] = _iv_len(iv)
        g["period_blind_sec"] = _iv_cross(iv, gap_iv)
        g["onsets"] = len(ons)
        before = sorted(t for t in ons if t < w0)
        if before:
            g["total_sec"] = _iv_len(_merge_iv([(t, ons[t]["end"]) for t in ons]))
            # По всем онсетам, как total_sec, и не меньше слепой доли за период:
            # период входит в «с начала» (ревью v5.1.9)
            g["total_blind_sec"] = max(sum(o["blind"] for o in ons.values()),
                                       g.get("period_blind_sec") or 0.0)
            g["since"] = before[0].isoformat()
            g["since_text"] = _fmt_dm(g["since"], tz)
    # Разовые события перехода (v5.1.10): «событие ЧЧ:ММ:СС», никогда не «на конец»,
    # +1 к вердикту; сортируются с остальными — ниже живых, выше снятых
    ev_groups = _event_groups(segments, event_eps, w0, w1, tz)
    for g in ev_groups:
        g["sev_rank"] = _SEV_RANK_MD.get("CAUTION", 0)
    remarks = sorted(list(groups.values()) + ev_groups,
                     key=lambda g: (-g["sev_rank"], _remark_live(g) <= 0,
                                    -g.get("period_sec", g["active_sec"])))
    for g in remarks:
        g.pop("sev_rank", None)

    # Парная событию проваленная проверка вторым замечанием не считается
    _ev_sc = {g["scenario"] for g in ev_groups}
    failed_checks = [c for c in failed_checks if EVENT_CHECKS.get(c.get("check")) not in _ev_sc]

    # Риск нагара: замечание — только где долг вырос (не в стопе)
    coking_part = _coking_part(segments, tz)
    if coking_part and coking_part["remark"]:
        remarks.append({
            "name": SCENARIO_RU["COKING_RISK"], "source": "analytics", "scenario": "COKING_RISK",
            "severity": "CAUTION", "count": 1, "active_sec": coking_part["added_sec"] or 0.0,
            "blind_sec": 0.0, "suppressed": 0, "unconfirmed": 0, "open": 0, "open_live": 0,
            "period_sec": coking_part["added_sec"] or 0.0, "period_blind_sec": 0.0,
            "before": coking_part["level_start"] not in (None, "GREEN"),
            "appeared": coking_part["level_start"] in (None, "GREEN"),
            "at_end": True, "kind": "state",
        })

    checks = []
    for c in failed_checks:
        checks.append({"check": c.get("check"), "details": _check_details(c, tz)})

    verdict, end_level = _verdict_from_remarks(remarks, checks, bool(emergency))

    # ── Ключевые показатели — за весь период работы ──
    key_metrics = _key_metrics_part(segments, trip_roles)
    coking = coking_part

    return {
        "version": SUMMARY_PARTS_VERSION,
        "verdict": verdict,
        "end_level": end_level,
        "mttr": mttr_part,
        "remarks": remarks,
        "has_remarks": bool(remarks or failed_checks),
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
# Наблюдения, а не замечания: аварию по ним несёт сама панель (v5.1.7) —
# «обороты не упали после перехода в Стоп» (у панели код 1245), «на конце
# сегмента активна аварийная маска»
_INFO_CHECKS = frozenset({"rpm_stop_profile", "no_active_shutdown_fault",
                          # v5.1.10: переход не оценён (нет нагрузки в данных); холодный
                          # пуск без фазы прогрева — первый сезон наблюдением (12.10)
                          "cooldown_not_evaluated", "cold_start_no_warmup"})
# Проверки, не влияющие на вердикт
_NO_VERDICT_CHECKS = _DATA_QUALITY_CHECKS | _INFO_CHECKS
# Только в данных (characteristics_json): маску на конце уже показывают блок отчёта
# «⚠ Активные неисправности» и панельное замечание сводки «активен на конец»
_HIDDEN_CHECKS = frozenset({"no_active_shutdown_fault"})


def _event_groups(segments, event_eps, w0, w1, tz) -> list[dict]:
    """Группы замечаний разовых событий перехода: из детекций сегментов окна;
    точечный эпизод — запасной источник, если детекции нет (сбой контекста)."""
    from .contract import EVENT_SCENARIOS
    on: dict[tuple, dict] = {}

    def _in(t):
        return t is not None and (w0 is None or t >= w0) and (w1 is None or t < w1)

    for s in segments or []:
        for sub in getattr(s, "subsegments", None) or []:
            for d in getattr(sub, "detections", None) or []:
                dd = _as_dict(d)
                sc = dd.get("scenario")
                t = _dt(dd.get("t_detected"))
                if sc in EVENT_SCENARIOS and _in(t):
                    on.setdefault((sc, t), {"text": dd.get("trigger")})
    for e in event_eps or []:
        sc, t = e.get("scenario"), _dt(e.get("t_open"))
        if sc in EVENT_SCENARIOS and _in(t) and (sc, t) not in on:
            on[(sc, t)] = {"text": None}
    groups: dict[str, dict] = {}
    for (sc, t), v in sorted(on.items(), key=lambda x: x[0][1]):
        g = groups.setdefault(sc, {
            "name": SCENARIO_RU.get(sc, sc), "source": "analytics", "scenario": sc,
            "severity": "CAUTION", "kind": "event", "count": 0, "events": [],
            "active_sec": 0.0, "blind_sec": 0.0, "period_sec": 0.0, "period_blind_sec": 0.0,
            "suppressed": 0, "unconfirmed": 0, "unconfirmed_at": [], "open": 0, "open_live": 0,
            "before": False, "appeared": True, "at_end": False,
        })
        g["count"] += 1
        tt = t.astimezone(tz) if tz is not None else t
        g["events"].append({"t": t.isoformat(), "t_text": tt.strftime("%d.%m %H:%M:%S"),
                            "text": v.get("text")})
    return list(groups.values())


def _remark_live(g: dict) -> int:
    """Эпизоды группы, идущие в вердикт: без снятых ИИ и неподтверждённых итоговым расчётом."""
    return g["count"] - g.get("suppressed", 0) - g.get("unconfirmed", 0)


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
    live = _remark_live

    panel = [g for g in remarks if g.get("source") == "panel" and live(g) > 0]
    shutdown = any(g.get("severity") == "SHUTDOWN" for g in panel)
    warning = any(g.get("severity") == "WARNING" for g in panel)
    n = (sum(live(g) for g in remarks if g.get("source") == "analytics")
         + sum(live(g) for g in panel if g.get("severity") not in ("SHUTDOWN", "WARNING"))
         + sum(1 for c in checks if c.get("check") not in _NO_VERDICT_CHECKS))
    suppressed_n = sum(g.get("suppressed", 0) for g in remarks)
    unconfirmed_n = sum(g.get("unconfirmed", 0) for g in remarks)

    if shutdown or emergency:
        verdict = {"level": "SHUTDOWN", "title": "🔴 АВАРИЯ — аварийный останов панели", "note": None}
    elif warning:
        verdict = {"level": "WARNING", "title": "🟠 ВНИМАНИЕ — предупреждение панели управления",
                   "note": None}
    elif n:
        verdict = {"level": "CAUTION", "title": f"🟡 ЗАМЕЧАНИЯ К РАБОТЕ — {n}", "note": None}
    elif suppressed_n or unconfirmed_n:
        _notes = []
        if suppressed_n:
            _notes.append(f"Предупреждения аналитики ({suppressed_n}) сняты ИИ.")
        if unconfirmed_n:
            _notes.append(f"Эпизоды по живым данным ({unconfirmed_n}) итоговым расчётом "
                          f"не подтверждены.")
        verdict = {"level": "НОРМА", "title": "🟢 НОРМА", "note": " ".join(_notes)}
    else:
        verdict = {"level": "НОРМА", "title": "🟢 НОРМА", "note": "Замечаний к работе нет."}
    verdict["remarks_n"] = n
    verdict["suppressed_n"] = suppressed_n
    verdict["unconfirmed_n"] = unconfirmed_n

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
        if g.get("kind") == "event":
            continue   # разовое событие перехода гейт не снимает (v5.1.10)
        if g.get("source") == "analytics" and g.get("scenario") in sc:
            # Вердикт гейта касается открытых эпизодов: закрывшиеся раньше в
            # этом же сегменте остаются замечаниями, как и при живой пометке
            n = g.get("open_live")
            if n is None:
                n = _remark_live(g)
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
            if g.get("kind") == "state":
                ck = parts.get("coking") or {}
                a(f"- {emoji} **{g['name']}** [{src_ru}]: {coking_state_text(ck) or ''}")
                continue
            if g.get("kind") == "event":
                evs = g.get("events") or []
                when = ", ".join(e.get("t_text") or "?" for e in evs)
                txt = next((e.get("text") for e in reversed(evs) if e.get("text")), None)
                a(f"- {emoji} **{g['name']}** [{src_ru}]: "
                  f"{'событие' if len(evs) == 1 else 'события'} {when}"
                  + (f" — {txt}" if txt else ""))
                continue
            if "period_sec" in g:
                n = g.get("onsets") or g["count"]
                line = (f"- {emoji} **{g['name']}** [{src_ru}]: {n} {_eps_ru(n)}, "
                        f"воздействие за период {_fmt_duration(g['period_sec'])}")
                if g.get("since"):
                    _tb = g.get("total_blind_sec") or 0
                    line += (f" (с начала {_fmt_duration(g['total_sec'])} — "
                             f"с {g.get('since_text') or g['since']}"
                             + (f", из них {_fmt_duration(_tb)} без связи" if _tb >= 1 else "")
                             + ")")
                # Слепая доля — обязательная оговорка: внутри неё мог быть сброс
                if g.get("period_blind_sec", 0) >= 1:
                    line += (f"; из них {_fmt_duration(g['period_blind_sec'])} без связи — "
                             f"снятие могло произойти раньше")
            else:   # части v1 (до v5.1.6)
                line = (f"- {emoji} **{g['name']}** [{src_ru}]: {g['count']} {_eps_ru(g['count'])}, "
                        f"воздействие {_fmt_duration(g['active_sec'])}")
                if g["blind_sec"] >= 1:
                    line += (f" (из них {_fmt_duration(g['blind_sec'])} без связи — "
                             f"снятие могло произойти раньше)")
            if g["suppressed"]:
                line += (" — *снято ИИ*" if g["suppressed"] == g["count"]
                         else f" — *снято ИИ: {g['suppressed']} из {g['count']}*")
            _u = g.get("unconfirmed") or 0
            if _u:
                _at = ", ".join(g.get("unconfirmed_at") or [])
                _at = f" ({_at})" if _at else ""
                line += (f" — *по живым данным{_at}, в итоговом расчёте не подтверждено*"
                         if _u == g["count"] - (g["suppressed"] or 0)
                         else f" — *в итоговом расчёте не подтверждено: {_u} из {g['count']}{_at}*")
            # То же, что end_level и шапка ИИ: снятое ИИ и снятое панелью к концу не висит
            if g.get("at_end", g["open"] and not g["suppressed"]):
                line += " — **активен на конец периода**"
            a(line)
        for c in parts.get("failed_checks") or []:
            det = c.get("details") if isinstance(c, dict) else c
            mark = "ℹ️" if isinstance(c, dict) and c.get("check") in _INFO_CHECKS else "❗"
            a(f"- {mark} {det}" if det else f"- {mark} проверка не пройдена")

    km = parts.get("key_metrics")
    if km and km.get("rows"):
        a("")
        a("### Ключевые показатели")
        if km.get("basis") == "period":
            a(f"*За период работы {_fmt_duration(km.get('work_sec') or 0)}: медиана — по времени, "
              f"мин/макс — за весь период.*")
            a("")
        a("| Параметр | Медиана | Мин | Макс | Ед. |")
        a("|----------|--------:|----:|----:|-----|")
        for r in km["rows"]:
            a(f"| {r['role']} | {_fmt_val(r.get('median'))} | {_fmt_val(r.get('min'))} "
              f"| {_fmt_val(r.get('max'))} | {r.get('unit', '')} |")
    ck = parts.get("coking")
    if ck and isinstance(ck, dict) and "before_start" in ck:
        if ck["before_start"]:
            a("")
            a("### Перед пуском")
            acc_txt = _coking_accum_text(ck)
            left = _coking_left(ck)
            a(f"⚠ **Накоплен риск нагара в выпуске — {_COKING_LEVEL_RU.get(ck['level'], ck['level'])}.** "
              f"{acc_txt[0].upper()}{acc_txt[1:]}. В этом сегменте установка стояла — риск "
              f"не изменился." + (f" До сброса — {left}." if left else ""))
            a("")
            a("Остановка риск не снимает — снимает только работа под нагрузкой (прожиг). "
              "Опасный момент — пуск и первые минуты под нагрузкой: осмотреть стыки "
              "коллектора и срез трубы, слить конденсат, нагрузку набирать постепенно "
              "(до 70 % не быстрее ~15 мин), прожиг — под присмотром.")
            if ck.get("service_needed"):
                a("")
                a(f"⚠ {_SERVICE_ADVICE}")
        elif not ck["stop"]:
            lvl = _COKING_LEVEL_RU.get(ck["level"], ck["level"])
            st = ck.get("level_start")
            show = (ck["level"] != "GREEN" or st not in (None, "GREEN")
                    or ck.get("purged_at_text") or ck.get("paid_sec"))
            line = (f"Риск нагара: на начало **{_COKING_LEVEL_RU.get(st, st)}** → на конец **{lvl}**"
                    if st else f"Риск нагара на конец: **{lvl}**")
            if ck.get("purged_at_text"):
                line += (f" — прожиг завершён {ck['purged_at_text']}, недогруз погашен"
                         if (ck.get("debt_sec") or 0) < 60 else
                         f" — прожиг завершён {ck['purged_at_text']}, затем недогруз набрался снова")
            elif ck.get("paid_sec"):
                line += f" — прожигом погашено ~{_fmt_hm(ck['paid_sec'])} долга"
            if ck["level"] != "GREEN" and not ck.get("remark"):
                left = _coking_left(ck)
                if left:
                    line += f"; до сброса — {left}"
            # Замечание о приросте уже всё сказало — строку не дублируем
            if show and not (ck.get("remark") and not ck.get("purged_at_text")
                             and not ck.get("paid_sec")):
                a("")
                a(line)
            if ck.get("service_needed"):
                a("")
                a(f"⚠ {_SERVICE_ADVICE}")
        # Календарь прогонов: у стоящей машины — всегда (резервные), у работающей —
        # только просрочка
        cal_txt = coking_calendar_text(ck, only_overdue=not ck["stop"])
        if cal_txt:
            a("")
            a(cal_txt)
    elif ck and ck.get("level") not in (None, "GREEN"):   # части до v5.1.3
        a("")
        a(f"Риск нагара на конец: **{_COKING_LEVEL_RU.get(ck['level'], ck['level'])}**")

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


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return few
    return many


def _fmt_dq(q: float | None) -> str:
    """Качество данных с одним знаком; неполное покрытие не округляется до 100 %."""
    if q is None:
        return "—"
    if q >= 1.0:
        return "100%"
    return f"{math.floor(q * 1000) / 10:.1f}%"


def _rs_time_label(rs: int) -> str:
    if rs == 3:
        return "Время под нагрузкой (RUN_STATE=3)"
    if rs == 0:
        return "Время в останове (RUN_STATE=0)"
    return f"Время в режиме «{RUN_STATE_RU.get(rs, rs)}» (RUN_STATE={rs})"


def _det_name(d: dict) -> str:
    """Русское имя обнаружения вместо ключа сценария (OIL_DILUTION читалось как
    «разжижение масла», хотя это давление ниже паспорта; ключ в KB не меняется)."""
    sc = d.get("scenario") or "?"
    v = d.get("values") or {}
    if sc == "CONTROLLER_FAULT":
        return (v.get("fault_name") or (d.get("trigger") or "").split(" (addr=")[0].strip()
                or SCENARIO_RU.get(sc, sc))
    if sc == "LIMIT_PROXIMITY" and ":" in (d.get("trigger") or ""):
        return f"{SCENARIO_RU.get(sc, sc)} ({d['trigger'].split(':', 1)[0].strip()})"
    return SCENARIO_RU.get(sc, sc)


def _det_kind_key(d: dict) -> tuple:
    """Вид обнаружения: сценарий, у панели — бит, у порогов — параметр."""
    sc = d.get("scenario")
    v = d.get("values") or {}
    if sc == "CONTROLLER_FAULT":
        return (sc, v.get("addr"), v.get("bit"))
    if sc == "LIMIT_PROXIMITY":
        return (sc, d.get("description_key"))
    return (sc,)


def _det_group_key(d: dict) -> tuple:
    """Пункт свёртки: вид, у панели ещё и фронт (бит, поднятый заново, — другой пункт)."""
    if d.get("scenario") == "CONTROLLER_FAULT":
        v = d.get("values") or {}
        return _det_kind_key(d) + (v.get("fault_start") or d.get("t_detected"),)
    return _det_kind_key(d)


def _fmt_sub_ranges(idxs: list[int], seg_idx: int) -> str:
    """[1,3,4,5] → «1.1, 1.3–1.5»."""
    out, run = [], []
    for i in sorted(set(idxs)):
        if run and i == run[-1] + 1:
            run.append(i)
            continue
        if run:
            out.append(run)
        run = [i]
    if run:
        out.append(run)
    return ", ".join(f"{seg_idx}.{r[0]}" if len(r) == 1 else f"{seg_idx}.{r[0]}–{seg_idx}.{r[-1]}"
                     for r in out)


# Худшее значение группы: (ключ values, направление) — что «хуже» для сценария
_WORST_KEY = {
    "OIL_DILUTION": ("oil_press_min", min),
    "RPM_UNDERSPEED": ("rpm_min", min),
    "LIMIT_PROXIMITY": ("value", max),
    "COOLING_FAILURE": ("coolant_temp_max", max),
    "PHASE_CURRENT_SPREAD": ("pct_max", max),
    "NEUTRAL_CURRENT": ("pct_max", max),
    "NEGATIVE_SEQUENCE": ("i2_pct_max", max),
}


def _det_worst(entries: list[tuple[int, dict]]) -> tuple[int, dict] | None:
    """Вхождение с худшим значением (минимум давления/оборотов, максимум температуры…)."""
    if not entries:
        return None
    rule = _WORST_KEY.get(entries[0][1].get("scenario"))
    if not rule:
        return None
    key, pick = rule
    vals = [(i, d) for i, d in entries
            if isinstance((d.get("values") or {}).get(key), (int, float))]
    if not vals:
        return None
    return pick(vals, key=lambda x: x[1]["values"][key])


def _group_detections(subsegments: list) -> list[dict]:
    """Одна детекция во многих подсегментах — один пункт (v5.1.6: повторы по
    подсегментам давали «45 обнаружений» вместо 4 видов и до 18 % отчёта)."""
    groups: dict[tuple, dict] = {}
    for i, sub in enumerate(subsegments, 1):
        for _d in sub.detections:
            d = _as_dict(_d)
            if d.get("severity") != cap_severity(d.get("scenario"), d.get("severity")):
                d = dict(d, severity=cap_severity(d.get("scenario"), d.get("severity")))
            g = groups.setdefault(_det_group_key(d), {
                "name": _det_name(d), "severity": d.get("severity"), "subs": [],
                "triggers": {}, "sources": [], "codes": [], "last": d, "first_sub": i,
                "entries": [],
            })
            g["entries"].append((i, d))
            if _SEV_RANK_MD.get(d.get("severity") or "", 0) > _SEV_RANK_MD.get(g["severity"] or "", 0):
                g["severity"] = d.get("severity")
            g["subs"].append(i)
            g["triggers"].setdefault(d.get("trigger") or "", []).append(i)
            if d.get("source") and d["source"] not in g["sources"]:
                g["sources"].append(d["source"])
            for c in d.get("fault_codes") or []:
                if c not in g["codes"]:
                    g["codes"].append(c)
            g["last"] = d
    return sorted(groups.values(),
                  key=lambda g: (-_SEV_RANK_MD.get(g["severity"] or "", 0), g["first_sub"]))


def _det_counter_lines(d: dict, fmt_ts) -> list[str]:
    v = d.get("values") or {}
    out = []
    count_30d = v.get("history_count_30d")
    if count_30d is not None:
        dur_30d = v.get("history_duration_30d_sec")
        blind_30d = v.get("history_blind_30d_sec")
        suffix = f" (суммарно {_fmt_duration(dur_30d)})" if dur_30d else ""
        if dur_30d and blind_30d:
            suffix = (f" (суммарно {_fmt_duration(dur_30d)}, из них "
                      f"{_fmt_duration(blind_30d)} без связи)")
        out.append(f"  - Срабатываний этого типа за 30 дней: **{count_30d}**{suffix}")
    startup_count = v.get("startup_count")
    if startup_count is not None:
        dur_startup = v.get("startup_duration_sec")
        suffix = f" (суммарно {_fmt_duration(dur_startup)})" if dur_startup else ""
        # Тревога поднята до пуска и висит: фронтов «с пуска» нет, но это не «чисто»
        onset, since = v.get("fault_start") or d.get("t_detected"), v.get("startup_since")
        if not startup_count and onset and since and str(onset) < str(since):
            suffix = f" (активна с {fmt_ts(onset)} — до пуска)"
        out.append(f"  - Срабатываний с пуска: **{startup_count}**{suffix}")
    return out


def _append_detections(lines: list[str], seg: Segment, idx: int, fmt_ts) -> None:
    """Обнаружения сегмента одним блоком, повторы по подсегментам свёрнуты."""
    groups = _group_detections(seg.subsegments)
    if not groups:
        return
    a = lines.append
    m = len(seg.subsegments)
    hits = sum(len(g["subs"]) for g in groups)
    a(f"**Обнаружения:** {len(groups)} {_plural(len(groups), 'пункт', 'пункта', 'пунктов')}"
      + (f", {hits} {_plural(hits, 'срабатывание', 'срабатывания', 'срабатываний')} по подсегментам"
         if m > 1 else ""))
    a("")
    for g in groups:
        n = len(set(g["subs"]))
        where = ("" if m == 1 else f" — во всех {m} подсегментах" if n == m
                 else f" — в подсегменте {idx}.{g['subs'][0]}" if n == 1
                 else f" — в {n} из {m} подсегментов: {_fmt_sub_ranges(g['subs'], idx)}")
        a(f"- {_SEVERITY_EMOJI.get(g['severity'], '')} **{g['name']}** ({g['severity']}){where}")
        trig = [(t, s) for t, s in g["triggers"].items() if t]
        if len(trig) <= 3:
            for t, s in trig:
                a(f"  - {t}" + (f" — {_fmt_sub_ranges(s, idx)}" if m > 1 and len(trig) > 1 else ""))
        else:
            # Много разных значений: первое, худшее (самая тяжёлая точка — её нельзя
            # терять: модель «не ищет сверх обнаружений») и последнее по времени
            (t0, s0) = trig[0]
            li, ld = max(g["entries"], key=lambda x: x[0])
            a(f"  - первое ({_fmt_sub_ranges(s0[:1], idx)}): {t0}")
            w = _det_worst(g["entries"])
            if w and w[1].get("trigger") not in (t0, ld.get("trigger")):
                a(f"  - худшее ({_fmt_sub_ranges([w[0]], idx)}): {w[1].get('trigger')}")
            a(f"  - последнее ({_fmt_sub_ranges([li], idx)}): {ld.get('trigger')}")
            a(f"  - всего разных значений: {len(trig)}")
        lv = g["last"].get("values") or {}
        if g["last"].get("scenario") == "CONTROLLER_FAULT" and lv.get("fault_start"):
            a(f"  - Активна с {fmt_ts(lv['fault_start'])}")
        a(f"  - Источник: `{', '.join(g['sources'])}`")
        if g["codes"]:
            a(f"  - Код панели (выставлен): {g['codes']}")
        lines.extend(_det_counter_lines(g["last"], fmt_ts))
    a("")


def _fmt_span(start: str, end: str, tz=None) -> str:
    s, e = _fmt_dm(start, tz), _fmt_dm(end, tz)
    try:
        ds = datetime.fromisoformat(start)
        de = datetime.fromisoformat(end)
        if tz is not None:
            ds, de = ds.astimezone(tz), de.astimezone(tz)
        sec = lambda x: x.strftime(":%S")   # noqa: E731
        if ds.date() == de.date():
            return f"{s}{sec(ds)}–{de.strftime('%H:%M:%S')}"
        return f"{s}{sec(ds)} – {e}{sec(de)}"
    except (TypeError, ValueError):
        return f"{start}–{end}"


def _check_details(c: dict, tz=None) -> str:
    """Подробности проверки; окно связи — в местном времени (было в UTC при МСК
    в остальной сводке)."""
    rng = c.get("ranges")
    if c.get("check") == "subseg_data_coverage" and rng:
        return "отсутствие связи/данных: " + ", ".join(_fmt_span(r["start"], r["end"], tz) for r in rng)
    return c.get("details") or c.get("detail") or ""


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
    period_rs: dict[int, float] = {}
    total_elevated_sec = 0.0
    seg_dqs: list[float] = []

    for seg in segments:
        seg_dqs.append(seg.data_quality)
        period_rs[seg.run_state] = period_rs.get(seg.run_state, 0.0) + seg.duration_sec
        for sub in seg.subsegments:
            all_detections.extend(_as_dict(d) for d in sub.detections)
            tr = sub.risk_accumulators.thermal_risk
            total_elevated_sec += tr.elevated_zone_sec

    avg_dq = sum(seg_dqs) / len(seg_dqs) if seg_dqs else 1.0

    a("## Сводка")
    a("")
    a(f"| Параметр | Значение |")
    a(f"|----------|----------|")
    a(f"| Сегментов | {len(segments)} |")
    # Время в режиме: за период — по сегментам окна; с начала состояния — только
    # у головы и только её режима (раньше строки показывали сквозное время цепочки
    # без подписи: «280 ч в останове» в суточном отчёте)
    for rs in sorted(period_rs, key=lambda r: (r != 3, r != 0, r)):
        a(f"| {_rs_time_label(rs)} за период | {_fmt_duration(period_rs[rs])} |")
    head = segments[0] if segments else None
    inh = float((inherited_run_state_sec or {}).get(head.run_state) or 0.0) if head else 0.0
    if inh > 0:
        try:
            t0 = (datetime.fromisoformat(head.t_start) - timedelta(seconds=inh)).isoformat()
            since = f" (с {_fmt_dm(t0, tz)})"
        except (TypeError, ValueError):
            since = ""
        a(f"| {_rs_time_label(head.run_state)} с начала состояния{since} | "
          f"{_fmt_duration(inh + head.duration_sec)} |")
    if 3 in period_rs:
        a(f"| Время в зоне повышенной нагрузки | {_fmt_duration(total_elevated_sec)} |")
    # Подсчёт по видам; уровня и вердикта здесь нет: отчёт замораживается при
    # закрытии, а вердикт (единый, со снятием ИИ) — в сводке сегмента
    hits = len(all_detections)
    kinds = len({_det_kind_key(d) for d in all_detections})
    a("| Обнаружения детекторов | " + (
        f"{kinds} {_plural(kinds, 'вид', 'вида', 'видов')}, {hits} "
        f"{_plural(hits, 'срабатывание', 'срабатывания', 'срабатываний')} по подсегментам"
        if hits else "нет") + " |")
    a(f"| Качество данных (среднее) | {_fmt_dq(avg_dq)} |")
    a("")
    if hits:
        a("*Уровни у обнаружений — уровни детекторов до разбора ИИ; вердикт периода — "
          "в сводке сегмента.*")
        a("")

    # ── Панель: аварии и предупреждения (свёрнуто по фронтам) ──
    panel_groups = [g for seg in segments for g in _group_detections(seg.subsegments)
                    if g["last"].get("scenario") == "CONTROLLER_FAULT"
                    and g["severity"] in ("SHUTDOWN", "WARNING")]
    if panel_groups:
        a("## Панель: аварии и предупреждения")
        a("")
        for g in panel_groups:
            lv = g["last"].get("values") or {}
            ts_str = fmt_ts(lv.get("fault_start") or g["last"].get("t_detected"))
            a(f"- {_SEVERITY_EMOJI.get(g['severity'], '')} **{g['name']}** @ {ts_str}: "
              f"{g['last'].get('trigger')}")
        a("")

    # ── Детали по сегментам ──
    a("## Сегменты")
    a("")

    for seg_idx, seg in enumerate(segments, 1):
        ps = segments[seg_idx - 2] if seg_idx >= 2 else prev_seg
        _append_segment(lines, seg, seg_idx, fmt_ts, prev_seg=ps, fault_ref=fault_ref, tz=tz)

    return "\n".join(lines)


def _append_segment(
    lines: list[str],
    seg: Segment,
    idx: int,
    fmt_ts,
    prev_seg: "Segment | None" = None,
    fault_ref=None,
    tz=None,
) -> None:
    a = lines.append
    state_label = (
        seg.run_state_label
        or RUN_STATE_RU.get(seg.run_state, f"RUN_STATE={seg.run_state}")
    )
    dq_str = _fmt_dq(seg.data_quality)
    dur_str = _fmt_duration(seg.duration_sec)
    # Наработка — в часах (v5.1.6: сегментатор переводит 40070 из секунд)
    hours_str = (
        f" | Наработка: {seg.engine_hours_start:.0f} ч"
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
        _seg_end_dt = _dt(seg.t_end)
        for ev in seg.events:
            sev = ev.get("severity") or "?"
            name = ev.get("name_ru") or ev.get("name") or "Unknown"
            t = fmt_ts(ev.get("t"))
            dur = ev.get("duration_sec")
            dur_s = f" ({_fmt_duration(dur)})" if dur else ""
            emo = _SEVERITY_EMOJI.get(sev) or _RAW_SEV_EMOJI.get(str(sev).lower(), "")
            _fe = _dt(ev.get("fault_end"))
            # Строго: снятая ровно на конце (рез SHUTDOWN_CLEARED, смена режима в
            # одном кадре) — уже не активна, как в segmenter._active_at_end
            live = ev.get("type") == "FAULT" and (
                _fe is None or (_seg_end_dt is not None and _fe > _seg_end_dt))
            a(f"- {emo + ' ' if emo else ''}`{name}` @ {t}{dur_s}"
              + (" — **активно на конец периода**" if live else ""))
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
            # Снятая к концу сегмента или ровно на нём — не «не сброшена»
            # (сравнение строк ISO давало «не сброшена» снятой на резе маске)
            _fe_a, _te_a = _dt(ev.get("fault_end")), _dt(seg.t_end)
            if _fe_a is not None and _te_a is not None and _fe_a <= _te_a:
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

    # Sequence checks: замечания и наблюдения (на вердикт не влияют) — раздельно
    failed_checks = [c for c in seg.sequence_checks
                     if not c.get("passed") and c.get("check") not in _HIDDEN_CHECKS]
    _obs = [c for c in failed_checks if c.get("check") in _INFO_CHECKS]
    _warn = [c for c in failed_checks if c.get("check") not in _INFO_CHECKS]
    if _warn:
        a("**Предупреждения по последовательности:**")
        a("")
        for c in _warn:
            a(f"- ⚠️ `{c['check']}`: {_check_details(c, tz)}")
        a("")
    if _obs:
        a("**Наблюдения по последовательности** (на вердикт не влияют):")
        a("")
        for c in _obs:
            a(f"- ℹ️ `{c['check']}`: {_check_details(c, tz)}")
        a("")

    # Обнаружения — одним блоком на сегмент, повторы по подсегментам свёрнуты
    _append_detections(lines, seg, idx, fmt_ts)

    # Подсегменты
    if len(seg.subsegments) > 1:
        a(f"**Подсегментов:** {len(seg.subsegments)}")
        a("")

    # Состояния (код и тип неисправности) — только при смене значения
    prev_state: dict[str, Any] = {}
    for sub_idx, sub in enumerate(seg.subsegments, 1):
        skip = {r for r, ch in sub.characteristics.items()
                if isinstance(ch, dict) and ch.get("kind") == "state" and sub_idx > 1
                and ch.get("min") == ch.get("max") == prev_state.get(r)}
        _append_subsegment(lines, sub, idx, sub_idx, fmt_ts, short=(len(seg.subsegments) == 1),
                           skip_roles=skip)
        prev_state.update({r: ch.get("value_end") for r, ch in sub.characteristics.items()
                           if isinstance(ch, dict) and ch.get("kind") == "state"})


def _append_subsegment(
    lines: list[str],
    sub: Subsegment,
    seg_idx: int,
    sub_idx: int,
    fmt_ts,
    short: bool = False,
    skip_roles: set[str] | None = None,
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
        a(f"| Качество данных | {_fmt_dq(sub.data_quality)} |")
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
            if role in (skip_roles or ()):
                continue
            dec = 0 if ch.get("kind") == "state" else 2   # код — целым: «1453», не «1453.00»
            med = _fmt_val(ch.get("median"), dec)
            mn = _fmt_val(ch.get("min"), dec)
            mx = _fmt_val(ch.get("max"), dec)
            slope = _fmt_val(ch.get("slope"))
            unit = ch.get("unit") or ""
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
                f"накоплено с прожига: малая нагрузка {_fmt_duration(cr['low_load_zone_sec'])}, "
                f"холостой ход {_fmt_duration(cr['idle_low_rpm_sec'])}, "
                f"ОЖ<60°C {_fmt_duration(cr['coolant_below_60_sec'])}"
            )
            a(f"| Риск нагара | {_RISK_EMOJI.get(cr_lvl, '')} {cr_lvl} | {details} |")
        if tr_lvl != "GREEN":
            details = f"ELEVATED зона: {_fmt_duration(tr['elevated_zone_sec'])}"
            a(f"| Тепловой | {_RISK_EMOJI.get(tr_lvl, '')} {tr_lvl} | {details} |")
        a("")

    # Обнаружения — блоком на уровне сегмента (_append_detections), здесь не повторяются


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
