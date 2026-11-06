from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from partner_monitor.config import load_config


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.yaml"

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, text):
        self.path.write_text(text, encoding="utf-8")
        return load_config(self.path)

    def test_milestones_reject_scalar_string_instead_of_parsing_digits_as_hours(self):
        with self.assertRaisesRegex(ValueError, "milestones_hours"):
            self.load('milestones_hours: "24"\n')

    def test_milestones_reject_boolean_string_negative_and_nan(self):
        for value in ("[true]", '["24"]', "[-24]", "[0]", "[.nan]", "[.inf]", "[9000]"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "milestones_hours"):
                self.load("milestones_hours: " + value + "\n")

    def test_valid_milestones_sorted_deduplicated_and_timezone_defaults_to_tashkent(self):
        config = self.load("milestones_hours: [48, 24, 24, 0.5]\nsource_group: '-10012345'\n")
        self.assertEqual(config.milestones_hours, (0.5, 24, 48))
        self.assertEqual(config.timezone, "Asia/Tashkent")
        self.assertEqual(config.source_group, -10012345)

    def test_unknown_settings_and_empty_keywords_rejected(self):
        for content in ("poll_interva: 300\n", "keywords: ['']\n", "domains: brb.uz\n"):
            with self.subTest(content=content), self.assertRaises(ValueError):
                self.load(content)

    def test_credentials_resolve_relative_to_configuration_directory(self):
        config = self.load("google_credentials: secrets/google.json\n")
        self.assertEqual(config.google_credentials, self.path.parent / "secrets" / "google.json")

    def test_realtime_limit_cannot_exceed_telegram_maximum(self):
        self.assertEqual(self.load("realtime_channel_limit: 10\n").realtime_channel_limit, 10)
        self.assertEqual(self.load("realtime_channel_limit: 0\n").realtime_channel_limit, 0)
        for value in ("11", "-1", "true", "2.5", "'10'"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "realtime_channel_limit"):
                self.load("realtime_channel_limit: " + value + "\n")

    def test_test_scope_requires_channel_and_separate_database_before_run(self):
        for content in ("test_mode: true\n", "test_mode: true\nallowed_channels: [partner_one]\n"):
            with self.subTest(content=content), self.assertRaisesRegex(ValueError, "Тест"):
                self.load(content).validate_run()
        config = self.load("test_mode: true\nallowed_channels: ['https://t.me/partner_one']\ndata_namespace: test\n")
        config.validate_run()
        self.assertEqual(config.allowed_channels, ("partner_one",))
        self.assertEqual(config.db_path, self.path.parent / "data" / "test" / "monitor.sqlite3")

    def test_namespace_and_channel_scope_cannot_be_arbitrary_paths_or_posts(self):
        for content in ("data_namespace: ../live\n", "allowed_channels: [https://t.me/partner_one/7]\n",
                        "test_mode: 'true'\n", "channel_plans: wrong\n"):
            with self.subTest(content=content), self.assertRaises(ValueError):
                self.load(content)

    def test_incomplete_historical_baseline_requires_explicit_cutoff(self):
        text = "channel_plans:\n  - contract: test\n    client: Example\n    channel: partner_one\n    planned_posts: 10\n    baseline_count: 3\n"
        with self.assertRaisesRegex(ValueError, "baseline_until"):
            self.load(text)
        config = self.load(text + "    baseline_until: '2026-11-06T00:00:00+05:00'\n")
        self.assertEqual(config.channel_plans[0]["baseline_until"], "2026-11-05T19:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
