"""The job titles the owner searches for: stored, carried over, and planned with.

Three things are defended.

**The field replaces the skills, it does not join them.** The measured failure
it exists for is a resume listing Java, Go, JavaScript and C beside Python, and
a crawl that then opened Go and PHP catalogue pages. Titles added *next to* the
skill groups would keep every one of those in the plan.

**An empty field is the old behaviour.** A profile nobody has touched must plan
exactly what it planned before the column existed.

**The effect is visible.** The plan endpoint renders what each source will be
asked for, from the same planner the run uses. Storage and inheritance are in
``test_target_titles_storage.py``.
"""

from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5

import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import ParseStatus, Seniority, SkillEvidence, SkillLevel
from app.db.models import CandidateProfile, SourceState
from app.db.repositories.profile import ProfileRepository
from app.schemas.profile import (
    MAX_TARGET_TITLES,
    CandidateProfileRead,
    CandidateProfileUpdate,
    SkillRead,
    clean_target_titles,
)
from app.sources.hh_roles import load_families, rank_slugs
from app.sources.query_planner import (
    INTENT_SEPARATOR,
    MAX_INTENT_CHARS,
    TARGET_TITLE_LABEL,
    intent_for,
    plan_queries,
)
from factories import make_profile

FIXED = "2026-09-16T00:00:00Z"


def skill(name: str, level: SkillLevel = SkillLevel.STRONG) -> SkillRead:
    """One profile skill, as the planner reads it."""
    return SkillRead(
        id=uuid5(NAMESPACE_URL, f"titles:{name}"),
        canonical_name=name,
        raw_names=[name],
        years=Decimal("3"),
        level=level,
        evidence=SkillEvidence.STATED,
        last_used_year=2026,
    )


def candidate(
    *, titles: list[str], headline: str | None = "Python Developer — Backend"
) -> CandidateProfileRead:
    """A profile with the polyglot skill list that started this."""
    return CandidateProfileRead.model_validate(
        {
            "id": uuid5(NAMESPACE_URL, "titles:profile"),
            "name": None,
            "headline": headline,
            "seniority": Seniority.MIDDLE,
            "total_years": Decimal("4"),
            "summary": None,
            "locations": ["Алматы"],
            "target_titles": titles,
            "relocation": False,
            "remote_pref": None,
            "salary_min": None,
            "salary_currency": None,
            "languages": [],
            "is_active": True,
            "parse_status": ParseStatus.READY,
            "parse_error": None,
            "parse_started_at": None,
            "resume_filename": None,
            "resume_size_bytes": None,
            "resume_format": None,
            "created_at": FIXED,
            "updated_at": FIXED,
            "skills": [
                skill("python", SkillLevel.EXPERT),
                skill("go"),
                skill("java"),
                skill("javascript"),
                skill("php"),
                skill("fastapi"),
                skill("postgresql"),
            ],
        }
    )


# ── the value itself ──────────────────────────────────────────────────


@pytest.mark.unit
def test_titles_are_trimmed_and_deduplicated_in_the_owners_order() -> None:
    """«python developer» after «Python Developer» is the same search twice."""
    cleaned = clean_target_titles(
        ["  Python   Developer ", "", "Backend разработчик", "python developer"]
    )

    assert cleaned == ["Python Developer", "Backend разработчик"]


@pytest.mark.unit
def test_a_sentence_or_a_pile_of_titles_is_refused() -> None:
    """A title is sent verbatim as a query; a paragraph matches nothing."""
    with pytest.raises(ValidationError):
        CandidateProfileUpdate(target_titles=["x" * 101])
    with pytest.raises(ValidationError):
        CandidateProfileUpdate(target_titles=[f"Title {n}" for n in range(MAX_TARGET_TITLES + 1)])


# ── the plan ──────────────────────────────────────────────────────────


@pytest.mark.unit
def test_titles_replace_the_skill_groups_in_the_plan() -> None:
    """The Go and PHP skills must not reach a plan whose owner asked for Python."""
    plan = plan_queries(candidate(titles=["Python Developer", "Backend разработчик"]))

    assert plan.groups == (TARGET_TITLE_LABEL,)
    words = {word for query in plan.queries for word in query.keywords}
    assert words == {"Python Developer", "Backend разработчик"}
    assert {query.area for query in plan.queries} == {"Алматы"}


@pytest.mark.unit
def test_an_empty_field_plans_by_skills_as_before() -> None:
    """Nothing typed is nothing changed."""
    plan = plan_queries(candidate(titles=[]))

    assert TARGET_TITLE_LABEL not in plan.groups
    assert any("python" in query.keywords for query in plan.queries)
    assert {query.headline for query in plan.queries} == {"Python Developer — Backend"}


@pytest.mark.unit
def test_the_intent_line_puts_titles_before_the_headline() -> None:
    """hh ranks by every word of this line with its top weight, so the titles
    weigh at least as much as the headline."""
    line = intent_for(candidate(titles=["Junior Python", "Backend разработчик"]))

    assert line == INTENT_SEPARATOR.join(
        ["Junior Python", "Backend разработчик", "Python Developer — Backend"]
    )


@pytest.mark.unit
def test_the_intent_line_is_cut_at_a_whole_title() -> None:
    """Half a title is a different job."""
    titles = [f"{'Developer' * 9} {n}" for n in range(6)]
    line = intent_for(candidate(titles=titles, headline=None))

    assert line is not None
    assert len(line) <= MAX_INTENT_CHARS
    assert all(part in titles for part in line.split(INTENT_SEPARATOR))


@pytest.mark.unit
def test_a_headline_repeating_a_title_is_not_said_twice() -> None:
    """Case aside, the same words count once."""
    assert intent_for(candidate(titles=["Python Developer"], headline="python developer")) == (
        "Python Developer"
    )


@pytest.mark.unit
def test_titles_move_python_pages_ahead_of_go_pages() -> None:
    """The measured failure, offline: with the title typed, a Python catalogue
    page outranks a Go page even though the resume claims both."""
    families = load_families()
    slugs = ("go-razrabotchik", "php-developer", "python-developer", "java-developer")
    plan = plan_queries(candidate(titles=["Python Developer"]))
    keywords = sorted({word for query in plan.queries for word in query.keywords})
    headline = plan.queries[0].headline

    ranked = rank_slugs(slugs, keywords, families=families, headline=headline)

    assert ranked[0] == "python-developer"


# ── the plan endpoint ─────────────────────────────────────────────────


async def stored_profile(
    profiles: ProfileRepository,
    session: AsyncSession,
    *,
    titles: list[str] | None = None,
    active: bool = True,
) -> CandidateProfile:
    """A profile row with titles set on it; the extraction schema carries none."""
    instance = await profiles.create(make_profile())
    instance.target_titles = list(titles or [])
    instance.is_active = active
    await session.flush()
    return instance


async def test_the_plan_endpoint_shows_what_the_titles_changed(
    async_client: AsyncClient, profiles: ProfileRepository, db_session: AsyncSession
) -> None:
    """After saving, the owner sees the titles as jsearch queries and hh pages
    re-ordered around them, without a request to either."""
    await stored_profile(profiles, db_session, titles=["Python Developer"])
    db_session.add(
        SourceState(
            source_slug="hh",
            key="catalog:almaty.hh.kz",
            value={
                "resolved_at": "2026-09-08T19:20:10+00:00",
                "families": ["backend"],
                "headline": "Backend Engineer",
                "slugs": ["go-razrabotchik", "python-developer"],
            },
        )
    )
    await db_session.flush()

    response = await async_client.get("/api/v1/sources/plan")

    assert response.status_code == 200
    body = response.json()
    assert body["basis"] == "titles"
    assert body["target_titles"] == ["Python Developer"]
    by_slug = {source["slug"]: source["preview"] for source in body["sources"]}
    assert by_slug["jsearch"]["use"] == "query"
    # The factory profile would relocate, so the city is followed by a search
    # with no place — and no country, since none can be derived from nothing.
    assert by_slug["jsearch"]["terms"] == ["Python Developer Алматы · KZ", "Python Developer"]
    assert by_slug["hh"]["use"] == "catalog"
    assert by_slug["hh"]["terms"][0] == "python-developer"
    assert "заново" in (by_slug["hh"]["note"] or "")
    assert by_slug["arbeitnow"]["terms"] == ["Python Developer"]


async def test_the_plan_endpoint_without_a_profile_is_a_404(async_client: AsyncClient) -> None:
    """No resume, no plan — and a sentence saying so."""
    response = await async_client.get("/api/v1/sources/plan")

    assert response.status_code == 404
