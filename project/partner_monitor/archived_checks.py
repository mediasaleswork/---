"""Use preserved captions as explicit incomplete historical evidence.

The original Telegram post is never fabricated. A positive/uncertain archived
comparison requires review; only a strict, evidenced rejection can close it.
"""
import asyncio
from datetime import datetime,timedelta,timezone
import json
from telethon import types
from .campaigns import packed,digest,validate_decision,enqueue,source_signature,model_identity,candidate_signature,CampaignAPIError
from .storage import utcnow


def saved_caption(job,partner):
    try:value=json.loads(job.get('candidate_snapshot') or '{}')
    except (ValueError,TypeError):return None
    if not isinstance(value,dict) or not isinstance(value.get('text'),str) or len(value['text'].strip())<40:return None
    expected=f"https://t.me/{partner['username']}/{min(json.loads(job['message_ids']))}" if partner.get('username') else None
    if value.get('source_url') and value['source_url'].casefold()!=(expected or '').casefold():return None
    try:published=datetime.fromisoformat(value['published_at'])
    except (ValueError,KeyError,TypeError):return None
    if published.tzinfo is None or published>datetime.now(timezone.utc):return None
    if not isinstance(value.get('urls',[]),list):return None
    return value


async def check_saved_caption(worker,job,partner,placements,entity,ai_ready):
    from .campaign_queue import current,unavailable
    value=saved_caption(job,partner)
    if not value or not callable(getattr(type(worker.judge),'compare_archived',None)):return None
    if not any(datetime.fromisoformat(p['starts_at'])<=datetime.fromisoformat(value['published_at'])<datetime.fromisoformat(p['ends_at']) for p in placements):return None
    signature=digest([job['source_signature'],value])
    if job.get('archive_signature')==signature and job['status']=='archived_no_match':
        unavailable(worker,job,archived=True)
        return 'cached'
    if not ai_ready:return None
    content=[]
    for p in placements:
        content.append({'type':'input_text','text':'REFERENCE '+packed({'campaign_id':p['id'],
            'client':p['client_name'],'advertisement':p['name'],'text':p['source_text'][:16000]})})
        content.extend(await worker.reference_images(p))
    content.append({'type':'input_text','text':'PUBLICATION '+packed({'text':value['text'][:16000],
        'urls':value.get('urls',[]),'historical_caption':True,'publication_visuals_available':False,
        'published_at':value['published_at'],'archive_checked_at':value.get('archive_checked_at')})})
    if sum(len(part.get('image_url','')) for part in content)>12*1024*1024:raise CampaignAPIError('image_too_large')
    store=worker.store;day=datetime.now(timezone.utc).date().isoformat()
    store.set_state('campaign_api_attempts:'+day,int(store.state('campaign_api_attempts:'+day))+1)
    store.set_state('campaign_api_attempts_total',int(store.state('campaign_api_attempts_total'))+1)
    store.set_state('campaign_next_request_at',(datetime.now(timezone.utc)+timedelta(seconds=10)).isoformat())
    store.execute('UPDATE campaign_checks SET attempts=attempts+1 WHERE id=?',(job['id'],))
    decision=await asyncio.to_thread(worker.judge.compare_archived,content)
    result=validate_decision(decision,[p['id'] for p in placements])
    _,latest=current(worker,job)
    latest_job=store.rows('SELECT candidate_snapshot FROM campaign_checks WHERE id=?',(job['id'],))
    if not latest or not latest_job or latest_job[0]['candidate_snapshot']!=job['candidate_snapshot']:
        worker.finish(job,'obsolete','Материал или сохранённая версия изменились во время архивной сверки')
        return 'requested'
    returned=await worker.monitor.client.get_messages(entity,ids=json.loads(job['message_ids']))
    returned=sorted([m for m in returned if isinstance(m,types.Message)],key=lambda m:m.id)
    if len(returned)==len(json.loads(job['message_ids'])):
        worker.finish(job,'obsolete','Публикация снова доступна; проверим текущую версию')
        enqueue(store,worker.config,partner,returned,latest)
        # A reappeared identical version has the same key; revive its job.
        ident=digest([partner['id'],sorted(m.id for m in returned),source_signature(latest,model_identity(worker.config)),candidate_signature(returned)])
        store.execute("UPDATE campaign_checks SET status='pending',next_try_at=? WHERE id=? AND status='obsolete'",(utcnow(),ident))
        return 'requested'
    status='archived_no_match' if result=='no_match' and decision['confidence']>=.97 else 'review'
    reason=('Сохранённый текст: совпадения с заданием нет. ' if status=='archived_no_match' else 'Сохранённый текст восстановлен; требуется сверка. ')+decision['reason']
    proof={'result':decision,'evidence_type':'archived_caption_without_visuals','published_at':value['published_at'],
        'source_url':value.get('source_url'),'archive_checked_at':value.get('archive_checked_at')}
    worker.finish(job,status,reason,proof)
    store.execute('UPDATE campaign_checks SET archive_signature=?,next_try_at=? WHERE id=?',
        (signature,(datetime.now(timezone.utc)+timedelta(hours=4)).isoformat(),job['id']))
    return 'requested'
