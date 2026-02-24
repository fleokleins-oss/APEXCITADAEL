import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict

def _bool_env(name: str, default: bool = False) -> bool:
    v = os.getenv(name, str(default)).strip().upper()
    return v in ("1", "TRUE", "YES", "Y", "ON")

LOG_JSON = _bool_env("LOG_JSON", False)

class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        for k in ("request_id", "service", "event", "symbol", "venue", "step"):
            if hasattr(record, k):
                payload[k] = getattr(record, k)
        return json.dumps(payload, ensure_ascii=False)

def get_logger(name: str) -> logging.Logger:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(level)
    handler = logging.StreamHandler(sys.stdout)

    if LOG_JSON:
        handler.setFormatter(JsonFormatter())
    else:
        fmt = logging.Formatter(
            fmt="%(asctime)s %(levelname)s %(name)s — %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler.setFormatter(fmt)

    logger.addHandler(handler)
    logger.propagate = False
    return logger
