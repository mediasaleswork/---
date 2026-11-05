from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
from pathlib import Path
import tempfile
import unittest

from partner_monitor.cli import main
from partner_monitor.storage import Store


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / "config.yaml"

    def command(self, *args):
        out = StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            code = main(["--config", str(self.config), *args])
        self.assertEqual(code, 0, out.getvalue())
        return out.getvalue()

    def test_preview_does_not_create_database_or_need_telegram(self):
        self.command("init")
        result = self.command("preview", "--text", "Название: Test\nКанал: @partner_one\nДлительность: 24 часа")
        self.assertIn('"retention_hours": 24.0', result)
        self.assertFalse((self.root / "data").exists())

    def test_review_resolve_export_workflow_without_accounts(self):
        self.command("init")
        self.command("add", "@partner_one @partner_two")
        self.assertIn("несколько каналов", self.command("review"))
        self.command("resolve", "1", "--start", "2026-11-06 10:00", "--end", "2026-11-07 10:00", "--keyword", "Product", "--hours", "24")
        self.command("dismiss", "2")
        self.command("export", "--output", str(self.root / "report.xlsx"))
        self.assertTrue((self.root / "report.xlsx").exists())
        with Store(self.root / "data" / "monitor.sqlite3") as store:
            self.assertEqual([r["status"] for r in store.rows("SELECT status FROM placements ORDER BY id")], ["watching", "dismissed"])
