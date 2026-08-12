import unittest

from aurum_bot.models import Direction, ExecutionKind
from aurum_bot.trading_math import (
    choose_execution,
    raw_volume_for_risk,
    volume_for_risk,
)


class ExecutionDecisionTests(unittest.TestCase):
    def test_strict_call_entry_never_uses_market(self):
        self.assertEqual(
            choose_execution(
                Direction.LONG,
                100,
                90,
                100,
                0.1,
                market_risk_in_range=True,
                strict_call_entry=True,
            ),
            ExecutionKind.LIMIT,
        )

    def test_long_quote_inside_risk_range_is_market(self):
        self.assertEqual(
            choose_execution(
                Direction.LONG,
                100,
                90,
                99.5,
                0.5,
                market_risk_in_range=True,
            ),
            ExecutionKind.MARKET,
        )

    def test_long_quote_outside_risk_range_is_limit(self):
        self.assertEqual(
            choose_execution(Direction.LONG, 100, 90, 102, 0.5),
            ExecutionKind.LIMIT,
        )

    def test_long_equal_price_at_1r_is_market(self):
        self.assertEqual(
            choose_execution(
                Direction.LONG,
                100,
                90,
                100,
                0.1,
                market_risk_in_range=True,
            ),
            ExecutionKind.MARKET,
        )

    def test_short_quote_inside_risk_range_is_market(self):
        self.assertEqual(
            choose_execution(
                Direction.SHORT,
                100,
                110,
                100.5,
                0.1,
                market_risk_in_range=True,
            ),
            ExecutionKind.MARKET,
        )

    def test_short_quote_outside_risk_range_is_limit(self):
        self.assertEqual(
            choose_execution(Direction.SHORT, 100, 110, 98, 0.5),
            ExecutionKind.LIMIT,
        )

    def test_short_equal_price_at_1r_is_market(self):
        self.assertEqual(
            choose_execution(
                Direction.SHORT,
                100,
                110,
                100,
                0.1,
                market_risk_in_range=True,
            ),
            ExecutionKind.MARKET,
        )

    def test_stop_crossed_uses_limit(self):
        self.assertEqual(
            choose_execution(
                Direction.LONG,
                100,
                90,
                79,
                0.5,
                market_risk_in_range=True,
            ),
            ExecutionKind.LIMIT,
        )
        self.assertEqual(
            choose_execution(
                Direction.SHORT,
                100,
                110,
                121,
                0.5,
                market_risk_in_range=True,
            ),
            ExecutionKind.LIMIT,
        )

    def test_tolerance_enters_market_on_small_adverse_drift(self):
        self.assertEqual(
            choose_execution(
                Direction.LONG,
                100,
                90,
                100.5,
                0.1,
                market_entry_tolerance_r=0.25,
            ),
            ExecutionKind.MARKET,
        )
        self.assertEqual(
            choose_execution(
                Direction.SHORT,
                100,
                110,
                99.5,
                0.1,
                market_entry_tolerance_r=0.25,
            ),
            ExecutionKind.MARKET,
        )

    def test_tolerance_does_not_fire_on_large_drift(self):
        self.assertEqual(
            choose_execution(
                Direction.LONG,
                100,
                90,
                103.0,
                0.5,
                market_entry_tolerance_r=0.25,
            ),
            ExecutionKind.LIMIT,
        )
        self.assertEqual(
            choose_execution(
                Direction.SHORT,
                100,
                110,
                97.0,
                0.5,
                market_entry_tolerance_r=0.25,
            ),
            ExecutionKind.LIMIT,
        )

    def test_tolerance_does_not_fire_far_within_stop_geometry(self):
        self.assertEqual(
            choose_execution(
                Direction.LONG,
                100,
                90,
                91,
                0.1,
                market_entry_tolerance_r=0.25,
            ),
            ExecutionKind.LIMIT,
        )

    def test_zero_tolerance_keeps_legacy_behavior(self):
        self.assertEqual(
            choose_execution(Direction.LONG, 100, 90, 100.5, 0.1),
            ExecutionKind.LIMIT,
        )
        self.assertEqual(
            choose_execution(
                Direction.LONG,
                100,
                90,
                99.5,
                0.1,
                market_risk_in_range=True,
                market_entry_tolerance_r=0.0,
            ),
            ExecutionKind.MARKET,
        )

    def test_strict_call_entry_ignores_tolerance(self):
        self.assertEqual(
            choose_execution(
                Direction.LONG,
                100,
                90,
                100.5,
                0.1,
                market_risk_in_range=True,
                strict_call_entry=True,
                market_entry_tolerance_r=0.25,
            ),
            ExecutionKind.LIMIT,
        )


class VolumeTests(unittest.TestCase):
    def test_raw_volume_is_not_rounded(self):
        self.assertAlmostEqual(raw_volume_for_risk(1_000, 1, 534), 0.01872659176)

    def test_commission_is_included_in_one_percent_risk(self):
        # $100 target / ($93 stop loss + $7 commission) = exactly 1 lot.
        self.assertEqual(raw_volume_for_risk(10_000, 1, 93, 7), 1.0)
        self.assertEqual(
            volume_for_risk(10_000, 1, 93, 0.01, 100, 0.01, 7),
            1.0,
        )

    def test_negative_commission_is_rejected(self):
        self.assertIsNone(raw_volume_for_risk(10_000, 1, 93, -1))

    def test_rounds_to_nearest_hundredth(self):
        # $100 risk / $333 per lot = 0.3003..., rounded to 0.30.
        self.assertEqual(volume_for_risk(10_000, 1, 333, 0.01, 100, 0.01), 0.3)

    def test_00187_rounds_down_to_001(self):
        self.assertEqual(volume_for_risk(1_000, 1, 534, 0.01, 100, 0.01), 0.01)

    def test_0109_rounds_down_to_010(self):
        self.assertEqual(volume_for_risk(1_000, 1, 91.743, 0.01, 100, 0.01), 0.10)

    def test_below_minimum_becomes_001_within_overshoot_limit(self):
        self.assertEqual(volume_for_risk(510, 1, 1000, 0.01, 100, 0.01), 0.01)

    def test_excessive_risk_overshoot_below_minimum_returns_none(self):
        # $100 risk_base, 1% risk = $1.00 target. Loss per lot = $5000. 0.01 lot loses $50 (50x target).
        self.assertIsNone(volume_for_risk(100, 1, 5000, 0.01, 100, 0.01))

    def test_caps_at_maximum(self):
        self.assertEqual(volume_for_risk(100_000, 1, 1, 0.01, 10, 0.01), 10.0)


if __name__ == "__main__":
    unittest.main()

