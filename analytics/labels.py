# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Подписи неудачных и неподтверждённых выходов из стопа (v5.1.16).

Одна функция на все места — календарь, отчёт, строку статуса, разбор аварии:
плашка неудачной попытки — это стоп с исходом, а подпись говорит, что было
до него («Неудачная работа · Авария»). Решение владельца 13.10.2026.
"""
from __future__ import annotations

from typing import Any

# Попытка какого режима не подтвердилась (по старшему в цепочке)
_ATTEMPT_HEAD = {2: "Неудачный прогрев", 3: "Неудачная работа"}
# Пока исход неизвестен — режим по сигналу панели
_PENDING_HEAD = {1: "Задержка пуска", 2: "Прогрев", 3: "Работа"}


def attempt_label(attempt: dict[str, Any] | None) -> str | None:
    """«Неудачная работа · Авария» / «Неудачный прогрев · Стоп» / … ; None — не попытка."""
    if not isinstance(attempt, dict):
        return None
    try:
        fs = int(attempt.get("from_state") or 3)
    except (TypeError, ValueError):
        fs = 3
    emergency = attempt.get("outcome") == "EMERGENCY"
    if fs == 1:
        # Пуск снят на задержке — не неудача машины; с аварией — неудачный пуск
        head = "Неудачный пуск" if emergency else "Отменённый пуск"
    else:
        head = _ATTEMPT_HEAD.get(fs, "Неудачный пуск")
    return f"{head} · {'Авария' if emergency else 'Стоп'}"


def pending_label(run_state: int | None) -> str:
    """Выход из стопа идёт, факта ещё нет."""
    base = _PENDING_HEAD.get(run_state if run_state is not None else -1, "Пуск")
    return f"{base} — ждём подтверждения"


def label_from_chars(chars: dict[str, Any] | None) -> str | None:
    """Подпись по characteristics_json строки сегмента, если это попытка."""
    if not isinstance(chars, dict):
        return None
    lab = attempt_label(chars.get("attempt"))
    if lab:
        return lab
    if chars.get("attempt_pending"):
        return pending_label(chars.get("run_state"))
    return None


# Модель в segment_analyses у заключения-фактов без ИИ (плашки, v5.1.16)
SYSTEM_MODEL = "Система (без ИИ)"


def conclusion_by(claude_model: str | None) -> str | None:
    """Кто написал заключение: «ai» — модель, «system» — код без ИИ, None — нет.

    Для UI (v5.1.19): значок и заголовок «Заключение ИИ» — только у модели.
    """
    if not claude_model:
        return None
    return "system" if str(claude_model) == SYSTEM_MODEL else "ai"


def attempt_facts_ru(attempt: dict[str, Any] | None) -> str | None:
    """Факты попытки одной строкой: что не подтвердило режим."""
    if not isinstance(attempt, dict):
        return None
    f = attempt.get("facts") or {}
    parts = []
    rpm = f.get("rpm_max")
    parts.append(f"обороты до {rpm:.0f} об/мин" if isinstance(rpm, (int, float))
                 else "обороты неизвестны")
    fq = f.get("freq_max")
    parts.append(f"частота до {fq:.1f} Гц" if isinstance(fq, (int, float)) and fq > 0
                 else "частоты нет")
    parts.append("«Готов к нагрузке» был" if f.get("ready") else "«Готов к нагрузке» не выставлен")
    dur = attempt.get("duration_sec")
    lead = (f"попытка {dur:.0f} с" if isinstance(dur, (int, float)) else "попытка")
    return f"{lead}: " + ", ".join(parts)
