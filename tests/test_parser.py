import unittest

from aurum_bot.models import Direction
from aurum_bot.parser import parse_signal


SHORT_CALL = """#XAUUSD SHORT 📉

🔸 Вход сейчас или 4093.58
🛑 SL 4097.81

🎯 TP1  4089.35
🎯 TP2  4085.12
🎯 TP3  4080.89
🎯 TP4  4076.66
"""

LONG_CALL = """#DE40 LONG 📈

🔸 Вход сейчас или 25000.4
🛑 SL 24975.9

🎯 TP1  25024.9
🎯 TP2  25049.4
🎯 TP3  25073.9
🎯 TP4  25098.4
"""


GBPUSD_CALL = """#GBPUSD LONG 📈

🔸 Вход сейчас или 1.33013
🛑 SL 1.32973

🎯 TP1  1.33053
🎯 TP2  1.33094
🎯 TP3  1.33134
🎯 TP4  1.33175
"""

US100_CALL = """#US100 LONG 📈

🔸 Вход сейчас или 27808.7
🛑 SL 27784.1

🎯 TP1  27833.3
🎯 TP2  27857.9
🎯 TP3  27882.5
🎯 TP4  27907.0
"""

XAGUSD_CALL = """#XAGUSD SHORT 📉

🔸 Вход сейчас или 57.948
🛑 SL 58.008

🎯 TP1  57.887
🎯 TP2  57.827
🎯 TP3  57.766
🎯 TP4  57.706
"""

GOLD_CALL = """#GOLD SHORT 📉

🔸 Вход сейчас или 4061.22
🛑 SL 4063.39

🎯 TP1  4059.05
🎯 TP2  4056.89
🎯 TP3  4054.72
🎯 TP4  4052.55
"""

GERMANY40_CALL = """#Germany40 SHORT 📉

🔸 Вход сейчас или 25925.7
🛑 SL 25966.4

🎯 TP1  25885.0
🎯 TP2  25844.3
🎯 TP3  25803.6
🎯 TP4  25762.9
"""

INDICATOR_2_COLON_CALL = """#GOLD SHORT

Вход: 4168
SL 4180

✅ TP1: 4164
✅ TP2 : 4160
✅Take profit 3: 4156
✅Take profit 4: 4130
"""

INDICATOR_2_PLAIN_CALL = """#GOLD SHORT

Вход 4164.97
❗️SL 4180

🎯 TP1  4156
🎯 TP2  4151
🎯 TP3 4147
🎯 TP4  4110
"""


class ParserTests(unittest.TestCase):
    def test_indicator_2_colon_and_take_profit_format(self):
        signal = parse_signal(117, INDICATOR_2_COLON_CALL)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.symbol, "XAUUSD")
        self.assertEqual(signal.entry, 4168.0)
        self.assertEqual(signal.stop_loss, 4180.0)
        self.assertEqual(signal.take_profits, (4164.0, 4160.0, 4156.0, 4130.0))

    def test_indicator_2_plain_entry_and_exclamation_stop(self):
        signal = parse_signal(118, INDICATOR_2_PLAIN_CALL)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.symbol, "XAUUSD")
        self.assertEqual(signal.entry, 4164.97)
        self.assertEqual(signal.stop_loss, 4180.0)
        self.assertEqual(signal.take_profits, (4156.0, 4151.0, 4147.0, 4110.0))

    def test_real_gold_call_with_all_targets_and_links(self):
        text = """#GOLD SHORT 📉

🔸 Вход сейчас или 4056.60
🛑 SL 4064.73

🎯 TP1  4048.46
🎯 TP2  4040.32
🎯 TP3  4032.18
🎯 TP4  4024.04

[пообщаться насчет сделки](https://t.me/example)
[получать автоматический расчет позиции](https://t.me/c/3978977082/134)
"""
        signal = parse_signal(999, text, take_profit_target=2)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.symbol, "XAUUSD")
        self.assertEqual(signal.direction, Direction.SHORT)
        self.assertEqual(signal.take_profits, (4048.46, 4040.32, 4032.18, 4024.04))

    def test_take_profit_target_is_selected_from_config(self):
        text = """#US100 LONG 📈

🔸 Вход сейчас или 27791.7
🛑 SL 27730.4

🎯 TP1  27853.0
🎯 TP2  27914.3
🎯 TP3  27975.6
🎯 TP4  28036.9

[пообщаться насчет сделки](https://t.me/example)
"""
        expected = {1: 27853.0, 2: 27914.3, 3: 27975.6, 4: 28036.9}
        for target, price in expected.items():
            with self.subTest(target=target):
                signal = parse_signal(115, text, take_profit_target=target)
                self.assertIsNotNone(signal)
                self.assertEqual(signal.take_profit, price)

    def test_three_targets_use_the_last_target(self):
        text = """#GOLD LONG

🔸 Вход сейчас или 4286.75
🛑 SL 4282.88

🎯 TP1 4290.62
🎯 TP2 4298.36
🎯 TP3 4306.10
"""
        signal = parse_signal(119, text)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.take_profits, (4290.62, 4298.36, 4306.10))
        self.assertEqual(signal.take_profit, 4306.10)

    def test_one_take_profit_is_accepted(self):
        text = """#GOLD LONG

Вход 4341
SL 4320

✅Take profit 1: 4344
"""
        signal = parse_signal(122, text)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.take_profits, (4344.0,))
        self.assertEqual(signal.take_profit, 4344.0)

    def test_two_mixed_take_profit_names_are_accepted(self):
        text = """#GOLD LONG

Вход 4341
SL 4320

✅ TP1: 4344
✅Take profit 2: 4349
"""
        signal = parse_signal(123, text)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.take_profits, (4344.0, 4349.0))
        self.assertEqual(signal.take_profit, 4349.0)

    def test_hundreds_of_mixed_targets_are_retained(self):
        targets = "\n".join(
            f"{'TP' if number % 2 else 'Take profit '} {number}: {4341 + number}"
            for number in range(1, 201)
        )
        text = f"""#GOLD LONG

Вход 4341
SL 4320

{targets}
"""
        signal = parse_signal(124, text, take_profit_target=4)
        self.assertIsNotNone(signal)
        self.assertEqual(len(signal.take_profits), 200)
        self.assertEqual(signal.take_profits[0], 4342.0)
        self.assertEqual(signal.take_profits[-1], 4541.0)
        self.assertEqual(signal.take_profit, 4345.0)

    def test_any_number_of_targets_keeps_tp2_tp4_strategy_levels(self):
        text = """#GOLD LONG 📈

🔸 Повторный вход сейчас или 4320.37
🛑 SL 4316.32

🎯 TP1  4324.43
🎯 TP2  4328.48
🎯 TP3  4332.53
🎯 TP4  4336.58
🎯 TP5  4340.64
"""
        signal = parse_signal(120, text, take_profit_target=4)
        self.assertIsNotNone(signal)
        self.assertEqual(
            signal.take_profits,
            (4324.43, 4328.48, 4332.53, 4336.58, 4340.64),
        )
        self.assertEqual(signal.take_profit, 4336.58)

    def test_entry_now_without_repeat_or_ili_is_parsed(self):
        text = """#GOLD LONG

🔸 Вход сейчас 4320.37
🛑 SL 4316.32

🎯 TP1 4324.43
🎯 TP2 4328.48
🎯 TP3 4332.53
🎯 TP4 4336.58
"""
        signal = parse_signal(121, text)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.entry, 4320.37)

    def test_invalid_take_profit_target_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "from 1 to 4"):
            parse_signal(116, LONG_CALL, take_profit_target=5)

    def test_gold_call_with_links_uses_tp2(self):
        text = """#GOLD SHORT 📉

🔸 Вход сейчас или 4056.60
🛑 SL 4064.73

🎯 TP1  4048.46
🎯 TP2  4040.32
🎯 TP3  4032.18
🎯 TP4  4024.04

[пообщаться насчет сделки](https://t.me/example)
"""
        signal = parse_signal(114, text)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.symbol, "XAUUSD")
        self.assertEqual(signal.take_profit, 4040.32)

    def test_short_photo_caption_text(self):
        signal = parse_signal(101, SHORT_CALL)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.symbol, "XAUUSD")
        self.assertEqual(signal.direction, Direction.SHORT)
        self.assertEqual(signal.entry, 4093.58)
        self.assertEqual(signal.stop_loss, 4097.81)
        self.assertEqual(signal.take_profit, 4085.12)

    def test_long(self):
        signal = parse_signal(102, LONG_CALL)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.symbol, "DE40")
        self.assertEqual(signal.direction, Direction.LONG)

    def test_take_status_is_ignored(self):
        text = """✅ XAUUSD TP 1 ВЗЯТ 📉
📈 Прибыль по сделке:
+1.00% (1.00% риск)
"""
        self.assertIsNone(parse_signal(103, text))

    def test_stop_status_is_ignored(self):
        self.assertIsNone(
            parse_signal(104, "❌ XAUUSD Сработал стоп-лосс\n📉 -1.00% закрыто")
        )

    def test_forex_pairs_are_parsed(self):
        for symbol in ("GBPUSD", "GBPJPY", "USDJPY"):
            with self.subTest(symbol=symbol):
                signal = parse_signal(105, GBPUSD_CALL.replace("GBPUSD", symbol))
                self.assertIsNotNone(signal)
                self.assertEqual(signal.symbol, symbol)
                self.assertEqual(signal.entry, 1.33013)
                self.assertEqual(signal.stop_loss, 1.32973)
                self.assertEqual(signal.take_profit, 1.33094)

    def test_non_currency_symbol_is_ignored(self):
        self.assertIsNone(parse_signal(107, SHORT_CALL.replace("#XAUUSD", "#OIL")))

    def test_us100_is_parsed(self):
        signal = parse_signal(108, US100_CALL)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.symbol, "US100")
        self.assertEqual(signal.entry, 27808.7)
        self.assertEqual(signal.stop_loss, 27784.1)
        self.assertEqual(signal.take_profit, 27857.9)

    def test_xagusd_is_parsed(self):
        signal = parse_signal(109, XAGUSD_CALL)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.symbol, "XAGUSD")
        self.assertEqual(signal.direction, Direction.SHORT)
        self.assertEqual(signal.entry, 57.948)
        self.assertEqual(signal.stop_loss, 58.008)
        self.assertEqual(signal.take_profit, 57.827)

    def test_gold_alias_is_parsed_as_xauusd(self):
        signal = parse_signal(110, GOLD_CALL)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.symbol, "XAUUSD")
        self.assertEqual(signal.direction, Direction.SHORT)
        self.assertEqual(signal.entry, 4061.22)
        self.assertEqual(signal.stop_loss, 4063.39)
        self.assertEqual(signal.take_profit, 4056.89)

    def test_germany40_alias_is_parsed_as_de40(self):
        signal = parse_signal(111, GERMANY40_CALL)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.symbol, "DE40")
        self.assertEqual(signal.direction, Direction.SHORT)
        self.assertEqual(signal.entry, 25925.7)
        self.assertEqual(signal.stop_loss, 25966.4)
        self.assertEqual(signal.take_profit, 25844.3)

    def test_fxpro_silver_alias_is_parsed_as_xagusd(self):
        signal = parse_signal(112, XAGUSD_CALL.replace("#XAGUSD", "#SILVER"))
        self.assertIsNotNone(signal)
        self.assertEqual(signal.symbol, "XAGUSD")

    def test_fxpro_usndaq100_alias_is_parsed_as_us100(self):
        signal = parse_signal(113, US100_CALL.replace("#US100", "#USNDAQ100"))
        self.assertIsNotNone(signal)
        self.assertEqual(signal.symbol, "US100")

    def test_invalid_geometry_is_ignored(self):
        text = LONG_CALL.replace("SL 24975.9", "SL 25010.0")
        self.assertIsNone(parse_signal(106, text))


if __name__ == "__main__":
    unittest.main()
