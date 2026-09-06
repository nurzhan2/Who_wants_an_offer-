"""Where a person says yes, and the only place a mandate is minted.

The brief's third boundary is that nothing is sent without a human's
confirmation, and that a batch confirmation is the minimum: «вот 12 вакансий,
отправляем?» with the ability to drop any of them. That is what this module is.

Two things about it are less obvious than they look.

**The confirmation is bound to what was shown, not to the item.** The human sees
a rendered card — title, employer, the first lines of the letter, the flags —
and the digest of exactly that text goes into the mandate. If the queue is
refetched, if the letter is regenerated, if anything about the payload changes
between the confirmation and the click, the digest no longer matches and the
submitter refuses. Confirmation is consent to a specific thing, and this is what
makes that literal rather than aspirational.

**Dropping is by exception, and the default answer is no.** The prompt asks
which to *drop*, and then asks for a word to be typed to proceed. Pressing
Enter, hitting Ctrl-C, closing the terminal, an EOF from a redirected stdin —
every one of those results in nothing being sent. There is no ``--yes``, no
``--all``, no environment variable that pre-answers this: the brief forbids
«способы убрать человека из цикла», and an interactive prompt that a flag can
satisfy is a flag.

The word to type is Russian and specific rather than "y", so that it cannot be
produced by a stray keypress or by a terminal replaying a buffer.
"""

import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, TextIO, final

from agent.letter import SafeLetter
from agent.mandate import SendMandate, digest, mint

#: Typed in full to proceed. Not "y": a single character is something a stuck
#: key produces, and this is the last gate before something irreversible.
CONFIRM_WORD: Final[str] = "отправляем"


@final
@dataclass(frozen=True, slots=True)
class Candidate:
    """One application, as it will be sent and as the human will see it."""

    vacancy_id: str
    title: str
    company: str | None
    url: str
    letter: SafeLetter | None

    def render(self) -> str:
        """Exactly what the human is shown. The mandate is bound to this text.

        Everything that affects what gets sent appears here: if a field is not
        rendered, a change to it cannot invalidate the confirmation, so anything
        added to this class belongs in this method too. That rule is the reason
        for both of the things below that look like clutter.

        **The vacancy id is printed.** It is the key everything downstream acts
        on — the journal row, the gate's comparison, the page the submitter
        opens — and it was the one field the card did not show. A queue item
        whose url and id disagree would have been confirmed on the strength of a
        title and a link that had nothing to do with the application actually
        sent.

        **The letter is printed whole.** It used to be cut at 200 characters,
        which meant the human was asked to approve text they had not read, in
        an employer-facing message they did not write — the backend generates
        it. A long letter is a long prompt; that is the correct cost.

        Everything printed here stays inside cp1251, which is what a Russian
        Windows console encodes to. A box-drawing character in the letter's
        margin raised ``UnicodeEncodeError`` out of the dry run before a single
        candidate could be read.
        """
        lines = [
            f"{self.title} — {self.company or 'без компании'}",
            f"  вакансия {self.vacancy_id}",
            f"  {self.url}",
        ]
        if self.letter is None:
            lines.append("  без сопроводительного письма")
        else:
            body = "\n".join(f"  | {line}" for line in self.letter.text.splitlines())
            lines.append(f"  письмо ({len(self.letter)} симв.):")
            lines.append(body)
        return "\n".join(lines)


@final
class CancelledError(Exception):
    """The human did not confirm. Not an error — the expected answer to a prompt."""


def confirm(
    candidates: Sequence[Candidate],
    *,
    stream_in: TextIO | None = None,
    stream_out: TextIO | None = None,
) -> list[SendMandate]:
    """Show the batch, take the drops, and mint one mandate per survivor.

    The streams are arguments so the whole exchange can be tested without a
    terminal; in production they are stdin and stdout. A test that had to drive
    a real TTY would be a test nobody runs, and this is the function that must
    never regress.
    """
    out = stream_out or sys.stdout
    src = stream_in or sys.stdin

    if not candidates:
        return []

    print(f"\nК отправке {len(candidates)} откликов:\n", file=out)
    for index, candidate in enumerate(candidates, start=1):
        print(f"[{index}] {candidate.render()}\n", file=out)

    print(
        "Введите номера, которые НЕ надо отправлять, через пробел "
        "(пустая строка — отправляем все).",
        file=out,
    )
    dropped = _read_drops(src, out, len(candidates))
    kept = [c for index, c in enumerate(candidates, start=1) if index not in dropped]
    if not kept:
        raise CancelledError("не осталось ни одного отклика")

    print(f"\nОтправляем {len(kept)} из {len(candidates)}.", file=out)
    print(f"Чтобы подтвердить, введите слово «{CONFIRM_WORD}»: ", end="", file=out)
    out.flush()
    answer = _read_line(src)
    if answer.strip().casefold() != CONFIRM_WORD:
        raise CancelledError("подтверждение не получено")

    # One mandate per surviving candidate, each bound to the exact text that was
    # printed above. This is the only call to mint() in the package.
    return [
        mint(
            vacancy_id=candidate.vacancy_id,
            url=candidate.url,
            letter=None if candidate.letter is None else candidate.letter.text,
            form_digest=digest(candidate.render()),
        )
        for candidate in kept
    ]


def _read_line(src: TextIO) -> str:
    """One line, treating end-of-input as a refusal.

    A closed stdin means nobody is there, and nobody being there is the one
    situation in which this program must do nothing at all.
    """
    line = src.readline()
    if line == "":
        raise CancelledError("ввод закрыт — подтверждать некому")
    return line


def _read_drops(src: TextIO, out: TextIO, count: int) -> set[int]:
    """The numbers the human wants removed, re-asked until they make sense."""
    while True:
        print("> ", end="", file=out)
        out.flush()
        raw = _read_line(src).strip()
        if not raw:
            return set()
        parts = raw.replace(",", " ").split()
        # ASCII digits only. `str.isdigit()` is True for superscripts like «²»,
        # which `int()` then refuses — and the ValueError escaped this loop, the
        # confirmation and main(), so a typo in the drop line ended the run in a
        # traceback instead of the re-ask this function promises.
        if all(part.isascii() and part.isdigit() and 1 <= int(part) <= count for part in parts):
            return {int(part) for part in parts}
        print(f"Нужны номера от 1 до {count}, через пробел.", file=out)
