"""Turning an arrangement plus the profile's rows into a file and its text.

**The format is DOCX, and the choice is the auditor's rather than a preference.**
``app/resume/ats_audit.py`` exists because phase 2 measured what an employer's
parser gets from a two-column PDF resume: a sidebar heading welded onto a
sentence from the other column, from which "the model extracts confident
nonsense". Everything the audit reports as critical — ``NO_TEXT_LAYER``,
``BROKEN_GLYPHS``, ``UNMAPPED_FONT``, ``COLUMN_INTERLEAVING`` — is a failure of
the step that turns characters into drawn glyphs at fixed coordinates. A DOCX
has no such step. Its text *is* text, in document order, and the four findings
that cost the most points are unreachable by construction rather than avoided by
care.

The audit does raise ``FORMAT_NOT_PDF`` on a DOCX, and the honest thing is to
quote it: it says most employers expect a PDF, and it costs zero points, because
it is a note about what was checked rather than a defect. So the trade is a real
one and it is stated where the owner can see it — the document is generated in
the format that parses best, and the report handed over with it says in as many
words that exporting to PDF changes the layout and should be re-audited. The
other half of the choice is CLAUDE.md's rule about dependencies: ``python-docx``
is already a project dependency, and no PDF writer is. ``reportlab`` is in the
dev group, where it generates test fixtures; promoting a dev tool into the
runtime to produce a format the auditor scores lower is the wrong trade twice.

**One column, no tables, no text boxes.** Not a style choice either.
``check_tables`` reports the share of words living inside a table and warns past
a configured ratio, precisely because many parsers read a table cell by cell and
turn a skills grid into a shuffled bag of words. So the skills section is
paragraphs, the experience section is paragraphs, and nothing in this module
constructs a table at all.

**Rendering is a pure function.** Same arrangement and same profile rows in,
same bytes and same text out. That is what lets ``generated_document`` store the
arrangement rather than the file: a stored version can be rebuilt on demand, and
a file that has drifted from the profile it claims to describe cannot exist,
because there is no file kept to drift.

The plain-text rendering is not a convenience copy. It is the document as the
audit reads it, and it is stored alongside so the audit's verdict stays
explainable after the fact.
"""

import io

from docx import Document as DocxDocument
from docx.shared import Pt
from pydantic import BaseModel, ConfigDict, Field

from app.documents.context import (
    CVContext,
    EducationEntry,
    ExperienceEntry,
    SkillChoice,
    render_period,
)

#: What the file is. Kept as a constant because it is written into every stored
#: row and read back to pick a media type.
FILE_FORMAT = "docx"

#: The media type a browser needs to save the file with the right icon.
MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

#: Section headings, in the words ``app.resume.ats_audit.SECTION_PATTERNS``
#: looks for. This is a contract between the renderer and the auditor, not a
#: label: the audit reports ``MISSING_SECTIONS`` when it cannot find these, so
#: renaming one here to something prettier makes the generated document score
#: worse for no reason a reader would ever see.
HEADING_SUMMARY = "О себе"
HEADING_SKILLS = "Навыки"
HEADING_EXPERIENCE = "Опыт работы"
HEADING_EDUCATION = "Образование"
HEADING_LANGUAGES = "Языки"

#: Point sizes. Three of them, because a CV needs a name, a heading and a body
#: and nothing else; a document with six type sizes reads as a template.
SIZE_NAME = 20
SIZE_HEADING = 12
SIZE_BODY = 10


class CVArrangement(BaseModel):
    """The decisions a generated CV consists of, and nothing else.

    This is the whole of what a model is allowed to decide, and reading the
    field list is the shortest way to see what the feature does and does not do.
    There is no field here for a company, a job title, a date, a skill level or
    a number of years — those are read from the database when the document is
    built — so the model has no way to express a change to one.

    Stored verbatim in ``generated_document.payload``, which is why it is a
    Pydantic model with ``extra="forbid"``: it crosses into JSONB and comes back
    out, and a key that arrived from a future version has to fail loudly rather
    than be rendered.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: One of :attr:`app.documents.context.CVContext.allowed_headlines`, verbatim.
    headline: str = ""
    #: The one free-text field in the document. May be empty.
    summary: str = ""
    #: Every skill the CV shows, in the order it shows them, each with the word
    #: this document uses for it.
    skills: tuple[SkillChoice, ...] = ()
    #: ``(ref, stack)`` per job, in the order the CV presents them.
    experience: tuple[tuple[int, tuple[str, ...]], ...] = Field(default_factory=tuple)

    @property
    def shown_experience(self) -> tuple[tuple[int, tuple[str, ...]], ...]:
        """The jobs and their stacks, for the checks that read membership."""
        return self.experience


def _resolved(
    arrangement: CVArrangement, context: CVContext
) -> list[tuple[ExperienceEntry, tuple[str, ...]]]:
    """Each arranged job paired with the row it refers to, unknown refs dropped.

    Dropping rather than raising because rendering is also how a stored payload
    is rebuilt, and a payload can outlive the experience row it names — a resume
    re-parsed with one job removed leaves exactly that. An old version of a CV
    then renders without the job it no longer has evidence for, which is the
    conservative direction: the alternative is rendering a job from a string in
    the payload, and a job that exists only inside a generated document is the
    invention this whole feature is built to prevent.
    """
    by_ref = context.by_ref
    resolved: list[tuple[ExperienceEntry, tuple[str, ...]]] = []
    for ref, stack in arrangement.experience:
        entry = by_ref.get(ref)
        if entry is not None:
            resolved.append((entry, stack))
    return resolved


def _contact_line(context: CVContext) -> str:
    """Phone, email, city and links on one line, separated by middle dots.

    One line and not a table: see the module docstring. The order puts the two
    the auditor looks for first, so that a document truncated by anything
    downstream keeps the parts an employer needs to reply.
    """
    contacts = context.contacts
    parts = [
        value
        for value in (contacts.phone, contacts.email, contacts.city)
        if value and value.strip()
    ]
    parts.extend(f"{label}: {address}" for label, address in contacts.links)
    return " · ".join(parts)


def to_lines(arrangement: CVArrangement, context: CVContext) -> list[str]:
    """The document as lines of text, in the order it is laid out.

    The single source of both renderings. The .docx writes these lines as
    paragraphs and the plain text joins them, so the file and the text the audit
    reads cannot describe different documents — which they could if each were
    assembled separately, and nothing would notice until an audit explained a
    finding that was not in the file.
    """
    lines: list[str] = []

    if contacts_name := (context.contacts.name or context.profile.name or "").strip():
        lines.append(contacts_name)
    if headline := arrangement.headline.strip():
        lines.append(headline)
    if contact_line := _contact_line(context):
        lines.append(contact_line)

    if summary := arrangement.summary.strip():
        lines.extend(("", HEADING_SUMMARY, summary))

    if arrangement.skills:
        shown = ", ".join(skill.shown_as for skill in arrangement.skills)
        lines.extend(("", HEADING_SKILLS, shown))

    resolved = _resolved(arrangement, context)
    if resolved:
        lines.extend(("", HEADING_EXPERIENCE))
        for entry, stack in resolved:
            lines.append(_job_title_line(entry))
            if period := render_period(entry):
                lines.append(period)
            if stack:
                lines.append(f"Стек: {', '.join(stack)}")

    if context.education:
        lines.extend(("", HEADING_EDUCATION))
        lines.extend(_education_line(item) for item in context.education)

    if context.profile.languages:
        lines.extend(("", HEADING_LANGUAGES, ", ".join(context.profile.languages)))

    return lines


def _job_title_line(entry: ExperienceEntry) -> str:
    """ "Backend Developer — Kaspi" from the columns, never from an answer."""
    parts = [part for part in (entry.title, entry.company) if part]
    return " — ".join(parts)


def _education_line(item: EducationEntry) -> str:
    """One degree on one line, omitting whatever the resume did not state."""
    head = ", ".join(part for part in (item.degree, item.field) if part)
    tail = f", {item.end_year}" if item.end_year else ""
    return f"{item.institution}{f' — {head}' if head else ''}{tail}"


def to_text(arrangement: CVArrangement, context: CVContext) -> str:
    """The document as plain text — what the audit reads and what is stored."""
    return "\n".join(to_lines(arrangement, context)).strip() + "\n"


def to_docx(arrangement: CVArrangement, context: CVContext) -> bytes:
    """The document as a .docx file: one column, paragraphs only, no tables.

    The heading paragraphs are styled by size and weight rather than by Word's
    built-in Heading styles. Both survive extraction identically — the text is
    the text — and a plain paragraph cannot bring an outline level or a numbering
    definition with it, which is one fewer thing for a parser to interpret.
    """
    document = DocxDocument()
    lines = to_lines(arrangement, context)
    headings = {
        HEADING_SUMMARY,
        HEADING_SKILLS,
        HEADING_EXPERIENCE,
        HEADING_EDUCATION,
        HEADING_LANGUAGES,
    }
    name_line = lines[0] if lines else ""

    for index, line in enumerate(lines):
        if not line:
            # A blank line in the layout is spacing, and an empty paragraph is
            # how a .docx spells it. It contributes no words, so it changes
            # nothing the audit counts.
            document.add_paragraph()
            continue
        paragraph = document.add_paragraph()
        run = paragraph.add_run(line)
        if index == 0 and line == name_line:
            run.bold = True
            run.font.size = Pt(SIZE_NAME)
        elif line in headings:
            run.bold = True
            run.font.size = Pt(SIZE_HEADING)
        else:
            run.font.size = Pt(SIZE_BODY)

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def filename_for(context: CVContext) -> str:
    """What the downloaded file is called.

    The candidate's name and the company, because the person downloading this
    will have twenty of them in one folder and "cv.docx" twenty times is not a
    filename. Reduced to characters that survive every filesystem: a job title
    with a slash in it is a real posting and an unopenable file.
    """
    parts = [context.contacts.name or context.profile.name or "cv", context.vacancy.company or ""]
    stem = "-".join(_slug(part) for part in parts if _slug(part)) or "cv"
    return f"{stem}.{FILE_FORMAT}"


def _slug(value: str) -> str:
    """A filename-safe fragment, keeping letters of either alphabet."""
    kept = [char if char.isalnum() else "-" for char in value.strip()]
    return "-".join(part for part in "".join(kept).split("-") if part)[:60]


def letter_to_docx(text: str, context: CVContext) -> bytes:
    """A cover letter as a .docx, so it can be attached rather than only pasted.

    The letter's text is not touched. It was generated, checked and stored by
    :mod:`app.letters`, a person may have edited it since, and this function's
    only job is to put it in a file — so it splits on blank lines into
    paragraphs and writes nothing of its own. In particular it adds no contact
    block: hh filters a letter carrying an address as spam, ``app.letters.guard``
    enforces that by reading the text, and a header helpfully added here would
    break the rule from outside the module that guards it.
    """
    document = DocxDocument()
    for block in [part.strip() for part in text.split("\n\n")]:
        paragraph = document.add_paragraph()
        run = paragraph.add_run(block)
        run.font.size = Pt(SIZE_BODY)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def letter_filename_for(context: CVContext) -> str:
    """What the downloaded letter is called: the CV's name with a suffix."""
    stem = filename_for(context).rsplit(".", 1)[0]
    return f"{stem}-letter.{FILE_FORMAT}"
