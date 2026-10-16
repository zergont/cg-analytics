# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Детерминированный сборщик структурного статуса машины.

Публичный API:
  build_structural_status(seg_row, fault_ref, tz) → dict
  compute_status_hash(struct_status) → str
  compute_fault_hash(struct_status) → str
  compute_severity_level(active_dets) → str
  format_status_text(struct_status) → str
  build_warning_prompt(struct_status) → str
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any

from analytics.contract import cap_severity

logger = logging.getLogger(__name__)

# ── Константы ─────────────────────────────────────────────────────────────────

# Вид стоп-сегмента поверх словаря режимов (v4.9.67)
_STOP_KIND_LABELS: dict[str, str] = {"EMERGENCY": "Аварийный стоп"}

_RUN_STATE_LABELS: dict[int, str] = {
    0: "Стоп",
    1: "Задержка пуска",
    2: "Прогрев",
    3: "Работа",
    4: "Разгрузка",
    5: "Охлаждение на х.х.",
    6: "Переход на х.х.",
}

# Ключевые роли параметров для каждого режима (2-3 значимых)
_KEY_ROLES: dict[int, list[str]] = {
    0: [],
    1: ["RPM", "BATTERY_V"],
    2: ["COOLANT_TEMP", "RPM"],
    3: ["LOAD_PCT", "COOLANT_TEMP", "OIL_PRESS"],
    4: ["ACTIVE_POWER_TOTAL", "COOLANT_TEMP"],
    5: ["COOLANT_TEMP", "RPM"],
    6: ["COOLANT_TEMP", "RPM"],
}

# Ранг severity от панели управления (CONTROLLER_FAULT)
_PANEL_SEV_RANK: dict[str, int] = {"SHUTDOWN": 3, "WARNING": 2}

# Общий ранг для сортировки итогового уровня
# предупреждение (аналитика) < внимание (панель WARNING) < авария (панель SHUTDOWN)
_OVERALL_RANK: dict[str, int] = {
    "авария":         4,
    "внимание":       3,
    "предупреждение": 2,
    "норма":          1,
}


# ── Вспомогательные ───────────────────────────────────────────────────────────

def _parse_json(val: Any) -> Any:
    if val is None:
        return None
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            return None
    return val


def _blind_note(blind_sec: Any) -> str:
    """Оговорка про слепое время: « (из них X без связи)» либо пусто.

    Воздействие считается астрономически и в дыре связи идёт — залипшая
    неисправность висит, пока её не сбросят. Но сброс мог произойти внутри
    слепого куска, и читающий должен понимать, что число — верхняя оценка.
    """
    try:
        v = float(blind_sec or 0)
    except (TypeError, ValueError):
        return ""
    return f" (из них {_fmt_duration(v)} без связи)" if v >= 1 else ""


def _fmt_duration(sec: float) -> str:
    """Как в UI, без секунд от минуты: строка пишется раз в тик (v5.1.20)."""
    from analytics.labels import fmt_duration
    return fmt_duration(sec, seconds=False)


# ── Публичный API ─────────────────────────────────────────────────────────────

def compute_panel_severity(active_dets: list[dict]) -> str:
    """Уровень по сигналам панели управления (scenario=CONTROLLER_FAULT).

    норма / внимание (WARNING) / авария (SHUTDOWN)
    """
    panel = [d for d in active_dets if d.get("scenario") == "CONTROLLER_FAULT"]
    if not panel:
        return "норма"
    rank = max((_PANEL_SEV_RANK.get(d.get("severity", ""), 0) for d in panel), default=0)
    if rank >= 3:
        return "авария"
    if rank >= 2:
        return "внимание"
    return "норма"


def compute_analytics_severity(active_dets: list[dict]) -> str:
    """Уровень по детекциям нашего аналитического движка (все кроме CONTROLLER_FAULT).

    норма / предупреждение
    """
    analytics = [d for d in active_dets if d.get("scenario") != "CONTROLLER_FAULT"]
    return "предупреждение" if analytics else "норма"


def compute_severity_level(active_dets: list[dict]) -> str:
    """Итоговый уровень — максимум из панели и аналитики.

    норма < предупреждение < внимание < авария
    """
    panel     = compute_panel_severity(active_dets)
    analytics = compute_analytics_severity(active_dets)
    return max(panel, analytics, key=lambda x: _OVERALL_RANK.get(x, 0))


def _hash_codes(d: dict) -> tuple:
    """Подключ состава гейта: у аналитики — справочные коды (стабильны, различают
    параметр LIMIT_PROXIMITY), у панели — её код. Старые детекции без ref_codes дают
    то же, что раньше: у них fault_codes и были справочными (v5.1.6).
    Нарушение паспорта отличается от «в пределах шага датчика» (v5.1.12): иначе
    разбор и снятие прежнего состава перенеслись бы на нарушение."""
    if d.get("scenario") != "CONTROLLER_FAULT" and d.get("ref_codes"):
        key = tuple(sorted(d["ref_codes"]))
    else:
        key = tuple(sorted(d.get("fault_codes") or []))
    return key + ("passport",) if passport_hard(d) else key


# Режимные замечания — факт по времени, модель его не опровергнет: гейт их не
# разбирает, не снимает и соседей не блокирует (решение владельца 12.10, как
# нагар и события перехода). В сводке и уровне — живые замечания
GATE_EXCLUDED_SCENARIOS = frozenset({"NO_LOAD_TOO_LONG", "IDLE_TOO_LONG"})
# Версия правил гейта: разборы и снятия по старым правилам не повторяются и не
# применяются заново (перепроверка один раз, v5.1.12). 5.1.15: тренд ОЖ убран —
# у снятого ложного тренда и близости к уставке один ключ состава
# (COOLING_FAILURE, 144), старое снятие глушило бы настоящий перегрев
GATE_RULES_VERSION = "5.1.15"


def _limit(d: dict) -> dict:
    return d.get("limit") or (d.get("values") or {}).get("limit") or {}


def passport_hard(d: dict) -> bool:
    """Нарушение паспорта двигателя дальше шага датчика — гейт не снимает (v5.1.12)."""
    lim = _limit(d) if isinstance(d, dict) else {}
    return lim.get("kind") == "passport" and lim.get("hard") is True


def level_dets(dets: list[dict], suppressed: bool) -> list[dict]:
    """Детекции для уровня: при действующем снятии — панель и режимные
    замечания (их гейт не снимает). Одно правило для статуса, API и карточки."""
    if not suppressed:
        return list(dets or [])
    return [d for d in dets or [] if isinstance(d, dict) and (
        d.get("scenario") == "CONTROLLER_FAULT" or d.get("scenario") in GATE_EXCLUDED_SCENARIOS)]


def passport_hard_seen(d: dict) -> bool:
    """В подсегменте было нарушение паспорта (живое или давнее): эпизод с ним
    снятым гейтом не помечается — снятие сниженного «в пределах шага» состава
    действует на статус, но не стирает факт нарушения из сводки (ревью v5.1.12)."""
    lim = _limit(d) if isinstance(d, dict) else {}
    return lim.get("kind") == "passport" and (lim.get("hard") is True or bool(lim.get("hard_until")))


def gate_dets(dets: list[dict]) -> list[dict]:
    """Состав для гейта: без режимных замечаний."""
    return [d for d in dets or [] if not (
        isinstance(d, dict) and d.get("scenario") in GATE_EXCLUDED_SCENARIOS)]


def compute_analytics_hash(dets: list[dict]) -> str:
    """Хэш состава аналитических детекций (без панельных) — ключ подавления гейта.

    Подавление действует, пока состав не изменился: новая аналитическая
    детекция меняет хэш, и вердикт Claude теряет силу.
    """
    from analytics.contract import EVENT_SCENARIOS
    analytics = [d for d in gate_dets(dets)
                 if isinstance(d, dict) and d.get("scenario") != "CONTROLLER_FAULT"
                 and d.get("scenario") not in EVENT_SCENARIOS]   # событие не в составе гейта
    key = tuple(sorted(
        (d.get("scenario", ""), _hash_codes(d))
        for d in analytics
    ))
    return hashlib.md5(str(key).encode()).hexdigest()[:12]


# Ранний сигнал несимметрии — предвестник отказа на стороне трансформатора или
# нагрузки (Сининда 29.09: провал фазы и ток нейтрали за 2,5 ч до отключения).
# Решение владельца 11.10: гейт его НЕ снимает, только пропускает с пояснением.
# Снятие у гейта — по составу целиком, поэтому состав с несимметрией не снимается
# весь; снимется, когда несимметрия уйдёт и состав сменится.
UNCANCELLABLE_SCENARIOS = frozenset({
    "NEGATIVE_SEQUENCE", "PHASE_CURRENT_SPREAD", "NEUTRAL_CURRENT",
})


def has_uncancellable(dets: list[dict]) -> bool:
    """Есть ли в составе сигнал, который гейт снимать не вправе: несимметрия или
    нарушение паспорта двигателя (v5.1.12)."""
    return any(isinstance(d, dict) and (d.get("scenario") in UNCANCELLABLE_SCENARIOS
                                        or passport_hard(d))
               for d in dets or [])


def uncancellable_reasons(s: dict) -> list[dict]:
    """Почему гейт не вправе снять состав: [{code, text}] (пусто — вправе).

    panel — сигнал панели WARNING/SHUTDOWN; panel_fresh — «Общая авария» с кодом,
    появившаяся в этом сегменте (решение 12.10: висевшая на начало — сведения);
    asymmetry — несимметрия (v5.0.1); passport — нарушение паспорта (v5.1.12).
    """
    out: list[dict] = []
    if s.get("panel_severity", "норма") != "норма":
        out.append({"code": "panel", "text": "активны сигналы панели управления"})
    for p in s.get("panel_alarms") or []:
        if p.get("severity") == "CAUTION" and p.get("fresh"):
            codes = ", ".join(str(c) for c in p.get("fault_codes") or [])
            out.append({"code": "panel_fresh",
                        "text": f"«Общая авария» с кодом {codes} появилась в этом сегменте"})
    alarms = s.get("analytics_alarms") or []
    if any(a.get("scenario") in UNCANCELLABLE_SCENARIOS for a in alarms):
        out.append({"code": "asymmetry", "text": "в составе ранний сигнал несимметрии"})
    for i, a in enumerate(alarms, 1):
        if passport_hard(a):
            out.append({"code": "passport", "text": f"A{i}: нарушение паспорта двигателя"})
    return out


def gate_can_cancel(s: dict) -> bool:
    """Может ли гейт снять состав (причин запрета нет)."""
    return not uncancellable_reasons(s)


def is_analytics_suppressed(seg_row: dict, active_dets: list[dict]) -> bool:
    """Действует ли вердикт гейта «отменить» для текущего состава аналитических детекций."""
    suppressed = seg_row.get("gate_suppressed_hash")
    if not suppressed:
        return False
    # Риск нагара в составе гейта не участвует (v5.1.3) — и в сверке хеша тоже;
    # режимные замечания — тоже (v5.1.12)
    active_dets = gate_dets(_without_state(active_dets))
    # Вердикт, поставленный до правила (или на составе, совпавшем по хешу), не
    # гасит несимметрию
    if has_uncancellable(active_dets):
        return False
    has_analytics = any(
        isinstance(d, dict) and d.get("scenario") != "CONTROLLER_FAULT" for d in active_dets
    )
    return has_analytics and compute_analytics_hash(active_dets) == suppressed


# ── Снятие гейтом: запреты, предикат, источник (v5.1.22, P1-1) ───────────────

# Виды записей журнала гейта (v5.1.22): разбор моделью (llm — шлюз или
# локальная, api — Claude API), повтор снятого в этой строке состава (repeat),
# наследование снятия новым эпизодом в той же строке (inherited), перенос
# продолжающегося снятого эпизода в новую строку (carry), поздний вердикт,
# дописанный в текущую строку (late). Своё решение строки — llm / api / repeat:
# только оно даёт основание повтору (_cancelled_hashes) и наследованию; carry и
# late защищают только продолжающиеся эпизоды, новые от них не наследуют.
GATE_MODEL_KINDS = frozenset({"llm", "api"})
GATE_OWN_KINDS = frozenset({"llm", "api", "repeat"})
GATE_CARRY_KINDS = frozenset({"carry", "late"})


def gate_entry_kind(e: dict) -> str | None:
    """Вид записи журнала гейта. Старые записи без ключа kind — по provider:
    «carried» до v5.1.22 писал поздний вердикт (→ late), repeat — повтор,
    api — Claude API, остальное — разбор моделью шлюза (llm)."""
    if not isinstance(e, dict):
        return None
    if e.get("kind"):
        return e["kind"]
    prov = e.get("provider")
    if prov == "carried":
        return "late"
    if prov in ("repeat", "api"):
        return prov
    return "llm"


def _applied_cancel(e: dict) -> bool:
    return isinstance(e, dict) and e.get("decision") == "cancel" and bool(e.get("decision_applied"))


def _record_source(rec: dict, gate_log: list) -> dict:
    """Источник записи: исходный разбор. У repeat — разбор моделью с тем же
    составом (#35891: повтор 09:41 → разбор 09:01:59), у carry/late/inherited —
    записанный в ней источник."""
    src = rec.get("source")
    if isinstance(src, dict) and src.get("ts"):
        return {"ts": src.get("ts"), "segment_id": src.get("segment_id")}
    if gate_entry_kind(rec) == "repeat":
        for e in gate_log or []:
            if (gate_entry_kind(e) in GATE_MODEL_KINDS and _applied_cancel(e)
                    and e.get("fault_hash") and e.get("fault_hash") == rec.get("fault_hash")):
                return {"ts": e.get("ts"), "segment_id": None}
    return {"ts": rec.get("ts"), "segment_id": None}


def gate_decision_source(gate_log: list | None, suppressed_hash: str | None,
                         kinds: frozenset = GATE_OWN_KINDS) -> dict | None:
    """Источник действующего снятия строки по её журналу (v5.1.22).

    kinds = GATE_OWN_KINDS — своё решение строки (llm / api / repeat): последняя
    применённая запись с этим suppressed_hash; у старых записей без
    suppressed_hash — последнее применённое снятие строки, если оно и есть
    последнее (по нему и стоял хеш). kinds = GATE_CARRY_KINDS — перенос или
    поздний вердикт с этим хешем. Возвращает {ts, segment_id, fault_hash, kind}
    или None — основания нет.
    """
    if not suppressed_hash:
        return None
    log = [e for e in gate_log or [] if isinstance(e, dict)]
    recs = [e for e in log if gate_entry_kind(e) in kinds and _applied_cancel(e)]
    match = [e for e in recs if e.get("suppressed_hash") == suppressed_hash]
    if not match and kinds is GATE_OWN_KINDS and recs and "suppressed_hash" not in recs[-1]:
        match = [recs[-1]]
    if not match:
        return None
    rec = match[-1]
    return {**_record_source(rec, log), "fault_hash": rec.get("fault_hash"),
            "kind": gate_entry_kind(rec)}


def gate_hash_source(gate_log: list | None, fault_hash: str | None) -> dict | None:
    """Исходный разбор состава fault_hash в этой строке — источник повтора
    (repeat, v5.1.22): первый применённый разбор моделью с этим составом, иначе
    источник прежнего повтора. None — разбора в строке нет."""
    if not fault_hash:
        return None
    log = [e for e in gate_log or [] if isinstance(e, dict)]
    for e in log:
        if (gate_entry_kind(e) in GATE_MODEL_KINDS and _applied_cancel(e)
                and e.get("fault_hash") == fault_hash):
            return {**_record_source(e, log), "fault_hash": fault_hash}
    for e in reversed(log):
        if (gate_entry_kind(e) == "repeat" and _applied_cancel(e)
                and e.get("fault_hash") == fault_hash):
            return {**_record_source(e, log), "fault_hash": fault_hash}
    return None


def gate_record_episodes(gate_log: list | None, suppressed_hash: str | None) -> set[int] | None:
    """id эпизодов, снятых записями строки с этим хешем (своё решение, повтор,
    наследование, перенос, поздний вердикт — v5.1.22). None — ни одна такая
    запись не несёт списка эпизодов (журнал до v5.1.22)."""
    if not suppressed_hash:
        return None
    ids: set[int] = set()
    seen = False
    for e in gate_log or []:
        if not (isinstance(e, dict) and _applied_cancel(e)
                and gate_entry_kind(e) in (GATE_OWN_KINDS | GATE_CARRY_KINDS | {"inherited"})):
            continue
        if e.get("suppressed_hash") != suppressed_hash or not isinstance(e.get("episodes"), list):
            continue
        seen = True
        for i in e["episodes"]:
            try:
                ids.add(int(i))
            except (TypeError, ValueError):
                pass
    return ids if seen else None


def _caution_fresh(since_raw, fault_codes, seg_t0, code_at_start) -> bool:
    """«Общая авария» с кодом появилась в этом сегменте (решение 12.10): фронт
    не раньше начала строки или код сменился с начала сегмента."""
    since = _as_dt(since_raw)
    try:
        code_changed = bool(code_at_start is not None and fault_codes
                            and int(fault_codes[0]) != code_at_start)
    except (TypeError, ValueError):
        code_changed = False
    return since is None or seg_t0 is None or since >= seg_t0 or code_changed


def suppression_bans(seg_row: dict, dets: list[dict]) -> list[str]:
    """Запреты действия снятия по детекциям и виду стопа (v5.1.22): panel —
    сигнал панели WARNING/SHUTDOWN, panel_fresh — свежая «Общая авария» с кодом,
    emergency — аварийный стоп. Несимметрию и паспорт проверяет
    is_analytics_suppressed. Тот же расчёт свежести, что у статуса."""
    chars = _parse_json(seg_row.get("characteristics_json")) or {}
    out: list[str] = []
    if isinstance(chars, dict) and chars.get("stop_kind") == "EMERGENCY":
        out.append("emergency")
    seg_t0 = _as_dt(seg_row.get("t_start"))
    code0 = _code_at_segment_start(chars if isinstance(chars, dict) else {})
    for d in dets or []:
        if not isinstance(d, dict) or d.get("scenario") != "CONTROLLER_FAULT":
            continue
        sev = d.get("severity")
        if sev in ("WARNING", "SHUTDOWN"):
            code = "panel"
        elif sev == "CAUTION" and _caution_fresh((d.get("values") or {}).get("fault_start"),
                                                 d.get("fault_codes") or [], seg_t0, code0):
            code = "panel_fresh"
        else:
            continue
        if code not in out:
            out.append(code)
    return out


def suppression_active(seg_row: dict, active_dets: list[dict]) -> bool:
    """Действует ли снятие гейтом сейчас (v5.1.22): хеш состава совпал и запретов
    нет (сигнал панели, свежая «Общая авария», аварийный стоп, несимметрия,
    паспорт). Один предикат у статуса, наследования эпизодом и пометки новых
    тревог в сводке. is_analytics_suppressed (без запретов панели) остаётся для
    закрытых сегментов."""
    if not is_analytics_suppressed(seg_row, active_dets):
        return False
    return not suppression_bans(seg_row, active_dets)


def _without_state(dets: list[dict]) -> list[dict]:
    """Без сценариев-состояний (риск нагара) и разовых событий перехода: это не
    тревога, гейт их не разбирает (v5.1.3, v5.1.10)."""
    from analytics.contract import EVENT_SCENARIOS, STATE_SCENARIOS
    return [d for d in dets or [] if not (
        isinstance(d, dict) and (d.get("scenario") in STATE_SCENARIOS
                                 or d.get("scenario") in EVENT_SCENARIOS))]


_COKING_LEVEL_RU = {"GREEN": "низкий", "YELLOW": "повышенный", "RED": "высокий"}


def _coking_state(seg_row: dict) -> dict | None:
    """Накопленный риск нагара из coking_risk_json — для строки «Состояние»."""
    raw = _parse_json(seg_row.get("coking_risk_json"))
    if not isinstance(raw, dict):
        return None
    return {k: raw.get(k) for k in (
        "risk_level", "low_load_zone_sec", "idle_low_rpm_sec", "coolant_below_60_sec",
        "debt_sec", "last_purge_ts", "last_accum_ts", "purge_left_low_min",
        "purge_left_high_min", "purge_low_pct", "purge_high_pct",
        "service_needed", "last_monthly_run_ts", "last_annual_run_ts", "calendar_since_ts")}


def build_structural_status(
    seg_row: dict,
    fault_ref=None,
    tz=None,
    origin_ts=None,
) -> dict[str, Any]:
    """Собрать структурный статус из строки открытого сегмента.

    Args:
        seg_row:   строка из auto_segments (dict)
        fault_ref: FaultRef для расшифровки кодов тревог (может быть None)
        tz:        часовой пояс (зарезервирован)
        origin_ts: реальное начало режима (datetime) — перекрывает t_start сегмента;
                   нужно когда текущий сегмент создан суточным срезом, а режим
                   начался раньше (цепочка continued_from)

    Returns:
        dict с полями:
          run_state, mode_label, load_pct, load_pct_bucket,
          severity_level, panel_severity, analytics_severity,
          time_in_mode_sec, panel_alarms[], analytics_alarms[], key_params[]
    """
    run_state: int = seg_row.get("run_state") or 0
    # Разбор поднят сюда из блока времени в режиме: вид стопа нужен для подписи
    chars        = _parse_json(seg_row.get("characteristics_json")) or {}
    _stop_kind   = chars.get("stop_kind")
    # Неудачная попытка выхода из стопа и неподтверждённый выход (v5.1.16)
    from analytics.labels import label_from_chars
    mode_label = (label_from_chars(chars)
                  or _STOP_KIND_LABELS.get(_stop_kind or "")
                  or _RUN_STATE_LABELS.get(run_state, f"RUN_STATE={run_state}"))

    current_vals = _parse_json(seg_row.get("current_values_json")) or {}
    # Риск нагара — накопленное состояние, а не тревога (v5.1.3): в уровень,
    # состав гейта и тревоги не идёт, у него своя часть строки «Состояние»
    active_dets  = _without_state(_parse_json(seg_row.get("active_detections_json")) or [])
    values: dict = current_vals.get("values") or {}

    # ── Нагрузка ──
    load_pct: float | None = None
    if "LOAD_PCT" in values:
        raw = values["LOAD_PCT"].get("value")
        if raw is not None:
            try:
                load_pct = float(raw)
            except (TypeError, ValueError):
                pass
    load_pct_bucket = (int(load_pct // 10) * 10) if load_pct is not None else None

    # ── Время в режиме ──
    # Берём из _total_run_state_sec в characteristics_json — аналитика уже посчитала
    # накопленное время через всю цепочку суточных резов (inherited_run_state_sec + duration).
    # Fallback: wallclock от t_start (для первого тика, когда chars ещё нет).
    total_rs_sec = chars.get("_total_run_state_sec") or {}
    # JSON ключи — строки; сравниваем и str и int
    time_in_mode_sec = float(
        total_rs_sec.get(run_state) or total_rs_sec.get(str(run_state)) or 0.0
    )
    if not time_in_mode_sec:
        raw_ts = origin_ts or seg_row.get("t_start")
        if raw_ts:
            now_utc = datetime.now(timezone.utc)
            if isinstance(raw_ts, datetime):
                ts = raw_ts if raw_ts.tzinfo else raw_ts.replace(tzinfo=timezone.utc)
            else:
                try:
                    ts = datetime.fromisoformat(str(raw_ts))
                    if not ts.tzinfo:
                        ts = ts.replace(tzinfo=timezone.utc)
                except Exception:
                    ts = now_utc
            time_in_mode_sec = max(0.0, (now_utc - ts).total_seconds())

    # ── Severity (раздельно по источнику) ──
    # Вердикт гейта «отменить» подавляет аналитику, пока состав детекций не изменился.
    # Режимные замечания гейт не снимает — в уровне они остаются (v5.1.12).
    # v5.1.22: при сигнале панели, свежей «Общей аварии» и аварийном стопе снятие
    # не действует — тот же предикат, что у наследования эпизодом и сводки
    suppressed    = suppression_active(seg_row, active_dets)
    dets_for_level = level_dets(active_dets, suppressed)
    panel_sev     = compute_panel_severity(active_dets)
    analytics_sev = compute_analytics_severity(dets_for_level)
    sev_level     = compute_severity_level(dets_for_level)
    # Аварийный стоп — авария панели, даже когда в детекциях её нет: 1452
    # «Отказ включения автомата» роняет панель в Shutdown без бита в масках,
    # и видно это только по типу последней неисправности (40013), который
    # решает вид стопа, но в детекцию не идёт. Предупреждение панели без
    # останова (Warning в 40013) сюда не попадает — стоп тогда простой.
    if _stop_kind == "EMERGENCY":
        panel_sev = sev_level = "авария"

    # ── Активные тревоги с расшифровкой из справочника ──
    panel_alarms:     list[dict] = []
    analytics_alarms: list[dict] = []
    regime_alarms:    list[dict] = []
    seg_t0 = _as_dt(seg_row.get("t_start"))
    code_at_start = _code_at_segment_start(chars)
    for d in active_dets:
        scenario    = d.get("scenario", "?")
        severity    = cap_severity(scenario, d.get("severity", "INFO"))
        trigger     = d.get("trigger", "")
        fault_codes = d.get("fault_codes") or []

        # Расшифровка: первый verified-код из справочника
        description: str | None = None
        if fault_ref:
            for code in fault_codes:
                try:
                    entry = fault_ref.lookup(int(code))
                    if entry and entry.get("verified"):
                        desc = entry.get("description") or {}
                        description = desc.get("ru") or desc.get("en")
                        break
                except (TypeError, ValueError):
                    pass

        # Панель — с номером кода: «код 1248 «Предупреждение двигателя — общее»»
        # (v5.1.16); без кода — имя бита без «addr=…, bit=…»
        if scenario == "CONTROLLER_FAULT":
            if description and fault_codes:
                description = f"код {fault_codes[0]} «{description}»"
            elif not description:
                description = (d.get("values") or {}).get("fault_name") or None

        # Аналитику не подписывать именем кода Cummins: «Высокая температура масла 1»
        # у сигнала «близко к порогу» (v5.1.6, решение B)
        if not description and scenario != "CONTROLLER_FAULT":
            from analytics.serializer import SCENARIO_RU
            # Порог LIMIT_PROXIMITY — уставка останова панели, не паспорт (v5.1.12)
            description = (f"{trigger.split(':', 1)[0].strip()} у уставки останова панели"
                           if scenario == "LIMIT_PROXIMITY" and ":" in (trigger or "")
                           else SCENARIO_RU.get(scenario))
        if not description:
            description = trigger or scenario

        # Счётчики срабатываний из enriched values (заполняются движком каждый цикл)
        det_vals = d.get("values") or {}
        alarm = {
            "scenario":          scenario,
            "severity":          severity,
            "trigger":           trigger,
            "fault_codes":       fault_codes,
            "ref_codes":         d.get("ref_codes"),
            "description":       description,
            "history_count_30d":        det_vals.get("history_count_30d"),
            "history_duration_30d_sec": det_vals.get("history_duration_30d_sec"),
            "startup_count":            det_vals.get("startup_count"),
            "startup_duration_sec":     det_vals.get("startup_duration_sec"),
            # v5.1.12: свой параметр тревоги и её порог — гейту, чтобы судить
            # по нему, а не по соседнему
            "related_roles":     [r for r in d.get("related_roles") or []
                                  if not str(r).startswith(("FAULT_MASK", "LAST_FAULT"))],
            "limit":             _limit(d) or None,
        }
        if scenario == "CONTROLLER_FAULT":
            alarm["since"] = det_vals.get("fault_start")
            # «Общая авария» с кодом висела на начало сегмента (и код тот же) —
            # сведения; появилась или сменила код в этом сегменте — запрет снятия
            # (расчёт общий с suppression_bans, v5.1.22)
            alarm["fresh"] = bool(
                severity == "CAUTION" and _caution_fresh(
                    det_vals.get("fault_start"), fault_codes, seg_t0, code_at_start))
            panel_alarms.append(alarm)
        elif scenario in GATE_EXCLUDED_SCENARIOS:
            regime_alarms.append(alarm)
        else:
            analytics_alarms.append(alarm)

    # ── Ключевые параметры для режима ──
    key_params: list[dict] = []
    for role in _KEY_ROLES.get(run_state, []):
        if role in values:
            val_data = values[role]
            raw_val = val_data.get("value")
            if raw_val is None:
                raw_val = val_data.get("median")
            if raw_val is not None:
                try:
                    key_params.append({
                        "role":  role,
                        "value": round(float(raw_val), 1),
                        "unit":  val_data.get("unit", ""),
                    })
                except (TypeError, ValueError):
                    pass

    try:
        last_code = int((values.get("LAST_FAULT_CODE") or {}).get("value") or 0)
    except (TypeError, ValueError):
        last_code = 0

    return {
        "run_state":          run_state,
        "mode_label":         mode_label,
        "load_pct":           load_pct,
        "load_pct_bucket":    load_pct_bucket,
        "severity_level":     sev_level,
        "panel_severity":     panel_sev,
        "analytics_severity": analytics_sev,
        "analytics_suppressed": suppressed,
        "time_in_mode_sec":   time_in_mode_sec,
        "panel_alarms":       panel_alarms,
        # Состав гейта (без режимных замечаний); режимные — отдельно, для строки
        "analytics_alarms":   analytics_alarms,
        "regime_alarms":      regime_alarms,
        "key_params":         key_params,
        # Все параметры подсегмента — гейту (решение 12.10: не 2–3 ключевых)
        "params":             _all_params(values),
        "last_fault_code":    last_code or None,
        # Вид стопа нарезки: аварийность здесь не только по маскам, но и по
        # типу последней неисправности (40013), которого нет в детекциях
        "stop_kind":          _stop_kind,
        "emergency_text":     (_emergency_text(panel_alarms, values, fault_ref)
                               if _stop_kind == "EMERGENCY" else None),
        "coking":             _coking_state(seg_row),
    }


def _as_dt(raw) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    try:
        ts = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _code_at_segment_start(chars: dict) -> int | None:
    """Код последней неисправности (40012) на начало сегмента — по первому подсегменту."""
    subs = (chars or {}).get("subsegments") or []
    if not subs:
        return None
    lfc = ((subs[0] or {}).get("characteristics") or {}).get("LAST_FAULT_CODE") or {}
    v = lfc.get("value_start")
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


# Служебные счётчики и коды — не параметры для разбора
_PARAM_SKIP = frozenset({"HEARTBEAT", "FUEL_TOTAL", "ENGINE_HOURS", "START_ATTEMPTS_TOTAL",
                         "LAST_FAULT_CODE", "LAST_FAULT_TYPE"})
_PARAM_GROUPS = ("Двигатель", "Генератор", "Нагрузка")


def _param_group(role: str) -> str | None:
    if role in _PARAM_SKIP or role.startswith("FAULT_MASK"):
        return None
    if role.startswith(("LOAD_PCT", "ACTIVE_POWER", "REACTIVE_POWER", "APPARENT_POWER", "RATING_")):
        return "Нагрузка"
    if role.startswith(("VOLTAGE", "CURRENT", "FREQUENCY", "POWER_FACTOR")):
        return "Генератор"
    return "Двигатель"


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, 1)


def _all_params(values: dict) -> list[dict]:
    """Все параметры открытой строки: роль, группа, сейчас / мин / макс, единица."""
    out: list[dict] = []
    for role, v in (values or {}).items():
        grp = _param_group(role)
        if grp is None or not isinstance(v, dict):
            continue
        val = _num(v.get("value") if v.get("value") is not None else v.get("median"))
        if val is None:
            continue
        out.append({"role": role, "group": grp, "value": val, "min": _num(v.get("min")),
                    "max": _num(v.get("max")), "unit": v.get("unit") or ""})
    return out


def _emergency_text(panel_alarms: list[dict], values: dict, fault_ref=None) -> str:
    """Подпись аварийного стопа: аварийная маска, иначе код панели из 40012.

    У 1452 бита в масках нет, и из панельных тревог висит разве что CAUTION
    «CommonAlarm при несброшенном коде» — подписать аварию им значило бы
    назвать Shutdown предупреждением. Имя кода — из справочника: это
    официальное название Cummins, а не сгенерированное описание.
    """
    shut = next((a for a in panel_alarms if a.get("severity") == "SHUTDOWN"), None)
    if shut:
        return shut.get("description") or shut.get("scenario") or "аварийный останов"
    try:
        code = int((values.get("LAST_FAULT_CODE") or {}).get("value") or 0)
    except (TypeError, ValueError):
        code = 0
    if code <= 0:
        return "аварийный останов"
    name = None
    if fault_ref:
        try:
            desc = (fault_ref.lookup(code) or {}).get("description") or {}
            name = desc.get("ru") or desc.get("en")
        except (TypeError, ValueError, AttributeError):
            name = None
    return f"аварийный останов, код {code}" + (f" — {name}" if name else "")


def compute_fault_hash(s: dict) -> str:
    """Хэш активных тревог — для детекции новых неисправностей.

    Включает scenario + fault_codes, чтобы аналитические детекции без fault_codes
    (METRIC_RULE) тоже давали уникальный хэш и не считались «уже проанализированными».
    """
    all_alarms = s.get("panel_alarms", []) + s.get("analytics_alarms", [])
    key = tuple(sorted(
        (alarm.get("scenario", ""), _hash_codes(alarm))
        for alarm in all_alarms
    ))
    return hashlib.md5(str(key).encode()).hexdigest()[:12]


def format_status_text(s: dict) -> str:
    """Детерминированная статус-строка — всегда актуальна, без LLM.

    Нагрузку намеренно не включаем: строка обновляется раз в интервал
    планировщика и рядом с live-значениями выглядит устаревшей.
    """
    mode              = s["mode_label"]
    panel_sev         = s.get("panel_severity", "норма")
    analytics_sev     = s.get("analytics_severity", "норма")
    panel_alarms      = s.get("panel_alarms", [])
    analytics_alarms  = s.get("analytics_alarms", [])
    dur               = s.get("time_in_mode_sec", 0)

    # Режим + время в режиме
    base = mode
    if dur > 60:
        base += f" {_fmt_duration(dur)}"

    parts = []

    if s.get("emergency_text"):
        parts.append(f"🔴 панель: {s['emergency_text']}")
    elif panel_sev == "авария" and panel_alarms:
        desc = panel_alarms[0].get("description") or panel_alarms[0]["scenario"]
        parts.append(f"🔴 панель: {desc}")
    elif panel_sev == "внимание" and panel_alarms:
        desc = panel_alarms[0].get("description") or panel_alarms[0]["scenario"]
        parts.append(f"🟠 панель: {desc}")

    # Снятый состав — «проверено ИИ»; режимное замечание рядом остаётся живым
    regime = s.get("regime_alarms") or []
    supp = s.get("analytics_suppressed")
    shown = regime if supp else (analytics_alarms or regime)
    if analytics_sev == "предупреждение" and shown:
        desc = shown[0].get("description") or shown[0]["scenario"]
        parts.append(f"🟡 аналитика: {desc}")
    if supp:
        # Ненавязчивый след гейта: срабатывание было, ИИ проверил и отменил
        parts.append("✓ аналитика: срабатывание проверено ИИ, угрозы нет")

    coking = _coking_status_part(s.get("coking"), s.get("run_state"))
    if coking:
        parts.append(coking)

    if parts:
        return f"{base} — {' | '.join(parts)}."
    return f"{base} — параметры в норме."


def _coking_status_part(ck: dict | None, run_state) -> str | None:
    """Риск нагара для строки «Состояние»: перед пуском — на стоящей машине.

    Накопленный недогруз опасен при пуске и первых минутах под нагрузкой, поэтому
    знать о нём нужно до пуска (решение 11.10.2026).
    """
    if not ck:
        return None
    overdue = _monthly_overdue_days(ck)
    cal = (f"⚠ прогон под нагрузкой не выполнялся {overdue:.0f} дн. (Cummins — ежемесячно)"
           if overdue else None)
    if ck.get("risk_level") in (None, "GREEN"):
        return cal
    lvl = _COKING_LEVEL_RU.get(ck["risk_level"], ck["risk_level"])
    left = ck.get("purge_left_low_min")
    pct = ck.get("purge_low_pct")
    tail = (f"до сброса ~{left:.0f} мин под нагрузкой ≥{pct:.0f} %"
            if left and pct else "")
    if ck.get("service_needed"):
        tail = "нужен сервисный прожиг (долг больше 32 ч)"
    if run_state == 0:
        since = (f"накоплен с прожига {ck['last_purge_ts'][8:10]}.{ck['last_purge_ts'][5:7]}"
                 if ck.get("last_purge_ts") else "накоплен при прошлой работе")
        txt = f"⚠ перед пуском: риск нагара {lvl} — {since}" + (f"; {tail}" if tail else "")
    else:
        txt = f"🟡 риск нагара {lvl}" + (f" — {tail}" if tail else "")
    return f"{txt} | {cal}" if cal else txt


def _monthly_overdue_days(ck: dict) -> float | None:
    """Сколько дней без ежемесячного прогона, если больше 30 (Cummins), иначе None."""
    ref = ck.get("last_monthly_run_ts") or ck.get("calendar_since_ts")
    if not ref:
        return None
    try:
        t = datetime.fromisoformat(ref)
    except (TypeError, ValueError):
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    days = (datetime.now(timezone.utc) - t).total_seconds() / 86400.0
    return days if days > 30 else None


def extract_alarm_text(s: dict) -> str | None:
    """Текст главной тревоги для карточки внешнего UI: панель важнее аналитики.

    None — тревог нет (норма или срабатывание подавлено гейтом).
    """
    panel_sev        = s.get("panel_severity", "норма")
    analytics_sev    = s.get("analytics_severity", "норма")
    panel_alarms     = s.get("panel_alarms", [])
    analytics_alarms = s.get("analytics_alarms", [])

    # Аварийный стоп без аварийной маски (1452): в панельных тревогах самое
    # тяжёлое — CAUTION, подписывать им аварию нельзя
    if s.get("emergency_text") and not any(
            a.get("severity") == "SHUTDOWN" for a in panel_alarms):
        return s["emergency_text"]
    if panel_sev != "норма" and panel_alarms:
        return _compose_alarm_text(panel_alarms)
    regime = s.get("regime_alarms") or []
    shown = regime if s.get("analytics_suppressed") else analytics_alarms + regime
    if analytics_sev == "предупреждение" and shown:
        return _compose_alarm_text(shown)
    return None


def _compose_alarm_text(alarms: list[dict]) -> str | None:
    """Подпись СОСТАВА тревог: самая тяжёлая плюс счёт остальных.

    Гейт разбирает не одну тревогу, а весь состав разом — в промпт уходят
    все активные. Подписывать разбор одним именем значит врать: 20.09 разбор
    аварийного останова вышел озаглавлен предупреждением по температуре ОЖ
    (оно просто стояло первым в списке), и выглядело так, будто саму аварию
    никто не разбирал.
    """
    if not alarms:
        return None
    rank = {"SHUTDOWN": 3, "WARNING": 2, "CAUTION": 1}
    top = max(alarms, key=lambda a: rank.get(a.get("severity"), 0))
    name = top.get("description") or top.get("scenario")
    rest = len(alarms) - 1
    return f"{name} + ещё {rest}" if rest > 0 else name


def build_warning_prompt(
    s: dict,
    trip_context: dict | None = None,
    prev_analyses: list[dict] | None = None,
    episode_timeline: list[dict] | None = None,
) -> str:
    """Промпт для Claude-анализа предупреждения (новые fault-коды).

    trip_context — контекст аварии с SHUTDOWN-эпизода (Фаза C): что машина
    делала до отключения. Передаётся только когда панель в аварийном останове.
    prev_analyses — предыдущие разборы ЭТОГО сегмента (смена состава тревог:
    сброс, кнопка останова): новый разбор — продолжение истории, а не
    изолированный анализ последнего события.
    episode_timeline — хронология эпизодов тревог сегмента.
    """
    lines = [
        "Выполни анализ неисправности дизель-генераторной установки.",
        "",
        f"Режим работы: {s['mode_label']} (RUN_STATE={s['run_state']})",
    ]

    if prev_analyses:
        lines.append("")
        lines.append(
            "Предыдущие разборы этого сегмента — прошлые мнения, не довод (события "
            "развиваются — новые сигналы могут быть следствием исходной аварии или "
            "действий персонала):"
        )
        from analytics.labels import fmt_local, tz_label
        for pa in prev_analyses:
            # Местное время с поясом (v5.1.22, P1-6), было — UTC
            _lt = fmt_local(pa.get("t"), day=True, seconds=False)
            t = f"{_lt} {tz_label()}" if _lt else (pa.get("t") or "")[:16].replace("T", " ") + " UTC"
            alarm = pa.get("alarm_text") or "состав тревог не записан"
            lines.append(f"--- Разбор {t} — {alarm} ---")
            lines.append(pa.get("md") or "")
        lines.append("--- Конец предыдущих разборов ---")

    if episode_timeline:
        lines.append("")
        from analytics.labels import fmt_local as _fl, tz_label as _tzl
        # Время — местное (v5.1.22, P1-6), было — UTC без пометки
        lines.append(f"Хронология тревог сегмента (время {_tzl()}):")
        for e in episode_timeline:
            t_open = e.get("t_open")
            t_open_s = _fl(t_open) or str(t_open or "?")
            t_close = e.get("t_close")
            span = f"{t_open_s} → " + (_fl(t_close) or "активна" if t_close else "активна")
            supp = (" — снималась гейтом (прошлое решение, не довод)"
                    if e.get("gate_suppressed") else "")
            if e.get("close_reason") == "transition_event":
                lines.append(f"  [{cap_severity(e.get('scenario'), e.get('severity'))}] "
                             f"{e.get('scenario')}: разовое событие {t_open_s}")
                continue
            lines.append(
                f"  [{cap_severity(e.get('scenario'), e.get('severity'))}] {e.get('scenario')}: {span}, "
                f"воздействие {_fmt_duration(e.get('active_sec') or 0)}"
                f"{_blind_note(e.get('blind_sec'))}{supp}"
            )

    if trip_context:
        lines.append("")
        lines.append("Контекст аварийного останова:")
        prev = trip_context.get("prev_segment")
        if prev:
            label = prev.get("run_state_label") or f"RUN_STATE={prev.get('run_state')}"
            dur = prev.get("duration_sec")
            line = f"  До аварии: {label}"
            if dur:
                line += f" длительностью {_fmt_duration(dur)}"
            zones = prev.get("zones_sec") or {}
            if zones:
                zstr = ", ".join(f"{z}: {_fmt_duration(v)}" for z, v in zones.items() if v)
                if zstr:
                    line += f" (зоны нагрузки — {zstr})"
            lines.append(line)
        trend = trip_context.get("trend")
        if trend and trend.get("params"):
            lines.append(f"  Параметры за {trend.get('window_min', 15)} мин до аварии:")
            for role, v in trend["params"].items():
                unit = v.get("unit", "")
                lines.append(
                    f"    {role}: {v.get('first')} → {v.get('last')} {unit}"
                    f" (мин {v.get('min')}, макс {v.get('max')})"
                )
        for al in trip_context.get("open_alarms", []):
            supp = (" — снималась гейтом (прошлое решение, не довод)"
                    if al.get("gate_suppressed") else "")
            lines.append(
                f"  Висела тревога: {al.get('scenario')} "
                f"[{cap_severity(al.get('scenario'), al.get('severity'))}]"
                f" {_fmt_duration(al.get('active_sec') or 0)}"
                f"{_blind_note(al.get('blind_sec'))}{supp}"
            )
        day = trip_context.get("last_24h")
        if day:
            lines.append(
                f"  За 24ч: пусков {day.get('starts_count', 0)}, наработка "
                f"{_fmt_duration(day.get('running_sec') or 0)}, "
                f"макс. зона {day.get('max_load_zone', 'NA')}"
            )

    if s.get("load_pct") is not None:
        lines.append(f"Нагрузка: {s['load_pct']:.0f}%")

    dur = s.get("time_in_mode_sec", 0)
    if dur > 60:
        lines.append(f"Время в режиме: {_fmt_duration(dur)}")

    lines.append(f"Итоговый уровень: {s['severity_level']}")
    panel_caution = [a for a in s.get("panel_alarms") or [] if a.get("severity") == "CAUTION"]
    lines.append(f"Источник панели: {s.get('panel_severity', 'норма')}"
                 + (" (есть CAUTION панели, см. ниже)" if panel_caution else ""))
    lines.append(f"Источник аналитики: {s.get('analytics_severity', 'норма')}")
    lines.append(_panel_line(s))
    reasons = uncancellable_reasons(s)
    if reasons:
        line = ("Гейт: отмена НЕДОСТУПНА — " + "; ".join(r["text"] for r in reasons)
                + ". Вердикт только pass; поясни оператору, что проверить.")
        if any(r["code"] == "asymmetry" for r in reasons):
            line += (" Несимметрия (ток обратной последовательности, перекос фаз или ток "
                     "нейтрали): распределение однофазной нагрузки по фазам, соединения, "
                     "трансформатор.")
        lines.append(line)
    else:
        lines.append(
            "Гейт: тревоги чисто аналитические — вердикт cancel ДОПУСТИМ, если угроза "
            "не подтверждается по собственным параметрам тревог."
        )
    lines.append("")

    panel_alarms     = s.get("panel_alarms", [])
    analytics_alarms = s.get("analytics_alarms", [])

    if panel_alarms:
        lines.append("Сигналы панели управления (CONTROLLER_FAULT):")
        for a in panel_alarms:
            desc  = a.get("description") or a["scenario"]
            codes = ", ".join(str(c) for c in a.get("fault_codes", []))
            lines.append(f"  [{a['severity']}] {desc}" + (f" (коды: {codes})" if codes else ""))

    if analytics_alarms:
        if panel_alarms:
            lines.append("")
        lines.append("Сигналы аналитического движка:")
        for n, a in enumerate(analytics_alarms, 1):
            desc          = a.get("description") or a["scenario"]
            codes         = ", ".join(str(c) for c in a.get("fault_codes", []))
            count_30d     = a.get("history_count_30d")
            count_startup = a.get("startup_count")
            trig          = a.get("trigger")
            line = (f"  A{n} [{a['severity']}] {desc}" + (f": {trig}" if trig and trig != desc else "")
                    + (f" (код панели выставлен: {codes})" if codes else ""))
            if count_30d is not None:
                line += f" — за 30 дней: {count_30d}"
                dur_30d = a.get("history_duration_30d_sec")
                if dur_30d:
                    line += f" ({int(dur_30d) // 60} мин суммарно"
                    blind_30d = a.get("history_blind_30d_sec")
                    if blind_30d:
                        line += f", {int(blind_30d) // 60} мин без связи"
                    line += ")"
            if count_startup is not None:
                line += f", с пуска: {count_startup}"
                dur_startup = a.get("startup_duration_sec")
                if dur_startup:
                    line += f" ({int(dur_startup) // 60} мин)"
            lines.append(line)
            lim_line = _limit_line(a)
            if lim_line:
                lines.append(f"     {lim_line}")

    params = s.get("params") or []
    if params:
        marks: dict[str, list[str]] = {}
        for n, a in enumerate(analytics_alarms, 1):
            for r in a.get("related_roles") or []:
                marks.setdefault(r, []).append(f"A{n}")
        lines.append("")
        lines.append("Параметры (сейчас; у параметров тревог — мин / макс за подсегмент):")
        for grp in _PARAM_GROUPS:
            rows = [p for p in params if p["group"] == grp]
            if not rows:
                continue
            lines.append(f"  {grp}:")
            for p in rows:
                txt = f"    {p['role']}: {_fmt_num(p['value'])} {p['unit']}".rstrip()
                if p["role"] in marks:
                    if p.get("min") is not None and p.get("max") is not None:
                        txt += f" (мин {_fmt_num(p['min'])} / макс {_fmt_num(p['max'])})"
                    txt += f"  ← параметр тревоги {', '.join(marks[p['role']])}"
                lines.append(txt)
    else:
        kp = s.get("key_params", [])
        if kp:
            lines.append("")
            lines.append("Ключевые параметры:")
            for p in kp:
                lines.append(f"  {p['role']}: {p['value']} {p['unit']}")

    lines += [
        "",
        "Правила вердикта:",
        "- каждую тревогу оценивай по её собственному параметру (помечен «← параметр "
        "тревоги»); другие параметры её не подтверждают и не опровергают;",
        "- нарушение паспорта двигателя — не норма, даже при большом запасе до уставок "
        "панели; ниже паспорта в пределах шага датчика — тоже не норма, снять можно "
        "только как погрешность данных;",
        "- прошлые разборы и снятия гейтом — не довод, решай по текущим значениям;",
        "- cancel — только с причиной: недостоверные данные / кратковременно / порог "
        "аналитики строже панели и тренда нет.",
        "",
        "Дай краткий технический анализ: что произошло, возможные причины, "
        "на что обратить внимание оператору. Ответ на русском языке.",
    ]
    return "\n".join(lines)


def _fmt_num(v) -> str:
    if v is None:
        return "—"
    f = float(v)
    return f"{f:.0f}" if f == int(f) else f"{f:g}"


def _panel_line(s: dict) -> str:
    """Честная строка панели (v5.1.12): раньше при CAUTION панели гейт писал
    «сигналов панели нет» — во всех 66 снятиях за 19.09–12.10."""
    sev = s.get("panel_severity", "норма")
    caution = [a for a in s.get("panel_alarms") or [] if a.get("severity") == "CAUTION"]
    if sev != "норма":
        return f"Панель управления: {sev} — сигналы WARNING/SHUTDOWN, см. ниже."
    if caution:
        parts = []
        for a in caution:
            codes = ", ".join(str(c) for c in a.get("fault_codes") or [])
            # Местное время с поясом (v5.1.22, P1-6): «с 13.10 15:01 МСК»
            from analytics.labels import fmt_local, tz_label
            _lt = fmt_local(a.get("since"), day=True, seconds=False) if a.get("since") else None
            since = (f"{_lt} {tz_label()}" if _lt
                     else ((a.get("since") or "")[:16].replace("T", " ") + " UTC" if a.get("since") else ""))
            txt = (f"CAUTION — {a.get('description') or 'Общая авария'}, несброшенный код {codes}"
                   + (f", с {since}" if since else ""))
            txt += (" — появилась в этом сегменте: снятие аналитики запрещено"
                    if a.get("fresh") else
                    " — висела на начало сегмента: это сведения, снятие аналитики не блокирует")
            parts.append(txt)
        return "Панель управления: " + "; ".join(parts) + "."
    code = s.get("last_fault_code")
    return ("Панель управления: аварийных и предупредительных сигналов нет"
            + (f"; в 40012 несброшенный код последней неисправности {code}." if code else "."))


def _limit_line(a: dict) -> str | None:
    """Порог тревоги и его вид — для паспорта; у остальных порог в тексте срабатывания."""
    lim = a.get("limit") or {}
    if lim.get("kind") != "passport":
        return None
    step = lim.get("step") or 0
    step_txt = (f", шаг датчика {_fmt_num(step)} {lim.get('unit', '')}"
                + (" (по истории машины)" if lim.get("step_source") == "history" else ""))
    head = (f"порог: паспорт {lim.get('engine') or 'двигателя'} — минимум "
            f"{_fmt_num(lim.get('limit'))} {lim.get('unit', '')}{step_txt}; "
            f"мин. {_fmt_num(lim.get('value'))}, ниже паспорта непрерывно "
            f"{_fmt_duration(lim.get('run_sec') or 0)}")
    if lim.get("hard"):
        return head + ". НАРУШЕНИЕ ПАСПОРТА — это не норма, снять нельзя."
    return head + ". Ниже паспорта в пределах шага датчика — не норма; снять можно, только если это погрешность данных."


def gate_facts_md(s: dict, decision: str, applied: bool, reasons: list[dict]) -> str:
    """Абзац «Факты (система, не ИИ)» в начало разбора гейта (v5.1.12): заключение
    и оператор видят факты рядом с мнением модели, а не только мнение."""
    facts = [_panel_line(s).replace("Панель управления: ", "панель — ").rstrip(".")]
    for n, a in enumerate(s.get("analytics_alarms") or [], 1):
        lim = a.get("limit") or {}
        if lim.get("kind") == "passport":
            facts.append(
                f"A{n}: давление масла мин. {_fmt_num(lim.get('value'))} {lim.get('unit', '')} "
                f"при паспорте {_fmt_num(lim.get('limit'))} (шаг датчика {_fmt_num(lim.get('step'))})"
                + (" — нарушение паспорта" if lim.get("hard") else " — ниже паспорта в пределах шага, не норма"))
    if decision == "cancel":
        facts.append("вердикт модели «снять» — " + (
            "применён (на момент разбора)" if applied else
            "не применён: " + "; ".join(r["text"] for r in reasons)))
    return "**Факты (система, не ИИ):** " + "; ".join(facts) + ".\n\n"
