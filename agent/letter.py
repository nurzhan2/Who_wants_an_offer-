"""The cover letter, and the one thing it may not contain.

A link in a cover letter on hh is not a style problem. Letters carrying URLs get
filtered, and a candidate who sends fifty of them gets quietly ranked into
nowhere without ever being told. The brief's rule is therefore absolute and its
remedy is deliberately blunt: a letter with a link or an ``@`` in it goes to a
human, and is never edited into shape and sent anyway. Cutting a line out of
somebody's letter and sending the remainder is a worse failure than not sending
it, because they still believe they sent what they wrote.

So this module refuses; it does not repair.

**The interesting part is the false positives, not the true ones.** A naive URL
pattern — anything with a dot in it — reads a Russian cover letter as one long
URL. Measured against realistic text, the words this has to leave alone include
``Python 3.12``, ``Node.js 18.x``, ``и т.д.``, ``и т.п.``, ``C++``, ``C1``,
``5.000 тг/час`` and ``с 1.09``. The pattern therefore matches a bare domain
only when the last label is a real top-level domain, which none of those are,
and matches a scheme or ``www.`` outright.

The list includes the Cyrillic TLDs, because ``портфолио.рф`` and
``мойсайт.қаз`` are the domains this market actually writes and an ASCII-only
pattern would wave them through — which is the failure that costs something. In
the other direction, a letter mentioning ``React.dev`` as a technology is
flagged and goes to a human: that is a real domain, the guard cannot know it was
meant as a name, and the cost of being wrong that way is one glance.

``letterMaxLength`` is hh's own limit, measured at 10 000 characters in
``applicantVacancyResponseStatuses``. It is enforced here rather than by the
textarea, because a letter silently cut at the field's maximum is a letter whose
last paragraph the employer never sees.
"""

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import final

#: hh's own ceiling, read from ``applicantVacancyResponseStatuses.letterMaxLength``
#: on a live page on 2026-09-06. A per-vacancy value may be smaller; this is the
#: default when the page did not say.
DEFAULT_MAX_LENGTH = 10_000

#: Top-level domains a bare host must end in before this counts as a link.
#: Requiring one is what separates ``nurzhan.dev`` from ``и т.д.`` and
#: ``Python 3.12``. Not exhaustive, and deliberately so: a domain under a TLD
#: nobody in this market uses is a miss, while a shorter list means fewer
#: letters stopped for nothing.
TOP_LEVEL_DOMAINS = (
    "ru|kz|com|net|org|io|dev|me|co|app|xyz|site|online|info|biz|pro|tech|ai"
    "|uz|by|ua|de|uk|рф|рус|укр|қаз|срб|бел"
)

LINK = re.compile(
    r"""(?xi)
    (?: https?://                       # an explicit scheme, whatever follows
      | www\.                           # or the habit of writing www
      | (?<![\w.])                      # or a bare host: label(.label)*.tld
        \w[\w-]{0,62}
        (?: \.\w[\w-]{0,62} )*
        \. (?: """
    + TOP_LEVEL_DOMAINS
    + r""" ) \b
    )""",
)

#: Both the ASCII at-sign and the full-width one, which arrives from phones.
AT_SIGN = re.compile(r"[@＠]")


class LetterProblem(StrEnum):
    """Why a letter may not be sent as it stands."""

    #: A URL, a bare domain, or something that reads as one.
    CONTAINS_LINK = "contains_link"
    #: An at-sign: an email address, or a messenger handle.
    CONTAINS_AT_SIGN = "contains_at_sign"
    #: Longer than the vacancy accepts; hh would silently truncate it.
    TOO_LONG = "too_long"
    #: Required by this vacancy and not supplied.
    MISSING = "missing"


@final
@dataclass(frozen=True, slots=True)
class SafeLetter:
    """A letter that has passed every check, or no letter at all.

    A distinct type from ``str`` so that the submitter's signature can demand
    one. Passing an unchecked string where this is expected does not type-check,
    which is the difference between a rule and a habit.
    """

    text: str

    def __len__(self) -> int:
        """Characters, which is what hh counts."""
        return len(self.text)


def inspect(text: str | None, *, max_length: int = DEFAULT_MAX_LENGTH) -> list[LetterProblem]:
    """Everything wrong with this letter, in the order a person would fix it.

    A list rather than the first problem, so the human is told once what to
    change instead of discovering the next one after each edit.
    """
    if text is None or not text.strip():
        return []
    problems: list[LetterProblem] = []
    if LINK.search(text):
        problems.append(LetterProblem.CONTAINS_LINK)
    if AT_SIGN.search(text):
        problems.append(LetterProblem.CONTAINS_AT_SIGN)
    if len(text) > max_length:
        problems.append(LetterProblem.TOO_LONG)
    return problems


def check(
    text: str | None, *, required: bool, max_length: int = DEFAULT_MAX_LENGTH
) -> SafeLetter | None:
    """The letter as something sendable, or raise with everything wrong with it.

    Raises rather than returning a result object because there is exactly one
    correct response to a bad letter — stop and show the human — and a return
    value invites a caller to decide otherwise.
    """
    problems = inspect(text, max_length=max_length)
    if required and (text is None or not text.strip()):
        problems.append(LetterProblem.MISSING)
    if problems:
        raise UnsafeLetterError(problems)
    if text is None or not text.strip():
        return None
    return SafeLetter(text)


class UnsafeLetterError(Exception):
    """This letter may not be sent, and these are the reasons.

    Carries every problem so the reporting layer can list them; the message is
    Russian because it reaches the person deciding what to do about it.
    """

    def __init__(self, problems: list[LetterProblem]) -> None:
        self.problems = problems
        described = ", ".join(_RUSSIAN[problem] for problem in problems)
        super().__init__(f"Письмо нельзя отправить как есть: {described}")


#: What each problem is called in the terminal, where a person reads it.
_RUSSIAN: dict[LetterProblem, str] = {
    LetterProblem.CONTAINS_LINK: "в тексте ссылка",
    LetterProblem.CONTAINS_AT_SIGN: "в тексте символ @",
    LetterProblem.TOO_LONG: "письмо длиннее лимита вакансии",
    LetterProblem.MISSING: "эта вакансия требует сопроводительное письмо",
}
