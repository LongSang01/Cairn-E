from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from cairn.logcontext import correlation_id


class DispatcherLogFormatter(logging.Formatter):
    _PREFIX = "cairn.dispatcher."

    def format(self, record: logging.LogRecord) -> str:
        record.shortname = record.name.removeprefix(self._PREFIX)
        return super().format(record)


class JsonFormatter(logging.Formatter):
    """Render each record as one JSON object per line.

    Attributes passed via `logger.info(..., extra={...})` become top-level fields,
    and the request's correlation id is attached automatically, so every line
    emitted while handling one request shares a searchable identifier.
    """

    _RESERVED = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {
        "message",
        "asctime",
        "shortname",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).strftime("%Y-%m-%dT%H:%M:%S")
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        request_id = correlation_id.get()
        if request_id:
            payload["request_id"] = request_id
        payload.update({key: value for key, value in record.__dict__.items() if key not in self._RESERVED})
        if record.exc_info and record.exc_info[0] is not None:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO", *, bare: bool = False, json_format: bool = False) -> None:
    handler = logging.StreamHandler()
    if bare:
        handler.setFormatter(logging.Formatter(fmt="%(message)s"))
    elif json_format:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            DispatcherLogFormatter(
                fmt="[%(asctime)s] %(levelname)s %(shortname)s %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO), handlers=[handler], force=True)
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
