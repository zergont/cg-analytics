# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
"""Тест эквивалентности слоистого конфига монолитной папке (Этап 1).

Доказывает: конфиг, собранный из слоёв
  _defaults + controllers/pcc3300 + engines/cummins_kta50
поведенчески эквивалентен старой монолитной папке
  equipment/cummins_kta50_pcc3300

«Поведенчески» = совпадает всё, что читает движок аналитики. Слой контроллера
дополнительно несёт `domain` у ролей (шов под H3) — это АДДИТИВНО и допускается:
старая мета должна быть подмножеством новой.

Запуск:  py -3 scripts/test_layered_config.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analytics.config import AnalyticsConfig  # noqa: E402
from analytics.fault_ref import FaultRef  # noqa: E402
from analytics import binding  # noqa: E402

KB = Path("knowledge_base")
OLD_DIR = KB / "equipment" / "cummins_kta50_pcc3300"

_errors: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        _errors.append(msg)


def main() -> int:
    old = AnalyticsConfig(OLD_DIR)
    new = AnalyticsConfig.from_pair(KB, "pcc3300", "cummins_kta50")

    # ── Файлы, скопированные verbatim → строгое равенство ──────────────────
    for attr in ("thresholds", "zones", "segmentation", "detectors", "fault_matrix"):
        check(getattr(old, attr) == getattr(new, attr), f"{attr}: расхождение слитого конфига")

    # ── Whitelist-множества регистров ──────────────────────────────────────
    check(old.whitelist_analog == new.whitelist_analog, "whitelist_analog расходится")
    check(old.whitelist_enum == new.whitelist_enum, "whitelist_enum расходится")
    check(old.whitelist_fault == new.whitelist_fault, "whitelist_fault расходится")

    # ── register_map: старая мета ⊆ новой (new добавляет только domain) ────
    check(set(old.register_map) == set(new.register_map), "набор адресов регистров расходится")
    for addr, ometa in old.register_map.items():
        nmeta = new.register_map.get(addr, {})
        for k, v in ometa.items():
            check(nmeta.get(k) == v, f"регистр {addr}: поле {k!r} {v!r} != {nmeta.get(k)!r}")
        # Шов H3: у новой меты обязан быть domain
        check("domain" in nmeta, f"регистр {addr}: не проставлен domain (шов H3)")

    # ── Производные представления, которыми пользуется движок ──────────────
    check(old.trip_snapshot_roles == new.trip_snapshot_roles, "trip_snapshot_roles расходится")
    check(old.zone_boundaries() == new.zone_boundaries(), "zone_boundaries расходится")
    check(abs(old.hysteresis_pct() - new.hysteresis_pct()) < 1e-9, "hysteresis_pct расходится")
    for role in ("LOAD_PCT", "COOLANT_TEMP", "OIL_PRESS", "RPM", "RUN_STATE"):
        check(old.role_to_addr(role) == new.role_to_addr(role), f"role_to_addr({role}) расходится")

    # ── FaultRef: индекс кодов совпадает ───────────────────────────────────
    fr_old = FaultRef(OLD_DIR)
    fr_new = FaultRef(search_paths=[
        KB / "engines" / "cummins_kta50",
        KB / "controllers" / "pcc3300",
    ])
    check(len(fr_old) == len(fr_new), f"FaultRef: разное число кодов {len(fr_old)} != {len(fr_new)}")

    # ── Резолвер привязки (analytics.binding) ──────────────────────────────
    # Пара даёт те же слои, что from_pair
    pair_cfg = binding.build_config(KB, controller_id="pcc3300", engine_id="cummins_kta50")
    check(pair_cfg.mapping == new.mapping, "binding.build_config(пара): mapping расходится")
    check([p.name for p in pair_cfg.layers] == ["_defaults", "pcc3300", "cummins_kta50"],
          "binding: неверный порядок слоёв пары")
    # Legacy-режим по kb_path
    legacy_cfg = binding.build_config(KB, kb_path="cummins_kta50_pcc3300")
    check(legacy_cfg.thresholds == old.thresholds, "binding.build_config(legacy): thresholds расходится")
    # FaultRef через резолвер
    fr_pair = binding.build_fault_ref(KB, controller_id="pcc3300", engine_id="cummins_kta50")
    check(len(fr_pair) == len(fr_old), "binding.build_fault_ref: число кодов расходится")
    # Пустая привязка → ошибка
    try:
        binding.build_config(KB)
        check(False, "binding.build_config без привязки должен бросать BindingError")
    except binding.BindingError:
        pass

    # ── Итог ───────────────────────────────────────────────────────────────
    if _errors:
        print(f"FAIL — {len(_errors)} расхождений:")
        for e in _errors:
            print(f"  • {e}")
        return 1
    print("PASS — слоистый конфиг эквивалентен монолиту")
    print(f"   слои new: {[p.name for p in new.layers]}")
    print(f"   регистров: {len(new.register_map)}, кодов FaultRef: {len(fr_new)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
