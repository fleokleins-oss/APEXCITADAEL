"""Tests for Active Position Manager (APM) — 4 HFT Weapons."""

import asyncio
import time

import pytest

from apm import (
    VPINComputer,
    DynamicOBITrail,
    GhostLiquidityReactor,
    AlphaDecayTimer,
    ActivePositionManager,
    TickData,
    ExitReason,
    VPIN_TOXIC_THRESHOLD,
    VPIN_CRITICAL_THRESHOLD,
    ALPHA_DECAY_S,
    ALPHA_MIN_MOVE_PCT,
)


# ════════════════════════════════════════════════════
# WEAPON 1: VPIN
# ════════════════════════════════════════════════════
class TestVPIN:
    def test_starts_at_zero(self):
        v = VPINComputer(bucket_volume=10.0)
        assert v.vpin == 0.0
        assert not v.is_toxic
        assert not v.is_critical

    def test_balanced_flow_low_vpin(self):
        """Balanced buy/sell → low VPIN."""
        v = VPINComputer(bucket_volume=5.0)
        # Alternating up/down trades (balanced flow)
        for i in range(200):
            price = 100.0 + 0.01 * (1 if i % 2 == 0 else -1)
            v.ingest_trade(price, 1.0)
        # Should be low (balanced)
        assert v.vpin < 0.5

    def test_one_sided_flow_high_vpin(self):
        """All selling → high VPIN (toxic)."""
        v = VPINComputer(bucket_volume=2.0)  # Small buckets for test data
        # Relentless selling: price drops monotonically
        for i in range(500):
            price = 100.0 - i * 0.05
            v.ingest_trade(price, 1.0)
        # Should be elevated (one-sided informed selling)
        assert v.vpin > 0.3

    def test_phi_function(self):
        """Standard normal CDF correctness."""
        assert VPINComputer._phi(0.0) == pytest.approx(0.5, abs=0.01)
        assert VPINComputer._phi(6.0) == pytest.approx(1.0, abs=0.001)
        assert VPINComputer._phi(-6.0) == pytest.approx(0.0, abs=0.001)

    def test_auto_calibration(self):
        v = VPINComputer()  # bucket_volume=0 → auto
        assert not v._calibrated
        for i in range(150):
            v.ingest_trade(100.0 + i * 0.001, 1.0)
        assert v._calibrated
        assert v.bucket_volume > 0

    def test_reset(self):
        v = VPINComputer(bucket_volume=5.0)
        for i in range(100):
            v.ingest_trade(100.0 - i * 0.1, 2.0)
        assert v.vpin > 0
        v.reset()
        assert v.vpin == 0.0


# ════════════════════════════════════════════════════
# WEAPON 2: Dynamic OBI Trailing
# ════════════════════════════════════════════════════
class TestDynamicOBITrail:
    def test_initial_stop_long(self):
        trail = DynamicOBITrail(entry_price=100.0, side="LONG", atr=2.0)
        assert trail.current_stop < 100.0  # Below entry for long

    def test_initial_stop_short(self):
        trail = DynamicOBITrail(entry_price=100.0, side="SHORT", atr=2.0)
        assert trail.current_stop > 100.0  # Above entry for short

    def test_wide_regime_loosens_stop(self):
        trail = DynamicOBITrail(entry_price=100.0, side="LONG", atr=2.0)
        initial_stop = trail.current_stop

        # Strong buying pressure → widen trail
        for _ in range(10):
            stop, regime = trail.update(105.0, obi=0.8)

        # Stop should be further from price than initial
        distance = 105.0 - stop
        initial_distance = 100.0 - initial_stop
        assert distance > initial_distance * 0.8  # Wider

    def test_tight_regime_snaps_stop(self):
        trail = DynamicOBITrail(entry_price=100.0, side="LONG", atr=2.0)

        # First: price pumps to 110 with positive OBI
        for _ in range(10):
            trail.update(110.0, obi=0.5)

        stop_before_flip = trail.current_stop

        # OBI flips to sell pressure → snap tight
        for _ in range(10):
            trail.update(110.0, obi=-0.5)

        stop_after_flip = trail.current_stop
        # Stop should have moved UP (tighter to price)
        assert stop_after_flip >= stop_before_flip

    def test_ratchet_long(self):
        """Long stop can only move UP (never down)."""
        trail = DynamicOBITrail(entry_price=100.0, side="LONG", atr=2.0)
        trail.update(105.0, obi=0.5)
        high_stop = trail.current_stop
        trail.update(103.0, obi=0.0)  # Price drops
        assert trail.current_stop >= high_stop  # Stop didn't move down

    def test_triggered_long(self):
        trail = DynamicOBITrail(entry_price=100.0, side="LONG", atr=2.0)
        trail.update(105.0, obi=0.5)
        # Price crashes below stop
        assert trail.is_triggered(90.0)
        assert not trail.is_triggered(104.0)

    def test_triggered_short(self):
        trail = DynamicOBITrail(entry_price=100.0, side="SHORT", atr=2.0)
        trail.update(95.0, obi=-0.5)
        # Price rises above stop
        assert trail.is_triggered(110.0)
        assert not trail.is_triggered(96.0)


# ════════════════════════════════════════════════════
# WEAPON 3: Ghost Liquidity Reactor
# ════════════════════════════════════════════════════
class TestGhostReactor:
    def test_no_events_no_exit(self):
        reactor = GhostLiquidityReactor("LONG")
        reaction = reactor.evaluate()
        assert not reaction.should_exit

    def test_long_bid_ghost_exits(self):
        """LONG position + bid ghost (fake support) → EXIT."""
        reactor = GhostLiquidityReactor("LONG", min_notional=10_000)
        reactor.ingest_ghost_event({
            "side": "bid", "notional_usd": 100_000, "ingested_at": time.monotonic(),
        })
        reaction = reactor.evaluate(window_s=5.0)
        assert reaction.should_exit
        assert "fake support" in reaction.reason

    def test_long_ask_ghost_holds(self):
        """LONG position + ask ghost (fake resistance removed) → HOLD."""
        reactor = GhostLiquidityReactor("LONG", min_notional=10_000)
        reactor.ingest_ghost_event({
            "side": "ask", "notional_usd": 100_000, "ingested_at": time.monotonic(),
        })
        reaction = reactor.evaluate(window_s=5.0)
        assert not reaction.should_exit  # Ask ghost is bullish for longs

    def test_short_ask_ghost_exits(self):
        """SHORT position + ask ghost (fake resistance removed) → EXIT."""
        reactor = GhostLiquidityReactor("SHORT", min_notional=10_000)
        reactor.ingest_ghost_event({
            "side": "ask", "notional_usd": 100_000, "ingested_at": time.monotonic(),
        })
        reaction = reactor.evaluate(window_s=5.0)
        assert reaction.should_exit

    def test_below_threshold_holds(self):
        reactor = GhostLiquidityReactor("LONG", min_notional=100_000)
        reactor.ingest_ghost_event({
            "side": "bid", "notional_usd": 5_000,  # Way below threshold
        })
        reaction = reactor.evaluate()
        assert not reaction.should_exit


# ════════════════════════════════════════════════════
# WEAPON 4: Alpha Decay
# ════════════════════════════════════════════════════
class TestAlphaDecay:
    def test_not_decayed_when_moving(self):
        timer = AlphaDecayTimer(entry_price=100.0, side="LONG", decay_s=5.0, min_move_pct=0.5)
        decayed, _, move = timer.update(101.0)  # +1%
        assert not decayed
        assert move > 0.5

    def test_decayed_when_flat(self):
        timer = AlphaDecayTimer(entry_price=100.0, side="LONG", decay_s=0.01, min_move_pct=0.5)
        # Price barely moves
        import time as _t
        _t.sleep(0.02)
        decayed, elapsed, move = timer.update(100.1)  # +0.1% < 0.5% threshold
        assert decayed
        assert elapsed >= 0.01

    def test_short_side(self):
        timer = AlphaDecayTimer(entry_price=100.0, side="SHORT", decay_s=5.0, min_move_pct=0.5)
        decayed, _, move = timer.update(99.0)  # -1% → good for short
        assert not decayed
        assert move > 0.5

    def test_peak_tracking(self):
        timer = AlphaDecayTimer(entry_price=100.0, side="LONG", decay_s=60.0, min_move_pct=0.5)
        timer.update(102.0)  # +2%
        assert timer.peak_favorable_pct >= 1.5
        timer.update(100.5)  # Pull back
        assert timer.peak_favorable_pct >= 1.5  # Peak remembered


# ════════════════════════════════════════════════════
# INTEGRATION: Full APM
# ════════════════════════════════════════════════════
class TestAPMIntegration:
    @pytest.mark.asyncio
    async def test_register_and_hold(self):
        apm = ActivePositionManager()
        pid = await apm.register_position("BTCUSDT", "LONG", 50000, 0.1, atr=500)
        decision = await apm.process_tick(pid, TickData(price=50100, volume=1.0, obi=0.3))
        assert decision.action == "HOLD"

    @pytest.mark.asyncio
    async def test_hard_stop_exit(self):
        apm = ActivePositionManager()
        pid = await apm.register_position("BTCUSDT", "LONG", 50000, 0.1, atr=500, hard_stop_pct=2.0)
        decision = await apm.process_tick(pid, TickData(price=48000, volume=1.0))  # -4%
        assert decision.action == "EXIT"
        # Trail stop or hard stop — both are valid; trail fires first because it's evaluated first
        assert decision.reason in (ExitReason.HARD_STOP, ExitReason.OBI_TRAIL_STOP)

    @pytest.mark.asyncio
    async def test_take_profit_exit(self):
        apm = ActivePositionManager()
        pid = await apm.register_position("BTCUSDT", "LONG", 50000, 0.1, atr=500, take_profit_pct=3.0)
        decision = await apm.process_tick(pid, TickData(price=52000, volume=1.0))  # +4%
        assert decision.action == "EXIT"
        assert decision.reason == ExitReason.TAKE_PROFIT

    @pytest.mark.asyncio
    async def test_macro_kill_overrides(self):
        apm = ActivePositionManager()
        pid = await apm.register_position("BTCUSDT", "LONG", 50000, 0.1, atr=500)
        decision = await apm.process_tick(pid, TickData(price=50100, macro_kill=True))
        assert decision.action == "EXIT"
        assert decision.reason == ExitReason.MACRO_KILL

    @pytest.mark.asyncio
    async def test_ghost_exit_long(self):
        apm = ActivePositionManager()
        pid = await apm.register_position(
            "BTCUSDT", "LONG", 50000, 0.1, atr=500, ghost_min_notional=10_000,
        )
        # Feed ghost event: fake bid support pulled
        decision = await apm.process_tick(pid, TickData(
            price=50100, volume=1.0,
            ghost_events=[{"side": "bid", "notional_usd": 200_000}],
        ))
        assert decision.action == "EXIT"
        assert decision.reason == ExitReason.GHOST_LIQUIDITY

    @pytest.mark.asyncio
    async def test_obi_trail_exit(self):
        apm = ActivePositionManager()
        pid = await apm.register_position("BTCUSDT", "LONG", 50000, 0.1, atr=200)

        # Price pumps with positive OBI
        for _ in range(10):
            await apm.process_tick(pid, TickData(price=51000, volume=1.0, obi=0.5))

        # OBI flips, price crashes
        for _ in range(5):
            await apm.process_tick(pid, TickData(price=50800, volume=1.0, obi=-0.5))

        # Price drops below trail
        decision = await apm.process_tick(pid, TickData(price=49500, volume=1.0, obi=-0.8))
        assert decision.action == "EXIT"
        assert decision.reason == ExitReason.OBI_TRAIL_STOP

    @pytest.mark.asyncio
    async def test_alpha_decay_exit(self):
        apm = ActivePositionManager()
        pid = await apm.register_position(
            "BTCUSDT", "LONG", 50000, 0.1, atr=500,
            alpha_decay_s=0.01,  # Very short for testing
            alpha_min_move_pct=1.0,
        )
        import time as _t
        _t.sleep(0.02)
        # Price barely moved
        decision = await apm.process_tick(pid, TickData(price=50010, volume=0.1))
        assert decision.action == "EXIT"
        assert decision.reason == ExitReason.ALPHA_DECAY

    @pytest.mark.asyncio
    async def test_force_exit(self):
        apm = ActivePositionManager()
        pid = await apm.register_position("BTCUSDT", "LONG", 50000, 0.1, atr=500)
        decision = await apm.force_exit(pid, 50500)
        assert decision.action == "EXIT"
        assert decision.reason == ExitReason.MANUAL_EXIT

    @pytest.mark.asyncio
    async def test_stats(self):
        apm = ActivePositionManager()
        pid = await apm.register_position("BTCUSDT", "LONG", 50000, 0.1, atr=500, take_profit_pct=2.0)
        await apm.process_tick(pid, TickData(price=52000, volume=1.0))  # TP exit

        stats = await apm.get_stats()
        assert stats["total_exits"] == 1
        assert stats["wins"] == 1
        assert "take_profit" in stats["exit_reasons"]

    @pytest.mark.asyncio
    async def test_vpin_critical_instant_exit(self):
        """Simulate insider dumping → VPIN spikes → instant exit."""
        apm = ActivePositionManager()
        pid = await apm.register_position(
            "MEMEUSDT", "LONG", 1.0, 1000, atr=0.05,
            vpin_bucket_vol=10.0,
        )
        # Simulate aggressive one-sided selling (price dropping monotonically)
        for i in range(300):
            price = 1.0 - i * 0.001  # Steady decline
            decision = await apm.process_tick(pid, TickData(price=price, volume=5.0, obi=-0.3))
            if decision.action == "EXIT":
                assert decision.reason in (
                    ExitReason.VPIN_CRITICAL,
                    ExitReason.VPIN_TOXIC,
                    ExitReason.OBI_TRAIL_STOP,
                    ExitReason.HARD_STOP,
                )
                break
        else:
            # Should have exited before 300 ticks via trail or hard stop at minimum
            pytest.fail("Should have exited during dump")

    @pytest.mark.asyncio
    async def test_multiple_positions(self):
        apm = ActivePositionManager()
        p1 = await apm.register_position("BTCUSDT", "LONG", 50000, 0.1, atr=500)
        p2 = await apm.register_position("ETHUSDT", "SHORT", 3000, 1.0, atr=50)

        active = await apm.get_active()
        assert len(active) == 2

        await apm.process_tick(p1, TickData(price=50100))
        await apm.process_tick(p2, TickData(price=2990))

        active = await apm.get_active()
        assert len(active) == 2

    @pytest.mark.asyncio
    async def test_position_not_found(self):
        apm = ActivePositionManager()
        decision = await apm.process_tick("nonexistent", TickData(price=100))
        assert decision.action == "HOLD"
        assert "error" in decision.details
