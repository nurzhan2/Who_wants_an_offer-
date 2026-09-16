"""What the agent works from: the backend's queue, plus a file for hand-additions.

The brief says the agent's only contact with the backend is its HTTP API: it
takes a queue and returns results, with no database access. That is the right
shape and it is written below as :class:`HttpQueue`.

It was written before the endpoint existed. For a while ``api/v1`` mounted only
resume, profile, sources and pipeline, so the honest thing was to define the
contract, implement the client against it, and ship something the owner could
actually run — which is :class:`FileQueue`, the same JSON read from
``agent/queue.json``.

``/api/v1/applications`` has since landed against this shape, behind a shared
local token, and **it is the queue**: what the pipeline produced overnight —
scored, above the threshold, with a letter written and no application sent —
exists only in the database, and for a while nothing here asked for it. A run
read a file a person had edited a month earlier and reported that as the queue.

Both transports are therefore kept and :class:`MergedQueue` puts them in their
places: the backend answers what is worth applying to, and the file is where a
person adds something the pipeline did not offer. The file still needs no
server, no database and no token, which is what makes it the fallback on the
first day of a fresh checkout — ``--no-backend`` in ``agent/run.py``.

The tests run against the shape rather than against either source, so nothing
below the transport had to change when the endpoint arrived.

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
class ATSCard:
    """What the backend's ATS audit says about this item's letter.

    A summary of ``app.schemas.ats.ATSReport``, reduced at the backend to what a
    console card can print. Nothing here is computed in this package: the audit
    reads a document the way an employer's parser will, and this shows the
    answer to the person about to send it.

    The two counts that matter are kept apart on purpose. ``unstated`` names
    requirements the candidate *has* and this letter does not mention — a thing
    a regenerated letter fixes. ``absent`` is only a number, because those are
    requirements nobody holds, and naming them on a card seconds before an
    application would read as a list of things to claim.
    """

    overall: str
    score: float | None = None
    critical: tuple[str, ...] = ()
    requirements_total: int = 0
    requirements_present: int = 0
    unstated: tuple[str, ...] = ()
    absent: int = 0

    @classmethod
    def from_json(cls, payload: object) -> "ATSCard | None":
        """One summary off the wire, or nothing when the backend sent none.

        ``None`` for anything unreadable rather than a default-constructed card:
        an item whose audit did not run must not print as one that passed.
        """
        if not isinstance(payload, dict):
            return None
        overall = payload.get("overall")
        if not isinstance(overall, str) or not overall.strip():
            return None
        return cls(
            overall=overall.strip(),
            score=_score(payload.get("score")),
            critical=_texts(payload.get("critical")),
            requirements_total=_count(payload.get("requirements_total")),
            requirements_present=_count(payload.get("requirements_present")),
            unstated=_texts(payload.get("unstated")),
            absent=_count(payload.get("absent")),
        )


def _texts(value: object) -> tuple[str, ...]:
    """A list of strings off the wire, with everything else dropped."""
    if not isinstance(value, list):
        return ()
    return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())


def _count(value: object) -> int:
    """A non-negative count off the wire, or zero."""
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, value)


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
    #: The backend's ATS audit of :attr:`letter`, against this vacancy's
    #: requirement list. ``None`` when the queue carried none — an older
    #: backend, or an item with no letter to audit — and the card says so
    #: rather than printing silence, for the same reason it does with the score.
    ats: "ATSCard | None" = None

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
            ats=ATSCard.from_json(payload.get("ats")),
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
class QueueUnreachableError(Exception):
    """The backend did not answer, and the message says what to do about it.

    Separate from :class:`QueueFormatError` because the answers differ: a
    malformed queue is somebody's mistake to fix, a backend that is not running
    is a service to start — or a reason to fall back to the file, which needs no
    server at all. Raised instead of letting ``httpx`` out so that a run ends
    with a sentence a person can act on rather than a stack trace.
    """


@final
@dataclass(frozen=True, slots=True)
class Result:
    """What happened to one item, on its way back to whoever asked."""

    vacancy_id: str
    status: str
    reason: str | None = None
    #: hh's own words about this application, quoted from the response form.
    #: One or two sentences, one per line: «Такой отклик может получить отказ»
    #: followed by the requirement it names, and — since 2026-09-07 — «поменяйте
    #: видимость резюме…», which is about the resume rather than this vacancy and
    #: is therefore true of every application sent while that setting stands. It
    #: used to be treated as a refusal and never reached a ``sent`` result at
    #: all; hh accepts those applications, measured, and
    #: ``agent/state_page.py`` carries the record.
    #:
    #: A field of its own rather than a sentence inside :attr:`reason`, because
    #: the two are different things to whoever stores this. ``reason`` is why
    #: this agent ended where it did and is written for a person to read;
    #: this is hh's analysis of the application itself — it names one unmet
    #: requirement, which is more precise than any similarity score this project
    #: computes, and it belongs beside the match score rather than in a log
    #: line. It is set on a ``sent`` result: nothing hh writes in that form stops
    #: an application.
    hh_warning: str | None = None
    #: The letter that was typed into the form, character for character.
    #:
    #: Asked for on the wire rather than copied from ``application.cover_letter``
    #: at the other end, because those two can differ silently: the letter column
    #: is overwritten in place by a regeneration, so a run between the queue
    #: being taken and this result arriving replaces the evidence with text no
    #: employer saw. Only the process that did the typing knows what went out.
    #:
    #: ``agent/run.py`` fills it on every ``sent`` result since 2026-09-16 with
    #: ``mandate.letter``, unmodified — the text the human confirmed and the text
    #: ``agent/submit.py`` types. It must never be reconstructed from anywhere
    #: else.
    #:
    #: ``None`` means "not reported"; ``""`` would mean "reported that nothing
    #: was typed", which hh permits on some vacancies. The two are different
    #: facts and the backend stores them differently.
    sent_letter: str | None = None
    #: ``negotiations.total`` as hh reported it, from
    #: ``applicantVacancyResponseStatuses``. ``None`` is "not measured" and ``0``
    #: is hh saying there are none — the distinction is the whole value of the
    #: field, so it must never be defaulted to a number.
    negotiations_total: int | None = None
    #: ``topicList[].lastState``, quoted. hh's vocabulary and hh's to extend:
    #: ``RESPONSE`` and ``DISCARD`` are all anyone here has seen, so this is a
    #: string and deliberately not an enum. See ``agent/outcomes.py``, which is
    #: what fills it, for why an unfamiliar value is carried rather than mapped.
    last_state: str | None = None

    def to_json(self) -> dict[str, Any]:
        """The wire form. ``Any`` for the same boundary reason as above.

        Every key here has to exist on ``app.schemas.agent.ApplicationResult``;
        ``backend/tests/test_agent_queue.py`` parses this literal and asserts
        exactly that, because a field the backend rejects is a 422 on a result
        describing an application that has already left.
        """
        return {
            "vacancy_id": self.vacancy_id,
            "status": self.status,
            "reason": self.reason,
            "sent_letter": self.sent_letter,
            "hh_warning": self.hh_warning,
            "negotiations_total": self.negotiations_total,
            "last_state": self.last_state,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "Result":
        """One result read back off a document this package wrote.

        Only :class:`ResultsFile` needs this, and only so that a later run can
        see what the previous one recorded. ``Any`` for the same boundary reason
        as :meth:`QueueItem.from_json`.

        Every field but the two required ones degrades to ``None`` rather than
        raising: this reads a *memory*, and a memory that refuses to load
        because one entry grew a shape it did not have is a memory that makes
        the next run start from nothing.
        """
        vacancy_id = payload.get("vacancy_id")
        status = payload.get("status")
        if not isinstance(vacancy_id, str) or not isinstance(status, str):
            raise QueueFormatError(f"В результате нет vacancy_id и status: {payload!r}")
        total = payload.get("negotiations_total")
        return cls(
            vacancy_id=vacancy_id,
            status=status,
            reason=_text(payload.get("reason")),
            sent_letter=_text(payload.get("sent_letter")),
            hh_warning=_text(payload.get("hh_warning")),
            # ``bool`` is an ``int`` in Python and ``true`` would read as 1.
            negotiations_total=(
                total if isinstance(total, int) and not isinstance(total, bool) else None
            ),
            last_state=_text(payload.get("last_state")),
        )


def _text(value: object) -> str | None:
    """A string off the wire, or nothing. Never a coerced ``repr``."""
    return value if isinstance(value, str) else None


def _document(results: Sequence[Result]) -> str:
    """The results document, in the one shape both transports carry."""
    return json.dumps(
        {"version": CONTRACT_VERSION, "results": [result.to_json() for result in results]},
        ensure_ascii=False,
        indent=1,
    )


@runtime_checkable
class Queue(Protocol):
    """Where applications come from and where their outcomes go."""

    def take(self, limit: int) -> Sequence[QueueItem]:
        """At most ``limit`` vacancies to consider this run."""
        ...

    def report(self, results: Sequence[Result]) -> None:
        """Hand back what happened."""
        ...


@runtime_checkable
class ResultSink(Protocol):
    """Somewhere results go. The half of :class:`Queue` that does not read.

    Split out because ``agent/outcomes.py`` takes no queue at all: it walks
    applications the journal already records as sent and has nothing to be
    handed. Asking it for a :class:`Queue` would mean handing it a ``take`` it
    must not call, and an unused method on an object that reaches the network
    is an invitation.
    """

    def report(self, results: Sequence[Result]) -> None:
        """Hand back what happened."""
        ...


@final
class ResultsFile:
    """A results document at a path of its own.

    The same shape :class:`FileQueue` writes beside its queue, addressable
    directly, because two different runs answer two different questions and
    must not overwrite each other's answer: ``queue-results.json`` is what one
    apply run did, and ``agent/outcomes.json`` is what hh has said since. A
    single file holding both would lose whichever ran last.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def report(self, results: Sequence[Result]) -> None:
        """Write the document, replacing whatever was there."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(_document(results), encoding="utf-8")

    def read(self) -> list[Result]:
        """What this document holds, or nothing at all when there is no file.

        A missing file is an ordinary state — no run has written one yet — and
        reads as an empty list. A file that is *not* this document raises, and
        that difference matters to the caller: "nothing to compare against" and
        "the memory is unreadable" produce different sentences for a person.
        """
        if not self.path.is_file():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except ValueError as error:
            raise QueueFormatError(f"{self.path.name} — не JSON: {error}") from error
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise QueueFormatError(f"{self.path.name} не содержит поля results")
        return [Result.from_json(entry) for entry in payload["results"] if isinstance(entry, dict)]


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
        """Write the outcomes next to the queue.

        Through :class:`ResultsFile` rather than beside it: the document's shape
        is the contract, and two writers of one shape is one writer too many.
        """
        ResultsFile(self.results_path).report(results)


#: The environment variable holding the shared local token the queue endpoint
#: sits behind. Read here rather than hardcoded anywhere, like every other
#: secret in this project; the name is configuration, the value never is.
#:
#: Both processes belong to the same person on the same machine, so there is no
#: second party to authenticate. What the token buys is that nothing *else* on
#: the host reaches the queue by guessing a URL, and a queue reachable that way
#: hands out the owner's cover letters.
TOKEN_VARIABLE: Final[str] = "AGENT_API_TOKEN"

#: The settings file the backend reads the same token from. Read here as well
#: since 2026-09-16: the token written into ``.env``, as ``.env.example`` says
#: to, used to reach the backend and not this process, and the run ended in a
#: 401 whose message did not mention the file. The environment wins when both
#: are set, as it does for the backend.
DOTENV: Final[Path] = Path(__file__).resolve().parents[1] / ".env"


def local_token() -> tuple[str, str]:
    """The local token and where it was found: ``окружение``, ``.env`` or ``""``.

    Parsed by hand rather than through the backend's settings, which this
    package must not import (``agent/tests/test_isolation.py``). ``wwao`` has
    the same few lines for the same reason.
    """
    from_environment = os.environ.get(TOKEN_VARIABLE, "").strip()
    if from_environment:
        return from_environment, "окружение"
    try:
        text = DOTENV.read_text(encoding="utf-8")
    except OSError:
        return "", ""
    for raw in text.splitlines():
        key, _, value = raw.strip().removeprefix("export ").partition("=")
        if key.strip() != TOKEN_VARIABLE:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if value:
            return value, ".env"
    return "", ""


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
        found, self.token_source = local_token() if token is None else (token, "аргумент")
        self.token = found

    def _headers(self) -> dict[str, str]:
        """The one header this client sends. Never logged, never printed."""
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def take(self, limit: int) -> Sequence[QueueItem]:
        """``GET {base}/api/v1/applications/queue?limit=…``."""
        import httpx

        try:
            response = httpx.get(
                f"{self.base_url}/api/v1/applications/queue",
                params={"limit": limit},
                headers=self._headers(),
                timeout=self.timeout,
            )
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise QueueUnreachableError(self._unreachable(error, "очередь")) from error
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise QueueFormatError("Ответ бэкенда не содержит items")
        return [QueueItem.from_json(entry) for entry in payload["items"]]

    def report(self, results: Sequence[Result]) -> None:
        """``POST {base}/api/v1/applications/results``."""
        import httpx

        try:
            response = httpx.post(
                f"{self.base_url}/api/v1/applications/results",
                json={"version": CONTRACT_VERSION, "results": [r.to_json() for r in results]},
                headers=self._headers(),
                timeout=self.timeout,
            )
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise QueueUnreachableError(self._unreachable(error, "результаты")) from error

    def _unreachable(self, error: Exception, what: str) -> str:
        """One sentence naming the address, the failure and the way round it.

        The status code is spelled out where there is one, because 401 and 503
        are configuration and everything else is not: the endpoint sits behind
        a shared local token, and «no token here» and «no token there» are
        different five-minute problems. The token itself is never printed.
        """
        import httpx

        detail = str(error) or error.__class__.__name__
        if isinstance(error, httpx.HTTPStatusError):
            code = error.response.status_code
            detail = f"{code}"
            if code in (401, 403):
                detail += f" — очередь закрыта локальным токеном ({TOKEN_VARIABLE}); " + (
                    f"токен взят из: {self.token_source}, и бэкенд его не принял"
                    if self.token
                    else "он не задан ни в окружении, ни в файле .env в корне проекта"
                )
            elif code == 503:
                detail += f" — у бэкенда не задан {TOKEN_VARIABLE}"
            elif code == 404:
                detail += " — этого эндпоинта в бэкенде нет"
        return (
            f"{self.base_url} не отдал {what}: {detail}. "
            "Запустите бэкенд или возьмите одну ручную очередь: --no-backend."
        )


@final
class MergedQueue:
    """The backend's queue, plus whatever a person put in the file by hand.

    The queue is a question about the database — vacancies scored above
    ``agent_queue_min_score`` that have a letter and no application yet — and
    the backend answers it. ``agent/queue.json`` used to be the only source,
    which meant a night of crawling, scoring and letter writing produced nothing
    the agent could see: it read a file somebody had edited a month earlier.

    So the file stays, as an addition rather than as the source. What a person
    typed into it is offered after what the pipeline produced, and only when the
    backend did not already offer the same vacancy — an entry added by hand
    carries no score, and showing it in place of the scored row would hide the
    only reason to prefer one vacancy over another.

    **Results go everywhere they can.** To the local file first, because that
    record must survive a tracker that is down — a run that sent applications
    and then failed to say so is the one outcome worth engineering against — and
    to the backend after, including the results of hand-added items: an id it
    does not know writes nothing at the far end, and one it does know is an
    application the tracker should have.
    """

    def __init__(
        self,
        *,
        backend: Queue,
        extra: Queue | None = None,
        memory: ResultSink | None = None,
    ) -> None:
        self.backend = backend
        self.extra = extra
        self.memory = memory

    def take(self, limit: int) -> Sequence[QueueItem]:
        """The backend's items first, then the hand-written ones it did not name."""
        items = list(self.backend.take(limit))
        if self.extra is None or len(items) >= limit:
            return items
        seen = {item.vacancy_id for item in items}
        for item in self.extra.take(limit):
            if len(items) >= limit:
                break
            if item.vacancy_id in seen:
                continue
            seen.add(item.vacancy_id)
            items.append(item)
        return items

    def report(self, results: Sequence[Result]) -> None:
        """Write the local record, then hand the same results to the tracker.

        In that order and never the other way round: the file write cannot fail
        for a reason outside this machine, and if the POST does, what happened
        is already on disk. The caller decides what a failed hand-over means —
        here it is simply not allowed to lose the record.
        """
        if self.memory is not None:
            self.memory.report(results)
        self.backend.report(results)
