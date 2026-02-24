"""Apex v2 legacy pipeline — Shadowglass → Brain → Executioner.

Retained for backward compatibility. The v3 orchestrator exposes this
as /orchestrate_v2 while the new confluence pipeline is /orchestrate.
"""

from __future__ import annotations

from typing import Literal

import httpx

from apex_common.rate_limit import AsyncRateLimiter
from apex_common.retry import retry_with_backoff

Side = Literal["buy", "sell"]


def clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def map_side(brain_side: str) -> Side:
    s = (brain_side or "").upper()
    if s == "LONG":
        return "buy"
    if s == "SHORT":
        return "sell"
    raise ValueError(f"Unsupported brain side: {brain_side}")


async def _get_json(http: httpx.AsyncClient, url: str, *, limiter: AsyncRateLimiter, timeout: float, attempts: int) -> dict:
    async def _do():
        await limiter.acquire()
        r = await http.get(url, timeout=timeout)
        r.raise_for_status()
        return r.json()
    return await retry_with_backoff(_do, attempts=attempts)


async def _post_json(http: httpx.AsyncClient, url: str, payload: dict, *, limiter: AsyncRateLimiter, timeout: float, attempts: int) -> dict:
    async def _do():
        await limiter.acquire()
        r = await http.post(url, json=payload, timeout=timeout)
        r.raise_for_status()
        return r.json()
    return await retry_with_backoff(_do, attempts=attempts)


async def fetch_premium_index(http: httpx.AsyncClient, binance_fapi: str, symbol: str, timeout: float) -> dict:
    try:
        r = await http.get(f"{binance_fapi}/fapi/v1/premiumIndex", params={"symbol": symbol.upper()}, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        return {
            "markPrice": float(data.get("markPrice") or 0.0),
            "lastFundingRate": float(data.get("lastFundingRate") or 0.0),
        }
    except Exception:
        return {"markPrice": 0.0, "lastFundingRate": 0.0}


async def run_pipeline(
    *,
    http: httpx.AsyncClient,
    req: dict,
    brain_url: str,
    shadow_url: str,
    exec_url: str,
    binance_fapi: str,
    timeout_s: float,
    attempts: int,
    lim_shadow: AsyncRateLimiter,
    lim_brain: AsyncRateLimiter,
    lim_exec: AsyncRateLimiter,
) -> dict:
    notes: list[str] = []

    symbol = (req.get("symbol") or "").strip()
    if not symbol:
        raise ValueError("symbol vazio")

    shadow_symbol = req.get("shadow_symbol", symbol)
    exec_symbol = req.get("exec_symbol", symbol)

    shadow = await _get_json(
        http, f"{shadow_url}/get_market_state/{shadow_symbol}",
        limiter=lim_shadow, timeout=timeout_s, attempts=attempts,
    )

    metrics = shadow.get("metrics", {}) or {}
    micro_shift = float(metrics.get("micro_price_shift") or 0.0)
    imbalance = float(metrics.get("orderbook_imbalance") or 0.0)
    ls_ratio = float(shadow.get("long_short_ratio") or 1.0)

    funding_rate = req.get("funding_rate", None)
    if funding_rate is None:
        prem = await fetch_premium_index(http, binance_fapi, shadow_symbol, timeout_s)
        funding_rate = prem.get("lastFundingRate", 0.0)
        notes.append("funding_rate from premiumIndex (best-effort)")

    brain_payload = {
        "symbol": shadow_symbol,
        "lle": req.get("lle", -0.05),
        "drawdown_pct": req.get("drawdown_pct", 0.0),
        "chaos_detected": bool(req.get("chaos_detected", False)),
        "funding_rate": float(funding_rate or 0.0),
        "oi_spike": bool(req.get("oi_spike", False)),
        "heatmap_intensity": req.get("heatmap_intensity", "LOW"),
        "recent_pnl_history": req.get("recent_pnl_history", []) or [],
        "returns_array": req.get("returns_array", []) or [],
        "contagion_correlation": float(req.get("contagion_correlation", 0.0) or 0.0),
        "micro_price_shift": micro_shift,
        "orderbook_imbalance": imbalance,
        "long_short_ratio": ls_ratio,
    }

    decision = await _post_json(
        http, f"{brain_url}/process_tick", brain_payload,
        limiter=lim_brain, timeout=timeout_s, attempts=attempts,
    )

    action = (decision.get("action") or "WAIT").upper()
    conf = float(decision.get("confidence") or 0.0)
    risk_mult = float(decision.get("risk_multiplier") or 0.0)
    venue = (req.get("venue") or "binance").upper()

    resp = {
        "status": "OK",
        "pipeline": "v2_legacy",
        "symbol": str(shadow_symbol).upper(),
        "venue": venue,
        "decision": decision,
        "shadowglass": shadow,
        "execution": None,
        "notes": notes,
    }

    if action != "EXECUTE":
        resp["status"] = "SKIPPED"
        notes.append(f"brain_action={action}")
        return resp

    min_conf = float(req.get("min_confidence", 0.55) or 0.55)
    if conf < min_conf:
        resp["status"] = "SKIPPED"
        notes.append(f"confidence {conf:.2f} < min_confidence {min_conf:.2f}")
        return resp

    base_risk_pct = float(req.get("base_risk_pct", 0.01) or 0.01)
    scale_by_conf = bool(req.get("scale_by_confidence", True))
    scale = risk_mult
    if scale_by_conf:
        scale *= clamp(conf, 0.25, 1.0)

    final_risk_pct = clamp(base_risk_pct * scale, 0.0005, 0.05)
    notes.append(f"final_risk_pct={final_risk_pct:.5f}")

    if bool(req.get("dry_run", False)):
        resp["status"] = "DRY_RUN"
        resp["execution"] = {
            "would_execute": True,
            "risk_pct": final_risk_pct,
            "side": decision.get("side"),
            "mapped_side": map_side(str(decision.get("side", "NONE"))) if action == "EXECUTE" else "none",
            "sl_pct": req.get("sl_pct", 0.015),
            "tp_pct": req.get("tp_pct", 0.045),
        }
        return resp

    side = map_side(str(decision.get("side") or "NONE"))
    exec_payload = {
        "symbol": exec_symbol,
        "side": side,
        "venue": (req.get("venue") or "binance"),
        "risk_pct": final_risk_pct,
        "sl_pct": float(req.get("sl_pct", 0.015)),
        "tp_pct": float(req.get("tp_pct", 0.045)),
        "reduce_only_brackets": True,
    }

    result = await _post_json(
        http, f"{exec_url}/execute_strike", exec_payload,
        limiter=lim_exec, timeout=max(timeout_s, 8.0), attempts=attempts,
    )
    resp["execution"] = result
    resp["status"] = "EXECUTED" if result.get("status") == "SUCCESS" else result.get("status", "EXECUTED")
    return resp
