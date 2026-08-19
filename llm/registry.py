# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Реестр подключений к моделям ИИ (окно «Доступные модели» в настройках).

Каждая запись — паспорт подключения:
  id             — слаг-идентификатор (ссылка из приоритетных цепочек)
  name           — человекочитаемое имя для UI
  type           — "llm" (plain chat) | "api" (Claude-агент с инструментами)
  provider       — ollama | lmstudio | deepseek | anthropic
  base_url       — адрес сервера (для anthropic не используется)
  model          — имя модели
  api_key        — ключ API (deepseek; для локальных пусто)
  max_ctx_tokens — паспортный лимит контекста: по нему проактивный роутинг
                   решает, влезает ли сегмент в модель
  temperature, num_ctx, stream — параметры генерации (per-модель)
  max_concurrent — лимит одновременных запросов к подключению
                   (локальные — 1, чтобы не выгрызать слоты GPU)

Хранение: app_settings["llm_model_registry"] одним JSON-списком.
Семафоры создаются лениво и живут в памяти процесса.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re

logger = logging.getLogger(__name__)

REGISTRY_SETTING_KEY = "llm_model_registry"

ENTRY_PROVIDERS = ("ollama", "lmstudio", "deepseek", "anthropic")
ENTRY_TYPES     = ("llm", "api")

# Дефолтные паспорта контекста по провайдеру (для подсказок в UI)
DEFAULT_MAX_CTX = {
    "ollama":    16384,
    "lmstudio":  40960,
    "deepseek":  131072,
    "anthropic": 200000,
}

_entries: list[dict] = []
_semaphores: dict[str, asyncio.Semaphore] = {}


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "model"


def normalize_entry(raw: dict) -> dict:
    """Привести запись к каноническому виду с дефолтами (валидация форм и JSON)."""
    provider = str(raw.get("provider", "ollama")).strip()
    if provider not in ENTRY_PROVIDERS:
        provider = "ollama"
    etype = str(raw.get("type", "api" if provider == "anthropic" else "llm")).strip()
    if etype not in ENTRY_TYPES:
        etype = "llm"
    if provider == "anthropic":
        etype = "api"   # Claude всегда идёт через агентский пайплайн
    name = str(raw.get("name", "")).strip() or f"{provider}/{raw.get('model', '')}"
    entry_id = str(raw.get("id", "")).strip() or _slugify(name)
    try:
        max_ctx = int(raw.get("max_ctx_tokens") or DEFAULT_MAX_CTX[provider])
    except (TypeError, ValueError):
        max_ctx = DEFAULT_MAX_CTX[provider]
    try:
        max_conc = int(raw.get("max_concurrent") or 0)
    except (TypeError, ValueError):
        max_conc = 0
    if max_conc < 1:
        # локальные серверы по умолчанию строго последовательно
        max_conc = 1 if provider in ("ollama", "lmstudio") else 4
    try:
        temperature = float(raw.get("temperature", 0.1))
    except (TypeError, ValueError):
        temperature = 0.1
    try:
        num_ctx = int(raw.get("num_ctx") or 16384)
    except (TypeError, ValueError):
        num_ctx = 16384
    return {
        "id":             entry_id,
        "name":           name,
        "type":           etype,
        "provider":       provider,
        "base_url":       str(raw.get("base_url", "")).strip().rstrip("/"),
        "model":          str(raw.get("model", "")).strip(),
        "api_key":        str(raw.get("api_key", "")).strip(),
        "max_ctx_tokens": max_ctx,
        "temperature":    temperature,
        "num_ctx":        num_ctx,
        "stream":         bool(raw.get("stream", True)),
        "max_concurrent": max_conc,
    }


# ── Доступ к записям ───────────────────────────────────────────────────────────

def get_entries() -> list[dict]:
    """Все записи реестра (копия, в порядке добавления)."""
    return [dict(e) for e in _entries]


def get_entry(entry_id: str) -> dict | None:
    for e in _entries:
        if e["id"] == entry_id:
            return dict(e)
    return None


def upsert_entry(raw: dict) -> dict:
    """Добавить или обновить запись; возвращает нормализованную запись."""
    entry = normalize_entry(raw)
    for i, e in enumerate(_entries):
        if e["id"] == entry["id"]:
            _entries[i] = entry
            _semaphores.pop(entry["id"], None)   # пересоздать с новым лимитом
            logger.info("llm/registry: обновлена запись «%s» (%s)", entry["name"], entry["id"])
            return entry
    _entries.append(entry)
    logger.info("llm/registry: добавлена запись «%s» (%s)", entry["name"], entry["id"])
    return entry


def delete_entry(entry_id: str) -> bool:
    global _entries
    before = len(_entries)
    _entries = [e for e in _entries if e["id"] != entry_id]
    _semaphores.pop(entry_id, None)
    if len(_entries) < before:
        logger.info("llm/registry: удалена запись %s", entry_id)
        return True
    return False


def get_semaphore(entry_id: str) -> asyncio.Semaphore:
    """Семафор подключения (лимит одновременных запросов max_concurrent)."""
    if entry_id not in _semaphores:
        entry = get_entry(entry_id)
        limit = entry["max_concurrent"] if entry else 1
        _semaphores[entry_id] = asyncio.Semaphore(limit)
    return _semaphores[entry_id]


# ── Сериализация / загрузка ────────────────────────────────────────────────────

def serialize() -> str:
    return json.dumps(_entries, ensure_ascii=False)


def load_registry(json_str: str) -> int:
    """Загрузить реестр из JSON (app_settings). Возвращает число записей."""
    global _entries
    try:
        raw_list = json.loads(json_str) if json_str else []
    except json.JSONDecodeError:
        logger.error("llm/registry: битый JSON в настройках — реестр не загружен")
        raw_list = []
    _entries = [normalize_entry(r) for r in raw_list if isinstance(r, dict)]
    _semaphores.clear()
    return len(_entries)


def migrate_from_legacy(llm_cfg: dict, claude_cfg: dict) -> None:
    """Первый запуск с пустым реестром: создать записи из старых настроек.

    - локальная модель из llm_* (Ollama/LM Studio)
    - Claude API из claude_* настроек
    Старые ключи не трогаем — они продолжают работать для задач без цепочек.
    """
    if _entries:
        return
    provider = llm_cfg.get("provider", "ollama")
    upsert_entry({
        "id":             "local-default",
        "name":           f"Локальная LLM ({provider})",
        "type":           "llm",
        "provider":       provider,
        "base_url":       llm_cfg.get("base_url", ""),
        "model":          llm_cfg.get("model", ""),
        "max_ctx_tokens": llm_cfg.get("num_ctx") if provider == "ollama" else DEFAULT_MAX_CTX["lmstudio"],
        "temperature":    llm_cfg.get("temperature", 0.1),
        "num_ctx":        llm_cfg.get("num_ctx", 16384),
        "stream":         llm_cfg.get("stream", True),
    })
    upsert_entry({
        "id":             "claude-api",
        "name":           "Claude API",
        "type":           "api",
        "provider":       "anthropic",
        "model":          claude_cfg.get("model", ""),
        "max_ctx_tokens": DEFAULT_MAX_CTX["anthropic"],
    })
    logger.info("llm/registry: миграция из legacy-настроек — создано %d записи", len(_entries))
