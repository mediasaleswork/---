"""Preserve each observed candidate before its Telegram message can disappear."""
import asyncio
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
import re
import shutil

from telethon import errors, types

from .message_content import message_content
from .parsing import PartnerReference
from .storage import utcnow

log = logging.getLogger(__name__)
RESERVE = 128 * 1024**2


def packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def structured(message):
    media = getattr(message, 'media', None)
    kind = type(media).__name__
    if media is None or kind in {'MessageMediaEmpty','MessageMediaPhoto','MessageMediaDocument'}:
        return None
    def clean(value):
        if isinstance(value, dict):
            return {k:clean(v) for k,v in value.items() if k not in
                    {'access_hash','file_reference','key','iv','bytes','photos','documents','thumbs','cached_page','results','views'}
                    and not isinstance(v,bytes)}
        if isinstance(value,list):
            return [clean(v) for v in value if not isinstance(v,bytes)]
        if isinstance(value,datetime):return value.isoformat()
        if isinstance(value,bytes):return None
        return value
    return clean(media.to_dict()) if hasattr(media,'to_dict') else {'_':kind}


def attachments(message):
    """Return original photo/document objects, including album and rich-message media."""
    rich=getattr(message,'rich_message',None)
    objects={}
    for kind,plural in (('photo','photos'),('document','documents')):
        for item in [getattr(message,kind,None),*(getattr(rich,plural,None) or [])]:
            if getattr(item,'id',None):objects[f'{kind}:{item.id}']=item
    for key in message_content(message).media:
        obj=objects.get(key)
        name=next((a.file_name for a in getattr(obj,'attributes',[]) if getattr(a,'file_name',None)),None)
        name=(name or (key.replace(':','-')+('.jpg' if key.startswith('photo:') else ''))).replace('\\','/').split('/')[-1]
        name=re.sub(r'[\x00-\x1f\x7f]','_',name)[:170] or 'Вложение'
        mime=getattr(obj,'mime_type',None) or ('image/jpeg' if key.startswith('photo:') else 'application/octet-stream')
        if not re.fullmatch(r'[\w.+-]+/[\w.+-]+',mime):mime='application/octet-stream'
        yield key,obj,name,mime,int(getattr(obj,'size',0) or 0)


def later(seconds):
    return (datetime.now(timezone.utc)+timedelta(seconds=seconds)).isoformat()


def files_for_job(store,job):
    return store.rows('''SELECT f.* FROM candidate_parts a JOIN candidate_files f ON f.key=a.asset
         WHERE a.partner_id=? AND a.signature=? ORDER BY a.position''',(job['partner_id'],job['candidate_signature']))


def image_parts(archive,files):
    from .campaigns import image_input,CampaignAPIError
    result=[]
    for f in files:
        if len(result)>=2:break
        if not f['mime'].startswith('image/') or not f['saved'] or f['size']>16*1024**2:continue
        try:
            path=archive.path(f['key'])
            if not path.is_file():continue
            data=path.read_bytes()
            if len(data)!=f['size'] or hashlib.md5(data).hexdigest()!=f['md5']:continue
            result.append(image_input(data))
        except (CampaignAPIError,OSError,ValueError):continue
    return result


class CandidateArchive:
    def __init__(self, monitor):
        self.monitor,self.store=monitor,monitor.store
        self.root=monitor.config.runtime_dir/'candidate-files'
        self.targets=OrderedDict()
        self.busy=set()
        self.reserved={}
        self.wake=asyncio.Event()
        self.reporter=None

    def path(self,key):
        return self.root/hashlib.sha256(key.encode()).hexdigest()

    def observe(self,partner,messages,signature):
        messages=sorted(messages,key=lambda m:m.id)
        p=partner['id']
        existing=self.store.rows('SELECT 1 FROM candidate_versions WHERE partner_id=? AND signature=?',(p,signature))
        if existing:
            pending=self.store.rows("SELECT a.asset FROM candidate_parts a JOIN candidate_files f ON f.key=a.asset WHERE a.partner_id=? AND a.signature=? AND f.saved=''",(p,signature))
            if not pending:return
            needed={r['asset'] for r in pending}
        else:needed=None
        target=partner.get('username') or 'c/'+str(partner.get('channel_id') or partner.get('private_channel_id'))
        manifest={'channel_id':partner.get('channel_id'),'channel':partner.get('title') or target,
                  'url':f'https://t.me/{target}/{messages[0].id}','observed_at':utcnow(),'messages':[]}
        files={}
        for m in messages:
            content=message_content(m)
            ms={'id':m.id,'published':m.date.isoformat(),'text':content.text,'urls':content.urls,'files':[]}
            if (extra:=structured(m)) is not None:ms['telegram_media']=extra
            if getattr(m,'edit_date',None):ms['edited']=m.edit_date.isoformat()
            for key,obj,name,mime,size in attachments(m):
                ms['files'].append({'key':key,'name':name,'mime':mime,'expected_bytes':size})
                files.setdefault(key,(name,mime,size,m.id))
                if obj is not None and (needed is None or key in needed):
                    self.targets[key]=obj
                    self.targets.move_to_end(key)
            manifest['messages'].append(ms)
        while len(self.targets)>512:self.targets.popitem(last=False)
        if not existing:
            meta='post:'+hashlib.sha256(f'{p}:{signature}'.encode()).hexdigest()
            label=partner.get('username') or str(partner.get('channel_id') or p)
            all_files=[(meta,(f'{label} — пост {messages[0].id} — {signature[:8]}.txt','text/plain',0,messages[0].id)),*files.items()]
            with self.store.transaction():
                self.store.execute('INSERT INTO candidate_versions VALUES(?,?,?)',(p,signature,packed(manifest)))
                for pos,(key,(name,mime,size,mid)) in enumerate(all_files):
                    self.store.execute('INSERT OR IGNORE INTO candidate_files(key,name,mime,expected) VALUES(?,?,?,?)',(key,name,mime,size))
                    self.store.execute('INSERT INTO candidate_parts VALUES(?,?,?,?,?)',(p,signature,pos,mid,key))
        self.wake.set()

    def can_fetch(self,partner):
        return (getattr(self.monitor,'scope_ready',True) and partner['status']=='active'
                and self.monitor.config.permits(PartnerReference(partner['username'],private_channel_id=partner.get('private_channel_id'))))

    async def original(self,key,origins):
        for item in origins:
            if not self.can_fetch(item):continue
            if key in self.targets:return self.targets[key]
            entity=await self.monitor.client.get_entity(types.PeerChannel(item['channel_id']))
            if entity.id!=item['channel_id']:continue
            msg=await self.monitor.client.get_messages(entity,ids=item['mid'])
            if not isinstance(msg,types.Message) or msg.id!=item['mid']:continue
            for found,obj,*_ in attachments(msg):
                if found==key and obj is not None:return obj
        raise FileNotFoundError('Telegram не вернул исходное вложение')

    def make_room(self,size):
        # Only evict a local cache copy after Drive confirmed its bytes and checksum.
        required=max(size,1024**2)+RESERVE+sum(self.reserved.values())
        if shutil.disk_usage(self.root).free>=required:return
        for item in self.store.rows("SELECT key FROM candidate_files WHERE drive_id!='' ORDER BY saved"):
            if item['key'] in self.busy:continue
            self.path(item['key']).unlink(missing_ok=True)
            if shutil.disk_usage(self.root).free>=required:return
        raise OSError('Недостаточно места; неподтверждённые копии сохранены')

    async def capture(self,row):
        key=row['key'];path=self.path(key);temp=path.with_suffix('.part')
        self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        origins=self.store.rows('''SELECT p.*,a.mid,v.manifest FROM candidate_parts a JOIN partners p ON p.id=a.partner_id
            JOIN candidate_versions v ON v.partner_id=a.partner_id AND v.signature=a.signature
            WHERE a.asset=? ORDER BY a.rowid DESC''',(key,))
        self.make_room(row['expected'])
        self.reserved[key]=max(row['expected'],1024**2)
        try:
            recovered=path.is_file()
            if recovered:
                # Only complete, validated downloads ever receive the final name. A crash
                # between the rename and SQLite commit must not make us ask Telegram again.
                pass
            elif key.startswith('post:'):
                manifest=json.loads(origins[0]['manifest'])
                body=[manifest['channel'],manifest['url'],'Сохранено UTC: '+manifest['observed_at']]
                for m in manifest['messages']:
                    body.extend(['',f"Сообщение {m['id']} · {m['published']}",m['text'],*m['urls']])
                    body.extend('Вложение: '+f['name']+' ('+f['key']+')' for f in m['files'])
                    if m.get('telegram_media'):body.append(json.dumps(m['telegram_media'],ensure_ascii=False,indent=2))
                temp.write_bytes('\n'.join(body).encode('utf-8-sig'))
            else:
                media=await self.original(key,origins)
                with temp.open('wb') as stream:
                    await self.monitor.client.download_original(media,stream,row['expected'])
            ready=path if recovered else temp
            size=ready.stat().st_size
            if size==0 or row['expected'] and size!=row['expected']:raise OSError('Неполное вложение')
            def checksum():
                if key.startswith('photo:'):
                    from PIL import Image
                    with Image.open(ready) as im:im.verify()
                with ready.open('rb+') as f:
                    os.fsync(f.fileno())
                    return hashlib.file_digest(f,'md5').hexdigest()
            md5=await asyncio.to_thread(checksum)
            if not recovered:os.replace(temp,path)
            self.store.execute("UPDATE candidate_files SET saved=?,size=?,md5=?,retry='',error='' WHERE key=?",(utcnow(),size,md5,key))
            self.targets.pop(key,None)
            self.wake.set()
        finally:
            self.reserved.pop(key,None)
            temp.unlink(missing_ok=True)

    async def process_one(self,upload=False):
        cfg=self.monitor.config
        if upload and (not cfg.google_credentials or not cfg.drive_folder_id):return False
        if not upload and not getattr(self.monitor,'scope_ready',True):return False
        where="saved!='' AND drive_id='' AND upload_retry<=?" if upload else "saved='' AND retry<=?"
        rows=self.store.rows('SELECT * FROM candidate_files WHERE '+where+
                             ' ORDER BY '+('saved' if upload else "(key LIKE 'post:%'),rowid")+' LIMIT 8',(utcnow(),))
        row=next((r for r in rows if r['key'] not in self.busy),None)
        if not row:return False
        key=row['key'];self.busy.add(key)
        try:
            if upload:
                from .candidate_drive import upload_file
                from .candidate_drive import verify_private_folder
                from .reports import GoogleReporter
                if self.reporter is None:
                    reporter=GoogleReporter(cfg.google_credentials,cfg.spreadsheet_id,cfg.drive_folder_id,cfg.timezone)
                    await asyncio.to_thread(verify_private_folder,reporter)
                    self.reporter=reporter
                remote=await asyncio.to_thread(upload_file,self.reporter,self.path(key),row)
                self.store.execute("UPDATE candidate_files SET drive_id=?,upload_retry='',error='' WHERE key=?",(remote,key))
                self.monitor.changed.set()
            else:
                await self.capture(row)
        except Exception as exc:
            if not upload and isinstance(exc,errors.FileReferenceExpiredError):self.targets.pop(key,None)
            column='upload_retry' if upload else 'retry'
            delay=max(60,int(getattr(exc,'seconds',0) or 0)+1)
            if isinstance(exc,FileNotFoundError):delay=900
            if upload:delay=max(delay,300)
            detail=('Drive: ' if upload else 'Telegram/диск: ')+type(exc).__name__
            self.store.execute('UPDATE candidate_files SET '+column+'=?,error=? WHERE key=?',(later(delay),detail,key))
            log.warning('Архив кандидатов: %s; повтор через %s с',detail,delay)
        finally:self.busy.discard(key)
        return True

    async def run(self,upload=False):
        while True:
            self.wake.clear()
            if await self.process_one(upload):continue
            try:await asyncio.wait_for(self.wake.wait(),timeout=5)
            except asyncio.TimeoutError:pass
