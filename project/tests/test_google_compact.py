from io import BytesIO
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from openpyxl import load_workbook

from partner_monitor.google_compact import REPORT_SHEETS, MARKER, VALUE, SHEETS, replace_workbook, sync_compact, verify_target
from partner_monitor.reports import GoogleReporter
import test_unified_report


class CompactGoogleTests(unittest.TestCase):
    def setUp(self):
        self.data = test_unified_report.UnifiedReportTests()
        self.data.setUp()
        self.addCleanup(self.data.doCleanups)
        self.reporter = GoogleReporter.__new__(GoogleReporter)
        self.reporter.spreadsheet_id = "dedicated-test"
        self.reporter.drive_folder_id = None
        self.reporter.timezone = "Asia/Tashkent"
        self.reporter.layout = "compact"
        self.calls = []

    def metadata(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if "drive/v3/files" in url:
            return {"mimeType": SHEETS, "appProperties": {MARKER: VALUE}}
        return {"sheets": [{"properties": {"title": n, "sheetId": i}} for i, n in enumerate(REPORT_SHEETS)]}

    def test_compact_report_has_two_visible_tabs_and_real_embedded_evidence(self):
        self.reporter._json = self.metadata
        self.reporter._upload = Mock(return_value=None)
        captured = []
        def upload(reporter, path):
            captured.append(Path(path).read_bytes())
        with patch("partner_monitor.google_compact.replace_workbook", side_effect=upload):
            self.reporter.sync(self.data.partners, self.data.posts, self.data.snapshots)
        book = load_workbook(BytesIO(captured[0]))
        self.addCleanup(book.close)
        self.assertEqual(book.sheetnames, list(REPORT_SHEETS))
        self.assertEqual([s.title for s in book if s.sheet_state == "visible"], ["Отчёт", "Недоступные посты"])
        self.assertEqual(len(book["Отчёт"]._images), 1)
        self.assertEqual(book["Отчёт"]["E8"].value, "План")
        self.assertIsNone(book["Отчёт"]["A5"].value)
        self.assertFalse(any("BRB_" in str(c) for c in self.calls))

    def test_unmarked_or_unrelated_workbook_is_never_overwritten(self):
        for extra, marker in [(False, False), (True, True)]:
            def metadata(method, url, **kwargs):
                if "drive/v3/files" in url:
                    return {"mimeType": SHEETS, "appProperties": {MARKER: VALUE} if marker else {}}
                names = list(REPORT_SHEETS) + (["User notes"] if extra else [])
                return {"sheets": [{"properties": {"title": n}} for n in names]}
            self.reporter._json = metadata
            self.reporter._upload = Mock()
            with self.subTest(extra=extra, marker=marker), patch("partner_monitor.google_compact.replace_workbook") as upload:
                with self.assertRaises(ValueError):
                    self.reporter.sync([], [], [])
                upload.assert_not_called()
                self.reporter._upload.assert_not_called()

    def test_native_template_marker_allows_initial_conversion(self):
        def metadata(method, url, **kwargs):
            if "drive/v3/files" in url:
                return {"mimeType": SHEETS}
            return {"sheets": [{"properties": {"title": n}} for n in REPORT_SHEETS],
                    "developerMetadata": [{"metadataKey": MARKER, "metadataValue": VALUE}]}
        self.reporter._json = metadata
        verify_target(self.reporter)

    def test_contract_plan_is_not_repeated_per_creative_and_other_clients_stay_visible(self):
        from partner_monitor.report_view import build_view
        self.data.partners[0]["contract_plans"]=[{"contract_key":"contract-a","client_name":"Client A", "title":"Channel",
            "language":"UZ","planned_posts":10,"baseline_count":3,"baseline_urls":"[]","baseline_until":"2026-10-01T00:00:00+00:00"}]
        self.data.partners[0]["placements"][0]["contract_key"]="contract-a"
        groups=build_view(self.data.partners,self.data.posts,self.data.snapshots)["summary"]
        self.assertEqual([(g["client"],g["plan"],g["total"],g["remaining"]) for g in groups],[("Client A",10,4,6),("Client B",5,1,4)])

    def test_private_drive_evidence_can_be_restored_when_local_file_is_missing(self):
        self.reporter._json = self.metadata
        self.reporter._upload = Mock(return_value=None)
        picture = self.data.old.read_bytes()
        self.data.old.unlink()
        self.data.snapshots[0]["screenshot_url"] = "https://drive.google.com/file/d/private-evidence/view"
        response = Mock(content=picture)
        self.reporter.session = Mock(request=Mock(return_value=response))
        copies = []
        with patch("partner_monitor.google_compact.replace_workbook", side_effect=lambda r,p: copies.append(Path(p).read_bytes())):
            self.reporter.sync(self.data.partners, self.data.posts, self.data.snapshots)
        book = load_workbook(BytesIO(copies[0])); self.addCleanup(book.close)
        self.assertEqual(len(book["Отчёт"]._images), 1)
        self.assertEqual(self.reporter.session.request.call_args.args[0], "GET")
        self.assertNotIn("permissions", str(self.calls))

    def test_resumable_update_keeps_same_file_id_and_does_not_publish_images(self):
        init = Mock(headers={"Location": "https://www.googleapis.com/upload/drive/v3/files/dedicated-test?upload_id=private"})
        uploaded = Mock(); uploaded.json.return_value = {"id": "dedicated-test"}
        self.reporter.session = Mock(request=Mock(side_effect=[init, uploaded]))
        file = self.data.root / "report.xlsx";file.write_bytes(b"xlsx fixture")
        replace_workbook(self.reporter, file)
        first,second = self.reporter.session.request.call_args_list
        self.assertEqual(first.args[0], "PATCH")
        self.assertEqual(first.kwargs["json"]["mimeType"], SHEETS)
        self.assertEqual(second.args[0], "PUT")
        self.assertEqual(second.kwargs["data"], b"xlsx fixture")
        self.assertNotIn("permissions", str(self.reporter.session.request.call_args_list))

    def test_upload_location_cannot_leak_google_authorization_to_another_host(self):
        response = Mock(headers={"Location": "https://unrelated.example/upload"})
        self.reporter.session = Mock(request=Mock(return_value=response))
        file = self.data.root / "report.xlsx";file.write_bytes(b"xlsx")
        with self.assertRaises(ValueError):
            replace_workbook(self.reporter,file)
        self.assertEqual(self.reporter.session.request.call_count,1)
