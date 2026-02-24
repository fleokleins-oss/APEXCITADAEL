# ===================================================================
# APEX NEWTONIAN BROTHER GRAVITATIONAL MODEL v3.0 (Port 8001)
# Physics-inspired cross-asset correlation with acceleration vectors
# Regime detection: CONVERGENCE / DIVERGENCE / CONTAGION / ISOLATION
# ===================================================================

from __future__ import annotations

import asyncio
import math
import os
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Dict, List, Literal, Optional, Tuple

import httpx
import numpy as np
from dotenv import load_dotenv
from fastapi import FastAPI
from pydantic import BaseModel, Field

from apex_common.logging import get_logger
from apex_common.metrics import instrument_app
from apex_common.security import check_env_file_permissions

load_dotenv()
log = get_logger("newtonian")
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


TRACKED_ASSETS = [s.strip().upper() for s in _env("NEWTON_TRACKED_ASSETS", "BTC,ETH,SOL").split(",") if s.strip()]
LOOKBACK_PERIODS = _i("NEWTON_LOOKBACK_PERIODS", 60)
EPOCH_INTERVAL_S = _f("NEWTON_EPOCH_INTERVAL_S", 300.0)
G_CONSTANT = _f("NEWTON_G_CONSTANT", 1.0)
ACCEL_THRESHOLD = _f("NEWTON_ACCEL_THRESHOLD", 0.15)
CONTAGION_MULTIPLIER = _f("NEWTON_CONTAGION_MULTIPLIER", 2.0)
KLINE_INTERVAL = _env("NEWTON_KLINE_INTERVAL", "5m")
KLINE_LIMIT = _i("NEWTON_KLINE_LIMIT", 100)
BINANCE_FAPI = _env("NEWTON_BINANCE_FAPI", "https://fapi.binance.com")

Regime = Literal["CONVERGENCE", "DIVERGENCE", "CONTAGION", "ISOLATION", "UNKNOWN"]


# ────────────────────────────────────────────────────
# Data structures
# ────────────────────────────────────────────────────
@dataclass
class AssetState:
    """Rolling state for a single asset."""
    symbol: str
    returns: deque = field(default_factory=lambda: deque(maxlen=500))
    last_price: float = 0.0
    market_cap_proxy: float = 0.0       # sqrt(mcap * vol24h) or just vol24h
    volume_24h: float = 0.0
    last_update: float = 0.0


@dataclass
class PairGravity:
    """Gravitational state between two assets."""
    asset_a: str
    asset_b: str
    correlation: float = 0.0
    g_force: float = 0.0
    acceleration: float = 0.0           # dF/dt
    prev_g_force: float = 0.0
    regime: Regime = "UNKNOWN"
    last_compute: float = 0.0


# ────────────────────────────────────────────────────
# Math
# ────────────────────────────────────────────────────
def rolling_correlation(a: List[float], b: List[float], window: int) -> float:
    """Pearson correlation over the last `window` observations."""
    n = min(len(a), len(b), window)
    if n < 5:
        return 0.0
    x = np.array(a[-n:], dtype=float)
    y = np.array(b[-n:], dtype=float)
    sx, sy = np.std(x), np.std(y)
    if sx < 1e-12 or sy < 1e-12:
        return 0.0
    r = float(np.corrcoef(x, y)[0, 1])
    if not math.isfinite(r):
        return 0.0
    return max(-1.0, min(1.0, r))


def compute_g_force(
    mass_a: float,
    mass_b: float,
    correlation: float,
    g_constant: float = 1.0,
) -> float:
    """Gravitational force between two assets.

    F = G * M_a * M_b / d^2
    where d = 1 - |correlation| (clamped to avoid division by zero)
    """
    d = max(0.05, 1.0 - abs(correlation))
    return g_constant * (mass_a * mass_b) / (d * d)


def classify_regime(
    correlation: float,
    acceleration: float,
    threshold: float,
    contagion_mult: float,
) -> Regime:
    """Classify the gravitational regime between two assets."""
    if acceleration > threshold * contagion_mult and correlation > 0.7:
        return "CONTAGION"
    if abs(correlation) < 0.3:
        return "ISOLATION"
    if acceleration > threshold and correlation > 0.5:
        return "CONVERGENCE"
    if acceleration < -threshold:
        return "DIVERGENCE"
    return "CONVERGENCE" if correlation > 0.5 else "ISOLATION"


# ────────────────────────────────────────────────────
# Gravity Engine
# ────────────────────────────────────────────────────
class GravityEngine:
    """Manages gravitational state across all tracked asset pairs."""

    def __init__(self, assets: List[str]):
        self.assets: Dict[str, AssetState] = {
            a: AssetState(symbol=a) for a in assets
        }
        self.pairs: Dict[str, PairGravity] = {}
        self._lock = asyncio.Lock()
        self.last_epoch: float = 0.0
        self.epochs_computed: int = 0

        # Initialize all pairs
        for i, a in enumerate(assets):
            for b in assets[i + 1:]:
                key = f"{a}_{b}"
                self.pairs[key] = PairGravity(asset_a=a, asset_b=b)

    def _pair_key(self, a: str, b: str) -> str:
        return f"{a}_{b}" if f"{a}_{b}" in self.pairs else f"{b}_{a}"

    async def ingest_returns(self, symbol: str, returns: List[float], price: float, volume_24h: float = 0.0):
        """Ingest new return data for an asset."""
        async with self._lock:
            s = symbol.upper()
            if s not in self.assets:
                return
            asset = self.assets[s]
            asset.returns.extend(returns)
            asset.last_price = price
            asset.volume_24h = volume_24h
            # Mass proxy: sqrt(price * volume) as a rough relative measure
            asset.market_cap_proxy = math.sqrt(max(1.0, price * max(1.0, volume_24h)))
            asset.last_update = time.monotonic()

    async def compute_epoch(self):
        """Recompute all gravitational vectors. Called periodically."""
        async with self._lock:
            now = time.monotonic()
            self.last_epoch = now
            self.epochs_computed += 1

            for key, pair in self.pairs.items():
                a_state = self.assets.get(pair.asset_a)
                b_state = self.assets.get(pair.asset_b)
                if not a_state or not b_state:
                    continue

                a_rets = list(a_state.returns)
                b_rets = list(b_state.returns)

                # Correlation
                corr = rolling_correlation(a_rets, b_rets, LOOKBACK_PERIODS)
                pair.correlation = corr

                # G-force
                pair.prev_g_force = pair.g_force
                pair.g_force = compute_g_force(
                    a_state.market_cap_proxy,
                    b_state.market_cap_proxy,
                    corr,
                    G_CONSTANT,
                )

                # Acceleration (dF/dt)
                if pair.prev_g_force > 0 and pair.last_compute > 0:
                    dt = max(1.0, now - pair.last_compute)
                    pair.acceleration = (pair.g_force - pair.prev_g_force) / dt
                else:
                    pair.acceleration = 0.0

                # Regime
                pair.regime = classify_regime(
                    corr,
                    pair.acceleration,
                    ACCEL_THRESHOLD,
                    CONTAGION_MULTIPLIER,
                )
                pair.last_compute = now

    async def get_pair_state(self, pair_key: str) -> Optional[dict]:
        async with self._lock:
            pair = self.pairs.get(pair_key.upper())
            if not pair:
                return None
            return self._serialize_pair(pair)

    async def get_all_state(self) -> dict:
        async with self._lock:
            pairs_out = {}
            for key, pair in self.pairs.items():
                pairs_out[key] = self._serialize_pair(pair)

            # Global regime: if any pair is CONTAGION, global is CONTAGION
            regimes = [p.regime for p in self.pairs.values()]
            if "CONTAGION" in regimes:
                global_regime = "CONTAGION"
            elif all(r == "ISOLATION" for r in regimes):
                global_regime = "ISOLATION"
            elif regimes.count("CONVERGENCE") > regimes.count("DIVERGENCE"):
                global_regime = "CONVERGENCE"
            elif regimes.count("DIVERGENCE") > 0:
                global_regime = "DIVERGENCE"
            else:
                global_regime = "UNKNOWN"

            return {
                "pairs": pairs_out,
                "global_regime": global_regime,
                "epochs_computed": self.epochs_computed,
                "tracked_assets": list(self.assets.keys()),
            }

    def _serialize_pair(self, pair: PairGravity) -> dict:
        return {
            "asset_a": pair.asset_a,
            "asset_b": pair.asset_b,
            "correlation": round(pair.correlation, 6),
            "g_force": round(pair.g_force, 4),
            "acceleration": round(pair.acceleration, 6),
            "regime": pair.regime,
            "last_compute": pair.last_compute,
        }

    async def signal_for_asset(self, symbol: str) -> dict:
        """Produce a NodeSignal-compatible output for a specific asset."""
        async with self._lock:
            sym = symbol.upper()
            # Find all pairs involving this asset
            relevant_pairs = [
                p for p in self.pairs.values()
                if p.asset_a == sym or p.asset_b == sym
            ]

            if not relevant_pairs:
                return {
                    "action": "WAIT",
                    "side": "NONE",
                    "confidence": 0.0,
                    "regime": "UNKNOWN",
                    "pairs": [],
                }

            regimes = [p.regime for p in relevant_pairs]
            avg_corr = np.mean([abs(p.correlation) for p in relevant_pairs]) if relevant_pairs else 0.0
            avg_accel = np.mean([p.acceleration for p in relevant_pairs]) if relevant_pairs else 0.0

            # Signal logic
            action = "WAIT"
            side = "NONE"
            confidence = 0.0

            if "CONTAGION" in regimes:
                # Risk-off: high correlation + accelerating = systemic risk
                action = "KILL"
                confidence = min(1.0, 0.6 + avg_corr * 0.3)
            elif all(r == "ISOLATION" for r in regimes):
                # Idiosyncratic; let other signals dominate
                action = "WAIT"
                confidence = 0.3
            elif "CONVERGENCE" in regimes:
                # Momentum regime: follow the trend
                # Use acceleration sign for direction hint
                if avg_accel > 0:
                    action = "EXECUTE"
                    side = "LONG"
                    confidence = min(1.0, 0.5 + avg_corr * 0.3 + abs(avg_accel) * 0.2)
                else:
                    action = "EXECUTE"
                    side = "SHORT"
                    confidence = min(1.0, 0.5 + avg_corr * 0.2)
            elif "DIVERGENCE" in regimes:
                # Decorrelation: mean-reversion potential
                action = "EXECUTE"
                side = "LONG" if avg_accel < 0 else "SHORT"  # Contrarian
                confidence = min(1.0, 0.45 + abs(avg_accel) * 0.3)

            if confidence < 0.40:
                action = "WAIT"
                side = "NONE"

            pair_data = [self._serialize_pair(p) for p in relevant_pairs]

            # Determine the primary pair's regime
            primary_regime = regimes[0] if regimes else "UNKNOWN"
            if "CONTAGION" in regimes:
                primary_regime = "CONTAGION"

            return {
                "action": action,
                "side": side,
                "confidence": round(confidence, 4),
                "regime": primary_regime,
                "avg_correlation": round(float(avg_corr), 4),
                "avg_acceleration": round(float(avg_accel), 6),
                "pairs": pair_data,
            }


# ────────────────────────────────────────────────────
# Data fetcher: Binance klines for returns
# ────────────────────────────────────────────────────
async def fetch_klines_returns(
    client: httpx.AsyncClient,
    symbol: str,
    interval: str = "5m",
    limit: int = 100,
) -> Tuple[List[float], float, float]:
    """Fetch klines from Binance Futures and compute log returns.

    Returns: (returns_list, last_close_price, volume_24h_usd)
    """
    url = f"{BINANCE_FAPI}/fapi/v1/klines"
    params = {"symbol": f"{symbol.upper()}USDT", "interval": interval, "limit": limit}
    try:
        r = await client.get(url, params=params, timeout=5.0)
        r.raise_for_status()
        klines = r.json()
        if not klines or len(klines) < 2:
            return [], 0.0, 0.0

        closes = [float(k[4]) for k in klines]
        volumes = [float(k[7]) for k in klines]  # quote volume

        returns = []
        for i in range(1, len(closes)):
            if closes[i - 1] > 0:
                returns.append(math.log(closes[i] / closes[i - 1]))

        last_price = closes[-1]
        vol_24h = sum(volumes[-288:]) if len(volumes) >= 288 else sum(volumes)  # approx 24h of 5m candles

        return returns, last_price, vol_24h
    except Exception as e:
        log.warning(f"klines fetch failed for {symbol}: {e}")
        return [], 0.0, 0.0


# ────────────────────────────────────────────────────
# Epoch loop
# ────────────────────────────────────────────────────
gravity = GravityEngine(TRACKED_ASSETS)
http_client: httpx.AsyncClient | None = None


async def epoch_loop(stop_event: asyncio.Event):
    """Periodically fetch data and recompute gravity vectors."""
    global http_client
    log.info(f"Newtonian epoch loop online: assets={TRACKED_ASSETS} interval={EPOCH_INTERVAL_S}s")

    while not stop_event.is_set():
        try:
            if http_client:
                # Fetch returns for all assets in parallel
                tasks = {
                    asset: fetch_klines_returns(http_client, asset, KLINE_INTERVAL, KLINE_LIMIT)
                    for asset in TRACKED_ASSETS
                }
                results = {}
                for asset, coro in tasks.items():
                    try:
                        results[asset] = await coro
                    except Exception as e:
                        log.warning(f"Failed fetching {asset}: {e}")
                        results[asset] = ([], 0.0, 0.0)

                # Ingest
                for asset, (rets, price, vol) in results.items():
                    if rets:
                        await gravity.ingest_returns(asset, rets, price, vol)

                # Compute epoch
                await gravity.compute_epoch()

                state = await gravity.get_all_state()
                log.info(
                    f"Epoch #{state['epochs_computed']} computed: "
                    f"regime={state['global_regime']} "
                    f"pairs={len(state['pairs'])}"
                )

        except Exception as e:
            log.error(f"Epoch loop error: {e}")

        # Sleep until next epoch
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=EPOCH_INTERVAL_S)
            break  # stop_event was set
        except asyncio.TimeoutError:
            pass  # normal: epoch interval elapsed


# ────────────────────────────────────────────────────
# FastAPI app
# ────────────────────────────────────────────────────
stop_event = asyncio.Event()
epoch_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client, epoch_task
    http_client = httpx.AsyncClient(headers={"User-Agent": "ApexNewtonian/3.0"})
    epoch_task = asyncio.create_task(epoch_loop(stop_event))
    yield
    stop_event.set()
    if epoch_task:
        epoch_task.cancel()
        try:
            await epoch_task
        except Exception:
            pass
    if http_client:
        await http_client.aclose()


app = FastAPI(title="Apex Newtonian Gravitational Model", version="3.0.0", lifespan=lifespan)
instrument_app(app)


@app.get("/health")
async def health():
    state = await gravity.get_all_state()
    return {
        "status": "ok",
        "service": "newtonian",
        "version": app.version,
        "tracked_assets": TRACKED_ASSETS,
        "epochs_computed": state["epochs_computed"],
        "global_regime": state["global_regime"],
        "epoch_interval_s": EPOCH_INTERVAL_S,
    }


@app.get("/gravity_state")
async def gravity_state_all():
    """Full gravity matrix + regime for all tracked pairs."""
    return await gravity.get_all_state()


@app.get("/gravity_state/{symbol}")
async def gravity_state_symbol(symbol: str):
    """NodeSignal-compatible output for a specific asset.

    The Master Orchestrator calls this endpoint.
    """
    return await gravity.signal_for_asset(symbol)


@app.get("/pair/{pair_key}")
async def pair_state(pair_key: str):
    """G-force for a specific pair (e.g., BTC_ETH)."""
    result = await gravity.get_pair_state(pair_key)
    if result is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"Pair {pair_key} not found")
    return result


class ComputeRequest(BaseModel):
    returns_a: List[float] = Field(..., min_length=2)
    returns_b: List[float] = Field(..., min_length=2)
    mass_a: float = Field(1.0, gt=0)
    mass_b: float = Field(1.0, gt=0)
    lookback: int = Field(60, ge=5, le=500)


@app.post("/compute_vectors")
async def compute_vectors(req: ComputeRequest):
    """On-demand gravity computation with custom returns arrays."""
    corr = rolling_correlation(req.returns_a, req.returns_b, req.lookback)
    g = compute_g_force(req.mass_a, req.mass_b, corr, G_CONSTANT)
    regime = classify_regime(corr, 0.0, ACCEL_THRESHOLD, CONTAGION_MULTIPLIER)

    return {
        "correlation": round(corr, 6),
        "g_force": round(g, 4),
        "distance": round(max(0.05, 1.0 - abs(corr)), 4),
        "regime": regime,
        "g_constant": G_CONSTANT,
    }
