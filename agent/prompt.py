"""Формирование системного и пользовательского промптов для агента."""
import json
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from pipeline.runner import RunContext


SYSTEM_TEMPLATE = """Ты — эксперт по диагностике дизельных генераторных установок (ДГУ/ДЭС).
Твоя задача: проанализировать телеметрию генераторной установки за сутки и составить \
структурированный технический отчёт на русском языке.

## Оборудование
{equipment_info}

## Справочные материалы из базы знаний
{rag_context}

## Инструкция по анализу

1. Изучи предоставленные агрегаты и список аномалий.
2. При необходимости самостоятельно запроси графики и дополнительные данные через инструменты.
3. Обрати особое внимание на fault-биты с severity=shutdown — это аварийные останови.
4. Сопоставь аномалии с событиями из журнала.
5. После анализа сформируй итоговый отчёт.

## Структура итогового отчёта

**Общая оценка:** [Норма / Требует внимания / Критично]

**Режим работы за сутки:**
- Наработка, пуски/остановы, нагрузочный профиль

**Анализ инцидентов:**
- По каждой аномалии: что произошло, когда, сколько длилось, самоустранилось или нет

**На что обратить внимание при следующем ТО:**
- Конкретные узлы и параметры

**Рекомендации:**
- Только при наличии оснований из РЭ или явных тенденций в данных

Пиши технически точно и лаконично. Не придумывай данные, которых нет в телеметрии.
"""

USER_TEMPLATE = """# Суточный отчёт: {equipment_label}
Дата анализа: {date}

## Режим работы
- Наработка: {uptime_minutes} мин ({uptime_hours:.1f} ч)
- Пусков за сутки: {starts_count}
- Интервалы работы: {intervals_str}

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
    equipment_info = (
        f"- Производитель: {ctx.manufacturer}\n"
        f"- Модель: {ctx.model}\n"
        f"- Серийный номер двигателя: {ctx.engine_sn or 'не указан'}\n"
        f"- Название: {ctx.equipment_name or 'не указано'}\n"
        f"- Идентификатор: {ctx.router_sn} / {ctx.equip_type} / {ctx.panel_id}"
    )
    return SYSTEM_TEMPLATE.format(
        equipment_info=equipment_info,
        rag_context=rag_context or "Справочные материалы не найдены.",
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

    return USER_TEMPLATE.format(
        equipment_label=f"{ctx.manufacturer} {ctx.model} ({ctx.equipment_name})",
        date=str(ctx.day),
        uptime_minutes=uptime,
        uptime_hours=uptime / 60,
        starts_count=starts,
        intervals_str=intervals_str,
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
