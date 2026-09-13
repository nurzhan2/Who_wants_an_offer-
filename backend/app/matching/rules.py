"""The rule-based score of ``docs/MATCHING.md``, and only what the data supports.

Pure functions over values already read from the database. Nothing here opens a
session, calls a model or touches the network, so the whole formula is testable
without any of the three — which matters, because a scoring bug is silent: it
does not raise, it just ranks the wrong job first.

**The deviation from the document, and why it is not an invention.**

``docs/MATCHING.md`` weights six components that sum to 1.0 and assumes every
one of them can be measured. On this data they cannot, and the six fall into
three groups that have to be treated differently. Treating them alike was tried
first and measured; it inverted the ranking, and that measurement is recorded
here because it is the reason this rule has three cases rather than one.

*Structural absences* — ``skill_coverage_nice`` (0.10) and ``domain_fit``
(0.10). Neither can ever be anything but zero here: hh's ``keySkills`` carries
no required/nice split, and nothing extracts domains — no column, no payload
field, no code. Their weight is **renormalised away**. Left in as zeroes they
cap every score at 80, which puts the document's own ``apply_now`` band (85 and
up) out of reach for every vacancy that will ever be scored, and a flawless
candidate reads as merely ``strong``.

*Evidence* — ``skill_coverage_required`` (0.35) and ``semantic_similarity``
(0.20). These are the only two components that say anything about whether the
candidate can do the job, so when one is missing its weight **stays in the
divisor** and the component scores zero. Renormalising it away instead promotes
the modifiers to fill the gap, and the measured result was an exactly inverted
list: of 582 vacancies, all 75 that scored 70 or better were ones where the
employer had listed no skills at all, and not one vacancy with real skill data
reached 70. Two postings scored 80 on nothing but «wants no more experience
than you have» and «is in your city».

*Modifiers* — ``experience_fit`` (0.15) and ``logistics_fit`` (0.10). These say
whether a job is reachable, not whether it fits. When the employer states
neither a requirement nor a place there is nothing to check, and that is their
silence rather than the candidate's shortcoming, so the weight is
**renormalised away**. 306 of 643 rows state no experience at all; charging
them fifteen points for it would rank a vacancy by how completely its form was
filled in.

    rule_score = Σ(wᵢ·cᵢ) / Σ(wᵢ)
        over the evidence always, and the modifiers that could be measured

The document's weights and their ratios are untouched — skills still matter
three and a half times as much as logistics. What changes is only which of them
land in the divisor, and each case has a reason a reader can check.

This is also what the brief asks for in as many words. A vacancy with no
embedding is still scored on its skills instead of dropping to a silent zero,
which is what «получать score по навыкам с честной пометкой» has to mean. Its
ceiling is 75, approached rather than reached since
:data:`UNSTATED_REQUIREMENT`: one held requirement scores 53, ten score 71.

:attr:`Score.counted` records which components were in the divisor, so the
explanation can say so instead of leaving a reader to wonder.

**What is deliberately not here.** The number of responses a posting has
received is not mixed into the score: the owner has to be able to see what
influenced what, and it is not in the stored data anyway (measured: zero of 294
hh payloads carry ``responsesCount``). A missing salary is not a fault of the
vacancy — five hh postings in six have none — so it drops out of logistics
rather than scoring zero.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Final

from app.db.enums import MatchBucket, RemoteType, RequirementSource, Seniority, SkillLevel
from app.resume.skills import SkillCanonicalizer, default_canonicalizer

#: Two decimals, the scale of every score column.
CENTS: Final[Decimal] = Decimal("0.01")

#: From ``docs/MATCHING.md``. Not renormalised here — that happens per vacancy,
#: over whichever of these could be measured for it.
WEIGHTS: Final[dict[str, Decimal]] = {
    "skill_coverage_required": Decimal("0.35"),
    "skill_coverage_nice": Decimal("0.10"),
    "semantic_similarity": Decimal("0.20"),
    "experience_fit": Decimal("0.15"),
    "domain_fit": Decimal("0.10"),
    "logistics_fit": Decimal("0.10"),
}

#: One requirement the employer did not write down, added to the divisor of
#: skill coverage, as a count of requirements. What one of them weighs is
#: :class:`UnstatedRequirement`'s question. See :func:`skill_coverage` for the
#: measurement that produced it and the consequence it carries.
UNSTATED_REQUIREMENT: Final[Decimal] = Decimal("1.0")


class UnstatedRequirement(StrEnum):
    """What the one requirement the employer did not write down weighs.

    Both stay selectable — ``scripts/run_matching.py --unstated`` — because the
    choice between them is to be settled by measuring the corpus, not by
    argument.
    """

    #: :data:`UNSTATED_REQUIREMENT` times the mean weight of this vacancy's own
    #: requirements. The unwritten requirement is as well evidenced as what we
    #: have about this vacancy, so a list read out of the description (0.6
    #: each) and a named one (1.0 each) are judged by the same rule.
    MEAN_WEIGHT = "mean-weight"
    #: :data:`UNSTATED_REQUIREMENT` flat, in ``vacancy_skill.weight`` units.
    #: Judges a list read out of the description more strictly than a named
    #: one — 0.375 against 0.5 for one held requirement — which is a penalty
    #: for leaving an optional field empty.
    FIXED = "fixed"


#: The rule a scoring pass uses unless told otherwise.
DEFAULT_UNSTATED: Final[UnstatedRequirement] = UnstatedRequirement.MEAN_WEIGHT

#: The two components that say whether the candidate can do the job. Their
#: weight stays in the divisor even when they cannot be measured, so that a
#: vacancy nobody could assess never outranks one that was assessed and fits.
#: See the module docstring for the measurement that made this necessary.
EVIDENCE: Final[frozenset[str]] = frozenset({"skill_coverage_required", "semantic_similarity"})

#: Components with no data source at all, for any vacancy, ever. Renormalised
#: away rather than scored zero: they are a gap in this pipeline, not a fact
#: about a job, and as zeroes they put ``apply_now`` out of reach for everyone.
STRUCTURAL: Final[frozenset[str]] = frozenset({"skill_coverage_nice", "domain_fit"})

#: Bucket floors, highest first. From the document's table.
BUCKETS: Final[tuple[tuple[Decimal, MatchBucket], ...]] = (
    (Decimal("85"), MatchBucket.APPLY_NOW),
    (Decimal("70"), MatchBucket.STRONG),
    (Decimal("55"), MatchBucket.STRETCH),
)


class Formula(StrEnum):
    """Which number a scoring pass computes.

    Both stay selectable — ``scripts/run_matching.py --formula`` — so that a
    before-and-after comparison stays reproducible on the same corpus.
    """

    #: The title against the profile's headline, then the description against
    #: the profile. Skills, experience and logistics are still computed and
    #: stored for the explanation, and do not move the number.
    TITLE = "title"
    #: The six weighted components of :data:`WEIGHTS`, renormalised.
    COMPONENTS = "components"


#: The formula a scoring pass uses unless told otherwise.
DEFAULT_FORMULA: Final[Formula] = Formula.TITLE

#: :attr:`Formula.TITLE`'s two signals. Fixed at 0.7 / 0.3 before the corpus
#: was measured (13 Sep 2026) and not tuned after: the title is the main
#: signal, the description the second one. Both are evidence in the sense of
#: :data:`EVIDENCE` — a missing vector keeps its weight and scores zero.
TITLE_WEIGHTS: Final[dict[str, Decimal]] = {
    "title_similarity": Decimal("0.7"),
    "semantic_similarity": Decimal("0.3"),
}

#: :attr:`Formula.TITLE`'s bucket floors, from the measured distribution rather
#: than carried over from :data:`BUCKETS`. On the live corpus (1355 unfiltered
#: vacancies, 13 Sep 2026) the score spans 64.45-86.87 with its quartiles at
#: 70.74 / 72.48 / 74.99, so the old floors would call three vacancies in four
#: a strong match. 75 is the upper quartile, 78 leaves 78 vacancies (about the
#: top 6%), 80 leaves 24 (about the top 2%).
TITLE_BUCKETS: Final[tuple[tuple[Decimal, MatchBucket], ...]] = (
    (Decimal("80"), MatchBucket.APPLY_NOW),
    (Decimal("78"), MatchBucket.STRONG),
    (Decimal("75"), MatchBucket.STRETCH),
)

#: How well a claimed skill counts, by how well the candidate claims to know it.
LEVEL_MULTIPLIER: Final[dict[str, Decimal]] = {
    SkillLevel.BASIC: Decimal("0.7"),
    SkillLevel.WORKING: Decimal("0.85"),
    SkillLevel.STRONG: Decimal("1.0"),
    SkillLevel.EXPERT: Decimal("1.0"),
}

#: Same group, different stack — «java ↔ python for backend». The document's
#: middle tier (related technology, 0.5 to 0.7) needs a relatedness graph that
#: ``skills_min.yaml`` says outright it does not carry, so it is absent rather
#: than guessed: a made-up 0.6 between two technologies would move rankings
#: while looking like a measurement.
SAME_GROUP: Final[Decimal] = Decimal("0.25")

#: CEFR, plus the level a native speaker has. Compared by position.
CEFR: Final[tuple[str, ...]] = ("A1", "A2", "B1", "B2", "C1", "C2", "NATIVE")

#: hh states language requirements by Russian name; a profile states them by
#: ISO code. Closed and small on purpose — an unknown language is reported as
#: unknown rather than folded onto a code that might be wrong, because this
#: table decides a hard filter.
LANGUAGE_CODES: Final[dict[str, str]] = {
    "русский": "ru",
    "английский": "en",
    "казахский": "kk",
    "немецкий": "de",
    "французский": "fr",
    "испанский": "es",
    "итальянский": "it",
    "китайский": "zh",
    "японский": "ja",
    "корейский": "ko",
    "турецкий": "tr",
    "арабский": "ar",
    "польский": "pl",
    "украинский": "uk",
    "кыргызский": "ky",
    "узбекский": "uz",
}

#: Penalty for holding a language below the level asked for. The document's
#: ``language_gap_penalty``: subtracted from the final score, not from a
#: component, because it is a fact about the candidate rather than about fit.
LANGUAGE_GAP_PENALTY: Final[Decimal] = Decimal("8")

#: Subtracted when the pay on offer is more than 30% under the candidate's
#: floor. The document's soft-fail.
SALARY_SOFT_FAIL_PENALTY: Final[Decimal] = Decimal("15")
SALARY_SHORTFALL: Final[Decimal] = Decimal("0.7")

#: Beyond this the vacancy is not a stretch, it is a different job.
MAX_EXPERIENCE_GAP: Final[Decimal] = Decimal("4")


@dataclass(frozen=True, slots=True)
class ProfileFacts:
    """What scoring needs to know about the candidate, already read out."""

    total_years: Decimal | None = None
    seniority: Seniority | None = None
    #: canonical name -> level, as stored in ``profile_skill``.
    skills: Mapping[str, str] = field(default_factory=dict)
    #: ISO code -> CEFR level or "native".
    languages: Mapping[str, str] = field(default_factory=dict)
    locations: Sequence[str] = ()
    relocation: bool = False
    remote_pref: RemoteType | None = None
    salary_min: Decimal | None = None
    has_embedding: bool = False


@dataclass(frozen=True, slots=True)
class VacancyFacts:
    """What scoring needs to know about one vacancy, already read out."""

    #: canonical name -> weight, from ``vacancy_skill``.
    required_skills: Mapping[str, Decimal] = field(default_factory=dict)
    #: canonical name -> who says it is a requirement, from the same rows. A
    #: name missing from here is read as the employer's own, which is what
    #: every row in that table was before ``0014_requirement_source`` and what
    #: the column's own default says.
    requirement_sources: Mapping[str, RequirementSource] = field(default_factory=dict)
    min_years: Decimal | None = None
    seniority: Seniority | None = None
    city: str | None = None
    remote: RemoteType = RemoteType.NO
    salary_min: Decimal | None = None
    employment_type: str | None = None
    #: ``(name, level)`` pairs as the connector rendered them, lower-cased name.
    language_requirements: Sequence[tuple[str, str]] = ()
    closed_for_applicants: bool = False
    #: Cosine similarity already normalised into 0..1, or None when either side
    #: has no vector.
    similarity: Decimal | None = None
    #: The same, between this vacancy's title and the profile's headline.
    title_similarity: Decimal | None = None


@dataclass(frozen=True, slots=True)
class SkillMatch:
    """One required skill and how far the candidate covers it."""

    canonical_name: str
    weight: Decimal
    coverage: Decimal
    #: Whether the candidate actually claims this skill, as opposed to earning
    #: partial credit for a neighbour of it. The difference decides which list
    #: the entry goes in, and that matters more than the number: the cover
    #: letter is built around the matched list, and one claiming JavaScript
    #: because the candidate knows Python is an invented qualification.
    held: bool = True
    #: Whether the employer named this requirement or we read it out of their
    #: description. Carried through scoring so that the card, the queue and the
    #: ATS report can show a third state rather than presenting a reading of
    #: prose as a stated requirement.
    source: RequirementSource = RequirementSource.EMPLOYER_FIELD


@dataclass(slots=True)
class Score:
    """A score with the reasons for it. Never a bare number, by design."""

    rule_score: Decimal = Decimal("0")
    final_score: Decimal = Decimal("0")
    bucket: MatchBucket = MatchBucket.SKIP
    components: dict[str, Decimal] = field(default_factory=dict)
    #: Components that were actually measurable and so are in the divisor.
    counted: tuple[str, ...] = ()
    matched: list[SkillMatch] = field(default_factory=list)
    missing: list[SkillMatch] = field(default_factory=list)
    red_flags: list[str] = field(default_factory=list)
    penalties: Decimal = Decimal("0")
    experience_gap_years: Decimal | None = None
    #: Why the vacancy was filtered, in Russian, or None if it was not.
    filtered_reason: str | None = None
    similarity: Decimal | None = None
    title_similarity: Decimal | None = None
    formula: Formula = DEFAULT_FORMULA


def cefr_rank(level: str) -> int | None:
    """Position of a CEFR level, or None if it is not one we know."""
    normalised = level.strip().upper()
    return CEFR.index(normalised) if normalised in CEFR else None


def language_code(name: str) -> str | None:
    """The ISO code hh's Russian language name means, or None when unknown."""
    return LANGUAGE_CODES.get(name.strip().casefold())


def have(
    required: str, profile_skills: Mapping[str, str], *, canonicalizer: SkillCanonicalizer
) -> tuple[Decimal, bool]:
    """How much of one required skill the candidate has, and whether they hold it.

    Exact after canonicalisation is 1.0. The document's separate 0.95 for an
    alias cannot be reached and is not missing: the canonicaliser has already
    folded every alias onto its canonical name by the time either side is
    stored, so an alias match *is* an exact match, and paying it 0.95 would
    penalise a candidate for the spelling they happened to use.

    The second value is the one that keeps the explanation honest. Partial
    credit for a skill in the same group is a real signal about fit — knowing
    Python says something about a Go job — but it is not the candidate having
    the skill, and only the first may be shown as «совпадает».
    """
    if required in profile_skills:
        return LEVEL_MULTIPLIER.get(profile_skills[required], Decimal("1.0")), True

    group = canonicalizer.group_of(required)
    if group is None:
        return Decimal("0"), False
    for held, level in profile_skills.items():
        if canonicalizer.group_of(held) == group:
            return SAME_GROUP * LEVEL_MULTIPLIER.get(level, Decimal("1.0")), False
    return Decimal("0"), False


def skill_coverage(
    required: Mapping[str, Decimal],
    profile_skills: Mapping[str, str],
    *,
    sources: Mapping[str, RequirementSource] | None = None,
    canonicalizer: SkillCanonicalizer | None = None,
    unstated: UnstatedRequirement = DEFAULT_UNSTATED,
) -> tuple[Decimal | None, list[SkillMatch], list[SkillMatch]]:
    """Weighted coverage of a vacancy's requirements, and the two lists behind it.

    ``None`` when the vacancy states no requirements — which was 449 of this
    corpus's 643 rows before descriptions were read, and is fewer now: since
    ``0014_requirement_source`` a posting that left hh's field empty can still
    have requirements, read out of its own text and weighed at 0.60. What has
    not changed is the meaning of ``None``: an employer who said nothing
    anywhere has not said the candidate lacks anything, and scoring that as a
    total miss would rank every silent posting below every explicit one
    regardless of fit.
    """
    if not required:
        return None, [], []

    resolver = canonicalizer or default_canonicalizer()
    provenance = sources or {}
    matched: list[SkillMatch] = []
    missing: list[SkillMatch] = []
    earned = Decimal("0")
    total = Decimal("0")
    for name, weight in required.items():
        coverage, holds = have(name, profile_skills, canonicalizer=resolver)
        entry = SkillMatch(
            canonical_name=name,
            weight=weight,
            coverage=coverage,
            held=holds,
            source=provenance.get(name, RequirementSource.EMPLOYER_FIELD),
        )
        # Split on whether the candidate HOLDS it, not on whether it scored.
        # A skill earning 0.25 for being a neighbour still counts towards the
        # number and still belongs in the missing list, because that is what it
        # is: a requirement the candidate does not meet.
        (matched if holds else missing).append(entry)
        earned += weight * coverage
        total += weight
    if total == 0:
        return None, matched, missing
    # One requirement the employer did not write down, in the same units as the
    # weights. Measured 9 Sep 2026 on the live corpus: five vacancies reached
    # 87.5-88.6 and topped the queue on a single matched requirement each, read
    # out of their own description text — the ratio cannot tell one-of-one from
    # ten-of-ten. hh's key-skills field is optional and 893 of 1958 employers
    # left it empty, so the list is what somebody found time to type, not what
    # the job needs. The allowance is smooth over the whole length (1 stated ->
    # 0.50, 2 -> 0.67, 5 -> 0.83, 10 -> 0.91) rather than a threshold, which
    # would need a number nobody has measured and would put a cliff between two
    # postings that differ by one line of an advert.
    # Consequence to state out loud: 100 is no longer reachable. The ceiling is
    # how much the employer said — 78 at one requirement, 96 at ten.
    # What the unwritten requirement weighs is the rule's business; see
    # ``UnstatedRequirement``. On a list of named requirements the two agree.
    allowance = unstated_allowance(total, len(required), unstated)
    return earned / (total + allowance), matched, missing


def unstated_allowance(total: Decimal, count: int, rule: UnstatedRequirement) -> Decimal:
    """The weight of the one requirement the employer did not write down.

    ``total`` and ``count`` are the summed weight and number of the requirements
    that were written down; ``count`` is never zero here, because a vacancy with
    no requirements has no coverage to divide.
    """
    if rule is UnstatedRequirement.FIXED:
        return UNSTATED_REQUIREMENT
    return UNSTATED_REQUIREMENT * total / Decimal(count)


def experience_fit(required: Decimal | None, candidate: Decimal | None) -> Decimal | None:
    """The document's ladder over ``gap = required - candidate``.

    ``None`` when either side is silent. Most postings are: ``min_years`` is set
    on 337 of 643 rows, and a vacancy that did not state a requirement has not
    stated an unmet one.
    """
    if required is None or candidate is None:
        return None
    gap = required - candidate
    if gap <= Decimal("-2"):
        return Decimal("0.9")  # strongly overqualified is its own kind of miss
    if gap <= 0:
        return Decimal("1.0")
    if gap <= 1:
        return Decimal("0.8")
    if gap <= 2:
        return Decimal("0.55")
    if gap <= MAX_EXPERIENCE_GAP:
        return Decimal("0.25")
    return Decimal("0")


def seniority_multiplier(vacancy: Seniority | None, candidate: Seniority | None) -> Decimal:
    """0.8–1.0 on how far apart the two grades are, 1.0 when either is unknown."""
    if vacancy is None or candidate is None:
        return Decimal("1.0")
    order = list(Seniority)
    distance = abs(order.index(vacancy) - order.index(candidate))
    return max(Decimal("0.8"), Decimal("1.0") - Decimal("0.1") * distance)


def logistics_fit(vacancy: VacancyFacts, profile: ProfileFacts) -> Decimal | None:
    """Format, city and pay, each 0/0.5/1, averaged over the ones that are known.

    A sub-point with no data is left out rather than scored zero. That is what
    keeps «зарплата не указана» from reading as «зарплата не подходит»: five hh
    postings in six carry no compensation at all, and counting that as a miss
    would drag the whole corpus down for a fact about hh's form rather than
    about any job.
    """
    points: list[Decimal] = []

    if profile.remote_pref is not None:
        points.append(_format_fit(vacancy.remote, profile.remote_pref))

    if vacancy.city and profile.locations:
        wanted = {place.strip().casefold() for place in profile.locations}
        city = vacancy.city.strip().casefold()
        # «город Алматы» and «Алматы» are one city; hh writes both.
        hit = any(place in city or city in place for place in wanted)
        near = Decimal("0.5") if profile.relocation else Decimal("0")
        points.append(Decimal("1") if hit else near)

    if vacancy.salary_min is not None and profile.salary_min is not None:
        if vacancy.salary_min >= profile.salary_min:
            points.append(Decimal("1"))
        elif vacancy.salary_min >= profile.salary_min * SALARY_SHORTFALL:
            points.append(Decimal("0.5"))
        else:
            points.append(Decimal("0"))

    if not points:
        return None
    return sum(points, Decimal("0")) / Decimal(len(points))


def _format_fit(offered: RemoteType, preferred: RemoteType) -> Decimal:
    """Whether the work format on offer is one the candidate would take."""
    if offered == preferred:
        return Decimal("1")
    rank = {RemoteType.NO: 0, RemoteType.HYBRID: 1, RemoteType.FULL: 2}
    return Decimal("0.5") if abs(rank[offered] - rank[preferred]) == 1 else Decimal("0")


@dataclass(frozen=True, slots=True)
class LanguageVerdict:
    """Stage 0's answer about languages: refuse, flag, or neither."""

    refusal: str | None = None
    flags: tuple[str, ...] = ()
    #: How many languages are held but below the level asked for. Counted
    #: rather than re-derived from the flag text: a penalty that depends on the
    #: wording of a message is a penalty that changes when somebody edits it.
    gaps: int = 0


def language_verdict(
    requirements: Sequence[tuple[str, str]], spoken: Mapping[str, str]
) -> LanguageVerdict:
    """Stage 0's language gate: a refusal, some flags, or neither.

    Two outcomes rather than one, because they are different situations. A
    language the candidate does not have at any level is a refusal — «требуется
    казахский» is not a borderline case, and leaving it in the list at score 60
    costs a person the time to read it. A language they have but weaker is a
    flag and a penalty: C1 asked, B2 held, three months of work.

    Measured here: 80 of 294 hh postings state a requirement, and 42 of those
    ask for Kazakh, which this profile does not claim at all.
    """
    flags: list[str] = []
    gaps = 0
    for name, level in requirements:
        code = language_code(name)
        if code is None:
            # An unknown language name must not become a refusal: the table is
            # closed, and refusing on a name we cannot read would filter a
            # vacancy for our own gap.
            flags.append(f"язык не распознан: {name}")
            continue
        held = spoken.get(code)
        if held is None:
            return LanguageVerdict(refusal=f"требуется {name}", flags=tuple(flags), gaps=gaps)
        wanted, has = cefr_rank(level), cefr_rank(held)
        if wanted is not None and has is not None and has < wanted:
            flags.append(f"{name}: требуется {level.upper()}, заявлен {held.upper()}")
            gaps += 1
    return LanguageVerdict(flags=tuple(flags), gaps=gaps)


def hard_filters(
    vacancy: VacancyFacts, profile: ProfileFacts
) -> tuple[str | None, LanguageVerdict]:
    """Stage 0. Returns the reason to filter, or None, plus any flags raised.

    Two of the document's filters are absent because the data is not there, and
    saying so is better than a rule that compares the wrong things:

    * **Age.** ``published_at`` is set on 55 of 643 rows — 16 of 294 on hh — so
      a ``max_age_days`` cut would filter almost the whole corpus for silence
      rather than for staleness. The crawl walks the sitemap newest-first, so
      freshness is already handled where it can be measured honestly.
    * **Company blacklist and title stop-words.** Nothing configures either.
    """
    if vacancy.closed_for_applicants:
        return "вакансия закрыта для откликов", LanguageVerdict()

    languages = language_verdict(vacancy.language_requirements, profile.languages)
    if languages.refusal is not None:
        return languages.refusal, languages

    if (
        vacancy.min_years is not None
        and profile.total_years is not None
        and vacancy.min_years - profile.total_years > MAX_EXPERIENCE_GAP
    ):
        return (
            f"опыт: требуется {vacancy.min_years:g}, есть {profile.total_years:g}",
            languages,
        )

    if _location_refuses(vacancy, profile):
        return f"локация не совпадает: {vacancy.city}", languages

    return None, languages


def _location_refuses(vacancy: VacancyFacts, profile: ProfileFacts) -> bool:
    """Whether the city rules the vacancy out entirely.

    Only when everything says no at once: the city is stated and is not one of
    the candidate's, the job is not remote, and the candidate will not move.
    Any one of those being false makes the vacancy reachable.
    """
    if not vacancy.city or not profile.locations:
        return False
    if vacancy.remote == RemoteType.FULL or profile.relocation:
        return False
    city = vacancy.city.strip().casefold()
    return not any(
        place.strip().casefold() in city or city in place.strip().casefold()
        for place in profile.locations
    )


def score_vacancy(
    vacancy: VacancyFacts,
    profile: ProfileFacts,
    *,
    canonicalizer: SkillCanonicalizer | None = None,
    unstated: UnstatedRequirement = DEFAULT_UNSTATED,
    formula: Formula = DEFAULT_FORMULA,
) -> Score:
    """One vacancy against one profile: the number, and every reason for it.

    Under either formula the hard filters decide the bucket first, and the
    skill breakdown is computed and returned in full: the card, the ATS report
    and the letter generator read it whichever number was computed.
    """
    result = Score(
        similarity=vacancy.similarity,
        title_similarity=vacancy.title_similarity,
        formula=formula,
    )

    reason, languages = hard_filters(vacancy, profile)
    result.red_flags.extend(languages.flags)

    coverage, matched, missing = skill_coverage(
        vacancy.required_skills,
        profile.skills,
        sources=vacancy.requirement_sources,
        canonicalizer=canonicalizer,
        unstated=unstated,
    )
    result.matched = matched
    result.missing = missing

    fit = experience_fit(vacancy.min_years, profile.total_years)
    if fit is not None:
        fit *= seniority_multiplier(vacancy.seniority, profile.seniority)
    if vacancy.min_years is not None and profile.total_years is not None:
        result.experience_gap_years = vacancy.min_years - profile.total_years

    measured: dict[str, Decimal | None] = {
        "skill_coverage_required": coverage,
        # Always absent, never zero: hh marks nothing as nice-to-have, so there
        # is no such thing here to cover or to miss.
        "skill_coverage_nice": None,
        "semantic_similarity": vacancy.similarity,
        "experience_fit": fit,
        # No extractor, no column, no payload field. See the module docstring.
        "domain_fit": None,
        "logistics_fit": logistics_fit(vacancy, profile),
    }

    shown = {**measured, "title_similarity": vacancy.title_similarity}
    result.components = {
        name: (value * 100).quantize(CENTS, rounding=ROUND_HALF_UP)
        for name, value in shown.items()
        if value is not None
    }

    if formula is Formula.TITLE:
        signals = {name: shown[name] for name in TITLE_WEIGHTS}
        result.rule_score = _title_weighted(signals)
        result.counted = tuple(name for name, value in signals.items() if value is not None)
        # A filter answers "may I apply", a score "how well does it fit", and
        # this formula keeps the two apart: what used to be subtracted is said
        # on the card instead.
        result.red_flags.extend(_soft_fail_flags(vacancy, profile))
        floors = TITLE_BUCKETS
    else:
        result.rule_score = _weighted(measured)
        result.counted = tuple(name for name, value in measured.items() if value is not None)
        result.penalties = _penalties(vacancy, profile, languages)
        floors = BUCKETS

    result.final_score = max(
        Decimal("0"), min(Decimal("100"), result.rule_score - result.penalties)
    ).quantize(CENTS, rounding=ROUND_HALF_UP)
    result.rule_score = result.rule_score.quantize(CENTS, rounding=ROUND_HALF_UP)

    if reason is not None:
        result.filtered_reason = reason
        result.bucket = MatchBucket.FILTERED
    else:
        result.bucket = bucket_for(result.final_score, floors)
    return result


def _soft_fail_flags(vacancy: VacancyFacts, profile: ProfileFacts) -> list[str]:
    """The salary soft-fail as a flag, for the formula that does not subtract it.

    The language gap needs nothing here: :func:`language_verdict` already flags
    it under both formulas.
    """
    if (
        vacancy.salary_min is not None
        and profile.salary_min is not None
        and vacancy.salary_min < profile.salary_min * SALARY_SHORTFALL
    ):
        return [
            f"зарплата ниже минимума более чем на 30%: {vacancy.salary_min:g} "
            f"при минимуме {profile.salary_min:g}"
        ]
    return []


def _title_weighted(signals: Mapping[str, Decimal | None]) -> Decimal:
    """:attr:`Formula.TITLE`'s number: both signals are evidence.

    A missing vector keeps its weight in the divisor and scores zero, for the
    reason :data:`EVIDENCE` gives: a vacancy nobody could compare must not
    outrank one that was compared and fits.
    """
    earned = sum(
        (TITLE_WEIGHTS[name] * value for name, value in signals.items() if value is not None),
        Decimal("0"),
    )
    return earned / sum(TITLE_WEIGHTS.values(), Decimal("0")) * 100


def _penalties(vacancy: VacancyFacts, profile: ProfileFacts, languages: LanguageVerdict) -> Decimal:
    """Soft-fails, subtracted from the final score where the document says so."""
    total = LANGUAGE_GAP_PENALTY * languages.gaps
    if (
        vacancy.salary_min is not None
        and profile.salary_min is not None
        and vacancy.salary_min < profile.salary_min * SALARY_SHORTFALL
    ):
        total += SALARY_SOFT_FAIL_PENALTY
    return total


def _weighted(measured: Mapping[str, Decimal | None]) -> Decimal:
    """The document's formula, over a divisor that depends on why a value is missing.

    Three cases, and the module docstring gives the measurement behind each:
    a structural absence is renormalised away, an evidence absence keeps its
    weight and scores zero, a modifier absence is renormalised away.
    """
    earned = Decimal("0")
    divisor = Decimal("0")
    for name, value in measured.items():
        if name in STRUCTURAL:
            continue
        weight = WEIGHTS[name]
        if value is None:
            # An evidence gap is charged; a modifier the employer did not state
            # is not, because that is their silence and not the candidate's.
            if name in EVIDENCE:
                divisor += weight
            continue
        earned += weight * value
        divisor += weight
    if divisor == 0:
        return Decimal("0")
    return (earned / divisor) * 100


def bucket_for(
    score: Decimal, floors: Sequence[tuple[Decimal, MatchBucket]] = BUCKETS
) -> MatchBucket:
    """Which bucket a final score lands in, on the given formula's floors."""
    for floor, bucket in floors:
        if score >= floor:
            return bucket
    return MatchBucket.SKIP


def normalise_similarity(cosine: float | Decimal | None) -> Decimal | None:
    """Cosine in [-1;1] onto [0;1], as the document prescribes."""
    if cosine is None:
        return None
    value = (Decimal(str(cosine)) + Decimal("1")) / Decimal("2")
    return max(Decimal("0"), min(Decimal("1"), value))


def as_names(entries: Iterable[SkillMatch]) -> list[str]:
    """Just the names, for a report or a letter."""
    return [entry.canonical_name for entry in entries]
