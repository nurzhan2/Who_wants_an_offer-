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
"""

import ast
import dataclasses
import io
import pickle
from pathlib import Path

import pytest

from agent import gate as gate_module
from agent import selectors
from agent.human import CONFIRM_WORD, CancelledError, Candidate, confirm
from agent.letter import SafeLetter
from agent.mandate import ForgedMandateError, SendMandate, SpentMandateError, mint, verify
from agent.state import Actor, IllegalTransitionError, Status, check

pytestmark = pytest.mark.unit

AGENT_ROOT = Path(__file__).resolve().parent.parent
PRODUCTION_FILES = [path for path in AGENT_ROOT.glob("*.py") if path.name not in {"__init__.py"}]


def a_mandate(vacancy_id: str = "136773120", letter: str | None = "письмо") -> SendMandate:
    """A legitimately minted mandate, as the confirmation would produce."""
    return mint(vacancy_id=vacancy_id, letter=letter, form_digest="digest-of-what-was-shown")


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
        ("letter", "never shown to anybody"),
        ("form_digest", "whatever"),
        ("signature", honest.signature),
        ("confirmed_at", honest.confirmed_at),
    ):
        object.__setattr__(forged, field, value)

    with pytest.raises(ForgedMandateError):
        verify(forged)


def test_a_mandate_cannot_be_stored_and_replayed_tomorrow() -> None:
    """Consent is for one run. A mandate that could be written down could be reused."""
    with pytest.raises(TypeError):
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
    """Not the queue's letter: the one rendered on screen, digest and all."""
    letter = SafeLetter("здравствуйте, меня заинтересовала вакансия")
    candidate = Candidate("1", "A", None, "https://hh.kz/vacancy/1", letter)

    (mandate,) = confirm(
        [candidate], stream_in=io.StringIO(f"\n{CONFIRM_WORD}\n"), stream_out=io.StringIO()
    )

    assert mandate.letter == letter.text
    verify(mandate)  # and it is a real, usable mandate


def test_only_the_confirmation_module_mints_a_mandate() -> None:
    """``mint`` is a capability, and this is the test that keeps it scarce.

    Python has no way to make a function callable from one module only, so the
    boundary is held here: if a second production module learns to mint consent,
    this fails and somebody has to explain why.
    """
    callers = {
        path.name
        for path in PRODUCTION_FILES
        if "mint(" in path.read_text(encoding="utf-8") and path.name != "mandate.py"
    }

    assert callers == {"human.py"}, (
        f"mandate.mint() is called from {sorted(callers)}; consent is minted where a "
        "person answers a prompt and nowhere else"
    )


# ── 1. never solve a captcha ──────────────────────────────────────────


def test_nothing_in_the_package_tries_to_solve_a_captcha() -> None:
    """Detection is allowed; solving, guessing and outsourcing are not.

    A crude scan, and that is the point: the failure mode it guards against is a
    future contributor adding an OCR call or a solving service because the agent
    kept stopping, and a crude scan notices that.
    """
    forbidden = ("solve_captcha", "anticaptcha", "2captcha", "rucaptcha", "captcha_solver")
    offenders = [
        path.name
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
    source = (AGENT_ROOT / "browser.py").read_text(encoding="utf-8")

    assert "headless=False" in source
    tree = ast.parse(source)
    launcher = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "open_browser"
    )
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
    source = (AGENT_ROOT / "browser.py").read_text(encoding="utf-8")

    assert 'service_workers="block"' in source


# ── 5. never touch the password or the session ────────────────────────


def test_nothing_reads_a_cookie_or_a_stored_credential() -> None:
    """The session lives in the browser profile and this program has no reason to read it.

    Parsed rather than grepped, and the difference matters: several modules
    explain in prose that they deliberately do not call ``storage_state()`` or
    ``context.cookies()``, and a text search would flag the promise as if it
    were the violation. This looks at names the code actually uses.
    """
    forbidden = {"storage_state", "cookies", "add_cookies", "getpass", "keyring"}
    offenders: list[str] = []
    for path in PRODUCTION_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            used = (
                node.attr
                if isinstance(node, ast.Attribute)
                else node.id
                if isinstance(node, ast.Name)
                else None
            )
            if used in forbidden:
                offenders.append(f"{path.name}:{used}")

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


def test_the_apply_flow_refuses_to_start_before_the_probe_has_been_run() -> None:
    """And says which selectors are missing, rather than timing out one by one."""
    with pytest.raises(selectors.SelectorsNotVerifiedError) as excinfo:
        selectors.assert_ready_to_apply()

    message = str(excinfo.value)
    for name in ("response_form", "submit_button", "letter_field"):
        assert name in message
    assert "agent.probe_apply" in message


def test_a_selector_measured_without_an_account_is_not_enough_to_apply_with() -> None:
    """The apply link was measured — logged out. The form behind it never was."""
    assert selectors.APPLY_LINK.query
    assert not selectors.APPLY_LINK.usable_for_applying


def test_no_selector_is_written_anywhere_but_the_selectors_module() -> None:
    """Otherwise the evidence rule guards a file nobody has to go through.

    ``probe_apply.py`` is exempt because finding raw ``data-qa`` attributes on a
    page is its entire job.
    """
    offenders = [
        path.name
        for path in PRODUCTION_FILES
        if path.name not in {"selectors.py", "probe_apply.py"}
        and "data-qa" in path.read_text(encoding="utf-8")
    ]

    assert not offenders, f"selectors belong in selectors.py, not in {offenders}"


# ── the gate ──────────────────────────────────────────────────────────


def test_the_gate_matches_the_url_and_not_the_method() -> None:
    """The apply control is an ``<a href>``, so a write-blocking guard misses it.

    Measured on 2026-09-06: following it is a GET document navigation. A guard
    that aborts non-GET requests lets exactly the dangerous one through while
    stopping harmless telemetry.
    """
    apply_url = "https://hh.kz/applicant/vacancy_response?vacancyId=136773120"

    assert gate_module.looks_like_an_application(apply_url)
    assert not gate_module.looks_like_an_application("https://hh.kz/vacancy/136773120")
