"""Tranche-aware paired inventory ledger for UP/DOWN accumulation.

Tracks per-fill lots, FIFO matching into pairs, and portfolio-level WAC
without double-counting shares across matched inventory.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


_EPS = 1e-9
_VALID = ("YES", "NO")


@dataclass
class InventoryLot:
    lot_id: str
    token_id: str
    market_id: str
    side: str
    shares: float
    execution_price: float
    total_cost: float
    fees: float
    timestamp: float
    order_id: Optional[str]
    tranche_id: str
    unmatched: float
    matched: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "lot_id": self.lot_id,
            "token_id": self.token_id,
            "market_id": self.market_id,
            "side": self.side,
            "shares": self.shares,
            "execution_price": self.execution_price,
            "total_cost": self.total_cost,
            "fees": self.fees,
            "timestamp": self.timestamp,
            "order_id": self.order_id,
            "tranche_id": self.tranche_id,
            "unmatched": self.unmatched,
            "matched": self.matched,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "InventoryLot":
        return cls(
            lot_id=str(raw["lot_id"]),
            token_id=str(raw.get("token_id") or ""),
            market_id=str(raw.get("market_id") or ""),
            side=str(raw["side"]).upper(),
            shares=float(raw["shares"]),
            execution_price=float(raw["execution_price"]),
            total_cost=float(raw["total_cost"]),
            fees=float(raw.get("fees") or 0.0),
            timestamp=float(raw.get("timestamp") or 0.0),
            order_id=raw.get("order_id"),
            tranche_id=str(raw.get("tranche_id") or raw["lot_id"]),
            unmatched=float(raw.get("unmatched", raw["shares"])),
            matched=float(raw.get("matched") or 0.0),
        )


@dataclass
class MatchedPairRecord:
    pair_id: str
    qty: float
    yes_cost: float
    no_cost: float
    fees: float
    yes_lot_ids: Tuple[str, ...]
    no_lot_ids: Tuple[str, ...]
    timestamp: float

    @property
    def combined_cost_per_pair(self) -> float:
        if self.qty <= _EPS:
            return 0.0
        return (self.yes_cost + self.no_cost + self.fees) / self.qty

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pair_id": self.pair_id,
            "qty": self.qty,
            "yes_cost": self.yes_cost,
            "no_cost": self.no_cost,
            "fees": self.fees,
            "yes_lot_ids": list(self.yes_lot_ids),
            "no_lot_ids": list(self.no_lot_ids),
            "timestamp": self.timestamp,
            "combined_cost_per_pair": self.combined_cost_per_pair,
        }


@dataclass
class LedgerMetrics:
    up_total_shares: float
    down_total_shares: float
    up_matched_shares: float
    down_matched_shares: float
    up_unmatched_shares: float
    down_unmatched_shares: float
    up_average_cost: float
    down_average_cost: float
    matched_pair_cost: Optional[float]
    realized_profit: float
    unmatched_up_cost: float
    unmatched_down_cost: float
    capital_committed: float
    dca_legs_yes: int
    dca_legs_no: int


@dataclass
class PairedInventoryLedger:
    """Persistent-friendly ledger with FIFO pair allocation."""

    market_id: str = ""
    lots: List[InventoryLot] = field(default_factory=list)
    pairs: List[MatchedPairRecord] = field(default_factory=list)
    realized_profit: float = 0.0
    settlement_realized: float = 0.0
    dca_legs_yes: int = 0
    dca_legs_no: int = 0
    processed_order_keys: set = field(default_factory=set)

    def reset(self, market_id: str = "") -> None:
        self.market_id = market_id
        self.lots.clear()
        self.pairs.clear()
        self.realized_profit = 0.0
        self.settlement_realized = 0.0
        self.dca_legs_yes = 0
        self.dca_legs_no = 0
        self.processed_order_keys.clear()

    def _side_lots(self, side: str) -> List[InventoryLot]:
        s = side.upper()
        return [lot for lot in self.lots if lot.side == s and lot.unmatched > _EPS]

    def total_shares(self, side: str) -> float:
        s = side.upper()
        return sum(lot.shares for lot in self.lots if lot.side == s)

    def unmatched_shares(self, side: str) -> float:
        return sum(lot.unmatched for lot in self._side_lots(side))

    def matched_shares(self, side: str) -> float:
        s = side.upper()
        return sum(lot.matched for lot in self.lots if lot.side == s)

    def total_cost(self, side: str) -> float:
        s = side.upper()
        return sum(lot.total_cost for lot in self.lots if lot.side == s)

    def average_cost(self, side: str) -> float:
        shares = self.total_shares(side)
        if shares <= _EPS:
            return 0.0
        return self.total_cost(side) / shares

    def unmatched_average_cost(self, side: str) -> float:
        lots = self._side_lots(side)
        qty = sum(lot.unmatched for lot in lots)
        if qty <= _EPS:
            return 0.0
        cost = 0.0
        for lot in lots:
            unit = lot.total_cost / lot.shares if lot.shares > _EPS else lot.execution_price
            cost += unit * lot.unmatched
        return cost / qty

    def max_hedge_price(self, heavy_side: str, *, target_combined: float) -> float:
        """Max opposite acquisition price given unmatched WAC on heavy side."""
        avg = self.unmatched_average_cost(heavy_side)
        if avg <= _EPS:
            return 0.0
        return max(0.0, float(target_combined) - avg)

    def record_buy(
        self,
        side: str,
        qty: float,
        price: float,
        *,
        fees: float = 0.0,
        order_id: Optional[str] = None,
        token_id: str = "",
        market_id: str = "",
        tranche_id: Optional[str] = None,
        timestamp: Optional[float] = None,
        is_dca: bool = False,
    ) -> Optional[InventoryLot]:
        s = side.upper()
        if s not in _VALID:
            raise ValueError(f"invalid side {side!r}")
        q = float(qty)
        px = float(price)
        if q <= _EPS or px <= 0:
            return None
        key = f"{order_id}:{s}:{q:.6f}:{px:.6f}" if order_id else None
        if key and key in self.processed_order_keys:
            return None
        if key:
            self.processed_order_keys.add(key)

        fee = max(0.0, float(fees))
        lot = InventoryLot(
            lot_id=str(uuid.uuid4()),
            token_id=token_id,
            market_id=market_id or self.market_id,
            side=s,
            shares=q,
            execution_price=px,
            total_cost=q * px + fee,
            fees=fee,
            timestamp=float(timestamp if timestamp is not None else time.time()),
            order_id=order_id,
            tranche_id=tranche_id or str(uuid.uuid4()),
            unmatched=q,
            matched=0.0,
        )
        self.lots.append(lot)
        if is_dca:
            if s == "YES":
                self.dca_legs_yes += 1
            else:
                self.dca_legs_no += 1
        self.match_fifo()
        return lot

    def record_sell(
        self,
        side: str,
        qty: float,
        price: float,
        *,
        fees: float = 0.0,
    ) -> float:
        """Reduce unmatched inventory FIFO; return realized PnL."""
        s = side.upper()
        remaining = float(qty)
        px = float(price)
        fee = max(0.0, float(fees))
        pnl = -fee
        for lot in self._side_lots(s):
            if remaining <= _EPS:
                break
            take = min(lot.unmatched, remaining)
            unit = lot.total_cost / lot.shares if lot.shares > _EPS else lot.execution_price
            cost = unit * take
            proceeds = px * take
            pnl += proceeds - cost
            lot.unmatched -= take
            lot.shares -= take
            lot.total_cost = max(0.0, lot.total_cost - cost)
            remaining -= take
        self.realized_profit += pnl
        self.lots = [lot for lot in self.lots if lot.shares > _EPS]
        return pnl

    def match_fifo(self, *, max_combined: Optional[float] = None) -> List[MatchedPairRecord]:
        """Match oldest unmatched YES against oldest unmatched NO.

        When ``max_combined`` is set, only form pairs whose allocated cost
        per share is <= threshold (skips incompatible lot combinations).
        """
        created: List[MatchedPairRecord] = []
        yes_lots = sorted(self._side_lots("YES"), key=lambda x: x.timestamp)
        no_lots = sorted(self._side_lots("NO"), key=lambda x: x.timestamp)
        yi = 0
        ni = 0
        while yi < len(yes_lots) and ni < len(no_lots):
            y = yes_lots[yi]
            n = no_lots[ni]
            if y.unmatched <= _EPS:
                yi += 1
                continue
            if n.unmatched <= _EPS:
                ni += 1
                continue
            qty = round(min(y.unmatched, n.unmatched), 6)
            y_unit = y.total_cost / y.shares if y.shares > _EPS else y.execution_price
            n_unit = n.total_cost / n.shares if n.shares > _EPS else n.execution_price
            pair_cost = y_unit + n_unit
            if max_combined is not None and pair_cost > float(max_combined) + _EPS:
                # Skip this NO lot for this YES lot — try next opposite lot.
                ni += 1
                continue
            y_cost = y_unit * qty
            n_cost = n_unit * qty
            fee_share = (y.fees / y.shares if y.shares > _EPS else 0.0) * qty
            fee_share += (n.fees / n.shares if n.shares > _EPS else 0.0) * qty
            y.unmatched = round(max(0.0, y.unmatched - qty), 6)
            y.matched = round(y.matched + qty, 6)
            n.unmatched = round(max(0.0, n.unmatched - qty), 6)
            n.matched = round(n.matched + qty, 6)
            rec = MatchedPairRecord(
                pair_id=str(uuid.uuid4()),
                qty=qty,
                yes_cost=y_cost,
                no_cost=n_cost,
                fees=fee_share,
                yes_lot_ids=(y.lot_id,),
                no_lot_ids=(n.lot_id,),
                timestamp=time.time(),
            )
            self.pairs.append(rec)
            created.append(rec)
            if y.unmatched <= _EPS:
                yi += 1
            if n.unmatched <= _EPS:
                ni += 1
        return created

    def portfolio_matched_pair_cost(self) -> Optional[float]:
        matched = min(self.matched_shares("YES"), self.matched_shares("NO"))
        if matched <= _EPS:
            return None
        # Prefer sum of recorded pair cash flows when available.
        if self.pairs:
            total_qty = sum(p.qty for p in self.pairs)
            if total_qty > _EPS:
                total_cost = sum(p.yes_cost + p.no_cost + p.fees for p in self.pairs)
                return total_cost / total_qty
        return self.average_cost("YES") + self.average_cost("NO")

    def projected_pair_cost_after_hedge(
        self,
        hedge_side: str,
        qty: float,
        price: float,
        *,
        fees: float = 0.0,
    ) -> Optional[float]:
        """Portfolio WAC pair cost if ``qty`` is bought on the hedge side."""
        heavy = "NO" if hedge_side.upper() == "YES" else "YES"
        unmatched = self.unmatched_shares(heavy)
        if unmatched <= _EPS or qty <= _EPS:
            return None
        match_qty = min(unmatched, float(qty))
        heavy_avg = self.unmatched_average_cost(heavy)
        hedge_unit = float(price) + (float(fees) / float(qty) if qty > _EPS else 0.0)
        return heavy_avg + hedge_unit * (match_qty / match_qty)

    def metrics(self) -> LedgerMetrics:
        up = self.total_shares("YES")
        down = self.total_shares("NO")
        up_m = self.matched_shares("YES")
        down_m = self.matched_shares("NO")
        return LedgerMetrics(
            up_total_shares=up,
            down_total_shares=down,
            up_matched_shares=up_m,
            down_matched_shares=down_m,
            up_unmatched_shares=self.unmatched_shares("YES"),
            down_unmatched_shares=self.unmatched_shares("NO"),
            up_average_cost=self.average_cost("YES"),
            down_average_cost=self.average_cost("NO"),
            matched_pair_cost=self.portfolio_matched_pair_cost(),
            realized_profit=self.realized_profit + self.settlement_realized,
            unmatched_up_cost=self.unmatched_average_cost("YES") * self.unmatched_shares("YES"),
            unmatched_down_cost=self.unmatched_average_cost("NO") * self.unmatched_shares("NO"),
            capital_committed=sum(lot.total_cost for lot in self.lots),
            dca_legs_yes=self.dca_legs_yes,
            dca_legs_no=self.dca_legs_no,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "market_id": self.market_id,
            "lots": [lot.to_dict() for lot in self.lots],
            "pairs": [p.to_dict() for p in self.pairs],
            "realized_profit": self.realized_profit,
            "settlement_realized": self.settlement_realized,
            "dca_legs_yes": self.dca_legs_yes,
            "dca_legs_no": self.dca_legs_no,
            "processed_order_keys": list(self.processed_order_keys),
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "PairedInventoryLedger":
        ledger = cls(market_id=str(raw.get("market_id") or ""))
        ledger.lots = [InventoryLot.from_dict(x) for x in raw.get("lots") or []]
        for p in raw.get("pairs") or []:
            ledger.pairs.append(
                MatchedPairRecord(
                    pair_id=str(p["pair_id"]),
                    qty=float(p["qty"]),
                    yes_cost=float(p["yes_cost"]),
                    no_cost=float(p["no_cost"]),
                    fees=float(p.get("fees") or 0.0),
                    yes_lot_ids=tuple(p.get("yes_lot_ids") or ()),
                    no_lot_ids=tuple(p.get("no_lot_ids") or ()),
                    timestamp=float(p.get("timestamp") or 0.0),
                )
            )
        ledger.realized_profit = float(raw.get("realized_profit") or 0.0)
        ledger.settlement_realized = float(raw.get("settlement_realized") or 0.0)
        ledger.dca_legs_yes = int(raw.get("dca_legs_yes") or 0)
        ledger.dca_legs_no = int(raw.get("dca_legs_no") or 0)
        ledger.processed_order_keys = set(raw.get("processed_order_keys") or [])
        return ledger


def new_average_cost(existing_shares: float, existing_cost: float, new_shares: float, new_price: float) -> float:
    """NEW_AVERAGE_COST = (EXISTING_TOTAL_COST + NEW_PURCHASE_COST) / (EXISTING + NEW)."""
    total_shares = float(existing_shares) + float(new_shares)
    if total_shares <= _EPS:
        return 0.0
    return (float(existing_cost) + float(new_shares) * float(new_price)) / total_shares
