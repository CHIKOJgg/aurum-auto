import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aurum_bot.entry_guards import (
    load_news_events,
    margin_allowed,
    news_blocked,
    spread_allowed,
)


def _msc(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


class SpreadGuardTests(unittest.TestCase):
    def test_spread_within_limit_is_allowed(self):
        self.assertTrue(
            spread_allowed(100.30, 100.00, max_spread_points=50, point=0.01)
        )

    def test_spread_over_limit_is_rejected(self):
        self.assertFalse(
            spread_allowed(101.00, 100.00, max_spread_points=50, point=0.01)
        )

    def test_disabled_limit_always_allows(self):
        self.assertTrue(
            spread_allowed(110.00, 100.00, max_spread_points=0, point=0.01)
        )


class MarginGuardTests(unittest.TestCase):
    def test_sufficient_margin_is_allowed(self):
        self.assertTrue(margin_allowed(150.0, 200.0))

    def test_insufficient_margin_is_rejected(self):
        self.assertFalse(margin_allowed(250.0, 200.0))

    def test_missing_values_are_accepted(self):
        """MT5 API failures resulting in None should bypass the guard to avoid blocking trading."""
        self.assertTrue(margin_allowed(None, 1000.0))
        self.assertTrue(margin_allowed(100.0, None))
        self.assertTrue(margin_allowed(None, None))


class NewsFilterTests(unittest.TestCase):
    def test_event_inside_window_is_blocked(self):
        now = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
        events = [
            {"start_msc": _msc(now), "title": "CPI"},
        ]
        blocked, title = news_blocked(
            events,
            now_msc=_msc(now),
            window_before_minutes=10,
            window_after_minutes=15,
        )
        self.assertTrue(blocked)
        self.assertEqual(title, "CPI")

    def test_event_outside_window_is_allowed(self):
        now = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
        events = [
            {"start_msc": _msc(now + timedelta(hours=2)), "title": "NFP"},
        ]
        blocked, _ = news_blocked(
            events,
            now_msc=_msc(now),
            window_before_minutes=10,
            window_after_minutes=15,
        )
        self.assertFalse(blocked)

    def test_before_window_blocked_includes_pre_event_margin(self):
        event = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
        probe = event - timedelta(minutes=9)
        blocked, _ = news_blocked(
            [{"start_msc": _msc(event), "title": "FOMC"}],
            now_msc=_msc(probe),
            window_before_minutes=10,
            window_after_minutes=15,
        )
        self.assertTrue(blocked)

    def test_after_window_blocked_includes_post_event_margin(self):
        event = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
        probe = event + timedelta(minutes=14)
        blocked, _ = news_blocked(
            [{"start_msc": _msc(event), "title": "FOMC"}],
            now_msc=_msc(probe),
            window_before_minutes=10,
            window_after_minutes=15,
        )
        self.assertTrue(blocked)

    def test_disabled_windows_never_block(self):
        now = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
        blocked, _ = news_blocked(
            [{"start_msc": _msc(now), "title": "CPI"}],
            now_msc=_msc(now),
            window_before_minutes=0,
            window_after_minutes=0,
        )
        self.assertFalse(blocked)


class NewsFileLoadingTests(unittest.TestCase):
    def test_loads_events_with_naive_and_aware_timestamps(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "news.json"
            path.write_text(
                json.dumps(
                    [
                        {"start_utc": "2026-08-11T12:00", "title": "CPI"},
                        {"start_utc": "2026-08-11T12:00+00:00", "title": "NFP"},
                        {"start_utc": "bad-date", "title": "ignored"},
                        "not-a-dict",
                    ]
                ),
                encoding="utf-8",
            )
            events = load_news_events(path)
        self.assertEqual([event["title"] for event in events], ["CPI", "NFP"])

    def test_missing_file_returns_empty_list(self):
        self.assertEqual(load_news_events(Path("no/such/file.json")), [])


if __name__ == "__main__":
    unittest.main()
