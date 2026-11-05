from pathlib import Path

root = Path(__file__).resolve().parent / "partner-monitor"
path = root / "partner_monitor" / "reports.py"
text = path.read_text(encoding="utf-8")
start = text.index("def export_xlsx(")
end = text.index("\n\nclass GoogleReporter:", start)
replacement = '''def export_xlsx(partners: list[dict], posts: list[dict], snapshots: list[dict], output_path: Path,
                timezone: str = "Asia/Tashkent", month: str | None = None) -> Path:
    """One workbook with client/channel totals, post cards and embedded evidence."""
    from .unified_report import export_unified
    return export_unified(partners, posts, snapshots, output_path, timezone, month)
'''
text = text[:start] + replacement + text[end:]
text = text.replace("from openpyxl import Workbook\n", "").replace("from openpyxl.drawing.image import Image\n", "")
path.write_text(text, encoding="utf-8")
