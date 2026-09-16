"""One run: prefilter, confirm, then send what a person agreed to.

The order of the first two statements in :func:`main` is the design. Both refuse
before a browser opens, both raise something the per-vacancy loop does not
catch, and between them they make "this has not been set up yet" a sentence
rather than a puzzle:

    assert_ready_to_apply()        # stage 0 has not been run
    _assert_idempotency_known()    # we cannot tell an applied vacancy from a fresh one

Without the first, running this before the probe produces a handful of Playwright
timeouts and stops after the second failure with the message "two things went
wrong" — a true statement about the wrong problem. Without the second, the agent
would be willing to apply while unable to tell whether it has already applied,
which is the one mistake nobody can undo.

**Dry run is the default and sending takes two independent acts.** ``--send``
gets as far as the confirmation; the confirmation itself needs a word typed in
full. Neither alone is enough, there is no flag that answers the prompt, and an
EOF on stdin — a cron job, a pipe, a closed terminal — is a refusal rather than
a default yes. The brief forbids «способы убрать человека из цикла» and a flag
that pre-answers a prompt is one.

Everything irreversible happens inside ``gate.armed(mandate)``, and the mandate
is bound to the text the human read. A page that changed underneath the
confirmation, a queue that was refetched, a letter that was regenerated: all of
them break the binding, and breaking the binding stops the send.

**One candidate per vacancy per run, and :func:`_to_candidates` is where that is
decided** (2026-09-07). Nothing below it can decide it. Each candidate gets its
own mandate; ``SubmitGate`` resets its window per arming — correctly, because
one confirmation is one window — so two mandates naming one vacancy are, and
must remain, indistinguishable to the gate. ``submit()`` re-reads
``negotiations.total`` before every click and would usually catch the second
attempt, but that is a race against hh's own bookkeeping and it is a second
line, not the line. The queue is a hand-maintained JSON file today, so a
repeated row is ordinary input rather than a hypothesis.

**The record of what a run did outlives the run's own bookkeeping**
(2026-09-07). A journal write inside the per-vacancy ``except`` handler used to
be able to raise — ``IllegalTransitionError``, on a row a duplicate had already
moved to ``sent`` — and it escaped the loop, the ``with open_browser()`` block
and ``main()`` on a run where an application had *already gone out*: no results
file, and an interception escape would have gone unreported. So every journal
write in the loop goes through :class:`_Bookkeeping`, which turns a refusal into
a visible note instead of an exception, and the results file and
``gate.assert_no_escapes()`` are written and run whatever ends the run. A
refused transition is never swallowed: it is printed, counted, and the run exits
non-zero, because the state machine is a guarantee and not a formality.

**A vacancy leaves ``needs_manual`` or ``failed`` only by a person's hand, and
``--requeue`` is that hand** (2026-09-07). ``agent.state`` reserves those two
moves for :data:`~agent.state.Actor.HUMAN` and nothing in this package performed
them, so a vacancy the run had set aside was skipped for ever and anything the
journal remembered about it could never reach a confirmation card. The command
sends nothing and opens nothing; it moves rows the owner names, keeping what was
said about them.

**hh's own words are now something the owner reads, never something the agent
obeys** (2026-09-07). «Чтобы откликнуться на эту вакансию, поменяйте видимость
резюме…» used to arrive here as a refusal, be written to ``needs_manual`` and
stop the application; it is advice, hh accepts those applications, and the
measurement is in ``agent/state_page.py`` beside the words it matches. So there
is one more thing this file has to get right than there was: the sentence is
about the *resume*, not about the vacancy it appeared on, which makes it true of
every application in a batch. It goes into the journal and the results file per
vacancy, onto the next confirmation card under its own heading, and — because
twelve identical lines scrolling past read as twelve small remarks rather than
one large one — into a single line after the run's own count. See
:func:`_say_what_hh_said_about_the_resume`.
"""

import argparse
import random
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, final

from agent.browser import open_browser, screenshot_on_error
from agent.config import JOURNAL_PATH, QUEUE_PATH, Limits
from agent.gate import InterceptionEscapedError, SubmitGate
from agent.hosts import open_hh_page
from agent.human import CancelledError, Candidate, confirm
from agent.journal import Entry, Journal
from agent.letter import UnsafeLetterError
from agent.letter import check as check_letter
from agent.mandate import SendMandate, digest
from agent.prefilter import Verdict, decide_before_opening
from agent.queue import (
    FileQueue,
    HttpQueue,
    MergedQueue,
    Queue,
    QueueItem,
    QueueUnreachableError,
    Result,
    ResultsFile,
)
from agent.selectors import (
    LetterFieldUnknownError,
    SelectorsNotVerifiedError,
    assert_ready_to_apply,
)
from agent.session import check as session_check
from agent.session import load_signal, signal_source
from agent.state import (
    Actor,
    IllegalInitialStatusError,
    IllegalTransitionError,
    Status,
)
from agent.state_page import (
    APPLIED_SIGNAL_MEASURED,
    mentions_visibility,
    printable,
    read_state,
)
from agent.submit import (
    AlreadyAppliedError,
    CaptchaPresentedError,
    IdempotencyUnknownError,
    LetterNotTypedError,
    WrongVacancyError,
    looks_like_a_challenge,
    submit,
)

#: How many queue items one run will even look at. Well under the daily cap so a
#: single run cannot exhaust the day's budget by itself.
BATCH: Final[int] = 20

#: Where the backend is when nobody says otherwise. Both processes belong to the
#: same person on the same machine — that is the whole arrangement, and it is
#: also why the endpoint is behind a shared local token rather than a login.
#: A URL and not a secret, so it is a default and a flag rather than an
#: environment variable; the token beside it is the opposite and never appears
#: on a command line.
DEFAULT_BACKEND: Final[str] = "http://localhost:8000"

#: Marks a journal reason that is hh's own sentence rather than this agent's.
#:
#: The journal has one column for "why it ended here" and two kinds of text want
#: it: what this agent concluded, and what hh said. Only the second may be shown
#: on a confirmation card as hh's words, so the two have to be told apart, and a
#: prefix written at the one place that knows the difference is the cheapest way
#: that does not mean changing a table another change owns. It reads correctly
#: to a person looking at the journal, which is the other half of the job.
HH_QUOTE: Final[str] = "hh: "

#: The two states a person may bring a vacancy back from, and the whole of what
#: ``--requeue`` will touch. Taken from ``agent.state.TRANSITIONS``, where both
#: moves are reserved for :data:`~agent.state.Actor.HUMAN`; ``sent`` and
#: ``skipped`` are terminal and ``confirmed`` has no way back by design.
REQUEUEABLE: Final[frozenset[Status]] = frozenset({Status.NEEDS_MANUAL, Status.FAILED})


class NotSetUpError(Exception):
    """Something about stage 0 is missing. Never caught per vacancy."""


class ChallengedError(Exception):
    """hh challenged the browser before the run could start. Never caught per vacancy.

    Separate from the per-vacancy :class:`~agent.submit.CaptchaPresentedError`
    because it means something different: not "this one vacancy needs a person"
    but "nothing in this run can proceed". A challenge is a decision hh made
    about the whole session, and walking a batch of twenty against it would be
    both useless and the rudest possible response.
    """


def _assert_idempotency_known() -> None:
    """Refuse to send while "have I already applied" has no answer.

    Measured on 2026-09-06 on the owner's own profile, at ``total == 0`` and at
    ``total == 1``, so this now passes — and the flag it reads says what it
    means rather than standing in for it. It used to ask whether a tuple of
    marker paths was non-empty, which was a proxy for the question and a proxy
    that outlived the design: the applied signal is a count now, not the
    presence of a key.

    The refusal stays because the condition can come back. If hh changes the
    shape and somebody sets that flag to ``False`` while they work it out, every
    vacancy reads as "unknown", every unknown goes to a human, and a run that
    could only produce manual work should say so at the start instead of opening
    a browser to discover it.
    """
    if not APPLIED_SIGNAL_MEASURED:
        raise NotSetUpError(
            "Неизвестно, как выглядит уже отправленный отклик в состоянии страницы,\n"
            "а без этого агент не может отличить новую вакансию от той, куда уже\n"
            "откликались. Отправка запрещена.\n\n"
            "Нужен прогон: uv run python -m agent.probe_apply --stage inspect\n"
            "по вакансии с уже отправленным откликом, затем заполнить\n"
            "agent/state_page.py."
        )


@final
@dataclass
class _Bookkeeping:
    """The journal, and what it refused to write.

    Every journal write the run loop makes goes through :meth:`remember`, and
    the reason is a defect this class exists to make impossible rather than a
    stylistic preference. The handler that records why one vacancy failed was
    itself able to raise: ``Journal.record`` checks the transition, a row that
    something else had already moved to ``sent`` cannot legally go to
    ``skipped``, and the resulting ``IllegalTransitionError`` escaped the loop,
    the ``with open_browser()`` block and ``main()`` — on a run where an
    application had already been sent. ``queue.report(results)`` and
    ``gate.assert_no_escapes()`` sit after that block and never ran, so the run
    destroyed its own record of what it had done while trying to write it down.

    A refusal is not swallowed. The state machine is a real guarantee — a
    transition it rejects means the journal and this loop disagree about what
    happened to somebody's application — so the refusal is printed to stderr as
    it occurs, kept here, and reported at the end, and the run exits non-zero.
    What it no longer does is take the record down with it.
    """

    journal: Journal
    #: One line per refused write, in the order they happened.
    refused: list[str] = field(default_factory=list)

    def remember(self, entry: Entry, *, actor: Actor) -> bool:
        """Write one row; answer whether it was written.

        Returns a bool, unlike ``agent.state.check``, which deliberately does
        not: there the answer is "may this happen", and a predicate is a
        predicate somebody forgets to check. Here the answer is "did this get
        written", and one caller — the confirmation write — genuinely has to
        branch on it, because a mandate whose ``confirmed`` row does not exist
        must not be sent.
        """
        try:
            self.journal.record(entry, actor=actor)
        except (IllegalTransitionError, IllegalInitialStatusError) as error:
            note = f"{entry.vacancy_id}: журнал не принял «{entry.status.value}» — {error}"
            self.refused.append(note)
            print(f"  {note}", file=sys.stderr)
            return False
        return True


def _hh_words_in(entry: Entry | None) -> tuple[str | None, str | None]:
    """hh's own words out of a journal row, split back into the two things hh says.

    Everything else in that column — this agent's own conclusions — returns
    ``(None, None)``, because a card that attributes «не удалось прочитать
    состояние страницы» to hh is a card that teaches its reader not to believe
    the attribution.

    **Why this splits rather than handing back one string** (2026-09-07). The
    journal has one column for "why it ended here" and this package must not
    grow it a second one for a change this size; ``agent/journal.py`` belongs to
    another change. But the two things hh says are not the same kind of thing,
    and the card has to label them differently: «может получить отказ» is about
    this vacancy, and the resume-visibility notice is about the resume, so it is
    true of every application in the batch and not only of the one it happens to
    be recorded against. The split uses
    :func:`~agent.state_page.mentions_visibility` — the same rule that produced
    the lines in the first place, rather than a second opinion about them.
    """
    reason = entry.reason if entry is not None else None
    if reason is None or not reason.startswith(HH_QUOTE):
        return None, None
    lines = [line for line in reason[len(HH_QUOTE) :].splitlines() if line.strip()]
    visibility = [line for line in lines if mentions_visibility(line)]
    rest = [line for line in lines if not mentions_visibility(line)]
    return "\n".join(visibility) or None, "\n".join(rest) or None


def _to_candidates(items: Sequence[QueueItem], journal: Journal) -> list[Candidate]:
    """Everything worth showing a human, with the rest recorded and dropped.

    The prefilter runs on what the queue already knows, before any page is
    opened — which is the point of having one. The page is read again later,
    because a vacancy can close between a crawl and a run.

    **A vacancy named twice in one queue is looked at once, and this is the only
    place that can decide it.** The journal cannot: its own write for the first
    copy leaves the row at ``queued``, which is exactly the status this function
    reads as "not dealt with yet", so the second copy walks through the guard
    below — measured, two candidates for one vacancy, each with its own mandate.
    Nothing downstream can decide it either. Every candidate is confirmed
    separately and armed separately, and ``SubmitGate`` resets its window per
    arming because one confirmation is one window; a gate that refused the
    second arming would be a gate that refuses a legitimate retry after a
    person confirmed it again. ``submit()`` re-reads ``negotiations.total``
    before every click and will usually see the first application, but "usually"
    is a race against hh's own bookkeeping and this is the one mistake the owner
    cannot undo. So it is settled here, on the natural key, before a person is
    ever shown the card — which is also the last moment at which a duplicate
    costs nothing.
    """
    candidates: list[Candidate] = []
    seen: set[str] = set()
    for item in items:
        if item.vacancy_id in seen:
            # Counted as looked-at rather than as a candidate, so a queue with
            # the same vacancy three times produces one card and two lines.
            print(
                f"  пропускаю {item.vacancy_id} ({printable(item.title)}): "
                "эта вакансия уже есть в очереди этого прогона"
            )
            continue
        seen.add(item.vacancy_id)
        previous = journal.get(item.vacancy_id)
        if previous is not None and previous.status is not Status.QUEUED:
            # Already dealt with, or waiting on a person. A run must not drag an
            # item back out of a state only a human may leave — the state
            # machine forbids it, and the right behaviour when the journal
            # remembers something is to leave it alone rather than to crash.
            # Both the title and the reason are text this program did not
            # write — hh's, through the crawler and through the response form —
            # so both go through ``printable`` before they reach a console that
            # encodes cp1251 and dies on anything else.
            tail = f" — {printable(previous.reason)}" if previous.reason else ""
            print(
                f"  пропускаю {item.vacancy_id} ({printable(item.title)}): "
                f"{previous.status.value}{tail}"
            )
            continue
        # The queue stage, which knows only what the crawler stored. The
        # letter requirement, the employer test and whether we have already
        # applied all live on the page, and submit() decides them there against
        # prefilter.decide before it touches anything.
        decision = decide_before_opening(
            closed_for_applicants=item.closed_for_applicants,
            archived=item.archived,
            external_application=item.external_application,
        )
        if decision.verdict is not Verdict.PROCEED:
            journal.record(
                Entry(
                    item.vacancy_id,
                    decision.status,
                    title=item.title,
                    company=item.company,
                    url=item.url,
                    reason=decision.reason,
                ),
                actor=Actor.AGENT,
            )
            continue
        try:
            letter = check_letter(item.letter, required=False)
        except UnsafeLetterError as exc:
            journal.record(
                Entry(
                    item.vacancy_id,
                    Status.NEEDS_MANUAL,
                    title=item.title,
                    company=item.company,
                    url=item.url,
                    reason=str(exc),
                ),
                actor=Actor.AGENT,
            )
            continue
        # The reason is carried over rather than overwritten. ``Journal.record``
        # replaces that column on every write, so re-queueing a vacancy used to
        # erase the last thing hh said about it — and the last thing hh said is
        # exactly what the person answering the next confirmation needs.
        journal.record(
            Entry(
                item.vacancy_id,
                Status.QUEUED,
                title=item.title,
                company=item.company,
                url=item.url,
                reason=previous.reason if previous is not None else None,
            ),
            actor=Actor.AGENT,
        )
        visibility, warning = _hh_words_in(previous)
        candidates.append(
            Candidate(
                vacancy_id=item.vacancy_id,
                title=item.title,
                company=item.company,
                url=item.url,
                letter=letter,
                hh_warning=warning,
                # Carried straight from the queue to the card. This package
                # neither computes a score nor edits an explanation; it shows
                # what the backend said, so the person approving can disagree
                # with it.
                score=item.score,
                score_explanation=item.score_explanation,
                hh_visibility=visibility,
                ats=item.ats,
            )
        )
    return candidates


def _queue(base_url: str, manual: Path, *, use_backend: bool) -> Queue:
    """Where this run takes its work from, and where its outcomes go.

    The backend answers the question the queue *is*: which vacancies scored
    above the threshold, have a letter and have never been applied to. The file
    is what a person added by hand, offered after that list rather than instead
    of it — for a year it was the only source, and a night of crawling, scoring
    and letter-writing reached the agent as nothing at all.

    A missing file is the ordinary case and not an error: most runs have nothing
    hand-added. ``--no-backend`` inverts the arrangement for the day the backend
    is not running, and then the file is all there is — the same program that
    worked before any endpoint existed.
    """
    memory = ResultsFile(manual.with_name(f"{manual.stem}-results.json"))
    extra = FileQueue(manual) if manual.is_file() else None
    if not use_backend:
        if extra is None:
            raise SystemExit(
                f"--no-backend, а файла очереди {manual} нет. "
                "Создайте его (формат — в agent/README.md) или уберите --no-backend."
            )
        return extra
    return MergedQueue(backend=HttpQueue(base_url), extra=extra, memory=memory)


def _report(queue: Queue, results: Sequence[Result]) -> None:
    """Hand the outcomes back, and never lose them to a tracker that is down.

    Called from the ``finally`` that closes a run, which is the one place an
    exception costs the most: applications have already been sent by then, and
    a stack trace here would replace the summary of what went out. The local
    file is written first inside :class:`~agent.queue.MergedQueue`, so a failure
    to reach the backend is a line to read rather than a record lost.
    """
    try:
        queue.report(results)
    except QueueUnreachableError as error:
        print(f"\nРезультаты записаны локально, но трекер их не принял: {error}", file=sys.stderr)


def _requeue(journal: Journal, vacancy_ids: Sequence[str]) -> int:
    """Put vacancies a person has dealt with back in the queue. Sends nothing.

    The move this performs — ``needs_manual`` or ``failed`` back to ``queued`` —
    is one ``agent.state.TRANSITIONS`` reserves for a human, and until this
    existed nothing in the package performed it. That left two holes at once. A
    vacancy set aside for a person stayed at ``needs_manual`` for ever, so
    dealing with whatever it was set aside for changed nothing; and anything the
    journal remembered about it could never reach a confirmation card, because a
    card is only built for a row at ``queued``. The reason column is therefore
    written back rather than dropped — when it holds hh's own sentence it is the
    most specific thing anybody has about that application, and the card quotes
    it as hh's.

    The actor is the person typing this command, which is what the state machine
    requires and what makes this different from a retry loop. Nothing is opened,
    nothing is sent, and the next run still asks for a confirmation.
    """
    refused = 0
    for vacancy_id in vacancy_ids:
        entry = journal.get(vacancy_id)
        if entry is None:
            print(f"{vacancy_id}: такой вакансии в журнале нет.")
            refused += 1
            continue
        if entry.status not in REQUEUEABLE:
            allowed = " или ".join(sorted(status.value for status in REQUEUEABLE))
            print(
                f"{vacancy_id}: сейчас «{entry.status.value}», а вернуть в очередь "
                f"можно только из «{allowed}»."
            )
            refused += 1
            continue
        # ``Journal.record`` replaces the reason column on every write, so what
        # hh said has to be written back explicitly or it is gone.
        journal.record(Entry(vacancy_id, Status.QUEUED, reason=entry.reason), actor=Actor.HUMAN)
        tail = f" — {printable(entry.reason)}" if entry.reason else ""
        print(f"{vacancy_id}: снова в очереди{tail}")
    return 1 if refused else 0


def _close_the_books(
    books: _Bookkeeping,
    mandates: Sequence[SendMandate],
    attempted: set[str],
    results: list[Result],
) -> None:
    """Record the confirmed vacancies this run never got to.

    A run stops after two consecutive failures, and the confirmations it has not
    used are then stuck: ``confirmed`` is a status with no way out except by
    this loop, so those rows were skipped by every later run — silently, for
    ever. They are recorded as ``failed``, which is both true (the run ended
    without them being sent) and the only status a person can bring back, with
    ``--requeue``.

    Only vacancies the loop never reached are touched. One that was in flight
    when the run died keeps its ``confirmed`` row: its request may have left the
    browser, and marking it ``failed`` would invite a retry of an application
    that hh may already hold.
    """
    for mandate in mandates:
        if mandate.vacancy_id in attempted:
            continue
        reason = "прогон закончился, не дойдя до этой вакансии; подтверждение не использовано"
        books.remember(Entry(mandate.vacancy_id, Status.FAILED, reason=reason), actor=Actor.AGENT)
        results.append(Result(mandate.vacancy_id, Status.FAILED.value, reason))


def _escape_check(gate: SubmitGate) -> InterceptionEscapedError | None:
    """Ask the gate whether anything got out around it, without raising yet.

    Handed back rather than raised so the caller can decide: on the normal path
    it is raised, because nothing this run says about consent can be trusted
    after it; on the way out of another failure it is printed instead, so that
    an escape is never lost and never replaces the error already leaving.
    """
    try:
        gate.assert_no_escapes()
    except InterceptionEscapedError as error:
        return error
    return None


def main(argv: Sequence[str] | None = None) -> int:
    """Plan a run, show it to a person, and do only what they agreed to."""
    parser = argparse.ArgumentParser(description="Отклики на hh под аккаунтом владельца")
    parser.add_argument(
        "--send",
        action="store_true",
        help="дойти до подтверждения и отправить (по умолчанию — только показать)",
    )
    parser.add_argument(
        "--from",
        dest="backend",
        default=DEFAULT_BACKEND,
        help=(
            "базовый адрес бэкенда, откуда берётся очередь: вакансии со скором "
            "выше порога, с готовым письмом и без отправленного отклика "
            "(по умолчанию: %(default)s)"
        ),
    )
    parser.add_argument(
        "--no-backend",
        action="store_true",
        help="не спрашивать бэкенд, работать по одному файлу очереди",
    )
    parser.add_argument(
        "--queue",
        default=str(QUEUE_PATH),
        help="файл ручных добавлений к очереди; читается вдобавок к бэкенду",
    )
    parser.add_argument(
        "--requeue",
        nargs="+",
        metavar="ID",
        default=(),
        help=(
            "вернуть эти вакансии из needs_manual или failed обратно в очередь "
            "и выйти; ничего не открывает и не отправляет"
        ),
    )
    args = parser.parse_args(argv)

    limits = Limits.from_env()
    journal = Journal(JOURNAL_PATH)

    if args.requeue:
        # A move only a person may make, made by the person who typed it.
        # Nothing else runs on this path: no browser, no queue, no confirmation.
        return _requeue(journal, args.requeue)

    if args.send:
        # Both refusals happen here, before anything opens, and neither is
        # catchable by the loop below.
        assert_ready_to_apply()
        _assert_idempotency_known()

    queue = _queue(args.backend, Path(args.queue), use_backend=not args.no_backend)
    try:
        items = queue.take(BATCH)
    except QueueUnreachableError as error:
        # The backend is the queue; a run that cannot read it has nothing to
        # offer and must say why rather than reporting an empty morning.
        print(str(error), file=sys.stderr)
        return 1
    candidates = _to_candidates(items, journal)

    sent_today = journal.count_sent_since(
        datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    )
    room = max(0, limits.daily_cap - sent_today)
    if len(candidates) > room:
        print(f"Дневной лимит: сегодня осталось {room} из {limits.daily_cap}.")
        candidates = candidates[:room]

    if not candidates:
        print("Отправлять нечего.")
        return 0

    if not args.send:
        # The default. Everything above ran; nothing below will.
        print(f"\nСухой прогон. К отправке было бы {len(candidates)}:\n")
        for candidate in candidates:
            print(candidate.render(), "\n")
        print("Чтобы отправить: тот же запуск с --send.")
        return 0

    now = datetime.now().time()
    if not limits.within_working_hours(now):
        print(
            f"Сейчас {now:%H:%M}, а рабочие часы "
            f"{limits.work_starts:%H:%M}–{limits.work_ends:%H:%M}. Отправка не начата."
        )
        return 0

    try:
        mandates = confirm(candidates)
    except CancelledError as exc:
        print(f"Ничего не отправлено: {exc}")
        return 0

    books = _Bookkeeping(journal)
    results: list[Result] = []

    # The human has now said yes to each of these, so the journal says so too —
    # before anything opens. Without this write the run went from `queued`
    # straight at `sent`, a pair the state machine does not have, and the
    # IllegalTransitionError landed *after* the application had left: no `sent`
    # row, no results file, no escape check, and the next run offering the same
    # vacancy again because its row still read `queued`.
    #
    # What hh last said about the vacancy is written back with it. `record`
    # replaces that column on every write, so a bare `confirmed` row erased the
    # sentence the card had just been built from — and the vacancy would arrive
    # at the next run with nothing to show if this one did not finish.
    #
    # A mandate whose row the journal refuses is dropped from the run. Sending
    # it would mean sending without a `confirmed` row, which is the state the
    # `sent` write below is defined against.
    carried = {
        candidate.vacancy_id: "\n".join(
            line for line in (candidate.hh_visibility, candidate.hh_warning) if line
        )
        for candidate in candidates
    }
    confirmed: list[SendMandate] = []
    for mandate in mandates:
        warning = carried.get(mandate.vacancy_id)
        written = books.remember(
            Entry(
                mandate.vacancy_id,
                Status.CONFIRMED,
                reason=f"{HH_QUOTE}{warning}" if warning else None,
            ),
            actor=Actor.HUMAN,
        )
        if written:
            confirmed.append(mandate)
        else:
            results.append(
                Result(
                    mandate.vacancy_id,
                    Status.FAILED.value,
                    "журнал не принял подтверждение — отправка не начиналась",
                )
            )

    gate = SubmitGate()
    # Vacancies ``submit()`` was actually called for. Everything confirmed and
    # not in here never had a page opened for it; see :func:`_close_the_books`.
    attempted: set[str] = set()
    escaped: InterceptionEscapedError | None = None

    try:
        _apply_each(
            books=books,
            gate=gate,
            limits=limits,
            mandates=confirmed,
            attempted=attempted,
            results=results,
        )
    finally:
        # Whatever ends the run — a refusal, a challenge, Ctrl-C part way
        # through a batch — what it already did is written down first. The
        # results file and the escape check used to sit after the browser
        # block, where an exception from anywhere inside it skipped both.
        _close_the_books(books, confirmed, attempted, results)
        _report(queue, results)
        escaped = _escape_check(gate)
        if escaped is not None and sys.exc_info()[1] is not None:
            # Something else is already leaving. The escape still has to be
            # visible, but it must not replace the error the owner is about to
            # read, so it is printed rather than raised.
            print(f"\n{escaped}\n", file=sys.stderr)

    # If anything reached an application URL without passing the interceptor,
    # nothing this run says about consent can be trusted, and that has to be
    # louder than the summary below it.
    if escaped is not None:
        raise escaped

    sent_count = sum(1 for r in results if r.status == Status.SENT.value)
    print(f"\nОтправлено: {sent_count}")
    _say_what_hh_said_about_the_resume(results)
    if books.refused:
        # The journal and this loop disagree about what happened to somebody's
        # application. Nothing here can repair that, and a run that ends 0 says
        # it is fine.
        print(
            f"\nЖурнал отказал в {len(books.refused)} записях — состояние в журнале неполное:",
            file=sys.stderr,
        )
        for note in books.refused:
            print(f"  {note}", file=sys.stderr)
        return 1
    return 0


def _say_what_hh_said_about_the_resume(results: Sequence[Result]) -> None:
    """One line for the whole batch when hh objected to the resume's visibility.

    The per-vacancy lines are printed as each application goes out, and twelve of
    them scroll past looking like twelve opinions about twelve jobs. They are
    not: hh shows that sentence because of a setting on the resume, so it is one
    statement about the whole batch. Somebody who has just sent twelve
    applications should be told that hh thinks all twelve are limited — once,
    where they will read it, in hh's own words.

    Printed after «Отправлено: N» rather than instead of anything, and it reports
    rather than acts. The applications are sent, hh accepted them, and whether to
    change the setting is the owner's call to make with hh's sentence in front of
    them. Nothing here decides anything: that is the whole point of the change
    this function came with.
    """
    said = [
        result.hh_warning
        for result in results
        if result.status == Status.SENT.value
        and result.hh_warning
        and mentions_visibility(result.hh_warning)
    ]
    if not said:
        return
    # hh's own line, out of the first application that carried it. They are the
    # same sentence on every vacancy — it is about the resume — so quoting one is
    # quoting all of them, and quoting all of them would be twelve copies.
    quoted = next(line for line in (said[0] or "").splitlines() if mentions_visibility(line))
    print(
        f"\nВНИМАНИЕ. hh показал это предупреждение на {len(said)} из "
        f"{sum(1 for r in results if r.status == Status.SENT.value)} отправленных откликов:"
    )
    print(f"  | {printable(quoted)}")
    print(
        "  Отклики ушли: hh их принимает, это измерено. Но предупреждение — про само\n"
        "  резюме, а не про эти вакансии, поэтому оно верно и для всех следующих\n"
        "  откликов, пока настройка видимости не изменится."
    )


def _apply_each(
    *,
    books: _Bookkeeping,
    gate: SubmitGate,
    limits: Limits,
    mandates: Sequence[SendMandate],
    attempted: set[str],
    results: list[Result],
) -> None:
    """Open the browser once and work through the confirmed mandates in order.

    Split out of :func:`main` so that the bookkeeping which must survive this —
    the results file and the escape check — is written in a ``finally`` around
    one call rather than around a block that also owns the browser. Everything
    it records, it records through ``books``, which cannot raise.

    ``attempted`` and ``results`` are written into rather than returned, because
    the caller needs both of them after this raises as much as after it does
    not: that is the whole point of the split.
    """
    rng = random.Random()
    consecutive_failures = 0

    with open_browser() as context:
        page = context.new_page()
        context.route("**/*", gate.handle)
        page.on("request", gate.observe)
        signal = load_signal()
        # Checked on the same kind of page the signal was measured on. login.py
        # records the keys that appear on a *vacancy* page after signing in, and
        # asserting them against the home page compared two different documents:
        # keys that are simply absent from the front page read as an expired
        # session, and a run could refuse forever on a perfectly good login.
        # Through the helper, like every other navigation in this package: hh
        # redirects to a regional subdomain after signing in, playwright reports
        # the interrupted navigation as an error, and a raw ``goto`` turns that
        # into a run that dies on its own health check. No vacancy is named,
        # because this page is only being asked whether the account is still
        # there — which page it is was decided when the signal was measured.
        landed = open_hh_page(page, signal_source())
        if looks_like_a_challenge(landed):
            # Told apart from an expired session on purpose. Both make the next
            # line fail, and the sentence they produce is the whole output of
            # this branch: «войдите заново» sends the owner to sign in again for
            # no reason, when what happened is that hh decided to check whether
            # the browser is a robot. The window is not raised here — unlike in
            # the loop below, nothing follows this, so the context would close
            # over the raised window before anybody could look at it.
            raise ChallengedError(
                f"hh показывает проверку на робота вместо страницы: {landed}\n"
                "Агент её не решает. Откройте hh в браузере, пройдите проверку "
                "руками и запустите прогон заново."
            )
        # The health check reads the page rather than the navigation result: a
        # session that has expired still serves a perfectly good 200.
        session_check(read_state(page.content()) or {}, signal=signal)

        for index, mandate in enumerate(mandates):
            if consecutive_failures >= limits.stop_after_consecutive_failures:
                # The brief's rule. Not a retry budget — two in a row means
                # something changed and a person should look before we spend
                # more of the day's allowance discovering it.
                print("Две ошибки подряд — останавливаюсь, посмотрите, что происходит.")
                break
            if index:
                time.sleep(limits.pause(rng))
            # Recorded before the attempt rather than after it: everything not
            # in here is a vacancy no page was ever opened for, which is what
            # lets :func:`_close_the_books` tell "never reached" apart from
            # "reached and something happened".
            attempted.add(mandate.vacancy_id)
            try:
                sent = submit(page, mandate, gate)
            except (
                AlreadyAppliedError,
                IdempotencyUnknownError,
                CaptchaPresentedError,
                WrongVacancyError,
                # The letter has to go into a field nobody has measured, or the
                # field never took it. Not a breakage: a vacancy for a person,
                # with a sentence saying what would change the answer.
                #
                # ``RefusedByHHError`` was on this list until 2026-09-07 and is
                # gone with the class. Nothing hh writes in the response form
                # stops an application any more, so no branch here carries hh's
                # words: every sentence recorded below is this agent's own
                # conclusion and is written as such. hh's words now leave through
                # the success path, which is where hh puts them.
                LetterFieldUnknownError,
                LetterNotTypedError,
            ) as exc:
                status = (
                    Status.SKIPPED if isinstance(exc, AlreadyAppliedError) else Status.NEEDS_MANUAL
                )
                # Through ``books``: this write is the one that was measured
                # raising ``IllegalTransitionError`` and taking the whole run's
                # record with it, on a run that had already sent something.
                books.remember(
                    Entry(mandate.vacancy_id, status, reason=str(exc)),
                    actor=Actor.AGENT,
                )
                results.append(Result(mandate.vacancy_id, status.value, str(exc)))
                # A skip is a normal outcome; the others are not. A captcha in
                # particular is the case the brief calls stop-and-wait, and this
                # used to be a no-op assignment on that branch — so hh could
                # challenge every vacancy in the batch, the window would be
                # raised twenty times, and the run would exit 0 saying nothing
                # was sent.
                consecutive_failures = 0 if status is Status.SKIPPED else consecutive_failures + 1
                continue
            except Exception as exc:
                screenshot_on_error(page, f"fail-{mandate.vacancy_id}")
                books.remember(
                    Entry(mandate.vacancy_id, Status.FAILED, reason=f"{type(exc).__name__}: {exc}"),
                    actor=Actor.AGENT,
                )
                results.append(Result(mandate.vacancy_id, Status.FAILED.value, str(exc)))
                consecutive_failures += 1
                continue
            # The application left. Everything hh said while its form was open
            # is kept rather than dropped, and since 2026-09-07 that is both
            # families rather than one: «Такой отклик может получить отказ» and
            # the requirement it names, which is hh's own analysis of why this
            # one is likely to fail and is more specific than any score computed
            # here; and «поменяйте видимость резюме…», which used to stop the
            # send and is now advice about the resume — so it is true of every
            # application in this batch, not only of this one, and the owner has
            # to be able to see that.
            said = "\n".join(sent.said)
            books.remember(
                Entry(
                    mandate.vacancy_id,
                    Status.SENT,
                    letter_digest=digest(mandate.letter),
                    reason=f"{HH_QUOTE}{said}" if said else None,
                ),
                actor=Actor.AGENT,
            )
            # The evidence behind "sent", and the letter it went with. Until
            # 2026-09-16 neither reached the result: the first real run reported
            # four applications with ``negotiations_total`` empty, so the tracker
            # could not tell a confirmed send from a report of one. The count is
            # the one ``submit`` re-read off hh after the click, and the letter is
            # the mandate's — the text the person confirmed and the only string
            # ``submit`` had in scope to type.
            results.append(
                Result(
                    mandate.vacancy_id,
                    Status.SENT.value,
                    hh_warning=said or None,
                    sent_letter=mandate.letter,
                    negotiations_total=sent.confirmed.total,
                    last_state=sent.last_state,
                )
            )
            for line in sent.said:
                print(f"  {mandate.vacancy_id}: hh предупреждает — {line}")
            consecutive_failures = 0


if __name__ == "__main__":  # pragma: no cover - a console entry point
    # The refusals that stop a whole run are caught here and nowhere else.
    # Printing them to stderr and exiting 2 is what makes "you have not run the
    # probe yet" and "hh is checking whether you are a robot" readable sentences
    # rather than tracebacks, and keeping the handler at the top level is what
    # stops anything inside the run loop from swallowing them.
    try:
        raise SystemExit(main())
    except (SelectorsNotVerifiedError, NotSetUpError, ChallengedError) as error:
        print(f"\n{error}\n", file=sys.stderr)
        raise SystemExit(2) from error
