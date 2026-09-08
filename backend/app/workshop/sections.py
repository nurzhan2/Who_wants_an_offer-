"""Finding a named section of a plain-text document, and counting its items.

The owner's example of a rule is «в навыках не меньше 21 пункта». To check that
by reading the finished document, two things have to be decided from text alone:
where the section called "навыки" starts and ends, and what counts as one item
inside it.

Neither has a correct answer, only a defensible one, so both are written down
here rather than spread through the checkers:

**A heading** is a short line that announces what follows. Four spellings are
recognised, because those are the four a model writes when asked for plain text
with no markdown: a Markdown heading (``## Навыки``), a line ending in a colon
(``Навыки:``), an all-capitals line (``НАВЫКИ``), and a bold line
(``**Навыки**``). A long line is never a heading whatever it ends with — a
sentence ending in a colon before a list is prose, and reading it as a heading
would swallow the section that follows.

**An item** is one entry of the section, and the section decides which of two
shapes it has. A body written as lines — bulleted or not — has one item per
line. A body written as a single line of separated values (``Python, Go, SQL``)
has one item per value. Both are ordinary ways to write a skills section, and a
counter that understood only one of them would report 1 for the other and stop
a letter for a rule the document actually keeps.

What this deliberately does not do is parse structure the way a resume parser
would. It answers "how many things are listed under this heading", which is what
a rule can ask, and it answers it the same way every time so that a person can
predict what their own rule will do.
"""

import re
from dataclasses import dataclass

#: Longest line still eligible to be a heading. A heading is a label; past this
#: it is a sentence that happens to end in a colon, and treating it as a heading
#: would put the section boundary in the middle of a paragraph.
MAX_HEADING_CHARS = 70

#: Most words a colon may still be announcing a section after. The colon is the
#: only heading spelling that also occurs mid-prose — "Пишу об этом сразу,
#: чтобы не тратить ваше время: опыта здесь нет" is a sentence, not a heading —
#: and a word count is what separates a label from a clause. The Markdown,
#: bold and all-capitals forms need no such guard: nobody writes a sentence
#: that way by accident.
MAX_HEADING_WORDS = 6

#: A heading is a label and labels do not end sentences. Any of these in a
#: candidate heading means prose, whatever its length.
_SENTENCE_MARKS = frozenset(".!?")

#: Bullet markers stripped from the front of an item. The dash forms are the
#: ASCII hyphen, the en dash and the em dash, which models mix freely.
_BULLET = re.compile(r"^\s*(?:[-*•–—·]|\d{1,2}[.)])\s+")

#: What separates values inside a single-line list. The semicolon and the
#: middle dot are as common as the comma in a Russian skills line, and the pipe
#: turns up in documents written for an ATS.
_INLINE_SEPARATORS = re.compile(r"[,;|•·]")

#: Markdown emphasis around a whole heading line: ``**Навыки**``.
_BOLD_LINE = re.compile(r"^\*\*(?P<text>.+?)\*\*$")

#: Everything that carries no meaning when two spellings of one heading are
#: compared: case, surrounding whitespace, the markers, the trailing colon.
_HEADING_NOISE = re.compile(r"[\s:*#_\-–—]+")


@dataclass(frozen=True, slots=True)
class Section:
    """One heading and everything listed under it."""

    #: The heading as the document wrote it, markers stripped.
    heading: str
    #: The entries under it, in order, each already stripped of its bullet.
    items: tuple[str, ...]
    #: The body verbatim, for a checker that wants the prose rather than a count.
    body: str


def fold_heading(name: str) -> str:
    """The key two spellings of one heading must share to be the same heading.

    Case, punctuation and the markers all go, so ``## Навыки``, ``НАВЫКИ:`` and
    ``**Навыки**`` are one heading, and a rule written as ``навыки`` finds all
    three. Whitespace goes with them, so ``Ключевые навыки`` and
    ``Ключевые  навыки`` do not differ.
    """
    return _HEADING_NOISE.sub("", name).casefold()


def _heading_text(line: str) -> str | None:
    """The heading this line announces, or None if it is not a heading.

    Only the four spellings named in the module docstring, and only within
    :data:`MAX_HEADING_CHARS`. Order matters: the Markdown and bold forms are
    checked before the colon form so ``## Навыки:`` loses both markers.
    """
    text = line.strip()
    if not text or len(text) > MAX_HEADING_CHARS:
        return None

    if text.startswith("#"):
        stripped = text.lstrip("#").strip().rstrip(":").strip()
        return stripped or None

    bold = _BOLD_LINE.match(text)
    if bold is not None:
        stripped = bold.group("text").strip().rstrip(":").strip()
        return stripped or None

    if text.endswith(":"):
        return _label(text[:-1])

    letters = [character for character in text if character.isalpha()]
    if letters and all(character.isupper() for character in letters):
        return text.rstrip(":").strip() or None

    return None


def _label(text: str) -> str | None:
    """A colon-terminated line's heading, or None when it is a sentence."""
    stripped = text.strip()
    if not stripped or len(stripped.split()) > MAX_HEADING_WORDS:
        return None
    if _SENTENCE_MARKS.intersection(stripped):
        return None
    return stripped


def _split_heading_line(line: str) -> tuple[str, str] | None:
    """A heading and whatever the same line already says after it.

    ``Навыки: Python, Go, SQL`` is one line carrying both, and it is how a
    skills section is written more often than not. Without this the section
    would be found with an empty body and every count rule over it would report
    zero for a document that in fact lists twelve.
    """
    text = line.strip()
    if not text or text.startswith("#") or ":" not in text:
        return None
    head, _, tail = text.partition(":")
    tail = tail.strip()
    if not tail:
        return None
    heading = _heading_text(f"{head.strip()}:")
    return (heading, tail) if heading else None


def sections_of(document: str) -> tuple[Section, ...]:
    """Every section of the document, in the order it writes them.

    Text before the first heading belongs to no section and is dropped: it is
    the name and contact block of a CV, or the greeting of a letter, and it is
    not something a rule about a named section can be asking about.
    """
    lines = document.splitlines()
    found: list[Section] = []
    heading: str | None = None
    body: list[str] = []

    def close() -> None:
        if heading is not None:
            found.append(Section(heading=heading, items=items_of(body), body="\n".join(body)))

    for line in lines:
        inline = _split_heading_line(line)
        if inline is not None:
            close()
            heading, body = inline[0], [inline[1]]
            continue
        announced = _heading_text(line)
        if announced is not None:
            close()
            heading, body = announced, []
            continue
        if heading is not None:
            body.append(line)

    close()
    return tuple(found)


def find_section(document: str, name: str) -> Section | None:
    """The section this document calls ``name``, or None.

    The first match rather than a merge of all of them: a document with two
    sections of one name is malformed, and merging them would report a count
    nothing in the document actually shows.
    """
    wanted = fold_heading(name)
    if not wanted:
        return None
    for section in sections_of(document):
        if fold_heading(section.heading) == wanted:
            return section
    return None


def items_of(lines: list[str]) -> tuple[str, ...]:
    """The entries in a section body. See the module docstring for the rule.

    One item per non-empty line, except when the body is a single line holding
    separated values — then one item per value. The single-line test is what
    makes ``Python, Go, SQL`` three items and a one-sentence paragraph one.
    """
    filled = [line.strip() for line in lines if line.strip()]
    if not filled:
        return ()

    if len(filled) == 1:
        parts = [part.strip(" \t·") for part in _INLINE_SEPARATORS.split(filled[0])]
        values = [_BULLET.sub("", part).strip() for part in parts if part.strip()]
        if len(values) > 1:
            return tuple(values)

    return tuple(_BULLET.sub("", line).strip() for line in filled)
