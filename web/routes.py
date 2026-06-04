"""FastAPI роуты Web UI аналитики."""
import asyncio
import logging
import re
import time
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

# Статус переиндексации KB (kb_path → dict)
_reindex_status: dict[str, dict] = {}


# ── Главная страница ──────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    reports = await analytics.get_latest_reports(limit=100)
    return templates.TemplateResponse(request, "index.html", {"reports": reports})


# ── Отчёт ─────────────────────────────────────────────────────────────────────

@router.get("/report/{report_id}", response_class=HTMLResponse)
async def report(request: Request, report_id: str):
    rep = await analytics.get_report(report_id)
    if not rep:
        raise HTTPException(status_code=404, detail="Отчёт не найден")
    return templates.TemplateResponse(request, "report.html", {"report": rep})


# ── История ──────────────────────────────────────────────────────────────────

@router.get("/history/{router_sn}/{equip_type}/{panel_id}", response_class=HTMLResponse)
async def history(
    request: Request,
    router_sn: str,
    equip_type: str,
    panel_id: int,
):
    reports = await analytics.get_equipment_history(router_sn, equip_type, panel_id, limit=90)
    return templates.TemplateResponse(request, "history.html", {
        "router_sn": router_sn,
        "equip_type": equip_type,
        "panel_id": panel_id,
        "reports": reports,
    })


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
        "result": None,
        "error": None,
    })


@router.post("/analyze", response_class=HTMLResponse)
async def analyze_run(
    request: Request,
    router_sn: str = Form(...),
    equip_type: str = Form(...),
    panel_id: int  = Form(...),
    ts_from_local: str = Form(...),   # YYYY-MM-DDTHH:MM из datetime-local
    ts_to_local:   str = Form(...),
):
    from config import get_tz
    equipment = await analytics.get_equipment_registry()
    result = None
    error  = None
    try:
        result = await _run_analysis(
            router_sn, equip_type, panel_id,
            ts_from_local, ts_to_local, get_tz(),
        )
    except Exception as e:
        logger.exception("Ошибка анализа: %s", e)
        error = str(e)

    return templates.TemplateResponse(request, "analyze.html", {
        "equipment": equipment,
        "default_from": ts_from_local,
        "default_to":   ts_to_local,
        "selected_sn":    router_sn,
        "selected_type":  equip_type,
        "selected_panel": str(panel_id),
        "result": result,
        "error":  error,
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

            # Конфигурация
            kb_path_rel = await analytics.get_equipment_kb_path(router_sn, equip_type, panel_id)
            if not kb_path_rel:
                yield _evt({"stage": "error", "message": "Не задан kb_path для оборудования"})
                return
            kb_path = _cfg.knowledge_base_path / "equipment" / kb_path_rel
            cfg = AnalyticsConfig(kb_path)

            registry = await analytics.get_equipment_registry()
            eq = next(
                (e for e in registry
                 if e["router_sn"] == router_sn and e["equip_type"] == equip_type
                 and str(e["panel_id"]) == str(panel_id)), {}
            )
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


async def _run_analysis(
    router_sn: str,
    equip_type: str,
    panel_id: int,
    ts_from_local: str,
    ts_to_local: str,
    tz,
) -> dict:
    """Запустить аналитику v2 за диапазон и вернуть результат с Markdown-отчётом."""
    from datetime import datetime, timezone as _tz
    from config import settings as _cfg
    from analytics.runner import run_analysis

    fmt = "%Y-%m-%dT%H:%M"
    ts_from_utc = datetime.strptime(ts_from_local, fmt).replace(tzinfo=tz).astimezone(_tz.utc)
    ts_to_utc   = datetime.strptime(ts_to_local,   fmt).replace(tzinfo=tz).astimezone(_tz.utc)

    if ts_to_utc <= ts_from_utc:
        raise ValueError("Конец диапазона должен быть позже начала")

    # Метаданные из реестра (engine_sn, kb_path)
    kb_path_rel = await analytics.get_equipment_kb_path(router_sn, equip_type, panel_id)
    if not kb_path_rel:
        raise ValueError(
            f"Не задан kb_path для {router_sn}/{equip_type}/{panel_id}. "
            "Укажите путь к Knowledge Base в настройках оборудования."
        )
    kb_path = _cfg.knowledge_base_path / "equipment" / kb_path_rel

    registry = await analytics.get_equipment_registry()
    eq = next(
        (e for e in registry
         if e["router_sn"] == router_sn
         and e["equip_type"] == equip_type
         and str(e["panel_id"]) == str(panel_id)),
        {}
    )
    engine_sn = eq.get("engine_sn") or ""

    result = await run_analysis(
        router_sn=router_sn,
        equip_type=equip_type,
        panel_id=panel_id,
        engine_sn=engine_sn,
        ts_from=ts_from_utc,
        ts_to=ts_to_utc,
        kb_path=kb_path,
        tz=tz,
    )

    if result.get("error"):
        raise RuntimeError(result["error"])

    return {
        "markdown":          result["report_md"],
        "run_id":            result.get("run_id"),
        "segments_count":    result["segments_count"],
        "detections_count":  result["detections_count"],
        "max_severity":      result.get("max_severity"),
        "data_quality_avg":  result.get("data_quality_avg"),
        "duration_ms":       result.get("duration_ms"),
        "ts_from_local":     ts_from_local,
        "ts_to_local":       ts_to_local,
        "tz_name":           tz.key,
        "router_sn":         router_sn,
        "equip_type":        equip_type,
        "panel_id":          panel_id,
    }


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
            models.append({
                "kb_path": kb_dir.name,
                "registers": reg_count,
                "faults": fault_count,
                "has_rules": has_rules,
                "pdfs": pdf_count,
            })

    return templates.TemplateResponse(request, "knowledge.html", {
        "models": models,
        "reindex_status": _reindex_status,
    })


@router.post("/knowledge/reindex")
async def reindex(kb_path: str = Form(...)):
    """Запустить переиндексацию в фоне."""
    _reindex_status[kb_path] = {
        "status": "running",
        "step": "Инициализация…",
        "docs": 0,
        "started_at": time.time(),
        "error": None,
    }

    async def _reindex():
        from knowledge.indexer import index_equipment
        from knowledge.retriever import invalidate_cache
        from knowledge.loader import invalidate_cache as loader_invalidate

        def _cb(step: str, total: int = 0):
            _reindex_status[kb_path]["step"] = step
            _reindex_status[kb_path]["docs"] = total

        try:
            loop = asyncio.get_running_loop()
            count = await loop.run_in_executor(
                None, lambda: index_equipment(kb_path, _cb)
            )
            invalidate_cache(kb_path)
            loader_invalidate(kb_path)
            _reindex_status[kb_path]["status"] = "done"
            _reindex_status[kb_path]["step"] = f"Готово: {count} документов"
            _reindex_status[kb_path]["docs"] = count
            logger.info("Переиндексация завершена: %s, %d документов", kb_path, count)
        except Exception as e:
            logger.exception("Ошибка переиндексации: %s", e)
            _reindex_status[kb_path]["status"] = "error"
            _reindex_status[kb_path]["error"] = str(e)

    asyncio.create_task(_reindex())
    return RedirectResponse(url="/knowledge", status_code=303)


@router.get("/knowledge/reindex/status", response_class=JSONResponse)
async def reindex_status_api():
    """Текущий статус переиндексаций (для JS-поллинга)."""
    now = time.time()
    result = {}
    for kb, s in _reindex_status.items():
        result[kb] = {**s, "elapsed_s": int(now - s["started_at"])}
    return JSONResponse(result)


# ── KB: управление файлами ────────────────────────────────────────────────────

_KB_NAME_RE = re.compile(r"^[a-z0-9_]+$")
_KB_DATA_FILES = [
    "register_map.jsonl",
    "fault_bitmap_map.jsonl",
    "enum_map.json",
    "operation_rules.json",
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


@router.get("/knowledge/{kb_path}/search", response_class=JSONResponse)
async def kb_search(kb_path: str, q: str = ""):
    """Тестовый поиск по RAG-индексу. Возвращает топ-5 результатов."""
    _kb_base(kb_path)  # валидация пути
    if not q.strip():
        return JSONResponse({"results": [], "error": None})
    try:
        from knowledge.retriever import search_manual_docs
        import asyncio as _asyncio
        # Поиск синхронный (LlamaIndex) — выносим в executor чтобы не блокировать event loop
        raw = await _asyncio.get_event_loop().run_in_executor(
            None, lambda: search_manual_docs(q.strip(), kb_path, top_k=5)
        )
        # Разбиваем склеенный текст обратно на отдельные результаты
        if raw:
            chunks = [c.strip() for c in raw.split("\n\n---\n\n") if c.strip()]
        else:
            chunks = []
        return JSONResponse({"results": chunks, "error": None})
    except Exception as e:
        logger.warning("kb_search ошибка: %s", e)
        return JSONResponse({"results": [], "error": str(e)})


# ── Настройки ─────────────────────────────────────────────────────────────────

@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    from config import settings as cfg, TIMEZONE_CHOICES, get_tz
    from llm.client import get_llm_settings
    from corpus.settings import get_claude_settings
    registry = await analytics.get_equipment_registry()
    kb_list = _list_kb_paths(cfg.knowledge_base_path / "equipment")
    corpus_auto = await analytics.get_app_setting("corpus_auto_analyze", "false")
    qwen_auto   = await analytics.get_app_setting("qwen_auto_analyze",   "false")
    return templates.TemplateResponse(request, "settings.html", {
        "settings": cfg,
        "registry": registry,
        "kb_list": kb_list,
        "timezone_choices": TIMEZONE_CHOICES,
        "current_timezone": get_tz().key,
        "llm": get_llm_settings(),
        "claude": get_claude_settings(),
        "corpus_auto_analyze": corpus_auto == "true",
        "qwen_auto_analyze":   qwen_auto   == "true",
    })


@router.post("/settings/llm")
async def update_llm_settings(
    llm_base_url:       str   = Form(...),
    llm_model:          str   = Form(...),
    llm_temperature:    float = Form(...),
    llm_num_ctx:        int   = Form(...),
    llm_system_prompt:  str   = Form(...),
):
    """Сохранить настройки LLM и применить без перезапуска."""
    from llm.client import apply_llm_settings
    apply_llm_settings(llm_base_url, llm_model, llm_temperature, llm_num_ctx, llm_system_prompt)
    await analytics.set_app_setting("llm_base_url",     llm_base_url)
    await analytics.set_app_setting("llm_model",        llm_model)
    await analytics.set_app_setting("llm_temperature",  str(llm_temperature))
    await analytics.set_app_setting("llm_num_ctx",      str(llm_num_ctx))
    await analytics.set_app_setting("llm_system_prompt", llm_system_prompt)
    logger.info("LLM настройки сохранены: model=%s", llm_model)
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/settings/claude")
async def update_claude_settings(
    claude_model:          str = Form(...),
    claude_max_tool_calls: int = Form(...),
    claude_max_tokens:     int = Form(...),
    claude_proxy:          str = Form(""),
    claude_system_prompt:  str = Form(...),
):
    """Сохранить настройки Claude API и применить без перезапуска."""
    from corpus.settings import apply_claude_settings
    apply_claude_settings(claude_model, claude_max_tool_calls, claude_max_tokens,
                          claude_proxy, claude_system_prompt)
    await analytics.set_app_setting("claude_model",          claude_model)
    await analytics.set_app_setting("claude_max_tool_calls", str(claude_max_tool_calls))
    await analytics.set_app_setting("claude_max_tokens",     str(claude_max_tokens))
    await analytics.set_app_setting("claude_proxy",          claude_proxy)
    await analytics.set_app_setting("claude_system_prompt",  claude_system_prompt)
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


@router.post("/settings/rag")
async def update_rag_settings(
    embedding_base_url: str = Form(...),
    embedding_model:    str = Form(...),
):
    """Сохранить настройки RAG-модели, сбросить кэш индексов."""
    from config import settings as cfg
    from knowledge.retriever import clear_index_cache
    cfg.embedding_base_url = embedding_base_url.rstrip("/")
    cfg.embedding_model    = embedding_model.strip()
    clear_index_cache()
    await analytics.set_app_setting("embedding_base_url", embedding_base_url)
    await analytics.set_app_setting("embedding_model",    embedding_model)
    logger.info("RAG настройки сохранены: model=%s url=%s", embedding_model, embedding_base_url)
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

    # Для каждого наблюдения — открытый сегмент (если есть)
    open_segs: dict[str, dict] = {}
    for obs in observations:
        seg = await odb.get_open_segment(obs["router_sn"], obs["equip_type"], obs["panel_id"])
        if seg:
            open_segs[f"{obs['router_sn']}|{obs['equip_type']}|{obs['panel_id']}"] = seg

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
    _SEV_RANK = {"INFO": 1, "WARNING": 2, "ALARM": 3, "SHUTDOWN": 3}

    def _violation_level(characteristics_json) -> str | None:
        """None = нет данных/открытый; 'ok'; 'warning'; 'alarm'."""
        import json as _json
        if isinstance(characteristics_json, str):
            try: characteristics_json = _json.loads(characteristics_json)
            except: return None
        if not characteristics_json or not isinstance(characteristics_json, dict):
            return None
        checks = characteristics_json.get("sequence_checks") or []
        if not any(isinstance(c, dict) for c in checks):
            return None
        if not any(not c.get("passed", True) for c in checks if isinstance(c, dict)):
            return "ok"
        max_rank = 0
        for sub in (characteristics_json.get("subsegments") or []):
            for det in (sub.get("detections") or []):
                max_rank = max(max_rank, _SEV_RANK.get(det.get("severity", ""), 0))
        return "alarm" if max_rank >= 3 else "warning"

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

    registry = await analytics.get_equipment_registry()
    eq_meta = next(
        (e for e in registry
         if e["router_sn"] == router_sn and e["equip_type"] == equip_type
         and str(e["panel_id"]) == str(panel_id)), {},
    )
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
        if val is None: return None
        if isinstance(val, str):
            try: return _json.loads(val)
            except: return None
        return val

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

    # Загрузить анализ Claude (Этап 2) — только для закрытых сегментов
    claude_analysis = None
    if not is_open:
        try:
            from corpus.db import get_analysis as _get_analysis
            from corpus.humanizer import _extract_block2 as _exb2
            claude_analysis = await _get_analysis(seg_id)
            # Извлекаем только Блок 2 (аналитика Claude без мета-шапок) — для qwen
            if claude_analysis and claude_analysis.get("conclusion_md"):
                claude_analysis = dict(claude_analysis)
                claude_analysis["conclusion_block2"] = _exb2(claude_analysis["conclusion_md"])
        except Exception as _ce:
            logger.debug("corpus: анализ для #%d недоступен: %s", seg_id, _ce)

    if is_open:
        # Открытый сегмент: показать текущие значения
        return templates.TemplateResponse(request, "auto_segment_report.html", {
            "seg": seg, "is_open": True,
            "current_values": current_vals,
            "active_detections": active_dets,
            "coking_risk": coking_risk,
            "claude_analysis": None,
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

    sev_rank = {"SHUTDOWN": 4, "ALARM": 3, "WARNING": 2, "INFO": 1}
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
    result = []
    for obs in observations:
        key = f"{obs['router_sn']}|{obs['equip_type']}|{obs['panel_id']}"
        open_seg = await odb.get_open_segment(
            obs["router_sn"], obs["equip_type"], obs["panel_id"]
        )
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
            "batch_end_ts":     batch_end_iso, # фиксированный правый край batch-добора (знаменатель)
            "cursor_ts":        cursor_ts,    # зафиксированный рубеж (t_end последнего закр. сег.)
            "processed_to":     processed_to, # куда дошли в последнем цикле (для прогресс-бара)
            "lag_sec":          lag_sec,       # отставание processed_to от now
            "t_start_open": (
                open_seg["t_start"].isoformat()
                if open_seg and open_seg.get("t_start") else None
            ),
        })
    return JSONResponse(result)


def _count_lines(path) -> int:
    if not path or not path.exists():
        return 0
    try:
        return sum(1 for line in path.open(encoding="utf-8") if line.strip())
    except OSError:
        return 0


def _list_kb_paths(equipment_dir) -> list[str]:
    """Список папок knowledge base из equipment/."""
    if not equipment_dir.exists():
        return []
    return sorted(d.name for d in equipment_dir.iterdir() if d.is_dir())
