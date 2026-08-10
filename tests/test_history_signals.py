import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from aurum_bot.history_signals import (
    load_signals,
    message_has_image,
    merge_signals,
    parse_historical_signal,
    save_signals,
)
from aurum_bot.models import Direction


LONG_CALL = """#XAUUSD LONG 📈

🔸 Вход сейчас или 4004.97
🛑 SL 4002.34

🎯 TP1  4007.60
🎯 TP2  4010.24
🎯 TP3  4012.87
🎯 TP4  4015.51
"""

SHORT_CALL = """#DE40 SHORT 📉

🔸 Вход сейчас или 24931.3
🛑 SL 24968.0

🎯 TP1  24894.6
🎯 TP2  24857.9
🎯 TP3  24821.3
🎯 TP4  24784.6
"""

INDICATOR_2_CALL = """#GOLD SHORT

Вход: 4168
SL 4180

✅ TP1: 4164
✅ TP2 : 4160
✅Take profit 3: 4156
✅Take profit 4: 4130
"""


class HistoricalSignalTests(unittest.TestCase):
    def test_parses_indicator_2_text_format_without_image(self):
        signal = parse_historical_signal(
            205,
            INDICATOR_2_CALL,
            datetime(2026, 8, 5, tzinfo=timezone.utc),
            has_image=False,
        )
        self.assertIsNotNone(signal)
        self.assertEqual(signal.symbol, "XAUUSD")
        self.assertEqual(signal.take_profits, (4164.0, 4160.0, 4156.0, 4130.0))
        self.assertEqual(signal.indicator, "Индюк 2")

    def test_detects_photo_and_image_document(self):
        self.assertTrue(message_has_image(SimpleNamespace(photo=object())))
        self.assertTrue(
            message_has_image(
                SimpleNamespace(
                    photo=None,
                    document=SimpleNamespace(mime_type="image/png"),
                )
            )
        )
        self.assertFalse(
            message_has_image(SimpleNamespace(photo=None, document=None))
        )

    def test_parses_all_targets_and_second_timestamp(self):
        signal = parse_historical_signal(
            200,
            LONG_CALL,
            datetime(2026, 7, 19, 10, 11, 12, 987654, tzinfo=timezone.utc),
        )
        self.assertIsNotNone(signal)
        self.assertEqual(signal.direction, Direction.LONG)
        self.assertEqual(signal.take_profits, (4007.6, 4010.24, 4012.87, 4015.51))
        self.assertEqual(signal.timestamp_utc.microsecond, 0)
        self.assertEqual(signal.timestamp_moscow.hour, 13)

    def test_extra_targets_do_not_prevent_archiving(self):
        signal = parse_historical_signal(
            206,
            LONG_CALL + "\n🎯 TP5 4018.14\n",
            datetime(2026, 7, 19, tzinfo=timezone.utc),
        )
        self.assertIsNotNone(signal)
        self.assertEqual(signal.take_profits, (4007.6, 4010.24, 4012.87, 4015.51))

    def test_parses_short_geometry(self):
        signal = parse_historical_signal(
            201,
            SHORT_CALL,
            datetime(2026, 7, 19, tzinfo=timezone.utc),
        )
        self.assertIsNotNone(signal)
        self.assertEqual(signal.symbol, "DE40")
        self.assertEqual(signal.direction, Direction.SHORT)

    def test_parses_us100(self):
        signal = parse_historical_signal(
            204,
            LONG_CALL.replace("#XAUUSD", "#US100"),
            datetime(2026, 7, 19, tzinfo=timezone.utc),
        )
        self.assertIsNotNone(signal)
        self.assertEqual(signal.symbol, "US100")

    def test_gold_alias_is_stored_as_xauusd(self):
        signal = parse_historical_signal(
            101,
            LONG_CALL.replace("#XAUUSD", "#GOLD"),
            datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc),
        )
        self.assertIsNotNone(signal)
        self.assertEqual(signal.symbol, "XAUUSD")

    def test_germany40_alias_is_stored_as_de40(self):
        signal = parse_historical_signal(
            102,
            SHORT_CALL.replace("#DE40", "#Germany40"),
            datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc),
        )
        self.assertIsNotNone(signal)
        self.assertEqual(signal.symbol, "DE40")

    def test_fxpro_silver_alias_is_stored_as_xagusd(self):
        signal = parse_historical_signal(
            103,
            LONG_CALL.replace("#XAUUSD", "#SILVER"),
            datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc),
        )
        self.assertIsNotNone(signal)
        self.assertEqual(signal.symbol, "XAGUSD")

    def test_fxpro_usndaq100_alias_is_stored_as_us100(self):
        signal = parse_historical_signal(
            104,
            LONG_CALL.replace("#XAUUSD", "#USNDAQ100"),
            datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc),
        )
        self.assertIsNotNone(signal)
        self.assertEqual(signal.symbol, "US100")

    def test_requires_all_four_targets(self):
        self.assertIsNone(
            parse_historical_signal(
                202,
                LONG_CALL.replace("🎯 TP4  4015.51", ""),
                datetime(2026, 7, 19, tzinfo=timezone.utc),
            )
        )

    def test_json_and_csv_round_trip(self):
        signal = parse_historical_signal(
            203,
            LONG_CALL,
            datetime(2026, 7, 19, tzinfo=timezone.utc),
            has_image=True,
        )
        self.assertIsNotNone(signal)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save_signals([signal], root / "signals.json", root / "signals.csv")
            loaded = load_signals(root / "signals.json")
            self.assertEqual(loaded, [signal])
            self.assertTrue(loaded[0].has_image)
            self.assertEqual(loaded[0].indicator, "Индюк 1")
            self.assertTrue((root / "signals.csv").read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_merge_deduplicates_by_message_id_and_incoming_wins(self):
        original = parse_historical_signal(
            203, LONG_CALL, datetime(2026, 7, 19, tzinfo=timezone.utc)
        )
        edited = parse_historical_signal(
            203,
            LONG_CALL.replace("4015.51", "4016.51"),
            datetime(2026, 7, 19, tzinfo=timezone.utc),
        )
        self.assertIsNotNone(original)
        self.assertIsNotNone(edited)
        merged = merge_signals([original], [edited])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].take_profits[3], 4016.51)


if __name__ == "__main__":
    unittest.main()
