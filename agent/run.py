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
"""

import argparse
import random
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from agent.browser import open_browser, screenshot_on_error
from agent.config import JOURNAL_PATH, QUEUE_PATH, Limits
from agent.gate import SubmitGate
from agent.human import CancelledError, Candidate, confirm
from agent.journal import Entry, Journal
from agent.letter import UnsafeLetterError
from agent.letter import check as check_letter
from agent.mandate import digest
from agent.prefilter import Verdict, decide_before_opening
from agent.queue import FileQueue, QueueItem, Result
from agent.selectors import SelectorsNotVerifiedError, assert_ready_to_apply
from agent.session import check as session_check
from agent.session import load_signal
from agent.state import Actor, Status
from agent.state_page import APPLIED_MARKERS, read_state
from agent.submit import (
    AlreadyAppliedError,
    CaptchaPresentedError,
    IdempotencyUnknownError,
    WrongVacancyError,
    submit,
)

#: hh's front page: somewhere harmless to land while the session is checked.
HH_HOME: Final[str] = "https://hh.kz/"

#: How many queue items one run will even look at. Well under the daily cap so a
#: single run cannot exhaust the day's budget by itself.
BATCH: Final[int] = 20


class NotSetUpError(Exception):
    """Something about stage 0 is missing. Never caught per vacancy."""


def _assert_idempotency_known() -> None:
    """Refuse to send while "have I already applied" has no answer.

    ``APPLIED_MARKERS`` is filled from a probe run against a vacancy the owner
    has already applied to. While it is empty every vacancy reads as "unknown",
    every unknown goes to a human, and a run that could only produce manual work
    should say so at the start instead of opening a browser to discover it.
    """
    if not APPLIED_MARKERS:
        raise NotSetUpError(
            "Неизвестно, как выглядит уже отправленный отклик в состоянии страницы,\n"
            "а без этого агент не может отличить новую вакансию от той, куда уже\n"
            "откликались. Отправка запрещена.\n\n"
            "Нужен прогон: uv run python -m agent.probe_apply --stage inspect\n"
            "по вакансии с уже отправленным откликом, затем заполнить\n"
            "APPLIED_MARKERS в agent/state_page.py."
        )


def _to_candidates(items: Sequence[QueueItem], journal: Journal) -> list[Candidate]:
    """Everything worth showing a human, with the rest recorded and dropped.

    The prefilter runs on what the queue already knows, before any page is
    opened — which is the point of having one. The page is read again later,
    because a vacancy can close between a crawl and a run.
    """
    candidates: list[Candidate] = []
    for item in items:
        previous = journal.get(item.vacancy_id)
        if previous is not None and previous.status is not Status.QUEUED:
            # Already dealt with, or waiting on a person. A run must not drag an
            # item back out of a state only a human may leave — the state
            # machine forbids it, and the right behaviour when the journal
            # remembers something is to leave it alone rather than to crash.
            print(
                f"  пропускаю {item.vacancy_id} ({item.title}): "
                f"{previous.status.value}" + (f" — {previous.reason}" if previous.reason else "")
            )
            continue
        # The queue stage, which knows only what the crawler stored. The
        # letter requirement, the employer test and whether we have already
        # applied all live on the page and are decided there, in submit().
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
        journal.record(
            Entry(
                item.vacancy_id,
                Status.QUEUED,
                title=item.title,
                company=item.company,
                url=item.url,
            ),
            actor=Actor.AGENT,
        )
        candidates.append(
            Candidate(
                vacancy_id=item.vacancy_id,
                title=item.title,
                company=item.company,
                url=item.url,
                letter=letter,
            )
        )
    return candidates


def main(argv: Sequence[str] | None = None) -> int:
    """Plan a run, show it to a person, and do only what they agreed to."""
    parser = argparse.ArgumentParser(description="Отклики на hh под аккаунтом владельца")
    parser.add_argument(
        "--send",
        action="store_true",
        help="дойти до подтверждения и отправить (по умолчанию — только показать)",
    )
    parser.add_argument("--queue", default=str(QUEUE_PATH), help="файл очереди")
    args = parser.parse_args(argv)

    limits = Limits.from_env()
    journal = Journal(JOURNAL_PATH)

    if args.send:
        # Both refusals happen here, before anything opens, and neither is
        # catchable by the loop below.
        assert_ready_to_apply()
        _assert_idempotency_known()

    queue = FileQueue(Path(args.queue))
    items = queue.take(BATCH)
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

    gate = SubmitGate()
    rng = random.Random()
    results: list[Result] = []
    consecutive_failures = 0

    with open_browser() as context:
        page = context.new_page()
        context.route("**/*", gate.handle)
        page.on("request", gate.observe)
        signal = load_signal()
        page.goto(HH_HOME, wait_until="domcontentloaded")
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
            try:
                submit(page, mandate, gate)
            except (
                AlreadyAppliedError,
                IdempotencyUnknownError,
                CaptchaPresentedError,
                WrongVacancyError,
            ) as exc:
                status = (
                    Status.SKIPPED if isinstance(exc, AlreadyAppliedError) else Status.NEEDS_MANUAL
                )
                journal.record(
                    Entry(mandate.vacancy_id, status, reason=str(exc)), actor=Actor.AGENT
                )
                results.append(Result(mandate.vacancy_id, status.value, str(exc)))
                consecutive_failures = 0 if status is Status.SKIPPED else consecutive_failures
                continue
            except Exception as exc:
                screenshot_on_error(page, f"fail-{mandate.vacancy_id}")
                journal.record(
                    Entry(mandate.vacancy_id, Status.FAILED, reason=f"{type(exc).__name__}: {exc}"),
                    actor=Actor.AGENT,
                )
                results.append(Result(mandate.vacancy_id, Status.FAILED.value, str(exc)))
                consecutive_failures += 1
                continue
            journal.record(
                Entry(mandate.vacancy_id, Status.SENT, letter_digest=digest(mandate.letter)),
                actor=Actor.AGENT,
            )
            results.append(Result(mandate.vacancy_id, Status.SENT.value))
            consecutive_failures = 0

    queue.report(results)
    # If anything reached an application URL without passing the interceptor,
    # nothing this run says about consent can be trusted, and that has to be
    # louder than the summary above it.
    gate.assert_no_escapes()
    sent_count = sum(1 for r in results if r.status == Status.SENT.value)
    print(f"\nОтправлено: {sent_count}")
    return 0


if __name__ == "__main__":  # pragma: no cover - a console entry point
    # The two refusals are caught here and nowhere else. Printing them to stderr
    # and exiting 2 is what makes "you have not run the probe yet" a readable
    # sentence rather than a traceback, and keeping the handler at the top level
    # is what stops anything inside the run loop from swallowing them.
    try:
        raise SystemExit(main())
    except (SelectorsNotVerifiedError, NotSetUpError) as error:
        print(f"\n{error}\n", file=sys.stderr)
        raise SystemExit(2) from error
