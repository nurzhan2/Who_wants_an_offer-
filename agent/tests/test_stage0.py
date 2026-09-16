"""Stage 0: the host, the redirect, the authentication signal, and the evidence.

Four things that all failed in the same way — quietly, on the owner's machine,
in a place no test looked.

*The host was written into the code.* ``login.py`` carried a city subdomain as a
stopgap, which CLAUDE.md forbids by name, and the cities were already described
in a data file next door.

*The redirect after sign-in killed the script.* hh sets a regional cookie and
navigates away; Playwright reports the interrupted navigation as an error and
``page.goto`` raised. Nothing caught it, so ``python -m agent.login`` could not
finish on the one machine it exists for.

*Sign-in was detected as a difference between two page reads*, which is empty on
a profile that is already signed in — that is, on every run after the first.

*And the evidence for every selector lived in a gitignored directory*, so the
check that selectors have been verified could only ever pass on one laptop.

Nothing here needs a browser, a network or an account. The pages are fakes that
answer the four questions the real code asks them.
"""

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from agent import hosts, login, probe_apply, selectors, session

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
VACANCY = "136773120"


# ── the host comes from data ──────────────────────────────────────────


def test_the_host_comes_from_the_data_file_and_not_from_this_package() -> None:
    """The city is a property of the deployment, not of the code."""
    sites = hosts.load_sites()

    assert sites, "backend/app/sources/hh_sites.yaml should be readable from here"
    assert hosts.default_host() in {site.host for site in sites}
    assert any(site.is_default for site in sites)


def test_the_default_marker_decides_which_host_is_opened(tmp_path: Path) -> None:
    """Moving the deployment to another city is an edit to the data file."""
    path = tmp_path / "sites.yaml"
    path.write_text(
        "sites:\n"
        "  - host: almaty.hh.kz\n    city: Алматы\n"
        "  - host: astana.hh.kz\n    city: Астана\n    default: true\n",
        encoding="utf-8",
    )

    assert hosts.default_host(path) == "astana.hh.kz"
    assert hosts.known_hosts(path) == frozenset({"almaty.hh.kz", "astana.hh.kz"})


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not: a mapping of sites",
        "sites: 5",
        "sites:\n  - notahost: x\n",
        "sites: [\n",
    ],
)
def test_an_unreadable_sites_file_degrades_instead_of_crashing(tmp_path: Path, text: str) -> None:
    """The agent runs on a laptop that may hold nothing but this package.

    Every failure here has to end in a working host rather than an exception:
    refusing to open a browser because a file in a sibling package moved would
    be failing at the wrong thing.
    """
    path = tmp_path / "sites.yaml"
    path.write_text(text, encoding="utf-8")

    assert hosts.load_sites(path) == ()
    assert hosts.default_host(path) == hosts.FALLBACK_HOST


def test_a_missing_sites_file_falls_back_to_the_country_root(tmp_path: Path) -> None:
    """And the fallback names no city, because hh redirects the root by itself."""
    assert hosts.default_host(tmp_path / "nothing-here.yaml") == hosts.FALLBACK_HOST
    assert hosts.FALLBACK_HOST == "hh.kz"


# ── the redirect ──────────────────────────────────────────────────────


class _Page:
    """A page that can be told to fail its navigation the way hh's redirect does."""

    def __init__(self, *, lands_on: str, raises: str | None = None) -> None:
        self.url = "about:blank"
        self.lands_on = lands_on
        self.raises = raises
        self.waited_for_load = False

    def goto(self, url: str, wait_until: str = "load", timeout: int = 0) -> None:
        """Either lands, or raises the way an interrupted navigation does."""
        self.url = self.lands_on
        if self.raises is not None:
            raise RuntimeError(self.raises)

    def wait_for_load_state(self, state: str, timeout: int = 0) -> None:
        """What the helper does after absorbing the interrupted navigation."""
        self.waited_for_load = True


def test_the_regional_redirect_after_sign_in_is_survived() -> None:
    """The bug that stopped ``python -m agent.login`` from ever finishing.

    hh sets a regional cookie and navigates away; Playwright reports the
    interrupted navigation as ``net::ERR_ABORTED`` out of ``page.goto``.
    """
    page = _Page(
        lands_on=f"https://almaty.hh.kz/vacancy/{VACANCY}",
        raises="Error: page.goto: net::ERR_ABORTED at https://hh.kz/vacancy/x",
    )

    landed = hosts.open_hh_page(page, f"https://hh.kz/vacancy/{VACANCY}", expect_vacancy=VACANCY)

    assert landed == f"https://almaty.hh.kz/vacancy/{VACANCY}"
    assert page.waited_for_load, "the helper has to wait for the navigation that won"


def test_a_navigation_that_lands_on_another_vacancy_is_a_failure() -> None:
    """A redirect somewhere else is a different failure, not a quiet success."""
    page = _Page(lands_on="https://almaty.hh.kz/vacancy/999999999")

    with pytest.raises(hosts.NavigatedElsewhereError):
        hosts.open_hh_page(page, f"https://hh.kz/vacancy/{VACANCY}", expect_vacancy=VACANCY)


def test_a_sign_in_wall_is_not_mistaken_for_the_vacancy_it_names() -> None:
    """The reason the id is read from the path only.

    hh's login page carries the address it interrupted in ``?backurl=``, so a
    search over the whole URL finds the id that was asked for and reports a page
    that never loaded as the page that did.

    Corrected 2026-09-07: the fixture used to percent-encode that backurl, as
    ``?backurl=%2Fvacancy%2F<id>``, which no reading matches — not the path-only
    one and not the whole-URL one either. The test therefore passed whichever
    way ``hosts`` was written and asserted nothing about the choice it exists to
    defend. The unencoded form is the one that tells the two readings apart, and
    the wrong reading is spelled out below rather than left to the imagination:
    ``agent/state_page.py`` really does search whole URLs with this pattern, on
    addresses this package built itself.
    """
    wall = f"https://almaty.hh.kz/account/login?backurl=/vacancy/{VACANCY}"

    over_the_whole_url = re.search(r"/vacancy/(\d+)", wall)
    assert over_the_whole_url is not None
    assert over_the_whole_url.group(1) == VACANCY, "the wrong reading finds the id it wanted"

    page = _Page(lands_on=wall)
    with pytest.raises(hosts.NavigatedElsewhereError):
        hosts.open_hh_page(page, f"https://hh.kz/vacancy/{VACANCY}", expect_vacancy=VACANCY)

    assert hosts.vacancy_id_in_path(wall) is None
    assert hosts.vacancy_id_in_path(f"https://almaty.hh.kz/vacancy/{VACANCY}?hhtmFrom=x") == VACANCY


def test_a_navigation_error_that_is_not_the_redirect_is_raised() -> None:
    """Absorbing everything would turn a dead network into an empty page."""
    page = _Page(lands_on="about:blank", raises="Error: page.goto: net::ERR_NAME_NOT_RESOLVED")

    with pytest.raises(RuntimeError, match="ERR_NAME_NOT_RESOLVED"):
        hosts.open_hh_page(page, f"https://hh.kz/vacancy/{VACANCY}")


def test_a_lookalike_host_is_not_the_same_site() -> None:
    """Two labels, so the regional redirect passes and a phishing host does not."""
    assert hosts.same_site("https://almaty.hh.kz/x", "https://hh.kz/y")
    assert not hosts.same_site("https://hh.kz.example.com/x", "https://hh.kz/y")
    assert not hosts.same_site("https://hh.ru/x", "https://hh.kz/y")


def test_a_url_that_is_not_hh_is_refused_before_the_browser_moves() -> None:
    """The check ``agent/submit.py`` advertised and this helper did not make.

    It compared the landing host against the REQUESTED host, and any address
    that does not redirect satisfies that by construction. The requested address
    is not a constant — it comes out of a queue file — so a queue naming
    ``https://hh.kz.example.com/vacancy/<id>`` produced a host that matches
    itself, a path carrying the right id, and a page whose content was then read
    as the vacancy and clicked on.

    Refused before ``goto`` rather than after it: a page whose content must not
    be read is a page that must not be opened, and by the time it has loaded the
    request has already been made from the owner's own browser and session.
    """
    lookalike = f"https://hh.kz.example.com/vacancy/{VACANCY}"
    page = _Page(lands_on=lookalike)

    assert hosts.same_site(lookalike, lookalike), "the weak check passes it"
    assert hosts.vacancy_id_in_path(lookalike) == VACANCY, "so does the vacancy check"

    with pytest.raises(hosts.NotOnHhError):
        hosts.open_hh_page(page, lookalike, expect_vacancy=VACANCY)

    assert page.url == "about:blank", "nothing may be opened before the host is checked"


def test_a_redirect_that_leaves_hh_is_refused_after_the_page_loads() -> None:
    """The other half: the address was fine and hh sent us off the site."""
    page = _Page(lands_on=f"https://hh.kz.example.com/vacancy/{VACANCY}")

    with pytest.raises(hosts.NotOnHhError):
        hosts.open_hh_page(page, f"https://hh.kz/vacancy/{VACANCY}", expect_vacancy=VACANCY)


def test_what_on_hh_means_is_the_site_and_not_a_list_of_cities(tmp_path: Path) -> None:
    """Because the regional subdomains are the reason this helper exists at all.

    An allowlist of whole hostnames would reject the very redirect the module
    was written to survive: hh chooses the city, and it may choose one the data
    file never named. So the unit is the last two labels — every subdomain of an
    hh site passes, a country root the deployment does not crawl does not, and
    ``hh.kz.example.com`` reduces to ``example.com``.
    """
    path = tmp_path / "sites.yaml"
    path.write_text("sites:\n  - host: almaty.hh.kz\n    city: Алматы\n", encoding="utf-8")

    sites = hosts.hh_sites(path)

    assert sites == frozenset({"hh.kz"})
    assert hosts.on_hh(f"https://taraz.hh.kz/vacancy/{VACANCY}", sites), "an unnamed city passes"
    assert hosts.on_hh("https://hh.kz/", sites)
    assert not hosts.on_hh("https://hh.ru/", sites)
    assert not hosts.on_hh("https://hh.kz.example.com/", sites)
    assert not hosts.on_hh("http://almaty.hh.kz/", sites), "http is not a site hh serves"
    assert not hosts.on_hh("about:blank", sites), "and neither is a page that never loaded"


def test_the_allowlist_narrows_when_the_data_file_is_gone_and_never_empties(
    tmp_path: Path,
) -> None:
    """An allowlist that empties itself refuses every navigation, which is its own bug.

    The floor is :data:`agent.hosts.FALLBACK_HOST`, which is also the address
    ``login.py`` opens when nothing more specific is known.
    """
    assert hosts.hh_sites(tmp_path / "nothing-here.yaml") == frozenset({"hh.kz"})
    assert hosts.on_hh(f"https://astana.hh.kz/vacancy/{VACANCY}", hosts.hh_sites(tmp_path))


def test_a_redirect_to_a_city_the_data_file_never_named_still_opens(tmp_path: Path) -> None:
    """The check must not break the redirect it was added next to.

    hh picks the regional subdomain from a cookie, not from this repository, and
    a deployment that lists one city still has to survive being sent to another.
    """
    path = tmp_path / "sites.yaml"
    path.write_text("sites:\n  - host: astana.hh.kz\n    city: Астана\n", encoding="utf-8")
    page = _Page(
        lands_on=f"https://taraz.hh.kz/vacancy/{VACANCY}",
        raises="Error: page.goto: net::ERR_ABORTED at https://hh.kz/vacancy/x",
    )

    landed = hosts.open_hh_page(
        page, f"https://hh.kz/vacancy/{VACANCY}", expect_vacancy=VACANCY, sites_path=path
    )

    assert landed == f"https://taraz.hh.kz/vacancy/{VACANCY}"


def test_every_navigation_in_this_package_goes_through_the_helper() -> None:
    """A ratchet, because the same bug returns wherever a URL arrives from outside.

    Tightened 2026-09-07. It used to forgive ``submit.py`` and ``run.py`` as
    stragglers of a migration that has since finished: neither contains a raw
    ``page.goto`` any more, so the allowance forgave nothing that existed and
    quietly licensed both files to grow one back. A ratchet that does not turn
    is a comment. The only file permitted to navigate is the one that owns the
    checks that come with navigating.
    """
    offenders = {
        path.name
        for path in (REPO_ROOT / "agent").glob("*.py")
        if "page.goto(" in path.read_text(encoding="utf-8") and path.name != "hosts.py"
    }

    assert not offenders, f"these must use agent.hosts.open_hh_page: {offenders}"


# ── what "signed in" means ────────────────────────────────────────────


def test_an_already_signed_in_profile_is_still_detected() -> None:
    """The defect: the signal was the difference between two reads of the page.

    On a profile that is already authenticated both reads come from the same
    session, so the difference is empty — for the exact opposite of the reason
    it was checking for, and sign-in was never detected at all.
    """
    both = {"applicantVacancyResponseStatuses", "userLabelsForVacancies", "vacancyView"}

    signal = login.choose_signal(before=both, after=both)

    assert signal.signed_in
    assert signal.method == "markers"
    assert set(signal.keys) == set(login.AUTH_MARKERS)


def test_a_measured_difference_is_preferred_over_the_named_markers() -> None:
    """The markers are a constant with evidence; a difference is a measurement.

    When both are available the difference is narrowed to the markers it
    confirms, because a whole diff carries per-page keys that legitimately vary
    between two loads and asserting those turns an ordinary page change into
    "your session expired".
    """
    before = {"vacancyView"}
    after = before | {"applicantVacancyResponseStatuses", "someUnrelatedBanner"}

    signal = login.choose_signal(before=before, after=after)

    assert signal.method == "difference"
    assert signal.markers_confirmed
    assert signal.keys == ("applicantVacancyResponseStatuses",)
    assert signal.also_appeared == ("someUnrelatedBanner",)


def test_no_sign_of_an_account_reads_as_no_account() -> None:
    """Nothing may record "cannot tell" as "yes"."""
    signal = login.choose_signal(before={"vacancyView"}, after={"vacancyView"})

    assert not signal.signed_in
    assert signal.method == "none"


def test_what_login_writes_is_exactly_what_the_health_check_asserts(tmp_path: Path) -> None:
    """The two halves have to agree, and here they are made to prove it.

    The field name comes from ``login.SIGNAL_FIELD`` on both sides and the
    contents come from ``login.choose_signal``, so the writer and the reader
    cannot drift apart in a rename or in a change of policy.
    """
    both = {"applicantVacancyResponseStatuses", "userLabelsForVacancies"}
    signal = login.choose_signal(before=both, after=both)
    path = tmp_path / "session_signal.json"
    payload = login.signal_payload(signal, "https://astana.hh.kz/vacancy/1")
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    recorded = session.load_signal(path)

    assert recorded == list(signal.keys)
    assert session.signal_source(path) == "https://astana.hh.kz/vacancy/1"
    live: dict[str, Any] = {key: {"something": 1} for key in recorded}
    session.check(live, signal=recorded)
    assert session.looks_authenticated(live, path)

    # hh ships null shells for keys it has not filled, so an empty value is an
    # expired session and not a present key.
    expired: dict[str, Any] = {key: {} for key in recorded}
    with pytest.raises(session.SessionExpiredError):
        session.check(expired, signal=recorded)
    assert not session.looks_authenticated(expired, path)


def test_the_signal_field_is_named_in_one_place(tmp_path: Path) -> None:
    """Renaming it in login.py must not leave session.py reading the old key."""
    path = tmp_path / "session_signal.json"
    path.write_text(json.dumps({login.SIGNAL_FIELD: ["x"]}), encoding="utf-8")
    assert session.load_signal(path) == ["x"]

    path.write_text(json.dumps({"some_other_field": ["x"]}), encoding="utf-8")
    with pytest.raises(session.SignalUnknownError):
        session.load_signal(path)


def test_the_uncertainty_about_the_markers_is_written_down_not_implied() -> None:
    """They were chosen from one account on one day and there is no second one."""
    both = set(login.AUTH_MARKERS)
    payload = login.signal_payload(login.choose_signal(before=both, after=both))

    assert payload["how_chosen"] == "markers"
    assert "AUTH_MARKERS" in payload["uncertainty"]
    assert "аккаунт" in payload["uncertainty"]


# ── the two apply controls ────────────────────────────────────────────


def test_both_apply_controls_are_recorded_and_either_can_be_waited_for() -> None:
    """A fresh vacancy and an already-applied one carry different controls.

    Measured 2026-09-06: on a vacancy that already has an application the plain
    ``-top`` control is absent ENTIRELY, which is how ``--stage open-form``
    became unreachable by its own guard — it required an already-applied target
    and then waited for a selector that never exists on one.
    """
    query = selectors.any_apply_control()

    assert selectors.APPLY_LINK.query in query
    assert selectors.APPLY_LINK_AGAIN.query in query
    assert selectors.APPLY_LINK.query != selectors.APPLY_LINK_AGAIN.query
    assert selectors.VIEW_TOPIC.usable_for_applying


def test_every_control_the_apply_flow_can_click_is_one_stage_0_verified() -> None:
    """Corrected 2026-09-07, having asserted the opposite for a day.

    This test used to require that the repeat control is NOT in
    ``REQUIRED_FOR_APPLYING``, reasoning that a repeat application is not
    something this agent performs. True, and an answer to a different question.
    ``agent/submit.py`` clicks ``page.locator(any_apply_control()).first``, and
    :func:`agent.selectors.any_apply_control` joins the two controls, so the
    flow could click one that :func:`assert_ready_to_apply` had never looked at
    — which is the whole failure this module exists to prevent, arrived at by
    the front door.

    Written over the query rather than over a list of names, so that a third
    control added to ``any_apply_control`` fails here until somebody verifies it
    too.
    """
    verified = {
        name
        for selector in selectors.REQUIRED_FOR_APPLYING
        for name, _is_prefix in selectors.names_in(selector.query)
    }
    clickable = {name for name, _is_prefix in selectors.names_in(selectors.any_apply_control())}

    assert clickable, "the flow clicks something, and it has to be a data-qa"
    assert clickable <= verified, f"clicked but never verified: {sorted(clickable - verified)}"
    selectors.assert_ready_to_apply()


def test_the_control_that_asks_the_employer_a_question_stays_unclickable() -> None:
    """Seven decoys on the page whose names start like the real control's.

    ``any_apply_control`` is a query the flow clicks blind, so the thing to
    check is not that the decoy is absent from a list but that the query cannot
    reach it.
    """
    decoy_prefixes = [
        name
        for name, is_prefix in selectors.names_in(selectors.QUESTION_WIDGET_DECOY.query)
        if is_prefix
    ]

    assert decoy_prefixes, "the decoy is recorded as a prefix query"
    for name, _is_prefix in selectors.names_in(selectors.any_apply_control()):
        for prefix in decoy_prefixes:
            assert not name.startswith(prefix), f"{name} is reachable from the decoy query"


def test_which_apply_control_is_on_the_page_still_decides_nothing() -> None:
    """hh ALLOWS a repeat application, so neither button is evidence either way.

    Verifying the repeat control says the flow may click it. It says nothing
    about idempotency, which is read from ``negotiations.total`` in the page's
    own state, and the read-only link to the conversation an existing
    application started is not part of applying at all.
    """
    required = set(selectors.REQUIRED_FOR_APPLYING)

    assert selectors.APPLY_LINK in required
    assert selectors.APPLY_LINK_AGAIN in required
    assert selectors.VIEW_TOPIC not in required
    assert "idempotency" in selectors.APPLY_LINK_AGAIN.note


def test_the_warning_line_is_one_name_with_two_measured_meanings() -> None:
    """Read by text, never by presence — both were seen under one data-qa.

    And neither of them stops an application. That was true of the second from
    the start and became true of the first on 2026-09-07, when somebody measured
    it instead of assuming it; see the test two below.
    """
    assert selectors.RESUME_VISIBILITY_NOTICE != selectors.LIKELY_REJECTION_WARNING
    assert "видимость резюме" in selectors.RESUME_VISIBILITY_NOTICE
    assert selectors.HIDDEN_RESUME_WARNING.usable_for_applying


def test_the_notice_is_what_hh_wrote_and_not_what_somebody_remembered() -> None:
    """Corrected 2026-09-07: hh writes U+00A0 and the constant had plain spaces.

    Documented as "hh's exact words", in the file whose entire discipline is
    that measurements are not retyped from memory, and inexact in two places.
    It matters twice over: the string is the one the owner is shown instead of a
    paraphrase, and a constant allowed to drift by one invisible character is a
    constant nobody can use as a reference later.

    The raw dump it was taken from lives under ``agent/probe/``, which is
    gitignored for good reasons, so the comparison against it runs on the
    owner's machine and the property runs everywhere. That is the same split the
    evidence files exist for.
    """
    assert selectors.RESUME_VISIBILITY_NOTICE.count("\u00a0") == 2
    assert "на\u00a0эту вакансию" in selectors.RESUME_VISIBILITY_NOTICE
    assert "на\u00a0«Видно" in selectors.RESUME_VISIBILITY_NOTICE
    # U+00A0 is 0xA0 in cp1251, so being exact costs the console nothing.
    selectors.RESUME_VISIBILITY_NOTICE.encode("cp1251")

    dump = REPO_ROOT / "agent" / "probe" / "_warn.json"
    if dump.is_file():
        measured = json.loads(dump.read_text(encoding="utf-8"))["warnings"][0]["text"]
        assert measured == selectors.RESUME_VISIBILITY_NOTICE


def test_the_measurement_that_removed_the_hard_stop_is_in_the_repository() -> None:
    """The evidence for a deletion, committed, and checked the way selectors are.

    Until 2026-09-07 this package treated hh's resume-visibility sentence as a
    refusal, and since that sentence stands on every vacancy while the owner's
    resume carries that setting, it could not send anything at all. The rule came
    from a guess in a brief and stood for a day because it forbade the one
    experiment that refutes it. Deleting it on the strength of a sentence in a
    commit message would be the same mistake pointed the other way, so the
    measurement lives here: every number, hh's own string, and how it was taken.

    Held to three things. The record has to exist and be readable, or the claim
    is back to being something somebody typed. hh's string in it has to be the
    string the code carries, exactly, so the record and
    :data:`~agent.selectors.RESUME_VISIBILITY_NOTICE` cannot drift apart. And it
    must not dress itself up: ``produced_by`` has to say in words that a person
    wrote it. No probe produced this — the probe does not press the submit button
    — and a hand-made record wearing a machine's name is worse than no record.
    """
    path = selectors.EVIDENCE_DIR / f"{selectors.SEND_UNDER_VISIBILITY_EVIDENCE}.json"
    record = json.loads(path.read_text(encoding="utf-8"))

    assert record["schema"] == selectors.MEASUREMENT_SCHEMA
    assert record["measured_on"] == "2026-09-07"
    # The measured numbers: no application before, one after, and the control
    # left on the page afterwards is the repeat-application one.
    assert record["applications_before"] == 0
    assert record["applications_after"] == 1
    assert record["repeat_apply_controls_after"] == 1
    assert record["warning_shown"] == selectors.RESUME_VISIBILITY_NOTICE
    produced_by = record["produced_by"].casefold()
    assert "человек" in produced_by, "a hand-made record has to say a person made it"
    assert "не вывод" in produced_by, "and has to say which machine did not"
    assert record["source_artefact"].startswith("agent/probe/_cdp_send.json")

    # On the owner's machine the unredacted artefact is there and the numbers
    # have to agree with it. Everywhere else this half is skipped, exactly as it
    # is for the selector dumps: the artefact names the vacancy applied to.
    artefact = REPO_ROOT / "agent" / "probe" / "_cdp_send.json"
    if artefact.is_file():
        cdp = json.loads(artefact.read_text(encoding="utf-8"))
        assert cdp["total_before"] == record["applications_before"]
        assert cdp["total_after"] == record["applications_after"]
        assert cdp["again_button"] == record["repeat_apply_controls_after"]
        assert cdp["warning"] == record["warning_shown"]


def test_nothing_in_the_package_turns_a_form_warning_into_a_refusal() -> None:
    """The rule is gone from every place it lived, not only from the classifier.

    It lived in four: an exception class in ``agent/submit.py``, the call site
    that raised it, a ``decide_on_form`` branch in ``agent/prefilter.py``, and a
    handler in ``agent/run.py`` that caught it by name. Removing the branch and
    leaving the class would leave the next reader with a refusal that is caught
    and never raised, which reads exactly like a rule that still exists — and the
    properties on ``FormWarnings`` that answered "may I send" would read like a
    permission check with nothing behind it.
    """
    from agent import prefilter, run, state_page, submit

    assert not hasattr(submit, "RefusedByHHError")
    assert not hasattr(prefilter, "decide_on_form")
    assert not hasattr(run, "RefusedByHHError"), "run.py still imports the refusal it cannot get"
    quiet = state_page.FormWarnings(visibility=None, likely_rejection=None)
    for gone in ("may_send", "verdict"):
        assert not hasattr(quiet, gone)


# ── evidence that survives a gitignored directory ─────────────────────

#: Everything an artefact of the owner's own session knows that a committed file
#: in ``agent/evidence/`` must not repeat. The two real vacancy ids are the ones
#: the artefacts behind these files name: 136131345 for the 2026-09-06 selector
#: dumps, and 136638256 for the 2026-09-07 send measurement. Both are vacancies
#: the owner applied to, which is exactly the kind of fact a redaction drops.
PRIVATE_TO_THE_OWNER = (
    VACANCY,
    "136131345",
    "136638256",
    "https://",
    "negotiations",
    "applicantVacancyResponseStatuses",
    "topicList",
)


def test_the_evidence_directory_is_committable() -> None:
    """The whole point: ``agent/probe/`` is ignored and this must not be.

    While the evidence lived in the probe's report, a selector could only be
    verified on the machine that ran the probe, and a test asserting the
    selectors are verified could not run anywhere else.
    """
    ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")

    assert "agent/probe/" in ignored
    assert "agent/evidence" not in ignored
    assert selectors.EVIDENCE_DIR.is_dir()


def test_every_evidence_file_says_what_produced_it_and_carries_nothing_private() -> None:
    """A redaction is only worth something if it is checked, so it is checked.

    Widened 2026-09-07, when a second kind of file arrived in this directory: a
    measurement that is not about a selector. Two rules are held over every file
    whatever its schema — it says where it came from, and it carries nothing that
    belongs to the owner — because those are the two that make the directory
    committable at all. The selector contract is then held over the files that
    claim it, and a file claiming neither schema fails rather than passing
    quietly, which is the failure mode this whole check exists to avoid.
    """
    files = sorted(selectors.EVIDENCE_DIR.glob("*.json"))

    assert files, "no evidence at all means no selector can be verified"
    for path in files:
        text = path.read_text(encoding="utf-8")
        payload = json.loads(text)
        assert str(payload.get("produced_by") or "").strip(), path.name
        for secret in PRIVATE_TO_THE_OWNER:
            assert secret not in text, f"{path.name} carries {secret!r}"

        assert payload["schema"] in {
            selectors.EVIDENCE_SCHEMA,
            selectors.MEASUREMENT_SCHEMA,
        }, path.name
        if payload["schema"] == selectors.EVIDENCE_SCHEMA:
            assert payload["stage"] in {"inspect", "open-form"}, path.name
            assert isinstance(payload["data_qa_seen"], list), path.name


def test_the_redaction_is_a_whitelist_and_not_a_deletion() -> None:
    """Built by naming what goes in, so a new report field cannot leak by default."""
    report: dict[str, Any] = {
        "url": f"https://almaty.hh.kz/vacancy/{VACANCY}",
        "vacancy_id": VACANCY,
        "stage": "open-form",
        "authenticated": True,
        "applicant_vacancy_response_status": {"negotiations": {"total": 1, "topicList": [{}]}},
        "modal_text": "Отклик на вакансию",
        "data_qa_after_click": {
            "candidates": ["modal-overlay"],
            "decoys_ask_the_employer_a_question": ["vacancy-response-question"],
        },
        "something_added_next_year": {"vacancy_id": VACANCY},
    }

    evidence = probe_apply.evidence_for(report)
    text = json.dumps(evidence, ensure_ascii=False)

    assert evidence["data_qa_seen"] == ["modal-overlay"]
    assert evidence["decoys_seen"] == ["vacancy-response-question"]
    for secret in PRIVATE_TO_THE_OWNER:
        assert secret not in text


def test_an_evidence_file_that_does_not_say_where_it_came_from_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One file in this repository was not written by the probe, and it says so.

    Corrected 2026-09-07, along with the comment in ``selectors.py`` this used
    to repeat. Requiring the field does NOT mean "a hand-made record can never
    look machine-made" — the check is that the string is non-empty, and anybody
    typing a file by hand can type ``agent/probe_apply.py`` into it. What it
    buys is that a record which does not say where it came from is refused
    instead of silently trusted, and that a record which lies has to lie in
    writing, in a committed file, beside the artefact it names. That is worth a
    check. It is not worth a sentence claiming more than it does.
    """
    payload = selectors.redact(["modal-overlay"], [], stage="open-form", authenticated=True)
    del payload["produced_by"]
    (tmp_path / "anonymous-run.json").write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(selectors, "EVIDENCE_DIR", tmp_path)

    unsourced = selectors.Selector(
        name="unsourced",
        query='[data-qa="modal-overlay"]',
        scope=selectors.Scope.AUTHENTICATED,
        evidence="anonymous-run",
    )

    assert any("чем он получен" in problem for problem in selectors.problems_with((unsourced,)))


def test_evidence_from_a_run_that_was_not_signed_in_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The response form exists only under an account, so anything else is not it."""
    payload = selectors.redact(["modal-overlay"], [], stage="open-form", authenticated=False)
    (tmp_path / "logged-out.json").write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(selectors, "EVIDENCE_DIR", tmp_path)

    anonymous_run = selectors.Selector(
        name="from_a_logged_out_run",
        query='[data-qa="modal-overlay"]',
        scope=selectors.Scope.AUTHENTICATED,
        evidence="logged-out",
    )

    problems = selectors.problems_with((anonymous_run,))
    assert any("без авторизации" in problem for problem in problems)


def _filed(
    directory: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    seen: list[str],
    decoys: list[str] | None = None,
    schema: str | None = None,
) -> None:
    """Put one evidence file in front of ``problems_with`` and nothing else.

    Written through :func:`agent.selectors.redact`, the same function the probe
    uses, so a test can only file evidence in the shape the probe produces.
    """
    payload = selectors.redact(seen, decoys or [], stage="open-form", authenticated=True)
    if schema is not None:
        payload["schema"] = schema
    (directory / "run.json").write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(selectors, "EVIDENCE_DIR", directory)


def _selector(query: str, *, evidence: str | None = "run") -> selectors.Selector:
    """One selector claiming the file :func:`_filed` just wrote."""
    return selectors.Selector(
        name="under_test",
        query=query,
        scope=selectors.Scope.AUTHENTICATED,
        evidence=evidence,
    )


def test_a_selector_naming_a_data_qa_the_run_never_saw_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check the module docstring calls "what makes this more than a checkbox".

    Untested until 2026-09-07, which is the wrong thing to leave uncovered: it
    is the one check that separates a selector from a guess filed next to real
    evidence. Everything else about the file below is correct — it exists, its
    schema matches, its stage could have seen the control, its authentication
    was measured — and the query names something that was not on the page.
    """
    _filed(tmp_path, monkeypatch, seen=["modal-overlay"])

    assert selectors.problems_with((_selector('[data-qa="modal-overlay"]'),)) == []

    guessed = selectors.problems_with((_selector('[data-qa="vacancy-response-submit-popup"]'),))
    assert any("не из этого прогона" in problem for problem in guessed)
    assert any("vacancy-response-submit-popup" in problem for problem in guessed)


def test_a_query_naming_several_data_qa_needs_all_of_them_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One of two being real is how a guess rides in on something measured."""
    _filed(tmp_path, monkeypatch, seen=["modal-overlay"])

    half_true = selectors.problems_with(
        (_selector('[data-qa="modal-overlay"] [data-qa="letter-textarea"]'),)
    )

    assert any("letter-textarea" in problem for problem in half_true)


def test_a_prefix_query_is_checked_by_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``[data-qa^="x"]`` is the other form this package writes, and it matters.

    The seven question-widget decoys are recorded under a prefix, and reading
    that query as an exact name would refuse evidence that genuinely covers it —
    while ignoring the caret would let any prefix of anything pass.
    """
    _filed(tmp_path, monkeypatch, seen=[], decoys=["vacancy-response-question_other"])

    assert selectors.problems_with((_selector('[data-qa^="vacancy-response-question"]'),)) == []

    unseen = selectors.problems_with((_selector('[data-qa^="vacancy-response-link"]'),))
    assert any("vacancy-response-link" in problem for problem in unseen)


def test_a_query_with_no_data_qa_in_it_cannot_be_verified_at_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A class selector has nothing to check against, which is not the same as passing.

    hh's class names are hashed per release, so this is exactly the query
    somebody reaches for when the ``data-qa`` stops matching.
    """
    _filed(tmp_path, monkeypatch, seen=["modal-overlay"])

    problems = selectors.problems_with((_selector(".magritte-button_style-accent___TE21J"),))

    assert any("нет ни одного data-qa" in problem for problem in problems)


def test_an_evidence_file_written_to_another_schema_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stamp exists so a change of shape is loud rather than quietly empty.

    Also untested until 2026-09-07, and it guards the check above: a file whose
    ``data_qa_seen`` moved or was renamed would otherwise read as a run that saw
    nothing, and "saw nothing" only ever produces refusals — until the day some
    other field happens to line up.
    """
    _filed(tmp_path, monkeypatch, seen=["modal-overlay"], schema="hh-agent-selector-evidence/2")

    problems = selectors.problems_with((_selector('[data-qa="modal-overlay"]'),))

    assert any("схема" in problem for problem in problems)


def test_a_selector_pointing_at_an_evidence_file_that_is_not_there_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Naming a file is not having one."""
    _filed(tmp_path, monkeypatch, seen=["modal-overlay"])

    problems = selectors.problems_with((_selector('[data-qa="modal-overlay"]', evidence="nope"),))

    assert any("нет" in problem for problem in problems)


def test_a_modal_selector_is_not_verified_by_a_stage_that_never_opened_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``inspect`` clicks nothing, so it cannot have seen the response form."""
    payload = selectors.redact(["modal-overlay"], [], stage="inspect", authenticated=True)
    (tmp_path / "looked-only.json").write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(selectors, "EVIDENCE_DIR", tmp_path)

    modal = selectors.Selector(
        name="response_form",
        query='[data-qa="modal-overlay"]',
        scope=selectors.Scope.AUTHENTICATED,
        evidence="looked-only",
    )

    assert any("этап" in problem for problem in selectors.problems_with((modal,)))


def test_the_probe_writes_both_halves_of_a_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The full report where it is ignored, the redaction where it can be committed."""
    monkeypatch.setattr(selectors, "PROBE_DIR", tmp_path / "probe")
    monkeypatch.setattr(probe_apply, "PROBE_DIR", tmp_path / "probe")
    monkeypatch.setattr(probe_apply, "EVIDENCE_DIR", tmp_path / "evidence")
    report: dict[str, Any] = {
        "url": f"https://almaty.hh.kz/vacancy/{VACANCY}",
        "vacancy_id": VACANCY,
        "stage": "open-form",
        "authenticated": True,
        "data_qa_after_click": {
            "candidates": ["modal-overlay", "vacancy-response-submit-popup"],
            "decoys_ask_the_employer_a_question": [],
        },
    }

    full, evidence = probe_apply.write_report(report)

    assert full.parent.parent == tmp_path / "probe"
    assert evidence.parent == tmp_path / "evidence"
    assert evidence.stem == full.parent.name, "the evidence is named after its run"
    assert VACANCY in full.read_text(encoding="utf-8")
    assert VACANCY not in evidence.read_text(encoding="utf-8")


# ── the console this actually runs on ─────────────────────────────────


def test_every_refusal_this_stage_prints_survives_a_cp1251_console(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Russian Windows console encodes cp1251 and a stray glyph kills the run.

    These messages are printed at exactly the moment something has gone wrong,
    which is the worst moment to replace the explanation with a traceback about
    an encoding. The letter field is measured now, so its refusal is produced
    by taking that measurement away.
    """
    unmeasured = selectors.Selector(name="letter_field", query="", scope=selectors.Scope.UNVERIFIED)
    monkeypatch.setattr(
        selectors, "REQUIRED_FOR_A_LETTER", (selectors.ADD_COVER_LETTER, unmeasured)
    )
    with pytest.raises(selectors.LetterFieldUnknownError) as letter:
        selectors.assert_letter_field_known()
    str(letter.value).encode("cp1251")

    page = _Page(lands_on="https://almaty.hh.kz/account/login")
    with pytest.raises(hosts.NavigatedElsewhereError) as elsewhere:
        hosts.open_hh_page(page, f"https://hh.kz/vacancy/{VACANCY}", expect_vacancy=VACANCY)
    str(elsewhere.value).encode("cp1251")

    with pytest.raises(hosts.NotOnHhError) as other_host:
        hosts.open_hh_page(
            _Page(lands_on="https://example.com/"), f"https://hh.kz/vacancy/{VACANCY}"
        )
    str(other_host.value).encode("cp1251")

    # The refusal that happens before the browser moves prints the allowlist,
    # which is built out of a data file somebody else edits.
    with pytest.raises(hosts.NotOnHhError) as not_hh:
        hosts.open_hh_page(_Page(lands_on="about:blank"), f"https://example.com/vacancy/{VACANCY}")
    str(not_hh.value).encode("cp1251")

    # hh's own words, kept exactly, have to reach the console too — the
    # non-breaking space in them is 0xA0 in cp1251 and survives.
    selectors.RESUME_VISIBILITY_NOTICE.encode("cp1251")
    selectors.LIKELY_REJECTION_WARNING.encode("cp1251")

    both = set(login.AUTH_MARKERS)
    json.dumps(
        login.signal_payload(login.choose_signal(before=both, after=both)), ensure_ascii=False
    ).encode("cp1251")


@pytest.mark.parametrize(
    "url",
    [
        r"https://evil.com\@hh.kz/vacancy/136773120",
        r"https://hh.kz\.evil.com/vacancy/136773120",
        "https://hh.kz\n.evil.com/vacancy/136773120",
        "https://hh.kz\t.evil.com/vacancy/136773120",
    ],
)
def test_a_url_python_and_chromium_read_differently_is_refused(url: str) -> None:
    """The host check must answer the question the browser is about to act on.

    WHATWG ends a special-scheme URL's authority at a backslash, and Chromium
    parses with WHATWG; ``urlsplit`` does not. So ``https://evil.com\\@hh.kz/x``
    has the hostname ``hh.kz`` as far as Python is concerned and navigates to
    ``evil.com`` as far as the browser is concerned. A newline or a tab is the
    same disagreement wearing a different hat: the browser strips them, Python
    keeps them.

    This check is the one place in the package that does not trust the queue,
    and the queue is a file. A check that answers a different question from the
    one the browser is about to act on is not a check.
    """
    assert urlsplit(url).hostname is not None, "the point is that Python parses it happily"
    assert not hosts.on_hh(url)


def test_an_ordinary_regional_vacancy_url_is_still_accepted() -> None:
    """The refusal above must not have closed the door on the normal case."""
    assert hosts.on_hh("https://almaty.hh.kz/vacancy/136773120")
    assert hosts.on_hh("https://hh.kz/vacancy/136773120")
