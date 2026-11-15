# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Пометки базы знаний к регистрам (v5.2.13; решение владельца 14.11.2026,
docs/plans/design_kb_register_notes.md).

Смысл регистра, которого нет ни в имени, ни в единице: из чего складывается значение,
чем ограничено, какие регистры его объясняют. Запись регистра в `mapping.yaml` несёт
два поля:

    note_ru      — текст для модели; только то, что написано в источнике
    note_source  — документ, раздел, страница

База знаний — источник фактов с указанием источника, поэтому пометка без
`note_source` модели не показывается: её как будто нет, в журнал идёт одно
предупреждение на адрес.

Печатают пометки `register_catalog` (под строкой регистра, пока список короткий) и
`kb_passport` (отдельным блоком). Функции здесь чистые: ни БД, ни сети.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable

logger = logging.getLogger(__name__)

# В списке длиннее пометки под строками не печатаются: полный каталог — сотня строк,
# а пометка — абзац. Вместо текста в примечания ответа идёт подсказка с адресами
NOTE_ROWS_MAX = 10
# Сколько адресов назвать в подсказке
_HINT_ADDRS_MAX = 12

_warned: set[Any] = set()


def _clean(v: Any) -> str:
    """Текст поля одной строкой: YAML-блок приходит с переводами строк и отступами."""
    return " ".join(str(v).split()) if isinstance(v, str) else ""


def register_note(meta: dict[str, Any] | None, addr: int | None = None) -> tuple[str, str] | None:
    """(текст, источник) пометки записи регистра. None — пометки нет или она без
    источника (тогда одно предупреждение в журнал на адрес)."""
    if not isinstance(meta, dict):
        return None
    text = _clean(meta.get("note_ru"))
    if not text:
        return None
    src = _clean(meta.get("note_source"))
    if not src:
        if addr not in _warned:
            _warned.add(addr)
            logger.warning("telemetry_mcp: у регистра %s пометка note_ru без note_source — модели не показывается",
                           addr if addr is not None else "?")
        return None
    return text, src


def note_lines(meta: dict[str, Any] | None, addr: int | None = None, indent: str = "    ") -> list[str]:
    """Две строки под строкой регистра: «пометка: …» и «источник: …»; без пометки — пусто."""
    n = register_note(meta, addr)
    if n is None:
        return []
    return [f"{indent}пометка: {n[0]}", f"{indent}источник: {n[1]}"]


def rows_with_notes(rows: list[str], addrs: list[int], rm: dict[int, Any], notes: list[str],
                    *, limit: int = NOTE_ROWS_MAX) -> list[str]:
    """Строки каталога с пометками базы знаний. `rows` и `addrs` идут парами (строка и
    её адрес). До `limit` строк — пометка и источник под строкой регистра; в списке
    длиннее строки остаются как есть, а в `notes` (примечания ответа) добавляется
    подсказка, у каких регистров пометка есть."""
    noted = [a for a in addrs[:len(rows)] if register_note(rm.get(a), a) is not None]
    if not noted:
        return rows
    if len(rows) > limit:
        notes.append("есть пометка базы знаний у регистров: " + ", ".join(str(a) for a in noted[:_HINT_ADDRS_MAX])
                     + (" …" if len(noted) > _HINT_ADDRS_MAX else "")
                     + " — запросите регистр по адресу (query), пометка придёт вместе со строкой")
        return rows
    out: list[str] = []
    for a, line in zip(addrs, rows):
        out.append(line)
        out.extend(note_lines(rm.get(a), a))
    return out


def passport_notes(rm: dict[int, Any], catalog: dict[int, Any] | None = None,
                   addrs: Iterable[int] | None = None) -> list[str]:
    """Блоки пометок для паспорта KB: «[регистр 40595 · имя]», пометка, источник.
    `addrs` — только эти регистры; None — все регистры с пометкой, по возрастанию адреса."""
    from .kb import param_label
    out: list[str] = []
    for a in (sorted(rm) if addrs is None else list(dict.fromkeys(addrs))):
        meta = rm.get(a)
        if register_note(meta, a) is None:
            continue
        out.append(f"\n[регистр {a} · {param_label(meta, (catalog or {}).get(a), a)}]")
        out.extend(note_lines(meta, a, indent="  "))
    return out
