from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from partner_monitor.parsing import PartnerReference
from partner_monitor.storage import Store


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "database.sqlite3"
        self.store = Store(self.path)

    def tearDown(self):
        self.store.__exit__(None, None, None)
        self.tmp.cleanup()

    def test_duplicate_channel_and_direct_post_keep_single_partner_and_request(self):
        first = self.store.add_partner(PartnerReference("partner_one", source_url="https://t.me/partner_one"), 100, 1)
        direct = PartnerReference("partner_one", 7, "https://t.me/partner_one/7")
        self.assertEqual(self.store.add_partner(direct, 100, 2), first)
        self.assertEqual(self.store.add_partner(direct, 100, 3), first)
        self.assertEqual(len(self.store.rows("SELECT * FROM partners")), 1)
        self.assertEqual(len(self.store.rows("SELECT * FROM requested_posts")), 1)

    def test_private_reference_never_enqueues_public_post_request(self):
        self.store.add_partner(PartnerReference(None, 7, "https://t.me/c/12345/7", True, 12345), 100, 1)
        self.assertEqual(self.store.rows("SELECT * FROM requested_posts"), [])
        self.assertEqual(self.store.rows("SELECT * FROM partners")[0]["status"], "private")

    def test_duplicate_album_updates_text_and_preserves_first_seen(self):
        partner = self.store.add_partner(PartnerReference("partner_one"), 100, 1)
        date = datetime.now(timezone.utc)
        item = SimpleNamespace(id=7, grouped_id=1234, message="BRB original", date=date)
        post = self.store.save_post(partner, [item], "keyword:BRB", "partner_one")
        original = self.store.rows("SELECT * FROM posts")[0]
        item.message = "BRB edited"
        same = self.store.save_post(partner, [item], "direct_link", "partner_one")
        result = self.store.rows("SELECT * FROM posts")[0]
        self.assertEqual(post, same)
        self.assertEqual(original["first_seen_at"], result["first_seen_at"])
        self.assertEqual(result["text"], "BRB edited")

    def test_snapshot_unique_constraint_prevents_duplicate_measurement(self):
        partner = self.store.add_partner(PartnerReference("partner_one"), 100, 1)
        item = SimpleNamespace(id=7, grouped_id=None, message="BRB", date=datetime.now(timezone.utc))
        post = self.store.save_post(partner, [item], "keyword:BRB", "partner_one")
        sql = "INSERT OR IGNORE INTO snapshots(post_id,due_key,due_at,checked_at,views,screenshot_status) VALUES (?,?,?,?,?,'pending')"
        self.store.execute(sql, (post, "initial", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00", 10))
        self.store.execute(sql, (post, "initial", "2026-01-01T00:00:00+00:00", "2026-01-01T01:00:00+00:00", 20))
        self.assertEqual(len(self.store.rows("SELECT * FROM snapshots")), 1)
        self.assertEqual(self.store.rows("SELECT * FROM snapshots")[0]["views"], 10)

    def test_expanded_album_updates_anchor_url_without_resetting_first_seen(self):
        partner = self.store.add_partner(PartnerReference("partner_one"), 100, 1)
        date = datetime.now(timezone.utc)
        later = SimpleNamespace(id=8, grouped_id=1234, message="BRB caption", date=date)
        self.store.save_post(partner, [later], "keyword:BRB", "partner_one")
        first_seen = self.store.rows("SELECT * FROM posts")[0]["first_seen_at"]
        earlier = SimpleNamespace(id=7, grouped_id=1234, message="", date=date)
        self.store.save_post(partner, [earlier, later], "keyword:BRB", "partner_one")
        post = self.store.rows("SELECT * FROM posts")[0]
        self.assertEqual(post["message_id"], 7)
        self.assertEqual(post["url"], "https://t.me/partner_one/7")
        self.assertEqual(post["first_seen_at"], first_seen)


if __name__ == "__main__":
    unittest.main()
