# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""KB для установки: конфиг аналитики по привязке панели, справочник кодов,
разбор имён параметров («мощность», «ток L1», «давление масла», роль, адрес).

Конфиг собирается так же, как у движка (online/manager.py): слои
_defaults → controllers/<id> → engines/<id> по привязке из equipment_registry,
корень — settings.knowledge_base_path. Кэш на 10 минут (KB правят в веб-морде).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from analytics.labels import ROLE_LABELS, unit_ru

from .common import ToolError, UnitRef

logger = logging.getLogger(__name__)

_KB_TTL = 600.0
_cache: dict[tuple[str, str, int], tuple[float, Any, Any, str | None]] = {}

# Русские имена параметров → роль KB. Регистр не важен, «ё» = «е».
ROLE_ALIASES: dict[str, str] = {
    "мощность": "ACTIVE_POWER_TOTAL", "активная мощность": "ACTIVE_POWER_TOTAL",
    "p": "ACTIVE_POWER_TOTAL", "квт": "ACTIVE_POWER_TOTAL", "нагрузка квт": "ACTIVE_POWER_TOTAL",
    "реактивная мощность": "REACTIVE_POWER_TOTAL", "квар": "REACTIVE_POWER_TOTAL",
    "полная мощность": "APPARENT_POWER_TOTAL",
    "коэффициент мощности": "POWER_FACTOR", "cos": "POWER_FACTOR", "pf": "POWER_FACTOR",
    "частота": "FREQUENCY", "гц": "FREQUENCY", "f": "FREQUENCY",
    "задание частоты": "FREQ_REFERENCE", "задание": "FREQ_REFERENCE",
    "обороты": "RPM", "об/мин": "RPM", "rpm": "RPM",
    "нагрузка": "LOAD_PCT", "нагрузка %": "LOAD_PCT", "процент нагрузки": "LOAD_PCT",
    "температура ож": "COOLANT_TEMP", "ож": "COOLANT_TEMP", "охлаждающая жидкость": "COOLANT_TEMP",
    "температура охлаждающей жидкости": "COOLANT_TEMP", "антифриз": "COOLANT_TEMP",
    "температура масла": "OIL_TEMP", "масло температура": "OIL_TEMP",
    "давление масла": "OIL_PRESS", "масло": "OIL_PRESS", "давление": "OIL_PRESS",
    "акб": "BATTERY_V", "батарея": "BATTERY_V", "напряжение акб": "BATTERY_V", "аккумулятор": "BATTERY_V",
    "питание эбу": "ECM_BATTERY_V", "эбу": "ECM_BATTERY_V",
    "температура воздуха на впуске": "INTAKE_MANIFOLD_TEMP", "впуск": "INTAKE_MANIFOLD_TEMP",
    "наддув": "BOOST_PRESSURE", "давление наддува": "BOOST_PRESSURE",
    "расход топлива": "FUEL_RATE", "топливо": "FUEL_RATE",
    "напряжение": "VOLTAGE_LL_AVG", "линейное напряжение": "VOLTAGE_LL_AVG", "u": "VOLTAGE_LL_AVG",
    "ток": "CURRENT_AVG", "средний ток": "CURRENT_AVG", "i": "CURRENT_AVG",
    "ток l1": "CURRENT_L1", "ток a": "CURRENT_L1", "i l1": "CURRENT_L1", "l1": "CURRENT_L1",
    "ток l2": "CURRENT_L2", "ток b": "CURRENT_L2", "i l2": "CURRENT_L2", "l2": "CURRENT_L2",
    "ток l3": "CURRENT_L3", "ток c": "CURRENT_L3", "i l3": "CURRENT_L3", "l3": "CURRENT_L3",
    "мощность l1": "ACTIVE_POWER_L1", "мощность l2": "ACTIVE_POWER_L2", "мощность l3": "ACTIVE_POWER_L3",
    "наработка": "ENGINE_HOURS", "моточасы": "ENGINE_HOURS", "часы": "ENGINE_HOURS",
    "попытки пуска": "START_ATTEMPTS_TOTAL", "успешные пуски": "START_SUCCESS_TOTAL",
    "автомат": "GENSET_CB_POS", "выключатель генератора": "GENSET_CB_POS", "автомат генератора": "GENSET_CB_POS",
    "режим": "RUN_STATE", "состояние": "RUN_STATE", "режим работы": "RUN_STATE",
    "переключатель": "SWITCH_POS", "положение переключателя": "SWITCH_POS", "выкл/авто/ручной": "SWITCH_POS",
    "команда": "RUN_COMMAND", "команда пуска": "RUN_COMMAND", "команда останова": "RUN_COMMAND",
    "сброс": "FAULT_RESET", "сброс неисправностей": "FAULT_RESET",
    "тип последней неисправности": "LAST_FAULT_TYPE", "последняя неисправность": "LAST_FAULT_TYPE",
}


# Короткие русские имена ролей-состояний (в ROLE_LABELS их нет; описания KB длинные)
EXTRA_ROLE_LABELS: dict[str, str] = {
    "RUN_STATE": "Режим работы", "SWITCH_POS": "Переключатель управления",
    "RUN_COMMAND": "Команда работы панели", "FAULT_RESET": "Сброс неисправностей",
    "GENSET_CB_POS": "Автомат генератора", "GENSET_CB_CLOSE_CMD": "Команда включения автомата",
    "LAST_FAULT_TYPE": "Тип последней неисправности", "LAST_FAULT_CODE": "Код последней неисправности",
    "COMMON_SHUTDOWN": "Общий аварийный останов", "FUEL_SOLENOID": "Топливный электромагнит",
    "EXCITATION": "Возбуждение генератора", "READY_TO_LOAD": "Готовность к нагрузке",
    "PARALLEL_READY": "Готовность к параллельной работе",
}


def role_label_ru(role: str | None) -> str | None:
    if not role:
        return None
    return ROLE_LABELS.get(role) or EXTRA_ROLE_LABELS.get(role)


@dataclass
class ParamRef:
    addr: int
    role: str | None
    label: str          # по-русски
    unit: str           # по-русски
    kind: str           # analog | enum | fault_bitmap
    step: float | None = None
    source: str = "KB"  # KB | каталог

    @property
    def key(self) -> str:
        return self.role or f"reg_{self.addr}"


def _norm(s: str) -> str:
    return " ".join(str(s).lower().replace("ё", "е").replace("_", " ").split())


async def kb_for(unit: UnitRef) -> tuple[Any, Any, str | None]:
    """(AnalyticsConfig | None, FaultRef | None, примечание): конфиг по привязке
    панели; без привязки — (None, None, «привязка KB не задана»)."""
    key = unit.triple
    now = time.monotonic()
    c = _cache.get(key)
    if c and now - c[0] < _KB_TTL:
        return c[1], c[2], c[3]
    from config import settings as _settings
    from analytics import binding
    b = unit.binding
    cfg = ref = None
    note: str | None = None
    if not ((b.get("controller_id") and b.get("engine_id")) or b.get("kb_path")):
        note = "привязка KB у панели не задана — имена и пороги только из каталога"
    else:
        try:
            cfg = binding.build_config(_settings.knowledge_base_path, controller_id=b.get("controller_id"),
                                       engine_id=b.get("engine_id"), kb_path=b.get("kb_path"))
        except Exception as e:  # noqa: BLE001
            note = f"конфиг KB не собран ({type(e).__name__}): имена и пороги только из каталога"
            logger.warning("telemetry_mcp: KB %s не собран", unit.name, exc_info=True)
        try:
            ref = binding.build_fault_ref(_settings.knowledge_base_path, controller_id=b.get("controller_id"),
                                          engine_id=b.get("engine_id"), kb_path=b.get("kb_path"))
        except Exception:  # noqa: BLE001
            logger.debug("telemetry_mcp: FaultRef %s не собран", unit.name, exc_info=True)
    _cache[key] = (now, cfg, ref, note)
    return cfg, ref, note


def param_label(meta: dict[str, Any] | None, cat: dict[str, Any] | None, addr: int) -> str:
    role = (meta or {}).get("role")
    lab = role_label_ru(role)
    if lab:
        return lab
    if meta and meta.get("description"):
        return str(meta["description"])
    if cat and (cat.get("name_ru") or cat.get("name")):
        return str(cat.get("name_ru") or cat.get("name"))
    return role or f"регистр {addr}"


def param_from_addr(cfg: Any, catalog: dict[int, dict[str, Any]], addr: int) -> ParamRef:
    meta = (cfg.register_map.get(addr) if cfg is not None else None) or None
    cat = catalog.get(addr)
    if meta is None and cat is None:
        raise ToolError(f"Регистр {addr} не описан ни в KB, ни в каталоге.")
    role = (meta or {}).get("role")
    kind = (meta or {}).get("kind") or (cat or {}).get("kind") or "analog"
    unit = (meta or {}).get("unit") or (cat or {}).get("unit") or ""
    step = None
    if cfg is not None and role:
        try:
            step = cfg.sensor_step(role)[0]
        except Exception:  # noqa: BLE001
            step = None
    return ParamRef(addr=addr, role=role, label=param_label(meta, cat, addr), unit=unit_ru(unit),
                    kind=str(kind), step=step, source="KB" if meta else "каталог")


def resolve_params(cfg: Any, catalog: dict[int, dict[str, Any]], names: list[str],
                   *, max_params: int | None = None) -> list[ParamRef]:
    """Имена параметров модели → регистры. Понимает адрес, роль KB, русское имя
    роли, псевдонимы (ROLE_ALIASES), описание KB и имя каталога (подстрока).
    Неоднозначная подстрока — ошибка со списком кандидатов."""
    out: list[ParamRef] = []
    seen: set[int] = set()
    rm: dict[int, dict[str, Any]] = cfg.register_map if cfg is not None else {}
    for raw in names:
        s = str(raw).strip()
        if not s:
            continue
        addr = _resolve_one(s, rm, catalog)
        if addr in seen:
            continue
        seen.add(addr)
        out.append(param_from_addr(cfg, catalog, addr))
    if max_params is not None and len(out) > max_params:
        raise ToolError(f"Параметров больше {max_params} — разбейте на два вызова.")
    if not out:
        raise ToolError("Укажите хотя бы один параметр (например: мощность, частота, обороты, ток L1).")
    return out


def _resolve_one(s: str, rm: dict[int, dict[str, Any]], catalog: dict[int, dict[str, Any]]) -> int:
    if s.isdigit():
        a = int(s)
        if a in rm or a in catalog:
            return a
        raise ToolError(f"Регистр {a} не описан ни в KB, ни в каталоге.")
    n = _norm(s)
    role = ROLE_ALIASES.get(n)
    if role is None:
        up = s.upper()
        if up in ROLE_LABELS or (up == s and "_" in s):
            role = up
    if role is None:
        for r, lab in ROLE_LABELS.items():
            if _norm(lab) == n:
                role = r
                break
    if role is not None:
        for a, m in rm.items():
            if m.get("role") == role:
                return a
        # роль есть в словарях, но у этой машины такого регистра нет — ищем по имени
    cands: list[tuple[int, str]] = []
    for a, m in rm.items():
        d = _norm(m.get("description") or "")
        lab = _norm(ROLE_LABELS.get(m.get("role") or "", ""))
        if n and (n in d or (lab and n in lab)):
            cands.append((a, m.get("description") or lab))
    if not cands:
        for a, c in catalog.items():
            nm = _norm(c.get("name_ru") or ""), _norm(c.get("name") or "")
            if n and (n in nm[0] or n in nm[1]):
                cands.append((a, c.get("name_ru") or c.get("name") or str(a)))
    if len(cands) == 1:
        return cands[0][0]
    if len(cands) > 1:
        exact = [c for c in cands if _norm(c[1]) == n]
        if len(exact) == 1:
            return exact[0][0]
        listing = "; ".join(f"{a} {nm}" for a, nm in cands[:12])
        raise ToolError(f"«{s}» подходит нескольким регистрам, уточните адресом: {listing}")
    raise ToolError(f"Параметр «{s}» не найден. Примеры: мощность, частота, обороты, ток L1, "
                    "давление масла, температура ОЖ, напряжение, АКБ, или адрес регистра.")


def key_roles(cfg: Any) -> list[str]:
    """«Ключевые» параметры: trip_snapshot KB + роли статуса + колонки ленты акта."""
    from analytics.config import _TRACE_DEFAULT_ROLES
    roles: list[str] = []
    try:
        roles.extend(cfg.trip_snapshot_roles)
    except Exception:  # noqa: BLE001
        pass
    try:
        from online.status_assembler import _KEY_ROLES
        for rs in sorted(_KEY_ROLES):
            roles.extend(_KEY_ROLES[rs])
    except Exception:  # noqa: BLE001
        pass
    roles.extend(_TRACE_DEFAULT_ROLES)
    out: list[str] = []
    for r in roles:
        if r not in out and cfg.role_to_addr(r) is not None:
            out.append(r)
    return out


_NOMINAL_KEYS: dict[str, tuple[str, ...]] = {
    "FREQUENCY": ("nominal_hz",), "RPM": ("rated_rpm", "nominal_rpm"),
    "BATTERY_V": ("nominal_v",), "ACTIVE_POWER_TOTAL": ("rated_kw", "nominal_kw", "prime_kw"),
    "CURRENT_AVG": ("rated_a", "nominal_a"), "CURRENT_L1": ("rated_a", "nominal_a"),
    "CURRENT_L2": ("rated_a", "nominal_a"), "CURRENT_L3": ("rated_a", "nominal_a"),
    "VOLTAGE_LL_AVG": ("nominal_v_ll", "rated_v", "nominal_voltage_v"),
    "OIL_PRESS": ("rated_min_kpa",), "COOLANT_TEMP": ("normal_max_c",),
}


def nominal_for(cfg: Any, role: str | None) -> tuple[float, str] | None:
    """Номинал/паспортный ориентир роли из thresholds: (значение, ключ)."""
    if cfg is None or not role or role not in _NOMINAL_KEYS:
        return None
    want = _NOMINAL_KEYS[role]
    found: list[tuple[str, float]] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k in want and isinstance(v, (int, float)):
                    found.append((f"{path}.{k}".strip("."), float(v)))
                elif isinstance(v, dict):
                    walk(v, f"{path}.{k}".strip("."))
    walk(cfg.thresholds, "")
    if not found:
        return None
    found.sort(key=lambda x: (0 if ".passport." in x[0] else 1, x[0]))
    return found[0][1], found[0][0]
