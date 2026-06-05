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


class FaultRef:
    """Справочник кодов неисправностей PCC3300.

    Загружается из pcc3300_fault_codes.json в директории KB оборудования.
    Синглтон не используется — объект создаётся один раз при старте runner/engine.
    """

    def __init__(self, kb_path: str | Path) -> None:
        self._index: dict[int, dict[str, Any]] = {}
        self._load(Path(kb_path))

    def _load(self, base: Path) -> None:
        for name in _SEARCH_NAMES:
            p = base / name
            if p.exists():
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    codes_list = data.get("fault_codes", [])
                    self._index = {
                        int(entry["code"]): entry
                        for entry in codes_list
                        if "code" in entry
                    }
                    logger.info(
                        "FaultRef: загружено %d кодов из %s", len(self._index), p.name
                    )
                    return
                except Exception as exc:
                    logger.warning("FaultRef: ошибка загрузки %s: %s", p, exc)
        logger.warning("FaultRef: справочник кодов не найден в %s", base)

    # ── Публичный API ─────────────────────────────────────────────────────────

    def lookup(self, code: int) -> dict[str, Any] | None:
        """Точный lookup по коду. Возвращает запись из справочника или None."""
        return self._index.get(int(code))

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
