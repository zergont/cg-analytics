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
(ложное/преждевременное); допустимо ТОЛЬКО когда в данных написано «cancel ДОПУСТИМ». \
«Общая авария» с несброшенным кодом, висевшая на начало сегмента, — сведения, \
снятие она не запрещает.
   - decision="pass" — угроза реальна или сомнения остаются; при сигналах панели \
WARNING/SHUTDOWN или «Общей аварии», появившейся в этом сегменте (в данных «отмена \
НЕДОСТУПНА»), — всегда pass.
В reason укажи краткое обоснование решения (1-2 предложения).
Сомневаешься — выбирай pass: пропустить лишнее предупреждение дешевле, \
чем скрыть реальную проблему.\
"""

# Фраза системного промпта гейта о панели (v5.1.12, решение владельца 12.10):
# CAUTION панели («Общая авария» с несброшенным кодом), висевший на начало
# сегмента, — сведения; запрет снятия решает код и пишет его в данных гейта
GATE_PANEL_RULE_OLD = "допустимо ТОЛЬКО когда сигналов панели нет."
GATE_PANEL_RULE_NEW = ("допустимо ТОЛЬКО когда в данных написано «cancel ДОПУСТИМ». "
                       "«Общая авария» с несброшенным кодом, висевшая на начало сегмента, "
                       "— сведения, снятие она не запрещает.")


GATE_PANEL_PASS_OLD = ("при любых сигналах панели управления (внимание/авария) — "
                       "всегда pass.")
GATE_PANEL_PASS_NEW = ("при сигналах панели WARNING/SHUTDOWN или «Общей аварии», "
                       "появившейся в этом сегменте (в данных «отмена НЕДОСТУПНА»), — "
                       "всегда pass.")


def migrate_gate_prompt(text: str) -> str | None:
    """Промпт гейта из настроек с новыми фразами о панели; None — менять нечего
    (фраз нет: владелец переписал промпт сам — не трогаем)."""
    if not text:
        return None
    out = text
    for old, new in ((GATE_PANEL_RULE_OLD, GATE_PANEL_RULE_NEW),
                     (GATE_PANEL_PASS_OLD, GATE_PANEL_PASS_NEW)):
        if old in out:
            out = out.replace(old, new)
    return out if out != text else None


_INCIDENT_PROMPT = """\
Ты — инженер по дизель-генераторным установкам и разбираешь аварийный останов.

Это НЕ проверка ложного срабатывания: останов уже произошёл, его факт и вид \
установлены детерминированным актом. Твоя задача — объяснить, что к нему привело.

ВХОД: акт аварийного останова — окно от последнего штатного останова: что висело \
на машине к моменту падения, события панели по времени, расчётные параметры цикла.

ПРАВИЛА:
- Факты об этой машине — только из входа. Не выдумывай коды, уставки и события, \
которых нет.
- Найденное в документации и в сети (если есть) — ориентир со ссылкой [Wn]: как \
устроены такие устройства, а не данные этой машины. Цифры этой машины — только из входа.
- Отделяй установленное по данным от предположений.
- Не повторяй акт целиком — выдели то, что относится к причине.
- Время пиши по местному времени, как во входе (МСК), а не по UTC.
- Лента отсчётов пишется с шагом около 10 с и по изменению: значение держится до \
следующей записи («·» — новой записи нет), что было между отсчётами — не утверждай.
- Нагрузку, ток и положение автомата перед аварией бери из ленты отсчётов и строки \
«Без изменений»; работа до штатного останова — фон, не условия перед аварией.
- Состояние кода неисправности во входе — снимок на момент останова: сброс мог \
произойти позже. Не утверждай, сброшен ли код сейчас.

ФОРМАТ ОТВЕТА:
## Коротко
2–4 строки: что произошло, наиболее вероятная причина, что проверить первым.
## Условия перед аварией
## Вероятная причина
## Что относится к делу, а что нет
## Чего не хватает для полной картины\
"""

# Правила входа совета v5.1.22 (P1-4, P1-6): сохранённая в настройках копия
# промпта не получит их сама — первое сохранение страницы заморозило бы старый
# текст. Перенос — после строки-якоря, если правил ещё нет (migrate_incident_prompt)
INCIDENT_RULES_ANCHOR = "- Не повторяй акт целиком — выдели то, что относится к причине."
INCIDENT_RULES_V5122 = (
    "- Время пиши по местному времени, как во входе (МСК), а не по UTC.\n"
    "- Лента отсчётов пишется с шагом около 10 с и по изменению: значение держится до "
    "следующей записи («·» — новой записи нет), что было между отсчётами — не утверждай.\n"
    "- Нагрузку, ток и положение автомата перед аварией бери из ленты отсчётов и строки "
    "«Без изменений»; работа до штатного останова — фон, не условия перед аварией.\n"
    # Прогон совета 16.10 (#35941): «код 234 не сброшен — не сбрасывать», а код
    # сбросили через 29 с — разбор заказывается в момент останова, ответ через час
    "- Состояние кода неисправности во входе — снимок на момент останова: сброс мог произойти позже. Не утверждай, сброшен ли код сейчас.")


def migrate_incident_prompt(text: str) -> str | None:
    """Сохранённый промпт совета с правилами v5.1.22; None — менять нечего
    (правила уже есть или якоря нет: владелец переписал промпт сам — не трогаем)."""
    if not text or "Лента отсчётов пишется с шагом" in text or INCIDENT_RULES_ANCHOR not in text:
        return None
    return text.replace(INCIDENT_RULES_ANCHOR,
                        INCIDENT_RULES_ANCHOR + "\n" + INCIDENT_RULES_V5122, 1)

# ── Таблица задач: id → (метка UI, провайдер по умолчанию, промпт по умолчанию) ─

TASKS: dict[str, tuple[str, str, str]] = {
    "seg_auto":      ("Авто-анализ сегмента",    "api", _SEG_PROMPT),
    "seg_manual":    ("Ручной анализ сегмента",   "api", _SEG_PROMPT),
    "human_auto":    ("Авто-хуманизация",         "llm", _HUMANIZER_PROMPT),
    "human_manual":  ("Ручная хуманизация",       "llm", _HUMANIZER_PROMPT),
    "analyze_page":  ("Страница «Ручной анализ»", "llm", _ANALYZE_PAGE_PROMPT),
    "warning_claude": ("Гейт предупреждений (онлайн)", "api", _WARNING_GATE_PROMPT),
    "incident":      ("Разбор аварийного останова", "llm", _INCIDENT_PROMPT),
}

# Когда срабатывает каждая задача — подсказки для страницы настроек
TASK_HINTS: dict[str, str] = {
    "seg_auto":      "Автоматически при закрытии сегмента (если включён Claude-конвейер).",
    "seg_manual":    "Кнопка «Анализ» на странице сегмента.",
    "human_auto":    "Автоматически после авто-анализа: переписывает заключение для оператора.",
    "human_manual":  "Кнопка «Хуманизировать» на странице сегмента.",
    "analyze_page":  "Страница «Ручной анализ»: заключение по MD-пакету телеметрии.",
    "warning_claude": "Онлайн-гейт: предупреждение стабильно 60 с → анализ и вердикт cancel/pass; "
                      "cancel подавляет аналитику до смены состава детекций. Промпт общий для гейта "
                      "аналитики и гейта панели; кто разбирает — в таблице «Кто и как разбирает».",
    "incident":      "Акт аварийного останова готов — разбор причины по циклу от штатного останова. "
                     "Свой промпт (v5.1.11): раньше шёл промпт гейта «фильтр ложных срабатываний».",
}

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


# Кто разбирает (v5.1.11): «llm» — общее подключение «Горыныч» (карточка
# подключения, llm.client._cfg) с моделью/рассуждением/приоритетом строки; «api» —
# Claude API, модель строки или (пусто) из карточки Claude. Одна таблица вместо
# двух: раньше провайдера решала карточка «по уровню», а глубину — градация
ROUTE_PROVIDERS: tuple[str, ...] = ("llm", "api", "system")
# «Система (без ИИ)» (v5.1.22, P1-5, решение В1): заключение аварийного стопа —
# фактами из акта и сводки (corpus/worker._shutdown_conclusion). Только у строки
# seg_shutdown: гейт (manager), совет (incident_gate) и пересказ устроены как
# «llm, иначе api» — «system» в их строке ушёл бы в платный Claude API
SYSTEM_PROVIDER = "system"
SYSTEM_ROUTE_KINDS: frozenset[str] = frozenset({"seg_shutdown"})


def _default_depth(kind: str) -> dict[str, str]:
    return {"model": "fast", "reasoning": "default",
            "priority": _DEPTH_DEFAULT_PRIORITY.get(kind, "normal"),
            "provider": "llm", "claude_model": ""}


_depth: dict[str, dict[str, str]] = {k: _default_depth(k) for k in DEPTH_KINDS}


def get_depth(kind: str) -> dict[str, str]:
    """Профиль вида разбора (неизвестный вид — профиль по умолчанию)."""
    return dict(_depth.get(kind) or _default_depth(kind))


def apply_depth(kind: str, model: str, reasoning: str, priority: str, *,
                provider: str | None = None, claude_model: str | None = None) -> None:
    """Строка таблицы. provider/claude_model не переданы — остаются прежними."""
    if kind not in DEPTH_KINDS:
        raise ValueError(f"Неизвестный вид разбора: {kind}")
    cur = _depth.get(kind) or _default_depth(kind)
    prov = cur.get("provider", "llm") if provider is None else str(provider).strip()
    if prov == SYSTEM_PROVIDER and kind not in SYSTEM_ROUTE_KINDS:
        prov = "llm"      # «Система (без ИИ)» — только заключение аварийного стопа
    _depth[kind] = {
        "model":     str(model or "").strip(),
        "reasoning": reasoning if reasoning in DEPTH_REASONING else "default",
        "priority":  priority if priority in DEPTH_PRIORITIES else _DEPTH_DEFAULT_PRIORITY[kind],
        "provider":  prov if prov in ROUTE_PROVIDERS else "llm",
        "claude_model": (cur.get("claude_model", "") if claude_model is None
                         else str(claude_model).strip()),
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
            _depth[kind] = _default_depth(kind)
            apply_depth(kind, prof.get("model", ""), prof.get("reasoning", "default"),
                        prof.get("priority", ""), provider=prof.get("provider", "llm"),
                        claude_model=prof.get("claude_model", ""))
        else:
            _depth[kind] = _default_depth(kind)


def serialize_depth() -> str:
    import json
    return json.dumps(_depth, ensure_ascii=False)


def get_route(kind: str) -> dict[str, str]:
    """Кто и как разбирает вид работы — единственный источник правды (v5.1.11)."""
    return get_depth(kind)


def claude_model_for(kind: str) -> str:
    """Модель Claude строки; пусто — из карточки Claude API."""
    m = (get_depth(kind).get("claude_model") or "").strip()
    if m:
        return m
    try:
        from corpus.settings import get_claude_settings
        return get_claude_settings().get("model") or ""
    except Exception:
        return ""


def route_label(kind: str) -> str:
    """«Горыныч: smart (xhigh)» / «Claude: claude-sonnet-5» — для журнала и подписи.

    Строка «Горыныч» идёт через общее подключение; если оно не шлюз, модель и
    рассуждение строки не действуют (resolve_depth) — подписываем тем, что есть.
    """
    r = get_depth(kind)
    if r.get("provider") == "api":
        return f"Claude: {claude_model_for(kind)}"
    if r.get("provider") == SYSTEM_PROVIDER:
        from analytics.labels import SYSTEM_MODEL
        return SYSTEM_MODEL
    try:
        from llm.client import get_llm_settings
        cfg = get_llm_settings()
    except Exception:
        cfg = {}
    prov = cfg.get("provider") or "gorynych"
    if prov != "gorynych":
        return f"LLM ({prov}): {cfg.get('model') or 'модель подключения'}"
    m = r.get("model") or "модель подключения"
    lvl = r.get("reasoning")
    return f"Горыныч: {m}" + (f" ({lvl})" if lvl and lvl != "default" else "")


def apply_route_form(form) -> None:
    """Таблица «Кто и как разбирает» из формы настроек (/settings/ai-depth).

    Поля нет — текущее значение: вкладка, открытая до деплоя, не сбрасывает ни
    провайдера, ни параметры шлюза. «Ручные запросы» — всегда шлюз.
    """
    def _txt(name: str, cur: str) -> str:
        v = form.get(name)
        return cur if v is None else str(v).strip()

    for kind in DEPTH_KINDS:
        cur = get_depth(kind)
        provider = form.get(f"route_{kind}_provider")
        claude_model = form.get(f"route_{kind}_claude_model")
        apply_depth(
            kind,
            _txt(f"depth_{kind}_model", cur.get("model", "")),
            _txt(f"depth_{kind}_reasoning", cur.get("reasoning", "default")),
            _txt(f"depth_{kind}_priority", cur.get("priority", "")),
            provider="llm" if kind == "manual" else (
                None if provider is None else str(provider).strip()),
            claude_model=None if claude_model is None else str(claude_model).strip(),
        )


def migrate_routes(raw_json: str, legacy: dict[str, str]) -> tuple[str | None, list[str]]:
    """Разовый перенос настроек в таблицу (v5.1.11): провайдер в строки, где его нет.

    legacy — старые ключи app_settings: ai_warning_level_<уровень>_provider/_model
    (карточка «по уровню»), ai_task_<задача>_provider, seg_chain_head_provider
    (провайдер первой записи цепочки заключений: gorynych → llm, anthropic → api).
    Разбор аварии — всегда шлюз (решение владельца 12.10). Битый JSON — None: ключ
    не трогаем, чтобы не потерять градацию. Строки с провайдером — без изменений.
    """
    import json
    try:
        raw = json.loads(raw_json) if raw_json else {}
    except json.JSONDecodeError:
        return None, ["битый JSON градации — перенос пропущен"]
    if not isinstance(raw, dict):
        return None, ["градация не словарь — перенос пропущен"]

    def _p(v: str | None) -> str:
        return "api" if (v or "").strip() in ("api", "anthropic") else "llm"

    seg_head = legacy.get("seg_chain_head_provider") or legacy.get("ai_task_seg_auto_provider")
    plan: dict[str, tuple[str, str]] = {}
    for k in ("seg_norma", "seg_caution", "seg_warning", "seg_shutdown"):
        plan[k] = (_p(seg_head), "")
    for kind, level in (("gate_caution", "предупреждение"), ("gate_warning", "внимание")):
        prov = _p(legacy.get(f"ai_warning_level_{level}_provider"))
        model = legacy.get(f"ai_warning_level_{level}_model") or ""
        plan[kind] = (prov, model if prov == "api" else "")   # путь к gguf шлюзу не доходил
    plan["incident"] = ("llm", "")                            # решение 12.10: шлюз
    plan["humanize"] = (_p(legacy.get("ai_task_human_auto_provider")), "")
    plan["manual"] = ("llm", "")                              # «Ручной анализ» всегда шлюз
    changes: list[str] = []
    for kind in DEPTH_KINDS:
        prof = raw.get(kind)
        if not isinstance(prof, dict):
            prof = dict(_default_depth(kind))
            prof.pop("provider", None)
            prof.pop("claude_model", None)
            raw[kind] = prof
        if prof.get("provider") in ROUTE_PROVIDERS:
            continue
        prov, cm = plan.get(kind, ("llm", ""))
        prof["provider"] = prov
        prof["claude_model"] = prof.get("claude_model") or cm
        changes.append(f"{kind}: {'Горыныч' if prov == 'llm' else 'Claude ' + (cm or '(карточка)')}")
    if not changes:
        return None, []
    return json.dumps(raw, ensure_ascii=False), changes


SHUTDOWN_ROUTE_VERSION = "5.1.22"


def migrate_shutdown_route(raw_json: str) -> tuple[str | None, str]:
    """Разовая установка строки «🔴 авария панели» в «Система (без ИИ)» (v5.1.22,
    P1-5, решение В1; по образцу ai_routes_version). (новый JSON или None, что
    сделано). Битый JSON — None: ключ не ставится, градацию не теряем. Вернуть
    ИИ — в таблице «Кто и как разбирает»: повторно ключ строку не трогает."""
    import json
    try:
        raw = json.loads(raw_json) if raw_json else {}
    except json.JSONDecodeError:
        return None, "битый JSON градации — строка не изменена"
    if not isinstance(raw, dict):
        return None, "градация не словарь — строка не изменена"
    prof = raw.get("seg_shutdown")
    if not isinstance(prof, dict):
        prof = dict(_default_depth("seg_shutdown"))
    if prof.get("provider") == SYSTEM_PROVIDER:
        return None, "уже «Система (без ИИ)»"
    was = prof.get("provider") or "llm"
    prof["provider"] = SYSTEM_PROVIDER
    raw["seg_shutdown"] = prof
    return json.dumps(raw, ensure_ascii=False), f"seg_shutdown: {was} → {SYSTEM_PROVIDER}"


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


def merge_fallbacks(current: list[str], posted: list[str], is_anchor) -> list[str]:
    """«Запасные» из формы (v5.1.11): записи-якоря (шлюз) из текущей цепочки —
    первыми и без изменений, из формы — только прочие, без повторов.

    Якорь держит откат на v5.1.10 (там основного выбирает голова цепочки) и
    запасной шлюз у строк с Claude; в карточке он не показывается, и форма —
    даже из вкладки, открытой до деплоя, — его не снимает и не переставляет.
    """
    anchors = [e for e in current if is_anchor(e)]
    out = list(anchors)
    for e in posted:
        if not is_anchor(e) and e not in out:
            out.append(e)
    return out


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
    """Промпт задачи; пустой (поле очищено в форме) — промпт по умолчанию."""
    return _routing[task_id]["prompt"] or TASKS[task_id][2]


def apply_task(task_id: str, provider: str, prompt: str) -> None:
    if task_id not in TASKS:
        raise ValueError(f"Неизвестная задача AI-роутера: {task_id}")
    _routing[task_id] = {"provider": provider.strip(), "prompt": prompt.strip()}
    logger.debug("ai_router: задача %s → provider=%s", task_id, provider)


def apply_prompts_form(form) -> dict[str, str]:
    """Системные промпты из формы (/settings/ai-routing): {задача: промпт} принятых.

    Поля нет — промпт не трогаем; пустое — промпт по умолчанию (get_prompt).
    Провайдер задачи больше не выбирается: кто разбирает — таблица (v5.1.11).
    """
    out: dict[str, str] = {}
    for task_id in TASKS:
        v = form.get(f"ai_{task_id}_prompt")
        if v is None:
            continue
        prompt = str(v).strip()
        apply_task(task_id, _routing[task_id]["provider"], prompt)
        out[task_id] = prompt
    return out


def get_all() -> dict[str, dict[str, str]]:
    """Полная таблица маршрутизации (копия); пустой промпт — показываем действующий."""
    return {k: {**v, "prompt": v["prompt"] or TASKS[k][2]} for k, v in _routing.items()}


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
