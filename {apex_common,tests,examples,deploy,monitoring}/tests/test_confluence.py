"""Tests for Apex Citadel v3 Confluence Engine."""

import pytest
from apex_common.confluence import (
    ConfluenceEngine,
    ConfluenceMode,
    NodeSignal,
)


# ──── Fixtures ────

def make_signal(node, action="EXECUTE", side="LONG", confidence=0.8, available=True, **meta):
    return NodeSignal(node=node, action=action, side=side, confidence=confidence, available=available, metadata=meta)


def engine(mode="MAJORITY", min_conf=0.55, required=None, weights=None):
    return ConfluenceEngine(
        mode=ConfluenceMode(mode),
        min_confidence=min_conf,
        required_nodes=required,
        node_weights=weights or {},
    )


# ──── SURVIVAL GATE ────

class TestSurvivalGate:
    def test_kill_from_any_survival_node_blocks(self):
        e = engine()
        signals = [
            make_signal("brain", action="EXECUTE", side="LONG", confidence=0.9, risk_multiplier=1.0),
            make_signal("spoofhunter", action="KILL", side="NONE", confidence=0.0),
        ]
        result = e.evaluate(signals)
        assert result.action == "KILL"
        assert result.side == "NONE"
        assert not result.should_execute

    def test_all_survival_pass(self):
        e = engine()
        signals = [
            make_signal("brain", action="EXECUTE", side="LONG", confidence=0.8, risk_multiplier=0.9),
            make_signal("spoofhunter", action="EXECUTE", side="LONG", confidence=0.7),
        ]
        result = e.evaluate(signals)
        assert result.action != "KILL"

    def test_no_survival_nodes_passes_by_default(self):
        """If no survival-role nodes are present, gate passes."""
        e = engine()
        signals = [
            make_signal("newtonian", action="EXECUTE", side="LONG", confidence=0.8),
        ]
        result = e.evaluate(signals)
        assert result.action != "KILL"


# ──── DIRECTION GATE — MAJORITY ────

class TestDirectionMajority:
    def test_majority_long(self):
        e = engine("MAJORITY")
        signals = [
            make_signal("brain", action="EXECUTE", side="LONG", confidence=0.8, risk_multiplier=1.0),
            make_signal("spoofhunter", action="EXECUTE", side="LONG", confidence=0.7),
            make_signal("newtonian", action="EXECUTE", side="SHORT", confidence=0.6),
        ]
        result = e.evaluate(signals)
        assert result.side == "LONG"

    def test_majority_short(self):
        e = engine("MAJORITY")
        signals = [
            make_signal("brain", action="EXECUTE", side="SHORT", confidence=0.8, risk_multiplier=1.0),
            make_signal("spoofhunter", action="EXECUTE", side="SHORT", confidence=0.7),
            make_signal("newtonian", action="EXECUTE", side="LONG", confidence=0.6),
        ]
        result = e.evaluate(signals)
        assert result.side == "SHORT"

    def test_no_majority_waits(self):
        e = engine("MAJORITY")
        signals = [
            make_signal("brain", action="EXECUTE", side="LONG", confidence=0.8, risk_multiplier=1.0),
            make_signal("spoofhunter", action="EXECUTE", side="SHORT", confidence=0.7),
        ]
        result = e.evaluate(signals)
        # 1 vs 1 = no majority
        assert result.action == "WAIT"

    def test_all_wait_means_no_direction(self):
        e = engine("MAJORITY")
        signals = [
            make_signal("brain", action="WAIT", side="NONE", confidence=0.3, risk_multiplier=1.0),
            make_signal("newtonian", action="WAIT", side="NONE", confidence=0.2),
        ]
        result = e.evaluate(signals)
        assert result.action == "WAIT"


# ──── DIRECTION GATE — AND ────

class TestDirectionAnd:
    def test_all_agree_long(self):
        e = engine("AND")
        signals = [
            make_signal("brain", action="EXECUTE", side="LONG", confidence=0.8, risk_multiplier=1.0),
            make_signal("spoofhunter", action="EXECUTE", side="LONG", confidence=0.7),
        ]
        result = e.evaluate(signals)
        assert result.side == "LONG"

    def test_one_disagrees_blocks(self):
        e = engine("AND")
        signals = [
            make_signal("brain", action="EXECUTE", side="LONG", confidence=0.8, risk_multiplier=1.0),
            make_signal("spoofhunter", action="EXECUTE", side="SHORT", confidence=0.7),
        ]
        result = e.evaluate(signals)
        assert result.action == "WAIT"


# ──── DIRECTION GATE — OR ────

class TestDirectionOr:
    def test_any_signal_triggers(self):
        e = engine("OR")
        signals = [
            make_signal("brain", action="EXECUTE", side="LONG", confidence=0.7, risk_multiplier=1.0),
            make_signal("newtonian", action="WAIT", side="NONE", confidence=0.3),
        ]
        result = e.evaluate(signals)
        assert result.side == "LONG"


# ──── DIRECTION GATE — WEIGHTED ────

class TestDirectionWeighted:
    def test_weighted_high_confidence_wins(self):
        e = engine("WEIGHTED", weights={"brain": 2.0, "newtonian": 1.0})
        signals = [
            make_signal("brain", action="EXECUTE", side="LONG", confidence=0.9, risk_multiplier=1.0),
            make_signal("newtonian", action="EXECUTE", side="SHORT", confidence=0.5),
        ]
        result = e.evaluate(signals)
        assert result.side == "LONG"


# ──── CONFIDENCE GATE ────

class TestConfidenceGate:
    def test_high_confidence_passes(self):
        e = engine("MAJORITY", min_conf=0.55)
        signals = [
            make_signal("brain", action="EXECUTE", side="LONG", confidence=0.85, risk_multiplier=1.0),
            make_signal("spoofhunter", action="EXECUTE", side="LONG", confidence=0.75),
            make_signal("newtonian", action="EXECUTE", side="LONG", confidence=0.70),
        ]
        result = e.evaluate(signals)
        assert result.action == "EXECUTE"
        assert result.confidence >= 0.55

    def test_low_confidence_waits(self):
        e = engine("MAJORITY", min_conf=0.80)
        signals = [
            make_signal("brain", action="EXECUTE", side="LONG", confidence=0.50, risk_multiplier=1.0),
            make_signal("spoofhunter", action="EXECUTE", side="LONG", confidence=0.40),
            make_signal("newtonian", action="EXECUTE", side="LONG", confidence=0.45),
        ]
        result = e.evaluate(signals)
        assert result.action == "WAIT"


# ──── RISK MULTIPLIER ────

class TestRiskMultiplier:
    def test_risk_multiplier_combined(self):
        e = engine("MAJORITY")
        signals = [
            make_signal("brain", action="EXECUTE", side="LONG", confidence=0.8, risk_multiplier=0.5),
            make_signal("dreamer", action="EXECUTE", side="LONG", confidence=0.7, risk_multiplier=0.8),
            make_signal("spoofhunter", action="EXECUTE", side="LONG", confidence=0.7),
        ]
        result = e.evaluate(signals)
        # brain * dreamer = 0.5 * 0.8 = 0.4
        assert result.risk_multiplier == pytest.approx(0.4, abs=0.01)


# ──── REQUIRED NODES ────

class TestRequiredNodes:
    def test_missing_required_node_fallback(self):
        e = engine("MAJORITY", required=["spoofhunter"])
        signals = [
            make_signal("brain", action="EXECUTE", side="LONG", confidence=0.9, risk_multiplier=1.0),
            make_signal("spoofhunter", available=False),
        ]
        result = e.evaluate(signals)
        assert result.action == "WAIT"

    def test_required_node_present_proceeds(self):
        e = engine("MAJORITY", required=["spoofhunter"])
        signals = [
            make_signal("brain", action="EXECUTE", side="LONG", confidence=0.8, risk_multiplier=1.0),
            make_signal("spoofhunter", action="EXECUTE", side="LONG", confidence=0.7),
            make_signal("newtonian", action="EXECUTE", side="LONG", confidence=0.6),
        ]
        result = e.evaluate(signals)
        assert result.action == "EXECUTE"


# ──── FULL PIPELINE ────

class TestFullPipeline:
    def test_execute_happy_path(self):
        e = engine("MAJORITY", min_conf=0.55)
        signals = [
            make_signal("brain", action="EXECUTE", side="LONG", confidence=0.85, risk_multiplier=0.9),
            make_signal("spoofhunter", action="EXECUTE", side="LONG", confidence=0.80),
            make_signal("newtonian", action="EXECUTE", side="LONG", confidence=0.75),
        ]
        result = e.evaluate(signals)
        assert result.action == "EXECUTE"
        assert result.side == "LONG"
        assert result.confidence >= 0.55
        assert result.risk_multiplier > 0

    def test_mixed_signals_wait(self):
        e = engine("AND", min_conf=0.55)
        signals = [
            make_signal("brain", action="EXECUTE", side="LONG", confidence=0.8, risk_multiplier=1.0),
            make_signal("spoofhunter", action="EXECUTE", side="SHORT", confidence=0.7),
        ]
        result = e.evaluate(signals)
        assert result.action == "WAIT"

    def test_empty_signals_wait(self):
        e = engine("MAJORITY")
        result = e.evaluate([])
        assert result.action in ("WAIT", "KILL")

    def test_confidence_clamped(self):
        sig = NodeSignal(node="test", confidence=1.5)
        assert sig.confidence == 1.0
        sig2 = NodeSignal(node="test", confidence=-0.3)
        assert sig2.confidence == 0.0
