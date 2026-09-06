"""Consent, as a value that cannot be edited into meaning something else.

The agent sends job applications from a real person's account to real employers.
Nothing here is undoable, so the interesting question is not "where do we check
that the human agreed" but "what shape must the code have so that sending
something they did not agree to is not expressible".

A boolean cannot carry that. ``if confirmed: submit(item)`` is true of a
confirmation for a different vacancy, for an older version of the letter, or
from twenty minutes ago before the queue was refetched. So consent is a value —
:class:`SendMandate` — and it binds *what will be sent*, not merely *that
something was approved*.

Three properties, and each of them was arrived at by attacking the previous
version rather than by reasoning about it. The first draft carried an opaque
token drawn from a module-private set, which is the obvious design and is
forgeable two ways:

    dataclasses.replace(mandate, vacancy_id="000", letter="never shown")
    → SUCCEEDED

``replace`` re-runs ``__post_init__``, but it *copies the token from the
existing instance*, so the check passes and one honest confirmation for vacancy
A becomes a valid mandate for vacancy B carrying a letter nobody read.

    SendMandate.__new__(SendMandate)  + object.__setattr__(...)
    → SUCCEEDED, and the gate accepted it

``__new__`` never calls ``__init__``, so no validation runs at all.

What survives both, measured against the same attacks:

**The signature is over the content.** It is an HMAC of the vacancy id, the
digest of the letter and the digest of the form the human was shown. Change any
of them and the signature no longer matches, so ``replace`` produces an object
that refuses to construct. A mandate is therefore not "permission to apply", it
is "permission to send exactly this".

**The gate re-verifies instead of trusting the object.** :func:`verify` recomputes
the signature from the fields in front of it, so an instance conjured past
``__init__`` is rejected at the point of use. Validation that only happens in a
constructor is validation that ``__new__`` skips.

**The registry holds live signatures, not spent ones.** An unknown mandate is
refused, so the failure mode of a bug in the bookkeeping is that a legitimate
application does not go out — never that an unknown one does. A "spent" set has
the opposite failure mode and reads identically at a glance.

The secret is generated per process and never written anywhere. That is not
key management, it is the point: a mandate cannot outlive the process that
minted it, so a confirmation cannot be replayed tomorrow, and ``__reduce__``
raises so it cannot be pickled into a queue or a journal and brought back.
"""

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, final

#: Per process, never persisted, never logged. A mandate is meaningless outside
#: the run that minted it, which is exactly the lifetime consent should have.
_SECRET: Final[bytes] = secrets.token_bytes(32)

#: Signatures that have been minted and not yet spent. Live rather than spent so
#: that anything unrecognised is refused.
_LIVE: set[str] = set()


class MandateError(PermissionError):
    """The base for every refusal in this module. Never caught per-vacancy."""


class ForgedMandateError(MandateError):
    """The signature does not match the contents it is supposed to bind."""


class SpentMandateError(MandateError):
    """This consent has been used, or was never minted by this process."""


def digest(text: str | None) -> str:
    """A stable digest of a piece of text, or of its absence."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _sign(vacancy_id: str, letter: str | None, form_digest: str) -> str:
    """The signature binding one mandate to one exact payload."""
    material = f"{vacancy_id}\x00{digest(letter)}\x00{form_digest}".encode()
    return hmac.new(_SECRET, material, hashlib.sha256).hexdigest()


@final
@dataclass(frozen=True, slots=True)
class SendMandate:
    """A human's decision to send one specific application, once.

    Construct it only through :func:`mint`, which is called from exactly one
    place: the interactive confirmation. Every function that can cause an
    application to leave takes one of these positionally, so under
    ``mypy --strict`` there is no route to the submitter without one.
    """

    #: The vacancy this consent is for. Compared against the page before the
    #: click, so a stale or mis-scrolled page cannot borrow it.
    vacancy_id: str
    #: Exactly the letter the human saw. The submitter types this and has no
    #: other string in scope to type.
    letter: str | None
    #: Digest of the form as it was shown, so a form that changed underneath
    #: the confirmation invalidates it.
    form_digest: str
    #: HMAC over the three fields above. See the module docstring.
    signature: str
    confirmed_at: datetime

    def __post_init__(self) -> None:
        """Refuse to exist if the signature does not match the contents."""
        if not hmac.compare_digest(
            self.signature, _sign(self.vacancy_id, self.letter, self.form_digest)
        ):
            raise ForgedMandateError("mandate does not match the payload it carries")

    def __reduce__(self) -> Any:
        """Refuse to be serialised, in any form.

        ``Any`` because this is the pickle protocol's own signature and the
        method never returns. A mandate that could be written down could be read
        back tomorrow, which is precisely the replay this type exists to stop —
        so it may not reach a journal, a queue file or a subprocess.
        """
        raise TypeError("a SendMandate must not cross a process boundary")


def mint(*, vacancy_id: str, letter: str | None, form_digest: str) -> SendMandate:
    """Create consent for one application. Call this from the confirmation only.

    Deliberately not named ``create``: every call site is a place where a human
    said yes, and the word should look wrong anywhere else. ``test_boundaries``
    asserts that this module's only production caller is ``agent/human.py``.
    """
    signature = _sign(vacancy_id, letter, form_digest)
    _LIVE.add(signature)
    return SendMandate(
        vacancy_id=vacancy_id,
        letter=letter,
        form_digest=form_digest,
        signature=signature,
        confirmed_at=datetime.now(UTC),
    )


def verify(mandate: SendMandate) -> None:
    """Check a mandate at the point of use, and spend it. Raises, never returns False.

    Recomputes the signature rather than trusting the instance: an object built
    through ``__new__`` never ran ``__post_init__``, and this is the only place
    that catches it. Spending is part of the same call so that no caller can
    verify without consuming — a check that leaves the mandate usable is a check
    a retry loop walks straight past.
    """
    expected = _sign(mandate.vacancy_id, mandate.letter, mandate.form_digest)
    if not hmac.compare_digest(mandate.signature, expected):
        raise ForgedMandateError("mandate does not match the payload it carries")
    if mandate.signature not in _LIVE:
        raise SpentMandateError("this consent has already been used, or was never given")
    _LIVE.discard(mandate.signature)
