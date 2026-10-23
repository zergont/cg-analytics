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

    v5.1.21 (П7): эпизод аналитики с участками (values.live в open_values_json)
    закрывается точно по концу нарушения — хвоста дебаунса у него нет, и любое
    пересечение с окном — настоящее: перенос 60 с после 09:00 остаётся в
    сводке нового дня (порог ≈120 с его выбрасывал).
    """
    out = []
    for e in eps:
        t_open, t_close = e.get("t_open"), e.get("t_close")
        if t_open is not None and _tz_utc(t_open) >= t_from:
            out.append(e)
        elif t_close is None or _tz_utc(t_close) >= t_to:
            out.append(e)
        elif _episode_has_live(e) and _tz_utc(t_close) > t_from:
            out.append(e)
        elif (_tz_utc(t_close) - t_from).total_seconds() > tail_sec:
            out.append(e)
    return out


def _episode_open_values(ep: dict) -> dict:
    """open_values_json строки эпизода — словарь (asyncpg отдаёт JSON строкой)."""
    ov = ep.get("open_values_json")
    if isinstance(ov, str):
        try:
            ov = json.loads(ov)
        except (TypeError, ValueError):
            return {}
    return ov if isinstance(ov, dict) else {}


def _episode_has_live(ep: dict) -> bool:
    """Эпизод аналитики с участками (v5.1.21): в open_values_json есть блок live."""
    return (ep.get("scenario") != "CONTROLLER_FAULT"
            and isinstance(_episode_open_values(ep).get("live"), dict))


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


def _analytics_cleared_at(segments: list,
                          episodes: dict[str, dict] | None = None) -> dict[str, datetime]:
    """Где кончилось нарушение аналитики: ключ → конец кластера (v5.1.21, П5).

    Рядом с _panel_cleared_at и по той же причине: движок отстаёт на цикл, и
    первый промах вставал на горизонт обработки. Кластеры — из values.live
    детекций всех сегментов окна; у сегмента, закрытого сменой режима 3 → 4/0,
    идущий кластер кончается краем его рабочей части (до сброса оборотов) —
    это и есть конец. Без episodes — самый поздний конец по ключу. С episodes —
    только для точных эпизодов памяти: цепочка кластеров (следующий начинается
    не дальше N видимого времени от конца предыдущего) от кластера самого
    эпизода в этом окне — первого, кончившегося не раньше t_open и начавшегося
    не позже until (у эпизода, открытого в окне, — кластер с началом в t_open).
    v5.1.21, ревью (engine-1, shadow-1/2): не от until памяти — until мог
    остаться горизонтом синтетического хвоста (пауза пакетов без data_gaps:
    воздействие включало паузу, кластер после неё склеивался с прежним) или
    устареть за простой сервиса (JSON — последний цикл до остановки: хвост
    кластера терялся). Кластера эпизода в окне нет (мост после реза, рестарт
    без него) — цепочка от until, как раньше. Кластер после простоя сервиса,
    дальше N, — уже другой эпизод: его засеет закрытие сегмента, а этот
    закроется по концу своего кластера, без времени простоя.
    Сбой — пустая карта (конец берётся из until памяти)."""
    try:
        from analytics.live import LIVE_RECENT_SEC, live_block, subs_gaps, visible_sec
        subs = [sub for seg in segments or [] for sub in getattr(seg, "subsegments", None) or []]
        ends: dict[str, dict[datetime, datetime]] = {}
        for sub in subs:
            for d in getattr(sub, "detections", None) or []:
                lv = live_block(d)
                if lv is None:
                    continue
                key = _alert_key(d if isinstance(d, dict) else d.to_dict())
                m = ends.setdefault(key, {})
                for c in lv.get("clusters") or []:
                    s, e = _as_utc(c.get("start")), _as_utc(c.get("end"))
                    if s is not None and e is not None and (s not in m or e > m[s]):
                        m[s] = e
        gl = subs_gaps(subs)
        out: dict[str, datetime] = {}
        for key, m in ends.items():
            if not m:
                continue
            if episodes is None:
                out[key] = max(m.values())
                continue
            ep = episodes.get(key) or {}
            t_open = ep.get("t_open")
            if not ep.get("exact") or t_open is None:
                continue
            u0 = ep.get("until")
            items = sorted(m.items())
            _lim = max(u0, t_open) if u0 is not None else None
            anchor = next((i for i, (s, e) in enumerate(items)
                           if e >= t_open and (_lim is None or s <= _lim)), None)
            if anchor is not None:
                cur = best = items[anchor][1]
                rest = items[anchor + 1:]
            else:
                cur, best = u0 or t_open, None
                rest = [x for x in items if x[0] >= t_open]
            for s, e in rest:
                if s > cur and visible_sec(cur, s, gl) > LIVE_RECENT_SEC:
                    break
                cur = max(cur, e)
                best = cur
            if best is not None:
                out[key] = best
        return out
    except Exception:
        logger.warning("Карта концов нарушений аналитики не собрана", exc_info=True)
        return {}


def _exact_end(ep: dict, ca: datetime | None) -> datetime | None:
    """Конец нарушения точного эпизода памяти (v5.1.21, ревью engine-1/shadow-2):
    по карте концов окна (ca — _analytics_cleared_at, цепочка от кластера самого
    эпизода), until памяти — запасной (мост после реза, рестарт без кластера в
    окне). until — горизонт детекции без live (смешанный ключ, until_kind
    «plain»): она была видна до него, берётся позднейшее, как раньше. Карта
    раньше t_open — прежний период ключа, не в счёт."""
    u = ep.get("until")
    t_open = ep.get("t_open")
    if ca is not None and t_open is not None and ca < t_open:
        ca = None
    if ca is None:
        return u
    if u is not None and ep.get("until_kind") == "plain":
        return max(u, ca)
    return ca


def _until_kind(li: dict) -> str:
    """Чем является until кластера цикла (v5.1.21, ревью engine-1): «plain» —
    горизонт из-за детекции без live (смешанный ключ), «horizon» — горизонт
    идущего кластера, «end» — фактический конец кластера."""
    if li.get("mixed"):
        return "plain"
    return "horizon" if li.get("ongoing") else "end"


def _drop_debounce_tails(eps: list[dict], segments: list,
                         live_episodes: dict[str, dict],
                         live_keys: set[str] | None = None) -> list[dict]:
    """Убрать из сводки ЗАКРЫВАЕМОГО сегмента эпизоды, снятые до него.

    Эпизод закрывается после нескольких чистых циклов (дебаунс), и в цикле, где
    одним пакетом пришли снятие тревоги и смена режима, он ещё открыт — сводка
    следующего короткого сегмента получала «живую на конец окна» тревогу
    (06.10: «АВАРИЯ» в 9-секундном прогреве после снятой кнопки).
    Открытый эпизод без детекции в данных самого сегмента — хвост дебаунса:
    у панели это точно (детекция строится по периоду неисправности), у
    аналитики — только если движок уже видит промах (miss > 0).

    v5.1.21 (П7): точный эпизод аналитики (exact в памяти), не живой на конец
    сегмента (нет в live_keys), закроется по концу нарушения — в сводку идёт
    копией с t_close = конец по карте кластеров сегмента (цепочка от кластера
    эпизода), без неё — until памяти (_exact_end, ревью v5.1.21: горизонт
    синтетического хвоста паузы — не конец), как его закроет движок
    (_process_episodes). Мост держит его открытым до
    N после реза, промах в памяти ещё 0 — без этого сводка показала бы его
    висящим до конца окна. Кончился до сегмента — хвост, в сводку не идёт.
    БД не трогается."""
    det_keys = {
        _alert_key(d.to_dict())
        for seg in segments or []
        for sub in getattr(seg, "subsegments", None) or []
        for d in sub.detections
    }
    live_keys = live_keys or set()
    w0 = _as_utc(getattr(segments[0], "t_start", None)) if segments else None
    _ca: dict[str, datetime] | None = None
    out = []
    for ep in eps:
        if ep.get("t_close") is None:
            key = _episode_key(ep)
            mem = live_episodes.get(key) or {}
            if (mem.get("exact") and key not in live_keys
                    and ep.get("scenario") != "CONTROLLER_FAULT"
                    and (mem.get("id") is None or ep.get("id") is None or mem["id"] == ep["id"])):
                if _ca is None:
                    _ca = _analytics_cleared_at(segments, live_episodes)
                _tc = _exact_end(mem, _ca.get(key))
                if _tc is not None:
                    _to = _as_utc(ep.get("t_open"))
                    if _to is not None and _tc < _to:
                        _tc = _to
                    if w0 is not None and _tc <= w0 and (_to is None or _to < w0):
                        continue
                    out.append(dict(ep, t_close=_tc))
                    continue
            if key not in det_keys:
                if ep.get("scenario") == "CONTROLLER_FAULT" or mem.get("miss", 0) > 0:
                    continue
        out.append(ep)
    return out


def _bridge_only(ep: dict, live_episodes: dict[str, dict] | None, t_from: datetime) -> bool:
    """Открытый точный эпизод, держащийся мостом: его нарушение (until памяти)
    кончилось не позже начала окна — в окне его нет (v5.1.21)."""
    if ep.get("t_close") is not None or ep.get("scenario") == "CONTROLLER_FAULT":
        return False
    mem = (live_episodes or {}).get(_episode_key(ep)) or {}
    u = mem.get("until")
    return (bool(mem.get("exact")) and u is not None and _tz_utc(u) <= t_from
            and (mem.get("id") is None or ep.get("id") is None or mem["id"] == ep["id"]))


def _ban_row(open_row: dict | None, seg, h: str) -> dict:
    """Строка для проверки запретов снятия на конце закрываемого сегмента: начало
    и код 40012 на начало — снятой открытой строки (свежесть «Общей аварии»),
    вид стопа — самого сегмента."""
    chars = (open_row or {}).get("characteristics_json")
    if isinstance(chars, str):
        try:
            chars = json.loads(chars)
        except Exception:
            chars = None
    chars = dict(chars) if isinstance(chars, dict) else {}
    chars["stop_kind"] = getattr(seg, "stop_kind", None)
    # Профиль пуска (v5.1.23, п.4): запрет снятия на неустойчивом пуске — по
    # профилю самого закрываемого сегмента (детекции тоже его), не строки
    if hasattr(seg, "start_profile"):
        _sp = getattr(seg, "start_profile", None)
        chars["start_profile"] = _sp if isinstance(_sp, dict) else None
        # Продолжение «Работы» через суточный рез — профиль предка (ревью R1)
        _spp = getattr(seg, "start_profile_prev", None)
        chars["start_profile_prev"] = _spp if isinstance(_spp, dict) else None
    return {"gate_suppressed_hash": h, "t_start": (open_row or {}).get("t_start"),
            "characteristics_json": chars}


def _stop_between(src_ts: datetime | None, seg, closed: list | None) -> bool:
    """Стоп-сегмент между решением гейта (src_ts) и сегментом seg среди
    закрываемых этим циклом (v5.1.22, ревью gate-3; то же правило, что у
    _gate_carry): стоп раньше seg, кончившийся после решения, или сам seg — стоп,
    начавшийся после решения. Решение без времени — любой стоп."""
    try:
        s0 = _as_utc(getattr(seg, "t_start", None))
    except Exception:
        s0 = None
    for s in closed or []:
        if getattr(s, "run_state", None) != 0:
            continue
        a, b = _as_utc(getattr(s, "t_start", None)), _as_utc(getattr(s, "t_end", None))
        if s is seg:
            if src_ts is None or (a is not None and a > src_ts):
                return True
            continue
        if src_ts is None:
            return True
        if b is not None and b > src_ts and (s0 is None or b <= s0):
            return True
    return False


def _gate_suppressed_scenarios(gate_state: dict | None, seg,
                               open_row: dict | None = None,
                               closed: list | None = None) -> set[str]:
    """Сценарии новых тревог закрываемого сегмента, которые сводка помечает
    снятыми (синтетика _live_new_episodes: живой эпизод по ним заведётся после
    закрытия).

    Хеш снятия — из записей гейта открытой строки, которую цикл закрытия удаляет.
    v5.1.22 (P1-1 п. 2, 4): только от своего решения этой строки (разбор моделью
    или повтор — от перенесённого снятия новые тревоги не наследуют) и тем же
    предикатом, что статус: состав на конец совпал и запретов нет (сигнал
    панели, свежая «Общая авария», аварийный стоп, несимметрия, паспорт).
    closed — все сегменты, закрываемые этим циклом (ревью gate-3): при догонке
    [Работа (решение) → Стоп → Работа] стоп между решением и сегментом снятие
    не пропускает, как и перенос (_gate_carry).
    """
    h = (gate_state or {}).get("suppressed_hash")
    if not h:
        return set()
    from online.status_assembler import (
        GATE_EXCLUDED_SCENARIOS, UNCANCELLABLE_SCENARIOS, gate_decision_source,
        passport_hard_seen, suppression_active,
    )
    _src = gate_decision_source((gate_state or {}).get("gate_log"), h)
    if _src is None:
        return set()
    if closed and _stop_between(_as_utc(_src.get("ts")), seg, closed):
        return set()
    try:
        _, dets = _extract_open_segment_data(seg)
    except Exception:
        logger.warning("Не удалось снять активные тревоги сегмента", exc_info=True)
        return set()
    if not suppression_active(_ban_row(open_row, seg, h), dets):
        return set()
    # Режимные замечания гейт не снимает (решение 12.10)
    return ({d.get("scenario") for d in dets
             if d.get("scenario") and d.get("scenario") != "CONTROLLER_FAULT"
             and not passport_hard_seen(d)}
            - UNCANCELLABLE_SCENARIOS - GATE_EXCLUDED_SCENARIOS)


def _gate_own_src(gate_state: dict | None) -> dict | None:
    """Источник своего снятия снятой строки {ts, segment_id, kind: inherited} —
    для подписи синтетических эпизодов сводки (v5.1.22)."""
    try:
        from online.status_assembler import gate_decision_source
        src = gate_decision_source((gate_state or {}).get("gate_log"),
                                   (gate_state or {}).get("suppressed_hash"))
    except Exception:
        return None
    if not src:
        return None
    return {"ts": src.get("ts"), "segment_id": src.get("segment_id"), "kind": "inherited"}


def _gate_carry(gate_state: dict | None, closed: list[tuple], episodes: dict[str, dict],
                live_end: set[str], live_open: set[str] | None, open_run_state: int | None,
                boundary: datetime, dets_end: list[dict] | None = None,
                supp_sc: set[str] | None = None) -> dict | None:
    """Хеш снятия для новой открытой строки при закрытии (v5.1.22, P1-1 п. 3).

    closed — закрытые этим циклом сегменты [(seg, db_id, t_start, t_end)];
    episodes — память эпизодов движка; live_end — ключи тревог на конце
    последнего закрытого, live_open — в остающемся открытом (путь смены режима:
    пересечение, В5); boundary — начало новой строки.

    - Своё решение принято уже в новой строке (хвост записей после границы) —
      хеш остаётся как есть: {"hash"}.
    - Иначе переходит снятие только снятых эпизодов, которые ещё идут на
      границе (id из записей гейта с этим хешем есть в памяти, ключ жив на
      конце), и только если между решением и границей нет стоп-сегмента —
      проверяются ВСЕ закрываемые (догонка после рестарта даёт [Работа → Стоп →
      Работа]) — и новая строка не стоп. Запись carry с исходным разбором и
      номером его закрытого сегмента: {"hash", "record", "ids", "segment_id"}.
    - Иначе None: хеш не переходит, новые эпизоды разбирает гейт.
    Журнал до v5.1.22 без списков эпизодов — по составу на конце (dets_end).
    supp_sc — сценарии новых тревог, которые сводка закрытого сегмента пометила
    снятыми (синтетика, _gate_suppressed_scenarios): в записи carry ключом
    synthetic — их живой эпизод, заведённый после закрытия, получает снятие
    переносом; прочие тревоги, появившиеся до границы, — нет (ревью gate-4).
    """
    from online.status_assembler import (
        GATE_CARRY_KINDS, GATE_RULES_VERSION, gate_decision_source, gate_record_episodes,
        is_analytics_suppressed,
    )
    h = (gate_state or {}).get("suppressed_hash")
    if not h:
        return None
    log = (gate_state or {}).get("gate_log") or []
    src = gate_decision_source(log, h) or gate_decision_source(log, h, GATE_CARRY_KINDS)
    if src is None:
        return None
    b = _tz_utc(boundary)
    src_ts = _as_utc(src.get("ts"))
    if src_ts is not None and src_ts >= b:
        return {"hash": h, "record": None, "ids": [], "segment_id": None}
    if open_run_state == 0:
        return None
    for seg, _id, _t0, t1 in closed or []:
        if getattr(seg, "run_state", None) == 0 and (src_ts is None or _tz_utc(t1) > src_ts):
            return None
    ids = gate_record_episodes(log, h)
    if ids is None:
        # Журнал до v5.1.22: снятые — эпизоды состава на конце, если он тот же
        if not is_analytics_suppressed({"gate_suppressed_hash": h}, dets_end or []):
            return None
        _sc = {d.get("scenario") for d in dets_end or []
               if isinstance(d, dict) and d.get("scenario") != "CONTROLLER_FAULT"}
        ids = {m.get("id") for k, m in (episodes or {}).items() if k in _sc}
    ongoing = sorted(m["id"] for k, m in (episodes or {}).items()
                     if m.get("id") in ids and k in (live_end or set())
                     and (live_open is None or k in live_open))
    if not ongoing:
        return None
    seg_id = src.get("segment_id")
    if seg_id is None and src_ts is not None:
        seg_id = next((_id for _s, _id, t0, t1 in closed or []
                       if _id is not None and _tz_utc(t0) <= src_ts < _tz_utc(t1)), None)
    source = {"ts": src.get("ts"), "segment_id": seg_id}
    record = {
        "ts": b.isoformat(), "kind": "carry",
        "fault_hash": src.get("fault_hash"), "suppressed_hash": h,
        "decision": "cancel", "decision_applied": True,
        "reason": "снятый эпизод продолжается через границу сегмента — снятие перенесено "
                  "по исходному разбору",
        "rules": GATE_RULES_VERSION,
        "provider": "carry", "model": None, "tokens_in": 0, "tokens_out": 0,
        "source": source, "episodes": ongoing,
        # Тревоги закрытого сегмента, снятые в его сводке без живого эпизода
        # (ревью gate-4): только их продолжение наследует перенос
        "synthetic": sorted(supp_sc or []),
    }
    return {"hash": h, "record": record, "ids": ongoing, "segment_id": seg_id}


def _carry_scope(gate_log: list | None, h: str | None) -> tuple[set[int], set[str], dict[str, datetime]]:
    """Что защищает перенесённое снятие строки (v5.1.22, ревью gate-4): id
    эпизодов из записей carry / late с этим хешем; сценарии тревог, снятых в
    сводке закрытого сегмента без живого эпизода (synthetic у carry); у позднего
    вердикта — сценарии записи с моментом разбора (снимал начавшиеся не позже).
    Тревога, которую закрытая карточка не снимала, сюда не входит."""
    from online.status_assembler import gate_entry_kind
    ids: set[int] = set()
    synth: set[str] = set()
    late: dict[str, datetime] = {}
    for e in gate_log or []:
        if not (isinstance(e, dict) and e.get("decision") == "cancel" and e.get("decision_applied")
                and e.get("suppressed_hash") == h):
            continue
        kind = gate_entry_kind(e)
        if kind not in ("carry", "late"):
            continue
        for i in e.get("episodes") or []:
            try:
                ids.add(int(i))
            except (TypeError, ValueError):
                pass
        if kind == "carry":
            synth |= {s for s in e.get("synthetic") or [] if isinstance(s, str)}
        else:
            _t = _as_utc(((e.get("source") or {}) if isinstance(e.get("source"), dict) else {}).get("ts")
                         or e.get("ts"))
            for a in e.get("alarms") or []:
                sc = a.get("scenario") if isinstance(a, dict) else None
                if sc and _t is not None and (sc not in late or _t > late[sc]):
                    late[sc] = _t
    return ids, synth, late


def _live_new_episodes(segments: list, eps: list[dict], live_keys: set[str],
                       t_end: datetime, suppressed_sc: set[str] | None = None,
                       supp_src: dict | None = None) -> list[dict]:
    """Тревоги, появившиеся в закрываемом сегменте и продолжающиеся в открытом.

    Живой эпизод по ним заведётся только после закрытия (_process_episodes идёт
    после сводок), эфемерный не сеется (тревога переживает рез) — в сводку
    закрываемого сегмента они не попадали, хотя детекция лежит в его данных.
    Для сводки — синтетический открытый эпизод с фазой «появилось»; снятый
    гейтом состав (suppressed_sc) помечается снятым, как пометит живой эпизод
    _process_episodes.

    v5.1.21 (П7): начало синтетики аналитики с участками — начало живого
    кластера ключа (последнего живого на конец сегмента, со взглядом назад —
    alive_detections, как живой состав движка), а не t_detected первого
    подсегмента с детекцией: иначе «появилось» в 09:00 при нарушении с 08:57.
    Сбой — по t_detected, как раньше.

    v5.1.22: supp_src — источник снятия (своё решение строки) для подписи
    «снято ИИ (разбор ЧЧ:ММ)».
    """
    if not live_keys:
        return []
    # Закрытый эпизод того же ключа — прежняя вспышка, текущую он не покрывает.
    # Кроме того же онсета (v5.1.1): это та же тревога, закрытая промахом, —
    # синтетика её заменяет (вызывающий убирает строку, _drop_same_onset; у
    # аналитики с v5.1.21 — склейка строк одного онсета в сводке)
    have = {_episode_key(e) for e in eps if e.get("t_close") is None}
    closed_onsets = {(_episode_key(e), _tz_utc(e["t_open"])): e for e in eps
                     if e.get("t_close") is not None and e.get("t_open")}
    live_on: dict[str, datetime] = {}
    try:
        from analytics.live import alive_detections
        _last = (segments or [None])[-1]
        _subs = getattr(_last, "subsegments", None) or []
        if _subs:
            live_on = {k: li["onset"] for k, li in
                       _live_by_key(alive_detections(_subs), _tz_utc(t_end)).items()}
    except Exception:
        logger.warning("Начало живых кластеров для сводки не найдено — по t_detected",
                       exc_info=True)
        live_on = {}
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
                if not panel and key in live_on:
                    t_open = live_on[key]
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
                    "gate_suppressed_src": (supp_src if not panel
                                            and dd.get("scenario") in (suppressed_sc or ())
                                            else None),
                    "open_values_json": {"fault_name": v.get("fault_name")} if panel else None,
                }
    return list(out.values())


def _drop_same_onset(eps: list[dict], synthetic: list[dict]) -> list[dict]:
    """Убрать закрытые строки, которые синтетика того же онсета заменяет.

    v5.1.21 (П7): только панель — тот же фронт неисправности. У аналитики с
    участками тот же онсет — тот же кластер (засеянный при закрытии или
    закрытый движком): строки склеивает сводка (_by_onset в
    build_summary_parts), а убранная строка унесла бы его воздействие."""
    onsets = {(_episode_key(s), _tz_utc(s["t_open"])) for s in synthetic
              if s.get("t_open") and s.get("source") == "panel"}
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
                    else:
                        # «В очереди» видно сразу, а не с начала работы (v5.1.19)
                        from corpus.db import mark_queued
                        await mark_queued(seg_id)
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
    # v5.1.22 (P1-4): границы и отметка обрыва аварией с лентой отсчётов — акт
    # стопа, открытого до рестарта, берёт ленту сегмента падения отсюда, без чтения
    trip = chars.get("closed_by_trip") if isinstance(chars, dict) else None
    # v5.1.23 (п.3): профиль пуска — подсказка повторяет поле Segment: после
    # рестарта предок окна — она, и getattr(…, "start_profile") не расходится.
    # Его читает окно после суточного реза посреди «Работы» (_start_carry,
    # ревью R1): запрет снятия эпизода, начавшегося в окне пуска до реза
    sprof = chars.get("start_profile") if isinstance(chars, dict) else None

    def _iso(v):
        return v.isoformat() if hasattr(v, "isoformat") else v
    return SimpleNamespace(run_state=db_row.get("run_state"),
                           run_state_label=label, stop_kind=kind,
                           t_start=_iso(db_row.get("t_start")), t_end=_iso(db_row.get("t_end")),
                           closed_by_trip=trip if isinstance(trip, dict) else None,
                           start_profile=sprof if isinstance(sprof, dict) else None)


def _start_carry(prev_hint) -> dict | None:
    """Профиль пуска предка окна для «Работы», идущей через суточный рез (ревью
    v5.1.23, R1): предок — «Работа» (Segment или подсказка _make_seg_hint) со
    своим профилем; иначе None. Сегментатор сам проверит, что «Работа» на
    голове окна — продолжение (segmenter.start_profile_carry)."""
    if prev_hint is None or getattr(prev_hint, "run_state", None) != 3:
        return None
    sp = getattr(prev_hint, "start_profile", None)
    return sp if isinstance(sp, dict) else None


def _mark_episode_onsets(dets: list[dict] | None, episodes: dict | None,
                         since: datetime | None = None) -> None:
    """Начало эпизода у просадки и заброса оборотов (ревью v5.1.23, R1).

    Эпизод, продолжившийся мостом через суточный рез, начался в прежней
    строке, а кластер нового окна — после реза. Запрет снятия на неустойчивом
    пуске (status_assembler.start_unstable_scenarios) сравнивает с окном пуска
    начало эпизода: оно — в поле onset детекции (его первым берёт
    first_cluster_start). Только если эпизод памяти движка начался раньше
    кластера (мост); иначе поля нет. Без запросов — память движка.

    since — не раньше этого момента (прокрутка «Работы», идущей через рез,
    _carry_onset_floor): эпизод, продолжившийся с прежней «Работы» через стоп
    и быстрый повторный пуск, начался до этого пуска — его начало окну пуска
    не родня, остаётся кластер нового окна (перепроверка R1)."""
    from analytics.labels import START_SCENARIOS
    for d in dets or []:
        if not isinstance(d, dict) or d.get("scenario") not in START_SCENARIOS:
            continue
        ep = (episodes or {}).get(_alert_key(d))
        t0 = _as_utc(ep.get("t_open")) if isinstance(ep, dict) else None
        lv = (d.get("values") or {}).get("live")
        on = _as_utc(lv.get("onset")) if isinstance(lv, dict) else None
        if t0 is not None and on is not None and t0 < on and (since is None or t0 >= since):
            d["onset"] = t0.isoformat()


def _carry_onset_floor(carried) -> datetime | None:
    """Нижняя граница начала эпизода для _mark_episode_onsets: прокрутка пуска
    «Работы», идущей через суточный рез (Segment.start_profile_prev), минус
    стык кадра. None — открытая «Работа» профиль предка не несёт (свой пуск в
    окне, стоп, не «Работа»): начало эпизода не отмечается — запрет снятия по
    кластеру окна, как без моста (перепроверка R1: быстрый повторный пуск после
    стопа продолжал эпизод прежней «Работы», и запрет нового пуска терялся)."""
    if not isinstance(carried, dict):
        return None
    c = _as_utc(carried.get("crank_at"))
    return c - timedelta(seconds=2.0) if c is not None else None


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
    """Извлечь текущие значения и активные детекции из открытого сегмента.

    Детекции с участками (values.live, v5.1.21, П4) — по свежести, а не по
    одному последнему подсегменту: живая, пока её кластер идёт или кончился не
    больше N видимого времени до конца последнего подсегмента; ключ, которого
    нет среди живых в последнем подсегменте, берётся взглядом назад из прежних
    подсегментов того же сегмента (analytics.live.alive_detections — та же
    функция, что у «активен на конец» в сводке). Детекции без live — как раньше.
    """
    if not seg.subsegments:
        return {}, []
    last_sub = seg.subsegments[-1]

    current_values: dict[str, Any] = {
        "ts": getattr(last_sub, "t_end", None) or last_sub.t_start,
        "run_state": seg.run_state,
        "run_state_label": seg.run_state_label,
        "coking_risk": last_sub.risk_accumulators.coking_risk.to_dict(),
        "values": {
            role: {
                "value": char.get("value_end"),
                "unit":  char.get("unit", ""),
                "median": char.get("median"),
                # Мин/макс подсегмента — гейту у параметров тревог (v5.1.12)
                "min": char.get("min"),
                "max": char.get("max"),
            }
            for role, char in last_sub.characteristics.items()
            if isinstance(char, dict)
        },
    }
    _end = _as_utc(getattr(last_sub, "t_end", None) or getattr(last_sub, "t_start", None))

    # Детекции из последнего подсегмента — текущее состояние детекторов, у
    # детекций с участками — по свежести кластера и со взглядом назад (v5.1.21).
    # Накопленная «живая» картина ведётся отдельно через _diff_alerts / alert_journal.
    # Снятая панелью ошибка (fault_end проставлен) — исторический факт для отчёта,
    # но НЕ активная тревога: иначе она «залипает» в статусе до границы подсегмента,
    # потому что её период всегда пересекается с окном последнего подсегмента.
    # Сбой расчёта свежести — прежний состав: последний подсегмент как есть
    try:
        from analytics.live import alive_detections, subs_gaps
        _gl = subs_gaps(seg.subsegments)
        _cands = alive_detections(seg.subsegments, _end, _gl)
    except Exception:
        logger.warning("Живой состав по участкам не собран — по последнему подсегменту",
                       exc_info=True)
        _gl, _cands = None, [d.to_dict() for d in last_sub.detections]
    active_dets: list[dict] = []
    for dd in _cands:
        if (dd.get("scenario") == "CONTROLLER_FAULT"
                and (dd.get("values") or {}).get("fault_end")):
            continue
        # Разовое событие перехода — не живая тревога (v5.1.10): иначе эпизод,
        # «воздействие» на весь стоп, журнал и состав гейта
        if dd.get("scenario") in EVENT_SCENARIOS:
            continue
        # Давление ниже паспорта, давно вернувшееся в норму, — факт подсегмента
        # для отчёта, но не живая тревога: иначе висела бы до его конца (v5.1.12)
        if _passport_stale(dd, _end, _gl):
            continue
        _passport_live_hardness(dd, _end, _gl)
        active_dets.append(dd)

    return current_values, active_dets


# Нарушение паспорта в живом составе — пока идёт или закончилось недавно
from analytics.detectors import PASSPORT_RECENT_SEC as _PASSPORT_RECENT_SEC  # noqa: E402


def _as_utc(ts) -> datetime | None:
    if ts is None:
        return None
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts)
        except ValueError:
            return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _passport_live_hardness(d: dict, end: datetime | None, gaps: list | None = None) -> None:
    """Живая копия: «нарушение» давно прошло, а свежий участок — в пределах шага
    датчика → живое значение «в пределах шага» (снимаемо). В отчёте подсегмента
    детекция остаётся как есть (ревью v5.1.12: запрет снятия залипал).

    Вторая ступень свежести паспорта. У детекции с участками (v5.1.21) давность
    «нарушения» — видимое время (gaps — дыры подсегментов): за обрыв связи оно
    не устаревает, как и сам кластер (detection_live)."""
    lim = (d.get("values") or {}).get("limit") or {}
    if lim.get("kind") != "passport" or not lim.get("hard") or lim.get("hard_ongoing") or end is None:
        return
    hu = _as_utc(lim.get("hard_until"))
    if hu is None:
        return
    age = (end - hu).total_seconds()
    if gaps and age > 0 and isinstance((d.get("values") or {}).get("live"), dict):
        from analytics.live import visible_sec
        age = visible_sec(hu, end, gaps)
    if age <= _PASSPORT_RECENT_SEC:
        return
    from analytics.detectors import passport_trigger
    live = dict(lim, hard=False)
    if lim.get("last_value") is not None:
        live["value"] = lim["last_value"]
    d["values"] = dict(d["values"], limit=live, oil_press_min=live.get("value"))
    d["trigger"] = passport_trigger(live)


def _passport_stale(d: dict, end: datetime | None, gaps: list | None = None) -> bool:
    """Первая ступень свежести паспорта: нарушение кончилось давно — не живое.

    С v5.1.21 — обёртка над analytics.live.detection_live для детекции с
    участками (кластер, видимое время); без участков — как в v5.1.12, по
    limit.until/ongoing (импортирует test_gate_passport_v5112)."""
    lim = (d.get("values") or {}).get("limit") or {}
    if lim.get("kind") != "passport" or end is None:
        return False
    from analytics.live import detection_live
    alive = detection_live(d, end, gaps)
    if alive is not None:
        return not alive
    if lim.get("ongoing"):
        return False
    until = _as_utc(lim.get("until"))
    return until is not None and (end - until).total_seconds() > _PASSPORT_RECENT_SEC


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


def _live_by_key(curr_dets: list[dict], now_ts: datetime) -> dict[str, dict]:
    """Кластер нарушения ключа на этот цикл (v5.1.21, П5): {ключ: {onset, until,
    ongoing, mixed, values}}.

    По всем детекциям ключа с values.live берётся самый поздний кластер: у
    близости к уставке детекция на роль, и у каждой свои пересекающие кластеры —
    по одной (последней в списке) until уезжал бы назад. until — конец кластера,
    у идущего — горизонт now_ts (ongoing: until — горизонт, не конец; ревью
    v5.1.21). mixed — у ключа есть и детекция без live (комбинированная ветка
    OIL_DILUTION, прогноз прогрева COOLING): воздействие идёт сейчас, until —
    горизонт. Ключа без live в ответе нет."""
    from analytics.live import live_end
    out: dict[str, dict] = {}
    plain: set[str] = set()
    for d in curr_dets:
        sc = d.get("scenario")
        if not sc or sc in STATE_SCENARIOS:
            continue
        key = _alert_key(d)
        lv = (d.get("values") or {}).get("live")
        if not isinstance(lv, dict):
            plain.add(key)
            continue
        s = _as_utc(lv.get("onset")) or _as_utc(d.get("t_detected"))
        if s is None:
            plain.add(key)
            continue
        until, ongoing = live_end(lv)
        _hz = bool(ongoing or until is None or until > now_ts)
        u = now_ts if _hz else until
        cur = out.get(key)
        if cur is None or (s, u) > (cur["onset"], cur["until"]):
            out[key] = {"onset": s, "until": u, "ongoing": _hz, "values": d.get("values") or {}}
    for key, li in out.items():
        li["mixed"] = key in plain
        if li["mixed"]:
            li["until"] = now_ts
    return out


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


# Удержанные значения на голове окна (v5.1.21, П10): частота — сверке оборотов,
# остальное — переносу участков аналитики через начало окна. v5.1.22: счётчик
# наработки — тем же запросом; у сегментов короче его «пульса» (15 мин) строки
# нет, и модель выдумывала наработку из событий журнала («680 ч»)
# v5.1.23, ревью R7: задание частоты — профилю пуска (значение на начало
# прокрутки, когда прогрев цепочки 0→2→3 длиннее преамбулы окна); тем же запросом
_HELD_ROLES = ("RPM", "FREQUENCY", "OIL_TEMP", "COOLANT_TEMP", "OIL_PRESS", "ENGINE_HOURS",
               "FREQ_REFERENCE")
# Сколько ждать аварийности следующего стопа после закрытия сегмента сменой
# режима (v5.1.22, P1-3): 40013 и маски приходят тем же кадром или следующим
_TRIP_WATCH_SEC = 600
# _run_segment: посев не передан — прочитать самому
_SEED_UNSET: Any = object()


def _gap_overlaps(g: dict, t0: datetime, t1: datetime) -> bool:
    """Дыра пересекает (t0, t1) — то же условие, что у source.get_data_gaps."""
    gs, ge = g.get("gap_start"), g.get("gap_end")
    if gs is None:
        return False
    return _tz_utc(gs) < t1 and (ge is None or _tz_utc(ge) > t0)


async def _load_seed(router_sn: str, equip_type: str, panel_id: int,
                     ts_from: datetime, cfg) -> dict | None:
    """Удержанные значения перед окном истории и обрывы связи от них до окна.

    У головы окна преамбула истории — 300 с, а регистры пишутся по изменению
    (при постоянном значении — «пульс» ~15 мин). Без последнего замера окно не
    знает, что было на его начале:
    - ts/value/gaps — нагрузка: проверка охлаждения («не оценено» у 13 из 14
      стопов, v5.1.10); ключей нет, если нет регистра или его строки;
    - held {роль: {ts, value}} и held_gaps (v5.1.21, П10) — обороты, частота,
      температуры масла и ОЖ, давление масла: частота — сверке оборотов с
      частотой генератора, остальное — переносу участков. Отдаются всегда, даже
      без нагрузки. Значение за дырой связи (между строкой и окном) отброшено.
    Один запрос DISTINCT ON на все регистры и один — дыр от самой старой
    строки до окна (было два на одну нагрузку). None — строк нет или ошибка.
    """
    from analytics import source as _src
    load_addr = cfg.role_to_addr("LOAD_PCT")
    held_addrs: dict[int, str] = {}
    for role in _HELD_ROLES:
        a = cfg.role_to_addr(role)
        if a is not None:
            held_addrs[a] = role
    addrs = sorted(set(held_addrs) | ({load_addr} if load_addr is not None else set()))
    if not addrs:
        return None
    t_from = _tz_utc(ts_from)
    t_hist = t_from - timedelta(seconds=_PREAMBLE_LOOKBACK_SEC)
    load_age = float(cfg.det("COOLDOWN_VIOLATION", "load_hold_max_sec", default=1800.0))
    held_age = float(cfg.seg("rpm_freq_check", "freq_hold_max_sec", default=1800.0) or 1800.0)
    try:
        rows = await _src.get_last_values_before(router_sn, equip_type, panel_id, addrs,
                                                 t_hist, max(load_age, held_age))
        if not rows:
            return None
        oldest = min(_tz_utc(r["ts"]) for r in rows.values())
        gaps = await _src.get_data_gaps(router_sn, equip_type, panel_id, oldest, t_from)
        seed: dict = {"held": {}, "held_gaps": gaps}
        lr = rows.get(load_addr) if load_addr is not None else None
        if lr and lr.get("value") is not None and (t_hist - _tz_utc(lr["ts"])).total_seconds() <= load_age:
            lts = _tz_utc(lr["ts"])
            seed.update(ts=lts, value=float(lr["value"]),
                        gaps=[g for g in gaps if _gap_overlaps(g, lts, t_from)])
        for addr, role in held_addrs.items():
            r = rows.get(addr)
            if not r or r.get("value") is None:
                continue
            rts = _tz_utc(r["ts"])
            if (t_hist - rts).total_seconds() > held_age:
                continue
            if any(_gap_overlaps(g, rts, t_from) for g in gaps):
                continue   # за дырой — что было на голове окна, неизвестно
            seed["held"][role] = {"ts": rts, "value": float(r["value"])}
        return seed
    except Exception:
        logger.warning("Удержанные значения перед окном не прочитаны", exc_info=True)
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
    confirm_to: datetime | None = None,
    load_seed: Any = _SEED_UNSET,
    prev_hint: Any = None,
) -> tuple[list, dict[str, dict], list[dict], dict[str, dict]]:
    """Загрузить данные и запустить segment() в отдельном потоке.

    segment() — CPU-bound синхронная функция. asyncio.to_thread() выносит её
    в ThreadPoolExecutor, освобождая event loop для обработки веб-запросов.

    Третьим возвращает дыры связи окна (уже с синтетическим хвостовым гэпом):
    они нужны вызывающему, чтобы посчитать слепую долю воздействия тревог.
    Четвёртым — хронологии стоянок (ключ тот же, seg.t_start).

    confirm_to — докуда читать данные, чтобы решить исход попытки пуска на
    краю окна (v5.1.16): сегменты режутся по ts_to, данные за ним — только для
    подтверждения.

    load_seed — удержанные значения перед окном (_load_seed), если уже прочитаны:
    рез берёт их из кэша курсора, повторный прогон с confirm_to — тоже (v5.1.21).

    prev_hint — сегмент перед окном (подсказка предка движка): если окно
    начинается с аварийного стопа, лента отсчётов акта — из его отметки обрыва
    (v5.1.22, P1-4).
    """
    import functools
    from analytics.segmenter import segment as _segment

    _load_to = max(_tz_utc(ts_to), _tz_utc(confirm_to)) if confirm_to else ts_to
    history, enum_periods, fault_periods, gaps = await _load_data(
        router_sn, equip_type, panel_id, ts_from, _load_to, cfg
    )
    if load_seed is _SEED_UNSET:
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
            confirm_to=_tz_utc(confirm_to) if confirm_to else None,
            # «Работа» через суточный рез — профиль пуска предка (ревью R1)
            start_carry=_start_carry(prev_hint),
        )
    )
    # Стоп-сегменты получают ДВА артефакта, и они не исключают друг друга:
    #   акт — что произошло, окно вокруг аварии, замирает на лаге;
    #   лента — что было дальше, всё окно сегмента: видно, как неисправности
    #           снимаются одна за другой, пока аварийный стоп не станет обычным.
    # Ключ — seg.t_start.
    incidents: dict[str, dict] = {}
    chronologies: dict[str, dict] = {}
    from analytics.reconstructor import build_segment_chronology as _chrono
    tl_enum, tl_faults = await _timeline_for(
        segments, router_sn, equip_type, panel_id, ts_from, ts_to, cfg,
        enum_periods, fault_periods,
    )
    _plates = _attempt_stops(segments)
    for seg in segments:
        if seg.run_state != 0 or not seg.t_end:
            continue
        # Исключение внутри цикла, а не снаружи: одно падение не должно
        # лишать артефактов все следующие сегменты окна
        try:
            _st = _tz_utc(datetime.fromisoformat(seg.t_start))
            _te = _tz_utc(datetime.fromisoformat(seg.t_end))
            # Момент останова: у плашки неудачной попытки — падение, не её начало
            _stop = _stop_ts_of(seg)
            # Падение плашки за суточным резом — акт строит её продолжение
            if (getattr(seg, "stop_kind", None) == "EMERGENCY" and _stop < _te
                    and _is_stop_head(enum_periods, _stop)):
                # Общий построитель (v5.1.22): лента — из сегмента падения окна
                # или подсказки предка (окно начинается со стопа)
                inc = await _build_act(
                    router_sn, equip_type, panel_id, seg, _te, ts_to, cfg,
                    tl_enum=tl_enum, fault_periods=fault_periods, tl_faults=tl_faults,
                    skip_stops=_plates, segments=segments, prev=prev_hint,
                    key=f"{router_sn}/{equip_type}/{panel_id}",
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


# Запас при догрузке данных для акта: SQL берёт периоды строго с
# state_end > ts_from, без запаса якорный стоп-период выпал бы из выборки.
_ACT_LOAD_MARGIN_SEC = 60
# Сколько прошлых стопов проверить на «после неудачной попытки» при поиске
# якоря (v5.1.16): серия попыток — единицы, дальше — предохранитель
_ANCHOR_ATTEMPT_CHECKS = 8
# Докуда смотреть за суточный рез, решая исход попытки на нём (v5.1.16):
# попытка решается за секунды-минуты, два часа — с запасом на добор истории
_CONFIRM_LOOKAHEAD_SEC = 2 * 3600


def _stop_ts_of(seg) -> datetime:
    """Момент останова стоп-сегмента (v5.1.16).

    У плашки неудачной попытки сегмент начинается на попытке, а машина
    встала позже — на падении: от него лаг акта, ключ «акт построен», проверка
    головы стопа (период стопа в 40011 начинается там) и заказ разбора.
    """
    get = (lambda k: seg.get(k)) if isinstance(seg, dict) else (lambda k: getattr(seg, k, None))
    att = get("attempt")
    raw = att.get("t_fall") if isinstance(att, dict) else None
    raw = raw or get("t_start")
    return _tz_utc(datetime.fromisoformat(raw) if isinstance(raw, str) else raw)


def _fall_segment(seg, segments: list | None, prev=None):
    """Нестоповый сегмент, из которого машина упала в стоп seg (v5.1.22, P1-4):
    предшествующий в окне, иначе подсказка предка (закрыт прошлым циклом; после
    рестарта — из строки БД, _make_seg_hint). Конец — ровно на начале стопа.
    None — не найден (окно начинается со стопа без подсказки, режим другой)."""
    def _get(s, k):
        return s.get(k) if isinstance(s, dict) else getattr(s, k, None)
    try:
        t0 = _tz_utc(datetime.fromisoformat(_get(seg, "t_start")))
    except (TypeError, ValueError):
        return None
    cand = None
    segs = list(segments or [])
    idx = next((i for i, s in enumerate(segs) if s is seg), None)
    if idx is not None and idx > 0:
        cand = segs[idx - 1]
    elif prev is not None:
        cand = prev
    if cand is None or _get(cand, "run_state") in (0, None):
        return None
    te = _get(cand, "t_end")
    try:
        if te is not None and _tz_utc(datetime.fromisoformat(te) if isinstance(te, str) else te) != t0:
            return None
    except (TypeError, ValueError):
        return None
    return cand


def _act_trace(seg, segments: list | None, prev=None) -> tuple[dict | None, dict | None, bool]:
    """(лента отсчётов, сведения о сегменте падения, есть ли отметка обрыва) для
    акта стопа seg (v5.1.22, P1-4, решение В2). Лента снята нарезкой при
    закрытии сегмента падения: у плашки неудачной попытки — Segment.fall_trace
    (плашка сама сегмент падения), у прочих — closed_by_trip["trace"]
    предшествующего нестопового сегмента. Запросов нет."""
    def _get(s, k):
        return s.get(k) if isinstance(s, dict) else getattr(s, k, None)
    att = _get(seg, "attempt")
    if isinstance(att, dict):
        tr = _get(seg, "fall_trace")
        fall = {"run_state": att.get("from_state"), "label": _get(seg, "run_state_label"),
                "t_start": att.get("t_start"), "t_end": att.get("t_fall"), "attempt": True}
        return (tr if isinstance(tr, dict) else None), fall, True
    fs = _fall_segment(seg, segments, prev)
    if fs is None:
        return None, None, False
    trip = _get(fs, "closed_by_trip")
    tr = trip.get("trace") if isinstance(trip, dict) else None
    from analytics.serializer import RUN_STATE_RU
    rs = _get(fs, "run_state")
    # Режим — как в отчёте («Работа»), а не метка каталога панели
    fall = {"run_state": rs, "label": RUN_STATE_RU.get(rs) or _get(fs, "run_state_label"),
            "t_start": _get(fs, "t_start"), "t_end": _get(fs, "t_end")}
    return (tr if isinstance(tr, dict) else None), fall, isinstance(trip, dict)


async def _build_act(
    router_sn: str, equip_type: str, panel_id: int, seg, t_end: datetime | None,
    ts_to: datetime, cfg, *, tl_enum: list[dict], fault_periods: list[dict],
    tl_faults: list[dict], skip_stops: set[datetime] | None,
    segments: list | None = None, prev=None, key: str = "",
) -> dict | None:
    """Акт аварийного стопа — один на все три пути (v5.1.22, P1-4): открытый стоп
    по лагу стабилизации, закрытие сменой режима и прогон окна (_run_segment:
    суточный рез, догонка). Было три почти одинаковые копии — забытая копия уже
    стоила NameError до v4.9.94.

    Контекст от якоря (_load_incident_context, как раньше), лента отсчётов и
    сведения о сегменте падения (_act_trace — из нарезки, без чтения), начало
    выхода из стопа — по уже загруженным enum-периодам. Сегмент падения без
    отметки обрыва аварией (P1-3: «Работа» закрылась раньше, чем аварийность
    стопа пришла в данные) — предупреждение в журнал. None — не аварийный стоп.
    """
    from analytics import classifier as _clf
    stop = _stop_ts_of(seg)
    ctx = await _load_incident_context(router_sn, equip_type, panel_id, stop, ts_to, cfg,
                                       skip_stops=skip_stops)
    ep, fp, tlf, anc = ctx if ctx else (tl_enum, fault_periods, tl_faults, None)
    trace, fall, marked = None, None, True
    try:
        trace, fall, marked = _act_trace(seg, segments, prev)
        if fall is not None:
            chain = _clf.exit_chain_before(ep, stop)
            fall["chain_start"] = chain[0].isoformat() if chain else None
    except Exception:
        logger.warning("OnlineEngine[%s]: лента отсчётов акта не собрана", key, exc_info=True)
    if not marked:
        logger.warning("OnlineEngine[%s]: акт аварии %s — у сегмента падения нет отметки "
                       "обрыва аварией (P1-3) и ленты отсчётов", key, stop.isoformat())
    return _clf.build_stop_incident(
        ep, fp, stop, t_end, cfg,
        stop_kind="EMERGENCY",
        stabilization_sec=_stab_sec(cfg),
        detail_events=_act_detail_events(cfg),
        timeline_faults=tlf,
        anchor=anc,
        min_window_sec=_act_min_window_sec(cfg),
        attempt=getattr(seg, "attempt", None),
        trace=trace, fall=fall,
    )


def _attempt_stops(segments: list) -> set[datetime]:
    """Начала стопов после неудачных попыток в окне — эталоном не бывают."""
    out: set[datetime] = set()
    for s in segments or []:
        att = getattr(s, "attempt", None)
        if isinstance(att, dict) and att.get("t_fall"):
            out.add(_tz_utc(datetime.fromisoformat(att["t_fall"])))
    return out


def _act_min_window_sec(cfg) -> int:
    """Окно данных акта не короче, сек (KB stop_kind.act_min_window_h, дефолт 12 ч)."""
    try:
        h = cfg.seg("stop_kind", "act_min_window_h", default=12)
        return int(float(12 if h is None else h) * 3600)
    except Exception:
        return 12 * 3600


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
    skip_stops: set[datetime] | None = None,
) -> tuple[list[dict], list[dict], list[dict], tuple[datetime, str]] | None:
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

    Стоп после неподтверждённой попытки пуска эталоном не бывает (v5.1.16):
    кандидат проверяется по оборотам и частоте начала цепочки перед ним
    (короткий запрос на каждый), плашки из окна известны заранее (skip_stops).
    Окно данных — от якоря, но не короче act_min_window_h (решение 13.10.2026).
    Четвёртым возвращается якорь — тот же, что ляжет в акт.

    None — если догрузить не удалось; вызывающий работает на обычных данных.
    """
    from analytics import classifier as _clf
    from analytics import source as _src
    from analytics.segmenter import (
        confirm_exit, confirm_params, fault_type_shutdown, ready_intervals, value_series,
    )

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
        skip = {_tz_utc(t) for t in (skip_stops or ())}
        anchor_ts, _reason = _clf.find_baseline_anchor(
            rs_periods, fault_periods, stop_ts, cfg, skip_stops=skip
        )
        prm = confirm_params(cfg)
        if prm["enabled"]:
            _rpm_a, _fq_a = cfg.role_to_addr("RPM"), cfg.role_to_addr("FREQUENCY")
            _addrs = frozenset(a for a in (_rpm_a, _fq_a) if a)
            _ready = ready_intervals(fault_periods, prm, _tz_utc(ts_to))
            for _ in range(_ANCHOR_ATTEMPT_CHECKS):
                if _reason != "normal_stop" or not _addrs:
                    break
                _chain = _clf.exit_chain_before(rs_periods, anchor_ts)
                if _chain is None:
                    break
                _c0, _c1 = _chain
                if (_c1 - _c0).total_seconds() > prm["anchor_check_sec"]:
                    break   # долгая цепочка — машина работала, якорь честный
                _hist, _gaps = await asyncio.gather(
                    _src.get_whitelist_history(
                        router_sn, equip_type, panel_id,
                        _c0 - timedelta(seconds=prm["carry_sec"]), _c1, _addrs,
                    ),
                    _src.get_data_gaps(router_sn, equip_type, panel_id, _c0, _c1),
                )
                _by: dict[int, list[dict]] = {}
                for _r in _hist:
                    _by.setdefault(_r["addr"], []).append(_r)
                _st, _, _ = confirm_exit(
                    _c0, _c1, value_series(_by, _rpm_a), value_series(_by, _fq_a),
                    _ready, _gaps, prm,
                )
                if _st != "unconfirmed":
                    break
                skip.add(anchor_ts)
                anchor_ts, _reason = _clf.find_baseline_anchor(
                    rs_periods, fault_periods, stop_ts, cfg, skip_stops=skip
                )
        data_from = anchor_ts
        if _reason == "normal_stop":
            data_from = min(anchor_ts, stop_ts - timedelta(seconds=_act_min_window_sec(cfg)))
        load_from = data_from - timedelta(seconds=_ACT_LOAD_MARGIN_SEC)
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
        return enum_periods, fault_periods, timeline_faults, (anchor_ts, _reason)
    except Exception:
        logger.warning(
            "Не удалось догрузить данные для акта %s/%s/%s от %s",
            router_sn, equip_type, panel_id, stop_ts, exc_info=True,
        )
        return None


# Напоминание снять ключ переходного месяца (решение В5 15.10.2026): ключу больше
# этого — предупреждение в журнал при старте сервиса
_COUNTS_SINCE_WARN_DAYS = 35
# Нечитаемые значения ключа, о которых уже предупредили (не спамить журнал)
_COUNTS_SINCE_BAD: set[str] = set()


def analytics_counts_since(cfg) -> datetime | None:
    """С какого момента считать счётчики аналитики (v5.1.21, решение В5).

    ALARM_EPISODES.analytics_counts_since в detectors.yaml — дата выкладки
    v5.1.21: эпизоды аналитики до неё строились по границам подсегмента (один
    отсчёт давал «24 ч») и месяц раздували бы «за 30 дней» и «с пуска» в
    гейте, отчёте и заключении. Панель ключ не трогает. Дата без времени —
    полночь в поясе сервиса. Пусто или нечитаемо — None, фильтра нет."""
    from datetime import date as _date
    raw = None
    try:
        raw = cfg.det("ALARM_EPISODES", "analytics_counts_since", default=None)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return None
        if isinstance(raw, datetime):
            ts = raw
        elif isinstance(raw, _date):
            ts = datetime(raw.year, raw.month, raw.day)
        else:
            ts = datetime.fromisoformat(str(raw).strip())
        if ts.tzinfo is None:
            try:
                from config import get_tz
                ts = ts.replace(tzinfo=get_tz())
            except Exception:
                ts = ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc)
    except Exception:
        # Раз на значение: зовётся каждый цикл открытого окна
        _key = repr(raw)
        if _key not in _COUNTS_SINCE_BAD:
            _COUNTS_SINCE_BAD.add(_key)
            logger.warning("ALARM_EPISODES.analytics_counts_since = %s не прочитан — счётчики "
                           "аналитики без фильтра", _key, exc_info=True)
        return None


def warn_stale_counts_since(cfg, now: datetime | None = None) -> bool:
    """Ключ analytics_counts_since старше 35 дней — предупреждение в журнал
    (зовёт менеджер при старте сервиса). True — предупредили."""
    since = analytics_counts_since(cfg)
    if since is None:
        return False
    now = _tz_utc(now or datetime.now(timezone.utc))
    if (now - since).total_seconds() <= _COUNTS_SINCE_WARN_DAYS * 86400:
        return False
    logger.warning(
        "ALARM_EPISODES.analytics_counts_since = %s — ключу больше %d дней: переходный "
        "месяц v5.1.21 прошёл, снимите ключ в detectors.yaml (решение В5 15.10.2026)",
        since.isoformat(), _COUNTS_SINCE_WARN_DAYS)
    return True


def _det_clusters(d) -> list[dict]:
    """Кластеры values.live детекции (Detection или dict); [] — без участков."""
    v = d.get("values") if isinstance(d, dict) else getattr(d, "values", None)
    lv = (v or {}).get("live")
    if not isinstance(lv, dict):
        return []
    return [c for c in lv.get("clusters") or []
            if isinstance(c, dict) and _as_utc(c.get("start")) and _as_utc(c.get("end"))]


def _segment_clusters(seg) -> dict[tuple[str, datetime], dict]:
    """Уникальные кластеры детекций с участками сегмента (v5.1.21, П6):
    (ключ, начало) → {key, scenario, severity, start, end, ongoing, blind_sec, values}.

    Кластер считается на сегменте, а виден в каждом подсегменте, который
    пересекает его участок, у близости — ещё и в детекции каждой роли: один
    кластер — одна запись, конец — самый поздний. values — детекции, где он
    встретился первым, с блоком live только этого кластера (open_values_json
    засеянного эпизода)."""
    out: dict[tuple[str, datetime], dict] = {}
    for sub in getattr(seg, "subsegments", None) or []:
        for d in getattr(sub, "detections", None) or []:
            cl = _det_clusters(d)
            if not cl:
                continue
            dd = d if isinstance(d, dict) else {"scenario": d.scenario, "severity": d.severity,
                                                "values": d.values}
            v = dd.get("values") or {}
            key = _alert_key(dd)
            for c in cl:
                s, e = _as_utc(c["start"]), _as_utc(c["end"])
                cur = out.get((key, s))
                if cur is None:
                    out[(key, s)] = {
                        "key": key, "scenario": dd.get("scenario"), "severity": dd.get("severity"),
                        "start": s, "end": e, "ongoing": bool(c.get("ongoing")),
                        "blind_sec": float(c.get("blind_sec") or 0.0),
                        "values": dict(v, live=dict(v.get("live") or {}, clusters=[c],
                                                    onset=c["start"], until=c["end"],
                                                    ongoing=bool(c.get("ongoing")))),
                    }
                    continue
                if e > cur["end"]:
                    cur["end"] = e
                cur["ongoing"] = cur["ongoing"] or bool(c.get("ongoing"))
                cur["blind_sec"] = max(cur["blind_sec"], float(c.get("blind_sec") or 0.0))
                cur["severity"] = _max_severity(cur["severity"], dd.get("severity"),
                                                dd.get("scenario"))
    return out


def _seed_carry(daily_run: bool, t_to: datetime, live_to: datetime | None,
                poll_sec: float | None) -> bool:
    """Продолжит ли перенос в новом окне кластер, идущий на резе (v5.1.21, П6).

    Только суточный рез «Работы» (daily_run) и только если живой цикл открытого
    окна дошёл почти до реза (не дальше трёх циклов): кластер, по которому
    эпизода нет, живой расчёт не успел увидеть (появился в последний цикл или
    паспорт ещё набирал выдержку 60 с) — его подхватит перенос с 09:00, начало
    эпизода в БД — 09:00 (известное ограничение плана). Догонка после простоя
    или рестарт — False: кластер мог начаться за часы до реза, он сеется
    (эпизод режется на 09:00, но не теряется)."""
    if not daily_run or live_to is None:
        return False
    lag = (_tz_utc(t_to) - _tz_utc(live_to)).total_seconds()
    return lag <= 3 * float(poll_sec or 30)


async def _seed_clusters(
    seg, clusters: dict[tuple[str, datetime], dict],
    router_sn: str, equip_type: str, panel_id: int, seg_t_end: datetime,
    open_keys: set, already: set, seeded_out: set | None,
    gaps: list[dict] | None, live_episodes: dict[str, dict] | None, carry_end: bool,
) -> None:
    """Посев кластеров закрытого сегмента (v5.1.21, П6; правила пропуска — в
    _collect_and_enrich_detections). Эпизоды окна — одним запросом."""
    from analytics.accumulators import gap_union_overlap_sec as _gos
    from analytics.live import LIVE_RECENT_SEC, visible_sec
    n_sec = float(LIVE_RECENT_SEC)
    t_end = _tz_utc(seg_t_end)
    t0 = _as_utc(getattr(seg, "t_start", None)) or min(c["start"] for c in clusters.values())
    # Секунда запаса: эпизод, кончившийся ровно на голове сегмента, тоже сверяется
    try:
        ov = await online_db.get_episodes_overlapping(
            router_sn, equip_type, panel_id, t0 - timedelta(seconds=1), t_end)
    except Exception:
        logger.warning("Эпизоды окна сегмента не получены — кластеры сеются без сверки "
                       "пересечения", exc_info=True)
        ov = []
    # Конец нарушения открытого точного эпизода: open_values_json.live.until, память
    # свежее и решает (ревью v5.1.21: в БД мог остаться горизонт паузы пакетов,
    # память уточнена по кластерам окна — _refine_until)
    mem_until = {ep.get("id"): ep.get("until") for ep in (live_episodes or {}).values()
                 if ep.get("exact") and ep.get("until") is not None}
    covers: list[tuple] = []     # (сценарий, начало, конец закрытого | None, until открытого | None)
    for e in ov:
        if e.get("scenario") == "CONTROLLER_FAULT" or e.get("t_open") is None:
            continue
        a = _tz_utc(e["t_open"])
        if e.get("t_close") is not None:
            covers.append((e.get("scenario"), a, _tz_utc(e["t_close"]), None))
            continue
        lv = _episode_open_values(e).get("live")
        u = None
        if isinstance(lv, dict):
            u = _as_utc(lv.get("until"))
            mu = mem_until.get(e.get("id"))
            if mu is not None:
                u = _tz_utc(mu)
        covers.append((e.get("scenario"), a, None, u))   # u None — до бесконечности

    def _covered(c: dict) -> bool:
        for sc, a, b, u in covers:
            if sc != c["scenario"] or c["end"] < a:
                continue
            if b is not None:
                # Закрытый — только настоящим пересечением (ревью v5.1.21, engine-2):
                # засеянный до реза [S, 09:00] касается перенесённого [09:00, E] при
                # догонке — касание не покрытие, иначе продолжение терялось
                if c["start"] < b:
                    return True
                continue
            if u is None or c["start"] <= u or visible_sec(u, c["start"], gaps or []) <= n_sec:
                return True
        return False

    for (k, s), c in sorted(clusters.items(), key=lambda x: x[0][1]):
        if (k, s) in open_keys or (k, s) in already or _covered(c):
            continue
        # Идёт на конец окна, эпизода по нему нет — новое окно продолжит его
        # переносом с 09:00 (ошибка начала — около цикла)
        if carry_end and c["ongoing"]:
            continue
        e_ = max(c["end"], s)
        span = (e_ - s).total_seconds()
        blind = _gos(gaps, s, e_) if gaps else c["blind_sec"]
        blind = min(span, max(0.0, float(blind or 0.0)))
        try:
            await online_db.insert_closed_episode(
                router_sn, equip_type, panel_id,
                scenario=c["scenario"], source="analytics", severity=c["severity"],
                t_open=s, t_close=e_, active_sec=span, open_values=c["values"],
                blind_sec=blind, close_reason="condition_cleared",
            )
        except Exception:
            logger.warning("Не удалось создать эпизод кластера %s с %s", k, s, exc_info=True)
            continue
        if seeded_out is not None:
            seeded_out.add((k, s))
        # Засеянный кластер покрывает свой интервал (поздние строки сдвинули начало)
        covers.append((c["scenario"], s, e_, None))


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
    live_episodes: dict[str, dict] | None = None,
    carry_end: bool = False,
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

    v5.1.21 (П6): детекции с участками (values.live) сеются по кластерам — один
    эпизод на уникальный кластер сегмента (ключ, начало), [начало, конец],
    воздействие = конец − начало, слепая доля — дыры внутри. Кластер не сеется:
    - пара (ключ, начало) уже есть в alarm_episodes (existing_episode_onsets) или
      засеяна раньше в этом окне (open_keys);
    - его пересекает эпизод того же сценария — один запрос get_episodes_overlapping
      на окно сегмента, фильтр в памяти. Открытый точный эпизод покрывает только
      [t_open, until + N] (until — из open_values_json и памяти live_episodes), а
      не «до бесконечности»: после догонки остальные кластеры сеются; открытая
      старая строка без live — до бесконечности, как раньше;
    - carry_end и кластер идёт на конец окна — его продолжит перенос в новом
      окне (суточный рез «Работы» без смены режима, живой цикл шёл до самого
      реза — _seed_carry). Начало эпизода в БД — 09:00, ошибка — около цикла.
      При догонке после простоя carry_end = False: такой кластер мог начаться
      за часы до 09:00, и он сеется (эпизод режется на 09:00, но не теряется).
    Кластер, живой на конец, без эпизода в памяти и без переноса — сеется. В
    seeded_out — пара (ключ, начало): второй закрываемый сегмент окна получает
    свои кластеры. Прежние правила (ключ в open_keys, первый участок
    подсегментов) — только для детекций без live; панель и события — как было.

    Мутация до seg.to_dict() / to_markdown() — счётчики попадают в отчёт.
    """
    window_days = int(cfg.det("DETECTION_COUNTER", "window_days", default=30) or 30)

    # Уникальные тревоги per-fault (панельные — по addr/bit, аналитика — по scenario).
    # У аналитики — где кончается первый непрерывный участок подсегментов с ней и
    # последний подсегмент с ней (v5.1.15): засеянный эпизод не тянется до конца
    # сегмента и не дублирует живой, открытый на подсегмент раньше
    def _gather(pick) -> dict:
        seen: dict = {}
        _run_open: set = set()
        for sub in seg.subsegments:
            _sub_end = None
            if getattr(sub, "t_end", None):
                try:
                    _sub_end = _tz_utc(datetime.fromisoformat(sub.t_end))
                except (TypeError, ValueError):
                    _sub_end = None
            _here: set = set()
            for d in sub.detections:
                if not pick(d):
                    continue
                k = _alert_key({"scenario": d.scenario, "values": d.values})
                # Панель — по фронту: бит, снятый и поднятый в одном сегменте, — две тревоги
                sk = (k, d.t_detected) if d.scenario == "CONTROLLER_FAULT" else k
                if d.scenario != "CONTROLLER_FAULT":
                    _here.add(sk)
                    if sk in seen and _sub_end is not None:
                        seen[sk]["last_end"] = _sub_end
                        if sk in _run_open:
                            seen[sk]["first_run_end"] = _sub_end
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
                        "values": v, "first_run_end": _sub_end, "last_end": _sub_end,
                    }
                    if d.scenario != "CONTROLLER_FAULT":
                        _run_open.add(sk)
            _run_open &= _here
        return seen

    # Все тревоги — для счётчиков и detection_events (как было)
    seen = _gather(lambda d: True)
    if not seen:
        return []
    # Кластеры детекций с участками (v5.1.21); сбой — посев по-старому для всех
    try:
        clusters = _segment_clusters(seg)
    except Exception:
        logger.warning("Кластеры сегмента для посева не собраны — посев по-старому",
                       exc_info=True)
        clusters = {}
    plain = _gather(lambda d: not _det_clusters(d)) if clusters else seen

    # Эпизоды для эфемерных тревог — тех, у которых эпизода НЕТ ВООБЩЕ.
    # Память движка отвечает только на вопрос «жива ли тревога сейчас», а
    # тревога, прожившая своё и закрытая дебаунсом внутри сегмента, под него
    # не подходит и засевалась второй раз. Поэтому спрашиваем базу.
    if open_keys is not None:
        try:
            _already = await online_db.existing_episode_onsets(
                router_sn, equip_type, panel_id,
                [i["t_open"] for i in plain.values() if i.get("t_open")]
                + [c["start"] for c in clusters.values()],
            )
        except Exception:
            logger.warning("Не удалось проверить существующие эпизоды", exc_info=True)
            _already = set()
        # Детекции с участками — по кластерам (v5.1.21, П6)
        if clusters:
            await _seed_clusters(seg, clusters, router_sn, equip_type, panel_id, seg_t_end,
                                 open_keys, _already, seeded_out, gaps, live_episodes, carry_end)
        for _sk, info in plain.items():
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
            # Живой эпизод мог открыться на подсегмент раньше: граница зоны нагрузки
            # подтверждается с задержкой, и живой расчёт видел показание ещё в
            # старом подсегменте (онсет b0), итоговый — в новом (c0). Эпизод того
            # же сценария, пересекающий первый участок детекции, — уже есть (v5.1.15)
            if (not _panel and not _event and info.get("t_open")
                    and info.get("first_run_end")):
                try:
                    _ov = await online_db.get_episodes_overlapping(
                        router_sn, equip_type, panel_id, info["t_open"], info["first_run_end"])
                except Exception:
                    _ov = []
                if any(e.get("scenario") == info["scenario"] for e in _ov):
                    continue
            t_open = info["t_open"] or _tz_utc(seg_t_end)
            # Снятая панелью тревога закрывается временем снятия, а не концом
            # сегмента; аналитика — концом последнего подсегмента с ней (v5.1.15)
            t_close = _tz_utc(seg_t_end)
            if not _panel and not _event and info.get("last_end"):
                t_close = min(info["last_end"], t_close)
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

    # Счётчики по эпизодам: WHERE по scenario, результат — per-fault ключом;
    # у аналитики — с даты выкладки v5.1.21 (В5, переходный месяц)
    try:
        counts = await online_db.count_episodes_batch(
            router_sn, equip_type, panel_id,
            list({info["scenario"] for info in seen.values()}),
            window_days, run_origin_ts,
            analytics_since=analytics_counts_since(cfg),
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
            analytics_since=analytics_counts_since(cfg),
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
        # Удержанные значения перед окном (нагрузка, частота, …) — одни на курсор
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
        # отложить: цикл смены режима удалил открытую строку до _process_episodes.
        # v5.1.22: [(сценарий, id эпизода, начало)] и детекции
        self._supp_recheck: tuple[list[tuple], list[dict]] | None = None
        # Эпизоды, которым отказано в наследовании снятия по запрету (сигнал
        # панели, свежая «Общая авария», аварийный стоп): id → (сценарий, id,
        # начало). Когда запрет снимется, а эпизод ещё идёт, наследование
        # повторяется (v5.1.22, критика 4: иначе статус «снято», гейт молчит, а
        # в сводке живое 🟡). Подпись панели и строки на момент отказа — чтобы не
        # спрашивать строку каждый цикл, пока запрет тот же
        self._supp_refused: dict[int, tuple] = {}
        self._supp_refused_sig: tuple | None = None
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
        # Когда последний раз оценивали шаг показаний датчиков (v5.1.12)
        self._sensor_steps_at: datetime | None = None
        # Сегмент, закрытый сменой режима в стоп без отметки обрыва аварией:
        # {id, t_start, t_end, run_state}. Если аварийность стопа придёт кадром
        # позже, отметка дописывается в закрытую сводку (v5.1.22, P1-3)
        self._trip_watch: dict | None = None

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
        """Заказать разбор аварии. Повод — готовый акт.

        Гейт предупреждений для аварии не годился: он ждёт 60 секунд
        стабилизации состава, а у трети аварий маску снимали раньше, состав
        становился «норма» и отсчёт выбрасывался — разбора не было вовсе.
        Акт же строится у каждой аварии по построению и несёт материал
        богаче снимка состояния.

        Учтённая фоновая задача (online.incident_gate, v5.1.11): цикл движка
        её не ждёт; дубль отбрасывается, срок — сутки, один повтор.
        """
        try:
            from online.incident_gate import order_incident_analysis
            if order_incident_analysis(self.router_sn, self.equip_type, self.panel_id,
                                       incident, stop_ts, segment_id):
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
                # Ключ — момент останова из самого акта: у плашки неудачной
                # попытки строка начинается раньше падения (v5.1.16)
                _inc = _row["incident_json"]
                if isinstance(_inc, str):
                    try:
                        import json as _j
                        _inc = _j.loads(_inc)
                    except Exception:
                        _inc = None
                _stop_raw = _inc.get("stop_ts") if isinstance(_inc, dict) else None
                self._act_done_for = (_tz_utc(datetime.fromisoformat(_stop_raw)) if _stop_raw
                                      else _tz_utc(_row["t_start"]))
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
                # Точный эпизод аналитики (v5.1.21): в open_values_json есть live,
                # конец кластера until движок пишет туда же (update_episode). Нет
                # until — первая же детекция ключа считается продолжением; давний
                # until — моста нет; восстановленный until — «горизонт»: первый цикл
                # уточнит его по карте кластеров окна (_refine_until, ревью
                # shadow-2), и эпизод закроется по концу своего кластера, без
                # времени простоя сервиса
                _ov = e.get("open_values_json")
                if isinstance(_ov, str):
                    try:
                        _ov = json.loads(_ov)
                    except Exception:
                        _ov = None
                _lv = _ov.get("live") if isinstance(_ov, dict) else None
                _exact = isinstance(_lv, dict) and e.get("scenario") != "CONTROLLER_FAULT"
                _until = _as_utc(_lv.get("until")) if _exact else None
                # until из JSON — момент последнего цикла до остановки (мог быть
                # горизонтом идущего кластера): первый цикл уточняет его по карте
                # кластеров окна — закрытия окна (рез в простое) и открытого
                # (пересегментированного от курсора), ревью v5.1.21 shadow-2
                self._episodes[k] = {
                    "id": e["id"], "severity": e.get("severity"),
                    "miss": 0, "first_miss_ts": None,
                    "t_open": _tz_utc(e["t_open"]) if e.get("t_open") else None,
                    "until": _until,
                    "until_kind": "horizon" if _until is not None else None,
                    "exact": _exact,
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

    @staticmethod
    def _panel_sig(curr_dets: list[dict]) -> frozenset:
        """Подпись сигналов панели в детекциях: запрет снятия по ним меняется
        только вместе с ней (или со строкой)."""
        return frozenset(
            (_alert_key(d), d.get("severity"), tuple(d.get("fault_codes") or []))
            for d in curr_dets or []
            if isinstance(d, dict) and d.get("scenario") == "CONTROLLER_FAULT")

    def _retry_sig(self, curr_dets: list[dict]) -> tuple:
        """Подпись условий наследования: панель, строка (начало окна), состав
        аналитики. Пока она та же, повтор дал бы тот же отказ."""
        try:
            from online.status_assembler import _without_state, compute_analytics_hash, gate_dets
            comp = compute_analytics_hash(gate_dets(_without_state(curr_dets)))
        except Exception:
            comp = None
        return (self._panel_sig(curr_dets), getattr(self, "cursor_ts", None), comp)

    def _refused_retry_due(self, curr_dets: list[dict]) -> bool:
        """Повторить наследование отказанным эпизодам (v5.1.22) — без запроса:
        только если сигнала панели WARNING/SHUTDOWN нет и подпись условий
        (панель, строка, состав) сменилась с прошлой попытки."""
        if any(isinstance(d, dict) and d.get("scenario") == "CONTROLLER_FAULT"
               and d.get("severity") in ("WARNING", "SHUTDOWN") for d in curr_dets or []):
            return False
        return self._retry_sig(curr_dets) != getattr(self, "_supp_refused_sig", None)

    async def _inherit_gate_suppression(self, new_eps: list[tuple], curr_dets: list[dict],
                                        ts: datetime | None = None) -> bool:
        """Пометить новые эпизоды снятыми, если гейт уже снял их состав в этой строке.

        False — открытой строки нет (проверку надо повторить после upsert).

        v5.1.22 (P1-1 п. 2–4). new_eps — [(сценарий, id эпизода, начало)].
        - Снятие действует (suppression_active: состав совпал, запретов нет) и
          решение принято в этой строке (разбор моделью или повтор) — помечаются
          только эти id (и отказанные раньше, ещё идущие), запись inherited со
          ссылкой на исходный разбор.
        - Хеш строки перенесён (carry/late), своего решения нет — помечается
          только продолжение снятого эпизода через границу (начался до начала
          строки: тревога появилась в закрытом сегменте, живой эпизод заведён
          после закрытия), запись carry. Новый эпизод в этой строке снятие не
          наследует: хеш с строки снимается, его разберёт гейт (иначе статус
          «снято», гейт молчит, а в сводке живое 🟡).
        - Запрет (сигнал панели, свежая «Общая авария», аварийный стоп) — отказ:
          id в памяти, повтор при снятии запрета (_refused_retry_due).
        Строка — тот же запрос, что раньше; запись в БД — только при пометке.
        """
        try:
            from online.status_assembler import (
                GATE_CARRY_KINDS, GATE_EXCLUDED_SCENARIOS, GATE_RULES_VERSION,
                UNCANCELLABLE_SCENARIOS, gate_decision_source, is_analytics_suppressed,
                passport_hard_seen, suppression_bans,
            )
            _row = await online_db.get_open_segment(
                self.router_sn, self.equip_type, self.panel_id)
            if not _row:
                return False
            refused = getattr(self, "_supp_refused", None)
            if refused is None:
                refused = self._supp_refused = {}
            # Отказанные раньше и ещё идущие — повтор вместе с новыми
            _alive = {m.get("id") for m in (self._episodes or {}).values()}
            for _i in [i for i in refused if i not in _alive]:
                refused.pop(_i, None)
            _hard = {d.get("scenario") for d in curr_dets or []
                     if isinstance(d, dict) and passport_hard_seen(d)}
            cand: dict[int, tuple] = {}
            for c in list(new_eps or []) + list(refused.values()):
                sc, ep_id = c[0], c[1]
                if (ep_id is None or sc in UNCANCELLABLE_SCENARIOS
                        or sc in GATE_EXCLUDED_SCENARIOS or sc in _hard):
                    continue
                cand[ep_id] = c
            # Подпись условий этой попытки — повтор отказанных только при её смене
            self._supp_refused_sig = self._retry_sig(curr_dets)
            h = _row.get("gate_suppressed_hash")
            if not cand or not h or not is_analytics_suppressed(_row, curr_dets):
                return True
            _log = _row.get("gate_log")
            if isinstance(_log, str):
                try:
                    _log = json.loads(_log)
                except Exception:
                    _log = []
            _log = _log if isinstance(_log, list) else []
            src = gate_decision_source(_log, h)
            kind = "inherited"
            take = list(cand.values())
            if src is None:
                # Перенесённое снятие: только продолжение через границу того, что
                # закрытая карточка сняла (эпизоды записи, синтетика, снятая в её
                # сводке, поздний вердикт — до момента разбора; ревью gate-4: новая
                # тревога за цикл до реза, в закрытой карточке живая, снималась
                # без разбора гейтом)
                src = gate_decision_source(_log, h, GATE_CARRY_KINDS)
                kind = "carry"
                _t0 = _as_utc(_row.get("t_start"))
                _ids, _synth, _late = _carry_scope(_log, h)

                def _carried(c: tuple) -> bool:
                    if c[1] in _ids or c[0] in _synth:
                        return True
                    _u = _late.get(c[0])
                    return _u is not None and _tz_utc(c[2]) <= _u

                take = [c for c in cand.values()
                        if src is not None and _t0 is not None and len(c) > 2
                        and c[2] is not None and _tz_utc(c[2]) < _t0 and _carried(c)]
                fresh = [c for c in cand.values() if c not in take]
                if fresh:
                    if await online_db.clear_open_gate_suppression_hash(
                            self.router_sn, self.equip_type, self.panel_id, h):
                        logger.info("OnlineEngine[%s]: новый эпизод %s в строке с перенесённым "
                                    "снятием — хеш снят, состав разберёт гейт", self.key,
                                    [c[1] for c in fresh])
            if not take or src is None:
                return True
            bans = suppression_bans(_row, curr_dets)
            if bans:
                for c in take:
                    refused[c[1]] = c
                logger.info("OnlineEngine[%s]: снятие на эпизоды %s не перенесено — %s; "
                            "повтор, когда запрет снимется", self.key,
                            [c[1] for c in take], ", ".join(bans))
                return True
            _source = {"ts": src.get("ts"), "segment_id": src.get("segment_id")}
            marked = await online_db.set_episodes_gate_suppressed(
                self.router_sn, self.equip_type, self.panel_id,
                [c[0] for c in take], ids=[c[1] for c in take],
                src={**_source, "kind": kind},
            )
            for c in take:
                refused.pop(c[1], None)
            if marked:
                _ts = _tz_utc(ts) if ts is not None else datetime.now(timezone.utc)
                await online_db.append_segment_gate_event(
                    self.router_sn, self.equip_type, self.panel_id,
                    segment_id=_row.get("id"), ts=_ts,
                    event={
                        "ts": _ts.isoformat(), "kind": kind,
                        "fault_hash": src.get("fault_hash"), "suppressed_hash": h,
                        "alarms": [{"scenario": sc} for sc in sorted({c[0] for c in take})],
                        "decision": "cancel", "decision_applied": True,
                        "reason": ("новый эпизод того же состава в этой строке — снятие "
                                   "по исходному разбору" if kind == "inherited" else
                                   "тревога продолжается через границу сегмента — снятие "
                                   "по исходному разбору"),
                        "rules": GATE_RULES_VERSION,
                        "provider": kind, "model": None, "tokens_in": 0, "tokens_out": 0,
                        "source": _source, "episodes": list(marked),
                    },
                )
        except Exception:
            logger.warning("OnlineEngine[%s]: не удалось перенести снятие гейтом "
                           "на новый эпизод", self.key, exc_info=True)
        return True

    async def _process_episodes(
        self, curr_dets: list[dict], t_to: datetime, gaps: list[dict],
        cleared_at: dict[str, datetime] | None = None,
        gaps_from: datetime | None = None,
        run_state: int | None = None,
        start_carry: dict | None = None,
    ) -> None:
        """Материализовать эпизоды тревог из снимка активных детекций.

        start_carry — профиль пуска «Работы»-предка у открытой «Работы», идущей
        через суточный рез (Segment.start_profile_prev, v5.1.23, R1): только
        тогда детекциям оборотов ставится начало эпизода (_mark_episode_onsets).

        Открытие — сразу; закрытие — после N циклов подряд «чисто»
        (ALARM_EPISODES.close_debounce_cycles в detectors.yaml, default 3 —
        защита счётчиков от мерцания условия у порога).

        active_sec — астрономическое воздействие, оно идёт и в дыре: залипшая
        неисправность висит на панели, пока её не сбросят. Слепая доля не
        вычитается, а копится отдельно в blind_sec. Дебаунс при этом в дыре
        замирает — объявлять тревогу снятой, пока мы её не видим, нельзя.

        Эпизоды аналитики по событию (v5.1.21, П5; жалоба 15.10: одиночный
        отсчёт давал эпизод «24 ч» от начала подсегмента). Детекция с
        values.live несёт кластер нарушения (analytics/live.py), её эпизод —
        «точный» (exact в памяти):
        - открывается с начала кластера S (values.live.onset);
        - продолжается, если S не дальше N видимого времени от конца прежнего
          кластера until (до начала окна — астрономически, ошибка ≤ N, без
          запроса) или until неизвестен (после рестарта); until — конец
          кластера, у идущего — горизонт; детекция того же ключа без live
          (комбинированная ветка OIL_DILUTION, прогноз COOLING) — тоже горизонт;
        - кластер дальше конца прежнего + N — разрез: прежний закрывается по
          концу своего кластера (_exact_end), новый открывается с S;
        - мост: окно после суточного реза или рестарта прежнего кластера не
          видит — промах не считается, если машина в работе (run_state 3, и
          окно продолжает работу), until не позже начала окна (gaps_from) и с
          until прошло не больше N видимого времени. Отозванное внутри окна —
          без моста;
        - закрывается точно: t_close — конец по карте cleared_at (until —
          запасной, см. ниже), не раньше t_open; active_sec = t_close − t_open,
          blind_sec — дыры внутри (close_episode(exact=True)).
        Эпизоды без live (панель, старые строки, восстановленные без live) —
        как раньше.

        v5.1.21, ревью (engine-1, shadow-1/2): «конец прежнего кластера» для
        продолжения, разреза, моста и промаха — по карте cleared_at (цепочка от
        кластера самого эпизода в окне, _analytics_cleared_at), until памяти —
        запасной (_exact_end). until идущего кластера — горизонт (until_kind
        «horizon»): когда тот же кластер больше не идёт, until — его
        фактический конец, и вниз (пауза пакетов без data_gaps кончает участок
        на последнем пакете — время паузы не воздействие). Иначе until назад не
        уезжает (роли, поздние строки, детекция без live — «plain»).
        """
        from analytics.live import LIVE_RECENT_SEC, visible_sec
        _n_sec = float(LIVE_RECENT_SEC)
        debounce = int(self.cfg.det("ALARM_EPISODES", "close_debounce_cycles", default=3) or 3)
        now_ts = _tz_utc(t_to)
        # Сценарии-состояния (риск нагара) — не тревоги: эпизодов у них нет
        curr_map = {_alert_key(d): d for d in curr_dets
                    if d.get("scenario") and d.get("scenario") not in STATE_SCENARIOS}
        # Кластеры ключей с участками (v5.1.21); сбой — эпизоды по-старому
        try:
            live_info = _live_by_key(curr_dets, now_ts)
        except Exception:
            logger.warning("OnlineEngine[%s]: кластеры детекций не разобраны — эпизоды "
                           "по-старому", self.key, exc_info=True)
            live_info = {}

        # База — конец окна, а не последние данные (как было): иначе при
        # обрыве она замирала бы и воздействие переставало идти.
        span_sec, gap_sec, live_sec = _accrual_split(
            self._episode_accrual_ts, now_ts, gaps
        )
        self._episode_accrual_ts = now_ts

        # Открытие новых / обновление живущих
        # Новые эпизоды аналитики: (сценарий, id, начало) — наследование снятия по id (v5.1.22)
        _new_analytics: list[tuple] = []
        _hc: dict = {}    # кэш разрывов до окна (голова эпизода)
        for key, d in curr_map.items():
            ep = self._episodes.get(key)
            sc = d.get("scenario")
            sev = d.get("severity")
            _vals = d.get("values") or {}
            li = live_info.get(key)
            # Точный эпизод, новый кластер дальше конца прежнего + N — разрез:
            # прежний закрывается по концу своего нарушения (по карте окна, ревью
            # v5.1.21: не по горизонту паузы и не по until JSON после рестарта),
            # новый — с начала своего
            _end = (_exact_end(ep, (cleared_at or {}).get(key))
                    if ep is not None and ep.get("exact") and li is not None else None)
            if (ep is not None and ep.get("exact") and li is not None
                    and not self._episode_continues(ep, li["onset"], gaps, _n_sec, end=_end)):
                if not await self._close_exact(key, ep, _end or now_ts,
                                               gaps, gaps_from, _hc):
                    continue   # строка не закрылась — разрез повторит следующий цикл
                logger.info("OnlineEngine[%s]: эпизод %s закрыт по концу нарушения %s — "
                            "новый кластер с %s", self.key, key,
                            (_end or now_ts).isoformat(), li["onset"].isoformat())
                self._episodes.pop(key, None)
                ep = None
            if ep is None:
                t_open = now_ts
                if li is not None:
                    # Начало кластера, а не начало подсегмента (v5.1.21)
                    t_open = li["onset"]
                elif d.get("t_detected"):
                    try:
                        t_open = _tz_utc(datetime.fromisoformat(d["t_detected"]))
                    except Exception:
                        pass
                # Память точного эпизода; в open_values — блок live с until
                # горизонта (его же пишет update_episode — для рестарта)
                _mem_live = ({"exact": True, "until": li["until"], "until_kind": _until_kind(li)}
                             if li is not None
                             else {"exact": False, "until": None, "until_kind": None})
                if li is not None:
                    _lv0 = (li["values"] or {}).get("live") or {}
                    _vals = dict(li["values"], live=dict(_lv0, until=li["until"].isoformat()))
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
                    # until — тем же UPDATE (v5.1.21): посев и рестарт читают его из БД
                    _ru = {"until": li["until"]} if li is not None else {}
                    try:
                        await online_db.reopen_episode(
                            ep_id, now_ts=now_ts, blind_sec_add=tail_blind,
                            severity=_max_severity(prev.get("severity"), sev, sc), **_ru,
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
                                           "t_open": t_open, **_mem_live}
                    if sc != "CONTROLLER_FAULT":
                        _new_analytics.append((sc, ep_id, t_open))
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
                                       "t_open": t_open, **_mem_live}
                if sc != "CONTROLLER_FAULT":
                    _new_analytics.append((sc, ep_id, t_open))
                logger.info("OnlineEngine[%s]: эпизод открыт — %s (severity=%s%s)",
                            self.key, key, sev,
                            f", с {t_open.isoformat()}" if li is not None else "")
                if sc == "CONTROLLER_FAULT" and sev == "SHUTDOWN":
                    await self._attach_trip_context(ep_id, t_open)
            else:
                ep["miss"] = 0
                ep["first_miss_ts"] = None
                new_sev = _max_severity(ep.get("severity"), sev, sc)
                if (sc == "CONTROLLER_FAULT" and new_sev == "SHUTDOWN"
                        and ep.get("severity") != "SHUTDOWN"):
                    await self._attach_trip_context(ep["id"], now_ts)
                # Точный эпизод: конец нарушения — конец кластера (у идущего и у
                # детекции без live — горизонт); назад не уезжает (поздние строки
                # и детекции по ролям). В БД — тем же UPDATE (для рестарта).
                # v5.1.21, ревью (engine-1): until был горизонтом идущего кластера,
                # а тот же кластер (начался не позже прежнего горизонта) больше не
                # идёт — until = его фактический конец, и вниз: синтетический хвост
                # паузы пакетов без data_gaps держал горизонт, итоговый участок
                # кончается на последнем пакете перед паузой
                _upd: dict = {}
                if ep.get("exact"):
                    if li is None:
                        u_new, _kind = now_ts, "plain"
                    else:
                        u_new, _kind = li["until"], _until_kind(li)
                    _u0 = ep.get("until")
                    if (_u0 is None or u_new >= _u0
                            or (_kind == "end" and ep.get("until_kind") == "horizon"
                                and li is not None and li["onset"] <= _u0)):
                        ep["until"], ep["until_kind"] = u_new, _kind
                    _upd["until"] = ep["until"]
                try:
                    await online_db.update_episode(
                        ep["id"], active_sec_add=span_sec, blind_sec_add=gap_sec,
                        severity=new_sev if new_sev != ep.get("severity") else None,
                        **_upd,
                    )
                    ep["severity"] = new_sev
                except Exception:
                    logger.warning("OnlineEngine[%s]: не удалось обновить эпизод %s",
                                   self.key, key, exc_info=True)

        # Новый аналитический эпизод того состава, который гейт уже снял: живой
        # статус по хешу открытой строки — «норма», гейт не перезапустится, и
        # флаг эпизоду иначе не поставит никто — в сводке выйдет живое замечание.
        # v5.1.22: по id, с запретами и источником; отказанным по запрету —
        # повтор, когда запрет снимется (без запроса, пока запрет тот же)
        # Начало эпизода у просадки и заброса, идущих мостом через рез, — для
        # запрета снятия на неустойчивом пуске (ревью v5.1.23, R1); только у
        # «Работы», несущей профиль предка (start_carry)
        try:
            _since = _carry_onset_floor(start_carry)
            if _since is not None:
                _mark_episode_onsets(curr_dets, self._episodes, since=_since)
        except Exception:
            logger.warning("OnlineEngine[%s]: начало эпизодов оборотов не отмечено", self.key,
                           exc_info=True)
        _refused = getattr(self, "_supp_refused", None) or {}
        if _refused and not _new_analytics:
            _alive = {m.get("id") for m in self._episodes.values()}
            for _i in [i for i in _refused if i not in _alive]:
                _refused.pop(_i, None)
        if _new_analytics or (_refused and self._refused_retry_due(curr_dets)):
            if not await self._inherit_gate_suppression(_new_analytics, curr_dets, ts=now_ts):
                # Открытую строку цикл смены режима уже удалил, новая ещё не записана —
                # проверить после upsert (хвост гейта с хешем переносится туда же)
                self._supp_recheck = (_new_analytics, curr_dets)

        # Дебаунс закрытия — только когда данные реально шли (в дыре всё замирает)
        if live_sec <= 0:
            return
        # Мост — только когда окно продолжает работу (суточный рез, рестарт): после
        # смены режима (стоп → пуск) прежний кластер новому окну не родня
        _prev_rs = getattr(getattr(self, "_prev_seg_hint", None), "run_state", None)
        _bridge_ok = run_state == 3 and gaps_from is not None and _prev_rs in (None, 3)
        for key in list(self._episodes.keys()):
            if key in curr_map:
                continue
            ep = self._episodes[key]
            exact = bool(ep.get("exact"))
            _u = ep.get("until")
            if exact:
                # Конец нарушения — по карте окна, until памяти — запасной (ревью
                # v5.1.21: горизонт синтетического хвоста паузы не конец). Память
                # — тоже фактический конец (сводка закрываемого сегмента, посев)
                _ca_x = (cleared_at or {}).get(key)
                _u = _exact_end(ep, _ca_x)
                if (_u is not None and _ca_x is not None and _u != ep.get("until")
                        and ep.get("until_kind") != "plain"):
                    ep["until"], ep["until_kind"] = _u, "end"
            # Мост (v5.1.21): новое окно прежнего кластера не видит (until не
            # позже начала окна: идущий на резе кластер кончается на нём) — пока
            # с его конца прошло не больше N, это не промах (повтор в этот срок —
            # продолжение, иначе закрытие по until)
            if (exact and _bridge_ok and _u is not None and _u <= _tz_utc(gaps_from)
                    and visible_sec(_u, now_ts, gaps) <= _n_sec):
                continue
            ep["miss"] += 1
            if ep["first_miss_ts"] is None:
                # Панель знает точное время снятия (fault_end) — оно и есть
                # конец эпизода; иначе — горизонт обработки, как раньше.
                # Снятие раньше фронта — это прежний период той же маски
                # (_panel_cleared_at берёт самый поздний fault_end по ключу)
                _ca = (cleared_at or {}).get(key)
                if _ca is not None and ep.get("t_open") is not None and _ca < ep["t_open"]:
                    _ca = None
                if exact:
                    # Конец нарушения: по карте (цепочка от кластера эпизода), без
                    # неё — until памяти (_u выше); не раньше t_open
                    _fm = _u if _u is not None else now_ts
                    if ep.get("t_open") is not None and _fm < ep["t_open"]:
                        _fm = ep["t_open"]
                    ep["first_miss_ts"] = _fm
                else:
                    ep["first_miss_ts"] = _ca or now_ts
            if ep["miss"] < debounce:
                continue
            if exact:
                if not await self._close_exact(key, ep, ep["first_miss_ts"], gaps, gaps_from, _hc):
                    continue
            else:
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

    @staticmethod
    def _episode_continues(ep: dict, onset: datetime, gaps: list[dict], n_sec: float,
                           end: datetime | None = None) -> bool:
        """Кластер с началом onset — продолжение точного эпизода (v5.1.21): until
        неизвестен (рестарт без записи), тот же онсет или начало не дальше N
        видимого времени от конца прежнего кластера. Часть до начала окна —
        астрономически (дыр до окна у цикла нет; ошибка не больше N, лишнего
        запроса нет). end — конец по карте окна (_exact_end, ревью v5.1.21):
        until памяти мог остаться горизонтом паузы или устареть за простой."""
        if ep.get("until") is None:
            return True
        u = end if end is not None else ep["until"]
        if onset <= u or onset == ep.get("t_open"):
            return True
        from analytics.live import visible_sec
        return visible_sec(u, onset, gaps) <= n_sec

    def _refine_until(self, segments: list) -> None:
        """until точных эпизодов памяти — по кластерам закрываемых сегментов
        (v5.1.21, ревью shadow-2): цепочка от кластера эпизода в окне
        (_analytics_cleared_at → _exact_end). После рестарта с простоем через
        09:00 until из JSON — последний цикл до остановки: кластер, шедший на
        резе, покрыт открытым эпизодом и не сеется, а перенос с 09:00 оказывался
        дальше until + N — разрез, хвост до реза терялся. Горизонт
        синтетического хвоста паузы — тоже не конец. До посева и сводки
        закрываемых сегментов; в памяти, без запросов. Сбой — until как был."""
        try:
            ca = _analytics_cleared_at(segments, self._episodes)
            for key, ep in self._episodes.items():
                _c = ca.get(key)
                if not ep.get("exact") or _c is None or ep.get("until_kind") == "plain":
                    continue
                _e = _exact_end(ep, _c)
                if _e is not None and _e != ep.get("until"):
                    logger.info("OnlineEngine[%s]: конец нарушения %s уточнён по кластерам окна: "
                                "%s → %s", self.key, key,
                                ep["until"].isoformat() if ep.get("until") else None, _e.isoformat())
                    ep["until"], ep["until_kind"] = _e, "end"
        except Exception:
            logger.warning("OnlineEngine[%s]: конец нарушения по кластерам окна не уточнён",
                           self.key, exc_info=True)

    async def _close_exact(self, key: str, ep: dict, t_close: datetime, gaps: list[dict],
                           gaps_from: datetime | None, cache: dict) -> bool:
        """Точное закрытие эпизода аналитики (v5.1.21, П5): t_close — конец
        нарушения (не раньше t_open), active_sec = t_close − t_open (без хвоста
        свежести и времени после конца), blind_sec — дыры внутри [t_open,
        t_close]: голова до окна — догрузкой разрывов (_gaps_back_to), сбой —
        накопленная слепая доля. False — строка не закрыта."""
        t_open = ep.get("t_open")
        t_close = _tz_utc(t_close)
        if t_open is not None and t_close < t_open:
            t_close = t_open
        blind: float | None = None
        if t_open is not None:
            try:
                g: list[dict] | None = gaps
                if gaps_from is not None and t_open < _tz_utc(gaps_from):
                    g = await self._gaps_back_to(t_open, gaps, gaps_from, cache)
                    _need = max(t_open, _tz_utc(gaps_from) - timedelta(days=30))
                    if cache.get("from") is None or cache["from"] > _need:
                        g = None   # разрывы до окна не догружены — оставить накопленное
                if g is not None:
                    from analytics.accumulators import gap_union_overlap_sec
                    blind = max(0.0, gap_union_overlap_sec(g, t_open, t_close))
            except Exception:
                logger.warning("OnlineEngine[%s]: слепая доля эпизода %s не пересчитана",
                               self.key, key, exc_info=True)
                blind = None
        try:
            await online_db.close_episode(ep["id"], t_close, "condition_cleared",
                                          exact=True, blind_sec=blind)
        except Exception:
            logger.warning("OnlineEngine[%s]: не удалось закрыть эпизод %s",
                           self.key, key, exc_info=True)
            return False
        return True

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
        supp_src: dict | None = None,
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
                # Точный эпизод, не живой на конец, — до конца нарушения (v5.1.21)
                eps = _drop_debounce_tails(eps, segments, self._episodes, live_keys or set())
                _syn = _live_new_episodes(segments, eps, live_keys or set(), _tz_utc(t_to),
                                          suppressed_sc, supp_src)
                eps = _drop_same_onset(eps, _syn) + _syn
            else:
                # Мост после реза (v5.1.21): точный эпизод, чьё нарушение кончилось
                # до окна, держится открытым до N — в данных окна его нет, в сводку
                # открытого сегмента не идёт (иначе «не подтверждено» на 10 мин)
                eps = [e for e in eps
                       if not _bridge_only(e, getattr(self, "_episodes", None), _tz_utc(t_from))]
            from analytics.serializer import build_summary_parts, render_summary_md
            # Имя кода панели — справочник KB (v5.1.23, В5): эпизоду, открытому до
            # v5.1.23, имя кода из детекции не досталось
            parts = build_summary_parts(segments, episodes=eps, tz=self.tz,
                                        trip_roles=self.cfg.trip_snapshot_roles,
                                        t_from=_tz_utc(t_from), t_to=_tz_utc(t_to),
                                        code_name=getattr(self.cfg, "fault_code_name", None))
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

    async def _refresh_sensor_steps(self, now: datetime) -> None:
        """Шаг показаний датчиков машины по истории 30 дней — раз в сутки (v5.1.12).

        Допуск «ниже паспорта в пределах точности датчика» (решение 12.10): у ДЭС
        №3 (QSX15 через ESM) сетка давления 8 кПа, у ДГУ №1/№2 — 4. Мало истории —
        остаётся умолчание контроллера из KB. Сбой чтения — тоже умолчание.
        """
        if (self._sensor_steps_at is not None
                and (now - self._sensor_steps_at).total_seconds() < 86400):
            return
        self._sensor_steps_at = now
        from analytics import source as _src
        from analytics.metrics import estimate_sensor_step
        for role in ("OIL_PRESS",):
            addr = self.cfg.role_to_addr(role)
            if not addr:
                continue
            try:
                hist = await _src.get_value_histogram(
                    self.router_sn, self.equip_type, self.panel_id, addr,
                    now - timedelta(days=30))
            except Exception:
                logger.warning("OnlineEngine[%s]: шаг датчика %s не оценён", self.key, role,
                               exc_info=True)
                continue
            step = estimate_sensor_step(hist)
            if step is None:
                continue
            if self.cfg.sensor_step(role) != (step, "history"):
                logger.info("OnlineEngine[%s]: шаг показаний %s по истории 30 дней — %g",
                            self.key, role, step)
            self.cfg.set_sensor_step(role, step, "history")

    async def _poll_cycle(self) -> None:
        now = datetime.now(timezone.utc)
        await self._refresh_sensor_steps(now)

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
        # live_to — докуда шёл живой цикл открытого окна (посев кластеров, v5.1.21)
        _live_to = self.last_processed_to
        for boundary in pending:
            await self._close_window(self.cursor_ts, boundary, "DAILY_BOUNDARY",
                                     lookahead_to=process_to, live_to=_live_to)

        # Открытое окно — всегда в конце (после всех границ или сразу если их нет)
        await self._update_open_window(self.cursor_ts, process_to)
        self.last_processed_to = process_to

    # ── Закрытие окна (DAILY_BOUNDARY) ────────────────────────────────────────

    async def _open_load_seed(self, ts_from: datetime) -> dict | None:
        """Удержанные значения перед окном — кэш на курсор (окно открытого сегмента
        начинается с одного и того же места, пока сегмент не закроется; рез
        закрывает окно с того же курсора — v5.1.21)."""
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
        lookahead_to: datetime | None = None,
        live_to: datetime | None = None,
    ) -> None:
        """Запустить полный анализ [t_from, t_to], сохранить все сегменты как закрытые.

        lookahead_to — докуда уже есть данные: если на суточной границе идёт
        неподтверждённая попытка пуска, её исход решается по данным за
        границей (v5.1.16). Иначе попытка за минуту до 09:00 оставалась бы
        «ждём подтверждения» в этом окне навсегда, и рез откладывался на весь
        следующий режим (ревью v5.1.16).

        live_to — докуда дошёл живой цикл открытого окна перед резом (v5.1.21,
        П6): кластер без эпизода, идущий на 09:00, не сеется — его продолжит
        перенос в новом окне. Живой цикл отстал больше чем на три цикла
        (догонка после простоя, рестарт) — сеется всё непокрытое (_seed_carry).
        """
        self._fill_coking_accum_end(t_from)
        if t_to <= t_from:
            return

        from analytics.runner import ANALYTICS_VERSION

        try:
            # Удержанные значения перед окном — те же, что у открытого окна этого
            # курсора: один запрос на курсор, а не на каждый прогон (v5.1.21)
            _seed = await self._open_load_seed(t_from)
            segments, incidents, gaps, chronologies = await _run_segment(
                self.router_sn, self.equip_type, self.panel_id, self.engine_sn,
                t_from, t_to, self.cfg,
                self.inherited_coking_risk,
                transition_ctx=self._transition_ctx,
                load_seed=_seed,
                prev_hint=self._prev_seg_hint,
            )
            if (close_reason == "DAILY_BOUNDARY" and segments
                    and getattr(segments[-1], "attempt_pending", False)
                    and lookahead_to is not None and _tz_utc(lookahead_to) > _tz_utc(t_to)):
                segments, incidents, gaps, chronologies = await _run_segment(
                    self.router_sn, self.equip_type, self.panel_id, self.engine_sn,
                    t_from, t_to, self.cfg,
                    self.inherited_coking_risk,
                    transition_ctx=self._transition_ctx,
                    confirm_to=min(_tz_utc(lookahead_to),
                                   _tz_utc(t_to) + timedelta(seconds=_CONFIRM_LOOKAHEAD_SEC)),
                    load_seed=_seed,
                    prev_hint=self._prev_seg_hint,
                )
        except Exception:
            logger.exception("OnlineEngine[%s]: ошибка анализа [%s, %s]", self.key, t_from, t_to)
            return

        # Снять открытую строку: удалить и получить снимок одним запросом (v5.1.13) —
        # запись гейта/аварии между чтением и удалением больше не теряется молча.
        # Характеристики — для верификации, записи гейта — разложим ниже
        _open_row = await online_db.pop_open_segment(
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

        # Записи гейта снятой строки — разложим по закрытым сегментам ниже
        _gate_state = _parse_gate_state(_open_row)

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

        # Конец нарушения точных эпизодов памяти — по кластерам закрываемого окна,
        # до посева и сводки (v5.1.21, ревью shadow-2: рестарт с простоем через рез)
        self._refine_until(segments)

        # Тревоги, активные на конец окна: живой эпизод по ним заведётся на
        # следующем цикле, поэтому «эфемерными» их считать нельзя
        _live_keys = _live_alert_keys(segments[-1])
        # Стоп между решением и сегментом (догонка) — снятие не переходит (ревью gate-3)
        try:
            _supp_sc = _gate_suppressed_scenarios(_gate_state, segments[-1], _open_row,
                                                  closed=segments)
        except Exception:
            logger.warning("OnlineEngine[%s]: снятые сценарии сводки не определены — без снятия",
                           self.key, exc_info=True)
            _supp_sc = set()
        # Уже засеянные в этом окне — одна тревога на несколько сегментов
        # должна дать один эпизод, а не по одному на сегмент
        _seeded: set[str] = set()
        # Закрытые этим циклом: (сегмент, id, начало, конец) — правило переноса
        # снятия проверяет их все (v5.1.22)
        _closed_info: list[tuple] = []

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
            # Неподтверждённый выход из стопа — тоже переходное (v5.1.16): иначе
            # попытку разрезало бы, и склеить её в плашку было бы уже нельзя
            if is_last and close_reason == "DAILY_BOUNDARY" and (
                    seg.run_state not in {0, 3} or getattr(seg, "attempt_pending", False)):
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
            # Кластер без эпизода, идущий на суточном резе «Работы», продолжит
            # перенос с 09:00 в новом окне — его не сеять (v5.1.21, П6); при
            # догонке живого цикла не было — сеять всё непокрытое
            _carry_end = _seed_carry(
                is_last and close_reason == "DAILY_BOUNDARY" and seg.run_state == 3,
                t_to, live_to, getattr(self, "poll_interval_sec", 30))
            _det_events = [] if _seg_no_data else await _collect_and_enrich_detections(
                seg, self.router_sn, self.equip_type, self.panel_id,
                seg_t_end, self.cfg, run_origin_ts=_run_origin,
                open_keys=set(self._episodes) | _live_keys | _seeded,
                seeded_out=_seeded, gaps=gaps, live_episodes=self._episodes,
                carry_end=_carry_end,
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
                    supp_src=_gate_own_src(_gate_state) if _supp_sc else None,
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
            _closed_info.append((seg, db_id, _tz_utc(datetime.fromisoformat(seg.t_start)),
                                 seg_t_end))
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
                _daily_st = _stop_ts_of(seg)
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
        # v5.1.22 (P1-1 п. 3): хеш снятия переходит, только если снятый эпизод
        # ещё идёт на границе и стопа между решением и границей не было
        # (_gate_carry); ни один сегмент не закрыт (рез пропущен в переходном
        # режиме) — строка та же, хеш остаётся
        if _seed_open_id:
            _tail = _slice_gate_state(_gate_state, last_committed_t_end)
            _carry = None
            if _gate_state.get("suppressed_hash"):
                if not _closed_info:
                    _tail["gate_suppressed_hash"] = _gate_state["suppressed_hash"]
                else:
                    try:
                        _last_closed = _closed_info[-1][0]
                        _open_side = None if _last_closed is segments[-1] else segments[-1]
                        _, _dets_end = _extract_open_segment_data(_last_closed)
                        _carry = _gate_carry(
                            _gate_state, _closed_info, self._episodes,
                            live_end=(_live_keys if _open_side is None
                                      else _live_alert_keys(_last_closed)),
                            live_open=_live_keys if _open_side is not None else None,
                            open_run_state=segments[-1].run_state,
                            boundary=last_committed_t_end, dets_end=_dets_end,
                            supp_sc=_supp_sc)
                    except Exception:
                        logger.warning("OnlineEngine[%s]: правило переноса снятия не проверено — "
                                       "хеш не переносится", self.key, exc_info=True)
                        _carry = None
                    if _carry:
                        _tail["gate_suppressed_hash"] = _carry["hash"]
                        if _carry.get("record"):
                            _tail["gate_log"] = list(_tail.get("gate_log") or []) + [_carry["record"]]
                    logger.info("OnlineEngine[%s]: снятие гейтом %s в новую строку %s",
                                self.key,
                                ("перенесено (эпизоды %s)" % _carry["ids"]) if _carry and _carry.get("ids")
                                else "остаётся" if _carry else "не переносится", _seed_open_id)
            try:
                await online_db.append_segment_gate_state(_seed_open_id, _tail)
                if _carry and _carry.get("ids") and _carry.get("segment_id"):
                    await online_db.set_episodes_gate_src_segment(_carry["ids"], _carry["segment_id"])
            except Exception:
                logger.warning("OnlineEngine[%s]: хвост записей гейта не восстановлен",
                               self.key, exc_info=True)

        self._open_history_cache = None
        self._open_history_cache_ts = None
        logger.debug(
            "OnlineEngine[%s]: закрыто %d сегментов до %s (%s)",
            self.key, len(segments), t_to, close_reason,
        )

    # ── Поздняя отметка обрыва сегмента аварией (v5.1.22, P1-3) ───────────────

    def _watch_trip(self, closed: list[tuple], segments: list) -> None:
        """Запомнить сегмент, закрытый сменой режима в ещё открытый стоп, если
        нарезка не отметила его обрыв аварией: аварийность стопа (40013, маска)
        может прийти кадром позже 40011. closed — [(сегмент, id, начало, конец)]
        этого цикла. По данным 60 дней 23 из 23 приходили одним кадром — это
        страховка, а не основной путь."""
        self._trip_watch = None
        if not closed or not segments:
            return
        seg, db_id, t0, t1 = closed[-1]
        open_seg = segments[-1]
        if (not db_id or seg is open_seg or getattr(seg, "run_state", 0) == 0
                or getattr(seg, "closed_by_trip", None)
                or getattr(seg, "cause_close", None) != "RUN_STATE_CHANGE"
                or getattr(open_seg, "run_state", None) != 0
                or getattr(open_seg, "attempt", None)
                or getattr(open_seg, "stop_kind", None) == "EMERGENCY"):
            return
        try:
            if _tz_utc(datetime.fromisoformat(open_seg.t_start)) != t1:
                return
        except (TypeError, ValueError):
            return
        self._trip_watch = {"id": db_id, "t_start": t0, "t_end": t1,
                            "run_state": int(seg.run_state)}

    async def _late_run_trip(self, segments: list, enum_periods: list[dict],
                             fault_periods: list[dict], t_to: datetime) -> None:
        """Стоп, в который закрылся отслеживаемый сегмент, стал аварийным —
        дописать в его закрытую сводку «… прервана аварийным остановом».

        Тем же правилом и той же подписью, что нарезка (segmenter._trip_mark /
        trip_record), по данным открытого окна — оно начинается ровно на конце
        закрытого сегмента, периоды 40012/40013 и маски уже загружены: запросов
        к БД нет, пока отметки нет. Запись — одна, как поздняя отмена гейтом;
        заключение перезаказывается при любой дописке (ревью verdict-6). Пока
        стоп простой — ждём (_trip_mark простому стопу отметку не даёт, ревью
        verdict-5).
        """
        w = getattr(self, "_trip_watch", None)
        if not w or not segments:
            return
        head = segments[0]
        try:
            h_start = _tz_utc(datetime.fromisoformat(head.t_start))
        except (TypeError, ValueError, AttributeError):
            self._trip_watch = None
            return
        if (h_start != w["t_end"] or getattr(head, "run_state", None) != 0
                or getattr(head, "attempt", None)
                or _tz_utc(t_to) > w["t_end"] + timedelta(seconds=_TRIP_WATCH_SEC)):
            self._trip_watch = None      # стоп сменился или ждать больше нечего
            return
        from analytics.segmenter import _state_periods, _trip_mark, shutdown_intervals
        try:
            iv = shutdown_intervals(fault_periods, self.cfg, _tz_utc(t_to), enum_periods)
            codes = (_state_periods(enum_periods, self.cfg)
                     .get(self.cfg.role_to_addr("LAST_FAULT_CODE")) or [])
            nxt = {"value": 0, "state_start": h_start,
                   "stop_kind": getattr(head, "stop_kind", None), "attempt": None}
            trip = _trip_mark(w["run_state"], w["t_start"], w["t_end"], iv, nxt, set(),
                              codes, fault_periods, self.cfg)
        except Exception:
            logger.warning("OnlineEngine[%s]: поздняя отметка обрыва аварией не проверена",
                           self.key, exc_info=True)
            return
        if not trip:
            return
        self._trip_watch = None
        try:
            changed, lvl_changed = await online_db.apply_late_run_trip(w["id"], trip, self.tz)
        except Exception:
            logger.warning("OnlineEngine[%s]: отметка обрыва аварией в сегмент %s не дописана",
                           self.key, w["id"], exc_info=True)
            return
        if changed:
            logger.info("OnlineEngine[%s]: сегмент %s оборван аварийным остановом — отметка "
                        "дописана поздно (аварийность стопа пришла после закрытия)%s — "
                        "заключение перезаказано", self.key, w["id"],
                        ", вердикт сменился" if lvl_changed else "")
            # v5.1.22, ревью verdict-6: перезаказ при любой дописке, а не только при
            # смене уровня — у CAUTION / WARNING уровень прежний, а заключение,
            # заказанное при закрытии, написано без факта обрыва и без запрета
            # «штатный» в шапке
            _enqueue_segment(w["id"], again=True)

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
                    # Открытое окно: ts_to — живой край (правило B ждёт следующего
                    # отсчёта только здесь; рез и перечитка — нет, ревью v5.1.21)
                    live_edge=True,
                    # «Работа» через суточный рез — профиль пуска предка: запрет
                    # снятия эпизода, начавшегося в окне пуска до реза (ревью R1)
                    start_carry=_start_carry(self._prev_seg_hint),
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

        # Аварийность стопа пришла после закрытия сегмента сменой режима — отметка
        # обрыва дописывается в его закрытую сводку (v5.1.22, P1-3)
        if getattr(self, "_trip_watch", None):
            try:
                await self._late_run_trip(segments, enum_periods, fault_periods, ts_to_utc)
            except Exception:
                logger.warning("OnlineEngine[%s]: поздняя отметка обрыва аварией", self.key,
                               exc_info=True)

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
        # Закрытые этим циклом: (сегмент, id, начало, конец) — для правила
        # переноса снятия (v5.1.22)
        _rs_closed_info: list[tuple] = []
        _win_plates = _attempt_stops(segments)
        if closed_segs:
            # Снять открытую строку одним запросом (v5.1.13, см. _close_window)
            _rs_open_row = await online_db.pop_open_segment(
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

            # Записи гейта снятой строки — разложим по закрытым сегментам ниже
            _rs_gate_state = _parse_gate_state(_rs_open_row)
            # Первый закрытый сегмент в окне может быть продолжением после DAILY_BOUNDARY
            carry_continued_from = self.continued_from_id
            # Подсказка предка головы окна — цикл ниже перезаписывает _prev_seg_hint
            _prev_hint_rs0 = self._prev_seg_hint
            # Унаследованное RS время: ci==0 продолжает цепочку, ci>0 — новый RS
            _rs_inherited_before = dict(self.inherited_run_state_sec)
            # Конец нарушения точных эпизодов памяти — по кластерам окна, до посева
            # и сводки закрываемых сегментов (v5.1.21, ревью shadow-2)
            self._refine_until(segments)
            # Тревоги, активные в остающемся открытом сегменте: по ним ниже в
            # этом же цикле заведётся живой эпизод (память движка отстаёт)
            _live_keys = _live_alert_keys(segments[-1])
            # Новые тревоги закрываемого сегмента, продолжающиеся в открытом, —
            # по составу на конце последнего закрытого (до v5.1.22 — по
            # остающемуся открытому, то есть уже по новому режиму после пуска);
            # синтетика строится только по живым в открытом — пересечение
            try:
                _rs_supp_sc = _gate_suppressed_scenarios(_rs_gate_state, closed_segs[-1], _rs_open_row,
                                                         closed=closed_segs)
            except Exception:
                logger.warning("OnlineEngine[%s]: снятые сценарии сводки не определены — без снятия",
                               self.key, exc_info=True)
                _rs_supp_sc = set()
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
                    seeded_out=_seeded, gaps=gaps, live_episodes=self._episodes,
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
                        supp_src=_gate_own_src(_rs_gate_state) if _rs_supp_sc else None,
                    )

                _rs_seg_dict = seg.to_dict()
                _rs_incident = None
                _rs_chronology = None
                # Пустой сегмент не исключение — см. суточный путь
                if seg.run_state == 0:
                    try:
                        from analytics.reconstructor import (
                            build_segment_chronology as _chrono,
                        )
                        _rs_st = _tz_utc(datetime.fromisoformat(seg.t_start))
                        # Момент останова: у плашки неудачной попытки — падение
                        _rs_stop = _stop_ts_of(seg)
                        # Акт собран один раз и не пересобирается: если он уже
                        # лежит на закрываемой открытой строке — переносим как
                        # есть. Пересборка дала бы тот же документ, но «один
                        # раз» должно значить буквально один раз.
                        _rs_incident = _carry_incident(_rs_open_row, _rs_st)
                        # Акт — только у головы стопа: продолжение после
                        # суточного реза ссылается на неё через continued_from
                        if (_rs_incident is None
                                and getattr(seg, "stop_kind", None) == "EMERGENCY"
                                and _is_stop_head(enum_periods, _rs_stop)):
                            # Общий построитель (v5.1.22): сегмент падения —
                            # предыдущий закрываемый или подсказка предка
                            _rs_incident = await _build_act(
                                self.router_sn, self.equip_type, self.panel_id,
                                seg, seg_t_end_rs, ts_to_utc, self.cfg,
                                tl_enum=tl_enum, fault_periods=fault_periods,
                                tl_faults=tl_faults, skip_stops=_win_plates,
                                segments=closed_segs, prev=ps_rs, key=self.key,
                            )
                            # Авария короче лага: акта на открытой строке не
                            # было, значит разбор заказывается отсюда
                            if _rs_incident is not None:
                                self._act_done_for = _rs_stop
                                self._order_incident_analysis(_rs_incident, _rs_stop)
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
                _rs_closed_info.append((seg, _rs_db_id,
                                        _tz_utc(datetime.fromisoformat(seg.t_start)), seg_t_end_rs))
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

            # Закрыт сменой режима в ещё не аварийный стоп без отметки обрыва —
            # ждать аварийности стопа (v5.1.22, P1-3)
            try:
                self._watch_trip(_rs_closed_info, segments)
            except Exception:
                logger.warning("OnlineEngine[%s]: ожидание отметки обрыва не заведено",
                               self.key, exc_info=True)

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

        # Эпизоды тревог — ДО обогащения: счётчики уже включат текущий эпизод.
        # Конец нарушения: у панели — снятие (fault_end), у аналитики — конец
        # кластера (v5.1.21); режим открытого сегмента — для моста
        try:
            _, _raw_curr_dets = _extract_open_segment_data(open_seg)
            await self._process_episodes(_raw_curr_dets, t_to, gaps,
                                         cleared_at={**_panel_cleared_at(segments),
                                                     **_analytics_cleared_at(segments, self._episodes)},
                                         gaps_from=ts_from_utc,
                                         run_state=open_seg.run_state,
                                         start_carry=getattr(open_seg, "start_profile_prev", None))
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
        # Начало эпизода у просадки и заброса, идущих мостом через рез, — в
        # активные тревоги строки: запрет снятия гейтом и статус (ревью v5.1.23, R1);
        # только у «Работы», несущей профиль предка через рез
        try:
            _since = _carry_onset_floor(getattr(open_seg, "start_profile_prev", None))
            if _since is not None:
                _mark_episode_onsets(_curr_dets, self._episodes, since=_since)
        except Exception:
            logger.warning("OnlineEngine[%s]: начало эпизодов оборотов не отмечено", self.key,
                           exc_info=True)
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
                from analytics.reconstructor import (
                    build_segment_chronology as _chrono,
                )
                _op_st = _tz_utc(datetime.fromisoformat(open_seg.t_start))
                # Момент останова: у плашки неудачной попытки — падение (v5.1.16)
                _op_stop = _stop_ts_of(open_seg)
                _lag_done = ts_to_utc >= _op_stop + timedelta(
                    seconds=_stab_sec(self.cfg)
                )
                if (getattr(open_seg, "stop_kind", None) == "EMERGENCY"
                        and _lag_done
                        and self._act_done_for != _op_stop
                        and _is_stop_head(enum_periods, _op_stop)):
                    # Общий построитель (v5.1.22): окно начинается со стопа —
                    # сегмент падения закрыт прошлым циклом, это подсказка предка
                    _open_incident = await _build_act(
                        self.router_sn, self.equip_type, self.panel_id,
                        open_seg, None, ts_to_utc, self.cfg,
                        tl_enum=tl_enum, fault_periods=fault_periods, tl_faults=tl_faults,
                        skip_stops=_attempt_stops(segments), segments=segments,
                        prev=self._prev_seg_hint, key=self.key,
                    )
                    if _open_incident is not None:
                        self._act_done_for = _op_stop
                        self._order_incident_analysis(_open_incident, _op_stop)
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

        # Хвост записей гейта удалённой строки — в новую открытую (см. _close_window).
        # v5.1.22 (P1-1 п. 3, В5): хеш снятия — только если снятый эпизод идёт и
        # на конце последнего закрытого, и в остающемся открытом, а стопа между
        # решением и границей нет (смена режима без стопа — как суточный рез)
        if _rs_gate_state is not None and _open_seg_id:
            _rs_tail = _slice_gate_state(_rs_gate_state, self.cursor_ts)
            _rs_carry_st = None
            if _rs_gate_state.get("suppressed_hash"):
                try:
                    _rs_last = _rs_closed_info[-1][0] if _rs_closed_info else closed_segs[-1]
                    _, _rs_dets_end = _extract_open_segment_data(_rs_last)
                    _rs_carry_st = _gate_carry(
                        _rs_gate_state, _rs_closed_info, self._episodes,
                        live_end=_live_alert_keys(_rs_last), live_open=_live_keys,
                        open_run_state=open_seg.run_state,
                        boundary=self.cursor_ts, dets_end=_rs_dets_end,
                        supp_sc=_rs_supp_sc)
                except Exception:
                    logger.warning("OnlineEngine[%s]: правило переноса снятия не проверено — "
                                   "хеш не переносится", self.key, exc_info=True)
                    _rs_carry_st = None
                if _rs_carry_st:
                    _rs_tail["gate_suppressed_hash"] = _rs_carry_st["hash"]
                    if _rs_carry_st.get("record"):
                        _rs_tail["gate_log"] = (list(_rs_tail.get("gate_log") or [])
                                                + [_rs_carry_st["record"]])
                logger.info("OnlineEngine[%s]: снятие гейтом %s в новую строку %s",
                            self.key,
                            ("перенесено (эпизоды %s)" % _rs_carry_st["ids"])
                            if _rs_carry_st and _rs_carry_st.get("ids")
                            else "остаётся" if _rs_carry_st else "не переносится", _open_seg_id)
            try:
                await online_db.append_segment_gate_state(_open_seg_id, _rs_tail)
                if _rs_carry_st and _rs_carry_st.get("ids") and _rs_carry_st.get("segment_id"):
                    await online_db.set_episodes_gate_src_segment(
                        _rs_carry_st["ids"], _rs_carry_st["segment_id"])
            except Exception:
                logger.warning("OnlineEngine[%s]: хвост записей гейта не восстановлен",
                               self.key, exc_info=True)

        # Отложенная проверка снятия гейтом новых эпизодов (см. _process_episodes)
        if self._supp_recheck:
            _sc, _dets = self._supp_recheck
            self._supp_recheck = None
            await self._inherit_gate_suppression(_sc, _dets, ts=t_to)

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
