import unittest

from aurum_bot.config import TradingConfig
from aurum_bot.main import _any_active_plans, _indicator_execution_enabled, _notification_text
from aurum_bot.models import Direction, ExecutionResult, Signal
from pathlib import Path
import json
import tempfile


class IndicatorExecutionTests(unittest.TestCase):
    def test_indicator_1_is_always_enabled(self):
        self.assertTrue(_indicator_execution_enabled(True, False))

    def test_indicator_2_follows_yaml_switch(self):
        self.assertFalse(_indicator_execution_enabled(False, False))
        self.assertTrue(_indicator_execution_enabled(False, True))

    def test_strict_call_entry_default_is_enabled(self):
        trading = TradingConfig(1, 0.9, 1.1, 0.01, 20, 3, 2, 397897)
        self.assertTrue(trading.strict_call_entry)
        self.assertFalse(trading.enable_indicator_2)

    def test_notification_text_uses_execution_and_skip_icons(self):
        signal = Signal(417, "XAUUSD", Direction.SHORT, 1, 2, 0)
        self.assertIn("♻️ 417 XAUUSD SHORT executed lot=0.04", _notification_text(417, signal, ExecutionResult("a", "executed", "ok", volume=0.04)))
        self.assertIn("⛔ 417 skipped_news", _notification_text(417, signal, ExecutionResult("a", "skipped_news", "CPI")))

    def test_only_active_strategy_plans_wake_manager(self):
        account = type("A", (), {"enabled": True, "name": "a"})()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "a"
            target.mkdir()
            (target / "finished.json").write_text(json.dumps({"status": "completed"}), encoding="utf-8")
            self.assertFalse(_any_active_plans((account,), root))
            (target / "active.json").write_text(json.dumps({"status": "active"}), encoding="utf-8")
            self.assertTrue(_any_active_plans((account,), root))


if __name__ == "__main__":
    unittest.main()
