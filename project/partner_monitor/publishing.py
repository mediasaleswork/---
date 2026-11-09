"""Immediate local/Drive archiving, independently throttled native Sheet writes."""
import asyncio
from datetime import datetime, timezone
import logging
from zoneinfo import ZoneInfo

from .evidence_archive import archive_evidence, backup_database, report_fingerprint
from .reports import GoogleReporter, export_xlsx

log = logging.getLogger(__name__)


def sync_key(config):
    return "google_sync_attempt:" + config.spreadsheet_id


def seconds_until_sync(config, store, now=None):
    if not config.google_credentials or not config.spreadsheet_id:
        return config.google_sync_interval_seconds
    now = now or datetime.now(timezone.utc)
    stamp = store.state(sync_key(config), "")
    if not stamp:
        return 0.0
    previous = datetime.fromisoformat(stamp)
    return max(0.0, config.google_sync_interval_seconds - (now - previous).total_seconds())


async def publish_reports(config, store):
    data = store.dump()
    await asyncio.to_thread(export_xlsx, *data, config.runtime_dir / "report.xlsx", timezone=config.timezone)
    day = datetime.now(timezone.utc).astimezone(ZoneInfo(config.timezone)).date()
    await asyncio.to_thread(backup_database, config.db_path, config.runtime_dir / "backups", day)
    if not config.google_credentials or not config.spreadsheet_id:
        return
    fingerprint = report_fingerprint(*data)
    archive_key = "google_archive_fingerprint:" + config.spreadsheet_id
    needs_archive = store.state(archive_key, "") != fingerprint
    due = seconds_until_sync(config, store) <= 0
    if not needs_archive and not due:
        return
    reporter = GoogleReporter(config.google_credentials, config.spreadsheet_id, config.drive_folder_id, config.timezone)
    if needs_archive:
        links = await asyncio.to_thread(archive_evidence, reporter, *data)
        for sid, url in links.items():
            store.execute("UPDATE snapshots SET screenshot_url=? WHERE id=?", (url, int(sid)))
        store.set_state(archive_key, fingerprint)
        log.info("Архив сохранён: публикаций %s, замеров %s", len(data[1]), len(data[2]))
    if seconds_until_sync(config, store) > 0:
        return
    # Reserve BEFORE Google writes. A partial conversion failure or a restart
    # must never resume the old five-second rewrite loop.
    stamp = datetime.now(timezone.utc)
    store.set_state(sync_key(config), stamp.isoformat())
    try:
        links = await asyncio.to_thread(reporter.sync, *data)
        for sid, url in links.items():
            store.execute("UPDATE snapshots SET screenshot_url=? WHERE id=?", (url, int(sid)))
        store.set_state("google_sync_success:" + config.spreadsheet_id, datetime.now(timezone.utc).isoformat())
        log.info("Google Sheets обновлён: публикаций %s; интервал %s секунд", len(data[1]), config.google_sync_interval_seconds)
    except Exception as exc:
        log.warning("Google Sheets временно недоступен (%s); архив сохранён, повтор не раньше чем через %s секунд", type(exc).__name__, config.google_sync_interval_seconds)


async def report_loop(monitor, publish=None):
    publish = publish or publish_reports
    while True:
        delay = max(1.0, seconds_until_sync(monitor.config, monitor.store))
        try:
            await asyncio.wait_for(monitor.changed.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass
        monitor.changed.clear()
        try:
            await publish(monitor.config, monitor.store)
        except Exception as exc:
            log.warning("Сохранение отчёта отложено (%s); данные остаются в базе", type(exc).__name__)
            await asyncio.sleep(60)
            monitor.changed.set()
