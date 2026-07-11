# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Загрузка конфигурации из config.yml."""
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

_CONFIG_PATH = Path(__file__).parent / "config.yml"


class Settings:
    def __init__(self, data: dict) -> None:
        db = data.get("databases", {})
        self.source_db_url: str = db["source"]
        self.analytics_db_url: str = db["analytics"]
        self.source_query_timeout: int = int(db.get("source_query_timeout", 120))

        ant = data.get("anthropic", {})
        self.anthropic_api_key: str = ant["api_key"]
        self.anthropic_model: str = ant.get("model", "claude-sonnet-4-6")
        self.max_tool_calls: int = int(ant.get("max_tool_calls", 10))
        self.max_tokens: int = int(ant.get("max_tokens", 8096))
        # Опциональный HTTP-прокси для доступа к Anthropic API (например через tinyproxy)
        self.anthropic_proxy: str | None = ant.get("proxy") or None

        sched = data.get("schedule", {})
        self.schedule_hour: int = int(sched.get("hour", 21))
        self.schedule_minute: int = int(sched.get("minute", 5))

        llm = data.get("llm", {})
        self.llm_base_url: str = llm.get("base_url", "http://localhost:11434")
        self.llm_model: str = llm.get("model", "qwen2.5:14b")
        self.llm_temperature: float = float(llm.get("temperature", 0.1))
        self.llm_num_ctx: int = int(llm.get("num_ctx", 16384))

        kb = data.get("knowledge_base", {})
        self.knowledge_base_path: Path = Path(kb.get("path", "./knowledge_base"))
        # Рабочий оверлей KB (правки из веб-морды). НЕ в git — эталон везёт git,
        # рабочая версия перекрывает его пофайлно (kb_read/kb_write в config.py).
        # git pull не конфликтует; не тронутые файлы едут из git и обновляются сами.
        _kb_work = kb.get("work_path")
        self.knowledge_base_work_path: Path = Path(_kb_work) if _kb_work else (
            self.knowledge_base_path.parent
            / (self.knowledge_base_path.name + "_work")
        )

        web = data.get("web", {})
        self.web_host: str = web.get("host", "0.0.0.0")
        self.web_port: int = int(web.get("port", 8090))
        # Часовой пояс для разбивки суток. По умолчанию МСК (UTC+3).
        # Границы суток в БД-запросах и сегментаторе считаются по этому поясу.
        tz_name: str = web.get("timezone", "Europe/Moscow")
        self.timezone: ZoneInfo = ZoneInfo(tz_name)
        self.timezone_name: str = tz_name

        log = data.get("logging", {})
        self.log_level: str = log.get("level", "INFO").upper()

    @classmethod
    def load(cls, path: Path = _CONFIG_PATH) -> "Settings":
        if not path.exists():
            raise FileNotFoundError(
                f"Файл конфигурации не найден: {path}\n"
                f"Скопируйте config.example.yml → config.yml и заполните значения."
            )
        with path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return cls(data)


settings = Settings.load()

# ── Изменяемый часовой пояс ───────────────────────────────────────────────────
# Инициализируется из config.yml, может быть обновлён из БД при старте приложения
# или сменён пользователем через Web UI без перезапуска сервиса.

_current_tz: ZoneInfo = settings.timezone


def get_tz() -> ZoneInfo:
    """Текущий часовой пояс разбивки суток (обновляется из БД при старте)."""
    return _current_tz


def set_tz(tz_name: str) -> None:
    """Обновить часовой пояс в памяти. Вызывается при загрузке из БД или смене через UI."""
    global _current_tz
    _current_tz = ZoneInfo(tz_name)


# ── KB: оверлей рабочей версии поверх git-эталона (Вариант 2) ─────────────────
# git трекает эталон в knowledge_base_path; правки из веб-морды живут в
# knowledge_base_work_path (gitignored). Резолвинг пофайлный: рабочая версия
# перекрывает эталон, если существует. git pull не конфликтует.

def kb_read(path: Path) -> Path:
    """Путь для ЧТЕНИЯ KB-файла: рабочий оверлей если есть, иначе git-эталон.

    path — абсолютный/относительный путь под knowledge_base_path. Если он вне
    KB-дерева — возвращается как есть (оверлей не применяется).
    """
    path = Path(path)
    try:
        rel = path.resolve().relative_to(settings.knowledge_base_path.resolve())
    except (ValueError, OSError):
        return path
    work = settings.knowledge_base_work_path / rel
    return work if work.exists() else path


def kb_write(path: Path) -> Path:
    """Путь для ЗАПИСИ KB-файла: всегда в рабочий оверлей (создаёт папки).

    Правки из веб-морды идут только в work — эталон git остаётся нетронутым.
    """
    path = Path(path)
    try:
        rel = path.resolve().relative_to(settings.knowledge_base_path.resolve())
    except (ValueError, OSError):
        return path
    work = settings.knowledge_base_work_path / rel
    work.parent.mkdir(parents=True, exist_ok=True)
    return work


# Список часовых поясов, доступных в UI (IANA-имя → метка)
TIMEZONE_CHOICES: list[tuple[str, str]] = [
    ("UTC",                  "UTC+0 — UTC"),
    ("Europe/Kaliningrad",   "UTC+2 — Калининград"),
    ("Europe/Moscow",        "UTC+3 — Москва / МСК"),
    ("Europe/Samara",        "UTC+4 — Самара"),
    ("Asia/Yekaterinburg",   "UTC+5 — Екатеринбург"),
    ("Asia/Omsk",            "UTC+6 — Омск"),
    ("Asia/Krasnoyarsk",     "UTC+7 — Красноярск"),
    ("Asia/Irkutsk",         "UTC+8 — Иркутск"),
    ("Asia/Yakutsk",         "UTC+9 — Якутск"),
    ("Asia/Vladivostok",     "UTC+10 — Владивосток"),
    ("Asia/Magadan",         "UTC+11 — Магадан"),
    ("Asia/Kamchatka",       "UTC+12 — Камчатка"),
]
