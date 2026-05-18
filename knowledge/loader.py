"""Загрузка карт регистров из knowledge_base в память."""
import json
import logging
from pathlib import Path
from typing import Any

from config import settings

logger = logging.getLogger(__name__)

# Кэш в памяти: (manufacturer, model) → knowledge dict
_cache: dict[tuple[str, str], dict] = {}


def load_knowledge(manufacturer: str, model: str) -> dict[str, Any]:
    """Загрузить карты регистров для модели оборудования.

    Returns:
        {
            "register_map": {addr: {...}},       # dict, ключ — int(addr)
            "fault_bitmap_map": {addr: [...]},   # dict, ключ — int(addr)
            "enum_map": {"holding:addr": {...}}, # dict
            "base_path": Path,
        }
    """
    cache_key = (manufacturer.lower(), model.lower())
    if cache_key in _cache:
        return _cache[cache_key]

    base_path = settings.knowledge_base_path / "equipment" / manufacturer / model
    if not base_path.exists():
        raise FileNotFoundError(
            f"Knowledge base не найдена: {base_path}\n"
            f"Создайте папку и добавьте register_map.jsonl, fault_bitmap_map.jsonl, enum_map.json"
        )

    register_map = _load_register_map(base_path / "register_map.jsonl")
    fault_bitmap_map = _load_fault_bitmap_map(base_path / "fault_bitmap_map.jsonl")
    enum_map = _load_enum_map(base_path / "enum_map.json")

    result = {
        "register_map": register_map,
        "fault_bitmap_map": fault_bitmap_map,
        "enum_map": enum_map,
        "base_path": base_path,
    }
    _cache[cache_key] = result

    logger.info(
        "Knowledge base загружена: %s/%s | регистров=%d | fault-битов=%d",
        manufacturer, model,
        len(register_map),
        sum(len(v) for v in fault_bitmap_map.values()),
    )
    return result


def invalidate_cache(manufacturer: str | None = None, model: str | None = None) -> None:
    """Сбросить кэш (вызвать после переиндексации)."""
    if manufacturer and model:
        _cache.pop((manufacturer.lower(), model.lower()), None)
    else:
        _cache.clear()


def _load_register_map(path: Path) -> dict[int, dict]:
    """Карта регистров. Ключ — int(addr)."""
    if not path.exists():
        logger.warning("register_map.jsonl не найден: %s", path)
        return {}

    result: dict[int, dict] = {}
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                result[int(rec["addr"])] = rec
            except (json.JSONDecodeError, KeyError, ValueError) as e:
                logger.warning("Ошибка в register_map.jsonl: %s | %s", line[:60], e)
    return result


def _load_fault_bitmap_map(path: Path) -> dict[int, list[dict]]:
    """Карта fault-битов. Ключ — int(addr), значение — список битовых дескрипторов."""
    if not path.exists():
        logger.warning("fault_bitmap_map.jsonl не найден: %s", path)
        return {}

    result: dict[int, list[dict]] = {}
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                addr = int(rec["addr"])
                result.setdefault(addr, []).append(rec)
            except (json.JSONDecodeError, KeyError, ValueError) as e:
                logger.warning("Ошибка в fault_bitmap_map.jsonl: %s | %s", line[:60], e)
    return result


def _load_enum_map(path: Path) -> dict[str, dict]:
    """Карта enum-значений. Ключ — 'holding:addr'."""
    if not path.exists():
        logger.warning("enum_map.json не найден: %s", path)
        return {}
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Ошибка в enum_map.json: %s", e)
        return {}
