"""Тест сегментации суток без агента и RAG.

Использование (из корня проекта):
    python scripts/test_segments.py --sn 6003790403 --type pcc --panel 1 --date 2025-05-19
    python scripts/test_segments.py --sn 6003790403 --type pcc --panel 1 --date 2025-05-19 --out segments.json
"""
import argparse
import asyncio
import json
import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path

# Позволяет запускать из корня проекта без установки пакета
sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


async def _run(router_sn: str, equip_type: str, panel_id: int, day: date, out_path: str | None):
    from db import source, analytics
    from knowledge.loader import load_knowledge
    from pipeline import aggregator, detector, segmenter

    day_start = datetime(day.year, day.month, day.day, 0, 0, 0, tzinfo=timezone.utc)
    day_end   = datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=timezone.utc)

    logger.info("Загрузка истории: %s/%s/%s за %s", router_sn, equip_type, panel_id, day)
    history      = await source.get_daily_history(router_sn, equip_type, panel_id, day)
    state_events = await source.get_daily_state_events(router_sn, equip_type, panel_id, day)
    logger.info("Строк истории: %d | state_events: %d", len(history), len(state_events))

    kb_path = await analytics.get_equipment_kb_path(router_sn, equip_type, panel_id)
    if kb_path:
        logger.info("KB: %s", kb_path)
        kb = load_knowledge(kb_path)
    else:
        logger.warning("kb_path не задан — анализ без базы знаний")
        kb = {"register_map": {}, "fault_bitmap_map": {}, "enum_map": {}, "operation_rules": {}}

    logger.info("Агрегация...")
    agg = aggregator.aggregate(history, kb["register_map"])

    logger.info("Детектирование...")
    anomalies = detector.detect(
        history=history,
        events=events,
        register_map=kb["register_map"],
        fault_bitmap_map=kb["fault_bitmap_map"],
        aggregates=agg,
    )

    logger.info("Наработка из state_events...")
    from pipeline import aggregator as agg_mod
    uptime_min, starts_count, intervals = agg_mod.calc_uptime_from_state_events(state_events)
    logger.info("Наработка: %d мин | пусков: %d", uptime_min, starts_count)

    logger.info("Сегментация...")
    segments = segmenter.segment(
        history=history,
        state_events=state_events,
        anomalies=anomalies,
        operation_rules=kb.get("operation_rules", {}),
        register_map=kb["register_map"],
        day_start=day_start,
        day_end=day_end,
    )

    logger.info("Сегментов: %d", len(segments))

    def _serialize(obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        raise TypeError(f"Not serializable: {type(obj)}")

    result = {
        "device": f"{router_sn}/{equip_type}/{panel_id}",
        "date": str(day),
        "history_rows": len(history),
        "anomalies_count": len(anomalies),
        "segments": segments,
    }

    out_json = json.dumps(result, ensure_ascii=False, indent=2, default=_serialize)

    if out_path:
        Path(out_path).write_text(out_json, encoding="utf-8")
        logger.info("Сохранено в %s", out_path)
    else:
        print(out_json)


def main():
    parser = argparse.ArgumentParser(description="Тест сегментации суток (Layer 1)")
    parser.add_argument("--sn",    required=True, help="router_sn оборудования")
    parser.add_argument("--type",  required=True, help="equip_type (например: pcc)")
    parser.add_argument("--panel", required=True, type=int, help="panel_id")
    parser.add_argument("--date",  required=True, help="Дата в формате YYYY-MM-DD")
    parser.add_argument("--out",   help="Файл для сохранения JSON (по умолчанию — stdout)")
    args = parser.parse_args()

    day = date.fromisoformat(args.date)
    asyncio.run(_run(args.sn, args.type, args.panel, day, args.out))


if __name__ == "__main__":
    main()
