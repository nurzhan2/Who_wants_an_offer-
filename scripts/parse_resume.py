"""Parse one resume and print what came out, what it cost, and how long it took.

    uv run python scripts/parse_resume.py backend/tests/fixtures/resumes/two_column_ru.pdf

No database: this runs the extraction pipeline only, so it can be pointed at any
file without touching the profile store. It needs ANTHROPIC_API_KEY.

``--show-columns`` needs no key at all. It prints what pdfplumber makes of the
file, which is the fastest way to see why PDFs are handed to the model natively:
on a two-column resume the sidebar and the body interleave line by line, and a
model fed that text extracts confident nonsense.

Personal data is masked by default. The bundled fixtures are invented people,
but this script will be pointed at a real CV, and a terminal is a place things
get pasted from. ``--show-pii`` turns the masking off.
"""

import argparse
import asyncio
import re
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import pdfplumber

from app.core.config import settings
from app.core.logging import configure_logging
from app.llm.client import LLMClient
from app.resume import enricher, extractor, profile_builder

EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
PHONE = re.compile(r"\+?\d[\d\s()-]{8,}\d")


def mask(value: str | None, *, show: bool) -> str:
    """Hide anything that identifies a person, unless explicitly asked not to."""
    if value is None:
        return "—"
    if show:
        return value
    masked = EMAIL.sub("<email>", value)
    masked = PHONE.sub("<phone>", masked)
    if masked == value and " " in value and len(value) < 60:
        # Looks like a name: keep the shape, drop the identity.
        return " ".join(part[0] + "…" for part in value.split() if part)
    return masked


def show_columns(path: Path) -> int:
    """Print pdfplumber's view of a PDF, warts and all."""
    if path.suffix.lower() != ".pdf":
        print(f"{path.name} is not a PDF; there is no column problem to show.")
        return 0

    with pdfplumber.open(path) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)

    print(f"pdfplumber's extraction of {path.name}, first 25 lines:\n")
    for line in text.splitlines()[:25]:
        print(f"  {line}")
    print(
        "\nIf the sidebar (skills, contacts) and the body (job titles, companies) "
        "\nappear on the same lines above, that is the interleaving. It is why the "
        "\nPDF itself goes to the model, and this text only goes to full-text search."
    )
    return 0


async def parse(path: Path, *, show_pii: bool, today: date) -> int:
    """Run extraction and the LLM, and report the result and the bill."""
    document = extractor.extract(path.read_bytes(), path.name)
    print(f"file              {path.name}")
    print(f"format            {document.source_format}")
    print(f"size              {document.size_bytes / 1024:.1f} KB")
    print(f"pages             {document.page_count}")
    print(f"extracted text    {len(document.raw_text)} chars")
    print(f"sent to the model {'the PDF itself' if document.source_format == 'pdf' else 'text'}")
    for warning in document.warnings:
        print(f"warning           {warning}")

    if settings.anthropic_api_key is None:
        print(
            "\nANTHROPIC_API_KEY is not set, so the model was not called.\n"
            "Copy .env.example to .env, fill the key in, and run this again to see\n"
            "the extracted profile and what the call cost."
        )
        return 2

    started = datetime.now(UTC)
    extraction, usage, cost = await profile_builder.extract_profile(
        document, client=LLMClient(), today=today
    )
    seconds = (datetime.now(UTC) - started).total_seconds()
    enriched = enricher.enrich(extraction, today=today)

    print("\n--- profile ---------------------------------------------------")
    print(f"name              {mask(extraction.full_name, show=show_pii)}")
    print(f"headline          {extraction.headline or '—'}")
    print(f"city / country    {extraction.city or '—'} / {extraction.country or '—'}")
    print(f"seniority         {enriched.seniority.value if enriched.seniority else '—'}")
    print(f"domains           {', '.join(enriched.domains) or '—'}")

    print("\n--- experience ------------------------------------------------")
    for period in extraction.work_periods:
        end = "present" if period.is_current else (period.end or "?")
        stack = ", ".join(period.stack[:6])
        print(f"  {period.start or '?':>7} .. {end:<8} {period.company:<24} {period.title}")
        if stack:
            print(f"                             stack: {stack}")

    stated = extraction.stated_total_years
    print(f"\ncomputed total    {enriched.total_years} years   (union of the periods above)")
    print(f"resume claims     {stated if stated is not None else '—'}")
    if enriched.stated_years_delta is not None:
        print(f"difference        {enriched.stated_years_delta:+}")

    print(f"\n--- skills ({len(enriched.skills)}) -------------------------------------------")
    for skill in sorted(enriched.skills, key=lambda s: s.years or 0, reverse=True):
        variants = " / ".join(skill.raw_names)
        years = f"{skill.years}y" if skill.years else "—"
        flag = " [not in dictionary]" if skill.is_unknown else ""
        print(f"  {skill.canonical_name:<22} {years:>5}  {skill.level.value:<8} {variants}{flag}")

    for warning in enriched.warnings:
        print(f"\nnote              {warning}")

    print("\n--- cost ------------------------------------------------------")
    print(f"model             {settings.anthropic_model_heavy}")
    print(f"input tokens      {usage.input_tokens:,}")
    print(f"output tokens     {usage.output_tokens:,}")
    print(f"cache read        {usage.cache_read_tokens:,}")
    print(f"cost              {'unpriced' if cost is None else f'${cost:.4f}'}")
    print(f"wall time         {seconds:.1f}s")
    if cost is not None:
        print(f"1000 resumes      ${cost * 1000:.2f}")
    return 0


def main() -> int:
    """Entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="Resume file: PDF, DOCX, TXT or Markdown.")
    parser.add_argument(
        "--show-columns",
        action="store_true",
        help="Print pdfplumber's raw extraction and stop. Needs no API key.",
    )
    parser.add_argument(
        "--show-pii", action="store_true", help="Do not mask names, emails or phone numbers."
    )
    args = parser.parse_args()

    if not args.path.is_file():
        print(f"no such file: {args.path}")
        return 1

    configure_logging()
    if args.show_columns:
        return show_columns(args.path)
    return asyncio.run(parse(args.path, show_pii=args.show_pii, today=datetime.now(UTC).date()))


if __name__ == "__main__":
    sys.exit(main())
