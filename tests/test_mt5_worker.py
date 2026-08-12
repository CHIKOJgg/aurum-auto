import unittest
from types import SimpleNamespace

from aurum_bot.models import Direction
from aurum_bot.mt5_worker import (
    _already_applied,
    _is_hedging_account,
    _pending_order_type,
    _prepare_for_new_signal,
)
from aurum_bot.models import ExecutionKind


class FakeMt5:
    TRADE_RETCODE_DONE = 10009
    TRADE_RETCODE_PLACED = 10008
    TRADE_RETCODE_DONE_PARTIAL = 10010
    TRADE_ACTION_REMOVE = 8
    ORDER_TYPE_BUY_LIMIT = 2
    ORDER_TYPE_SELL_LIMIT = 3
    ORDER_TYPE_BUY_STOP = 4
    ORDER_TYPE_SELL_STOP = 5

    def __init__(self, positions):
        self.positions = positions
        self.orders_read = False
        self.orders = []
        self.requests = []

    def positions_get(self, *, symbol):
        return None if self.positions is None else tuple(self.positions)

    def orders_get(self, *, symbol):
        self.orders_read = True
        return tuple(self.orders)

    def order_send(self, request):
        self.requests.append(request)
        return SimpleNamespace(retcode=self.TRADE_RETCODE_DONE, comment="done")

    def last_error(self):
        return (0, "ok")


class FakeNettingMt5(FakeMt5):
    TRADE_ACTION_DEAL = 1
    POSITION_TYPE_BUY = 0
    POSITION_TYPE_SELL = 1
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TIME_GTC = 0
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_RETURN = 2
    SYMBOL_TRADE_EXECUTION_MARKET = 2

    def __init__(self, positions=(), orders=()):
        super().__init__(list(positions))
        self.orders = list(orders)
        self.tick = SimpleNamespace(bid=99.5, ask=100.5)

    def symbol_info_tick(self, symbol):
        return self.tick

    def order_check(self, request):
        return SimpleNamespace(retcode=0, comment="ok")

    def order_send(self, request):
        self.requests.append(dict(request))
        if request["action"] == self.TRADE_ACTION_REMOVE:
            self.orders = [
                order for order in self.orders if order.ticket != request["order"]
            ]
        elif request["action"] == self.TRADE_ACTION_DEAL:
            self.positions = [
                position
                for position in self.positions
                if position.ticket != request["position"]
            ]
        return SimpleNamespace(retcode=self.TRADE_RETCODE_DONE, comment="done")


class HedgingPreparationTests(unittest.TestCase):
    def test_requires_hedging_margin_mode(self):
        mt5 = SimpleNamespace(ACCOUNT_MARGIN_MODE_RETAIL_HEDGING=2)
        self.assertTrue(_is_hedging_account(mt5, SimpleNamespace(margin_mode=2)))
        self.assertFalse(_is_hedging_account(mt5, SimpleNamespace(margin_mode=0)))

    def test_existing_position_does_not_block_new_signal(self):
        mt5 = FakeMt5([SimpleNamespace(ticket=42)])

        status, detail = _prepare_for_new_signal(mt5, "XAUUSD", 1234)

        self.assertEqual(status, "ready")
        self.assertIn("positions and pending orders are preserved", detail)
        self.assertFalse(mt5.orders_read)
        self.assertEqual(mt5.requests, [])

    def test_old_pending_order_is_preserved(self):
        mt5 = FakeMt5([])
        mt5.orders = [
            SimpleNamespace(
                ticket=77,
                magic=1234,
                type=FakeMt5.ORDER_TYPE_BUY_LIMIT,
            )
        ]

        status, detail = _prepare_for_new_signal(mt5, "XAUUSD", 1234)

        self.assertEqual(status, "ready")
        self.assertIn("pending orders are preserved", detail)
        self.assertFalse(mt5.orders_read)
        self.assertEqual(mt5.requests, [])

    def test_market_idempotency_is_scoped_to_message_comment(self):
        mt5 = FakeMt5(
            [
                SimpleNamespace(ticket=78, magic=1234, comment="AURUM:100"),
                SimpleNamespace(ticket=79, magic=1234, comment="AURUM:101"),
            ]
        )
        self.assertEqual(
            _already_applied(
                mt5, "XAUUSD", 1234, "AURUM:101", ExecutionKind.MARKET
            ),
            79,
        )
        self.assertIsNone(
            _already_applied(
                mt5, "XAUUSD", 1234, "AURUM:102", ExecutionKind.MARKET
            )
        )


class NettingPreparationTests(unittest.TestCase):
    def test_pending_orders_are_removed_before_buy_position_is_closed(self):
        mt5 = FakeNettingMt5(
            positions=[
                SimpleNamespace(
                    ticket=42,
                    type=FakeNettingMt5.POSITION_TYPE_BUY,
                    volume=0.3,
                    magic=1234,
                )
            ],
            orders=[SimpleNamespace(ticket=77, magic=1234), SimpleNamespace(ticket=78, magic=1234)],
        )
        symbol_info = SimpleNamespace(
            digits=2,
            filling_mode=1,
            trade_exemode=FakeNettingMt5.SYMBOL_TRADE_EXECUTION_MARKET,
        )

        status, detail = _prepare_for_new_signal(
            mt5,
            "XAUUSD",
            1234,
            replace_existing=True,
            symbol_info=symbol_info,
            deviation_points=25,
        )

        self.assertEqual(status, "ready")
        self.assertIn("removed 2 pending order(s), closed 1 position(s)", detail)
        self.assertEqual(
            [request["action"] for request in mt5.requests],
            [
                FakeNettingMt5.TRADE_ACTION_REMOVE,
                FakeNettingMt5.TRADE_ACTION_REMOVE,
                FakeNettingMt5.TRADE_ACTION_DEAL,
            ],
        )
        close_request = mt5.requests[-1]
        self.assertEqual(close_request["position"], 42)
        self.assertEqual(close_request["type"], FakeNettingMt5.ORDER_TYPE_SELL)
        self.assertEqual(close_request["price"], 99.5)
        self.assertEqual(close_request["volume"], 0.3)
        self.assertEqual(close_request["deviation"], 25)

    def test_sell_position_is_closed_with_buy_at_ask(self):
        mt5 = FakeNettingMt5(
            positions=[
                SimpleNamespace(
                    ticket=43,
                    type=FakeNettingMt5.POSITION_TYPE_SELL,
                    volume=0.2,
                    magic=1234,
                )
            ]
        )
        symbol_info = SimpleNamespace(
            digits=2,
            filling_mode=1,
            trade_exemode=FakeNettingMt5.SYMBOL_TRADE_EXECUTION_MARKET,
        )

        status, _ = _prepare_for_new_signal(
            mt5,
            "XAUUSD",
            1234,
            replace_existing=True,
            symbol_info=symbol_info,
        )

        self.assertEqual(status, "ready")
        close_request = mt5.requests[-1]
        self.assertEqual(close_request["type"], FakeNettingMt5.ORDER_TYPE_BUY)
        self.assertEqual(close_request["price"], 100.5)

    def test_rejected_pending_removal_blocks_position_close(self):
        class RejectingMt5(FakeNettingMt5):
            def order_send(self, request):
                self.requests.append(dict(request))
                return SimpleNamespace(retcode=10030, comment="rejected")

        mt5 = RejectingMt5(
            positions=[
                SimpleNamespace(
                    ticket=42,
                    type=FakeNettingMt5.POSITION_TYPE_BUY,
                    volume=0.3,
                    magic=1234,
                )
            ],
            orders=[SimpleNamespace(ticket=77, magic=1234)],
        )
        symbol_info = SimpleNamespace(
            digits=2,
            filling_mode=1,
            trade_exemode=FakeNettingMt5.SYMBOL_TRADE_EXECUTION_MARKET,
        )

        status, detail = _prepare_for_new_signal(
            mt5,
            "XAUUSD",
            1234,
            replace_existing=True,
            symbol_info=symbol_info,
        )

        self.assertEqual(status, "failed")
        self.assertIn("could not remove pending order 77", detail)
        self.assertEqual(len(mt5.requests), 1)
        self.assertEqual(len(mt5.positions), 1)


class PendingOrderTypeTests(unittest.TestCase):
    def test_long_beyond_tp_uses_buy_limit(self):
        self.assertEqual(
            _pending_order_type(FakeMt5, Direction.LONG, 100, 106),
            FakeMt5.ORDER_TYPE_BUY_LIMIT,
        )

    def test_long_beyond_sl_uses_buy_stop(self):
        self.assertEqual(
            _pending_order_type(FakeMt5, Direction.LONG, 100, 89),
            FakeMt5.ORDER_TYPE_BUY_STOP,
        )

    def test_short_beyond_tp_uses_sell_limit(self):
        self.assertEqual(
            _pending_order_type(FakeMt5, Direction.SHORT, 100, 89),
            FakeMt5.ORDER_TYPE_SELL_LIMIT,
        )

    def test_short_beyond_sl_uses_sell_stop(self):
        self.assertEqual(
            _pending_order_type(FakeMt5, Direction.SHORT, 100, 111),
            FakeMt5.ORDER_TYPE_SELL_STOP,
        )


class ClosePositionTests(unittest.TestCase):
    def test_emergency_close_position_success(self):
        from aurum_bot.mt5_worker import _close_position
        mt5 = FakeNettingMt5()
        pos = SimpleNamespace(type=0, symbol="XAUUSD", ticket=123, volume=0.1, magic=777)
        symbol_info = SimpleNamespace(digits=2, filling_mode=1, trade_exemode=2)
        res = _close_position(mt5, pos, symbol_info, 10)
        self.assertTrue(res)
        self.assertEqual(len(mt5.requests), 1)
        self.assertEqual(mt5.requests[0]["comment"], "AURUM:emergency_close")


if __name__ == "__main__":
    unittest.main()
