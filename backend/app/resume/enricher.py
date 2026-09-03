"""Turning a raw extraction into the profile that gets stored.

Three jobs, in order:

1. **Canonicalise and merge skills.** This is where the extraction stops being a
   list of strings and becomes a set of facts keyed by canonical name.
2. **Attach evidence.** Years per skill and last-used year come from
   ``app.resume.dates``, computed from the jobs where the skill was used — never
   from anything the model asserted.
3. **Infer what the resume left implicit**: proficiency level and seniority.

The merge step is not optional cleanup. A resume that lists "Python" in its
skills sidebar and "Python 3" in a job description produces two extracted
skills that canonicalise to the same ``python``. Writing both would violate the
``(profile_id, canonical_name)`` unique constraint, and it happens on close to
every real resume rather than as an edge case.
"""

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal

from app.db.enums import Seniority, SkillLevel
from app.resume import dates
from app.resume.skills import SkillCanonicalizer, default_canonicalizer
from app.schemas.llm import ProfileExtraction, StatedLevel

#: Ascending proficiency, so merging two records of the same skill can take the
#: stronger claim rather than whichever happened to be extracted last.
LEVEL_ORDER: tuple[SkillLevel, ...] = (
    SkillLevel.BASIC,
    SkillLevel.WORKING,
    SkillLevel.STRONG,
    SkillLevel.EXPERT,
)

#: A skill unused for this long is worth flagging: the market moves, and
#: "Angular, last touched in 2019" is not the same claim as "Angular".
STALE_AFTER_YEARS = 3

# Title patterns, in the languages this market's resumes use.
#
# The Russian forms are stems with no trailing word boundary, on purpose. Titles
# inflect — "руководитель", "руководителя", "старший", "старшего" — and `\b`
# after a stem never matches, because the letter that follows it is still a word
# character. That silently made every Russian leadership title invisible. The
# Latin forms keep their boundaries, where boundaries do work.
LEAD_TITLE = re.compile(
    r"\b(?:lead|head\s+of|principal|staff|cto|team\s*lead)\b|тимлид|руководител|начальник",
    re.IGNORECASE,
)
SENIOR_TITLE = re.compile(r"\b(?:senior|sr\.?)\b|ведущ|старш", re.IGNORECASE)
JUNIOR_TITLE = re.compile(r"\b(?:junior|jr\.?|intern|trainee)\b|стажёр|стажер|младш", re.IGNORECASE)

_NON_SLUG = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True, slots=True)
class EnrichedSkill:
    """One canonical skill, with the evidence behind it."""

    canonical_name: str
    #: Every spelling the resume used, in the order they were found.
    raw_names: tuple[str, ...]
    years: Decimal | None
    level: SkillLevel
    last_used_year: int | None
    #: True when the canonicaliser did not recognise the name and the canonical
    #: form is a slug of the original. Counted so the phase 4 dictionary work
    #: has a measure of how much it is missing.
    is_unknown: bool = False

    def is_stale(self, *, today: date) -> bool:
        """Whether the skill has gone cold."""
        if self.last_used_year is None:
            return False
        return today.year - self.last_used_year > STALE_AFTER_YEARS


@dataclass(frozen=True, slots=True)
class EnrichedProfile:
    """Everything derived from an extraction, ready to be persisted."""

    skills: tuple[EnrichedSkill, ...]
    total_years: Decimal
    seniority: Seniority | None
    domains: tuple[str, ...]
    titles: tuple[str, ...]
    #: Notes worth a log line, none of which quote the resume.
    warnings: tuple[str, ...]
    #: How far the resume's own claim about experience is from the computed
    #: figure. Populated only when the resume made a claim.
    stated_years_delta: Decimal | None = None


def slugify(raw: str) -> str:
    """Fallback canonical name for a skill the dictionary does not know.

    Keeping unknown skills under a stable slug beats dropping them: the matcher
    can still compare them textually, and the stored ``raw_names`` become the
    input for extending the dictionary in phase 4.
    """
    folded = unicodedata.normalize("NFKD", raw).casefold().strip()
    slug = _NON_SLUG.sub("-", folded).strip("-")
    return slug or "unknown"


def _level_from(stated: StatedLevel | None) -> SkillLevel | None:
    """Map the model's stated level onto ours, when it stated one."""
    if stated is None:
        return None
    return SkillLevel(stated)


def infer_level(
    *, stated: StatedLevel | None, years: Decimal | None, has_work_evidence: bool
) -> SkillLevel:
    """Proficiency, preferring what the resume says over what we can guess.

    Without a stated level the evidence is years plus where the skill appeared.
    A skill that only ever shows up in a bullet list is capped at ``working``
    however long the career is: listing a technology is not the same as
    describing having used it.
    """
    explicit = _level_from(stated)
    if explicit is not None:
        return explicit

    if years is None or years < Decimal("1"):
        inferred = SkillLevel.BASIC
    elif years < Decimal("3"):
        inferred = SkillLevel.WORKING
    elif years < Decimal("6"):
        inferred = SkillLevel.STRONG
    else:
        inferred = SkillLevel.EXPERT

    if not has_work_evidence and LEVEL_ORDER.index(inferred) > LEVEL_ORDER.index(
        SkillLevel.WORKING
    ):
        return SkillLevel.WORKING
    return inferred


def _stronger(left: SkillLevel, right: SkillLevel) -> SkillLevel:
    """The higher of two levels."""
    return max(left, right, key=LEVEL_ORDER.index)


def _merge(existing: EnrichedSkill, incoming: EnrichedSkill) -> EnrichedSkill:
    """Fold a duplicate canonical skill into the one already collected.

    Takes the strongest claim on every axis: the highest level, the longest
    experience, the most recent use. The raw spellings accumulate instead of
    overwriting each other, because they are the only record of what the resume
    actually said.
    """
    raw_names = list(existing.raw_names)
    for name in incoming.raw_names:
        if name not in raw_names:
            raw_names.append(name)

    years = max(
        (value for value in (existing.years, incoming.years) if value is not None),
        default=None,
    )
    last_used = max(
        (
            value
            for value in (existing.last_used_year, incoming.last_used_year)
            if value is not None
        ),
        default=None,
    )
    return EnrichedSkill(
        canonical_name=existing.canonical_name,
        raw_names=tuple(raw_names),
        years=years,
        level=_stronger(existing.level, incoming.level),
        last_used_year=last_used,
        # Known beats unknown: if any spelling was recognised, the skill is.
        is_unknown=existing.is_unknown and incoming.is_unknown,
    )


def companies_for(
    extracted_companies: Sequence[str],
    canonical: str,
    extraction: ProfileExtraction,
    resolver: SkillCanonicalizer,
) -> list[str]:
    """Which employers a skill was used at.

    The model is asked to link each skill to the companies where it was used,
    but it fills that field unreliably — and when it is empty there is no
    evidence to date the skill with, so every skill collapses to zero years and
    ``basic``. That would gut the level multiplier in the coverage score for a
    reason that has nothing to do with the candidate.

    The per-job ``stack`` lists are filled far more consistently, and they carry
    the same information from the other direction. Falling back to them recovers
    the evidence. Matching goes through the canonicaliser so a job listing
    "Python 3" counts for the skill "Python".
    """
    if extracted_companies:
        return list(extracted_companies)

    matched: list[str] = []
    for period in extraction.work_periods:
        if not period.company:
            continue
        for technology in period.stack:
            resolved = resolver.canonicalize(technology) or slugify(technology)
            if resolved == canonical:
                matched.append(period.company)
                break
    return matched


def enrich_skills(
    extraction: ProfileExtraction,
    *,
    today: date,
    canonicalizer: SkillCanonicalizer | None = None,
) -> tuple[tuple[EnrichedSkill, ...], tuple[str, ...]]:
    """Canonicalise, merge and date every extracted skill.

    Returns the skills keyed uniquely by canonical name, so the caller can hand
    them straight to ``ProfileRepository.replace_skills`` without tripping the
    unique constraint.
    """
    resolver = canonicalizer or default_canonicalizer()
    merged: dict[str, EnrichedSkill] = {}
    unknown_count = 0

    for extracted in extraction.skills:
        raw = extracted.name.strip()
        if not raw:
            continue

        resolved = resolver.canonicalize(raw)
        is_unknown = resolved is None
        if resolved is None:
            unknown_count += 1
        canonical = resolved if resolved is not None else slugify(raw)

        companies = companies_for(extracted.companies, canonical, extraction, resolver)
        span = dates.experience_with(extraction.work_periods, companies, today=today)
        years = span.years if span.months else None
        has_work_evidence = extracted.mentioned_in == "work_description" or bool(companies)

        skill = EnrichedSkill(
            canonical_name=canonical,
            raw_names=(raw,),
            years=years,
            level=infer_level(
                stated=extracted.level, years=years, has_work_evidence=has_work_evidence
            ),
            last_used_year=dates.last_used_year(extraction.work_periods, companies, today=today),
            is_unknown=is_unknown,
        )

        previous = merged.get(canonical)
        merged[canonical] = _merge(previous, skill) if previous else skill

    warnings: list[str] = []
    collapsed = len(extraction.skills) - len(merged)
    if collapsed > 0:
        warnings.append(f"{collapsed} skill spellings collapsed into canonical names")
    if unknown_count:
        # Counts only. The names themselves are resume content and stay in the
        # database, where the dictionary work can read them.
        warnings.append(f"{unknown_count} skills were not in the dictionary")

    return tuple(merged.values()), tuple(warnings)


def infer_seniority(*, total_years: Decimal, titles: Sequence[str]) -> Seniority | None:
    """Grade, from computed experience and the titles actually held.

    Experience is the baseline because titles inflate — "Senior" after three
    years is ordinary in this market. A title may promote the result by at most
    one step, except for a leadership title, which is a fact about the role
    rather than a self-assessment and is taken at face value.
    """
    if not titles and total_years <= 0:
        return None

    joined = " ".join(titles)
    if LEAD_TITLE.search(joined):
        return Seniority.LEAD

    if total_years >= Decimal("6"):
        baseline = Seniority.SENIOR
    elif total_years >= Decimal("3"):
        baseline = Seniority.MIDDLE
    else:
        baseline = Seniority.JUNIOR

    ladder = (Seniority.JUNIOR, Seniority.MIDDLE, Seniority.SENIOR, Seniority.LEAD)
    index = ladder.index(baseline)
    if SENIOR_TITLE.search(joined):
        index = min(index + 1, ladder.index(Seniority.SENIOR))
    elif JUNIOR_TITLE.search(joined) and index > 0:
        index -= 1
    return ladder[index]


def enrich(
    extraction: ProfileExtraction,
    *,
    today: date,
    canonicalizer: SkillCanonicalizer | None = None,
) -> EnrichedProfile:
    """Everything derived from an extraction, in one pass."""
    experience = dates.total_experience(extraction.work_periods, today=today)
    skills, skill_warnings = enrich_skills(extraction, today=today, canonicalizer=canonicalizer)

    titles = tuple(period.title for period in extraction.work_periods if period.title)
    domains = tuple(
        dict.fromkeys(
            domain.strip().casefold()
            for period in extraction.work_periods
            for domain in period.domains
            if domain.strip()
        )
    )

    warnings = [*experience.warnings, *skill_warnings]
    delta: Decimal | None = None
    if extraction.stated_total_years is not None:
        delta = Decimal(str(extraction.stated_total_years)) - experience.years
        if abs(delta) > 1:
            # Not an error: overlapping jobs make the resume's own arithmetic
            # wrong far more often than ours. Worth seeing in the logs.
            warnings.append(
                f"resume claims {extraction.stated_total_years} years, computed {experience.years}"
            )

    return EnrichedProfile(
        skills=skills,
        total_years=experience.years,
        seniority=infer_seniority(total_years=experience.years, titles=titles),
        domains=domains,
        titles=titles,
        warnings=tuple(warnings),
        stated_years_delta=delta,
    )


def with_level(skill: EnrichedSkill, level: SkillLevel) -> EnrichedSkill:
    """Copy of a skill at a different level, for manual corrections."""
    return replace(skill, level=level)
