# ===================================================================
# APEX DREAMERV3 LATENT IMAGINATION NODE v3.0 (Port 8006)
#
# Reinforcement learning world model inspired by DreamerV3 (Hafner et al.)
# Instead of just reacting, this node:
#   1. Encodes recent market observations into a latent state
#   2. Uses learned dynamics to roll out thousands of imagined futures
#   3. Evaluates each trajectory with a value model
#   4. Returns the action with highest expected return
#
# Roles: DIRECTION + RISK (provides both signal and risk multiplier)
#
# Simplified architecture (no GPU required):
#   - Encoder: Observation → Latent State (numpy, lightweight)
#   - Dynamics: Latent + Action → Next Latent (learned transition)
#   - Reward: Latent → Expected Return
#   - Policy: Latent → Action Distribution
#
# Can operate in two modes:
#   A) PRETRAINED: Load a trained model from disk
#   B) ONLINE:     Train continuously from live data (slower)
#   C) HEURISTIC:  Fallback regime-based model (no training needed)
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

import numpy as np
from dotenv import load_dotenv
from fastapi import FastAPI
from pydantic import BaseModel, Field

from apex_common.logging import get_logger
from apex_common.metrics import instrument_app
from apex_common.security import check_env_file_permissions

load_dotenv()
log = get_logger("dreamer")
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


# Latent space
LATENT_DIM = _i("DREAMER_LATENT_DIM", 64)
OBS_DIM = _i("DREAMER_OBS_DIM", 12)           # Number of input features
HIDDEN_DIM = _i("DREAMER_HIDDEN_DIM", 128)

# Imagination
N_TRAJECTORIES = _i("DREAMER_N_TRAJECTORIES", 500)
HORIZON = _i("DREAMER_HORIZON", 15)            # steps to imagine ahead
DISCOUNT = _f("DREAMER_DISCOUNT", 0.99)

# Training
LEARNING_RATE = _f("DREAMER_LR", 0.001)
REPLAY_BUFFER_SIZE = _i("DREAMER_REPLAY_SIZE", 10000)
BATCH_SIZE = _i("DREAMER_BATCH_SIZE", 32)
TRAIN_INTERVAL_S = _f("DREAMER_TRAIN_INTERVAL_S", 60.0)
MIN_REPLAY_SIZE = _i("DREAMER_MIN_REPLAY_SIZE", 200)

# Mode
MODE = _env("DREAMER_MODE", "heuristic").lower()  # "heuristic" | "online" | "pretrained"
MODEL_PATH = _env("DREAMER_MODEL_PATH", "")

# Observation window
OBS_WINDOW = _i("DREAMER_OBS_WINDOW", 60)


# ────────────────────────────────────────────────────
# Observation builder
# ────────────────────────────────────────────────────
@dataclass
class MarketObservation:
    """Flattened market state vector for the world model.

    12 features:
      0: log_return_1       (1-step log return)
      1: log_return_5       (5-step log return)
      2: log_return_15      (15-step log return)
      3: volatility_20      (20-period realized vol)
      4: volume_ratio       (current vol / avg vol)
      5: funding_rate       (from EconoPredator)
      6: oi_delta_pct       (OI change %)
      7: obi                (order book imbalance)
      8: vpin               (from APM)
      9: ghost_intensity    (from SpoofHunter: 0/1/2)
      10: fear_greed_norm   (0-1 normalized fear & greed)
      11: regime_code       (0=isolation, 1=convergence, 2=divergence, 3=contagion)
    """
    features: np.ndarray  # shape (OBS_DIM,)
    ts: float = 0.0


class ObservationBuilder:
    """Constructs observation vectors from upstream node data."""

    def __init__(self, window: int = OBS_WINDOW):
        self.prices: deque[float] = deque(maxlen=window + 20)
        self.volumes: deque[float] = deque(maxlen=window + 20)
        self.last_obs: Optional[MarketObservation] = None

    def _log_return(self, n: int) -> float:
        if len(self.prices) < n + 1:
            return 0.0
        return math.log(max(1e-12, self.prices[-1]) / max(1e-12, self.prices[-1 - n]))

    def _realized_vol(self, n: int) -> float:
        if len(self.prices) < n + 1:
            return 0.0
        rets = [
            math.log(max(1e-12, self.prices[i]) / max(1e-12, self.prices[i - 1]))
            for i in range(max(1, len(self.prices) - n), len(self.prices))
        ]
        if len(rets) < 2:
            return 0.0
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / len(rets)
        return math.sqrt(max(0, var))

    def _volume_ratio(self) -> float:
        if len(self.volumes) < 10:
            return 1.0
        avg = sum(list(self.volumes)[-20:]) / min(20, len(self.volumes))
        return self.volumes[-1] / max(1e-12, avg) if avg > 0 else 1.0

    def build(
        self,
        price: float,
        volume: float,
        *,
        funding_rate: float = 0.0,
        oi_delta_pct: float = 0.0,
        obi: float = 0.0,
        vpin: float = 0.0,
        ghost_intensity: int = 0,
        fear_greed: int = 50,
        regime_code: int = 0,
    ) -> MarketObservation:
        """Build observation vector from current market state."""
        self.prices.append(price)
        self.volumes.append(volume)

        features = np.array([
            self._log_return(1),
            self._log_return(5),
            self._log_return(15),
            self._realized_vol(20),
            self._volume_ratio(),
            funding_rate,
            oi_delta_pct / 100.0,  # normalize to [-1, 1] range
            obi,
            vpin,
            ghost_intensity / 2.0,  # normalize: 0, 0.5, 1.0
            fear_greed / 100.0,     # normalize to [0, 1]
            regime_code / 3.0,      # normalize: 0, 0.33, 0.67, 1.0
        ], dtype=np.float32)

        obs = MarketObservation(features=features, ts=time.time())
        self.last_obs = obs
        return obs


# ────────────────────────────────────────────────────
# World Model Components (numpy-based, no PyTorch/TF)
# ────────────────────────────────────────────────────
def _xavier_init(fan_in: int, fan_out: int) -> np.ndarray:
    limit = math.sqrt(6.0 / (fan_in + fan_out))
    return np.random.uniform(-limit, limit, (fan_in, fan_out)).astype(np.float32)


def _relu(x: np.ndarray) -> np.ndarray:
    return np.maximum(0, x)


def _tanh(x: np.ndarray) -> np.ndarray:
    return np.tanh(x)


def _softmax(x: np.ndarray) -> np.ndarray:
    e = np.exp(x - np.max(x))
    return e / (np.sum(e) + 1e-8)


class WorldModel:
    """Lightweight world model with encoder, dynamics, reward, and policy heads.

    All operations are numpy-based for zero-dependency inference.
    Architecture:
      Encoder:  obs(12) → hidden(128) → latent(64)
      Dynamics: latent(64) + action(3) → hidden(128) → next_latent(64)
      Reward:   latent(64) → hidden(64) → scalar
      Policy:   latent(64) → hidden(64) → action_logits(3)
    """

    def __init__(
        self,
        obs_dim: int = OBS_DIM,
        latent_dim: int = LATENT_DIM,
        hidden_dim: int = HIDDEN_DIM,
        n_actions: int = 3,  # LONG, SHORT, FLAT
    ):
        self.obs_dim = obs_dim
        self.latent_dim = latent_dim
        self.hidden_dim = hidden_dim
        self.n_actions = n_actions
        np.random.seed(42)

        # Encoder: obs → latent
        self.enc_w1 = _xavier_init(obs_dim, hidden_dim)
        self.enc_b1 = np.zeros(hidden_dim, dtype=np.float32)
        self.enc_w2 = _xavier_init(hidden_dim, latent_dim)
        self.enc_b2 = np.zeros(latent_dim, dtype=np.float32)

        # Dynamics: (latent + action_onehot) → next_latent
        self.dyn_w1 = _xavier_init(latent_dim + n_actions, hidden_dim)
        self.dyn_b1 = np.zeros(hidden_dim, dtype=np.float32)
        self.dyn_w2 = _xavier_init(hidden_dim, latent_dim)
        self.dyn_b2 = np.zeros(latent_dim, dtype=np.float32)

        # Reward: latent → scalar
        self.rew_w1 = _xavier_init(latent_dim, 64)
        self.rew_b1 = np.zeros(64, dtype=np.float32)
        self.rew_w2 = _xavier_init(64, 1)
        self.rew_b2 = np.zeros(1, dtype=np.float32)

        # Policy: latent → action logits
        self.pol_w1 = _xavier_init(latent_dim, 64)
        self.pol_b1 = np.zeros(64, dtype=np.float32)
        self.pol_w2 = _xavier_init(64, n_actions)
        self.pol_b2 = np.zeros(n_actions, dtype=np.float32)

    def encode(self, obs: np.ndarray) -> np.ndarray:
        """Encode observation into latent state."""
        h = _relu(obs @ self.enc_w1 + self.enc_b1)
        z = _tanh(h @ self.enc_w2 + self.enc_b2)
        return z

    def imagine_step(self, latent: np.ndarray, action: int) -> Tuple[np.ndarray, float]:
        """Imagine one step forward: (latent, action) → (next_latent, reward)."""
        action_oh = np.zeros(self.n_actions, dtype=np.float32)
        action_oh[action] = 1.0
        inp = np.concatenate([latent, action_oh])
        h = _relu(inp @ self.dyn_w1 + self.dyn_b1)
        next_latent = _tanh(h @ self.dyn_w2 + self.dyn_b2)

        # Reward
        rh = _relu(next_latent @ self.rew_w1 + self.rew_b1)
        reward = float((rh @ self.rew_w2 + self.rew_b2)[0])

        return next_latent, reward

    def policy(self, latent: np.ndarray) -> np.ndarray:
        """Get action probabilities from latent state."""
        h = _relu(latent @ self.pol_w1 + self.pol_b1)
        logits = h @ self.pol_w2 + self.pol_b2
        return _softmax(logits)

    def imagine_trajectory(
        self,
        start_latent: np.ndarray,
        horizon: int = HORIZON,
        discount: float = DISCOUNT,
    ) -> Tuple[int, float]:
        """Roll out one imagined trajectory using the policy.

        Returns: (first_action, discounted_return)
        """
        latent = start_latent.copy()
        total_return = 0.0
        first_action = -1

        for t in range(horizon):
            probs = self.policy(latent)
            action = int(np.random.choice(self.n_actions, p=probs))
            if t == 0:
                first_action = action

            latent, reward = self.imagine_step(latent, action)
            total_return += reward * (discount ** t)

        return first_action, total_return

    def best_action(
        self,
        obs: np.ndarray,
        n_trajectories: int = N_TRAJECTORIES,
        horizon: int = HORIZON,
    ) -> Tuple[int, float, np.ndarray]:
        """Run N imagined trajectories and return the best first action.

        Returns: (action, expected_return, action_returns)
        """
        latent = self.encode(obs)

        # Accumulate returns per first action
        action_returns: Dict[int, List[float]] = {a: [] for a in range(self.n_actions)}

        for _ in range(n_trajectories):
            first_act, ret = self.imagine_trajectory(latent, horizon)
            action_returns[first_act].append(ret)

        # Average return per action
        avg_returns = np.zeros(self.n_actions)
        for a in range(self.n_actions):
            if action_returns[a]:
                avg_returns[a] = np.mean(action_returns[a])
            else:
                avg_returns[a] = -999.0

        best = int(np.argmax(avg_returns))
        return best, float(avg_returns[best]), avg_returns

    def save(self, path: str):
        """Save model weights."""
        weights = {
            "enc_w1": self.enc_w1, "enc_b1": self.enc_b1,
            "enc_w2": self.enc_w2, "enc_b2": self.enc_b2,
            "dyn_w1": self.dyn_w1, "dyn_b1": self.dyn_b1,
            "dyn_w2": self.dyn_w2, "dyn_b2": self.dyn_b2,
            "rew_w1": self.rew_w1, "rew_b1": self.rew_b1,
            "rew_w2": self.rew_w2, "rew_b2": self.rew_b2,
            "pol_w1": self.pol_w1, "pol_b1": self.pol_b1,
            "pol_w2": self.pol_w2, "pol_b2": self.pol_b2,
        }
        np.savez_compressed(path, **weights)
        log.info(f"Model saved: {path}")

    def load(self, path: str):
        """Load model weights."""
        data = np.load(path)
        for key in data.files:
            setattr(self, key, data[key])
        log.info(f"Model loaded: {path}")


# ────────────────────────────────────────────────────
# Replay buffer for online learning
# ────────────────────────────────────────────────────
@dataclass
class Transition:
    obs: np.ndarray
    action: int
    reward: float
    next_obs: np.ndarray
    done: bool = False


class ReplayBuffer:
    def __init__(self, capacity: int = REPLAY_BUFFER_SIZE):
        self._buffer: deque[Transition] = deque(maxlen=capacity)

    def add(self, t: Transition):
        self._buffer.append(t)

    def sample(self, batch_size: int) -> List[Transition]:
        indices = np.random.randint(0, len(self._buffer), size=min(batch_size, len(self._buffer)))
        return [self._buffer[i] for i in indices]

    def __len__(self) -> int:
        return len(self._buffer)


# ────────────────────────────────────────────────────
# Heuristic model (no training needed)
# ────────────────────────────────────────────────────
class HeuristicModel:
    """Rule-based imagination that uses statistical patterns.

    Combines momentum, mean-reversion, and regime signals
    to produce action probabilities without any learned weights.
    """

    def __init__(self):
        self.n_actions = 3  # 0=LONG, 1=SHORT, 2=FLAT

    def evaluate(self, obs: np.ndarray) -> Tuple[int, float, np.ndarray]:
        """Evaluate observation and return action + confidence.

        Returns: (action, confidence, action_scores)
        """
        # Unpack features
        lr1 = float(obs[0])    # log_return_1
        lr5 = float(obs[1])    # log_return_5
        lr15 = float(obs[2])   # log_return_15
        vol = float(obs[3])    # volatility
        vol_ratio = float(obs[4])
        funding = float(obs[5])
        oi_delta = float(obs[6])
        obi = float(obs[7])
        vpin = float(obs[8])
        ghost = float(obs[9])
        fear_greed = float(obs[10])
        regime = float(obs[11])

        # Score accumulation
        long_score = 0.0
        short_score = 0.0
        flat_score = 0.0

        # ── Momentum (lr5 + lr15 alignment) ──
        if lr5 > 0.002 and lr15 > 0.005:
            long_score += 2.0
        elif lr5 < -0.002 and lr15 < -0.005:
            short_score += 2.0
        else:
            flat_score += 0.5

        # ── Order book pressure (OBI) ──
        if obi > 0.4:
            long_score += 1.5
        elif obi < -0.4:
            short_score += 1.5

        # ── VPIN toxicity → go flat or contrarian ──
        if vpin > 0.7:
            flat_score += 3.0  # Toxic flow → step aside
            long_score -= 1.0
        elif vpin > 0.5:
            flat_score += 1.0

        # ── Ghost liquidity → contrarian ──
        if ghost > 0.5:
            # Ghosts detected — spoofing happening
            # Contrarian: if bid ghosts (ghost > 0.5), go short
            short_score += 1.0
            long_score -= 0.5

        # ── Funding rate ──
        if funding > 0.0005:
            short_score += 1.0  # Longs paying shorts → crowded long
        elif funding < -0.0003:
            long_score += 1.0   # Shorts paying longs → crowded short

        # ── OI delta ──
        if oi_delta > 0.02:
            long_score += 0.5   # Rising OI with price up = new longs
        elif oi_delta < -0.02:
            short_score += 0.5  # Falling OI = deleveraging

        # ── Fear & Greed ──
        if fear_greed < 0.25:
            long_score += 0.8   # Extreme fear → contrarian buy
        elif fear_greed > 0.75:
            short_score += 0.5  # Extreme greed → caution

        # ── Regime (from Newtonian) ──
        if regime > 0.9:  # CONTAGION
            flat_score += 5.0   # KILL everything
            long_score -= 2.0
            short_score -= 2.0
        elif regime > 0.6:  # DIVERGENCE
            flat_score += 1.0

        # ── Volume surge ──
        if vol_ratio > 2.5:
            # Volume spike: amplify existing direction signal
            if long_score > short_score:
                long_score *= 1.3
            else:
                short_score *= 1.3

        scores = np.array([long_score, short_score, flat_score])
        action = int(np.argmax(scores))

        # Confidence: softmax-style normalization
        exp_scores = np.exp(scores - np.max(scores))
        probs = exp_scores / (np.sum(exp_scores) + 1e-8)
        confidence = float(probs[action])

        return action, confidence, scores


# ────────────────────────────────────────────────────
# DreamerV3 Engine
# ────────────────────────────────────────────────────
ACTION_MAP = {0: "LONG", 1: "SHORT", 2: "FLAT"}
SIDE_MAP = {0: "LONG", 1: "SHORT", 2: "NONE"}


class DreamerEngine:
    """Main engine combining world model + heuristic + observation builder."""

    def __init__(self, mode: str = MODE):
        self.mode = mode
        self._lock = asyncio.Lock()
        self.obs_builders: Dict[str, ObservationBuilder] = {}
        self.world_model = WorldModel()
        self.heuristic = HeuristicModel()
        self.replay = ReplayBuffer()
        self.epochs_trained: int = 0
        self.signals_generated: int = 0

        if mode == "pretrained" and MODEL_PATH:
            try:
                self.world_model.load(MODEL_PATH)
                log.info(f"DreamerV3 loaded pretrained model: {MODEL_PATH}")
            except Exception as e:
                log.warning(f"Failed to load pretrained model, falling back to heuristic: {e}")
                self.mode = "heuristic"

        log.info(f"DreamerV3 engine initialized: mode={self.mode}")

    def _get_builder(self, symbol: str) -> ObservationBuilder:
        s = symbol.upper()
        if s not in self.obs_builders:
            self.obs_builders[s] = ObservationBuilder()
        return self.obs_builders[s]

    async def ingest_tick(
        self,
        symbol: str,
        price: float,
        volume: float = 0.0,
        **kwargs,
    ) -> MarketObservation:
        """Ingest a market tick and build observation."""
        async with self._lock:
            builder = self._get_builder(symbol)
            return builder.build(price, volume, **kwargs)

    async def imagine(self, symbol: str) -> dict:
        """Generate an imagination signal for a symbol.

        Returns full signal with action, side, confidence, and imagination details.
        """
        async with self._lock:
            builder = self._get_builder(symbol)
            if builder.last_obs is None:
                return self._empty_signal()

            obs = builder.last_obs.features
            self.signals_generated += 1

            if self.mode == "heuristic":
                action, confidence, scores = self.heuristic.evaluate(obs)
                method = "heuristic"
                trajectories_run = 0
            else:
                action, expected_return, avg_returns = self.world_model.best_action(
                    obs, N_TRAJECTORIES, HORIZON,
                )
                # Normalize expected return to confidence [0, 1]
                confidence = float(1.0 / (1.0 + math.exp(-expected_return)))  # sigmoid
                scores = avg_returns
                method = "world_model"
                trajectories_run = N_TRAJECTORIES

            side = SIDE_MAP.get(action, "NONE")
            action_name = ACTION_MAP.get(action, "FLAT")

            # Risk multiplier: higher confidence → allow more risk
            # FLAT action → risk_multiplier = 0 (no trade)
            if action == 2:  # FLAT
                risk_mult = 0.0
                act = "WAIT"
            elif confidence >= 0.55:
                risk_mult = min(1.0, confidence)
                act = "EXECUTE"
            else:
                risk_mult = 0.0
                act = "WAIT"

            return {
                "action": act,
                "side": side,
                "confidence": round(confidence, 4),
                "risk_multiplier": round(risk_mult, 4),
                "imagination": {
                    "method": method,
                    "chosen_action": action_name,
                    "trajectories_run": trajectories_run,
                    "horizon": HORIZON,
                    "action_scores": {
                        "LONG": round(float(scores[0]), 4),
                        "SHORT": round(float(scores[1]), 4),
                        "FLAT": round(float(scores[2]), 4),
                    },
                },
                "epochs_trained": self.epochs_trained,
                "signals_generated": self.signals_generated,
            }

    def _empty_signal(self) -> dict:
        return {
            "action": "WAIT",
            "side": "NONE",
            "confidence": 0.0,
            "risk_multiplier": 0.0,
            "imagination": {"method": "none", "reason": "no observations yet"},
        }

    async def add_experience(self, symbol: str, action: int, reward: float, done: bool = False):
        """Add a completed experience for online learning."""
        async with self._lock:
            builder = self._get_builder(symbol)
            if builder.last_obs is not None and len(builder.prices) >= 2:
                obs = builder.last_obs.features
                # Build next obs from latest data
                next_obs = obs.copy()  # Simplified: real impl would use actual next state
                self.replay.add(Transition(
                    obs=obs, action=action, reward=reward, next_obs=next_obs, done=done,
                ))

    async def train_step(self):
        """One training step from replay buffer (online mode only)."""
        if self.mode != "online" or len(self.replay) < MIN_REPLAY_SIZE:
            return

        async with self._lock:
            batch = self.replay.sample(BATCH_SIZE)
            # Simplified gradient-free training: nudge weights toward observed rewards
            # In production: use proper backprop (torch/jax)
            for t in batch:
                latent = self.world_model.encode(t.obs)
                _, predicted_reward = self.world_model.imagine_step(latent, t.action)
                error = t.reward - predicted_reward

                # Crude weight update (reward head only)
                rh = _relu(latent @ self.world_model.rew_w1 + self.world_model.rew_b1)
                self.world_model.rew_w2 += LEARNING_RATE * error * rh.reshape(-1, 1)
                self.world_model.rew_b2 += LEARNING_RATE * error

            self.epochs_trained += 1


# ────────────────────────────────────────────────────
# FastAPI app
# ────────────────────────────────────────────────────
engine = DreamerEngine()
stop_event = asyncio.Event()
train_task: Optional[asyncio.Task] = None


async def training_loop(stop: asyncio.Event):
    """Periodic training loop for online mode."""
    while not stop.is_set():
        try:
            await engine.train_step()
        except Exception as e:
            log.error(f"Training error: {e}")
        try:
            await asyncio.wait_for(stop.wait(), timeout=TRAIN_INTERVAL_S)
            break
        except asyncio.TimeoutError:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    global train_task
    if MODE == "online":
        train_task = asyncio.create_task(training_loop(stop_event))
    log.info(f"DreamerV3 node online: mode={engine.mode}")
    yield
    stop_event.set()
    if train_task:
        train_task.cancel()


app = FastAPI(title="Apex DreamerV3 Latent Imagination", version="3.0.0", lifespan=lifespan)
instrument_app(app)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "dreamer",
        "version": app.version,
        "mode": engine.mode,
        "latent_dim": LATENT_DIM,
        "n_trajectories": N_TRAJECTORIES,
        "horizon": HORIZON,
        "epochs_trained": engine.epochs_trained,
        "signals_generated": engine.signals_generated,
        "replay_buffer_size": len(engine.replay),
        "symbols_tracked": list(engine.obs_builders.keys()),
    }


@app.get("/imagination_signal/{symbol}")
async def imagination_signal(symbol: str):
    """NodeSignal-compatible output for the Master Orchestrator."""
    return await engine.imagine(symbol)


class IngestTickRequest(BaseModel):
    symbol: str
    price: float = Field(..., gt=0)
    volume: float = Field(0.0, ge=0)
    funding_rate: float = 0.0
    oi_delta_pct: float = 0.0
    obi: float = 0.0
    vpin: float = 0.0
    ghost_intensity: int = Field(0, ge=0, le=2)
    fear_greed: int = Field(50, ge=0, le=100)
    regime_code: int = Field(0, ge=0, le=3)


@app.post("/ingest_tick")
async def ingest_tick(req: IngestTickRequest):
    """Feed a market tick for observation building."""
    obs = await engine.ingest_tick(
        req.symbol, req.price, req.volume,
        funding_rate=req.funding_rate,
        oi_delta_pct=req.oi_delta_pct,
        obi=req.obi,
        vpin=req.vpin,
        ghost_intensity=req.ghost_intensity,
        fear_greed=req.fear_greed,
        regime_code=req.regime_code,
    )
    return {"status": "ok", "features": obs.features.tolist()}


class ExperienceRequest(BaseModel):
    symbol: str
    action: int = Field(..., ge=0, le=2)
    reward: float
    done: bool = False


@app.post("/add_experience")
async def add_experience(req: ExperienceRequest):
    """Add a completed experience for online learning."""
    await engine.add_experience(req.symbol, req.action, req.reward, req.done)
    return {"status": "ok", "replay_size": len(engine.replay)}


@app.get("/latent_state/{symbol}")
async def latent_state(symbol: str):
    """Get the current latent encoding for a symbol."""
    builder = engine._get_builder(symbol.upper())
    if builder.last_obs is None:
        return {"error": "no observations yet"}
    latent = engine.world_model.encode(builder.last_obs.features)
    return {
        "symbol": symbol.upper(),
        "latent": latent.tolist(),
        "latent_dim": len(latent),
        "latent_norm": float(np.linalg.norm(latent)),
    }
