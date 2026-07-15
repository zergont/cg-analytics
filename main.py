# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

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
from llm.router import (
    apply_task, TASKS as _ROUTER_TASKS,
    apply_warning_level_route, WARNING_LEVELS as _WARNING_LEVELS,
    get_all_warning_level_routes as _get_warning_level_routes,
)
from corpus.settings import apply_claude_settings, get_claude_settings
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
_root_logger = logging.getLogger()
if not any(isinstance(h, _BufHandler) for h in _root_logger.handlers):
    _bh = _BufHandler()
    _bh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S"))
    _root_logger.addHandler(_bh)


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
        base_url       = await get_app_setting("llm_base_url",       _defaults["base_url"]),
        model          = await get_app_setting("llm_model",          _defaults["model"]),
        temperature    = float(await get_app_setting("llm_temperature",    str(_defaults["temperature"]))),
        num_ctx        = int(await get_app_setting("llm_num_ctx",       str(_defaults["num_ctx"]))),
        stream         = await get_app_setting("llm_stream", "true") == "true",
        provider       = await get_app_setting("llm_provider", _defaults.get("provider", "ollama")),
    )
    # Загружаем настройки Claude API из БД
    _claude_defaults = get_claude_settings()
    apply_claude_settings(
        model=          await get_app_setting("claude_model",          _claude_defaults["model"]),
        max_tool_calls= int(await get_app_setting("claude_max_tool_calls", str(_claude_defaults["max_tool_calls"]))),
        max_tokens=     int(await get_app_setting("claude_max_tokens",     str(_claude_defaults["max_tokens"]))),
        proxy=          await get_app_setting("claude_proxy",          _claude_defaults["proxy"]),
    )
    # Загружаем маршрутизацию AI-задач из БД
    for _task_id, (_label, _def_provider, _def_prompt) in _ROUTER_TASKS.items():
        _provider = await get_app_setting(f"ai_task_{_task_id}_provider", _def_provider)
        _prompt   = await get_app_setting(f"ai_task_{_task_id}_prompt",   _def_prompt)
        apply_task(_task_id, _provider, _prompt)
    logger.info("AI router загружен (%d задач)", len(_ROUTER_TASKS))
    # Загружаем маршрутизацию гейта предупреждений по уровням серьёзности
    _wl_defaults = _get_warning_level_routes()
    for _level in _WARNING_LEVELS:
        _wl_provider = await get_app_setting(f"ai_warning_level_{_level}_provider", _wl_defaults[_level]["provider"])
        _wl_model    = await get_app_setting(f"ai_warning_level_{_level}_model",    _wl_defaults[_level]["model"])
        apply_warning_level_route(_level, _wl_provider, _wl_model)
    logger.info("Гейт предупреждений: маршрутизация по уровням загружена (%d уровня)", len(_WARNING_LEVELS))
    # Загрузить режим источника телеметрии
    from db.source import set_source_mode as _set_source_mode
    _set_source_mode(await get_app_setting("source_mode", "external"))
    start_scheduler()
    # Запустить онлайн-мониторинг (Этап 1.5)
    mgr = _online_mgr.init_manager()
    await mgr.start_all_running()
    # Запустить Claude-конвейер (Этап 2)
    import asyncio as _asyncio
    from corpus.worker import init_worker as _init_worker
    _corpus_worker = _init_worker()
    _corpus_worker._task = _asyncio.create_task(_corpus_worker.run())
    _corpus_auto = await get_app_setting("corpus_auto_analyze", "false")
    if _corpus_auto == "true":
        pending = await _corpus_worker.enqueue_pending()
        if pending:
            logger.info("corpus: авто-старт — %d сегментов добавлено в очередь", pending)
    else:
        logger.info("corpus: авто-анализ выключен (включить в Настройки → ИИ-конвейер)")
    # Запустить Qwen-конвейер (очеловечивание Claude-заключений)
    from corpus.qwen_worker import init_worker as _init_qwen_worker
    _qwen_worker = _init_qwen_worker()
    _qwen_worker._task = _asyncio.create_task(_qwen_worker.run())
    _qwen_auto = await get_app_setting("qwen_auto_analyze", "false")
    if _qwen_auto == "true":
        qwen_pending = await _qwen_worker.enqueue_pending()
        if qwen_pending:
            logger.info("qwen: авто-старт — %d сегментов добавлено в очередь", qwen_pending)
    else:
        logger.info("qwen: авто-анализ выключен (включить в Настройки → ИИ-конвейер)")
    yield
    stop_scheduler()
    await _online_mgr.stop_manager()
    await _corpus_worker.stop()
    await _qwen_worker.stop()
    await close_source_pool()
    from db.pool import close_pools
    await close_pools()
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
