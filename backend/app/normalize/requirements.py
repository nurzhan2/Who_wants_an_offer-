"""Turning what a connector already extracted into rows scoring can compare.

``matching/`` needs two things from a vacancy that the crawl does not write into
columns: the skills it asks for, and how much experience. Both are already in
``vacancy_source.raw["_derived"]`` — the connector put them there — and both sit
unused, which is why ``vacancy_skill`` was empty for all 643 rows and why
``min_years`` was set on exactly the 60 seeded ones.

**Why this reads from the stored payload rather than from the posting.** The
same function then serves the crawl and the backfill: the pipeline calls it for
the ids it has just written, a script calls it for everything, and there is one
implementation to be right about rather than two that drift. It also means a
change here can be applied to the corpus already collected without re-crawling
it, which at one page every four to five seconds is the difference between a
minute and a day.

**Why the skills are not run through an LLM.** hh hands them over as a
structured list, so the comparison is a set intersection rather than a reading
of prose — which is the whole reason this source is worth the trouble. Sending
that list to a model would make the cheapest and most accurate part of the
pipeline the slowest, the most expensive and the least reproducible.
"""

import re
from collections.abc import Iterable
from decimal import Decimal
from typing import Any, Final

from app.resume.skills import SkillCanonicalizer, default_canonicalizer

#: ``vacancy_skill.canonical_name`` is ``varchar(100)``.
MAX_NAME: Final[int] = 100

#: How hh states the experience it wants. Mapped to the LOWER bound of each
#: band, because the field is a minimum: "between1And3" is an employer saying
#: one year will do, and reading it as three would fail a candidate the vacancy
#: would have accepted. ``docs/MATCHING.md`` scores the gap from this number.
EXPERIENCE_YEARS: Final[dict[str, Decimal]] = {
    "noExperience": Decimal("0"),
    "between1And3": Decimal("1"),
    "between3And6": Decimal("3"),
    "moreThan6": Decimal("6"),
}

#: A language requirement wearing a skill's clothes.
#:
#: The connector already separates the rendered form — «Казахский — B2 —
#: Средне-продвинутый» — into ``_derived.language_requirements``, and measured
#: over 1291 skill mentions not one of those reaches ``key_skills``. What does
#: reach it is the bare form: «Английский язык» 14 times and «Казахский язык»
#: once. Left in, each becomes a hard skill nobody can hold, and a candidate who
#: speaks English fluently reads as missing a requirement — the exact failure
#: the connector's split was written to prevent, arriving by the other door.
#:
#: Deliberately narrow: it matches a named language, not the word "язык", so
#: «Язык разметки» or «Языки программирования» stay skills.
LANGUAGE_SKILL: Final[re.Pattern[str]] = re.compile(
    r"^\s*(русский|английский|казахский|немецкий|французский|испанский|итальянский"
    r"|китайский|японский|корейский|турецкий|арабский|польский|украинский)\b"
    r"(\s+язык\w*)?\s*(—.*)?$",
    re.IGNORECASE,
)

#: Collapse runs of whitespace so two spellings of one phrase are one row.
_SPACES: Final[re.Pattern[str]] = re.compile(r"\s+")


def is_language(raw: str) -> bool:
    """Whether this ``key_skills`` entry is really a language requirement."""
    return bool(LANGUAGE_SKILL.match(raw))


def readable_fold(raw: str) -> str:
    """A stable name for a skill the dictionary has never heard of.

    The shipped dictionary is deliberately small — measured against this corpus
    it recognises 9 of 645 distinct spellings, because the corpus is mostly
    sales, construction and network roles rather than the candidate's field. An
    unrecognised skill is still worth a row: it is what the vacancy asks for and
    the candidate does not have, which is exactly what ``missing_required`` and
    the skill-gap analytics are made of.

    Folded rather than canonicalised, and the difference is honest: case and
    spacing only. Nothing is invented, so «Активные продажи» never quietly
    becomes something it is not, and it will match a candidate who wrote the
    same words.
    """
    return _SPACES.sub(" ", raw.strip()).casefold()[:MAX_NAME]


def skill_names(
    derived: dict[str, Any] | None, *, canonicalizer: SkillCanonicalizer | None = None
) -> list[str]:
    """Every skill this vacancy asks for, canonical where the dictionary knows it.

    Order is preserved and duplicates are dropped, because ``vacancy_skill`` is
    unique on ``(vacancy_id, canonical_name)`` and two spellings of one skill
    would otherwise lose the whole batch to a constraint violation rather than
    one row to a fold.
    """
    resolver = canonicalizer or default_canonicalizer()
    raw = (derived or {}).get("key_skills")
    if not isinstance(raw, list):
        return []

    seen: dict[str, None] = {}
    for item in raw:
        if not isinstance(item, str) or not item.strip() or is_language(item):
            continue
        name = resolver.canonicalize(item) or readable_fold(item)
        if name:
            seen.setdefault(name[:MAX_NAME], None)
    return list(seen)


def min_years(derived: dict[str, Any] | None) -> Decimal | None:
    """The experience the vacancy asks for, in years, or None when it does not say.

    None is not zero. "This employer wants no experience" and "this employer did
    not say" score differently in ``docs/MATCHING.md``, and collapsing them would
    make every silent vacancy look like an entry-level one.
    """
    stated = (derived or {}).get("work_experience")
    if not isinstance(stated, str):
        return None
    return EXPERIENCE_YEARS.get(stated)


def language_requirements(derived: dict[str, Any] | None) -> list[tuple[str, str]]:
    """The languages a vacancy wants, as ``(name, CEFR level)`` pairs.

    hh renders them as one string — «Казахский — B2 — Средне-продвинутый» — with
    an em dash and a human label after the code. Only the first two parts carry
    meaning; the third is the same information in words.

    ``docs/MATCHING.md`` says this gate cannot run because nothing extracts a
    vacancy's language requirement. That stopped being true: the connector
    extracts it, and this reads it. The document is corrected alongside.
    """
    stated = (derived or {}).get("language_requirements")
    if not isinstance(stated, list):
        return []
    found: list[tuple[str, str]] = []
    for item in stated:
        if not isinstance(item, str):
            continue
        parts = [part.strip() for part in item.split("—")]
        if len(parts) >= 2 and parts[0] and parts[1]:
            found.append((parts[0].casefold(), parts[1].upper()))
    return found


def dedupe(names: Iterable[str]) -> list[str]:
    """Order-preserving unique, for callers assembling names from several places."""
    return list(dict.fromkeys(names))
