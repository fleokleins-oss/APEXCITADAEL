"""Async circuit breaker for upstream node health tracking.

States:
  CLOSED   — normal operation, requests pass through
  OPEN     — node is disabled after N consecutive failures; all requests short-circuit
  HALF_OPEN — cooldown expired, one probe request is allowed to test recovery

Transitions:
  CLOSED → OPEN:      consecutive failures >= threshold
  OPEN → HALF_OPEN:   cooldown_s elapsed
  HALF_OPEN → CLOSED: probe succeeds
  HALF_OPEN → OPEN:   probe fails (reset cooldown)
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict


class CBState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


@dataclass
class _NodeBreaker:
    """Per-node circuit breaker state."""
    state: CBState = CBState.CLOSED
    consecutive_failures: int = 0
    last_failure_ts: float = 0.0
    opened_at: float = 0.0
    total_failures: int = 0
    total_successes: int = 0


class CircuitBreakerRegistry:
    """Manages circuit breakers for multiple upstream nodes."""

    def __init__(
        self,
        failure_threshold: int = 5,
        cooldown_s: float = 60.0,
        probe_interval_s: float = 15.0,
    ):
        self.failure_threshold = failure_threshold
        self.cooldown_s = cooldown_s
        self.probe_interval_s = probe_interval_s
        self._breakers: Dict[str, _NodeBreaker] = {}
        self._lock = asyncio.Lock()

    def _get(self, node: str) -> _NodeBreaker:
        if node not in self._breakers:
            self._breakers[node] = _NodeBreaker()
        return self._breakers[node]

    async def is_available(self, node: str) -> bool:
        """Check if a node is available for requests.

        Returns True if CLOSED or if HALF_OPEN (probe allowed).
        Returns False if OPEN and cooldown has not elapsed.
        """
        async with self._lock:
            cb = self._get(node)

            if cb.state == CBState.CLOSED:
                return True

            if cb.state == CBState.OPEN:
                elapsed = time.monotonic() - cb.opened_at
                if elapsed >= self.cooldown_s:
                    cb.state = CBState.HALF_OPEN
                    return True
                return False

            # HALF_OPEN: allow probe
            return True

    async def record_success(self, node: str):
        """Record a successful call. Resets breaker to CLOSED."""
        async with self._lock:
            cb = self._get(node)
            cb.consecutive_failures = 0
            cb.total_successes += 1
            if cb.state in (CBState.HALF_OPEN, CBState.OPEN):
                cb.state = CBState.CLOSED

    async def record_failure(self, node: str):
        """Record a failed call. May trip breaker to OPEN."""
        async with self._lock:
            cb = self._get(node)
            cb.consecutive_failures += 1
            cb.total_failures += 1
            cb.last_failure_ts = time.monotonic()

            if cb.state == CBState.HALF_OPEN:
                # Probe failed: back to OPEN
                cb.state = CBState.OPEN
                cb.opened_at = time.monotonic()
                return

            if cb.consecutive_failures >= self.failure_threshold:
                cb.state = CBState.OPEN
                cb.opened_at = time.monotonic()

    async def get_status(self, node: str) -> dict:
        """Return human-readable status for a node."""
        async with self._lock:
            cb = self._get(node)
            return {
                "node": node,
                "state": cb.state.value,
                "consecutive_failures": cb.consecutive_failures,
                "total_failures": cb.total_failures,
                "total_successes": cb.total_successes,
            }

    async def get_all_status(self) -> list[dict]:
        """Return status for all tracked nodes."""
        async with self._lock:
            return [
                {
                    "node": name,
                    "state": cb.state.value,
                    "consecutive_failures": cb.consecutive_failures,
                    "total_failures": cb.total_failures,
                    "total_successes": cb.total_successes,
                }
                for name, cb in self._breakers.items()
            ]

    async def force_close(self, node: str):
        """Manually reset a breaker to CLOSED (admin action)."""
        async with self._lock:
            cb = self._get(node)
            cb.state = CBState.CLOSED
            cb.consecutive_failures = 0

    async def force_open(self, node: str):
        """Manually open a breaker (admin action)."""
        async with self._lock:
            cb = self._get(node)
            cb.state = CBState.OPEN
            cb.opened_at = time.monotonic()
