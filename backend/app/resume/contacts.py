"""Pull a contact block out of resume text.

Rule-based on purpose. The model has already read the resume by the time this
runs and could be asked for the phone number too, but three things argue
against it: the answer would cost a second round trip on every upload, it would
not be reproducible, and a hallucinated digit in a phone number is invisible —
it looks exactly like a real one. Regexes are dull, deterministic and wrong in
ways a person can see.

**Not shared with** :mod:`app.resume.ats_audit`, which also matches emails and
phones. That module answers "does this page carry a contact line at all", where
a false positive costs nothing; this one produces the value that gets printed
on a CV, where a false positive is a wrong phone number on a document sent to
an employer. Same shapes, opposite tolerances — sharing one pattern between
them would mean tuning it for both at once and getting neither right.

The phone rules are where the care goes. A resume is full of digit groups that
are not phone numbers — ``от 1 400 000 KZT``, ``03.2021 - 05.2023``, a postal
code, a year — so a candidate is only accepted when something marks it out: a
label in front of it, a leading ``+``, or the brackets and dashes people write
numbers with. A bare run of digits separated by spaces is left alone, because
that is what a salary looks like.

Nothing here logs, and nothing here calls out. The text goes in, values come
out, and the caller decides what may be written.
"""

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.schemas.contact import DEFAULT_LINK_KIND

#: An address in running text. Anchored on both sides by characters that cannot
#: be part of one, so "почта:k.maketov@example.com." yields the address without
#: the trailing full stop.
EMAIL = re.compile(
    r"(?<![A-Za-z0-9._%+-])"
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*"
    r"\.[A-Za-z]{2,24}"
)

#: The characters a phone number may be written with, once it has started.
PHONE_BODY = r"[\d\s().-]{5,24}\d"

#: A number introduced by a word. The strongest signal there is: whoever wrote
#: "Телефон:" in front of it meant a telephone.
LABELLED_PHONE = re.compile(
    r"(?:телефон|тел|моб(?:ильный)?|сот(?:овый)?|phone|mobile|cell|tel)"
    # The dashes are written as escapes so the pattern stays readable as ASCII:
    # an en dash and an em dash are indistinguishable from a hyphen on screen.
    r"\s*[:.\u2013\u2014-]?\s*"
    rf"(\+?\d{PHONE_BODY})",
    re.IGNORECASE,
)

#: An international number. The ``+`` is a claim about what the digits are.
PLUS_PHONE = re.compile(rf"(\+\d{PHONE_BODY})")

#: A national number written with the punctuation people use for phones:
#: "8 (700) 000-00-15". The brackets or dashes are what separate it from money.
PUNCTUATED_PHONE = re.compile(r"(\d[\d\s]*[(-][\d\s().-]{4,20}\d)")

#: Digits only, for counting and for the date guard.
DIGITS = re.compile(r"\d")

#: ``03.2021`` or ``12/2019``. A candidate containing one is a date range that
#: happened to be punctuated like a phone number.
DATE_LIKE = re.compile(r"\b\d{1,2}[./]\d{4}\b")

#: Shortest and longest national number worth believing. E.164 caps at 15
#: digits; below 7 there is nothing to distinguish a phone from a house number.
MIN_PHONE_DIGITS = 7
MAX_PHONE_DIGITS = 15

#: A full URL anywhere in the text. Trailing punctuation is trimmed afterwards
#: rather than excluded here, because a URL may legitimately end in ``)``.
URL = re.compile(r"https?://[^\s<>\"'«»]+", re.IGNORECASE)

#: Hosts people write without a scheme, which is most of them. Only hosts this
#: module can classify: a bare "example.com" in prose is a company name far
#: more often than it is the candidate's website.
BARE_URL = re.compile(
    # The lookbehind keeps this from firing a second time inside a URL that
    # :data:`URL` has already matched: "https://www.github.com/x" would
    # otherwise also yield a bare "github.com/x" and store the same profile
    # twice under two spellings.
    r"(?<![A-Za-z0-9@._/:-])"
    r"((?:www\.)?"
    r"(?:github\.com|gitlab\.com|t\.me|telegram\.me|linkedin\.com|"
    r"behance\.net|dribbble\.com|medium\.com|habr\.com|stackoverflow\.com)"
    r"/[^\s<>\"'«»]+)",
    re.IGNORECASE,
)

#: "Telegram: @nickname". A bare ``@nickname`` is deliberately not matched: it
#: is as likely to be a truncated email or a mention of an employer's handle.
TELEGRAM_HANDLE = re.compile(
    r"(?:telegram|telegramm|телеграмм?|тг|tg)"
    r"\s*[:.\u2013\u2014-]?\s*@([A-Za-z0-9_]{4,32})",
    re.IGNORECASE,
)

#: Characters a URL is allowed to end on. Prose puts the rest there.
TRAILING_NOISE = ".,;:!?)]}\u00bb\"'\u2013\u2014-"

#: host suffix -> link kind. Matched against the host and every parent domain,
#: so ``www.github.com`` and ``ru.linkedin.com`` both land on the right one.
HOSTS: dict[str, str] = {
    "github.com": "github",
    "gitlab.com": "gitlab",
    "t.me": "telegram",
    "telegram.me": "telegram",
    "telegram.dog": "telegram",
    "linkedin.com": "linkedin",
}

#: Enough for any real contact block, and a bound on what a hostile resume can
#: make this write. The owner can add more by hand.
MAX_EXTRACTED_LINKS = 10


@dataclass(frozen=True, slots=True)
class ExtractedLink:
    """One address found in the resume, classified by its host."""

    kind: str
    url: str


@dataclass(frozen=True, slots=True)
class ExtractedContacts:
    """What the resume text says about reaching its author.

    Everything is optional: plenty of resumes carry no links at all, and a
    scanned one carries nothing this module can see. Absent means "not found",
    never "the person has none" — which is why the caller only ever fills gaps
    with it and never clears a field.
    """

    email: str | None = None
    phone: str | None = None
    links: tuple[ExtractedLink, ...] = ()

    def __bool__(self) -> bool:
        """True when anything at all was found."""
        return bool(self.email or self.phone or self.links)


def digit_count(value: str) -> int:
    """How many digits a candidate string contains."""
    return sum(1 for character in value if character.isdigit())


def normalise_phone(candidate: str) -> str | None:
    """Tidy a matched phone number, or reject it as not one.

    The rejections are the point. A date range punctuated with a hyphen and a
    salary written in groups of three both survive the patterns above; neither
    survives this.
    """
    # ``split`` folds every kind of Unicode space, the non-breaking one a PDF
    # puts between the country code and the rest included.
    value = " ".join(candidate.split()).strip(" -.")
    if not value or DATE_LIKE.search(value):
        return None
    if not MIN_PHONE_DIGITS <= digit_count(value) <= MAX_PHONE_DIGITS:
        return None
    return value


def find_phone(text: str) -> str | None:
    """The first believable phone number, trying the strongest signal first.

    Order matters and is the whole heuristic: a labelled number beats an
    unlabelled one, an international number beats a national one, and a number
    written with brackets beats a bare run of digits — which never wins at all.
    """
    for pattern in (LABELLED_PHONE, PLUS_PHONE, PUNCTUATED_PHONE):
        for match in pattern.finditer(text):
            phone = normalise_phone(match.group(1))
            if phone is not None:
                return phone
    return None


def find_email(text: str) -> str | None:
    """The first address in the text, or None."""
    match = EMAIL.search(text)
    return match.group(0) if match else None


def classify(url: str) -> str:
    """The kind slug for a URL, from its host.

    Unknown hosts are ``website`` rather than something cleverer. Guessing that
    a personal domain is a "portfolio" would be a label the owner never chose,
    and they can change it in one click.
    """
    host = (urlsplit(url).hostname or "").lower()
    parts = host.split(".")
    for index in range(len(parts) - 1):
        kind = HOSTS.get(".".join(parts[index:]))
        if kind is not None:
            return kind
    return DEFAULT_LINK_KIND


def tidy_url(raw: str) -> str | None:
    """Trim prose off the end of a URL and give it a scheme.

    Returns None for anything left without a host, which is what a match on
    something that only looked like a URL degenerates to.
    """
    url = raw.strip().rstrip(TRAILING_NOISE)
    if not url:
        return None
    if not url.lower().startswith(("http://", "https://")):
        # Bare hosts are written without a scheme far more often than with one.
        # https, not http: every host this module recognises redirects to it,
        # and a stored http link is one the owner would have to fix by hand.
        url = f"https://{url}"
    parsed = urlsplit(url)
    if not parsed.hostname or parsed.username or parsed.password:
        return None
    return url


def find_links(text: str) -> tuple[ExtractedLink, ...]:
    """Every address in the text, deduplicated and capped.

    Deduplication is on the URL as stored, so the same profile written once
    with a scheme and once without collapses to one link rather than to two
    that differ by five characters.
    """
    found: list[ExtractedLink] = []
    seen: set[str] = set()

    def add(url: str | None) -> None:
        """Keep a tidied URL if it is new and there is room."""
        if url is None or url in seen or len(found) >= MAX_EXTRACTED_LINKS:
            return
        seen.add(url)
        found.append(ExtractedLink(kind=classify(url), url=url))

    for match in URL.finditer(text):
        add(tidy_url(match.group(0)))
    for match in BARE_URL.finditer(text):
        add(tidy_url(match.group(1)))
    for match in TELEGRAM_HANDLE.finditer(text):
        add(tidy_url(f"https://t.me/{match.group(1)}"))
    return tuple(found)


def extract(raw_text: str | None) -> ExtractedContacts:
    """Read the contact block out of resume text.

    The one entry point. Empty text is not an error: a scanned resume has no
    text layer at all, and an empty result says exactly that.
    """
    if not raw_text:
        return ExtractedContacts()
    return ExtractedContacts(
        email=find_email(raw_text),
        phone=find_phone(raw_text),
        links=find_links(raw_text),
    )
