"""Entry side selection — buy the leading side whose ask is above the floor."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional

from utils.clob_helpers import clamp_buy_price

_MAX_BUY = 0.99


class EntryDecision(Enum):
    BUY = auto()
    SKIP = auto()


@dataclass(frozen=True)
class EntrySideChoice:
    decision: EntryDecision
    side: Optional[str]
    best_ask: float
    reason: str


def best_ask_above_floor(best_ask: float, min_entry_price: float) -> bool:
    ask = float(best_ask)
    if ask <= 0 or ask > _MAX_BUY + 1e-9:
        return False
    floor = clamp_buy_price(float(min_entry_price))
    ask = clamp_buy_price(ask)
    return ask + 1e-9 >= floor and ask <= _MAX_BUY + 1e-9


def pick_entry_side(
    yes_ask: float,
    no_ask: float,
    *,
    min_entry_price: float,
    max_entry_price: float | None = None,
) -> EntrySideChoice:
    """Pick at most one side whose ask is at or above ``min_entry_price`` (e.g. 97¢)."""
    del max_entry_price
    floor = clamp_buy_price(float(min_entry_price))
    candidates: list[tuple[str, float]] = []
    for side, raw in (("YES", yes_ask), ("NO", no_ask)):
        if not raw or float(raw) <= 0:
            continue
        raw_f = float(raw)
        if raw_f > _MAX_BUY + 1e-9:
            continue
        ask = clamp_buy_price(raw_f)
        if ask + 1e-9 >= floor and ask <= _MAX_BUY + 1e-9:
            candidates.append((side, ask))
    if not candidates:
        return EntrySideChoice(EntryDecision.SKIP, None, 0.0, "ask_below_floor")
    # Higher ask first (leading side); YES wins ties.
    candidates.sort(key=lambda row: (-row[1], 0 if row[0] == "YES" else 1))
    side, px = candidates[0]
    return EntrySideChoice(EntryDecision.BUY, side, px, "eligible")
