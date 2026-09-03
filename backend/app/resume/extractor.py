"""Uploaded file -> plain text plus the metadata the pipeline needs.

**The extracted text is not what the model reads for PDFs.** ``pdfplumber``
walks a page line by line, and a resume laid out in two columns has a sidebar
and a body sharing every line: the output interleaves them, so a skills chip
lands in the middle of a job description. That text is good enough for
full-text search and for eyeballing what was uploaded, and it is actively
misleading as LLM input. PDFs are therefore handed to the model as a document
block (:class:`app.llm.client.Document`), which is why this module keeps
``file_bytes`` around and why a text-poor PDF is a warning rather than a
failure.

The same reasoning explains the missing OCR. A scanned PDF needs no OCR here —
the model reads the pages itself — so the only real gap is a scanned image
pasted into a DOCX, or an image uploaded directly. Those are rejected with an
actionable message instead of silently returning an empty profile; wire
:data:`ocr_backend` to close the gap.

The file's declared extension is never trusted: format detection sniffs magic
bytes, so a renamed executable is rejected before any parser touches it.
"""

import io
import time
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Protocol

import docx
import filetype  # type: ignore[import-untyped]  # ships no py.typed marker
import pdfplumber
from docx.document import Document as DocxDocument
from docx.table import Table
from docx.text.paragraph import Paragraph

from app.core.config import settings
from app.core.exceptions import ParsingError
from app.core.logging import get_logger

logger = get_logger(__name__)

BYTES_PER_MB = 1024 * 1024

#: Below this many characters a PDF is assumed to be scanned pages rather than
#: text. Chosen well under the length of a one-page resume: a real CV that
#: extracts to less than this has no text layer worth searching.
MIN_PDF_TEXT_CHARS = 200

#: Signatures we accept, mapped to the ``source_format`` we report.
BINARY_FORMATS = {"pdf": "pdf", "docx": "docx"}

ZIP_MIME = "application/zip"
#: The one part every DOCX contains; its presence identifies the archive.
WORD_DOCUMENT_PART = "word/document.xml"

#: Extensions that identify a plain-text upload. Text and Markdown have no
#: magic bytes at all, so ``filetype`` returns None for them and the extension
#: plus a successful decode is the only evidence available.
TEXT_EXTENSIONS = {".txt": "txt", ".md": "md", ".markdown": "md"}

#: Tried in order. cp1251 comes before latin-1 because Russian resumes saved
#: on Windows are the realistic case; latin-1 accepts every byte string and so
#: can only ever be the last resort.
TEXT_ENCODINGS = ("utf-8", "cp1251", "latin-1")


class OcrBackend(Protocol):
    """Extension point for optical character recognition.

    Nothing implements this yet. Assign :data:`ocr_backend` to enable image
    resumes; the contract is bytes of a single image or document in, plain
    text out.
    """

    def __call__(self, content: bytes, *, media_type: str) -> str: ...


#: Set at startup to enable OCR. ``None`` means image-only content is rejected.
ocr_backend: OcrBackend | None = None


@dataclass(frozen=True, slots=True)
class ExtractedDocument:
    """What one upload yielded, before any LLM sees it."""

    raw_text: str
    page_count: int
    source_format: str
    #: True when a PDF carries no usable text layer. Not fatal — see the module
    #: docstring — but the caller must not use ``raw_text`` as LLM input.
    needs_ocr: bool
    #: Notes about degraded extraction. Never contains document text: these
    #: reach the logs and the resume is personal data.
    warnings: tuple[str, ...]
    #: The upload verbatim, because PDFs go to the model as documents.
    file_bytes: bytes
    size_bytes: int


def extract(content: bytes, filename: str) -> ExtractedDocument:
    """Read an uploaded resume, or raise :class:`ParsingError` explaining why not."""
    started = time.monotonic()
    _reject_bad_size(content)
    source_format = _detect_format(content, filename)

    if source_format == "pdf":
        result = _extract_pdf(content)
    elif source_format == "docx":
        result = _extract_docx(content)
    else:
        result = _extract_plain_text(content, source_format)

    logger.info(
        "resume.extracted",
        source_format=result.source_format,
        size_bytes=result.size_bytes,
        page_count=result.page_count,
        text_chars=len(result.raw_text),
        needs_ocr=result.needs_ocr,
        warnings=len(result.warnings),
        duration_ms=round((time.monotonic() - started) * 1000),
    )
    return result


def _reject_bad_size(content: bytes) -> None:
    """Refuse an empty or oversized upload before any parser allocates memory."""
    limit_mb = settings.resume_max_file_size_mb
    if not content:
        raise ParsingError("the uploaded file is empty (0 bytes)")
    if len(content) > limit_mb * BYTES_PER_MB:
        actual_mb = len(content) / BYTES_PER_MB
        raise ParsingError(
            f"the file is {actual_mb:.1f} MB, above the {limit_mb} MB limit for resumes"
        )


def _detect_format(content: bytes, filename: str) -> str:
    """Identify the upload by its signature, falling back to its extension.

    The extension alone is a claim by whoever uploaded the file, so it decides
    nothing for binary formats: ``filetype.guess`` reads the magic bytes and a
    ``.exe`` renamed to ``.pdf`` never reaches pdfplumber. Plain text and
    Markdown are the exception — they have no signature to read, so a
    successful decode plus a known extension is the only evidence there is.
    """
    # Returns a filetype.Type or None; the library is untyped, hence Any.
    guessed: Any = filetype.guess(content)
    if guessed is not None:
        known = BINARY_FORMATS.get(str(guessed.extension))
        if known is not None:
            return known
        mime = str(guessed.mime)
        if mime == ZIP_MIME and _zip_holds_word_document(content):
            return "docx"
        if mime.startswith("image/"):
            raise ParsingError(
                f"{mime} is an image and OCR is not enabled; "
                "export the resume to PDF or DOCX and upload that"
            )
        raise ParsingError(
            f"unsupported file type {mime}; upload a PDF, DOCX, TXT or Markdown resume"
        )

    suffix = PurePosixPath(filename.replace("\\", "/")).suffix.lower()
    text_format = TEXT_EXTENSIONS.get(suffix)
    if text_format is not None and _decode(content)[0] is not None:
        return text_format
    raise ParsingError(
        "could not identify the file: it matches no known document signature "
        "and is not readable as text; upload a PDF, DOCX, TXT or Markdown resume"
    )


def _zip_holds_word_document(content: bytes) -> bool:
    """Second opinion on an archive ``filetype`` could only call a zip.

    ``filetype`` decides DOCX from the first few archive entries alone, so a
    genuine resume whose zip lists ``customXml/`` parts ahead of ``word/``
    sniffs as a plain zip and would be rejected although Word opens it happily.
    Reading the archive index is still evidence from the bytes themselves, not
    a claim made by the filename, so the signature rule is intact.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            return WORD_DOCUMENT_PART in archive.namelist()
    except Exception:  # an unreadable index means it is not a DOCX, not an error
        return False


def _extract_pdf(content: bytes) -> ExtractedDocument:
    """Text and page count via pdfplumber; a text-poor PDF is a warning, not an error."""
    try:
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            pages = [page.extract_text() or "" for page in pdf.pages]
    except Exception as exc:  # every pdfminer failure is one rejection
        raise ParsingError(f"the PDF could not be read: {type(exc).__name__}") from exc

    raw_text = "\n\n".join(page for page in pages if page.strip())
    warnings: list[str] = []
    needs_ocr = len(raw_text.strip()) < MIN_PDF_TEXT_CHARS
    if needs_ocr:
        warnings.append(
            "the PDF has little or no text layer; full-text search will be poor, "
            "but the pages themselves are still sent to the model"
        )

    return ExtractedDocument(
        raw_text=raw_text,
        page_count=len(pages),
        source_format="pdf",
        needs_ocr=needs_ocr,
        warnings=tuple(warnings),
        file_bytes=content,
        size_bytes=len(content),
    )


def _extract_docx(content: bytes) -> ExtractedDocument:
    """Text of a DOCX in reading order, tables included.

    Tables are not optional. Modern resume templates lay the whole page out as
    a borderless table, and ``document.paragraphs`` skips every cell in it —
    which returns a third of the document with no error to show for it.
    """
    try:
        document = docx.Document(io.BytesIO(content))
        body = "\n".join(_docx_blocks(document.iter_inner_content()))
        chrome = "\n".join(_docx_headers_and_footers(document))
    except Exception as exc:  # every python-docx failure is one rejection
        raise ParsingError(f"the DOCX could not be read: {type(exc).__name__}") from exc

    raw_text = "\n".join(part for part in (body, chrome) if part.strip())
    if not raw_text.strip():
        raw_text = _ocr_or_fail(
            content,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            reason="this DOCX has no text, only images",
        )

    return ExtractedDocument(
        raw_text=raw_text,
        # DOCX has no page concept until it is rendered; Word computes pagination
        # at layout time and python-docx never lays anything out.
        page_count=0,
        source_format="docx",
        needs_ocr=False,
        warnings=(),
        file_bytes=content,
        size_bytes=len(content),
    )


def _docx_blocks(blocks: Iterator[Paragraph | Table]) -> Iterator[str]:
    """Flatten paragraphs and tables, keeping the order they appear in the file."""
    for block in blocks:
        if isinstance(block, Paragraph):
            text = block.text.strip()
            if text:
                yield text
        else:
            yield from _docx_table_rows(block)


def _docx_table_rows(table: Table) -> Iterator[str]:
    """One line per table row, and every cell exactly once.

    ``row.cells`` repeats a merged cell once for each grid position it covers,
    so the tall sidebar of a two-column layout table comes back again for every
    row it spans — the exact template where reading tables mattered in the
    first place. Cells are therefore tracked by the ``w:tc`` element behind
    them, which python-docx shares between the positions of one merge. The
    elements are kept in a list rather than a set of ids because an lxml proxy
    that nothing references may be collected and its id reused.
    """
    seen: list[object] = []
    for row in table.rows:
        fields: list[str] = []
        for cell in row.cells:
            element = cell._tc  # no public identity for a cell exists
            if any(element is other for other in seen):
                continue
            seen.append(element)
            # Newlines and any nested table's own tabs collapse to spaces so
            # that the tab below stays unambiguously the column separator.
            fields.append(" ".join(" ".join(_docx_blocks(cell.iter_inner_content())).split()))
        # Tab-separated so a two-column layout table still reads as two fields
        # rather than one run-on line.
        line = "\t".join(field for field in fields if field)
        if line:
            yield line


def _docx_headers_and_footers(document: DocxDocument) -> Iterator[str]:
    """Header and footer text, which is where contact details often hide."""
    seen: set[str] = set()
    for section in document.sections:
        for part in (section.header, section.footer):
            if part.is_linked_to_previous:
                continue
            for paragraph in part.paragraphs:
                text: str = paragraph.text.strip()
                if text and text not in seen:
                    seen.add(text)
                    yield text


def _extract_plain_text(content: bytes, source_format: str) -> ExtractedDocument:
    """Decode a TXT or Markdown upload, reporting which encoding had to be used."""
    text, encoding = _decode(content)
    if text is None:
        raise ParsingError("the text file is not readable in UTF-8, CP1251 or Latin-1")
    if not text.strip():
        # Whitespace is not content. Rejecting here matches the DOCX path and
        # spares the user a "profile" extracted from nothing.
        raise ParsingError("the file contains no text, only whitespace")

    warnings: list[str] = []
    if encoding == "latin-1":
        # latin-1 never fails, so reaching it means the real encoding is unknown
        # and any non-ASCII character is probably now mojibake.
        warnings.append("encoding could not be determined; non-ASCII characters may be wrong")

    return ExtractedDocument(
        raw_text=text,
        page_count=0,
        source_format=source_format,
        needs_ocr=False,
        warnings=tuple(warnings),
        file_bytes=content,
        size_bytes=len(content),
    )


def _decode(content: bytes) -> tuple[str | None, str | None]:
    """Decode bytes with the first encoding that accepts them."""
    for encoding in TEXT_ENCODINGS:
        try:
            # utf-8-sig strips the BOM Windows editors prepend; it is plain
            # utf-8 for every file that does not have one.
            return content.decode("utf-8-sig" if encoding == "utf-8" else encoding), encoding
        except UnicodeDecodeError:
            continue
    return None, None


def _ocr_or_fail(content: bytes, *, media_type: str, reason: str) -> str:
    """Run the configured OCR backend, or explain what the user should do instead."""
    if ocr_backend is None:
        raise ParsingError(
            f"{reason}, and OCR is not enabled; "
            "upload the PDF version of this resume instead — scanned PDFs are read "
            "by the model directly"
        )
    return ocr_backend(content, media_type=media_type)
