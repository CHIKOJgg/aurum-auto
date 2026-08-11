import json
import tempfile
import unittest
from pathlib import Path

from aurum_bot.state import StateStore


class StateTests(unittest.TestCase):
    def test_startup_cutoff_and_atomic_records(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            state = StateStore(path, 3978977082)
            state.initialize_cutoff(100)
            self.assertEqual(state.last_seen_message_id, 100)

            state.mark(101, "claimed")
            state.mark(101, "completed", signal={"symbol": "DE40"})
            loaded = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(loaded["last_seen_message_id"], 101)
            self.assertEqual(loaded["messages"]["101"]["status"], "completed")

    def test_channel_mismatch_uses_isolated_state_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            first = StateStore(path, 1)
            first.initialize_cutoff(10)
            second = StateStore(path, 2)
            second.load()
            self.assertEqual(second.path, Path(directory) / "state.2.json")
            self.assertEqual(second.last_seen_message_id, 0)
            second.initialize_cutoff(20)

            original = StateStore(path, 1)
            original.load()
            self.assertEqual(original.path, path)
            self.assertEqual(original.last_seen_message_id, 10)

            test_channel = StateStore(path, 2)
            test_channel.load()
            self.assertEqual(test_channel.last_seen_message_id, 20)

    def test_unfinished_messages_only_returns_recoverable_statuses(self):
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json", 1)
            state.mark(1, "claimed")
            state.mark(2, "executing", signal={"symbol": "XAUUSD"})
            state.mark(3, "completed")
            self.assertEqual([item[0] for item in state.unfinished_messages()], [1, 2])


if __name__ == "__main__":
    unittest.main()
