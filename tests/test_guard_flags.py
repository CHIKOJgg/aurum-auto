import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from aurum_bot.config import load_config
from aurum_bot.models import Direction
from aurum_bot.parser import parse_signal
from aurum_bot.state import StateStore

BTCUSD_CALL = """#BTCUSD LONG 📈

🔸 Вход сейчас или 100000.0
🛑 SL 99000.0

🎯 TP1  101000.0
🎯 TP2  102000.0
🎯 TP3  103000.0
🎯 TP4  104000.0
"""

class ConfigGuardFlagsTests(unittest.TestCase):
    def _load(self, mutate=None):
        source = Path(__file__).parents[1] / "config.example.yaml"
        data = yaml.safe_load(source.read_text(encoding="utf-8"))
        if mutate:
            mutate(data)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(yaml.safe_dump(data), encoding="utf-8")
            with patch.dict(
                os.environ,
                {
                    "TELEGRAM_API_ID": "1",
                    "TELEGRAM_API_HASH": "hash",
                    "TELEGRAM_PHONE": "+10000000000",
                },
                clear=False,
            ):
                return load_config(path)

    def test_guard_flags_default_to_true(self):
        config = self._load()
        self.assertTrue(config.trading.trading_enabled)
        self.assertTrue(config.trading.market_entry_tolerance_enabled)
        self.assertTrue(config.trading.pending_timeout_enabled)
        self.assertTrue(config.trading.entry_spread_guard_enabled)
        self.assertTrue(config.trading.news_guard_enabled)
        self.assertTrue(config.trading.margin_guard_enabled)
        self.assertTrue(config.trading.exit_spread_guard_enabled)

    def test_guard_flag_can_be_disabled(self):
        config = self._load(lambda data: data["trading"].update({"trading_enabled": False}))
        self.assertFalse(config.trading.trading_enabled)

    def test_guard_flag_quoted_string_raises(self):
        with self.assertRaises(ValueError):
            self._load(lambda data: data["trading"].update({"trading_enabled": "true"}))

    def test_allowed_symbols_from_yaml(self):
        config = self._load()
        self.assertEqual(config.trading.allowed_symbols, frozenset({"XAUUSD", "XAGUSD", "DE40", "US100"}))

    def test_symbol_aliases_from_yaml(self):
        config = self._load()
        self.assertEqual(config.trading.symbol_aliases, {
            "GOLD": "XAUUSD",
            "SILVER": "XAGUSD",
            "GERMANY40": "DE40",
            "USNDAQ100": "US100"
        })

    def test_server_time_mode_defaults_to_auto(self):
        config = self._load()
        self.assertEqual(config.trading.server_time_mode, "auto")

    def test_missing_guard_flags_default_to_true(self):
        def remove_flags(data):
            flags = [
                "trading_enabled",
                "market_entry_tolerance_enabled",
                "pending_timeout_enabled",
                "entry_spread_guard_enabled",
                "news_guard_enabled",
                "margin_guard_enabled",
                "exit_spread_guard_enabled"
            ]
            for flag in flags:
                data["trading"].pop(flag, None)

        config = self._load(remove_flags)
        self.assertTrue(config.trading.trading_enabled)
        self.assertTrue(config.trading.market_entry_tolerance_enabled)
        self.assertTrue(config.trading.pending_timeout_enabled)
        self.assertTrue(config.trading.entry_spread_guard_enabled)
        self.assertTrue(config.trading.news_guard_enabled)
        self.assertTrue(config.trading.margin_guard_enabled)
        self.assertTrue(config.trading.exit_spread_guard_enabled)


class StateCorruptionTests(unittest.TestCase):
    def test_corrupted_state_file_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "state.json"
            state_file.write_text("{invalid json", encoding="utf-8")
            
            store = StateStore(state_file, channel_id=12345)
            store.load()
            
            # Corrupted file should result in clean state with empty messages
            self.assertEqual(store.data.get("messages"), {})
            # Check if corrupted backup exists
            backups = list(Path(directory).glob("state.json.corrupted.*"))
            self.assertEqual(len(backups), 1)

    def test_valid_state_loads_normally(self):
        with tempfile.TemporaryDirectory() as directory:
            valid_data = {
                "version": 1,
                "channel_id": 12345,
                "last_seen_message_id": 0,
                "messages": {"42": {"status": "claimed"}},
            }
            state_file = Path(directory) / "state.json"
            state_file.write_text(json.dumps(valid_data), encoding="utf-8")
            
            store = StateStore(state_file, channel_id=12345)
            store.load()
            
            self.assertEqual(store.data["messages"]["42"]["status"], "claimed")


class ParserConfigurableTests(unittest.TestCase):
    def test_custom_allowed_symbols(self):
        signal = parse_signal(
            1, 
            BTCUSD_CALL, 
            allowed_symbols=frozenset({"BTCUSD"})
        )
        self.assertIsNotNone(signal)
        self.assertEqual(signal.symbol, "BTCUSD")

    def test_custom_symbol_alias(self):
        text = BTCUSD_CALL.replace("#BTCUSD", "#BITCOIN")
        signal = parse_signal(
            2, 
            text, 
            symbol_aliases={"BITCOIN": "BTCUSD"}, 
            allowed_symbols=frozenset({"BTCUSD"})
        )
        self.assertIsNotNone(signal)
        self.assertEqual(signal.symbol, "BTCUSD")

    def test_default_symbols_still_work(self):
        text = """#XAUUSD LONG 📈

🔸 Вход сейчас или 100000.0
🛑 SL 99000.0

🎯 TP1  101000.0
🎯 TP2  102000.0
🎯 TP3  103000.0
🎯 TP4  104000.0
"""
        signal = parse_signal(3, text)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.symbol, "XAUUSD")


if __name__ == "__main__":
    unittest.main()
