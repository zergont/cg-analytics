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


def fmt_duration(sec: Any, seconds: bool = True) -> str:
    """Длительность как в UI: «18 ч 00 мин», «6 мин 15 с», «8 мин», «40 с» (v5.1.20).

    От часа — без секунд; seconds=False — без секунд и до часа («6 мин»): для
    строки статуса, которая пишется раз в тик и с секундами выглядит устаревшей.
    Дробная часть отбрасывается, как Math.floor в UI.
    """
    try:
        s = max(0, int(float(sec or 0)))
    except (TypeError, ValueError):
        s = 0
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h} ч {m:02d} мин"
    if m:
        return f"{m} мин {s} с" if seconds and s else f"{m} мин"
    return f"{s} с"


# Короткие русские названия ролей — таблица «Ключевые показатели» (v5.1.20).
# В KB у ролей только длинные описания (многострочные у QSX15), поэтому — словарь
# в коде; неизвестная роль печатается кодом
ROLE_LABELS: dict[str, str] = {
    "ACTIVE_POWER_TOTAL": "Активная мощность",
    "ACTIVE_POWER_L1": "Активная мощность L1",
    "ACTIVE_POWER_L2": "Активная мощность L2",
    "ACTIVE_POWER_L3": "Активная мощность L3",
    "REACTIVE_POWER_TOTAL": "Реактивная мощность",
    "APPARENT_POWER_TOTAL": "Полная мощность",
    "POWER_FACTOR": "Коэффициент мощности",
    "RPM": "Обороты двигателя",
    "LOAD_PCT": "Нагрузка",
    "LOAD_PCT_TOTAL": "Нагрузка (общая)",
    "COOLANT_TEMP": "Температура ОЖ",
    "OIL_TEMP": "Температура масла",
    "OIL_PRESS": "Давление масла",
    "FREQUENCY": "Частота генератора",
    "BATTERY_V": "Напряжение АКБ",
    "ECM_BATTERY_V": "Питание ЭБУ",
    "INTAKE_MANIFOLD_TEMP": "Температура воздуха на впуске",
    "BOOST_PRESSURE": "Давление наддува",
    "FUEL_RATE": "Расход топлива",
    "FUEL_TOTAL": "Расход топлива с начала эксплуатации",
    "FUEL_OUTLET_PRESS": "Давление топлива после фильтра",
    "VOLTAGE_LL_AVG": "Линейное напряжение",
    "CURRENT_AVG": "Средний ток",
    "CURRENT_L1": "Ток L1",
    "CURRENT_L2": "Ток L2",
    "CURRENT_L3": "Ток L3",
    "ENGINE_HOURS": "Наработка",
}

# Единицы карты регистров → по-русски. psi и gal/h — единицы датчика ДЭС №3:
# пересчёт разошёлся бы с панелью (решение 14.10.2026)
UNIT_RU: dict[str, str] = {
    "kW": "кВт", "kVAr": "квар", "kVA": "кВА", "rpm": "об/мин", "Hz": "Гц",
    "V": "В", "Vdc": "В", "Vac": "В", "A": "А", "kPa": "кПа", "h": "ч",
}


def role_label(role: str | None) -> str:
    return ROLE_LABELS.get(role or "", role or "?")


def unit_ru(unit: str | None) -> str:
    return UNIT_RU.get(unit or "", unit or "")


def number_decimals(values: list[Any]) -> int:
    """Знаков после запятой на строку таблицы: от 10 — один, меньше — два.

    Одна точность на строку: «20,8 | 2,4 | 49,4», а не «20,8 | 2,38 | 49,4».
    """
    nums = []
    for v in values:
        try:
            nums.append(abs(float(v)))
        except (TypeError, ValueError):
            pass
    return 1 if nums and max(nums) >= 10 else 2


def fmt_number(v: Any, decimals: int | None = None) -> str:
    """Число для таблицы без лишних нулей: «581», «49,9», «0,85» (v5.1.20)."""
    if v is None:
        return "—"
    try:
        x = float(v)
    except (TypeError, ValueError):
        return str(v)
    if decimals is None:
        decimals = 1 if abs(x) >= 10 else 2
    txt = f"{x:.{decimals}f}"
    if "." in txt:
        txt = txt.rstrip("0").rstrip(".")
    if txt in ("-0", ""):
        txt = "0"
    return txt.replace(".", ",")


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
