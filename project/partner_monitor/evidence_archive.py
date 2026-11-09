"""Immutable private evidence manifests and daily compressed local DB backups."""
import gzip
from contextlib import closing
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import uuid

log = logging.getLogger(__name__)


def report_payload(partners, posts, snapshots, *, include_urls=True):
    def clean(value):
        if isinstance(value, dict):
            omitted = {"intake", "last_message_id", "last_checked_at", "screenshot_path"}
            if not include_urls:
                omitted.add("screenshot_url")
            return {key: clean(item) for key, item in value.items() if key not in omitted}
        if isinstance(value, (list, tuple)):
            return [clean(item) for item in value]
        return value
    return {"schema_version": 1, "partners": clean(partners), "posts": clean(posts), "snapshots": clean(snapshots)}


def report_fingerprint(*data):
    raw = json.dumps(report_payload(*data, include_urls=False), ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def archive_evidence(reporter, partners, posts, snapshots):
    """Upload every successful picture, then an immutable portable JSON ledger."""
    from .google_compact import verify_target, verify_history_preserved
    metadata = verify_target(reporter)
    verify_history_preserved(reporter, posts, snapshots, metadata)
    links = {}
    for snapshot in snapshots:
        url = reporter._upload(snapshot)
        if url:
            snapshot["screenshot_url"] = url
            links[str(snapshot["id"])] = url
    if not reporter.drive_folder_id or not posts:
        return links
    raw = json.dumps(report_payload(partners, posts, snapshots), ensure_ascii=False, sort_keys=True).encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    found = reporter._json("GET", "https://www.googleapis.com/drive/v3/files", params={
        "q": f"'{reporter.drive_folder_id}' in parents and trashed=false and appProperties has {{ key='partner_monitor_manifest' and value='{digest}' }}",
        "fields": "files(id)", "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"})
    if found.get("files"):
        return links
    metadata = {"name": f"PartnerMonitor-evidence-{digest[:20]}.json.gz", "parents": [reporter.drive_folder_id],
                "appProperties": {"partner_monitor_manifest": digest, "post_count": str(len(posts)), "snapshot_count": str(len(snapshots))}}
    boundary = "partner_archive_" + uuid.uuid4().hex
    payload = gzip.compress(raw, mtime=0)
    body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode()
            + json.dumps(metadata).encode() + f"\r\n--{boundary}\r\nContent-Type: application/gzip\r\n\r\n".encode()
            + payload + f"\r\n--{boundary}--\r\n".encode())
    result = reporter._json("POST", "https://www.googleapis.com/upload/drive/v3/files",
        params={"uploadType": "multipart", "supportsAllDrives": "true", "fields": "id"},
        data=body, headers={"Content-Type": f"multipart/related; boundary={boundary}"})
    if not result.get("id"):
        raise ValueError("Drive не подтвердил резервный архив")
    return links


def backup_database(db_path, directory, day):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = directory / f"monitor-{day.isoformat()}.sqlite3.gz"
    if target.exists():
        return target
    scratch = directory / ("backup-" + uuid.uuid4().hex + ".sqlite3")
    temporary = target.with_suffix(".tmp")
    try:
        with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)) as source, closing(sqlite3.connect(scratch)) as destination:
            source.backup(destination)
        with scratch.open("rb") as source, gzip.open(temporary, "wb") as destination:
            shutil.copyfileobj(source, destination, length=1024 * 1024)
        os.replace(temporary, target)
        log.info("Резервная копия базы сохранена: %s", target.name)
        return target
    finally:
        scratch.unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)
