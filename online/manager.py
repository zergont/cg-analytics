# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""OnlineManager — управление пулом движков онлайн-наблюдения.

Жизненный цикл движков:
  init_manager()       — при старте приложения: создать менеджер
  start_all_running()  — запустить движки для всех наблюдений со status='running'
  start_machine(...)   — ПУСК ОНЛАЙН для конкретной машины
  stop_machine(...)    — СТОП ОНЛАЙН: принудительное закрытие + остановка цикла
  stop_all()           — остановить всё при завершении приложения

Логика СТОП/ПУСК (ТЗ раздел 8.3):
  СТОП: открытый сегмент закрывается как OPERATOR_STOP, движок останавливается.
  ПУСК после СТОП: сегмент OPERATOR_STOP УДАЛЯЕТСЯ, движок перечитывает с его t_start,
    coking_risk берётся из ПРЕДШЕСТВУЮЩЕГО сегмента.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from analytics.contract import CokingRisk
from online import db as online_db
from online.engine import OnlinePollEngine, _coking_from_json, _tz_utc

logger = logging.getLogger(__name__)

_manager: "OnlineManager | None" = None


def get_manager() -> "OnlineManager":
    if _manager is None:
        raise RuntimeError("OnlineManager не инициализирован")
    return _manager


def init_manager() -> "OnlineManager":
    global _manager
    _manager = OnlineManager()
    return _manager


async def stop_manager() -> None:
    global _manager
    if _manager:
        await _manager.stop_all()
        _manager = None


class OnlineManager:
    def __init__(self) -> None:
        self._engines: dict[str, OnlinePollEngine] = {}
        self._tasks:   dict[str, asyncio.Task]     = {}
        self._status_task: asyncio.Task | None     = None
        self._history_sync: "HistorySyncWorker | None" = None
        # {key: (fault_hash, first_seen_at)} — трекер стабилизации предупреждений
        self._warning_tracker: dict[str, tuple[str, datetime]] = {}

    # ── Запуск всех активных наблюдений ───────────────────────────────────────

    async def start_all_running(self) -> None:
        """Запустить движки для всех наблюдений со status='running'."""
        observations = await online_db.list_observations()
        running = [o for o in observations if o.get("status") == "running"]
        logger.info("OnlineManager: активных наблюдений %d", len(running))
        seen: set[str] = set()
        for obs in running:
            key = f"{obs['router_sn']}|{obs['equip_type']}|{obs['panel_id']}"
            if key in seen:
                logger.warning("OnlineManager: пропуск дублирующейся записи %s", key)
                continue
            seen.add(key)
            try:
                engine = await self._build_engine(obs)
                if engine is None:
                    continue
                await engine.initialize(obs["start_date"], allow_gap_fill=False)
                self._launch(engine)
            except Exception:
                logger.exception(
                    "OnlineManager: ошибка старта движка %s/%s/%s",
                    obs["router_sn"], obs["equip_type"], obs["panel_id"],
                )

        # Запустить планировщик статус-строк (ИИ-оператор Уровень 1)
        self._status_task = asyncio.create_task(
            self._run_status_scheduler(), name="status_line_scheduler"
        )
        logger.info("OnlineManager: планировщик статус-строк запущен")

        # Запустить синхронизацию history из источника
        from online.history_sync import HistorySyncWorker
        self._history_sync = HistorySyncWorker(interval_sec=30)
        self._history_sync.start()

    # ── ПУСК ОНЛАЙН ───────────────────────────────────────────────────────────

    async def start_machine(
        self,
        router_sn: str,
        equip_type: str,
        panel_id: int,
        start_date: datetime,
        poll_interval_sec: int = 30,
    ) -> None:
        """ПУСК ОНЛАЙН.

        Логика (ТЗ 8.3):
        - Если последний сегмент — OPERATOR_STOP: удалить его,
          взять coking_risk из предшествующего, продолжить с t_start удалённого сегмента.
        - Иначе: обычное возобновление (initialize из последнего закрытого).
        """
        key = f"{router_sn}|{equip_type}|{panel_id}"

        # Остановить уже работающий движок если есть
        if key in self._engines:
            await self._stop_engine(key)

        # batch_end_ts = момент нажатия «Пуск» (фиксируется один раз, не обновляется при resume)
        await online_db.upsert_observation({
            "router_sn":         router_sn,
            "equip_type":        equip_type,
            "panel_id":          panel_id,
            "start_date":        _tz_utc(start_date),
            "status":            "running",
            "poll_interval_sec": poll_interval_sec,
            "batch_end_ts":      datetime.now(timezone.utc),
        })

        obs = await online_db.get_observation(router_sn, equip_type, panel_id)
        engine = await self._build_engine(obs)
        if engine is None:
            raise RuntimeError(
                f"Нет kb_path для {router_sn}/{equip_type}/{panel_id} — "
                "укажите путь в настройках оборудования."
            )

        # Обработать сценарий OPERATOR_STOP → перечитка (ТЗ 8.3)
        last_closed = await online_db.get_last_closed_segment(router_sn, equip_type, panel_id)
        if last_closed and last_closed.get("cause_close") == "OPERATOR_STOP":
            op_stop_t_start = _tz_utc(last_closed["t_start"])
            op_stop_id = last_closed["id"]
            # Взять coking_risk из ПРЕДШЕСТВУЮЩЕГО сегмента
            prev_seg = await online_db.get_segment_before(
                router_sn, equip_type, panel_id, op_stop_t_start
            )
            prev_coking = _coking_from_json(
                prev_seg.get("coking_risk_json") if prev_seg else None
            )
            # Удалить OPERATOR_STOP сегмент
            await online_db.delete_segment_by_id(op_stop_id)
            # Настроить состояние движка вручную (без initialize)
            engine.cursor_ts = op_stop_t_start
            engine.inherited_coking_risk = prev_coking
            engine.forward_fill_memory = None
            engine.continued_from_id = None
            logger.info(
                "OnlineManager[%s]: ПУСК после СТОП — перечитка с %s, coking=%s",
                key, op_stop_t_start, prev_coking.risk_level,
            )
        else:
            await engine.initialize(_tz_utc(start_date), allow_gap_fill=True)

        self._launch(engine)
        logger.info("OnlineManager[%s]: движок запущен", key)

    # ── СТОП ОНЛАЙН ───────────────────────────────────────────────────────────

    async def stop_machine(
        self,
        router_sn: str,
        equip_type: str,
        panel_id: int,
    ) -> None:
        """СТОП ОНЛАЙН: закрыть открытый сегмент как OPERATOR_STOP, остановить движок."""
        key = f"{router_sn}|{equip_type}|{panel_id}"
        now = datetime.now(timezone.utc)

        from analytics.runner import ANALYTICS_VERSION

        # Принудительно закрыть открытый сегмент
        await online_db.close_open_as_operator_stop(
            router_sn, equip_type, panel_id,
            t_end=now,
            analytics_version=ANALYTICS_VERSION,
        )

        # Обновить статус в БД
        await online_db.set_observation_status(router_sn, equip_type, panel_id, "stopped")

        # Остановить движок
        if key in self._engines:
            await self._stop_engine(key)

        logger.info("OnlineManager[%s]: остановлен (OPERATOR_STOP)", key)

    # ── Остановка всех ────────────────────────────────────────────────────────

    async def stop_all(self) -> None:
        # Остановить планировщик статус-строк
        if self._status_task and not self._status_task.done():
            self._status_task.cancel()
            try:
                await self._status_task
            except (asyncio.CancelledError, Exception):
                pass
        self._status_task = None

        # Остановить синхронизацию history
        if self._history_sync:
            await self._history_sync.stop()
            self._history_sync = None

        for key in list(self._engines.keys()):
            try:
                await self._stop_engine(key)
            except Exception:
                logger.exception("OnlineManager: ошибка остановки %s", key)

    # ── Внутренние методы ─────────────────────────────────────────────────────

    async def _build_engine(self, obs: dict) -> OnlinePollEngine | None:
        """Создать экземпляр OnlinePollEngine по записи из online_observations."""
        from db import analytics as db_analytics
        from analytics.config import AnalyticsConfig
        from config import settings, get_tz

        router_sn  = obs["router_sn"]
        equip_type = obs["equip_type"]
        panel_id   = obs["panel_id"]

        kb_path_rel = await db_analytics.get_equipment_kb_path(
            router_sn, equip_type, panel_id
        )
        if not kb_path_rel:
            logger.warning(
                "OnlineManager: нет kb_path для %s/%s/%s — пропуск",
                router_sn, equip_type, panel_id,
            )
            return None

        kb_path = settings.knowledge_base_path / "equipment" / kb_path_rel
        try:
            cfg = AnalyticsConfig(kb_path)
        except Exception as e:
            logger.error(
                "OnlineManager: ошибка загрузки AnalyticsConfig для %s: %s",
                kb_path_rel, e,
            )
            return None

        # Детерминированный справочник кодов неисправностей
        fault_ref = None
        try:
            from analytics.fault_ref import FaultRef
            fault_ref = FaultRef(kb_path)
        except Exception as e:
            logger.warning("OnlineManager: FaultRef не загружен для %s: %s", kb_path_rel, e)

        # engine_sn из реестра
        eq = await db_analytics.get_equipment(router_sn, equip_type, panel_id) or {}
        engine_sn = eq.get("engine_sn") or ""

        # daily_split_hour из app_settings (дефолт 9 = 09:00)
        from db.analytics import get_app_setting
        daily_hour_str = await get_app_setting("daily_split_hour", "9")
        daily_hour = int(daily_hour_str)

        engine = OnlinePollEngine(
            router_sn=router_sn,
            equip_type=equip_type,
            panel_id=panel_id,
            engine_sn=engine_sn,
            cfg=cfg,
            poll_interval_sec=obs.get("poll_interval_sec", 30),
            daily_hour=daily_hour,
            tz=get_tz(),
            fault_ref=fault_ref,
        )
        # Свежесть телеметрии переживает рестарт (иначе до первого цикла — «нет данных»)
        if obs.get("last_data_ts"):
            engine.last_data_ts = obs["last_data_ts"]
        return engine

    def _launch(self, engine: OnlinePollEngine) -> None:
        key = engine.key
        self._engines[key] = engine
        task = asyncio.create_task(engine.run(), name=f"online_{key}")
        task.add_done_callback(lambda t: self._on_task_done(key, t))
        self._tasks[key] = task
        engine._task = task

    def _on_task_done(self, key: str, task: asyncio.Task) -> None:
        self._tasks.pop(key, None)
        self._engines.pop(key, None)
        if task.cancelled():
            logger.debug("OnlineEngine[%s] задача отменена", key)
        elif task.exception():
            logger.error("OnlineEngine[%s] задача завершилась с ошибкой: %s", key, task.exception())

    async def _stop_engine(self, key: str) -> None:
        engine = self._engines.pop(key, None)
        task   = self._tasks.pop(key, None)
        if engine:
            await engine.stop()
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    # ── Публичные методы опроса состояния ────────────────────────────────────

    def is_running(self, router_sn: str, equip_type: str, panel_id: int) -> bool:
        return f"{router_sn}|{equip_type}|{panel_id}" in self._engines

    def running_keys(self) -> list[str]:
        return list(self._engines.keys())

    def get_cursor_ts(self, router_sn: str, equip_type: str, panel_id: int):
        """Вернуть cursor_ts живого движка (datetime | None)."""
        key = f"{router_sn}|{equip_type}|{panel_id}"
        engine = self._engines.get(key)
        return engine.cursor_ts if engine else None

    def get_last_processed_to(self, router_sn: str, equip_type: str, panel_id: int):
        """Куда дошёл движок в последнем цикле — для прогресс-бара."""
        key = f"{router_sn}|{equip_type}|{panel_id}"
        engine = self._engines.get(key)
        return engine.last_processed_to if engine else None

    # ── Планировщик статусов и детектор предупреждений ───────────────────────

    async def _run_status_scheduler(self) -> None:
        """Периодически обновляет детерминированный статус и детектирует предупреждения."""
        await asyncio.sleep(60)
        logger.info("StatusScheduler: первый тик")

        while True:
            try:
                from db.analytics import get_app_setting
                interval_min = int(await get_app_setting("status_line_interval_min", "1"))
            except Exception:
                interval_min = 1

            try:
                await self._tick_status_lines()
            except Exception:
                logger.exception("StatusScheduler: ошибка тика")

            await asyncio.sleep(interval_min * 60)

    async def _tick_status_lines(self) -> None:
        """Один тик: детерминированный статус + детектор новых предупреждений → Claude."""
        from online.status_assembler import (
            build_structural_status,
            compute_fault_hash, format_status_text, extract_alarm_text,
        )
        from online import db as online_db
        from db.analytics import get_app_setting

        now = datetime.now(timezone.utc)
        try:
            stale_sec = int(await get_app_setting("data_stale_threshold_sec", "90"))
        except Exception:
            stale_sec = 90

        for key, engine in list(self._engines.items()):
            try:
                # Телеметрия устарела → статус не пересчитываем (остаётся со своим
                # timestamp'ом), гейт не запускаем — «норма от ИИ» без данных подрывает доверие
                if engine.last_data_ts is not None and (
                    (now - engine.last_data_ts).total_seconds() > stale_sec
                ):
                    logger.debug("StatusScheduler[%s]: данные устарели (%.0fс) — пропуск",
                                 key, (now - engine.last_data_ts).total_seconds())
                    continue

                seg = await online_db.get_open_segment(
                    engine.router_sn, engine.equip_type, engine.panel_id
                )
                if not seg:
                    continue

                struct = build_structural_status(seg, engine._fault_ref, engine.tz)

                # ── Детерминированный статус — пишем каждый тик ──
                await online_db.update_open_segment_status(
                    engine.router_sn, engine.equip_type, engine.panel_id,
                    status_text=format_status_text(struct),
                    status_struct={
                        "run_state":            struct.get("run_state"),
                        "mode_label":           struct.get("mode_label"),
                        "time_in_mode_sec":     struct.get("time_in_mode_sec"),
                        "alarm_text":           extract_alarm_text(struct),
                        "analytics_suppressed": struct.get("analytics_suppressed", False),
                    },
                )

                # ── Детектор предупреждений → Claude ──
                severity = struct["severity_level"]
                if severity == "норма":
                    self._warning_tracker.pop(key, None)
                    continue

                fault_hash = compute_fault_hash(struct)
                already_analyzed = seg.get("warning_analyzed_hash") == fault_hash

                if already_analyzed:
                    continue

                # Трекер стабилизации: ждём 1 минуту без изменения fault-кодов
                prev = self._warning_tracker.get(key)
                if prev is None or prev[0] != fault_hash:
                    self._warning_tracker[key] = (fault_hash, now)
                    logger.info(
                        "StatusScheduler[%s]: новые fault-коды (hash=%s), ждём стабилизации",
                        key, fault_hash,
                    )
                    continue

                _, first_seen = prev
                if (now - first_seen).total_seconds() < 60:
                    continue  # ещё ждём стабилизации

                # Стабилизировались → отправляем в Claude
                logger.info(
                    "StatusScheduler[%s]: предупреждение стабильно 60с, отправляю в Claude",
                    key,
                )
                self._warning_tracker.pop(key, None)
                asyncio.create_task(
                    _analyze_warning_claude(
                        engine.router_sn, engine.equip_type, engine.panel_id,
                        struct, fault_hash,
                    ),
                    name=f"warning_claude_{key}",
                )

            except Exception:
                logger.exception("StatusScheduler: ошибка для %s", key)


# Инструмент вердикта гейта: машинно-читаемое решение вместо парсинга текста
_VERDICT_TOOL = {
    "name": "verdict",
    "description": (
        "Вердикт по предупреждению: cancel — срабатывание аналитики не отражает "
        "реальной угрозы (допустимо только без сигналов панели), pass — пропустить дальше."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "decision": {"type": "string", "enum": ["cancel", "pass"]},
            "reason":   {"type": "string", "description": "Краткое обоснование (1-2 предложения)"},
        },
        "required": ["decision", "reason"],
    },
}


async def _analyze_warning_claude(
    router_sn: str, equip_type: str, panel_id: int,
    struct: dict, fault_hash: str,
) -> None:
    """Гейт предупреждений: Claude анализирует сигнал и выносит вердикт.

    cancel (только для чисто аналитических предупреждений) — подавляет аналитику
    до изменения состава детекций или закрытия сегмента; pass — предупреждение
    идёт дальше. Любой исход логируется в gate_log сегмента. Fail-open: при
    ошибке/недоступности Claude предупреждение проходит без отмены.
    """
    import anthropic
    from corpus.settings import get_claude_settings
    from config import settings as app_settings
    from llm.router import get_prompt
    from online.status_assembler import build_warning_prompt, compute_analytics_hash
    from online import db as online_db

    logger.info("WarningGate: анализ для %s/%s/%s (hash=%s)",
                router_sn, equip_type, panel_id, fault_hash)
    http_client = None
    try:
        import httpx
        claude_cfg  = get_claude_settings()

        # Обогатить analytics_alarms счётчиками до передачи в промпт (свежие запросы)
        _seg_row = await online_db.get_open_segment(router_sn, equip_type, panel_id)
        _seg_id = _seg_row["id"] if _seg_row else None
        _run_origin_ts = None
        if _seg_id:
            try:
                _run_origin_ts = await online_db.get_run_state_origin_ts(_seg_id)
            except Exception:
                pass

        _alarm_scenarios = [
            a.get("scenario") for a in struct.get("analytics_alarms", []) if a.get("scenario")
        ]
        if _alarm_scenarios:
            try:
                _counts = await online_db.count_episodes_batch(
                    router_sn, equip_type, panel_id, _alarm_scenarios, 30, _run_origin_ts
                )
            except Exception:
                logger.warning("WarningGate: счётчики эпизодов не получены", exc_info=True)
                _counts = {}  # fail-open: счётчик не критичен
            for alarm in struct.get("analytics_alarms", []):
                sc = alarm.get("scenario")
                c = _counts.get(sc)
                if c:
                    # Текущий эпизод уже в alarm_episodes — без +1
                    alarm["history_count_30d"]        = c["count_window"]
                    alarm["history_duration_30d_sec"] = round(c["dur_window"])
                    if _run_origin_ts is not None:
                        alarm["startup_count"]        = c["count_since"]
                        alarm["startup_duration_sec"] = round(c["dur_since"])

        # Контекст аварии с SHUTDOWN-эпизода (Фаза C) — если панель в аварийном останове
        _trip_ctx = None
        if struct.get("panel_severity") == "авария":
            try:
                import json as _json
                for _e in await online_db.get_open_episodes(router_sn, equip_type, panel_id):
                    if _e["scenario"] == "CONTROLLER_FAULT" and _e.get("context_json"):
                        _trip_ctx = _e["context_json"]
                        if isinstance(_trip_ctx, str):
                            _trip_ctx = _json.loads(_trip_ctx)
                        break
            except Exception:
                logger.warning("WarningGate: контекст аварии не получен", exc_info=True)
                _trip_ctx = None

        user_prompt = build_warning_prompt(struct, trip_context=_trip_ctx)
        can_cancel  = struct.get("panel_severity", "норма") == "норма"

        # API ходит через прокси из настроек Claude (как corpus/agent и playground)
        if claude_cfg.get("proxy"):
            http_client = httpx.AsyncClient(proxy=claude_cfg["proxy"])
        client = anthropic.AsyncAnthropic(
            api_key=app_settings.anthropic_api_key,
            http_client=http_client,
            # SDK сам ретраит 429/5xx/сетевые ошибки с экспоненциальным backoff
            max_retries=3,
        )
        response = await client.messages.create(
            model=claude_cfg["model"],
            max_tokens=1024,
            system=get_prompt("warning_claude"),
            tools=[_VERDICT_TOOL],
            messages=[{"role": "user", "content": user_prompt}],
        )

        analysis = "".join(
            b.text for b in response.content if hasattr(b, "text")
        ).strip()
        decision, reason = "pass", "вердикт не вынесен (fail-open)"
        for b in response.content:
            if getattr(b, "type", "") == "tool_use" and b.name == "verdict":
                decision = str(b.input.get("decision", "pass"))
                reason   = str(b.input.get("reason", "")).strip() or "без обоснования"
                break

        # Отмена допустима только для чисто аналитического предупреждения
        applied = decision == "cancel" and can_cancel
        if decision == "cancel" and not can_cancel:
            logger.warning("WarningGate: cancel отклонён — активны сигналы панели (%s/%s/%s)",
                           router_sn, equip_type, panel_id)

        if applied:
            await online_db.set_segment_gate_suppression(
                router_sn, equip_type, panel_id,
                suppressed_hash=compute_analytics_hash(struct.get("analytics_alarms", [])),
            )
            # Эпизод живёт и меряется, но помечен: из severity исключён,
            # копим статистику ложных срабатываний для тюнинга порогов
            try:
                await online_db.set_episodes_gate_suppressed(
                    router_sn, equip_type, panel_id, _alarm_scenarios,
                )
            except Exception:
                logger.warning("WarningGate: не удалось пометить эпизоды gate_suppressed",
                               exc_info=True)

        if analysis:
            await online_db.update_open_segment_warning(
                router_sn, equip_type, panel_id,
                analysis_md=analysis,
                fault_hash=fault_hash,
            )

        # Обязательный журнал гейта — пишется при любом исходе
        await online_db.append_segment_gate_event(
            router_sn, equip_type, panel_id,
            event={
                "ts":                 datetime.now(timezone.utc).isoformat(),
                "fault_hash":         fault_hash,
                "severity_level":     struct.get("severity_level"),
                "panel_severity":     struct.get("panel_severity"),
                "analytics_severity": struct.get("analytics_severity"),
                "alarms": [
                    {"scenario": a.get("scenario"), "severity": a.get("severity"),
                     "fault_codes": a.get("fault_codes"), "description": a.get("description")}
                    for a in struct.get("panel_alarms", []) + struct.get("analytics_alarms", [])
                ],
                "decision":         decision,
                "decision_applied": applied,
                "reason":           reason,
                "model":            claude_cfg["model"],
                "tokens_in":        response.usage.input_tokens,
                "tokens_out":       response.usage.output_tokens,
            },
        )
        logger.info("WarningGate: %s/%s/%s — decision=%s applied=%s (%d симв. анализа)",
                    router_sn, equip_type, panel_id, decision, applied, len(analysis))

    except Exception:
        # Fail-open: предупреждение остаётся видимым, подавление не ставится
        logger.exception("WarningGate: ошибка для %s/%s/%s",
                         router_sn, equip_type, panel_id)
    finally:
        if http_client:
            await http_client.aclose()
