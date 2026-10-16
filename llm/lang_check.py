# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Чужой язык в текстах модели для оператора (v5.1.22, решение владельца В4).

16.10 заключение #35931 вышло с «冷却» и «remains active», пересказ #35624 — с
«风险信息…» и протёкшим рассуждением «Final answer should be clean». По
калибровке за 30 суток (317 заключений): иероглифы — 1 заключение и 1 пересказ,
английские фразы — около 4.

Решение В4 (вместо повторов и Claude API): один быстрый проход «очистка /
перевод» коротким запросом — на вход только текст ответа, быстрая модель шлюза
(настройка ai_lang_cleanup_model, по умолчанию «fast»), задача — перевести
вставки на русский, не меняя смысла, разметки и чисел. Результат принимается,
если чужого языка в нём нет и сохранены все числа и заголовки исходника; иначе
остаётся исходник с вычищенными иероглифами. Отметка — debug_json.lang_check
{found, cleaned, accepted}. Повторов нет: один запрос, и только если чужой язык
найден. Применяется ко всем текстам модели, которые видит оператор:
заключение, пересказ, разборы гейта, ответ совета.

Что считается чужим языком:
- иероглифы, кана, хангыль — всегда;
- английская фраза — два и больше латинских слова подряд, из них не меньше двух
  со строчной буквы, вне `кода` и ссылок, которой нет во входе модели. Термины с
  заглавной (Hot Stop, Fault Log, Shutdown with Cooldown), слова через дефис и
  подчёркивание (fail-open, is_fall_from_work), цитаты из входа («fault
  simulation») — не чужой язык. У совета строки со ссылкой [Wn] — цитаты
  руководств, законны (allow_ref_quotes).
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Иероглифы CJK, кана, хангыль, их пунктуация и полноширинные формы
_CJK_RE = re.compile(
    "[ᄀ-ᇿ⺀-⿟　-〿぀-ヿ㄀-ㄯ㄰-㆏"
    "㆐-ㇿ㐀-䶿一-鿿ꥠ-꥿가-힯豈-﫿"
    "︰-﹏＀-￯]+")
_CODE_RE = re.compile(r"```.*?```|`[^`\n]*`", re.S)
_URL_RE = re.compile(r"https?://\S+")
# Последовательность латинских слов (≥ 2 букв), не часть идентификатора
_PHRASE_RE = re.compile(
    r"(?<![A-Za-z0-9_\-/.@])[A-Za-z][A-Za-z'’]+(?:[ \t]+[A-Za-z][A-Za-z'’]+)+(?![A-Za-z0-9_\-/@])")
_NUM_RE = re.compile(r"\d+(?:[.,]\d+)?")
_HEAD_RE = re.compile(r"^\s{0,3}#{1,6}\s+\S")
# Ссылка совета на руководство: «[W10]», «[W10, по памяти, не проверено]»,
# «[W1, W2]» (v5.1.22, ревью incident-3: точное «[Wn]» пропускало только первую
# форму — цитата #35855 со ссылкой с пояснением считалась чужим языком)
_REF_RE = re.compile(r"\[W\d+[^\]\n]*\]")
# Разметка, которую очистка менять не вправе (ревью incident-8): пункты списков
# и строки таблиц; внешняя обёртка ```…``` всего ответа
_LIST_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\S")
_TABLE_RE = re.compile(r"^\s*\|")
_FENCE_WRAP_RE = re.compile(r"\A\s*```[^\n`]*\n(.*?)\n?[ \t]*```\s*\Z", re.S)

# Модель и рассуждение прохода очистки (настройки ai_lang_cleanup_model /
# ai_lang_cleanup_reasoning, main.py); у шлюза — его модель, у прямого сервера —
# модель подключения
_cfg: dict[str, str] = {"model": "fast", "reasoning": "off"}
# Ожидание ответа очистки: дольше — исходник с вычищенными иероглифами
CLEANUP_TIMEOUT_SEC = 240.0
# В отметке — не больше стольких фрагментов
_MAX_FOUND = 10

CLEANUP_PROMPT = (
    "Ты редактор русского технического текста. В тексте встречаются вставки не на "
    "русском языке: иероглифы или английские фразы. Переведи только эти вставки на "
    "русский язык. Остальное не меняй: смысл, разметку Markdown (заголовки, списки, "
    "таблицы), все числа, единицы, коды, время и названия сигналов оставь как есть. "
    "Термины с заглавной буквы (названия режимов и сигналов панели, например Hot Stop, "
    "Fault Log) и цитаты в кавычках не переводи. Ничего не добавляй и не сокращай. "
    "Верни только исправленный текст целиком, без пояснений.")


def configure(model: str | None = None, reasoning: str | None = None) -> None:
    """Настройки прохода очистки из app_settings (main.py, при старте)."""
    if model is not None and str(model).strip():
        _cfg["model"] = str(model).strip()
    if reasoning is not None and str(reasoning).strip():
        _cfg["reasoning"] = str(reasoning).strip()


def get_config() -> dict[str, str]:
    return dict(_cfg)


def strip_cjk(text: str) -> str:
    """Вычистить иероглифы, кану, хангыль — только сами вставки.

    v5.1.22, ревью incident-1: пробелы схлопывались по всему тексту — вложенные
    пункты «   - …» становились соседними, выравнивание в ```-блоках и жёсткий
    перенос «  \\n» пропадали, даже когда иероглифов не было вовсе (найдены одни
    английские фразы). Теперь пробелы правятся только в месте выреза: отступ
    строки и пробелы у её конца сохраняются; иероглифов нет — текст как есть.
    """
    if not text or not _CJK_RE.search(text):
        return text

    def _cut(m: re.Match) -> str:
        s, a, b = m.string, m.start(), m.end()
        pre, post = m.group(1), m.group(2)
        if a == 0 or s[a - 1] == "\n":
            return pre                  # вставка в начале строки — отступ строки цел
        if b == len(s) or s[b] == "\n":
            return post                 # у конца строки — её хвост («  \n») цел
        if pre or post:
            return " "
        # «режим冷却после» — слова не склеиваем
        return " " if s[a - 1].isalnum() and s[b].isalnum() else ""

    return re.sub(r"([ \t]*)" + _CJK_RE.pattern + r"([ \t]*)", _cut, text)


def _visible(text: str, allow_ref_quotes: bool) -> str:
    """Текст без кода и ссылок; у совета — без строк-цитат со ссылкой [Wn]."""
    t = _URL_RE.sub(" ", _CODE_RE.sub(" ", text or ""))
    if allow_ref_quotes:
        t = "\n".join(ln for ln in t.splitlines() if not _REF_RE.search(ln))
    return t


def _in_source(phrase: str, src_low: str) -> bool:
    p = " ".join(phrase.split()).lower()
    if p in src_low:
        return True
    # Цитата из входа, к которой прилипли заглавные слова: «Hot Stop remains» —
    # нет, «fault simulation» внутри «Fault Simulation Mode» — да
    words = phrase.split()
    while words and words[0][:1].isupper():
        words = words[1:]
    while words and words[-1][:1].isupper():
        words = words[:-1]
    return len(words) >= 2 and " ".join(words).lower() in src_low


def foreign_fragments(text: str, source: str = "", allow_ref_quotes: bool = False) -> list[str]:
    """Фрагменты чужого языка в тексте модели: иероглифы и английские фразы,
    которых нет во входе (source). Пусто — текст русский."""
    if not text:
        return []
    out: list[str] = []
    for m in _CJK_RE.finditer(text):
        frag = m.group(0).strip()
        if frag and frag not in out:
            out.append(frag)
    vis = _visible(text, allow_ref_quotes)
    src_low = " ".join((source or "").split()).lower()
    for m in _PHRASE_RE.finditer(vis):
        phrase = " ".join(m.group(0).split())
        words = phrase.split()
        if sum(1 for w in words if w[:1].islower()) < 2:
            continue      # термины с заглавной: Hot Stop, Shutdown with Cooldown
        if _in_source(phrase, src_low):
            continue
        if phrase not in out:
            out.append(phrase)
    return out


def _numbers(text: str) -> set[str]:
    return {n.replace(",", ".") for n in _NUM_RE.findall(text or "")}


def _headers(text: str) -> list[str]:
    return [ln.strip() for ln in (text or "").splitlines() if _HEAD_RE.match(ln)]


def _markup(text: str) -> tuple[int, int, int, int]:
    """Остов разметки: (ограды ```, заголовки, пункты списков, строки таблиц)."""
    lines = (text or "").splitlines()
    return (sum(ln.count("```") for ln in lines),
            sum(1 for ln in lines if _HEAD_RE.match(ln)),
            sum(1 for ln in lines if _LIST_RE.match(ln)),
            sum(1 for ln in lines if _TABLE_RE.match(ln)))


def unwrap_fence(original: str, cleaned: str) -> str:
    """Снять одну внешнюю обёртку ```…``` (```markdown) с ответа очистки, если у
    исходника её нет: быстрые модели на «верни исправленный текст» часто
    оборачивают его целиком (v5.1.22, ревью incident-8)."""
    if not cleaned or (original or "").lstrip().startswith("```"):
        return cleaned
    m = _FENCE_WRAP_RE.match(cleaned)
    return m.group(1) if m else cleaned


def accept_cleaned(original: str, cleaned: str, source: str = "",
                   allow_ref_quotes: bool = False) -> tuple[bool, str]:
    """Принять ли результат очистки: чужого языка нет, все числа и заголовки
    исходника на месте, разметка та же, текст не обрезан. (да/нет, причина отказа).

    v5.1.22, ревью incident-8: остов разметки сверяется построчно — ответ в
    обёртке ```…``` (внутри неё чужой язык не ищется, весь текст лёг бы в БД
    блоком кода) и ответ со снятыми маркерами списков или заголовков не
    принимаются."""
    if not cleaned or not cleaned.strip():
        return False, "пустой ответ"
    if len(cleaned) < 0.6 * len(original):
        return False, f"ответ короче исходника ({len(cleaned)} из {len(original)} симв.)"
    # Та же проверка, что у исходника: английская фраза, оставленная как есть, —
    # по-прежнему чужой язык
    left = foreign_fragments(cleaned, source, allow_ref_quotes)
    if left:
        return False, "чужой язык остался: " + "; ".join(left[:3])
    lost = sorted(_numbers(original) - _numbers(cleaned))
    if lost:
        return False, "потеряны числа: " + ", ".join(lost[:5])
    have = set(_headers(cleaned))
    bad = [h for h in _headers(original)
           if h not in have and not foreign_fragments(h, source, allow_ref_quotes)]
    if bad:
        return False, "потерян заголовок: " + bad[0]
    mo, mc = _markup(original), _markup(cleaned)
    if mo != mc:
        what = ("блоки кода ```", "заголовки", "пункты списков", "строки таблиц")
        diff = [f"{w} {a}→{b}" for w, a, b in zip(what, mo, mc) if a != b]
        return False, "изменена разметка: " + ", ".join(diff)
    return True, ""


async def _ask_cleanup(text: str, priority: str | None, meta: dict) -> str:
    """Один короткий запрос очистки через общее подключение (llm.client.chat)."""
    from llm.client import chat, get_llm_settings
    gateway = (get_llm_settings() or {}).get("provider") == "gorynych"
    return await chat(CLEANUP_PROMPT, text,
                      model=_cfg["model"] if gateway else None,
                      reasoning=_cfg.get("reasoning") or None,
                      priority=priority, meta=meta)


async def clean_text(text: str, source: str = "", *, priority: str | None = None,
                     allow_ref_quotes: bool = False) -> tuple[str, dict[str, Any] | None]:
    """(текст для оператора, отметка lang_check или None — чужого языка нет).

    Чужой язык найден — один запрос очистки к быстрой модели; принят
    (accept_cleaned) — его текст, иначе исходник с вычищенными иероглифами.
    Отметка: {found, cleaned (ответ очистки получен), accepted, [reason|error],
    model, stripped}. Сбой очистки не роняет вызывающего — исходник остаётся.
    """
    frags = foreign_fragments(text, source, allow_ref_quotes)
    if not frags:
        return text, None
    info: dict[str, Any] = {"found": frags[:_MAX_FOUND], "cleaned": False, "accepted": False}
    meta: dict = {}
    try:
        out = (await asyncio.wait_for(_ask_cleanup(text, priority, meta),
                                      CLEANUP_TIMEOUT_SEC) or "").strip()
        out = unwrap_fence(text, out)        # v5.1.22, ревью incident-8
        info["cleaned"] = bool(out)
        try:
            from llm.client import gateway_label
            info["model"] = gateway_label(meta, _cfg["model"])
        except Exception:
            info["model"] = _cfg["model"]
        ok, why = accept_cleaned(text, out, source, allow_ref_quotes)
        if ok:
            info["accepted"] = True
            logger.info("lang_check: чужой язык %s переведён (%s)", frags[:3], info.get("model"))
            return out, info
        info["reason"] = why
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        info["error"] = repr(exc)[:200]
    fallback = strip_cjk(text)
    # Отметка — по наличию иероглифов в исходнике (ревью incident-1: при одних
    # английских фразах «вычищено» не врёт)
    info["stripped"] = bool(_CJK_RE.search(text))
    logger.warning("lang_check: очистка не принята (%s) — исходник%s; найдено: %s",
                   info.get("reason") or info.get("error") or "?",
                   " с вычищенными иероглифами" if info["stripped"] else "", frags[:3])
    return fallback, info
