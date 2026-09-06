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
"""

# ruff: noqa: RUF001 - everything this script prints is Russian, which
# is what the homoglyph guard cannot tell from a homoglyph attack. The project
# grants scripts/run_pipeline.py the same exemption through per-file-ignores in
# pyproject.toml; it is declared here because that file belongs to another change.

import argparse
import asyncio
import sys
from decimal import Decimal
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.core.logging import configure_logging
from app.db.session import session_factory
from app.letters import store
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


def parse_args() -> argparse.Namespace:
    """Command line."""
    parser = argparse.ArgumentParser(description="Generate cover letters for matched vacancies.")
    parser.add_argument("--vacancy", help="one vacancy id; otherwise a queue is worked")
    parser.add_argument("--profile", help="profile id; defaults to the active profile")
    parser.add_argument("--limit", type=int, default=5, help="how many vacancies from the queue")
    parser.add_argument(
        "--min-score", type=Decimal, default=Decimal("70"), help="lowest match score to write for"
    )
    parser.add_argument("--force", action="store_true", help="rewrite a letter that already exists")
    parser.add_argument(
        "--dry-run", action="store_true", help="show the overlap, call no model, write nothing"
    )
    parser.add_argument("--show", action="store_true", help="print each letter in full")
    return parser.parse_args()


def show(outcomes: list[LetterOutcome] | None, *, full: bool) -> None:
    """Print the run, in the order a person reads it."""
    print(RULE)
    print("ПИСЬМА")
    print(RULE)
    if outcomes is None:
        print("  нет активного профиля: сначала загрузите резюме")
        print(RULE)
        return
    if not outcomes:
        print("  нечего писать: в очереди нет вакансий с подходящим скором")
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

    written = [outcome for outcome in outcomes if outcome.saved]
    print()
    print(RULE)
    print(f"ИТОГО  написано {len(written)} из {len(outcomes)}")
    print(RULE)

    if full:
        for outcome in written:
            print()
            print(RULE)
            print(f"{outcome.title} / {outcome.company or '—'}")
            print(RULE)
            print(outcome.letter.text if outcome.letter else "")


def _clip(text: str, width: int) -> str:
    """Fit a title into the column without wrapping the table."""
    return text if len(text) <= width else text[: width - 1] + "…"


async def run(args: argparse.Namespace) -> list[LetterOutcome] | None:
    """Do the work, in one transaction that is committed at the end.

    None means there is no profile to write for, which is a different thing from
    an empty queue and reads differently in the report.
    """
    profile_id = UUID(args.profile) if args.profile else None
    async with session_factory() as session:
        profile = await store.load_profile_facts(session, profile_id)
        if profile is None:
            return None
        if args.vacancy:
            outcomes = [
                await write_letter(
                    session,
                    UUID(args.vacancy),
                    profile,
                    force=args.force,
                    dry_run=args.dry_run,
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
            )
        if not args.dry_run:
            await session.commit()
        return outcomes


async def main() -> int:
    """Run it."""
    args = parse_args()
    configure_logging()
    outcomes = await run(args)
    show(outcomes, full=args.show)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
