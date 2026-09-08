"""One ATS report, for whichever screen is asking.

The audit answers three questions that used to belong to three different
moments: can a parser read this file, how much of it survives the reading, and
does it say the words *this* vacancy's filter searches for. Only the third needs
a vacancy, and it is the one every screen in the application flow wants — the
vacancy page, the preview before a document is generated, the confirmation card
seconds before an application is sent.

**One assembly, three callers.** They render the same
:class:`app.schemas.ats.ATSReport` and none of them computes anything: a card
that scored the same document differently from the page the person read ten
minutes earlier would make both numbers worthless, and that is the failure mode
a second implementation always produces eventually.

**Where the two halves come from.** The structural half is read back from the
profile, where it was stored at upload — the file itself is deleted when parsing
ends, so it cannot be recomputed and is never silently recomputed as "clean".
The keyword half is computed here, from ``candidate_profile.raw_text``, which is
the text layer the audit was measuring in the first place, against the vacancy's
own requirement strings.

**Why the requirement strings come from the payload and not from the rows.**
``vacancy_skill.canonical_name`` is a lookup key: ``app.normalize.requirements``
casefolds it or replaces it with a dictionary entry, so the employer's own
spelling — the string their filter searches for — is exactly what those rows do
not keep. ``vacancy_source.raw["_derived"]["key_skills"]`` does keep it. The
rows are read anyway, for the one thing they hold and the payload does not:
which requirements are hard.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import ProfileSkill, VacancySkill, VacancySource
from app.db.repositories.profile import ProfileRepository
from app.resume import ats_audit
from app.resume.ats_keywords import HeldSkill, match_requirements
from app.resume.skills import fold
from app.schemas.ats import ATSKeywords, ATSReport, DocumentKind, FindingCode

logger = get_logger(__name__)

#: Where a connector leaves what it extracted from a posting.
DERIVED_KEY = "_derived"


async def held_skills(session: AsyncSession, profile_id: UUID) -> list[HeldSkill]:
    """What the profile says this candidate has, with the spellings they used.

    The evidence for calling a requirement "unstated" rather than "missing", so
    it is read from the profile rather than from anything the document says
    about itself.
    """
    rows = (
        await session.scalars(
            select(ProfileSkill)
            .where(ProfileSkill.profile_id == profile_id)
            .order_by(ProfileSkill.canonical_name)
        )
    ).all()
    return [
        HeldSkill(
            canonical_name=row.canonical_name,
            spellings=tuple(
                name.strip() for name in row.raw_names if isinstance(name, str) and name.strip()
            ),
        )
        for row in rows
    ]


async def requirements_of(session: AsyncSession, vacancy_id: UUID) -> tuple[list[str], list[bool]]:
    """A vacancy's requirements as the posting spelled them, and which are hard.

    Payload first for the spelling, rows second for the hardness, and rows alone
    only when no payload carries a list. That last case is a degraded answer and
    is worth naming: the row names are folded, so a literal check against them
    compares the document with a lowercased string the employer never wrote. It
    is still better than reporting a vacancy as asking for nothing.
    """
    rows = (
        await session.scalars(
            select(VacancySkill)
            .where(VacancySkill.vacancy_id == vacancy_id)
            .order_by(VacancySkill.is_required.desc(), VacancySkill.canonical_name)
        )
    ).all()
    hardness = {row.canonical_name: row.is_required for row in rows}

    spellings = _key_skills(await _derived_of(session, vacancy_id))
    if not spellings:
        if rows:
            logger.info("ats.requirements_from_rows", vacancy_id=str(vacancy_id), count=len(rows))
        return [row.canonical_name for row in rows], [row.is_required for row in rows]

    # Matched back through the same fold the rows were written under, so a
    # verbatim spelling keeps the hardness its normalised row carries.
    by_fold = {fold(name): required for name, required in hardness.items()}
    return spellings, [by_fold.get(fold(name), True) for name in spellings]


async def report_for_vacancy(
    session: AsyncSession, profile_id: UUID, vacancy_id: UUID
) -> ATSReport | None:
    """The stored readability audit, read against one vacancy's requirements.

    ``None`` when there is no stored report — a profile uploaded before the
    audit existed, or one whose upload predates it. Deliberately not a report
    with an empty structural half: absence of an audit is not a clean audit, and
    a caller that cannot tell them apart will show the second.
    """
    profiles = ProfileRepository(session)
    stored = await profiles.get_ats_report(profile_id)
    if stored is None:
        return None

    profile = await profiles.get(profile_id)
    text = (profile.raw_text if profile else None) or ""
    requirements, required = await requirements_of(session, vacancy_id)
    keywords = match_requirements(
        text, requirements, await held_skills(session, profile_id), required=required
    )
    return with_keywords(stored, keywords)


def with_keywords(report: ATSReport, keywords: ATSKeywords) -> ATSReport:
    """Attach a keyword reading to a stored structural report.

    Idempotent: any keyword finding already on the report is dropped before the
    new one is added, so reading the same stored report against a second vacancy
    cannot leave the first vacancy's gaps on it.

    The score is recomputed from the findings rather than carried over, because
    the score is defined as the arithmetic of the findings, and adding one
    without redoing the arithmetic would leave the report explaining a number it
    no longer holds.
    """
    kept = [f for f in report.findings if f.code is not FindingCode.REQUIREMENTS_NOT_NAMED]
    unnamed = ats_audit.check_requirements(keywords)
    if any(f.code is FindingCode.NO_TEXT_LAYER for f in kept):
        # The same rule the audit itself applies: a file a parser extracts
        # nothing from cannot also be told which words it failed to say. Every
        # requirement would read as unnamed, and nine of those would bury the
        # one finding that matters. The keyword reading is still attached — "the
        # parser matches none of them" is true, and it is what the screen shows
        # under the banner — but it does not add a second penalty for the same
        # defect.
        unnamed = None
    findings = [*kept, *([unnamed] if unnamed is not None else [])]
    codes = [*report.checks_run]
    if FindingCode.REQUIREMENTS_NOT_NAMED not in codes:
        codes.append(FindingCode.REQUIREMENTS_NOT_NAMED)
    return report.model_copy(
        update={
            "findings": findings,
            "score": max(0, 100 - sum(finding.penalty for finding in findings)),
            "keywords": keywords,
            "checks_run": codes,
        }
    )


async def audit_generated_for_vacancy(
    session: AsyncSession,
    text: str,
    *,
    kind: DocumentKind,
    profile_id: UUID,
    vacancy_id: UUID,
) -> ATSReport:
    """Audit a document this system produced, against the vacancy it is for.

    The point of requirement 1: nothing generated reaches a person unaudited.
    Everything here is computed rather than stored, because a generated document
    is audited on the way out and the report belongs to that document, not to
    the profile.
    """
    requirements, required = await requirements_of(session, vacancy_id)
    keywords = match_requirements(
        text, requirements, await held_skills(session, profile_id), required=required
    )
    return ats_audit.audit_generated(text, kind=kind, keywords=keywords)


async def _derived_of(session: AsyncSession, vacancy_id: UUID) -> list[dict[str, Any]]:
    """The connectors' derived blocks for one vacancy, in payload order.

    ``dict[str, Any]`` because a JSONB column is a source's own shape validated
    by nobody; every read below checks what actually arrived.
    """
    raws = [
        source.raw
        for source in (
            await session.scalars(
                select(VacancySource).where(VacancySource.vacancy_id == vacancy_id)
            )
        ).all()
        if isinstance(source.raw, dict)
    ]
    return [raw[DERIVED_KEY] for raw in raws if isinstance(raw.get(DERIVED_KEY), dict)]


def _key_skills(derived: list[dict[str, Any]]) -> list[str]:
    """The requirement strings, deduplicated by fold, order and spelling kept."""
    values: list[str] = []
    seen: set[str] = set()
    for block in derived:
        for item in block.get("key_skills") or []:
            if not isinstance(item, str) or not item.strip():
                continue
            key = fold(item)
            if key and key not in seen:
                seen.add(key)
                values.append(item.strip())
    return values
