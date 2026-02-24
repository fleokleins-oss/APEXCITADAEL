# ===================================================================
# APEX SPOOFHUNTER L2 TELEMETRY v3.0 (Port 8002)
# Ghost wall detection, micro-price model, iceberg patterns
# Multi-exchange L2 order book monitoring
# ===================================================================

from __future__ import annotations

import asyncio
import json
import math
import os
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple

import websockets
from dotenv import load_dotenv
from fastapi import FastAPI

from apex_common.logging import get_logger
from apex_common.metrics import instrument_app
from apex_common.security import check_env_file_permissions

load_dotenv()
log = get_logger("spoofhunter")
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


GHOST_MIN_NOTIONAL_USD = _f("SPOOF_GHOST_MIN_NOTIONAL_USD", 50000.0)
GHOST_MAX_LIFETIME_S = _f("SPOOF_GHOST_MAX_LIFETIME_S", 8.0)
GHOST_PROXIMITY_BPS = _f("SPOOF_GHOST_PROXIMITY_BPS", 15.0)
SNAPSHOT_BUFFER_SIZE = _i("SPOOF_SNAPSHOT_BUFFER_SIZE", 300)
DEPTH_STREAM = _env("SPOOF_DEPTH_STREAM", "depth20@100ms")
DEFAULT_SYMBOL = _env("SPOOF_DEFAULT_SYMBOL", "btcusdt")
RECONNECT_BASE_DELAY = _f("SPOOF_RECONNECT_BASE_DELAY", 1.0)
RECONNECT_MAX_DELAY = _f("SPOOF_RECONNECT_MAX_DELAY", 20.0)
GHOST_WINDOW_S = _f("SPOOF_GHOST_WINDOW_S", 30.0)
ICEBERG_REFILL_COUNT = _i("SPOOF_ICEBERG_REFILL_COUNT", 3)
ICEBERG_SIZE_TOLERANCE = _f("SPOOF_ICEBERG_SIZE_TOLERANCE", 0.15)


# ────────────────────────────────────────────────────
# Data structures
# ────────────────────────────────────────────────────
class WallState(str, Enum):
    APPEARED = "APPEARED"
    ACTIVE = "ACTIVE"
    PULLED = "PULLED"
    FILLED = "FILLED"
    DECAYED = "DECAYED"


@dataclass
class WallTracker:
    """Tracks lifecycle of a single large limit order (potential ghost wall)."""
    wall_id: str
    side: str                   # "bid" or "ask"
    price: float
    initial_qty: float
    initial_notional_usd: float
    first_seen: float           # monotonic time
    last_seen: float
    current_qty: float
    state: WallState = WallState.APPEARED
    mid_price_at_appear: float = 0.0

    @property
    def age_s(self) -> float:
        return time.monotonic() - self.first_seen

    @property
    def reduction_pct(self) -> float:
        if self.initial_qty <= 0:
            return 0.0
        return 1.0 - (self.current_qty / self.initial_qty)

    def distance_bps(self, mid: float) -> float:
        if mid <= 0:
            return 0.0
        return abs(self.price - mid) / mid * 10000.0


@dataclass
class IcebergTracker:
    """Tracks potential iceberg orders at BBO that refill repeatedly."""
    price: float
    side: str
    refill_count: int = 0
    last_qty: float = 0.0
    last_seen: float = 0.0

    @property
    def is_iceberg(self) -> bool:
        return self.refill_count >= ICEBERG_REFILL_COUNT


@dataclass
class GhostWallEvent:
    """A confirmed ghost wall detection event."""
    wall_id: str
    side: str
    price: float
    notional_usd: float
    lifetime_s: float
    distance_bps: float
    ts: float


# ────────────────────────────────────────────────────
# Core engine
# ────────────────────────────────────────────────────
class SpoofEngine:
    """Processes L2 depth snapshots to detect ghost walls and compute microstructure signals."""

    def __init__(self):
        self._lock = asyncio.Lock()

        # Micro-price state
        self.mid_price: float = 0.0
        self.micro_price: float = 0.0
        self.micro_shift: float = 0.0
        self.imbalance: float = 0.0

        # Wall tracking
        self._active_walls: Dict[str, WallTracker] = {}
        self._ghost_events: deque[GhostWallEvent] = deque(maxlen=200)

        # Iceberg tracking
        self._bid_iceberg: Optional[IcebergTracker] = None
        self._ask_iceberg: Optional[IcebergTracker] = None

        # Stats
        self.snapshots_processed: int = 0
        self.last_snapshot_ts: float = 0.0

    def _wall_key(self, side: str, price: float) -> str:
        return f"{side}:{price:.8f}"

    async def process_depth(
        self,
        bids: List[List],
        asks: List[List],
        mark_price: float = 0.0,
    ):
        """Process an L2 depth snapshot.

        bids/asks: [[price, qty], ...] sorted by proximity to mid.
        mark_price: optional external mark price for notional calculations.
        """
        if not bids or not asks:
            return

        async with self._lock:
            now = time.monotonic()
            self.snapshots_processed += 1
            self.last_snapshot_ts = now

            # ── 1. Micro-price computation ──
            try:
                best_bid_px = float(bids[0][0])
                best_bid_sz = float(bids[0][1])
                best_ask_px = float(asks[0][0])
                best_ask_sz = float(asks[0][1])

                self.mid_price = 0.5 * (best_bid_px + best_ask_px)
                denom = max(best_bid_sz + best_ask_sz, 1e-12)
                self.micro_price = (
                    best_ask_px * best_bid_sz + best_bid_px * best_ask_sz
                ) / denom
                self.micro_shift = self.micro_price - self.mid_price
                self.imbalance = (best_bid_sz - best_ask_sz) / denom
            except (IndexError, ValueError, TypeError):
                return

            ref_price = mark_price if mark_price > 0 else self.mid_price

            # ── 2. Iceberg detection at BBO ──
            self._check_iceberg("bid", best_bid_px, best_bid_sz, now)
            self._check_iceberg("ask", best_ask_px, best_ask_sz, now)

            # ── 3. Wall tracking ──
            seen_keys: set = set()

            # Scan bids for large walls
            for level in bids:
                try:
                    px = float(level[0])
                    qty = float(level[1])
                except (IndexError, ValueError, TypeError):
                    continue
                notional = qty * ref_price
                key = self._wall_key("bid", px)
                seen_keys.add(key)

                dist_bps = abs(px - self.mid_price) / max(self.mid_price, 1e-12) * 10000.0
                if notional >= GHOST_MIN_NOTIONAL_USD and dist_bps <= GHOST_PROXIMITY_BPS:
                    if key not in self._active_walls:
                        self._active_walls[key] = WallTracker(
                            wall_id=uuid.uuid4().hex[:8],
                            side="bid",
                            price=px,
                            initial_qty=qty,
                            initial_notional_usd=notional,
                            first_seen=now,
                            last_seen=now,
                            current_qty=qty,
                            mid_price_at_appear=self.mid_price,
                        )
                    else:
                        w = self._active_walls[key]
                        w.last_seen = now
                        w.current_qty = qty
                        if w.state == WallState.APPEARED:
                            w.state = WallState.ACTIVE

            # Scan asks
            for level in asks:
                try:
                    px = float(level[0])
                    qty = float(level[1])
                except (IndexError, ValueError, TypeError):
                    continue
                notional = qty * ref_price
                key = self._wall_key("ask", px)
                seen_keys.add(key)

                dist_bps = abs(px - self.mid_price) / max(self.mid_price, 1e-12) * 10000.0
                if notional >= GHOST_MIN_NOTIONAL_USD and dist_bps <= GHOST_PROXIMITY_BPS:
                    if key not in self._active_walls:
                        self._active_walls[key] = WallTracker(
                            wall_id=uuid.uuid4().hex[:8],
                            side="ask",
                            price=px,
                            initial_qty=qty,
                            initial_notional_usd=notional,
                            first_seen=now,
                            last_seen=now,
                            current_qty=qty,
                            mid_price_at_appear=self.mid_price,
                        )
                    else:
                        w = self._active_walls[key]
                        w.last_seen = now
                        w.current_qty = qty
                        if w.state == WallState.APPEARED:
                            w.state = WallState.ACTIVE

            # ── 4. Classify disappeared / pulled walls ──
            to_remove: List[str] = []
            for key, w in self._active_walls.items():
                if key not in seen_keys:
                    # Wall disappeared from book
                    if w.age_s <= GHOST_MAX_LIFETIME_S:
                        # Short-lived + pulled = GHOST WALL
                        w.state = WallState.PULLED
                        self._ghost_events.append(GhostWallEvent(
                            wall_id=w.wall_id,
                            side=w.side,
                            price=w.price,
                            notional_usd=w.initial_notional_usd,
                            lifetime_s=w.age_s,
                            distance_bps=w.distance_bps(self.mid_price),
                            ts=now,
                        ))
                    else:
                        w.state = WallState.DECAYED
                    to_remove.append(key)
                elif w.reduction_pct > 0.80:
                    # Still in book but >80% consumed = likely filled, not spoofed
                    w.state = WallState.FILLED
                    to_remove.append(key)
                elif w.age_s > GHOST_MAX_LIFETIME_S * 5:
                    # Survived too long; it's a real wall, stop tracking
                    to_remove.append(key)

            for key in to_remove:
                del self._active_walls[key]

    def _check_iceberg(self, side: str, price: float, qty: float, now: float):
        tracker = self._bid_iceberg if side == "bid" else self._ask_iceberg

        if tracker is None or abs(tracker.price - price) > 1e-12:
            # New price level at BBO
            new_tracker = IcebergTracker(price=price, side=side, refill_count=0, last_qty=qty, last_seen=now)
            if side == "bid":
                self._bid_iceberg = new_tracker
            else:
                self._ask_iceberg = new_tracker
            return

        # Same price: check if qty "refilled" after partial consumption
        if qty > tracker.last_qty * (1.0 - ICEBERG_SIZE_TOLERANCE) and tracker.last_qty > 0:
            # Qty stayed stable or increased after being partially eaten → refill
            if now - tracker.last_seen < 2.0:  # must be within 2s
                tracker.refill_count += 1
        tracker.last_qty = qty
        tracker.last_seen = now

    async def get_recent_ghosts(self, window_s: float = None) -> List[GhostWallEvent]:
        window = window_s or GHOST_WINDOW_S
        cutoff = time.monotonic() - window
        async with self._lock:
            return [g for g in self._ghost_events if g.ts >= cutoff]

    async def snapshot(self) -> dict:
        async with self._lock:
            ghosts = [g for g in self._ghost_events if g.ts >= time.monotonic() - GHOST_WINDOW_S]
            bid_ghosts = [g for g in ghosts if g.side == "bid"]
            ask_ghosts = [g for g in ghosts if g.side == "ask"]

            total_ghost_notional = sum(g.notional_usd for g in ghosts)

            # Ghost wall side: spoofing bids = fake support = bearish; spoofing asks = fake resistance = bullish
            bid_notional = sum(g.notional_usd for g in bid_ghosts)
            ask_notional = sum(g.notional_usd for g in ask_ghosts)

            if bid_notional > ask_notional * 1.5:
                ghost_side = "SHORT"  # Fake bids = bearish signal (contrarian)
            elif ask_notional > bid_notional * 1.5:
                ghost_side = "LONG"   # Fake asks = bullish signal (contrarian)
            else:
                ghost_side = "NONE"

            # Intensity classification
            if total_ghost_notional > GHOST_MIN_NOTIONAL_USD * 5:
                intensity = "HIGH"
            elif total_ghost_notional > GHOST_MIN_NOTIONAL_USD * 2:
                intensity = "MED"
            else:
                intensity = "LOW"

            iceberg_detected = False
            if self._bid_iceberg and self._bid_iceberg.is_iceberg:
                iceberg_detected = True
            if self._ask_iceberg and self._ask_iceberg.is_iceberg:
                iceberg_detected = True

            return {
                "mid_price": self.mid_price,
                "micro_price": self.micro_price,
                "micro_price_shift": self.micro_shift,
                "orderbook_imbalance": self.imbalance,
                "ghost_walls_detected": len(ghosts),
                "ghost_wall_side": ghost_side,
                "ghost_wall_intensity": intensity,
                "ghost_bid_count": len(bid_ghosts),
                "ghost_ask_count": len(ask_ghosts),
                "ghost_total_notional_usd": round(total_ghost_notional, 2),
                "iceberg_detected": iceberg_detected,
                "active_wall_trackers": len(self._active_walls),
                "snapshots_processed": self.snapshots_processed,
            }

    async def signal(self) -> dict:
        """Produce a NodeSignal-compatible output for the Master Orchestrator."""
        snap = await self.snapshot()

        ghosts = snap["ghost_walls_detected"]
        ghost_side = snap["ghost_wall_side"]
        intensity = snap["ghost_wall_intensity"]
        imbalance = snap["orderbook_imbalance"]

        # Signal logic: combine ghost wall contrarian signal with microstructure
        action = "WAIT"
        side = "NONE"
        confidence = 0.0

        # Ghost walls provide a contrarian signal
        if ghosts > 0 and ghost_side != "NONE":
            ghost_conf = min(1.0, 0.3 + ghosts * 0.1)
            if intensity == "HIGH":
                ghost_conf = min(1.0, ghost_conf + 0.2)
            elif intensity == "MED":
                ghost_conf = min(1.0, ghost_conf + 0.1)

            # Blend with microstructure imbalance
            micro_conf = abs(imbalance) * 0.3

            # If ghost side aligns with imbalance direction, stronger signal
            imbalance_side = "LONG" if imbalance > 0.05 else "SHORT" if imbalance < -0.05 else "NONE"
            if imbalance_side == ghost_side:
                confidence = min(1.0, ghost_conf + micro_conf + 0.1)
            else:
                confidence = min(1.0, ghost_conf + micro_conf * 0.5)

            if confidence >= 0.45:
                action = "EXECUTE"
                side = ghost_side

        # Even without ghosts, strong imbalance can produce a weak signal
        elif abs(imbalance) > 0.25:
            action = "EXECUTE"
            side = "LONG" if imbalance > 0 else "SHORT"
            confidence = min(0.65, abs(imbalance) * 0.8)

        return {
            **snap,
            "action": action,
            "side": side,
            "confidence": round(confidence, 4),
        }


# ────────────────────────────────────────────────────
# WebSocket ingestion
# ────────────────────────────────────────────────────
engines: Dict[str, SpoofEngine] = {}


def get_engine(symbol: str) -> SpoofEngine:
    s = symbol.lower().replace("/", "").replace(":", "")
    if s not in engines:
        engines[s] = SpoofEngine()
    return engines[s]


async def stream_binance_depth(symbol: str, stop_event: asyncio.Event):
    """Connect to Binance Futures depth + markPrice streams."""
    sym = symbol.lower()
    streams = f"{sym}@{DEPTH_STREAM}/{sym}@markPrice@1s"
    url = f"wss://fstream.binance.com/stream?streams={streams}"

    engine = get_engine(sym)
    delay = RECONNECT_BASE_DELAY
    mark_price = 0.0

    log.info(f"SpoofHunter online: {sym.upper()} (depth + mark)")

    while not stop_event.is_set():
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20, close_timeout=5) as ws:
                delay = RECONNECT_BASE_DELAY
                while not stop_event.is_set():
                    msg = await ws.recv()
                    try:
                        data = json.loads(msg)
                    except Exception:
                        continue

                    stream_name = data.get("stream", "")
                    payload = data.get("data", {})

                    if "@markPrice" in stream_name:
                        mark_price = float(payload.get("p", mark_price) or mark_price)
                    elif "@depth" in stream_name:
                        await engine.process_depth(
                            payload.get("b", []),
                            payload.get("a", []),
                            mark_price=mark_price,
                        )
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.warning(f"WS reconnecting in {delay:.1f}s: {e}")
            await asyncio.sleep(delay)
            delay = min(RECONNECT_MAX_DELAY, delay * 1.7)


# ────────────────────────────────────────────────────
# FastAPI app
# ────────────────────────────────────────────────────
stop_event = asyncio.Event()
ws_tasks: List[asyncio.Task] = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    global ws_tasks
    # Start depth streams for default symbol
    symbols = [s.strip().lower() for s in _env("SPOOF_SYMBOLS", DEFAULT_SYMBOL).split(",") if s.strip()]
    for sym in symbols:
        task = asyncio.create_task(stream_binance_depth(sym, stop_event))
        ws_tasks.append(task)
    yield
    stop_event.set()
    for t in ws_tasks:
        t.cancel()
    for t in ws_tasks:
        try:
            await t
        except Exception:
            pass


app = FastAPI(title="Apex SpoofHunter L2 Telemetry", version="3.0.0", lifespan=lifespan)
instrument_app(app)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "spoofhunter",
        "version": app.version,
        "engines": list(engines.keys()),
        "config": {
            "ghost_min_notional_usd": GHOST_MIN_NOTIONAL_USD,
            "ghost_max_lifetime_s": GHOST_MAX_LIFETIME_S,
            "ghost_proximity_bps": GHOST_PROXIMITY_BPS,
            "ghost_window_s": GHOST_WINDOW_S,
        },
    }


@app.get("/spoof_state/{symbol}")
async def spoof_state(symbol: str):
    """Full signal output for the Master Orchestrator."""
    engine = get_engine(symbol)
    return await engine.signal()


@app.get("/snapshot/{symbol}")
async def snapshot(symbol: str):
    """Raw snapshot without signal computation."""
    engine = get_engine(symbol)
    return await engine.snapshot()


@app.get("/ghost_events/{symbol}")
async def ghost_events(symbol: str, window_s: float = 30.0):
    """Recent ghost wall events."""
    engine = get_engine(symbol)
    ghosts = await engine.get_recent_ghosts(window_s)
    return {
        "symbol": symbol.upper(),
        "window_s": window_s,
        "count": len(ghosts),
        "events": [
            {
                "wall_id": g.wall_id,
                "side": g.side,
                "price": g.price,
                "notional_usd": round(g.notional_usd, 2),
                "lifetime_s": round(g.lifetime_s, 3),
                "distance_bps": round(g.distance_bps, 2),
            }
            for g in ghosts
        ],
    }
