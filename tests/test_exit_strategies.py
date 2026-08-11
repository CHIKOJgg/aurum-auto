import unittest

from aurum_bot.exit_strategies import (
    STRATEGIES,
    effective_target,
    executable_legs,
    get_strategy,
)


class ExitStrategyTests(unittest.TestCase):
    def test_all_strategy_keys_are_unique(self):
        self.assertEqual(len(STRATEGIES), 41)
        self.assertEqual(len({item.key for item in STRATEGIES}), 41)

    def test_market_pending_strategy_selects_target_from_execution(self):
        strategy = get_strategy("market_tp2_pending_tp4")
        common = {"strategy": strategy, "symbol": "XAUUSD", "published_at_ms": 0}
        self.assertEqual(effective_target(execution_kind="market", **common), 2)
        self.assertEqual(effective_target(execution_kind="limit", **common), 4)

    def test_partial_rounding_carries_unexecuted_fraction_forward(self):
        legs = executable_legs(
            get_strategy("tp1_tp2_tp3_tp4_equal"),
            total_volume=0.02,
            volume_min=0.01,
            volume_step=0.01,
            target_number=4,
        )
        self.assertEqual(legs, [(2, 0.01), (4, 0.01)])

    def test_unknown_strategy_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unknown exit strategy"):
            get_strategy("does_not_exist")


if __name__ == "__main__":
    unittest.main()
