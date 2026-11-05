from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml


@dataclass(frozen=True)
class Config:
    root: Path
    source_group: str | int
    keywords: list[str]
    domains: list[str]
    timezone: str = "Asia/Tashkent"
    interval_seconds: int = 300
    lookback_days: int = 7
    batch_size: int = 200
    default_window_days: int = 7
    realtime_channel_limit: int = 10
    milestones_hours: tuple[float, ...] = (24, 48, 168)
    spreadsheet_id: str = ""
    google_credentials: Path | None = None
    drive_folder_id: str | None = None

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "monitor.sqlite3"

    def telegram_credentials(self) -> tuple[int, str]:
        try:
            api_id = int(os.environ.get("TELEGRAM_API_ID", "0"))
        except ValueError as exc:
            raise ValueError("TELEGRAM_API_ID должен быть числом") from exc
        api_hash = os.environ.get("TELEGRAM_API_HASH", "")
        if api_id <= 0 or len(api_hash) != 32 or any(c not in "0123456789abcdefABCDEF" for c in api_hash):
            raise ValueError("Задайте TELEGRAM_API_ID и TELEGRAM_API_HASH из my.telegram.org в окружении")
        return api_id, api_hash


def load_config(path: Path) -> Config:
    path = path.resolve()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("Конфигурация должна быть YAML-объектом")
    allowed = {"source_group", "keywords", "domains", "timezone", "interval_seconds", "lookback_days", "batch_size", "default_window_days", "realtime_channel_limit", "milestones_hours", "spreadsheet_id", "google_credentials", "drive_folder_id"}
    unknown = set(data) - allowed
    if unknown:
        raise ValueError("Неизвестные настройки: " + ", ".join(sorted(unknown)))
    for key in ("keywords", "domains"):
        if not isinstance(data.get(key, []), list) or any(not isinstance(x, str) or not x.strip() for x in data.get(key, [])):
            raise ValueError(f"{key}: ожидается список непустых строк")
    timezone = data.get("timezone", "Asia/Tashkent")
    ZoneInfo(timezone)
    source = data.get("source_group", "")
    if isinstance(source, str) and source.lstrip("-").isdigit():
        source = int(source)
    if not isinstance(source, (str, int)):
        raise ValueError("source_group: укажите @username или числовой ID группы")
    numbers = {k: int(data.get(k, default)) for k, default in (("interval_seconds", 300), ("lookback_days", 7), ("batch_size", 200), ("default_window_days", 7))}
    if numbers["interval_seconds"] < 30 or not 1 <= numbers["lookback_days"] <= 365 or not 1 <= numbers["batch_size"] <= 1000:
        raise ValueError("interval_seconds >= 30, lookback_days: 1–365, batch_size: 1–1000")
    if not 1 <= numbers["default_window_days"] <= 365:
        raise ValueError("default_window_days: 1–365 дней")
    realtime_limit = data.get("realtime_channel_limit", 10)
    if type(realtime_limit) is not int or not 0 <= realtime_limit <= 10:
        raise ValueError("realtime_channel_limit: целое число от 0 до 10")
    raw_milestones = data.get("milestones_hours", [24, 48, 168])
    if not isinstance(raw_milestones, list) or any(type(h) not in (int, float) for h in raw_milestones):
        raise ValueError("milestones_hours: ожидается список чисел, например [24, 48, 168]")
    milestones = tuple(sorted(set(float(h) for h in raw_milestones)))
    if any(not 0 < h <= 8760 for h in milestones):
        raise ValueError("milestones_hours: положительные часы, не больше года")
    credentials = data.get("google_credentials")
    return Config(root=path.parent, source_group=source, keywords=data.get("keywords", []), domains=data.get("domains", []), timezone=timezone, milestones_hours=milestones, realtime_channel_limit=realtime_limit,
                  spreadsheet_id=str(data.get("spreadsheet_id", "")), google_credentials=(path.parent / credentials).resolve() if credentials else None,
                  drive_folder_id=data.get("drive_folder_id") or None, **numbers)
