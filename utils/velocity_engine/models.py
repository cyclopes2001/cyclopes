"""Shared models for velocity engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional, Tuple


class VelocitySignalState(str, Enum):
    FALLING_FAST = "FALLING_FAST"
    FALLING_SLOW = "FALLING_SLOW"
    STABILIZING = "STABILIZING"
    REVERSAL_CONFIRMED = "REVERSAL_CONFIRMED"
    RISING_SLOW = "RISING_SLOW"
    RISING_FAST = "RISING_FAST"
    UNCONFIRMED = "UNCONFIRMED"


class EntryClassification(str, Enum):
    INITIAL_ENTRY = "INITIAL_ENTRY"
    QUALIFYING_HEDGE = "QUALIFYING_HEDGE"
    VELOCITY_ENTRY = "VELOCITY_ENTRY"
    RECOVERY_DCA = "RECOVERY_DCA"
    RISK_REDUCTION = "RISK_REDUCTION"
    NONE = "NONE"


class VelocityTradeDecision(str, Enum):
    BUY_UP = "BUY UP"
    BUY_DOWN = "BUY DOWN"
    WAIT = "WAIT"


@dataclass
class SideMetrics:
    state: VelocitySignalState = VelocitySignalState.UNCONFIRMED
    mid_velocity: float = 0.0
    bid_velocity: float = 0.0
    ask_velocity: float = 0.0
    acceleration: float = 0.0
    obi: float = 0.0
    ofi: float = 0.0
    confidence: float = 0.0
    best_bid: float = 0.0
    best_ask: float = 0.0
    spread: float = 0.0
    bid_depth_5: float = 0.0
    ask_depth_5: float = 0.0
    stale: bool = True
    invalid_book: bool = False
    observation_count: int = 0


@dataclass
class VelocityContext:
    """Advisory context passed into the paired decision engine."""

    enabled: bool = False
    valid: bool = False
    stale: bool = True
    confidence: float = 0.0
    preferred_side: Optional[str] = None
    decision: VelocityTradeDecision = VelocityTradeDecision.WAIT
    reason: str = ""
    entry_classification: EntryClassification = EntryClassification.NONE
    avoid_buy_sides: Tuple[str, ...] = ()
    allow_recovery_dca_sides: Tuple[str, ...] = ()
    up: SideMetrics = field(default_factory=SideMetrics)
    down: SideMetrics = field(default_factory=SideMetrics)
    signal_valid: bool = False
    persisted_ms: float = 0.0


@dataclass
class VelocityDecision:
    """Full engine output for dashboard and audit logs."""

    market_id: str = ""
    ts: float = 0.0
    valid: bool = False
    stale: bool = True
    confidence: float = 0.0
    preferred_side: Optional[str] = None
    decision: str = "WAIT"
    reason: str = ""
    entry_classification: str = EntryClassification.NONE.value
    up_state: str = VelocitySignalState.UNCONFIRMED.value
    down_state: str = VelocitySignalState.UNCONFIRMED.value
    up_mid_velocity: float = 0.0
    down_mid_velocity: float = 0.0
    up_obi: float = 0.0
    down_obi: float = 0.0
    up_ofi: float = 0.0
    down_ofi: float = 0.0
    up_acceleration: float = 0.0
    down_acceleration: float = 0.0
    signal_valid: bool = False

    def to_dashboard_dict(self) -> Dict[str, object]:
        up_label = "UP"
        down_label = "DOWN"
        pref = "UP" if self.preferred_side == "YES" else "DOWN" if self.preferred_side == "NO" else "—"
        return {
            "enabled": True,
            "valid": self.valid,
            "stale": self.stale,
            "signal_valid": self.signal_valid,
            "up_state": self.up_state,
            "down_state": self.down_state,
            "up_velocity": round(self.up_mid_velocity, 6),
            "down_velocity": round(self.down_mid_velocity, 6),
            "up_obi": round(self.up_obi, 4),
            "down_obi": round(self.down_obi, 4),
            "up_ofi": round(self.up_ofi, 4),
            "down_ofi": round(self.down_ofi, 4),
            "confidence": round(self.confidence, 1),
            "preferred_side": pref,
            "decision": self.decision,
            "reason": self.reason,
            "entry_classification": self.entry_classification,
            "up_label": up_label,
            "down_label": down_label,
        }

    def to_audit_dict(self) -> Dict[str, object]:
        d = self.to_dashboard_dict()
        d["market_id"] = self.market_id
        d["ts"] = self.ts
        d["up_acceleration"] = self.up_acceleration
        d["down_acceleration"] = self.down_acceleration
        return d