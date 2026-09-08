"""Reading the workshop's rows, and writing them back.

The queries live in this package rather than under ``db/repositories/`` for the
reason ``app/letters/store.py`` gives about its own: they answer one feature's
questions and nothing else asks them. If a second caller ever needs the active
rule set, that is the moment to move them.

One thing here is not a query and belongs next to them anyway.
:func:`active_rules` returns the built-ins **and** the stored rows, always, in
that order. Every caller that is about to check a document goes through it, so
there is no path on which a built-in can be left out by forgetting it — which is
the difference between "undeletable" and "undeletable unless you use the other
function".
"""

from uuid import UUID

from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.enums import ReferenceKind, RuleScope
from app.db.models import (
    CandidateProfile,
    GenerationRule,
    ProfileSkill,
    ReferenceDocument,
    Vacancy,
)
from app.letters.context import fold
from app.workshop.references import ReferenceText
from app.workshop.rules import BUILTIN_RULES, RuleParams, RuleSpec

logger = get_logger(__name__)

#: Validates a stored ``params`` blob back into the model that wrote it. A
#: module-level adapter rather than one per call: building it walks the whole
#: discriminated union, and this runs once per rule per generation.
PARAMS = TypeAdapter[RuleParams](RuleParams)


class MalformedRuleError(ValueError):
    """A stored rule's parameters do not validate.

    Reachable in exactly one way — a row written by something other than this
    application, or one left behind by a change to the parameter models — and it
    is raised rather than skipped. A rule that cannot be read is a rule nobody
    is enforcing, and dropping it quietly would turn "my documents are checked"
    into "some of my documents are checked" with nothing to see.
    """


def spec_of(row: GenerationRule) -> RuleSpec:
    """One stored row as the thing the checker and the prompt both consume."""
    try:
        params = PARAMS.validate_python(row.params)
    except ValueError as exc:
        raise MalformedRuleError(
            f"rule {row.id} stores parameters that do not validate as {row.kind.value}: {exc}"
        ) from exc
    if params.kind is not row.kind:
        raise MalformedRuleError(
            f"rule {row.id} is stored as {row.kind.value} but its parameters say "
            f"{params.kind.value}; the row disagrees with itself"
        )
    return RuleSpec(
        id=str(row.id),
        scope=row.scope,
        severity=row.severity,
        params=params,
        message=row.message,
        is_active=row.is_active,
        is_builtin=False,
    )


async def stored_rules(
    session: AsyncSession, *, scope: RuleScope | None = None, active_only: bool = False
) -> tuple[RuleSpec, ...]:
    """Every rule the owner wrote, oldest first.

    Oldest first because the list is edited by a person and a stable order is
    what makes it editable; nothing about checking depends on it.
    """
    statement = select(GenerationRule).order_by(GenerationRule.created_at, GenerationRule.id)
    if scope is not None:
        statement = statement.where(GenerationRule.scope.in_((scope, RuleScope.BOTH)))
    if active_only:
        statement = statement.where(GenerationRule.is_active.is_(True))
    rows = (await session.scalars(statement)).all()
    return tuple(spec_of(row) for row in rows)


async def active_rules(session: AsyncSession, *, scope: RuleScope) -> tuple[RuleSpec, ...]:
    """The rules a document of this scope is actually checked against.

    Built-ins first and unconditionally. They are not read from the database and
    cannot be switched off; see :data:`app.workshop.rules.BUILTIN_RULES` for why
    they are constants rather than seeded rows.
    """
    builtin = tuple(rule for rule in BUILTIN_RULES if rule.applies_to(scope))
    return builtin + await stored_rules(session, scope=scope, active_only=True)


async def get_rule(session: AsyncSession, rule_id: UUID) -> GenerationRule | None:
    """One stored rule row, or None."""
    return await session.get(GenerationRule, rule_id)


async def references(
    session: AsyncSession, *, kind: ReferenceKind | None = None, active_only: bool = False
) -> tuple[ReferenceDocument, ...]:
    """The reference documents, newest first.

    Newest first because a reference is added when the owner has decided their
    documents should look more like this one, and the newest is the current
    answer to that.
    """
    statement = select(ReferenceDocument).order_by(
        ReferenceDocument.created_at.desc(), ReferenceDocument.id
    )
    if kind is not None:
        statement = statement.where(ReferenceDocument.kind == kind)
    if active_only:
        statement = statement.where(ReferenceDocument.is_active.is_(True))
    return tuple((await session.scalars(statement)).all())


async def get_reference(session: AsyncSession, reference_id: UUID) -> ReferenceDocument | None:
    """One reference row, or None."""
    return await session.get(ReferenceDocument, reference_id)


def reference_text_of(row: ReferenceDocument) -> ReferenceText:
    """One stored reference as the prompt builder consumes it."""
    return ReferenceText(
        id=str(row.id),
        kind=row.kind,
        title=row.title,
        note=row.note,
        text=row.text,
    )


async def active_references(
    session: AsyncSession, *, kind: ReferenceKind
) -> tuple[ReferenceText, ...]:
    """The references of one kind that a generation is allowed to be shown."""
    rows = await references(session, kind=kind, active_only=True)
    return tuple(reference_text_of(row) for row in rows)


async def choosable_vacancies(
    session: AsyncSession, *, query: str | None = None, limit: int = 20
) -> tuple[Vacancy, ...]:
    """Vacancies a trial letter can be written for, newest first.

    Only what the preview needs, and it lives here rather than in a vacancy
    list endpoint because there is not one yet — the dashboard's own list is
    phase 7's. The moment it lands, this is the query to delete rather than the
    one to keep beside it.

    Active and not spam, because a preview is meant to answer "what would my
    rules do to a real letter" and a dead posting is not a real letter.
    """
    statement = (
        select(Vacancy)
        .where(Vacancy.is_active.is_(True))
        .where(Vacancy.is_spam.is_(False))
        .order_by(Vacancy.created_at.desc(), Vacancy.id)
        .limit(limit)
    )
    if query and query.strip():
        pattern = f"%{query.strip()}%"
        statement = statement.where(Vacancy.title.ilike(pattern) | Vacancy.company.ilike(pattern))
    return tuple((await session.scalars(statement)).all())


async def held_skills(
    session: AsyncSession, *, profile_id: UUID | None = None
) -> tuple[frozenset[str], bool]:
    """Every skill the profile holds, folded, and whether a profile exists.

    Two values because "no profile" and "a profile with no skills" are different
    facts and only one of them is fixed by uploading a resume — see
    :func:`app.workshop.truth.ensure_truthful`, which says which of the two
    messages a person is shown.

    The fold is :func:`app.letters.context.fold`, so a rule naming ``Node.js``
    and a resume saying ``nodejs`` are the same skill here and in the letter.
    """
    # Asked for by id or not, the row has to exist: a caller naming a profile
    # that was deleted is in the same position as one with no profile at all,
    # and answering "no skills, but there is a profile" would send them looking
    # for a resume to correct rather than for one to upload.
    found = await session.scalar(
        select(CandidateProfile.id).where(CandidateProfile.id == profile_id)
        if profile_id is not None
        else select(CandidateProfile.id)
        .where(CandidateProfile.is_active.is_(True))
        .order_by(CandidateProfile.created_at.desc())
        .limit(1)
    )
    if found is None:
        return frozenset(), False
    profile_id = found

    names = (
        await session.scalars(
            select(ProfileSkill.canonical_name).where(ProfileSkill.profile_id == profile_id)
        )
    ).all()
    return frozenset(key for key in (fold(name) for name in names) if key), True
