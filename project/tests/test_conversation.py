"""Offline scenarios resembling a working advertising chat, with no real data."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from telethon import types, utils

from partner_monitor.engine import Monitor
from partner_monitor.intake import ConversationIntake
from partner_monitor.message_content import message_content
from partner_monitor.measurements import ensure_jobs, ready_jobs
from partner_monitor.parsing import parse_partner_references
from partner_monitor.realtime import Realtime
from partner_monitor.reports import _google_tables
from partner_monitor.storage import Store
import test_engine as fixtures
from test_engine import NOW, FakeClient, FakeCapture, message, channel


def image_message(mid, text="", photo=900, peer=100):
    m = message(mid, text, peer=peer)
    m.media = types.MessageMediaPhoto(photo=types.Photo(id=photo, access_hash=1, file_reference=b"private",
        date=NOW, sizes=[], dc_id=2))
    return m


class ConversationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.MonitorTests.asyncSetUp(self)
        self.config = replace(self.config, keywords=[], domains=[], test_mode=True,
            conversation_mode=True, data_namespace="test", allowed_channels=("partner_one",), default_client="Test Client",
            channel_plans=({"contract": "test-contract", "client": "Test Client", "channel": "partner_one",
                "title": "Test Channel", "aliases": ["Тестовый канал"], "planned_posts": 3, "language": "UZ"},))
        self.store.configure_plans(self.config)

    async def asyncTearDown(self):
        await fixtures.MonitorTests.asyncTearDown(self)

    def consume(self, m):
        content = message_content(m)
        ConversationIntake(self.store, self.config).consume(-1000000000100, m, content,
            [r for r in parse_partner_references(content.text, content.urls) if self.config.permits(r)])

    async def tick(self, client, capture=None):
        with patch("partner_monitor.engine.asyncio.sleep", new=AsyncMock()):
            await Monitor(self.config, self.store, client, capture or FakeCapture()).tick()

    async def test_separate_material_channel_preview_approval_survive_restart(self):
        self.consume(message(1, "Название: Product One\nИскать: Product One", peer=100))
        self.consume(message(2, "Тестовый канал", peer=100))
        self.consume(message(3, "Превью", peer=100))
        self.assertEqual(self.store.rows("SELECT status FROM placements")[0]["status"], "needs_review")
        self.store.__exit__(None, None, None)
        self.store = Store(self.root / "monitor.sqlite3")
        self.consume(message(4, "ок", peer=100))
        client = FakeClient(channel_messages=[message(7, "Product One — details"), message(8, "Unrelated news")])
        await self.tick(client)
        self.assertEqual([p["message_id"] for p in self.store.rows("SELECT * FROM posts")], [7])
        self.assertEqual(len(self.store.rows("SELECT * FROM intake_drafts")), 1)
        self.assertEqual(self.store.rows("SELECT contract_key FROM placements")[0]["contract_key"], "test-contract")

    async def test_unapproved_preview_never_reads_channel_or_counts_post(self):
        client = FakeClient([message(1, "Название: Product One\nКанал: @partner_one\nПревью", peer=100)], [message(7, "Product One")])
        await self.tick(client)
        self.assertFalse(self.store.rows("SELECT * FROM posts"))
        self.assertFalse(any(c[0] == "history" and c[1] == 200 for c in client.calls))

    async def test_invalid_explicit_schedule_is_not_silently_approved(self):
        self.consume(message(1, "Название: Product One\nКанал: @partner_one\nДата публикации: завтра вечером\nПревью", peer=100))
        self.consume(message(2, "ок", peer=100))
        self.assertEqual(self.store.rows("SELECT status FROM placements")[0]["status"], "needs_review")
        item = self.store.rows("SELECT * FROM source_messages WHERE message_id=2")[0]
        self.assertEqual(item["kind"], "needs_review")
        self.assertIn("период", item["review_reason"])

    async def test_two_pending_previews_make_unthreaded_ok_ambiguous(self):
        for mid, name in ((1, "Product One"), (2, "Product Two")):
            self.consume(message(mid, f"Название: {name}\nКанал: @partner_one\nПревью", peer=100))
        self.consume(message(3, "ок", peer=100))
        self.assertTrue(all(p["status"] == "needs_review" for p in self.store.rows("SELECT * FROM placements")))
        self.assertIn("однозначного", self.store.rows("SELECT * FROM source_messages WHERE message_id=3")[0]["review_reason"])
        reply = message(4, "ок", peer=100)
        reply.reply_to = types.MessageReplyHeader(reply_to_msg_id=1)
        self.consume(reply)
        self.assertEqual([p["status"] for p in self.store.rows("SELECT * FROM placements ORDER BY id")], ["watching", "needs_review"])

    async def test_material_revision_reuses_draft_and_requires_new_approval(self):
        self.consume(image_message(1, "Название: Product One\nКанал: @partner_one\nПревью"))
        self.consume(message(2, "ок", peer=100))
        self.consume(image_message(3, "Вот обновленный имидж", photo=901))
        drafts = self.store.rows("SELECT * FROM intake_drafts")
        self.assertEqual(len(drafts), 1)
        self.assertEqual(json.loads(drafts[0]["media_keys"]), ["photo:901"])
        self.assertIn("Product One", drafts[0]["creative_text"])
        self.assertEqual(self.store.rows("SELECT status FROM placements")[0]["status"], "needs_review")
        self.assertEqual(len(self.store.rows("SELECT * FROM source_messages")), 3)

    async def test_media_without_brand_caption_matches_only_approved_material(self):
        source = image_message(1)
        self.consume(source)
        self.consume(message(2, "Канал: @partner_one", peer=100))
        self.consume(message(3, "Превью", peer=100))
        self.consume(message(4, "ок", peer=100))
        client = FakeClient(channel_messages=[image_message(7, "Generic caption", peer=200), image_message(8, "Other", photo=901, peer=200)])
        await self.tick(client)
        self.assertEqual([p["message_id"] for p in self.store.rows("SELECT * FROM posts")], [7])
        self.assertTrue(self.store.rows("SELECT match_reason FROM posts")[0]["match_reason"].startswith("media:"))

    async def test_approved_copy_matches_when_channel_footer_and_emoji_differ(self):
        body = "This new financing product helps small businesses buy their own production equipment with instalments."
        self.consume(message(1, "🏦 Approved news\n\n" + body + "\n\nРеклама\n@first_footer", peer=100))
        self.consume(message(2, "Канал: @partner_one", peer=100))
        self.consume(message(3, "Превью", peer=100))
        self.consume(message(4, "ок", peer=100))
        client = FakeClient(channel_messages=[message(7, "✅ News\n\n" + body + "\n\n@second_footer")])
        await self.tick(client)
        self.assertEqual([p["message_id"] for p in self.store.rows("SELECT * FROM posts")], [7])

    async def test_new_client_command_is_local_context_without_messages(self):
        self.consume(message(1, "Новый клиент: Second Client", peer=100))
        self.consume(message(2, "Название: Another Product\nКанал: @partner_one\nПревью", peer=100))
        self.assertEqual(self.store.rows("SELECT client_name FROM intake_drafts")[0]["client_name"], "Second Client")
        self.assertFalse(self.store.rows("SELECT * FROM posts"))

    async def test_other_channels_and_person_mentions_are_not_registered(self):
        client = FakeClient([message(1, "@someone_person https://t.me/other_channel/77", peer=100)])
        await self.tick(client)
        self.assertEqual([p["username"] for p in self.store.rows("SELECT * FROM partners")], ["partner_one"])
        self.assertFalse(any("other_channel" in str(c) or "someone_person" in str(c) for c in client.calls))

    async def test_forwarded_template_outside_scope_is_not_a_paid_placement(self):
        m = image_message(1, "A long approved source caption " * 5)
        m.fwd_from = types.MessageFwdHeader(date=NOW, from_id=types.PeerChannel(999), channel_post=88)
        client = FakeClient([m])
        await self.tick(client)
        self.assertFalse(self.store.rows("SELECT * FROM placements"))
        self.assertFalse(self.store.rows("SELECT * FROM posts"))
        self.assertFalse(any("999" in str(c) for c in client.calls))

    async def test_repeated_direct_post_and_album_count_once_and_survive_deletion(self):
        client = FakeClient([message(1, "https://t.me/partner_one/7", peer=100),
                             message(2, "https://t.me/partner_one/8", peer=100)],
                            [message(7, "One advert", grouped_id=55), message(8, "", grouped_id=55)])
        await self.tick(client)
        before = _google_tables(*self.store.dump(), self.config.timezone)
        self.assertEqual(before[3][1][3:6], [3, 1, 2])
        client.histories[200].clear()
        await self.tick(client)
        after = _google_tables(*self.store.dump(), self.config.timezone)
        self.assertEqual(after[3][1][3:6], [3, 1, 2])
        self.assertEqual(len(after[6]), 2)
        self.assertEqual(len(self.store.rows("SELECT * FROM posts")), 1)

    async def test_migrated_source_data_and_unrelated_realtime_posts_are_isolated(self):
        self.assertNotEqual(self.config.db_path, self.config.data_dir / "monitor.sqlite3")
        self.consume(message(1, "https://t.me/partner_one/7", peer=100))
        self.store.execute("UPDATE partners SET channel_id=200 WHERE username='partner_one'")
        other = replace(self.config, allowed_channels=("another_channel",))
        await Monitor(other, self.store, FakeClient(), FakeCapture()).process_live([message(7, "One advert")])
        self.assertFalse(self.store.rows("SELECT * FROM posts"))

    async def test_confirmed_deletion_event_keeps_plan_and_evidence(self):
        client = FakeClient([message(1, "https://t.me/partner_one/7", peer=100)], [message(7, "One advert")])
        await self.tick(client)
        monitor = Monitor(self.config, self.store, client, FakeCapture())
        live = Realtime(monitor)
        await live.deliver_deletions([types.UpdateDeleteChannelMessages(channel_id=200, messages=[7], pts=2, pts_count=1)])
        self.assertEqual(self.store.rows("SELECT status FROM posts")[0]["status"], "deleted")
        client.histories[200].clear()
        await monitor.measure(self.store.rows("SELECT * FROM partners")[0], client.partner)
        self.assertEqual(self.store.rows("SELECT status FROM posts")[0]["status"], "deleted")
        self.assertEqual(_google_tables(*self.store.dump(), self.config.timezone)[3][1][3:6], [3, 1, 2])
        self.assertEqual(len(self.store.rows("SELECT * FROM snapshots")), 1)

    async def test_multiple_creatives_use_one_contract_plan_and_baseline_is_not_counted_twice(self):
        plan = dict(self.config.channel_plans[0], baseline_count=1, baseline_urls=["https://t.me/partner_one/7"])
        self.config = replace(self.config, channel_plans=(plan,))
        self.store.configure_plans(self.config)
        client = FakeClient([message(1, "https://t.me/partner_one/7", peer=100), message(2, "https://t.me/partner_one/8", peer=100)],
                            [message(7, "Advert One"), message(8, "Advert Two")])
        await self.tick(client)
        summary = _google_tables(*self.store.dump(), self.config.timezone)[3]
        self.assertEqual(len(summary), 2)
        self.assertEqual(summary[1][3:6], [3, 2, 1])
        self.assertEqual(summary[1][-1], 1)

    async def test_source_album_is_one_draft_with_all_media(self):
        first = image_message(1, "Название: Product One")
        second = image_message(2, photo=901)
        first.grouped_id = second.grouped_id = 99
        client = FakeClient([first, second, message(3, "Канал: @partner_one", peer=100)])
        await self.tick(client)
        drafts = self.store.rows("SELECT * FROM intake_drafts")
        self.assertEqual(len(drafts), 1)
        self.assertEqual(set(json.loads(drafts[0]["media_keys"])), {"photo:900", "photo:901"})
        self.assertEqual(self.store.state("source_cursor:100"), "3")

    async def test_baseline_link_to_second_album_member_does_not_count_album_again(self):
        plan = dict(self.config.channel_plans[0], baseline_count=1, baseline_urls=["https://t.me/partner_one/8"])
        self.config = replace(self.config, channel_plans=(plan,))
        self.store.configure_plans(self.config)
        client = FakeClient([message(1, "https://t.me/partner_one/8", peer=100)],
                            [message(7, "One advert", grouped_id=55), message(8, "", grouped_id=55)])
        await self.tick(client)
        self.assertEqual(_google_tables(*self.store.dump(), self.config.timezone)[3][1][3:6], [3, 1, 2])

    async def test_approval_is_scoped_to_preview_channel_and_new_target_is_not_autoapproved(self):
        extra = dict(self.config.channel_plans[0], channel="partner_two", title="Second Channel", aliases=["Второй канал"])
        self.config = replace(self.config, allowed_channels=("partner_one", "partner_two"), channel_plans=(*self.config.channel_plans, extra))
        self.consume(message(1, "Название: Product One\nКанал: @partner_one @partner_two", peer=100))
        self.consume(message(2, "Превью Тестовый канал", peer=100))
        self.consume(message(3, "ок", peer=100))
        rows = self.store.rows("SELECT p.username,b.status FROM placements b JOIN partners p ON p.id=b.partner_id")
        self.assertEqual({r["username"]:r["status"] for r in rows}, {"partner_one":"watching", "partner_two":"needs_review"})
        self.consume(message(4, "Второй канал", peer=100))
        rows = self.store.rows("SELECT p.username,b.status FROM placements b JOIN partners p ON p.id=b.partner_id")
        self.assertEqual({r["username"]:r["status"] for r in rows}, {"partner_one":"watching", "partner_two":"needs_review"})


class RichContentTests(unittest.TestCase):
    def test_carousel_text_urls_and_media_are_extracted_without_private_bytes(self):
        m = message(1)
        m.rich_message = {"_": "RichMessage", "blocks": [
            {"_": "PageBlockSlideshow", "items": [{"_": "PageBlockPhoto", "photo_id": 555}]},
            {"_": "PageBlockParagraph", "text": {"_": "TextConcat", "texts": [
                {"_": "TextBold", "text": {"_": "TextPlain", "text": "Brand "}},
                {"_": "TextUrl", "url": "https://brand.example/product", "text": {"_": "TextPlain", "text": "Product"}}]}}],
            "photos": [{"access_hash": "secret", "file_reference": b"private"}]}
        result = message_content(m)
        self.assertIn("Brand Product", result.text)
        self.assertEqual(result.media, ("photo:555",))
        self.assertEqual(result.urls, ("https://brand.example/product",))
        self.assertNotIn("private", repr(result))
        self.assertNotIn("secret", repr(result))


class DurableJobTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.MonitorTests.asyncSetUp(self)
        ref = parse_partner_references("@partner_one")[0]
        self.pid = self.store.add_partner(ref, 100, 1)
        self.post_id = self.store.save_post(self.pid, [message(7, "Product")], "direct", "partner_one")
        self.store.execute("UPDATE posts SET first_seen_at=?", ((NOW + timedelta(seconds=1)).isoformat(),))

    async def asyncTearDown(self):
        await fixtures.MonitorTests.asyncTearDown(self)

    def post(self):
        return self.store.rows("SELECT * FROM posts")[0]

    async def test_queue_survives_restart_and_does_not_recreate_completed_measurements(self):
        ensure_jobs(self.store, self.post(), (24, 48))
        self.store.__exit__(None, None, None)
        self.store = Store(self.root / "monitor.sqlite3")
        self.assertEqual(len(self.store.rows("SELECT * FROM measurement_jobs")), 3)
        self.store.execute("UPDATE measurement_jobs SET status='done' WHERE due_key='initial'")
        ready = ready_jobs(self.store, self.post_id, NOW + timedelta(hours=25))
        self.assertEqual([j["due_key"] for j in ready], ["24h"])

    async def test_late_discovery_marks_old_milestones_missed_without_fake_views(self):
        self.store.execute("UPDATE posts SET published_at=?", ((NOW - timedelta(days=3)).isoformat(),))
        ensure_jobs(self.store, self.post(), (24, 48))
        self.assertEqual({j["status"] for j in self.store.rows("SELECT * FROM measurement_jobs WHERE due_key!='initial'")}, {"missed"})
        self.assertFalse(self.store.rows("SELECT * FROM snapshots"))

    async def test_restart_after_both_deadlines_does_not_duplicate_current_counter_as_two_historical_values(self):
        ensure_jobs(self.store, self.post(), (24, 48))
        ready = ready_jobs(self.store, self.post_id, NOW + timedelta(hours=50))
        self.assertEqual([j["due_key"] for j in ready], ["initial", "48h"])
        self.assertEqual(self.store.rows("SELECT status FROM measurement_jobs WHERE due_key='24h'")[0]["status"], "missed")

    async def test_actual_24_48_measurements_execute_individually_and_keep_queue_on_restart(self):
        capture = FakeCapture()
        client = FakeClient(channel_messages=[message(7, "Product", views=10)])
        monitor = Monitor(self.config, self.store, client, capture)
        partner = self.store.rows("SELECT * FROM partners")[0]
        await monitor.record_post(partner, client.partner, client.histories[200], "direct")
        for hour, count in ((24, 100), (48, 200)):
            instant = NOW + timedelta(hours=hour, seconds=5)
            class Clock(datetime):
                @classmethod
                def now(cls, tz=None):
                    return instant
            client.histories[200][0].views = count
            with patch("partner_monitor.engine.datetime", Clock), patch("partner_monitor.engine.utcnow", return_value=instant.isoformat()):
                await monitor.measure(partner, client.partner)
            self.store.__exit__(None, None, None)
            self.store = Store(self.root / "monitor.sqlite3")
            monitor = Monitor(self.config, self.store, client, capture)
        snapshots = self.store.rows("SELECT * FROM snapshots ORDER BY id")
        self.assertEqual([(s["due_key"], s["views"]) for s in snapshots], [("initial", 10), ("24h", 100), ("48h", 200)])
        self.assertTrue(all(j["status"] == "done" for j in self.store.rows("SELECT * FROM measurement_jobs")))
        self.assertEqual(len(capture.calls), 3)

