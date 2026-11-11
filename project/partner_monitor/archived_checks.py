"""Use preserved captions as explicit incomplete historical evidence.

The original Telegram post is never fabricated. A positive/uncertain archived
comparison requires review; only a strict, evidenced rejection can close it.
"""
import asyncio
from datetime import datetime,timedelta,timezone
import json
from telethon import types
from .campaigns import packed,digest,validate_decision,enqueue,source_signature,model_identity,candidate_signature,CampaignAPIError
from .message_content import combined_content
from .storage import utcnow


def caption_payload(job, min_length=40):
    try:value=json.loads(job.get('candidate_snapshot') or '{}')
    except (ValueError,TypeError):return None
    if not isinstance(value,dict) or not isinstance(value.get('text'),str) or len(value['text'].strip())<min_length:return None
    return value


def saved_caption(job,partner,min_length=40):
    value=caption_payload(job,min_length)
    if not value:return None
    mid=min(json.loads(job['message_ids']))
    expected=f"https://t.me/{partner['username']}/{mid}" if partner.get('username') else f"https://t.me/c/{partner.get('channel_id')}/{mid}"
    if value.get('source_url') and value['source_url'].casefold()!=(expected or '').casefold():return None
    try:published=datetime.fromisoformat(value['published_at'])
    except (ValueError,KeyError,TypeError):return None
    if published.tzinfo is None or published>datetime.now(timezone.utc):return None
    if not isinstance(value.get('urls',[]),list):return None
    return value


def local_archive_review(worker,job,partner,placements):
    from .candidate_archive import files_for_job
    from .campaign_local import conflicting_numbers,text_copy
    files=files_for_job(worker.store,job)
    value=saved_caption(job,partner,0) if files else None
    if not value:return False
    keys={f['key'] for f in files if f['saved'] and not f['key'].startswith('post:')}
    matched=[p['id'] for p in placements if not conflicting_numbers(value['text'],p) and
        (text_copy(value['text'],p) or keys.intersection(json.loads(p.get('media_keys') or '[]')))]
    if not matched:return False
    worker.finish(job,'review','Оригинал недоступен. В сохранённой копии совпадает текст или файл задания; требуется подтверждение.',
        {'evidence_type':'observed_archive','mode':'local','matching_campaign_ids':matched})
    return True


async def recover_forwarded_captions(worker):
    """Look only at already ingested messages in the verified source group."""
    from .campaign_queue import current
    targets={}
    for job in worker.store.rows("SELECT * FROM campaign_checks WHERE status='unavailable' AND candidate_snapshot='' ORDER BY id"):
        partner,_=current(worker,job)
        if partner and partner.get('channel_id'):
            for mid in json.loads(job['message_ids']):targets.setdefault((partner['channel_id'],mid),[]).append((job,partner))
    if not targets:return
    source_ids=[r['message_id'] for r in worker.store.rows('SELECT message_id FROM source_messages WHERE source_chat_id=? ORDER BY message_id DESC LIMIT 100',(str(worker.monitor.source_peer_id),))]
    if not source_ids:return
    signature=digest([sorted(targets),source_ids])
    if worker.store.state('forward_caption_probe_signature','')==signature:return
    entity=await worker.monitor.client.get_entity(int(worker.monitor.source_peer_id))
    copies=await worker.monitor.client.get_messages(entity,ids=source_ids)
    recovered=[]
    for copy in copies:
        if not isinstance(copy,types.Message):continue
        forward=getattr(copy,'fwd_from',None);origin=getattr(forward,'from_id',None)
        key=(getattr(origin,'channel_id',None),getattr(forward,'channel_post',None))
        if not isinstance(origin,types.PeerChannel) or key not in targets:continue
        content=combined_content([copy]);published=getattr(forward,'date',None)
        if not published or published.tzinfo is None:continue
        for job,partner in targets[key]:
            if not current(worker,job)[0]:continue
            url=f"https://t.me/{partner['username']}/{key[1]}" if partner.get('username') else f"https://t.me/c/{partner['channel_id']}/{key[1]}"
            snapshot={'text':content.text,'urls':list(content.urls),'media':list(content.media),'published_at':published.isoformat(),
                'source_url':url,'archive_method':'verified_telegram_forward','archive_checked_at':utcnow(),
                'forward_copy_message_id':copy.id,'publication_visuals_available':False}
            worker.store.execute("UPDATE campaign_checks SET candidate_snapshot=?,next_try_at=? WHERE id=? AND candidate_snapshot=''",(packed(snapshot),utcnow(),job['id']))
            recovered.append({'source_url':url,'copy_message_id':copy.id,'has_caption':len(content.text.strip())>=40})
    worker.store.set_state('forward_caption_probe_signature',signature)
    worker.store.set_state('forward_caption_probe_result',packed({'checked_at':utcnow(),'source_copies_inspected':len(source_ids),'recovered':recovered}))
    if recovered:worker.monitor.changed.set()


async def check_saved_caption(worker,job,partner,placements,entity,ai_ready):
    from .campaign_queue import current,unavailable
    refreshed=worker.store.rows('SELECT candidate_snapshot,archive_signature,provisional_decision FROM campaign_checks WHERE id=?',(job['id'],))
    if refreshed:job={**job,**refreshed[0]}
    from .candidate_archive import files_for_job,image_parts
    files=files_for_job(worker.store,job)
    archive=getattr(worker.store,'candidate_archive',None)
    value=saved_caption(job,partner,0 if files else 40)
    if not value or not callable(getattr(type(worker.judge),'compare_archived',None)):return None
    published = datetime.fromisoformat(value['published_at'])
    in_window = any(datetime.fromisoformat(p['starts_at']) <= published < datetime.fromisoformat(p['ends_at'])
                    for p in placements)
    if not in_window:
        request = json.loads(worker.store.state('requested_channel_history', '{}'))
        included = worker.store.rows('SELECT 1 FROM history_checks WHERE run=? AND job=?',
                                     (request.get('id'), job['id']))
        if not included or not datetime.fromisoformat(request['start']) <= published <= datetime.fromisoformat(request['end']):
            return None
    visuals=await asyncio.to_thread(image_parts,archive,files) if archive and files else []
    if not value['text'].strip() and not visuals:return None
    signature=digest([job['source_signature'],value]+([visuals] if visuals else []))
    if job.get('archive_signature')==signature and job['status']=='archived_no_match':
        unavailable(worker,job,archived=True)
        return 'cached'
    try:cached=json.loads(job.get('provisional_decision') or '{}')
    except (ValueError,TypeError):cached={}
    decision=cached.get('result') if isinstance(cached,dict) and cached.get('archive_signature')==signature else None
    if decision is not None:
        try:validate_decision(decision,[p['id'] for p in placements])
        except ValueError:decision=None
    if decision is None and not ai_ready:return 'waiting'
    store=worker.store
    if decision is None:
        from .campaign_runtime import reserve_request,finish_request
        content=[]
        for p in placements:
            content.append({'type':'input_text','text':'REFERENCE '+packed({'campaign_id':p['id'],
                'client':p['client_name'],'advertisement':p['name'],'text':p['source_text'][:16000]})})
            content.extend(await worker.reference_images(p))
        content.append({'type':'input_text','text':'PUBLICATION '+packed({'text':value['text'][:16000],
            'urls':value.get('urls',[]),'historical_caption':True,'publication_visuals_available':bool(visuals),
            'published_at':value['published_at'],'archive_checked_at':value.get('archive_checked_at')})})
        content.extend(visuals)
        if sum(len(part.get('image_url','')) for part in content)>12*1024*1024:raise CampaignAPIError('image_too_large')
        request_id=reserve_request(worker,[job])
        try:
            decision=await asyncio.to_thread(worker.judge.compare_archived,content)
            validate_decision(decision,[p['id'] for p in placements])
        except CampaignAPIError as exc:
            finish_request(worker,request_id,exc)
            raise
        except (ValueError,TypeError):
            finish_request(worker,request_id,'invalid_batch_response')
            raise CampaignAPIError('invalid_batch_response') from None
        store.execute('UPDATE campaign_checks SET provisional_decision=?,provisional_at=? WHERE id=?',
            (packed({'archive_signature':signature,'result':decision}),utcnow(),job['id']))
        finish_request(worker,request_id)
    result=validate_decision(decision,[p['id'] for p in placements])
    if not getattr(worker.monitor,'scope_ready',True):
        return 'requested'
    _,latest=current(worker,job)
    latest_job=store.rows('SELECT candidate_snapshot FROM campaign_checks WHERE id=?',(job['id'],))
    if not latest or not latest_job or latest_job[0]['candidate_snapshot']!=job['candidate_snapshot']:
        worker.finish(job,'obsolete','Материал или сохранённая версия изменились во время архивной сверки')
        return 'requested'
    returned=await worker.monitor.client.get_messages(entity,ids=json.loads(job['message_ids']))
    returned=sorted([m for m in returned if isinstance(m,types.Message)],key=lambda m:m.id)
    if not getattr(worker.monitor,'scope_ready',True):
        return 'requested'
    if len(returned)==len(json.loads(job['message_ids'])):
        worker.finish(job,'obsolete','Публикация снова доступна; проверим текущую версию')
        enqueue(store,worker.config,partner,returned,latest,replaces=job['id'])
        # A reappeared identical version has the same key; revive its job.
        ident=digest([partner['id'],sorted(m.id for m in returned),source_signature(latest,model_identity(worker.config)),candidate_signature(returned)])
        store.execute("UPDATE campaign_checks SET status='pending',next_try_at=? WHERE id=? AND status='obsolete'",(utcnow(),ident))
        return 'requested'
    # A caption or two images cannot speak for a missing video or the rest of an album.
    incomplete=bool(files) and len([f for f in files if not f['key'].startswith('post:')])>len(visuals)
    status='archived_no_match' if result=='no_match' and decision['confidence']>=.97 and not incomplete else 'review'
    reason=('Сохранённый текст: совпадения с заданием нет. ' if status=='archived_no_match' else 'Сохранённый текст восстановлен; требуется сверка. ')+decision['reason']
    if visuals:reason='Проверены сохранённые текст и изображения. '+decision['reason']
    proof={'result':decision,'evidence_type':'observed_archive' if visuals or incomplete else 'archived_caption_without_visuals','published_at':value['published_at'],
        'source_url':value.get('source_url'),'archive_checked_at':value.get('archive_checked_at')}
    worker.finish(job,status,reason,proof)
    store.execute('UPDATE campaign_checks SET archive_signature=?,next_try_at=? WHERE id=?',
        (signature,(datetime.now(timezone.utc)+timedelta(hours=4)).isoformat(),job['id']))
    return 'requested'
