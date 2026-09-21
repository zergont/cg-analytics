# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль веб-интерфейса и API
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Представление сегмента для API: разбор JSONB и окраска.

Вынесено из web/routes.py, чтобы правила окраски проверялись без поднятия
FastAPI — это чистые функции над тем, что лежит в auto_segments.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _parse_json(val, default=None, ctx: str = ""):
    """JSONB из asyncpg может прийти строкой — распарсить, при ошибке вернуть default."""
    import json as _json
    if val is None:
        return default
    if isinstance(val, str):
        try:
            return _json.loads(val)
        except Exception:
            logger.warning("Битый JSON%s", f" ({ctx})" if ctx else "")
            return default
    return val


def _seg_collect_dets(chars_json: Any, active_dets_json: Any = None) -> list[dict]:
    """Все детекции сегмента: закрытый — из characteristics_json, открытый — из active_detections_json."""
    dets: list[dict] = []
    ch = _parse_json(chars_json, {}, ctx="characteristics_json") or {}
    if isinstance(ch, dict):
        for sub in ch.get("subsegments", []):
            dets.extend(sub.get("detections", []))
    if not dets and active_dets_json:
        dets = _parse_json(active_dets_json, [], ctx="active_detections_json") or []
    return [d for d in dets if isinstance(d, dict)]


def _seg_active_dets(chars_json: Any, active_dets_json: Any = None) -> list[dict]:
    """Детекции, висящие на конец сегмента: последний подсегмент, снятое убрано.

    Зеркало online.engine._extract_open_segment_data: маска со снятым фронтом
    (`values.fault_end`) — исторический факт для отчёта, но не активная тревога.
    """
    ch = _parse_json(chars_json, {}, ctx="characteristics_json") or {}
    dets: list[dict] = []
    if isinstance(ch, dict):
        subs = ch.get("subsegments") or []
        if subs and isinstance(subs[-1], dict):
            dets = list(subs[-1].get("detections") or [])
    if not dets and active_dets_json:
        dets = _parse_json(active_dets_json, [], ctx="active_detections_json") or []
    return [
        d for d in dets
        if isinstance(d, dict)
        and not (d.get("scenario") == "CONTROLLER_FAULT"
                 and (d.get("values") or {}).get("fault_end"))
    ]


def _seg_gate_checked(dets: list[dict], gate_suppressed_hash: str | None) -> bool:
    """Действует ли вердикт гейта «отменить» (срабатывание проверено ИИ).

    Вердикт касается ТОЛЬКО аналитики: вынести его гейт может лишь при чистой
    панели (`can_cancel` требует panel_severity == «норма»). Но хеш считается
    по составу аналитических детекций, и если ПОСЛЕ вердикта поднялась маска
    панели, состав не изменился — вердикт продолжал действовать, и сегмент
    получал пометку «проверено ИИ, угрозы нет» при живой аварии.

    Наблюдалось 21.09: сегмент 35508, активная маска SHUTDOWN, а плашка в
    календаре жёлтая с щитом. Поэтому при активном сигнале панели тяжести
    WARNING или выше вердикт считается недействительным.
    """
    from online.status_assembler import compute_analytics_hash
    panel_loud = any(
        d.get("scenario") == "CONTROLLER_FAULT"
        and d.get("severity") in ("SHUTDOWN", "WARNING")
        for d in dets
    )
    if panel_loud:
        return False
    return bool(
        gate_suppressed_hash and dets
        and compute_analytics_hash(dets) == gate_suppressed_hash
    )


def _seg_severity(
    chars_json: Any, active_dets_json: Any = None,
    gate_suppressed_hash: str | None = None,
    stop_kind: str | None = None,
) -> str | None:
    """Severity сегмента для API — значение поля `severity` в ответе.

    Шкала (v4.8.9+):
      "SHUTDOWN" — панель: аварийный останов
      "WARNING"  — панель: тревога (derate / панельный warning)
      "CAUTION"  — детекция аналитического движка  (до v4.8.9 называлось "INFO")
      None       — детекций нет

    Аналитика, отменённая гейтом Claude (gate_suppressed_hash совпал с хешем
    текущего состава детекций), не учитывается → severity может стать None
    даже при наличии детекций в characteristics_json.

    Стоп-сегмент красится не по истории, а по тому, что висит (v4.9.73) —
    в режиме 0 панель копит сообщения, не меняя режим, и максимум за период
    оставил бы стоянку красной до конца суток:
      EMERGENCY — всегда SHUTDOWN: снятие аварии и есть граница этого
                  сегмента, цвет держится до неё;
      SIMPLE    — по активным на конец окна: у открытого стопа цвет живой и
                  идёт вслед за панелью, у закрытого застывает последним.
    Остальные режимы — по самому серьёзному сообщению за весь сегмент.
    """
    if stop_kind == "EMERGENCY":
        return "SHUTDOWN"
    all_dets = _seg_collect_dets(chars_json, active_dets_json)
    suppressed = _seg_gate_checked(all_dets, gate_suppressed_hash)
    dets = (_seg_active_dets(chars_json, active_dets_json)
            if stop_kind == "SIMPLE" else all_dets)
    if suppressed:
        dets = [d for d in dets if d.get("scenario") == "CONTROLLER_FAULT"]
    if not dets:
        return None

    panel_rank = {"SHUTDOWN": 4, "WARNING": 3}
    panel = [d.get("severity", "") for d in dets if d.get("scenario") == "CONTROLLER_FAULT"]
    best_panel = max(panel, key=lambda s: panel_rank.get(s, 0), default="")
    if panel_rank.get(best_panel, 0) >= 3:
        return best_panel
    if any(d.get("scenario") != "CONTROLLER_FAULT" for d in dets):
        return "CAUTION"
    return None
