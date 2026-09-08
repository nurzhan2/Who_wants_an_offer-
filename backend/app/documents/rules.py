"""The hard rules a generated document is held to, and the identity of the set.

The brief for this feature says a document that fails a hard rule is not handed
over: the person is shown what is wrong instead. It also says every stored
document records the version of the rules it was written under, so that two
versions of the same CV can be compared knowing whether the rules moved between
them.

**The workshop is not in this branch, and this is the stopgap that says so.**
The phase that lets the owner write rules by hand — counts, required sections,
forbidden words, lengths, each with a scope and a strictness — is a separate one
and is not merged. Until it is, the hard rules are the built-in ones: the
constants in :mod:`app.documents.guard`, which are the rules that are not a
matter of taste. They are enforced the same way stored rules will be — code
reading the finished document, never an instruction in the prompt — so the
enforcement point does not move when the workshop arrives. What moves is where
the list comes from, and that is :func:`current`, one function.

**Why the version is a fingerprint rather than a number.** A hand-maintained
version constant records that somebody remembered to bump it. A fingerprint of
the values themselves records what was actually in force: change a threshold and
the identity changes with it, whether or not anyone noticed. That is the
property the stored column is for — "why does version 3 differ from version 2"
has to be answerable from the rows, and a stale constant answers it wrongly.

The digest is short on purpose. It is an identity, not a checksum against
tampering: twelve hexadecimal characters distinguish every rule set this project
will ever have, and a sixty-four character string in a UI column helps nobody.
"""

import hashlib
from dataclasses import dataclass

from app.documents import guard
from app.documents.context import MAX_EXPERIENCE_ENTRIES, MAX_SUMMARY_CHARS

#: How the identity is spelled, so a stored value says what produced it. When
#: the workshop lands its rule sets get their own prefix and the two are
#: distinguishable at a glance in the documents list.
VERSION_PREFIX = "builtin"

#: Characters of the digest kept. See the module docstring.
DIGEST_CHARS = 12


@dataclass(frozen=True, slots=True)
class HardRule:
    """One rule a document must satisfy before it is handed over.

    ``key`` is stable and machine-readable; ``ru`` is what the person is shown
    when the document is withheld. Both are needed: the UI groups by the first
    and displays the second, and a rule that can only be displayed cannot be
    tested against.
    """

    key: str
    ru: str
    #: The value in force, rendered. Part of the fingerprint, so changing a
    #: threshold changes the rule set's identity.
    value: str


def current() -> tuple[HardRule, ...]:
    """The hard rules in force for a CV right now.

    Reads the constants rather than restating them, so this cannot drift from
    what :mod:`app.documents.guard` actually enforces. When the workshop lands,
    this function is where its stored rules are loaded and merged in — and the
    built-ins stay, because "every fact traces to the profile" is not a
    preference the owner gets to switch off.
    """
    return (
        HardRule(
            key="facts_traceable",
            ru="каждый факт в документе прослеживается до профиля",
            value="always",
        ),
        HardRule(
            key="min_skills",
            ru=f"в разделе навыков не меньше {guard.MIN_SKILLS} пунктов",
            value=str(guard.MIN_SKILLS),
        ),
        HardRule(
            key="min_document_chars",
            ru=f"документ длиннее {guard.MIN_DOCUMENT_CHARS} символов",
            value=str(guard.MIN_DOCUMENT_CHARS),
        ),
        HardRule(
            key="max_summary_chars",
            ru=f"блок «о себе» не длиннее {MAX_SUMMARY_CHARS} символов",
            value=str(MAX_SUMMARY_CHARS),
        ),
        HardRule(
            key="max_experience_entries",
            ru=f"в опыте не больше {MAX_EXPERIENCE_ENTRIES} мест работы",
            value=str(MAX_EXPERIENCE_ENTRIES),
        ),
        HardRule(
            key="single_column",
            ru="одна колонка, без таблиц: двухколоночная вёрстка перемешивает строки",
            value="always",
        ),
    )


def version() -> str:
    """Identity of the rule set in force, for the row a document is stored in."""
    material = "\n".join(f"{rule.key}={rule.value}" for rule in current())
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:DIGEST_CHARS]
    return f"{VERSION_PREFIX}:{digest}"


def describe() -> tuple[str, ...]:
    """The rules as a person reads them, for the screen that withholds a document."""
    return tuple(rule.ru for rule in current())
