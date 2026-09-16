"""The outcome walk: what it reads, what it refuses to invent, and what it cannot do.

Three kinds of claim are made here and they are proved three different ways.

*What hh said* is driven over a fake page that answers with the boot state
measured on 2026-09-06, so the tests exercise the real
``agent/state_page.py`` reader rather than a stand-in for it.

*What must never be invented* is asserted against the printed report and
against the wire form: no rate, no percentage, no state picked out of two, no
``negotiations_total`` conjured where hh said nothing. There are two outcomes
on this whole account and every one of those would be a number about nothing.

*That it cannot send* is executed rather than promised. The walk runs with a
real :class:`agent.gate.SubmitGate`, the fake page fires the apply URL hh
itself puts on a vacancy page, and the assertion is that the route was aborted
— the same code path that runs in Chromium. The source scan beside it catches
the other direction: a future change that mints consent here would have to
write the import in front of a test that names it.

Nothing here needs a browser, a network or an account.
"""

import ast
import io
import json
import random
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from datetime import time as clock
from pathlib import Path
from typing import Any, Final, final

import pytest

from agent import outcomes
from agent.config import Limits
from agent.gate import SubmitGate
from agent.journal import Entry, Journal
from agent.outcomes import Outcome, Seen, describe, previous_states, read_outcome, walk
from agent.queue import QueueFormatError, Result, ResultsFile
from agent.session import SignalUnknownError
from agent.state import Actor, Status

pytestmark = pytest.mark.unit

VACANCY: Final[str] = "136773120"
OTHER: Final[str] = "133542745"
PAGE_URL: Final[str] = f"https://almaty.hh.kz/vacancy/{VACANCY}"
OTHER_URL: Final[str] = f"https://almaty.hh.kz/vacancy/{OTHER}"

#: The page the session signal was measured on. A vacancy page like the rest —
#: ``agent/login.py`` records the keys that appear on one — and deliberately not
#: any of the vacancies being walked, so a test can tell the health check apart
#: from the work.
SIGNAL_URL: Final[str] = "https://almaty.hh.kz/vacancy/1"

#: The apply control hh puts on a vacancy page, measured 2026-09-06. The walk
#: never follows it; the point of firing it below is that if anything ever did,
#: the gate would abort it because no mandate exists.
APPLY_URL: Final[str] = f"https://hh.kz/applicant/vacancy_response?vacancyId={VACANCY}"

#: Where hh sends a visitor it has decided is a robot, as this project's
#: crawler measured it: a plain ``GET /vacancy/<id>`` answered ``302``.
CHALLENGE_URL: Final[str] = f"https://hh.kz/account/captcha?backurl=%2Fvacancy%2F{VACANCY}"

MODULE: Final[Path] = Path(outcomes.__file__)


def a_state(*applications: dict[str, Any], vacancy_id: str = VACANCY, total: int) -> dict[str, Any]:
    """hh's boot state for one vacancy, in the shape measured on the owner's account.

    ``vacancy_id`` is the key hh files the entry under and it has to match the
    vacancy the page is for. Use :func:`answering` for a map of several: a state
    filed under the wrong id reads as "hh said nothing", which is a real
    behaviour and a confusing way to write a fixture by accident.
    """
    return {
        "applicantVacancyResponseStatuses": {
            vacancy_id: {"negotiations": {"total": total, "topicList": list(applications)}}
        }
    }


def answering(
    *vacancy_ids: str, state: str | None = "DISCARD", total: int = 1
) -> dict[str, dict[str, Any] | None]:
    """A page map where each vacancy answers about itself."""
    return {
        vacancy_id: a_state(
            *((a_topic(state),) if state else ()), vacancy_id=vacancy_id, total=total
        )
        for vacancy_id in vacancy_ids
    }


def a_topic(last_state: str | None = "DISCARD", topic_id: int = 7) -> dict[str, Any]:
    """One entry of ``topicList``, with the four keys this project reads."""
    return {
        "id": topic_id,
        "chatId": topic_id + 1,
        "initialState": "RESPONSE",
        "lastState": last_state,
    }


def a_page_html(state: dict[str, Any] | None) -> str:
    """A vacancy page carrying that state, the way hh boots its frontend."""
    if state is None:
        return "<html><body>hh отдал что-то другое</body></html>"
    blob = json.dumps(state, ensure_ascii=False).replace("&", "&amp;").replace("<", "&lt;")
    return f'<html><body><template id="HH-Lux-InitialState">{blob}</template></body></html>'


@final
@dataclass
class _Route:
    """A playwright route over one URL. Only what the gate reads is answered."""

    url: str
    action: str | None = None

    @property
    def request(self) -> "_Route":
        """The route is its own request here."""
        return self

    @property
    def method(self) -> str:
        """Recorded by the gate, never used by it to decide."""
        return "GET"

    @property
    def post_data(self) -> str | None:
        """A navigation has no body."""
        return None

    def abort(self, error_code: str = "failed") -> None:
        """Refused."""
        self.action = "abort"

    def continue_(self) -> None:
        """Allowed through."""
        self.action = "continue"


@dataclass
class Page:
    """Answers like hh and records what was done to it.

    Every navigation goes through the gate exactly as ``context.route`` routes
    it in Chromium, so a gate that aborts something produces here what it
    produces there.
    """

    gate: SubmitGate
    #: Boot state per vacancy id. A vacancy absent from this map is served a
    #: page with no state at all, which is hh redesigned or an error page.
    states: dict[str, dict[str, Any] | None] = field(default_factory=dict)
    #: Where a navigation actually lands, when that is not where it was asked
    #: to go. hh's robot check and its sign-in wall are both redirects.
    lands_on: dict[str, str] = field(default_factory=dict)
    #: hh's own traffic, fired once a page is open. Every one of these carries a
    #: vacancy id and is therefore application-shaped to the gate.
    fires: tuple[str, ...] = ()
    #: Addresses that never load at all — a timeout, a dead socket, a tab that
    #: died. Not a redirect: nothing here says where it went instead.
    breaks: frozenset[str] = frozenset()
    url: str = ""
    visited: list[str] = field(default_factory=list)
    aborted: list[str] = field(default_factory=list)
    listeners: dict[str, list[Any]] = field(default_factory=dict)
    _served: dict[str, Any] | None = None

    def on(self, event: str, handler: Any) -> None:
        """Playwright's event registration, used by the gate's escape recorder."""
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
        """Opening a page, then whatever hh talks to itself about afterwards."""
        self.visited.append(url)
        if url in self.breaks:
            raise TimeoutError(f"Timeout 30000ms exceeded: {url}")
        self.navigate(url)
        self.url = self.lands_on.get(url, url)
        vacancy = self.url.rsplit("/", 1)[-1].split("?")[0]
        self._served = self.states.get(vacancy)
        for beacon in self.fires:
            self.navigate(beacon)

    def content(self) -> str:
        """The page as it was last served."""
        return a_page_html(self._served)

    def wait_for_load_state(self, state: str, timeout: int = 0) -> None:
        """Only reached when a navigation was interrupted; nothing to do here."""


def an_entry(vacancy_id: str = VACANCY, url: str | None = PAGE_URL) -> Entry:
    """One journal row for a vacancy already applied to."""
    return Entry(
        vacancy_id=vacancy_id,
        status=Status.SENT,
        title="Python-разработчик",
        company="Inspire",
        url=url,
    )


def a_page(**overrides: Any) -> Page:
    """A page answering for one vacancy with one rejected application on it."""
    gate = overrides.pop("gate", None) or SubmitGate()
    states: dict[str, dict[str, Any] | None] = overrides.pop(
        "states", {VACANCY: a_state(a_topic(), total=1)}
    )
    return Page(gate=gate, states=states, **overrides)


def unpaced() -> Limits:
    """The real limits with the wait taken out, so a test does not sit for a minute."""
    return Limits(pause_min_seconds=0, pause_max_seconds=0)


# ── what hh said, read off the page it says it on ─────────────────────


def test_the_state_and_the_count_are_both_read_off_one_page() -> None:
    """Both come free with a page load, and both go into the tracker."""
    page = a_page()

    outcome = read_outcome(page, an_entry())

    assert (outcome.total, outcome.last_state) == (1, "DISCARD")
    assert page.visited == [PAGE_URL]


def test_a_state_nobody_here_has_seen_is_carried_rather_than_mapped() -> None:
    """hh's vocabulary is hh's, and it is open.

    ``DISCARD`` is the only value ever observed. A reader that recognised a
    closed set would have to decide what to do with the first value outside it,
    and every answer to that is an invention: dropping it loses the outcome,
    mapping it to a neighbour states something hh did not.
    """
    page = a_page(states={VACANCY: a_state(a_topic("INVITATION"), total=1)})

    outcome = read_outcome(page, an_entry())

    assert outcome.last_state == "INVITATION"
    assert outcome.to_result().last_state == "INVITATION"


def test_hh_saying_zero_is_a_measurement_and_hh_saying_nothing_is_not() -> None:
    """The one distinction the whole record rests on.

    ``0`` is hh's own count and belongs in the column; an unreadable page is not
    a zero, and reporting it as one would put a number where there is no
    measurement at all.
    """
    counted = read_outcome(a_page(states={VACANCY: a_state(total=0)}), an_entry())
    silent = read_outcome(a_page(states={VACANCY: {"somethingElse": {}}}), an_entry())

    assert (counted.total, counted.answered) == (0, True)
    assert (silent.total, silent.answered) == (None, False)
    assert silent.note is not None


def test_a_page_without_hhs_state_at_all_says_so_rather_than_reading_as_empty() -> None:
    """A redesign, an error page, an interstitial. Never guessed at."""
    outcome = read_outcome(a_page(states={VACANCY: None}), an_entry())

    assert not outcome.answered
    assert "не отдал" in (outcome.note or "")


def test_two_conversations_in_two_states_have_no_single_outcome() -> None:
    """hh allows a second application under another resume, and then there are two.

    Picking one of them would invent the answer. Both are kept for the person
    reading the run and neither reaches the tracker's single column.
    """
    page = a_page(states={VACANCY: a_state(a_topic("DISCARD", 1), a_topic("RESPONSE", 2), total=2)})

    outcome = read_outcome(page, an_entry())

    assert outcome.states == ("DISCARD", "RESPONSE")
    assert outcome.last_state is None
    assert outcome.to_result().last_state is None
    assert outcome.to_result().negotiations_total == 2


def test_two_conversations_agreeing_on_one_state_still_have_one_outcome() -> None:
    """The distinction is disagreement, not plurality."""
    page = a_page(states={VACANCY: a_state(a_topic("DISCARD", 1), a_topic("DISCARD", 2), total=2)})

    assert read_outcome(page, an_entry()).last_state == "DISCARD"


def test_a_conversation_hh_has_not_named_a_state_for_is_not_a_state() -> None:
    """``lastState`` is ``null`` until there is one. Null is not an outcome."""
    page = a_page(states={VACANCY: a_state(a_topic(None), total=1)})

    outcome = read_outcome(page, an_entry())

    assert (outcome.total, outcome.states, outcome.last_state) == (1, (), None)


def test_a_row_without_a_url_is_opened_on_the_configured_host() -> None:
    """The journal may hold a row whose address was never written down."""
    page = a_page()

    read_outcome(page, an_entry(url=None))

    assert page.visited[0].endswith(f"/vacancy/{VACANCY}")


# ── it reads, and the gate is what makes that true ────────────────────


def test_an_application_shaped_request_is_aborted_because_nothing_was_armed() -> None:
    """Executed, not asserted about. This is the same path Chromium takes.

    The walk mints no mandate, so ``SubmitGate`` has none armed, so every
    request that could be an application is refused. The apply URL fired below
    is the one hh puts on a vacancy page.
    """
    gate = SubmitGate()
    page = a_page(gate=gate, fires=(APPLY_URL,))

    read, stopped = walk(page, [an_entry()], limits=unpaced(), rng=random.Random(1), sleep=_no_wait)

    assert stopped is None
    assert read[0].answered, "страница вакансии сама по себе не похожа на отклик и должна открыться"
    assert PAGE_URL not in page.aborted
    assert APPLY_URL in page.aborted
    assert APPLY_URL in gate.blocked
    # Nothing application-shaped got through at all, which is the whole claim:
    # ``allowed`` only ever holds URLs that looked like an application.
    assert gate.allowed == []
    gate.assert_no_escapes()


def test_the_unarmed_walk_lets_hhs_beacons_by_and_refuses_the_response_form() -> None:
    """Measured shapes: beacons and widgets carry a vacancy id and are not applying.

    Since 2026-09-16 the gate decides by path, so hh's furniture proceeds and
    only a request under the response path is refused — which is the visible
    sign that the walk is running unarmed.
    """
    furniture = (
        f"https://almaty.hh.kz/anatskytics?hhtmSource=vacancy&vacancyId={VACANCY}",
        f"https://almaty.hh.kz/applicant/blacklist/state?vacancyId={VACANCY}",
    )
    form = f"https://almaty.hh.kz/applicant/vacancy_response/popup?vacancyId={VACANCY}"
    gate = SubmitGate()
    page = a_page(gate=gate, fires=(*furniture, form))

    walk(page, [an_entry()], limits=unpaced(), rng=random.Random(1), sleep=_no_wait)

    assert page.aborted == [form]


def test_the_walk_never_reaches_for_consent_or_for_the_sender() -> None:
    """The other direction: a change that could send would have to import one of these.

    Parsed rather than grepped, because this module's docstring names every one
    of them while arguing that it does not use them, and a substring search
    would read the explanation as the offence.
    """
    forbidden = {"agent.mandate", "agent.human", "agent.submit", "agent.run", "agent.letter"}
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)

    assert not imported & forbidden, f"обход дотянулся до отправки: {imported & forbidden}"


def test_nothing_in_the_walk_arms_clicks_or_types() -> None:
    """A read-only page visit has no verbs. These are the ones that would give it some."""
    forbidden = {"armed", "mint", "click", "fill", "press", "select_option", "record"}
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    reached = sorted(
        {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)} & forbidden
    )

    assert not reached, f"обход что-то делает со страницей, а он только читает: {reached}"


# ── stopping, and what a captcha costs ────────────────────────────────


def test_a_page_that_is_not_the_vacancy_stops_the_whole_walk() -> None:
    """hh's robot check is a redirect, and after one nothing else is worth asking for.

    The crawler measured that redirect. What it cost there is the argument for
    stopping here: once hh has decided a client is a robot, walking the
    remaining nineteen pages spends the account's goodwill to learn nothing.
    """
    page = a_page(
        states=answering(VACANCY, OTHER),
        lands_on={PAGE_URL: CHALLENGE_URL},
    )

    read, stopped = walk(
        page,
        [an_entry(), an_entry(OTHER, OTHER_URL)],
        limits=unpaced(),
        rng=random.Random(1),
        sleep=_no_wait,
    )

    assert read == []
    assert stopped is not None and VACANCY in stopped
    assert page.visited == [PAGE_URL], "вторую страницу открывать уже нельзя"


def test_two_unreadable_pages_in_a_row_stop_the_walk() -> None:
    """hh renaming a field looks exactly like this, on every vacancy at once."""
    page = a_page(states={VACANCY: None, OTHER: None})

    read, stopped = walk(
        page,
        [an_entry(), an_entry(OTHER, OTHER_URL), an_entry("999", None)],
        limits=unpaced(),
        rng=random.Random(1),
        sleep=_no_wait,
    )

    assert len(read) == 2, "третью страницу открывать уже незачем"
    assert stopped is not None and "python -m agent.login" in stopped


def test_one_unreadable_page_between_two_good_ones_stops_nothing() -> None:
    """A single vacancy hh has nothing to say about is an ordinary state."""
    page = a_page(
        states={
            VACANCY: a_state(a_topic(), total=1),
            OTHER: None,
            "999": a_state(a_topic("RESPONSE"), vacancy_id="999", total=1),
        }
    )

    read, stopped = walk(
        page,
        [an_entry(), an_entry(OTHER, OTHER_URL), an_entry("999", "https://hh.kz/vacancy/999")],
        limits=unpaced(),
        rng=random.Random(1),
        sleep=_no_wait,
    )

    assert stopped is None
    assert [outcome.answered for outcome in read] == [True, False, True]


def test_the_walk_waits_between_pages_and_never_before_the_first() -> None:
    """The agent's own pacing, not a faster one invented for reads.

    Same account, same session, same fingerprint; what a site notices is a
    pattern rather than a payload, so the read walk keeps the interval the
    apply run keeps.
    """
    waits: list[float] = []
    limits = Limits()
    page = a_page(states=answering(VACANCY, OTHER))

    walk(
        page,
        [an_entry(), an_entry(OTHER, OTHER_URL)],
        limits=limits,
        rng=random.Random(1),
        sleep=waits.append,
    )

    assert len(waits) == 1
    assert limits.pause_min_seconds <= waits[0] <= limits.pause_max_seconds


def _no_wait(seconds: float) -> None:
    """Stand in for the pause. The only way to shorten it, and not a flag."""


# ── going backwards, which is disappearance and nothing else ──────────


def test_a_state_that_disappears_is_reported_to_the_person_and_not_to_the_tracker() -> None:
    """Silence on the wire means "I did not look", so the tracker keeps what it has.

    Erasing an outcome the owner has already read, on the strength of one page
    load that came back thin, is the more expensive of the two mistakes.
    """
    outcome = Outcome(vacancy_id=VACANCY, url=PAGE_URL, total=1)

    lines = describe(outcome, Seen(total=1, last_state="DISCARD"))

    assert any("ВНИМАНИЕ" in line and "DISCARD" in line for line in lines)
    assert outcome.to_result().last_state is None


def test_an_application_hh_stops_counting_is_said_out_loud_and_recorded_as_zero() -> None:
    """``0`` is hh's measurement, so it goes in. Nothing else moves.

    The journal row stays ``sent``: ``agent/state.py`` has no move out of it,
    and a local ``sent`` hh disagrees with is the safe direction — it stops the
    agent from applying twice, which is the mistake nobody can undo.
    """
    outcome = Outcome(vacancy_id=VACANCY, url=PAGE_URL, total=0)

    lines = describe(outcome, Seen(total=1, last_state=None))

    assert any("больше не считает" in line for line in lines)
    assert outcome.to_result().negotiations_total == 0


def test_a_state_that_changes_is_reported_as_a_change_and_not_as_progress() -> None:
    """No order is defined over hh's states, so nothing here calls one better."""
    outcome = Outcome(vacancy_id=VACANCY, url=PAGE_URL, total=1, states=("INVITATION",))

    lines = " ".join(describe(outcome, Seen(total=1, last_state="RESPONSE")))

    assert "было RESPONSE" in lines and "стало INVITATION" in lines
    for word in ("продвинул", "прогресс", "лучше", "хуже", "успех", "провал"):
        assert word not in lines


def test_the_first_state_on_a_vacancy_reads_as_new_rather_than_as_a_change() -> None:
    """There was nothing before it, and «изменилось с ничего» is not a sentence."""
    outcome = Outcome(vacancy_id=VACANCY, url=PAGE_URL, total=1, states=("DISCARD",))

    assert any("Появилось состояние" in line for line in describe(outcome, Seen(1, None)))


def test_nothing_printed_is_a_rate_a_share_or_a_percentage() -> None:
    """Two outcomes on this account. Every ratio computed from that is a fiction."""
    report = " ".join(
        line
        for outcome in (
            Outcome(vacancy_id=VACANCY, url=PAGE_URL, total=1, states=("DISCARD",)),
            Outcome(vacancy_id=OTHER, url=OTHER_URL, total=0),
            Outcome(vacancy_id="999", url=PAGE_URL, note="не прочиталось"),
        )
        for line in describe(outcome, None)
    )

    assert "%" not in report
    for word in ("процент", "доля", "конверси", "в среднем"):
        assert word not in report


def test_the_outcome_carries_counts_and_has_nowhere_to_put_a_rate() -> None:
    """Held by the shape, so adding one has to be a deliberate edit here."""
    fields = {name for name in dir(Outcome) if not name.startswith("_")}

    assert not [
        name for name in fields if any(word in name for word in ("rate", "percent", "share"))
    ]


# ── the memory between two walks ──────────────────────────────────────


def test_what_one_walk_wrote_is_what_the_next_one_compares_against(tmp_path: Path) -> None:
    """The document round-trips, which is the whole basis of "it changed"."""
    memory = ResultsFile(tmp_path / "outcomes.json")
    memory.report([Outcome(VACANCY, PAGE_URL, total=1, states=("DISCARD",)).to_result()])

    seen = previous_states(memory.read())

    assert seen == {VACANCY: Seen(total=1, last_state="DISCARD")}


def test_a_memory_that_is_not_the_document_is_reported_rather_than_believed(
    tmp_path: Path,
) -> None:
    """ "Nothing to compare against" and "the file is unreadable" are different sentences."""
    broken = tmp_path / "outcomes.json"
    broken.write_text("{}", encoding="utf-8")

    assert ResultsFile(tmp_path / "missing.json").read() == []
    with pytest.raises(QueueFormatError):
        ResultsFile(broken).read()


def test_a_result_read_back_keeps_the_difference_between_zero_and_nothing() -> None:
    """The one distinction the document has to survive being written down."""
    counted = Result.from_json({"vacancy_id": VACANCY, "status": "sent", "negotiations_total": 0})
    silent = Result.from_json({"vacancy_id": VACANCY, "status": "sent"})
    lying = Result.from_json({"vacancy_id": VACANCY, "status": "sent", "negotiations_total": True})

    assert counted.negotiations_total == 0
    assert silent.negotiations_total is None
    assert lying.negotiations_total is None, "true — не количество откликов"


# ── the whole subcommand, over a fake browser ─────────────────────────


def a_journal(path: Path, *vacancy_ids: str) -> Journal:
    """A journal holding one sent application per id, through the legal moves."""
    journal = Journal(path)
    for vacancy_id in vacancy_ids:
        url = f"https://almaty.hh.kz/vacancy/{vacancy_id}"
        journal.record(
            Entry(vacancy_id, Status.QUEUED, title="Python-разработчик", url=url),
            actor=Actor.AGENT,
        )
        journal.record(Entry(vacancy_id, Status.CONFIRMED, url=url), actor=Actor.HUMAN)
        journal.record(
            Entry(vacancy_id, Status.SENT, url=url, reason="hh: предупреждение про резюме"),
            actor=Actor.AGENT,
        )
    return journal


def _install(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    journal: Journal,
    page: Page,
    *,
    hours: tuple[clock, clock] = (clock(0, 0), clock(23, 59)),
) -> Path:
    """Point ``main`` at a temporary journal and a fake page instead of Chromium."""
    memory = tmp_path / "outcomes.json"
    monkeypatch.setattr(outcomes, "Journal", lambda path: journal)
    monkeypatch.setattr(outcomes, "OUTCOMES_PATH", memory)
    monkeypatch.setattr(outcomes, "load_signal", lambda: ["applicantVacancyResponseStatuses"])
    monkeypatch.setattr(outcomes, "signal_source", lambda: SIGNAL_URL)
    monkeypatch.setattr(outcomes, "session_check", lambda state, *, signal: None)
    monkeypatch.setattr(outcomes, "screenshot_on_error", lambda page, name: None)
    # One gate, shared, exactly as the run has one: the page routes through the
    # gate it was built with, and ``main`` builds its own. Two of them would
    # make every assertion below about a gate nothing ever asked.
    monkeypatch.setattr(outcomes, "SubmitGate", lambda: page.gate)
    monkeypatch.setattr(
        Limits,
        "from_env",
        classmethod(
            lambda cls: cls(
                pause_min_seconds=0,
                pause_max_seconds=0,
                work_starts=hours[0],
                work_ends=hours[1],
            )
        ),
    )

    class Context:
        """A browser context that hands out the prepared page."""

        def new_page(self) -> Page:
            """The single tab the walk uses."""
            return page

        def route(self, pattern: str, handler: Any) -> None:
            """Registered for real; the page calls the gate itself."""

    class Opened:
        """``open_browser`` as a context manager."""

        def __enter__(self) -> Context:
            return Context()

        def __exit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(outcomes, "open_browser", lambda: Opened())
    return memory


def run_main(argv: list[str] | None = None) -> tuple[int, str]:
    """Drive ``main`` and capture what a person would have read."""
    printed = io.StringIO()
    with redirect_stdout(printed):
        code = outcomes.main(argv or [])
    return code, printed.getvalue()


def test_the_walk_reads_the_sent_rows_and_writes_what_hh_said(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End to end: journal in, one page per row, a document out."""
    journal = a_journal(tmp_path / "agent.sqlite3", VACANCY, OTHER)
    page = a_page(
        states={
            VACANCY: a_state(a_topic("DISCARD"), total=1),
            OTHER: a_state(vacancy_id=OTHER, total=0),
        }
    )
    memory = _install(monkeypatch, tmp_path, journal, page)

    code, printed = run_main()

    assert code == 0
    written = previous_states(ResultsFile(memory).read())
    assert written == {
        VACANCY: Seen(total=1, last_state="DISCARD"),
        OTHER: Seen(total=0, last_state=None),
    }
    assert "Прочитано: 2 из 2" in printed


def test_the_walk_never_writes_to_the_journal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The journal's one ``reason`` column already holds hh's warning.

    ``Journal.record`` replaces it on every write, so an outcome written there
    would erase the sentence the confirmation card was built from — and there is
    no move out of ``sent`` to make anyway.
    """
    journal = a_journal(tmp_path / "agent.sqlite3", VACANCY)
    page = a_page()
    _install(monkeypatch, tmp_path, journal, page)

    def refuse(entry: Entry, *, actor: Actor) -> None:
        raise AssertionError(f"обход написал в журнал: {entry}")

    monkeypatch.setattr(journal, "record", refuse)
    code, _ = run_main()

    assert code == 0
    still = journal.get(VACANCY)
    assert still is not None and still.status is Status.SENT
    assert still.reason == "hh: предупреждение про резюме"


def test_a_vacancy_hh_said_nothing_about_is_not_reported_at_all(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A result with a status and two nulls tells the tracker nothing it did not know.

    It is not free either: at the far end an empty ``sent_at`` is filled from
    the moment the report arrives, so reporting every page opened would stamp
    today onto rows whose sends this walk knows nothing about.
    """
    journal = a_journal(tmp_path / "agent.sqlite3", VACANCY)
    memory = _install(monkeypatch, tmp_path, journal, a_page(states={VACANCY: None}))

    code, printed = run_main()

    assert code == 0
    assert ResultsFile(memory).read() == []
    assert "Прочитано: 0 из 1" in printed


def test_the_run_says_out_loud_that_nothing_was_armed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The line is the proof, printed where the owner reads it."""
    journal = a_journal(tmp_path / "agent.sqlite3", VACANCY)
    _install(monkeypatch, tmp_path, journal, a_page(fires=(APPLY_URL,)))

    _, printed = run_main()

    assert "Мандатов не выдавалось" in printed
    # Two: the health check opens a page as well, and hh's furniture fires on
    # that one too. Every page this walk opens is an unarmed page.
    assert "отклонил похожих на отклик запросов: 2" in printed


def test_the_walk_does_not_start_outside_working_hours(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The same rule the apply run keeps: it is the same account at four in the morning."""
    journal = a_journal(tmp_path / "agent.sqlite3", VACANCY)
    page = a_page()
    _install(monkeypatch, tmp_path, journal, page, hours=(clock(23, 58), clock(23, 59)))

    code, printed = run_main()

    assert code == 0
    assert page.visited == []
    assert "Обход не начат" in printed


def test_an_empty_journal_is_a_sentence_and_not_a_browser(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Nothing sent yet is the ordinary first-day state."""
    journal = a_journal(tmp_path / "agent.sqlite3")
    page = a_page()
    _install(monkeypatch, tmp_path, journal, page)

    code, printed = run_main()

    assert (code, page.visited) == (0, [])
    assert "обходить нечего" in printed


def test_a_walk_that_was_cut_short_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A run that stopped early is not a run that finished, and cron should see that."""
    journal = a_journal(tmp_path / "agent.sqlite3", VACANCY)
    page = a_page(lands_on={PAGE_URL: CHALLENGE_URL})
    _install(monkeypatch, tmp_path, journal, page)

    code, printed = run_main()

    assert code == 1
    assert "остановлен" in printed


def test_the_limit_bounds_how_many_pages_one_walk_opens(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One invocation may not turn into a long unattended session on the account."""
    journal = a_journal(tmp_path / "agent.sqlite3", VACANCY, OTHER)
    page = a_page(states=answering(VACANCY, OTHER))
    _install(monkeypatch, tmp_path, journal, page)

    code, _ = run_main(["--limit", "1"])

    # The first navigation is the session health check, on the page the signal
    # was measured on; the walk itself opened exactly one vacancy.
    assert (code, page.visited) == (0, [SIGNAL_URL, PAGE_URL])


def test_a_destination_that_is_not_a_backend_is_refused_before_the_browser_opens(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--to`` posts to the tracker; a path there would silently write nothing."""
    journal = a_journal(tmp_path / "agent.sqlite3", VACANCY)
    page = a_page()
    _install(monkeypatch, tmp_path, journal, page)

    with pytest.raises(SystemExit):
        run_main(["--to", "outcomes.json"])

    assert page.visited == []


def test_the_tracker_gets_the_same_results_the_local_file_gets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One reading, two destinations, and no second opinion between them."""
    journal = a_journal(tmp_path / "agent.sqlite3", VACANCY)
    memory = _install(monkeypatch, tmp_path, journal, a_page())
    posted: list[Result] = []

    class Tracker:
        """Stands in for the endpoint, and remembers what it was handed."""

        def report(self, results: Any) -> None:
            """What the walk decided to send."""
            posted.extend(results)

    monkeypatch.setattr(outcomes, "HttpQueue", lambda base_url: Tracker())
    code, _ = run_main(["--to", "http://localhost:8000"])

    assert code == 0
    assert [result.to_json() for result in posted] == [
        result.to_json() for result in ResultsFile(memory).read()
    ]
    assert posted[0].status == Status.SENT.value


def test_a_page_that_will_not_load_costs_one_vacancy_and_not_the_walk() -> None:
    """One dead page is not a reason to lose the pages that did load.

    Recorded against the vacancy it happened on, with the exception's own words,
    so that "hh had nothing to say" and "the tab timed out" do not arrive
    looking the same.
    """
    page = a_page(
        states=answering(VACANCY, OTHER),
        breaks=frozenset({PAGE_URL}),
    )

    read, stopped = walk(
        page,
        [an_entry(), an_entry(OTHER, OTHER_URL)],
        limits=unpaced(),
        rng=random.Random(1),
        sleep=_no_wait,
    )

    assert stopped is None
    assert not read[0].answered and "TimeoutError" in (read[0].note or "")
    assert read[1].answered


def test_two_dead_pages_in_a_row_stop_the_walk_and_point_at_the_screenshots() -> None:
    """The apply run's own rule: two in a row means something changed."""
    page = a_page(states={}, breaks=frozenset({PAGE_URL, OTHER_URL}))

    read, stopped = walk(
        page,
        [an_entry(), an_entry(OTHER, OTHER_URL), an_entry("999", None)],
        limits=unpaced(),
        rng=random.Random(1),
        sleep=_no_wait,
    )

    assert len(read) == 2
    assert stopped is not None and "agent/screenshots" in stopped


def test_two_states_at_once_are_shown_and_the_report_says_neither_is_written() -> None:
    """A person reading the run has to be told why the tracker stayed empty."""
    outcome = Outcome(vacancy_id=VACANCY, url=PAGE_URL, total=2, states=("DISCARD", "RESPONSE"))

    lines = " ".join(describe(outcome, None))

    assert "DISCARD, RESPONSE" in lines
    assert "не пишется ни одно" in lines


def test_an_unreadable_memory_does_not_stop_the_walk_that_would_replace_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Refusing to walk because last time's notes will not parse helps nobody.

    What is lost is the comparison, and only that, so the run says so and goes
    on to write a document the next run can read.
    """
    journal = a_journal(tmp_path / "agent.sqlite3", VACANCY)
    memory = _install(monkeypatch, tmp_path, journal, a_page())
    memory.write_text("не JSON вовсе", encoding="utf-8")

    code, printed = run_main()

    assert code == 0
    assert "сравнивать не с чем" in printed
    assert previous_states(ResultsFile(memory).read()) == {
        VACANCY: Seen(total=1, last_state="DISCARD")
    }


def test_a_machine_that_has_never_signed_in_is_told_which_step_it_missed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without ``session_signal.json`` nothing here knows what signed in looks like.

    That has to refuse rather than degrade: a walk that cannot tell an expired
    session from an account with no answers yet would record the second on
    seeing the first, and the record is the whole point.
    """
    journal = a_journal(tmp_path / "agent.sqlite3", VACANCY)
    _install(monkeypatch, tmp_path, journal, a_page())

    def unmeasured() -> list[str]:
        raise SignalUnknownError("Нет session_signal.json")

    monkeypatch.setattr(outcomes, "load_signal", unmeasured)

    with pytest.raises(outcomes.NotSignedInError):
        run_main()


def test_the_walks_memory_lands_where_git_already_refuses_to_look() -> None:
    """It records which jobs the owner applied to and who turned them down.

    That is the class of artefact this repository has committed by accident
    before, which is what
    ``test_isolation.py::test_everything_the_agent_writes_is_ignored_by_git``
    is about. ``agent/outcomes.json`` is the name this file deserves and it is
    one ``.gitignore`` line away; until that line exists the document lives in a
    directory git is already told to ignore.
    """
    ignored = (MODULE.parents[1] / ".gitignore").read_text(encoding="utf-8")
    relative = outcomes.OUTCOMES_PATH.relative_to(MODULE.parents[1]).as_posix()

    assert any(
        pattern and relative.startswith(pattern.rstrip("/") + "/")
        for pattern in ignored.splitlines()
        if not pattern.startswith("#")
    ), f"{relative} не закрыт ни одним правилом .gitignore"
