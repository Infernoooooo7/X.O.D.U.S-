"""Parsers: Markdown, text, CSV, PDF, DOCX; encodings; hostile and malformed input."""

from __future__ import annotations

import random
import tracemalloc
import zipfile
from pathlib import Path

import pytest

from tests.kb_helpers import pdf_bytes, write_csv, write_docx, write_pdf, zip_with
from xodus.config.settings import KnowledgeSettings
from xodus.ingestion.parsers import Limits, ParseError, detect_format, hash_file, inspect_file, iter_blocks

LIM = Limits.from_settings(KnowledgeSettings())


def blocks(path: Path, limits: Limits = LIM):
    info = inspect_file(path, limits)
    return info, list(iter_blocks(path, info, limits))


def small(**over) -> Limits:
    values = dict(max_file_bytes=LIM.max_file_bytes, max_pdf_pages=LIM.max_pdf_pages, max_docx_uncompressed=LIM.max_docx_uncompressed,
                  max_zip_ratio=LIM.max_zip_ratio, max_zip_entries=LIM.max_zip_entries, max_csv_field=LIM.max_csv_field)
    values.update(over)
    return Limits(**values)


# ---------------------------------------------------------------------------------------------- format detection


@pytest.mark.parametrize("name, fmt", [("a.md", "markdown"), ("A.MARKDOWN", "markdown"), ("a.txt", "text"), ("a.log", "text"),
                                       ("a.csv", "csv"), ("A.PDF", "pdf"), ("a.docx", "docx")])
def test_supported_extensions(name, fmt):
    assert detect_format(Path(name)) == fmt


@pytest.mark.parametrize("name", ["a.exe", "a.doc", "a.xlsx", "a.html", "a.py", "noext", "a.txt.exe", "a.pdf.lnk"])
def test_unsupported_extensions_are_refused(name):
    with pytest.raises(ParseError) as err:
        detect_format(Path(name))
    assert err.value.code == "unsupported_format"


def test_magic_bytes_must_match_the_extension(tmp_path):
    fake_pdf = tmp_path / "x.pdf"
    fake_pdf.write_text("just some text")
    fake_docx = tmp_path / "x.docx"
    fake_docx.write_text("just some text")
    for path, code in ((fake_pdf, "not_a_pdf"), (fake_docx, "not_a_docx")):
        with pytest.raises(ParseError) as err:
            inspect_file(path, LIM)
        assert err.value.code == code


def test_empty_file_and_size_limit(tmp_path):
    empty = tmp_path / "e.txt"
    empty.write_bytes(b"")
    with pytest.raises(ParseError) as err:
        inspect_file(empty, LIM)
    assert err.value.code == "empty_file"
    big = tmp_path / "b.txt"
    big.write_text("x" * 5000)
    with pytest.raises(ParseError) as err:
        inspect_file(big, small(max_file_bytes=1000))
    assert err.value.code == "file_too_large"
    with pytest.raises(ParseError) as err:  # hashing enforces the limit on bytes actually read
        hash_file(big, 1000)
    assert err.value.code == "file_too_large"


def test_content_hash_is_stable_and_changes_with_content(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("one")
    h1 = inspect_file(p, LIM).content_hash
    assert inspect_file(p, LIM).content_hash == h1
    p.write_text("two")
    assert inspect_file(p, LIM).content_hash != h1


# ---------------------------------------------------------------------------------------------- text encodings


def test_utf8_bom_and_utf16(tmp_path):
    (tmp_path / "bom.txt").write_bytes(b"\xef\xbb\xbf" + "Grüße aus Köln".encode())
    (tmp_path / "u16.txt").write_bytes("Привет мир, это текст".encode("utf-16"))
    info, got = blocks(tmp_path / "bom.txt")
    assert info.encoding == "utf-8-sig" and got[0].text == "Grüße aus Köln"
    info, got = blocks(tmp_path / "u16.txt")
    assert info.encoding == "utf-16" and "Привет" in got[0].text


def test_cp1252_fallback_for_non_utf8_bytes(tmp_path):
    p = tmp_path / "old.txt"
    p.write_bytes("café crème brûlée".encode("cp1252"))
    info, got = blocks(p)
    assert info.encoding == "cp1252" and got[0].text == "café crème brûlée"


def test_binary_content_in_a_text_file_is_refused(tmp_path):
    p = tmp_path / "bin.txt"
    p.write_bytes(b"MZ\x90\x00\x03\x00\x00\x00" + bytes(range(256)))
    with pytest.raises(ParseError) as err:
        inspect_file(p, LIM)
    assert err.value.code == "binary_file"


def test_crlf_and_control_characters_are_normalised(tmp_path):
    p = tmp_path / "c.txt"
    p.write_bytes(b"first line\r\nsecond\x01line with\x07bell\r\n\r\nnew paragraph \xe2\x80\xae reversed\r\n")
    _, got = blocks(p)
    texts = [b.text for b in got]
    assert texts[0] == "first line\nsecondline withbell"
    assert "\u202e" not in texts[1] and "\x01" not in "".join(texts)  # control and bidi-override characters are gone


def test_crlf_pair_split_across_read_boundaries(tmp_path):
    p = tmp_path / "boundary.txt"
    # the first read ends exactly between \r and \n
    p.write_bytes(b"a" * 65535 + b"\r\n" + b"b" * 10 + b"\r\n\r\nsecond paragraph")
    _, got = blocks(p)
    assert [b.text[:3] for b in got][-2:] == ["bbb", "sec"]
    # Each \r\n counts as one newline, including the pair split across two reads. A phantom extra newline
    # would shift these character offsets by one (the a-line is 65535 characters, so b starts at 65536).
    assert [b.start for b in got][-2:] == [65536, 65548]


def test_one_enormous_line_is_split_not_loaded_whole(tmp_path):
    p = tmp_path / "long.txt"
    p.write_text("word " * 200_000)  # 1 MB on a single line
    _, got = blocks(p)
    assert len(got) > 100 and max(len(b.text) for b in got) <= 8000


def test_large_text_file_streams_with_bounded_memory(tmp_path):
    p = tmp_path / "big.txt"
    p.write_text(("A paragraph of ordinary text that repeats many times. " * 20 + "\n\n") * 4000)  # ~4.4 MB
    info = inspect_file(p, LIM)
    tracemalloc.start()
    count = sum(1 for _ in iter_blocks(p, info, LIM))
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert count == 4000 and peak < 3 * 2**20  # far below the file size


# ---------------------------------------------------------------------------------------------- markdown


def test_markdown_heading_paths_and_code_fences(tmp_path):
    p = tmp_path / "g.md"
    p.write_text("# Guide\n\nIntro text.\n\n## Install\n\nStep one.\n\n```bash\n# this is a comment, not a heading\nmake\n```\n\n"
                 "### Windows\n\nUse the installer.\n\n## Usage\n\nRun it.\n", encoding="utf-8")
    _, got = blocks(p)
    by_text = {b.text: b.heading for b in got}
    assert by_text["Guide"] == "Guide"
    assert by_text["Intro text."] == "Guide"
    assert by_text["Step one."] == "Guide > Install"
    assert by_text["Use the installer."] == "Guide > Install > Windows"
    assert by_text["Run it."] == "Guide > Usage"  # a level-2 heading closes the level-3 one
    fenced = next(b for b in got if "make" in b.text)
    assert "# this is a comment" in fenced.text and fenced.heading == "Guide > Install"
    assert not any(b.text == "this is a comment, not a heading" for b in got)


def test_plain_text_has_no_markdown_headings(tmp_path):
    p = tmp_path / "t.txt"
    p.write_text("# not a heading in a text file\n\nsecond", encoding="utf-8")
    _, got = blocks(p)
    assert got[0].text.startswith("# not a heading") and got[0].heading == ""


def test_unterminated_fence_does_not_swallow_memory(tmp_path):
    p = tmp_path / "f.md"
    p.write_text("```\n" + "code line\n" * 5000, encoding="utf-8")
    _, got = blocks(p)
    assert len(got) > 3 and max(len(b.text) for b in got) <= 8100


# ---------------------------------------------------------------------------------------------- csv


def test_csv_rows_become_labelled_blocks(tmp_path):
    p = write_csv(tmp_path / "people.csv", ["name", "city", "note"], [["Ann", "Oslo", "likes\nmultiline notes"], ["Bo", "", "x"]])
    info, got = blocks(p)
    assert info.format == "csv" and len(got) == 2
    assert got[0].text == "name: Ann | city: Oslo | note: likes\nmultiline notes" and got[0].loc == "row 2"
    assert got[1].text == "name: Bo | note: x" and got[1].loc == "row 3"  # empty cells are dropped
    assert got[0].heading.startswith("people: name, city, note")


def test_csv_semicolon_delimiter_and_cp1252(tmp_path):
    p = write_csv(tmp_path / "eu.csv", ["Straße", "Preis"], [["Hauptstraße", "3,50"]], delimiter=";", encoding="cp1252")
    info, got = blocks(p)
    assert info.encoding == "cp1252" and "Straße: Hauptstraße" in got[0].text and "Preis: 3,50" in got[0].text


def test_csv_ragged_rows_and_header_only(tmp_path):
    p = tmp_path / "r.csv"
    p.write_text("a,b\n1,2,3,4\n5\n", encoding="utf-8")
    _, got = blocks(p)
    assert "column 3: 3" in got[0].text and got[1].text == "a: 5"
    h = tmp_path / "h.csv"
    h.write_text("a,b\n", encoding="utf-8")
    assert blocks(h)[1] == []


def test_csv_oversized_field_is_refused(tmp_path):
    p = tmp_path / "huge.csv"
    p.write_text('a,b\n"' + "x" * 50_000 + '",1\n', encoding="utf-8")
    info = inspect_file(p, LIM)
    with pytest.raises(ParseError) as err:
        list(iter_blocks(p, info, small(max_csv_field=1000)))
    assert err.value.code == "malformed_csv"


# ---------------------------------------------------------------------------------------------- pdf


def test_pdf_text_layer_by_page(tmp_path):
    p = write_pdf(tmp_path / "doc.pdf", ["First page about astronomy.\nStars and nebulae.", "Second page about cooking.\nButter and flour."])
    info, got = blocks(p)
    assert info.format == "pdf" and info.pages == 2
    assert {b.page for b in got} == {1, 2}
    assert next(b for b in got if "astronomy" in b.text).loc == "page 1"
    assert next(b for b in got if "cooking" in b.text).loc == "page 2"


def test_pdf_page_limit(tmp_path):
    p = write_pdf(tmp_path / "many.pdf", [f"page {i}" for i in range(6)])
    with pytest.raises(ParseError) as err:
        inspect_file(p, small(max_pdf_pages=5))
    assert err.value.code == "too_many_pages" and "6 pages" in err.value.message


def test_pdf_without_text_layer(tmp_path):
    p = write_pdf(tmp_path / "blank.pdf", ["", ""])
    info = inspect_file(p, LIM)
    with pytest.raises(ParseError) as err:
        list(iter_blocks(p, info, LIM))
    assert err.value.code == "no_text_layer" and "OCR" in err.value.message


def test_pdf_garbage_after_header(tmp_path):
    p = tmp_path / "junk.pdf"
    p.write_bytes(b"%PDF-1.7\n" + bytes(random.Random(1).randrange(256) for _ in range(4000)))
    with pytest.raises(ParseError) as err:
        inspect_file(p, LIM)
    assert err.value.code == "malformed_pdf"


def test_encrypted_pdf_is_refused(tmp_path):
    from pypdf import PdfReader, PdfWriter

    src = write_pdf(tmp_path / "plain.pdf", ["secret text"])
    writer = PdfWriter(clone_from=PdfReader(str(src)))
    try:
        writer.encrypt("pw", algorithm="RC4-128")
    except Exception as exc:  # pypdf may need an optional crypto package for some algorithms
        pytest.skip(f"cannot create an encrypted PDF here: {exc}")
    enc = tmp_path / "enc.pdf"
    with open(enc, "wb") as fh:
        writer.write(fh)
    with pytest.raises(ParseError) as err:
        inspect_file(enc, LIM)
    assert err.value.code == "encrypted_pdf"


def test_damaged_pdfs_only_ever_raise_parse_errors(tmp_path):
    """Truncations and byte flips of a valid PDF: a ParseError or a result, never another exception."""
    good = pdf_bytes([f"Paragraph about topic {i}.\nMore words follow here." for i in range(4)])
    rng = random.Random(7)
    outcomes = {"parsed": 0, "refused": 0}
    for n in range(60):
        data = bytearray(good)
        if n % 2:
            del data[rng.randrange(len(data) // 3, len(data)):]
        else:
            for _ in range(rng.randint(1, 12)):
                data[rng.randrange(len(data))] = rng.randrange(256)
        p = tmp_path / f"d{n}.pdf"
        p.write_bytes(bytes(data))
        try:
            info = inspect_file(p, LIM)
            list(iter_blocks(p, info, LIM))
            outcomes["parsed"] += 1
        except ParseError:
            outcomes["refused"] += 1
    assert outcomes["parsed"] + outcomes["refused"] == 60


# ---------------------------------------------------------------------------------------------- docx


def test_docx_headings_paragraphs_and_tables(tmp_path):
    p = write_docx(tmp_path / "r.docx", [("h1", "Annual report"), ("p", "Opening remarks about the year."), ("h2", "Results"),
                                          ("p", "Revenue grew steadily."), ("h1", "Outlook"), ("p", "Next year looks fine.")],
                   table=[["Region", "Sales"], ["North", "120"], ["South", "95"]])
    info, got = blocks(p)
    assert info.format == "docx"
    by_text = {b.text: b.heading for b in got}
    assert by_text["Opening remarks about the year."] == "Annual report"
    assert by_text["Revenue grew steadily."] == "Annual report > Results"
    assert by_text["Next year looks fine."] == "Outlook"
    rows = [b.text for b in got if "|" in b.text]
    assert rows == ["Region | Sales", "North | 120", "South | 95"]


def test_docx_not_an_archive_or_missing_document(tmp_path):
    p = tmp_path / "x.docx"
    p.write_bytes(b"PK\x03\x04" + b"not really a zip" * 50)
    with pytest.raises(ParseError) as err:
        blocks(p)
    assert err.value.code == "malformed_docx"
    q = zip_with(tmp_path / "y.docx", {"hello.txt": b"hi", "[Content_Types].xml": b"<x/>"})
    with pytest.raises(ParseError) as err:
        blocks(q)
    assert err.value.code == "not_a_docx"


def test_docx_zip_bomb_by_total_size_and_ratio(tmp_path):
    xml = b"<?xml version='1.0'?><w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'><w:body>" + \
          b"<w:p><w:r><w:t>x</w:t></w:r></w:p>" * 10 + b"</w:body></w:document>"
    bomb = zip_with(tmp_path / "bomb.docx", {"[Content_Types].xml": b"<x/>", "word/document.xml": xml + b" " * (30 * 2**20)})
    with pytest.raises(ParseError) as err:
        blocks(bomb, small(max_docx_uncompressed=10 * 2**20))
    assert err.value.code == "zip_bomb" and "limit" in err.value.message
    with pytest.raises(ParseError) as err:  # under the size cap but a far too high compression ratio
        blocks(bomb, small(max_zip_ratio=50))
    assert err.value.code == "zip_bomb" and "50x" in err.value.message


def test_docx_too_many_entries(tmp_path):
    entries = {f"f{i}.txt": b"x" for i in range(120)}
    entries.update({"[Content_Types].xml": b"<x/>", "word/document.xml": b"<w:document xmlns:w='x'/>"})
    p = zip_with(tmp_path / "many.docx", entries)
    with pytest.raises(ParseError) as err:
        blocks(p, small(max_zip_entries=100))
    assert err.value.code == "zip_too_many_entries"


@pytest.mark.parametrize("prologue", [
    b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;">]>',
    b'<?xml version="1.0"?><!doctype w [<!entity xxe SYSTEM "file:///C:/Windows/win.ini">]>',
])
def test_docx_entity_declarations_are_refused(tmp_path, prologue):
    xml = prologue + b"<w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'><w:body>" \
                     b"<w:p><w:r><w:t>&lol2;</w:t></w:r></w:p></w:body></w:document>"
    p = zip_with(tmp_path / "xxe.docx", {"[Content_Types].xml": b"<x/>", "word/document.xml": xml})
    with pytest.raises(ParseError) as err:
        blocks(p)
    assert err.value.code == "xml_entities"


def test_docx_malformed_xml_and_empty_document(tmp_path):
    bad = zip_with(tmp_path / "bad.docx", {"[Content_Types].xml": b"<x/>", "word/document.xml": b"<w:document><w:body><w:p>"})
    with pytest.raises(ParseError) as err:
        blocks(bad)
    assert err.value.code == "malformed_docx"
    empty = write_docx(tmp_path / "empty.docx", [])
    with pytest.raises(ParseError) as err:
        blocks(empty)
    assert err.value.code == "no_text"


def test_docx_streams_a_long_document_with_bounded_memory(tmp_path):
    body = "".join(f"<w:p><w:r><w:t>Paragraph number {i} with some ordinary words in it.</w:t></w:r></w:p>" for i in range(60_000))
    xml = ("<?xml version='1.0'?><w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'><w:body>"
           + body + "</w:body></w:document>").encode()
    p = zip_with(tmp_path / "long.docx", {"[Content_Types].xml": b"<x/>", "word/document.xml": xml})
    info = inspect_file(p, LIM)
    tracemalloc.start()
    count = sum(1 for _ in iter_blocks(p, info, LIM))
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert count == 60_000 and peak < 8 * 2**20


def test_docx_archive_entries_cannot_escape_through_names(tmp_path):
    """Entry names are only looked up, never extracted to disk: a path-traversal name is harmless."""
    p = zip_with(tmp_path / "evil.docx", {"../../evil.txt": b"x", "[Content_Types].xml": b"<x/>",
                                          "word/document.xml": b"<w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'><w:body>"
                                                              b"<w:p><w:r><w:t>fine</w:t></w:r></w:p></w:body></w:document>"})
    _, got = blocks(p)
    assert [b.text for b in got] == ["fine"] and not (tmp_path.parent / "evil.txt").exists()
    assert zipfile.is_zipfile(p)
