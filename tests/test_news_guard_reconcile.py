import unittest

def _should_check_news(trading: dict, payload: dict) -> bool:
    return (
        bool(trading.get("news_guard_enabled", True))
        and not bool(payload.get("reconcile", False))
        and (
            float(trading.get("news_window_before_minutes", 0.0)) > 0
            or float(trading.get("news_window_after_minutes", 0.0)) > 0
        )
    )

class TestNewsGuardReconcileCondition(unittest.TestCase):

    def test_news_guard_skipped_during_reconcile(self):
        trading = {
            "news_guard_enabled": True,
            "news_window_before_minutes": 15.0,
            "news_window_after_minutes": 15.0,
        }
        payload = {"reconcile": True}
        self.assertFalse(_should_check_news(trading, payload))

    def test_news_guard_fires_on_normal_entry(self):
        trading = {
            "news_guard_enabled": True,
            "news_window_before_minutes": 15.0,
            "news_window_after_minutes": 15.0,
        }
        payload = {"reconcile": False}
        self.assertTrue(_should_check_news(trading, payload))

    def test_news_guard_disabled_by_flag(self):
        trading = {
            "news_guard_enabled": False,
            "news_window_before_minutes": 15.0,
            "news_window_after_minutes": 15.0,
        }
        payload = {"reconcile": False}
        self.assertFalse(_should_check_news(trading, payload))

    def test_news_guard_inactive_when_both_windows_zero(self):
        trading = {
            "news_guard_enabled": True,
            "news_window_before_minutes": 0.0,
            "news_window_after_minutes": 0.0,
        }
        payload = {"reconcile": False}
        self.assertFalse(_should_check_news(trading, payload))

if __name__ == '__main__':
    unittest.main()
