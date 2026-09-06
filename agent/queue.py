"""What the agent works from, and why it is a file today.

The brief says the agent's only contact with the backend is its HTTP API: it
takes a queue and returns results, with no database access. That is the right
shape and it is written below as :class:`HttpQueue`.

It does not exist yet. ``backend/app/api/v1/router.py`` mounts resume, profile,
sources and pipeline; there is no vacancy list, no application endpoint and no
queue. `Application` exists as a table and a set of schemas with nothing serving
them. The brief also forbids adding it — «не трогать backend» — so the honest
thing is to define the contract, implement the client against it, and ship
something the owner can actually run today.

That is :class:`FileQueue`: the same JSON the endpoint will return, read from
``agent/queue.json``. Swapping one for the other is a single line in the CLI,
and the fixture tests run against the shape rather than against either source,
so the day the endpoint lands nothing here needs re-testing.

The fields are chosen so the prefilter can run **before** a page is opened, which
is the whole point of a prefilter. Every one of them is something the crawler in
``backend/app/sources/hh.py`` already derives and stores in
``vacancy_source.raw["_derived"]`` — external_id, url, title, company,
closed_for_applicants — so the endpoint, when someone writes it, is a projection
of rows that exist rather than new work.
"""

import json
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
        return cls(
            vacancy_id=vacancy_id,
            url=url,
            title=title if isinstance(title, str) and title.strip() else "без названия",
            company=payload.get("company") if isinstance(payload.get("company"), str) else None,
            letter=payload.get("letter") if isinstance(payload.get("letter"), str) else None,
            closed_for_applicants=bool(payload.get("closed_for_applicants", False)),
            archived=bool(payload.get("archived", False)),
            external_application=bool(payload.get("external_application", False)),
        )


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

    def to_json(self) -> dict[str, Any]:
        """The wire form. ``Any`` for the same boundary reason as above."""
        return {"vacancy_id": self.vacancy_id, "status": self.status, "reason": self.reason}


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


@final
class HttpQueue:
    """The queue as the brief describes it, for the endpoint that does not exist.

    Written now so the contract is a thing in the repository rather than a
    sentence in a brief, and so switching is one line. It is deliberately thin:
    the shape it parses is :class:`QueueItem`'s, identical to the file's, and
    the tests exercise that shape rather than this transport.
    """

    def __init__(self, base_url: str, *, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def take(self, limit: int) -> Sequence[QueueItem]:
        """``GET {base}/api/v1/applications/queue?limit=…``."""
        import httpx

        response = httpx.get(
            f"{self.base_url}/api/v1/applications/queue",
            params={"limit": limit},
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
            timeout=self.timeout,
        )
        response.raise_for_status()
