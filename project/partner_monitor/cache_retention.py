"""Expire local copies of non-image evidence already preserved in private Drive."""
import argparse
import asyncio
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3
import threading
import time

from .candidate_drive import verify_private_folder
from .config import load_config
from .reports import GoogleReporter

LOG = logging.getLogger(__name__)
TYPES = ('video/mp4', 'video/quicktime', 'audio/ogg', 'application/pdf')
API = 'https://www.googleapis.com/drive/v3/files'
FIELDS = 'id,parents,trashed,size,md5Checksum,appProperties,capabilities(canDownload)'


def verified(row, obj, folder):
    tag = hashlib.sha256((row['key'] + ':' + row['md5']).encode()).hexdigest()
    return (obj.get('id') == row['drive_id'] and obj.get('trashed') is False
            and folder in obj.get('parents', []) and obj.get('capabilities', {}).get('canDownload') is True
            and obj.get('appProperties', {}).get('msbrb_candidate') == tag
            and obj.get('md5Checksum') == row['md5'] and obj.get('size') == str(row['size']))


def remote_files(reporter, wanted):
    if len(wanted) <= 8:
        for ident in tuple(wanted):
            try:
                yield reporter._json('GET', API + '/' + ident,
                                     params={'fields': FIELDS, 'supportsAllDrives': 'true'})
            except Exception as exc:
                if getattr(getattr(exc, 'response', None), 'status_code', None) != 404:
                    raise
        return
    types = ' or '.join("mimeType='" + kind + "'" for kind in TYPES)
    params = {'q': f"'{reporter.drive_folder_id}' in parents and trashed=false and ({types})",
              'fields': f'nextPageToken,incompleteSearch,files({FIELDS})', 'pageSize': 64,
              'supportsAllDrives': 'true', 'includeItemsFromAllDrives': 'true'}
    while True:
        page = reporter._json('GET', API, params=params)
        if page.get('incompleteSearch'):
            raise ValueError('Google Drive returned an incomplete inventory')
        yield from page.get('files', [])
        if not page.get('nextPageToken'):
            return
        params['pageToken'] = page['nextPageToken']


def sweep(config, hours, *, apply=False, stop=None, pause=1, reporter=None, saved_before=None):
    if not 0 <= hours <= 24*365:
        raise ValueError('cache hours must be between 0 and 8760')
    root = config.runtime_dir / 'candidate-files'
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    if saved_before is not None:
        limit = datetime.fromisoformat(saved_before)
        if limit.tzinfo is None:
            raise ValueError('saved-before must include a timezone')
        cutoff = min(cutoff, limit.astimezone(timezone.utc).isoformat())
    select = """SELECT key,size,md5,drive_id,saved,mime FROM candidate_files
                WHERE saved<>'' AND saved<=? AND drive_id<>'' AND size>0 AND md5 IS NOT NULL
                AND mime IN (?,?,?,?)"""
    wanted = {}
    with closing(sqlite3.connect(config.db_path.resolve().as_uri()+'?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        for row in db.execute(select, (cutoff, *TYPES)):
            path = root / hashlib.sha256(row['key'].encode()).hexdigest()
            if path.is_symlink() or not path.is_file() or path.stat().st_size != row['size']:
                continue
            wanted.setdefault(row['drive_id'], []).append((row, path))
    stats = dict(mode='apply' if apply else 'dry-run', retention_hours=hours, saved_before=cutoff,
                 local_candidates=sum(map(len, wanted.values())), verified=0, bytes=0,
                 first_saved=None, last_saved=None, by_type={}, checksum_mismatch=0)
    if not wanted:
        return stats
    if reporter is None:
        reporter = GoogleReporter(config.google_credentials, config.spreadsheet_id,
                                  config.drive_folder_id, config.timezone)
    verify_private_folder(reporter)
    counts = Counter()
    for obj in remote_files(reporter, wanted):
        for row, path in wanted.pop(obj.get('id'), []):
            if stop and stop.is_set():
                return stats
            if not verified(row, obj, config.drive_folder_id):
                continue
            if apply:
                try:
                    with path.open('rb') as source:
                        before = os.fstat(source.fileno())
                        checksum = hashlib.file_digest(source, 'md5').hexdigest()
                    current = path.lstat()
                    if (not os.path.samestat(before, current) or before.st_mtime_ns != current.st_mtime_ns
                            or checksum != row['md5']):
                        stats['checksum_mismatch'] += 1
                        continue
                    path.unlink()
                except FileNotFoundError:
                    continue
            stats['verified'] += 1
            stats['bytes'] += row['size']
            counts[row['mime']] += 1
            stats['by_type'] = dict(counts)
            stats['first_saved'] = min(stats['first_saved'] or row['saved'], row['saved'])
            stats['last_saved'] = max(stats['last_saved'] or row['saved'], row['saved'])
            if apply and stats['verified'] % 4 == 0:
                if stop:
                    stop.wait(pause)
                else:
                    time.sleep(pause)
    return stats


async def maintain_cache(config):
    stop = threading.Event()
    try:
        while True:
            try:
                policy = config.runtime_dir / 'candidate-cache-hours'
                if policy.exists():
                    hours = int(policy.read_text(encoding='ascii').strip())
                    stats = await asyncio.to_thread(sweep, config, hours, apply=True, stop=stop)
                    if stats['local_candidates']:
                        LOG.info('Candidate cache: %s', stats)
            except Exception:
                LOG.exception('Candidate cache maintenance deferred')
            await asyncio.sleep(900)
    finally:
        stop.set()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path('/app/config.yaml'))
    parser.add_argument('--hours', type=int, default=1)
    parser.add_argument('--saved-before', help='upper bound from a reviewed dry-run')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true')
    mode.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(json.dumps(sweep(load_config(args.config), args.hours, apply=args.apply,
                           saved_before=args.saved_before), sort_keys=True))


if __name__ == '__main__':
    main()
