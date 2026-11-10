"""Bounded multi-publication comparisons using the monitor's existing client.

No news is discarded by a keyword heuristic. Obsolete/duplicate jobs retain an
explicit audit result; unavailable posts retry separately from the ready queue.
"""
from datetime import datetime,timedelta,timezone
import json
import logging

from .campaigns import packed,source_signature,model_identity
from .parsing import PartnerReference
from .storage import utcnow

def current(worker,job):
    partners=worker.store.rows('SELECT * FROM partners WHERE id=?',(job['partner_id'],))
    if not partners:return None,[]
    partner=partners[0];ids=json.loads(job['placement_ids'])
    placements=[p for p in worker.store.rows('SELECT * FROM placements WHERE partner_id=?',(partner['id'],))
        if p['id'] in ids and p['status'] not in {'needs_review','dismissed'}]
    allowed=worker.config.permits(PartnerReference(partner['username'],private_channel_id=partner.get('private_channel_id')))
    # A transport/access failure is a pause, not a change of campaign scope.
    valid=(partner['status'] not in {'ignored','needs_review','identity_changed'} and allowed and len(placements)==len(ids)
        and all(str(p['source_chat_id'])==str(worker.monitor.source_peer_id) for p in placements)
        and source_signature(placements,model_identity(worker.config))==job['source_signature'])
    return (partner,placements) if valid else (None,[])


def maintain_queue(worker):
    store=worker.store
    if not store.state('campaign_local_rules_v2',''):
        store.execute("UPDATE campaign_checks SET status='pending',next_try_at=? WHERE status IN ('local_checked','offline_review')",(utcnow(),))
        store.set_state('campaign_local_rules_v2',utcnow())
    if not store.state('campaign_batch_labels_v2',''):
        store.execute("UPDATE campaign_checks SET next_try_at=? WHERE status='error' AND reason LIKE 'http_400%'",(utcnow(),))
        store.set_state('campaign_batch_labels_v2',utcnow())
    if not store.state('campaign_individual_review_v1',''):
        store.execute("UPDATE campaign_checks SET status='error',reason='individual_review',next_try_at=? WHERE status='review' AND decision IS NOT NULL AND attempts<3",(utcnow(),))
        store.set_state('campaign_individual_review_v1',utcnow())
    if not store.state('campaign_unavailable_retry_v1',''):
        store.execute("UPDATE campaign_checks SET status='unavailable',next_try_at=? WHERE status='review' AND reason='Публикация недоступна; совпадение не засчитано'",(utcnow(),))
        store.set_state('campaign_unavailable_retry_v1',utcnow())
    groups={}
    for job in store.rows("SELECT * FROM campaign_checks WHERE status IN ('pending','error','unavailable','archived_no_match','local_checked','offline_review') ORDER BY created_at DESC"):
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


def settle_placements(worker):
    """Close a completed search only after every incomplete check is resolved."""
    waiting={pid for job in worker.store.rows("SELECT placement_ids FROM campaign_checks WHERE status IN ('pending','error','review','unavailable','archived_no_match','local_checked','offline_review')")
             for pid in json.loads(job['placement_ids'])}
    for row in worker.store.rows("SELECT id FROM placements WHERE scan_complete=1 AND status='watching'"):
        if row['id'] not in waiting:
            worker.store.execute("""UPDATE placements SET status=CASE WHEN EXISTS(
              SELECT 1 FROM placement_posts WHERE placement_id=placements.id) THEN 'found' ELSE 'not_found' END WHERE id=?""",(row['id'],))


def save_status(worker):
    store=worker.store
    data={'checked_at':utcnow(),'counts':store.rows('SELECT status,COUNT(*) AS n FROM campaign_checks GROUP BY status'),
        'oldest_pending':store.rows("SELECT MIN(created_at) AS created_at FROM campaign_checks WHERE status IN ('pending','error')"),
        'unavailable':store.rows("SELECT p.username,j.message_ids,j.status,j.reason,j.availability_attempts,j.next_try_at FROM campaign_checks j JOIN partners p ON p.id=j.partner_id WHERE j.status IN ('unavailable','archived_no_match')"),
        'mode':store.state('campaign_mode','ai'),'mode_reason':store.state('campaign_local_reason','')}
    store.set_state('campaign_queue_status',packed(data))
    # Operational details remain on the service's existing private volume.
    root=worker.config.runtime_dir
    root.mkdir(parents=True,exist_ok=True)
    path=root/'campaign-queue-status.json';temporary=path.with_suffix('.tmp')
    temporary.write_text(packed(data),encoding='utf-8');temporary.replace(path)


def unavailable(worker,job,archived=False):
    count=job.get('availability_attempts',0)+1
    delay=min(240,5*2**min(count-1,6))
    retry=(datetime.now(timezone.utc)+timedelta(minutes=delay)).isoformat()
    worker.store.execute("""UPDATE campaign_checks SET status=?,reason=?,
        availability_attempts=availability_attempts+1,next_try_at=?,checked_at=?
        WHERE id=? AND status IN ('pending','error','review','unavailable','archived_no_match')""",
        ('archived_no_match' if archived else 'unavailable',job['reason'] if archived else 'Telegram не вернул публикацию; удаление не подтверждено. Повторная проверка запланирована.',retry,utcnow(),job['id']))
    worker.monitor.changed.set()


def budget(worker):
    day=datetime.now(timezone.utc).date().isoformat()
    used=int(worker.store.state('campaign_api_attempts:'+day))
    total=int(worker.store.state('campaign_api_attempts_total'))
    return used<worker.config.campaign_daily_limit and (not worker.config.campaign_total_limit or total<worker.config.campaign_total_limit)


def defer(worker,jobs,code):
    hint=getattr(code,'retry_after',None)
    code=str(code)
    now=datetime.now(timezone.utc)
    if code in {'request_timeout','network_unavailable','http_500','http_502','http_503','http_504'}:
        failures=int(worker.store.state('campaign_service_failures'))+1
        worker.store.set_state('campaign_service_failures',failures)
        pause=max(hint or 0,min(300,30*2**min(failures-1,4)))
        retry=(now+timedelta(seconds=pause)).isoformat()
        worker.store.set_state('campaign_service_retry_at',retry)
    if code=='http_429':
        failures=int(worker.store.state('campaign_quota_failures'))+1
        worker.store.set_state('campaign_quota_failures',failures)
        pause=max(hint or 0,min(900,60*2**min(failures-1,4)))
        retry=(now+timedelta(seconds=pause)).isoformat()
        worker.store.set_state('campaign_quota_retry_at',retry)
    elif code.startswith('http_400'):retry=(now+timedelta(seconds=30)).isoformat()
    elif code not in {'request_timeout','network_unavailable','http_500','http_502','http_503','http_504'}:
        retry=(now+timedelta(seconds=60)).isoformat()
    logging.getLogger(__name__).warning('Пакет распознавания отложен: %s, публикаций %s',code,len(jobs))
    for job in jobs:
        count=worker.store.rows('SELECT attempts FROM campaign_checks WHERE id=?',(job['id'],))[0]['attempts']
        if count>=3 and code in {'invalid_batch_response','incomplete_batch_response','incomplete_or_blocked_response','model_refusal'}:
            worker.finish(job,'review','После повторной сверки ответ распознавания неполон; требуется проверка: '+code)
        else:
            worker.store.execute("UPDATE campaign_checks SET status='error',reason=?,next_try_at=? WHERE id=?",(code,retry,job['id']))
    worker.monitor.changed.set()


async def process_batch(worker):
    from .campaign_batching import choose_group,process_group
    from .campaign_local import local_reason
    from .archived_checks import caption_payload
    store=worker.store
    try:
        for _ in range(worker.config.campaign_batch_size):
            now=utcnow()
            offline=local_reason(worker,now)
            store.set_state('campaign_mode','local' if offline else 'ai')
            store.set_state('campaign_local_reason',offline)
            ai_ready=not offline and store.state('campaign_next_request_at','')<=now
            statuses="'pending','error','unavailable','archived_no_match'"
            if ai_ready:statuses+=",'local_checked','offline_review'"
            due="j.next_try_at<=?"
            if offline:
                # A provider retry deadline must not hold back a local check.
                due="("+due+" OR (j.status='error' AND (j.reason LIKE 'http_%' OR j.reason IN ('request_timeout','network_unavailable','missing_api_key'))))"
            where="p.status='active' AND j.status IN ("+statuses+") AND "+due
            sql='SELECT j.* FROM campaign_checks j JOIN partners p ON p.id=j.partner_id WHERE '+where
            jobs=store.rows(sql+' ORDER BY j.priority DESC,j.created_at,j.id LIMIT 1000',(now,))
            # Include global oldest/newest even when >1000 high-priority jobs
            # would otherwise hide them from the bounded working set.
            for order in ('j.created_at,j.id','j.created_at DESC,j.id DESC'):
                for job in store.rows(sql+' ORDER BY '+order+' LIMIT 1',(now,)):
                    if not any(j['id']==job['id'] for j in jobs):jobs.append(job)
            if not ai_ready and not offline:
                jobs=[j for j in jobs if j.get('provisional_decision') or j['status']=='archived_no_match'
                    or j['status']=='unavailable' and not caption_payload(j)]
            if not jobs:return
            turn=int(store.state('campaign_queue_turn'))
            group=choose_group(worker,jobs,turn)
            if not group:return
            store.set_state('campaign_queue_turn',turn+1)
            if not await process_group(worker,group,ai_ready,offline):return
    finally:
        settle_placements(worker)
        save_status(worker)
