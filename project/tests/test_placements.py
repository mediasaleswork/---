from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock, patch
import unittest

from telethon import types

from partner_monitor.engine import Monitor
from partner_monitor.parsing import parse_partner_references
import test_engine as fixtures
from test_engine import FakeClient, FakeCapture, NOW, message, channel, attachment_message, docx_bytes


class PlacementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.MonitorTests.asyncSetUp(self)
        self.config = replace(self.config, keywords=[], domains=[])

    async def asyncTearDown(self):
        await fixtures.MonitorTests.asyncTearDown(self)

    async def tick(self, client, capture=None):
        with patch("partner_monitor.engine.asyncio.sleep", new=AsyncMock()):
            await Monitor(self.config, self.store, client, capture or FakeCapture()).tick()

    async def test_new_brand_is_extracted_from_group_and_retention_saved(self):
        text = "Название: Новый продукт\nТип: видео\nКанал: @partner_one\nДлительность: 24 часа"
        client = FakeClient([message(1, text, peer=100)], [message(7, "Новый продукт: подробности", views=88), message(8, "BRB")])
        await self.tick(client)
        self.assertEqual([p["message_id"] for p in self.store.rows("SELECT * FROM posts")], [7])
        brief = self.store.rows("SELECT * FROM placements")[0]
        self.assertEqual(brief["retention_hours"], 24)
        self.assertEqual(brief["status"], "found")
        self.assertEqual(brief["source_chat_id"], "-1000000000100")

    async def test_ambiguous_channels_do_not_resolve_or_read_either_channel(self):
        client = FakeClient([message(1, "Бренд: Test\n@partner_one @partner_two", peer=100)])
        await self.tick(client)
        self.assertEqual({p["status"] for p in self.store.rows("SELECT * FROM placements")}, {"needs_review"})
        self.assertEqual(client.calls, [("entity", "@work_group"), ("history", 100, client.calls[1][2])])

    async def test_different_briefs_on_one_channel_keep_separate_matching_and_terms(self):
        client = FakeClient([message(1, "Бренд: Alpha\n@partner_one\nДлительность: 12 часов", peer=100),
                             message(2, "Бренд: Beta\n@partner_one\nДлительность: 48 часов", peer=100)],
                            [message(10, "Alpha launched"), message(11, "Beta launched")])
        await self.tick(client)
        rows = self.store.rows("SELECT b.name,p.message_id FROM placements b JOIN placement_posts x ON x.placement_id=b.id JOIN posts p ON p.id=x.post_id ORDER BY b.id")
        self.assertEqual(rows, [{"name": "Alpha", "message_id": 10}, {"name": "Beta", "message_id": 11}])
        await self.tick(client)
        self.assertEqual(len(self.store.rows("SELECT * FROM placement_posts")), 2)

    async def test_post_outside_window_is_not_matched_and_backlog_is_fully_drained(self):
        self.config = replace(self.config, batch_size=1)
        start, end = NOW - timedelta(days=2), NOW - timedelta(days=1)
        text = f"Бренд: Test\n@partner_one\nНачало: {start.isoformat()}\nОкончание: {end.isoformat()}"
        client = FakeClient([message(1, text, peer=100)], [message(2, "News", date=start + timedelta(hours=1)),
            message(3, "Test", date=start + timedelta(hours=2)), message(4, "Test", date=NOW)])
        await self.tick(client)
        self.assertEqual(self.store.rows("SELECT status FROM placements")[0]["status"], "watching")
        await self.tick(client)
        await self.tick(client)
        self.assertEqual([p["message_id"] for p in self.store.rows("SELECT * FROM posts")], [3])
        self.assertEqual(self.store.rows("SELECT scan_complete FROM placements")[0]["scan_complete"], 1)
        before = len([c for c in client.calls if c[:2] == ("history", 200)])
        await self.tick(client)
        self.assertEqual(len([c for c in client.calls if c[:2] == ("history", 200)]), before)

    async def test_final_retention_measurement_and_missing_post_check(self):
        client = FakeClient([message(1, "Бренд: Test\n@partner_one\nДлительность: 24 часа", peer=100)], [message(7, "Test", views=20)])
        capture = FakeCapture()
        await self.tick(client, capture)
        self.store.execute("UPDATE posts SET published_at=?,first_seen_at=?", ((NOW - timedelta(hours=25)).isoformat(), (NOW - timedelta(hours=24)).isoformat()))
        client.histories[200][0].views = 70
        await self.tick(client, capture)
        rows = self.store.rows("SELECT * FROM snapshots WHERE due_key LIKE 'placement-%'")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["views"], 70)
        self.assertEqual(rows[0]["due_key"], "placement-1-24h")
        # A different post that is still inside its retention period is read
        # each cycle even if its initial screenshot already succeeded.
        self.store.execute("UPDATE posts SET published_at=?,first_seen_at=?", ((NOW - timedelta(hours=1)).isoformat(), (NOW - timedelta(minutes=30)).isoformat()))
        self.store.execute("DELETE FROM snapshots WHERE due_key LIKE 'placement-%'")
        client.histories[200].clear()
        await self.tick(client, capture)
        self.assertEqual(self.store.rows("SELECT status FROM posts")[0]["status"], "unavailable")

    async def test_private_member_keeps_counters_when_card_renderer_is_unavailable(self):
        client = FakeClient([message(1, "https://t.me/+existingInvite", peer=100)], [message(7, "Test", views=34)])
        client.invites["existingInvite"] = types.ChatInviteAlready(chat=channel(username=None))
        # Use a direct private post so no brand cue is needed.
        client.histories[100][0].message = "https://t.me/c/200/7"
        capture = FakeCapture()
        await self.tick(client, capture)
        self.assertEqual(self.store.rows("SELECT url FROM posts")[0]["url"], "https://t.me/c/200/7")
        snapshot = self.store.rows("SELECT * FROM snapshots")[0]
        self.assertEqual(snapshot["views"], 34)
        self.assertEqual(snapshot["screenshot_status"], "failed")
        self.assertIsNone(snapshot["screenshot_path"])
        self.assertEqual(capture.calls, [])

    async def test_invite_of_existing_member_reads_history_without_importing_invite(self):
        client = FakeClient([message(1, "Бренд: Test\nhttps://t.me/+existingInvite", peer=100)], [message(7, "Test", views=34)])
        client.invites["existingInvite"] = types.ChatInviteAlready(chat=channel(username=None))
        await self.tick(client)
        self.assertEqual(self.store.rows("SELECT url FROM posts")[0]["url"], "https://t.me/c/200/7")
        self.assertIn(("check_invite", "existingInvite"), client.calls)

    async def test_resolving_one_of_ambiguous_briefs_does_not_activate_the_others(self):
        text = "Бренд: Test\n@partner_one @partner_two"
        client = FakeClient([message(1, text, peer=100)], [message(7, "Test", views=34)])
        await self.tick(client)
        self.store.resolve_placement(1, start=(NOW - timedelta(hours=1)).isoformat(), end=(NOW + timedelta(hours=1)).isoformat())
        await self.tick(client)
        self.assertEqual([r["status"] for r in self.store.rows("SELECT status FROM placements ORDER BY id")], ["found", "needs_review"])
        self.assertFalse(any(c == ("entity", "partner_two") for c in client.calls))

    async def test_no_brand_or_direct_link_requires_review_with_empty_global_defaults(self):
        client = FakeClient([message(1, "Тип: видео\n@partner_one\nДлительность: 24 часа", peer=100)])
        await self.tick(client)
        self.assertEqual(self.store.rows("SELECT status FROM placements")[0]["status"], "needs_review")
        self.assertEqual(len([c for c in client.calls if c[0] == "entity"]), 1)

    async def test_attachment_conditions_use_the_same_parser(self):
        data = docx_bytes("Название: OtherBrand\nТип: пост\nКанал: @partner_one\nДлительность: 48 часов")
        client = FakeClient([attachment_message(1, data)], [message(8, "OtherBrand")])
        client.attachments[1] = data
        await self.tick(client)
        self.assertEqual(self.store.rows("SELECT name,retention_hours FROM placements"), [{"name": "OtherBrand", "retention_hours": 48}])
        self.assertEqual(len(self.store.rows("SELECT * FROM posts")), 1)

    async def test_report_places_brief_terms_and_result_next_to_partner(self):
        from openpyxl import load_workbook
        from partner_monitor.reports import export_xlsx
        text = "Название: Новый продукт\nТип: видео\nКанал: @partner_one\nДлительность: 24 часа\nУсловия: без удаления"
        await self.tick(FakeClient([message(1, text, peer=100)], [message(7, "Новый продукт", views=88)]))
        book = load_workbook(export_xlsx(*self.store.dump(), self.root / "report.xlsx"))
        try:
            sheet = book["Партнёры"]
            row = dict(zip([c.value for c in sheet[1]], [c.value for c in sheet[2]]))
            self.assertEqual(row["Реклама"], "Новый продукт")
            self.assertEqual(row["Тип рекламы"], "видео")
            self.assertEqual(row["Длительность, ч"], 24)
            self.assertEqual(row["Условия"], "без удаления")
            self.assertEqual(row["Найденные посты"], "https://t.me/partner_one/7")
            self.assertEqual(row["Последние просмотры"], "88")
            self.assertEqual(row["Исходное сообщение"], "https://t.me/c/100/1")
            self.assertEqual(row["Контроль длительности"], "Наблюдение")
        finally:
            book.close()
