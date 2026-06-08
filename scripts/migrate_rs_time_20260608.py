"""Миграция: сквозное время RUN_STATE=3 через continued_from цепочки.

Пересчитывает поле "Время под нагрузкой (RUN_STATE=3)" в report_md
для всех сегментов, у которых continued_from IS NOT NULL.

Запуск:
    python scripts/migrate_rs_time_20260608.py [--dry-run]

Скрипт удаляет сам себя после успешного завершения.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def _fmt_duration(sec: float) -> str:
    s = int(sec)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h}ч")
    if m:
        parts.append(f"{m}м")
    if s or not parts:
        parts.append(f"{s}с")
    return " ".join(parts)


def _patch_rs3(report_md: str, total_sec: float) -> str:
    """Заменить строку «Время под нагрузкой (RUN_STATE=3)»."""
    new_val = _fmt_duration(total_sec)
    return re.sub(
        r"(\| Время под нагрузкой \(RUN_STATE=3\) \| )([^|]+?)( \|)",
        lambda m: f"{m.group(1)}{new_val}{m.group(3)}",
        report_md,
    )


def _insert_rs0_line(report_md: str, total_sec: float) -> str:
    """Вставить строку «Время в останове (RUN_STATE=0)» после строки RS=3."""
    new_line = f"| Время в останове (RUN_STATE=0) | {_fmt_duration(total_sec)} |"
    # Если строка уже есть — обновить
    if "Время в останове (RUN_STATE=0)" in report_md:
        return re.sub(
            r"(\| Время в останове \(RUN_STATE=0\) \| )([^|]+?)( \|)",
            lambda m: f"{m.group(1)}{_fmt_duration(total_sec)}{m.group(3)}",
            report_md,
        )
    # Иначе вставить после строки RS=3
    return re.sub(
        r"(\| Время под нагрузкой \(RUN_STATE=3\) \| [^|]+\|)",
        lambda m: f"{m.group(1)}\n{new_line}",
        report_md,
    )


async def main(dry_run: bool) -> None:
    from config import settings
    import asyncpg
    import json

    conn = await asyncpg.connect(settings.analytics_db_url)
    try:
        # Все сегменты у которых есть предшественник (continued_from IS NOT NULL)
        rows = await conn.fetch("""
            SELECT id, run_state, continued_from, report_md,
                   (characteristics_json->>'duration_sec')::float AS duration_sec
            FROM auto_segments
            WHERE continued_from IS NOT NULL
            ORDER BY t_start ASC
        """)

        if not rows:
            logger.info("Нет сегментов с continued_from — миграция не нужна.")
            return

        logger.info("Найдено %d сегментов с continued_from.", len(rows))
        updated = 0
        skipped = 0

        for row in rows:
            seg_id = row["id"]
            seg_rs = row["run_state"]
            seg_dur = row["duration_sec"] or 0.0
            report_md = row["report_md"]

            if not report_md:
                logger.debug("  seg %d: нет report_md — пропуск.", seg_id)
                skipped += 1
                continue

            # Накапливаем RS=3 и RS=0 время из предшественников
            inherited_rs3_sec = 0.0
            inherited_rs0_sec = 0.0
            pred_id = row["continued_from"]
            depth = 0
            while pred_id is not None and depth < 100:
                pred = await conn.fetchrow("""
                    SELECT id, run_state, continued_from,
                           (characteristics_json->>'duration_sec')::float AS duration_sec
                    FROM auto_segments WHERE id = $1
                """, pred_id)
                if not pred:
                    break
                if pred["run_state"] == 3:
                    inherited_rs3_sec += pred["duration_sec"] or 0.0
                elif pred["run_state"] == 0:
                    inherited_rs0_sec += pred["duration_sec"] or 0.0
                pred_id = pred["continued_from"]
                depth += 1

            if inherited_rs3_sec <= 0 and inherited_rs0_sec <= 0:
                logger.debug("  seg %d: нет унаследованного времени RS — пропуск.", seg_id)
                skipped += 1
                continue

            # Итоговое время для этого сегмента
            total_rs3 = inherited_rs3_sec + (seg_dur if seg_rs == 3 else 0.0)
            total_rs0 = inherited_rs0_sec + (seg_dur if seg_rs == 0 else 0.0)

            new_report_md = report_md
            if inherited_rs3_sec > 0:
                new_report_md = _patch_rs3(new_report_md, total_rs3)
            # Вставить/обновить строку RS=0 (новая строка в формате v3.7.1)
            new_report_md = _insert_rs0_line(new_report_md, total_rs0)

            if new_report_md == report_md:
                logger.debug("  seg %d: report_md не изменился — пропуск.", seg_id)
                skipped += 1
                continue

            logger.info(
                "  seg %d (RS=%d): RS3 +%s→%s | RS0 +%s→%s",
                seg_id, seg_rs,
                _fmt_duration(inherited_rs3_sec), _fmt_duration(total_rs3),
                _fmt_duration(inherited_rs0_sec), _fmt_duration(total_rs0),
            )

            if not dry_run:
                await conn.execute(
                    "UPDATE auto_segments SET report_md=$2 WHERE id=$1",
                    seg_id, new_report_md,
                )
            updated += 1

        logger.info(
            "Итого: обновлено=%d, пропущено=%d%s.",
            updated, skipped,
            " (dry-run, запись не выполнялась)" if dry_run else "",
        )

    finally:
        await conn.close()


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    if dry_run:
        logger.info("Режим dry-run: изменения в БД не записываются.")

    asyncio.run(main(dry_run))

    if not dry_run:
        script_path = Path(__file__)
        script_path.unlink()
        logger.info("Скрипт %s удалён.", script_path.name)
