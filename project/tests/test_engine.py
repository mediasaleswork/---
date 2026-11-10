from __future__ import annotations

import json
import io
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from telethon import errors, types
from telethon.tl.functions.messages import CheckChatInviteRequest

from partner_monitor.capture import CaptureResult
from partner_monitor.config import Config
from partner_monitor.engine import Monitor, due_milestones
from partner_monitor.parsing import PartnerReference
from partner_monitor.storage import Store


NOW = datetime.now(timezone.utc)


def channel(id=200, username="partner_one", *, group=False):
    return types.Channel(id=id, title=username, photo=types.ChatPhotoEmpty(), date=NOW,
                         username=username, megagroup=group, broadcast=not group)


def message(id, text="", *, peer=200, date=None, views=10, grouped_id=None, entities=None):
    return types.Message(id=id, peer_id=types.PeerChannel(peer), date=date or NOW,
                         message=text, views=views, grouped_id=grouped_id, entities=entities)


class FakeClient:
    def __init__(self, group_messages=(), channel_messages=()):
        self.group = channel(100, "work_group", group=True)
        self.partner = channel()
        self.entities = {"@work_group": self.group, "partner_one": self.partner}
        self.histories = {100: list(group_messages), 200: list(channel_messages)}
        self.calls = []
        self.fail_group_after = None
        self.attachments = {}
        self.invites = {}

    async def get_entity(self, key):
        self.calls.append(("entity", str(key)))
        if isinstance(key, types.PeerChannel):
            return next(e for e in self.entities.values() if e.id == key.channel_id)
        value = self.entities[key]
        if isinstance(value, BaseException):
            raise value
        return value

    async def get_messages(self, entity, ids):
        self.calls.append(("messages", entity.id, ids))
        by_id = {m.id: m for m in self.histories[entity.id]}
        if isinstance(ids, int):
            return by_id.get(ids)
        return [by_id.get(id) for id in ids]

    async def iter_messages(self, entity, **options):
        self.calls.append(("history", entity.id, dict(options)))
        items = [m for m in self.histories[entity.id] if m.id > options.get("min_id", 0)]
        if options.get("offset_date"):
            items = [m for m in items if m.date > options["offset_date"]]
        items.sort(key=lambda m: m.id, reverse=not options.get("reverse"))
        for index, item in enumerate(items[:options["limit"]]):
            if entity.id == 100 and self.fail_group_after == index:
                raise OSError("connection interrupted")
            yield item

    async def __call__(self, request):
        if isinstance(request, CheckChatInviteRequest):
            self.calls.append(("check_invite", request.hash))
            return self.invites.get(request.hash, types.ChatInvite(title="Private", photo=types.PhotoEmpty(id=1), participants_count=0, color=0))
        raise AssertionError("Monitor must not send join requests or mutating Telegram requests")

    async def download_media(self, message, file):
        Path(file).write_bytes(self.attachments[message.id])
        return file


def docx_bytes(text="Текст договора", hidden_url=None):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>' + text + '</w:t></w:r></w:p></w:body></w:document>')
        if hidden_url:
            archive.writestr("word/_rels/document.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" TargetMode="External" Target="' + hidden_url + '"/></Relationships>')
    return out.getvalue()


def attachment_message(id, data, name="partner.docx"):
    msg = message(id, "Новый партнёр", peer=100)
    document = types.Document(id=300, access_hash=1, file_reference=b"", date=NOW,
                              mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                              size=len(data), dc_id=2, attributes=[types.DocumentAttributeFilename(name)])
    msg.media = types.MessageMediaDocument(document=document)
    return msg


class FakeCapture:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def capture(self, username, ids, key):
        self.calls.append((username, ids, key))
        return CaptureResult(None, "failed", "test failure") if self.fail else CaptureResult("/tmp/example-real-capture.png", "ok")


class MonitorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "monitor.sqlite3")
        self.config = Config(root=self.root, source_group="@work_group", keywords=["BRB"], domains=["brb.uz"])

    async def asyncTearDown(self):
        self.store.__exit__(None, None, None)
        self.tmp.cleanup()

    async def test_two_cycles_and_restart_preserve_cursors_without_duplicates(self):
        client = FakeClient([message(1, "Новый партнёр @partner_one", peer=100)],
                            [message(1, "Editorial"), message(2, "BRB кредит", views=245)])
        capture = FakeCapture()
        with patch("partner_monitor.engine.asyncio.sleep", new=AsyncMock()):
            await Monitor(self.config, self.store, client, capture).tick()
            self.store.__exit__(None, None, None)
            self.store = Store(self.root / "monitor.sqlite3")
            client.histories[100].append(message(2, "https://t.me/partner_one", peer=100))
            client.histories[200].append(message(3, "Следующая новость"))
            await Monitor(self.config, self.store, client, capture).tick()
        partners, posts, snapshots = self.store.dump()
        self.assertEqual((len(partners), len(posts), len(snapshots)), (1, 1, 1))
        self.assertEqual(partners[0]["last_message_id"], 3)
        self.assertEqual(snapshots[0]["views"], 245)
        self.assertEqual(self.store.state("source_cursor:100"), "2")
        self.assertEqual(len(capture.calls), 1)
        history_calls = [x for x in client.calls if x[0] == "history" and x[1] == 100]
        self.assertEqual(history_calls[1][2]["min_id"], 1)
        self.assertNotIn("offset_date", history_calls[1][2])

    async def test_hidden_direct_link_tracks_post_without_brand_keyword(self):
        link = types.MessageEntityTextUrl(offset=0, length=6, url="https://t.me/partner_one/7")
        client = FakeClient([message(1, "Партнёр", peer=100, entities=[link])], [message(7, "Согласованная публикация", views=0)])
        capture = FakeCapture()
        with patch("partner_monitor.engine.asyncio.sleep", new=AsyncMock()):
            await Monitor(self.config, self.store, client, capture).tick()
        self.assertEqual(self.store.rows("SELECT * FROM posts")[0]["message_id"], 7)
        self.assertEqual(self.store.rows("SELECT * FROM snapshots")[0]["views"], 0)
        self.assertEqual(self.store.rows("SELECT * FROM requested_posts")[0]["done"], 1)

    async def test_private_invite_only_checks_existing_access_without_join(self):
        client = FakeClient([message(1, "https://t.me/+privateInviteCode", peer=100)])
        with patch("partner_monitor.engine.asyncio.sleep", new=AsyncMock()):
            await Monitor(self.config, self.store, client, FakeCapture()).tick()
        self.assertEqual(self.store.rows("SELECT * FROM partners")[0]["status"], "private")
        self.assertEqual([x for x in client.calls if x[0] == "entity"], [("entity", "@work_group")])
        self.assertEqual([x for x in client.calls if x[0] == "check_invite"], [("check_invite", "privateInviteCode")])

    async def test_failed_screenshot_retries_without_remeasuring_old_view_count(self):
        client = FakeClient([message(1, "@partner_one", peer=100)], [message(4, "BRB", views=50)])
        capture = FakeCapture(fail=True)
        monitor = Monitor(self.config, self.store, client, capture)
        with patch("partner_monitor.engine.asyncio.sleep", new=AsyncMock()):
            await monitor.tick()
            first = self.store.rows("SELECT * FROM snapshots")[0]
            client.histories[200][0].views = 120
            capture.fail = False
            self.store.execute("UPDATE snapshots SET next_capture_at='2000-01-01T00:00:00+00:00'")
            await monitor.tick()
        after = self.store.rows("SELECT * FROM snapshots")[0]
        self.assertEqual(after["checked_at"], first["checked_at"])
        self.assertEqual(after["views"], 50)
        self.assertEqual(after["capture_attempts"], 2)
        self.assertIsNotNone(after["screenshot_at"])

    async def test_missing_post_is_unavailable_without_confirming_deletion(self):
        client = FakeClient([message(1, "@partner_one", peer=100)], [message(4, "BRB")])
        capture = FakeCapture(fail=True)
        monitor = Monitor(self.config, self.store, client, capture)
        with patch("partner_monitor.engine.asyncio.sleep", new=AsyncMock()):
            await monitor.tick()
            client.histories[200].clear()
            await monitor.tick()
        post = self.store.rows("SELECT * FROM posts")[0]
        self.assertEqual(post["status"], "unavailable")
        self.assertIn("не подтверждено", post["last_error"])
        self.assertEqual(len(capture.calls), 1)

    async def test_album_records_one_post_and_does_not_sum_views(self):
        album = [message(10, "BRB кредит", views=40, grouped_id=555), message(11, "", views=40, grouped_id=555)]
        client = FakeClient([message(1, "@partner_one", peer=100)], album)
        capture = FakeCapture()
        with patch("partner_monitor.engine.asyncio.sleep", new=AsyncMock()):
            await Monitor(self.config, self.store, client, capture).tick()
        posts = self.store.rows("SELECT * FROM posts")
        self.assertEqual(len(posts), 1)
        self.assertEqual(json.loads(posts[0]["message_ids"]), [10, 11])
        self.assertEqual(self.store.rows("SELECT * FROM snapshots")[0]["views"], 40)
        self.assertEqual(capture.calls[0][1], [10, 11])

    async def test_interrupted_group_read_advances_only_processed_messages(self):
        client = FakeClient([message(1, "@partner_one", peer=100), message(2, "@partner_two", peer=100)])
        client.fail_group_after = 1
        monitor = Monitor(self.config, self.store, client, FakeCapture())
        with self.assertRaises(OSError):
            await monitor.ingest_group()
        self.assertEqual(self.store.state("source_cursor:100"), "1")
        client.fail_group_after = None
        await monitor.ingest_group()
        self.assertEqual(self.store.state("source_cursor:100"), "2")
        self.assertEqual(len(self.store.rows("SELECT * FROM partners")), 2)

    async def test_flood_wait_propagates_to_scheduler_without_join(self):
        client = FakeClient([message(1, "@partner_one", peer=100)])
        client.entities["partner_one"] = errors.FloodWaitError(request=None, capture=17)
        with self.assertRaises(errors.FloodWaitError):
            await Monitor(self.config, self.store, client, FakeCapture()).tick()

    async def test_reassigned_username_is_not_scanned_or_captured(self):
        client = FakeClient([message(1, "@partner_one", peer=100)], [message(4, "BRB")])
        capture = FakeCapture()
        monitor = Monitor(self.config, self.store, client, capture)
        with patch("partner_monitor.engine.asyncio.sleep", new=AsyncMock()):
            await monitor.tick()
            client.entities["partner_one"] = channel(id=999, username="partner_one")
            before = len(client.calls)
            await monitor.tick()
        row = self.store.rows("SELECT * FROM partners")[0]
        self.assertEqual(row["status"], "identity_changed")
        self.assertEqual(row["channel_id"], 200)
        self.assertFalse(any(call[0] in {"history", "messages"} and call[1] == 999 for call in client.calls[before:]))
        self.assertEqual(len(capture.calls), 1)

    async def test_document_without_channel_link_is_recorded_for_review(self):
        data = docx_bytes("Договор о размещении рекламы BRB. Канал не указан.")
        client = FakeClient([attachment_message(1, data)])
        client.attachments[1] = data
        await Monitor(self.config, self.store, client, FakeCapture()).ingest_group()
        partners = self.store.rows("SELECT * FROM partners")
        self.assertEqual(len(partners), 1)
        self.assertEqual(partners[0]["status"], "needs_review")
        self.assertIsNone(partners[0]["username"])
        self.assertEqual(self.store.state("source_cursor:100"), "1")

    async def test_document_hidden_hyperlink_adds_channel(self):
        data = docx_bytes("Партнёр", hidden_url="https://t.me/partner_one")
        client = FakeClient([attachment_message(1, data)])
        client.attachments[1] = data
        await Monitor(self.config, self.store, client, FakeCapture()).ingest_group()
        partners = self.store.rows("SELECT * FROM partners")
        self.assertEqual(len(partners), 1)
        self.assertEqual(partners[0]["username"], "partner_one")
        self.assertEqual(partners[0]["status"], "active")


class MilestoneTests(unittest.TestCase):
    def test_old_post_does_not_fabricate_historical_24_hour_views(self):
        published = NOW - timedelta(days=5)
        post = {"published_at": published.isoformat(), "first_seen_at": NOW.isoformat()}
        result = due_milestones(post, [], (24, 48, 168), NOW)
        self.assertEqual([key for key, _ in result], ["initial"])

    def test_restart_after_due_date_preserves_actual_deadline(self):
        published = NOW - timedelta(hours=26)
        first_seen = published + timedelta(hours=1)
        post = {"published_at": published.isoformat(), "first_seen_at": first_seen.isoformat()}
        result = due_milestones(post, [{"due_key": "initial"}], (24, 48), NOW)
        self.assertEqual(result, [("24h", (published + timedelta(hours=24)).isoformat())])


if __name__ == "__main__":
    unittest.main()
