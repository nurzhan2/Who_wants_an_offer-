"""The sequences: store a reference, save a rule, try the rules on a vacancy.

Three jobs, and the interesting one is the middle. Saving a rule is not a write;
it is a write with a gate in front of it, and the gate is
:func:`app.workshop.truth.ensure_truthful`. Every path that can put a rule's
parameters into the database goes through :func:`_refuse_untruthful` —
:func:`create_rule` and :func:`update_rule` both — so a rule that could not be
created is not reachable by editing one that could. That is the whole of the
boundary the brief asks for, and it is one function rather than a convention.

The preview is the workshop's answer to "what do my rules actually do". It
generates a letter for a real vacancy under the rules as they stand and **saves
nothing**: no ``application`` row is touched, no ``cover_letter`` is written.
A preview that wrote into the tracker would let somebody discover their
experiment by finding it in the apply queue, addressed to an employer.

A refusal is a result here rather than an exception. When the hard rules cannot
all be met, :func:`preview_letter` returns a response saying so and naming them,
because that is the most useful thing this feature can tell somebody about a
rule they wrote thirty seconds ago — and the alternative, a 422 the dashboard
renders as a red box, throws away the list of what was broken.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.base import uuid7
from app.db.enums import ReferenceKind
from app.db.models import GenerationRule, ReferenceDocument
from app.letters import examples as few_shot
from app.letters import store as letter_store
from app.letters.context import build_context
from app.letters.generator import LetterUnwritableError, generate
from app.letters.service import Workshop, load_workshop
from app.llm.router import LLMRouter, get_router
from app.schemas.workshop import (
    PreviewResponse,
    ReferenceUpdate,
    RuleCreate,
    RuleUpdate,
    ViolationRead,
)
from app.workshop import store
from app.workshop.rules import BUILTIN_RULES, RuleParams, RuleSpec, RuleViolation
from app.workshop.truth import ensure_truthful

logger = get_logger(__name__)


class VacancyNotFoundError(LookupError):
    """A preview was asked for a vacancy that is not in the database."""


class ProfileNotFoundError(LookupError):
    """A preview was asked for with no resume to write from."""


# ── reference documents ───────────────────────────────────────────────


async def store_reference(
    session: AsyncSession,
    *,
    kind: ReferenceKind,
    title: str,
    note: str | None,
    text: str,
    source_filename: str | None = None,
    source_format: str | None = None,
    size_bytes: int | None = None,
) -> ReferenceDocument:
    """Keep a reference document. The text is already extracted and validated."""
    row = ReferenceDocument(
        id=uuid7(),
        kind=kind,
        title=title.strip(),
        note=(note or "").strip() or None,
        text=text,
        is_active=True,
        source_filename=source_filename,
        source_format=source_format,
        size_bytes=size_bytes,
    )
    session.add(row)
    await session.flush()
    logger.info(
        "workshop.reference_stored",
        reference_id=str(row.id),
        kind=kind.value,
        characters=len(text),
        source_format=source_format,
    )
    return row


async def update_reference(
    session: AsyncSession, row: ReferenceDocument, changes: ReferenceUpdate
) -> ReferenceDocument:
    """Apply the fields a reference may be corrected in. Never the text."""
    if changes.title is not None:
        row.title = changes.title.strip()
    if changes.note is not None:
        row.note = changes.note.strip() or None
    if changes.is_active is not None:
        row.is_active = changes.is_active
    await _flush_and_reload(session, row)
    return row


async def delete_reference(session: AsyncSession, row: ReferenceDocument) -> None:
    """Remove a reference. Nothing else points at it, so nothing else changes."""
    await session.delete(row)
    await session.flush()
    logger.info("workshop.reference_deleted", reference_id=str(row.id))


# ── rules ─────────────────────────────────────────────────────────────


async def create_rule(session: AsyncSession, payload: RuleCreate) -> GenerationRule:
    """Save a new rule, or refuse it."""
    await _refuse_untruthful(session, payload.params)
    row = GenerationRule(
        id=uuid7(),
        kind=payload.params.kind,
        scope=payload.scope,
        severity=payload.severity,
        params=payload.params.model_dump(mode="json"),
        message=payload.message.strip(),
        is_active=payload.is_active,
    )
    session.add(row)
    await session.flush()
    logger.info(
        "workshop.rule_created",
        rule_id=str(row.id),
        kind=row.kind.value,
        scope=row.scope.value,
        severity=row.severity.value,
    )
    return row


async def update_rule(
    session: AsyncSession, row: GenerationRule, changes: RuleUpdate
) -> GenerationRule:
    """Change a stored rule, through the same gate a new one passes.

    A rule whose parameters are replaced is a new rule wearing an old id, so it
    is checked as one. Without that, "create the harmless version, then edit it
    into the dishonest one" would be an open door with a validator beside it.
    """
    if changes.params is not None:
        await _refuse_untruthful(session, changes.params)
        row.kind = changes.params.kind
        row.params = changes.params.model_dump(mode="json")
    if changes.scope is not None:
        row.scope = changes.scope
    if changes.severity is not None:
        row.severity = changes.severity
    if changes.message is not None:
        row.message = changes.message.strip()
    if changes.is_active is not None:
        row.is_active = changes.is_active
    await _flush_and_reload(session, row)
    logger.info("workshop.rule_updated", rule_id=str(row.id), kind=row.kind.value)
    return row


async def _flush_and_reload(session: AsyncSession, row: object) -> None:
    """Write the change, then read back what the server decided.

    ``updated_at`` carries ``onupdate=func.now()``, so an UPDATE leaves it
    expired: the value is the server's and SQLAlchemy has not seen it. Reading
    it afterwards would be lazy IO from whatever context happens to be running,
    which under asyncio is a ``MissingGreenlet`` in the caller rather than a slow
    query. Refreshing here makes the reload explicit and awaited, and the caller
    gets a row it can render.
    """
    await session.flush()
    await session.refresh(row)


async def delete_rule(session: AsyncSession, row: GenerationRule) -> None:
    """Remove a rule. Built-ins never reach here — they have no row and no UUID."""
    await session.delete(row)
    await session.flush()
    logger.info("workshop.rule_deleted", rule_id=str(row.id))


async def _refuse_untruthful(session: AsyncSession, params: RuleParams) -> None:
    """The gate. Every write of a rule's parameters goes through it."""
    held, has_profile = await store.held_skills(session)
    ensure_truthful(params, held=held, has_profile=has_profile)


# ── the trial letter ──────────────────────────────────────────────────


async def preview_letter(
    session: AsyncSession,
    *,
    vacancy_id: UUID,
    profile_id: UUID | None = None,
    router: LLMRouter | None = None,
    workshop: Workshop | None = None,
) -> PreviewResponse:
    """Write one letter for one vacancy under the current rules, and save nothing.

    Everything the real path does except the write: the same context, the same
    examples, the same prompt, the same checks, the same fallback, the same
    refusal. It has to be the same or the preview would be answering a different
    question from the one the owner asked.
    """
    facts = await letter_store.load_vacancy_facts(session, vacancy_id)
    if facts is None:
        raise VacancyNotFoundError(str(vacancy_id))
    profile = await letter_store.load_profile_facts(session, profile_id)
    if profile is None:
        raise ProfileNotFoundError(str(profile_id) if profile_id else "no active profile")

    bench = workshop if workshop is not None else await load_workshop(session)
    context = build_context(facts, profile)
    # The past letters the real path would show this vacancy, and for the same
    # reason: a preview written without them is not a preview of what will be
    # written. Almost always empty — see :mod:`app.letters.examples` — and the
    # count reaches the response either way, so a person can tell which they got.
    pool = await letter_store.load_examples(session, profile_id=profile.profile_id)
    chosen, _ = few_shot.select(pool, context)

    try:
        letter = await generate(
            context,
            router=router or get_router(),
            examples=chosen,
            rules=bench.rules,
            references=bench.references,
        )
    except LetterUnwritableError as exc:
        logger.info(
            "workshop.preview_refused",
            vacancy_id=str(vacancy_id),
            problems=[problem.value for problem in exc.problems],
            broken_rules=[violation.rule_id for violation in exc.violations],
        )
        return PreviewResponse(
            written=False,
            detail=exc.detail,
            broken_rules=tuple(_violation(violation) for violation in exc.violations),
            rules_applied=len(bench.rules),
            references_used=len(bench.references),
        )

    logger.info(
        "workshop.preview_written",
        vacancy_id=str(vacancy_id),
        source=letter.source,
        attempts=letter.attempts,
        characters=len(letter.text),
        rules_applied=len(bench.rules),
        references_used=len(bench.references),
    )
    return PreviewResponse(
        written=True,
        text=letter.text,
        source=letter.source,
        language=letter.language,
        characters=len(letter.text),
        attempts=letter.attempts,
        warnings=tuple(_violation(violation) for violation in letter.warnings),
        rules_applied=len(bench.rules),
        references_used=len(bench.references),
        examples_used=letter.examples_used,
    )


def _violation(violation: RuleViolation) -> ViolationRead:
    """One violation on its way out of the API."""
    return ViolationRead(
        rule_id=violation.rule_id,
        kind=violation.kind,
        severity=violation.severity,
        message=violation.message,
        detail=violation.detail,
    )


# ── listing, for the dashboard ────────────────────────────────────────


async def rules_for_display(session: AsyncSession) -> tuple[RuleSpec, ...]:
    """Every rule the owner can see: the built-ins, then their own.

    Not filtered by scope and not filtered by active: this is the editing view,
    and a rule switched off is exactly the one somebody is looking for when they
    wonder why nothing is being checked.
    """
    return BUILTIN_RULES + await store.stored_rules(session)


async def reference_kinds_in_use(session: AsyncSession) -> dict[ReferenceKind, int]:
    """How many active references there are of each kind, for the dashboard."""
    counts: dict[ReferenceKind, int] = {kind: 0 for kind in ReferenceKind}
    for row in await store.references(session, active_only=True):
        counts[row.kind] += 1
    return counts


__all__ = [
    "ProfileNotFoundError",
    "VacancyNotFoundError",
    "create_rule",
    "delete_reference",
    "delete_rule",
    "preview_letter",
    "reference_kinds_in_use",
    "rules_for_display",
    "store_reference",
    "update_reference",
    "update_rule",
]
