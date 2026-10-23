# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль онлайн-мониторинга
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Разбор аварийного останова: заказывается АКТОМ, а не таймером.

Гейт предупреждений ждёт 60 секунд стабилизации состава тревог, и для аварии
это не работало: у трёх аварий из десяти в истории маску снимали раньше, состав
становился «норма», отсчёт выбрасывался — разбора не было вовсе.

Здесь повод другой. Акт строится у каждой аварии по построению, на границе
`min(останов + лаг, закрытие сегмента)`. Когда он готов — готов и материал для
разбора, причём материал куда богаче снимка состояния: цикл от последнего
штатного останова, срез «висело на момент останова», свод по видам и лента
через лаг стабилизации.

Результат кладётся в ту же `warning_analyses`, что и разборы гейта: граница
«факты отдельно, мнение отдельно» сохраняется — акт остаётся детерминированным
артефактом, интерпретация живёт рядом. В карточке они показываются одним
блоком, но в базе не смешиваются.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from analytics.labels import attempt_facts_ru, attempt_label, label_from_chars

logger = logging.getLogger(__name__)

_SEVERITY_RU = {
    "shutdown": "АВАРИЯ", "shutdown_cooldown": "АВАРИЯ",
    "derate": "снижение номинала", "warning": "предупреждение",
}


def _ts(raw: Any, tz=None, ref: Any = None) -> str:
    """Метка входа в местном поясе (v5.1.22, P1-6): «09:26:23»; другие сутки,
    чем у ref (момент останова), — «15.10 21:41:02». Было — срез ISO в UTC без
    пометки: совет писал «06:26:23» и «00:45:32 → 06:25:52» при МСК в отчёте."""
    if not raw:
        return "—"
    from analytics.labels import fmt_local
    t = fmt_local(raw, tz)
    if t is None:
        s = str(raw)
        return s[11:19] if len(s) > 19 else s
    if ref is not None:
        d1, d2 = fmt_local(raw, tz, day=True), fmt_local(ref, tz, day=True)
        if d1 and d2 and d1[:5] != d2[:5]:
            return d1
    return t


def _day(raw: Any, tz=None) -> str:
    """«16.10 09:26:23» в местном поясе; битая метка — как есть."""
    from analytics.labels import fmt_local
    return fmt_local(raw, tz, day=True) or (str(raw) if raw else "—")


def _age(sec: float) -> str:
    sec = int(sec or 0)
    if sec >= 86400:
        return f"{sec // 86400} сут"
    if sec >= 3600:
        return f"{sec // 3600} ч"
    if sec >= 60:
        return f"{sec // 60} мин"
    return f"{sec} с"


# Не параметры, а коды: последняя неисправность и её тип идут в ленту
# Счётчик наработки — монотонный, не параметр режима (в акте «мин–макс» бессмыслен).
# v5.1.22: пульс опроса и номиналы установки — не параметры режима, только шум
_NOT_PARAMS = {"LAST_FAULT_CODE", "LAST_FAULT_TYPE", "ENGINE_HOURS",
               "HEARTBEAT", "RATING_KW", "RATING_KVA"}
# Строка хода цикла: ключевые параметры сегмента (v5.1.22, P1-4) — нагрузка,
# ток, обороты, частота, масло, ОЖ, АКБ; прочее — в подробных блоках
_CYCLE_KEYS = (("ACTIVE_POWER_TOTAL", "P"), ("CURRENT_AVG", "I"), ("RPM", "обороты"),
               ("FREQUENCY", "частота"), ("OIL_PRESS", "Pм"), ("OIL_TEMP", "Tм"),
               ("COOLANT_TEMP", "ОЖ"), ("BATTERY_V", "АКБ"))
# Не больше строк хода цикла; более ранние сворачиваются в одну
_CYCLE_MAX_LINES = 30


def _chars_of(seg: dict[str, Any] | list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Расчётные характеристики сегмента по ролям, сведённые по подсегментам.

    Мин и макс — по всему сегменту, «на конце» — последнее значение
    последнего подсегмента, где роль есть. Список — части одного режима через
    суточный рез, сведённые вместе (v5.1.22).
    """
    import json as _json
    segs = seg if isinstance(seg, list) else [seg]
    out: dict[str, dict[str, Any]] = {}
    for sg in segs:
        raw = sg.get("characteristics_json")
        if isinstance(raw, str):
            try:
                raw = _json.loads(raw)
            except Exception:
                continue
        for sub in (raw or {}).get("subsegments") or []:
            for role, ch in ((sub or {}).get("characteristics") or {}).items():
                if not isinstance(ch, dict) or role in _NOT_PARAMS:
                    continue
                acc = out.setdefault(role, {"min": None, "max": None, "end": None,
                                            "unit": ch.get("unit") or ""})
                for k, pick in (("min", min), ("max", max)):
                    v = ch.get(k)
                    if v is not None:
                        acc[k] = v if acc[k] is None else pick(acc[k], v)
                if ch.get("value_end") is not None:
                    acc["end"] = ch["value_end"]
    return out


def _fmt_num(v: Any) -> str:
    """Число входа совета — как в ленте отсчётов: «56,8», «1696», «0,85»."""
    from analytics.labels import fmt_number
    if v is None:
        return "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return fmt_number(f, 0 if abs(f) >= 100 else None)


def _cycle_dt(x: Any) -> datetime | None:
    if isinstance(x, str):
        try:
            x = datetime.fromisoformat(x)
        except ValueError:
            return None
    if not isinstance(x, datetime):
        return None
    return x if x.tzinfo else x.replace(tzinfo=timezone.utc)


def _key_params(chars: dict[str, dict[str, Any]]) -> str:
    """«P 0…713 кВт; I 0…1324 А; обороты 1496…1504 об/мин; …» — ключевые роли."""
    from analytics.labels import unit_ru
    bits = []
    for role, name in _CYCLE_KEYS:
        c = chars.get(role)
        if not c or (c.get("min") is None and c.get("max") is None):
            continue
        lo, hi = _fmt_num(c.get("min")), _fmt_num(c.get("max"))
        rng = lo if lo == hi or c.get("max") is None else (hi if c.get("min") is None else f"{lo}…{hi}")
        bits.append(f"{name} {rng} {unit_ru(c.get('unit'))}".rstrip())
    return "; ".join(bits)


def _format_cycle_params(
    segments: list[dict[str, Any]], stop_ts: Any,
    attempt: dict[str, Any] | None = None,
    anchor: Any = None, trace: dict[str, Any] | None = None, tz=None,
) -> list[str]:
    """Цикл от последнего нормального останова: ход по сегментам и параметры
    (v5.1.22, P1-4).

    Параметры не читаются заново — это расчётные характеристики, которые
    аналитика посчитала при закрытии каждого сегмента. До v5.1.22 они сводились
    мин/макс ПО РЕЖИМАМ за всё окно: в «Работу» #35931 попали 14 ч под
    нагрузкой до штатного останова вместе с пусками без нагрузки — «0 / 713 кВт,
    41,5 / 56,8 Гц», совет писал «под нагрузкой» при разомкнутом автомате, а
    параметры #35929 у #35941 «отсутствовали». Теперь у каждого сегмента своя
    строка с ключевыми параметрами (части одного режима через суточный рез — одной
    строкой «N части»), после якоря — разделитель «последний штатный останов;
    дальше — возможные предвестники», не больше 30 строк (ранние свёрнуты).
    Подробно (мин / макс / на конце) — сегмент, из которого машина
    остановилась, сразу за ним лента отсчётов (trace), и последняя работа до
    штатного останова — фоном. Время — местное (P1-6).
    """
    from analytics.labels import fmt_duration
    from analytics.serializer import RUN_STATE_RU

    stop = _cycle_dt(stop_ts)
    if stop is None:
        return []
    anc = _cycle_dt(anchor)

    def _t(x: Any) -> str:
        return _ts(x, tz, stop) if x else "—"

    def _span(a: Any, b: Any) -> str:
        """«15.10 13:28:53 — 03:41:03 (14 ч 12 мин)»: конец — с датой, если сутки
        другие, чем у начала."""
        ta, tb = _cycle_dt(a), _cycle_dt(b)
        end = _ts(b, tz, a) if b else "…"
        dur = f" ({fmt_duration((tb - ta).total_seconds())})" if ta and tb else ""
        return f"{_t(a)} — {end}{dur}"

    def _fall(sg: dict[str, Any]) -> datetime | None:
        # У плашки неудачной попытки момент останова — падение, не её начало:
        # своя плашка этого акта в цикл не входит, прошлые — входят (v5.1.16)
        att = (_chars_json(sg) or {}).get("attempt")
        return _cycle_dt((att or {}).get("t_fall") if isinstance(att, dict) else None) \
            or _cycle_dt(sg.get("t_start"))

    before = [sg for sg in segments if (_fall(sg) or stop) < stop]
    if not before and not attempt:
        return []

    def _mode(sg: dict[str, Any]) -> str:
        rs = sg.get("run_state")
        name = RUN_STATE_RU.get(rs, f"RUN_STATE={rs}")
        raw = _chars_json(sg)
        # Плашка неудачной попытки (v5.1.16): «Неудачная работа · Авария»
        lab = label_from_chars(raw) if isinstance(raw, dict) else None
        if lab:
            return lab
        if rs == 0 and isinstance(raw, dict) and raw.get("stop_kind") == "EMERGENCY":
            name += " (аварийный)"
        return name

    def _with_params(sg: dict[str, Any]) -> bool:
        # Стоянка — без параметров (нули и остывание); плашка попытки — с ними
        return sg.get("run_state") != 0 or bool(label_from_chars(_chars_json(sg) or {}))

    # Части одного режима через суточный рез — одна строка
    groups: list[list[dict[str, Any]]] = []
    for sg in before:
        if groups:
            prev = groups[-1][-1]
            pc = _chars_json(prev) or {}
            if (pc.get("cause_close") == "DAILY_BOUNDARY" and _mode(prev) == _mode(sg)
                    and _cycle_dt(prev.get("t_end")) == _cycle_dt(sg.get("t_start"))):
                groups[-1].append(sg)
                continue
        groups.append([sg])

    def _line(g: list[dict[str, Any]]) -> str:
        txt = f"  - {_mode(g[0])}: {_span(g[0].get('t_start'), g[-1].get('t_end'))}"
        if len(g) > 1:
            txt += f", {len(g)} части через суточный рез"
        if _with_params(g[0]):
            kp = _key_params(_chars_of(g))
            if kp:
                txt += f": {kp}"
        return txt

    lines: list[tuple[datetime | None, str]] = [(_cycle_dt(g[0].get("t_start")), _line(g))
                                                for g in groups]
    folded = ""
    if len(lines) > _CYCLE_MAX_LINES:
        k = len(lines) - _CYCLE_MAX_LINES + 1
        cnt: dict[str, int] = {}
        for g in groups[:k]:
            cnt[_mode(g[0])] = cnt.get(_mode(g[0]), 0) + 1
        folded = (f"  - … ранее {k} строк: {_t(groups[0][0].get('t_start'))} — "
                  f"{_t(groups[k - 1][-1].get('t_end'))} ("
                  + ", ".join(f"{m} ×{n}" for m, n in cnt.items()) + ")")
        lines = lines[k:]

    out: list[str] = ["", f"Ход цикла (сегменты аналитики, время {_tz_label(tz)}):"]
    if folded:
        out.append(folded)
    sep = (f"  ── {_t(anc)} последний штатный останов; дальше — возможные предвестники ──"
           if anc is not None else None)
    for t0, txt in lines:
        if sep and t0 is not None and t0 >= anc:
            out.append(sep)
            sep = None
        out.append(txt)
    if attempt:
        if sep:
            out.append(sep)
            sep = None
        out.append(f"  - {attempt_label(attempt)} (эта попытка): "
                   f"{_span(attempt.get('t_start'), attempt.get('t_fall'))}")

    def _detail(g: list[dict[str, Any]], title: str) -> None:
        chars = _chars_of(g)
        if not chars:
            return
        out.extend(["", f"{title} — {_mode(g[0])}, "
                        f"{_span(g[0].get('t_start'), g[-1].get('t_end'))} "
                        f"(мин / макс / на конце):"])
        for role in sorted(chars):
            c = chars[role]
            out.append(f"  {role}: {_fmt_num(c['min'])} / {_fmt_num(c['max'])} / "
                       f"{_fmt_num(c['end'])} {c['unit']}".rstrip())

    # Откуда упала: последний не-стоповый сегмент. У 1452 это пуск или
    # прогрев — до работы машина не дошла; у штатной нагрузки — работа или
    # разгрузка. Отказ пуска: машина остановилась с этой попытки — её факты, а
    # не прошлая плашка или работа до штатного останова (ревью v5.1.16)
    if attempt:
        out += ["", f"Сегмент, из которого машина остановилась — {attempt_label(attempt)}, "
                    f"{_span(attempt.get('t_start'), attempt.get('t_fall'))}: "
                    f"{attempt_facts_ru(attempt, tz) or ''}".rstrip(": ")]
        fell = None
    else:
        fell = next((g for g in reversed(groups) if g[0].get("run_state") != 0), None)
    if fell is not None:
        _detail(fell, "Сегмент, из которого машина остановилась")
        # Профиль пуска сегмента падения (v5.1.23, п.3): #35939 — «на разгоне
        # задание 55 Гц при 1294 об/мин» за секунды до разноса 234
        try:
            from analytics.labels import start_profile_text
            _sp = (_chars_json(fell[0]) or {}).get("start_profile")
            _st = start_profile_text(_sp, tz) if isinstance(_sp, dict) else None
            if _st:
                out += ["", f"Пуск {_t(_sp.get('crank_at'))}: {_st}"]
        except Exception:
            logger.warning("IncidentGate: строка профиля пуска не напечатана", exc_info=True)
    # Лента отсчётов — сразу за сегментом падения (v5.1.22, P1-4)
    try:
        from analytics.reconstructor import format_analog_trace_ru
        tl = format_analog_trace_ru(trace, tz)
    except Exception:
        logger.warning("IncidentGate: лента отсчётов не напечатана", exc_info=True)
        tl = []
    if tl:
        out += [""] + tl
    # Фон: последняя работа до штатного останова — то, на чём машина ходила до
    # него (нагрузка 14 ч — не условия падения). Якоря нет — последняя работа
    # цикла, если упала не из неё
    work = next((g for g in reversed(groups) if g[0].get("run_state") == 3
                 and (anc is None or (_cycle_dt(g[-1].get("t_end")) or stop) <= anc)), None)
    if work is not None and work is not fell:
        _detail(work, "Последняя работа до штатного останова (фон)" if anc is not None
                else "Последний рабочий сегмент цикла")
    return out


def _tz_label(tz=None) -> str:
    from analytics.labels import tz_label
    return tz_label(tz)


def _chars_json(sg: dict[str, Any]) -> dict[str, Any] | None:
    raw = sg.get("characteristics_json")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    return raw if isinstance(raw, dict) else None


def _dt_any(x: Any) -> datetime | None:
    if isinstance(x, str):
        try:
            x = datetime.fromisoformat(x)
        except ValueError:
            return None
    if not isinstance(x, datetime):
        return None
    return x if x.tzinfo else x.replace(tzinfo=timezone.utc)


def series_facts(
    cycle_segments: list[dict[str, Any]], stop_ts: Any, anchor: Any,
    code_name=None, tz=None,
) -> list[str]:
    """Предыдущие аварии серии — только факты, без выводов прошлых разборов
    (решение владельца 13.10.2026).

    Серия — аварийные стопы после последнего нормального останова и до этого.
    По каждому: когда, код панели, из какого состояния, сколько стояла до
    сброса. Из характеристик сегментов, уже посчитанных при закрытии.
    Время — местное, с датой (v5.1.22, P1-6).
    """
    stop = _dt_any(stop_ts)
    anc = _dt_any(anchor)
    if stop is None:
        return []
    segs = list(cycle_segments or [])
    chars = [_chars_json(sg) or {} for sg in segs]

    def _is_cont(k: int) -> bool:
        """Продолжение аварийного стопа после суточного реза — не новая авария."""
        if k == 0:
            return False
        pc = chars[k - 1]
        return (segs[k - 1].get("run_state") == 0 and pc.get("stop_kind") == "EMERGENCY"
                and pc.get("cause_close") == "DAILY_BOUNDARY"
                and _dt_any(segs[k - 1].get("t_end")) == _dt_any(segs[k].get("t_start")))

    out: list[str] = []
    for k, sg in enumerate(segs):
        ch = chars[k]
        if sg.get("run_state") != 0 or ch.get("stop_kind") != "EMERGENCY" or _is_cont(k):
            continue
        att = ch.get("attempt") if isinstance(ch.get("attempt"), dict) else None
        fall = _dt_any((att or {}).get("t_fall") or sg.get("t_start"))
        if fall is None or fall >= stop or (anc is not None and fall < anc):
            continue
        # Конец аварии — через продолжения после суточных резов
        m = k
        while m + 1 < len(segs) and _is_cont(m + 1):
            m += 1
        if att is not None:
            # Коды самой попытки: плашка тянется весь стоп, и её агрегат
            # подхватил бы и код, висевший до попытки, и кнопку через час
            codes = [int(c) for c in att.get("codes") or []]
        else:
            codes = []
            for x in range(k, m + 1):
                for sub in chars[x].get("subsegments") or []:
                    lfc = ((sub or {}).get("characteristics") or {}).get("LAST_FAULT_CODE") or {}
                    for key in ("value_start", "max", "value_end"):
                        try:
                            c = int(lfc.get(key) or 0)
                        except (TypeError, ValueError):
                            c = 0
                        if c > 0 and c not in codes:
                            codes.append(c)
        code_txt = "; ".join(
            f"{c} «{code_name(c)}»" if code_name and code_name(c) else str(c) for c in codes
        ) or "код не выставлен"
        what = label_from_chars(ch)
        if not what:
            # Из какого режима упала — последний нестоповый перед ней
            prev = next((segs[x] for x in range(k - 1, -1, -1)
                         if segs[x].get("run_state") not in (0, None)), None)
            prs = prev.get("run_state") if prev else None
            if prs in (3, 4, 5, 6):
                what = "аварийный стоп (падение с работы)"
            elif prs in (1, 2):
                from analytics.serializer import RUN_STATE_RU
                what = f"аварийный стоп из режима «{RUN_STATE_RU.get(prs, prs)}»"
            else:
                what = "аварийный стоп"
        t_end = _dt_any(segs[m].get("t_end"))
        if chars[m].get("cause_close") == "SHUTDOWN_CLEARED" and t_end is not None:
            reset = f"сброшена через {_age((t_end - fall).total_seconds())}"
        else:
            reset = "до конца сегмента не сброшена"
        line = f"  - {_day(fall, tz)} — {what}; код: {code_txt}; {reset}"
        facts = attempt_facts_ru(att, tz)
        if facts:
            line += f"; {facts}"
        out.append(line)
    return out


def _fault_codes_line(incident: dict[str, Any]) -> str | None:
    """Числовые коды неисправности панели в акте (40012) с названием (v5.1.14):
    по марке и коду поиск шлюза находит руководство — по одному названию нет."""
    seen: dict[int, str] = {}
    for e in incident.get("chronology") or []:
        if e.get("role") != "LAST_FAULT_CODE" or e.get("value") in (None, 0):
            continue
        try:
            code = int(e["value"])
        except (TypeError, ValueError):
            continue
        if code > 0 and code not in seen:
            seen[code] = e.get("label") or ""
    if not seen:
        return None
    return "Коды неисправностей панели в акте (40012): " + "; ".join(
        f"{c} — {n}" if n else str(c) for c, n in seen.items())


def build_incident_prompt(
    incident: dict[str, Any], seg_row: dict[str, Any] | None = None,
    cycle_segments: list[dict[str, Any]] | None = None,
    context: dict[str, Any] | None = None,
    tz=None,
) -> str:
    """Промпт разбора аварии из акта.

    Порядок продуман под чтение моделью: сперва что за машина и что она уже
    везла на себе к моменту падения, потом сам останов, потом лента. Вопрос в
    конце — иначе модель отвечает на первое, что увидела. context — оборудование
    из реестра и состояние на момент останова (_context, v5.1.14).

    v5.1.22: время во входе — местное (МСК, P1-6; было — UTC без пометки, и
    совет писал «06:26:23» при «09:26:23» в отчёте); ход цикла по сегментам
    вместо склейки по режимам и лента отсчётов сегмента падения (P1-4).
    tz — пояс подписей (None — пояс сервиса, config.get_tz).
    """
    from analytics.labels import tz_label
    tzl = tz_label(tz)
    ch = incident.get("character") or {}
    win = incident.get("window") or {}
    eq = (context or {}).get("equipment") or {}
    att = incident.get("attempt") if isinstance(incident.get("attempt"), dict) else None
    stop_raw = incident.get("stop_ts")
    out: list[str] = [
        "Разбери отказ пуска дизель-генераторной установки: выход из стопа не "
        "подтвердился — машина не вышла на работу — и закончился аварийным "
        "остановом панели." if att else
        "Разбери аварийный останов дизель-генераторной установки.",
        f"Время во входе — {tzl}."]
    if eq:
        out += ["", "Оборудование (реестр):"]
        if eq.get("name"):
            out.append(f"  - Установка: {eq['name']}")
        if eq.get("engine"):
            out.append(f"  - Двигатель: {eq['engine']}"
                       + (f", s/n {eq['engine_sn']}" if eq.get("engine_sn") else ""))
        if eq.get("controller"):
            out.append(f"  - Контроллер (панель управления): {eq['controller']}")
    out += ["", f"Останов: {_day(stop_raw, tz) if stop_raw else '—'}"]
    if att:
        # Гейт характера писан для падения с работы: у отказа пуска «без
        # охлаждения» и «прямой аварийный» — не признаки, а свойство попытки
        out.append(f"Попытка: {attempt_label(att)}, с {_ts(att.get('t_start'), tz, stop_raw)}; "
                   f"{attempt_facts_ru(att, tz) or ''}".rstrip("; "))
    else:
        out.append(f"Характер по детерминированному гейту: {ch.get('character', '?')} "
                   f"(уверенность {ch.get('confidence', '?')})")
        votes = (ch.get("immediate_votes") or []) + (ch.get("controlled_votes") or [])
        if votes:
            out.append("Сигналы: " + "; ".join(votes))
    if win.get("baseline") == "fallback":
        out.append("Предшествующего штатного останова в истории нет — "
                   "окно взято по последним сегментам.")
    elif win.get("from"):
        anchor = win.get("anchor") or win["from"]
        out.append(f"Последний штатный останов: {_day(anchor, tz)}")
        if win["from"] != anchor:
            out.append(f"Окно данных: с {_day(win['from'], tz)}"
                       + (f" (не менее {win['min_window_h']:g} ч)" if win.get("min_window_h") else ""))
        out.append("До штатного останова — фон (машина остановилась штатно), "
                   "после — возможные предвестники.")
    codes = _fault_codes_line(incident)
    if codes:
        out.append(codes)

    states = (context or {}).get("states") or []
    if states:
        out += ["", "Состояние на момент останова (не обязательно менялось в окне — "
                    "в ленту попадают только смены):"]
        for s in states:
            since = ""
            if s.get("since"):
                # Давнее состояние — с датой: «с 29.09 13:10:22», сегодняшнее — время
                since = f" (с {_ts(s['since'], tz, stop_raw)})"
            out.append(f"  - {s['addr']} {s['name']} = {s['label']}{since}")

    series = series_facts(cycle_segments or [], stop_raw,
                          win.get("anchor") or (win.get("from") if win.get("baseline") != "fallback"
                                                else None),
                          (context or {}).get("code_name"), tz=tz)
    if series:
        out += ["", "Предыдущие аварии этой серии — после штатного останова (только "
                    "факты, без выводов прошлых разборов):"] + series

    standing = incident.get("standing") or []
    if standing:
        out += ["", "Висело на момент останова (маска — с какого времени):"]
        for f in standing:
            sev = _SEVERITY_RU.get(f.get("severity") or "", f.get("severity") or "событие")
            out.append(f"  - {f.get('name')} [{sev}] — с {_day(f.get('since'), tz)} "
                       f"({_age(f.get('age_sec') or 0)})")

    summary = incident.get("summary") or []
    if summary:
        out += ["", f"Что происходило за период (событий всего "
                    f"{incident.get('events_total', '?')}):"]
        # Все виды, без обрезки: в шумных событиях часто кроются действия
        # персонала, а число видов ограничено каталогом регистров, не временем
        for g in summary:
            # Метка — по тяжести бита из каталога: в битовых регистрах есть и
            # аварии, и предупреждения, и события без тяжести
            if g.get("kind") != "fault":
                mark = "состояние"
            elif g.get("severity"):
                mark = "фронт: " + _SEVERITY_RU.get(g["severity"], g["severity"])
            else:
                mark = "событие"
            span = (f"{_ts(g.get('first'), tz, stop_raw)}—{_ts(g.get('last'), tz, stop_raw)}"
                    if (g.get("count") or 1) > 1 else _ts(g.get("first"), tz, stop_raw))
            out.append(f"  - {g.get('name')} [{mark}] × {g.get('count')} ({span})")

    chrono = incident.get("chronology") or []
    if chrono:
        out += ["", f"Лента вокруг останова ({tzl}):", "```"]
        for e in chrono:
            kind = "СОБЫТИЕ  " if e.get("kind") == "fault" else "состояние"
            bit = e.get("bit")
            addr = f"{e.get('addr')}/{bit}" if bit is not None else str(e.get("addr"))
            val = (f" = {e.get('label')}"
                   if e.get("kind") == "state" and e.get("label") else "")
            sev = f"  [{e.get('severity')}]" if e.get("severity") else ""
            out.append(f"{_ts(e.get('ts'), tz, stop_raw)}  {kind} {addr:9} "
                       f"{e.get('name') or ''}{val}{sev}")
        out.append("```")

    out += _format_cycle_params(
        cycle_segments or [], stop_raw, att,
        anchor=win.get("anchor") if win.get("baseline") != "fallback" else None,
        trace=incident.get("trace") if isinstance(incident.get("trace"), dict) else None,
        tz=tz)

    if seg_row and seg_row.get("report_md"):
        # Разбор заказывается в момент останова, ответ — через час: блок о коде в
        # отчёте — снимок на тот момент, а не «сейчас» (прогон совета 16.10, #35941:
        # «код 234 не сброшен — не сбрасывать», а сброшен он был через 29 с)
        _rep = (str(seg_row["report_md"])
                .replace("**⚠ Несброшенная неисправность:**",
                         "**⚠ Код неисправности на момент останова:**")
                .replace("Код не сброшен кнопкой сброса на панели — неисправность считается "
                         "не устранённой.",
                         "На момент останова код не сброшен; сброс мог произойти позже."))
        out += ["", "Отчёт аналитики по сегменту (снимок на момент останова; состояние "
                "кода — на этот момент, сброс мог произойти позже):", "",
                _rep[:8000]]

    out += [
        "",
        ("Задача: назови наиболее вероятную причину отказа пуска — почему машина "
         "не вышла на работу — и на чём основан вывод. " if att else
         "Задача: назови наиболее вероятную причину останова и на чём основан "
         "вывод. ")
        + "Отдельно укажи, что из висевшего к моменту падения могло к "
        "нему привести, а что к делу не относится. Если данных для вывода "
        "не хватает — скажи прямо, какого сигнала недостаёт. Не предлагай "
        "регламентных работ, которых не следует из наблюдаемого. "
        f"Время в ответе указывай по {tzl}, как во входе.",
    ]
    return "\n".join(out)


async def _ask_claude(user_prompt: str, model: str) -> str:
    """Разбор через Claude API. Без инструмента verdict: у аварии нечего
    отменять, вердикт «авария или нет» уже вынесен детерминированно."""
    import anthropic
    import httpx

    from config import settings as app_settings
    from corpus.settings import get_claude_settings
    from llm.router import get_prompt

    cfg = get_claude_settings()
    http_client = None
    try:
        if cfg.get("proxy"):
            http_client = httpx.AsyncClient(proxy=cfg["proxy"])
        client = anthropic.AsyncAnthropic(
            api_key=app_settings.anthropic_api_key,
            http_client=http_client,
            max_retries=3,
        )
        response = await client.messages.create(
            model=model or cfg["model"],
            max_tokens=cfg["max_tokens"],
            system=get_prompt("incident"),
            messages=[{"role": "user", "content": user_prompt}],
        )
        if response.stop_reason == "max_tokens":
            logger.warning("IncidentGate: разбор обрезан по max_tokens=%s",
                           cfg["max_tokens"])
        return "".join(b.text for b in response.content if hasattr(b, "text"))
    finally:
        if http_client:
            await http_client.aclose()


# ── Жизненный цикл разбора (v5.1.11) ───────────────────────────────────────────
#
# Разбор идёт на совете шлюза (smart + xhigh): десятки минут, в очереди batch —
# дольше. Диспетчер к этому времени уже получил предварительные выводы из
# заключения по сегменту, разбор аварии — дополнение (решение владельца 12.10),
# поэтому ждём долго, но учитываем: дубль заказа отбрасывается, на каждую
# попытку — отметка в журнале гейта, в конце — терминальная отметка, после
# рестарта недоразобранное заказывается снова.

# Срок ожидания ответа — сутки; нет ответа → перезаказ один раз → «не получен»
# с причиной (решение 10.10, подтверждено 12.10)
INCIDENT_DEADLINE_SEC = 24 * 3600
INCIDENT_ATTEMPTS = 2
INCIDENT_RETRY_AFTER_SEC = 15 * 60
# Совет на шлюзе без соединения живёт дальше (отмены задачи шлюза в версии нет):
# заказов на одну аварию за всю её жизнь — не больше трёх, включая рестарты
INCIDENT_MAX_ORDERS = 3
# Текст получен, а сегмента нет (СТОП/ПУСК с перечиткой удалил строку): держим
# текст и пробуем записать раз в минуту до 15 мин — второй совет не заказываем
INCIDENT_SAVE_RETRY_SEC = 60
INCIDENT_SAVE_WINDOW_SEC = 15 * 60
# Отметка в журнал: окно цикла закрытия (открытая строка удалена, закрытая ещё
# не вставлена) — несколько секунд
_MARK_RETRIES = 3
_MARK_RETRY_SEC = 5.0
# Восстановление после рестарта: аварии не старше 48 ч, не больше 3 заказов за старт
RECOVER_HOURS = 48
RECOVER_MAX = 3
RECOVER_DELAY_SEC = 120

# Итоги отметок журнала гейта (kind="incident")
INC_ORDERED, INC_FAILED, INC_REMOVED, INC_SAVE_FAILED = (
    "ordered", "failed", "removed", "save_failed")
INC_TERMINAL = (INC_FAILED, INC_REMOVED, INC_SAVE_FAILED)

_inflight: dict[tuple[str, str, int, str], asyncio.Task] = {}
_closing = False
_recover_task: asyncio.Task | None = None


class EmptyIncidentAnswer(Exception):
    """Модель вернула пустой текст — сбой, а не разбор."""


def _utc(ts: datetime) -> datetime:
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts.astimezone(timezone.utc)


def incident_hash(stop_ts: datetime) -> str:
    """Ключ разбора аварии в warning_analyses и журнале гейта."""
    return f"incident:{_utc(stop_ts).isoformat()}"


def order_incident_analysis(
    router_sn: str, equip_type: str, panel_id: int,
    incident: dict[str, Any], stop_ts: datetime,
    segment_id: int | None = None,
) -> bool:
    """Заказать разбор аварии отдельной учтённой задачей. False — не заказан.

    Цикл движка разбор не ждёт. Повторный заказ того же останова, пока разбор
    в работе, отбрасывается: учёт модульный, поэтому и перезапуск движка
    (СТОП/ПУСК с перечиткой) второй совет не закажет.
    """
    if _closing:
        logger.info("IncidentGate: сервис останавливается — разбор аварии %s/%s/%s на %s "
                    "не заказан (закажет восстановление после старта)",
                    router_sn, equip_type, panel_id, _utc(stop_ts).isoformat())
        return False
    key = (router_sn, equip_type, int(panel_id), incident_hash(stop_ts))
    cur = _inflight.get(key)
    if cur is not None and not cur.done():
        logger.info("IncidentGate: разбор аварии %s/%s/%s на %s уже в работе — дубль отброшен",
                    router_sn, equip_type, panel_id, _utc(stop_ts).isoformat())
        return False
    # Без метки движка (v5.1.24): разбор — часы ожидания совета; его чтения
    # source не должны продлевать пульс движка, а сам он — шуметь в стеке сторожа
    from db.remote import detached_context
    task = asyncio.create_task(
        _run_incident(router_sn, equip_type, int(panel_id), incident, _utc(stop_ts), segment_id),
        name=f"incident:{router_sn}/{equip_type}/{panel_id}",
        context=detached_context(),
    )
    _inflight[key] = task
    task.add_done_callback(lambda t, k=key: _task_done(k, t))
    return True


def _task_done(key: tuple, task: asyncio.Task) -> None:
    if _inflight.get(key) is task:
        _inflight.pop(key, None)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("IncidentGate: разбор %s упал", key[3], exc_info=exc)


def inflight_count() -> int:
    return sum(1 for t in _inflight.values() if not t.done())


async def cancel_all(timeout: float = 10.0) -> int:
    """Снять разборы при остановке сервиса. Зовётся ПОСЛЕ остановки движков —
    иначе ещё работающий движок закажет новый. Задача шлюза остаётся жить
    (её task_id — в журнале); после старта аварию закажет восстановление."""
    global _closing
    _closing = True
    tasks = [t for t in _inflight.values() if not t.done()]
    if _recover_task is not None and not _recover_task.done():
        tasks.append(_recover_task)
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.wait(tasks, timeout=timeout)
    _inflight.clear()
    return len(tasks)


def _outcome_for_error(exc: BaseException) -> str:
    """Снята на шлюзе намеренно (оператор, отмена совета) — removed, без повтора;
    остановка шлюза и прочие ошибки — failed, один повтор."""
    from llm.client import LLMError, _is_gateway_stop
    if (isinstance(exc, LLMError) and exc.gateway
            and exc.code in ("stopped", "council_cancelled")
            and not _is_gateway_stop(exc)):
        return INC_REMOVED
    return INC_FAILED


def _route() -> tuple[str, str]:
    """(провайдер, подпись маршрута) строки «Разбор аварии» таблицы."""
    from llm.router import get_route, route_label
    return get_route("incident").get("provider", "llm"), route_label("incident")


async def _ask(prompt: str, meta: dict) -> str:
    """Спросить модель строки «Разбор аварии». Текст с подписью; пустой — ошибка."""
    from llm.router import claude_model_for, format_ai_signature, get_prompt
    provider, label = _route()
    if provider == "llm":
        from llm.client import chat, gateway_label
        # Модель, рассуждение и приоритет — строка таблицы (depth="incident")
        # Совет, принятый шлюзом, не пересылаем после обрыва: он живёт на шлюзе
        # и без соединения — перезаказ решает и учитывает _run_incident
        text = (await chat(get_prompt("incident"), prompt, depth="incident",
                           meta=meta, resend_accepted=False)).strip()
        label = gateway_label(meta, label)
    else:
        model = claude_model_for("incident")
        text = (await _ask_claude(prompt, model)).strip()
        label = model or "Claude API"
    if not text:
        raise EmptyIncidentAnswer("модель вернула пустой ответ")
    # Чужой язык в ответе совета (v5.1.22, решение В4): один проход «очистка /
    # перевод» быстрой моделью, без перезаказа совета (это час работы шлюза).
    # Цитаты руководств со ссылкой [Wn] — законны. Отметка — meta["lang_check"]
    try:
        from llm.lang_check import clean_text
        from llm.router import get_depth
        text, _lang = await clean_text(text, prompt, priority=get_depth("incident").get("priority"),
                                       allow_ref_quotes=True)
        if _lang:
            meta["lang_check"] = _lang
    except asyncio.CancelledError:
        # v5.1.22, ревью incident-4: остановка сервиса посреди очистки (до 240 с) —
        # ответ совета (час работы шлюза) уже получен: исходник без очистки, с
        # подписью — в meta, _run_incident сохранит его до проброса отмены
        try:
            from llm.lang_check import foreign_fragments
            _found = foreign_fragments(text, prompt, allow_ref_quotes=True)[:10]
        except Exception:
            _found = []
        try:
            _sig = format_ai_signature(label)
        except Exception:
            _sig = ""
        meta["uncleaned"] = text + _sig
        meta["lang_check"] = {"found": _found, "cleaned": False, "accepted": False,
                              "error": "остановка сервиса во время очистки — сохранён исходник"}
        raise
    except Exception:
        logger.warning("IncidentGate: проверка языка не выполнена", exc_info=True)
    try:
        text += format_ai_signature(label)
    except Exception:
        logger.warning("IncidentGate: подпись модели не добавлена", exc_info=True)
    return text


def _task_id(meta: dict, exc: BaseException | None = None) -> str | None:
    """task_id задачи шлюза: клиент кладёт его в meta["task_id"] сразу при приёме
    (meta["gorynych"] — только с ответом); у ошибки шлюза — в самой ошибке."""
    m = meta or {}
    return (getattr(exc, "task_id", None) or m.get("task_id")
            or (m.get("gorynych") or {}).get("task_id"))


async def _mark(router_sn: str, equip_type: str, panel_id: int, stop_ts: datetime,
                segment_id: int | None, decision: str, **extra: Any) -> bool:
    """Отметка в журнале гейта. ts — момент останова ВНУТРИ события: по нему
    _slice_gate_state переносит запись при закрытии строки, иначе отметка
    пропала бы, и восстановление заказало бы совет снова."""
    from online import db as online_db
    event = {"kind": "incident", "fault_hash": incident_hash(stop_ts), "decision": decision,
             "ts": stop_ts.isoformat(), "at": datetime.now(timezone.utc).isoformat(),
             **{k: v for k, v in extra.items() if v is not None}}
    for i in range(_MARK_RETRIES + 1):
        try:
            if await online_db.append_segment_gate_event(
                    router_sn, equip_type, panel_id, event,
                    segment_id=segment_id, ts=stop_ts):
                return True
        except Exception:
            logger.warning("IncidentGate: отметка %s не записана", decision, exc_info=True)
        if i < _MARK_RETRIES:
            await asyncio.sleep(_MARK_RETRY_SEC)
    logger.warning("IncidentGate: отметка %s для %s не записана — сегмент не найден",
                   decision, event["fault_hash"])
    return False


async def _state(router_sn: str, equip_type: str, panel_id: int,
                 stop_ts: datetime) -> dict[str, Any]:
    """Разобрана ли авария, есть ли терминальная отметка, сколько было заказов.
    Сбой чтения — «ничего не известно»: разбор лучше заказать, чем потерять."""
    from online import db as online_db
    try:
        return await online_db.get_incident_analysis_state(
            router_sn, equip_type, panel_id, incident_hash(stop_ts), stop_ts)
    except Exception:
        logger.warning("IncidentGate: состояние разбора не прочитано", exc_info=True)
        return {"analysed": False, "terminal": None, "ordered": 0}


async def _context(router_sn: str, equip_type: str, panel_id: int,
                   stop_ts: datetime) -> dict[str, Any]:
    """Оборудование из реестра и состояние регистров на момент останова (v5.1.14).

    Состояние — по пометке incident_state в KB (у PCC3300 — команда на включение и
    положение автомата): если в окне не менялось, в ленту акта оно не попадает,
    а для причины важно. Сбой любого шага — без этого блока (fail-open).
    """
    ctx: dict[str, Any] = {}
    eq = None
    try:
        from db.analytics import get_equipment
        eq = await get_equipment(router_sn, equip_type, panel_id)
    except Exception:
        logger.warning("IncidentGate: оборудование из реестра не прочитано", exc_info=True)
    if not eq:
        return ctx
    cfg = None
    try:
        from analytics import binding
        from config import settings as app_settings
        cfg = binding.build_config(app_settings.knowledge_base_path,
                                   controller_id=eq.get("controller_id"),
                                   engine_id=eq.get("engine_id"), kb_path=eq.get("kb_path"))
    except Exception:
        logger.warning("IncidentGate: конфиг оборудования не собран", exc_info=True)
    try:
        from analytics import binding as _b
        from config import settings as _s
        _fr = _b.build_fault_ref(_s.knowledge_base_path,
                                 controller_id=eq.get("controller_id"),
                                 engine_id=eq.get("engine_id"), kb_path=eq.get("kb_path"))

        def _code_name(code: int) -> str | None:
            e = _fr.lookup(int(code)) or {}
            d = e.get("description") or {}
            return d.get("ru") or d.get("en")
        ctx["code_name"] = _code_name
    except Exception:
        logger.warning("IncidentGate: справочник кодов не собран", exc_info=True)
    try:
        ctx["equipment"] = {
            "name": eq.get("name"),
            "engine": " ".join(str(x) for x in (eq.get("manufacturer"), eq.get("model")) if x) or None,
            "engine_sn": eq.get("engine_sn"),
            "controller": (cfg.controller_label if cfg else None) or eq.get("controller_id"),
        }
        regs = cfg.incident_state_registers if cfg else []
    except Exception:
        logger.warning("IncidentGate: оборудование для разбора не собрано", exc_info=True)
        regs = []
    if regs:
        try:
            from analytics import source as _src
            names = dict(regs)
            periods = await _src.get_enum_periods(
                router_sn, equip_type, panel_id,
                stop_ts - timedelta(seconds=1), stop_ts, addrs=list(names))
            last: dict[int, dict] = {}
            for p in periods:
                last[p["addr"]] = p          # по возрастанию state_start — последний
            ctx["states"] = [
                {"addr": a, "name": names[a],
                 # 65535 — «нет данных» декодера (u16 NA), не значение
                 "label": ("нет данных" if p.get("value") == 65535
                           else p.get("label") or p.get("value")),
                 "since": p["state_start"].isoformat() if p.get("state_start") else None}
                for a, p in sorted(last.items())]
        except Exception:
            logger.warning("IncidentGate: состояние на момент останова не прочитано",
                           exc_info=True)
    return ctx


async def _material(router_sn: str, equip_type: str, panel_id: int,
                    incident: dict[str, Any], stop_ts: datetime,
                    segment_id: int | None) -> str:
    from online import db as online_db
    seg_row = None
    try:
        seg_row = await online_db.get_segment_by_id(segment_id) if segment_id else None
    except Exception:
        logger.warning("IncidentGate: строка сегмента не прочитана", exc_info=True)

    cycle_segments: list[dict[str, Any]] = []
    try:
        win_from = (incident.get("window") or {}).get("from")
        if win_from:
            cycle_segments = await online_db.get_segments_between(
                router_sn, equip_type, panel_id,
                datetime.fromisoformat(win_from), stop_ts,
            )
    except Exception:
        logger.warning("IncidentGate: сегменты цикла не прочитаны — разбор "
                       "без расчётных параметров", exc_info=True)
    try:
        context = await _context(router_sn, equip_type, panel_id, stop_ts)
    except Exception:
        # Блок оборудования — дополнение: его сбой не мешает разбору
        logger.warning("IncidentGate: контекст оборудования не собран", exc_info=True)
        context = {}
    return build_incident_prompt(incident, seg_row, cycle_segments, context)


async def _save(router_sn: str, equip_type: str, panel_id: int, stop_ts: datetime,
                segment_id: int | None, analysis: str, extra: dict | None = None,
                tries: int | None = None) -> bool:
    """Записать разбор рядом с разборами гейта; сегмента нет — повтор раз в
    минуту до 15 мин. Модель повторно не спрашиваем. extra — поля записи разбора
    (v5.1.22: lang_check — отметка прохода «очистка / перевод»). tries — число
    попыток (остановка сервиса — одна, без ожидания, ревью incident-4)."""
    from online import db as online_db
    if tries is None:
        tries = max(1, INCIDENT_SAVE_WINDOW_SEC // max(1, INCIDENT_SAVE_RETRY_SEC)) + 1
    for i in range(tries):
        try:
            if await online_db.save_segment_warning(
                    router_sn, equip_type, panel_id,
                    analysis_md=analysis, fault_hash=incident_hash(stop_ts),
                    alarm_text="Аварийный останов", segment_id=segment_id, ts=stop_ts,
                    **({"extra": extra} if extra else {})):
                logger.info("IncidentGate: разбор аварии сохранён (%d симв.)", len(analysis))
                return True
            logger.warning("IncidentGate: сегмент на %s не найден — разбор держу, повтор "
                           "записи через %d с", stop_ts.isoformat(), INCIDENT_SAVE_RETRY_SEC)
        except Exception:
            logger.warning("IncidentGate: не удалось сохранить разбор", exc_info=True)
        if i < tries - 1:
            await asyncio.sleep(INCIDENT_SAVE_RETRY_SEC)
    return False


async def _reorder_conclusion(router_sn: str, equip_type: str, panel_id: int,
                              stop_ts: datetime, segment_id: int | None) -> None:
    """Совет не получен (failed / save_failed) — заключение аварийного стопа
    пишет ИИ (v5.1.22, P1-5): сегмент заказывается повторно, воркер увидит
    терминальную отметку в журнале и отдаст его модели строки. Только при строке
    «Система (без ИИ)»: иначе ИИ-заключение уже есть. Сбой — в журнал."""
    try:
        from llm.router import SYSTEM_PROVIDER, get_route
        if get_route("seg_shutdown").get("provider") != SYSTEM_PROVIDER:
            return
        from online import db as online_db
        # v5.1.22, ревью incident-5: id мог устареть (совет заказан по открытой
        # стоп-строке, цикл закрытия её удалил, закрытая получила новый id) —
        # прямая ссылка, иначе сегмент по моменту останова, как у _mark и _save
        sid = await online_db.resolve_segment_id(
            router_sn, equip_type, panel_id, segment_id, stop_ts)
        if not sid:
            return
        # Стоп ещё открыт — перезаказ не нужен: закрытый сегмент получит
        # отметку журнала (_slice_gate_state) и уйдёт к ИИ при закрытии
        row = await online_db.get_segment_by_id(int(sid))
        if not row or row.get("t_end") is None:
            return
        from online.engine import _enqueue_segment
        _enqueue_segment(int(sid), again=True)
        logger.info("IncidentGate: совет по %s не получен — заключение аварии #%s "
                    "перезаказано у ИИ", incident_hash(stop_ts), sid)
    except Exception:
        logger.warning("IncidentGate: заключение аварии не перезаказано", exc_info=True)


async def _run_incident(
    router_sn: str, equip_type: str, panel_id: int,
    incident: dict[str, Any], stop_ts: datetime,
    segment_id: int | None = None,
) -> str:
    """Разбор аварии с учётом попыток. Итог: ok / analysed / removed / failed /
    save_failed. Fail-open: разбор — мнение поверх фактов, его отсутствие не
    ломает ни цикл движка, ни акт, который уже записан."""
    fh = incident_hash(stop_ts)
    _, label = _route()
    logger.info("IncidentGate: разбор аварии %s/%s/%s на %s (разбирает %s)",
                router_sn, equip_type, panel_id, stop_ts.isoformat(), label)
    prompt = await _material(router_sn, equip_type, panel_id, incident, stop_ts, segment_id)

    attempt = 0
    prev_tid: str | None = None       # задача шлюза прошлой попытки (осталась жить на шлюзе)
    while True:
        st = await _state(router_sn, equip_type, panel_id, stop_ts)
        if st.get("analysed"):
            logger.info("IncidentGate: %s уже разобрана — совет не заказываю", fh)
            return "analysed"
        if st.get("terminal"):
            logger.info("IncidentGate: %s закрыта отметкой «%s» — не заказываю",
                        fh, st["terminal"])
            return str(st["terminal"])
        ordered = int(st.get("ordered") or 0)
        if ordered >= INCIDENT_MAX_ORDERS:
            await _mark(router_sn, equip_type, panel_id, stop_ts, segment_id, INC_FAILED,
                        reason=f"исчерпано заказов: {ordered}", model=label)
            logger.warning("IncidentGate: %s — исчерпано %d заказов, разбор не получен",
                           fh, ordered)
            await _reorder_conclusion(router_sn, equip_type, panel_id, stop_ts, segment_id)
            return INC_FAILED
        attempt += 1
        await _mark(router_sn, equip_type, panel_id, stop_ts, segment_id, INC_ORDERED,
                    attempt=ordered + 1, model=label, prev_task_id=prev_tid)
        meta: dict = {}
        reason = ""
        err: BaseException | None = None
        try:
            analysis = await asyncio.wait_for(_ask(prompt, meta), INCIDENT_DEADLINE_SEC)
            outcome = "ok"
        except asyncio.TimeoutError:
            outcome, reason = INC_FAILED, f"нет ответа за {INCIDENT_DEADLINE_SEC // 3600} ч"
        except asyncio.CancelledError:
            _raw = meta.get("uncleaned")
            if _raw:
                # v5.1.22, ревью incident-4: ответ совета получен, остановка пришла
                # во время очистки языка — сохраняем исходник одной записью (иначе
                # восстановление заказало бы совет заново: час шлюза и заказ в
                # INCIDENT_MAX_ORDERS), текст — и в журнал сервиса
                _ok = False
                try:
                    _lc = meta.get("lang_check")
                    _ok = await _save(router_sn, equip_type, panel_id, stop_ts, segment_id, _raw,
                                      extra={"lang_check": _lc} if _lc else None, tries=1)
                except BaseException:
                    logger.warning("IncidentGate: исходник совета %s не сохранён", fh, exc_info=True)
                logger.error("IncidentGate: разбор %s получен, очистка языка прервана остановкой "
                             "сервиса — исходник %s; текст:\n%s", fh,
                             "сохранён" if _ok else "НЕ СОХРАНЁН", _raw)
                raise
            logger.warning("IncidentGate: разбор %s снят (остановка сервиса); задача шлюза %s "
                           "остаётся на шлюзе", fh, _task_id(meta) or "—")
            raise
        except Exception as exc:
            err = exc
            outcome, reason = _outcome_for_error(exc), repr(exc)[:300]

        # Совет, принятый шлюзом и оборванный/просроченный, живёт на шлюзе дальше
        # (отмены задачи в версии нет): его task_id — в журнал и в отметки, чтобы
        # снять вручную
        tid = _task_id(meta, err)
        if outcome != "ok":
            if tid and tid not in reason:
                reason = f"{reason}; задача шлюза {tid}"
            logger.warning("IncidentGate: разбор %s не получен (%s): %s", fh, outcome, reason)

        if outcome == "ok":
            try:
                _lc = meta.get("lang_check")
                saved = await _save(router_sn, equip_type, panel_id, stop_ts, segment_id, analysis,
                                    extra={"lang_check": _lc} if _lc else None)
            except asyncio.CancelledError:
                # Остановка сервиса посреди повторов записи: текст уже получен —
                # хотя бы в журнал сервиса. Отметки нет: восстановление закажет снова
                logger.error("IncidentGate: разбор %s не сохранён — остановка сервиса; "
                             "задача шлюза %s; текст:\n%s", fh, _task_id(meta) or "—", analysis)
                raise
            if saved:
                return "ok"
            # Текст — в журнал сервиса: в БД его положить некуда
            logger.error("IncidentGate: разбор %s НЕ СОХРАНЁН за %d мин — текст:\n%s",
                         fh, INCIDENT_SAVE_WINDOW_SEC // 60, analysis)
            await _mark(router_sn, equip_type, panel_id, stop_ts, segment_id, INC_SAVE_FAILED,
                        reason="сегмент не найден", task_id=_task_id(meta), model=label)
            await _reorder_conclusion(router_sn, equip_type, panel_id, stop_ts, segment_id)
            return INC_SAVE_FAILED
        if outcome == INC_REMOVED or attempt >= INCIDENT_ATTEMPTS:
            await _mark(router_sn, equip_type, panel_id, stop_ts, segment_id, outcome,
                        reason=reason, task_id=tid, model=label)
            logger.warning("IncidentGate: разбор %s не получен — %s (%s)", fh, outcome, reason)
            if outcome == INC_FAILED:
                await _reorder_conclusion(router_sn, equip_type, panel_id, stop_ts, segment_id)
            return outcome
        logger.info("IncidentGate: повтор разбора %s через %d мин (задача шлюза %s остаётся "
                    "на шлюзе)", fh, INCIDENT_RETRY_AFTER_SEC // 60, tid or "—")
        prev_tid = tid
        await asyncio.sleep(INCIDENT_RETRY_AFTER_SEC)


async def recover_pending(delay: float = RECOVER_DELAY_SEC) -> int:
    """После старта: заказать разбор аварий, оставшихся без него (рестарт посреди
    совета, деплой). Не старше 48 ч, не больше 3 за старт; только через
    order_incident_analysis — чтобы сработал отбор дублей."""
    from online import db as online_db
    if delay:
        await asyncio.sleep(delay)
    try:
        since = datetime.now(timezone.utc) - timedelta(hours=RECOVER_HOURS)
        rows = await online_db.get_incident_acts_since(since)
    except Exception:
        logger.warning("IncidentGate: восстановление — акты не прочитаны", exc_info=True)
        return 0
    n = 0
    for row in rows:
        if n >= RECOVER_MAX:
            logger.info("IncidentGate: восстановление — лимит %d за старт, остальные "
                        "закажет следующий старт", RECOVER_MAX)
            break
        raw = row.get("incident_json")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                raw = None
        if not isinstance(raw, dict):
            continue
        # Момент останова — из акта: у плашки неудачной попытки строка
        # начинается на попытке, а авария — позже (v5.1.16)
        try:
            stop_ts = _utc(datetime.fromisoformat(raw["stop_ts"])) if raw.get("stop_ts") \
                else _utc(row["t_start"])
        except (TypeError, ValueError):
            stop_ts = _utc(row["t_start"])
        st = await _state(row["router_sn"], row["equip_type"], row["panel_id"], stop_ts)
        if st.get("analysed") or st.get("terminal"):
            continue
        if order_incident_analysis(row["router_sn"], row["equip_type"], row["panel_id"],
                                   raw, stop_ts, row.get("id")):
            n += 1
            logger.info("IncidentGate: восстановление — заказан разбор аварии %s/%s/%s на %s "
                        "(заказов было %d)", row["router_sn"], row["equip_type"],
                        row["panel_id"], stop_ts.isoformat(), int(st.get("ordered") or 0))
    return n


def start_recovery(delay: float = RECOVER_DELAY_SEC) -> None:
    """Запустить восстановление фоном (main.py, после старта движков)."""
    global _recover_task
    _recover_task = asyncio.create_task(recover_pending(delay), name="incident:recover")
