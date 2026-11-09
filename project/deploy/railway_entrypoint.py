"""Railway setup container and explicit start of the existing monitor CLI."""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
import signal
import sys
import threading

import yaml

from partner_monitor.config import load_config

log = logging.getLogger(__name__)


def prepare_config(root: Path, environment):
    path = root / "config.yaml"
    configured = environment.get("MONITOR_CONFIG_FILE", "").strip()
    source = Path(configured) if configured else path if path.exists() else root / "config.example.yaml"
    supplied = environment.get("MONITOR_CONFIG_YAML", "").strip()
    data = yaml.safe_load(supplied if supplied else source.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("config.yaml must contain a YAML mapping")
    if environment.get("SOURCE_GROUP", "").strip():
        data["source_group"] = environment["SOURCE_GROUP"].strip()
    for variable, field in (("GOOGLE_SPREADSHEET_ID", "spreadsheet_id"),
                            ("GOOGLE_DRIVE_FOLDER_ID", "drive_folder_id"),
                            ("GOOGLE_CREDENTIALS_FILE", "google_credentials")):
        if environment.get(variable, "").strip():
            data[field] = environment[variable].strip()
    if environment.get("GOOGLE_SYNC_INTERVAL_SECONDS", "").strip():
        data["google_sync_interval_seconds"] = int(environment["GOOGLE_SYNC_INTERVAL_SECONDS"])
    if environment.get("CAMPAIGN_MATCHING", "").strip():
        value = environment["CAMPAIGN_MATCHING"].strip().lower()
        if value not in {"true", "false"}:
            raise ValueError("CAMPAIGN_MATCHING must be true or false")
        data["campaign_matching"] = value == "true"
    if environment.get("CHANNEL_FOLDER", "").strip():
        data["channel_folder"] = environment["CHANNEL_FOLDER"].strip()
    if environment.get("CHANNEL_FOLDER_WATCH", "").strip():
        value = environment["CHANNEL_FOLDER_WATCH"].strip().lower()
        if value not in {"true", "false"}:
            raise ValueError("CHANNEL_FOLDER_WATCH must be true or false")
        data["channel_folder_watch"] = value == "true"
    for variable, field in (("CAMPAIGN_PROVIDER", "campaign_provider"), ("CAMPAIGN_MODEL", "campaign_model"),
                            ("CAMPAIGN_DAILY_LIMIT", "campaign_daily_limit"),
                            ("CAMPAIGN_TOTAL_LIMIT", "campaign_total_limit"),
                            ("CAMPAIGN_BATCH_SIZE", "campaign_batch_size")):
        if environment.get(variable, "").strip():
            value = environment[variable].strip()
            data[field] = value if field in {"campaign_model", "campaign_provider"} else int(value)
    temporary = path.with_suffix(".yaml.tmp")
    temporary.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    temporary.replace(path)
    return load_config(path)


def validate_volume(config, environment):
    if not environment.get("RAILWAY_PROJECT_ID"):
        return
    mount = environment.get("RAILWAY_VOLUME_MOUNT_PATH")
    if not mount or Path(mount).resolve() != config.data_dir.resolve():
        raise ValueError("Attach a persistent Railway volume at /app/data before starting this service")


async def verify_account(config, expected_user_id=None):
    from partner_monitor.engine import client_for

    client = client_for(config)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise ValueError("Telegram login is required; use setup mode and run partner-monitor login")
        account = await client.get_me()
        if account is None or account.bot:
            raise ValueError("This service requires an authorized Telegram user account")
        if expected_user_id is not None and account.id != expected_user_id:
            raise ValueError("The saved Telegram session belongs to a different account")
    finally:
        await client.disconnect()


def wait_for_setup():
    stopped = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stopped.set())
    log.info("Setup mode: container is ready; Telegram monitoring is disabled. See deploy/RAILWAY.md.")
    stopped.wait()


def main(root=None, environment=None):
    os.umask(0o077)
    environment = os.environ if environment is None else environment
    root = Path(root) if root is not None else Path(__file__).resolve().parents[1]
    mode = environment.get("MONITOR_MODE", "setup").strip().lower()
    if mode not in {"setup", "run"}:
        raise ValueError("MONITOR_MODE must be setup or run")
    config = prepare_config(root, environment)
    validate_volume(config, environment)
    config.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if mode == "setup":
        wait_for_setup()
        return 0
    if not config.source_group:
        raise ValueError("Set SOURCE_GROUP before switching MONITOR_MODE to run")
    config.validate_run()
    config.telegram_credentials()
    if not (config.data_dir / "telegram.session").is_file():
        raise ValueError("Telegram session is missing; finish login in setup mode first")
    expected_id = environment.get("TELEGRAM_ACCOUNT_ID", "").strip()
    if expected_id and (not expected_id.isdecimal() or int(expected_id) <= 0):
        raise ValueError("TELEGRAM_ACCOUNT_ID must be a positive user ID")
    asyncio.run(verify_account(config, int(expected_id) if expected_id else None))
    log.info("Telegram account verified; starting the configured group monitor")
    os.execv(sys.executable, [sys.executable, "-m", "partner_monitor", "--config", str(root / "config.yaml"), "run"])
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        raise SystemExit(main())
    except Exception as exc:
        # Third-party errors may contain connection details; print only own validation messages.
        log.error("%s", str(exc) if isinstance(exc, ValueError) else type(exc).__name__)
        raise SystemExit(1)

