"""Build a marked demonstration from the supplied visual reference, not live data."""
import json
from pathlib import Path
from datetime import datetime, timedelta, timezone
import sys

from PIL import Image

work = Path(__file__).resolve().parent
sys.path.insert(0, str(work / "partner-monitor"))
from partner_monitor.report_view import build_view
from partner_monitor.unified_report import export_unified

assets = work / "reference-fragments"
assets.mkdir(exist_ok=True)
source = Image.open(r"C:\Users\Acer\AppData\Local\Temp\codex-clipboard-9b741f66-b23f-4911-b1ba-5550e83404b2.png")
# These are literal excerpts from the user's example, not generated Telegram
# evidence. Their illustrative attribution is shown in every sample post card.
for key, box in {"daryo": (440, 210, 532, 411), "general": (440, 414, 532, 614),
                 "bank": (440, 616, 532, 807), "davletov": (440, 811, 532, 1002)}.items():
    source.crop(box).save(assets / f"{key}.png")
source.close()
specs = [("Daryo", "Daryo", "UZ", "daryo", [137650, 137932, 138395, 138451]),
         ("GENERALISSIMUS", "u_generalissimus", "UZ", "general", [27962, 28496, 28612, 29392]),
         ("Bankir kundaligi", "bankirkundaligi", "UZ", "bank", [10472, 10545]),
         ("Davletov", "davletovuz", "UZ", "davletov", [26600, 26798, 26927])]
partners, posts, snapshots = [], [], []
for pid, (title, username, lang, asset, ids) in enumerate(specs, 1):
    brief = {"id": pid, "partner_id": pid, "client_name": "BRB (образец)", "language": lang, "planned_posts": 10,
        "source_chat_id": "sample", "source_message_id": pid, "source_text": "Макет по предоставленным изображениям.",
        "name": "Реклама BRB", "ad_type": "Пост", "terms": "Пример размещения на 24 часа", "starts_at": "2026-09-01T00:00:00+00:00",
        "ends_at": "2026-11-01T00:00:00+00:00", "retention_hours": 24, "keywords": '["BRB"]', "domains": '[]',
        "direct_ids": '[]', "status": "found", "review_reason": None, "default_window": 0,
        "post_urls": [f"https://t.me/{username}/{mid}" for mid in ids]}
    partners.append({"id": pid, "username": username, "title": title, "status": "active", "last_error": None, "placements": [brief]})
    for index, mid in enumerate(ids):
        ident = len(posts) + 1
        # Dates demonstrate September/October grouping; they are explicitly
        # marked as illustrative because the supplied images do not prove them.
        published = datetime(2026, 9 if index == 0 and pid > 1 else 10, 2 + index * 6, 9, tzinfo=timezone.utc)
        checked = published + timedelta(hours=1)
        missing = pid == 2 and index == len(ids) - 1
        posts.append({"id": ident, "partner_id": pid, "message_id": mid, "url": f"https://t.me/{username}/{mid}",
            "published_at": published.isoformat(), "first_seen_at": (published + timedelta(minutes=1)).isoformat(),
            "text": "Пример рекламного поста BRB", "match_reason": "Макет по образцу", "placement_ids": [pid],
            "status": "unavailable" if missing else "demo", "last_error": "Пример недоступности. Реальный статус не проверялся." if missing else None,
            "last_checked_at": checked.isoformat(), "unavailable_since": checked.isoformat() if missing else None,
            "evidence_note": "Иллюстрация из вашего образца; привязка снимка к ссылке в макете условная."})
        snapshots.append({"id": ident, "post_id": ident, "due_key": "initial", "due_at": checked.isoformat(),
            "checked_at": checked.isoformat(), "views": None, "screenshot_at": checked.isoformat(),
            "screenshot_path": str(assets / f"{asset}.png"), "screenshot_status": "ok", "capture_attempts": 1})
note = "МАКЕТ. План и ссылки — из вашего образца. Даты и статусы условные; просмотры неизвестны. Изображения — фрагменты присланного образца."
(work / "sample-input.json").write_text(json.dumps({"partners": partners, "posts": posts, "snapshots": snapshots, "note": note}, ensure_ascii=False), encoding="utf-8")
(work / "sample-view.json").write_text(json.dumps(build_view(partners, posts, snapshots), ensure_ascii=False), encoding="utf-8")
export_unified(partners, posts, snapshots, work / "sample-generated.xlsx", note=note)
from openpyxl import load_workbook
book = load_workbook(work / "sample-generated.xlsx")
dates = [{"sheet": sheet.title, "cell": cell.coordinate, "value": cell.value.isoformat()} for sheet in book for row in sheet for cell in row if isinstance(cell.value, datetime)]
(work / "sample-dates.json").write_text(json.dumps(dates, ensure_ascii=False), encoding="utf-8")
book.close()
print("Sample: 4 channels, 13 publications, 40 planned, 27 remaining. No live statuses or view counts asserted.")
