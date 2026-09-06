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

**The unknown shape is a stop, not a default.** The one thing this module cannot
yet know is what that key looks like once the owner has actually applied — that
needs an authenticated probe. So :func:`read` returns ``None`` when the shape is
not one it recognises, and a ``None`` sends the vacancy to a human rather than
to the apply flow. The alternative — treating "I could not read it" as "not
applied yet" — is a design that starts double-applying on the day hh changes
that key, quietly, to every vacancy at once.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, final

from agent.state import Status


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
class ResponseFacts:
    """What hh says about applying to one vacancy, read from its own page state."""

    #: This vacancy will not accept an application without a covering letter.
    letter_required: bool
    #: The employer attached a test. The agent never answers one.
    has_test: bool
    #: hh's ceiling on the letter for this vacancy.
    letter_max_length: int


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
    )


def decide(
    *,
    facts: ResponseFacts | None,
    closed_for_applicants: bool,
    archived: bool,
    already_applied: bool | None,
    has_letter: bool,
    external_application: bool = False,
) -> Decision:
    """What to do with one vacancy, in the order the reasons matter.

    ``already_applied`` is tri-state on purpose: ``None`` means the page did not
    tell us, which is a reason to stop rather than a reason to proceed. It is
    checked first because applying twice is the one mistake the owner cannot
    undo, and it is the mistake a stale local journal produces.
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
        return Decision(Verdict.MANUAL, "работодатель приложил тест — отвечает человек")
    if facts.letter_required and not has_letter:
        return Decision(Verdict.MANUAL, "нужно сопроводительное письмо, а его нет")
    return Decision(Verdict.PROCEED, "можно откликаться")
