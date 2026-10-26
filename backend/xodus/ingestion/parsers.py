"""Streaming parsers for Markdown, plain text, CSV, PDF (text layer) and DOCX.

Every source file is untrusted. The rules applied here:
  * the size limit is enforced on the bytes actually read, not only on the size the file system reports;
  * nothing is executed or fetched: no macros, no embedded objects, no external references, no OCR;
  * text is read in blocks (never the whole file at once), PDFs page by page, DOCX XML as a stream;
  * DOCX archives are checked for entry count, total size and compression ratio before anything is read, and
    documents that declare a DTD or entities are refused (XML entity-expansion attacks);
  * any parser failure becomes a ParseError with a code and a message meant for the user.
"""

from __future__ import annotations

import codecs
import csv
import hashlib
import io
import re
import zipfile
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

from xodus.config.settings import KnowledgeSettings
from xodus.ingestion.text import Block, clean_text, collapse_blank_lines

MiB = 2**20
EXTENSIONS = {".md": "markdown", ".markdown": "markdown", ".txt": "text", ".text": "text", ".log": "text",
              ".csv": "csv", ".pdf": "pdf", ".docx": "docx"}
TEXT_FORMATS = {"markdown", "text", "csv"}
MAX_BLOCK_CHARS = 8000  # parsers cut blocks here; the chunker splits further
MAX_LINE_CHARS = 65_536
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class ParseError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Limits:
    max_file_bytes: int
    max_pdf_pages: int
    max_docx_uncompressed: int
    max_zip_ratio: int
    max_zip_entries: int
    max_csv_field: int

    @classmethod
    def from_settings(cls, k: KnowledgeSettings) -> Limits:
        return cls(k.max_file_mb * MiB, k.max_pdf_pages, k.max_docx_uncompressed_mb * MiB, k.max_zip_ratio,
                   k.max_zip_entries, k.max_csv_field_kb * 1024)


@dataclass(frozen=True)
class SourceInfo:
    format: str
    size_bytes: int
    content_hash: str
    encoding: str | None
    pages: int | None = None


def detect_format(path: Path) -> str:
    fmt = EXTENSIONS.get(path.suffix.lower())
    if fmt is None:
        raise ParseError("unsupported_format", f"{path.suffix or 'files without an extension'} is not supported "
                                               f"(supported: {', '.join(sorted(EXTENSIONS))}).")
    return fmt


def hash_file(path: Path, max_bytes: int) -> tuple[str, int]:
    """SHA-256 and size of the bytes actually read (a file that grows past the limit while read is refused)."""
    sha, total = hashlib.sha256(), 0
    with open(path, "rb") as fh:
        while chunk := fh.read(MiB):
            total += len(chunk)
            if total > max_bytes:
                raise ParseError("file_too_large", f"The file is larger than the {max_bytes // MiB} MB limit.")
            sha.update(chunk)
    return sha.hexdigest(), total


def inspect_file(path: Path, limits: Limits) -> SourceInfo:
    """One streaming pass over the file: format, size limit, SHA-256, text encoding, page count."""
    fmt = detect_format(path)
    size = path.stat().st_size
    if size == 0:
        raise ParseError("empty_file", "The file is empty.")
    if size > limits.max_file_bytes:
        raise ParseError("file_too_large", f"The file is {size / MiB:.1f} MB; the limit is {limits.max_file_bytes // MiB} MB.")
    sha, total, head = hashlib.sha256(), 0, b""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    bad_utf8, utf16 = False, False
    with open(path, "rb") as fh:
        while chunk := fh.read(MiB):
            if not head:
                head = chunk[:8192]
                utf16 = head[:2] in (b"\xff\xfe", b"\xfe\xff")
            total += len(chunk)
            if total > limits.max_file_bytes:
                raise ParseError("file_too_large", "The file grew past the size limit while it was being read.")
            sha.update(chunk)
            if fmt in TEXT_FORMATS and not utf16:
                if b"\x00" in chunk:
                    raise ParseError("binary_file", "The file contains NUL bytes, so it is not a text file.")
                if not bad_utf8:
                    try:
                        decoder.decode(chunk)
                    except UnicodeDecodeError:
                        bad_utf8 = True
    encoding: str | None = None
    if fmt in TEXT_FORMATS:
        if head.startswith(b"\xef\xbb\xbf"):
            encoding = "utf-8-sig"
        elif utf16:
            encoding = "utf-16"
        else:
            if not bad_utf8:
                try:
                    decoder.decode(b"", final=True)
                except UnicodeDecodeError:
                    bad_utf8 = True
            encoding = "cp1252" if bad_utf8 else "utf-8"
    elif fmt == "pdf" and b"%PDF-" not in head[:1024]:
        raise ParseError("not_a_pdf", "The file does not start like a PDF.")
    elif fmt == "docx" and not head.startswith(b"PK\x03\x04"):
        raise ParseError("not_a_docx", "The file is not a DOCX (Office Open XML) archive.")
    pages = _pdf_pages(path, limits) if fmt == "pdf" else None
    return SourceInfo(fmt, total, sha.hexdigest(), encoding, pages)


# ---------------------------------------------------------------------------------------------- text and markdown


def _iter_lines(path: Path, encoding: str) -> Iterator[tuple[str, bool]]:
    """Decoded lines as (text, ended_with_newline). Lines longer than MAX_LINE_CHARS come back in pieces."""
    errors = "strict" if encoding in ("utf-8", "utf-8-sig") else "replace"  # utf-8 was validated by inspect_file()
    with open(path, encoding=encoding, errors=errors, newline="") as fh:
        pending = ""
        while True:
            data = fh.read(MAX_LINE_CHARS)
            if data.endswith("\r"):
                data += fh.read(1)  # never split a \r\n pair across two reads
            if not data:
                break
            pending += data.replace("\r\n", "\n").replace("\r", "\n")
            *lines, pending = pending.split("\n")
            for line in lines:
                yield line, True
            while len(pending) > MAX_LINE_CHARS:
                yield pending[:MAX_LINE_CHARS], False
                pending = pending[MAX_LINE_CHARS:]
        if pending:
            yield pending, False


_ATX = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t#]*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


def _heading_path(stack: list[tuple[int, str]]) -> str:
    return " > ".join(title for _, title in stack)


def iter_markdown_blocks(path: Path, encoding: str, *, markdown: bool = True) -> Iterator[Block]:
    stack: list[tuple[int, str]] = []
    paragraph: list[str] = []
    para_start = offset = 0
    size = 0
    fence: str | None = None

    def flush() -> Iterator[Block]:
        nonlocal paragraph, size
        text = collapse_blank_lines(clean_text("\n".join(paragraph)))
        if text:
            yield Block(text, _heading_path(stack), None, None, para_start)
        paragraph, size = [], 0

    for line, ended in _iter_lines(path, encoding):
        line_start, offset = offset, offset + len(line) + (1 if ended else 0)
        if markdown:
            fence_match = _FENCE.match(line)
            if fence_match:
                marker = fence_match.group(1)
                if fence is None:
                    yield from flush()
                    fence, para_start = marker[0] * 3, line_start
                elif marker.startswith(fence):
                    paragraph.append(line)
                    fence = None
                    yield from flush()
                    continue
            if fence is None:
                heading = _ATX.match(line)
                if heading:
                    yield from flush()
                    level = len(heading.group(1))
                    while stack and stack[-1][0] >= level:
                        stack.pop()
                    stack.append((level, clean_text(heading.group(2)).strip()))
                    yield Block(clean_text(heading.group(2)).strip(), _heading_path(stack), None, None, line_start)
                    continue
        if fence is None and not line.strip():
            yield from flush()
            continue
        if not paragraph:
            para_start = line_start
        paragraph.append(line)
        size += len(line) + 1
        if size >= MAX_BLOCK_CHARS:
            yield from flush()
    yield from flush()


# ---------------------------------------------------------------------------------------------- csv


def iter_csv_blocks(path: Path, encoding: str, limits: Limits, title: str) -> Iterator[Block]:
    csv.field_size_limit(limits.max_csv_field)
    errors = "replace" if encoding == "cp1252" else "strict"
    with open(path, encoding=encoding, errors=errors, newline="") as fh:
        sample = fh.read(65_536)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        reader = csv.reader(fh, dialect)
        try:
            header = [clean_text(h).strip() or f"column {i + 1}" for i, h in enumerate(next(reader))]
        except StopIteration:
            return
        except csv.Error as exc:
            raise ParseError("malformed_csv", f"The CSV could not be read: {exc}") from exc
        heading = f"{title}: " + ", ".join(header)
        heading = heading[:160]
        offset = 0
        try:
            for number, row in enumerate(reader, start=2):
                cells = []
                for i, value in enumerate(row):
                    value = clean_text(value).strip()
                    if value:
                        cells.append(f"{header[i] if i < len(header) else f'column {i + 1}'}: {value}")
                text = " | ".join(cells)
                if text:
                    yield Block(text[:MAX_BLOCK_CHARS], heading, f"row {number}", None, offset)
                    offset += len(text) + 1
        except csv.Error as exc:
            raise ParseError("malformed_csv", f"The CSV could not be read: {exc}") from exc


# ---------------------------------------------------------------------------------------------- pdf


def _open_pdf(path: Path, limits: Limits):  # noqa: ANN202 - pypdf type is imported lazily
    from pypdf import PdfReader

    try:
        reader = PdfReader(str(path), strict=False)
        if reader.is_encrypted:
            raise ParseError("encrypted_pdf", "The PDF is encrypted; encrypted documents are not ingested.")
        pages = len(reader.pages)
    except ParseError:
        raise
    except Exception as exc:  # pypdf raises many types for damaged files
        raise ParseError("malformed_pdf", f"The PDF could not be read ({type(exc).__name__}).") from exc
    if pages > limits.max_pdf_pages:
        raise ParseError("too_many_pages", f"The PDF has {pages} pages; the limit is {limits.max_pdf_pages}.")
    return reader, pages


def _pdf_pages(path: Path, limits: Limits) -> int:
    return _open_pdf(path, limits)[1]


def iter_pdf_blocks(path: Path, limits: Limits) -> Iterator[Block]:
    reader, pages = _open_pdf(path, limits)
    offset, produced, failed = 0, 0, 0
    for index in range(pages):
        try:
            text = reader.pages[index].extract_text() or ""
        except Exception:  # one bad page must not lose the others
            failed += 1
            continue
        finally:
            _release_page_cache(reader)
        text = collapse_blank_lines(clean_text(text))
        if not text:
            continue
        number = index + 1
        for paragraph in _split_paragraphs(text):
            yield Block(paragraph, "", f"page {number}", number, offset)
            offset += len(paragraph) + 2
            produced += 1
    if produced == 0:
        reason = f"; {failed} pages could not be read" if failed else ""
        raise ParseError("no_text_layer", "The PDF has no extractable text (a scanned PDF needs OCR, which is not supported)" + reason + ".")


def _release_page_cache(reader) -> None:  # noqa: ANN001
    """pypdf keeps every parsed page object; drop them so memory does not grow with the page count."""
    flush = getattr(reader, "_flush_cache", None)
    if callable(flush):
        try:
            flush()
        except Exception:
            pass


def _split_paragraphs(text: str) -> Iterator[str]:
    """Paragraphs at blank lines; text without any is grouped from lines into blocks of about 1,000 characters."""
    parts = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if len(parts) > 1:
        yield from (p[:MAX_BLOCK_CHARS] for p in parts)
        return
    current: list[str] = []
    size = 0
    for line in text.split("\n"):
        current.append(line)
        size += len(line) + 1
        if size >= 1000:
            yield "\n".join(current)[:MAX_BLOCK_CHARS]
            current, size = [], 0
    if current:
        yield "\n".join(current)[:MAX_BLOCK_CHARS]


# ---------------------------------------------------------------------------------------------- docx

_HEADING_STYLE = re.compile(r"^heading\s*([1-9])$", re.IGNORECASE)


def _check_docx_archive(zf: zipfile.ZipFile, limits: Limits) -> zipfile.ZipInfo:
    infos = zf.infolist()
    if len(infos) > limits.max_zip_entries:
        raise ParseError("zip_too_many_entries", f"The archive has {len(infos)} entries; the limit is {limits.max_zip_entries}.")
    total = sum(i.file_size for i in infos)
    if total > limits.max_docx_uncompressed:
        raise ParseError("zip_bomb", f"The archive would expand to {total // MiB} MB; the limit is {limits.max_docx_uncompressed // MiB} MB.")
    for info in infos:
        if info.file_size > MiB and info.file_size > limits.max_zip_ratio * max(1, info.compress_size):
            raise ParseError("zip_bomb", f"{info.filename!r} expands by more than {limits.max_zip_ratio}x; refusing it.")
    names = {i.filename for i in infos}
    if "word/document.xml" not in names or "[Content_Types].xml" not in names:
        raise ParseError("not_a_docx", "The archive has no word/document.xml; it is not a DOCX document.")
    return zf.getinfo("word/document.xml")


def iter_docx_blocks(path: Path, limits: Limits) -> Iterator[Block]:
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ParseError("malformed_docx", f"The DOCX archive could not be opened ({type(exc).__name__}).") from exc
    with zf:
        document = _check_docx_archive(zf, limits)
        with zf.open(document) as probe:
            head = probe.read(16_384).lower()
        if b"<!doctype" in head or b"<!entity" in head:
            raise ParseError("xml_entities", "The document declares a DTD or entities; this is refused (entity-expansion attack).")
        with zf.open(document) as stream:
            try:
                yield from _docx_blocks(stream)
            except ET.ParseError as exc:
                raise ParseError("malformed_docx", f"The document XML is malformed ({exc}).") from exc
            except (zipfile.BadZipFile, zlib.error) as exc:
                raise ParseError("malformed_docx", f"The DOCX archive is damaged ({exc}).") from exc


def _docx_blocks(stream: io.BufferedIOBase) -> Iterator[Block]:
    heading_stack: list[tuple[int, str]] = []
    runs: list[str] = []
    style: str | None = None
    cells: list[list[str]] = []  # one list of paragraph texts per open table cell
    rows: list[list[str]] = []  # one list of cell texts per open table row
    parents: list[ET.Element] = []
    offset = 0
    produced = 0

    for event, elem in ET.iterparse(stream, events=("start", "end")):
        tag = elem.tag
        if event == "start":
            parents.append(elem)
            if tag == W + "p":
                runs, style = [], None
            elif tag == W + "tr":
                rows.append([])
            elif tag == W + "tc":
                cells.append([])
            continue
        parents.pop()
        if tag == W + "t":
            runs.append(elem.text or "")
        elif tag == W + "tab":
            runs.append("\t")
        elif tag in (W + "br", W + "cr"):
            runs.append("\n")
        elif tag == W + "pStyle":
            style = elem.get(W + "val")
        elif tag == W + "p":
            text = collapse_blank_lines(clean_text("".join(runs)))
            if cells:
                if text:
                    cells[-1].append(text)
            elif text:
                level = _docx_level(style)
                if level:
                    while heading_stack and heading_stack[-1][0] >= level:
                        heading_stack.pop()
                    heading_stack.append((level, text[:200]))
                yield Block(text[:MAX_BLOCK_CHARS], _heading_path(heading_stack), None, None, offset)
                offset += len(text) + 2
                produced += 1
            _drop(elem, parents)
        elif tag == W + "tc":
            done = cells.pop()
            if rows:
                rows[-1].append(" ".join(done))
        elif tag == W + "tr":
            row = [c for c in rows.pop() if c.strip()]
            if row:
                text = " | ".join(row)[:MAX_BLOCK_CHARS]
                yield Block(text, _heading_path(heading_stack), None, None, offset)
                offset += len(text) + 2
                produced += 1
            _drop(elem, parents)
    if produced == 0:
        raise ParseError("no_text", "The document contains no text.")


def _drop(elem: ET.Element, parents: list[ET.Element]) -> None:
    """Free a finished element so memory does not grow with document length."""
    elem.clear()
    if parents:
        try:
            parents[-1].remove(elem)
        except ValueError:
            pass


def _docx_level(style: str | None) -> int | None:
    if not style:
        return None
    if style.lower() == "title":
        return 1
    match = _HEADING_STYLE.match(style.replace(" ", ""))
    return int(match.group(1)) if match else None


# ---------------------------------------------------------------------------------------------- entry point


def iter_blocks(path: Path, info: SourceInfo, limits: Limits) -> Iterator[Block]:
    """Blocks of a file already checked by inspect_file()."""
    try:
        if info.format == "markdown" or info.format == "text":
            yield from iter_markdown_blocks(path, info.encoding or "utf-8", markdown=info.format == "markdown")
        elif info.format == "csv":
            yield from iter_csv_blocks(path, info.encoding or "utf-8", limits, path.stem)
        elif info.format == "pdf":
            yield from iter_pdf_blocks(path, limits)
        elif info.format == "docx":
            yield from iter_docx_blocks(path, limits)
        else:  # pragma: no cover - detect_format already restricts this
            raise ParseError("unsupported_format", f"Unsupported format {info.format!r}.")
    except ParseError:
        raise
    except UnicodeDecodeError as exc:
        raise ParseError("bad_encoding", f"The text could not be decoded as {info.encoding}.") from exc
    except RecursionError as exc:
        raise ParseError("malformed_" + info.format, "The document is nested too deeply to read.") from exc
    except MemoryError as exc:
        raise ParseError("out_of_memory", "The document needs more memory than is allowed.") from exc
