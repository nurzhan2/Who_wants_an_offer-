"""Reading a contact block out of resume text.

This module is a pile of regexes, and the reason it gets a test file of its own
is that the interesting behaviour is what it *refuses*. A resume is dense with
digit groups that are not phone numbers and with domains that are not the
candidate's: a salary written in thousands, a date range, an employer's
website. Every false positive here ends up printed on a document sent to an
employer, where nobody checks it against the original.

So the tests come in pairs — what must be found, and what must not be — and the
second half is the one worth keeping.

Every person, number and address below is invented; the domains are the
reserved ``example.com`` or the real hosts whose *shape* is being classified.
"""

import pytest

from app.resume import contacts
from app.schemas.contact import ContactLinkWrite, ProfileContactUpdate

pytestmark = pytest.mark.unit


# ── email ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Email: k.maketov@example.com", "k.maketov@example.com"),
        # A full stop ending the sentence is not part of the address.
        ("Пишите на k.maketov@example.com.", "k.maketov@example.com"),
        ("<a.testwood@example.co.uk>", "a.testwood@example.co.uk"),
        ("почта:k.maketov+jobs@example.com", "k.maketov+jobs@example.com"),
    ],
)
def test_an_address_is_read_without_the_prose_around_it(text: str, expected: str) -> None:
    """Contacts are written inline, in a sentence, in a table cell or inside
    angle brackets. What comes back has to be the address and nothing else —
    a trailing full stop makes it undeliverable and nothing downstream looks."""
    assert contacts.extract(text).email == expected


@pytest.mark.parametrize(
    "text",
    [
        "Email: см. выше",
        "@example.com",
        "user@localhost",
        "почта не указана",
    ],
)
def test_something_that_is_not_an_address_is_not_reported_as_one(text: str) -> None:
    """An empty contact block is honest. A made-up address is not: it would be
    prefilled, shown as if it had been read off the page, and mailed to."""
    assert contacts.extract(text).email is None


# ── phone ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Телефон: +7 700 000 00 15", "+7 700 000 00 15"),
        ("Тел.: 8 (700) 000-00-15", "8 (700) 000-00-15"),
        ("Phone: +995 500 00 00 13", "+995 500 00 00 13"),
        ("Mobile +1 (555) 010-4477", "+1 (555) 010-4477"),
        # A PDF puts non-breaking spaces inside a number; they are still spaces.
        ("телефон +7 700 000 00 15", "+7 700 000 00 15"),
        # No label at all: the leading + is the claim.
        ("Астана · +7 700 000 00 15 · k.maketov@example.com", "+7 700 000 00 15"),
    ],
)
def test_a_number_is_read_in_whatever_format_it_was_written(text: str, expected: str) -> None:
    """Every country writes these differently and this service never dials one,
    so the rule is to keep what the person wrote and just tidy the whitespace."""
    assert contacts.extract(text).phone == expected


@pytest.mark.parametrize(
    "text",
    [
        # The trap this module exists for: a salary is a run of digit groups.
        "Зарплатные ожидания: от 1 400 000 KZT",
        "Ожидания по доходу 2 500 000 тенге на руки",
        # A date range punctuated exactly like a phone number.
        "03.2021 - 05.2023",
        "Опыт работы 09.2018 - 01.2020, затем 02.2020 - 06.2022",
        # Too few digits to be anybody's number.
        "Кабинет 415-16",
        # Too many: this is an IBAN-shaped thing, not a phone.
        "Счёт 1234 5678 9012 3456 7890",
    ],
)
def test_a_number_that_is_not_a_phone_number_is_left_alone(text: str) -> None:
    """The failure this guards against is silent. Nobody notices that the phone
    field holds a salary until an employer tries to call it."""
    assert contacts.extract(text).phone is None


def test_a_labelled_number_wins_over_an_unlabelled_one() -> None:
    """A resume mentions plenty of numbers; only one has "Телефон" in front of
    it. The label is the strongest evidence available and is tried first, so
    the order of the lines on the page cannot decide the answer."""
    text = "+7 717 000 00 99 — приёмная работодателя\nТелефон: +7 700 000 00 15"

    assert contacts.extract(text).phone == "+7 700 000 00 15"


# ── links ─────────────────────────────────────────────────────────────


def test_a_link_is_classified_by_its_host() -> None:
    """The kind is what the UI groups and labels by, and it is derived rather
    than asked for: nobody types "this is my GitHub" next to a github.com URL."""
    text = (
        "GitHub: https://github.com/maketov\n"
        "GitLab https://gitlab.com/maketov\n"
        "LinkedIn: https://www.linkedin.com/in/maketov/\n"
        "Сайт: https://maketov.example.com/cv\n"
    )

    found = {link.kind: link.url for link in contacts.extract(text).links}

    assert found == {
        "github": "https://github.com/maketov",
        "gitlab": "https://gitlab.com/maketov",
        "linkedin": "https://www.linkedin.com/in/maketov/",
        "website": "https://maketov.example.com/cv",
    }


def test_a_regional_linkedin_subdomain_is_still_linkedin() -> None:
    """Hosts are matched suffix-first, so ``ru.linkedin.com`` and
    ``www.github.com`` land where they belong instead of falling through to the
    "some website" bucket the user then has to relabel by hand."""
    text = "https://ru.linkedin.com/in/maketov  https://www.github.com/maketov"

    kinds = [link.kind for link in contacts.extract(text).links]

    assert kinds == ["linkedin", "github"]


def test_a_host_written_without_a_scheme_gets_one() -> None:
    """Almost nobody writes ``https://`` on a CV. The stored value has to be a
    URL anyway — the API rejects anything else, and a link is rendered as
    something the owner clicks."""
    found = contacts.extract("github.com/maketov, t.me/maketov_k").links

    assert [link.url for link in found] == [
        "https://github.com/maketov",
        "https://t.me/maketov_k",
    ]


def test_a_telegram_handle_becomes_a_link_only_when_it_is_labelled() -> None:
    """``@maketov_k`` on its own is as likely to be half an email or a mention
    of an employer's channel. The word in front of it is what makes it an
    address, so only the labelled form is turned into a link."""
    labelled = contacts.extract("Telegram: @maketov_k").links
    bare = contacts.extract("Пишите мне @maketov_k").links

    assert [link.url for link in labelled] == ["https://t.me/maketov_k"]
    assert bare == ()


def test_the_same_address_written_twice_is_stored_once() -> None:
    """Resumes repeat contacts — once in a sidebar, once in a footer — and the
    two spellings differ only by the scheme. Two rows for one address would be
    two rows the owner has to delete by hand."""
    found = contacts.extract("github.com/maketov\n...\nhttps://github.com/maketov").links

    assert [link.url for link in found] == ["https://github.com/maketov"]


def test_prose_after_a_link_is_not_part_of_it() -> None:
    """A URL at the end of a sentence takes the punctuation with it unless
    something trims it, and a trailing bracket or comma is enough to make the
    link 404 for whoever clicks it."""
    text = "Портфолио (https://maketov.example.com/cv), код — github.com/maketov."

    assert [link.url for link in contacts.extract(text).links] == [
        "https://maketov.example.com/cv",
        "https://github.com/maketov",
    ]


def test_the_number_of_links_taken_from_one_resume_is_bounded() -> None:
    """A resume is an untrusted document. Without a cap, a page carrying a
    hundred URLs — a reference list, a spam-stuffed PDF — becomes a hundred
    rows the owner never asked for."""
    text = "\n".join(f"https://example.com/link-{index}" for index in range(50))

    assert len(contacts.extract(text).links) == contacts.MAX_EXTRACTED_LINKS


def test_a_url_carrying_credentials_is_dropped() -> None:
    """``https://user:token@host`` is a credential. Storing one would put it on
    a screen, in a generated document and in whatever the owner copies next."""
    assert contacts.extract("https://user:secret@example.com/cv").links == ()


def test_everything_extracted_passes_the_validation_the_api_applies() -> None:
    """The two halves of this feature have to agree. Prefill writes rows
    directly and the API validates before writing, so an extractor that
    produced a value the API would reject would leave a block the owner cannot
    save back without editing a field they never touched."""
    text = (
        "Кирилл Макетов\n"
        "Астана · Телефон: +7 700 000 00 15 · k.maketov@example.com\n"
        "github.com/maketov · Telegram: @maketov_k\n"
    )
    found = contacts.extract(text)

    # Raises if anything found here would be a 422 coming the other way.
    ProfileContactUpdate(
        phone=found.phone,
        email=found.email,
        links=[ContactLinkWrite(kind=link.kind, url=link.url) for link in found.links],
    )


# ── the whole block ───────────────────────────────────────────────────


def test_a_real_resume_yields_the_whole_contact_block() -> None:
    """The shapes above, together, on the kind of header every resume opens
    with."""
    text = (
        "Кирилл Макетов\n"
        "Python-разработчик\n"
        "\n"
        "Город: Астана, Казахстан\n"
        "Email: k.maketov@example.com\n"
        "Телефон: +7 700 000 00 15\n"
        "GitHub: github.com/maketov\n"
        "Telegram: @maketov_k\n"
        "Зарплатные ожидания: от 1 400 000 KZT\n"
    )

    found = contacts.extract(text)

    assert found.email == "k.maketov@example.com"
    assert found.phone == "+7 700 000 00 15"
    assert [(link.kind, link.url) for link in found.links] == [
        ("github", "https://github.com/maketov"),
        ("telegram", "https://t.me/maketov_k"),
    ]
    assert found


@pytest.mark.parametrize("text", [None, "", "   \n\n  "])
def test_a_resume_with_no_text_layer_yields_nothing_rather_than_failing(text: str | None) -> None:
    """A scanned resume is a picture of a page: the text is empty and there is
    nothing to read. Empty is a valid answer here — the caller only ever fills
    gaps with this, so "found nothing" can never clear a field."""
    found = contacts.extract(text)

    assert found == contacts.ExtractedContacts()
    assert not found
