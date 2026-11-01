# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Карта установок: публичный API дерева и формы карточки «Установки» в
настройках (v5.2.0, MCP Э1; решения Р7–Р8 design_mcp_telemetry.md).

GET /api/units — дерево объект → установки → панель + датчики (для UI-telemetry
и сервера телеметрии; серийники роутеров здесь есть — диспетчерскому UI они
нужны, в ответы инструментов они не попадают).
POST /settings/units/* — правки только руками владельца.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Form
from fastapi.responses import JSONResponse, RedirectResponse

from db import units as _units

logger = logging.getLogger(__name__)

router = APIRouter()


def _panel_types() -> frozenset[str]:
    try:
        from telemetry_mcp.config import load
        return load().panel_types
    except Exception:  # noqa: BLE001
        return _units.DEFAULT_PANEL_TYPES


async def _object_names() -> dict[str, str]:
    try:
        from telemetry_mcp.catalog import get_object_names
        return await get_object_names()
    except Exception:  # noqa: BLE001
        return {}


@router.get("/api/units", response_class=JSONResponse)
async def api_units(ensure: int = 0):
    """Дерево установок. ensure=1 — сперва завести недостающие установки по реестру."""
    tree = await _units.get_tree(panel_types=_panel_types(), object_names=await _object_names(),
                                 ensure=bool(ensure))
    return JSONResponse(tree)


@router.post("/settings/units/ensure")
async def units_ensure():
    await _units.get_tree(panel_types=_panel_types(), ensure=True)
    return RedirectResponse(url="/settings#units", status_code=303)


@router.post("/settings/units/update")
async def units_update(
    unit_id: int = Form(...),
    name: str = Form(""),
    notes: str = Form(""),
    active: str = Form("on"),
):
    ok = await _units.update_unit(int(unit_id), name=name.strip() or None, notes=notes.strip() or None,
                                  active=(active == "on"))
    if not ok:
        logger.warning("Установка #%s не найдена при правке карточки", unit_id)
    return RedirectResponse(url="/settings#units", status_code=303)


@router.post("/settings/units/create")
async def units_create(
    router_sn: str = Form(...),
    panel_type: str = Form(...),
    panel_id: int = Form(...),
    name: str = Form(""),
):
    await _units.upsert_unit(router_sn, panel_type, int(panel_id), name=name.strip() or None)
    return RedirectResponse(url="/settings#units", status_code=303)


@router.post("/settings/units/delete")
async def units_delete(unit_id: int = Form(...)):
    await _units.delete_unit(int(unit_id))
    return RedirectResponse(url="/settings#units", status_code=303)


@router.post("/settings/units/sensor")
async def units_sensor(
    router_sn: str = Form(...),
    equip_type: str = Form(...),
    panel_id: int = Form(...),
    unit_id: str = Form(""),      # id установки; "" — не привязан; "default" — правило по умолчанию
    role: str = Form(""),
    name: str = Form(""),
):
    if unit_id.strip().lower() == "default":
        await _units.clear_sensor(router_sn, equip_type, int(panel_id))
    else:
        uid = int(unit_id) if unit_id.strip() else None
        await _units.set_sensor(router_sn, equip_type, int(panel_id), unit_id=uid,
                                role=role.strip() or None, name=name.strip() or None)
    return RedirectResponse(url="/settings#units", status_code=303)
