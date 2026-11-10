"""Shared request accounting, safe retry hints, and immutable reference caches."""
import base64
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import io
import json
import logging
from pathlib import Path
import uuid

from PIL import Image

from .storage import utcnow

TRANSIENT = {'request_timeout', 'network_unavailable', 'http_500', 'http_502', 'http_503', 'http_504'}
MAX_CACHE_BYTES = 12 * 1024 * 1024


def retry_hint(response):
    """Retain only a bounded numeric delay, never the error body or credentials."""
    delays = []
    header = getattr(response, 'headers', {}).get('Retry-After', '')
    try:
        delays.append(float(header))
    except (TypeError, ValueError):
        try:
            delays.append((parsedate_to_datetime(header) - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            pass
    try:
        for detail in response.json().get('error', {}).get('details', []):
            if isinstance(detail, dict) and str(detail.get('@type', '')).endswith('google.rpc.RetryInfo'):
                delays.append(float(str(detail.get('retryDelay', '')).removesuffix('s')))
    except (ValueError, TypeError, AttributeError):
        pass
    valid = [delay for delay in delays if 0 < delay <= 86400]
    return max(valid, default=None)


def reserve_request(worker, jobs, mappings=None):
    """Count every HTTP attempt atomically; cached decisions spend no requests."""
    store = worker.store
    stamp = datetime.now(timezone.utc)
    ident = uuid.uuid4().hex
    day_key = 'campaign_api_attempts:' + stamp.date().isoformat()
    with store.db:
        for key in (day_key, 'campaign_api_attempts_total'):
            store.db.execute("INSERT INTO state VALUES (?, '1') ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1", (key,))
        store.db.execute("INSERT INTO state VALUES ('campaign_next_request_at',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         ((stamp + timedelta(seconds=10)).isoformat(),))
        for job in jobs:
            store.db.execute('UPDATE campaign_checks SET attempts=attempts+1 WHERE id=?', (job['id'],))
        store.db.execute('INSERT INTO campaign_requests(id,started_at,provider,model,job_ids,campaign_mappings,status) VALUES (?,?,?,?,?,?,?)',
                         (ident, stamp.isoformat(), worker.config.campaign_provider, worker.config.campaign_model,
                          json.dumps([job['id'] for job in jobs]), json.dumps(mappings or {}, sort_keys=True), 'started'))
    return ident


def finish_request(worker, ident, error=None):
    worker.store.execute('UPDATE campaign_requests SET finished_at=?,status=?,error=? WHERE id=?',
                         (utcnow(), 'error' if error else 'success', str(error) if error else None, ident))
    if not error:
        worker.store.set_state('campaign_service_failures', 0)
        worker.store.set_state('campaign_service_retry_at', '')
        worker.store.set_state('campaign_quota_failures', 0)


def cache_path(worker, key):
    return Path(worker.config.runtime_dir) / 'reference-cache' / (key + '.json')


def read_reference_cache(worker, key):
    path = cache_path(worker, key)
    try:
        if path.stat().st_size > MAX_CACHE_BYTES:
            return None
        parts = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(parts, list) or not 0 < len(parts) <= 2:
            return None
        for part in parts:
            if not isinstance(part, dict) or part.get('type') != 'input_image':
                return None
            url = part.get('image_url', '')
            if not url.startswith('data:image/jpeg;base64,'):
                return None
            raw = base64.b64decode(url.split(',', 1)[1], validate=True)
            with Image.open(io.BytesIO(raw)) as picture:
                if picture.width * picture.height > 20_000_000:
                    return None
                picture.verify()
        return parts
    except (OSError, ValueError, TypeError, Image.DecompressionBombError):
        return None


def write_reference_cache(worker, key, parts):
    if not parts:
        return
    data = json.dumps(parts)
    if len(data) > MAX_CACHE_BYTES:
        return
    try:
        path = cache_path(worker, key)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_suffix('.tmp')
        temporary.write_text(data, encoding='utf-8')
        temporary.replace(path)
    except OSError as exc:
        logging.getLogger(__name__).warning('Кэш материалов не сохранён (%s); используем полученные данные', type(exc).__name__)
