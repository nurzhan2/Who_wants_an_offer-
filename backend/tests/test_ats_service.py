"""One report, assembled once, rendered in three places.

The feature's fourth requirement was that the vacancy screen, the preview and
the confirmation card before an application show the *same* report. That is not
a UI statement, it is an architectural one: the moment two of them compute the
answer, they will disagree, and a person who reads 72 on one screen and 84 on
the next has learned that neither number means anything.

So what is defended here is the single assembly. The report the endpoint serves,
the summary the agent's card prints and the audit a generated letter goes
through are all built from :class:`app.schemas.ats.ATSReport`, and the tests
below check that the projection cannot drift from the object and that adding the
keyword half leaves the score explaining itself.

The database-backed half of the module is exercised with real rows rather than
mocks: it exists to read the employer's own spelling out of a JSONB payload
rather than the folded copy in ``vacancy_skill``, and a mock would agree with
whichever of those the test author had in mind.
"""

from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import VacancySkill
from app.db.repositories.profile import ProfileRepository
from app.db.repositories.vacancy import VacancyRepository
from app.resume.ats_keywords import HeldSkill, match_requirements
from app.schemas.ats import (
    ATSKeywords,
    ATSReport,
    ATSSummary,
    DocumentKind,
    Finding,
    FindingCode,
    KeywordStatus,
    Overall,
    RequirementMatch,
    Severity,
)
from app.services import ats as ats_service
from factories import make_profile, make_vacancy

pytestmark = pytest.mark.unit

HELD = (
    HeldSkill(canonical_name="python", spellings=("Python",)),
    HeldSkill(canonical_name="postgresql", spellings=("PostgreSQL",)),
)


def stored_report(findings: list[Finding] | None = None) -> ATSReport:
    """A structural report as it would come back from the profile column."""
    raised = (
        [
            Finding(
                code=FindingCode.TEXT_IN_TABLES,
                severity=Severity.WARNING,
                title="Содержимое свёрстано таблицами",
                explanation="...",
                fix="...",
                penalty=12,
            )
        ]
        if findings is None
        else findings
    )
    return ATSReport(
        score=100 - sum(finding.penalty for finding in raised),
        findings=raised,
        checks_run=[FindingCode.TEXT_IN_TABLES],
        source_format="pdf",
    )


def keywords_with(*statuses: KeywordStatus) -> ATSKeywords:
    """A keyword reading holding one requirement per status given."""
    return ATSKeywords(
        requirements=[
            RequirementMatch(
                requirement=f"Skill{index}",
                status=status,
                held_as="Skill" if status is KeywordStatus.UNSTATED else None,
            )
            for index, status in enumerate(statuses)
        ]
    )


# ── the assembly ──────────────────────────────────────────────────────


def test_the_score_is_recomputed_when_the_keyword_half_is_added() -> None:
    """The score is the arithmetic of the findings, or it explains nothing.

    Carrying the stored number across would leave a report whose findings add up
    to something else — the one part of it nobody could check against the rest.
    """
    report = ats_service.with_keywords(stored_report(), keywords_with(KeywordStatus.UNSTATED))

    assert FindingCode.REQUIREMENTS_NOT_NAMED in {f.code for f in report.findings}
    assert report.score == 100 - sum(f.penalty for f in report.findings)


def test_applying_a_second_vacancy_does_not_leave_the_first_one_s_gaps() -> None:
    """The stored report is read once and compared many times."""
    once = ats_service.with_keywords(stored_report(), keywords_with(KeywordStatus.UNSTATED))
    twice = ats_service.with_keywords(once, keywords_with(KeywordStatus.PRESENT))

    assert [f.code for f in twice.findings] == [FindingCode.TEXT_IN_TABLES]
    assert twice.keywords is not None
    assert twice.keywords.unstated == []
    assert twice.checks_run.count(FindingCode.REQUIREMENTS_NOT_NAMED) == 1


def test_requirements_nobody_holds_do_not_produce_a_finding() -> None:
    """The boundary again, this time in the assembly.

    An ``absent`` requirement is reported in the keyword list and generates no
    advice, because the only advice available would be to claim it.
    """
    report = ats_service.with_keywords(stored_report(), keywords_with(KeywordStatus.ABSENT))

    assert FindingCode.REQUIREMENTS_NOT_NAMED not in {f.code for f in report.findings}
    assert report.keywords is not None
    assert len(report.keywords.absent) == 1


def test_the_keyword_half_is_listed_in_checks_run_only_once_compared() -> None:
    """Absence of a comparison must never read as a clean comparison."""
    assert FindingCode.REQUIREMENTS_NOT_NAMED not in stored_report().checks_run


# ── the projection the card prints ────────────────────────────────────


def test_the_card_summary_is_a_projection_of_the_report() -> None:
    """Built by ``of`` and by nothing else, so the two cannot disagree."""
    report = ats_service.with_keywords(
        stored_report(),
        keywords_with(KeywordStatus.PRESENT, KeywordStatus.UNSTATED, KeywordStatus.ABSENT),
    )
    summary = ATSSummary.of(report)

    assert summary.overall is report.overall
    assert summary.score == report.score
    assert summary.requirements_total == 3
    assert summary.requirements_present == 1
    assert summary.absent == 1
    assert summary.unstated == ["Skill1"]


def test_the_card_summary_names_what_is_fixable_and_counts_what_is_not() -> None:
    """The asymmetry is the design.

    Named requirements are ones the candidate holds — a letter to rewrite. The
    ones they do not hold stay a number, because a list of them printed seconds
    before an application reads as a list of things to claim.
    """
    keywords = keywords_with(*([KeywordStatus.ABSENT] * 4))
    summary = ATSSummary.of(ats_service.with_keywords(stored_report(), keywords))

    assert summary.unstated == []
    assert summary.absent == 4


def test_a_summary_of_a_report_with_no_vacancy_says_nothing_about_requirements() -> None:
    """Zero requirements and zero present must not read as "none matched"."""
    summary = ATSSummary.of(stored_report())

    assert summary.requirements_total == 0
    assert summary.unstated == []


def test_the_summary_carries_the_critical_titles_a_person_can_act_on() -> None:
    """A card prints for a human; a code is for a client."""
    report = stored_report(
        [
            Finding(
                code=FindingCode.HIDDEN_TEXT,
                severity=Severity.CRITICAL,
                title="В документе есть скрытый текст",
                explanation="...",
                fix="...",
                penalty=60,
            )
        ]
    )
    summary = ATSSummary.of(report)

    assert summary.overall is Overall.UNREADABLE
    assert summary.critical == ["В документе есть скрытый текст"]


# ── against the database ──────────────────────────────────────────────


async def seed_vacancy(session: AsyncSession, seed: str, key_skills: list[str]) -> UUID:
    """One vacancy whose payload carries the employer's own spellings."""
    upserted = await VacancyRepository(session).upsert_by_external_id(
        make_vacancy(seed),
        source_slug="hh",
        external_id=f"hh-{seed}",
        url=f"https://e.test/{seed}",
        raw={"_derived": {"key_skills": key_skills}},
    )
    await session.flush()
    return upserted.vacancy_id


async def test_requirements_are_read_in_the_employer_s_own_spelling(
    db_session: AsyncSession,
) -> None:
    """The whole reason the payload is preferred over the normalised rows.

    ``vacancy_skill.canonical_name`` is casefolded by ``app.normalize``, so a
    literal check against it would compare the document with a string the
    employer never wrote — and "PostgreSQL" is exactly the string their filter
    searches for.
    """
    vacancy_id = await seed_vacancy(db_session, "ats-spelling", ["PostgreSQL", "Apache Kafka"])
    db_session.add(
        VacancySkill(
            vacancy_id=vacancy_id,
            canonical_name="postgresql",
            is_required=True,
            weight=Decimal("1.00"),
        )
    )
    await db_session.flush()

    names, required = await ats_service.requirements_of(db_session, vacancy_id)

    assert names == ["PostgreSQL", "Apache Kafka"]
    assert required == [True, True]


async def test_hardness_comes_from_the_rows_that_hold_it(db_session: AsyncSession) -> None:
    """The payload has the spelling; only the rows say which are hard."""
    vacancy_id = await seed_vacancy(db_session, "ats-hardness", ["PostgreSQL", "Apache Kafka"])
    db_session.add_all(
        [
            VacancySkill(
                vacancy_id=vacancy_id,
                canonical_name="postgresql",
                is_required=True,
                weight=Decimal("1.00"),
            ),
            VacancySkill(
                vacancy_id=vacancy_id,
                canonical_name="apache kafka",
                is_required=False,
                weight=Decimal("0.60"),
            ),
        ]
    )
    await db_session.flush()

    _, required = await ats_service.requirements_of(db_session, vacancy_id)

    assert required == [True, False]


async def test_rows_alone_still_answer_when_no_payload_carries_a_list(
    db_session: AsyncSession,
) -> None:
    """Degraded, and better than reporting a vacancy as asking for nothing."""
    vacancy_id = await seed_vacancy(db_session, "ats-rows-only", [])
    db_session.add(
        VacancySkill(
            vacancy_id=vacancy_id,
            canonical_name="kubernetes",
            is_required=True,
            weight=Decimal("1.00"),
        )
    )
    await db_session.flush()

    names, _ = await ats_service.requirements_of(db_session, vacancy_id)

    assert names == ["kubernetes"]


async def test_held_skills_carry_the_spelling_the_resume_used(
    db_session: AsyncSession,
) -> None:
    """The evidence for "unstated" is what the candidate actually wrote."""
    profile = await ProfileRepository(db_session).create(make_profile(skills=("postgresql",)))
    await db_session.flush()

    (skill,) = await ats_service.held_skills(db_session, profile.id)

    assert skill.canonical_name == "postgresql"
    assert skill.spelling == "Postgresql"


async def test_report_for_vacancy_joins_the_stored_audit_to_the_requirements(
    db_session: AsyncSession,
) -> None:
    """The endpoint's whole job, end to end against real rows."""
    profiles = ProfileRepository(db_session)
    profile = await profiles.create(
        make_profile(
            skills=("python", "postgresql"),
            raw_text="Опыт: Python, Docker. Работал с постгрес.",
        )
    )
    await db_session.flush()
    await profiles.set_ats_report(profile.id, stored_report())
    await db_session.flush()
    vacancy_id = await seed_vacancy(db_session, "ats-join", ["Python", "PostgreSQL", "Kubernetes"])

    report = await ats_service.report_for_vacancy(db_session, profile.id, vacancy_id)

    assert report is not None
    assert report.keywords is not None
    assert [r.requirement for r in report.keywords.present] == ["Python"]
    assert [r.requirement for r in report.keywords.unstated] == ["PostgreSQL"]
    assert [r.requirement for r in report.keywords.absent] == ["Kubernetes"]
    # The structural half survived the join rather than being recomputed as clean.
    assert FindingCode.TEXT_IN_TABLES in {f.code for f in report.findings}


async def test_no_stored_audit_is_not_a_clean_audit(db_session: AsyncSession) -> None:
    """A profile with no report must not be served an empty passing one."""
    profile = await ProfileRepository(db_session).create(make_profile())
    await db_session.flush()
    vacancy_id = await seed_vacancy(db_session, "ats-none", ["Python"])

    assert await ats_service.report_for_vacancy(db_session, profile.id, vacancy_id) is None


async def test_a_generated_document_is_audited_against_the_same_requirements(
    db_session: AsyncSession,
) -> None:
    """Requirement 1 and requirement 2 meeting: our own output, this vacancy."""
    profile = await ProfileRepository(db_session).create(make_profile(skills=("python",)))
    await db_session.flush()
    vacancy_id = await seed_vacancy(db_session, "ats-generated", ["Python", "Kubernetes"])

    report = await ats_service.audit_generated_for_vacancy(
        db_session,
        "Здравствуйте! Работал с Python в двух проектах.",
        kind=DocumentKind.COVER_LETTER,
        profile_id=profile.id,
        vacancy_id=vacancy_id,
    )

    assert report.keywords is not None
    assert [r.requirement for r in report.keywords.present] == ["Python"]
    assert [r.requirement for r in report.keywords.absent] == ["Kubernetes"]


def test_matching_needs_no_database_at_all() -> None:
    """The comparison is a pure function; the service only fetches its inputs.

    Worth pinning: it is what lets the queue builder audit every letter in a
    batch without a query per item.
    """
    keywords = match_requirements("Python и PostgreSQL", ("Python", "Kubernetes"), HELD)

    assert keywords.literal_coverage == 0.5


def test_a_file_with_no_text_layer_is_not_also_told_which_words_it_missed() -> None:
    """The audit's own rule, applied where the keyword half is joined on.

    Nothing is extracted from the file, so every requirement reads as unnamed —
    and nine of those would bury the one finding that matters. The reading is
    still attached, because "the parser matches none of them" is true and is
    what the screen shows under the banner; it just does not charge twice for
    one defect.
    """
    unreadable = stored_report(
        [
            Finding(
                code=FindingCode.NO_TEXT_LAYER,
                severity=Severity.CRITICAL,
                title="В файле нет текстового слоя",
                explanation="...",
                fix="...",
                penalty=100,
            )
        ]
    )
    report = ats_service.with_keywords(unreadable, keywords_with(KeywordStatus.UNSTATED))

    assert [f.code for f in report.findings] == [FindingCode.NO_TEXT_LAYER]
    assert report.score == 0
    assert report.keywords is not None
