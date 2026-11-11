"""A bounded, restartable replay on the live monitor's authorized Telegram client."""
import asyncio
from datetime import datetime,timedelta,timezone
import json
import logging

from telethon import types,errors,utils
from .campaigns import enqueue
from .campaign_local import conflicting_numbers,equivalent_matches,text_copy,video_ids
from .fallback import offline_match
from .intake import ConversationIntake
from .message_content import combined_content
from .parsing import PartnerReference
from .storage import utcnow

log=logging.getLogger(__name__)


async def check_publication(monitor,partner,members,placements,run):
    store=monitor.store
    ids={m.id for m in members}
    known=store.recorded_members(partner,members)
    if len(known)==len(ids):
        for m in members:
            store.save_post(partner['id'],[m],'historical',partner['username'] or f"c/{partner['channel_id']}",separate=True)
        return
    actual=combined_content(members)
    matches={}
    for placement in placements:
        direct=json.loads(placement['direct_ids'])
        if direct:
            if ids.intersection(direct):matches[placement['id']]='direct_link:ссылка из задания'
            continue
        if conflicting_numbers(actual.text,placement):continue
        match=offline_match(members,placement)
        if match.matched:
            matches[placement['id']]=match.reason
        elif text_copy(actual.text,placement):
            matches[placement['id']]='offline:совпадает полный рекламный текст с небольшими правками'
        elif video_ids(placement['source_text']) & video_ids(actual.text,actual.urls):
            matches[placement['id']]='offline:совпадает идентификатор рекламируемого видео'
    matches=equivalent_matches(matches,placements)
    if len(matches)==1:
        ident,reason=next(iter(matches.items()))
        await monitor.record_post(partner,members[0].peer_id,members,reason,[next(p for p in placements if p['id']==ident)])
        return
    candidates=[p for p in placements if not json.loads(p['direct_ids'])]
    with store.transaction():
        job=enqueue(store,monitor.config,partner,members,candidates)
        if job:store.execute('INSERT OR IGNORE INTO history_checks VALUES(?,?)',(run,job))
    monitor.campaign_wake.set()


async def history_page(monitor,request):
    store=monitor.store
    if not monitor.source_peer_id or not monitor.scope_ready or not monitor.config.forwarded_intake:return False
    run=request['id'];start=datetime.fromisoformat(request['start']);end=datetime.fromisoformat(request['end'])
    if not start.tzinfo or not end.tzinfo or start>end:raise ValueError('Некорректные границы загрузки истории')
    group=monitor.source_peer_id
    store.execute('INSERT OR IGNORE INTO history_pages(run,peer) VALUES(?,?)',(run,group))
    tasks=store.rows('SELECT * FROM history_pages WHERE run=? AND done=0 AND retry<=? ORDER BY checked,peer LIMIT 1',(run,utcnow()))
    if not tasks:return False
    task=tasks[0];peer=task['peer'];is_group=peer==group
    try:
        if is_group:
            entity=await monitor.client.get_entity(monitor.config.source_group)
            if utils.get_peer_id(entity)!=group or (monitor.config.source_title and entity.title.strip().casefold()!=monitor.config.source_title.strip().casefold()):
                raise ValueError('Не совпала рабочая группа')
            intake=ConversationIntake(store,monitor.config,historical=True)
            options={'reverse':True,'min_id':task['cursor']}
            if not task['cursor']:options['offset_date']=start-timedelta(seconds=1)
        else:
            partner=store.rows('SELECT * FROM partners WHERE id=?',(task['partner_id'],))[0]
            if not monitor.config.permits(PartnerReference(partner['username'],private_channel_id=partner.get('private_channel_id'))):
                raise ValueError('Канал больше не в подключённой папке')
            entity=await monitor.client.get_entity(types.PeerChannel(peer))
            if entity.id!=peer or not getattr(entity,'broadcast',False):raise ValueError('Изменилась идентичность канала')
            # The fixed retrospective interval also covers days before a sample was forwarded.
            # Live placement windows and their seven-day deadline are kept independently.
            placements=store.rows("""SELECT p.* FROM placements p WHERE p.partner_id=? AND p.source_chat_id=?
                AND p.status NOT IN ('dismissed','needs_review') AND p.starts_at<=? AND p.ends_at>=? ORDER BY p.id""",
                (partner['id'],str(group),end.isoformat(),start.isoformat()))
            options={'offset_id':task['cursor']} if task['cursor'] else {'offset_date':end+timedelta(seconds=1)}
        size=100
        page=[m async for m in monitor.client.iter_messages(entity,limit=size,**options)]
        boundary=False
        handled=set()
        for message in page:
            if not getattr(message,'date',None):continue
            if (is_group and message.date>end) or (not is_group and message.date<start):
                boundary=True
                break
            if start<=message.date<=end and isinstance(message,types.Message):
                album=getattr(message,'grouped_id',None)
                tag=('g',album) if album else ('m',message.id)
                if tag not in handled:
                    already=store.rows('SELECT 1 FROM history_seen WHERE run=? AND peer=? AND mid=?',(run,peer,message.id))
                    if not already:
                        members=await monitor.album(entity,message) if album else [message]
                        members=[m for m in members if start<=m.date<=end]
                        if members:
                            if is_group:
                                intake.consume(group,members[0],combined_content(members),[])
                            else:
                                await check_publication(monitor,partner,members,placements,run)
                            with store.transaction():
                                for m in members:
                                    store.execute('INSERT OR IGNORE INTO history_seen VALUES(?,?,?,?)',(run,peer,m.id,m.date.isoformat()))
                    handled.add(tag)
        done=boundary or len(page)<size
        with store.transaction():
            if is_group and done:
                for item in request['channels']:
                    store.execute('INSERT OR IGNORE INTO history_pages(run,peer,partner_id) VALUES(?,?,?)',(run,item['channel_id'],item['id']))
            store.execute("UPDATE history_pages SET cursor=?,done=?,checked=?,retry='',error='' WHERE run=? AND peer=?",
                          (page[-1].id if page else task['cursor'],int(done),utcnow(),run,peer))
        return True
    except (ValueError,OSError,errors.RPCError,asyncio.TimeoutError) as exc:
        retry=(datetime.now(timezone.utc)+timedelta(seconds=max(60,getattr(exc,'seconds',0)+1))).isoformat()
        store.execute('UPDATE history_pages SET error=?,retry=?,checked=? WHERE run=? AND peer=?',(type(exc).__name__,retry,utcnow(),run,peer))
        log.warning('Загрузка истории: канал %s, %s, повтор %s',peer,type(exc).__name__,retry)
        return False


async def history_loop(monitor):
    while True:
        try:
            raw=monitor.store.state('requested_channel_history','')
            worked=await history_page(monitor,json.loads(raw)) if raw else False
        except Exception:
            log.exception('Загрузка истории отложена; сохранённая позиция сохранена')
            worked=False
        await asyncio.sleep(0.35 if worked else 5)
