"""Bounded replay of observed history, independent from the new-post cursor."""
from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

from telethon import types
from .campaigns import model_identity, source_signature


def pending_source_edits(store):
    pending = json.loads(store.state('source_edited_ids', '{}'))
    if isinstance(pending, list):  # Already queued by the previous release.
        return {str(mid): {'version': 'legacy', 'retry_at': ''} for mid in pending}
    return pending


def queue_source_edit(store, mid):
    pending = pending_source_edits(store)
    pending[str(mid)] = {'version': uuid4().hex, 'retry_at': ''}
    store.set_state('source_edited_ids', json.dumps(pending))


async def refresh_source_materials(monitor,entity):
    if not monitor.config.forwarded_intake or not monitor.scope_ready:
        return
    store=monitor.store;now=datetime.now(timezone.utc)
    pending=pending_source_edits(store)
    due=store.state('source_material_recheck_at','')<=now.isoformat()
    if not pending and not due:
        return
    chat=str(monitor.source_peer_id)
    cycle=json.loads(store.state('source_material_recheck_cycle','{}'))
    if due and not cycle:
        upper=store.rows('SELECT MAX(message_id) mid FROM source_messages WHERE source_chat_id=?',(chat,))[0]['mid'] or 0
        cycle={'cursor':0,'upper':upper}
    cutoff=(now-timedelta(days=monitor.config.default_window_days)).isoformat()
    rows=store.rows('SELECT message_id FROM source_messages WHERE source_chat_id=? AND message_id>? AND message_id<=? AND received_at>=? ORDER BY message_id LIMIT 100',
        (chat,cycle.get('cursor',0),cycle.get('upper',0),cutoff)) if due else []
    ready_edits=[int(mid) for mid, item in pending.items() if item['retry_at']<=now.isoformat()]
    # Reserve room for both history and live edits, even when some IDs are unavailable.
    priority=ready_edits[:50 if rows else 100]
    ids=list(dict.fromkeys(priority+[r['message_id'] for r in rows]))[:100]
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
        current=pending_source_edits(store)
        for mid in ids:
            key=str(mid)
            if key not in pending or current.get(key)!=pending[key]:
                continue  # An edit to this ID may have arrived during the network read.
            if mid in received:
                del current[key]
            else:
                current[key]['retry_at']=(now+timedelta(minutes=15)).isoformat()
        store.set_state('source_edited_ids',json.dumps(current))
        for row in rows:
            if row['message_id'] not in ids:
                break
            cycle['cursor']=row['message_id']
    if due:
        if not rows or (len(rows)<100 and cycle['cursor']>=rows[-1]['message_id']) or cycle['cursor']>=cycle['upper']:
            store.set_state('source_material_recheck_cycle','{}')
            store.set_state('source_material_recheck_at',(now+timedelta(minutes=15)).isoformat())
        else:
            store.set_state('source_material_recheck_cycle',json.dumps(cycle))


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
