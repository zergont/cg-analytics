# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Формирование системного и пользовательского промптов для агента."""
import json
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from pipeline.runner import RunContext


# Редактируемая часть системного промпта (роль, инструкция, структура отчёта)
# живёт в AI-роутере (llm/router.py, задача daily_agent) и правится через /settings.
# Здесь остаётся только автоматическая сборка блоков данных.

USER_TEMPLATE = """# Суточный отчёт: {equipment_label}
Дата анализа: {date}

## Режим работы
- Наработка: {uptime_minutes} мин ({uptime_hours:.1f} ч)
- Пусков за сутки: {starts_count}
- Интервалы работы: {intervals_str}

## Сегменты суток ({segment_count})
```json
{segments}
```

## Агрегированные показатели (ключевые параметры)
```json
{key_aggregates}
```

## Выявленные аномалии ({anomaly_count})
```json
{anomalies}
```

## События за сутки ({event_count})
```json
{events_summary}
```

Начни анализ. При необходимости используй инструменты для детального исследования.
"""


def build_system_prompt(ctx: "RunContext", rag_context: str) -> str:
    from llm.router import get_prompt

    equipment_info = (
        f"- Производитель: {ctx.manufacturer}\n"
        f"- Модель: {ctx.model}\n"
        f"- Серийный номер двигателя: {ctx.engine_sn or 'не указан'}\n"
        f"- Название: {ctx.equipment_name or 'не указано'}\n"
        f"- Идентификатор: {ctx.router_sn} / {ctx.equip_type} / {ctx.panel_id}"
    )
    return (
        f"{get_prompt('daily_agent')}\n\n"
        f"## Оборудование\n{equipment_info}\n\n"
        f"## Справочные материалы из базы знаний\n"
        f"{rag_context or 'Справочные материалы не найдены.'}"
    )


def build_user_prompt(ctx: "RunContext") -> str:
    uptime = ctx.aggregates.get("uptime_minutes", 0) or 0
    starts = ctx.aggregates.get("starts_count", 0) or 0
    intervals = ctx.aggregates.get("operating_intervals", [])

    intervals_str = (
        ", ".join(f"{s[:16]}–{e[:16]}" for s, e in intervals[:5])
        if intervals else "нет данных"
    )
    if len(intervals) > 5:
        intervals_str += f" (и ещё {len(intervals) - 5})"

    # Ключевые параметры для промпта (не более 30 регистров)
    by_reg = ctx.aggregates.get("by_register", {})
    key_regs = _select_key_registers(by_reg, ctx.register_map)

    segments_brief = _summarize_segments(ctx.segments)

    return USER_TEMPLATE.format(
        equipment_label=f"{ctx.manufacturer} {ctx.model} ({ctx.equipment_name})",
        date=str(ctx.day),
        uptime_minutes=uptime,
        uptime_hours=uptime / 60,
        starts_count=starts,
        intervals_str=intervals_str,
        segment_count=len(ctx.segments),
        segments=json.dumps(segments_brief, ensure_ascii=False, indent=2),
        key_aggregates=json.dumps(key_regs, ensure_ascii=False, indent=2),
        anomaly_count=len(ctx.anomalies),
        anomalies=json.dumps(ctx.anomalies, ensure_ascii=False, indent=2),
        event_count=len(ctx.events),
        events_summary=_summarize_events(ctx.events),
    )


def _select_key_registers(
    by_reg: dict[str, Any],
    register_map: dict[int, dict],
    max_count: int = 30,
) -> dict[str, Any]:
    """Выбрать наиболее информативные регистры для промпта."""
    # Приоритет: температура, давление, мощность, напряжение, ток, частота
    priority_keywords = [
        "temp", "pressure", "oil", "coolant", "power", "kw", "voltage",
        "current", "freq", "speed", "rpm", "fuel", "battery",
        "температур", "давлен", "мощност", "напряжен", "ток", "частот",
    ]

    scored: list[tuple[int, str, dict]] = []
    for addr_str, agg in by_reg.items():
        name_lower = agg.get("name", "").lower()
        desc_lower = register_map.get(int(addr_str), {}).get("description", "").lower()
        score = sum(1 for kw in priority_keywords if kw in name_lower or kw in desc_lower)
        scored.append((score, addr_str, agg))

    scored.sort(key=lambda x: -x[0])

    result = {}
    for _, addr_str, agg in scored[:max_count]:
        result[addr_str] = {
            "name": agg.get("name"),
            "unit": agg.get("unit"),
            "min": agg.get("min"),
            "max": agg.get("max"),
            "mean": agg.get("mean"),
        }
    return result


def _summarize_events(events: list[dict[str, Any]]) -> str:
    if not events:
        return "[]"
    summary = [
        {
            "ts": str(ev.get("created_at", ""))[:19],
            "type": ev.get("type"),
            "description": ev.get("description"),
        }
        for ev in events[:50]  # не более 50 событий в промпт
    ]
    return json.dumps(summary, ensure_ascii=False, indent=2)


def _summarize_segments(segments: list[dict[str, Any]]) -> list[dict]:
    """Краткое представление сегментов для промпта."""
    result = []
    for seg in segments:
        brief: dict[str, Any] = {
            "type": seg.get("type"),
            "label": seg.get("label"),
            "start": str(seg.get("start", ""))[:19],
            "end": str(seg.get("end", ""))[:19],
            "duration_min": seg.get("duration_min"),
        }
        if seg.get("notes"):
            brief["notes"] = seg["notes"]
        if seg.get("related_anomalies"):
            brief["related_anomalies"] = len(seg["related_anomalies"])
        result.append(brief)
    return result
