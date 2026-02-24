from __future__ import annotations

import os
import random

def _env(name: str, default: str) -> str:
    return os.getenv(name, default)

BASE_DELAY = float(_env("MAESTRO_RETRY_BASE_DELAY_S", "2"))
MAX_DELAY = float(_env("MAESTRO_RETRY_MAX_DELAY_S", "120"))
JITTER_PCT = float(_env("MAESTRO_RETRY_JITTER_PCT", "0.25"))

def compute_delay(attempt_n: int) -> float:
    """Exponential backoff: base*2^(attempt-1), capped, with jitter."""
    a = max(1, int(attempt_n))
    delay = min(MAX_DELAY, BASE_DELAY * (2 ** (a - 1)))
    jitter = 1.0 + random.uniform(-JITTER_PCT, JITTER_PCT)
    return max(0.0, delay * jitter)
