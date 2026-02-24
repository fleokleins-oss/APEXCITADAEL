"""Tests for Newtonian Brother Gravitational Model — G-force, regime, signal."""

import math

import pytest
import numpy as np

from newtonian import (
    rolling_correlation,
    compute_g_force,
    classify_regime,
    GravityEngine,
    ACCEL_THRESHOLD,
    CONTAGION_MULTIPLIER,
)


# ──── Math unit tests ────

class TestRollingCorrelation:
    def test_perfect_positive(self):
        a = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
        b = [2.0, 4.0, 6.0, 8.0, 10.0, 12.0, 14.0, 16.0, 18.0, 20.0]
        corr = rolling_correlation(a, b, 10)
        assert corr == pytest.approx(1.0, abs=0.001)

    def test_perfect_negative(self):
        a = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
        b = [20.0, 18.0, 16.0, 14.0, 12.0, 10.0, 8.0, 6.0, 4.0, 2.0]
        corr = rolling_correlation(a, b, 10)
        assert corr == pytest.approx(-1.0, abs=0.001)

    def test_uncorrelated(self):
        np.random.seed(42)
        a = list(np.random.randn(200))
        b = list(np.random.randn(200))
        corr = rolling_correlation(a, b, 200)
        assert abs(corr) < 0.2  # Should be near zero

    def test_insufficient_data(self):
        corr = rolling_correlation([1.0, 2.0], [3.0, 4.0], 10)
        assert corr == 0.0  # Not enough data

    def test_constant_series(self):
        a = [5.0] * 20
        b = [3.0] * 20
        corr = rolling_correlation(a, b, 20)
        assert corr == 0.0  # Zero std → zero correlation

    def test_uses_window(self):
        """Only the last `window` values should matter."""
        # First 50: uncorrelated noise
        np.random.seed(42)
        a = list(np.random.randn(50))
        b = list(np.random.randn(50))
        # Last 20: perfectly correlated
        tail = list(range(20))
        a.extend(tail)
        b.extend([x * 2 for x in tail])

        corr_full = rolling_correlation(a, b, 70)
        corr_tail = rolling_correlation(a, b, 20)
        # Tail correlation should be higher than full
        assert corr_tail > corr_full


class TestGForce:
    def test_high_correlation_high_force(self):
        f_high = compute_g_force(100.0, 100.0, 0.9)
        f_low = compute_g_force(100.0, 100.0, 0.1)
        assert f_high > f_low  # Higher correlation = smaller distance = more force

    def test_zero_correlation(self):
        f = compute_g_force(100.0, 100.0, 0.0)
        assert f > 0  # Distance = 1.0, still nonzero force

    def test_mass_proportional(self):
        f1 = compute_g_force(100.0, 100.0, 0.5)
        f2 = compute_g_force(200.0, 200.0, 0.5)
        assert f2 > f1  # More mass = more force

    def test_g_constant_scales(self):
        f1 = compute_g_force(100.0, 100.0, 0.5, g_constant=1.0)
        f2 = compute_g_force(100.0, 100.0, 0.5, g_constant=2.0)
        assert f2 == pytest.approx(f1 * 2.0, abs=0.01)

    def test_clamps_distance(self):
        """Very high correlation shouldn't cause infinity (d clamped to 0.05)."""
        f = compute_g_force(100.0, 100.0, 0.999)
        assert math.isfinite(f)
        assert f > 0


class TestRegimeClassification:
    def test_contagion(self):
        r = classify_regime(0.85, ACCEL_THRESHOLD * CONTAGION_MULTIPLIER + 0.1, ACCEL_THRESHOLD, CONTAGION_MULTIPLIER)
        assert r == "CONTAGION"

    def test_convergence(self):
        r = classify_regime(0.7, ACCEL_THRESHOLD + 0.01, ACCEL_THRESHOLD, CONTAGION_MULTIPLIER)
        assert r == "CONVERGENCE"

    def test_divergence(self):
        r = classify_regime(0.6, -(ACCEL_THRESHOLD + 0.01), ACCEL_THRESHOLD, CONTAGION_MULTIPLIER)
        assert r == "DIVERGENCE"

    def test_isolation(self):
        r = classify_regime(0.1, 0.0, ACCEL_THRESHOLD, CONTAGION_MULTIPLIER)
        assert r == "ISOLATION"


# ──── GravityEngine integration tests ────

class TestGravityEngine:
    @pytest.mark.asyncio
    async def test_ingest_and_compute(self):
        engine = GravityEngine(["BTC", "ETH"])
        np.random.seed(42)

        # Correlated returns
        btc_rets = list(np.random.randn(100) * 0.01)
        eth_rets = [r * 0.8 + np.random.randn() * 0.002 for r in btc_rets]

        await engine.ingest_returns("BTC", btc_rets, 50000.0, 1e9)
        await engine.ingest_returns("ETH", eth_rets, 3000.0, 5e8)
        await engine.compute_epoch()

        state = await engine.get_all_state()
        assert state["epochs_computed"] == 1
        assert "BTC_ETH" in state["pairs"]

        pair = state["pairs"]["BTC_ETH"]
        assert pair["correlation"] > 0.5  # Should be positively correlated

    @pytest.mark.asyncio
    async def test_acceleration_computed(self):
        engine = GravityEngine(["BTC", "ETH"])
        np.random.seed(42)

        # Epoch 1: moderate correlation
        btc = list(np.random.randn(100) * 0.01)
        eth = [r * 0.5 + np.random.randn() * 0.005 for r in btc]
        await engine.ingest_returns("BTC", btc, 50000.0, 1e9)
        await engine.ingest_returns("ETH", eth, 3000.0, 5e8)
        await engine.compute_epoch()

        # Epoch 2: higher correlation (assets converging)
        btc2 = list(np.random.randn(100) * 0.01)
        eth2 = [r * 0.95 + np.random.randn() * 0.001 for r in btc2]
        await engine.ingest_returns("BTC", btc2, 51000.0, 1.1e9)
        await engine.ingest_returns("ETH", eth2, 3100.0, 5.5e8)
        await engine.compute_epoch()

        state = await engine.get_all_state()
        assert state["epochs_computed"] == 2
        # Acceleration should be nonzero (correlation increased → force changed)
        pair = state["pairs"]["BTC_ETH"]
        assert pair["acceleration"] != 0.0

    @pytest.mark.asyncio
    async def test_signal_for_asset(self):
        engine = GravityEngine(["BTC", "ETH", "SOL"])
        np.random.seed(42)

        btc = list(np.random.randn(100) * 0.01)
        eth = [r * 0.8 + np.random.randn() * 0.002 for r in btc]
        sol = [r * 0.3 + np.random.randn() * 0.008 for r in btc]

        await engine.ingest_returns("BTC", btc, 50000.0, 1e9)
        await engine.ingest_returns("ETH", eth, 3000.0, 5e8)
        await engine.ingest_returns("SOL", sol, 150.0, 2e8)
        await engine.compute_epoch()

        sig = await engine.signal_for_asset("BTC")
        assert "action" in sig
        assert "side" in sig
        assert "confidence" in sig
        assert "regime" in sig
        assert sig["action"] in ("EXECUTE", "WAIT", "KILL")
        assert isinstance(sig["pairs"], list)
        assert len(sig["pairs"]) == 2  # BTC_ETH and BTC_SOL

    @pytest.mark.asyncio
    async def test_unknown_asset_returns_wait(self):
        engine = GravityEngine(["BTC", "ETH"])
        sig = await engine.signal_for_asset("DOGE")
        assert sig["action"] == "WAIT"
        assert sig["regime"] == "UNKNOWN"

    @pytest.mark.asyncio
    async def test_pair_key_lookup(self):
        engine = GravityEngine(["BTC", "ETH"])
        np.random.seed(42)
        btc = list(np.random.randn(50) * 0.01)
        eth = list(np.random.randn(50) * 0.01)
        await engine.ingest_returns("BTC", btc, 50000.0, 1e9)
        await engine.ingest_returns("ETH", eth, 3000.0, 5e8)
        await engine.compute_epoch()

        pair = await engine.get_pair_state("BTC_ETH")
        assert pair is not None
        assert pair["asset_a"] == "BTC"
        assert pair["asset_b"] == "ETH"

    @pytest.mark.asyncio
    async def test_global_regime(self):
        engine = GravityEngine(["BTC", "ETH"])
        np.random.seed(42)
        btc = list(np.random.randn(100) * 0.01)
        eth = [r * 0.9 + np.random.randn() * 0.001 for r in btc]  # Highly correlated
        await engine.ingest_returns("BTC", btc, 50000.0, 1e9)
        await engine.ingest_returns("ETH", eth, 3000.0, 5e8)
        await engine.compute_epoch()

        state = await engine.get_all_state()
        # High correlation should yield CONVERGENCE (or possibly CONTAGION)
        assert state["global_regime"] in ("CONVERGENCE", "CONTAGION")
