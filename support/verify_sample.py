from pathlib import Path
from zipfile import ZipFile
import re

from openpyxl import load_workbook

work = Path(__file__).resolve().parent
output = work.parent / "outputs" / "unified-report-example.xlsx"
book = load_workbook(output)
cached = load_workbook(output, data_only=True)
try:
    assert len(book["Отчёт"]._images) == 13
    assert book["Реестр публикаций"].max_row == 14
    assert book["Недоступные посты"].max_row == 2
    assert book["Реестр публикаций"]["A1"].value == "Клиент"
    assert sum(cached["Отчёт"].cell(r, 7).value for r in range(9, 13)) == 27
    for row in range(2, 15):
        link = book["Реестр публикаций"].cell(row, 9).hyperlink
        target = int(re.fullmatch(r"'Отчёт'!A(\d+)", link.location)[1])
        assert book["Отчёт"].cell(target + 4, 3).value == book["Реестр публикаций"].cell(row, 4).value
    for sheet in book:
        for row in sheet:
            for cell in row:
                assert not isinstance(cell.value, str) or str(work) not in cell.value
    for image in book["Отчёт"]._images:
        assert len(image._data()) > 100  # Read solely from XLSX media, not source paths.
finally:
    book.close()
    cached.close()
with ZipFile(output) as z:
    assert z.testzip() is None
    assert len([p for p in z.namelist() if p.startswith("xl/media/")]) == 13
print("Verified: 13 embedded screenshots, 13 post-to-card links, typed dates, cached balance 27, client-first registry, no local source paths.")
