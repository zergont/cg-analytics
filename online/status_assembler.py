"""ИИ-оператор Уровень 1: детерминированный сборщик структурного статуса машины.

Принцип: статус и факты — из открытого сегмента (детерминированно).
qwen только облекает в прозу, не интерпретирует.

Публичный API:
  build_structural_status(seg_row, fault_ref, tz) → dict
  compute_status_hash(struct_status) → str
  compute_severity_level(active_dets) → str
  build_fallback_text(struct_status) → str
  build_status_prompt(struct_status) → str
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

_SEV_RANK: dict[str, int] = {"SHUTDOWN": 3, "ALARM": 3, "WARNING": 2, "INFO": 1}


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

def compute_severity_level(active_dets: list[dict]) -> str:
    """Вычислить уровень серьёзности: норма / внимание / тревога."""
    if not active_dets:
        return "норма"
    max_rank = max((_SEV_RANK.get(d.get("severity", ""), 0) for d in active_dets), default=0)
    if max_rank >= 3:
        return "тревога"
    if max_rank >= 1:
        return "внимание"
    return "норма"


def build_structural_status(
    seg_row: dict,
    fault_ref=None,
    tz=None,
) -> dict[str, Any]:
    """Собрать структурный статус из строки открытого сегмента.

    Args:
        seg_row:   строка из auto_segments (dict)
        fault_ref: FaultRef для расшифровки кодов тревог (может быть None)
        tz:        часовой пояс (не используется для вычислений, зарезервирован)

    Returns:
        dict с полями:
          run_state, mode_label, load_pct, load_pct_bucket,
          severity_level, time_in_mode_sec, active_alarms[], key_params[]
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
    t_start = seg_row.get("t_start")
    time_in_mode_sec = 0.0
    if t_start:
        now_utc = datetime.now(timezone.utc)
        if isinstance(t_start, datetime):
            ts = t_start if t_start.tzinfo else t_start.replace(tzinfo=timezone.utc)
        else:
            try:
                ts = datetime.fromisoformat(str(t_start))
                if not ts.tzinfo:
                    ts = ts.replace(tzinfo=timezone.utc)
            except Exception:
                ts = now_utc
        time_in_mode_sec = max(0.0, (now_utc - ts).total_seconds())

    # ── Severity ──
    sev_level = compute_severity_level(active_dets)

    # ── Активные тревоги с расшифровкой из справочника ──
    active_alarms: list[dict] = []
    for d in active_dets:
        severity   = d.get("severity", "INFO")
        scenario   = d.get("scenario", "?")
        trigger    = d.get("trigger", "")
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

        active_alarms.append({
            "scenario":    scenario,
            "severity":    severity,
            "trigger":     trigger,
            "fault_codes": fault_codes,
            "description": description,
        })

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
        "run_state":         run_state,
        "mode_label":        mode_label,
        "load_pct":          load_pct,
        "load_pct_bucket":   load_pct_bucket,
        "severity_level":    sev_level,
        "time_in_mode_sec":  time_in_mode_sec,
        "active_alarms":     active_alarms,
        "key_params":        key_params,
    }


def compute_status_hash(s: dict) -> str:
    """Хэш структурного статуса для детекции изменений.

    Реагирует на: смену режима, уровня тревоги, набора fault-кодов, ведра нагрузки.
    НЕ реагирует на флуктуации аналоговых значений.
    """
    fault_codes = tuple(sorted(
        code
        for alarm in s.get("active_alarms", [])
        for code in alarm.get("fault_codes", [])
    ))
    key = (
        s["run_state"],
        s["severity_level"],
        s.get("load_pct_bucket"),
        fault_codes,
    )
    return hashlib.md5(str(key).encode()).hexdigest()[:12]


def build_fallback_text(s: dict) -> str:
    """Детерминированная строка без qwen — показывается до первой генерации.

    Даёт оператору полезную информацию немедленно, пока qwen в очереди.
    """
    mode   = s["mode_label"]
    load   = s.get("load_pct")
    level  = s["severity_level"]
    alarms = s.get("active_alarms", [])
    dur    = s.get("time_in_mode_sec", 0)

    # Базовая часть с нагрузкой
    if load is not None and s["run_state"] == 3:
        base = f"{mode}, нагрузка {load:.0f}%"
    else:
        base = mode

    # Время в режиме (если > 1 минуты)
    if dur > 60:
        base += f" ({_fmt_duration(dur)})"

    # Статус
    if level == "норма":
        text = f"{base} — параметры в норме."
    elif alarms:
        first = alarms[0]
        desc = first.get("description") or first["scenario"]
        emoji = "⚠" if level == "внимание" else "🔴"
        text = f"{base} — {emoji} {level}: {desc}."
    else:
        text = f"{base} — {level}."

    return text + " Анализ готовится…"


def build_status_prompt(s: dict) -> str:
    """Промпт для qwen: структурный статус → 1-2 фразы живой прозы."""
    lines = [
        "[ФАКТЫ ДЛЯ СВОДКИ]",
        f"Режим: {s['mode_label']} (RUN_STATE={s['run_state']})",
    ]

    if s.get("load_pct") is not None:
        lines.append(f"Нагрузка: {s['load_pct']:.0f}%")

    dur = s.get("time_in_mode_sec", 0)
    if dur > 60:
        lines.append(f"Время в режиме: {_fmt_duration(dur)}")

    lines.append(f"Статус: {s['severity_level']}")

    alarms = s.get("active_alarms", [])
    if alarms:
        lines.append("Активные тревоги:")
        for a in alarms:
            desc = a.get("description") or a["scenario"]
            lines.append(f"  - {a['severity']}: {desc}")

    kp = s.get("key_params", [])
    if kp:
        lines.append("Ключевые параметры:")
        for p in kp:
            lines.append(f"  - {p['role']}: {p['value']} {p['unit']}")

    return "\n".join(lines)
