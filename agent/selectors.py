"""Every selector this agent uses, and proof that somebody looked at it.

The brief is blunt about why this file exists: the selectors for the apply
button and the letter form are NOT KNOWN, must not be invented, and must not be
recalled from memory — «Угаданный селектор — это молчаливый провал на проде».
hh does use ``data-qa`` attributes, which is exactly what makes guessing feel
safe, and a guess that happens to match something produces an agent that clicks
the wrong thing on somebody's real account.

So a selector here is not a string. It is a string plus the evidence that a
human saw it on a real page, and the evidence is checked at startup rather than
believed.

**Why the evidence and not a date.** The obvious version of this file is a
constant with ``# checked 2026-09-06`` beside it. That reduces the whole
guarantee to "somebody typed a date", and a date is the easiest thing in the
world to type next to a guess. :func:`assert_ready_to_apply` instead parses the
probe's own report and requires that every ``data-qa`` the query depends on is
one the probe recorded seeing, on the stage that can see the form, in a session
it measured as logged in.

The first version of that check asked whether the query string appeared anywhere
in the file as text, and it was wrong in both directions at once. ``json.dumps``
escapes the quotes inside ``[data-qa="…"]``, so a correctly written selector
could never match — the documented unblocking procedure could not be completed.
And any substring of the file could match, so a URL fragment, the JSON key
``letterMaxLength`` and the single letter "a" all passed, as did a one-line file
that is not even valid JSON. Both halves are now tested.

**Why scope matters.** ``APPLY_LINK`` below was measured — but measured
*logged out*, by fetching three public vacancy pages. That is genuinely useful
and it is not enough to apply with: the page a logged-in applicant sees may
carry a different control, and the form behind it was never seen at all. An
anonymous scope is therefore explicitly insufficient for the apply flow, and
:func:`assert_ready_to_apply` says so by name.

**Why this fails at startup.** An unverified selector is not a per-vacancy
error. If it were, running the apply flow before the probe would produce a run
of failures that stops after the second one with the reason "two things went
wrong", which is a true statement about the wrong problem. It raises
:class:`SelectorsNotVerifiedError` before the browser opens, listing everything
outstanding at once, so the message is "you have not run the probe yet" and the
answer is one command.
"""

import json
import re
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, final

#: Where ``probe_apply.py`` writes what it saw. One directory per run; the file
#: inside is what :func:`assert_ready_to_apply` reads back.
PROBE_DIR: Final[Path] = Path(__file__).parent / "probe"
PROBE_FILENAME: Final[str] = "probe.json"


class Scope(StrEnum):
    """The session a selector was confirmed under."""

    #: Seen on a public page with no account. Fine for reading, never enough to
    #: apply with: the applicant's page is a different page.
    ANONYMOUS = "anonymous"
    #: Seen while logged in as the owner, which is where applications happen.
    AUTHENTICATED = "authenticated"
    #: Nobody has looked yet. The apply flow refuses to start.
    UNVERIFIED = "unverified"


@final
@dataclass(frozen=True, slots=True)
class Selector:
    """One query, and the record of who saw it work."""

    #: The name used in error messages and in the probe's report.
    name: str
    #: The Playwright query. ``data-qa`` only: hh's class names are hashed CSS
    #: modules (``magritte-button_style-accent___TE21J_7-2-27``) that change
    #: every release, so a class selector is a time bomb with a fuse of days.
    query: str
    scope: Scope
    #: The run directory under :data:`PROBE_DIR` whose report contains
    #: :attr:`query`. ``None`` while unverified.
    evidence: str | None = None
    checked_on: date | None = None
    #: What this control does, in the words of somebody who has to fix it later.
    note: str = ""

    @property
    def usable_for_applying(self) -> bool:
        """Whether this may be relied on to send a real application."""
        return self.scope is Scope.AUTHENTICATED and self.evidence is not None

    def evidence_path(self) -> Path | None:
        """Where the proof should be, if this claims to have any."""
        return None if self.evidence is None else PROBE_DIR / self.evidence / PROBE_FILENAME


# ── what has actually been seen ───────────────────────────────────────

#: Measured on three live vacancy pages on 2026-09-06 with no account: an
#: ``<a role="button">`` whose href is
#: ``/applicant/vacancy_response?vacancyId=…&employerId=…&hhtmFrom=vacancy``.
#: Anonymous scope on purpose — see the module docstring.
APPLY_LINK = Selector(
    name="apply_link",
    query='[data-qa="vacancy-response-link-top"]',
    scope=Scope.ANONYMOUS,
    checked_on=date(2026, 9, 6),
    note="Откликнуться, at the top of a vacancy page. Anonymous only.",
)

#: A decoy, recorded so that nobody rediscovers it as a candidate. Eighteen of
#: these across three pages: the «задать вопрос работодателю» widget, whose
#: names all start the same way as the real control. Never used for anything;
#: it is here to be recognised.
QUESTION_WIDGET_DECOY = Selector(
    name="question_widget_decoy",
    query='[data-qa^="vacancy-response-question"]',
    scope=Scope.ANONYMOUS,
    checked_on=date(2026, 9, 6),
    note="NOT the application form — this asks the employer a question.",
)

# ── what nobody has seen yet ──────────────────────────────────────────
# Each of these is filled in from a probe run, by copying the query AND the run
# directory name out of the report. Until then the apply flow will not start.

SUBMIT_BUTTON = Selector(
    name="submit_button",
    query="",
    scope=Scope.UNVERIFIED,
    note="The control that actually sends the application.",
)

LETTER_FIELD = Selector(
    name="letter_field",
    query="",
    scope=Scope.UNVERIFIED,
    note="The cover letter textarea, wherever the response form puts it.",
)

RESPONSE_FORM = Selector(
    name="response_form",
    query="",
    scope=Scope.UNVERIFIED,
    note="The form or modal the apply link opens; the anchor everything else is found inside.",
)

TEST_QUESTIONS = Selector(
    name="test_questions",
    query="",
    scope=Scope.UNVERIFIED,
    note="Employer test questions, to READ and show a human. Never answered here.",
)

#: Everything the apply flow touches. Read-only paths (opening a vacancy,
#: reading its state) are deliberately not in here: they need no selector at
#: all, because the page's own JSON carries what they read.
REQUIRED_FOR_APPLYING: Final[tuple[Selector, ...]] = (
    RESPONSE_FORM,
    SUBMIT_BUTTON,
    LETTER_FIELD,
)


@final
class SelectorsNotVerifiedError(Exception):
    """The apply flow cannot start because stage 0 has not been done.

    Deliberately not a subclass of any per-vacancy error: nothing in the run
    loop may catch this and carry on to the next vacancy.
    """


#: The ``data-qa`` names inside a Playwright query. ``[data-qa="x"]`` and
#: ``[data-qa^="x"]`` are the only two forms this package writes, and the second
#: one matches by prefix, which is how it is checked below.
_DATA_QA_IN_QUERY: Final[re.Pattern[str]] = re.compile(r'\[data-qa(\^?)="([^"]+)"\]')


def names_in(query: str) -> list[tuple[str, bool]]:
    """The ``data-qa`` names this query depends on, and whether each is a prefix.

    A query naming none of them cannot be checked against a probe report, and
    :func:`assert_ready_to_apply` treats that as a selector that has not been
    verified rather than as one with nothing to verify.
    """
    return [(name, bool(caret)) for caret, name in _DATA_QA_IN_QUERY.findall(query)]


def _recorded_names(report: dict[str, Any]) -> set[str]:
    """Every ``data-qa`` the probe actually saw, from whichever stage wrote it.

    Reads the report as JSON rather than as text. The previous version asked
    whether the query string appeared anywhere in the file, which was wrong in
    both directions at once: ``json.dumps`` escapes the quotes in
    ``[data-qa="…"]`` so a correctly written selector could never match, while
    any substring of the file could — a URL fragment, the JSON key
    ``letterMaxLength``, or the single letter "a". The claim in this module's
    docstring, that promoting a guess takes forging a probe run rather than
    editing one line, was false until this was fixed.
    """
    names: set[str] = set()
    for key in ("data_qa", "data_qa_after_click"):
        section = report.get(key)
        if isinstance(section, dict):
            found = section.get("candidates")
            if isinstance(found, list):
                names.update(str(name) for name in found)
    return names


def _evidence_problems(selector: "Selector") -> list[str]:
    """Everything wrong with one filled-in selector's evidence, in words."""
    path = selector.evidence_path()
    if path is None or not path.is_file():
        return [
            f"  {selector.name}: ссылается на прогон {selector.evidence!r}, "
            "а файла с результатами нет"
        ]
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        return [f"  {selector.name}: {path.name} — не JSON ({error.msg})"]
    if not isinstance(report, dict):
        return [f"  {selector.name}: {path.name} — не отчёт прогона"]

    problems: list[str] = []
    # The scope field is typed by hand; this is the probe's own measurement of
    # whether it was logged in, so that "authenticated" cannot be a claim.
    if report.get("authenticated") is not True:
        problems.append(
            f"  {selector.name}: прогон {selector.evidence!r} сделан без авторизации — "
            "форма отклика видна только под аккаунтом"
        )
    if report.get("stage") != "open-form":
        problems.append(
            f"  {selector.name}: прогон {selector.evidence!r} — этап "
            f"{report.get('stage')!r}, а форму видит только open-form"
        )
    wanted = names_in(selector.query)
    if not wanted:
        return [
            *problems,
            f"  {selector.name}: в {selector.query!r} нет ни одного data-qa — "
            "проверить такой селектор по отчёту нельзя",
        ]
    seen = _recorded_names(report)
    for name, is_prefix in wanted:
        matched = (
            any(candidate.startswith(name) for candidate in seen) if is_prefix else name in seen
        )
        if not matched:
            problems.append(
                f"  {selector.name}: data-qa {name!r} нет среди увиденных в "
                f"{path.name} — селектор не из этого прогона"
            )
    return problems


def assert_ready_to_apply() -> None:
    """Refuse to start the apply flow unless every selector has evidence behind it.

    Four things per selector, because each has been the way a guess got promoted
    somewhere: that it claims an authenticated scope, that the run directory it
    names exists, that the report there was written by a logged-in ``open-form``
    run, and that every ``data-qa`` the query depends on is one the probe
    recorded seeing. The last one is what makes this more than a checkbox.
    """
    problems: list[str] = []
    for selector in REQUIRED_FOR_APPLYING:
        if selector.scope is Scope.UNVERIFIED or not selector.query:
            problems.append(f"  {selector.name}: не заполнен — {selector.note}")
            continue
        if selector.scope is Scope.ANONYMOUS:
            problems.append(
                f"  {selector.name}: проверен только без авторизации; форма отклика "
                "видна лишь под аккаунтом"
            )
            continue
        problems.extend(_evidence_problems(selector))
    if problems:
        raise SelectorsNotVerifiedError(
            "Селекторы формы отклика не проверены на живой странице под аккаунтом.\n"
            + "\n".join(problems)
            + "\n\nСначала: python -m agent.login, затем python -m agent.probe_apply "
            "на четырёх вакансиях (обычная, с обязательным письмом, с тестом, "
            "и с уже отправленным откликом). Потом перенести значения сюда вместе "
            "с именем каталога прогона."
        )
