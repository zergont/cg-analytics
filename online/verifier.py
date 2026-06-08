"""Финальная проверка характеристик: инкрементальные vs полный pipeline.

Используется как опция при закрытии сегмента (переключатель в настройках).
Результат пишется в стандартный логгер → виден в /log веб-морды.

Публичный API:
    fire_verify(...)  — fire-and-forget: проверяет флаг в БД, запускает сравнение
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Поля для сравнения (присутствуют в Characteristic.to_dict())
_CMP_FIELDS = ("median", "mad", "slope", "min_value", "max_value", "sample_count")

# Порог Δ% — выше → WARNING в логе
_WARN_PCT = 2.0


# ── Сравнение ─────────────────────────────────────────────────────────────────

def _pct_diff(a: Any, b: Any) -> float | None:
    """Относительное расхождение в % между двумя числами."""
    try:
        fa, fb = float(a), float(b)
    except (TypeError, ValueError):
        return None
    if abs(fa) < 1e-9 and abs(fb) < 1e-9:
        return 0.0
    denom = max(abs(fa), abs(fb))
    if denom < 1e-9:
        return None
    return abs(fa - fb) / denom * 100.0


def _compare_chars(incr: dict, ref: dict) -> tuple[list[str], float, str]:
    """Сравнить две characteristics_json.

    Returns:
        (lines, max_delta_pct, max_delta_label)
    """
    lines: list[str] = []
    max_delta = 0.0
    max_label = ""

    incr_subs: list[dict] = incr.get("subsegments") or []
    ref_subs:  list[dict] = ref.get("subsegments")  or []

    if not incr_subs or not ref_subs:
        lines.append("  нет подсегментов для сравнения")
        return lines, 0.0, ""

    n = min(len(incr_subs), len(ref_subs))
    if len(incr_subs) != len(ref_subs):
        lines.append(
            f"  ⚠ кол-во подсегментов: инкр={len(incr_subs)} ref={len(ref_subs)}"
            f" — сравниваем первые {n}"
        )

    for idx in range(n):
        ichars: dict = (incr_subs[idx].get("characteristics") or {})
        rchars: dict = (ref_subs[idx].get("characteristics")  or {})

        all_params = sorted(set(ichars) | set(rchars))
        for param in all_params:
            ic = ichars.get(param) or {}
            rc = rchars.get(param) or {}
            if not isinstance(ic, dict) or not isinstance(rc, dict):
                continue

            diffs: list[str] = []
            for field in _CMP_FIELDS:
                iv = ic.get(field)
                rv = rc.get(field)
                if iv is None and rv is None:
                    continue
                delta = _pct_diff(iv, rv)
                if delta is None:
                    if iv != rv:
                        diffs.append(f"{field}: {iv!r}→{rv!r}")
                elif delta > 0.05:        # не логируем совсем незначительные
                    diffs.append(f"{field}: {iv}→{rv} (Δ{delta:.1f}%)")
                    if delta > max_delta:
                        max_delta = delta
                        max_label = f"sub[{idx}].{param}.{field}"

            if diffs:
                lines.append(f"  [{param}] " + " | ".join(diffs))

    return lines, max_delta, max_label


def log_comparison(
    seg_id: int | None,
    unit_key: str,
    run_state: int | None,
    t_start_str: str,
    t_end_str: str,
    incr: dict | None,
    ref: dict,
) -> None:
    """Залогировать результат сравнения инкрементальных и reference-характеристик."""
    label = ref.get("run_state_label") or f"RS={run_state}"
    header = (
        f"[VERIFY] seg#{seg_id} [{unit_key}] {label}"
        f" | {t_start_str} → {t_end_str}"
    )

    if incr is None:
        logger.info("%s | инкрементальных данных нет — нечего сравнивать", header)
        return

    # Проверяем совпадение run_state
    if incr.get("run_state") != ref.get("run_state"):
        logger.info(
            "%s | run_state разошёлся: инкр=%s ref=%s (смена режима до закрытия — ок)",
            header,
            incr.get("run_state"),
            ref.get("run_state"),
        )
        return

    diff_lines, max_delta, max_label = _compare_chars(incr, ref)

    logger.info(header)
    for line in diff_lines:
        logger.info(line)

    if not diff_lines:
        logger.info("  ✓ характеристики идентичны (Δ=0)")
    elif max_delta < _WARN_PCT:
        logger.info("  ✓ max Δ=%.2f%% (%s) — ОК", max_delta, max_label)
    else:
        logger.warning("  ⚠ max Δ=%.1f%% (%s) — ПРОВЕРИТЬ", max_delta, max_label)


# ── Fire-and-forget ───────────────────────────────────────────────────────────

def fire_verify(
    seg_id: int | None,
    unit_key: str,
    run_state: int | None,
    t_start_str: str,
    t_end_str: str,
    incr_chars: dict | None,
    ref_chars: dict,
) -> None:
    """Запустить верификацию как fire-and-forget корутину (если флаг включён в БД).

    Не блокирует вызывающий код. Флаг «analytics_verify_on_close» читается асинхронно.
    """
    async def _run():
        try:
            from db.analytics import get_app_setting
            flag = await get_app_setting("analytics_verify_on_close", "false")
            if flag != "true":
                return
            log_comparison(
                seg_id, unit_key, run_state,
                t_start_str, t_end_str,
                incr_chars, ref_chars,
            )
        except Exception:
            logger.debug("[VERIFY] ошибка верификации", exc_info=True)

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(_run())
    except Exception:
        pass
