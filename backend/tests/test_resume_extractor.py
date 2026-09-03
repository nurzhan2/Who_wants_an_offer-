"""Uploaded bytes in, :class:`ExtractedDocument` or a refusal out.

Two properties of this module are load-bearing for everything downstream and
are what most of these tests defend:

* **The bytes survive.** A PDF is handed to the model as a document block, not
  as the text extracted here, so ``file_bytes`` must come back byte-for-byte.
  A copy that is "almost" the upload is a corrupted document for the model.
* **The extension decides nothing.** Format comes from the signature, so a
  renamed executable is refused before pdfplumber ever opens it, and a PDF a
  user saved as ``.docx`` still parses.

Everything hostile is built here from bytes literals and ``tmp_path`` — the
checked-in fixtures are the well-formed cases only. No network, no model, no
database.
"""

import base64
import io
import zipfile
from pathlib import Path
from typing import Any

import docx
import filetype  # type: ignore[import-untyped]  # ships no py.typed marker
import pytest
import structlog
from docx.document import Document as DocxDocument

from app.core.config import settings
from app.core.exceptions import ParsingError
from app.resume import extractor
from app.resume.extractor import BYTES_PER_MB, extract

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parent / "fixtures" / "resumes"

DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

#: A one-pixel PNG. Small enough to inline, real enough that python-docx reads
#: its dimensions and embeds it.
ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQ"
    "AAAABJRU5ErkJggg=="
)

#: A structurally valid PDF with one page and no content stream at all — the
#: shape a scanned resume degrades to once its images carry no text layer.
TEXTLESS_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 595 842]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n"
    b"%%EOF\n"
)

#: The DOS header every Windows executable starts with.
EXECUTABLE = b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff\x00\x00" + b"\x00" * 128


def fixture_bytes(name: str) -> bytes:
    """Read one checked-in resume fixture."""
    return (FIXTURES / name).read_bytes()


def docx_bytes(document: DocxDocument) -> bytes:
    """Serialise an in-memory python-docx document to the bytes of an upload."""
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


# ── the well-formed fixtures ──────────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "source_format"),
    [
        ("single_column_ru.pdf", "pdf"),
        ("two_column_ru.pdf", "pdf"),
        ("english.pdf", "pdf"),
        ("mixed_ru_en.pdf", "pdf"),
        ("with_table.docx", "docx"),
        ("plain.txt", "txt"),
    ],
)
def test_every_fixture_extracts_to_usable_text(name: str, source_format: str) -> None:
    """The six shapes a real upload takes must all produce text and the format
    the pipeline branches on. A fixture that stops extracting is a regression
    nothing downstream would report, because an empty profile looks like a
    sparse resume."""
    content = fixture_bytes(name)

    result = extract(content, name)

    assert result.source_format == source_format
    assert result.raw_text.strip()
    assert result.size_bytes == len(content)


@pytest.mark.parametrize(
    "name",
    ["single_column_ru.pdf", "two_column_ru.pdf", "english.pdf", "mixed_ru_en.pdf"],
)
def test_a_real_pdf_reports_its_pages_and_is_not_mistaken_for_a_scan(name: str) -> None:
    """A PDF with a text layer must come back with pages counted and
    ``needs_ocr`` false: raising the OCR flag on a normal resume would send the
    user off to re-export a file that was fine."""
    result = extract(fixture_bytes(name), name)

    assert result.page_count > 0
    assert result.needs_ocr is False
    assert result.warnings == ()


@pytest.mark.parametrize(
    "name", ["single_column_ru.pdf", "with_table.docx", "plain.txt", "english.pdf"]
)
def test_the_upload_is_kept_byte_for_byte(name: str) -> None:
    """``file_bytes`` is what the model actually reads for a PDF. Any
    re-encoding, truncation or normalisation on the way through this module
    would hand the model a different document than the one uploaded."""
    content = fixture_bytes(name)

    assert extract(content, name).file_bytes == content


# ── the signature decides, not the extension ──────────────────────────


def test_a_renamed_executable_is_refused_before_any_parser_sees_it() -> None:
    """The extension is a claim made by whoever uploaded the file. If it were
    trusted, ``resume.pdf`` would be enough to feed arbitrary binaries to
    pdfminer."""
    with pytest.raises(ParsingError) as excinfo:
        extract(EXECUTABLE, "resume.pdf")

    assert "unsupported file type" in str(excinfo.value)


def test_a_pdf_saved_under_a_docx_name_is_still_read_as_a_pdf() -> None:
    """The mirror image of the rejection above: sniffing must not only refuse
    lies, it must also rescue the honest mistake of a wrong extension."""
    content = fixture_bytes("english.pdf")

    result = extract(content, "resume.docx")

    assert result.source_format == "pdf"
    assert result.raw_text == extract(content, "english.pdf").raw_text


def test_an_image_upload_is_refused_with_something_to_do_about_it() -> None:
    """A photo of a CV is the commonest bad upload. Without OCR it can only be
    refused, so the message has to name the way out rather than say "no"."""
    with pytest.raises(ParsingError) as excinfo:
        extract(ONE_PIXEL_PNG, "resume.png")

    message = str(excinfo.value)
    assert "image/png" in message
    assert "PDF" in message


def test_a_docx_that_sniffs_as_a_plain_zip_is_still_read() -> None:
    """``filetype`` judges a DOCX from the first archive entries alone, and the
    checked-in fixture is exactly the case it gets wrong. Word opens it; so
    must we, by reading the archive index rather than believing the filename."""
    content = fixture_bytes("with_table.docx")
    assert str(filetype.guess(content).extension) != "docx", "fixture no longer exercises this path"

    assert extract(content, "with_table.docx").source_format == "docx"


def test_an_archive_without_a_word_document_part_is_refused_as_an_unknown_type() -> None:
    """The archive-index fallback must not turn every zip into a resume: a
    renamed zip has no ``word/document.xml``. The message matters as much as
    the refusal — a fallback that waved this through would still fail, but only
    once python-docx choked on it, and the user would be told their DOCX is
    corrupt instead of that they uploaded the wrong file."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("holiday-photos/readme.txt", "not a resume")

    with pytest.raises(ParsingError) as excinfo:
        extract(buffer.getvalue(), "resume.docx")

    message = str(excinfo.value)
    assert "unsupported file type" in message
    assert "application/zip" in message


def test_a_file_that_is_neither_a_known_signature_nor_text_is_refused() -> None:
    """Plain text has no magic bytes, so the extension is all there is for
    ``.txt``. That leniency must not leak to unknown binary content."""
    with pytest.raises(ParsingError) as excinfo:
        extract(b"\x00\x01\x02\x03" * 32, "resume.bin")

    assert "could not identify the file" in str(excinfo.value)


def test_a_corrupt_pdf_is_one_rejection_not_a_pdfminer_traceback() -> None:
    """Truncated and malformed PDFs are ordinary user input. They must surface
    as a ParsingError the API can render, not as whatever pdfminer raises."""
    with pytest.raises(ParsingError) as excinfo:
        extract(b"%PDF-1.4\nthis is not a PDF body at all\n", "resume.pdf")

    assert "the PDF could not be read" in str(excinfo.value)


# ── size limits ───────────────────────────────────────────────────────


@pytest.fixture
def small_limit(monkeypatch: pytest.MonkeyPatch) -> int:
    """Shrink the upload limit to 1 MB so the size tests stay cheap."""
    monkeypatch.setattr(settings, "resume_max_file_size_mb", 1)
    return 1


def test_an_oversized_upload_is_refused_with_both_numbers_and_no_content(
    small_limit: int,
) -> None:
    """The user needs to know the limit and how far over it they are, and the
    message reaches the logs — where a resume must never appear."""
    half_over = small_limit * BYTES_PER_MB * 3 // 2
    content = b"SECRET-RESUME-BYTES" + b"\x00" * (half_over - 19)

    with pytest.raises(ParsingError) as excinfo:
        extract(content, "resume.pdf")

    message = str(excinfo.value)
    assert f"{small_limit} MB limit" in message
    assert "1.5 MB" in message
    assert "SECRET" not in message


def test_an_empty_upload_is_refused(small_limit: int) -> None:
    """A zero-byte file is a failed upload, not a resume with no content. It
    must be named as such before a parser is asked to make sense of nothing."""
    with pytest.raises(ParsingError) as excinfo:
        extract(b"", "resume.pdf")

    assert "empty" in str(excinfo.value)


def test_an_upload_exactly_at_the_limit_is_accepted(small_limit: int) -> None:
    """The limit is inclusive. An off-by-one here rejects the very file the
    documented maximum promised would work."""
    content = b"a" * (small_limit * BYTES_PER_MB)

    result = extract(content, "resume.txt")

    assert result.size_bytes == small_limit * BYTES_PER_MB


# ── DOCX: the content lives in tables ─────────────────────────────────


def test_docx_table_text_is_extracted_although_paragraphs_alone_would_miss_it() -> None:
    """The reason ``_docx_table_rows`` exists. The fixture is a modern resume
    template: one bold name as a paragraph, everything else — jobs, skills,
    contacts — inside a layout table. The contrast below is the whole point:
    walking ``document.paragraphs`` loses the resume and reports success."""
    content = fixture_bytes("with_table.docx")
    paragraphs_only = "\n".join(p.text for p in docx.Document(io.BytesIO(content)).paragraphs)

    raw_text = extract(content, "with_table.docx").raw_text

    assert "SQLAlchemy" in raw_text
    assert "SQLAlchemy" not in paragraphs_only


def test_a_vertically_merged_cell_is_reported_once() -> None:
    """``row.cells`` yields a merged cell again for every row it spans, so a
    tall sidebar would be repeated once per row — inflating the text and making
    a skill look like it appears three times to anything counting mentions."""
    document = docx.Document()
    table = document.add_table(rows=3, cols=2)
    table.cell(0, 0).merge(table.cell(2, 0))
    table.cell(0, 0).text = "Kubernetes"
    for index, right in enumerate(("first", "second", "third")):
        table.cell(index, 1).text = right

    raw_text = extract(docx_bytes(document), "merged.docx").raw_text

    assert raw_text.count("Kubernetes") == 1
    assert "third" in raw_text


def test_docx_header_text_is_kept() -> None:
    """Contact details are routinely put in the header, where the body walk
    never looks — and a resume without an email is unusable."""
    document = docx.Document()
    document.add_paragraph("Опыт работы")
    document.sections[0].header.paragraphs[0].text = "candidate@example.com"

    assert "candidate@example.com" in extract(docx_bytes(document), "cv.docx").raw_text


# ── plain text decoding ───────────────────────────────────────────────


@pytest.mark.parametrize("encoding", ["utf-8", "cp1251"])
def test_a_russian_text_resume_round_trips(encoding: str) -> None:
    """Russian resumes are saved on Windows as often as not, and cp1251 bytes
    are not valid UTF-8. Failing over to it is what keeps a whole class of real
    uploads from arriving as mojibake or a rejection."""
    original = "Опыт работы: Python-разработчик, Алматы"

    result = extract(original.encode(encoding), "resume.txt")

    assert result.raw_text == original


def test_a_utf8_bom_is_not_part_of_the_text() -> None:
    """Windows editors prepend a BOM. Left in place it becomes an invisible
    first character of the first heading and quietly breaks any comparison
    against it."""
    result = extract("﻿Резюме".encode(), "resume.md")

    assert result.raw_text == "Резюме"
    assert result.source_format == "md"


def test_an_undecodable_encoding_still_yields_text_but_warns() -> None:
    """latin-1 accepts every byte string, so reaching it proves the real
    encoding is unknown. The text is returned because something is better than
    nothing, and the warning exists because it is probably wrong."""
    result = extract(b"Caf\x98 experience: 5 years of Python engineering", "resume.txt")

    assert result.raw_text
    assert any("encoding could not be determined" in warning for warning in result.warnings)


def test_a_whitespace_only_file_is_refused() -> None:
    """Whitespace is not content. Accepting it would send an empty document to
    the model and bill for a profile extracted from nothing."""
    with pytest.raises(ParsingError) as excinfo:
        extract(b"   \r\n\t\n   ", "resume.txt")

    assert "no text" in str(excinfo.value)


# ── OCR: a gap for DOCX, not for PDF ──────────────────────────────────


def test_a_text_poor_pdf_succeeds_and_says_why_it_is_degraded() -> None:
    """The decision this pins: a scanned PDF is NOT a failure. The model reads
    the pages itself, so the extraction succeeds and only warns — turning this
    into a rejection would refuse every scanned resume the product can handle."""
    result = extract(TEXTLESS_PDF, "scan.pdf")

    assert result.source_format == "pdf"
    assert result.page_count == 1
    assert result.needs_ocr is True
    assert result.warnings
    assert result.file_bytes == TEXTLESS_PDF


def image_only_docx() -> bytes:
    """A DOCX whose entire content is one embedded image."""
    document = docx.Document()
    document.add_picture(io.BytesIO(ONE_PIXEL_PNG))
    return docx_bytes(document)


def test_an_image_only_docx_is_refused_with_a_next_step() -> None:
    """Unlike a PDF, a DOCX is not sent to the model as a document, so a
    scan pasted into Word yields nothing at all. Returning an empty profile
    would look like a bad resume; the refusal has to name the way forward."""
    with pytest.raises(ParsingError) as excinfo:
        extract(image_only_docx(), "scan.docx")

    message = str(excinfo.value)
    assert "no text, only images" in message
    assert "PDF" in message


def test_an_injected_ocr_backend_closes_the_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    """The refusal above is a configuration state, not a dead end: wiring
    ``ocr_backend`` must make the same upload succeed, and the backend must be
    handed the DOCX media type so it knows what it was given."""
    seen: dict[str, Any] = {}

    def fake_ocr(content: bytes, *, media_type: str) -> str:
        seen["media_type"] = media_type
        seen["content"] = content
        return "Python developer, five years"

    monkeypatch.setattr(extractor, "ocr_backend", fake_ocr)
    content = image_only_docx()

    result = extract(content, "scan.docx")

    assert result.raw_text == "Python developer, five years"
    assert seen["media_type"] == DOCX_MEDIA_TYPE
    assert seen["content"] == content


# ── logs never carry the resume ───────────────────────────────────────


@pytest.mark.parametrize(
    ("filename", "secret"),
    [("text.txt", "SEKRET-MARKER-42"), ("with_table.docx", "SQLAlchemy")],
)
def test_no_log_line_contains_document_text(filename: str, secret: str) -> None:
    """A resume is personal data and the log stream is not. Everything logged
    here must be a count or a duration; the moment someone logs ``raw_text`` to
    debug an extraction, this fails."""
    content = (
        f"Опыт работы: {secret}".encode() if filename.endswith(".txt") else fixture_bytes(filename)
    )

    with structlog.testing.capture_logs() as entries:
        extract(content, filename)

    assert entries, "extraction logged nothing, so this test proves nothing"
    assert secret not in repr(entries)
