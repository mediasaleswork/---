"""Excel presentation and a Google mirror of the approved compact workbook.

Compact mode replaces only a separately marked report copy. Legacy detailed
mode retains its managed BRB_ tabs. Neither mode changes sharing permissions.
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter


POST_HEADERS = ["Канал", "Ссылка на пост", "Опубликован", "Последняя проверка", "Просмотры",
                "Скриншот", "Основание совпадения", "Статус", "Ошибка", "Текст", "Заявки", "Реклама", "Тип рекламы", "Длительность, ч"]
HISTORY_HEADERS = ["Канал", "Ссылка на пост", "Опубликован", "Просмотры измерены", "Просмотры",
                   "Контрольная точка", "Скриншот", "Статус скриншота", "Ошибка скриншота",
                   "Проверка запланирована на", "Скриншот сделан"]
PARTNER_HEADERS = ["Канал", "Ссылка на канал", "Статус", "Последняя ошибка", "Заявка", "Реклама",
                   "Тип рекламы", "Длительность, ч", "Условия", "Публикация от", "Публикация до",
                   "Состояние заявки", "Требуется уточнить", "Период по умолчанию", "Найденные посты", "Исходное сообщение", "Исходный текст",
                   "Последние просмотры", "Проверено", "Скриншоты", "Контроль длительности"]
TABS = ("BRB_Публикации", "BRB_История", "BRB_Партнёры", "BRB_Сводка", "BRB_Задания", "BRB_Разбор", "BRB_Недоступные")
SUMMARY_HEADERS = ["Клиент", "Инструмент", "Язык", "План", "Отработали",
                   "Остаток", "Выходов в базе", "Недоступны сейчас",
                   "Со скриншотом", "Последний выход", "Площадки", "Договор", "Учтено до запуска"]
JOB_HEADERS = ["Инструмент", "Ссылка на пост", "Контрольная точка", "Проверка запланирована на",
               "Состояние замера", "Фактическое время замера", "Примечание", "Просмотры", "Скриншот", "Состояние снимка"]
INTAKE_HEADERS = ["№ сообщения", "Реклама", "Клиент", "Этап", "Текст", "Требуется уточнить", "Получено"]
GOOGLE_DATE_COLUMNS = ((2, 3, 18, 19), (2, 3, 9, 10), (9, 10), (9,), (3, 5), (6,), (2, 3, 18, 19))


def _datetime(value: str | datetime | None) -> datetime | None:
    if not value:
        return None
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt_timezone.utc)


def _time(value: str | datetime | None, tz: ZoneInfo) -> str:
    value = _datetime(value)
    return value.astimezone(tz).strftime("%d.%m.%Y %H:%M:%S %Z") if value else ""


def _channel(partner: dict) -> str:
    return str(partner.get("title") or partner.get("username") or partner.get("id", ""))


def _channel_url(partner: dict) -> str:
    username = partner.get("username")
    return f"https://t.me/{username}" if username and re.fullmatch(r"[A-Za-z0-9_]+", str(username)) else str(partner.get("source_url") or "")


def _source_url(placement: dict) -> str:
    chat, mid = str(placement.get("source_chat_id", "")), placement.get("source_message_id")
    return f"https://t.me/c/{int(chat.removeprefix('-100'))}/{mid}" if chat.startswith("-100") and mid else ""


def _shot_link(snapshot: dict) -> str:
    return str(snapshot.get("screenshot_url") or snapshot.get("screenshot_path") or "")


def _google_value(value):
    if value is None:
        return ""
    if isinstance(value, str):
        # Sheets accepts at most 50,000 characters in one cell. Bounding UTF-16
        # code units is safe for both BMP text and emoji (surrogate pairs).
        return value.encode("utf-16-le", errors="replace")[:100000].decode("utf-16-le", errors="ignore")
    return value


def _google_date(value, timezone):
    """Sheets serial number in the report's timezone; naive values are local."""
    if not value:
        return ""
    date = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if date.tzinfo is not None:
        date = date.astimezone(ZoneInfo(timezone)).replace(tzinfo=None)
    return (date - datetime(1899, 12, 30)).total_seconds() / 86400


def _google_tables(partners, posts, snapshots, timezone):
    from .report_view import build_view

    publications, history, briefs, by_post = _tables(partners, posts, snapshots, timezone)
    view = build_view(partners, posts, snapshots, timezone)
    record_by_id = {row["id"]: row for row in view["records"]}
    brief_by_id = {b["id"]: b for p in partners for b in p.get("placements", [])}
    best = {}
    for snapshot in _ordered(snapshots):
        if snapshot.get("screenshot_url") and snapshot.get("screenshot_status") not in {
                "error", "failed", "invalid", "unavailable", "pending"}:
            best[snapshot["post_id"]] = snapshot
    clients_by_post = {}
    publications[0] = publications[0] + ["Клиент", "Язык", "Месяц", "Состояние",
        "Плановое снятие", "Скриншот сделан", "Статус последнего снимка", "Ошибка последнего снимка"]
    publications[0][0] = "Инструмент"
    for row, post in zip(publications[1:], posts):
        record = record_by_id[post["id"]]
        checks = by_post[post["id"]]
        latest = checks[-1] if checks else {}
        picture = best.get(post["id"], {})
        linked = [brief_by_id[i] for i in post.get("placement_ids", []) if i in brief_by_id]
        clients_by_post[post["id"]] = {b.get("client_name") or "Клиент не указан" for b in linked} or {"Клиент не указан"}
        row[2] = _google_date(post.get("published_at"), timezone)
        row[3] = _google_date(post.get("last_checked_at") or latest.get("checked_at"), timezone)
        row[5] = picture.get("screenshot_url", "")
        row.extend([record["client"],
                    record["languages"], record["month"], record["state"],
                    _google_date(record["until"], timezone),
                    _google_date(picture.get("screenshot_at") or picture.get("checked_at"), timezone),
                    latest.get("screenshot_status", ""), latest.get("screenshot_error") or ""])
    history[0] = history[0] + ["Клиент", "Язык"]
    history[0][0] = "Инструмент"
    valid_snapshots = [s for s in _ordered(snapshots) if s["post_id"] in record_by_id]
    post_by_id = {p["id"]: p for p in posts}
    for row, snapshot in zip(history[1:], valid_snapshots):
        post = post_by_id[snapshot["post_id"]]
        record = record_by_id[post["id"]]
        for index, value in ((2, post.get("published_at")), (3, snapshot.get("checked_at")),
                             (9, snapshot.get("due_at")), (10, snapshot.get("screenshot_at"))):
            row[index] = _google_date(value, timezone)
        row.extend([record["client"], record["languages"]])
    briefs[0] = briefs[0] + ["Клиент", "Язык", "План заявки"]
    briefs[0][0] = "Инструмент"
    briefs[0][1] = "Площадки"
    for row in briefs[1:]:
        row.extend([""] * (len(PARTNER_HEADERS) - len(row)))
        brief = brief_by_id.get(row[4])
        if brief:
            row[9] = _google_date(brief.get("starts_at"), timezone)
            row[10] = _google_date(brief.get("ends_at"), timezone)
            row[19] = "\n".join(best[p["id"]]["screenshot_url"] for p in posts
                if brief["id"] in p.get("placement_ids", []) and p["id"] in best)
        row.extend([brief.get("client_name", ""), brief.get("language", ""), brief.get("planned_posts")]
                   if brief else ["", "", None])
    summary = [SUMMARY_HEADERS]
    for group in view["summary"]:
        picture_count = sum(p["id"] in best for p in posts if p["partner_id"] == group["partner_id"]
            and group["client"] in clients_by_post.get(p["id"], set()))
        if any(plan["client_name"].casefold() == group["client"].casefold() for p in partners
               if p["id"] == group["partner_id"] for plan in p.get("contract_plans", [])):
            continue
        summary.append([group["client"],
            group["channel"], group["languages"], group["plan"], group["total"], group["remaining"],
            group["visible_count"], group["unavailable_count"], picture_count,
            _google_date(group["last_published"], timezone), group["channel_url"], "", 0])
    from .contracts import contract_summary
    for group in contract_summary(partners, posts, best):
        summary.append([group["client"], group["channel"], group["language"], group["plan"], group["total"],
                        group["remaining"], group["in_database"], group["unavailable"], group["pictures"],
                        _google_date(group["last"], timezone), group["url"], group["contract"], group["baseline"]])
    partner_by_id = {p["id"]: p for p in partners}
    jobs = [JOB_HEADERS]
    states = {"pending": "Ожидает", "done": "Измерено", "missed": "Пропущено", "unavailable": "Пост недоступен"}
    for post in posts:
        checks = {s["due_key"]: s for s in by_post[post["id"]]}
        for job in post.get("measurement_jobs", []):
            snapshot = checks.get(job["due_key"], {})
            jobs.append([_channel(partner_by_id[post["partner_id"]]), post["url"],
                "Первый замер" if job["due_key"] == "initial" else job["due_key"],
                _google_date(job["due_at"], timezone), states.get(job["status"], job["status"]),
                _google_date(snapshot.get("checked_at"), timezone), job.get("last_error") or "",
                snapshot.get("views"), snapshot.get("screenshot_url") or "", snapshot.get("screenshot_status") or ""])
    intake = [INTAKE_HEADERS]
    stages = {"client": "Новый клиент", "needs_review": "Уточнить", "approved": "Согласовано",
              "publication": "Ссылка на выход", "preview": "Превью", "material": "Материал", "context": "Уточнение"}
    for partner in partners:
        for item in partner.get("intake", []):
            intake.append([item["message_id"], item.get("draft_name") or "", item.get("client_name") or "",
                stages.get(item["kind"], item["kind"]), item["text"], item.get("review_reason") or "",
                _google_date(item["received_at"], timezone)])
    unavailable = [list(publications[0])] + [list(row) for row, post in zip(publications[1:], posts) if post.get("status") in {"unavailable", "deleted"}]
    return publications, history, briefs, summary, jobs, intake, unavailable


def _ordered(snapshots: list[dict]) -> list[dict]:
    return sorted(snapshots, key=lambda x: (_datetime(x.get("checked_at")) or datetime.min.replace(tzinfo=dt_timezone.utc), str(x.get("id", ""))))


def _tables(partners: list[dict], posts: list[dict], snapshots: list[dict], timezone: str):
    tz = ZoneInfo(timezone)
    partner_by_id = {p["id"]: p for p in partners}
    placement_by_id = {b["id"]: b for p in partners for b in p.get("placements", [])}
    post_by_id = {p["id"]: p for p in posts}
    by_post = defaultdict(list)
    for snapshot in _ordered(snapshots):
        if snapshot.get("post_id") in post_by_id:
            by_post[snapshot["post_id"]].append(snapshot)
    post_rows = [POST_HEADERS]
    for post in posts:
        partner = partner_by_id.get(post["partner_id"], {})
        latest = by_post[post["id"]][-1] if by_post[post["id"]] else {}
        briefs = [placement_by_id[i] for i in post.get("placement_ids", []) if i in placement_by_id]
        post_rows.append([
            _channel(partner), post.get("url", ""), _time(post.get("published_at"), tz),
            _time(latest.get("checked_at"), tz), latest.get("views"), _shot_link(latest),
            post.get("match_reason", ""), post.get("status", ""), post.get("last_error") or "",
            str(post.get("text") or "")[:32767],
            ", ".join(str(b["id"]) for b in briefs), "; ".join(b["name"] for b in briefs),
            "; ".join(b["ad_type"] for b in briefs), "; ".join(str(b["retention_hours"]) for b in briefs if b.get("retention_hours")),
        ])
    history_rows = [HISTORY_HEADERS]
    for snapshot in _ordered(snapshots):
        post = post_by_id.get(snapshot.get("post_id"))
        if post is None:
            continue
        partner = partner_by_id.get(post["partner_id"], {})
        history_rows.append([
            _channel(partner), post.get("url", ""), _time(post.get("published_at"), tz),
            _time(snapshot.get("checked_at"), tz), snapshot.get("views"), snapshot.get("due_key", ""),
            _shot_link(snapshot), snapshot.get("screenshot_status", ""), snapshot.get("screenshot_error") or "",
            _time(snapshot.get("due_at"), tz), _time(snapshot.get("screenshot_at"), tz),
        ])
    partner_rows = [PARTNER_HEADERS]
    for partner in partners:
        base = [_channel(partner), _channel_url(partner), partner.get("status", ""), partner.get("last_error") or ""]
        if not partner.get("placements"):
            partner_rows.append(base)
        for brief in partner.get("placements", []):
            linked = [p for p in posts if brief["id"] in p.get("placement_ids", [])]
            latest = [by_post[p["id"]][-1] if by_post[p["id"]] else {} for p in linked]
            retention = []
            for post in linked:
                checks = by_post[post["id"]]
                if not brief.get("retention_hours"):
                    retention.append("Длительность не указана")
                elif post.get("status") == "unavailable":
                    retention.append("Пост недоступен при проверке; удаление не подтверждено")
                elif any(s["due_key"] == f"placement-{brief['id']}-{brief['retention_hours']:g}h" for s in checks):
                    retention.append("Доступен при итоговой проверке")
                elif (_datetime(post.get("first_seen_at")) and _datetime(post["first_seen_at"]) >=
                      _datetime(post["published_at"]) + timedelta(hours=brief["retention_hours"])):
                    retention.append("Обнаружен после окончания срока; прошлое не подтверждено")
                else:
                    retention.append("Наблюдение")
            partner_rows.append(base + [brief["id"], brief["name"], brief["ad_type"], brief.get("retention_hours"),
                brief["terms"], _time(brief["starts_at"], tz), _time(brief["ends_at"], tz), brief["status"],
                brief.get("review_reason") or "", "да" if brief.get("default_window") else "нет",
                "\n".join(brief.get("post_urls", [])), _source_url(brief), brief["source_text"],
                "\n".join(str(s["views"]) if s.get("views") is not None else "" for s in latest),
                "\n".join(_time(s.get("checked_at"), tz) for s in latest),
                "\n".join(_shot_link(s) for s in latest), "\n".join(retention)])
    return post_rows, history_rows, partner_rows, by_post


def _cell(ws, row: int, col: int, value):
    cell = ws.cell(row, col)
    # Explicit string type prevents =/+/-/@ content from becoming Excel formulas.
    if isinstance(value, str):
        cell.value = ILLEGAL_CHARACTERS_RE.sub("", value)[:32767]
        cell.data_type = "s"
        if value.startswith(("https://", "http://")) and urlparse(value).scheme in {"https", "http"}:
            cell.hyperlink = value
            cell.font = Font(color="0563C1", underline="single")
    else:
        cell.value = value
    cell.alignment = Alignment(vertical="top", wrap_text=True)
    return cell


def _write_table(ws, rows: list[list]):
    for row_no, values in enumerate(rows, start=1):
        for col_no, value in enumerate(values, start=1):
            _cell(ws, row_no, col_no, value)
    for cell in ws[1]:
        cell.fill = PatternFill("solid", fgColor="24384A")
        cell.font = Font(color="FFFFFF", bold=True)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for col in range(1, len(rows[0]) + 1):
        ws.column_dimensions[get_column_letter(col)].width = 25 if col > 1 else 28
    ws.row_dimensions[1].height = 30


def export_xlsx(partners: list[dict], posts: list[dict], snapshots: list[dict], output_path: Path,
                timezone: str = "Asia/Tashkent", month: str | None = None) -> Path:
    """One workbook with client/channel totals, post cards and embedded evidence."""
    from .unified_report import export_unified
    return export_unified(partners, posts, snapshots, output_path, timezone, month)


class GoogleReporter:
    """Mirror the compact workbook, optionally upload screenshots to Drive.

    credentials_path accepts google-auth service-account or authorized-user JSON.
    A service account must use a Shared Drive folder for uploads (personal Drive
    storage quota is unavailable to service accounts). No public sharing is set.
    """
    def __init__(self, credentials_path: Path, spreadsheet_id: str, drive_folder_id: str | None = None,
                 timezone: str = "Asia/Tashkent", layout: str = "compact"):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", spreadsheet_id):
            raise ValueError("Invalid spreadsheet ID")
        if drive_folder_id and not re.fullmatch(r"[A-Za-z0-9_-]+", drive_folder_id):
            raise ValueError("Invalid Drive folder ID")
        ZoneInfo(timezone)
        from google.auth import load_credentials_from_file
        from google.auth.transport.requests import AuthorizedSession
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        if layout not in {"compact", "detailed"}:
            raise ValueError("Unknown Google report layout")
        if drive_folder_id or layout == "compact":
            scopes.append("https://www.googleapis.com/auth/drive")
        credentials, _ = load_credentials_from_file(str(credentials_path), scopes=scopes)
        self.session = AuthorizedSession(credentials)
        self.spreadsheet_id = spreadsheet_id
        self.drive_folder_id = drive_folder_id
        self.timezone = timezone
        self.layout = layout

    def _json(self, method: str, url: str, **kwargs) -> dict:
        response = self.session.request(method, url, timeout=60, **kwargs)
        response.raise_for_status()
        return response.json()

    def _upload(self, snapshot: dict) -> str | None:
        if snapshot.get("screenshot_url"):
            return str(snapshot["screenshot_url"])
        if not self.drive_folder_id or not snapshot.get("screenshot_path"):
            return None
        path = Path(snapshot["screenshot_path"])
        if not path.is_file():
            return None
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        key = hashlib.sha256(f"{snapshot.get('post_id')}:{snapshot.get('checked_at')}:{digest}".encode()).hexdigest()
        result = self._json("GET", "https://www.googleapis.com/drive/v3/files", params={
            "q": f"'{self.drive_folder_id}' in parents and trashed = false and appProperties has {{ key='partner_monitor_key' and value='{key}' }}",
            "fields": "files(id,webViewLink)", "supportsAllDrives": "true", "includeItemsFromAllDrives": "true",
        })
        found = result.get("files", [])
        if found:
            return found[0].get("webViewLink") or f"https://drive.google.com/file/d/{found[0]['id']}/view"
        metadata = {"name": f"PartnerMonitor-{key[:20]}{path.suffix}", "parents": [self.drive_folder_id],
                    "appProperties": {"partner_monitor_key": key}}
        boundary = "partner_monitor_" + uuid.uuid4().hex
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        # Drive's multipart endpoint requires multipart/related, not the
        # multipart/form-data body generated by requests' files= parameter.
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode()
                + json.dumps(metadata).encode() + b"\r\n"
                + f"--{boundary}\r\nContent-Type: {mime}\r\n\r\n".encode()
                + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode())
        uploaded = self._json("POST", "https://www.googleapis.com/upload/drive/v3/files", params={
            "uploadType": "multipart", "supportsAllDrives": "true", "fields": "id,webViewLink",
        }, data=body, headers={"Content-Type": f"multipart/related; boundary={boundary}"})
        return uploaded.get("webViewLink") or f"https://drive.google.com/file/d/{uploaded['id']}/view"

    def sync(self, partners: list[dict], posts: list[dict], snapshots: list[dict]) -> dict:
        if self.layout == "compact":
            from .google_compact import sync_compact
            return sync_compact(self, partners, posts, snapshots)
        snapshots = [dict(s) for s in snapshots]
        uploaded = {}
        for snapshot in snapshots:
            url = self._upload(snapshot)
            if url:
                snapshot["screenshot_url"] = url
                uploaded[str(snapshot["id"])] = url
            elif not snapshot.get("screenshot_url"):
                # A server-local path is not a usable link for spreadsheet readers.
                snapshot["screenshot_path"] = ""
        tables = _google_tables(partners, posts, snapshots, self.timezone)
        base = f"https://sheets.googleapis.com/v4/spreadsheets/{self.spreadsheet_id}"
        metadata = self._json("GET", base, params={"fields": "sheets(properties)"})
        existing = {sheet["properties"]["title"]: sheet["properties"] for sheet in metadata.get("sheets", [])}
        requests = []
        grid_sizes = {}
        used_ids = {p["sheetId"] for p in existing.values()}
        for title, rows, date_columns in zip(TABS, tables, GOOGLE_DATE_COLUMNS):
            needed_rows = max(100, len(rows))
            needed_cols = len(rows[0])
            if title not in existing:
                sheet_id = next(i for i in range(1, len(used_ids) + 2) if i not in used_ids)
                used_ids.add(sheet_id)
                requests.append({"addSheet": {"properties": {"title": title, "sheetId": sheet_id, "gridProperties": {
                    "rowCount": needed_rows, "columnCount": max(needed_cols, 10), "frozenRowCount": 1}}}})
                grid_sizes[title] = needed_rows
            else:
                properties = existing[title]
                sheet_id = properties["sheetId"]
                grid = properties.get("gridProperties", {})
                current_rows = grid.get("rowCount", 1000)
                current_cols = grid.get("columnCount", 26)
                grid_sizes[title] = max(current_rows, needed_rows)
                if needed_rows > current_rows or needed_cols > current_cols:
                    requests.append({"updateSheetProperties": {"properties": {"sheetId": properties["sheetId"],
                        "gridProperties": {"rowCount": max(current_rows, needed_rows), "columnCount": max(current_cols, needed_cols)}},
                        "fields": "gridProperties.rowCount,gridProperties.columnCount"}})
            requests.extend([
                {"updateSheetProperties": {"properties": {"sheetId": sheet_id,
                    "gridProperties": {"frozenRowCount": 1}}, "fields": "gridProperties.frozenRowCount"}},
                {"repeatCell": {"range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1,
                    "startColumnIndex": 0, "endColumnIndex": needed_cols}, "cell": {"userEnteredFormat": {
                    "backgroundColor": {"red": 0.94, "green": 0.94, "blue": 0.94},
                    "textFormat": {"bold": True, "fontFamily": "Arial", "foregroundColor": {"red": 0, "green": 0, "blue": 0}},
                    "wrapStrategy": "WRAP"}}, "fields": "userEnteredFormat"}},
                {"setBasicFilter": {"filter": {"range": {"sheetId": sheet_id,
                    "startRowIndex": 0, "endRowIndex": max(1, len(rows)), "startColumnIndex": 0, "endColumnIndex": needed_cols}}}},
            ])
            if len(rows) > 1:
                for column in date_columns:
                    requests.append({"repeatCell": {"range": {"sheetId": sheet_id, "startRowIndex": 1,
                        "endRowIndex": len(rows), "startColumnIndex": column, "endColumnIndex": column + 1},
                        "cell": {"userEnteredFormat": {"numberFormat": {"type": "DATE_TIME", "pattern": "dd.mm.yyyy hh:mm:ss"}}},
                        "fields": "userEnteredFormat.numberFormat"}})
        if requests:
            self._json("POST", base + ":batchUpdate", json={"requests": requests})
        self._json("POST", base + "/values:batchUpdate", json={"valueInputOption": "RAW", "data": [
            {"range": f"'{title}'!A1", "values": [[_google_value(v) for v in row] for row in rows]}
            for title, rows in zip(TABS, tables)
        ]})
        # Clear only managed tabs' old tails, after a successful write.
        ranges = [f"'{title}'!A{len(rows)+1}:{get_column_letter(len(rows[0]))}{grid_sizes[title]}"
                  for title, rows in zip(TABS, tables) if len(rows) < grid_sizes[title]]
        if ranges:
            self._json("POST", base + "/values:batchClear", json={"ranges": ranges})
        return uploaded
