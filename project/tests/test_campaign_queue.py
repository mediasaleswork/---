"""Queue integration: batching, recovery and attribution, without real API calls."""
import json
from unittest import IsolatedAsyncioTestCase,TestCase
from unittest.mock import AsyncMock,patch

import test_campaigns as cases
from test_engine import message
from partner_monitor.campaigns import CampaignWorker,CampaignAPIError,GeminiCampaignJudge


class BatchJudge:
    def __init__(self):self.calls=[];self.failure=None
    def compare_many(self,content,ids):
        self.calls.append((content,ids))
        if self.failure:raise self.failure
        return {i:cases.decision() for i in ids}


class QueueTests(IsolatedAsyncioTestCase):
    asyncSetUp=cases.CampaignIntegrationTests.asyncSetUp
    asyncTearDown=cases.CampaignIntegrationTests.asyncTearDown
    consume=cases.CampaignIntegrationTests.consume
    prepare=cases.CampaignIntegrationTests.prepare
    tick=cases.CampaignIntegrationTests.tick

    def batch(self,**kwargs):
        self.prepare(**kwargs);self.judge=BatchJudge()
        self.monitor.campaign_worker=CampaignWorker(self.monitor,self.judge)

    async def queue_only(self):
        with patch.object(self.monitor.campaign_worker,'process',new=AsyncMock()):await self.tick()

    async def test_eight_candidates_share_one_request_and_have_separate_attribution(self):
        self.batch(candidates=[message(i,'Новый вариант объявления '+str(i)) for i in range(7,15)])
        await self.tick()
        self.assertEqual(len(self.judge.calls),1)
        self.assertEqual(len(self.store.rows('SELECT * FROM posts')),8)
        self.assertEqual(self.store.state('campaign_api_attempts_total'),'1')
        self.assertEqual(len(self.store.rows('SELECT * FROM snapshots')),8)
        self.assertEqual(len(self.store.rows('SELECT * FROM placement_posts')),8)

    async def test_incomplete_batch_does_not_commit_even_the_first_match(self):
        self.batch(candidates=[message(7,'Материал один'),message(8,'Материал два')])
        original=self.judge.compare_many
        self.judge.compare_many=lambda content,ids:{ids[0]:original(content,ids)[ids[0]]}
        await self.tick()
        self.assertFalse(self.store.rows('SELECT * FROM posts'))
        self.assertEqual({r['status'] for r in self.store.rows('SELECT status FROM campaign_checks')},{'error'})

    async def test_shared_quota_error_switches_to_local_checks_and_never_fabricates_matches(self):
        self.batch(candidates=[message(7,'Первый материал'),message(8,'Второй материал')])
        self.judge.failure=CampaignAPIError('http_429')
        await self.tick();await self.tick()
        self.assertEqual(len(self.judge.calls),1)
        self.assertFalse(self.store.rows('SELECT * FROM posts'))
        self.assertEqual(len(self.store.rows("SELECT * FROM campaign_checks WHERE status='local_checked'")),2)
        self.assertEqual(self.store.state('campaign_mode'),'local')

    async def test_unavailable_post_is_retried_and_can_be_recovered(self):
        self.batch(candidates=[message(7,'Материал с изменённым текстом')])
        await self.queue_only();saved=self.client.histories[200];self.client.histories[200]=[]
        await self.monitor.campaign_worker.process()
        row=self.store.rows('SELECT * FROM campaign_checks')[0]
        self.assertEqual(row['status'],'unavailable')
        self.assertEqual(row['availability_attempts'],1)
        self.assertFalse(self.judge.calls)
        self.client.histories[200]=saved
        self.store.execute("UPDATE campaign_checks SET next_try_at='2000-01-01T00:00:00+00:00'")
        await self.monitor.campaign_worker.process()
        self.assertEqual(self.store.rows('SELECT status FROM campaign_checks')[0]['status'],'match')
        self.assertEqual(len(self.store.rows('SELECT * FROM posts')),1)

    async def test_old_terminal_unavailable_review_is_automatically_retried(self):
        self.batch(candidates=[message(7,'Проверяемый материал')]);await self.queue_only()
        self.store.execute("UPDATE campaign_checks SET status='review',reason='Публикация недоступна; совпадение не засчитано'")
        await self.monitor.campaign_worker.process()
        self.assertEqual(self.store.rows('SELECT status FROM campaign_checks')[0]['status'],'match')

    async def test_reference_image_is_downloaded_once_across_requests(self):
        self.batch(source_image=True,candidates=[message(i,'Неизвестный вариант '+str(i)) for i in range(7,16)])
        original=self.judge.compare_many
        def negative(content,ids):
            original(content,ids)
            return {i:cases.decision(None,'different_campaign') for i in ids}
        self.judge.compare_many=negative
        await self.tick()
        self.store.set_state('campaign_next_request_at','')
        await self.monitor.campaign_worker.process()
        self.assertEqual(len(self.judge.calls),2)
        self.assertEqual(sum(call==('image',1,-1) for call in self.client.calls),1)

    async def test_candidate_snapshot_is_saved_before_recognition(self):
        self.batch(candidates=[message(7,'Текст, который может исчезнуть из Telegram')]);await self.queue_only()
        payload=json.loads(self.store.rows('SELECT candidate_snapshot FROM campaign_checks')[0]['candidate_snapshot'])
        self.assertIn('может исчезнуть',payload['text'])
        self.assertNotIn('access_hash',payload)
        self.assertNotIn('file_reference',payload)

    async def test_background_worker_does_not_block_channel_tick(self):
        self.batch(candidates=[message(7,'Материал для фонового сравнения')])
        self.monitor.campaign_background=True
        await self.tick()
        self.assertFalse(self.judge.calls)
        self.assertTrue(self.monitor.campaign_wake.is_set())

    async def test_uncertain_batch_is_rechecked_individually_before_manual_review(self):
        self.batch(candidates=[message(7,'Непохожий материал')])
        self.judge.compare_many=lambda content,ids:{i:cases.decision(None,'different_campaign',confidence=.8) for i in ids}
        await self.tick()
        self.assertEqual(self.store.rows('SELECT reason FROM campaign_checks')[0]['reason'],'individual_review')
        self.store.set_state('campaign_next_request_at','')
        with patch.object(BatchJudge,'compare',create=True,return_value=cases.decision(None,'different_campaign')) as single:
            await self.monitor.campaign_worker.process()
            single.assert_called_once()
        self.assertEqual(self.store.rows('SELECT status FROM campaign_checks')[0]['status'],'no_match')


class BatchProtocolTests(TestCase):
    def test_short_labels_keep_schema_small_and_results_map_to_exact_jobs(self):
        judge=GeminiCampaignJudge('test')
        identifiers=['a'*64,'b'*64]
        content=[{'type':'input_text','text':'PUBLICATION '+json.dumps({'job_id':i,'text':'данные'})} for i in identifiers]
        response={'results':[dict(cases.decision(),job_id='p2'),dict(cases.decision(),job_id='p1')]}
        with patch.object(judge,'request',return_value=response) as request:
            result=judge.compare_many(content,identifiers)
        self.assertEqual(set(result),set(identifiers))
        self.assertNotIn('a'*64,json.dumps(request.call_args.args[0]))
        self.assertNotIn('enum',request.call_args.args[2]['properties']['results']['items']['properties']['job_id'])

    def test_duplicate_or_missing_job_ids_are_not_accepted(self):
        judge=GeminiCampaignJudge('test')
        for value in ({'results':[]},{'results':[dict(cases.decision(),job_id='a'),dict(cases.decision(),job_id='a')]},
            {'results':[dict(cases.decision(),job_id='unknown')]}):
            with patch.object(judge,'request',return_value=value),self.assertRaises(CampaignAPIError):
                judge.compare_many([],['a','b'])

    def test_unconfirmed_unavailable_candidates_are_visible_without_being_counted(self):
        import test_unified_report
        from openpyxl import load_workbook
        from partner_monitor.unified_report import export_unified
        fixture=test_unified_report.UnifiedReportTests();fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        partner=fixture.partners[0]
        check={'message_ids':'[999]','reason':'Telegram не вернул публикацию','checked_at':'2026-11-10T09:00:00+00:00',
            'next_try_at':'2026-11-10T09:05:00+00:00','availability_attempts':1,'candidate_snapshot':''}
        partner['campaign_checks']=[check,dict(check),dict(check,message_ids='[1000]',status='review',reason='Нужно уточнить материал')]
        partner['campaign_queue']=[{'status':'pending','n':8},{'status':'unavailable','n':2}]
        path=export_unified(fixture.partners,fixture.posts,fixture.snapshots,fixture.root/'queue.xlsx',compact=True)
        book=load_workbook(path);self.addCleanup(book.close)
        self.assertIn('Ожидают распознавания: 8',book['Отчёт']['A5'].value)
        rows=[row for row in book['Недоступные посты'].iter_rows(values_only=True) if row[0]=='Не подтверждено']
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[0][3],'Не засчитано — проверка доступности')
        self.assertEqual(rows[1][3],'Не засчитано — нужна сверка')
        self.assertIn('Нужна сверка: 1',book['Отчёт']['A5'].value)
        self.assertEqual(book['Реестр публикаций'].max_row,len(fixture.posts)+1)
