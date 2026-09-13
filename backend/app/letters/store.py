"""Reading the rows a letter is written from, and writing the letter back.

Every query here is explicit about its columns because ``Vacancy.matches``,
``Vacancy.applications`` and ``Match.vacancy`` are all ``lazy="raise"`` — the
models make an accidental N+1 an error rather than a slow afternoon — so a
letter run over a hundred vacancies has to say what it wants.

The queries live in this package rather than under ``db/repositories/`` for the
same reason ``app/sources/hh.py`` keeps its own payload models: they answer one
feature's questions and nothing else asks them. If a second caller ever needs
the queue, that is the moment to move it.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Select, func, nulls_last, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.db.base import uuid7
from app.db.enums import ApplicationStatus, MatchBucket, RequirementSource
from app.db.models import (
    Application,
    CandidateProfile,
    Match,
    ProfileSkill,
    Vacancy,
    VacancySkill,
    VacancySource,
)
from app.letters.context import (
    ProfileFacts,
    SkillFact,
    VacancyFacts,
    letter_max_length,
    role_names,
)
from app.letters.examples import (
    ANSWERED_STATUSES,
    POSITIVE_STATUSES,
    ExamplePool,
    LetterExample,
    OutcomeEvidence,
    grade_of,
)

logger = get_logger(__name__)

#: hh keeps its structured requirement list here. Read through the same helper
#: the rest of the payload is read through, so a source that nests it elsewhere
#: still works.
DERIVED_KEY = "_derived"

#: How many answered applications to read before ranking them. Generous on
#: purpose — the real number today is one — and bounded anyway, because the
#: ranking
#: happens in Python and an unbounded ``SELECT`` over a tracker that grew for a
#: year is a query nobody would notice going slow.
MAX_CANDIDATES = 50


@dataclass(frozen=True, slots=True)
class QueuedVacancy:
    """One vacancy waiting for a letter, with what put it in the queue."""

    vacancy_id: UUID
    title: str
    company: str | None
    score: Decimal
    #: True when an application row already holds a letter for this vacancy.
    has_letter: bool


async def load_vacancy_facts(session: AsyncSession, vacancy_id: UUID) -> VacancyFacts | None:
    """Everything about one vacancy the letter may use, or None if it is gone."""
    vacancy = await session.get(Vacancy, vacancy_id)
    if vacancy is None:
        return None

    raws = [
        source.raw
        for source in (await session.scalars(_sources_of(vacancy_id))).all()
        if isinstance(source.raw, dict)
    ]
    derived = [raw[DERIVED_KEY] for raw in raws if isinstance(raw.get(DERIVED_KEY), dict)]

    return VacancyFacts(
        vacancy_id=vacancy.id,
        title=vacancy.title,
        company=vacancy.company,
        city=vacancy.city,
        description=vacancy.description_md or vacancy.description_raw,
        key_skills=await _required_skills(session, vacancy_id, derived),
        inferred_skills=await _inferred_skills(session, vacancy_id),
        language_requirements=_strings(derived, "language_requirements"),
        work_experience=_work_experience(derived),
        professional_roles=role_names(raws),
        letter_max_length=letter_max_length(raws),
    )


async def load_profile_facts(
    session: AsyncSession, profile_id: UUID | None = None
) -> ProfileFacts | None:
    """The candidate the letters are for: the named profile, or the active one."""
    if profile_id is None:
        profile = await session.scalar(
            select(CandidateProfile)
            .where(CandidateProfile.is_active.is_(True))
            .order_by(CandidateProfile.created_at.desc())
            .limit(1)
        )
    else:
        profile = await session.get(CandidateProfile, profile_id)
    if profile is None:
        return None

    skills = (
        await session.scalars(
            select(ProfileSkill)
            .where(ProfileSkill.profile_id == profile.id)
            .order_by(ProfileSkill.canonical_name)
        )
    ).all()

    return ProfileFacts(
        profile_id=profile.id,
        name=profile.name,
        headline=profile.headline,
        summary=profile.summary,
        seniority=profile.seniority.value if profile.seniority else None,
        total_years=float(profile.total_years) if profile.total_years is not None else None,
        locations=tuple(str(item) for item in profile.locations),
        languages=_languages(profile.languages),
        skills=tuple(_skill_fact(skill) for skill in skills),
    )


async def queue(
    session: AsyncSession,
    *,
    profile_id: UUID,
    limit: int = 10,
    min_score: Decimal | None = None,
    include_written: bool = False,
) -> list[QueuedVacancy]:
    """The best-scoring vacancies that still need a letter, best first.

    "Still need" means no application row holds one. A vacancy whose letter was
    already written is skipped rather than rewritten, so a batch run is safe to
    repeat — the same idempotence rule the connectors follow, for the same
    reason: the expensive call is the one worth not making twice.

    **A filtered vacancy is never queued, whatever its score.** ``filtered``
    means "do not apply" — a language the candidate does not speak, six years
    asked of 1.1 — and a letter for it spends a model call on an application
    that must not go out. Measured 13 Sep 2026: selecting by score alone wrote
    letters for «.NET Backend Developer» (82.03, filtered on experience) and two
    vacancies filtered on English, ahead of apply_now vacancies with none.

    ``min_score`` defaults to ``agent_queue_min_score``, the floor the agent
    queue serves from: a letter below it is written for a vacancy the queue will
    never offer.
    """
    threshold = min_score if min_score is not None else Decimal(settings.agent_queue_min_score)
    written = (
        select(Application.vacancy_id)
        .where(Application.cover_letter.is_not(None))
        .where(Application.vacancy_id == Match.vacancy_id)
    )
    stmt = (
        select(
            Match.vacancy_id,
            Vacancy.title,
            Vacancy.company,
            Match.score,
            written.exists().label("has_letter"),
        )
        .join(Vacancy, Vacancy.id == Match.vacancy_id)
        .where(Match.profile_id == profile_id)
        .where(Match.score >= threshold)
        .where(Match.bucket != MatchBucket.FILTERED)
        .where(Vacancy.is_active.is_(True))
        .where(Vacancy.is_spam.is_(False))
        .order_by(Match.score.desc(), Match.vacancy_id)
        .limit(limit)
    )
    if not include_written:
        stmt = stmt.where(~written.exists())

    rows = (await session.execute(stmt)).all()
    return [
        QueuedVacancy(
            vacancy_id=row.vacancy_id,
            title=row.title,
            company=row.company,
            score=row.score,
            has_letter=bool(row.has_letter),
        )
        for row in rows
    ]


async def save_letter(
    session: AsyncSession,
    *,
    vacancy_id: UUID,
    text: str,
    profile_id: UUID,
    rules_version: str,
) -> tuple[UUID, bool]:
    """Store the letter on this vacancy's application row, and say what happened.

    Returns the row's id and whether it had to be created. The tracker has no
    unique constraint on ``vacancy_id`` — a person may legitimately track two
    attempts at the same job — so this updates the oldest row rather than
    inserting a second one, which is what keeps a repeated batch run idempotent.

    ``rules_version`` is passed in rather than computed: it is the fingerprint of
    the rules that actually judged this text, the workshop's included, and only a
    caller holding a session can read those. The same value
    ``generated_document.rules_version`` carries — one vocabulary for "which
    rules wrote this", across both tables.

    ``profile_id`` is recorded because this letter may later be shown to the
    model as an example of what got an answer, and an example written from a
    different resume would teach it to claim experience this candidate has not
    got. On an existing row it is filled in only when it is empty: a row that
    already names a profile was written from that one, and overwriting it would
    manufacture the false provenance the column exists to prevent.

    The rules version travels with the text, because a letter outlives the
    rules that judged it: the documents screen has to be able to say which set
    produced this one rather than implying today's produced all of them.

    The letter is saved, never sent. Sending is ``agent/``'s, after a human
    confirms it, and nothing in ``backend/`` can do it.
    """
    application = await session.scalar(
        select(Application)
        .where(Application.vacancy_id == vacancy_id)
        .order_by(Application.created_at, Application.id)
        .limit(1)
    )
    if application is None:
        application = Application(
            id=uuid7(),
            vacancy_id=vacancy_id,
            cover_letter=text,
            profile_id=profile_id,
            letter_rules_version=rules_version,
        )
        session.add(application)
        await session.flush()
        return application.id, True

    application.cover_letter = text
    # Overwritten with the letter, unlike ``profile_id`` one line down. They are
    # facts about different things: the profile is provenance of the row and is
    # only ever filled in, while this describes the text that is being replaced
    # right now, and leaving the old number beside a new letter would say a
    # version wrote something it never saw.
    application.letter_rules_version = rules_version
    if application.profile_id is None:
        application.profile_id = profile_id
    await session.flush()
    return application.id, False


async def existing_letter(session: AsyncSession, vacancy_id: UUID) -> str | None:
    """The letter already stored for this vacancy, if there is one."""
    return await session.scalar(
        select(Application.cover_letter)
        .where(Application.vacancy_id == vacancy_id)
        .where(Application.cover_letter.is_not(None))
        .order_by(Application.created_at, Application.id)
        .limit(1)
    )


async def load_examples(
    session: AsyncSession, *, profile_id: UUID, limit: int = MAX_CANDIDATES
) -> ExamplePool:
    """Past letters that got an answer, and the counts describing the pool.

    Returns the candidates and an :class:`app.letters.examples.OutcomeEvidence`
    carrying the figures only a query can produce — how many applications went
    out, how many the employer answered either way, how many of those were
    positive, and how many positives cannot be shown because the text that was
    sent was never recorded. :func:`app.letters.examples.select` fills in the
    three that depend on the vacancy in hand. The counts are computed here
    rather than derived from the candidate list because "two sent, none
    answered" and "two sent, two rejected" are different facts and both are
    invisible in a list of positives, which is empty in either case.

    **The text is ``sent_letter`` and never ``cover_letter``.** Since migration
    ``0008_application_send_record`` the row holds both: the string the agent
    typed into hh's form, written once, and the letter as it stands now, which
    this module overwrites on every ``--force`` regeneration. Only the first is
    evidence about what an employer actually read, and showing the second as
    "the letter that got an interview" would be presenting an unsent letter as a
    successful one — a lie of exactly the kind this feature is not allowed to
    tell, and one reachable through a code path in this same package. A positive
    outcome whose ``sent_letter`` is NULL is therefore counted in
    :attr:`OutcomeEvidence.text_unknown` and shown to nobody. Every row that
    predates the send record is one of those.

    **Only letters written for this profile**, and that is now recorded rather
    than inferred. It used to be reached through the ``match`` row, which does
    not hold it: a match exists for every profile against every vacancy in the
    shared pool, so once this profile had been scored against a vacancy an
    earlier profile applied to, that earlier profile's sent letter was served as
    an example of what to claim. Showing the model a letter written from a
    different resume is the shortest path to a letter claiming experience its
    candidate has not got. The honest fix was an
    ``application.profile_id`` column, and since migration 0009 that is what
    this reads. A row whose profile is NULL — typed into the tracker by hand, or
    written before 0009 — is not evidence about any resume and is not shown.

    **The requirement list is the snapshot when there is one.**
    ``application.vacancy_key_skills`` is what the posting asked for on the day
    it was sent to; the ``vacancy_skill`` rows and the payload are what a
    re-crawl has since made of it. Similarity has to be measured against the
    first, because the letter that got the answer was answering that. NULL means
    the row predates the snapshot and the vacancy is read as it stands now,
    which costs two queries per such candidate — an N+1 that is deliberate and
    bounded: N is the number of answered applications, which is one on this
    account today, and capped by :data:`MAX_CANDIDATES` in any case.
    """
    # Since migration 0009 the row records whose resume it was written from, so
    # this is the fact rather than the proxy it used to be. The old join went
    # through ``match``, and a match row exists for every profile against every
    # vacancy in the shared pool — so once this profile had been scored against a
    # vacancy an earlier profile applied to, that earlier profile's sent letter
    # was served as an example of what to claim. NULL is not this profile: a
    # letter whose provenance is unknown is not evidence about any resume.
    for_profile = select(Application).where(Application.profile_id == profile_id)
    # "Sent" is the predicate ``app/services/agent_queue.py`` already treats as
    # acted on, widened by the agent's own record: ``sent_at`` is set by the
    # process that did the sending, the other two by a person working the
    # kanban, and an application sent by hand is still an application sent.
    sent = (
        Application.sent_at.is_not(None)
        | (Application.status != ApplicationStatus.SAVED)
        | Application.applied_at.is_not(None)
    )
    answered = Application.status.in_(sorted(ANSWERED_STATUSES))
    positive = Application.status.in_(sorted(POSITIVE_STATUSES))

    totals = (
        await session.execute(
            for_profile.with_only_columns(
                func.count().filter(sent).label("sent"),
                func.count().filter(answered).label("answered"),
                func.count().filter(positive).label("positive"),
                func.count()
                .filter(positive & Application.sent_letter.is_(None))
                .label("text_unknown"),
            )
        )
    ).one()

    rows = (
        await session.execute(
            for_profile.with_only_columns(
                Application.vacancy_id,
                Application.status,
                Application.sent_at,
                Application.sent_letter,
                Application.vacancy_key_skills,
                Vacancy.title,
            )
            .join(Vacancy, Vacancy.id == Application.vacancy_id)
            .where(positive)
            .where(Application.sent_letter.is_not(None))
            .order_by(nulls_last(Application.sent_at.desc()), Application.created_at.desc())
            .limit(limit)
        )
    ).all()

    examples: list[LetterExample] = []
    seen: set[UUID] = set()
    for row in rows:
        grade = grade_of(row.status)
        # A vacancy can carry more than one tracker row — two attempts at the
        # same job are legitimate — and the same letter twice in one prompt is
        # not two pieces of evidence.
        if grade is None or row.sent_letter is None or row.vacancy_id in seen:
            continue
        seen.add(row.vacancy_id)
        examples.append(
            LetterExample(
                vacancy_id=row.vacancy_id,
                title=row.title,
                key_skills=await _asked_for(session, row.vacancy_id, row.vacancy_key_skills),
                text=row.sent_letter,
                grade=grade,
                sent_at=row.sent_at,
            )
        )

    return ExamplePool(
        candidates=tuple(examples),
        counts=OutcomeEvidence(
            sent=totals.sent,
            answered=totals.answered,
            positive=totals.positive,
            text_unknown=totals.text_unknown,
        ),
    )


async def _asked_for(session: AsyncSession, vacancy_id: UUID, snapshot: object) -> tuple[str, ...]:
    """What that vacancy asked for: the send-time snapshot, or the row today.

    ``snapshot`` is a JSONB column, so its declared ``list[str]`` is a promise
    nothing enforces — it was last written by a service reading a payload. NULL
    means "not recorded" and falls through to the live rows; ``[]`` means
    "recorded, and the posting named none", which is a real answer and is
    returned as one rather than re-read into a different one.
    """
    if isinstance(snapshot, list):
        return tuple(name.strip() for name in snapshot if isinstance(name, str) and name.strip())
    return await _required_skills(session, vacancy_id, await _derived_of(session, vacancy_id))


async def _derived_of(session: AsyncSession, vacancy_id: UUID) -> list[dict[str, Any]]:
    """The connectors' derived blocks for one vacancy, in payload order."""
    raws = [
        source.raw
        for source in (await session.scalars(_sources_of(vacancy_id))).all()
        if isinstance(source.raw, dict)
    ]
    return [raw[DERIVED_KEY] for raw in raws if isinstance(raw.get(DERIVED_KEY), dict)]


def _sources_of(vacancy_id: UUID) -> Select[tuple[VacancySource]]:
    """Every posting this vacancy was deduplicated from."""
    return select(VacancySource).where(VacancySource.vacancy_id == vacancy_id)


async def _required_skills(
    session: AsyncSession, vacancy_id: UUID, derived: list[dict[str, Any]]
) -> tuple[str, ...]:
    """The vacancy's requirement list, from wherever this row actually holds it.

    Normalised ``vacancy_skill`` rows first, because that is the schema of
    record. Nothing writes them today — phase 3 stores hh's ``keySkills`` in the
    source payload and normalisation into rows is phase 4's — so in practice the
    second branch runs, and it keeps running correctly on the day the first one
    starts producing rows.

    Required skills lead, but nice-to-haves are kept: they are still things the
    employer asked for, and a letter that answers one is answering the vacancy.
    """
    rows = (
        await session.scalars(
            select(VacancySkill)
            .where(VacancySkill.vacancy_id == vacancy_id)
            .order_by(VacancySkill.is_required.desc(), VacancySkill.canonical_name)
        )
    ).all()
    if rows:
        return tuple(row.canonical_name for row in rows)
    return _strings(derived, "key_skills")


async def _inferred_skills(session: AsyncSession, vacancy_id: UUID) -> tuple[str, ...]:
    """Which of those requirements nobody stated — read out of the description.

    A separate read rather than a second return value from the function above,
    because that one has a payload branch with no provenance to report: a
    vacancy answered from ``_derived.key_skills`` has no rows, and every name it
    yields was named by the employer.
    """
    rows = await session.scalars(
        select(VacancySkill.canonical_name)
        .where(
            VacancySkill.vacancy_id == vacancy_id,
            VacancySkill.source == RequirementSource.DESCRIPTION_TEXT,
        )
        .order_by(VacancySkill.canonical_name)
    )
    return tuple(rows.all())


def _strings(derived: list[dict[str, Any]], key: str) -> tuple[str, ...]:
    """One string list out of the derived blocks, deduplicated, order kept."""
    values: list[str] = []
    for block in derived:
        for item in block.get(key) or []:
            if isinstance(item, str) and item.strip() and item.strip() not in values:
                values.append(item.strip())
    return tuple(values)


def _work_experience(derived: list[dict[str, Any]]) -> str | None:
    """What the vacancy asks for, rendered if the payload rendered it.

    hh ships ``workExperience`` as a code (``between1And3``) and its own Russian
    rendering in the page's translations, which the connector keeps in
    ``labels``. The rendering is what a letter can use; the code is a fallback
    so something true is passed on rather than nothing.
    """
    for block in derived:
        labels = block.get("labels")
        if isinstance(labels, dict):
            rendered = labels.get("workExperience")
            if isinstance(rendered, str) and rendered.strip():
                return rendered.strip()
    for block in derived:
        code = block.get("work_experience")
        if isinstance(code, str) and code.strip():
            return code.strip()
    return None


def _languages(stored: Sequence[object]) -> tuple[str, ...]:
    """The profile's languages as "en C1" strings.

    The level vocabulary is not this project's — CEFR from one resume, "native"
    from another — so it is passed through rather than mapped.

    ``Sequence[object]`` rather than the column's declared
    ``list[dict[str, Any]]``: the annotation on a JSONB column is a promise the
    database does not enforce, and this list was last written by an LLM
    extraction. Checking what actually arrived costs one line.
    """
    rendered: list[str] = []
    for item in stored:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").strip()
        level = str(item.get("level") or "").strip()
        text = f"{code} {level}".strip()
        if text:
            rendered.append(text)
    return tuple(rendered)


def _skill_fact(skill: ProfileSkill) -> SkillFact:
    """One profile skill, preferring the spelling the resume actually used.

    ``canonical_name`` is a lookup key: it is lowercase and stripped of
    punctuation, so a letter written from it says "postgresql" where the person
    wrote "PostgreSQL". The first raw name is what they wrote.
    """
    spelling = next(
        (name for name in skill.raw_names if isinstance(name, str) and name.strip()),
        skill.canonical_name,
    )
    return SkillFact(
        canonical_name=skill.canonical_name,
        spelling=spelling.strip(),
        years=float(skill.years) if skill.years is not None else None,
        level=skill.level.value,
    )
