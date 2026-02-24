# ===================================================================
# APEX ACTIVE POSITION MANAGER (APM) v3.0
# The 4 Weapons of HFT Scalping:
#   1. VPIN — Volume-Synchronized Probability of Informed Trading
#   2. Dynamic OBI Trailing — Rubber-band stops from live order book
#   3. Ghost Liquidity Reaction — Spoof detection → instant exit
#   4. Alpha Decay — Time-based kill for stale setups
#
# This module watches the L2 book and exits BEFORE price moves against
# you. It does NOT decide entries — it only manages active positions.
# ===================================================================

from __future__ import annotations

import asyncio
import math
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Literal, Optional, Tuple

from apex_common.logging import get_logger

log = get_logger("apm")


# ────────────────────────────────────────────────────
# Configuration defaults (overridable per-position)
# ────────────────────────────────────────────────────
VPIN_BUCKET_COUNT = 50          # number of volume buckets
VPIN_TOXIC_THRESHOLD = 0.70     # >0.7 = probable informed trading
VPIN_CRITICAL_THRESHOLD = 0.85  # >0.85 = guaranteed dump
OBI_WIDE_THRESHOLD = 0.60       # OBI > 0.6 = widen trail (room to pump)
OBI_TIGHT_THRESHOLD = -0.30     # OBI < -0.3 = snap trail tight
OBI_TRAIL_WIDE_ATR_MULT = 3.5   # wide trail = 3.5× ATR
OBI_TRAIL_TIGHT_ATR_MULT = 0.8  # tight trail = 0.8× ATR
OBI_TRAIL_DEFAULT_ATR_MULT = 2.0
GHOST_EXIT_CONFIDENCE = 0.60    # exit if ghost wall confidence > 60%
ALPHA_DECAY_S = 180.0           # 3 minutes of no-pump = alpha dead
ALPHA_MIN_MOVE_PCT = 0.5        # must move ≥ 0.5% within decay window
TICK_INTERVAL_MS = 100          # L2 refresh rate target


# ────────────────────────────────────────────────────
# WEAPON 1: VPIN (Volume-Synchronized Probability
#   of Informed Trading)
# ────────────────────────────────────────────────────
class VPINComputer:
    """Computes VPIN from trade-level or L2-inferred volume data.

    Instead of time-bars, VPIN uses volume-bars (buckets of equal volume).
    Each bucket classifies net buying vs selling pressure using the
    Bulk Volume Classification (BVC) method:

      V_buy  = V_total × Φ( ΔP / σ_ΔP )
      V_sell = V_total - V_buy

    where Φ is the standard normal CDF and ΔP is the price change
    within the bucket.

    VPIN = Σ|V_buy - V_sell| / (n × V_bucket)
    """

    def __init__(self, n_buckets: int = VPIN_BUCKET_COUNT, bucket_volume: float = 0.0):
        self.n_buckets = n_buckets
        self.bucket_volume = bucket_volume  # 0 = auto-calibrate
        self._buckets: deque[float] = deque(maxlen=n_buckets)
        # Accumulator for current bucket
        self._current_buy: float = 0.0
        self._current_sell: float = 0.0
        self._current_vol: float = 0.0
        self._prices: deque[float] = deque(maxlen=500)
        self._volumes: deque[float] = deque(maxlen=500)
        self._vpin: float = 0.0
        self._total_volume_seen: float = 0.0
        self._calibrated: bool = False

    @staticmethod
    def _phi(x: float) -> float:
        """Approximate standard normal CDF (Abramowitz & Stegun)."""
        if x < -6:
            return 0.0
        if x > 6:
            return 1.0
        a1, a2, a3 = 0.4361836, -0.1201676, 0.9372980
        t = 1.0 / (1.0 + 0.33267 * abs(x))
        phi = 1.0 - (1.0 / math.sqrt(2 * math.pi)) * math.exp(-x * x / 2) * (
            a1 * t + a2 * t * t + a3 * t * t * t
        )
        return phi if x >= 0 else 1.0 - phi

    def ingest_trade(self, price: float, volume: float):
        """Feed a single trade (or L2-inferred trade) into the VPIN engine."""
        self._prices.append(price)
        self._volumes.append(volume)
        self._total_volume_seen += volume

        # Auto-calibrate bucket size from first ~100 trades
        if not self._calibrated and len(self._prices) >= 100:
            total_vol = sum(self._volumes)
            # Target: ~50 buckets per rolling window
            self.bucket_volume = max(1e-9, total_vol / self.n_buckets)
            self._calibrated = True
            log.debug(f"VPIN calibrated: bucket_vol={self.bucket_volume:.4f}")

        if self.bucket_volume <= 0:
            return

        # BVC classification
        if len(self._prices) >= 2:
            dp = self._prices[-1] - self._prices[-2]
            sigma = self._estimate_sigma()
            if sigma > 1e-8:
                z = dp / sigma
                buy_frac = self._phi(z)
            else:
                # Sigma ≈ 0 means constant-direction moves.
                # This IS the signal: all moves same direction = informed trading.
                # Classify purely by direction.
                buy_frac = 1.0 if dp > 0 else (0.0 if dp < 0 else 0.5)
        else:
            buy_frac = 0.5

        v_buy = volume * buy_frac
        v_sell = volume * (1.0 - buy_frac)

        self._current_buy += v_buy
        self._current_sell += v_sell
        self._current_vol += volume

        # Check if bucket is full
        while self._current_vol >= self.bucket_volume and self.bucket_volume > 0:
            overflow = self._current_vol - self.bucket_volume
            # Proportional split for overflow
            ratio = self.bucket_volume / (self._current_vol) if self._current_vol > 0 else 1.0
            bucket_buy = self._current_buy * ratio
            bucket_sell = self._current_sell * ratio

            imbalance = abs(bucket_buy - bucket_sell)
            self._buckets.append(imbalance)

            # Carry over overflow
            self._current_buy = self._current_buy * (1 - ratio)
            self._current_sell = self._current_sell * (1 - ratio)
            self._current_vol = overflow

        # Recompute VPIN
        if len(self._buckets) >= 5:
            total_imbalance = sum(self._buckets)
            n = len(self._buckets)
            self._vpin = total_imbalance / (n * self.bucket_volume) if self.bucket_volume > 0 else 0.0
            self._vpin = max(0.0, min(1.0, self._vpin))

    def _estimate_sigma(self) -> float:
        """Rolling standard deviation of price changes (last 20 ticks)."""
        n = min(len(self._prices), 20)
        if n < 5:
            return 1e-6
        recent = list(self._prices)[-n:]
        changes = [recent[i] - recent[i - 1] for i in range(1, len(recent))]
        if not changes:
            return 1e-6
        mean = sum(changes) / len(changes)
        variance = sum((c - mean) ** 2 for c in changes) / len(changes)
        return max(1e-12, math.sqrt(variance))

    @property
    def vpin(self) -> float:
        return self._vpin

    @property
    def is_toxic(self) -> bool:
        return self._vpin >= VPIN_TOXIC_THRESHOLD

    @property
    def is_critical(self) -> bool:
        return self._vpin >= VPIN_CRITICAL_THRESHOLD

    def reset(self):
        self._buckets.clear()
        self._current_buy = 0.0
        self._current_sell = 0.0
        self._current_vol = 0.0
        self._prices.clear()
        self._volumes.clear()
        self._vpin = 0.0


# ────────────────────────────────────────────────────
# WEAPON 2: Dynamic OBI Trailing Stop
# ────────────────────────────────────────────────────
class DynamicOBITrail:
    """Trailing stop that breathes with the order book.

    When OBI shows strong buying pressure → widen the stop, let it run.
    When OBI flips to sell pressure → snap tight, lock in profit.

    The "rubber band" metaphor: the stop stretches when there's fuel
    in the book, and snaps back when the fuel runs out.
    """

    def __init__(self, entry_price: float, side: str, atr: float):
        self.entry_price = entry_price
        self.side = side.upper()  # "LONG" or "SHORT"
        self.atr = max(atr, entry_price * 0.001)  # min 0.1% ATR
        self.highest_price = entry_price  # for LONG
        self.lowest_price = entry_price   # for SHORT
        self.current_stop: float = 0.0
        self.current_atr_mult: float = OBI_TRAIL_DEFAULT_ATR_MULT
        self.obi_history: deque[float] = deque(maxlen=100)
        self._set_initial_stop()

    def _set_initial_stop(self):
        dist = self.atr * self.current_atr_mult
        if self.side == "LONG":
            self.current_stop = self.entry_price - dist
        else:
            self.current_stop = self.entry_price + dist

    def update(self, current_price: float, obi: float) -> Tuple[float, str]:
        """Update trailing stop based on current price and OBI.

        Returns: (stop_price, regime) where regime is "WIDE" / "TIGHT" / "NORMAL"
        """
        self.obi_history.append(obi)
        # Smooth OBI over last 5 ticks to avoid whipsaws
        smooth_obi = sum(list(self.obi_history)[-5:]) / min(5, len(self.obi_history))

        # Determine trail regime
        if smooth_obi > OBI_WIDE_THRESHOLD:
            target_mult = OBI_TRAIL_WIDE_ATR_MULT
            regime = "WIDE"
        elif smooth_obi < OBI_TIGHT_THRESHOLD:
            target_mult = OBI_TRAIL_TIGHT_ATR_MULT
            regime = "TIGHT"
        else:
            target_mult = OBI_TRAIL_DEFAULT_ATR_MULT
            regime = "NORMAL"

        # Smooth transition (don't jerk the stop around)
        self.current_atr_mult = 0.7 * self.current_atr_mult + 0.3 * target_mult

        dist = self.atr * self.current_atr_mult

        if self.side == "LONG":
            self.highest_price = max(self.highest_price, current_price)
            new_stop = self.highest_price - dist
            # Stop can only move UP for longs (ratchet)
            self.current_stop = max(self.current_stop, new_stop)
        else:
            self.lowest_price = min(self.lowest_price, current_price)
            new_stop = self.lowest_price + dist
            # Stop can only move DOWN for shorts (ratchet)
            self.current_stop = min(self.current_stop, new_stop)

        return self.current_stop, regime

    def is_triggered(self, current_price: float) -> bool:
        if self.side == "LONG":
            return current_price <= self.current_stop
        else:
            return current_price >= self.current_stop


# ────────────────────────────────────────────────────
# WEAPON 3: Ghost Liquidity Reactor
# ────────────────────────────────────────────────────
@dataclass
class GhostReaction:
    """Reaction to detected ghost wall pulls."""
    should_exit: bool = False
    confidence: float = 0.0
    reason: str = ""
    ghost_side: str = ""
    notional_usd: float = 0.0


class GhostLiquidityReactor:
    """Watches for ghost wall events and triggers exits.

    Logic for LONG positions:
    - Ghost wall on BID side (fake support pulled) → EXIT immediately
      This means someone was spoofing buy orders to lure you in.
    - Ghost wall on ASK side (fake resistance pulled) → HOLD
      This actually means resistance was fake, price can go up.

    Logic for SHORT positions: inverse of above.
    """

    def __init__(self, position_side: str, min_notional: float = 50_000.0):
        self.position_side = position_side.upper()
        self.min_notional = min_notional
        self._recent_ghosts: deque[dict] = deque(maxlen=50)

    def ingest_ghost_event(self, ghost: dict):
        """Feed a ghost wall event from SpoofHunter."""
        self._recent_ghosts.append({**ghost, "ingested_at": time.monotonic()})

    def evaluate(self, window_s: float = 10.0) -> GhostReaction:
        """Check if recent ghost events warrant an exit."""
        cutoff = time.monotonic() - window_s
        recent = [g for g in self._recent_ghosts if g.get("ingested_at", 0) >= cutoff]

        if not recent:
            return GhostReaction()

        # Aggregate by side
        bid_notional = sum(g.get("notional_usd", 0) for g in recent if g.get("side") == "bid")
        ask_notional = sum(g.get("notional_usd", 0) for g in recent if g.get("side") == "ask")

        # For LONG: bid ghost (fake support pulled) = DANGER
        if self.position_side == "LONG" and bid_notional >= self.min_notional:
            confidence = min(1.0, 0.5 + bid_notional / (self.min_notional * 5))
            if confidence >= GHOST_EXIT_CONFIDENCE:
                return GhostReaction(
                    should_exit=True,
                    confidence=confidence,
                    reason=f"ghost_bid_pulled: ${bid_notional:,.0f} fake support removed",
                    ghost_side="bid",
                    notional_usd=bid_notional,
                )

        # For SHORT: ask ghost (fake resistance pulled) = DANGER
        if self.position_side == "SHORT" and ask_notional >= self.min_notional:
            confidence = min(1.0, 0.5 + ask_notional / (self.min_notional * 5))
            if confidence >= GHOST_EXIT_CONFIDENCE:
                return GhostReaction(
                    should_exit=True,
                    confidence=confidence,
                    reason=f"ghost_ask_pulled: ${ask_notional:,.0f} fake resistance removed",
                    ghost_side="ask",
                    notional_usd=ask_notional,
                )

        return GhostReaction()


# ────────────────────────────────────────────────────
# WEAPON 4: Alpha Decay Timer
# ────────────────────────────────────────────────────
class AlphaDecayTimer:
    """Kills stale setups that go nowhere.

    If price doesn't move ≥ ALPHA_MIN_MOVE_PCT in the favorable
    direction within ALPHA_DECAY_S seconds, the alpha is dead.
    Exit at break-even instead of waiting for a stop loss.
    """

    def __init__(
        self,
        entry_price: float,
        side: str,
        decay_s: float = ALPHA_DECAY_S,
        min_move_pct: float = ALPHA_MIN_MOVE_PCT,
    ):
        self.entry_price = entry_price
        self.side = side.upper()
        self.decay_s = decay_s
        self.min_move_pct = min_move_pct
        self.entry_ts = time.monotonic()
        self.peak_favorable_pct: float = 0.0
        self.decayed: bool = False

    def update(self, current_price: float) -> Tuple[bool, float, float]:
        """Check if alpha has decayed.

        Returns: (is_decayed, elapsed_s, favorable_move_pct)
        """
        elapsed = time.monotonic() - self.entry_ts

        if self.side == "LONG":
            move_pct = (current_price - self.entry_price) / max(self.entry_price, 1e-12) * 100
        else:
            move_pct = (self.entry_price - current_price) / max(self.entry_price, 1e-12) * 100

        self.peak_favorable_pct = max(self.peak_favorable_pct, move_pct)

        # Alpha is alive if we've moved enough in the right direction
        if self.peak_favorable_pct >= self.min_move_pct:
            self.decayed = False
            return False, elapsed, move_pct

        # Check decay
        if elapsed >= self.decay_s:
            self.decayed = True
            return True, elapsed, move_pct

        return False, elapsed, move_pct


# ────────────────────────────────────────────────────
# THE UNIFIED ACTIVE POSITION MANAGER
# ────────────────────────────────────────────────────

class ExitReason(str, Enum):
    VPIN_TOXIC = "vpin_toxic"
    VPIN_CRITICAL = "vpin_critical"
    OBI_TRAIL_STOP = "obi_trail_stop"
    GHOST_LIQUIDITY = "ghost_liquidity"
    ALPHA_DECAY = "alpha_decay"
    MANUAL_EXIT = "manual_exit"
    MACRO_KILL = "macro_kill"
    TIME_LIMIT = "time_limit"
    TAKE_PROFIT = "take_profit"
    HARD_STOP = "hard_stop"


@dataclass
class ManagedPosition:
    """A position actively managed by the APM."""
    position_id: str
    symbol: str
    side: str               # "LONG" or "SHORT"
    entry_price: float
    quantity: float
    entry_ts: float

    # Components
    vpin: VPINComputer = field(default_factory=VPINComputer)
    trail: Optional[DynamicOBITrail] = None
    ghost_reactor: Optional[GhostLiquidityReactor] = None
    alpha_timer: Optional[AlphaDecayTimer] = None

    # Limits
    take_profit_pct: float = 5.0
    hard_stop_pct: float = 3.0
    time_limit_s: float = 1800.0  # 30 min max

    # State
    current_price: float = 0.0
    highest_price: float = 0.0
    lowest_price: float = 0.0
    unrealized_pnl_pct: float = 0.0
    exit_reason: Optional[ExitReason] = None
    exit_price: float = 0.0
    exit_ts: float = 0.0
    ticks_processed: int = 0


@dataclass
class TickData:
    """One tick of L2 + trade data fed to the APM."""
    price: float
    volume: float = 0.0          # trade volume in this tick
    obi: float = 0.0             # order book imbalance [-1, 1]
    ghost_events: List[dict] = field(default_factory=list)
    macro_kill: bool = False      # from EconoPredator


@dataclass
class APMDecision:
    """The APM's decision for a single tick."""
    action: str = "HOLD"         # "HOLD" | "EXIT"
    reason: Optional[ExitReason] = None
    confidence: float = 0.0
    details: Dict = field(default_factory=dict)


class ActivePositionManager:
    """Unified exit engine that runs the 4 HFT weapons in parallel.

    Evaluation order (fastest to slowest threat):
      1. VPIN Critical (>0.85)  → instant market exit
      2. Ghost Liquidity pulled  → instant market exit
      3. VPIN Toxic (>0.70)     → tighten trail to 0.5× ATR
      4. OBI Trailing Stop       → dynamic exit
      5. Alpha Decay             → break-even exit
      6. Hard Stop / TP / Time   → safety net

    The APM processes one tick at a time. Call `process_tick()` every
    100ms (or on every L2 update) and act on the returned decision.
    """

    def __init__(self):
        self._lock = asyncio.Lock()
        self._positions: Dict[str, ManagedPosition] = {}
        self._closed: deque[ManagedPosition] = deque(maxlen=500)
        self.total_exits: int = 0
        self.exit_reason_counts: Dict[str, int] = {}

    async def register_position(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        quantity: float,
        atr: float,
        *,
        take_profit_pct: float = 5.0,
        hard_stop_pct: float = 3.0,
        alpha_decay_s: float = ALPHA_DECAY_S,
        alpha_min_move_pct: float = ALPHA_MIN_MOVE_PCT,
        time_limit_s: float = 1800.0,
        vpin_bucket_vol: float = 0.0,
        ghost_min_notional: float = 50_000.0,
    ) -> str:
        """Register a new position for active management.

        Returns: position_id
        """
        async with self._lock:
            pos_id = uuid.uuid4().hex[:10]
            pos = ManagedPosition(
                position_id=pos_id,
                symbol=symbol,
                side=side.upper(),
                entry_price=entry_price,
                quantity=quantity,
                entry_ts=time.monotonic(),
                highest_price=entry_price,
                lowest_price=entry_price,
                take_profit_pct=take_profit_pct,
                hard_stop_pct=hard_stop_pct,
                time_limit_s=time_limit_s,
            )

            # Initialize weapons
            pos.vpin = VPINComputer(bucket_volume=vpin_bucket_vol)
            pos.trail = DynamicOBITrail(entry_price, side, atr)
            pos.ghost_reactor = GhostLiquidityReactor(side, ghost_min_notional)
            pos.alpha_timer = AlphaDecayTimer(entry_price, side, alpha_decay_s, alpha_min_move_pct)

            self._positions[pos_id] = pos
            log.info(
                f"[APM] Position registered: {pos_id} {side} {symbol} "
                f"@{entry_price} qty={quantity} ATR={atr:.4f}"
            )
            return pos_id

    async def process_tick(self, position_id: str, tick: TickData) -> APMDecision:
        """Process one tick of data through all 4 weapons.

        This is the hot path — called every ~100ms per position.
        """
        async with self._lock:
            pos = self._positions.get(position_id)
            if not pos:
                return APMDecision(action="HOLD", details={"error": "position_not_found"})

            pos.ticks_processed += 1
            pos.current_price = tick.price
            pos.highest_price = max(pos.highest_price, tick.price)
            pos.lowest_price = min(pos.lowest_price, tick.price)

            # PnL
            if pos.side == "LONG":
                pos.unrealized_pnl_pct = (tick.price - pos.entry_price) / pos.entry_price * 100
            else:
                pos.unrealized_pnl_pct = (pos.entry_price - tick.price) / pos.entry_price * 100

            elapsed = time.monotonic() - pos.entry_ts
            details = {
                "position_id": position_id,
                "price": tick.price,
                "pnl_pct": round(pos.unrealized_pnl_pct, 4),
                "elapsed_s": round(elapsed, 1),
                "ticks": pos.ticks_processed,
            }

            # ════════════════════════════════════════════
            # PRIORITY 0: Macro Kill Switch
            # ════════════════════════════════════════════
            if tick.macro_kill:
                return self._exit(pos, tick.price, ExitReason.MACRO_KILL, 1.0, {
                    **details, "trigger": "macro_kill_switch"
                })

            # ════════════════════════════════════════════
            # PRIORITY 1: VPIN Critical (>0.85) → INSTANT EXIT
            # ════════════════════════════════════════════
            if tick.volume > 0:
                pos.vpin.ingest_trade(tick.price, tick.volume)

            vpin_val = pos.vpin.vpin
            details["vpin"] = round(vpin_val, 4)

            if pos.vpin.is_critical:
                return self._exit(pos, tick.price, ExitReason.VPIN_CRITICAL, 0.95, {
                    **details, "trigger": f"vpin={vpin_val:.3f} > {VPIN_CRITICAL_THRESHOLD}"
                })

            # ════════════════════════════════════════════
            # PRIORITY 2: Ghost Liquidity Exit
            # ════════════════════════════════════════════
            if pos.ghost_reactor and tick.ghost_events:
                for ge in tick.ghost_events:
                    pos.ghost_reactor.ingest_ghost_event(ge)

                reaction = pos.ghost_reactor.evaluate()
                details["ghost_exit"] = reaction.should_exit
                if reaction.should_exit:
                    return self._exit(pos, tick.price, ExitReason.GHOST_LIQUIDITY, reaction.confidence, {
                        **details, "trigger": reaction.reason,
                        "ghost_notional": reaction.notional_usd,
                    })

            # ════════════════════════════════════════════
            # PRIORITY 3: VPIN Toxic (>0.70) → TIGHTEN trail
            # ════════════════════════════════════════════
            if pos.vpin.is_toxic and pos.trail:
                # Override trail to ultra-tight
                pos.trail.current_atr_mult = min(pos.trail.current_atr_mult, 0.5)
                details["vpin_tightened"] = True

            # ════════════════════════════════════════════
            # PRIORITY 4: Dynamic OBI Trailing Stop
            # ════════════════════════════════════════════
            if pos.trail:
                stop_price, regime = pos.trail.update(tick.price, tick.obi)
                details["trail_stop"] = round(stop_price, 6)
                details["trail_regime"] = regime
                details["trail_atr_mult"] = round(pos.trail.current_atr_mult, 3)

                if pos.trail.is_triggered(tick.price):
                    return self._exit(pos, tick.price, ExitReason.OBI_TRAIL_STOP, 0.85, {
                        **details, "trigger": f"price={tick.price} hit trail_stop={stop_price:.6f} regime={regime}"
                    })

            # ════════════════════════════════════════════
            # PRIORITY 5: Alpha Decay
            # ════════════════════════════════════════════
            if pos.alpha_timer:
                decayed, elapsed_s, move_pct = pos.alpha_timer.update(tick.price)
                details["alpha_decayed"] = decayed
                details["alpha_move_pct"] = round(move_pct, 4)

                if decayed:
                    return self._exit(pos, tick.price, ExitReason.ALPHA_DECAY, 0.70, {
                        **details,
                        "trigger": f"no {ALPHA_MIN_MOVE_PCT}% move in {ALPHA_DECAY_S}s (got {move_pct:.2f}%)"
                    })

            # ════════════════════════════════════════════
            # SAFETY NET: Hard Stop / Take Profit / Time
            # ════════════════════════════════════════════
            if pos.unrealized_pnl_pct >= pos.take_profit_pct:
                return self._exit(pos, tick.price, ExitReason.TAKE_PROFIT, 1.0, {
                    **details, "trigger": f"pnl={pos.unrealized_pnl_pct:.2f}% >= tp={pos.take_profit_pct}%"
                })

            if pos.unrealized_pnl_pct <= -pos.hard_stop_pct:
                return self._exit(pos, tick.price, ExitReason.HARD_STOP, 1.0, {
                    **details, "trigger": f"pnl={pos.unrealized_pnl_pct:.2f}% <= -stop={pos.hard_stop_pct}%"
                })

            if elapsed >= pos.time_limit_s:
                return self._exit(pos, tick.price, ExitReason.TIME_LIMIT, 0.60, {
                    **details, "trigger": f"elapsed={elapsed:.0f}s >= limit={pos.time_limit_s}s"
                })

            # ════════════════════════════════════════════
            # ALL CLEAR: HOLD
            # ════════════════════════════════════════════
            return APMDecision(action="HOLD", details=details)

    def _exit(
        self,
        pos: ManagedPosition,
        price: float,
        reason: ExitReason,
        confidence: float,
        details: dict,
    ) -> APMDecision:
        """Record exit and remove position from active management."""
        pos.exit_reason = reason
        pos.exit_price = price
        pos.exit_ts = time.monotonic()
        self._closed.append(pos)
        del self._positions[pos.position_id]

        self.total_exits += 1
        self.exit_reason_counts[reason.value] = self.exit_reason_counts.get(reason.value, 0) + 1

        log.info(
            f"[APM] EXIT {reason.value}: {pos.position_id} {pos.side} {pos.symbol} "
            f"entry={pos.entry_price} exit={price} pnl={pos.unrealized_pnl_pct:.2f}% "
            f"ticks={pos.ticks_processed}"
        )

        return APMDecision(action="EXIT", reason=reason, confidence=confidence, details=details)

    async def force_exit(self, position_id: str, price: float) -> APMDecision:
        """Force-exit a position (admin action)."""
        async with self._lock:
            pos = self._positions.get(position_id)
            if not pos:
                return APMDecision(action="HOLD", details={"error": "not_found"})
            if pos.side == "LONG":
                pos.unrealized_pnl_pct = (price - pos.entry_price) / pos.entry_price * 100
            else:
                pos.unrealized_pnl_pct = (pos.entry_price - price) / pos.entry_price * 100
            return self._exit(pos, price, ExitReason.MANUAL_EXIT, 1.0, {
                "position_id": position_id, "price": price
            })

    async def get_active(self) -> List[dict]:
        async with self._lock:
            return [
                {
                    "position_id": p.position_id,
                    "symbol": p.symbol,
                    "side": p.side,
                    "entry_price": p.entry_price,
                    "current_price": p.current_price,
                    "unrealized_pnl_pct": round(p.unrealized_pnl_pct, 4),
                    "vpin": round(p.vpin.vpin, 4),
                    "trail_stop": round(p.trail.current_stop, 6) if p.trail else 0.0,
                    "trail_regime": "N/A",
                    "alpha_decayed": p.alpha_timer.decayed if p.alpha_timer else False,
                    "ticks": p.ticks_processed,
                    "elapsed_s": round(time.monotonic() - p.entry_ts, 1),
                }
                for p in self._positions.values()
            ]

    async def get_stats(self) -> dict:
        async with self._lock:
            closed = list(self._closed)
            wins = [p for p in closed if p.unrealized_pnl_pct > 0]
            losses = [p for p in closed if p.unrealized_pnl_pct <= 0]
            pnls = [p.unrealized_pnl_pct for p in closed]
            return {
                "active_positions": len(self._positions),
                "total_exits": self.total_exits,
                "wins": len(wins),
                "losses": len(losses),
                "win_rate_pct": round(len(wins) / max(len(closed), 1) * 100, 1),
                "avg_pnl_pct": round(sum(pnls) / max(len(pnls), 1), 4),
                "best_pnl_pct": round(max(pnls, default=0), 4),
                "worst_pnl_pct": round(min(pnls, default=0), 4),
                "exit_reasons": dict(self.exit_reason_counts),
            }
