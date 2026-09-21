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


def build_incident_prompt(
    incident: dict[str, Any], seg_row: dict[str, Any] | None = None,
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
            sev = _SEVERITY_RU.get(f.get("severity") or "", f.get("severity") or "?")
            out.append(f"  - {f.get('name')} [{sev}] — с {f.get('since')} "
                       f"({_age(f.get('age_sec') or 0)})")

    summary = incident.get("summary") or []
    if summary:
        out += ["", f"Что происходило за период (событий всего "
                    f"{incident.get('events_total', '?')}):"]
        for g in summary[:20]:
            mark = "авария/фронт" if g.get("kind") == "fault" else "состояние"
            span = (f"{_ts(g.get('first'))}—{_ts(g.get('last'))}"
                    if (g.get("count") or 1) > 1 else _ts(g.get("first")))
            out.append(f"  - {g.get('name')} [{mark}] × {g.get('count')} ({span})")
        if len(summary) > 20:
            out.append(f"  … ещё {len(summary) - 20} видов")

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

    prompt = build_incident_prompt(incident, seg_row)

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
