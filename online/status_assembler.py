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
    # Используем origin_ts если передан (реальное начало режима через цепочку
    # continued_from), иначе t_start текущего сегмента.
    raw_ts = origin_ts or seg_row.get("t_start")
    time_in_mode_sec = 0.0
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

    Реагирует на: смену режима, уровня тревоги, набора fault-кодов, ведра нагрузки,
    каждый час работы в режиме, изменение ключевых параметров на 10+ единиц.
    НЕ реагирует на мелкие флуктуации аналоговых значений.
    """
    fault_codes = tuple(sorted(
        code
        for alarm in s.get("active_alarms", [])
        for code in alarm.get("fault_codes", [])
    ))
    # Часовой бакет: текст регенерируется раз в час (актуализирует "время в режиме")
    time_bucket = int(s.get("time_in_mode_sec", 0) // 3600)
    # Ключевые параметры с шагом 10 единиц (игнорируем мелкий шум, ловим значимые изменения)
    params_bucket = tuple(
        (p["role"], int(p["value"] // 10) * 10)
        for p in sorted(s.get("key_params", []), key=lambda x: x["role"])
    )
    key = (
        s["run_state"],
        s["severity_level"],
        s.get("load_pct_bucket"),
        fault_codes,
        time_bucket,
        params_bucket,
    )
    return hashlib.md5(str(key).encode()).hexdigest()[:12]


def compute_fault_hash(s: dict) -> str:
    """Хэш только набора fault-кодов — для детекции новых неисправностей."""
    fault_codes = tuple(sorted(
        code
        for alarm in s.get("active_alarms", [])
        for code in alarm.get("fault_codes", [])
    ))
    return hashlib.md5(str(fault_codes).encode()).hexdigest()[:12]


def format_status_text(s: dict) -> str:
    """Детерминированная статус-строка — всегда актуальна, без LLM."""
    mode   = s["mode_label"]
    load   = s.get("load_pct")
    level  = s["severity_level"]
    alarms = s.get("active_alarms", [])
    dur    = s.get("time_in_mode_sec", 0)

    # Режим + время (если > 1 мин)
    base = mode
    if dur > 60:
        base += f" {_fmt_duration(dur)}"

    # Нагрузка — только в режиме Работа
    if load is not None and s["run_state"] == 3:
        base += f", нагрузка {load:.0f}%"

    if level == "норма":
        return f"{base} — параметры в норме."
    elif alarms:
        first = alarms[0]
        desc = first.get("description") or first["scenario"]
        marker = "⚠" if level == "внимание" else "🔴"
        return f"{base} — {marker} {level}: {desc}."
    else:
        return f"{base} — {level}."


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

    lines.append(f"Уровень: {s['severity_level']}")
    lines.append("")

    alarms = s.get("active_alarms", [])
    if alarms:
        lines.append("Активные неисправности:")
        for a in alarms:
            desc = a.get("description") or a["scenario"]
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
