"""Letters that got an answer, pasted into the next prompt as examples.

**This is few-shot prompting, and that is all it is.** Nothing here trains
anything. No weights are updated, no model is fine-tuned, no adapter is stored;
the model does not learn between calls and is exactly as good on the hundredth
letter as on the first. What happens is narrower and worth stating in plain
words: a letter that was sent under the owner's name and got a reply is copied
into the text of the next prompt, the model sees it for the length of that one
call, and it is gone. Anything written about this feature — a docstring, a
commit message, a line in the dashboard — has to say "examples in the prompt"
and not "the system learns", because a reader who believes the second one will
expect the letters to improve on their own, and they will not.

The loop this closes is real even so::

    letter -> application -> outcome -> the next letter is written having seen
    the ones the employer answered

The outcome is the only new ingredient, and it is the scarce one.

**There is nothing to show, and the exact nothing is worth writing down.**
Measured against the live database on 2026-09-07, after migration
``0008_application_send_record``: three tracker rows, two of them sent, one of
those answered — an interview — and that one carries no ``sent_letter``,
because it predates the column that records what was typed. So the pool this
module can build today is *empty*, and it is empty for a reason a count can
state rather than for a shrug: ``sent 2, answered 1, text unknown 1``. The
project's shorthand for this is "two outcomes on the whole account"; the
narrower true statement is two applications, one answer, and no example.

The ordinary case, for a long time, is therefore *no examples at all*, and that
is the case this module is built around rather than the case it apologises for:

* with nothing to show, :func:`select` returns nothing, :func:`block` renders
  the empty string, and the prompt is byte-for-byte the prompt that was being
  sent before this module existed. No hedging sentence, no "we have little data
  so far", nothing for the model to write around. A letter for the first
  vacancy of the first day is exactly as good as it was yesterday;
* nothing is ever synthesised. There is no invented example, no "here is what a
  good letter looks like" written by us and presented as one that worked;
* an unsent letter is never an example, and neither is a sent one that has had
  no answer yet. Silence on hh is the default state and it arrives long before
  the answer does; counting it as success is the same lie as inventing one,
  taken one step round. The rule is enforced on the column that can prove it:
  an example's text is ``application.sent_letter``, which only the process that
  typed it into hh's form ever writes, and never ``application.cover_letter``,
  which this package overwrites on every regeneration. Both hold "a letter for
  a vacancy that got an interview"; only the first holds *the letter the
  employer read*. A ``--force`` run over a vacancy that has already been sent to
  is the reachable case that makes them differ, and it is a path in this very
  module;
* :class:`OutcomeEvidence` reports the counts and deliberately computes no rate.
  Two data points cannot carry a percentage, and a percentage is what a
  dashboard will draw a line through. See :data:`MIN_FOR_A_TREND`.

**The boundaries did not move because there is feedback now.** A letter that got
an interview is not licensed to have invented experience, and an example
carrying a link teaches the model to write one. So every example goes through
:func:`app.letters.guard.find_problems` — the same function that judges the
model's output, against this vacancy's own ceiling — before it can be shown, and
one that fails is *dropped*, never repaired and never truncated. The dropping is
counted and logged rather than done silently: the reachable case is the owner
hand-editing a letter to add their GitHub, sending it, and getting an interview
for it, which makes the best-performing letter on the account the one that must
never be copied.

**Where the outcome is read from, now that there is more than one place.**
``application`` gained a send record on 2026-09-07 (migration
``0008_application_send_record``), and with it two columns that look like
outcomes. ``sent_at``/``sent_letter``/``vacancy_key_skills`` are used here and
are exactly what this needed: the moment, the text, and the requirement list as
the employer stated it *that day* rather than as a re-crawl has since rewritten
it. ``hh_last_state`` is deliberately **not** used. It is hh's own vocabulary,
it is open, and one value of it has ever been observed (``DISCARD``); deciding
here which of hh's states mean "the employer answered" would be inventing a
mapping from a single observation, which is the same class of mistake as
inventing a rate from two applications. So the outcome remains
``application.status`` — the person's own kanban, moved by the person who read
the reply — and :func:`grade_of` stays the single point of contact with that
choice. If hh's states are ever mapped, they belong in one dict beside
:data:`POSITIVE_STATUSES`, and nowhere else.

What the guard cannot check is the prose of an old letter for invented
experience; no string test can, for the reason
:func:`app.letters.generator.inspect_draft` sets out about its own output. Two
things bound it instead. The examples are restricted to letters written for
*this profile*, so the evidence base behind them is the evidence base now; and
the prompt block says, in the imperative, that no claim may be copied out of an
example — while ``inspect_draft`` goes on checking the model's declared skills
against the profile exactly as before, so a claim copied out of an example is
rejected by the machinery that was already there.
"""

# ruff: noqa: RUF001 - the labels below are Russian prose, which is what the
# homoglyph guard cannot tell from a homoglyph attack. Same exemption the
# project grants app/sources/hh.py and agent/*.py in pyproject.toml, declared
# here because pyproject.toml belongs to another change.

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from app.core.logging import get_logger
from app.db.enums import ApplicationStatus
from app.letters.context import Facts, LetterContext, fold
from app.letters.guard import LetterProblem, find_problems

logger = get_logger(__name__)

#: How many examples one prompt may carry. Three is the ceiling because a fourth
#: letter costs a heavy call's worth of tokens to say what the third already
#: said, and because a prompt whose examples outweigh its instructions produces
#: pastiche of the examples.
MAX_EXAMPLES = 3

#: How much of the two requirement lists must be shared before an example counts
#: as being for a similar vacancy. Jaccard: shared skills over the union, so two
#: five-item lists sharing one item score 0.11 and pass, while a twenty-item list
#: sharing one item with a ten-item list scores 0.03 and does not. The number is
#: a judgement about what "similar" is worth, not a measurement — with no
#: showable letters on the account nothing here has been tuned against anything,
#: and pretending otherwise would be the same dishonesty as a rate over two
#: points.
MIN_SIMILARITY = 0.1

#: Ceiling on the whole rendered block. A letter runs to a couple of thousand
#: characters and three of them plus the vacancy description is already the
#: expensive part of the call. Examples that do not fit are dropped from the
#: back, which is the worst-ranked end.
MAX_BLOCK_CHARS = 9_000

#: Below this many answered applications, no proportion computed from them means
#: anything, and whatever displays this must say so. The number is a judgement:
#: at one answered application the difference between a good letter and a lucky
#: one is not visible, at twenty it starts to be. What matters is not the value
#: but
#: that the flag exists and defaults to "not enough" — the failure this prevents
#: is a dashboard drawing "50% reply rate" through two data points.
MIN_FOR_A_TREND = 20

#: Delimiters around one example letter inside the prompt. A letter containing
#: one of these is dropped rather than edited: it is text a person sent under
#: their own name, and quietly rewriting it would make the example a claim about
#: something that was never sent.
LETTER_OPEN = "[EXAMPLE LETTER BEGIN]"
LETTER_CLOSE = "[EXAMPLE LETTER END]"


class ExampleGrade(StrEnum):
    """How far an application got, for the letters good enough to show.

    A vocabulary of its own rather than :class:`app.db.enums.ApplicationStatus`
    directly, because this is the one place that has to survive the outcome
    being recorded somewhere else. It is derived from ``application.status`` by
    :func:`grade_of`, and the module docstring says why that column and not
    ``application.hh_last_state``, which now exists beside it. If hh's states
    are ever mapped to these grades, that function and the query in
    :mod:`app.letters.store` are the whole of the change.
    """

    #: The employer moved it out of the pile: hh's "рассмотрение".
    REPLIED = "replied"
    INTERVIEW = "interview"
    OFFER = "offer"


#: Best last, so ``max`` and a descending sort both read the right way round.
GRADE_RANK: dict[ExampleGrade, int] = {
    ExampleGrade.REPLIED: 1,
    ExampleGrade.INTERVIEW: 2,
    ExampleGrade.OFFER: 3,
}

#: What the model is told the outcome was. English, like the rest of the prompt.
GRADE_ENGLISH: dict[ExampleGrade, str] = {
    ExampleGrade.REPLIED: "the employer replied and opened the application",
    ExampleGrade.INTERVIEW: "it led to an interview",
    ExampleGrade.OFFER: "it led to an offer",
}

#: What a person reads. Everything here stays inside cp1251 — the console this
#: runs on encodes cp1251, and a character outside it raises at print time
#: rather than at test time.
GRADE_RUSSIAN: dict[ExampleGrade, str] = {
    ExampleGrade.REPLIED: "ответили",
    ExampleGrade.INTERVIEW: "позвали на интервью",
    ExampleGrade.OFFER: "оффер",
}

#: Statuses that mean an employer engaged, and the grade each one earns. These
#: are the only letters that may be shown as examples.
#:
#: ``APPLIED`` is pointedly absent: it means the letter was sent and nothing has
#: come back, which is what every application looks like for its first fortnight.
#: ``SAVED`` is absent because nothing was sent at all. ``REJECTED`` is absent
#: from *here* and present in :data:`ANSWERED_STATUSES`: it is a real outcome and
#: belongs in the count of what is known, and it is not a letter to imitate.
POSITIVE_STATUSES: dict[ApplicationStatus, ExampleGrade] = {
    ApplicationStatus.SCREENING: ExampleGrade.REPLIED,
    ApplicationStatus.INTERVIEW: ExampleGrade.INTERVIEW,
    ApplicationStatus.OFFER: ExampleGrade.OFFER,
}

#: Everything that counts as the employer having answered at all, good or bad.
#: This is the denominator of "how much do we actually know", and the number
#: :data:`MIN_FOR_A_TREND` is compared against.
ANSWERED_STATUSES: frozenset[ApplicationStatus] = frozenset(
    {*POSITIVE_STATUSES, ApplicationStatus.REJECTED}
)

#: What a person is told when the pool is too small to support a conclusion.
NOT_ENOUGH_RU = "данных пока мало"


def grade_of(status: ApplicationStatus) -> ExampleGrade | None:
    """The grade this application earns, or None if it is not an example.

    The single point of contact with how the outcome happens to be stored.
    """
    return POSITIVE_STATUSES.get(status)


class LetterExample(Facts):
    """One past letter, its vacancy, and how far it got.

    ``text`` is ``application.sent_letter``: the exact string the agent typed
    into hh's form, written once by the only process that knows it. It is never
    edited here; an example that cannot be shown as it stands is not shown.
    """

    vacancy_id: UUID
    title: str
    #: The requirement list of the vacancy it was written for, as the posting
    #: stated it on the day it was sent to. What "similar" is computed on.
    key_skills: tuple[str, ...] = ()
    text: str
    grade: ExampleGrade
    #: When it went out, from ``application.sent_at`` — the machine's record,
    #: not the person's editable ``applied_at``. Used only as a tiebreaker, and
    #: None sorts last rather than crashing the sort.
    sent_at: datetime | None = None


class ChosenExample(Facts):
    """An example that made it into a prompt, and how similar its vacancy was."""

    example: LetterExample
    #: Jaccard overlap of the two requirement lists, 0.0 to 1.0.
    similarity: float


class OutcomeEvidence(Facts):
    """How much is actually known, in counts and never in a rate.

    Every field is something that happened and was recorded. There is
    deliberately **no** ``reply_rate`` and no ``success_rate`` property: the
    moment one exists something will render it, and a percentage computed over
    two applications is a statistic that does not exist. :attr:`is_enough` is
    the flag a caller is supposed to consult, and it is False until there is
    enough to say anything at all.
    """

    #: Applications that actually went out under the owner's name.
    sent: int = 0
    #: Of those, the ones the employer answered either way. The denominator.
    answered: int = 0
    #: Of those, the ones the employer engaged with. Candidate examples.
    positive: int = 0
    #: Of the positives, the ones whose sent text was never recorded, and which
    #: therefore cannot be shown as examples however well they did. A separate
    #: count rather than a silent shortfall: it is the difference between "no
    #: letter has ever worked" and "one did and nobody kept it", and only the
    #: second is fixable. Every row that predates the send record is one of
    #: these, including the interview on this account.
    text_unknown: int = 0
    #: Of the positives, the ones for a vacancy similar enough to this one.
    similar: int = 0
    #: Of the similar ones, the ones the guard refused. Counted rather than
    #: dropped quietly: this is where "the owner added a link by hand and it
    #: worked" shows up.
    blocked: int = 0
    #: How many actually went into the prompt.
    used: int = 0

    @property
    def is_enough(self) -> bool:
        """Whether the answered outcomes could support any conclusion at all.

        False for a long time, on purpose. See :data:`MIN_FOR_A_TREND`.
        """
        return self.answered >= MIN_FOR_A_TREND


class ExamplePool(Facts):
    """Everything one run needs to pick examples, read from the database once.

    A batch asks the same question for every vacancy in it, so the query happens
    once and the per-vacancy work — similarity, the guard, the ranking — is
    Python over a list that is two entries long at most.

    ``candidates`` are the showable letters: positive outcome, text recorded,
    not yet filtered by similarity. ``counts`` are the account-wide figures
    :func:`app.letters.store.load_examples` measures, including the positives
    that are *not* in ``candidates`` because their text was never kept.
    """

    candidates: tuple[LetterExample, ...] = ()
    counts: OutcomeEvidence = OutcomeEvidence()


def summary_ru(evidence: OutcomeEvidence) -> str:
    """One line for a person: what is known, in counts, with no proportion.

    Written here rather than in the dashboard or the script because the honesty
    rule belongs next to the data: whatever displays this has to be able to say
    «данных пока мало» and mean it, and the safest way to make that happen is
    for the sentence to arrive already saying it. cp1251-safe, like everything
    printed.

    It reports the state of the evidence and not the state of one run, so
    :attr:`OutcomeEvidence.used` is absent on purpose. "Two examples went into
    this prompt" is a fact about a prompt; read next to "sent 2, answered 1" it
    invites the arithmetic that is exactly what nobody may do with two points.
    A caller that wants the per-run number has it on the field.
    """
    known = f"отправлено {evidence.sent}, с ответом {evidence.answered}"
    if evidence.text_unknown:
        known = f"{known}, без сохранённого текста {evidence.text_unknown}"
    if evidence.is_enough:
        return known
    return f"{NOT_ENOUGH_RU}: {known}"


def similarity(left: tuple[str, ...], right: tuple[str, ...]) -> float:
    """How alike two requirement lists are: shared skills over the union.

    **Why the requirement lists and not anything else.** The letter is built on
    the intersection between a vacancy's ``key_skills`` and the profile — that
    is what :mod:`app.letters.context` computes and what the whole prompt is
    arranged around. So two vacancies asking for the same things produce letters
    that make the same claims, and a letter that got an answer for one is a
    demonstration of how to say *this particular thing* well, not merely of tone.
    Matching on anything else would pick examples that are similar in a way the
    letter does not use.

    It is also structured data. hh ships ``keySkills`` as a list, so this is a
    set intersection with nothing guessed out of prose, no second model call and
    no cost — the same property the overlap itself leans on.

    The alternatives, and why not:

    * **the description embedding.** There is a pgvector column and it is
      populated, but posting descriptions are mostly benefits and boilerplate,
      so the nearest neighbour of a Python vacancy is whichever posting has the
      most similar corporate voice. It would also tie example selection to the
      state of the embedding backfill, and cost a query per letter.
    * **the job title.** Titles on this market are marketing
      ("Ведущий разработчик (Python/Go) в дружную команду"), and two identical
      titles routinely mean two different jobs.
    * **the same employer.** Precise and almost never true; it would fire on
      nothing.

    Jaccard rather than the overlap coefficient, because a twenty-requirement
    posting that happens to include Python is not similar to a three-requirement
    Python job, and the overlap coefficient would score that a perfect 1.0.

    Folded through :func:`app.letters.context.fold`, so ``Node.js`` and
    ``nodejs`` meet the same way they do in the overlap itself.
    """
    first = {key for key in (fold(name) for name in left) if key}
    second = {key for key in (fold(name) for name in right) if key}
    if not first or not second:
        # A vacancy with no requirement list — arbeitnow, remotive, an hh row
        # from before the skills are normalised — is not similar to anything,
        # because nothing here can tell whether it is. Saying "no examples" is
        # the honest answer; picking the most recent one anyway would be
        # inventing a resemblance.
        return 0.0
    return len(first & second) / len(first | second)


def problems_with(example: LetterExample, *, max_length: int) -> list[LetterProblem]:
    """Everything that disqualifies this example, by the rules for output.

    The same :func:`app.letters.guard.find_problems` the model's own answer
    faces, against *this* vacancy's ceiling rather than the example's: an
    example twice as long as the target vacancy accepts teaches a letter that
    will be refused.
    """
    return find_problems(example.text, max_length=max_length)


def select(
    pool: ExamplePool,
    context: LetterContext,
    *,
    limit: int = MAX_EXAMPLES,
) -> tuple[tuple[ChosenExample, ...], OutcomeEvidence]:
    """The examples this vacancy gets, and an honest account of the pool.

    Ordered by outcome first, then by how alike the two requirement lists are,
    then by recency: the brief is "recent letters with the best outcome for
    similar vacancies", and a worse outcome for a closer vacancy is still the
    weaker evidence — an offer is a fact about the letter, similarity is a guess
    about the vacancy.

    ``pool.counts`` carries the figures only a query can know — how many
    applications went out, how many were answered, how many of those were
    positive, and how many positives had no recorded text. This fills in the
    three that depend on the vacancy in hand, and overwrites none of the
    others, so what the caller reports is what actually happened rather than
    what was asked for.
    """
    scored: list[tuple[int, float, float, ChosenExample]] = []
    similar = 0
    blocked = 0

    for candidate in pool.candidates:
        if candidate.vacancy_id == context.vacancy.vacancy_id:
            # A letter is not its own example. Reachable with --force on a
            # vacancy whose first letter already got an answer.
            continue
        score = similarity(candidate.key_skills, context.vacancy.key_skills)
        if score < MIN_SIMILARITY:
            continue
        similar += 1

        problems = problems_with(candidate, max_length=context.vacancy.letter_max_length)
        if problems or LETTER_OPEN in candidate.text or LETTER_CLOSE in candidate.text:
            blocked += 1
            logger.warning(
                "letters.examples.blocked",
                vacancy_id=str(candidate.vacancy_id),
                grade=candidate.grade.value,
                problems=[problem.value for problem in problems],
                characters=len(candidate.text),
            )
            continue

        scored.append(
            (
                GRADE_RANK[candidate.grade],
                score,
                candidate.sent_at.timestamp() if candidate.sent_at else 0.0,
                ChosenExample(example=candidate, similarity=score),
            )
        )

    scored.sort(key=lambda row: (row[0], row[1], row[2]), reverse=True)
    chosen = _within_budget([row[3] for row in scored[:limit]])
    return chosen, pool.counts.model_copy(
        update={"similar": similar, "blocked": blocked, "used": len(chosen)}
    )


def _within_budget(chosen: list[ChosenExample]) -> tuple[ChosenExample, ...]:
    """Drop from the worst-ranked end until the block fits :data:`MAX_BLOCK_CHARS`."""
    kept: list[ChosenExample] = []
    spent = 0
    for item in chosen:
        cost = len(item.example.text)
        if spent + cost > MAX_BLOCK_CHARS:
            logger.info(
                "letters.examples.over_budget",
                vacancy_id=str(item.example.vacancy_id),
                characters=cost,
            )
            break
        kept.append(item)
        spent += cost
    return tuple(kept)


def block(chosen: tuple[ChosenExample, ...]) -> str:
    """The prompt section, or the empty string when there is nothing to show.

    The empty string is the important half. It is what keeps a prompt with no
    examples identical to the prompt this project sent before the feature
    existed — no dangling heading, no sentence explaining that there are no
    examples, nothing that changes what the model writes for the letters that
    get none, which today is all of them. The template carries this variable
    with no blank line after it and the block supplies its own, so an empty
    render is byte-for-byte the old prompt rather than merely a similar one; a
    test asserts that.
    """
    if not chosen:
        return ""

    lines = [
        "## Letters from this candidate that got an answer",
        "",
        "The letters below were written for this same candidate for other "
        "vacancies, sent under their name, and answered by the employer. They "
        "are here as examples of structure, length and tone. That is the whole "
        "of what they are for.",
        "",
        "**Do not copy a claim, a number, a technology or an employer's name out "
        'of them.** The evidence about this candidate is in "The candidate" and '
        '"The overlap, already computed" above, and nowhere else. An example was '
        "written for a different vacancy with a different requirement list; a "
        "sentence that was true there is not evidence here, and repeating one is "
        "inventing experience. Answer this vacancy's requirement list, not the "
        "one the example answered.",
        "",
    ]
    for number, item in enumerate(chosen, start=1):
        lines.extend(_one(number, item))
    return "\n".join(lines).rstrip() + "\n\n"


def _one(number: int, item: ChosenExample) -> list[str]:
    """One rendered example: what happened to it, what it answered, its text."""
    example = item.example
    asked = ", ".join(example.key_skills) if example.key_skills else "(not recorded)"
    return [
        f"### Example {number} - {GRADE_ENGLISH[example.grade]}",
        "",
        f"- That vacancy's title: {example.title}",
        f"- The skills that vacancy asked for: {asked}",
        "",
        LETTER_OPEN,
        example.text,
        LETTER_CLOSE,
        "",
    ]
