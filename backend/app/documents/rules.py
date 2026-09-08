"""The hard rules a generated document is held to, and the identity of the set.

The brief for this feature says a document that fails a hard rule is not handed
over: the person is shown what is wrong instead. It also says every stored
document records the version of the rules it was written under, so that two
versions of the same CV can be compared knowing whether the rules moved between
them.

**The workshop is where the list comes from.** This module used to hold its own
copy — the constants in :mod:`app.documents.guard`, written while the phase that
lets the owner author rules was on another branch, with :func:`current` named as
the one seam that would move when it landed. It has landed, and it has moved:
the rules a CV is held to are now :func:`app.workshop.store.active_rules` for
``RuleScope.CV``, which is the owner's own list plus the two built-ins nobody can
switch off.

That matters for one rule in particular. The owner's example when they asked for
the workshop was «в навыках не меньше 21 пункта», and a rule written with
``scope=cv`` had nothing enforcing it: letters checked only ``cover_letter``
rules and a CV checked only these constants, so a CV rule was a row in a table
that no document was ever measured against. Now both scopes are enforced by the
same checker on the finished text.

The structural guarantees stay here and stay unconditional — every fact traces
to the profile, one column, no invented skills. They are not preferences the
owner gets to switch off, they are checked against the *arrangement* rather than
against the text, and :mod:`app.documents.guard` is where they live.

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
from collections.abc import Sequence
from dataclasses import dataclass

from app.db.enums import RuleSeverity
from app.documents import guard
from app.documents.context import MAX_EXPERIENCE_ENTRIES, MAX_SUMMARY_CHARS
from app.workshop.rules import RuleSpec

#: How the identity is spelled, so a stored value says what produced it. The
#: rules behind it are the workshop's, so a document written before the owner
#: edited a rule and one written after carry different values.
VERSION_PREFIX = "workshop"

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


def current(rules: Sequence[RuleSpec] = ()) -> tuple[HardRule, ...]:
    """The hard rules in force for a CV right now.

    The structural ones first, from the constants that enforce them, so this
    cannot drift from what :mod:`app.documents.guard` actually checks. Then the
    workshop's, hard ones only: a soft rule is a warning printed beside a
    document that was handed over, and this list is the set that can stop one.
    """
    structural = (
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
    authored = tuple(
        HardRule(
            key=f"workshop:{rule.id}",
            ru=rule.message,
            # The parameters rather than the message: an owner who rewords a rule
            # without changing what it checks has not changed the rule set, and a
            # fingerprint that moved would say two identical documents were
            # written under different rules.
            value=rule.params.model_dump_json(),
        )
        for rule in rules
        if rule.severity is RuleSeverity.HARD
    )
    return structural + authored


def version(rules: Sequence[RuleSpec] = ()) -> str:
    """Identity of the rule set in force, for the row a document is stored in."""
    material = "\n".join(f"{rule.key}={rule.value}" for rule in current(rules))
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:DIGEST_CHARS]
    return f"{VERSION_PREFIX}:{digest}"


def describe(rules: Sequence[RuleSpec] = ()) -> tuple[str, ...]:
    """The rules as a person reads them, for the screen that withholds a document."""
    return tuple(rule.ru for rule in current(rules))
