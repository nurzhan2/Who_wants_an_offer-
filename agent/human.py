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

**The card is reduced to the console's codepage before it is shown, and the
digest is taken of the reduced text.** Added 2026-09-07, after a run was killed
by its own confirmation prompt: this is hh.KZ, the Kazakh letters ә ғ қ ң ө ұ ү
һ are ordinary in an employer's name and an emoji in a job title is not rare,
and none of them exist in cp1251. Printing one raised ``UnicodeEncodeError`` out
of :func:`confirm` — at the moment the owner was being asked to agree — and out
of the dry run before a single candidate could be read. See :meth:`Candidate
.render`, which also says why the digest binds the reduced text rather than the
original.

**Since 2026-09-07 this is the only thing between a queue file and somebody's
real application, and it got one new job rather than one less.** hh's «поменяйте
видимость резюме…» used to stop every send by itself, so the confirmation was
never the last line of defence in practice — nothing could get past that rule to
reach it. That rule is gone: it was a guess, hh accepts those applications, and
the measurement is in ``agent/state_page.py``. Nothing here was relaxed to make
room for it — the word is still typed in full, the default is still no, EOF is
still a refusal, a duplicate is still a cancellation, and :func:`mint` is still
called nowhere else. What changed is that hh's advice now has to *arrive*: it is
on the card under its own heading and once more for the whole batch, because an
owner about to send twelve applications should learn from this prompt, not from
the summary afterwards, that hh thinks all twelve will be limited.

**Since 2026-09-16 there is a second way to say yes, and it is also here.** The
dashboard shows the same card in a modal — built from the same queue item — and
records the owner's confirmation bound to a digest of that card.
:func:`accept_dashboard_confirmations` turns such a confirmation into a mandate
without asking again, and it gives up nothing the typed word gives: the mandate
binds the card digest the owner confirmed, the letter's own digest is compared
with the letter about to be typed, a candidate without a confirmation is left
alone, and a duplicate is still a cancellation. :func:`mint` is still called
from this module and nowhere else.

**One candidate per vacancy, checked here as well as upstream.** ``run.py``
deduplicates the batch; this refuses to mint a second mandate for a vacancy that
is already in the list, because :func:`mint` is the point where a duplicate
stops being a list entry and becomes a second application. Two mandates for one
vacancy are indistinguishable to ``agent.gate``, and correctly so — one
confirmation is one window — so the last place to catch it is the place that
mints them.
"""

import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, TextIO, final

from agent.letter import SafeLetter
from agent.mandate import SendMandate, digest, mint
from agent.queue import ATSCard, DashboardConfirmation
from agent.state_page import printable

#: Typed in full to proceed. Not "y": a single character is something a stuck
#: key produces, and this is the last gate before something irreversible.
CONFIRM_WORD: Final[str] = "отправляем"

#: The audit's one-word verdict, in the words the card prints. The backend's
#: vocabulary is English and stable (``app.schemas.ats.Overall``); the console is
#: read by the person whose account this is. An unknown value is printed as it
#: arrived rather than guessed at, so a backend that grows a fourth verdict
#: shows something true here instead of the wrong one of three.
ATS_VERDICTS: Final[dict[str, str]] = {
    "ok": "робот прочитает",
    "degraded": "робот прочитает не всё",
    "unreadable": "робот не прочитает",
}

#: What hh's resume-visibility sentence is filed under on one card. Says out
#: loud that it is not about this vacancy, because the line under it names a
#: vacancy («Чтобы откликнуться на эту вакансию…») and would otherwise read as
#: one job's problem.
VISIBILITY_HEADING: Final[str] = (
    "  hh: видимость резюме — это касается ВСЕХ откликов, не только этого:"
)


@final
@dataclass(frozen=True, slots=True)
class Candidate:
    """One application, as it will be sent and as the human will see it."""

    vacancy_id: str
    title: str
    company: str | None
    url: str
    letter: SafeLetter | None
    #: What hh itself has already said about applying to this vacancy, in hh's
    #: own words, carried over from the last run that got as far as the response
    #: form. Optional and last, so every existing call site keeps working.
    hh_warning: str | None = None
    #: Why this vacancy is in front of the owner at all: the match score on the
    #: project's 0-100 scale, and the sentence behind it. Both come from the
    #: queue untouched — this package neither computes nor edits them.
    score: float | None = None
    score_explanation: str | None = None
    #: hh's «поменяйте видимость резюме…», in hh's own words, when the journal
    #: carries it. A field of its own rather than a second line inside
    #: :attr:`hh_warning`, and the difference is the whole reason it was added
    #: (2026-09-07): that sentence is about the *resume*, so it is not a remark
    #: about this vacancy but a statement about every application in the list —
    #: including the ones no run has ever opened a form for. A card that files it
    #: under "what hh said about this job" is a card that reads it as small.
    hh_visibility: str | None = None
    #: The backend's ATS audit of this letter against this vacancy, carried
    #: from the queue untouched. This package does not audit anything; it shows
    #: the answer at the last moment it can still change a decision.
    ats: ATSCard | None = None

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

        **The match score is printed with its explanation, and its absence is
        printed too.** A number on its own is not a reason to write to an
        employer; the explanation is what the owner can check against the
        vacancy in front of them, and disagreeing with it is exactly what the
        drop list is for. When the queue carried no score, the card says so
        instead of staying quiet: silence there reads as "nobody scored this
        one" and as "this one scored badly" equally well, and the difference
        decides whether the list in front of the person is ordered by anything
        at all. Both fields are the backend's, carried through the queue
        untouched, so binding them into the digest also means a card approved
        against one explanation cannot be reused for a payload carrying
        another.

        **The letter is printed whole.** It used to be cut at 200 characters,
        which meant the human was asked to approve text they had not read, in
        an employer-facing message they did not write — the backend generates
        it. A long letter is a long prompt; that is the correct cost.

        **hh's own words are printed too, when there are any, and the two kinds
        are printed differently.** The response form is the only place hh says
        anything about an application, and it opens after this card has been
        answered — so whatever is shown here is what hh said last time, carried
        through the journal. Both kinds are quoted rather than summarised,
        because both name a setting or a requirement by a name the owner can
        search for and a paraphrase sends them looking for one hh does not use.

        They are separated because they are not the same size of statement
        (2026-09-07). «Такой отклик может получить отказ» is about this vacancy:
        it names one requirement this employer set that the resume does not meet.
        «Чтобы откликнуться на эту вакансию, поменяйте видимость резюме…» is
        about the resume, so hh shows it on every vacancy while that setting
        stands, and reading it as a note about this one job is reading it as
        twelve small remarks instead of one large one. It used to stop the
        application outright, which made the distinction moot; now that the
        application goes out, the card is where the owner finds out that hh
        thinks it will not be seen — so it gets its own heading, saying that it
        is true of every application here, and :func:`confirm` says the same
        thing once for the batch.

        Being on the card means the mandate binds both, so a decision made while
        reading hh's objection cannot be reused for a payload that no longer
        carries it.

        **Everything printed here is reduced to cp1251, which is what a Russian
        Windows console encodes to.** Not only hh's warnings, which arrive
        already reduced from ``agent.state_page.read_form_warnings`` — the
        title, the employer and the letter too, and that is the correction
        (2026-09-07). Every one of those is text this program did not write:
        the title and the employer are hh's, and this is hh.KZ, where the
        Kazakh letters ә ғ қ ң ө ұ ү һ do not exist in cp1251 and are ordinary
        in a company name; the letter is the backend's. One such character
        raised ``UnicodeEncodeError`` out of :func:`confirm` itself — at the
        moment the owner was being asked to agree — and out of the dry run
        before a single candidate could be read.

        **The reduction is announced when it changes anything.** Characters
        outside the codepage become ``?``, and on the letter that is a
        difference between the text on screen and the text that will be sent:
        the mandate carries the letter unreduced, because an employer must
        receive what the owner wrote and not what a codepage survived. A line
        saying so is cheaper than a person wondering what the question marks
        were, and far cheaper than a card that quietly misrepresents the
        payload.

        **The digest is taken of the reduced card**, because the digest is
        supposed to bind what the human read, and what they read is what the
        console could print.
        """
        lines = [
            f"{self.title} — {self.company or 'без компании'}",
            f"  вакансия {self.vacancy_id}",
            f"  {self.url}",
            *self._score_lines(),
            *self._ats_lines(),
        ]
        # Truthiness rather than ``is not None``: an empty string would print a
        # heading with nothing under it, which reads as a warning nobody wrote.
        if self.hh_visibility:
            lines.append(VISIBILITY_HEADING)
            lines.extend(f"  | {line}" for line in self.hh_visibility.splitlines())
        if self.hh_warning:
            lines.append("  hh уже предупреждал об этой вакансии:")
            lines.extend(f"  | {line}" for line in self.hh_warning.splitlines())
        if self.letter is None:
            lines.append("  без сопроводительного письма")
        else:
            body = "\n".join(f"  | {line}" for line in self.letter.text.splitlines())
            lines.append(f"  письмо ({len(self.letter)} симв.):")
            lines.append(body)
        card = "\n".join(lines)
        shown = printable(card)
        if shown != card:
            shown = f"{shown}\n  (часть символов не в кодировке консоли, показаны как «?»)"
        return shown

    def _score_lines(self) -> list[str]:
        """The match score block: the number, then the reasoning under it.

        One decimal place because the scale is 0-100 and the second digit is
        noise a person will read as precision. The explanation is quoted with
        the same ``|`` gutter the letter and hh's warning use, so everything on
        the card that was written elsewhere looks like it was.
        """
        if self.score is None:
            return ["  соответствие: не посчитано"]
        lines = [f"  соответствие: {self.score:.1f} из 100"]
        if self.score_explanation:
            lines.extend(f"  | {line}" for line in self.score_explanation.splitlines())
        return lines

    def _ats_lines(self) -> list[str]:
        """What a machine reading this letter will get out of it.

        The last place the ATS report is shown, and the only one where the next
        click sends an application. Printed on the card rather than left to the
        dashboard for the same reason the score is: a person is being asked to
        approve this letter for this vacancy, and «названо 3 из 9 требований» is
        something they can act on by dropping the item and regenerating it.

        Two lists, never merged. The named one is requirements the owner *has*
        and this letter does not mention — that is a letter to rewrite. The
        counted one is requirements nobody holds, and it stays a number: naming
        them here, seconds before sending, would read as a list of things to
        claim, which is the one thing this card must never suggest.

        Absence is printed too. A letter nobody audited and a letter that passed
        must not look the same to somebody approving an application.
        """
        card = self.ats
        if card is None:
            return ["  проверка ATS: не выполнялась"]

        lines = [
            f"  проверка ATS: {ATS_VERDICTS.get(card.overall, card.overall)}"
            + (f", {card.score:.0f} из 100" if card.score is not None else "")
        ]
        lines.extend(f"  | не прочитает: {title}" for title in card.critical)
        if card.requirements_total:
            lines.append(
                f"  | требований вакансии названо дословно: "
                f"{card.requirements_present} из {card.requirements_total}"
            )
        if card.unstated:
            lines.append("  | есть в профиле, но не названо в письме: " + ", ".join(card.unstated))
        if card.absent:
            lines.append(f"  | требований, которых нет в профиле: {card.absent}")
        return lines


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

    A batch naming one vacancy twice is refused before anything is printed. It
    is a defect in the caller rather than a decision of the human's, and it is
    raised as a cancellation because that is what it must produce: nothing sent,
    a sentence on the screen, no traceback over a run that has not opened a
    browser yet. Nothing downstream can catch it — each copy would get its own
    mandate, and the gate arms one window per mandate — so it stops here.
    """
    out = stream_out or sys.stdout
    src = stream_in or sys.stdin

    if not candidates:
        return []

    repeated = sorted(
        vacancy_id
        for vacancy_id, times in Counter(c.vacancy_id for c in candidates).items()
        if times > 1
    )
    if repeated:
        raise CancelledError(
            f"одна и та же вакансия в списке дважды: {', '.join(repeated)}. "
            "Подтверждение не запрашивалось, ничего не отправлено."
        )

    print(f"\nК отправке {len(candidates)} откликов:\n", file=out)
    for index, candidate in enumerate(candidates, start=1):
        print(f"[{index}] {candidate.render()}\n", file=out)

    _say_it_once_for_the_batch(candidates, out)
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


def accept_dashboard_confirmations(
    candidates: Sequence[Candidate],
    confirmations: Mapping[str, DashboardConfirmation],
    *,
    stream_out: TextIO | None = None,
) -> list[SendMandate]:
    """Mint one mandate per candidate the owner confirmed on the dashboard.

    Nothing is read from the terminal: the question was asked and answered in
    the browser, on the same card. What is checked here is that the answer
    still applies to what will be sent:

    * a candidate with no confirmation gets no mandate;
    * the letter this process is about to type must have the digest the owner
      confirmed — a letter the safety check changed, or one regenerated after
      the backend looked, gets no mandate;
    * the mandate's ``form_digest`` is the card digest the owner confirmed, so
      the consent is bound to that card exactly as a typed word is bound to
      the printed one.

    Each card is still printed, with the moment it was confirmed, so the window
    shows what is about to go out.
    """
    out = stream_out or sys.stdout
    repeated = sorted(
        vacancy_id
        for vacancy_id, times in Counter(c.vacancy_id for c in candidates).items()
        if times > 1
    )
    if repeated:
        raise CancelledError(
            f"одна и та же вакансия в списке дважды: {', '.join(repeated)}. Ничего не отправлено."
        )

    mandates: list[SendMandate] = []
    for candidate in candidates:
        confirmation = confirmations.get(candidate.vacancy_id)
        if confirmation is None:
            print(
                f"  {candidate.vacancy_id}: на дашборде не подтверждена — не отправляю.",
                file=out,
            )
            continue
        letter = None if candidate.letter is None else candidate.letter.text
        if digest(letter) != confirmation.letter_digest:
            print(
                f"  {candidate.vacancy_id}: письмо не то, которое подтверждали на дашборде — "
                "не отправляю. Откройте вакансию и подтвердите заново.",
                file=out,
            )
            continue
        print(
            f"\n{candidate.render()}\n  подтверждено на дашборде: {confirmation.confirmed_at}",
            file=out,
        )
        mandates.append(
            mint(
                vacancy_id=candidate.vacancy_id,
                url=candidate.url,
                letter=letter,
                form_digest=confirmation.card_digest,
            )
        )
    _say_it_once_for_the_batch([c for c in candidates if c.vacancy_id in confirmations], out)
    return mandates


def _say_it_once_for_the_batch(candidates: Sequence[Candidate], out: TextIO) -> None:
    """Repeat hh's resume-visibility sentence once, for the whole list.

    Printed between the cards and the drop prompt, which is the last thing read
    before the answer. It is a repetition on purpose: the sentence is already on
    every card that carries it, and the point being made here is the one no
    single card can make — that this is one statement about the resume rather
    than a coincidence across N vacancies, and that it is equally true of the
    ones whose cards say nothing, because a card only knows what hh happened to
    say the last time a form was open for that vacancy.

    **It adds nothing to the digest and it must not.** Every word here is either
    fixed text or a line already printed inside a card, and the cards are what
    :func:`mint` binds. A summary carrying anything of its own would be text the
    human read and the mandate did not cover, which is the failure this module
    exists to prevent, in the one function that is supposed to prevent it.
    """
    carrying = [candidate for candidate in candidates if candidate.hh_visibility]
    if not carrying:
        return
    print("ВНИМАНИЕ. hh пишет про видимость резюме:", file=out)
    # Through ``printable`` like everything else quoted out of hh. It reaches
    # here from the journal rather than straight from the page, and a column
    # somebody else may have written into is not a reason to trust the codepage.
    for line in printable(carrying[0].hh_visibility or "").splitlines():
        print(f"  | {line}", file=out)
    print(
        "Это про само резюме, а не про вакансию: hh сказал это про "
        f"{len(carrying)} из {len(candidates)} в списке,\n"
        "и это верно для всех остальных тоже. Отклики уйдут — hh их принимает, —\n"
        "но hh считает, что работодатель может их не увидеть.\n",
        file=out,
    )


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
