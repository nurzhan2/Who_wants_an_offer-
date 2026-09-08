"""Documents kept as examples of shape, and getting the text out of them.

A reference is somebody's good CV or somebody's good cover letter. It is shown
to the model as an example of **form** — how long, in what order, with what
register, saying which kinds of thing where — and never as a source of content.
The content is the owner's profile, and there is nothing else.

Three things follow, and all three are enforced somewhere rather than hoped for:

**Only the text is kept.** The upload is read once, through
:mod:`app.resume.extractor` — the same extractor an uploaded resume goes
through, because there is no second thing to write and a second one would drift
— and then discarded. Nothing downstream can use anything but the text, so
keeping the bytes would mean holding somebody else's document indefinitely for
no gain.

**The text is untrusted.** It is a file this project did not write, and the
warning ``app/llm/base.py`` gives about vacancy descriptions applies to it word
for word: "Ignore the previous instructions..." is the base case, not an exotic
one. So a reference reaches the model fenced, defanged and labelled as data —
:mod:`app.workshop.prompt` — and the fence it is wrapped in cannot be closed
from inside it.

**It is bounded.** A CV is one or two pages; a reference twice that is a company
brochure somebody saved as a CV, and every character of it is billed on every
generation. Long references are clipped, several of them are clipped together,
and the clipping is visible rather than silent.

A PDF is worth one warning of its own. ``pdfplumber`` reads a two-column layout
line by line, so a sidebar and a body come back interleaved — the extractor's own
docstring says so, which is why a *resume* PDF goes to the model as a document
rather than as this text. A reference has no such path: it is stored as text and
quoted as text. So a two-column PDF reference is stored interleaved, and the
honest answer is to say so at upload and let the person paste the text instead.
"""

from pydantic import BaseModel, ConfigDict, Field

from app.core.exceptions import ParsingError
from app.db.enums import ReferenceKind
from app.resume import extractor

#: Longest one reference may be when it reaches a prompt. Two pages of a CV is
#: around 4 000 characters; this leaves room for a long one and stops a
#: forty-page "portfolio" from becoming the most expensive part of every call.
MAX_REFERENCE_CHARS = 8_000

#: Ceiling on the whole rendered block, across every reference in it. A prompt
#: already carries the vacancy description and possibly past letters; past this
#: the examples outweigh the instructions and the model writes pastiche.
MAX_BLOCK_CHARS = 12_000

#: How many references of one kind are shown. Two is a comparison; five is a
#: corpus, and a corpus is what the model starts averaging instead of following.
MAX_PER_KIND = 2

#: Below this a "reference" is not a document. A CV or a letter shorter than
#: this is a fragment, and storing it as an example of shape teaches a shape
#: nobody meant.
MIN_REFERENCE_CHARS = 120

#: What is appended where a reference was clipped. Visible on purpose: the model
#: is being shown a document that stops in the middle, and it should know that
#: rather than imitate the abrupt ending.
CLIP_MARKER = "\n[...]"


class ReferenceText(BaseModel):
    """One reference as everything downstream sees it: text, and what it is."""

    model_config = ConfigDict(frozen=True)

    #: The row's id as a string. Rendered into no prompt; carried so a report
    #: can say which reference a generation was shown.
    id: str
    kind: ReferenceKind
    title: str
    #: The owner's note on what makes it good. Reaches the model, because "the
    #: opening names the product rather than the company" is exactly the kind of
    #: instruction a reference exists to give — and it is the owner's own text,
    #: not the uploaded document's.
    note: str | None = None
    text: str = Field(min_length=1)


class ExtractedReference(BaseModel):
    """What one uploaded file yielded, before it is stored."""

    model_config = ConfigDict(frozen=True)

    text: str
    source_format: str
    size_bytes: int
    #: Notes for the person about degraded extraction. Never document text: this
    #: reaches the API and the logs, and the file is somebody's CV.
    warnings: tuple[str, ...] = ()


#: Said about a PDF whose text layer is there but whose layout may not survive.
#: Every PDF gets it: whether a page is one column or two is not something this
#: can tell without the audit the resume path runs, and a warning that only
#: sometimes appears is one nobody learns to read.
PDF_LAYOUT_WARNING = (
    "PDF читается построчно: в двухколоночном макете боковая колонка и основной "
    "текст перемешиваются. Если эталон свёрстан в две колонки, вставьте его "
    "текстом — так он сохранит порядок."
)

#: Said when the file gave almost no text at all.
SCANNED_WARNING = (
    "В файле почти нет текстового слоя — похоже, это скан. Как эталон формы он "
    "бесполезен: вставьте текст вручную."
)


def extract(content: bytes, filename: str) -> ExtractedReference:
    """Read an uploaded reference, or raise :class:`ParsingError` explaining why not.

    Delegates wholly to :func:`app.resume.extractor.extract`: format sniffing,
    the size limit, the DOCX table handling and the encoding fallbacks are all
    already written there and there is no second version of them here. What this
    adds is the part that is about references rather than resumes — the floor
    below which a document is not an example of anything, and the warnings a
    person needs to see before deciding to keep this file as their model.
    """
    document = extractor.extract(content, filename)
    text = document.raw_text.strip()

    warnings = list(document.warnings)
    if document.source_format == "pdf":
        warnings.append(PDF_LAYOUT_WARNING)
    if document.needs_ocr:
        warnings.append(SCANNED_WARNING)

    if len(text) < MIN_REFERENCE_CHARS:
        raise ParsingError(
            f"only {len(text)} characters could be read from this file, which is "
            f"below the {MIN_REFERENCE_CHARS} a document has to have to be an "
            "example of anything; paste the text instead"
        )

    return ExtractedReference(
        text=text,
        source_format=document.source_format,
        size_bytes=document.size_bytes,
        warnings=tuple(warnings),
    )


def accept_text(text: str) -> str:
    """Validate a reference pasted as text rather than uploaded as a file.

    The same floor as an extracted one, for the same reason. Pasting is not a
    way round the check; it is a way round the extractor.
    """
    stripped = text.strip()
    if len(stripped) < MIN_REFERENCE_CHARS:
        raise ParsingError(
            f"the text is {len(stripped)} characters long, below the "
            f"{MIN_REFERENCE_CHARS} a document has to have to be an example of "
            "anything"
        )
    return stripped


def clip(text: str, limit: int = MAX_REFERENCE_CHARS) -> str:
    """One reference, bounded, with the cut marked rather than hidden."""
    stripped = text.strip()
    if len(stripped) <= limit:
        return stripped
    return stripped[:limit].rstrip() + CLIP_MARKER


def within_budget(
    references: tuple[ReferenceText, ...],
    *,
    per_kind: int = MAX_PER_KIND,
    budget: int = MAX_BLOCK_CHARS,
) -> tuple[ReferenceText, ...]:
    """The references a prompt actually gets, in the order they were given.

    Two passes, both from the front: at most :data:`MAX_PER_KIND` of each kind,
    then as many as fit the character budget. Dropping from the back rather than
    sampling keeps it predictable — the person's list order is what decides, and
    a reference that was shown yesterday is shown today.
    """
    kept: list[ReferenceText] = []
    counted: dict[ReferenceKind, int] = {}
    spent = 0
    for reference in references:
        seen = counted.get(reference.kind, 0)
        if seen >= per_kind:
            continue
        clipped = clip(reference.text)
        if spent + len(clipped) > budget:
            continue
        counted[reference.kind] = seen + 1
        spent += len(clipped)
        kept.append(reference.model_copy(update={"text": clipped}))
    return tuple(kept)
