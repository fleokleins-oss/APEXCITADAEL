# ===================================================================
# APEX MASTER ORCHESTRATOR v3 (Port 8007) - WEAPONIZED WITH APM
# Confluence Engine: parallelized node queries, Boolean gates,
# ATR-scaled sizing, circuit breakers, Redis async queue + HFT Scalper
# ===================================================================

from __future__ import annotations

import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Literal, Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel, Field

from apex_common.logging import get_logger
from apex_common.metrics import instrument_app
from apex_common.security import check_env_file_permissions
from apex_common.config import MaestroV3Config
from apex_common.rate_limit import AsyncRateLimiter
from apex_common.circuit_breaker import CircuitBreakerRegistry
from apex_common.confluence import (
    ConfluenceEngine,
    ConfluenceMode,
    ConfluenceResult,
    NodeSignal,
)
from apex_common.symbols import normalize_symbols
from apex_common.node_adapters import (
    call_brain,
    call_shadowglass,
    call_antirug,
    call_spoofhunter,
    call_newtonian,
    call_narrative,
    call_dreamer,
    fetch_premium_index,
)
from apex_common.maestro_pipeline import run_pipeline as run_v2_pipeline
from apex_common.redis_queue import (
    get_redis,
    ensure_group,
    enqueue_job,
    get_job,
    set_job_status,
    set_job_result,
    requeue_job,
    dlq_recent,
    STREAM,
    GROUP,
    CONSUMER,
    DLQ_STREAM,
)

# ────────────────────────────────────────────────────
# ⚔️ THE HFT WEAPONS (Active Position Manager)
# ────────────────────────────────────────────────────
from apm import ActivePositionManager, TickData

load_dotenv()
log = get_logger("maestro_v3")
_ok, _msg = check_env_file_permissions(".env")
if not _ok:
    log.warning(_msg)
else:
    log.info(_msg)

cfg = MaestroV3Config()

# Rate limiters & circuit breakers
lim_shadow = AsyncRateLimiter(cfg.rps_shadow, burst=cfg.rps_shadow)
lim_brain = AsyncRateLimiter(cfg.rps_brain, burst=cfg.rps_brain)
lim_exec = AsyncRateLimiter(cfg.rps_exec, burst=cfg.rps_exec)
lim_generic = AsyncRateLimiter(5.0, burst=5.0)

cb_registry = CircuitBreakerRegistry(
    failure_threshold=cfg.cb_failure_threshold,
    cooldown_s=cfg.cb_cooldown_s,
    probe_interval_s=cfg.cb_probe_interval_s,
)

confluence = ConfluenceEngine(
    mode=ConfluenceMode(cfg.confluence_mode.upper()),
    min_confidence=cfg.min_confidence,
    node_weights=cfg.node_weights,
    required_nodes=cfg.required_nodes,
    fallback_on_timeout=cfg.fallback_on_timeout,
)

# Globals
http_client: httpx.AsyncClient | None = None
redis_client = None
ADMIN_TOKEN = os.getenv("MAESTRO_V3_ADMIN_TOKEN", "").strip()

# Initialize the Active Position Manager Globally
apm_engine = ActivePositionManager()

# ────────────────────────────────────────────────────
# Request / Response models
# ────────────────────────────────────────────────────
Venue = Literal["binance", "bybit", "okx"]

class OrchestrateRequest(BaseModel):
    symbol: str = Field(..., description="BTCUSDT / BTC/USDT / BTC/USDT:USDT")
    venue: Venue = "binance"
    base_risk_pct: float = Field(0.01, gt=0, le=0.05)
    sl_pct: float = Field(0.015, gt=0, lt=0.25)
    tp_pct: float = Field(0.045, gt=0, lt=1.0)
    lle: float = -0.05
    drawdown_pct: float = 0.0
    chaos_detected: bool = False
    heatmap_intensity: Literal["LOW", "MED", "HIGH"] = "LOW"
    oi_spike: bool = False
    funding_rate: Optional[float] = None
    recent_pnl_history: list[float] = Field(default_factory=list)
    returns_array: list[float] = Field(default_factory=list)
    contagion_correlation: float = Field(0.0, ge=0.0, le=1.0)
    token_metrics: Optional[dict] = None
    dry_run: bool = False
    min_confidence: float = Field(0.55, ge=0.0, le=1.0)
    scale_by_confidence: bool = True
    idempotency_key: Optional[str] = None
    confluence_mode: Optional[str] = None

class QueuedResponse(BaseModel):
    status: str
    job_id: str

class JobResponse(BaseModel):
    status: str
    job_id: str
    payload: Optional[dict] = None
    result: Optional[dict] = None
    detail: Optional[str] = None

# ────────────────────────────────────────────────────
# Lifespan
# ────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client, redis_client
    http_client = httpx.AsyncClient(headers={"User-Agent": "ApexMaestroV3/3.0"})
    try:
        redis_client = await get_redis()
        await ensure_group(redis_client)
        log.info("Redis connected and group ensured")
    except Exception as e:
        log.warning(f"Redis unavailable (async endpoints disabled): {e}")
        redis_client = None
    yield
    if http_client:
        await http_client.aclose()
    if redis_client:
        await redis_client.aclose()

app = FastAPI(title="Apex Master Orchestrator v3", version="3.0.0", lifespan=lifespan)
instrument_app(app)

def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x

# ────────────────────────────────────────────────────
# 🦅 APM MONITORING LOOP (The Scalpel)
# ────────────────────────────────────────────────────
async def apm_monitor_loop(pos_id: str, symbol: str, shadow_symbol: str, exec_symbol: str, side: str, venue: str):
    log.info(f"🦅 [APM] Scalpel activated. Monitoring {symbol} continuously...")
    
    while True:
        await asyncio.sleep(0.1) # Loop 10 times per second
        try:
            if not http_client: break
            
            # 1. Fetch live order book data
            _, shadow_raw = await call_shadowglass(
                http_client, cfg.shadow_url, shadow_symbol, lim_shadow, cb_registry, timeout=1.0, attempts=1
            )
            
            price = float(shadow_raw.get("price", 0.0))
            metrics = shadow_raw.get("metrics", {}) or {}
            obi = float(metrics.get("orderbook_imbalance", 0.0))
            
            if price <= 0: continue

            # 2. Feed it to the APM Engine
            tick = TickData(price=price, volume=100.0, obi=obi)
            decision = await apm_engine.process_tick(pos_id, tick)
            
            # 3. PANIC BUTTON TRIGGERED
            if decision.action == "EXIT":
                log.warning(f"🚨 [APM FIRE] KILLING TRADE ON {symbol} | Reason: {decision.reason}")
                
                # Invert side to close the position (Long -> Sell, Short -> Buy)
                close_side = "sell" if side.upper() == "LONG" else "buy"
                
                exec_payload = {
                    "symbol": exec_symbol,
                    "side": close_side,
                    "venue": venue,
                    "risk_pct": 1.0, # Close 100% of the bag
                    "reduce_only_brackets": True,
                }
                
                await http_client.post(
                    f"{cfg.exec_url}/execute_strike",
                    json=exec_payload,
                    timeout=8.0,
                )
                break # Exit the loop, the trade is closed

        except Exception as e:
            # Silently continue on brief network hiccups
            pass

async def _gather_signals(
    req: OrchestrateRequest,
    shadow_symbol: str,
    exec_symbol: str,
) -> tuple[List[NodeSignal], dict, list[str]]:
    assert http_client is not None
    notes: list[str] = []
    timeout = cfg.parallel_timeout_s
    attempts = cfg.attempts

    tasks: Dict[str, asyncio.Task] = {}

    if cfg.shadow_url:
        tasks["shadowglass"] = asyncio.create_task(call_shadowglass(http_client, cfg.shadow_url, shadow_symbol, limiter=lim_shadow, cb=cb_registry, timeout=timeout, attempts=attempts))
    if cfg.spoofhunter_url:
        tasks["spoofhunter"] = asyncio.create_task(call_spoofhunter(http_client, cfg.spoofhunter_url, shadow_symbol, limiter=lim_generic, cb=cb_registry, timeout=timeout, attempts=attempts))
    if cfg.newtonian_url:
        tasks["newtonian"] = asyncio.create_task(call_newtonian(http_client, cfg.newtonian_url, shadow_symbol, limiter=lim_generic, cb=cb_registry, timeout=timeout, attempts=attempts))
    if cfg.narrative_url:
        tasks["narrative"] = asyncio.create_task(call_narrative(http_client, cfg.narrative_url, shadow_symbol, limiter=lim_generic, cb=cb_registry, timeout=timeout, attempts=attempts))
    if cfg.dreamer_url:
        tasks["dreamer"] = asyncio.create_task(call_dreamer(http_client, cfg.dreamer_url, shadow_symbol, limiter=lim_generic, cb=cb_registry, timeout=timeout, attempts=attempts))
    if cfg.antirug_url and req.token_metrics:
        tasks["antirug_v3"] = asyncio.create_task(call_antirug(http_client, cfg.antirug_url, req.token_metrics, limiter=lim_generic, cb=cb_registry, timeout=timeout, attempts=attempts))

    signals: List[NodeSignal] = []
    shadow_raw: dict = {}

    if tasks:
        done, pending = await asyncio.wait(tasks.values(), timeout=cfg.parallel_timeout_s + 1.0)
        for t in pending: t.cancel()

    for name, task in tasks.items():
        if task.done() and not task.cancelled():
            try:
                result = task.result()
                if name == "shadowglass":
                    sig, shadow_raw = result
                    signals.append(sig)
                else:
                    signals.append(result)
            except Exception as e:
                notes.append(f"{name}: exception {e}")
                signals.append(NodeSignal(node=name, available=False))
        else:
            notes.append(f"{name}: timed out or cancelled")
            signals.append(NodeSignal(node=name, available=False))

    if cfg.brain_url:
        metrics = shadow_raw.get("metrics", {}) or {}
        micro_shift = float(metrics.get("micro_price_shift", 0.0) or 0.0)
        imbalance = float(metrics.get("orderbook_imbalance", 0.0) or 0.0)
        ls_ratio = float(shadow_raw.get("long_short_ratio", 1.0) or 1.0)

        funding_rate = req.funding_rate
        if funding_rate is None:
            prem = await fetch_premium_index(http_client, cfg.binance_fapi, shadow_symbol, timeout)
            funding_rate = prem.get("lastFundingRate", 0.0)
            notes.append("funding_rate from premiumIndex")

        brain_payload = {
            "symbol": shadow_symbol,
            "lle": req.lle,
            "drawdown_pct": req.drawdown_pct,
            "chaos_detected": req.chaos_detected,
            "funding_rate": float(funding_rate or 0.0),
            "oi_spike": req.oi_spike,
            "heatmap_intensity": req.heatmap_intensity,
            "recent_pnl_history": req.recent_pnl_history or [],
            "returns_array": req.returns_array or [],
            "contagion_correlation": float(req.contagion_correlation or 0.0),
            "micro_price_shift": micro_shift,
            "orderbook_imbalance": imbalance,
            "long_short_ratio": ls_ratio,
        }
        brain_signal = await call_brain(http_client, cfg.brain_url, brain_payload, limiter=lim_brain, cb=cb_registry, timeout=timeout, attempts=attempts)
        signals.append(brain_signal)

    for s in signals:
        status = "OK" if s.available else "DOWN"
        notes.append(f"signal:{s.node}={status} action={s.action} side={s.side} conf={s.confidence:.2f}")

    return signals, shadow_raw, notes

async def _execute_trade(
    req: OrchestrateRequest,
    shadow_symbol: str,
    exec_symbol: str,
    result: ConfluenceResult,
) -> dict:
    assert http_client is not None

    side_map = {"LONG": "buy", "SHORT": "sell"}
    side = side_map.get(result.side, "buy")

    final_risk_pct = _clamp(
        req.base_risk_pct * result.risk_multiplier * (result.confidence if req.scale_by_confidence else 1.0),
        0.0005,
        0.05,
    )

    exec_payload = {
        "symbol": exec_symbol,
        "side": side,
        "venue": req.venue,
        "risk_pct": final_risk_pct,
        "sl_pct": req.sl_pct,
        "tp_pct": req.tp_pct,
        "reduce_only_brackets": True,
    }

    async def _do():
        await lim_exec.acquire()
        r = await http_client.post(
            f"{cfg.exec_url}/execute_strike",
            json=exec_payload,
            timeout=max(cfg.timeout_s, 8.0),
        )
        r.raise_for_status()
        return r.json()

    from apex_common.retry import retry_with_backoff
    exec_result = await retry_with_backoff(_do, attempts=cfg.attempts)

    # ────────────────────────────────────────────────────
    # ⚔️ REGISTER WITH APM UPON SUCCESSFUL ENTRY
    # ────────────────────────────────────────────────────
    if exec_result.get("status") in ["SUCCESS", "EXECUTED"]:
        entry_price = float(exec_result.get("average") or exec_result.get("price") or 100.0)
        qty = float(exec_result.get("quantity") or 1.0)
        
        pos_id = await apm_engine.register_position(
            symbol=req.symbol,
            side=result.side,
            entry_price=entry_price,
            quantity=qty,
            atr=entry_price * 0.015, # dynamic ATR proxy
            take_profit_pct=req.tp_pct * 100,
            hard_stop_pct=req.sl_pct * 100
        )
        
        # Fire and forget the background monitor loop
        asyncio.create_task(
            apm_monitor_loop(
                pos_id=pos_id,
                symbol=req.symbol,
                shadow_symbol=shadow_symbol,
                exec_symbol=exec_symbol,
                side=result.side,
                venue=req.venue
            )
        )

    return exec_result

def _serialize_confluence(result: ConfluenceResult) -> dict:
    return {
        "action": result.action,
        "side": result.side,
        "confidence": round(result.confidence, 4),
        "risk_multiplier": round(result.risk_multiplier, 4),
        "gates": [{"name": g.name, "passed": g.passed, "reason": g.reason} for g in result.gates],
        "signals": [
            {"node": s.node, "available": s.available, "action": s.action, "side": s.side, "confidence": round(s.confidence, 4)}
            for s in result.signals
        ],
        "reasoning": result.reasoning,
    }

# ────────────────────────────────────────────────────
# Endpoints
# ────────────────────────────────────────────────────
@app.get("/health")
async def health():
    cb_status = await cb_registry.get_all_status()
    return {
        "status": "ok",
        "service": "maestro_v3",
        "version": app.version,
        "confluence_mode": cfg.confluence_mode,
        "nodes": {
            "brain": cfg.brain_url or None,
            "shadowglass": cfg.shadow_url or None,
            "executioner": cfg.exec_url or None,
        },
        "circuit_breakers": cb_status,
        "redis_stream": STREAM,
    }

@app.post("/orchestrate")
async def orchestrate(req: OrchestrateRequest):
    if not http_client:
        raise HTTPException(status_code=503, detail="HTTP client not ready")

    rid = uuid.uuid4().hex[:12]
    sym_in, shadow_symbol, exec_symbol = normalize_symbols(req.symbol)

    log.info(f"rid={rid} orchestrate(v3) symbol={sym_in} venue={req.venue} mode={cfg.confluence_mode}")

    try:
        signals, shadow_raw, notes = await _gather_signals(req, shadow_symbol, exec_symbol)

        engine = confluence
        if req.confluence_mode:
            try:
                mode = ConfluenceMode(req.confluence_mode.upper())
                engine = ConfluenceEngine(mode=mode, min_confidence=req.min_confidence, node_weights=cfg.node_weights, required_nodes=cfg.required_nodes, fallback_on_timeout=cfg.fallback_on_timeout)
            except ValueError:
                notes.append(f"Invalid confluence_mode '{req.confluence_mode}', using default")

        result = engine.evaluate(signals)

        resp: Dict[str, Any] = {
            "status": result.action,
            "pipeline": "v3_confluence",
            "request_id": rid,
            "symbol": shadow_symbol.upper(),
            "venue": req.venue.upper(),
            "confluence": _serialize_confluence(result),
            "execution": None,
            "notes": notes,
        }

        if not result.should_execute:
            return resp

        if req.dry_run:
            side_map = {"LONG": "buy", "SHORT": "sell"}
            final_risk = _clamp(req.base_risk_pct * result.risk_multiplier * (result.confidence if req.scale_by_confidence else 1.0), 0.0005, 0.05)
            resp["status"] = "DRY_RUN"
            resp["execution"] = {
                "would_execute": True, "risk_pct": round(final_risk, 6), "side": result.side, "mapped_side": side_map.get(result.side, "none"), "sl_pct": req.sl_pct, "tp_pct": req.tp_pct,
            }
            return resp

        # Pass shadow_symbol here for the APM hook
        exec_result = await _execute_trade(req, shadow_symbol, exec_symbol, result)
        resp["execution"] = exec_result
        resp["status"] = "EXECUTED" if exec_result.get("status") == "SUCCESS" else exec_result.get("status", "EXECUTED")
        return resp

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        log.error(f"rid={rid} orchestrate failed: {e}")
        raise HTTPException(status_code=502, detail=str(e))

@app.post("/orchestrate_v2")
async def orchestrate_v2(req: OrchestrateRequest):
    if not http_client: raise HTTPException(status_code=503, detail="HTTP client not ready")
    rid = uuid.uuid4().hex[:12]
    sym_in, shadow_symbol, exec_symbol = normalize_symbols(req.symbol)
    payload = req.model_dump()
    payload["shadow_symbol"] = shadow_symbol
    payload["exec_symbol"] = exec_symbol

    try:
        result = await run_v2_pipeline(http=http_client, req=payload, brain_url=cfg.brain_url, shadow_url=cfg.shadow_url, exec_url=cfg.exec_url, binance_fapi=cfg.binance_fapi, timeout_s=cfg.timeout_s, attempts=cfg.attempts, lim_shadow=lim_shadow, lim_brain=lim_brain, lim_exec=lim_exec)
        result.setdefault("notes", []).append(f"request_id={rid}")
        return result
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

@app.post("/orchestrate_async", response_model=QueuedResponse)
async def orchestrate_async(req: OrchestrateRequest):
    if not redis_client: raise HTTPException(status_code=503, detail="Redis not ready")
    sym_in, shadow_symbol, exec_symbol = normalize_symbols(req.symbol)
    payload = req.model_dump()
    payload["shadow_symbol"] = shadow_symbol
    payload["exec_symbol"] = exec_symbol
    job_id = (req.idempotency_key or "").strip()
    job_id = f"idem:{job_id}" if job_id else uuid.uuid4().hex
    mode, msg_id = await enqueue_job(redis_client, job_id, payload)
    if mode == "ENQUEUED":
        await set_job_status(redis_client, job_id, "QUEUED")
        return QueuedResponse(status="QUEUED", job_id=job_id)
    return QueuedResponse(status="EXISTS", job_id=job_id)

@app.get("/jobs/{job_id}", response_model=JobResponse)
async def job_status(job_id: str):
    if not redis_client: raise HTTPException(status_code=503, detail="Redis not ready")
    data = await get_job(redis_client, job_id)
    if not data: raise HTTPException(status_code=404, detail="job not found")
    payload = result = None
    try:
        if "payload" in data: payload = json.loads(data["payload"])
        if "result" in data: result = json.loads(data["result"])
    except Exception: pass
    return JobResponse(status=data.get("status", "UNKNOWN"), job_id=job_id, payload=payload, result=result, detail=data.get("error"))

@app.get("/dlq/recent")
async def dlq_recent_endpoint(count: int = 50):
    if not redis_client: raise HTTPException(status_code=503, detail="Redis not ready")
    return {"status": "ok", "count": max(1, min(500, int(count))), "items": await dlq_recent(redis_client, count=max(1, min(500, int(count))))}

@app.post("/jobs/{job_id}/retry")
async def retry_job(job_id: str):
    if not redis_client: raise HTTPException(status_code=503, detail="Redis not ready")
    data = await get_job(redis_client, job_id)
    if not data: raise HTTPException(status_code=404, detail="job not found")
    msg_id = await requeue_job(redis_client, job_id)
    return {"status": "QUEUED", "job_id": job_id, "msg_id": msg_id}

def _check_admin(token: Optional[str]):
    if not ADMIN_TOKEN: raise HTTPException(status_code=403, detail="Admin token not configured")
    if (token or "") != ADMIN_TOKEN: raise HTTPException(status_code=403, detail="Invalid admin token")

@app.get("/circuit_breakers")
async def get_circuit_breakers():
    return {"circuit_breakers": await cb_registry.get_all_status()}

@app.post("/circuit_breakers/{node}/reset")
async def reset_circuit_breaker(node: str, x_admin_token: Optional[str] = Header(default=None)):
    _check_admin(x_admin_token)
    await cb_registry.force_close(node)
    return {"status": "ok", "node": node, "state": "CLOSED"}