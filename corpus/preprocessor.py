# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Препроцессор: формирует вход для Claude из закрытого auto_segment.

Не урезает данные — подаёт полный report_md сегмента as-is.
Добавляет только шапку с вердиктом блока, чтобы Claude не пересматривал его.
"""
from __future__ import annotations
import json
from typing import Any

# Русские названия RUN_STATE (из analytics/serializer.py — дублируем чтобы не создавать зависимость)
RUN_STATE_RU: dict[int, str] = {
    0: "Стоп",
    1: "Задержка пуска",
    2: "Прогрев",
    3: "Работа",
    4: "Разгрузка",
    5: "Охлаждение на х.х.",
    6: "Переход на х.х.",
}


def _parse_json(val: Any) -> Any:
    if val is None:
        return None
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            return None
    return val


def _extract_detections(chars_json: Any) -> list[dict]:
    """Извлечь все детекции из characteristics_json сегмента."""
    parsed = _parse_json(chars_json)
    if not parsed:
        return []
    from analytics.contract import cap_severity
    detections: list[dict] = []
    for sub in parsed.get("subsegments", []):
        for d in sub.get("detections", []):
            if not isinstance(d, dict):
                detections.append({})
                continue
            # Аналитика не выше CAUTION (v5.1.7): старые сегменты со «не остановился»
            # WARNING не получают вердикт «предупреждение панели» и разбор seg_warning
            sev = cap_severity(d.get("scenario"), d.get("severity"))
            detections.append(d if sev == d.get("severity") else dict(d, severity=sev))
    return detections


def _stop_kind(chars_json: Any) -> str | None:
    """Вид стоп-сегмента нарезки: 'EMERGENCY' / 'SIMPLE' / None."""
    parsed = _parse_json(chars_json)
    return parsed.get("stop_kind") if isinstance(parsed, dict) else None


def _gate_suppressed(segment_row: dict, detections: list[dict]) -> bool:
    """Снял ли гейт (ИИ) аналитические предупреждения сегмента.

    Вердикт «отменить» действует, пока состав аналитических детекций
    не изменился — сверяем hash состава с gate_suppressed_hash сегмента.
    """
    suppressed = segment_row.get("gate_suppressed_hash")
    if not suppressed:
        return False
    from online.status_assembler import compute_analytics_hash, has_uncancellable
    if has_uncancellable(detections):
        return False
    has_analytics = any(
        isinstance(d, dict) and d.get("scenario") != "CONTROLLER_FAULT"
        for d in detections
    )
    return has_analytics and compute_analytics_hash(detections) == suppressed


def _extract_verdict(
    detections: list[dict],
    gate_suppressed: bool = False,
    stop_kind: str | None = None,
) -> tuple[str, str]:
    """Вычислить вердикт и уровень тревоги из списка детекций.

    Аварийный стоп — авария, даже если в детекциях её нет: 1452 «Отказ
    включения автомата» роняет панель в Shutdown без бита в масках, и
    видно это только по типу последней неисправности (40013), который
    решает вид стопа, но в детекцию не идёт.

    Шкала серьёзности (4 уровня, ALARM исключён — в панелях его нет):
        НОРМА    🟢 — детекций нет / только INFO / аналитика снята ИИ
        CAUTION  🟡 — предупреждение аналитики (не снятое ИИ)
        WARNING  🟠 — предупреждение панели
        SHUTDOWN 🔴 — авария панели

    Returns:
        (verdict, alarm_level)
        verdict:     норма / требует внимания / авария
        alarm_level: НОРМА / CAUTION / WARNING / SHUTDOWN
    """
    sevs = {d.get("severity") for d in detections if isinstance(d, dict) and d.get("severity")}

    if "SHUTDOWN" in sevs or stop_kind == "EMERGENCY":
        return "авария", "SHUTDOWN"
    if "WARNING" in sevs or "ALARM" in sevs:   # ALARM — легаси старых сегментов
        return "требует внимания", "WARNING"
    if "CAUTION" in sevs:
        # Режимное замечание гейт не снимает (v5.1.12)
        from online.status_assembler import GATE_EXCLUDED_SCENARIOS
        if gate_suppressed and not any(
                isinstance(d, dict) and d.get("severity") == "CAUTION"
                and d.get("scenario") in GATE_EXCLUDED_SCENARIOS for d in detections):
            return "норма", "НОРМА"
        return "требует внимания", "CAUTION"
    return "норма", "НОРМА"


# Визуальные метки уровней: emoji + русская расшифровка (мелко в отчёте).
# Emoji вердикта берётся от уровня тревоги — цвет у них общий.
ALARM_LEVEL_META: dict[str, tuple[str, str]] = {
    "НОРМА":    ("🟢", "всё в порядке"),
    "CAUTION":  ("🟡", "предупреждение аналитики"),
    "WARNING":  ("🟠", "предупреждение панели"),
    "SHUTDOWN": ("🔴", "авария панели"),
}


def _fmt_detections(detections: list[dict]) -> str:
    """Форматировать список детекций для шапки."""
    if not detections:
        return "нет"
    parts = []
    for d in detections:
        sev = d.get("severity", "INFO")
        scenario = d.get("scenario", "?")
        trigger = d.get("trigger", "")
        parts.append(f"{scenario} ({sev}): {trigger}")
    return "; ".join(parts)


# ── Агрегированные «Обнаружения» для БЛОКА 1 заключения ──────────────────────
# Человеческие названия сценариев — канонический словарь в analytics/serializer
from analytics.serializer import SCENARIO_RU  # noqa: E402
from analytics.contract import cap_severity  # noqa: E402

_SEV_EMOJI = {"SHUTDOWN": "🔴", "ALARM": "🟠", "WARNING": "🟠",
              "CAUTION": "🟡", "INFO": "🔵"}
_SEV_RANK = {"SHUTDOWN": 4, "ALARM": 3, "WARNING": 3, "CAUTION": 2, "INFO": 1}


def _fmt_dur(sec: float) -> str:
    """Как в отчёте и UI, без секунд от минуты: «1 ч 05 мин», «5 мин», «40 с» (v5.1.20)."""
    from analytics.labels import fmt_duration
    return fmt_duration(sec, seconds=False)


def _plural_hits(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} срабатывание"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} срабатывания"
    return f"{n} срабатываний"


def _group_worst(scenario: str, values_list: list[dict]) -> str | None:
    """Худший показатель группы за период — вместо дампа каждого срабатывания."""
    def _nums(key):
        return [v[key] for v in values_list
                if isinstance(v.get(key), (int, float))]

    if scenario == "OIL_DILUTION":
        mins = _nums("oil_press_min")
        passports = _nums("passport_min_kpa")
        if mins:
            base = f"мин. {min(mins):.0f} кПа"
            return base + (f" при паспорте ≥{passports[0]:.0f}" if passports else "")
    elif scenario == "LIMIT_PROXIMITY":
        vals, limits = _nums("value"), _nums("shutdown_limit")
        if vals and limits:
            return f"макс. {max(vals):.0f} при пороге {limits[0]:.0f}"
    elif scenario in ("PHASE_CURRENT_SPREAD", "NEUTRAL_CURRENT"):
        vals, thr = _nums("pct_max"), _nums("threshold_pct")
        if vals:
            refs = [v.get("norm_ref") for v in values_list if v.get("norm_ref")]
            base = f"до {max(vals):.0f}% I_ном"
            if thr:
                base += f" при норме ≤{thr[0]:.0f}%"
            return base + (f" ({refs[0]})" if refs else "")
    elif scenario == "NEGATIVE_SEQUENCE":
        vals, thr = _nums("i2_pct_max"), _nums("proximity_threshold_pct")
        if vals:
            return f"I₂ до {max(vals):.1f}% I_ном" + (f" при пороге {thr[0]:.0f}%" if thr else "")
    elif scenario == "RPM_UNDERSPEED":
        mins = _nums("rpm_min")
        if mins:
            thr = _nums("threshold_rpm")
            base = f"мин. {min(mins):.0f} об/мин"
            return base + (f" — на {thr[0] - min(mins):.0f} ниже порога {thr[0]:.0f}" if thr else "")
    elif scenario == "COKING_RISK":
        secs = _nums("low_load_zone_sec")
        if secs:
            return f"накоплено с прожига до {_fmt_dur(max(secs))} малой нагрузки"
    elif scenario == "COOLING_FAILURE":
        temps = _nums("coolant_temp_max")
        slopes = _nums("coolant_slope_c_per_s")
        if temps:
            return f"макс. {max(temps):.0f}°C"
        if slopes:
            return f"тренд до {max(slopes):.4f} °C/с"
    return None


def _live_clusters(d: dict) -> dict[str, float]:
    """Кластеры values.live детекции: {начало: чистое время} (v5.1.21). У близости
    к уставке — участки своей роли (values.role) внутри кластера. {} — без участков."""
    v = d.get("values") if isinstance(d.get("values"), dict) else {}
    lv = v.get("live")
    if not isinstance(lv, dict):
        return {}
    role = v.get("role")
    out: dict[str, float] = {}
    for c in lv.get("clusters") or []:
        if not isinstance(c, dict) or not c.get("start"):
            continue
        rr = c.get("roles")
        if role and isinstance(rr, dict) and isinstance(rr.get(role), dict):
            viol = rr[role].get("viol_sec")
        else:
            viol = c.get("viol_sec")
        try:
            out[str(c["start"])] = max(0.0, float(viol or 0.0))
        except (TypeError, ValueError):
            out[str(c["start"])] = 0.0
    return out


def _fmt_detections_hierarchy(chars_json: Any) -> str:
    """Агрегированные обнаружения для БЛОКА 1: иерархия панель→аналитика,
    по-русски, с количеством срабатываний, длительностью и худшим показателем.

    Длительность = сумма длительностей подсегментов, где сценарий срабатывал
    (у самой детекции длительности нет).

    v5.1.21 (П9): у детекций с участками (values.live) число — уникальные
    кластеры нарушения (один кластер виден в каждом подсегменте, который
    пересекает), длительность — чистое время участков своей роли внутри
    кластеров (близость по параметру: температура масла не захватывает участки
    давления), а не длительность подсегментов. Старые сегменты — как было.
    """
    parsed = _parse_json(chars_json)
    subsegments = (parsed or {}).get("subsegments", [])

    # key → агрегат; ключ различает панельные аварии по имени,
    # LIMIT_PROXIMITY — по параметру (из префикса триггера: «Температура масла: …»)
    groups: dict[tuple, dict] = {}
    for sub in subsegments:
        if not isinstance(sub, dict):
            continue
        sub_dur = sub.get("duration_sec") or 0
        seen_in_sub: set[tuple] = set()
        for d in sub.get("detections", []):
            if not isinstance(d, dict):
                continue
            scenario = d.get("scenario", "?")
            severity = cap_severity(scenario, d.get("severity", "INFO"))
            trigger = d.get("trigger", "") or ""
            is_panel = d.get("source") == "CONTROLLER_FAULT" or scenario == "CONTROLLER_FAULT"

            if is_panel:
                # «Имя неисправности (addr=…, bit=…)» → имя без адресов
                name = trigger.split(" (addr=")[0].strip() or SCENARIO_RU["CONTROLLER_FAULT"]
                key = ("panel", name)
            elif scenario == "LIMIT_PROXIMITY" and ":" in trigger:
                param = trigger.split(":", 1)[0].strip()
                name = f"{param} у уставки останова панели"
                key = ("analytics", scenario, param)
            else:
                name = SCENARIO_RU.get(scenario, scenario)
                key = ("analytics", scenario)

            g = groups.setdefault(key, {
                "name": name, "scenario": scenario, "panel": is_panel,
                "sev_rank": 0, "severity": severity,
                "count": 0, "dur_sec": 0.0, "values": [],
                "fault_codes": set(), "live": {},
            })
            if _SEV_RANK.get(severity, 0) > g["sev_rank"]:
                g["sev_rank"] = _SEV_RANK.get(severity, 0)
                g["severity"] = severity
            if isinstance(d.get("values"), dict):
                g["values"].append(d["values"])
            for fc in d.get("fault_codes") or []:
                g["fault_codes"].add(fc)
            # С участками — кластер: {начало: чистое время своей роли}
            _cl = _live_clusters(d)
            if _cl:
                for start, viol in _cl.items():
                    g["live"][start] = max(g["live"].get(start, 0.0), viol)
                continue
            g["count"] += 1
            if key not in seen_in_sub:
                seen_in_sub.add(key)
                g["dur_sec"] += sub_dur

    for g in groups.values():
        if g["live"]:
            g["count"] += len(g["live"])
            g["dur_sec"] += sum(g["live"].values())

    def _render(g: dict) -> str:
        parts = [_plural_hits(g["count"])]
        if g["count"] > 1 and g["dur_sec"] > 0:
            parts.append(f"суммарно {_fmt_dur(g['dur_sec'])}")
        elif g["live"] and g["dur_sec"] > 0:
            # Один эпизод нарушения — его чистое время (9 с провала — не «сутки»)
            parts.append(f"длительность {_fmt_dur(g['dur_sec'])}")
        worst = _group_worst(g["scenario"], g["values"])
        if worst:
            parts.append(worst)
        emoji = _SEV_EMOJI.get(g["severity"], "⚪")
        line = f"    - {emoji} {g['name']} — {', '.join(parts)}"
        if g["panel"] and g["fault_codes"]:
            codes = "/".join(str(c) for c in sorted(g["fault_codes"]))
            line += f" (код {codes})"
        return line

    ordered = sorted(groups.values(), key=lambda g: (-g["sev_rank"], -g["count"]))
    panel_alarm = [g for g in ordered if g["panel"] and g["sev_rank"] >= 4]
    panel_warn  = [g for g in ordered if g["panel"] and g["sev_rank"] < 4]
    analytics   = [g for g in ordered if not g["panel"]]

    lines: list[str] = []
    for emoji, title, items in (("🔴", "Аварии панели", panel_alarm),
                                ("🟠", "Предупреждения панели", panel_warn),
                                ("🟡", "Предупреждения аналитики", analytics)):
        if items:
            lines.append(f"- {emoji} **{title}:**")
            lines.extend(_render(g) for g in items)
        else:
            lines.append(f"- {emoji} **{title}** — нет")
    return "\n".join(lines)


def _format_gate_analyses(segment_row: dict) -> str:
    """Секция «Разборы в моменты событий» — что ИИ уже говорил по ходу сегмента.

    Разборы делает гейт в момент срабатывания: у него есть контекст, которого
    в итоговом заключении нет по построению — предыдущий сегмент, тренд
    параметров, состав висевших тревог на ту секунду. До v4.9.83 они в промпт
    не попадали, и модель писала вывод, не видя собственных наблюдений.
    """
    items = segment_row.get("warning_analyses")
    if isinstance(items, str):
        try:
            items = json.loads(items)
        except (ValueError, TypeError):
            items = None
    if not items:
        md = segment_row.get("warning_analysis_md")
        if not md:
            return ""
        items = [{"md": md}]
    if not isinstance(items, list):
        return ""

    # v5.1.12: разбор — мнение по ходу сегмента; факты (абзац «Факты (система)»,
    # вердикт блока аналитики, отчёт) главнее. Раньше «не противоречь без
    # основания» переносило в заключение «240 кПа в норме» при паспорте выше
    out: list[str] = ["## Разборы в моменты событий", "",
                      "Это мнения модели по ходу сегмента, в момент срабатывания. Абзац "
                      "«Факты (система, не ИИ)» и вердикт блока аналитики — проверенные факты, "
                      "рассуждение им подчинено: если разбор называет нормой то, что указано как "
                      "нарушение паспорта или сигнал панели, опирайся на факты и отметь расхождение. "
                      "Что снято ИИ, а что нет, — по пометкам замечаний в шапке, а не по разборам.",
                      ""]
    from analytics.labels import fmt_local, tz_label
    for it in items:
        if not isinstance(it, dict) or not it.get("md"):
            continue
        # Местное время с поясом (v5.1.22, P1-6): у #35872 четыре разбора
        # «12:03:25» в UTC при МСК в отчёте
        _lt = fmt_local(it.get("t"), day=True)
        ts = f"{_lt} {tz_label()}" if _lt else (it.get("t") or "")[:19].replace("T", " ")
        verdict = None
        if it.get("decision") == "cancel":
            verdict = ("снятие применено (на момент разбора)" if it.get("applied")
                       else "снятие не применено")
        head = " — ".join(x for x in (ts, it.get("alarm_text"), verdict) if x)
        out.append(f"### {head}" if head else "###")
        out.append(str(it["md"]).strip())
        out.append("")
    return "\n".join(out) + "\n" if len(out) > 4 else ""


def _format_chronology(chronology_json: Any) -> str:
    """Секция «Хронология стоянки» — что панель записала за период.

    Для стоп-сегмента это «кто что нажимал и сбрасывал»: в режиме 0 панель
    копит сообщения, не меняя режим, и без ленты период выглядит однородным.
    """
    if not chronology_json:
        return ""
    if isinstance(chronology_json, str):
        try:
            chronology_json = json.loads(chronology_json)
        except (ValueError, TypeError):
            return ""
    events = (chronology_json or {}).get("chronology") or []
    if not events:
        return ""
    from analytics.labels import fmt_local, tz_label
    # Время — местное (v5.1.22, P1-6), было — UTC без пометки
    out = ["## Хронология стоянки", "", f"Время — {tz_label()}.", "", "```"]
    for e in events:
        ts = fmt_local(e.get("ts")) or (e.get("ts") or "")[11:19]
        kind = "СОБЫТИЕ  " if e.get("kind") == "fault" else "состояние"
        bit = e.get("bit")
        a = f"{e.get('addr')}/{bit}" if bit is not None else str(e.get("addr"))
        val = f" = {e.get('label')}" if (e.get("kind") == "state" and e.get("label")) else ""
        sev = f"  [{e.get('severity')}]" if e.get("severity") else ""
        out.append(f"{ts}  {kind} {a:9} {e.get('name') or ''}{val}{sev}")
    out += ["```", ""]
    return "\n".join(out) + "\n"


def _format_incident(incident_json: Any) -> str:
    """Секция «Реконструкция аварийного останова» для промпта Claude (лента целиком).

    Только для аварийных сегментов (падение работа→не-штат): вердикт характер-гейта
    + полная лента действий (журнал состояний + фронты неисправностей). Обычные
    сегменты incident_json не имеют → пустая строка, вход не меняется.
    """
    if not incident_json:
        return ""
    if isinstance(incident_json, str):
        try:
            incident_json = json.loads(incident_json)
        except (ValueError, TypeError):
            return ""
    if not isinstance(incident_json, dict):
        return ""

    ch = incident_json.get("character") or {}
    sig = ch.get("signals") or {}
    out: list[str] = [
        "## Реконструкция аварийного останова",
        "",
        f"**Характер:** {ch.get('character', '?')} "
        f"(уверенность {ch.get('confidence', '?')}); падение из работы: {sig.get('is_fall_from_work')}",
        f"**Сигналы:** cooldown пройден={sig.get('passed_cooldown')}, "
        f"RunCommand={sig.get('run_command_path')}, RUN_STATE перед остановом={sig.get('run_state_before')}, "
        f"тип 40013={sig.get('fault_type_40013')}",
    ]
    if ch.get("immediate_votes"):
        out.append(f"**За немедленный:** {'; '.join(ch['immediate_votes'])}")
    if ch.get("controlled_votes"):
        out.append(f"**За контролируемый:** {'; '.join(ch['controlled_votes'])}")
    from analytics.labels import fmt_local, tz_label
    # Время — местное (v5.1.22, P1-6)
    out += ["", f"**Лента действий (время {tz_label()}; при равном ts состояние-причина перед "
                f"фолтом-следствием):**", "", "```"]
    for e in incident_json.get("chronology") or []:
        ts = fmt_local(e.get("ts")) or (e.get("ts") or "")[11:19]
        kind = "СОБЫТИЕ  " if e.get("kind") == "fault" else "состояние"
        bit = e.get("bit")
        a = f"{e.get('addr')}/{bit}" if bit is not None else str(e.get("addr"))
        name = e.get("name") or ""
        val = f" = {e.get('label')}" if (e.get("kind") == "state" and e.get("label")) else ""
        sev = f"  [{e.get('severity')}]" if e.get("severity") else ""
        out.append(f"{ts}  {kind} {a:9} {name}{val}{sev}")
    out += ["```", "", "---", ""]
    return "\n".join(out)


_LEVEL_VERDICT = {"НОРМА": "норма", "CAUTION": "требует внимания",
                  "WARNING": "требует внимания", "SHUTDOWN": "авария"}


def _verdict_from_summary(segment_row: dict) -> tuple[str, str] | None:
    """(verdict, alarm_level) из частей сводки (v5.1.0) — единый вердикт сегмента.

    Тот же, что показан в «Виде сегмента» и сводке: по эпизодам, со снятым
    гейтом. Раньше корпус считал свой — по детекциям и хешу снятия, который в
    закрытые сегменты не переносится, — и сегмент с «🟢 НОРМА, сняты ИИ» уходил
    модели как CAUTION «не пересматривать». None — у сегментов до v5.1.0.
    """
    parts = _parse_json(segment_row.get("report_summary_json"))
    if not isinstance(parts, dict) or not parts.get("version"):
        return None
    level = (parts.get("verdict") or {}).get("level")
    if level not in _LEVEL_VERDICT:
        return None
    return _LEVEL_VERDICT[level], level


def _remarks_from_summary(segment_row: dict) -> str | None:
    """Обнаружения для шапки — из замечаний сводки (тот же вердикт, что уровень).

    Уровень шапки берётся из частей, а список обнаружений раньше — из детекций и
    хеша снятия, которого в закрытых сегментах нет: «снято ИИ» терялось.
    """
    parts = _parse_json(segment_row.get("report_summary_json"))
    if not isinstance(parts, dict) or not parts.get("version"):
        return None
    from analytics.contract import cap_severity
    from analytics.serializer import _DATA_QUALITY_CHECKS, _INFO_CHECKS
    items = []
    for g in parts.get("remarks") or []:
        if g.get("kind") == "state":
            continue      # риск нагара — отдельной строкой состояния (_coking_from_summary)
        if g.get("kind") == "event" and g.get("scenario") == "RUN_TRIP":
            # v5.1.22 (P1-3): отметка панели, а не аналитики и не «уже случилось,
            # не тревога» — сегмент оборван аварийным остановом
            ev = (g.get("events") or [{}])[0]
            items.append(f"{g.get('name')} [CAUTION, панель, событие {ev.get('t_text') or '?'}"
                         + (f" — {ev['text']}" if ev.get("text") else "")
                         + "; сегмент закончился аварийным остановом, сам останов — "
                           "следующий сегмент]")
            continue
        if g.get("kind") == "event":
            when = ", ".join(e.get("t_text") or "?" for e in g.get("events") or [])
            items.append(f"{g.get('name')} [{g.get('severity') or 'CAUTION'}, аналитика, "
                         f"разовое событие {when} — уже случилось, не тревога]")
            continue
        tags = [cap_severity(g.get("scenario"), g.get("severity")) or "?",
                "панель" if g.get("source") == "panel" else "аналитика"]
        k, n = g.get("suppressed") or 0, g.get("count") or 0
        # Подпись с источником (v5.1.22): «снято по разбору от 14.10 03:36
        # (#35876)» — решение принято в другом сегменте, модель не должна писать,
        # что его снял ИИ в этом; старые части — «снято ИИ»
        if k:
            # Снятые до v5.1.22 (без источника) — отдельной подписью «снято ИИ»
            from analytics.serializer import supp_remark_text
            tags.append(supp_remark_text(g, n) or "снято ИИ")
        # «На конец» — про подтверждённый эпизод: до оговорки о неподтверждённых.
        # Стоя после неё, метка читалась как относящаяся к ним, и модель назвала
        # неподтверждённый эпизод «активным на конец периода» (#35872, v5.1.20)
        if g.get("at_end"):
            tags.append("активно на конец")
        u = g.get("unconfirmed") or 0
        at = ", ".join(g.get("unconfirmed_at") or [])
        at = f" ({at})" if at else ""
        if u and u >= n - k:
            tags.append(f"только по живым данным{at}: в отчёте ниже этого эпизода нет, "
                        f"итоговым расчётом не подтверждён, в вердикт не входит")
        elif u:
            tags.append(f"ещё {u} из {n} — только по живым данным{at}: в отчёте ниже их нет, "
                        f"итоговым расчётом не подтверждены, в вердикт не входят")
        items.append(f"{g.get('name')} [{', '.join(tags)}]")
    # Проваленные проверки последовательности — тоже строки «Замечаний»; обрыв
    # связи на вердикт не влияет (решение 11.10, 2а) — так и помечаем
    for c in parts.get("failed_checks") or []:
        det = (c.get("details") if isinstance(c, dict) else c) or "проверка не пройдена"
        ck = c.get("check") if isinstance(c, dict) else None
        # Сбой датчика оборотов (v5.1.21, В4) — данные, но не связь
        tag = ("связь, на вердикт не влияет" if ck == "subseg_data_coverage"
               else "качество данных, на вердикт не влияет" if ck in _DATA_QUALITY_CHECKS
               else "наблюдение, на вердикт не влияет" if ck in _INFO_CHECKS else "проверка")
        items.append(f"{det} [{tag}]")
    return "; ".join(items) if items else "нет"


def _coking_from_summary(segment_row: dict) -> str | None:
    """Риск нагара из частей сводки — перенесённое состояние с датами (v5.1.3).

    Модель писала «блок аналитики зафиксировал риск … по коду 2342» на машине,
    которая сутки стояла: риск накоплен при прошлой работе, кода панель не давала.
    """
    parts = _parse_json(segment_row.get("report_summary_json"))
    if not isinstance(parts, dict) or not parts.get("version"):
        return None
    try:
        from analytics.serializer import coking_state_text
        ck = parts.get("coking") or {}
        txt = coking_state_text(ck)
    except Exception:
        return None
    if not txt:
        return None
    if ck.get("remark"):
        return f"Риск нагара (накопленное состояние; в этом сегменте вырос — замечание): {txt}"
    return f"Риск нагара (накопленное состояние, не обнаружение этого сегмента): {txt}"


# Наработка в часах больше этого не бывает (≈ 23 года без остановки): такое число в
# запасных полях — секунды сегментов до v5.1.6 (v5.1.22, ревью verdict-1)
_EH_HOURS_MAX = 200_000.0


def _version_before(version: Any, ref: tuple[int, ...]) -> bool:
    """analytics_version строки раньше ref; не разобрать — False."""
    try:
        # У строк 34986–35268 версия записана с BOM («\ufeff4.9.62»)
        parts = tuple(int(x) for x in str(version).strip().lstrip("\ufeff").split(".")[:3])
    except (TypeError, ValueError):
        return False
    return bool(parts) and parts < ref


def _engine_hours_of(chars: Any, version: Any = None) -> float | None:
    """Наработка по счётчику на конец сегмента: engine_hours_end (v5.1.22), иначе
    последнее ENGINE_HOURS подсегментов (сегменты до v5.1.22), иначе на начало.
    None — нет в данных.

    v5.1.22, ревью verdict-1: до v5.1.6 (_apply_scale) запасные поля хранили
    СЕКУНДЫ — «48 809 700 ч» в шапке #35763. Сегмент старше 5.1.6 (version —
    analytics_version строки) или число, неправдоподобное для часов, —
    секунды → часы; и после пересчёта неправдоподобно — «нет в данных»."""
    chars = _parse_json(chars)
    if not isinstance(chars, dict):
        return None
    v = chars.get("engine_hours_end")
    if isinstance(v, (int, float)):
        return float(v) if 0 <= v <= _EH_HOURS_MAX else None
    legacy_sec = _version_before(version, (5, 1, 6))

    def _hours(x: float) -> float | None:
        h = float(x) / 3600.0 if (legacy_sec or x > _EH_HOURS_MAX) else float(x)
        return h if 0 <= h <= _EH_HOURS_MAX else None

    for sub in reversed(chars.get("subsegments") or []):
        eh = ((sub or {}).get("characteristics") or {}).get("ENGINE_HOURS") or {}
        v = eh.get("value_end") if isinstance(eh, dict) else None
        if isinstance(v, (int, float)):
            return _hours(v)
    v = chars.get("engine_hours_start")
    return _hours(v) if isinstance(v, (int, float)) else None


def _trip_of(segment_row: dict, parts: dict | None) -> tuple[str | None, str | None]:
    """(время, чем оборван) сегмента, оборванного аварийным остановом: из части
    сводки RUN_TRIP (в том числе дописанной поздней доработкой), иначе из
    characteristics_json.closed_by_trip; (None, None) — не оборван."""
    from analytics.labels import fmt_local, run_trip_text
    for g in (parts or {}).get("remarks") or []:
        if g.get("scenario") == "RUN_TRIP":
            ev = (g.get("events") or [{}])[0]
            return ev.get("t_text") or fmt_local(ev.get("t")), ev.get("text")
    chars = _parse_json(segment_row.get("characteristics_json"))
    trip = chars.get("closed_by_trip") if isinstance(chars, dict) else None
    if isinstance(trip, dict) and trip.get("t"):
        return fmt_local(trip["t"]), run_trip_text(trip)
    return None, None


def conclusion_facts(segment_row: dict) -> list[str]:
    """Факты шапки входа заключения — каждый из одного источника (v5.1.22, план
    «Общая шапка входа заключения»; P1-2, P1-3, P1-5).

    Порядок плана: 1 «Время во входе — МСК» (P1-6), 2 наработка,
    3 аварийный стоп: «Авария снята в … (через N с)» — по маске (часть mttr),
    код 40012 — только из части fault_code, никогда из cause_close (на сбросе
    встаёт другой код: #35861 1438 → 1248), 4 сегмент, оборванный аварией, —
    запрет «штатный / все параметры в норме», 5 «До останова: …» — лента
    отсчётов акта (P1-4/P1-5; заключение аварийного стопа пишет ИИ: без строки
    «Система (без ИИ)», по кнопке «Анализ», совет не получен).
    Наработка — счётчик панели: модель выдумывала её из событий журнала —
    «680 ч» у ДГУ №1 16.10 (длительность бита «Не в автомате»), «1340 ч 39 мин»
    и «955 моточасов» (счётчик пусков) у ДЭС №3 13.10 при 13 643 и 15 297 ч.
    """
    from analytics.labels import engine_hours_text, fmt_duration, fmt_local, tz_label
    parts = _parse_json(segment_row.get("report_summary_json"))
    parts = parts if isinstance(parts, dict) and parts.get("version") else None
    out: list[str] = [f"Время во входе — {tz_label()}."]

    eh = engine_hours_text(_engine_hours_of(segment_row.get("characteristics_json"),
                                            segment_row.get("analytics_version")))
    out.append(f"Наработка двигателя на конец периода: {eh} (счётчик панели)." if eh else
               "Наработка двигателя: нет в данных — не указывай её; часы у событий журнала — "
               "длительность события, счётчик пусков — не наработка.")

    m = (parts or {}).get("mttr")
    if isinstance(m, dict) and m.get("kind") == "SHUTDOWN_CLEARED" and segment_row.get("t_end"):
        t_end = fmt_local(segment_row.get("t_end"))
        if t_end:
            from analytics.labels import shutdown_cleared_how
            out.append(f"Авария снята в {t_end} (через {fmt_duration(m.get('sec'))}) — "
                       f"{shutdown_cleared_how(m)}.")
    fc = (parts or {}).get("fault_code")
    if isinstance(fc, dict) and (fc.get("text_regs") or fc.get("text")):
        out.append(f"{fc.get('text_regs') or fc.get('text')}; на конец сегмента код "
                   f"{fc.get('code')} не висит. Не называй его несброшенным и не советуй его сбрасывать.")

    t_trip, what = _trip_of(segment_row, parts)
    if t_trip:
        out.append(f"Сегмент закончился аварийным остановом панели ({what or 'код панели'} в {t_trip}). "
                   f"Он не штатный: не пиши «штатный», «все параметры в норме», «плановый "
                   f"контроль»; причину не выводи — её разбирает совет по акту следующего сегмента.")

    # 5. До останова — факты ленты отсчётов акта (v5.1.22, P1-4/P1-5)
    inc = _parse_json(segment_row.get("incident_json"))
    trace = inc.get("trace") if isinstance(inc, dict) else None
    if isinstance(trace, dict):
        try:
            from analytics.reconstructor import fall_facts_ru
            ff = fall_facts_ru(trace)
        except Exception:
            ff = None
        if ff:
            out.append(ff + " Это отсчёты панели с шагом опроса: что было между ними — не "
                            "утверждай; нагрузку и автомат бери отсюда.")
    return out


def build_claude_input(segment_row: dict) -> str:
    """Сформировать полный вход для Claude: шапка вердикта + реконструкция + report_md.

    v5.1.22: факты шапки (conclusion_facts) — наработка по счётчику, снятие
    аварии и код 40012 на закрытии, обрыв сегмента аварией."""
    chars_json = segment_row.get("characteristics_json")
    run_state = segment_row.get("run_state")
    report_md = segment_row.get("report_md") or ""

    detections = _extract_detections(chars_json)
    verdict, alarm_level = _verdict_from_summary(segment_row) or _extract_verdict(
        detections, _gate_suppressed(segment_row, detections),
        _stop_kind(chars_json),
    )
    remarks_line = _remarks_from_summary(segment_row)
    coking_line = _coking_from_summary(segment_row)
    run_state_label = (
        RUN_STATE_RU.get(run_state, str(run_state))
        if run_state is not None
        else "Неизвестно"
    )
    try:
        facts = conclusion_facts(segment_row)
    except Exception:
        facts = []

    header = (
        f"[ВЕРДИКТ БЛОКА АНАЛИТИКИ — НЕ ПЕРЕСМАТРИВАТЬ]\n"
        f"Режим: {run_state_label} (RUN_STATE={run_state})\n"
        f"Вердикт: {verdict}\n"
        f"Уровень тревоги: {alarm_level}\n"
        f"Обнаружения: {remarks_line or _fmt_detections(detections)}\n"
        + (f"{coking_line}\n" if coking_line else "")
        + "".join(f"{x}\n" for x in facts)
        + f"Задача: объясни зафиксированную картину. "
        f"Не пересматривай вердикт. Не ищи дополнительных проблем.\n"
        f"---\n\n"
    )
    incident_md = _format_incident(segment_row.get("incident_json"))
    gate_md = _format_gate_analyses(segment_row)
    chrono_md = _format_chronology(segment_row.get("chronology_json"))
    return header + incident_md + gate_md + chrono_md + report_md


def extract_verdict_alarm(segment_row: dict) -> tuple[str, str]:
    """Публичный метод: (verdict, alarm_level) для записи в БД."""
    from_summary = _verdict_from_summary(segment_row)
    if from_summary:
        return from_summary
    chars_json = segment_row.get("characteristics_json")
    detections = _extract_detections(chars_json)
    return _extract_verdict(detections, _gate_suppressed(segment_row, detections),
                            _stop_kind(chars_json))
