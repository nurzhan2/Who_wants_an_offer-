"""The queue contract as the agent reads it, and what it puts on the card.

The card is the last thing between a model's text and an employer's inbox, and
what belongs on it is decided by one rule, written down in
:meth:`agent.human.Candidate.render`: everything that affects what gets sent
appears there, because the mandate binds the rendered text and a field that is
not rendered cannot invalidate a confirmation.

The match score was the field that decided *which* vacancies a person is looking
at and was the one thing the card did not show. A number alone would not have
been much better — the reason to send is which requirements the profile covers
and which it does not, which is checkable against the posting in front of the
reader, and disagreeing with it is exactly what the drop list is for.

The rest of the file is the client end of the same contract: how a score
survives the wire, and the local token ``/api/v1/applications`` sits behind.

These tests live in the agent's suite rather than beside the CLI's because all
of this is the agent's code. The CLI never imports the agent — see
``wwao/tests/test_separation.py`` — so a test of the card written over there
would have been the one crossing that package forbids.
"""

from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx

from agent.human import Candidate
from agent.letter import check as check_letter
from agent.mandate import digest
from agent.queue import HttpQueue, QueueItem, QueueUnreachableError

pytestmark = pytest.mark.unit

VACANCY = "136773120"
PAGE_URL = f"https://almaty.hh.kz/vacancy/{VACANCY}"


def a_candidate(**overrides: object) -> Candidate:
    """A candidate with everything the card is supposed to show."""
    fields: dict[str, object] = {
        "vacancy_id": VACANCY,
        "title": "Python-разработчик",
        "company": "Inspire",
        "url": PAGE_URL,
        "letter": check_letter("Здравствуйте! Опыт — Python, FastAPI.", required=False),
        "score": 82.5,
        "score_explanation": "закрыто: Python, FastAPI, PostgreSQL\nне закрыто: Kubernetes",
    }
    fields.update(overrides)
    return Candidate(**fields)  # type: ignore[arg-type]


def test_the_card_shows_the_score_and_the_reasoning_under_it() -> None:
    """Both, because the number on its own is not a reason to write to anybody."""
    card = a_candidate().render()

    assert "соответствие: 82.5 из 100" in card
    assert "не закрыто: Kubernetes" in card


def test_a_card_built_without_a_score_says_so_rather_than_staying_quiet() -> None:
    """«Nobody scored this» and «this scored badly» must not look the same.

    Silence reads as both, and the difference decides whether the list in front
    of the person is ordered by anything at all. The scoring step does not exist
    yet in this repository, so this is the card the owner would see today.
    """
    card = a_candidate(score=None, score_explanation=None).render()

    assert "соответствие: не посчитано" in card


def test_a_score_with_no_explanation_still_prints_the_number() -> None:
    """A backend that scores before it can explain is a state, not an error."""
    card = a_candidate(score_explanation=None).render()

    assert "соответствие: 82.5 из 100" in card


def test_changing_the_explanation_invalidates_a_confirmation() -> None:
    """The mandate binds the text that was read, and the score is now in it.

    Without this, a card approved while reading «не закрыто: Kubernetes» could
    be reused for a payload whose explanation said something else — and the
    explanation is the half of the score a person actually weighs.
    """
    before = digest(a_candidate().render())
    after = digest(a_candidate(score_explanation="закрыто: всё").render())

    assert before != after


def test_the_score_survives_the_console_the_card_is_printed_on() -> None:
    """Everything else on the card is reduced to cp1251; this must not be the gap."""
    card = a_candidate(
        company="Қазақстан Темір Жолы",
        score_explanation="закрыто: Python 🙂",
    ).render()

    card.encode("cp1251")
    assert "соответствие: 82.5 из 100" in card


# ── the queue is where the score comes from ───────────────────────────


def test_the_queue_carries_the_score_and_its_explanation() -> None:
    """The agent computes neither; it shows what the backend put in the payload."""
    item = QueueItem.from_json(
        {
            "vacancy_id": VACANCY,
            "url": PAGE_URL,
            "title": "Python-разработчик",
            "score": 82.5,
            "score_explanation": "закрыто: Python",
        }
    )

    assert (item.score, item.score_explanation) == (82.5, "закрыто: Python")


def test_a_score_serialised_as_a_string_is_still_a_score() -> None:
    """The backend stores ``Numeric(5, 2)``; JSON encoders disagree about it.

    Refusing ``"82.50"`` would make the most useful line on the confirmation
    card depend on which encoder the endpoint happens to use.
    """
    item = QueueItem.from_json(
        {"vacancy_id": VACANCY, "url": PAGE_URL, "score": str(Decimal("82.50"))}
    )

    assert item.score == 82.5


@pytest.mark.parametrize("value", [None, "высокий", 120, -1, float("nan"), True, {"score": 1}])
def test_a_score_that_is_not_a_score_becomes_no_score_rather_than_a_guess(
    value: object,
) -> None:
    """A coerced 0 reads as a terrible match and a coerced 100 as the opposite.

    Both are inventions shown to somebody deciding whether to write to an
    employer, so anything unreadable becomes «not computed» — which the card
    says out loud.
    """
    item = QueueItem.from_json({"vacancy_id": VACANCY, "url": PAGE_URL, "score": value})

    assert item.score is None


# ── the client end of the contract ────────────────────────────────────

BASE = "http://localhost:8000"
QUEUE_URL = f"{BASE}/api/v1/applications/queue"
RESULTS_URL = f"{BASE}/api/v1/applications/results"


@respx.mock
def test_the_client_presents_the_local_token_the_endpoint_asks_for() -> None:
    """``/api/v1/applications`` is behind a shared token and answers 401 without it.

    Both processes belong to the same person on one machine, so this
    authenticates nothing between them; what it buys is that nothing else on the
    host reaches the queue — and the owner's cover letters — by guessing a URL.
    """
    route = respx.get(QUEUE_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "version": 1,
                "items": [{"vacancy_id": VACANCY, "url": PAGE_URL, "title": "x"}],
            },
        )
    )

    items = HttpQueue(BASE, token="local-token-for-tests-only").take(5)

    assert [item.vacancy_id for item in items] == [VACANCY]
    assert route.calls[0].request.headers["authorization"] == "Bearer local-token-for-tests-only"


@respx.mock
def test_results_go_back_with_the_same_token() -> None:
    """The other half of the contract, and the half that writes."""
    route = respx.post(RESULTS_URL).mock(return_value=httpx.Response(200, json={"version": 1}))

    HttpQueue(BASE, token="local-token-for-tests-only").report([])

    assert route.calls[0].request.headers["authorization"] == "Bearer local-token-for-tests-only"


@respx.mock
def test_without_a_token_no_header_is_invented() -> None:
    """A 401 that says "no credentials" beats one that says "these are wrong".

    An empty bearer would make an unconfigured agent and a misconfigured one
    look identical in the backend's logs, and they are different five-minute
    problems.
    """
    route = respx.get(QUEUE_URL).mock(
        return_value=httpx.Response(200, json={"version": 1, "items": []})
    )

    HttpQueue(BASE, token="").take(5)

    assert "authorization" not in route.calls[0].request.headers


@respx.mock
@pytest.mark.parametrize(
    ("status", "says"),
    [
        (401, "AGENT_API_TOKEN"),
        (503, "AGENT_API_TOKEN"),
        (404, "этого эндпоинта"),
        (500, "500"),
    ],
)
def test_a_backend_that_refuses_says_which_five_minute_problem_it_is(
    monkeypatch: pytest.MonkeyPatch, status: int, says: str
) -> None:
    """The queue is the backend's, so "it did not answer" has to be actionable.

    401 and 503 are configuration on two different sides, 404 is a version
    mismatch and everything else is a service that fell over. One exit code for
    all four sends whoever reads the morning log to the wrong place three times
    out of four.
    """
    monkeypatch.setenv("AGENT_API_TOKEN", "s3cret-token")
    respx.get(QUEUE_URL).mock(return_value=httpx.Response(status))

    with pytest.raises(QueueUnreachableError) as failure:
        HttpQueue(BASE).take(5)

    message = str(failure.value)
    assert says in message
    assert "--no-backend" in message, "and how to work without it"
    assert "s3cret-token" not in message, "the token is never printed"


@respx.mock
def test_a_backend_that_is_not_running_is_not_a_stack_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ordinary case: nothing is listening on the port.

    ``httpx.ConnectError`` reaching a person is a stack trace where a sentence
    belongs — and the sentence has to name the address, because the default is
    localhost and the backend may well be somewhere else.
    """
    monkeypatch.delenv("AGENT_API_TOKEN", raising=False)
    respx.get(QUEUE_URL).mock(side_effect=httpx.ConnectError("Connection refused"))

    with pytest.raises(QueueUnreachableError, match=BASE):
        HttpQueue(BASE).take(5)


def test_the_token_is_read_from_the_environment_and_never_from_the_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLAUDE.md rule 4: secrets come from the environment, with no default.

    Also why it is not a command-line flag: an argument is visible in the
    process list to everything else on the machine, which is precisely what the
    token exists to keep away from the queue.
    """
    monkeypatch.setenv("AGENT_API_TOKEN", "from-the-environment")

    assert HttpQueue(BASE).token == "from-the-environment"

    monkeypatch.delenv("AGENT_API_TOKEN")

    assert HttpQueue(BASE).token == ""


def test_the_agent_reads_the_token_from_dotenv_when_the_environment_has_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The same file the backend reads; the environment still wins."""
    from agent import queue as queue_module

    dotenv = tmp_path / ".env"
    dotenv.write_text("OTHER=1\nAGENT_API_TOKEN='from-file'\n", encoding="utf-8")
    monkeypatch.setattr(queue_module, "DOTENV", dotenv)
    monkeypatch.delenv("AGENT_API_TOKEN", raising=False)
    assert queue_module.HttpQueue("http://x").token == "from-file"

    monkeypatch.setenv("AGENT_API_TOKEN", "from-environment")
    assert queue_module.HttpQueue("http://x").token == "from-environment"

    monkeypatch.delenv("AGENT_API_TOKEN")
    monkeypatch.setattr(queue_module, "DOTENV", tmp_path / "missing.env")
    assert queue_module.local_token() == ("", "")
