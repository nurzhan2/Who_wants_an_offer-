"""Cover letters: built from the requirement overlap, checked, saved, never sent.

    context  what the letter may know: the vacancy, the profile, their overlap
    prompt   filling in app/llm/prompts/cover_letter.md, fencing untrusted text
    examples past letters that got an answer, offered as few-shot examples
    generator the model call, the checks on its answer, the rule-based fallback
    guard    the constraints, enforced by reading the output rather than asking
    store    the rows in and the letter out
    service  the sequence, for one vacancy or for a queue

The letter always ends up in ``application.cover_letter``. Nothing in this
package can send one: that is ``agent/``'s job, from a browser, under the user's
own account, and only after a human has confirmed it.

``examples`` is the feedback loop and it is few-shot prompting, not learning:
letters the employer answered are pasted into the next prompt and forgotten when
the call ends. Measured on 2026-09-07 this account has two sent applications,
one answer, and no letter that can be shown — so the ordinary case is no
examples and a prompt identical to the one sent before the module existed. Read
its docstring before describing this feature to anyone.
"""

from app.letters.context import (
    LetterContext,
    ProfileFacts,
    SkillFact,
    SkillOverlap,
    VacancyFacts,
    build_context,
    overlap_of,
)
from app.letters.examples import (
    MIN_FOR_A_TREND,
    NOT_ENOUGH_RU,
    ChosenExample,
    ExampleGrade,
    ExamplePool,
    LetterExample,
    OutcomeEvidence,
    summary_ru,
)
from app.letters.generator import (
    CoverLetterDraft,
    FallbackLetter,
    GeneratedLetter,
    LetterUnwritableError,
    compose_fallback,
    generate,
)
from app.letters.guard import DEFAULT_MAX_LENGTH, LetterProblem, find_problems, is_safe
from app.letters.service import LetterOutcome, write_batch, write_letter

__all__ = [
    "DEFAULT_MAX_LENGTH",
    "MIN_FOR_A_TREND",
    "NOT_ENOUGH_RU",
    "ChosenExample",
    "CoverLetterDraft",
    "ExampleGrade",
    "ExamplePool",
    "FallbackLetter",
    "GeneratedLetter",
    "LetterContext",
    "LetterExample",
    "LetterOutcome",
    "LetterProblem",
    "LetterUnwritableError",
    "OutcomeEvidence",
    "ProfileFacts",
    "SkillFact",
    "SkillOverlap",
    "VacancyFacts",
    "build_context",
    "compose_fallback",
    "find_problems",
    "generate",
    "is_safe",
    "overlap_of",
    "summary_ru",
    "write_batch",
    "write_letter",
]
