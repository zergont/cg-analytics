"""Загрузка карт регистров из knowledge_base в память."""
import json
import logging
from pathlib import Path
from typing import Any

from config import settings

logger = logging.getLogger(__name__)

# Кэш в памяти: kb_path → knowledge dict
_cache: dict[str, dict] = {}


def load_knowledge(kb_path: str) -> dict[str, Any]:
    """Загрузить карты регистров и правила для папки kb_path.

    Returns:
        {
            "register_map": {addr: {...}},       # dict, ключ — int(addr)
            "fault_bitmap_map": {addr: [...]},   # dict, ключ — int(addr)
            "enum_map": {"holding:addr": {...}}, # dict
            "operation_rules": {...},            # dict из operation_rules.json
            "base_path": Path,
        }
    """
    cache_key = kb_path.lower()
    if cache_key in _cache:
        return _cache[cache_key]

    base_path = settings.knowledge_base_path / "equipment" / kb_path
    if not base_path.exists():
        raise FileNotFoundError(
            f"Knowledge base не найдена: {base_path}\n"
            f"Создайте папку и добавьте register_map.jsonl, fault_bitmap_map.jsonl, enum_map.json"
        )

    register_map = _load_register_map(base_path / "register_map.jsonl")
    fault_bitmap_map = _load_fault_bitmap_map(base_path / "fault_bitmap_map.jsonl")
    enum_map = _load_enum_map(base_path / "enum_map.json")
    operation_rules = _load_operation_rules(base_path / "operation_rules.json")

    result = {
        "register_map": register_map,
        "fault_bitmap_map": fault_bitmap_map,
        "enum_map": enum_map,
        "operation_rules": operation_rules,
        "base_path": base_path,
    }
    _cache[cache_key] = result

    logger.info(
        "Knowledge base загружена: %s | регистров=%d | fault-битов=%d | правила=%s",
        kb_path,
        len(register_map),
        sum(len(v) for v in fault_bitmap_map.values()),
        "да" if operation_rules else "нет",
    )
    return result


def invalidate_cache(kb_path: str | None = None) -> None:
    """Сбросить кэш (вызвать после переиндексации)."""
    if kb_path:
        _cache.pop(kb_path.lower(), None)
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


def _load_operation_rules(path: Path) -> dict:
    """Структурированные правила эксплуатации из operation_rules.json."""
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Ошибка в operation_rules.json: %s", e)
        return {}
