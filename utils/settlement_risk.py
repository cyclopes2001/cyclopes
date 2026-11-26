"""Settlement scenario PnL and unmatched-inventory projection for paired markets."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Tuple

if TYPE_CHECKING:
    from utils.paired_decision_engine import ActionKind
    from utils.paired_inventory_ledger import PairedInventoryLedger

_EPS = 1e-9


@dataclass(frozen=True)
class SettlementOutlook:
    total_acquisition_cost: float
    yes_shares: float
    no_shares: float
    matched_pairs: float
    unmatched_yes: float
    unmatched_no: float
    yes_settlement_value: float
    no_settlement_value: float
    pnl_if_yes_wins: float
    pnl_if_no_wins: float
    worst_case_pnl: float
    best_case_pnl: float


def _unmatched_from_totals(yes_total: float, no_total: float) -> Tuple[float, float]:
    matched = min(yes_total, no_total)
    return max(0.0, yes_total - matched), max(0.0, no_total - matched)


def projected_totals(
    ledger: "PairedInventoryLedger",
    side: str,
    qty: float,
) -> Tuple[float, float]:
    y = ledger.total_shares("YES")
    n = ledger.total_shares("NO")
    s = side.upper()
    if s == "YES":
        y += qty
    elif s == "NO":
        n += qty
    return y, n


def projected_unmatched(
    ledger: "PairedInventoryLedger",
    side: str,
    qty: float,
) -> Tuple[float, float]:
    y, n = projected_totals(ledger, side, qty)
    return _unmatched_from_totals(y, n)


def projected_unmatched_with_pending(
    ledger: "PairedInventoryLedger",
    side: str,
    qty: float,
    *,
    pending_side: str | None = None,
    pending_remaining_qty: float = 0.0,
) -> Tuple[float, float]:
    y, n = projected_totals(ledger, side, qty)
    if pending_side and pending_remaining_qty > _EPS:
        ps = pending_side.upper()
        if ps == "YES":
            y += pending_remaining_qty
        elif ps == "NO":
            n += pending_remaining_qty
    return _unmatched_from_totals(y, n)


def exceeds_unmatched_cap(
    um_yes: float,
    um_no: float,
    *,
    max_unmatched_per_side: float,
    buy_side: str,
) -> bool:
    cap = float(max_unmatched_per_side)
    s = buy_side.upper()
    if s == "YES" and um_yes > cap + _EPS:
        return True
    if s == "NO" and um_no > cap + _EPS:
        return True
    return False


def settlement_outcomes(
    *,
    yes_shares: float,
    no_shares: float,
    total_cost: float,
) -> SettlementOutlook:
    matched = min(yes_shares, no_shares)
    um_y, um_n = _unmatched_from_totals(yes_shares, no_shares)
    yes_val = yes_shares  # $1 per share if YES wins
    no_val = no_shares   # $1 per share if NO wins
    pnl_yes = yes_val - total_cost
    pnl_no = no_val - total_cost
    return SettlementOutlook(
        total_acquisition_cost=round(total_cost, 4),
        yes_shares=round(yes_shares, 4),
        no_shares=round(no_shares, 4),
        matched_pairs=round(matched, 4),
        unmatched_yes=round(um_y, 4),
        unmatched_no=round(um_n, 4),
        yes_settlement_value=round(yes_val, 4),
        no_settlement_value=round(no_val, 4),
        pnl_if_yes_wins=round(pnl_yes, 4),
        pnl_if_no_wins=round(pnl_no, 4),
        worst_case_pnl=round(min(pnl_yes, pnl_no), 4),
        best_case_pnl=round(max(pnl_yes, pnl_no), 4),
    )


def verify_sample_settlement_loss() -> bool:
    """Reconcile the Nov 26 BTC example: 15 YES, 10 NO, cost 11.15, NO wins → -1.15."""
    o = settlement_outcomes(yes_shares=15.0, no_shares=10.0, total_cost=11.15)
    return abs(o.pnl_if_no_wins - (-1.15)) < 1e-6 and abs(o.unmatched_yes - 5.0) < 1e-6