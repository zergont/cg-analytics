"""Загрузка конфигурации из config.yml."""
from pathlib import Path

import yaml

_CONFIG_PATH = Path(__file__).parent / "config.yml"


class Settings:
    def __init__(self, data: dict) -> None:
        db = data.get("databases", {})
        self.source_db_url: str = db["source"]
        self.analytics_db_url: str = db["analytics"]

        ant = data.get("anthropic", {})
        self.anthropic_api_key: str = ant["api_key"]
        self.anthropic_model: str = ant.get("model", "claude-sonnet-4-6")
        self.max_tool_calls: int = int(ant.get("max_tool_calls", 10))
        self.max_tokens: int = int(ant.get("max_tokens", 8096))

        sched = data.get("schedule", {})
        self.schedule_hour: int = int(sched.get("hour", 21))
        self.schedule_minute: int = int(sched.get("minute", 5))

        emb = data.get("embeddings", {})
        self.embedding_base_url: str = emb.get("base_url", "http://localhost:11434")
        self.embedding_model: str = emb.get("model", "nomic-embed-text")
        self.embedding_dim: int = int(emb.get("dim", 768))

        kb = data.get("knowledge_base", {})
        self.knowledge_base_path: Path = Path(kb.get("path", "./knowledge_base"))

        web = data.get("web", {})
        self.web_host: str = web.get("host", "0.0.0.0")
        self.web_port: int = int(web.get("port", 8090))

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
