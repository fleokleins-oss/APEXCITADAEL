"""Tests for DreamerV3 Latent Imagination Node."""

import asyncio
import math

import numpy as np
import pytest

from dreamer import (
    WorldModel,
    ObservationBuilder,
    HeuristicModel,
    DreamerEngine,
    ReplayBuffer,
    Transition,
    MarketObservation,
    OBS_DIM,
    LATENT_DIM,
    HORIZON,
)


# ════════════════════════════════════════════════════
# World Model
# ════════════════════════════════════════════════════
class TestWorldModel:
    def test_encode_shape(self):
        wm = WorldModel()
        obs = np.random.randn(OBS_DIM).astype(np.float32)
        latent = wm.encode(obs)
        assert latent.shape == (LATENT_DIM,)

    def test_encode_deterministic(self):
        wm = WorldModel()
        obs = np.random.randn(OBS_DIM).astype(np.float32)
        z1 = wm.encode(obs)
        z2 = wm.encode(obs)
        np.testing.assert_array_almost_equal(z1, z2)

    def test_imagine_step(self):
        wm = WorldModel()
        latent = np.random.randn(LATENT_DIM).astype(np.float32)
        next_latent, reward = wm.imagine_step(latent, action=0)
        assert next_latent.shape == (LATENT_DIM,)
        assert isinstance(reward, float)
        assert math.isfinite(reward)

    def test_policy_sums_to_one(self):
        wm = WorldModel()
        latent = np.random.randn(LATENT_DIM).astype(np.float32)
        probs = wm.policy(latent)
        assert probs.shape == (3,)
        assert abs(probs.sum() - 1.0) < 0.01
        assert all(p >= 0 for p in probs)

    def test_imagine_trajectory(self):
        wm = WorldModel()
        obs = np.random.randn(OBS_DIM).astype(np.float32)
        latent = wm.encode(obs)
        first_action, total_return = wm.imagine_trajectory(latent, horizon=5)
        assert first_action in (0, 1, 2)
        assert math.isfinite(total_return)

    def test_best_action(self):
        wm = WorldModel()
        obs = np.random.randn(OBS_DIM).astype(np.float32)
        action, expected_ret, avg_rets = wm.best_action(obs, n_trajectories=50, horizon=5)
        assert action in (0, 1, 2)
        assert math.isfinite(expected_ret)
        assert avg_rets.shape == (3,)

    def test_save_load(self, tmp_path):
        wm = WorldModel()
        obs = np.random.randn(OBS_DIM).astype(np.float32)
        z_before = wm.encode(obs)

        path = str(tmp_path / "test_model.npz")
        wm.save(path)

        wm2 = WorldModel()
        wm2.load(path)
        z_after = wm2.encode(obs)
        np.testing.assert_array_almost_equal(z_before, z_after)


# ════════════════════════════════════════════════════
# Observation Builder
# ════════════════════════════════════════════════════
class TestObservationBuilder:
    def test_build_basic(self):
        builder = ObservationBuilder()
        obs = builder.build(50000.0, 10.0)
        assert obs.features.shape == (OBS_DIM,)
        assert obs.ts > 0

    def test_log_returns_populated(self):
        builder = ObservationBuilder()
        for i in range(20):
            obs = builder.build(50000.0 + i * 10.0, 5.0)
        # log_return_1 should be positive (price increasing)
        assert obs.features[0] > 0  # lr1

    def test_volume_ratio(self):
        builder = ObservationBuilder()
        # Normal volume
        for _ in range(20):
            builder.build(50000.0, 10.0)
        # Volume spike
        obs = builder.build(50000.0, 50.0)
        assert obs.features[4] > 1.0  # vol_ratio should be elevated

    def test_features_normalized(self):
        builder = ObservationBuilder()
        obs = builder.build(
            50000.0, 10.0,
            fear_greed=80,
            regime_code=3,
            ghost_intensity=2,
        )
        # Check normalization ranges
        assert 0 <= obs.features[10] <= 1.0  # fear_greed_norm
        assert 0 <= obs.features[11] <= 1.0  # regime_code_norm
        assert 0 <= obs.features[9] <= 1.0   # ghost_norm


# ════════════════════════════════════════════════════
# Heuristic Model
# ════════════════════════════════════════════════════
class TestHeuristicModel:
    def test_bullish_signal(self):
        """Strong momentum + positive OBI → LONG."""
        h = HeuristicModel()
        obs = np.array([
            0.01,   # lr1 (strong up)
            0.02,   # lr5 (strong up)
            0.04,   # lr15 (strong up)
            0.01,   # vol
            1.5,    # vol_ratio
            -0.0001, # funding (neutral)
            0.03,   # oi_delta (rising)
            0.6,    # obi (bullish)
            0.2,    # vpin (low)
            0.0,    # ghost (none)
            0.5,    # fear_greed (neutral)
            0.33,   # regime (convergence)
        ], dtype=np.float32)
        action, conf, scores = h.evaluate(obs)
        assert action == 0  # LONG
        assert conf > 0.3

    def test_toxic_vpin_goes_flat(self):
        """High VPIN → FLAT."""
        h = HeuristicModel()
        obs = np.zeros(OBS_DIM, dtype=np.float32)
        obs[8] = 0.8  # vpin toxic
        action, conf, scores = h.evaluate(obs)
        assert action == 2  # FLAT
        assert scores[2] > scores[0]  # flat > long

    def test_contagion_goes_flat(self):
        """CONTAGION regime → FLAT."""
        h = HeuristicModel()
        obs = np.zeros(OBS_DIM, dtype=np.float32)
        obs[11] = 1.0  # regime = contagion
        action, conf, scores = h.evaluate(obs)
        assert action == 2  # FLAT

    def test_extreme_fear_contrarian(self):
        """Extreme fear + low VPIN → lean LONG (contrarian)."""
        h = HeuristicModel()
        obs = np.zeros(OBS_DIM, dtype=np.float32)
        obs[10] = 0.15  # extreme fear
        obs[8] = 0.1    # low vpin
        action, conf, scores = h.evaluate(obs)
        # Long score should have some boost from fear
        assert scores[0] > 0  # LONG score positive

    def test_crowded_funding(self):
        """High positive funding → lean SHORT."""
        h = HeuristicModel()
        obs = np.zeros(OBS_DIM, dtype=np.float32)
        obs[5] = 0.001  # high funding (longs paying)
        action, conf, scores = h.evaluate(obs)
        assert scores[1] > 0  # SHORT has positive score


# ════════════════════════════════════════════════════
# Replay Buffer
# ════════════════════════════════════════════════════
class TestReplayBuffer:
    def test_add_and_sample(self):
        rb = ReplayBuffer(capacity=100)
        for i in range(50):
            rb.add(Transition(
                obs=np.zeros(OBS_DIM), action=0, reward=1.0, next_obs=np.zeros(OBS_DIM),
            ))
        assert len(rb) == 50
        batch = rb.sample(10)
        assert len(batch) == 10

    def test_overflow(self):
        rb = ReplayBuffer(capacity=10)
        for i in range(20):
            rb.add(Transition(
                obs=np.zeros(OBS_DIM), action=0, reward=float(i), next_obs=np.zeros(OBS_DIM),
            ))
        assert len(rb) == 10  # Capped


# ════════════════════════════════════════════════════
# DreamerEngine Integration
# ════════════════════════════════════════════════════
class TestDreamerEngine:
    @pytest.mark.asyncio
    async def test_ingest_and_imagine(self):
        eng = DreamerEngine(mode="heuristic")
        # Feed some ticks
        for i in range(30):
            await eng.ingest_tick("BTCUSDT", 50000 + i * 10, volume=5.0, obi=0.3)

        signal = await eng.imagine("BTCUSDT")
        assert "action" in signal
        assert "side" in signal
        assert "confidence" in signal
        assert "risk_multiplier" in signal
        assert "imagination" in signal
        assert signal["imagination"]["method"] == "heuristic"

    @pytest.mark.asyncio
    async def test_no_obs_returns_wait(self):
        eng = DreamerEngine(mode="heuristic")
        signal = await eng.imagine("ETHUSDT")
        assert signal["action"] == "WAIT"
        assert signal["confidence"] == 0.0

    @pytest.mark.asyncio
    async def test_world_model_mode(self):
        eng = DreamerEngine(mode="online")
        for i in range(30):
            await eng.ingest_tick("BTCUSDT", 50000 + i * 10, volume=5.0)

        signal = await eng.imagine("BTCUSDT")
        assert signal["action"] in ("EXECUTE", "WAIT")
        assert signal["imagination"]["method"] == "world_model"
        assert signal["imagination"]["trajectories_run"] > 0

    @pytest.mark.asyncio
    async def test_add_experience(self):
        eng = DreamerEngine(mode="online")
        for i in range(5):
            await eng.ingest_tick("BTCUSDT", 50000 + i * 10, volume=5.0)
        await eng.add_experience("BTCUSDT", action=0, reward=0.5)
        assert len(eng.replay) == 1

    @pytest.mark.asyncio
    async def test_signals_counter(self):
        eng = DreamerEngine(mode="heuristic")
        for i in range(10):
            await eng.ingest_tick("BTCUSDT", 50000, volume=1.0)
        await eng.imagine("BTCUSDT")
        await eng.imagine("BTCUSDT")
        assert eng.signals_generated == 2

    @pytest.mark.asyncio
    async def test_risk_multiplier_flat(self):
        """FLAT action → risk_multiplier = 0."""
        eng = DreamerEngine(mode="heuristic")
        # Feed toxic VPIN + contagion regime to force FLAT
        for i in range(20):
            await eng.ingest_tick(
                "BTCUSDT", 50000, volume=1.0,
                vpin=0.9, regime_code=3,  # CONTAGION
            )
        signal = await eng.imagine("BTCUSDT")
        if signal["imagination"].get("chosen_action") == "FLAT":
            assert signal["risk_multiplier"] == 0.0
