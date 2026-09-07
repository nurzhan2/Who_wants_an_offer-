"""What the agent works from, and why it is a file today.

The brief says the agent's only contact with the backend is its HTTP API: it
takes a queue and returns results, with no database access. That is the right
shape and it is written below as :class:`HttpQueue`.

It was written before the endpoint existed. For a while ``api/v1`` mounted only
resume, profile, sources and pipeline, so the honest thing was to define the
contract, implement the client against it, and ship something the owner could
actually run — which is :class:`FileQueue`, the same JSON read from
``agent/queue.json``. ``/api/v1/applications`` has since landed against this
shape, behind a shared local token; both transports are kept, because the file
needs no server, no database and no token, and that is what a person can run on
the first day of a fresh checkout.

Swapping one for the other is a single line in the CLI, and the tests run
against the shape rather than against either source, so nothing below the
transport had to change when the endpoint arrived.

The fields are chosen so the prefilter can run **before** a page is opened, which
is the whole point of a prefilter. Every one of them is something the crawler in
``backend/app/sources/hh.py`` already derives and stores in
``vacancy_source.raw["_derived"]`` — external_id, url, title, company,
closed_for_applicants — so the endpoint, when someone writes it, is a projection
of rows that exist rather than new work.

The two exceptions are :attr:`QueueItem.letter` and
:attr:`QueueItem.score`/:attr:`QueueItem.score_explanation`, which come from the
steps after the crawl. They are here rather than left out because both belong on
the confirmation card: the letter is what an employer will read in the owner's
name, and the score with its explanation is the only answer this project has to
"why is this vacancy in front of me". Neither is used to decide anything in this
package — the agent never scores and never writes a letter — so both are carried
untouched and shown.
"""

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol, final, runtime_checkable

from agent.state_page import vacancy_id_from_url

#: The contract's version, carried in the payload. A queue produced by an older
#: backend than this agent expects is a thing to notice rather than to guess at.
CONTRACT_VERSION: Final[int] = 1


@final
@dataclass(frozen=True, slots=True)
class QueueItem:
    """One vacancy the owner's dashboard put forward for an application."""

    #: hh's own id, as a string. The natural key everywhere in this package.
    vacancy_id: str
    url: str
    title: str
    company: str | None = None
    #: Written by the backend's letter generation. The agent never writes one
    #: and never edits one; it checks it and either pastes it or stops.
    letter: str | None = None
    #: What the crawler knew when it last saw the posting. Advisory only: the
    #: page is read again before anything is clicked, because a vacancy can
    #: close between a crawl and a run.
    closed_for_applicants: bool = False
    archived: bool = False
    #: The application is completed on the employer's own site. A human's job.
    external_application: bool = False
    #: How well this vacancy matches the profile, on the 0-100 scale the whole
    #: project uses, and the sentence behind that number.
    #:
    #: Both reach the confirmation card, and the explanation is the half that
    #: matters there. A person deciding whether to send is not helped by «82» —
    #: they are helped by which requirements this profile covers and which it
    #: does not, which is what makes the number checkable rather than trusted.
    #: Both are optional because the scoring step may not have run; a card built
    #: from an item without a score says so rather than staying silent, because
    #: "no score" and "a bad score" must not look the same to the person
    #: approving an application.
    score: float | None = None
    score_explanation: str | None = None

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "QueueItem":
        """One item from the wire, with the two required fields enforced.

        ``Any`` because this is the boundary where untyped JSON becomes typed
        data — which is the one place CLAUDE.md's rule expects it.
        """
        vacancy_id = payload.get("vacancy_id")
        url = payload.get("url")
        if not isinstance(vacancy_id, str) or not vacancy_id.isdigit():
            raise QueueFormatError(f"vacancy_id должен быть числовой строкой: {vacancy_id!r}")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise QueueFormatError(f"url должен быть https-ссылкой: {url!r}")
        # The two must agree. Everything downstream acts on the id — the journal
        # row, the gate's comparison, the page the submitter opens — while the
        # human reads the url on the confirmation card. Validating them
        # separately, as this did, let one item send an application to a vacancy
        # nobody had looked at.
        in_url = vacancy_id_from_url(url)
        if in_url is not None and in_url != vacancy_id:
            raise QueueFormatError(
                f"vacancy_id {vacancy_id!r} не совпадает с вакансией в ссылке: {url!r}"
            )
        title = payload.get("title")
        explanation = payload.get("score_explanation")
        return cls(
            vacancy_id=vacancy_id,
            url=url,
            title=title if isinstance(title, str) and title.strip() else "без названия",
            company=payload.get("company") if isinstance(payload.get("company"), str) else None,
            letter=payload.get("letter") if isinstance(payload.get("letter"), str) else None,
            closed_for_applicants=bool(payload.get("closed_for_applicants", False)),
            archived=bool(payload.get("archived", False)),
            external_application=bool(payload.get("external_application", False)),
            score=_score(payload.get("score")),
            score_explanation=explanation if isinstance(explanation, str) else None,
        )


def _score(value: object) -> float | None:
    """A match score off the wire, or nothing, and never a wrong number.

    The backend keeps scores as ``Numeric(5, 2)``, and how that arrives depends
    on the JSON encoder at the other end: a number from one, the string
    ``"82.50"`` from another. Both are accepted because both are the same score
    and refusing one of them would make the card's most useful line depend on a
    serialisation detail.

    Anything else — a null, a word, a number outside the scale the project
    defines — becomes ``None`` rather than a guess. The card then says the score
    was not computed, which is true and readable; a silently coerced 0 would
    read as "a terrible match" and a coerced 100 as the opposite, and both are
    inventions shown to somebody deciding whether to write to an employer.
    """
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        score = float(value)
    except ValueError:
        return None
    if not 0.0 <= score <= 100.0:
        return None
    return score


@final
class QueueFormatError(Exception):
    """The queue is not in the shape this agent understands."""


@final
@dataclass(frozen=True, slots=True)
class Result:
    """What happened to one item, on its way back to whoever asked."""

    vacancy_id: str
    status: str
    reason: str | None = None
    #: hh's own warning about this application, quoted from the response form:
    #: «Такой отклик может получить отказ» followed by the requirement it names.
    #:
    #: A field of its own rather than a sentence inside :attr:`reason`, because
    #: the two are different things to whoever stores this. ``reason`` is why
    #: this agent ended where it did and is written for a person to read;
    #: this is hh's analysis of the application itself — it names one unmet
    #: requirement, which is more precise than any similarity score this project
    #: computes, and it belongs beside the match score rather than in a log
    #: line. It can be set on a ``sent`` result: the soft warning never blocks.
    hh_warning: str | None = None

    def to_json(self) -> dict[str, Any]:
        """The wire form. ``Any`` for the same boundary reason as above."""
        return {
            "vacancy_id": self.vacancy_id,
            "status": self.status,
            "reason": self.reason,
            "hh_warning": self.hh_warning,
        }


@runtime_checkable
class Queue(Protocol):
    """Where applications come from and where their outcomes go."""

    def take(self, limit: int) -> Sequence[QueueItem]:
        """At most ``limit`` vacancies to consider this run."""
        ...

    def report(self, results: Sequence[Result]) -> None:
        """Hand back what happened."""
        ...


@final
class FileQueue:
    """A queue in a JSON file. What the owner can use today.

    The results are written beside the input rather than back into it, so a run
    never rewrites the thing it was reading and a half-finished run leaves the
    queue intact.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.results_path = path.with_name(f"{path.stem}-results.json")

    def take(self, limit: int) -> Sequence[QueueItem]:
        """Read the queue file, validating every entry."""
        if not self.path.is_file():
            raise QueueFormatError(
                f"Нет файла очереди {self.path}. Создайте его или укажите другой путь; "
                "формат — в agent/README.md."
            )
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise QueueFormatError("Ожидался объект с полем items")
        version = payload.get("version")
        if version != CONTRACT_VERSION:
            raise QueueFormatError(
                f"Версия очереди {version!r}, а агент понимает {CONTRACT_VERSION}"
            )
        return [QueueItem.from_json(entry) for entry in payload["items"][:limit]]

    def report(self, results: Sequence[Result]) -> None:
        """Write the outcomes next to the queue."""
        self.results_path.write_text(
            json.dumps(
                {"version": CONTRACT_VERSION, "results": [r.to_json() for r in results]},
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )


#: The environment variable holding the shared local token the queue endpoint
#: sits behind. Read here rather than hardcoded anywhere, like every other
#: secret in this project; the name is configuration, the value never is.
#:
#: Both processes belong to the same person on the same machine, so there is no
#: second party to authenticate. What the token buys is that nothing *else* on
#: the host reaches the queue by guessing a URL, and a queue reachable that way
#: hands out the owner's cover letters.
TOKEN_VARIABLE: Final[str] = "AGENT_API_TOKEN"


@final
class HttpQueue:
    """The queue as the brief describes it, over the endpoint that now exists.

    Written before the endpoint did, so the contract was a thing in the
    repository rather than a sentence in a brief; ``/api/v1/applications``
    landed against this shape and is guarded by a shared local token, which is
    why the header below is here. Still deliberately thin: what it parses is
    :class:`QueueItem`, identical to the file's, and the tests exercise that
    shape rather than this transport.
    """

    def __init__(self, base_url: str, *, token: str | None = None, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        #: Taken from the environment unless a caller passes one, so the value
        #: never reaches a command line or a log. An empty token sends no header
        #: at all: the endpoint then answers 401, which reads as "not configured"
        #: rather than as "rejected", and that is the more useful of the two.
        self.token = os.environ.get(TOKEN_VARIABLE, "") if token is None else token

    def _headers(self) -> dict[str, str]:
        """The one header this client sends. Never logged, never printed."""
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def take(self, limit: int) -> Sequence[QueueItem]:
        """``GET {base}/api/v1/applications/queue?limit=…``."""
        import httpx

        response = httpx.get(
            f"{self.base_url}/api/v1/applications/queue",
            params={"limit": limit},
            headers=self._headers(),
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise QueueFormatError("Ответ бэкенда не содержит items")
        return [QueueItem.from_json(entry) for entry in payload["items"]]

    def report(self, results: Sequence[Result]) -> None:
        """``POST {base}/api/v1/applications/results``."""
        import httpx

        response = httpx.post(
            f"{self.base_url}/api/v1/applications/results",
            json={"version": CONTRACT_VERSION, "results": [r.to_json() for r in results]},
            headers=self._headers(),
            timeout=self.timeout,
        )
        response.raise_for_status()
