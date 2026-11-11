"""Natural forwarded materials require neither fields nor a preview approval."""
from dataclasses import replace
from datetime import timedelta
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from partner_monitor.intake import ConversationIntake
from partner_monitor.message_content import message_content
from partner_monitor.engine import Monitor
from partner_monitor.campaigns import CampaignWorker
from test_engine import FakeClient, FakeCapture, NOW, message
from test_conversation import image_message
import test_conversation as conversation
from test_campaigns import decision


class ForwardedIntakeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await conversation.ConversationTests.asyncSetUp(self)
        self.config = replace(self.config, forwarded_intake=True, campaign_matching=True,
                              allowed_channels=("partner_one", "partner_two", "c:300"), channel_plans=())
        self.intake = ConversationIntake(self.store, self.config)

    async def asyncTearDown(self):
        await conversation.ConversationTests.asyncTearDown(self)

    def consume(self, m):
        self.intake.consume(-1000000000100, m, message_content(m), [])

    async def test_forwarded_photo_without_template_or_ok_watches_all_selected_channels(self):
        source = image_message(1, "Банк объявил запуск новой лизинговой услуги для предпринимателей.")
        source.fwd_from = SimpleNamespace(from_id=None)
        self.consume(source)
        self.assertEqual(len(self.store.rows("SELECT * FROM placements")), 3)
        self.assertEqual({p["status"] for p in self.store.rows("SELECT * FROM placements")}, {"watching"})
        self.assertTrue(all(json.loads(p["direct_ids"]) == [] for p in self.store.rows("SELECT * FROM placements")))
        self.assertEqual(self.store.rows("SELECT status FROM intake_drafts")[0]["status"], "approved")

    async def test_plain_conversation_is_logged_without_a_campaign(self):
        self.consume(message(1, "Коллеги, когда выйдет материал?", peer=100))
        self.assertFalse(self.store.rows("SELECT * FROM placements"))
        self.assertEqual(self.store.rows("SELECT kind FROM source_messages")[0]["kind"], "ignored")

    async def test_captionless_picture_is_a_material_and_forwarded_text_is_supported(self):
        self.consume(image_message(1))
        text = message(2, "Новое предложение для бизнеса", peer=100)
        text.fwd_from = SimpleNamespace(from_id=None)
        self.consume(text)
        self.assertEqual(len(self.store.rows("SELECT * FROM intake_drafts")), 2)

    async def test_advertisement_dates_and_mentions_are_not_configuration(self):
        self.consume(image_message(1, "Клиент: Любой предприниматель\nДата: 01.01.2020\nКанал: @outside_news\nИскать: деньги"))
        placements = self.store.rows("SELECT * FROM placements")
        self.assertEqual(len(placements), 3)
        self.assertEqual({p["client_name"] for p in placements}, {self.config.default_client})
        from partner_monitor.intake import publication_day_start
        self.assertEqual({p["starts_at"] for p in placements}, {publication_day_start(NOW, self.config.timezone)})
        self.assertTrue(all(not p["review_reason"] for p in placements))

    async def test_repeated_same_material_does_not_create_competing_campaigns(self):
        self.consume(image_message(1, "Услуга лизинга", photo=900))
        self.consume(image_message(2, "Услуга лизинга", photo=900))
        self.assertEqual(len(self.store.rows("SELECT * FROM intake_drafts")), 1)
        self.assertEqual(len(self.store.rows("SELECT * FROM placements")), 3)
        self.assertEqual(self.store.rows("SELECT kind FROM source_messages WHERE message_id=2")[0]["kind"], "duplicate")

    async def test_empty_folder_does_not_discard_source_material(self):
        self.intake.config = replace(self.config, allowed_channels=())
        with self.assertRaises(OSError):
            self.consume(image_message(1))
        self.assertFalse(self.store.rows("SELECT * FROM source_messages"))

    async def test_offline_matching_does_not_use_shared_footer_or_reused_bank_logo(self):
        from partner_monitor.fallback import offline_match
        footer = "Следите за новостями нашего банка, узнавайте о новых предложениях и возможностях для бизнеса."
        self.consume(image_message(1, "Лизинг оборудования на 12 месяцев.\n\n" + footer, photo=900))
        placement = self.store.rows("SELECT * FROM placements")[0]
        self.assertFalse(offline_match([image_message(7, "Документальный фильм.\n\n" + footer, photo=900, peer=200)], placement).matched)
        self.assertFalse(offline_match([message(8, "Совсем другая услуга.\n\n" + footer)], placement).matched)
        self.assertTrue(offline_match([image_message(9, placement["source_text"], photo=900, peer=200)], placement).matched)
        self.assertFalse(offline_match([image_message(10, placement["source_text"], photo=901, peer=200)], placement).matched)

    async def test_new_folder_channel_receives_existing_active_material(self):
        self.consume(image_message(1, "Услуга лизинга"))
        self.intake.config = replace(self.config, allowed_channels=(*self.config.allowed_channels, "new_channel"))
        self.intake.attach_folder_channels(NOW)
        self.intake.attach_folder_channels(NOW)
        self.assertEqual(len(self.store.rows("SELECT * FROM placements")), 4)

    async def test_live_matching_has_no_approval_and_only_captures_channel(self):
        config = replace(self.config, allowed_channels=("partner_one",))
        source = message(1, "Новая услуга лизинга с особыми условиями для малого бизнеса", peer=100)
        source.fwd_from = SimpleNamespace(from_id=None)
        client = FakeClient([source], [message(7, "Предприниматели могут оформить оборудование в рассрочку")])
        capture = FakeCapture()
        monitor = Monitor(config, self.store, client, capture)
        judge = Mock()
        judge.compare.return_value = decision()
        monitor.campaign_worker = CampaignWorker(monitor, judge)
        with patch("partner_monitor.engine.asyncio.sleep", new=AsyncMock()):
            await monitor.tick()
        self.assertEqual(len(self.store.rows("SELECT * FROM posts")), 1)
        self.assertEqual(len(capture.calls), 1)
        self.assertEqual(self.store.rows("SELECT status FROM intake_drafts")[0]["status"], "approved")

    async def test_source_since_limits_initial_history(self):
        start = NOW - timedelta(minutes=2)
        config = replace(self.config, source_since=start.isoformat(), allowed_channels=("partner_one",))
        old = image_message(1, "Старый материал")
        old.date = NOW - timedelta(days=1)
        client = FakeClient([old, image_message(2, "Новый материал")])
        monitor = Monitor(config, self.store, client, FakeCapture())
        await monitor.ingest_group()
        self.assertEqual([r["anchor_id"] for r in self.store.rows("SELECT * FROM intake_drafts")], [2])

    async def test_wrong_source_title_fails_before_history(self):
        client = FakeClient([image_message(1)])
        monitor = Monitor(replace(self.config, source_title="Different group"), self.store, client, FakeCapture())
        with self.assertRaises(ValueError):
            await monitor.ingest_group()
        self.assertFalse([c for c in client.calls if c[0] == "history"])

    async def test_earlier_start_replays_history_without_duplicate_materials(self):
        recent_start = NOW - timedelta(minutes=1)
        config = replace(self.config, source_since=recent_start.isoformat(), allowed_channels=("partner_one",))
        early = image_message(1, "Ранний материал")
        early.date = NOW - timedelta(hours=2)
        client = FakeClient([early, image_message(2, "Свежий материал")])
        monitor = Monitor(config, self.store, client, FakeCapture())
        await monitor.ingest_group()
        self.assertEqual([r["anchor_id"] for r in self.store.rows("SELECT * FROM intake_drafts")], [2])
        replay = Monitor(replace(config, source_since=(NOW - timedelta(hours=3)).isoformat()), self.store, client, FakeCapture())
        await replay.ingest_group()
        await replay.ingest_group()
        self.assertEqual(sorted(r["anchor_id"] for r in self.store.rows("SELECT * FROM intake_drafts")), [1, 2])
        self.assertEqual(len(self.store.rows("SELECT * FROM source_messages")), 2)
        self.assertEqual(len(self.store.rows("SELECT * FROM placements")), 2)

    async def test_already_published_post_before_forward_is_found_and_not_duplicated(self):
        config = replace(self.config, allowed_channels=("partner_one",))
        self.intake.config = config
        source = image_message(1, "Ролик об истории бизнеса")
        self.consume(source)
        earlier = image_message(7, source.message, peer=200)
        earlier.date = NOW - timedelta(minutes=30)
        from partner_monitor.intake import publication_day_start
        if earlier.date.isoformat() < publication_day_start(NOW, config.timezone):
            earlier.date = NOW
        client = FakeClient([], [earlier])
        capture = FakeCapture()
        monitor = Monitor(config, self.store, client, capture)
        with patch("partner_monitor.engine.asyncio.sleep", new=AsyncMock()):
            await monitor.tick()
            await monitor.tick()
        self.assertEqual(len(self.store.rows("SELECT * FROM posts")), 1)
        self.assertEqual(len(capture.calls), 1)
