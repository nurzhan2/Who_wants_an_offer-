"""The upload pipeline end to end: extraction -> enrichment -> row -> vector.

Every step is exercised individually elsewhere. What this module owns is the
wiring between them, which is where the failures nobody sees live:

* the stored ``total_years`` must be the computed union of the work periods,
  never the number the resume claimed — the whole point of ``app.resume.dates``
  evaporates if the orchestration reads the model's arithmetic back out;
* skills that canonicalise to the same name must be collapsed before the write,
  or the ``(profile_id, canonical_name)`` unique constraint rejects a resume
  that is entirely ordinary;
* every expected failure must leave the row ``failed`` with a reason. A profile
  stuck in ``pending`` is invisible to the user and to the logs alike, and the
  embedding step is the one that used to end up there.

No network and no model: the LLM is a stand-in returning a prepared extraction,
and the embedding provider is the deterministic fake.
"""

from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from app.core.config import settings
from app.core.exceptions import LLMError
from app.db.enums import ParseStatus, RemoteType, Seniority
from app.db.models import CandidateProfile
from app.db.repositories.profile import ProfileRepository
from app.llm.client import LLMClient, LLMRefusalError, LLMResult
from app.llm.pricing import TokenUsage
from app.matching import embeddings
from app.resume import profile_builder
from app.resume.extractor import ExtractedDocument
from app.schemas.llm import ExtractedLanguage, ExtractedSkill, ProfileExtraction, WorkPeriod
from factories import make_profile

#: Fixed so "still employed" and staleness have a reproducible answer.
TODAY = date(2026, 9, 1)

#: A string no code path is allowed to copy into a log line or into
#: ``parse_error``. Planted in the resume text and in the extraction.
RESUME_MARKER = "ZZ-SECRET-RESUME-CONTENT-ZZ"


# ── stand-ins ─────────────────────────────────────────────────────────


class StubLLM:
    """An :class:`LLMClient` that answers from memory instead of the network.

    Holds either the extraction to return or the exception to raise, so a test
    picks a failure mode by construction rather than by patching internals.
    """

    def __init__(self, answer: ProfileExtraction | Exception) -> None:
        self._answer = answer
        #: Every call, so a test can assert the resume really was sent.
        self.calls: list[dict[str, Any]] = []

    async def complete_json(
        self,
        prompt_name: str,
        response_model: type[ProfileExtraction],
        **kwargs: Any,
    ) -> LLMResult[ProfileExtraction]:
        """Record the call, then return the prepared answer or raise."""
        self.calls.append({"prompt_name": prompt_name, "response_model": response_model, **kwargs})
        if isinstance(self._answer, Exception):
            raise self._answer
        return LLMResult(
            value=self._answer,
            model="stub-model",
            usage=TokenUsage(input_tokens=1200, output_tokens=300),
            cost_usd=0.02,
            attempts=1,
        )


def unavailable_provider() -> embeddings.EmbeddingProvider:
    """Exactly what ``get_provider`` returns without the ``[embeddings]`` extra.

    The real placeholder rather than a hand-rolled raiser: the message it
    produces is the one that ends up in ``parse_error``, so a test that invents
    its own would stop checking the text the user actually reads.
    """
    return embeddings.UnavailableEmbeddingProvider(
        "embedding_provider='bge-m3' needs sentence_transformers, which is not installed"
    )


# ── builders ──────────────────────────────────────────────────────────


def job(
    company: str,
    start: str,
    end: str,
    *,
    title: str = "Backend Engineer",
    domains: Sequence[str] = ("fintech",),
) -> WorkPeriod:
    """One work period with the fields the enrichment actually reads."""
    return WorkPeriod(company=company, title=title, start=start, end=end, domains=list(domains))


#: Two jobs held at the same time. Summed they are three years; merged, which
#: is the only honest reading, they are two.
OVERLAPPING_JOBS = (
    job("Acme", "2022-01", "2023-12"),
    job("Freelance", "2023-01", "2023-12"),
)


def make_extraction(
    *,
    skills: Sequence[ExtractedSkill] | None = None,
    work_periods: Sequence[WorkPeriod] = OVERLAPPING_JOBS,
    **overrides: Any,
) -> ProfileExtraction:
    """What the model is pretending to have read out of a resume."""
    fields: dict[str, Any] = {
        "full_name": "Нуржан Кандидатов",
        "headline": "Backend Engineer",
        "summary": "Builds services.",
        "city": "Алматы",
        "country": "KZ",
        "relocation": True,
        "remote_pref": "hybrid",
        "salary_expectation": 4000.0,
        "salary_currency": "USD",
        "languages": [ExtractedLanguage(code="ru", level="native")],
        "work_periods": list(work_periods),
        "skills": list(
            skills
            if skills is not None
            else [
                ExtractedSkill(name="Python", mentioned_in="work_description", companies=["Acme"]),
                ExtractedSkill(name="FastAPI", mentioned_in="skills_block"),
            ]
        ),
    }
    fields.update(overrides)
    return ProfileExtraction(**fields)


def make_document(
    raw_text: str = "Резюме кандидата.", source_format: str = "txt"
) -> ExtractedDocument:
    """An upload that already went through the extractor."""
    content = raw_text.encode("utf-8")
    return ExtractedDocument(
        raw_text=raw_text,
        page_count=1,
        source_format=source_format,
        needs_ocr=False,
        warnings=(),
        file_bytes=content,
        size_bytes=len(content),
    )


# ── fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def fake_embeddings(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Iterator[None]:
    """Deterministic vectors and a throwaway cache directory.

    Without the explicit provider ``get_provider`` would return the unavailable
    placeholder on this machine — or, worse, load 2.3 GB of weights on one that
    has the extra installed.
    """
    # Held by name: a test may replace ``embeddings.get_provider`` outright, and
    # the teardown still has to clear the cache of the real one.
    real_get_provider = embeddings.get_provider
    monkeypatch.setattr(settings, "embedding_provider", "fake")
    monkeypatch.setattr(settings, "embedding_cache_dir", tmp_path / "vectors")
    real_get_provider.cache_clear()
    try:
        yield
    finally:
        real_get_provider.cache_clear()


@pytest_asyncio.fixture
async def pending_id(profiles: ProfileRepository) -> UUID:
    """The reserved row the upload endpoint hands the background task."""
    profile = await profiles.create_pending(
        filename="resume.pdf",
        size_bytes=2048,
        source_format="pdf",
        started_at=datetime(2026, 9, 1, tzinfo=UTC),
    )
    return profile.id


async def build(
    session: AsyncSession,
    profile_id: UUID,
    answer: ProfileExtraction | Exception,
    *,
    document: ExtractedDocument | None = None,
) -> profile_builder.BuildResult:
    """Run the pipeline against a stand-in model."""
    return await profile_builder.build_profile(
        document if document is not None else make_document(),
        session=session,
        profile_id=profile_id,
        client=cast(LLMClient, StubLLM(answer)),
        today=TODAY,
    )


async def stored(
    profiles: ProfileRepository, session: AsyncSession, profile_id: UUID
) -> CandidateProfile:
    """Re-read the row, ignoring anything the session still holds in memory."""
    session.expire_all()
    profile = await profiles.get(profile_id)
    assert profile is not None
    return profile


# ── the happy path ────────────────────────────────────────────────────


async def test_a_successful_build_clears_the_pending_state(
    db_session: AsyncSession, profiles: ProfileRepository, pending_id: UUID, fake_embeddings: None
) -> None:
    """The client polls ``parse_status``; leaving it pending after a good run
    would spin the dashboard for ever on a profile that is actually finished."""
    result = await build(db_session, pending_id, make_extraction())

    profile = await stored(profiles, db_session, pending_id)
    assert result.status is ParseStatus.READY
    assert profile.parse_status is ParseStatus.READY
    assert profile.parse_error is None


async def test_the_extracted_columns_are_written_onto_the_reserved_row(
    db_session: AsyncSession, profiles: ProfileRepository, pending_id: UUID, fake_embeddings: None
) -> None:
    """``create_pending`` reserves an empty row and nothing else writes to it,
    so a field the assembly forgets is a field that stays null in production."""
    await build(db_session, pending_id, make_extraction())

    profile = await stored(profiles, db_session, pending_id)
    assert profile.name == "Нуржан Кандидатов"
    assert profile.headline == "Backend Engineer"
    assert profile.summary == "Builds services."
    assert profile.locations == ["Алматы"]
    assert profile.relocation is True
    assert profile.remote_pref is RemoteType.HYBRID
    assert profile.salary_min == Decimal("4000")
    assert profile.salary_currency == "USD"
    assert profile.languages == [{"code": "ru", "level": "native"}]
    assert profile.raw_text == "Резюме кандидата."


async def test_seniority_is_derived_rather_than_asked_for(
    db_session: AsyncSession, profiles: ProfileRepository, pending_id: UUID, fake_embeddings: None
) -> None:
    """The extraction contract has no seniority field at all: grade is inferred
    from computed years and the titles held, so two years of plain engineering
    titles must land as junior however the resume presents itself."""
    await build(db_session, pending_id, make_extraction())

    assert (await stored(profiles, db_session, pending_id)).seniority is Seniority.JUNIOR


async def test_skills_land_as_rows_of_their_own(
    db_session: AsyncSession, profiles: ProfileRepository, pending_id: UUID, fake_embeddings: None
) -> None:
    """Skills are a separate table written by a separate call. Forgetting it
    still produces a READY profile — one that matches nothing."""
    result = await build(db_session, pending_id, make_extraction())

    profile = await stored(profiles, db_session, pending_id)
    assert {skill.canonical_name for skill in profile.skills} == {"python", "fastapi"}
    assert result.skill_count == 2


async def test_a_vector_of_the_configured_width_is_stored(
    db_session: AsyncSession, profiles: ProfileRepository, pending_id: UUID, fake_embeddings: None
) -> None:
    """Semantic scoring reads this column. A profile that is READY with a null
    embedding scores against nothing and gives no hint why."""
    await build(db_session, pending_id, make_extraction())

    embedding = (await stored(profiles, db_session, pending_id)).embedding
    assert embedding is not None
    assert len(embedding) == settings.embedding_dim
    assert any(value != 0.0 for value in embedding)


async def test_a_finished_build_retires_every_other_profile(
    db_session: AsyncSession, profiles: ProfileRepository, pending_id: UUID, fake_embeddings: None
) -> None:
    """Uploading a new resume supersedes the old profile. Two active profiles
    would make ``get_active`` pick by creation order, so which resume the
    dashboard scores against would depend on a timestamp."""
    previous = await profiles.create(make_profile(name="Previous"))
    previous_id = previous.id
    assert previous.is_active is True

    await build(db_session, pending_id, make_extraction())

    assert (await stored(profiles, db_session, previous_id)).is_active is False


async def test_a_finished_build_activates_the_profile_it_just_wrote(
    db_session: AsyncSession, profiles: ProfileRepository, pending_id: UUID, fake_embeddings: None
) -> None:
    """The reserved row is created inactive so a half-parsed profile can never
    be scored against, and something has to switch it on once parsing works.

    Nothing did. ``deactivate_others`` retired the previous profile and the new
    one stayed inactive, so a successful upload ended with NO active profile at
    all and ``get_active`` returned None — the dashboard had nothing to score
    against, and the only symptom was an empty screen."""
    await build(db_session, pending_id, make_extraction())

    assert (await stored(profiles, db_session, pending_id)).is_active is True


async def test_after_an_upload_exactly_one_profile_is_active(
    db_session: AsyncSession, profiles: ProfileRepository, pending_id: UUID, fake_embeddings: None
) -> None:
    """The property that matters is the pair, not either half: activating
    without retiring, or retiring without activating, both leave the dashboard
    wrong. Asserted through get_active, which is what actually reads it."""
    await profiles.create(make_profile(name="Previous"))

    await build(db_session, pending_id, make_extraction())

    active = await profiles.get_active()
    assert active is not None
    assert active.id == pending_id


async def test_a_failed_build_does_not_activate_anything(
    db_session: AsyncSession, profiles: ProfileRepository, pending_id: UUID, fake_embeddings: None
) -> None:
    """A profile that failed to parse must not become the live one; the
    previous resume is better than an empty one."""
    await build(db_session, pending_id, LLMError("the model returned nothing usable"))

    assert (await stored(profiles, db_session, pending_id)).is_active is False


# ── the headline guarantee ────────────────────────────────────────────


@pytest.mark.parametrize("claim", [99.0, 5.0])
async def test_total_years_comes_from_the_computed_union_not_from_the_resume(
    db_session: AsyncSession,
    profiles: ProfileRepository,
    pending_id: UUID,
    fake_embeddings: None,
    claim: float,
) -> None:
    """THE assertion of this module: it is what keeps a model's arithmetic out
    of the product.

    ``stated_total_years`` is what the resume claims, and people claim the sum
    of their overlapping jobs — a full-time role plus freelance, counted twice.
    Every score downstream is derived from ``total_years``, so the moment the
    orchestration reads the claim instead of the union, a third-year student
    becomes a principal engineer and nothing in the system disagrees.

    Two jobs running side by side for one of their two years: the union is 2.0
    whatever the resume says. The claim is tried both absurd (99, which the
    ``le=60`` bound on the column would also reject) and merely flattering (5,
    which nothing but this assertion would ever catch)."""
    await build(
        db_session,
        pending_id,
        make_extraction(work_periods=OVERLAPPING_JOBS, stated_total_years=claim),
    )

    assert (await stored(profiles, db_session, pending_id)).total_years == Decimal("2.0")


async def test_the_reported_years_match_the_stored_ones(
    db_session: AsyncSession, profiles: ProfileRepository, pending_id: UUID, fake_embeddings: None
) -> None:
    """The result feeds the run report and the logs. If it disagreed with the
    column, an investigation would start from a number nobody stored."""
    result = await build(db_session, pending_id, make_extraction(stated_total_years=99.0))

    profile = await stored(profiles, db_session, pending_id)
    assert profile.total_years is not None
    assert result.total_years == float(profile.total_years)
    assert result.skill_count == len(profile.skills)


async def test_an_implausible_claim_is_reported_without_changing_the_number(
    db_session: AsyncSession, pending_id: UUID, fake_embeddings: None
) -> None:
    """A large gap between the claim and the computation is worth seeing, but
    it is a warning and never a correction: the computation wins."""
    result = await build(db_session, pending_id, make_extraction(stated_total_years=99.0))

    assert any("99" in warning and "2.0" in warning for warning in result.warnings)


# ── canonicalisation before the write ─────────────────────────────────


async def test_spellings_of_one_skill_are_collapsed_before_the_write(
    db_session: AsyncSession, profiles: ProfileRepository, pending_id: UUID, fake_embeddings: None
) -> None:
    """ "Python" in the sidebar and "Python 3" in a job description is what
    close to every real resume looks like. Both canonicalise to ``python``, and
    writing two rows violates the (profile_id, canonical_name) unique
    constraint — the upload fails on an ordinary CV, not an exotic one."""
    extraction = make_extraction(
        skills=[
            ExtractedSkill(name="Python", mentioned_in="skills_block"),
            ExtractedSkill(name="Python 3", mentioned_in="work_description", companies=["Acme"]),
        ]
    )

    result = await build(db_session, pending_id, extraction)

    profile = await stored(profiles, db_session, pending_id)
    assert [skill.canonical_name for skill in profile.skills] == ["python"]
    assert result.status is ParseStatus.READY


async def test_collapsing_keeps_both_spellings_on_the_surviving_row(
    db_session: AsyncSession, profiles: ProfileRepository, pending_id: UUID, fake_embeddings: None
) -> None:
    """The raw names are the only record of what the resume said, and the input
    for extending the dictionary. Collapsing must merge them, not pick one."""
    extraction = make_extraction(
        skills=[
            ExtractedSkill(name="Python", mentioned_in="skills_block"),
            ExtractedSkill(name="Python 3", mentioned_in="skills_block"),
        ]
    )

    await build(db_session, pending_id, extraction)

    profile = await stored(profiles, db_session, pending_id)
    assert set(profile.skills[0].raw_names) == {"Python", "Python 3"}


# ── failure handling ──────────────────────────────────────────────────


async def test_an_llm_failure_marks_the_profile_failed_with_its_reason(
    db_session: AsyncSession, profiles: ProfileRepository, pending_id: UUID, fake_embeddings: None
) -> None:
    """The row is the only place the user can learn what happened: the request
    that started the parse was answered long before it failed."""
    error = LLMError("extract_profile returned a payload that failed validation twice")

    result = await build(db_session, pending_id, error)

    profile = await stored(profiles, db_session, pending_id)
    assert result.status is ParseStatus.FAILED
    assert profile.parse_status is ParseStatus.FAILED
    assert profile.parse_error == str(error)


async def test_a_failed_parse_leaves_the_working_profile_in_place(
    db_session: AsyncSession,
    profiles: ProfileRepository,
    pending_id: UUID,
    fake_embeddings: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retiring the previous profile is the last step of the run for a reason.

    A failed upload must cost the user nothing: if the old profile were retired
    before the new one was finished, a resume the model happens to choke on
    would leave the dashboard with no active profile at all — every match gone,
    and the only way back a second successful upload.

    The failure is forced at the embedding step, i.e. after the columns and the
    skills have already been written, so this really does pin the ordering
    rather than the fact that the run stopped early."""
    previous_id = (await profiles.create(make_profile(name="Previous"))).id
    monkeypatch.setattr(embeddings, "get_provider", unavailable_provider)

    result = await build(db_session, pending_id, make_extraction())

    assert result.status is ParseStatus.FAILED
    assert (await stored(profiles, db_session, previous_id)).is_active is True


async def test_a_refusal_is_recorded_as_the_model_declining(
    db_session: AsyncSession, profiles: ProfileRepository, pending_id: UUID, fake_embeddings: None
) -> None:
    """A safety refusal is not a broken resume, and telling the user their file
    is unparseable would send them off to fix a file that is fine."""
    refusal = LLMRefusalError("the model declined to process this document")

    result = await build(db_session, pending_id, refusal)

    profile = await stored(profiles, db_session, pending_id)
    assert result.status is ParseStatus.FAILED
    assert profile.parse_error is not None
    assert "declined" in profile.parse_error


async def test_an_embedding_failure_also_marks_the_profile_failed(
    db_session: AsyncSession,
    profiles: ProfileRepository,
    pending_id: UUID,
    fake_embeddings: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression this guards is specifically an ``except`` clause.

    ``EmbeddingError`` is a sibling of ``LLMError`` under ``AppError``, not a
    subclass of it. A handler that listed ``(ParsingError, LLMError)`` therefore
    let it straight through, and on any machine without the ``[embeddings]``
    extra every single upload stayed pending for ever — no failure, no reason,
    no log line. Catching the base class is the fix; this is the test that says
    so, and it fails again the moment somebody narrows the clause."""
    monkeypatch.setattr(embeddings, "get_provider", unavailable_provider)

    result = await build(db_session, pending_id, make_extraction())

    profile = await stored(profiles, db_session, pending_id)
    assert result.status is ParseStatus.FAILED
    assert profile.parse_status is ParseStatus.FAILED
    assert profile.parse_error is not None
    assert "uv sync --extra embeddings" in profile.parse_error


async def test_an_unexpected_error_is_re_raised_rather_than_swallowed(
    db_session: AsyncSession, pending_id: UUID, fake_embeddings: None
) -> None:
    """Only expected failures become a ``parse_error``. A ``TypeError`` in this
    pipeline is a bug, and recording it as "parsing failed" would hide the
    traceback that is the only way to find it."""
    with pytest.raises(RuntimeError, match="boom"):
        await build(db_session, pending_id, RuntimeError("boom"))


# ── the resume never leaks ────────────────────────────────────────────


async def test_parse_error_never_carries_resume_content(
    db_session: AsyncSession,
    profiles: ProfileRepository,
    pending_id: UUID,
    fake_embeddings: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``parse_error`` is served by the API and copied into the logs, so a
    fragment of the resume in it is personal data leaving the database.

    The failure is forced after extraction on purpose: that is the only point
    where the pipeline holds the resume and a message at the same time."""
    monkeypatch.setattr(embeddings, "get_provider", unavailable_provider)
    extraction = make_extraction(
        full_name=RESUME_MARKER, summary=RESUME_MARKER, headline=RESUME_MARKER
    )

    await build(db_session, pending_id, extraction, document=make_document(raw_text=RESUME_MARKER))

    profile = await stored(profiles, db_session, pending_id)
    assert profile.parse_error is not None
    assert RESUME_MARKER not in profile.parse_error


@pytest.mark.parametrize("fails", [False, True])
async def test_no_log_record_carries_resume_content(
    db_session: AsyncSession,
    pending_id: UUID,
    fake_embeddings: None,
    monkeypatch: pytest.MonkeyPatch,
    fails: bool,
) -> None:
    """Logs are shipped off the machine and kept far longer than the profile.
    Both the success line and the failure line are checked: the success path
    holds every field of the resume, and the failure path is where a "helpful"
    ``error=str(exc)`` gets added under pressure."""
    if fails:
        monkeypatch.setattr(embeddings, "get_provider", unavailable_provider)
    extraction = make_extraction(
        full_name=RESUME_MARKER,
        summary=RESUME_MARKER,
        headline=RESUME_MARKER,
        skills=[ExtractedSkill(name=RESUME_MARKER, mentioned_in="skills_block")],
    )

    with capture_logs() as records:
        await build(
            db_session, pending_id, extraction, document=make_document(raw_text=RESUME_MARKER)
        )

    assert records, "the pipeline must log the outcome of every run"
    assert all(RESUME_MARKER not in repr(record) for record in records)


# ── what actually reaches the model ───────────────────────────────────


async def test_a_pdf_is_handed_to_the_model_as_a_document(
    db_session: AsyncSession, pending_id: UUID, fake_embeddings: None
) -> None:
    """Line-oriented text extraction reads straight across a two-column resume,
    interleaving the sidebar with the body. Sending that text instead of the
    file would quietly halve the quality of every extraction."""
    client = StubLLM(make_extraction())
    pdf = make_document(raw_text="sidebar text", source_format="pdf")

    await profile_builder.build_profile(
        pdf,
        session=db_session,
        profile_id=pending_id,
        client=cast(LLMClient, client),
        today=TODAY,
    )

    call = client.calls[0]
    assert [document.content for document in call["documents"]] == [pdf.file_bytes]
    assert "sidebar text" not in str(call["variables"]["resume_text"])


async def test_a_text_upload_is_sent_as_text(
    db_session: AsyncSession, pending_id: UUID, fake_embeddings: None
) -> None:
    """DOCX and plain text have no column problem worth solving, and attaching
    them as documents would inflate every request for nothing."""
    client = StubLLM(make_extraction())

    await profile_builder.build_profile(
        make_document(raw_text="Опыт работы: 2 года."),
        session=db_session,
        profile_id=pending_id,
        client=cast(LLMClient, client),
        today=TODAY,
    )

    call = client.calls[0]
    assert call["documents"] == ()
    assert call["variables"]["resume_text"] == "Опыт работы: 2 года."
