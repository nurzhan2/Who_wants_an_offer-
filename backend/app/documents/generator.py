"""Arranging one CV: ask, check the answer, ask once more, then fall back.

The shape CLAUDE.md prescribes for every LLM feature — validate with a Pydantic
model, one retry carrying the error, then a rule-based fallback — with the same
two kinds of wrong answer the letter generator distinguishes:

* **the wrong shape**, which the provider handles: it validates
  :class:`CVDraft` and feeds the validator's own message back;
* **the wrong content**, which is this module's business, and which here means
  an arrangement that names a skill the profile has not got, renames one skill
  into another, refers to a job that does not exist, or attributes a technology
  to an employer the resume never connected it to.

The second check is code reading the answer and then code reading the finished
document, never an instruction in the prompt. The prompt does say all of it —
that raises the first-pass hit rate and costs nothing — but what makes it true is
:mod:`app.documents.guard` running twice and the arrangement being thrown away
when it fails.

**The fallback is a real CV, not a stub.** ``compose_fallback`` arranges the same
document from the context alone: skills ordered by the vacancy's own requirement
list first, jobs in the order the resume gave them, each job's whole recorded
stack, the profile's own summary. It is not tailored beyond that ordering, and
that is the entire difference between it and a model's answer — the facts are
identical, because in both cases the facts come from the database. A person can
see which they got from :attr:`GeneratedCV.source`.

**The fallback is checked too**, and by the same functions. "Safe by
construction" is an argument, not a guarantee: the fallback is assembled from
the profile, so it cannot invent a skill, but it can still be too short to be a
CV — a profile with two skills and no jobs produces a name and a heading. So it
goes through :func:`app.documents.guard.check_document` like the model's answer,
and when even it fails, :func:`generate` raises :class:`CVUnwritableError`
rather than returning something the caller will store and hand over.

**What this module does not do is render.** It produces an arrangement and the
text that arrangement makes; turning that into a file and auditing the file
belong to :mod:`app.documents.render` and :mod:`app.documents.review`, because
the checks here have to run before anything is written anywhere.
"""

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.exceptions import AppError, LLMError
from app.core.logging import get_logger
from app.documents import guard
from app.documents import prompt as prompt_builder
from app.documents.context import (
    MAX_EXPERIENCE_ENTRIES,
    MAX_SUMMARY_CHARS,
    CVContext,
    SkillChoice,
)
from app.documents.guard import CVProblem
from app.documents.render import CVArrangement, to_text
from app.letters.context import fold
from app.letters.prompt import FENCE_CLOSE, FENCE_OPEN
from app.llm import usage as usage_ledger
from app.llm.base import LLMTask, LLMUsage
from app.llm.router import LLMRouter, get_router

logger = get_logger(__name__)

#: Which task this is. Provider, model, effort and tool policy all follow from
#: it through configuration; like COVER_LETTER it is denied every tool, because
#: its prompt carries a vacancy description fetched from a job board.
TASK = LLMTask.CV_TAILORING

PROMPT_NAME = "tailored_cv"

#: One arrangement, then one correction. The same figure the letter generator
#: settled on, and for the same reason: a third attempt has not been observed to
#: fix what a second did not, and the fallback is always available.
MAX_ATTEMPTS = 2

#: Where the arrangement came from.
CVSource = Literal["model", "fallback"]


class CVUnwritableError(AppError):
    """No arrangement could be produced that passes every hard rule.

    Raised only when the rule-based arrangement itself fails the checks, which
    means the profile has too little in it to make a CV from — no jobs and
    almost no skills. The alternative is storing a two-line document under the
    owner's name and reporting it as generated, and there is no way for them to
    find out that happened until an employer opens it.

    Declared here rather than in ``app/core/exceptions.py`` because that file
    belongs to another change; it still subclasses :class:`AppError`, so the
    problem+json handler renders it like every other domain failure.
    """

    #: 422: the request was understood and the data in it cannot yield a CV.
    status_code = 422
    title = "No usable CV could be generated"
    problem_type = "cv-unwritable"

    def __init__(self, detail: str, *, problems: tuple[CVProblem, ...] = ()) -> None:
        super().__init__(detail, problems=[problem.value for problem in problems])
        self.problems = problems


class DraftSkill(BaseModel):
    """One skill in the model's answer: which skill, and what to call it."""

    model_config = ConfigDict(extra="forbid")

    canonical_name: str
    #: Defaults to the canonical name, so a model that omits it has chosen not
    #: to rename rather than chosen to print an empty string.
    shown_as: str = ""


class DraftJob(BaseModel):
    """One job in the model's answer: which job, and which of its technologies."""

    model_config = ConfigDict(extra="forbid")

    ref: int
    stack: list[str] = Field(default_factory=list)


class CVDraft(BaseModel):
    """What the model must return. Shape only — the content checks are code.

    Worth reading as a list of what a model is *able* to say, because that is
    the design. There is no company field, no title field, no date field, no
    level field and no per-job description: not because a model would fill them
    in badly, but so that the question of whether it filled them in badly never
    arises.
    """

    model_config = ConfigDict(extra="forbid")

    headline: str = ""
    summary: str = ""
    skills: list[DraftSkill] = Field(default_factory=list)
    experience: list[DraftJob] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class GeneratedCV:
    """One arrangement, the text it makes, and how it was arrived at.

    A dataclass rather than a Pydantic model for the same reason
    :class:`app.letters.generator.GeneratedLetter` is one: it carries
    ``LLMUsage`` objects and never crosses a serialisation boundary. The
    arrangement inside it does — and that is a Pydantic model.
    """

    arrangement: CVArrangement
    text: str
    source: CVSource
    attempts: int
    #: Everything the checks caught on the way, in order. Empty on a clean first
    #: answer. Kept even when a later attempt succeeded: "the model wanted to put
    #: Kubernetes in it" is worth seeing in a log.
    rejected_for: tuple[CVProblem, ...] = ()
    usages: tuple[LLMUsage, ...] = ()

    @property
    def cost_usd(self) -> float | None:
        """What the attempts cost, or None when no model priced them."""
        priced = [usage.cost_usd for usage in self.usages if usage.cost_usd is not None]
        return sum(priced) if priced else None


def to_arrangement(draft: CVDraft, context: CVContext) -> CVArrangement:
    """Turn a validated draft into an arrangement, normalising what it omitted.

    Two normalisations, both of which turn an incomplete answer into a defined
    one rather than into a rejection:

    * a skill with no ``shown_as`` is shown under its canonical name;
    * the job list is truncated at :data:`app.documents.context.
      MAX_EXPERIENCE_ENTRIES`, which the prompt states, so a model that returned
      every job gets the ceiling applied rather than the whole answer thrown
      away over a rule about length.

    Duplicate skills are collapsed, keeping the first: a repeated entry is a
    model listing something twice, and a CV that names PostgreSQL three times
    reads worse than one that names it once. Duplicate refs are collapsed for a
    stronger reason — the same job printed twice is a document that misrepresents
    a career, and it is the sort of thing nobody notices in review.
    """
    skills: list[SkillChoice] = []
    seen_skills: set[str] = set()
    for item in draft.skills:
        key = fold(item.canonical_name)
        if key in seen_skills:
            continue
        seen_skills.add(key)
        shown = item.shown_as.strip() or item.canonical_name.strip()
        skills.append(SkillChoice(canonical_name=item.canonical_name.strip(), shown_as=shown))

    jobs: list[tuple[int, tuple[str, ...]]] = []
    seen_refs: set[int] = set()
    for job in draft.experience:
        if job.ref in seen_refs:
            continue
        seen_refs.add(job.ref)
        jobs.append((job.ref, tuple(name.strip() for name in job.stack if name.strip())))

    return CVArrangement(
        headline=draft.headline.strip(),
        summary=draft.summary.strip(),
        skills=tuple(skills),
        experience=tuple(jobs[:MAX_EXPERIENCE_ENTRIES]),
    )


def inspect(arrangement: CVArrangement, context: CVContext) -> list[CVProblem]:
    """Every hard rule, checked against the arrangement and the document it makes.

    Two passes, and both are needed. The arrangement checks catch the faults that
    are about *provenance* — a skill that is not the candidate's, a job that does
    not exist, a technology moved between employers — and they can name what is
    wrong precisely enough for a retry to fix it. The document check catches what
    is about the *finished text*: it renders the arrangement and searches the
    result for any requirement the candidate does not hold, which is the check
    the brief's own example turns on.

    The order matters for the feedback message, not for the verdict: a person and
    a retrying model both read the specific fault before the general one.
    """
    problems: list[CVProblem] = []

    if arrangement.headline and arrangement.headline not in context.allowed_headlines:
        problems.append(CVProblem.HEADLINE_NOT_SUPPORTED)
    problems.extend(guard.check_skills(arrangement.skills, context))
    problems.extend(guard.check_experience(arrangement.shown_experience, context))
    problems.extend(guard.check_summary(arrangement.summary, context))

    text = to_text(arrangement, context)
    if FENCE_OPEN in text or FENCE_CLOSE in text:
        problems.append(CVProblem.LEAKED_FENCE)
    problems.extend(guard.check_document(text, context))

    # Deduplicated, order kept: the document check and the summary check can
    # both report an unsupported claim, and telling a model the same thing twice
    # in one feedback message reads as two separate faults.
    return list(dict.fromkeys(problems))


async def generate(context: CVContext, *, router: LLMRouter | None = None) -> GeneratedCV:
    """One arrangement for one vacancy, guaranteed to pass every hard rule.

    The guarantee is the return type's and it is checked rather than argued: a
    ``GeneratedCV`` coming out of here has been through :func:`inspect` clean,
    whichever branch produced it.

    A bad model answer is not an error — it becomes the rule-based arrangement,
    and the caller can see from ``source`` which it got. A rule-based arrangement
    that is itself unusable *is* an error and raises :class:`CVUnwritableError`:
    there is nothing left to fall back to, and both ways of not saying so —
    storing the stub, or returning it and letting the service call that success —
    end with an employer reading it.
    """
    router = router or get_router()
    usages: list[LLMUsage] = []
    seen: list[CVProblem] = []
    feedback = ""

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            result = await router.complete_json(
                PROMPT_NAME,
                CVDraft,
                task=TASK,
                variables=prompt_builder.variables(context, feedback=feedback),
            )
        except LLMError as exc:
            # The provider is gone, or it failed to produce the shape twice.
            # Both mean there is no answer to check; the fallback is the answer.
            # Narrow on purpose: a bug in this module must still raise.
            logger.warning(
                "documents.generate.no_model_answer",
                vacancy_id=str(context.vacancy.vacancy_id),
                attempt=attempt,
                reason=type(exc).__name__,
            )
            break

        usages.append(usage_ledger.record(result.usage))
        arrangement = to_arrangement(result.value, context)
        problems = inspect(arrangement, context)
        if not problems:
            return GeneratedCV(
                arrangement=arrangement,
                text=to_text(arrangement, context),
                source="model",
                attempts=attempt,
                rejected_for=tuple(seen),
                usages=tuple(usages),
            )

        seen.extend(problems)
        feedback = guard.feedback_for(problems)
        logger.warning(
            "documents.generate.rejected",
            vacancy_id=str(context.vacancy.vacancy_id),
            attempt=attempt,
            problems=[problem.value for problem in problems],
        )

    fallback = compose_fallback(context)
    logger.warning(
        "documents.generate.fell_back",
        vacancy_id=str(context.vacancy.vacancy_id),
        problems=[problem.value for problem in seen],
    )

    remaining = inspect(fallback, context)
    if remaining:
        logger.error(
            "documents.generate.fallback_unusable",
            vacancy_id=str(context.vacancy.vacancy_id),
            problems=[problem.value for problem in remaining],
            skills=len(context.profile.skills),
            jobs=len(context.experience),
        )
        raise CVUnwritableError(
            "the rule-based arrangement fails the checks too, so there is nothing "
            "left to fall back to: this profile has too little recorded in it to "
            "make a CV from",
            problems=tuple(remaining),
        )

    return GeneratedCV(
        arrangement=fallback,
        text=to_text(fallback, context),
        source="fallback",
        attempts=len(usages),
        rejected_for=tuple(seen),
        usages=tuple(usages),
    )


def compose_fallback(context: CVContext) -> CVArrangement:
    """An arrangement built from the context alone, with no model involved.

    Correct, complete and untailored beyond one ordering rule: skills the
    vacancy asked for come first, in the vacancy's own order and under the
    vacancy's own spelling, and everything else follows in the order the profile
    holds it. Jobs stay in the resume's order — reverse chronological, as they
    were written — and each shows its whole recorded stack, because narrowing a
    stack is a judgement about relevance and this branch makes no judgements.

    The vacancy's spelling is used for a matched skill here, and that is not a
    liberty: ``matched`` means the fold keys agree, so the two strings are two
    names for one thing, and the employer's own word is the one their parser is
    searching for. It is the single piece of tailoring that needs no model,
    which is why it is the piece this branch keeps.

    The summary is the profile's own, and only when it passes the same check the
    model's would: it was written by a person about themselves, but it is prose,
    and prose is where an unsupported claim lives. A summary that names a
    requirement the profile does not record is dropped rather than the document
    being failed — nothing is lost, since the same words are already in the
    resume the owner uploaded.
    """
    by_key = {fold(skill.canonical_name): skill for skill in context.profile.skills}
    by_key.pop("", None)

    skills: list[SkillChoice] = []
    used: set[str] = set()
    for requirement in context.vacancy.key_skills:
        key = fold(requirement)
        held = by_key.get(key)
        if held is None or key in used:
            continue
        used.add(key)
        skills.append(SkillChoice(canonical_name=held.canonical_name, shown_as=requirement.strip()))
    for key, skill in by_key.items():
        if key not in used:
            skills.append(SkillChoice(canonical_name=skill.canonical_name, shown_as=skill.spelling))

    summary = (context.profile.summary or "").strip()
    if len(summary) > MAX_SUMMARY_CHARS or guard.unsupported_mentions(summary, context):
        summary = ""

    return CVArrangement(
        headline=context.allowed_headlines[0] if context.allowed_headlines else "",
        summary=summary,
        skills=tuple(skills),
        experience=tuple((entry.ref, entry.stack) for entry in context.experience),
    )
