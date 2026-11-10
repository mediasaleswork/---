"""Cross-channel batches with exact per-channel attribution and durable results."""
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os

from telethon import errors, types

from .campaigns import (CampaignAPIError, MAX_CAMPAIGNS, candidate_signature,
                        enqueue, model_identity, packed, source_signature, validate_decision)
from .campaign_runtime import reserve_request, finish_request
from .fallback import offline_match
from .message_content import combined_content
from .storage import utcnow

GROUP_SIZE = 8
MAX_PAYLOAD = 12 * 1024 * 1024
COLLECT_SECONDS = 180


def individual(job):
    return bool(job.get('individual_review') or job['reason']=='individual_review')


def creative_key(placement, model):
    # Only the placement ID differs between channels. Every source revision,
    # window, client and media identifier must still match exactly.
    return source_signature([dict(placement, id=0)], model)


def compatible(placements, model):
    return tuple(sorted(creative_key(p, model) for p in placements))


def choose_group(worker, jobs, turn):
    from .campaign_queue import current
    if turn % 4 == 0:
        anchor = min(jobs, key=lambda j: (j['created_at'], j['id']))
    elif turn % 4 == 3:
        anchor = max(jobs, key=lambda j: (j['created_at'], j['id']))
    else:
        anchor = max(jobs, key=lambda j: (j['priority'], -datetime.fromisoformat(j['created_at']).timestamp()))
    partner, placements = current(worker, anchor)
    if not partner:
        return []
    size = GROUP_SIZE
    if individual(anchor):
        size = 1
    elif anchor['reason'].startswith('http_400') or anchor['reason'] in {'request_timeout', 'invalid_batch_response', 'incomplete_batch_response'}:
        size = max(1, GROUP_SIZE // 2 ** min(anchor['attempts'], 3))
    model = model_identity(worker.config)
    signature = compatible(placements, model)
    group = [anchor]
    for job in jobs:
        if len(group) >= size:
            break
        if job['id'] == anchor['id'] or individual(job):
            continue
        other, campaigns = current(worker, job)
        if other and compatible(campaigns, model) == signature:
            group.append(job)
    return group


@dataclass
class Candidate:
    job: dict
    partner: dict
    placements: list
    entity: object
    messages: list
    actual: object


def telegram_retry(worker, jobs):
    retry = (datetime.now(timezone.utc) + timedelta(seconds=15)).isoformat()
    for job in jobs:
        worker.store.execute("UPDATE campaign_checks SET reason='telegram_retry',next_try_at=? WHERE id=?", (retry, job['id']))


async def commit(worker, candidate, value, messages):
    from .campaign_queue import current, unavailable
    job = candidate.job
    if not getattr(worker.monitor, 'scope_ready', True):
        return
    partner, latest = current(worker, job)
    if not partner:
        worker.finish(job, 'obsolete', 'Материал или область наблюдения изменились')
        return
    if partner['status'] != 'active':
        return
    if len(messages) != len(candidate.messages):
        unavailable(worker, job)
        return
    if candidate_signature(messages) != job['candidate_signature']:
        worker.finish(job, 'obsolete', 'Публикация изменилась; проверим новую версию')
        enqueue(worker.store, worker.config, partner, messages, latest)
        return
    status = validate_decision(value, [p['id'] for p in latest])
    if status == 'match':
        chosen = next(p for p in latest if p['id'] == value['campaign_id'])
        pid = await worker.monitor.record_post(partner, candidate.entity, messages, 'campaign: ' + value['reason'][:700], [chosen])
        worker.store.execute('UPDATE campaign_checks SET post_id=? WHERE id=?', (pid, job['id']))
    worker.finish(job, status, value['reason'], value)
    if status == 'review' and not individual(job):
        worker.store.execute("UPDATE campaign_checks SET status='error',reason='individual_review',individual_review=1,next_try_at=?,provisional_decision='' WHERE id=?", (utcnow(), job['id']))


async def prepare(worker, group, ai_ready, offline=''):
    from .campaign_queue import current, unavailable
    ready = []
    partners = {}
    for job in group:
        partners.setdefault(job['partner_id'], []).append(job)
    for jobs in partners.values():
        partner, placements = current(worker, jobs[0])
        if not partner or partner['status'] != 'active':
            continue
        try:
            entity = await worker.monitor.client.get_entity(partner['username'] or types.PeerChannel(partner['private_channel_id']))
            if entity.id != partner['channel_id']:
                for job in jobs:
                    worker.finish(job, 'obsolete', 'Канал изменился; нужна проверка')
                continue
            ids = sorted({mid for job in jobs for mid in json.loads(job['message_ids'])})
            fetched = await worker.monitor.client.get_messages(entity, ids=ids)
        except errors.FloodWaitError:
            raise
        except (OSError, asyncio.TimeoutError, errors.RPCError, ValueError):
            telegram_retry(worker, jobs)
            continue
        by_id = {m.id: m for m in fetched if isinstance(m, types.Message)}
        for job in jobs:
            _, own = current(worker, job)
            if not own:
                continue
            messages = [by_id[mid] for mid in json.loads(job['message_ids']) if mid in by_id]
            if len(messages) != len(json.loads(job['message_ids'])):
                if offline and not job.get('provisional_decision'):
                    unavailable(worker,job,archived=job['status']=='archived_no_match')
                    continue
                from .archived_checks import check_saved_caption
                from .campaign_queue import defer
                try:
                    result = await check_saved_caption(worker, job, partner, own, entity, ai_ready)
                except CampaignAPIError as exc:
                    defer(worker,[job],exc)
                    return ready,True
                except (ValueError,TypeError):
                    defer(worker,[job],'invalid_batch_response')
                    return ready,True
                if result in {'requested', 'waiting'}:
                    return ready, True
                if result != 'cached':
                    unavailable(worker, job)
                continue
            if candidate_signature(messages) != job['candidate_signature']:
                worker.finish(job, 'obsolete', 'Публикация отредактирована; проверим новую версию')
                enqueue(worker.store, worker.config, partner, messages, own)
                continue
            recorded = worker.store.rows('SELECT message_ids FROM posts WHERE partner_id=?', (partner['id'],))
            if any(set(m.id for m in messages) & set(json.loads(p['message_ids'])) for p in recorded):
                worker.finish(job, 'obsolete', 'Публикация уже учтена')
                continue
            actual = combined_content(messages)
            candidate = Candidate(job, partner, own, entity, messages, actual)
            snapshot = packed({'text': actual.text, 'urls': actual.urls, 'media': actual.media,
                               'published_at': messages[0].date.isoformat(), 'views': getattr(messages[0], 'views', None)})
            worker.store.execute('UPDATE campaign_checks SET candidate_snapshot=? WHERE id=?', (snapshot, job['id']))
            if job.get('provisional_decision'):
                try:
                    value = json.loads(job['provisional_decision'])
                    validate_decision(value, [p['id'] for p in own])
                except (ValueError, TypeError):
                    worker.store.execute("UPDATE campaign_checks SET provisional_decision='' WHERE id=?", (job['id'],))
                else:
                    await commit(worker, candidate, value, messages)
                    continue
            exact = [p for p in own if offline_match(messages, p).matched]
            if len(exact) == 1:
                pid = await worker.monitor.record_post(partner, entity, messages, 'offline:совпадает согласованный материал', exact)
                worker.store.execute('UPDATE campaign_checks SET post_id=? WHERE id=?', (pid, job['id']))
                worker.finish(job, 'match', 'Совпадает согласованный материал без обращения к ИИ')
                continue
            if job['status'] in {'unavailable', 'archived_no_match'}:
                worker.store.execute("UPDATE campaign_checks SET status='pending',reason='' WHERE id=?", (job['id'],))
            ready.append(candidate)
    return ready, False


def collect_more(worker, ready):
    if not getattr(worker.monitor, 'campaign_background', False) or len(ready) >= GROUP_SIZE:
        return False
    if any(c.job['attempts'] or c.job['status'] == 'error' for c in ready):
        return False  # Retries and existing backlogs are never held for collection.
    oldest = min(datetime.fromisoformat(c.job['created_at']) for c in ready)
    return (datetime.now(timezone.utc) - oldest).total_seconds() < COLLECT_SECONDS


async def process_group(worker, group, ai_ready, offline=''):
    from .campaign_queue import current, defer
    ready, requested = await prepare(worker, group, ai_ready, offline)
    if requested or not ready:
        return False
    if offline:
        from .campaign_local import check_candidate
        for candidate in ready:
            await check_candidate(worker,candidate,offline)
        return True
    if not ai_ready or collect_more(worker, ready):
        return False
    variable=getattr(worker.judge,'key_variable',None)
    if variable and not os.environ.get(variable,'').strip():
        for candidate in ready:
            worker.store.execute("UPDATE campaign_checks SET reason='missing_api_key' WHERE id=?",(candidate.job['id'],))
        return False
    references = ready[0].placements
    if len(references) > MAX_CAMPAIGNS:
        for candidate in ready:
            worker.finish(candidate.job, 'review', 'Слишком много одновременно подходящих кампаний; уточните материал')
        return True
    model = model_identity(worker.config)
    canonical = {creative_key(p, model): p['id'] for p in references}
    content = []
    selected = []
    mappings = {}
    request_id = None
    try:
        for placement in references:
            content.append({'type': 'input_text', 'text': 'REFERENCE ' + packed({
                'campaign_id': placement['id'], 'client': placement['client_name'],
                'advertisement': placement['name'], 'text': placement['source_text'][:16000]})})
            content.extend(await worker.reference_images(placement))
        for candidate in ready:
            job = candidate.job
            parts = [{'type': 'input_text', 'text': 'PUBLICATION ' + packed({
                'job_id': job['id'], 'text': candidate.actual.text[:16000], 'urls': candidate.actual.urls})}]
            try:
                parts.extend(await worker.candidate_images(candidate.messages))
            except CampaignAPIError as exc:
                if str(exc) in {'invalid_image', 'image_too_large'}:
                    worker.finish(job, 'review', 'Изображение не прочитано; нужна сверка: ' + str(exc))
                else:
                    defer(worker, [job], exc)
                continue
            if sum(len(p.get('image_url', '')) for p in content + parts) > MAX_PAYLOAD:
                if not selected:
                    worker.finish(job, 'review', 'Набор изображений превышает размер запроса; нужна сверка')
                    continue
                break
            content.extend(parts)
            mappings[job['id']] = {canonical[creative_key(p, model)]: p['id'] for p in candidate.placements}
            selected.append(candidate)
        if not selected:
            return True
        # Telegram/media awaits can change the scope or approved reference.
        if not getattr(worker.monitor, 'scope_ready', True) or any(not current(worker, c.job)[1] for c in selected):
            return False
        request_id = reserve_request(worker, [c.job for c in selected], mappings)
        if len(selected) == 1 and individual(selected[0].job) and callable(getattr(type(worker.judge), 'compare', None)):
            values = {selected[0].job['id']: await asyncio.to_thread(worker.judge.compare, content)}
        else:
            values = await asyncio.to_thread(worker.judge.compare_many, content, [c.job['id'] for c in selected])
        if not isinstance(values, dict) or set(values) != {c.job['id'] for c in selected}:
            raise CampaignAPIError('incomplete_batch_response')
        canonical_ids = [p['id'] for p in references]
        for value in values.values():
            validate_decision(value, canonical_ids)
        # Save every validated result BEFORE another Telegram request. A restart
        # or FloodWait must not pay for the same successful inference again.
        for candidate in selected:
            job = candidate.job
            value = dict(values[job['id']])
            if value['campaign_id'] is not None:
                value['campaign_id'] = mappings[job['id']][value['campaign_id']]
            values[job['id']] = value
            worker.store.execute('UPDATE campaign_checks SET provisional_decision=?,provisional_at=? WHERE id=?',
                                 (packed(value), utcnow(), job['id']))
        finish_request(worker, request_id)
    except CampaignAPIError as exc:
        if request_id:
            finish_request(worker, request_id, exc)
        defer(worker, [c.job for c in selected] or [c.job for c in ready], exc)
        return False
    except (ValueError, KeyError, TypeError):
        error = CampaignAPIError('invalid_batch_response')
        if request_id:
            finish_request(worker, request_id, error)
        defer(worker, [c.job for c in selected] or [c.job for c in ready], error)
        return False
    by_partner = {}
    for candidate in selected:
        by_partner.setdefault(candidate.partner['id'], []).append(candidate)
    for candidates in by_partner.values():
        ids = sorted({mid for c in candidates for mid in json.loads(c.job['message_ids'])})
        try:
            refreshed = await worker.monitor.client.get_messages(candidates[0].entity, ids=ids)
        except errors.FloodWaitError:
            raise  # All provisional results are already durable.
        except (OSError, asyncio.TimeoutError, errors.RPCError, ValueError):
            telegram_retry(worker, [c.job for c in candidates])
            continue
        by_id = {m.id: m for m in refreshed if isinstance(m, types.Message)}
        for candidate in candidates:
            messages = [by_id[mid] for mid in json.loads(candidate.job['message_ids']) if mid in by_id]
            await commit(worker, candidate, values[candidate.job['id']], messages)
    return True
