"""Deciding what not to open, from what hh already told us.

The brief's reason for a prefilter is economy: «не тратить дневной лимит на
заведомую ошибку». A vacancy that is closed, already applied to, or gated behind
an employer's test cannot be applied to by this agent, and finding that out by
opening it costs a page load and a slot out of a deliberately small daily
budget.

**The brief names the wrong fields, and this is the module where that matters.**
It says to read ``@responseLetterRequired``, ``userTestPresent``, ``userTestId``
and ``autoResponse`` off ``vacancyView``. Measured on live pages on 2026-09-06:
all four are ``null`` there, and ``userTestPresent`` is not on a vacancy page at
all — it belongs to the search payload, which this project does not read.
A prefilter written to the brief would therefore see "no letter required, no
test" for every vacancy in the corpus and route all of them straight at the
apply flow.

The real values are in ``applicantVacancyResponseStatuses``, a top-level key of
the page state, present even without an account, keyed by the vacancy id **as a
string**:

    {"136962420": {"test": {"hasTests": false},
                   "letterMaxLength": 10000,
                   "shortVacancy": {"@responseLetterRequired": false, ...}}}

**The unknown shape is a stop, not a default.** :func:`read` returns ``None``
when the shape is not one it recognises, and a ``None`` sends the vacancy to a
human rather than to the apply flow. The alternative — treating "I could not
read it" as "not applied yet" — is a design that starts double-applying on the
day hh changes that key, quietly, to every vacancy at once. Since 2026-09-06 the
applied signal itself is measured and lives in ``agent/state_page.py``; this
module reads the conditions around it.

**Two stages, not one, because they know different things.**
:func:`decide_before_opening` runs on the queue, before a page load is spent.
:func:`decide` runs on the vacancy page, where the letter requirement, the
employer's test and the idempotency reading actually live. Collapsing them
produces a decision made on facts that were not knowable yet, which is a bug
unit tests do not catch because the missing fact simply reads as ``None``.

**Removed 2026-09-07: ``decide_on_form``, and there is no third stage now.** It
ran on the open response modal and it existed to turn one sentence — hh's
«поменяйте видимость резюме…» — into a ``MANUAL`` verdict that stopped the
send. That sentence is advice, not a refusal: measured on 2026-09-07 in real
Chrome on the owner's account, an application went out with it on screen and
``negotiations.total`` went 0 -> 1. See :data:`~agent.state_page.VISIBILITY_WORDS`
for the measurement and for how a guess got to stand as a fact for a day.

Nothing replaced it, deliberately. Everything that can stop an application is a
checkable fact and every one of them is knowable before the modal opens —
``negotiations.total``, ``status.archived``, ``closedForApplicants``, an
employer's test, a required letter that is missing — so :func:`decide` is where
they are all decided. A stage that reads a card and can only ever answer
``PROCEED`` would be a decision function shaped like a guard with nothing behind
it, and the next person to read this file would believe it. What the modal is
still good for is hh's own words, which ``agent/submit.py`` hands back to the
caller and the caller shows to a person; that is a reading, not a decision, and
it lives in ``agent/state_page.py`` with the rest of the reading.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, final

from agent.state import Status
from agent.state_page import printable


class Verdict(StrEnum):
    """What the prefilter concluded, before anything was opened."""

    #: Worth opening: nothing known says otherwise.
    PROCEED = "proceed"
    #: Nothing to do — closed, archived, or already applied to.
    SKIP = "skip"
    #: A person has to handle this one.
    MANUAL = "manual"


@final
@dataclass(frozen=True, slots=True)
class Decision:
    """The verdict and the sentence a person will read next to it."""

    verdict: Verdict
    reason: str

    @property
    def status(self) -> Status:
        """Where this leaves the vacancy in the journal."""
        return {
            Verdict.PROCEED: Status.QUEUED,
            Verdict.SKIP: Status.SKIPPED,
            Verdict.MANUAL: Status.NEEDS_MANUAL,
        }[self.verdict]


@final
@dataclass(frozen=True, slots=True)
class VacancyStatus:
    """Whether the page still accepts applications at all.

    Read from ``vacancyView`` rather than from the queue, because a vacancy can
    close, be archived or be filled between a crawl and a run and the page is
    the later witness.
    """

    #: The employer has archived it. Nothing to apply to.
    archived: bool
    #: hh's own ``closedForApplicants``, measured as a real boolean on live
    #: pages on 2026-09-06 (unlike the four fields the brief named, which are
    #: all ``null`` there).
    closed_for_applicants: bool


@final
@dataclass(frozen=True, slots=True)
class ResponseFacts:
    """What hh says about applying to one vacancy, read from its own page state."""

    #: This vacancy will not accept an application without a covering letter.
    letter_required: bool
    #: The employer attached a test. The agent never answers one.
    has_test: bool
    #: hh's ceiling on the letter for this vacancy.
    letter_max_length: int
    #: The test's questions, when hh put them somewhere this code recognises.
    #: Empty is **not** "there are no questions" — see :func:`_test_questions`.
    #: They exist to be shown to the person; nothing here or anywhere else in
    #: this package produces an answer to one, not even a blank default.
    test_questions: tuple[str, ...] = ()
    #: ``responseImpossible``, measured as ``false`` on both probed vacancies.
    #: What ``true`` means is inferred from the name, which is the reasoning
    #: this package distrusts everywhere else — with one asymmetry that makes it
    #: acceptable here. ``alreadyApplied`` was read to decide *go ahead*, and
    #: being wrong about it meant a second application nobody can take back.
    #: This is read only to *add* a stop: being wrong costs one vacancy shown to
    #: a person. A guess that can only refuse is a different kind of guess from
    #: one that can send.
    response_impossible: bool = False


def read_status(state: dict[str, Any]) -> VacancyStatus:
    """Whether this vacancy is still open, from ``vacancyView``.

    ``Any`` for the state for the reason CLAUDE.md asks for: it is hh's whole
    boot payload and only the keys read here are validated.

    Two shapes are accepted for the archive flag — ``vacancyView.status.archived``
    and a flat ``vacancyView.archived`` — because hh serves the vacancy status as
    a nested object in its API and the flat spelling is what this package's own
    fixtures use. Neither was measured on a live archived page, so an absent flag
    reads as "not archived" rather than as a stop: unlike the idempotency
    question, being wrong here costs a page load and a refusal from hh, and
    treating every unreadable page as archived would skip the whole queue in
    silence.
    """
    view = state.get("vacancyView")
    if not isinstance(view, dict):
        return VacancyStatus(archived=False, closed_for_applicants=False)
    status = view.get("status")
    nested = status.get("archived") if isinstance(status, dict) else None
    archived = nested if isinstance(nested, bool) else view.get("archived")
    return VacancyStatus(
        archived=archived is True,
        closed_for_applicants=view.get("closedForApplicants") is True,
    )


def read(state: dict[str, Any], vacancy_id: str) -> ResponseFacts | None:
    """The application facts for one vacancy, or None when the shape is unfamiliar.

    ``Any`` for the state, with the reason CLAUDE.md asks for: this is hh's
    whole boot payload, dozens of unrelated keys, and the shape belongs to them.
    Everything read out of it is validated here.

    None is not "no facts". It means the page did not say what this function
    knows how to read, and every caller must treat it as a reason to stop.
    """
    statuses = state.get("applicantVacancyResponseStatuses")
    if not isinstance(statuses, dict):
        return None
    entry = statuses.get(str(vacancy_id))
    if not isinstance(entry, dict):
        return None

    test = entry.get("test")
    if not isinstance(test, dict) or not isinstance(test.get("hasTests"), bool):
        return None
    short = entry.get("shortVacancy")
    if not isinstance(short, dict):
        return None
    required = short.get("@responseLetterRequired")
    if not isinstance(required, bool):
        return None
    max_length = entry.get("letterMaxLength")
    if not isinstance(max_length, int) or max_length <= 0:
        return None

    return ResponseFacts(
        letter_required=required,
        has_test=test["hasTests"],
        letter_max_length=max_length,
        test_questions=_test_questions(test),
        response_impossible=entry.get("responseImpossible") is True,
    )


#: Keys that could hold the list of an employer's test questions, and keys that
#: could hold one question's text. **None of this was measured**: every probed
#: vacancy had ``{"hasTests": false}`` and an empty test object, and the popup
#: fetches the test separately. So this is a net cast over the plausible
#: spellings, not a reading of a known shape.
_QUESTION_LISTS: tuple[str, ...] = ("questions", "questionList", "items", "tasks")
_QUESTION_TEXTS: tuple[str, ...] = ("text", "title", "name", "question", "body")


def _test_questions(test: dict[str, Any]) -> tuple[str, ...]:
    """The employer's questions, if hh happened to put them on the page.

    ``Any`` because the test object is hh's; each candidate value is type-checked
    before it is used.

    **An empty result means "not found here", never "there are none".** The
    caller must say so to the person in those words. The failure to avoid is a
    card that reads «тест: вопросов нет» over a test that has five, because the
    next thing a person does with that card is stop reading it.

    Text quoted out of hh goes through :func:`~agent.state_page.printable`
    first: a question is written by an employer, an employer can type an emoji,
    and this console encodes cp1251 — which turns somebody else's decoration
    into a crash halfway through a batch.
    """
    found: list[str] = []
    for key in _QUESTION_LISTS:
        items = test.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            text = _question_text(item)
            if text is not None:
                found.append(text)
    return tuple(found)


def _question_text(item: object) -> str | None:
    """One question as a printable line, or None if this is not a question.

    The first spelling that yields a non-empty string wins, so an object
    carrying both ``title`` and ``text`` contributes one line rather than two.
    """
    if isinstance(item, str):
        return printable(item.strip()) or None
    if not isinstance(item, dict):
        return None
    for name in _QUESTION_TEXTS:
        value = item.get(name)
        if isinstance(value, str) and value.strip():
            return printable(value.strip())
    return None


def decide_before_opening(
    *,
    closed_for_applicants: bool,
    archived: bool,
    external_application: bool = False,
) -> Decision:
    """What the queue alone can rule out, before a page is opened.

    A separate function from :func:`decide` because the two stages know
    different things, and conflating them produces a bug that unit tests do not
    catch: ``decide`` reads ``facts=None`` as "the page was unreadable", which is
    a stop — but at the queue stage the facts are not missing, they are simply
    not knowable yet, because they live on the page. Calling the page-stage
    decision here sends every vacancy to a human and the run reports, truthfully
    and uselessly, that there is nothing to send.

    A ``PROCEED`` from this function means "worth opening", never "worth
    sending". The real decision happens on the page, with the letter
    requirement, the test flag and the idempotency reading in hand.
    """
    if archived:
        return Decision(Verdict.SKIP, "вакансия в архиве")
    if closed_for_applicants:
        return Decision(Verdict.SKIP, "вакансия закрыта для откликов")
    if external_application:
        return Decision(Verdict.MANUAL, "отклик оформляется на сайте работодателя")
    return Decision(Verdict.PROCEED, "стоит открыть")


def decide(
    *,
    facts: ResponseFacts | None,
    closed_for_applicants: bool,
    archived: bool,
    already_applied: bool | None,
    has_letter: bool,
    external_application: bool = False,
    letter_field_known: bool = True,
) -> Decision:
    """What to do with one vacancy once its page has been read.

    Everything :func:`decide_before_opening` rules out is re-checked here,
    because a vacancy can close between a crawl and a run and the page is the
    later witness.

    ``already_applied`` is tri-state on purpose: ``None`` means the page did not
    tell us, which is a reason to stop rather than a reason to proceed. It is
    checked first because applying twice is the one mistake the owner cannot
    undo, and it is the mistake a stale local journal produces. Since 2026-09-06
    the value comes from ``negotiations.total`` in
    :func:`agent.state_page.read_applied`, which is a count rather than the
    presence of an element — the apply *button* is present on vacancies already
    applied to, because hh permits a second application.

    ``closed_for_applicants`` and ``archived`` stay plain booleans so that this
    signature keeps working for callers that already have them; :func:`read_status`
    is where they come from when the caller is holding the page state.

    ``external_application`` — an application completed on the employer's own
    site — arrives from the queue, because no marker for it has been measured in
    the page state. It goes to a person rather than being skipped: the vacancy is
    real and worth applying to, just not from here.

    ``letter_field_known`` defaults to ``True`` so that this signature keeps
    working, but the caller in the browser must pass
    ``selectors.letter_field_is_known()``. Sending *without* a letter was
    measured end to end on 2026-09-06, the textarea that holds a letter on
    2026-09-16. If that second measurement is ever missing — a redesign, a
    deleted evidence file — a vacancy that demands a letter goes to a person,
    even when a letter is sitting right there. This is a routing
    decision, not an error: it takes one vacancy out of the batch rather than
    stopping the run, and it is the difference between "we cannot do this yet"
    and a guessed selector typing somebody's letter into whatever it happens to
    match. It is checked here rather than by an import of ``agent.selectors``,
    which would tie this module to the file another change owns.
    """
    if already_applied is None:
        return Decision(
            Verdict.MANUAL,
            "hh не сообщил, откликались ли уже — форма ответа изменилась, нужен человек",
        )
    if already_applied:
        return Decision(Verdict.SKIP, "отклик уже отправлен")
    if archived:
        return Decision(Verdict.SKIP, "вакансия в архиве")
    if closed_for_applicants:
        return Decision(Verdict.SKIP, "вакансия закрыта для откликов")
    if external_application:
        return Decision(Verdict.MANUAL, "отклик оформляется на сайте работодателя")
    if facts is None:
        return Decision(
            Verdict.MANUAL,
            "не удалось прочитать условия отклика на странице — нужен человек",
        )
    if facts.has_test:
        # The brief's rule, and the reason for it: the employer reads these
        # answers as the candidate's own.
        return Decision(Verdict.MANUAL, _test_reason(facts.test_questions))
    if facts.response_impossible:
        return Decision(
            Verdict.MANUAL,
            "hh отметил вакансию как недоступную для отклика — посмотрите сами",
        )
    if facts.letter_required and not has_letter:
        return Decision(Verdict.MANUAL, "нужно сопроводительное письмо, а его нет")
    if facts.letter_required and not letter_field_known:
        return Decision(
            Verdict.MANUAL,
            "вакансия требует письмо, а поле для него ещё никто не видел на живой "
            "странице — отправьте этот отклик руками",
        )
    return Decision(Verdict.PROCEED, "можно откликаться")


def _test_reason(questions: tuple[str, ...]) -> str:
    """The line a person reads about an employer's test, with the questions in it.

    When the questions were not on the page it says exactly that. It must not
    say «вопросов нет»: the questions are fetched separately by hh's own popup,
    so "not on this page" and "does not exist" are different facts, and a card
    that conflates them teaches its reader to stop reading it.
    """
    head = "работодатель приложил тест — отвечает человек"
    if not questions:
        return f"{head}; вопросы hh на странице не отдал, откройте вакансию"
    listed = "; ".join(f"{number}) {text}" for number, text in enumerate(questions, start=1))
    return f"{head}; вопросы: {listed}"
