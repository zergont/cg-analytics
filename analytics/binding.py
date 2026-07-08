# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Резолвинг конфигурационных слоёв KB по привязке оборудования.

Единая точка, которая по записи реестра (controller_id / engine_id / kb_path)
строит список слоёв конфига и собирает AnalyticsConfig + FaultRef.

Два режима (в порядке приоритета):
  1. Пара «контроллер × двигатель» (controller_id и engine_id заданы):
        <kb_root>/_defaults  →  <kb_root>/controllers/<controller_id>
                             →  <kb_root>/engines/<engine_id>
  2. Legacy — монолитная папка (задан только kb_path):
        <kb_root>/equipment/<kb_path>

Так старые записи реестра продолжают работать, а новое оборудование
подключается парой без дублирования контроллерного слоя.
"""
from __future__ import annotations

from pathlib import Path

from .config import AnalyticsConfig
from .fault_ref import FaultRef


class BindingError(ValueError):
    """Привязка оборудования не позволяет собрать конфиг (нет ни пары, ни kb_path)."""


def resolve_layer_dirs(
    kb_root: str | Path,
    *,
    controller_id: str | None = None,
    engine_id: str | None = None,
    kb_path: str | None = None,
) -> list[Path]:
    """Вернуть слои конфига в порядке ВОЗРАСТАНИЯ приоритета (для deep-merge).

    Пустой список означает, что привязки нет — вызывающий код решает,
    считать это ошибкой или пропуском.
    """
    root = Path(kb_root)

    if controller_id and engine_id:
        layers: list[Path] = []
        defaults = root / AnalyticsConfig.DEFAULTS_DIRNAME
        if defaults.is_dir():
            layers.append(defaults)
        layers.append(root / "controllers" / controller_id)
        layers.append(root / "engines" / engine_id)
        return layers

    if kb_path:
        return [root / "equipment" / kb_path]

    return []


def describe_binding(
    *,
    controller_id: str | None = None,
    engine_id: str | None = None,
    kb_path: str | None = None,
) -> str:
    """Короткая человекочитаемая метка привязки для логов и ошибок."""
    if controller_id and engine_id:
        return f"pair({controller_id} × {engine_id})"
    if kb_path:
        return f"legacy({kb_path})"
    return "none"


def build_config(
    kb_root: str | Path,
    *,
    controller_id: str | None = None,
    engine_id: str | None = None,
    kb_path: str | None = None,
) -> AnalyticsConfig:
    """Собрать AnalyticsConfig по привязке. Бросает BindingError, если привязки нет."""
    dirs = resolve_layer_dirs(
        kb_root, controller_id=controller_id, engine_id=engine_id, kb_path=kb_path
    )
    if not dirs:
        raise BindingError(
            "Не задана привязка конфига: нужна пара (controller_id, engine_id) "
            "или legacy kb_path."
        )
    return AnalyticsConfig(layers=dirs)


def build_fault_ref(
    kb_root: str | Path,
    *,
    controller_id: str | None = None,
    engine_id: str | None = None,
    kb_path: str | None = None,
) -> FaultRef:
    """Собрать FaultRef по привязке.

    Справочник ищется от приоритетного слоя к базовому (двигатель → контроллер
    → _defaults), берётся первый найденный. Для пары реальный файл лежит в слое
    контроллера; двигательный слой может его переопределить.
    """
    dirs = resolve_layer_dirs(
        kb_root, controller_id=controller_id, engine_id=engine_id, kb_path=kb_path
    )
    if not dirs:
        raise BindingError(
            "Не задана привязка конфига: нужна пара (controller_id, engine_id) "
            "или legacy kb_path."
        )
    return FaultRef(search_paths=list(reversed(dirs)))
