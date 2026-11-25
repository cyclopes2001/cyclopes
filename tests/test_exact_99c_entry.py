import unittest
from pathlib import Path


class PairedEntryConfigTests(unittest.TestCase):
    def test_paired_entry_threshold_configurable(self):
        from config import load_worker_configs

        wc = load_worker_configs()[0]
        self.assertEqual(wc.strategy_mode, "paired_inventory_accumulation")
        self.assertAlmostEqual(wc.paired_initial_entry_threshold, 0.64)
        self.assertAlmostEqual(wc.target_combined_pair_cost, 0.98)

    def test_polymarket_tick_drives_strategy(self):
        source = Path("bot.py").read_text(encoding="utf-8")
        self.assertIn("await self.strategy.on_market_tick(self)", source)


if __name__ == "__main__":
    unittest.main()
