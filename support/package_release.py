from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
import hashlib
import tomllib

workspace = Path(__file__).resolve().parent.parent
source = workspace / "work" / "partner-monitor"
output = workspace / "outputs" / "partner-monitor-unified.zip"
files = [source / name for name in (
    ".env.example", ".gitignore", "README.md", "VERIFICATION.txt", "config.example.yaml",
    "demo-report.xlsx", "pyproject.toml", "requirements.lock.txt")]
for folder in ("deploy", "examples", "partner_monitor", "tests"):
    files.extend(p for p in (source / folder).rglob("*") if p.is_file()
                 and "__pycache__" not in p.parts and p.suffix != ".pyc")
output.parent.mkdir(parents=True, exist_ok=True)
with ZipFile(output, "w", ZIP_DEFLATED) as archive:
    for path in sorted(files):
        assert path.is_file(), path
        assert path.suffix not in {".session", ".sqlite3"}
        archive.write(path, "partner-monitor/" + path.relative_to(source).as_posix())
with ZipFile(output) as archive:
    assert archive.testzip() is None
    names = archive.namelist()
    required = {"partner-monitor/partner_monitor/realtime.py", "partner-monitor/partner_monitor/telegram_access.py", "partner-monitor/tests/test_realtime.py"}
    assert required.issubset(names)
    assert tomllib.loads(archive.read("partner-monitor/pyproject.toml").decode())["project"]["version"] == "0.4.0"
    assert "partner-monitor/partner_monitor/unified_report.py" in names
    assert "partner-monitor/partner_monitor/report_view.py" in names
    assert not any(".venv" in n or "secrets/" in n or n.endswith("/.env") for n in names)
    for path in files:
        assert archive.read("partner-monitor/" + path.relative_to(source).as_posix()) == path.read_bytes()
print(f"Release: {output}\nFiles: {len(files)}\nBytes: {output.stat().st_size}\nSHA256: {hashlib.sha256(output.read_bytes()).hexdigest()}")
