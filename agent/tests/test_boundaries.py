"""The six prohibitions, held by tests where they cannot be held by types.

The brief lists things this agent does not do and says they are not settings:
«Это не настройки, отключаемых флагов нет». Most of them are enforced by shape —
a mandate that cannot be forged, a gate that refuses an unauthorised request, a
state machine in which no machine can confirm. This file covers the rest, and
the two kinds are worth telling apart: a boundary held by a type is one nobody
can cross by accident, and a boundary held by a test is one somebody can cross
and will be told about.

Nothing here needs a browser, a network or an account, which is the brief's own
requirement for the test suite and also the reason these run on every commit
rather than when somebody remembers.

**What the source scans model, and what they do not.** Four of the checks below
read the package's own source. They are written against one threat and not
another, and the difference is worth stating so that nobody relies on more than
is here.

They model *a change that looks helpful*: a contributor adds an OCR call because
the agent kept stopping on captchas, a second module learns to mint consent
because minting it in a loop is convenient, a helper reads ``storage_state()``
because passing the session around would save a login. That change is written
plainly, so parsing the syntax finds it — including through the two spellings
that used to walk past: an import alias (``import mint as _consent``) and a
one-step ``getattr`` with the name as a literal (``getattr(context, "cookies")``).

They do **not** model deliberate concealment. A name assembled at runtime
(``getattr(ctx, "cook" + "ies")``), a lookup through ``vars()`` or
``__getattribute__``, a module fetched by ``importlib.import_module`` or an
attribute reached through a dictionary all pass unseen, and no amount of
tightening here would change that — the language has too many ways to say the
same thing. Somebody who wants to cross one of these boundaries can. The
guarantee is that they cannot do it *by accident*, and that crossing it on
purpose has to be written in a way a reader recognises as hiding something.
"""

import ast
import dataclasses
import io
import pickle
from pathlib import Path
from typing import Final

import pytest

from agent import gate as gate_module
from agent import selectors
from agent.human import CONFIRM_WORD, CancelledError, Candidate, confirm
from agent.letter import SafeLetter
from agent.mandate import (
    ForgedMandateError,
    SendMandate,
    SpentMandateError,
    digest,
    mint,
    verify,
)
from agent.state import TERMINAL, TRANSITIONS, Actor, IllegalTransitionError, Status, check

pytestmark = pytest.mark.unit

AGENT_ROOT = Path(__file__).resolve().parent.parent

#: Directories under ``agent/`` that hold Python which is not the program.
#: ``tests`` is the one that matters: every string these scans hunt for appears
#: in this file deliberately — the captcha needles, ``mint``, ``cookies``, a raw
#: ``data-qa`` — because a test that guards a rule has to name what it forbids.
#: Scanning the tests would make each of them fail on its own text, which is why
#: the exclusion exists at all and why it must survive any widening of the walk.
NOT_THE_PROGRAM: Final[frozenset[str]] = frozenset({"tests", "__pycache__"})

#: Directories the agent writes into when it runs: a Chromium profile of several
#: hundred files, probe reports, redacted evidence, screenshots, answer files.
#: They are skipped for speed and because a ``.py`` inside somebody's browser
#: profile is not this program's code.
#:
#: That none of them holds code is checked rather than assumed — see
#: :func:`test_the_skipped_directories_hold_no_code`. Three of these names
#: (``probe``, ``answers``, ``evidence``) would be perfectly ordinary names for
#: a package, and a skip list that quietly swallowed one would be the same
#: silent hole this walk was widened to close.
WRITTEN_AT_RUNTIME: Final[frozenset[str]] = frozenset(
    {"profile", "probe", "screenshots", "answers", "evidence"}
)


def production_modules(root: Path) -> list[Path]:
    """Every module of the program itself, at any depth below ``root``.

    ``root.glob("*.py")`` used to do this and saw only the top level, so a
    module in any subpackage of ``agent/`` was invisible to four scans at once:
    no captcha solving, ``mint`` in ``human.py`` only, no credential access, and
    no selector outside ``selectors.py``. ``agent/`` has no subpackage today,
    which is exactly what made the hole cheap to leave open and silent on the
    day somebody adds one — a scan that finds nothing reports nothing wrong.
    ``test_isolation.py`` already walks this directory with ``rglob``, which is
    what the intent was here too.

    ``__init__.py`` is now included rather than skipped. It was skipped when a
    top-level glob was the whole walk and that one file was empty; parsing an
    empty file costs nothing, and a package's ``__init__`` is an ordinary place
    to re-export something — including something this file forbids.
    """
    skip = NOT_THE_PROGRAM | WRITTEN_AT_RUNTIME
    found: list[Path] = []
    for entry in sorted(root.iterdir()):
        if entry.is_dir():
            if entry.name not in skip and not entry.name.startswith("."):
                found.extend(production_modules(entry))
        elif entry.suffix == ".py":
            found.append(entry)
    return found


PRODUCTION_FILES = production_modules(AGENT_ROOT)


def where(path: Path) -> str:
    """How a module is named in a failure message: its path inside ``agent/``.

    Not ``path.name``. Once the walk reaches subpackages two modules can share a
    basename, and "the violation is in ``run.py``" is an unhelpful thing to be
    told when there are two of them.
    """
    return path.relative_to(AGENT_ROOT).as_posix()


def a_mandate(vacancy_id: str = "136773120", letter: str | None = "письмо") -> SendMandate:
    """A legitimately minted mandate, as the confirmation would produce."""
    return mint(
        vacancy_id=vacancy_id,
        url=f"https://almaty.hh.kz/vacancy/{vacancy_id}",
        letter=letter,
        form_digest="digest-of-what-was-shown",
    )


# ── 3. never send without a human's confirmation ──────────────────────


def test_a_confirmation_cannot_be_retargeted_to_another_vacancy() -> None:
    """The attack that defeated the first design, kept as a test.

    ``dataclasses.replace`` re-runs validation but copies the token from the
    instance it was given, so a design whose token only proved "this was minted"
    let one honest confirmation for vacancy A become a valid mandate for vacancy
    B, carrying a letter nobody had read. Binding the signature to the contents
    is what closes it, and this is the test that says so.
    """
    honest = a_mandate("136773120", "письмо, которое человек прочитал")

    with pytest.raises(ForgedMandateError):
        dataclasses.replace(
            honest, vacancy_id="000000000", letter="письмо, которого человек не видел"
        )


def test_a_mandate_conjured_past_its_constructor_is_refused_at_the_point_of_use() -> None:
    """``__new__`` skips ``__init__``, so validation in a constructor is not enough.

    ``verify`` recomputes the signature from the fields in front of it rather
    than trusting that the object was built properly, which is the only reason
    this fails.
    """
    honest = a_mandate()
    forged = SendMandate.__new__(SendMandate)
    for field, value in (
        ("vacancy_id", "000"),
        ("url", "https://almaty.hh.kz/vacancy/000"),
        ("letter", "never shown to anybody"),
        ("form_digest", "whatever"),
        ("signature", honest.signature),
        ("confirmed_at", honest.confirmed_at),
    ):
        object.__setattr__(forged, field, value)

    with pytest.raises(ForgedMandateError):
        verify(forged)


def test_a_mandate_missing_a_field_is_a_forgery_and_not_an_attribute_error() -> None:
    """The same vector, half-built. Every refusal here must be a MandateError.

    An object past ``__new__`` need not have every field, and reading a missing
    one raises ``AttributeError`` — which no caller catches, so it would escape
    the run loop as a traceback rather than as a refusal.
    """
    half_built = SendMandate.__new__(SendMandate)
    object.__setattr__(half_built, "vacancy_id", "136773120")

    with pytest.raises(ForgedMandateError):
        verify(half_built)


def test_a_mandate_cannot_be_stored_and_replayed_tomorrow() -> None:
    """Consent is for one run. A mandate that could be written down could be reused.

    Matched on the message, not merely on ``TypeError``. A frozen slots
    dataclass of these six fields pickles perfectly well — checked — so today
    the only thing that can raise here is :meth:`SendMandate.__reduce__`. Pin
    that, and a field whose type happens to be unpicklable can never stand in
    for the refusal after somebody deletes it.
    """
    with pytest.raises(TypeError, match="must not cross a process boundary"):
        pickle.dumps(a_mandate())


def test_one_confirmation_authorises_exactly_one_send() -> None:
    """A retry loop is still a second application."""
    mandate = a_mandate()
    verify(mandate)
    with pytest.raises(SpentMandateError):
        verify(mandate)


@pytest.mark.parametrize(
    ("script", "why"),
    [
        ("\n\n", "the human pressed Enter instead of confirming"),
        ("\nда\n", "the human typed something else"),
        ("\ny\n", "a single letter is not the confirmation word"),
        ("", "stdin was closed — a pipe, a cron job, nobody there"),
    ],
)
def test_anything_short_of_the_word_sends_nothing(script: str, why: str) -> None:
    """The default answer is no, and every way of not answering means no."""
    candidates = [Candidate("1", "Backend", "Inspire", "https://hh.kz/vacancy/1", None)]

    with pytest.raises(CancelledError):
        confirm(candidates, stream_in=io.StringIO(script), stream_out=io.StringIO())


def test_dropping_an_item_leaves_no_mandate_for_it() -> None:
    """The brief's «с возможностью выбросить любую», asserted rather than assumed."""
    candidates = [
        Candidate("1", "A", None, "https://hh.kz/vacancy/1", SafeLetter("письмо A")),
        Candidate("2", "B", None, "https://hh.kz/vacancy/2", SafeLetter("письмо B")),
        Candidate("3", "C", None, "https://hh.kz/vacancy/3", None),
    ]

    mandates = confirm(
        candidates,
        stream_in=io.StringIO(f"2\n{CONFIRM_WORD}\n"),
        stream_out=io.StringIO(),
    )

    assert [m.vacancy_id for m in mandates] == ["1", "3"]


def test_the_confirmation_carries_the_letter_the_human_saw() -> None:
    """Not the queue's letter: the one rendered on screen, digest and all.

    "Digest and all" was the half the assertions did not cover. Checking only
    ``mandate.letter`` leaves the binding to the *card* — the thing that makes a
    changed payload invalidate a confirmation — resting on a field nothing read,
    which is how ``TERMINAL`` was a declaration nobody consulted two rounds ago.
    """
    letter = SafeLetter("здравствуйте, меня заинтересовала вакансия")
    candidate = Candidate("1", "A", None, "https://hh.kz/vacancy/1", letter)

    (mandate,) = confirm(
        [candidate], stream_in=io.StringIO(f"\n{CONFIRM_WORD}\n"), stream_out=io.StringIO()
    )

    assert mandate.letter == letter.text
    assert mandate.form_digest == digest(candidate.render()), (
        "the mandate must be bound to the text that was printed, or a payload "
        "that changed under the confirmation would still verify"
    )
    verify(mandate)  # and it is a real, usable mandate


def test_hhs_advice_is_bound_into_the_confirmation_like_everything_else_shown() -> None:
    """Added 2026-09-07, with the change that made this prompt load-bearing.

    hh's «поменяйте видимость резюме…» used to stop every application on its own,
    so nothing could reach this prompt under it and the prompt was not, in
    practice, the last thing between a queue file and a real application. It is
    now. The advice therefore has to be on the card — and being on the card has
    to mean what it means for every other field: the digest covers it, so a
    payload whose warning changed cannot reuse a confirmation given while the old
    one was on screen.

    Both directions are checked, because a field that is rendered but not bound,
    or bound but not rendered, fails in a way nobody sees.
    """
    said = "Чтобы откликнуться на эту вакансию, поменяйте видимость резюме"
    warned = Candidate("1", "A", None, "https://hh.kz/vacancy/1", None, hh_visibility=said)
    quiet = Candidate("1", "A", None, "https://hh.kz/vacancy/1", None)

    assert said in warned.render()
    assert digest(warned.render()) != digest(quiet.render())

    (mandate,) = confirm(
        [warned], stream_in=io.StringIO(f"\n{CONFIRM_WORD}\n"), stream_out=io.StringIO()
    )

    assert mandate.form_digest == digest(warned.render())
    assert mandate.form_digest != digest(quiet.render())


def test_the_batch_is_told_once_what_hh_said_about_the_resume() -> None:
    """One statement about twelve applications, said once where it is read.

    The per-card heading cannot make this point: hh's sentence names a vacancy
    («Чтобы откликнуться на эту вакансию…»), so twelve cards carrying it read as
    twelve separate remarks about twelve jobs rather than one remark about the
    resume they all share. The summary sits between the cards and the drop
    prompt, which is the last thing read before the answer is typed.

    It adds nothing to what the mandate binds, and that is asserted here rather
    than assumed: every word of it is either fixed text or a line already inside
    a card, so consent still covers exactly what was shown.
    """
    said = "Чтобы откликнуться на эту вакансию, поменяйте видимость резюме"
    candidates = [
        Candidate("1", "A", None, "https://hh.kz/vacancy/1", None, hh_visibility=said),
        Candidate("2", "B", None, "https://hh.kz/vacancy/2", None),
    ]
    shown = io.StringIO()

    mandates = confirm(candidates, stream_in=io.StringIO(f"\n{CONFIRM_WORD}\n"), stream_out=shown)

    printed = shown.getvalue()
    assert "ВНИМАНИЕ" in printed
    assert said in printed
    assert "1 из 2" in printed, "the batch is told how much of it hh has spoken about"
    assert "для всех остальных" in printed, "and that it is true of the rest too"
    assert [m.form_digest for m in mandates] == [digest(c.render()) for c in candidates]


def test_a_quiet_batch_says_nothing_about_visibility() -> None:
    """A banner nobody earned is a banner everybody learns to skip."""
    shown = io.StringIO()

    confirm(
        [Candidate("1", "A", None, "https://hh.kz/vacancy/1", None)],
        stream_in=io.StringIO(f"\n{CONFIRM_WORD}\n"),
        stream_out=shown,
    )

    assert "ВНИМАНИЕ" not in shown.getvalue()


def test_only_the_confirmation_module_mints_a_mandate() -> None:
    """``mint`` is a capability, and this is the test that keeps it scarce.

    Python has no way to make a function callable from one module only, so the
    boundary is held here: if a second production module learns to mint consent,
    this fails and somebody has to explain why.
    """
    callers = {
        where(path)
        for path in PRODUCTION_FILES
        # Relative to the package, not by basename: the walk is recursive now,
        # so `agent/anything/mandate.py` would have exempted itself.
        if path.relative_to(AGENT_ROOT) != Path("mandate.py") and _imports_mint(path)
    }

    assert callers == {"human.py"}, (
        f"mandate.mint() is called from {sorted(callers)}; consent is minted where a "
        "person answers a prompt and nowhere else"
    )


def function_named(source: str, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    """The one function with this name, or a failure that says what happened.

    ``next(node for node in ...)`` used to be written out at each of the three
    call sites. It raised ``StopIteration`` when the function was not found —
    which pytest reports as an error with no explanation — and it matched
    ``ast.FunctionDef`` only, so turning ``open_browser`` into a coroutine would
    have taken the launch arguments out from under the two checks that guard
    the visible window without either of them saying so.
    """
    matches = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name
    ]
    assert len(matches) == 1, f"expected exactly one {name}(), found {len(matches)}"
    return matches[0]


def _keyword_values(function: ast.FunctionDef | ast.AsyncFunctionDef, name: str) -> list[object]:
    """Every literal passed as ``name=`` by a call inside this function.

    Reading the argument rather than searching the file is the whole point:
    a source-text assertion is satisfied by the same characters in a comment,
    and the two arguments checked with it are the ones every consent guarantee
    in this package rests on.
    """
    found: list[object] = []
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg == name and isinstance(keyword.value, ast.Constant):
                found.append(keyword.value.value)
    return found


def attribute_named_by_getattr(node: ast.AST) -> str | None:
    """The attribute name in ``getattr(x, "cookies")``, when that is what this node is.

    The one indirection the scans in this file model, and the reason it is
    modelled: ``getattr`` with a literal name is not concealment, it is the
    ordinary way to reach an attribute whose owner is awkwardly typed — the
    package already does it once, in ``gate.post_body``, for exactly that
    reason. Somebody adding ``getattr(context, "storage_state")()`` is far more
    likely to be taking the path of least resistance than to be hiding, and
    before this the scans below saw nothing at all: an ``ast.Call`` is neither
    an ``ast.Attribute`` nor an ``ast.Name``.

    A name that is not a literal — built by concatenation, read from a config,
    passed in as an argument — is not modelled, and the module docstring says
    why that line is where it is.
    """
    if not isinstance(node, ast.Call) or len(node.args) < 2:
        return None
    func = node.func
    called = (
        func.id
        if isinstance(func, ast.Name)
        else func.attr
        if isinstance(func, ast.Attribute)
        else None
    )
    if called != "getattr":
        return None
    wanted = node.args[1]
    if isinstance(wanted, ast.Constant) and isinstance(wanted.value, str):
        return wanted.value
    return None


def names_reached_in(tree: ast.AST, forbidden: frozenset[str]) -> list[str]:
    """Every forbidden name this module reaches for, however it spells the reach.

    Three spellings: ``context.cookies``, a bare ``cookies``, and
    ``getattr(context, "cookies")``. Parsed rather than grepped, because several
    modules explain in prose that they deliberately do **not** call
    ``storage_state()``, and a text search would flag the promise as the
    violation.
    """
    found: list[str] = []
    for node in ast.walk(tree):
        by_getattr = attribute_named_by_getattr(node)
        used = (
            node.attr
            if isinstance(node, ast.Attribute)
            else node.id
            if isinstance(node, ast.Name)
            else by_getattr
        )
        if used in forbidden:
            found.append(str(used))
    return found


def _imports_mint(path: Path) -> bool:
    """Whether this module can reach ``mint``, under any name and by any spelling.

    ``from agent.mandate import mint as _consent`` walks past a search for
    "mint(" — and a module that can mint consent can mint it for a whole batch
    with nobody asked. Parsing catches the alias; the substring did not.

    ``getattr(mandate, "mint")`` used to walk past the parsing too, for the same
    reason the credential scan missed ``getattr(context, "cookies")``: the name
    lives in a string argument, so there is no ``ast.Attribute`` to find.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and (node.module or "").endswith("mandate")
            and any(alias.name == "mint" for alias in node.names)
        ):
            return True
        if isinstance(node, ast.Attribute) and node.attr == "mint":
            return True
        if attribute_named_by_getattr(node) == "mint":
            return True
    return False


# ── 1. never solve a captcha ──────────────────────────────────────────


def test_nothing_in_the_package_tries_to_solve_a_captcha() -> None:
    """Detection is allowed; solving, guessing and outsourcing are not.

    A crude scan, and that is the point: the failure mode it guards against is a
    future contributor adding an OCR call or a solving service because the agent
    kept stopping, and a crude scan notices that.
    """
    forbidden = ("solve_captcha", "anticaptcha", "2captcha", "rucaptcha", "captcha_solver")
    offenders = [
        where(path)
        for path in PRODUCTION_FILES
        for needle in forbidden
        if needle in path.read_text(encoding="utf-8").casefold()
    ]

    assert not offenders, f"captcha solving must never appear here: {offenders}"


# ── 4. never headless, never hidden ───────────────────────────────────


def test_the_browser_is_launched_visible_and_headless_is_not_a_parameter() -> None:
    """A boolean argument is a boolean somebody passes.

    This is a speed bump rather than a wall — a virtual display defeats it — and
    the README says so. What it does buy is that making this headless is an edit
    to a file with the reason next to it, not a flag in a command line.
    """
    launcher = function_named(
        (AGENT_ROOT / "browser.py").read_text(encoding="utf-8"), "open_browser"
    )

    # The value actually passed, not the presence of the characters
    # "headless=False" somewhere in the file. Flipping the call site and leaving
    # the literal in a comment — or quoting it in the module docstring — used to
    # pass this test with a headless browser behind it.
    passed = _keyword_values(launcher, "headless")
    assert passed == [False], f"open_browser passes headless={passed}, and it must pass False"

    names = {arg.arg for arg in launcher.args.args + launcher.args.kwonlyargs}
    assert "headless" not in names


def test_service_workers_are_blocked_so_the_gate_can_see_everything() -> None:
    """The one launch argument the request interception depends on.

    ``context.route`` does not observe requests issued from a service worker. hh
    is a single-page application and may register one, and if it did, an
    application could leave without the gate ever being asked. Without this line
    every consent guarantee in the package is conditional on a fact nobody
    checked.
    """
    launcher = function_named(
        (AGENT_ROOT / "browser.py").read_text(encoding="utf-8"), "open_browser"
    )

    passed = _keyword_values(launcher, "service_workers")
    assert passed == ["block"], f"open_browser passes service_workers={passed}"


# ── 5. never touch the password or the session ────────────────────────


#: Names that read the session or the password. The agent has no reason to
#: touch any of them: the owner logs in by hand and the session lives in a
#: Chromium profile directory this program never opens.
CREDENTIAL_NAMES: Final[frozenset[str]] = frozenset(
    {"storage_state", "cookies", "add_cookies", "getpass", "keyring"}
)


def test_nothing_reads_a_cookie_or_a_stored_credential() -> None:
    """The session lives in the browser profile and this program has no reason to read it.

    Parsed rather than grepped, and the difference matters: several modules
    explain in prose that they deliberately do not call ``storage_state()`` or
    ``context.cookies()``, and a text search would flag the promise as if it
    were the violation. This looks at names the code actually reaches for — see
    :func:`names_reached_in` for which spellings of "reaches for" are modelled.
    """
    offenders = [
        f"{where(path)}:{name}"
        for path in PRODUCTION_FILES
        for name in names_reached_in(ast.parse(path.read_text(encoding="utf-8")), CREDENTIAL_NAMES)
    ]

    assert not offenders, f"the agent must not handle credentials: {offenders}"


# ── the state machine's own boundary ──────────────────────────────────


def test_a_machine_cannot_confirm_and_nothing_can_unsend() -> None:
    """Two rules that would be comments in most designs."""
    check(Status.QUEUED, Status.CONFIRMED, actor=Actor.HUMAN)

    with pytest.raises(IllegalTransitionError):
        check(Status.QUEUED, Status.CONFIRMED, actor=Actor.AGENT)
    with pytest.raises(IllegalTransitionError):
        check(Status.SENT, Status.QUEUED, actor=Actor.HUMAN)
    with pytest.raises(IllegalTransitionError):
        check(Status.FAILED, Status.QUEUED, actor=Actor.AGENT)


# ── stage 0 ───────────────────────────────────────────────────────────


def test_the_measured_half_of_the_apply_flow_may_start() -> None:
    """An application with no cover letter was measured end to end on 2026-09-06.

    Every selector it needs carries a redacted evidence file in
    ``agent/evidence/``, so this passes here and in CI rather than only on the
    machine that holds the gitignored probe reports — which was the point of
    splitting the evidence out of them.
    """
    selectors.assert_ready_to_apply()

    for selector in selectors.REQUIRED_FOR_APPLYING:
        assert selector.usable_for_applying, selector.name


def test_the_letter_field_is_measured_and_backed_by_a_committed_record() -> None:
    """Measured 2026-09-16: the textarea behind «Добавить сопроводительное».

    Passes here and in CI because its evidence is the redacted file in
    ``agent/evidence/``, not the gitignored probe report.
    """
    selectors.assert_letter_field_known()

    assert selectors.letter_field_is_known()
    assert selectors.LETTER_FIELD.query == ('[data-qa="vacancy-response-popup-form-letter-input"]')
    assert selectors.LETTER_FIELD.evidence == "20260916-125039"


def test_a_letter_field_whose_evidence_is_gone_refuses_and_names_the_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Losing the measurement must route letters to a person, not guess.

    The refusal names the probe step that measures the field again.
    """
    unmeasured = dataclasses.replace(
        selectors.LETTER_FIELD, query="", scope=selectors.Scope.UNVERIFIED, evidence=None
    )
    monkeypatch.setattr(
        selectors, "REQUIRED_FOR_A_LETTER", (selectors.ADD_COVER_LETTER, unmeasured)
    )

    assert not selectors.letter_field_is_known()
    with pytest.raises(selectors.LetterFieldUnknownError) as excinfo:
        selectors.assert_letter_field_known()

    message = str(excinfo.value)
    assert "letter_field" in message
    assert "agent.probe_apply" in message
    assert "open-form" in message


def test_a_selector_measured_without_an_account_is_not_enough_to_apply_with() -> None:
    """The applicant's page is a different page, so an anonymous sighting is not proof.

    The scope is the *only* thing wrong with the selector below, and that took a
    correction. The first version built one from scratch and left ``evidence``
    at its default of ``None``, so it was refused for having no evidence at all:
    the assertion held, its name did not, and deleting the scope check would not
    have made it fail. This starts from a selector the package already accepts
    and changes nothing but the scope, so an anonymous sighting being
    insufficient is the one thing that can be under test.
    """
    anonymous = dataclasses.replace(
        selectors.APPLY_LINK, name="seen_logged_out", scope=selectors.Scope.ANONYMOUS
    )

    assert selectors.APPLY_LINK.usable_for_applying
    assert not selectors.problems_with((selectors.APPLY_LINK,))
    assert anonymous.evidence is not None, "the evidence must not be what fails here"
    assert not anonymous.usable_for_applying
    assert selectors.problems_with((anonymous,))


def selector_literals_in(tree: ast.AST) -> list[str]:
    """Every ``data-qa`` string this module could actually hand to a browser.

    A query and an explanation of a query are the same characters, and only one
    of them is a violation. This returns the string constants that are *used* —
    an argument, an assignment, a piece of an f-string — and skips the ones that
    are only said: docstrings and bare string expressions, which are
    ``ast.Expr`` nodes and cannot reach Playwright. Comments never enter the
    tree at all.

    The distinction is not hypothetical. On 2026-09-07 ``agent/submit.py``'s
    module docstring explained a bug that had just been fixed — that the module
    used to wait for the response overlay and classify it before the card
    arrived — and quoted the overlay's ``[data-qa="modal-overlay"]`` query to
    say which element it meant. The text search that used to stand here read
    that as a selector written outside ``selectors.py`` and failed. It is the
    opposite: it is the record of why the module now waits on something else,
    and a rule that fails on accurate documentation is satisfied by deleting the
    documentation. (That paragraph has since been rewritten, which is the
    outcome this check should not be able to encourage.) The credential scan
    below was parsed for exactly this reason; this one was not.
    """
    said = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and "data-qa" in node.value
        and node not in said
    ]


def test_no_selector_is_written_anywhere_but_the_selectors_module() -> None:
    """Otherwise the evidence rule guards a file nobody has to go through.

    ``probe_apply.py`` is exempt because finding raw ``data-qa`` attributes on a
    page is its entire job.
    """
    offenders = [
        f"{where(path)}: {query}"
        for path in PRODUCTION_FILES
        # Relative to the package, for the reason given above the mint scan.
        if path.relative_to(AGENT_ROOT) not in {Path("selectors.py"), Path("probe_apply.py")}
        for query in selector_literals_in(ast.parse(path.read_text(encoding="utf-8")))
    ]

    assert not offenders, f"selectors belong in selectors.py, not in {offenders}"


def test_the_selector_check_tells_a_query_from_an_explanation(tmp_path: Path) -> None:
    """Both halves, because each failure mode costs something different.

    A scan that misses the query lets a module click something no evidence file
    has ever seen. A scan that flags the explanation — which the text search did,
    on ``submit.py``, on 2026-09-07 — teaches people to stop writing down why
    the code does what it does, and the docstrings in this package are the only
    record of what was measured.
    """
    used = tmp_path / "clicker.py"
    used.write_text(
        "def apply(page, name):\n"
        "    page.click('[data-qa=\"vacancy-response-submit-popup\"]')\n"
        "    page.wait_for_selector(f'[data-qa=\"{name}\"]')\n",
        encoding="utf-8",
    )
    explained = tmp_path / "documented.py"
    explained.write_text(
        '"""This used to wait for [data-qa="modal-overlay"], which was too early."""\n'
        "\n"
        "\n"
        "def apply(page):\n"
        '    """Waits for selectors.SUBMIT_BUTTON, not for [data-qa="modal-overlay"]."""\n'
        '    # the overlay is [data-qa="modal-overlay"] and it attaches too soon\n'
        "    page.click(SUBMIT_BUTTON.query)\n",
        encoding="utf-8",
    )

    def found(path: Path) -> list[str]:
        return selector_literals_in(ast.parse(path.read_text(encoding="utf-8")))

    assert len(found(used)) == 2, "a query in a call and one built into an f-string"
    assert not found(explained)


# ── the gate ──────────────────────────────────────────────────────────


@dataclasses.dataclass
class _RecordingRoute:
    """One playwright route, answering the little of its protocol the gate uses."""

    url: str
    action: str | None = None

    @property
    def request(self) -> "_RecordingRoute":
        """The route is its own request here; only ``url`` and the body are read."""
        return self

    @property
    def method(self) -> str:
        """Recorded by the gate, never used by it to decide."""
        return "GET"

    @property
    def post_data(self) -> str | None:
        """No body: the measured apply control is a link."""
        return None

    def abort(self, error_code: str = "failed") -> None:
        """Refused."""
        self.action = "abort"

    def continue_(self) -> None:
        """Allowed through."""
        self.action = "continue"


def test_the_gate_matches_the_url_and_not_the_method() -> None:
    """The apply control is an ``<a href>``, so a write-blocking guard misses it.

    Measured on 2026-09-06: following it is a GET document navigation. A guard
    that aborts non-GET requests lets exactly the dangerous one through while
    stopping harmless telemetry.
    """
    apply_url = "https://hh.kz/applicant/vacancy_response?vacancyId=136773120"

    assert gate_module.looks_like_an_application(apply_url)
    assert not gate_module.looks_like_an_application("https://hh.kz/vacancy/136773120")


@pytest.mark.parametrize(
    "url",
    [
        "https://hh.kz/applicant/vacancy_response?vacancyId=136773120",
        "https://almaty.hh.kz/applicant/vacancy_response/popup?vacancyId=136773120",
    ],
)
def test_the_exemption_list_cannot_become_a_hole_in_consent(
    url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The window's own question must not be reachable from the refusal decision.

    ``is_the_application_itself`` decides only what the armed window counts. It
    is the predicate a later contributor narrows — one more beacon, one more
    widget — and if a refusal ever consulted it, narrowing it would widen the
    set of requests that can leave the browser with nobody's consent behind
    them.

    Asserted by making the narrow predicate answer "not an application" for
    *everything* and requiring an unarmed gate to abort anyway. It is the same
    trick as the headless check below: not "the source says so" but "the
    behaviour survives the thing that would break it".
    """
    monkeypatch.setattr(gate_module, "is_the_application_itself", lambda _: False)
    gate = gate_module.SubmitGate()
    route = _RecordingRoute(url)

    gate.handle(route)

    assert route.action == "abort"
    assert gate.blocked == [url]


def test_the_gate_records_a_submit_click_and_never_judges_it() -> None:
    """The second job this gate was quietly given, and no longer has.

    ``require_progress`` refused after the irreversible click when it had seen no
    application-shaped request — a rule that needs to know what hh's «Откликнуться»
    emits, which nobody has recorded. A successful application would be reported
    ``failed`` and offered to the owner again, and applying twice is the mistake
    that cannot be undone. Whether an application exists is hh's answer, read off
    a re-opened page in ``submit.py``.

    So: no method on the gate may raise about a send, and the record it keeps is
    a record. Both halves are asserted, the second over an empty click, because
    an empty click is exactly the case the old rule got wrong.
    """
    mandate = a_mandate()
    gate = gate_module.SubmitGate()

    assert not hasattr(gate, "require_progress")
    assert not hasattr(gate_module, "UnmandatedRequestError")

    with gate.armed(mandate):
        click = gate.note_submit_click(mandate, since=gate.mark())

    assert click.allowed == ()
    assert click.refused == ()
    assert dataclasses.is_dataclass(click)


def test_no_move_out_of_a_terminal_state_is_expressible() -> None:
    """``TERMINAL`` used to be a declaration nothing read.

    A single line added to ``TRANSITIONS`` — ``(SENT, QUEUED): AGENT`` — made an
    application retryable and left the whole suite green. ``check`` now consults
    the set before the table, so the table cannot disagree with it.
    """
    for source in TERMINAL:
        for target in Status:
            for actor in Actor:
                with pytest.raises(IllegalTransitionError):
                    check(source, target, actor=actor)

    # And the table itself says the same thing, so the two cannot drift apart.
    assert not [pair for pair in TRANSITIONS if pair[0] in TERMINAL]


def test_the_headless_check_is_not_satisfied_by_a_comment(tmp_path: Path) -> None:
    """The test that guards the visible window, tested against the way past it.

    A source-text assertion is satisfied by the characters appearing anywhere —
    including in a comment beside a call that now launches headless. This drives
    the real helper over a module shaped exactly like that.
    """
    disguised = tmp_path / "browser.py"
    disguised.write_text(
        "def open_browser():\n"
        '    # headless=False, service_workers="block"\n'
        '    return launch(headless=True, service_workers="allow")\n',
        encoding="utf-8",
    )
    launcher = function_named(disguised.read_text(encoding="utf-8"), "open_browser")

    assert _keyword_values(launcher, "headless") == [True]
    assert _keyword_values(launcher, "service_workers") == ["allow"]


def test_the_mint_check_is_not_satisfied_by_an_alias(tmp_path: Path) -> None:
    """``from agent.mandate import mint as _consent`` walked past the substring.

    A module that can mint consent can mint it for a whole batch with nobody
    asked, which is the one thing this package exists to prevent.
    """
    aliased = tmp_path / "sneaky.py"
    aliased.write_text("from agent.mandate import mint as _consent\n", encoding="utf-8")
    innocent = tmp_path / "plain.py"
    innocent.write_text("from agent.state import Status\n", encoding="utf-8")

    assert _imports_mint(aliased)
    assert not _imports_mint(innocent)


def test_the_mint_check_is_not_satisfied_by_getattr(tmp_path: Path) -> None:
    """The same boundary, walked past by the indirection instead of by the alias.

    Verified against the code as it stood: this exact module answered ``False``,
    because the name it reaches for lives in a string argument and the scan only
    looked at ``ast.ImportFrom`` and ``ast.Attribute``. It could then mint one
    mandate per queue item with nobody at the keyboard.
    """
    indirect = tmp_path / "sneaky.py"
    indirect.write_text(
        "from agent import mandate\n"
        "\n"
        "\n"
        "def consent_for_everything(items):\n"
        '    make = getattr(mandate, "mint")\n'
        "    return [make(vacancy_id=i, url=i, letter=None, form_digest='d') for i in items]\n",
        encoding="utf-8",
    )
    innocent = tmp_path / "plain.py"
    innocent.write_text('x = getattr(request, "post_data", None)\n', encoding="utf-8")

    assert _imports_mint(indirect)
    assert not _imports_mint(innocent)


def test_the_credential_check_is_not_satisfied_by_getattr(tmp_path: Path) -> None:
    """The session is the one thing this program must not be able to carry off.

    Verified against the code as it stood: the scan matched ``ast.Attribute``
    and ``ast.Name`` only, so this module reached both the cookie jar and the
    storage state and was reported clean. What is on the other side of that hole
    is not a style point — it is the owner's live hh session, in a file, in a
    repository with a remote.
    """
    indirect = tmp_path / "sneaky.py"
    indirect.write_text(
        "def take(context):\n"
        '    jar = getattr(context, "cookies")()\n'
        '    state = getattr(context, "storage_state")()\n'
        "    return jar, state\n",
        encoding="utf-8",
    )
    spelled_out = tmp_path / "plain.py"
    spelled_out.write_text("def take(context):\n    return context.cookies()\n", encoding="utf-8")
    innocent = tmp_path / "honest.py"
    innocent.write_text(
        '"""This module never calls storage_state() or context.cookies()."""\n'
        'data = getattr(request, "post_data", None)\n',
        encoding="utf-8",
    )

    def reached(path: Path) -> list[str]:
        return names_reached_in(ast.parse(path.read_text(encoding="utf-8")), CREDENTIAL_NAMES)

    assert sorted(reached(indirect)) == ["cookies", "storage_state"]
    assert reached(spelled_out) == ["cookies"]
    # Prose about not doing it is not doing it. This is why the scan parses.
    assert not reached(innocent)


def test_the_scans_reach_a_module_in_a_subpackage(tmp_path: Path) -> None:
    """The walk that used to stop at the top level, driven over a tree that has depth.

    Verified against the code as it stood: with ``glob("*.py")`` the module
    below was collected by nothing, so the captcha scan, the ``mint`` scan, the
    credential scan and the selector scan all reported a package that was clean
    and unexamined. The assertion on the excluded directories is the other half
    — a widened walk that swallowed ``tests/`` would make every scan in this
    file fail on its own text, and the temptation would be to narrow the wrong
    one back down.
    """
    (tmp_path / "sub").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "profile" / "Default").mkdir(parents=True)
    (tmp_path / "__pycache__").mkdir()
    for relative in (
        "__init__.py",
        "top.py",
        "sub/__init__.py",
        "sub/deep.py",
        "tests/test_boundaries.py",
        "profile/Default/whatever.py",
        "__pycache__/top.py",
        "notes.md",
    ):
        (tmp_path / relative).write_text("", encoding="utf-8")

    collected = {path.relative_to(tmp_path).as_posix() for path in production_modules(tmp_path)}

    assert collected == {"__init__.py", "top.py", "sub/__init__.py", "sub/deep.py"}
    assert "sub/deep.py" not in {path.name for path in tmp_path.glob("*.py")}


def test_the_scans_cover_the_program_and_leave_the_tests_alone() -> None:
    """A scan that collects nothing passes everything, so check what it collected.

    The failure mode of the fix above is quieter than the bug it fixes: one name
    too many in :data:`NOT_THE_PROGRAM` and every check in this file goes green
    over an empty list.
    """
    collected = {where(path) for path in PRODUCTION_FILES}

    assert {"mandate.py", "human.py", "gate.py", "browser.py", "selectors.py"} <= collected
    assert "__init__.py" in collected, "a package's __init__ can re-export anything"
    assert not [name for name in collected if name.startswith("tests/")]


def test_the_skipped_directories_hold_no_code() -> None:
    """The skip list is allowed to skip data. It is not allowed to skip a package.

    ``probe``, ``answers`` and ``evidence`` are names a package could plausibly
    take — ``probe_apply.py`` is right there — and the day one of them became a
    directory of modules, every scan in this file would go quietly around it.
    So the claim "these hold no source" is checked against the disk rather than
    written down once and trusted.
    """
    holding_code = [
        f"{name}/{path.relative_to(AGENT_ROOT / name).as_posix()}"
        for name in sorted(WRITTEN_AT_RUNTIME)
        if (AGENT_ROOT / name).is_dir()
        for path in (AGENT_ROOT / name).rglob("*.py")
    ]

    assert not holding_code, (
        f"code appeared in a directory the boundary scans skip: {holding_code}. "
        "Either move it, or take that name out of WRITTEN_AT_RUNTIME — a skipped "
        "package is an unexamined one."
    )
