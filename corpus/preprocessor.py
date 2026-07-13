# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Препроцессор: формирует вход для Claude из закрытого auto_segment.

Не урезает данные — подаёт полный report_md сегмента as-is.
Добавляет только шапку с вердиктом блока, чтобы Claude не пересматривал его.
"""
from __future__ import annotations
import json
from typing import Any

# Русские названия RUN_STATE (из analytics/serializer.py — дублируем чтобы не создавать зависимость)
RUN_STATE_RU: dict[int, str] = {
    0: "Стоп",
    1: "Задержка пуска",
    2: "Прогрев",
    3: "Работа",
    4: "Разгрузка",
    5: "Охлаждение на х.х.",
    6: "Переход на х.х.",
}


def _parse_json(val: Any) -> Any:
    if val is None:
        return None
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            return None
    return val


def _extract_detections(chars_json: Any) -> list[dict]:
    """Извлечь все детекции из characteristics_json сегмента."""
    parsed = _parse_json(chars_json)
    if not parsed:
        return []
    detections: list[dict] = []
    for sub in parsed.get("subsegments", []):
        for d in sub.get("detections", []):
            detections.append(d if isinstance(d, dict) else {})
    return detections


def _gate_suppressed(segment_row: dict, detections: list[dict]) -> bool:
    """Снял ли гейт (ИИ) аналитические предупреждения сегмента.

    Вердикт «отменить» действует, пока состав аналитических детекций
    не изменился — сверяем hash состава с gate_suppressed_hash сегмента.
    """
    suppressed = segment_row.get("gate_suppressed_hash")
    if not suppressed:
        return False
    from online.status_assembler import compute_analytics_hash
    has_analytics = any(
        isinstance(d, dict) and d.get("scenario") != "CONTROLLER_FAULT"
        for d in detections
    )
    return has_analytics and compute_analytics_hash(detections) == suppressed


def _extract_verdict(detections: list[dict], gate_suppressed: bool = False) -> tuple[str, str]:
    """Вычислить вердикт и уровень тревоги из списка детекций.

    Шкала серьёзности (4 уровня, ALARM исключён — в панелях его нет):
        НОРМА    🟢 — детекций нет / только INFO / аналитика снята ИИ
        CAUTION  🟡 — предупреждение аналитики (не снятое ИИ)
        WARNING  🟠 — предупреждение панели
        SHUTDOWN 🔴 — авария панели

    Returns:
        (verdict, alarm_level)
        verdict:     норма / требует внимания / авария
        alarm_level: НОРМА / CAUTION / WARNING / SHUTDOWN
    """
    sevs = {d.get("severity") for d in detections if isinstance(d, dict) and d.get("severity")}

    if "SHUTDOWN" in sevs:
        return "авария", "SHUTDOWN"
    if "WARNING" in sevs or "ALARM" in sevs:   # ALARM — легаси старых сегментов
        return "требует внимания", "WARNING"
    if "CAUTION" in sevs:
        if gate_suppressed:
            return "норма", "НОРМА"
        return "требует внимания", "CAUTION"
    return "норма", "НОРМА"


# Визуальные метки уровней: emoji + русская расшифровка (мелко в отчёте).
# Emoji вердикта берётся от уровня тревоги — цвет у них общий.
ALARM_LEVEL_META: dict[str, tuple[str, str]] = {
    "НОРМА":    ("🟢", "всё в порядке"),
    "CAUTION":  ("🟡", "предупреждение аналитики"),
    "WARNING":  ("🟠", "предупреждение панели"),
    "SHUTDOWN": ("🔴", "авария панели"),
}


def _fmt_detections(detections: list[dict]) -> str:
    """Форматировать список детекций для шапки."""
    if not detections:
        return "нет"
    parts = []
    for d in detections:
        sev = d.get("severity", "INFO")
        scenario = d.get("scenario", "?")
        trigger = d.get("trigger", "")
        parts.append(f"{scenario} ({sev}): {trigger}")
    return "; ".join(parts)


# ── Агрегированные «Обнаружения» для БЛОКА 1 заключения ──────────────────────
# Человеческие названия сценариев — канонический словарь в analytics/serializer
from analytics.serializer import SCENARIO_RU  # noqa: E402

_SEV_EMOJI = {"SHUTDOWN": "🔴", "ALARM": "🟠", "WARNING": "🟠",
              "CAUTION": "🟡", "INFO": "🔵"}
_SEV_RANK = {"SHUTDOWN": 4, "ALARM": 3, "WARNING": 3, "CAUTION": 2, "INFO": 1}


def _fmt_dur(sec: float) -> str:
    s = int(sec)
    h, m = s // 3600, (s % 3600) // 60
    if h:
        return f"{h}ч {m:02d}м"
    if m:
        return f"{m}м"
    return f"{s}с"


def _plural_hits(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} срабатывание"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} срабатывания"
    return f"{n} срабатываний"


def _group_worst(scenario: str, values_list: list[dict]) -> str | None:
    """Худший показатель группы за период — вместо дампа каждого срабатывания."""
    def _nums(key):
        return [v[key] for v in values_list
                if isinstance(v.get(key), (int, float))]

    if scenario == "OIL_DILUTION":
        mins = _nums("oil_press_min")
        passports = _nums("passport_min_kpa")
        if mins:
            base = f"мин. {min(mins):.0f} кПа"
            return base + (f" при паспорте ≥{passports[0]:.0f}" if passports else "")
    elif scenario == "LIMIT_PROXIMITY":
        vals, limits = _nums("value"), _nums("shutdown_limit")
        if vals and limits:
            return f"макс. {max(vals):.0f} при пороге {limits[0]:.0f}"
    elif scenario == "RPM_UNDERSPEED":
        mins = _nums("rpm_min")
        if mins:
            return f"мин. {min(mins):.0f} об/мин"
    elif scenario == "COKING_RISK":
        secs = _nums("low_load_zone_sec")
        if secs:
            return f"до {_fmt_dur(max(secs))} подряд"
    elif scenario == "COOLING_FAILURE":
        temps = _nums("coolant_temp_max")
        slopes = _nums("coolant_slope_c_per_s")
        if temps:
            return f"макс. {max(temps):.0f}°C"
        if slopes:
            return f"тренд до {max(slopes):.4f} °C/с"
    return None


def _fmt_detections_hierarchy(chars_json: Any) -> str:
    """Агрегированные обнаружения для БЛОКА 1: иерархия панель→аналитика,
    по-русски, с количеством срабатываний, длительностью и худшим показателем.

    Длительность = сумма длительностей подсегментов, где сценарий срабатывал
    (у самой детекции длительности нет).
    """
    parsed = _parse_json(chars_json)
    subsegments = (parsed or {}).get("subsegments", [])

    # key → агрегат; ключ различает панельные аварии по имени,
    # LIMIT_PROXIMITY — по параметру (из префикса триггера: «Температура масла: …»)
    groups: dict[tuple, dict] = {}
    for sub in subsegments:
        if not isinstance(sub, dict):
            continue
        sub_dur = sub.get("duration_sec") or 0
        seen_in_sub: set[tuple] = set()
        for d in sub.get("detections", []):
            if not isinstance(d, dict):
                continue
            scenario = d.get("scenario", "?")
            severity = d.get("severity", "INFO")
            trigger = d.get("trigger", "") or ""
            is_panel = d.get("source") == "CONTROLLER_FAULT" or scenario == "CONTROLLER_FAULT"

            if is_panel:
                # «Имя неисправности (addr=…, bit=…)» → имя без адресов
                name = trigger.split(" (addr=")[0].strip() or SCENARIO_RU["CONTROLLER_FAULT"]
                key = ("panel", name)
            elif scenario == "LIMIT_PROXIMITY" and ":" in trigger:
                param = trigger.split(":", 1)[0].strip()
                name = f"{param} у паспортного порога"
                key = ("analytics", scenario, param)
            else:
                name = SCENARIO_RU.get(scenario, scenario)
                key = ("analytics", scenario)

            g = groups.setdefault(key, {
                "name": name, "scenario": scenario, "panel": is_panel,
                "sev_rank": 0, "severity": severity,
                "count": 0, "dur_sec": 0.0, "values": [],
                "fault_codes": set(),
            })
            g["count"] += 1
            if _SEV_RANK.get(severity, 0) > g["sev_rank"]:
                g["sev_rank"] = _SEV_RANK.get(severity, 0)
                g["severity"] = severity
            if isinstance(d.get("values"), dict):
                g["values"].append(d["values"])
            for fc in d.get("fault_codes") or []:
                g["fault_codes"].add(fc)
            if key not in seen_in_sub:
                seen_in_sub.add(key)
                g["dur_sec"] += sub_dur

    def _render(g: dict) -> str:
        parts = [_plural_hits(g["count"])]
        if g["count"] > 1 and g["dur_sec"] > 0:
            parts.append(f"суммарно {_fmt_dur(g['dur_sec'])}")
        worst = _group_worst(g["scenario"], g["values"])
        if worst:
            parts.append(worst)
        emoji = _SEV_EMOJI.get(g["severity"], "⚪")
        line = f"    - {emoji} {g['name']} — {', '.join(parts)}"
        if g["panel"] and g["fault_codes"]:
            codes = "/".join(str(c) for c in sorted(g["fault_codes"]))
            line += f" (код {codes})"
        return line

    ordered = sorted(groups.values(), key=lambda g: (-g["sev_rank"], -g["count"]))
    panel_alarm = [g for g in ordered if g["panel"] and g["sev_rank"] >= 4]
    panel_warn  = [g for g in ordered if g["panel"] and g["sev_rank"] < 4]
    analytics   = [g for g in ordered if not g["panel"]]

    lines: list[str] = []
    for emoji, title, items in (("🔴", "Аварии панели", panel_alarm),
                                ("🟠", "Предупреждения панели", panel_warn),
                                ("🟡", "Предупреждения аналитики", analytics)):
        if items:
            lines.append(f"- {emoji} **{title}:**")
            lines.extend(_render(g) for g in items)
        else:
            lines.append(f"- {emoji} **{title}** — нет")
    return "\n".join(lines)


def build_claude_input(segment_row: dict) -> str:
    """Сформировать полный вход для Claude: шапка вердикта + report_md as-is."""
    chars_json = segment_row.get("characteristics_json")
    run_state = segment_row.get("run_state")
    report_md = segment_row.get("report_md") or ""

    detections = _extract_detections(chars_json)
    verdict, alarm_level = _extract_verdict(
        detections, _gate_suppressed(segment_row, detections)
    )
    run_state_label = (
        RUN_STATE_RU.get(run_state, str(run_state))
        if run_state is not None
        else "Неизвестно"
    )

    header = (
        f"[ВЕРДИКТ БЛОКА АНАЛИТИКИ — НЕ ПЕРЕСМАТРИВАТЬ]\n"
        f"Режим: {run_state_label} (RUN_STATE={run_state})\n"
        f"Вердикт: {verdict}\n"
        f"Уровень тревоги: {alarm_level}\n"
        f"Обнаружения: {_fmt_detections(detections)}\n"
        f"Задача: объясни зафиксированную картину. "
        f"Не пересматривай вердикт. Не ищи дополнительных проблем.\n"
        f"---\n\n"
    )
    return header + report_md


def extract_verdict_alarm(segment_row: dict) -> tuple[str, str]:
    """Публичный метод: (verdict, alarm_level) для записи в БД."""
    chars_json = segment_row.get("characteristics_json")
    detections = _extract_detections(chars_json)
    return _extract_verdict(detections, _gate_suppressed(segment_row, detections))
