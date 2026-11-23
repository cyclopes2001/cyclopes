"""Safety-only market helpers for the directional single-leg strategy.

Legacy momentum, spread-capture, market-making, and external feed hooks are removed.
This module retains position accounting only.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot import MarketWorker

_EPS = 1e-9


@dataclass(frozen=True)
class EconomicPosition:
    yes_shares: float
    no_shares: float
    yes_avg: float
    no_avg: float
    invested: float
    position_delta: float
    pnl_if_yes: float
    pnl_if_no: float
    mark_pnl: float
    matched_pairs: float
    locked_profit: float

    @property
    def unpaired(self) -> float:
        return abs(self.yes_shares - self.no_shares)

    @property
    def combined_avg(self) -> float:
        if self.yes_shares <= _EPS or self.no_shares <= _EPS:
            return 0.0
        return self.yes_avg + self.no_avg


def economic_position(worker: "MarketWorker") -> EconomicPosition:
    inv = worker.inventory
    ys = float(inv.shares("YES"))
    ns = float(inv.shares("NO"))
    ya = float(inv.avg_cost("YES")) if ys > _EPS else 0.0
    na = float(inv.avg_cost("NO")) if ns > _EPS else 0.0
    invested = ys * ya + ns * na
    pnl_yes = ys - invested
    pnl_no = ns - invested
    y_bid = worker.best_bid("YES")
    n_bid = worker.best_bid("NO")
    mark_value = ys * max(y_bid, 0.0) + ns * max(n_bid, 0.0)
    mark_pnl = mark_value - invested
    matched = min(ys, ns)
    locked = max(0.0, matched * (1.0 - ya - na)) if matched > _EPS else 0.0
    return EconomicPosition(
        yes_shares=ys, no_shares=ns, yes_avg=ya, no_avg=na,
        invested=invested, position_delta=ys - ns,
        pnl_if_yes=pnl_yes, pnl_if_no=pnl_no, mark_pnl=mark_pnl,
        matched_pairs=matched, locked_profit=locked,
    )


class _NoOpPreemptiveCancel:
    """Placeholder — external preemptive-cancel feeds removed."""

    async def run(self, worker: "MarketWorker") -> bool:
        worker.dashboard["preemptive_cancel_active"] = False
        return False


class ArbigabEngineRouter:
    """Safety-only router. Legacy execution engines are removed."""

    def __init__(self) -> None:
        self.cancel = _NoOpPreemptiveCancel()

    async def on_tick(self, worker: "MarketWorker") -> bool:
        worker.dashboard["engine"] = "risk"
        await self.cancel.run(worker)
        return False