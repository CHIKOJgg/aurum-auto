import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from aurum_bot.strategy_manager import manage


class FakeMt5:
    TRADE_RETCODE_DONE = 10009
    TRADE_RETCODE_PLACED = 10008
    TRADE_RETCODE_DONE_PARTIAL = 10010
    TRADE_RETCODE_ORDER_REMOVED = 4108
    TRADE_ACTION_REMOVE = 8

    def __init__(self, orders=()):
        self.orders = list(orders)
        self.requests = []

    def initialize(self, *args, **kwargs):
        return True

    def shutdown(self):
        pass

    def last_error(self):
        return (0, "ok")

    def positions_get(self, *, symbol):
        return ()

    def orders_get(self, *, symbol):
        return tuple(self.orders)

    def order_send(self, request):
        self.requests.append(dict(request))
        if request.get("action") == self.TRADE_ACTION_REMOVE:
            self.orders = [
                order for order in self.orders if order.ticket != request["order"]
            ]
        return SimpleNamespace(retcode=self.TRADE_RETCODE_DONE, comment="done")


def _now_msc() -> int:
    return time.time_ns() // 1_000_000


class PendingTimeoutTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self._state_dir = Path(self._dir.name)
        self._account_dir = self._state_dir / "fxpro_demo"
        self._account_dir.mkdir()

    def tearDown(self):
        self._dir.cleanup()

    def _payload(self, pending_timeout_minutes=15.0):
        return {
            "account": {
                "name": "fxpro_demo",
                "enabled": True,
                "risk_base_usd": 500,
                "terminal_path": "C:/unused/terminal64.exe",
                "symbols": {"XAUUSD": "GOLD", "DE40": "#Germany40"},
                "commission_per_lot_usd": {},
                "commission_rate_percent": {},
            },
            "deviation_points": 20,
            "strategy_state_dir": str(self._state_dir),
            "pending_timeout_minutes": pending_timeout_minutes,
        }

    def _write_plan(self, message_id=900):
        plan = {
            "status": "active",
            "comment": f"AURUM:{message_id}",
            "magic": 397897,
            "symbol": "GOLD",
            "strategy": "sl_tp4",
            "message_id": message_id,
        }
        path = self._account_dir / f"{message_id}.json"
        path.write_text(json.dumps(plan), encoding="utf-8")
        return path

    def _order(self, ticket=7, message_id=900, age_minutes=20):
        return SimpleNamespace(
            ticket=ticket,
            magic=397897,
            comment=f"AURUM:{message_id}",
            time_setup_msc=_now_msc() - int(age_minutes * 60_000),
            time_setup=0,
        )

    def test_stale_pending_order_is_cancelled_and_plan_completed(self):
        fake = FakeMt5(orders=[self._order()])
        path = self._write_plan()
        with unittest.mock.patch.dict(sys.modules, {"MetaTrader5": fake}):
            result = manage(self._payload())

        self.assertEqual(result["managed"], 1)
        self.assertEqual(len(fake.requests), 1)
        self.assertEqual(fake.requests[0]["action"], fake.TRADE_ACTION_REMOVE)
        self.assertEqual(fake.requests[0]["order"], 7)
        plan = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(plan["status"], "completed")
        self.assertEqual(plan["completion_reason"], "pending_timeout")

    def test_fresh_pending_order_is_left_untouched(self):
        fake = FakeMt5(orders=[self._order(age_minutes=5)])
        path = self._write_plan()
        with unittest.mock.patch.dict(sys.modules, {"MetaTrader5": fake}):
            result = manage(self._payload())

        self.assertEqual(result["managed"], 1)
        self.assertEqual(fake.requests, [])
        plan = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(plan["status"], "active")

    def test_timeout_disabled_keeps_stale_pending_order(self):
        fake = FakeMt5(orders=[self._order(age_minutes=60)])
        path = self._write_plan()
        with unittest.mock.patch.dict(sys.modules, {"MetaTrader5": fake}):
            result = manage(self._payload(pending_timeout_minutes=0.0))

        self.assertEqual(result["managed"], 1)
        self.assertEqual(fake.requests, [])
        plan = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(plan["status"], "active")


if __name__ == "__main__":
    unittest.main()
