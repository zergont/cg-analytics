"""Очередь и воркер для анализа сегментов (Этап 2).

Singleton-воркер запускается в main.py lifespan.
Интеграция:
  - online/engine.py: enqueue(seg_id) после insert_closed_segment()
  - web/routes.py:    enqueue(seg_id, PRIORITY_MANUAL) по кнопке
  - main.py:          enqueue_pending() при старте (исторический batch)

Провайдер и промпт для каждого запроса берутся из llm.router:
  PRIORITY_NORMAL  → task_id="seg_auto"
  PRIORITY_MANUAL  → task_id="seg_manual"
"""
from __future__ import annotations
import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Приоритеты (меньше = выше приоритет в PriorityQueue)
PRIORITY_MANUAL = 0   # ручной запуск из UI
PRIORITY_NORMAL  = 1   # авто-закрытие сегмента + исторический batch


class AnalysisWorker:
    def __init__(self) -> None:
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        # Ждут в очереди: id → лучший приоритет. Один сегмент — один разбор:
        # старт ставит и незаконченные, и новые закрытия, тумблер — ещё раз;
        # без этого сегмент мог уйти в модель дважды (с советом — надолго)
        self._pending: dict[int, int] = {}
        self._current: int | None = None
        self._running: bool = False
        self._task: asyncio.Task | None = None

    # ── Очередь ──────────────────────────────────────────────────────────────

    def enqueue(self, seg_id: int, priority: int = PRIORITY_NORMAL, force: bool = False,
                again: bool = False) -> None:
        """Добавить сегмент в очередь на анализ.

        force=True разрешает обработку открытых сегментов (t_end IS NULL).
        Автоматически выставляется при priority=PRIORITY_MANUAL.
        again=True — повторный заказ: ставится и тогда, когда сегмент в работе
        (вердикт сменился после того, как заключение начало готовиться).
        """
        _force = force or (priority == PRIORITY_MANUAL)
        task_id = "seg_manual" if priority == PRIORITY_MANUAL else "seg_auto"
        best = self._pending.get(seg_id)
        if best is not None and best <= priority:
            logger.debug("corpus/worker: #%d уже в очереди — пропуск", seg_id)
            return
        if seg_id == self._current and priority != PRIORITY_MANUAL and not again:
            logger.debug("corpus/worker: #%d сейчас в работе — пропуск", seg_id)
            return
        self._pending[seg_id] = priority
        self._queue.put_nowait((priority, seg_id, _force, task_id))
        logger.debug("corpus/worker: enqueue #%d (p=%d force=%s task=%s)",
                     seg_id, priority, _force, task_id)

    async def enqueue_pending(self) -> int:
        """Batch: закрытые сегменты без анализа и незаконченные → NORMAL очередь.

        Незаконченные — строки «в очереди» / «в работе»: очередь живёт в памяти,
        и после перезапуска их никто не подбирал, сегмент вечно «готовился».
        """
        from corpus.db import get_unanalyzed_segments
        seg_ids = await get_unanalyzed_segments()
        for seg_id in seg_ids:
            self.enqueue(seg_id, PRIORITY_NORMAL)
        if seg_ids:
            logger.info(
                "corpus/worker: batch-старт — %d сегментов добавлено в очередь",
                len(seg_ids),
            )
        return len(seg_ids)

    # ── Основной цикл ─────────────────────────────────────────────────────────

    async def run(self) -> None:
        self._running = True
        logger.info("corpus/worker: запущен")

        while self._running:
            try:
                _, seg_id, _force, _task_id = await asyncio.wait_for(
                    self._queue.get(), timeout=5.0
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            # Дубль, уже разобранный по записи с лучшим приоритетом, — пропуск
            if self._pending.pop(seg_id, None) is None:
                self._queue.task_done()
                continue
            self._current = seg_id
            try:
                await _process_segment(seg_id, force=_force, task_id=_task_id)
            except Exception:
                logger.exception(
                    "corpus/worker: необработанная ошибка при анализе #%d", seg_id
                )
            finally:
                self._current = None
                self._queue.task_done()

        logger.info("corpus/worker: остановлен")

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    def get_status(self) -> dict[str, Any]:
        return {
            "running":            self._running,
            "processing_seg_id":  self._current,
            "queue_size":         self._queue.qsize(),
        }


# ── Обработка одного сегмента ─────────────────────────────────────────────────

async def _process_segment(
    seg_id: int,
    force: bool = False,
    task_id: str = "seg_auto",
) -> None:
    """Полный цикл обработки: анализ → сохранение в БД.

    Промпт — из llm.router по task_id; кто пишет — строка таблицы «Кто и как
    разбирает» по виду разбора, при отказе — запасные (цепочка реестра).
    force=True — разрешить анализ открытого сегмента (ручной запуск).
    """
    import corpus.db as corpus_db
    from llm.router import get_prompt

    try:
        from analytics.runner import ANALYTICS_VERSION
    except Exception:
        ANALYTICS_VERSION = "3.0.0"

    logger.info("corpus/worker: начинаю #%d (task=%s)", seg_id, task_id)

    await corpus_db.set_status(seg_id, "processing")

    seg_row = await corpus_db.get_segment_row(seg_id)
    if not seg_row:
        await corpus_db.set_status(seg_id, "error", "Сегмент не найден в БД")
        return

    if seg_row.get("t_end") is None and not force:
        await corpus_db.set_status(seg_id, "error", "Сегмент ещё открытый (t_end IS NULL)")
        return
    if seg_row.get("t_end") is None:
        logger.info("corpus/worker: сегмент #%d открытый — ручной запуск (force=True)", seg_id)

    # Неудачная попытка выхода из стопа (v5.1.16): заключения модели нет —
    # машина не вышла на работу, кроме фактов разбирать нечего (решение
    # владельца 13.10.2026). Отказ пуска с аварией разбирает совет по акту
    if not force:
        _facts = _attempt_conclusion(seg_row)
        _what = "неудачная попытка пуска"
        if _facts is None:
            # Аварийный стоп с актом (v5.1.22, P1-5, решение В1): заключение —
            # фактами, причину разбирает совет по акту. ИИ — по кнопке «Анализ»
            # (force), без акта и когда совет не получен (отметка failed /
            # save_failed в журнале); строка таблицы — «Система (без ИИ)»
            _facts = await _shutdown_facts_if_due(seg_row, task_id)
            _what = "аварийный стоп"
        if _facts is not None:
            await corpus_db.upsert_analysis(seg_id, {**_facts, "status": "done",
                                                     "analytics_version": ANALYTICS_VERSION})
            logger.info("corpus #%d: %s — факты без модели", seg_id, _what)
            return

    kb_path = await corpus_db.get_equipment_kb_path(
        seg_row["router_sn"], seg_row["equip_type"], seg_row["panel_id"]
    )

    prompt = get_prompt(task_id)
    depth = _segment_depth(seg_row, task_id)

    # Основной — строка таблицы (v5.1.11); цепочка реестра — запасные при отказе.
    # До v5.1.11 основного выбирала голова цепочки, а модель шлюза — градация:
    # две настройки на одно решение. Запись, совпадающая с основным, пропускается —
    # шлюз дважды не зовём (она остаётся в цепочке ради отката на v5.1.10)
    entries = _route_entries(task_id, depth)
    provider = entries[0]["name"] + (f" +{len(entries) - 1} запасн." if len(entries) > 1 else "")
    result = await _analyse_segment_chain(seg_row, kb_path, prompt, entries, depth=depth)

    if result["error"]:
        logger.error("corpus ERROR #%d | %s", seg_id, result["error"])
        await corpus_db.upsert_analysis(seg_id, {**result, "status": "error"})
        return

    await corpus_db.upsert_analysis(seg_id, {
        **result,
        "status":            "done",
        "humanized_md":      None,
        "analytics_version": ANALYTICS_VERSION,
    })

    from db.analytics import get_app_setting
    qwen_auto = await get_app_setting("qwen_auto_analyze", "false")
    if qwen_auto == "true":
        from corpus.qwen_worker import get_worker as get_qwen_worker
        qwen_w = get_qwen_worker()
        if qwen_w:
            qwen_w.enqueue(seg_id, task_id="human_auto")
            logger.debug("corpus/worker: сегмент #%d → очередь хуманизации", seg_id)

    logger.info(
        "corpus OK #%d | provider=%s | вердикт=%s | уровень=%s | "
        "токены=%d | тулы=%d | %.1fс",
        seg_id, provider,
        result.get("verdict", "—"),
        result.get("alarm_level", "—"),
        result.get("tokens_used", 0),
        result.get("tool_calls_count", 0),
        result.get("generation_time_sec", 0),
    )


def _system_signature() -> str:
    """Подпись заключения-фактов: местное время с поясом (v5.1.22, P1-6)."""
    from datetime import datetime as _dt, timezone as _tzu
    from analytics.labels import SYSTEM_MODEL, _local_tz, tz_label
    now = _dt.now(_tzu.utc)
    z = _local_tz()
    return f"*{SYSTEM_MODEL} · {(now.astimezone(z) if z else now).strftime('%d.%m.%Y %H:%M')} {tz_label()}*"


def _attempt_conclusion(seg_row: dict) -> dict | None:
    """Заключение-факты для плашки неудачной попытки; None — не попытка.

    v5.1.22 (P1-6): время останова и подписи — местное с поясом («Останов
    09:27:43 МСК»), было — сырой ISO в UTC «2026-10-16T06:27:43+00:00»."""
    import json as _json
    from analytics.labels import SYSTEM_MODEL, attempt_facts_ru, attempt_label, fmt_local, tz_label
    chars = seg_row.get("characteristics_json")
    if isinstance(chars, str):
        try:
            chars = _json.loads(chars)
        except ValueError:
            return None
    att = chars.get("attempt") if isinstance(chars, dict) else None
    if not isinstance(att, dict):
        return None
    emergency = att.get("outcome") == "EMERGENCY"
    # Коды самой попытки (сегментатор): агрегат плашки подхватил бы и код,
    # висевший до попытки, и кнопку на стоящей машине через час (ревью v5.1.16)
    codes = [int(c) for c in att.get("codes") or []]
    _f = fmt_local(att.get("t_fall")) if att.get("t_fall") else None
    fall = f"{_f} {tz_label()}" if _f else (att.get("t_fall") or "—")
    short = (f"{attempt_label(att)}: выход из стопа не подтвердился — "
             f"{attempt_facts_ru(att)}. Останов {fall}"
             + (f", код панели {', '.join(map(str, codes))}" if codes else "") + ".")
    md = "\n".join([
        "## Сводка",
        short,
        "",
        "Заключение модели для неудачной попытки не пишется: машина не вышла на "
        "работу, разбирать нечего, кроме фактов выше. Отказ пуска с аварией "
        "разбирает совет шлюза по акту аварии.",
        "",
        "---",
        _system_signature(),
    ])
    # Уровень — тот же, что у обычного пути: в стопе после попытки бывают и
    # предупреждения панели (ревью v5.1.16)
    try:
        verdict, level = _verdict_fields(seg_row)
    except Exception:
        verdict, level = None, None
    if not level:
        verdict, level = (("авария", "SHUTDOWN") if emergency else ("норма", "НОРМА"))
    return {
        "conclusion_md": md,
        "humanized_md": short,
        "verdict": verdict,
        "alarm_level": level,
        "claude_model": SYSTEM_MODEL,
        "tokens_used": 0, "tool_calls_count": 0, "loops_count": 0,
        "generation_time_sec": 0,
        "debug_json": {"entry": "attempt_facts", "depth": "attempt"},
    }


# ── Заключение аварийного стопа фактами (v5.1.22, P1-5, решение В1) ──────────
#
# 16.10 оба ИИ-заключения по стопам ДГУ №1 утверждали то, чего нет в данных:
# «под нагрузкой» при разомкнутом автомате, «размыкает выключатель», порог
# «48 Гц», «冷却», «680 ч». Как у отказов пуска (13.10): заключение — факты из
# акта и сводки, причину разбирает совет по акту. Включается строкой «🔴 авария
# панели» таблицы «Кто и как разбирает» = «Система (без ИИ)» (разовая установка
# при деплое, вернуть ИИ — в таблице).

def _parse(v: Any) -> Any:
    import json as _json
    if isinstance(v, str):
        try:
            return _json.loads(v)
        except ValueError:
            return None
    return v


def _council_failed(seg_row: dict, inc: dict) -> bool:
    """Совет по этой аварии не получен: в журнале гейта терминальная отметка
    failed / save_failed (incident_gate._run_incident) — заключение пишет ИИ."""
    from datetime import datetime as _dt
    from online.incident_gate import INC_FAILED, INC_SAVE_FAILED, incident_hash
    try:
        fh = incident_hash(_dt.fromisoformat(inc["stop_ts"]))
    except (KeyError, TypeError, ValueError):
        return False
    for e in _parse(seg_row.get("gate_log")) or []:
        if (isinstance(e, dict) and e.get("kind") == "incident" and e.get("fault_hash") == fh
                and e.get("decision") in (INC_FAILED, INC_SAVE_FAILED)):
            return True
    return False


def _act_code_names(inc: dict) -> dict[int, str]:
    """Названия кодов 40012 из ленты акта (метки каталога БД телеметрии)."""
    from analytics.labels import split_code_label
    out: dict[int, str] = {}
    for e in inc.get("chronology") or []:
        if e.get("role") != "LAST_FAULT_CODE" or e.get("value") in (None, 0):
            continue
        try:
            c = int(e["value"])
        except (TypeError, ValueError):
            continue
        name, _ = split_code_label(e.get("label"))
        if c > 0 and name and c not in out:
            out[c] = name
    return out


def _stop_code(inc: dict, parts: dict | None) -> tuple[int | None, str | None, str | None]:
    """(код, название, тип) аварии: часть сводки fault_code (P1-2), иначе первый
    код 40012 ленты акта в кадре останова (±15 с)."""
    from datetime import datetime as _dt
    from analytics.labels import split_code_label
    fc = (parts or {}).get("fault_code")
    if isinstance(fc, dict) and fc.get("code"):
        return int(fc["code"]), fc.get("name"), fc.get("kind")
    try:
        stop = _dt.fromisoformat(inc["stop_ts"])
    except (KeyError, TypeError, ValueError):
        return None, None, None
    for e in inc.get("chronology") or []:
        if e.get("role") != "LAST_FAULT_CODE" or e.get("value") in (None, 0, 65535):
            continue
        try:
            if abs((_dt.fromisoformat(e["ts"]) - stop).total_seconds()) > 15:
                continue
            name, kind = split_code_label(e.get("label"))
            return int(e["value"]), name, kind
        except (KeyError, TypeError, ValueError):
            continue
    return None, None, None


def _shutdown_conclusion(seg_row: dict, cycle_segments: list[dict] | None = None,
                         tz=None) -> dict | None:
    """Заключение аварийного стопа фактами без ИИ (v5.1.22, P1-5, решение В1);
    None — акта нет (тогда пишет ИИ).

    Факты — каждый из одного источника: время останова (акт, местное), код и
    его подпись (часть сводки fault_code / лента акта), из какого режима и через
    сколько после пуска (акт: fall, chain_start), «Авария снята в … (через N с)»
    (часть mttr — по маске) и что стало с кодом 40012 (часть fault_code, P1-2),
    до останова — нагрузка, ток, автомат, ряд оборотов и частоты (лента
    отсчётов акта, fall_facts_ru), предыдущие аварии серии (series_facts по
    сегментам цикла), наработка (счётчик панели). Голос 40599 «EmergencyStop» не
    упоминается. Поля записи — как у плашек (_attempt_conclusion).
    """
    from datetime import datetime as _dt
    from analytics.labels import (
        SYSTEM_MODEL, engine_hours_text, fmt_duration, fmt_local, tz_label,
    )
    from analytics.reconstructor import fall_facts_ru
    from analytics.serializer import RUN_STATE_RU
    from corpus.preprocessor import _engine_hours_of
    from online.incident_gate import series_facts

    inc = _parse(seg_row.get("incident_json"))
    if not isinstance(inc, dict) or not inc.get("stop_ts"):
        return None
    parts = _parse(seg_row.get("report_summary_json"))
    parts = parts if isinstance(parts, dict) and parts.get("version") else None
    tzl = tz_label(tz)
    stop_raw = inc["stop_ts"]
    stop_t = fmt_local(stop_raw, tz, day=True) or stop_raw

    code, name, kind = _stop_code(inc, parts)
    code_txt = (f"код панели {code}" + (f" «{name}»" if name else "")
                + (f" ({kind})" if kind else "")) if code else "код панели не записан"

    fall = inc.get("fall") if isinstance(inc.get("fall"), dict) else {}
    mode = fall.get("label")
    if not mode:
        rs = fall.get("run_state")
        if rs is None:
            rs = ((inc.get("character") or {}).get("signals") or {}).get("run_state_before")
        mode = RUN_STATE_RU.get(rs) if rs is not None else None
    since_start = None
    try:
        if fall.get("chain_start"):
            since_start = (_dt.fromisoformat(stop_raw)
                           - _dt.fromisoformat(fall["chain_start"])).total_seconds()
    except (TypeError, ValueError):
        since_start = None

    facts: list[str] = [f"Останов: {stop_t} {tzl}, {code_txt}."]
    if mode:
        line = f"Из какого режима: «{mode}»"
        if fall.get("t_start"):
            line += (f" {fmt_local(fall['t_start'], tz) or ''}–"
                     f"{fmt_local(fall.get('t_end') or stop_raw, tz) or ''}")
        if since_start is not None and since_start >= 0:
            line += (f", через {fmt_duration(since_start)} после пуска (выход из стопа в "
                     f"{fmt_local(fall['chain_start'], tz)})")
        facts.append(line + ".")
    m = (parts or {}).get("mttr")
    if isinstance(m, dict) and m.get("kind") == "SHUTDOWN_CLEARED" and seg_row.get("t_end"):
        from analytics.labels import shutdown_cleared_how
        facts.append(f"Авария снята в {fmt_local(seg_row['t_end'], tz)} (через "
                     f"{fmt_duration(m.get('sec'))}) — {shutdown_cleared_how(m)}.")
    elif seg_row.get("t_end") is None:
        facts.append("Авария на момент заключения не снята.")
    fc = (parts or {}).get("fault_code")
    if isinstance(fc, dict) and (fc.get("text_regs") or fc.get("text")):
        facts.append(f"{fc.get('text_regs') or fc.get('text')}.")
    trace = inc.get("trace") if isinstance(inc.get("trace"), dict) else None
    ff = fall_facts_ru(trace, tz) if trace else None
    facts.append(ff or "До останова: ленты отсчётов в акте нет (акт до v5.1.22 или "
                       "история окна недоступна) — параметры сегмента падения в отчёте и акте.")
    try:
        names = _act_code_names(inc)
        win = inc.get("window") or {}
        anchor = win.get("anchor") if win.get("baseline") != "fallback" else None
        ser = series_facts(cycle_segments or [], stop_raw, anchor,
                           lambda c: names.get(int(c)), tz=tz) if cycle_segments is not None else None
    except Exception:
        logger.warning("corpus/worker: серия аварий для заключения не собрана", exc_info=True)
        ser = None
    if ser:
        facts.append("Предыдущие аварии серии после штатного останова:")
        facts += [f"  {x.strip()[2:] if x.strip().startswith('- ') else x.strip()}" for x in ser]
    elif ser is not None:
        facts.append("Предыдущих аварий после штатного останова нет.")
    eh = engine_hours_text(_engine_hours_of(seg_row.get("characteristics_json"),
                                            seg_row.get("analytics_version")))
    facts.append(f"Наработка двигателя на конец периода: {eh} (счётчик панели)." if eh
                 else "Наработка двигателя: нет в данных.")
    facts.append("Справочник кода (описание и порядок проверки по руководству) — в отчёте сегмента.")

    short = (f"Аварийный останов {stop_t} {tzl}: {code_txt}"
             + (f", из режима «{mode}»" if mode else "")
             + (f" через {fmt_duration(since_start)} после пуска"
                if since_start is not None and since_start >= 0 else "") + ".")
    if isinstance(m, dict) and m.get("kind") == "SHUTDOWN_CLEARED" and seg_row.get("t_end"):
        short += f" Авария снята через {fmt_duration(m.get('sec'))}."
    md = "\n".join(["## Сводка", short, "", "## Факты"]
                   + [f"- {x}" if not x.startswith("  ") else f"  - {x.strip()}" for x in facts]
                   + ["",
                      "Заключение аварийного стопа — факты без ИИ. Причину разбирает совет ИИ "
                      "по акту аварии — разбор появится в карточке.",
                      "", "---", _system_signature()])
    try:
        verdict, level = _verdict_fields(seg_row)
    except Exception:
        verdict, level = None, None
    if not level:
        verdict, level = "авария", "SHUTDOWN"
    return {
        "conclusion_md": md,
        "humanized_md": short,
        "verdict": verdict,
        "alarm_level": level,
        "claude_model": SYSTEM_MODEL,
        "tokens_used": 0, "tool_calls_count": 0, "loops_count": 0,
        "generation_time_sec": 0,
        "debug_json": {"entry": "shutdown_facts", "depth": "seg_shutdown",
                       "investigator_version": inc.get("investigator_version"),
                       "trace": trace is not None},
    }


async def _shutdown_facts_if_due(seg_row: dict, task_id: str) -> dict | None:
    """Заключение фактами, если положено (v5.1.22, P1-5); None — пишет ИИ.

    Положено: строка «🔴 авария панели» — «Система (без ИИ)», вид разбора
    seg_shutdown, у сегмента есть акт, совет по нему не закончился неудачей.
    Сегменты цикла для серии — один запрос (воркер, не горячий цикл); сбой —
    без строки серии.
    """
    from llm.router import SYSTEM_PROVIDER, get_route
    try:
        if get_route("seg_shutdown").get("provider") != SYSTEM_PROVIDER:
            return None
        if _segment_depth(seg_row, task_id) != "seg_shutdown":
            return None
        inc = _parse(seg_row.get("incident_json"))
        if not isinstance(inc, dict) or not inc.get("stop_ts"):
            # Акта нет (сбой построения, продолжение после реза) — пишет ИИ
            return None
        if _council_failed(seg_row, inc):
            logger.info("corpus #%s: совет по аварии не получен — заключение пишет ИИ",
                        seg_row.get("id"))
            return None
    except Exception:
        logger.warning("corpus/worker: проверка заключения фактами не выполнена — пишет ИИ",
                       exc_info=True)
        return None
    cycle: list[dict] | None = None
    try:
        from datetime import datetime as _dt
        from online import db as online_db
        win_from = (inc.get("window") or {}).get("from")
        if win_from:
            cycle = await online_db.get_segments_between(
                seg_row["router_sn"], seg_row["equip_type"], seg_row["panel_id"],
                _dt.fromisoformat(win_from), _dt.fromisoformat(inc["stop_ts"]))
    except Exception:
        logger.warning("corpus/worker: сегменты цикла для заключения не прочитаны", exc_info=True)
        cycle = None
    try:
        return _shutdown_conclusion(seg_row, cycle)
    except Exception:
        logger.warning("corpus/worker: заключение фактами не собрано — пишет ИИ", exc_info=True)
        return None


async def _lang_clean(text: str, source: str, depth: str | None) -> tuple[str, dict | None]:
    """Проход «очистка / перевод» заключения (v5.1.22, решение В4): один запрос к
    быстрой модели, только если найден чужой язык; приоритет — строки вида."""
    try:
        from llm.lang_check import clean_text
        from llm.router import get_depth
        prio = get_depth(depth).get("priority") if depth else None
        return await clean_text(text, source, priority=prio)
    except Exception:
        logger.warning("corpus/worker: проверка языка не выполнена", exc_info=True)
        return text, None


def _segment_depth(seg_row: dict, task_id: str) -> str:
    """Вид разбора для градации: ручной запуск — «manual», иначе по уровню тревоги.

    Уровень — тот же детерминированный вердикт, что уходит модели в шапке
    («Уровень тревоги»): сегмент без происшествий простой, авария — нет.
    """
    from llm.router import depth_for_segment
    if task_id == "seg_manual":
        return "manual"
    try:
        from corpus.preprocessor import extract_verdict_alarm
        _, level = extract_verdict_alarm(seg_row)
    except Exception:
        level = None
    return depth_for_segment(level)


def _route_head(depth: str) -> dict[str, Any]:
    """Основной исполнитель заключения — строка таблицы «Кто и как разбирает».

    api — Claude-агент с моделью строки (пусто — модель карточки Claude); llm —
    общее подключение (карточка «Горыныч», llm.client._cfg): модель, рассуждение
    и приоритет шлюзу задаёт та же строка через depth (resolve_depth).
    Запись в формате реестра — чтобы цепочка обрабатывала её как любую другую.
    «Система (без ИИ)» (v5.1.22, только seg_shutdown): заключение пишет ИИ лишь
    там, где фактов нет или их мало — без акта, совет не получен, — и тогда это
    общее подключение с моделью строки (явно, как llm).
    """
    from llm.client import get_llm_settings
    from llm.registry import DEFAULT_MAX_CTX
    from llm.router import claude_model_for, get_route
    if get_route(depth).get("provider") == "api":
        return {"id": "route:api", "name": "Claude API", "type": "api",
                "provider": "anthropic", "model": claude_model_for(depth),
                "max_ctx_tokens": DEFAULT_MAX_CTX["anthropic"], "_route": True}
    cfg = get_llm_settings()
    prov = cfg.get("provider") or "ollama"
    max_ctx = (int(cfg.get("num_ctx") or 16384) if prov == "ollama"
               else DEFAULT_MAX_CTX.get(prov, 32768))
    return {"id": "route:llm", "name": "Горыныч" if prov == "gorynych" else "LLM",
            "type": "llm", "provider": prov, "base_url": cfg.get("base_url", ""),
            "model": cfg.get("model", ""), "max_ctx_tokens": max_ctx,
            "stream": True if prov == "gorynych" else cfg.get("stream", True),
            "_global": True}


def _same_as_head(entry: dict, head: dict) -> bool:
    """Запасная совпадает с основным: шлюз и Claude — по провайдеру (у каждого
    одно подключение), прочие серверы — по адресу и модели."""
    if entry.get("provider") != head.get("provider"):
        return False
    if head.get("provider") in ("gorynych", "anthropic"):
        return True
    return ((entry.get("base_url") or "").rstrip("/") == (head.get("base_url") or "").rstrip("/")
            and entry.get("model") == head.get("model"))


def _route_entries(task_id: str, depth: str) -> list[dict[str, Any]]:
    """[основной из таблицы] + запасные цепочки задачи без совпадающих с основным."""
    from llm.registry import get_entry
    from llm.router import get_chain
    head = _route_head(depth)
    return [head] + [e for e in (get_entry(eid) for eid in get_chain(task_id))
                     if e and not _same_as_head(e, head)]


def _llm_result_fields(meta: dict, fallback_model: str) -> dict[str, Any]:
    """Подпись, отладка и токены по реально ответившей модели.

    У шлюза ответить могла не та голова, что заказана (подмена при долгом
    ожидании или когда промпт не влез) — подписываем её, а task_id и причину
    подмены кладём в debug_json, чтобы найти запрос в журнале шлюза.
    """
    from llm.client import gateway_label
    g = meta.get("gorynych") or {}
    usage = meta.get("usage") or {}
    label = gateway_label(meta, fallback_model)
    out: dict[str, Any] = {
        "label":       label,
        "tokens_used": int(usage.get("total_tokens") or 0),
        "debug":       {},
        "claude_model": None,
    }
    if g:
        out["debug"]["gorynych"] = g
        out["claude_model"] = f"Горыныч: {label}"
    if usage:
        out["debug"]["usage"] = usage
    return out


def _error_detail(exc: Exception) -> dict[str, Any]:
    """Запись следа цепочки об ошибке: у ошибки шлюза — код и task_id отдельно."""
    from llm.client import LLMError
    rec: dict[str, Any] = {"detail": repr(exc)[:300]}
    if isinstance(exc, LLMError):
        rec.update({"http_status": exc.status, "code": exc.code, "task_id": exc.task_id})
    return rec


class EmptyLLMResponse(Exception):
    """Модель вернула пустой текст — это ошибка, а не готовое заключение."""


def _model_input(seg_row: dict) -> str:
    """Вход заключения — один для всех провайдеров (build_claude_input).

    Шапка единого вердикта (части сводки, v5.1.0), акт аварии, разборы гейта,
    хронология стоянки и отчёт. До v5.1.2 модели шлюза и локальные получали
    голый report_md — шапка доходила только до claude-api, последнего в
    цепочке, а 276 из 285 заключений за месяц писал шлюз: «45 предупреждений»
    из старой таблицы отчёта при вердикте «3 замечания, 9 снято ИИ».
    """
    try:
        from corpus.preprocessor import build_claude_input
        return build_claude_input(seg_row)
    except Exception:
        logger.warning("corpus/worker: вход заключения #%s не собран — отдаю отчёт",
                       seg_row.get("id"), exc_info=True)
        return seg_row.get("report_md") or ""


def _verdict_fields(seg_row: dict) -> tuple[str | None, str | None]:
    """(вердикт, уровень) для корпуса — тот же расчёт, что в шапке входа.

    Пути llm и цепочки писали «LLM» и NULL: календарь .194 рисовал зелёный
    бейдж «Claude: норма» у сегмента с замечаниями.
    """
    try:
        from corpus.preprocessor import extract_verdict_alarm
        return extract_verdict_alarm(seg_row)
    except Exception:
        logger.warning("corpus/worker: вердикт #%s не определён", seg_row.get("id"),
                       exc_info=True)
        return None, None


# ── Цепочка приоритетов моделей ───────────────────────────────────────────────

# Оценка размера промпта: по ответам шлюза русский текст с markdown-таблицами —
# 1,75–2,1 символа на токен (21 разбор, 12.10); прежние 3 занижали вход в 1,4–1,7
# раза — выбор модели по окну мог отправить его в модель с меньшим контекстом
_EST_CHARS_PER_TOKEN = 1.7
# Резерв контекста под ответ модели
_RESPONSE_RESERVE_TOKENS = 4000


async def _analyse_segment_chain(
    seg_row: dict,
    kb_path: str | None,
    system_prompt: str,
    entries: list[dict],
    depth: str | None = None,
) -> dict[str, Any]:
    """Анализ по цепочке приоритетов: проактивный отбор по размеру + fallback по ошибкам.

    Для каждой записи цепочки по порядку (первая — основной из таблицы, _route_head):
      1. промпт не влезает в max_ctx_tokens → пропуск без запроса;
      2. type="api"  → Claude-агент с инструментами (у основного — модель строки);
         type="llm"  → plain chat через запись реестра (стрим по настройке записи),
                       у основного — через общее подключение;
      3. ошибка запроса (сеть после ретраев, 4xx сразу) → следующая запись;
         пустой ответ — тоже ошибка: сохранять пустое заключение как готовое
         нельзя, повторно его уже никто не возьмёт.
    depth — вид разбора: у записей шлюза задаёт модель, рассуждение и приоритет.
    След маршрутизации пишется в debug_json.routing.
    """
    import time
    from llm.client import chat

    user_input = _model_input(seg_row)
    verdict, alarm_level = _verdict_fields(seg_row)
    est_tokens = int((len(system_prompt) + len(user_input)) / _EST_CHARS_PER_TOKEN) \
                 + _RESPONSE_RESERVE_TOKENS
    trace: list[dict] = []

    for entry in entries:
        if est_tokens > entry["max_ctx_tokens"]:
            trace.append({"entry": entry["id"],
                          "action": "skip_size",
                          "detail": f"~{est_tokens} ток. > лимита {entry['max_ctx_tokens']}"})
            logger.info("corpus/chain: #%s → «%s» пропущена по размеру (~%d > %d ток.)",
                        seg_row.get("id"), entry["name"], est_tokens, entry["max_ctx_tokens"])
            continue

        t0 = time.monotonic()
        try:
            if entry["type"] == "api":
                from corpus.agent import analyse_segment
                result = await analyse_segment(
                    seg_row, kb_path, system_prompt=system_prompt,
                    model=entry.get("model") if entry.get("_route") else None)
                if result.get("error"):
                    trace.append({"entry": entry["id"], "action": "error",
                                  "detail": str(result["error"])[:300]})
                    continue
                # Чужой язык (В4) — один проход очистки, без повтора и смены модели
                _cm, _lang = await _lang_clean(result.get("conclusion_md") or "", user_input, depth)
                if _lang:
                    result["conclusion_md"] = _cm
                    result["debug_json"] = {**(result.get("debug_json") or {}), "lang_check": _lang}
            else:
                from llm.router import format_ai_signature
                meta: dict = {}
                content = await chat(system_prompt, user_input,
                                     entry=None if entry.get("_global") else entry,
                                     stream=entry.get("stream", True),
                                     depth=depth, meta=meta)
                if not content:
                    raise EmptyLLMResponse("модель вернула пустой ответ")
                f = _llm_result_fields(meta, entry["model"])
                # Чужой язык в ответе (иероглифы, английские фразы; v5.1.22, В4):
                # один проход «очистка / перевод» быстрой моделью — до подписи
                content, _lang = await _lang_clean(content, user_input, depth)
                if _lang:
                    f["debug"]["lang_check"] = _lang
                content += format_ai_signature(f["label"])
                result = {
                    "verdict":            verdict,
                    "alarm_level":        alarm_level,
                    "conclusion_md":      content,
                    "error":              None,
                    "tokens_used":        f["tokens_used"],
                    "tool_calls_count":   0,
                    "loops_count":        0,
                    "generation_time_sec": round(time.monotonic() - t0, 1),
                    "debug_json":         {"provider": entry["provider"], "entry": entry["id"],
                                           "depth": depth, "input_length": len(user_input),
                                           **f["debug"]},
                    "claude_model":       f["claude_model"] or f"{entry['name']} ({entry['model']})",
                }
            trace.append({"entry": entry["id"], "action": "ok"})
            dbg = result.get("debug_json") or {}
            dbg["routing"] = trace
            dbg["est_prompt_tokens"] = est_tokens
            result["debug_json"] = dbg
            return result
        except Exception as exc:
            # сеть/429/5xx уже отретраены внутри chat(); сюда доходят
            # исчерпанные ретраи и 4xx — в обоих случаях идём к следующей модели
            trace.append({"entry": entry["id"], "action": "error", **_error_detail(exc)})
            logger.warning("corpus/chain: #%s → «%s» ошибка, перехожу дальше: %r",
                           seg_row.get("id"), entry["name"], exc)

    detail = "; ".join(f"{t['entry']}: {t['action']}" for t in trace) or "цепочка пуста"
    return {
        "verdict": None, "alarm_level": None, "conclusion_md": "",
        "error": f"Ни одна модель цепочки не обработала сегмент "
                 f"(промпт ~{est_tokens} ток.): {detail}",
        "tokens_used": 0, "tool_calls_count": 0, "loops_count": 0,
        "generation_time_sec": 0,
        "debug_json": {"routing": trace, "est_prompt_tokens": est_tokens},
        "claude_model": None,
    }


# ── Singleton ─────────────────────────────────────────────────────────────────

_worker: AnalysisWorker | None = None


def init_worker() -> AnalysisWorker:
    global _worker
    _worker = AnalysisWorker()
    return _worker


def get_worker() -> AnalysisWorker | None:
    return _worker
