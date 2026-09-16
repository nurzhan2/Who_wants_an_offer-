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
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, final

import pytest

from agent import selectors, submit
from agent.gate import (
    InterceptionEscapedError,
    SubmitGate,
    is_the_application_itself,
    looks_like_an_application,
    vacancy_id_in,
    vacancy_ids_in,
)
from agent.journal import Entry, Journal
from agent.letter import DEFAULT_MAX_LENGTH, LetterProblem, UnsafeLetterError, check, inspect
from agent.mandate import SendMandate, digest, mint, verify
from agent.prefilter import (
    Verdict,
    decide,
    decide_before_opening,
    read,
    read_status,
)
from agent.queue import (
    CONTRACT_VERSION,
    FileQueue,
    MergedQueue,
    QueueFormatError,
    QueueItem,
    Result,
    ResultsFile,
)
from agent.state import Actor, Status
from agent.state_page import (
    Application,
    FormWarnings,
    Negotiations,
    mentions_visibility,
    printable,
    read_applied,
    read_form_warnings,
    read_negotiations,
    read_state,
)

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
    """The three attributes the gate reads, and one way for the third to fail."""

    url: str
    method: str = "GET"
    _post_data: str | None = None
    #: Playwright raises on some request bodies instead of returning None.
    explode_on_body: bool = False

    @property
    def post_data(self) -> str | None:
        """The body, or an exception the gate has to survive."""
        if self.explode_on_body:
            raise RuntimeError("playwright declines to produce this body")
        return self._post_data


class FakeRoute:
    """A Playwright route, without Playwright."""

    def __init__(self, url: str, method: str = "GET", post_data: str | None = None) -> None:
        self.request = FakeRequest(url, method, post_data)
        self.action: str | None = None

    def abort(self, error_code: str = "failed") -> None:
        """Record the refusal."""
        self.action = "abort"

    def continue_(self) -> None:
        """Record the pass-through."""
        self.action = "continue"


APPLY_URL = "https://hh.kz/applicant/vacancy_response?vacancyId=136773120&employerId=99"
OTHER_URL = "https://hh.kz/applicant/vacancy_response?vacancyId=999999999"
READ_URL = "https://almaty.hh.kz/vacancy/136773120"
#: What the form itself sends, once it is open. A second application-shaped URL
#: for the same job - which is why one arming has to allow more than one request.
SEND_URL = "https://hh.kz/applicant/vacancy_response?vacancyId=136773120&lux=true"


def hh_furniture(vacancy_id: str) -> tuple[str, ...]:
    """The four hh endpoints measured carrying a vacancy id with nothing being sent.

    Shapes taken from ``requests_allowed`` in
    ``agent/probe/20260906-181519/probe.json``, a run recorded at
    ``stage: open-form``: the response form was opened and the run stopped before
    submitting, so none of these can be the request that sends. Twenty of that
    run's twenty-one requests were one of these; the twenty-first was the form's
    own card, which is application-shaped and matched by path.

    Trimmed to the parameters this module actually reads, and trimmed on purpose:
    the recorded URLs also carry the owner's hh id, a request id and a browser
    fingerprint, ``agent/probe/`` is gitignored for exactly that reason, and this
    file is committed. The vacancy id is substituted rather than kept, so the
    same shapes can be tested against a mandate.
    """
    return (
        f"https://almaty.hh.kz/anatskytics?hhtmSource=vacancy&vacancyId={vacancy_id}",
        f"https://almaty.hh.kz/applicant/blacklist/state?vacancyId={vacancy_id}",
        f"https://almaty.hh.kz/shards/vacancies/feedback/roulette?vacancyId={vacancy_id}",
        "https://employer-reviews-front.hh.kz/employer_reviews/proxy_components"
        f"/complain_button?vacancyId={vacancy_id}",
    )


#: The card inside the response modal, which arrives on its own request after the
#: apply link is followed. Measured in ``agent/probe/form_136131345.json``; the
#: fingerprint parameter it carries is dropped for the reason above.
POPUP_URL = (
    "https://almaty.hh.kz/applicant/vacancy_response/popup"
    "?vacancyId=136773120&isTest=no&withoutTest=no&lux=true&alreadyApplied=false"
)


def test_an_application_request_with_no_consent_armed_is_refused() -> None:
    """Including a GET, which is the shape the apply control actually uses."""
    gate = SubmitGate()
    route = FakeRoute(APPLY_URL)

    gate.handle(route)

    assert route.action == "abort"
    assert gate.blocked == [APPLY_URL]


def test_consent_covers_this_vacancy_and_nothing_repeats_inside_it() -> None:
    """The window is the flow, so the form-opening request and the send both pass.

    What is refused inside it: the same URL twice (a retry of an application is
    a second application) and any URL naming another vacancy. What is refused
    outside it: everything.
    """
    gate = SubmitGate()
    mandate = mint(vacancy_id="136773120", url=READ_URL, letter=None, form_digest="d")

    with gate.armed(mandate):
        opening, sending, again, elsewhere = (
            FakeRoute(APPLY_URL),
            FakeRoute(SEND_URL),
            FakeRoute(APPLY_URL),
            FakeRoute(OTHER_URL),
        )
        gate.handle(opening)
        gate.handle(sending)
        gate.handle(again)
        gate.handle(elsewhere)

    after = FakeRoute(APPLY_URL)
    gate.handle(after)

    assert [opening.action, sending.action, again.action, elsewhere.action, after.action] == [
        "continue",
        "continue",
        "abort",
        "abort",
        "abort",
    ]


def test_a_repeat_is_refused_even_with_something_else_in_between() -> None:
    """The old guard compared only the previously allowed URL, so A, B, A passed."""
    gate = SubmitGate()
    mandate = mint(vacancy_id="136773120", url=READ_URL, letter=None, form_digest="d")

    with gate.armed(mandate):
        first, other, repeat = FakeRoute(APPLY_URL), FakeRoute(SEND_URL), FakeRoute(APPLY_URL)
        gate.handle(first)
        gate.handle(other)
        gate.handle(repeat)

    assert [first.action, other.action, repeat.action] == ["continue", "continue", "abort"]


@pytest.mark.parametrize(
    "url,body",
    [
        # The four spellings of "this request is about vacancy 999999999".
        ("https://hh.kz/applicant/vacancy_response?vacancyId=999999999", None),
        ("https://hh.kz/applicant/vacancy_response?vacancyId%3D999999999", None),
        ("https://hh.kz/applicant/vacancy_response/999999999?vacancyId=136773120", None),
        (
            "https://hh.kz/applicant/vacancy_response?vacancyId=136773120",
            '{"vacancyId": "999999999", "letter": ""}',
        ),
    ],
)
def test_a_cross_vacancy_request_is_refused_however_it_names_the_other_job(
    url: str, body: str | None
) -> None:
    """Three of these four walked through the query-string-only version."""
    gate = SubmitGate()
    mandate = mint(vacancy_id="136773120", url=READ_URL, letter=None, form_digest="d")
    route = FakeRoute(url, post_data=body)

    with gate.armed(mandate):
        gate.handle(route)

    assert route.action == "abort"
    assert "999999999" in gate.refused_because[-1]


def test_the_gate_survives_a_request_whose_body_cannot_be_read() -> None:
    """Playwright raises on some bodies, and a gate that dies while deciding fails open."""
    gate = SubmitGate()
    mandate = mint(vacancy_id="136773120", url=READ_URL, letter=None, form_digest="d")
    route = FakeRoute(APPLY_URL)
    route.request.explode_on_body = True

    with gate.armed(mandate):
        gate.handle(route)

    assert route.action == "continue"


def test_playwright_can_actually_register_the_gate_as_a_route_handler() -> None:
    """The gate must not be a slots dataclass, and only playwright can say so.

    ``wrap_handler`` caches its wrapper with ``setattr`` on the bound method's
    owner. Against a slots instance that raises AttributeError - on the first
    line after the browser opens, which is right after the human confirmed.
    Every other test here uses hand-written fakes and cannot see it.
    """
    mapping = pytest.importorskip("playwright._impl._impl_to_api_mapping")
    gate = SubmitGate()

    mapping.ImplToApiMapping().wrap_handler(gate.handle)
    mapping.ImplToApiMapping().wrap_handler(gate.observe)


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


def test_a_submit_click_the_gate_did_not_recognise_is_not_treated_as_a_failure() -> None:
    """The gate does not know what hh's submit emits, so it must not judge one.

    This is the shape of the blocker fixed on 2026-09-07. ``require_progress``
    raised here, on the far side of the irreversible click, so hh accepting an
    application through a request this module does not recognise — which is what
    every hh request anybody *has* recorded looks like — was reported as
    ``failed`` and the owner was invited to send it again.

    Restore the raise and this test fails: it asserts the call returns, and that
    the record it leaves says the click produced nothing rather than concluding
    anything from that.
    """
    gate = SubmitGate()
    mandate = mint(vacancy_id="136773120", url=READ_URL, letter=None, form_digest="d")

    with gate.armed(mandate):
        gate.handle(FakeRoute(APPLY_URL))
        mark = gate.mark()
        # ...and the submit click produces nothing the gate can see.
        click = gate.note_submit_click(mandate, since=mark)

    assert click.allowed == ()
    assert click.refused == ()
    assert gate.submit_clicks == [click]
    assert click.vacancy_id == "136773120"


def test_what_a_submit_click_put_on_the_wire_is_recorded_against_that_click() -> None:
    """The record is per click, so the form-opening request is not credited to it.

    That is the whole reason the mark is taken before the click rather than the
    window being read as a total: the apply link is application-shaped too, and a
    number that counted it would say "something was sent" on every run.
    """
    gate = SubmitGate()
    mandate = mint(vacancy_id="136773120", url=READ_URL, letter=None, form_digest="d")

    with gate.armed(mandate):
        gate.handle(FakeRoute(APPLY_URL))
        mark = gate.mark()
        gate.handle(FakeRoute(SEND_URL))
        click = gate.note_submit_click(mandate, since=mark)

    assert click.allowed == (SEND_URL,)


def test_a_refusal_during_the_send_is_recorded_with_the_click_that_caused_it() -> None:
    """The half of the record that *is* unambiguous.

    An empty ``allowed`` means nothing — hh's submit may be a shape this module
    does not recognise. A refusal is different: the gate aborting something in
    the middle of a send is the gate interfering with the send, and it is the one
    thing here a person should act on.
    """
    gate = SubmitGate()
    mandate = mint(vacancy_id="136773120", url=READ_URL, letter=None, form_digest="d")

    with gate.armed(mandate):
        gate.handle(FakeRoute(SEND_URL))
        mark = gate.mark()
        gate.handle(FakeRoute(SEND_URL))  # a repeat, inside one confirmation
        click = gate.note_submit_click(mandate, since=mark)

    assert click.allowed == ()
    assert len(click.refused) == 1
    assert SEND_URL in click.refused[0]


#: hh's analytics call as it left the page on 2026-09-16
#: (``agent/probe/20260916-125039``): a POST with the vacancy id in its query.
#: Fifteen of these were reported as escaped applications on that run.
BEACON_20260916 = (
    "https://almaty.hh.kz/anatskytics?active=true&activeTab=main&archived=false"
    "&disabled=false&hhtmSource=vacancy&vacancyId=136717950&waiting=false"
    "&event=button_click&buttonName=vacancy_response_letter_toggle"
)


def test_hhs_own_furniture_is_not_an_application_armed_or_not() -> None:
    """A vacancy id in the query is not applying; the path is.

    Until 2026-09-16 these were refused outside an armed window on the grounds
    that they carry ``vacancyId``. The same rule made the page recorder report
    every analytics POST as an escaped application and failed each run. The
    apply link itself is still refused with nothing armed.
    """
    gate = SubmitGate()

    routes = [FakeRoute(url) for url in (*hh_furniture("136773120"), BEACON_20260916)]
    for route in routes:
        gate.handle(route)
    apply_link = FakeRoute(APPLY_URL)
    gate.handle(apply_link)

    assert [route.action for route in routes] == ["continue"] * 5
    assert apply_link.action == "abort"
    assert gate.blocked == [APPLY_URL]


def test_an_analytics_call_the_router_never_saw_is_not_an_escape() -> None:
    """The failure measured on 2026-09-16, reproduced.

    The probe aborted hh's analytics POSTs in its own non-GET guard, the page
    recorder still saw them, and each one read as an application that got past
    the interceptor. They are not applications, so there is nothing to escape.
    """
    gate = SubmitGate()

    gate.observe(FakeRequest(BEACON_20260916))

    gate.assert_no_escapes()


def test_an_application_the_router_never_saw_is_still_an_escape() -> None:
    """The check itself must survive the narrowing."""
    gate = SubmitGate()

    gate.observe(FakeRequest(POPUP_URL))

    with pytest.raises(InterceptionEscapedError):
        gate.assert_no_escapes()


def test_hhs_own_furniture_does_not_fill_the_window_or_count_as_a_repeat() -> None:
    """Measured: seventeen identical beacons on one page load.

    Under the old rule the first one entered the window and the next sixteen were
    aborted as «a second application» — real hh traffic, aborted in the middle of
    a real apply flow, on a page the owner was watching. And the window then
    reported that plenty had been sent when nothing had.
    """
    gate = SubmitGate()
    mandate = mint(vacancy_id="136773120", url=READ_URL, letter=None, form_digest="d")
    beacon = hh_furniture("136773120")[0]

    with gate.armed(mandate):
        routes = [FakeRoute(beacon) for _ in range(17)]
        for route in routes:
            gate.handle(route)
        counted = gate.requests_in_window()

    assert [route.action for route in routes] == ["continue"] * 17
    assert counted == 0
    assert gate.blocked == []


def test_the_popup_and_the_apply_link_are_both_the_application_itself() -> None:
    """Both halves of the flow that *was* measured still count, so repeats of them are refused."""
    assert is_the_application_itself(APPLY_URL)
    assert is_the_application_itself(POPUP_URL)
    assert is_the_application_itself(SEND_URL)


@pytest.mark.parametrize("url", [*hh_furniture("136773120"), BEACON_20260916])
def test_a_vacancy_id_in_the_query_does_not_make_a_request_an_application(url: str) -> None:
    """Neither question is answered by a parameter every beacon carries."""
    assert not looks_like_an_application(url)
    assert not is_the_application_itself(url)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/applicant/vacancy_response", True),
        ("/applicant/vacancy_response/popup", True),
        ("/applicant/vacancy_response/send", True),
        ("/shards/applicant/negotiations", False),
        ("/vacancy/136773120", False),
    ],
)
def test_everything_under_the_response_path_is_an_application(path: str, expected: bool) -> None:
    """Measured as the apply link and the form's card; anything below it counts too.

    hh's own submit request has never been recorded. If it lives under this
    path, it is refused without a mandate and counted inside one.
    """
    url = f"https://almaty.hh.kz{path}?vacancyId=136773120"

    assert looks_like_an_application(url) is expected
    assert is_the_application_itself(url) is expected


def test_the_vacancy_is_read_out_of_the_url_however_it_is_written() -> None:
    """Measured shape, plus the path form a page URL uses."""
    assert vacancy_id_in(APPLY_URL) == "136773120"
    assert vacancy_id_in(READ_URL) == "136773120"
    assert vacancy_ids_in(SEND_URL, '{"vacancyId": "999999999"}') == {"136773120", "999999999"}
    assert vacancy_id_in("https://hh.kz/search/vacancy?text=python") is None


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
    """A file of somebody's cover letters is a thing to leak.

    The assertion that matters is the *absence* of the text. This test used to
    check that the digest was present, which is the opposite direction: writing
    the whole letter into that column left it green.
    """
    letter = "Здравствуйте! Меня зовут Нуржан, и я хотел бы работать у вас."
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "agent.sqlite3"
        journal = Journal(path)
        journal.record(
            Entry("1", Status.QUEUED, letter_digest=digest(letter)),
            actor=Actor.AGENT,
        )

        written = path.read_bytes().decode("utf-8", "ignore")
        assert letter not in written, "the cover letter is stored verbatim on disk"
        for fragment in ("Нуржан", "Здравствуйте"):
            assert fragment not in written
        assert digest(letter) in written


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


# ── the queue is the backend's, and the file is an addition to it ─────
#
# For a year ``agent/queue.json`` was the only source, so a night of crawling,
# scoring and letter-writing reached the agent as whatever a person had typed
# into that file — once, a month earlier. These tests are about the arrangement
# that ends it: the backend answers what is worth applying to, and the file adds
# to that answer instead of standing in for it.


@final
class _Recording:
    """A queue that answers with what it was given and remembers what it heard."""

    def __init__(self, items: Sequence[QueueItem] = ()) -> None:
        self.items = list(items)
        self.reported: list[Result] = []
        self.asked_for: list[int] = []

    def take(self, limit: int) -> Sequence[QueueItem]:
        """The items, cut to the limit, with the limit recorded."""
        self.asked_for.append(limit)
        return self.items[:limit]

    def report(self, results: Sequence[Result]) -> None:
        """Keep what was handed back."""
        self.reported.extend(results)


def _item(vacancy_id: str, title: str = "A") -> QueueItem:
    """One queue item, with the two fields everything else depends on."""
    return QueueItem.from_json(
        {"vacancy_id": vacancy_id, "url": f"https://hh.kz/vacancy/{vacancy_id}", "title": title}
    )


def test_the_backend_leads_and_the_hand_written_file_follows() -> None:
    """Order is the point: a scored vacancy must not be pushed down the list.

    Both halves are offered, and the backend's come first. An entry somebody
    typed into the file carries no score at all, so showing it above a scored
    row would hide the only reason there is to prefer one vacancy over another.
    """
    backend = _Recording([_item("1"), _item("2")])
    extra = _Recording([_item("3")])

    items = MergedQueue(backend=backend, extra=extra).take(10)

    assert [item.vacancy_id for item in items] == ["1", "2", "3"]


def test_a_vacancy_the_backend_already_offered_is_not_offered_twice() -> None:
    """The file is where a person adds what the queue missed, and they overlap.

    A duplicate is not harmless: the run looks at each item once, so a second
    copy of a vacancy is a slot spent on nothing — and the copy that would win
    is the one with no score and no letter behind it.
    """
    backend = _Recording([_item("1", "scored")])
    extra = _Recording([_item("1", "typed by hand"), _item("2")])

    items = MergedQueue(backend=backend, extra=extra).take(10)

    assert [(item.vacancy_id, item.title) for item in items] == [("1", "scored"), ("2", "A")]


def test_the_batch_limit_holds_across_both_sources() -> None:
    """The daily cap is enforced later; this is the limit on what is looked at."""
    backend = _Recording([_item("1"), _item("2")])
    extra = _Recording([_item("3")])

    items = MergedQueue(backend=backend, extra=extra).take(2)

    assert [item.vacancy_id for item in items] == ["1", "2"]


def test_a_run_with_no_hand_written_file_is_the_ordinary_case() -> None:
    """Most runs have nothing added by hand, and that is not a missing file."""
    backend = _Recording([_item("1")])

    assert [item.vacancy_id for item in MergedQueue(backend=backend).take(10)] == ["1"]


def test_results_reach_the_local_file_before_the_tracker() -> None:
    """The one outcome worth engineering against: applications sent, nobody told.

    The file write cannot fail for a reason outside this machine; the POST can.
    So the record is written first and the hand-over second, and the results of
    hand-added items go too — an id the backend does not know writes nothing at
    the far end, and one it does know is an application its tracker should have.
    """
    with tempfile.TemporaryDirectory() as directory:
        memory = ResultsFile(Path(directory) / "queue-results.json")
        backend = _Recording()
        queue = MergedQueue(backend=backend, extra=_Recording([_item("2")]), memory=memory)
        results = [Result("1", "sent"), Result("2", "sent")]

        queue.take(10)
        queue.report(results)

        assert [result.vacancy_id for result in memory.read()] == ["1", "2"]
        assert [result.vacancy_id for result in backend.reported] == ["1", "2"]


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


# ── technology names that are spelled like domains ────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "Пять лет на ASP.NET, сейчас перехожу на Python.",
        "Реалтайм делал на socket.io.",
        "Стек: ASP.NET Core, PostgreSQL, Redis.",
        "Знаком с Socket.IO и с вебсокетами напрямую.",
        "Использую socket.io: комнаты, подписки, реконнект.",
    ],
)
def test_a_technology_that_is_spelled_like_a_domain_is_not_a_link(text: str) -> None:
    """Verified by running the module: both of these were refused.

    ``ASP.NET`` and ``socket.io`` end in a real top-level domain and are
    therefore indistinguishable from ``nurzhan.dev`` by shape alone. They are
    also ordinary words in a backend CV, so the guard was sending a perfectly
    good letter to a person for nothing — and a warning that fires on nothing is
    a warning that stops being read.
    """
    assert inspect(text) == []


@pytest.mark.parametrize(
    "text",
    [
        "Документация тут: socket.io/docs",
        "Хост asp.net.example.ru",
        "Смотрите vb.net.mysite.kz",
    ],
)
def test_the_technology_exception_does_not_open_a_hole(text: str) -> None:
    """A technology name with a path is an address, and so is a longer host.

    The exception is matched against the whole token the pattern found, which is
    what keeps ``socket.io`` a library and ``socket.io/docs`` a link.
    """
    assert inspect(text)


# ── idempotency, which is a number ────────────────────────────────────

#: The measured entry for a vacancy with no application: 2026-09-06, the owner's
#: logged-in profile, vacancy 136962420.
NO_APPLICATION: dict[str, Any] = {
    "applicantVacancyResponseStatuses": {
        "136962420": {
            "test": {"hasTests": False},
            "letterMaxLength": 10000,
            "shortVacancy": {"@responseLetterRequired": False},
            "negotiations": {
                "topicList": [],
                "total": 0,
                "readOnlyInterval": 180,
                "untrustedEmployerRestrictionsApplied": None,
            },
            "alreadyApplied": False,
            "responseImpossible": False,
        }
    },
}

#: The measured entry for a vacancy that *has* an application: vacancy 133542745,
#: same session. Note ``alreadyApplied`` — hh really does say ``false`` there.
ONE_APPLICATION: dict[str, Any] = {
    "applicantVacancyResponseStatuses": {
        "133542745": {
            "test": {"hasTests": False},
            "letterMaxLength": 10000,
            "shortVacancy": {"@responseLetterRequired": False},
            "negotiations": {
                "topicList": [
                    {
                        "id": 5347572809,
                        "chatId": 5389562648,
                        "initialState": "RESPONSE",
                        "lastState": "DISCARD",
                        "vacancyId": 133542745,
                    }
                ],
                "total": 1,
                "readOnlyInterval": 180,
                "untrustedEmployerRestrictionsApplied": None,
            },
            "alreadyApplied": False,
            "responseImpossible": False,
        }
    },
}


def test_no_application_reads_as_no_application() -> None:
    """``total`` at 0 with an empty ``topicList``, which is what hh serves."""
    assert read_applied(NO_APPLICATION, "136962420") is False


def test_one_application_reads_as_applied() -> None:
    """``total`` at 1. The count decides; nothing else is consulted."""
    assert read_applied(ONE_APPLICATION, "133542745") is True


def test_the_key_called_already_applied_is_not_the_answer() -> None:
    """Measured: it was ``false`` on the vacancy that had an application.

    This is the whole argument for reading a count instead of a flag, and it is
    also why the apply *button* is not the answer either: hh permits a repeat
    application, so an already-applied vacancy still renders an apply control.
    """
    entry = ONE_APPLICATION["applicantVacancyResponseStatuses"]["133542745"]

    assert entry["alreadyApplied"] is False
    assert read_applied(ONE_APPLICATION, "133542745") is True


def test_the_outcome_of_the_application_comes_free_with_the_page() -> None:
    """``lastState`` is what the site shows as «Вам отказали». Worth storing."""
    negotiations = read_negotiations(ONE_APPLICATION, "133542745")

    assert negotiations is not None
    assert negotiations.total == 1
    assert negotiations.exists is True
    application = negotiations.applications[0]
    assert (application.topic_id, application.chat_id) == (5347572809, 5389562648)
    assert (application.initial_state, application.last_state) == ("RESPONSE", "DISCARD")


@pytest.mark.parametrize(
    "state",
    [
        pytest.param(LIVE_STATE, id="no negotiations key at all"),
        pytest.param({"applicantVacancyResponseStatuses": {}}, id="no entry for this vacancy"),
        pytest.param(
            {"applicantVacancyResponseStatuses": {"136962420": {"negotiations": {"total": "1"}}}},
            id="total is a string",
        ),
        pytest.param(
            {"applicantVacancyResponseStatuses": {"136962420": {"negotiations": {}}}},
            id="negotiations without a total",
        ),
        pytest.param({}, id="not a vacancy page"),
    ],
)
def test_a_shape_this_code_does_not_know_still_answers_none(state: dict[str, Any]) -> None:
    """The three-valued contract, which is the thing that must never be lost.

    None of these may read as ``False``. "I could not tell" turning into "not
    applied yet" is the design that double-applies to the whole queue at once on
    the day hh renames a key, and a second application is the one thing the
    owner cannot take back.
    """
    assert read_applied(state, "136962420") is None


# ── the response modal, classified by what it says ────────────────────

#: hh writes a non-breaking space wherever a short word must not end a line.
#: Spelled with :func:`chr` rather than as a literal, because the character
#: is invisible in an editor and this fixture is worthless if it silently
#: turns into an ordinary space.
NBSP = chr(0xA0)

#: The modal measured in full on vacancy 136131345, six seconds after the
#: click, on the owner's logged-in profile.
MEASURED_MODAL = (
    "Отклик на вакансию\n"
    "Python Backend Trainee\n"
    f"Чтобы откликнуться на{NBSP}эту вакансию, поменяйте видимость резюме "
    f"на{NBSP}«Видно компаниям-клиентам HeadHunter»\n"
    "Python-разработчик\n"
    "Такой отклик может получить отказ\n"
    f"Английский язык в{NBSP}резюме{NBSP}«Python-разработчик» ниже обязательного "
    "уровня, который указал работодатель.\n"
    "Добавить сопроводительное\n"
    "Откликнуться"
)


def test_the_visibility_notice_is_read_in_hh_s_own_words_and_stops_nothing() -> None:
    """It used to be a hard stop. It was a guess, and it blocked every send.

    Rewritten 2026-09-07 from ``test_the_visibility_demand_is_a_hard_stop_in_hh_s
    _own_words``, which asserted ``may_send is False`` on this exact card. hh
    accepts these applications — measured the same day, recorded in
    ``agent/evidence/20260907-send-under-visibility-notice.json`` — so what is
    pinned here is the half that was always right: hh's sentence is read out
    whole, quoted rather than paraphrased, because it names a setting under a
    name the owner can search for. The non-breaking spaces are why matching
    happens on a normalised copy: the string hh serves is not the string
    anybody would type.
    """
    warnings = read_form_warnings(MEASURED_MODAL)

    assert warnings.visibility is not None
    assert "поменяйте видимость резюме" in warnings.visibility
    assert "«Видно компаниям-клиентам HeadHunter»" in warnings.visibility
    # Nothing on this object can be asked "may I send" any more. The properties
    # that answered it are gone, and this is what stops them coming back.
    assert not hasattr(warnings, "may_send")
    assert not hasattr(warnings, "verdict")


def test_both_things_hh_says_are_kept_and_neither_outranks_the_other() -> None:
    """They arrived on one card, and they are about different things.

    «может получить отказ» is about this vacancy — it names the requirement the
    resume misses. The visibility notice is about the resume, so it is equally
    true of every other application in the batch. A single-valued verdict used
    to hide the first behind the second whenever both were present.
    """
    warnings = read_form_warnings(MEASURED_MODAL)

    assert warnings.likely_rejection is not None
    assert "может получить отказ" in warnings.likely_rejection
    assert "ниже обязательного уровня" in warnings.likely_rejection
    assert "Добавить сопроводительное" not in warnings.likely_rejection
    # In the order hh had them on the card, which is the order they are shown in.
    assert warnings.said == (warnings.visibility, warnings.likely_rejection)


def test_the_likely_rejection_warning_on_its_own_is_read_and_carried() -> None:
    """It is an opinion about the odds, and it was never a refusal."""
    warnings = read_form_warnings(
        "Отклик на вакансию\nPython-разработчик\n"
        "Такой отклик может получить отказ\n"
        "Опыт работы меньше, чем указал работодатель.\n"
        "Откликнуться"
    )

    assert warnings.visibility is None
    assert warnings.likely_rejection is not None
    assert "меньше, чем указал работодатель" in warnings.likely_rejection
    assert len(warnings.said) == 1


def test_a_warning_in_words_nobody_has_seen_is_not_invented_into_a_meaning() -> None:
    """An unrecognised sentence is reported as neither warning, on purpose.

    Nothing is invented from it in either direction. Since 2026-09-07 the cost
    of being wrong here is a line on a confirmation card rather than a vacancy
    taken out of the run, but the rule is the same one: this reads two measured
    families and refuses to guess outside them, because a card full of text hh
    did not write teaches its reader to stop reading the card.
    """
    warnings = read_form_warnings(
        "Отклик на вакансию\nPython-разработчик\n"
        "Работодатель обычно отвечает в течение недели\n"
        "Откликнуться"
    )

    assert (warnings.visibility, warnings.likely_rejection) == (None, None)
    assert warnings.said == ()


def test_nothing_in_the_prefilter_decides_anything_from_the_modal_s_text() -> None:
    """``decide_on_form`` is gone, and this is what keeps it gone.

    It existed to turn one sentence into a ``MANUAL`` verdict. That sentence is
    advice, so the stage had nothing left to decide, and a decision function
    that can only answer ``PROCEED`` is a guard shape with nothing behind it —
    the next reader would believe it. What may stop an application is a
    checkable fact, and every one of them is knowable before the modal opens, so
    :func:`decide` is where they all live.
    """
    import agent.prefilter as prefilter_module

    assert not hasattr(prefilter_module, "decide_on_form")
    assert not any(
        "FormWarnings" in str(annotation)
        for annotation in getattr(prefilter_module.decide, "__annotations__", {}).values()
    )


def test_everything_quoted_out_of_hh_survives_the_console_it_is_printed_on() -> None:
    """A Russian Windows console encodes cp1251, and this text is not ours.

    An employer can put anything in a warning or in a test question. One
    character outside the codepage used to end the run at print time, in the
    middle of a batch, with the browser already open.
    """
    for line in read_form_warnings(MEASURED_MODAL).said:
        line.encode("cp1251")

    assert printable("Опыт ┌─ Python") == "Опыт ?? Python"
    printable("вопрос 🙂").encode("cp1251")


# ── the rest of the prefilter ─────────────────────────────────────────


def test_an_employer_test_shows_its_questions_and_answers_none_of_them() -> None:
    """The questions go on the card; nothing generates an answer, not even a blank."""
    with_questions: dict[str, Any] = {
        "applicantVacancyResponseStatuses": {
            "1": {
                "test": {
                    "hasTests": True,
                    "questions": [
                        {"title": "Сколько лет вы писали на Python?"},
                        "Опишите ваш опыт с асинхронностью.",
                    ],
                },
                "letterMaxLength": 10000,
                "shortVacancy": {"@responseLetterRequired": False},
                "negotiations": {"topicList": [], "total": 0},
            }
        }
    }

    facts = read(with_questions, "1")
    assert facts is not None
    assert facts.test_questions == (
        "Сколько лет вы писали на Python?",
        "Опишите ваш опыт с асинхронностью.",
    )

    decision = decide(
        facts=facts,
        closed_for_applicants=False,
        archived=False,
        already_applied=False,
        has_letter=True,
    )
    assert decision.verdict is Verdict.MANUAL
    assert "Сколько лет вы писали на Python?" in decision.reason


def test_a_test_whose_questions_are_not_on_the_page_says_so() -> None:
    """«вопросов нет» over a test with five questions is how a card stops being read."""
    facts = read(
        {
            "applicantVacancyResponseStatuses": {
                "1": {
                    "test": {"hasTests": True},
                    "letterMaxLength": 10000,
                    "shortVacancy": {"@responseLetterRequired": False},
                    "negotiations": {"topicList": [], "total": 0},
                }
            }
        },
        "1",
    )

    assert facts is not None
    assert facts.test_questions == ()
    decision = decide(
        facts=facts,
        closed_for_applicants=False,
        archived=False,
        already_applied=False,
        has_letter=True,
    )
    assert decision.verdict is Verdict.MANUAL
    assert "не отдал" in decision.reason


def test_a_letter_cannot_be_typed_into_a_field_nobody_has_seen() -> None:
    """Sending without a letter was measured end to end; sending with one was not.

    ``add-cover-letter`` is only the button that reveals the textarea, and
    nobody clicked it during the 2026-09-06 measurement, so the field's selector
    is genuinely unknown. A vacancy that demands a letter therefore goes to the
    owner even when a letter is sitting right there — one vacancy out of the
    batch, rather than a guessed selector typing somebody's letter into whatever
    it happens to match.
    """
    needs_a_letter: dict[str, Any] = {
        "applicantVacancyResponseStatuses": {
            "1": {
                "test": {"hasTests": False},
                "letterMaxLength": 10000,
                "shortVacancy": {"@responseLetterRequired": True},
                "negotiations": {"topicList": [], "total": 0},
            }
        }
    }
    base: dict[str, Any] = {
        "facts": read(needs_a_letter, "1"),
        "closed_for_applicants": False,
        "archived": False,
        "already_applied": False,
        "has_letter": True,
    }

    blocked = decide(**base, letter_field_known=False)
    assert blocked.verdict is Verdict.MANUAL
    assert "поле" in blocked.reason
    # Once the field has been measured the same vacancy is ordinary again, and
    # the default keeps the signature working for callers that predate this.
    assert decide(**base, letter_field_known=True).verdict is Verdict.PROCEED
    assert decide(**base).verdict is Verdict.PROCEED


def test_a_vacancy_hh_marks_as_impossible_goes_to_a_person() -> None:
    """A guess that can only refuse is a different guess from one that can send."""
    facts = read(
        {
            "applicantVacancyResponseStatuses": {
                "1": {
                    "test": {"hasTests": False},
                    "letterMaxLength": 10000,
                    "shortVacancy": {"@responseLetterRequired": False},
                    "negotiations": {"topicList": [], "total": 0},
                    "responseImpossible": True,
                }
            }
        },
        "1",
    )

    assert facts is not None
    assert facts.response_impossible is True
    assert (
        decide(
            facts=facts,
            closed_for_applicants=False,
            archived=False,
            already_applied=False,
            has_letter=True,
        ).verdict
        is Verdict.MANUAL
    )


@pytest.mark.parametrize(
    ("view", "expected"),
    [
        pytest.param({"status": {"archived": True}}, (True, False), id="nested status"),
        pytest.param({"archived": True}, (True, False), id="flat archived"),
        pytest.param({"closedForApplicants": True}, (False, True), id="closed for applicants"),
        pytest.param({"closedForApplicants": False}, (False, False), id="open, as measured"),
        pytest.param({}, (False, False), id="nothing said"),
    ],
)
def test_whether_the_vacancy_is_still_open_is_read_off_the_page(
    view: dict[str, Any], expected: tuple[bool, bool]
) -> None:
    """The page is the later witness: a vacancy can close between a crawl and a run.

    An absent flag reads as "not archived" rather than as a stop, which is the
    opposite of the idempotency rule on purpose — being wrong here costs a page
    load and a refusal from hh, while treating every unreadable page as archived
    would skip the whole queue in silence.
    """
    status = read_status({"vacancyView": view})

    assert (status.archived, status.closed_for_applicants) == expected


# ── the modal is classified once the modal is there ────────────────
#
# All of this follows from one measured fact: the response modal's frame and the
# card inside it arrive separately, the card on
# ``GET /applicant/vacancy_response/popup?vacancyId=…`` (recorded in
# ``agent/probe/form_136131345.json``). A reader that classifies the frame reads
# an empty card as "hh raised no objection", and that reading ends in an
# application nobody agreed to send.

#: The vacancy these fakes are about, and the id contained inside it. The
#: digits matter once, in the substring test below.
#:
#: Deliberately not the number ``test_apply_flow.py`` uses. A mandate's signature
#: is an HMAC over the vacancy, the URL, the letter and the form digest, and
#: ``agent.mandate`` keeps live signatures in one process-wide set — so two test
#: modules minting the same four values would be leaving each other consent.
FAKE_VACANCY = "142598301"
FAKE_SUBSTRING_OF_IT = "4259830"
FAKE_HREF = f"/applicant/vacancy_response?vacancyId={FAKE_VACANCY}&employerId=99"

#: The measured happy path: a card with nothing to warn about. It ends with the
#: submit control's own label, which is what proves the card rendered at all.
RENDERED_CARD = (
    "Отклик на вакансию\nPython Backend Trainee\n"
    "Python-разработчик\nДобавить сопроводительное\nОткликнуться"
)

#: What hh's submit button says, measured on both dumped modals
#: (``agent/probe/_warn.json`` and ``agent/probe/send_136638256.json``).
SUBMIT_LABEL = "Откликнуться"

#: hh's demand in full, non-breaking spaces and all, as it was measured.
RESUME_VISIBILITY_LINE = (
    f"Чтобы откликнуться на{NBSP}эту вакансию, поменяйте видимость резюме "
    f"на{NBSP}«Видно компаниям-клиентам HeadHunter»"
)


def _the_whole_modal() -> set[str]:
    """Frame and card together, which is how hh's modal usually finishes."""
    return {selectors.RESPONSE_FORM.query, selectors.SUBMIT_BUTTON.query}


@dataclass
class _Element:
    """One query resolved against a page that may or may not carry it."""

    page: "_ModalPage"
    query: str

    @property
    def first(self) -> "_Element":
        """Playwright's ``.first``, recorded so its absence would be visible."""
        self.page.took_first.append(self.query)
        return self

    def get_attribute(self, name: str) -> str | None:
        """Only ``href`` is ever asked for."""
        return self.page.href if name == "href" else None

    def click(self) -> None:
        """Following the apply link, which is what brings the modal up."""
        self.page.clicked.append(self.query)
        self.page.present |= self.page.reveals

    def inner_text(self) -> str:
        """What this element renders, and "" for anything not on screen.

        Playwright answers the same way: ``inner_text`` on an element that is
        attached but not rendered is empty, which is the state this whole
        section is about.
        """
        if self.query == selectors.SUBMIT_BUTTON.query:
            return self.page.submit_label
        return self.page.card


@dataclass
class _ModalPage:
    """A vacancy page whose modal arrives in two stages, the way hh's does.

    Deliberately small: :func:`agent.submit._open_the_form` needs a locator and
    a ``wait_for_selector`` and nothing else. The whole apply flow has a much
    larger double of its own, in ``test_apply_flow.py``.
    """

    #: Which queries the page answers right now.
    present: set[str] = field(default_factory=set)
    #: What the apply click adds. Override with the frame alone to model the
    #: measured race.
    reveals: set[str] = field(default_factory=_the_whole_modal)
    #: The modal's rendered text, as ``inner_text`` would return it.
    card: str = RENDERED_CARD
    #: The submit control's own rendered text.
    submit_label: str = SUBMIT_LABEL
    href: str | None = FAKE_HREF
    clicked: list[str] = field(default_factory=list)
    waited: list[str] = field(default_factory=list)
    took_first: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        """A fresh vacancy: the apply control, and none of the modal yet."""
        self.present.add(selectors.APPLY_LINK.query)

    def locator(self, query: str) -> _Element:
        """Every control the submitter touches goes through this."""
        return _Element(self, query)

    def wait_for_selector(self, query: str, timeout: int = 0) -> None:
        """Present only once whatever reveals it has actually happened."""
        self.waited.append(query)
        if query not in self.present:
            raise TimeoutError(f"{query} не появился")


def a_fake_mandate(vacancy_id: str = FAKE_VACANCY) -> SendMandate:
    """A mandate for these fakes, spent the moment it is made.

    Nothing in this section can send anything: :func:`agent.submit._open_the_form`
    reads the vacancy id off the mandate and never arms a gate with it. Spending
    it here is housekeeping — ``mint`` records the signature in a set that lives
    as long as the process, and a test module that mints without spending leaves
    usable consent lying about for every module that runs after it.
    """
    mandate = mint(
        vacancy_id=vacancy_id,
        url=f"https://almaty.hh.kz/vacancy/{vacancy_id}",
        letter=None,
        form_digest="a card only this module has seen",
    )
    verify(mandate)
    return mandate


def test_the_frame_arriving_is_not_the_card_arriving() -> None:
    """The measured race, and the reason this section exists.

    hh fetches the card into an overlay that is already on the page. Waiting for
    the overlay and classifying it straight away reads an application hh has
    refused as one it has no objection to, and sends it. The submit control only
    exists once the card has rendered, so it is waited for too —
    ``agent/probe_apply.py`` waits for the same pair, on the same measurement.
    """
    page = _ModalPage(reveals={selectors.RESPONSE_FORM.query})

    with pytest.raises(TimeoutError):
        submit._open_the_form(page, a_fake_mandate())

    assert page.waited == [selectors.RESPONSE_FORM.query, selectors.SUBMIT_BUTTON.query]


@pytest.mark.parametrize(
    ("card", "label"),
    [
        pytest.param("", SUBMIT_LABEL, id="the overlay renders nothing"),
        pytest.param("", "", id="nothing renders at all"),
        pytest.param("Мы используем куки\nПринять", SUBMIT_LABEL, id="some other overlay"),
    ],
)
def test_a_card_that_cannot_be_read_is_never_read_as_permission(card: str, label: str) -> None:
    """ "No warning" and "no text" are the same value, and only one of them may send.

    The second half of the race: the submit control is attached, the card is not
    on screen, and ``inner_text`` gives "" — which classifies exactly like hh's
    quiet modal. The classifier cannot tell those apart, so the distinction is
    made here, from the page: the control's own label has to be inside the text
    being classified. The third case is the same test doing a second job — an
    overlay that is not this modal is not read as this modal.
    """
    page = _ModalPage(card=card, submit_label=label)

    with pytest.raises(submit.FormUnreadableError) as excinfo:
        submit._open_the_form(page, a_fake_mandate())

    assert FAKE_VACANCY in str(excinfo.value)
    assert "Ничего не отправлено" in str(excinfo.value)
    # It runs on a Russian Windows console, and this sentence is printed there.
    str(excinfo.value).encode("cp1251")


def test_the_modal_and_its_button_are_both_taken_by_first() -> None:
    """Playwright's strict mode raises when a query matches twice.

    Without ``.first`` a second element answering either query turns the read
    into an exception in the middle of the flow. With it, the choice is
    deliberate — and the reading is then checked against the control it is
    supposed to contain, which is the test above.
    """
    page = _ModalPage()

    submit._open_the_form(page, a_fake_mandate())

    assert selectors.RESPONSE_FORM.query in page.took_first
    assert selectors.SUBMIT_BUTTON.query in page.took_first


def test_a_card_that_did_render_is_read_and_believed() -> None:
    """The happy path still works, which is the other half of not sending blind."""
    page = _ModalPage()

    warnings = submit._open_the_form(page, a_fake_mandate())

    assert (warnings.visibility, warnings.likely_rejection) == (None, None)
    assert page.clicked == [selectors.any_apply_control()]


def test_the_visibility_notice_comes_back_out_of_the_open_form_and_raises_nothing() -> None:
    """Rewritten 2026-09-07 from ``test_the_refusal_is_still_a_refusal_…``.

    That test asserted this same card raised ``RefusedByHHError`` out of
    ``_open_the_form``. The class is gone with the rule: hh accepts these
    applications. What the card is worth is hh's sentence, so what is pinned now
    is that the sentence comes back to the caller intact — and that opening the
    form is a reading, with nothing raised out of it.
    """
    page = _ModalPage(
        card=(f"Отклик на вакансию\n{RESUME_VISIBILITY_LINE}\nPython-разработчик\nОткликнуться")
    )

    warnings = submit._open_the_form(page, a_fake_mandate())

    assert warnings.visibility is not None
    assert "видимость резюме" in warnings.visibility
    assert not hasattr(submit, "RefusedByHHError")


def test_the_apply_link_must_name_this_vacancy_and_no_other() -> None:
    """A substring is not an id.

    ``142598301`` contains ``4259830``, so the old check — ``vacancy_id not in
    href`` — accepted a link to the first as a link to the second. The ids are
    parsed with the gate's own reader, so this and the gate that will see the
    same URL a moment later cannot hold different opinions about what it names.
    """
    page = _ModalPage()

    with pytest.raises(submit.WrongVacancyError, match=FAKE_SUBSTRING_OF_IT):
        submit._open_the_form(page, a_fake_mandate(FAKE_SUBSTRING_OF_IT))

    assert page.clicked == []


@pytest.mark.parametrize(
    "href",
    [
        pytest.param(f"/vacancy/999999999/apply?vacancyId={FAKE_VACANCY}", id="two vacancies"),
        pytest.param("/applicant/vacancy_response", id="no vacancy at all"),
        pytest.param(None, id="no href at all"),
    ],
)
def test_an_apply_link_that_does_not_name_exactly_this_vacancy_is_refused(
    href: str | None,
) -> None:
    """Consent is for one job. A link naming two names one nobody agreed to.

    A link naming none is refused too: it cannot be checked, and an apply control
    that stopped being a link is a change to hh worth a person's attention.
    """
    page = _ModalPage(href=href)

    with pytest.raises(submit.WrongVacancyError):
        submit._open_the_form(page, a_fake_mandate())

    assert page.clicked == []


def test_what_hh_said_at_any_point_in_the_open_form_is_kept() -> None:
    """The card is read before the letter is typed, and the send is after it.

    hh can answer the letter — a length it will not take, a policy on the text —
    and the first reading cannot have seen that. Both readings are kept, so a
    sentence from either reaches the journal and the person reading it next.
    """
    before = FormWarnings(visibility=None, likely_rejection="Такой отклик может получить отказ")
    after = FormWarnings(visibility="Поменяйте видимость резюме", likely_rejection=None)

    both = submit._everything_hh_said(before, after)

    assert both.visibility == "Поменяйте видимость резюме"
    assert both.likely_rejection == "Такой отклик может получить отказ"
    assert both.said == (both.visibility, both.likely_rejection)


# ── the reading survives hh rewording its own sentence ───────

#: Zero-width space: invisible, not whitespace to :meth:`str.split`, and a thing
#: web typography really does insert. Spelled with :func:`chr` for the same
#: reason as :data:`NBSP` — a fixture nobody can see is worthless.
ZWSP = chr(0x200B)


@pytest.mark.parametrize(
    "line",
    [
        pytest.param(RESUME_VISIBILITY_LINE, id="the measured sentence"),
        pytest.param("Поменяйте видимость вашего резюме", id="one word in between"),
        pytest.param("Измените настройки видимости резюме", id="another case ending"),
        pytest.param("Резюме скрыто — поменяйте видимость", id="the other order"),
        pytest.param(f"Поменяйте видимость рез{ZWSP}юме", id="a zero-width space inside a word"),
    ],
)
def test_the_notice_survives_hh_rewording_its_own_sentence(line: str) -> None:
    """The docstring promised "any mention of resume visibility". It is true.

    It used to be an exact two-word bigram inside a single line, so «видимость
    вашего резюме» — one word wider — matched nothing, and neither did any other
    case ending. What being wrong costs has changed since (2026-09-07: this is
    advice, and it stops nothing), but the direction has not — missing it means
    the owner sends a batch without being told hh thinks the whole batch is
    limited, and matching one line too many costs a quoted line on a card.
    """
    warnings = read_form_warnings(f"Отклик на вакансию\n{line}\nОткликнуться")

    assert warnings.visibility is not None
    assert warnings.said == (warnings.visibility,)


def test_a_notice_hh_has_split_over_two_lines_is_still_read() -> None:
    """No single line carries it, so every line that mentions it is quoted."""
    warnings = read_form_warnings(
        "Отклик на вакансию\nРезюме скрыто.\nПоменяйте видимость в настройках.\nОткликнуться"
    )

    assert warnings.visibility is not None
    assert "Резюме скрыто." in warnings.visibility
    assert "Поменяйте видимость" in warnings.visibility


@pytest.mark.parametrize(
    "modal",
    [
        pytest.param(RENDERED_CARD, id="the quiet card"),
        pytest.param(
            "Отклик на вакансию\nТакой отклик может получить отказ\n"
            "Английский язык в резюме «Python-разработчик» ниже уровня.\nОткликнуться",
            id="the likely-rejection warning, which itself names a resume",
        ),
    ],
)
def test_the_wider_net_does_not_read_a_visibility_notice_into_these_cards(modal: str) -> None:
    """Casting wider costs false positives, so the quiet cards are checked.

    The second one matters: hh's own «может получить отказ» reason names
    «резюме», so half of the notice's words sit on a card that says nothing
    about visibility, and only the other half keeps it out of that heading. A
    card headed «видимость резюме — это касается ВСЕХ откликов» over a sentence
    about an English level is a card its reader learns to skip.
    """
    warnings = read_form_warnings(modal)

    assert warnings.visibility is None


def test_one_rule_decides_what_counts_as_the_visibility_notice() -> None:
    """Two callers, one rule, and no second opinion about hh's sentence.

    :func:`read_form_warnings` applies it to a line of the open card;
    ``agent/run.py`` applies it to a sentence that came back out of the journal,
    where hh's two kinds of sentence share one reason column and have to be told
    apart again before a confirmation card can label them. A second matcher would
    drift from this one and the failure would be silent: hh's statement about the
    resume filed under "about this vacancy" reads as a small remark about one
    job, which is exactly the thing it is not.
    """
    assert mentions_visibility(RESUME_VISIBILITY_LINE)
    assert mentions_visibility("Поменяйте видимость вашего резюме")
    assert not mentions_visibility("Такой отклик может получить отказ")

    warnings = read_form_warnings(MEASURED_MODAL)

    assert warnings.visibility is not None and mentions_visibility(warnings.visibility)
    assert warnings.likely_rejection is not None
    assert not mentions_visibility(warnings.likely_rejection)


# ── when the count and the list disagree ─────────────────────

#: A shape nobody has measured: hh's count says nothing was sent and hh's own
#: list of conversations for this vacancy says something was. Built from the
#: entry measured on vacancy 133542745 with the count put back to zero.
COUNT_DISAGREES_WITH_LIST: dict[str, Any] = {
    "applicantVacancyResponseStatuses": {
        "133542745": {
            "test": {"hasTests": False},
            "letterMaxLength": 10000,
            "shortVacancy": {"@responseLetterRequired": False},
            "negotiations": {
                "topicList": [
                    {
                        "id": 5347572809,
                        "chatId": 5389562648,
                        "initialState": "RESPONSE",
                        "lastState": None,
                        "vacancyId": 133542745,
                    }
                ],
                "total": 0,
            },
            "alreadyApplied": False,
        }
    },
}


def test_a_conversation_hh_remembers_is_an_application_even_at_zero() -> None:
    """The one disagreement in this reader that used to resolve toward sending.

    Every other unfamiliar shape here stops. This one — ``total == 0`` beside a
    non-empty ``topicList`` — read as "no application exists", and the agent
    applied again, which is the mistake the owner cannot undo.
    """
    negotiations = read_negotiations(COUNT_DISAGREES_WITH_LIST, "133542745")

    assert negotiations is not None
    assert negotiations.total == 0
    assert len(negotiations.applications) == 1
    assert negotiations.exists is True
    assert read_applied(COUNT_DISAGREES_WITH_LIST, "133542745") is True
    assert (
        decide(
            facts=read(COUNT_DISAGREES_WITH_LIST, "133542745"),
            closed_for_applicants=False,
            archived=False,
            already_applied=read_applied(COUNT_DISAGREES_WITH_LIST, "133542745"),
            has_letter=False,
        ).verdict
        is Verdict.SKIP
    )


def test_the_list_can_only_add_an_application_never_remove_one() -> None:
    """The direction is what makes the wider reading safe rather than just stricter.

    A count of 1 with an empty list still says an application exists: hh trims
    that list, and a trimmed list is not an application that stopped existing.
    """
    an_application = Application(
        topic_id=1, chat_id=2, initial_state="RESPONSE", last_state="DISCARD"
    )

    assert Negotiations(total=1, applications=()).exists is True
    assert Negotiations(total=0, applications=()).exists is False
    assert Negotiations(total=0, applications=(an_application,)).exists is True
