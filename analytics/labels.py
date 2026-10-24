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
до него. Решение владельца 13.10.2026 («Неудачная работа · Авария» — по режиму
панели), с v5.1.23 — по прокрутке («Неудачный пуск · Авария», решение В4).

v5.1.22: местное время подписей (fmt_local, tz_label — «МСК» во всех входах и
фактах, P1-6) и подпись снятия гейтом с источником (gate_supp_label, P1-1);
код 40012 на закрытии сегмента — «сброшен / снят / сменился»
(fault_code_close_text, P1-2, решение В7), сегмент, оборванный аварийным
остановом (run_trip_name / run_trip_text, P1-3, решение В3), наработка
строкой шапки (engine_hours_text).

v5.1.23: тексты профиля пуска (решение В1) — строка «Пуск» отчёта, акта и
входа гейта (start_profile_text), пометка «на пуске» в сводке
(start_note_text), причины неустойчивости (start_reasons_ru), окно пуска
(start_window). Подпись плашки попытки по прокрутке, а не по режиму панели
(attempt_label, решение В4), факты попытки — только наблюдаемое
(attempt_facts_ru); метка значения enum-регистра — KB поверх каталога
(enum_label), имя неисправности панели по привязанному коду
(panel_fault_name, решение В5).

v5.1.25: попытка без отсчётов оборотов внутри — «Неудачный пуск» и факт
«прокрутка по данным не видна» (attempt_cranked/attempt_facts_ru, решение А
24.10); оценка работы за обрыв связи по счётчику наработки в заглушке
(no_data_hours_text, П10); «Общая авария» при несброшенном коде — «Общая
авария: имя кода (код N)» (panel_fault_name, П6).
"""
from __future__ import annotations

import re
from typing import Any

# Пока исход неизвестен — режим по сигналу панели
_PENDING_HEAD = {1: "Задержка пуска", 2: "Прогрев", 3: "Работа"}
# Прокрутка стартером: обороты выше — коленвал вращали. Умолчание ключа KB
# transition_confirm.crank_rpm_min — для старых записей без поля cranked
CRANK_RPM_MIN = 50.0


def attempt_cranked(attempt: dict[str, Any] | None) -> bool | None:
    """Была ли прокрутка на попытке: поле cranked (с v5.1.23), у старой записи —
    по rpm_max фактов; None — оборотов нет, не знаем.

    v5.1.25 (решение А): поле есть — его значение, в том числе None (внутри
    попытки нет отсчётов оборотов): иначе None проваливался в rpm_max из
    отсчёта до попытки и давал «Отменённый пуск» (ДЭС №3 24.10 13:34, 13:35)."""
    if not isinstance(attempt, dict):
        return None
    if "cranked" in attempt:
        c = attempt["cranked"]
        return c if isinstance(c, bool) else None
    rpm = (attempt.get("facts") or {}).get("rpm_max")
    if isinstance(rpm, (int, float)):
        return float(rpm) > CRANK_RPM_MIN
    return None


def attempt_label(attempt: dict[str, Any] | None) -> str | None:
    """«Неудачный пуск · Стоп/Авария» / «Отменённый пуск · Стоп»; None — не попытка.

    v5.1.23 (решение В4, пересмотр решения 13.10): первое слово — по прокрутке,
    а не по режиму панели. У PCC режим не физичен (0→3 ставится сразу при
    прокрутке), а на любой плашке обороты не выше 250 — «работы» не было.
    Прокрутка выше crank_rpm_min без вспышки, код панели или авария — «Неудачный
    пуск»; ни прокрутки, ни кода, ни аварии — «Отменённый пуск · Стоп».
    Переключатель 40010 и команда 40599 подпись не решают: 40010 падает в
    «Выкл» и на паузе циклической прокрутки. Оборотов нет (None) — «Неудачный
    пуск»: что пуск отменили, не видно. Второе слово — исход (вид стопа).
    """
    if not isinstance(attempt, dict):
        return None
    emergency = attempt.get("outcome") == "EMERGENCY"
    cancelled = (attempt_cranked(attempt) is False and not attempt.get("codes")
                 and not emergency)
    head = "Отменённый пуск" if cancelled else "Неудачный пуск"
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
    "FREQ_REFERENCE": "Задание частоты (регулятор)",
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
    "START_ATTEMPTS_TOTAL": "Попытки пуска (счётчик панели)",
    "START_SUCCESS_TOTAL": "Успешные пуски (счётчик панели)",
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


def attempt_facts_ru(attempt: dict[str, Any] | None, tz=None, codes: bool = False) -> str | None:
    """Факты попытки одной строкой — только наблюдаемое (v5.1.23, решение В4).

    «прокрутка 20 с до 189 об/мин без вспышки, АКБ до 21,5 В, переключатель
    «Выкл» 09:27:43, команда работы — «Работа»»: длительность и обороты
    прокрутки, минимум АКБ, переключатель 40010 и команда 40599 в кадре падения
    (сменились в нём — со временем, «и» при одном моменте), codes=True — коды
    панели попытки. Без выводов «прервал оператор» / «пауза цикла»: 40010
    падает в «Выкл» и на паузе циклической прокрутки. Коды по умолчанию не
    печатаются: у заключения плашки и серии аварий они своей строкой.
    Запись до v5.1.23 (без полей прокрутки и сигналов) — прежний текст.
    """
    if not isinstance(attempt, dict):
        return None
    f = attempt.get("facts") or {}
    rpm = f.get("rpm_max")
    dur = attempt.get("duration_sec")
    if "cranked" not in attempt:
        parts = []
        parts.append(f"обороты до {rpm:.0f} об/мин" if isinstance(rpm, (int, float))
                     else "обороты неизвестны")
        fq = f.get("freq_max")
        parts.append(f"частота до {fq:.1f} Гц" if isinstance(fq, (int, float)) and fq > 0
                     else "частоты нет")
        parts.append("«Готов к нагрузке» был" if f.get("ready") else "«Готов к нагрузке» не выставлен")
        lead = (f"попытка {dur:.0f} с" if isinstance(dur, (int, float)) else "попытка")
        return f"{lead}: " + ", ".join(parts)

    parts: list[str] = []
    cranked = attempt_cranked(attempt)
    sec = attempt.get("crank_sec")
    if cranked:
        parts.append("прокрутка" + (f" {sec:.0f} с" if isinstance(sec, (int, float)) else "")
                     + f" до {rpm:.0f} об/мин без вспышки")
    else:
        lead = f"попытка {dur:.0f} с" if isinstance(dur, (int, float)) else "попытка"
        if cranked is None and attempt.get("rpm_in") == 0:
            # Решение А (24.10): внутри попытки ни одного отсчёта оборотов —
            # ноль до попытки отмены не доказывает
            parts.append(f"{lead}, прокрутка по данным не видна (отсчётов внутри попытки нет)")
        elif not isinstance(rpm, (int, float)):
            parts.append(f"{lead}, обороты неизвестны")
        else:
            parts.append(f"{lead} без прокрутки (обороты "
                         + (f"до {rpm:.0f} об/мин)" if rpm > 0 else "0)"))
    fq = f.get("freq_max")
    if isinstance(fq, (int, float)) and fq > 0:
        parts.append(f"частота до {fmt_number(fq, 1)} Гц")
    if f.get("ready"):
        parts.append("«Готов к нагрузке» был")
    bv = attempt.get("battery_min_v")
    if isinstance(bv, (int, float)):
        parts.append(f"АКБ до {bv:.1f} В".replace(".", ","))
    parts += _attempt_signals_ru(attempt.get("switch"), attempt.get("run_command"), tz)
    if codes:
        names = attempt.get("code_names") or {}
        for c in attempt.get("codes") or []:
            parts.append(f"код {_code_ref(c, names.get(str(c)))}")
    return ", ".join(parts)


def _attempt_signals_ru(sw: Any, rc: Any, tz=None) -> list[str]:
    """Переключатель и команда работы в кадре падения попытки: сменились в нём —
    «переключатель «Выкл» 09:27:43» (оба в один момент — «переключатель «Выкл»
    и команда работы «Стоп» 09:28:02»), держались — «команда работы — «Работа»»."""
    sw = sw if isinstance(sw, dict) else None
    rc = rc if isinstance(rc, dict) else None

    def lab(x: dict) -> str:
        return str(x.get("label") or x.get("value"))

    sw_at = fmt_local(sw.get("at"), tz) if sw and sw.get("at") else None
    rc_at = fmt_local(rc.get("at"), tz) if rc and rc.get("at") else None
    if sw_at and rc_at and sw_at == rc_at:
        return [f"переключатель «{lab(sw)}» и команда работы «{lab(rc)}» {sw_at}"]
    out: list[str] = []
    if sw:
        out.append(f"переключатель «{lab(sw)}» {sw_at}" if sw_at else f"переключатель — «{lab(sw)}»")
    if rc:
        out.append(f"команда работы «{lab(rc)}» {rc_at}" if rc_at else f"команда работы — «{lab(rc)}»")
    return out


def enum_label(meta: dict[str, Any] | None, value: Any, catalog_label: Any = None,
               fallback_value: bool = True) -> str | None:
    """Метка значения enum-регистра (v5.1.23, решение В5): labels_ru из KB
    (mapping.yaml) поверх каталога БД телеметрии; каталог — labels_ru, затем
    labels, затем само значение (так их сливает analytics.source).
    fallback_value=False — без метки KB и каталога None, а не значение (лента
    событий печатает метку как есть, как до v5.1.23)."""
    kb = (meta or {}).get("labels_ru") if isinstance(meta, dict) else None
    if isinstance(kb, dict) and value is not None:
        for k, v in kb.items():
            try:
                if int(k) == int(value):
                    return str(v)
            except (TypeError, ValueError):
                if str(k) == str(value):
                    return str(v)
    if catalog_label is not None and str(catalog_label) != "":
        return str(catalog_label)
    return None if value is None or not fallback_value else str(value)


def panel_fault_name(values: dict[str, Any] | None, code_name=None) -> str | None:
    """Имя неисправности панели (v5.1.23, решение В5): код привязан к биту
    (values.panel_code) — имя кода из справочника KB и «(код N)»: «Разнос
    двигателя — превышение частоты вращения (код 234)». Имя кода — values.
    code_name (детекция с v5.1.23), иначе code_name(код) — справочник по вызову
    (эпизод, открытый до v5.1.23). Кода в справочнике нет — имя бита и «(код N)»
    (у записи v5.1.23 — values.bit_name). Код не привязан или старая запись без
    имени кода — values.fault_name, как раньше. Ключи эпизодов (addr/bit) те же.

    v5.1.25 (П6, разбор ДЭС №3 24.10): «Общая авария» при несброшенном коде
    (кода бита нет, есть values.last_fault_code) — «Общая авария: имя кода (код
    N)»; имя — values.code_name, иначе справочник, иначе «Общая авария (код N)».
    И у старых строк («CommonAlarm (Общая авария)», 14901) — подпись при чтении."""
    if not isinstance(values, dict):
        return None
    base = values.get("fault_name")
    code = values.get("panel_code")
    if code is None:
        try:
            lfc = int(values.get("last_fault_code") or 0)
        except (TypeError, ValueError):
            lfc = 0
        if lfc > 0:
            cname = values.get("code_name")
            if not cname and code_name is not None:
                try:
                    cname = code_name(lfc)
                except Exception:
                    cname = None
            return (f"Общая авария: {cname} (код {lfc})" if cname
                    else f"Общая авария (код {lfc})")
        return base
    cname = values.get("code_name")
    if not cname and code_name is not None:
        try:
            cname = code_name(code)
        except Exception:
            cname = None
    if cname:
        return f"{cname} (код {code})"
    if "bit_name" in values:
        return f"{values.get('bit_name') or base} (код {code})"
    return base


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


# ── Профиль пуска (v5.1.23, п.3, решение В1) ─────────────────────────────────
# Одни тексты на все места: строка «Пуск» отчёта, пометка «на пуске» в сводке,
# строка входа гейта и акта, причина запрета снятия start_unstable

_START_STAGE = {"crank": "на прокрутке", "ramp": "на разгоне", "rated": "после выхода на номинал"}
# Замечания оборотов, к которым относится пуск: пометка «на пуске» в сводке и
# запрет снятия гейтом start_unstable (online.status_assembler)
START_SCENARIOS = frozenset({"RPM_UNDERSPEED", "RPM_OVERSPEED"})


def _hz(v: Any) -> str:
    return fmt_number(v, 1)


def _rpm(v: Any) -> str:
    return fmt_number(v, 0)


def _span_txt(rng: Any, unit: str, fmt) -> str | None:
    if not isinstance(rng, (list, tuple)) or len(rng) != 2 or rng[0] is None:
        return None
    lo, hi = fmt(rng[0]), fmt(rng[1])
    return f"{lo} {unit}" if lo == hi else f"{lo}–{hi} {unit}"


def _flips_txt(n: int) -> str:
    n = int(n)
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} смена знака"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} смены знака"
    return f"{n} смен знака"


def start_fref_text(sp: dict[str, Any] | None) -> str | None:
    """«задание 30,6 → 55 Гц» — до трёх разных значений с начала прокрутки
    (последовательность, с холостым ходом 30), иначе размах значений номинала
    «задание 48,6–50,7 Гц». None — задания в данных нет."""
    f = (sp or {}).get("fref") if isinstance(sp, dict) else None
    if not isinstance(f, dict):
        return None
    seq = [x for x in f.get("seq") or [] if isinstance(x, (int, float))]
    shown: list[str] = []
    for x in seq:
        t = _hz(x)
        if not shown or shown[-1] != t:
            shown.append(t)
    if shown and len(shown) <= 3 and (f.get("seq_n") or len(seq)) <= 3:
        return f"задание {' → '.join(shown)} Гц"
    rng = _span_txt(f.get("range"), "Гц", _hz)
    if rng:
        return f"задание {rng}"
    return f"задание {' → '.join(shown[:3])} Гц" if shown else None


def start_reasons_ru(sp: dict[str, Any] | None) -> list[str]:
    """Почему пуск неустойчив (вариант «б»): «заброс 1696 об/мин (56,8 Гц)»,
    «раскачка 1 мин 50 с, 6 смен знака», «на разгоне задание 55 Гц при 1294
    об/мин», «задание до 42,3 Гц после выхода на номинал»."""
    if not isinstance(sp, dict):
        return []
    rs = sp.get("reasons") or []
    out: list[str] = []
    o = sp.get("overspeed") or {}
    if "overspeed" in rs and isinstance(o, dict) and o.get("rpm") is not None:
        out.append(f"заброс {_rpm(o['rpm'])} об/мин" + (f" ({_hz(o['hz'])} Гц)" if o.get("hz") else ""))
    sw = []
    if "swing" in rs:
        sw.append(f"раскачка {fmt_duration(sp.get('swing_sec'))}")
    if "flips" in rs:
        sw.append(_flips_txt(sp.get("flips") or 0))
    if sw:
        out.append(", ".join(sw))
    f = sp.get("fref") or {}
    hi = f.get("high") if isinstance(f, dict) else None
    if "fref_high" in rs and isinstance(hi, dict):
        txt = f"{_START_STAGE.get(hi.get('stage'), '')} задание {_hz(hi.get('v'))} Гц".strip()
        if hi.get("rpm") is not None and hi.get("stage") != "crank":
            txt += f" при {_rpm(hi['rpm'])} об/мин"
        out.append(txt)
    lo = f.get("low") if isinstance(f, dict) else None
    if "fref_low" in rs and isinstance(lo, dict):
        txt = f"задание до {_hz(lo.get('v'))} Гц после выхода на номинал"
        if lo.get("rpm") is not None:
            txt += f" (обороты {_rpm(lo['rpm'])})"
        out.append(txt)
    return out


def _start_tail(sp: dict[str, Any], tz=None) -> list[str]:
    """Автомат и нагрузка окна пуска: «автомат разомкнут, 0 кВт»."""
    out = []
    cb = sp.get("breaker_closed_at")
    if cb and sp.get("unobserved"):
        # Пуск без связи (ревью v5.1.25, Q5): момент — первый отсчёт, не замыкание
        out.append("автомат уже замкнут")
    else:
        out.append(f"автомат замкнут {fmt_local(cb, tz) or '?'}" if cb else "автомат разомкнут")
    kw = sp.get("kw_max")
    if isinstance(kw, (int, float)):
        out.append(f"до {_rpm(kw)} кВт" if kw >= 1 else "0 кВт")
    return out


def start_profile_text(sp: dict[str, Any] | None, tz=None) -> str | None:
    """Строка профиля пуска без заголовка: «выход на номинал за 9 с; обороты
    1439–1584 об/мин, частота 48,1–52,9 Гц; раскачка 1 мин 50 с, 6 смен знака;
    задание 48,6–50,7 Гц; автомат разомкнут, 0 кВт; масло перед пуском 25 °C — неустойчивый:
    заброс 1584 об/мин (52,9 Гц); раскачка 1 мин 50 с, 6 смен знака».
    У неустойчивого — пометка с причинами (В1, вариант «б»). None — профиля нет."""
    if not isinstance(sp, dict):
        return None
    parts: list[str] = []
    rsec = sp.get("rated_sec")
    # Номинал в кадре смены режима (ревью R14): разгон прошёл между отсчётами —
    # «за 0 с» была бы ложной точностью; оценка сверху, если она есть
    _first = sp.get("rated_first") if "rated_first" in sp else (
        isinstance(rsec, (int, float)) and rsec < 1)
    if sp.get("unobserved"):
        # Пуск прошёл без связи (ревью v5.1.25, Q5): самого пуска не видели
        _t0 = fmt_local(sp.get("crank_at"), tz)
        parts.append("пуск прошёл без связи — " + (
            f"на номинале с первого отсчёта после обрыва ({_t0})" if _first and _t0
            else f"первый отсчёт после обрыва {_t0}" if _t0 else "первый отсчёт после обрыва"))
    elif isinstance(rsec, (int, float)) and _first:
        _bnd = sp.get("rated_bound_sec")
        parts.append("на номинале с первого отсчёта"
                     + (f" (разгон между отсчётами, не дольше {fmt_duration(_bnd)})"
                        if isinstance(_bnd, (int, float)) and _bnd > 0 else ""))
    else:
        parts.append(f"выход на номинал за {fmt_duration(rsec)}" if isinstance(rsec, (int, float))
                     else "до номинала не вышел")
    rr = []
    _r = _span_txt(sp.get("rpm"), "об/мин", _rpm)
    if _r:
        rr.append(f"обороты {_r}")
    _f = _span_txt(sp.get("freq"), "Гц", _hz)
    if _f:
        rr.append(f"частота {_f}")
    if rr:
        parts.append(", ".join(rr))
    sw = []
    if (sp.get("swing_sec") or 0) >= 1:
        sw.append(f"раскачка {fmt_duration(sp.get('swing_sec'))}")
    if sp.get("flips"):
        sw.append(_flips_txt(sp["flips"]))
    if sw:
        parts.append(", ".join(sw))
    fz = start_fref_text(sp)
    # Размах после номинала — отдельно, если он уже размаха с прокрутки
    _fr = sp.get("fref") if isinstance(sp.get("fref"), dict) else {}
    _rr = _span_txt(_fr.get("range_rated"), "Гц", _hz)
    if fz and _rr and "→" not in fz and _rr not in fz:
        fz += f", после номинала {_rr}"
    if fz:
        parts.append(fz)
    parts.append(", ".join(_start_tail(sp, tz)))
    oil = sp.get("oil_temp")
    if isinstance(oil, (int, float)):
        # Значение на начало прокрутки, а не за окно пуска: таблица того же
        # сегмента показывает масло за всю работу
        parts.append(f"масло перед пуском {_rpm(oil)} °C")
    txt = "; ".join(parts)
    if sp.get("open"):
        txt += " (окно пуска ещё идёт)"
    if sp.get("unstable"):
        why = start_reasons_ru(sp)
        txt += " — неустойчивый" + (": " + "; ".join(why) if why else "")
    return txt


def start_note_text(sp: dict[str, Any] | None, tz=None) -> str | None:
    """Пометка к «Просадке» и «Забросу оборотов», начавшимся в окне пуска (В1):
    «на пуске: раскачка 1 мин 50 с, 6 смен знака, задание 48,6–50,7 Гц,
    автомат разомкнут, 0 кВт», у неустойчивого — «, пуск неустойчивый»."""
    if not isinstance(sp, dict):
        return None
    bits: list[str] = []
    if (sp.get("swing_sec") or 0) >= 1:
        bits.append(f"раскачка {fmt_duration(sp.get('swing_sec'))}")
    if sp.get("flips"):
        bits.append(_flips_txt(sp["flips"]))
    fz = start_fref_text(sp)
    if fz:
        bits.append(fz)
    bits += _start_tail(sp, tz)
    if sp.get("unstable"):
        bits.append("пуск неустойчивый")
    return "на пуске: " + ", ".join(bits)


def start_window(sp: dict[str, Any] | None) -> tuple[Any, Any] | None:
    """Окно пуска [начало прокрутки, конец окна) профиля — ISO-строки; None —
    профиля нет. Окно ещё идёт (open) — конец None: растёт с каждым циклом."""
    if not isinstance(sp, dict) or not sp.get("crank_at"):
        return None
    return sp.get("crank_at"), (None if sp.get("open") else sp.get("t_to"))


def no_data_hours_text(est: dict[str, Any] | None, tz=None) -> str | None:
    """Оценка работы за обрыв связи по счётчику наработки — строкой заглушки
    (v5.1.25, П10; serializer.no_data_run_estimate). «Наработка за перерыв по
    счётчику — 6 ч 18 мин: двигатель работал до ~12:05–12:08 (счётчик идёт
    шагом 3 мин).» Наработка — от тика счётчика, а не от последних данных
    (критика SHOULD 5); «до ~» — только при непрерывной работе (счётчики пусков
    не менялись). hours_only (ревью v5.1.25, Q1) — только наработка: на
    обрыве стоял или счётчика пусков нет. None — оценки нет."""
    if not isinstance(est, dict) or not est.get("kind"):
        return None
    kind = est["kind"]
    worked = fmt_duration(est.get("worked_sec") or 0, seconds=False)
    if kind == "idle":
        return "По счётчику наработки за перерыв двигатель не работал."
    if kind == "starts":
        return (f"Наработка за перерыв по счётчику — {worked}; счётчики пусков сменились — были "
                f"остановы и пуски, момент останова не оценить.")
    if kind == "whole":
        return f"Наработка за перерыв по счётчику — {worked}: двигатель работал весь перерыв."
    if kind == "hours_only":
        # Непрерывность работы с последних данных не подтверждена (ревью v5.1.25, Q1)
        why = ("на последних данных до обрыва двигатель не работал — когда он работал, "
               if est.get("reason") == "stood" else
               "счётчика пусков у машины нет — были ли остановы и когда двигатель остановился, ")
        return f"Наработка за перерыв по счётчику — {worked}; {why}по счётчику не оценить."
    a = fmt_local(est.get("end_from"), tz, seconds=False)
    b = fmt_local(est.get("end_to"), tz, seconds=False)
    if not a or not b:
        return f"Наработка за перерыв по счётчику — {worked}."
    step = fmt_duration(est.get("step_sec") or 0, seconds=False)
    if kind == "before":
        d = fmt_local(est.get("end_from"), tz, day=True, seconds=False)
        return f"В этот период не работал (по счётчику наработки остановился ~{d}–{b})."
    return (f"Наработка за перерыв по счётчику — {worked}: двигатель работал до ~{a}–{b} "
            f"(счётчик идёт шагом {step}).")


def engine_hours_text(hours: Any) -> str | None:
    """«13 643 ч» — наработка по счётчику панели; None — значения нет."""
    try:
        h = float(hours)
    except (TypeError, ValueError):
        return None
    if h < 0:
        return None
    return f"{round(h):,}".replace(",", " ") + " ч"
