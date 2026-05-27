"""Кольцевой буфер логов в памяти — хранит последние N записей."""
from collections import deque
import logging

_MAX = 500
_buffer: deque[dict] = deque(maxlen=_MAX)


class BufferHandler(logging.Handler):
    """Logging handler, складывающий записи в кольцевой буфер."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            _buffer.append({
                "ts":     record.created,
                "level":  record.levelname,
                "logger": record.name,
                "msg":    self.format(record),
            })
        except Exception:
            pass


def get_entries(n: int = 200) -> list[dict]:
    buf = list(_buffer)
    return buf[-n:] if len(buf) > n else buf


def clear_buffer() -> None:
    _buffer.clear()
