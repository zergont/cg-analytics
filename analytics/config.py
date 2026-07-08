# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Загрузчик и валидатор YAML-конфигов аналитического блока."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

_log = logging.getLogger(__name__)


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

    # Имя папки универсального базового слоя внутри knowledge_base/
    DEFAULTS_DIRNAME = "_defaults"

    def __init__(
        self,
        kb_path: str | Path | None = None,
        *,
        layers: "list[str | Path] | None" = None,
    ) -> None:
        """Загрузить конфиг из одного KB-пути или из упорядоченного списка слоёв.

        Обратная совместимость: `AnalyticsConfig(kb_path)` работает как раньше —
        читает `kb_path/analytics/*.yaml` (один слой).

        Композиция: `layers=[_defaults, controllers/<c>, engines/<e>]` — каждый из
        6 YAML собирается deep-merge по слоям (позднейший слой перекрывает ранний).
        См. `AnalyticsConfig.from_pair`.
        """
        self._layers: list[Path] = self._resolve_layer_dirs(kb_path, layers)

        self.mapping: dict[str, Any] = self._load_merged("mapping.yaml")
        self.thresholds: dict[str, Any] = self._load_merged("thresholds.yaml")
        self.zones: dict[str, Any] = self._load_merged("zones.yaml")
        self.segmentation: dict[str, Any] = self._load_merged("segmentation.yaml")
        self.detectors: dict[str, Any] = self._load_merged("detectors.yaml")
        self.fault_matrix: dict[str, Any] = self._load_merged("fault_matrix.yaml")

        self._register_map: dict[int, dict[str, Any]] = self._build_register_map()
        self._whitelist_analog: frozenset[int] = self._build_whitelist("analog")
        self._whitelist_enum: frozenset[int] = self._build_whitelist("enum")
        self._whitelist_fault: frozenset[int] = self._build_whitelist("fault_bitmap")

        self._validate()
        self._warn_missing_keys()

    # ── Фабрики ──────────────────────────────────────────────────────────────

    @classmethod
    def from_pair(
        cls,
        kb_root: str | Path,
        controller_id: str,
        engine_id: str,
    ) -> "AnalyticsConfig":
        """Собрать конфиг из пары (контроллер, двигатель) поверх базового слоя.

        Слои (в порядке возрастания приоритета):
          kb_root/_defaults  →  kb_root/controllers/<controller_id>  →
          kb_root/engines/<engine_id>

        Отсутствующий базовый слой `_defaults` пропускается (не обязателен).
        Слои контроллера и двигателя обязаны существовать.
        """
        root = Path(kb_root)
        layers: list[Path] = []
        defaults = root / cls.DEFAULTS_DIRNAME
        if defaults.is_dir():
            layers.append(defaults)
        layers.append(root / "controllers" / controller_id)
        layers.append(root / "engines" / engine_id)
        return cls(layers=layers)

    # ── Публичные свойства ──────────────────────────────────────────────────

    @property
    def layers(self) -> list[Path]:
        """Слои конфига в порядке возрастания приоритета (для FaultRef и диагностики)."""
        return list(self._layers)

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

    def role_unit(self, role: str) -> str:
        """Единица измерения роли (из mapping.yaml)."""
        for meta in self._register_map.values():
            if meta.get("role") == role:
                return meta.get("unit") or ""
        return ""

    @property
    def trip_snapshot_roles(self) -> list[str]:
        """Роли для снапшота контекста аварии (trip_snapshot: true в mapping.yaml).

        Тот же набор — «ключевые показатели» отчёта. Задаётся в KB,
        не в коде: у другого оборудования свой набор.
        """
        return [
            meta["role"]
            for meta in self._register_map.values()
            if meta.get("trip_snapshot") and meta.get("role")
        ]

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

    def bitmap_severity(self, raw_severity: str | None) -> str:
        """Перевести severity из fault_bitmap_map → шкалу ТЗ (SHUTDOWN/WARNING/CAUTION/INFO).

        Бит без поля severity (raw_severity=None/'') → INFO:
        это статусный/информационный бит, не неисправность.
        """
        if not raw_severity:
            return "INFO"
        return self.fault_matrix.get("bitmap_severity_map", {}).get(
            raw_severity, self.fault_matrix.get("default_severity", "WARNING")
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

    # ── Валидация ───────────────────────────────────────────────────────────

    def _validate(self) -> None:
        """Проверить конфиг на корректность. Бросает ValueError при ошибках.

        Запускается при загрузке — невалидный конфиг отклоняется целиком.
        """
        errors: list[str] = []

        # ── Зоны нагрузки ──────────────────────────────────────────────────
        zones = self.zones.get("load_zones", {})
        ordered = ["LOW", "NORMAL", "ELEVATED", "OVERLOAD"]
        prev_max: float = 0.0
        for name in ordered:
            z = zones.get(name, {})
            z_min = z.get("min_pct")
            z_max = z.get("max_pct")
            if z_min is None:
                errors.append(f"zones.load_zones.{name}: отсутствует min_pct")
                continue
            try:
                z_min_f = float(z_min)
            except (TypeError, ValueError):
                errors.append(f"zones.load_zones.{name}.min_pct={z_min!r}: не число")
                continue
            if abs(z_min_f - prev_max) > 1e-6 and prev_max > 0:
                errors.append(
                    f"zones: разрыв/перекрытие между зонами: {name}.min={z_min_f} != "
                    f"предыдущей max={prev_max}"
                )
            if z_max is not None:
                try:
                    z_max_f = float(z_max)
                except (TypeError, ValueError):
                    errors.append(f"zones.load_zones.{name}.max_pct={z_max!r}: не число")
                    continue
                if z_max_f <= z_min_f:
                    errors.append(f"zones.load_zones.{name}: max_pct={z_max_f} <= min_pct={z_min_f}")
                prev_max = z_max_f

        # Гистерезис < ширины самой узкой зоны (без OVERLOAD)
        hyst = self.zones.get("hysteresis", {}).get("pct")
        if hyst is not None:
            try:
                hyst_f = float(hyst)
            except (TypeError, ValueError):
                errors.append(f"zones.hysteresis.pct={hyst!r}: не число")
                hyst_f = 0.0
            for name in ["LOW", "NORMAL", "ELEVATED"]:
                z = zones.get(name, {})
                z_min = z.get("min_pct")
                z_max = z.get("max_pct")
                if z_min is not None and z_max is not None:
                    try:
                        width = float(z_max) - float(z_min)
                        if hyst_f >= width:
                            errors.append(
                                f"zones.hysteresis.pct={hyst_f} >= ширины зоны {name} "
                                f"({width}): это сделает зону недостижимой"
                            )
                    except (TypeError, ValueError):
                        pass

        # ── Сегментация ────────────────────────────────────────────────────
        n_stab = self.segmentation.get("boundary_confirmation", {}).get("n_stab")
        if n_stab is None or not isinstance(n_stab, int) or n_stab < 1:
            errors.append(
                f"segmentation.boundary_confirmation.n_stab должен быть целым >= 1, "
                f"получено: {n_stab!r}"
            )
        heartbeat = self.segmentation.get("data_quality", {}).get("heartbeat_nominal_sec")
        if heartbeat is not None:
            try:
                if float(heartbeat) <= 0:
                    errors.append("segmentation.data_quality.heartbeat_nominal_sec должен быть > 0")
            except (TypeError, ValueError):
                errors.append(f"segmentation.data_quality.heartbeat_nominal_sec={heartbeat!r}: не число")

        # ── Детекторы: severity_default ────────────────────────────────────
        valid_sev = {"INFO", "CAUTION", "WARNING", "SHUTDOWN"}
        for scenario, params in self.detectors.items():
            if not isinstance(params, dict):
                continue
            sev = params.get("severity_default")
            if sev is not None and sev not in valid_sev:
                errors.append(
                    f"detectors.{scenario}.severity_default={sev!r} "
                    f"не входит в {valid_sev}"
                )
            # Числовые пороги >= 0
            for key, val in params.items():
                if key.endswith(("_sec", "_pct", "_kpa", "_c", "_v", "_hz", "_kw_per_s")):
                    try:
                        if float(val) < 0:
                            errors.append(
                                f"detectors.{scenario}.{key}={val}: ожидается >= 0"
                            )
                    except (TypeError, ValueError):
                        pass  # не числовое поле

        # ── Fault matrix: severity mapping ─────────────────────────────────
        for raw_sev, mapped in self.fault_matrix.get("bitmap_severity_map", {}).items():
            if mapped not in valid_sev:
                errors.append(
                    f"fault_matrix.bitmap_severity_map.{raw_sev}={mapped!r} "
                    f"не входит в {valid_sev}"
                )

        # ── Mapping: критические роли присутствуют ─────────────────────────
        critical_roles = ["LOAD_PCT", "COOLANT_TEMP", "OIL_PRESS", "RPM", "RUN_STATE"]
        defined_roles = {m.get("role") for m in self._register_map.values()}
        for role in critical_roles:
            if role not in defined_roles:
                errors.append(f"mapping: критическая роль {role!r} не найдена ни в одном регистре")

        if errors:
            raise ValueError(
                f"Невалидная конфигурация аналитики ({len(errors)} ошибок):\n"
                + "\n".join(f"  • {e}" for e in errors)
            )

    def _warn_missing_keys(self) -> None:
        """Логировать предупреждение если ожидаемые ключи отсутствуют в YAML.

        Стратегия B (пункт 6 Addendum v1.1): тихий fallback разрешён,
        но расхождение YAML ↔ default должно быть видимым.
        """
        expected: list[tuple[str, str, str]] = [
            # (путь для логирования, метод, аргументы для проверки)
            ("THERMAL_HIGHLOAD.thermal_decay_rate_per_sec", "det", "THERMAL_HIGHLOAD.thermal_decay_rate_per_sec"),
            ("WARMUP_VIOLATION.hot_start_warmup_sec", "det", "WARMUP_VIOLATION.hot_start_warmup_sec"),
            ("COKING_RISK.purge_min_coolant_c", "det", "COKING_RISK.purge_min_coolant_c"),
        ]
        for label, _method, dotted in expected:
            parts = dotted.split(".")
            node = self.detectors
            found = True
            for p in parts:
                if not isinstance(node, dict) or p not in node:
                    found = False
                    break
                node = node[p]
            if not found:
                _log.warning("config: ключ %r отсутствует в detectors.yaml — используется default", label)

    # ── Приватные методы ────────────────────────────────────────────────────

    @staticmethod
    def _resolve_layer_dirs(
        kb_path: "str | Path | None",
        layers: "list[str | Path] | None",
    ) -> list[Path]:
        """Определить список слоёв. Ровно один из аргументов должен быть задан."""
        if layers is not None:
            dirs = [Path(p) for p in layers]
            if not dirs:
                raise ValueError("AnalyticsConfig: список layers пуст")
            return dirs
        if kb_path is None:
            raise ValueError("AnalyticsConfig: нужен либо kb_path, либо layers")
        base = Path(kb_path)
        if not (base / "analytics").is_dir():
            raise FileNotFoundError(
                f"Директория конфигов аналитики не найдена: {base / 'analytics'}\n"
                f"Ожидается: {base / 'analytics' / 'mapping.yaml'} и другие файлы."
            )
        return [base]

    def _load_merged(self, filename: str) -> dict[str, Any]:
        """Собрать один YAML из всех слоёв: <layer>/analytics/<filename>.

        Слои сливаются deep-merge по порядку (позднейший перекрывает ранний).
        Файл может отсутствовать в части слоёв, но должен быть хотя бы в одном.
        """
        acc: dict[str, Any] = {}
        found = False
        for layer in self._layers:
            path = layer / "analytics" / filename
            if path.exists():
                acc = self._deep_merge(acc, self._load(path))
                found = True
        if not found:
            searched = ", ".join(str(l / "analytics" / filename) for l in self._layers)
            raise FileNotFoundError(
                f"Файл конфигурации {filename} не найден ни в одном слое: {searched}"
            )
        return acc

    @classmethod
    def _deep_merge(cls, base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
        """Рекурсивное слияние словарей: override поверх base.

        Правила: вложенные dict сливаются по ключам; списки и скаляры —
        override ЗАМЕНЯЕТ base целиком (не конкатенация). Важно для `zones`,
        `detectors`, whitelist-подобных списков — двигательный слой задаёт
        полную замену, а не дополнение.
        """
        result = dict(base)
        for key, val in override.items():
            if (
                key in result
                and isinstance(result[key], dict)
                and isinstance(val, dict)
            ):
                result[key] = cls._deep_merge(result[key], val)
            else:
                result[key] = val
        return result

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
