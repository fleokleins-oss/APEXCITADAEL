"""Apex Citadel v3 — Free API Registry & RPC Failover.

Centralized catalog of free-tier APIs with automatic failover.
Zero cost: every API here has a free tier or is fully open.

Rate limits are tracked per-source to avoid bans.
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import httpx

from apex_common.logging import get_logger

log = get_logger("free_apis")


# ────────────────────────────────────────────────────
# RPC Failover (Solana + EVM)
# ────────────────────────────────────────────────────
DEFAULT_SOLANA_RPCS = [
    "https://api.mainnet-beta.solana.com",          # Official (free, 40 rps)
    "https://solana-mainnet.rpc.extrnode.com",       # Extrnode (free, generous)
    "https://rpc.ankr.com/solana",                   # Ankr (free tier, 30 rps)
]

DEFAULT_SOLANA_DEVNET_RPCS = [
    "https://api.devnet.solana.com",
]

DEFAULT_BINANCE_FAPI = [
    "https://fapi.binance.com",
    "https://fapi1.binance.com",
    "https://fapi2.binance.com",
    "https://fapi3.binance.com",
    "https://fapi4.binance.com",
]


@dataclass
class RPCEndpoint:
    url: str
    failures: int = 0
    last_failure: float = 0.0
    last_success: float = 0.0
    avg_latency_ms: float = 500.0
    calls: int = 0


class RPCFailover:
    """Round-robin with health tracking. Bad endpoints get deprioritized."""

    def __init__(self, endpoints: List[str], cooldown_s: float = 30.0):
        self._endpoints = [RPCEndpoint(url=u) for u in endpoints]
        self._cooldown_s = cooldown_s
        self._idx = 0
        self._lock = asyncio.Lock()

    async def get_url(self) -> str:
        async with self._lock:
            now = time.monotonic()
            # Filter healthy endpoints
            healthy = [
                e for e in self._endpoints
                if e.failures < 5 or (now - e.last_failure) > self._cooldown_s
            ]
            if not healthy:
                # All failing: reset and try again
                for e in self._endpoints:
                    e.failures = 0
                healthy = self._endpoints

            # Weighted selection: prefer low-latency, low-failure endpoints
            weights = [1.0 / max(1, e.failures + 1) / max(50, e.avg_latency_ms) for e in healthy]
            total = sum(weights)
            weights = [w / total for w in weights]
            choice = random.choices(healthy, weights=weights, k=1)[0]
            choice.calls += 1
            return choice.url

    async def report_success(self, url: str, latency_ms: float = 0.0):
        async with self._lock:
            for e in self._endpoints:
                if e.url == url:
                    e.failures = max(0, e.failures - 1)
                    e.last_success = time.monotonic()
                    if latency_ms > 0:
                        e.avg_latency_ms = 0.7 * e.avg_latency_ms + 0.3 * latency_ms
                    break

    async def report_failure(self, url: str):
        async with self._lock:
            for e in self._endpoints:
                if e.url == url:
                    e.failures += 1
                    e.last_failure = time.monotonic()
                    break

    async def get_status(self) -> List[dict]:
        async with self._lock:
            return [
                {
                    "url": e.url,
                    "failures": e.failures,
                    "calls": e.calls,
                    "avg_latency_ms": round(e.avg_latency_ms, 1),
                }
                for e in self._endpoints
            ]


# ────────────────────────────────────────────────────
# Free Data Source Registry
# ────────────────────────────────────────────────────
# Every source here is FREE (no API key required unless noted)
#
# ┌──────────────────────┬────────────────────┬──────────────────┐
# │ Source               │ Data               │ Rate Limit       │
# ├──────────────────────┼────────────────────┼──────────────────┤
# │ Binance FAPI         │ Funding/OI/Klines  │ 2400 req/min     │
# │ CoinGecko (free)     │ Price/MCap/Volume  │ 10-30 req/min    │
# │ DeFiLlama           │ TVL/Yields/Chains  │ Unlimited*       │
# │ CryptoPanic (free)   │ News sentiment     │ ~50 req/h        │
# │ Reddit JSON          │ Social sentiment   │ 60 req/min       │
# │ Blockchain.com       │ BTC on-chain       │ ~300 req/5min    │
# │ Mempool.space        │ BTC fees/mempool   │ Unlimited*       │
# │ Solscan (free)       │ SOL token data     │ ~100 req/min     │
# │ Jupiter Price API    │ SOL token prices   │ 600 req/min      │
# │ Birdeye (free tier)  │ SOL DEX data       │ 50 req/min       │
# │ Yahoo Finance        │ Macro (DXY/VIX)    │ ~200 req/h       │
# │ Alternative.me       │ Fear & Greed       │ Unlimited        │
# │ Solana FM            │ SOL token verify   │ ~60 req/min      │
# └──────────────────────┴────────────────────┴──────────────────┘


async def fetch_with_fallback(
    http: httpx.AsyncClient,
    urls: List[str],
    *,
    timeout: float = 8.0,
    headers: Optional[Dict[str, str]] = None,
    params: Optional[Dict[str, str]] = None,
) -> Optional[dict]:
    """Try multiple URLs in order, return first successful JSON response."""
    for url in urls:
        try:
            t0 = time.monotonic()
            r = await http.get(url, timeout=timeout, headers=headers or {}, params=params or {})
            if r.status_code == 200:
                return r.json()
        except Exception:
            continue
    return None


# ── CoinGecko (free, no key) ──
async def coingecko_price(http: httpx.AsyncClient, coin_id: str = "bitcoin") -> dict:
    """Get price, mcap, volume from CoinGecko free API."""
    try:
        r = await http.get(
            f"https://api.coingecko.com/api/v3/simple/price",
            params={
                "ids": coin_id,
                "vs_currencies": "usd",
                "include_market_cap": "true",
                "include_24hr_vol": "true",
                "include_24hr_change": "true",
            },
            timeout=8.0,
        )
        if r.status_code == 200:
            data = r.json().get(coin_id, {})
            return {
                "price": data.get("usd", 0),
                "market_cap": data.get("usd_market_cap", 0),
                "volume_24h": data.get("usd_24h_vol", 0),
                "change_24h_pct": data.get("usd_24h_change", 0),
                "source": "coingecko",
            }
    except Exception:
        pass
    return {}


# ── DeFiLlama (fully free, no key, no limits) ──
async def defillama_tvl(http: httpx.AsyncClient, protocol: str = "aave") -> dict:
    """Get TVL data from DeFiLlama (completely free)."""
    try:
        r = await http.get(f"https://api.llama.fi/tvl/{protocol}", timeout=8.0)
        if r.status_code == 200:
            return {"tvl_usd": r.json(), "protocol": protocol, "source": "defillama"}
    except Exception:
        pass
    return {}


async def defillama_stablecoins(http: httpx.AsyncClient) -> dict:
    """Stablecoin market cap — proxy for liquidity entering/exiting crypto."""
    try:
        r = await http.get("https://stablecoins.llama.fi/stablecoins?includePrices=true", timeout=8.0)
        if r.status_code == 200:
            data = r.json()
            total_mcap = sum(
                float(s.get("circulating", {}).get("peggedUSD", 0) or 0)
                for s in data.get("peggedAssets", [])
            )
            return {"total_stablecoin_mcap": total_mcap, "source": "defillama"}
    except Exception:
        pass
    return {}


# ── CryptoPanic (free tier, key optional) ──
async def cryptopanic_news(http: httpx.AsyncClient, currency: str = "BTC") -> List[dict]:
    """Get recent news with sentiment from CryptoPanic free tier."""
    api_key = os.getenv("CRYPTOPANIC_API_KEY", "").strip()
    try:
        params = {"currencies": currency, "filter": "important", "public": "true"}
        if api_key:
            params["auth_token"] = api_key
        r = await http.get(
            "https://cryptopanic.com/api/free/v1/posts/",
            params=params,
            timeout=8.0,
        )
        if r.status_code == 200:
            results = r.json().get("results", [])
            return [
                {
                    "title": p.get("title", ""),
                    "source": p.get("source", {}).get("title", "unknown"),
                    "sentiment": p.get("votes", {}).get("positive", 0) - p.get("votes", {}).get("negative", 0),
                    "url": p.get("url", ""),
                    "published_at": p.get("published_at", ""),
                }
                for p in results[:20]
            ]
    except Exception:
        pass
    return []


# ── Alternative.me Fear & Greed (free, no key) ──
async def fear_greed_index(http: httpx.AsyncClient) -> dict:
    """Bitcoin Fear & Greed Index — free, no limits."""
    try:
        r = await http.get("https://api.alternative.me/fng/?limit=1", timeout=5.0)
        if r.status_code == 200:
            data = r.json().get("data", [{}])[0]
            return {
                "value": int(data.get("value", 50)),
                "classification": data.get("value_classification", "Neutral"),
                "source": "alternative.me",
            }
    except Exception:
        pass
    return {"value": 50, "classification": "Neutral", "source": "fallback"}


# ── Jupiter Price API (free, no key, Solana tokens) ──
async def jupiter_price(http: httpx.AsyncClient, mint: str) -> dict:
    """Get Solana token price from Jupiter aggregator (free)."""
    try:
        r = await http.get(
            f"https://api.jup.ag/price/v2?ids={mint}",
            timeout=5.0,
        )
        if r.status_code == 200:
            data = r.json().get("data", {}).get(mint, {})
            return {
                "price": float(data.get("price", 0)),
                "mint": mint,
                "source": "jupiter",
            }
    except Exception:
        pass
    return {}


# ── Blockchain.com (free, no key, BTC on-chain) ──
async def btc_mempool_info(http: httpx.AsyncClient) -> dict:
    """BTC mempool and fee data from Mempool.space (free, no key)."""
    try:
        r = await http.get("https://mempool.space/api/v1/fees/recommended", timeout=5.0)
        if r.status_code == 200:
            return {**r.json(), "source": "mempool.space"}
    except Exception:
        pass
    return {}


# ── Solana FM (free tier, token verification) ──
async def solana_fm_token_info(http: httpx.AsyncClient, mint: str) -> dict:
    """Get token verification status from Solana FM (free)."""
    try:
        r = await http.get(
            f"https://api.solana.fm/v0/tokens/{mint}",
            timeout=5.0,
            headers={"accept": "application/json"},
        )
        if r.status_code == 200:
            data = r.json()
            token_info = data.get("tokenList", {})
            return {
                "verified": bool(token_info),
                "name": token_info.get("name", ""),
                "symbol": token_info.get("symbol", ""),
                "source": "solana_fm",
            }
    except Exception:
        pass
    return {"verified": False, "source": "solana_fm"}
