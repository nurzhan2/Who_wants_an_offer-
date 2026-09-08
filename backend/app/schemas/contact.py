"""Contact block of a profile: who the candidate is and how to reach them.

Separate from ``app.schemas.profile`` because the two are read by different
code for different reasons. A profile is matched, scored and embedded; a
contact block is copied into a generated document and nothing else. Keeping the
contracts apart is what makes "contacts never reach the matcher" checkable by
reading imports rather than by reading every function.

Validation here is deliberately uneven, and the unevenness is the design:

* **email** is checked for shape, because a malformed address means an employer
  cannot reply and nothing downstream will ever notice;
* **phone** is a free string with one rule — it has to contain a digit. Formats
  differ per country far more than any regex worth maintaining knows, and this
  service has no reason to have an opinion about a number it never dials;
* **links** are checked hard, because a link is the one field that is rendered
  as something clickable: the scheme must be http(s), and a URL carrying
  credentials is rejected outright.
"""

import re
from datetime import datetime
from typing import Annotated, Self
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    Field,
    StringConstraints,
    model_validator,
)

from app.schemas.common import ReadModel

#: Shape only, not deliverability. Anything stricter is a losing argument with
#: RFC 5322, and anything looser lets a resume's "email: см. выше" through as
#: an address. Deliberately not shared with ``app.resume.ats_audit``: that
#: module asks whether the page has a contact line at all, where a false
#: positive is harmless; this one produces a value that gets printed on a CV.
EMAIL_SHAPE = re.compile(
    r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
    r"@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+$"
)

#: A link kind is a slug the UI groups by, not a closed vocabulary: the set of
#: places a developer keeps a profile on changes faster than a migration can.
LINK_KIND_SHAPE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

#: The kinds this codebase recognises today, for the UI to label and sort by.
#: Anything else is stored and shown as typed — the column is a string on
#: purpose, and this tuple is a hint, never a constraint.
KNOWN_LINK_KINDS: tuple[str, ...] = (
    "github",
    "gitlab",
    "telegram",
    "linkedin",
    "website",
    "portfolio",
)

#: Fallback kind for a link whose host says nothing useful.
DEFAULT_LINK_KIND = "website"

#: More than this and it is not a contact block any more. A cap exists so a
#: scripted client cannot turn one profile into an unbounded link farm.
MAX_LINKS = 20


def blank_to_none(value: object) -> object:
    """Read an empty or whitespace-only string as "cleared", not as a value.

    HTML inputs submit ``""`` for a field the user emptied, and storing that
    would leave a phone number that is present, blank, and impossible to
    distinguish from one nobody has filled in yet.
    """
    if isinstance(value, str) and not value.strip():
        return None
    return value


def collapse_whitespace(value: str) -> str:
    """Fold newlines and runs of spaces into single spaces.

    Contact values are routinely pasted out of a PDF, which brings the page's
    line breaks with them. A name split across two lines is the same name.
    """
    return " ".join(value.split())


def validate_email(value: str) -> str:
    """Reject an address that could not receive a reply."""
    if not EMAIL_SHAPE.match(value):
        raise ValueError(f"{value!r} is not a valid email address")
    return value


def validate_phone(value: str) -> str:
    """Accept any national format, reject text that is not a number at all.

    One rule: there has to be a digit in it. "+7 700 000 00 15", "8 (700)
    000-00-15" and "+995 500 00 00 13 (доб. 21)" are all real ways people write
    the same kind of thing, and a service that never dials the number has no
    business preferring one of them.
    """
    if not any(character.isdigit() for character in value):
        raise ValueError("a phone number must contain at least one digit")
    return value


def validate_http_url(value: str) -> str:
    """Only http(s), only with a host, never with credentials in it.

    ``javascript:`` and ``data:`` are rejected because this string is rendered
    as an anchor the owner clicks. Userinfo (``https://user:token@host``) is
    rejected because it is a credential, and a credential in a contact link
    would be stored, displayed and copied into a generated document.
    """
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("a link must be an http(s) URL")
    if not parsed.hostname:
        raise ValueError("a link must have a host")
    if parsed.username or parsed.password:
        raise ValueError("a link must not carry credentials")
    return value


def validate_link_kind(value: str) -> str:
    """Reject a kind that is not a slug, so it stays usable as a UI key."""
    if not LINK_KIND_SHAPE.match(value):
        raise ValueError(f"{value!r} is not a valid link kind")
    return value


FullName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
    AfterValidator(collapse_whitespace),
]
Phone = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=64),
    AfterValidator(collapse_whitespace),
    AfterValidator(validate_phone),
]
EmailAddress = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=3, max_length=320),
    AfterValidator(validate_email),
]
City = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=120),
    AfterValidator(collapse_whitespace),
]
LinkUrl = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=8, max_length=2048),
    AfterValidator(validate_http_url),
]
LinkKind = Annotated[
    str,
    StringConstraints(strip_whitespace=True, to_lower=True, min_length=1, max_length=32),
    AfterValidator(validate_link_kind),
]
LinkLabel = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=60),
    AfterValidator(collapse_whitespace),
]

# The optional forms every write model uses. ``blank_to_none`` sits outside the
# union so an empty string never reaches the format checks: a cleared input is
# a deletion, not a validation error.
OptionalFullName = Annotated[FullName | None, BeforeValidator(blank_to_none)]
OptionalPhone = Annotated[Phone | None, BeforeValidator(blank_to_none)]
OptionalEmail = Annotated[EmailAddress | None, BeforeValidator(blank_to_none)]
OptionalCity = Annotated[City | None, BeforeValidator(blank_to_none)]
OptionalLinkLabel = Annotated[LinkLabel | None, BeforeValidator(blank_to_none)]


def reject_duplicate_urls(links: list["ContactLinkWrite"] | None) -> None:
    """Refuse a link list the database would reject anyway.

    ``profile_contact_link`` is unique on ``(contact_id, url)``, so a repeated
    address arrives as an IntegrityError and, with nothing catching it, becomes
    an opaque 500 that does not say which link was duplicated. A hand-edited
    list is exactly where a duplicate comes from.
    """
    if not links:
        return
    seen: set[str] = set()
    duplicates: list[str] = []
    for link in links:
        if link.url in seen and link.url not in duplicates:
            duplicates.append(link.url)
        seen.add(link.url)
    if duplicates:
        raise ValueError(f"url must be unique; repeated: {sorted(duplicates)}")


class ContactLinkWrite(BaseModel):
    """One link as the owner typed it."""

    kind: LinkKind = DEFAULT_LINK_KIND
    url: LinkUrl
    label: OptionalLinkLabel = None


class ContactLinkRead(ReadModel):
    """One stored link."""

    id: UUID
    kind: str
    url: str
    label: str | None
    #: True when a human typed or kept this link. Extraction never overwrites
    #: those, and the UI marks the rest as "из резюме".
    is_manual: bool


class ContactEdits(BaseModel):
    """Which scalar fields a human has settled, per field.

    This is the whole reason re-parsing a resume is safe. Extraction runs again
    on every upload and would happily replace a corrected phone number with the
    one misread off the page; a field flagged here is never written by anything
    but a person.
    """

    full_name: bool = False
    phone: bool = False
    email: bool = False
    city: bool = False


class ProfileContactUpdate(BaseModel):
    """Manual corrections. Every field optional; unset means unchanged.

    Present-and-null is not the same as absent. ``{"phone": null}`` means "I
    have no phone number here, stop filling it in from my resume" and sets the
    edited flag exactly as a new value would.
    """

    full_name: OptionalFullName = None
    phone: OptionalPhone = None
    email: OptionalEmail = None
    city: OptionalCity = None
    #: Replaces the whole manual link set when present. Links extracted from
    #: the resume that the owner did not keep are dropped with it.
    links: list[ContactLinkWrite] | None = Field(default=None, max_length=MAX_LINKS)

    @model_validator(mode="after")
    def _urls_are_unique(self) -> Self:
        """A repeated URL is a 422 naming it, not a 500 from PostgreSQL."""
        reject_duplicate_urls(self.links)
        return self


class ProfileContactRead(BaseModel):
    """The contact block as the owner's own screen shows it.

    Not a ``ReadModel``: ``edited`` is assembled from four columns rather than
    read off one attribute, so this is built explicitly in the service layer
    instead of validated straight off an ORM row.
    """

    profile_id: UUID
    full_name: str | None = None
    phone: str | None = None
    email: str | None = None
    city: str | None = None
    edited: ContactEdits = Field(default_factory=ContactEdits)
    links: list[ContactLinkRead] = Field(default_factory=list)
    #: When the block was last written, or None when nothing has written one
    #: yet. A profile with no contact row reads as an empty block, not a 404.
    updated_at: datetime | None = None
