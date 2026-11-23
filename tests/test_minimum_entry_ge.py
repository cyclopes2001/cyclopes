"""Minimum entry floor (best ask >= threshold) and FOK limit selection."""

from __future__ import annotations

import unittest

from utils.clob_helpers import clamp_buy_price
from utils.directional_entry import EntryDecision, pick_entry_side
from utils.entry_limit import compute_buy_fok_limit_price, resolve_minimum_entry_price


class MinimumEntryFloorTests(unittest.TestCase):
    def test_96c_no_entry(self):
        ch = pick_entry_side(0.96, 0.05, min_entry_price=0.97)
        self.assertEqual(ch.decision, EntryDecision.SKIP)

    def test_97c_eligible(self):
        ch = pick_entry_side(0.97, 0.05, min_entry_price=0.97)
        self.assertEqual(ch.decision, EntryDecision.BUY)
        self.assertEqual(ch.side, "YES")

    def test_both_above_picks_higher_ask(self):
        ch = pick_entry_side(0.97, 0.98, min_entry_price=0.97)
        self.assertEqual(ch.side, "NO")

    def test_limit_price_matches_executable(self):
        min_res = resolve_minimum_entry_price(0.97)
        limit, reason = compute_buy_fok_limit_price(0.98, min_res.minimum_price)
        self.assertEqual(reason, "ok")
        self.assertAlmostEqual(limit, 0.98)

    def test_limit_below_floor_rejected(self):
        limit, reason = compute_buy_fok_limit_price(0.96, 0.97)
        self.assertIsNone(limit)
        self.assertEqual(reason, "below_minimum_entry")


class DashboardLimitSyncTests(unittest.TestCase):
    def test_clamp_is_idempotent_for_submission(self):
        px = 0.55
        self.assertAlmostEqual(clamp_buy_price(px), clamp_buy_price(clamp_buy_price(px)))


if __name__ == "__main__":
    unittest.main()
