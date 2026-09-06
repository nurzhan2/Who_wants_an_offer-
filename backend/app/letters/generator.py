"""Generating one letter: ask, check the answer, ask once more, then fall back.

The shape CLAUDE.md prescribes for every LLM feature in this project — validate
with a Pydantic model, one retry carrying the error, then a rule-based fallback
— applies here twice over, because there are two different kinds of wrong answer:

* **the wrong shape**, which the provider handles: it validates
  :class:`CoverLetterDraft` and feeds the validator's own message back;
* **the wrong content**, which is this module's business. A letter can be
  perfectly valid JSON and still carry a link, run past the vacancy's ceiling,
  or claim experience the candidate does not have.

The second check is code reading the finished text, never an instruction in the
prompt. The prompt does say "no links" — that raises the first-pass hit rate and
costs nothing — but it is not what makes the rule true. What makes it true is
:func:`app.letters.guard.find_problems` running over the answer, and the letter
being thrown away when it fails.

When both attempts fail, :func:`compose_fallback` writes a letter from the
context alone: no model, no invention, nothing in it that was not already in the
database. It is worse prose than the model's, which is the right way round for
something a person will read before sending.

**The fallback is checked too.** "Safe by construction" is an argument, not a
guarantee, and it is wrong in at least one reachable case: a vacancy whose title
reads as a domain, no company, no overlap and no headline yields a hundred
characters of greeting and sign-off, which is under
:data:`app.letters.guard.MIN_LENGTH`. So the fallback goes through the same
:func:`app.letters.guard.find_problems` as the model's answer, and when even it
fails, :func:`generate` raises :class:`LetterUnwritableError` rather than
returning something the caller will save. Saving a letter that breaks a hard
constraint and reporting success is the one outcome nobody can recover from: it
is discovered by the employer.

Nothing here sends anything. The letter is generated and saved; sending is the
agent's job, and only after a human has confirmed it.
"""

# ruff: noqa: RUF001 - the fallback letter is Russian prose, which is
# what the homoglyph guard cannot tell from a homoglyph attack. Same exemption
# the project grants app/sources/hh.py and agent/*.py in pyproject.toml,
# declared here because pyproject.toml belongs to another change.

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.exceptions import AppError, LLMError
from app.core.logging import get_logger
from app.letters import prompt as prompt_builder
from app.letters.context import LetterContext, MatchedSkill, fold
from app.letters.guard import ENGLISH, LetterProblem, find_problems, is_safe
from app.llm import usage as usage_ledger
from app.llm.base import LLMTask, LLMUsage
from app.llm.router import LLMRouter, get_router

logger = get_logger(__name__)

#: Which task this is. Provider, model, effort and tool policy all follow from
#: it through configuration — COVER_LETTER is already in the heavy tasks and is
#: already denied every tool, both decided in ``app/llm/base.py``.
TASK = LLMTask.COVER_LETTER

PROMPT_NAME = "cover_letter"

#: One generation, then one correction. A third attempt has never been observed
#: to fix what a second did not, and the fallback is always available.
MAX_ATTEMPTS = 2

#: Where the saved text came from. Recorded because the two are not the same
#: thing to a person deciding whether to send it.
LetterSource = Literal["model", "fallback"]


class LetterUnwritableError(AppError):
    """No text could be produced that passes every hard constraint.

    Raised only when the rule-based fallback itself fails the checks, which
    means the context has too little in it to say anything with — an empty
    profile, or a vacancy whose every nameable field reads as a web address.
    The alternative is saving a stub under the owner's name and reporting it as
    written, and there is no way for the owner to find out that happened until
    an employer reads it.

    Declared here rather than in ``app/core/exceptions.py`` because that file
    belongs to another change; it still subclasses :class:`AppError`, so the
    problem+json handler renders it like every other domain failure.
    """

    #: 422: the request was understood and the data in it cannot yield a letter.
    #: A literal rather than ``fastapi.status``, to keep the letters package free
    #: of a web framework import for one constant.
    status_code = 422
    title = "No usable cover letter could be written"
    problem_type = "letter-unwritable"

    def __init__(self, detail: str, *, problems: tuple[LetterProblem, ...] = ()) -> None:
        super().__init__(detail, problems=[problem.value for problem in problems])
        self.problems = problems


class CoverLetterDraft(BaseModel):
    """What the model must return. Shape only — the content checks are code."""

    model_config = ConfigDict(extra="forbid")

    #: The finished letter, ready to paste into the response form.
    letter: str
    #: ISO 639-1 of the language it was actually written in.
    language: str = "ru"
    #: The requirements the letter speaks to. Checked against the profile: this
    #: is the model stating, in a machine-readable field, exactly which claims
    #: it made about the candidate.
    addressed_skills: list[str] = Field(default_factory=list)
    #: The requirements the letter names as not covered.
    acknowledged_gaps: list[str] = Field(default_factory=list)

    @field_validator("letter")
    @classmethod
    def _trim(cls, value: str) -> str:
        """Strip here, so the text that is checked is the text that is saved.

        Models pad an answer with a leading newline often enough that stripping
        it later would mean the length check ran against one string and the
        database received another.
        """
        return value.strip()


@dataclass(frozen=True, slots=True)
class GeneratedLetter:
    """One letter and how it was arrived at.

    A dataclass rather than a Pydantic model because it carries ``LLMUsage``
    objects and never crosses a serialisation boundary — it goes from the
    generator to the service and to the terminal.
    """

    text: str
    language: str
    source: LetterSource
    attempts: int
    #: Everything the checks caught on the way, in order. Empty on a clean first
    #: answer. Kept even when a later attempt succeeded: "the model wanted to put
    #: a link in it" is worth seeing in a log.
    rejected_for: tuple[LetterProblem, ...] = ()
    usages: tuple[LLMUsage, ...] = ()
    addressed_skills: tuple[str, ...] = ()
    acknowledged_gaps: tuple[str, ...] = ()

    @property
    def cost_usd(self) -> float | None:
        """What the attempts cost, or None when no model priced them."""
        priced = [usage.cost_usd for usage in self.usages if usage.cost_usd is not None]
        return sum(priced) if priced else None


def inspect_draft(draft: CoverLetterDraft, context: LetterContext) -> list[LetterProblem]:
    """Every hard constraint, checked against the finished text.

    Two of the checks need the context, and they are two halves of one rule
    about ``addressed_skills``:

    ``UNSUPPORTED_CLAIM``
        a skill the model says the letter claims, that the profile does not
        list, is an invention. Checked against everything the candidate has
        rather than only against the requirement list, because the offence is
        claiming something untrue, not answering the wrong requirement.
    ``UNDECLARED_SKILLS``
        an empty declaration while the profile covers part of the requirement
        list. Without this the honesty check is opt-in: ``addressed_skills=[]``
        satisfies "nothing declared is unsupported" trivially, so a model that
        declares nothing passes while the prose says whatever it likes.

    **What this does not do.** It reads the declaration, not the prose. A letter
    whose ``addressed_skills`` are all real and whose text still claims Kafka is
    not caught here, and no string matching would catch it either: the prompt
    asks the model to *name* the uncovered requirements ("I have not worked with
    Kafka"), so the missing skills legitimately appear in the text and looking
    for them there would reject the honest letters and pass the dishonest ones.
    What bounds the prose instead is that the prompt carries no material about
    the candidate beyond the profile, and that a person reads the letter before
    it is sent. That is a smaller guarantee than "the letter is true"; it is the
    one the code actually provides.
    """
    problems = find_problems(draft.letter, max_length=context.vacancy.letter_max_length)

    possessed = context.possessed
    unsupported = [
        name for name in draft.addressed_skills if not possessed.intersection(_declared_keys(name))
    ]
    if unsupported:
        problems.append(LetterProblem.UNSUPPORTED_CLAIM)
    elif context.overlap.matched and not draft.addressed_skills:
        problems.append(LetterProblem.UNDECLARED_SKILLS)

    if prompt_builder.FENCE_OPEN in draft.letter or prompt_builder.FENCE_CLOSE in draft.letter:
        problems.append(LetterProblem.LEAKED_FENCE)

    return problems


def _declared_keys(name: str) -> set[str]:
    """Every fold key one ``addressed_skills`` entry could legitimately mean.

    The prompt renders a covered requirement as ``Python (resume: Питон) - 4
    years`` and asks for the name alone, but a model that echoes the whole line
    is being obedient in the way models usually are — and folding that string
    finds nothing in the profile, so the letter was rejected as an *invented
    claim* and the retry spent a heavy call on a fault that was the prompt's.
    Accepting the entry as the prompt writes it is what makes the two agree.

    Both spellings are offered rather than only the undecorated one, so a skill
    whose real name happens to contain the separator is not damaged on the way
    through.
    """
    return {key for key in (fold(name), fold(prompt_builder.undecorate(name))) if key}


def feedback_for(problems: list[LetterProblem]) -> str:
    """The correction the model is shown on its second attempt."""
    faults = "\n".join(f"- {ENGLISH[problem]}" for problem in problems)
    return (
        "\n## Your previous answer was rejected\n\n"
        "It was checked in code, not by a person, and it failed on:\n\n"
        f"{faults}\n\n"
        "Write the letter again, correcting every point above, and return the "
        "JSON object and nothing else."
    )


async def generate(context: LetterContext, *, router: LLMRouter | None = None) -> GeneratedLetter:
    """One letter for one vacancy, guaranteed to pass every hard constraint.

    The guarantee is the return type's, and it is checked rather than argued: a
    ``GeneratedLetter`` coming out of here has been through
    :func:`app.letters.guard.find_problems` clean, whichever branch produced it.

    A bad model answer is not an error — it becomes the rule-based letter, and
    the caller can see from ``source`` which it got. A rule-based letter that is
    itself unusable *is* an error, and raises :class:`LetterUnwritableError`:
    there is nothing left to fall back to, and the two ways of not saying so —
    saving the stub, or returning it and letting the service call that success —
    both end with an employer reading it.
    """
    router = router or get_router()
    usages: list[LLMUsage] = []
    seen: list[LetterProblem] = []
    feedback = ""

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            result = await router.complete_json(
                PROMPT_NAME,
                CoverLetterDraft,
                task=TASK,
                variables=prompt_builder.variables(context, feedback=feedback),
            )
        except LLMError as exc:
            # The provider is gone, or it failed to produce the shape twice.
            # Both mean there is no model answer to check; the fallback is the
            # answer. Narrow on purpose: a bug in this module must still raise.
            logger.warning(
                "letters.generate.no_model_answer",
                vacancy_id=str(context.vacancy.vacancy_id),
                attempt=attempt,
                reason=type(exc).__name__,
            )
            break

        usages.append(usage_ledger.record(result.usage))
        problems = inspect_draft(result.value, context)
        if not problems:
            return GeneratedLetter(
                text=result.value.letter,
                language=result.value.language or context.language,
                source="model",
                attempts=attempt,
                rejected_for=tuple(seen),
                usages=tuple(usages),
                addressed_skills=tuple(result.value.addressed_skills),
                acknowledged_gaps=tuple(result.value.acknowledged_gaps),
            )

        seen.extend(problems)
        feedback = feedback_for(problems)
        logger.warning(
            "letters.generate.rejected",
            vacancy_id=str(context.vacancy.vacancy_id),
            attempt=attempt,
            problems=[problem.value for problem in problems],
        )

    fallback = compose_fallback(context)
    logger.warning(
        "letters.generate.fell_back",
        vacancy_id=str(context.vacancy.vacancy_id),
        problems=[problem.value for problem in seen],
        unnameable_skills=list(fallback.unnameable_skills),
    )

    remaining = find_problems(fallback.text, max_length=context.vacancy.letter_max_length)
    if remaining:
        logger.error(
            "letters.generate.fallback_unusable",
            vacancy_id=str(context.vacancy.vacancy_id),
            problems=[problem.value for problem in remaining],
            characters=len(fallback.text),
        )
        raise LetterUnwritableError(
            "the rule-based letter fails the checks too, so there is nothing left "
            "to fall back to: this vacancy and this profile have too little in "
            "them to write a letter from",
            problems=tuple(remaining),
        )

    return GeneratedLetter(
        text=fallback.text,
        language=context.language,
        source="fallback",
        attempts=len(usages),
        rejected_for=tuple(seen),
        usages=tuple(usages),
        # What the letter names, not what the overlap holds: a skill the guard
        # would not let it name is not a skill it addressed, and reporting the
        # overlap here would be the same silent deletion in the outcome record.
        addressed_skills=fallback.addressed_skills,
        acknowledged_gaps=context.overlap.missing,
    )


@dataclass(frozen=True, slots=True)
class FallbackLetter:
    """The rule-based letter, and an account of what it left out.

    The text alone is not enough to hand back. Dropping a fragment that trips
    the link guard is the fallback's licence, but the two things it can drop are
    not the same kind of loss, so the caller has to be able to tell them apart —
    which is what :attr:`unnameable_skills` is for.
    """

    text: str
    #: The matched skills the letter actually names, in the order it names them.
    #: This is what ``addressed_skills`` reports for a fallback: the overlap is
    #: what the candidate has, this is what the letter said.
    addressed_skills: tuple[str, ...] = ()
    #: Matched skills that could be written under neither spelling. Empty in
    #: every ordinary case; never silently empty when something was lost.
    unnameable_skills: tuple[str, ...] = ()


def compose_fallback(context: LetterContext) -> FallbackLetter:
    """A letter built from the context alone, with no model involved.

    Dull, short, true: every sentence is assembled from values already in the
    database, and every one of them is passed through the link guard before it
    goes in. Dropping a fragment that trips the guard is allowed here in a way it
    is never allowed for a letter a person wrote — nobody wrote this, so nothing
    is being silently changed on anyone's behalf.

    **What may be dropped and what may not.** An employer's name is droppable:
    a company genuinely called "Kaspi.kz" is the ordinary case on this market,
    the opening simply does not name where the vacancy is, and the letter still
    says everything it came to say. A *matched skill* is not droppable, it is the
    sentence the letter exists to carry — a candidate whose overlap is ASP.NET
    and socket.io would otherwise be sent a letter that claims nothing at all,
    with nobody told. So a matched skill is written under the resume's spelling,
    or failing that under the vacancy's own — they are the same skill, that is
    what ``matched`` means — and if neither can be written, it is reported in
    :attr:`FallbackLetter.unnameable_skills` and logged by :func:`generate`
    instead of disappearing.
    """
    vacancy = context.vacancy
    overlap = context.overlap
    title = vacancy.title if is_safe(vacancy.title) else None
    company = vacancy.company if vacancy.company and is_safe(vacancy.company) else None

    paragraphs = ["Здравствуйте!", _opening(title, company)]

    named, unnameable = _nameable(overlap.matched)
    if named:
        paragraphs.append(
            "Из того, что перечислено в требованиях, у меня есть коммерческий "
            f"опыт работы с: {_enumerate([_skill_phrase(*pair) for pair in named])}."
        )
    elif context.profile.headline and is_safe(context.profile.headline):
        paragraphs.append(f"Моя специализация — {context.profile.headline}.")

    missing = [name for name in overlap.missing if is_safe(name)]
    if missing:
        paragraphs.append(
            f"С чем не работал: {_enumerate(missing)}. Пишу об этом сразу, "
            "чтобы не тратить ваше время: опыта здесь у меня нет, разобраться готов."
        )

    other = [name for name in overlap.other[:3] if is_safe(name)]
    if other:
        paragraphs.append(f"Помимо этого использую: {_enumerate(other)}.")

    paragraphs.append("Готов обсудить детали и ответить на вопросы.")
    name = context.profile.name
    paragraphs.append(f"С уважением, {name}" if name and is_safe(name) else "С уважением")

    text = _fit(paragraphs, vacancy.letter_max_length)
    return FallbackLetter(
        text=text,
        # Only the ones the assembled text actually kept: ``_fit`` drops whole
        # paragraphs at a small ceiling, and the skills paragraph is one of them.
        addressed_skills=tuple(
            written for skill, written in named if _skill_phrase(skill, written) in text
        ),
        unnameable_skills=unnameable,
    )


def _nameable(
    matched: tuple[MatchedSkill, ...],
) -> tuple[list[tuple[MatchedSkill, str]], tuple[str, ...]]:
    """Each matched skill paired with a spelling the guard allows, or reported.

    The resume's spelling first, because that is the candidate's own word for
    it; the vacancy's second, because a matched skill is by definition the same
    skill under both names, and the employer's own spelling is never a worse
    thing to read than nothing at all.
    """
    named: list[tuple[MatchedSkill, str]] = []
    unnameable: list[str] = []
    for skill in matched:
        spelling = next(
            (form for form in (skill.possessed_as, skill.required_as) if is_safe(form)), None
        )
        if spelling is None:
            unnameable.append(skill.possessed_as)
        else:
            named.append((skill, spelling))
    return named, tuple(unnameable)


def _opening(title: str | None, company: str | None) -> str:
    """The sentence naming what is being applied for."""
    position = f"вакансия «{title}»" if title else "ваша вакансия"
    where = f" в компании {company}" if company else ""
    return f"Меня заинтересовала {position}{where}."


def _skill_phrase(skill: MatchedSkill, spelling: str) -> str:
    """One matched skill with its depth, when the profile knows it."""
    if skill.years is not None and skill.years >= 1:
        return f"{spelling} ({_years_in_russian(int(skill.years))})"
    return spelling


def _years_in_russian(years: int) -> str:
    """Years in the grammatical case Russian requires: 1 год, 3 года, 5 лет."""
    tail, hundred = years % 10, years % 100
    if tail == 1 and hundred != 11:
        return f"{years} год"
    if tail in (2, 3, 4) and hundred not in (12, 13, 14):
        return f"{years} года"
    return f"{years} лет"


def _enumerate(values: list[str]) -> str:
    """A Russian list: commas, and "и" before the last item."""
    if len(values) == 1:
        return values[0]
    return f"{', '.join(values[:-1])} и {values[-1]}"


def _fit(paragraphs: list[str], max_length: int) -> str:
    """Assemble the letter, dropping whole paragraphs rather than cutting words.

    A letter cut mid-sentence at a character limit is worse than a shorter one:
    the reader sees a person who did not check what they sent. Only a limit too
    small for even the first paragraph forces a hard cut, and that is a limit no
    job board has ever set.
    """
    text = ""
    for paragraph in paragraphs:
        candidate = f"{text}\n\n{paragraph}" if text else paragraph
        if len(candidate) > max_length:
            break
        text = candidate
    return text if text else paragraphs[0][:max_length]
