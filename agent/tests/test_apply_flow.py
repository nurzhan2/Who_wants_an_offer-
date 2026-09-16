"""The apply flow, driven end to end with the selectors filled in.

Every other test file here exercises one module against hand-written fakes, and
72 of them passed while five separate defects sat in the path between a
confirmation and a sent application. All five were invisible for the same
reason: nothing imported ``run.py`` or ``submit.py``, so the *order* of the
steps — which is the whole design — was never executed.

That is what this file does. It runs the real :func:`agent.submit.submit` and
the real :func:`agent.run.main` against a fake page that answers like hh. The
fakes are deliberately dumb: they answer questions and record what was done to
them. Every decision under test belongs to the code being driven.

**The selectors are the real ones.** Since 2026-09-06 they carry redacted
evidence in ``agent/evidence/``, so ``assert_ready_to_apply`` passes here and in
CI, and these tests drive the queries that will run against hh rather than
stand-ins. The one exception is the cover-letter field, which nobody has
measured: the ``ready`` fixture supplies it, and the tests that check what
happens *without* it deliberately do not use that fixture.

**Why every run below says ``--no-backend``.** The queue is the backend's
answer — vacancies scored above the threshold, with a letter and no application
— and ``agent/queue.json`` is where a person adds something it did not offer.
These tests drive the flow off a file they write themselves, so they ask for the
file alone. A run that silently fell back to the file when the backend was
unreachable would be the defect this arrangement was written to end: a night of
crawling reaching the agent as a month-old hand-written row.

The defects with a test each below, so none can come back quietly:

* the gate aborted the navigation that opens the response form, because
  ``submit()`` clicked the apply link before arming;
* ``run.py`` recorded ``sent`` through a transition the state machine does not
  have, so the journal write crashed *after* the application had left;
* a captcha never incremented the consecutive-failure counter, so hh could
  challenge every vacancy in the batch and the run would walk the whole list;
* ``submit()`` never re-read the page's own application facts, so an employer
  test or a newly required letter went unnoticed;
* the captcha detector substring-matched the whole rendered page, and hh ships
  the word inside its own error dictionary — so it answered "captcha" for every
  page and nothing could ever be sent. Every fake page in this file carries that
  dictionary line for exactly that reason.
"""

import io
import json
import sys
import time as time_module
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, time
from pathlib import Path
from typing import Any

import pytest

from agent import (
    human,
    login,
    prefilter,
    probe_apply,
    run,
    selectors,
    session,
    state_page,
    submit,
)
from agent.config import Limits
from agent.gate import InterceptionEscapedError, SubmitGate
from agent.human import CONFIRM_WORD, CancelledError, Candidate, confirm
from agent.journal import Entry, Journal
from agent.letter import check as check_letter
from agent.mandate import SendMandate, digest, mint
from agent.queue import ATSCard, QueueFormatError, QueueItem, QueueUnreachableError, Result
from agent.selectors import LetterFieldUnknownError, Scope, Selector
from agent.state import (
    Actor,
    IllegalInitialStatusError,
    IllegalTransitionError,
    Status,
)
from agent.state_page import read_state
from agent.submit import (
    AlreadyAppliedError,
    CaptchaPresentedError,
    IdempotencyUnknownError,
    LetterNotTypedError,
)

pytestmark = pytest.mark.unit

VACANCY = "136773120"
PAGE_URL = f"https://almaty.hh.kz/vacancy/{VACANCY}"
APPLY_HREF = f"/applicant/vacancy_response?vacancyId={VACANCY}&employerId=99"
#: What the open form sends. A second application-shaped URL for the same job.
SEND_URL = f"https://hh.kz/applicant/vacancy_response?vacancyId={VACANCY}&lux=true"
#: Where hh sends a visitor it has decided is a robot. Measured by this
#: project's crawler on 2026-09-06 as the target of a 302 answering a plain
#: ``GET /vacancy/<id>``.
CHALLENGE_URL = f"https://hh.kz/account/captcha?backurl=%2Fvacancy%2F{VACANCY}&state=7f3c1a"

#: hh's own requests that carry a vacancy id without being an application, in
#: the shapes measured in ``agent/probe/20260906-181519/probe.json`` — a run
#: recorded at ``stage: open-form``, which opened the response form and stopped
#: before submitting. Twenty of that run's twenty-one requests were one of these,
#: and every one of them used to consume the armed window — and be aborted on
#: repeat, in the middle of the apply flow.
#:
#: Trimmed to the parameters the gate reads: the recorded URLs also carry the
#: owner's hh id and a browser fingerprint, ``agent/probe/`` is gitignored for
#: that reason, and this file is committed.
FURNITURE = (
    f"https://almaty.hh.kz/anatskytics?hhtmSource=vacancy&vacancyId={VACANCY}",
    f"https://almaty.hh.kz/anatskytics?hhtmSource=vacancy&vacancyId={VACANCY}",
    f"https://almaty.hh.kz/applicant/blacklist/state?vacancyId={VACANCY}",
    f"https://almaty.hh.kz/shards/vacancies/feedback/roulette?vacancyId={VACANCY}",
)

#: The real capture that broke the old detector: an ordinary hh page, 1.18 MB,
#: taken from the owner's signed-in session. Not committed — it is a logged-in
#: page — so the test that reads it checks it only where it exists.
PAGE_HTML = Path(__file__).resolve().parents[2] / "page.html"

#: hh ships its own error dictionary inside every page it serves, and one entry
#: is keyed ``error.signup.captcha.invalid``. Copied verbatim out of page.html,
#: escaping and non-breaking space included — the space is why the Russian
#: marker beside ``captcha`` did *not* also match, which was luck rather than
#: design. Every fake page below carries this line, so a detector that goes back
#: to reading the body fails every test in this file rather than one.
HH_ERROR_DICTIONARY = (
    "&#34;error.signup.captcha.invalid&#34;:&#34;Пожалуйста, подтвердите, что вы не робот&#34;"
)

#: The response modal exactly as it was measured on 2026-09-06 (vacancy
#: 136131345, ``agent/probe/_warn.json``). It carries both of hh's sentences at
#: once: the resume-visibility notice and hh's own analysis of why the
#: application would likely fail. The non-breaking spaces are hh's, and they are
#: the reason a comparison typed with ordinary spaces misses this text entirely.
#:
#: Renamed 2026-09-07 from ``BLOCKED_MODAL``. Nothing on it blocks: an
#: application was sent under this exact notice in real Chrome that day —
#: ``agent/evidence/20260907-send-under-visibility-notice.json`` — and the tests
#: below that used to assert it stopped the send now assert the opposite.
VISIBILITY_MODAL = (
    "Отклик на вакансию\n"
    "Python Backend Trainee\n"
    "Чтобы откликнуться на эту вакансию, поменяйте видимость резюме "
    "на «Видно компаниям-клиентам HeadHunter»\n"
    "Python-разработчик\n"
    "Такой отклик может получить отказ\n"
    "Английский язык в резюме «Python-разработчик» ниже обязательного "
    "уровня, который указал работодатель.\n"
    "Добавить сопроводительное\n"
    "Откликнуться"
)

#: The same modal without the visibility notice: hh says only that this
#: application will probably be turned down, and names the requirement it
#: misses. Never was a reason to stop.
REJECTION_MODAL = (
    "Отклик на вакансию\n"
    "Python Backend Trainee\n"
    "Python-разработчик\n"
    "Такой отклик может получить отказ\n"
    "Английский язык в резюме «Python-разработчик» ниже обязательного "
    "уровня, который указал работодатель.\n"
    "Добавить сопроводительное\n"
    "Откликнуться"
)

#: A modal with nothing to say. What the measured happy path looks like.
QUIET_MODAL = "Отклик на вакансию\nPython-разработчик\nДобавить сопроводительное\nОткликнуться"

#: A stand-in for the one selector nobody has seen. The fixture below supplies
#: it so the letter path can be exercised at all; the name is not a claim about
#: what hh calls that element, and nothing outside this file uses it.
LETTER_QA = "vacancy-response-letter-input"


def qa_of(selector: Selector) -> str:
    """The single ``data-qa`` a selector's query depends on.

    Read out of the query rather than written twice, so these fakes follow
    ``agent/selectors.py`` when it changes instead of drifting from it.
    """
    name, _ = selectors.names_in(selector.query)[0]
    return name


def a_state(
    *,
    applied: bool = False,
    has_tests: bool = False,
    letter_required: bool = False,
    closed: bool = False,
    vacancy_id: str = VACANCY,
) -> dict[str, Any]:
    """hh's boot payload, in the shape measured on live pages on 2026-09-06.

    ``negotiations.total`` is the applied signal and the only one. The key
    literally called ``alreadyApplied`` is here, and stays ``False`` even when
    an application exists, because that is what was measured on vacancy
    133542745 — a name is not a measurement, and a fixture that quietly agreed
    with the name would let a reader of this file believe the wrong thing.
    """
    topic = {"id": 1, "chatId": 2, "initialState": "RESPONSE", "lastState": "DISCARD"}
    return {
        "applicantVacancyResponseStatuses": {
            vacancy_id: {
                "test": {"hasTests": has_tests},
                "letterMaxLength": 10000,
                "shortVacancy": {"@responseLetterRequired": letter_required},
                "alreadyApplied": False,
                "negotiations": {
                    "topicList": [topic] if applied else [],
                    "total": 1 if applied else 0,
                },
            }
        },
        "vacancyView": {"closedForApplicants": closed, "archived": False},
    }


def a_contradictory_state(vacancy_id: str = VACANCY) -> dict[str, Any]:
    """hh answering the idempotency question two ways at once.

    ``negotiations.total`` says nothing was sent; ``topicList`` lists a
    conversation about this very vacancy. Nobody has measured what that means —
    ``state_page.Negotiations.exists`` says so in as many words — which is why
    ``exists`` resolves it toward stopping and why, since 2026-09-07, the stop is
    recorded as a disagreement rather than as an application.
    """
    state = a_state(applied=True, vacancy_id=vacancy_id)
    state["applicantVacancyResponseStatuses"][vacancy_id]["negotiations"]["total"] = 0
    return state


def a_page_html(state: dict[str, Any]) -> str:
    """A vacancy page carrying that state, the way hh boots its frontend."""
    blob = json.dumps(state, ensure_ascii=False).replace("&", "&amp;").replace("<", "&lt;")
    return (
        "<html><body>"
        f'<template id="HH-Lux-InitialState">{blob}</template>'
        f"<script>{{{HH_ERROR_DICTIONARY}}}</script>"
        "</body></html>"
    )


@dataclass
class FakeLocator:
    """One query resolved against what the page actually carries."""

    page: "FakePage"
    query: str
    #: Which of the page's controls this query matched, if any. ``None`` models
    #: the measured case that broke the probe: on a vacancy already applied to
    #: there is no «Откликнуться» element at all.
    control: str | None

    @property
    def first(self) -> "FakeLocator":
        """Playwright's ``.first``; a query naming two controls matches one."""
        return self

    def get_attribute(self, name: str) -> str | None:
        """Only ``href`` is ever asked for."""
        if name != "href" or self.control is None:
            return None
        return self.page.controls[self.control]

    def click(self) -> None:
        """Following the apply link, which is an ordinary GET navigation."""
        if self.control is None:
            raise RuntimeError(f"нечего кликать: {self.query}")
        self.page.used_control = self.control
        if self.page.navigate(f"https://hh.kz{self.page.controls[self.control]}"):
            self.page.visible.update(self.page.modal_parts())
            # hh talks to itself while its own modal opens. Inside the armed
            # window, which is where these used to be aborted as repeats.
            for beacon in self.page.beacons:
                self.page.navigate(beacon)

    def inner_text(self) -> str:
        """The modal's own words, which is what the classifier reads."""
        return self.page.modal_text


@dataclass
class FakePage:
    """Answers like hh and records what was done to it.

    Requests go through the gate exactly as playwright's ``context.route``
    would, so a gate that aborts a navigation produces here what it produces in
    Chromium: the page does not change.
    """

    gate: SubmitGate
    state: dict[str, Any]
    #: The state served after a successful submit **and a fresh page load**.
    #: None means "unchanged". Two conditions rather than one, because hh bakes
    #: its boot state into the document: re-reading the page the modal was
    #: opened over cannot show a number that changed after it was served.
    state_after_send: dict[str, Any] | None = None
    #: Where every navigation actually lands, when that is not where it was
    #: asked to go. hh's challenge and its sign-in wall are both redirects, and
    #: which address they redirect *to* is the whole difference between them.
    lands_on: str | None = None
    #: Which navigation the redirect above starts on. hh's own challenge arrived
    #: on request 173 of a measured run of 184, not on the first, so a scenario
    #: that redirects everything from the start is a different scenario — and
    #: the run's health check would meet it before a single vacancy was opened.
    lands_on_from: int = 0
    #: The page comes back without hh's boot state at all — a redesign, an error
    #: page, an interstitial. Never guessed at, always a stop.
    blank: bool = False
    #: What the submit click puts on the wire, if anything this gate can see.
    #: The default is an application-shaped URL because that is the shape a
    #: reader expects; ``None`` is the case **nobody has measured** — hh's submit
    #: going out as an XHR to a path this package does not recognise, or from a
    #: context ``context.route`` reports differently. It is a scenario rather
    #: than a claim, and it exists because the flow used to fail the whole
    #: application on it.
    send_url: str | None = SEND_URL
    #: hh's own beacons, fired once the response modal is open — i.e. inside the
    #: armed window. Measured shapes; see :data:`FURNITURE`.
    beacons: tuple[str, ...] = ()
    #: What the response modal says once it is open.
    modal_text: str = QUIET_MODAL
    #: What the modal says once «Добавить сопроводительное» has been clicked.
    #: None means it says the same thing it said before. hh raises warnings in
    #: response to what is typed, and the send is still ahead of that.
    modal_text_after_letter: str | None = None
    #: Which apply controls this page carries. The default is a fresh vacancy.
    controls: dict[str, str] = field(default_factory=dict)
    #: Clicks that reached a control, in order.
    clicks: list[str] = field(default_factory=list)
    filled: dict[str, str] = field(default_factory=dict)
    #: How many times the letter field answers "not editable" before it does.
    #: ``None`` means it never does.
    editable_after: int | None = 0
    #: What the letter field holds after typing, when that is not what was typed.
    typed_instead: str | None = None
    editability_checks: int = 0
    #: URLs the gate refused, so a test can tell a block from a miss.
    aborted: list[str] = field(default_factory=list)
    sent: bool = False
    #: Every page opened, in order. The first is the session health check.
    visited: list[str] = field(default_factory=list)
    #: One entry per time the window was raised, which is what a captcha does.
    fronted: list[str] = field(default_factory=list)
    listeners: dict[str, list[Any]] = field(default_factory=dict)
    #: Which apply control the flow actually used.
    used_control: str | None = None
    #: Every selector waited for, in order.
    waited: list[str] = field(default_factory=list)
    #: The ``data-qa`` names currently on the page.
    visible: set[str] = field(default_factory=set)
    #: Where the browser currently is, which is what the navigation helper reads.
    url: str = ""
    _served: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        """A fresh vacancy unless the test said otherwise."""
        if not self.controls:
            self.controls = {qa_of(selectors.APPLY_LINK): APPLY_HREF}
        self.visible.update(self.controls)

    def modal_parts(self) -> set[str]:
        """What appears once the response form has opened."""
        return {qa_of(selectors.RESPONSE_FORM), qa_of(selectors.SUBMIT_BUTTON)}

    def on(self, event: str, handler: Any) -> None:
        """Playwright's event registration. Used for the escape recorder."""
        self.listeners.setdefault(event, []).append(handler)

    def navigate(self, url: str) -> bool:
        """One request, routed through the gate like every other."""
        route = _Route(url)
        for handler in self.listeners.get("request", []):
            handler(route)
        self.gate.handle(route)
        if route.action == "abort":
            self.aborted.append(url)
            return False
        return True

    def goto(self, url: str, wait_until: str = "load", timeout: int = 0) -> None:
        """Opening a page. Not application-shaped, so the gate lets it by."""
        self.visited.append(url)
        self.navigate(url)
        redirected = self.lands_on is not None and len(self.visited) > self.lands_on_from
        self.url = self.lands_on if redirected and self.lands_on else url
        # A fresh document, so the boot state is whatever hh would serve now.
        self._served = self.state_after_send if self.sent and self.state_after_send else self.state
        self.visible = set(self.controls)

    def content(self) -> str:
        """The page as it was last served."""
        if self.blank:
            return "<html><body>hh redesigned this</body></html>"
        return a_page_html(self._served if self._served is not None else self.state)

    def wait_for_load_state(self, state: str, timeout: int = 0) -> None:
        """Only reached when a navigation was interrupted; nothing to do here."""

    def locator(self, query: str) -> FakeLocator:
        """Resolve a query against the controls this page carries.

        A query naming a control this page does not have resolves to nothing,
        which is the measured case: the plain «Откликнуться» is absent entirely
        from a vacancy that already has an application.
        """
        matched = [name for name in self.controls if f'"{name}"' in query]
        return FakeLocator(self, query, matched[0] if matched else None)

    def wait_for_selector(self, query: str, state: str = "visible", timeout: int = 0) -> None:
        """Present only once whatever reveals it has actually happened."""
        self.waited.append(query)
        if not any(f'"{name}"' in query for name in self.visible):
            raise TimeoutError(f"{query} не появился")

    def fill(self, query: str, text: str) -> None:
        """Typing the letter into the form."""
        if not self.is_editable(query):
            raise RuntimeError(f"{query} не принимает ввод")
        self.filled[query] = text

    def is_editable(self, query: str) -> bool:
        """The letter field takes input only after ``editable_after`` checks."""
        self.editability_checks += 1
        if self.editable_after is None:
            return False
        return self.editability_checks > self.editable_after

    def input_value(self, query: str) -> str:
        """What the field holds now."""
        if self.typed_instead is not None:
            return self.typed_instead
        return self.filled.get(query, "")

    def click(self, query: str) -> None:
        """The two buttons inside the modal."""
        self.clicks.append(query)
        if query == selectors.ADD_COVER_LETTER.query:
            self.visible.add(LETTER_QA)
            if self.modal_text_after_letter is not None:
                self.modal_text = self.modal_text_after_letter
            return
        self.sent = True
        if self.send_url is not None:
            self.navigate(self.send_url)

    def wait_for_timeout(self, ms: int) -> None:
        """The settle after the irreversible click. Nothing to wait for here."""

    def bring_to_front(self) -> None:
        """What the agent does when it sees a captcha, instead of solving it."""
        self.fronted.append("raised")


@dataclass
class _Route:
    """A playwright route over one URL."""

    url: str
    action: str | None = None

    @property
    def request(self) -> "_Route":
        """The route is its own request; the gate reads url and post_data."""
        return self

    @property
    def method(self) -> str:
        """Recorded, never used to decide."""
        return "GET"

    @property
    def post_data(self) -> str | None:
        """No body on a navigation."""
        return None

    def abort(self, error_code: str = "failed") -> None:
        """Refused."""
        self.action = "abort"

    def continue_(self) -> None:
        """Allowed."""
        self.action = "continue"


@pytest.fixture
def ready(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Stage 0 as it will look once the letter field has been measured too.

    Everything else keeps the real selectors and the real evidence rules: the
    file written here is built by the same :func:`agent.selectors.redact` the
    probe uses, and it records exactly the ``data-qa`` names the real selectors
    depend on, read out of their own queries. The only addition is the
    cover-letter field, which nobody has clicked through to yet — so the tests
    that check what happens *without* it must not use this fixture.
    """
    known = (
        selectors.APPLY_LINK,
        selectors.APPLY_LINK_AGAIN,
        selectors.VIEW_TOPIC,
        selectors.RESPONSE_FORM,
        selectors.SUBMIT_BUTTON,
        selectors.ADD_COVER_LETTER,
        selectors.CLOSE_RESPONSE_FORM,
        selectors.RESUME_TITLE,
        selectors.HIDDEN_RESUME_WARNING,
    )
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir(parents=True)
    (evidence_dir / f"{selectors.OPEN_FORM_EVIDENCE}.json").write_text(
        json.dumps(
            selectors.redact(
                [qa_of(one) for one in known] + [LETTER_QA],
                [],
                stage="open-form",
                authenticated=True,
            ),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    # The apply controls carry the other evidence file; one copy under both
    # names keeps every real selector verifiable without inventing a second run.
    (evidence_dir / f"{selectors.ALREADY_APPLIED_EVIDENCE}.json").write_text(
        (evidence_dir / f"{selectors.OPEN_FORM_EVIDENCE}.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    monkeypatch.setattr(selectors, "EVIDENCE_DIR", evidence_dir)

    letter_field = Selector(
        name="letter_field",
        query=f'[data-qa="{LETTER_QA}"]',
        scope=Scope.AUTHENTICATED,
        evidence=selectors.OPEN_FORM_EVIDENCE,
        checked_on=date(2026, 9, 6),
    )
    monkeypatch.setattr(selectors, "LETTER_FIELD", letter_field)
    monkeypatch.setattr(
        selectors, "REQUIRED_FOR_A_LETTER", (selectors.ADD_COVER_LETTER, letter_field)
    )
    yield


def a_mandate(letter: str | None = "Здравствуйте!") -> SendMandate:
    """A mandate as the confirmation would mint it."""
    return mint(vacancy_id=VACANCY, url=PAGE_URL, letter=letter, form_digest="what-was-shown")


# ── the blockers ──────────────────────────────────────────────────────


def test_an_application_can_actually_be_sent(ready: None) -> None:
    """The whole point, and it did not work.

    The apply control is a link to an application-shaped URL. Arming the gate
    only around the final click — as this did — meant the gate aborted the
    navigation that opens the form, ``wait_for_selector`` timed out, and every
    vacancy failed. Nothing could ever be sent.
    """
    gate = SubmitGate()
    page = FakePage(gate, a_state(), state_after_send=a_state(applied=True))

    warnings = submit.submit(page, a_mandate(), gate)

    assert page.aborted == [], f"the gate refused part of its own flow: {gate.refused_because}"
    # The letter field is revealed by a button, so it is two clicks and then a
    # third for the send — in that order.
    assert page.clicks == [
        selectors.ADD_COVER_LETTER.query,
        selectors.SUBMIT_BUTTON.query,
    ]
    assert page.filled == {selectors.LETTER_FIELD.query: "Здравствуйте!"}
    assert (warnings.visibility, warnings.likely_rejection) == (None, None)
    # Two application-shaped requests for one application: the form, then the send.
    assert len(gate.allowed) == 2
    gate.assert_no_escapes()


def test_a_send_hh_confirms_is_not_failed_because_the_gate_did_not_recognise_it(
    ready: None,
) -> None:
    """The blocker fixed on 2026-09-07, in the flow rather than on the gate alone.

    Nothing in this repository records what request hh's «Откликнуться» emits.
    ``require_progress`` refused when it saw none it recognised — after the click
    — so an application hh accepted through an unfamiliar request came back as a
    failure, ``run.py`` wrote ``failed``, and the owner was invited to send it a
    second time. That is the one mistake that cannot be undone.

    The page here sends nothing the gate can see and hh's own count then says an
    application exists. Restore the raise and this test fails.
    """
    gate = SubmitGate()
    page = FakePage(gate, a_state(), state_after_send=a_state(applied=True), send_url=None)

    submit.submit(page, a_mandate(), gate)

    assert page.sent
    # The gate saw the form open and nothing else, and says exactly that.
    assert [click.allowed for click in gate.submit_clicks] == [()]
    assert gate.submit_clicks[0].refused == ()
    assert gate.submit_clicks[0].vacancy_id == VACANCY


def test_hh_saying_nothing_arrived_is_still_the_thing_that_stops_a_send(ready: None) -> None:
    """The other direction of the same change, so this is not a check deleted.

    The authority moved to hh's own count rather than being removed. A page whose
    count does not move after the click is unconfirmed — and unconfirmed says the
    request may well have gone out, because it may have.
    """
    gate = SubmitGate()
    page = FakePage(gate, a_state(), send_url=None)

    with pytest.raises(IdempotencyUnknownError) as raised:
        submit.submit(page, a_mandate(), gate)

    assert str(raised.value) == submit.UNCONFIRMED


def test_the_letter_waits_for_the_field_to_take_input(ready: None) -> None:
    """Present is not ready: the modal renders in stages."""
    gate = SubmitGate()
    page = FakePage(gate, a_state(), state_after_send=a_state(applied=True), editable_after=3)

    submit.submit(page, a_mandate(), gate)

    assert page.filled == {selectors.LETTER_FIELD.query: "Здравствуйте!"}
    assert page.editability_checks > 3
    assert page.clicks[-1] == selectors.SUBMIT_BUTTON.query


def test_a_field_that_never_takes_input_sends_nothing(ready: None) -> None:
    """The form stays open, the submit button untouched, a person decides."""
    gate = SubmitGate()
    page = FakePage(gate, a_state(), editable_after=None)

    with pytest.raises(LetterNotTypedError) as raised:
        submit.submit(page, a_mandate(), gate)

    assert page.clicks == [selectors.ADD_COVER_LETTER.query]
    assert page.sent is False
    assert "не отправлен" in str(raised.value)


def test_a_letter_that_did_not_land_whole_sends_nothing(ready: None) -> None:
    """A half-typed letter is still an application, and it cannot be taken back."""
    gate = SubmitGate()
    page = FakePage(gate, a_state(), typed_instead="Здравс")

    with pytest.raises(LetterNotTypedError):
        submit.submit(page, a_mandate(), gate)

    assert selectors.SUBMIT_BUTTON.query not in page.clicks
    assert page.sent is False


def test_a_vacancy_archived_after_the_crawl_is_skipped_before_anything_is_clicked(
    ready: None,
) -> None:
    """The database is older than the page; the page decides.

    Measured 2026-09-16: 136105998 sat in apply_now with a letter while hh's own
    analytics reported ``active=false&archived=true&disabled=false`` for it and
    the page carried no apply control. Those are the three flags hh keeps in
    ``vacancyView.status``, the shape ``app/sources/hh.py`` reads when crawling.
    """
    gate = SubmitGate()
    state = a_state()
    state["vacancyView"] = {
        "closedForApplicants": False,
        "status": {"active": False, "archived": True, "disabled": False},
    }
    page = FakePage(gate, state)

    with pytest.raises(AlreadyAppliedError) as raised:
        submit.submit(page, a_mandate(), gate)

    assert "архив" in str(raised.value)
    assert page.clicks == []
    assert page.used_control is None
    assert gate.allowed == []


def test_hhs_own_beacons_do_not_interfere_with_the_flow_they_arrive_in(ready: None) -> None:
    """Measured over-match, driven through the real flow.

    hh's beacon, its blacklist check and its feedback survey each carry
    ``vacancyId``. Inside the armed window that used to mean the first of them
    consumed a slot and a repeat was aborted as «a second application»; since
    2026-09-16 none of them is application-shaped at all, because the rule is
    the path.
    """
    gate = SubmitGate()
    page = FakePage(gate, a_state(), state_after_send=a_state(applied=True), beacons=FURNITURE)

    submit.submit(page, a_mandate(), gate)

    assert page.aborted == [], f"the gate refused hh's own page: {gate.refused_because}"
    # Two application requests — the form and the send. hh's four beacons went
    # through without the gate treating them as applications at all.
    assert gate.requests_in_window() == 2
    assert len(gate.allowed) == 2
    assert gate.submit_clicks[0].allowed == (SEND_URL,)
    gate.assert_no_escapes()


def test_with_nothing_armed_a_beacon_proceeds_and_the_apply_link_does_not(
    ready: None,
) -> None:
    """The narrowing to the path must not have become permission to apply."""
    gate = SubmitGate()
    page = FakePage(gate, a_state(), beacons=FURNITURE)

    page.navigate(FURNITURE[0])
    page.navigate(SEND_URL)

    assert page.aborted == [SEND_URL]


def test_hh_contradicting_itself_stops_the_send_and_says_which_answer_it_gave(
    ready: None,
) -> None:
    """The unmeasured half of ``exists`` must not be filed as a measured fact.

    A count of zero beside a list of conversations stops the agent — that is
    right and it is unchanged. What changed on 2026-09-07 is that it no longer
    borrows «отклик уже отправлен», which is a claim about something that may not
    exist, on a shape whose own docstring says nobody has measured it.

    Revert it and this fails on the last two assertions: the sentence becomes the
    one a genuinely applied vacancy gets, and the vacancy is skipped for ever
    instead of going to a person who can look.
    """
    gate = SubmitGate()
    page = FakePage(gate, a_contradictory_state())

    with pytest.raises(IdempotencyUnknownError) as raised:
        submit.submit(page, a_mandate(), gate)

    said = str(raised.value)
    assert page.clicks == []
    assert VACANCY in said
    # Both halves of hh's answer, because the next person to meet this shape is
    # the one who can measure it.
    assert "0" in said and "переписка" in said
    assert "отклик уже отправлен" not in said


def test_the_two_ways_hh_can_say_applied_are_recorded_differently(
    ready: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same stop, two records, and a person can tell which rule produced which.

    The measured answer — hh's own count — skips the vacancy. The disagreement
    goes to a person, because a skip is terminal (``--requeue`` moves
    ``needs_manual`` and ``failed`` and nothing else) and burning a vacancy for
    ever on a shape nobody has measured is not a decision this code is entitled
    to make.
    """

    def outcome(name: str, state: dict[str, Any]) -> Entry:
        """One whole run against one page, in its own directory."""
        room = tmp_path / name
        room.mkdir()
        journal = _prepared_journal(room)
        gate = SubmitGate()
        _install(room, monkeypatch, journal, [FakePage(gate, state)])
        monkeypatch.setattr(run, "SubmitGate", lambda: gate)
        _confirms(monkeypatch, None)
        assert run.main(["--send", "--no-backend", "--queue", str(room / "queue.json")]) == 0
        entry = journal.get(VACANCY)
        assert entry is not None
        return entry

    measured = outcome("measured", a_state(applied=True))
    odd = outcome("odd", a_contradictory_state())

    assert measured.status is Status.SKIPPED
    assert odd.status is Status.NEEDS_MANUAL
    assert measured.reason != odd.reason


def test_a_sent_application_is_recorded_rather_than_crashing(
    ready: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The journal write after a send used to be an illegal transition.

    Nothing wrote ``confirmed``, so ``run.py`` moved a row from ``queued``
    straight to ``sent`` — a pair ``TRANSITIONS`` does not have. It raised after
    the application had irreversibly left: no ``sent`` row, no results file, no
    escape check, and the next run offered the same vacancy again.
    """
    journal = _prepared_journal(tmp_path)
    page = _run_one(ready, tmp_path, monkeypatch, journal, a_state(), a_state(applied=True))

    # The health check happens on the page the signal was measured on — a
    # vacancy page — and not on hh's front page, where a third of the recorded
    # keys do not exist and a live session therefore reads as expired.
    assert page.visited[0] == PAGE_URL
    entry = journal.get(VACANCY)
    assert entry is not None
    assert entry.status is Status.SENT
    results = json.loads((tmp_path / "queue-results.json").read_text(encoding="utf-8"))
    # Every key of the contract. Since 2026-09-16 a ``sent`` result carries the
    # evidence behind it: hh's own count, read off the page re-opened after the
    # click, and hh's word for the application it lists. The first real run
    # reported four sends with both empty, and a dashboard cannot tell such a row
    # from a send that never happened.
    [sent] = results["results"]
    assert sent == {
        "vacancy_id": VACANCY,
        "status": "sent",
        "reason": None,
        "sent_letter": sent["sent_letter"],
        "hh_warning": None,
        "negotiations_total": 1,
        "last_state": "DISCARD",
    }
    # The letter the mandate carried, which is the letter the person confirmed
    # and the only one ``submit`` could type.
    assert sent["sent_letter"] == page.filled[selectors.LETTER_FIELD.query]
    assert "Отправлено: 1" in capsys.readouterr().out


def test_a_captcha_stops_the_run_instead_of_walking_the_batch(
    ready: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The counter was assigned to itself on this branch, so the rule was dead.

    hh challenging the account meant every mandate in the batch was attempted,
    the window was raised once per vacancy, and the run exited 0 saying nothing
    was sent.
    """
    journal = _prepared_journal(tmp_path, count=4)
    page = _run_many(ready, tmp_path, monkeypatch, journal, captcha=True)

    assert len(page.fronted) == 2, "the run must stop after the second captcha, not walk the batch"
    assert "Две ошибки подряд" in capsys.readouterr().out


def test_a_challenge_before_the_run_starts_is_not_reported_as_an_expired_session(
    ready: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The health check meets it first, and the two failures look identical there.

    Both leave the page without the keys that mean "signed in", so the check
    below would say «войдите заново» — and the owner would go and sign in again
    for no reason, because they are already signed in and hh is asking whether
    they are a robot. A challenge is also a decision about the whole session,
    so it stops the run rather than costing one vacancy.
    """
    journal = _prepared_journal(tmp_path, count=4)
    gate = SubmitGate()
    page = FakePage(gate, a_state(), lands_on=CHALLENGE_URL)
    _install(tmp_path, monkeypatch, journal, [page])
    monkeypatch.setattr(run, "SubmitGate", lambda: gate)
    _confirms(monkeypatch, None)

    with pytest.raises(run.ChallengedError, match="проверку на робота"):
        run.main(["--send", "--no-backend", "--queue", str(tmp_path / "queue.json")])

    assert page.clicks == []
    assert journal.get(VACANCY) is not None
    assert journal.get(VACANCY).status is not Status.SENT  # type: ignore[union-attr]


def test_the_page_is_re_read_for_an_employer_test(ready: None) -> None:
    """An employer test is not in the queue, and it was never looked for.

    ``prefilter.decide`` had no production caller at all: the letter
    requirement, the test flag and the closed/archived re-read were described in
    ``submit``'s docstring and performed nowhere.
    """
    gate = SubmitGate()
    page = FakePage(gate, a_state(has_tests=True))

    with pytest.raises(IdempotencyUnknownError, match="тест"):
        submit.submit(page, a_mandate(), gate)

    assert page.clicks == []
    assert gate.allowed == []


def test_a_vacancy_that_closed_since_the_crawl_is_skipped(ready: None) -> None:
    """The queue is a snapshot; the page is the later witness."""
    gate = SubmitGate()
    page = FakePage(gate, a_state(closed=True))

    with pytest.raises(AlreadyAppliedError, match="закрыта"):
        submit.submit(page, a_mandate(), gate)

    assert page.clicks == []


def test_a_letter_that_became_required_stops_the_send(ready: None) -> None:
    """Measured off the page, because the queue cannot know it."""
    gate = SubmitGate()
    page = FakePage(gate, a_state(letter_required=True))

    with pytest.raises(IdempotencyUnknownError, match="письмо"):
        submit.submit(page, a_mandate(letter=None), gate)

    assert page.clicks == []


def test_an_already_applied_vacancy_is_never_applied_to_twice(ready: None) -> None:
    """The one mistake the owner cannot undo, checked against hh and not the journal.

    Decided by ``negotiations.total``. The fixture's ``alreadyApplied`` says
    ``false`` on this very page — as hh's did on the vacancy that was measured —
    and the apply control on it is «Отклик другим резюме», because hh permits a
    repeat. Neither of those is allowed to be the answer.
    """
    gate = SubmitGate()
    page = FakePage(
        gate,
        a_state(applied=True),
        controls={qa_of(selectors.APPLY_LINK_AGAIN): APPLY_HREF},
    )

    with pytest.raises(AlreadyAppliedError):
        submit.submit(page, a_mandate(), gate)

    assert page.clicks == []
    assert page.used_control is None


def test_a_captcha_raises_and_raises_the_window(ready: None) -> None:
    """Detection, never a solver. The window comes forward and a person deals with it."""
    gate = SubmitGate()
    page = FakePage(gate, a_state(), lands_on=CHALLENGE_URL)

    with pytest.raises(CaptchaPresentedError, match="проверку на робота"):
        submit.submit(page, a_mandate(), gate)

    assert page.fronted == ["raised"]
    assert page.clicks == []


def test_a_sign_in_wall_is_the_wrong_page_and_is_not_called_a_captcha(ready: None) -> None:
    """Both are redirects; only one of them is hh asking whether we are a robot.

    Telling them apart matters because the sentence a person reads is the whole
    output of this branch: «разберитесь с капчей» sends them looking for one
    that is not there, when what they have to do is sign in again.
    """
    gate = SubmitGate()
    page = FakePage(
        gate, a_state(), lands_on=f"https://hh.kz/account/login?backurl=%2Fvacancy%2F{VACANCY}"
    )

    with pytest.raises(submit.WrongVacancyError) as excinfo:
        submit.submit(page, a_mandate(), gate)

    assert page.fronted == []
    assert "agent.login" in str(excinfo.value)


def test_a_page_without_hhs_state_stops_rather_than_being_guessed_at(ready: None) -> None:
    """A redesign, an error page, an interstitial: none of them is an application.

    Reading nothing must never read as "nothing in the way". Everything this
    flow decides — the test, the letter, whether an application already exists —
    comes out of that blob, so its absence is the end of the road for this
    vacancy and a person picks it up.
    """
    gate = SubmitGate()
    page = FakePage(gate, a_state(), blank=True)

    with pytest.raises(IdempotencyUnknownError, match="состояние страницы"):
        submit.submit(page, a_mandate(), gate)

    assert page.clicks == []
    assert gate.allowed == []


def test_the_submitter_opens_the_page_the_human_was_shown(ready: None) -> None:
    """Not a URL rebuilt from the id against a hardcoded host.

    And it opens it twice: once to decide, once to ask hh whether the
    application now exists. hh bakes its boot state into the document, so the
    second read has to be a second document — re-reading the page the modal was
    opened over would report every successful application as unconfirmed.
    """
    gate = SubmitGate()
    page = FakePage(gate, a_state(), state_after_send=a_state(applied=True))

    submit.submit(page, a_mandate(), gate)

    assert page.visited == [PAGE_URL, PAGE_URL]


def test_the_visibility_notice_does_not_stop_the_application(ready: None) -> None:
    """Rewritten 2026-09-07 from ``test_an_application_hh_will_not_take_is_never_sent``.

    That test drove this exact card and asserted ``RefusedByHHError``, no click
    on the submit button and ``not page.sent``. It pinned a rule that came from
    a guess in a brief and was never measured — and because the rule blocked
    every application this package could make, it also forbade the one
    experiment that refutes it. The experiment was run on 2026-09-07 in real
    Chrome on the owner's account: the notice was on the card, the button was
    clicked, hh created the application. See
    ``agent/evidence/20260907-send-under-visibility-notice.json``.

    So the same card, down the same path, now asserts the opposite of what it
    used to — and asserts that hh's sentence comes back out with the result,
    because advice that does not reach the owner is advice nobody gave.
    """
    gate = SubmitGate()
    page = FakePage(
        gate,
        a_state(),
        state_after_send=a_state(applied=True),
        modal_text=VISIBILITY_MODAL,
    )

    warnings = submit.submit(page, a_mandate(letter=None), gate)

    assert page.sent, "hh accepts these applications; the agent must be able to send one"
    assert page.used_control == qa_of(selectors.APPLY_LINK)
    assert selectors.SUBMIT_BUTTON.query in page.clicks
    assert warnings.visibility is not None
    assert "видимость резюме" in warnings.visibility
    assert "Видно компаниям-клиентам HeadHunter" in warnings.visibility
    # And the other sentence on the same card is not lost behind it.
    assert warnings.likely_rejection is not None
    assert "Английский язык" in warnings.likely_rejection


def test_the_likely_rejection_warning_is_carried_out_of_the_send_rather_than_dropped(
    ready: None,
) -> None:
    """«Такой отклик может получить отказ» does not block, and must not vanish.

    It is hh's own analysis of why this application will probably fail and it
    names one unmet requirement, which is more specific than any score this
    project computes. Blocking on it would throw away applications the owner
    wants sent; dropping it would throw away the most useful sentence hh gives.
    """
    gate = SubmitGate()
    page = FakePage(
        gate,
        a_state(),
        state_after_send=a_state(applied=True),
        modal_text=REJECTION_MODAL,
    )

    warnings = submit.submit(page, a_mandate(letter=None), gate)

    assert page.sent
    assert warnings.visibility is None
    assert warnings.likely_rejection is not None
    assert "может получить отказ" in warnings.likely_rejection
    assert "Английский язык" in warnings.likely_rejection


def test_whichever_apply_control_the_page_carries_is_the_one_used(ready: None) -> None:
    """On an already-applied vacancy the plain control is absent entirely.

    Measured 2026-09-06. Waiting for it there is waiting for something that will
    never appear, which is how the probe's own open-form stage became
    unreachable by its own guard. Whether the repeat control is present decides
    nothing about idempotency — that was answered from a number, above.
    """
    gate = SubmitGate()
    page = FakePage(
        gate,
        a_state(),
        state_after_send=a_state(applied=True),
        controls={qa_of(selectors.APPLY_LINK_AGAIN): APPLY_HREF},
    )

    submit.submit(page, a_mandate(letter=None), gate)

    assert page.used_control == qa_of(selectors.APPLY_LINK_AGAIN)
    assert page.sent


def test_the_apply_control_must_belong_to_this_vacancy(ready: None) -> None:
    """A stale tab, a mis-scrolled list, a card from a "similar vacancies" block."""
    gate = SubmitGate()
    page = FakePage(
        gate,
        a_state(),
        controls={qa_of(selectors.APPLY_LINK): "/applicant/vacancy_response?vacancyId=999999999"},
    )

    with pytest.raises(submit.WrongVacancyError, match=VACANCY):
        submit.submit(page, a_mandate(letter=None), gate)

    assert not page.sent


def test_a_send_hh_does_not_confirm_is_reported_as_unconfirmed_not_as_unsent(
    ready: None,
) -> None:
    """The gate can say a request left; only hh can say an application exists.

    And the wording matters more than it looks: the request has already gone, so
    a message saying nothing happened is a message that gets this vacancy
    applied to twice.
    """
    gate = SubmitGate()
    page = FakePage(gate, a_state())  # hh still says total == 0 afterwards

    with pytest.raises(IdempotencyUnknownError) as excinfo:
        submit.submit(page, a_mandate(letter=None), gate)

    assert page.sent
    assert "мог быть создан" in str(excinfo.value)


# ── the captcha detector, which said yes to every page ────────────────


def test_the_measured_challenge_is_recognised_by_where_it_sends_you() -> None:
    """A 302 into ``/account/captcha`` is what hh answered a permitted GET with."""
    assert submit.looks_like_a_challenge(CHALLENGE_URL)
    assert submit.looks_like_a_challenge("https://hh.kz/account/captcha")


@pytest.mark.parametrize(
    "url",
    [
        PAGE_URL,
        "https://hh.kz/",
        # A path that merely starts with the same letters is not the same path.
        "https://hh.kz/accountancy/captcha",
        # The word in a query string is a search, not a challenge.
        "https://hh.kz/search/vacancy?text=captcha",
    ],
)
def test_an_ordinary_address_is_not_a_challenge(url: str) -> None:
    """Everything that is not that redirect is an ordinary page."""
    assert not submit.looks_like_a_challenge(url)


def test_a_page_that_merely_mentions_a_captcha_is_not_one(ready: None) -> None:
    """The defect, as a test: the old detector answered yes for every hh page.

    hh ships its own error dictionary inside the page, one entry is keyed
    ``error.signup.captcha.invalid``, and the old detector substring-matched the
    whole rendered document. So every vacancy raised before the form was ever
    touched, the window was brought to the front, and nothing could be sent at
    all. This drives the real flow over a page carrying that exact line.
    """
    gate = SubmitGate()
    page = FakePage(gate, a_state(), state_after_send=a_state(applied=True))
    assert "captcha" in page.content().casefold()

    submit.submit(page, a_mandate(letter=None), gate)

    assert page.sent
    assert page.fronted == []


def test_the_real_capture_that_broke_this_reads_as_an_ordinary_page() -> None:
    """page.html, when it is on this machine: 1.18 MB from a signed-in session.

    Not committed — it is a logged-in page and belongs on the owner's disk only
    — so this asserts against it where it exists and stays silent where it does
    not. The committable half of the same guarantee is the test above, which
    carries the one line out of this file that mattered.
    """
    if not PAGE_HTML.is_file():
        return
    html = PAGE_HTML.read_text(encoding="utf-8", errors="replace")

    assert "captcha" in html.casefold(), "the capture no longer contains the word it broke on"
    # It really is an ordinary hh page: it boots the frontend the usual way.
    assert read_state(html) is not None
    # And nothing about it is a challenge, because a challenge is an address.
    assert not submit.looks_like_a_challenge(PAGE_URL)


# ── the letter field nobody has measured ──────────────────────────────


def test_a_letter_is_never_typed_into_a_field_whose_evidence_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The field was measured on 2026-09-16; this is the state if that record goes.

    Guessing the field is the exact failure this package exists to prevent, so
    the vacancy goes to the owner — and the refusal names the one probe step that
    closes the gap.
    """
    unmeasured = Selector(name="letter_field", query="", scope=Scope.UNVERIFIED)
    monkeypatch.setattr(
        selectors, "REQUIRED_FOR_A_LETTER", (selectors.ADD_COVER_LETTER, unmeasured)
    )
    gate = SubmitGate()
    page = FakePage(gate, a_state())

    with pytest.raises(LetterFieldUnknownError) as excinfo:
        submit.submit(page, a_mandate(), gate)

    message = str(excinfo.value)
    assert "agent.probe_apply" in message
    assert "open-form" in message
    # Nothing was opened: the refusal costs no page load and no slot.
    assert page.visited == []


def test_a_vacancy_needing_a_letter_goes_to_the_owner_rather_than_failing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One vacancy out of the batch, not a run that stops.

    The refusal is a subclass of the stage-0 error, so a run that caught only
    the broad class would have stopped everything; caught narrowly it is a
    routing decision, and the whole daily budget is still available to the rest.
    """
    unmeasured = Selector(name="letter_field", query="", scope=Scope.UNVERIFIED)
    monkeypatch.setattr(
        selectors, "REQUIRED_FOR_A_LETTER", (selectors.ADD_COVER_LETTER, unmeasured)
    )
    journal = _prepared_journal(tmp_path)
    _run_one(None, tmp_path, monkeypatch, journal, a_state(), a_state(applied=True))

    entry = journal.get(VACANCY)
    assert entry is not None
    assert entry.status is Status.NEEDS_MANUAL
    assert entry.reason is not None
    assert "agent.probe_apply" in entry.reason


# ── what hh said, on its way to the next confirmation ─────────────────


def test_hhs_warning_reaches_the_results_and_the_journal(
    ready: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A sent application keeps hh's own analysis of it.

    In the results file as a field of its own, so whatever stores them can put
    it beside the match score rather than parse it out of a sentence; in the
    journal tagged as hh's words, so the next confirmation card can quote it as
    hh's rather than as this agent's.
    """
    journal = _prepared_journal(tmp_path)
    _run_one(
        ready,
        tmp_path,
        monkeypatch,
        journal,
        a_state(),
        a_state(applied=True),
        modal_text=REJECTION_MODAL,
    )

    results = json.loads((tmp_path / "queue-results.json").read_text(encoding="utf-8"))
    warning = results["results"][0]["hh_warning"]
    assert warning is not None and "Английский язык" in warning
    entry = journal.get(VACANCY)
    assert entry is not None and entry.reason is not None
    assert entry.reason.startswith(run.HH_QUOTE)
    assert "Английский язык" in entry.reason
    assert "hh предупреждает" in capsys.readouterr().out


def test_a_whole_run_sends_under_the_visibility_notice_and_carries_hhs_words(
    ready: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The Definition of Done, driven through ``run.main`` rather than the classifier.

    Rewritten 2026-09-07 from ``test_a_refusal_is_recorded_as_hhs_words_and
    _needs_a_person``, which asserted this run left the vacancy at
    ``needs_manual`` and sent nothing. It is the whole point of the change and it
    is deliberately not a unit test of ``read_form_warnings``: a classifier that
    returns the right dataclass proves nothing about whether ``run.main`` can get
    an application out of the door, and the rule this replaces lived in four
    places between the two.

    So this drives the real ``main()`` against the fakes, and asserts the three
    things that have to be true together: the application went out, the journal
    says ``sent``, and hh's sentence arrived with it — in the results file, in
    the journal tagged as hh's, and on screen — because it is now advice for a
    person rather than an instruction for the agent.
    """
    journal = _prepared_journal(tmp_path)
    page = _run_one(
        ready,
        tmp_path,
        monkeypatch,
        journal,
        a_state(),
        a_state(applied=True),
        modal_text=VISIBILITY_MODAL,
    )

    assert page.sent
    entry = journal.get(VACANCY)
    assert entry is not None
    assert entry.status is Status.SENT
    assert entry.reason is not None
    assert entry.reason.startswith(run.HH_QUOTE)
    assert "видимость резюме" in entry.reason
    # Both of hh's sentences, one per line, in the one column the journal has.
    assert "Английский язык" in entry.reason

    results = json.loads((tmp_path / "queue-results.json").read_text(encoding="utf-8"))
    assert results["results"][0]["status"] == Status.SENT.value
    warning = results["results"][0]["hh_warning"]
    assert warning is not None and "видимость резюме" in warning

    printed = capsys.readouterr().out
    assert "Отправлено: 1" in printed
    # Once for the batch, saying what the per-vacancy line cannot: this is about
    # the resume, so it is true of every application and not only of this one.
    assert "ВНИМАНИЕ" in printed
    assert "видимость резюме" in printed
    assert "про само" in printed


def test_what_hh_said_last_time_is_on_the_next_confirmation_card(
    ready: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The card is answered before the form opens, so hh's sentence arrives late.

    It reaches the person on the next round, through the journal — which is also
    why a run must stop overwriting that column with nothing every time it looks
    at a vacancy again.
    """
    journal = _prepared_journal(tmp_path)
    journal.record(
        Entry(VACANCY, Status.QUEUED, reason=f"{run.HH_QUOTE}Такой отклик может получить отказ"),
        actor=Actor.AGENT,
    )
    shown: list[Candidate] = []
    _run_one(
        ready,
        tmp_path,
        monkeypatch,
        journal,
        a_state(),
        a_state(applied=True),
        watch=shown,
    )

    assert [c.hh_warning for c in shown] == ["Такой отклик может получить отказ"]
    card = shown[0].render()
    assert "hh уже предупреждал об этой вакансии:" in card
    assert "Такой отклик может получить отказ" in card
    # This one is about the vacancy, so it does not claim to be about the resume.
    assert shown[0].hh_visibility is None
    assert human.VISIBILITY_HEADING not in card


def test_looking_at_a_vacancy_again_does_not_erase_what_hh_said_about_it(
    tmp_path: Path,
) -> None:
    """``Journal.record`` replaces that column on every write.

    So the run's own «this one is queued again» write used to wipe the last
    thing hh said, one step before the card that needed it. A later ``sent``
    row replacing it is a different matter and is correct: that warning was
    about an application that no longer needs deciding.
    """
    journal = _prepared_journal(tmp_path)
    said = f"{run.HH_QUOTE}Такой отклик может получить отказ"
    journal.record(Entry(VACANCY, Status.QUEUED, reason=said), actor=Actor.AGENT)
    item = QueueItem(vacancy_id=VACANCY, url=PAGE_URL, title="Python-разработчик")

    (candidate,) = run._to_candidates([item], journal)

    entry = journal.get(VACANCY)
    assert entry is not None and entry.reason == said
    assert candidate.hh_warning == "Такой отклик может получить отказ"


def test_the_card_never_attributes_this_agents_own_words_to_hh() -> None:
    """Only text tagged as hh's is quoted as hh's, and each half lands in its own place.

    The journal has one reason column and two kinds of sentence now go into it,
    so the split back out is part of the contract: the resume-visibility notice
    has to reach the field the card gives its own heading to, and everything
    else has to reach the field that says "about this vacancy". Getting it
    backwards would print «касается ВСЕХ откликов» over a remark about one
    employer's English requirement.
    """
    ours = Entry(VACANCY, Status.QUEUED, reason="не удалось прочитать состояние страницы")
    theirs = Entry(
        VACANCY, Status.QUEUED, reason=f"{run.HH_QUOTE}Такой отклик может получить отказ"
    )
    both = Entry(
        VACANCY,
        Status.QUEUED,
        reason=f"{run.HH_QUOTE}Поменяйте видимость резюме\nТакой отклик может получить отказ",
    )

    assert run._hh_words_in(ours) == (None, None)
    assert run._hh_words_in(None) == (None, None)
    assert run._hh_words_in(theirs) == (None, "Такой отклик может получить отказ")
    assert run._hh_words_in(both) == (
        "Поменяйте видимость резюме",
        "Такой отклик может получить отказ",
    )


def test_the_measured_modal_reads_the_way_the_flow_relies_on() -> None:
    """Both of hh's sentences at once, which is what the measured modal carried.

    Neither outranks the other and neither stops anything. The old version of
    this test ended by asserting ``decide_on_form`` returned ``MANUAL``; that
    stage is gone, because once the visibility notice stopped being a refusal a
    decision function on the modal could only ever answer ``PROCEED``.
    """
    # hh writes these lines with non-breaking spaces, and they are invisible in
    # an editor: a reformat that quietly turned them into ordinary ones would
    # leave this file testing a string hh does not send.
    assert " " in VISIBILITY_MODAL, "the measured text has lost hh's own spaces"

    warnings = state_page.read_form_warnings(VISIBILITY_MODAL)

    assert warnings.visibility is not None
    assert "видимость резюме" in warnings.visibility
    assert warnings.likely_rejection is not None
    assert "Английский язык" in warnings.likely_rejection
    assert warnings.said == (warnings.visibility, warnings.likely_rejection)
    assert not hasattr(prefilter, "decide_on_form")


# ── driving run.main ──────────────────────────────────────────────────


def _prepared_journal(tmp_path: Path, count: int = 1) -> Journal:
    """A queue file and an empty journal beside it."""
    items = [
        {
            "vacancy_id": str(int(VACANCY) + index),
            "url": f"https://almaty.hh.kz/vacancy/{int(VACANCY) + index}",
            "title": f"Python-разработчик {index}",
            "company": "Inspire",
            "letter": "Здравствуйте!",
        }
        for index in range(count)
    ]
    (tmp_path / "queue.json").write_text(
        json.dumps({"version": 1, "items": items}, ensure_ascii=False), encoding="utf-8"
    )
    return Journal(tmp_path / "agent.sqlite3")


def _install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, journal: Journal, pages: list[FakePage]
) -> None:
    """Point run.main at the temporary journal, and at fake pages instead of Chromium."""
    monkeypatch.setattr(run, "Journal", lambda path: journal)
    monkeypatch.setattr(run, "load_signal", lambda: ["applicantVacancyResponseStatuses"])
    monkeypatch.setattr(run, "signal_source", lambda: PAGE_URL)
    monkeypatch.setattr(run, "session_check", lambda state, *, signal: None)
    monkeypatch.setattr(time_module, "sleep", lambda seconds: None)
    # The working-hours rule is real and tested elsewhere; here it would make
    # the suite pass or fail depending on the time of day it is run.
    monkeypatch.setattr(
        Limits,
        "from_env",
        classmethod(lambda cls: cls(work_starts=time(0, 0), work_ends=time(23, 59))),
    )

    class FakeContext:
        """A browser context that hands out the prepared pages."""

        def new_page(self) -> FakePage:
            """The single tab the agent uses."""
            return pages[0]

        def route(self, pattern: str, handler: Any) -> None:
            """Registered for real; the pages call the gate themselves."""

    class Opened:
        """``open_browser`` as a context manager."""

        def __enter__(self) -> FakeContext:
            return FakeContext()

        def __exit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(run, "open_browser", lambda: Opened())


def _confirms(monkeypatch: pytest.MonkeyPatch, watch: list[Candidate] | None) -> None:
    """Stand in for the human, and let a test see the cards they were shown.

    The confirmation itself is exercised where it belongs — in
    ``test_boundaries.py``, over the real prompt — because a stub that answered
    it would be the flag the brief forbids.
    """

    def confirmed(candidates: list[Candidate]) -> list[SendMandate]:
        if watch is not None:
            watch.extend(candidates)
        return [
            mint(
                vacancy_id=c.vacancy_id,
                url=c.url,
                letter=None if c.letter is None else c.letter.text,
                form_digest="what-was-shown",
            )
            for c in candidates
        ]

    monkeypatch.setattr(run, "confirm", confirmed)


def _run_one(
    ready: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    journal: Journal,
    state: dict[str, Any],
    after: dict[str, Any],
    *,
    modal_text: str = QUIET_MODAL,
    watch: list[Candidate] | None = None,
) -> FakePage:
    """One confirmed application, all the way through ``run.main``."""
    gate = SubmitGate()
    page = FakePage(gate, state, state_after_send=after, modal_text=modal_text)
    _install(tmp_path, monkeypatch, journal, [page])
    monkeypatch.setattr(run, "SubmitGate", lambda: gate)
    _confirms(monkeypatch, watch)
    assert run.main(["--send", "--no-backend", "--queue", str(tmp_path / "queue.json")]) == 0
    return page


def _run_many(
    ready: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    journal: Journal,
    *,
    captcha: bool,
) -> FakePage:
    """A batch where every vacancy behaves the same way; returns the page used.

    How many vacancies were attempted is counted by how many times the window
    was raised, because raising it is exactly what a captcha does and exactly
    what the owner would sit through once per vacancy in the batch.
    """
    gate = SubmitGate()
    # The health check navigates first and is not challenged: hh's own
    # challenge arrived part way through a run, and a scenario where it is
    # there from the start is the one the run refuses outright, below.
    page = FakePage(gate, a_state(), lands_on=CHALLENGE_URL if captcha else None, lands_on_from=1)
    _install(tmp_path, monkeypatch, journal, [page])
    monkeypatch.setattr(run, "SubmitGate", lambda: gate)
    _confirms(monkeypatch, None)
    run.main(["--send", "--no-backend", "--queue", str(tmp_path / "queue.json")])
    return page


# ── what the human is asked to approve ────────────────────────────────


def test_the_confirmation_card_names_the_vacancy_and_shows_the_whole_letter() -> None:
    """Both were missing, and both are what the mandate binds itself to.

    Without the id the human approves a title and a link; the application goes
    to whatever ``vacancy_id`` says. With the letter cut at 200 characters the
    human approves text they have not read, in a message the backend wrote.
    """
    long_letter = "Здравствуйте! " + "Опыт работы с Python. " * 40
    candidate = Candidate(
        vacancy_id=VACANCY,
        title="Python-разработчик",
        company="Inspire",
        url=PAGE_URL,
        letter=check_letter(long_letter, required=False),
    )

    card = candidate.render()

    assert VACANCY in card
    assert long_letter.strip() in " ".join(card.replace("│", "").split())
    assert "…" not in card


def test_the_page_facts_are_read_from_the_key_hh_actually_uses() -> None:
    """The brief named four fields on ``vacancyView``; all four are null there."""
    facts = prefilter.read(a_state(has_tests=True, letter_required=True), VACANCY)

    assert facts is not None
    assert (facts.has_test, facts.letter_required, facts.letter_max_length) == (True, True, 10000)


def test_the_card_names_the_vacancy_on_its_own_line() -> None:
    """Not incidentally, inside the url. The id is what everything downstream acts on."""
    candidate = Candidate(
        vacancy_id=VACANCY,
        title="Python-разработчик",
        company="Inspire",
        url="https://almaty.hh.kz/vacancy/999999999",
        letter=None,
    )

    assert f"вакансия {VACANCY}" in candidate.render()


def test_a_malformed_drop_line_is_re_asked_rather_than_crashing() -> None:
    """«²» is a digit to ``str.isdigit`` and not to ``int``.

    The ValueError escaped the re-ask loop, the confirmation and ``main()``, so
    a typo ended the run in a traceback — after the batch had been printed and
    before anything could be sent.
    """
    candidate = Candidate(VACANCY, "Python-разработчик", "Inspire", PAGE_URL, None)
    typed = io.StringIO("²\n1\n")
    shown = io.StringIO()

    with pytest.raises(CancelledError, match="не осталось"):
        confirm([candidate], stream_in=typed, stream_out=shown)

    assert "Нужны номера от 1 до 1" in shown.getvalue()


def test_the_journal_will_not_create_a_row_already_sent(tmp_path: Path) -> None:
    """The transition table governs moves; a first write had no source to look up.

    So one call could put a row straight into ``sent`` — which counts against
    the daily cap — or into ``confirmed``, the status reserved for a human.
    """
    journal = Journal(tmp_path / "agent.sqlite3")

    for forbidden in (Status.SENT, Status.CONFIRMED):
        with pytest.raises(IllegalInitialStatusError):
            journal.record(Entry(VACANCY, forbidden), actor=Actor.AGENT)

    assert journal.get(VACANCY) is None


def test_a_queue_item_whose_url_and_id_disagree_is_refused() -> None:
    """The human reads the url; everything else acts on the id."""
    with pytest.raises(QueueFormatError, match="не совпадает"):
        QueueItem.from_json(
            {"vacancy_id": "999999999", "url": PAGE_URL, "title": "Python-разработчик"}
        )

    # A url with no id in it is not a contradiction, only an absence.
    kept = QueueItem.from_json(
        {"vacancy_id": VACANCY, "url": "https://hh.kz/redirect?to=x", "title": "т"}
    )
    assert kept.vacancy_id == VACANCY


def test_the_session_is_checked_on_the_page_the_signal_was_measured_on(tmp_path: Path) -> None:
    """login.py records the keys of a vacancy page; the run asserted them on the home page.

    Fifteen of the sixty captured keys do not exist on hh's front page at all,
    so a live session read as expired and the run refused forever.
    """
    signal = tmp_path / "session_signal.json"
    signal.write_text(
        json.dumps({"probe_url": PAGE_URL, "appeared_after_login": ["applicantNegotiations"]}),
        encoding="utf-8",
    )

    assert session.signal_source(signal) == PAGE_URL
    assert session.load_signal(signal) == ["applicantNegotiations"]
    # An older signal file without the field still works.
    signal.write_text(json.dumps({"appeared_after_login": ["x"]}), encoding="utf-8")
    assert session.signal_source(signal) == login.PROBE_URL


class _ProbeContext:
    """A browser context that hands its route handler to the test."""

    def __init__(self, captured: dict[str, Any], page: Any) -> None:
        self.captured = captured
        self.page = page

    def route(self, pattern: str, handler: Any) -> None:
        """What ``open_form`` installs; the test drives it directly."""
        self.captured["guard"] = handler

    def new_page(self) -> Any:
        """The single tab."""
        return self.page


class _Opened:
    """``open_browser`` as a context manager."""

    def __init__(self, context: _ProbeContext) -> None:
        self.context = context

    def __enter__(self) -> _ProbeContext:
        return self.context

    def __exit__(self, *exc: object) -> None:
        return None


@dataclass
class _ProbeLocator:
    """``page.locator(q).first`` and the two things the probe does with it."""

    page: "_ProbePage"
    query: str

    @property
    def first(self) -> "_ProbeLocator":
        """The probe always takes the first match."""
        return self

    def click(self, timeout: int = 0) -> None:
        """Whatever this query names, clicked."""
        self.page.clicked(self.query)

    def inner_text(self) -> str:
        """The modal's own words, which is what the classifier reads."""
        return "Отклик на вакансию" if self.page.opened else ""


class _ProbePage:
    """A page that reveals the response form once the navigation is allowed."""

    def __init__(self, captured: dict[str, Any], reached: list[str]) -> None:
        self.captured = captured
        self.reached = reached
        self.opened = False
        self.letter_open = False
        self.url = PAGE_URL
        #: Every selector waited for, in order. The stage used to wait on a
        #: clock instead and photographed the page before the modal existed.
        self.waited: list[str] = []

    def on(self, event: str, handler: Any) -> None:
        """The escape recorder."""

    def goto(self, url: str, wait_until: str = "load", timeout: int = 0) -> None:
        """Opening the vacancy page."""
        self.url = url

    def wait_for_load_state(self, state: str, timeout: int = 0) -> None:
        """Only reached when a navigation was interrupted; nothing to do here."""

    def locator(self, query: str) -> _ProbeLocator:
        """Every control the probe touches goes through this."""
        return _ProbeLocator(self, query)

    def clicked(self, query: str) -> None:
        """Following the apply link, or opening the letter field."""
        if "vacancy-response-link-top" in query:
            route = _Route(f"https://hh.kz{APPLY_HREF}")
            self.captured["guard"](route)
            if route.action == "continue":
                self.reached.append(route.url)
                self.opened = True
        elif "add-cover-letter" in query:
            self.letter_open = True

    def wait_for_selector(self, query: str, timeout: int = 0) -> None:
        """Present once whatever reveals it has been clicked."""
        self.waited.append(query)
        if "textarea" in query and not self.letter_open:
            raise RuntimeError("Timeout: waiting for a letter field nobody opened")
        if not self.opened:
            raise RuntimeError(f"Timeout: {query}")

    def eval_on_selector_all(self, query: str, script: str) -> list[dict[str, str]]:
        """The form controls, which is how the letter field gets measured."""
        if not self.letter_open:
            return []
        return [{"tag": "TEXTAREA", "qa": LETTER_QA}]

    def content(self) -> str:
        """The error page, the modal, or the modal with the letter field open."""
        if not self.opened:
            return "<html><body>chrome error</body></html>"
        letter = f'<textarea data-qa="{LETTER_QA}"></textarea>' if self.letter_open else ""
        return (
            '<html><body><div data-qa="modal-overlay">'
            '<div data-qa="resume-title">Python-разработчик</div>'
            '<div data-qa="hidden-resume-warning"></div>'
            f'<button data-qa="add-cover-letter"></button>{letter}'
            '<button data-qa="vacancy-response-submit-popup">Откликнуться</button>'
            "</div></body></html>"
        )


def test_the_probe_can_reach_the_form_it_exists_to_photograph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stage 0 aborted its own navigation, so its report came back empty.

    The whole package was unblockable by the procedure its README documents:
    the form's selectors can only be seen after the click, and the click was
    cancelled by the guard meant to protect it.
    """
    captured: dict[str, Any] = {}
    reached: list[str] = []
    page = _ProbePage(captured, reached)
    monkeypatch.setattr(probe_apply, "open_browser", lambda: _Opened(_ProbeContext(captured, page)))

    report = probe_apply.open_form(PAGE_URL, already_applied=True)

    assert reached, "the guard aborted the navigation this stage exists to make"
    assert "vacancy-response-submit-popup" in report["data_qa_after_click"]["candidates"]
    assert '[data-qa="vacancy-response-submit-popup"]' in report["selectors_ready_to_paste"]
    assert report["requests_escaped_interception"] == []


def test_the_probe_waits_for_the_modal_instead_of_for_a_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The measured reason an earlier report was empty of every modal name.

    The modal renders later than the two seconds that run waited, so the page
    was photographed before it existed. Waiting on the selector is the fix, and
    this asserts the stage does that rather than sleeping.
    """
    captured: dict[str, Any] = {}
    page = _ProbePage(captured, [])
    monkeypatch.setattr(probe_apply, "open_browser", lambda: _Opened(_ProbeContext(captured, page)))

    probe_apply.open_form(PAGE_URL, already_applied=False)

    assert selectors.RESPONSE_FORM.query in page.waited
    assert selectors.SUBMIT_BUTTON.query in page.waited
    source = Path(probe_apply.__file__).read_text(encoding="utf-8")
    assert "wait_for_timeout" not in source, "a fixed wait is what produced an empty report"


def test_the_probe_opens_the_letter_field_so_it_can_be_measured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one selector nobody has seen is behind a second click.

    ``add-cover-letter`` reveals the letter field; before this the stage never
    clicked it, so no textarea appeared in any dump and the selector could only
    have been guessed.
    """
    captured: dict[str, Any] = {}
    page = _ProbePage(captured, [])
    monkeypatch.setattr(probe_apply, "open_browser", lambda: _Opened(_ProbeContext(captured, page)))

    report = probe_apply.open_form(PAGE_URL, already_applied=False)

    assert page.letter_open, "the letter button was never clicked"
    after = report["data_qa_after_letter_click"]["candidates"]
    assert LETTER_QA in after
    assert LETTER_QA not in report["data_qa_after_click"]["candidates"]
    assert report["form_controls_after_letter_click"]


def test_the_probe_still_refuses_another_vacancys_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """Letting this vacancy through is not letting everything through."""
    captured: dict[str, Any] = {}
    page = _ProbePage(captured, [])
    monkeypatch.setattr(probe_apply, "open_browser", lambda: _Opened(_ProbeContext(captured, page)))
    probe_apply.open_form(PAGE_URL, already_applied=True)

    stranger = _Route("https://hh.kz/applicant/vacancy_response?vacancyId=999999999")
    captured["guard"](stranger)
    mine = _Route("https://hh.kz/applicant/vacancy_response?vacancyId=" + VACANCY)
    captured["guard"](mine)

    assert (stranger.action, mine.action) == ("abort", "continue")


def test_the_probe_cannot_send_because_nothing_but_a_get_leaves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What replaced the ``--already-applied`` refusal, now that the click was measured.

    Following the apply link was measured on 2026-09-06 to send nothing, so the
    stage no longer refuses fresh vacancies. What holds instead is a method
    check: an application is sent with a non-GET request, and this aborts every
    one of those — including one that names this very vacancy, which the URL
    check on its own would allow through.
    """
    captured: dict[str, Any] = {}
    page = _ProbePage(captured, [])
    monkeypatch.setattr(probe_apply, "open_browser", lambda: _Opened(_ProbeContext(captured, page)))
    probe_apply.open_form(PAGE_URL, already_applied=False)

    send = _PostRoute(f"https://hh.kz/applicant/vacancy_response?vacancyId={VACANCY}")
    captured["guard"](send)

    assert send.action == "abort"


@dataclass
class _PostRoute(_Route):
    """The shape of an actual send: same URL, different method."""

    @property
    def method(self) -> str:
        """A non-GET, which is what an application is."""
        return "POST"


#: Text that is ordinary on hh.KZ and does not exist in cp1251. The Kazakh
#: letters ә ғ қ ң ө ұ ү һ are in every other company name in Almaty, and an
#: emoji in a job title is a marketing decision somebody makes every day.
#: Written as a literal rather than escapes so that a reader can see what it is,
#: and asserted below to be genuinely unprintable so this file cannot quietly
#: stop testing anything.
KAZAKH_COMPANY = "Ұлттық Қорғаныс — Әрқашан"
EMOJI_TITLE = "Python-разработчик 🚀"


def test_the_card_prints_on_the_console_this_actually_runs_on() -> None:
    """A Russian Windows console encodes cp1251, and the card is the first thing shown.

    **This test used to pass with the defect in front of it**, and how it did is
    the interesting half. It reduced hh's warning with ``state_page.printable``
    *itself*, before handing it to the card, and then gave the card a title, an
    employer and a letter that were all inside cp1251 anyway — so it asserted
    that a function which reduces nothing had reduced its input. The three
    fields ``render`` prints straight from the queue were never tested, and one
    Kazakh letter in an employer's name killed the run at the confirmation
    prompt.

    So everything here goes in raw, and every field that is not ours is a field
    hh could really send.
    """
    warning = "Такой отклик может получить отказ 🙃"
    # The visibility notice is on the card too since 2026-09-07, under its own
    # heading, and it reaches ``render`` through the journal rather than straight
    # from ``read_form_warnings`` — so it is a field that is not ours either.
    visibility = "Поменяйте видимость резюме 🙈"
    for hazard in (KAZAKH_COMPANY, EMOJI_TITLE, warning, visibility):
        with pytest.raises(UnicodeEncodeError):
            # Otherwise this test proves nothing: a card assembled out of text
            # the console can already print encodes whatever render() does.
            hazard.encode("cp1251")

    candidate = Candidate(
        vacancy_id=VACANCY,
        title=EMOJI_TITLE,
        company=KAZAKH_COMPANY,
        url=PAGE_URL,
        letter=check_letter("Здравствуйте!\nОпыт — Python, FastAPI ✨", required=False),
        hh_warning=warning,
        hh_visibility=visibility,
    )

    card = candidate.render()

    card.encode("cp1251")
    # Including the fixed heading this package wrote itself.
    human.VISIBILITY_HEADING.encode("cp1251")
    # And the reader is told the card is a rendering rather than the payload:
    # the letter that will be sent still carries the characters shown as «?».
    assert "часть символов не в кодировке консоли" in card
    assert candidate.letter is not None
    assert "✨" in candidate.letter.text


def test_what_is_said_once_for_the_batch_prints_on_that_console_too() -> None:
    """The summary quotes hh, so it is text this program did not write.

    ``render`` reduces the card it builds, but this line is printed outside any
    card and would otherwise be the one sentence in the prompt that can end a run
    with ``UnicodeEncodeError`` — at the moment the owner is being asked to
    agree, which is exactly where that has happened before.
    """
    hazard = "Поменяйте видимость резюме 🙈"
    with pytest.raises(UnicodeEncodeError):
        hazard.encode("cp1251")
    shown = io.StringIO()

    confirm(
        [Candidate(VACANCY, "Python", None, PAGE_URL, None, hh_visibility=hazard)],
        stream_in=io.StringIO(f"\n{CONFIRM_WORD}\n"),
        stream_out=shown,
    )

    shown.getvalue().encode("cp1251")


def test_the_run_s_own_summary_prints_on_that_console_too(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The same for the line printed after «Отправлено: N».

    It quotes hh out of the results file, which is the far end of a journey that
    starts in an employer's browser, and the fixed half of it is this package's
    own Russian. Both have to survive the codepage the owner's console encodes.
    """
    hazard = "Поменяйте видимость резюме 🙈"

    run._say_what_hh_said_about_the_resume([Result(VACANCY, Status.SENT.value, hh_warning=hazard)])

    capsys.readouterr().out.encode("cp1251")


def test_a_card_that_needs_no_reduction_says_nothing_about_one() -> None:
    """The note is a fact about this card, not a disclaimer printed on every one."""
    candidate = Candidate(
        vacancy_id=VACANCY,
        title="Python-разработчик",
        company="Inspire",
        url=PAGE_URL,
        letter=check_letter("Здравствуйте! Опыт — Python, FastAPI…", required=False),
        hh_warning=state_page.printable(VISIBILITY_MODAL),
    )

    card = candidate.render()

    card.encode("cp1251")
    assert "часть символов" not in card


# ── one vacancy, one application ──────────────────────────────────────


def test_a_vacancy_named_twice_in_one_queue_is_one_candidate(tmp_path: Path) -> None:
    """The measured blocker: ``_to_candidates`` deduplicated nothing.

    Its own write puts the row at ``queued``, and ``queued`` is exactly the
    status it reads as "not dealt with yet" — so the second row naming the same
    vacancy walked through the guard, and the run confirmed and armed twice.
    The journal cannot answer this question; only the loop that builds the batch
    can.
    """
    journal = Journal(tmp_path / "agent.sqlite3")
    item = QueueItem(vacancy_id=VACANCY, url=PAGE_URL, title="Python-разработчик")

    candidates = run._to_candidates([item, item, item], journal)

    assert [c.vacancy_id for c in candidates] == [VACANCY]


def test_a_duplicated_queue_sends_one_application_and_finishes(
    ready: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole chain, because the duplicate broke two things and not one.

    Two candidates meant two mandates; ``SubmitGate`` resets its window per
    arming — correctly, one confirmation is one window — so the gate allowed
    both. hh's own state caught the second attempt here, and then the journal
    write recording that skip raised ``sent -> skipped`` and took the run down
    with it: no results file, no escape check, on a run that had already sent an
    application.
    """
    item = {
        "vacancy_id": VACANCY,
        "url": PAGE_URL,
        "title": "Python-разработчик",
        "company": "Inspire",
    }
    (tmp_path / "queue.json").write_text(
        json.dumps({"version": 1, "items": [item, item]}, ensure_ascii=False), encoding="utf-8"
    )
    journal = Journal(tmp_path / "agent.sqlite3")
    gate = SubmitGate()
    page = FakePage(gate, a_state(), state_after_send=a_state(applied=True))
    _install(tmp_path, monkeypatch, journal, [page])
    monkeypatch.setattr(run, "SubmitGate", lambda: gate)
    _confirms(monkeypatch, None)

    assert run.main(["--send", "--no-backend", "--queue", str(tmp_path / "queue.json")]) == 0

    # One application: the GET that opens the form and the send it makes.
    assert len(gate.allowed) == 2
    assert page.clicks.count(selectors.SUBMIT_BUTTON.query) == 1
    entry = journal.get(VACANCY)
    assert entry is not None and entry.status is Status.SENT
    results = json.loads((tmp_path / "queue-results.json").read_text(encoding="utf-8"))
    assert [r["vacancy_id"] for r in results["results"]] == [VACANCY]


def test_the_confirmation_refuses_to_mint_two_mandates_for_one_vacancy() -> None:
    """The last place a duplicate can be caught, because minting is what it becomes.

    ``run.py`` deduplicates the batch; if it ever stops, this is the floor. Two
    mandates for one vacancy are indistinguishable to the gate — and must be,
    because one confirmation is one window.
    """
    candidate = Candidate(VACANCY, "Python-разработчик", "Inspire", PAGE_URL, None)

    with pytest.raises(CancelledError, match="дважды"):
        confirm(
            [candidate, candidate],
            stream_in=io.StringIO(f"\n{CONFIRM_WORD}\n"),
            stream_out=io.StringIO(),
        )


# ── the run's record of what it did ───────────────────────────────────


def test_a_journal_refusal_no_longer_destroys_the_record_of_the_run(
    ready: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The write that says why one vacancy failed was itself able to raise.

    Measured as ``sent -> skipped``: a duplicate had already moved the row to
    ``sent``, the second attempt was skipped, and recording the skip raised out
    of the loop, out of ``with open_browser()`` and out of ``main()`` — on a run
    where an application had already gone out. ``queue.report`` and
    ``gate.assert_no_escapes()`` sit after that block and never ran.

    Here the journal refuses the same transition. The refusal is reported rather
    than swallowed — it is printed and the run exits non-zero — and the record
    survives it.
    """
    journal = _prepared_journal(tmp_path)
    real_record = journal.record

    def refusing(entry: Entry, *, actor: Actor) -> None:
        if entry.status is Status.SKIPPED:
            raise IllegalTransitionError(Status.SENT, entry.status, actor)
        real_record(entry, actor=actor)

    monkeypatch.setattr(journal, "record", refusing)
    gate = SubmitGate()
    # hh says an application already exists, so the run skips this vacancy —
    # and recording that skip is the write the journal refuses.
    page = FakePage(gate, a_state(applied=True))
    _install(tmp_path, monkeypatch, journal, [page])
    monkeypatch.setattr(run, "SubmitGate", lambda: gate)
    _confirms(monkeypatch, None)

    assert run.main(["--send", "--no-backend", "--queue", str(tmp_path / "queue.json")]) == 1

    results = json.loads((tmp_path / "queue-results.json").read_text(encoding="utf-8"))
    assert [r["status"] for r in results["results"]] == [Status.SKIPPED.value]
    assert "журнал не принял" in capsys.readouterr().err


def test_a_confirmed_vacancy_the_run_never_reached_is_not_left_stranded(
    ready: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``confirmed`` has no way out except this loop, and the loop can stop early.

    Two captchas in a row end the run with confirmations unspent. Those rows
    stayed at ``confirmed`` for ever: every later run skipped them, because
    ``_to_candidates`` only builds a card for a row at ``queued`` and nothing —
    not even a person — may move a row out of ``confirmed``. Recorded as
    ``failed`` they are both truthfully described and reachable again, by hand.
    """
    journal = _prepared_journal(tmp_path, count=4)
    _run_many(ready, tmp_path, monkeypatch, journal, captcha=True)

    stranded = [entry.vacancy_id for entry in journal.by_status(Status.CONFIRMED)]
    assert stranded == []
    never_reached = journal.by_status(Status.FAILED)
    assert [e.vacancy_id for e in never_reached] == [str(int(VACANCY) + 2), str(int(VACANCY) + 3)]
    assert all(e.reason is not None and "не дойдя" in e.reason for e in never_reached)


def test_a_confirmation_the_journal_will_not_record_is_never_acted_on(
    ready: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No ``confirmed`` row, no send — because the ``sent`` write is defined against it.

    The run used to write the confirmations and walk on regardless of what the
    journal did with them, which is how a ``queued -> sent`` pair the state
    machine does not have came to be attempted after an application had already
    left.
    """
    journal = _prepared_journal(tmp_path)
    real_record = journal.record

    def refusing(entry: Entry, *, actor: Actor) -> None:
        if entry.status is Status.CONFIRMED:
            raise IllegalTransitionError(Status.QUEUED, entry.status, actor)
        real_record(entry, actor=actor)

    monkeypatch.setattr(journal, "record", refusing)
    gate = SubmitGate()
    page = FakePage(gate, a_state(), state_after_send=a_state(applied=True))
    _install(tmp_path, monkeypatch, journal, [page])
    monkeypatch.setattr(run, "SubmitGate", lambda: gate)
    _confirms(monkeypatch, None)

    assert run.main(["--send", "--no-backend", "--queue", str(tmp_path / "queue.json")]) == 1

    assert page.clicks == []
    assert gate.allowed == []
    results = json.loads((tmp_path / "queue-results.json").read_text(encoding="utf-8"))
    assert results["results"] == [
        {
            "vacancy_id": VACANCY,
            "status": Status.FAILED.value,
            "reason": "журнал не принял подтверждение — отправка не начиналась",
            "sent_letter": None,
            "hh_warning": None,
            "negotiations_total": None,
            "last_state": None,
        }
    ]
    assert "журнал не принял" in capsys.readouterr().err


@dataclass
class _LeakyPage(FakePage):
    """A page where one application request never reaches the interceptor.

    What a service worker would do: ``page.on("request")`` sees it and
    ``context.route`` does not. ``agent/browser.py`` blocks service workers for
    exactly this reason, and the gate keeps the independent record so that a day
    when blocking them stops working is a day this notices.
    """

    def goto(self, url: str, wait_until: str = "load", timeout: int = 0) -> None:
        """Open a page, and let one request out around the router."""
        super().goto(url, wait_until, timeout)
        self.gate.observe(_Route(f"{SEND_URL}&sw=1"))


def test_an_escape_is_raised_only_after_the_record_has_been_written(
    ready: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An escape means nothing this run says about consent can be trusted.

    It still has to be raised — that is unchanged — but it used to be raised
    from a line that a failure anywhere in the browser block skipped, and it sat
    after the results file for the same reason. Both now happen whatever ends
    the run.
    """
    journal = _prepared_journal(tmp_path)
    gate = SubmitGate()
    page = _LeakyPage(gate, a_state(), state_after_send=a_state(applied=True))
    _install(tmp_path, monkeypatch, journal, [page])
    monkeypatch.setattr(run, "SubmitGate", lambda: gate)
    _confirms(monkeypatch, None)

    with pytest.raises(InterceptionEscapedError, match="мимо перехватчика"):
        run.main(["--send", "--no-backend", "--queue", str(tmp_path / "queue.json")])

    assert (tmp_path / "queue-results.json").is_file()


def test_a_run_that_ends_in_a_challenge_still_writes_down_what_it_did(
    ready: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two things had to survive the exception, and neither did.

    The results file is the only thing the queue's owner ever sees, and an
    escape that happens on a run ending in a challenge is the most serious
    thing this package can detect. Both sat after the ``with open_browser()``
    block. Here the run ends in a challenge *and* something got out around the
    interceptor: the challenge is what the owner reads, because it is what they
    have to act on, and the escape is printed rather than thrown away.
    """
    journal = _prepared_journal(tmp_path, count=2)
    gate = SubmitGate()
    page = _LeakyPage(gate, a_state(), lands_on=CHALLENGE_URL)
    _install(tmp_path, monkeypatch, journal, [page])
    monkeypatch.setattr(run, "SubmitGate", lambda: gate)
    _confirms(monkeypatch, None)

    with pytest.raises(run.ChallengedError):
        run.main(["--send", "--no-backend", "--queue", str(tmp_path / "queue.json")])

    results = json.loads((tmp_path / "queue-results.json").read_text(encoding="utf-8"))
    # Nothing was attempted, and both confirmations are on record as unused
    # rather than left at `confirmed`, where nothing could ever move them.
    assert [r["status"] for r in results["results"]] == [Status.FAILED.value] * 2
    assert "мимо перехватчика" in capsys.readouterr().err


# ── hh's warning, all the way to the next card ────────────────────────


def test_what_a_person_requeues_brings_hhs_words_back_to_the_next_card(
    ready: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The carry-over used to be unreachable, and this is the path that reaches it.

    A row set aside for a person sits at ``needs_manual``; ``_to_candidates``
    builds a card only for a row at ``queued``; and nothing in the package
    performed the one move between them, which ``agent.state`` reserves for a
    human. So the lookup was never non-None outside a test, and whatever hh had
    said about that application could not reach the person deciding whether to
    send it again. ``--requeue`` is that human move, and it keeps the reason
    column instead of replacing it with nothing.

    Rewritten 2026-09-07, in the half that had become fiction. It used to say
    the row got here because hh refused it over the resume's visibility; that
    is not a refusal and no longer produces this row. The sentence is still the
    one used, because it is the one that must land in the field the card gives
    its own heading to — and this is the path on which an old row, written while
    the rule still stood, comes back to a person.
    """
    journal = _prepared_journal(tmp_path)
    said = "Чтобы откликнуться, поменяйте видимость резюме"
    journal.record(Entry(VACANCY, Status.QUEUED), actor=Actor.AGENT)
    journal.record(
        Entry(VACANCY, Status.NEEDS_MANUAL, reason=f"{run.HH_QUOTE}{said}"), actor=Actor.AGENT
    )
    monkeypatch.setattr(run, "Journal", lambda path: journal)

    assert run.main(["--requeue", VACANCY]) == 0

    back = journal.get(VACANCY)
    assert back is not None
    assert back.status is Status.QUEUED
    assert back.reason == f"{run.HH_QUOTE}{said}"
    assert said in capsys.readouterr().out

    shown: list[Candidate] = []
    _run_one(ready, tmp_path, monkeypatch, journal, a_state(), a_state(applied=True), watch=shown)

    # Under the heading that says it is about the resume, not about this job.
    assert [c.hh_visibility for c in shown] == [said]
    assert [c.hh_warning for c in shown] == [None]
    card = shown[0].render()
    assert said in card
    assert human.VISIBILITY_HEADING in card


def test_a_sent_application_is_never_requeued(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``sent`` is terminal because the world does not take an application back.

    The command names the two states it will touch rather than handing the
    request to the state machine and reporting a traceback.
    """
    journal = _prepared_journal(tmp_path)
    journal.record(Entry(VACANCY, Status.QUEUED), actor=Actor.AGENT)
    journal.record(Entry(VACANCY, Status.CONFIRMED), actor=Actor.HUMAN)
    journal.record(Entry(VACANCY, Status.SENT), actor=Actor.AGENT)
    monkeypatch.setattr(run, "Journal", lambda path: journal)

    assert run.main(["--requeue", VACANCY, "999999999"]) == 1

    entry = journal.get(VACANCY)
    assert entry is not None and entry.status is Status.SENT
    printed = capsys.readouterr().out
    assert "сейчас «sent»" in printed
    assert "999999999: такой вакансии в журнале нет." in printed


def test_a_warning_hh_raises_only_after_the_letter_is_typed_is_still_carried_out(
    ready: None,
) -> None:
    """The card is read before the letter exists, and the send is after it.

    Rewritten 2026-09-07 from ``test_a_refusal_hh_raises_only_after_the_letter
    _is_typed_still_stops_the_send``, which asserted this stopped the send. It no
    longer does — nothing hh writes on that card does — but the reason the card
    is read a second time survives the change, and it is what this now pins. hh
    answers what is typed, the modal that was quiet when it opened is not the
    modal that receives the click, and a sentence that appeared in between must
    still reach the owner. Both lines that re-read the card could be deleted with
    the rest of the suite green, which is why this test exists at all.
    """
    gate = SubmitGate()
    page = FakePage(
        gate,
        a_state(),
        state_after_send=a_state(applied=True),
        modal_text=QUIET_MODAL,
        modal_text_after_letter=VISIBILITY_MODAL,
    )

    warnings = submit.submit(page, a_mandate(), gate)

    assert page.sent
    assert page.filled, "the letter was typed, which is what provoked the warning"
    # Read only on the second pass, and kept anyway.
    assert warnings.visibility is not None and "видимость" in warnings.visibility
    assert warnings.likely_rejection is not None and "Английский язык" in warnings.likely_rejection


def test_a_card_that_stays_quiet_after_the_letter_is_still_sent(ready: None) -> None:
    """The other direction, so re-reading the card cannot become a blanket refusal."""
    gate = SubmitGate()
    page = FakePage(
        gate,
        a_state(),
        state_after_send=a_state(applied=True),
        modal_text=QUIET_MODAL,
        modal_text_after_letter=QUIET_MODAL,
    )

    warnings = submit.submit(page, a_mandate(), gate)

    assert selectors.SUBMIT_BUTTON.query in page.clicks
    assert (warnings.visibility, warnings.likely_rejection) == (None, None)


# ── where the run takes its work from ─────────────────────────────────


def test_a_backend_that_does_not_answer_stops_the_run_rather_than_reading_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The silent fallback is the defect, not the safety net.

    A run that quietly used ``agent/queue.json`` when the backend was down would
    report a month-old hand-written row as this morning's queue — which is
    exactly what happened for a year, and what nobody could see happening. So
    the run says the backend did not answer, names the flag that works without
    it, and stops.
    """
    journal = _prepared_journal(tmp_path)
    monkeypatch.setattr(run, "Journal", lambda path: journal)

    class Unreachable:
        """The endpoint, not answering."""

        def __init__(self, base_url: str) -> None:
            self.base_url = base_url

        def take(self, limit: int) -> Sequence[QueueItem]:
            """What ``HttpQueue`` raises when nothing is listening."""
            raise QueueUnreachableError(f"{self.base_url} не отдал очередь: ConnectError.")

        def report(self, results: Sequence[Result]) -> None:
            """Never reached: the run stops before anything is confirmed."""

    monkeypatch.setattr(run, "HttpQueue", Unreachable)

    assert run.main(["--queue", str(tmp_path / "queue.json")]) == 1

    printed = capsys.readouterr()
    assert "не отдал очередь" in printed.err
    assert "--no-backend" not in printed.out
    # The file was right there and was not read in its place.
    assert "Сухой прогон" not in printed.out


def test_the_queue_is_the_backends_and_the_file_adds_to_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """What the pipeline produced overnight, plus what a person typed in.

    The dry run is enough to see the composition: both vacancies reach the
    cards, the scored one first, and neither source replaced the other.
    """
    journal = _prepared_journal(tmp_path)
    monkeypatch.setattr(run, "Journal", lambda path: journal)
    monkeypatch.setattr(
        Limits,
        "from_env",
        classmethod(lambda cls: cls(work_starts=time(0, 0), work_ends=time(23, 59))),
    )

    class Served:
        """The endpoint, answering with what the pipeline scored."""

        def __init__(self, base_url: str) -> None:
            self.base_url = base_url

        def take(self, limit: int) -> Sequence[QueueItem]:
            """One scored vacancy with a letter, as the queue serves them."""
            return [
                QueueItem.from_json(
                    {
                        "vacancy_id": "777777777",
                        "url": "https://almaty.hh.kz/vacancy/777777777",
                        "title": "Backend-разработчик Python",
                        "company": "Kaspi",
                        "letter": "Здравствуйте!",
                        "score": 88.0,
                        "score_explanation": "совпадает: python, postgresql",
                    }
                )
            ]

        def report(self, results: Sequence[Result]) -> None:
            """Not reached in a dry run."""

    monkeypatch.setattr(run, "HttpQueue", Served)

    assert run.main(["--queue", str(tmp_path / "queue.json")]) == 0

    printed = capsys.readouterr().out
    assert "777777777" in printed, "the queue the pipeline produced"
    assert VACANCY in printed, "and the row a person added by hand"
    assert printed.index("777777777") < printed.index(VACANCY)


# ── that run.main asks a person at all ────────────────────────────────
#
# Every other test in this file that drives ``run.main(["--send", …])`` goes
# through ``_confirms``, which replaces ``run.confirm`` with a stub that mints a
# mandate for every candidate. That stub is right for those tests — they are
# about what happens after a person has said yes — but it means the property the
# whole package exists for was covered by nothing: ``run.main`` could stop
# calling ``confirm`` altogether and the suite would stay green, because the real
# prompt was only ever driven in isolation in ``test_boundaries.py``.
#
# The tests below drive ``run.main`` with the real :func:`agent.human.confirm`
# over a scripted stdin. Nothing is stubbed between the typed word and the click.


def _run_with_a_person(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    journal: Journal,
    typed: str,
) -> FakePage:
    """``run.main`` with a real confirmation and a scripted person at the keyboard.

    ``confirm`` reads ``sys.stdin`` when it is called with no stream of its own,
    which is exactly how ``run.main`` calls it — so replacing the module's stdin
    is what makes this the real prompt rather than a second stub.
    """
    gate = SubmitGate()
    page = FakePage(gate, a_state(), state_after_send=a_state(applied=True))
    _install(tmp_path, monkeypatch, journal, [page])
    monkeypatch.setattr(run, "SubmitGate", lambda: gate)
    monkeypatch.setattr(sys, "stdin", io.StringIO(typed))
    assert run.main(["--send", "--no-backend", "--queue", str(tmp_path / "queue.json")]) == 0
    return page


def test_run_main_sends_only_after_a_person_types_the_word(
    ready: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The word is typed, and only then does anything leave.

    Delete the ``confirm`` call from ``run.main`` and this fails: with nobody
    asked there is no mandate, and without a mandate the gate aborts the
    navigation that opens the form.
    """
    journal = _prepared_journal(tmp_path)

    # An empty line keeps every card, then the word.
    page = _run_with_a_person(tmp_path, monkeypatch, journal, f"\n{CONFIRM_WORD}\n")

    assert page.sent
    entry = journal.get(VACANCY)
    assert entry is not None
    assert entry.status is Status.SENT


@pytest.mark.parametrize(
    ("typed", "why"),
    [
        ("\n\n", "Enter instead of the word"),
        ("\nда\n", "a different word"),
        ("\nотправляем это\n", "the word with something after it"),
        ("", "stdin closed before the drop line — a pipe, a cron job, nobody there"),
        ("\n", "stdin closed at the confirmation itself"),
        ("1\n", "every card dropped, so there is nothing left to confirm"),
    ],
)
def test_run_main_sends_nothing_when_the_word_is_not_typed(
    ready: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, typed: str, why: str
) -> None:
    """Every way of not answering, driven through the whole program.

    Each of these ends the run at zero — not answering is a normal outcome, not a
    failure — with the browser never opened, nothing clicked, and the row left at
    ``queued`` so the next run offers the vacancy again.
    """
    journal = _prepared_journal(tmp_path)

    page = _run_with_a_person(tmp_path, monkeypatch, journal, typed)

    assert not page.sent, why
    assert page.clicks == []
    # Not even the session health check: a refusal ends the run before the
    # browser is opened at all.
    assert page.visited == []
    entry = journal.get(VACANCY)
    assert entry is not None
    assert entry.status is Status.QUEUED


def test_the_cards_a_person_answers_are_the_ones_run_main_built(
    ready: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """What was on screen, and that the answer bound itself to it.

    A prompt that asked for a word without printing what it is a word about would
    satisfy the test above. So this one reads the transcript: the vacancy id and
    the letter that will be typed into hh's form both have to be on it before the
    question is asked.
    """
    journal = _prepared_journal(tmp_path)

    _run_with_a_person(tmp_path, monkeypatch, journal, f"\n{CONFIRM_WORD}\n")

    shown = capsys.readouterr().out
    asked = shown.index(CONFIRM_WORD)
    assert VACANCY in shown[:asked]
    assert "Здравствуйте!" in shown[:asked]


def test_dropping_the_only_card_leaves_the_vacancy_for_next_time(
    ready: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """«с возможностью выбросить любую», through the program rather than the prompt.

    Dropping the last card cancels the run, and a cancelled run must leave the
    journal alone: a row written here would be a row no later run could offer
    again, on a vacancy nobody decided anything about.
    """
    journal = _prepared_journal(tmp_path, count=2)
    gate = SubmitGate()
    page = FakePage(gate, a_state(), state_after_send=a_state(applied=True))
    _install(tmp_path, monkeypatch, journal, [page])
    monkeypatch.setattr(run, "SubmitGate", lambda: gate)
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"1 2\n{CONFIRM_WORD}\n"))

    assert run.main(["--send", "--no-backend", "--queue", str(tmp_path / "queue.json")]) == 0

    assert not page.sent
    assert page.visited == []
    for offset in (0, 1):
        entry = journal.get(str(int(VACANCY) + offset))
        assert entry is not None
        assert entry.status is Status.QUEUED


# ── the ATS report on the confirmation card ───────────────────────────
#
# The third and last place the backend's audit is shown, and the only one where
# the next thing that happens is an application leaving under the owner's name.
# The card neither computes nor edits it — the audit runs in the backend, over
# the same report the vacancy screen renders — so what is defended here is that
# it arrives, that it is printed in terms a person can act on, and that the one
# thing it must never print stays unprinted.


def a_card(**ats: object) -> str:
    """One rendered card, carrying an ATS summary built from these fields."""
    candidate = Candidate(
        vacancy_id=VACANCY,
        title="Python-разработчик",
        company="Inspire",
        url=PAGE_URL,
        letter=check_letter("Здравствуйте! Работал с Python.", required=False),
        ats=ATSCard(**ats),  # type: ignore[arg-type]
    )
    return candidate.render()


def test_the_card_prints_how_much_of_the_vacancy_the_letter_names() -> None:
    """«названо 1 из 3» is something a person can act on by dropping the item."""
    card = a_card(overall="degraded", score=90.0, requirements_total=3, requirements_present=1)

    assert "проверка ATS" in card
    assert "1 из 3" in card


def test_the_card_names_what_is_fixable_and_only_counts_what_is_not() -> None:
    """The asymmetry the whole feature turns on, at the last possible moment.

    A requirement the owner *has* and this letter does not mention is a letter
    to regenerate, so it is named. A requirement they do not have stays a
    number: a list of those, printed seconds before an application, reads as a
    list of things to claim, and this card is the last place that could be
    suggested.
    """
    card = a_card(
        overall="degraded",
        requirements_total=5,
        requirements_present=2,
        unstated=("PostgreSQL", "Docker"),
        absent=1,
    )

    assert "PostgreSQL, Docker" in card
    assert "требований, которых нет в профиле: 1" in card


def test_a_letter_nobody_audited_does_not_print_as_one_that_passed() -> None:
    """Same rule the score already follows: silence is not a pass."""
    candidate = Candidate(
        vacancy_id=VACANCY,
        title="Python-разработчик",
        company="Inspire",
        url=PAGE_URL,
        letter=None,
    )

    assert "проверка ATS: не выполнялась" in candidate.render()


def test_a_critical_finding_is_printed_in_words() -> None:
    """The card is read by a person; a finding code is for a client."""
    card = a_card(overall="unreadable", critical=("В документе есть скрытый текст",))

    assert "робот не прочитает" in card
    assert "не прочитает: В документе есть скрытый текст" in card


def test_an_unknown_verdict_is_printed_as_it_arrived() -> None:
    """A backend that grows a fourth verdict must not be shown as one of three."""
    assert "что-то новое" in a_card(overall="что-то новое")


def test_the_audit_is_bound_into_the_mandate() -> None:
    """Anything on the card is part of what was approved.

    The mandate is a digest of the rendered text, so a decision made while
    reading «названо 1 из 9» cannot be reused for a payload that no longer
    carries it. That is the rule ``Candidate.render`` states about every field,
    and it has to hold for this one too.
    """
    quiet = Candidate(
        vacancy_id=VACANCY,
        title="Python-разработчик",
        company="Inspire",
        url=PAGE_URL,
        letter=check_letter("Здравствуйте! Работал с Python.", required=False),
    )
    audited = replace(
        quiet, ats=ATSCard(overall="degraded", requirements_total=9, requirements_present=1)
    )

    assert digest(quiet.render()) != digest(audited.render())


def test_a_summary_the_backend_did_not_send_is_not_invented() -> None:
    """An item whose audit did not run must not parse into one that passed."""
    assert ATSCard.from_json(None) is None
    assert ATSCard.from_json({"score": 100}) is None
    assert ATSCard.from_json({"overall": "ok"}) == ATSCard(overall="ok")


def test_the_summary_survives_the_queue_wire() -> None:
    """It reaches the card through ``QueueItem``, so it is parsed there."""
    item = QueueItem.from_json(
        {
            "vacancy_id": VACANCY,
            "url": PAGE_URL,
            "title": "Python-разработчик",
            "ats": {
                "overall": "degraded",
                "score": 90,
                "unstated": ["PostgreSQL"],
                "requirements_total": 3,
                "requirements_present": 2,
                "absent": 0,
            },
        }
    )

    assert item.ats is not None
    assert item.ats.unstated == ("PostgreSQL",)
    assert item.ats.requirements_present == 2


def test_an_older_backend_that_sends_no_summary_still_parses() -> None:
    """The wire stays additive in the direction the contract allows."""
    item = QueueItem.from_json({"vacancy_id": VACANCY, "url": PAGE_URL, "title": "x"})

    assert item.ats is None
