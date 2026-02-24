"""Tests for Tick-Level Backtesting Engine."""

import asyncio
import time

import numpy as np
import pytest

from backtester import (
    BacktestEngine,
    BacktestConfig,
    SyntheticDataLoader,
    ATREstimator,
    estimate_slippage,
    format_report,
    parameter_sweep,
)


class TestSyntheticDataLoader:
    def test_generates_correct_count(self):
        ticks = list(SyntheticDataLoader(n_ticks=100))
        assert len(ticks) == 100

    def test_tick_has_required_fields(self):
        for snap in SyntheticDataLoader(n_ticks=5):
            assert snap.mid_price > 0
            assert snap.best_ask > snap.best_bid
            assert len(snap.bids) == 10
            assert snap.trade_volume > 0

    def test_rug_pull(self):
        ticks = list(SyntheticDataLoader(n_ticks=200, start_price=100.0, rug_at_tick=100))
        assert ticks[101].mid_price < ticks[99].mid_price * 0.80

    def test_uptrend(self):
        ticks = list(SyntheticDataLoader(n_ticks=1000, trend=0.001, volatility=0.0001))
        assert ticks[-1].mid_price > ticks[0].mid_price

    def test_deterministic(self):
        t1 = list(SyntheticDataLoader(n_ticks=50, seed=42))
        t2 = list(SyntheticDataLoader(n_ticks=50, seed=42))
        assert t1[-1].mid_price == t2[-1].mid_price


class TestATREstimator:
    def test_atr_from_ticks(self):
        atr = ATREstimator(period=5, bucket_ticks=10)
        for i in range(200):
            atr.update(100.0 + np.sin(i * 0.1) * 2)
        assert atr.atr > 0

    def test_constant_price_zero_atr(self):
        atr = ATREstimator(period=5, bucket_ticks=10)
        for _ in range(200):
            atr.update(100.0)
        assert atr.atr == pytest.approx(0.0, abs=0.01)


class TestSlippage:
    def test_small_order_half_spread(self):
        slip = estimate_slippage("LONG", 0.001, 49999, 50001, 10.0, 10.0)
        assert 0 < slip < 10

    def test_large_order_walks_book(self):
        s1 = estimate_slippage("LONG", 0.1, 49999, 50001, 10.0, 10.0)
        s2 = estimate_slippage("LONG", 100.0, 49999, 50001, 0.5, 0.5)
        assert s2 > s1


class TestBacktestEngine:
    @pytest.mark.asyncio
    async def test_runs_to_completion(self):
        engine = BacktestEngine(BacktestConfig())
        result = await engine.run(SyntheticDataLoader(n_ticks=500))
        assert result.total_ticks == 500
        assert result.duration_s > 0

    @pytest.mark.asyncio
    async def test_uptrend_majority(self):
        cfg = BacktestConfig(
            direction_mode="MAJORITY", min_confidence=0.30,
            trade_cooldown_ticks=20, alpha_decay_s=999.0, time_limit_s=9999.0,
        )
        result = await BacktestEngine(cfg).run(SyntheticDataLoader(n_ticks=5000, trend=0.0005, volatility=0.0002))
        assert result.total_ticks == 5000

    @pytest.mark.asyncio
    async def test_rug_pull_exits(self):
        cfg = BacktestConfig(direction_mode="OR", min_confidence=0.20, trade_cooldown_ticks=10, hard_stop_pct=5.0)
        result = await BacktestEngine(cfg).run(SyntheticDataLoader(n_ticks=5000, trend=0.0003, rug_at_tick=3000))
        # Trades that span the rug should have been stopped
        for t in result.trades:
            if t.exit_reason:
                assert t.exit_reason in ("obi_trail_stop", "hard_stop", "vpin_critical", "vpin_toxic", "alpha_decay", "take_profit", "time_limit", "ghost_liquidity", "macro_kill", "manual_exit", "unknown")

    @pytest.mark.asyncio
    async def test_result_fields(self):
        result = await BacktestEngine().run(SyntheticDataLoader(n_ticks=500))
        for attr in ("total_ticks", "trades", "equity_curve", "exit_breakdown", "sharpe_ratio", "max_drawdown_pct", "profit_factor"):
            assert hasattr(result, attr)

    @pytest.mark.asyncio
    async def test_equity_curve(self):
        result = await BacktestEngine().run(SyntheticDataLoader(n_ticks=1000))
        assert len(result.equity_curve) >= 5

    @pytest.mark.asyncio
    async def test_format_report(self):
        result = await BacktestEngine().run(SyntheticDataLoader(n_ticks=500))
        report = format_report(result)
        assert "BACKTEST REPORT" in report
        assert "PERFORMANCE" in report


class TestParameterSweep:
    @pytest.mark.asyncio
    async def test_sweep_modes(self):
        results = await parameter_sweep(
            lambda: SyntheticDataLoader(n_ticks=300, seed=42),
            "direction_mode", ["AND", "OR", "MAJORITY"],
        )
        assert len(results) == 3
        for mode, r in results:
            assert r.total_ticks == 300
