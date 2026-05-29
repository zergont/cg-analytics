"""Загрузчик и валидатор YAML-конфигов аналитического блока."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


class AnalyticsConfig:
    """Все параметры аналитики, загруженные из YAML-файлов в директории analytics/."""

    REQUIRED_FILES = (
        "mapping.yaml",
        "thresholds.yaml",
        "zones.yaml",
        "segmentation.yaml",
        "detectors.yaml",
        "fault_matrix.yaml",
    )

    def __init__(self, kb_path: str | Path) -> None:
        base = Path(kb_path) / "analytics"
        if not base.is_dir():
            raise FileNotFoundError(
                f"Директория конфигов аналитики не найдена: {base}\n"
                f"Ожидается: {base / 'mapping.yaml'} и другие файлы."
            )

        self.mapping: dict[str, Any] = self._load(base / "mapping.yaml")
        self.thresholds: dict[str, Any] = self._load(base / "thresholds.yaml")
        self.zones: dict[str, Any] = self._load(base / "zones.yaml")
        self.segmentation: dict[str, Any] = self._load(base / "segmentation.yaml")
        self.detectors: dict[str, Any] = self._load(base / "detectors.yaml")
        self.fault_matrix: dict[str, Any] = self._load(base / "fault_matrix.yaml")

        self._register_map: dict[int, dict[str, Any]] = self._build_register_map()
        self._whitelist_analog: frozenset[int] = self._build_whitelist("analog")
        self._whitelist_enum: frozenset[int] = self._build_whitelist("enum")
        self._whitelist_fault: frozenset[int] = self._build_whitelist("fault_bitmap")

    # ── Публичные свойства ──────────────────────────────────────────────────

    @property
    def register_map(self) -> dict[int, dict[str, Any]]:
        """Карта {addr: metadata} для всех whitelist-регистров."""
        return self._register_map

    @property
    def whitelist_analog(self) -> frozenset[int]:
        """Адреса аналоговых регистров (из history_rich)."""
        return self._whitelist_analog

    @property
    def whitelist_enum(self) -> frozenset[int]:
        """Адреса enum-регистров (из enum_history)."""
        return self._whitelist_enum

    @property
    def whitelist_fault(self) -> frozenset[int]:
        """Адреса fault-bitmap регистров (из fault_history)."""
        return self._whitelist_fault

    def role_to_addr(self, role: str) -> int | None:
        """Найти addr по имени роли."""
        for addr, meta in self._register_map.items():
            if meta.get("role") == role:
                return addr
        return None

    def thr(self, *keys: str, default: Any = None) -> Any:
        """Быстрый доступ к вложенному значению в thresholds.

        Пример: cfg.thr("oil_pressure", "controller", "shutdown_rated_kpa")
        """
        node = self.thresholds
        for k in keys:
            if not isinstance(node, dict):
                return default
            node = node.get(k, {})
        return node if node != {} else default

    def det(self, scenario: str, key: str, default: Any = None) -> Any:
        """Быстрый доступ к параметру детектора."""
        return self.detectors.get(scenario, {}).get(key, default)

    def seg(self, *keys: str, default: Any = None) -> Any:
        """Быстрый доступ к параметрам сегментации."""
        node = self.segmentation
        for k in keys:
            if not isinstance(node, dict):
                return default
            node = node.get(k, {})
        return node if node != {} else default

    def bitmap_severity(self, raw_severity: str) -> str:
        """Перевести severity из fault_bitmap_map → шкалу ТЗ (SHUTDOWN/ALARM/WARNING/INFO)."""
        return self.fault_matrix.get("bitmap_severity_map", {}).get(
            raw_severity, "WARNING"
        )

    def code_severity(self, code: int) -> str:
        """Severity для кода неисправности из fault_matrix.codes."""
        codes = self.fault_matrix.get("codes", {})
        entry = codes.get(code) or codes.get(str(code))
        if entry:
            return entry.get("severity", self.fault_matrix.get("default_severity", "WARNING"))
        return self.fault_matrix.get("default_severity", "WARNING")

    def zone_boundaries(self) -> dict[str, tuple[float, float]]:
        """Вернуть {zone_name: (min_pct, max_pct)} для всех зон кроме NA."""
        result = {}
        for name, z in self.zones.get("load_zones", {}).items():
            if name == "NA":
                continue
            result[name] = (
                float(z.get("min_pct", 0)),
                float(z.get("max_pct") or float("inf")),
            )
        return result

    def hysteresis_pct(self) -> float:
        return float(self.zones.get("hysteresis", {}).get("pct", 5.0))

    # ── Приватные методы ────────────────────────────────────────────────────

    @staticmethod
    def _load(path: Path) -> dict[str, Any]:
        if not path.exists():
            raise FileNotFoundError(f"Файл конфигурации не найден: {path}")
        with path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data

    def _build_register_map(self) -> dict[int, dict[str, Any]]:
        result: dict[int, dict[str, Any]] = {}
        for addr_str, meta in self.mapping.get("registers", {}).items():
            try:
                addr = int(addr_str)
            except (ValueError, TypeError):
                continue
            result[addr] = meta or {}
        return result

    def _build_whitelist(self, kind: str) -> frozenset[int]:
        return frozenset(
            addr
            for addr, meta in self._register_map.items()
            if meta.get("kind") == kind
        )
