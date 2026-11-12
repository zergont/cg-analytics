# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Инструмент 13: график (v5.2.5, Э2а; design_mcp_e2.md разд. 9, решения Р17, Р21).

Тот же сбор, что у `series` (gather_series), два представления: картинка PNG —
форма ряда, числовая сводка — числа. Сервер про модель не знает и отдаёт оба
всегда; моделям без зрения инструмент не предлагает хост (Р21).
"""
from __future__ import annotations

import asyncio
from typing import Annotated, Any

from mcp.server.mcpserver import Context, Image
from pydantic import Field

from config import get_tz

from .catalog import get_catalog
from .chart import has_data, panels_from, png_size, render_png
from .common import parse_window, resolve_unit, run_tool, settings
from .envelope import Envelope, fmt_window
from .kb import kb_for, nominal_for, resolve_params
from .tools_series import _multi_day, _split, gather_series, summary_lines


def register(server: Any) -> None:

    @server.tool(name="chart", structured_output=False,
                 description="График параметров установки за окно: картинка PNG (панель на параметр, общая "
                             "ось времени, соседи пунктиром, разрывы связи штриховкой) и та же числовая "
                             "сводка, что у series. Форму смотри на картинке (раскачка, провал, выброс), "
                             "числа бери только из сводки. Аргументы как у series, параметров до 4.")
    async def chart(
        ctx: Context,
        unit: Annotated[str, Field(description="Установка: имя («ДЭС №3») или id из equipment_tree")],
        params: Annotated[str, Field(description="Параметры через запятую (до 4): частота, обороты, мощность, "
                                                 "ток L1, давление масла … или адреса регистров")],
        t_from: Annotated[str | None, Field(description="Начало окна, пояс сервиса (МСК): «16.10.2026 09:20»")] = None,
        t_to: Annotated[str | None, Field(description="Конец окна; пусто — сейчас")] = None,
        at: Annotated[str | None, Field(description="Вместо от/до: момент, вокруг которого окно ± minutes")] = None,
        minutes: Annotated[int | None, Field(description="Полуширина окна вокруг at, минут (по умолчанию 30)")] = None,
        with_neighbors: Annotated[bool, Field(description="Добавить линии соседних установок объекта")] = False,
    ) -> Any:
        st = settings()
        holder: dict[str, bytes] = {}

        async def body(env: Envelope) -> str:
            u = await resolve_unit(unit)
            env.unit, env.unit_id = u.title, u.id
            cfg, _ref, note = await kb_for(u)
            if note:
                env.notes.append(note)
            cat = await get_catalog(u.equip_type)
            prefs = resolve_params(cfg, cat, _split(params), max_params=int(st.limit("chart_max_params")))
            t0, t1 = parse_window(t_from, t_to, at, minutes, default_hours=2,
                                  max_days=float(st.limit("series_max_window_days")))
            g = await gather_series(env, u, cfg, prefs, t0, t1, int(st.limit("chart_max_points")),
                                    with_neighbors, st)
            lines = summary_lines(g)
            nominals = {}
            for p in prefs:
                nom = nominal_for(cfg, p.role)
                if nom:
                    nominals[p.label] = float(nom[0])
            panels = panels_from(g.cols, g.rs.cells, nominals)
            if not has_data(panels):
                env.notes.append("записей в окне нет — график не построен")
                return "\n".join(lines)
            title = f"{u.name} · {fmt_window(t0, t1)}"
            png = await asyncio.to_thread(render_png, panels, g.rs.times, t0, t1, title=title,
                                          gaps=env.gaps, tz=get_tz(), multi_day=_multi_day(t0, t1))
            holder["png"] = png
            env.attachment_bytes = len(png)
            w, h = png_size(png)
            env.notes.append(f"график: PNG {w}×{h}, панелей {len(panels)}; форма — на картинке, "
                             "числа — только из сводки")
            return "\n".join(lines)

        text = await run_tool("chart", ctx, {"unit": unit, "params": params, "t_from": t_from, "t_to": t_to,
                                             "at": at, "minutes": minutes, "with_neighbors": with_neighbors}, body)
        png = holder.get("png")
        if png and not text.startswith("ОШИБКА"):
            return [text, Image(data=png, format="png")]
        return text
