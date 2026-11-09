"""Offline integration checks; judge responses are mocked, not live AI accuracy."""
import asyncio
from dataclasses import replace
from datetime import timedelta
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image

from partner_monitor.campaigns import (CampaignAPIError, CampaignWorker, LimitedBuffer,
    OpenAICampaignJudge, GeminiCampaignJudge, candidate_signature, enqueue, image_input, source_signature, validate_decision)
from partner_monitor.config import load_config
from partner_monitor.engine import Monitor
from partner_monitor.storage import Store
import test_conversation as conversation
from test_conversation import image_message
from test_engine import FakeCapture, FakeClient, NOW, message


def decision(pid=1, kind="same_campaign", **changes):
    value = {"decision": kind, "campaign_id": pid, "confidence": .98,
             "brand_match": True, "subject_match": True, "specific_detail_match": True,
             "shared_facts": ["Компания Alpha Lease начинает работу", "Оборудование можно приобрести в рассрочку"],
             "conflicts": [], "reason": "Тот же запуск лизинговой компании и предложение оборудования в рассрочку"}
    value.update(changes)
    return value


def png(color):
    stream = io.BytesIO()
    Image.new("RGB", (220, 160), color).save(stream, "PNG")
    return stream.getvalue()


class ImageClient(FakeClient):
    async def get_entity(self, key):
        if key == -1000000000100:
            return self.group
        return await super().get_entity(key)

    async def download_media(self, msg, file, thumb=None):
        self.calls.append(("image", msg.id, thumb))
        file.write(self.attachments[msg.id])
        return file


class CampaignIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await conversation.ConversationTests.asyncSetUp(self)
        self.config = replace(self.config, campaign_matching=True)
        self.capture = FakeCapture()

    async def asyncTearDown(self):
        await conversation.ConversationTests.asyncTearDown(self)

    def consume(self, msg):
        conversation.ConversationTests.consume(self, msg)

    def prepare(self, *, source_image=False, candidates=None):
        text = "Название: Alpha Lease\nКанал: @partner_one\nТекст рекламы: Компания Alpha Lease начала работу. Оборудование доступно в рассрочку."
        source = image_message(1, text) if source_image else message(1, text, peer=100)
        self.consume(source)
        self.consume(message(2, "Превью", peer=100))
        self.consume(message(3, "ок", peer=100))
        self.client = ImageClient([source], candidates or [message(7, "Эндиликда янги лизинг компанияси орқали техника учун бўлиб тўлаш мумкин.")])
        self.client.attachments[1] = png("red")
        self.client.attachments[7] = png("blue")
        self.monitor = Monitor(self.config, self.store, self.client, self.capture)
        self.judge = Mock()
        self.judge.compare.return_value = decision()
        self.monitor.campaign_worker = CampaignWorker(self.monitor, self.judge)

    async def tick(self):
        with patch("partner_monitor.engine.asyncio.sleep", new=AsyncMock()):
            await self.monitor.tick()

    async def test_rewritten_caption_and_different_picture_are_sent_and_attributed(self):
        self.prepare(source_image=True, candidates=[image_message(7, "Лизинг орқали ускуна олиш имконияти пайдо бўлди!", photo=1900, peer=200)])
        await self.tick()
        self.assertEqual([p["message_id"] for p in self.store.rows("SELECT * FROM posts")], [7])
        self.assertEqual(len(self.store.rows("SELECT * FROM snapshots")), 1)
        payload = self.judge.compare.call_args.args[0]
        images = [c for c in payload if c["type"] == "input_image"]
        self.assertEqual(len(images), 2)
        self.assertNotEqual(images[0]["image_url"], images[1]["image_url"])
        self.assertNotIn("access_hash", json.dumps(payload))
        self.assertNotIn("source_chat_id", json.dumps(payload))
        self.assertEqual(self.store.rows("SELECT due_key FROM measurement_jobs ORDER BY due_at")[-1]["due_key"], "48h")

    async def test_same_brand_different_product_rejected_even_if_old_keyword_matches(self):
        self.prepare(candidates=[message(7, "Alpha Lease: фильм об истории предпринимательства")])
        self.judge.compare.return_value = decision(None, "different_campaign", subject_match=False,
            specific_detail_match=False, shared_facts=["Один бренд"], conflicts=["Фильм вместо услуги лизинга"])
        await self.tick()
        self.assertFalse(self.store.rows("SELECT * FROM posts"))
        self.assertEqual(self.store.rows("SELECT status FROM campaign_checks")[0]["status"], "no_match")

    async def test_uncertain_candidate_does_not_count_or_capture(self):
        self.prepare()
        self.judge.compare.return_value = decision(None, "uncertain", confidence=.6)
        await self.tick()
        self.assertFalse(self.store.rows("SELECT * FROM posts"))
        self.assertFalse(self.capture.calls)
        self.assertEqual(self.store.rows("SELECT status FROM campaign_checks")[0]["status"], "review")

    async def test_low_confidence_or_brand_only_not_accepted(self):
        for changes in ({"confidence": .92}, {"specific_detail_match": False}, {"subject_match": False}, {"shared_facts": ["Один бренд"]}, {"conflicts": ["Другая ставка"]}):
            with self.subTest(changes=changes):
                self.assertEqual(validate_decision(decision(**changes), [1]), "review")

    async def test_unknown_campaign_and_malformed_response_not_accepted(self):
        self.prepare()
        self.judge.compare.return_value = decision(999)
        await self.tick()
        self.assertFalse(self.store.rows("SELECT * FROM posts"))
        self.assertEqual(self.store.rows("SELECT status FROM campaign_checks")[0]["status"], "review")

    async def test_restart_preserves_verdict_and_does_not_call_again(self):
        self.prepare()
        await self.tick()
        self.store.__exit__(None, None, None)
        self.store = Store(self.root / "monitor.sqlite3")
        self.monitor = Monitor(self.config, self.store, self.client, self.capture)
        self.monitor.campaign_worker = CampaignWorker(self.monitor, self.judge)
        await self.tick()
        self.assertEqual(self.judge.compare.call_count, 1)
        self.assertEqual(len(self.store.rows("SELECT * FROM posts")), 1)
        self.assertEqual(len(self.capture.calls), 1)

    async def test_failure_is_durable_and_retry_uses_same_job(self):
        self.prepare()
        self.judge.compare.side_effect = CampaignAPIError("http_503")
        await self.tick()
        self.assertEqual(self.store.rows("SELECT status,attempts FROM campaign_checks")[0], {"status": "error", "attempts": 1})
        self.assertFalse(self.store.rows("SELECT * FROM posts"))
        self.store.execute("UPDATE campaign_checks SET next_try_at=?", ((NOW - timedelta(hours=1)).isoformat(),))
        self.judge.compare.side_effect = None
        await self.tick()
        self.assertEqual(len(self.store.rows("SELECT * FROM campaign_checks")), 1)
        self.assertEqual(self.store.rows("SELECT status,attempts FROM campaign_checks")[0], {"status": "match", "attempts": 2})

    async def test_daily_budget_persists_and_pending_not_lost_by_cursor(self):
        self.prepare(candidates=[message(7, "Вариант 1"), message(8, "Вариант 2")])
        self.config = replace(self.config, campaign_daily_limit=1)
        self.monitor.config = self.config
        self.monitor.campaign_worker.config = self.config
        await self.tick()
        self.assertEqual(self.judge.compare.call_count, 1)
        self.assertEqual(self.store.rows("SELECT COUNT(*) AS n FROM campaign_checks WHERE status='pending'")[0]["n"], 1)
        self.assertEqual(self.store.rows("SELECT last_message_id FROM partners")[0]["last_message_id"], 8)
        await self.tick()
        self.assertEqual(self.judge.compare.call_count, 1)
        self.assertEqual(len(self.store.rows("SELECT * FROM posts")), 1)

    async def test_small_budget_prioritizes_likely_campaign_over_unrelated_news(self):
        self.prepare(candidates=[message(7, "Сегодня ожидается хорошая погода"), message(8, "Реклама: Компания Alpha Lease предлагает оборудование в рассрочку")])
        self.monitor.config = replace(self.config, campaign_daily_limit=1)
        self.monitor.campaign_worker.config = self.monitor.config
        await self.tick()
        self.assertEqual([p["message_id"] for p in self.store.rows("SELECT * FROM posts")], [8])
        self.assertEqual(self.judge.compare.call_count, 1)

    async def test_no_api_key_keeps_queue_and_does_not_spend_budget(self):
        self.prepare()
        self.monitor.campaign_worker = CampaignWorker(self.monitor)
        with patch.dict(os.environ, {}, clear=True), patch("partner_monitor.campaigns.requests.post") as http:
            await self.tick()
            http.assert_not_called()
        self.assertEqual(self.store.rows("SELECT status,attempts,reason FROM campaign_checks")[0],
                         {"status": "pending", "attempts": 0, "reason": "missing_api_key"})
        self.assertFalse(self.store.rows("SELECT * FROM posts"))

    async def test_total_test_limit_survives_new_day_budget_reset(self):
        self.prepare(candidates=[message(7, "Материал один"), message(8, "Материал два")])
        self.monitor.config = replace(self.config, campaign_total_limit=1)
        self.monitor.campaign_worker.config = self.monitor.config
        await self.tick()
        self.store.execute("DELETE FROM state WHERE key LIKE 'campaign_api_attempts:%'")
        await self.tick()
        self.assertEqual(self.judge.compare.call_count, 1)
        self.assertEqual(self.store.state("campaign_api_attempts_total"), "1")
        self.assertEqual(self.store.rows("SELECT COUNT(*) AS n FROM campaign_checks WHERE status='pending'")[0]["n"], 1)

    async def test_quota_error_pauses_whole_queue_and_never_counts_or_switches_provider(self):
        self.prepare(candidates=[message(7, "Первый"), message(8, "Второй")])
        self.judge.compare.side_effect = CampaignAPIError("http_429")
        await self.tick()
        self.assertEqual(self.judge.compare.call_count, 1)
        self.assertEqual({r["status"] for r in self.store.rows("SELECT status FROM campaign_checks")}, {"error", "pending"})
        self.assertFalse(self.store.rows("SELECT * FROM posts"))
        await self.tick()
        self.assertEqual(self.judge.compare.call_count, 1)
        self.assertTrue(self.store.state("campaign_quota_retry_at"))

    async def test_changed_reference_invalidates_old_pending_job(self):
        self.prepare()
        with patch.dict(os.environ, {}, clear=True):
            worker = self.monitor.campaign_worker
            self.monitor.campaign_worker = CampaignWorker(self.monitor)
            await self.tick()
        self.store.execute("UPDATE placements SET source_text='Новая версия материала'")
        self.monitor.campaign_worker = worker
        await worker.process()
        self.judge.compare.assert_not_called()
        self.assertEqual(self.store.rows("SELECT status FROM campaign_checks")[0]["status"], "obsolete")

    async def test_channel_scope_and_source_scope_checked_before_media_or_ai(self):
        self.prepare()
        self.monitor.campaign_worker = CampaignWorker(self.monitor)
        await self.tick()
        self.monitor.campaign_worker = CampaignWorker(self.monitor, self.judge)
        self.monitor.source_peer_id = -12345
        await self.monitor.campaign_worker.process()
        self.judge.compare.assert_not_called()
        self.assertEqual(self.store.rows("SELECT status FROM campaign_checks")[0]["status"], "obsolete")

    async def test_no_source_identity_yet_does_not_discard_pending(self):
        self.prepare()
        partner = self.store.rows("SELECT * FROM partners")[0]
        enqueue(self.store, self.config, partner, self.client.histories[200], self.store.rows("SELECT * FROM placements"))
        await self.monitor.campaign_worker.process()
        self.assertEqual(self.store.rows("SELECT status FROM campaign_checks")[0]["status"], "pending")

    async def test_direct_link_still_counts_without_ai(self):
        self.prepare()
        self.consume(message(4, "https://t.me/partner_one/7", peer=100))
        await self.tick()
        self.judge.compare.assert_not_called()
        self.assertEqual(len(self.store.rows("SELECT * FROM posts")), 1)

    async def test_two_campaigns_choose_one_without_using_brand_as_attribution(self):
        self.prepare()
        self.consume(message(4, "Название: Alpha Film\nКанал: @partner_one\nТекст рекламы: Фильм об истории предпринимательства.", peer=100))
        self.consume(message(5, "Превью", peer=100))
        self.consume(message(6, "ок", peer=100))
        self.judge.compare.return_value = decision(2)
        await self.tick()
        self.assertEqual(self.store.rows("SELECT placement_id FROM placement_posts"), [{"placement_id": 2}])
        references = [part["text"] for part in self.judge.compare.call_args.args[0] if part["type"] == "input_text" and part["text"].startswith("REFERENCE")]
        self.assertEqual(len(references), 2)

    async def test_edit_during_comparison_does_not_commit_stale_verdict(self):
        self.prepare()
        self.judge.compare.side_effect = lambda content: (setattr(self.client.histories[200][0], "message", "Теперь совершенно другая публикация"), decision())[1]
        await self.tick()
        self.assertFalse(self.store.rows("SELECT * FROM posts"))
        self.assertEqual({j["status"] for j in self.store.rows("SELECT status FROM campaign_checks")}, {"obsolete", "pending"})

    async def test_transient_outage_keeps_retrying_after_three_failures(self):
        self.prepare()
        self.judge.compare.side_effect = CampaignAPIError("http_503")
        for _ in range(3):
            self.store.execute("UPDATE campaign_checks SET next_try_at=?", ((NOW - timedelta(hours=1)).isoformat(),))
            await self.tick()
        self.assertEqual(self.store.rows("SELECT status,attempts FROM campaign_checks")[0], {"status": "error", "attempts": 3})
        self.assertFalse(self.store.rows("SELECT * FROM posts"))

    async def test_quota_exhausted_exact_creative_is_captured_without_ai(self):
        self.prepare(source_image=True, candidates=[image_message(7, "Согласованный материал", photo=900, peer=200)])
        self.store.set_state("campaign_quota_retry_at", (NOW + timedelta(days=3650)).isoformat())
        await self.tick()
        self.judge.compare.assert_not_called()
        self.assertEqual(len(self.store.rows("SELECT * FROM posts")), 1)
        self.assertEqual(len(self.capture.calls), 1)

    async def test_quota_recovery_processes_waiting_changed_material(self):
        self.prepare()
        self.store.set_state("campaign_quota_retry_at", (NOW + timedelta(days=3650)).isoformat())
        await self.tick()
        self.judge.compare.assert_not_called()
        self.assertEqual(self.store.rows("SELECT status FROM campaign_checks")[0]["status"], "pending")
        self.store.set_state("campaign_quota_retry_at", "")
        await self.tick()
        self.assertEqual(len(self.store.rows("SELECT * FROM posts")), 1)

    async def test_service_messages_stay_local_and_no_group_screenshots(self):
        self.prepare(candidates=[])
        self.client.histories[200] = []
        self.client.histories[100] += [message(2, "Превью", peer=100), message(3, "ок", peer=100),
                                      message(4, "Когда будет готово?", peer=100)]
        await self.tick()
        self.judge.compare.assert_not_called()
        self.assertFalse(self.capture.calls)
        self.assertEqual(self.store.rows("SELECT COUNT(*) AS n FROM source_messages")[0]["n"], 4)

    async def test_live_other_channel_is_ignored_before_queue_or_ai(self):
        self.prepare()
        self.monitor.source_peer_id = -1000000000100
        await self.monitor.process_live([message(9, "Alpha Lease", peer=999)])
        self.assertFalse(self.store.rows("SELECT * FROM campaign_checks"))
        self.judge.compare.assert_not_called()

    async def test_updated_reference_uses_latest_image_not_old_anchor(self):
        self.prepare(source_image=True)
        corrected = image_message(4, "Обновлённый имидж", photo=990)
        from telethon import types
        corrected.reply_to = types.MessageReplyHeader(reply_to_msg_id=1)
        self.consume(corrected)
        self.consume(message(5, "Превью", peer=100))
        self.consume(message(6, "ок", peer=100))
        self.client.histories[100].append(corrected)
        self.client.attachments[4] = png("green")
        await self.tick()
        self.assertIn(("image", 4, -1), self.client.calls)
        self.assertNotIn(("image", 1, -1), self.client.calls)

    async def test_expired_scan_retains_pending_candidate_and_can_finish_later(self):
        self.prepare(candidates=[message(7, "Подходящий материал", date=NOW - timedelta(hours=2))])
        self.store.execute("UPDATE placements SET starts_at=?,ends_at=?", ((NOW - timedelta(hours=3)).isoformat(), (NOW - timedelta(hours=1)).isoformat()))
        self.monitor.campaign_worker = CampaignWorker(self.monitor)
        with patch.dict(os.environ, {}, clear=True):
            await self.tick()
        self.assertEqual(self.store.rows("SELECT status,scan_complete FROM placements")[0], {"status": "watching", "scan_complete": 1})
        self.monitor.campaign_worker = CampaignWorker(self.monitor, self.judge)
        await self.tick()
        self.assertEqual(len(self.store.rows("SELECT * FROM posts")), 1)

    async def test_pending_recognition_does_not_block_due_24h_measurement(self):
        old_date = NOW - timedelta(hours=23)
        self.prepare(candidates=[message(7, "Неизвестная реклама", date=old_date)])
        # First explicitly discovered, then a second unknown publication awaits AI.
        self.consume(message(4, "https://t.me/partner_one/7", peer=100))
        await self.tick()
        due_date = NOW - timedelta(hours=25)
        self.store.execute("UPDATE posts SET published_at=?,first_seen_at=?", (due_date.isoformat(), (due_date + timedelta(minutes=1)).isoformat()))
        self.store.execute("UPDATE measurement_jobs SET due_at=? WHERE due_key='24h'", ((due_date + timedelta(hours=24)).isoformat(),))
        self.client.histories[200].append(message(8, "Ещё один материал"))
        self.monitor.campaign_worker = CampaignWorker(self.monitor)
        with patch.dict(os.environ, {}, clear=True):
            await self.tick()
        self.assertTrue(self.store.rows("SELECT * FROM snapshots WHERE due_key='24h'"))


class CampaignProtocolTests(unittest.TestCase):
    def gemini_response(self, value=None, status="STOP"):
        response = Mock(status_code=200)
        response.json.return_value = {"candidates": [{"finishReason": status, "content": {"parts": [{"text": json.dumps(value or decision())}]}}]}
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        return response

    def test_gemini_generate_content_inline_images_and_json_schema(self):
        response = self.gemini_response()
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-only-gemini-key"}), patch("partner_monitor.campaigns.requests.post", return_value=response) as http:
            value = GeminiCampaignJudge("gemini-3.1-flash-lite").compare([{"type": "input_text", "text": "test"}, image_input(png("blue"))])
        self.assertEqual(value, decision())
        args = http.call_args.kwargs
        self.assertEqual(http.call_args.args[0], "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent")
        self.assertEqual(args["json"]["generationConfig"]["thinkingConfig"]["thinkingLevel"], "MINIMAL")
        self.assertEqual(args["json"]["generationConfig"]["maxOutputTokens"], 4000)
        self.assertEqual(args["json"]["generationConfig"]["responseMimeType"], "application/json")
        self.assertEqual(args["json"]["contents"][0]["parts"][1]["inlineData"]["mimeType"], "image/jpeg")
        self.assertNotIn("image_url", args["json"]["contents"][0]["parts"][1])
        self.assertNotIn("tools", args["json"])
        self.assertNotIn("test-only-gemini-key", json.dumps(args["json"]))
        self.assertFalse(args["allow_redirects"])

    def test_gemini_rejects_empty_output_incomplete_and_external_images(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}):
            for result in ({"candidates": []}, {"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": []}}]}):
                response = self.gemini_response()
                response.json.return_value = result
                with patch("partner_monitor.campaigns.requests.post", return_value=response), self.assertRaises(CampaignAPIError):
                    GeminiCampaignJudge("test").compare([])
            with self.assertRaisesRegex(CampaignAPIError, "invalid_image"):
                GeminiCampaignJudge("test").compare([{"type": "input_image", "image_url": "https://unrelated.example/picture"}])

    def test_gemini_quota_and_permission_errors_are_safe_codes(self):
        for status in (429, 403):
            response = self.gemini_response()
            response.status_code = status
            with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}), patch("partner_monitor.campaigns.requests.post", return_value=response), self.assertRaisesRegex(CampaignAPIError, f"^http_{status}$"):
                GeminiCampaignJudge("test").compare([])

    def test_http_shape_vision_json_schema_no_storage_or_tools(self):
        response = Mock()
        response.status_code = 200
        response.json.return_value = {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(decision())}]}]}
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-only-key"}), patch("partner_monitor.campaigns.requests.post", return_value=response) as http:
            self.assertEqual(OpenAICampaignJudge("test-model").compare([image_input(png("red"))]), decision())
        args = http.call_args.kwargs
        self.assertIs(args["json"]["store"], False)
        self.assertTrue(args["json"]["text"]["format"]["strict"])
        self.assertNotIn("tools", args["json"])
        self.assertFalse(args["allow_redirects"])
        self.assertEqual(http.call_args.args[0], "https://api.openai.com/v1/responses")

    def test_errors_do_not_contain_response_or_key(self):
        response = Mock(status_code=401, text="private-advertisement-and-key")
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-only-key"}), patch("partner_monitor.campaigns.requests.post", return_value=response):
            with self.assertRaisesRegex(CampaignAPIError, "^http_401$"):
                OpenAICampaignJudge("test").compare([])

    def test_invalid_images_and_oversized_downloads_rejected(self):
        with self.assertRaises(CampaignAPIError):
            image_input(b"not an image")
        with LimitedBuffer() as buffer:
            with self.assertRaises(CampaignAPIError):
                buffer.write(b"x" * (5 * 1024 * 1024 + 1))

    def test_nan_confidence_and_bool_campaign_id_rejected(self):
        for value in (decision(confidence=float("nan")), decision(True), decision(confidence=True)):
            with self.assertRaises(ValueError):
                validate_decision(value, [1])

    def test_config_explicit_enable_and_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text("source_group: -1000000000100\nconversation_mode: true\nallowed_channels: [partner_one]\ncampaign_matching: true\ncampaign_daily_limit: 8\n", encoding="utf-8")
            config = load_config(path)
            config.validate_run()
            self.assertTrue(config.campaign_matching)
            self.assertEqual(config.campaign_daily_limit, 8)
            path.write_text("campaign_matching: yesplease\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_config(path)


if __name__ == "__main__":
    unittest.main()
