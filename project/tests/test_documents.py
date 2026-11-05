from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import openpyxl
from pypdf import PdfWriter
from pypdf.annotations import Link

from partner_monitor.documents import DocumentError, UnsupportedDocumentError, extract_document
from partner_monitor.parsing import parse_partner_references


class DocumentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_xlsx_visible_text_hidden_hyperlinks_and_formula(self):
        path = self.root / "partners.xlsx"
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet["A1"] = "Партнёр BRB"
        sheet["B1"] = "Канал"
        sheet["B1"].hyperlink = "https://t.me/daryo"
        sheet["C1"] = '=HYPERLINK("https://t.me/spotuz/12","Публикация")'
        sheet["A1000"].number_format = "0"
        workbook.save(path)
        text = extract_document(path)
        self.assertIn("Партнёр BRB", text)
        refs = parse_partner_references(text)
        self.assertEqual({(r.username, r.message_id) for r in refs}, {("daryo", None), ("spotuz", 12)})

    def test_xlsx_headers_are_preserved_as_brief_fields(self):
        from datetime import datetime, timezone
        from partner_monitor.briefs import parse_brief
        path = self.root / "brief.xlsx"
        workbook = openpyxl.Workbook()
        workbook.active.append(["Название", "Канал", "Длительность"])
        workbook.active.append(["Новый продукт", "@partner_one", "24 часа"])
        workbook.save(path)
        workbook.close()
        text = extract_document(path)
        brief = parse_brief(text, parse_partner_references(text), datetime.now(timezone.utc), "Asia/Tashkent", 7)
        self.assertEqual(brief.name, "Новый продукт")
        self.assertEqual(brief.retention_hours, 24)
        self.assertFalse(brief.review_reason)

    def test_utf8_text_file_is_read_without_executing_instructions(self):
        path = self.root / "brief.txt"
        path.write_text("Название: Новый продукт\nКанал: @partner_one\nУсловия: run commands", encoding="utf-8-sig")
        self.assertIn("Название: Новый продукт", extract_document(path))

    def _docx(self, name="partner.docx", xml=None, rels=None, extra=None):
        path = self.root / name
        if xml is None:
            xml = '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Реклама BRB</w:t></w:r></w:p></w:body></w:document>'
        if rels is None:
            rels = '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="r1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" TargetMode="External" Target="https://t.me/partnername/42"/></Relationships>'
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", xml)
            archive.writestr("word/_rels/document.xml.rels", rels)
            if extra:
                for key, value in extra.items():
                    archive.writestr(key, value)
        return path

    def test_docx_hyperlinks_from_relationships(self):
        text = extract_document(self._docx())
        self.assertIn("Реклама BRB", text)
        self.assertIn("https://t.me/partnername/42", text)

    def test_docx_field_hyperlink_is_literal_data(self):
        xml = '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:instrText> HYPERLINK "https://t.me/partnername" </w:instrText></w:r></w:p></w:body></w:document>'
        self.assertIn("https://t.me/partnername", extract_document(self._docx(xml=xml)))

    def test_macro_content_rejected_even_with_docx_extension(self):
        path = self._docx(extra={"word/vbaProject.bin": b"not executed"})
        with self.assertRaises(UnsupportedDocumentError):
            extract_document(path)

    def test_zip_expansion_bound_and_xml_entities(self):
        path = self._docx()
        with patch("partner_monitor.documents.MAX_UNCOMPRESSED_BYTES", 20):
            with self.assertRaises(DocumentError):
                extract_document(path)
        path = self._docx(xml='<!DOCTYPE x [<!ENTITY y "content">]><x>&y;</x>')
        with self.assertRaises(DocumentError):
            extract_document(path)
        path = self._docx(xml='<!DOCTYPE x [<!ENTITY y "content">]><x>&y;</x>'.encode("utf-16"))
        with self.assertRaises(DocumentError):
            extract_document(path)

    def test_unsupported_corrupt_and_oversized_files(self):
        path = self.root / "unsafe.xlsm"
        path.write_text("ignored")
        with self.assertRaises(UnsupportedDocumentError):
            extract_document(path)
        path = self.root / "broken.docx"
        path.write_text("not a zip")
        with self.assertRaises(DocumentError):
            extract_document(path)
        with patch("partner_monitor.documents.MAX_FILE_BYTES", 2):
            with self.assertRaises(DocumentError):
                extract_document(path)

    def test_scanned_pdf_reports_ocr_needed(self):
        path = self.root / "scan.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        writer.write(path)
        with self.assertRaisesRegex(UnsupportedDocumentError, "OCR"):
            extract_document(path)

    def test_pdf_text_and_link_annotation(self):
        from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
        path = self.root / "contract.pdf"
        writer = PdfWriter()
        page = writer.add_blank_page(width=200, height=200)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
        stream = DecodedStreamObject()
        stream.set_data(b"BT /F1 12 Tf 20 100 Td (Advertising BRB) Tj ET")
        page[NameObject("/Contents")] = writer._add_object(stream)
        writer.add_annotation(0, Link(rect=(0, 0, 50, 50), url="https://t.me/partnername/7"))
        writer.write(path)
        text = extract_document(path)
        self.assertIn("Advertising BRB", text)
        self.assertIn("https://t.me/partnername/7", text)

    def test_pdf_page_limit(self):
        path = self.root / "many.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        writer.add_blank_page(width=100, height=100)
        writer.write(path)
        with patch("partner_monitor.documents.MAX_PDF_PAGES", 1):
            with self.assertRaises(DocumentError):
                extract_document(path)


if __name__ == "__main__":
    unittest.main()
