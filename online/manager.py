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
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from analytics.contract import CokingRisk
from config import settings
from db import remote
from online import db as online_db
from online.engine import OnlinePollEngine, _coking_from_json, _enqueue_segment, _tz_utc

logger = logging.getLogger(__name__)

# Класс datetime на момент импорта: тесты подменяют manager.datetime своим
# подклассом, а проверки типа надзора должны принимать настоящие метки
_DT = datetime

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

        # ── Надзор за движками (v5.1.24, только память) ──
        # 17.10 движок ДГУ №2 завис в asyncpg и молчал 4 сут 20 ч: надзора не
        # было, отмену задачи писали в DEBUG. Сторож поднимает ТОЛЬКО зависшую
        # машину тем же путём, что старт сервиса
        # Машины, у которых должен работать движок (status='running')
        self._wanted: set[str] = set()
        # СТОП / ПУСК / перезапуск одной машины — по очереди
        self._key_locks: dict[str, asyncio.Lock] = {}
        self._lock_since: dict[str, tuple[float, str]] = {}   # monotonic взятия, кто держит
        # Перезапуски зависших движков (monotonic) — лимит max_restarts за окно
        self._restart_times: dict[str, deque] = {}
        # Для API: {count, last_ts, last_reason}
        self._restart_info: dict[str, dict] = {}
        # Почему движка нет при running: (вид, текст). Вид: failed — старт или
        # подъём упал (повтор с растущей паузой); dead — задача завершилась сама;
        # limit — исчерпан лимит перезапусков; zombie — старая задача жива
        self._down: dict[str, tuple[str, str]] = {}
        # Неудачные подъёмы подряд и когда можно повторить (monotonic)
        self._fail_streak: dict[str, int] = {}
        self._next_try: dict[str, float] = {}
        # Старая задача не завершилась после отмены: новый движок — только после неё
        self._zombies: dict[str, asyncio.Task] = {}
        # live_ok_at последнего движка машины — переживает перезапуск и окно ПУСК
        self._last_live_ok: dict[str, datetime] = {}
        # Он же — от настоящего обновления живого анализа, а не момент ПУСК / старта (W7)
        self._live_real: dict[str, bool] = {}
        # Эпоха памяти гейта: +1 при СТОП / ПУСК (сторож её не трогает)
        self._mem_epoch: dict[str, int] = {}
        # Журнал сторожа без повторов: (ключ, вид) → monotonic последней записи
        self._logged: dict[tuple[str, str], float] = {}
        # Долгий цикл, пока идёт (К8): ключ → (начало цикла, последний WARNING)
        self._cycle_warned: dict[str, tuple[float, float]] = {}
        # Жёсткий потолок (Р6): ключ → (курсор, обработано по, цикл, с какого момента)
        self._progress_mark: dict[str, tuple] = {}
        # Устаревшие данные в статус-тике: ключ → с какого момента (одно WARNING)
        self._stale_since: dict[str, datetime] = {}
        # Машины, у которых идёт перезапуск сторожем (повтор не создаётся)
        self._restarting: set[str] = set()
        # Эпизод, уже описанный в журнале, когда сторож движок не трогает (выключен;
        # потолок при исчерпанном лимите; долгая нарезка в потоке): ключ →
        # (вид, метка продвижения). Раз на эпизод: эпизод «stall» закрывает свежий
        # пульс, эпизод потолка — продвижение или конец цикла (ревью v5.1.24, W5)
        self._diag_open: dict[str, tuple] = {}
        self._watchdog_task: asyncio.Task | None = None
        self._restart_tasks: set[asyncio.Task] = set()
        self._stopping = False

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
            # Движок должен работать, даже если старт упадёт — в том числе в
            # _build_engine (локальная БД перезапускается вместе с сервисом, как
            # 22–23.10): такую машину поднимет сторож (ревью v5.1.24, W4)
            self._wanted.add(key)
            try:
                engine = await self._build_engine(obs)
                if engine is None:
                    self._wanted.discard(key)
                    self._down[key] = ("needs_start", _NO_BINDING_TEXT)
                    continue
                await engine.initialize(obs["start_date"], allow_gap_fill=False)
                self._launch(engine)
            except Exception as e:
                self._mark_failed(key, f"ошибка старта: {type(e).__name__}: {e}"[:200])
                logger.exception(
                    "OnlineManager: ошибка старта движка %s/%s/%s — сторож повторит",
                    obs["router_sn"], obs["equip_type"], obs["panel_id"],
                )

        # Напоминание снять ключ переходного месяца v5.1.21 (решение В5): ключ
        # analytics_counts_since старше 35 дней — предупреждение, раз на значение
        try:
            from online.engine import analytics_counts_since, warn_stale_counts_since
            _since_seen: set = set()
            for _eng in self._engines.values():
                _s = analytics_counts_since(_eng.cfg)
                if _s is not None and _s not in _since_seen:
                    _since_seen.add(_s)
                    warn_stale_counts_since(_eng.cfg)
        except Exception:
            logger.warning("OnlineManager: срок ключа analytics_counts_since не проверен",
                           exc_info=True)

        # Запустить планировщик статус-строк (ИИ-оператор Уровень 1)
        self._status_task = asyncio.create_task(
            self._run_status_scheduler(), name="status_line_scheduler"
        )
        logger.info("OnlineManager: планировщик статус-строк запущен")

        # Запустить синхронизацию history из источника
        from online.history_sync import HistorySyncWorker
        self._history_sync = HistorySyncWorker(interval_sec=30)
        self._history_sync.start()

        # Сторож движков (v5.1.24)
        self._start_watchdog()

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
        if self._stopping:
            raise RuntimeError("Сервис останавливается — ПУСК не выполнен, повторите после старта")
        # Под замком машины и со сроком (v5.1.24): ПУСК, СТОП и перезапуск
        # сторожем — по очереди; зависание локальной БД не держит замок вечно
        try:
            async with self._key_section(key, "ПУСК"):
                await self._start_machine_locked(key, router_sn, equip_type, panel_id,
                                                 start_date, poll_interval_sec)
        except BaseException as e:
            # Машина вне надзора (Р8) — до нового ПУСК: API скажет «нужен ПУСК», а
            # не «сторож перезапускает» (ревью v5.1.24, W9)
            if key not in self._wanted and not isinstance(e, asyncio.CancelledError):
                self._down[key] = ("needs_start",
                                   f"ПУСК не выполнен: {type(e).__name__}: {e}"[:200])
            raise

    async def _start_machine_locked(self, key: str, router_sn: str, equip_type: str,
                                    panel_id: int, start_date: datetime,
                                    poll_interval_sec: int) -> None:
        # Старая задача машины не завершилась после отмены — второй движок на ту
        # же машину не запускаем (две записи в одну строку)
        zombie = self._zombies.get(key)
        if zombie is not None and not zombie.done():
            raise RuntimeError(
                f"Предыдущий движок {key} не завершился за "
                f"{settings.watchdog_cancel_wait_sec:.0f} с после отмены — повторите позже")
        # Ошибка ПУСК должна дойти до оператора, а не подниматься сторожем молча
        # через initialize без перечитки (Р8): ключ — в «должен работать» только
        # после удачного запуска
        self._wanted.discard(key)
        self._last_live_ok[key] = datetime.now(timezone.utc)
        self._live_real[key] = False

        # Остановить уже работающий движок, если есть. Память гейта и эпоха —
        # сбрасываются всегда, даже если движка нет (лимит, зомби, упал старт):
        # перечитка удаляет строки, по которым помнили разбор (К5)
        if not await self._stop_engine(key):
            raise RuntimeError(
                f"Движок {key} не завершился за {settings.watchdog_cancel_wait_sec:.0f} с "
                "после отмены — повторите позже")

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

        if not self._launch(engine):
            raise RuntimeError("Сервис останавливается — движок не запущен")
        self._wanted.add(key)
        self._clear_down(key)
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
        # Сторож машину больше не поднимает — до замка: перезапуск, ждущий
        # замок, увидит это и выйдет (v5.1.24)
        was_wanted = key in self._wanted
        self._wanted.discard(key)
        async with self._key_section(key, "СТОП"):
            now = datetime.now(timezone.utc)

            from analytics.runner import ANALYTICS_VERSION

            try:
                # Принудительно закрыть открытый сегмент
                await online_db.close_open_as_operator_stop(
                    router_sn, equip_type, panel_id,
                    t_end=now,
                    analytics_version=ANALYTICS_VERSION,
                )

                # Обновить статус в БД
                await online_db.set_observation_status(router_sn, equip_type, panel_id, "stopped")
            except BaseException:
                # СТОП не прошёл — движок работает дальше и остаётся под надзором
                if was_wanted:
                    self._wanted.add(key)
                raise

            # Остановить движок. Память гейта и эпоха — всегда, даже без движка (К5)
            if not await self._stop_engine(key):
                logger.error("OnlineManager[%s]: СТОП — задача движка не завершилась за %.0f с "
                             "после отмены (остаётся до завершения; новый ПУСК — после неё)",
                             key, settings.watchdog_cancel_wait_sec)
            self._clear_down(key)

        logger.info("OnlineManager[%s]: остановлен (OPERATOR_STOP)", key)

    # ── Остановка всех ────────────────────────────────────────────────────────

    async def stop_all(self) -> None:
        """Остановка сервиса за ограниченное время (v5.1.24): сначала сторож и
        идущие перезапуски — чтобы ни один движок не поднялся посреди остановки,
        затем всё остальное параллельно, каждое ожидание со сроком."""
        t0 = time.monotonic()
        self._stopping = True
        wait = settings.watchdog_cancel_wait_sec

        # Сторож и перезапуски, которые он уже начал (К5: задача, ждущая
        # initialize, иначе запустила бы движок после обхода _engines)
        await remote.cancel_and_wait(self._watchdog_task, wait)
        self._watchdog_task = None
        restarts = [t for t in self._restart_tasks if not t.done()]
        if restarts:
            for t in restarts:
                t.cancel()
            await asyncio.wait(restarts, timeout=wait)

        async def _stop_status() -> None:
            if not await remote.cancel_and_wait(self._status_task, wait):
                logger.warning("OnlineManager: планировщик статус-строк не завершился за %.0f с",
                               wait)

        async def _stop_sync() -> None:
            if self._history_sync:
                await self._history_sync.stop()

        async def _stop_one(key: str) -> None:
            try:
                await self._stop_engine(key)
            except Exception:
                logger.exception("OnlineManager: ошибка остановки %s", key)

        keys = list(self._engines.keys())
        await asyncio.gather(_stop_status(), _stop_sync(), *(_stop_one(k) for k in keys),
                             return_exceptions=True)
        self._status_task = None
        self._history_sync = None
        logger.info("Онлайн-движки остановлены за %.1f с (движков %d)", time.monotonic() - t0,
                    len(keys))

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

    def _launch(self, engine: OnlinePollEngine) -> bool:
        """Запустить задачу движка. False — сервис останавливается (К5: движок,
        поднятый посреди остановки, работал бы на закрытых пулах)."""
        if self._stopping:
            logger.warning("OnlineManager[%s]: сервис останавливается — движок не запущен",
                           engine.key)
            return False
        key = engine.key
        # Второй движок на машину — никогда (две записи в одну строку): прежняя
        # задача должна быть снята до запуска (СТОП / ПУСК / сторож — под замком)
        old = self._tasks.get(key)
        zombie = self._zombies.get(key)
        if (old is not None and not old.done()) or (zombie is not None and not zombie.done()):
            logger.error("OnlineManager[%s]: прежняя задача движка жива — второй движок не "
                         "запускаю", key)
            return False
        self._engines[key] = engine
        task = asyncio.create_task(engine.run(), name=f"online_{key}")
        # Колбэк знает СВОЙ движок: колбэк старой задачи не снимет новый
        task.add_done_callback(lambda t, e=engine: self._on_task_done(key, t, e))
        self._tasks[key] = task
        engine._task = task
        return True

    def _on_task_done(self, key: str, task: asyncio.Task, engine=None) -> None:
        """Задача движка завершилась. Снимаем записи, только если это всё ещё
        задача машины; неожиданный конец — в журнал громко (v5.1.24: отмену
        писали в DEBUG — 17.10 зависание не оставило следа), сторож поднимет."""
        if self._tasks.get(key) is task:
            self._tasks.pop(key, None)
            if engine is None or self._engines.get(key) is engine:
                self._engines.pop(key, None)
        elif engine is not None and self._engines.get(key) is engine:
            self._engines.pop(key, None)
        if engine is not None:
            self._remember_live_ok(key, engine)
        expected = (engine is None or not getattr(engine, "_running", False)
                    or self._stopping)
        if task.cancelled():
            if expected:
                logger.debug("OnlineEngine[%s] задача отменена", key)
            else:
                self._down.setdefault(key, ("dead", "задача движка отменена неожиданно"))
                logger.warning("OnlineEngine[%s] задача движка отменена неожиданно — сторож "
                               "поднимет её", key)
            return
        exc = task.exception()
        if exc is not None:
            if not self._stopping and key in self._wanted:
                self._down[key] = ("dead", f"задача завершилась с ошибкой: "
                                           f"{type(exc).__name__}: {exc}"[:200])
            logger.error("OnlineEngine[%s] задача движка завершилась с ошибкой — сторож "
                         "поднимет её", key, exc_info=exc)
        elif not expected:
            self._down.setdefault(key, ("dead", "задача движка завершилась без остановки"))
            logger.warning("OnlineEngine[%s] задача движка завершилась без остановки — сторож "
                           "поднимет её", key)

    async def _stop_engine(self, key: str) -> bool:
        """Снять движок машины. True — задачи больше нет (False — не завершилась
        за срок после отмены: она в _zombies, новый движок только после неё).

        Память гейта и эпоха сбрасываются ВСЕГДА — даже если движка нет."""
        engine = self._engines.pop(key, None)
        task   = self._tasks.pop(key, None)
        # СТОП / ПУСК с перечиткой: сегменты, по которым помним разбор, удаляются
        # и строятся заново — память гейта по машине больше не про них. Разбор,
        # который сейчас в работе, забываем: вернувшись, он увидит другую эпоху
        # и память не тронет (_run_gate), а составы новой машины не держит
        self._mem_epoch[key] = self._mem_epoch.get(key, 0) + 1
        self._gate_done.pop(key, None)
        self._gate_failed.pop(key, None)
        self._warning_tracker.pop(key, None)
        for _ik in [k for k in self._gate_inflight if k[0] == key]:
            self._gate_inflight.pop(_ik, None)
        if engine:
            self._remember_live_ok(key, engine)
            await engine.stop()             # ожидание со сроком (OnlinePollEngine.stop)
        if task is None:
            task = getattr(engine, "_task", None) if engine is not None else None
        # Второе ожидание — тоже со сроком (К5): задача, глотающая отмену, не
        # держит СТОП / ПУСК / остановку сервиса
        if task is not None and not task.done():
            # Не завершилась после отмены: оборвать соединения source этого
            # движка (обрыв снимает и ожидания asyncpg вне помощника) и подождать ещё
            tag = getattr(engine, "_tag", None) if engine is not None else None
            if tag is not None:
                remote.terminate_stale(tag, 0)
            if not await remote.cancel_and_wait(task, settings.watchdog_cancel_wait_sec):
                self._zombies[key] = task
                return False
        zombie = self._zombies.get(key)
        if zombie is not None:
            if not zombie.done():
                return False
            self._zombies.pop(key, None)
        return True

    # ── Публичные методы опроса состояния ────────────────────────────────────

    def is_running(self, router_sn: str, equip_type: str, panel_id: int) -> bool:
        """Движок работает или должен работать (окно перезапуска сторожем):
        удаление и очистка сегментов в routes не должны проскочить без СТОПа."""
        key = f"{router_sn}|{equip_type}|{panel_id}"
        return key in self._engines or key in self._wanted

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

    # ── Надзор за движками (v5.1.24) ─────────────────────────────────────────
    #
    # 17.10 ~13:40 движок ДГУ №2 завис в asyncpg (возврат соединения ждал
    # подтверждения отмены без срока) и молчал до 22.10 09:25. Сторож смотрит
    # только память: пульс движка (начало и конец цикла, закрытое окно, каждый
    # завершённый запрос к БД). Нет пульса дольше stall_sec — ERROR со стеками
    # задач движка (где висит), отмена и подъём ТОЛЬКО этой машины тем же путём,
    # что при старте сервиса; не чаще max_restarts за окно. Потолок — на время
    # без продвижения, а не на длительность цикла: догонка месяца законно идёт
    # минуты и пульсирует.

    def _key_lock(self, key: str) -> asyncio.Lock:
        lock = self._key_locks.get(key)
        if lock is None:
            lock = self._key_locks[key] = asyncio.Lock()
        return lock

    @asynccontextmanager
    async def _key_section(self, key: str, who: str):
        """Замок машины и срок на всё, что под ним (критика v5.1.24, К4): зависшая
        локальная БД не должна держать замок вечно — иначе ПУСК и СТОП из UI
        ждали бы его без конца, а сторож пропускал бы машину навсегда."""
        lock = self._key_lock(key)
        async with lock:
            self._lock_since[key] = (time.monotonic(), who)
            limit = settings.watchdog_stall_sec
            cm = asyncio.timeout(limit)
            try:
                async with cm:
                    yield
            except TimeoutError:
                if not cm.expired():
                    raise
                logger.error("OnlineManager[%s]: %s не уложился в %.0f с — прерван (замок машины "
                             "освобождён)", key, who, limit)
                raise RuntimeError(f"{who} {key}: не уложился в {limit:.0f} с") from None
            finally:
                self._lock_since.pop(key, None)

    @staticmethod
    def _stall_limit(engine) -> float:
        poll = float(getattr(engine, "poll_interval_sec", 30) or 30)
        return max(settings.watchdog_stall_sec, 3 * poll + 60)

    def _log_due(self, key: str, kind: str, every: float) -> bool:
        """Запись сторожа не чаще раза в every секунд на (машину, вид)."""
        now_m = time.monotonic()
        last = self._logged.get((key, kind))
        if last is not None and now_m - last < every:
            return False
        self._logged[(key, kind)] = now_m
        return True

    def _limit_left(self, key: str, now_m: float | None = None) -> int:
        now_m = time.monotonic() if now_m is None else now_m
        dq = self._restart_times.get(key)
        if dq is None:
            return settings.watchdog_max_restarts
        while dq and now_m - dq[0] > settings.watchdog_restart_window_sec:
            dq.popleft()
        return settings.watchdog_max_restarts - len(dq)

    def _mark_failed(self, key: str, text: str) -> float:
        """Старт или подъём не удался: повтор с растущей паузой (Р7), без лимита
        перезапусков — тот для зависших движков."""
        n = self._fail_streak.get(key, 0) + 1
        self._fail_streak[key] = n
        delay = _RETRY_BACKOFF_SEC[min(n - 1, len(_RETRY_BACKOFF_SEC) - 1)]
        self._next_try[key] = time.monotonic() + delay
        self._down[key] = ("failed", text)
        return delay

    def _clear_down(self, key: str) -> None:
        for d in (self._down, self._fail_streak, self._next_try, self._cycle_warned,
                  self._progress_mark, self._diag_open):
            d.pop(key, None)

    def _remember_live_ok(self, key: str, engine) -> None:
        lok = getattr(engine, "live_ok_at", None)
        if isinstance(lok, _DT):
            cur = self._last_live_ok.get(key)
            if cur is None or lok > cur:
                self._last_live_ok[key] = lok
                self._live_real[key] = bool(getattr(engine, "live_ok_real", False))

    def _start_watchdog(self) -> None:
        if self._watchdog_task is not None and not self._watchdog_task.done():
            return
        self._watchdog_task = asyncio.create_task(self._run_watchdog(), name="engine_watchdog")
        self._watchdog_task.add_done_callback(self._on_watchdog_done)

    def _on_watchdog_done(self, task: asyncio.Task) -> None:
        """Надзор за самим сторожем (Р18): конец не при остановке — ERROR."""
        if self._stopping:
            return
        if task.cancelled():
            logger.error("Сторож движков: задача отменена не при остановке — надзора за "
                         "движками нет")
            return
        exc = task.exception()
        logger.error("Сторож движков: задача завершилась — надзора за движками нет",
                     exc_info=exc)

    async def _run_watchdog(self) -> None:
        logger.info(
            "Сторож движков: запущен (нет продвижения дольше %.0f с → %s, не чаще %d раз за "
            "%.0f мин; проверка раз в %.0f с)",
            settings.watchdog_stall_sec,
            "перезапуск" if settings.watchdog_enabled else "только запись в журнал (выключен)",
            settings.watchdog_max_restarts, settings.watchdog_restart_window_sec / 60,
            settings.watchdog_tick_sec)
        while True:
            await asyncio.sleep(settings.watchdog_tick_sec)
            try:
                self._watch_tick()
            except Exception:
                logger.exception("Сторож движков: ошибка тика")

    def _watch_tick(self) -> None:
        """Один тик сторожа — только память, без запросов (100 машин — доли мс)."""
        if self._stopping:
            return
        now_m = time.monotonic()
        stall = settings.watchdog_stall_sec
        # Уборщик (Р9): соединение source у любого владельца, выданное дольше,
        # чем возможно законно (слот + запрос + возврат + 60 с), — обрыв.
        # Сторож выключен — только запись в журнал
        if settings.watchdog_enabled:
            for c in remote.terminate_stale(None, remote.stale_conn_sec()):
                logger.error("Сторож движков: соединение %s (владелец %s) выдано %.0f с назад — "
                             "оборвано", c["label"], c["owner"] or "—", c["age_sec"])
        else:
            for c in remote.held(None):
                if (c["age_sec"] > remote.stale_conn_sec()
                        and self._log_due(f"{c['owner']}|{c['task']}", "conn", stall)):
                    logger.error("Сторож движков: соединение %s (владелец %s) выдано %.0f с назад "
                                 "(сторож выключен — не обрываю)", c["label"], c["owner"] or "—",
                                 c["age_sec"])
        for key in list(self._wanted):
            if key in self._restarting:
                continue
            lock = self._key_locks.get(key)
            if lock is not None and lock.locked():
                since = self._lock_since.get(key)
                if since and now_m - since[0] > stall and self._log_due(key, "lock", stall):
                    logger.error("Сторож[%s]: замок машины держит «%s» уже %.0f с", key, since[1],
                                 now_m - since[0])
                continue
            zombie = self._zombies.get(key)
            if zombie is not None:
                if not zombie.done():
                    if self._log_due(key, "zombie", stall):
                        th = remote.threads(key)
                        logger.error("Сторож[%s]: старая задача движка всё ещё не завершилась "
                                     "после отмены — новый движок не запускаю, пока она жива "
                                     "(две записи в одну машину)%s", key,
                                     f"; ждёт поток нарезки, считающий "
                                     f"{max(t['age_sec'] for t in th):.0f} с" if th else "")
                    continue
                if settings.watchdog_enabled and self._limit_left(key, now_m) > 0:
                    self._spawn_restart(key, "zombie", "старая задача движка завершилась",
                                        None, zombie)
                continue
            eng = self._engines.get(key)
            if eng is None:
                if not settings.watchdog_enabled:
                    continue
                kind, text = self._down.get(key, ("dead", "движок не работает"))
                if kind == "zombie":
                    # Записи о зомби уже нет (снята СТОП / ПУСК) — просто движка нет
                    kind = "dead"
                if kind == "failed":
                    if now_m < self._next_try.get(key, 0.0):
                        continue
                elif self._limit_left(key, now_m) <= 0:
                    continue
                self._spawn_restart(key, kind, text, None, None)
                continue
            idle = now_m - float(getattr(eng, "beat_mono", now_m))
            if idle > self._stall_limit(eng):
                self._on_stall(key, eng, "stall", f"нет продвижения {idle:.0f} с")
                continue
            # Пульс свежий — закрыт только эпизод «нет пульса»; эпизод потолка
            # (пульс есть по определению) закрывает продвижение (W5)
            self._close_episode(key, "stall")
            self._watch_cycle(key, eng, now_m)

    def _close_episode(self, key: str, *kinds: str) -> None:
        ep = self._diag_open.get(key)
        if ep is not None and ep[0] in kinds:
            self._diag_open.pop(key, None)

    def _log_episode(self, key: str, eng, episode: tuple, reason: str, action: str) -> None:
        """ERROR со стеком — один раз на эпизод: сторож движок не трогает."""
        if self._diag_open.get(key) == episode:
            return
        self._diag_open[key] = episode
        logger.error("%s", self._diag_text(key, eng, reason, action=action))

    def _on_stall(self, key: str, eng, kind: str, reason: str, mark: tuple | None = None) -> None:
        if not settings.watchdog_enabled:
            # Откат без кода: только ERROR со стеком — один раз на эпизод
            self._log_episode(key, eng, (kind, mark), reason,
                              "сторож выключен — движок не трогаю")
            return
        if kind == "ceiling" and self._limit_left(key) <= 0:
            # Пульс есть — движок жив; снять его при исчерпанном лимите хуже, чем
            # оставить: нового до конца окна не будет (ревью v5.1.24, W3)
            self._log_episode(key, eng, (kind, mark), reason, _CEILING_LIMIT_ACTION)
            return
        self._spawn_restart(key, kind, reason, eng, self._tasks.get(key), mark=mark)

    def _watch_cycle(self, key: str, eng, now_m: float) -> None:
        """Долгий цикл, пока он идёт (К8), и жёсткий потолок «пульс есть, а
        продвижения нет» (Р6). Только память.

        Продвижение — курсор, обработанное «по» или завершённый удалённый вызов
        (ревью v5.1.24, W3): холодный цикл на деградировавшем VPN законно идёт
        дольше stall_sec, курсор не двигая, — его снимал потолок, а новый движок
        начинал тот же цикл с пустым кэшем. Потолок ловит цикл, где нет ни
        курсора, ни ответов source (крутится на локальной БД). Пульс только от
        потока нарезки — не перезапуск: поток не прерывается, перезапуск лишь
        поставил бы ту же нарезку вторым потоком (W6)."""
        cs = getattr(eng, "cycle_started_mono", None)
        if cs is None:
            self._cycle_warned.pop(key, None)
            self._progress_mark.pop(key, None)
            self._close_episode(key, "ceiling", "thread")
            return
        running = now_m - cs
        stall = settings.watchdog_stall_sec
        if running > settings.watchdog_cycle_warn_sec:
            prev = self._cycle_warned.get(key)
            if prev is None or prev[0] != cs or now_m - prev[1] >= stall:
                self._cycle_warned[key] = (cs, now_m)
                logger.warning(
                    "Сторож[%s]: цикл идёт %.0f с (закрыто окон %d, курсор %s, обработано по %s, "
                    "пульс %.0f с назад)", key, running, getattr(eng, "_cycle_windows", 0),
                    _fmt_ts(getattr(eng, "cursor_ts", None)),
                    _fmt_ts(getattr(eng, "last_processed_to", None)),
                    now_m - float(getattr(eng, "beat_mono", now_m)))
        mark = _progress_of(eng)
        pm = self._progress_mark.get(key)
        if pm is None or pm[0] != mark:
            self._progress_mark[key] = (mark, now_m)
            self._close_episode(key, "ceiling", "thread")
            return
        still = now_m - pm[1]
        if running > max(stall, _catchup_estimate_sec(eng)) and still > stall:
            tag = getattr(eng, "_tag", None)
            th = remote.threads(tag) if tag is not None else []
            if th:
                self._log_episode(
                    key, eng, ("thread", mark),
                    f"цикл идёт {running:.0f} с, нарезка в потоке считает "
                    f"{max(t['age_sec'] for t in th):.0f} с, курсор не двигается {still:.0f} с",
                    "поток не прервать, перезапуск лишь поставил бы ту же нарезку вторым "
                    "потоком — движок не трогаю, нужен разбор")
                return
            self._on_stall(key, eng, "ceiling",
                           f"цикл идёт {running:.0f} с, нет продвижения {still:.0f} с — ни "
                           f"курсора, ни ответов source (пульс есть)", mark=mark)

    def _spawn_restart(self, key: str, kind: str, reason: str, engine, task,
                       mark: tuple | None = None) -> None:
        if key in self._restarting or self._stopping:
            return
        self._restarting.add(key)
        t = asyncio.create_task(self._restart_engine(key, kind, reason, engine, task, mark),
                                name=f"watchdog_{key}")
        self._restart_tasks.add(t)

        def _done(t: asyncio.Task, key: str = key) -> None:
            self._restart_tasks.discard(t)
            self._restarting.discard(key)
            if not t.cancelled() and t.exception() is not None:
                logger.error("Сторож[%s]: перезапуск упал", key, exc_info=t.exception())

        t.add_done_callback(_done)

    async def _restart_engine(self, key: str, kind: str, reason: str, engine=None,
                              task: asyncio.Task | None = None, mark: tuple | None = None) -> None:
        """Перезапуск одной машины под её замком (СТОП/ПУСК — по очереди)."""
        try:
            async with self._key_section(key, "перезапуск сторожем"):
                await self._restart_locked(key, kind, reason, engine, task, mark)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if self._stopping or key not in self._wanted:
                return
            delay = self._mark_failed(key, f"подъём не удался: {type(e).__name__}: {e}"[:200])
            logger.error("Сторож[%s]: подъём движка не удался — повтор через %.0f с", key, delay,
                         exc_info=True)

    async def _restart_locked(self, key: str, kind: str, reason: str, engine,
                              task: asyncio.Task | None, mark: tuple | None) -> None:
        if self._stopping or key not in self._wanted:
            return
        now_m = time.monotonic()
        hung = kind in ("stall", "ceiling")
        # Причина ещё в силе? Тик решал по снимку, а замок взят позже: ПУСК мог
        # успеть раньше — свежий движок не трогаем (критика v5.1.24, К3)
        if hung:
            if self._engines.get(key) is not engine:
                return
            if kind == "stall" and now_m - engine.beat_mono <= self._stall_limit(engine):
                return
            if kind == "ceiling" and (mark != _progress_of(engine)
                                      or remote.threads(getattr(engine, "_tag", None) or key)):
                return
        elif kind == "zombie":
            if (task is None or self._zombies.get(key) is not task or not task.done()
                    or key in self._engines):
                return
        else:
            if key in self._engines:
                return
            z = self._zombies.get(key)
            if z is not None and not z.done():
                return
        counted = kind != "failed"
        if counted and self._limit_left(key, now_m) <= 0:
            if kind == "ceiling":
                # Живой (пульс есть) движок при лимите не снимаем (W3)
                self._log_episode(key, engine, (kind, mark), reason, _CEILING_LIMIT_ACTION)
                return
            if hung:
                # Лимит: стек — один раз на окно, зависшую задачу снимаем, новый
                # движок не строим. Освободится окно — тик поднимет машину
                dq = self._restart_times.get(key)
                left = (settings.watchdog_restart_window_sec - (now_m - dq[0])) if dq else 0.0
                until = datetime.now(timezone.utc) + timedelta(seconds=max(0.0, left))
                if self._log_due(key, "limit", settings.watchdog_restart_window_sec):
                    logger.error("%s", self._diag_text(
                        key, engine, reason,
                        action=(f"перезапусков {settings.watchdog_max_restarts} за "
                                f"{settings.watchdog_restart_window_sec / 60:.0f} мин — больше не "
                                f"перезапускаю до {_fmt_ts(until)}, нужен разбор")))
                if await self._teardown(key, engine, task):
                    self._down[key] = ("limit", f"исчерпан лимит перезапусков ({reason})")
            return
        carried = None
        if hung:
            logger.error("%s", self._diag_text(key, engine, reason, action="перезапуск движка"))
            carried = getattr(engine, "live_ok_at", None)
            if not await self._teardown(key, engine, task):
                return
        elif kind == "zombie":
            self._zombies.pop(key, None)
            logger.warning("Сторож[%s]: старая задача движка завершилась — поднимаю машину", key)
        if counted:
            self._restart_times.setdefault(key, deque()).append(now_m)
            info = self._restart_info.setdefault(key, {"count": 0, "last_ts": None,
                                                       "last_reason": None})
            info["count"] += 1
            info["last_ts"] = datetime.now(timezone.utc)
            info["last_reason"] = reason
        if self._stopping or key not in self._wanted:
            return
        if not await self._rise(key, carried):
            return
        if counted:
            logger.warning("Сторож[%s]: движок перезапущен (%s), перезапуск №%d с запуска сервиса",
                           key, reason, self._restart_info[key]["count"])
        else:
            logger.warning("Сторож[%s]: движок запущен после неудачного старта (%s)", key, reason)

    async def _teardown(self, key: str, engine, task: asyncio.Task | None) -> bool:
        """Снять зависший движок: отмена, ожидание со сроком; не завершился —
        обрыв его соединений и ещё одно ожидание; всё ещё жив — зомби (новый
        движок только после его конца). True — задачи больше нет."""
        wait = settings.watchdog_cancel_wait_sec
        engine._running = False
        task = task or getattr(engine, "_task", None)
        done = await remote.cancel_and_wait(task, wait)
        tag = getattr(engine, "_tag", None)
        if not done and tag is not None:
            remote.terminate_stale(tag, 0)
            done = await remote.cancel_and_wait(task, wait)
        if self._engines.get(key) is engine:
            self._engines.pop(key, None)
        if task is not None and self._tasks.get(key) is task:
            self._tasks.pop(key, None)
        self._remember_live_ok(key, engine)
        if not done:
            self._zombies[key] = task
            self._down[key] = ("zombie", "задача движка не завершилась после отмены")
            logger.error("Сторож[%s]: задача движка не завершилась за %.0f с после отмены — новый "
                         "движок не запускаю, пока старая жива (две записи в одну машину)",
                         key, 2 * wait)
            return False
        if tag is not None:
            stale = remote.terminate_stale(tag, remote.stale_conn_sec())
            if stale:
                logger.warning("Сторож[%s]: оборвано соединений source, державшихся дольше "
                               "%.0f с: %d", key, remote.stale_conn_sec(), len(stale))
        return True

    async def _rise(self, key: str, carried: datetime | None) -> bool:
        """Подъём — тот же путь, что start_all_running: строка наблюдения из БД,
        _build_engine, initialize(allow_gap_fill=False), _launch."""
        r, e, p = key.rsplit("|", 2)
        obs = await online_db.get_observation(r, e, int(p))
        if not obs or obs.get("status") != "running":
            # Оператор нажал СТОП (или строки нет) — машину не поднимаем
            self._wanted.discard(key)
            self._clear_down(key)
            logger.info("Сторож[%s]: наблюдение не в работе (%s) — машину не поднимаю", key,
                        (obs or {}).get("status") or "нет строки")
            return False
        new = await self._build_engine(obs)
        if new is None:
            self._wanted.discard(key)
            self._clear_down(key)
            self._down[key] = ("needs_start", _NO_BINDING_TEXT)
            logger.warning("Сторож[%s]: нет привязки конфига — машину не поднимаю, снята с "
                           "надзора", key)
            return False
        lok = carried or self._last_live_ok.get(key)
        if isinstance(lok, _DT):
            new.live_ok_at = lok
            new.live_ok_real = self._live_real.get(key, False)
        await new.initialize(obs["start_date"], allow_gap_fill=False)
        # Остановка или СТОП, пока шёл подъём (К5) — не запускаем
        if self._stopping or key not in self._wanted:
            return False
        if not self._launch(new):
            return False
        self._clear_down(key)
        return True

    def _diag_text(self, key: str, engine, reason: str, action: str) -> str:
        """Одна запись ERROR: что видно по памяти + цепочки await всех задач движка."""
        now_m = time.monotonic()
        cs = getattr(engine, "cycle_started_mono", None)
        tag = getattr(engine, "_tag", None)
        conns = remote.held(tag) if tag is not None else []
        tasks = remote.tasks_of(tag)
        main = self._tasks.get(key) or getattr(engine, "_task", None)
        if main is not None and main not in tasks and not main.done():
            tasks.insert(0, main)
        conn_txt = (f"соединений source у машины {len(conns)}"
                    + (f" (старейшее {max(c['age_sec'] for c in conns):.0f} с)" if conns else ""))
        th = remote.threads(tag) if tag is not None else []
        if th:
            conn_txt += f", поток нарезки считает {max(t['age_sec'] for t in th):.0f} с"
        return (f"Сторож[{key}]: {reason} — {action}. "
                f"Цикл {'идёт %.0f с' % (now_m - cs) if cs is not None else 'не идёт'}, "
                f"курсор {_fmt_ts(getattr(engine, 'cursor_ts', None))}, "
                f"обработано по {_fmt_ts(getattr(engine, 'last_processed_to', None))}, "
                f"{conn_txt}. Задачи движка:\n{remote.format_task_stacks(tasks)}")

    def _engine_progress_text(self, key: str, engine, now: datetime) -> str:
        """«аналитика обновлялась 95 с назад; движок: продвигается» — для журнала
        статус-тика (только память)."""
        parts = []
        lok = getattr(engine, "live_ok_at", None)
        if isinstance(lok, _DT):
            parts.append(f"аналитика обновлялась {_fmt_dur((now - lok).total_seconds())} назад")
        beat = getattr(engine, "beat_mono", None)
        if isinstance(beat, (int, float)) and beat:
            idle = time.monotonic() - beat
            parts.append("движок: продвигается" if idle <= self._stall_limit(engine)
                         else f"движок: нет продвижения {idle:.0f} с")
        return "; ".join(parts) or "движок без учёта цикла"

    def engine_health(self, key: str) -> dict:
        """Состояние движка машины для API — только память, без БД (v5.1.24).

        state: running | restarting | stalled | zombie | restart_limit |
        not_started | stopped (Р12: один флаг engine_stalled смешивал бы их)."""
        now_m = time.monotonic()
        eng = self._engines.get(key)
        if eng is not None:
            self._remember_live_ok(key, eng)
        lock = self._key_locks.get(key)
        restarting = key in self._restarting or (lock is not None and lock.locked())
        down = self._down.get(key)
        zombie = self._zombies.get(key)
        if eng is not None:
            idle = now_m - float(getattr(eng, "beat_mono", now_m))
            state = "stalled" if (idle > self._stall_limit(eng) or key in self._diag_open) \
                else "running"
        elif zombie is not None and not zombie.done():
            state = "zombie"
        elif restarting:
            state = "restarting"
        elif down is not None and down[0] == "limit":
            state = "restart_limit"
        elif down is not None and down[0] == "failed":
            state = "not_started"
        elif key in self._wanted:
            if not settings.watchdog_enabled:
                state = "not_started"
            elif self._limit_left(key, now_m) <= 0:
                # Задача умерла или зомби завершился при исчерпанном лимите — тик
                # ждёт окна, никто не перезапускает (ревью v5.1.24, W9)
                state = "restart_limit"
            else:
                state = "restarting"
        else:
            state = "stopped"
        info = self._restart_info.get(key) or {}
        cs = getattr(eng, "cycle_started_mono", None) if eng is not None else None
        if eng is not None and getattr(eng, "live_ok_at", None) is not None:
            live_real = bool(getattr(eng, "live_ok_real", False))
        else:
            live_real = self._live_real.get(key, False)
        return {
            "state":          state,
            # Сторож поднимет машину сам: включён, машина под надзором и это не
            # эпизод, где он сознательно не трогает движок (W9)
            "auto_restart":   (settings.watchdog_enabled and key in self._wanted
                               and key not in self._diag_open),
            "live_ok_real":   live_real,
            "live_ok_at":     (getattr(eng, "live_ok_at", None) if eng is not None else None)
                              or self._last_live_ok.get(key),
            "restarts":       int(info.get("count") or 0),
            "last_restart":   ({"ts": info["last_ts"].isoformat() if info.get("last_ts") else None,
                                "reason": info.get("last_reason")} if info else None),
            "down_reason":    down[1] if down else None,
            "beat_age_sec":   (round(now_m - float(eng.beat_mono), 1)
                               if eng is not None and getattr(eng, "beat_mono", None) else None),
            "cycle_running_sec": round(now_m - cs, 1) if cs is not None else None,
            "last_cycle_sec": (round(eng.last_cycle_sec, 2)
                               if eng is not None and getattr(eng, "last_cycle_sec", None) is not None
                               else None),
            "max_cycle_sec":  (round(eng.max_cycle_sec, 2)
                               if eng is not None and getattr(eng, "max_cycle_sec", None) is not None
                               else None),
            "cycles_failed_streak": getattr(eng, "cycles_failed_streak", None) if eng else None,
            "last_cycle_error": getattr(eng, "last_cycle_error", None) if eng else None,
            "poll_interval_sec": getattr(eng, "poll_interval_sec", None) if eng else None,
        }

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
                    _age = (now - engine.last_data_ts).total_seconds()
                    if key not in self._stale_since:
                        # Вход в «устарело» — одно WARNING (v5.1.24: 17.10 было
                        # только DEBUG каждую минуту — 4 сут тишины в журнале)
                        self._stale_since[key] = now
                        logger.warning("StatusScheduler[%s]: данные устарели %.0f с — статус и "
                                       "гейт заморожены (%s)", key, _age,
                                       self._engine_progress_text(key, engine, now))
                    else:
                        logger.debug("StatusScheduler[%s]: данные устарели (%.0fс) — пропуск",
                                     key, _age)
                    continue
                _since_stale = self._stale_since.pop(key, None)
                if _since_stale is not None:
                    logger.info("StatusScheduler[%s]: данные снова свежие (перерыв %s)", key,
                                _fmt_dur((now - _since_stale).total_seconds()))

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
                        "coking":               struct.get("coking"),
                    },
                )

                # Тревога стала нарушением паспорта — снятие гейтом на неё больше
                # не действует: снять пометку «снято» с её открытого эпизода (v5.1.12)
                from online.status_assembler import passport_hard_seen
                _hard = [a.get("scenario") for a in struct.get("analytics_alarms") or []
                         if passport_hard_seen(a) and a.get("scenario")]
                if _hard:
                    try:
                        _n = await online_db.clear_episodes_gate_suppressed(
                            engine.router_sn, engine.equip_type, engine.panel_id, _hard)
                        if _n:
                            logger.info("WarningGate[%s]: нарушение паспорта — снята пометка "
                                        "«снято гейтом» с %d эпизод(ов)", key, _n)
                    except Exception:
                        logger.warning("WarningGate[%s]: пометка «снято» не снята", key,
                                       exc_info=True)

                # Просадка или заброс в окне пуска, ставшего неустойчивым после
                # снятия (окно растёт до номинал + 5 мин): снятие на них больше не
                # действует — снять пометку «снято» с их открытых эпизодов, как у
                # паспорта (ревью v5.1.23, R4). Тот же расчёт, что у запрета
                try:
                    from online.status_assembler import start_unstable_scenarios
                    _su = start_unstable_scenarios(struct.get("start"),
                                                   struct.get("analytics_alarms"))
                except Exception:
                    logger.warning("WarningGate[%s]: запрет снятия на неустойчивом пуске не "
                                   "проверен", key, exc_info=True)
                    _su = []
                if _su:
                    try:
                        _n = await online_db.clear_episodes_gate_suppressed(
                            engine.router_sn, engine.equip_type, engine.panel_id, _su)
                        if _n:
                            logger.info("WarningGate[%s]: неустойчивый пуск — снята пометка "
                                        "«снято гейтом» с %d эпизод(ов) %s", key, _n, _su)
                    except Exception:
                        logger.warning("WarningGate[%s]: пометка «снято» не снята", key,
                                       exc_info=True)

                # ── Детектор предупреждений → Claude ──
                severity = struct["severity_level"]
                if severity == "норма":
                    self._warning_tracker.pop(key, None)
                    continue
                # Только режимные замечания (без нагрузки, холостой ход) или состав
                # уже снят — гейту разбирать нечего (решение 12.10; уровень при
                # снятии держит режимное замечание, а не снятый состав)
                if ((not struct.get("analytics_alarms") or struct.get("analytics_suppressed"))
                        and struct.get("panel_severity") == "норма"):
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
                # Разобранным считается только разбор по нынешним правилам
                # (rules в записи, v5.1.12): по старым правилам — один новый разбор
                already_analyzed = (
                    (done_row is not None and done_row == seg.get("id"))
                    # Память _gate_done живёт в процессе: после перезапуска
                    # (выкладки) состав, разобранный в этой строке, уходил
                    # снова — 4 повтора в 35765. Разборы строки хранят хеш
                    or fault_hash in _analyzed_hashes(seg)
                )

                if already_analyzed:
                    # Состав уже разобран в этой строке. Если тогда его сняли, а
                    # снятие с тех пор перезаписал разбор другого состава (хеш снятия
                    # в строке один — последний), применить прежний cancel заново,
                    # без вопроса модели. Без этого дедуп разборов превращал cancel в
                    # pass: ДЭС №3 12.10 — 2 эпизода порога живыми, «ЗАМЕЧАНИЯ — 4»
                    # вместо 2 (v5.1.9)
                    if fault_hash in _cancelled_hashes(seg):
                        await self._reapply_cancel(engine, seg, struct, fault_hash)
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

    async def _reapply_cancel(self, engine, seg: dict, struct: dict, fault_hash: str) -> None:
        """Применить заново вердикт «отменить» к составу, уже снятому в этой строке.

        Условия те же, что у гейта (gate_can_cancel); если хеш снятия строки уже
        этот — ничего не делаем (защита от повтора на каждом тике).
        v5.1.22: запись вида repeat с источником — исходным разбором этого
        состава в строке, хешем снятия и id помеченных эпизодов.
        """
        from online.status_assembler import (
            GATE_RULES_VERSION, UNCANCELLABLE_SCENARIOS, compute_analytics_hash, gate_can_cancel,
            gate_hash_source, passport_hard_seen,
        )
        if struct.get("analytics_suppressed") or not gate_can_cancel(struct):
            return
        alarms = struct.get("analytics_alarms") or []
        supp = compute_analytics_hash(alarms)
        if not alarms or seg.get("gate_suppressed_hash") == supp:
            return
        r, e, p = engine.router_sn, engine.equip_type, engine.panel_id
        now = datetime.now(timezone.utc)
        # Источник повтора — исходный разбор этого состава в строке (v5.1.22:
        # #35891 — разбор 09:01:59, а не повтор 09:41)
        _src = gate_hash_source(_gate_log(seg), fault_hash) or {"ts": now.isoformat()}
        _source = {"ts": _src.get("ts"), "segment_id": _src.get("segment_id")}
        try:
            if not await online_db.set_segment_gate_suppression(
                    r, e, p, suppressed_hash=supp, segment_id=seg.get("id"), ts=now):
                return
            _ids = await online_db.set_episodes_gate_suppressed(
                r, e, p,
                [a.get("scenario") for a in alarms
                 if a.get("scenario") and a.get("scenario") not in UNCANCELLABLE_SCENARIOS
                 and not passport_hard_seen(a)],
                src={**_source, "kind": "repeat"},
            )
            await online_db.append_segment_gate_event(
                r, e, p, segment_id=seg.get("id"), ts=now,
                event={
                    "ts":                 now.isoformat(),
                    "kind":               "repeat",
                    "fault_hash":         fault_hash,
                    "suppressed_hash":    supp,
                    "source":             _source,
                    "episodes":           list(_ids or []),
                    "severity_level":     struct.get("severity_level"),
                    "panel_severity":     struct.get("panel_severity"),
                    "analytics_severity": struct.get("analytics_severity"),
                    "alarms": [
                        {"scenario": a.get("scenario"), "severity": a.get("severity"),
                         "fault_codes": a.get("fault_codes"), "description": a.get("description")}
                        for a in struct.get("panel_alarms", []) + alarms
                    ],
                    "decision":         "cancel",
                    "decision_applied": True,
                    "reason":           "повтор состава, снятого ранее в этом сегменте, — "
                                        "снятие применено без нового разбора",
                    "rules":            GATE_RULES_VERSION,
                    "provider":         "repeat",
                    "model":            None,
                    "tokens_in":        0,
                    "tokens_out":       0,
                },
            )
            logger.info("WarningGate: %s/%s/%s — повтор снятого состава %s, снятие применено "
                        "заново", r, e, p, fault_hash)
        except Exception:
            logger.warning("WarningGate: не удалось заново применить снятие %s", fault_hash,
                           exc_info=True)

    async def _run_gate(self, key: str, ikey: tuple[str, str], engine,
                        struct: dict, fault_hash: str) -> None:
        """Разбор гейта с учётом «в работе».

        Итог: ok — модель ответила; removed — задачу сняли (оператор шлюза) или
        она не вернулась за срок; failed — ошибка. ok и removed — состав считается
        разобранным (снятую задачу сами не возвращаем), failed — пауза и повтор.
        Срок держит сама задача: проверка на тике не сработала бы, пока машина
        в норме, а снятие на тике тут же отправляло тот же состав снова.
        """
        # Счётчики аналитики — с даты выкладки v5.1.21 (ключ пуст — без фильтра)
        _since = _counts_since_of(engine)
        # Эпоха памяти машины на момент заказа (v5.1.24): СТОП / ПУСК её меняют,
        # перезапуск сторожем — нет (открытая строка та же, память гейта цела)
        epoch = self._mem_epoch.get(key, 0)

        def _engine_now():
            """Отказы наследования — в текущий движок машины той же эпохи (Р11):
            после перезапуска сторожем старый объект уже не работает."""
            if self._mem_epoch.get(key, 0) != epoch:
                return None
            return self._engines.get(key) or engine

        try:
            try:
                outcome = await asyncio.wait_for(
                    _analyze_warning_claude(
                        engine.router_sn, engine.equip_type, engine.panel_id,
                        struct, fault_hash,
                        **({"analytics_since": _since} if _since is not None else {}),
                        engine=_engine_now,
                    ),
                    _GATE_TIMEOUT_SEC,
                )
            except asyncio.TimeoutError:
                logger.warning("WarningGate[%s]: разбор %s не вернулся за %d мин — снят, "
                               "повторно не шлём до смены состава",
                               key, fault_hash, _GATE_TIMEOUT_SEC // 60)
                outcome = GATE_REMOVED
            # Машину остановили или нажали ПУСК, пока шёл разбор: её память
            # сброшена, сегменты перестраиваются — старый ответ её не трогает.
            # Перезапуск сторожем эпоху не меняет: итог разбора засчитывается
            if self._mem_epoch.get(key, 0) != epoch:
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


# ── Надзор за движками: вспомогательные (v5.1.24) ─────────────────────────────

# Повтор неудачного старта или подъёма: растущая пауза, без лимита (Р7)
_RETRY_BACKOFF_SEC = (30, 60, 120, 300, 600)

# Состояния движка, при которых UI показывает «движок завис / не запущен».
# needs_start — running, но вне надзора (ПУСК не выполнен, нет привязки KB):
# сам не поднимется, нужен ПУСК (ревью v5.1.24, W9)
ENGINE_STALLED_STATES = frozenset({"stalled", "zombie", "restart_limit", "not_started",
                                   "needs_start"})

_NO_BINDING_TEXT = "нет привязки или конфига KB (см. журнал) — после настройки нужен ПУСК"
_NEEDS_START_TEXT = "движок не запущен и сторож его не поднимет — нужен ПУСК"
_CEILING_LIMIT_ACTION = ("лимит перезапусков исчерпан — живой движок (пульс есть) не снимаю, "
                         "нужен разбор")


def _progress_of(engine) -> tuple:
    """Метка продвижения для потолка: курсор, обработано «по», цикл и число
    завершённых удалённых вызовов (W3)."""
    tag = getattr(engine, "_tag", None)
    return (getattr(engine, "cursor_ts", None), getattr(engine, "last_processed_to", None),
            getattr(engine, "cycle_started_mono", None), getattr(tag, "calls", None))


def _fmt_dur(sec: float) -> str:
    """Длительность для журнала: «45 с», «11 мин», «2 ч 05 мин», «4 сут 20 ч»."""
    sec = max(0.0, float(sec))
    if sec < 90:
        return f"{sec:.0f} с"
    if sec < 3600:
        return f"{sec / 60:.0f} мин"
    if sec < 86400:
        return f"{int(sec // 3600)} ч {int(sec % 3600 // 60):02d} мин"
    return f"{int(sec // 86400)} сут {int(sec % 86400 // 3600)} ч"


def _fmt_ts(ts: "datetime | None") -> str:
    """dd.mm HH:MM:SS в часовом поясе приложения — для журнала сторожа."""
    if not isinstance(ts, _DT):
        return "—"
    try:
        from config import get_tz
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(get_tz()).strftime("%d.%m %H:%M:%S")
    except Exception:
        return ts.isoformat()


def _catchup_estimate_sec(engine) -> float:
    """Законная длительность догонки: число суточных рубежей от курсора × 60 с
    (4,9 сут — 57 с за 10 сегментов, 22.10)."""
    cur = getattr(engine, "cursor_ts", None)
    if not isinstance(cur, _DT):
        return 0.0
    if cur.tzinfo is None:
        cur = cur.replace(tzinfo=timezone.utc)
    days = max(0.0, (_DT.now(timezone.utc) - cur).total_seconds() / 86400)
    return (int(days) + 1) * 60.0


def analytics_stale_threshold(data_stale_sec: float, poll_interval_sec: float | None = None) -> int:
    """Порог «аналитика не обновляется» (критика v5.1.24, К7).

    live_ok_at ставится в конце цикла: перед обновлением лаг = сон + цикл.
    Цикл реза 09:00 бывает дольше штатного (2 из 84 резов закрыты позже
    121 с после t_end) — с порогом 90 с UI скрывал бы живой статус как раз в
    09:00. Цикл короче cycle_warn_sec «не обновляется» не считается, а длиннее
    уже даёт WARNING — журнал и UI согласованы. При опросе 30 с — 180 с."""
    poll = float(poll_interval_sec or 30)
    return int(max(float(data_stale_sec or 0), poll + settings.watchdog_cycle_warn_sec + 30))


def health_fields(mgr: "OnlineManager | None", key: str, monitoring_status: str | None,
                  now: datetime, data_stale_sec: float, poll_interval_sec: float | None = None,
                  diagnostics: bool = False, data_stale: bool = False) -> dict:
    """Поля «аналитика не обновляется» для /api/machines и /api/online/status
    (v5.1.24) — только память менеджера, без БД.

    analytics_stale ≠ data_stale: первое — живой статус не текущий по вине
    аналитики (движок завис, циклы падают, БД телеметрии недоступна), второе —
    нет свежей телеметрии по last_data_ts, который пишет сам движок. При
    analytics_stale значение data_stale недостоверно.

    data_stale — уже посчитанный маршрутом признак: при нём analytics_stale
    наступает раньше порога (ревью v5.1.24, W7) — движок не подтверждал живой
    анализ дольше порога телеметрии, либо ещё ни разу не обновил его с запуска
    сервиса / ПУСК. Иначе первые ~2 мин любого простоя аналитики и вся догонка
    после рестарта UI писал бы «Нет связи — данные на …» при панели на связи."""
    running = monitoring_status == "running"
    h = mgr.engine_health(key) if mgr is not None else None
    threshold = analytics_stale_threshold(
        data_stale_sec, (h or {}).get("poll_interval_sec") or poll_interval_sec)
    restarts = int((h or {}).get("restarts") or 0)
    last_restart = (h or {}).get("last_restart")
    out: dict[str, Any] = {
        "analytics_updated_at": None,
        "analytics_lag_sec":    None,
        "analytics_stale":      False,
        "engine_state":         "stopped",
        "engine_stalled":       False,
        "engine_auto_restart":  False,
        "engine_down_reason":   None,
        "engine_restarts":      restarts,
        "last_engine_restart":  last_restart,
    }
    if running:
        state = (h or {}).get("state") or "not_started"
        reason = (h or {}).get("down_reason")
        if state == "stopped":
            # running без движка и без надзора: ПУСК упал, нет привязки KB — сам
            # не поднимется, нужен ПУСК (W9: не «сторож повторяет»)
            state = "needs_start"
            reason = reason or _NEEDS_START_TEXT
        lok = (h or {}).get("live_ok_at")
        lag = None
        if isinstance(lok, _DT):
            if lok.tzinfo is None:
                lok = lok.replace(tzinfo=timezone.utc)
            lag = max(0, int((now - lok).total_seconds()))
        # data_stale при аналитике, которая не подтверждала живой анализ дольше
        # порога телеметрии или ни разу с запуска / ПУСК, — недостоверен (W7)
        stale_by_data = bool(data_stale) and (
            lag is None or lag > float(data_stale_sec or 0)
            or not (h or {}).get("live_ok_real", False))
        out.update({
            "analytics_updated_at": lok.isoformat() if isinstance(lok, _DT) else None,
            "analytics_lag_sec":    lag,
            # Окно ПУСК / перезапуска (движка в _engines нет) считается по лагу
            # от live_ok_at прежнего движка, а не «сразу stale» — иначе мигало бы
            # при каждом ПУСК (К7). Движка нет и не будет — stale сразу
            "analytics_stale":      (lag is None or lag > threshold or stale_by_data
                                     or state in ("not_started", "restart_limit", "zombie",
                                                  "needs_start")),
            "engine_state":         state,
            "engine_stalled":       state in ENGINE_STALLED_STATES,
            "engine_auto_restart":  bool((h or {}).get("auto_restart")),
            "engine_down_reason":   reason,
        })
    if diagnostics:
        out.update({
            "analytics_stale_threshold_sec": threshold,
            "engine_beat_age_sec":   (h or {}).get("beat_age_sec"),
            "cycle_running_sec":     (h or {}).get("cycle_running_sec"),
            "last_cycle_sec":        (h or {}).get("last_cycle_sec"),
            "max_cycle_sec":         (h or {}).get("max_cycle_sec"),
            "cycles_failed_streak":  (h or {}).get("cycles_failed_streak"),
            "last_cycle_error":      (h or {}).get("last_cycle_error"),
        })
    return out


def _gate_log(seg: dict) -> list:
    raw = seg.get("gate_log")
    if isinstance(raw, str):
        try:
            import json as _json
            raw = _json.loads(raw)
        except ValueError:
            return []
    return raw if isinstance(raw, list) else []


def _cancelled_hashes(seg: dict) -> set[str]:
    """Составы, по которым в этой строке был применён вердикт «отменить» (gate_log).
    Только по нынешним правилам гейта (v5.1.12): старые снятия заново не применяются.
    v5.1.22: основание повтора — только своё решение строки (разбор моделью и
    повтор от него); наследование, перенос и поздний вердикт — нет (критика 1:
    иначе перенос стал бы основанием для повтора и вошёл в счётчики)."""
    from online.status_assembler import GATE_OWN_KINDS, GATE_RULES_VERSION, gate_entry_kind
    return {e.get("fault_hash") for e in _gate_log(seg)
            if isinstance(e, dict) and e.get("decision") == "cancel"
            and e.get("decision_applied") and e.get("fault_hash")
            and e.get("rules") == GATE_RULES_VERSION
            and gate_entry_kind(e) in GATE_OWN_KINDS}


def _gate_mark_event(kind: str, ts: datetime, fault_hash: str | None, suppressed_hash: str | None,
                     source: dict, episodes: list[int], scenarios: list[str],
                     reason: str) -> dict:
    """Запись журнала гейта без модели (v5.1.22): наследование или поздний
    вердикт — со ссылкой на исходный разбор и id помеченных эпизодов. fault_hash
    — исходного разбора: состав панели здесь не строится."""
    from online.status_assembler import GATE_RULES_VERSION
    return {
        "ts": ts.isoformat(), "kind": kind,
        "fault_hash": fault_hash, "suppressed_hash": suppressed_hash,
        "alarms": [{"scenario": sc} for sc in sorted(set(scenarios or []))],
        "decision": "cancel", "decision_applied": True,
        "reason": reason, "rules": GATE_RULES_VERSION,
        "provider": kind, "model": None, "tokens_in": 0, "tokens_out": 0,
        "source": {"ts": (source or {}).get("ts"), "segment_id": (source or {}).get("segment_id")},
        "episodes": list(episodes or []),
    }


def _inherit_allowed(cur_row: dict | None, supp_hash: str | None) -> tuple[bool, list[str]]:
    """Снятие действует для эпизодов, открытых во время разбора (v5.1.22, ревью
    gate-1): тот же предикат suppression_active, что у статуса и движка, по уже
    прочитанной открытой строке — её детекциям на момент ответа и хешу этого
    решения. (действует, запреты панели / вид стопа)."""
    if not cur_row or not supp_hash:
        return False, []
    from online.status_assembler import _parse_json, _without_state, suppression_active, suppression_bans
    dets = _without_state(_parse_json(cur_row.get("active_detections_json")) or [])
    row = dict(cur_row, gate_suppressed_hash=supp_hash)
    return suppression_active(row, dets), suppression_bans(row, dets)


def _refuse_after(engine, scenarios: list[str], gate_ts: datetime) -> list[int]:
    """Эпизоды движка этих сценариев, открытые после момента разбора, — в
    отказанные наследованию (engine._supp_refused, v5.1.22, ревью gate-1), как
    отказывает сам движок: повтор — когда запрет снимется и состав совпадёт.
    Подпись прошлой попытки сбрасывается — первая же смена условий даст повтор
    (иначе после ухода сигнала подпись совпала бы с той, что была до него).
    Без запроса: id и начало — из памяти эпизодов движка."""
    if engine is None:
        return []
    out: list[int] = []
    try:
        eps = getattr(engine, "_episodes", None) or {}
        refused = getattr(engine, "_supp_refused", None)
        if refused is None:
            refused = engine._supp_refused = {}
        g = gate_ts if gate_ts.tzinfo else gate_ts.replace(tzinfo=timezone.utc)
        for sc in scenarios or []:
            m = eps.get(sc) or {}
            t_open, ep_id = m.get("t_open"), m.get("id")
            if ep_id is None or not isinstance(t_open, datetime):
                continue
            t = t_open if t_open.tzinfo else t_open.replace(tzinfo=timezone.utc)
            if t > g:
                refused[ep_id] = (sc, ep_id, t)
                out.append(ep_id)
        if out:
            engine._supp_refused_sig = None
    except Exception:
        logger.warning("WarningGate: отказанные эпизоды движку не переданы", exc_info=True)
    return out


def _late_carry_ok(landed_row: dict | None, cur_row: dict | None) -> bool:
    """Поздний вердикт — в текущую открытую строку (v5.1.22, P1-1 п. 3, В5):
    закрытый сегмент с вердиктом закрыт суточным резом или сменой режима, ни он,
    ни текущая строка не стоп, и текущая начинается ровно в его конце (между
    ними ничего нет). Проверять по continued_from нельзя: у посевной строки он
    пуст (engine._close_window)."""
    if not landed_row or not cur_row or landed_row.get("t_end") is None:
        return False
    if landed_row.get("cause_close") not in ("DAILY_BOUNDARY", "RUN_STATE_CHANGE"):
        return False
    if landed_row.get("run_state") == 0 or cur_row.get("run_state") == 0:
        return False
    a, b = landed_row.get("t_end"), cur_row.get("t_start")
    try:
        if not isinstance(a, datetime):
            a = datetime.fromisoformat(str(a))
        if not isinstance(b, datetime):
            b = datetime.fromisoformat(str(b))
    except (TypeError, ValueError):
        return False
    if a.tzinfo is None:
        a = a.replace(tzinfo=timezone.utc)
    if b.tzinfo is None:
        b = b.replace(tzinfo=timezone.utc)
    return a == b


def _analyzed_hashes(seg: dict) -> set[str]:
    """Хеши составов, разобранных в этой строке по нынешним правилам гейта
    (warning_analyses с rules, v5.1.12)."""
    raw = seg.get("warning_analyses")
    if isinstance(raw, str):
        try:
            import json as _json
            raw = _json.loads(raw)
        except ValueError:
            return set()
    if not isinstance(raw, list):
        return set()
    from online.status_assembler import GATE_RULES_VERSION
    # Разбор, чьё снятие не записалось (v5.1.13), — не «разобран»: после паузы
    # состав спросят снова, иначе снятие пропало бы насовсем
    return {a.get("fault_hash") for a in raw if isinstance(a, dict) and a.get("fault_hash")
            and a.get("rules") == GATE_RULES_VERSION and not a.get("not_written")}
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
        "реальной угрозы (допустимо, только если в данных написано «cancel ДОПУСТИМ»), "
        "pass — пропустить дальше."
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


def _counts_since_of(engine) -> "datetime | None":
    """С какой даты гейт считает счётчики аналитики машины (v5.1.21, В5)."""
    try:
        from online.engine import analytics_counts_since
        return analytics_counts_since(engine.cfg)
    except Exception:
        return None


async def _analyze_warning_claude(
    router_sn: str, equip_type: str, panel_id: int,
    struct: dict, fault_hash: str,
    analytics_since: "datetime | None" = None,
    engine=None,
) -> str:
    """Гейт предупреждений: анализирует сигнал и выносит вердикт cancel/pass.

    engine — движок машины (v5.1.22, ревью gate-1): эпизоды, открытые во время
    разбора, которым наследование снятия запрещено, передаются ему в отказанные.
    v5.1.24: может быть поставщиком (вызываемым) — текущий движок машины на
    момент ответа (после перезапуска сторожем — новый).

    Кто разбирает — строка таблицы «Кто и как разбирает» (llm.router.get_route:
    gate_caution / gate_warning по уровню, v5.1.11); системный промпт единый.
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
    from llm.router import claude_model_for, depth_for_gate, get_route, route_label
    from online.status_assembler import (
        build_warning_prompt, compute_analytics_hash, extract_alarm_text,
    )
    from online import db as online_db

    level    = struct.get("severity_level", "")
    kind     = depth_for_gate(level)
    provider = get_route(kind).get("provider", "llm")
    model    = route_label(kind)

    logger.info("WarningGate: анализ для %s/%s/%s (hash=%s, уровень=%s, разбирает %s)",
                router_sn, equip_type, panel_id, fault_hash, level, model)
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
                # Аналитика — с даты выкладки v5.1.21 (ALARM_EPISODES.analytics_counts_since)
                _counts = await online_db.count_episodes_batch(
                    router_sn, equip_type, panel_id, _alarm_scenarios, 30, _run_origin_ts,
                    analytics_since=analytics_since,
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
        from online.status_assembler import (
            GATE_RULES_VERSION, UNCANCELLABLE_SCENARIOS, gate_facts_md, passport_hard_seen,
            uncancellable_reasons,
        )
        reasons     = uncancellable_reasons(struct)
        can_cancel  = not reasons

        if provider == "llm":
            # Модель, рассуждение и приоритет — строка таблицы (resolve_depth по виду)
            analysis, decision, reason, tokens_in, tokens_out = await _run_warning_gate_llm(
                user_prompt, None, depth=kind, meta=_gw_meta,
            )
        else:
            analysis, decision, reason, tokens_in, tokens_out = await _run_warning_gate_api(
                user_prompt, claude_model_for(kind), claude_cfg,
            )
        # Подпись и журнал — по реально ответившей модели (у шлюза это голова,
        # а не имя из маршрута уровня)
        from llm.client import gateway_label
        model = gateway_label(_gw_meta, model)
        # Подпись — после прохода проверки языка (ниже, после записи снятия)
        _lang = None

        # Отмена допустима только при отсутствии запретов (панель, свежая
        # «Общая авария», несимметрия, нарушение паспорта — решает код, v5.1.12)
        applied = decision == "cancel" and can_cancel
        rejected_by = [r["code"] for r in reasons] if decision == "cancel" and not applied else []
        if rejected_by:
            logger.warning("WarningGate: cancel отклонён — %s (%s/%s/%s)",
                           "; ".join(r["text"] for r in reasons),
                           router_sn, equip_type, panel_id)
        _supp_written = False
        _supp_hash = None
        _own_ids: list[int] = []
        # Вид записи журнала (v5.1.22): разбор шлюзом или локальной моделью —
        # llm, Claude API — api
        _entry_kind = provider if provider in ("llm", "api") else "llm"
        if applied:
            _supp_hash = compute_analytics_hash(struct.get("analytics_alarms", []))
            # Окно цикла закрытия (строка снята, закрытая ещё не вставлена) —
            # повтор записи, а не вопрос модели (v5.1.13)
            for _i in range(_GATE_SAVE_RETRIES + 1):
                _supp_written = await online_db.set_segment_gate_suppression(
                    router_sn, equip_type, panel_id,
                    suppressed_hash=_supp_hash,
                    segment_id=_seg_id, ts=_gate_ts,
                )
                if _supp_written or _i == _GATE_SAVE_RETRIES:
                    break
                await asyncio.sleep(_GATE_SAVE_RETRY_SEC)
            if not _supp_written:
                logger.warning("WarningGate: вердикт «отменить» не записан — сегмент "
                               "на %s не найден (%s/%s/%s)",
                               _gate_ts.isoformat(), router_sn, equip_type, panel_id)
            # Снятие, которое не удалось записать ни в одну строку, — не применено:
            # журнал и разбор не врут, эпизоды не помечаются, состав разберут
            # снова после паузы (v5.1.13)
            if not _supp_written:
                applied = False
                rejected_by = ["not_written"]
                reasons = reasons + [{"code": "not_written",
                                      "text": "снятие не записано — сегмент не найден"}]
            if _supp_written:
                _hard_seen = {a.get("scenario") for a in struct.get("analytics_alarms", [])
                              if passport_hard_seen(a)}
                _cancel_sc = [sc for sc in _alarm_scenarios
                              if sc not in UNCANCELLABLE_SCENARIOS and sc not in _hard_seen]
                # Куда лёг вердикт: открытая строка (разбор успел) или закрытый
                # сегмент (строку закрыли, пока шёл разбор) — по тому же
                # (сегмент, момент), что разбор и журнал этого вызова
                _cur, _landed = None, None
                try:
                    _cur = await online_db.get_open_segment(router_sn, equip_type, panel_id)
                    _landed = (_seg_id if _cur and _cur.get("id") == _seg_id
                               else await online_db.resolve_segment_id(
                                   router_sn, equip_type, panel_id, _seg_id, _gate_ts))
                except Exception:
                    logger.warning("WarningGate: строка вердикта не определена", exc_info=True)
                _in_open = _cur is not None and _landed is not None and _landed == _cur.get("id")
                # Источник снятия (v5.1.22): момент разбора и, если он уже в
                # закрытом сегменте, его номер
                _src = {"ts": _gate_ts.isoformat(),
                        "segment_id": None if _in_open else _landed}
                # Профиль окна пуска растёт, пока шёл разбор (ревью v5.1.23, R4):
                # просадку и заброс, начавшиеся в окне ставшего неустойчивым
                # пуска, не метим — по свежей открытой строке или по закрытому
                # сегменту, куда лёг вердикт (чтение — только в позднем пути)
                try:
                    from online.status_assembler import (
                        _parse_json as _pj, start_profile_of, start_unstable_scenarios,
                    )
                    _vrow = (_cur if _in_open else
                             await online_db.get_segment_by_id(_landed) if _landed is not None else None)
                    _su_now = start_unstable_scenarios(
                        start_profile_of(_pj((_vrow or {}).get("characteristics_json"))),
                        struct.get("analytics_alarms"))
                except Exception:
                    logger.warning("WarningGate: запрет снятия на неустойчивом пуске не проверен — "
                                   "снятие как есть", exc_info=True)
                    _su_now = []
                if _su_now:
                    _cancel_sc = [sc for sc in _cancel_sc if sc not in _su_now]
                    logger.info("WarningGate: %s/%s/%s — пуск стал неустойчивым, пока шёл разбор: "
                                "%s не снимаются", router_sn, equip_type, panel_id, _su_now)
                # Эпизод живёт и меряется, но помечен: из severity исключён,
                # копим статистику ложных срабатываний для тюнинга порогов.
                # v5.1.22 (P1-1 п. 1): снятие — эпизодам, открытым на момент
                # разбора; открытые позже в этой же строке наследуют (п. 2)
                try:
                    _own_ids = await online_db.set_episodes_gate_suppressed(
                        router_sn, equip_type, panel_id, _cancel_sc,
                        until=_gate_ts, src={**_src, "kind": _entry_kind},
                    ) or []
                    # v5.1.22, ревью gate-1: эпизоды, открытые, пока шёл разбор,
                    # наследуют снятие тем же предикатом, что статус и движок, — по
                    # уже прочитанной строке (_cur, без запроса): за время разбора
                    # мог прийти сигнал панели (#35944: просадка внутри 1464) или
                    # смениться состав. Запрет — не метим, id движку в отказанные:
                    # повтор наследования — по общему правилу, когда запрет снимется
                    _inherit_ok, _bans = (_inherit_allowed(_cur, _supp_hash)
                                          if _in_open else (False, []))
                    if _in_open and _inherit_ok:
                        _inh_ids = await online_db.set_episodes_gate_suppressed(
                            router_sn, equip_type, panel_id, _cancel_sc,
                            after=_gate_ts, src={**_src, "kind": "inherited"},
                        ) or []
                        if _inh_ids:
                            _now = datetime.now(timezone.utc)
                            await online_db.append_segment_gate_event(
                                router_sn, equip_type, panel_id, segment_id=_cur["id"], ts=_now,
                                event=_gate_mark_event(
                                    "inherited", _now, fault_hash, _supp_hash, _src, _inh_ids,
                                    _cancel_sc, "эпизоды того же состава, открытые в этой строке, "
                                    "пока шёл разбор, — снятие по нему"))
                    elif _in_open:
                        _refused = _refuse_after(engine() if callable(engine) else engine,
                                                 _cancel_sc, _gate_ts)
                        if _refused:
                            logger.info("WarningGate: %s/%s/%s — эпизоды %s, открытые во время "
                                        "разбора, снятие не наследуют (%s); повтор, когда запрет "
                                        "снимется", router_sn, equip_type, panel_id, _refused,
                                        ", ".join(_bans) or "состав сменился")
                except Exception:
                    logger.warning("WarningGate: не удалось пометить эпизоды gate_suppressed",
                                   exc_info=True)
                # Пока шёл разбор, строку закрыли: вердикт лёг в закрытый сегмент.
                # В текущую открытую он попадает, только если граница без стопа —
                # суточный рез или смена режима (В5) — и снятый эпизод ещё идёт
                # (P1-1 п. 3; до v5.1.22 переносился всегда, и ответ, пришедший
                # после стопа и пуска, снимал новый пуск)
                if not _in_open and _cur is not None and _landed is not None and _own_ids:
                    try:
                        _lrow = await online_db.get_segment_by_id(_landed)
                        if _late_carry_ok(_lrow, _cur):
                            _now = datetime.now(timezone.utc)
                            if await online_db.set_segment_gate_suppression(
                                router_sn, equip_type, panel_id,
                                suppressed_hash=_supp_hash, segment_id=_cur["id"], ts=_now,
                            ):
                                await online_db.append_segment_gate_event(
                                    router_sn, equip_type, panel_id, segment_id=_cur["id"], ts=_now,
                                    event=_gate_mark_event(
                                        "late", _now, fault_hash, _supp_hash, _src, _own_ids,
                                        _cancel_sc, "вердикт разбора, вернувшегося после закрытия "
                                        "сегмента: снятый эпизод продолжается в новой строке"))
                    except Exception:
                        logger.warning("WarningGate: вердикт не перенесён в новую открытую строку",
                                       exc_info=True)
                # Вердикт лёг в уже закрытый сегмент — пересчитать его сводку: её
                # части (единый вердикт) заморожены при закрытии. Только после
                # пометки эпизодов: сегмент, закрывшийся между ними, собрал сводку
                # по непомеченным; закрывшийся после — уже по помеченным (пересчёт
                # тогда ничего не меняет). v5.1.22: только эпизоды, начавшиеся не
                # позже момента разбора, с подписью источника
                if _landed is not None and not _in_open:
                    try:
                        _changed, _lvl_changed = await online_db.apply_late_gate_suppression(
                            _landed, _alarm_scenarios, until=_gate_ts, src=_src)
                        if _changed:
                            logger.info("WarningGate: сводка закрытого сегмента %s пересчитана "
                                        "после поздней отмены%s", _landed,
                                        " — вердикт сменился, заключение перезаказано"
                                        if _lvl_changed else "")
                        if _lvl_changed:
                            _enqueue_segment(_landed, again=True)
                    except Exception:
                        logger.warning("WarningGate: сводка закрытого сегмента не пересчитана",
                                       exc_info=True)

        # Обязательный журнал гейта — пишется при любом исходе; окно цикла
        # закрытия — повтор записи (v5.1.13). v5.1.22, ревью gate-2 / incident-2:
        # сразу после записи хеша и пометки эпизодов, до прохода языка (до 240 с)
        # и сохранения разбора — перенос и наследование опираются на «своё
        # решение» строки в журнале; в окне «хеш есть, записи нет» движок снимал
        # свежий хеш как перенесённый, а рез его не переносил. Отметка проверки
        # языка — в записи разбора (extra.lang_check)
        _gate_event = {
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
                # v5.1.22: вид записи, хеш снятия и снятые эпизоды — основание
                # повтора, наследования и переноса (P1-1)
                "kind":             _entry_kind,
                "suppressed_hash":  _supp_hash if applied else None,
                "episodes":         list(_own_ids) if applied else [],
                "decision":         decision,
                "decision_applied": applied,
                "reason":           reason,
                "rules":            GATE_RULES_VERSION,
                "rejected_by":      rejected_by,
                "panel_caution":    [{"codes": a.get("fault_codes"), "since": a.get("since"),
                                      "fresh": a.get("fresh")}
                                     for a in struct.get("panel_alarms", [])
                                     if a.get("severity") == "CAUTION"],
                "provider":         provider,
                "model":            model,
                "tokens_in":        tokens_in,
                "tokens_out":       tokens_out,
        }
        _logged = False
        for _i in range(_GATE_SAVE_RETRIES + 1):
            _logged = await online_db.append_segment_gate_event(
                router_sn, equip_type, panel_id,
                segment_id=_seg_id, ts=_gate_ts, event=_gate_event,
            )
            if _logged or _i == _GATE_SAVE_RETRIES:
                break
            await asyncio.sleep(_GATE_SAVE_RETRY_SEC)

        # Чужой язык в разборе (иероглифы, английские фразы; v5.1.22, решение
        # В4): один проход «очистка / перевод» быстрой моделью, до подписи. После
        # записи снятия и журнала: вердикт уже извлечён, применён и записан,
        # проход его не меняет
        if analysis:
            try:
                from llm.lang_check import clean_text
                from llm.router import get_depth
                analysis, _lang = await clean_text(analysis, user_prompt,
                                                   priority=get_depth(kind).get("priority"))
            except asyncio.CancelledError:
                # v5.1.22, перепроверка: снятие и журнал уже записаны, после рестарта
                # гейт по этому составу молчит — разбор не теряем: исходник без
                # очистки одной записью, затем отмена
                try:
                    from llm.router import format_ai_signature as _sig
                    await asyncio.shield(online_db.save_segment_warning(
                        router_sn, equip_type, panel_id,
                        analysis_md=gate_facts_md(struct, decision, applied, reasons)
                        + analysis + _sig(model),
                        fault_hash=fault_hash, alarm_text=extract_alarm_text(struct),
                        segment_id=_seg_id, ts=_gate_ts,
                        extra={"rules": GATE_RULES_VERSION, "decision": decision, "applied": applied,
                               "lang_check": {"cleaned": False, "accepted": False,
                                              "error": "остановка сервиса во время очистки — сохранён исходник"}},
                    ))
                except BaseException:
                    logger.warning("WarningGate: исходник разбора при остановке не сохранён",
                                   exc_info=True)
                raise
            except Exception:
                logger.warning("WarningGate: проверка языка не выполнена", exc_info=True)
        if analysis:
            from llm.router import format_ai_signature
            analysis = analysis + format_ai_signature(model)

        # Факты рядом с мнением: панель, паспорт, применён ли вердикт (после
        # записи снятия — «применён» значит записан, v5.1.13)
        if analysis:
            analysis = gate_facts_md(struct, decision, applied, reasons) + analysis

        saved = None
        if analysis:
            _wa_extra = {"rules": GATE_RULES_VERSION, "decision": decision, "applied": applied}
            if "not_written" in rejected_by:
                _wa_extra["not_written"] = True
            if _lang:
                _wa_extra["lang_check"] = _lang
            saved = await online_db.save_segment_warning(
                router_sn, equip_type, panel_id,
                analysis_md=analysis,
                fault_hash=fault_hash,
                alarm_text=extract_alarm_text(struct),
                segment_id=_seg_id, ts=_gate_ts, extra=_wa_extra,
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
                    segment_id=_seg_id, ts=_gate_ts, extra=_wa_extra,
                )
            if not saved:
                logger.warning("WarningGate: разбор НЕ СОХРАНЁН — сегмент на %s не найден "
                               "(%s/%s/%s, %d симв.) — повтор после паузы",
                               _gate_ts.isoformat(), router_sn, equip_type, panel_id,
                               len(analysis))

        logger.info("WarningGate: %s/%s/%s — decision=%s applied=%s (%d симв. анализа, "
                    "разбор=%s, журнал=%s)",
                    router_sn, equip_type, panel_id, decision, applied, len(analysis or ""),
                    "сохранён" if saved else ("нет" if analysis else "—"),
                    "записан" if _logged else "ПОТЕРЯН")
        # Ответ есть, а записать некуда — разбор потерян: не «разобрано», а пауза
        # и повтор (иначе потерянный разбор не повторился бы до смены состава).
        # Так же — снятие, которое не записалось (v5.1.13)
        if (analysis and not saved) or "not_written" in rejected_by:
            return GATE_FAILED
        return GATE_OK

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


