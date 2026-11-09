"""Mirror the compact Excel presentation into a dedicated native Google Sheet.

Drive's documented update-and-convert operation keeps the file ID and embeds
private screenshots without public IMAGE URLs or extra technical tabs.
"""
from __future__ import annotations

from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit

REPORT_SHEETS = ("Отчёт", "Недоступные посты", "Реестр публикаций", "История просмотров", "Партнёры")
HIDDEN_SHEETS = frozenset(REPORT_SHEETS[2:])
MARKER = "partner_monitor_compact"
VALUE = "v1"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
SHEETS = "application/vnd.google-apps.spreadsheet"


def verify_target(reporter):
    metadata = reporter._json("GET", f"https://www.googleapis.com/drive/v3/files/{reporter.spreadsheet_id}",
        params={"fields": "mimeType,appProperties", "supportsAllDrives": "true"})
    sheet = reporter._json("GET", f"https://sheets.googleapis.com/v4/spreadsheets/{reporter.spreadsheet_id}",
        params={"fields": "sheets.properties,developerMetadata"})
    names = {s["properties"]["title"] for s in sheet.get("sheets", [])}
    owned = metadata.get("appProperties", {}).get(MARKER) == VALUE or any(
        m.get("metadataKey") == MARKER and m.get("metadataValue") == VALUE for m in sheet.get("developerMetadata", []))
    if metadata.get("mimeType") != SHEETS or not owned or names != set(REPORT_SHEETS):
        raise ValueError("Компактный отчёт: нужна отдельная копия шаблона с отметкой partner_monitor_compact=v1 и пятью листами образца")


def replace_workbook(reporter, path):
    data = Path(path).read_bytes()
    response = reporter.session.request("PATCH",
        f"https://www.googleapis.com/upload/drive/v3/files/{reporter.spreadsheet_id}",
        params={"uploadType": "resumable", "supportsAllDrives": "true", "fields": "id"},
        json={"mimeType": SHEETS, "appProperties": {MARKER: VALUE}},
        headers={"X-Upload-Content-Type": XLSX, "X-Upload-Content-Length": str(len(data))}, timeout=60)
    response.raise_for_status()
    location = response.headers.get("Location", "")
    parsed = urlsplit(location)
    if parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.endswith(".googleapis.com"):
        raise ValueError("Google Drive не предоставил корректный адрес загрузки")
    uploaded = reporter.session.request("PUT", location, data=data, headers={"Content-Type": XLSX}, timeout=120)
    uploaded.raise_for_status()
    if uploaded.json().get("id") != reporter.spreadsheet_id:
        raise ValueError("Google Drive вернул другой ID отчёта")


def restore_pictures(reporter, snapshots, scratch):
    for snapshot in snapshots:
        path = snapshot.get("screenshot_path")
        if path and Path(path).is_file():
            continue
        if snapshot.get("screenshot_status") not in {"ok", "success"}:
            continue
        match = re.fullmatch(r"https://drive\.google\.com/file/d/([A-Za-z0-9_-]+)/view(?:\?.*)?", snapshot.get("screenshot_url") or "")
        if not match:
            continue
        response = reporter.session.request("GET", f"https://www.googleapis.com/drive/v3/files/{match[1]}",
            params={"alt": "media", "supportsAllDrives": "true"}, timeout=60)
        response.raise_for_status()
        data = response.content
        if len(data) > 20 * 1024 * 1024:
            raise ValueError("Сохранённый снимок превышает 20 МБ")
        target = Path(scratch) / f"evidence-{snapshot['id']}.png"
        target.write_bytes(data)
        snapshot["screenshot_path"] = str(target)


def sync_compact(reporter, partners, posts, snapshots):
    verify_target(reporter)
    from .unified_report import export_unified
    from .report_view import build_view
    copies = [dict(s) for s in snapshots]
    links = {}
    for snapshot in copies:
        url = reporter._upload(snapshot)
        if url:
            snapshot["screenshot_url"] = url
            links[str(snapshot["id"])] = url
    with tempfile.TemporaryDirectory(prefix="partner-google-") as scratch:
        restore_pictures(reporter, copies, scratch)
        output = Path(scratch) / "report.xlsx"
        export_unified(partners, posts, copies, output, reporter.timezone, compact=True)
        view = build_view(partners, posts, copies, reporter.timezone)
        replace_workbook(reporter, output)
    base = f"https://sheets.googleapis.com/v4/spreadsheets/{reporter.spreadsheet_id}"
    result = reporter._json("GET", base, params={"fields": "sheets.properties"})
    sheets = result.get("sheets", [])
    if {s["properties"]["title"] for s in sheets} != set(REPORT_SHEETS):
        raise ValueError("После конвертации состав листов отличается от шаблона")
    requests = [
        {"updateSpreadsheetProperties": {"properties": {"timeZone": reporter.timezone}, "fields": "timeZone"}},
        *[{"updateSheetProperties": {"properties": {"sheetId": s["properties"]["sheetId"],
             "hidden": s["properties"]["title"] in HIDDEN_SHEETS}, "fields": "hidden"}} for s in sheets]
    ]
    # Excel's internal links reference sheet names. After conversion, bind them
    # to the actual Google sheet IDs so navigation still works after replacement.
    gids = {s["properties"]["title"]:s["properties"]["sheetId"] for s in sheets}
    partner_ids = {p["id"]:p["partner_id"] for p in posts}
    ordered = sorted(view["records"], key=lambda p:(p["client"].casefold(),p["channel"].casefold(),partner_ids[p["id"]],p["published"],p["id"]))
    start = max(8,8+len(view["summary"]))+6
    anchors = {p["id"]:start+i for i,p in enumerate(ordered)}
    def link(sheet,row,col,target,address):
        uri=f"https://docs.google.com/spreadsheets/d/{reporter.spreadsheet_id}/edit#gid={gids[target]}&range={address}"
        requests.append({"repeatCell":{"range":{"sheetId":gids[sheet],"startRowIndex":row-1,"endRowIndex":row,
            "startColumnIndex":col-1,"endColumnIndex":col},"cell":{"userEnteredFormat":{"textFormat":{"link":{"uri":uri}}}},
            "fields":"userEnteredFormat.textFormat.link"}})
    for i,group in enumerate(view["summary"],9):
        if group["first_post_id"] in anchors:
            link("Отчёт",i,12,"Отчёт",f"A{anchors[group['first_post_id']]}")
    selected=[p for p in posts if p["id"] in anchors]
    for i,post in enumerate(selected,2):
        link("Реестр публикаций",i,9,"Отчёт",f"A{anchors[post['id']]}")
        link("Отчёт",anchors[post["id"]],1,"Реестр публикаций",f"A{i}")
    for i,record in enumerate(view["unavailable"],2):
        link("Недоступные посты",i,9,"Отчёт",f"A{anchors[record['id']]}")
    reporter._json("POST", base + ":batchUpdate", json={"requests":requests})
    return links
