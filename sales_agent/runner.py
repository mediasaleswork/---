from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections import deque
from contextlib import closing
from datetime import datetime, timedelta, timezone

from .research import Deferred, validate, enrich_contact, sponsored_article
from .rules import RuleResearcher
from .sheets import Sheets
from .sources import collect, relevant
from .store import Store, normalized, stamp

log = logging.getLogger(__name__)


def pending_batches(store,since,batch_size,oldest_first):
    cursor=None
    while page:=store.pending(since,cursor,oldest_first):
        cursor=(page[-1]['published'],page[-1]['id'])
        grouped={}
        ignored=[]
        for item in page:
            if relevant(item):
                grouped.setdefault(item['origin'],deque()).append(item)
            else:
                ignored.append(item['id'])
        store.processed(ignored)
        origins=deque(grouped.values())
        batch=[]
        while origins:
            source=origins.popleft()
            batch.append(source.popleft())
            if source:
                origins.append(source)
            if len(batch)==batch_size:
                yield batch
                batch=[]
        if batch:
            yield batch


def cycle(cfg, store, *, now=None, researcher=None, collect_sources=True):
    now = now or datetime.now(timezone.utc)
    started=stamp()
    run = store.db.execute("INSERT INTO runs(started,status) VALUES(?,?)", (started, "running")).lastrowid
    store.db.commit()
    new_count, count, status, detail = 0, 0, "ok", ""
    publications_before=store.db.execute('SELECT COUNT(*) FROM publications').fetchone()[0]
    try:
        if store.state('research_engine')!='rules-2':
            # Re-evaluate previously model-processed public stories once; keep all CRM rows.
            with store.db:
                store.db.execute('UPDATE sources SET processed=0 WHERE published>=?',((now-timedelta(days=cfg.lookback_days)).isoformat(),))
            store.set_state('research_engine','rules-2')
        available=True
        if collect_sources:
            count = collect(cfg, store, now)
            available=bool(store.db.execute("SELECT 1 FROM source_health WHERE status='ok' AND checked>=?",(started,)).fetchone())
        since=(now-timedelta(days=cfg.lookback_days)).isoformat()
        companies=[lead['company'] for lead in store.leads()]
        known={normalized(company) for company in companies}
        researcher = researcher or RuleResearcher(companies)
        had_candidates=False
        capped=False
        for batch in pending_batches(store,since,cfg.batch_size,oldest_first=run%2==0):
            had_candidates=True
            candidates = researcher.classify(batch, now)
            sources = {r["id"]:r for r in batch}
            overflow = set()
            identified={c['source_id'] for c in candidates}
            for item in batch:
                if item['id'] in identified:continue
                try:
                    article=sponsored_article(item)
                except Deferred:
                    overflow.add(item['id'])
                    continue
                if article:
                    for c in researcher.classify([article],now):
                        if c['company']==article['article_company']:
                            candidates.append(c)
                            sources[item['id']]=article
            for candidate in candidates:
                lead = validate(candidate, sources, now)
                if lead:
                    key=normalized(lead['company'])
                    if new_count >= cfg.max_leads and key not in known:
                        overflow.add(candidate["source_id"])
                        capped=True
                    else:
                        lead=enrich_contact(lead,sources[candidate['source_id']])
                        if article_url:=sources[candidate['source_id']].get('article_url'):
                            lead['evidence_url']=article_url
                            lead['contact']+=' · Источник цитаты: '+article_url
                        if store.add_lead(lead):
                            new_count += 1
                            known.add(key)
            store.processed([r["id"] for r in batch if r["id"] not in overflow])
        new_publications=store.db.execute('SELECT COUNT(*) FROM publications').fetchone()[0]-publications_before
        detail = "Новых компаний: "+str(new_count)+", публикаций: "+str(new_publications)+". История размещений — на листе «Публикации». Перед обращением нужна проверка CRM"
        if not had_candidates:
            detail += "; в очереди нет свежих коммерческих сигналов"
        if not available:
            status="error"
            detail += "; ни один источник не ответил в этом цикле"
        store.set_state('drain_after_cap', '1' if capped else '0')
        if not collect_sources:
            detail += "; обработана сохранённая очередь без повторного обхода каналов"
    except Deferred as exc:
        status, detail = "deferred", str(exc)
    except Exception as exc:
        status, detail = "error", type(exc).__name__ + "; очередь и подготовленные лиды сохранены"
    store.db.execute("UPDATE runs SET finished=?,status=?,sources=?,leads=?,detail=? WHERE id=?", (stamp(),status,count,new_count,detail,run))
    store.db.commit()
    if collect_sources:
        store.set_state("last_researched", now.isoformat())
    return {"status":status, "sources":count, "new_leads":new_count, "new_publications":store.db.execute('SELECT COUNT(*) FROM publications').fetchone()[0]-publications_before, "detail":detail}


def due(store, cfg, now):
    last=store.state('last_researched')
    return not last or now-datetime.fromisoformat(last)>=timedelta(minutes=cfg.scan_interval_minutes)


def saved_queue_due(store, cfg, now):
    if store.state('drain_after_cap','1')!='1':
        return False
    since=(now-timedelta(days=cfg.lookback_days)).isoformat()
    return store.db.execute('SELECT 1 FROM sources WHERE processed=0 AND published>=? LIMIT 1',(since,)).fetchone() is not None


def run(cfg, once=False):
    # SQLite releases this process lock on exit/crash on both Linux and Windows.
    # Keep it off the sales database so normal reads and durable commits continue.
    with closing(sqlite3.connect(cfg.data_dir / 'sales-runner.lock',timeout=0)) as lock:
        try:
            lock.execute('BEGIN IMMEDIATE')
        except sqlite3.OperationalError as exc:
            if exc.sqlite_errorcode in (sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED):
                raise RuntimeError("Mediasales уже выполняется на этом томе") from exc
            raise
        run_locked(cfg,once)


def run_locked(cfg, once):
    store = Store(cfg.data_dir / "sales.sqlite3")
    try:
        with store.db:
            store.db.execute("""UPDATE runs SET status='interrupted',detail=?
                WHERE status='running' AND finished IS NULL""",
                ('Процесс прерван до записи итога. Время окончания неизвестно. Очередь сохранена.',))
        # Google access and workbook identity are checked before monitoring.
        sheet = Sheets(cfg)
        verified = False
        while True:
            now = datetime.now(timezone.utc)
            try:
                if not verified:
                    sheet.verify()
                    verified = True
                # B2 is the agent control, including for a one-off scan.
                paused = sheet.read("Работа агента", "B2:B2")
                paused = bool(paused and paused[0] and str(paused[0][0]).strip().casefold() in {"true","да","пауза","1"})
                retry_at = store.state("next_retry")
                retry_due = retry_at and datetime.fromisoformat(retry_at) <= now
                scan = once or due(store, cfg, now) or bool(retry_due)
                if not paused and (scan or saved_queue_due(store,cfg,now)):
                    result = cycle(cfg, store, now=now,collect_sources=scan)
                    log.info("Mediasales: %s", json.dumps(result,ensure_ascii=False))
                    if result["status"] != "ok":
                        store.set_state("next_retry",(now+timedelta(minutes=5)).isoformat())
                    elif scan:
                        store.set_state("next_retry", "")
                # Retry Google writes independently of source collection.
                sheet.sync(store)
                policy = cfg.data_dir / 'storage-retention.json'
                if policy.exists():
                    try:
                        from .audit_storage import pack
                        from .store import old_sources
                        storage = json.loads(policy.read_text(encoding='utf8'))
                        old_sources(store.db, now, int(storage['source_days']), apply=True)
                        pack(cfg.data_dir, int(storage['audit_hours']), apply=True, limit=1)
                    except Exception:
                        log.exception('Storage maintenance deferred')
                if once:
                    return
            except Exception as exc:
                log.error("Mediasales sync: %s; durable results will retry",type(exc).__name__)
                if once:
                    raise
            time.sleep(300)
    finally:
        store.close()
