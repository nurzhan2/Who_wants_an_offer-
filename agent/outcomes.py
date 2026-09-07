"""What came of the applications this agent sent, read off hh a page at a time.

Everything else in this package is about getting one application out of the
door. This is the other end of the loop: it walks the vacancies the journal
already records as ``sent`` and writes down what hh now says about each one.
Nothing here can send anything — see "It reads, and that is structural" below.

**The vacancy page is the source of truth, and it is the only page opened.**
hh puts the applicant's own half of the page into the boot state it already
serves::

    "applicantVacancyResponseStatuses": {
      "133542745": {"negotiations": {"total": 1, "topicList": [
        {"id": …, "chatId": …, "initialState": "RESPONSE", "lastState": "DISCARD"}
      ]}}
    }

``agent/state_page.py`` has parsed that since 2026-09-06 and records where it
was measured; this module adds no reader of its own. Three reasons it is the
right source rather than the applicant's own responses list:

* it is keyed by the vacancy id, which is the id the journal, the gate and the
  confirmation card all key on, so no outcome can be attached to the wrong
  application by a matching step that guessed;
* it answers in hh's own vocabulary — ``lastState`` — which is what the
  tracker's ``hh_last_state`` column stores. The responses page answers in
  rendered Russian labels («Приглашение», «Собеседование», «Выход на работу»,
  «Ожидание», «Отказ», «Просмотрен», measured 2026-09-07 with counts beside
  them), and turning «Отказ» into ``DISCARD`` would be a mapping invented from
  a single coincidence;
* it is a page this package already knows how to open safely, through
  :func:`agent.hosts.open_hh_page` with the vacancy id it expects.

The responses page is cheaper — one page for the whole account instead of one
per application — and that is a real argument for reading it one day. **If
somebody adds it, the rule when the two disagree is: the vacancy page wins for
the state of one application, and the disagreement is written down rather than
reconciled.** The vacancy page names the vacancy; the responses list names a
row somebody has to match back to a vacancy, and a mismatch there is silent. A
second reader that quietly overrode the first would make every number below a
measurement of whichever reader ran last. Today there is one reader, so nothing
can disagree, and this paragraph is the design for the day that changes.

**``lastState`` is hh's set and it is open.** ``DISCARD`` is the only value
anyone here has observed, and the site renders it «Вам отказали».
:class:`agent.state_page.Application` says the set must not be enumerated in a
type and this module does not enumerate it either: whatever string hh puts
there is carried through to the tracker unread. Nothing in this package decides
what a state *means* — not "answered", not "rejected", not "in progress" —
because deciding that from one observed value is exactly the kind of invention
the rest of the package refuses. The tracker has a column; the meaning is a
question for a dashboard with more than two applications behind it.

**An outcome that goes backwards.** This module defines no order over hh's
states, so it cannot say one went backwards: with one value ever observed there
is nothing to order. What it *can* see is disappearance, and there are two
shapes of it, both handled the same way — recorded, said out loud, never acted
on:

* hh stops naming a state we had recorded. Nothing is reported for
  ``last_state``, because silence on the wire means "I did not look" and the
  tracker keeps what it has. Erasing an outcome the owner already read, on the
  strength of one page load, is the more expensive mistake.
* ``negotiations.total`` falls to ``0`` where it was ``1``. That number *is*
  reported, because ``0`` is hh's own measurement and the tracker column tells
  ``0`` apart from "unmeasured". It changes nothing else: the journal row stays
  ``sent`` — ``agent/state.py`` has no move out of it, and the journal's own
  docstring already says a local ``sent`` that hh disagrees with is the safe
  direction — and the walk prints the disagreement where a person will see it.

Either way the walk never re-applies, never re-queues and never edits the
journal. It has no opinion to act on.

**Rate: the same limits as the rest of the agent, and for the same reason.**
One page per sent application, the pause between them drawn from
:meth:`agent.config.Limits.pause` (40–140 seconds, randomised), the whole walk
refused outside working hours. A read is cheaper than an application but it is
the same account, the same session and the same fingerprint, and what hh
notices is a pattern rather than a payload. What a challenge costs is the
argument: this project's crawler measured a plain ``GET /vacancy/<id>``
answered ``302`` to ``/account/captcha`` on 2026-09-06, and once hh has decided
a client is a robot nothing it asks for afterwards is worth having. So the walk
stops at the first page that is not the vacancy it asked for —
:func:`agent.hosts.open_hh_page` already refuses that, and a challenge is
exactly that shape — and stops after two unreadable pages in a row, on
``stop_after_consecutive_failures``, the same rule the apply run uses. The
walk does *not* spend the daily cap: that budget counts applications, and this
sends none.

**It reads, and that is structural rather than promised.** The walk builds a
:class:`agent.gate.SubmitGate` and never arms it. Every application-shaped
request the browser tries therefore meets a gate with no mandate and is
aborted — including hh's own beacons, which carry a vacancy id and are refused
here exactly as they are on any un-armed page in this package. That is not a
side effect to apologise for; it is the proof, and the walk prints the count.
Nothing here imports :mod:`agent.mandate`, :mod:`agent.human` or
:mod:`agent.submit`, nothing clicks and nothing types, and
``agent/tests/test_outcomes.py`` executes the refusal rather than asserting the
absence of a call.

**The session is checked before anything is believed.** A logged-out browser
serves a perfectly good vacancy page with no
``applicantVacancyResponseStatuses`` on it at all, and every vacancy would then
read as "hh said nothing" — which is data-shaped and false. The health check
runs first, on the page the signal was measured on, and a walk that fails it
records nothing.
"""

import argparse
import random
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final, final

from agent.browser import open_browser, screenshot_on_error
from agent.config import AGENT_DIR, JOURNAL_PATH, Limits
from agent.gate import SubmitGate
from agent.hosts import NavigatedElsewhereError, open_hh_page, vacancy_url
from agent.journal import Entry, Journal
from agent.queue import HttpQueue, QueueFormatError, Result, ResultsFile, ResultSink
from agent.session import SignalUnknownError, load_signal, signal_source
from agent.session import check as session_check
from agent.state import Status
from agent.state_page import printable, read_negotiations, read_state

#: The walk's own memory: what hh said the last time each application was
#: looked at, in the results shape both transports already carry.
#:
#: A file rather than a row in ``agent.sqlite3`` because the journal has one
#: ``reason`` column and ``agent/run.py`` already stores hh's warning in it;
#: writing an outcome there would erase the sentence the confirmation card was
#: built from.
#:
#: **Under ``agent/probe/`` because of what is in it, not because of what wrote
#: it.** ``.gitignore`` describes that directory as holding "measurements of a
#: logged-in session … the owner's own application state for a vacancy", and
#: this document is exactly that: which jobs the owner applied to and whether
#: they were turned down. A repository with a live remote committed three
#: artefacts of that kind once already — see
#: ``agent/tests/test_isolation.py::test_everything_the_agent_writes_is_ignored_by_git``
#: — and this is the directory the answer to that lives in. ``agent/outcomes.json``
#: is the name this deserves and it is one ``.gitignore`` line away; until that
#: line exists, a file there would be one ``git add -A`` away from publishing
#: somebody's rejection history, and a filename is not worth that.
OUTCOMES_PATH: Final[Path] = AGENT_DIR / "probe" / "outcomes.json"

#: How many applications one walk looks at. Matches ``agent/run.py``'s ``BATCH``
#: for the same reason: a bound a person can hold in their head, well under a
#: day's worth of applications, so one invocation cannot turn into a long
#: unattended session on somebody's account.
WALK_LIMIT: Final[int] = 20


@final
class NotSignedInError(Exception):
    """The walk cannot tell "no outcome" from "not logged in", so it refuses."""


@final
@dataclass(frozen=True, slots=True)
class Seen:
    """What the previous walk recorded for one vacancy."""

    total: int | None
    last_state: str | None


@final
@dataclass(frozen=True, slots=True)
class Outcome:
    """What hh said about one application, on one page, at one moment.

    Every field is an observation. Nothing here is a verdict, a grade or a
    stage: the states are hh's strings and this class does not know what any of
    them means.
    """

    vacancy_id: str
    url: str
    title: str | None = None
    #: ``negotiations.total``. ``None`` means the page did not answer in a shape
    #: :func:`agent.state_page.read_negotiations` recognises — which is a
    #: different fact from ``0``, and the two must never be collapsed.
    total: int | None = None
    #: Every distinct non-null ``lastState`` on the vacancy, in the order hh
    #: listed them. Usually none or one.
    states: tuple[str, ...] = ()
    #: Why the page said nothing usable, when it said nothing usable.
    note: str | None = None

    @property
    def answered(self) -> bool:
        """Whether hh answered the question at all."""
        return self.total is not None

    @property
    def last_state(self) -> str | None:
        """The one state this application is in, when there is exactly one.

        A vacancy can carry more than one conversation — hh allows a second
        application under another resume — and two conversations in two states
        do not have a single outcome between them. Picking one of them would
        invent the answer, so this returns nothing and :attr:`states` keeps
        both for the person reading the run.
        """
        return self.states[0] if len(self.states) == 1 else None

    def to_result(self) -> Result:
        """This observation on the wire, as the results contract carries it.

        The status is ``sent`` because that is what the journal says and it is
        true; this walk observes outcomes and never moves an application
        anywhere. ``negotiations_total`` and ``last_state`` are the two fields
        the backend declared for exactly this and has been waiting for.
        """
        return Result(
            vacancy_id=self.vacancy_id,
            status=Status.SENT.value,
            negotiations_total=self.total,
            last_state=self.last_state,
        )


def read_outcome(page: Any, entry: Entry) -> Outcome:
    """Open one vacancy and read hh's own record of the application on it.

    ``Any`` for the page for the reason the rest of this package gives: typing
    it would mean importing playwright at module scope, and every pure module
    here stays importable on a machine with no browser.

    Raises whatever :func:`agent.hosts.open_hh_page` raises. A page that is not
    the vacancy asked for is not a page whose content may be read as that
    vacancy's — a sign-in wall, an archived redirect and hh's robot check all
    land there, and none of them is an outcome.
    """
    url = entry.url or vacancy_url(entry.vacancy_id)
    landed = open_hh_page(page, url, expect_vacancy=entry.vacancy_id)
    state = read_state(page.content())
    if state is None:
        return Outcome(
            vacancy_id=entry.vacancy_id,
            url=landed,
            title=entry.title,
            note="страница открылась, но hh не отдал на ней своё состояние",
        )
    negotiations = read_negotiations(state, entry.vacancy_id)
    if negotiations is None:
        return Outcome(
            vacancy_id=entry.vacancy_id,
            url=landed,
            title=entry.title,
            note="hh не пишет на этой странице ничего про отклики на неё",
        )
    states: list[str] = []
    for application in negotiations.applications:
        if application.last_state and application.last_state not in states:
            states.append(application.last_state)
    return Outcome(
        vacancy_id=entry.vacancy_id,
        url=landed,
        title=entry.title,
        total=negotiations.total,
        states=tuple(states),
    )


def walk(
    page: Any,
    entries: Sequence[Entry],
    *,
    limits: Limits,
    rng: random.Random,
    sleep: Callable[[float], object] = time.sleep,
) -> tuple[list[Outcome], str | None]:
    """Read every entry in order, pausing between pages, and stop when told to.

    Returns what was read and, when the walk was cut short, the sentence saying
    why. Cut short rather than failed: a walk that stopped after three pages
    still learned three things, and they are written down.

    ``sleep`` is an argument so the tests do not wait forty seconds a page. It
    is the only way to make the pause shorter, and it is not reachable from a
    command line.
    """
    read: list[Outcome] = []
    consecutive_failures = 0
    for index, entry in enumerate(entries):
        if index:
            sleep(limits.pause(rng))
        try:
            outcome = read_outcome(page, entry)
        except NavigatedElsewhereError as error:
            # hh served something other than the vacancy. That is the shape its
            # robot check takes, and the shape a sign-in wall takes, and after
            # either of them nothing more this walk asks for is worth having.
            return read, (
                f"hh отдал не ту страницу на вакансии {entry.vacancy_id}, и обход "
                f"остановлен на этом месте.\n{error}"
            )
        except Exception as error:
            # Broad, and recorded per vacancy rather than swallowed: one page
            # that will not load is not a reason to lose the pages that did.
            screenshot_on_error(page, f"outcome-{entry.vacancy_id}")
            read.append(
                Outcome(
                    vacancy_id=entry.vacancy_id,
                    url=entry.url or vacancy_url(entry.vacancy_id),
                    title=entry.title,
                    note=f"{type(error).__name__}: {error}",
                )
            )
            consecutive_failures += 1
            if consecutive_failures >= limits.stop_after_consecutive_failures:
                return read, (
                    "Две страницы подряд прочитать не удалось — обход остановлен. "
                    "Так выглядит и упавшая сессия, и изменившийся hh; посмотрите "
                    "скриншоты в agent/screenshots."
                )
            continue
        read.append(outcome)
        # An unreadable page counts the same way: hh changing shape looks like
        # this on every vacancy, and walking twenty of them to find that out is
        # twenty page loads spent proving the first one.
        consecutive_failures = 0 if outcome.answered else consecutive_failures + 1
        if consecutive_failures >= limits.stop_after_consecutive_failures:
            return read, (
                "Две страницы подряд ничего не сказали про отклики — обход остановлен. "
                "Либо сессия перестала быть сессией, либо hh переименовал поле; "
                "проверьте: python -m agent.login"
            )
    return read, None


def previous_states(recorded: Sequence[Result]) -> dict[str, Seen]:
    """What the last walk wrote, keyed by vacancy."""
    return {
        result.vacancy_id: Seen(total=result.negotiations_total, last_state=result.last_state)
        for result in recorded
    }


def describe(outcome: Outcome, before: Seen | None) -> list[str]:
    """The lines one vacancy contributes to the run's report, in hh's words.

    Everything quoted from hh or from the journal goes through
    :func:`agent.state_page.printable` first: this console encodes cp1251, and a
    character outside it raises in the middle of a walk rather than in a test.
    """
    head = f"  {outcome.vacancy_id}  {printable(outcome.title or 'без названия')}"
    if not outcome.answered:
        return [head, f"    hh ничего не сказал: {printable(outcome.note or 'причина неизвестна')}"]

    lines = [head]
    if outcome.states:
        said = ", ".join(printable(state) for state in outcome.states)
        lines.append(f"    hh: откликов {outcome.total}, состояние {said}")
        if len(outcome.states) > 1:
            lines.append(
                "    Переписок больше одной и состояния разные — какое из них про "
                "этот отклик, hh здесь не говорит, поэтому в трекер не пишется ни одно."
            )
    else:
        lines.append(f"    hh: откликов {outcome.total}, состояние не названо")

    if before is None:
        return lines
    if before.last_state and not outcome.last_state:
        lines.append(
            f"    ВНИМАНИЕ. В прошлый раз hh называл состояние {printable(before.last_state)}, "
            "а теперь не называет. Прошлое значение остаётся: молчание — это «не смотрели», "
            "а не «hh забрал слова назад»."
        )
    elif before.last_state and outcome.last_state and before.last_state != outcome.last_state:
        lines.append(
            f"    Изменилось: было {printable(before.last_state)}, "
            f"стало {printable(outcome.last_state)}."
        )
    elif not before.last_state and outcome.last_state:
        lines.append(f"    Появилось состояние: {printable(outcome.last_state)}.")
    if before.total is not None and before.total > 0 and outcome.total == 0:
        lines.append(
            f"    ВНИМАНИЕ. hh больше не считает этот отклик: было {before.total}, стало 0. "
            "Журнал не меняется — отправленное не отменяется тем, что его перестали "
            "показывать. Откройте вакансию сами и посмотрите."
        )
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    """Walk the sent applications and write down what hh says about them."""
    parser = argparse.ArgumentParser(
        prog="python -m agent.outcomes",
        description=(
            "Пройти по уже отправленным откликам и прочитать, что о них говорит hh. "
            "Ничего не отправляет и отправить не может."
        ),
    )
    parser.add_argument(
        "--limit", type=int, default=WALK_LIMIT, help="сколько откликов обойти за раз"
    )
    parser.add_argument(
        "--to",
        default=None,
        metavar="URL",
        help=(
            "дополнительно отправить прочитанное в трекер: базовый адрес бэкенда "
            "(http://localhost:8000). Локальный файл пишется в любом случае"
        ),
    )
    args = parser.parse_args(argv)

    limits = Limits.from_env()
    now = datetime.now().time()
    if not limits.within_working_hours(now):
        # The same rule the apply run keeps, for the same reason: this is the
        # owner's own account and what looks like a robot is a pattern of
        # activity rather than the content of one request.
        print(
            f"Сейчас {now:%H:%M}, а рабочие часы "
            f"{limits.work_starts:%H:%M}–{limits.work_ends:%H:%M}. Обход не начат."
        )
        return 0

    sink = _tracker(args.to)
    memory = ResultsFile(OUTCOMES_PATH)
    entries = Journal(JOURNAL_PATH).by_status(Status.SENT)[: max(0, args.limit)]
    if not entries:
        print("В журнале нет отправленных откликов — обходить нечего.")
        return 0

    try:
        before = previous_states(memory.read())
    except QueueFormatError as error:
        print(f"Прошлые результаты не прочитались ({error}); сравнивать не с чем.")
        before = {}

    gate = SubmitGate()
    print(f"Отправленных откликов в журнале: {len(entries)}. Открываю по одной странице.\n")
    read, stopped = _in_a_browser(entries, gate=gate, limits=limits)

    for outcome in read:
        for line in describe(outcome, before.get(outcome.vacancy_id)):
            print(line)
    if stopped is not None:
        print(f"\n{stopped}")

    answered = [outcome for outcome in read if outcome.answered]
    print(f"\nПрочитано: {len(answered)} из {len(entries)}.")
    _record(answered, memory=memory, sink=sink, destination=args.to)
    _say_nothing_was_armed(gate)
    gate.assert_no_escapes()
    return 1 if stopped is not None else 0


def _tracker(destination: str | None) -> ResultSink | None:
    """The optional second destination, checked before the browser opens."""
    if destination is None:
        return None
    if not destination.startswith(("http://", "https://")):
        raise SystemExit(
            f"--to ждёт базовый адрес бэкенда, а не {destination!r}. "
            "Локальный файл пишется без всяких флагов."
        )
    return HttpQueue(destination)


def _in_a_browser(
    entries: Sequence[Entry], *, gate: SubmitGate, limits: Limits
) -> tuple[list[Outcome], str | None]:
    """Open the owner's browser, prove the session is alive, and walk.

    The gate is registered exactly as the apply run registers it and is never
    armed, so every application-shaped request in this context is refused. The
    health check is first: a walk that cannot tell an expired session from an
    account with no answers yet would write the second when it saw the first.
    """
    with open_browser() as context:
        page = context.new_page()
        context.route("**/*", gate.handle)
        page.on("request", gate.observe)
        try:
            signal = load_signal()
        except SignalUnknownError as error:
            raise NotSignedInError(str(error)) from error
        open_hh_page(page, signal_source())
        session_check(read_state(page.content()) or {}, signal=signal)
        return walk(page, entries, limits=limits, rng=random.Random())


def _record(
    answered: Sequence[Outcome],
    *,
    memory: ResultsFile,
    sink: ResultSink | None,
    destination: str | None,
) -> None:
    """Write what was read, locally always and to the tracker when asked.

    Only vacancies hh actually answered about are reported. A result carrying a
    status and two nulls says nothing the tracker did not already know, and at
    the far end it is not free: ``record_results`` fills a still-empty
    ``sent_at`` from the moment the report arrives, so a walk that reported
    every page it opened would stamp today onto rows whose sends it knows
    nothing about.
    """
    memory.report([outcome.to_result() for outcome in answered])
    print(f"Записано: {memory.path}")
    if sink is None or not answered:
        return
    sink.report([outcome.to_result() for outcome in answered])
    print(
        f"Отправлено в трекер ({destination}): {len(answered)}. "
        "Что он с ними сделал, видно в его собственном логе."
    )


def _say_nothing_was_armed(gate: SubmitGate) -> None:
    """Print the proof that this walk could not have sent anything.

    The number is hh's own furniture — beacons, a blacklist check, a feedback
    survey, all of which carry a vacancy id — meeting a gate with no mandate
    behind it. It is printed rather than hidden because it is the one line that
    says out loud what the module docstring claims.
    """
    print(
        f"Мандатов не выдавалось; шлюз отклонил похожих на отклик запросов: "
        f"{len(gate.blocked)}. Так и должно быть — отправить этот обход не может."
    )


if __name__ == "__main__":  # pragma: no cover - a console entry point
    try:
        raise SystemExit(main())
    except NotSignedInError as error:
        print(f"\n{error}\n", file=sys.stderr)
        raise SystemExit(2) from error
