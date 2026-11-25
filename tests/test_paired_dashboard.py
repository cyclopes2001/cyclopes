"""Dashboard payload scenarios A–G (paired inventory visualization)."""

from __future__ import annotations

import unittest

from utils.paired_dashboard_payload import (
    build_dca_leg_slots,
    build_paired_dashboard,
    build_tranche_blocks,
    format_position_line,
)
from utils.paired_inventory_ledger import PairedInventoryLedger
from utils.market_time_phases import MarketPhase


def _panel(ledger: PairedInventoryLedger, **kwargs) -> dict:
    defaults = dict(
        target_combined=0.98,
        fee_rate=0.0,
        slippage_reserve=0.0,
        dca_increment=5.0,
        max_dca_legs=3,
        max_inventory_per_side=15.0,
        yes_ask=0.50,
        no_ask=0.50,
        phase=MarketPhase.EARLY,
        seconds_left=250.0,
        action=None,
        pending_order=None,
        order_pending=False,
        activity_feed=[],
        decision_feed=[],
        market_resolved=False,
        reserved_capital_usd=0.0,
        allow_new_accumulation=True,
    )
    defaults.update(kwargs)
    return build_paired_dashboard(ledger, **defaults)


class ScenarioATests(unittest.TestCase):
    def test_five_up_only(self):
        ledger = PairedInventoryLedger()
        ledger.record_buy("YES", 5, 0.34)
        dash = _panel(ledger, yes_ask=0.30, no_ask=0.70)
        self.assertEqual(dash["positions"]["up"]["line"], "5 UP @ $1.70")
        self.assertEqual(dash["positions"]["down"]["line"], "0 DOWN @ $0.00")
        legs = dash["dca"]["up_legs"]
        self.assertEqual(legs[0]["status"], "FILLED")
        self.assertEqual(legs[0]["filled_qty"], 5)
        self.assertEqual(dash["pairing"]["unmatched_up"], 5)
        self.assertLess(dash["pairing"]["pairing_percent"], 100)


class ScenarioBTests(unittest.TestCase):
    def test_fully_paired_98c(self):
        ledger = PairedInventoryLedger()
        ledger.record_buy("YES", 5, 0.34)
        ledger.record_buy("NO", 5, 0.64)
        ledger.match_fifo(max_combined=0.98)
        dash = _panel(ledger)
        self.assertEqual(dash["positions"]["up"]["line"], "5 UP @ $1.70")
        self.assertEqual(dash["positions"]["down"]["line"], "5 DOWN @ $3.20")
        self.assertEqual(dash["pairing"]["matched_shares"], 5)
        self.assertAlmostEqual(dash["economics"]["combined_pair_cost_c"], 98.0, places=1)
        self.assertAlmostEqual(dash["economics"]["gross_profit_per_pair_c"] or 0, 2.0, places=1)


class ScenarioCTests(unittest.TestCase):
    def test_partial_pairing_not_100_percent(self):
        ledger = PairedInventoryLedger()
        ledger.record_buy("YES", 5, 0.34)
        ledger.record_buy("YES", 5, 0.24, is_dca=True)
        ledger.record_buy("NO", 5, 0.64)
        ledger.match_fifo(max_combined=1.0)
        dash = _panel(ledger)
        self.assertEqual(dash["positions"]["up"]["line"], "10 UP @ $2.90")
        self.assertEqual(dash["positions"]["down"]["line"], "5 DOWN @ $3.20")
        self.assertEqual(dash["pairing"]["matched_shares"], 5)
        self.assertEqual(dash["pairing"]["unmatched_up"], 5)
        self.assertEqual(dash["pairing"]["pairing_percent"], 50.0)


class ScenarioDTests(unittest.TestCase):
    def test_partial_leg_3_of_5(self):
        ledger = PairedInventoryLedger()
        ledger.record_buy("YES", 3, 0.34)
        legs = build_dca_leg_slots(ledger, side="YES", target_qty=5, max_slots=3)
        self.assertEqual(legs[0]["status"], "PARTIALLY_FILLED")
        self.assertEqual(legs[0]["fill_ratio"], "3/5")
        dash = _panel(ledger)
        self.assertEqual(dash["positions"]["up"]["shares"], 3)


class ScenarioETests(unittest.TestCase):
    def test_pending_order_not_in_inventory(self):
        ledger = PairedInventoryLedger()
        pending = {
            "side": "NO",
            "requested_qty": 5.0,
            "filled_qty": 0.0,
            "limit_price": 0.64,
            "status": "SUBMITTED",
            "purpose": "paired_hedge",
        }
        dash = _panel(ledger, pending_order=pending, order_pending=True)
        self.assertEqual(dash["positions"]["down"]["shares"], 0)
        self.assertEqual(dash["dca"]["down_legs"][0]["status"], "PENDING")
        self.assertGreater(dash["capital"]["reserved_open_orders_usd"], 0)


class ScenarioFTests(unittest.TestCase):
    def test_three_up_legs_filled(self):
        ledger = PairedInventoryLedger()
        for px in (0.34, 0.30, 0.28):
            ledger.record_buy("YES", 5, px, is_dca=True)
        dash = _panel(ledger)
        self.assertEqual(dash["positions"]["up"]["shares"], 15)
        filled = [leg for leg in dash["dca"]["up_legs"] if leg["status"] == "FILLED"]
        self.assertEqual(len(filled), 3)


class ScenarioGTests(unittest.TestCase):
    def test_expired_unmatched_no_false_profit(self):
        ledger = PairedInventoryLedger()
        ledger.record_buy("YES", 5, 0.34)
        dash = _panel(ledger, seconds_left=5, phase=MarketPhase.LATE, market_resolved=False)
        self.assertTrue(dash["economics"]["note_unhedged_not_guaranteed"])
        self.assertEqual(dash["pairing"]["matched_shares"], 0)
        self.assertIn("RISK", dash["strategy_ui"]["state"])


class FormatTests(unittest.TestCase):
    def test_format_position_uses_total_cost(self):
        self.assertEqual(format_position_line(5, 1.70, "YES"), "5 UP @ $1.70")
        self.assertEqual(format_position_line(0, 0, "NO"), "0 DOWN @ $0.00")

    def test_tranche_blocks_unmatched_tail(self):
        ledger = PairedInventoryLedger()
        ledger.record_buy("YES", 10, 0.30)
        ledger.record_buy("NO", 5, 0.60)
        ledger.match_fifo()
        blocks = build_tranche_blocks(ledger, side="YES", slot_size=5, max_slots=3)
        self.assertEqual(blocks[0]["kind"], "PAIRED")
        self.assertEqual(blocks[1]["kind"], "UNMATCHED")


if __name__ == "__main__":
    unittest.main()