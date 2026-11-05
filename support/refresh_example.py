"""Rebuild the illustrative workbook on any machine without Telegram access."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "project"))

from partner_monitor.unified_report import export_unified


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path,
                        default=ROOT / "deliverables" / "unified-report-example.xlsx")
    args = parser.parse_args()
    sample = json.loads((ROOT / "support" / "sample-input.json").read_text(encoding="utf-8"))
    for snapshot in sample["snapshots"]:
        # The original export contains Windows paths; only use bundled assets.
        name = snapshot["screenshot_path"].replace("\\", "/").rsplit("/", 1)[-1]
        image = ROOT / "support" / "reference-fragments" / name
        if not image.is_file():
            raise FileNotFoundError(image)
        snapshot["screenshot_path"] = str(image)
    output = export_unified(sample["partners"], sample["posts"], sample["snapshots"],
                            args.output, note=sample["note"])
    print(output)


if __name__ == "__main__":
    main()
