"""What a rule is, and what it says about a finished document.

**Every rule here is decided by reading the output.** That is the whole design.
The model is asked to obey them — it raises the first-pass hit rate and costs
nothing — but what makes a hard rule true is a function running over the text
the model produced, and the document being thrown away when the function says
no. A rule that is only asked for is a rule that is silently broken, and the
person who wrote it finds out from an employer.

Which is why the vocabulary is closed. ``RuleKind`` has eight members and no
"custom text" member, and it is not going to grow one: a free-prose requirement
can only ever be pasted into a prompt, so it would arrive in the same list as
the checkable ones, wearing the same "hard" badge, and mean something entirely
different. Style that cannot be counted belongs to the reference documents,
which is what they are for — form by example, constraints by measurement.

The eight cover the four families the brief names:

======================  ===================================================
family                  kinds
======================  ===================================================
counts                  ``section_item_count``
presence                ``required_section``, ``required_keyword``,
                        ``date_format``
prohibition             ``forbidden_phrase``, ``no_links``,
                        ``no_contact_handles``
length                  ``length``
======================  ===================================================

**The last two are built in and cannot be deleted.** "No links, no email
addresses in a cover letter" is not a preference: hh files a letter carrying one
as spam and the application is lost without anyone being told. It existed before
this module, in :mod:`app.letters.guard`, and it is here as
:data:`BUILTIN_RULES` — *entries*, not a second implementation. Their checkers
call ``guard.has_link`` and ``guard.AT_SIGN``, the same functions
``find_problems`` calls and the same ones
``backend/tests/test_letter_guard_drift.py`` pins against ``agent/letter.py``.
There is one definition of what a link is, and this is not it.

**A rule cannot require asserting an untruth.** It is not a check that happens
here — see :mod:`app.workshop.truth`, which runs when a rule is saved — but the
reason it is *possible* to check lives in this module's shape. Because a rule is
structured rather than prose, the only way it can put a claim into a document is
by naming one: a required keyword, or a required section heading. Two fields, on
two kinds. Everything else constrains form, and form cannot be false.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.db.enums import RuleKind, RuleScope, RuleSeverity
from app.workshop.sections import Section, find_section

#: Longest a keyword, phrase or section name may be. A rule is a label, not a
#: paragraph: past this it is prose that happens to be stored in a params field,
#: and the substring match it turns into would never fire.
MAX_SUBJECT_CHARS = 120

#: Ceiling on any count or length a rule may demand. Not a judgement about what
#: is reasonable — 21 skills is the owner's own example and well inside it — but
#: a guard against a rule nothing could ever satisfy, which would turn every
#: generation into the exhausted-attempts refusal for ever.
MAX_COUNT = 500
MAX_LENGTH = 100_000


class LengthUnit(StrEnum):
    """What a length rule counts."""

    CHARACTERS = "characters"
    WORDS = "words"


class DateFormat(StrEnum):
    """How every date in the document must be written.

    A closed list, because the rule is "all of them the same way" and that only
    means something against a named way. The four are the ones this market's
    resumes actually use.
    """

    MM_DOT_YYYY = "mm.yyyy"
    MM_SLASH_YYYY = "mm/yyyy"
    YYYY_DASH_MM = "yyyy-mm"
    YYYY = "yyyy"


#: How each accepted format is written, and how it is recognised.
DATE_PATTERNS: dict[DateFormat, re.Pattern[str]] = {
    DateFormat.MM_DOT_YYYY: re.compile(r"^(0[1-9]|1[0-2])\.(19|20)\d{2}$"),
    DateFormat.MM_SLASH_YYYY: re.compile(r"^(0[1-9]|1[0-2])/(19|20)\d{2}$"),
    DateFormat.YYYY_DASH_MM: re.compile(r"^(19|20)\d{2}-(0[1-9]|1[0-2])$"),
    DateFormat.YYYY: re.compile(r"^(19|20)\d{2}$"),
}

#: Anything in a document that reads as a month-and-year date, in any spelling.
#: Deliberately narrow: it matches numeric dates only, so a date written in
#: words ("сентябрь 2024") is not reported as badly formatted. Claiming to
#: check every date and then missing the written ones would be worse than
#: saying plainly that this checks the numeric ones.
_DATE_LIKE = re.compile(r"(?<![\d./-])(?:\d{1,2}[./-]\d{4}|\d{4}[./-]\d{1,2})(?![\d./-])")

#: A word, for the word-count unit. Anything separated by whitespace and
#: carrying at least one alphanumeric character, so a lone dash between two
#: clauses is not counted as a word.
_WORD = re.compile(r"\S*[^\W_]\S*", re.UNICODE)


class _Params(BaseModel):
    """Base for every kind's parameters: immutable and closed to stray keys."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class SectionItemCountParams(_Params):
    """«В навыках не меньше 21 пункта», in fields.

    Both bounds are optional and at least one is required: a rule with neither
    asks nothing and would sit in the list looking enforced.
    """

    kind: Literal[RuleKind.SECTION_ITEM_COUNT] = RuleKind.SECTION_ITEM_COUNT
    section: str = Field(min_length=1, max_length=MAX_SUBJECT_CHARS)
    minimum: int | None = Field(default=None, ge=0, le=MAX_COUNT)
    maximum: int | None = Field(default=None, ge=0, le=MAX_COUNT)

    @model_validator(mode="after")
    def _bounds_make_sense(self) -> "SectionItemCountParams":
        """Refuse a rule that asks nothing, or that asks the impossible."""
        if self.minimum is None and self.maximum is None:
            raise ValueError("a count rule needs a minimum, a maximum, or both")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError(
                f"minimum {self.minimum} is above maximum {self.maximum}; "
                "no document could satisfy this rule"
            )
        return self


class RequiredSectionParams(_Params):
    """A named section must exist at all."""

    kind: Literal[RuleKind.REQUIRED_SECTION] = RuleKind.REQUIRED_SECTION
    section: str = Field(min_length=1, max_length=MAX_SUBJECT_CHARS)


class RequiredKeywordParams(_Params):
    """A word or phrase must appear somewhere in the document.

    The one kind that can put a *claim* into a document, which is why
    :mod:`app.workshop.truth` reads this field when the rule is saved.
    """

    kind: Literal[RuleKind.REQUIRED_KEYWORD] = RuleKind.REQUIRED_KEYWORD
    keyword: str = Field(min_length=1, max_length=MAX_SUBJECT_CHARS)
    case_sensitive: bool = False


class DateFormatParams(_Params):
    """Every numeric date in the document is written the agreed way."""

    kind: Literal[RuleKind.DATE_FORMAT] = RuleKind.DATE_FORMAT
    pattern: DateFormat = DateFormat.MM_DOT_YYYY


class ForbiddenPhraseParams(_Params):
    """A word or phrase that must not appear.

    Never checked for truthfulness: forbidding a name is not a claim about
    anyone, so a rule saying "never write Kubernetes" is legitimate whether or
    not the candidate has ever touched it.
    """

    kind: Literal[RuleKind.FORBIDDEN_PHRASE] = RuleKind.FORBIDDEN_PHRASE
    phrase: str = Field(min_length=1, max_length=MAX_SUBJECT_CHARS)
    case_sensitive: bool = False


class LengthParams(_Params):
    """A floor and/or a ceiling, in characters or in words."""

    kind: Literal[RuleKind.LENGTH] = RuleKind.LENGTH
    unit: LengthUnit = LengthUnit.CHARACTERS
    minimum: int | None = Field(default=None, ge=0, le=MAX_LENGTH)
    maximum: int | None = Field(default=None, ge=0, le=MAX_LENGTH)

    @model_validator(mode="after")
    def _bounds_make_sense(self) -> "LengthParams":
        """Refuse a rule that asks nothing, or that asks the impossible."""
        if self.minimum is None and self.maximum is None:
            raise ValueError("a length rule needs a minimum, a maximum, or both")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError(
                f"minimum {self.minimum} is above maximum {self.maximum}; "
                "no document could satisfy this rule"
            )
        return self


class NoLinksParams(_Params):
    """Built in. Checked by :func:`app.letters.guard.has_link`."""

    kind: Literal[RuleKind.NO_LINKS] = RuleKind.NO_LINKS


class NoContactHandlesParams(_Params):
    """Built in. Checked by :data:`app.letters.guard.AT_SIGN`."""

    kind: Literal[RuleKind.NO_CONTACT_HANDLES] = RuleKind.NO_CONTACT_HANDLES


#: One rule's parameters, discriminated by the kind they belong to. The kind is
#: inside the payload as well as in its own column so that a stored row is
#: self-describing: a disagreement between the two is a validation failure
#: rather than something a reader resolves by trusting whichever it happens to
#: read first.
RuleParams = Annotated[
    SectionItemCountParams
    | RequiredSectionParams
    | RequiredKeywordParams
    | DateFormatParams
    | ForbiddenPhraseParams
    | LengthParams
    | NoLinksParams
    | NoContactHandlesParams,
    Field(discriminator="kind"),
]


class RuleSpec(BaseModel):
    """One rule, ready to be checked, wherever it was stored.

    A row of ``generation_rule`` becomes one of these, and so does a built-in.
    Everything downstream — the checker, the prompt block, the report — sees
    only this, so a built-in is enforced exactly like the owner's own and no
    branch anywhere asks which it is.
    """

    model_config = ConfigDict(frozen=True)

    #: A UUID string for a stored rule; ``builtin:<kind>`` for a built-in. Not a
    #: UUID field: the mutation endpoints take a UUID path parameter, so a
    #: built-in has no address there and cannot be edited or deleted by anything
    #: that only speaks HTTP.
    id: str
    scope: RuleScope
    severity: RuleSeverity
    params: RuleParams
    #: What a person reads when it is broken. Russian, theirs. Never rendered
    #: into a prompt — see :mod:`app.workshop.prompt`.
    message: str
    is_active: bool = True
    is_builtin: bool = False

    @property
    def kind(self) -> RuleKind:
        """The kind, read off the params so there is one place it lives."""
        return self.params.kind

    def applies_to(self, scope: RuleScope) -> bool:
        """Whether this rule is checked against a document of that scope."""
        return self.scope is RuleScope.BOTH or self.scope is scope


@dataclass(frozen=True, slots=True)
class RuleViolation:
    """One rule, broken, and what the document actually did.

    Two texts because they have two readers. :attr:`message` is the owner's own
    sentence, shown in the dashboard and in the terminal; :attr:`detail` is
    written here, in English, and is what the model is shown when it is asked to
    try again. The model must be told what was measured — "the section «навыки»
    holds 12 items and the rule asks for at least 21" — and the owner's sentence
    may say anything at all.
    """

    rule_id: str
    kind: RuleKind
    severity: RuleSeverity
    message: str
    detail: str

    @property
    def is_hard(self) -> bool:
        """Whether this violation stops the document being handed back."""
        return self.severity is RuleSeverity.HARD


#: The rules nobody may switch off, in the order they are shown.
#:
#: They are constants and not seeded rows on purpose. A row is deletable by
#: anything holding a ``DELETE``, and re-seeding it on the next start would make
#: "I removed it" and "it came back" both true. As constants they are simply not
#: removable, and the reason they exist — hh files a letter carrying a link as
#: spam, and the application is lost silently — is written next to them.
BUILTIN_RULES: tuple[RuleSpec, ...] = (
    RuleSpec(
        id=f"builtin:{RuleKind.NO_LINKS.value}",
        scope=RuleScope.COVER_LETTER,
        severity=RuleSeverity.HARD,
        params=NoLinksParams(),
        message=(
            "Ни одной ссылки и ни одного адреса сайта в сопроводительном: "
            "hh считает такое письмо спамом, и отклик пропадёт молча."
        ),
        is_builtin=True,
    ),
    RuleSpec(
        id=f"builtin:{RuleKind.NO_CONTACT_HANDLES.value}",
        scope=RuleScope.COVER_LETTER,
        severity=RuleSeverity.HARD,
        params=NoContactHandlesParams(),
        message=(
            "Ни одного email и ни одного ника через @ в сопроводительном: тот же спам-фильтр hh."
        ),
        is_builtin=True,
    ),
)


def check(
    text: str,
    *,
    scope: RuleScope,
    rules: Iterable[RuleSpec],
) -> list[RuleViolation]:
    """Every rule this text breaks, in the order the rules were given.

    Inactive rules and rules of another scope are skipped. A rule that applies
    and holds produces nothing; the empty list is the only "it passed".
    """
    broken: list[RuleViolation] = []
    for rule in rules:
        if not rule.is_active or not rule.applies_to(scope):
            continue
        detail = _detail_of(rule, text)
        if detail is not None:
            broken.append(
                RuleViolation(
                    rule_id=rule.id,
                    kind=rule.kind,
                    severity=rule.severity,
                    message=rule.message,
                    detail=detail,
                )
            )
    return broken


def hard(violations: Sequence[RuleViolation]) -> tuple[RuleViolation, ...]:
    """The ones that stop the document."""
    return tuple(violation for violation in violations if violation.is_hard)


def soft(violations: Sequence[RuleViolation]) -> tuple[RuleViolation, ...]:
    """The ones that travel beside it as warnings."""
    return tuple(violation for violation in violations if not violation.is_hard)


def _detail_of(rule: RuleSpec, text: str) -> str | None:
    """What this rule found wrong, or None when the document satisfies it."""
    params = rule.params
    match params:
        case SectionItemCountParams():
            return _count_detail(params, text)
        case RequiredSectionParams():
            return _required_section_detail(params, text)
        case RequiredKeywordParams():
            return _required_keyword_detail(params, text)
        case DateFormatParams():
            return _date_format_detail(params, text)
        case ForbiddenPhraseParams():
            return _forbidden_phrase_detail(params, text)
        case LengthParams():
            return _length_detail(params, text)
        case NoLinksParams():
            # Imported here, not at the top of the module, and the reason is the
            # direction of the dependency rather than a startup cost. The letter
            # package imports this one — its generator checks these rules — so
            # this module importing ``app.letters`` at module scope would make a
            # cycle whose failure depends on which of the two something happens
            # to import first. Called rather than copied is still the whole
            # point: this is the same function ``find_problems`` calls and the
            # same one ``test_letter_guard_drift`` pins against the agent's.
            from app.letters.guard import has_link

            return "it contains a URL or a bare domain name" if has_link(text) else None
        case NoContactHandlesParams():
            from app.letters.guard import AT_SIGN  # see NoLinksParams above

            return (
                "it contains an @ sign, which reads as an email address or a messenger handle"
                if AT_SIGN.search(text)
                else None
            )


def _count_detail(params: SectionItemCountParams, text: str) -> str | None:
    """How the named section's item count compares with the bounds."""
    section = _section(text, params.section)
    if section is None:
        return (
            f'there is no section called "{params.section}", so its item count cannot be satisfied'
        )
    count = len(section.items)
    if params.minimum is not None and count < params.minimum:
        return (
            f'the section "{params.section}" lists {count} '
            f"{_plural(count)}; at least {params.minimum} are required"
        )
    if params.maximum is not None and count > params.maximum:
        return (
            f'the section "{params.section}" lists {count} '
            f"{_plural(count)}; at most {params.maximum} are allowed"
        )
    return None


def _required_section_detail(params: RequiredSectionParams, text: str) -> str | None:
    """Whether the named section is there at all."""
    if _section(text, params.section) is None:
        return f'there is no section called "{params.section}"'
    return None


def _required_keyword_detail(params: RequiredKeywordParams, text: str) -> str | None:
    """Whether the required word or phrase appears."""
    haystack = text if params.case_sensitive else text.casefold()
    needle = params.keyword if params.case_sensitive else params.keyword.casefold()
    if needle not in haystack:
        return f'it does not contain "{params.keyword}"'
    return None


def _forbidden_phrase_detail(params: ForbiddenPhraseParams, text: str) -> str | None:
    """Whether the forbidden word or phrase appears."""
    haystack = text if params.case_sensitive else text.casefold()
    needle = params.phrase if params.case_sensitive else params.phrase.casefold()
    if needle in haystack:
        return f'it contains "{params.phrase}", which must not appear'
    return None


def _date_format_detail(params: DateFormatParams, text: str) -> str | None:
    """Whether every numeric date is written the agreed way.

    Only numeric dates: see :data:`_DATE_LIKE`. A rule that claimed to check
    "сентябрь 2024" and did not would be worse than one that says what it does.
    """
    wrong = [
        found
        for found in _DATE_LIKE.findall(text)
        if not DATE_PATTERNS[params.pattern].match(found)
    ]
    if not wrong:
        return None
    listed = ", ".join(sorted(set(wrong))[:5])
    return (
        f"the dates {listed} are not written as {params.pattern.value}; "
        "every numeric date must use that form"
    )


def _length_detail(params: LengthParams, text: str) -> str | None:
    """How long the document is against the bounds it was given."""
    if params.unit is LengthUnit.WORDS:
        size = len(_WORD.findall(text))
        unit = "words"
    else:
        size = len(text)
        unit = "characters"
    if params.minimum is not None and size < params.minimum:
        return f"it is {size} {unit} long; at least {params.minimum} are required"
    if params.maximum is not None and size > params.maximum:
        return f"it is {size} {unit} long; at most {params.maximum} are allowed"
    return None


def _section(text: str, name: str) -> Section | None:
    """The named section of the document, however the document spells it."""
    return find_section(text, name)


def _plural(count: int) -> str:
    """ "item" or "items", so the English detail reads like English."""
    return "item" if count == 1 else "items"
