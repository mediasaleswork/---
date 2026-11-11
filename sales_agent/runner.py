from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone

from .research import Deferred, validate, enrich_contact
from .rules import RuleResearcher
from .sheets import Sheets
from .sources import collect, relevant
from .store import Store, normalized, stamp

log = logging.getLogger(__name__)


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
        pending=store.pending((now - timedelta(days=cfg.lookback_days)).isoformat())
        queue = [s for s in pending if relevant(s)]
        accepted_ids={s['id'] for s in queue}
        # Drain irrelevant stories too, so a busy news channel cannot permanently hide older candidates.
        store.processed([s['id'] for s in pending if s['id'] not in accepted_ids])
        # Round-robin origins gives the configured sources a fair share of new leads.
        grouped = {}
        for item in queue:
            grouped.setdefault(item["origin"], []).append(item)
        queue = []
        while any(grouped.values()):
            for items in grouped.values():
                if items:
                    queue.append(items.pop(0))
        companies=[lead['company'] for lead in store.leads()]
        known={normalized(company) for company in companies}
        researcher = researcher or RuleResearcher(companies)
        for offset in range(0, len(queue), cfg.batch_size):
            batch = queue[offset:offset + cfg.batch_size]
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
        if not queue:
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
