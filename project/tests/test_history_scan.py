from datetime import timedelta
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from openpyxl import load_workbook
from telethon import types
from partner_monitor.history_scan import check_publication,history_page
from partner_monitor.parsing import PartnerReference
from partner_monitor.unified_report import export_unified
import test_campaign_queue as queue
from test_engine import message,NOW


class HistoryTests(IsolatedAsyncioTestCase):
    asyncSetUp=queue.QueueTests.asyncSetUp
    asyncTearDown=queue.QueueTests.asyncTearDown
    consume=queue.QueueTests.consume
    prepare=queue.QueueTests.prepare
    tick=queue.QueueTests.tick
    batch=queue.QueueTests.batch
    queue_only=queue.QueueTests.queue_only

    async def test_historical_repeat_album_other_channel_edit_and_second_export(self):
        self.batch(candidates=[message(7,'новости')])
        await self.queue_only()
        partner=self.store.rows('SELECT * FROM partners')[0]
        placement=self.store.rows('SELECT * FROM placements')[0]
        text='Компания Alpha Lease начала работу. Оборудование доступно в рассрочку.'
        earlier=NOW-timedelta(days=5)
        same=message(500,text,date=earlier,grouped_id=88)
        other=message(501,text,date=earlier,grouped_id=88)
        repeat=message(502,text,date=earlier+timedelta(days=1))
        # Exact material may precede the start of the normal forwarding-day window.
        for members in ([same,other],[repeat],[same,other]):
            await check_publication(self.monitor,partner,members,[placement],'test-history')
        self.assertEqual([r['message_id'] for r in self.store.rows('SELECT message_id FROM posts ORDER BY message_id')],[500,501,502])
        alias=self.store.add_partner(PartnerReference('another'),1,1,enqueue=False)
        self.store.execute('UPDATE partners SET channel_id=301 WHERE id=?',(alias,))
        second_partner=self.store.rows('SELECT * FROM partners WHERE id=?',(alias,))[0]
        second=message(500,text,peer=301,date=earlier+timedelta(days=2))
        await self.monitor.record_post(second_partner,types.PeerChannel(301),[second],'historical')
        same.message='Отредактированный текст'
        await check_publication(self.monitor,partner,[same,other],[placement],'test-history')
        self.assertEqual(self.store.rows('SELECT text FROM posts WHERE telegram_channel_id=200 AND message_id=500')[0]['text'],same.message)
        self.assertEqual(self.store.rows("SELECT count(*) n FROM snapshots WHERE due_key='initial'")[0]['n'],4)
        self.assertFalse(self.store.rows("SELECT id FROM snapshots WHERE due_key!='initial'"))
        self.assertTrue(self.store.rows("SELECT 1 FROM measurement_jobs WHERE status='missed'"))
        for _ in range(2):
            partners,posts,snaps=self.store.dump()
            path=export_unified(partners,list(reversed(posts)),snaps,self.root/'history.xlsx',compact=True)
            book=load_workbook(path)
            try:
                urls=[row[3].value for row in list(book['Реестр публикаций'].rows)[1:]]
                self.assertEqual([u.split('/')[-1] for u in urls],['500','502','501','500'])
                self.assertEqual(len(set(urls)),4)
            finally:book.close()

    async def test_finished_history_page_is_not_read_again_after_restart(self):
        from dataclasses import replace
        self.batch(candidates=[message(7,'в периоде')]);await self.queue_only()
        self.monitor.config=replace(self.monitor.config,forwarded_intake=True)
        self.monitor.scope_ready=True
        partner=self.store.rows('SELECT * FROM partners')[0]
        req={'id':'r','start':(NOW-timedelta(days=3)).isoformat(),'end':(NOW+timedelta(minutes=1)).isoformat(),'channels':[partner]}
        async def page(entity,**kw):
            if entity.id==200:yield message(7,'в периоде')
            if entity.id==100:yield message(1,'переписка',peer=100)
        with patch.object(self.monitor.client,'iter_messages',side_effect=page):
            await history_page(self.monitor,req)
            await history_page(self.monitor,req)
            self.assertFalse(await history_page(self.monitor,req))
        self.assertEqual(self.store.rows('SELECT SUM(done) n FROM history_pages')[0]['n'],2)
        self.assertEqual(len(self.store.rows('SELECT * FROM history_seen')),2)
