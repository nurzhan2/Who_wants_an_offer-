"""The contact block a generated CV carries, read out of the stored resume.

A CV that an employer cannot reply to is not a CV. This project's own auditor
agrees in the strongest terms it has: ``check_contacts`` raises
``CONTACTS_NOT_TEXT`` at ``CRITICAL`` severity for thirty-five points, and a
critical finding is what makes ``ATSReport.is_machine_readable`` false. So the
contact block is not decoration on the generated document — it is the difference
between a document the system is willing to hand over and one it is not.

**Where the values come from.** ``profile_contact`` — the table behind the
"Мои данные" screen — is the source, and :func:`from_stored` is the seam this
module was written to have. The parsing below is what fills that table in the
first place and what still answers for a profile uploaded before the screen
existed, so it is the fallback rather than the source.

Which way round they compose is the whole point of the screen. A field the owner
has settled wins over anything a parser reads off a page, **including a field
they deliberately cleared**: ``edited.phone`` with no phone means "I have no
number here, stop filling it in", and a fallback that helpfully re-read one out
of ``raw_text`` would make the form a suggestion box. Only a field nobody has
touched falls back.

**Nothing here is ever logged.** These are the owner's phone number and email
address, and ``backend/tests/test_pii_logging.py`` exists to keep resume content
out of the log stream. The functions below return values; they emit no log lines
at all, and the modules that call them log counts rather than contents.

**The patterns are the auditor's own.** ``EMAIL`` and ``PHONE`` are imported
from :mod:`app.resume.ats_audit` rather than written again here, because the
question this module asks ("what is the candidate's email") and the question the
audit asks ("can a parser find an email in this document") have to have the same
answer. Two copies of the pattern would eventually disagree, and the shape of
that disagreement is a generated CV whose email is written in a form the
auditor's own regular expression cannot see.
"""

import re

from pydantic import BaseModel, ConfigDict

from app.resume.ats_audit import EMAIL, MIN_PHONE_DIGITS, PHONE
from app.schemas.contact import ProfileContactRead

#: Handles and addresses worth carrying into a CV, by the label to print. Order
#: is the order they are rendered in. Deliberately a small closed list: an
#: arbitrary URL found in a resume is as likely to be a former employer's site
#: or an article the candidate linked as it is to be a contact.
LINK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("GitHub", re.compile(r"\b(?:https?://)?(?:www\.)?github\.com/[\w.-]+/?", re.IGNORECASE)),
    (
        "LinkedIn",
        re.compile(r"\b(?:https?://)?(?:[\w-]+\.)?linkedin\.com/in/[\w%-]+/?", re.IGNORECASE),
    ),
    ("Telegram", re.compile(r"\b(?:https?://)?(?:t\.me|telegram\.me)/[\w]+/?", re.IGNORECASE)),
    ("GitLab", re.compile(r"\b(?:https?://)?(?:www\.)?gitlab\.com/[\w.-]+/?", re.IGNORECASE)),
)

#: A Telegram handle written the way people write it, when no t.me link is
#: given. Anchored on a preceding word so an email's local part cannot match.
TELEGRAM_HANDLE = re.compile(r"(?:telegram|телеграм|тг)\W{0,3}@([A-Za-z][\w]{4,31})", re.IGNORECASE)


class ContactBlock(BaseModel):
    """What the top of the generated CV says, and nothing else.

    Frozen and closed to stray keys like every other fact model in this feature:
    these values are rendered into a document that goes to an employer, and a
    field that arrived from somewhere unaccounted for has no business in one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str | None = None
    email: str | None = None
    #: As written in the resume. Not normalised: phone formatting differs by
    #: country and rewriting somebody's own number is not this code's business.
    phone: str | None = None
    city: str | None = None
    #: ``("GitHub", "github.com/nurzhan")`` and so on, in :data:`LINK_PATTERNS`
    #: order. A tuple of pairs rather than a dict so the render order is the
    #: data's rather than a dict's.
    links: tuple[tuple[str, str], ...] = ()

    @property
    def is_reachable(self) -> bool:
        """Whether an employer could actually get in touch.

        The same two facts ``app.resume.ats_audit.check_contacts`` looks for, so
        that a caller can tell before rendering what the audit is going to say
        after it.
        """
        return bool(self.email and self.phone)


def find_email(text: str) -> str | None:
    """The first address in the resume text, or None."""
    match = EMAIL.search(text)
    return match.group(0) if match else None


def find_phone(text: str) -> str | None:
    """The first thing in the resume text with enough digits to be a phone.

    The digit floor is the auditor's, and it is what stops "04.2022 — 09.2023"
    from being read as a phone number: an employment period is digits, spaces
    and a dash, which the pattern alone happily matches.
    """
    for match in PHONE.findall(text):
        if sum(char.isdigit() for char in match) >= MIN_PHONE_DIGITS:
            return str(match).strip()
    return None


def find_links(text: str) -> tuple[tuple[str, str], ...]:
    """Profile addresses worth putting in a CV, labelled, deduplicated.

    The scheme is stripped: ``github.com/nurzhan`` is what people write on a CV,
    it is what a parser matches, and it is one line shorter.
    """
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for label, pattern in LINK_PATTERNS:
        match = pattern.search(text)
        if match is None:
            continue
        address = _bare(match.group(0))
        if address.lower() in seen:
            continue
        seen.add(address.lower())
        found.append((label, address))

    if not any(label == "Telegram" for label, _ in found):
        handle = TELEGRAM_HANDLE.search(text)
        if handle is not None:
            found.append(("Telegram", f"@{handle.group(1)}"))
    return tuple(found)


def _bare(url: str) -> str:
    """A URL without its scheme, its ``www.`` or its trailing slash."""
    for prefix in ("https://", "http://"):
        if url.lower().startswith(prefix):
            url = url[len(prefix) :]
    if url.lower().startswith("www."):
        url = url[4:]
    return url.rstrip("/")


def from_stored(
    stored: ProfileContactRead | None,
    *,
    text: str | None = None,
    name: str | None = None,
    city: str | None = None,
) -> ContactBlock:
    """The contact block for a generated document: the owner's, then the page's.

    ``stored`` is what the "Мои данные" screen holds. Every field it carries is
    used as it stands; a field it does not carry falls back to what
    :func:`from_resume_text` can find in the resume — unless the owner has
    settled that field, in which case their answer stands even when it is empty.

    Links are taken whole from ``stored`` when it has any, rather than merged one
    by one. A merge would put back a GitHub URL the owner had removed, and the
    list is short enough that "these are my links" is a thing a person can say
    completely.
    """
    parsed = from_resume_text(text, name=name, city=city)
    if stored is None:
        return parsed

    edits = stored.edited

    def settled(value: str | None, *, edited: bool, fallback: str | None) -> str | None:
        if edited:
            return value
        return value if value is not None else fallback

    return ContactBlock(
        name=settled(stored.full_name, edited=edits.full_name, fallback=parsed.name),
        email=settled(stored.email, edited=edits.email, fallback=parsed.email),
        phone=settled(stored.phone, edited=edits.phone, fallback=parsed.phone),
        city=settled(stored.city, edited=edits.city, fallback=parsed.city),
        links=(
            tuple((link.label or link.kind, link.url) for link in stored.links)
            if stored.links
            else parsed.links
        ),
    )


def from_resume_text(
    text: str | None, *, name: str | None = None, city: str | None = None
) -> ContactBlock:
    """Read the contact block out of the resume the profile was built from.

    ``name`` and ``city`` are passed in rather than parsed, because the profile
    already holds both: extraction wrote them into columns, and a person may
    since have corrected them there. Re-deriving them from ``raw_text`` would
    quietly overrule that correction, which is the one thing a fallback must not
    do — see :func:`from_stored`, which is where the owner's answer wins.

    Empty text yields an empty block rather than an error. A profile parsed from
    a file whose text layer was unreadable genuinely has nothing to read, and
    the caller finds that out from :attr:`ContactBlock.is_reachable` and from the
    audit, both of which say so in terms a person can act on.
    """
    if not text or not text.strip():
        return ContactBlock(name=name, city=city)
    return ContactBlock(
        name=name,
        email=find_email(text),
        phone=find_phone(text),
        city=city,
        links=find_links(text),
    )
