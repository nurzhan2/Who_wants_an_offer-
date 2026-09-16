"""What is ready to apply to, and why the rest is not.

The queue is the seam between the two halves of this project: the backend puts
vacancies forward, the agent applies to them, and nothing but JSON crosses. The
contract is version 1 and is written down twice on purpose — here, and in
``agent/queue.py`` on the client side — because the two ends must not share
code. This module never imports the agent for the same reason the agent never
imports the backend, and reading ``agent/queue.json`` as a *file* is not an
import: ``agent/hosts.py`` reads ``backend/app/sources/hh_sites.yaml`` the same
way and for the same reason.

**Two transports, one shape, and one of them is the queue.** ``--from`` takes
an HTTP base URL, where ``/api/v1/applications/queue`` answers behind a local
token, or a path to a JSON file that works with no server, no database and no
token at all. The parsing and everything below it is identical, so which one is
in use changes nothing about the report.

What changed is which one answers the question by default. The queue is a fact
about the database — vacancies scored above ``agent_queue_min_score``, with a
letter written and no application sent — and for a year this command read
``agent/queue.json`` instead: a file somebody edits by hand. A night of
crawling, scoring and letter writing therefore showed up here as one row typed
in weeks earlier, with no score, and nothing said that was what you were
looking at. Now the backend answers and the file is merged in behind it, marked
as hand-added; see :func:`merge`.

**Why a vacancy is not ready is the interesting half of the report.** A list of
what is ready answers "what will happen"; the rest answers "why is this thing I
expected not here", which is the question a person actually arrives with. Three
kinds of answer are available without opening a page or logging in:

* what the crawler knew — archived, closed for applicants, applications handled
  on the employer's own site;
* what the last run concluded — ``agent/queue-results.json``, written beside the
  queue by every run, including hh's own sentence about the application;
* what is simply missing — no letter written yet, no match score computed yet.

The first two are blocking and the third is a note: hh accepts an application
without a cover letter, so a missing letter is something to know rather than a
refusal. A missing *score* is also only a note, but it is printed loudly,
because a queue with no scores in it means the scoring step has not run and the
order of the list means nothing.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, final

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from wwao.console import printable

#: The queue contract this view understands. Same number as
#: ``agent.queue.CONTRACT_VERSION``; a payload carrying another one is reported
#: rather than guessed at.
CONTRACT_VERSION: Final[int] = 1

#: Where the endpoint will live. Written down here as well as in
#: ``agent/queue.py`` because the two sides of an HTTP boundary are allowed to
#: know the same URL and are not allowed to share the module that holds it.
QUEUE_PATH: Final[str] = "/api/v1/applications/queue"


class QueueUnavailableError(Exception):
    """The queue could not be read, and the message says what to do instead.

    This one means "something is wrong right now": the backend is not running,
    the token does not match, the file is not the contract. The answer is to fix
    the thing named and run it again.
    """


@final
class QueueNotBuiltError(QueueUnavailableError):
    """The queue is not there to be read, because that part is not built.

    Separate from its parent because the answers differ and the exit codes
    follow the answers: a 404 from an older backend and a payload from a
    contract version nobody here understands both mean "somebody has to write
    or update something", not "look at the logs and retry".
    """


class QueueEntry(BaseModel):
    """One vacancy the backend put forward, exactly as it comes over the wire.

    Unknown fields are ignored rather than rejected: the endpoint is expected to
    grow, and a queue view that refuses to render because the backend added a
    column is a worse failure than one that shows what it understands.
    """

    model_config = ConfigDict(extra="ignore")

    vacancy_id: str
    url: str
    title: str = "без названия"
    company: str | None = None
    letter: str | None = None
    #: The match score on the 0-100 scale the whole project uses, and the
    #: explanation behind it. Both optional, because the scoring step may not
    #: have run yet, and both shown — a bare number tells a person nothing about
    #: whether to send.
    score: float | None = None
    score_explanation: str | None = None
    #: What the crawler knew when it last saw the posting. Advisory: the agent
    #: reads the page again before it clicks anything.
    closed_for_applicants: bool = False
    archived: bool = False
    external_application: bool = False

    @field_validator("score", mode="before")
    @classmethod
    def _readable_score(cls, value: object) -> float | None:
        """A score off the wire, or nothing, and never a wrong number.

        The backend keeps scores as ``Numeric(5, 2)``, which arrives as a number
        from one JSON encoder and as the string ``"82.50"`` from another; both
        are the same score and both are accepted. Anything outside the project's
        0-100 scale becomes ``None`` — reported as "not computed" — rather than
        rejecting the whole payload: one nonsensical number in one row must not
        be able to stop the report that says what is ready to apply to.

        The same rule is implemented in ``agent.queue`` for the other end of
        this contract. Duplicated deliberately: the two sides of an HTTP
        boundary do not share code, which is what makes them two sides.
        """
        if isinstance(value, bool) or not isinstance(value, int | float | str):
            return None
        try:
            score = float(value)
        except ValueError:
            return None
        return score if 0.0 <= score <= 100.0 else None


class QueuePayload(BaseModel):
    """The whole answer, from either transport."""

    model_config = ConfigDict(extra="ignore")

    version: int
    items: list[QueueEntry] = Field(default_factory=list)


class RunResult(BaseModel):
    """What the last run said about one vacancy."""

    model_config = ConfigDict(extra="ignore")

    vacancy_id: str
    status: str
    reason: str | None = None
    #: hh's own words about this application, kept apart from ``reason`` because
    #: one is this project's conclusion and the other is the employer platform's.
    hh_warning: str | None = None


class ResultsPayload(BaseModel):
    """``agent/queue-results.json``, written beside the queue by every run."""

    model_config = ConfigDict(extra="ignore")

    version: int = CONTRACT_VERSION
    results: list[RunResult] = Field(default_factory=list)


#: A status in the results file that means this vacancy is done with, or is
#: waiting on a person. Anything here keeps the vacancy out of "ready", and the
#: row says which one it was and why.
FINISHED: Final[dict[str, str]] = {
    "sent": "отклик уже отправлен",
    "skipped": "прошлый прогон пропустил",
    "needs_manual": "нужен человек",
    "failed": "прошлый прогон не смог",
    "confirmed": "подтверждено, но прогон не дошёл до отправки",
}


@final
@dataclass(frozen=True, slots=True)
class Row:
    """One queue entry with the verdict this view puts on it."""

    entry: QueueEntry
    #: Why this vacancy will not be applied to. Empty means it is ready.
    blockers: tuple[str, ...] = ()
    #: True of a ready vacancy, worth reading, not in the way.
    notes: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        """Whether the agent would offer this one on a confirmation card."""
        return not self.blockers


@final
@dataclass(frozen=True, slots=True)
class QueueView:
    """The whole report, before it is turned into text."""

    source: str
    rows: tuple[Row, ...] = ()
    #: Set when the results file was read, so the report can say whether the
    #: "why not" half is based on anything at all.
    results_seen: int = 0
    warnings: tuple[str, ...] = field(default=())

    @property
    def ready(self) -> tuple[Row, ...]:
        """The rows the agent would put on a card."""
        return tuple(row for row in self.rows if row.ready)

    @property
    def blocked(self) -> tuple[Row, ...]:
        """The rest, each carrying its own reasons."""
        return tuple(row for row in self.rows if not row.ready)


def parse_queue(raw: str) -> QueuePayload:
    """Turn a payload from either transport into typed data.

    The one place untyped JSON becomes something the rest of this module can
    rely on, which is why it is also the only place that raises about shape.
    """
    try:
        payload = QueuePayload.model_validate_json(raw)
    except ValidationError as error:
        raise QueueUnavailableError(
            f"Очередь не в том формате, который понимает CLI: {error}"
        ) from error
    if payload.version != CONTRACT_VERSION:
        raise QueueNotBuiltError(
            f"Версия очереди {payload.version}, а CLI понимает {CONTRACT_VERSION}. "
            "Обновите одну из сторон — молча разбирать чужую версию нельзя."
        )
    return payload


def parse_results(path: Path) -> ResultsPayload:
    """The last run's outcomes, or an empty set of them.

    A missing file is not an error: it means no run has finished yet, which is
    an ordinary state and reads as "nothing known" in the report. A malformed
    one is not an error either — the queue is still worth showing — so it turns
    into a warning line rather than an exit code.
    """
    if not path.is_file():
        return ResultsPayload()
    try:
        return ResultsPayload.model_validate_json(path.read_text(encoding="utf-8"))
    except (ValidationError, OSError, json.JSONDecodeError):
        return ResultsPayload()


def merge(
    primary: Sequence[QueueEntry], extra: Sequence[QueueEntry]
) -> tuple[tuple[QueueEntry, ...], frozenset[str]]:
    """The database's queue, plus the rows a person added to the file by hand.

    The backend answers the question the queue *is*: which vacancies scored
    above the threshold, have a letter written and have never been applied to.
    ``agent/queue.json`` used to be the only source, so a night of crawling,
    scoring and letter writing showed up here as whatever somebody had typed
    into that file — once, weeks earlier — and nothing said so.

    Order is the point of the merge. What the pipeline produced leads, and the
    hand-written rows follow: an entry typed into the file carries no score, and
    putting it above a scored one hides the only reason there is to prefer one
    vacancy over another. A vacancy in both is the backend's row — same reason.

    Returns the entries and the ids that came from the file, because the report
    has to say which is which. A row with no score looks identical to a row the
    scoring step never reached, and those are different problems.
    """
    entries = list(primary)
    seen = {entry.vacancy_id for entry in entries}
    added: list[str] = []
    for entry in extra:
        if entry.vacancy_id in seen:
            continue
        seen.add(entry.vacancy_id)
        entries.append(entry)
        added.append(entry.vacancy_id)
    return tuple(entries), frozenset(added)


def classify(
    entries: list[QueueEntry], results: ResultsPayload, *, manual: frozenset[str] = frozenset()
) -> tuple[Row, ...]:
    """Decide, for each entry, whether it is ready and what to say about it.

    Every applicable reason is collected rather than the first one found: a
    vacancy that is archived *and* was already applied to should say both, or
    fixing one of them looks like it should have helped.

    This is presentation, not policy. Nothing here decides whether an
    application may be sent — the agent's own prefilter does that again, on the
    page, before anything is clicked, and hh gets the last word after that.
    What this produces is the sentence a person reads.
    """
    outcomes = {result.vacancy_id: result for result in results.results}
    rows: list[Row] = []
    for entry in entries:
        blockers: list[str] = []
        notes: list[str] = []
        outcome = outcomes.get(entry.vacancy_id)
        if outcome is not None and outcome.status in FINISHED:
            said = FINISHED[outcome.status]
            tail = f": {outcome.reason}" if outcome.reason else ""
            blockers.append(f"прошлый прогон — {said}{tail}")
            if outcome.hh_warning:
                blockers.append(f"hh сказал: {outcome.hh_warning}")
        if entry.archived:
            blockers.append("вакансия в архиве")
        if entry.closed_for_applicants:
            blockers.append("работодатель закрыл приём откликов")
        if entry.external_application:
            blockers.append("отклик оформляется на сайте работодателя, руками")
        if entry.vacancy_id in manual:
            # Said before the missing score below it, because it explains the
            # missing score: nobody scored this row, somebody typed it.
            notes.append("добавлено вручную — этой строки нет в очереди из базы")
        elif entry.score is None:
            notes.append("оценка соответствия не посчитана — порядок списка ничего не значит")
        if not entry.letter:
            notes.append("письма нет — отклик уйдёт без сопроводительного")
        rows.append(Row(entry=entry, blockers=tuple(blockers), notes=tuple(notes)))
    return tuple(rows)


#: The report is printed to a cp1251 console, so the rules are plain ASCII.
#: A box-drawing character here would kill the run at the last line.
RULE: Final[str] = "-" * 78

_ID = 12
_SCORE = 6
_COMPANY = 22


def render(view: QueueView, *, encoding: str) -> str:
    """The whole report as one string, ready to print.

    Returned rather than printed so a test can read it without a console, and
    so the reduction to the console's codepage happens once, here, on the text
    that will actually be shown.
    """
    lines: list[str] = [RULE, f"ОЧЕРЕДЬ ОТКЛИКОВ   источник: {view.source}", RULE]
    for warning in view.warnings:
        lines.append(f"  {warning}")
    if view.warnings:
        lines.append("")

    if not view.rows:
        lines.append("  Очередь пуста.")
        lines.append(RULE)
        return printable("\n".join(lines), encoding)

    lines.append(f"ГОТОВО К ОТКЛИКУ: {len(view.ready)}")
    lines.append(f"  {'id':<{_ID}} {'score':>{_SCORE}}  {'компания':<{_COMPANY}} вакансия")
    if not view.ready:
        lines.append("  (ничего)")
    lines.extend(_row_lines(row) for row in view.ready)

    lines.append("")
    lines.append(f"НЕ ГОТОВО: {len(view.blocked)}")
    if not view.blocked:
        lines.append("  (ничего)")
    for row in view.blocked:
        lines.append(_row_lines(row))

    lines.append("")
    lines.append(RULE)
    lines.append(
        f"ИТОГО в очереди {len(view.rows)}: готово {len(view.ready)}, "
        f"не готово {len(view.blocked)}. Прогонов записано: {view.results_seen}."
    )
    lines.append("Отправка: python -m wwao apply --send. Нужен человек за клавиатурой.")
    lines.append(RULE)
    return printable("\n".join(lines), encoding)


def _row_lines(row: Row) -> str:
    """One vacancy: the line of columns, then its reasons under it."""
    entry = row.entry
    score = f"{entry.score:>{_SCORE}.1f}" if entry.score is not None else f"{'--':>{_SCORE}}"
    head = (
        f"  {entry.vacancy_id:<{_ID}} {score}  "
        f"{_fit(entry.company or '-', _COMPANY):<{_COMPANY}} {entry.title}"
    )
    tail = [f"  {'':<{_ID}} {'':>{_SCORE}}  {text}" for text in (*row.blockers, *row.notes)]
    if entry.score_explanation:
        tail.extend(
            f"  {'':<{_ID}} {'':>{_SCORE}}  | {line}"
            for line in entry.score_explanation.splitlines()
        )
    if entry.letter:
        tail.append(f"  {'':<{_ID}} {'':>{_SCORE}}  письмо готово, {len(entry.letter)} симв.")
    tail.append(f"  {'':<{_ID}} {'':>{_SCORE}}  {entry.url}")
    return "\n".join([head, *tail])


def _fit(text: str, width: int) -> str:
    """Keep a column a column. The full value is on the vacancy's own line."""
    return text if len(text) <= width else text[: width - 1] + "…"


#: The one environment variable this package reads, and the only one it names.
#:
#: ``/api/v1/applications`` is behind a shared local token: both processes run
#: on the owner's machine, so there is no second party to authenticate, but an
#: unauthenticated queue would hand somebody's cover letters to anything else on
#: the host that guessed the URL. The value is read here, put in one header and
#: never printed — not into an error, not into the report.
#:
#: It is deliberately the *only* variable read anywhere in this package, and
#: ``apply`` reads none at all: the confirmation is a word typed by a person and
#: nothing outside the terminal may be able to stand in for it.
TOKEN_VARIABLE: Final[str] = "AGENT_API_TOKEN"

#: The settings file the backend reads the same token from. Read here too since
#: 2026-09-16: a person who put the token in ``.env`` — which is what
#: ``.env.example`` tells them to do — got a 401 from this command and a message
#: saying only that the variable was missing from the environment. The
#: environment still wins when both are set, as it does for the backend.
DOTENV: Final[Path] = Path(__file__).resolve().parents[1] / ".env"


def local_token() -> tuple[str, str]:
    """The local token and where it was found: ``окружение``, ``.env`` or ``""``.

    Where it came from is returned so an error can say which of the two places
    to fix. The value itself is never printed by anything in this package.
    """
    import os

    from_environment = os.environ.get(TOKEN_VARIABLE, "").strip()
    if from_environment:
        return from_environment, "окружение"
    from_file = read_dotenv(DOTENV).get(TOKEN_VARIABLE, "")
    if from_file:
        return from_file, ".env"
    return "", ""


def read_dotenv(path: Path) -> dict[str, str]:
    """``KEY=value`` lines of a dotenv file, as pydantic-settings reads them.

    Comments, blank lines and an ``export`` prefix are tolerated; one layer of
    matching quotes is removed. A file that is missing or unreadable is simply
    empty — the caller then says the token is not set anywhere.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def fetch_over_http(base_url: str, limit: int) -> str:
    """``GET {base}/api/v1/applications/queue?limit=…``, as raw text.

    Raw text rather than parsed JSON so that :func:`parse_queue` stays the only
    place a payload becomes typed data, and so a test can hand this function's
    job to a string without a network stub.

    ``httpx`` is imported here rather than at module import: the file transport
    is what works without a backend running, and it must not need an HTTP client
    for that. The call is async because that is the project's rule for HTTP, and
    ``asyncio.run`` is what turns one request into a command-line program.
    """
    import asyncio

    import httpx

    url = f"{base_url.rstrip('/')}{QUEUE_PATH}"
    token, found_in = local_token()
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    async def get() -> str:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, params={"limit": limit}, headers=headers)
        if response.status_code == 404:
            raise QueueNotBuiltError(
                f"{url} отвечает 404: эндпоинта очереди в этом бэкенде нет. "
                "Очередь можно прочитать из файла: "
                "python -m wwao queue --from agent/queue.json"
            )
        if response.status_code in (401, 403):
            raise QueueUnavailableError(
                f"{url} отвечает {response.status_code}: очередь закрыта локальным токеном. "
                f"Он берётся из {TOKEN_VARIABLE} — из окружения, а если там его нет, из "
                f"файла .env в корне проекта — и должен совпадать с тем, что видит бэкенд."
                + (
                    f" Сейчас токен взят из: {found_in}, и бэкенд его не принял — "
                    "перезапустите бэкенд, если меняли .env после его запуска."
                    if token
                    else f" Сейчас {TOKEN_VARIABLE} не задан ни в окружении, ни в .env."
                )
            )
        if response.status_code == 503:
            raise QueueUnavailableError(
                f"{url} отвечает 503: у бэкенда не задан {TOKEN_VARIABLE}, "
                "и без него очередь не отдаётся вообще."
            )
        if response.status_code >= 400:
            # The body is the backend's own detail — a 422 naming the parameter
            # it disliked, for instance. Clipped, because a stack of HTML from
            # something that is not our API should not become the report.
            raise QueueUnavailableError(
                f"{url} ответил {response.status_code}: {response.text[:400]}"
            )
        return response.text

    try:
        return asyncio.run(get())
    except httpx.HTTPError as error:
        raise QueueUnavailableError(f"Не удалось спросить {url}: {error}") from error
