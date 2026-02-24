# ===================================================================
# APEX NARRATIVE DIVERGENCE & HYBLOCK NODE v3.0 (Port 8004)
# NLP sentiment analysis (FinBERT) + Hyblock liquidation clusters
# Divergence detection: sentiment vs. market structure
# ===================================================================

from __future__ import annotations

import asyncio
import math
import os
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import httpx
import numpy as np
from dotenv import load_dotenv
from fastapi import FastAPI
from pydantic import BaseModel, Field

from apex_common.logging import get_logger
from apex_common.metrics import instrument_app
from apex_common.security import check_env_file_permissions

load_dotenv()
log = get_logger("narrative")
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


HYBLOCK_API_KEY = _env("HYBLOCK_API_KEY", "").strip()
HYBLOCK_BASE_URL = _env("HYBLOCK_BASE_URL", "https://api.hyblock.co/v1")
SENTIMENT_HALF_LIFE_H = _f("NARRATIVE_HALF_LIFE_H", 4.0)
SENTIMENT_POLL_S = _f("NARRATIVE_SENTIMENT_POLL_S", 120.0)
HYBLOCK_POLL_S = _f("NARRATIVE_HYBLOCK_POLL_S", 300.0)
DIVERGENCE_THRESHOLD = _f("NARRATIVE_DIVERGENCE_THRESHOLD", 0.3)

# Social data sources
TWITTER_BEARER = _env("TWITTER_BEARER", "").strip()
REDDIT_CLIENT_ID = _env("REDDIT_CLIENT_ID", "").strip()
REDDIT_CLIENT_SECRET = _env("REDDIT_CLIENT_SECRET", "").strip()

USE_FINBERT = os.getenv("NARRATIVE_USE_FINBERT", "FALSE").strip().upper() in ("1", "TRUE", "YES")


# ────────────────────────────────────────────────────
# Sentiment scoring
# ────────────────────────────────────────────────────
@dataclass
class SentimentSample:
    text: str
    score: float       # [-1, 1]
    source: str        # "twitter", "reddit", "telegram"
    volume: float      # engagement weight
    ts: float


class SentimentEngine:
    """Aggregates sentiment scores with exponential decay."""

    def __init__(self, half_life_h: float = 4.0):
        self._lock = asyncio.Lock()
        self._samples: Dict[str, deque] = {}  # symbol → samples
        self.half_life_s = half_life_h * 3600.0
        self._decay_lambda = math.log(2) / max(1.0, self.half_life_s)

        # Try loading FinBERT
        self._finbert_pipeline = None
        if USE_FINBERT:
            try:
                from transformers import pipeline
                self._finbert_pipeline = pipeline(
                    "sentiment-analysis",
                    model="ProsusAI/finbert",
                    truncation=True,
                    max_length=512,
                )
                log.info("FinBERT model loaded successfully")
            except Exception as e:
                log.warning(f"FinBERT unavailable, using keyword fallback: {e}")

    def score_text(self, text: str) -> float:
        """Score a text snippet: [-1 (bearish), 0 (neutral), +1 (bullish)]."""
        if self._finbert_pipeline:
            try:
                result = self._finbert_pipeline(text[:512])[0]
                label = result["label"].lower()
                conf = float(result["score"])
                if label == "positive":
                    return conf
                elif label == "negative":
                    return -conf
                return 0.0
            except Exception:
                pass

        # Keyword fallback (word boundary matching)
        import re
        t = text.lower()
        bull_words = ["bullish", "moon", "pump", "buy", "long", "breakout", "rally", "ath", "green"]
        bear_words = ["bearish", "dump", "crash", "sell", "short", "breakdown", "capitulation", "red"]
        bull = sum(1 for w in bull_words if re.search(rf'\b{w}\b', t))
        bear = sum(1 for w in bear_words if re.search(rf'\b{w}\b', t))
        total = bull + bear
        if total == 0:
            return 0.0
        return (bull - bear) / total

    async def add_sample(self, symbol: str, sample: SentimentSample):
        async with self._lock:
            sym = symbol.upper()
            if sym not in self._samples:
                self._samples[sym] = deque(maxlen=1000)
            self._samples[sym].append(sample)

    async def add_batch(self, symbol: str, texts: List[dict]):
        """Batch-score and add samples. Each dict: {text, source, volume?}."""
        for item in texts:
            score = self.score_text(item.get("text", ""))
            sample = SentimentSample(
                text=item.get("text", "")[:200],
                score=score,
                source=item.get("source", "unknown"),
                volume=float(item.get("volume", 1.0)),
                ts=time.time(),
            )
            await self.add_sample(symbol, sample)

    async def get_aggregate(self, symbol: str) -> dict:
        """Volume-weighted exponentially-decayed sentiment aggregate."""
        async with self._lock:
            sym = symbol.upper()
            samples = list(self._samples.get(sym, []))

        if not samples:
            return {
                "sentiment_score": 0.0,
                "sentiment_volume": 0.0,
                "sample_count": 0,
                "sources": {},
            }

        now = time.time()
        weighted_sum = 0.0
        weight_total = 0.0
        source_counts: Dict[str, int] = {}

        for s in samples:
            age_s = max(0.0, now - s.ts)
            decay = math.exp(-self._decay_lambda * age_s)
            w = s.volume * decay
            weighted_sum += s.score * w
            weight_total += w
            source_counts[s.source] = source_counts.get(s.source, 0) + 1

        avg_score = weighted_sum / max(weight_total, 1e-9)

        return {
            "sentiment_score": round(max(-1.0, min(1.0, avg_score)), 4),
            "sentiment_volume": round(weight_total, 2),
            "sample_count": len(samples),
            "sources": source_counts,
        }


# ────────────────────────────────────────────────────
# Hyblock liquidation clusters
# ────────────────────────────────────────────────────
@dataclass
class LiquidationCluster:
    price: float
    side: str          # "long" or "short"
    volume_usd: float
    leverage: float


@dataclass
class HyblockState:
    symbol: str
    long_clusters: List[LiquidationCluster] = field(default_factory=list)
    short_clusters: List[LiquidationCluster] = field(default_factory=list)
    nearest_long_dist_pct: float = 0.0
    nearest_short_dist_pct: float = 0.0
    cluster_imbalance: float = 0.0  # >0 = more short liq above, <0 = more long liq below
    ts: float = 0.0


class HyblockEngine:
    """Manages Hyblock liquidation cluster data."""

    def __init__(self):
        self._lock = asyncio.Lock()
        self._states: Dict[str, HyblockState] = {}

    async def update(self, symbol: str, state: HyblockState):
        async with self._lock:
            self._states[symbol.upper()] = state

    async def get_state(self, symbol: str) -> Optional[HyblockState]:
        async with self._lock:
            return self._states.get(symbol.upper())

    async def get_signal_data(self, symbol: str) -> dict:
        async with self._lock:
            s = self._states.get(symbol.upper())
            if not s:
                return {
                    "nearest_long_liq_cluster": 0.0,
                    "nearest_short_liq_cluster": 0.0,
                    "cluster_imbalance": 0.0,
                    "long_cluster_count": 0,
                    "short_cluster_count": 0,
                }
            return {
                "nearest_long_liq_cluster": s.nearest_long_dist_pct,
                "nearest_short_liq_cluster": s.nearest_short_dist_pct,
                "cluster_imbalance": s.cluster_imbalance,
                "long_cluster_count": len(s.long_clusters),
                "short_cluster_count": len(s.short_clusters),
            }


# ────────────────────────────────────────────────────
# Divergence detection
# ────────────────────────────────────────────────────
def compute_divergence(
    sentiment_score: float,
    market_direction: float,  # e.g., from Newtonian or SpoofHunter
) -> dict:
    """Detect when sentiment diverges from market structure.

    Returns divergence direction and magnitude.
    """
    diff = sentiment_score - market_direction
    magnitude = abs(diff)

    if magnitude < DIVERGENCE_THRESHOLD:
        return {
            "divergence": False,
            "direction": "ALIGNED",
            "magnitude": round(magnitude, 4),
        }

    if sentiment_score > 0 and market_direction < 0:
        direction = "BULLISH_DIVERGENCE"  # Sentiment bullish, market bearish
    elif sentiment_score < 0 and market_direction > 0:
        direction = "BEARISH_DIVERGENCE"  # Sentiment bearish, market bullish
    else:
        direction = "ALIGNED"

    return {
        "divergence": magnitude >= DIVERGENCE_THRESHOLD,
        "direction": direction,
        "magnitude": round(magnitude, 4),
    }


# ────────────────────────────────────────────────────
# Pollers
# ────────────────────────────────────────────────────
sentiment_engine = SentimentEngine(SENTIMENT_HALF_LIFE_H)
hyblock_engine = HyblockEngine()
http_client: httpx.AsyncClient | None = None


async def poll_cryptopanic(stop: asyncio.Event):
    """Poll CryptoPanic for news sentiment (FREE, optional API key for more volume).

    CryptoPanic is superior to Reddit because:
    - Pre-filtered crypto news (not random memes)
    - Has community vote sentiment (positive/negative)
    - Covers multiple sources aggregated
    - Free tier: ~50 req/hour (more than enough)
    """
    CRYPTOPANIC_KEY = os.getenv("CRYPTOPANIC_API_KEY", "").strip()
    CURRENCIES = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT"}

    while not stop.is_set():
        try:
            if http_client:
                for currency, sym in CURRENCIES.items():
                    try:
                        params = {
                            "currencies": currency,
                            "filter": "important",
                            "public": "true",
                        }
                        if CRYPTOPANIC_KEY:
                            params["auth_token"] = CRYPTOPANIC_KEY

                        r = await http_client.get(
                            "https://cryptopanic.com/api/free/v1/posts/",
                            params=params,
                            timeout=8.0,
                            headers={"User-Agent": "ApexNarrative/3.0"},
                        )
                        if r.status_code == 200:
                            results = r.json().get("results", [])
                            texts = []
                            for post in results[:15]:
                                votes = post.get("votes", {})
                                pos = votes.get("positive", 0)
                                neg = votes.get("negative", 0)
                                # Use vote ratio as volume weight
                                vol = max(1, pos + neg)
                                texts.append({
                                    "text": post.get("title", ""),
                                    "source": f"cryptopanic/{post.get('source', {}).get('title', 'unknown')}",
                                    "volume": vol,
                                })
                            if texts:
                                await sentiment_engine.add_batch(sym, texts)
                                log.debug(f"CryptoPanic: {len(texts)} items for {sym}")
                    except Exception as e:
                        log.warning(f"CryptoPanic poll {currency}: {e}")
                    await asyncio.sleep(3.0)  # Respect rate limit
        except Exception as e:
            log.error(f"CryptoPanic poller error: {e}")
        try:
            await asyncio.wait_for(stop.wait(), timeout=SENTIMENT_POLL_S)
            break
        except asyncio.TimeoutError:
            pass


async def poll_reddit(stop: asyncio.Event):
    """Poll Reddit crypto subreddits (FALLBACK, free, rate-limited)."""
    SUBREDDITS = ["cryptocurrency", "bitcoin", "ethtrader", "solana"]
    while not stop.is_set():
        try:
            if http_client:
                for sub in SUBREDDITS:
                    try:
                        r = await http_client.get(
                            f"https://www.reddit.com/r/{sub}/hot.json?limit=25",
                            timeout=10.0,
                            headers={"User-Agent": "ApexNarrative/3.0"},
                        )
                        if r.status_code == 200:
                            data = r.json()
                            posts = data.get("data", {}).get("children", [])
                            texts = []
                            for post in posts:
                                pd = post.get("data", {})
                                title = pd.get("title", "")
                                score = pd.get("score", 1)
                                texts.append({
                                    "text": title,
                                    "source": f"reddit/{sub}",
                                    "volume": max(1, score),
                                })

                            # Map subreddit to symbol
                            sym_map = {"bitcoin": "BTCUSDT", "ethtrader": "ETHUSDT", "solana": "SOLUSDT"}
                            sym = sym_map.get(sub, "BTCUSDT")
                            if texts:
                                await sentiment_engine.add_batch(sym, texts)
                    except Exception as e:
                        log.warning(f"Reddit poll {sub}: {e}")
        except Exception as e:
            log.error(f"Reddit poller error: {e}")
        try:
            await asyncio.wait_for(stop.wait(), timeout=SENTIMENT_POLL_S)
            break
        except asyncio.TimeoutError:
            pass


async def poll_hyblock(stop: asyncio.Event):
    """Poll Hyblock API for liquidation clusters."""
    while not stop.is_set():
        try:
            if http_client and HYBLOCK_API_KEY:
                symbols = ["BTCUSDT", "ETHUSDT"]
                for sym in symbols:
                    try:
                        r = await http_client.get(
                            f"{HYBLOCK_BASE_URL}/liquidation/heatmap",
                            params={"symbol": sym, "exchange": "binance"},
                            headers={"Authorization": f"Bearer {HYBLOCK_API_KEY}"},
                            timeout=10.0,
                        )
                        if r.status_code == 200:
                            data = r.json()
                            # Parse clusters from Hyblock response
                            longs = []
                            shorts = []
                            for cluster in data.get("clusters", []):
                                c = LiquidationCluster(
                                    price=float(cluster.get("price", 0)),
                                    side=cluster.get("side", "long"),
                                    volume_usd=float(cluster.get("volume_usd", 0)),
                                    leverage=float(cluster.get("leverage", 1)),
                                )
                                if c.side == "long":
                                    longs.append(c)
                                else:
                                    shorts.append(c)

                            # Compute distances (need current price)
                            current_price = float(data.get("current_price", 0))
                            nearest_long = 0.0
                            nearest_short = 0.0
                            if current_price > 0:
                                if longs:
                                    nearest_long = min(abs(c.price - current_price) / current_price * 100 for c in longs)
                                if shorts:
                                    nearest_short = min(abs(c.price - current_price) / current_price * 100 for c in shorts)

                            # Imbalance: ratio of short vs long liquidation volume
                            long_vol = sum(c.volume_usd for c in longs)
                            short_vol = sum(c.volume_usd for c in shorts)
                            total_vol = long_vol + short_vol
                            imbalance = (short_vol - long_vol) / max(total_vol, 1.0)

                            await hyblock_engine.update(sym, HyblockState(
                                symbol=sym,
                                long_clusters=sorted(longs, key=lambda x: x.volume_usd, reverse=True)[:10],
                                short_clusters=sorted(shorts, key=lambda x: x.volume_usd, reverse=True)[:10],
                                nearest_long_dist_pct=nearest_long,
                                nearest_short_dist_pct=nearest_short,
                                cluster_imbalance=imbalance,
                                ts=time.time(),
                            ))
                    except Exception as e:
                        log.warning(f"Hyblock poll {sym}: {e}")
        except Exception as e:
            log.error(f"Hyblock poller error: {e}")
        try:
            await asyncio.wait_for(stop.wait(), timeout=HYBLOCK_POLL_S)
            break
        except asyncio.TimeoutError:
            pass


# ────────────────────────────────────────────────────
# FastAPI
# ────────────────────────────────────────────────────
stop_event = asyncio.Event()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client
    http_client = httpx.AsyncClient(headers={"User-Agent": "ApexNarrative/3.0"})
    tasks = [
        asyncio.create_task(poll_cryptopanic(stop_event)),  # Primary: free, pre-filtered
        asyncio.create_task(poll_reddit(stop_event)),       # Fallback: free, noisy
        asyncio.create_task(poll_hyblock(stop_event)),
    ]
    log.info("Narrative Divergence node online (CryptoPanic + Reddit + Hyblock)")
    yield
    stop_event.set()
    for t in tasks:
        t.cancel()
    if http_client:
        await http_client.aclose()


app = FastAPI(title="Apex Narrative Divergence & Hyblock", version="3.0.0", lifespan=lifespan)
instrument_app(app)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "narrative",
        "version": app.version,
        "finbert_loaded": sentiment_engine._finbert_pipeline is not None,
        "hyblock_active": bool(HYBLOCK_API_KEY),
    }


@app.get("/sentiment_state/{symbol}")
async def sentiment_state(symbol: str):
    """NodeSignal-compatible output for the Master Orchestrator."""
    sym = symbol.upper().replace("/", "").replace(":", "")
    sent = await sentiment_engine.get_aggregate(sym)
    hyblock = await hyblock_engine.get_signal_data(sym)

    score = sent["sentiment_score"]
    volume = sent["sentiment_volume"]

    # Divergence: compare sentiment with cluster imbalance
    div = compute_divergence(score, hyblock["cluster_imbalance"])

    # Signal logic
    action = "WAIT"
    side = "NONE"
    confidence = 0.0

    if div["divergence"] and div["direction"] == "BULLISH_DIVERGENCE":
        action = "EXECUTE"
        side = "LONG"
        confidence = min(1.0, 0.4 + div["magnitude"] * 0.5 + min(volume / 100.0, 0.3))
    elif div["divergence"] and div["direction"] == "BEARISH_DIVERGENCE":
        action = "EXECUTE"
        side = "SHORT"
        confidence = min(1.0, 0.4 + div["magnitude"] * 0.5 + min(volume / 100.0, 0.3))
    elif abs(score) > 0.4 and volume > 10:
        # Strong aligned sentiment
        action = "EXECUTE"
        side = "LONG" if score > 0 else "SHORT"
        confidence = min(1.0, abs(score) * 0.6 + min(volume / 100.0, 0.2))

    if confidence < 0.40:
        action = "WAIT"
        side = "NONE"

    return {
        "action": action,
        "side": side,
        "confidence": round(confidence, 4),
        "sentiment_score": score,
        "sentiment_volume": volume,
        "narrative_divergence": div,
        **hyblock,
    }


@app.get("/sentiment/{symbol}")
async def sentiment_detail(symbol: str):
    """Raw sentiment aggregate (no signal computation)."""
    return await sentiment_engine.get_aggregate(symbol.upper().replace("/", "").replace(":", ""))


@app.get("/hyblock/{symbol}")
async def hyblock_detail(symbol: str):
    """Raw Hyblock liquidation cluster data."""
    return await hyblock_engine.get_signal_data(symbol.upper().replace("/", "").replace(":", ""))


class IngestRequest(BaseModel):
    symbol: str
    texts: List[dict] = Field(..., description="List of {text, source, volume?}")


@app.post("/ingest_sentiment")
async def ingest_sentiment(req: IngestRequest):
    """Manual sentiment ingestion endpoint."""
    await sentiment_engine.add_batch(req.symbol, req.texts)
    return {"status": "ok", "ingested": len(req.texts)}
