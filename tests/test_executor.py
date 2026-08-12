import unittest
import json
from types import SimpleNamespace
from unittest.mock import patch

from aurum_bot.config import TradingConfig
from aurum_bot.executor import _run_account
from aurum_bot.models import AccountConfig, Direction, Signal


class ExecutorTests(unittest.TestCase):
    def test_unsupported_account_is_skipped_without_starting_mt5(self):
        account = AccountConfig(
            name="secondary_demo",
            enabled=True,
            risk_base_usd=25_000,
            terminal_path="terminal64.exe",
            symbols={"XAUUSD": "XAUUSD", "DE40": "GER30"},
        )
        signal = Signal(1, "US100", Direction.LONG, 27808.7, 27784.1, 27833.3)
        trading = TradingConfig(1, 0.9, 1.1, 0.01, 20, 3, 2, 397897)

        with patch("aurum_bot.executor.subprocess.Popen") as popen:
            result = _run_account(account, signal, trading)

        popen.assert_not_called()
        self.assertEqual(result.status, "skipped_unsupported_symbol")

    def test_passes_timing_context_to_worker(self):
        account = AccountConfig(
            name="fxpro",
            enabled=True,
            risk_base_usd=1000,
            terminal_path="terminal64.exe",
            symbols={"XAUUSD": "GOLD"},
        )
        signal = Signal(1, "XAUUSD", Direction.LONG, 2780, 2770, 2790)
        trading = TradingConfig(1, 0.9, 1.1, 0.01, 20, 3, 2, 397897)
        response = json.dumps(
            {
                "account": "fxpro",
                "status": "executed",
                "detail": "ok",
                "publication_to_receive_ms": 500,
                "publication_to_confirmation_ms": 812,
                "receive_to_confirmation_ms": 312,
                "order_send_round_trip_ms": 45,
            }
        )

        mock_proc = SimpleNamespace(returncode=0)
        mock_proc.communicate = lambda input=None, timeout=None: (response, "")
        with patch(
            "aurum_bot.executor.subprocess.Popen",
            return_value=mock_proc,
        ) as popen:
            result = _run_account(
                account,
                signal,
                trading,
                {"published_at_ms": 1000, "received_monotonic_ns": 2000},
            )

        call_kwargs = popen.call_args
        self.assertIsNotNone(call_kwargs)
        self.assertEqual(result.receive_to_confirmation_ms, 312)


if __name__ == "__main__":
    unittest.main()
