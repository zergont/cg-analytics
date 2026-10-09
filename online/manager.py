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
import re
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
        # Гейт по машине: в работе не больше одного разбора. Отметка «разобрано»
        # (warning_analyzed_hash) ставится только готовым разбором; пока совет
        # шлюза думал 25–40 мин, планировщик каждые 2 мин считал тот же состав
        # новым и слал его снова — 09.10 ушло 13 одинаковых советов за 24 мин.
        # Ключ — (машина, уровень): медленный разбор одного уровня не держит
        # разбор другого (у них свои маршруты и модели).
        self._gate_inflight: dict[tuple[str, str], tuple[str, datetime, asyncio.Task]] = {}
        # Разобранные составы по машине: {hash: id открытой строки на момент
        # ответа}. Разбор, вернувшийся после закрытия сегмента, кладёт отметку в
        # закрытую строку — новая открытая её не получает, и без этой памяти
        # состав ушёл бы снова. Засчитывается ровно той строке, что была
        # открытой при ответе: сегмент, открытый позже, разбирает состав заново,
        # как и раньше. Id, а не время: t_start сегмента — время телеметрии,
        # оно отстаёт от часов сервера (а у ДЭС №3 бывало и впереди на 2 ч).
        self._gate_done: dict[str, dict[str, int]] = {}
        # Неудачи разбора по составам: {hash: когда}. Тот же состав — не раньше
        # паузы, а не каждые 2 мин
        self._gate_failed: dict[str, dict[str, datetime]] = {}

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
            # initialize() пропущен, но живые тревоги и эпизоды поднять надо:
            # без этого память движка пуста при непустой БД — открытые эпизоды
            # висят вечно, а на те же тревоги заводятся новые
            await engine._restore_live_state()
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
        from analytics import binding
        from config import settings, get_tz

        router_sn  = obs["router_sn"]
        equip_type = obs["equip_type"]
        panel_id   = obs["panel_id"]

        bnd = await db_analytics.get_equipment_binding(
            router_sn, equip_type, panel_id
        ) or {}
        controller_id = bnd.get("controller_id")
        engine_id = bnd.get("engine_id")
        kb_path_rel = bnd.get("kb_path")
        label = binding.describe_binding(
            controller_id=controller_id, engine_id=engine_id, kb_path=kb_path_rel
        )
        if not ((controller_id and engine_id) or kb_path_rel):
            logger.warning(
                "OnlineManager: нет привязки конфига для %s/%s/%s — пропуск",
                router_sn, equip_type, panel_id,
            )
            return None

        kb_root = settings.knowledge_base_path
        try:
            cfg = binding.build_config(
                kb_root, controller_id=controller_id, engine_id=engine_id, kb_path=kb_path_rel
            )
        except Exception as e:
            logger.error(
                "OnlineManager: ошибка загрузки AnalyticsConfig для %s: %s", label, e,
            )
            return None

        # Детерминированный справочник кодов неисправностей
        fault_ref = None
        try:
            fault_ref = binding.build_fault_ref(
                kb_root, controller_id=controller_id, engine_id=engine_id, kb_path=kb_path_rel
            )
        except Exception as e:
            logger.warning("OnlineManager: FaultRef не загружен для %s: %s", label, e)

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
        # СТОП / ПУСК с перечиткой: сегменты, по которым помним разбор, удаляются
        # и строятся заново — память гейта по машине больше не про них. Разбор,
        # который сейчас в работе, забываем: вернувшись, он увидит другой движок
        # и память не тронет (_run_gate), а составы новой машины не держит
        self._gate_done.pop(key, None)
        self._gate_failed.pop(key, None)
        self._warning_tracker.pop(key, None)
        for _ik in [k for k in self._gate_inflight if k[0] == key]:
            self._gate_inflight.pop(_ik, None)
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

                # Аварию разбирает не этот гейт, а акт: он строится у каждой
                # аварии на границе min(останов + лаг, закрытие) и заказывает
                # разбор сам (online/incident_gate.py). Здешняя минута
                # стабилизации для аварии не работала — у трети из них маску
                # снимали раньше, состав становился «норма», и отсчёт просто
                # выбрасывался вместе с разбором.
                # Отменить что-либо гейт тут всё равно не может: can_cancel
                # требует panel_severity == «норма».
                # Аварийный стоп — тот же случай, даже когда панель в детекциях
                # молчит: 1452 роняет машину в Shutdown без бита в масках, и
                # его видно только по 40013 (вид стопа). Без этой проверки гейт
                # разбирал такую аварию параллельно с актом, оба писали в одну
                # строку и перетирали warning_analyzed_hash — гейт срабатывал
                # повторно и получал право отменять посреди аварии.
                if (struct.get("panel_severity") == "авария"
                        or struct.get("stop_kind") == "EMERGENCY"):
                    self._warning_tracker.pop(key, None)
                    continue

                fault_hash = compute_fault_hash(struct)
                done_row = self._gate_done.get(key, {}).get(fault_hash)
                already_analyzed = (
                    seg.get("warning_analyzed_hash") == fault_hash
                    or (done_row is not None and done_row == seg.get("id"))
                )

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

                # Разбор этого уровня уже в работе — второй не шлём, пока первый
                # не вернётся (срок ожидания держит сам разбор, см. _run_gate).
                # Трекер не сбрасываем: состав уже выдержан, и после ответа он
                # уйдёт на ближайшем тике, если к тому времени не разобран
                ikey = (key, severity)
                inflight = self._gate_inflight.get(ikey)
                if inflight is not None and not inflight[2].done():
                    logger.debug("StatusScheduler[%s]: разбор %s в работе с %s — повтор не шлём",
                                 key, inflight[0], inflight[1].isoformat())
                    continue
                failed_at = self._gate_failed.get(key, {}).get(fault_hash)
                if (failed_at is not None
                        and (now - failed_at).total_seconds() < _GATE_RETRY_AFTER_SEC):
                    continue

                # Стабилизировались → на разбор
                logger.info(
                    "StatusScheduler[%s]: предупреждение стабильно 60с, отправляю на разбор",
                    key,
                )
                self._warning_tracker.pop(key, None)
                task = asyncio.create_task(
                    self._run_gate(key, ikey, engine, struct, fault_hash),
                    name=f"warning_gate_{key}",
                )
                self._gate_inflight[ikey] = (fault_hash, now, task)

            except Exception:
                logger.exception("StatusScheduler: ошибка для %s", key)

    async def _run_gate(self, key: str, ikey: tuple[str, str], engine,
                        struct: dict, fault_hash: str) -> None:
        """Разбор гейта с учётом «в работе».

        Итог: ok — модель ответила; removed — задачу сняли (оператор шлюза) или
        она не вернулась за срок; failed — ошибка. ok и removed — состав считается
        разобранным (снятую задачу сами не возвращаем), failed — пауза и повтор.
        Срок держит сама задача: проверка на тике не сработала бы, пока машина
        в норме, а снятие на тике тут же отправляло тот же состав снова.
        """
        try:
            try:
                outcome = await asyncio.wait_for(
                    _analyze_warning_claude(
                        engine.router_sn, engine.equip_type, engine.panel_id,
                        struct, fault_hash,
                    ),
                    _GATE_TIMEOUT_SEC,
                )
            except asyncio.TimeoutError:
                logger.warning("WarningGate[%s]: разбор %s не вернулся за %d мин — снят, "
                               "повторно не шлём до смены состава",
                               key, fault_hash, _GATE_TIMEOUT_SEC // 60)
                outcome = GATE_REMOVED
            # Машину остановили или перезапустили, пока шёл разбор: её память
            # сброшена, сегменты перестраиваются — старый ответ её не трогает
            if self._engines.get(key) is not engine:
                return
            now = datetime.now(timezone.utc)
            failed = self._gate_failed.setdefault(key, {})
            for _h in [h for h, t in failed.items()
                       if (now - t).total_seconds() >= _GATE_RETRY_AFTER_SEC]:
                failed.pop(_h, None)
            if outcome in (GATE_OK, GATE_REMOVED):
                failed.pop(fault_hash, None)
                row_id = await self._open_row_id(engine)
                if row_id is not None:
                    done = self._gate_done.setdefault(key, {})
                    done.pop(fault_hash, None)          # порядок вставки = свежесть
                    done[fault_hash] = row_id
                    while len(done) > _GATE_DONE_KEEP:
                        done.pop(next(iter(done)))
            else:
                failed[fault_hash] = now
        finally:
            cur = self._gate_inflight.get(ikey)
            if cur is not None and cur[2] is asyncio.current_task():
                self._gate_inflight.pop(ikey, None)

    async def _open_row_id(self, engine) -> int | None:
        """id открытой строки машины сейчас. Ответ мог прийти в окно цикла
        закрытия (старая открытая строка удалена, новая ещё не засеяна) —
        тогда пробуем ещё несколько раз."""
        from online import db as online_db
        for i in range(_GATE_SAVE_RETRIES + 1):
            try:
                row = await online_db.get_open_segment(
                    engine.router_sn, engine.equip_type, engine.panel_id)
            except Exception:
                row = None
            if row:
                return row.get("id")
            if i < _GATE_SAVE_RETRIES:
                await asyncio.sleep(_GATE_SAVE_RETRY_SEC)
        return None


# Итоги разбора гейта для планировщика
GATE_OK, GATE_REMOVED, GATE_FAILED = "ok", "removed", "failed"
# Разбор, не вернувшийся за этот срок, снимается и повторно не шлётся до смены
# состава (совет шлюза — 25–40 мин, с очередью дольше; пинги шлюза держат поток,
# так что сам он не оборвётся)
_GATE_TIMEOUT_SEC = 3 * 3600
# После неудачного разбора тот же состав — не раньше чем через столько
_GATE_RETRY_AFTER_SEC = 15 * 60
# Сколько разобранных составов помнить на машину
_GATE_DONE_KEEP = 32
# Повтор записи разбора, вернувшегося в окно между удалением открытой строки и
# вставкой закрытой (цикл закрытия)
_GATE_SAVE_RETRIES = 3
_GATE_SAVE_RETRY_SEC = 5.0


def _gate_outcome_for_error(exc: Exception) -> str:
    """Ошибка разбора → итог для планировщика.

    Снята на шлюзе намеренно (оператор, отмена совета) — GATE_REMOVED: сами не
    возвращаем. Остановка самого шлюза (stopped/shutdown, gateway_stopping), даже
    если клиент исчерпал повторы, и любая другая ошибка — GATE_FAILED: пауза и
    повтор.
    """
    from llm.client import LLMError, _is_gateway_stop
    if (isinstance(exc, LLMError) and exc.gateway
            and exc.code in ("stopped", "council_cancelled")
            and not _is_gateway_stop(exc)):
        return GATE_REMOVED
    return GATE_FAILED


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


async def _run_warning_gate_api(
    user_prompt: str, model: str, claude_cfg: dict,
) -> tuple[str, str, str, int, int]:
    """Гейт через Claude API: анализ + машинно-читаемый вердикт (tool_use verdict).

    Returns (analysis, decision, reason, tokens_in, tokens_out).
    """
    import anthropic
    import httpx
    from config import settings as app_settings
    from llm.router import get_prompt

    http_client = None
    try:
        if claude_cfg.get("proxy"):
            http_client = httpx.AsyncClient(proxy=claude_cfg["proxy"])
        client = anthropic.AsyncAnthropic(
            api_key=app_settings.anthropic_api_key,
            http_client=http_client,
            # SDK сам ретраит 429/5xx/сетевые ошибки с экспоненциальным backoff
            max_retries=3,
        )
        response = await client.messages.create(
            model=model,
            # Лимит из настроек Claude (веб-морда): 1024 обрезало анализ на полуслове
            max_tokens=claude_cfg["max_tokens"],
            system=get_prompt("warning_claude"),
            tools=[_VERDICT_TOOL],
            messages=[{"role": "user", "content": user_prompt}],
        )
        if response.stop_reason == "max_tokens":
            logger.warning("WarningGate: анализ обрезан по max_tokens=%s — увеличьте лимит в настройках Claude",
                           claude_cfg["max_tokens"])

        analysis = "".join(
            b.text for b in response.content if hasattr(b, "text")
        ).strip()
        decision, reason = "pass", "вердикт не вынесен (fail-open)"
        for b in response.content:
            if getattr(b, "type", "") == "tool_use" and b.name == "verdict":
                decision = str(b.input.get("decision", "pass"))
                reason   = str(b.input.get("reason", "")).strip() or "без обоснования"
                break
        return analysis, decision, reason, response.usage.input_tokens, response.usage.output_tokens
    finally:
        if http_client:
            await http_client.aclose()


# Локальная LLM не умеет tool_use — вердикт просим текстовым маркером в конце
# ответа. Приписка добавляется только на этой ветке (в код, не в редактируемый
# в веб-морде системный промпт задачи warning_claude — тот остаётся единым).
#
# Базовый промпт (общий для Claude и LLM) описывает Claude-инструмент verdict
# буквально как decision="cancel"/"pass" reason="...". Claude это трактует как
# вызов инструмента, а локальная модель без tool_use иногда копирует этот же
# синтаксис как обычный текст вместо (или вместе с) запрошенного ниже маркера
# «ВЕРДИКТ: ...» — оба варианта реально встречаются, регэксп должен ловить оба.
_LLM_VERDICT_RE = re.compile(
    r'ВЕРДИКТ\s*:\s*(?P<v1>cancel|pass)\b'
    r'|decision\s*=\s*"(?P<v2>cancel|pass)"(?:\s*,?\s*reason\s*=\s*"(?P<reason>[^"]*)")?',
    re.IGNORECASE,
)
_LLM_VERDICT_SUFFIX = (
    "\n\nСНАЧАЛА напиши краткий текстовый анализ (несколько предложений) — "
    "это обязательная часть ответа, не пропускай её. И только В САМОМ КОНЦЕ, "
    "отдельной последней строкой, добавь вердикт строго в формате "
    "«ВЕРДИКТ: cancel» или «ВЕРДИКТ: pass» (без кавычек)."
)


async def _run_warning_gate_llm(
    user_prompt: str, model: str, depth: str | None = None, meta: dict | None = None,
) -> tuple[str, str, str, int, int]:
    """Гейт через локальную LLM (Ollama/LM Studio): вердикт — текстовый маркер, не tool_use.

    Fail-open: если маркер не найден в ответе — pass, как и при ошибке Claude.
    Текст анализа никогда не теряется: если модель ответила ТОЛЬКО маркером
    (без анализа), после вырезания маркера остался бы пустой analysis — тогда
    сохраняем исходный необрезанный ответ, чтобы событие не пропадало из
    истории «немо». Токены — из usage ответа, если сервер его отдал (шлюз
    отдаёт), иначе 0.

    depth — вид разбора для градации шлюза «Горыныч»; meta — сюда клиент
    кладёт, какая голова шлюза реально ответила (для подписи и журнала гейта).
    """
    from llm.client import chat
    from llm.router import get_prompt

    system = get_prompt("warning_claude") + _LLM_VERDICT_SUFFIX
    if meta is None:
        meta = {}
    raw = (await chat(system, user_prompt, model=model or None,
                      depth=depth, meta=meta)).strip()
    usage = meta.get("usage") or {}
    t_in = int(usage.get("prompt_tokens") or 0)
    t_out = int(usage.get("completion_tokens") or 0)

    m = _LLM_VERDICT_RE.search(raw)
    if m:
        decision = (m.group("v1") or m.group("v2")).lower()
        extracted_reason = m.group("reason")
        reason   = extracted_reason.strip() if extracted_reason else "локальная модель — обоснование см. в тексте анализа"
        analysis = _LLM_VERDICT_RE.sub("", raw).strip() or raw
    else:
        decision, reason = "pass", "вердикт не вынесен (fail-open)"
        analysis = raw or "(локальная модель вернула пустой ответ)"
    return analysis, decision, reason, t_in, t_out


async def _analyze_warning_claude(
    router_sn: str, equip_type: str, panel_id: int,
    struct: dict, fault_hash: str,
) -> str:
    """Гейт предупреждений: анализирует сигнал и выносит вердикт cancel/pass.

    Провайдер и модель настраиваются ПО УРОВНЮ серьёзности (см. llm.router
    get_warning_level_route) — системный промпт единый для всех уровней.
    cancel (только для чисто аналитических предупреждений) — подавляет
    аналитику до изменения состава детекций или закрытия сегмента; pass —
    предупреждение идёт дальше. Любой исход логируется в gate_log сегмента.
    Fail-open: при ошибке/недоступности провайдера предупреждение проходит
    без отмены.

    Итог для планировщика: GATE_OK — модель ответила; GATE_REMOVED — задачу
    сняли на шлюзе (повторно её не ставим); GATE_FAILED — ошибка, повтор после
    паузы.
    """
    from corpus.settings import get_claude_settings
    from llm.router import get_warning_level_route
    from online.status_assembler import (
        build_warning_prompt, compute_analytics_hash, extract_alarm_text,
    )
    from online import db as online_db

    level    = struct.get("severity_level", "")
    route    = get_warning_level_route(level)
    provider = route.get("provider", "api")
    model    = route.get("model", "")

    logger.info("WarningGate: анализ для %s/%s/%s (hash=%s, уровень=%s, provider=%s, model=%s)",
                router_sn, equip_type, panel_id, fault_hash, level, provider,
                _gate_model_shown(provider, model, level))
    _gw_meta: dict = {}
    try:
        claude_cfg  = get_claude_settings()

        # Обогатить analytics_alarms счётчиками до передачи в промпт (свежие запросы)
        _seg_row = await online_db.get_open_segment(router_sn, equip_type, panel_id)
        _seg_id = _seg_row["id"] if _seg_row else None
        # Момент срабатывания: им адресуются записи гейта и им же штампуются.
        # Разбор длится десятки секунд, сегмент за это время может закрыться —
        # адресация «в открытую строку» уводила запись в следующий сегмент.
        _gate_ts = datetime.now(timezone.utc)
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
                    alarm["history_blind_30d_sec"]    = round(c.get("blind_window", 0))
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

        # Предыдущие разборы сегмента: новый состав тревог (сброс, кнопка) —
        # продолжение той же истории, Claude должен видеть исходную аварию
        _prev_analyses: list = []
        if _seg_row and _seg_row.get("warning_analyses"):
            import json as _json
            _wa = _seg_row["warning_analyses"]
            if isinstance(_wa, str):
                try:
                    _wa = _json.loads(_wa)
                except Exception:
                    _wa = []
            if isinstance(_wa, list):
                _prev_analyses = [x for x in _wa if isinstance(x, dict)]

        # Хронология эпизодов сегмента (fail-open: не критична для разбора)
        _timeline: list = []
        if _seg_row and _seg_row.get("t_start"):
            try:
                _timeline = await online_db.get_episodes_overlapping(
                    router_sn, equip_type, panel_id,
                    _seg_row["t_start"], datetime.now(timezone.utc),
                )
            except Exception:
                logger.warning("WarningGate: хронология эпизодов не получена", exc_info=True)

        user_prompt = build_warning_prompt(
            struct,
            trip_context=_trip_ctx,
            prev_analyses=_prev_analyses,
            episode_timeline=_timeline,
        )
        can_cancel  = struct.get("panel_severity", "норма") == "норма"

        if provider == "llm":
            from llm.router import depth_for_gate
            analysis, decision, reason, tokens_in, tokens_out = await _run_warning_gate_llm(
                user_prompt, model, depth=depth_for_gate(level), meta=_gw_meta,
            )
        else:
            analysis, decision, reason, tokens_in, tokens_out = await _run_warning_gate_api(
                user_prompt, model, claude_cfg,
            )
        # Подпись и журнал — по реально ответившей модели (у шлюза это голова,
        # а не имя из маршрута уровня)
        from llm.client import gateway_label
        model = gateway_label(_gw_meta, model)
        if analysis:
            from llm.router import format_ai_signature
            analysis = analysis + format_ai_signature(model)

        # Отмена допустима только для чисто аналитического предупреждения
        applied = decision == "cancel" and can_cancel
        if decision == "cancel" and not can_cancel:
            logger.warning("WarningGate: cancel отклонён — активны сигналы панели (%s/%s/%s)",
                           router_sn, equip_type, panel_id)

        if applied:
            _supp_hash = compute_analytics_hash(struct.get("analytics_alarms", []))
            if not await online_db.set_segment_gate_suppression(
                router_sn, equip_type, panel_id,
                suppressed_hash=_supp_hash,
                segment_id=_seg_id, ts=_gate_ts,
            ):
                logger.warning("WarningGate: вердикт «отменить» не записан — сегмент "
                               "на %s не найден (%s/%s/%s)",
                               _gate_ts.isoformat(), router_sn, equip_type, panel_id)
            # Пока шёл разбор, сегмент мог закрыться: вердикт лёг в закрытый, а
            # новая открытая строка подавления не получила (хвост переносится при
            # закрытии, до ответа) — повторной проверки тоже не будет, состав
            # разобран. Дописываем вердикт в текущую открытую строку.
            try:
                _cur = await online_db.get_open_segment(router_sn, equip_type, panel_id)
                if _cur and _cur.get("id") != _seg_id:
                    await online_db.set_segment_gate_suppression(
                        router_sn, equip_type, panel_id,
                        suppressed_hash=_supp_hash, segment_id=_cur["id"],
                        ts=datetime.now(timezone.utc),
                    )
            except Exception:
                logger.warning("WarningGate: вердикт не перенесён в новую открытую строку",
                               exc_info=True)
            # Эпизод живёт и меряется, но помечен: из severity исключён,
            # копим статистику ложных срабатываний для тюнинга порогов
            try:
                await online_db.set_episodes_gate_suppressed(
                    router_sn, equip_type, panel_id, _alarm_scenarios,
                )
            except Exception:
                logger.warning("WarningGate: не удалось пометить эпизоды gate_suppressed",
                               exc_info=True)

        saved = None
        if analysis:
            saved = await online_db.save_segment_warning(
                router_sn, equip_type, panel_id,
                analysis_md=analysis,
                fault_hash=fault_hash,
                alarm_text=extract_alarm_text(struct),
                segment_id=_seg_id, ts=_gate_ts,
            )
            # Разбор вернулся, когда открытая строка уже удалена циклом закрытия,
            # а закрытая ещё не вставлена — повторяем запись, а не вопрос модели
            for _ in range(_GATE_SAVE_RETRIES):
                if saved:
                    break
                await asyncio.sleep(_GATE_SAVE_RETRY_SEC)
                saved = await online_db.save_segment_warning(
                    router_sn, equip_type, panel_id,
                    analysis_md=analysis,
                    fault_hash=fault_hash,
                    alarm_text=extract_alarm_text(struct),
                    segment_id=_seg_id, ts=_gate_ts,
                )
            if not saved:
                logger.warning("WarningGate: разбор НЕ СОХРАНЁН — сегмент на %s не найден "
                               "(%s/%s/%s, %d симв.) — повтор после паузы",
                               _gate_ts.isoformat(), router_sn, equip_type, panel_id,
                               len(analysis))

        # Обязательный журнал гейта — пишется при любом исходе
        _logged = await online_db.append_segment_gate_event(
            router_sn, equip_type, panel_id,
            segment_id=_seg_id, ts=_gate_ts,
            event={
                "ts":                 _gate_ts.isoformat(),
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
                "provider":         provider,
                "model":            model,
                "tokens_in":        tokens_in,
                "tokens_out":       tokens_out,
            },
        )
        logger.info("WarningGate: %s/%s/%s — decision=%s applied=%s (%d симв. анализа, "
                    "разбор=%s, журнал=%s)",
                    router_sn, equip_type, panel_id, decision, applied, len(analysis or ""),
                    "сохранён" if saved else ("нет" if analysis else "—"),
                    "записан" if _logged else "ПОТЕРЯН")
        # Ответ есть, а записать некуда — разбор потерян: не «разобрано», а пауза
        # и повтор (иначе потерянный разбор не повторился бы до смены состава)
        return GATE_FAILED if (analysis and not saved) else GATE_OK

    except asyncio.CancelledError:
        logger.warning("WarningGate: разбор снят для %s/%s/%s (задача шлюза %s)",
                       router_sn, equip_type, panel_id, _gw_meta.get("task_id") or "—")
        raise
    except Exception as exc:
        if _gate_outcome_for_error(exc) == GATE_REMOVED:
            logger.warning("WarningGate: задачу %s сняли на шлюзе (%s, %s) — %s/%s/%s, "
                           "повторно не шлём до смены состава",
                           getattr(exc, "task_id", None) or _gw_meta.get("task_id") or "—",
                           getattr(exc, "code", None), getattr(exc, "reason", None) or "—",
                           router_sn, equip_type, panel_id)
            return GATE_REMOVED
        # Fail-open: предупреждение остаётся видимым, подавление не ставится
        logger.exception("WarningGate: ошибка для %s/%s/%s (задача шлюза %s)",
                         router_sn, equip_type, panel_id, _gw_meta.get("task_id") or "—")
        return GATE_FAILED


def _gate_model_shown(provider: str, model: str, level: str) -> str:
    """Модель гейта для журнала: у шлюза — из градации разбора, а не из маршрута
    уровня (там путь к gguf прямого сервера, шлюзу он не уходит)."""
    if provider != "llm":
        return model
    try:
        from llm.client import get_llm_settings, resolve_depth
        from llm.router import depth_for_gate
        cfg = get_llm_settings()
        if cfg.get("provider") != "gorynych":
            return model
        m, r, _ = resolve_depth(cfg, depth_for_gate(level), model, None, None)
        return f"Горыныч: {m or cfg.get('model') or '?'}" + (f" ({r})" if r else "")
    except Exception:
        return model
