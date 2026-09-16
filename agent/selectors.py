"""Every selector this agent uses, and proof that somebody looked at it.

The brief is blunt about why this file exists: the selectors for the apply
button and the letter form must not be invented and must not be recalled from
memory — «Угаданный селектор — это молчаливый провал на проде». hh does use
``data-qa`` attributes, which is exactly what makes guessing feel safe, and a
guess that happens to match something produces an agent that clicks the wrong
thing on somebody's real account.

So a selector here is not a string. It is a string plus the evidence that a
human saw it on a real page, and the evidence is checked at startup rather than
believed.

**Why the evidence and not a date.** The obvious version of this file is a
constant with ``# checked 2026-09-06`` beside it. That reduces the whole
guarantee to "somebody typed a date", and a date is the easiest thing in the
world to type next to a guess. :func:`assert_ready_to_apply` instead parses a
recorded list of what was on the page and requires that every ``data-qa`` the
query depends on is one that record names, from a stage that could see it, in a
session that was measured as logged in.

**And exactly how strong that is.** Corrected 2026-09-07: this paragraph used to
say "a machine-written record", and the check below cannot tell. A record is a
JSON file in ``agent/evidence/``; it names what produced it, and that name is
its own claim about itself, not a signature. Somebody willing to type a file can
type ``"produced_by": "agent/probe_apply.py"`` into it. So the guarantee is not
"this was measured by a machine" — it is narrower and still worth the code:
a selector cannot be added without a committed file that lists the ``data-qa``
names the page carried, and the name in the query has to be in that list. A
guess fails that unless the guesser also edits the evidence, which is a
deliberate act, in a reviewable diff, next to a field that says which artefact
the file was redacted from. That is the whole of it, and overstating it would be
worse than stating it small, because the next person trusts what is written
here.

**Where that record lives, and why it moved.** It used to be the probe's own
report, in ``agent/probe/<run>/probe.json``. That directory is gitignored and
has to be: a report carries the owner's own application state for a vacancy,
read out of an authenticated page. The consequence was quietly fatal — the
selectors could only ever be verified on the owner's laptop, and a test that
asserts they are verified could not run anywhere else. So the probe now writes
two files per run: the full report, gitignored as before, and a REDACTED
evidence file in :data:`EVIDENCE_DIR` holding only what this check reads — the
``data-qa`` names, the stage, and the measured authentication flag. No vacancy
id, no URL, no negotiation records, nothing about the owner. That file is
committable, and it is the one this module reads.

**Why scope matters.** A selector measured logged out is genuinely useful and
is not enough to apply with: the page a logged-in applicant sees carries
different controls, and the form behind the apply link is not on the anonymous
page at all. An anonymous scope is therefore explicitly insufficient for the
apply flow, and :func:`assert_ready_to_apply` says so by name.

**Why this fails at startup.** An unverified selector is not a per-vacancy
error. If it were, running the apply flow before the probe would produce a run
of failures that stops after the second one with the reason "two things went
wrong", which is a true statement about the wrong problem. It raises
:class:`SelectorsNotVerifiedError` before the browser opens, listing everything
outstanding at once, so the message is "you have not run the probe yet" and the
answer is one command.

**Two things measured on 2026-09-06 that this file records rather than encodes.**

*There are two apply controls, not one.* A fresh vacancy carries
``vacancy-response-link-top``; one that already has an application carries
``vacancy-response-link-top-again`` («Отклик другим резюме») plus
``vacancy-response-link-view-topic``, and the plain ``-top`` control is absent
entirely. hh ALLOWS a repeat application, so the presence of ``-again`` does not
mean "not applied yet" and its absence does not mean "applied". Idempotency is
decided from ``applicantVacancyResponseStatuses[id].negotiations.total`` and
never from which element is on the page. Both controls are named below because
the flow has to recognise both, and — corrected 2026-09-07 — both are in
:data:`REQUIRED_FOR_APPLYING`, because the flow can click either one and this
file verifies what can be clicked rather than what is meant to be clicked.

*The warning line has one name and several meanings.* ``hidden-resume-warning``
carried, on the same account on the same day, a notice that the resume's
visibility should be changed and a prediction that the application may be
rejected, naming the specific requirement the resume misses. They must be told
apart by their text; the measured strings are :data:`RESUME_VISIBILITY_NOTICE`
and :data:`LIKELY_REJECTION_WARNING`. Neither of them stops an application —
corrected 2026-09-07, when the first one was measured not to; see that constant
and :data:`SEND_UNDER_VISIBILITY_EVIDENCE`.
"""

import json
import re
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, final

#: Where ``probe_apply.py`` writes the full report of a run. One directory per
#: run. Gitignored: a report contains the owner's own application state.
PROBE_DIR: Final[Path] = Path(__file__).parent / "probe"
PROBE_FILENAME: Final[str] = "probe.json"

#: Where the redacted evidence goes: one JSON file per run, named after the run,
#: containing only what :func:`assert_ready_to_apply` reads. Committable, and
#: the only thing this module will accept as proof.
EVIDENCE_DIR: Final[Path] = Path(__file__).parent / "evidence"

#: Stamped into every evidence file so a future change of shape is a loud
#: failure rather than a check that silently finds nothing and passes.
EVIDENCE_SCHEMA: Final[str] = "hh-agent-selector-evidence/1"

#: The other kind of file in :data:`EVIDENCE_DIR`: a measurement that is not
#: about a selector. Same directory and the same redaction rule — nothing about
#: the owner or their applications — and a schema of its own, so that
#: :func:`assert_ready_to_apply` can never mistake one for proof of a selector
#: and the test that walks the directory can tell which contract to hold a file
#: to.
MEASUREMENT_SCHEMA: Final[str] = "hh-agent-measurement/1"

#: What was measured on 2026-09-07: an application sent while hh's
#: resume-visibility notice was on the card. It is the evidence for the rule that
#: is no longer here — until that day the notice was treated as a refusal and
#: nothing could be sent at all — so it is committed rather than left in a
#: gitignored probe report where only the owner's laptop could see it.
#:
#: **It is hand-written, and it says so inside itself.** No probe produced it:
#: the run was driven over CDP against the owner's real Chrome, and its artefact
#: is ``agent/probe/_cdp_send.json``, which cannot be committed because it names
#: the vacancy the owner applied to. This file is a redaction of that artefact
#: typed by a person, and its ``produced_by`` says exactly that. Presenting it as
#: a machine's output would be the failure this whole module is about — a claim
#: about provenance is not provenance, and the one thing worse than a hand-made
#: record is a hand-made record wearing a machine's name.
SEND_UNDER_VISIBILITY_EVIDENCE: Final[str] = "20260907-send-under-visibility-notice"

#: hh's exact words about the resume's visibility. Measured 2026-09-06 in
#: ``hidden-resume-warning``, dumped to ``agent/probe/_warn.json``.
#:
#: **Renamed 2026-09-07 from ``RESUME_HIDDEN_REFUSAL``, because it is not one.**
#: The old name said "refusal" and the old comment said "a hard stop: do not
#: send", and both came from a guess in a brief rather than from anything
#: anybody had measured. On 2026-09-07 an application was sent in real Chrome,
#: on the owner's account, with this exact sentence on the card:
#: ``negotiations.total`` went 0 -> 1 and the apply control became
#: ``vacancy-response-link-top-again``. The measurement is committed in
#: :data:`SEND_UNDER_VISIBILITY_EVIDENCE` and it is asserted against this
#: constant by a test, so the two cannot drift apart. Because the rule blocked
#: every application this package could make, its own falsification was the one
#: experiment it forbade — the lesson is written up in
#: ``agent/state_page.py``, beside the words that do the matching.
#:
#: Corrected 2026-09-07. hh writes U+00A0 after both «на», and this constant had
#: them retyped as ordinary spaces — in the one file whose whole discipline is
#: that measurements are not retyped from memory. The escapes are deliberate:
#: the character is invisible in an editor, so spelling it out is the only way
#: the next person can see that it is not a typo. It survives the console this
#: runs on — cp1251 encodes U+00A0 as 0xA0 — so writing it exactly costs
#: nothing. Nothing matches against this string: ``agent/state_page.py`` owns
#: the matching, normalises hh's typographic spaces away first, and anchors on a
#: few words rather than the sentence, because the sentence is hh's to reword.
#: This constant is the record of what hh said, exactly, on the day it was read.
RESUME_VISIBILITY_NOTICE: Final[str] = (
    "Чтобы откликнуться на\u00a0эту вакансию, поменяйте видимость резюме "
    "на\u00a0«Видно компаниям-клиентам HeadHunter»"
)

#: The same element, a different meaning: hh's own analysis of how this
#: application is likely to go. Not a blocker — it is shown to the owner beside
#: the match score, and it is more precise than any embedding, because it names
#: the requirement that is unmet. Measured 2026-09-06, followed on that page by
#: «Английский язык в резюме … ниже обязательного уровня, который указал
#: работодатель.»
LIKELY_REJECTION_WARNING: Final[str] = "Такой отклик может получить отказ"


class Scope(StrEnum):
    """The session a selector was confirmed under."""

    #: Seen on a public page with no account. Fine for reading, never enough to
    #: apply with: the applicant's page is a different page.
    ANONYMOUS = "anonymous"
    #: Seen while logged in as the owner, which is where applications happen.
    AUTHENTICATED = "authenticated"
    #: Nobody has looked yet. The apply flow refuses to start.
    UNVERIFIED = "unverified"


@final
@dataclass(frozen=True, slots=True)
class Selector:
    """One query, and the record of who saw it work."""

    #: The name used in error messages and in the probe's report.
    name: str
    #: The Playwright query. ``data-qa`` only: hh's class names are hashed CSS
    #: modules (``magritte-button_style-accent___TE21J_7-2-27``) that change
    #: every release, so a class selector is a time bomb with a fuse of days.
    query: str
    scope: Scope
    #: The evidence file under :data:`EVIDENCE_DIR`, without its extension,
    #: whose record of the page contains :attr:`query`. ``None`` while
    #: unverified.
    evidence: str | None = None
    checked_on: date | None = None
    #: Which probe stages could have seen this control. The response modal only
    #: exists after the apply link is clicked, so its parts need an
    #: ``open-form`` run; the controls on the vacancy page itself are visible to
    #: a run that clicks nothing, and demanding ``open-form`` for those would
    #: reject perfectly good evidence.
    seen_at_stages: frozenset[str] = frozenset({"open-form"})
    #: What this control does, in the words of somebody who has to fix it later.
    note: str = ""

    @property
    def usable_for_applying(self) -> bool:
        """Whether this may be relied on to send a real application."""
        return self.scope is Scope.AUTHENTICATED and self.evidence is not None

    def evidence_path(self) -> Path | None:
        """Where the proof should be, if this claims to have any."""
        return None if self.evidence is None else EVIDENCE_DIR / f"{self.evidence}.json"


#: The evidence files the selectors below name. Each is a redaction of a real
#: measurement on the owner's logged-in profile and says inside itself which
#: script produced it.
OPEN_FORM_EVIDENCE: Final[str] = "20260906-open-form-modal"
ALREADY_APPLIED_EVIDENCE: Final[str] = "20260906-inspect-already-applied"
#: ``--stage open-form`` on 2026-09-16, the run that also clicked «Добавить
#: сопроводительное»: ``vacancy-response-popup-form-letter-input`` is absent
#: before that click and present after it, and ``add-cover-letter`` is the
#: other way round (``data_qa_after_click`` against
#: ``data_qa_after_letter_click`` in the full report, which stays local).
LETTER_FIELD_EVIDENCE: Final[str] = "20260916-125039"

#: Stages at which a control on the vacancy page itself can be recorded.
ON_THE_PAGE: Final[frozenset[str]] = frozenset({"inspect", "open-form"})


# ── the two apply controls ────────────────────────────────────────────

#: A fresh vacancy's «Откликнуться».
#:
#: Corrected 2026-09-07. This note used to state, as measurement, that the
#: control is ``<a role="button">`` with an href of
#: ``/applicant/vacancy_response?vacancyId=…&employerId=…&hhtmFrom=vacancy``.
#: Where that came from: ``page.html`` in the repository root — an untracked
#: scratch dump of an hh SEARCH RESULTS page, so this trail can be deleted by a
#: tidy-up, which is its own argument for writing down what it said — in which
#: every card's apply control is exactly
#: ``<a … role="button" data-qa="vacancy-serp__vacancy_response"
#: href="/applicant/vacancy_response?vacancyId=136131345&employerId=5991214
#: &hhtmFrom=vacancy_search_list">``. So the anchor, the role, the path and the
#: two id parameters ARE measured — on a different control, on a different page.
#: What is inferred is that ``vacancy-response-link-top`` on the vacancy page
#: has the same shape, and ``hhtmFrom=vacancy`` in particular appears in no
#: artefact at all; the 2026-09-06 probe dumps recorded ``data-qa`` names only
#: and captured no attributes. Marked inferred rather than deleted because the
#: inference is load-bearing: ``agent/submit.py`` reads this control's href and
#: refuses to click when the vacancy id is not in it, so a control that turns
#: out to be a ``<button>`` with no href fails closed and reports the wrong
#: reason. Measuring it is one attribute read away in the next probe run.
#:
#: Following such a link is a GET document navigation, which is why
#: ``agent/gate.py`` matches on the URL and not on the HTTP method.
APPLY_LINK = Selector(
    name="apply_link",
    query='[data-qa="vacancy-response-link-top"]',
    scope=Scope.AUTHENTICATED,
    evidence=OPEN_FORM_EVIDENCE,
    checked_on=date(2026, 9, 6),
    seen_at_stages=ON_THE_PAGE,
    note="«Откликнуться» on a vacancy with no application yet.",
)

#: The same place on a vacancy that already has an application: «Отклик другим
#: резюме».
#:
#: Corrected 2026-09-07: this used to say the control is named "so the flow can
#: recognise the page, not so it can use it", which was not true of the code.
#: ``agent/submit.py`` clicks ``any_apply_control().first``, so on a page that
#: carries only this one, this is the control the flow clicks. That is not a
#: repeat application slipping through — a vacancy with an application is turned
#: away earlier, from ``negotiations.total``, and the only way to arrive here
#: with this control on screen is a page that grew one between the read and the
#: click. It does mean the selector is one the flow can act on, so it is
#: verified like any other; see :data:`REQUIRED_FOR_APPLYING`.
APPLY_LINK_AGAIN = Selector(
    name="apply_link_again",
    query='[data-qa="vacancy-response-link-top-again"]',
    scope=Scope.AUTHENTICATED,
    evidence=ALREADY_APPLIED_EVIDENCE,
    checked_on=date(2026, 9, 6),
    seen_at_stages=ON_THE_PAGE,
    note="«Отклик другим резюме». Presence proves nothing about idempotency.",
)

#: Next to it: the link to the conversation the existing application started.
VIEW_TOPIC = Selector(
    name="view_topic",
    query='[data-qa="vacancy-response-link-view-topic"]',
    scope=Scope.AUTHENTICATED,
    evidence=ALREADY_APPLIED_EVIDENCE,
    checked_on=date(2026, 9, 6),
    seen_at_stages=ON_THE_PAGE,
    note="Link to the existing negotiation. Read-only, for the human.",
)

#: A decoy, recorded so that nobody rediscovers it as a candidate. Seven of
#: these on a vacancy page: the «задать вопрос работодателю» widget, whose names
#: all start the same way as the real control, so a substring search for
#: "response" finds them first. Never used for anything; it is here to be
#: recognised.
QUESTION_WIDGET_DECOY = Selector(
    name="question_widget_decoy",
    query='[data-qa^="vacancy-response-question"]',
    scope=Scope.AUTHENTICATED,
    evidence=OPEN_FORM_EVIDENCE,
    checked_on=date(2026, 9, 6),
    seen_at_stages=ON_THE_PAGE,
    note="NOT the application form — this asks the employer a question.",
)


def any_apply_control() -> str:
    """A query matching whichever of the two apply controls this page carries.

    Needed because the plain ``-top`` control is absent entirely from an
    already-applied vacancy: waiting on it there is waiting for something that
    will never appear, which is how ``--stage open-form`` became unreachable by
    its own guard. Nothing decides idempotency from the result — see the module
    docstring.
    """
    return f"{APPLY_LINK.query}, {APPLY_LINK_AGAIN.query}"


# ── the response modal, measured on 2026-09-06 ────────────────────────
# All of these appear only after the apply link is clicked, and later than two
# seconds after it. Everything below carries the same evidence file.

#: The modal itself. Its ``inner_text`` is what gets classified, and it is the
#: anchor everything else is found inside. In the main frame, not an iframe.
RESPONSE_FORM = Selector(
    name="response_form",
    query='[data-qa="modal-overlay"]',
    scope=Scope.AUTHENTICATED,
    evidence=OPEN_FORM_EVIDENCE,
    checked_on=date(2026, 9, 6),
    note="The response modal. Wait for this, never for a fixed number of seconds.",
)

#: ``type=submit``. This is the one that sends.
SUBMIT_BUTTON = Selector(
    name="submit_button",
    query='[data-qa="vacancy-response-submit-popup"]',
    scope=Scope.AUTHENTICATED,
    evidence=OPEN_FORM_EVIDENCE,
    checked_on=date(2026, 9, 6),
    note=(
        "Sends the application. Measured: it stays ENABLED even when hh has "
        "already refused the application in the warning above it, so its "
        "disabled state means nothing and must never be read as permission."
    ),
)

#: ``type=button``. Opens the letter field; it is not the letter field.
ADD_COVER_LETTER = Selector(
    name="add_cover_letter",
    query='[data-qa="add-cover-letter"]',
    scope=Scope.AUTHENTICATED,
    evidence=OPEN_FORM_EVIDENCE,
    checked_on=date(2026, 9, 6),
    note="«Добавить сопроводительное» — reveals the letter field, does not send.",
)

#: Closes the modal without sending. What the flow uses to back out.
CLOSE_RESPONSE_FORM = Selector(
    name="close_response_form",
    query='[data-qa="response-popup-close"]',
    scope=Scope.AUTHENTICATED,
    evidence=OPEN_FORM_EVIDENCE,
    checked_on=date(2026, 9, 6),
    note="Closes the modal. The way out of a form that must not be submitted.",
)

#: Which resume hh will attach. Shown on the confirmation card, because the
#: owner is agreeing to send this resume and not merely to apply.
RESUME_TITLE = Selector(
    name="resume_title",
    query='[data-qa="resume-title"]',
    scope=Scope.AUTHENTICATED,
    evidence=OPEN_FORM_EVIDENCE,
    checked_on=date(2026, 9, 6),
    note="The resume that will be sent. Belongs on the confirmation card.",
)

#: One element, several meanings. Classify by text — see the module docstring
#: and the two measured strings above.
HIDDEN_RESUME_WARNING = Selector(
    name="hidden_resume_warning",
    query='[data-qa="hidden-resume-warning"]',
    scope=Scope.AUTHENTICATED,
    evidence=OPEN_FORM_EVIDENCE,
    checked_on=date(2026, 9, 6),
    note="A warning line whose meaning is in its text, not in its presence.",
)


# ── what nobody has seen yet ──────────────────────────────────────────

#: The cover-letter ``<textarea>``. Not on the form until ``add_cover_letter``
#: is clicked, and that button is gone once it has been: the two swap places.
#: Measured 2026-09-16 on the run in :data:`LETTER_FIELD_EVIDENCE`, which waited
#: for the field and recorded «textarea appeared».
#:
#: **Present is not the same as ready.** That run's form dump reported
#: ``editable: false`` for this textarea — but the probe computed the flag as
#: ``isContentEditable``, which is false for every textarea by definition, so
#: the number says nothing about the modal. ``agent/submit.py`` waits for the
#: field to accept input anyway and checks what landed in it before sending:
#: the modal renders in stages, and typing into a field that is not ready yet is
#: a letter that silently does not arrive.
LETTER_FIELD = Selector(
    name="letter_field",
    query='[data-qa="vacancy-response-popup-form-letter-input"]',
    scope=Scope.AUTHENTICATED,
    evidence=LETTER_FIELD_EVIDENCE,
    checked_on=date(2026, 9, 16),
    note="The cover-letter textarea, revealed by add_cover_letter.",
)

#: Employer test questions, to READ and show a human. Never answered here, and
#: never needed either: a vacancy whose state says it carries a test is routed
#: to the owner before its page is opened, so this is not required to apply.
TEST_QUESTIONS = Selector(
    name="test_questions",
    query="",
    scope=Scope.UNVERIFIED,
    note="Employer test questions, to READ and show a human. Never answered here.",
)


#: Everything the apply flow touches to send an application with no letter,
#: which is the case that has actually been measured end to end. Read-only paths
#: (opening a vacancy, reading its state) are deliberately not here: they need
#: no selector at all, because the page's own JSON carries what they read.
#:
#: :data:`APPLY_LINK_AGAIN` joined this list on 2026-09-07, and the reason is
#: worth writing down because the omission looked principled. It was left out on
#: the grounds that a repeat application is not something this agent does — true,
#: and about the wrong list. ``agent/submit.py`` clicks
#: ``page.locator(any_apply_control()).first``, and :func:`any_apply_control`
#: joins the two, so the flow could click a control that this check had never
#: looked at. The rule this list encodes is "everything the flow can click", not
#: "everything the flow intends to click": an unverified selector is dangerous
#: because of what it might match, and intent does not narrow that. The
#: statement it does NOT make is still the one in the module docstring —
#: idempotency is decided from ``negotiations.total``, never from which of the
#: two controls the page carries.
REQUIRED_FOR_APPLYING: Final[tuple[Selector, ...]] = (
    APPLY_LINK,
    APPLY_LINK_AGAIN,
    RESPONSE_FORM,
    SUBMIT_BUTTON,
)

#: What a cover letter additionally needs. Split from the tuple above because
#: the two cases have different evidence: the form without a letter was measured
#: on 2026-09-06, the letter field on 2026-09-16. Kept apart so that losing the
#: second measurement — a redesign, a deleted file — routes letter vacancies to
#: the owner instead of stopping applications that need no letter.
REQUIRED_FOR_A_LETTER: Final[tuple[Selector, ...]] = (
    ADD_COVER_LETTER,
    LETTER_FIELD,
)


class SelectorsNotVerifiedError(Exception):
    """The apply flow cannot start because stage 0 has not been done.

    Deliberately not a subclass of any per-vacancy error: nothing in the run
    loop may catch this and carry on to the next vacancy.
    """


@final
class LetterFieldUnknownError(SelectorsNotVerifiedError):
    """This application needs a letter and the letter field has never been seen.

    A subclass, so a caller that wants to route one vacancy to the owner can
    catch this narrowly while a bare :class:`SelectorsNotVerifiedError` still
    stops the whole run.
    """


#: The ``data-qa`` names inside a Playwright query. ``[data-qa="x"]`` and
#: ``[data-qa^="x"]`` are the only two forms this package writes, and the second
#: one matches by prefix, which is how it is checked below.
_DATA_QA_IN_QUERY: Final[re.Pattern[str]] = re.compile(r'\[data-qa(\^?)="([^"]+)"\]')


def names_in(query: str) -> list[tuple[str, bool]]:
    """The ``data-qa`` names this query depends on, and whether each is a prefix.

    A query naming none of them cannot be checked against an evidence file, and
    :func:`assert_ready_to_apply` treats that as a selector that has not been
    verified rather than as one with nothing to verify.
    """
    return [(name, bool(caret)) for caret, name in _DATA_QA_IN_QUERY.findall(query)]


def redact(
    seen: list[str], decoys: list[str], *, stage: str, authenticated: bool
) -> dict[str, Any]:
    """Turn one probe run into the committable half of its evidence.

    ``Any`` because this is a JSON document with mixed value types; every field
    is written here rather than copied from the report, which is what makes the
    redaction a whitelist. Nothing about the vacancy, the owner or their
    applications can reach the output, because nothing about them is an input.
    """
    return {
        "schema": EVIDENCE_SCHEMA,
        "stage": stage,
        "authenticated": authenticated,
        "data_qa_seen": sorted(seen),
        "decoys_seen": sorted(decoys),
        "produced_by": "agent/probe_apply.py",
        "contains": (
            "Только имена data-qa, этап прогона и измеренный признак авторизации. "
            "Ни id вакансии, ни ссылок, ни откликов, ни данных владельца аккаунта."
        ),
    }


def _recorded_names(evidence: dict[str, Any]) -> set[str]:
    """Every ``data-qa`` the run actually saw, as the redacted file records them.

    Reads JSON rather than text. An earlier version asked whether the query
    string appeared anywhere in the file, which was wrong in both directions at
    once: ``json.dumps`` escapes the quotes in ``[data-qa="…"]`` so a correctly
    written selector could never match, while any substring of the file could —
    a URL fragment, the JSON key ``letterMaxLength``, or the single letter "a".
    """
    names: set[str] = set()
    for key in ("data_qa_seen", "decoys_seen"):
        section = evidence.get(key)
        if isinstance(section, list):
            names.update(str(name) for name in section)
    return names


def _evidence_problems(selector: Selector) -> list[str]:
    """Everything wrong with one filled-in selector's evidence, in words."""
    path = selector.evidence_path()
    if path is None or not path.is_file():
        return [
            f"  {selector.name}: ссылается на доказательство {selector.evidence!r}, "
            f"а файла {path} нет"
        ]
    try:
        evidence = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        return [f"  {selector.name}: {path.name} — не JSON ({error.msg})"]
    if not isinstance(evidence, dict):
        return [f"  {selector.name}: {path.name} — не файл доказательства"]

    problems: list[str] = []
    if evidence.get("schema") != EVIDENCE_SCHEMA:
        problems.append(
            f"  {selector.name}: {path.name} — схема {evidence.get('schema')!r}, "
            f"ожидается {EVIDENCE_SCHEMA!r}"
        )
    # Every evidence file has to say where it came from, and the one file in
    # this repository that was not written by probe_apply.py says so here.
    #
    # Corrected 2026-09-07, because this comment used to claim that requiring
    # the field means "a hand-made record can never look machine-made". It does
    # not, and could not: the check is that the string is non-empty, and anybody
    # typing a file by hand can type "agent/probe_apply.py" into it just as
    # easily as the probe can. There is no cryptographic provenance here and
    # this file should not imply one. What the field does buy is smaller and
    # real — a record that does not say where it came from is refused instead of
    # silently trusted, and a record that lies about it has to lie in writing,
    # in a committed file, where a reviewer reads it next to the artefact it
    # names. The load-bearing checks are the other four: a file must exist, its
    # schema must match, its stage must be one that could see the control, its
    # authenticated flag must have been measured, and every data-qa the query
    # depends on must be in the names it recorded.
    if not str(evidence.get("produced_by") or "").strip():
        problems.append(f"  {selector.name}: {path.name} не говорит, чем он получен")
    # The probe's own measurement of whether it was logged in, so that
    # "authenticated" cannot be a claim typed next to a guess.
    if evidence.get("authenticated") is not True:
        problems.append(
            f"  {selector.name}: прогон {selector.evidence!r} сделан без авторизации — "
            "форма отклика видна только под аккаунтом"
        )
    stage = evidence.get("stage")
    if stage not in selector.seen_at_stages:
        problems.append(
            f"  {selector.name}: прогон {selector.evidence!r} — этап {stage!r}, "
            f"а этот элемент виден на этапах {sorted(selector.seen_at_stages)}"
        )
    wanted = names_in(selector.query)
    if not wanted:
        return [
            *problems,
            f"  {selector.name}: в {selector.query!r} нет ни одного data-qa — "
            "проверить такой селектор по доказательству нельзя",
        ]
    seen = _recorded_names(evidence)
    for name, is_prefix in wanted:
        matched = (
            any(candidate.startswith(name) for candidate in seen) if is_prefix else name in seen
        )
        if not matched:
            problems.append(
                f"  {selector.name}: data-qa {name!r} нет среди увиденных в "
                f"{path.name} — селектор не из этого прогона"
            )
    return problems


def problems_with(group: tuple[Selector, ...]) -> list[str]:
    """Everything outstanding across a group of selectors, in words.

    Shared by the two assertions below so that "unverified", "anonymous" and
    "the evidence does not back this up" are judged the same way whichever
    group is being checked.
    """
    problems: list[str] = []
    for selector in group:
        if selector.scope is Scope.UNVERIFIED or not selector.query:
            problems.append(f"  {selector.name}: не заполнен — {selector.note}")
            continue
        if selector.scope is Scope.ANONYMOUS:
            problems.append(
                f"  {selector.name}: проверен только без авторизации; форма отклика "
                "видна лишь под аккаунтом"
            )
            continue
        problems.extend(_evidence_problems(selector))
    return problems


def assert_ready_to_apply() -> None:
    """Refuse to start the apply flow unless every selector has evidence behind it.

    Checked per selector, because each of these has been the way a guess got
    promoted somewhere: that it claims an authenticated scope, that the evidence
    file it names exists and says what produced it, that the run was measured as
    logged in, that the stage could have seen this control, and that every
    ``data-qa`` the query depends on is one that run recorded seeing. The last
    one is what makes this more than a checkbox.

    This covers an application WITHOUT a cover letter, which is the case that
    was measured end to end. A letter needs :func:`assert_letter_field_known`.
    """
    problems = problems_with(REQUIRED_FOR_APPLYING)
    if problems:
        raise SelectorsNotVerifiedError(
            "Селекторы формы отклика не проверены на живой странице под аккаунтом.\n"
            + "\n".join(problems)
            + "\n\nСначала: python -m agent.login, затем python -m agent.probe_apply "
            "--stage open-form на живой вакансии. Потом перенести значения сюда "
            "вместе с именем файла доказательства из agent/evidence/."
        )


def letter_field_is_known() -> bool:
    """Whether a cover letter can be typed at all, without raising to find out.

    For the caller that wants to route one vacancy to the owner rather than
    stop the run.
    """
    return not problems_with(REQUIRED_FOR_A_LETTER)


def assert_letter_field_known() -> None:
    """Refuse to send an application that needs a letter, and say what would fix it.

    The refusal names the one probe step that closes the gap, because "not
    verified" without "here is how to verify it" is how a blocker becomes
    permanent.
    """
    problems = problems_with(REQUIRED_FOR_A_LETTER)
    if problems:
        raise LetterFieldUnknownError(
            "Поле сопроводительного письма никто не видел на живой странице.\n"
            + "\n".join(problems)
            + "\n\nОтклик БЕЗ письма измерен и работает — блокируется только письмо.\n"
            "Закрывает пробел один шаг разведки, он уже встроен в пробу:\n"
            "  uv run python -m agent.probe_apply --stage open-form --url <вакансия>\n"
            "Проба открывает форму, нажимает «Добавить сопроводительное» и пишет\n"
            "появившиеся элементы в раздел data_qa_after_letter_click отчёта и в\n"
            "файл доказательства. Оттуда взять селектор textarea и вписать его в\n"
            "LETTER_FIELD вместе с именем этого файла."
        )
