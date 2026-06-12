# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Детерминированный сборщик структурного статуса машины.

Публичный API:
  build_structural_status(seg_row, fault_ref, tz) → dict
  compute_status_hash(struct_status) → str
  compute_fault_hash(struct_status) → str
  compute_severity_level(active_dets) → str
  format_status_text(struct_status) → str
  build_warning_prompt(struct_status) → str
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# ── Константы ─────────────────────────────────────────────────────────────────

_RUN_STATE_LABELS: dict[int, str] = {
    0: "Стоп",
    1: "Задержка пуска",
    2: "Прогрев",
    3: "Работа",
    4: "Разгрузка",
    5: "Охлаждение на х.х.",
    6: "Переход на х.х.",
}

# Ключевые роли параметров для каждого режима (2-3 значимых)
_KEY_ROLES: dict[int, list[str]] = {
    0: [],
    1: ["RPM", "BATTERY_V"],
    2: ["COOLANT_TEMP", "RPM"],
    3: ["LOAD_PCT", "COOLANT_TEMP", "OIL_PRESS"],
    4: ["ACTIVE_POWER_TOTAL", "COOLANT_TEMP"],
    5: ["COOLANT_TEMP", "RPM"],
    6: ["COOLANT_TEMP", "RPM"],
}

# Ранг severity от панели управления (CONTROLLER_FAULT)
_PANEL_SEV_RANK: dict[str, int] = {"SHUTDOWN": 3, "ALARM": 3, "WARNING": 2}

# Общий ранг для сортировки итогового уровня
# предупреждение (аналитика) < внимание (панель WARNING) < авария (панель ALARM/SHUTDOWN)
_OVERALL_RANK: dict[str, int] = {
    "авария":         4,
    "внимание":       3,
    "предупреждение": 2,
    "норма":          1,
}


# ── Вспомогательные ───────────────────────────────────────────────────────────

def _parse_json(val: Any) -> Any:
    if val is None:
        return None
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            return None
    return val


def _fmt_duration(sec: float) -> str:
    s = int(sec)
    h, rem = divmod(s, 3600)
    m = rem // 60
    if h:
        return f"{h}ч {m}м"
    if m:
        return f"{m}м"
    return f"{s}с"


# ── Публичный API ─────────────────────────────────────────────────────────────

def compute_panel_severity(active_dets: list[dict]) -> str:
    """Уровень по сигналам панели управления (scenario=CONTROLLER_FAULT).

    норма / внимание (WARNING) / авария (ALARM/SHUTDOWN)
    """
    panel = [d for d in active_dets if d.get("scenario") == "CONTROLLER_FAULT"]
    if not panel:
        return "норма"
    rank = max((_PANEL_SEV_RANK.get(d.get("severity", ""), 0) for d in panel), default=0)
    if rank >= 3:
        return "авария"
    if rank >= 2:
        return "внимание"
    return "норма"


def compute_analytics_severity(active_dets: list[dict]) -> str:
    """Уровень по детекциям нашего аналитического движка (все кроме CONTROLLER_FAULT).

    норма / предупреждение
    """
    analytics = [d for d in active_dets if d.get("scenario") != "CONTROLLER_FAULT"]
    return "предупреждение" if analytics else "норма"


def compute_severity_level(active_dets: list[dict]) -> str:
    """Итоговый уровень — максимум из панели и аналитики.

    норма < предупреждение < внимание < авария
    """
    panel     = compute_panel_severity(active_dets)
    analytics = compute_analytics_severity(active_dets)
    return max(panel, analytics, key=lambda x: _OVERALL_RANK.get(x, 0))


def compute_analytics_hash(dets: list[dict]) -> str:
    """Хэш состава аналитических детекций (без панельных) — ключ подавления гейта.

    Подавление действует, пока состав не изменился: новая аналитическая
    детекция меняет хэш, и вердикт Claude теряет силу.
    """
    analytics = [d for d in dets if isinstance(d, dict) and d.get("scenario") != "CONTROLLER_FAULT"]
    key = tuple(sorted(
        (d.get("scenario", ""), tuple(sorted(d.get("fault_codes") or [])))
        for d in analytics
    ))
    return hashlib.md5(str(key).encode()).hexdigest()[:12]


def is_analytics_suppressed(seg_row: dict, active_dets: list[dict]) -> bool:
    """Действует ли вердикт гейта «отменить» для текущего состава аналитических детекций."""
    suppressed = seg_row.get("gate_suppressed_hash")
    if not suppressed:
        return False
    has_analytics = any(
        isinstance(d, dict) and d.get("scenario") != "CONTROLLER_FAULT" for d in active_dets
    )
    return has_analytics and compute_analytics_hash(active_dets) == suppressed


def build_structural_status(
    seg_row: dict,
    fault_ref=None,
    tz=None,
    origin_ts=None,
) -> dict[str, Any]:
    """Собрать структурный статус из строки открытого сегмента.

    Args:
        seg_row:   строка из auto_segments (dict)
        fault_ref: FaultRef для расшифровки кодов тревог (может быть None)
        tz:        часовой пояс (зарезервирован)
        origin_ts: реальное начало режима (datetime) — перекрывает t_start сегмента;
                   нужно когда текущий сегмент создан суточным срезом, а режим
                   начался раньше (цепочка continued_from)

    Returns:
        dict с полями:
          run_state, mode_label, load_pct, load_pct_bucket,
          severity_level, panel_severity, analytics_severity,
          time_in_mode_sec, panel_alarms[], analytics_alarms[], key_params[]
    """
    run_state: int = seg_row.get("run_state") or 0
    mode_label = _RUN_STATE_LABELS.get(run_state, f"RUN_STATE={run_state}")

    current_vals = _parse_json(seg_row.get("current_values_json")) or {}
    active_dets  = _parse_json(seg_row.get("active_detections_json")) or []
    values: dict = current_vals.get("values") or {}

    # ── Нагрузка ──
    load_pct: float | None = None
    if "LOAD_PCT" in values:
        raw = values["LOAD_PCT"].get("value")
        if raw is not None:
            try:
                load_pct = float(raw)
            except (TypeError, ValueError):
                pass
    load_pct_bucket = (int(load_pct // 10) * 10) if load_pct is not None else None

    # ── Время в режиме ──
    # Берём из _total_run_state_sec в characteristics_json — аналитика уже посчитала
    # накопленное время через всю цепочку суточных резов (inherited_run_state_sec + duration).
    # Fallback: wallclock от t_start (для первого тика, когда chars ещё нет).
    chars        = _parse_json(seg_row.get("characteristics_json")) or {}
    total_rs_sec = chars.get("_total_run_state_sec") or {}
    # JSON ключи — строки; сравниваем и str и int
    time_in_mode_sec = float(
        total_rs_sec.get(run_state) or total_rs_sec.get(str(run_state)) or 0.0
    )
    if not time_in_mode_sec:
        raw_ts = origin_ts or seg_row.get("t_start")
        if raw_ts:
            now_utc = datetime.now(timezone.utc)
            if isinstance(raw_ts, datetime):
                ts = raw_ts if raw_ts.tzinfo else raw_ts.replace(tzinfo=timezone.utc)
            else:
                try:
                    ts = datetime.fromisoformat(str(raw_ts))
                    if not ts.tzinfo:
                        ts = ts.replace(tzinfo=timezone.utc)
                except Exception:
                    ts = now_utc
            time_in_mode_sec = max(0.0, (now_utc - ts).total_seconds())

    # ── Severity (раздельно по источнику) ──
    # Вердикт гейта «отменить» подавляет аналитику, пока состав детекций не изменился
    suppressed    = is_analytics_suppressed(seg_row, active_dets)
    dets_for_level = (
        [d for d in active_dets if d.get("scenario") == "CONTROLLER_FAULT"]
        if suppressed else active_dets
    )
    panel_sev     = compute_panel_severity(active_dets)
    analytics_sev = compute_analytics_severity(dets_for_level)
    sev_level     = compute_severity_level(dets_for_level)

    # ── Активные тревоги с расшифровкой из справочника ──
    panel_alarms:     list[dict] = []
    analytics_alarms: list[dict] = []
    for d in active_dets:
        severity    = d.get("severity", "INFO")
        scenario    = d.get("scenario", "?")
        trigger     = d.get("trigger", "")
        fault_codes = d.get("fault_codes") or []

        # Расшифровка: первый verified-код из справочника
        description: str | None = None
        if fault_ref:
            for code in fault_codes:
                try:
                    entry = fault_ref.lookup(int(code))
                    if entry and entry.get("verified"):
                        desc = entry.get("description") or {}
                        description = desc.get("ru") or desc.get("en")
                        break
                except (TypeError, ValueError):
                    pass

        if not description:
            description = trigger or scenario

        alarm = {
            "scenario":    scenario,
            "severity":    severity,
            "trigger":     trigger,
            "fault_codes": fault_codes,
            "description": description,
        }
        if scenario == "CONTROLLER_FAULT":
            panel_alarms.append(alarm)
        else:
            analytics_alarms.append(alarm)

    # ── Ключевые параметры для режима ──
    key_params: list[dict] = []
    for role in _KEY_ROLES.get(run_state, []):
        if role in values:
            val_data = values[role]
            raw_val = val_data.get("value")
            if raw_val is None:
                raw_val = val_data.get("median")
            if raw_val is not None:
                try:
                    key_params.append({
                        "role":  role,
                        "value": round(float(raw_val), 1),
                        "unit":  val_data.get("unit", ""),
                    })
                except (TypeError, ValueError):
                    pass

    return {
        "run_state":          run_state,
        "mode_label":         mode_label,
        "load_pct":           load_pct,
        "load_pct_bucket":    load_pct_bucket,
        "severity_level":     sev_level,
        "panel_severity":     panel_sev,
        "analytics_severity": analytics_sev,
        "analytics_suppressed": suppressed,
        "time_in_mode_sec":   time_in_mode_sec,
        "panel_alarms":       panel_alarms,
        "analytics_alarms":   analytics_alarms,
        "key_params":         key_params,
    }


def compute_fault_hash(s: dict) -> str:
    """Хэш активных тревог — для детекции новых неисправностей.

    Включает scenario + fault_codes, чтобы аналитические детекции без fault_codes
    (METRIC_RULE) тоже давали уникальный хэш и не считались «уже проанализированными».
    """
    all_alarms = s.get("panel_alarms", []) + s.get("analytics_alarms", [])
    key = tuple(sorted(
        (alarm.get("scenario", ""), tuple(sorted(alarm.get("fault_codes") or [])))
        for alarm in all_alarms
    ))
    return hashlib.md5(str(key).encode()).hexdigest()[:12]


def format_status_text(s: dict) -> str:
    """Детерминированная статус-строка — всегда актуальна, без LLM."""
    mode              = s["mode_label"]
    load              = s.get("load_pct")
    panel_sev         = s.get("panel_severity", "норма")
    analytics_sev     = s.get("analytics_severity", "норма")
    panel_alarms      = s.get("panel_alarms", [])
    analytics_alarms  = s.get("analytics_alarms", [])
    dur               = s.get("time_in_mode_sec", 0)

    # Режим + время + нагрузка
    base = mode
    if dur > 60:
        base += f" {_fmt_duration(dur)}"
    if load is not None and s["run_state"] == 3:
        base += f", нагрузка {load:.0f}%"

    parts = []

    if panel_sev == "авария" and panel_alarms:
        desc = panel_alarms[0].get("description") or panel_alarms[0]["scenario"]
        parts.append(f"🔴 панель: {desc}")
    elif panel_sev == "внимание" and panel_alarms:
        desc = panel_alarms[0].get("description") or panel_alarms[0]["scenario"]
        parts.append(f"🟠 панель: {desc}")

    if analytics_sev == "предупреждение" and analytics_alarms:
        desc = analytics_alarms[0].get("description") or analytics_alarms[0]["scenario"]
        parts.append(f"🟡 аналитика: {desc}")
    elif s.get("analytics_suppressed"):
        # Ненавязчивый след гейта: срабатывание было, ИИ проверил и отменил
        parts.append("✓ аналитика: срабатывание проверено ИИ, угрозы нет")

    if parts:
        return f"{base} — {' | '.join(parts)}."
    return f"{base} — параметры в норме."


def build_warning_prompt(s: dict) -> str:
    """Промпт для Claude-анализа предупреждения (новые fault-коды)."""
    lines = [
        "Выполни анализ неисправности дизель-генераторной установки.",
        "",
        f"Режим работы: {s['mode_label']} (RUN_STATE={s['run_state']})",
    ]

    if s.get("load_pct") is not None:
        lines.append(f"Нагрузка: {s['load_pct']:.0f}%")

    dur = s.get("time_in_mode_sec", 0)
    if dur > 60:
        lines.append(f"Время в режиме: {_fmt_duration(dur)}")

    lines.append(f"Итоговый уровень: {s['severity_level']}")
    lines.append(f"Источник панели: {s.get('panel_severity', 'норма')}")
    lines.append(f"Источник аналитики: {s.get('analytics_severity', 'норма')}")
    if s.get("panel_severity", "норма") == "норма":
        lines.append(
            "Гейт: сигналов панели нет — предупреждение чисто аналитическое, "
            "вердикт cancel ДОПУСТИМ, если угроза не подтверждается."
        )
    else:
        lines.append(
            "Гейт: активны сигналы панели управления — отмена НЕДОСТУПНА, "
            "вердикт только pass."
        )
    lines.append("")

    panel_alarms     = s.get("panel_alarms", [])
    analytics_alarms = s.get("analytics_alarms", [])

    if panel_alarms:
        lines.append("Сигналы панели управления (CONTROLLER_FAULT):")
        for a in panel_alarms:
            desc  = a.get("description") or a["scenario"]
            codes = ", ".join(str(c) for c in a.get("fault_codes", []))
            lines.append(f"  [{a['severity']}] {desc}" + (f" (коды: {codes})" if codes else ""))

    if analytics_alarms:
        if panel_alarms:
            lines.append("")
        lines.append("Сигналы аналитического движка:")
        for a in analytics_alarms:
            desc  = a.get("description") or a["scenario"]
            codes = ", ".join(str(c) for c in a.get("fault_codes", []))
            lines.append(f"  [{a['severity']}] {desc}" + (f" (коды: {codes})" if codes else ""))

    kp = s.get("key_params", [])
    if kp:
        lines.append("")
        lines.append("Ключевые параметры:")
        for p in kp:
            lines.append(f"  {p['role']}: {p['value']} {p['unit']}")

    lines += [
        "",
        "Дай краткий технический анализ: что произошло, возможные причины, "
        "на что обратить внимание оператору. Ответ на русском языке.",
    ]
    return "\n".join(lines)
