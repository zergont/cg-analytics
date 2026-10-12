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
# Счётчик наработки — монотонный, не параметр режима (в акте «мин–макс» бессмыслен)
_NOT_PARAMS = {"LAST_FAULT_CODE", "LAST_FAULT_TYPE", "ENGINE_HOURS"}


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
) -> str:
    """Промпт разбора аварии из акта.

    Порядок продуман под чтение моделью: сперва что за машина и что она уже
    везла на себе к моменту падения, потом сам останов, потом лента. Вопрос в
    конце — иначе модель отвечает на первое, что увидела. context — оборудование
    из реестра и состояние на момент останова (_context, v5.1.14).
    """
    ch = incident.get("character") or {}
    win = incident.get("window") or {}
    eq = (context or {}).get("equipment") or {}
    out: list[str] = ["Разбери аварийный останов дизель-генераторной установки."]
    if eq:
        out += ["", "Оборудование (реестр):"]
        if eq.get("name"):
            out.append(f"  - Установка: {eq['name']}")
        if eq.get("engine"):
            out.append(f"  - Двигатель: {eq['engine']}"
                       + (f", s/n {eq['engine_sn']}" if eq.get("engine_sn") else ""))
        if eq.get("controller"):
            out.append(f"  - Контроллер (панель управления): {eq['controller']}")
    out += [
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
    codes = _fault_codes_line(incident)
    if codes:
        out.append(codes)

    states = (context or {}).get("states") or []
    if states:
        out += ["", "Состояние на момент останова (не обязательно менялось в окне — "
                    "в ленту попадают только смены):"]
        stop_day = str(incident.get("stop_ts") or "")[:10]
        for s in states:
            since = ""
            if s.get("since"):
                raw = str(s["since"])
                # Давнее состояние — с датой: «с 29.09 13:10 UTC», а не «с 13:10:22»
                since = (f" (с {_ts(raw)})" if raw[:10] == stop_day
                         else f" (с {raw[8:10]}.{raw[5:7]} {raw[11:16]} UTC)")
            out.append(f"  - {s['addr']} {s['name']} = {s['label']}{since}")

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
    task = asyncio.create_task(
        _run_incident(router_sn, equip_type, int(panel_id), incident, _utc(stop_ts), segment_id),
        name=f"incident:{router_sn}/{equip_type}/{panel_id}",
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
                segment_id: int | None, analysis: str) -> bool:
    """Записать разбор рядом с разборами гейта; сегмента нет — повтор раз в
    минуту до 15 мин. Модель повторно не спрашиваем."""
    from online import db as online_db
    tries = max(1, INCIDENT_SAVE_WINDOW_SEC // max(1, INCIDENT_SAVE_RETRY_SEC)) + 1
    for i in range(tries):
        try:
            if await online_db.save_segment_warning(
                    router_sn, equip_type, panel_id,
                    analysis_md=analysis, fault_hash=incident_hash(stop_ts),
                    alarm_text="Аварийный останов", segment_id=segment_id, ts=stop_ts):
                logger.info("IncidentGate: разбор аварии сохранён (%d симв.)", len(analysis))
                return True
            logger.warning("IncidentGate: сегмент на %s не найден — разбор держу, повтор "
                           "записи через %d с", stop_ts.isoformat(), INCIDENT_SAVE_RETRY_SEC)
        except Exception:
            logger.warning("IncidentGate: не удалось сохранить разбор", exc_info=True)
        if i < tries - 1:
            await asyncio.sleep(INCIDENT_SAVE_RETRY_SEC)
    return False


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
                saved = await _save(router_sn, equip_type, panel_id, stop_ts, segment_id, analysis)
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
            return INC_SAVE_FAILED
        if outcome == INC_REMOVED or attempt >= INCIDENT_ATTEMPTS:
            await _mark(router_sn, equip_type, panel_id, stop_ts, segment_id, outcome,
                        reason=reason, task_id=tid, model=label)
            logger.warning("IncidentGate: разбор %s не получен — %s (%s)", fh, outcome, reason)
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
        stop_ts = _utc(row["t_start"])
        st = await _state(row["router_sn"], row["equip_type"], row["panel_id"], stop_ts)
        if st.get("analysed") or st.get("terminal"):
            continue
        raw = row.get("incident_json")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                raw = None
        if not isinstance(raw, dict):
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
