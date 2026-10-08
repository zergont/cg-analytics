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

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_SEVERITY_RU = {
    "shutdown": "АВАРИЯ", "shutdown_cooldown": "АВАРИЯ",
    "derate": "снижение номинала", "warning": "предупреждение",
}


def _ts(raw: Any) -> str:
    if not raw:
        return "—"
    s = str(raw)
    return s[11:19] if len(s) > 19 else s


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
_NOT_PARAMS = {"LAST_FAULT_CODE", "LAST_FAULT_TYPE"}


def _chars_of(seg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Расчётные характеристики сегмента по ролям, сведённые по подсегментам.

    Мин и макс — по всему сегменту, «на конце» — последнее значение
    последнего подсегмента, где роль есть.
    """
    import json as _json
    raw = seg.get("characteristics_json")
    if isinstance(raw, str):
        try:
            raw = _json.loads(raw)
        except Exception:
            return {}
    out: dict[str, dict[str, Any]] = {}
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
    if v is None:
        return "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return f"{f:.0f}" if abs(f) >= 100 else f"{f:.2f}".rstrip("0").rstrip(".")


def _format_cycle_params(
    segments: list[dict[str, Any]], stop_ts: Any,
) -> list[str]:
    """Цикл от последнего нормального останова: ход режимов и параметры.

    Параметры не читаются заново — это расчётные характеристики, которые
    аналитика посчитала при закрытии каждого сегмента. Весь цикл — сводкой
    мин/макс, подробно — сегмент, из которого машина упала, и последний
    рабочий: тот же приём, что у ленты событий.

    Сводка — ПО РЕЖИМАМ. Общая на весь цикл врала бы: минимум давления масла
    приходил бы со стоянки, а максимум температуры ОЖ — с прогрева после
    прошлой аварии, когда насос уже встал.
    """
    from analytics.serializer import RUN_STATE_RU

    def _dt(x: Any) -> datetime | None:
        if isinstance(x, str):
            try:
                x = datetime.fromisoformat(x)
            except ValueError:
                return None
        if not isinstance(x, datetime):
            return None
        return x if x.tzinfo else x.replace(tzinfo=timezone.utc)

    def _hms(x: Any) -> str:
        d = _dt(x)
        return d.strftime("%Y-%m-%d %H:%M:%S") if d else "—"

    stop = _dt(stop_ts)
    if stop is None:
        return []
    before = [sg for sg in segments
              if (_dt(sg.get("t_start")) or stop) < stop]
    if not before:
        return []
    def _mode(sg: dict[str, Any]) -> str:
        rs = sg.get("run_state")
        name = RUN_STATE_RU.get(rs, f"RUN_STATE={rs}")
        raw = sg.get("characteristics_json")
        if rs == 0 and isinstance(raw, str):
            try:
                import json as _json
                raw = _json.loads(raw)
            except Exception:
                raw = None
        if rs == 0 and isinstance(raw, dict) and raw.get("stop_kind") == "EMERGENCY":
            name += " (аварийный)"
        return name

    out: list[str] = ["", "Ход цикла (сегменты аналитики, UTC):"]
    for sg in before:
        out.append(f"  - {_mode(sg)}: "
                   f"{_hms(sg.get('t_start'))} — {_hms(sg.get('t_end'))}")

    by_mode: dict[str, dict[str, dict[str, Any]]] = {}
    for sg in before:
        cyc = by_mode.setdefault(_mode(sg), {})
        for role, ch in _chars_of(sg).items():
            acc = cyc.setdefault(role, {"min": None, "max": None, "unit": ch["unit"]})
            for k, pick in (("min", min), ("max", max)):
                if ch[k] is not None:
                    acc[k] = ch[k] if acc[k] is None else pick(acc[k], ch[k])
    for mode, cyc in by_mode.items():
        if not cyc:
            continue
        out += ["", f"Параметры за цикл — {mode} (расчётные, мин / макс):"]
        for role in sorted(cyc):
            c = cyc[role]
            out.append(f"  {role}: {_fmt_num(c['min'])} / {_fmt_num(c['max'])} {c['unit']}".rstrip())

    def _detail(sg: dict[str, Any], title: str) -> None:
        chars = _chars_of(sg)
        if not chars:
            return
        out.extend(["", f"{title} — {_mode(sg)}, "
                        f"{_hms(sg.get('t_start'))} — {_hms(sg.get('t_end'))} "
                        f"(мин / макс / на конце):"])
        for role in sorted(chars):
            c = chars[role]
            out.append(f"  {role}: {_fmt_num(c['min'])} / {_fmt_num(c['max'])} / "
                       f"{_fmt_num(c['end'])} {c['unit']}".rstrip())

    # Откуда упала: последний не-стоповый сегмент. У 1452 это пуск или
    # прогрев — до работы машина не дошла; у штатной нагрузки — работа или
    # разгрузка. Последний рабочий — отдельно, если упала не из работы.
    fell = next((sg for sg in reversed(before) if sg.get("run_state") != 0), None)
    work = next((sg for sg in reversed(before) if sg.get("run_state") == 3), None)
    if fell is not None:
        _detail(fell, "Сегмент, из которого машина остановилась")
    if work is not None and work is not fell:
        _detail(work, "Последний рабочий сегмент цикла")
    return out


def build_incident_prompt(
    incident: dict[str, Any], seg_row: dict[str, Any] | None = None,
    cycle_segments: list[dict[str, Any]] | None = None,
) -> str:
    """Промпт разбора аварии из акта.

    Порядок продуман под чтение моделью: сперва что машина уже везла на себе
    к моменту падения, потом сам останов, потом лента. Вопрос в конце —
    иначе модель отвечает на первое, что увидела.
    """
    ch = incident.get("character") or {}
    win = incident.get("window") or {}
    out: list[str] = [
        "Разбери аварийный останов газопоршневой установки.",
        "",
        f"Останов: {incident.get('stop_ts') or '—'}",
        f"Характер по детерминированному гейту: {ch.get('character', '?')} "
        f"(уверенность {ch.get('confidence', '?')})",
    ]
    votes = (ch.get("immediate_votes") or []) + (ch.get("controlled_votes") or [])
    if votes:
        out.append("Сигналы: " + "; ".join(votes))
    if win.get("baseline") == "fallback":
        out.append("Предшествующего штатного останова в истории нет — "
                   "окно взято по последним сегментам.")
    elif win.get("from"):
        out.append(f"Окно разбора от последнего штатного останова: {win['from']}")

    standing = incident.get("standing") or []
    if standing:
        out += ["", "Висело на момент останова (маска — с какого времени):"]
        for f in standing:
            sev = _SEVERITY_RU.get(f.get("severity") or "", f.get("severity") or "событие")
            out.append(f"  - {f.get('name')} [{sev}] — с {f.get('since')} "
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
            span = (f"{_ts(g.get('first'))}—{_ts(g.get('last'))}"
                    if (g.get("count") or 1) > 1 else _ts(g.get("first")))
            out.append(f"  - {g.get('name')} [{mark}] × {g.get('count')} ({span})")

    chrono = incident.get("chronology") or []
    if chrono:
        out += ["", "Лента вокруг останова (UTC):", "```"]
        for e in chrono:
            kind = "СОБЫТИЕ  " if e.get("kind") == "fault" else "состояние"
            bit = e.get("bit")
            addr = f"{e.get('addr')}/{bit}" if bit is not None else str(e.get("addr"))
            val = (f" = {e.get('label')}"
                   if e.get("kind") == "state" and e.get("label") else "")
            sev = f"  [{e.get('severity')}]" if e.get("severity") else ""
            out.append(f"{_ts(e.get('ts'))}  {kind} {addr:9} "
                       f"{e.get('name') or ''}{val}{sev}")
        out.append("```")

    out += _format_cycle_params(cycle_segments or [], incident.get("stop_ts"))

    if seg_row and seg_row.get("report_md"):
        out += ["", "Отчёт аналитики по сегменту:", "",
                str(seg_row["report_md"])[:8000]]

    out += [
        "",
        "Задача: назови наиболее вероятную причину останова и на чём основан "
        "вывод. Отдельно укажи, что из висевшего к моменту падения могло к "
        "нему привести, а что к делу не относится. Если данных для вывода "
        "не хватает — скажи прямо, какого сигнала недостаёт. Не предлагай "
        "регламентных работ, которых не следует из наблюдаемого.",
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
            system=get_prompt("warning_claude"),
            messages=[{"role": "user", "content": user_prompt}],
        )
        if response.stop_reason == "max_tokens":
            logger.warning("IncidentGate: разбор обрезан по max_tokens=%s",
                           cfg["max_tokens"])
        return "".join(b.text for b in response.content if hasattr(b, "text"))
    finally:
        if http_client:
            await http_client.aclose()


async def analyze_incident(
    router_sn: str, equip_type: str, panel_id: int,
    incident: dict[str, Any], stop_ts: datetime,
    segment_id: int | None = None,
) -> None:
    """Заказать разбор аварии и сохранить его рядом с разборами гейта.

    Fail-open во всём: разбор — это мнение поверх фактов, и его отсутствие не
    должно ломать ни цикл движка, ни сам акт, который уже записан.
    """
    from llm.router import get_warning_level_route, get_prompt
    from online import db as online_db

    route = get_warning_level_route("авария")
    provider = route.get("provider", "api")
    model = route.get("model", "")
    fault_hash = f"incident:{stop_ts.isoformat()}"

    logger.info("IncidentGate: разбор аварии %s/%s/%s на %s (provider=%s, model=%s)",
                router_sn, equip_type, panel_id, stop_ts.isoformat(), provider, model)

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

    prompt = build_incident_prompt(incident, seg_row, cycle_segments)

    try:
        if provider == "llm":
            from llm.client import chat
            analysis = (await chat(get_prompt("warning_claude"), prompt,
                                   model=model or None)).strip()
        else:
            analysis = (await _ask_claude(prompt, model)).strip()
    except Exception:
        logger.warning("IncidentGate: провайдер не ответил — авария остаётся "
                       "с актом, но без разбора", exc_info=True)
        return

    if not analysis:
        logger.warning("IncidentGate: пустой ответ провайдера, разбор не сохранён")
        return

    try:
        saved = await online_db.save_segment_warning(
            router_sn, equip_type, panel_id,
            analysis_md=analysis,
            fault_hash=fault_hash,
            alarm_text="Аварийный останов",
            segment_id=segment_id,
            ts=stop_ts,
        )
        if not saved:
            logger.warning("IncidentGate: разбор НЕ СОХРАНЁН — сегмент на %s не найден",
                           stop_ts.isoformat())
        else:
            logger.info("IncidentGate: разбор аварии сохранён (%d симв.)", len(analysis))
    except Exception:
        logger.warning("IncidentGate: не удалось сохранить разбор", exc_info=True)
