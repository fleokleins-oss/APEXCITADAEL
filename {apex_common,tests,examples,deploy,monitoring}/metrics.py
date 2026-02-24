from __future__ import annotations

import os
from fastapi import FastAPI

def _bool_env(name: str, default: bool = False) -> bool:
    v = os.getenv(name, str(default)).strip().upper()
    return v in ("1", "TRUE", "YES", "Y", "ON")

def instrument_app(app: FastAPI):
    """Adds /metrics if METRICS_ENABLED=TRUE. No-op otherwise."""
    if not _bool_env("METRICS_ENABLED", False):
        return
    try:
        from prometheus_fastapi_instrumentator import Instrumentator
        Instrumentator().instrument(app).expose(app, endpoint="/metrics")
    except Exception:
        return
