"""Archived captions never fabricate a live post or a confirmed placement."""
import json
from datetime import timedelta
from unittest.mock import patch
from unittest import IsolatedAsyncioTestCase
import test_campaigns as base
import test_campaign_queue as queue
from test_engine import message,NOW
from partner_monitor.archived_checks import saved_caption


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
