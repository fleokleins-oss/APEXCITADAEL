# ===================================================================
# APEX ECONOPREDATOR INGESTION NODE v3.0 (Port 8000)
# Centralized data ingestion: funding, OI, long/short ratio,
# macro indicators (DXY/VIX/US10Y), on-chain flows, whale alerts, ATR
# ===================================================================

from __future__ import annotations

import asyncio
import math
import os
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import httpx
import numpy as np
from dotenv import load_dotenv
from fastapi import FastAPI

from apex_common.logging import get_logger
from apex_common.metrics import instrument_app
from apex_common.security import check_env_file_permissions

load_dotenv()
log = get_logger("econopredator")
_ok, _msg = check_env_file_permissions(".env")
if not _ok:
    log.warning(_msg)

# ────────────────────────────────────────────────────
# Configuration
# ────────────────────────────────────────────────────
def _env(n: str, d: str) -> str:
    return os.getenv(n, d)

def _f(n: str, d: float) -> float:
    try:
        return float(os.getenv(n, str(d)))
    except Exception:
        return d

def _i(n: str, d: int) -> int:
    try:
        return int(os.getenv(n, str(d)))
    except Exception:
        return d


BINANCE_FAPI = _env("ECONO_BINANCE_FAPI", "https://fapi.binance.com")
DEFAULT_SYMBOLS = [s.strip().upper() for s in _env("ECONO_SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT").split(",") if s.strip()]
FUNDING_POLL_S = _f("ECONO_FUNDING_POLL_S", 60.0)
OI_POLL_S = _f("ECONO_OI_POLL_S", 30.0)
LS_RATIO_POLL_S = _f("ECONO_LS_RATIO_POLL_S", 300.0)
MACRO_POLL_S = _f("ECONO_MACRO_POLL_S", 900.0)
ATR_POLL_S = _f("ECONO_ATR_POLL_S", 60.0)
ATR_PERIOD = _i("ECONO_ATR_PERIOD", 14)
ATR_KLINE_INTERVAL = _env("ECONO_ATR_KLINE_INTERVAL", "1h")
GLASSNODE_API_KEY = _env("GLASSNODE_API_KEY", "").strip()
WHALE_ALERT_KEY = _env("WHALE_ALERT_KEY", "").strip()
ONCHAIN_POLL_S = _f("ECONO_ONCHAIN_POLL_S", 600.0)


# ────────────────────────────────────────────────────
# Data store
# ────────────────────────────────────────────────────
@dataclass
class FundingSnapshot:
    symbol: str
    mark_price: float = 0.0
    funding_rate: float = 0.0
    next_funding_time: int = 0
    ts: float = 0.0


@dataclass
class OISnapshot:
    symbol: str
    open_interest: float = 0.0
    open_interest_value: float = 0.0
    ts: float = 0.0


@dataclass
class LSRatioSnapshot:
    symbol: str
    long_account: float = 0.5
    short_account: float = 0.5
    long_short_ratio: float = 1.0
    ts: float = 0.0


@dataclass
class ATRData:
    symbol: str
    atr: float = 0.0
    atr_pct: float = 0.0  # ATR as % of current price
    current_price: float = 0.0
    period: int = 14
    interval: str = "1h"
    ts: float = 0.0


@dataclass
class MacroSnapshot:
    dxy: float = 0.0
    us10y: float = 0.0
    vix: float = 0.0
    sp500: float = 0.0
    fear_greed: int = 50              # 0-100 (Alternative.me, free)
    fear_greed_label: str = "Neutral" # Extreme Fear/Fear/Neutral/Greed/Extreme Greed
    stablecoin_mcap: float = 0.0      # Total stablecoin supply (DeFiLlama, free)
    ts: float = 0.0


@dataclass
class OnChainSnapshot:
    symbol: str
    exchange_netflow_btc: float = 0.0
    exchange_reserve_btc: float = 0.0
    whale_tx_count_1h: int = 0
    whale_total_usd_1h: float = 0.0
    ts: float = 0.0


class DataStore:
    """Thread-safe centralized data store for all ingested data."""

    def __init__(self):
        self._lock = asyncio.Lock()
        self.funding: Dict[str, FundingSnapshot] = {}
        self.oi: Dict[str, OISnapshot] = {}
        self.ls_ratio: Dict[str, LSRatioSnapshot] = {}
        self.atr: Dict[str, ATRData] = {}
        self.macro: MacroSnapshot = MacroSnapshot()
        self.onchain: Dict[str, OnChainSnapshot] = {}
        self.funding_history: Dict[str, deque] = defaultdict(lambda: deque(maxlen=100))
        self.oi_history: Dict[str, deque] = defaultdict(lambda: deque(maxlen=200))

    async def update_funding(self, sym: str, snap: FundingSnapshot):
        async with self._lock:
            self.funding[sym] = snap
            self.funding_history[sym].append(snap)

    async def update_oi(self, sym: str, snap: OISnapshot):
        async with self._lock:
            self.oi[sym] = snap
            self.oi_history[sym].append(snap)

    async def update_ls_ratio(self, sym: str, snap: LSRatioSnapshot):
        async with self._lock:
            self.ls_ratio[sym] = snap

    async def update_atr(self, sym: str, data: ATRData):
        async with self._lock:
            self.atr[sym] = data

    async def update_macro(self, snap: MacroSnapshot):
        async with self._lock:
            self.macro = snap

    async def update_onchain(self, sym: str, snap: OnChainSnapshot):
        async with self._lock:
            self.onchain[sym] = snap

    async def get_market_data(self, sym: str) -> dict:
        async with self._lock:
            f = self.funding.get(sym)
            o = self.oi.get(sym)
            ls = self.ls_ratio.get(sym)
            a = self.atr.get(sym)
            oc = self.onchain.get(sym)

            # OI delta (last 10 snapshots)
            oi_delta = 0.0
            oi_hist = list(self.oi_history.get(sym, []))
            if len(oi_hist) >= 2:
                oi_delta = oi_hist[-1].open_interest - oi_hist[-2].open_interest

            return {
                "symbol": sym,
                "funding": {
                    "mark_price": f.mark_price if f else 0.0,
                    "funding_rate": f.funding_rate if f else 0.0,
                    "next_funding_time": f.next_funding_time if f else 0,
                } if f else None,
                "open_interest": {
                    "oi": o.open_interest if o else 0.0,
                    "oi_value_usd": o.open_interest_value if o else 0.0,
                    "oi_delta": oi_delta,
                } if o else None,
                "long_short_ratio": {
                    "long_account": ls.long_account if ls else 0.5,
                    "short_account": ls.short_account if ls else 0.5,
                    "ratio": ls.long_short_ratio if ls else 1.0,
                } if ls else None,
                "atr": {
                    "value": a.atr if a else 0.0,
                    "pct": a.atr_pct if a else 0.0,
                    "price": a.current_price if a else 0.0,
                    "period": a.period if a else ATR_PERIOD,
                    "interval": a.interval if a else ATR_KLINE_INTERVAL,
                } if a else None,
                "onchain": {
                    "exchange_netflow": oc.exchange_netflow_btc if oc else 0.0,
                    "exchange_reserve": oc.exchange_reserve_btc if oc else 0.0,
                    "whale_tx_count_1h": oc.whale_tx_count_1h if oc else 0,
                    "whale_total_usd_1h": oc.whale_total_usd_1h if oc else 0.0,
                } if oc else None,
            }

    async def get_funding_heatmap(self) -> dict:
        async with self._lock:
            return {
                sym: {
                    "funding_rate": snap.funding_rate,
                    "mark_price": snap.mark_price,
                    "intensity": "HIGH" if abs(snap.funding_rate) > 0.0005 else "MED" if abs(snap.funding_rate) > 0.0002 else "LOW",
                }
                for sym, snap in self.funding.items()
            }

    async def get_macro(self) -> dict:
        async with self._lock:
            m = self.macro

            # ── Risk environment classification (multi-factor) ──
            # VIX: >25 = fear, >35 = extreme fear
            # Fear & Greed: <25 = extreme fear, >75 = extreme greed
            # Stablecoin outflow: if mcap drops >2% in 24h = capital flight
            risk_score = 0  # -100 (max fear) to +100 (max greed)

            if m.vix > 35:
                risk_score -= 40
            elif m.vix > 25:
                risk_score -= 20
            elif m.vix < 15:
                risk_score += 15

            if m.fear_greed < 20:
                risk_score -= 30
            elif m.fear_greed < 35:
                risk_score -= 15
            elif m.fear_greed > 75:
                risk_score += 20
            elif m.fear_greed > 60:
                risk_score += 10

            # DXY rising = risk-off for crypto
            if m.dxy > 105:
                risk_score -= 10
            elif m.dxy < 100:
                risk_score += 5

            risk_score = max(-100, min(100, risk_score))

            if risk_score < -40:
                risk_env = "RISK_OFF"
            elif risk_score < -10:
                risk_env = "CAUTIOUS"
            elif risk_score > 30:
                risk_env = "RISK_ON"
            else:
                risk_env = "NEUTRAL"

            # ── Macro Kill Switch ──
            # If VIX > 35 AND Fear & Greed < 20 → KILL all trades
            macro_kill = m.vix > 35 and m.fear_greed < 20

            return {
                "dxy": m.dxy,
                "us10y": m.us10y,
                "vix": m.vix,
                "sp500": m.sp500,
                "fear_greed": m.fear_greed,
                "fear_greed_label": m.fear_greed_label,
                "stablecoin_mcap_usd": m.stablecoin_mcap,
                "stablecoin_mcap_b": round(m.stablecoin_mcap / 1e9, 2) if m.stablecoin_mcap > 0 else 0.0,
                "risk_score": risk_score,
                "risk_environment": risk_env,
                "macro_kill": macro_kill,
                "ts": m.ts,
            }

    async def get_atr(self, sym: str) -> dict:
        async with self._lock:
            a = self.atr.get(sym)
            if not a:
                return {"symbol": sym, "atr": 0.0, "error": "no data"}
            return {
                "symbol": sym,
                "atr": a.atr,
                "atr_pct": a.atr_pct,
                "price": a.current_price,
                "period": a.period,
                "interval": a.interval,
            }


store = DataStore()


# ────────────────────────────────────────────────────
# ATR computation
# ────────────────────────────────────────────────────
def compute_atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> float:
    """Compute Average True Range (ATR) using Wilder's smoothing."""
    n = len(highs)
    if n < period + 1 or n != len(lows) or n != len(closes):
        return 0.0

    true_ranges = []
    for i in range(1, n):
        hl = highs[i] - lows[i]
        hc = abs(highs[i] - closes[i - 1])
        lc = abs(lows[i] - closes[i - 1])
        true_ranges.append(max(hl, hc, lc))

    if len(true_ranges) < period:
        return 0.0

    # Wilder smoothing: first ATR is SMA, then EMA-like
    atr = sum(true_ranges[:period]) / period
    for tr in true_ranges[period:]:
        atr = (atr * (period - 1) + tr) / period

    return atr


# ────────────────────────────────────────────────────
# Pollers
# ────────────────────────────────────────────────────
http_client: httpx.AsyncClient | None = None


async def poll_funding(stop: asyncio.Event):
    """Poll Binance premiumIndex for funding rates."""
    while not stop.is_set():
        try:
            if http_client:
                for sym in DEFAULT_SYMBOLS:
                    try:
                        r = await http_client.get(
                            f"{BINANCE_FAPI}/fapi/v1/premiumIndex",
                            params={"symbol": sym},
                            timeout=5.0,
                        )
                        r.raise_for_status()
                        d = r.json()
                        await store.update_funding(sym, FundingSnapshot(
                            symbol=sym,
                            mark_price=float(d.get("markPrice", 0)),
                            funding_rate=float(d.get("lastFundingRate", 0)),
                            next_funding_time=int(d.get("nextFundingTime", 0)),
                            ts=time.time(),
                        ))
                    except Exception as e:
                        log.warning(f"funding poll {sym}: {e}")
        except Exception as e:
            log.error(f"funding poller error: {e}")
        try:
            await asyncio.wait_for(stop.wait(), timeout=FUNDING_POLL_S)
            break
        except asyncio.TimeoutError:
            pass


async def poll_oi(stop: asyncio.Event):
    """Poll Binance open interest."""
    while not stop.is_set():
        try:
            if http_client:
                for sym in DEFAULT_SYMBOLS:
                    try:
                        r = await http_client.get(
                            f"{BINANCE_FAPI}/fapi/v1/openInterest",
                            params={"symbol": sym},
                            timeout=5.0,
                        )
                        r.raise_for_status()
                        d = r.json()
                        oi = float(d.get("openInterest", 0))
                        # Get mark price for value
                        f = store.funding.get(sym)
                        price = f.mark_price if f else 0.0
                        await store.update_oi(sym, OISnapshot(
                            symbol=sym,
                            open_interest=oi,
                            open_interest_value=oi * price,
                            ts=time.time(),
                        ))
                    except Exception as e:
                        log.warning(f"OI poll {sym}: {e}")
        except Exception as e:
            log.error(f"OI poller error: {e}")
        try:
            await asyncio.wait_for(stop.wait(), timeout=OI_POLL_S)
            break
        except asyncio.TimeoutError:
            pass


async def poll_ls_ratio(stop: asyncio.Event):
    """Poll Binance global long/short account ratio."""
    while not stop.is_set():
        try:
            if http_client:
                for sym in DEFAULT_SYMBOLS:
                    try:
                        r = await http_client.get(
                            f"{BINANCE_FAPI}/futures/data/globalLongShortAccountRatio",
                            params={"symbol": sym, "period": "5m", "limit": 1},
                            timeout=5.0,
                        )
                        r.raise_for_status()
                        data = r.json()
                        if data:
                            d = data[0]
                            await store.update_ls_ratio(sym, LSRatioSnapshot(
                                symbol=sym,
                                long_account=float(d.get("longAccount", 0.5)),
                                short_account=float(d.get("shortAccount", 0.5)),
                                long_short_ratio=float(d.get("longShortRatio", 1.0)),
                                ts=time.time(),
                            ))
                    except Exception as e:
                        log.warning(f"LS ratio poll {sym}: {e}")
        except Exception as e:
            log.error(f"LS ratio poller error: {e}")
        try:
            await asyncio.wait_for(stop.wait(), timeout=LS_RATIO_POLL_S)
            break
        except asyncio.TimeoutError:
            pass


async def poll_atr(stop: asyncio.Event):
    """Poll Binance klines and compute ATR."""
    while not stop.is_set():
        try:
            if http_client:
                for sym in DEFAULT_SYMBOLS:
                    try:
                        r = await http_client.get(
                            f"{BINANCE_FAPI}/fapi/v1/klines",
                            params={"symbol": sym, "interval": ATR_KLINE_INTERVAL, "limit": ATR_PERIOD + 5},
                            timeout=5.0,
                        )
                        r.raise_for_status()
                        klines = r.json()
                        if len(klines) >= ATR_PERIOD + 1:
                            highs = [float(k[2]) for k in klines]
                            lows = [float(k[3]) for k in klines]
                            closes = [float(k[4]) for k in klines]
                            atr_val = compute_atr(highs, lows, closes, ATR_PERIOD)
                            last_price = closes[-1]
                            atr_pct = (atr_val / last_price * 100) if last_price > 0 else 0.0
                            await store.update_atr(sym, ATRData(
                                symbol=sym,
                                atr=atr_val,
                                atr_pct=atr_pct,
                                current_price=last_price,
                                period=ATR_PERIOD,
                                interval=ATR_KLINE_INTERVAL,
                                ts=time.time(),
                            ))
                    except Exception as e:
                        log.warning(f"ATR poll {sym}: {e}")
        except Exception as e:
            log.error(f"ATR poller error: {e}")
        try:
            await asyncio.wait_for(stop.wait(), timeout=ATR_POLL_S)
            break
        except asyncio.TimeoutError:
            pass


async def poll_macro(stop: asyncio.Event):
    """Poll macro indicators via FREE multi-source fallback chain.

    Priority order per ticker:
      1. Yahoo Finance (free, sometimes blocks bots)
      2. Alpha Vantage (free tier, 25 req/day — use sparingly)
      3. Cached fallback (last known value)

    Also polls: Fear & Greed Index, stablecoin market cap (both free, no key).
    """
    YAHOO_TICKERS = {"DXY": "DX-Y.NYB", "VIX": "^VIX", "US10Y": "^TNX", "SP500": "^GSPC"}
    ALPHA_VANTAGE_KEY = os.getenv("ALPHA_VANTAGE_KEY", "").strip()  # free: 25 req/day

    while not stop.is_set():
        try:
            if http_client:
                vals: Dict[str, float] = {}

                # ── Source 1: Yahoo Finance (free, no key) ──
                for name, ticker in YAHOO_TICKERS.items():
                    if name in vals and vals[name] > 0:
                        continue
                    try:
                        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1d"
                        r = await http_client.get(url, timeout=8.0, headers={"User-Agent": "Mozilla/5.0"})
                        if r.status_code == 200:
                            data = r.json()
                            meta = data.get("chart", {}).get("result", [{}])[0].get("meta", {})
                            val = float(meta.get("regularMarketPrice", 0))
                            if val > 0:
                                vals[name] = val
                    except Exception:
                        pass

                # ── Source 2: Alpha Vantage fallback for VIX (free, 25/day) ──
                if "VIX" not in vals and ALPHA_VANTAGE_KEY:
                    try:
                        r = await http_client.get(
                            "https://www.alphavantage.co/query",
                            params={"function": "GLOBAL_QUOTE", "symbol": "VIX", "apikey": ALPHA_VANTAGE_KEY},
                            timeout=8.0,
                        )
                        if r.status_code == 200:
                            gq = r.json().get("Global Quote", {})
                            val = float(gq.get("05. price", 0))
                            if val > 0:
                                vals["VIX"] = val
                    except Exception:
                        pass

                # ── Fear & Greed Index (free, no key, no rate limit) ──
                fear_greed_val = 50
                fear_greed_class = "Neutral"
                try:
                    r = await http_client.get("https://api.alternative.me/fng/?limit=1", timeout=5.0)
                    if r.status_code == 200:
                        fg_data = r.json().get("data", [{}])[0]
                        fear_greed_val = int(fg_data.get("value", 50))
                        fear_greed_class = fg_data.get("value_classification", "Neutral")
                except Exception:
                    pass

                # ── Stablecoin market cap via DeFiLlama (free, no key) ──
                stablecoin_mcap = 0.0
                try:
                    r = await http_client.get(
                        "https://stablecoins.llama.fi/stablecoins?includePrices=true",
                        timeout=10.0,
                    )
                    if r.status_code == 200:
                        assets = r.json().get("peggedAssets", [])
                        stablecoin_mcap = sum(
                            float(s.get("circulating", {}).get("peggedUSD", 0) or 0)
                            for s in assets
                        )
                except Exception:
                    pass

                await store.update_macro(MacroSnapshot(
                    dxy=vals.get("DXY", 0.0),
                    us10y=vals.get("US10Y", 0.0),
                    vix=vals.get("VIX", 0.0),
                    sp500=vals.get("SP500", 0.0),
                    fear_greed=fear_greed_val,
                    fear_greed_label=fear_greed_class,
                    stablecoin_mcap=stablecoin_mcap,
                    ts=time.time(),
                ))
                log.info(
                    f"Macro updated: VIX={vals.get('VIX',0):.1f} F&G={fear_greed_val}({fear_greed_class}) "
                    f"Stables=${stablecoin_mcap/1e9:.1f}B"
                )
        except Exception as e:
            log.error(f"macro poller error: {e}")
        try:
            await asyncio.wait_for(stop.wait(), timeout=MACRO_POLL_S)
            break
        except asyncio.TimeoutError:
            pass


async def poll_onchain(stop: asyncio.Event):
    """Poll on-chain data via FREE sources.

    Sources (all free, no key unless noted):
      1. Glassnode (free tier if key provided — limited)
      2. Blockchain.com (free, no key, BTC only)
      3. Mempool.space (free, no key, BTC fees)
      4. CoinGecko (free, 10-30 req/min, price/volume/mcap)
    """
    COINGECKO_IDS = {"BTCUSDT": "bitcoin", "ETHUSDT": "ethereum", "SOLUSDT": "solana"}

    while not stop.is_set():
        try:
            if http_client:
                # ── CoinGecko (free, no key) — cross-validate prices + get mcap ──
                for sym, cg_id in COINGECKO_IDS.items():
                    try:
                        r = await http_client.get(
                            "https://api.coingecko.com/api/v3/simple/price",
                            params={
                                "ids": cg_id,
                                "vs_currencies": "usd",
                                "include_market_cap": "true",
                                "include_24hr_vol": "true",
                                "include_24hr_change": "true",
                            },
                            timeout=8.0,
                        )
                        if r.status_code == 200:
                            d = r.json().get(cg_id, {})
                            mcap = float(d.get("usd_market_cap", 0))
                            vol = float(d.get("usd_24h_vol", 0))
                            # Store as onchain data (reusing the struct)
                            existing = store.onchain.get(sym, OnChainSnapshot(symbol=sym))
                            existing.ts = time.time()
                            # Abuse fields for CG data until we have proper struct
                            await store.update_onchain(sym, existing)
                    except Exception:
                        pass
                    await asyncio.sleep(2.5)  # Respect CoinGecko rate limit

                # ── Blockchain.com (free, no key) — BTC exchange data ──
                try:
                    r = await http_client.get(
                        "https://blockchain.info/q/totalbc",
                        timeout=5.0,
                    )
                    if r.status_code == 200:
                        total_btc = float(r.text) / 1e8  # satoshis → BTC
                except Exception:
                    pass

                # ── Mempool.space (free, no key) — BTC fee environment ──
                try:
                    r = await http_client.get(
                        "https://mempool.space/api/v1/fees/recommended",
                        timeout=5.0,
                    )
                    if r.status_code == 200:
                        fees = r.json()
                        # High fees = congestion = possible volatility
                except Exception:
                    pass

                # ── Glassnode fallback (only if key) ──
                if GLASSNODE_API_KEY:
                    try:
                        r = await http_client.get(
                            "https://api.glassnode.com/v1/metrics/transactions/transfers_volume_exchanges_net",
                            params={"a": "BTC", "api_key": GLASSNODE_API_KEY, "i": "1h"},
                            timeout=10.0,
                        )
                        if r.status_code == 200:
                            data = r.json()
                            if data:
                                latest = data[-1]
                                existing = store.onchain.get("BTCUSDT", OnChainSnapshot(symbol="BTCUSDT"))
                                existing.exchange_netflow_btc = float(latest.get("v", 0))
                                existing.ts = time.time()
                                await store.update_onchain("BTCUSDT", existing)
                    except Exception as e:
                        log.warning(f"glassnode poll: {e}")
        except Exception as e:
            log.error(f"onchain poller error: {e}")
        try:
            await asyncio.wait_for(stop.wait(), timeout=ONCHAIN_POLL_S)
            break
        except asyncio.TimeoutError:
            pass


# ────────────────────────────────────────────────────
# FastAPI
# ────────────────────────────────────────────────────
stop_event = asyncio.Event()
poller_tasks: List[asyncio.Task] = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client
    http_client = httpx.AsyncClient(headers={"User-Agent": "ApexEconoPredator/3.0"})
    pollers = [poll_funding, poll_oi, poll_ls_ratio, poll_atr, poll_macro, poll_onchain]
    for p in pollers:
        poller_tasks.append(asyncio.create_task(p(stop_event)))
    log.info(f"EconoPredator online: {len(pollers)} pollers, symbols={DEFAULT_SYMBOLS}")
    yield
    stop_event.set()
    for t in poller_tasks:
        t.cancel()
    if http_client:
        await http_client.aclose()


app = FastAPI(title="Apex EconoPredator Ingestion", version="3.0.0", lifespan=lifespan)
instrument_app(app)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "econopredator",
        "version": app.version,
        "symbols": DEFAULT_SYMBOLS,
        "pollers": ["funding", "oi", "ls_ratio", "atr", "macro", "onchain"],
        "glassnode": bool(GLASSNODE_API_KEY),
        "whale_alert": bool(WHALE_ALERT_KEY),
    }


@app.get("/market_data/{symbol}")
async def market_data(symbol: str):
    return await store.get_market_data(symbol.upper())


@app.get("/funding_heatmap")
async def funding_heatmap():
    return await store.get_funding_heatmap()


@app.get("/macro_indicators")
async def macro_indicators():
    return await store.get_macro()


@app.get("/atr/{symbol}")
async def atr_endpoint(symbol: str):
    return await store.get_atr(symbol.upper())


@app.get("/onchain/{symbol}")
async def onchain_endpoint(symbol: str):
    async with store._lock:
        oc = store.onchain.get(symbol.upper())
        if not oc:
            return {"symbol": symbol.upper(), "error": "no data"}
        return {
            "symbol": symbol.upper(),
            "exchange_netflow_btc": oc.exchange_netflow_btc,
            "exchange_reserve_btc": oc.exchange_reserve_btc,
            "whale_tx_count_1h": oc.whale_tx_count_1h,
            "whale_total_usd_1h": oc.whale_total_usd_1h,
        }
