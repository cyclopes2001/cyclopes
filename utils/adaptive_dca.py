"""Adaptive DCA approval for paired inventory accumulation.

A DCA buy on the heavy (unmatched) side is approved only when it improves
hedge feasibility against the *actual* opposite ask — not merely because
the heavy side got cheaper.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from utils.paired_inventory_ledger import new_average_cost


_EPS = 1e-9


class DcaDecision(str, Enum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"


@dataclass(frozen=True)
class DcaEvaluation:
    decision: DcaDecision
    reason: str
    side: str
    qty: float
    proposed_price: float
    current_avg: float
    new_avg: float
    current_max_hedge: float
    new_max_hedge: float
    opposite_ask: float
    projected_pair_cost: Optional[float]
    hedge_feasible_now: bool
    hedge_feasible_after: bool
    improves_economics: bool


def evaluate_adaptive_dca(
    *,
    side: str,
    existing_shares: float,
    existing_total_cost: float,
    proposed_qty: float,
    proposed_price: float,
    opposite_ask: float,
    target_combined: float,
    max_inventory: float,
    unmatched_target: float,
    max_dca_legs: int,
    current_dca_legs: int,
    fee_rate: float = 0.0,
    slippage_reserve: float = 0.0,
    min_improvement: float = 0.0,
    require_hedge_feasible_after: bool = True,
    seconds_left: float = 300.0,
    min_seconds_left: float = 30.0,
    available_liquidity: float = 0.0,
) -> DcaEvaluation:
    """Approve DCA only when pairing economics improve with a credible hedge path."""
    s = side.upper()
    qty = float(proposed_qty)
    px = float(proposed_price)
    existing = float(existing_shares)
    cost = float(existing_total_cost)
    opp = float(opposite_ask)
    target = float(target_combined)
    fee_mult = 1.0 + max(0.0, float(fee_rate))
    slip = max(0.0, float(slippage_reserve))

    def _reject(reason: str, **kwargs) -> DcaEvaluation:
        cur_avg = (cost / existing) if existing > _EPS else 0.0
        new_avg = kwargs.get("new_avg", cur_avg)
        cur_max = max(0.0, target - cur_avg) if cur_avg > _EPS else 0.0
        new_max = max(0.0, target - new_avg) if new_avg > _EPS else 0.0
        proj = kwargs.get("projected_pair_cost")
        return DcaEvaluation(
            decision=DcaDecision.REJECT,
            reason=reason,
            side=s,
            qty=qty,
            proposed_price=px,
            current_avg=cur_avg,
            new_avg=new_avg,
            current_max_hedge=cur_max,
            new_max_hedge=new_max,
            opposite_ask=opp,
            projected_pair_cost=proj,
            hedge_feasible_now=kwargs.get("hedge_feasible_now", False),
            hedge_feasible_after=kwargs.get("hedge_feasible_after", False),
            improves_economics=False,
        )

    if qty <= _EPS or px <= 0:
        return _reject("BUY REJECTED: invalid DCA quantity or price")
    if existing <= _EPS:
        return _reject("BUY REJECTED: no existing inventory for DCA")
    if existing + qty > float(max_inventory) + _EPS:
        return _reject("BUY REJECTED: would exceed maximum inventory per side")
    if int(current_dca_legs) >= int(max_dca_legs):
        return _reject("BUY REJECTED: maximum DCA legs per side reached")
    if float(seconds_left) < float(min_seconds_left):
        return _reject("BUY REJECTED: insufficient market time remaining for DCA")
    if float(available_liquidity) + _EPS < qty:
        return _reject("BUY REJECTED: insufficient liquidity for DCA size")

    # Soft unmatched target: DCA beyond target is the adaptive exception path.
    if existing + _EPS < float(unmatched_target):
        # Still below soft target — treat as normal accumulation, not adaptive DCA.
        return _reject("BUY REJECTED: unmatched inventory below soft target; use initial entry path")

    cur_avg = cost / existing
    new_purchase_cost = qty * px * fee_mult
    new_avg = new_average_cost(existing, cost, qty, px * fee_mult)
    cur_max = max(0.0, target - cur_avg)
    new_max = max(0.0, target - new_avg)
    opp_exec = opp + slip

    hedge_now = opp_exec <= cur_max + _EPS and cur_max > _EPS
    hedge_after = opp_exec <= new_max + _EPS and new_max > _EPS
    projected = new_avg + opp_exec
    improves = new_avg + _EPS < cur_avg - float(min_improvement)
    expands_hedge_room = new_max > cur_max + _EPS

    if not improves and not expands_hedge_room:
        label = "UP" if s == "YES" else "DOWN"
        return _reject(
            f"BUY {label} REJECTED: Existing unmatched inventory and proposed DCA does not "
            "improve expected net pairing economics.",
            new_avg=new_avg,
            projected_pair_cost=projected,
            hedge_feasible_now=hedge_now,
            hedge_feasible_after=hedge_after,
        )

    # Spec example: UP 34¢ then 24¢ while DOWN stays 76¢ → pair cost 1.05 → reject.
    if projected > target + _EPS:
        if require_hedge_feasible_after and not hedge_after:
            return _reject(
                "BUY REJECTED: proposed DCA does not create a credible hedge at the "
                f"current opposite ask ({opp_exec:.4f} > max {new_max:.4f})",
                new_avg=new_avg,
                projected_pair_cost=projected,
                hedge_feasible_now=hedge_now,
                hedge_feasible_after=False,
            )
        return _reject(
            "BUY REJECTED: projected combined cost including fees exceeds target",
            new_avg=new_avg,
            projected_pair_cost=projected,
            hedge_feasible_now=hedge_now,
            hedge_feasible_after=hedge_after,
        )

    if require_hedge_feasible_after and not hedge_after:
        return _reject(
            "BUY REJECTED: opposite-side ask still exceeds max hedge after DCA",
            new_avg=new_avg,
            projected_pair_cost=projected,
            hedge_feasible_now=hedge_now,
            hedge_feasible_after=False,
        )

    side_label = "UP" if s == "YES" else "DOWN"
    return DcaEvaluation(
        decision=DcaDecision.APPROVE,
        reason=(
            f"DCA APPROVED: Additional {side_label} purchase reduces weighted-average "
            f"acquisition cost and passes hedge feasibility and risk checks."
        ),
        side=s,
        qty=qty,
        proposed_price=px,
        current_avg=cur_avg,
        new_avg=new_avg,
        current_max_hedge=cur_max,
        new_max_hedge=new_max,
        opposite_ask=opp,
        projected_pair_cost=projected,
        hedge_feasible_now=hedge_now,
        hedge_feasible_after=hedge_after,
        improves_economics=True,
    )
