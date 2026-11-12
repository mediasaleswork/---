import hashlib
import json
from pathlib import Path
import tempfile
from datetime import timedelta
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock,Mock,patch
from telethon import types
from openpyxl import Workbook

from partner_monitor.candidate_archive import CandidateArchive
from partner_monitor.candidate_drive import REPORT_RESERVE_BYTES, ReportStorageReserve, upload_file
from partner_monitor.campaigns import candidate_signature
from partner_monitor.review_queue import export_sheet
import test_campaign_queue as queue
import test_campaigns as base
from test_engine import NOW,message


class IntakeTests(IsolatedAsyncioTestCase):
    asyncSetUp=queue.QueueTests.asyncSetUp
    asyncTearDown=queue.QueueTests.asyncTearDown
    consume=queue.QueueTests.consume
    prepare=queue.QueueTests.prepare
    tick=queue.QueueTests.tick
    batch=queue.QueueTests.batch
    queue_only=queue.QueueTests.queue_only

    async def test_cache_expiry_replay_edit_and_prior_month_report(self):
        from openpyxl import load_workbook
        from PIL import Image
        from partner_monitor.cache_retention import sweep
        from partner_monitor.campaigns import enqueue
        from partner_monitor.reports import export_xlsx

        current = message(8, 'Supported edit window')
        self.batch(candidates=[current])
        await self.queue_only()
        previous = (NOW.replace(day=1) - timedelta(days=1)).replace(day=15)
        old = message(7, 'Last month', date=previous)
        old.media = types.MessageMediaDocument(document=types.Document(
            id=888, access_hash=2, file_reference=b'test', date=previous, mime_type='video/mp4',
            size=5, dc_id=2, attributes=[types.DocumentAttributeFilename('fixture.mp4')]))
        partner = self.store.rows('SELECT * FROM partners')[0]
        placements = self.store.rows('SELECT * FROM placements')
        await self.monitor.record_post(partner, old.peer_id, [old], 'test historical match', placements)
        picture = self.root / 'evidence.png'
        Image.new('RGB',(100,120),'blue').save(picture)
        self.store.execute('UPDATE snapshots SET screenshot_status=?,screenshot_path=?', ('ok',str(picture)))
        archive = self.store.candidate_archive
        path = archive.path('document:888')
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(b'video')
        md5 = hashlib.md5(path.read_bytes()).hexdigest()
        self.store.execute("UPDATE candidate_files SET size=5,md5=?,saved=?,drive_id='remote' WHERE key='document:888'",
                           (md5,previous.isoformat()))
        cfg = SimpleNamespace(runtime_dir=path.parent.parent,db_path=self.root/'monitor.sqlite3',drive_folder_id='folder')
        tag = hashlib.sha256(('document:888:'+md5).encode()).hexdigest()
        remote = dict(id='remote',parents=['folder'],trashed=False,size='5',md5Checksum=md5,
                      capabilities={'canDownload':True},appProperties={'msbrb_candidate':tag})
        reporter = Mock(drive_folder_id='folder')
        reporter._json.return_value = remote

        def report():
            book = load_workbook(export_xlsx(*self.store.dump(), self.root/'report.xlsx', month=previous.strftime('%Y-%m')))
            try:
                sheet = book['Реестр публикаций']
                values = list(sheet.values)
                self.assertIn('https://t.me/partner_one/7', str(values))
                self.assertGreater(len(book['Отчёт']._images), 0)
                return values
            finally:
                book.close()

        before = report()
        with patch('partner_monitor.cache_retention.verify_private_folder'):
            self.assertEqual(sweep(cfg,1,reporter=reporter)['bytes'],5)
            self.assertTrue(path.exists())
            remote['trashed']=True
            self.assertEqual(sweep(cfg,1,reporter=reporter,apply=True)['verified'],0)
            self.assertTrue(path.exists())
            remote['trashed']=False
            self.assertEqual(sweep(cfg,1,reporter=reporter,apply=True)['verified'],1)
        self.assertFalse(path.exists())
        def counts():
            return tuple(self.store.rows('SELECT COUNT(*) n FROM '+t)[0]['n'] for t in
                         ('candidate_versions','candidate_files','posts','placement_posts','campaign_checks'))
        saved = counts()
        enqueue(self.store,self.config,partner,[old],placements)
        self.assertEqual(counts(),saved)
        self.assertFalse(path.exists())
        current.message='Edited inside the supported period'
        enqueue(self.store,self.config,partner,[current],placements)
        self.assertEqual(counts()[0],saved[0]+1)
        self.assertEqual(counts()[4],saved[4]+1)
        self.assertEqual(report(),before)

    async def test_deleted_photo_only_uses_saved_pixels_after_restart(self):
        msg=base.image_message(7,'',710,200)
        self.batch(candidates=[msg])
        await self.queue_only()
        self.client.attachments[710]=base.png('blue')
        self.client.histories[200]=[]
        archive=self.store.candidate_archive
        while await archive.process_one():pass
        self.assertEqual(archive.path('photo:710').read_bytes(),base.png('blue'))
        self.assertEqual(self.store.rows("SELECT count(*) n FROM candidate_files WHERE saved!=''")[0]['n'],2)
        self.store.candidate_archive=CandidateArchive(self.monitor)
        with patch.object(queue.BatchJudge,'compare_archived',create=True,return_value=base.decision()) as judge:
            await self.monitor.campaign_worker.process()
        self.assertTrue(any(c['type']=='input_image' for c in judge.call_args.args[0]))
        job=self.store.rows('SELECT * FROM campaign_checks')[0]
        self.assertEqual((job['status'],json.loads(job['decision'])['evidence_type']),('review','observed_archive'))
        self.store.execute("UPDATE candidate_files SET drive_id='private-file-id' WHERE key='photo:710'")
        partners,*_=self.store.dump()
        sheet=Workbook().active
        export_sheet(sheet,partners,'UTC')
        self.assertEqual(sheet['P2'].hyperlink.target,'https://drive.google.com/open?id=private-file-id')
        self.assertIn('1 из 1',sheet['N2'].value)
        self.assertFalse(self.store.rows('SELECT * FROM posts'))

    async def test_partial_document_retries_with_original_id_and_album_edit_keeps_both(self):
        self.batch(candidates=[message(7,'Начало')])
        await self.queue_only()
        partner=self.store.rows('SELECT * FROM partners')[0]
        photo=base.image_message(9,'Первая подпись',790,200)
        doc=message(10,'Вторая подпись')
        doc.media=types.MessageMediaDocument(document=types.Document(id=888,access_hash=2,file_reference=b'secret-ref',
            date=NOW,mime_type='video/mp4',size=12,dc_id=2,attributes=[types.DocumentAttributeFilename('video.mp4')]))
        archive=self.store.candidate_archive
        for caption in ('Первая подпись','Изменённая подпись'):
            photo.message=caption
            archive.observe(partner,[photo,doc],candidate_signature([photo,doc]))
        current=self.store.rows("SELECT * FROM candidate_files WHERE key='document:888'")[0]
        self.monitor.client.download_original=AsyncMock(side_effect=lambda obj,file,size:file.write(b'short'))
        with self.assertRaises(OSError):await archive.capture(current)
        self.assertFalse(archive.path('document:888').exists())
        self.assertFalse(archive.path('document:888').with_suffix('.part').exists())
        self.assertFalse(self.store.rows("SELECT saved FROM candidate_files WHERE key='document:888'")[0]['saved'])
        self.client.histories[200]=[doc]
        restored=CandidateArchive(self.monitor)
        self.monitor.client.download_original=AsyncMock(side_effect=lambda obj,file,size:file.write(b'video binary'))
        await restored.capture(current)
        self.assertEqual(restored.path('document:888').read_bytes(),b'video binary')
        manifests=[r['manifest'] for r in self.store.rows("SELECT * FROM candidate_versions WHERE manifest LIKE '%video.mp4%'")]
        self.assertEqual(len(manifests),2)
        self.assertNotIn('secret-ref',''.join(manifests))
        self.assertEqual(self.store.rows("SELECT count(*) n FROM candidate_files WHERE key='document:888'")[0]['n'],1)

    async def test_restart_does_not_substitute_edit_with_another_attachment(self):
        first=base.image_message(7,'До',76,200)
        self.batch(candidates=[first]);await self.queue_only()
        self.client.histories[200]=[base.image_message(7,'После',77,200)]
        restored=CandidateArchive(self.monitor)
        self.assertTrue(await restored.process_one())
        row=self.store.rows("SELECT * FROM candidate_files WHERE key='photo:76'")[0]
        self.assertFalse(row['saved'])
        self.assertTrue(row['retry'])
        self.assertFalse(restored.path('photo:76').exists())

    async def test_new_photo_does_not_wait_for_history_backlog(self):
        first=base.image_message(7,'История',91,200)
        self.batch(candidates=[first]);await self.queue_only()
        partner=self.store.rows('SELECT * FROM partners')[0]
        self.store.execute('DELETE FROM candidate_fast')
        archive=self.store.candidate_archive
        for mid in range(8,20):
            m=base.image_message(mid,'Старый материал',100+mid,200)
            m.date=NOW-timedelta(days=1)
            archive.observe(partner,[m],candidate_signature([m]))
        latest=base.image_message(30,'Свежая публикация',133,200)
        archive.observe(partner,[latest],candidate_signature([latest]))
        self.assertTrue(await archive.process_one(text_only=True))
        self.assertTrue(self.store.rows("SELECT 1 FROM candidate_files WHERE key LIKE 'post:%' AND saved!=''"))
        self.assertFalse(self.store.rows("SELECT 1 FROM candidate_files WHERE key LIKE 'photo:%' AND saved!=''"))
        self.client.attachments[133]=base.png('green')
        self.assertTrue(await archive.process_one(realtime=True))
        self.assertEqual(archive.path('photo:133').read_bytes(),self.client.attachments[133])
        self.assertFalse(archive.path('photo:91').exists())


class UploadTests(TestCase):
    def test_candidate_cannot_use_space_needed_to_publish_confirmed_posts(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'sample'
            path.write_bytes(b'video')
            row = {'key':'document:42', 'size':5, 'md5':hashlib.md5(b'video').hexdigest()}
            reporter = SimpleNamespace(drive_folder_id='private', session=Mock(), _json=Mock())
            reporter._json.side_effect = [{'files':[]}, {'storageQuota':{'limit':str(REPORT_RESERVE_BYTES+4),'usage':'0'}}]
            with self.assertRaises(ReportStorageReserve):
                upload_file(reporter, path, row)
            reporter.session.request.assert_not_called()
            self.assertEqual(path.read_bytes(), b'video')

    def test_remote_id_requires_matching_content_and_reuses_an_existing_verified_upload(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'blob'
            path.write_bytes(b'abcdef')
            row={'size':6,'md5':hashlib.md5(b'abcdef').hexdigest(),'name':'test.pdf','mime':'application/pdf','key':'document:1'}
            remote={'id':'validid','size':'6','md5Checksum':row['md5']}
            reporter=SimpleNamespace(_json=Mock(return_value={'files':[]}),session=Mock(),drive_folder_id='private',in_shared_drive=True)
            start=Mock(headers={'Location':'https://www.googleapis.com/upload/session'})
            end=Mock();end.json.return_value={**remote,'md5Checksum':'wrong'}
            reporter.session.request.side_effect=[start,end]
            with self.assertRaises(ValueError):upload_file(reporter,path,row)
            reporter._json.return_value={'files':[remote]}
            self.assertEqual(upload_file(reporter,path,row),'validid')
            self.assertEqual(reporter.session.request.call_count,2)
