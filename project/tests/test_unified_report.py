from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from openpyxl import load_workbook
from PIL import Image

from partner_monitor.config import Config
from partner_monitor.parsing import parse_partner_references
from partner_monitor.report_view import build_view
from partner_monitor.reports import export_xlsx
from partner_monitor.storage import Store


class UnifiedReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.old = self.root / "first.png"
        self.new = self.root / "second.png"
        Image.new("RGB", (300, 900), "white").save(self.old)
        Image.new("RGB", (300, 900), "gray").save(self.new)
        self.partners = [{"id": 1, "title": "Channel", "username": "partner_one", "status": "active", "placements": [self.brief(1, "Client A", 10), self.brief(2, "Client B", 5)]}]
        self.posts = [{"id": 1, "partner_id": 1, "message_id": 10, "url": "https://t.me/partner_one/10", "published_at": "2026-10-31T20:00:00+00:00", "first_seen_at": "2026-10-31T20:01:00+00:00", "text": "A", "match_reason": "keyword:A", "status": "active", "placement_ids": [1]},
                      {"id": 2, "partner_id": 1, "message_id": 11, "url": "https://t.me/partner_one/11", "published_at": "2026-10-05T12:00:00+00:00", "first_seen_at": "2026-10-05T12:01:00+00:00", "text": "B", "match_reason": "keyword:B", "status": "unavailable", "unavailable_since": "2026-10-06T12:00:00+00:00", "placement_ids": [2]}]
        self.snapshots = [{"id": 1, "post_id": 1, "checked_at": "2026-11-01T10:00:00+00:00", "due_at": "2026-11-01T10:00:00+00:00", "due_key": "initial", "views": 11, "screenshot_status": "ok", "screenshot_path": str(self.old), "screenshot_at": "2026-11-01T10:01:00+00:00"},
                          {"id": 2, "post_id": 1, "checked_at": "2026-11-02T10:00:00+00:00", "due_at": "2026-11-02T10:00:00+00:00", "due_key": "24h", "views": 99, "screenshot_status": "failed", "screenshot_path": None, "screenshot_error": "network"}]

    def brief(self, ident, client, plan):
        return {"id": ident, "client_name": client, "language": "UZ", "planned_posts": plan, "name": "Ad", "ad_type": "Post", "terms": "24h", "retention_hours": 24, "starts_at": "2026-10-01T00:00:00+00:00", "ends_at": "2026-12-01T00:00:00+00:00", "status": "found", "source_chat_id": "-1001234567890", "source_message_id": ident, "source_text": "Source", "default_window": 0, "post_urls": []}

    def test_two_clients_in_one_channel_have_separate_totals(self):
        view = build_view(self.partners, self.posts, self.snapshots)
        self.assertEqual([(r["client"], r["plan"], r["total"], r["remaining"]) for r in view["summary"]], [("Client A", 10, 1, 9), ("Client B", 5, 1, 4)])

    def test_private_card_is_labelled_separately_from_a_telegram_screenshot(self):
        self.snapshots[0]["screenshot_error"] = "Карточка закрытого поста из данных Telegram; не снимок интерфейса"
        record = next(r for r in build_view(self.partners, self.posts, self.snapshots)["records"] if r["id"] == 1)
        self.assertEqual(record["picture_kind"], "card")
        self.assertIn("Карточка закрытого поста", record["picture_note"])

    def test_latest_failed_screenshot_preserves_last_successful_evidence(self):
        record = next(r for r in build_view(self.partners, self.posts, self.snapshots)["records"] if r["id"] == 1)
        self.assertEqual(record["picture_path"], str(self.old))
        self.assertEqual(record["views"], 99)
        self.assertEqual(record["picture_at"], "2026-11-01T15:01:00")
        self.assertIn("последний успешный", record["picture_note"])

    def test_new_successful_screenshot_replaces_visible_picture_only(self):
        self.snapshots[-1].update(screenshot_status="ok", screenshot_path=str(self.new), screenshot_at="2026-11-02T10:01:00+00:00")
        view = build_view(self.partners, self.posts, self.snapshots)
        record = next(r for r in view["records"] if r["id"] == 1)
        self.assertEqual(record["picture_path"], str(self.new))
        self.assertEqual(len(self.snapshots), 2)
        book = load_workbook(export_xlsx(self.partners, self.posts, self.snapshots, self.root / "out.xlsx"))
        try:
            self.assertEqual(len(book["Отчёт"]._images), 1)
            self.assertEqual(book["История просмотров"].max_row, 3)
        finally:
            book.close()

    def test_missing_latest_picture_file_falls_back_to_existing_picture(self):
        self.snapshots[-1].update(screenshot_status="ok", screenshot_path=str(self.root / "missing.png"))
        record = next(r for r in build_view(self.partners, self.posts, self.snapshots)["records"] if r["id"] == 1)
        self.assertEqual(record["picture_path"], str(self.old))

    def test_missing_plan_is_not_presented_as_zero(self):
        self.partners[0]["placements"][0]["planned_posts"] = None
        row = build_view(self.partners, self.posts, self.snapshots)["summary"][0]
        self.assertIsNone(row["plan"])
        self.assertIsNone(row["remaining"])

    def test_month_filter_uses_local_publication_date_but_keeps_contract_balance(self):
        view = build_view(self.partners, self.posts, self.snapshots, month="2026-11")
        self.assertEqual([r["id"] for r in view["records"]], [1])
        other = view["summary"][1]
        self.assertEqual((other["total"], other["visible_count"], other["remaining"]), (1, 0, 4))

    def test_one_unavailable_post_is_not_claimed_to_be_deleted(self):
        record = build_view(self.partners, self.posts, self.snapshots)["unavailable"][0]
        self.assertEqual(record["state"], "Недоступен")
        self.assertNotIn("Удал", record["state"])

    def test_all_images_and_navigation_stay_inside_the_workbook(self):
        book = load_workbook(export_xlsx(self.partners, self.posts, self.snapshots, self.root / "out.xlsx"))
        try:
            self.assertEqual(len(book["Отчёт"]._images), 1)
            link = book["Реестр публикаций"]["I2"].hyperlink
            self.assertTrue(link.location.startswith("'Отчёт'!A"))
            self.assertIsNone(link.target)
            for sheet in book:
                for row in sheet:
                    for cell in row:
                        self.assertFalse(isinstance(cell.value, str) and str(self.root) in cell.value)
            self.assertIsInstance(book["Реестр публикаций"]["E2"].value, datetime)
            self.assertEqual(book["Недоступные посты"].max_row, 2)
        finally:
            book.close()

    def test_compact_previews_preserve_full_images_without_enlarging_them(self):
        from io import BytesIO
        from zipfile import ZipFile
        from openpyxl.utils.units import EMU_to_pixels

        self.posts[0]["published_at"] = "2026-10-01T12:00:00+00:00"
        for size in [(92, 201), (720, 3600), (1400, 200)]:
            with self.subTest(size=size):
                Image.new("RGB", size, "white").save(self.old)
                path = export_xlsx(self.partners, self.posts, self.snapshots, self.root / "out.xlsx")
                with ZipFile(path) as archive:
                    media = [name for name in archive.namelist() if name.startswith("xl/media/")]
                    self.assertEqual(len(media), 1)
                    with Image.open(BytesIO(archive.read(media[0]))) as original:
                        self.assertEqual(original.size, size)
                book = load_workbook(path)
                try:
                    sheet = book["Отчёт"]
                    self.assertIsNone(sheet.freeze_panes)
                    picture = sheet._images[0]
                    width = EMU_to_pixels(picture.anchor.ext.cx)
                    height = EMU_to_pixels(picture.anchor.ext.cy)
                    self.assertLessEqual(width, min(240, size[0]))
                    self.assertLessEqual(height, min(280, size[1]))
                    self.assertLessEqual(abs(width * size[1] - height * size[0]),
                                         (size[0] + size[1]) / 2)
                    if size == (92, 201):
                        self.assertEqual((width, height), size)
                    link = book["Реестр публикаций"]["I3"].hyperlink.location
                    next_card = sheet[link.rsplit("!", 1)[1]].row
                    available = sum((sheet.row_dimensions[r].height or sheet.sheet_format.defaultRowHeight) * 4 / 3
                                    for r in range(picture.anchor._from.row + 1, next_card))
                    self.assertLess(height + 8, available)
                finally:
                    book.close()

    def test_publication_table_groups_instrument_and_platform_with_matching_images(self):
        self.partners.append({"id": 2, "title": "Another platform", "username": "partner_two",
                              "status": "active", "placements": [self.brief(3, "Client A", 3)]})
        self.posts.extend([
            dict(self.posts[0], id=3, partner_id=2, message_id=30, placement_ids=[3],
                 url="https://t.me/partner_two/30", published_at="2026-10-20T10:00:00+00:00"),
            dict(self.posts[0], id=4, message_id=40, url="https://t.me/partner_one/40",
                 published_at="2026-10-15T10:00:00+00:00"),
        ])
        self.snapshots.append(dict(self.snapshots[0], id=3, post_id=3, screenshot_path=str(self.new)))
        book = load_workbook(export_xlsx(self.partners, self.posts, self.snapshots, self.root / "out.xlsx"))
        try:
            sheet = book["Отчёт"]
            self.assertEqual((sheet["B8"].value, sheet["C8"].value), ("Инструмент", "Площадки"))
            rows = {}
            for row, post in enumerate(self.posts, 2):
                link = book["Реестр публикаций"].cell(row, 9).hyperlink
                address = link.location.rsplit("!", 1)[1]
                data_row = sheet[address].row
                rows[post["id"]] = data_row
                self.assertTrue(sheet.cell(data_row, 5).value.startswith(f"Пост #{post['id']}\n"))
                self.assertEqual(sheet.cell(data_row, 5).hyperlink.target, post["url"])
                self.assertEqual(sheet.cell(data_row, 1).hyperlink.location, f"'Реестр публикаций'!A{row}")
            self.assertEqual(sorted(rows, key=rows.get), [3, 4, 1, 2])
            self.assertEqual(sorted(rows.values()), list(range(min(rows.values()), min(rows.values()) + 4)))
            self.assertEqual(sheet.cell(rows[1], 1).value, "Client A")
            self.assertEqual(sheet.cell(rows[3], 3).value, "Another platform")
            self.assertIn("Просмотры: 99", sheet.cell(rows[1], 7).value)
            self.assertIn("последний успешный", sheet.cell(rows[1], 7).comment.text)
            image_rows = {picture.anchor._from.row + 1 for picture in sheet._images}
            self.assertEqual(image_rows, {rows[1], rows[3]})
            self.assertEqual(sheet.cell(rows[2], 10).value, "Скриншот ещё не получен")
            for tab in ("Реестр публикаций", "Недоступные посты", "Партнёры"):
                self.assertEqual(book[tab]["A1"].value, "Инструмент")
            for tab in ("Реестр публикаций", "История просмотров", "Партнёры"):
                self.assertEqual(book[tab].cell(1, 2 if tab != "История просмотров" else 1).value, "Площадки")
        finally:
            book.close()

    def test_optional_client_plan_and_language_persist_across_restart(self):
        path = self.root / "db.sqlite3"
        config = Config(root=self.root, source_group="group", keywords=[], domains=[])
        text = "Клиент: Acme\nНазвание: Test\nЯзык: UZ\nПлан публикаций: 10\n@partner_one\nДлительность: 24 часа"
        with Store(path) as store:
            store.register_brief(parse_partner_references(text), text, 100, 1, datetime.now(timezone.utc), config)
        with Store(path) as store:
            row = store.rows("SELECT client_name,language,planned_posts FROM placements")[0]
            self.assertEqual(row, {"client_name": "Acme", "language": "UZ", "planned_posts": 10})

    def test_upgrade_preserves_existing_placements_and_adds_optional_fields(self):
        path = self.root / "legacy.sqlite3"
        config = Config(root=self.root, source_group="group", keywords=["Test"], domains=[])
        with Store(path) as store:
            store.register_brief(parse_partner_references("@partner_one"), "@partner_one", 100, 1, datetime.now(timezone.utc), config)
            for name in ("client_name", "language", "planned_posts"):
                store.execute(f"ALTER TABLE placements DROP COLUMN {name}")
            for name in ("last_checked_at", "unavailable_since"):
                store.execute(f"ALTER TABLE posts DROP COLUMN {name}")
        with Store(path) as store:
            row = store.rows("SELECT * FROM placements")[0]
            self.assertEqual(row["id"], 1)
            self.assertEqual(row["client_name"], "")
            self.assertIsNone(row["planned_posts"])
            self.assertIn("unavailable_since", {r[1] for r in store.db.execute("PRAGMA table_info(posts)")})
