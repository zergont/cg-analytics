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
    "ALARM": "🟠",
    "WARNING": "🟡",
    "INFO": "🔵",
}

_ZONE_RU = {
    "LOW": "Малая нагрузка",
    "NORMAL": "Нормальная нагрузка",
    "ELEVATED": "Повышенная нагрузка",
    "OVERLOAD": "Перегрузка",
    "NA": "Н/Д",
}

_RUN_STATE_RU: dict[int, str] = {
    0: "Стоп",
    1: "Задержка пуска",
    2: "Прогрев",
    3: "Работа",
    4: "Разгрузка",
    5: "Охлаждение на х.х.",
    6: "Переход на х.х.",
}

_RISK_EMOJI = {"GREEN": "🟢", "YELLOW": "🟡", "RED": "🔴"}


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
    order = ["SHUTDOWN", "ALARM", "WARNING", "INFO"]
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

def to_markdown(
    segments: list[Segment],
    router_sn: str,
    equip_type: str,
    panel_id: int,
    ts_from: datetime,
    ts_to: datetime,
    analytics_version: str = "2.0.0",
    tz=None,
) -> str:
    """Сформировать Markdown-отчёт.

    tz — объект часового пояса (например, из config.get_tz()); None → UTC.
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
    total_running_sec = 0.0
    total_elevated_sec = 0.0
    seg_dqs: list[float] = []

    for seg in segments:
        seg_dqs.append(seg.data_quality)
        if seg.run_state == 3:
            total_running_sec += seg.duration_sec
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
    a(f"| Время в зоне повышенной нагрузки | {_fmt_duration(total_elevated_sec)} |")
    a(f"| Всего обнаружений | {len(all_detections)} |")
    for sev in ["SHUTDOWN", "ALARM", "WARNING", "INFO"]:
        cnt = by_severity.get(sev, 0)
        if cnt:
            a(f"| — {_SEVERITY_EMOJI.get(sev, '')} {sev} | {cnt} |")
    a(f"| Качество данных (среднее) | {avg_dq:.1%} |")
    if max_sev:
        a(f"| Максимальный уровень тревоги | {_SEVERITY_EMOJI.get(max_sev, '')} {max_sev} |")
    a("")

    # ── Быстрый список тревог ──
    alarm_detections = [d for d in all_detections if d["severity"] in ("SHUTDOWN", "ALARM")]
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
        prev_seg = segments[seg_idx - 2] if seg_idx >= 2 else None
        _append_segment(lines, seg, seg_idx, fmt_ts, prev_seg=prev_seg)

    return "\n".join(lines)


def _append_segment(
    lines: list[str],
    seg: Segment,
    idx: int,
    fmt_ts,
    prev_seg: "Segment | None" = None,
) -> None:
    a = lines.append
    state_label = (
        seg.run_state_label
        or _RUN_STATE_RU.get(seg.run_state, f"RUN_STATE={seg.run_state}")
    )
    dq_str = f"{seg.data_quality:.0%}"
    dur_str = _fmt_duration(seg.duration_sec)
    hours_str = (
        f" | Мото-часы: {seg.engine_hours_start:.1f}" if seg.engine_hours_start is not None else ""
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
                or _RUN_STATE_RU.get(prev_seg.run_state, f"RUN_STATE={prev_seg.run_state}")
            )
            a(f"- **Предыдущее состояние:** ← {prev_label} (RUN_STATE={prev_seg.run_state})")
        else:
            a(f"- **Предыдущее состояние:** ← неизвестно (вне окна анализа)")
    elif seg.cause_open == "REPORT_START":
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
