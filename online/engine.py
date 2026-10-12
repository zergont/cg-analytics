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

from analytics.contract import CokingRisk, EVENT_SCENARIOS, STATE_SCENARIOS
from online import db as online_db

logger = logging.getLogger(__name__)

# Добавляем этот отступ перед cursor_ts при загрузке аналоговых данных,
# чтобы сегментатор имел преамбулу (последние известные значения до t_start).
_PREAMBLE_LOOKBACK_SEC = 300


def _tz_utc(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _filter_window_episodes(
    eps: list[dict], t_from: datetime, t_to: datetime, tail_sec: float,
) -> list[dict]:
    """Эпизоды, реально относящиеся к окну сегмента (для сводки).

    get_episodes_overlapping отдаёт и «дебаунс-хвосты»: эпизод родился в конце
    предыдущего сегмента, детекция умерла на смене режима, но закрытие
    задержал close_debounce — эпизод формально пересёк окно с нулевым
    воздействием. Его место в сводке сегмента, где он родился.

    Остаются: родившиеся в окне; живые на конец окна (висящая авария обязана
    показываться в каждом суточном сегменте); закрывшиеся в окне после
    реального пересечения (> tail_sec ≈ дебаунс + цикл опроса).
    """
    out = []
    for e in eps:
        t_open, t_close = e.get("t_open"), e.get("t_close")
        if t_open is not None and _tz_utc(t_open) >= t_from:
            out.append(e)
        elif t_close is None or _tz_utc(t_close) >= t_to:
            out.append(e)
        elif (_tz_utc(t_close) - t_from).total_seconds() > tail_sec:
            out.append(e)
    return out


def _episode_key(ep: dict) -> str:
    """Ключ эпизода из строки alarm_episodes — тот же, что _alert_key детекции."""
    if ep.get("scenario") == "CONTROLLER_FAULT":
        return f'CONTROLLER_FAULT|{ep.get("addr")}|{ep.get("bit")}'
    return ep.get("scenario", "?")


def _panel_cleared_at(segments: list) -> dict[str, datetime]:
    """Когда панель сняла неисправность: ключ эпизода → fault_end (самый поздний).

    Снятие панель пишет сразу, с точным временем, а движок обрабатывает окно с
    отставанием на цикл (N+1 буфер). Первый «промах» эпизода раньше ставился
    временем горизонта обработки — t_close выходил раньше реального снятия
    (06.10: 09:04:07 при снятии кнопки в 09:04:26).
    """
    out: dict[str, datetime] = {}
    for seg in segments or []:
        for sub in getattr(seg, "subsegments", None) or []:
            for d in sub.detections:
                if d.scenario != "CONTROLLER_FAULT":
                    continue
                fe = (d.values or {}).get("fault_end")
                if not fe:
                    continue
                try:
                    ts = _tz_utc(datetime.fromisoformat(fe))
                except (TypeError, ValueError):
                    continue
                key = _alert_key(d.to_dict())
                if key not in out or ts > out[key]:
                    out[key] = ts
    return out


def _drop_debounce_tails(eps: list[dict], segments: list,
                         live_episodes: dict[str, dict]) -> list[dict]:
    """Убрать из сводки ЗАКРЫВАЕМОГО сегмента эпизоды, снятые до него.

    Эпизод закрывается после нескольких чистых циклов (дебаунс), и в цикле, где
    одним пакетом пришли снятие тревоги и смена режима, он ещё открыт — сводка
    следующего короткого сегмента получала «живую на конец окна» тревогу
    (06.10: «АВАРИЯ» в 9-секундном прогреве после снятой кнопки).
    Открытый эпизод без детекции в данных самого сегмента — хвост дебаунса:
    у панели это точно (детекция строится по периоду неисправности), у
    аналитики — только если движок уже видит промах (miss > 0).
    """
    det_keys = {
        _alert_key(d.to_dict())
        for seg in segments or []
        for sub in getattr(seg, "subsegments", None) or []
        for d in sub.detections
    }
    out = []
    for ep in eps:
        if ep.get("t_close") is None:
            key = _episode_key(ep)
            if key not in det_keys:
                mem = live_episodes.get(key) or {}
                if ep.get("scenario") == "CONTROLLER_FAULT" or mem.get("miss", 0) > 0:
                    continue
        out.append(ep)
    return out


def _gate_suppressed_scenarios(gate_state: dict | None, seg) -> set[str]:
    """Сценарии аналитики, снятые гейтом на составе, активном на конец сегмента.

    Хеш снятия — из записей гейта открытой строки, которую цикл закрытия удаляет.
    """
    h = (gate_state or {}).get("suppressed_hash")
    if not h:
        return set()
    from online.status_assembler import UNCANCELLABLE_SCENARIOS, is_analytics_suppressed
    try:
        _, dets = _extract_open_segment_data(seg)
    except Exception:
        logger.warning("Не удалось снять активные тревоги сегмента", exc_info=True)
        return set()
    if not is_analytics_suppressed({"gate_suppressed_hash": h}, dets):
        return set()
    return {d.get("scenario") for d in dets
            if d.get("scenario") and d.get("scenario") != "CONTROLLER_FAULT"} - UNCANCELLABLE_SCENARIOS


def _live_new_episodes(segments: list, eps: list[dict], live_keys: set[str],
                       t_end: datetime, suppressed_sc: set[str] | None = None) -> list[dict]:
    """Тревоги, появившиеся в закрываемом сегменте и продолжающиеся в открытом.

    Живой эпизод по ним заведётся только после закрытия (_process_episodes идёт
    после сводок), эфемерный не сеется (тревога переживает рез) — в сводку
    закрываемого сегмента они не попадали, хотя детекция лежит в его данных.
    Для сводки — синтетический открытый эпизод с фазой «появилось»; снятый
    гейтом состав (suppressed_sc) помечается снятым, как пометит живой эпизод
    _process_episodes.
    """
    if not live_keys:
        return []
    # Закрытый эпизод того же ключа — прежняя вспышка, текущую он не покрывает.
    # Кроме того же онсета (v5.1.1): это та же тревога, закрытая промахом, —
    # синтетика её заменяет (вызывающий убирает строку, _drop_same_onset)
    have = {_episode_key(e) for e in eps if e.get("t_close") is None}
    closed_onsets = {(_episode_key(e), _tz_utc(e["t_open"])): e for e in eps
                     if e.get("t_close") is not None and e.get("t_open")}
    out: dict[str, dict] = {}
    for seg in segments or []:
        for sub in getattr(seg, "subsegments", None) or []:
            for d in sub.detections:
                dd = d.to_dict()
                key = _alert_key(dd)
                if (key not in live_keys or key in have or key in out
                        or dd.get("scenario") in STATE_SCENARIOS
                        or dd.get("scenario") in EVENT_SCENARIOS):
                    continue
                # Снятый панелью фронт того же бита — не живой (с v5.1.6 у «Общей
                # аварии» по детекции на каждый период: брался закрытый фронт)
                if dd.get("scenario") == "CONTROLLER_FAULT" and (dd.get("values") or {}).get("fault_end"):
                    continue
                v = dd.get("values") or {}
                try:
                    t_open = _tz_utc(datetime.fromisoformat(dd.get("t_detected")))
                except (TypeError, ValueError):
                    t_open = _tz_utc(t_end)
                panel = dd.get("scenario") == "CONTROLLER_FAULT"
                same = closed_onsets.get((key, t_open))
                out[key] = {
                    "scenario": dd.get("scenario"),
                    "source": "panel" if panel else "analytics",
                    "severity": dd.get("severity"),
                    "addr": v.get("addr") if panel else None,
                    "bit": v.get("bit") if panel else None,
                    "t_open": t_open, "t_close": None,
                    "active_sec": max(0.0, (_tz_utc(t_end) - t_open).total_seconds()),
                    "blind_sec": float((same or {}).get("blind_sec") or 0.0),
                    "gate_suppressed": not panel and dd.get("scenario") in (suppressed_sc or ()),
                    "open_values_json": {"fault_name": v.get("fault_name")} if panel else None,
                }
    return list(out.values())


def _drop_same_onset(eps: list[dict], synthetic: list[dict]) -> list[dict]:
    """Убрать закрытые строки, которые синтетика того же онсета заменяет."""
    onsets = {(_episode_key(s), _tz_utc(s["t_open"])) for s in synthetic if s.get("t_open")}
    if not onsets:
        return eps
    return [e for e in eps
            if not (e.get("t_close") is not None and e.get("t_open")
                    and (_episode_key(e), _tz_utc(e["t_open"])) in onsets)]


def _enqueue_segment(seg_id: int | None, again: bool = False) -> None:
    """Добавить закрытый сегмент в очередь Claude-анализа (Этап 2).

    Fire-and-forget: если воркер не запущен или авто-анализ выключен — молча пропускаем.
    again — повторный заказ (вердикт сегмента сменился): ставится, даже если
    сегмент сейчас в работе — заключение по старому вердикту устарело.
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
                    if again:
                        # Статус в БД — чтобы перезаказ пережил перезапуск
                        # (очередь в памяти; enqueue_pending подберёт «в очереди»)
                        from corpus.db import set_status
                        await set_status(seg_id, "queued")
                    worker.enqueue(seg_id, PRIORITY_NORMAL, again=again)
            except Exception:
                logger.warning("Автопостановка сегмента %s в очередь Claude-анализа не удалась", seg_id, exc_info=True)

        loop = _aio.get_event_loop()
        if loop.is_running():
            _aio.ensure_future(_check_and_enqueue())
    except Exception:
        logger.warning("Автопостановка сегмента %s в очередь Claude-анализа не удалась", seg_id, exc_info=True)


def _make_seg_hint(db_row: dict):
    """Duck-typed хинт для prev_seg в to_markdown: извлекает run_state и run_state_label из DB-строки."""
    from types import SimpleNamespace
    chars = db_row.get("characteristics_json")
    if isinstance(chars, str):
        try:
            import json as _j; chars = _j.loads(chars)
        except Exception:
            logger.warning("Битый characteristics_json в сегменте %s", db_row.get("id"))
            chars = None
    label = (chars.get("run_state_label") if isinstance(chars, dict) else None)
    # stop_kind обязателен: после рестарта хинт подставляется вместо настоящего
    # Segment, и обращение к отсутствующему полю дало бы AttributeError в первом
    # же цикле — то есть ровно тогда, когда движок только поднялся.
    kind = (chars.get("stop_kind") if isinstance(chars, dict) else None)
    return SimpleNamespace(run_state=db_row.get("run_state"),
                           run_state_label=label, stop_kind=kind)


def _parse_gate_state(open_row: dict | None) -> dict:
    """Записи гейта из открытой строки, которую цикл закрытия сейчас удалит.

    Гейт пишет разбор только в открытую строку; при закрытии окна она удаляется,
    а insert_closed_segment до v4.9.65 этих колонок не содержал — за всю историю
    ни один закрытый сегмент разбора не сохранил.
    """
    empty = {"analyses": [], "gate_log": [], "suppressed_hash": None}
    if not open_row:
        return empty

    def _lst(raw):
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                logger.warning("Битый JSON записей гейта открытого сегмента")
                return []
        return raw if isinstance(raw, list) else []

    return {
        "analyses":        _lst(open_row.get("warning_analyses")),
        "gate_log":        _lst(open_row.get("gate_log")),
        "suppressed_hash": open_row.get("gate_suppressed_hash"),
    }


def _gate_entry_ts(entry: dict, key: str) -> datetime | None:
    raw = entry.get(key)
    if not raw:
        return None
    try:
        return _tz_utc(datetime.fromisoformat(raw))
    except Exception:
        return None


def _slice_gate_state(state: dict, t_from, t_to=None) -> dict:
    """Записи гейта, попавшие в окно [t_from, t_to) — доля одного сегмента.

    Нарезка по времени, а не «всё в первый сегмент»: когда работа и останов
    закрываются одним циклом, разбор аварии иначе ложится на зелёный сегмент
    работы вместо красного, который и откроет диспетчер. t_to=None — хвост,
    то есть записи после конца последнего закрытого сегмента: они принадлежат
    новой открытой строке.

    gate_suppressed_hash сознательно не разносится по закрытым сегментам: он
    гасит severity в календаре, и это видимое изменение, которое надо обсуждать
    отдельно. В закрытые едут только разборы и журнал.
    """
    t_from = _tz_utc(t_from)
    t_to = _tz_utc(t_to) if t_to is not None else None

    def _inside(ts: datetime | None) -> bool:
        if ts is None or ts < t_from:
            return False
        return t_to is None or ts < t_to

    analyses = [e for e in state.get("analyses", [])
                if isinstance(e, dict) and _inside(_gate_entry_ts(e, "t"))]
    gate_log = [e for e in state.get("gate_log", [])
                if isinstance(e, dict) and _inside(_gate_entry_ts(e, "ts"))]

    out: dict = {}
    if analyses:
        out["warning_analyses"] = analyses
        out["warning_analysis_md"] = analyses[-1].get("md")
        out["warning_analyzed_hash"] = analyses[-1].get("fault_hash")
    if gate_log:
        out["gate_log"] = gate_log
    return out


def _coking_or_none(d) -> CokingRisk | None:
    """CokingRisk из dict/JSON-строки; None — записи нет или она битая."""
    if not d:
        return None
    if isinstance(d, str):
        try:
            import json as _json
            d = _json.loads(d)
        except Exception:
            logger.warning("Битый coking_risk_json")
            return None
    if not isinstance(d, dict):
        return None
    return CokingRisk.from_dict(d)


def _coking_from_json(d) -> CokingRisk:
    """Десериализовать CokingRisk из dict или JSON-строки (asyncpg отдаёт JSONB как str)."""
    return _coking_or_none(d) or CokingRisk()


async def _coking_before(router_sn: str, equip_type: str, panel_id: int,
                         ts: datetime) -> CokingRisk:
    """Накопленный риск нагара на момент ts — из последнего сегмента до него.

    Риск — долг до прожига, переживает стоп и перезапуски. Обнулять его на
    техническом событии (ручной ПУСК с более ранней даты, битая запись) — значит
    молча стереть недогруз (v5.1.3). Неизвестная история (первый старт) — ноль.
    """
    try:
        prev = await online_db.get_segment_ended_before(router_sn, equip_type, panel_id, ts)
    except Exception:
        logger.warning("Не удалось прочитать сегмент до %s для риска нагара", ts, exc_info=True)
        return CokingRisk()
    return _coking_from_json(prev.get("coking_risk_json")) if prev else CokingRisk()


def _chain_link(i: int, continued_from_id, prev_hint, seg):
    """Ссылка на предка суточной цепочки: только голова окна и только в том же режиме."""
    if i != 0 or continued_from_id is None:
        return None
    prev_rs = getattr(prev_hint, "run_state", None)
    return continued_from_id if prev_rs is None or prev_rs == seg.run_state else None


def _origin_by_rs(inh: dict[int, float] | None, head) -> datetime | None:
    """Начало состояния головы окна по накопленному времени её режима.

    Тот же источник, что строка отчёта «с начала состояния»: ссылки
    continued_from, записанные до v5.1.6, рвутся (35765 без предка — «с пуска»
    считалось от 10.10 09:00 вместо пуска 09.10 15:01, v5.1.9).
    """
    if head is None:
        return None
    sec = float((inh or {}).get(getattr(head, "run_state", None)) or 0.0)
    if sec <= 0:
        return None
    try:
        return _tz_utc(datetime.fromisoformat(head.t_start)) - timedelta(seconds=sec)
    except (TypeError, ValueError):
        return None


def _push_transition(ctx: list, seg) -> list:
    """Добавить закрытый сегмент в контекст проверок перехода."""
    from analytics.transitions import link_from_segment, push_ctx
    try:
        return push_ctx(ctx, link_from_segment(seg))
    except Exception:
        logger.warning("Контекст переходов: сегмент не добавлен", exc_info=True)
        return list(ctx)


def _rs_carry(inh: dict[int, float] | None, rs: int | None) -> dict[int, float]:
    """Унаследованное время только режима сегмента: переход внутри обрыва связи
    на суточной границе не должен тащить в отчёт время чужого режима (35690:
    «Время в останове 18 ч» в сегменте работы)."""
    v = (inh or {}).get(rs) if rs is not None else None
    return {rs: float(v)} if v else {}


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

    # Детекции только из последнего подсегмента — текущее состояние детекторов.
    # Накопленная «живая» картина ведётся отдельно через _diff_alerts / alert_journal.
    # Снятая панелью ошибка (fault_end проставлен) — исторический факт для отчёта,
    # но НЕ активная тревога: иначе она «залипает» в статусе до границы подсегмента,
    # потому что её период всегда пересекается с окном последнего подсегмента.
    active_dets: list[dict] = []
    for d in last_sub.detections:
        dd = d.to_dict()
        if (dd.get("scenario") == "CONTROLLER_FAULT"
                and (dd.get("values") or {}).get("fault_end")):
            continue
        # Разовое событие перехода — не живая тревога (v5.1.10): иначе эпизод,
        # «воздействие» на весь стоп, журнал и состав гейта
        if dd.get("scenario") in EVENT_SCENARIOS:
            continue
        active_dets.append(dd)

    return current_values, active_dets


def _put_counts(d, c: dict, run_origin_ts) -> None:
    """Счётчики «за 30 дней» и «с пуска» в values детекции; у разового события —
    без «суммарно»: воздействия у него нет (v5.1.10)."""
    d.values["history_count_30d"] = c["count_window"]
    d.values["startup_count"] = c["count_since"]
    if d.scenario not in EVENT_SCENARIOS:
        d.values["history_duration_30d_sec"] = round(c["dur_window"])
        d.values["history_blind_30d_sec"] = round(c.get("blind_window", 0))
        d.values["startup_duration_sec"] = round(c["dur_since"])
    if run_origin_ts is not None:
        d.values["startup_since"] = _tz_utc(run_origin_ts).isoformat()


def _alert_key(d: dict) -> str:
    """Ключ живой тревоги: панельные — по-битно (scenario|addr|bit),
    аналитические — по сценарию (он уникален в снимке).

    Один scenario CONTROLLER_FAULT покрывает все биты панели — без per-bit
    ключа аварии затирают друг друга в словаре живых тревог и эпизодах.
    Совпадает с online_db.episode_key.
    """
    if d.get("scenario") == "CONTROLLER_FAULT":
        v = d.get("values") or {}
        return f'CONTROLLER_FAULT|{v.get("addr")}|{v.get("bit")}'
    return d.get("scenario", "?")


def _accrual_split(
    prev_base: datetime | None, now_ts: datetime, gaps: list[dict],
) -> tuple[float, float, float]:
    """Разложить интервал цикла на (воздействие, слепая доля, время под данными).

    span — астрономическое время от прошлой базы до конца окна: залипшая
    неисправность висит на панели, пока её не сбросят, и обрыв связи её не
    отменяет, поэтому воздействие идёт и в дыре.
    gap  — сколько из span мы были слепы. Не вычитается, а копится рядом:
    сброс мог произойти внутри слепого куска.
    live — span минус gap, только для дебаунса закрытия: объявлять тревогу
    снятой, пока мы её не видим, нельзя.
    """
    from analytics.accumulators import gap_union_overlap_sec
    if prev_base is None or now_ts <= prev_base:
        return 0.0, 0.0, 0.0
    span = (now_ts - prev_base).total_seconds()
    gap = min(span, max(0.0, gap_union_overlap_sec(gaps, prev_base, now_ts)))
    return span, gap, max(0.0, span - gap)


def _live_alert_keys(seg) -> set[str]:
    """Ключи тревог, активных на конец сегмента (по последнему подсегменту).

    Такая тревога переживёт рез: сразу после закрытия окна `_process_episodes`
    заведёт по ней живой эпизод. Сеять по ней ещё и «эфемерный» закрытый —
    значит удвоить одну непрерывную тревогу.
    """
    try:
        _, dets = _extract_open_segment_data(seg)
    except Exception:
        logger.warning("Не удалось снять активные тревоги сегмента", exc_info=True)
        return set()
    return {_alert_key(d) for d in dets if d.get("scenario")}


def _diff_alerts(
    prev_map: dict[str, dict],
    curr_list: list[dict],
    ts: datetime,
    segment_id: int | None,
) -> tuple[list[dict], list[dict]]:
    """Сравнить предыдущий снимок активных тревог с текущим набором детекций.

    Возвращает (new_active, journal_events).
    new_active  — список детекций, которые должны быть в active_detections_json.
    journal_events — события для записи в alert_journal.
    """
    # Ключ per-fault: панельные аварии не затирают друг друга (все — CONTROLLER_FAULT)
    curr_map: dict[str, dict] = {_alert_key(d): d for d in curr_list}
    journal: list[dict] = []
    new_active: list[dict] = []

    for key, d in curr_map.items():
        if key not in prev_map:
            journal.append({
                "scenario":    d.get("scenario"),
                "event_type":  "OPENED",
                "ts":          ts,
                "severity":    d.get("severity"),
                "trigger":     d.get("trigger"),
                "values":      d.get("values"),
                "segment_id":  segment_id,
            })
        else:
            prev_sev = prev_map[key].get("severity")
            curr_sev = d.get("severity")
            if prev_sev != curr_sev:
                journal.append({
                    "scenario":    d.get("scenario"),
                    "event_type":  "UPDATED",
                    "ts":          ts,
                    "severity":    curr_sev,
                    "trigger":     d.get("trigger"),
                    "values":      d.get("values"),
                    "segment_id":  segment_id,
                })
        new_active.append(d)

    for key, d in prev_map.items():
        if key not in curr_map:
            journal.append({
                "scenario":    d.get("scenario"),
                "event_type":  "CLOSED",
                "ts":          ts,
                "severity":    d.get("severity"),
                "trigger":     None,
                "values":      d.get("values"),
                "segment_id":  segment_id,
            })

    return new_active, journal


_SEV_RANK = {"SHUTDOWN": 4, "WARNING": 3, "CAUTION": 2, "INFO": 1}


def _max_severity(a: str | None, b: str | None, scenario: str | None = None) -> str | None:
    """Больший из двух severity по шкале SHUTDOWN > WARNING > CAUTION > INFO.

    С scenario — с потолком аналитики (CAUTION): эпизод, восстановленный из
    журнала или переоткрытый по онсету со старым WARNING, его не протащит.
    """
    if scenario is not None:
        from analytics.contract import cap_severity
        a, b = cap_severity(scenario, a), cap_severity(scenario, b)
    return a if _SEV_RANK.get(a or "", 0) >= _SEV_RANK.get(b or "", 0) else b


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
        _src.get_whitelist_history_chunked(
            router_sn, equip_type, panel_id,
            ts_history_from, ts_to_utc,
            cfg.whitelist_analog,
        )
    )
    enum_task = asyncio.create_task(
        _src.get_enum_periods(
            router_sn, equip_type, panel_id,
            ts_from_utc, ts_to_utc,
            addrs=_src.enum_read_addrs(cfg),
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
    history, enum_periods, fault_periods, gaps = await asyncio.gather(
        history_task, enum_task, fault_task, gaps_task
    )
    # 40012/40013 — состояния из enum_history: характеристики подсегментов
    # сегментатор строит из периодов (v5.1.1). Подмена в «замер», прижатый к
    # ts_from−300 с, уходила в преамбулу и терялась при суточном закрытии
    return history, enum_periods, fault_periods, gaps


async def _load_seed(router_sn: str, equip_type: str, panel_id: int,
                     ts_from: datetime, cfg) -> dict | None:
    """Последний замер нагрузки перед окном истории и обрывы связи от него до окна.

    У головы окна преамбула истории — 300 с, а ряд LOAD_PCT при постоянном
    значении пишется раз в ~15 мин: без этого замера проверка охлаждения не
    знала удержанной нагрузки («не оценено» у 13 из 14 стопов, v5.1.10).
    """
    from analytics import source as _src
    addr = cfg.role_to_addr("LOAD_PCT")
    if addr is None:
        return None
    t_hist = _tz_utc(ts_from) - timedelta(seconds=_PREAMBLE_LOOKBACK_SEC)
    max_age = float(cfg.det("COOLDOWN_VIOLATION", "load_hold_max_sec", default=1800.0))
    try:
        row = await _src.get_last_value_before(router_sn, equip_type, panel_id, addr,
                                               t_hist, max_age)
        if not row:
            return None
        gaps = await _src.get_data_gaps(router_sn, equip_type, panel_id,
                                        _tz_utc(row["ts"]), _tz_utc(ts_from))
        return {"ts": _tz_utc(row["ts"]), "value": float(row["value"]), "gaps": gaps}
    except Exception:
        logger.warning("Удержанная нагрузка перед окном не прочитана", exc_info=True)
        return None


def _synth_connectivity_gap(
    history: list[dict],
    t_from: datetime,
    t_to: datetime,
    cfg,
) -> dict | None:
    """Синтетический «хвостовой» гэп на случай ещё не разрешённого обрыва связи.

    data_gaps пишет внешний ingest-сервис только постфактум — строка
    появляется лишь при следующем успешно принятом пакете (INSERT+UPDATE
    одной транзакцией). Пока связи нет, в data_gaps по этому обрыву нет ни
    одной записи, и суточный рез, закрывающийся строго по wall-clock, не
    может о нём узнать. Оцениваем по факту: берём максимальный ts уже
    загруженной history этого окна — если он заметно отстаёт от t_to,
    считаем, что данных не было с этого момента и добавляем гэп с открытым
    концом (gap_end=None) — дальше его подхватывает штатный клиппинг
    (_clip_gap_intervals/_compute_data_quality уже умеют gap_end IS NULL).
    """
    t_from = _tz_utc(t_from)
    t_to = _tz_utc(t_to)
    last_ts = max(
        (_tz_utc(r["ts"]) for r in history if r.get("ts") and _tz_utc(r["ts"]) < t_to),
        default=None,
    )
    gap_start = last_ts if last_ts is not None else t_from
    if gap_start >= t_to:
        return None
    heartbeat = float(cfg.seg("data_quality", "heartbeat_nominal_sec", default=30) or 30)
    max_mult  = float(cfg.seg("data_quality", "heartbeat_max_multiplier", default=3) or 3)
    if (t_to - gap_start).total_seconds() <= heartbeat * max_mult:
        return None
    return {"gap_start": gap_start, "gap_end": None}


async def _run_segment(
    router_sn: str,
    equip_type: str,
    panel_id: int,
    engine_sn: str,
    ts_from: datetime,
    ts_to: datetime,
    cfg,
    initial_coking_risk: CokingRisk,
    transition_ctx: list | None = None,
) -> tuple[list, dict[str, dict], list[dict], dict[str, dict]]:
    """Загрузить данные и запустить segment() в отдельном потоке.

    segment() — CPU-bound синхронная функция. asyncio.to_thread() выносит её
    в ThreadPoolExecutor, освобождая event loop для обработки веб-запросов.

    Третьим возвращает дыры связи окна (уже с синтетическим хвостовым гэпом):
    они нужны вызывающему, чтобы посчитать слепую долю воздействия тревог.
    Четвёртым — хронологии стоянок (ключ тот же, seg.t_start).
    """
    import functools
    from analytics.segmenter import segment as _segment

    history, enum_periods, fault_periods, gaps = await _load_data(
        router_sn, equip_type, panel_id, ts_from, ts_to, cfg
    )
    load_seed = await _load_seed(router_sn, equip_type, panel_id, ts_from, cfg)
    synth_gap = _synth_connectivity_gap(history, ts_from, ts_to, cfg)
    if synth_gap is not None:
        gaps = gaps + [synth_gap]
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
            transition_ctx=list(transition_ctx or []),
            load_seed=load_seed,
        )
    )
    # Стоп-сегменты получают ДВА артефакта, и они не исключают друг друга:
    #   акт — что произошло, окно вокруг аварии, замирает на лаге;
    #   лента — что было дальше, всё окно сегмента: видно, как неисправности
    #           снимаются одна за другой, пока аварийный стоп не станет обычным.
    # Ключ — seg.t_start.
    incidents: dict[str, dict] = {}
    chronologies: dict[str, dict] = {}
    from analytics import classifier as _clf
    from analytics.reconstructor import build_segment_chronology as _chrono
    tl_enum, tl_faults = await _timeline_for(
        segments, router_sn, equip_type, panel_id, ts_from, ts_to, cfg,
        enum_periods, fault_periods,
    )
    for seg in segments:
        if seg.run_state != 0 or not seg.t_end:
            continue
        # Исключение внутри цикла, а не снаружи: одно падение не должно
        # лишать артефактов все следующие сегменты окна
        try:
            _st = _tz_utc(datetime.fromisoformat(seg.t_start))
            _te = _tz_utc(datetime.fromisoformat(seg.t_end))
            if (getattr(seg, "stop_kind", None) == "EMERGENCY"
                    and _is_stop_head(enum_periods, _st)):
                _ctx = await _load_incident_context(
                    router_sn, equip_type, panel_id, _st, ts_to, cfg
                )
                _ep, _fp, _tlf = (
                    _ctx if _ctx else (tl_enum, fault_periods, tl_faults)
                )
                inc = _clf.build_stop_incident(
                    _ep, _fp, _st, _te, cfg,
                    stop_kind="EMERGENCY",
                    stabilization_sec=_stab_sec(cfg),
                    detail_events=_act_detail_events(cfg),
                    timeline_faults=_tlf,
                )
                if inc is not None:
                    incidents[seg.t_start] = inc
            ch = _chrono(tl_enum, tl_faults, _st, _te, cfg)
            if ch is not None:
                chronologies[seg.t_start] = ch
        except Exception:
            logger.warning(
                "_run_segment: артефакты стоп-сегмента %s не построены",
                seg.t_start, exc_info=True,
            )
    return segments, incidents, gaps, chronologies


async def _timeline_for(
    segments: list, router_sn: str, equip_type: str, panel_id: int,
    ts_from: datetime, ts_to: datetime, cfg,
    enum_periods: list[dict], fault_periods: list[dict],
) -> tuple[list[dict], list[dict]]:
    """Данные для лент стоянок окна: всё, что прислала панель.

    Нарезка и детекторы работают по KB, ленты — по каталогу БД телеметрии
    (v4.9.96). Догружаем, только если в окне есть стоянка: у работающей
    машины лент нет, а пул соединений к исходной БД общий на весь парк.
    Не вышло — ленты строятся по данным нарезки, как раньше.
    """
    if not any(getattr(s, "run_state", None) == 0 for s in segments):
        return enum_periods, fault_periods
    from analytics import source as _src
    try:
        return await _src.get_timeline_periods(
            router_sn, equip_type, panel_id,
            _tz_utc(ts_from), _tz_utc(ts_to), cfg,
        )
    except Exception:
        logger.warning("Лента %s/%s/%s: полные данные не загружены — по KB",
                       router_sn, equip_type, panel_id, exc_info=True)
        return enum_periods, fault_periods


# Запас при догрузке данных для акта: якорный стоп-период надо захватить
# вместе с предшествующим ему расхолаживанием, иначе повторный расчёт якоря
# внутри build_stop_incident не увидит входа через 4/5 (SQL берёт периоды
# строго с state_end > ts_from) и уедет в фолбэк.
_ACT_LOAD_MARGIN_SEC = 60


def _stab_sec(cfg) -> int:
    """Лаг стабилизации машины после останова, сек (KB, дефолт 60).

    Сопутствующие неисправности — горячий останов, рост температуры при
    вставшей прокачке — приходят уже после самого падения и относятся к той
    же аварии. Лаг ничего не решает, он только не даёт обрезать запись, если
    маска ушла через полминуты.
    """
    try:
        return int(cfg.seg("stop_kind", "stabilization_sec", default=60) or 60)
    except Exception:
        return 60


def _act_detail_events(cfg) -> int:
    """Сколько событий перед остановом показать поштучно (KB, дефолт 100).

    Всё окно целиком идёт в свод по видам, так что история не теряется; это
    только глубина детальной части.
    """
    from analytics.classifier import ACT_DETAIL_EVENTS
    try:
        v = cfg.seg("stop_kind", "act_detail_events", default=ACT_DETAIL_EVENTS)
        return int(ACT_DETAIL_EVENTS if v is None else v)
    except Exception:
        return ACT_DETAIL_EVENTS


def _carry_incident(open_row: dict | None, seg_start: datetime) -> dict | None:
    """Готовый акт с закрываемой открытой строки, если он про этот же стоп.

    Акт строится один раз — по истечении лага стабилизации, ещё на живом
    сегменте. При закрытии его надо перенести, а не собрать заново: документ
    вышел бы тот же, но «один раз» должно значить буквально один раз.
    Сверяем t_start, чтобы не унести акт на соседний сегмент батча.
    """
    if not open_row or open_row.get("incident_json") is None:
        return None
    row_start = open_row.get("t_start")
    if row_start is None or _tz_utc(row_start) != _tz_utc(seg_start):
        return None
    raw = open_row["incident_json"]
    if isinstance(raw, str):
        try:
            import json as _j
            raw = _j.loads(raw)
        except Exception:
            return None
    return raw if isinstance(raw, dict) else None


def _is_stop_head(enum_periods: list[dict], t_start: datetime) -> bool:
    """Начинается ли сегмент на реальном фронте входа в стоп.

    Продолжение после суточного реза — это тот же стоп-период панели, просто
    показанный со второго дня: у него сохраняется вид EMERGENCY, и без этой
    проверки он заводил СВОЙ акт со stop_ts в момент реза и пустой лентой.
    Акт строится только у головы; продолжение ссылается на неё через уже
    существующий continued_from и несёт свою хронологию.
    """
    from analytics.classifier import _ADDR_RUN_STATE
    t_start = _tz_utc(t_start)
    for p in enum_periods or []:
        if p.get("addr") != _ADDR_RUN_STATE or p.get("value") != 0:
            continue
        if _tz_utc(p["state_start"]) == t_start:
            return True
    return False


async def _load_incident_context(
    router_sn: str, equip_type: str, panel_id: int,
    stop_ts: datetime, ts_to: datetime, cfg,
) -> tuple[list[dict], list[dict], list[dict]] | None:
    """enum/fault, доведённые назад до последнего нормального останова.

    (enum, маски для классификации, все fault-события для ленты). enum —
    все регистры панели: классификация выбирает свои по адресу. Маски для
    классификации — из KB, чтобы шкала аварийности совпадала с нарезкой;
    лента и срез «висело» — по всем битовым регистрам, включая события.

    Штатное окно движка начинается ровно в момент останова (cursor_ts = t_end
    предыдущего сегмента), поэтому преамбула акта физически пуста: SQL берёт
    периоды с `state_end > ts_from` строго, и предыдущий режим — тот самый,
    что отвечает на вопрос «падение из работы или через охлаждение» — в
    выборку не попадает. Из-за этого же у характер-гейта rs_before=None и за
    30 дней строился один акт на восемь аварий.

    Здесь окно расширяется назад: сначала дешёвый поиск якоря по RUN_STATE,
    типу последней неисправности и маскам, затем полная загрузка от якоря —
    всего, что прислала панель (каталог БД, не KB). Четыре запроса, и только
    когда в цикле закрывается аварийный стоп. Тип
    неисправности в поиске обязателен: без него прошлая авария без бита в
    масках (1452) сойдёт за нормальный останов, и акт обрежет серию на ней.

    None — если догрузить не удалось; вызывающий работает на обычных данных.
    """
    from analytics import classifier as _clf
    from analytics import source as _src
    from analytics.segmenter import fault_type_shutdown

    stop_ts = _tz_utc(stop_ts)
    horizon = stop_ts - timedelta(seconds=_clf.BASELINE_SEARCH_SEC)
    _ft_addr, _ = fault_type_shutdown(cfg)
    _anchor_addrs = [_clf._ADDR_RUN_STATE] + ([_ft_addr] if _ft_addr else [])
    try:
        rs_periods, fault_periods = await asyncio.gather(
            _src.get_enum_periods(
                router_sn, equip_type, panel_id, horizon, ts_to,
                addrs=_anchor_addrs,
            ),
            _src.get_fault_periods(
                router_sn, equip_type, panel_id, horizon, ts_to,
                fault_addrs=cfg.whitelist_fault,
            ),
        )
        anchor_ts, _reason = _clf.find_baseline_anchor(
            rs_periods, fault_periods, stop_ts, cfg
        )
        load_from = anchor_ts - timedelta(seconds=_ACT_LOAD_MARGIN_SEC)
        try:
            enum_periods, timeline_faults = await _src.get_timeline_periods(
                router_sn, equip_type, panel_id, load_from, ts_to, cfg,
            )
        except Exception:
            # Полный набор за весь цикл не прочитался (тяжёлый запрос). Цикл
            # от якоря важнее полноты: откат на списки KB, акт строится один
            # раз, и терять ради лишних регистров его преамбулу нельзя.
            logger.warning(
                "Акт %s/%s/%s: полная лента от якоря не прочитана — по KB",
                router_sn, equip_type, panel_id, exc_info=True,
            )
            enum_periods, timeline_faults = await asyncio.gather(
                _src.get_enum_periods(
                    router_sn, equip_type, panel_id, load_from, ts_to,
                    addrs=_src.enum_read_addrs(cfg),
                ),
                _src.get_fault_periods(
                    router_sn, equip_type, panel_id, load_from, ts_to,
                    fault_addrs=cfg.whitelist_fault,
                ),
            )
        # Якорь может оказаться раньше горизонта: SQL отдаёт стоп-период,
        # начавшийся до горизонта и закончившийся после, с настоящим началом.
        # Тогда маски берутся от того же места, что и лента, — иначе окно
        # акта заявляет покрытие, которого у масок нет.
        if load_from < horizon:
            _wl = cfg.whitelist_fault
            fault_periods = [f for f in timeline_faults if f.get("addr") in _wl]
        return enum_periods, fault_periods, timeline_faults
    except Exception:
        logger.warning(
            "Не удалось догрузить данные для акта %s/%s/%s от %s",
            router_sn, equip_type, panel_id, stop_ts, exc_info=True,
        )
        return None


async def _collect_and_enrich_detections(
    seg,
    router_sn: str,
    equip_type: str,
    panel_id: int,
    seg_t_end: datetime,
    cfg,
    run_origin_ts: datetime,
    open_keys: set[str] | None = None,
    seeded_out: set[str] | None = None,
    gaps: list[dict] | None = None,
) -> list[dict]:
    """Для закрытого сегмента: обогатить детекции счётчиками + вернуть events.

    1. Собирает уникальные (scenario, severity, run_state, front_count) из всех подсегментов.
    2. Для сценариев без живого эпизода (короткий сегмент закрылся внутри одного
       цикла, например START_FAILURE) — создаёт сразу закрытый эпизод. Живой —
       это и то, что уже в памяти движка, и то, что активно на конец сегмента
       (память отстаёт на цикл: тревога, поднявшая RUN_STATE, в неё ещё не
       попала, и без второй половины условия одна авария давала два эпизода).
       seeded_out — сюда складываются засеянные ключи; вызывающий копит их по
       всем сегментам окна и добавляет к open_keys, иначе одна тревога,
       пересекающая несколько закрываемых сегментов, сеется по разу на каждый.
    3. Счётчики фронтов И длительности по alarm_episodes (текущий эпизод уже в БД,
       поэтому без +1).
    4. Возвращает список event-dict для insert_detection_events (переходный период).

    Мутация до seg.to_dict() / to_markdown() — счётчики попадают в отчёт.
    """
    window_days = int(cfg.det("DETECTION_COUNTER", "window_days", default=30) or 30)

    # Уникальные тревоги per-fault (панельные — по addr/bit, аналитика — по scenario)
    seen: dict[str, dict] = {}
    for sub in seg.subsegments:
        for d in sub.detections:
            k = _alert_key({"scenario": d.scenario, "values": d.values})
            # Панель — по фронту: бит, снятый и поднятый в одном сегменте, — две тревоги
            sk = (k, d.t_detected) if d.scenario == "CONTROLLER_FAULT" else k
            if sk not in seen:
                v = d.values or {}
                fc = int(v.get("front_count", 1))
                t_open = None
                if d.t_detected:
                    try:
                        t_open = _tz_utc(datetime.fromisoformat(d.t_detected))
                    except Exception:
                        logger.warning("Некорректный t_detected %r в детекции %s",
                                       d.t_detected, d.scenario)
                seen[sk] = {
                    "key": k,
                    "scenario": d.scenario, "severity": d.severity,
                    "run_state": seg.run_state, "fc": fc,
                    "addr": v.get("addr"), "bit": v.get("bit"), "t_open": t_open,
                    "values": v,
                }

    if not seen:
        return []

    # Эпизоды для эфемерных тревог — тех, у которых эпизода НЕТ ВООБЩЕ.
    # Память движка отвечает только на вопрос «жива ли тревога сейчас», а
    # тревога, прожившая своё и закрытая дебаунсом внутри сегмента, под него
    # не подходит и засевалась второй раз. Поэтому спрашиваем базу.
    if open_keys is not None:
        try:
            _already = await online_db.existing_episode_onsets(
                router_sn, equip_type, panel_id,
                [i["t_open"] for i in seen.values() if i.get("t_open")],
            )
        except Exception:
            logger.warning("Не удалось проверить существующие эпизоды", exc_info=True)
            _already = set()
        for _sk, info in seen.items():
            k = info["key"]
            if info["scenario"] in STATE_SCENARIOS:
                continue
            # Панель: живой ключ блокирует только свой (не снятый) фронт — снятые
            # раньше фронты того же бита засеваются; засеянное — по фронту
            _panel = info["scenario"] == "CONTROLLER_FAULT"
            _cleared = _panel and bool((info.get("values") or {}).get("fault_end"))
            _event = info["scenario"] in EVENT_SCENARIOS
            # Событие — только по онсету: старый живой эпизод того же сценария
            # (до v5.1.10) не должен блокировать новый (v5.1.10)
            if ((k in open_keys and not _cleared and not _event)
                    or (k, info.get("t_open")) in open_keys):
                continue
            if (k, info.get("t_open")) in _already:
                continue
            t_open = info["t_open"] or _tz_utc(seg_t_end)
            # Снятая панелью тревога закрывается временем снятия, а не концом сегмента
            t_close = _tz_utc(seg_t_end)
            _fe = (info.get("values") or {}).get("fault_end")
            if _fe:
                try:
                    t_close = min(_tz_utc(datetime.fromisoformat(_fe)), t_close)
                except (TypeError, ValueError):
                    pass
            if _event:
                t_close = t_open   # разовое событие: точечный эпизод, без воздействия
            # Воздействие — астрономическое, слепая доля считается рядом
            _span = max(0.0, (t_close - t_open).total_seconds())
            _blind = 0.0
            if gaps and _span > 0:
                from analytics.accumulators import gap_union_overlap_sec as _gos
                _blind = min(_span, _gos(gaps, t_open, t_close))
            try:
                await online_db.insert_closed_episode(
                    router_sn, equip_type, panel_id,
                    scenario=info["scenario"],
                    source="panel" if info["scenario"] == "CONTROLLER_FAULT" else "analytics",
                    severity=info["severity"],
                    t_open=t_open,
                    t_close=t_close,
                    active_sec=_span,
                    open_values=info["values"],
                    addr=info["addr"], bit=info["bit"],
                    blind_sec=_blind,
                    close_reason="transition_event" if _event else "condition_cleared",
                )
                if seeded_out is not None:
                    seeded_out.add((k, info.get("t_open")) if (_panel or _event) else k)
            except Exception:
                logger.warning("Не удалось создать эпизод для %s", k, exc_info=True)

    # Счётчики по эпизодам: WHERE по scenario, результат — per-fault ключом
    try:
        counts = await online_db.count_episodes_batch(
            router_sn, equip_type, panel_id,
            list({info["scenario"] for info in seen.values()}),
            window_days, run_origin_ts,
        )
    except Exception:
        logger.warning("Счётчики эпизодов не получены", exc_info=True)
        counts = {}

    for sub in seg.subsegments:
        for d in sub.detections:
            c = counts.get(_alert_key({"scenario": d.scenario, "values": d.values}))
            if c:
                _put_counts(d, c, run_origin_ts)

    # События для detection_events (переходный период) — по тревоге
    events = [
        {
            "scenario":    info["scenario"],
            "detected_at": info["t_open"] or seg_t_end,
            "segment_id":  None,  # обновится после insert_closed_segment
            "severity":    info["severity"],
            "run_state":   info["run_state"],
            "front_count": info["fc"],
        }
        for info in seen.values()
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

    scenarios: set[str] = {
        d.scenario
        for sub in seg.subsegments
        for d in sub.detections
    }
    if not scenarios:
        return

    try:
        counts = await online_db.count_episodes_batch(
            router_sn, equip_type, panel_id, list(scenarios),
            window_days, run_origin_ts,
        )
    except Exception:
        logger.warning("Счётчики эпизодов не получены", exc_info=True)
        counts = {}

    for sub in seg.subsegments:
        for d in sub.detections:
            c = counts.get(_alert_key({"scenario": d.scenario, "values": d.values}))
            if c:
                _put_counts(d, c, run_origin_ts)


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
        # Звенья закрытых сегментов перед курсором — контекст проверок перехода
        # (analytics.transitions, v5.1.10); после перезапуска — из auto_segments
        self._transition_ctx: list = []
        # Удержанная нагрузка перед окном открытого сегмента — одна на курсор
        self._load_seed_cache: tuple | None = None
        # Засеянные точечные эпизоды событий перехода (сценарий, онсет)
        self._events_seeded: set = set()

        self._running = False
        self._task: asyncio.Task | None = None
        # Кэш аналоговых данных открытого окна (обнуляется при смене cursor_ts)
        self._open_history_cache: list[dict] | None = None
        self._open_history_cache_ts: datetime | None = None
        # Свежесть телеметрии: максимальный ts строки history, виденной движком.
        # None = данных с момента старта ещё не было.
        self.last_data_ts: datetime | None = None
        # Живые тревоги: scenario → detection_dict (только активные, не закрытые)
        self._active_alerts: dict[str, dict] = {}
        # Эпизоды тревог: scenario → {"id", "severity", "miss", "first_miss_ts"}
        self._episodes: dict[str, dict] = {}
        # База начисления живого времени эпизодов (тикает только по времени с данными)
        self._episode_accrual_ts: datetime | None = None
        # Новые аналитические эпизоды, чью проверку на снятие гейтом пришлось
        # отложить: цикл смены режима удалил открытую строку до _process_episodes
        self._supp_recheck: tuple[list[str], list[dict]] | None = None
        # id открытого сегмента с прошлого цикла — справочная ссылка новых эпизодов
        self._last_open_seg_id: int | None = None
        # Подпись ленты открытого сегмента (t_start, число событий) — чтобы не
        # переписывать её в БД, пока в ней ничего не менялось
        self._open_chrono_sig: tuple | None = None
        # Режим открытого сегмента на прошлом цикле: стоит — значит, ленте
        # нужны все регистры, и окно читается один раз полным набором
        self._last_open_run_state: int | None = None
        # Начало стопа, по которому акт уже собран. Акт строится ОДИН раз, и
        # он же заказывает разбор у Клауда — без этой отметки и то, и другое
        # повторялось бы каждым циклом
        self._act_done_for: datetime | None = None

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
                self.inherited_coking_risk = await _coking_before(
                    self.router_sn, self.equip_type, self.panel_id, requested)
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
                _cr = _coking_or_none(last.get("coking_risk_json"))
                if _cr is None and last.get("coking_risk_json"):
                    # Битая запись — накопленное берём у сегмента перед ней
                    _cr = await _coking_before(self.router_sn, self.equip_type,
                                               self.panel_id, _tz_utc(last["t_start"]))
                self.inherited_coking_risk = _cr or CokingRisk()
                self._prev_seg_hint = _make_seg_hint(last)
                if last.get("cause_close") == "DAILY_BOUNDARY":
                    ff = last.get("forward_fill_json")
                    if isinstance(ff, str):
                        try:
                            import json as _json
                            ff = _json.loads(ff)
                        except Exception:
                            logger.warning("OnlineEngine[%s]: битый forward_fill_json в сегменте %s", self.key, last.get("id"))
                            ff = None
                    self.forward_fill_memory = ff
                    # Пустой сегмент (нет связи весь период) цепочку не продолжает —
                    # как и в рабочем пути: иначе «с пуска» уходит за обрыв (v5.1.6)
                    _dq_last = None
                    try:
                        _ch = last.get("characteristics_json")
                        _ch = json.loads(_ch) if isinstance(_ch, str) else _ch
                        _dq_last = (_ch or {}).get("data_quality")
                    except Exception:
                        _dq_last = None
                    self.continued_from_id = None if _dq_last == 0.0 else last["id"]
                    self.inherited_run_state_sec = _rs_carry(_load_rs_sec_from_ff(ff),
                                                             last.get("run_state"))
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

        await self._restore_live_state()

    def _order_incident_analysis(
        self, incident: dict, stop_ts: datetime, segment_id: int | None = None,
    ) -> None:
        """Заказать разбор аварии у Клауда. Повод — готовый акт.

        Гейт предупреждений для аварии не годился: он ждёт 60 секунд
        стабилизации состава, а у трети аварий маску снимали раньше, состав
        становился «норма» и отсчёт выбрасывался — разбора не было вовсе.
        Акт же строится у каждой аварии по построению и несёт материал
        богаче снимка состояния.

        Фоновая задача: разбор — мнение поверх фактов, цикл движка его не ждёт.
        """
        try:
            from online.incident_gate import analyze_incident
            asyncio.create_task(analyze_incident(
                self.router_sn, self.equip_type, self.panel_id,
                incident, stop_ts, segment_id,
            ))
            logger.info("OnlineEngine[%s]: заказан разбор аварии на %s",
                        self.key, stop_ts.isoformat())
        except Exception:
            logger.warning("OnlineEngine[%s]: разбор аварии не заказан",
                           self.key, exc_info=True)

    async def _restore_live_state(self) -> None:
        """Поднять из БД то, что переживает рестарт: живые тревоги и эпизоды.

        Зовётся из initialize() И из ветки ПУСКа после OPERATOR_STOP в
        менеджере — та настраивает движок вручную, минуя initialize(), и без
        этого вызова память эпизодов оставалась пустой при непустой БД:
        живые эпизоды висели открытыми навсегда, а на те же тревоги
        заводились новые.
        """
        # Контекст проверок перехода — закрытые сегменты до курсора (v5.1.10).
        # Сбой — пустой контекст: голова на стыке не оценивается, ложных нарушений нет
        try:
            from analytics.transitions import link_from_row
            _rows = await online_db.get_transition_chain(
                self.router_sn, self.equip_type, self.panel_id, _tz_utc(self.cursor_ts),
            ) if self.cursor_ts is not None else []
            self._transition_ctx = [lk for lk in (link_from_row(r) for r in _rows) if lk is not None]
        except Exception:
            self._transition_ctx = []
            logger.warning("OnlineEngine[%s]: контекст переходов не восстановлен", self.key,
                           exc_info=True)

        # Восстановить живые тревоги из журнала (незакрытые на момент рестарта)
        try:
            active = await online_db.get_active_alerts(
                self.router_sn, self.equip_type, self.panel_id
            )
            self._active_alerts = {_alert_key(a): a for a in active}
            if self._active_alerts:
                logger.info(
                    "OnlineEngine[%s]: восстановлены активные тревоги: %s",
                    self.key, list(self._active_alerts),
                )
        except Exception:
            logger.warning(
                "OnlineEngine[%s]: не удалось восстановить active_alerts из журнала",
                self.key,
            )
            self._active_alerts = {}

        # Акт открытого сегмента: если он уже собран, повторно не строим и
        # разбор не перезаказываем
        try:
            _row = await online_db.get_open_segment(
                self.router_sn, self.equip_type, self.panel_id
            )
            if _row and _row.get("incident_json") is not None and _row.get("t_start"):
                self._act_done_for = _tz_utc(_row["t_start"])
                logger.info("OnlineEngine[%s]: акт открытого сегмента уже собран (%s)",
                            self.key, self._act_done_for)
        except Exception:
            logger.warning("OnlineEngine[%s]: не удалось проверить акт открытого сегмента",
                           self.key, exc_info=True)

        # Восстановить открытые эпизоды тревог (переживают рестарт)
        try:
            eps = await online_db.get_open_episodes(
                self.router_sn, self.equip_type, self.panel_id
            )
            # eps отсортированы по t_open. Если в БД два открытых эпизода с
            # одним ключом — это след дублей: словарь молча оставил бы младший,
            # а старший висел бы открытым вечно. Закрываем старший явно.
            self._episodes = {}
            for e in eps:
                k = online_db.episode_key(e["scenario"], e.get("addr"), e.get("bit"))
                prev = self._episodes.get(k)
                if prev is not None:
                    try:
                        await online_db.close_episode(
                            prev["id"], _tz_utc(e["t_open"]), "superseded_duplicate"
                        )
                        logger.warning(
                            "OnlineEngine[%s]: дубль эпизода %s — закрыт старший #%s",
                            self.key, k, prev["id"],
                        )
                    except Exception:
                        logger.warning(
                            "OnlineEngine[%s]: не удалось закрыть дубль эпизода %s",
                            self.key, k, exc_info=True,
                        )
                self._episodes[k] = {
                    "id": e["id"], "severity": e.get("severity"),
                    "miss": 0, "first_miss_ts": None,
                    "t_open": _tz_utc(e["t_open"]) if e.get("t_open") else None,
                }
            if self._episodes:
                logger.info(
                    "OnlineEngine[%s]: восстановлены открытые эпизоды: %s",
                    self.key, list(self._episodes),
                )
        except Exception:
            logger.warning(
                "OnlineEngine[%s]: не удалось восстановить эпизоды тревог",
                self.key, exc_info=True,
            )
            self._episodes = {}

    # ── Эпизоды тревог ────────────────────────────────────────────────────────

    async def _gaps_back_to(self, t0: datetime, gaps: list[dict], gaps_from: datetime | None,
                            cache: dict) -> list[dict]:
        """Разрывы связи с t0, если голова эпизода раньше окна (v5.1.6).

        Окно знает только свои разрывы: у эпизода с фронтом 06.10, заведённого
        11.10, слепая доля головы выходила 0 при 22 ч без связи. Глубина —
        не больше 30 суток (дольше висящие неисправности — редкость).
        """
        if gaps_from is None or t0 >= gaps_from:
            return gaps
        t0 = max(t0, gaps_from - timedelta(days=30))
        if cache.get("from") is None or t0 < cache["from"]:
            try:
                from analytics import source as _src
                cache["head"] = await _src.get_data_gaps(
                    self.router_sn, self.equip_type, self.panel_id, t0, gaps_from)
                cache["from"] = t0
            except Exception:
                logger.warning("OnlineEngine[%s]: разрывы до окна не догружены — голова эпизода "
                               "без них", self.key, exc_info=True)
                return gaps
        return (cache.get("head") or []) + gaps

    async def _inherit_gate_suppression(self, scenarios: list[str], curr_dets: list[dict]) -> bool:
        """Пометить новые эпизоды снятыми, если гейт уже снял их состав.

        False — открытой строки нет (проверку надо повторить после upsert).
        """
        try:
            from online.status_assembler import UNCANCELLABLE_SCENARIOS, is_analytics_suppressed
            _row = await online_db.get_open_segment(
                self.router_sn, self.equip_type, self.panel_id)
            if not _row:
                return False
            if is_analytics_suppressed(_row, curr_dets):
                await online_db.set_episodes_gate_suppressed(
                    self.router_sn, self.equip_type, self.panel_id,
                    [s for s in scenarios if s not in UNCANCELLABLE_SCENARIOS],
                )
        except Exception:
            logger.warning("OnlineEngine[%s]: не удалось перенести снятие гейтом "
                           "на новый эпизод", self.key, exc_info=True)
        return True

    async def _process_episodes(
        self, curr_dets: list[dict], t_to: datetime, gaps: list[dict],
        cleared_at: dict[str, datetime] | None = None,
        gaps_from: datetime | None = None,
    ) -> None:
        """Материализовать эпизоды тревог из снимка активных детекций.

        Открытие — сразу; закрытие — после N циклов подряд «чисто»
        (ALARM_EPISODES.close_debounce_cycles в detectors.yaml, default 3 —
        защита счётчиков от мерцания условия у порога).

        active_sec — астрономическое воздействие, оно идёт и в дыре: залипшая
        неисправность висит на панели, пока её не сбросят. Слепая доля не
        вычитается, а копится отдельно в blind_sec. Дебаунс при этом в дыре
        замирает — объявлять тревогу снятой, пока мы её не видим, нельзя.
        """
        debounce = int(self.cfg.det("ALARM_EPISODES", "close_debounce_cycles", default=3) or 3)
        now_ts = _tz_utc(t_to)
        # Сценарии-состояния (риск нагара) — не тревоги: эпизодов у них нет
        curr_map = {_alert_key(d): d for d in curr_dets
                    if d.get("scenario") and d.get("scenario") not in STATE_SCENARIOS}

        # База — конец окна, а не последние данные (как было): иначе при
        # обрыве она замирала бы и воздействие переставало идти.
        span_sec, gap_sec, live_sec = _accrual_split(
            self._episode_accrual_ts, now_ts, gaps
        )
        self._episode_accrual_ts = now_ts

        # Открытие новых / обновление живущих
        _new_analytics: list[str] = []
        _hc: dict = {}    # кэш разрывов до окна (голова эпизода)
        for key, d in curr_map.items():
            ep = self._episodes.get(key)
            sc = d.get("scenario")
            sev = d.get("severity")
            _vals = d.get("values") or {}
            if ep is None:
                t_open = now_ts
                if d.get("t_detected"):
                    try:
                        t_open = _tz_utc(datetime.fromisoformat(d["t_detected"]))
                    except Exception:
                        pass
                # Тот же онсет — та же тревога (см. existing_episode_onsets):
                # эпизод, закрытый промахом, переоткрываем, а не заводим новый
                # от того же фронта. До v5.1.1 код 40012 жил в первых 90 с окна,
                # «Общая авария» гасла на смене зоны нагрузки и на каждом резе
                # заводилась заново — у ДЭС №3 8 строк на одну неисправность.
                prev = None
                try:
                    prev = await online_db.find_episode_by_onset(
                        self.router_sn, self.equip_type, self.panel_id,
                        scenario=sc, addr=_vals.get("addr"), bit=_vals.get("bit"),
                        t_open=t_open,
                    )
                except Exception:
                    logger.warning("OnlineEngine[%s]: не удалось проверить онсет %s",
                                   self.key, key, exc_info=True)
                if prev is not None:
                    ep_id = prev["id"]
                    was_closed = prev.get("t_close") is not None
                    # Слепая доля хвоста — от закрытия (не раньше фронта)
                    tail_blind = 0.0
                    if was_closed:
                        _t0 = max(_tz_utc(prev["t_close"]), t_open)
                        _g = await self._gaps_back_to(_t0, gaps, gaps_from, _hc)
                        _, tail_blind, _ = _accrual_split(_t0, now_ts, _g)
                    try:
                        await online_db.reopen_episode(
                            ep_id, now_ts=now_ts, blind_sec_add=tail_blind,
                            severity=_max_severity(prev.get("severity"), sev, sc),
                        )
                    except Exception:
                        logger.warning("OnlineEngine[%s]: не удалось переоткрыть эпизод %s",
                                       self.key, key, exc_info=True)
                        continue
                    if was_closed:
                        logger.info("OnlineEngine[%s]: эпизод %s переоткрыт — %s "
                                    "(тот же онсет %s)", self.key, ep_id, key,
                                    t_open.isoformat())
                    self._episodes[key] = {"id": ep_id,
                                           "severity": _max_severity(prev.get("severity"), sev, sc),
                                           "miss": 0, "first_miss_ts": None,
                                           "t_open": t_open}
                    if sc != "CONTROLLER_FAULT":
                        _new_analytics.append(sc)
                    continue
                # Время от фронта неисправности до конца этого окна. Дальше
                # тикают поцикловые приращения от той же базы, так что
                # интервалы стыкуются и не накладываются. Без этого эпизод,
                # проживший ровно один снимок, записывался нулевым.
                _g = await self._gaps_back_to(t_open, gaps, gaps_from, _hc)
                head_sec, head_blind, _ = _accrual_split(t_open, now_ts, _g)
                try:
                    ep_id = await online_db.open_episode(
                        self.router_sn, self.equip_type, self.panel_id,
                        scenario=sc,
                        source="panel" if sc == "CONTROLLER_FAULT" else "analytics",
                        severity=sev,
                        t_open=t_open,
                        open_values=_vals,
                        segment_id=self._last_open_seg_id,
                        addr=_vals.get("addr"), bit=_vals.get("bit"),
                        active_sec=head_sec, blind_sec=head_blind,
                    )
                except Exception:
                    logger.warning("OnlineEngine[%s]: не удалось открыть эпизод %s",
                                   self.key, key, exc_info=True)
                    continue
                self._episodes[key] = {"id": ep_id, "severity": sev,
                                       "miss": 0, "first_miss_ts": None,
                                       "t_open": t_open}
                if sc != "CONTROLLER_FAULT":
                    _new_analytics.append(sc)
                logger.info("OnlineEngine[%s]: эпизод открыт — %s (severity=%s)",
                            self.key, key, sev)
                if sc == "CONTROLLER_FAULT" and sev == "SHUTDOWN":
                    await self._attach_trip_context(ep_id, t_open)
            else:
                ep["miss"] = 0
                ep["first_miss_ts"] = None
                new_sev = _max_severity(ep.get("severity"), sev, sc)
                if (sc == "CONTROLLER_FAULT" and new_sev == "SHUTDOWN"
                        and ep.get("severity") != "SHUTDOWN"):
                    await self._attach_trip_context(ep["id"], now_ts)
                try:
                    await online_db.update_episode(
                        ep["id"], active_sec_add=span_sec, blind_sec_add=gap_sec,
                        severity=new_sev if new_sev != ep.get("severity") else None,
                    )
                    ep["severity"] = new_sev
                except Exception:
                    logger.warning("OnlineEngine[%s]: не удалось обновить эпизод %s",
                                   self.key, key, exc_info=True)

        # Новый аналитический эпизод того состава, который гейт уже снял: живой
        # статус по хешу открытой строки — «норма», гейт не перезапустится, и
        # флаг эпизоду иначе не поставит никто — в сводке выйдет живое замечание
        if _new_analytics and not await self._inherit_gate_suppression(_new_analytics, curr_dets):
            # Открытую строку цикл смены режима уже удалил, новая ещё не записана —
            # проверить после upsert (хвост гейта с хешем переносится туда же)
            self._supp_recheck = (_new_analytics, curr_dets)

        # Дебаунс закрытия — только когда данные реально шли (в дыре всё замирает)
        if live_sec <= 0:
            return
        for key in list(self._episodes.keys()):
            if key in curr_map:
                continue
            ep = self._episodes[key]
            ep["miss"] += 1
            if ep["first_miss_ts"] is None:
                # Панель знает точное время снятия (fault_end) — оно и есть
                # конец эпизода; иначе — горизонт обработки, как раньше.
                # Снятие раньше фронта — это прежний период той же маски
                # (_panel_cleared_at берёт самый поздний fault_end по ключу)
                _ca = (cleared_at or {}).get(key)
                if _ca is not None and ep.get("t_open") is not None and _ca < ep["t_open"]:
                    _ca = None
                ep["first_miss_ts"] = _ca or now_ts
            if ep["miss"] < debounce:
                continue
            try:
                await online_db.close_episode(
                    ep["id"], ep["first_miss_ts"], "condition_cleared"
                )
            except Exception:
                logger.warning("OnlineEngine[%s]: не удалось закрыть эпизод %s",
                               self.key, key, exc_info=True)
                continue
            logger.info("OnlineEngine[%s]: эпизод закрыт — %s", self.key, key)
            self._episodes.pop(key, None)

    # ── Контекст аварии (Фаза C) ──────────────────────────────────────────────

    async def _attach_trip_context(self, episode_id: int, t_trip: datetime) -> None:
        """Собрать и прикрепить контекст аварии к SHUTDOWN-эпизоду. Fail-open."""
        try:
            ctx = await self._build_trip_context(t_trip)
            await online_db.set_episode_context(episode_id, ctx)
            logger.info("OnlineEngine[%s]: контекст аварии собран (эпизод %s)",
                        self.key, episode_id)
        except Exception:
            logger.warning("OnlineEngine[%s]: не удалось собрать контекст аварии",
                           self.key, exc_info=True)

    async def _build_trip_context(self, t_trip: datetime) -> dict:
        """Контекст SHUTDOWN: что машина делала до отключения.

        «Стоял 2 суток и нажали кнопку на ТО» и «работал на 100% и отключился» —
        принципиально разные происшествия. Содержимое: предыдущий закрытый
        сегмент детально, тренд trip_snapshot-ролей (mapping.yaml KB) за
        последние минуты, висевшие тревоги, компактная сводка 24ч.
        """
        t_trip = _tz_utc(t_trip)
        ctx: dict[str, Any] = {"t_trip": t_trip.isoformat()}

        # 1. Предыдущий закрытый сегмент — режим, длительность, зоны нагрузки
        try:
            prev = await online_db.get_last_closed_segment(
                self.router_sn, self.equip_type, self.panel_id
            )
            if prev:
                chars = prev.get("characteristics_json")
                if isinstance(chars, str):
                    try:
                        chars = json.loads(chars)
                    except Exception:
                        chars = {}
                chars = chars if isinstance(chars, dict) else {}
                zones: dict[str, float] = {}
                for sub in chars.get("subsegments", []):
                    z = sub.get("load_zone") or "NA"
                    zones[z] = zones.get(z, 0.0) + float(sub.get("duration_sec") or 0)
                t_s, t_e = prev.get("t_start"), prev.get("t_end")
                ctx["prev_segment"] = {
                    "run_state":       prev.get("run_state"),
                    "run_state_label": chars.get("run_state_label"),
                    "t_start":         _tz_utc(t_s).isoformat() if t_s else None,
                    "t_end":           _tz_utc(t_e).isoformat() if t_e else None,
                    "duration_sec":    round((_tz_utc(t_e) - _tz_utc(t_s)).total_seconds())
                                       if t_s and t_e else None,
                    "cause_close":     prev.get("cause_close"),
                    "zones_sec":       {z: round(v) for z, v in zones.items()},
                }
        except Exception:
            logger.warning("OnlineEngine[%s]: контекст — предыдущий сегмент не собран",
                           self.key, exc_info=True)

        # 2. Тренд ключевых параметров перед аварией (из кэша открытого окна)
        try:
            window_min = int(self.cfg.det(
                "ALARM_EPISODES", "trip_trend_window_min", default=15) or 15)
            t0 = t_trip - timedelta(minutes=window_min)
            hist = self._open_history_cache or []
            params: dict[str, dict] = {}
            for role in self.cfg.trip_snapshot_roles:
                addr = self.cfg.role_to_addr(role)
                if not addr:
                    continue
                vals = [
                    float(r["value"]) for r in hist
                    if r["addr"] == addr and r.get("value") is not None
                    and t0 <= _tz_utc(r["ts"]) <= t_trip
                ]
                if not vals:
                    continue
                params[role] = {
                    "first": round(vals[0], 2),
                    "last":  round(vals[-1], 2),
                    "min":   round(min(vals), 2),
                    "max":   round(max(vals), 2),
                    "unit":  self.cfg.role_unit(role),
                }
            if params:
                ctx["trend"] = {"window_min": window_min, "params": params}
        except Exception:
            logger.warning("OnlineEngine[%s]: контекст — тренд не собран",
                           self.key, exc_info=True)

        # 3. Висевшие тревоги на момент аварии (кроме самой панельной)
        try:
            eps = await online_db.get_open_episodes(
                self.router_sn, self.equip_type, self.panel_id
            )
            open_alarms = [
                {
                    "scenario":        e["scenario"],
                    "severity":        e.get("severity"),
                    "since":           _tz_utc(e["t_open"]).isoformat(),
                    "active_sec":      round(e.get("active_sec") or 0),
                    "blind_sec":       round(e.get("blind_sec") or 0),
                    "gate_suppressed": bool(e.get("gate_suppressed")),
                }
                for e in eps if e["scenario"] != "CONTROLLER_FAULT"
            ]
            if open_alarms:
                ctx["open_alarms"] = open_alarms
        except Exception:
            logger.warning("OnlineEngine[%s]: контекст — открытые эпизоды не собраны",
                           self.key, exc_info=True)

        # 4. Компактная сводка 24ч: пуски, наработка, максимальная зона
        try:
            segs = await online_db.get_segments_for_calendar(
                self.router_sn, self.equip_type, self.panel_id,
                ts_from=t_trip - timedelta(hours=24), ts_to=t_trip,
            )
            zone_rank = {"NA": 0, "LOW": 1, "NORMAL": 2, "ELEVATED": 3, "OVERLOAD": 4}
            starts = 0
            running_sec = 0.0
            max_zone = "NA"
            for s in segs:
                if s.get("run_state") != 3:
                    continue
                starts += 1
                t_s, t_e = s.get("t_start"), s.get("t_end")
                if t_s:
                    running_sec += (_tz_utc(t_e or t_trip) - _tz_utc(t_s)).total_seconds()
                chars = s.get("characteristics_json")
                if isinstance(chars, str):
                    try:
                        chars = json.loads(chars)
                    except Exception:
                        chars = {}
                if isinstance(chars, dict):
                    for sub in chars.get("subsegments", []):
                        z = sub.get("load_zone") or "NA"
                        if zone_rank.get(z, 0) > zone_rank.get(max_zone, 0):
                            max_zone = z
            ctx["last_24h"] = {
                "starts_count":  starts,
                "running_sec":   round(running_sec),
                "max_load_zone": max_zone,
            }
        except Exception:
            logger.warning("OnlineEngine[%s]: контекст — сводка 24ч не собрана",
                           self.key, exc_info=True)

        return ctx

    # ── Summary-отчёт (Фаза D) ────────────────────────────────────────────────

    async def _build_summary_md_for(
        self, segments: list, t_from: datetime, t_to: datetime,
        closing: bool = False, live_keys: set[str] | None = None,
        suppressed_sc: set[str] | None = None,
    ) -> tuple[str | None, dict | None]:
        """Сводка сегмента: (report_summary_md, части) — вердикт, замечания, показатели.

        Части (report_summary_json) — единый источник вердикта сегмента, md
        печатается из них. closing — сводка закрываемого сегмента: открытые
        эпизоды без детекции в его данных — хвосты дебаунса, в сводку не идут
        (_drop_debounce_tails).
        """
        try:
            eps = await online_db.get_episodes_overlapping(
                self.router_sn, self.equip_type, self.panel_id,
                _tz_utc(t_from), _tz_utc(t_to),
            )
            debounce = int(self.cfg.det(
                "ALARM_EPISODES", "close_debounce_cycles", default=3) or 3)
            eps = _filter_window_episodes(
                eps, _tz_utc(t_from), _tz_utc(t_to),
                tail_sec=(debounce + 1) * self.poll_interval_sec,
            )
            if closing:
                eps = _drop_debounce_tails(eps, segments, self._episodes)
                _syn = _live_new_episodes(segments, eps, live_keys or set(), _tz_utc(t_to),
                                          suppressed_sc)
                eps = _drop_same_onset(eps, _syn) + _syn
            from analytics.serializer import build_summary_parts, render_summary_md
            parts = build_summary_parts(segments, episodes=eps, tz=self.tz,
                                        trip_roles=self.cfg.trip_snapshot_roles,
                                        t_from=_tz_utc(t_from), t_to=_tz_utc(t_to))
            return render_summary_md(parts), parts
        except Exception:
            logger.warning("OnlineEngine[%s]: не удалось построить summary-отчёт",
                           self.key, exc_info=True)
            return None, None

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

    async def _open_load_seed(self, ts_from: datetime) -> dict | None:
        """Удержанная нагрузка перед окном — кэш на курсор (окно открытого сегмента
        начинается с одного и того же места, пока сегмент не закроется)."""
        key = _tz_utc(ts_from)
        if self._load_seed_cache and self._load_seed_cache[0] == key:
            return self._load_seed_cache[1]
        seed = await _load_seed(self.router_sn, self.equip_type, self.panel_id, key, self.cfg)
        self._load_seed_cache = (key, seed)
        return seed

    async def _seed_open_events(self, open_seg, t_to: datetime) -> None:
        """Точечный эпизод разового события открытого сегмента (v5.1.10).

        Иначе пока стоп открыт (часами), счётчики «за 30 дней» и хронология гейта
        его не видят. Сеется после стабилизации (60 с от онсета), один раз на
        (сценарий, онсет); при закрытии повтор отсекает existing_episode_onsets.
        """
        stab = float(self.cfg.det("ALARM_EPISODES", "event_seed_after_sec", default=60.0) or 60.0)
        cand = []
        for sub in open_seg.subsegments or []:
            for d in sub.detections:
                if d.scenario not in EVENT_SCENARIOS:
                    continue
                try:
                    t_open = _tz_utc(datetime.fromisoformat(d.t_detected))
                except (TypeError, ValueError):
                    continue
                if (d.scenario, t_open) in self._events_seeded:
                    continue
                if (_tz_utc(t_to) - t_open).total_seconds() < stab:
                    continue
                cand.append((d, t_open))
        if not cand:
            return
        try:
            have = await online_db.existing_episode_onsets(
                self.router_sn, self.equip_type, self.panel_id, [t for _, t in cand])
        except Exception:
            logger.warning("OnlineEngine[%s]: онсеты событий не проверены", self.key, exc_info=True)
            return
        for d, t_open in cand:
            key = (d.scenario, t_open)
            if (_alert_key(d.to_dict()), t_open) in have:
                self._events_seeded.add(key)
                continue
            try:
                await online_db.insert_closed_episode(
                    self.router_sn, self.equip_type, self.panel_id,
                    scenario=d.scenario, source="analytics", severity=d.severity,
                    t_open=t_open, t_close=t_open, active_sec=0.0,
                    open_values=d.values, blind_sec=0.0, close_reason="transition_event",
                )
                self._events_seeded.add(key)
            except Exception:
                logger.warning("OnlineEngine[%s]: точечный эпизод события не заведён",
                               self.key, exc_info=True)

    def _fill_coking_accum_end(self, t_from: datetime) -> None:
        """Конец накопления нагара, если в перенесённом состоянии его нет (v5.1.9).

        Цепочка ДГУ №2 перенесена из JSON до v5.1.3 без last_accum_ts: «Перед
        пуском» печаталось без «по DD.MM», и модель приняла дату прожига за дату
        работы («риск накопился с последней работы 06.07»). Машина стоит —
        накопление закончилось не позже начала стоянки.
        """
        cr = self.inherited_coking_risk
        if cr is None or cr.last_accum_ts or float(cr.debt_sec or 0) <= 0:
            return
        sec = float((self.inherited_run_state_sec or {}).get(0) or 0)
        if sec <= 0:
            return
        cr.last_accum_ts = (_tz_utc(t_from) - timedelta(seconds=sec)).isoformat()

    async def _close_window(
        self,
        t_from: datetime,
        t_to: datetime,
        close_reason: str,
    ) -> None:
        """Запустить полный анализ [t_from, t_to], сохранить все сегменты как закрытые."""
        self._fill_coking_accum_end(t_from)
        if t_to <= t_from:
            return

        from analytics.runner import ANALYTICS_VERSION

        try:
            segments, incidents, gaps, chronologies = await _run_segment(
                self.router_sn, self.equip_type, self.panel_id, self.engine_sn,
                t_from, t_to, self.cfg,
                self.inherited_coking_risk,
                transition_ctx=self._transition_ctx,
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
                    logger.warning("OnlineEngine[%s]: битый characteristics_json открытого сегмента", self.key)
            elif isinstance(_raw, dict):
                _open_chars = _raw

        # Записи гейта удаляемой строки — разложим по закрытым сегментам ниже
        _gate_state = _parse_gate_state(_open_row)

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
        # active_detections из _active_alerts: тревоги живут сквозь смену RUN_STATE.
        _seed_cv, _ = _extract_open_segment_data(segments[-1])
        _seed_dets = list(self._active_alerts.values())
        _seed_open_id = await online_db.upsert_open_segment({
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

        # Предок головы окна — для ссылки цепочки (до того как цикл сдвинет подсказку)
        _prev_hint_i0 = self._prev_seg_hint

        # Вычислить начало текущего непрерывного запуска (для счётчика «с пуска»):
        # по накопленному времени режима, запасной путь — цепочка continued_from
        chain_origin_ts: datetime | None = (
            _origin_by_rs(self.inherited_run_state_sec, segments[0] if segments else None)
            if self.continued_from_id is not None else None
        )
        if self.continued_from_id is not None and chain_origin_ts is None:
            try:
                _origin = await online_db.get_run_state_origin_ts(self.continued_from_id)
                chain_origin_ts = _tz_utc(_origin) if _origin else None
            except Exception:
                logger.warning("OnlineEngine[%s]: не удалось получить начало запуска для счётчика «с пуска»", self.key, exc_info=True)

        last_saved_id: int | None = None
        # Накопленное время RS до этого батча (из предыдущей суточной цепочки)
        running_rs_sec = dict(self.inherited_run_state_sec)
        # t_end последнего успешно сохранённого сегмента (для отката cursor_ts при пропуске границы)
        last_committed_t_end: datetime = _tz_utc(t_from)

        # Тревоги, активные на конец окна: живой эпизод по ним заведётся на
        # следующем цикле, поэтому «эфемерными» их считать нельзя
        _live_keys = _live_alert_keys(segments[-1])
        _supp_sc = _gate_suppressed_scenarios(_gate_state, segments[-1])
        # Уже засеянные в этом окне — одна тревога на несколько сегментов
        # должна дать один эпизод, а не по одному на сегмент
        _seeded: set[str] = set()

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

            # Сегмент без единой строки телеметрии (полный обрыв связи):
            # заглушка вместо отчёта, без corpus-анализа, цепочка ff рвётся.
            _seg_no_data = seg.data_quality == 0.0

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
            seg_inherited_rs = _rs_carry(running_rs_sec, seg.run_state) if i == 0 else {}

            # forward-fill память + накопленное время RS для суточного реза
            # (пустой сегмент память не строит — значения до обрыва не переносятся)
            ff_json = None
            updated_rs_sec: dict[int, float] | None = None
            if is_last and close_reason == "DAILY_BOUNDARY" and not _seg_no_data:
                if i == 0:
                    # Только время своего режима: чужой ключ — устаревший остаток
                    updated_rs_sec = {seg.run_state:
                                      seg_inherited_rs.get(seg.run_state, 0.0) + seg.duration_sec}
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
                if _chain_link(i, self.continued_from_id, _prev_hint_i0, seg) is not None
                else _tz_utc(datetime.fromisoformat(seg.t_start))
            )

            # Обогатить детекции счётчиками ДО to_dict() / to_markdown()
            _det_events = [] if _seg_no_data else await _collect_and_enrich_detections(
                seg, self.router_sn, self.equip_type, self.panel_id,
                seg_t_end, self.cfg, run_origin_ts=_run_origin,
                open_keys=set(self._episodes) | _live_keys | _seeded,
                seeded_out=_seeded, gaps=gaps,
            )

            seg_dict = seg.to_dict()
            seg_dict["t_end"] = seg_t_end.isoformat()
            seg_dict["cause_close"] = seg_cause_close

            # prev_seg для отчёта: предыдущий в текущем батче или хинт из движка
            ps = segments[i - 1] if i > 0 else self._prev_seg_hint

            # Генерация Markdown-отчёта для закрытого сегмента
            if _seg_no_data:
                from analytics.serializer import build_no_data_report as _nd_report
                from analytics.serializer import no_data_summary_parts as _nd_parts
                summary_parts = _nd_parts()
                report_md, summary_md = _nd_report(
                    self.router_sn, self.equip_type, self.panel_id,
                    _tz_utc(datetime.fromisoformat(seg.t_start)), seg_t_end,
                    tz=self.tz, last_data_ts=self.last_data_ts,
                )
            else:
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
                    logger.exception("OnlineEngine[%s]: не удалось построить отчёт закрытого сегмента, сохраняю без report_md", self.key)
                    report_md = None

                summary_md, summary_parts = await self._build_summary_md_for(
                    [seg], datetime.fromisoformat(seg.t_start), seg_t_end, closing=True,
                    live_keys=_live_keys if is_last else None,
                    suppressed_sc=_supp_sc,
                )

            db_id = await online_db.insert_closed_segment({
                "router_sn":          self.router_sn,
                "equip_type":         self.equip_type,
                "panel_id":           self.panel_id,
                "t_start":            _tz_utc(datetime.fromisoformat(seg.t_start)),
                "t_end":              seg_t_end,
                "run_state":          seg.run_state,
                "cause_close":        seg_cause_close,
                "split_reason":       "DAILY_BOUNDARY" if seg_cause_close == "DAILY_BOUNDARY" else None,
                # Голова окна в том же режиме — продолжение суточной цепочки. Без
                # ссылки начало пуска обрывалось на каждом резе: «Срабатываний с
                # пуска: 0» у эпизода, открытого в момент пуска (v5.1.6)
                "continued_from":     (None if _seg_no_data
                                       else _chain_link(i, self.continued_from_id, _prev_hint_i0, seg)),
                "coking_risk_json":   coking_risk.to_dict(),
                "forward_fill_json":  ff_json,
                "analytics_version":  ANALYTICS_VERSION,
                "characteristics_json": seg_dict,
                "report_md":          report_md,
                "report_summary_md":  summary_md,
                "report_summary_json": summary_parts,
                # Пустой сегмент (нет связи) акта не лишается: акт и лента
                # собираются из enum-периодов и фронтов масок, аналоговых
                # данных не трогают. Заглушка «нет связи» существует потому,
                # что врать нельзя про аналоги; здесь врать нечем. И самое
                # ценное как раз тут — с какой маской машина ушла в обрыв.
                "incident_json":      incidents.get(seg.t_start),
                "chronology_json":    chronologies.get(seg.t_start),
                **_slice_gate_state(
                    _gate_state,
                    _tz_utc(datetime.fromisoformat(seg.t_start)), seg_t_end,
                ),
            })
            # ← await выше = event loop обслужил API. Сигналим прогресс сразу.
            if _seg_no_data:
                logger.info(
                    "OnlineEngine[%s]: сегмент %s пустой (нет связи весь период) — corpus-анализ пропущен",
                    self.key, db_id,
                )
            else:
                _enqueue_segment(db_id)

            # Акт, собранный суточным резом: на открытой строке его не было
            # (авария началась в пределах лага до границы), значит и разбор
            # заказывается отсюда — иначе он бы не заказался вовсе
            _daily_inc = incidents.get(seg.t_start)
            if _daily_inc is not None:
                _daily_st = _tz_utc(datetime.fromisoformat(seg.t_start))
                if self._act_done_for != _daily_st:
                    self._act_done_for = _daily_st
                    self._order_incident_analysis(_daily_inc, _daily_st, db_id)

            # Связь эпизод→сегмент: ссылка на открытую строку обнуляется
            # при её удалении, поэтому перецеливаем на закрытую (v4.9.75)
            try:
                if db_id:
                    await online_db.retarget_episode_segments(
                        self.router_sn, self.equip_type, self.panel_id,
                        db_id, _tz_utc(datetime.fromisoformat(seg.t_start)), seg_t_end,
                    )
            except Exception:
                logger.warning("OnlineEngine[%s]: не удалось привязать эпизоды к сегменту %s",
                               self.key, db_id, exc_info=True)

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
            self._transition_ctx = _push_transition(self._transition_ctx, seg)
            last_committed_t_end = seg_t_end
            # Риск нагара — вместе с курсором: если суточную границу в переходном
            # режиме пропустят (break ниже), следующее окно начнётся с конца этого
            # сегмента — и с его накопленным долгом, а не с начала окна
            self.inherited_coking_risk = coking_risk

            if is_last:
                last_saved_id = db_id
                self.cursor_ts = _tz_utc(t_to)
                self.inherited_coking_risk = coking_risk
                # Пустой сегмент рвёт суточную цепочку: forward-fill и счётчик
                # «с пуска» из данных до обрыва — не контекст, а устаревшие данные.
                if close_reason == "DAILY_BOUNDARY" and not _seg_no_data:
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

        # Хвост записей гейта — в пересеянную открытую строку: они сделаны позже
        # конца последнего закрытого сегмента и принадлежат уже новому сегменту.
        if _seed_open_id:
            _tail = _slice_gate_state(_gate_state, last_committed_t_end)
            if _gate_state.get("suppressed_hash"):
                _tail["gate_suppressed_hash"] = _gate_state["suppressed_hash"]
            try:
                await online_db.append_segment_gate_state(_seed_open_id, _tail)
            except Exception:
                logger.warning("OnlineEngine[%s]: хвост записей гейта не восстановлен",
                               self.key, exc_info=True)

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
        self._fill_coking_accum_end(t_from)

        from analytics.runner import ANALYTICS_VERSION

        try:
            import time as _time
            from analytics import source as _src
            import functools
            from analytics.segmenter import segment as _segment_fn

            _OVERLAP = 120  # сек перекрытия при дозагрузке хвоста (защита от поздних строк)
            ts_from_utc = _tz_utc(t_from)
            ts_to_utc   = _tz_utc(t_to)

            _t_cycle_start = _time.perf_counter()
            _t0 = _time.perf_counter()
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
                    "TIMING[%s]: history кэш=%d + хвост=%d за %.2fs",
                    self.key, len(stable), len(tail), _time.perf_counter() - _t0,
                )
            else:
                preamble_floor = ts_from_utc - timedelta(seconds=_PREAMBLE_LOOKBACK_SEC)
                # Холодная загрузка после рестарта: окно может быть многочасовым,
                # грузим порциями чтобы каждый запрос влезал в command_timeout
                history = await _src.get_whitelist_history_chunked(
                    self.router_sn, self.equip_type, self.panel_id,
                    preamble_floor, ts_to_utc, self.cfg.whitelist_analog,
                )
                logger.debug(
                    "TIMING[%s]: history полная загрузка=%d строк за %.2fs",
                    self.key, len(history), _time.perf_counter() - _t0,
                )

            self._open_history_cache = history
            self._open_history_cache_ts = max((r["ts"] for r in history), default=None)

            # Свежесть телеметрии: продвинулась — фиксируем в памяти и в БД
            if self._open_history_cache_ts is not None and (
                self.last_data_ts is None or self._open_history_cache_ts > self.last_data_ts
            ):
                self.last_data_ts = _tz_utc(self._open_history_cache_ts)
                try:
                    await online_db.update_observation_last_data_ts(
                        self.router_sn, self.equip_type, self.panel_id, self.last_data_ts
                    )
                except Exception:
                    logger.warning("OnlineEngine[%s]: не удалось сохранить last_data_ts", self.key, exc_info=True)

            _t0 = _time.perf_counter()
            # Машина стояла на прошлом цикле — лента понадобится почти
            # наверняка: читаем окно один раз полным набором и режем из него
            # вход нарезки, а не читаем то же окно дважды (пул к исходной
            # БД — три соединения на весь парк, а резерв стоит неделями)
            tl_pre: tuple[list, list] | None = None
            if self._last_open_run_state == 0:
                (tl_e, tl_f), gaps = await asyncio.gather(
                    _src.get_timeline_periods(
                        self.router_sn, self.equip_type, self.panel_id,
                        ts_from_utc, ts_to_utc, self.cfg,
                    ),
                    _src.get_data_gaps(
                        self.router_sn, self.equip_type, self.panel_id,
                        ts_from_utc, ts_to_utc,
                    ),
                )
                tl_pre = (tl_e, tl_f)
                enum_periods, fault_periods = _src.kb_slice(tl_e, tl_f, self.cfg)
            else:
                enum_periods, fault_periods, gaps = await asyncio.gather(
                    _src.get_enum_periods(
                        self.router_sn, self.equip_type, self.panel_id,
                        ts_from_utc, ts_to_utc, addrs=_src.enum_read_addrs(self.cfg),
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
            logger.debug(
                "TIMING[%s]: gather(enum=%d, fault=%d, gaps=%d) за %.2fs",
                self.key, len(enum_periods), len(fault_periods), len(gaps),
                _time.perf_counter() - _t0,
            )

            synth_gap = _synth_connectivity_gap(history, ts_from_utc, ts_to_utc, self.cfg)
            if synth_gap is not None:
                gaps = gaps + [synth_gap]

            # 40012/40013 — состояния: сегментатор берёт их из enum_periods (v5.1.1)

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
                    transition_ctx=list(self._transition_ctx),
                    load_seed=await self._open_load_seed(ts_from_utc),
                )
            )
            if tl_pre is not None:
                tl_enum, tl_faults = tl_pre
            else:
                # Первый цикл стоянки: полный набор ещё не читался
                tl_enum, tl_faults = await _timeline_for(
                    segments, self.router_sn, self.equip_type, self.panel_id,
                    ts_from_utc, ts_to_utc, self.cfg, enum_periods, fault_periods,
                )
            self._last_open_run_state = (
                segments[-1].run_state if segments else self._last_open_run_state
            )
        except Exception:
            logger.exception("OnlineEngine[%s]: ошибка анализа открытого окна", self.key)
            return

        if not segments:
            return

        # Вычислить начало текущего непрерывного запуска (для счётчика «с пуска»):
        # по накопленному времени режима, запасной путь — цепочка continued_from
        chain_origin_ts: datetime | None = (
            _origin_by_rs(self.inherited_run_state_sec, segments[0] if segments else None)
            if self.continued_from_id is not None else None
        )
        if self.continued_from_id is not None and chain_origin_ts is None:
            try:
                _origin = await online_db.get_run_state_origin_ts(self.continued_from_id)
                chain_origin_ts = _tz_utc(_origin) if _origin else None
            except Exception:
                logger.warning("OnlineEngine[%s]: не удалось получить начало запуска для счётчика «с пуска»", self.key, exc_info=True)

        # Новые закрытые сегменты: смены RUN_STATE и устранение неисправностей
        # (FAULT_CLEARED режет СТОП-период — границу ставит сегментатор)
        closed_segs = [
            s for s in segments
            if s.cause_close in ("RUN_STATE_CHANGE", "FAULT_CLEARED", "SHUTDOWN_CLEARED")
        ]
        _rs_gate_state: dict | None = None
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
                        logger.warning("OnlineEngine[%s]: битый characteristics_json открытого сегмента", self.key)
                elif isinstance(_raw_rs, dict):
                    _rs_open_chars = _raw_rs

            # Записи гейта удаляемой строки — разложим по закрытым сегментам ниже
            _rs_gate_state = _parse_gate_state(_rs_open_row)

            await online_db.delete_open_segment(
                self.router_sn, self.equip_type, self.panel_id
            )
            # Первый закрытый сегмент в окне может быть продолжением после DAILY_BOUNDARY
            carry_continued_from = self.continued_from_id
            # Подсказка предка головы окна — цикл ниже перезаписывает _prev_seg_hint
            _prev_hint_rs0 = self._prev_seg_hint
            # Унаследованное RS время: ci==0 продолжает цепочку, ci>0 — новый RS
            _rs_inherited_before = dict(self.inherited_run_state_sec)
            # Тревоги, активные в остающемся открытом сегменте: по ним ниже в
            # этом же цикле заведётся живой эпизод (память движка отстаёт)
            _live_keys = _live_alert_keys(segments[-1])
            _rs_supp_sc = _gate_suppressed_scenarios(_rs_gate_state, segments[-1])
            # Уже засеянные в этом окне — см. тот же накопитель в _close_window
            _seeded: set[str] = set()
            for ci, seg in enumerate(closed_segs):
                coking_risk = _extract_coking_risk_from_segments([seg])
                seg_t_end_rs = _tz_utc(datetime.fromisoformat(seg.t_end))
                ps_rs = closed_segs[ci - 1] if ci > 0 else self._prev_seg_hint
                # Пустой сегмент (нет связи весь период): заглушка, без corpus-анализа
                _rs_no_data = seg.data_quality == 0.0

                # Начало запуска: голова окна продолжает цепочку только в том же
                # режиме (как в _close_window), иначе — новый запуск
                _rs_link = (None if _rs_no_data
                            else _chain_link(ci, carry_continued_from, _prev_hint_rs0, seg))
                _rs_run_origin = (
                    (chain_origin_ts or _tz_utc(datetime.fromisoformat(seg.t_start)))
                    if _rs_link is not None
                    else _tz_utc(datetime.fromisoformat(seg.t_start))
                )

                # Обогатить детекции счётчиками ДО to_dict() / to_markdown()
                _rs_det_events = [] if _rs_no_data else await _collect_and_enrich_detections(
                    seg, self.router_sn, self.equip_type, self.panel_id,
                    seg_t_end_rs, self.cfg, run_origin_ts=_rs_run_origin,
                    open_keys=set(self._episodes) | _live_keys | _seeded,
                    seeded_out=_seeded, gaps=gaps,
                )

                if _rs_no_data:
                    from analytics.serializer import build_no_data_report as _nd_report
                    from analytics.serializer import no_data_summary_parts as _nd_parts
                    summary_parts_rs = _nd_parts()
                    report_md_rs, summary_md_rs = _nd_report(
                        self.router_sn, self.equip_type, self.panel_id,
                        _tz_utc(datetime.fromisoformat(seg.t_start)), seg_t_end_rs,
                        tz=self.tz, last_data_ts=self.last_data_ts,
                    )
                else:
                    try:
                        from analytics.serializer import to_markdown as _to_md
                        report_md_rs = _to_md(
                            [seg], self.router_sn, self.equip_type, self.panel_id,
                            _tz_utc(datetime.fromisoformat(seg.t_start)), seg_t_end_rs,
                            ANALYTICS_VERSION, tz=self.tz, prev_seg=ps_rs,
                            fault_ref=self._fault_ref,
                            inherited_run_state_sec=(_rs_carry(_rs_inherited_before, seg.run_state)
                                                     if ci == 0 else {}),
                        )
                    except Exception:
                        logger.exception("OnlineEngine[%s]: не удалось построить отчёт сегмента RUN_STATE_CHANGE, сохраняю без report_md", self.key)
                        report_md_rs = None

                    summary_md_rs, summary_parts_rs = await self._build_summary_md_for(
                        [seg], datetime.fromisoformat(seg.t_start), seg_t_end_rs,
                        closing=True,
                        live_keys=_live_keys if ci == len(closed_segs) - 1 else None,
                        suppressed_sc=_rs_supp_sc,
                    )

                _rs_seg_dict = seg.to_dict()
                _rs_incident = None
                _rs_chronology = None
                # Пустой сегмент не исключение — см. суточный путь
                if seg.run_state == 0:
                    try:
                        from analytics import classifier as _clf
                        from analytics.reconstructor import (
                            build_segment_chronology as _chrono,
                        )
                        _rs_st = _tz_utc(datetime.fromisoformat(seg.t_start))
                        # Акт собран один раз и не пересобирается: если он уже
                        # лежит на закрываемой открытой строке — переносим как
                        # есть. Пересборка дала бы тот же документ, но «один
                        # раз» должно значить буквально один раз.
                        _rs_incident = _carry_incident(_rs_open_row, _rs_st)
                        # Акт — только у головы стопа: продолжение после
                        # суточного реза ссылается на неё через continued_from
                        if (_rs_incident is None
                                and getattr(seg, "stop_kind", None) == "EMERGENCY"
                                and _is_stop_head(enum_periods, _rs_st)):
                            _rs_ctx = await _load_incident_context(
                                self.router_sn, self.equip_type, self.panel_id,
                                _rs_st, ts_to_utc, self.cfg,
                            )
                            _rs_ep, _rs_fp, _rs_tlf = (
                                _rs_ctx if _rs_ctx
                                else (tl_enum, fault_periods, tl_faults)
                            )
                            _rs_incident = _clf.build_stop_incident(
                                _rs_ep, _rs_fp,
                                _rs_st, seg_t_end_rs, self.cfg,
                                stop_kind="EMERGENCY",
                                stabilization_sec=_stab_sec(self.cfg),
                                detail_events=_act_detail_events(self.cfg),
                                timeline_faults=_rs_tlf,
                            )
                            # Авария короче лага: акта на открытой строке не
                            # было, значит разбор заказывается отсюда
                            if _rs_incident is not None:
                                self._act_done_for = _rs_st
                                self._order_incident_analysis(_rs_incident, _rs_st)
                                # segment_id ещё не известен — строка
                                # вставляется ниже; разбор найдёт сегмент по ts
                        # Лента есть у любого стоп-сегмента, в том числе
                        # аварийного: акт говорит что произошло, лента — что
                        # было дальше, пока авария не снята
                        _rs_chronology = _chrono(
                            tl_enum, tl_faults,
                            _rs_st, seg_t_end_rs, self.cfg,
                        )
                    except Exception:
                        logger.warning("OnlineEngine[%s]: лента стоп-сегмента не построена (RS-change)",
                                       self.key, exc_info=True)
                _rs_db_id = await online_db.insert_closed_segment({
                    "router_sn":          self.router_sn,
                    "equip_type":         self.equip_type,
                    "panel_id":           self.panel_id,
                    "t_start":            _tz_utc(datetime.fromisoformat(seg.t_start)),
                    "t_end":              seg_t_end_rs,
                    "run_state":          seg.run_state,
                    "cause_close":        seg.cause_close or "RUN_STATE_CHANGE",
                    "split_reason":       None,
                    "continued_from":     _rs_link,
                    "coking_risk_json":   coking_risk.to_dict(),
                    "analytics_version":  ANALYTICS_VERSION,
                    "characteristics_json": _rs_seg_dict,
                    "report_md":          report_md_rs,
                    "report_summary_md":  summary_md_rs,
                    "report_summary_json": summary_parts_rs,
                    "incident_json":      _rs_incident,
                    "chronology_json":    _rs_chronology,
                    **_slice_gate_state(
                        _rs_gate_state,
                        _tz_utc(datetime.fromisoformat(seg.t_start)), seg_t_end_rs,
                    ),
                })
                # ← await выше = прогресс обновляется на каждой смене RUN_STATE
                if _rs_no_data:
                    logger.info(
                        "OnlineEngine[%s]: сегмент %s пустой (нет связи весь период) — corpus-анализ пропущен",
                        self.key, _rs_db_id,
                    )
                else:
                    _enqueue_segment(_rs_db_id)

                # Связь эпизод→сегмент: ссылка на открытую строку обнуляется
                # при её удалении, поэтому перецеливаем на закрытую (v4.9.75)
                try:
                    if _rs_db_id:
                        await online_db.retarget_episode_segments(
                            self.router_sn, self.equip_type, self.panel_id,
                            _rs_db_id,
                            _tz_utc(datetime.fromisoformat(seg.t_start)), seg_t_end_rs,
                        )
                except Exception:
                    logger.warning("OnlineEngine[%s]: не удалось привязать эпизоды к сегменту %s",
                                   self.key, _rs_db_id, exc_info=True)

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
                self._transition_ctx = _push_transition(self._transition_ctx, seg)
                carry_continued_from = None   # только первый сегмент несёт ссылку
                self.continued_from_id = None
                self.forward_fill_memory = None
                self._open_history_cache = None
                self._open_history_cache_ts = None

        # Открытый сегмент = последний в списке
        open_seg = segments[-1]

        # Начало запуска для открытого сегмента:
        # если были смены RS — открытый сегмент начинается свежо; иначе — продолжает цепочку
        # Ссылка на предка: только без смен в окне и в том же режиме — переход
        # внутри обрыва связи на суточной границе начинает новую цепочку (v5.1.6)
        open_continued_from = (
            None if closed_segs
            else _chain_link(0, self.continued_from_id, self._prev_seg_hint, open_seg)
        )
        if closed_segs:
            _open_run_origin = _tz_utc(datetime.fromisoformat(open_seg.t_start))
        elif open_continued_from is not None or self.continued_from_id is None:
            _open_run_origin = chain_origin_ts or _tz_utc(self.cursor_ts)
        else:
            _open_run_origin = _tz_utc(datetime.fromisoformat(open_seg.t_start))

        # Эпизоды тревог — ДО обогащения: счётчики уже включат текущий эпизод
        try:
            _, _raw_curr_dets = _extract_open_segment_data(open_seg)
            await self._process_episodes(_raw_curr_dets, t_to, gaps,
                                         cleared_at=_panel_cleared_at(segments),
                                         gaps_from=ts_from_utc)
        except Exception:
            logger.warning("OnlineEngine[%s]: ошибка обработки эпизодов тревог",
                           self.key, exc_info=True)

        # Разовые события перехода — точечный эпизод до счётчиков (v5.1.10)
        try:
            await self._seed_open_events(open_seg, t_to)
        except Exception:
            logger.warning("OnlineEngine[%s]: посев событий", self.key, exc_info=True)

        # Обогатить детекции счётчиками ДО _extract_open_segment_data / to_markdown
        _t0 = _time.perf_counter()
        await _enrich_open_seg_detections(
            open_seg, self.router_sn, self.equip_type, self.panel_id, self.cfg,
            run_origin_ts=_open_run_origin,
        )
        logger.debug(
            "TIMING[%s]: enrich за %.3fs",
            self.key, _time.perf_counter() - _t0,
        )

        current_values, _curr_dets = _extract_open_segment_data(open_seg)
        coking_risk = _extract_coking_risk_from_segments(segments)

        # Диффузия живых тревог: вычисляем diff до upsert; segment_id подставим после
        # Риск нагара — состояние, не тревога: в журнал и живые тревоги не идёт
        # (иначе «тревога» мигала бы с каждым подсегментом без недогруза)
        active_detections, _alert_events = _diff_alerts(
            self._active_alerts,
            [d for d in _curr_dets if d.get("scenario") not in STATE_SCENARIOS],
            ts=_tz_utc(t_to), segment_id=None,
        )
        self._active_alerts = {_alert_key(d): d for d in active_detections}

        # continued_from (open_continued_from выше): если после DAILY_BOUNDARY не
        # было смен RUN_STATE, открытый сегмент — продолжение pre-boundary сегмента

        _t0 = _time.perf_counter()
        try:
            from analytics.serializer import to_markdown as _to_md
            open_report_md = _to_md(
                [open_seg], self.router_sn, self.equip_type, self.panel_id,
                _tz_utc(self.cursor_ts), _tz_utc(t_to),
                ANALYTICS_VERSION, tz=self.tz, prev_seg=self._prev_seg_hint,
                fault_ref=self._fault_ref,
                inherited_run_state_sec=_rs_carry(self.inherited_run_state_sec, open_seg.run_state),
            )
        except Exception:
            logger.exception("OnlineEngine[%s]: не удалось построить отчёт открытого сегмента", self.key)
            open_report_md = None

        open_summary_md, open_summary_parts = await self._build_summary_md_for(
            [open_seg], self.cursor_ts, t_to
        )
        logger.debug(
            "TIMING[%s]: to_markdown за %.3fs",
            self.key, _time.perf_counter() - _t0,
        )

        # Артефакты ОТКРЫТОГО стоп-сегмента.
        #   Лента растёт каждый цикл — события видны сразу, живьём.
        #   Акт собирается ОДИН раз, по истечении лага стабилизации: раньше
        #   собирать нечего (сопутствующие ещё не пришли), и полуготовый
        #   документ хуже его отсутствия. Уже собранный не трогаем — в БД
        #   он под COALESCE, так что None его не сотрёт.
        _open_incident = None
        _open_chronology = None
        if open_seg.run_state == 0:
            try:
                from analytics import classifier as _clf
                from analytics.reconstructor import (
                    build_segment_chronology as _chrono,
                )
                _op_st = _tz_utc(datetime.fromisoformat(open_seg.t_start))
                _lag_done = ts_to_utc >= _op_st + timedelta(
                    seconds=_stab_sec(self.cfg)
                )
                if (getattr(open_seg, "stop_kind", None) == "EMERGENCY"
                        and _lag_done
                        and self._act_done_for != _op_st
                        and _is_stop_head(enum_periods, _op_st)):
                    _op_ctx = await _load_incident_context(
                        self.router_sn, self.equip_type, self.panel_id,
                        _op_st, ts_to_utc, self.cfg,
                    )
                    _op_ep, _op_fp, _op_tlf = (
                        _op_ctx if _op_ctx
                        else (tl_enum, fault_periods, tl_faults)
                    )
                    _open_incident = _clf.build_stop_incident(
                        _op_ep, _op_fp, _op_st, None, self.cfg,
                        stop_kind="EMERGENCY",
                        stabilization_sec=_stab_sec(self.cfg),
                        detail_events=_act_detail_events(self.cfg),
                        timeline_faults=_op_tlf,
                    )
                    if _open_incident is not None:
                        self._act_done_for = _op_st
                        self._order_incident_analysis(_open_incident, _op_st)
                _open_chronology = _chrono(
                    tl_enum, tl_faults, _op_st, ts_to_utc, self.cfg
                )
                # Передаём ленту, только если в ней что-то изменилось: иначе
                # строка переписывается каждые тридцать секунд впустую. Ключ
                # включает начало сегмента — у нового сегмента подпись своя.
                _sig = (
                    open_seg.t_start,
                    len((_open_chronology or {}).get("chronology") or []),
                )
                if _open_chronology is not None and _sig == self._open_chrono_sig:
                    _open_chronology = None
                elif _open_chronology is not None:
                    self._open_chrono_sig = _sig
            except Exception:
                logger.warning(
                    "OnlineEngine[%s]: артефакты открытого стоп-сегмента не построены",
                    self.key, exc_info=True,
                )

        _t0 = _time.perf_counter()
        _open_seg_id = await online_db.upsert_open_segment({
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
                    open_seg.run_state: (
                        _rs_carry(self.inherited_run_state_sec, open_seg.run_state)
                        .get(open_seg.run_state, 0.0) + open_seg.duration_sec
                    ),
                },
            },
            "report_md":              open_report_md,
            "report_summary_md":      open_summary_md,
            "report_summary_json":    open_summary_parts,
            "continued_from":         open_continued_from,
            "incident_json":          _open_incident,
            "chronology_json":        _open_chronology,
        })
        logger.debug(
            "TIMING[%s]: upsert за %.3fs | ИТОГО цикл %.2fs",
            self.key, _time.perf_counter() - _t0,
            _time.perf_counter() - _t_cycle_start,
        )

        self._last_open_seg_id = _open_seg_id

        # Хвост записей гейта удалённой строки — в новую открытую (см. _close_window)
        if _rs_gate_state is not None and _open_seg_id:
            _rs_tail = _slice_gate_state(_rs_gate_state, self.cursor_ts)
            if _rs_gate_state.get("suppressed_hash"):
                _rs_tail["gate_suppressed_hash"] = _rs_gate_state["suppressed_hash"]
            try:
                await online_db.append_segment_gate_state(_open_seg_id, _rs_tail)
            except Exception:
                logger.warning("OnlineEngine[%s]: хвост записей гейта не восстановлен",
                               self.key, exc_info=True)

        # Отложенная проверка снятия гейтом новых эпизодов (см. _process_episodes)
        if self._supp_recheck:
            _sc, _dets = self._supp_recheck
            self._supp_recheck = None
            await self._inherit_gate_suppression(_sc, _dets)

        # Записать события жизненного цикла тревог (segment_id теперь известен)
        if _alert_events:
            for ev in _alert_events:
                ev["segment_id"] = _open_seg_id
            try:
                await online_db.insert_alert_events(
                    self.router_sn, self.equip_type, self.panel_id, _alert_events
                )
            except Exception:
                logger.warning(
                    "OnlineEngine[%s]: не удалось записать alert_journal", self.key
                )
            for ev in _alert_events:
                logger.info(
                    "OnlineEngine[%s]: тревога %s — %s (severity=%s)",
                    self.key, ev["scenario"], ev["event_type"], ev.get("severity"),
                )

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
