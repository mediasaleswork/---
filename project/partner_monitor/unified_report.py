"""Server-side Excel presentation: totals and embedded post evidence in one file."""
from __future__ import annotations

from datetime import datetime
import math
from pathlib import Path

from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.drawing.image import Image
from openpyxl.drawing.spreadsheet_drawing import AnchorMarker, OneCellAnchor
from openpyxl.drawing.xdr import XDRPositiveSize2D
from openpyxl.utils.units import pixels_to_EMU
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.hyperlink import Hyperlink
from openpyxl.worksheet.table import Table, TableStyleInfo
from openpyxl.formatting.rule import CellIsRule
from openpyxl.workbook.properties import CalcProperties

from .report_view import build_view

RED = "EE343D"
INK = "26272B"
GRAY = "F1F2F4"
PINK = "FBE0E1"
DATE_FORMAT = "yyyy-mm-dd hh:mm"


def export_unified(partners, posts, snapshots, output_path, timezone="Asia/Tashkent", month=None, note=""):
    from .reports import _cell, _tables, _write_table, _ordered
    view = build_view(partners, posts, snapshots, timezone, month)
    selected = {p["id"] for p in view["records"]}
    filtered_posts = [p for p in posts if p["id"] in selected]
    filtered_snapshots = [s for s in snapshots if s["post_id"] in selected]
    registry, history, briefs, _ = _tables(partners, filtered_posts, filtered_snapshots, timezone)
    book = Workbook()
    book.calculation = CalcProperties(calcMode="auto", fullCalcOnLoad=True, forceFullCalc=True)
    report = book.active
    report.title = "Отчёт"
    unavailable = book.create_sheet("Недоступные посты")
    detail = book.create_sheet("Реестр публикаций")
    history_sheet = book.create_sheet("История просмотров")
    briefs_sheet = book.create_sheet("Партнёры")
    for sheet in book:
        sheet.sheet_view.showGridLines = False
        sheet.sheet_view.zoomScale = 85
        sheet.sheet_properties.pageSetUpPr.fitToPage = True
        sheet.page_setup.orientation = "landscape"
        sheet.page_setup.paperSize = sheet.PAPERSIZE_A3
        sheet.page_setup.fitToWidth = 1
        sheet.page_setup.fitToHeight = 0
    report.sheet_properties.tabColor = RED
    unavailable.sheet_properties.tabColor = "D69B36"
    detail.sheet_properties.tabColor = INK

    def put(sheet, row, col, value, bold=False):
        if isinstance(value, str) and value.startswith("@"):
            value = str(value)
        cell = _cell(sheet, row, col, value)
        cell.font = Font(name="Arial", size=10, bold=bold, color=INK)
        cell.alignment = Alignment(vertical="center", wrap_text=True)
        if isinstance(value, (int, float)):
            cell.number_format = "#,##0"
            cell.alignment = Alignment(horizontal="right", vertical="center")
        return cell

    def date_cell(sheet, row, col, value):
        cell = put(sheet, row, col, datetime.fromisoformat(value) if value else None)
        cell.number_format = DATE_FORMAT
        cell.alignment = Alignment(horizontal="center", vertical="center")
        return cell

    def jump(cell, sheet_name, address, label):
        cell.value = label
        cell.data_type = "s"
        cell.hyperlink = Hyperlink(ref=cell.coordinate, location=f"'{sheet_name}'!{address}", display=label)
        cell.font = Font(name="Arial", size=10, color="1567B0", underline="single")

    def band(sheet, row, title):
        sheet.merge_cells(start_row=row, start_column=1, end_row=row, end_column=3)
        cell = put(sheet, row, 1, title, True)
        cell.fill = PatternFill("solid", fgColor=GRAY)
        sheet.row_dimensions[row].height = 23

    report.merge_cells("A2:C2")
    put(report, 2, 1, "MEDIASALES", True).font = Font(name="Arial", size=16, bold=True, color=RED)
    report.merge_cells("D2:L2")
    put(report, 2, 4, "Размещения рекламы", True).font = Font(name="Arial", size=16, bold=True, color=INK)
    report.merge_cells("A4:L4")
    put(report, 4, 1, f"Период публикаций: {month or 'все даты'}. Время: {timezone}. Заказано, опубликовано и осталось — за весь срок заявки.")
    report.merge_cells("A5:L5")
    put(report, 5, 1, note or "Пример: заказано 10 постов, опубликовано 4, осталось 6. Пустое количество — заказ не указан полностью. Не удалось открыть пост — не значит, что он удалён.")
    report.row_dimensions[5].height = 30
    band(report, 7, "ТГ КАНАЛЫ")
    headers = ["№", "Клиент", "Канал", "Язык", "Заказано постов", "Опубликовано", "Осталось опубликовать", "Постов за период", "Не удалось открыть", "Постов со скриншотом", "Последняя публикация", "Публикации"]
    explanations = {
        5: "Сколько публикаций заказано по всем заявкам этого клиента в этом канале. Пусто, если заказанное количество указано не полностью.",
        6: "Сколько уникальных постов найдено за весь срок заявок, включая посты, которые позднее стали недоступны.",
        7: "Заказано минус опубликовано за весь срок заявок. Например: 10 − 4 = 6. Пусто, если заказанное количество неизвестно.",
        8: "Сколько постов включено в выбранный месяц отчёта. Если выбраны все даты, совпадает с общим числом опубликованных постов.",
        9: "Сколько постов за выбранный период сейчас недоступны для проверки. Потеря доступа или ошибка API не доказывает удаление.",
        10: "Сколько постов за выбранный период имеют сохранённый скриншот. Несколько снимков одного поста считаются один раз.",
    }
    for col, value in enumerate(headers, 1):
        cell = put(report, 8, col, value, True)
        cell.fill = PatternFill("solid", fgColor=GRAY)
        if col in explanations:
            cell.comment = Comment(explanations[col], "Partner Monitor")
    report.row_dimensions[8].height = 44
    summary_links = []
    for index, item in enumerate(view["summary"], 1):
        row = index + 8
        values = [index, item["client"], item["channel"], item["languages"], item["plan"], item["total"], None,
                  item["visible_count"], item["unavailable_count"], item["picture_count"]]
        for col, value in enumerate(values, 1):
            put(report, row, col, value)
        put(report, row, 7, f'=IF(E{row}="","",MAX(0,E{row}-F{row}))').data_type = "f"
        if item["channel_url"]:
            report.cell(row, 3).hyperlink = item["channel_url"]
            report.cell(row, 3).font = Font(name="Arial", size=10, color="1567B0", underline="single")
        date_cell(report, row, 11, item["last_published"])
        put(report, row, 1, index).fill = PatternFill("solid", fgColor=PINK)
        report.row_dimensions[row].height = 40
        if item["first_post_id"]:
            summary_links.append((row, item["first_post_id"]))
    summary_end = max(8, 8 + len(view["summary"]))
    report.auto_filter.ref = f"A8:L{summary_end}"
    if summary_end > 8:
        report.conditional_formatting.add(f"I9:I{summary_end}", CellIsRule(operator="greaterThan", formula=["0"], fill=PatternFill("solid", fgColor=PINK)))
    # A split at D9 leaves a cross through post cards in spreadsheet viewers.
    report.freeze_panes = None
    for col, width in {"A": 6, "B": 21, "C": 25, "D": 9, "E": 11, "F": 12, "G": 11, "H": 12, "I": 12, "J": 10, "K": 23, "L": 19}.items():
        report.column_dimensions[col].width = width
    row = summary_end + 3
    band(report, row, "ПУБЛИКАЦИИ И СКРИНШОТЫ")
    row += 2
    anchors = {}
    record_by_id = {r["id"]: r for r in view["records"]}
    registry_rows = {p["id"]: i + 2 for i, p in enumerate(filtered_posts)}
    for item in view["records"]:
        anchors[item["id"]] = row
        report.merge_cells(start_row=row, start_column=1, end_row=row, end_column=12)
        heading = put(report, row, 1, f"Пост #{item['id']}    {item['client']}    {item['channel']}", True)
        heading.fill = PatternFill("solid", fgColor=GRAY)
        report.row_dimensions[row].height = 28
        picture = None
        if item["picture_path"]:
            try:
                picture = Image(item["picture_path"])
                # Fit the complete post into a preview; never enlarge small images.
                scale = min(1, 240 / picture.width, 280 / picture.height)
                picture.width = round(picture.width * scale)
                picture.height = round(picture.height * scale)
            except (OSError, ValueError):
                item["picture_note"] = "Файл последнего скриншота не удалось прочитать"
        fields = [("Реклама", item["ad"]), ("Ссылка на пост", item["url"]),
                  ("Опубликован", item["published"]), ("Состояние", item["state"]),
                  ("Просмотры", item["views"] if item["views"] is not None else "Нет данных"),
                  ("Размещение, часов", "; ".join(f"{h:g}" for h in item["durations"]) or "Не указано"),
                  ("Плановое снятие", item["until"]), ("Снимок сделан", item["picture_at"]),
                  ("Примечание", item["picture_note"] or item["error"])]
        height = picture.height if picture else 0
        block_rows = max(len(fields) + 1, math.ceil((height + 16) / 32))
        for r in range(row + 1, row + block_rows + 1):
            report.row_dimensions[r].height = 24
        for offset, (label, value) in enumerate(fields, 1):
            report.merge_cells(start_row=row + offset, start_column=1, end_row=row + offset, end_column=2)
            put(report, row + offset, 1, label, True)
            report.merge_cells(start_row=row + offset, start_column=3, end_row=row + offset, end_column=6)
            cell = put(report, row + offset, 3, value)
            if label in {"Опубликован", "Плановое снятие", "Снимок сделан"}:
                date_cell(report, row + offset, 3, value)
            if label == "Ссылка на пост" and value:
                cell.hyperlink = value
                cell.font = Font(name="Arial", size=10, color="1567B0", underline="single")
            if label == "Примечание" and value:
                report.row_dimensions[row + offset].height = max(24, min(100, math.ceil(len(value) / 60) * 14))
        jump(put(report, row + len(fields) + 1, 3, ""), "Реестр публикаций", f"A{registry_rows[item['id']]}", "Вернуться в реестр")
        if picture:
            picture.anchor = OneCellAnchor(
                _from=AnchorMarker(col=6, row=row, colOff=pixels_to_EMU(12), rowOff=pixels_to_EMU(6)),
                ext=XDRPositiveSize2D(pixels_to_EMU(picture.width), pixels_to_EMU(picture.height)))
            report.add_image(picture)
        else:
            report.merge_cells(start_row=row + 1, start_column=7, end_row=row + len(fields), end_column=12)
            placeholder = put(report, row + 1, 7, item["picture_note"] or "Скриншот ещё не получен")
            placeholder.fill = PatternFill("solid", fgColor=GRAY)
        row += block_rows + 2
    for row, post_id in summary_links:
        jump(put(report, row, 12, ""), "Отчёт", f"A{anchors[post_id]}", "Открыть посты")
    if not view["records"]:
        put(report, summary_end + 6, 1, "Публикации за выбранный период ещё не найдены.")

    # Keep the machine-readable registry and history alongside the presentation.
    registry[0] = registry[0] + ["Клиент", "Язык", "Месяц", "Состояние", "Плановое снятие", "Снимок в отчёте"]
    for index, post in enumerate(filtered_posts, 1):
        item = record_by_id[post["id"]]
        registry[index] = registry[index] + [item["client"], item["languages"], item["month"], item["state"], None, "Открыть снимок"]
        registry[index][5] = "Открыть снимок"
    order = [14, 0, 11, 1, 2, 17, 4, 3, 5, 13, 18, 16, 15, 12, 10, 6, 8, 9]
    registry = [[row[i] for i in order] + ["Доступность проверена" if row_no == 0 else None] for row_no, row in enumerate(registry)]
    registry[0][7] = "Просмотры измерены"
    _write_table(detail, registry)
    for row, post in enumerate(filtered_posts, 2):
        item = record_by_id[post["id"]]
        date_cell(detail, row, 5, item["published"])
        date_cell(detail, row, 8, item["views_checked"])
        date_cell(detail, row, 11, item["until"])
        date_cell(detail, row, 19, item["checked"])
        jump(detail.cell(row, 9), "Отчёт", f"A{anchors[post['id']]}", "Открыть снимок")
    unavailable_headers = ["Клиент", "Канал", "Пост", "Состояние", "Опубликован", "Последняя проверка", "Недоступен с", "Последние просмотры", "Снимок", "Причина"]
    unavailable_rows = [unavailable_headers] + [[p["client"], p["channel"], p["url"], p["state"], None, None, None, p["views"], "Открыть снимок", p["error"]] for p in view["unavailable"]]
    _write_table(unavailable, unavailable_rows)
    for row, item in enumerate(view["unavailable"], 2):
        date_cell(unavailable, row, 5, item["published"])
        date_cell(unavailable, row, 6, item["checked"])
        date_cell(unavailable, row, 7, item["unavailable_since"])
        jump(unavailable.cell(row, 9), "Отчёт", f"A{anchors[item['id']]}", "Последний снимок")
    _write_table(history_sheet, history)
    from .report_view import local
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(timezone)
    post_by_id = {p["id"]: p for p in filtered_posts}
    for row, snap in enumerate(_ordered(filtered_snapshots), 2):
        post = post_by_id[snap["post_id"]]
        for col, value in [(3, post.get("published_at")), (4, snap.get("checked_at")), (10, snap.get("due_at")), (11, snap.get("screenshot_at"))]:
            date_cell(history_sheet, row, col, local(value, tz))
        # The report's current shot remains available even if the channel is gone.
        jump(history_sheet.cell(row, 7), "Отчёт", f"A{anchors[post['id']]}", "Текущий снимок")
    briefs[0] = briefs[0] + ["Клиент", "Язык", "План публикаций"]
    flat_briefs = [b for p in partners for b in p.get("placements", [])]
    brief_lookup = {b["id"]: b for b in flat_briefs}
    for row in briefs[1:]:
        brief = brief_lookup.get(row[4]) if len(row) > 4 else None
        if len(row) > 19:
            row[19] = "Открыть снимки" if brief and any(brief["id"] in p["placement_ids"] for p in view["records"]) else ""
        row.extend([brief.get("client_name", ""), brief.get("language", ""), brief.get("planned_posts")] if brief else ["", "", None])
    brief_order = [21, 0, 4, 5, 6, 7, 23, 22, 9, 10, 11, 12, 8, 14, 19, 20, 1, 2, 3, 13, 15, 16, 17, 18]
    briefs = [[(row + [None] * (24 - len(row)))[i] for i in brief_order] for row in briefs]
    _write_table(briefs_sheet, briefs)
    for row, values in enumerate(briefs[1:], 2):
        brief = brief_lookup.get(values[2])
        if brief:
            date_cell(briefs_sheet, row, 9, local(brief.get("starts_at"), tz))
            date_cell(briefs_sheet, row, 10, local(brief.get("ends_at"), tz))
            linked = [p for p in view["records"] if brief["id"] in p["placement_ids"]]
            if linked:
                jump(briefs_sheet.cell(row, 15), "Отчёт", f"A{anchors[linked[0]['id']]}", "Открыть снимки")
    for sheet in (unavailable, detail, history_sheet, briefs_sheet):
        for cell in sheet[1]:
            cell.fill = PatternFill("solid", fgColor=GRAY)
            cell.font = Font(name="Arial", size=10, bold=True, color=INK)
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        sheet.row_dimensions[1].height = 34
        sheet.freeze_panes = "A2"
        sheet.print_title_rows = "1:1"
        for cells in sheet.iter_rows(min_row=2):
            sheet.row_dimensions[cells[0].row].height = 45
            for cell in cells:
                cell.font = Font(name="Arial", size=10, color="1567B0" if cell.hyperlink else INK, underline="single" if cell.hyperlink else None)
        if sheet.max_row > 1:
            table = Table(displayName={detail.title: "Publications", unavailable.title: "UnavailablePosts", history_sheet.title: "ViewHistory", briefs_sheet.title: "PlacementBriefs"}[sheet.title], ref=sheet.dimensions)
            table.tableStyleInfo = TableStyleInfo(name="TableStyleLight1", showRowStripes=True)
            sheet.add_table(table)
    for col, width in {"A": 22, "B": 25, "C": 28, "D": 40, "E": 23, "F": 26, "G": 14, "H": 23, "I": 20, "J": 16, "K": 23, "L": 13, "M": 10, "R": 45}.items():
        detail.column_dimensions[col].width = width
    if detail.max_row > 1:
        for status in ("Недоступен", "Удалён (подтверждено)", "Доступ к каналу не подтверждён"):
            detail.conditional_formatting.add(f"F2:F{detail.max_row}", CellIsRule(operator="equal", formula=[f'"{status}"'], fill=PatternFill("solid", fgColor=PINK)))
    unavailable.column_dimensions["C"].width = 40
    unavailable.column_dimensions["J"].width = 48
    report.print_title_rows = "7:8"
    report.print_options.horizontalCentered = True
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.stem + ".tmp.xlsx")
    book.save(temporary)
    temporary.replace(output)
    book.close()
    return output
