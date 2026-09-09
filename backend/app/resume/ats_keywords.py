"""Does this document say the words the employer's filter searches for?

:mod:`app.resume.ats_audit` asks whether a parser can read the file at all.
This asks the next question down, and it only has an answer once a vacancy is
named: of the requirements *this posting* lists, which ones does *this variant*
of the document spell the way the posting spells them.

**Literal, on purpose.** hh ships ``keySkills`` as a structured list, so the
requirements are strings rather than prose, and a keyword filter looks for those
strings. It does not know that «постгрес» is PostgreSQL, that "к8s" is
Kubernetes, or that «писал на го» is Go. Matching them here with anything
cleverer than string search would report a resume as covered by a filter that
is about to drop it — the audit would be modelling a reader the candidate does
not have.

**The three answers are not three degrees of the same thing.** A requirement the
candidate holds and the document names is done. One they demonstrably hold and
this variant does not name is a *writing* problem: the fix is to generate a
variant that says it, and that invents nothing, because the profile is the
evidence. A requirement nobody has is not a problem this system may solve. It is
reported and left there. The one thing this module must never make easy is
turning the third case into the first.

**Which is why "not held" outranks a literal hit rather than the other way
round.** The case is real and it is the honest one: a cover letter that says «с
Kubernetes не работал» puts the string in the text, so an employer's filter
matches it — and a reading that took the match at face value would report the
requirement as covered by a candidate who has just said they do not have it. The
hit is kept, on the ``absent`` entry, because "the filter will find this word and
there is nothing behind it" is worth being able to say out loud.

So the output carries the evidence for the middle case — :attr:`held_as`, the
spelling the profile records — and carries nothing at all for the third beyond
the requirement's own name.

**What the middle case does not catch, and why that is the safe direction.**
Telling "named in other words" from "not named" runs through the same skill
dictionary the rest of the project folds names with, and that dictionary knows
spellings, not Russian morphology: «постгрес» folds onto PostgreSQL and
«постгресом» does not. So an inflected mention reads as *unnamed* rather than as
present. That is the error worth having: the finding it produces is "this
variant does not say PostgreSQL", which is true — the filter will not find it
either — and the fix is the same one. The opposite error would report a document
as covered by a filter about to drop it.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from app.core.logging import get_logger
from app.db.enums import RequirementSource
from app.resume.skills import fold
from app.schemas.ats import ATSKeywords, KeywordStatus, RequirementMatch

logger = get_logger(__name__)

#: Longest requirement, in words, that is looked for as a phrase. hh's skill
#: names run to three ("Atlassian Jira Service Desk" is four and is the outlier
#: rather than the rule); past this a "requirement" is a sentence somebody typed
#: into the wrong field, and searching a document for it literally would always
#: fail and always report the same unfixable finding.
MAX_PHRASE_WORDS = 5

#: What separates words when the document is scanned for phrases. Deliberately
#: not ``\s+``: a resume writes «Python, PostgreSQL» and a bullet writes
#: «Python·PostgreSQL», and a phrase search that only tolerates spaces reads
#: both as one long unmatched token.
_WORD = re.compile(r"[^\W_]+(?:[+#.][^\W_]+|\+\+|#)*", re.UNICODE)


@dataclass(frozen=True, slots=True)
class HeldSkill:
    """One skill the candidate has, as the profile records it.

    Deliberately not :class:`app.letters.context.SkillFact`: that model carries
    what a letter is allowed to say, this one carries what the audit is allowed
    to call evidence, and a shared model would be one of them borrowing the
    other's rules. Both are built from ``profile_skill`` rows.
    """

    canonical_name: str
    #: Every spelling the resume used. The first is what the candidate wrote,
    #: and it is what the audit quotes back at them.
    spellings: tuple[str, ...] = ()

    @property
    def spelling(self) -> str:
        """What to call this skill when talking to its owner."""
        return next((s for s in self.spellings if s.strip()), self.canonical_name)


def literal_pattern(requirement: str) -> re.Pattern[str] | None:
    """A pattern matching this requirement the way a keyword filter matches it.

    Two liberties are taken with "literal", and only two. Case is ignored,
    because every filter worth modelling folds it. Runs of whitespace and
    punctuation between the requirement's own words are treated as
    interchangeable, because «CI/CD» in a posting and «CI / CD» in a resume are
    the same string to a person and to any tokeniser, and a document is
    line-wrapped by a layout nobody controls.

    Everything else is left alone: ``C++`` matches ``C++`` and not ``C``,
    ``Go`` does not match ``Google``, and a missing word is a miss.
    """
    words = _WORD.findall(requirement)
    if not words or len(words) > MAX_PHRASE_WORDS:
        return None
    # Escaped individually and rejoined: the separators between them are the
    # only place flexibility is allowed, and building the pattern from the raw
    # string would let a bracket in a requirement compile into a character
    # class.
    body = r"[\s\W_]{0,3}".join(re.escape(word) for word in words)
    # Boundaries by hand rather than with \b: \b sits between "C" and "+", so
    # r"\bC\+\+\b" never matches "C++ разработчик". A requirement may not be
    # preceded or followed by a letter or a digit, which is the rule \b is
    # trying to express and gets wrong for the names this market writes.
    return re.compile(rf"(?<![^\W_])(?:{body})(?![^\W_])", re.IGNORECASE | re.UNICODE)


def _spellings_in(text: str) -> dict[str, str]:
    """Every skill-shaped phrase the document contains, by its fold key.

    Words and short runs of words, folded through the skill dictionary. This is
    what answers "the document does name this, in other words": the requirement
    PostgreSQL folds to the same key as the document's «постгрес», and the
    candidate is told to change a word rather than to acquire a database.

    First occurrence wins, so the quoted spelling is the one they wrote first.
    """
    words = _WORD.findall(text)
    found: dict[str, str] = {}
    for size in range(1, MAX_PHRASE_WORDS + 1):
        for start in range(len(words) - size + 1):
            phrase = " ".join(words[start : start + size])
            key = fold(phrase)
            if key:
                found.setdefault(key, phrase)
    return found


def _held_by_fold(held: Iterable[HeldSkill]) -> dict[str, HeldSkill]:
    """The candidate's skills, keyed the way requirements are compared."""
    index: dict[str, HeldSkill] = {}
    for skill in held:
        for name in (skill.canonical_name, *skill.spellings):
            key = fold(name)
            if key:
                index.setdefault(key, skill)
    return index


def match_requirements(
    text: str,
    requirements: Sequence[str],
    held: Sequence[HeldSkill],
    *,
    required: Sequence[bool] | None = None,
    sources: Sequence[RequirementSource] | None = None,
) -> ATSKeywords:
    """Read one document against one vacancy's requirement list.

    ``requirements`` are the posting's own strings in the posting's own order;
    ``held`` is what the profile says the candidate has. ``required`` marks the
    hard requirements when the caller knows which are which, and defaults to
    treating them all as hard — a vacancy whose source draws no distinction is
    better reported as asking for everything than as asking for nothing.
    ``sources`` says, per requirement, whether the employer named it or this
    project read it out of their description; it defaults to the employer,
    because a caller that does not pass it is a caller reading a structured
    list.

    Duplicates in the requirement list collapse: hh postings do repeat a skill
    under two spellings, and reporting «PostgreSQL» and «Postgres» as two
    separate gaps double-counts one fact.
    """
    document = _spellings_in(text)
    by_fold = _held_by_fold(held)
    hard = list(required) if required is not None else []
    told = list(sources) if sources is not None else []

    matches: list[RequirementMatch] = []
    seen: set[str] = set()
    for index, requirement in enumerate(requirements):
        name = requirement.strip()
        key = fold(name)
        if not name or not key or key in seen:
            continue
        seen.add(key)

        pattern = literal_pattern(name)
        hit = pattern.search(text) if pattern is not None else None
        skill = by_fold.get(key)
        status: KeywordStatus
        found_as: str | None
        held_as: str | None
        if skill is None:
            # Not held wins over a literal hit, and the case that forced the
            # rule is the honest one: a cover letter that names a gap — a
            # sentence saying this requirement was never met — puts the string
            # in the text, so an employer's filter matches it, and a purely
            # literal reading would report it as covered. It is not covered.
            # The hit is still recorded in `found_as`: "the filter will find
            # this word and there is nothing behind it" is worth being able to
            # say. Nothing else is offered — a requirement the candidate does
            # not hold has no fix this system is allowed to propose.
            status, found_as, held_as = KeywordStatus.ABSENT, hit.group(0) if hit else None, None
        elif hit is not None:
            status, found_as, held_as = KeywordStatus.PRESENT, hit.group(0), None
        else:
            status = KeywordStatus.UNSTATED
            found_as, held_as = document.get(key), skill.spelling

        matches.append(
            RequirementMatch(
                requirement=name,
                status=status,
                found_as=found_as,
                held_as=held_as,
                is_required=hard[index] if index < len(hard) else True,
                source=told[index] if index < len(told) else RequirementSource.EMPLOYER_FIELD,
            )
        )

    keywords = ATSKeywords(requirements=matches)
    logger.info(
        "resume.ats_keywords_matched",
        requirements=len(matches),
        present=len(keywords.present),
        unstated=len(keywords.unstated),
        absent=len(keywords.absent),
    )
    return keywords
