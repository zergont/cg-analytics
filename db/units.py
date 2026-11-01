# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Карта установок (v5.2.0, MCP Э1; docs/plans/design_mcp_telemetry.md, Р6–Р8).

Иерархия данных владельца: роутер (объект) → генераторная установка → у каждой
обязательно панель, кроме панели — свои датчики (анализ масла и т. п.).

В БД телеметрии уровня «установка» нет: датчик — отдельная строка оборудования со
своим equip_type, связь с панелью не записана. Карта живёт в аналитике (Р7):
таблицы `units` (установка = панель реестра `equipment_registry` типа панели) и
`unit_sensors` (явные решения владельца о датчиках). Чего в них нет — правило по
умолчанию: датчик того же router_sn с тем же panel_id принадлежит этой панели;
непривязанные датчики видны в дереве объекта как «не привязаны».

Привязка KB (controller_id/engine_id/kb_path) остаётся у панели в
equipment_registry — здесь её нет.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Типы оборудования, которые считаются панелью установки (остальные — датчики).
# Переопределяется конфигом сервера телеметрии (telemetry_mcp.panel_types).
DEFAULT_PANEL_TYPES: frozenset[str] = frozenset({"pcc"})


async def _connect():
    from db.pool import acquire_analytics
    return await acquire_analytics()


_missing_warned = False


def _tables_missing(exc: Exception) -> bool:
    """Таблиц карты ещё нет (cg-analytics не перезапущен после обновления схемы):
    карта работает по правилу умолчания, предупреждение — один раз."""
    global _missing_warned
    import asyncpg
    if not isinstance(exc, asyncpg.UndefinedTableError):
        return False
    if not _missing_warned:
        _missing_warned = True
        logger.warning("Таблиц карты установок (units/unit_sensors) ещё нет — карта по правилу "
                       "умолчания; их создаст cg-analytics при старте (db/online_schema.sql)")
    return True


# ── Чтение ───────────────────────────────────────────────────────────────────

async def list_units() -> list[dict[str, Any]]:
    conn = await _connect()
    try:
        rows = await conn.fetch("""
            SELECT id, router_sn, name, panel_type, panel_id, notes, active,
                   created_at, updated_at
            FROM units ORDER BY router_sn, panel_type, panel_id
        """)
        return [dict(r) for r in rows]
    except Exception as e:  # noqa: BLE001
        if _tables_missing(e):
            return []
        raise
    finally:
        await conn.close()


async def list_unit_sensors() -> list[dict[str, Any]]:
    conn = await _connect()
    try:
        rows = await conn.fetch("""
            SELECT router_sn, equip_type, panel_id, unit_id, role, name, updated_at
            FROM unit_sensors ORDER BY router_sn, equip_type, panel_id
        """)
        return [dict(r) for r in rows]
    except Exception as e:  # noqa: BLE001
        if _tables_missing(e):
            return []
        raise
    finally:
        await conn.close()


# ── Запись (только руками владельца через карточку UI) ───────────────────────

async def upsert_unit(router_sn: str, panel_type: str, panel_id: int, *,
                      name: str | None = None, notes: str | None = None,
                      active: bool | None = None) -> int:
    """Создать установку для панели или обновить имя/примечание/активность.
    None — поле не трогать (у новой строки — умолчания). Возвращает id."""
    conn = await _connect()
    try:
        row = await conn.fetchrow("""
            INSERT INTO units (router_sn, panel_type, panel_id, name, notes, active)
            VALUES ($1, $2, $3, $4, $5, COALESCE($6, true))
            ON CONFLICT (router_sn, panel_type, panel_id) DO UPDATE SET
                name       = COALESCE(EXCLUDED.name,   units.name),
                notes      = COALESCE(EXCLUDED.notes,  units.notes),
                active     = COALESCE($6,              units.active),
                updated_at = now()
            RETURNING id
        """, router_sn, panel_type, int(panel_id), name, notes, active)
        return int(row["id"])
    finally:
        await conn.close()


async def update_unit(unit_id: int, *, name: str | None, notes: str | None,
                      active: bool) -> bool:
    """Правка карточки: имя и примечание пишутся как есть (пустое → NULL)."""
    conn = await _connect()
    try:
        status = await conn.execute("""
            UPDATE units SET name = $2, notes = $3, active = $4, updated_at = now()
            WHERE id = $1
        """, int(unit_id), name or None, notes or None, bool(active))
        return status != "UPDATE 0"
    finally:
        await conn.close()


async def delete_unit(unit_id: int) -> bool:
    """Удалить явную запись установки: панель вернётся к правилу по умолчанию
    (установка без имени владельца), её явные датчики станут «не привязаны»."""
    conn = await _connect()
    try:
        status = await conn.execute("DELETE FROM units WHERE id = $1", int(unit_id))
        return status != "DELETE 0"
    finally:
        await conn.close()


async def set_sensor(router_sn: str, equip_type: str, panel_id: int, *,
                     unit_id: int | None, role: str | None = None,
                     name: str | None = None) -> None:
    """Явное решение о датчике: unit_id — к какой установке, None — не привязан."""
    conn = await _connect()
    try:
        await conn.execute("""
            INSERT INTO unit_sensors (router_sn, equip_type, panel_id, unit_id, role, name)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (router_sn, equip_type, panel_id) DO UPDATE SET
                unit_id = EXCLUDED.unit_id, role = EXCLUDED.role, name = EXCLUDED.name,
                updated_at = now()
        """, router_sn, equip_type, int(panel_id), unit_id, role or None, name or None)
    finally:
        await conn.close()


async def clear_sensor(router_sn: str, equip_type: str, panel_id: int) -> None:
    """Снять явное решение: датчик снова привязывается правилом по умолчанию."""
    conn = await _connect()
    try:
        await conn.execute("""
            DELETE FROM unit_sensors WHERE router_sn = $1 AND equip_type = $2 AND panel_id = $3
        """, router_sn, equip_type, int(panel_id))
    finally:
        await conn.close()


async def ensure_units(registry: list[dict[str, Any]], units: list[dict[str, Any]],
                       panel_types: frozenset[str] = DEFAULT_PANEL_TYPES) -> int:
    """Завести строки units для панелей реестра, у которых их ещё нет (имя — из
    реестра). Идемпотентно; возвращает число созданных. Нужно, чтобы у каждой
    установки был постоянный id для инструментов и карточки."""
    have = {(u["router_sn"], u["panel_type"], int(u["panel_id"])) for u in units}
    n = 0
    for eq in registry:
        key = (eq["router_sn"], eq["equip_type"], int(eq["panel_id"]))
        if eq["equip_type"] not in panel_types or key in have:
            continue
        await upsert_unit(key[0], key[1], key[2], name=eq.get("name") or None)
        have.add(key)
        n += 1
    return n


# ── Дерево объект → установки → панель + датчики (чистая функция) ────────────

def _triple(d: dict[str, Any]) -> tuple[str, str, int]:
    return d["router_sn"], d["equip_type"], int(d["panel_id"])


def _binding_label(eq: dict[str, Any]) -> str | None:
    if eq.get("controller_id") and eq.get("engine_id"):
        return f"{eq['controller_id']} × {eq['engine_id']}"
    return eq.get("kb_path") or None


def build_tree(
    registry: list[dict[str, Any]],
    units: list[dict[str, Any]],
    sensors: list[dict[str, Any]],
    *,
    panel_types: frozenset[str] = DEFAULT_PANEL_TYPES,
    object_names: dict[str, str] | None = None,
    freshness: dict[tuple[str, str, int], Any] | None = None,
    observation_status: dict[tuple[str, str, int], Any] | None = None,
) -> dict[str, Any]:
    """Собрать дерево из реестра аналитики, явных установок и явных решений о
    датчиках. Чистая функция — без БД, проверяется тестами.

    registry — строки equipment_registry; units — строки units; sensors — строки
    unit_sensors; object_names — {router_sn: имя объекта} из телеметрии (не
    обязательно); freshness — {тройка: last_data_ts} наблюдений движка;
    observation_status — {тройка: status} строк online_observations (v5.2.1:
    «наблюдение остановлено» у соседа в картине объекта; нет строки — None).

    Возвращает {"objects": [{router_sn, name, units: [...], unattached: [...]}]}.
    Установка без строки units (ещё не материализована) идёт с id None и именем
    из реестра. Соседи установки — другие активные установки того же объекта.
    """
    object_names = object_names or {}
    freshness = freshness or {}
    observation_status = observation_status or {}
    unit_rows = {(u["router_sn"], u["panel_type"], int(u["panel_id"])): u for u in units}
    units_by_id = {int(u["id"]): u for u in units if u.get("id") is not None}
    explicit = {_triple(s): s for s in sensors}

    by_object: dict[str, dict[str, Any]] = {}

    def obj(router_sn: str) -> dict[str, Any]:
        o = by_object.get(router_sn)
        if o is None:
            o = by_object[router_sn] = {
                "router_sn": router_sn,
                "name": object_names.get(router_sn) or router_sn,
                "units": [], "unattached": [],
            }
        return o

    # 1. Панели → установки
    unit_index: dict[tuple[str, str, int], dict[str, Any]] = {}
    for eq in registry:
        key = _triple(eq)
        if eq["equip_type"] not in panel_types:
            continue
        u = unit_rows.get(key)
        last = freshness.get(key)
        unit = {
            "id": int(u["id"]) if u else None,
            "name": (u.get("name") if u else None) or eq.get("name") or f"{eq['equip_type']}/{eq['panel_id']}",
            "object": object_names.get(eq["router_sn"]) or eq["router_sn"],
            "router_sn": eq["router_sn"],
            "active": bool(u["active"]) if u else bool(eq.get("active", True)),
            "notes": (u.get("notes") if u else None) or None,
            "materialized": u is not None,
            "panel": {
                "equip_type": eq["equip_type"], "panel_id": int(eq["panel_id"]),
                "registry_name": eq.get("name"),
                "manufacturer": eq.get("manufacturer"), "model": eq.get("model"),
                "engine_sn": eq.get("engine_sn"),
                "binding": _binding_label(eq),
                "controller_id": eq.get("controller_id"), "engine_id": eq.get("engine_id"),
                "kb_path": eq.get("kb_path"),
                "analysis_active": bool(eq.get("active", True)),
                "last_data_ts": last.isoformat() if hasattr(last, "isoformat") else last,
                "observation": observation_status.get(key),
            },
            "sensors": [],
        }
        unit_index[key] = unit
        obj(eq["router_sn"])["units"].append(unit)

    # 2. Датчики: явное решение, иначе правило по умолчанию
    for eq in registry:
        key = _triple(eq)
        if eq["equip_type"] in panel_types:
            continue
        s = explicit.get(key)
        target: dict[str, Any] | None = None
        how = "default"
        if s is not None:
            how = "explicit"
            uid = s.get("unit_id")
            if uid is not None and int(uid) in units_by_id:
                ur = units_by_id[int(uid)]
                target = unit_index.get((ur["router_sn"], ur["panel_type"], int(ur["panel_id"])))
        else:
            for pt in panel_types:
                target = unit_index.get((eq["router_sn"], pt, int(eq["panel_id"])))
                if target is not None:
                    break
        last = freshness.get(key)
        sensor = {
            "router_sn": eq["router_sn"],
            "equip_type": eq["equip_type"], "panel_id": int(eq["panel_id"]),
            "name": (s.get("name") if s else None) or eq.get("name") or eq["equip_type"],
            "role": (s.get("role") if s else None),
            "manufacturer": eq.get("manufacturer"), "model": eq.get("model"),
            "attached": how,
            # v5.2.1 (В11): картина объекта во входе моделей — что меряет датчик
            # (слой KB его привязки) и «данных нет», если источник молчит
            # (свежесть датчика — из телеметрии, llm/object_picture.load_context)
            "controller_id": eq.get("controller_id"), "kb_path": eq.get("kb_path"),
            "last_data_ts": last.isoformat() if hasattr(last, "isoformat") else last,
            "observation": observation_status.get(key),
        }
        if target is not None:
            target["sensors"].append(sensor)
        else:
            obj(eq["router_sn"])["unattached"].append(sensor)

    # 3. Соседи
    for o in by_object.values():
        names = [u["name"] for u in o["units"] if u["active"]]
        for u in o["units"]:
            u["neighbors"] = [n for n in names if n != u["name"]]

    objects = sorted(by_object.values(), key=lambda o: (o["name"] or "", o["router_sn"]))
    for o in objects:
        o["units"].sort(key=lambda u: (u["panel"]["equip_type"], u["panel"]["panel_id"]))
    return {"objects": objects}


async def get_tree(*, panel_types: frozenset[str] = DEFAULT_PANEL_TYPES,
                   object_names: dict[str, str] | None = None,
                   ensure: bool = False) -> dict[str, Any]:
    """Дерево из БД аналитики. ensure=True — сперва завести недостающие строки
    units (материализация умолчаний; вызывается из карточки UI и при старте)."""
    from db import analytics as _an
    registry = await _an.get_equipment_registry()
    units = await list_units()
    if ensure:
        try:
            created = await ensure_units(registry, units, panel_types)
        except Exception as e:  # noqa: BLE001
            if not _tables_missing(e):
                raise
            created = 0
        if created:
            logger.info("Карта установок: заведено %d установок по реестру", created)
            units = await list_units()
    sensors = await list_unit_sensors()
    freshness: dict[tuple[str, str, int], Any] = {}
    status: dict[tuple[str, str, int], Any] = {}
    try:
        from online.db import list_observations
        for ob in await list_observations():
            k = (ob["router_sn"], ob["equip_type"], int(ob["panel_id"]))
            freshness[k] = ob.get("last_data_ts")
            status[k] = ob.get("status")
    except Exception:
        logger.debug("Карта установок: свежесть наблюдений не прочитана", exc_info=True)
    return build_tree(registry, units, sensors, panel_types=panel_types,
                      object_names=object_names, freshness=freshness, observation_status=status)


def flat_units(tree: dict[str, Any]) -> list[dict[str, Any]]:
    """Все установки дерева одним списком (для поиска по имени/id)."""
    out: list[dict[str, Any]] = []
    for o in tree.get("objects", []):
        out.extend(o.get("units", []))
    return out
