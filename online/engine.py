# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""OnlinePollEngine — непрерывный движок сегментации для одной машины.

Алгоритм на каждом цикле опроса:
1. N+1 буфер: первый цикл запоминает timestamp, обработка начинается со второго.
2. Найти суточные границы (09:00 в настроенном TZ) в окне [cursor_ts, process_to].
3. Для каждой суточной границы: закрыть окно [cursor_ts, boundary] как DAILY_BOUNDARY.
4. Обработать открытое окно [cursor_ts, process_to]:
   - Если в окне есть смены RUN_STATE — закрыть соответствующие сегменты.
   - Создать/обновить открытый сегмент с текущими значениями и детекциями.

Возобновление после рестарта (п. 3.1 ТЗ):
  cursor_ts = t_end последнего ЗАКРЫТОГО сегмента (или start_date наблюдения).
  coking_risk переносится всегда.
  forward_fill_memory переносится только если сегмент закрыт по DAILY_BOUNDARY.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from analytics.contract import CokingRisk
from online import db as online_db

logger = logging.getLogger(__name__)

# Добавляем этот отступ перед cursor_ts при загрузке аналоговых данных,
# чтобы сегментатор имел преамбулу (последние известные значения до t_start).
_PREAMBLE_LOOKBACK_SEC = 300


def _tz_utc(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _enqueue_segment(seg_id: int | None) -> None:
    """Добавить закрытый сегмент в очередь Claude-анализа (Этап 2).

    Fire-and-forget: если воркер не запущен или авто-анализ выключен — молча пропускаем.
    """
    if seg_id is None:
        return
    try:
        import asyncio as _aio
        from corpus.worker import get_worker, PRIORITY_NORMAL

        worker = get_worker()
        if not worker:
            return

        # Проверяем флаг авто-анализа (асинхронно в текущем event loop)
        async def _check_and_enqueue():
            try:
                from db.analytics import get_app_setting
                flag = await get_app_setting("corpus_auto_analyze", "false")
                if flag == "true":
                    worker.enqueue(seg_id, PRIORITY_NORMAL)
            except Exception:
                pass

        loop = _aio.get_event_loop()
        if loop.is_running():
            _aio.ensure_future(_check_and_enqueue())
    except Exception:
        pass


def _make_seg_hint(db_row: dict):
    """Duck-typed хинт для prev_seg в to_markdown: извлекает run_state и run_state_label из DB-строки."""
    from types import SimpleNamespace
    chars = db_row.get("characteristics_json")
    if isinstance(chars, str):
        try:
            import json as _j; chars = _j.loads(chars)
        except Exception:
            chars = None
    label = (chars.get("run_state_label") if isinstance(chars, dict) else None)
    return SimpleNamespace(run_state=db_row.get("run_state"), run_state_label=label)


def _coking_from_json(d) -> CokingRisk:
    """Десериализовать CokingRisk из dict или JSON-строки (asyncpg отдаёт JSONB как str)."""
    if not d:
        return CokingRisk()
    if isinstance(d, str):
        try:
            import json as _json
            d = _json.loads(d)
        except Exception:
            return CokingRisk()
    if not isinstance(d, dict):
        return CokingRisk()
    return CokingRisk(
        idle_low_rpm_sec=float(d.get("idle_low_rpm_sec", 0.0)),
        coolant_below_60_sec=float(d.get("coolant_below_60_sec", 0.0)),
        low_load_zone_sec=float(d.get("low_load_zone_sec", 0.0)),
        risk_level=d.get("risk_level", "GREEN"),
        last_purge_ts=d.get("last_purge_ts"),
    )


def _load_rs_sec_from_ff(ff: dict | None) -> dict[int, float]:
    """Извлечь накопленное время RS из forward_fill_json (_run_state_sec ключ)."""
    if not isinstance(ff, dict):
        return {}
    raw = ff.get("_run_state_sec")
    if not isinstance(raw, dict):
        return {}
    result: dict[int, float] = {}
    for k, v in raw.items():
        try:
            result[int(k)] = float(v)
        except (ValueError, TypeError):
            pass
    return result


def _find_daily_boundaries(
    t_from: datetime,
    t_to: datetime,
    daily_hour: int,
    tz,
) -> list[datetime]:
    """Найти все 09:00 (local time) в интервале (t_from, t_to]."""
    t_from_local = _tz_utc(t_from).astimezone(tz)
    t_to_local   = _tz_utc(t_to).astimezone(tz)

    boundaries: list[datetime] = []
    # Первая кандидатная граница — 09:00 дня t_from
    candidate = t_from_local.replace(
        hour=daily_hour, minute=0, second=0, microsecond=0
    )
    # Если 09:00 этого дня уже прошло, берём следующий день
    if candidate <= t_from_local:
        candidate += timedelta(days=1)

    while candidate <= t_to_local:
        utc_b = candidate.astimezone(timezone.utc)
        # Граница должна быть строго внутри (t_from, t_to]
        if utc_b > _tz_utc(t_from) and utc_b <= _tz_utc(t_to):
            boundaries.append(utc_b)
        candidate += timedelta(days=1)

    return boundaries


def _extract_coking_risk_from_segments(segments: list) -> CokingRisk:
    """Взять coking_risk из последнего подсегмента последнего сегмента."""
    if not segments:
        return CokingRisk()
    last_seg = segments[-1]
    if not last_seg.subsegments:
        return CokingRisk()
    return copy.deepcopy(last_seg.subsegments[-1].risk_accumulators.coking_risk)


def _extract_open_segment_data(seg) -> tuple[dict, list]:
    """Извлечь текущие значения и активные детекции из открытого сегмента."""
    if not seg.subsegments:
        return {}, []
    last_sub = seg.subsegments[-1]

    current_values: dict[str, Any] = {
        "ts": last_sub.t_end or last_sub.t_start,
        "run_state": seg.run_state,
        "run_state_label": seg.run_state_label,
        "coking_risk": last_sub.risk_accumulators.coking_risk.to_dict(),
        "values": {
            role: {
                "value": char.get("value_end"),
                "unit":  char.get("unit", ""),
                "median": char.get("median"),
            }
            for role, char in last_sub.characteristics.items()
            if isinstance(char, dict)
        },
    }

    # Детекции из всех подсегментов: сигнал из раннего подсегмента
    # остаётся активным пока сегмент открыт. Дедупликация по (scenario, fault_codes).
    seen: set[tuple] = set()
    active_dets: list[dict] = []
    for sub in seg.subsegments:
        for d in sub.detections:
            d_dict = d.to_dict()
            key = (d_dict.get("scenario"), tuple(sorted(d_dict.get("fault_codes") or [])))
            if key not in seen:
                seen.add(key)
                active_dets.append(d_dict)

    return current_values, active_dets


async def _load_data(
    router_sn: str,
    equip_type: str,
    panel_id: int,
    ts_from: datetime,
    ts_to: datetime,
    cfg,
) -> tuple[list, list, list, list]:
    """Загрузить все данные из source-БД параллельно.

    История аналогов загружается с отступом _PREAMBLE_LOOKBACK_SEC до ts_from,
    чтобы сегментатор мог заполнить преамбулу.
    """
    from analytics import source as _src
    ts_from_utc = _tz_utc(ts_from)
    ts_to_utc   = _tz_utc(ts_to)
    ts_history_from = ts_from_utc - timedelta(seconds=_PREAMBLE_LOOKBACK_SEC)

    history_task = asyncio.create_task(
        _src.get_whitelist_history(
            router_sn, equip_type, panel_id,
            ts_history_from, ts_to_utc,
            cfg.whitelist_analog,
        )
    )
    enum_task = asyncio.create_task(
        _src.get_enum_periods(
            router_sn, equip_type, panel_id,
            ts_from_utc, ts_to_utc,
            addrs=[40011, 40010],
        )
    )
    fault_task = asyncio.create_task(
        _src.get_fault_periods(
            router_sn, equip_type, panel_id,
            ts_from_utc, ts_to_utc,
            fault_addrs=cfg.whitelist_fault,
        )
    )
    gaps_task = asyncio.create_task(
        _src.get_data_gaps(
            router_sn, equip_type, panel_id,
            ts_from_utc, ts_to_utc,
        )
    )
    return await asyncio.gather(history_task, enum_task, fault_task, gaps_task)


async def _run_segment(
    router_sn: str,
    equip_type: str,
    panel_id: int,
    engine_sn: str,
    ts_from: datetime,
    ts_to: datetime,
    cfg,
    initial_coking_risk: CokingRisk,
) -> list:
    """Загрузить данные и запустить segment() в отдельном потоке.

    segment() — CPU-bound синхронная функция. asyncio.to_thread() выносит её
    в ThreadPoolExecutor, освобождая event loop для обработки веб-запросов.
    """
    import functools
    from analytics.segmenter import segment as _segment

    history, enum_periods, fault_periods, gaps = await _load_data(
        router_sn, equip_type, panel_id, ts_from, ts_to, cfg
    )
    segments = await asyncio.to_thread(
        functools.partial(
            _segment,
            enum_periods=enum_periods,
            history=history,
            fault_periods=fault_periods,
            gaps=gaps,
            cfg=cfg,
            router_sn=router_sn,
            equip_type=equip_type,
            panel_id=panel_id,
            engine_sn=engine_sn,
            ts_from=_tz_utc(ts_from),
            ts_to=_tz_utc(ts_to),
            initial_coking_risk=copy.deepcopy(initial_coking_risk),
        )
    )
    return segments


async def _collect_and_enrich_detections(
    seg,
    router_sn: str,
    equip_type: str,
    panel_id: int,
    seg_t_end: datetime,
    cfg,
    run_origin_ts: datetime,
) -> list[dict]:
    """Для закрытого сегмента: обогатить детекции счётчиками + вернуть events.

    1. Собирает уникальные (scenario, severity, run_state, front_count) из всех подсегментов.
    2. Запрашивает history_count_30d и startup_count ДО текущего события.
    3. Мутирует detection.values с +1 (включая текущее событие).
    4. Возвращает список event-dict для insert_detection_events.

    Мутация до seg.to_dict() / to_markdown() — счётчики попадают в отчёт.
    """
    window_days = int(cfg.det("DETECTION_COUNTER", "window_days", default=30) or 30)

    # Собрать уникальные сценарии (дедупликация по scenario)
    # Сохраняем: (severity, run_state, front_count)
    seen_scenarios: dict[str, tuple[str | None, int | None, int]] = {}
    first_t_detected: dict[str, datetime] = {}
    for sub in seg.subsegments:
        for d in sub.detections:
            sc = d.scenario
            if sc not in seen_scenarios:
                fc = int(d.values.get("front_count", 1)) if d.values else 1
                seen_scenarios[sc] = (d.severity, seg.run_state, fc)
                if d.t_detected:
                    try:
                        first_t_detected[sc] = _tz_utc(datetime.fromisoformat(d.t_detected))
                    except Exception:
                        pass

    if not seen_scenarios:
        return []

    # Запрос обоих счётчиков
    counts_30d: dict[str, int] = {}
    counts_startup: dict[str, int] = {}
    for sc in seen_scenarios:
        counts_30d[sc] = await online_db.count_detection_events(
            router_sn, equip_type, panel_id, sc, window_days
        )
        try:
            counts_startup[sc] = await online_db.count_detection_events_since(
                router_sn, equip_type, panel_id, sc, run_origin_ts
            )
        except Exception:
            counts_startup[sc] = 0

    # Мутировать detection.values с +1 (текущее событие включается)
    for sub in seg.subsegments:
        for d in sub.detections:
            if d.scenario in counts_30d:
                d.values["history_count_30d"] = counts_30d[d.scenario] + 1
                d.values["startup_count"] = counts_startup.get(d.scenario, 0) + 1

    # Подготовить события для вставки в detection_events
    events = [
        {
            "scenario":    sc,
            "detected_at": first_t_detected.get(sc, seg_t_end),
            "segment_id":  None,  # segment_id будет обновлён после insert_closed_segment
            "severity":    sev,
            "run_state":   rs,
            "front_count": fc,
        }
        for sc, (sev, rs, fc) in seen_scenarios.items()
    ]
    return events


async def _enrich_open_seg_detections(
    seg,
    router_sn: str,
    equip_type: str,
    panel_id: int,
    cfg,
    run_origin_ts: datetime,
) -> None:
    """Для открытого сегмента: обогатить детекции счётчиками.

    Только запрос — без вставки в detection_events (сегмент не завершён).
    +1 добавляется для обоих счётчиков: текущее активное событие включается в счёт.
    """
    window_days = int(cfg.det("DETECTION_COUNTER", "window_days", default=30) or 30)

    seen_scenarios: set[str] = {
        d.scenario
        for sub in seg.subsegments
        for d in sub.detections
    }
    if not seen_scenarios:
        return

    for sc in seen_scenarios:
        try:
            count_30d = await online_db.count_detection_events(
                router_sn, equip_type, panel_id, sc, window_days
            )
        except Exception:
            count_30d = 0

        try:
            count_startup = await online_db.count_detection_events_since(
                router_sn, equip_type, panel_id, sc, run_origin_ts
            )
        except Exception:
            count_startup = 0

        for sub in seg.subsegments:
            for d in sub.detections:
                if d.scenario == sc:
                    d.values["history_count_30d"] = count_30d + 1
                    d.values["startup_count"] = count_startup + 1


class OnlinePollEngine:
    """Движок непрерывного онлайн-мониторинга для одной машины."""

    def __init__(
        self,
        router_sn: str,
        equip_type: str,
        panel_id: int,
        engine_sn: str,
        cfg,
        poll_interval_sec: int = 30,
        daily_hour: int = 9,
        tz=None,
        fault_ref=None,
    ) -> None:
        self.router_sn        = router_sn
        self.equip_type       = equip_type
        self.panel_id         = panel_id
        self.engine_sn        = engine_sn
        self.cfg              = cfg
        self.poll_interval_sec = poll_interval_sec
        self.daily_hour       = daily_hour
        self.tz               = tz
        self._fault_ref       = fault_ref  # справочник кодов неисправностей

        # Изменяемое состояние — переинициализируется из БД при старте
        self.cursor_ts: datetime | None          = None
        self.inherited_coking_risk: CokingRisk   = CokingRisk()
        self.inherited_run_state_sec: dict[int, float] = {}
        self.forward_fill_memory: dict | None    = None
        self.continued_from_id: int | None       = None  # предок в DAILY_BOUNDARY цепочке
        self.prev_poll_ts: datetime | None        = None
        # Куда дошли в последнем цикле (обновляется внутри цикла — для прогресс-бара)
        self.last_processed_to: datetime | None  = None
        # Последний закрытый сегмент как duck-typed хинт для prev_seg в отчётах
        self._prev_seg_hint = None

        self._running = False
        self._task: asyncio.Task | None = None
        # Кэш аналоговых данных открытого окна (обнуляется при смене cursor_ts)
        self._open_history_cache: list[dict] | None = None
        self._open_history_cache_ts: datetime | None = None

    @property
    def key(self) -> str:
        return f"{self.router_sn}|{self.equip_type}|{self.panel_id}"

    # ── Инициализация состояния из БД ─────────────────────────────────────────

    async def initialize(self, start_date: datetime, allow_gap_fill: bool = False) -> None:
        """Восстановить состояние из последнего закрытого сегмента (п. 3.1 ТЗ).

        allow_gap_fill=False (дефолт) — автоматический перезапуск сервиса:
            всегда возобновляем с t_end последнего сегмента, start_date игнорируется.
        allow_gap_fill=True — ручной Пуск по кнопке:
            если start_date < last.t_end, запускаемся с start_date чтобы
            batch-добор заполнил пропуск в середине истории.
        """
        last = await online_db.get_last_closed_segment(
            self.router_sn, self.equip_type, self.panel_id
        )
        if last:
            last_end = _tz_utc(last["t_end"])
            requested = _tz_utc(start_date)

            if allow_gap_fill and requested < last_end:
                # Ручной Пуск с датой раньше последнего сегмента →
                # запускаемся с неё, batch-добор заполнит пропуск.
                self.cursor_ts = requested
                self.inherited_coking_risk = CokingRisk()
                self.inherited_run_state_sec = {}
                self.forward_fill_memory = None
                self.continued_from_id = None
                logger.info(
                    "OnlineEngine[%s]: заполнение пропуска с %s (последний сегмент: %s)",
                    self.key, requested, last_end,
                )
            else:
                # Обычное возобновление с конца последнего сегмента
                self.cursor_ts = last_end
                self.inherited_coking_risk = _coking_from_json(last.get("coking_risk_json"))
                self._prev_seg_hint = _make_seg_hint(last)
                if last.get("cause_close") == "DAILY_BOUNDARY":
                    ff = last.get("forward_fill_json")
                    if isinstance(ff, str):
                        try:
                            import json as _json
                            ff = _json.loads(ff)
                        except Exception:
                            ff = None
                    self.forward_fill_memory = ff
                    self.continued_from_id = last["id"]
                    self.inherited_run_state_sec = _load_rs_sec_from_ff(ff)
                else:
                    self.forward_fill_memory = None
                    self.continued_from_id = None
                    self.inherited_run_state_sec = {}
                logger.info(
                    "OnlineEngine[%s]: возобновление с %s (coking=%s, причина_закрытия=%s)",
                    self.key, self.cursor_ts,
                    self.inherited_coking_risk.risk_level,
                    last.get("cause_close"),
                )
        else:
            self.cursor_ts = _tz_utc(start_date)
            self.inherited_coking_risk = CokingRisk()
            self.inherited_run_state_sec = {}
            self.forward_fill_memory = None
            self.continued_from_id = None
            logger.info(
                "OnlineEngine[%s]: первый старт с %s",
                self.key, self.cursor_ts,
            )

    # ── Главный цикл ──────────────────────────────────────────────────────────

    async def run(self) -> None:
        self._running = True
        logger.info(
            "OnlineEngine[%s] запущен, интервал=%ds",
            self.key, self.poll_interval_sec,
        )
        while self._running:
            try:
                await self._poll_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("OnlineEngine[%s]: ошибка цикла", self.key)
            if self._running:
                await asyncio.sleep(self.poll_interval_sec)

    async def stop(self) -> None:
        self._running = False
        self._open_history_cache = None
        self._open_history_cache_ts = None
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    # ── Один цикл опроса ──────────────────────────────────────────────────────

    async def _poll_cycle(self) -> None:
        now = datetime.now(timezone.utc)

        # N+1 буфер: первый цикл — только запомнить метку времени
        if self.prev_poll_ts is None:
            self.prev_poll_ts = now
            return

        process_to    = self.prev_poll_ts  # обрабатываем данные до предыдущего цикла
        self.prev_poll_ts = now

        if self.cursor_ts is None or process_to <= self.cursor_ts:
            return

        # Найти суточные границы в (cursor_ts, process_to]
        tz = self.tz
        if tz is None:
            from config import get_tz
            tz = get_tz()
        boundaries = _find_daily_boundaries(
            self.cursor_ts, process_to, self.daily_hour, tz
        )

        pending = [b for b in boundaries if self.cursor_ts < b < process_to]

        # Обрабатываем ВСЕ границы подряд — batch-добор идёт быстро.
        # last_processed_to обновляется внутри _close_window после каждого сегмента,
        # т.к. там есть await-точки между которыми event loop обслуживает API.
        for boundary in pending:
            await self._close_window(self.cursor_ts, boundary, "DAILY_BOUNDARY")

        # Открытое окно — всегда в конце (после всех границ или сразу если их нет)
        await self._update_open_window(self.cursor_ts, process_to)
        self.last_processed_to = process_to

    # ── Закрытие окна (DAILY_BOUNDARY) ────────────────────────────────────────

    async def _close_window(
        self,
        t_from: datetime,
        t_to: datetime,
        close_reason: str,
    ) -> None:
        """Запустить полный анализ [t_from, t_to], сохранить все сегменты как закрытые."""
        if t_to <= t_from:
            return

        from analytics.runner import ANALYTICS_VERSION

        try:
            segments = await _run_segment(
                self.router_sn, self.equip_type, self.panel_id, self.engine_sn,
                t_from, t_to, self.cfg,
                self.inherited_coking_risk,
            )
        except Exception:
            logger.exception("OnlineEngine[%s]: ошибка анализа [%s, %s]", self.key, t_from, t_to)
            return

        # Сохранить характеристики открытого сегмента для верификации (до удаления)
        _open_row = await online_db.get_open_segment(
            self.router_sn, self.equip_type, self.panel_id
        )
        _open_chars: dict | None = None
        if _open_row:
            _raw = _open_row.get("characteristics_json")
            if isinstance(_raw, str):
                try:
                    import json as _j; _open_chars = _j.loads(_raw)
                except Exception:
                    pass
            elif isinstance(_raw, dict):
                _open_chars = _raw

        # Удалить старый открытый сегмент
        await online_db.delete_open_segment(
            self.router_sn, self.equip_type, self.panel_id
        )

        if not segments:
            self.cursor_ts = _tz_utc(t_to)
            return

        # Сразу сеем новый открытый сегмент, чтобы не было gap'а между
        # delete и финальным upsert_open_segment в _update_open_window.
        # _update_open_window перезапишет его актуальными данными.
        _seed_cv, _seed_dets = _extract_open_segment_data(segments[-1])
        await online_db.upsert_open_segment({
            "router_sn":              self.router_sn,
            "equip_type":             self.equip_type,
            "panel_id":               self.panel_id,
            "t_start":                _tz_utc(t_to),
            "run_state":              segments[-1].run_state,
            "coking_risk_json":       self.inherited_coking_risk.to_dict(),
            "analytics_version":      ANALYTICS_VERSION,
            "current_values_json":    _seed_cv or None,
            "active_detections_json": _seed_dets,
            "continued_from":         None,
        })

        # Вычислить начало текущего непрерывного запуска (для счётчика «с пуска»)
        chain_origin_ts: datetime | None = None
        if self.continued_from_id is not None:
            try:
                _origin = await online_db.get_run_state_origin_ts(self.continued_from_id)
                chain_origin_ts = _tz_utc(_origin) if _origin else None
            except Exception:
                pass

        last_saved_id: int | None = None
        # Накопленное время RS до этого батча (из предыдущей суточной цепочки)
        running_rs_sec = dict(self.inherited_run_state_sec)
        # t_end последнего успешно сохранённого сегмента (для отката cursor_ts при пропуске границы)
        last_committed_t_end: datetime = _tz_utc(t_from)

        for i, seg in enumerate(segments):
            is_last = (i == len(segments) - 1)

            # Причина закрытия: последний сегмент — по переданной причине,
            # остальные — по RUN_STATE_CHANGE (детерминировано сегментатором)
            if seg.cause_close is not None:
                seg_cause_close = seg.cause_close
            elif is_last:
                seg_cause_close = close_reason
            else:
                seg_cause_close = "RUN_STATE_CHANGE"

            # Финальный t_end: если сегментатор не проставил — берём t_to
            seg_t_end = (
                _tz_utc(datetime.fromisoformat(seg.t_end))
                if seg.t_end
                else _tz_utc(t_to)
            )
            if is_last and close_reason == "DAILY_BOUNDARY":
                seg_t_end = _tz_utc(t_to)  # граница = t_to в этом вызове

            coking_risk = _extract_coking_risk_from_segments([seg])

            # DAILY_BOUNDARY пропускаем если переходное состояние (RS ≠ 0 и ≠ 3).
            # Переходные состояния кратковременны — сегмент закроется RUN_STATE_CHANGE.
            if is_last and close_reason == "DAILY_BOUNDARY" and seg.run_state not in {0, 3}:
                logger.info(
                    "OnlineEngine[%s]: DAILY_BOUNDARY@%s пропущен — RS=%d (переходное)",
                    self.key, t_to, seg.run_state,
                )
                self.cursor_ts = last_committed_t_end
                break

            # Унаследованное время RS для report_md этого сегмента:
            # i==0 — прямое продолжение предыдущей цепочки; i>0 — новый RS (смена).
            seg_inherited_rs = running_rs_sec if i == 0 else {}

            # forward-fill память + накопленное время RS для суточного реза
            ff_json = None
            updated_rs_sec: dict[int, float] | None = None
            if is_last and close_reason == "DAILY_BOUNDARY":
                if i == 0:
                    updated_rs_sec = dict(running_rs_sec)
                    updated_rs_sec[seg.run_state] = (
                        updated_rs_sec.get(seg.run_state, 0.0) + seg.duration_sec
                    )
                else:
                    # Последний сегмент начался с новым RS — счётчик свежий
                    updated_rs_sec = {seg.run_state: seg.duration_sec}
                if seg.run_state == 3:
                    ff_json = _build_ff_memory(seg) or {}
                else:
                    ff_json = {}
                ff_json["_run_state_sec"] = {str(k): v for k, v in updated_rs_sec.items()}

            # Начало запуска: i==0 продолжает цепочку, i>0 — новый запуск
            _run_origin = (
                (chain_origin_ts or _tz_utc(datetime.fromisoformat(seg.t_start)))
                if i == 0
                else _tz_utc(datetime.fromisoformat(seg.t_start))
            )

            # Обогатить детекции счётчиками ДО to_dict() / to_markdown()
            _det_events = await _collect_and_enrich_detections(
                seg, self.router_sn, self.equip_type, self.panel_id,
                seg_t_end, self.cfg, run_origin_ts=_run_origin,
            )

            seg_dict = seg.to_dict()
            seg_dict["t_end"] = seg_t_end.isoformat()
            seg_dict["cause_close"] = seg_cause_close

            # prev_seg для отчёта: предыдущий в текущем батче или хинт из движка
            ps = segments[i - 1] if i > 0 else self._prev_seg_hint

            # Генерация Markdown-отчёта для закрытого сегмента
            try:
                from analytics.serializer import to_markdown as _to_md
                report_md = _to_md(
                    [seg], self.router_sn, self.equip_type, self.panel_id,
                    _tz_utc(datetime.fromisoformat(seg.t_start)), seg_t_end,
                    ANALYTICS_VERSION, tz=self.tz, prev_seg=ps,
                    fault_ref=self._fault_ref,
                    inherited_run_state_sec=seg_inherited_rs,
                )
            except Exception:
                report_md = None

            db_id = await online_db.insert_closed_segment({
                "router_sn":          self.router_sn,
                "equip_type":         self.equip_type,
                "panel_id":           self.panel_id,
                "t_start":            _tz_utc(datetime.fromisoformat(seg.t_start)),
                "t_end":              seg_t_end,
                "run_state":          seg.run_state,
                "cause_close":        seg_cause_close,
                "split_reason":       "DAILY_BOUNDARY" if seg_cause_close == "DAILY_BOUNDARY" else None,
                "continued_from":     None,
                "coking_risk_json":   coking_risk.to_dict(),
                "forward_fill_json":  ff_json,
                "analytics_version":  ANALYTICS_VERSION,
                "characteristics_json": seg_dict,
                "report_md":          report_md,
            })
            # ← await выше = event loop обслужил API. Сигналим прогресс сразу.
            _enqueue_segment(db_id)

            # Записать события детекций (segment_id теперь известен)
            if _det_events:
                for ev in _det_events:
                    ev["segment_id"] = db_id
                try:
                    await online_db.insert_detection_events(
                        self.router_sn, self.equip_type, self.panel_id, _det_events
                    )
                except Exception:
                    logger.warning("OnlineEngine[%s]: не удалось записать detection_events", self.key)

            self.last_processed_to = seg_t_end
            self._prev_seg_hint = seg
            last_committed_t_end = seg_t_end

            if is_last:
                last_saved_id = db_id
                self.cursor_ts = _tz_utc(t_to)
                self.inherited_coking_risk = coking_risk
                if close_reason == "DAILY_BOUNDARY":
                    self.inherited_run_state_sec = updated_rs_sec  # type: ignore[assignment]
                    if seg.run_state in {0, 3}:  # стабильные состояния: тянем цепочку
                        self.continued_from_id = db_id
                        self.forward_fill_memory = ff_json
                    else:
                        self.continued_from_id = None
                        self.forward_fill_memory = None
                else:
                    self.inherited_run_state_sec = {}
                    self.continued_from_id = None
                    self.forward_fill_memory = None

                # Верификация: сравниваем сохранённые инкрементальные vs reference
                from online.verifier import fire_verify as _fire_verify
                _fire_verify(
                    seg_id=db_id,
                    unit_key=self.key,
                    run_state=seg.run_state,
                    t_start_str=seg.t_start,
                    t_end_str=seg_t_end.isoformat(),
                    incr_chars=_open_chars,
                    ref_chars=seg_dict,
                )

        self._open_history_cache = None
        self._open_history_cache_ts = None
        logger.debug(
            "OnlineEngine[%s]: закрыто %d сегментов до %s (%s)",
            self.key, len(segments), t_to, close_reason,
        )

    # ── Обновление открытого окна ─────────────────────────────────────────────

    async def _update_open_window(self, t_from: datetime, t_to: datetime) -> None:
        """Запустить анализ открытого окна, обновить открытый сегмент в БД."""
        if t_to <= t_from:
            return

        from analytics.runner import ANALYTICS_VERSION

        try:
            from analytics import source as _src
            import functools
            from analytics.segmenter import segment as _segment_fn

            _OVERLAP = 120  # сек перекрытия при дозагрузке хвоста (защита от поздних строк)
            ts_from_utc = _tz_utc(t_from)
            ts_to_utc   = _tz_utc(t_to)

            if self._open_history_cache is not None and self._open_history_cache_ts is not None:
                # Дозагрузка: стабильная часть из кэша + свежий хвост из БД
                split_ts = self._open_history_cache_ts - timedelta(seconds=_OVERLAP)
                preamble_floor = ts_from_utc - timedelta(seconds=_PREAMBLE_LOOKBACK_SEC)
                if split_ts < preamble_floor:
                    split_ts = preamble_floor
                stable = [r for r in self._open_history_cache if r["ts"] < split_ts]
                tail = await _src.get_whitelist_history(
                    self.router_sn, self.equip_type, self.panel_id,
                    split_ts, ts_to_utc, self.cfg.whitelist_analog,
                )
                history = stable + tail
                logger.debug(
                    "OnlineEngine[%s]: history кэш=%d + хвост=%d",
                    self.key, len(stable), len(tail),
                )
            else:
                preamble_floor = ts_from_utc - timedelta(seconds=_PREAMBLE_LOOKBACK_SEC)
                history = await _src.get_whitelist_history(
                    self.router_sn, self.equip_type, self.panel_id,
                    preamble_floor, ts_to_utc, self.cfg.whitelist_analog,
                )
                logger.debug(
                    "OnlineEngine[%s]: history полная загрузка=%d строк",
                    self.key, len(history),
                )

            self._open_history_cache = history
            self._open_history_cache_ts = max((r["ts"] for r in history), default=None)

            enum_periods, fault_periods, gaps = await asyncio.gather(
                _src.get_enum_periods(
                    self.router_sn, self.equip_type, self.panel_id,
                    ts_from_utc, ts_to_utc, addrs=[40011, 40010],
                ),
                _src.get_fault_periods(
                    self.router_sn, self.equip_type, self.panel_id,
                    ts_from_utc, ts_to_utc,
                    fault_addrs=self.cfg.whitelist_fault,
                ),
                _src.get_data_gaps(
                    self.router_sn, self.equip_type, self.panel_id,
                    ts_from_utc, ts_to_utc,
                ),
            )

            segments = await asyncio.to_thread(
                functools.partial(
                    _segment_fn,
                    enum_periods=enum_periods,
                    history=history,
                    fault_periods=fault_periods,
                    gaps=gaps,
                    cfg=self.cfg,
                    router_sn=self.router_sn,
                    equip_type=self.equip_type,
                    panel_id=self.panel_id,
                    engine_sn=self.engine_sn,
                    ts_from=ts_from_utc,
                    ts_to=ts_to_utc,
                    initial_coking_risk=copy.deepcopy(self.inherited_coking_risk),
                )
            )
        except Exception:
            logger.exception("OnlineEngine[%s]: ошибка анализа открытого окна", self.key)
            return

        if not segments:
            return

        # Вычислить начало текущего непрерывного запуска (для счётчика «с пуска»)
        chain_origin_ts: datetime | None = None
        if self.continued_from_id is not None:
            try:
                _origin = await online_db.get_run_state_origin_ts(self.continued_from_id)
                chain_origin_ts = _tz_utc(_origin) if _origin else None
            except Exception:
                pass

        # Новые закрытые сегменты (смены RUN_STATE внутри открытого окна)
        closed_segs = [s for s in segments if s.cause_close == "RUN_STATE_CHANGE"]
        if closed_segs:
            # Сохранить характеристики открытого сегмента для верификации (до удаления)
            _rs_open_row = await online_db.get_open_segment(
                self.router_sn, self.equip_type, self.panel_id
            )
            _rs_open_chars: dict | None = None
            if _rs_open_row:
                _raw_rs = _rs_open_row.get("characteristics_json")
                if isinstance(_raw_rs, str):
                    try:
                        import json as _j; _rs_open_chars = _j.loads(_raw_rs)
                    except Exception:
                        pass
                elif isinstance(_raw_rs, dict):
                    _rs_open_chars = _raw_rs

            await online_db.delete_open_segment(
                self.router_sn, self.equip_type, self.panel_id
            )
            # Первый закрытый сегмент в окне может быть продолжением после DAILY_BOUNDARY
            carry_continued_from = self.continued_from_id
            # Унаследованное RS время: ci==0 продолжает цепочку, ci>0 — новый RS
            _rs_inherited_before = dict(self.inherited_run_state_sec)
            for ci, seg in enumerate(closed_segs):
                coking_risk = _extract_coking_risk_from_segments([seg])
                seg_t_end_rs = _tz_utc(datetime.fromisoformat(seg.t_end))
                ps_rs = closed_segs[ci - 1] if ci > 0 else self._prev_seg_hint

                # Начало запуска: ci==0 продолжает цепочку, ci>0 — новый запуск
                _rs_run_origin = (
                    (chain_origin_ts or _tz_utc(datetime.fromisoformat(seg.t_start)))
                    if ci == 0
                    else _tz_utc(datetime.fromisoformat(seg.t_start))
                )

                # Обогатить детекции счётчиками ДО to_dict() / to_markdown()
                _rs_det_events = await _collect_and_enrich_detections(
                    seg, self.router_sn, self.equip_type, self.panel_id,
                    seg_t_end_rs, self.cfg, run_origin_ts=_rs_run_origin,
                )

                try:
                    from analytics.serializer import to_markdown as _to_md
                    report_md_rs = _to_md(
                        [seg], self.router_sn, self.equip_type, self.panel_id,
                        _tz_utc(datetime.fromisoformat(seg.t_start)), seg_t_end_rs,
                        ANALYTICS_VERSION, tz=self.tz, prev_seg=ps_rs,
                        fault_ref=self._fault_ref,
                        inherited_run_state_sec=_rs_inherited_before if ci == 0 else {},
                    )
                except Exception:
                    report_md_rs = None

                _rs_seg_dict = seg.to_dict()
                _rs_db_id = await online_db.insert_closed_segment({
                    "router_sn":          self.router_sn,
                    "equip_type":         self.equip_type,
                    "panel_id":           self.panel_id,
                    "t_start":            _tz_utc(datetime.fromisoformat(seg.t_start)),
                    "t_end":              seg_t_end_rs,
                    "run_state":          seg.run_state,
                    "cause_close":        "RUN_STATE_CHANGE",
                    "split_reason":       None,
                    "continued_from":     carry_continued_from,
                    "coking_risk_json":   coking_risk.to_dict(),
                    "analytics_version":  ANALYTICS_VERSION,
                    "characteristics_json": _rs_seg_dict,
                    "report_md":          report_md_rs,
                })
                # ← await выше = прогресс обновляется на каждой смене RUN_STATE
                _enqueue_segment(_rs_db_id)

                # Записать события детекций (segment_id теперь известен)
                if _rs_det_events:
                    for ev in _rs_det_events:
                        ev["segment_id"] = _rs_db_id
                    try:
                        await online_db.insert_detection_events(
                            self.router_sn, self.equip_type, self.panel_id, _rs_det_events
                        )
                    except Exception:
                        logger.warning("OnlineEngine[%s]: не удалось записать detection_events", self.key)

                # Верификация: сравниваем первый закрытый сегмент с открытым (тот же RS)
                if ci == 0:
                    from online.verifier import fire_verify as _fire_verify
                    _fire_verify(
                        seg_id=_rs_db_id,
                        unit_key=self.key,
                        run_state=seg.run_state,
                        t_start_str=seg.t_start,
                        t_end_str=seg_t_end_rs.isoformat(),
                        incr_chars=_rs_open_chars,
                        ref_chars=_rs_seg_dict,
                    )
                self.last_processed_to = seg_t_end_rs
                self.cursor_ts = _tz_utc(datetime.fromisoformat(seg.t_end))
                self.inherited_coking_risk = coking_risk
                self.inherited_run_state_sec = {}  # смена RS — счётчик сбрасывается
                self._prev_seg_hint = seg
                carry_continued_from = None   # только первый сегмент несёт ссылку
                self.continued_from_id = None
                self.forward_fill_memory = None
                self._open_history_cache = None
                self._open_history_cache_ts = None

        # Открытый сегмент = последний в списке
        open_seg = segments[-1]

        # Начало запуска для открытого сегмента:
        # если были смены RS — открытый сегмент начинается свежо; иначе — продолжает цепочку
        _open_run_origin = (
            _tz_utc(datetime.fromisoformat(open_seg.t_start))
            if closed_segs
            else (chain_origin_ts or _tz_utc(self.cursor_ts))
        )

        # Обогатить детекции счётчиками ДО _extract_open_segment_data / to_markdown
        await _enrich_open_seg_detections(
            open_seg, self.router_sn, self.equip_type, self.panel_id, self.cfg,
            run_origin_ts=_open_run_origin,
        )

        current_values, active_detections = _extract_open_segment_data(open_seg)
        coking_risk = _extract_coking_risk_from_segments(segments)

        # continued_from: если после DAILY_BOUNDARY не было смен RUN_STATE,
        # открытый сегмент сам является продолжением pre-boundary сегмента
        open_continued_from = self.continued_from_id

        try:
            from analytics.serializer import to_markdown as _to_md
            open_report_md = _to_md(
                [open_seg], self.router_sn, self.equip_type, self.panel_id,
                _tz_utc(self.cursor_ts), _tz_utc(t_to),
                ANALYTICS_VERSION, tz=self.tz, prev_seg=self._prev_seg_hint,
                fault_ref=self._fault_ref,
                inherited_run_state_sec=self.inherited_run_state_sec,
            )
        except Exception:
            open_report_md = None

        await online_db.upsert_open_segment({
            "router_sn":              self.router_sn,
            "equip_type":             self.equip_type,
            "panel_id":               self.panel_id,
            "t_start":                self.cursor_ts,
            "run_state":              open_seg.run_state,
            "coking_risk_json":       coking_risk.to_dict(),
            "analytics_version":      ANALYTICS_VERSION,
            "current_values_json":    current_values,
            "active_detections_json": active_detections,
            "characteristics_json":   {
                **open_seg.to_dict(),
                "_total_run_state_sec": {
                    **self.inherited_run_state_sec,
                    open_seg.run_state: (
                        self.inherited_run_state_sec.get(open_seg.run_state, 0.0)
                        + open_seg.duration_sec
                    ),
                },
            },
            "report_md":              open_report_md,
            "continued_from":         open_continued_from,
        })

        logger.debug(
            "OnlineEngine[%s]: открытый сегмент обновлён (run_state=%s, coking=%s)",
            self.key, open_seg.run_state, coking_risk.risk_level,
        )


# ── Вспомогательные ───────────────────────────────────────────────────────────

def _build_ff_memory(seg) -> dict | None:
    """Собрать forward-fill память из последнего подсегмента RUNNING-сегмента.

    Сохраняем последние известные значения ролей — чтобы первый срез следующего
    операционного дня не был пустым (ТЗ раздел 5.2).
    """
    if not seg.subsegments:
        return None
    last_sub = seg.subsegments[-1]
    memory: dict[str, Any] = {}
    for role, char in last_sub.characteristics.items():
        if isinstance(char, dict) and char.get("value_end") is not None:
            memory[role] = {"value": char["value_end"], "unit": char.get("unit", "")}
    return memory if memory else None
