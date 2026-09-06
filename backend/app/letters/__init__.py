"""Cover letters: built from the requirement overlap, checked, saved, never sent.

    context  what the letter may know: the vacancy, the profile, their overlap
    prompt   filling in app/llm/prompts/cover_letter.md, fencing untrusted text
    generator the model call, the checks on its answer, the rule-based fallback
    guard    the constraints, enforced by reading the output rather than asking
    store    the rows in and the letter out
    service  the sequence, for one vacancy or for a queue

The letter always ends up in ``application.cover_letter``. Nothing in this
package can send one: that is ``agent/``'s job, from a browser, under the user's
own account, and only after a human has confirmed it.
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
    "CoverLetterDraft",
    "FallbackLetter",
    "GeneratedLetter",
    "LetterContext",
    "LetterOutcome",
    "LetterProblem",
    "LetterUnwritableError",
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
    "write_batch",
    "write_letter",
]
