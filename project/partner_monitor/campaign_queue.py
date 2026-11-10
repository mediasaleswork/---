"""Bounded multi-publication comparisons using the monitor's existing client.

No news is discarded by a keyword heuristic. Obsolete/duplicate jobs retain an
explicit audit result; unavailable posts retry separately from the ready queue.
"""
import asyncio
from datetime import datetime,timedelta,timezone
import json

from telethon import types
from .campaigns import (CampaignAPIError,MAX_CAMPAIGNS,packed,source_signature,
    model_identity,candidate_signature,enqueue,validate_decision)
from .fallback import offline_match
from .message_content import combined_content
from .parsing import PartnerReference
from .storage import utcnow

GROUP_SIZE=8
MAX_PAYLOAD=12*1024*1024


def current(worker,job):
    partners=worker.store.rows('SELECT * FROM partners WHERE id=?',(job['partner_id'],))
    if not partners:return None,[]
    partner=partners[0];ids=json.loads(job['placement_ids'])
    placements=[p for p in worker.store.rows('SELECT * FROM placements WHERE partner_id=?',(partner['id'],))
        if p['id'] in ids and p['status'] not in {'needs_review','dismissed'}]
    allowed=worker.config.permits(PartnerReference(partner['username'],private_channel_id=partner.get('private_channel_id')))
    valid=(partner['status']=='active' and allowed and len(placements)==len(ids)
        and all(str(p['source_chat_id'])==str(worker.monitor.source_peer_id) for p in placements)
        and source_signature(placements,model_identity(worker.config))==job['source_signature'])
    return (partner,placements) if valid else (None,[])


def maintain_queue(worker):
    store=worker.store
    if not store.state('campaign_unavailable_retry_v1',''):
        store.execute("UPDATE campaign_checks SET status='unavailable',next_try_at=? WHERE status='review' AND reason='Публикация недоступна; совпадение не засчитано'",(utcnow(),))
        store.set_state('campaign_unavailable_retry_v1',utcnow())
    groups={}
    for job in store.rows("SELECT * FROM campaign_checks WHERE status IN ('pending','error','unavailable') ORDER BY created_at DESC"):
        partner,placements=current(worker,job)
        if not partner:
            worker.finish(job,'obsolete','Настройки, материал или область наблюдения изменились')
            continue
        key=(job['partner_id'],job['message_ids'],job['candidate_signature'])
        groups.setdefault(key,[]).append(job)
    for versions in groups.values():
        keep=[]
        for job in sorted(versions,key=lambda j:len(json.loads(j['placement_ids'])),reverse=True):
            ids=set(json.loads(job['placement_ids']))
            covering=next((k for k in keep if ids<=set(json.loads(k['placement_ids']))),None)
            if covering:
                worker.finish(job,'obsolete','Повтор той же версии публикации; полная проверка '+covering['id'][:12])
            else:keep.append(job)
    save_status(worker)


def save_status(worker):
    store=worker.store
    data={'checked_at':utcnow(),'counts':store.rows('SELECT status,COUNT(*) AS n FROM campaign_checks GROUP BY status'),
        'oldest_pending':store.rows("SELECT MIN(created_at) AS created_at FROM campaign_checks WHERE status IN ('pending','error')"),
        'unavailable':store.rows("SELECT p.username,j.message_ids,j.reason,j.availability_attempts,j.next_try_at FROM campaign_checks j JOIN partners p ON p.id=j.partner_id WHERE j.status='unavailable'")}
    store.set_state('campaign_queue_status',packed(data))
    # Operational details remain on the service's existing private volume.
    root=worker.config.runtime_dir
    root.mkdir(parents=True,exist_ok=True)
    path=root/'campaign-queue-status.json';temporary=path.with_suffix('.tmp')
    temporary.write_text(packed(data),encoding='utf-8');temporary.replace(path)


def unavailable(worker,job):
    count=job.get('availability_attempts',0)+1
    delay=min(240,5*2**min(count-1,6))
    retry=(datetime.now(timezone.utc)+timedelta(minutes=delay)).isoformat()
    worker.store.execute("""UPDATE campaign_checks SET status='unavailable',reason=?,
        availability_attempts=availability_attempts+1,next_try_at=?,checked_at=?
        WHERE partner_id=? AND message_ids=? AND status IN ('pending','error','review','unavailable')""",
        ('Telegram не вернул публикацию; удаление не подтверждено. Повторная проверка запланирована.',retry,utcnow(),job['partner_id'],job['message_ids']))
    worker.monitor.changed.set()


def budget(worker):
    day=datetime.now(timezone.utc).date().isoformat()
    used=int(worker.store.state('campaign_api_attempts:'+day))
    total=int(worker.store.state('campaign_api_attempts_total'))
    return used<worker.config.campaign_daily_limit and (not worker.config.campaign_total_limit or total<worker.config.campaign_total_limit)


def defer(worker,jobs,code,attempted=False):
    if code=='http_429':
        retry=(datetime.now(timezone.utc)+timedelta(minutes=15)).isoformat()
        worker.store.set_state('campaign_quota_retry_at',retry)
    else:retry=(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat()
    for job in jobs:
        worker.store.execute("UPDATE campaign_checks SET status='error',reason=?,next_try_at=? WHERE id=?",(code,retry,job['id']))
    worker.monitor.changed.set()


async def process_batch(worker):
    store=worker.store
    try:
        for _ in range(worker.config.campaign_batch_size):
            now=utcnow()
            ai_ready=store.state('campaign_quota_retry_at','')<=now and store.state('campaign_next_request_at','')<=now and budget(worker)
            jobs=store.rows("SELECT * FROM campaign_checks WHERE status IN ('pending','error','unavailable') AND next_try_at<=? ORDER BY priority DESC,created_at,id LIMIT 1000",(now,))
            if not ai_ready:jobs=[j for j in jobs if j['status']=='unavailable']
            if not jobs:return
            turn=int(store.state('campaign_queue_turn'));store.set_state('campaign_queue_turn',turn+1)
            # Every fourth request starts with the newest candidate, including
            # an image-only post. Other requests steadily drain the backlog.
            anchor=max(jobs,key=lambda j:j['created_at']) if turn%4==0 else jobs[0]
            group=[anchor]+[j for j in jobs if j['id']!=anchor['id'] and j['partner_id']==anchor['partner_id']
                and j['placement_ids']==anchor['placement_ids'] and j['source_signature']==anchor['source_signature']][:GROUP_SIZE-1]
            partner,placements=current(worker,anchor)
            if not partner:continue
            if len(placements)>MAX_CAMPAIGNS:
                for job in group:worker.finish(job,'review','Слишком много одновременно подходящих кампаний; уточните материал')
                continue
            if getattr(worker.judge,'key_variable',None):
                import os
                if not os.environ.get(worker.judge.key_variable,'').strip():
                    for job in group:store.execute("UPDATE campaign_checks SET reason='missing_api_key' WHERE id=?",(job['id'],))
                    return
            ready=[]
            try:
                entity=await worker.monitor.client.get_entity(partner['username'] or types.PeerChannel(partner['private_channel_id']))
                if entity.id!=partner['channel_id']:
                    for job in group:worker.finish(job,'obsolete','Канал изменился; нужна проверка')
                    continue
                ids=sorted({mid for j in group for mid in json.loads(j['message_ids'])})
                fetched=await worker.monitor.client.get_messages(entity,ids=ids)
                messages_by_id={m.id:m for m in fetched if isinstance(m,types.Message)}
                for job in group:
                    messages=[messages_by_id[mid] for mid in json.loads(job['message_ids']) if mid in messages_by_id]
                    if len(messages)!=len(json.loads(job['message_ids'])):
                        unavailable(worker,job);continue
                    if candidate_signature(messages)!=job['candidate_signature']:
                        worker.finish(job,'obsolete','Публикация отредактирована; проверим новую версию')
                        enqueue(store,worker.config,partner,messages,placements);continue
                    recorded=store.rows('SELECT message_ids FROM posts WHERE partner_id=?',(partner['id'],))
                    if any(set(m.id for m in messages)&set(json.loads(p['message_ids'])) for p in recorded):
                        worker.finish(job,'obsolete','Публикация уже учтена');continue
                    actual=combined_content(messages)
                    if job['status']=='unavailable':store.execute("UPDATE campaign_checks SET status='pending',reason='' WHERE id=?",(job['id'],))
                    snapshot=packed({'text':actual.text,'urls':actual.urls,'media':actual.media,
                        'published_at':messages[0].date.isoformat(),'views':getattr(messages[0],'views',None)})
                    store.execute('UPDATE campaign_checks SET candidate_snapshot=? WHERE id=?',(snapshot,job['id']))
                    exact=[p for p in placements if offline_match(messages,p).matched]
                    if len(exact)==1:
                        pid=await worker.monitor.record_post(partner,entity,messages,'offline:совпадает согласованный материал',exact)
                        store.execute('UPDATE campaign_checks SET post_id=? WHERE id=?',(pid,job['id']))
                        worker.finish(job,'match','Совпадает согласованный материал без обращения к ИИ');continue
                    ready.append((job,messages,actual))
                if not ready:continue
                if not ai_ready:return
                content=[]
                for p in placements:
                    content.append({'type':'input_text','text':'REFERENCE '+packed({'campaign_id':p['id'],
                        'client':p['client_name'],'advertisement':p['name'],'text':p['source_text'][:16000]})})
                    content.extend(await worker.reference_images(p))
                selected=[]
                for job,messages,actual in ready:
                    parts=[{'type':'input_text','text':'PUBLICATION '+packed({'job_id':job['id'],'text':actual.text[:16000],'urls':actual.urls})}]
                    parts.extend(await worker.images(messages))
                    if sum(len(p.get('image_url','')) for p in content+parts)>MAX_PAYLOAD:
                        if not selected:raise CampaignAPIError('image_too_large')
                        break
                    content.extend(parts);selected.append((job,messages,actual))
                ready=selected
                if not ready:continue
                day=datetime.now(timezone.utc).date().isoformat()
                store.set_state('campaign_api_attempts:'+day,int(store.state('campaign_api_attempts:'+day))+1)
                store.set_state('campaign_api_attempts_total',int(store.state('campaign_api_attempts_total'))+1)
                store.set_state('campaign_next_request_at',(datetime.now(timezone.utc)+timedelta(seconds=10)).isoformat())
                for job,_,_ in ready:store.execute('UPDATE campaign_checks SET attempts=attempts+1 WHERE id=?',(job['id'],))
                values=await asyncio.to_thread(worker.judge.compare_many,content,[j['id'] for j,_,_ in ready])
                if not isinstance(values,dict) or set(values)!={j['id'] for j,_,_ in ready}:raise CampaignAPIError('incomplete_batch_response')
                # Validate the whole response before committing ANY verdict.
                verdicts={key:validate_decision(value,[p['id'] for p in placements]) for key,value in values.items()}
                refreshed=await worker.monitor.client.get_messages(entity,ids=ids)
                by_id={m.id:m for m in refreshed if isinstance(m,types.Message)}
                for job,previous,_ in ready:
                    _,latest=current(worker,job)
                    if not latest:
                        worker.finish(job,'obsolete','Материал изменился во время проверки');continue
                    messages=[by_id[mid] for mid in json.loads(job['message_ids']) if mid in by_id]
                    if len(messages)!=len(previous):unavailable(worker,job);continue
                    if candidate_signature(messages)!=job['candidate_signature']:
                        worker.finish(job,'obsolete','Публикация изменилась во время распознавания')
                        enqueue(store,worker.config,partner,messages,latest);continue
                    value=values[job['id']];status=verdicts[job['id']]
                    if status=='match':
                        chosen=next(p for p in latest if p['id']==value['campaign_id'])
                        post_id=await worker.monitor.record_post(partner,entity,messages,'campaign: '+value['reason'][:700],[chosen])
                        store.execute('UPDATE campaign_checks SET post_id=? WHERE id=?',(post_id,job['id']))
                    worker.finish(job,status,value['reason'],value)
            except CampaignAPIError as exc:
                defer(worker,[j for j,_,_ in ready] or group,str(exc))
                return
            except ValueError:
                defer(worker,[j for j,_,_ in ready] or group,'invalid_batch_response')
                return
    finally:save_status(worker)
