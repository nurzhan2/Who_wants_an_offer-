"""The rule-based score: the formula, the gates, and the honesty of the gaps.

A scoring bug never raises. It ranks the wrong job first and looks exactly like
a scoring success, so most of what is asserted here is about the cases that
would be silently wrong: a vacancy with no embedding, one with no salary, one
whose employer left the skills field blank. Each of those is the common case in
the measured corpus, not an edge one.

Numbers quoted in the docstrings come from the live corpus of 643 vacancies.
"""

from decimal import Decimal

import pytest

from app.db.enums import MatchBucket, RemoteType, Seniority
from app.matching.rules import (
    LANGUAGE_GAP_PENALTY,
    STRUCTURAL,
    WEIGHTS,
    ProfileFacts,
    VacancyFacts,
    bucket_for,
    cefr_rank,
    experience_fit,
    have,
    language_verdict,
    logistics_fit,
    normalise_similarity,
    score_vacancy,
    skill_coverage,
)
from app.resume.skills import default_canonicalizer

pytestmark = pytest.mark.unit

#: The measured profile: senior backend, seven years, Almaty, ru native + en B2.
CANDIDATE = ProfileFacts(
    total_years=Decimal("7"),
    seniority=Seniority.SENIOR,
    skills={"python": "strong", "postgresql": "strong", "docker": "strong", "git": "working"},
    languages={"ru": "native", "en": "B2"},
    locations=["Алматы", "Астана"],
    relocation=True,
    remote_pref=RemoteType.FULL,
    salary_min=Decimal("4500"),
    has_embedding=True,
)


def vacancy(**overrides: object) -> VacancyFacts:
    """A vacancy that passes every gate, so a test can break exactly one thing."""
    base: dict[str, object] = {
        "required_skills": {"python": Decimal("1")},
        "min_years": Decimal("6"),
        "city": "Алматы",
        "remote": RemoteType.FULL,
        "similarity": Decimal("0.8"),
    }
    return VacancyFacts(**{**base, **overrides})  # type: ignore[arg-type]


# ── the weights ──────────────────────────────────────────────────────────────


def test_the_documented_weights_are_unchanged() -> None:
    """The formula is docs/MATCHING.md's, not one invented here.

    Renormalisation changes the divisor, never the weights or their ratios, so
    this is the assertion that says the deviation is the one documented and not
    a second one that crept in beside it.
    """
    assert {
        "skill_coverage_required": Decimal("0.35"),
        "skill_coverage_nice": Decimal("0.10"),
        "semantic_similarity": Decimal("0.20"),
        "experience_fit": Decimal("0.15"),
        "domain_fit": Decimal("0.10"),
        "logistics_fit": Decimal("0.10"),
    } == WEIGHTS
    assert sum(WEIGHTS.values()) == Decimal("1.00")


def test_a_perfect_match_can_actually_reach_apply_now() -> None:
    """The bug that renormalisation exists to fix, asserted as a number.

    Two of the six components can never be measured on this data: hh marks
    nothing nice-to-have, and nothing extracts domains. Multiplying them by zero
    and adding them in caps every score in the corpus at 80 — so the document's
    own apply_now band, 85 and up, would be unreachable for every vacancy that
    will ever be scored, and a flawless candidate would read as merely strong.
    """
    perfect = score_vacancy(
        vacancy(
            required_skills={"python": Decimal("1"), "postgresql": Decimal("1")},
            min_years=Decimal("7"),
            similarity=Decimal("1"),
            seniority=Seniority.SENIOR,
        ),
        CANDIDATE,
    )

    assert perfect.final_score == Decimal("100.00")
    assert perfect.bucket == MatchBucket.APPLY_NOW
    assert "domain_fit" not in perfect.counted
    assert "skill_coverage_nice" not in perfect.counted


def test_a_component_with_no_data_source_at_all_neither_helps_nor_hurts() -> None:
    """Structural absences are renormalised away; that is deviation one.

    ``skill_coverage_nice`` and ``domain_fit`` have no data source for any
    vacancy ever, so they are a gap in this pipeline rather than a fact about a
    job. Scored as zeroes they cap everything at 80 and empty the apply_now
    band, which the perfect-match test above asserts from the other side.
    """
    scored = score_vacancy(
        vacancy(
            required_skills={"python": Decimal("1")},
            min_years=Decimal("7"),
            similarity=Decimal("1"),
            seniority=Seniority.SENIOR,
        ),
        CANDIDATE,
    )

    assert scored.final_score == Decimal("100.00")
    assert not STRUCTURAL & set(scored.counted)


def test_a_vacancy_without_a_vector_is_still_scored_on_its_skills() -> None:
    """The brief's requirement, and the exact number it produces.

    128 of 643 rows have no embedding, and the backlog moves, so this
    population changes between runs. Perfect skill coverage with no vector
    scores 75 and lands in ``strong``: not the silent zero the brief rules out,
    and not the same as an identical vacancy that also matched semantically.
    """
    unembedded = score_vacancy(vacancy(similarity=None), CANDIDATE)

    assert unembedded.final_score == Decimal("75.00")
    assert unembedded.bucket == MatchBucket.STRONG
    assert "semantic_similarity" not in unembedded.counted


def test_the_modifiers_alone_cannot_carry_a_vacancy_into_the_shortlist() -> None:
    """The measured failure that gave this rule its three cases.

    Scored the naive way — renormalise every absent component away — a vacancy
    the employer left blank is judged only on «wants no more experience than
    you have» and «is in your city», and both are nearly always true for this
    profile. Measured over 582 vacancies, all 75 that reached 70 were ones with
    no listed skills, and not one vacancy with real skill data reached 70. The
    list was exactly inverted: the top of it was what we knew least about.
    """
    knows_nothing = score_vacancy(
        vacancy(required_skills={}, similarity=None, min_years=Decimal("1")), CANDIDATE
    )
    knows_the_skills = score_vacancy(
        vacancy(required_skills={"python": Decimal("1")}, similarity=None), CANDIDATE
    )

    assert knows_nothing.final_score < Decimal("55"), "must not reach even stretch"
    assert knows_nothing.bucket == MatchBucket.SKIP
    assert knows_the_skills.final_score > knows_nothing.final_score


def test_an_employer_who_states_no_experience_is_not_charged_for_the_silence() -> None:
    """Modifier absences are renormalised away; that is deviation three.

    306 of 643 rows state no experience. Charging them fifteen points for it
    would rank a vacancy by how completely its form was filled in rather than
    by how well it fits.
    """
    stated = score_vacancy(vacancy(min_years=Decimal("6")), CANDIDATE)
    silent = score_vacancy(vacancy(min_years=None), CANDIDATE)

    assert "experience_fit" not in silent.counted
    # Not identical, and the small gap is the honest one: the component that
    # dropped out had scored 1.0, so removing it lowers the mean of what is
    # left. What matters is that the vacancy keeps its band. Charging the
    # silence the way an evidence gap is charged would put it at 76.25 and out
    # of apply_now entirely, which is the failure this case exists to avoid.
    assert silent.bucket == stated.bucket == MatchBucket.APPLY_NOW
    assert stated.final_score - silent.final_score < Decimal("2")


def test_a_vacancy_with_nothing_measurable_scores_zero_rather_than_raising() -> None:
    """The divisor can genuinely be empty, and division is not the answer."""
    nothing = score_vacancy(
        VacancyFacts(), ProfileFacts(locations=[], remote_pref=None, salary_min=None)
    )

    assert nothing.final_score == Decimal("0.00")
    assert nothing.counted == ()


# ── skills ───────────────────────────────────────────────────────────────────


def test_coverage_is_weighted_and_lists_both_sides() -> None:
    """The explanation is the point: which are covered, which are not."""
    coverage, matched, missing = skill_coverage(
        {"python": Decimal("1"), "kafka": Decimal("1")}, {"python": "strong"}
    )

    assert coverage == Decimal("0.5")
    assert [item.canonical_name for item in matched] == ["python"]
    assert [item.canonical_name for item in missing] == ["kafka"]


def test_a_vacancy_that_lists_no_skills_is_not_a_vacancy_the_candidate_fails() -> None:
    """449 of 643 rows are this shape, so it is the majority case, not an edge.

    An employer who listed nothing has not said the candidate lacks anything.
    Scoring it as zero coverage would rank every silent posting below every
    explicit one for a fact about hh's optional field.
    """
    coverage, matched, missing = skill_coverage({}, {"python": "strong"})

    assert coverage is None
    assert matched == [] and missing == []


def test_how_well_the_candidate_knows_a_skill_counts() -> None:
    """The document's level multiplier: basic 0.7, working 0.85, strong 1.0."""
    resolver = default_canonicalizer()

    assert have("python", {"python": "strong"}, canonicalizer=resolver) == (Decimal("1.0"), True)
    assert have("python", {"python": "working"}, canonicalizer=resolver) == (Decimal("0.85"), True)
    assert have("python", {"python": "basic"}, canonicalizer=resolver) == (Decimal("0.7"), True)


def test_a_neighbouring_technology_counts_a_little_and_an_unrelated_one_not_at_all() -> None:
    """Same group, different stack, is the document's 0.25 tier.

    Its middle tier — related technologies at 0.5 to 0.7 — needs a relatedness
    graph that skills_min.yaml says outright it does not carry, so it is absent
    rather than guessed: an invented 0.6 between two technologies would move
    rankings while looking like a measurement.
    """
    resolver = default_canonicalizer()

    credit, holds = have("go", {"python": "strong"}, canonicalizer=resolver)
    assert credit == Decimal("0.25")
    assert not holds, "partial credit is not the candidate having the skill"
    assert have("активные продажи", {"python": "strong"}, canonicalizer=resolver) == (
        Decimal("0"),
        False,
    )


# ── experience ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("required", "candidate", "expected"),
    [
        (Decimal("1"), Decimal("7"), Decimal("0.9")),  # strongly overqualified
        (Decimal("6"), Decimal("7"), Decimal("1.0")),
        (Decimal("7"), Decimal("7"), Decimal("1.0")),
        (Decimal("8"), Decimal("7"), Decimal("0.8")),
        (Decimal("9"), Decimal("7"), Decimal("0.55")),
        (Decimal("11"), Decimal("7"), Decimal("0.25")),
        (Decimal("12"), Decimal("7"), Decimal("0")),
    ],
)
def test_the_experience_ladder_is_the_documented_one(
    required: Decimal, candidate: Decimal, expected: Decimal
) -> None:
    """Straight from docs/MATCHING.md, including that overqualified is a miss."""
    assert experience_fit(required, candidate) == expected


def test_a_vacancy_that_did_not_state_experience_is_not_a_vacancy_that_wants_none() -> None:
    """None is not zero, all the way through the formula.

    306 of 643 rows say nothing about experience. Reading silence as "no
    experience required" would make experience_fit a perfect 1.0 for half the
    corpus on no evidence at all.
    """
    assert experience_fit(None, Decimal("7")) is None
    assert experience_fit(Decimal("3"), None) is None

    silent = score_vacancy(vacancy(min_years=None), CANDIDATE)
    assert "experience_fit" not in silent.counted
    assert silent.experience_gap_years is None


def test_far_too_much_experience_asked_for_is_a_hard_filter() -> None:
    """Beyond four years it is not a stretch, it is a different job."""
    scored = score_vacancy(vacancy(min_years=Decimal("13")), CANDIDATE)

    assert scored.bucket == MatchBucket.FILTERED
    assert scored.filtered_reason is not None
    assert "опыт" in scored.filtered_reason


# ── languages ────────────────────────────────────────────────────────────────


def test_a_language_the_candidate_does_not_speak_is_a_refusal() -> None:
    """Measured: 42 of 294 hh postings ask for Kazakh, which this profile lacks.

    The document separates this from a weak level deliberately. «Требуется
    казахский» with no Kazakh at all is not borderline; leaving it in the list
    at score 60 costs the owner the time to read it.
    """
    verdict = language_verdict([("казахский", "C1")], CANDIDATE.languages)

    assert verdict.refusal == "требуется казахский"
    assert verdict.gaps == 0


def test_a_language_held_below_the_level_asked_for_is_a_penalty_not_a_refusal() -> None:
    """C1 wanted, B2 held: three months of work, and people do apply."""
    verdict = language_verdict([("английский", "C1")], CANDIDATE.languages)

    assert verdict.refusal is None
    assert verdict.gaps == 1
    assert verdict.flags and "английский" in verdict.flags[0]

    scored = score_vacancy(vacancy(language_requirements=[("английский", "C1")]), CANDIDATE)
    assert scored.bucket != MatchBucket.FILTERED
    assert scored.penalties == LANGUAGE_GAP_PENALTY


def test_a_language_held_at_or_above_the_level_costs_nothing() -> None:
    """Native outranks every CEFR level, which is why it is in the ladder."""
    assert language_verdict([("русский", "C2")], CANDIDATE.languages).gaps == 0
    assert language_verdict([("английский", "B1")], CANDIDATE.languages).gaps == 0
    assert cefr_rank("native") is not None
    assert cefr_rank("C2") is not None


def test_a_language_name_we_cannot_read_is_flagged_and_not_refused() -> None:
    """The name table is closed, so an unknown name is our gap, not the job's.

    Refusing on it would filter a vacancy for a word missing from a dictionary
    in this repository — the kind of hard block that looks like a measurement.
    """
    verdict = language_verdict([("клингонский", "C1")], CANDIDATE.languages)

    assert verdict.refusal is None
    assert verdict.flags == ("язык не распознан: клингонский",)


# ── logistics, and the salary rule ───────────────────────────────────────────


def test_a_vacancy_without_a_salary_is_not_a_worse_vacancy() -> None:
    """Five hh postings in six carry no compensation at all.

    Counting silence as a zero would drag the whole corpus down for a fact
    about hh's form rather than about any job — and would make the score depend
    on whether an employer filled in an optional field.
    """
    with_pay = logistics_fit(vacancy(salary_min=Decimal("5000")), CANDIDATE)
    without_pay = logistics_fit(vacancy(salary_min=None), CANDIDATE)

    assert with_pay == without_pay


def test_pay_far_under_the_floor_is_a_soft_fail_not_a_filter() -> None:
    """The document's −15, and the vacancy stays in the list."""
    scored = score_vacancy(vacancy(salary_min=Decimal("1000")), CANDIDATE)

    assert scored.penalties == Decimal("15")
    assert scored.bucket != MatchBucket.FILTERED


def test_the_city_the_candidate_lives_in_beats_one_they_would_move_to() -> None:
    """261 of 294 hh postings are in Almaty, which is where the candidate is."""
    home = logistics_fit(vacancy(city="Алматы", remote=RemoteType.NO), CANDIDATE)
    away = logistics_fit(vacancy(city="Варшава", remote=RemoteType.NO), CANDIDATE)

    assert home is not None and away is not None and home > away


def test_hh_writes_the_same_city_two_ways_and_both_are_home() -> None:
    """«город Алматы» and «Алматы» are one place; the corpus contains both."""
    plain = logistics_fit(vacancy(city="Алматы", remote=RemoteType.NO), CANDIDATE)
    prefixed = logistics_fit(vacancy(city="город Алматы", remote=RemoteType.NO), CANDIDATE)

    assert plain == prefixed


def test_a_candidate_who_will_relocate_is_never_filtered_on_location() -> None:
    """The gate fires only when every escape is closed at once."""
    scored = score_vacancy(vacancy(city="Варшава", remote=RemoteType.NO), CANDIDATE)

    assert scored.bucket != MatchBucket.FILTERED


def test_a_candidate_who_will_not_relocate_is_filtered_on_a_far_office_job() -> None:
    """The other half of the same rule, so the test above is not vacuously true."""
    rooted = ProfileFacts(
        locations=["Алматы"], relocation=False, remote_pref=RemoteType.NO, languages={}
    )

    scored = score_vacancy(vacancy(city="Варшава", remote=RemoteType.NO), rooted)

    assert scored.bucket == MatchBucket.FILTERED
    assert scored.filtered_reason is not None
    assert "локация" in scored.filtered_reason


# ── the rest of stage 0, and the buckets ─────────────────────────────────────


def test_a_posting_closed_for_applicants_is_filtered() -> None:
    """hh keeps such pages up, and applying to one is time spent on nothing."""
    scored = score_vacancy(vacancy(closed_for_applicants=True), CANDIDATE)

    assert scored.bucket == MatchBucket.FILTERED
    assert scored.filtered_reason == "вакансия закрыта для откликов"


@pytest.mark.parametrize(
    ("score", "bucket"),
    [
        (Decimal("100"), MatchBucket.APPLY_NOW),
        (Decimal("85"), MatchBucket.APPLY_NOW),
        (Decimal("84.99"), MatchBucket.STRONG),
        (Decimal("70"), MatchBucket.STRONG),
        (Decimal("69.99"), MatchBucket.STRETCH),
        (Decimal("55"), MatchBucket.STRETCH),
        (Decimal("54.99"), MatchBucket.SKIP),
        (Decimal("0"), MatchBucket.SKIP),
    ],
)
def test_the_bucket_boundaries_are_the_documented_ones(score: Decimal, bucket: MatchBucket) -> None:
    """From the document's table, boundaries included."""
    assert bucket_for(score) == bucket


def test_cosine_is_mapped_onto_the_unit_interval() -> None:
    """pgvector answers in [-1;1]; the document scores on [0;1]."""
    assert normalise_similarity(1.0) == Decimal("1")
    assert normalise_similarity(0.0) == Decimal("0.5")
    assert normalise_similarity(-1.0) == Decimal("0")
    assert normalise_similarity(None) is None


def test_a_filtered_vacancy_still_carries_its_score_and_its_reason() -> None:
    """The document says the row is saved with bucket=filtered, not dropped.

    Hiding it in the dashboard is a display decision; deleting the evidence
    would mean nobody could ever ask why a job stopped appearing.
    """
    scored = score_vacancy(vacancy(min_years=Decimal("20")), CANDIDATE)

    assert scored.bucket == MatchBucket.FILTERED
    assert scored.final_score > 0
    assert scored.filtered_reason is not None


def test_a_neighbouring_skill_is_never_reported_as_one_the_candidate_has() -> None:
    """Measured on the live corpus, and the reason `held` exists.

    The first pass ranked «Intern Developer» top and reported it as matching
    `javascript`, and «UX/UI Designer» as matching `figma`. This candidate has
    neither. Both came from the same-group tier: JavaScript and Python are both
    in the `language` group, so knowing one earned 0.25 towards the other — and
    the entry then landed in the matched list.

    That number is defensible; presenting it as «совпадает: javascript» is not.
    The cover-letter generator builds the letter around the matched list, so it
    would have written a claim to a skill the candidate never had.
    """
    scored = score_vacancy(
        vacancy(required_skills={"javascript": Decimal("1")}, similarity=None), CANDIDATE
    )

    assert [item.canonical_name for item in scored.matched] == []
    assert [item.canonical_name for item in scored.missing] == ["javascript"]
    # Still worth its partial credit in the number, just not in the sentence.
    assert scored.missing[0].coverage == Decimal("0.25")
    assert not scored.missing[0].held
