"""The title formula: what moves the number, what only explains it, what filters.

The formula exists because of one measurement. On 9 Sep 2026 five networking and
telecom postings topped the queue at 87.5-88.6 against a «Python Developer —
Backend» profile, each on a single matched skill, while the vacancy title — which
alone would have turned all five down — hardly took part in the score. So most of
what is asserted here is about the two properties that failure lacked: the title
decides, and a skill breakdown explains without deciding.

No database and no model: similarities arrive already normalised, exactly as the
scorer hands them over.
"""

import importlib.util
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import pytest

from app.core.config import Settings
from app.db.enums import MatchBucket, RemoteType, Seniority
from app.db.repositories.vacancy import EmbeddedVacancy, TitleCandidate
from app.matching.embeddings import EmbeddingsUnavailableError
from app.matching.rules import (
    BUCKETS,
    DEFAULT_FORMULA,
    TITLE_BUCKETS,
    TITLE_WEIGHTS,
    Formula,
    ProfileFacts,
    VacancyFacts,
    bucket_for,
    normalise_similarity,
    score_vacancy,
)
from app.matching.scorer import ScoringOutcome
from app.pipeline import embedding as step

pytestmark = pytest.mark.unit

CANDIDATE = ProfileFacts(
    total_years=Decimal("7"),
    seniority=Seniority.SENIOR,
    skills={"python": "strong", "postgresql": "strong", "linux": "strong"},
    languages={"ru": "native", "en": "B2"},
    locations=["Алматы"],
    relocation=False,
    remote_pref=RemoteType.FULL,
    salary_min=Decimal("4500"),
    has_embedding=True,
)


def vacancy(**overrides: object) -> VacancyFacts:
    """A vacancy that passes every gate, so a test can change exactly one thing."""
    base: dict[str, object] = {
        "required_skills": {"python": Decimal("1")},
        "city": "Алматы",
        "remote": RemoteType.FULL,
        "similarity": Decimal("0.8"),
        "title_similarity": Decimal("0.9"),
    }
    return VacancyFacts(**{**base, **overrides})  # type: ignore[arg-type]


# ── the number ───────────────────────────────────────────────────────────────


def test_the_title_formula_is_the_default() -> None:
    """The old formula stays selectable; it is no longer what a pass computes."""
    assert DEFAULT_FORMULA is Formula.TITLE
    assert {formula.value for formula in Formula} == {"title", "components"}


def test_the_weights_are_the_ones_fixed_before_the_measurement() -> None:
    """0.7 / 0.3 was chosen before the corpus was measured and not tuned after."""
    assert {
        "title_similarity": Decimal("0.7"),
        "semantic_similarity": Decimal("0.3"),
    } == TITLE_WEIGHTS


def test_the_score_is_the_weighted_title_and_description() -> None:
    """0.7 x 0.9 + 0.3 x 0.8 = 0.87."""
    score = score_vacancy(vacancy(), CANDIDATE)

    assert score.final_score == Decimal("87.00")
    assert score.rule_score == Decimal("87.00")
    assert score.counted == ("title_similarity", "semantic_similarity")
    assert score.bucket is MatchBucket.APPLY_NOW


def test_skills_explain_the_score_and_do_not_move_it() -> None:
    """The breakdown stays for the card, the ATS report and the letter.

    Same vacancy, once with a requirement the candidate holds and once with a
    list of ten they do not: the number is identical, and the two lists behind
    it are not.
    """
    held = score_vacancy(vacancy(required_skills={"python": Decimal("1")}), CANDIDATE)
    unheld = score_vacancy(
        vacancy(required_skills={f"skill-{i}": Decimal("1") for i in range(10)}), CANDIDATE
    )

    assert held.final_score == unheld.final_score
    assert [item.canonical_name for item in held.matched] == ["python"]
    assert unheld.matched == []
    assert len(unheld.missing) == 10
    assert "skill_coverage_required" in held.components
    assert "skill_coverage_required" not in held.counted


def test_a_missing_title_vector_keeps_its_weight_and_says_so_in_counted() -> None:
    """Evidence nobody could measure scores zero, as :data:`EVIDENCE` says.

    Renormalising it away would let a vacancy whose title was never compared
    outrank one whose title was compared and fits.
    """
    score = score_vacancy(vacancy(title_similarity=None), CANDIDATE)

    assert score.final_score == Decimal("24.00")
    assert score.counted == ("semantic_similarity",)
    assert "title_similarity" not in score.components


def test_one_matched_skill_under_a_distant_title_stays_out_of_the_queue() -> None:
    """The failure that started this, as a pure case.

    «Инженер связи» against «Python Developer — Backend / AI-интеграции»:
    measured raw cosines 0.488 for the title and about 0.5 for the description.
    One held requirement («linux») used to be enough for 88. Now it is under
    the queue floor.
    """
    telecom = vacancy(
        required_skills={"linux": Decimal("1")},
        title_similarity=normalise_similarity(0.488),
        similarity=normalise_similarity(0.5),
    )

    score = score_vacancy(telecom, CANDIDATE)

    assert [item.canonical_name for item in score.matched] == ["linux"]
    assert score.final_score < Decimal(Settings.model_fields["agent_queue_min_score"].default)
    assert score.bucket is MatchBucket.SKIP


# ── filters stay filters ─────────────────────────────────────────────────────


def test_a_language_the_candidate_lacks_still_filters() -> None:
    """41 of 294 hh vacancies were refused on Kazakh; that refusal is real."""
    score = score_vacancy(vacancy(language_requirements=[("казахский", "B2")]), CANDIDATE)

    assert score.bucket is MatchBucket.FILTERED
    assert score.filtered_reason is not None
    assert "казахский" in score.filtered_reason


def test_a_vacancy_closed_for_applicants_still_filters() -> None:
    """«May I apply» is answered before «how well does it fit»."""
    score = score_vacancy(vacancy(closed_for_applicants=True), CANDIDATE)

    assert score.bucket is MatchBucket.FILTERED


def test_a_weaker_language_is_a_flag_and_not_a_deduction() -> None:
    """The old formula subtracted 8 points; this one says it on the card."""
    score = score_vacancy(vacancy(language_requirements=[("английский", "C1")]), CANDIDATE)

    assert score.penalties == Decimal("0")
    assert score.final_score == score.rule_score
    assert any("английский" in flag for flag in score.red_flags)


def test_a_low_salary_is_a_flag_and_not_a_deduction() -> None:
    """The old formula subtracted 15 points for pay 30% under the floor."""
    score = score_vacancy(vacancy(salary_min=Decimal("1000")), CANDIDATE)

    assert score.penalties == Decimal("0")
    assert score.final_score == Decimal("87.00")
    assert any("зарплата ниже минимума" in flag for flag in score.red_flags)


# ── the old formula, still reproducible ──────────────────────────────────────


def test_the_component_formula_ignores_the_title() -> None:
    """``--formula components`` computes what it computed before this change."""
    close = score_vacancy(vacancy(), CANDIDATE, formula=Formula.COMPONENTS)
    far = score_vacancy(
        vacancy(title_similarity=Decimal("0.1")), CANDIDATE, formula=Formula.COMPONENTS
    )

    assert close.final_score == far.final_score
    assert "title_similarity" not in close.counted
    assert close.formula is Formula.COMPONENTS


def test_the_component_formula_still_subtracts_its_penalties() -> None:
    """Its soft-fails are part of what makes it reproducible."""
    score = score_vacancy(
        vacancy(salary_min=Decimal("1000")), CANDIDATE, formula=Formula.COMPONENTS
    )

    assert score.penalties == Decimal("15")


# ── buckets and the queue floor ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "bucket"),
    [
        ("86.87", MatchBucket.APPLY_NOW),
        ("80.00", MatchBucket.APPLY_NOW),
        ("79.99", MatchBucket.STRONG),
        ("78.00", MatchBucket.STRONG),
        ("77.99", MatchBucket.STRETCH),
        ("75.00", MatchBucket.STRETCH),
        ("74.99", MatchBucket.SKIP),
    ],
)
def test_the_title_formula_buckets_on_its_own_floors(value: str, bucket: MatchBucket) -> None:
    """Carried-over floors would call three vacancies in four a strong match."""
    assert bucket_for(Decimal(value), TITLE_BUCKETS) is bucket


def test_the_component_formula_keeps_the_documented_floors() -> None:
    """The default argument is the old table, so every old caller is unchanged."""
    assert bucket_for(Decimal("72")) is MatchBucket.STRONG
    assert bucket_for(Decimal("72"), BUCKETS) is MatchBucket.STRONG
    assert bucket_for(Decimal("72"), TITLE_BUCKETS) is MatchBucket.SKIP


def test_the_queue_floor_is_the_strong_floor_of_the_default_formula() -> None:
    """70 was ``strong`` on the old scale; left in place it would pass 1129 of 1355."""
    default = Settings.model_fields["agent_queue_min_score"].default

    assert Decimal(default) == dict((b, f) for f, b in TITLE_BUCKETS)[MatchBucket.STRONG]


# ── the report names the formula ─────────────────────────────────────────────


def _run_matching() -> ModuleType:
    """``scripts/`` is not on the suite's path, so load the script by file."""
    path = Path(__file__).resolve().parents[2] / "scripts" / "run_matching.py"
    spec = importlib.util.spec_from_file_location("run_matching_title_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("formula", list(Formula))
def test_the_scoring_report_names_the_formula(
    formula: Formula, capsys: pytest.CaptureFixture[str]
) -> None:
    """Bucket counts from two passes mean nothing without the formula beside them."""
    _run_matching().show(ScoringOutcome(formula=formula), None, dry_run=True)

    printed = capsys.readouterr().out
    assert formula.value in printed
    assert "без вектора названия" in printed
    printed.encode("cp1251")


# ── the title embedding step ─────────────────────────────────────────────────


class _Titles:
    """A repository double whose selection is exact, like the real predicate."""

    def __init__(self, count: int) -> None:
        self.pending = [TitleCandidate(id=uuid4(), title=f"title {i}") for i in range(count)]
        self.written: list[EmbeddedVacancy] = []

    async def needs_title_embedding(self, *, limit: int) -> list[TitleCandidate]:
        return self.pending[:limit]

    async def count_needing_title_embedding(self) -> int:
        return len(self.pending)

    async def set_title_embeddings(self, items: list[EmbeddedVacancy]) -> int:
        done = {item.id for item in items}
        self.pending = [row for row in self.pending if row.id not in done]
        self.written.extend(items)
        return len(items)


class _Session:
    """Counts commits, which is the durability the step promises."""

    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


def _wire(monkeypatch: pytest.MonkeyPatch, titles: _Titles, *, fail: bool = False) -> None:
    async def encode(texts: list[str]) -> list[list[float]]:
        if fail:
            raise EmbeddingsUnavailableError("no runtime")
        return [[0.0] for _ in texts]

    monkeypatch.setattr(step, "VacancyRepository", lambda _session: titles)
    monkeypatch.setattr(step, "encode_texts", encode)
    monkeypatch.setattr(step.settings, "embedding_batch_size", 4)


async def test_the_title_step_drains_and_commits_every_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ten titles in batches of four: three commits, nothing left."""
    titles, session = _Titles(10), _Session()
    _wire(monkeypatch, titles)

    outcome = await step.embed_pending_titles(session, limit=100, time_budget=60)  # type: ignore[arg-type]

    assert outcome.embedded == 10
    assert outcome.batches == 3
    assert session.commits == 3
    assert outcome.backlog == 0
    assert outcome.stopped == "drained"
    assert all(
        item.text_hash == step.text_hash(f"title {i}") for i, item in enumerate(titles.written)
    )


async def test_the_title_step_stops_on_its_row_cap_and_counts_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cap of six takes two batches (four, then two) and leaves four counted."""
    titles, session = _Titles(10), _Session()
    _wire(monkeypatch, titles)

    outcome = await step.embed_pending_titles(session, limit=6, time_budget=60)  # type: ignore[arg-type]

    assert outcome.embedded == 6
    assert outcome.backlog == 4
    assert outcome.stopped == "budget"


async def test_a_missing_runtime_stops_the_title_step_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Optional extra, known state: reported, not raised."""
    titles, session = _Titles(3), _Session()
    _wire(monkeypatch, titles, fail=True)

    outcome = await step.embed_pending_titles(session, limit=100, time_budget=60)  # type: ignore[arg-type]

    assert outcome.stopped == "unavailable"
    assert outcome.embedded == 0
    assert outcome.backlog == 3


def test_candidate_profile_facts_are_untouched_by_the_formula() -> None:
    """The formula is a scoring choice, not a fact about the candidate."""
    assert replace(CANDIDATE) == CANDIDATE
