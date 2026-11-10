"""Deterministic quota fallback. No model, HTTP provider or billable requests.

Only concrete creative identity can count an exit. A local non-match remains
eligible for an additional semantic check, and never proves a missing placement.
"""
import asyncio
import base64
from difflib import SequenceMatcher
import hashlib
import io
import json
import os
import re
from urllib.parse import parse_qs, urlsplit

from PIL import Image, ImageStat
from telethon import errors, types

from .briefs import _fields
from .campaigns import CampaignAPIError, candidate_signature, enqueue
from .fallback import offline_match
from .parsing import _normalize
from .storage import utcnow


def local_reason(worker, now):
    from .campaign_queue import budget
    if not budget(worker):
        return 'api_budget'
    variable = getattr(worker.judge, 'key_variable', None)
    if isinstance(variable,str) and not os.environ.get(variable, '').strip():
        return 'missing_api_key'
    if worker.store.state('campaign_quota_retry_at', '') > now:
        return 'api_quota'
    if worker.store.state('campaign_service_retry_at', '') > now:
        return 'api_service'
    return ''


def reference_texts(placement):
    if placement.get('ad_type') == 'рекламный материал':
        return [placement['source_text']]
    fields = _fields(placement['source_text'])
    return [fields.get('текст рекламы', ''), *json.loads(placement.get('keywords') or '[]')]


def tokens(text):
    return re.findall(r"[\w']+", _normalize(text))


def text_copy(actual, placement):
    actual = tokens(actual[:16000])
    for text in reference_texts(placement):
        source = tokens(text[:16000])
        if len(source) < 16 or len(source)>600 or len(actual)>3000 or len(set(source)) < 10:
            continue
        numbers = {t for t in source if any(c.isdigit() for c in t)}
        if not numbers.issubset(actual):
            continue
        blocks = [b for b in SequenceMatcher(None, source, actual, autojunk=False).get_matching_blocks() if b.size]
        if not blocks:
            continue
        span=actual[blocks[0].b:blocks[-1].b+blocks[-1].size]
        if [t for t in source if any(c.isdigit() for c in t)] != [t for t in span if any(c.isdigit() for c in t)]:
            continue
        if sum(b.size for b in blocks) / len(source) >= .95 and max(b.size for b in blocks) >= 8:
            return True
    return False


def video_ids(text, links=()):
    result = set()
    for value in [*re.findall(r'https?://[^\s<>]+', text), *links]:
        try:
            url = urlsplit(value.rstrip('.,!);'))
            host = (url.hostname or '').casefold().removeprefix('www.')
            if host == 'youtu.be':
                ident = url.path.strip('/').split('/')[0]
            elif host in {'youtube.com', 'm.youtube.com'}:
                ident = parse_qs(url.query).get('v', [''])[0]
                if url.path.startswith(('/shorts/', '/embed/')):
                    ident = url.path.split('/')[2]
            else:
                continue
            if re.fullmatch(r'[A-Za-z0-9_-]{11}', ident):
                result.add(ident)
        except (ValueError, IndexError):
            continue
    return result


def image_fingerprints(parts):
    result = set()
    for part in parts:
        if part.get('type') != 'input_image':
            continue
        raw = base64.b64decode(part['image_url'].split(',', 1)[1], validate=True)
        with Image.open(io.BytesIO(raw)) as image:
            # Solid backgrounds and tiny icons do not identify an advertisement.
            if min(image.size) < 64 or ImageStat.Stat(image.convert('L')).stddev[0] < 12:
                continue
            rgb = image.convert('RGB')
            result.add(hashlib.sha256(str(rgb.size).encode() + rgb.tobytes()).hexdigest())
    return result


async def check_candidate(worker, candidate, reason):
    from .campaign_queue import current, defer
    from .campaign_batching import telegram_retry
    job = candidate.job
    matches = {}
    for placement in candidate.placements:
        exact = offline_match(candidate.messages, placement)
        if exact.matched:
            matches[placement['id']] = exact.reason
        elif await asyncio.to_thread(text_copy,candidate.actual.text,placement):
            matches[placement['id']] = 'offline:совпадает полный рекламный текст с небольшими правками'
        elif video_ids(placement['source_text']) & video_ids(candidate.actual.text, candidate.actual.urls):
            matches[placement['id']] = 'offline:совпадает точный идентификатор рекламируемого видео'
    media_error = ''
    if not matches and candidate.actual.media:
        try:
            actual = await asyncio.to_thread(image_fingerprints, await worker.candidate_images(candidate.messages))
            if actual:
                for placement in candidate.placements:
                    expected = await asyncio.to_thread(image_fingerprints, await worker.reference_images(placement))
                    if actual & expected:
                        matches[placement['id']] = 'offline:совпадают пиксели согласованного рекламного изображения'
        except errors.FloodWaitError:
            raise
        except (OSError, asyncio.TimeoutError, errors.RPCError, ValueError):
            telegram_retry(worker, [job])
            return
        except CampaignAPIError as exc:
            if str(exc) in {'invalid_image', 'image_too_large'}:
                media_error = str(exc)
            else:
                defer(worker, [job], exc)
                return
    # Media retrieval yields the event loop. Validate both versions again.
    if not getattr(worker.monitor, 'scope_ready', True):
        return
    partner, placements = current(worker, job)
    if not partner:
        worker.finish(job, 'obsolete', 'Материал изменился во время локальной проверки')
        return
    if partner['status'] != 'active':
        return
    if matches:
        try:
            refreshed = await worker.monitor.client.get_messages(candidate.entity, ids=json.loads(job['message_ids']))
        except errors.FloodWaitError:
            raise
        except (OSError, asyncio.TimeoutError, errors.RPCError, ValueError):
            telegram_retry(worker, [job])
            return
        messages = [m for m in refreshed if isinstance(m, types.Message)]
        if len(messages) != len(candidate.messages):
            from .campaign_queue import unavailable
            unavailable(worker, job)
            return
        if candidate_signature(messages) != job['candidate_signature']:
            worker.finish(job, 'obsolete', 'Публикация изменилась во время локальной проверки')
            enqueue(worker.store, worker.config, partner, messages, placements)
            return
        partner, placements = current(worker, job)
        if not partner or partner['status']!='active' or not getattr(worker.monitor, 'scope_ready', True):
            return
        if len(matches) == 1:
            ident, match_reason = next(iter(matches.items()))
            chosen = next(p for p in placements if p['id'] == ident)
            pid = await worker.monitor.record_post(partner, candidate.entity, messages, match_reason, [chosen])
            worker.store.execute('UPDATE campaign_checks SET post_id=? WHERE id=?', (pid, job['id']))
            worker.finish(job, 'match', match_reason, {'mode': 'local', 'campaign_id': ident})
            return
    # Keep local checks distinct from authoritative AI negatives. Only plausible
    # or ambiguous matches enter the human-review list; other checks are logged.
    suspect = bool(matches or media_error or job['priority'] >= 200 or job.get('individual_review'))
    status = 'offline_review' if suspect else 'local_checked'
    detail = ('Несколько подходящих материалов; нужна сверка.' if matches else
              'Изображение не прочитано: ' + media_error if media_error else
              'Точное совпадение не подтверждено; смысловое сравнение не выполнялось.')
    worker.finish(job, status, 'Проверено без ИИ. ' + detail,
                  {'mode': 'local', 'trigger': reason, 'matching_campaign_ids': sorted(matches)})
    worker.store.execute("UPDATE campaign_checks SET next_try_at=? WHERE id=?", (utcnow(), job['id']))
