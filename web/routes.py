"""FastAPI роуты Web UI аналитики."""
import asyncio
import logging
import time
from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from db import analytics, source
from pipeline.runner import run_pipeline

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="web/templates")

# Версия приложения — читается один раз при старте, доступна во всех шаблонах
_version_file = __import__("pathlib").Path(__file__).parent.parent / "VERSION"
templates.env.globals["app_version"] = _version_file.read_text(encoding="utf-8").strip()

# Часовой пояс — строка, доступная во всех шаблонах.
# Обновляется через _apply_tz() при старте и при смене через UI.
from config import get_tz as _get_tz, set_tz as _set_tz
templates.env.globals["app_timezone"] = _get_tz().key


def _apply_tz(tz_name: str) -> None:
    """Применить новый TZ: обновить in-memory и глобал шаблонов."""
    _set_tz(tz_name)
    templates.env.globals["app_timezone"] = tz_name

# Хелперы для отображения сегментов в шаблоне segments.html
_SEG_BADGE = {
    "standstill":        "bg-secondary",
    "startup_window":    "bg-warning text-dark",
    "warmup":            "bg-info text-dark",
    "normal_operation":  "bg-success",
    "cooldown":          "bg-info text-dark",
    "shutdown_window":   "bg-secondary",
    "fault_window":      "bg-danger",
}
_SEG_LABEL = {
    "standstill":        "Простой",
    "startup_window":    "Пуск",
    "warmup":            "Прогрев",
    "normal_operation":  "Работа",
    "cooldown":          "Охлаждение",
    "shutdown_window":   "Останов",
    "fault_window":      "Авария",
}
_SEG_HEADER = {
    "standstill":        "bg-secondary bg-opacity-10",
    "startup_window":    "bg-warning bg-opacity-10",
    "warmup":            "bg-info bg-opacity-10",
    "normal_operation":  "bg-success bg-opacity-10",
    "cooldown":          "bg-info bg-opacity-10",
    "shutdown_window":   "bg-secondary bg-opacity-10",
    "fault_window":      "bg-danger bg-opacity-10",
}
templates.env.globals["_seg_badge"]       = lambda t: _SEG_BADGE.get(t, "bg-secondary")
templates.env.globals["_seg_label_short"] = lambda t: _SEG_LABEL.get(t, t)
templates.env.globals["_seg_header"]      = lambda t: _SEG_HEADER.get(t, "")

# Хранилище активных задач запуска (task_key → dict)
_running_tasks: dict[str, dict] = {}

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


# ── Ручной запуск ─────────────────────────────────────────────────────────────

@router.get("/run", response_class=HTMLResponse)
async def run_page(request: Request):
    equipment = await analytics.get_equipment_registry()
    yesterday = date.today() - timedelta(days=1)
    return templates.TemplateResponse(request, "run.html", {
        "equipment": equipment,
        "default_date": str(yesterday),
        "running_tasks": _running_tasks,
    })


@router.post("/run", response_class=HTMLResponse)
async def run_start(
    request: Request,
    router_sn: str = Form(...),
    equip_type: str = Form(...),
    panel_id: int = Form(...),
    run_date: str = Form(...),
):
    day = date.fromisoformat(run_date)
    task_key = f"{router_sn}_{equip_type}_{panel_id}_{run_date}"

    if task_key in _running_tasks and _running_tasks[task_key].get("status") == "running":
        return RedirectResponse(url="/run", status_code=303)

    _running_tasks[task_key] = {
        "status": "running",
        "label": f"{router_sn}/{equip_type}/{panel_id} за {run_date}",
        "report_id": None,
        "error": None,
    }

    async def _run():
        try:
            result = await run_pipeline(router_sn, equip_type, panel_id, day)
            _running_tasks[task_key]["status"] = "done"
            _running_tasks[task_key]["report_id"] = result["id"]
        except Exception as e:
            logger.exception("Ошибка pipeline: %s", e)
            _running_tasks[task_key]["status"] = "error"
            _running_tasks[task_key]["error"] = str(e)

    asyncio.create_task(_run())
    return RedirectResponse(url="/run", status_code=303)


@router.get("/run/status", response_class=JSONResponse)
async def run_status():
    return JSONResponse(_running_tasks)


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


async def _run_analysis(
    router_sn: str,
    equip_type: str,
    panel_id: int,
    ts_from_local: str,
    ts_to_local: str,
    tz,
) -> dict:
    """Собрать данные за диапазон и сформировать Markdown-пакет для ИИ."""
    from datetime import datetime, timezone as _tz
    from pipeline.aggregator import compute_register_stats
    from knowledge.loader import load_knowledge

    # Парсим локальное время → UTC
    fmt = "%Y-%m-%dT%H:%M"
    ts_from_utc = datetime.strptime(ts_from_local, fmt).replace(tzinfo=tz).astimezone(_tz.utc)
    ts_to_utc   = datetime.strptime(ts_to_local,   fmt).replace(tzinfo=tz).astimezone(_tz.utc)

    if ts_to_utc <= ts_from_utc:
        raise ValueError("Конец диапазона должен быть позже начала")

    duration_h = (ts_to_utc - ts_from_utc).total_seconds() / 3600

    # Загружаем данные параллельно
    history, state_events, events = await asyncio.gather(
        source.get_history_range(router_sn, equip_type, panel_id, ts_from_utc, ts_to_utc),
        source.get_state_events_range(router_sn, equip_type, panel_id, ts_from_utc, ts_to_utc),
        source.get_events_range(router_sn, equip_type, panel_id, ts_from_utc, ts_to_utc),
    )

    # Метаданные оборудования из реестра
    registry = await analytics.get_equipment_registry()
    eq = next(
        (e for e in registry
         if e["router_sn"] == router_sn
         and e["equip_type"] == equip_type
         and str(e["panel_id"]) == str(panel_id)),
        {}
    )

    # Карта регистров и правила эксплуатации из KB (если задана)
    kb_path = await analytics.get_equipment_kb_path(router_sn, equip_type, panel_id)
    register_map: dict = {}
    operation_rules: dict = {}
    if kb_path:
        try:
            kb = load_knowledge(kb_path)
            register_map    = kb.get("register_map", {})
            operation_rules = kb.get("operation_rules", {})
        except Exception:
            pass

    # Статистика по регистрам
    reg_stats = compute_register_stats(history, ts_to_utc, register_map)

    # ── RAG: обогащение из документации ──────────────────────────────────────
    rag_context = ""
    if kb_path:
        try:
            from knowledge.retriever import retrieve_context as _rag_retrieve
            # Адреса активных аналоговых регистров
            active_addrs = list(reg_stats.keys())
            # Адреса fault-регистров: state_events с ненулевым raw
            fault_addrs = list({
                ev["addr"] for ev in state_events
                if ev.get("raw") is not None and ev["raw"] != 0
            })
            loop = asyncio.get_running_loop()
            rag_context = await loop.run_in_executor(
                None,
                lambda: _rag_retrieve(kb_path, active_addrs, fault_addrs or None),
            )
        except Exception as e:
            logger.warning("RAG недоступен: %s", e)

    # Собираем Markdown
    md = _build_analysis_md(
        router_sn=router_sn, equip_type=equip_type, panel_id=panel_id,
        eq=eq, kb_path=kb_path or "",
        ts_from_utc=ts_from_utc, ts_to_utc=ts_to_utc,
        duration_h=duration_h, tz=tz,
        reg_stats=reg_stats, register_map=register_map,
        state_events=state_events, events=events,
        rag_context=rag_context,
        operation_rules=operation_rules,
    )

    return {
        "markdown": md,
        "history_rows":       len(history),
        "registers_count":    len(reg_stats),
        "state_events_count": len(state_events),
        "events_count":       len(events),
        "rag_chunks":         len(rag_context.split("\n\n")) if rag_context else 0,
        "ts_from_local": ts_from_local,
        "ts_to_local":   ts_to_local,
        "tz_name": tz.key,
        "router_sn": router_sn,
        "equip_type": equip_type,
        "panel_id": panel_id,
    }


def _build_analysis_md(
    router_sn, equip_type, panel_id, eq, kb_path,
    ts_from_utc, ts_to_utc, duration_h, tz,
    reg_stats, register_map, state_events, events,
    rag_context: str = "",
    operation_rules: dict | None = None,
) -> str:
    """Сформировать Markdown-пакет данных для передачи в ИИ."""
    from datetime import timezone as _tz

    fmt_dt = "%Y-%m-%d %H:%M"
    fmt_t  = "%H:%M:%S"
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

    # ── Журнал состояний (enum) ───────────────────────────────────────────────
    lines += [
        "",
        f"## Журнал состояний / enum ({len(state_events)} событий)",
    ]
    if state_events:
        for ev in state_events:
            ts_str = local_t(ev["ts"])
            lines.append(f"- `{ts_str}` [{ev['addr']}] {ev['text'] or ev['raw']}")
    else:
        lines.append("_Нет событий за период_")

    # ── Системные события / аварии ────────────────────────────────────────────
    lines += [
        "",
        f"## Системные события / аварии ({len(events)} событий)",
    ]
    if events:
        for ev in events:
            ts_dt = ev.get("created_at") or ev.get("ts")
            ts_str = local_t(ts_dt) if ts_dt else "?"
            desc = ev.get("description") or ev.get("text") or ""
            ev_type = ev.get("type") or ""
            lines.append(f"- `{ts_str}` [{ev_type}] {desc}")
    else:
        lines.append("_Нет событий за период_")

    # ── Правила эксплуатации из KB ───────────────────────────────────────────
    if operation_rules:
        import json as _json
        lines += [
            "",
            "## Правила эксплуатации (operation_rules)",
            "```json",
            _json.dumps(operation_rules, ensure_ascii=False, indent=2),
            "```",
        ]

    # ── Контекст из документации (RAG) ───────────────────────────────────────
    if rag_context:
        lines += [
            "",
            "## Контекст из базы знаний (RAG)",
            rag_context,
        ]
    elif kb_path:
        lines += [
            "",
            "## Контекст из базы знаний (RAG)",
            "_Индекс пуст или Ollama недоступна. Запустите переиндексацию в разделе «База знаний»._",
        ]

    return "\n".join(lines)


# ── Просмотр сегментов суток (Layer 1, без агента) ───────────────────────────

@router.get("/segments", response_class=HTMLResponse)
async def segments_page(
    request: Request,
    router_sn: str = "",
    equip_type: str = "",
    panel_id: str = "",
    seg_date: str = "",
):
    equipment = await analytics.get_equipment_registry()
    yesterday = date.today() - timedelta(days=1)
    result = None
    error = None

    if router_sn and equip_type and panel_id and seg_date:
        try:
            result = await _run_segments(router_sn, equip_type, int(panel_id), seg_date)
        except Exception as e:
            logger.exception("Ошибка сегментации: %s", e)
            error = str(e)

    return templates.TemplateResponse(request, "segments.html", {
        "equipment": equipment,
        "default_date": seg_date or str(yesterday),
        "selected_sn": router_sn,
        "selected_type": equip_type,
        "selected_panel": panel_id,
        "result": result,
        "error": error,
    })


async def _run_segments(router_sn: str, equip_type: str, panel_id: int, seg_date: str) -> dict:
    """Запустить агрегацию + детектирование + сегментацию без агента."""
    import json
    from datetime import datetime, timezone
    from db import source
    from knowledge.loader import load_knowledge
    from pipeline import aggregator, detector, segmenter
    from pipeline.runner import RunContext
    from agent.prompt import build_user_prompt

    day = date.fromisoformat(seg_date)
    from config import get_tz as _get_tz
    from datetime import timedelta
    day_start = datetime(day.year, day.month, day.day, tzinfo=_get_tz()).astimezone(timezone.utc)
    day_end   = day_start + timedelta(days=1) - timedelta(seconds=1)

    history      = await source.get_daily_history(router_sn, equip_type, panel_id, day)
    state_events = await source.get_daily_state_events(router_sn, equip_type, panel_id, day)
    events       = await source.get_daily_events(router_sn, equip_type, panel_id, day)

    has_data = bool(history or state_events)

    kb_path = await analytics.get_equipment_kb_path(router_sn, equip_type, panel_id)
    if kb_path:
        kb = load_knowledge(kb_path)
    else:
        kb = {"register_map": {}, "fault_bitmap_map": {}, "enum_map": {}, "operation_rules": {}}

    agg = aggregator.aggregate(history, kb["register_map"])
    uptime_min, starts_count, intervals = aggregator.calc_uptime_from_state_events(state_events)
    agg["uptime_minutes"]      = uptime_min
    agg["starts_count"]        = starts_count
    agg["operating_intervals"] = intervals

    anomalies = detector.detect(
        history=history,
        events=events,
        register_map=kb["register_map"],
        fault_bitmap_map=kb["fault_bitmap_map"],
        aggregates=agg,
    )
    segments = segmenter.segment(
        history=history,
        state_events=state_events,
        anomalies=anomalies,
        operation_rules=kb.get("operation_rules", {}),
        register_map=kb["register_map"],
        day_start=day_start,
        day_end=day_end,
    )

    def _ser(obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        raise TypeError

    segments_json = json.loads(json.dumps(segments, default=_ser))

    # Метаданные устройства для промпта
    equip_info = await analytics.get_equipment_registry()
    eq = next(
        (e for e in equip_info
         if e["router_sn"] == router_sn
         and e["equip_type"] == equip_type
         and str(e["panel_id"]) == str(panel_id)),
        {}
    )

    # Предпросмотр промпта (то, что получит агент)
    ctx = RunContext(
        router_sn=router_sn,
        equip_type=equip_type,
        panel_id=int(panel_id),
        day=day,
        manufacturer=eq.get("manufacturer") or "",
        model=eq.get("model") or "",
        engine_sn=eq.get("engine_sn") or "",
        equipment_name=eq.get("name") or "",
        kb_path=kb_path or "",
        register_map=kb["register_map"],
        fault_bitmap_map=kb.get("fault_bitmap_map", {}),
        enum_map=kb.get("enum_map", {}),
        operation_rules=kb.get("operation_rules", {}),
        aggregates=agg,
        history_series={},
        events=events,
        anomalies=anomalies,
        segments=segments_json,
    )
    prompt_preview = build_user_prompt(ctx)

    return {
        "date": str(day),
        "kb_path": kb_path or "",
        "has_data": has_data,
        "history_rows": len(history),
        "state_events_count": len(state_events),
        "anomalies_count": len(anomalies),
        "anomalies": anomalies,
        "uptime_minutes": agg.get("uptime_minutes", 0),
        "starts_count": agg.get("starts_count", 0),
        "segments": segments_json,
        "prompt_preview": prompt_preview,
        "equip_label": (
            f"{eq.get('name') or ''} "
            f"({eq.get('manufacturer') or ''} {eq.get('model') or ''})".strip()
        ),
    }


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
    equipment = await source.get_active_equipment()
    for eq in equipment:
        await analytics.sync_equipment_from_source(eq)
    return RedirectResponse(url="/settings", status_code=303)


# ── Helpers ───────────────────────────────────────────────────────────────────

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
