"""Точка входа cg-analytics: FastAPI + APScheduler."""
import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from config import settings
from db.analytics import init_db
from scheduler import start_scheduler, stop_scheduler
from web.routes import router

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("═══ cg-analytics запуск ═══")
    await init_db()
    start_scheduler()
    yield
    stop_scheduler()
    logger.info("═══ cg-analytics остановлен ═══")


app = FastAPI(
    title="cg-analytics",
    description="Интеллектуальная аналитика телеметрии генераторных установок",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(router)


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=settings.web_host,
        port=settings.web_port,
        reload=False,
        log_level=settings.log_level.lower(),
    )
