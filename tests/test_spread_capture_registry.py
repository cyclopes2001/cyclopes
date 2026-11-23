"""Spread registry / pair-cost sanity tests."""

import unittest

from utils.pair_cost import combined_pair_cost


class PairCostTests(unittest.TestCase):
    def test_profitable_pair(self):
        pc = combined_pair_cost(0.52, 0.45, fee_rate=0.0, slippage=0.0)
        self.assertAlmostEqual(pc, 0.97)


if __name__ == "__main__":
    unittest.main()