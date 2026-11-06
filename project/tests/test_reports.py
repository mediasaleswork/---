import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

from openpyxl import load_workbook
from PIL import Image

from partner_monitor.reports import GoogleReporter, TABS, _google_value, export_xlsx


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.png = self.root / "actual-screenshot.png"
        Image.new("RGB", (240, 640), "blue").save(self.png)
        self.partners = [
            {"id": 1, "username": "examplechannel", "title": "=BAD()", "status": "active", "last_error": None},
            {"id": 2, "username": None, "title": "Закрытый", "status": "unavailable", "last_error": "Нет доступа"},
        ]
        self.posts = [
            {"id": 10, "partner_id": 1, "message_id": 100, "url": "https://t.me/examplechannel/100",
             "published_at": "2026-10-31T20:00:00+00:00", "text": "=HYPERLINK(\"evil\")", "match_reason": "keyword: BRB", "status": "active"},
            {"id": 11, "partner_id": 1, "message_id": 101, "url": "https://t.me/examplechannel/101",
             "published_at": "2026-11-03T10:00:00+00:00", "text": "BRB кредит", "match_reason": "keyword: BRB", "status": "active"},
        ]
        self.snapshots = [
            {"id": 20, "post_id": 10, "checked_at": "2026-11-01T20:00:00+00:00", "views": 1200,
             "due_at": "2026-11-01T19:00:00+00:00", "screenshot_at": "2026-11-01T20:10:00+00:00",
             "screenshot_path": str(self.png), "screenshot_status": "ok", "due_key": "24h"},
            {"id": 21, "post_id": 10, "checked_at": "2026-11-02T20:00:00+00:00", "views": 1500,
             "screenshot_path": None, "screenshot_status": "error", "screenshot_error": "network", "due_key": "48h"},
            {"id": 22, "post_id": 11, "checked_at": "2026-11-03T12:00:00+00:00", "views": None,
             "screenshot_path": None, "screenshot_status": "pending", "due_key": "initial"},
        ]

    def test_excel_local_month_images_history_and_formula_safety(self):
        path = export_xlsx(self.partners, self.posts, self.snapshots, self.root / "out.xlsx", month="2026-11")
        book = load_workbook(path)
        self.addCleanup(book.close)
        self.assertEqual(book.sheetnames, ["Отчёт", "Недоступные посты", "Реестр публикаций", "История просмотров", "Партнёры"])
        registry = book["Реестр публикаций"]
        columns = {cell.value: cell.column for cell in registry[1]}
        self.assertEqual(registry.max_row, 3)  # 31 October UTC is already 1 November in Tashkent.
        self.assertEqual(registry.cell(2, columns["Площадки"]).value, "=BAD()")
        self.assertEqual(registry.cell(2, columns["Площадки"]).data_type, "s")
        for row in book["Отчёт"]:
            for cell in row:
                if cell.value == "=BAD()":
                    self.assertEqual(cell.data_type, "s")
        self.assertEqual(registry.cell(2, columns["Текст"]).data_type, "s")
        self.assertEqual(registry.cell(2, columns["Просмотры"]).value, 1500)
        self.assertIsNone(registry.cell(3, columns["Просмотры"]).value)
        self.assertEqual(registry.cell(2, columns["Опубликован"]).value, datetime(2026, 11, 1, 1, 0))
        self.assertEqual(registry.cell(2, columns["Ссылка на пост"]).hyperlink.target, self.posts[0]["url"])
        self.assertEqual(len(book["Отчёт"]._images), 1)
        self.assertEqual(book["История просмотров"].max_row, 4)
        self.assertEqual(book["Партнёры"].max_row, 3)

    def test_month_excludes_post_history_but_keeps_inaccessible_partner(self):
        path = export_xlsx(self.partners, self.posts, self.snapshots, self.root / "oct.xlsx", month="2026-10")
        book = load_workbook(path)
        self.addCleanup(book.close)
        self.assertEqual(book["Реестр публикаций"].max_row, 1)
        self.assertEqual(book["История просмотров"].max_row, 1)
        sheet = book["Партнёры"]
        error_column = next(cell.column for cell in sheet[1] if cell.value == "Последняя ошибка")
        self.assertEqual(sheet.cell(3, error_column).value, "Нет доступа")

    def test_history_distinguishes_schedule_measurement_and_capture_time(self):
        path = export_xlsx(self.partners, self.posts, self.snapshots, self.root / "times.xlsx")
        book = load_workbook(path)
        self.addCleanup(book.close)
        history = book["История просмотров"]
        self.assertEqual(history["D2"].value, datetime(2026, 11, 2, 1, 0))
        self.assertEqual(history["J2"].value, datetime(2026, 11, 2, 0, 0))
        self.assertEqual(history["K2"].value, datetime(2026, 11, 2, 1, 10))
        self.assertIsNone(history["K3"].value)  # No invented successful capture time.

    def test_google_cell_limits_include_surrogate_pairs(self):
        self.assertEqual(len(_google_value("я" * 60000)), 50000)
        self.assertEqual(len(_google_value("😀" * 40000)), 25000)
        self.assertEqual(_google_value("=" + "a" * 50000), "=" + "a" * 49999)
        self.assertEqual(_google_value(None), "")
        self.assertEqual(_google_value(123), 123)

    def test_invalid_month(self):
        for month in ["2026-13", "2026-1", "../2026-11"]:
            with self.assertRaises(ValueError):
                export_xlsx([], [], [], self.root / "invalid.xlsx", month=month)

    def reporter(self, folder=None):
        reporter = GoogleReporter.__new__(GoogleReporter)
        reporter.spreadsheet_id = "fake-sheet"
        reporter.drive_folder_id = folder
        reporter.timezone = "Asia/Tashkent"
        return reporter

    def test_google_only_dedicated_tabs_and_raw_values(self):
        reporter = self.reporter()
        calls = []

        def request(method, url, **kwargs):
            calls.append((method, url, kwargs))
            if method == "GET":
                return {"sheets": [{"properties": {"title": "Лист1", "sheetId": 0, "gridProperties": {"rowCount": 1000, "columnCount": 10}}}]}
            return {}

        reporter._json = request
        self.assertEqual(reporter.sync(self.partners, self.posts, self.snapshots), {})
        creates = next(c for c in calls if c[1].endswith(":batchUpdate") and "/values" not in c[1])
        self.assertEqual([r["addSheet"]["properties"]["title"] for r in creates[2]["json"]["requests"]], list(TABS))
        write = next(c for c in calls if c[1].endswith("/values:batchUpdate"))
        self.assertEqual(write[2]["json"]["valueInputOption"], "RAW")
        self.assertEqual(write[2]["json"]["data"][0]["values"][1][0], "=BAD()")
        self.assertEqual(write[2]["json"]["data"][0]["values"][1][5], "")
        self.assertFalse(any("Лист1" in str(c[2]) for c in calls))

    def test_google_upload_reuses_drive_file_and_does_not_share(self):
        reporter = self.reporter("folder")
        reporter._json = Mock(return_value={"files": [{"id": "existing"}]})
        url = reporter._upload(self.snapshots[0])
        self.assertEqual(url, "https://drive.google.com/file/d/existing/view")
        self.assertEqual(reporter._json.call_count, 1)
        params = reporter._json.call_args.kwargs["params"]
        self.assertIn("appProperties", params["q"])
        self.assertIn("'folder' in parents", params["q"])

    def test_google_upload_is_multipart_related(self):
        reporter = self.reporter("folder")
        reporter._json = Mock(side_effect=[{"files": []}, {"id": "new"}])
        url = reporter._upload(self.snapshots[0])
        self.assertEqual(url, "https://drive.google.com/file/d/new/view")
        upload = reporter._json.call_args
        self.assertIn("multipart/related", upload.kwargs["headers"]["Content-Type"])
        self.assertIn(self.png.read_bytes(), upload.kwargs["data"])
        self.assertNotIn(b"permissions", upload.kwargs["data"])

    def test_google_tail_cleanup_follows_successful_write(self):
        reporter = self.reporter()
        requests = []

        def request(method, url, **kwargs):
            requests.append((method, url, kwargs))
            if method == "GET":
                return {"sheets": [{"properties": {"title": title, "sheetId": index + 1, "gridProperties": {"rowCount": 1000, "columnCount": 10}}} for index, title in enumerate(TABS)]}
            return {}

        reporter._json = request
        reporter.sync([], [], [])
        self.assertTrue(requests[-2][1].endswith("/values:batchUpdate"))
        self.assertTrue(requests[-1][1].endswith("/values:batchClear"))
        self.assertEqual(requests[-1][2]["json"]["ranges"][0], "'BRB_Публикации'!A2:N1000")


if __name__ == "__main__":
    unittest.main()
