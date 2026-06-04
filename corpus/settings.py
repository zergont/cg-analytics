"""In-memory конфигурация Claude API (изменяется через веб-морду без перезапуска)."""
import logging
from config import settings as _cfg

logger = logging.getLogger(__name__)

_state: dict = {
    "model":          _cfg.anthropic_model,
    "max_tool_calls": _cfg.max_tool_calls,
    "max_tokens":     _cfg.max_tokens,
    "proxy":          _cfg.anthropic_proxy or "",
    "system_prompt":  "",  # заполняется при старте из corpus/prompt.py или БД
}


def apply_claude_settings(
    model: str,
    max_tool_calls: int,
    max_tokens: int,
    proxy: str,
    system_prompt: str,
) -> None:
    """Обновить конфигурацию Claude API в памяти (вступает в силу немедленно)."""
    _state["model"]          = model.strip()
    _state["max_tool_calls"] = int(max_tool_calls)
    _state["max_tokens"]     = int(max_tokens)
    _state["proxy"]          = proxy.strip()
    _state["system_prompt"]  = system_prompt.strip()
    logger.info("Claude API настройки обновлены: model=%s", _state["model"])


def get_claude_settings() -> dict:
    """Вернуть копию текущей конфигурации Claude API."""
    return dict(_state)
