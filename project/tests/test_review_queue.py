"""Review visibility, retained decisions and automatic rechecks across UTC days."""
from contextlib import ExitStack
from datetime import datetime,timedelta,timezone
import json
import unittest
from unittest.mock import patch

import test_campaign_queue as queue
import test_campaign_throughput as throughput
import test_campaigns as base
from partner_monitor.review_queue import reconcile
from partner_monitor.unified_report import export_unified


class ReviewTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=queue.QueueTests.asyncSetUp
    asyncTearDown=queue.QueueTests.asyncTearDown
    consume=queue.QueueTests.consume
    prepare=queue.QueueTests.prepare
    batch=queue.QueueTests.batch
    tick=queue.QueueTests.tick
    queue_only=queue.QueueTests.queue_only
    setup_jobs=throughput.ThroughputTests.setup_jobs

    def future_clock(self,day):
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):return day.astimezone(tz) if tz else day.replace(tzinfo=None)
        stack=ExitStack()
        for name in ('storage','campaign_queue','campaign_runtime','campaigns','campaign_batching','review_queue'):
            stack.enter_context(patch('partner_monitor.'+name+'.datetime',Clock))
        return stack

    async def test_low_priority_local_result_is_visible_and_survives_restart(self):
        from partner_monitor.storage import Store
        await self.setup_jobs(count=1)
        day='campaign_api_attempts:'+datetime.now(timezone.utc).date().isoformat()
        self.store.set_state(day,self.monitor.config.campaign_daily_limit)
        await self.monitor.campaign_worker.process()
        before=self.store.dump()[0][0]['manual_reviews']
        self.assertEqual(len(before),1);self.assertEqual(before[0]['status'],'local_checked')
        self.store.db.close()
        self.store=Store(self.root/'monitor.sqlite3')
        after=self.store.dump()[0][0]['manual_reviews']
        self.assertEqual(before[0]['id'],after[0]['id'])
        self.assertTrue(json.loads(after[0]['original_snapshot'])['text'])

    async def test_daily_capacity_returns_and_rechecks_without_resetting_old_counter(self):
        await self.setup_jobs(count=1)
        now=datetime.now(timezone.utc);old_key='campaign_api_attempts:'+now.date().isoformat()
        self.store.set_state(old_key,self.monitor.config.campaign_daily_limit)
        await self.monitor.campaign_worker.process()
        with self.future_clock(now+timedelta(days=1)):
            await self.monitor.campaign_worker.process()
        self.assertEqual(len(self.judge.calls),1)
        self.assertEqual(int(self.store.state(old_key)),self.monitor.config.campaign_daily_limit)
        review=self.store.dump()[0][0]['manual_reviews'][0]
        self.assertEqual(review['status'],'match');self.assertGreaterEqual(len(review['events']),2)
        self.assertIn('без ИИ',review['opened_reason'])

    async def test_final_uncertainty_does_not_loop_and_gets_new_ai_decision_next_day(self):
        await self.setup_jobs(count=1)
        self.store.execute('UPDATE campaign_checks SET individual_review=1')
        self.judge.compare_many=lambda content,ids:{i:base.decision(None,'uncertain',confidence=.5) for i in ids}
        await self.monitor.campaign_worker.process()
        job=self.store.rows('SELECT * FROM campaign_checks')[0]
        self.assertEqual(job['status'],'review');self.assertEqual(job['provisional_decision'],'')
        self.assertGreater(job['next_try_at'],datetime.now(timezone.utc).isoformat())
        first_count=self.store.state('campaign_api_attempts_total')
        self.store.set_state('campaign_next_request_at','')
        await self.monitor.campaign_worker.process()
        self.assertEqual(self.store.state('campaign_api_attempts_total'),first_count)
        self.judge.compare_many=lambda content,ids:{i:base.decision(None,'different_campaign') for i in ids}
        with self.future_clock(datetime.now(timezone.utc)+timedelta(days=1)):
            await self.monitor.campaign_worker.process()
        self.assertEqual(self.store.rows('SELECT status FROM campaign_checks')[0]['status'],'no_match')
        self.assertEqual(self.store.dump()[0][0]['manual_reviews'][0]['status'],'no_match')

    async def test_api_error_keeps_the_original_review_visible(self):
        from partner_monitor.campaigns import CampaignAPIError
        await self.setup_jobs(count=1)
        worker=self.monitor.campaign_worker
        job=self.store.rows('SELECT * FROM campaign_checks')[0]
        worker.finish(job,'offline_review','Требуется ручная сверка')
        self.judge.compare_many=lambda *args:(_ for _ in ()).throw(CampaignAPIError('http_503'))
        await worker.process()
        review=self.store.dump()[0][0]['manual_reviews'][0]
        self.assertEqual(review['status'],'error');self.assertEqual(review['opened_reason'],'Требуется ручная сверка')
        self.assertGreaterEqual(len(review['events']),2)

    async def test_report_retains_resolved_and_pending_reviews_and_original_text(self):
        from openpyxl import load_workbook
        await self.setup_jobs(count=1)
        worker=self.monitor.campaign_worker;job=self.store.rows('SELECT * FROM campaign_checks')[0]
        worker.finish(job,'local_checked','Нет уверенного совпадения без ИИ')
        worker.finish(job,'no_match','Проверено: другая кампания',base.decision(None,'different_campaign'))
        path=self.config.runtime_dir/'review.xlsx';export_unified(*self.store.dump(),path)
        book=load_workbook(path);sheet=book['Ручная проверка']
        self.assertEqual(sheet.max_row,2);self.assertEqual(sheet['A2'].value,'Не относится к рекламе')
        self.assertIn('Нет уверенного',sheet['E2'].value)
        self.assertTrue(sheet['D2'].value);self.assertEqual(sheet['L2'].value,job['id'])
        self.assertIn('Не относится',sheet['K2'].value);book.close()

    async def test_review_event_reconciliation_is_idempotent(self):
        await self.setup_jobs(count=1)
        job=self.store.rows('SELECT * FROM campaign_checks')[0]
        self.monitor.campaign_worker.finish(job,'review','Нужно проверить')
        before=self.store.rows('SELECT COUNT(*) n FROM campaign_review_events')[0]['n']
        reconcile(self.store);reconcile(self.store)
        self.assertEqual(self.store.rows('SELECT COUNT(*) n FROM campaign_review_events')[0]['n'],before)
