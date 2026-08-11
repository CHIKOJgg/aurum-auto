import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from aurum_bot.config import load_config


class YamlConfigurationTests(unittest.TestCase):
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

    def test_risk_is_taken_from_yaml_not_locked_in_code(self):
        config = self._load(lambda data: data["trading"].update(risk_percent=0.5))
        self.assertEqual(config.trading.risk_percent, 0.5)

    def test_missing_critical_yaml_setting_prevents_startup(self):
        with self.assertRaisesRegex(ValueError, "trading.risk_percent"):
            self._load(lambda data: data["trading"].pop("risk_percent"))


if __name__ == "__main__":
    unittest.main()
