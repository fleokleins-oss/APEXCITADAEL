"""Apex Citadel v3 — Centralized configuration."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Dict, List

def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default

def _i(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default

def _s(name: str, default: str) -> str:
    return os.getenv(name, default)

def _b(name: str, default: bool) -> bool:
    v = os.getenv(name, str(default)).strip().upper()
    return v in ("1", "TRUE", "YES", "Y", "ON")

def _list(name: str, default: str) -> List[str]:
    raw = os.getenv(name, default).strip()
    if not raw:
        return []
    return [s.strip() for s in raw.split(",") if s.strip()]

def _json_dict(name: str, default: str = "{}") -> Dict:
    raw = os.getenv(name, default).strip()
    try:
        return json.loads(raw)
    except Exception:
        return {}


# ────────────────────────────────────────────────────
# Legacy configs (carried forward from v2, unchanged)
# ────────────────────────────────────────────────────

@dataclass(frozen=True)
class BrainConfig:
    max_drawdown_pct: float = _f("BRAIN_MAX_DRAWDOWN_PCT", 8.0)
    funding_fear_threshold: float = _f("BRAIN_FUNDING_FEAR_THRESHOLD", 0.00015)
    hill_k: int = _i("BRAIN_HILL_K", 20)
    tail_alpha_warn: float = _f("BRAIN_TAIL_ALPHA_WARN", 1.5)
    tail_alpha_kill: float = _f("BRAIN_TAIL_ALPHA_KILL", 1.1)
    contagion_corr_warn: float = _f("BRAIN_CONTAGION_CORR_WARN", 0.85)

@dataclass(frozen=True)
class ShadowglassConfig:
    default_symbol: str = _s("SHADOWGLASS_DEFAULT_SYMBOL", "btcusdt")
    bucket_usd: float = _f("SHADOWGLASS_BUCKET_USD", 50.0)
    decay_rate: float = _f("SHADOWGLASS_DECAY_RATE", 0.95)
    min_cluster_usd: float = _f("SHADOWGLASS_MIN_CLUSTER_USD", 1000.0)
    oi_delta_threshold: float = _f("SHADOWGLASS_OI_DELTA_THRESHOLD", 10.0)
    reconnect_base_delay: float = _f("SHADOWGLASS_RECONNECT_BASE_DELAY", 1.0)
    reconnect_max_delay: float = _f("SHADOWGLASS_RECONNECT_MAX_DELAY", 20.0)

@dataclass(frozen=True)
class ExecutionerConfig:
    use_testnet: bool = _b("USE_TESTNET", True)
    default_risk_pct: float = _f("EXEC_DEFAULT_RISK_PCT", 0.01)
    default_sl_pct: float = _f("EXEC_DEFAULT_SL_PCT", 0.015)
    default_tp_pct: float = _f("EXEC_DEFAULT_TP_PCT", 0.045)
    max_notional_usd: float = _f("EXEC_MAX_NOTIONAL_USD", 25000.0)


# ────────────────────────────────────────────────────
# v3: Master Orchestrator Confluence Config
# ────────────────────────────────────────────────────

@dataclass(frozen=True)
class MaestroV3Config:
    """Configuration for the Master Orchestrator v3 Confluence Engine."""

    # ── Upstream node URLs ──
    brain_url: str = _s("MAESTRO_BRAIN_URL", "http://127.0.0.1:8000")
    shadow_url: str = _s("MAESTRO_SHADOW_URL", "http://127.0.0.1:8001")
    exec_url: str = _s("MAESTRO_EXEC_URL", "http://127.0.0.1:8002")
    binance_fapi: str = _s("MAESTRO_BINANCE_FAPI", "https://fapi.binance.com")

    # v3 node URLs (initially pointed at v2 for backward compat)
    econopredator_url: str = _s("MAESTRO_ECONOPREDATOR_URL", "")
    newtonian_url: str = _s("MAESTRO_NEWTONIAN_URL", "")
    spoofhunter_url: str = _s("MAESTRO_SPOOFHUNTER_URL", "")
    antirug_url: str = _s("MAESTRO_ANTIRUG_URL", "http://127.0.0.1:8003")
    narrative_url: str = _s("MAESTRO_NARRATIVE_URL", "")
    jito_url: str = _s("MAESTRO_JITO_URL", "")
    dreamer_url: str = _s("MAESTRO_DREAMER_URL", "")

    # ── Confluence ──
    confluence_mode: str = _s("MAESTRO_V3_CONFLUENCE_MODE", "AND")  # AND | OR | MAJORITY | WEIGHTED
    min_confidence: float = _f("MAESTRO_V3_MIN_CONFIDENCE", 0.55)
    parallel_timeout_s: float = _f("MAESTRO_V3_PARALLEL_TIMEOUT_S", 6.0)
    atr_period: int = _i("MAESTRO_V3_ATR_PERIOD", 14)
    max_notional_usd: float = _f("MAESTRO_V3_MAX_NOTIONAL_USD", 50000.0)

    # Node weights for WEIGHTED mode (JSON: {"spoofhunter": 1.5, "newtonian": 1.0, ...})
    node_weights: Dict = field(default_factory=lambda: _json_dict("MAESTRO_V3_NODE_WEIGHTS", "{}"))

    # Required nodes: if these don't respond, default to fallback_on_timeout
    required_nodes: List[str] = field(default_factory=lambda: _list("MAESTRO_V3_REQUIRED_NODES", ""))
    fallback_on_timeout: str = _s("MAESTRO_V3_FALLBACK_ON_TIMEOUT", "WAIT")

    # ── Rate limiting ──
    timeout_s: float = _f("MAESTRO_TIMEOUT_S", "4.0")
    attempts: int = _i("MAESTRO_ATTEMPTS", 4)
    rps_shadow: float = _f("MAESTRO_RPS_SHADOW", 5.0)
    rps_brain: float = _f("MAESTRO_RPS_BRAIN", 8.0)
    rps_exec: float = _f("MAESTRO_RPS_EXEC", 3.0)

    # ── Circuit breaker ──
    cb_failure_threshold: int = _i("MAESTRO_V3_CB_FAILURE_THRESHOLD", 5)
    cb_cooldown_s: float = _f("MAESTRO_V3_CB_COOLDOWN_S", 60.0)
    cb_probe_interval_s: float = _f("MAESTRO_V3_CB_PROBE_INTERVAL_S", 15.0)

    # ── ATR sizing ──
    base_risk_pct: float = _f("MAESTRO_V3_BASE_RISK_PCT", 0.01)
    target_risk_usd: float = _f("MAESTRO_V3_TARGET_RISK_USD", 500.0)
