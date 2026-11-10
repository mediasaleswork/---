"""Recovery regressions: offline Telegram/API doubles, never live test messages."""
import asyncio
from dataclasses import replace
from datetime import timedelta
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch

from telethon import types
import test_campaigns as campaigns
import test_engine as engine
import test_realtime as realtime
import test_forwarded_intake as forwarded
from test_campaign_queue import BatchJudge
from partner_monitor.campaigns import CampaignWorker
from partner_monitor.campaign_queue import settle_placements
from partner_monitor.engine import Monitor
from partner_monitor.reconciliation import reconcile_channel
from partner_monitor.telegram_access import TelegramAccess


class CampaignRecoveryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=campaigns.CampaignIntegrationTests.asyncSetUp
    asyncTearDown=campaigns.CampaignIntegrationTests.asyncTearDown
    consume=campaigns.CampaignIntegrationTests.consume
    prepare=campaigns.CampaignIntegrationTests.prepare
    tick=campaigns.CampaignIntegrationTests.tick

    def batch(self,**kwargs):
        self.prepare(**kwargs)
        self.judge=BatchJudge()
        self.monitor.campaign_worker=CampaignWorker(self.monitor,self.judge)

    async def queue_only(self):
        with patch.object(self.monitor.campaign_worker,'process',new=AsyncMock()):
            await self.tick()

    async def test_temporary_channel_outage_keeps_job_then_recovers(self):
        self.batch();await self.queue_only()
        self.store.execute("UPDATE partners SET status='unavailable'")
        await self.monitor.campaign_worker.process()
        self.assertEqual(self.store.rows('SELECT status FROM campaign_checks')[0]['status'],'pending')
        self.assertFalse(self.judge.calls)
        self.store.execute("UPDATE partners SET status='active'")
        await self.monitor.campaign_worker.process()
        self.assertEqual(len(self.store.rows('SELECT * FROM posts')),1)

    async def test_unverified_folder_pauses_without_discarding_jobs(self):
        self.batch();await self.queue_only()
        previous=self.monitor.campaign_worker.config
        self.monitor.scope_ready=False
        self.monitor.campaign_worker.config=replace(previous,allowed_channels=(),channel_folder_watch=True)
        await self.monitor.campaign_worker.process()
        self.assertEqual(self.store.rows('SELECT status FROM campaign_checks')[0]['status'],'pending')
        self.monitor.scope_ready=True;self.monitor.campaign_worker.config=previous
        await self.monitor.campaign_worker.process()
        self.assertEqual(len(self.store.rows('SELECT * FROM posts')),1)

    async def test_folder_failure_during_model_request_does_not_discard_or_count(self):
        self.batch();await self.queue_only()
        original=self.judge.compare_many
        def result(content,ids):
            self.monitor.scope_ready=False
            return original(content,ids)
        self.judge.compare_many=result
        await self.monitor.campaign_worker.process()
        self.assertEqual(self.store.rows('SELECT status FROM campaign_checks')[0]['status'],'pending')
        self.assertFalse(self.store.rows('SELECT * FROM posts'))

    async def test_batch_closes_finished_search_only_after_negative_verdict(self):
        self.batch();await self.queue_only()
        self.store.execute('UPDATE placements SET scan_complete=1')
        self.judge.compare_many=lambda content,ids:{i:campaigns.decision(None,'different_campaign') for i in ids}
        await self.monitor.campaign_worker.process()
        self.assertEqual(self.store.rows('SELECT status FROM placements')[0]['status'],'not_found')

    async def test_unavailable_or_incomplete_archive_prevents_false_missing_result(self):
        self.batch();await self.queue_only()
        self.store.execute('UPDATE placements SET scan_complete=1')
        for status in ('pending','error','review','unavailable','archived_no_match'):
            self.store.execute('UPDATE campaign_checks SET status=?',(status,))
            settle_placements(self.monitor.campaign_worker)
            self.assertEqual(self.store.rows('SELECT status FROM placements')[0]['status'],'watching')

    async def test_text_only_post_needs_no_media_and_is_captured(self):
        self.batch(candidates=[engine.message(7,'Только текст: оборудование для предпринимателей в рассрочку')])
        await self.tick()
        self.assertEqual(len(self.store.rows('SELECT * FROM posts')),1)
        self.assertEqual(len(self.capture.calls),1)
        self.assertFalse(any(part['type']=='input_image' for part in self.judge.calls[0][0]))

    async def test_failed_image_download_does_not_become_text_only_negative(self):
        from test_conversation import image_message
        self.batch(candidates=[image_message(7,'Неизвестное объявление',photo=999,peer=200)])
        self.client.attachments[7]=b''
        await self.tick()
        self.assertFalse(self.judge.calls)
        self.assertFalse(self.store.rows('SELECT * FROM posts'))
        self.assertEqual(self.store.rows('SELECT reason FROM campaign_checks')[0]['reason'],'media_unavailable')

    async def test_replay_recovers_text_edit_after_negative_result(self):
        self.batch(candidates=[engine.message(7,'Первоначально это была обычная новость')])
        original=self.judge.compare_many
        self.judge.compare_many=lambda content,ids:{i:campaigns.decision(None,'different_campaign') for i in ids}
        await self.tick()
        self.client.histories[200][0].message='Теперь текст рекламирует запуск лизинга оборудования'
        self.store.set_state('history_reconciliation:1','{}')
        self.store.set_state('campaign_next_request_at','')
        self.judge.compare_many=original
        await self.tick()
        self.assertEqual(len(self.store.rows('SELECT * FROM posts')),1)
        self.assertEqual({r['status'] for r in self.store.rows('SELECT status FROM campaign_checks')},{'no_match','match'})
        self.assertEqual(self.store.rows('SELECT last_message_id FROM partners')[0]['last_message_id'],7)

    async def test_unchanged_replay_does_not_repeat_ai_request(self):
        self.batch()
        original=self.judge.compare_many
        def negative(content,ids):
            original(content,ids)
            return {i:campaigns.decision(None,'different_campaign') for i in ids}
        self.judge.compare_many=negative
        await self.tick()
        self.store.set_state('history_reconciliation:1','{}')
        self.store.set_state('campaign_next_request_at','')
        await self.tick()
        self.assertEqual(len(self.judge.calls),1)
        self.assertEqual(len(self.store.rows('SELECT * FROM campaign_checks')),1)

    async def test_replay_checkpoint_survives_interruption_without_moving_new_cursor(self):
        self.batch(candidates=[engine.message(i,'Неизвестная публикация '+str(i)) for i in range(7,11)])
        await self.queue_only()
        self.monitor.config=replace(self.monitor.config,batch_size=2)
        self.store.set_state('history_reconciliation:1','{}')
        partner=self.store.rows('SELECT * FROM partners')[0]
        original=self.monitor.queue_campaign
        async def interrupted(partner,members,placements):
            if members[0].id==8:raise OSError('network')
            await original(partner,members,placements)
        with patch.object(self.monitor,'queue_campaign',new=interrupted):
            with self.assertRaises(OSError):await reconcile_channel(self.monitor,partner,self.client.partner)
        self.assertEqual(json.loads(self.store.state('history_reconciliation:1'))['cursor'],7)
        restarted=Monitor(self.monitor.config,self.store,self.client,self.capture)
        await reconcile_channel(restarted,partner,self.client.partner)
        await reconcile_channel(restarted,partner,self.client.partner)
        self.assertEqual(self.store.rows('SELECT last_message_id FROM partners')[0]['last_message_id'],10)
        self.assertEqual(len(self.store.rows('SELECT * FROM campaign_checks')),4)
        self.assertTrue(json.loads(self.store.state('history_reconciliation:1'))['completed_at'])


class MeasurementRecoveryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=engine.MonitorTests.asyncSetUp
    asyncTearDown=engine.MonitorTests.asyncTearDown

    async def setup_post(self,fail=False):
        self.client=engine.FakeClient([engine.message(1,'@partner_one',peer=100)],[engine.message(7,'BRB',views=25)])
        self.capture=engine.FakeCapture(fail=fail)
        self.monitor=Monitor(self.config,self.store,self.client,self.capture)
        with patch('partner_monitor.engine.asyncio.sleep',new=AsyncMock()):await self.monitor.tick()
        return self.store.rows('SELECT * FROM partners')[0]

    async def test_transient_unavailable_measurement_is_retried_with_actual_counter(self):
        partner=await self.setup_post()
        due=(engine.NOW-timedelta(minutes=1)).isoformat()
        self.store.execute("UPDATE measurement_jobs SET due_at=? WHERE due_key='24h'",(due,))
        saved=self.client.histories[200];self.client.histories[200]=[]
        await self.monitor.measure(partner,self.client.partner)
        self.assertEqual(self.store.rows("SELECT status FROM measurement_jobs WHERE due_key='24h'")[0]['status'],'unavailable')
        self.client.histories[200]=saved;saved[0].views=90
        await self.monitor.measure(partner,self.client.partner)
        self.assertEqual(self.store.rows("SELECT status FROM measurement_jobs WHERE due_key='24h'")[0]['status'],'done')
        self.assertEqual(self.store.rows("SELECT views FROM snapshots WHERE due_key='24h'")[0]['views'],90)
        self.assertEqual(self.store.rows("SELECT views FROM snapshots WHERE due_key='initial'")[0]['views'],25)

    async def test_screenshots_retry_beyond_three_attempts_with_backoff_and_original_counter(self):
        partner=await self.setup_post(fail=True)
        self.monitor._attempted.clear()
        await self.monitor.measure(partner,self.client.partner)
        self.assertEqual(len(self.capture.calls),1)  # No premature busy retry.
        for _ in range(3):
            self.store.execute("UPDATE snapshots SET next_capture_at='2000-01-01T00:00:00+00:00'")
            self.monitor._attempted.clear();await self.monitor.measure(partner,self.client.partner)
        self.capture.fail=False;self.client.histories[200][0].views=200
        self.store.execute("UPDATE snapshots SET next_capture_at='2000-01-01T00:00:00+00:00'")
        self.monitor._attempted.clear();await self.monitor.measure(partner,self.client.partner)
        row=self.store.rows('SELECT * FROM snapshots')[0]
        self.assertEqual(row['capture_attempts'],5)
        self.assertEqual(row['views'],25)
        self.assertEqual(row['screenshot_status'],'ok')

    async def test_channel_transport_error_does_not_starve_next_channel(self):
        client=engine.FakeClient([engine.message(1,'@partner_one',peer=100),engine.message(2,'@partner_two',peer=100)])
        client.entities['partner_one']=OSError('offline')
        client.entities['partner_two']=engine.channel(201,'partner_two')
        client.histories[201]=[engine.message(10,'BRB',peer=201)]
        monitor=Monitor(self.config,self.store,client,engine.FakeCapture())
        with patch('partner_monitor.engine.asyncio.sleep',new=AsyncMock()):await monitor.tick()
        self.assertEqual(len(self.store.rows('SELECT * FROM posts')),1)
        self.assertTrue(self.store.state('monitor_tick_completed_at',''))

    async def test_read_timeout_is_bounded_without_bypassing_flood_wait(self):
        raw=engine.FakeClient()
        entered=asyncio.Event()
        async def stuck(key):
            entered.set();await asyncio.Event().wait()
        raw.get_entity=stuck
        client=TelegramAccess(raw);client.request_timeout=.02
        with self.assertRaises(asyncio.TimeoutError):await client.get_entity('partner_one')
        self.assertTrue(entered.is_set())
        self.assertEqual(client.blocked_until,0)

    async def test_source_chat_failure_does_not_stop_existing_post_measurement(self):
        await self.setup_post()
        before=self.store.rows('SELECT last_checked_at FROM posts')[0]['last_checked_at']
        self.client.entities['@work_group']=OSError('source offline')
        with patch('partner_monitor.engine.asyncio.sleep',new=AsyncMock()):await self.monitor.tick()
        self.assertGreater(self.store.rows('SELECT last_checked_at FROM posts')[0]['last_checked_at'],before)
        self.assertEqual(self.store.state('source_intake_error'),'OSError')

    async def test_twenty_hour_schedule_replaces_current_picture_and_keeps_initial_history(self):
        from pathlib import Path
        from PIL import Image
        from partner_monitor.measurements import ensure_jobs,ready_jobs
        from partner_monitor.report_view import current_picture
        partner=await self.setup_post()
        published=engine.NOW-timedelta(hours=21)
        self.store.execute('UPDATE posts SET published_at=?,first_seen_at=?',(published.isoformat(),(published+timedelta(minutes=1)).isoformat()))
        self.monitor.config=replace(self.monitor.config,milestones_hours=(20,48))
        first=self.root/'initial.png';second=self.root/'20h.png'
        Image.new('RGB',(20,20),'red').save(first);Image.new('RGB',(20,20),'blue').save(second)
        self.store.execute('UPDATE snapshots SET screenshot_path=?',(str(first),))
        async def capture(*args):return engine.CaptureResult(str(second),'ok')
        self.capture.capture=capture
        await self.monitor.measure(partner,self.client.partner)
        checks=self.store.rows('SELECT * FROM snapshots')
        self.assertEqual({s['due_key'] for s in checks},{'initial','20h'})
        due=self.store.rows("SELECT due_at FROM measurement_jobs WHERE due_key='20h'")[0]['due_at']
        self.assertEqual(due,(published+timedelta(hours=20)).isoformat())
        self.assertEqual(current_picture(checks)['screenshot_path'],str(second))
        self.assertTrue(first.is_file())
        self.assertEqual(self.store.rows("SELECT status FROM measurement_jobs WHERE due_key='24h'")[0]['status'],'superseded')
        self.store.execute("UPDATE measurement_jobs SET due_at=? WHERE due_key='48h'",((engine.NOW-timedelta(minutes=1)).isoformat(),))
        self.client.histories[200]=[]
        await self.monitor.measure(partner,self.client.partner)
        self.assertEqual(current_picture(self.store.rows('SELECT * FROM snapshots'))['screenshot_path'],str(second))
        self.assertEqual(self.store.rows("SELECT status FROM measurement_jobs WHERE due_key='48h'")[0]['status'],'unavailable')
        self.assertFalse(ready_jobs(self.store,1))


class EditDeliveryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=realtime.RealtimeTests.asyncSetUp
    asyncTearDown=realtime.RealtimeTests.asyncTearDown
    add_brief=realtime.RealtimeTests.add_brief

    async def test_text_edit_can_match_after_original_message_did_not(self):
        await self.live.new_message(SimpleNamespace(message=engine.message(7,'Обычная новость')))
        self.assertFalse(self.capture.calls)
        await self.live.edited(SimpleNamespace(message=engine.message(7,'Test — текстовое объявление')))
        self.assertEqual(len(self.capture.calls),1)
        self.assertEqual(self.store.rows('SELECT last_message_id FROM partners')[0]['last_message_id'],0)

    async def test_channel_difference_delivers_edited_text(self):
        await self.live.deliver_deletions([types.UpdateEditChannelMessage(message=engine.message(7,'Test edited'),pts=11,pts_count=1)])
        self.assertEqual(len(self.capture.calls),1)


class SourceEditTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=forwarded.ForwardedIntakeTests.asyncSetUp
    asyncTearDown=forwarded.ForwardedIntakeTests.asyncTearDown
    consume=forwarded.ForwardedIntakeTests.consume

    async def test_text_only_forwarded_reference_and_edits_keep_original_window_and_revision(self):
        from partner_monitor.message_content import message_content
        source=engine.message(1,'Исходный текст рекламы',peer=100)
        source.fwd_from=SimpleNamespace(from_id=None)
        self.consume(source)
        before=self.store.rows('SELECT * FROM placements')[0]
        self.store.execute('UPDATE partners SET last_message_id=777')
        source.message='Исправленный текст рекламы'
        self.intake.refresh_forwarded(-1000000000100,source,message_content(source))
        self.assertEqual(len(self.store.rows('SELECT * FROM intake_drafts')),1)
        self.assertEqual(len(self.store.rows('SELECT * FROM source_revisions')),1)
        after=self.store.rows('SELECT * FROM placements')[0]
        self.assertEqual(after['ends_at'],before['ends_at'])
        self.assertEqual(after['source_text'],source.message)
        self.store.execute('UPDATE partners SET last_message_id=888')
        self.intake.refresh_forwarded(-1000000000100,source,message_content(source))
        self.assertEqual(self.store.rows('SELECT last_message_id FROM partners')[0]['last_message_id'],888)

    async def test_periodic_source_recheck_recovers_edit_missed_during_restart(self):
        from partner_monitor.reconciliation import refresh_source_materials
        source=engine.message(1,'Исходный текст рекламы',peer=100)
        source.fwd_from=SimpleNamespace(from_id=None);self.consume(source)
        source.message='Правка, пока монитор был отключён'
        client=engine.FakeClient([source])
        monitor=Monitor(self.config,self.store,client,engine.FakeCapture())
        monitor.source_peer_id=-1000000000100
        await refresh_source_materials(monitor,client.group)
        self.assertEqual(self.store.rows('SELECT source_text FROM placements')[0]['source_text'],source.message)
        self.assertEqual(len(self.store.rows('SELECT * FROM source_revisions')),1)
