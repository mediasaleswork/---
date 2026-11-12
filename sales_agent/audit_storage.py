"""Losslessly pack completed repair snapshots. The live database is never selected."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import time


def eligible(root, hours):
    if not 0 <= hours <= 8760:
        raise ValueError('hours must be between 0 and 8760')
    cutoff = time.time() - hours * 3600
    for folder in sorted(root.iterdir()):
        if folder.is_symlink() or not re.fullmatch(r'(?:brand-repair|rating-subject-repair)-\d{8}T\d{6}|queue-audit-\d{8}-[a-z0-9-]+', folder.name):
            continue
        file = folder/'sales.sqlite3'
        completed = any((folder/name).is_file() for name in ('sheets.json','original-sheets.json'))
        if not completed or file.is_symlink() or not file.is_file() or file.stat().st_mtime > cutoff:
            continue
        if not any(folder.glob('sales.sqlite3-*')):
            yield file


def pack(root, hours, *, apply=False, limit=None):
    results = []
    for file in eligible(root, hours):
        st = file.stat()
        item = {'file':str(file.relative_to(root)), 'bytes':st.st_size,
                'modified':datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat()}
        results.append(item)
        if not apply:
            continue
        target = file.with_suffix('.sqlite3.gz')
        if target.is_symlink():
            raise ValueError('unexpected symlink at archive destination')
        with tempfile.TemporaryDirectory(dir=file.parent) as scratch:
            tmp = Path(scratch)/'compressed.gz'
            if not target.exists():
                with file.open('rb') as source,gzip.open(tmp,'wb',compresslevel=6) as zipped:
                    shutil.copyfileobj(source,zipped,1<<20)
            candidate = target if target.exists() else tmp
            restored=Path(scratch)/'restore.sqlite3'
            with gzip.open(candidate,'rb') as zipped,restored.open('wb') as output:
                shutil.copyfileobj(zipped,output,1<<20)
            with closing(sqlite3.connect(restored.as_uri()+'?mode=ro&immutable=1',uri=True)) as db:
                if db.execute('PRAGMA integrity_check').fetchall()!=[('ok',)]:
                    raise ValueError('restored audit snapshot failed integrity_check')
            with restored.open('rb') as copy,file.open('rb') as source:
                if hashlib.file_digest(copy,'sha256').digest()!=hashlib.file_digest(source,'sha256').digest():
                    raise ValueError('restored audit snapshot differs')
            if candidate == tmp:
                with tmp.open('rb+') as stream:
                    os.fsync(stream.fileno())
                os.link(tmp,target)
            current=file.lstat()
            if st.st_mtime_ns != current.st_mtime_ns or not os.path.samestat(st,current) or any(file.parent.glob('sales.sqlite3-*')):
                raise ValueError('audit snapshot changed during compression')
            file.unlink()
            item['compressed_bytes']=target.stat().st_size
        if limit and len(results)>=limit:
            break
    return results


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path('/app/data'))
    parser.add_argument('--hours',type=int,default=24)
    parser.add_argument('--source-days',type=int,default=30)
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run',action='store_true')
    mode.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    from .store import Store, old_sources
    root=args.root.resolve()
    file=root/'sales.sqlite3'
    if not file.is_file():
        parser.error('sales.sqlite3 is missing')
    now=datetime.now(timezone.utc)
    with closing(sqlite3.connect(file.as_uri()+'?mode=ro',uri=True)) as db:
        summary=old_sources(db,now,args.source_days)
    if args.apply:
        store=Store(file)
        try:
            summary['compacted']=old_sources(store.db,now,args.source_days,apply=True)
        finally:
            store.close()
    print(json.dumps({'sources':summary,'audit_files':pack(root,args.hours,apply=args.apply,limit=1)}))


if __name__=='__main__':
    main()
