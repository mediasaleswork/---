"""Bounded replay of observed history, independent from the new-post cursor."""
from datetime import datetime, timedelta, timezone
import json

from telethon import types
from .campaigns import model_identity, source_signature


async def refresh_source_materials(monitor,entity):
    if not monitor.config.forwarded_intake or not monitor.scope_ready:
        return
    store=monitor.store;now=datetime.now(timezone.utc)
    pending=json.loads(store.state('source_edited_ids','[]'))
    if not pending and store.state('source_material_recheck_at','')>now.isoformat():
        return
    chat=str(monitor.source_peer_id)
    rows=store.rows('SELECT message_id FROM source_messages WHERE source_chat_id=? ORDER BY message_id DESC LIMIT 100',(chat,))
    pending_known=[mid for mid in pending if store.rows('SELECT message_id FROM source_messages WHERE source_chat_id=? AND message_id=?',(chat,mid))]
    ids=list(dict.fromkeys(pending_known+[r['message_id'] for r in rows]))[:100]
    if ids:
        from .intake import ConversationIntake
        from .message_content import combined_content
        intake=ConversationIntake(store,monitor.config)
        messages=await monitor.client.get_messages(entity,ids=ids)
        seen=set();received=set()
        for message in messages:
            if not isinstance(message,types.Message):continue
            received.add(message.id)
            members=await monitor.album(entity,message)
            anchor=members[0]
            if anchor.id in seen:continue
            seen.add(anchor.id)
            intake.refresh_forwarded(chat,anchor,combined_content(members))
        store.set_state('source_edited_ids',json.dumps([mid for mid in pending_known if mid not in received]))
    store.set_state('source_material_recheck_at',(now+timedelta(minutes=15)).isoformat())


async def reconcile_channel(monitor, partner, entity):
    if not monitor.scope_ready:
        return
    store=monitor.store
    now=datetime.now(timezone.utc)
    placements=[p for p in store.rows('SELECT * FROM placements WHERE partner_id=?',(partner['id'],))
                if p['status'] not in {'needs_review','dismissed'} and not json.loads(p['direct_ids'])
                and datetime.fromisoformat(p['starts_at'])<=now
                and now<datetime.fromisoformat(p['ends_at'])+timedelta(hours=24)]
    if not placements:
        return
    key=f'history_reconciliation:{partner["id"]}'
    signature=source_signature(placements,model_identity(monitor.config))
    state=json.loads(store.state(key,'{}'))
    if state.get('signature')!=signature:
        state={'signature':signature,'cursor':0,'upper':0}
    if state.get('next_at','')>now.isoformat():
        return
    if not state.get('upper'):
        state['upper']=store.rows('SELECT last_message_id FROM partners WHERE id=?',(partner['id'],))[0]['last_message_id']
        state['cursor']=0
        state['next_at']=''
    if not state['upper']:
        return
    options={'min_id':state['cursor'],'max_id':state['upper']+1,'reverse':True,'limit':monitor.config.batch_size}
    if not state['cursor']:
        options['offset_date']=min(datetime.fromisoformat(p['starts_at']) for p in placements)-timedelta(seconds=1)
    seen=set();count=0
    async for message in monitor.client.iter_messages(entity,**options):
        if message.id>state['upper']:
            break
        if isinstance(message,types.Message):
            album=getattr(message,'grouped_id',None) or f'm:{message.id}'
            if album not in seen:
                members=await monitor.album(entity,message)
                await monitor.queue_campaign(partner,members,placements)
                seen.add(album)
        # Save only successfully processed IDs. Replaying after a crash is safe.
        state['cursor']=message.id;count+=1
        store.set_state(key,json.dumps(state))
    if count<monitor.config.batch_size or state['cursor']>=state['upper']:
        state.update(cursor=0,upper=0,next_at=(now+timedelta(minutes=15)).isoformat(),completed_at=now.isoformat())
        store.set_state(key,json.dumps(state))
