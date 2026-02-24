"""Tests for Phase 2 nodes: EconoPredator, Narrative, Jito, AntiRug v3."""

import asyncio
import time

import numpy as np
import pytest

# ════════════════════════════════════════════════════════════
# EconoPredator — ATR computation
# ════════════════════════════════════════════════════════════
from econopredator import compute_atr, DataStore, FundingSnapshot, OISnapshot, ATRData, MacroSnapshot


class TestATRComputation:
    def test_basic_atr(self):
        """ATR of known data."""
        highs = [10, 12, 11, 13, 14, 12, 15, 13, 16, 14, 17, 15, 18, 16, 19, 17]
        lows = [8, 9, 8, 10, 11, 9, 12, 10, 13, 11, 14, 12, 15, 13, 16, 14]
        closes = [9, 11, 10, 12, 13, 11, 14, 12, 15, 13, 16, 14, 17, 15, 18, 16]
        atr = compute_atr(highs, lows, closes, period=5)
        assert atr > 0
        assert atr < 10  # Reasonable range

    def test_insufficient_data(self):
        assert compute_atr([10, 11], [8, 9], [9, 10], period=14) == 0.0

    def test_constant_prices(self):
        """ATR should be near zero for constant prices."""
        n = 20
        highs = [100.0] * n
        lows = [100.0] * n
        closes = [100.0] * n
        atr = compute_atr(highs, lows, closes, period=14)
        assert atr == pytest.approx(0.0, abs=0.01)

    def test_increasing_volatility(self):
        """ATR should increase with wider ranges."""
        n = 20
        highs_narrow = [100 + i * 0.1 + 0.5 for i in range(n)]
        lows_narrow = [100 + i * 0.1 - 0.5 for i in range(n)]
        closes_narrow = [100 + i * 0.1 for i in range(n)]

        highs_wide = [100 + i * 0.1 + 5.0 for i in range(n)]
        lows_wide = [100 + i * 0.1 - 5.0 for i in range(n)]
        closes_wide = [100 + i * 0.1 for i in range(n)]

        atr_narrow = compute_atr(highs_narrow, lows_narrow, closes_narrow, period=14)
        atr_wide = compute_atr(highs_wide, lows_wide, closes_wide, period=14)
        assert atr_wide > atr_narrow


class TestDataStore:
    @pytest.mark.asyncio
    async def test_funding_store(self):
        ds = DataStore()
        await ds.update_funding("BTCUSDT", FundingSnapshot(
            symbol="BTCUSDT", mark_price=50000, funding_rate=0.0001, ts=time.time(),
        ))
        data = await ds.get_market_data("BTCUSDT")
        assert data["funding"]["mark_price"] == 50000
        assert data["funding"]["funding_rate"] == 0.0001

    @pytest.mark.asyncio
    async def test_atr_store(self):
        ds = DataStore()
        await ds.update_atr("BTCUSDT", ATRData(
            symbol="BTCUSDT", atr=500.0, atr_pct=1.0, current_price=50000, ts=time.time(),
        ))
        result = await ds.get_atr("BTCUSDT")
        assert result["atr"] == 500.0
        assert result["atr_pct"] == 1.0

    @pytest.mark.asyncio
    async def test_funding_heatmap(self):
        ds = DataStore()
        await ds.update_funding("BTCUSDT", FundingSnapshot(
            symbol="BTCUSDT", funding_rate=0.001, ts=time.time(),
        ))
        heatmap = await ds.get_funding_heatmap()
        assert "BTCUSDT" in heatmap
        assert heatmap["BTCUSDT"]["intensity"] == "HIGH"

    @pytest.mark.asyncio
    async def test_macro_fear_greed(self):
        """Test new Fear & Greed + stablecoin fields in macro."""
        ds = DataStore()
        await ds.update_macro(MacroSnapshot(
            vix=30.0, fear_greed=15, fear_greed_label="Extreme Fear",
            stablecoin_mcap=150e9, dxy=106, ts=time.time(),
        ))
        result = await ds.get_macro()
        assert result["fear_greed"] == 15
        assert result["fear_greed_label"] == "Extreme Fear"
        assert result["stablecoin_mcap_b"] > 100
        assert result["risk_environment"] in ("RISK_OFF", "CAUTIOUS")
        assert result["risk_score"] < 0

    @pytest.mark.asyncio
    async def test_macro_kill_switch(self):
        """VIX > 35 AND Fear & Greed < 20 → macro_kill = True."""
        ds = DataStore()
        await ds.update_macro(MacroSnapshot(
            vix=40, fear_greed=10, fear_greed_label="Extreme Fear", ts=time.time(),
        ))
        result = await ds.get_macro()
        assert result["macro_kill"] is True

    @pytest.mark.asyncio
    async def test_macro_no_kill_normal(self):
        """Normal conditions → macro_kill = False."""
        ds = DataStore()
        await ds.update_macro(MacroSnapshot(
            vix=18, fear_greed=55, fear_greed_label="Neutral", ts=time.time(),
        ))
        result = await ds.get_macro()
        assert result["macro_kill"] is False


# ════════════════════════════════════════════════════════════
# Narrative — Sentiment + Divergence
# ════════════════════════════════════════════════════════════
from narrative import SentimentEngine, compute_divergence, SentimentSample, DIVERGENCE_THRESHOLD


class TestSentimentEngine:
    @pytest.mark.asyncio
    async def test_keyword_scoring(self):
        engine = SentimentEngine(half_life_h=4.0)
        assert engine.score_text("BTC is bullish, moon incoming!") > 0
        assert engine.score_text("crash dump sell everything bearish") < 0
        assert engine.score_text("the weather is nice today") == 0.0

    @pytest.mark.asyncio
    async def test_aggregate_empty(self):
        engine = SentimentEngine()
        agg = await engine.get_aggregate("BTCUSDT")
        assert agg["sentiment_score"] == 0.0
        assert agg["sample_count"] == 0

    @pytest.mark.asyncio
    async def test_aggregate_bullish(self):
        engine = SentimentEngine(half_life_h=24.0)
        for _ in range(10):
            await engine.add_sample("BTCUSDT", SentimentSample(
                text="bullish moon", score=0.8, source="test", volume=10.0, ts=time.time(),
            ))
        agg = await engine.get_aggregate("BTCUSDT")
        assert agg["sentiment_score"] > 0.5
        assert agg["sample_count"] == 10

    @pytest.mark.asyncio
    async def test_batch_ingest(self):
        engine = SentimentEngine()
        await engine.add_batch("BTCUSDT", [
            {"text": "bullish pump moon", "source": "test", "volume": 5},
            {"text": "crash dump sell", "source": "test", "volume": 5},
        ])
        agg = await engine.get_aggregate("BTCUSDT")
        assert agg["sample_count"] == 2


class TestDivergence:
    def test_aligned(self):
        d = compute_divergence(0.5, 0.5)
        assert d["direction"] == "ALIGNED"
        assert not d["divergence"]

    def test_bullish_divergence(self):
        d = compute_divergence(0.7, -0.2)
        assert d["direction"] == "BULLISH_DIVERGENCE"
        assert d["divergence"]

    def test_bearish_divergence(self):
        d = compute_divergence(-0.6, 0.3)
        assert d["direction"] == "BEARISH_DIVERGENCE"
        assert d["divergence"]

    def test_below_threshold(self):
        d = compute_divergence(0.1, 0.0)
        assert not d["divergence"]


# ════════════════════════════════════════════════════════════
# Jito — Volatility gate + Position management
# ════════════════════════════════════════════════════════════
from jito_spoof import JitoEngine, TokenDiscovery


class TestJitoVolatilityGate:
    def test_valid_volatility(self):
        jito = JitoEngine()
        result = jito.volatility_gate(atr_5m=50.0, mid_price=1000.0)
        # 50/1000 = 0.05 which is within [0.005, 0.15]
        assert result["passed"]

    def test_too_quiet(self):
        jito = JitoEngine()
        result = jito.volatility_gate(atr_5m=0.1, mid_price=1000.0)
        assert not result["passed"]
        assert "too quiet" in result["reason"]

    def test_too_volatile(self):
        jito = JitoEngine()
        result = jito.volatility_gate(atr_5m=200.0, mid_price=1000.0)
        assert not result["passed"]
        assert "too volatile" in result["reason"]

    def test_zero_price(self):
        jito = JitoEngine()
        result = jito.volatility_gate(atr_5m=10.0, mid_price=0.0)
        assert not result["passed"]


class TestJitoPositions:
    @pytest.mark.asyncio
    async def test_open_and_list(self):
        jito = JitoEngine()
        pos = await jito.open_position("MINT123", entry_price=1.0, amount_sol=2.0, atr_1m=0.01)
        assert pos.position_id
        assert pos.amount_sol == 2.0
        positions = await jito.get_active_positions()
        assert len(positions) == 1

    @pytest.mark.asyncio
    async def test_trail_stop_triggers(self):
        jito = JitoEngine()
        pos = await jito.open_position("MINT123", entry_price=1.0, amount_sol=1.0, atr_1m=0.05)
        # Price drops below trail stop
        reason = await jito.update_trail_stop(pos.position_id, 0.5)
        assert reason == "trail_stop"
        positions = await jito.get_active_positions()
        assert len(positions) == 0

    @pytest.mark.asyncio
    async def test_trail_stop_moves_up(self):
        jito = JitoEngine()
        pos = await jito.open_position("MINT123", entry_price=1.0, amount_sol=1.0, atr_1m=0.05)
        initial_stop = pos.trail_stop_price

        # Price goes up → stop should move up
        reason = await jito.update_trail_stop(pos.position_id, 2.0)
        assert reason is None  # Not triggered

        positions = await jito.get_active_positions()
        assert positions[0]["trail_stop_price"] > initial_stop

    @pytest.mark.asyncio
    async def test_emergency_exit(self):
        jito = JitoEngine()
        await jito.open_position("MINT_A", entry_price=1.0, amount_sol=1.0, atr_1m=0.05)
        result = await jito.emergency_exit("MINT_A")
        assert result is not None
        assert len(await jito.get_active_positions()) == 0

    @pytest.mark.asyncio
    async def test_max_position_capped(self):
        jito = JitoEngine()
        pos = await jito.open_position("MINT123", entry_price=1.0, amount_sol=100.0, atr_1m=0.05)
        assert pos.amount_sol <= 5.0  # MAX_POSITION_SOL

    @pytest.mark.asyncio
    async def test_stats(self):
        jito = JitoEngine()
        pos = await jito.open_position("MINT123", entry_price=1.0, amount_sol=1.0, atr_1m=0.05)
        await jito.update_trail_stop(pos.position_id, 0.5)  # Close at loss
        stats = await jito.get_stats()
        assert stats["total_closed"] == 1
        assert stats["losses"] == 1

    @pytest.mark.asyncio
    async def test_discoveries(self):
        jito = JitoEngine()
        await jito.add_discovery(TokenDiscovery(
            mint="MINT_NEW", source="pumpfun", pool_address="POOL",
            initial_price=0.001, initial_liquidity_usd=5000, discovered_at=time.time(),
        ))
        discoveries = await jito.get_recent_discoveries()
        assert len(discoveries) == 1
        assert discoveries[0]["mint"] == "MINT_NEW"


# ════════════════════════════════════════════════════════════
# AntiRug v3 — XGBoost/RF model
# ════════════════════════════════════════════════════════════
from antirug_v3 import _get_rug_prob, model, FEATURES


class TestAntiRugV3:
    def test_obvious_rug(self):
        """Low liquidity, high holder concentration, new token."""
        row = np.array([[
            1000,    # liquidity_usd
            90,      # top_holder_pct
            100,     # dev_wallet_tx_count
            0.5,     # age_hours
            5000,    # volume_24h
            100,     # holders_count
            20,      # buy_tax_pct
            30,      # sell_tax_pct
            0,       # contract_verified
            2,       # deployer_age_days
            5,       # deployer_prev_rugs
            5,       # social_account_age_days
            0,       # funding_divergence_bps
            0,       # liquidity_lock_pct
        ]], dtype=float)
        prob = _get_rug_prob(model, row)
        assert prob > 0.5  # Should be flagged as rug

    def test_obvious_legit(self):
        """High liquidity, low concentration, mature token."""
        row = np.array([[
            500000,  # liquidity_usd
            10,      # top_holder_pct
            5,       # dev_wallet_tx_count
            2000,    # age_hours
            5000000, # volume_24h
            100000,  # holders_count
            1,       # buy_tax_pct
            1,       # sell_tax_pct
            1,       # contract_verified
            500,     # deployer_age_days
            0,       # deployer_prev_rugs
            1000,    # social_account_age_days
            0,       # funding_divergence_bps
            80,      # liquidity_lock_pct
        ]], dtype=float)
        prob = _get_rug_prob(model, row)
        assert prob < 0.3  # Should pass

    def test_feature_count(self):
        assert len(FEATURES) == 14

    def test_model_has_two_classes(self):
        assert len(model.classes_) == 2
