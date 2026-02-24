"""Node adapters: call upstream nodes and convert responses to NodeSignal.

Each adapter handles:
  1. HTTP call with rate limiting + retry
  2. Circuit breaker check
  3. Response parsing into NodeSignal
  4. Graceful degradation on failure
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

import httpx

from apex_common.circuit_breaker import CircuitBreakerRegistry
from apex_common.confluence import NodeSignal
from apex_common.rate_limit import AsyncRateLimiter
from apex_common.retry import retry_with_backoff


async def _get_json(
    http: httpx.AsyncClient,
    url: str,
    *,
    limiter: AsyncRateLimiter,
    timeout: float,
    attempts: int,
) -> dict:
    async def _do():
        await limiter.acquire()
        r = await http.get(url, timeout=timeout)
        r.raise_for_status()
        return r.json()
    return await retry_with_backoff(_do, attempts=attempts)


async def _post_json(
    http: httpx.AsyncClient,
    url: str,
    payload: dict,
    *,
    limiter: AsyncRateLimiter,
    timeout: float,
    attempts: int,
) -> dict:
    async def _do():
        await limiter.acquire()
        r = await http.post(url, json=payload, timeout=timeout)
        r.raise_for_status()
        return r.json()
    return await retry_with_backoff(_do, attempts=attempts)


async def _safe_call(
    cb: CircuitBreakerRegistry,
    node_name: str,
    coro,
) -> tuple[bool, Any]:
    """Wraps a call with circuit breaker. Returns (success, result_or_none)."""
    if not await cb.is_available(node_name):
        return False, None
    try:
        result = await coro
        await cb.record_success(node_name)
        return True, result
    except Exception:
        await cb.record_failure(node_name)
        return False, None


# ────────────────────────────────────────────────────
# V2 LEGACY ADAPTERS
# ────────────────────────────────────────────────────

async def call_brain(
    http: httpx.AsyncClient,
    brain_url: str,
    payload: dict,
    *,
    limiter: AsyncRateLimiter,
    cb: CircuitBreakerRegistry,
    timeout: float,
    attempts: int,
) -> NodeSignal:
    """Call v2 Brain Engine /process_tick and convert to NodeSignal."""
    ok, data = await _safe_call(
        cb, "brain",
        _post_json(http, f"{brain_url}/process_tick", payload, limiter=limiter, timeout=timeout, attempts=attempts),
    )
    if not ok or data is None:
        return NodeSignal(node="brain", available=False)

    return NodeSignal(
        node="brain",
        action=str(data.get("action", "WAIT")).upper(),
        side=str(data.get("side", "NONE")).upper(),
        confidence=float(data.get("confidence", 0.0)),
        available=True,
        metadata={
            "risk_multiplier": float(data.get("risk_multiplier", 1.0)),
            "reasoning_log": data.get("reasoning_log", []),
        },
    )


async def call_shadowglass(
    http: httpx.AsyncClient,
    shadow_url: str,
    symbol: str,
    *,
    limiter: AsyncRateLimiter,
    cb: CircuitBreakerRegistry,
    timeout: float,
    attempts: int,
) -> tuple[NodeSignal, dict]:
    """Call v2 Shadowglass /get_market_state and convert to NodeSignal.

    Returns (signal, raw_data) — raw_data is needed to build Brain payload.
    """
    ok, data = await _safe_call(
        cb, "shadowglass",
        _get_json(http, f"{shadow_url}/get_market_state/{symbol}", limiter=limiter, timeout=timeout, attempts=attempts),
    )
    if not ok or data is None:
        return NodeSignal(node="shadowglass", available=False), {}

    metrics = data.get("metrics", {}) or {}
    imbalance = float(metrics.get("orderbook_imbalance", 0.0) or 0.0)

    # Shadowglass produces a weak directional signal from imbalance
    if abs(imbalance) > 0.15:
        side = "LONG" if imbalance > 0 else "SHORT"
        action = "EXECUTE"
        conf = min(1.0, abs(imbalance))
    else:
        side = "NONE"
        action = "WAIT"
        conf = 0.0

    signal = NodeSignal(
        node="shadowglass",
        action=action,
        side=side,
        confidence=conf,
        available=True,
        metadata={
            "micro_price_shift": float(metrics.get("micro_price_shift", 0.0) or 0.0),
            "orderbook_imbalance": imbalance,
            "long_short_ratio": float(data.get("long_short_ratio", 1.0) or 1.0),
            "is_crowded_long": data.get("is_crowded_long", False),
            "is_crowded_short": data.get("is_crowded_short", False),
        },
    )
    return signal, data


async def call_antirug(
    http: httpx.AsyncClient,
    antirug_url: str,
    token_metrics: dict,
    *,
    limiter: AsyncRateLimiter,
    cb: CircuitBreakerRegistry,
    timeout: float,
    attempts: int,
) -> NodeSignal:
    """Call Anti-Rug /analyze_token. Returns KILL if rug probability high."""
    ok, data = await _safe_call(
        cb, "antirug_v3",
        _post_json(http, f"{antirug_url}/analyze_token", token_metrics, limiter=limiter, timeout=timeout, attempts=attempts),
    )
    if not ok or data is None:
        return NodeSignal(node="antirug_v3", available=False)

    rug_prob = float(data.get("rug_probability_pct", 0.0)) / 100.0
    status = str(data.get("status", "")).upper()

    return NodeSignal(
        node="antirug_v3",
        action="KILL" if status == "REJEITADO" else "WAIT",
        side="NONE",
        confidence=1.0 - rug_prob,
        available=True,
        metadata={"rug_probability": rug_prob, "raw_status": status},
    )


# ────────────────────────────────────────────────────
# V3 NODE ADAPTERS (stubs for future nodes)
# ────────────────────────────────────────────────────

async def call_spoofhunter(
    http: httpx.AsyncClient,
    url: str,
    symbol: str,
    *,
    limiter: AsyncRateLimiter,
    cb: CircuitBreakerRegistry,
    timeout: float,
    attempts: int,
) -> NodeSignal:
    """Call SpoofHunter L2 Telemetry. Returns NodeSignal."""
    ok, data = await _safe_call(
        cb, "spoofhunter",
        _get_json(http, f"{url}/spoof_state/{symbol}", limiter=limiter, timeout=timeout, attempts=attempts),
    )
    if not ok or data is None:
        return NodeSignal(node="spoofhunter", available=False)

    return NodeSignal(
        node="spoofhunter",
        action=str(data.get("action", "WAIT")).upper(),
        side=str(data.get("side", "NONE")).upper(),
        confidence=float(data.get("confidence", 0.0)),
        available=True,
        metadata=data,
    )


async def call_newtonian(
    http: httpx.AsyncClient,
    url: str,
    symbol: str,
    *,
    limiter: AsyncRateLimiter,
    cb: CircuitBreakerRegistry,
    timeout: float,
    attempts: int,
) -> NodeSignal:
    """Call Newtonian Gravitational Model. Returns NodeSignal."""
    ok, data = await _safe_call(
        cb, "newtonian",
        _get_json(http, f"{url}/gravity_state/{symbol}", limiter=limiter, timeout=timeout, attempts=attempts),
    )
    if not ok or data is None:
        return NodeSignal(node="newtonian", available=False)

    return NodeSignal(
        node="newtonian",
        action=str(data.get("action", "WAIT")).upper(),
        side=str(data.get("side", "NONE")).upper(),
        confidence=float(data.get("confidence", 0.0)),
        available=True,
        metadata=data,
    )


async def call_narrative(
    http: httpx.AsyncClient,
    url: str,
    symbol: str,
    *,
    limiter: AsyncRateLimiter,
    cb: CircuitBreakerRegistry,
    timeout: float,
    attempts: int,
) -> NodeSignal:
    """Call Narrative Divergence & Hyblock node. Returns NodeSignal."""
    ok, data = await _safe_call(
        cb, "narrative",
        _get_json(http, f"{url}/sentiment_state/{symbol}", limiter=limiter, timeout=timeout, attempts=attempts),
    )
    if not ok or data is None:
        return NodeSignal(node="narrative", available=False)

    return NodeSignal(
        node="narrative",
        action=str(data.get("action", "WAIT")).upper(),
        side=str(data.get("side", "NONE")).upper(),
        confidence=float(data.get("confidence", 0.0)),
        available=True,
        metadata=data,
    )


async def call_dreamer(
    http: httpx.AsyncClient,
    url: str,
    symbol: str,
    *,
    limiter: AsyncRateLimiter,
    cb: CircuitBreakerRegistry,
    timeout: float,
    attempts: int,
) -> NodeSignal:
    """Call DreamerV3 Latent Imagination node. Returns NodeSignal."""
    ok, data = await _safe_call(
        cb, "dreamer",
        _get_json(http, f"{url}/imagination_signal/{symbol}", limiter=limiter, timeout=timeout, attempts=attempts),
    )
    if not ok or data is None:
        return NodeSignal(node="dreamer", available=False)

    return NodeSignal(
        node="dreamer",
        action=str(data.get("action", "WAIT")).upper(),
        side=str(data.get("side", "NONE")).upper(),
        confidence=float(data.get("confidence", 0.0)),
        available=True,
        metadata={
            "risk_multiplier": float(data.get("risk_multiplier", 1.0)),
            "mean_expected_reward": data.get("mean_expected_reward"),
            "tail_risk_pct": data.get("tail_risk_pct"),
        },
    )


async def fetch_premium_index(
    http: httpx.AsyncClient,
    binance_fapi: str,
    symbol: str,
    timeout: float,
) -> dict:
    """Fetch funding rate from Binance premiumIndex (best-effort)."""
    try:
        r = await http.get(
            f"{binance_fapi}/fapi/v1/premiumIndex",
            params={"symbol": symbol.upper()},
            timeout=timeout,
        )
        r.raise_for_status()
        data = r.json()
        return {
            "markPrice": float(data.get("markPrice") or 0.0),
            "lastFundingRate": float(data.get("lastFundingRate") or 0.0),
        }
    except Exception:
        return {"markPrice": 0.0, "lastFundingRate": 0.0}
