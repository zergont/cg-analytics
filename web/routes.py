# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""FastAPI роуты Web UI аналитики."""
import asyncio
import logging
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request, Form, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from analytics.serializer import RUN_STATE_RU as _RUN_STATE_LABELS
from db import analytics, source

logger = logging.getLogger(__name__)


def _parse_json(val, default=None, ctx: str = ""):
    """JSONB из asyncpg может прийти строкой — распарсить, при ошибке вернуть default."""
    import json as _json
    if val is None:
        return default
    if isinstance(val, str):
        try:
            return _json.loads(val)
        except Exception:
            logger.warning("Битый JSON%s", f" ({ctx})" if ctx else "")
            return default
    return val


def _active_dets(seg_row: dict) -> tuple[list[dict], bool]:
    """Детекции открытого сегмента + подавлены ли они вердиктом гейта «отменить».

    Возвращает (полный список детекций, suppressed). Фильтрацию аналитики
    при suppressed=True вызывающий делает сам — панельные severity считаются
    по полному списку.
    """
    from online.status_assembler import is_analytics_suppressed
    dets = _parse_json(seg_row.get("active_detections_json"), [], ctx="active_detections_json") or []
    dets = [d for d in dets if isinstance(d, dict)]
    return dets, is_analytics_suppressed(seg_row, dets)


router = APIRouter()
templates = Jinja2Templates(directory="web/templates")

# Версия приложения — читается из файла при каждом рендере шаблона.
# Меняется без перезапуска сервиса: достаточно обновить VERSION на диске.
_version_file = Path(__file__).parent.parent / "VERSION"
templates.env.globals["app_version"] = lambda: _version_file.read_text(encoding="utf-8").strip()

# Часовой пояс — строка, доступная во всех шаблонах.
# Обновляется через _apply_tz() при старте и при смене через UI.
from config import get_tz as _get_tz, set_tz as _set_tz
templates.env.globals["app_timezone"] = _get_tz().key


def _apply_tz(tz_name: str) -> None:
    """Применить новый TZ: обновить in-memory и глобал шаблонов."""
    _set_tz(tz_name)
    templates.env.globals["app_timezone"] = tz_name


# ── Главная страница ──────────────────────────────────────────────────────────

@router.get("/")
async def index():
    """Корень: онлайн-мониторинг — основной рабочий экран."""
    return RedirectResponse(url="/online", status_code=307)


# ── Анализ произвольного диапазона ───────────────────────────────────────────

@router.get("/analyze", response_class=HTMLResponse)
async def analyze_page(request: Request):
    from config import get_tz
    equipment = await analytics.get_equipment_registry()
    tz = get_tz()
    from datetime import datetime, timedelta
    now_local = datetime.now(tz)
    default_to   = now_local.strftime("%Y-%m-%dT%H:%M")
    default_from = (now_local - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M")
    return templates.TemplateResponse(request, "analyze.html", {
        "equipment": equipment,
        "default_from": default_from,
        "default_to":   default_to,
    })


@router.post("/analyze/stream")
async def analyze_stream(
    router_sn:     str = Form(...),
    equip_type:    str = Form(...),
    panel_id:      int = Form(...),
    ts_from_local: str = Form(...),
    ts_to_local:   str = Form(...),
):
    """SSE-стрим аналитики v2 с прогрессом по этапам."""
    import json as _json
    from datetime import datetime, timezone as _tz_mod
    from config import get_tz, settings as _cfg
    from analytics.config import AnalyticsConfig
    from analytics import source as _asrc
    from analytics.segmenter import segment as _segment
    from analytics.serializer import to_markdown, build_run_summary

    def _evt(data: dict) -> str:
        return f"data: {_json.dumps(data, ensure_ascii=False)}\n\n"

    async def _stream():
        try:
            import time as _time
            _t0 = _time.monotonic()
            tz = get_tz()
            fmt = "%Y-%m-%dT%H:%M"
            ts_from_utc = datetime.strptime(ts_from_local, fmt).replace(tzinfo=tz).astimezone(_tz_mod.utc)
            ts_to_utc   = datetime.strptime(ts_to_local,   fmt).replace(tzinfo=tz).astimezone(_tz_mod.utc)
            if ts_to_utc <= ts_from_utc:
                yield _evt({"stage": "error", "message": "Конец диапазона должен быть позже начала"})
                return

            # Конфигурация (слоистая привязка: пара controller×engine либо legacy kb_path)
            from analytics import binding as _binding
            _bnd = await analytics.get_equipment_binding(router_sn, equip_type, panel_id) or {}
            if not ((_bnd.get("controller_id") and _bnd.get("engine_id")) or _bnd.get("kb_path")):
                yield _evt({"stage": "error", "message": "Не задана привязка конфига (пара controller×engine или kb_path)"})
                return
            try:
                cfg = _binding.build_config(
                    _cfg.knowledge_base_path,
                    controller_id=_bnd.get("controller_id"),
                    engine_id=_bnd.get("engine_id"),
                    kb_path=_bnd.get("kb_path"),
                )
            except Exception as _e:
                yield _evt({"stage": "error", "message": f"Ошибка загрузки конфигурации: {_e}"})
                return

            eq = await analytics.get_equipment(router_sn, equip_type, panel_id) or {}
            engine_sn = eq.get("engine_sn") or ""

            # Этап 1: Аналоговая история
            yield _evt({"stage": "history", "status": "running", "label": "История аналогов"})
            history = await _asrc.get_whitelist_history(
                router_sn, equip_type, panel_id,
                ts_from_utc, ts_to_utc, cfg.whitelist_analog,
            )
            yield _evt({"stage": "history", "status": "done", "rows": len(history)})

            # Этап 2: Периоды состояний
            yield _evt({"stage": "enum", "status": "running", "label": "Периоды состояний"})
            enum_periods = await _asrc.get_enum_periods(
                router_sn, equip_type, panel_id, ts_from_utc, ts_to_utc,
                addrs=[40011, 40010],
            )
            yield _evt({"stage": "enum", "status": "done", "count": len(enum_periods)})

            # Этап 3: Fault-периоды
            yield _evt({"stage": "fault", "status": "running", "label": "События и неисправности"})
            fault_periods = await _asrc.get_fault_periods(
                router_sn, equip_type, panel_id, ts_from_utc, ts_to_utc,
                fault_addrs=cfg.whitelist_fault,
            )
            yield _evt({"stage": "fault", "status": "done", "count": len(fault_periods)})

            # Этап 4: Пропуски связи
            yield _evt({"stage": "gaps", "status": "running", "label": "Пропуски связи"})
            gaps = await _asrc.get_data_gaps(router_sn, equip_type, panel_id, ts_from_utc, ts_to_utc)
            yield _evt({"stage": "gaps", "status": "done", "count": len(gaps)})

            # Этап 5: Сегментация и расчёт
            yield _evt({"stage": "build", "status": "running", "label": "Сегментация и диагностика"})
            segments = _segment(
                enum_periods=enum_periods,
                history=history,
                fault_periods=fault_periods,
                gaps=gaps,
                cfg=cfg,
                router_sn=router_sn,
                equip_type=equip_type,
                panel_id=panel_id,
                engine_sn=engine_sn,
                ts_from=ts_from_utc,
                ts_to=ts_to_utc,
            )
            from analytics.runner import ANALYTICS_VERSION as _AV
            md = to_markdown(
                segments, router_sn, equip_type, panel_id,
                ts_from_utc, ts_to_utc, _AV, tz=tz,
            )
            summary = build_run_summary(segments)
            yield _evt({"stage": "build", "status": "done",
                        "segments": summary["segments_count"],
                        "detections": summary["detections_count"]})

            # Сохранение в БД (не блокируем стрим)
            run_id = None
            try:
                from analytics.serializer import to_json as _to_json
                from db.analytics import save_analysis_run as _save
                seg_json = _to_json(
                    segments, router_sn, equip_type, panel_id,
                    ts_from_utc, ts_to_utc, _AV,
                )
                run_id = await _save({
                    "router_sn": router_sn, "equip_type": equip_type, "panel_id": panel_id,
                    "engine_sn": engine_sn, "ts_from": ts_from_utc, "ts_to": ts_to_utc,
                    "analytics_version": _AV,
                    "segments_json": seg_json, "report_md": md,
                    "duration_ms": int((_time.monotonic() - _t0) * 1000),
                    **summary,
                })
            except Exception as _db_err:
                logger.warning("Ошибка сохранения в БД: %s", _db_err)

            yield _evt({
                "stage": "complete",
                "result": {
                    "markdown":          md,
                    "run_id":            run_id,
                    "history_rows":      len(history),
                    "enum_count":        len(enum_periods),
                    "fault_count":       len(fault_periods),
                    "gaps_count":        len(gaps),
                    "segments_count":    summary["segments_count"],
                    "detections_count":  summary["detections_count"],
                    "max_severity":      summary.get("max_severity"),
                    "data_quality_avg":  summary.get("data_quality_avg"),
                    "ts_from_local":     ts_from_local,
                    "ts_to_local":       ts_to_local,
                    "tz_name":           tz.key,
                    "analytics_version": _AV,
                },
            })
        except Exception as e:
            logger.exception("Ошибка аналитики: %s", e)
            yield _evt({"stage": "error", "message": str(e)})

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/analysis/{run_id}", response_class=HTMLResponse)
async def analysis_run_view(request: Request, run_id: str):
    """Просмотр результата аналитического прогона v2."""
    run = await analytics.get_analysis_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Прогон не найден")
    return templates.TemplateResponse(request, "analysis_run.html", {"run": run})


@router.get("/analysis/{run_id}/md")
async def analysis_run_md(run_id: str):
    """Скачать Markdown-отчёт прогона."""
    run = await analytics.get_analysis_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Прогон не найден")
    md = run.get("report_md") or "# Нет данных"
    filename = f"analysis_{run_id[:8]}.md"
    from fastapi.responses import Response
    return Response(
        content=md.encode("utf-8"),
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/history", response_class=HTMLResponse)
async def history_index(request: Request):
    """Главная страница истории: список оборудования с прогонами."""
    equipment = await analytics.list_equipment_with_runs()
    return templates.TemplateResponse(request, "history_index.html", {
        "equipment": equipment,
    })


@router.get("/history/{router_sn}/{equip_type}/{panel_id}", response_class=HTMLResponse)
async def analysis_history(
    request: Request,
    router_sn: str,
    equip_type: str,
    panel_id: int,
):
    """История аналитических прогонов для одной ГУ."""
    runs = await analytics.list_analysis_runs(router_sn, equip_type, panel_id, limit=100)
    return templates.TemplateResponse(request, "analysis_history.html", {
        "router_sn": router_sn,
        "equip_type": equip_type,
        "panel_id": panel_id,
        "runs": runs,
    })


@router.post("/history/delete/{run_id}")
async def delete_analysis_run(run_id: str, request: Request):
    """Удалить прогон и вернуться в историю оборудования."""
    # Читаем router_sn/equip_type/panel_id из формы чтобы вернуться на нужную страницу
    form = await request.form()
    back = form.get("back", "/history")
    await analytics.delete_analysis_run(run_id)
    return RedirectResponse(url=back, status_code=303)


# Обратная совместимость: старый URL /history-v2/... → редирект на новый
@router.get("/history-v2/{router_sn}/{equip_type}/{panel_id}", response_class=HTMLResponse)
async def analysis_history_compat(
    request: Request, router_sn: str, equip_type: str, panel_id: int,
):
    from fastapi.responses import RedirectResponse as _RR
    return _RR(url=f"/history/{router_sn}/{equip_type}/{panel_id}", status_code=301)


@router.get("/log", response_class=HTMLResponse)
async def log_page(request: Request):
    from config import get_tz
    return templates.TemplateResponse(request, "log.html", {"tz_name": get_tz().key})


@router.get("/api/log")
async def api_log(n: int = 200):
    from web.log_buffer import get_entries
    return JSONResponse(get_entries(min(n, 500)))


@router.post("/api/log/clear")
async def api_log_clear():
    from web.log_buffer import clear_buffer
    clear_buffer()
    return JSONResponse({"ok": True})


def _format_operation_rules(rules: dict) -> list[str]:
    """Сформировать компактную сводку operation_rules для промпта ИИ.

    Извлекает только критически важные числовые пороги, не дампит весь JSON.
    """
    lines: list[str] = []

    meta = rules.get("metadata", {})
    if meta.get("engine_model"):
        lines.append(f"Модель: {meta['engine_model']} / {meta.get('controller', '')}")

    def _val(obj) -> str | None:
        """Извлечь значение: из {value:...} или напрямую."""
        if obj is None:
            return None
        if isinstance(obj, dict):
            v = obj.get("value")
            return str(v) if v is not None else None
        return str(obj) if obj else None

    def _row(label: str, val_str: str | None) -> str | None:
        return f"  - {label}: **{val_str}**" if val_str else None

    normal = rules.get("normal_operation", {})

    # Давление масла
    oil = normal.get("oil_pressure_kpa", {})
    oil_rows = [
        _row("Норма при номинале, мин (кПа)",    _val(oil.get("rated", {}).get("min"))),
        _row("Предупреждение (кПа)",              _val(oil.get("warning_threshold_rated_kpa"))),
        _row("Аварийный останов (кПа)",           _val(oil.get("shutdown_threshold_rated_kpa"))),
    ]
    if any(oil_rows):
        lines += ["", "**Давление масла**"] + [r for r in oil_rows if r]

    # Температура ОЖ
    cwt = normal.get("coolant_temperature_c", {})
    cwt_rows = [
        _row("Норма мин–макс (°C)", f"{cwt.get('normal', {}).get('min')}–{cwt.get('normal', {}).get('max')}"
             if cwt.get("normal", {}).get("min") and cwt.get("normal", {}).get("max") else None),
        _row("Предупреждение (°C)",        _val(cwt.get("warning_threshold"))),
        _row("Останов с охлаждением (°C)", _val(cwt.get("shutdown_with_cooldown_threshold"))),
        _row("Аварийный останов (°C)",     _val(cwt.get("shutdown_threshold"))),
    ]
    if any(cwt_rows):
        lines += ["", "**Температура охлаждающей жидкости**"] + [r for r in cwt_rows if r]

    # Температура масла
    olt = normal.get("oil_temperature_c", {})
    olt_rows = [
        _row("Норма макс (°C)",         _val(olt.get("normal", {}).get("max"))),
        _row("Предупреждение (°C)",     _val(olt.get("warning_threshold"))),
        _row("Аварийный останов (°C)",  _val(olt.get("shutdown_threshold"))),
    ]
    if any(olt_rows):
        lines += ["", "**Температура масла**"] + [r for r in olt_rows if r]

    # Обороты
    rpm = normal.get("engine_speed_rpm", {})
    rpm_rows = [
        _row("Номинал (RPM)",      _val(rpm.get("rated", {}).get("value"))),
        _row("Заброс — останов",   _val(rpm.get("overspeed_shutdown"))),
    ]
    if any(rpm_rows):
        lines += ["", "**Обороты двигателя**"] + [r for r in rpm_rows if r]

    # АКБ
    bat = normal.get("battery_voltage_vdc", {})
    bat_rows = [
        _row("Предупреждение низкого (VDC)", _val(bat.get("warning_low_running"))),
        _row("Предупреждение высокого (VDC)", _val(bat.get("warning_high"))),
    ]
    if any(bat_rows):
        lines += ["", "**Напряжение АКБ**"] + [r for r in bat_rows if r]

    # Нагрузка
    load = normal.get("load_pct", {})
    load_rows = [
        _row("Рекомендуемый минимум (%)", _val(load.get("min_recommended_load_pct"))),
    ]
    if any(load_rows):
        lines += ["", "**Нагрузка**"] + [r for r in load_rows if r]

    # ТО
    maint = rules.get("maintenance_intervals", {})
    if maint:
        maint_rows = [
            _row("Замена масла (м/ч)", _val(maint.get("engine_oil_change_hours"))),
            _row("Замена масла (мес.)", _val(maint.get("engine_oil_change_months"))),
            _row("Регулировка клапанов (м/ч)", _val(maint.get("valve_adjustment_hours"))),
            _row("Смена ОЖ (м/ч)", _val(maint.get("coolant_change_hours"))),
        ]
        if any(maint_rows):
            lines += ["", "**Интервалы ТО**"] + [r for r in maint_rows if r]

    # Пуск
    startup = rules.get("startup_sequence", {})
    start_rows = [
        _row("Попыток запуска",         _val(startup.get("max_crank_attempts"))),
        _row("Время кручения (с)",      _val(startup.get("max_crank_time_sec"))),
        _row("Перерыв между попытками (с)", _val(startup.get("rest_between_cranks_sec"))),
    ]
    if any(start_rows):
        lines += ["", "**Последовательность пуска**"] + [r for r in start_rows if r]

    return lines


# ── ИИ-агент: анализ телеметрии ──────────────────────────────────────────────

class _AgentRequest(BaseModel):
    markdown: str


@router.post("/analyze/run")
async def analyze_run_agent(req: _AgentRequest):
    """SSE-стрим: отправить MD-пакет в локальную LLM и получить заключение."""
    import json as _json
    from fastapi.responses import StreamingResponse as _SR
    from llm.client import stream_analysis

    async def _generate():
        try:
            async for token in stream_analysis(req.markdown):
                yield f"data: {_json.dumps({'token': token}, ensure_ascii=False)}\n\n"
        except Exception as e:
            logger.exception("Ошибка LLM: %s", e)
            yield f"data: {_json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"
        finally:
            yield f"data: {_json.dumps({'done': True})}\n\n"

    return _SR(
        _generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _fmt_dur(seconds: float | None, is_open: bool = False) -> str:
    """Отформатировать длительность в секундах как читаемую строку."""
    if is_open:
        return "активно"
    if seconds is None:
        return "—"
    s = int(seconds)
    if s < 60:
        return f"{s} с"
    if s < 3600:
        return f"{s // 60} мин {s % 60} с"
    return f"{s // 3600} ч {(s % 3600) // 60} мин"


def _build_analysis_md(
    router_sn, equip_type, panel_id, eq, kb_path,
    ts_from_utc, ts_to_utc, duration_h, tz,
    reg_stats, enum_periods, fault_periods,
    operation_rules: dict | None = None,
) -> str:
    """Сформировать Markdown-пакет данных для передачи в ИИ."""
    fmt_dt   = "%Y-%m-%d %H:%M"
    fmt_t    = "%H:%M:%S"
    tz_label = tz.key

    def local(dt) -> str:
        return dt.astimezone(tz).strftime(fmt_dt)

    def local_t(dt) -> str:
        return dt.astimezone(tz).strftime(fmt_t)

    lines: list[str] = []

    # ── Заголовок ──────────────────────────────────────────────────────────────
    name = eq.get("name") or ""
    mfr  = eq.get("manufacturer") or ""
    mdl  = eq.get("model") or ""
    eng  = eq.get("engine_sn") or ""
    lines += [
        "# Пакет данных телеметрии",
        "",
        "## Оборудование",
        f"**{name}** | `{router_sn}/{equip_type}/{panel_id}`",
    ]
    if mfr or mdl:
        lines.append(f"Производитель: {mfr} {mdl}".strip())
    if eng:
        lines.append(f"Двигатель s/n: {eng}")
    if kb_path:
        lines.append(f"База знаний: `{kb_path}`")

    # ── Период ────────────────────────────────────────────────────────────────
    lines += [
        "",
        "## Период анализа",
        f"**{local(ts_from_utc)} — {local(ts_to_utc)}** {tz_label}  ",
        f"Длительность: **{duration_h:.1f} ч** ({duration_h * 60:.0f} мин)",
    ]

    # ── Аналоговые параметры ──────────────────────────────────────────────────
    lines += [
        "",
        f"## Аналоговые параметры ({len(reg_stats)} регистров)",
    ]
    if reg_stats:
        lines.append("| Адрес | Параметр | Ед. | Мин | Макс | Ср.взв. | Измерений |")
        lines.append("|------:|----------|-----|----:|-----:|--------:|----------:|")
        for addr in sorted(reg_stats.keys()):
            s = reg_stats[addr]
            lines.append(
                f"| {addr} | {s['name'] or '—'} | {s['unit'] or ''} "
                f"| {s['min']} | {s['max']} | {s['wmean']} | {s['count']} |"
            )
    else:
        lines.append("_Нет данных аналоговых регистров за период_")

    # ── Журнал состояний (enum) — периоды ────────────────────────────────────
    lines += [
        "",
        f"## Журнал состояний — enum ({len(enum_periods)} периодов)",
    ]
    if enum_periods:
        lines.append("| Начало | Конец | Адрес | Регистр | Состояние | Длительность |")
        lines.append("|--------|-------|------:|---------|-----------|-------------|")
        for p in enum_periods:
            t_start  = local_t(p["state_start"])
            t_end    = local_t(p["state_end"]) if p.get("state_end") else "—"
            reg_name = p.get("name_ru") or f"addr {p['addr']}"
            label    = p.get("label") or str(p.get("value", "?"))
            dur      = _fmt_dur(p.get("duration_sec"), is_open=p.get("state_end") is None)
            lines.append(
                f"| {t_start} | {t_end} | {p['addr']} | {reg_name} | {label} | {dur} |"
            )
    else:
        lines.append("_Нет данных за период_")

    # ── События (fault_history) ───────────────────────────────────────────────
    lines += [
        "",
        f"## События ({len(fault_periods)} записей)",
    ]
    if fault_periods:
        lines.append("| Начало | Конец | Адрес | Бит | Событие | Серьёзность | Длительность |")
        lines.append("|--------|-------|------:|----:|---------|-------------|-------------|")
        for f in fault_periods:
            t_start = local_t(f["fault_start"])
            t_end   = local_t(f["fault_end"]) if f.get("fault_end") else "—"
            name    = f.get("fault_name_ru") or f.get("fault_name") or f"addr={f['addr']} бит={f['bit']}"
            sev     = f.get("severity") or "—"
            dur     = _fmt_dur(f.get("duration_sec"), is_open=f.get("fault_end") is None)
            lines.append(
                f"| {t_start} | {t_end} | {f['addr']} | {f['bit']} | {name} | {sev} | {dur} |"
            )
    else:
        lines.append("_Событий в периоде не зафиксировано_")

    # ── Правила эксплуатации из KB (компактная выжимка) ──────────────────────
    if operation_rules:
        lines += [
            "",
            "## Правила эксплуатации (ключевые пороги)",
        ]
        lines += _format_operation_rules(operation_rules)

    return "\n".join(lines)


# ── Knowledge Base ────────────────────────────────────────────────────────────

@router.get("/knowledge", response_class=HTMLResponse)
async def knowledge_page(request: Request):
    from config import settings
    equipment_dir = settings.knowledge_base_path / "equipment"

    models = []
    if equipment_dir.exists():
        for kb_dir in sorted(equipment_dir.iterdir()):
            if not kb_dir.is_dir():
                continue
            reg_count = _count_lines(kb_dir / "register_map.jsonl")
            fault_count = _count_lines(kb_dir / "fault_bitmap_map.jsonl")
            has_rules = (kb_dir / "operation_rules.json").exists()
            pdf_count = len(list((kb_dir / "docs").glob("*.pdf"))) if (kb_dir / "docs").exists() else 0

            # Справочник кодов неисправностей
            fault_ref_path = kb_dir / "pcc3300_fault_codes.json"
            fault_ref_codes = 0
            if fault_ref_path.exists():
                try:
                    import json as _json
                    data = _json.loads(fault_ref_path.read_text(encoding="utf-8-sig"))
                    fault_ref_codes = data.get("statistics", {}).get("total_codes", 0) \
                                      or len(data.get("fault_codes", []))
                except Exception:
                    fault_ref_codes = -1  # файл есть, но не распарсился

            models.append({
                "kb_path": kb_dir.name,
                "registers": reg_count,
                "faults": fault_count,
                "has_rules": has_rules,
                "pdfs": pdf_count,
                "has_fault_ref": fault_ref_path.exists(),
                "fault_ref_codes": fault_ref_codes,
                "has_faultref_html": bool(list(kb_dir.glob("*.html"))),
            })

    return templates.TemplateResponse(request, "knowledge.html", {"models": models})



# ── KB: HTML-справочник кодов неисправностей ─────────────────────────────────

@router.get("/knowledge/{kb_path}/faultref", response_class=HTMLResponse)
async def kb_faultref(kb_path: str):
    """Открыть HTML-справочник кодов неисправностей прямо в браузере."""
    base = _kb_base(kb_path)
    html_files = sorted(base.glob("*.html"))
    if not html_files:
        raise HTTPException(status_code=404, detail="HTML-справочник не найден в этой KB")
    return HTMLResponse(content=html_files[0].read_text(encoding="utf-8"))


# ── KB: управление файлами ────────────────────────────────────────────────────

_KB_NAME_RE = re.compile(r"^[a-z0-9_]+$")
_KB_DATA_FILES = [
    "register_map.jsonl",
    "fault_bitmap_map.jsonl",
    "enum_map.json",
    "operation_rules.json",
    "pcc3300_fault_codes.json",   # детерминированный справочник кодов неисправностей
]
_KB_ALLOWED_EXT = {".jsonl", ".json", ".pdf"}


def _kb_base(kb_path: str) -> Path:
    """Вернуть абсолютный путь к папке KB с валидацией имени."""
    from config import settings as _cfg
    if not _KB_NAME_RE.match(kb_path):
        raise HTTPException(status_code=400, detail="Недопустимое имя KB")
    p = _cfg.knowledge_base_path / "equipment" / kb_path
    if not p.exists():
        raise HTTPException(status_code=404, detail="KB не найдена")
    return p


def _safe_filename(filename: str) -> str:
    """Валидировать имя файла — без path-traversal."""
    if not filename or "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Недопустимое имя файла")
    return filename


def _fmt_size(b: int) -> str:
    if b < 1024:
        return f"{b} Б"
    if b < 1024 * 1024:
        return f"{b / 1024:.1f} КБ"
    return f"{b / (1024 * 1024):.1f} МБ"


@router.get("/knowledge/{kb_path}/files", response_class=JSONResponse)
async def kb_files(kb_path: str):
    """Список файлов KB (для модального окна)."""
    base = _kb_base(kb_path)

    data_files = []
    for name in _KB_DATA_FILES:
        p = base / name
        data_files.append({
            "name": name,
            "exists": p.exists(),
            "size_fmt": _fmt_size(p.stat().st_size) if p.exists() else None,
        })

    pdf_files = []
    docs_dir = base / "docs"
    if docs_dir.exists():
        for pdf in sorted(docs_dir.glob("*.pdf")):
            pdf_files.append({"name": pdf.name, "size_fmt": _fmt_size(pdf.stat().st_size)})

    analytics_files = []
    analytics_dir = base / "analytics"
    for name in _ANALYTICS_CONFIG_FILES:
        p = analytics_dir / name
        analytics_files.append({
            "name": name,
            "exists": p.exists(),
            "size_fmt": _fmt_size(p.stat().st_size) if p.exists() else None,
        })

    return JSONResponse({"data_files": data_files, "pdf_files": pdf_files, "analytics_files": analytics_files})


@router.get("/knowledge/{kb_path}/download/{filename}")
async def kb_download(kb_path: str, filename: str):
    """Скачать файл KB."""
    base = _kb_base(kb_path)
    _safe_filename(filename)

    # Сначала ищем в корне KB, затем в docs/
    for candidate in [base / filename, base / "docs" / filename]:
        if candidate.exists() and candidate.is_file():
            # Проверяем что файл внутри папки KB
            try:
                candidate.resolve().relative_to(base.resolve())
            except ValueError:
                raise HTTPException(status_code=403, detail="Доступ запрещён")
            return FileResponse(path=str(candidate), filename=filename)

    raise HTTPException(status_code=404, detail="Файл не найден")


@router.post("/knowledge/{kb_path}/upload")
async def kb_upload(kb_path: str, file: UploadFile = File(...)):
    """Загрузить или заменить файл KB."""
    base = _kb_base(kb_path)
    filename = _safe_filename(file.filename or "")

    suffix = Path(filename).suffix.lower()
    if suffix not in _KB_ALLOWED_EXT:
        raise HTTPException(status_code=400, detail=f"Недопустимое расширение: {suffix}")

    if suffix == ".pdf":
        dest = base / "docs" / filename
        dest.parent.mkdir(exist_ok=True)
    elif filename in _KB_DATA_FILES:
        dest = base / filename
    else:
        raise HTTPException(
            status_code=400,
            detail=f"Неизвестный файл данных: {filename}. Разрешены: {', '.join(_KB_DATA_FILES)}",
        )

    content = await file.read()

    # Валидация JSON-файлов (кроме .jsonl — построчные)
    if suffix == ".json":
        try:
            import json as _json
            _json.loads(content)
        except Exception as _je:
            raise HTTPException(status_code=400, detail=f"Невалидный JSON: {_je}")

    dest.write_bytes(content)
    logger.info("KB upload: %s / %s (%d байт)", kb_path, filename, len(content))
    return RedirectResponse(url="/knowledge", status_code=303)


@router.post("/knowledge/{kb_path}/delete")
async def kb_delete_file(kb_path: str, filename: str = Form(...)):
    """Удалить PDF из docs/."""
    base = _kb_base(kb_path)
    _safe_filename(filename)

    if not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Удалять можно только PDF-файлы")

    p = base / "docs" / filename
    if not p.exists():
        raise HTTPException(status_code=404, detail="Файл не найден")

    p.unlink()
    logger.info("KB delete PDF: %s / docs/%s", kb_path, filename)
    return RedirectResponse(url="/knowledge", status_code=303)



# ── Настройки ─────────────────────────────────────────────────────────────────

@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    from config import settings as cfg, TIMEZONE_CHOICES, get_tz
    from llm.client import get_llm_settings
    from corpus.settings import get_claude_settings
    registry = await analytics.get_equipment_registry()
    kb_list = _list_kb_paths(cfg.knowledge_base_path / "equipment")
    controller_list = _list_layer_dirs(cfg.knowledge_base_path / "controllers")
    engine_list = _list_layer_dirs(cfg.knowledge_base_path / "engines")
    corpus_auto       = await analytics.get_app_setting("corpus_auto_analyze",       "false")
    qwen_auto         = await analytics.get_app_setting("qwen_auto_analyze",         "false")
    analytics_verify  = await analytics.get_app_setting("analytics_verify_on_close", "false")
    status_line_interval_min = int(
        await analytics.get_app_setting("status_line_interval_min", "1")
    )
    data_stale_threshold_sec = int(
        await analytics.get_app_setting("data_stale_threshold_sec", "90")
    )
    from llm.router import get_all as _get_router, TASKS as _TASKS, TASK_HINTS as _HINTS
    return templates.TemplateResponse(request, "settings.html", {
        "settings": cfg,
        "registry": registry,
        "kb_list": kb_list,
        "controller_list": controller_list,
        "engine_list": engine_list,
        "timezone_choices": TIMEZONE_CHOICES,
        "current_timezone": get_tz().key,
        "llm": get_llm_settings(),
        "claude": get_claude_settings(),
        "corpus_auto_analyze":       corpus_auto      == "true",
        "qwen_auto_analyze":         qwen_auto        == "true",
        "analytics_verify_on_close": analytics_verify == "true",
        "status_line_interval_min":  status_line_interval_min,
        "data_stale_threshold_sec":  data_stale_threshold_sec,
        "ai_routing":   _get_router(),
        "ai_task_meta": {k: {"label": v[0], "hint": _HINTS.get(k, "")} for k, v in _TASKS.items()},
    })


@router.post("/settings/status-line-interval")
async def update_status_line_interval(interval_min: int = Form(...)):
    """Сохранить интервал обновления статус-строки ИИ-оператора."""
    interval_min = max(1, min(60, interval_min))   # зажимаем в [1, 60]
    await analytics.set_app_setting("status_line_interval_min", str(interval_min))
    logger.info("ИИ-оператор: интервал статус-строки → %d мин", interval_min)
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/settings/data-stale-threshold")
async def update_data_stale_threshold(stale_sec: int = Form(...)):
    """Порог устаревания телеметрии: старше → data_stale, статус/гейт замирают."""
    stale_sec = max(30, min(3600, stale_sec))   # зажимаем в [30с, 1ч]
    await analytics.set_app_setting("data_stale_threshold_sec", str(stale_sec))
    logger.info("Порог устаревания телеметрии → %d сек", stale_sec)
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/settings/llm")
async def update_llm_settings(
    llm_provider:    str   = Form("ollama"),
    llm_base_url:    str   = Form(...),
    llm_model:       str   = Form(...),
    llm_temperature: float = Form(...),
    llm_num_ctx:     int   = Form(...),
    llm_stream:      str   = Form(""),   # checkbox: "on" если отмечен, "" если нет
):
    """Сохранить настройки LLM и применить без перезапуска."""
    from llm.client import apply_llm_settings, PROVIDERS
    if llm_provider not in PROVIDERS:
        llm_provider = "ollama"
    stream = llm_stream == "on"
    apply_llm_settings(llm_base_url, llm_model, llm_temperature, llm_num_ctx,
                       stream=stream, provider=llm_provider)
    await analytics.set_app_setting("llm_provider",    llm_provider)
    await analytics.set_app_setting("llm_base_url",    llm_base_url)
    await analytics.set_app_setting("llm_model",       llm_model)
    await analytics.set_app_setting("llm_temperature", str(llm_temperature))
    await analytics.set_app_setting("llm_num_ctx",     str(llm_num_ctx))
    await analytics.set_app_setting("llm_stream",      "true" if stream else "false")
    logger.info("LLM настройки сохранены: provider=%s model=%s num_ctx=%d stream=%s",
                llm_provider, llm_model, llm_num_ctx, stream)
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/settings/ai-routing")
async def update_ai_routing(request: Request):
    """Сохранить маршрутизацию AI-задач (провайдер + промпт для каждой задачи)."""
    from llm.router import apply_task, TASKS as _TASKS
    form = await request.form()
    for task_id in _TASKS:
        provider = str(form.get(f"ai_{task_id}_provider", "llm")).strip()
        prompt   = str(form.get(f"ai_{task_id}_prompt",   "")).strip()
        apply_task(task_id, provider, prompt)
        await analytics.set_app_setting(f"ai_task_{task_id}_provider", provider)
        await analytics.set_app_setting(f"ai_task_{task_id}_prompt",   prompt)
    logger.info("AI routing сохранён (%d задач)", len(_TASKS))
    return RedirectResponse(url="/settings#ai-routing", status_code=303)


@router.get("/ai-playground", response_class=HTMLResponse)
async def ai_playground_page(request: Request):
    """Страница ручного запроса к AI (playground)."""
    from llm.client import get_llm_settings
    from corpus.settings import get_claude_settings
    return templates.TemplateResponse(request, "ai_playground.html", {
        "llm": get_llm_settings(),
        "claude": get_claude_settings(),
    })


@router.post("/ai-playground/run")
async def ai_playground_run(request: Request):
    """Выполнить запрос к AI и вернуть ответ (streaming SSE)."""
    import json as _json
    from fastapi.responses import StreamingResponse

    form = await request.form()
    provider      = str(form.get("provider", "llm"))
    model_override= str(form.get("model", "")).strip()
    system_prompt = str(form.get("system_prompt", "")).strip()
    user_message  = str(form.get("user_message", "")).strip()
    use_stream    = form.get("stream") == "on"

    async def _generate():
        raw_chunks: list[str] = []
        try:
            if provider == "llm":
                from llm.client import chat_stream
                # chat_stream знает текущего провайдера (Ollama/LM Studio) и ретраит сам
                async for token in chat_stream(
                    system_prompt, user_message,
                    model=model_override or None, stream=use_stream,
                ):
                    raw_chunks.append(token)
                    yield f"data: {_json.dumps({'token': token})}\n\n"

            else:  # api (Claude)
                import anthropic, httpx as _httpx
                from corpus.settings import get_claude_settings
                from config import settings as app_settings
                claude_cfg = get_claude_settings()
                model = model_override or claude_cfg["model"]
                _http = _httpx.AsyncClient(proxy=claude_cfg["proxy"]) if claude_cfg.get("proxy") else None
                client = anthropic.AsyncAnthropic(
                    api_key=app_settings.anthropic_api_key,
                    http_client=_http,
                )
                if use_stream:
                    async with client.messages.stream(
                        model=model,
                        max_tokens=claude_cfg["max_tokens"],
                        system=system_prompt,
                        messages=[{"role": "user", "content": user_message}],
                    ) as stream:
                        async for token in stream.text_stream:
                            raw_chunks.append(token)
                            yield f"data: {_json.dumps({'token': token})}\n\n"
                    raw_msg = await stream.get_final_message()
                    raw_chunks.append(
                        f"\n\n[raw]\n{_json.dumps({'usage': dict(raw_msg.usage.__dict__)}, ensure_ascii=False, indent=2, default=str)}"
                    )
                else:
                    response = await client.messages.create(
                        model=model,
                        max_tokens=claude_cfg["max_tokens"],
                        system=system_prompt,
                        messages=[{"role": "user", "content": user_message}],
                    )
                    content = "".join(b.text for b in response.content if hasattr(b, "text"))
                    raw_chunks.append(content)
                    yield f"data: {_json.dumps({'token': content})}\n\n"
                    raw_chunks.append(
                        f"\n\n[raw]\n{_json.dumps({'usage': dict(response.usage.__dict__)}, ensure_ascii=False, indent=2, default=str)}"
                    )

            yield f"data: {_json.dumps({'done': True, 'raw': ''.join(raw_chunks)})}\n\n"

        except Exception as exc:
            err = repr(exc)
            logger.error("ai-playground: ошибка: %s", err)
            yield f"data: {_json.dumps({'error': err, 'done': True, 'raw': err})}\n\n"

    return StreamingResponse(_generate(), media_type="text/event-stream")


@router.post("/settings/claude")
async def update_claude_settings(
    claude_model:          str = Form(...),
    claude_max_tool_calls: int = Form(...),
    claude_max_tokens:     int = Form(...),
    claude_proxy:          str = Form(""),
):
    """Сохранить настройки Claude API и применить без перезапуска."""
    from corpus.settings import apply_claude_settings
    apply_claude_settings(claude_model, claude_max_tool_calls, claude_max_tokens, claude_proxy)
    await analytics.set_app_setting("claude_model",          claude_model)
    await analytics.set_app_setting("claude_max_tool_calls", str(claude_max_tool_calls))
    await analytics.set_app_setting("claude_max_tokens",     str(claude_max_tokens))
    await analytics.set_app_setting("claude_proxy",          claude_proxy)
    logger.info("Claude API настройки сохранены: model=%s", claude_model)
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/settings/qwen-toggle")
async def qwen_toggle(enabled: str = Form("off")):
    """Включить / выключить авто-анализ Qwen-конвейера."""
    value = "true" if enabled == "on" else "false"
    await analytics.set_app_setting("qwen_auto_analyze", value)
    if value == "true":
        from corpus.qwen_worker import get_worker as get_qwen_worker
        w = get_qwen_worker()
        if w:
            pending = await w.enqueue_pending()
            if pending:
                logger.info("qwen авто-анализ включён: %d сегментов добавлено в очередь", pending)
    logger.info("qwen авто-анализ: %s", "включён" if value == "true" else "выключен")
    return RedirectResponse(url="/settings", status_code=303)



@router.post("/settings/analytics-verify-toggle")
async def analytics_verify_toggle(enabled: str = Form("off")):
    """Включить / выключить финальную проверку характеристик при закрытии сегмента."""
    value = "true" if enabled == "on" else "false"
    await analytics.set_app_setting("analytics_verify_on_close", value)
    logger.info(
        "Финальная проверка характеристик: %s",
        "включена" if value == "true" else "выключена",
    )
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/settings/corpus-toggle")
async def corpus_toggle(enabled: str = Form("off")):
    """Включить / выключить авто-анализ Claude-конвейера."""
    value = "true" if enabled == "on" else "false"
    await analytics.set_app_setting("corpus_auto_analyze", value)
    if value == "true":
        from corpus.worker import get_worker
        w = get_worker()
        if w:
            pending = await w.enqueue_pending()
            if pending:
                logger.info("corpus авто-анализ включён: %d сегментов добавлено в очередь", pending)
    logger.info("corpus авто-анализ: %s", "включён" if value == "true" else "выключен")
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/settings/timezone")
async def update_timezone(timezone_name: str = Form(...)):
    """Сменить часовой пояс разбивки суток без перезапуска сервиса."""
    from config import set_tz, TIMEZONE_CHOICES
    valid_keys = {tz for tz, _ in TIMEZONE_CHOICES}
    if timezone_name not in valid_keys:
        raise HTTPException(status_code=400, detail=f"Неизвестный часовой пояс: {timezone_name}")
    _apply_tz(timezone_name)
    await analytics.set_app_setting("timezone", timezone_name)
    logger.info("Часовой пояс изменён на %s", timezone_name)
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/settings/equipment/update")
async def update_equipment(
    router_sn: str = Form(...),
    equip_type: str = Form(...),
    panel_id: int = Form(...),
    manufacturer: str = Form(""),
    model: str = Form(""),
    engine_sn: str = Form(""),
    name: str = Form(""),
    kb_path: str = Form(""),
    controller_id: str = Form(""),
    engine_id: str = Form(""),
):
    """Обновить метаданные оборудования в реестре аналитики."""
    await analytics.upsert_equipment({
        "router_sn": router_sn,
        "equip_type": equip_type,
        "panel_id": panel_id,
        "manufacturer": manufacturer or None,
        "model": model or None,
        "engine_sn": engine_sn or None,
        "name": name or None,
        "kb_path": kb_path or None,
        "controller_id": controller_id or None,
        "engine_id": engine_id or None,
    })
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/settings/equipment/toggle")
async def toggle_equipment(
    router_sn: str = Form(...),
    equip_type: str = Form(...),
    panel_id: int = Form(...),
    active: str = Form("off"),
):
    await analytics.set_equipment_active(router_sn, equip_type, panel_id, active == "on")
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/settings/equipment/delete")
async def delete_equipment(
    router_sn: str = Form(...),
    equip_type: str = Form(...),
    panel_id: int = Form(...),
):
    await analytics.delete_equipment(router_sn, equip_type, panel_id)
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/settings/equipment/clear")
async def clear_equipment():
    await analytics.clear_equipment_registry()
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/settings/sync")
async def sync_equipment():
    """Синхронизировать реестр аналитики с основной БД.

    Добавляет новые устройства и заполняет пустые поля из источника,
    но не затирает данные уже введённые вручную в реестре аналитики.
    """
    try:
        equipment = await source.get_active_equipment()
        for eq in equipment:
            await analytics.sync_equipment_from_source(eq)
    except Exception as e:
        logger.exception("Ошибка синхронизации: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    return RedirectResponse(url="/settings", status_code=303)


# ── Helpers ───────────────────────────────────────────────────────────────────

_ANALYTICS_CONFIG_FILES = [
    "mapping.yaml",
    "thresholds.yaml",
    "zones.yaml",
    "segmentation.yaml",
    "detectors.yaml",
    "fault_matrix.yaml",
]


@router.get("/knowledge/{kb_path}/analytics-config", response_class=JSONResponse)
async def analytics_config_files(kb_path: str):
    """Список YAML-конфигов аналитики для KB."""
    base = _kb_base(kb_path)
    analytics_dir = base / "analytics"
    files = []
    for name in _ANALYTICS_CONFIG_FILES:
        p = analytics_dir / name
        files.append({
            "name": name,
            "exists": p.exists(),
            "size_fmt": _fmt_size(p.stat().st_size) if p.exists() else None,
        })
    return JSONResponse({"files": files, "dir_exists": analytics_dir.exists()})


@router.get("/knowledge/{kb_path}/analytics-config/download/{filename}")
async def analytics_config_download(kb_path: str, filename: str):
    """Скачать YAML-конфиг аналитики."""
    base = _kb_base(kb_path)
    _safe_filename(filename)
    if filename not in _ANALYTICS_CONFIG_FILES:
        raise HTTPException(status_code=400, detail="Недопустимое имя файла конфига")
    p = base / "analytics" / filename
    if not p.exists():
        raise HTTPException(status_code=404, detail="Файл не найден")
    return FileResponse(path=str(p), filename=filename, media_type="application/x-yaml")


@router.post("/knowledge/{kb_path}/analytics-config/upload")
async def analytics_config_upload(kb_path: str, file: UploadFile = File(...)):
    """Загрузить (заменить) YAML-конфиг аналитики."""
    base = _kb_base(kb_path)
    filename = _safe_filename(file.filename or "")
    if filename not in _ANALYTICS_CONFIG_FILES:
        raise HTTPException(
            status_code=400,
            detail=f"Недопустимое имя файла. Разрешены: {', '.join(_ANALYTICS_CONFIG_FILES)}",
        )
    analytics_dir = base / "analytics"
    analytics_dir.mkdir(exist_ok=True)
    content = await file.read()
    # Базовая валидация: YAML должен парситься
    try:
        import yaml as _yaml
        _yaml.safe_load(content)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Невалидный YAML: {e}")
    (analytics_dir / filename).write_bytes(content)
    logger.info("Analytics config upload: %s / analytics/%s (%d байт)", kb_path, filename, len(content))
    return RedirectResponse(url="/knowledge", status_code=303)


# ── Онлайн-мониторинг ────────────────────────────────────────────────────────


_COKING_COLORS = {"GREEN": "success", "YELLOW": "warning", "RED": "danger"}
_CAUSE_CLOSE_RU = {
    "RUN_STATE_CHANGE": "Смена режима",
    "DAILY_BOUNDARY":   "Суточный рез",
    "OPERATOR_STOP":    "Стоп оператором",
}


@router.get("/online", response_class=HTMLResponse)
async def online_monitor(request: Request):
    from config import get_tz
    from online import db as odb
    from datetime import datetime

    observations = await odb.list_observations()

    # Открытые сегменты всех машин одним запросом
    open_segs = await odb.get_open_segments_all()

    equipment = await analytics.get_equipment_registry()
    tz = get_tz()
    now_local = datetime.now(tz).strftime("%Y-%m-%dT%H:%M")
    corpus_auto = await analytics.get_app_setting("corpus_auto_analyze", "false")
    qwen_auto   = await analytics.get_app_setting("qwen_auto_analyze",   "false")

    return templates.TemplateResponse(request, "online_monitor.html", {
        "observations":        observations,
        "open_segs":           open_segs,
        "equipment":           equipment,
        "now_local":           now_local,
        "run_state_labels":    _RUN_STATE_LABELS,
        "coking_colors":       _COKING_COLORS,
        "cause_close_ru":      _CAUSE_CLOSE_RU,
        "corpus_auto_analyze": corpus_auto == "true",
        "qwen_auto_analyze":   qwen_auto   == "true",
    })


@router.post("/online/start")
async def online_start(
    request: Request,
    router_sn:        str = Form(...),
    equip_type:       str = Form(...),
    panel_id:         int = Form(...),
    start_date_local: str = Form(...),   # YYYY-MM-DDTHH:MM
    poll_interval_sec: int = Form(30),
):
    from config import get_tz
    from online.manager import get_manager
    from datetime import datetime

    tz = get_tz()
    fmt = "%Y-%m-%dT%H:%M"
    start_date = datetime.strptime(start_date_local, fmt).replace(tzinfo=tz)

    try:
        mgr = get_manager()
        await mgr.start_machine(
            router_sn, equip_type, panel_id,
            start_date, poll_interval_sec,
        )
    except Exception as e:
        logger.exception("Ошибка ПУСК ОНЛАЙН: %s", e)
        raise HTTPException(status_code=400, detail=str(e))

    return RedirectResponse(url="/online", status_code=303)


@router.post("/online/stop")
async def online_stop(
    router_sn:  str = Form(...),
    equip_type: str = Form(...),
    panel_id:   int = Form(...),
):
    from online.manager import get_manager
    try:
        mgr = get_manager()
        await mgr.stop_machine(router_sn, equip_type, panel_id)
    except Exception as e:
        logger.exception("Ошибка СТОП ОНЛАЙН: %s", e)
        raise HTTPException(status_code=400, detail=str(e))
    return RedirectResponse(url="/online", status_code=303)


@router.post("/online/corpus-toggle")
async def online_corpus_toggle(enabled: str = Form("off")):
    """Тоггл авто-анализа Claude прямо со страницы мониторинга."""
    value = "true" if enabled == "on" else "false"
    await analytics.set_app_setting("corpus_auto_analyze", value)
    if value == "true":
        from corpus.worker import get_worker
        w = get_worker()
        if w:
            pending = await w.enqueue_pending()
            if pending:
                logger.info("corpus авто-анализ включён (online): %d сегментов в очередь", pending)
    logger.info("corpus авто-анализ (online): %s", "включён" if value == "true" else "выключен")
    return RedirectResponse(url="/online", status_code=303)


@router.post("/online/qwen-toggle")
async def online_qwen_toggle(enabled: str = Form("off")):
    """Тоггл авто-анализа Qwen прямо со страницы мониторинга."""
    value = "true" if enabled == "on" else "false"
    await analytics.set_app_setting("qwen_auto_analyze", value)
    if value == "true":
        from corpus.qwen_worker import get_worker as get_qwen_worker
        w = get_qwen_worker()
        if w:
            pending = await w.enqueue_pending()
            if pending:
                logger.info("qwen авто-анализ включён (online): %d сегментов в очередь", pending)
    logger.info("qwen авто-анализ (online): %s", "включён" if value == "true" else "выключен")
    return RedirectResponse(url="/online", status_code=303)


@router.post("/online/clear-claude-analysis")
async def online_clear_claude_analysis():
    """Удалить все записи Claude-анализа (segment_analyses)."""
    from corpus.db import clear_all_analyses
    n = await clear_all_analyses()
    logger.warning("Удалён весь Claude-анализ: %d строк", n)
    return RedirectResponse(url="/online", status_code=303)


@router.post("/online/clear-qwen-analysis")
async def online_clear_qwen_analysis():
    """Обнулить humanized_md во всех записях (сброс Qwen-анализа)."""
    from corpus.db import clear_all_humanized
    n = await clear_all_humanized()
    logger.warning("Сброшен весь Qwen-анализ: %d строк", n)
    return RedirectResponse(url="/online", status_code=303)


@router.post("/online/start-multi")
async def online_start_multi(request: Request):
    """Запустить мониторинг сразу для нескольких машин (JSON-тело)."""
    from config import get_tz
    from online.manager import get_manager
    from datetime import datetime

    data = await request.json()
    machines:          list[dict] = data.get("machines", [])
    start_date_local:  str        = data.get("start_date_local", "")
    poll_interval_sec: int        = int(data.get("poll_interval_sec", 30))

    tz = get_tz()
    try:
        start_date = datetime.strptime(start_date_local, "%Y-%m-%dT%H:%M").replace(tzinfo=tz)
    except ValueError as e:
        return JSONResponse({"started": 0, "errors": [f"Неверная дата: {e}"]}, status_code=400)

    mgr = get_manager()
    started, errors = 0, []
    for m in machines:
        try:
            await mgr.start_machine(
                m["router_sn"], m["equip_type"], int(m["panel_id"]),
                start_date, poll_interval_sec,
            )
            started += 1
        except Exception as e:
            errors.append(f"{m['router_sn']}/{m['equip_type']}/{m['panel_id']}: {e}")

    logger.info("online/start-multi: запущено %d, ошибок %d", started, len(errors))
    return JSONResponse({"started": started, "errors": errors})


@router.get("/api/pipeline/status")
async def api_pipeline_status():
    """Статус обоих воркеров + флаги авто-анализа."""
    from corpus.worker import get_worker as get_claude_worker
    from corpus.qwen_worker import get_worker as get_qwen_worker

    cw = get_claude_worker()
    qw = get_qwen_worker()
    corpus_auto = await analytics.get_app_setting("corpus_auto_analyze", "false")
    qwen_auto   = await analytics.get_app_setting("qwen_auto_analyze",   "false")

    return JSONResponse({
        "claude": {
            **(cw.get_status() if cw else {"running": False, "processing_seg_id": None, "queue_size": 0}),
            "auto_analyze": corpus_auto == "true",
        },
        "qwen": {
            **(qw.get_status() if qw else {"running": False, "processing_seg_id": None, "queue_size": 0}),
            "auto_analyze": qwen_auto == "true",
        },
    })


@router.get("/online/calendar/{router_sn}/{equip_type}/{panel_id}", response_class=HTMLResponse)
async def online_calendar(
    request: Request,
    router_sn: str,
    equip_type: str,
    panel_id: int,
    year: int = None,
    month: int = None,
):
    import calendar as _cal
    from config import get_tz
    from online import db as odb
    from datetime import date, timedelta
    from collections import defaultdict

    tz = get_tz()
    daily_hour = 9

    today = date.today()
    if not year or not month:
        year, month = today.year, today.month
    year, month = int(year), int(month)

    segments = await odb.get_segments_for_calendar(router_sn, equip_type, panel_id)

    # Группируем по операционному дню (сутки = 09:00 local → следующие 09:00)
    from online.status_assembler import compute_severity_level

    def _violation_level(characteristics_json) -> str | None:
        """None = нет данных/открытый; иначе итоговый уровень как в status_assembler:
        норма / предупреждение (аналитика) / внимание (панель WARNING) / авария (панель SHUTDOWN)."""
        characteristics_json = _parse_json(characteristics_json, ctx="characteristics_json в календаре")
        if not characteristics_json or not isinstance(characteristics_json, dict):
            return None
        checks = characteristics_json.get("sequence_checks") or []
        if not any(isinstance(c, dict) for c in checks):
            return None
        dets = _seg_collect_dets(characteristics_json)
        level = compute_severity_level(dets)
        # Проваленная sequence-проверка без детекции — тоже сигнал аналитики
        if level == "норма" and any(
            not c.get("passed", True) for c in checks if isinstance(c, dict)
        ):
            level = "предупреждение"
        return level

    segs_by_day: dict[date, list] = defaultdict(list)
    for seg in segments:
        t = seg["t_start"]
        if not t:
            continue
        local = t.astimezone(tz)
        op_date = (local - timedelta(hours=daily_hour)).date()
        s = dict(seg)
        s["t_start"] = local
        if s.get("t_end"):
            s["t_end"] = s["t_end"].astimezone(tz)
        s["violation_level"] = _violation_level(s.get("characteristics_json"))
        segs_by_day[op_date].append(s)

    # Сетка месяца (недели с Пн)
    _cal.setfirstweekday(0)
    weeks = _cal.monthcalendar(year, month)
    grid = []
    for week in weeks:
        row = []
        for day_num in week:
            if day_num == 0:
                row.append(None)
            else:
                d = date(year, month, day_num)
                row.append({"date": d, "segments": segs_by_day.get(d, [])})
        grid.append(row)

    prev_y, prev_m = (year - 1, 12) if month == 1 else (year, month - 1)
    next_y, next_m = (year + 1, 1)  if month == 12 else (year, month + 1)

    _MONTH_RU = ["","Январь","Февраль","Март","Апрель","Май","Июнь",
                 "Июль","Август","Сентябрь","Октябрь","Ноябрь","Декабрь"]

    eq_meta = await analytics.get_equipment(router_sn, equip_type, panel_id) or {}
    obs = await odb.get_observation(router_sn, equip_type, panel_id)

    # Загружаем статусы Claude-анализа для всех сегментов месяца одним запросом
    all_seg_ids = [
        seg["id"]
        for row in grid for cell in row
        if cell is not None
        for seg in cell.get("segments", [])
        if seg.get("id") is not None
    ]
    ai_statuses: dict[int, dict] = {}
    if all_seg_ids:
        try:
            from corpus.db import get_analyses_for_segments
            ai_statuses = await get_analyses_for_segments(all_seg_ids)
        except Exception as _ae:
            logger.debug("corpus статусы недоступны: %s", _ae)

    return templates.TemplateResponse(request, "online_calendar.html", {
        "router_sn":        router_sn,
        "equip_type":       equip_type,
        "panel_id":         panel_id,
        "eq_meta":          eq_meta,
        "observation":      obs,
        "grid":             grid,
        "year":             year,
        "month":            month,
        "month_name":       _MONTH_RU[month],
        "today":            today,
        "prev_y": prev_y,   "prev_m": prev_m,
        "next_y": next_y,   "next_m": next_m,
        "tz_name":          tz.key,
        "daily_hour":       daily_hour,
        "run_state_labels": _RUN_STATE_LABELS,
        "coking_colors":    _COKING_COLORS,
        "cause_close_ru":   _CAUSE_CLOSE_RU,
        "ai_statuses":      ai_statuses,
    })


@router.get("/online/segment/{seg_id}", response_class=HTMLResponse)
async def online_segment_detail(request: Request, seg_id: int):
    from online import db as odb
    from config import get_tz
    import json as _json

    try:
        seg = await odb.get_segment_by_id(seg_id)
    except Exception as _e:
        logger.warning("online_segment_detail #%d: ошибка загрузки: %s", seg_id, _e)
        seg = None

    if not seg:
        # Сегмент временно отсутствует (gap при суточном резе) — авто-повтор
        return HTMLResponse(
            content=(
                f'<html><head>'
                f'<meta http-equiv="refresh" content="3;url=/online/segment/{seg_id}">'
                f'<style>body{{font-family:sans-serif;padding:2rem;color:#555}}</style>'
                f'</head><body>'
                f'<p>⏳ Сегмент #{seg_id} обновляется, страница перезагрузится автоматически…</p>'
                f'</body></html>'
            ),
            status_code=503,
        )

    tz = get_tz()
    is_open = seg["t_end"] is None

    def _pj(val):
        return _parse_json(val, ctx=f"сегмент {seg_id}")

    chars_dict   = _pj(seg.get("characteristics_json"))
    current_vals = _pj(seg.get("current_values_json"))
    active_dets  = _pj(seg.get("active_detections_json"))
    coking_risk  = _pj(seg.get("coking_risk_json"))

    # Предыдущий / следующий сегменты — для навигации и «← откуда пришли»
    prev_seg_raw = await odb.get_segment_before(
        seg["router_sn"], seg["equip_type"], seg["panel_id"],
        seg["t_start"],
    ) if seg.get("t_start") else None
    next_seg_raw = await odb.get_segment_after(
        seg["router_sn"], seg["equip_type"], seg["panel_id"],
        seg["t_start"],
    ) if seg.get("t_start") else None

    def _nav_label(s: dict | None) -> str | None:
        """Короткая метка для кнопки навигации: 'Работа · 4ч 12м'."""
        if not s:
            return None
        rs = s.get("run_state")
        chars = _pj(s.get("characteristics_json")) if "characteristics_json" in s else None
        label = (chars.get("run_state_label") if chars else None) or _RUN_STATE_LABELS.get(rs, f"RS={rs}")
        t_start = s.get("t_start")
        t_end   = s.get("t_end")
        if t_start and t_end:
            dur_s = int((t_end - t_start).total_seconds())
            if dur_s < 60:   dur = f"{dur_s}с"
            elif dur_s < 3600: dur = f"{dur_s//60}м {dur_s%60}с"
            else:              dur = f"{dur_s//3600}ч {(dur_s%3600)//60}м"
            return f"{label} · {dur}"
        return label

    prev_nav = {"id": prev_seg_raw["id"], "label": _nav_label(prev_seg_raw)} if prev_seg_raw else None
    next_nav = {"id": next_seg_raw["id"], "label": _nav_label(next_seg_raw)} if next_seg_raw else None

    prev_seg_chars = _pj(prev_seg_raw.get("characteristics_json")) if prev_seg_raw else None
    prev_run_state_label = None
    if prev_seg_chars and isinstance(prev_seg_chars, dict):
        prev_run_state_label = (
            prev_seg_chars.get("run_state_label")
            or _RUN_STATE_LABELS.get(prev_seg_chars.get("run_state"))
        )
    elif prev_seg_raw:
        rs = prev_seg_raw.get("run_state")
        prev_run_state_label = _RUN_STATE_LABELS.get(rs, f"RUN_STATE={rs}") if rs is not None else None

    # Загрузить анализ Claude (Этап 2) — для всех сегментов (открытые — ручной запуск)
    claude_analysis = None
    try:
        from corpus.db import get_analysis as _get_analysis
        from corpus.humanizer import _extract_block2 as _exb2
        claude_analysis = await _get_analysis(seg_id)
        if claude_analysis and claude_analysis.get("conclusion_md"):
            claude_analysis = dict(claude_analysis)
            claude_analysis["conclusion_block2"] = _exb2(claude_analysis["conclusion_md"])
    except Exception as _ce:
        logger.debug("corpus: анализ для #%d недоступен: %s", seg_id, _ce)

    if is_open:
        _sev_rank = {"SHUTDOWN": 4, "WARNING": 3, "CAUTION": 2, "INFO": 1}
        _open_dets = []
        if chars_dict:
            for _sub in chars_dict.get("subsegments", []):
                _open_dets.extend(_sub.get("detections", []))
        _max_sev_open = max(
            (_d.get("severity") for _d in _open_dets),
            key=lambda s: _sev_rank.get(s, 0),
            default=None,
        ) if _open_dets else None

        run_open = {
            "id":                str(seg_id),
            "router_sn":         seg["router_sn"],
            "equip_type":        seg["equip_type"],
            "panel_id":          seg["panel_id"],
            "ts_from":           seg["t_start"].astimezone(tz).strftime("%Y-%m-%d %H:%M:%S %Z")
                                 if seg.get("t_start") else "—",
            "ts_to":             "открытый",
            "analytics_version": seg.get("analytics_version", "—"),
            "report_md":         seg.get("report_md"),
            "report_summary_md": seg.get("report_summary_md"),
            "max_severity":      _max_sev_open,
            "_is_auto_seg":      True,
            "_seg_id":           seg_id,
            "_calendar_url":     f"/online/calendar/{seg['router_sn']}/{seg['equip_type']}/{seg['panel_id']}",
        }
        from online.status_assembler import is_analytics_suppressed as _gate_sup
        return templates.TemplateResponse(request, "auto_segment_report.html", {
            "seg": seg, "is_open": True,
            "run": run_open,
            "gate_checked": _gate_sup(seg, active_dets or []),
            "current_values": current_vals,
            "active_detections": active_dets,
            "coking_risk": coking_risk,
            "claude_analysis": claude_analysis,
            "prev_run_state_label": prev_run_state_label,
            "prev_nav": prev_nav,
            "next_nav": next_nav,
            "run_state_labels": _RUN_STATE_LABELS,
            "coking_colors": _COKING_COLORS,
            "cause_close_ru": _CAUSE_CLOSE_RU,
            "tz_name": tz.key,
        })

    # Закрытый сегмент — строим run-like dict для рендера как аналитический прогон
    detections, dq_pairs = [], []
    if chars_dict:
        for sub in chars_dict.get("subsegments", []):
            detections.extend(sub.get("detections", []))
            dq = sub.get("data_quality")
            dur = sub.get("duration_sec", 0)
            if dq is not None:
                dq_pairs.append((dq, dur))

    sev_rank = {"SHUTDOWN": 4, "WARNING": 3, "CAUTION": 2, "INFO": 1}
    max_sev = max((d.get("severity") for d in detections), key=lambda s: sev_rank.get(s, 0), default=None)
    total_dur = sum(w for _, w in dq_pairs)
    dq_avg = sum(q * w for q, w in dq_pairs) / total_dur if total_dur > 0 else None

    # Форматируем ts как строки с учётом TZ для шаблона
    def _fmt_ts(dt):
        if dt is None: return "—"
        return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S %Z")

    run = {
        "id":               str(seg_id),
        "router_sn":        seg["router_sn"],
        "equip_type":       seg["equip_type"],
        "panel_id":         seg["panel_id"],
        "engine_sn":        "—",
        "ts_from":          _fmt_ts(seg.get("t_start")),
        "ts_to":            _fmt_ts(seg.get("t_end")),
        "analytics_version": seg.get("analytics_version", "—"),
        "report_md":        seg.get("report_md"),
        "report_summary_md": seg.get("report_summary_md"),
        "error":            None,
        "max_severity":     max_sev,
        "segments_count":   1,
        "detections_count": len(detections),
        "data_quality_avg": dq_avg,
        "duration_ms":      None,
        "created_at":       _fmt_ts(seg.get("created_at")),
        # для хлебных крошек
        "_is_auto_seg":     True,
        "_seg_id":          seg_id,
        "_calendar_url": (
            f"/online/calendar/{seg['router_sn']}/{seg['equip_type']}/{seg['panel_id']}"
        ),
    }
    return templates.TemplateResponse(request, "auto_segment_report.html", {
        "seg": seg, "is_open": False,
        "run": run,
        "claude_analysis": claude_analysis,
        "prev_run_state_label": prev_run_state_label,
        "prev_nav": prev_nav,
        "next_nav": next_nav,
        "run_state_labels": _RUN_STATE_LABELS,
        "coking_colors": _COKING_COLORS,
        "cause_close_ru": _CAUSE_CLOSE_RU,
        "tz_name": tz.key,
    })


@router.post("/online/segment/{seg_id}/analyze")
async def online_segment_analyze(seg_id: int):
    """Поставить сегмент в очередь на ручной Claude-анализ (приоритетный)."""
    from corpus.worker import get_worker, PRIORITY_MANUAL
    from corpus.db import set_status as _set_status

    worker = get_worker()
    if not worker:
        raise HTTPException(status_code=503, detail="Corpus worker не запущен")

    await _set_status(seg_id, "queued")
    worker.enqueue(seg_id, priority=PRIORITY_MANUAL)
    logger.info("corpus: ручной анализ сегмента #%d поставлен в очередь", seg_id)
    return RedirectResponse(f"/online/segment/{seg_id}", status_code=303)


@router.get("/api/corpus/status")
async def api_corpus_status():
    """Статус воркера Claude-конвейера."""
    from corpus.worker import get_worker
    worker = get_worker()
    if not worker:
        return JSONResponse({"running": False, "processing_seg_id": None, "queue_size": 0})
    return JSONResponse(worker.get_status())


@router.get("/online/segment/{seg_id}/md")
async def online_segment_md(seg_id: int):
    """Скачать Markdown-отчёт авто-сегмента."""
    from online import db as odb
    from fastapi.responses import Response
    seg = await odb.get_segment_by_id(seg_id)
    if not seg or not seg.get("report_md"):
        raise HTTPException(status_code=404, detail="Отчёт не найден")
    return Response(
        content=seg["report_md"].encode("utf-8"),
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="segment_{seg_id}.md"'},
    )


@router.post("/online/delete-segments")
async def online_delete_segments(request: Request):
    """Удалить выбранные сегменты по списку ID.

    Тело запроса (JSON):
      {"router_sn": ..., "equip_type": ..., "panel_id": ..., "seg_ids": [1, 2, 3]}
    """
    from online import db as odb
    data = await request.json()
    router_sn  = data.get("router_sn",  "")
    equip_type = data.get("equip_type", "")
    panel_id   = int(data.get("panel_id", 0))
    seg_ids    = [int(i) for i in data.get("seg_ids", [])]

    if not seg_ids:
        return JSONResponse({"deleted": 0, "error": "Не указаны сегменты"}, status_code=400)

    # Остановить движок перед удалением
    try:
        from online.manager import get_manager
        mgr = get_manager()
        if mgr.is_running(router_sn, equip_type, panel_id):
            await mgr.stop_machine(router_sn, equip_type, panel_id)
    except Exception:
        pass

    deleted = await odb.delete_segments_by_ids(seg_ids)
    logger.info("Удалено сегментов: %d (ids: %s)", deleted, seg_ids[:10])
    return JSONResponse({"deleted": deleted})


@router.post("/online/clear")
async def online_clear(
    request: Request,
    router_sn:  str = Form(...),
    equip_type: str = Form(...),
    panel_id:   int = Form(...),
    ts_from_local: str = Form(""),
    ts_to_local:   str = Form(""),
    confirm:    str = Form(""),
):
    """Очистка авто-сегментов с фильтром. Полная очистка требует confirm='yes'."""
    from config import get_tz
    from online import db as odb
    from datetime import datetime

    if not confirm:
        # Возвращаемся в календарь — без подтверждения ничего не удаляем
        return RedirectResponse(
            url=f"/online/calendar/{router_sn}/{equip_type}/{panel_id}",
            status_code=303,
        )

    tz = get_tz()
    fmt = "%Y-%m-%dT%H:%M"

    ts_from = None
    ts_to   = None
    if ts_from_local:
        try:
            ts_from = datetime.strptime(ts_from_local, fmt).replace(tzinfo=tz)
        except ValueError:
            pass
    if ts_to_local:
        try:
            ts_to = datetime.strptime(ts_to_local, fmt).replace(tzinfo=tz)
        except ValueError:
            pass

    # Остановить движок перед очисткой
    try:
        from online.manager import get_manager
        mgr = get_manager()
        if mgr.is_running(router_sn, equip_type, panel_id):
            await mgr.stop_machine(router_sn, equip_type, panel_id)
    except Exception:
        pass

    deleted = await odb.clear_segments(router_sn, equip_type, panel_id, ts_from, ts_to)
    logger.info(
        "Очистка авто-сегментов %s/%s/%s: удалено %d, ts_from=%s, ts_to=%s",
        router_sn, equip_type, panel_id, deleted, ts_from, ts_to,
    )
    return RedirectResponse(
        url=f"/online/calendar/{router_sn}/{equip_type}/{panel_id}",
        status_code=303,
    )


@router.get("/api/online/status", response_class=JSONResponse)
async def api_online_status():
    """Текущий статус всех наблюдений + открытых сегментов (для JS-поллинга)."""
    from online import db as odb
    from datetime import datetime, timezone
    import json as _j

    try:
        from online.manager import get_manager
        mgr = get_manager()
        running_keys = mgr.running_keys()
    except RuntimeError:
        mgr = None
        running_keys = []

    now_utc = datetime.now(timezone.utc)
    observations = await odb.list_observations()
    _open_segs = await odb.get_open_segments_all()
    result = []
    for obs in observations:
        key = f"{obs['router_sn']}|{obs['equip_type']}|{obs['panel_id']}"
        open_seg = _open_segs.get(key)
        cr = None
        run_state = None
        if open_seg and open_seg.get("coking_risk_json"):
            cr_data = open_seg["coking_risk_json"]
            if isinstance(cr_data, str):
                cr_data = _j.loads(cr_data)
            cr = cr_data.get("risk_level") if isinstance(cr_data, dict) else None
        if open_seg:
            run_state = open_seg.get("run_state")

        # cursor_ts: последний ЗАФИКСИРОВАННЫЙ рубеж (t_end последнего закрытого сегмента)
        cursor_ts = None
        if mgr:
            ct = mgr.get_cursor_ts(obs["router_sn"], obs["equip_type"], obs["panel_id"])
            if ct:
                cursor_ts = ct.isoformat()
        if not cursor_ts and open_seg and open_seg.get("t_start"):
            cursor_ts = open_seg["t_start"].isoformat()

        # processed_to: куда дошли в последнем цикле (= cursor в текущем N+1 окне)
        processed_to = None
        if mgr:
            pt = mgr.get_last_processed_to(obs["router_sn"], obs["equip_type"], obs["panel_id"])
            if pt:
                processed_to = pt.isoformat()
        if not processed_to:
            processed_to = cursor_ts  # fallback

        # lag_sec: отставание processed_to от now
        lag_sec = None
        if processed_to:
            try:
                pt_dt = datetime.fromisoformat(processed_to)
                if pt_dt.tzinfo is None:
                    pt_dt = pt_dt.replace(tzinfo=timezone.utc)
                lag_sec = (now_utc - pt_dt).total_seconds()
            except Exception:
                pass

        start_date_iso  = obs["start_date"].isoformat()  if obs.get("start_date")  else None
        batch_end_iso   = obs["batch_end_ts"].isoformat() if obs.get("batch_end_ts") else None

        # ── ИИ-оператор: статус-строка ──
        status_text      = None
        severity_level   = "норма"
        panel_severity   = "норма"
        analytics_severity = "норма"
        status_updated   = None
        if open_seg:
            status_text = open_seg.get("status_text")
            if open_seg.get("status_updated_at"):
                status_updated = open_seg["status_updated_at"].isoformat()
            try:
                from online.status_assembler import (
                    compute_severity_level,
                    compute_panel_severity,
                    compute_analytics_severity,
                )
                dets, suppressed = _active_dets(open_seg)
                panel_severity = compute_panel_severity(dets)
                # Вердикт гейта «отменить» подавляет аналитику до смены состава детекций
                if suppressed:
                    dets = [d for d in dets if d.get("scenario") == "CONTROLLER_FAULT"]
                severity_level     = compute_severity_level(dets)
                analytics_severity = compute_analytics_severity(dets)
            except Exception:
                logger.warning("api_online_status: не удалось вычислить severity", exc_info=True)

        result.append({
            "key":              key,
            "router_sn":        obs["router_sn"],
            "equip_type":       obs["equip_type"],
            "panel_id":         obs["panel_id"],
            "status":           obs["status"],
            "engine_live":      key in running_keys,
            "run_state":        run_state,
            "coking_risk":      cr,
            "poll_interval_sec": obs.get("poll_interval_sec", 30),
            "start_date":       start_date_iso,
            "batch_end_ts":     batch_end_iso,
            "cursor_ts":        cursor_ts,
            "processed_to":     processed_to,
            "lag_sec":          lag_sec,
            "t_start_open": (
                open_seg["t_start"].isoformat()
                if open_seg and open_seg.get("t_start") else None
            ),
            # ИИ-оператор Уровень 1
            "status_text":       status_text,
            "severity_level":    severity_level,
            "panel_severity":    panel_severity,
            "analytics_severity": analytics_severity,
            "status_updated":    status_updated,
        })
    return JSONResponse(result)


# ── Публичный JSON API для внешнего UI ───────────────────────────────────────
#
# Эндпоинты читают данные только на чтение.
# Для использования с внешним фронтендом рекомендуется настроить CORS
# через fastapi.middleware.cors.CORSMiddleware в main.py.

def _seg_collect_dets(chars_json: Any, active_dets_json: Any = None) -> list[dict]:
    """Все детекции сегмента: закрытый — из characteristics_json, открытый — из active_detections_json."""
    dets: list[dict] = []
    ch = _parse_json(chars_json, {}, ctx="characteristics_json") or {}
    if isinstance(ch, dict):
        for sub in ch.get("subsegments", []):
            dets.extend(sub.get("detections", []))
    if not dets and active_dets_json:
        dets = _parse_json(active_dets_json, [], ctx="active_detections_json") or []
    return [d for d in dets if isinstance(d, dict)]


def _seg_gate_checked(dets: list[dict], gate_suppressed_hash: str | None) -> bool:
    """Действует ли для сегмента вердикт гейта «отменить» (срабатывание проверено ИИ)."""
    from online.status_assembler import compute_analytics_hash
    return bool(
        gate_suppressed_hash and dets
        and compute_analytics_hash(dets) == gate_suppressed_hash
    )


def _seg_severity(
    chars_json: Any, active_dets_json: Any = None,
    gate_suppressed_hash: str | None = None,
) -> str | None:
    """Severity сегмента для API — значение поля `severity` в ответе.

    Шкала (v4.8.9+):
      "SHUTDOWN" — панель: аварийный останов
      "WARNING"  — панель: тревога (derate / панельный warning)
      "CAUTION"  — детекция аналитического движка  (до v4.8.9 называлось "INFO")
      None       — детекций нет

    Аналитика, отменённая гейтом Claude (gate_suppressed_hash совпал с хешем
    текущего состава детекций), не учитывается → severity может стать None
    даже при наличии детекций в characteristics_json.
    """
    dets = _seg_collect_dets(chars_json, active_dets_json)
    if _seg_gate_checked(dets, gate_suppressed_hash):
        dets = [d for d in dets if d.get("scenario") == "CONTROLLER_FAULT"]
    if not dets:
        return None

    panel_rank = {"SHUTDOWN": 4, "WARNING": 3}
    panel = [d.get("severity", "") for d in dets if d.get("scenario") == "CONTROLLER_FAULT"]
    best_panel = max(panel, key=lambda s: panel_rank.get(s, 0), default="")
    if panel_rank.get(best_panel, 0) >= 3:
        return best_panel
    if any(d.get("scenario") != "CONTROLLER_FAULT" for d in dets):
        return "CAUTION"
    return None


@router.get("/api/machines", response_class=JSONResponse)
async def api_machines():
    """Список наблюдаемых машин с текущим состоянием.

    Возвращает массив объектов — по одному на каждое наблюдение.
    Подходит для главного экрана UI: список машин + живой статус.
    Рекомендуется поллинг каждые 10–30 с.
    """
    from online import db as odb
    from datetime import datetime, timezone
    import json as _json

    observations = await odb.list_observations()
    _open_segs = await odb.get_open_segments_all()
    _open_eps = await odb.get_open_episodes_all()
    try:
        _stale_sec = int(await analytics.get_app_setting("data_stale_threshold_sec", "90"))
    except Exception:
        _stale_sec = 90
    _now = datetime.now(timezone.utc)
    result = []

    for obs in observations:
        key = f"{obs['router_sn']}|{obs['equip_type']}|{obs['panel_id']}"
        seg = _open_segs.get(key)

        # Свежесть телеметрии: максимальный ts history, виденный движком.
        # Нет отметки (наблюдение ни разу не работало) → считаем устаревшим.
        _last_data = obs.get("last_data_ts")
        data_stale = (
            _last_data is None
            or (_now - _last_data).total_seconds() > _stale_sec
        )

        # Текущее состояние из открытого сегмента
        run_state      = seg.get("run_state")    if seg else None
        coking_risk    = None
        status_text    = None
        severity_level = None
        status_updated = None
        gate_checked   = False
        status_struct: dict = {}
        gate_events_count = 0
        gate_cancelled_count = 0

        if seg:
            cr = _parse_json(seg.get("coking_risk_json"), {}, ctx="coking_risk_json")
            coking_risk    = (cr or {}).get("risk_level")
            status_text    = seg.get("status_text")
            status_updated = seg["status_updated_at"].isoformat() if seg.get("status_updated_at") else None
            ss = _parse_json(seg.get("status_struct_json"), ctx="status_struct_json")
            status_struct = ss if isinstance(ss, dict) else {}
            gl = _parse_json(seg.get("gate_log"), ctx="gate_log")
            if isinstance(gl, list):
                gate_events_count    = len(gl)
                gate_cancelled_count = sum(
                    1 for e in gl if isinstance(e, dict) and e.get("decision_applied")
                )
            try:
                from online.status_assembler import compute_severity_level
                # Вердикт гейта «отменить» подавляет аналитику до смены состава детекций
                dets, gate_checked = _active_dets(seg)
                if gate_checked:
                    dets = [d for d in dets if d.get("scenario") == "CONTROLLER_FAULT"]
                severity_level = compute_severity_level(dets)
            except Exception:
                logger.warning("api_machines: не удалось вычислить severity", exc_info=True)

        result.append({
            "router_sn":     obs["router_sn"],
            "equip_type":    obs["equip_type"],
            "panel_id":      obs["panel_id"],
            "name":          obs.get("name") or obs["router_sn"],
            "manufacturer":  obs.get("manufacturer"),
            "model":         obs.get("model"),
            "status":        obs["status"],            # running / stopped
            "run_state":     run_state,
            "run_state_label": _RUN_STATE_LABELS.get(run_state, str(run_state)) if run_state is not None else None,
            # Уровень однозначно кодирует источник: предупреждение — только аналитика,
            # внимание/авария — только панель
            "severity_level":    severity_level,       # норма / предупреждение / внимание / авария
            # Срабатывание аналитики проверено и отменено гейтом Claude
            "gate_checked":      gate_checked,
            # Сколько предупреждений обработал гейт за текущий сегмент
            "gate_events_count":    gate_events_count,
            "gate_cancelled_count": gate_cancelled_count,
            "status_text":     status_text,
            # Структурный статус для карточки: режим, время в режиме, текст тревоги
            "mode_label":       status_struct.get("mode_label"),
            "time_in_mode_sec": status_struct.get("time_in_mode_sec"),
            "alarm_text":       status_struct.get("alarm_text"),
            "status_updated":  status_updated,
            "coking_risk":     coking_risk,            # GREEN / YELLOW / RED
            # Свежесть телеметрии: при data_stale=true UI должен скрывать блок
            # аналитики — статус/severity отражают момент last_data_ts, не «сейчас»
            "last_data_ts":    _last_data.isoformat() if _last_data else None,
            "data_stale":      data_stale,
            # Открытые эпизоды тревог: висят с t_open, duration_sec не тикает в дырах
            "active_alarms": [
                {
                    "scenario":        e["scenario"],
                    "severity":        e.get("severity"),
                    "source":          e.get("source"),
                    # per-fault: панельные аварии различаются по addr/bit
                    "addr":            e.get("addr"),
                    "bit":             e.get("bit"),
                    "name":            (_parse_json(e.get("open_values_json")) or {}).get("fault_name"),
                    "since":           e["t_open"].isoformat() if e.get("t_open") else None,
                    "duration_sec":    round(e.get("active_sec") or 0),
                    "gate_suppressed": bool(e.get("gate_suppressed")),
                    # Контекст аварии (только SHUTDOWN): предыдущий сегмент,
                    # тренд ключевых параметров, висевшие тревоги, сводка 24ч
                    "context":         _parse_json(e.get("context_json"), ctx="context_json"),
                }
                for e in _open_eps.get(key, [])
            ],
            "warning_analysis_md": seg.get("warning_analysis_md") if seg else None,
            "_links": {
                "segments": f"/api/machine/{obs['router_sn']}/{obs['equip_type']}/{obs['panel_id']}/segments",
                "calendar":  f"/online/calendar/{obs['router_sn']}/{obs['equip_type']}/{obs['panel_id']}",
            },
        })

    return JSONResponse(result)


@router.get("/api/machine/{router_sn}/{equip_type}/{panel_id}/segments", response_class=JSONResponse)
async def api_machine_segments(
    router_sn: str,
    equip_type: str,
    panel_id: int,
    year: int | None  = None,
    month: int | None = None,
    limit: int = 200,
):
    """История сегментов машины — для построения календаря.

    Query-параметры:
      year, month  — фильтр по календарному месяцу (оба или ни одного)
      limit        — макс. число сегментов (дефолт 200)

    Возвращает массив сегментов от новых к старым. Поле `severity` кодирует
    наивысший уровень тревоги сегмента согласно шкале (v4.8.9+):
      "SHUTDOWN" — панель: аварийный останов
      "WARNING"  — панель: тревога
      "CAUTION"  — детекция аналитики  ← было "INFO" до v4.8.9
      null       — нет детекций / аналитика отменена гейтом
    """
    from online import db as odb
    from datetime import datetime, timezone, timedelta
    from config import get_tz
    from db.analytics import get_app_setting
    import json as _json

    ts_from = ts_to = None
    if year and month:
        ts_from = datetime(year, month, 1, tzinfo=timezone.utc)
        if month == 12:
            ts_to = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            ts_to = datetime(year, month + 1, 1, tzinfo=timezone.utc)

    # Операционные сутки: daily_split_hour local → следующие daily_split_hour (дефолт 09:00)
    tz = get_tz()
    try:
        daily_hour = int(await get_app_setting("daily_split_hour", "9"))
    except Exception:
        daily_hour = 9

    segments = await odb.get_segments_for_calendar(
        router_sn, equip_type, panel_id,
        ts_from=ts_from, ts_to=ts_to, limit=limit,
    )

    # Загружаем статусы ИИ-анализа одним запросом
    seg_ids = [s["id"] for s in segments if s.get("id")]
    ai_map: dict = {}
    if seg_ids:
        try:
            from corpus.db import get_analyses_for_segments
            ai_map = await get_analyses_for_segments(seg_ids)
        except Exception:
            pass

    result = []
    for seg in reversed(segments):   # новые первыми
        seg_id    = seg.get("id")
        is_open   = seg.get("t_end") is None
        ai_status = ai_map.get(seg_id, {})
        sev       = _seg_severity(seg.get("characteristics_json"), seg.get("active_detections_json"),
                                  gate_suppressed_hash=seg.get("gate_suppressed_hash"))
        gate_ok   = _seg_gate_checked(
            _seg_collect_dets(seg.get("characteristics_json"), seg.get("active_detections_json")),
            seg.get("gate_suppressed_hash"),
        )
        run_state = seg.get("run_state")
        dur       = None
        if seg.get("t_start") and seg.get("t_end"):
            dur = (seg["t_end"] - seg["t_start"]).total_seconds()

        # Операционный день сегмента — как в календаре cg-analytics
        op_day = (
            (seg["t_start"].astimezone(tz) - timedelta(hours=daily_hour)).date().isoformat()
            if seg.get("t_start") else None
        )

        result.append({
            "id":            seg_id,
            "t_start":       seg["t_start"].isoformat() if seg.get("t_start") else None,
            "t_end":         seg["t_end"].isoformat()   if seg.get("t_end")   else None,
            "op_day":        op_day,                      # YYYY-MM-DD операционных суток
            "is_open":       is_open,
            "run_state":     run_state,
            "run_state_label": _RUN_STATE_LABELS.get(run_state, str(run_state)) if run_state is not None else None,
            "duration_sec":  dur,
            "cause_close":   seg.get("cause_close"),
            "severity":      sev,                         # SHUTDOWN/WARNING/CAUTION/None (было INFO до v4.8.9)
            "gate_checked":  gate_ok,                     # срабатывание аналитики отменено гейтом
            "coking_risk":   None,                        # в calendar-запросе не грузим JSONB полностью
            "analytics_version": seg.get("analytics_version"),
            # Наличие ИИ-анализа
            "has_report":    bool(seg.get("report_md") if "report_md" in (seg or {}) else None),
            "has_claude":    ai_status.get("status") == "done",
            "has_qwen":      bool(ai_status.get("humanized_md")),
            "_links": {
                "detail": f"/api/segment/{seg_id}",
                "view":   f"/online/segment/{seg_id}",
            },
        })

    return JSONResponse(result)


@router.get("/api/segment/{seg_id}", response_class=JSONResponse)
async def api_segment_detail(seg_id: int):
    """Полный детальный отчёт по сегменту — для страницы анализа в UI.

    Возвращает:
      - метаданные сегмента
      - report_md   — Markdown-отчёт аналитического блока (сегментация, детекции)
      - analysis    — ИИ-анализ (Claude conclusion + Qwen humanized)
      - is_open     — true если сегмент ещё активен (открытый)
    """
    from online import db as odb
    import json as _json

    seg = await odb.get_segment_by_id(seg_id)
    if not seg:
        raise HTTPException(status_code=404, detail=f"Сегмент #{seg_id} не найден")

    is_open   = seg.get("t_end") is None
    run_state = seg.get("run_state")
    sev       = _seg_severity(seg.get("characteristics_json"), seg.get("active_detections_json"),
                              gate_suppressed_hash=seg.get("gate_suppressed_hash"))
    gate_ok   = _seg_gate_checked(
        _seg_collect_dets(seg.get("characteristics_json"), seg.get("active_detections_json")),
        seg.get("gate_suppressed_hash"),
    )

    # ИИ-анализ (только для закрытых)
    analysis = None
    if not is_open:
        try:
            from corpus.db import get_analysis
            rec = await get_analysis(seg_id)
            if rec:
                analysis = {
                    "status":        rec.get("status"),
                    "conclusion_md": rec.get("conclusion_md"),   # сухое заключение Claude
                    "humanized_md":  rec.get("humanized_md"),    # очеловеченное Qwen
                    "created_at":    rec["created_at"].isoformat() if rec.get("created_at") else None,
                    "updated_at":    rec["updated_at"].isoformat() if rec.get("updated_at") else None,
                }
        except Exception:
            pass

    # Длительность
    dur = None
    if seg.get("t_start") and seg.get("t_end"):
        dur = (seg["t_end"] - seg["t_start"]).total_seconds()

    return JSONResponse({
        "id":            seg_id,
        "router_sn":     seg["router_sn"],
        "equip_type":    seg["equip_type"],
        "panel_id":      seg["panel_id"],
        "t_start":       seg["t_start"].isoformat() if seg.get("t_start") else None,
        "t_end":         seg["t_end"].isoformat()   if seg.get("t_end")   else None,
        "is_open":       is_open,
        "run_state":     run_state,
        "run_state_label": _RUN_STATE_LABELS.get(run_state, str(run_state)) if run_state is not None else None,
        "duration_sec":  dur,
        "cause_close":   seg.get("cause_close"),
        "severity":      sev,
        "gate_checked":  gate_ok,
        "analytics_version": seg.get("analytics_version"),
        # Аналитический Markdown-отчёт (сегментация + детекции)
        "report_md":     seg.get("report_md"),
        # Верхняя часть отчёта: вердикт, замечания (эпизоды), ключевые
        # показатели. UI показывает её сверху, report_md сворачивает
        "report_summary_md": seg.get("report_summary_md"),
        # ИИ-анализ
        "analysis":      analysis,
        # Онлайн-анализ предупреждения (гейт Claude) — есть и у открытого сегмента
        "warning_analysis_md": seg.get("warning_analysis_md"),
        # Для открытого сегмента — живые данные
        "status_text":   seg.get("status_text") if is_open else None,
        "_links": {
            "view":     f"/online/segment/{seg_id}",
            "segments": f"/api/machine/{seg['router_sn']}/{seg['equip_type']}/{seg['panel_id']}/segments",
        },
    })


def _count_lines(path) -> int:
    if not path or not path.exists():
        return 0
    try:
        return sum(1 for line in path.open(encoding="utf-8") if line.strip())
    except OSError:
        return 0


def _list_kb_paths(equipment_dir) -> list[str]:
    """Список папок knowledge base из equipment/ (legacy монолиты)."""
    if not equipment_dir.exists():
        return []
    return sorted(d.name for d in equipment_dir.iterdir() if d.is_dir())


def _list_layer_dirs(base) -> list[str]:
    """Список библиотечных папок слоя (controllers/ или engines/).

    Служебные папки (имя с ведущим `_`, напр. `_defaults`) скрыты — их нельзя
    назначить оборудованию.
    """
    if not base.exists():
        return []
    return sorted(
        d.name for d in base.iterdir()
        if d.is_dir() and not d.name.startswith("_")
    )


# ── База данных: здоровье и настройки ────────────────────────────────────────

@router.get("/db", response_class=HTMLResponse)
async def db_health_page(request: Request):
    from db.analytics import get_db_health_stats, get_app_setting, get_equipment_registry
    from db.source import get_source_mode

    stats, registry, sync_interval_str = await asyncio.gather(
        get_db_health_stats(),
        get_equipment_registry(),
        get_app_setting("history_sync_interval_sec", "30"),
    )

    sync_by_key = {
        f"{s['router_sn']}|{s['equip_type']}|{s['panel_id']}": s
        for s in stats["sync_state"]
    }
    devices = [
        {**eq, "sync": sync_by_key.get(f"{eq['router_sn']}|{eq['equip_type']}|{eq['panel_id']}")}
        for eq in registry
    ]

    return templates.TemplateResponse(request, "db_health.html", {
        "stats":         stats,
        "devices":       devices,
        "sync_interval": int(sync_interval_str),
        "source_mode":   get_source_mode(),
    })


@router.get("/api/db/health")
async def api_db_health():
    from db.analytics import get_db_health_stats
    from db.source import get_source_mode
    stats = await get_db_health_stats()
    stats["source_mode"] = get_source_mode()
    return JSONResponse(stats)


@router.post("/settings/source-mode")
async def set_source_mode_route(mode: str = Form(...)):
    from db.analytics import set_app_setting
    from db.source import set_source_mode
    if mode not in ("external", "local"):
        raise HTTPException(400, "mode must be external or local")
    await set_app_setting("source_mode", mode)
    set_source_mode(mode)
    logger.info("Источник телеметрии переключён: %s", mode)
    return RedirectResponse("/db", status_code=303)


@router.post("/settings/history-sync-interval")
async def set_history_sync_interval(interval_sec: int = Form(...)):
    from db.analytics import set_app_setting
    import online.manager as _mgr
    if not (5 <= interval_sec <= 3600):
        raise HTTPException(400, "interval_sec must be 5..3600")
    await set_app_setting("history_sync_interval_sec", str(interval_sec))
    mgr = _mgr.get_manager()
    if mgr._history_sync:
        mgr._history_sync._interval_sec = interval_sec
    return RedirectResponse("/db", status_code=303)
