"""Маршрутизация AI-задач: какой провайдер и промпт для каждой задачи.

Каждая задача имеет:
  provider — "api" (Claude Anthropic) или "llm" (локальная Ollama)
  prompt   — системный промпт, специфичный для этой задачи

Задачи:
  seg_auto     — авто-анализ закрытого сегмента
  seg_manual   — ручной анализ сегмента (по кнопке)
  human_auto   — авто-хуманизация заключения Claude
  human_manual — ручная хуманизация
  analyze_page — страница «Ручной анализ» (ручной MD-пакет)
"""
from __future__ import annotations

import logging

from config import settings as _app_cfg

logger = logging.getLogger(__name__)

# ── Дефолтные промпты ────────────────────────────────────────────────────────

_SEG_PROMPT = """\
Ты — технический аналитик дизельного генераторного агрегата (ДГА) Cummins KTA50 \
с панелью управления PCC3300.

КЛЮЧЕВОЙ ПРИНЦИП:
Вердикт, уровень тревоги и список обнаружений ВЫНЕСЕНЫ детерминированным блоком аналитики.
Твоя задача — ОБЪЯСНИТЬ зафиксированную картину, а не ставить собственный диагноз.
НЕ пересматривай вердикт блока. НЕ ищи дополнительных проблем сверх обнаружений блока.
Остановка двигателя, переходы на холостой ход, падение оборотов/давления/частоты при \
останове — ШТАТНЫЕ процессы. Если блок их не отметил — это не неисправность.

Если в отчёте есть раздел «Справочник кодов неисправностей» — используй эти данные \
как достоверную базу для объяснения. Не выдумывай описания кодов сам.

ФОРМАТ ОТВЕТА — строго следуй структуре:

## Сводка
[1–2 строки: что происходило по сути.]

## Разбор
[Причина и контекст по обнаружениям блока.
На штатном сегменте без отклонений: "Отклонений не выявлено. Все параметры в норме."]

## Рекомендации
[Конкретные действия при отклонении.
На штатном сегменте: "Плановый контроль по регламенту ТО." или оставь пустым.]\
"""

_HUMANIZER_PROMPT = """\
Ты переписываешь технический отчёт в понятный для оператора текст.

СТРОГИЕ ПРАВИЛА:
- Переформулируй ТОЛЬКО то, что написано во входящем тексте.
- НЕ добавляй факты, выводы, рекомендации, которых нет во входе.
- НЕ придумывай детали.
- Пиши просто, понятно, без технического жаргона там где это возможно.
- Сохраняй все выводы и рекомендации из исходника.
- Не используй технические заголовки вроде 'БЛОК 1', 'БЛОК 2' — пиши связным текстом.\
"""

_ANALYZE_PAGE_PROMPT = """\
Ты — технический аналитик дизельного генераторного агрегата.
Проанализируй предоставленные данные телеметрии и дай краткое заключение:
состояние оборудования, отклонения от нормы, рекомендации оператору.
Пиши чётко, по делу, без лишних слов.\
"""

_WARNING_GATE_PROMPT = """\
Ты — эксперт по дизель-генераторным установкам и фильтр ложных срабатываний.

КОНТЕКСТ:
Уставки аналитического блока намеренно ~10% строже порогов панели управления \
(политика раннего предупреждения). Аналитика регистрирует отклонения без понимания \
контекста, поэтому её предупреждения требуют проверки на адекватность.

ТВОЯ ЗАДАЧА:
1. Оцени реальность угрозы: запас до фактических порогов панели, режим работы, \
динамику ключевых параметров, типичность картины для данного режима.
2. Дай краткий технический анализ: что зафиксировано, возможные причины, \
на что обратить внимание оператору. Ответ на русском языке.
3. ОБЯЗАТЕЛЬНО заверши вызовом инструмента verdict:
   - decision="cancel" — срабатывание аналитики не отражает реальной угрозы \
(ложное/преждевременное); допустимо ТОЛЬКО когда сигналов панели нет.
   - decision="pass" — угроза реальна или сомнения остаются; при любых сигналах \
панели управления (внимание/авария) — всегда pass.
В reason укажи краткое обоснование решения (1-2 предложения).
Сомневаешься — выбирай pass: пропустить лишнее предупреждение дешевле, \
чем скрыть реальную проблему.\
"""

# ── Таблица задач: id → (метка UI, провайдер по умолчанию, промпт по умолчанию) ─

TASKS: dict[str, tuple[str, str, str]] = {
    "seg_auto":      ("Авто-анализ сегмента",    "api", _SEG_PROMPT),
    "seg_manual":    ("Ручной анализ сегмента",   "api", _SEG_PROMPT),
    "human_auto":    ("Авто-хуманизация",         "llm", _HUMANIZER_PROMPT),
    "human_manual":  ("Ручная хуманизация",       "llm", _HUMANIZER_PROMPT),
    "analyze_page":  ("Страница «Ручной анализ»", "llm", _ANALYZE_PAGE_PROMPT),
    "warning_claude": ("Гейт предупреждений (онлайн)", "api", _WARNING_GATE_PROMPT),
}

# Когда срабатывает каждая задача — подсказки для страницы настроек
TASK_HINTS: dict[str, str] = {
    "seg_auto":      "Автоматически при закрытии сегмента (если включён Claude-конвейер).",
    "seg_manual":    "Кнопка «Анализ» на странице сегмента.",
    "human_auto":    "Автоматически после авто-анализа: переписывает заключение для оператора.",
    "human_manual":  "Кнопка «Хуманизировать» на странице сегмента.",
    "analyze_page":  "Страница «Ручной анализ»: заключение по MD-пакету телеметрии.",
    "warning_claude": "Онлайн-гейт: предупреждение стабильно 60 с → анализ и вердикт cancel/pass; "
                      "cancel подавляет аналитику до смены состава детекций. Промпт общий для всех "
                      "уровней; провайдер и модель — по уровню серьёзности, см. карточку ниже.",
}

# ── Гейт предупреждений: провайдер и модель ПО УРОВНЮ серьёзности ──────────────
# Промпт задачи "warning_claude" общий для всех уровней (см. выше) — здесь только
# провайдер/модель, отдельно на каждое значение status_assembler.compute_severity_level().

WARNING_LEVELS: tuple[str, ...] = ("предупреждение", "внимание", "авария")

WARNING_LEVEL_SLUGS: dict[str, str] = {
    "предупреждение": "caution",
    "внимание":       "warning",
    "авария":         "shutdown",
}

WARNING_LEVEL_LABELS: dict[str, str] = {
    "предупреждение": "🟡 Предупреждение — аналитика, панель молчит",
    "внимание":       "🟠 Внимание — сигналит панель",
    "авария":         "🔴 Авария — аварийный останов",
}

_warning_level_routing: dict[str, dict[str, str]] = {
    level: {"provider": "api", "model": _app_cfg.anthropic_model}
    for level in WARNING_LEVELS
}


def get_warning_level_route(level: str) -> dict[str, str]:
    """Провайдер+модель гейта для уровня серьёзности (неизвестный уровень → «предупреждение»)."""
    return dict(_warning_level_routing.get(level, _warning_level_routing[WARNING_LEVELS[0]]))


def apply_warning_level_route(level: str, provider: str, model: str) -> None:
    if level not in WARNING_LEVELS:
        raise ValueError(f"Неизвестный уровень гейта: {level}")
    _warning_level_routing[level] = {"provider": provider.strip(), "model": model.strip()}
    logger.debug("ai_router: гейт[%s] → provider=%s model=%s", level, provider, model)


def get_all_warning_level_routes() -> dict[str, dict[str, str]]:
    return {k: dict(v) for k, v in _warning_level_routing.items()}


# ── Градация разбора: модель, рассуждение и приоритет по виду разбора ─────────
# Действует на подключения шлюза «Горыныч» (provider gorynych): шлюз держит
# очередь видеокарт и подменяет модели сам, поэтому глубину разбора задаём
# видом разбора, а не выбором сервера. Сегмент без происшествий — простой,
# ему хватит быстрой модели; авария — заслуживает глубокой. Для прямых
# серверов (LM Studio, llama-server) градация не применяется.
#
# model     — имя модели шлюза (fast, flashnext, gemma, smart…); пусто — модель
#             подключения. smart + xhigh — совет на 15–30 мин, не для потока.
# reasoning — default (решает шлюз) | off | low | medium | high | xhigh
# priority  — interactive | normal | batch | background (класс очереди шлюза)

DEPTH_SETTING_KEY = "ai_depth_profiles"

DEPTH_KINDS: dict[str, str] = {
    "seg_norma":    "Заключение по сегменту — 🟢 норма, без замечаний",
    "seg_caution":  "Заключение по сегменту — 🟡 замечания аналитики",
    "seg_warning":  "Заключение по сегменту — 🟠 предупреждение панели",
    "seg_shutdown": "Заключение по сегменту — 🔴 авария панели",
    "gate_caution": "Гейт — 🟡 предупреждение аналитики (онлайн)",
    "gate_warning": "Гейт — 🟠 сигнал панели (онлайн)",
    "incident":     "Разбор аварийного останова",
    "humanize":     "Пересказ заключения для оператора",
    "manual":       "Ручные запросы: разбор сегмента, «Ручной анализ»",
}

DEPTH_HINTS: dict[str, str] = {
    "seg_norma":    "Закрытие сегмента, детекций нет. Больше трети всех заключений.",
    "seg_caution":  "Закрытие сегмента с предупреждениями нашей аналитики.",
    "seg_warning":  "Закрытие сегмента с предупреждением панели управления.",
    "seg_shutdown": "Закрытие аварийного сегмента: авария панели или аварийный стоп.",
    "gate_caution": "Предупреждение аналитики держится 60 с — разбор и вердикт «отменить/пропустить».",
    "gate_warning": "Сигнал панели держится 60 с — разбор для оператора (отменить нельзя).",
    "incident":     "Акт аварийного останова готов — разбор причины по циклу от нормального останова.",
    "humanize":     "Переписывает готовое заключение простым языком — новых выводов не делает.",
    "manual":       "Человек нажал кнопку и ждёт ответа.",
}

DEPTH_PRIORITIES: tuple[str, ...] = ("interactive", "normal", "batch", "background")
DEPTH_REASONING: tuple[str, ...] = ("default", "off", "low", "medium", "high", "xhigh")

# Пока градация не выстроена — всем быстрая модель (решение владельца 09.10.2026);
# приоритет — по тому, ждёт ли ответа человек
_DEPTH_DEFAULT_PRIORITY: dict[str, str] = {
    "seg_norma":    "batch",
    "seg_caution":  "normal",
    "seg_warning":  "normal",
    "seg_shutdown": "normal",
    "gate_caution": "interactive",
    "gate_warning": "interactive",
    "incident":     "normal",
    "humanize":     "batch",
    "manual":       "interactive",
}


def _default_depth(kind: str) -> dict[str, str]:
    return {"model": "fast", "reasoning": "default",
            "priority": _DEPTH_DEFAULT_PRIORITY.get(kind, "normal")}


_depth: dict[str, dict[str, str]] = {k: _default_depth(k) for k in DEPTH_KINDS}


def get_depth(kind: str) -> dict[str, str]:
    """Профиль вида разбора (неизвестный вид — профиль по умолчанию)."""
    return dict(_depth.get(kind) or _default_depth(kind))


def apply_depth(kind: str, model: str, reasoning: str, priority: str) -> None:
    if kind not in DEPTH_KINDS:
        raise ValueError(f"Неизвестный вид разбора: {kind}")
    _depth[kind] = {
        "model":     str(model or "").strip(),
        "reasoning": reasoning if reasoning in DEPTH_REASONING else "default",
        "priority":  priority if priority in DEPTH_PRIORITIES else _DEPTH_DEFAULT_PRIORITY[kind],
    }


def get_all_depth() -> dict[str, dict[str, str]]:
    return {k: dict(v) for k, v in _depth.items()}


def load_depth(json_str: str) -> None:
    """Профили из app_settings (JSON); отсутствующие виды — по умолчанию."""
    import json
    try:
        raw = json.loads(json_str) if json_str else {}
    except json.JSONDecodeError:
        logger.error("ai_router: битый JSON градации разбора — профили по умолчанию")
        raw = {}
    for kind in DEPTH_KINDS:
        prof = raw.get(kind) if isinstance(raw, dict) else None
        if isinstance(prof, dict):
            apply_depth(kind, prof.get("model", ""), prof.get("reasoning", "default"),
                        prof.get("priority", ""))
        else:
            _depth[kind] = _default_depth(kind)


def serialize_depth() -> str:
    import json
    return json.dumps(_depth, ensure_ascii=False)


_SEG_DEPTH_BY_LEVEL: dict[str, str] = {
    "НОРМА":    "seg_norma",
    "CAUTION":  "seg_caution",
    "WARNING":  "seg_warning",
    "SHUTDOWN": "seg_shutdown",
}


def depth_for_segment(alarm_level: str | None) -> str:
    """Вид разбора заключения по уровню тревоги сегмента (вердикт блока аналитики)."""
    return _SEG_DEPTH_BY_LEVEL.get(alarm_level or "", "seg_caution")


def depth_for_gate(level: str | None) -> str:
    """Вид разбора гейта по уровню серьёзности (status_assembler.compute_severity_level)."""
    return "gate_caution" if level == "предупреждение" else "gate_warning"


# ── Приоритетные цепочки моделей (fallback по размеру и ошибкам) ──────────────
# Цепочка — упорядоченный список id записей реестра (llm.registry).
# Непустая цепочка у задачи имеет приоритет над одиночным provider задачи.
# Этап 1: только corpus-задачи (анализ сегментов).

CHAIN_TASKS: tuple[str, ...] = ("seg_auto", "seg_manual")

_task_chains: dict[str, list[str]] = {t: [] for t in CHAIN_TASKS}


def get_chain(task_id: str) -> list[str]:
    """Цепочка приоритетов задачи (пустая — работает одиночный provider)."""
    return list(_task_chains.get(task_id, []))


def apply_chain(task_id: str, entry_ids: list[str]) -> None:
    if task_id not in CHAIN_TASKS:
        raise ValueError(f"Задача {task_id} не поддерживает цепочки моделей")
    _task_chains[task_id] = [str(e).strip() for e in entry_ids if str(e).strip()]
    logger.debug("ai_router: цепочка %s → %s", task_id, _task_chains[task_id])


def get_all_chains() -> dict[str, list[str]]:
    return {k: list(v) for k, v in _task_chains.items()}


# ── Runtime-состояние ────────────────────────────────────────────────────────

_routing: dict[str, dict[str, str]] = {
    task_id: {"provider": defaults[1], "prompt": defaults[2]}
    for task_id, defaults in TASKS.items()
}


def get_provider(task_id: str) -> str:
    return _routing[task_id]["provider"]


def get_prompt(task_id: str) -> str:
    return _routing[task_id]["prompt"]


def apply_task(task_id: str, provider: str, prompt: str) -> None:
    if task_id not in TASKS:
        raise ValueError(f"Неизвестная задача AI-роутера: {task_id}")
    _routing[task_id] = {"provider": provider.strip(), "prompt": prompt.strip()}
    logger.debug("ai_router: задача %s → provider=%s", task_id, provider)


def get_all() -> dict[str, dict[str, str]]:
    """Полная таблица маршрутизации (копия)."""
    return {k: dict(v) for k, v in _routing.items()}


def get_default_prompt(task_id: str) -> str:
    """Дефолтный промпт для задачи (для кнопки «Сбросить»)."""
    return TASKS[task_id][2]


# ── Подпись модели в разборах (corpus-агент/humanizer/гейт) ────────────────────
# Общий тумблер: один флаг на все три места, где ИИ пишет текст для человека.

_signature_enabled: bool = True


def get_signature_enabled() -> bool:
    return _signature_enabled


def set_signature_enabled(enabled: bool) -> None:
    global _signature_enabled
    _signature_enabled = enabled


def format_ai_signature(model: str) -> str:
    """Строка-подпись модели в конце разбора (гейт/humanizer). Пусто, если подпись выключена."""
    if not _signature_enabled:
        return ""
    from datetime import datetime, timezone
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"\n\n---\n*Модель: {model} · {now_str}*\n"
