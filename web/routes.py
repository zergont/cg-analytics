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
            md = to_markdown(segments, router_sn, equip_type, panel_id, ts_from_utc, ts_to_utc)
            summary = build_run_summary(segments)
            yield _evt({"stage": "build", "status": "done"})

            # Сохранение в БД (не блокируем стрим)
            run_id = None
            try:
                from analytics.serializer import to_json as _to_json
                from db.analytics import save_analysis_run as _save
                seg_json = _to_json(segments, router_sn, equip_type, panel_id, ts_from_utc, ts_to_utc)
                run_id = await _save({
                    "router_sn": router_sn, "equip_type": equip_type, "panel_id": panel_id,
                    "engine_sn": engine_sn, "ts_from": ts_from_utc, "ts_to": ts_to_utc,
                    "analytics_version": "2.0.0",
                    "segments_json": seg_json, "report_md": md,
                    **summary,
                })
            except Exception as _db_err:
                logger.warning("Ошибка сохранения в БД: %s", _db_err)

            yield _evt({
                "stage": "complete",
                "result": {
                    "markdown":          md,
                    "run_id":            run_id,
                    "segments_count":    summary["segments_count"],
                    "detections_count":  summary["detections_count"],
                    "max_severity":      summary.get("max_severity"),
                    "data_quality_avg":  summary.get("data_quality_avg"),
                    "ts_from_local":     ts_from_local,
                    "ts_to_local":       ts_to_local,
                    "tz_name":           tz.key,
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


@router.get("/history-v2/{router_sn}/{equip_type}/{panel_id}", response_class=HTMLResponse)
async def analysis_history(
    request: Request,
    router_sn: str,
    equip_type: str,
    panel_id: int,
):
    """История аналитических прогонов v2 для одной ГУ."""
    runs = await analytics.list_analysis_runs(router_sn, equip_type, panel_id, limit=50)
    return templates.TemplateResponse(request, "analysis_history.html", {
        "router_sn": router_sn,
        "equip_type": equip_type,
        "panel_id": panel_id,
        "runs": runs,
    })


@router.get("/log", response_class=HTMLResponse)
async def log_page(request: Request):
    return templates.TemplateResponse(request, "log.html", {})


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

    return JSONResponse({"data_files": data_files, "pdf_files": pdf_files})


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


# ── Настройки ─────────────────────────────────────────────────────────────────

@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    from config import settings as cfg, TIMEZONE_CHOICES, get_tz
    registry = await analytics.get_equipment_registry()
    kb_list = _list_kb_paths(cfg.knowledge_base_path / "equipment")
    return templates.TemplateResponse(request, "settings.html", {
        "settings": cfg,
        "registry": registry,
        "kb_list": kb_list,
        "timezone_choices": TIMEZONE_CHOICES,
        "current_timezone": get_tz().key,
    })


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
