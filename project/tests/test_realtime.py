import asyncio
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from telethon import errors, types
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.updates import GetChannelDifferenceRequest

from partner_monitor.engine import Monitor, report_loop, run
from partner_monitor.parsing import parse_partner_references
from partner_monitor.realtime import Realtime, difference_batch
from partner_monitor.telegram_access import TelegramAccess
import test_engine as fixtures
from test_engine import FakeClient, FakeCapture, NOW, channel, message


class EventClient(FakeClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.handlers = []
        self.differences = []

    def add_event_handler(self, callback, builder):
        self.handlers.append(callback)

    def remove_event_handler(self, callback):
        self.handlers.remove(callback)

    async def __call__(self, request):
        if isinstance(request, GetFullChannelRequest):
            self.calls.append(("full_channel", request.channel.channel_id))
            return SimpleNamespace(full_chat=SimpleNamespace(pts=10))
        if isinstance(request, GetChannelDifferenceRequest):
            self.calls.append(("difference", request.channel.channel_id, request.pts, request.limit))
            return self.differences.pop(0)
        return await super().__call__(request)  # Reject every join/mutating request.


class RealtimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.MonitorTests.asyncSetUp(self)
        self.config = replace(self.config, keywords=[], domains=[])
        self.client = EventClient()
        self.capture = FakeCapture()
        self.monitor = Monitor(self.config, self.store, self.client, self.capture)
        self.live = Realtime(self.monitor)
        self.live.start()
        self.add_brief()

    async def asyncTearDown(self):
        await self.live.close()
        await fixtures.MonitorTests.asyncTearDown(self)

    def add_brief(self, text="Бренд: Test\nКанал: @partner_one", mid=1):
        self.store.register_brief(parse_partner_references(text), text, -100100, mid, NOW - timedelta(minutes=1), self.config)
        self.store.execute("UPDATE partners SET channel_id=200 WHERE username='partner_one'")

    async def test_event_captures_immediately_without_waiting_for_history_cycle(self):
        await self.live.new_message(SimpleNamespace(message=message(20, "Test launch", views=42)))
        self.assertEqual(self.capture.calls[0][1], [20])
        self.assertEqual(self.store.rows("SELECT views FROM snapshots"), [{"views": 42}])
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.store.rows("SELECT last_message_id FROM partners")[0]["last_message_id"], 0)
        self.assertTrue(self.monitor.changed.is_set())

    async def test_history_captures_before_reading_the_next_message(self):
        capture = self.capture

        class OrderedClient(EventClient):
            async def iter_messages(self, entity, **options):
                yield message(7, "Test launch")
                self.assertion = len(capture.calls)
                yield message(8, "News")

        client = OrderedClient()
        monitor = Monitor(self.config, self.store, client, capture)
        partner = self.store.rows("SELECT * FROM partners")[0]
        await monitor.discover(partner, client.partner)
        self.assertEqual(client.assertion, 1)

    async def test_concurrent_event_and_history_create_one_initial_screenshot(self):
        started, release = asyncio.Event(), asyncio.Event()
        original_capture = self.capture.capture

        async def slow_capture(*args):
            started.set()
            await release.wait()
            return await original_capture(*args)

        self.capture.capture = slow_capture
        self.client.histories[200] = [message(20, "Test launch")]
        event_job = asyncio.create_task(self.live.new_message(SimpleNamespace(message=self.client.histories[200][0])))
        await asyncio.wait_for(started.wait(), 1)
        history_job = asyncio.create_task(self.monitor.discover(self.store.rows("SELECT * FROM partners")[0], self.client.partner))
        release.set()
        await asyncio.wait_for(asyncio.gather(event_job, history_job), 1)
        self.assertEqual(len(self.capture.calls), 1)
        self.assertEqual(len(self.store.rows("SELECT * FROM snapshots")), 1)

    async def test_live_post_does_not_skip_older_history_messages(self):
        await self.live.new_message(SimpleNamespace(message=message(20, "Test newest")))
        self.client.histories[200] = [message(8, "Test older"), message(20, "Test newest")]
        await self.monitor.discover(self.store.rows("SELECT * FROM partners")[0], self.client.partner)
        self.assertEqual({p["message_id"] for p in self.store.rows("SELECT * FROM posts")}, {8, 20})
        self.assertEqual(len(self.capture.calls), 2)

    async def test_irrelevant_channel_and_nonmatching_ad_are_ignored(self):
        await self.live.new_message(SimpleNamespace(message=message(1, "Test", peer=999)))
        await self.live.new_message(SimpleNamespace(message=message(2, "Different advertisement")))
        self.assertEqual(self.capture.calls, [])

    async def test_needs_review_does_not_capture_even_if_text_matches(self):
        self.store.execute("UPDATE placements SET status='needs_review'")
        await self.live.new_message(SimpleNamespace(message=message(1, "Test")))
        self.assertEqual(self.capture.calls, [])

    async def test_direct_post_does_not_need_brand_and_other_posts_do_not_match(self):
        self.store.execute("DELETE FROM placements")
        self.add_brief("https://t.me/partner_one/7", mid=2)
        await self.live.new_message(SimpleNamespace(message=message(8, "Test")))
        await self.live.new_message(SimpleNamespace(message=message(7, "No configured words")))
        self.assertEqual([c[1] for c in self.capture.calls], [[7]])

    async def test_album_waits_for_members_and_captures_once_with_first_counter(self):
        members = [message(10, "Test", grouped_id=9, views=51), message(11, "", grouped_id=9, views=51)]
        for item in members:
            await self.live.new_message(SimpleNamespace(message=item))
        self.assertEqual(self.capture.calls, [])
        await self.live.album(SimpleNamespace(messages=members))
        self.assertEqual([c[1] for c in self.capture.calls], [[10, 11]])
        self.assertEqual(self.store.rows("SELECT views FROM snapshots"), [{"views": 51}])

    async def test_source_group_event_wakes_ingestion(self):
        self.monitor.source_peer_id = -1000000000100
        await self.live.new_message(SimpleNamespace(message=message(1, "New partner", peer=100)))
        self.assertTrue(self.monitor.wake.is_set())
        self.assertEqual(self.capture.calls, [])

    async def test_non_member_channel_difference_saves_pts_and_respects_server_timeout(self):
        self.client.differences = [types.updates.ChannelDifference(pts=11, new_messages=[message(7, "Test")],
            other_updates=[], chats=[], users=[], final=True, timeout=17)]
        entity = channel()
        entity.access_hash = 123
        entity.left = True
        observed_delay = []

        async def stop_after_response(delay):
            observed_delay.append(delay)
            raise asyncio.CancelledError()

        with patch("partner_monitor.realtime.asyncio.sleep", new=stop_after_response):
            with self.assertRaises(asyncio.CancelledError):
                await self.live.poll_channel(1, entity)
        self.assertEqual(observed_delay, [17])
        self.assertEqual(self.store.state("realtime_pts:200"), "11")
        self.assertIn(("difference", 200, 10, 100), self.client.calls)
        self.assertEqual(len(self.capture.calls), 1)

    async def test_short_poll_limit_is_ten_and_joined_channels_need_no_slot(self):
        wait = asyncio.Event()
        # Use a real async body, so cancellation leaves no unawaited coroutine.
        async def idle(*args):
            await wait.wait()
        self.live.poll_channel = idle
        partner = self.store.rows("SELECT * FROM partners")[0]
        for cid in range(300, 311):
            entity = channel(cid, f"partner_{cid}")
            entity.left = True
            entity.access_hash = 123
            self.live.register_channel(partner, entity)
        self.assertEqual(len(self.live.tasks), 10)
        self.assertNotIn(310, self.live.tasks)
        self.live.register_channel(partner, channel(999, "member_channel"))
        self.assertNotIn(999, self.live.tasks)

    async def test_failed_live_capture_is_retried_by_history_without_remeasuring_initial_views(self):
        self.capture.fail = True
        await self.live.new_message(SimpleNamespace(message=message(7, "Test", views=11)))
        self.assertEqual(len(self.capture.calls), 1)
        self.capture.fail = False
        self.client.histories[200] = [message(7, "Test", views=77)]
        self.monitor._attempted.clear()  # Beginning of the next history cycle.
        await self.monitor.measure(self.store.rows("SELECT * FROM partners")[0], self.client.partner)
        row = self.store.rows("SELECT * FROM snapshots")[0]
        self.assertEqual(row["views"], 11)
        self.assertEqual(row["capture_attempts"], 2)

    async def test_live_screenshot_publishes_report_without_waiting_for_tick(self):
        published = asyncio.Event()
        async def publish(*args):
            published.set()
        with patch("partner_monitor.engine.publish_reports", new=publish):
            worker = asyncio.create_task(report_loop(self.monitor))
            try:
                await self.live.new_message(SimpleNamespace(message=message(7, "Test")))
                await asyncio.wait_for(published.wait(), 1)
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

    async def test_close_waits_for_active_screenshot_handler_to_stop(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def blocked_capture(*args):
            started.set()
            await release.wait()
        self.capture.capture = blocked_capture
        job = asyncio.create_task(self.live.new_message(SimpleNamespace(message=message(7, "Test"))))
        await asyncio.wait_for(started.wait(), 1)
        await self.live.close()
        self.assertTrue(job.cancelled())
        self.assertEqual(self.client.handlers, [])
        self.assertEqual(self.monitor._capturing, set())

    async def test_expired_and_dismissed_placements_do_not_keep_a_live_lease(self):
        self.store.execute("UPDATE placements SET ends_at=?", ((NOW - timedelta(seconds=1)).isoformat(),))
        self.assertFalse(self.live.watching(1))
        self.store.execute("UPDATE placements SET ends_at=?,status='dismissed'", ((NOW + timedelta(days=1)).isoformat(),))
        self.assertFalse(self.live.watching(1))


class RunningServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.MonitorTests.asyncSetUp(self)

    async def asyncTearDown(self):
        await fixtures.MonitorTests.asyncTearDown(self)

    async def test_service_captures_live_post_during_the_five_minute_history_wait(self):
        class ConnectedClient(EventClient):
            async def connect(self):
                pass
            async def is_user_authorized(self):
                return True
            async def disconnect(self):
                self.disconnected = True
        class OpenCapture(FakeCapture):
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
        client = ConnectedClient([message(1, "Бренд: BRB\n@partner_one", peer=100)])
        capture = OpenCapture()
        first_report, second_report = asyncio.Event(), asyncio.Event()
        calls = []
        async def publish(*args):
            calls.append(1)
            (first_report if len(calls) == 1 else second_report).set()
        with patch("partner_monitor.engine.client_for", return_value=client), \
             patch("partner_monitor.engine.Screenshotter", return_value=capture), \
             patch("partner_monitor.engine.publish_reports", new=publish):
            job = asyncio.create_task(run(self.config))
            try:
                await asyncio.wait_for(first_report.wait(), 3)
                handler = next(h for h in client.handlers if h.__name__ == "new_message")
                await handler(SimpleNamespace(message=message(7, "BRB launch", views=123)))
                await asyncio.wait_for(second_report.wait(), 1)
                self.assertEqual([c[1] for c in capture.calls], [[7]])
                self.assertEqual(len([c for c in client.calls if c[:2] == ("history", 200)]), 1)
            finally:
                job.cancel()
                await asyncio.gather(job, return_exceptions=True)
        self.assertEqual(client.handlers, [])
        self.assertTrue(client.disconnected)


class DifferenceTests(unittest.TestCase):
    def test_final_empty_nonfinal_and_too_long_responses(self):
        self.assertEqual(difference_batch(types.updates.ChannelDifferenceEmpty(pts=11, final=True, timeout=9))[1:], (11, 9, False))
        self.assertEqual(difference_batch(types.updates.ChannelDifference(pts=12, new_messages=[], other_updates=[], chats=[], users=[], final=False, timeout=9))[1:], (12, 0, False))
        self.assertEqual(difference_batch(types.updates.ChannelDifferenceTooLong(dialog=SimpleNamespace(pts=13), messages=[], chats=[], users=[], final=True))[1:], (13, 1, True))


class TelegramAccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_flood_wait_pauses_new_requests_across_all_modes(self):
        raw = FakeClient()
        raw.entities["flood"] = errors.FloodWaitError(request=None, capture=17)
        client = TelegramAccess(raw)
        with self.assertRaises(errors.FloodWaitError):
            await client.get_entity("flood")
        entered, release = asyncio.Event(), asyncio.Event()
        async def controlled_sleep(seconds):
            self.assertGreater(seconds, 17)
            entered.set()
            await release.wait()
        with patch("partner_monitor.telegram_access.asyncio.sleep", new=controlled_sleep):
            job = asyncio.create_task(client.get_entity("partner_one"))
            await asyncio.wait_for(entered.wait(), 1)
            self.assertNotIn(("entity", "partner_one"), raw.calls)
            client.blocked_until = 0
            release.set()
            await asyncio.wait_for(job, 1)
        self.assertIn(("entity", "partner_one"), raw.calls)
