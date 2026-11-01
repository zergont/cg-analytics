# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Справочное из реплики телеметрии: каталог регистров и имена объектов.

Каталог общий у всех машин типа и меняется только с прошивкой декодера — кэш на
час (как get_catalog_addrs). Имена объектов (таблица objects) — кэш 10 минут.
Читается тем же пулом source-БД, что у движка (analytics.source), но в своём
процессе — пул свой.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

_CATALOG_TTL = 3600.0
_OBJECTS_TTL = 600.0
_catalog: dict[str, tuple[float, dict[int, dict[str, Any]]]] = {}
_objects: tuple[float, dict[str, str]] | None = None


def _states(v: Any) -> Any:
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return None
    return v


async def get_catalog(equip_type: str) -> dict[int, dict[str, Any]]:
    """{addr: {name_ru, name, unit, kind, states}} для типа оборудования."""
    now = time.monotonic()
    c = _catalog.get(equip_type)
    if c and now - c[0] < _CATALOG_TTL:
        return c[1]
    from analytics import source as _src
    from db import remote
    out: dict[int, dict[str, Any]] = {}
    try:
        async with remote.connection(_src._get_pool()) as conn:
            rows = await conn.fetch("""
                SELECT addr, name_default, name_ru, unit_default, register_kind, states_json
                FROM register_catalog WHERE equip_type = $1 ORDER BY addr
            """, equip_type)
        for r in rows:
            out[int(r["addr"])] = {
                "name_ru": r["name_ru"], "name": r["name_default"],
                "unit": r["unit_default"], "kind": r["register_kind"],
                "states": _states(r["states_json"]),
            }
        _catalog[equip_type] = (now, out)
    except Exception:
        logger.warning("Каталог регистров %s не прочитан", equip_type, exc_info=True)
        if c:
            return c[1]
    return out


async def get_object_names() -> dict[str, str]:
    """{router_sn: имя объекта} из таблицы objects телеметрии; при сбое — {}."""
    global _objects
    now = time.monotonic()
    if _objects and now - _objects[0] < _OBJECTS_TTL:
        return _objects[1]
    names: dict[str, str] = {}
    try:
        from analytics import source as _src
        from db import remote
        async with remote.connection(_src._get_pool()) as conn:
            rows = await conn.fetch("SELECT router_sn, name FROM objects")
        names = {r["router_sn"]: (r["name"] or "").strip() for r in rows if (r["name"] or "").strip()}
        _objects = (now, names)
    except Exception:
        logger.debug("Имена объектов не прочитаны", exc_info=True)
        if _objects:
            return _objects[1]
    return names


def enum_text(states: Any, value: Any) -> str | None:
    """Метка состояния по states_json каталога: labels_ru, затем labels."""
    if not isinstance(states, dict) or value is None:
        return None
    key = str(int(value)) if isinstance(value, (int, float)) and float(value).is_integer() else str(value)
    for k in ("labels_ru", "labels"):
        d = states.get(k)
        if isinstance(d, dict) and key in d:
            return str(d[key])
    return None


def bit_info(states: Any, bit: int) -> dict[str, Any] | None:
    """Описание бита маски неисправностей: {name, name_ru, severity}."""
    if not isinstance(states, dict):
        return None
    d = states.get(str(int(bit)))
    return d if isinstance(d, dict) else None
