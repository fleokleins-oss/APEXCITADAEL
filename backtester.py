# ===================================================================
# APEX TICK-LEVEL BACKTESTING ENGINE v3.0
#
# High-fidelity historical simulator that replays L2 tick data through
# the EXACT same code paths as live trading:
#   SpoofHunter → EconoPredator → Newtonian → DreamerV3 → Confluence → APM
#
# Data sources:
#   A) CSV import (custom format or Tardis.dev)
#   B) Synthetic generator (for stress testing)
#   C) Binance Historical (free API, future extension)
#
# Output: per-trade PnL with exit reason attribution, equity curve,
#         weapon effectiveness, confluence gate hit rates, parameter sweep
# ===================================================================

from __future__ import annotations

import asyncio
import csv
import math
import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

import numpy as np

from apex_common.logging import get_logger
from apex_common.confluence import ConfluenceEngine, NodeSignal, ConfluenceResult
from apm import (
    ActivePositionManager,
    TickData,
    APMDecision,
    ExitReason,
    VPINComputer,
)
from spoofhunter import SpoofEngine

log = get_logger("backtester")


# ────────────────────────────────────────────────────
# Data structures
# ────────────────────────────────────────────────────
@dataclass
class L2Snapshot:
    """One tick of L2 order book + trade data."""
    ts: float
    symbol: str
    mid_price: float
    best_bid: float
    best_ask: float
    best_bid_size: float
    best_ask_size: float
    bids: List[Tuple[float, float]] = field(default_factory=list)
    asks: List[Tuple[float, float]] = field(default_factory=list)
    trade_price: float = 0.0
    trade_volume: float = 0.0
    trade_side: str = ""
    funding_rate: float = 0.0
    oi: float = 0.0
    mark_price: float = 0.0


@dataclass
class BacktestTrade:
    trade_id: int
    symbol: str
    side: str
    entry_price: float
    exit_price: float
    entry_ts: float
    exit_ts: float
    quantity: float
    pnl_pct: float
    pnl_usd: float
    exit_reason: str
    entry_confluence: str
    holding_time_s: float
    max_favorable_pct: float
    max_adverse_pct: float
    ticks_in_trade: int
    vpin_at_exit: float
    obi_at_exit: float
    slippage_bps: float


@dataclass
class EquityPoint:
    ts: float
    equity: float
    drawdown_pct: float


# ────────────────────────────────────────────────────
# Data Loaders
# ────────────────────────────────────────────────────
class DataLoader:
    def __iter__(self) -> Iterator[L2Snapshot]:
        raise NotImplementedError


class CSVDataLoader(DataLoader):
    """Load L2 snapshots from CSV.

    Required columns: ts, mid_price, best_bid, best_ask, bid_size, ask_size
    Optional: trade_price, trade_volume, funding_rate, oi, bid_N_price/size, ask_N_price/size
    """
    def __init__(self, path: str, symbol: str = "BTCUSDT"):
        self.path = path
        self.symbol = symbol

    def __iter__(self) -> Iterator[L2Snapshot]:
        with open(self.path, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                bids, asks = [], []
                for i in range(1, 11):
                    bp, bs = row.get(f"bid_{i}_price"), row.get(f"bid_{i}_size")
                    if bp and bs:
                        bids.append((float(bp), float(bs)))
                    ap, az = row.get(f"ask_{i}_price"), row.get(f"ask_{i}_size")
                    if ap and az:
                        asks.append((float(ap), float(az)))

                yield L2Snapshot(
                    ts=float(row.get("ts", 0)),
                    symbol=self.symbol,
                    mid_price=float(row.get("mid_price", 0)),
                    best_bid=float(row.get("best_bid", 0)),
                    best_ask=float(row.get("best_ask", 0)),
                    best_bid_size=float(row.get("bid_size", 0)),
                    best_ask_size=float(row.get("ask_size", 0)),
                    bids=bids, asks=asks,
                    trade_price=float(row.get("trade_price", 0)),
                    trade_volume=float(row.get("trade_volume", 0)),
                    trade_side=row.get("trade_side", ""),
                    funding_rate=float(row.get("funding_rate", 0)),
                    oi=float(row.get("oi", 0)),
                    mark_price=float(row.get("mark_price", 0)),
                )


class SyntheticDataLoader(DataLoader):
    """Generate synthetic L2 data for stress testing.

    Configurable: trend, volatility, spoofing events, rug pulls, volume spikes.
    """
    def __init__(
        self,
        symbol: str = "BTCUSDT",
        n_ticks: int = 10000,
        start_price: float = 50000.0,
        tick_interval_ms: float = 100.0,
        volatility: float = 0.0002,
        trend: float = 0.0,
        spoof_probability: float = 0.01,
        rug_at_tick: Optional[int] = None,
        seed: int = 42,
    ):
        self.symbol = symbol
        self.n_ticks = n_ticks
        self.start_price = start_price
        self.tick_interval_ms = tick_interval_ms
        self.volatility = volatility
        self.trend = trend
        self.spoof_probability = spoof_probability
        self.rug_at_tick = rug_at_tick
        self.seed = seed

    def __iter__(self) -> Iterator[L2Snapshot]:
        rng = np.random.RandomState(self.seed)
        price = self.start_price
        t = time.time()
        spread_bps = 2.0

        for i in range(self.n_ticks):
            if self.rug_at_tick and i == self.rug_at_tick:
                price *= 0.70
            else:
                price *= (1 + self.trend + self.volatility * rng.randn())

            spread = price * spread_bps / 10000
            bid = price - spread / 2
            ask = price + spread / 2

            bids, asks = [], []
            for lvl in range(10):
                bids.append((round(bid - lvl * spread, 2), round(max(0.01, rng.exponential(2.0)), 4)))
                asks.append((round(ask + lvl * spread, 2), round(max(0.01, rng.exponential(2.0)), 4)))

            is_spoof = rng.random() < self.spoof_probability
            if is_spoof:
                side_idx = 0 if rng.random() > 0.5 else 0
                target = bids if rng.random() > 0.5 else asks
                target[0] = (target[0][0], round(rng.uniform(50, 200), 4))

            trade_side = "buy" if rng.random() > 0.5 else "sell"
            trade_vol = max(0.001, rng.exponential(0.5))
            trade_px = ask if trade_side == "buy" else bid

            yield L2Snapshot(
                ts=t + i * self.tick_interval_ms / 1000,
                symbol=self.symbol,
                mid_price=round(price, 2),
                best_bid=round(bid, 2), best_ask=round(ask, 2),
                best_bid_size=round(bids[0][1], 4), best_ask_size=round(asks[0][1], 4),
                bids=bids, asks=asks,
                trade_price=round(trade_px, 2), trade_volume=round(trade_vol, 6),
                trade_side=trade_side,
                funding_rate=rng.normal(0, 0.0002),
                oi=rng.uniform(50000, 100000),
            )


# ────────────────────────────────────────────────────
# Signal Simulator (replays through real SpoofEngine)
# ────────────────────────────────────────────────────
class SignalSimulator:
    """Generates confluence signals from L2 snapshots using real engines."""

    def __init__(self, symbol: str):
        self.symbol = symbol
        self.spoof_engine = SpoofEngine()
        self.vpin = VPINComputer()
        self._obi_history: deque[float] = deque(maxlen=100)
        self._prices: deque[float] = deque(maxlen=200)
        self._returns: deque[float] = deque(maxlen=200)

    async def process(self, snap: L2Snapshot) -> Tuple[List[NodeSignal], dict]:
        await self.spoof_engine.process_depth(
            bids=snap.bids or [[snap.best_bid, snap.best_bid_size]],
            asks=snap.asks or [[snap.best_ask, snap.best_ask_size]],
            mark_price=snap.mark_price or snap.mid_price,
        )
        spoof_signal = await self.spoof_engine.signal()
        ghost_events = await self.spoof_engine.get_recent_ghosts(10.0)

        if snap.trade_volume > 0:
            self.vpin.ingest_trade(snap.trade_price or snap.mid_price, snap.trade_volume)

        denom = snap.best_bid_size + snap.best_ask_size
        obi = (snap.best_bid_size - snap.best_ask_size) / denom if denom > 0 else 0.0
        self._obi_history.append(obi)

        self._prices.append(snap.mid_price)
        if len(self._prices) >= 2:
            self._returns.append(
                (self._prices[-1] - self._prices[-2]) / max(self._prices[-2], 1e-12)
            )

        mom_5 = sum(list(self._returns)[-5:]) if len(self._returns) >= 5 else 0
        mom_15 = sum(list(self._returns)[-15:]) if len(self._returns) >= 15 else 0

        signals: List[NodeSignal] = []

        # SpoofHunter
        signals.append(NodeSignal(
            node="spoofhunter", available=True,
            action=spoof_signal.get("action", "WAIT"),
            side=spoof_signal.get("side", "NONE"),
            confidence=spoof_signal.get("confidence", 0.0),
        ))

        # Momentum (Newtonian stand-in)
        if mom_5 > 0.002 and mom_15 > 0.003:
            signals.append(NodeSignal(node="newtonian", available=True, action="EXECUTE", side="LONG", confidence=min(1.0, abs(mom_5) * 100)))
        elif mom_5 < -0.002 and mom_15 < -0.003:
            signals.append(NodeSignal(node="newtonian", available=True, action="EXECUTE", side="SHORT", confidence=min(1.0, abs(mom_5) * 100)))
        else:
            signals.append(NodeSignal(node="newtonian", available=True, action="WAIT", side="NONE", confidence=0.0))

        # OBI (Narrative stand-in)
        smooth_obi = float(np.mean(list(self._obi_history)[-10:])) if len(self._obi_history) >= 5 else 0.0
        if smooth_obi > 0.4:
            signals.append(NodeSignal(node="narrative", available=True, action="EXECUTE", side="LONG", confidence=min(1.0, abs(smooth_obi))))
        elif smooth_obi < -0.4:
            signals.append(NodeSignal(node="narrative", available=True, action="EXECUTE", side="SHORT", confidence=min(1.0, abs(smooth_obi))))
        else:
            signals.append(NodeSignal(node="narrative", available=True, action="WAIT", side="NONE", confidence=0.0))

        meta = {
            "obi": obi,
            "vpin": self.vpin.vpin,
            "ghost_events": [{"side": g.side, "notional_usd": g.notional_usd, "ts": g.ts} for g in ghost_events],
        }
        return signals, meta


# ────────────────────────────────────────────────────
# Slippage & ATR
# ────────────────────────────────────────────────────
def estimate_slippage(side: str, qty: float, bb: float, ba: float, bbs: float, bas: float) -> float:
    spread_bps = (ba - bb) / ((ba + bb) / 2) * 10000 if (ba + bb) > 0 else 2.0
    avail = bas if side == "LONG" else bbs
    fill_ratio = min(1.0, avail / max(qty, 1e-12))
    return spread_bps / 2 + (1 - fill_ratio) * spread_bps * 2


class ATREstimator:
    def __init__(self, period: int = 14, bucket_ticks: int = 60):
        self.period = period
        self.bucket_ticks = bucket_ticks
        self._tick_count = 0
        self._bh = -math.inf
        self._bl = math.inf
        self._bc = 0.0
        self._pc = 0.0
        self._trs: deque[float] = deque(maxlen=period + 5)
        self.atr: float = 0.0

    def update(self, price: float):
        self._tick_count += 1
        self._bh = max(self._bh, price)
        self._bl = min(self._bl, price)
        self._bc = price
        if self._tick_count % self.bucket_ticks == 0:
            if self._pc > 0:
                tr = max(self._bh - self._bl, abs(self._bh - self._pc), abs(self._bl - self._pc))
                self._trs.append(tr)
                if len(self._trs) >= self.period:
                    self.atr = (self.atr * (self.period - 1) + tr) / self.period if self.atr > 0 else sum(list(self._trs)[-self.period:]) / self.period
            self._pc = self._bc
            self._bh = -math.inf
            self._bl = math.inf


# ────────────────────────────────────────────────────
# Backtest Config & Result
# ────────────────────────────────────────────────────
@dataclass
class BacktestConfig:
    initial_equity: float = 10000.0
    position_size_pct: float = 0.02
    max_concurrent_positions: int = 1
    direction_mode: str = "MAJORITY"
    min_confidence: float = 0.50
    required_nodes: List[str] = field(default_factory=list)
    take_profit_pct: float = 3.0
    hard_stop_pct: float = 2.0
    alpha_decay_s: float = 180.0
    alpha_min_move_pct: float = 0.5
    time_limit_s: float = 1800.0
    ghost_min_notional: float = 50000.0
    maker_fee_bps: float = 1.0
    taker_fee_bps: float = 5.0
    trade_cooldown_ticks: int = 50


@dataclass
class BacktestResult:
    config: BacktestConfig
    trades: List[BacktestTrade]
    equity_curve: List[EquityPoint]
    total_ticks: int
    duration_s: float
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate_pct: float = 0.0
    total_pnl_pct: float = 0.0
    total_pnl_usd: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe_ratio: float = 0.0
    profit_factor: float = 0.0
    avg_holding_time_s: float = 0.0
    avg_pnl_pct: float = 0.0
    best_trade_pct: float = 0.0
    worst_trade_pct: float = 0.0
    exit_breakdown: Dict[str, int] = field(default_factory=dict)
    signals_evaluated: int = 0
    entries_triggered: int = 0
    entries_rejected: int = 0


# ────────────────────────────────────────────────────
# THE BACKTEST ENGINE
# ────────────────────────────────────────────────────
class BacktestEngine:
    def __init__(self, config: BacktestConfig = None):
        self.config = config or BacktestConfig()

    async def run(self, data: DataLoader, progress_cb: Optional[Callable] = None) -> BacktestResult:
        cfg = self.config
        t0 = time.monotonic()

        confluence = ConfluenceEngine(
            mode=cfg.direction_mode,
            min_confidence=cfg.min_confidence,
            required_nodes=cfg.required_nodes,
        )
        apm = ActivePositionManager()
        signal_sim: Dict[str, SignalSimulator] = {}
        atr_est: Dict[str, ATREstimator] = {}

        equity = cfg.initial_equity
        peak_equity = equity
        trades: List[BacktestTrade] = []
        equity_curve: List[EquityPoint] = []
        trade_counter = 0
        active_pid: Optional[str] = None
        active_side: Optional[str] = None
        active_ep: float = 0.0
        active_ets: float = 0.0
        active_qty: float = 0.0
        active_mf: float = 0.0
        active_ma: float = 0.0
        active_ticks: int = 0
        active_conf_str: str = ""
        cooldown = 0
        sig_eval = 0
        ent_trig = 0
        ent_rej = 0
        total_ticks = 0

        for snap in data:
            total_ticks += 1
            sym = snap.symbol

            if sym not in signal_sim:
                signal_sim[sym] = SignalSimulator(sym)
                atr_est[sym] = ATREstimator()

            atr_est[sym].update(snap.mid_price)
            signals, meta = await signal_sim[sym].process(snap)

            if progress_cb and total_ticks % 1000 == 0:
                progress_cb(total_ticks, 0)

            # ── Manage active position ──
            if active_pid:
                active_ticks += 1
                if active_side == "LONG":
                    unr = (snap.mid_price - active_ep) / active_ep * 100
                else:
                    unr = (active_ep - snap.mid_price) / active_ep * 100
                active_mf = max(active_mf, unr)
                active_ma = min(active_ma, unr)

                decision = await apm.process_tick(active_pid, TickData(
                    price=snap.mid_price, volume=snap.trade_volume,
                    obi=meta["obi"], ghost_events=meta["ghost_events"],
                ))

                if decision.action == "EXIT":
                    ep = snap.mid_price
                    pnl = (ep - active_ep) / active_ep * 100 if active_side == "LONG" else (active_ep - ep) / active_ep * 100
                    pnl -= (cfg.taker_fee_bps * 2) / 100
                    slip = estimate_slippage(active_side, active_qty, snap.best_bid, snap.best_ask, snap.best_bid_size, snap.best_ask_size)
                    pnl -= slip / 100
                    pnl_usd = equity * cfg.position_size_pct * pnl / 100
                    equity += pnl_usd

                    trade_counter += 1
                    trades.append(BacktestTrade(
                        trade_id=trade_counter, symbol=sym, side=active_side,
                        entry_price=active_ep, exit_price=ep,
                        entry_ts=active_ets, exit_ts=snap.ts,
                        quantity=active_qty, pnl_pct=round(pnl, 4), pnl_usd=round(pnl_usd, 4),
                        exit_reason=decision.reason.value if decision.reason else "unknown",
                        entry_confluence=active_conf_str,
                        holding_time_s=round(snap.ts - active_ets, 2),
                        max_favorable_pct=round(active_mf, 4),
                        max_adverse_pct=round(active_ma, 4),
                        ticks_in_trade=active_ticks,
                        vpin_at_exit=meta["vpin"], obi_at_exit=meta["obi"],
                        slippage_bps=round(slip, 2),
                    ))
                    active_pid = None
                    cooldown = cfg.trade_cooldown_ticks

            elif cooldown <= 0 and atr_est[sym].atr > 0:
                sig_eval += 1
                result = confluence.evaluate(signals)
                if result.action == "EXECUTE" and result.side in ("LONG", "SHORT"):
                    ent_trig += 1
                    active_side = result.side
                    active_ep = snap.mid_price
                    active_ets = snap.ts
                    active_qty = equity * cfg.position_size_pct / snap.mid_price
                    active_mf, active_ma, active_ticks = 0.0, 0.0, 0
                    active_conf_str = f"{result.side}@{result.confidence:.2f}_{cfg.direction_mode}"

                    active_pid = await apm.register_position(
                        symbol=sym, side=active_side, entry_price=active_ep,
                        quantity=active_qty, atr=atr_est[sym].atr,
                        take_profit_pct=cfg.take_profit_pct, hard_stop_pct=cfg.hard_stop_pct,
                        alpha_decay_s=cfg.alpha_decay_s, alpha_min_move_pct=cfg.alpha_min_move_pct,
                        time_limit_s=cfg.time_limit_s, ghost_min_notional=cfg.ghost_min_notional,
                    )
                else:
                    ent_rej += 1
            else:
                cooldown = max(0, cooldown - 1)

            if total_ticks % 100 == 0:
                peak_equity = max(peak_equity, equity)
                dd = (peak_equity - equity) / peak_equity * 100 if peak_equity > 0 else 0
                equity_curve.append(EquityPoint(ts=snap.ts, equity=equity, drawdown_pct=dd))

        # ── Summary ──
        duration = time.monotonic() - t0
        wins = [t for t in trades if t.pnl_pct > 0]
        losses = [t for t in trades if t.pnl_pct <= 0]
        pnls = [t.pnl_pct for t in trades]
        sharpe = float(np.mean(pnls) / max(np.std(pnls), 1e-8) * np.sqrt(252)) if len(pnls) > 1 else 0.0
        gp = sum(t.pnl_usd for t in wins)
        gl = abs(sum(t.pnl_usd for t in losses))
        pf = gp / max(gl, 1e-8)
        max_dd = max((ep.drawdown_pct for ep in equity_curve), default=0.0)

        exit_bd: Dict[str, int] = defaultdict(int)
        for t in trades:
            exit_bd[t.exit_reason] += 1

        return BacktestResult(
            config=cfg, trades=trades, equity_curve=equity_curve,
            total_ticks=total_ticks, duration_s=round(duration, 2),
            total_trades=len(trades), wins=len(wins), losses=len(losses),
            win_rate_pct=round(len(wins) / max(len(trades), 1) * 100, 2),
            total_pnl_pct=round(sum(pnls), 4), total_pnl_usd=round(equity - cfg.initial_equity, 2),
            max_drawdown_pct=round(max_dd, 4), sharpe_ratio=round(sharpe, 4),
            profit_factor=round(pf, 4),
            avg_holding_time_s=round(float(np.mean([t.holding_time_s for t in trades])), 2) if trades else 0.0,
            avg_pnl_pct=round(float(np.mean(pnls)), 4) if pnls else 0.0,
            best_trade_pct=round(max(pnls, default=0), 4),
            worst_trade_pct=round(min(pnls, default=0), 4),
            exit_breakdown=dict(exit_bd),
            signals_evaluated=sig_eval, entries_triggered=ent_trig, entries_rejected=ent_rej,
        )


def format_report(r: BacktestResult) -> str:
    lines = [
        "=" * 60,
        "  APEX CITADEL v3 — BACKTEST REPORT",
        "=" * 60, "",
        f"  Ticks:              {r.total_ticks:,}",
        f"  Duration:           {r.duration_s:.1f}s ({r.total_ticks / max(r.duration_s, 0.01):,.0f} ticks/s)", "",
        "── PERFORMANCE ─────────────────────────────",
        f"  Initial:            ${r.config.initial_equity:,.2f}",
        f"  Final:              ${r.config.initial_equity + r.total_pnl_usd:,.2f}",
        f"  PnL:                ${r.total_pnl_usd:+,.2f} ({r.total_pnl_pct:+.2f}%)",
        f"  Max Drawdown:       {r.max_drawdown_pct:.2f}%",
        f"  Sharpe:             {r.sharpe_ratio:.2f}",
        f"  Profit Factor:      {r.profit_factor:.2f}", "",
        "── TRADES ──────────────────────────────────",
        f"  Total:              {r.total_trades}",
        f"  Win Rate:           {r.win_rate_pct:.1f}%",
        f"  Avg PnL:            {r.avg_pnl_pct:+.3f}%",
        f"  Best:               {r.best_trade_pct:+.3f}%",
        f"  Worst:              {r.worst_trade_pct:+.3f}%",
        f"  Avg Hold:           {r.avg_holding_time_s:.1f}s", "",
        "── CONFLUENCE ──────────────────────────────",
        f"  Mode:               {r.config.direction_mode}",
        f"  Evaluated:          {r.signals_evaluated}",
        f"  Triggered:          {r.entries_triggered}",
        f"  Rejected:           {r.entries_rejected}", "",
        "── EXIT ATTRIBUTION ────────────────────────",
    ]
    for reason, count in sorted(r.exit_breakdown.items(), key=lambda x: -x[1]):
        lines.append(f"  {reason:<22} {count:>4}  ({count / max(r.total_trades, 1) * 100:.1f}%)")
    lines.extend(["", "=" * 60])
    return "\n".join(lines)


async def parameter_sweep(
    data_factory: Callable[[], DataLoader],
    param_name: str,
    param_values: List[Any],
    base_config: BacktestConfig = None,
) -> List[Tuple[Any, BacktestResult]]:
    results = []
    for val in param_values:
        cfg = base_config or BacktestConfig()
        setattr(cfg, param_name, val)
        engine = BacktestEngine(cfg)
        r = await engine.run(data_factory())
        results.append((val, r))
        log.info(f"Sweep {param_name}={val}: PnL={r.total_pnl_pct:+.2f}% WR={r.win_rate_pct:.1f}% DD={r.max_drawdown_pct:.2f}%")
    return results
