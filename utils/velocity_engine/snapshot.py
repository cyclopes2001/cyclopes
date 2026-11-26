"""Build normalized order-book snapshots from Polymarket CLOB books."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any, List, Optional, Sequence, Tuple

from utils.order_book_math import (
    _levels,
    best_ask_from_levels,
    best_ask_size_from_levels,
    best_bid_from_levels,
)


def weighted_depth(levels: Sequence[Tuple[float, float]], weights: Sequence[float]) -> float:
    total = 0.0
    for i, (_, size) in enumerate(levels):
        w = weights[i] if i < len(weights) else weights[-1] if weights else 1.0
        total += w * float(size)
    return total


def weighted_obi(bid_depth: float, ask_depth: float) -> float:
    denom = bid_depth + ask_depth
    if denom <= 1e-12:
        return 0.0
    return max(-1.0, min(1.0, (bid_depth - ask_depth) / denom))


@dataclass(frozen=True)
class BookSnapshot:
    side: str
    ts_mono: float
    ts_wall: float
    best_bid: float
    best_ask: float
    best_bid_size: float
    best_ask_size: float
    mid: float
    spread: float
    bid_levels: Tuple[Tuple[float, float], ...]
    ask_levels: Tuple[Tuple[float, float], ...]
    bid_depth_5: float
    ask_depth_5: float
    obi: float
    crossed: bool
    sequence: Optional[str] = None

    @property
    def invalid(self) -> bool:
        return self.crossed or self.best_ask <= 0 or self.best_bid < 0


def _top_n(levels: Sequence[Tuple[float, float]], n: int, *, reverse: bool) -> Tuple[Tuple[float, float], ...]:
    ordered = sorted(levels, key=lambda x: x[0], reverse=reverse)
    return tuple(ordered[:n])


def snapshot_from_book(
    side: str,
    book: Any,
    *,
    level_weights: Sequence[float],
    ts_mono: Optional[float] = None,
) -> Optional[BookSnapshot]:
    if book is None:
        return None
    asks_raw = book.get("asks") if isinstance(book, dict) else getattr(book, "asks", None)
    bids_raw = book.get("bids") if isinstance(book, dict) else getattr(book, "bids", None)
    bid_lv = _levels(bids_raw)
    ask_lv = _levels(asks_raw)
    if not bid_lv and not ask_lv:
        return None
    best_bid = best_bid_from_levels(bids_raw)
    best_ask = best_ask_from_levels(asks_raw)
    if best_ask <= 0:
        return None
    best_bid_size = bid_lv[0][1] if bid_lv else 0.0
    if bid_lv:
        best_bid_size = sorted(bid_lv, key=lambda x: x[0], reverse=True)[0][1]
    best_ask_size = best_ask_size_from_levels(asks_raw)
    mid = (best_bid + best_ask) / 2.0 if best_bid > 0 else best_ask
    spread = max(0.0, best_ask - best_bid) if best_bid > 0 else best_ask
    crossed = best_bid > best_ask + 1e-9 if best_bid > 0 else False
    bids5 = _top_n(bid_lv, 5, reverse=True)
    asks5 = _top_n(ask_lv, 5, reverse=False)
    w_bid = weighted_depth(bids5, level_weights)
    w_ask = weighted_depth(asks5, level_weights)
    obi = weighted_obi(w_bid, w_ask)
    seq = None
    if isinstance(book, dict):
        seq = str(book.get("hash") or book.get("sequence") or "") or None
    now_mono = float(ts_mono if ts_mono is not None else time.monotonic())
    return BookSnapshot(
        side=side.upper(),
        ts_mono=now_mono,
        ts_wall=time.time(),
        best_bid=best_bid,
        best_ask=best_ask,
        best_bid_size=best_bid_size,
        best_ask_size=best_ask_size,
        mid=mid,
        spread=spread,
        bid_levels=bids5,
        ask_levels=asks5,
        bid_depth_5=sum(s for _, s in bids5),
        ask_depth_5=sum(s for _, s in asks5),
        obi=obi,
        crossed=crossed,
        sequence=seq,
    )


def ofi_delta(prev: BookSnapshot, cur: BookSnapshot, *, tick: float) -> float:
    """Cont-style BBO OFI increment (unknown aggressor direction not inferred)."""
    if prev.invalid or cur.invalid:
        return 0.0
    e = 0.0
    # bid side contribution
    if cur.best_bid > prev.best_bid + tick - 1e-12:
        e += cur.best_bid_size
    elif cur.best_bid < prev.best_bid - tick + 1e-12:
        e -= prev.best_bid_size
    else:
        e += cur.best_bid_size - prev.best_bid_size
    # ask side contribution (sign flipped for buy pressure)
    if cur.best_ask < prev.best_ask - tick + 1e-12:
        e += prev.best_ask_size
    elif cur.best_ask > prev.best_ask + tick - 1e-12:
        e -= cur.best_ask_size
    else:
        e -= cur.best_ask_size - prev.best_ask_size
    if not math.isfinite(e):
        return 0.0
    return e