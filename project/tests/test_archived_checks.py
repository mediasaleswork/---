"""Archived captions never fabricate a live post or a confirmed placement."""
import json
from datetime import timedelta
from unittest.mock import patch
from unittest import IsolatedAsyncioTestCase
import test_campaigns as base
import test_campaign_queue as queue
from test_engine import message,NOW
from partner_monitor.archived_checks import saved_caption
from partner_monitor.archived_checks import recover_forwarded_captions
from telethon import types


class ArchivedTests(IsolatedAsyncioTestCase):
    asyncSetUp=queue.QueueTests.asyncSetUp
    asyncTearDown=queue.QueueTests.asyncTearDown
    consume=queue.QueueTests.consume
    prepare=queue.QueueTests.prepare
    tick=queue.QueueTests.tick
    batch=queue.QueueTests.batch
    queue_only=queue.QueueTests.queue_only
    async def prepare_archive(self):
        self.batch(candidates=[message(7,'Исходная публикация о совсем другом предложении компании')])
        await self.queue_only()
        self.client.histories[200]=[]
        job=self.store.rows('SELECT * FROM campaign_checks')[0]
        data=json.loads(job['candidate_snapshot'])
        data.update(source_url='https://t.me/partner_one/7',archive_checked_at=NOW.isoformat(),media=['unknown'])
        self.store.execute('UPDATE campaign_checks SET candidate_snapshot=? WHERE id=?',(json.dumps(data),job['id']))
        return job

    async def test_preserved_caption_can_exclude_different_campaign_without_creating_post(self):
        await self.prepare_archive()
        with patch.object(queue.BatchJudge,'compare_archived',create=True,return_value=base.decision(None,'different_campaign')):
            await self.monitor.campaign_worker.process()
        job=self.store.rows('SELECT * FROM campaign_checks')[0]
        self.assertEqual(job['status'],'archived_no_match')
        self.assertTrue(job['archive_signature'])
        self.assertFalse(self.store.rows('SELECT * FROM posts'))
        self.assertEqual(json.loads(job['decision'])['evidence_type'],'archived_caption_without_visuals')

    async def test_archived_positive_requires_review_and_never_counts(self):
        await self.prepare_archive()
        with patch.object(queue.BatchJudge,'compare_archived',create=True,return_value=base.decision()):
            await self.monitor.campaign_worker.process()
        self.assertEqual(self.store.rows('SELECT status FROM campaign_checks')[0]['status'],'review')
        self.assertFalse(self.store.rows('SELECT * FROM posts'))

    async def test_scope_pause_during_archival_request_keeps_original_job(self):
        await self.prepare_archive()
        def response(content):
            self.monitor.scope_ready=False
            return base.decision(None,'different_campaign')
        with patch.object(queue.BatchJudge,'compare_archived',create=True,side_effect=response):
            await self.monitor.campaign_worker.process()
        self.assertEqual(self.store.rows('SELECT status FROM campaign_checks')[0]['status'],'pending')
        self.assertFalse(self.store.rows('SELECT * FROM posts'))

    async def test_archival_response_survives_failed_telegram_refresh_without_another_ai_call(self):
        await self.prepare_archive()
        original=self.client.get_messages
        fail=False
        async def get_messages(entity,ids):
            if fail:raise OSError('Telegram disconnected after inference')
            return await original(entity,ids)
        self.client.get_messages=get_messages
        def answer(content):
            nonlocal fail
            fail=True
            return base.decision(None,'different_campaign')
        with patch.object(queue.BatchJudge,'compare_archived',create=True,side_effect=answer) as compare:
            with self.assertRaises(OSError):await self.monitor.campaign_worker.process()
            self.assertTrue(self.store.rows('SELECT provisional_decision FROM campaign_checks')[0]['provisional_decision'])
            fail=False
            self.store.set_state('campaign_quota_retry_at',(NOW+timedelta(days=1)).isoformat())
            with patch.object(self.monitor.campaign_worker,'reference_images',side_effect=OSError('reference no longer available')):
                await self.monitor.campaign_worker.process()
            self.assertEqual(compare.call_count,1)
        self.assertEqual(self.store.rows('SELECT status FROM campaign_checks')[0]['status'],'archived_no_match')

    async def test_archived_rejection_is_not_sent_again_on_each_availability_retry(self):
        await self.prepare_archive()
        with patch.object(queue.BatchJudge,'compare_archived',create=True,return_value=base.decision(None,'different_campaign')) as compare:
            await self.monitor.campaign_worker.process()
            self.store.execute("UPDATE campaign_checks SET next_try_at='2000-01-01T00:00:00+00:00'")
            self.store.set_state('campaign_next_request_at','')
            await self.monitor.campaign_worker.process()
            self.assertEqual(compare.call_count,1)
        self.assertEqual(self.store.rows('SELECT status FROM campaign_checks')[0]['status'],'archived_no_match')

    async def test_wrong_post_url_or_future_date_is_not_used_as_evidence(self):
        await self.prepare_archive()
        job=self.store.rows('SELECT * FROM campaign_checks')[0];partner=self.store.rows('SELECT * FROM partners')[0]
        data=json.loads(job['candidate_snapshot'])
        self.assertIsNotNone(saved_caption(job,partner))
        self.assertIsNone(saved_caption(dict(job,candidate_snapshot=json.dumps(dict(data,source_url='https://t.me/other/7'))),partner))
        self.assertIsNone(saved_caption(dict(job,candidate_snapshot=json.dumps(dict(data,published_at=(NOW+timedelta(days=100)).isoformat()))),partner))

    async def test_api_spacing_does_not_delay_preserved_caption_by_hours(self):
        await self.prepare_archive()
        self.store.execute("UPDATE campaign_checks SET status='unavailable',next_try_at='2000-01-01T00:00:00+00:00'")
        self.store.set_state('campaign_next_request_at',(NOW+timedelta(hours=1)).isoformat())
        with patch.object(queue.BatchJudge,'compare_archived',create=True,return_value=base.decision(None,'different_campaign')) as compare:
            await self.monitor.campaign_worker.process()
            row=self.store.rows('SELECT * FROM campaign_checks')[0]
            self.assertEqual(row['next_try_at'],'2000-01-01T00:00:00+00:00')
            self.assertEqual(row['availability_attempts'],0)
            compare.assert_not_called()
            self.store.set_state('campaign_next_request_at','')
            await self.monitor.campaign_worker.process()
        self.assertEqual(self.store.rows('SELECT status FROM campaign_checks')[0]['status'],'archived_no_match')

    async def test_exact_forwarded_source_copy_recovers_missing_caption(self):
        await self.prepare_archive()
        self.store.execute("UPDATE campaign_checks SET status='unavailable',candidate_snapshot=''")
        copy=self.client.histories[100][0]
        copy.message='Сохранённая пересылка содержит исходный текст публикации и сведения об объявлении.'
        copy.fwd_from=types.MessageFwdHeader(date=NOW,from_id=types.PeerChannel(200),channel_post=7)
        await recover_forwarded_captions(self.monitor.campaign_worker)
        value=json.loads(self.store.rows('SELECT candidate_snapshot FROM campaign_checks')[0]['candidate_snapshot'])
        self.assertEqual(value['archive_method'],'verified_telegram_forward')
        self.assertEqual(value['forward_copy_message_id'],1)
        self.assertEqual(value['text'],copy.message)
        self.assertFalse(self.store.rows('SELECT * FROM posts'))

    async def test_forward_from_another_channel_is_never_attached(self):
        await self.prepare_archive()
        self.store.execute("UPDATE campaign_checks SET status='unavailable',candidate_snapshot=''")
        self.client.histories[100][0].fwd_from=types.MessageFwdHeader(date=NOW,from_id=types.PeerChannel(999),channel_post=7)
        await recover_forwarded_captions(self.monitor.campaign_worker)
        self.assertEqual(self.store.rows('SELECT candidate_snapshot FROM campaign_checks')[0]['candidate_snapshot'],'')
