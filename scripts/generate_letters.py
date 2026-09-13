"""Write cover letters and print what was written.

    uv run python scripts/generate_letters.py --vacancy 0192f0c1-....
    uv run python scripts/generate_letters.py --limit 5
    uv run python scripts/generate_letters.py --limit 5 --dry-run
    uv run python scripts/generate_letters.py --vacancy <id> --force --show

One vacancy with ``--vacancy``, a queue of them with ``--limit``. The queue is
the best-scoring matches for the active profile that do not have a letter yet,
so running it twice does not rewrite yesterday's work; ``--force`` overrides
that, for both modes.

Nothing here sends anything. Every letter is saved to ``application.cover_letter``
and read by a person before it goes anywhere near an employer.

Two output rules this file follows deliberately:

* **No box-drawing characters and no emoji.** The console this runs on encodes
  cp1251. A character outside it does not degrade — it raises UnicodeEncodeError
  in the middle of the report, after the work is done and before it is shown.
  Guillemets, the em dash and the ellipsis are inside cp1251 and are used freely;
  the rules are plain ASCII hyphens. ``backend/tests/test_letters.py`` asserts
  that this whole file survives an encode to cp1251.
* **The letter itself is only printed when asked for.** A batch of ten letters
  is ten screens; the table says what happened, ``--show`` prints the text.

The last block of the report is what past applications are known to have done,
and it is the one place in this script where being boring is the requirement.
Letters that got an answer are shown to the model as few-shot examples — see
:mod:`app.letters.examples`, which is emphatic that nothing here learns — and
there are two answered applications on this whole account. So the line is
counts, never a rate, and it opens with «данных пока мало» until there are
enough of them to mean something. The sentence is built by
:func:`app.letters.examples.summary_ru` rather than assembled here, so that the
script cannot be the place where the hedge gets dropped.
"""

# ruff: noqa: RUF001 - everything this script prints is Russian, which
# is what the homoglyph guard cannot tell from a homoglyph attack. The project
# grants scripts/run_pipeline.py the same exemption through per-file-ignores in
# pyproject.toml; it is declared here because that file belongs to another change.

import argparse
import asyncio
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.core.logging import configure_logging
from app.db.session import session_factory
from app.letters import store
from app.letters.examples import ExamplePool, OutcomeEvidence, summary_ru
from app.letters.guard import RUSSIAN
from app.letters.service import LetterOutcome, write_batch, write_letter

RULE = "-" * 78

#: Why a vacancy produced no letter, in the words a person reads.
SKIPPED: dict[str, str] = {
    "vacancy_not_found": "вакансия не найдена",
    "letter_exists": "письмо уже написано (--force перезапишет)",
    "dry_run": "dry-run: письмо не генерировалось",
    "letter_unwritable": "письмо не удалось написать так, чтобы оно прошло проверки",
}

#: Where the saved text came from. The distinction matters to whoever sends it:
#: a fallback letter is true and dull, and worth editing before sending.
SOURCE: dict[str, str] = {
    "model": "модель",
    "fallback": "шаблон",
}


@dataclass(frozen=True, slots=True)
class Report:
    """What one run wrote, and what was known about past letters while it did.

    The two travel together because they are read together and misread apart: a
    letter written with two examples in its prompt and one written with none are
    indistinguishable in the tracker, and the second line of the report is the
    only place that says which happened.
    """

    outcomes: list[LetterOutcome]
    #: The account's own record, from the same query the generator used. Counts
    #: only — see :func:`app.letters.examples.summary_ru`.
    evidence: OutcomeEvidence


def parse_args() -> argparse.Namespace:
    """Command line."""
    parser = argparse.ArgumentParser(description="Generate cover letters for matched vacancies.")
    parser.add_argument("--vacancy", help="one vacancy id; otherwise a queue is worked")
    parser.add_argument("--profile", help="profile id; defaults to the active profile")
    parser.add_argument("--limit", type=int, default=5, help="how many vacancies from the queue")
    parser.add_argument(
        "--min-score",
        type=Decimal,
        default=None,
        help=(
            "lowest match score to write for (default: AGENT_QUEUE_MIN_SCORE); "
            "a filtered vacancy is never written for, whatever its score"
        ),
    )
    parser.add_argument("--force", action="store_true", help="rewrite a letter that already exists")
    parser.add_argument(
        "--dry-run", action="store_true", help="show the overlap, call no model, write nothing"
    )
    parser.add_argument("--show", action="store_true", help="print each letter in full")
    return parser.parse_args()


def show(report: Report | None, *, full: bool) -> None:
    """Print the run, in the order a person reads it."""
    print(RULE)
    print("ПИСЬМА")
    print(RULE)
    if report is None:
        print("  нет активного профиля: сначала загрузите резюме")
        print(RULE)
        return
    outcomes = report.outcomes
    if not outcomes:
        print("  нечего писать: в очереди нет вакансий с подходящим скором")
        _show_evidence(report)
        print(RULE)
        return

    print(f"  {'вакансия':<38} {'закрыто':>8} {'пробел':>7} {'знаков':>7}  источник")
    for outcome in outcomes:
        title = _clip(f"{outcome.title} / {outcome.company or '—'}", 38)
        if outcome.skipped is not None:
            print(f"  {title:<38} {SKIPPED.get(outcome.skipped, outcome.skipped)}")
            if outcome.skipped == "dry_run":
                print(f"  {'':<38} закрыто {outcome.matched}, не закрыто {outcome.missing}")
            continue
        letter = outcome.letter
        source = SOURCE.get(letter.source, letter.source) if letter else "—"
        print(
            f"  {title:<38} {outcome.matched:>8} {outcome.missing:>7} "
            f"{outcome.characters:>7}  {source}"
        )
        if letter and letter.rejected_for:
            faults = ", ".join(RUSSIAN[problem] for problem in letter.rejected_for)
            print(f"  {'':<38} отклонено и переписано: {faults}")
        if letter and letter.examples_used:
            print(f"  {'':<38} примеров в промте: {letter.examples_used}")

    written = [outcome for outcome in outcomes if outcome.saved]
    print()
    print(RULE)
    print(f"ИТОГО  написано {len(written)} из {len(outcomes)}")
    _show_evidence(report)
    print(RULE)

    if full:
        for outcome in written:
            print()
            print(RULE)
            print(f"{outcome.title} / {outcome.company or '—'}")
            print(RULE)
            print(outcome.letter.text if outcome.letter else "")


def _show_evidence(report: Report) -> None:
    """What past applications are known to have done, in counts and never a rate.

    Two lines at most. The first is the account's record and comes from
    :func:`app.letters.examples.summary_ru` already carrying its own «данных
    пока мало»; the second is a fact about this run and is printed only when
    there is one, so a run with no examples — which is every run today — says
    nothing about examples at all.
    """
    print(f"ОТВЕТЫ  {summary_ru(report.evidence)}")
    with_examples = [
        outcome
        for outcome in report.outcomes
        if outcome.letter is not None and outcome.letter.examples_used
    ]
    if with_examples:
        print(f"        писем с примерами в промте: {len(with_examples)}")


def _clip(text: str, width: int) -> str:
    """Fit a title into the column without wrapping the table."""
    return text if len(text) <= width else text[: width - 1] + "…"


async def run(args: argparse.Namespace) -> Report | None:
    """Do the work, in one transaction that is committed at the end.

    None means there is no profile to write for, which is a different thing from
    an empty queue and reads differently in the report.

    The pool of past outcomes is read here, once, and handed to whichever of the
    two entry points does the writing. Reading it here rather than letting them
    read it themselves is what lets the report state what was known even when
    nothing was written — an empty queue, or a vacancy that already had a
    letter, are both runs during which the account's record still exists.
    """
    profile_id = UUID(args.profile) if args.profile else None
    async with session_factory() as session:
        profile = await store.load_profile_facts(session, profile_id)
        if profile is None:
            return None
        pool: ExamplePool = await store.load_examples(session, profile_id=profile.profile_id)
        if args.vacancy:
            outcomes = [
                await write_letter(
                    session,
                    UUID(args.vacancy),
                    profile,
                    force=args.force,
                    dry_run=args.dry_run,
                    pool=pool,
                )
            ]
        else:
            outcomes = await write_batch(
                session,
                profile_id=profile.profile_id,
                limit=args.limit,
                min_score=args.min_score,
                force=args.force,
                dry_run=args.dry_run,
                pool=pool,
            )
        if not args.dry_run:
            await session.commit()
        return Report(outcomes=outcomes, evidence=pool.counts)


async def main() -> int:
    """Run it."""
    args = parse_args()
    configure_logging()
    report = await run(args)
    show(report, full=args.show)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
