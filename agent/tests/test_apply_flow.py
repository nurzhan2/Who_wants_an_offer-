"""The apply flow, driven end to end with the selectors filled in.

Every other test file here exercises one module against hand-written fakes, and
72 of them passed while five separate defects sat in the path between a
confirmation and a sent application. All five were invisible for the same
reason: nothing imported ``run.py`` or ``submit.py``, so the *order* of the
steps — which is the whole design — was never executed.

That is what this file does. It patches in the three selectors stage 0 will
produce, an applied-marker, and a fake page that answers like hh, and then runs
the real :func:`agent.submit.submit` and the real :func:`agent.run.main`. The
fakes are deliberately dumb: they answer questions and record clicks. Every
decision under test belongs to the code being driven.

The four defects with a test each below, so none can come back quietly:

* the gate aborted the navigation that opens the response form, because
  ``submit()`` clicked the apply link before arming;
* ``run.py`` recorded ``sent`` through a transition the state machine does not
  have, so the journal write crashed *after* the application had left;
* a captcha never incremented the consecutive-failure counter, so hh could
  challenge every vacancy in the batch and the run would walk the whole list;
* ``submit()`` never re-read the page's own application facts, so an employer
  test or a newly required letter went unnoticed.
"""

import io
import json
import time as time_module
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, time
from pathlib import Path
from typing import Any

import pytest

from agent import (
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
from agent.gate import SubmitGate
from agent.human import CancelledError, Candidate, confirm
from agent.journal import Entry, Journal
from agent.letter import check as check_letter
from agent.mandate import SendMandate, mint
from agent.queue import QueueFormatError, QueueItem
from agent.selectors import Scope, Selector
from agent.state import Actor, IllegalInitialStatusError, Status
from agent.submit import (
    AlreadyAppliedError,
    CaptchaPresentedError,
    IdempotencyUnknownError,
)

pytestmark = pytest.mark.unit

VACANCY = "136773120"
PAGE_URL = f"https://almaty.hh.kz/vacancy/{VACANCY}"
APPLY_HREF = f"/applicant/vacancy_response?vacancyId={VACANCY}&employerId=99"
#: What the open form sends. A second application-shaped URL for the same job.
SEND_URL = f"https://hh.kz/applicant/vacancy_response?vacancyId={VACANCY}&lux=true"


def a_state(
    *,
    applied: bool = False,
    has_tests: bool = False,
    letter_required: bool = False,
    closed: bool = False,
) -> dict[str, Any]:
    """hh's boot payload, in the shape measured on live pages on 2026-09-06."""
    return {
        "applicantVacancyResponseStatuses": {
            VACANCY: {
                "test": {"hasTests": has_tests},
                "letterMaxLength": 10000,
                "shortVacancy": {"@responseLetterRequired": letter_required},
                "alreadyApplied": applied,
            }
        },
        "vacancyView": {"closedForApplicants": closed, "archived": False},
    }


def a_page_html(state: dict[str, Any]) -> str:
    """A vacancy page carrying that state, the way hh boots its frontend."""
    blob = json.dumps(state, ensure_ascii=False).replace("&", "&amp;").replace("<", "&lt;")
    return f'<html><body><template id="HH-Lux-InitialState">{blob}</template></body></html>'


@dataclass
class FakeLocator:
    """One element: an href to read and a click that navigates."""

    page: "FakePage"
    href: str

    def get_attribute(self, name: str) -> str | None:
        """Only ``href`` is ever asked for."""
        return self.href if name == "href" else None

    def click(self) -> None:
        """Following the apply link, which is an ordinary GET navigation."""
        self.page.navigate(f"https://hh.kz{self.href}")


@dataclass
class FakePage:
    """Answers like hh and records what was done to it.

    Requests go through the gate exactly as playwright's ``context.route``
    would, so a gate that aborts a navigation produces here what it produces in
    Chromium: the page does not change.
    """

    gate: SubmitGate
    state: dict[str, Any]
    #: The state served after a successful submit. None means "unchanged".
    state_after_send: dict[str, Any] | None = None
    captcha: bool = False
    #: Clicks that reached a control, in order.
    clicks: list[str] = field(default_factory=list)
    filled: dict[str, str] = field(default_factory=dict)
    #: URLs the gate refused, so a test can tell a block from a miss.
    aborted: list[str] = field(default_factory=list)
    sent: bool = False
    #: Every page opened, in order. The first is the session health check.
    visited: list[str] = field(default_factory=list)
    #: One entry per time the window was raised, which is what a captcha does.
    fronted: list[str] = field(default_factory=list)
    listeners: dict[str, list[Any]] = field(default_factory=dict)

    def on(self, event: str, handler: Any) -> None:
        """Playwright's event registration. Used for the escape recorder."""
        self.listeners.setdefault(event, []).append(handler)

    def navigate(self, url: str) -> None:
        """One request, routed through the gate like every other."""
        route = _Route(url)
        for handler in self.listeners.get("request", []):
            handler(route)
        self.gate.handle(route)
        if route.action == "abort":
            self.aborted.append(url)
            return
        if "vacancy_response" in url and not self.sent:
            # hh's response flow: the form opens. The application itself is
            # sent by the submit control, below.
            pass

    def goto(self, url: str, wait_until: str = "load") -> None:
        """Opening a page. Not application-shaped, so the gate lets it by."""
        self.visited.append(url)
        self.navigate(url)

    def content(self) -> str:
        """The page as it currently stands."""
        if self.captcha:
            return "<html><body>Подтвердите, что вы не робот</body></html>"
        state = self.state_after_send if self.sent and self.state_after_send else self.state
        return a_page_html(state)

    def locator(self, query: str) -> FakeLocator:
        """Only the apply link is located rather than clicked by query."""
        return FakeLocator(self, APPLY_HREF)

    def wait_for_selector(self, query: str, timeout: int = 0) -> None:
        """The response form. Present only once the navigation actually happened."""
        if self.aborted:
            raise TimeoutError(f"{query} не появился: переход был отменён")

    def fill(self, query: str, text: str) -> None:
        """Typing the letter into the form."""
        self.filled[query] = text

    def click(self, query: str) -> None:
        """The submit control, which puts the application on the wire."""
        self.clicks.append(query)
        self.sent = True
        self.navigate(SEND_URL)

    def wait_for_timeout(self, ms: int) -> None:
        """Nothing to wait for here."""

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
    """Stage 0 as it will look once the owner has run the probe.

    The evidence is a real report in the real format, written by hand here
    because the alternative is a live logged-in browser. Everything
    ``assert_ready_to_apply`` reads is present and honest: the stage that can
    see the form, a measured authentication, and the names in ``candidates``.
    """
    probe_dir = tmp_path / "probe" / "20260906-101500"
    probe_dir.mkdir(parents=True)
    (probe_dir / "probe.json").write_text(
        json.dumps(
            {
                "stage": "open-form",
                "authenticated": True,
                "vacancy_id": VACANCY,
                "data_qa_after_click": {
                    "candidates": [
                        "vacancy-response-letter-toggle",
                        "vacancy-response-popup-form",
                        "vacancy-response-submit-popup",
                    ],
                    "decoys_ask_the_employer_a_question": [],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(selectors, "PROBE_DIR", tmp_path / "probe")

    filled = tuple(
        Selector(
            name=name,
            query=f'[data-qa="{qa}"]',
            scope=Scope.AUTHENTICATED,
            evidence="20260906-101500",
            checked_on=date(2026, 9, 6),
        )
        for name, qa in (
            ("response_form", "vacancy-response-popup-form"),
            ("submit_button", "vacancy-response-submit-popup"),
            ("letter_field", "vacancy-response-letter-toggle"),
        )
    )
    monkeypatch.setattr(selectors, "REQUIRED_FOR_APPLYING", filled)
    monkeypatch.setattr(selectors, "RESPONSE_FORM", filled[0])
    monkeypatch.setattr(selectors, "SUBMIT_BUTTON", filled[1])
    monkeypatch.setattr(selectors, "LETTER_FIELD", filled[2])
    # What the probe on an already-applied vacancy will teach us. The path is
    # relative to ``applicantVacancyResponseStatuses[id]``, and run.py imported
    # the tuple by value, so both names have to be moved.
    markers = (("alreadyApplied",),)
    monkeypatch.setattr(state_page, "APPLIED_MARKERS", markers)
    monkeypatch.setattr(run, "APPLIED_MARKERS", markers)
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

    submit.submit(page, a_mandate(), gate)

    assert page.aborted == [], f"the gate refused part of its own flow: {gate.refused_because}"
    assert page.clicks == ['[data-qa="vacancy-response-submit-popup"]']
    assert page.filled == {'[data-qa="vacancy-response-letter-toggle"]': "Здравствуйте!"}
    # Two application-shaped requests for one application: the form, then the send.
    assert len(gate.allowed) == 2
    gate.assert_no_escapes()


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
    assert results["results"] == [{"vacancy_id": VACANCY, "status": "sent", "reason": None}]
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
    """The one mistake the owner cannot undo, checked against hh and not the journal."""
    gate = SubmitGate()
    page = FakePage(gate, a_state(applied=True))

    with pytest.raises(AlreadyAppliedError):
        submit.submit(page, a_mandate(), gate)

    assert page.clicks == []


def test_a_captcha_raises_and_raises_the_window(ready: None) -> None:
    """Detection, never a solver. The window comes forward and a person deals with it."""
    gate = SubmitGate()
    page = FakePage(gate, a_state(), captcha=True)

    with pytest.raises(CaptchaPresentedError):
        submit.submit(page, a_mandate(), gate)

    assert page.fronted == ["raised"]
    assert page.clicks == []


def test_the_submitter_opens_the_page_the_human_was_shown(ready: None) -> None:
    """Not a URL rebuilt from the id against a hardcoded host."""
    gate = SubmitGate()
    page = FakePage(gate, a_state(), state_after_send=a_state(applied=True))
    opened: list[str] = []
    original = page.goto

    def record(url: str, wait_until: str = "load") -> None:
        opened.append(url)
        original(url, wait_until)

    page.goto = record  # type: ignore[method-assign]
    submit.submit(page, a_mandate(), gate)

    assert opened == [PAGE_URL]


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


def _run_one(
    ready: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    journal: Journal,
    state: dict[str, Any],
    after: dict[str, Any],
) -> FakePage:
    """One confirmed application, all the way through ``run.main``."""
    gate = SubmitGate()
    page = FakePage(gate, state, state_after_send=after)
    _install(tmp_path, monkeypatch, journal, [page])
    monkeypatch.setattr(run, "SubmitGate", lambda: gate)
    monkeypatch.setattr(
        run,
        "confirm",
        lambda candidates: [
            mint(
                vacancy_id=c.vacancy_id,
                url=c.url,
                letter=None if c.letter is None else c.letter.text,
                form_digest="what-was-shown",
            )
            for c in candidates
        ],
    )
    assert run.main(["--send", "--queue", str(tmp_path / "queue.json")]) == 0
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
    page = FakePage(gate, a_state(), captcha=captcha)
    _install(tmp_path, monkeypatch, journal, [page])
    monkeypatch.setattr(run, "SubmitGate", lambda: gate)
    monkeypatch.setattr(
        run,
        "confirm",
        lambda candidates: [
            mint(
                vacancy_id=c.vacancy_id,
                url=c.url,
                letter=None if c.letter is None else c.letter.text,
                form_digest="what-was-shown",
            )
            for c in candidates
        ],
    )
    run.main(["--send", "--queue", str(tmp_path / "queue.json")])
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
    typed = io.StringIO("\u00b2\n1\n")
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


class _ProbePage:
    """A page that reveals the response form once the navigation is allowed."""

    def __init__(self, captured: dict[str, Any], reached: list[str]) -> None:
        self.captured = captured
        self.reached = reached
        self.opened = False

    def on(self, event: str, handler: Any) -> None:
        """The escape recorder."""

    def goto(self, url: str, wait_until: str = "load") -> None:
        """Opening the vacancy page."""

    def click(self, query: str, timeout: int = 0) -> None:
        """Following the apply link, through the guard the probe installed."""
        route = _Route(f"https://hh.kz{APPLY_HREF}")
        self.captured["guard"](route)
        if route.action == "continue":
            self.reached.append(route.url)
            self.opened = True

    def wait_for_timeout(self, ms: int) -> None:
        """Nothing to wait for."""

    def content(self) -> str:
        """The error page, or the form."""
        if not self.opened:
            return "<html><body>chrome error</body></html>"
        return (
            '<html><body><form data-qa="vacancy-response-popup-form">'
            '<textarea data-qa="vacancy-response-letter-toggle"></textarea>'
            '<button data-qa="vacancy-response-submit-popup">Отправить</button>'
            "</form></body></html>"
        )


def test_the_probe_can_reach_the_form_it_exists_to_photograph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stage 0 aborted its own navigation, so its report came back empty.

    The whole package was unblockable by the procedure its README documents:
    the three selectors can only be seen after the click, and the click was
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


def test_the_probe_refuses_a_fresh_vacancy_outright() -> None:
    """The refusal that carries the safety, not the interceptor."""
    with pytest.raises(SystemExit, match="already-applied"):
        probe_apply.open_form(PAGE_URL, already_applied=False)


def test_the_card_prints_on_the_console_this_actually_runs_on() -> None:
    """A Russian Windows console encodes cp1251, and the card is the first thing shown.

    A box-drawing character in the letter's margin raised UnicodeEncodeError out
    of the dry run before a single candidate could be read.
    """
    candidate = Candidate(
        vacancy_id=VACANCY,
        title="Python-разработчик",
        company="Inspire",
        url=PAGE_URL,
        letter=check_letter("Здравствуйте!\nОпыт — Python, FastAPI…", required=False),
    )

    candidate.render().encode("cp1251")
