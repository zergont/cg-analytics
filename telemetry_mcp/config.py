# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Настройки сервера телеметрии — секция `telemetry_mcp` в config.yml.

Лимиты сервера (Р12: сервисный конфиг, не KB) — разд. 5.3 проекта. Всё
необязательно, ниже — умолчания.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Умолчания лимитов (design_mcp_telemetry.md, разд. 5.3)
DEFAULT_LIMITS: dict[str, Any] = {
    "series_raw_window_hours": 48,     # окно сырого ряда; больше — укрупнение шага
    "series_max_points": 150,          # точек на колонку
    "series_max_params": 8,            # колонок (параметров) на вызов
    "series_max_window_days": 31,      # дальше — «берите trend»
    "snapshot_max_registers": 400,     # снимок «все»
    "timeline_max_rows": 300,          # строк ленты
    "trend_max_points": 400,           # точек долгого тренда
    "trend_max_window_days": 400,
    "result_max_tokens": 8000,         # ~ размер результата (оценка 1,7 зн/ток)
    "call_timeout_sec": 20,            # один вызов
    "state_max_age_days": 14,          # снимок: насколько давно искать последнее значение
    "debounce_sec": 60,                # лента: дребезг — переключения одного регистра чаще
    "debounce_min_count": 3,           # … и не меньше стольких подряд
    "chars_per_token": 1.7,            # оценка v5.1.9
}


@dataclass
class McpSettings:
    host: str = "0.0.0.0"
    port: int = 8095
    path: str = "/mcp"
    panel_types: frozenset[str] = field(default_factory=lambda: frozenset({"pcc"}))
    limits: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_LIMITS))
    # Разрешённые инструменты по источнику (заголовок X-Source): "*" — все.
    sources: dict[str, Any] = field(default_factory=lambda: {"default": "*"})
    # Заголовок, по которому хост передаёт сквозной task_id (кроме _meta вызова)
    task_header: str = "X-Task-Id"
    source_header: str = "X-Source"
    journal: bool = True
    log_level: str = "INFO"

    def limit(self, key: str) -> Any:
        return self.limits.get(key, DEFAULT_LIMITS.get(key))

    def allowed(self, source: str | None, tool: str) -> bool:
        rule = self.sources.get(source or "", None)
        if rule is None:
            rule = self.sources.get("default", "*")
        if rule == "*" or rule is True:
            return True
        if not rule:
            return False
        return tool in set(rule)


def load(path: Path | None = None) -> McpSettings:
    """Прочитать секцию telemetry_mcp из config.yml (путь — как у config.Settings)."""
    from config import _CONFIG_PATH
    p = path or _CONFIG_PATH
    data: dict[str, Any] = {}
    if p.exists():
        with p.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    sec = data.get("telemetry_mcp") or {}
    limits = dict(DEFAULT_LIMITS)
    limits.update({k: v for k, v in (sec.get("limits") or {}).items() if v is not None})
    pt = sec.get("panel_types")
    return McpSettings(
        host=str(sec.get("host", "0.0.0.0")),
        port=int(sec.get("port", 8095)),
        path=str(sec.get("path", "/mcp")),
        panel_types=frozenset(pt) if pt else frozenset({"pcc"}),
        limits=limits,
        sources=dict(sec.get("sources") or {"default": "*"}),
        task_header=str(sec.get("task_header", "X-Task-Id")),
        source_header=str(sec.get("source_header", "X-Source")),
        journal=bool(sec.get("journal", True)),
        log_level=str(sec.get("log_level", "INFO")).upper(),
    )
