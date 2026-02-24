from __future__ import annotations

import asyncio
import time

class AsyncRateLimiter:
    """Token-bucket style async rate limiter.

    rate_per_sec: tokens added per second
    burst: maximum bucket size
    """
    def __init__(self, rate_per_sec: float, burst: float | None = None):
        self.rate = max(0.1, float(rate_per_sec))
        self.capacity = float(burst if burst is not None else max(1.0, self.rate))
        self.tokens = self.capacity
        self.updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0):
        tokens = float(tokens)
        while True:
            async with self._lock:
                now = time.monotonic()
                elapsed = now - self.updated
                self.updated = now
                self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)

                if self.tokens >= tokens:
                    self.tokens -= tokens
                    return

                needed = tokens - self.tokens
                wait_s = needed / self.rate

            await asyncio.sleep(min(2.0, max(0.0, wait_s)))
