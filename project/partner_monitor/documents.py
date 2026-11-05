"""Read partner references from document attachments without executing content.

The returned text is untrusted source data. Nothing here follows links, runs
Office formulas/macros, opens external resources, or performs PDF OCR.
"""

from __future__ import annotations

from pathlib import Path
import re
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET
import zipfile


MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 50 * 1024 * 1024
MAX_PDF_PAGES = 100
MAX_TEXT_CHARS = 2_000_000
MAX_ARCHIVE_ENTRIES = 10_000
MAX_SPREADSHEET_ROWS = 100_000


class DocumentError(ValueError):
    """An attachment cannot be read safely or within the configured bounds."""


class UnsupportedDocumentError(DocumentError):
    """The attachment format/content needs an unsupported extraction method."""


class _Text:
    def __init__(self) -> None:
        self.parts: list[str] = []
        self.size = 0

    def add(self, value: object) -> None:
        text = str(value).strip()
        if not text:
            return
        self.size += len(text) + 1
        if self.size > MAX_TEXT_CHARS:
            raise DocumentError("Документ содержит слишком много текста (максимум 2 млн символов).")
        self.parts.append(text)

    def result(self) -> str:
        return "\n".join(self.parts)


def _has_declarations(data: bytes) -> bool:
    # Removing NUL bytes covers UTF-16/32 XML too, without decoding text or
    # relying on an attacker-controlled XML encoding declaration.
    return bool(re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", data.replace(b"\x00", b""), re.I))


def _xml(data: bytes) -> ET.Element:
    # ElementTree does not resolve external entities. Reject declarations too,
    # so Office parts cannot use DTD/entity expansion at all.
    if _has_declarations(data):
        raise DocumentError("DTD и XML-сущности в документе не поддерживаются.")
    return ET.fromstring(data)


def _safe_zip(path: Path) -> zipfile.ZipFile:
    archive = zipfile.ZipFile(path)
    try:
        entries = archive.infolist()
        if len(entries) > MAX_ARCHIVE_ENTRIES:
            raise DocumentError("Вложение содержит слишком много частей архива.")
        if sum(entry.file_size for entry in entries) > MAX_UNCOMPRESSED_BYTES:
            raise DocumentError("Распакованный документ превышает лимит 50 МиБ.")
        for entry in entries:
            if entry.flag_bits & 1:
                raise DocumentError("Зашифрованные Office-документы не поддерживаются.")
            if "vbaproject" in entry.filename.casefold() or "vbadata" in entry.filename.casefold():
                raise UnsupportedDocumentError("Office-документы с макросами не поддерживаются.")
        return archive
    except Exception:
        archive.close()
        raise


def _relationships(archive: zipfile.ZipFile, result: _Text) -> None:
    seen: set[str] = set()
    for name in archive.namelist():
        if not name.endswith(".rels"):
            continue
        root = _xml(archive.read(name))
        for relation in root:
            if not relation.attrib.get("Type", "").endswith("/hyperlink"):
                continue
            if relation.attrib.get("TargetMode") != "External":
                continue
            target = relation.attrib.get("Target", "").strip()
            try:
                scheme = urlsplit(target).scheme.lower()
            except ValueError:
                continue
            if scheme in {"http", "https", "tg"} and target not in seen:
                result.add(target)
                seen.add(target)


def _docx(archive: zipfile.ZipFile, result: _Text) -> None:
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    if "word/document.xml" not in archive.namelist():
        raise DocumentError("Во вложении отсутствует основной документ DOCX.")
    for name in archive.namelist():
        if not re.fullmatch(r"word/(?:document|header\d+|footer\d+|footnotes|endnotes)\.xml", name):
            continue
        root = _xml(archive.read(name))
        for paragraph in root.iter(namespace + "p"):
            pieces: list[str] = []
            for element in paragraph.iter():
                if element.tag in {namespace + "t", namespace + "instrText"}:
                    pieces.append(element.text or "")
                elif element.tag in {namespace + "tab", namespace + "br", namespace + "cr"}:
                    pieces.append(" ")
            result.add("".join(pieces))


def _xlsx(path: Path, archive: zipfile.ZipFile, result: _Text) -> None:
    import openpyxl

    # Preflight XML declarations before handing the package to openpyxl.
    for name in archive.namelist():
        if name.endswith(".xml"):
            data = archive.read(name)
            if _has_declarations(data):
                raise DocumentError("DTD и XML-сущности в документе не поддерживаются.")
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=False, keep_links=False)
    from .briefs import _FIELDS
    try:
        rows_seen = 0
        for sheet in workbook:
            result.add(sheet.title)
            headers = None
            first_row = True
            # Ignore inflated formatting dimensions; retain actual cell values.
            sheet.reset_dimensions()
            for row in sheet.iter_rows(values_only=True):
                rows_seen += 1
                if rows_seen > MAX_SPREADSHEET_ROWS:
                    raise DocumentError("В книге слишком много строк (максимум 100 тыс.).")
                values = ["" if value is None else str(value) for value in row]
                if not any(v.strip() for v in values):
                    continue
                if first_row and sum(v.strip().rstrip(":").casefold().replace("ё", "е") in _FIELDS for v in values) >= 2:
                    headers = values
                elif headers:
                    for header, value in zip(headers, values):
                        if value.strip():
                            result.add(f"{header.rstrip(':')}: {value}")
                else:
                    # Preserve label/value rows; formulas remain literal text.
                    result.add("\t".join(values))
                first_row = False
    finally:
        workbook.close()


def _pdf(path: Path, result: _Text) -> None:
    from pypdf import PdfReader

    reader = PdfReader(path)
    if reader.is_encrypted:
        raise UnsupportedDocumentError("Зашифрованные PDF не поддерживаются.")
    if len(reader.pages) > MAX_PDF_PAGES:
        raise DocumentError("PDF превышает лимит 100 страниц.")
    has_text = False
    for page in reader.pages:
        text = page.extract_text() or ""
        if text.strip():
            has_text = True
            result.add(text)
        for annotation_ref in page.get("/Annots", []):
            annotation = annotation_ref.get_object()
            action = annotation.get("/A")
            if action is None:
                continue
            action = action.get_object()
            if action.get("/S") != "/URI":
                continue
            target = str(action.get("/URI", ""))
            try:
                if urlsplit(target).scheme.lower() in {"http", "https", "tg"}:
                    result.add(target)
            except ValueError:
                continue
    if not has_text:
        raise UnsupportedDocumentError("PDF не содержит извлекаемого текста; для скана нужен OCR (не поддерживается).")


def extract_document(path: Path) -> str:
    """Return bounded text and hyperlink targets from XLSX, DOCX, TXT or text PDF.

    Only these extensions are accepted. Invalid, encrypted, oversized, scanned
    or macro-enabled documents raise a clear ``DocumentError`` subclass.
    """
    path = Path(path)
    if not path.is_file():
        raise DocumentError("Файл вложения не найден.")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise DocumentError("Размер вложения превышает лимит 20 МиБ.")
    extension = path.suffix.lower()
    if extension not in {".xlsx", ".docx", ".pdf", ".txt"}:
        raise UnsupportedDocumentError("Поддерживаются XLSX, DOCX, TXT (UTF-8) и PDF с текстовым слоем.")
    result = _Text()
    try:
        if extension == ".txt":
            result.add(path.read_text(encoding="utf-8-sig"))
        elif extension == ".pdf":
            _pdf(path, result)
        else:
            with _safe_zip(path) as archive:
                if extension == ".xlsx":
                    _xlsx(path, archive, result)
                else:
                    _docx(archive, result)
                # Read hyperlink relationships because their targets may be
                # absent from visible labels such as "Telegram channel".
                _relationships(archive, result)
    except DocumentError:
        raise
    except Exception as exc:
        # Never include source text or library exception values in this error.
        raise DocumentError(f"Не удалось прочитать {extension}: повреждённый или неподдерживаемый документ ({type(exc).__name__}).") from exc
    if not result.result().strip():
        raise UnsupportedDocumentError("Документ не содержит извлекаемого текста или ссылок.")
    return result.result()
