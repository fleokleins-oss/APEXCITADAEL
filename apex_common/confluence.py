"""Apex Citadel v3 — Confluence Gate Engine.

Evaluates signals from multiple upstream nodes through configurable
Boolean gates to produce a unified trading decision.

Gate hierarchy:
  1. SURVIVAL gate: Hard kill if any risk node rejects (AND logic)
  2. DIRECTION gate: Majority of directional nodes must agree on side
  3. CONFIDENCE gate: Weighted average confidence must exceed threshold
  4. EXECUTION gate: All three sub-gates must pass (AND)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Literal, Optional

Action = Literal["EXECUTE", "WAIT", "KILL"]
Side = Literal["LONG", "SHORT", "NONE"]


@dataclass
class NodeSignal:
    """Standardized signal from any upstream node."""
    node: str                       # e.g. "spoofhunter", "newtonian", "brain"
    action: Action = "WAIT"
    side: Side = "NONE"
    confidence: float = 0.0         # [0, 1]
    available: bool = True          # False if node was unavailable / timed out
    metadata: Dict = field(default_factory=dict)

    def __post_init__(self):
        self.confidence = max(0.0, min(1.0, float(self.confidence)))


class ConfluenceMode(str, Enum):
    AND = "AND"                 # All directional nodes must agree
    OR = "OR"                   # Any directional node triggers
    MAJORITY = "MAJORITY"       # >50% of directional nodes agree on side
    WEIGHTED = "WEIGHTED"       # Weighted confidence threshold


@dataclass
class GateResult:
    """Result of a single gate evaluation."""
    name: str
    passed: bool
    reason: str


@dataclass
class ConfluenceResult:
    """Full result of confluence evaluation across all gates."""
    action: Action
    side: Side
    confidence: float           # Final weighted confidence
    risk_multiplier: float      # Combined risk scaling factor
    gates: List[GateResult]
    signals: List[NodeSignal]
    reasoning: List[str]

    @property
    def should_execute(self) -> bool:
        return self.action == "EXECUTE"


# ──── Gate Roles ────
# Nodes are classified into roles to determine which gate evaluates them.
# A node can have multiple roles.

ROLE_SURVIVAL = "survival"      # Hard gate: any KILL → abort
ROLE_DIRECTION = "direction"    # Directional signal: side + confidence
ROLE_RISK = "risk"              # Risk multiplier provider

# Default node → role mapping (can be overridden via config)
DEFAULT_NODE_ROLES: Dict[str, List[str]] = {
    # v2 legacy nodes
    "brain":        [ROLE_DIRECTION, ROLE_RISK, ROLE_SURVIVAL],
    "shadowglass":  [ROLE_DIRECTION],

    # v3 nodes
    "spoofhunter":  [ROLE_DIRECTION, ROLE_SURVIVAL],
    "newtonian":    [ROLE_DIRECTION],
    "narrative":    [ROLE_DIRECTION],
    "antirug_v3":   [ROLE_SURVIVAL],
    "dreamer":      [ROLE_DIRECTION, ROLE_RISK],
    "econopredator": [],  # data-only, no signal
    "jito_spoof":   [],   # execution node, no signal
}


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


class ConfluenceEngine:
    """Evaluates multiple node signals through Boolean confluence gates."""

    def __init__(
        self,
        mode: ConfluenceMode = ConfluenceMode.MAJORITY,
        min_confidence: float = 0.55,
        node_weights: Optional[Dict[str, float]] = None,
        node_roles: Optional[Dict[str, List[str]]] = None,
        required_nodes: Optional[List[str]] = None,
        fallback_on_timeout: Action = "WAIT",
    ):
        self.mode = mode
        self.min_confidence = min_confidence
        self.node_weights = node_weights or {}
        self.node_roles = node_roles or DEFAULT_NODE_ROLES
        self.required_nodes = set(required_nodes or [])
        self.fallback_on_timeout = fallback_on_timeout

    def _get_roles(self, node: str) -> List[str]:
        return self.node_roles.get(node, [])

    def _get_weight(self, node: str) -> float:
        return float(self.node_weights.get(node, 1.0))

    def _signals_with_role(self, signals: List[NodeSignal], role: str) -> List[NodeSignal]:
        return [s for s in signals if role in self._get_roles(s.node) and s.available]

    # ──── SURVIVAL GATE ────
    def _eval_survival(self, signals: List[NodeSignal]) -> GateResult:
        """Hard gate: ANY survival node with KILL → gate fails."""
        survival_nodes = self._signals_with_role(signals, ROLE_SURVIVAL)
        if not survival_nodes:
            return GateResult("SURVIVAL", True, "No survival nodes available; pass by default")

        killers = [s for s in survival_nodes if s.action == "KILL"]
        if killers:
            names = ", ".join(s.node for s in killers)
            return GateResult("SURVIVAL", False, f"KILL from: {names}")

        return GateResult("SURVIVAL", True, f"All {len(survival_nodes)} survival nodes passed")

    # ──── DIRECTION GATE ────
    def _eval_direction(self, signals: List[NodeSignal]) -> tuple[GateResult, Side]:
        """Evaluate directional agreement based on confluence mode."""
        dir_nodes = self._signals_with_role(signals, ROLE_DIRECTION)
        executing = [s for s in dir_nodes if s.action == "EXECUTE" and s.side in ("LONG", "SHORT")]

        if not executing:
            return GateResult("DIRECTION", False, "No directional signals (all WAIT/KILL)"), "NONE"

        longs = [s for s in executing if s.side == "LONG"]
        shorts = [s for s in executing if s.side == "SHORT"]
        total = len(executing)

        if self.mode == ConfluenceMode.AND:
            # All must agree on same side
            if len(longs) == total:
                return GateResult("DIRECTION", True, f"AND: all {total} agree LONG"), "LONG"
            if len(shorts) == total:
                return GateResult("DIRECTION", True, f"AND: all {total} agree SHORT"), "SHORT"
            return GateResult("DIRECTION", False, f"AND: split {len(longs)}L/{len(shorts)}S of {total}"), "NONE"

        if self.mode == ConfluenceMode.OR:
            # Any signal is enough; take majority direction
            side: Side = "LONG" if len(longs) >= len(shorts) else "SHORT"
            return GateResult("DIRECTION", True, f"OR: {len(longs)}L/{len(shorts)}S → {side}"), side

        if self.mode == ConfluenceMode.MAJORITY:
            threshold = total / 2.0
            if len(longs) > threshold:
                return GateResult("DIRECTION", True, f"MAJORITY: {len(longs)}/{total} LONG"), "LONG"
            if len(shorts) > threshold:
                return GateResult("DIRECTION", True, f"MAJORITY: {len(shorts)}/{total} SHORT"), "SHORT"
            return GateResult("DIRECTION", False, f"MAJORITY: no majority ({len(longs)}L/{len(shorts)}S of {total})"), "NONE"

        if self.mode == ConfluenceMode.WEIGHTED:
            # Weighted vote: sum(weight * confidence * direction_sign)
            weighted_score = 0.0
            total_weight = 0.0
            for s in executing:
                w = self._get_weight(s.node)
                sign = 1.0 if s.side == "LONG" else -1.0
                weighted_score += w * s.confidence * sign
                total_weight += w * s.confidence

            if total_weight < 1e-9:
                return GateResult("DIRECTION", False, "WEIGHTED: zero total weight"), "NONE"

            normalized = weighted_score / total_weight  # [-1, 1]
            if abs(normalized) >= 0.1:  # Threshold for directional conviction
                side = "LONG" if normalized > 0 else "SHORT"
                return GateResult("DIRECTION", True, f"WEIGHTED: score={normalized:.3f} → {side}"), side
            return GateResult("DIRECTION", False, f"WEIGHTED: score={normalized:.3f} below conviction threshold"), "NONE"

        return GateResult("DIRECTION", False, f"Unknown mode: {self.mode}"), "NONE"

    # ──── CONFIDENCE GATE ────
    def _eval_confidence(self, signals: List[NodeSignal], target_side: Side) -> tuple[GateResult, float]:
        """Weighted average confidence of nodes agreeing with target_side."""
        dir_nodes = self._signals_with_role(signals, ROLE_DIRECTION)
        agreeing = [s for s in dir_nodes if s.available and s.side == target_side and s.action == "EXECUTE"]

        if not agreeing:
            return GateResult("CONFIDENCE", False, "No nodes agreeing with target side"), 0.0

        total_weight = 0.0
        weighted_conf = 0.0
        for s in agreeing:
            w = self._get_weight(s.node)
            weighted_conf += w * s.confidence
            total_weight += w

        avg_conf = weighted_conf / max(total_weight, 1e-9)

        if avg_conf >= self.min_confidence:
            return GateResult("CONFIDENCE", True, f"avg_conf={avg_conf:.3f} >= {self.min_confidence}"), avg_conf
        return GateResult("CONFIDENCE", False, f"avg_conf={avg_conf:.3f} < {self.min_confidence}"), avg_conf

    # ──── RISK MULTIPLIER ────
    def _compute_risk_multiplier(self, signals: List[NodeSignal]) -> tuple[float, str]:
        """Combine risk multipliers from risk-role nodes. Product of all multipliers."""
        risk_nodes = self._signals_with_role(signals, ROLE_RISK)
        if not risk_nodes:
            return 1.0, "No risk nodes; default multiplier 1.0"

        multiplier = 1.0
        parts = []
        for s in risk_nodes:
            rm = float(s.metadata.get("risk_multiplier", 1.0))
            rm = _clamp(rm, 0.0, 1.0)
            multiplier *= rm
            parts.append(f"{s.node}={rm:.2f}")

        multiplier = _clamp(multiplier, 0.0, 1.0)
        return multiplier, f"risk_mult={multiplier:.3f} ({', '.join(parts)})"

    # ──── REQUIRED NODES CHECK ────
    def _check_required(self, signals: List[NodeSignal]) -> tuple[bool, str]:
        """Check that all required nodes responded."""
        if not self.required_nodes:
            return True, "No required nodes configured"

        missing = []
        for node_name in self.required_nodes:
            found = [s for s in signals if s.node == node_name and s.available]
            if not found:
                missing.append(node_name)

        if missing:
            return False, f"Required nodes unavailable: {', '.join(missing)}"
        return True, "All required nodes responded"

    # ──── MAIN EVALUATE ────
    def evaluate(self, signals: List[NodeSignal]) -> ConfluenceResult:
        """Run all gates and produce a unified decision."""
        reasoning: List[str] = []
        gates: List[GateResult] = []

        # 0. Required nodes check
        req_ok, req_msg = self._check_required(signals)
        reasoning.append(f"[REQUIRED] {req_msg}")
        if not req_ok:
            reasoning.append(f"[FALLBACK] Required nodes missing → {self.fallback_on_timeout}")
            return ConfluenceResult(
                action=self.fallback_on_timeout,
                side="NONE",
                confidence=0.0,
                risk_multiplier=0.0,
                gates=gates,
                signals=signals,
                reasoning=reasoning,
            )

        # 1. SURVIVAL gate
        survival = self._eval_survival(signals)
        gates.append(survival)
        reasoning.append(f"[SURVIVAL] {'PASS' if survival.passed else 'FAIL'}: {survival.reason}")
        if not survival.passed:
            return ConfluenceResult(
                action="KILL",
                side="NONE",
                confidence=0.0,
                risk_multiplier=0.0,
                gates=gates,
                signals=signals,
                reasoning=reasoning,
            )

        # 2. DIRECTION gate
        direction, target_side = self._eval_direction(signals)
        gates.append(direction)
        reasoning.append(f"[DIRECTION] {'PASS' if direction.passed else 'FAIL'}: {direction.reason}")
        if not direction.passed:
            return ConfluenceResult(
                action="WAIT",
                side="NONE",
                confidence=0.0,
                risk_multiplier=0.0,
                gates=gates,
                signals=signals,
                reasoning=reasoning,
            )

        # 3. CONFIDENCE gate
        conf_gate, avg_conf = self._eval_confidence(signals, target_side)
        gates.append(conf_gate)
        reasoning.append(f"[CONFIDENCE] {'PASS' if conf_gate.passed else 'FAIL'}: {conf_gate.reason}")
        if not conf_gate.passed:
            return ConfluenceResult(
                action="WAIT",
                side=target_side,
                confidence=avg_conf,
                risk_multiplier=0.0,
                gates=gates,
                signals=signals,
                reasoning=reasoning,
            )

        # 4. Risk multiplier
        risk_mult, risk_msg = self._compute_risk_multiplier(signals)
        reasoning.append(f"[RISK] {risk_msg}")

        # All gates passed → EXECUTE
        reasoning.append(f"[CONFLUENCE] ALL GATES PASSED → EXECUTE {target_side} conf={avg_conf:.3f} risk_mult={risk_mult:.3f}")

        return ConfluenceResult(
            action="EXECUTE",
            side=target_side,
            confidence=avg_conf,
            risk_multiplier=risk_mult,
            gates=gates,
            signals=signals,
            reasoning=reasoning,
        )
