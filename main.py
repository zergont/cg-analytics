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

from config import settings, get_tz
from db.analytics import init_db, get_app_setting, set_app_setting
from analytics.source import init_source_pool, close_source_pool
from llm.client import apply_llm_settings, get_llm_settings
from llm.router import (
    apply_task, TASKS as _ROUTER_TASKS,
    set_signature_enabled,
)
from corpus.settings import apply_claude_settings, get_claude_settings
from scheduler import start_scheduler, stop_scheduler
from web.routes import router, _apply_tz
import online.manager as _online_mgr

class _TzFormatter(logging.Formatter):
    """%(asctime)s в часовом поясе приложения (config.get_tz()), а не в поясе ОС сервера.

    Обычный logging.Formatter берёт time.localtime() — если ОС сервера в UTC,
    текст записи расходится с часовым поясом, в котором вся остальная БД/UI
    показывают время (виден 3-часовой сдвиг МСК против сырого UTC в тексте).
    """
    def formatTime(self, record, datefmt=None):
        from datetime import datetime
        dt = datetime.fromtimestamp(record.created, tz=get_tz())
        return dt.strftime(datefmt) if datefmt else dt.isoformat()


_log_formatter = _TzFormatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

logging.basicConfig(level=settings.log_level)
for _h in logging.getLogger().handlers:
    _h.setFormatter(_log_formatter)
logger = logging.getLogger(__name__)

from web.log_buffer import BufferHandler as _BufHandler
_root_logger = logging.getLogger()
if not any(isinstance(h, _BufHandler) for h in _root_logger.handlers):
    _bh = _BufHandler()
    _bh.setFormatter(_TzFormatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S"))
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
    # Зачистка ключей удалённой задачи daily_agent (суточный агент выпилен в v4.9.3)
    from db.analytics import delete_app_settings_by_prefix as _del_settings
    _orphaned = await _del_settings("ai_task_daily_agent_")
    if _orphaned:
        logger.info("Удалены осиротевшие настройки daily_agent (%d шт.)", _orphaned)
    # Реестр моделей ИИ: загрузка из БД, при пустом — миграция из legacy-настроек
    from llm import registry as _llm_registry
    _reg_count = _llm_registry.load_registry(
        await get_app_setting(_llm_registry.REGISTRY_SETTING_KEY, "")
    )
    if _reg_count == 0:
        _llm_registry.migrate_from_legacy(get_llm_settings(), get_claude_settings())
        await set_app_setting(_llm_registry.REGISTRY_SETTING_KEY, _llm_registry.serialize())
        _reg_count = len(_llm_registry.get_entries())
    logger.info("Реестр моделей ИИ загружен (%d записей)", _reg_count)
    # Цепочки приоритетов моделей для corpus-задач
    import json as _json
    from llm.router import (
        apply_chain as _apply_chain, CHAIN_TASKS as _CHAIN_TASKS,
        get_all_chains as _get_all_chains,
    )
    for _task_id in _CHAIN_TASKS:
        try:
            _chain = _json.loads(await get_app_setting(f"ai_task_{_task_id}_chain", "[]"))
        except Exception:
            _chain = []
        _apply_chain(_task_id, _chain if isinstance(_chain, list) else [])
    logger.info("Цепочки моделей загружены: %s",
                {t: len(c) for t, c in _get_all_chains().items()})
    # «Кто и как разбирает» — одна таблица (v5.1.11): строки по видам работы,
    # провайдер и параметры в строке. Разовый перенос старых настроек: карточки
    # «Гейт — модель по уровню» и провайдеров задач; старые ключи не читаются
    # (остаются в БД до зачистки в v5.2.5 — откат на v5.1.10 безопасен)
    from llm.router import (
        load_depth as _load_depth, DEPTH_SETTING_KEY as _DEPTH_KEY,
        migrate_routes as _migrate_routes,
    )
    if await get_app_setting("ai_routes_version", "") != "5.1.11":
        _legacy = {k: await get_app_setting(k, "") for k in (
            "ai_warning_level_предупреждение_provider", "ai_warning_level_предупреждение_model",
            "ai_warning_level_внимание_provider", "ai_warning_level_внимание_model",
            "ai_task_human_auto_provider", "ai_task_seg_auto_provider",
        )}
        _seg_chain = _get_all_chains().get("seg_auto") or []
        _head = _llm_registry.get_entry(_seg_chain[0]) if _seg_chain else None
        if _head:
            _legacy["seg_chain_head_provider"] = (
                "api" if _head.get("provider") == "anthropic" else "llm")
        _new_depth, _route_changes = _migrate_routes(await get_app_setting(_DEPTH_KEY, ""), _legacy)
        if _new_depth is not None:
            await set_app_setting(_DEPTH_KEY, _new_depth)
        # Запасные заключений: шлюз основной, при сбое — облако (решение 12.10). Запись
        # шлюза остаётся первой ради безопасного отката (новая версия её пропускает)
        _keep = [e for e in _seg_chain
                 if (_llm_registry.get_entry(e) or {}).get("provider") in ("gorynych", "anthropic")]
        if _keep != _seg_chain:
            _apply_chain("seg_auto", _keep)
            await set_app_setting("ai_task_seg_auto_chain", _json.dumps(_keep))
            _route_changes.append(f"запасные заключений: {_seg_chain} → {_keep}")
        if _new_depth is None and _route_changes:
            # Битый JSON градации: ключ не трогаем (ручное восстановление), перенос
            # повторится при следующем старте
            logger.warning("Перенос настроек ИИ в таблицу не выполнен: %s", "; ".join(_route_changes))
        else:
            await set_app_setting("ai_routes_version", "5.1.11")
            logger.info("Настройки ИИ перенесены в таблицу «Кто и как разбирает»: %s",
                        "; ".join(_route_changes) or "без изменений")
    _load_depth(await get_app_setting(_DEPTH_KEY, ""))
    # Правила гейта (v5.1.12 — паспорт, честная панель; v5.1.15 — тренд ОЖ убран):
    # разовый перенос при смене версии — снятия по старым правилам в открытых
    # строках перестают действовать (гейт разберёт живой состав заново), фразы
    # о панели в промпте гейта (повторный перенос ничего не меняет)
    from online.status_assembler import GATE_RULES_VERSION as _GRV
    if await get_app_setting("gate_rules_version", "") != _GRV:
        try:
            from online.db import reset_open_gate_suppression as _reset_supp
            from llm.router import get_all as _get_routing, migrate_gate_prompt as _mig_gate
            _rows, _eps = await _reset_supp()
            _gp = _mig_gate(await get_app_setting("ai_task_warning_claude_prompt", ""))
            if _gp is not None:
                await set_app_setting("ai_task_warning_claude_prompt", _gp)
                apply_task("warning_claude", _get_routing()["warning_claude"]["provider"], _gp)
            await set_app_setting("gate_rules_version", _GRV)
            logger.info("Правила гейта %s: снятий сброшено — строк %d, эпизодов %d; "
                        "промпт гейта %s", _GRV, _rows, _eps,
                        "обновлён" if _gp is not None else "без изменений")
        except Exception:
            logger.warning("Правила гейта %s: перенос не выполнен — повторится при "
                           "следующем старте", _GRV, exc_info=True)
    # Сироты: status_auto (задачи нет с v4.x), llm_system_prompt (не читается)
    for _orph in ("ai_task_status_auto_", "llm_system_prompt"):
        _n = await _del_settings(_orph)
        if _n:
            logger.info("Удалены осиротевшие настройки %s* (%d шт.)", _orph, _n)
    # Загружаем настройку подписи модели в разборах (corpus/humanizer/гейт)
    set_signature_enabled(await get_app_setting("ai_signature_enabled", "true") == "true")
    # Загрузить режим источника телеметрии
    from db.source import set_source_mode as _set_source_mode
    _set_source_mode(await get_app_setting("source_mode", "external"))
    start_scheduler()
    # Запустить онлайн-мониторинг (Этап 1.5)
    mgr = _online_mgr.init_manager()
    await mgr.start_all_running()
    # Разборы аварий, оставшиеся без ответа (рестарт посреди совета) — через 2 мин
    from online.incident_gate import start_recovery as _start_incident_recovery
    _start_incident_recovery()
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
    # После движков: иначе ещё работающий движок закажет новый разбор
    from online.incident_gate import cancel_all as _cancel_incidents
    _n_inc = await _cancel_incidents()
    if _n_inc:
        logger.info("Сняты разборы аварий в работе: %d (закажет восстановление)", _n_inc)
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
