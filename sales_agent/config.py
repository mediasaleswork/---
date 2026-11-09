from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo


@dataclass
class Config:
    data_dir: Path = Path("data")
    spreadsheet_id: str = ""
    credentials_file: Path = Path("data/google.json")
    model: str = "gemini-3.1-flash-lite"
    timezone: str = "Asia/Tashkent"
    hour: int = 9
    lookback_days: int = 14
    daily_requests: int = 6
    max_leads: int = 10
    batch_size: int = 15
    channels: list[str] = field(default_factory=list)
    feeds: list[str] = field(default_factory=lambda: [
        "https://www.spot.uz/ru/rss/", "https://www.gazeta.uz/ru/rss/",
        "https://kun.uz/news/rss?lang=ru", "https://www.uzdaily.uz/ru/rss/",
    ])

    @classmethod
    def load(cls):
        raw = json.loads(os.environ.get("SALES_CONFIG_JSON", "{}"))
        root = Path(os.environ.get("SALES_DATA_DIR", raw.pop("data_dir", "data"))).resolve()
        root.mkdir(parents=True, exist_ok=True)
        raw["data_dir"] = root
        raw["credentials_file"] = root / "google.json"
        cfg = cls(**raw)
        ZoneInfo(cfg.timezone)
        if not 0 <= cfg.hour <= 23 or not 1 <= cfg.daily_requests <= 20:
            raise ValueError("Invalid schedule or request budget")
        if not 1 <= cfg.lookback_days <= 30 or not 1 <= cfg.batch_size <= 20 or not 1 <= cfg.max_leads <= 20:
            raise ValueError("Invalid research limits")
        supplied = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")
        if supplied:
            value = json.loads(supplied)
            if value.get("type") not in {"authorized_user", "service_account"}:
                raise ValueError("Unsupported Google credential")
            temp = cfg.credentials_file.with_suffix(".tmp")
            temp.write_text(json.dumps(value), encoding="utf-8")
            temp.chmod(0o600)
            temp.replace(cfg.credentials_file)
        return cfg
