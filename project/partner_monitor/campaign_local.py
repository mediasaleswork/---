"""Deterministic quota fallback. No model, HTTP provider or billable requests.

Only concrete creative identity can count an exit. A local non-match remains
eligible for an additional semantic check, and never proves a missing placement.
"""
import asyncio
import base64
from difflib import SequenceMatcher
from functools import lru_cache
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

_CYRILLIC = str.maketrans(dict(zip(
    'абвгдеёжзийклмнопрстуфхцчшщъыьэюяўқғҳ',
    ['a','b','v','g','d','e','yo','j','z','i','y','k','l','m','n','o','p','r','s','t','u','f','x','ts','ch','sh','shch','','i','','e','yu','ya',"o'",'q',"g'",'h'])))


def normalized_text(text):
    # Links/timestamps and channel footers are not the creative's offer terms.
    text=re.sub(r'https?://[^\s<>]+|(?<!\w)@[\w]+',' ',_normalize(text))
    return text.translate(_CYRILLIC).replace("'",'')


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
    return re.findall(r'\w+',normalized_text(text))


def conflicting_numbers(actual,placement):
    expected={t for text in reference_texts(placement) for t in tokens(text) if any(c.isdigit() for c in t)}
    present={t for t in tokens(actual) if any(c.isdigit() for c in t)}
    return bool(expected and present and not expected.issubset(present))


def equivalent_matches(matches,placements,visuals=None):
    """Alternative references to the very same video count as one campaign."""
    if len(matches)<2:return matches
    selected=[p for p in placements if p['id'] in matches]
    identities=[video_ids(p['source_text']) for p in selected]
    common_video=bool(identities and set.intersection(*identities))
    visual_same=bool(visuals and all(p['id'] in visuals for p in selected)
                     and set.intersection(*(visuals[p['id']] for p in selected)))
    nonempty=[s for s in identities if s]
    conflicting_videos=bool(nonempty and not set.intersection(*nonempty))
    same_client=len({_normalize(p.get('client_name','')) for p in selected})==1
    terms=[{t for text in reference_texts(p) for t in tokens(text) if any(c.isdigit() for c in t)} for p in selected]
    terms=[t for t in terms if t]
    conflicting_offers=any(t!=terms[0] for t in terms[1:])
    if same_client and not conflicting_offers and (common_video or visual_same and not conflicting_videos):
        specific=[p['id'] for p in selected if video_ids(p['source_text'])]
        ident=min(specific or matches)
        return {ident:matches[ident]+'; альтернативные образцы одного рекламного материала'}
    return matches


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


@lru_cache(maxsize=32)
def image_fingerprint(url):
    raw = base64.b64decode(url.split(',', 1)[1], validate=True)
    with Image.open(io.BytesIO(raw)) as image:
        if min(image.size) < 64 or ImageStat.Stat(image.convert('L')).stddev[0] < 12:
            return None
        rgb = image.convert('RGB')
        return hashlib.sha256(str(rgb.size).encode() + rgb.tobytes()).hexdigest()


def image_fingerprints(parts):
    return {value for part in parts if part.get('type') == 'input_image'
            if (value := image_fingerprint(part['image_url'])) is not None}


async def check_candidate(worker, candidate, reason):
    from .campaign_queue import current, defer
    from .campaign_batching import telegram_retry
    job = candidate.job
    matches = {}
    visuals = {}
    conflicts = set()
    for placement in candidate.placements:
        if conflicting_numbers(candidate.actual.text,placement):
            conflicts.add(placement['id'])
            continue
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
            actual_parts=await worker.candidate_images(candidate.messages)
            actual = await asyncio.to_thread(image_fingerprints,actual_parts)
            if actual_parts:
                for placement in candidate.placements:
                    if placement['id'] in conflicts:continue
                    expected_parts=await worker.reference_images(placement)
                    expected = await asyncio.to_thread(image_fingerprints,expected_parts)
                    if actual & expected:
                        matches[placement['id']] = 'offline:совпадают пиксели согласованного рекламного изображения'
                        visuals[placement['id']]={i for i,part in enumerate(actual_parts) if image_fingerprints([part]) & expected}
                    elif expected_parts:
                        from .campaign_visual import matching_indices
                        indices=await asyncio.to_thread(matching_indices,actual_parts,expected_parts)
                        if indices:
                            matches[placement['id']] = 'offline:совпадает рекламное изображение после изменения размера, сжатия или обрезки'
                            visuals[placement['id']]=indices
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
    matches=equivalent_matches(matches,placements,visuals)
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
            enqueue(worker.store, worker.config, partner, messages, placements, replaces=job['id'])
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
    suspect = bool(matches or media_error or conflicts or job['priority'] >= 200 or job.get('individual_review'))
    status = 'offline_review' if suspect else 'local_checked'
    detail = ('Несколько подходящих материалов; нужна сверка.' if matches else
              'Числовые условия отличаются; нужна сверка.' if conflicts else
              'Изображение не прочитано: ' + media_error if media_error else
              'Точное совпадение не подтверждено; смысловое сравнение не выполнялось.')
    worker.finish(job, status, 'Проверено без ИИ. ' + detail,
                  {'mode': 'local', 'trigger': reason, 'matching_campaign_ids': sorted(matches)})
    worker.store.execute("UPDATE campaign_checks SET next_try_at=? WHERE id=?", (utcnow(), job['id']))
