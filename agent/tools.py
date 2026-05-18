"""Определения инструментов для Anthropic API (tool use)."""

TOOLS: list[dict] = [
    {
        "name": "get_timeseries_chart",
        "description": (
            "Построить график временного ряда одного или нескольких параметров "
            "за произвольный период внутри анализируемых суток. "
            "Используй для визуализации динамики параметра во времени."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "addrs": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Список Modbus-адресов регистров для отображения на одном графике.",
                },
                "from_hour": {
                    "type": "integer",
                    "description": "Начальный час UTC (0–23). По умолчанию 0 (начало суток).",
                    "default": 0,
                },
                "to_hour": {
                    "type": "integer",
                    "description": "Конечный час UTC (1–24). По умолчанию 24 (конец суток).",
                    "default": 24,
                },
            },
            "required": ["addrs"],
        },
    },
    {
        "name": "get_correlation_chart",
        "description": (
            "Построить график корреляции двух параметров (scatter plot). "
            "Полезно для анализа зависимостей, например: нагрузка vs температура масла."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "addr_x": {
                    "type": "integer",
                    "description": "Modbus-адрес параметра по оси X.",
                },
                "addr_y": {
                    "type": "integer",
                    "description": "Modbus-адрес параметра по оси Y.",
                },
            },
            "required": ["addr_x", "addr_y"],
        },
    },
    {
        "name": "get_aggregates",
        "description": (
            "Получить агрегированные статистики (мин/макс/среднее/медиана) "
            "для указанных регистров за произвольный период внутри суток."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "addrs": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Список Modbus-адресов регистров.",
                },
                "from_hour": {
                    "type": "integer",
                    "description": "Начальный час UTC (0–23).",
                    "default": 0,
                },
                "to_hour": {
                    "type": "integer",
                    "description": "Конечный час UTC (1–24).",
                    "default": 24,
                },
            },
            "required": ["addrs"],
        },
    },
    {
        "name": "get_events",
        "description": (
            "Получить список событий и ошибок за период с возможностью фильтрации. "
            "Используй для детального изучения инцидентов."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "from_hour": {
                    "type": "integer",
                    "description": "Начальный час UTC (0–23).",
                    "default": 0,
                },
                "to_hour": {
                    "type": "integer",
                    "description": "Конечный час UTC (1–24).",
                    "default": 24,
                },
                "event_type_filter": {
                    "type": "string",
                    "description": "Фильтр по типу события (подстрока). Оставь пустым для всех событий.",
                    "default": "",
                },
            },
            "required": [],
        },
    },
]
