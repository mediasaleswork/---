"""Quota fallback integration; no real provider, session or Telegram writes."""
from datetime import datetime, timezone
import io
import os
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

import test_campaign_queue as queue
import test_campaign_throughput as throughput
from test_conversation import image_message
from test_engine import message
from partner_monitor.campaigns import CampaignAPIError, CampaignWorker, GeminiCampaignJudge, enqueue
from partner_monitor.campaign_local import image_fingerprints, text_copy, video_ids
from partner_monitor.campaign_queue import settle_placements
from partner_monitor.evidence_archive import report_fingerprint

AD = 'Компания Alpha Lease начала работу. Оборудование доступно в рассрочку.'


class LocalTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = queue.QueueTests.asyncSetUp
    asyncTearDown = queue.QueueTests.asyncTearDown
    consume = queue.QueueTests.consume
    prepare = queue.QueueTests.prepare
    batch = queue.QueueTests.batch
    tick = queue.QueueTests.tick
    queue_only = queue.QueueTests.queue_only
    setup_jobs = throughput.ThroughputTests.setup_jobs

    def exhaust_budget(self):
        self.day = 'campaign_api_attempts:' + datetime.now(timezone.utc).date().isoformat()
        self.store.set_state(self.day, self.monitor.config.campaign_daily_limit)

    async def test_daily_cap_still_detects_copied_text_and_creates_measurements(self):
        self.batch(candidates=[message(7, AD)])
        self.exhaust_budget()
        await self.tick()
        self.assertFalse(self.judge.calls)
        self.assertEqual(len(self.store.rows('SELECT * FROM posts')), 1)
        self.assertTrue(self.store.rows('SELECT * FROM measurement_jobs'))
        self.assertEqual(int(self.store.state(self.day)), self.monitor.config.campaign_daily_limit)
        self.assertFalse(self.store.rows('SELECT * FROM campaign_requests'))

    async def test_all_channels_are_checked_and_new_jobs_continue_without_ai(self):
        await self.setup_jobs(count=10)
        self.exhaust_budget()
        await self.monitor.campaign_worker.process()
        self.assertFalse(self.judge.calls)
        self.assertEqual(len(self.store.rows("SELECT * FROM campaign_checks WHERE status='local_checked'")), 10)
        partner = self.store.rows('SELECT * FROM partners WHERE id=1')[0]
        post = message(8, AD)
        self.client.histories[200].append(post)
        enqueue(self.store, self.monitor.config, partner, [post], self.store.rows('SELECT * FROM placements WHERE partner_id=1'))
        await self.monitor.campaign_worker.process()
        self.assertEqual(len(self.store.rows('SELECT * FROM posts')), 1)
        self.assertFalse(self.judge.calls)

    async def test_local_checks_receive_additional_ai_verification_when_capacity_returns(self):
        await self.setup_jobs()
        self.exhaust_budget()
        await self.monitor.campaign_worker.process()
        self.store.set_state(self.day, 0)  # Only this isolated fake database.
        await self.monitor.campaign_worker.process()
        self.assertEqual(len(self.judge.calls), 1)
        self.assertEqual(len(self.store.rows('SELECT * FROM posts')), 2)
        self.assertEqual(self.store.state('campaign_mode'), 'ai')

    async def test_missing_key_uses_local_matching_without_calling_provider(self):
        self.batch(candidates=[message(7, AD)])
        await self.queue_only()
        self.monitor.campaign_worker = CampaignWorker(self.monitor, GeminiCampaignJudge(self.config.campaign_model))
        with patch.dict(os.environ, {'GEMINI_API_KEY': ''}), patch.object(GeminiCampaignJudge, 'compare_many', side_effect=AssertionError('No API allowed')):
            await self.monitor.campaign_worker.process()
        self.assertEqual(len(self.store.rows('SELECT * FROM posts')), 1)
        self.assertEqual(self.store.state('campaign_local_reason'), 'missing_api_key')

    async def test_outage_backoff_does_not_block_local_checks(self):
        await self.setup_jobs()
        original = self.judge.compare_many
        def fail(content, ids):
            self.judge.calls.append((content, ids))
            raise CampaignAPIError('http_503')
        self.judge.compare_many = fail
        await self.monitor.campaign_worker.process()
        self.assertEqual(len(self.judge.calls), 1)
        await self.monitor.campaign_worker.process()
        self.assertEqual(len(self.judge.calls), 1)
        self.assertEqual(len(self.store.rows("SELECT * FROM campaign_checks WHERE status='local_checked'")), 2)
        self.assertEqual(len(self.store.rows('SELECT * FROM campaign_requests')), 1)
        self.judge.compare_many = original

    async def test_reuploaded_identical_artwork_is_found_without_ai(self):
        self.batch(source_image=True, candidates=[image_message(7, 'Новая подпись к согласованному изображению', photo=1900, peer=200)])
        image = Image.new('RGB', (220, 160), 'white')
        draw = ImageDraw.Draw(image)
        draw.rectangle((20, 20, 180, 110), fill='navy')
        draw.ellipse((100, 40, 210, 150), fill='red')
        buffer = io.BytesIO()
        image.save(buffer, format='PNG')
        self.client.attachments[1] = self.client.attachments[7] = buffer.getvalue()
        await self.queue_only()
        self.exhaust_budget()
        await self.monitor.campaign_worker.process()
        self.assertEqual(len(self.store.rows('SELECT * FROM posts')), 1)
        self.assertIn('пиксели', self.store.rows('SELECT reason FROM campaign_checks')[0]['reason'])
        self.assertFalse(self.judge.calls)

    async def test_reference_revision_during_local_image_read_prevents_stale_match(self):
        self.batch(source_image=True, candidates=[image_message(7, 'Изменённая подпись', photo=1900, peer=200)])
        await self.queue_only()
        self.exhaust_budget()
        async def changed(messages):
            self.store.execute("UPDATE placements SET source_text='Новая версия материала'")
            return []
        with patch.object(self.monitor.campaign_worker, 'candidate_images', new=changed):
            await self.monitor.campaign_worker.process()
        self.assertFalse(self.store.rows('SELECT * FROM posts'))
        self.assertEqual(self.store.rows('SELECT status FROM campaign_checks')[0]['status'], 'obsolete')

    async def test_unavailable_oldest_job_does_not_stop_other_local_checks(self):
        await self.setup_jobs()
        self.client.histories[200] = []
        self.exhaust_budget()
        await self.monitor.campaign_worker.process()
        self.assertEqual({r['status'] for r in self.store.rows('SELECT status FROM campaign_checks')}, {'unavailable', 'local_checked'})
        self.assertFalse(self.judge.calls)

    async def test_local_negatives_do_not_produce_a_false_missing_placement(self):
        await self.setup_jobs(count=1)
        self.exhaust_budget()
        await self.monitor.campaign_worker.process()
        self.store.execute("UPDATE placements SET scan_complete=1,status='watching'")
        settle_placements(self.monitor.campaign_worker)
        self.assertEqual(self.store.rows('SELECT status FROM placements')[0]['status'], 'watching')

    async def test_alternative_references_to_same_video_count_as_one_exit(self):
        self.batch(candidates=[message(7, 'Смотрите фильм https://youtu.be/AbCdEfGhI_1')])
        self.consume(message(4, 'Название: Film\nКанал: @partner_one\nТекст рекламы: Другой фильм https://youtu.be/AbCdEfGhI_1', peer=100))
        self.consume(message(5, 'Превью', peer=100))
        self.consume(message(6, 'ок', peer=100))
        self.store.execute("UPDATE placements SET source_text=source_text || ' https://youtu.be/AbCdEfGhI_1'")
        await self.queue_only()
        self.exhaust_budget()
        await self.monitor.campaign_worker.process()
        self.assertEqual(len(self.store.rows('SELECT * FROM posts')),1)
        self.assertEqual(len(self.store.rows('SELECT * FROM placement_posts')),1)
        self.assertEqual(self.store.rows('SELECT status FROM campaign_checks')[0]['status'], 'match')

    async def test_report_distinguishes_local_checks_from_unprocessed_jobs(self):
        from openpyxl import load_workbook
        from partner_monitor.unified_report import export_unified
        await self.setup_jobs(count=1)
        self.exhaust_budget()
        await self.monitor.campaign_worker.process()
        data = self.store.dump()
        path = self.config.runtime_dir / 'local-mode-report.xlsx'
        export_unified(*data, path)
        book = load_workbook(path)
        self.assertIn('Без ИИ', book['Отчёт']['A5'].value)
        self.assertIn('Проверено локально без подтверждения: 1', book['Отчёт']['A5'].value)
        before = report_fingerprint(*data)
        data[0][0]['campaign_mode'] = 'ai'
        data[0][0]['campaign_local_reason'] = ''
        self.assertEqual(report_fingerprint(*data), before)
        book.close()


class LocalRuleTests(unittest.TestCase):
    def test_small_text_edits_match_but_changed_offer_numbers_do_not(self):
        text = 'Банк предлагает новый продукт для покупки производственного оборудования в рассрочку на срок 36 месяцев со ставкой 12 процентов годовых для предпринимателей.'
        placement = {'ad_type': 'рекламный материал', 'source_text': text}
        self.assertTrue(text_copy(text + ' Подписывайтесь на канал.', placement))
        self.assertFalse(text_copy(text.replace('12 процентов', '25 процентов'), placement))

    def test_video_keys_match_exact_id_and_reject_lookalike_hosts(self):
        self.assertEqual(video_ids('https://youtu.be/AbCdEfGhI_1'), video_ids('https://www.youtube.com/watch?v=AbCdEfGhI_1&t=25'))
        self.assertFalse(video_ids('https://youtube.com.evil.example/watch?v=AbCdEfGhI_1'))

    def test_blank_images_are_not_creative_identity(self):
        from partner_monitor.campaigns import image_input
        stream = io.BytesIO()
        Image.new('RGB', (220, 160), 'white').save(stream, format='PNG')
        self.assertFalse(image_fingerprints([image_input(stream.getvalue())]))
