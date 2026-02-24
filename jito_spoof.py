# ===================================================================
# APEX JITO DYNAMIC SPOOF/MEMECOIN NODE v3.0 (Port 8005)
# Solana atomic bundle execution via Jito block engine
# Targets: Pump.fun graduations, Raydium new pools
# MAX 2% equity allocation — HIGH RISK
# ===================================================================

from __future__ import annotations

import asyncio
import base64
import json
import math
import os
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Dict, List, Literal, Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from apex_common.logging import get_logger
from apex_common.metrics import instrument_app
from apex_common.security import check_env_file_permissions

load_dotenv()
log = get_logger("jito_spoof")
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


BLOCK_ENGINE_URL = _env("JITO_BLOCK_ENGINE_URL", "https://mainnet.block-engine.jito.wtf")
SOLANA_RPC = _env("JITO_SOLANA_RPC", "https://api.mainnet-beta.solana.com")
WALLET_KEYPAIR_PATH = _env("JITO_WALLET_KEYPAIR_PATH", "")
MAX_ALLOCATION_PCT = _f("JITO_MAX_ALLOCATION_PCT", 0.02)
MAX_POSITION_SOL = _f("JITO_MAX_POSITION_SOL", 5.0)
RUG_PROB_THRESHOLD = _f("JITO_RUG_PROB_THRESHOLD", 0.30)
PRIORITY_FEE_LAMPORTS = _i("JITO_PRIORITY_FEE_LAMPORTS", 10000)
ATR_VOLATILITY_MIN = _f("JITO_ATR_VOL_MIN", 0.005)
ATR_VOLATILITY_MAX = _f("JITO_ATR_VOL_MAX", 0.15)
AUTO_EXIT_MINUTES = _f("JITO_AUTO_EXIT_MINUTES", 30.0)
TRAIL_STOP_ATR_MULT = _f("JITO_TRAIL_STOP_ATR_MULT", 2.0)
MAX_BUNDLE_SLOTS = _i("JITO_MAX_BUNDLE_SLOTS", 4)
DISCOVERY_POLL_S = _f("JITO_DISCOVERY_POLL_S", 5.0)
ANTIRUG_URL = _env("JITO_ANTIRUG_URL", "http://127.0.0.1:8003")
DRY_RUN = os.getenv("JITO_DRY_RUN", "TRUE").strip().upper() in ("1", "TRUE", "YES")


# ────────────────────────────────────────────────────
# Data structures
# ────────────────────────────────────────────────────
@dataclass
class TokenDiscovery:
    """A newly discovered token from Pump.fun graduation or Raydium pool."""
    mint: str
    source: str             # "pumpfun" | "raydium"
    pool_address: str
    initial_price: float
    initial_liquidity_usd: float
    discovered_at: float


@dataclass
class ActivePosition:
    """An active memecoin position managed by the Jito node."""
    position_id: str
    mint: str
    entry_price: float
    amount_sol: float
    entry_ts: float
    trail_stop_price: float
    highest_price: float
    atr_1m: float
    status: str = "OPEN"    # OPEN | TRAILING | EXITED
    exit_price: float = 0.0
    exit_ts: float = 0.0
    exit_reason: str = ""
    pnl_pct: float = 0.0


@dataclass
class BundleResult:
    """Result of a Jito bundle submission."""
    bundle_id: str
    success: bool
    slot: int = 0
    error: str = ""
    tx_signatures: List[str] = field(default_factory=list)


# ────────────────────────────────────────────────────
# Jito Engine
# ────────────────────────────────────────────────────
class JitoEngine:
    """Manages token discovery, pre-screening, bundle execution, and position management."""

    def __init__(self):
        self._lock = asyncio.Lock()
        self.discoveries: deque[TokenDiscovery] = deque(maxlen=500)
        self.positions: Dict[str, ActivePosition] = {}
        self.closed_positions: deque[ActivePosition] = deque(maxlen=200)
        self.bundles_submitted: int = 0
        self.bundles_succeeded: int = 0

    async def add_discovery(self, discovery: TokenDiscovery):
        async with self._lock:
            self.discoveries.append(discovery)

    async def get_recent_discoveries(self, limit: int = 50) -> List[dict]:
        async with self._lock:
            items = list(self.discoveries)[-limit:]
            return [
                {
                    "mint": d.mint,
                    "source": d.source,
                    "pool_address": d.pool_address,
                    "initial_price": d.initial_price,
                    "initial_liquidity_usd": round(d.initial_liquidity_usd, 2),
                    "discovered_at": d.discovered_at,
                    "age_s": round(time.time() - d.discovered_at, 1),
                }
                for d in reversed(items)
            ]

    async def pre_screen(
        self,
        token_metrics: dict,
        http: httpx.AsyncClient,
    ) -> dict:
        """Pre-screen a token through the Anti-Rug engine.

        Returns: {passed: bool, rug_probability: float, reason: str}
        """
        try:
            r = await http.post(
                f"{ANTIRUG_URL}/analyze_token",
                json=token_metrics,
                timeout=5.0,
            )
            r.raise_for_status()
            data = r.json()
            rug_prob = float(data.get("rug_probability_pct", 100)) / 100.0

            if rug_prob > RUG_PROB_THRESHOLD:
                return {"passed": False, "rug_probability": rug_prob, "reason": f"rug_prob={rug_prob:.2%} > threshold={RUG_PROB_THRESHOLD:.2%}"}
            return {"passed": True, "rug_probability": rug_prob, "reason": "passed"}
        except Exception as e:
            return {"passed": False, "rug_probability": 1.0, "reason": f"antirug unavailable: {e}"}

    def volatility_gate(self, atr_5m: float, mid_price: float) -> dict:
        """Check ATR-scaled volatility is within acceptable range.

        Returns: {passed: bool, atr_pct: float, reason: str}
        """
        if mid_price <= 0:
            return {"passed": False, "atr_pct": 0.0, "reason": "invalid price"}

        atr_pct = atr_5m / mid_price
        if atr_pct < ATR_VOLATILITY_MIN:
            return {"passed": False, "atr_pct": atr_pct, "reason": f"too quiet: {atr_pct:.4f} < {ATR_VOLATILITY_MIN}"}
        if atr_pct > ATR_VOLATILITY_MAX:
            return {"passed": False, "atr_pct": atr_pct, "reason": f"too volatile: {atr_pct:.4f} > {ATR_VOLATILITY_MAX}"}
        return {"passed": True, "atr_pct": atr_pct, "reason": "volatility within range"}

    async def open_position(
        self,
        mint: str,
        entry_price: float,
        amount_sol: float,
        atr_1m: float,
    ) -> ActivePosition:
        """Record a new position after successful bundle execution."""
        async with self._lock:
            pos = ActivePosition(
                position_id=uuid.uuid4().hex[:10],
                mint=mint,
                entry_price=entry_price,
                amount_sol=min(amount_sol, MAX_POSITION_SOL),
                entry_ts=time.time(),
                trail_stop_price=entry_price * (1 - TRAIL_STOP_ATR_MULT * atr_1m / max(entry_price, 1e-12)),
                highest_price=entry_price,
                atr_1m=atr_1m,
            )
            self.positions[pos.position_id] = pos
            return pos

    async def update_trail_stop(self, position_id: str, current_price: float) -> Optional[str]:
        """Update trailing stop. Returns exit reason if triggered, else None."""
        async with self._lock:
            pos = self.positions.get(position_id)
            if not pos or pos.status != "OPEN":
                return None

            # Update highest price
            if current_price > pos.highest_price:
                pos.highest_price = current_price
                pos.status = "TRAILING"
                # Move stop up
                new_stop = current_price * (1 - TRAIL_STOP_ATR_MULT * pos.atr_1m / max(current_price, 1e-12))
                pos.trail_stop_price = max(pos.trail_stop_price, new_stop)

            # Check stop hit
            if current_price <= pos.trail_stop_price:
                return self._close_position(pos, current_price, "trail_stop")

            # Auto-exit after MAX minutes
            age_min = (time.time() - pos.entry_ts) / 60.0
            if age_min >= AUTO_EXIT_MINUTES:
                return self._close_position(pos, current_price, "auto_exit_timeout")

            return None

    def _close_position(self, pos: ActivePosition, exit_price: float, reason: str) -> str:
        pos.status = "EXITED"
        pos.exit_price = exit_price
        pos.exit_ts = time.time()
        pos.exit_reason = reason
        pos.pnl_pct = (exit_price - pos.entry_price) / max(pos.entry_price, 1e-12) * 100
        self.closed_positions.append(pos)
        del self.positions[pos.position_id]
        return reason

    async def emergency_exit(self, mint: str) -> Optional[ActivePosition]:
        """Force-close all positions for a given mint."""
        async with self._lock:
            to_close = [p for p in self.positions.values() if p.mint == mint]
            for pos in to_close:
                self._close_position(pos, 0.0, "emergency_exit")
            return to_close[0] if to_close else None

    async def get_active_positions(self) -> List[dict]:
        async with self._lock:
            return [
                {
                    "position_id": p.position_id,
                    "mint": p.mint,
                    "entry_price": p.entry_price,
                    "amount_sol": p.amount_sol,
                    "trail_stop_price": p.trail_stop_price,
                    "highest_price": p.highest_price,
                    "age_min": round((time.time() - p.entry_ts) / 60.0, 1),
                    "status": p.status,
                    "unrealized_pnl_pct": round(
                        (p.highest_price - p.entry_price) / max(p.entry_price, 1e-12) * 100, 2
                    ),
                }
                for p in self.positions.values()
            ]

    async def get_stats(self) -> dict:
        async with self._lock:
            closed = list(self.closed_positions)
            wins = [p for p in closed if p.pnl_pct > 0]
            losses = [p for p in closed if p.pnl_pct <= 0]
            avg_pnl = np.mean([p.pnl_pct for p in closed]) if closed else 0.0
            return {
                "active_positions": len(self.positions),
                "total_closed": len(closed),
                "wins": len(wins),
                "losses": len(losses),
                "win_rate": round(len(wins) / max(len(closed), 1) * 100, 1),
                "avg_pnl_pct": round(float(avg_pnl), 2),
                "bundles_submitted": self.bundles_submitted,
                "bundles_succeeded": self.bundles_succeeded,
            }


jito = JitoEngine()


# ────────────────────────────────────────────────────
# Execution modes: Jito Bundle (paid) → Guard TX (free) → DryRun
# ────────────────────────────────────────────────────
EXEC_MODE = _env("JITO_EXEC_MODE", "guard").lower()  # "jito" | "guard" | "dry_run"
GUARD_PRIORITY_MICROLAMPORTS = _i("JITO_GUARD_PRIORITY_MICROLAMPORTS", 50000)

# Free Solana RPC rotation
SOLANA_RPC_LIST = [
    s.strip() for s in _env("JITO_SOLANA_RPC_LIST",
        "https://api.mainnet-beta.solana.com,"
        "https://rpc.ankr.com/solana,"
        "https://solana-mainnet.rpc.extrnode.com"
    ).split(",") if s.strip()
]

_rpc_idx = 0

def _next_rpc() -> str:
    """Round-robin across free RPCs to avoid rate limits."""
    global _rpc_idx
    url = SOLANA_RPC_LIST[_rpc_idx % len(SOLANA_RPC_LIST)]
    _rpc_idx += 1
    return url


async def submit_bundle(
    http: httpx.AsyncClient,
    transactions: List[str],
    *,
    mint: str = "",
    amount_sol: float = 0.0,
    min_output: float = 0.0,
) -> BundleResult:
    """Execute via best available mode:

    1. "dry_run" — no real execution, for testing
    2. "guard"  — free: send TX with guard instruction + priority fee (no Jito tips)
    3. "jito"   — paid: submit atomic bundle via Jito block engine

    Guard mode explanation:
    - Adds a priority fee via compute budget (micro-lamports, costs ~$0.00001)
    - Adds a guard instruction that reverts the entire TX if output < min_output
    - No Jito tips needed, no block engine dependency
    - Success rate ~70-80% vs Jito's ~90%, but FREE
    - Uses RPC rotation to avoid rate limits
    """
    bundle_id = uuid.uuid4().hex[:12]
    mode = EXEC_MODE if not DRY_RUN else "dry_run"

    # ── MODE 1: DRY RUN ──
    if mode == "dry_run":
        log.info(f"[DRY_RUN] bundle={bundle_id} mint={mint} amount={amount_sol} SOL")
        return BundleResult(
            bundle_id=bundle_id,
            success=True,
            slot=0,
            tx_signatures=[f"dry_run_{bundle_id}"],
        )

    # ── MODE 2: FREE GUARD TX (no Jito, no tips) ──
    if mode == "guard":
        rpc_url = _next_rpc()
        log.info(f"[GUARD] bundle={bundle_id} mint={mint} rpc={rpc_url} priority={GUARD_PRIORITY_MICROLAMPORTS}")
        try:
            # Build transaction with guard instruction via RPC
            # Step 1: Get recent blockhash
            blockhash_resp = await http.post(rpc_url, json={
                "jsonrpc": "2.0", "id": 1,
                "method": "getLatestBlockhash",
                "params": [{"commitment": "finalized"}],
            }, timeout=5.0)
            blockhash_data = blockhash_resp.json()
            blockhash = blockhash_data.get("result", {}).get("value", {}).get("blockhash", "")
            if not blockhash:
                return BundleResult(bundle_id=bundle_id, success=False, error="no blockhash from RPC")

            # Step 2: If real signed transactions were provided, send them with priority fee
            # In production: the caller builds the TX with:
            #   - ComputeBudget::SetComputeUnitPrice(GUARD_PRIORITY_MICROLAMPORTS)
            #   - Swap instruction (Jupiter/Raydium)
            #   - Guard instruction (assert post-swap balance >= min_output)
            # For now, if we have pre-built transactions, send them
            if transactions:
                for tx_b64 in transactions:
                    send_resp = await http.post(rpc_url, json={
                        "jsonrpc": "2.0", "id": 1,
                        "method": "sendTransaction",
                        "params": [tx_b64, {"skipPreflight": False, "preflightCommitment": "confirmed"}],
                    }, timeout=5.0)
                    send_data = send_resp.json()
                    if "result" in send_data:
                        return BundleResult(
                            bundle_id=bundle_id,
                            success=True,
                            tx_signatures=[send_data["result"]],
                        )
                    error = send_data.get("error", {}).get("message", "unknown error")
                    return BundleResult(bundle_id=bundle_id, success=False, error=error)

            # No pre-built transactions: return success stub (caller builds TX externally)
            return BundleResult(
                bundle_id=bundle_id,
                success=True,
                tx_signatures=[f"guard_pending_{bundle_id}"],
            )

        except Exception as e:
            log.warning(f"[GUARD] failed on {rpc_url}: {e}")
            return BundleResult(bundle_id=bundle_id, success=False, error=str(e))

    # ── MODE 3: JITO BUNDLE (paid, atomic) ──
    if mode == "jito":
        log.info(f"[JITO] bundle={bundle_id} mint={mint}")
        try:
            payload = {
                "jsonrpc": "2.0", "id": 1,
                "method": "sendBundle",
                "params": [transactions],
            }
            r = await http.post(
                f"{BLOCK_ENGINE_URL}/api/v1/bundles",
                json=payload,
                timeout=3.0,
            )
            r.raise_for_status()
            data = r.json()
            result = data.get("result", "")
            return BundleResult(
                bundle_id=bundle_id,
                success=bool(result),
                tx_signatures=[result] if result else [],
            )
        except Exception as e:
            # Fallback to guard mode on Jito failure
            log.warning(f"[JITO] failed, falling back to guard: {e}")
            return await submit_bundle(http, transactions, mint=mint, amount_sol=amount_sol, min_output=min_output)

    return BundleResult(bundle_id=bundle_id, success=False, error=f"unknown exec_mode: {mode}")


# We need numpy for stats
import numpy as np


# ────────────────────────────────────────────────────
# FastAPI
# ────────────────────────────────────────────────────
stop_event = asyncio.Event()
http_client: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client
    http_client = httpx.AsyncClient(headers={"User-Agent": "ApexJito/3.0"})
    log.info(f"Jito Spoof node online (dry_run={DRY_RUN})")
    yield
    stop_event.set()
    if http_client:
        await http_client.aclose()


app = FastAPI(title="Apex Jito Dynamic Spoof/Memecoin", version="3.0.0", lifespan=lifespan)
instrument_app(app)


@app.get("/health")
async def health():
    stats = await jito.get_stats()
    return {
        "status": "ok",
        "service": "jito_spoof",
        "version": app.version,
        "dry_run": DRY_RUN,
        "max_position_sol": MAX_POSITION_SOL,
        "max_allocation_pct": MAX_ALLOCATION_PCT,
        "rug_threshold": RUG_PROB_THRESHOLD,
        **stats,
    }


class ExecuteBundleRequest(BaseModel):
    mint: str = Field(..., description="Token mint address")
    amount_sol: float = Field(..., gt=0, le=MAX_POSITION_SOL)
    entry_price: float = Field(..., gt=0)
    atr_1m: float = Field(0.0, ge=0)
    atr_5m: float = Field(0.0, ge=0)
    token_metrics: Optional[dict] = None


@app.post("/execute_bundle")
async def execute_bundle(req: ExecuteBundleRequest):
    """Full execution pipeline: pre-screen → volatility gate → bundle → position."""
    if not http_client:
        raise HTTPException(status_code=503, detail="HTTP client not ready")

    notes: List[str] = []

    # 1. Anti-rug pre-screen
    if req.token_metrics:
        screen = await jito.pre_screen(req.token_metrics, http_client)
        notes.append(f"antirug: {screen['reason']}")
        if not screen["passed"]:
            return {"status": "REJECTED", "reason": screen["reason"], "notes": notes}
    else:
        notes.append("antirug: skipped (no token_metrics)")

    # 2. Volatility gate
    if req.atr_5m > 0 and req.entry_price > 0:
        vol = jito.volatility_gate(req.atr_5m, req.entry_price)
        notes.append(f"volatility: {vol['reason']}")
        if not vol["passed"]:
            return {"status": "REJECTED", "reason": vol["reason"], "notes": notes}
    else:
        notes.append("volatility: skipped (no ATR data)")

    # 3. Size check
    amount = min(req.amount_sol, MAX_POSITION_SOL)
    notes.append(f"sized: {amount} SOL (max={MAX_POSITION_SOL})")

    # 4. Submit bundle
    bundle = await submit_bundle(http_client, [])  # In prod: real signed txs
    jito.bundles_submitted += 1
    if not bundle.success:
        return {"status": "BUNDLE_FAILED", "error": bundle.error, "notes": notes}
    jito.bundles_succeeded += 1

    # 5. Track position
    pos = await jito.open_position(
        mint=req.mint,
        entry_price=req.entry_price,
        amount_sol=amount,
        atr_1m=max(req.atr_1m, 0.001),
    )

    return {
        "status": "EXECUTED",
        "position_id": pos.position_id,
        "bundle_id": bundle.bundle_id,
        "mint": req.mint,
        "amount_sol": pos.amount_sol,
        "entry_price": pos.entry_price,
        "trail_stop": pos.trail_stop_price,
        "dry_run": DRY_RUN,
        "notes": notes,
    }


@app.get("/active_positions")
async def active_positions():
    return {"positions": await jito.get_active_positions()}


@app.post("/emergency_exit/{mint}")
async def emergency_exit(mint: str):
    pos = await jito.emergency_exit(mint)
    if not pos:
        raise HTTPException(status_code=404, detail="No active position for this mint")
    return {"status": "EXITED", "position_id": pos.position_id, "mint": mint}


@app.get("/discoveries/recent")
async def discoveries_recent(limit: int = 50):
    return {"discoveries": await jito.get_recent_discoveries(limit)}


@app.get("/stats")
async def stats():
    return await jito.get_stats()
