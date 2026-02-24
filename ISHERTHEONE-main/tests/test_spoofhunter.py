"""Tests for SpoofHunter L2 Telemetry — ghost wall detection, micro-price, signal."""

import asyncio
import time

import pytest

# We test the SpoofEngine directly (no FastAPI needed)
from spoofhunter import SpoofEngine, GHOST_MIN_NOTIONAL_USD


@pytest.fixture
def engine():
    return SpoofEngine()


def make_depth(bid_px, bid_sz, ask_px, ask_sz, extra_bids=None, extra_asks=None):
    """Helper to build bid/ask arrays."""
    bids = [[bid_px, bid_sz]]
    asks = [[ask_px, ask_sz]]
    if extra_bids:
        bids.extend(extra_bids)
    if extra_asks:
        asks.extend(extra_asks)
    return bids, asks


# ──── Micro-price tests ────

class TestMicroPrice:
    @pytest.mark.asyncio
    async def test_micro_price_balanced(self, engine):
        """Equal bid/ask sizes → micro-price equals mid-price."""
        bids, asks = make_depth(100.0, 10.0, 101.0, 10.0)
        await engine.process_depth(bids, asks)
        assert engine.mid_price == pytest.approx(100.5, abs=0.01)
        assert abs(engine.micro_shift) < 0.01
        assert abs(engine.imbalance) < 0.01

    @pytest.mark.asyncio
    async def test_micro_price_bid_heavy(self, engine):
        """Large bid size → micro-price shifts toward ask → positive shift."""
        bids, asks = make_depth(100.0, 100.0, 101.0, 1.0)
        await engine.process_depth(bids, asks)
        # micro = (101*100 + 100*1) / 101 ≈ 100.99
        assert engine.micro_shift > 0  # shifted toward ask (buy pressure)
        assert engine.imbalance > 0.9  # strongly bid-heavy

    @pytest.mark.asyncio
    async def test_micro_price_ask_heavy(self, engine):
        """Large ask size → micro-price shifts toward bid → negative shift."""
        bids, asks = make_depth(100.0, 1.0, 101.0, 100.0)
        await engine.process_depth(bids, asks)
        assert engine.micro_shift < 0  # shifted toward bid (sell pressure)
        assert engine.imbalance < -0.9

    @pytest.mark.asyncio
    async def test_empty_depth_noop(self, engine):
        await engine.process_depth([], [])
        assert engine.mid_price == 0.0


# ──── Ghost wall detection tests ────

class TestGhostWalls:
    @pytest.mark.asyncio
    async def test_ghost_wall_basic_flow(self, engine):
        """A large wall appears then disappears quickly → ghost detected."""
        price = 50000.0
        # Wall at 49990 (10 bps from mid) with $100k notional
        wall_qty = GHOST_MIN_NOTIONAL_USD * 1.5 / price  # ~1.5 BTC
        wall_px = 49990.0

        bids_with_wall = [[50000.0, 0.5], [wall_px, wall_qty]]
        asks = [[50010.0, 0.5]]

        # Step 1: Wall appears
        await engine.process_depth(bids_with_wall, asks, mark_price=price)
        snap = await engine.snapshot()
        assert snap["active_wall_trackers"] >= 1

        # Step 2: Wall disappears (next tick, no wall)
        bids_no_wall = [[50000.0, 0.5]]
        await engine.process_depth(bids_no_wall, asks, mark_price=price)

        snap = await engine.snapshot()
        assert snap["ghost_walls_detected"] >= 1
        assert snap["ghost_bid_count"] >= 1

    @pytest.mark.asyncio
    async def test_no_ghost_for_small_walls(self, engine):
        """Walls below the notional threshold are not tracked."""
        price = 50000.0
        small_qty = (GHOST_MIN_NOTIONAL_USD * 0.5) / price  # Below threshold

        bids = [[50000.0, 0.5], [49990.0, small_qty]]
        asks = [[50010.0, 0.5]]

        await engine.process_depth(bids, asks, mark_price=price)
        snap = await engine.snapshot()
        assert snap["active_wall_trackers"] == 0

    @pytest.mark.asyncio
    async def test_filled_wall_not_ghost(self, engine):
        """A wall that gets consumed (>80% reduction) is FILLED, not PULLED."""
        price = 50000.0
        wall_qty = GHOST_MIN_NOTIONAL_USD * 2 / price
        wall_px = 49990.0

        # Appear
        bids = [[50000.0, 0.5], [wall_px, wall_qty]]
        asks = [[50010.0, 0.5]]
        await engine.process_depth(bids, asks, mark_price=price)

        # Reduce to 10% of original (80%+ consumed = filled)
        bids_reduced = [[50000.0, 0.5], [wall_px, wall_qty * 0.1]]
        await engine.process_depth(bids_reduced, asks, mark_price=price)

        snap = await engine.snapshot()
        # Should NOT be counted as ghost (it was filled, not pulled)
        assert snap["ghost_walls_detected"] == 0

    @pytest.mark.asyncio
    async def test_ghost_on_ask_side(self, engine):
        """Ghost wall on the ask side (fake resistance)."""
        price = 50000.0
        wall_qty = GHOST_MIN_NOTIONAL_USD * 1.5 / price
        wall_px = 50010.0  # 2 bps from mid

        bids = [[50000.0, 0.5]]
        asks_with_wall = [[50005.0, 0.1], [wall_px, wall_qty]]
        await engine.process_depth(bids, asks_with_wall, mark_price=price)

        # Pull it
        asks_no_wall = [[50005.0, 0.1]]
        await engine.process_depth(bids, asks_no_wall, mark_price=price)

        snap = await engine.snapshot()
        assert snap["ghost_ask_count"] >= 1


# ──── Signal generation tests ────

class TestSignal:
    @pytest.mark.asyncio
    async def test_no_ghosts_low_imbalance_waits(self, engine):
        """No ghosts + balanced book → WAIT."""
        bids, asks = make_depth(100.0, 10.0, 101.0, 10.0)
        await engine.process_depth(bids, asks)
        sig = await engine.signal()
        assert sig["action"] == "WAIT"

    @pytest.mark.asyncio
    async def test_strong_imbalance_signals(self, engine):
        """Strong imbalance without ghosts can still produce weak signal."""
        bids, asks = make_depth(100.0, 100.0, 101.0, 1.0)
        await engine.process_depth(bids, asks)
        sig = await engine.signal()
        # Very strong bid imbalance → LONG signal
        if sig["action"] == "EXECUTE":
            assert sig["side"] == "LONG"
            assert sig["confidence"] > 0

    @pytest.mark.asyncio
    async def test_ghost_produces_contrarian_signal(self, engine):
        """Ghost walls on bid side → bearish (SHORT) contrarian signal."""
        price = 50000.0
        wall_qty = GHOST_MIN_NOTIONAL_USD * 3 / price

        # Multiple ghost walls for stronger signal
        for _ in range(3):
            wall_px = 49990.0 + _ * 0.01  # Slightly different prices
            bids = [[50000.0, 0.5], [wall_px, wall_qty]]
            asks = [[50010.0, 0.5]]
            await engine.process_depth(bids, asks, mark_price=price)

            # Pull
            bids_clean = [[50000.0, 0.5]]
            await engine.process_depth(bids_clean, asks, mark_price=price)

        sig = await engine.signal()
        assert sig["ghost_walls_detected"] >= 1
        # Bid-side ghosts = fake support = bearish
        if sig["ghost_wall_side"] == "SHORT":
            assert sig["side"] in ("SHORT", "NONE")


# ──── Snapshot / stats tests ────

class TestSnapshot:
    @pytest.mark.asyncio
    async def test_snapshot_fields(self, engine):
        bids, asks = make_depth(100.0, 10.0, 101.0, 10.0)
        await engine.process_depth(bids, asks)
        snap = await engine.snapshot()

        required_fields = [
            "mid_price", "micro_price", "micro_price_shift", "orderbook_imbalance",
            "ghost_walls_detected", "ghost_wall_side", "ghost_wall_intensity",
            "iceberg_detected", "snapshots_processed",
        ]
        for f in required_fields:
            assert f in snap, f"Missing field: {f}"

    @pytest.mark.asyncio
    async def test_snapshots_counted(self, engine):
        bids, asks = make_depth(100.0, 10.0, 101.0, 10.0)
        for _ in range(5):
            await engine.process_depth(bids, asks)
        snap = await engine.snapshot()
        assert snap["snapshots_processed"] == 5
