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


def _extract_verdict(detections: list[dict]) -> tuple[str, str]:
    """Вычислить вердикт и уровень тревоги из списка детекций.

    Returns:
        (verdict, alarm_level)
        verdict:     норма / требует_внимания / отклонение
        alarm_level: нет / INFO / WARNING / ALARM / SHUTDOWN
    """
    if not detections:
        return "норма", "нет"

    sevs = {d.get("severity") for d in detections if d.get("severity")}

    if "SHUTDOWN" in sevs:
        return "отклонение", "SHUTDOWN"
    if "ALARM" in sevs:
        return "отклонение", "ALARM"
    if "WARNING" in sevs:
        return "требует_внимания", "WARNING"
    return "норма", "INFO"


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


def build_claude_input(segment_row: dict) -> str:
    """Сформировать полный вход для Claude: шапка вердикта + report_md as-is."""
    chars_json = segment_row.get("characteristics_json")
    run_state = segment_row.get("run_state")
    report_md = segment_row.get("report_md") or ""

    detections = _extract_detections(chars_json)
    verdict, alarm_level = _extract_verdict(detections)
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
    return _extract_verdict(detections)
