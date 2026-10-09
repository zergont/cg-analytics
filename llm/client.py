# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Async клиент моделей ИИ: Ollama, LM Studio / llama-server / DeepSeek
(OpenAI-совместимый API) и шлюз «Горыныч».

Все обращения к моделям в проекте идут через chat_stream()/chat() — провайдер
и параметры берутся из записи реестра (llm.registry) или глобального _cfg.

Шлюз «Горыныч» (provider gorynych) — OpenAI-совместимый, но со своей
очередью видеокарт, приоритетами и подменой моделей. Модель, глубину
рассуждения и приоритет запроса для него задаёт градация разбора
(llm.router, параметр depth): глубина задаётся видом разбора, а не выбором
сервера. Ошибки шлюза приходят с кодом — и в теле HTTP-ответа, и событием
внутри потока — и превращаются в LLMError.
"""
import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator

import httpx

logger = logging.getLogger(__name__)

# Ретраи при недоступности LLM: попытки и базовая задержка (2с, 4с)
_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY_SEC = 2.0

# Чтение — межбайтовый таймаут: в потоке его продлевает каждый кусок (у шлюза —
# и пинги раз в 15 с). Соединение и пул — короткие: недоступный сервер должен
# отваливаться быстро, а не через 600 с на каждую из трёх попыток.
_TIMEOUT = httpx.Timeout(600.0, connect=10.0, write=60.0, pool=10.0)

# Подмена транспорта для тестов (httpx.MockTransport); в работе — None
_TRANSPORT: httpx.AsyncBaseTransport | None = None

PROVIDERS = ("ollama", "lmstudio", "gorynych")

# Приоритеты очереди шлюза «Горыныч» (поле priority в теле запроса)
GORYNYCH_PRIORITIES = ("interactive", "normal", "batch", "background")
# Коды шлюза, после которых запрос можно повторить: оборван оператором или
# перезапуском шлюза. Остальные (backend_unavailable, council_*, 4xx) — сразу
# к следующей модели цепочки, повтор там ничего не даст.
_GATEWAY_RETRY_CODES = ("stopped", "gateway_stopping")
# Пауза перед повтором после остановки шлюза: он поднимается 10–30 с
_GATEWAY_RETRY_DELAY_SEC = 15.0


class LLMError(Exception):
    """Ошибка модели с кодом: из тела HTTP-ответа или события внутри потока.

    status — HTTP-код (у ошибки внутри потока — 200: поток уже открыт),
    code — машинный код шлюза (backend_unavailable, stopped, …) или None,
    task_id — номер запроса в журнале шлюза.
    """

    def __init__(self, status: int, code: str | None, message: str,
                 task_id: str | None = None, reason: str | None = None,
                 gateway: bool = False) -> None:
        self.status = status
        self.code = code
        self.message = message
        self.task_id = task_id
        self.reason = reason
        # Ошибка от шлюза «Горыныч»: только у него коды значат политику
        # повтора. У прямых серверов code — что угодно (llama-server кладёт
        # туда HTTP-статус числом), для них решает статус, как раньше
        self.gateway = gateway
        super().__init__(str(self))

    def __str__(self) -> str:
        tail = f" [task {self.task_id}]" if self.task_id else ""
        return f"LLMError({self.status} {self.code or '-'}: {self.message}){tail}"


def _error_from_body(status: int, body: dict, headers=None,
                     gateway: bool = False) -> LLMError | None:
    """LLMError из OpenAI-подобного тела {"error": {...}}; None — тело не про ошибку."""
    err = body.get("error") if isinstance(body, dict) else None
    if err is None:
        return None
    if isinstance(err, str):
        err = {"message": err}
    task_id = (err.get("task_id") or body.get("task_id")
               or (headers.get("x-gorynych-task-id") if headers is not None else None))
    return LLMError(status, err.get("code"), str(err.get("message") or "ошибка без описания"),
                    task_id=task_id, reason=err.get("reason") or body.get("reason"),
                    gateway=gateway)


def retriable_llm_error(exc: Exception) -> bool:
    """Стоит ли повторять запрос к LLM: сетевые ошибки, 429 и 5xx.

    Ошибку шлюза с кодом повторяем только при его остановке; остальное — по
    HTTP-статусу, как раньше (у прямых серверов тело ошибки тоже бывает с
    кодом: llama-server на 503 «Loading model» — и его надо повторить).
    """
    if isinstance(exc, LLMError):
        if exc.gateway and exc.code:
            return exc.code in _GATEWAY_RETRY_CODES
        return exc.status == 429 or exc.status >= 500
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or code >= 500
    return False


def _is_gateway_stop(exc: Exception) -> bool:
    return isinstance(exc, LLMError) and exc.gateway and exc.code in _GATEWAY_RETRY_CODES


def _retry_delay(exc: Exception, attempt: int, gateway_restart: bool = False) -> float:
    """Пауза перед повтором. После остановки шлюза — длинная до конца попыток:
    перезапускаясь, шлюз дальше отвечает уже отказом соединения, и обычные
    2 + 4 с не покрыли бы его подъём (10–30 с)."""
    if gateway_restart or _is_gateway_stop(exc):
        return _GATEWAY_RETRY_DELAY_SEC * attempt
    return _RETRY_BASE_DELAY_SEC * attempt


def _http_client() -> httpx.AsyncClient:
    if _TRANSPORT is not None:
        return httpx.AsyncClient(timeout=_TIMEOUT, transport=_TRANSPORT)
    return httpx.AsyncClient(timeout=_TIMEOUT)


# ── In-memory конфигурация LLM (применяется при старте и через веб-морду) ──────
# Системные промпты живут в AI-роутере (llm/router.py) — здесь только подключение.
_cfg: dict = {
    "provider":    "ollama",   # ollama | lmstudio (OpenAI-совместимый API)
    "base_url":    "http://localhost:11434",
    "model":       "qwen2.5:14b",
    "temperature": 0.1,
    "num_ctx":     16384,      # только Ollama; в LM Studio контекст задаётся при загрузке модели
    "stream":      True,       # False — модели без поддержки стриминга (например gemma)
}


def apply_llm_settings(
    base_url: str,
    model: str,
    temperature: float,
    num_ctx: int,
    stream: bool = True,
    provider: str = "ollama",
) -> None:
    """Обновить конфигурацию LLM в памяти (вступает в силу немедленно)."""
    _cfg["provider"]    = provider if provider in PROVIDERS else "ollama"
    _cfg["base_url"]    = _clean_base_url(base_url, _cfg["provider"])
    _cfg["model"]       = model.strip()
    _cfg["temperature"] = float(temperature)
    _cfg["num_ctx"]     = int(num_ctx)
    _cfg["stream"]      = bool(stream)
    logger.info("LLM настройки обновлены: provider=%s model=%s url=%s num_ctx=%d stream=%s",
                _cfg["provider"], _cfg["model"], _cfg["base_url"], _cfg["num_ctx"], _cfg["stream"])


def get_llm_settings() -> dict:
    """Вернуть копию текущей конфигурации LLM."""
    return dict(_cfg)


def _clean_base_url(base_url: str, provider: str) -> str:
    """Адрес без хвостового «/»; у шлюза ещё и без «/v1» — клиент добавляет его сам."""
    url = (base_url or "").strip().rstrip("/")
    if provider == "gorynych" and url.endswith("/v1"):
        url = url[:-3].rstrip("/")
    return url


# ── Запрос/разбор по провайдерам ───────────────────────────────────────────────

# Провайдеры с OpenAI-совместимым API (/v1/chat/completions):
# LM Studio, llama-server (llama.cpp), DeepSeek, шлюз «Горыныч»
_OPENAI_COMPAT = ("lmstudio", "llamacpp", "deepseek", "gorynych")


# Режим рассуждения (llm.registry.REASONING_LEVELS): "default" — ничего не передаём.
_THINK_TAG_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)


def strip_think(text: str) -> str:
    """Убрать блок рассуждений <think>…</think>, если сервер вклеил его в текст ответа."""
    if "<think>" not in text:
        return text
    cleaned = _THINK_TAG_RE.sub("", text)
    # незакрытый <think> (оборванный ответ) — отрезаем всё после него
    return cleaned.split("<think>", 1)[0].strip() if "<think>" in cleaned else cleaned.strip()


def _apply_reasoning(payload: dict, provider: str, level: str, system: str) -> str:
    """Перевести уровень рассуждения на язык провайдера; вернуть системный промпт.

    OpenAI-совместимые (LM Studio, llama-server): reasoning_effort + enable_thinking;
    Ollama: think (false | уровень); DeepSeek: thinking.type.
    «off» дополнительно подмешивает /no_think — мягкий выключатель Qwen3,
    работает даже если сервер не знает chat_template_kwargs.
    """
    if not level or level == "default":
        return system
    off = level == "off"
    if provider == "gorynych":
        # Шлюз сам ставит выключатель рассуждений под каждую голову:
        # chat_template_kwargs к головам не пропускает, /no_think в system не
        # учитывает — только reasoning_effort
        payload["reasoning_effort"] = "none" if off else level
        return system
    if provider == "deepseek":
        payload["thinking"] = {"type": "disabled" if off else "enabled"}
    elif provider in _OPENAI_COMPAT:
        # LM Studio (проверено на qwen3.8): выключает только reasoning_effort="none",
        # enable_thinking игнорирует; enable_thinking оставлен для llama-server/Qwen3
        payload["reasoning_effort"] = "none" if off else level
        payload["chat_template_kwargs"] = {"enable_thinking": not off}
    else:  # ollama
        payload["think"] = False if off else level
    if off and provider != "deepseek":
        system = (system + "\n/no_think") if system else "/no_think"
    return system


def _build_request(
    system: str, user: str, model: str | None, stream: bool, cfg: dict,
    reasoning: str | None = None, priority: str | None = None,
) -> tuple[str, dict, dict]:
    """URL, payload и заголовки под провайдера из cfg (глобальный _cfg или запись реестра).

    reasoning — переопределение режима рассуждения (иначе cfg["reasoning"]).
    priority  — класс очереди шлюза «Горыныч»; другим провайдерам не уходит.
    """
    mdl = (model or cfg["model"]).strip()
    provider = cfg.get("provider", "ollama")
    headers: dict = {}
    if cfg.get("api_key"):
        headers["Authorization"] = f"Bearer {cfg['api_key']}"
    if provider in _OPENAI_COMPAT:
        # OpenAI-совместимый API: LM Studio (контекст задаётся при загрузке модели) и DeepSeek
        url, payload = f"{cfg['base_url']}/v1/chat/completions", {
            "model":       mdl,
            "stream":      stream,
            "temperature": cfg["temperature"],
        }
    else:
        url, payload = f"{cfg['base_url']}/api/chat", {
            "model":    mdl,
            "stream":   stream,
            "options": {
                "temperature": cfg["temperature"],
                "num_ctx":     cfg["num_ctx"],
            },
        }
    level = reasoning if reasoning is not None else cfg.get("reasoning", "default")
    system = _apply_reasoning(payload, provider, level, system)
    if provider == "gorynych":
        # Пометка о подмене модели — только в gorynych.note, не в тексте разбора;
        # fallback — не влез промпт или голова лежит → ответит та, что может
        payload["notes"] = False
        payload["fallback"] = True
        payload["priority"] = priority if priority in GORYNYCH_PRIORITIES else "normal"
        # Температура уходит всегда: на уровнях с рассуждениями шлюз заменяет её
        # сам (у Qwen при 0.1–0.3 рассуждение ходит по кругу), без рассуждений —
        # берёт нашу. Какой уровень у «по умолчанию», знает только шлюз
    payload["messages"] = [
        {"role": "system", "content": system},
        {"role": "user",   "content": user},
    ]
    return url, payload, headers


async def _iter_ollama_stream(response) -> AsyncIterator[str]:
    """NDJSON-стрим Ollama: по JSON-объекту на строку, конец — done=true."""
    async for line in response.aiter_lines():
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        token = data.get("message", {}).get("content", "")
        if token:
            yield token
        if data.get("done"):
            logger.info("LLM завершил генерацию: eval_count=%s", data.get("eval_count", "?"))
            return


# Поля ответа, которые отдаются вызывающему через meta (подпись, отладка, токены)
_META_KEYS = ("gorynych", "usage", "model")


def _collect_meta(data: dict, meta: dict | None) -> None:
    if meta is None or not isinstance(data, dict):
        return
    for k in _META_KEYS:
        if data.get(k):
            meta[k] = data[k]


async def _iter_openai_stream(response, meta: dict | None = None,
                              gateway: bool = False) -> AsyncIterator[str]:
    """SSE-стрим OpenAI-совместимого API: 'data: {...}', конец — 'data: [DONE]'.

    Строки-комментарии (': ping' шлюза) пропускаются. Событие
    'data: {"error": …}' — ошибка, случившаяся после открытия потока (HTTP уже
    200): бросаем LLMError, иначе она молча превращалась в пустой ответ.
    """
    async for line in response.aiter_lines():
        if not line or not line.startswith("data:"):
            continue
        data_str = line[5:].strip()
        if data_str == "[DONE]":
            logger.info("LLM завершил генерацию (поток)")
            return
        try:
            data = json.loads(data_str)
        except json.JSONDecodeError:
            continue
        err = _error_from_body(200, data, response.headers, gateway=gateway)
        if err is not None:
            raise err
        _collect_meta(data, meta)
        choices = data.get("choices") or []
        if choices:
            token = (choices[0].get("delta") or {}).get("content", "")
            if token:
                yield token


def _extract_content(data: dict, cfg: dict) -> str:
    """Текст ответа из non-stream JSON под провайдера из cfg."""
    if cfg.get("provider") in _OPENAI_COMPAT:
        choices = data.get("choices") or []
        return (choices[0].get("message") or {}).get("content", "") if choices else ""
    return data.get("message", {}).get("content", "")


# ── Публичный API ──────────────────────────────────────────────────────────────

async def _raise_for_status(response, stream_mode: bool, gateway: bool = False) -> None:
    """HTTP-ошибка с разбором тела: у шлюза в нём код и task_id."""
    if response.is_success:
        return
    if stream_mode:
        await response.aread()
    try:
        body = response.json()
    except Exception:
        body = None
    err = (_error_from_body(response.status_code, body, response.headers, gateway=gateway)
           if response.status_code >= 400 and body else None)
    if err is not None:
        raise err
    response.raise_for_status()


def resolve_depth(cfg: dict, depth: str | None, model: str | None,
                  reasoning: str | None, priority: str | None,
                  ) -> tuple[str | None, str | None, str | None]:
    """Модель, рассуждение и приоритет запроса с учётом градации разбора.

    Градация (llm.router.get_depth) действует только на шлюз «Горыныч»: там
    глубина разбора задаётся видом разбора. Для прямых серверов depth не
    значит ничего — поведение прежнее.
    """
    if not depth or cfg.get("provider") != "gorynych":
        return model, reasoning, priority
    from llm.router import get_depth
    prof = get_depth(depth)
    # Модель вызывающего здесь не берём: у гейта это имя для прямого сервера
    # (путь к gguf), шлюз его не знает. Пустая модель в градации — модель
    # подключения (запись реестра или общие настройки)
    return (prof.get("model") or None,
            prof.get("reasoning") or reasoning,
            prof.get("priority") or priority)


async def chat_stream(
    system: str,
    user: str,
    *,
    model: str | None = None,
    stream: bool | None = None,
    entry: dict | None = None,
    reasoning: str | None = None,
    priority: str | None = None,
    depth: str | None = None,
    meta: dict | None = None,
) -> AsyncIterator[str]:
    """Ответ LLM токен за токеном (или одним блоком при stream=False).

    Единая точка вызова для всех потребителей (ручной анализ, humanizer,
    corpus-воркер, playground). Ретраит сеть/429/5xx, но только до первого
    отданного токена — часть ответа уже у клиента.

    entry — запись реестра моделей (llm.registry): параметры подключения
    берутся из неё, лимит одновременных запросов — её семафор. Без entry —
    глобальный _cfg (legacy-путь, настройка «LLM — локальная модель»).

    depth — вид разбора для градации (llm.router.DEPTH_KINDS): у шлюза задаёт
    модель, рассуждение и приоритет. priority — класс очереди шлюза напрямую.
    meta — словарь, который клиент заполняет полями ответа: gorynych (какая
    голова реально ответила, task_id, note), usage, model. Чистится на каждой
    попытке, чтобы не отдать данные неудачной.
    """
    cfg = entry if entry is not None else _cfg
    gateway = cfg.get("provider") == "gorynych"
    model, reasoning, priority = resolve_depth(cfg, depth, model, reasoning, priority)
    # Шлюз — всегда потоком: без потока долгий разбор упирается в 600 с без
    # единого байта; псевдопоток шлюза с пингами этого не допускает
    use_stream = True if gateway else (cfg.get("stream", True) if stream is None else stream)
    url, payload, headers = _build_request(system, user, model, use_stream, cfg,
                                           reasoning, priority)

    logger.info("LLM запрос: provider=%s model=%s prompt_len=%d stream=%s%s",
                cfg.get("provider"), payload["model"], len(user), use_stream,
                f" priority={payload.get('priority')} reasoning={payload.get('reasoning_effort', '-')}"
                if gateway else "")

    if entry is not None:
        from llm.registry import get_semaphore
        sem = get_semaphore(entry["id"])
    else:
        sem = None

    if sem is not None:
        await sem.acquire()
    try:
        gateway_restart = False
        for attempt in range(1, _RETRY_ATTEMPTS + 1):
            yielded = False
            if meta is not None:
                meta.clear()
            try:
                if use_stream:
                    async with _http_client() as client:
                        async with client.stream("POST", url, json=payload, headers=headers) as response:
                            await _raise_for_status(response, stream_mode=True, gateway=gateway)
                            it = (_iter_openai_stream(response, meta, gateway=gateway)
                                  if cfg.get("provider") in _OPENAI_COMPAT
                                  else _iter_ollama_stream(response))
                            async for token in it:
                                yielded = True
                                yield token
                    _log_gateway(meta)
                    return
                else:
                    async with _http_client() as client:
                        resp = await client.post(url, json=payload, headers=headers)
                        await _raise_for_status(resp, stream_mode=False, gateway=gateway)
                        data = resp.json()
                        _collect_meta(data, meta)
                        content = _extract_content(data, cfg)
                        logger.info("LLM завершил генерацию (no-stream): %d символов", len(content))
                        if content:
                            yield content
                    _log_gateway(meta)
                    return
            except Exception as exc:
                if yielded or attempt == _RETRY_ATTEMPTS or not retriable_llm_error(exc):
                    raise
                gateway_restart = gateway_restart or _is_gateway_stop(exc)
                delay = _retry_delay(exc, attempt, gateway_restart)
                logger.warning("LLM недоступен (попытка %d/%d), повтор через %.0fс: %r",
                               attempt, _RETRY_ATTEMPTS, delay, exc)
                await asyncio.sleep(delay)
    finally:
        if sem is not None:
            sem.release()


def _log_gateway(meta: dict | None) -> None:
    g = (meta or {}).get("gorynych")
    if g:
        logger.info("Горыныч: ответила %s (%s), task=%s, %s мс%s",
                    g.get("backend"), g.get("reasoning_level"), g.get("task_id"),
                    g.get("duration_ms"), f", подмена: {g['note']}" if g.get("note") else "")


def gateway_label(meta: dict | None, fallback: str) -> str:
    """Подпись модели по реально ответившей голове шлюза: «flashnext (xhigh)».

    Без метаданных шлюза — fallback (имя модели из настроек), как раньше.
    """
    g = (meta or {}).get("gorynych") or {}
    backend = g.get("backend")
    if not backend:
        return fallback
    level = g.get("reasoning_level")
    return f"{backend} ({level})" if level else str(backend)


async def chat(
    system: str, user: str, *, model: str | None = None,
    entry: dict | None = None, stream: bool | None = None,
    reasoning: str | None = None, priority: str | None = None,
    depth: str | None = None, meta: dict | None = None,
) -> str:
    """Ответ LLM одной строкой (с ретраями).

    stream=None — без потока (у шлюза — всегда поток, см. chat_stream); для
    цепочек corpus передаётся stream=True: поток токенов служит признаком
    «сервер жив», таймаут httpx становится межтокенным, а не на весь ответ.
    """
    use_stream = stream if stream is not None else False
    parts = [t async for t in chat_stream(system, user, model=model,
                                          stream=use_stream, entry=entry,
                                          reasoning=reasoning, priority=priority,
                                          depth=depth, meta=meta)]
    return strip_think("".join(parts)).strip()


async def list_server_models(provider: str, base_url: str, api_key: str = "") -> list[str]:
    """Имена моделей, которые отдаёт сервер: Ollama — /api/tags, остальные — /v1/models.

    Бросает исключение при недоступности; для anthropic список не запрашивается.
    """
    if provider == "anthropic":
        return []
    base_url = _clean_base_url(base_url, provider)
    url = f"{base_url}/api/tags" if provider == "ollama" else f"{base_url}/v1/models"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    async with httpx.AsyncClient(timeout=10.0, transport=_TRANSPORT) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    if provider == "ollama":
        return [m.get("name", "") for m in data.get("models", []) if m.get("name")]
    return [m.get("id", "") for m in data.get("data", []) if m.get("id")]


async def ping_entry(entry: dict) -> dict:
    """Проверка доступности подключения из реестра (кнопка «Проверить» в UI).

    Возвращает {"ok": bool, "detail": str}. Модель не дёргаем — только список
    моделей эндпоинта; для anthropic проверяем лишь наличие ключа в конфигурации.
    """
    provider = entry.get("provider")
    if provider == "anthropic":
        import os
        from config import settings as _app_settings
        has_key = bool(_app_settings.anthropic_api_key
                       or os.environ.get("ANTHROPIC_API_KEY")
                       or entry.get("api_key"))
        return {"ok": has_key,
                "detail": "ключ API найден (config.yml)" if has_key
                          else "ключ не задан ни в config.yml, ни в ANTHROPIC_API_KEY"}
    try:
        models = await list_server_models(provider, entry["base_url"], entry.get("api_key", ""))
    except Exception as exc:
        return {"ok": False, "detail": repr(exc)}
    if entry.get("model") and entry["model"] not in models:
        shown = ", ".join(models[:5]) + (" …" if len(models) > 5 else "")
        return {"ok": True,
                "detail": f"сервер доступен, но модель «{entry['model']}» не найдена. "
                          f"Сервер отдаёт: {shown or '—'}"}
    return {"ok": True, "detail": f"доступен, моделей: {len(models)}"}


async def stream_analysis(md_packet: str) -> AsyncIterator[str]:
    """Заключение по Markdown-пакету телеметрии (страница ручного анализа).

    Args:
        md_packet: Markdown-пакет телеметрии (выход _build_analysis_md)

    Yields:
        Строки-токены по мере генерации (или вся строка сразу при stream=False).
    """
    from llm.router import get_prompt
    system = get_prompt("analyze_page")
    async for token in chat_stream(system, md_packet, depth="manual", priority="interactive"):
        yield token
