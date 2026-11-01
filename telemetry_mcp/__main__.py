# Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
# Программный комплекс «Честная Генерация»
# Модуль детерминированной аналитики и LLM-аннотации
# Автор: Саввиди Александр Анатольевич | ИНН 4725009270
#
# Данное программное обеспечение является конфиденциальным.
# Несанкционированное копирование, распространение или использование
# без письменного разрешения правообладателя запрещено.

"""Точка входа: `python -m telemetry_mcp` — сервер телеметрии (юнит cg-telemetry-mcp)."""
from __future__ import annotations

import logging

import uvicorn

from .config import load


def main() -> None:
    st = load()
    logging.basicConfig(level=getattr(logging, st.log_level, logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from .server import build_app
    app = build_app(st)
    uvicorn.run(app, host=st.host, port=st.port, log_level=st.log_level.lower(),
                timeout_graceful_shutdown=10)


if __name__ == "__main__":
    main()
