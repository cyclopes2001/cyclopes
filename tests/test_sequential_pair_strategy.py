"""Paired inventory strategy export and wiring tests."""

import unittest
from pathlib import Path


class PairedStrategyWiringTests(unittest.TestCase):
    def test_inventory_strategy_is_paired(self):
        src = Path("strategies/inventory_accumulation_strategy.py").read_text(encoding="utf-8")
        self.assertIn("class InventoryAccumulationStrategy", src)
        self.assertIn("paired_inventory_accumulation", src)
        self.assertIn("decide(", src)

    def test_momentum_exports_paired_as_active(self):
        from strategies.momentum import InventoryAccumulationStrategy, MomentumStrategy
        from strategies.inventory_accumulation_strategy import InventoryAccumulationStrategy as IAS

        self.assertIs(MomentumStrategy, IAS)
        self.assertIs(InventoryAccumulationStrategy, IAS)

    def test_directional_preserved(self):
        from strategies.directional_fok_strategy import DirectionalFokStrategy

        self.assertTrue(hasattr(DirectionalFokStrategy, "on_market_tick"))


if __name__ == "__main__":
    unittest.main()
