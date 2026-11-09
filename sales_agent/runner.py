from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .research import Gemini, Deferred, validate
from .sheets import Sheets
from .sources import collect, relevant
from .store import Store, stamp

log = logging.getLogger(__name__)


def cycle(cfg, store, *, now=None, researcher=None, collect_sources=True):
    now = now or datetime.now(timezone.utc)
    run = store.db.execute("INSERT INTO runs(started,status) VALUES(?,?)", (stamp(), "running")).lastrowid
    store.db.commit()
    new_count, count, status, detail = 0, 0, "ok", ""
    try:
        if collect_sources:
            count = collect(cfg, store, now)
            if not store.db.execute("SELECT 1 FROM source_health WHERE status='ok'").fetchone():
                raise ValueError("No sources available")
        queue = [s for s in store.pending((now - timedelta(days=cfg.lookback_days)).isoformat()) if relevant(s)]
        # Round-robin origins avoids letting one busy news source consume every daily request.
        grouped = {}
        for item in queue:
            grouped.setdefault(item["origin"], []).append(item)
        queue = []
        while any(grouped.values()):
            for items in grouped.values():
                if items:
                    queue.append(items.pop(0))
        researcher = researcher or Gemini(cfg, store)
        for offset in range(0, len(queue), cfg.batch_size):
            batch = queue[offset:offset + cfg.batch_size]
            candidates = researcher.classify(batch, now)
            sources = {r["id"]:r for r in batch}
            overflow = set()
            for candidate in candidates:
                lead = validate(candidate, sources, now)
                if lead:
                    if new_count >= cfg.max_leads:
                        overflow.add(candidate["source_id"])
                    elif store.add_lead(lead):
                        new_count += 1
            store.processed([r["id"] for r in batch if r["id"] not in overflow])
            if new_count >= cfg.max_leads:
                break
        detail = "Новых лидов нет; источники проверены" if not new_count else "Новые лиды и скрипты подготовлены; перед обращением нужна CRM-проверка"
        if not queue:
            detail += "; в очереди нет свежих коммерческих сигналов"
    except Deferred as exc:
        status, detail = "deferred", str(exc)
    except Exception as exc:
        status, detail = "error", type(exc).__name__ + "; очередь и подготовленные лиды сохранены"
    store.db.execute("UPDATE runs SET finished=?,status=?,sources=?,leads=?,detail=? WHERE id=?", (stamp(),status,count,new_count,detail,run))
    store.db.commit()
    store.set_state("last_researched", now.isoformat())
    return {"status":status, "sources":count, "new_leads":new_count, "detail":detail}


def due(store, cfg, now):
    local = now.astimezone(ZoneInfo(cfg.timezone))
    if local.weekday() >= 5 or local.hour < cfg.hour:
        return False
    return store.state("last_schedule_day") != local.date().isoformat()


def run(cfg, once=False):
    store = Store(cfg.data_dir / "sales.sqlite3")
    try:
        # Credentials and workbook are checked before any model calls.
        sheet = Sheets(cfg)
        sheet.verify()
        while True:
            now = datetime.now(timezone.utc)
            try:
                # B2 is the only agent control. It is read on each minute/sync.
                paused = sheet.read("Работа агента", "B2:B2")
                paused = bool(paused and str(paused[0][0]).strip().casefold() in {"true","да","пауза","1"})
                localday = now.astimezone(ZoneInfo(cfg.timezone)).date().isoformat()
                retry_at = store.state("next_retry")
                retry_due = retry_at and datetime.fromisoformat(retry_at) <= now
                if once or not paused and (due(store, cfg, now) or retry_due):
                    result = cycle(cfg, store, now=now)
                    log.info("Mediasales: %s", json.dumps(result,ensure_ascii=False))
                    store.set_state("last_schedule_day",localday)
                    if result["status"] != "ok":
                        store.set_state("next_retry",(now+timedelta(hours=4)).isoformat())
                    else:
                        store.set_state("next_retry", "")
                # Retry Google writes without re-running research/model calls.
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
