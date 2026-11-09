import copy
from datetime import datetime, timedelta, timezone
import gzip
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from partner_monitor.config import Config
from partner_monitor.evidence_archive import archive_evidence, backup_database, report_fingerprint, report_payload
from partner_monitor.publishing import publish_reports, seconds_until_sync
from partner_monitor.storage import Store
import test_unified_report


class PublishingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = test_unified_report.UnifiedReportTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.config = Config(self.fixture.root, "", [], [], google_credentials=Path("private-token.json"), spreadsheet_id="dedicated-test", drive_folder_id="private-folder")
        self.store = Store(self.config.db_path)
        self.addCleanup(self.store.db.close)
        self.data = (self.fixture.partners, self.fixture.posts, self.fixture.snapshots)
        self.store.dump = Mock(side_effect=lambda: copy.deepcopy(self.data))
        self.clock = self.enterContext(patch("partner_monitor.publishing.datetime"))
        self.clock.now.return_value = datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc)
        self.clock.fromisoformat.side_effect = datetime.fromisoformat
        self.reporter = Mock()
        self.reporter.sync.return_value = {}
        self.enterContext(patch("partner_monitor.publishing.GoogleReporter", return_value=self.reporter))
        self.archive = self.enterContext(patch("partner_monitor.publishing.archive_evidence", return_value={}))
        self.enterContext(patch("partner_monitor.publishing.export_xlsx"))

    async def test_monitoring_changes_archive_immediately_but_sheet_waits_fifteen_minutes(self):
        await publish_reports(self.config, self.store)
        self.assertEqual(self.reporter.sync.call_count, 1)
        for seconds in (5, 10, 60, 899):
            self.clock.now.return_value = datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc) + timedelta(seconds=seconds)
            self.fixture.snapshots[0]["views"] += 1
            await publish_reports(self.config, self.store)
        self.assertEqual(self.archive.call_count, 5)
        self.assertEqual(self.reporter.sync.call_count, 1)
        self.clock.now.return_value += timedelta(seconds=1)
        await publish_reports(self.config, self.store)
        self.assertEqual(self.reporter.sync.call_count, 2)

    async def test_restart_and_partial_google_failure_do_not_trigger_rapid_rewrites(self):
        self.reporter.sync.side_effect = RuntimeError("partial conversion failure")
        await publish_reports(self.config, self.store)
        self.store.db.close()
        self.store = Store(self.config.db_path)
        self.addCleanup(self.store.db.close)
        self.store.dump = Mock(return_value=copy.deepcopy(self.data))
        self.clock.now.return_value += timedelta(seconds=10)
        await publish_reports(self.config, self.store)
        self.assertEqual(self.reporter.sync.call_count, 1)
        self.assertEqual(seconds_until_sync(self.config, self.store), 890)
        self.clock.now.return_value += timedelta(seconds=890)
        await publish_reports(self.config, self.store)
        self.assertEqual(self.reporter.sync.call_count, 2)

    async def test_periodic_polling_does_not_create_duplicate_evidence_archives(self):
        await publish_reports(self.config, self.store)
        self.clock.now.return_value += timedelta(seconds=60)
        self.fixture.posts[0]["last_checked_at"] = self.clock.now.return_value.isoformat()
        self.fixture.partners[0]["last_message_id"] = 500
        self.fixture.partners[0]["intake"] = [{"text": "ordinary private group chatter"}]
        await publish_reports(self.config, self.store)
        self.assertEqual(self.archive.call_count, 1)
        self.assertEqual(self.reporter.sync.call_count, 1)

    async def test_archive_failure_never_overwrites_sheet_and_does_not_mark_data_as_saved(self):
        self.archive.side_effect = ValueError("existing history would be lost")
        with self.assertRaises(ValueError):
            await publish_reports(self.config, self.store)
        self.reporter.sync.assert_not_called()
        self.assertEqual(self.store.state("google_archive_fingerprint:dedicated-test", ""), "")


class EvidenceArchiveTests(unittest.TestCase):
    def test_daily_backup_is_a_readable_database_and_previous_backups_are_immutable(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            with Store(root / "monitor.sqlite3") as store:
                store.set_state("evidence", "original")
                day = datetime(2026, 11, 9).date()
                first = backup_database(root / "monitor.sqlite3", root / "backups", day)
                original = first.read_bytes()
                store.set_state("evidence", "later")
                self.assertEqual(backup_database(root / "monitor.sqlite3", root / "backups", day).read_bytes(), original)
                second = backup_database(root / "monitor.sqlite3", root / "backups", day + timedelta(days=1))
                restored = root / "restored.sqlite3"
                restored.write_bytes(gzip.decompress(original))
                from contextlib import closing
                with closing(sqlite3.connect(restored)) as db:
                    self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                    self.assertEqual(db.execute("SELECT value FROM state WHERE key='evidence'").fetchone()[0], "original")
                self.assertTrue(second.is_file())
                self.assertEqual(len(list((root / "backups").iterdir())), 2)

    def test_portable_manifest_excludes_group_chatter_paths_and_poll_cursors(self):
        data = ([{"id": 1, "intake": [{"text": "private chatter"}], "last_message_id": 42}],
                [{"id": 2, "url": "https://t.me/example/3", "last_checked_at": "volatile"}],
                [{"id": 4, "post_id": 2, "due_key": "initial", "screenshot_path": "local-secret-path", "screenshot_url": "https://drive.google.com/file/d/private-shot/view"}])
        raw = json.dumps(report_payload(*data))
        self.assertNotIn("private chatter", raw)
        self.assertNotIn("local-secret-path", raw)
        self.assertNotIn("last_message_id", raw)
        self.assertIn("private-shot", raw)
        changed = copy.deepcopy(data)
        changed[2][0]["screenshot_url"] = "different-url"
        self.assertEqual(report_fingerprint(*data), report_fingerprint(*changed))
        changed[2][0]["due_key"] = "24h"
        self.assertNotEqual(report_fingerprint(*data), report_fingerprint(*changed))

    def test_drive_manifest_and_pictures_are_added_without_deleting_previous_versions(self):
        reporter = Mock(spreadsheet_id="dedicated-test", drive_folder_id="private-folder")
        url = "https://drive.google.com/file/d/private-shot/view"
        reporter._upload.return_value = url
        reporter._json.side_effect = [{"files": []}, {"id": "manifest-id"}]
        snapshots = [{"id": 1, "post_id": 1, "due_key": "initial", "screenshot_status": "ok"}]
        with patch("partner_monitor.google_compact.verify_target"), patch("partner_monitor.google_compact.verify_history_preserved"):
            links = archive_evidence(reporter, [], [{"id": 1, "url": "https://t.me/example/3"}], snapshots)
            self.assertEqual(links, {"1": url})
            calls = reporter._json.call_args_list
            self.assertEqual([c.args[0] for c in calls], ["GET", "POST"])
            body = calls[-1].kwargs["data"]
            compressed = body[body.index(b"\x1f\x8b"):].split(b"\r\n--partner_archive_")[0]
            manifest = json.loads(gzip.decompress(compressed))
            self.assertEqual(manifest["snapshots"][0]["screenshot_url"], url)
            reporter._json.reset_mock()
            reporter._json.side_effect = [{"files": [{"id": "manifest-id"}]}]
            archive_evidence(reporter, [], [{"id": 1, "url": "https://t.me/example/3"}], snapshots)
            self.assertEqual([c.args[0] for c in reporter._json.call_args_list], ["GET"])
