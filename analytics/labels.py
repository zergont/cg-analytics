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

v5.1.22: местное время подписей (fmt_local, tz_label — «МСК» во всех входах и
фактах, P1-6) и подпись снятия гейтом с источником (gate_supp_label, P1-1);
код 40012 на закрытии сегмента — «сброшен / снят / сменился»
(fault_code_close_text, P1-2, решение В7), сегмент, оборванный аварийным
остановом (run_trip_name / run_trip_text, P1-3, решение В3), наработка
строкой шапки (engine_hours_text).
"""
from __future__ import annotations

import re
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


# ── Местное время в подписях (v5.1.22, P1-6) ─────────────────────────────────

def _local_tz(tz=None):
    """Пояс подписи: переданный, иначе пояс разбивки суток (config.get_tz)."""
    if tz is not None:
        return tz
    try:
        from config import get_tz
        return get_tz()
    except Exception:
        return None


def fmt_local(ts: Any, tz=None, day: bool = False, seconds: bool = True) -> str | None:
    """Метка в местном поясе: «09:26:23», day=True — «16.10 09:26:23»,
    seconds=False — без секунд. ts — datetime или ISO; без пояса — UTC.
    None — метки нет или она битая."""
    from datetime import datetime, timezone
    if ts is None or ts == "":
        return None
    if isinstance(ts, datetime):
        dt = ts
    else:
        try:
            dt = datetime.fromisoformat(str(ts))
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    z = _local_tz(tz)
    if z is not None:
        dt = dt.astimezone(z)
    fmt = ("%d.%m " if day else "") + ("%H:%M:%S" if seconds else "%H:%M")
    return dt.strftime(fmt)


def tz_label(tz=None) -> str:
    """«МСК» для Europe/Moscow, иначе «UTC+N» (по текущему смещению пояса)."""
    from datetime import datetime, timezone
    z = _local_tz(tz)
    if z is None:
        return "UTC"
    if getattr(z, "key", None) == "Europe/Moscow":
        return "МСК"
    off = datetime.now(timezone.utc).astimezone(z).utcoffset()
    if off is None:
        return "UTC"
    mins = int(off.total_seconds() // 60)
    if mins == 0:
        return "UTC"
    sign = "+" if mins > 0 else "-"
    h, m = divmod(abs(mins), 60)
    return f"UTC{sign}{h}" + (f":{m:02d}" if m else "")


def gate_supp_label(srcs: list[dict[str, Any]] | dict[str, Any] | None, tz=None,
                    plural: bool = False) -> str:
    """Подпись снятия гейтом (v5.1.22, P1-1): «снято ИИ (разбор 09:59)» — решение
    принято в этой карточке (own), «снято по разбору от 14.10 03:36 (#35876)» —
    раньше, в другой карточке. Несколько источников — через «; ». Без источника
    — «снято ИИ», как в частях до v5.1.22. plural — «сняты» (примечание вердикта).
    srcs — [{ts, segment_id, own}] или один такой словарь."""
    verb = "сняты" if plural else "снято"
    if isinstance(srcs, dict):
        srcs = [srcs]
    own_t: list[str] = []
    other_t: list[str] = []
    for s in srcs or []:
        if not isinstance(s, dict) or not s.get("ts"):
            continue
        if s.get("own"):
            t = fmt_local(s.get("ts"), tz, seconds=False)
            if t and t not in own_t:
                own_t.append(t)
        else:
            t = fmt_local(s.get("ts"), tz, day=True, seconds=False)
            sid = s.get("segment_id")
            ref = f"от {t}" + (f" (#{sid})" if sid else "") if t else None
            if ref and ref not in other_t:
                other_t.append(ref)
    if own_t and other_t:
        return f"{verb} ИИ (разбор {', '.join(own_t)}; по разбору {'; '.join(other_t)})"
    if own_t:
        return f"{verb} ИИ (разбор {', '.join(own_t)})"
    if other_t:
        return f"{verb} по разбору {'; '.join(other_t)}"
    return f"{verb} ИИ"


# ── Код 40012 на закрытии, обрыв сегмента аварией, наработка (v5.1.22) ───────

# Метка кода в каталоге БД телеметрии: «Пониженная частота (ОСТАНОВ)» — тип
# суффиксом заглавными (ОСТАНОВ, ПРЕДУПРЕЖДЕНИЕ, СОБЫТИЕ, СНИЖЕНИЕ НОМИНАЛА, …)
_CODE_KIND_RE = re.compile(r"^(.*?)\s*\(([А-ЯЁA-Z][А-ЯЁA-Z \-]*)\)\s*$")


def split_code_label(label: Any) -> tuple[str | None, str | None]:
    """«Пониженная частота (ОСТАНОВ)» → («Пониженная частота», «останов»).

    Без суффикса — (метка, None); метки нет или это само число (каталог не
    знает кода) — (None, None).
    """
    if label is None:
        return None, None
    s = str(label).strip()
    if not s or s.isdigit():
        return None, None
    m = _CODE_KIND_RE.match(s)
    if m and m.group(1):
        return m.group(1), m.group(2).lower()
    return s, None


def _code_ref(code: Any, name: str | None) -> str:
    return f"{code} «{name}»" if name else f"{code}"


def fault_code_close_text(rec: dict[str, Any] | None, tz=None, regs: bool = True) -> str | None:
    """Что стало с кодом 40012 на закрытии сегмента (v5.1.22, P1-2, решение В7).

    Сброшен (подъём «Сброса неисправностей» 40569 перед снятием или переход
    переключателя 40010 в «Выкл» в кадре снятия):
      «Код 1448 сброшен: сброс неисправностей 09:26:32 (40569), код снят 09:26:43;
      переключатель «Выкл» (40010) в том же кадре».
    Признака сброса нет: «Код 1248 снят в 14:59:49» (+ «, в момент пуска»); а
    следом встал другой код — «Код 146 сменился в 16:26:11 на код 151 — он
    относится к следующему сегменту» (ревью verdict-2).
    Сброшен, и следом встал другой код: «…; сразу после сброса в регистре код 1248
    «Предупреждение двигателя — общее» (предупреждение) — он относится к
    следующему сегменту». Слов «кнопка» и «несброшенная» нет: сброс бывает и с
    панели, и переключателем, а код на конец сегмента не висит.
    regs=False — без номеров регистров (строка сводки для оператора).
    rec — Segment.fault_code_close (segmenter.fault_code_close); None — нет записи.
    """
    if not isinstance(rec, dict) or rec.get("code") is None:
        return None
    code = rec.get("code")
    t_clear = fmt_local(rec.get("t_clear"), tz)
    if not t_clear:
        return None
    r_at = fmt_local(rec.get("reset_at"), tz) if rec.get("reset_at") else None
    sw_raw = rec.get("switch_off_at")
    sw_at = fmt_local(sw_raw, tz) if sw_raw else None
    sw_lab = rec.get("switch_label") or "Выкл"
    if r_at or sw_at:
        bits: list[str] = []
        if r_at:
            bits.append(f"сброс неисправностей {r_at}" + (" (40569)" if regs else ""))
        same = False
        if sw_at:
            try:
                from datetime import datetime as _dt
                same = abs((_dt.fromisoformat(str(sw_raw))
                            - _dt.fromisoformat(str(rec.get("t_clear")))).total_seconds()) <= 1.0
            except (TypeError, ValueError):
                same = False
        if sw_at and not r_at:
            bits.append(f"переключатель «{sw_lab}»" + (" (40010)" if regs else "")
                        + ("" if same else f" {sw_at}"))
        bits.append(f"код снят {t_clear}")
        txt = f"Код {code} сброшен: " + ", ".join(bits)
        if sw_at and r_at:
            txt += (f"; переключатель «{sw_lab}»" + (" (40010)" if regs else "")
                    + (" в том же кадре" if same else f" {sw_at}"))
        after = "сброса"
    elif rec.get("next_code"):
        # Признака сброса нет, а следом встал другой код — код не снимали, его
        # вытеснили (v5.1.22, ревью verdict-2; план P1-2 «сменился»): ДЭС №3 16.09
        # 146 → 151 в кадре аварийного останова
        nc = rec.get("next_code")
        kind = f" ({rec['next_kind']})" if rec.get("next_kind") else ""
        return (f"Код {code} сменился в {t_clear} на код {_code_ref(nc, rec.get('next_name'))}{kind}"
                f" — он относится к следующему сегменту")
    else:
        txt = f"Код {code} снят в {t_clear}"
        if rec.get("at_end") and rec.get("next_state") in (1, 2, 3):
            txt += ", в момент пуска"
        after = "снятия"
    nc = rec.get("next_code")
    if nc:
        kind = f" ({rec['next_kind']})" if rec.get("next_kind") else ""
        txt += (f"; сразу после {after} в регистре код {_code_ref(nc, rec.get('next_name'))}{kind}"
                f" — он относится к следующему сегменту")
    return txt


# Имя замечания «сегмент оборван аварией» по режиму — род слова режима
_TRIP_HEAD = {1: "Задержка пуска прервана", 2: "Прогрев прерван", 3: "Работа прервана",
              4: "Разгрузка прервана", 5: "Охлаждение на х.х. прервано",
              6: "Переход на х.х. прерван"}


def run_trip_name(run_state: Any) -> str:
    """«Работа прервана аварийным остановом» / «Прогрев прерван …» (v5.1.22, P1-3)."""
    try:
        rs = int(run_state)
    except (TypeError, ValueError):
        rs = -1
    return f"{_TRIP_HEAD.get(rs, 'Режим прерван')} аварийным остановом"


def run_trip_text(trip: dict[str, Any] | None) -> str:
    """Чем оборван сегмент: «код 234 «Разнос двигателя — превышение частоты
    вращения»»; кода нет — маски аварийной тяжести; нет и их — «аварийный
    останов панели» (v5.1.22, P1-3, решение В3)."""
    if not isinstance(trip, dict):
        return "аварийный останов панели"
    codes = [c for c in trip.get("codes") or [] if isinstance(c, dict) and c.get("code")]
    if codes:
        return ", ".join(f"код {_code_ref(c['code'], c.get('name'))}" for c in codes)
    masks = [m for m in trip.get("masks") or [] if m]
    if masks:
        return ", ".join(f"«{m}»" for m in masks)
    return "аварийный останов панели"


def shutdown_cleared_how(mttr: dict[str, Any] | None) -> str:
    """Чем панель сняла аварию — по источнику интервала аварии (часть mttr, ключ
    by, v5.1.22, ревью verdict-3): маска аварийной тяжести — «сняла аварийную
    маску»; авария без бита в масках (1452 «Отказ включения автомата» — только
    тип неисправности 40013) — так и пишем; части без ключа — нейтрально."""
    by = (mttr or {}).get("by") if isinstance(mttr, dict) else None
    if by == "mask":
        return "панель сняла аварийную маску"
    if by == "state":
        return ("панель сняла аварию (аварийной маски не было — авария по типу "
                "неисправности 40013)")
    return "панель сняла аварию"


def engine_hours_text(hours: Any) -> str | None:
    """«13 643 ч» — наработка по счётчику панели; None — значения нет."""
    try:
        h = float(hours)
    except (TypeError, ValueError):
        return None
    if h < 0:
        return None
    return f"{round(h):,}".replace(",", " ") + " ч"
