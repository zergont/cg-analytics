"""FastAPI роуты Web UI аналитики."""
import asyncio
import logging
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

# Хранилище активных задач запуска (task_key → dict)
_running_tasks: dict[str, dict] = {}


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


# ── Knowledge Base ────────────────────────────────────────────────────────────

@router.get("/knowledge", response_class=HTMLResponse)
async def knowledge_page(request: Request):
    from config import settings
    equipment_dir = settings.knowledge_base_path / "equipment"

    models = []
    if equipment_dir.exists():
        for mfr_dir in sorted(equipment_dir.iterdir()):
            if not mfr_dir.is_dir():
                continue
            for model_dir in sorted(mfr_dir.iterdir()):
                if not model_dir.is_dir():
                    continue
                reg_count = _count_lines(model_dir / "register_map.jsonl")
                fault_count = _count_lines(model_dir / "fault_bitmap_map.jsonl")
                pdf_count = len(list((model_dir / "docs").glob("*.pdf"))) if (model_dir / "docs").exists() else 0
                models.append({
                    "manufacturer": mfr_dir.name,
                    "model": model_dir.name,
                    "registers": reg_count,
                    "faults": fault_count,
                    "pdfs": pdf_count,
                })

    return templates.TemplateResponse(request, "knowledge.html", {"models": models})


@router.post("/knowledge/reindex")
async def reindex(
    manufacturer: str = Form(...),
    model: str = Form(...),
):
    """Запустить переиндексацию в фоне."""
    async def _reindex():
        from knowledge.indexer import index_equipment
        from knowledge.retriever import invalidate_cache
        from knowledge.loader import invalidate_cache as loader_invalidate
        try:
            count = index_equipment(manufacturer, model)
            invalidate_cache(manufacturer, model)
            loader_invalidate(manufacturer, model)
            logger.info("Переиндексация завершена: %s/%s, %d документов", manufacturer, model, count)
        except Exception as e:
            logger.exception("Ошибка переиндексации: %s", e)

    asyncio.create_task(_reindex())
    return RedirectResponse(url="/knowledge", status_code=303)


# ── Настройки ─────────────────────────────────────────────────────────────────

@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    from config import settings as cfg
    registry = await analytics.get_equipment_registry()
    return templates.TemplateResponse(request, "settings.html", {
        "settings": cfg,
        "registry": registry,
    })


@router.post("/settings/equipment/update")
async def update_equipment(
    router_sn: str = Form(...),
    equip_type: str = Form(...),
    panel_id: int = Form(...),
    manufacturer: str = Form(""),
    model: str = Form(""),
    engine_sn: str = Form(""),
    name: str = Form(""),
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


@router.post("/settings/sync")
async def sync_equipment():
    """Синхронизировать реестр аналитики с основной БД."""
    equipment = await source.get_active_equipment()
    for eq in equipment:
        await analytics.upsert_equipment(eq)
    return RedirectResponse(url="/settings", status_code=303)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _count_lines(path) -> int:
    if not path or not path.exists():
        return 0
    try:
        return sum(1 for line in path.open(encoding="utf-8") if line.strip())
    except OSError:
        return 0
