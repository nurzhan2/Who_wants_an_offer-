"""Stage 0: look at the real apply form and write down what is there.

The brief makes this blocking, and it is right to: hh uses ``data-qa``
attributes, so a guessed selector feels safe, and a guess that happens to match
something clicks an unknown control on somebody's live account. Nothing in this
package that touches the apply flow starts until a run here has recorded what
the page actually carries — see ``agent/selectors.py::assert_ready_to_apply``.

Two stages, because they carry different risks.

``--stage inspect`` never clicks anything. It reads the page, dumps every
``data-qa`` that could plausibly belong to an application, and dumps this
vacancy's entry in ``applicantVacancyResponseStatuses`` verbatim. It cannot send
anything at all.

``--stage open-form`` clicks the apply control, because the submit button, the
resume line, the warning and the letter button live on the other side of that
click and there is no way to see them without it.

**What that click was feared to do, and what it was measured to do.** The
original version of this module refused to run ``open-form`` on anything but a
vacancy the owner had already applied to, and the argument was sound at the
time: the apply control is ``<a href="/applicant/vacancy_response?vacancyId=…">``
so following it is a GET document navigation that a method-based guard does not
touch, and ``context.route`` cannot see a request issued from a service worker,
so no interception is absolute. If hh completed the application on that first
GET, the click would send something nobody confirmed. Nobody knew whether it
did, and the brief's instruction for exactly that situation is not to route
around it.

MEASURED 2026-09-06, on the owner's logged-in profile, on a fresh vacancy:
**the click sends nothing.** Following the link fetches
``GET /applicant/vacancy_response/popup?vacancyId=…&isTest=no&withoutTest=no
&lux=true&fingerprintIteration2=…&alreadyApplied=false`` and renders a modal;
the only other traffic was analytics, and a run that aborted every non-GET
request produced the modal intact. So the refusal above is retired: it was
protecting against a possibility that has now been checked, and keeping it would
have kept ``--stage open-form`` unreachable — on an already-applied vacancy the
plain ``-top`` control does not exist at all, so the stage's own guard demanded
a page where its own selector could never match.

``--already-applied`` survives as a *label*: it records what the owner believes
about the target, so the report says which kind of page it photographed. It no
longer gates anything.

**What now carries the safety, since it is no longer a refusal.** Two
independent guards, kept both because neither subsumes the other. The send is a
non-GET request, so every non-GET is aborted here — measured to leave the modal
working. And the URL check refuses any application-shaped request naming a
different vacancy, which a method check would happily allow. A separate
``page.on("request")`` recorder shouts if anything reached an application URL
without passing the interceptor at all, which is what a service worker would
look like.

**Waiting.** The modal lives in the main frame, not an iframe, and it appears
LATER than two seconds after the click. The earlier report at
``agent/probe/20260906-181519/probe.json`` contains none of the modal's names
for exactly that reason: it waited two seconds and photographed the page before
the modal rendered. Everything here waits for a selector.

**The letter field, measured 2026-09-16.** ``add-cover-letter`` is the button
that reveals it. This stage clicks it and dumps again, and lists every form
control inside the modal with its attributes; the run in
``agent/evidence/20260916-125039.json`` is where ``selectors.LETTER_FIELD``
comes from. Run it again after a redesign rather than guessing.

    uv run python -m agent.probe_apply --stage inspect   --url https://hh.kz/vacancy/123
    uv run python -m agent.probe_apply --stage open-form --url https://hh.kz/vacancy/123
"""

import argparse
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, final

from agent.browser import open_browser, screenshot_on_error
from agent.gate import looks_like_an_application, post_body, vacancy_ids_in
from agent.hosts import open_hh_page, vacancy_id_in_path
from agent.selectors import (
    ADD_COVER_LETTER,
    EVIDENCE_DIR,
    PROBE_DIR,
    PROBE_FILENAME,
    RESPONSE_FORM,
    SUBMIT_BUTTON,
    any_apply_control,
    redact,
)
from agent.session import looks_authenticated
from agent.state_page import read_state

#: Attributes worth reporting. Broad on purpose — a probe that only looked for
#: what we expect would confirm what we expect. ``modal``, ``overlay``,
#: ``popup``, ``warning`` and ``cover`` were added after the 2026-09-06
#: measurement: the response form's own container is ``modal-overlay`` and the
#: earlier pattern matched none of the modal's names, so the one element every
#: other selector is scoped inside would not have been recorded at all.
INTERESTING = re.compile(
    r"response|apply|negotiat|letter|submit|captcha|test|resume"
    r"|modal|overlay|popup|warning|cover|dialog",
    re.I,
)

#: The «задать вопрос работодателю» widget. Seven of these on a vacancy page and
#: every one of them matches a search for "response". They are reported in their
#: own section so nobody mistakes one for the apply form.
DECOY = re.compile(r"^vacancy-response-question")

DATA_QA = re.compile(r'data-qa="([^"]+)"')

#: How long the modal may take to render. Measured at more than two seconds and
#: waited for by selector, never by clock.
MODAL_TIMEOUT_MS: int = 20_000

#: How long to wait for something to change after «Добавить сопроводительное».
#: Shorter, and a timeout here is not a failure: the dump afterwards is taken
#: either way and records whatever did appear.
LETTER_TIMEOUT_MS: int = 8_000

#: Every control that could be a text field, listed with its attributes. A tag
#: name is not a guessed ``data-qa``: this asks the page what it has rather than
#: asserting what it should have, which is the whole difference between this
#: file and a guess.
FORM_CONTROLS_JS = """els => els.map(e => ({
  tag: e.tagName,
  qa: e.getAttribute('data-qa') || '',
  name: e.getAttribute('name') || '',
  type: e.getAttribute('type') || '',
  placeholder: e.getAttribute('placeholder') || '',
  aria: e.getAttribute('aria-label') || '',
  editable: e.isContentEditable === true
    || ((e.tagName === 'TEXTAREA' || e.tagName === 'INPUT')
        && !e.disabled && !e.readOnly && e.type !== 'hidden')
}))"""
# ``editable`` used to be ``isContentEditable`` alone, which is false for every
# ``<textarea>`` and ``<input>`` by definition: the 2026-09-16 run reported the
# letter field as not editable for that reason and no other.


@final
@dataclass(slots=True)
class RequestLog:
    """Every application-shaped request, and whether the interceptor saw it."""

    #: Application-shaped requests that were refused: another vacancy's.
    intercepted: list[str] = field(default_factory=list)
    #: Anything that was not a GET, as ``"METHOD url"`` for the report. The send
    #: is a non-GET, so this list being the reason nothing left is a fact about
    #: the network, not a promise.
    blocked_non_get: list[str] = field(default_factory=list)
    #: The same requests by bare URL, which is what :attr:`observed` holds. The
    #: escape check compared the prefixed strings against bare URLs, so a
    #: request the handler *had* refused could never match and read as escaped.
    blocked_non_get_urls: list[str] = field(default_factory=list)
    #: This vacancy's own GETs, which were let through: the popup fetch.
    allowed: list[str] = field(default_factory=list)
    observed: list[str] = field(default_factory=list)

    def escapes(self) -> list[str]:
        """URLs the page reported that the route handler never got."""
        seen = set(self.intercepted) | set(self.allowed) | set(self.blocked_non_get_urls)
        return [url for url in self.observed if url not in seen]


def _collect_data_qa(page_html: str) -> dict[str, list[str]]:
    """Candidate controls, split so a decoy cannot be mistaken for the real thing."""
    found: set[str] = set()
    for value in DATA_QA.findall(page_html):
        for part in value.split():
            if INTERESTING.search(part):
                found.add(part)
    return {
        "candidates": sorted(name for name in found if not DECOY.match(name)),
        "decoys_ask_the_employer_a_question": sorted(name for name in found if DECOY.match(name)),
    }


def _response_status(state: dict[str, Any], vacancy_id: str) -> Any:
    """This vacancy's entry in the applicant status map, verbatim.

    ``Any`` because the whole point is to record a shape as it is: interpreting
    it here would be the guess this stage exists to avoid. What reads it later
    is looking for ``negotiations.total``, which is the only source of truth
    about whether an application already exists — never the presence of a
    button, because hh allows a repeat application and renders one.
    """
    statuses = state.get("applicantVacancyResponseStatuses")
    if not isinstance(statuses, dict):
        return None
    return statuses.get(str(vacancy_id))


def _form_controls(page: Any) -> Any:
    """Every text-ish control inside the response modal, with its attributes.

    ``Any`` for the page and the result: the page would mean importing
    playwright at module scope, which ``agent/browser.py`` explains we do not
    do, and the result is whatever the browser found, which is the point.

    A failure is recorded rather than raised. This runs after the useful part of
    the measurement and losing the whole run to it would be the wrong trade —
    but it is never silently dropped, because a missing measurement that looks
    like an empty one is how a gap gets forgotten.
    """
    try:
        return page.eval_on_selector_all(
            f"{RESPONSE_FORM.query} textarea, {RESPONSE_FORM.query} input, "
            f"{RESPONSE_FORM.query} [contenteditable]",
            FORM_CONTROLS_JS,
        )
    except Exception as error:
        return {"error": f"{type(error).__name__}: {error}"}


def _run_dir() -> Path:
    """A fresh directory for this run's full report."""
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    path = PROBE_DIR / stamp
    path.mkdir(parents=True, exist_ok=True)
    return path


def _seen_names(report: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Every ``data-qa`` this report saw, across all of its dumps.

    The evidence file is built from this rather than from one section, so a name
    that only appeared after the letter button was clicked still counts as seen.
    """
    candidates: set[str] = set()
    decoys: set[str] = set()
    for key in ("data_qa", "data_qa_after_click", "data_qa_after_letter_click"):
        section = report.get(key)
        if not isinstance(section, dict):
            continue
        found = section.get("candidates")
        if isinstance(found, list):
            candidates.update(str(name) for name in found)
        widgets = section.get("decoys_ask_the_employer_a_question")
        if isinstance(widgets, list):
            decoys.update(str(name) for name in widgets)
    return sorted(candidates), sorted(decoys)


def evidence_for(report: dict[str, Any]) -> dict[str, Any]:
    """The committable half of a run: names, stage, authentication, nothing else.

    Built by naming every field that goes in rather than by removing fields from
    the report, so a new key in the report cannot leak by being forgotten.
    """
    candidates, decoys = _seen_names(report)
    return redact(
        candidates,
        decoys,
        stage=str(report.get("stage") or ""),
        authenticated=report.get("authenticated") is True,
    )


def inspect(url: str, *, already_applied: bool) -> dict[str, Any]:
    """Read one vacancy page without touching anything on it."""
    vacancy_id = vacancy_id_in_path(url)
    if vacancy_id is None:
        raise SystemExit(f"Не похоже на ссылку вакансии: {url}")

    with open_browser() as context:
        page = context.new_page()
        landed = open_hh_page(page, url, expect_vacancy=vacancy_id)
        content = page.content()

    state = read_state(content) or {}
    return {
        "url": url,
        "landed_url": landed,
        "vacancy_id": vacancy_id,
        "labelled_already_applied": already_applied,
        "stage": "inspect",
        "authenticated": looks_authenticated(state),
        "data_qa": _collect_data_qa(content),
        "selectors_ready_to_paste": [
            f'[data-qa="{name}"]' for name in _collect_data_qa(content)["candidates"]
        ],
        "applicant_vacancy_response_status": _response_status(state, vacancy_id),
        "vacancy_view_null_fields": {
            key: (state.get("vacancyView") or {}).get(key)
            for key in (
                "@responseLetterRequired",
                "userTestPresent",
                "userTestId",
                "autoResponse",
                "closedForApplicants",
            )
        },
        "state_top_level_keys": sorted(state),
    }


def open_form(url: str, *, already_applied: bool) -> dict[str, Any]:
    """Click the apply control, wait for the modal, and record what appeared.

    Then click «Добавить сопроводительное» and record again, because the letter
    field is behind that second click and is the one selector nobody has
    measured. See the module docstring for what the click was measured to do and
    for the two guards that make sure nothing leaves.
    """
    vacancy_id = vacancy_id_in_path(url)
    if vacancy_id is None:
        raise SystemExit(f"Не похоже на ссылку вакансии: {url}")

    log = RequestLog()
    with open_browser() as context:

        def guard(route: Any) -> None:
            """Two independent refusals, plus a record of everything else.

            ``Any`` for the route because typing it means importing playwright
            at module scope; see ``agent/browser.py``.

            The method check: an application is sent with a non-GET request, so
            aborting every non-GET makes a send physically impossible here
            rather than merely unintended. Measured 2026-09-06 — a run that did
            exactly this still rendered the modal completely, so nothing is lost.

            The URL check: an application-shaped request naming a *different*
            vacancy is refused whatever its method. Neither check subsumes the
            other, which is why both are here.

            What is NOT refused is this vacancy's own GET popup fetch. An
            earlier version aborted it, the click landed on a Chromium error
            page, the report came back empty, and the package could not be
            unblocked by its own documented procedure.
            """
            request = route.request
            request_url = str(request.url)
            if str(request.method).upper() != "GET":
                log.blocked_non_get.append(f"{request.method} {request_url}")
                log.blocked_non_get_urls.append(request_url)
                route.abort()
                return
            if not looks_like_an_application(request_url):
                route.continue_()
                return
            strangers = vacancy_ids_in(request_url, post_body(request)) - {vacancy_id}
            if strangers:
                log.intercepted.append(request_url)
                route.abort()
                return
            log.allowed.append(request_url)
            route.continue_()

        context.route("**/*", guard)
        page = context.new_page()
        page.on(
            "request",
            lambda request: (
                log.observed.append(request.url) if looks_like_an_application(request.url) else None
            ),
        )
        try:
            open_hh_page(page, url, expect_vacancy=vacancy_id)
            # Whichever control this page carries. On an already-applied vacancy
            # the plain "-top" one is absent entirely, which is what made this
            # stage unreachable when it waited on that selector alone.
            page.locator(any_apply_control()).first.click(timeout=15_000)
            # By selector, never by clock: the modal renders later than two
            # seconds after the click, and a fixed wait photographed the page
            # before it existed. Waiting for the submit button as well, because
            # the overlay can be on the page before its contents have loaded.
            page.wait_for_selector(RESPONSE_FORM.query, timeout=MODAL_TIMEOUT_MS)
            page.wait_for_selector(SUBMIT_BUTTON.query, timeout=MODAL_TIMEOUT_MS)
        except Exception as error:
            # Includes NavigatedElsewhereError, which is the interesting one: a
            # page that is not the vacancy asked for is a finding, not a crash,
            # and the report has to say which page it actually got.
            screenshot_on_error(page, f"probe-{vacancy_id}")
            return _failed(url, vacancy_id, already_applied, error, log)

        opened = page.content()
        modal_text = _modal_text(page)
        controls_before = _form_controls(page)

        # The second click, and the only reason this stage changed: the letter
        # field is revealed by this button and has never been seen. Its absence
        # is not fatal — the dump below records whatever is actually there.
        letter_error: str | None = None
        try:
            page.locator(ADD_COVER_LETTER.query).first.click(timeout=LETTER_TIMEOUT_MS)
            page.wait_for_selector(f"{RESPONSE_FORM.query} textarea", timeout=LETTER_TIMEOUT_MS)
        except Exception as error:
            # Recorded, not swallowed: a textarea that never appeared is a
            # finding about the page, and the dump that follows still runs.
            letter_error = f"{type(error).__name__}: {error}"
        with_letter = page.content()
        controls_after = _form_controls(page)

    return {
        "url": url,
        "vacancy_id": vacancy_id,
        "labelled_already_applied": already_applied,
        "stage": "open-form",
        # Measured, not claimed. agent/selectors.py refuses evidence from a run
        # this came back false for, because the response form is only visible
        # under an account and a hand-typed scope proves nothing.
        "authenticated": looks_authenticated(read_state(opened) or {}),
        "data_qa_after_click": _collect_data_qa(opened),
        "data_qa_after_letter_click": _collect_data_qa(with_letter),
        "selectors_ready_to_paste": [
            f'[data-qa="{name}"]' for name in _collect_data_qa(with_letter)["candidates"]
        ],
        # The whole point of the second click. Whatever is in here is the answer
        # to "what is the cover letter field", and it is an observation.
        "form_controls_before_letter_click": controls_before,
        "form_controls_after_letter_click": controls_after,
        "letter_field_wait": letter_error or "textarea appeared",
        # hh's own words in the modal. One data-qa carries several meanings and
        # they can only be told apart by their text.
        "modal_text": modal_text,
        # This vacancy's own requests, which were allowed through.
        "requests_allowed": log.allowed,
        # Requests naming some other vacancy.
        "requests_blocked": log.intercepted,
        # Everything that was not a GET, which is where a send would have been.
        "requests_blocked_non_get": log.blocked_non_get,
        # If this is non-empty the interception is not total and nothing this
        # package claims about consent holds. It is the loudest line in the report.
        "requests_escaped_interception": log.escapes(),
    }


def _modal_text(page: Any) -> str:
    """The modal's own words, or why they could not be read.

    ``Any`` for the page, as everywhere here. Recorded because
    ``hidden-resume-warning`` carries two different messages under the same
    name — hh's notice about the resume's visibility and its prediction that
    this application may be turned down — and the only thing that separates them
    is this text.

    Corrected 2026-09-07: this said "a hard refusal and a soft prediction".
    Neither of them refuses anything. The first was treated as a refusal on a
    guess, was measured that day not to be one, and the measurement is in
    ``agent/evidence/20260907-send-under-visibility-notice.json``. Nothing about
    this function changed — it records the text and judges none of it, which is
    why it needed a corrected sentence rather than a corrected line of code.
    """
    try:
        text = page.locator(RESPONSE_FORM.query).first.inner_text()
    except Exception as error:
        return f"<не прочитано: {type(error).__name__}: {error}>"
    return str(text)


def _failed(
    url: str,
    vacancy_id: str,
    already_applied: bool,
    error: Exception,
    log: RequestLog,
) -> dict[str, Any]:
    """A report for a run that never reached the form."""
    return {
        "url": url,
        "vacancy_id": vacancy_id,
        "labelled_already_applied": already_applied,
        "stage": "open-form",
        # Unknown is recorded as not authenticated: the evidence check must
        # never accept a run that could not say.
        "authenticated": False,
        "error": f"{type(error).__name__}: {error}",
        "requests_allowed": log.allowed,
        "requests_blocked": log.intercepted,
        "requests_blocked_non_get": log.blocked_non_get,
        "requests_escaped_interception": log.escapes(),
    }


def write_report(report: dict[str, Any]) -> tuple[Path, Path]:
    """Write both halves of a run and return where they went.

    Two files, because they have different audiences. The full report stays in
    the gitignored ``agent/probe/`` directory: it carries this vacancy's id, its
    URL and the owner's own application state for it. The redacted evidence file
    carries only what ``agent/selectors.py`` reads, and it is committable — which
    is the only way a selector can be verified anywhere but the owner's laptop.
    """
    directory = _run_dir()
    full = directory / PROBE_FILENAME
    full.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    evidence = EVIDENCE_DIR / f"{directory.name}.json"
    evidence.write_text(
        json.dumps(evidence_for(report), ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return full, evidence


def main() -> int:
    """Run one stage against one URL and write the evidence."""
    parser = argparse.ArgumentParser(description="Stage 0 reconnaissance for the hh agent")
    parser.add_argument("--url", required=True, help="ссылка на вакансию")
    parser.add_argument("--stage", choices=("inspect", "open-form"), default="inspect")
    parser.add_argument(
        "--already-applied",
        action="store_true",
        help="пометка: на эту вакансию отклик уже есть (только для отчёта)",
    )
    args = parser.parse_args()

    report = (
        inspect(args.url, already_applied=args.already_applied)
        if args.stage == "inspect"
        else open_form(args.url, already_applied=args.already_applied)
    )

    full, evidence = write_report(report)
    print(f"Полный отчёт: {full}")
    print(f"Доказательство для agent/selectors.py: {evidence}")
    print(f"Имя для поля evidence: {evidence.stem}")

    escaped = report.get("requests_escaped_interception") or []
    if escaped:
        print(
            "\nВНИМАНИЕ: запрос отклика прошёл мимо перехватчика:\n  "
            + "\n  ".join(escaped[:3])
            + "\nЗначит, перехват покрывает не все пути наружу, и на него нельзя\n"
            "опираться. Не запускайте отправку, пока это не выяснено."
        )
    return 0


if __name__ == "__main__":  # pragma: no cover - a console entry point
    raise SystemExit(main())
