"""Configurable EARLY / MIDDLE / LATE phases for 5-minute markets."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class MarketPhase(str, Enum):
    EARLY = "EARLY"
    MIDDLE = "MIDDLE"
    LATE = "LATE"
    CLOSED = "CLOSED"


@dataclass(frozen=True)
class PhasePolicy:
    """Seconds-left thresholds (inclusive lower bound for each phase)."""

    early_min_seconds_left: float = 180.0
    middle_min_seconds_left: float = 60.0
    late_min_seconds_left: float = 0.0
    stop_new_accumulation_seconds: float = 90.0
    stop_initial_entry_seconds: float = 120.0
    stop_unmatched_dca_seconds: float = 90.0
    hedge_priority_seconds: float = 60.0
    final_risk_seconds: float = 30.0
    force_exit_eval_seconds: float = 45.0


def resolve_phase(seconds_left: float, policy: PhasePolicy) -> MarketPhase:
    left = float(seconds_left)
    if left <= 0:
        return MarketPhase.CLOSED
    if left > policy.early_min_seconds_left:
        return MarketPhase.EARLY
    if left > policy.middle_min_seconds_left:
        return MarketPhase.MIDDLE
    return MarketPhase.LATE


def allow_new_accumulation(seconds_left: float, policy: PhasePolicy) -> bool:
    return float(seconds_left) > float(policy.stop_new_accumulation_seconds)


def should_evaluate_exit(seconds_left: float, policy: PhasePolicy) -> bool:
    return 0 < float(seconds_left) <= float(policy.force_exit_eval_seconds)
