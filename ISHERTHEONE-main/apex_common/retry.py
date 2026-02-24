from __future__ import annotations

import asyncio
import random
from typing import Awaitable, Callable, TypeVar

T = TypeVar("T")

def _jitter(base: float, spread: float = 0.25) -> float:
    return max(0.0, base * (1.0 + random.uniform(-spread, spread)))

async def retry_with_backoff(
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int = 4,
    base_delay: float = 0.25,
    max_delay: float = 5.0,
    retry_on: tuple[int, ...] = (429, 500, 502, 503, 504),
) -> T:
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            return await fn()
        except Exception as e:
            last_exc = e
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status is not None and int(status) not in retry_on:
                raise
            if i == attempts - 1:
                raise
            delay = min(max_delay, base_delay * (2 ** i))
            await asyncio.sleep(_jitter(delay))
    raise last_exc or RuntimeError("retry_with_backoff failed")
