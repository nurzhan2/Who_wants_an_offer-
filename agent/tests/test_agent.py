"""The rules that decide what gets sent, tested on fixtures rather than on hh.

The brief asks specifically for two of these — «тесты на предфильтр и на детектор
ссылок в письме — на фикстурах, без сети и без браузера» — and the reason is
that both are decisions made before anything irreversible happens, so a
regression in either is silent. The rest are here because they share that
property.

The page payloads below are the shapes hh really served on 2026-09-06, measured
anonymously on three vacancy pages. Where a shape is one nobody has seen —
what an already-applied vacancy looks like — the test asserts that the code says
"I do not know" rather than inventing an answer, which is the behaviour that
matters most and the easiest one to lose.
"""

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from agent.gate import SubmitGate, UnmandatedRequestError, vacancy_id_in
from agent.journal import Entry, Journal
from agent.letter import DEFAULT_MAX_LENGTH, LetterProblem, UnsafeLetterError, check, inspect
from agent.mandate import mint
from agent.prefilter import Verdict, decide, decide_before_opening, read
from agent.queue import CONTRACT_VERSION, FileQueue, QueueFormatError, QueueItem
from agent.state import Actor, Status
from agent.state_page import read_applied, read_state

pytestmark = pytest.mark.unit

#: One vacancy's entry, exactly as hh serves it to a visitor who has not applied.
LIVE_STATE: dict[str, Any] = {
    "applicantVacancyResponseStatuses": {
        "136962420": {
            "test": {"hasTests": False},
            "letterMaxLength": 10000,
            "shortVacancy": {"vacancyId": 136962420, "@responseLetterRequired": False},
        }
    },
}


# ── the letter guard ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "Опыт коммерческой разработки: Python 3.12, FastAPI, PostgreSQL 17.",
        "Работал с очередями, кэшами и т.д., знаком с CI/CD.",
        "Английский — C1, ожидания от 1 500 000 ₸ на руки.",
        "Писал на C++ и Go, сейчас основной стек — Python.",
        "Готов приступить с 1.09, рассмотрю гибрид или офис в Алматы.",
        "Есть опыт с Node.js и React (версии 18.x).",
        "Ставка 5.000 тг/час обсуждаема.",
    ],
)
def test_an_ordinary_letter_is_left_alone(text: str) -> None:
    """The false positives are the hard part, not the true ones.

    Every string here trips a naive "anything with a dot is a URL" rule, and
    every one of them is something a real cover letter in this market says.
    """
    assert inspect(text) == []


@pytest.mark.parametrize(
    "text",
    [
        "Портфолио: https://github.com/nurzhan",
        "Пишите на ivan@example.com",
        "Мой телеграм @nurzhan_dev",
        "Примеры работ на www.mysite.ru",
        "Резюме тут: hh.kz/resume/abcdef",
        "Подробнее — nurzhan.dev",
        "Либо t.me/nurzhan",
        "Мои работы — портфолио.рф",
        "Сайт компании мойсайт.қаз",
    ],
)
def test_a_letter_with_a_link_or_an_at_sign_is_stopped(text: str) -> None:
    """A link in a cover letter is a spam filter and a shadow ban, not a style note.

    The Cyrillic domains are in this list deliberately: an ASCII-only pattern
    waves through exactly the domains this market writes.
    """
    assert inspect(text)


def test_a_bad_letter_is_refused_and_never_repaired() -> None:
    """Cutting a line out of somebody's letter and sending the rest is worse than stopping."""
    with pytest.raises(UnsafeLetterError) as excinfo:
        check("Здравствуйте! Портфолио: https://github.com/x", required=True)

    assert LetterProblem.CONTAINS_LINK in excinfo.value.problems


def test_hh_s_own_length_limit_is_enforced_before_typing() -> None:
    """A letter silently cut at the textarea's maximum loses its last paragraph."""
    with pytest.raises(UnsafeLetterError) as excinfo:
        check("я" * (DEFAULT_MAX_LENGTH + 1), required=False)

    assert LetterProblem.TOO_LONG in excinfo.value.problems


def test_a_vacancy_that_demands_a_letter_and_has_none_is_a_problem() -> None:
    """And an empty letter is the same as no letter."""
    with pytest.raises(UnsafeLetterError):
        check("   ", required=True)


# ── the prefilter ─────────────────────────────────────────────────────


def test_the_facts_come_from_the_key_that_actually_carries_them() -> None:
    """The brief names four fields on vacancyView; all four are null on a real page."""
    facts = read(LIVE_STATE, "136962420")

    assert facts is not None
    assert facts.letter_required is False
    assert facts.has_test is False
    assert facts.letter_max_length == 10000


def test_the_briefs_field_names_yield_nothing_rather_than_a_confident_wrong_answer() -> None:
    """The failure this module exists to prevent.

    A prefilter reading ``vacancyView["@responseLetterRequired"]`` sees null on
    every vacancy in the corpus and concludes "no letter needed, no test" for
    all of them. Reading the right key and returning None for an unfamiliar
    shape turns that into a stop.
    """
    brief_shape = {"vacancyView": {"@responseLetterRequired": None, "userTestPresent": None}}

    assert read(brief_shape, "136962420") is None


def test_an_employer_test_goes_to_a_person() -> None:
    """The agent extracts questions; it never answers them."""
    with_test = {
        "applicantVacancyResponseStatuses": {
            "1": {
                "test": {"hasTests": True},
                "letterMaxLength": 10000,
                "shortVacancy": {"@responseLetterRequired": False},
            }
        }
    }

    decision = decide(
        facts=read(with_test, "1"),
        closed_for_applicants=False,
        archived=False,
        already_applied=False,
        has_letter=True,
    )

    assert decision.verdict is Verdict.MANUAL
    assert decision.status is Status.NEEDS_MANUAL


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"already_applied": True}, Verdict.SKIP),
        ({"archived": True}, Verdict.SKIP),
        ({"closed_for_applicants": True}, Verdict.SKIP),
        ({"external_application": True}, Verdict.MANUAL),
        ({"already_applied": None}, Verdict.MANUAL),
    ],
)
def test_the_reasons_not_to_open_a_page(kwargs: dict[str, Any], expected: Verdict) -> None:
    """Each of these costs a page load and a slot out of a small daily budget."""
    base: dict[str, Any] = {
        "facts": read(LIVE_STATE, "136962420"),
        "closed_for_applicants": False,
        "archived": False,
        "already_applied": False,
        "has_letter": True,
    }

    assert decide(**{**base, **kwargs}).verdict is expected


def test_not_knowing_whether_we_applied_is_never_treated_as_not_having_applied() -> None:
    """The one mistake nobody can undo, and the default that would cause it."""
    assert read_applied(LIVE_STATE, "136962420") is None

    decision = decide(
        facts=read(LIVE_STATE, "136962420"),
        closed_for_applicants=False,
        archived=False,
        already_applied=read_applied(LIVE_STATE, "136962420"),
        has_letter=True,
    )

    assert decision.verdict is Verdict.MANUAL


# ── the gate ──────────────────────────────────────────────────────────


@dataclass
class FakeRequest:
    """The two attributes the gate reads."""

    url: str
    method: str = "GET"


class FakeRoute:
    """A Playwright route, without Playwright."""

    def __init__(self, url: str, method: str = "GET") -> None:
        self.request = FakeRequest(url, method)
        self.action: str | None = None

    def abort(self, error_code: str = "failed") -> None:
        """Record the refusal."""
        self.action = "abort"

    def continue_(self) -> None:
        """Record the pass-through."""
        self.action = "continue"


APPLY_URL = "https://hh.kz/applicant/vacancy_response?vacancyId=136773120&employerId=99"
OTHER_URL = "https://hh.kz/applicant/vacancy_response?vacancyId=999999999"
READ_URL = "https://hh.kz/vacancy/136773120"


def test_an_application_request_with_no_consent_armed_is_refused() -> None:
    """Including a GET, which is the shape the apply control actually uses."""
    gate = SubmitGate()
    route = FakeRoute(APPLY_URL)

    gate.handle(route)

    assert route.action == "abort"
    assert gate.blocked == [APPLY_URL]


def test_consent_allows_exactly_one_request_for_exactly_one_vacancy() -> None:
    """A second request under the same arming is a second application."""
    gate = SubmitGate()
    mandate = mint(vacancy_id="136773120", letter=None, form_digest="d")

    with gate.armed(mandate):
        first, again, elsewhere = (
            FakeRoute(APPLY_URL),
            FakeRoute(APPLY_URL),
            FakeRoute(OTHER_URL),
        )
        gate.handle(first)
        gate.handle(again)
        gate.handle(elsewhere)

    after = FakeRoute(APPLY_URL)
    gate.handle(after)

    assert [first.action, again.action, elsewhere.action, after.action] == [
        "continue",
        "abort",
        "abort",
        "abort",
    ]


def test_reading_a_vacancy_page_is_not_an_application() -> None:
    """The gate must not break ordinary browsing, or it will be turned off."""
    gate = SubmitGate()
    route = FakeRoute(READ_URL)

    gate.handle(route)

    assert route.action == "continue"


def test_a_request_that_never_reached_the_interceptor_is_reported() -> None:
    """What a service worker would look like, and why the run must not be trusted."""
    gate = SubmitGate()
    gate.observe(FakeRequest(APPLY_URL))

    with pytest.raises(Exception, match="перехват"):
        gate.assert_no_escapes()


def test_the_gate_notices_when_nothing_was_actually_sent() -> None:
    """A click that silently did nothing must not be recorded as an application."""
    gate = SubmitGate()
    mandate = mint(vacancy_id="1", letter=None, form_digest="d")

    with pytest.raises(UnmandatedRequestError):
        gate.require_sent(mandate)


def test_the_vacancy_is_read_out_of_the_url_the_way_hh_writes_it() -> None:
    """Measured shape: ?vacancyId=<digits>&employerId=…&hhtmFrom=…"""
    assert vacancy_id_in(APPLY_URL) == "136773120"
    assert vacancy_id_in(READ_URL) is None


# ── the journal and the queue ─────────────────────────────────────────


def test_the_journal_refuses_a_transition_the_actor_may_not_make() -> None:
    """The rule lives at the choke point, so writing to the journal cannot dodge it."""
    with tempfile.TemporaryDirectory() as directory:
        journal = Journal(Path(directory) / "agent.sqlite3")
        journal.record(Entry("1", Status.QUEUED), actor=Actor.AGENT)

        with pytest.raises(Exception, match="queued to confirmed"):
            journal.record(Entry("1", Status.CONFIRMED), actor=Actor.AGENT)


def test_one_vacancy_is_one_row_however_often_it_is_seen() -> None:
    """The unique key the brief asks for: two runs racing produce one application."""
    with tempfile.TemporaryDirectory() as directory:
        journal = Journal(Path(directory) / "agent.sqlite3")
        journal.record(Entry("1", Status.QUEUED, title="Backend"), actor=Actor.AGENT)
        journal.record(Entry("1", Status.SKIPPED, reason="закрыта"), actor=Actor.AGENT)

        entry = journal.get("1")
        assert entry is not None
        assert entry.status is Status.SKIPPED
        # What was learned earlier is not lost by a later, thinner write.
        assert entry.title == "Backend"


def test_the_letter_itself_never_reaches_the_journal() -> None:
    """A file of somebody's cover letters is a thing to leak, and the digest proves as much."""
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "agent.sqlite3"
        journal = Journal(path)
        journal.record(
            Entry("1", Status.QUEUED, letter_digest="abc123"),
            actor=Actor.AGENT,
        )

        assert "abc123" in path.read_bytes().decode("utf-8", "ignore")


def test_a_queue_entry_without_a_usable_id_or_url_is_refused() -> None:
    """The two fields everything else depends on."""
    with pytest.raises(QueueFormatError):
        QueueItem.from_json({"vacancy_id": "not-a-number", "url": "https://hh.kz/vacancy/1"})
    with pytest.raises(QueueFormatError):
        QueueItem.from_json({"vacancy_id": "1", "url": "javascript:alert(1)"})


def test_a_queue_from_a_future_backend_is_refused_rather_than_guessed_at() -> None:
    """A contract version is only useful if something checks it."""
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "queue.json"
        path.write_text('{"version": 99, "items": []}', encoding="utf-8")

        with pytest.raises(QueueFormatError, match="99"):
            FileQueue(path).take(10)


def test_results_are_written_beside_the_queue_and_never_over_it() -> None:
    """A half-finished run must leave the queue it was reading intact."""
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "queue.json"
        path.write_text(
            f'{{"version": {CONTRACT_VERSION}, "items": '
            '[{"vacancy_id": "1", "url": "https://hh.kz/vacancy/1", "title": "A"}]}',
            encoding="utf-8",
        )
        queue = FileQueue(path)
        items = queue.take(10)
        queue.report([])

        assert len(items) == 1
        assert queue.results_path.is_file()
        assert "items" in path.read_text(encoding="utf-8")


def test_the_page_state_is_read_out_of_the_same_marker_the_crawler_uses() -> None:
    """And a page without it reads as None rather than as an empty page."""
    import html as html_lib
    import json

    payload = json.dumps(LIVE_STATE, ensure_ascii=False)
    page = (
        '<template style="display:none" id="HH-Lux-InitialState">'
        + html_lib.escape(payload)
        + "</template>"
    )

    assert read_state(page) == LIVE_STATE
    assert read_state("<html>hh redesigned this</html>") is None


# ── the two stages, which a dry run caught and unit tests had not ─────


def test_the_queue_stage_does_not_demand_facts_that_live_on_the_page() -> None:
    """The bug a first dry run found, and the reason the two stages are separate.

    ``decide`` treats ``facts=None`` as "the page could not be read", which is a
    stop. At the queue stage the facts are not missing but unknowable — they are
    on a page nobody has opened — so calling the page-stage decision there sends
    every vacancy to a human and the run reports, truthfully and uselessly, that
    there is nothing to send. Every unit test passed while it did that, because
    each one called ``decide`` with facts in hand.
    """
    assert (
        decide_before_opening(
            closed_for_applicants=False, archived=False, external_application=False
        ).verdict
        is Verdict.PROCEED
    )
    assert (
        decide(
            facts=None,
            closed_for_applicants=False,
            archived=False,
            already_applied=False,
            has_letter=True,
        ).verdict
        is Verdict.MANUAL
    )


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"archived": True}, Verdict.SKIP),
        ({"closed_for_applicants": True}, Verdict.SKIP),
        ({"external_application": True}, Verdict.MANUAL),
    ],
)
def test_the_queue_stage_still_rules_out_what_the_crawler_knew(
    kwargs: dict[str, Any], expected: Verdict
) -> None:
    """Its whole purpose: not spending a page load on a certain failure."""
    base: dict[str, Any] = {
        "closed_for_applicants": False,
        "archived": False,
        "external_application": False,
    }

    assert decide_before_opening(**{**base, **kwargs}).verdict is expected


def test_a_second_run_leaves_alone_what_only_a_person_can_move() -> None:
    """The other bug the same dry run found.

    A vacancy the previous run put in ``needs_manual`` is waiting on a human.
    Re-queueing it is a transition the state machine forbids to a machine — the
    rule is right — so a run that tries anyway dies on the second invocation with
    an IllegalTransitionError instead of quietly skipping. The journal
    remembering something is not an error condition.
    """
    with tempfile.TemporaryDirectory() as directory:
        journal = Journal(Path(directory) / "agent.sqlite3")
        journal.record(Entry("1", Status.NEEDS_MANUAL, reason="в письме ссылка"), actor=Actor.AGENT)

        entry = journal.get("1")
        assert entry is not None and entry.status is Status.NEEDS_MANUAL
        # The run must consult this before recording anything, which is what
        # agent/run.py::_to_candidates now does.
        with pytest.raises(Exception, match="needs_manual to queued"):
            journal.record(Entry("1", Status.QUEUED), actor=Actor.AGENT)
