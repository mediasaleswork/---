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
    milestones_hours: tuple[float, ...] = (24, 48)
    spreadsheet_id: str = ""
    google_credentials: Path | None = None
    drive_folder_id: str | None = None
    conversation_mode: bool = False
    default_client: str = ""
    allowed_channels: tuple[str, ...] = ()
    channel_plans: tuple[dict, ...] = ()
    data_namespace: str = ""
    test_mode: bool = False
    campaign_matching: bool = False
    campaign_provider: str = "gemini"
    campaign_model: str = "gemini-3.1-flash-lite"
    campaign_daily_limit: int = 50
    campaign_batch_size: int = 3
    campaign_total_limit: int = 0
    channel_folder: str = ""
    channel_folder_watch: bool = False

    @property
    def runtime_dir(self) -> Path:
        return self.data_dir / self.data_namespace if self.data_namespace else self.data_dir

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def db_path(self) -> Path:
        return self.runtime_dir / "monitor.sqlite3"

    def permits(self, ref) -> bool:
        if not self.allowed_channels:
            return not self.test_mode and not self.channel_folder_watch
        key = ref.username.lower() if ref.username else f"c:{ref.private_channel_id}"
        return key in self.allowed_channels

    def validate_run(self):
        if self.test_mode and (not self.allowed_channels or not self.data_namespace):
            raise ValueError("Тест: заполните allowed_channels и отдельный data_namespace")
        folder_scope = self.channel_folder_watch and self.channel_folder and not self.test_mode
        if self.channel_folder_watch and not self.channel_folder:
            raise ValueError("channel_folder_watch: задайте channel_folder")
        if self.conversation_mode and not self.allowed_channels and not folder_scope:
            raise ValueError("conversation_mode: задайте явный список allowed_channels")
        if self.campaign_matching and (not self.conversation_mode or not (self.allowed_channels or folder_scope)):
            raise ValueError("campaign_matching: нужны conversation_mode и allowed_channels")

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
    allowed = {"source_group", "keywords", "domains", "timezone", "interval_seconds", "lookback_days", "batch_size", "default_window_days", "realtime_channel_limit", "milestones_hours", "spreadsheet_id", "google_credentials", "drive_folder_id", "conversation_mode", "default_client", "allowed_channels", "channel_plans", "data_namespace", "test_mode"}
    allowed.update({"campaign_matching", "campaign_provider", "campaign_model", "campaign_daily_limit", "campaign_batch_size", "campaign_total_limit"})
    allowed.update({"channel_folder", "channel_folder_watch"})
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
    raw_milestones = data.get("milestones_hours", [24, 48])
    if not isinstance(raw_milestones, list) or any(type(h) not in (int, float) for h in raw_milestones):
        raise ValueError("milestones_hours: ожидается список чисел, например [24, 48, 168]")
    milestones = tuple(sorted(set(float(h) for h in raw_milestones)))
    if any(not 0 < h <= 8760 for h in milestones):
        raise ValueError("milestones_hours: положительные часы, не больше года")
    credentials = data.get("google_credentials")
    from .scope import read_scope
    scope = read_scope(data)
    campaign = {"campaign_matching": data.get("campaign_matching", False),
                "campaign_provider": data.get("campaign_provider", "gemini"),
                "campaign_model": data.get("campaign_model", "gemini-3.1-flash-lite"),
                "campaign_daily_limit": data.get("campaign_daily_limit", 50),
                "campaign_batch_size": data.get("campaign_batch_size", 3),
                "campaign_total_limit": data.get("campaign_total_limit", 0)}
    folder = {"channel_folder": data.get("channel_folder", ""), "channel_folder_watch": data.get("channel_folder_watch", False)}
    if not isinstance(folder["channel_folder"], str) or len(folder["channel_folder"]) > 100:
        raise ValueError("channel_folder: название папки, до 100 символов")
    if type(folder["channel_folder_watch"]) is not bool:
        raise ValueError("channel_folder_watch: ожидается true или false")
    if type(campaign["campaign_matching"]) is not bool:
        raise ValueError("campaign_matching: ожидается true или false")
    if campaign["campaign_provider"] not in {"gemini", "openai"}:
        raise ValueError("campaign_provider: gemini или openai")
    if not isinstance(campaign["campaign_model"], str) or not campaign["campaign_model"].strip():
        raise ValueError("campaign_model: укажите имя модели")
    for key, maximum in (("campaign_daily_limit", 10000), ("campaign_batch_size", 10)):
        if type(campaign[key]) is not int or not 1 <= campaign[key] <= maximum:
            raise ValueError(f"{key}: целое число от 1 до {maximum}")
    if type(campaign["campaign_total_limit"]) is not int or not 0 <= campaign["campaign_total_limit"] <= 1000000:
        raise ValueError("campaign_total_limit: 0 (без общего лимита) или положительное целое до 1000000")
    return Config(root=path.parent, source_group=source, keywords=data.get("keywords", []), domains=data.get("domains", []), timezone=timezone, milestones_hours=milestones, realtime_channel_limit=realtime_limit,
                  spreadsheet_id=str(data.get("spreadsheet_id", "")), google_credentials=(path.parent / credentials).resolve() if credentials else None,
                  drive_folder_id=data.get("drive_folder_id") or None, **numbers, **scope, **campaign, **folder)
