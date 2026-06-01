"""Точка входа cg-analytics: FastAPI + APScheduler."""
import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from config import settings
from db.analytics import init_db, get_app_setting
from analytics.source import init_source_pool, close_source_pool
from llm.client import apply_llm_settings, get_llm_settings
from scheduler import start_scheduler, stop_scheduler
from web.routes import router, _apply_tz
import online.manager as _online_mgr

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

from web.log_buffer import BufferHandler as _BufHandler
_bh = _BufHandler()
_bh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S"))
logging.getLogger().addHandler(_bh)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("═══ cg-analytics запуск ═══")
    await init_db()
    await init_source_pool()
    # Загружаем сохранённый часовой пояс из БД (если пользователь менял через UI)
    saved_tz = await get_app_setting("timezone", settings.timezone_name)
    _apply_tz(saved_tz)   # обновляет config.get_tz() и глобал Jinja2
    logger.info("Часовой пояс: %s", saved_tz)
    # Загружаем настройки LLM из БД
    _defaults = get_llm_settings()
    apply_llm_settings(
        base_url    = await get_app_setting("llm_base_url",    _defaults["base_url"]),
        model       = await get_app_setting("llm_model",       _defaults["model"]),
        temperature = float(await get_app_setting("llm_temperature", str(_defaults["temperature"]))),
        num_ctx     = int(await get_app_setting("llm_num_ctx",       str(_defaults["num_ctx"]))),
        prompt      = await get_app_setting("llm_system_prompt",     _defaults["prompt"]),
    )
    start_scheduler()
    # Запустить онлайн-мониторинг (Этап 1.5)
    mgr = _online_mgr.init_manager()
    await mgr.start_all_running()
    yield
    stop_scheduler()
    await _online_mgr.stop_manager()
    await close_source_pool()
    logger.info("═══ cg-analytics остановлен ═══")


app = FastAPI(
    title="cg-analytics",
    description="Интеллектуальная аналитика телеметрии генераторных установок",
    version="1.0.0",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory="web/static"), name="static")
app.include_router(router)


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=settings.web_host,
        port=settings.web_port,
        reload=False,
        log_level=settings.log_level.lower(),
    )
