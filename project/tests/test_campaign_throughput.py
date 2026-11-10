"""Cross-channel isolation and restart recovery; all providers/Telegram are fakes."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from telethon import types
import test_campaigns as base
import test_campaign_queue as queue
from test_engine import channel, message, NOW
from test_conversation import image_message
from partner_monitor.campaigns import CampaignAPIError, CampaignWorker, GeminiCampaignJudge, enqueue
from partner_monitor.campaign_runtime import retry_hint
from partner_monitor.campaign_queue import defer
from partner_monitor.engine import Monitor
from partner_monitor.parsing import PartnerReference


class ThroughputTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=queue.QueueTests.asyncSetUp
    asyncTearDown=queue.QueueTests.asyncTearDown
    consume=queue.QueueTests.consume
    prepare=queue.QueueTests.prepare
    batch=queue.QueueTests.batch
    tick=queue.QueueTests.tick
    queue_only=queue.QueueTests.queue_only

    async def setup_jobs(self, count=2, second_campaign=False):
        self.batch(candidates=[message(7,'Первый вариант рекламной публикации')])
        def answer(content,ids):
            self.judge.calls.append((content,ids))
            canonical=json.loads(next(p['text'][10:] for p in content if p['type']=='input_text' and p['text'].startswith('REFERENCE ')))['campaign_id']
            return {i:base.decision(canonical) for i in ids}
        self.judge.compare_many=answer
        if second_campaign:
            self.consume(message(4,'Название: Film\nКанал: @partner_one\nТекст рекламы: Фильм об истории бизнеса.',peer=100))
            self.consume(message(5,'Превью',peer=100));self.consume(message(6,'ок',peer=100))
        await self.queue_only()
        names=['partner_one']+[f'partner_{i}' for i in range(2,count+1)]
        config=replace(self.monitor.config,allowed_channels=tuple(names))
        self.monitor.config=config;self.monitor.campaign_worker.config=config
        originals=self.store.rows('SELECT * FROM placements WHERE partner_id=1')
        for i,name in enumerate(names[1:],2):
            pid=self.store.add_partner(PartnerReference(name),-1000000000100,1,enqueue=False)
            cid=199+i
            self.store.execute("UPDATE partners SET channel_id=?,status='active' WHERE id=?",(cid,pid))
            self.client.entities[name]=channel(cid,name)
            self.client.histories[cid]=[message(7,f'Вариант рекламы для канала {i}',peer=cid)]
            for original in originals:
                values={k:v for k,v in original.items() if k!='id'};values['partner_id']=pid
                self.store.execute('INSERT INTO placements('+','.join(values)+') VALUES ('+','.join('?' for _ in values)+')',tuple(values.values()))
            enqueue(self.store,config,self.store.rows('SELECT * FROM partners WHERE id=?',(pid,))[0],
                    self.client.histories[cid],self.store.rows('SELECT * FROM placements WHERE partner_id=?',(pid,)))

    async def test_same_message_id_in_two_channels_shares_one_request_but_correct_placements(self):
        await self.setup_jobs()
        await self.monitor.campaign_worker.process()
        self.assertEqual(len(self.judge.calls),1)
        self.assertEqual(len(self.judge.calls[0][1]),2)
        self.assertEqual(len(self.store.rows('SELECT * FROM posts')),2)
        links=self.store.rows('SELECT b.partner_id campaign_partner,p.partner_id post_partner FROM placement_posts x JOIN placements b ON b.id=x.placement_id JOIN posts p ON p.id=x.post_id')
        self.assertTrue(all(r['campaign_partner']==r['post_partner'] for r in links))
        self.assertEqual(self.store.state('campaign_api_attempts_total'),'1')
        self.assertEqual(len(self.store.rows("SELECT * FROM campaign_requests WHERE status='success'")),1)

    async def test_cross_channel_positive_does_not_leak_to_other_candidate(self):
        await self.setup_jobs()
        jobs=self.store.rows('SELECT * FROM campaign_checks ORDER BY partner_id')
        self.judge.compare_many=lambda content,ids:{i:base.decision(1) if i==jobs[1]['id'] else base.decision(None,'different_campaign') for i in ids}
        await self.monitor.campaign_worker.process()
        self.assertEqual([r['partner_id'] for r in self.store.rows('SELECT * FROM posts')],[2])
        self.assertEqual(self.store.rows('SELECT placement_id FROM placement_posts')[0]['placement_id'],2)

    async def test_mapping_selects_same_actual_creative_across_different_placement_ids(self):
        await self.setup_jobs(second_campaign=True)
        self.judge.compare_many=lambda content,ids:{i:base.decision(2) for i in ids}
        await self.monitor.campaign_worker.process()
        self.assertEqual({r['placement_id'] for r in self.store.rows('SELECT * FROM placement_posts')},{2,4})
        self.assertEqual(len(self.store.rows('SELECT * FROM posts')),2)

    async def test_different_references_never_share_a_request(self):
        await self.setup_jobs()
        self.store.execute("UPDATE placements SET source_text='Другая рекламная кампания' WHERE partner_id=2")
        partner=self.store.rows('SELECT * FROM partners WHERE id=2')[0]
        enqueue(self.store,self.monitor.config,partner,self.client.histories[201],self.store.rows('SELECT * FROM placements WHERE partner_id=2'))
        await self.monitor.campaign_worker.process()
        self.assertEqual(len(self.judge.calls[0][1]),1)
        self.assertEqual(len(self.store.rows('SELECT * FROM posts')),1)

    async def test_eight_channel_limit_and_second_batch_remain_separate(self):
        await self.setup_jobs(count=10)
        await self.monitor.campaign_worker.process()
        self.assertEqual(len(self.judge.calls[0][1]),8)
        self.store.set_state('campaign_next_request_at','')
        await self.monitor.campaign_worker.process()
        self.assertEqual([len(c[1]) for c in self.judge.calls],[8,2])
        self.assertEqual(len(self.store.rows('SELECT * FROM posts')),10)

    async def test_valid_response_survives_telegram_failure_and_restart_without_new_ai(self):
        await self.setup_jobs()
        original=self.client.get_messages
        fail=False
        def answer(content,ids):
            nonlocal fail
            fail=True
            return {i:base.decision(1) for i in ids}
        async def messages(entity,ids):
            if fail:raise OSError('temporary Telegram outage')
            return await original(entity,ids)
        self.judge.compare_many=answer;self.client.get_messages=messages
        await self.monitor.campaign_worker.process()
        self.assertFalse(self.store.rows('SELECT * FROM posts'))
        self.assertTrue(all(r['provisional_decision'] for r in self.store.rows('SELECT * FROM campaign_checks')))
        fail=False
        self.store.execute("UPDATE campaign_checks SET next_try_at='2000-01-01T00:00:00+00:00'")
        restarted=Monitor(self.monitor.config,self.store,self.client,self.capture)
        restarted.source_peer_id=self.monitor.source_peer_id
        restarted.campaign_worker=CampaignWorker(restarted,self.judge)
        self.store.set_state('campaign_quota_retry_at',(NOW+timedelta(days=10)).isoformat())
        await restarted.campaign_worker.process()
        self.assertEqual(len(self.store.rows('SELECT * FROM posts')),2)
        self.assertEqual(self.store.state('campaign_api_attempts_total'),'1')

    async def test_cached_result_commits_even_after_api_key_removed(self):
        await self.setup_jobs(count=1)
        self.store.execute('UPDATE campaign_checks SET provisional_decision=?',(json.dumps(base.decision(1)),))
        worker=CampaignWorker(self.monitor,GeminiCampaignJudge('test-model'))
        self.monitor.campaign_worker=worker
        with patch.dict('os.environ',{},clear=True):await worker.process()
        self.assertEqual(len(self.store.rows('SELECT * FROM posts')),1)
        self.assertEqual(self.store.state('campaign_api_attempts_total'),'0')

    async def test_partial_response_does_not_persist_any_provisional_match(self):
        await self.setup_jobs()
        self.judge.compare_many=lambda content,ids:{ids[0]:base.decision(1)}
        await self.monitor.campaign_worker.process()
        self.assertFalse(any(r['provisional_decision'] for r in self.store.rows('SELECT * FROM campaign_checks')))
        self.assertFalse(self.store.rows('SELECT * FROM posts'))

    async def test_one_missing_image_does_not_poison_other_candidate(self):
        self.batch(candidates=[image_message(7,'Реклама с картинкой',peer=200),message(8,'Другое объявление без картинки')])
        self.client.attachments[7]=b''
        await self.tick()
        self.assertEqual(len(self.judge.calls),1)
        self.assertEqual([p['message_id'] for p in self.store.rows('SELECT * FROM posts')],[8])
        self.assertEqual(self.store.rows("SELECT reason FROM campaign_checks WHERE message_ids='[7]'")[0]['reason'],'media_unavailable')

    async def test_reference_images_are_reused_after_worker_restart(self):
        self.batch(source_image=True)
        await self.queue_only()
        placement=self.store.rows('SELECT * FROM placements')[0]
        first=await self.monitor.campaign_worker.reference_images(placement)
        self.client.histories[100]=[]
        restarted=CampaignWorker(self.monitor,self.judge)
        second=await restarted.reference_images(placement)
        self.assertEqual(first,second)
        self.assertEqual(sum(c==('image',1,-1) for c in self.client.calls),1)

    async def test_short_collect_window_flushes_without_waiting_forever_for_eight(self):
        await self.setup_jobs()
        self.monitor.campaign_background=True
        await self.monitor.campaign_worker.process()
        self.assertFalse(self.judge.calls)
        self.store.execute('UPDATE campaign_checks SET created_at=?',((NOW-timedelta(minutes=4)).isoformat(),))
        await self.monitor.campaign_worker.process()
        self.assertEqual(len(self.judge.calls),1)

    async def test_retry_is_not_held_for_collection(self):
        await self.setup_jobs(count=1)
        self.monitor.campaign_background=True
        self.store.execute("UPDATE campaign_checks SET status='error',reason='http_503',attempts=1")
        await self.monitor.campaign_worker.process()
        self.assertEqual(len(self.judge.calls),1)

    async def test_service_retry_uses_real_backoff_instead_of_fixed_five_minutes(self):
        await self.setup_jobs(count=1)
        job=self.store.rows('SELECT * FROM campaign_checks')[0]
        before=datetime.now(timezone.utc)
        defer(self.monitor.campaign_worker,[job],CampaignAPIError('http_503',retry_after=45))
        row=self.store.rows('SELECT * FROM campaign_checks')[0]
        delay=(datetime.fromisoformat(row['next_try_at'])-before).total_seconds()
        self.assertTrue(44<=delay<60)

    async def test_rich_message_photos_are_sent_to_judge(self):
        rich=types.RichMessage(blocks=[types.PageBlockParagraph(types.TextPlain('Текст карусели')),types.PageBlockPhoto(photo_id=900,caption=types.PageCaption(types.TextEmpty(),types.TextEmpty()))],
            photos=[types.Photo(id=900,access_hash=1,file_reference=b'x',date=NOW,sizes=[],dc_id=1)],documents=[])
        self.batch(candidates=[types.Message(id=7,peer_id=types.PeerChannel(200),date=NOW,message='',rich_message=rich)])
        self.client.attachments[900]=base.png('green')
        await self.tick()
        self.assertTrue(any(p['type']=='input_image' for p in self.judge.calls[0][0]))
        self.assertEqual(len(self.store.rows('SELECT * FROM posts')),1)

    async def test_updated_material_reopens_a_completed_history_scan(self):
        from partner_monitor.intake import ConversationIntake
        self.batch()
        self.store.execute("UPDATE placements SET scan_complete=1,status='not_found'")
        self.store.execute('UPDATE partners SET last_message_id=999,channel_id=200')
        intake=ConversationIntake(self.store,self.config)
        draft=self.store.rows('SELECT * FROM intake_drafts')[0]
        intake._sync(draft,NOW,['partner_one'],approved=True)
        await self.tick()
        self.assertEqual(len(self.store.rows('SELECT * FROM posts')),1)

    async def test_optional_source_copy_probe_failure_does_not_block_main_queue(self):
        from unittest.mock import AsyncMock
        await self.setup_jobs(count=1)
        with patch('partner_monitor.archived_checks.recover_forwarded_captions',new=AsyncMock(side_effect=OSError('source chat unavailable'))):
            await self.monitor.campaign_worker.process()
        self.assertEqual(len(self.store.rows('SELECT * FROM posts')),1)

    async def test_individual_review_mode_survives_a_service_error(self):
        await self.setup_jobs(count=1)
        self.judge.compare_many=lambda content,ids:{i:base.decision(None,'uncertain',confidence=.5) for i in ids}
        await self.monitor.campaign_worker.process()
        self.assertEqual(self.store.rows('SELECT individual_review FROM campaign_checks')[0]['individual_review'],1)
        self.store.set_state('campaign_next_request_at','')
        with patch.object(queue.BatchJudge,'compare',create=True,side_effect=CampaignAPIError('http_503')):
            await self.monitor.campaign_worker.process()
        self.assertEqual(self.store.rows('SELECT reason FROM campaign_checks')[0]['reason'],'http_503')
        self.store.execute("UPDATE campaign_checks SET next_try_at='2000-01-01T00:00:00+00:00'")
        self.store.set_state('campaign_next_request_at','');self.store.set_state('campaign_service_retry_at','')
        with patch.object(queue.BatchJudge,'compare',create=True,return_value=base.decision(None,'different_campaign')) as compare:
            await self.monitor.campaign_worker.process()
            compare.assert_called_once()
        self.assertEqual(self.store.rows('SELECT status FROM campaign_checks')[0]['status'],'no_match')


class ConfigurationAndCaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_channel_scope_is_supported_by_yaml(self):
        from partner_monitor.scope import read_scope
        scope=read_scope({'allowed_channels':['c:300','@partner_one']})
        self.assertEqual(scope['allowed_channels'],('c:300','partner_one'))
        with self.assertRaises(ValueError):read_scope({'allowed_channels':['c:0']})

    async def test_concurrent_screenshots_are_bounded_and_survive_page_close_failure(self):
        import asyncio,tempfile
        from pathlib import Path
        from partner_monitor.capture import Screenshotter
        active=0;peak=0
        async def page():
            nonlocal active,peak
            active+=1;peak=max(peak,active)
            async def close():
                nonlocal active
                active-=1
                raise OSError('browser closed after PNG was saved')
            return SimpleNamespace(close=close)
        with tempfile.TemporaryDirectory() as directory:
            capture=Screenshotter(Path(directory));capture._context=SimpleNamespace(new_page=page)
            async def widget(page,username,message_id,path):
                await asyncio.sleep(.02)
                path.write_bytes(base.png('blue'))
            capture._capture_widget=widget
            results=await asyncio.gather(*(capture.capture('partner_one',[i],str(i)) for i in range(1,6)))
            self.assertEqual(peak,2)
            self.assertTrue(all(result.status=='ok' and Path(result.path).is_file() for result in results))


class RetryHintTests(unittest.TestCase):
    def test_header_and_retry_info_use_longest_valid_delay(self):
        response=SimpleNamespace(headers={'Retry-After':'15'},json=lambda:{'error':{'details':[{'@type':'type.googleapis.com/google.rpc.RetryInfo','retryDelay':'42.5s'}]}})
        self.assertEqual(retry_hint(response),42.5)

    def test_invalid_or_unbounded_hint_does_not_escape_into_delay(self):
        for value in ('secret-text','nan','inf','-1','999999999'):
            response=SimpleNamespace(headers={'Retry-After':value},json=lambda:{})
            self.assertIsNone(retry_hint(response))
