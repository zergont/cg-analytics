# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Детерминированный справочник кодов неисправностей PCC3300.

Точный lookup по коду из регистра 40012 (LAST_FAULT_CODE).
Заменяет семантический RAG-поиск по документации.

Структура записи (pcc3300_fault_codes.json):
  code         — числовой код
  severity     — sh / sc / wa / de / ev
  description  — {en, ru}
  documentation (если есть):
    what       — описание неисправности
    causes[]   — возможные причины
    steps[]    — инструкция по устранению
    related    — связанные коды
    source     — ссылка на документ + страница
  verified     — true/false

verified=true  → источник из проектных документов → полная расшифровка
verified=false → ai-задел, внешние источники → только базовое описание
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Имена файлов для поиска (первый существующий используется)
_SEARCH_NAMES = [
    "pcc3300_fault_codes.json",
    "pcc3300_fault_codes_tagged.json",
]

# Маппинг severity из JSON → метка для отчёта
_SEV_LABEL: dict[str, str] = {
    "sh": "SHUTDOWN",
    "sc": "SHUTDOWN+охлаждение",
    "wa": "WARNING",
    "de": "DERATE",
    "ev": "EVENT",
}

# Совместимость severity бита (fault_bitmap_map) ↔ severity кода (справочник).
# Порядок в кортеже = приоритет при разрешении неоднозначности имени.
_BITMAP_SEV_COMPAT: dict[str, tuple[str, ...]] = {
    "shutdown": ("sh", "sc"),
    "shutdown_cooldown": ("sc", "sh"),
    "derate": ("de",),
    "warning": ("wa",),
}


def _norm_name(s: str) -> str:
    """Нормализация имени для сопоставления бит ↔ код: без регистра и пунктуации."""
    return re.sub(r"[^a-z0-9]", "", s.lower())


class FaultRef:
    """Справочник кодов неисправностей PCC3300.

    Загружается из pcc3300_fault_codes.json в директории KB оборудования.
    Синглтон не используется — объект создаётся один раз при старте runner/engine.
    """

    def __init__(
        self,
        kb_path: "str | Path | None" = None,
        *,
        search_paths: "list[str | Path] | None" = None,
    ) -> None:
        """Загрузить справочник из одного KB-пути или из списка слоёв.

        `search_paths` перебираются по порядку — берётся первый найденный файл.
        Для композиции передавать слои от приоритетного к базовому, напр.
        [engines/<e>, controllers/<c>] — справочник живёт в слое контроллера.
        """
        self._index: dict[int, dict[str, Any]] = {}
        self._name_index: dict[str, list[dict[str, Any]]] = {}
        if search_paths is not None:
            bases = [Path(p) for p in search_paths]
        elif kb_path is not None:
            bases = [Path(kb_path)]
        else:
            raise ValueError("FaultRef: нужен либо kb_path, либо search_paths")
        self._load(bases)

    def _load(self, bases: list[Path]) -> None:
        from config import kb_read
        for base in bases:
            for name in _SEARCH_NAMES:
                p = kb_read(base / name)   # рабочий оверлей поверх git-эталона
                if p.exists():
                    try:
                        data = json.loads(p.read_text(encoding="utf-8-sig"))
                        codes_list = data.get("fault_codes", [])
                        self._index = {
                            int(entry["code"]): entry
                            for entry in codes_list
                            if "code" in entry
                        }
                        # Индекс по нормализованному EN-имени: для сопоставления
                        # битов fault_bitmap_map (у них нет поля code) с кодами.
                        self._name_index = {}
                        for entry in self._index.values():
                            en = (entry.get("description") or {}).get("en") or ""
                            if en:
                                self._name_index.setdefault(_norm_name(en), []).append(entry)
                        logger.info(
                            "FaultRef: загружено %d кодов из %s", len(self._index), p.name
                        )
                        return
                    except Exception as exc:
                        logger.warning("FaultRef: ошибка загрузки %s: %s", p, exc)
        logger.warning(
            "FaultRef: справочник кодов не найден в %s",
            ", ".join(str(b) for b in bases),
        )

    # ── Публичный API ─────────────────────────────────────────────────────────

    def lookup(self, code: int) -> dict[str, Any] | None:
        """Точный lookup по коду. Возвращает запись из справочника или None."""
        return self._index.get(int(code))

    def lookup_by_name(
        self, name: str, raw_severity: str | None = None
    ) -> dict[str, Any] | None:
        """Сопоставить имя бита (fault_bitmap_map) с кодом справочника.

        Матч по нормализованному EN-имени. Неоднозначность (одно имя — несколько
        кодов, напр. Low Coolant Level → 197 warning / 235 shutdown) разрешается
        по severity бита; если и после этого неоднозначно — None (не гадаем).
        Имена битов в стиле J1939 (напр. «...: Vtg Above Normal») в справочнике
        отсутствуют — для них тоже None.
        """
        if not name:
            return None
        candidates = getattr(self, "_name_index", {}).get(_norm_name(name)) or []
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1 and raw_severity:
            compat = _BITMAP_SEV_COMPAT.get(raw_severity.lower(), ())
            for sev in compat:
                matched = [e for e in candidates if e.get("severity") == sev]
                if len(matched) == 1:
                    return matched[0]
        return None

    def format_for_report(self, code: int) -> str | None:
        """Форматировать расшифровку кода для вставки в Markdown-отчёт.

        verified=true  → полное описание (what + causes + steps + related + source)
        verified=false → только базовое описание (ru/en)
        Не найден      → None
        """
        entry = self.lookup(code)
        if entry is None:
            return None

        desc = entry.get("description") or {}
        desc_ru = desc.get("ru") or desc.get("en") or "—"
        sev_raw = entry.get("severity", "")
        sev_label = _SEV_LABEL.get(sev_raw, sev_raw.upper())
        verified = entry.get("verified", False)
        doc = entry.get("documentation") or {}

        lines: list[str] = []

        # Заголовок
        lines.append(f"**Код {code}** ({sev_label}): {desc_ru}")

        if verified and doc:
            # Полная расшифровка
            if doc.get("what"):
                lines.append(f"  - **Описание:** {doc['what']}")
            causes = doc.get("causes") or []
            if causes:
                lines.append("  - **Причины:**")
                for c in causes:
                    lines.append(f"    - {c}")
            steps = doc.get("steps") or []
            if steps:
                lines.append("  - **Устранение:**")
                for s in steps:
                    lines.append(f"    - {s}")
            if doc.get("related"):
                lines.append(f"  - **Связанные коды:** {doc['related']}")
            if doc.get("source"):
                lines.append(f"  - **Источник:** {doc['source']}")
        elif doc:
            # verified=false — только базовое, не подставляем расширенное
            desc_en = desc.get("en") or ""
            if desc_en and desc_en != desc_ru:
                lines.append(f"  - *(EN: {desc_en})*")
            lines.append("  - *(расширенное описание не верифицировано по проектным документам)*")

        return "\n".join(lines)

    def __bool__(self) -> bool:
        return bool(self._index)

    def __len__(self) -> int:
        return len(self._index)
