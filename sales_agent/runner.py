from __future__ import annotations

import json
import logging
import time
from collections import deque
from datetime import datetime, timedelta, timezone

from .research import Deferred, validate, enrich_contact
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
        for batch in pending_batches(store,since,cfg.batch_size,oldest_first=run%2==0):
            had_candidates=True
            candidates = researcher.classify(batch, now)
            sources = {r["id"]:r for r in batch}
            overflow = set()
            for candidate in candidates:
                lead = validate(candidate, sources, now)
                if lead:
                    key=normalized(lead['company'])
                    if new_count >= cfg.max_leads and key not in known:
                        overflow.add(candidate["source_id"])
                    else:
                        lead=enrich_contact(lead,sources[candidate['source_id']])
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
    except Deferred as exc:
        status, detail = "deferred", str(exc)
    except Exception as exc:
        status, detail = "error", type(exc).__name__ + "; очередь и подготовленные лиды сохранены"
    store.db.execute("UPDATE runs SET finished=?,status=?,sources=?,leads=?,detail=? WHERE id=?", (stamp(),status,count,new_count,detail,run))
    store.db.commit()
    store.set_state("last_researched", now.isoformat())
    return {"status":status, "sources":count, "new_leads":new_count, "new_publications":store.db.execute('SELECT COUNT(*) FROM publications').fetchone()[0]-publications_before, "detail":detail}


def due(store, cfg, now):
    last=store.state('last_researched')
    return not last or now-datetime.fromisoformat(last)>=timedelta(minutes=cfg.scan_interval_minutes)


def run(cfg, once=False):
    store = Store(cfg.data_dir / "sales.sqlite3")
    try:
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
                if not paused and (once or due(store, cfg, now) or retry_due):
                    result = cycle(cfg, store, now=now)
                    log.info("Mediasales: %s", json.dumps(result,ensure_ascii=False))
                    if result["status"] != "ok":
                        store.set_state("next_retry",(now+timedelta(minutes=5)).isoformat())
                    else:
                        store.set_state("next_retry", "")
                # Retry Google writes independently of source collection.
                sheet.sync(store)
                if once:
                    return
            except Exception as exc:
                log.error("Mediasales sync: %s; durable results will retry",type(exc).__name__)
                if once:
                    raise
            time.sleep(300)
    finally:
        store.close()
