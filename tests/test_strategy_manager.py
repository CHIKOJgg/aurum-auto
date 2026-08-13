import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from aurum_bot.strategy_manager import _manage_plan


class OffsetMt5:
    COPY_TICKS_ALL = 3
    POSITION_TYPE_BUY = 0

    def __init__(self, *, entry_utc_msc: int, offset_msc: int):
        self.offset_msc = offset_msc
        self.entry_utc_msc = entry_utc_msc
        self.position = SimpleNamespace(
            ticket=42,
            symbol="GOLD",
            type=self.POSITION_TYPE_BUY,
            volume=0.02,
            magic=397897,
            comment="AURUM:447",
            time_msc=entry_utc_msc + offset_msc,
            time=(entry_utc_msc + offset_msc) // 1000,
            price_open=4375.59,
        )
        self.tick = SimpleNamespace(
            bid=4376.0,
            ask=4376.2,
            time_msc=entry_utc_msc + 10_000 + offset_msc,
        )
        self.copy_range = None
        self.requests = []

    def positions_get(self, *, symbol):
        return (self.position,)

    def orders_get(self, *, symbol):
        return ()

    def symbol_info_tick(self, symbol):
        return self.tick

    def symbol_info(self, symbol):
        return SimpleNamespace(
            point=0.01,
            trade_stops_level=0,
            volume_step=0.01,
            volume_min=0.01,
        )

    def copy_ticks_range(self, symbol, start, end, flags):
        self.copy_range = (start, end)
        dtype = [("time_msc", "<i8"), ("bid", "<f8"), ("ask", "<f8")]
        # Deliberately return a pre-entry high even though it is outside the
        # requested range. The manager must reject it independently.
        return np.array(
            [
                (
                    self.entry_utc_msc - 60_000 + self.offset_msc,
                    4397.0,
                    4397.2,
                ),
                (
                    self.entry_utc_msc + self.offset_msc,
                    4375.8,
                    4376.0,
                ),
                (
                    self.entry_utc_msc + 10_000 + self.offset_msc,
                    4376.0,
                    4376.2,
                ),
            ],
            dtype=dtype,
        )

    def order_send(self, request):
        self.requests.append(request)
        raise AssertionError("pre-entry TP1 must not move the stop")


class StrategyManagerClockTests(unittest.TestCase):
    def test_utc_plus_three_history_never_uses_pre_entry_high(self):
        entry_utc_msc = 1_786_605_996_000
        offset_msc = 3 * 3_600_000
        mt5 = OffsetMt5(
            entry_utc_msc=entry_utc_msc,
            offset_msc=offset_msc,
        )
        plan = {
            "version": 1,
            "status": "active",
            "message_id": 447,
            "strategy": "tp3_be_after_tp1",
            "symbol": "GOLD",
            "signal_symbol": "XAUUSD",
            "direction": "LONG",
            "magic": 397897,
            "comment": "AURUM:447",
            "stop_loss": 4369.05,
            "take_profits": [4381.44, 4387.63, 4393.82, 4400.01],
            "final_target": 3,
            "exit_legs": [{"target": 3, "volume": 0.02, "closed": False}],
            "active_stop_target": -1,
            "touched_target": 0,
            # Reproduce a legacy plan containing raw UTC+3 position time.
            "entry_time_msc": entry_utc_msc + offset_msc,
            "entry_price": 4375.59,
            "last_check_msc": entry_utc_msc,
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "447.json"
            path.write_text(json.dumps(plan), encoding="utf-8")
            utc_now_msc = entry_utc_msc + 10_000
            with patch(
                "aurum_bot.strategy_manager.time.time_ns",
                return_value=utc_now_msc * 1_000_000,
            ):
                _manage_plan(mt5, path, plan, deviation=20)

            saved = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(saved["clock_version"], 2)
        self.assertEqual(saved["mt5_time_offset_msc"], offset_msc)
        self.assertEqual(saved["entry_time_msc"], entry_utc_msc)
        self.assertEqual(saved["touched_target"], 0)
        self.assertEqual(saved["active_stop_target"], -1)
        self.assertEqual(mt5.requests, [])
        self.assertEqual(
            int(mt5.copy_range[0].timestamp() * 1000),
            entry_utc_msc + offset_msc,
        )


if __name__ == "__main__":
    unittest.main()
