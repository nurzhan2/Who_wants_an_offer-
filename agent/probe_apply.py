"""Stage 0: look at the real apply form and write down what is there.

The brief makes this blocking, and it is right to: the selectors for the apply
button and the letter field are not known, hh uses ``data-qa`` attributes so a
guess feels safe, and a guess that happens to match something clicks an unknown
control on somebody's live account. Nothing in this package that touches the
apply flow will start until this has been run — see
``agent/selectors.py::assert_ready_to_apply``.

Two stages, because they carry different risks.

``--stage inspect`` never clicks anything. It reads the page, dumps every
``data-qa`` that could plausibly belong to an application, and dumps
``applicantVacancyResponseStatuses`` for the vacancy. Run this on all four
targets. It answers most of the open questions and it cannot send anything.

``--stage open-form`` clicks «Откликнуться», because the submit control and the
letter field live on the other side of that click and there is no way to see
them without it.

**And that click is the one genuinely dangerous thing in this package.** Two
measured facts collide. The apply control is
``<a href="/applicant/vacancy_response?vacancyId=…">``, so following it is a GET
document navigation — a guard that blocks "writes" by looking at the HTTP method
does not touch it. And ``context.route`` cannot see a request issued from a
service worker or ``navigator.sendBeacon``, so no interception is absolute. If
hh's response flow ever completes on that first GET, that click sends an
application the owner did not confirm.

Nobody knows whether it does. The brief's instruction for exactly this situation
is not to route around it: «Если какая-то из этих границ мешает выполнить
задачу — не обходить её, а остановиться и написать об этом в отчёте».

So ``--stage open-form`` **refuses any target not labelled already_applied**.
On a vacancy the owner has already applied to, a stray submit is a no-op, and
the form is still there to be read. That is a structural refusal in
:func:`open_form`, not a warning in a docstring. If the form on an
already-applied vacancy turns out to differ so much that it teaches nothing
about the fresh one, the honest answer is in the report: this design cannot
learn the submit selector without risking one unconfirmed application, and that
is the owner's decision to make, not this program's.

**The interceptor here refuses other vacancies, not this one.** The first
version aborted every application-shaped request, and that included the
navigation the apply link itself performs: the click landed on a Chromium error
page, ``data_qa_after_click`` came back empty, and the procedure the README
documents for unblocking the package could not be completed by anyone. It read
like a second safety net and was in fact a hole in the floor.

So the refusal above is the safety, and this is what it leaves: requests naming
this one vacancy proceed, requests naming any other are aborted and reported,
and a separate ``page.on("request")`` recorder shouts if anything reached an
application URL without passing the interceptor at all — which is what a service
worker would look like.

    uv run python -m agent.probe_apply --stage inspect  --url https://hh.kz/vacancy/123
    uv run python -m agent.probe_apply --stage open-form --url ... --already-applied
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
from agent.selectors import PROBE_DIR, PROBE_FILENAME
from agent.session import looks_authenticated
from agent.state_page import read_state, vacancy_id_from_url

#: Attributes worth reporting. Broad on purpose — a probe that only looked for
#: what we expect would confirm what we expect.
INTERESTING = re.compile(r"response|apply|negotiat|letter|submit|captcha|test|resume", re.I)

#: The «задать вопрос работодателю» widget. Eighteen of these were measured on
#: three pages and every one of them matches a search for "response". They are
#: reported in their own section so nobody mistakes one for the apply form.
DECOY = re.compile(r"^vacancy-response-question")

DATA_QA = re.compile(r'data-qa="([^"]+)"')


@final
@dataclass(slots=True)
class RequestLog:
    """Every application-shaped request, and whether the interceptor saw it."""

    #: Application-shaped requests that were refused: another vacancy's.
    intercepted: list[str] = field(default_factory=list)
    #: This vacancy's own, which were let through. See ``open_form``'s guard.
    allowed: list[str] = field(default_factory=list)
    observed: list[str] = field(default_factory=list)

    def escapes(self) -> list[str]:
        """URLs the page reported that the route handler never got."""
        seen = set(self.intercepted) | set(self.allowed)
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

    ``Any`` because the whole point is to record a shape nobody has characterised
    yet: interpreting it here would be the guess this stage exists to avoid.
    """
    statuses = state.get("applicantVacancyResponseStatuses")
    if not isinstance(statuses, dict):
        return None
    return statuses.get(str(vacancy_id))


def _run_dir() -> Path:
    """A fresh directory for this run's evidence."""
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    path = PROBE_DIR / stamp
    path.mkdir(parents=True, exist_ok=True)
    return path


def inspect(url: str, *, already_applied: bool) -> dict[str, Any]:
    """Read one vacancy page without touching anything on it."""
    vacancy_id = vacancy_id_from_url(url)
    if vacancy_id is None:
        raise SystemExit(f"Не похоже на ссылку вакансии: {url}")

    with open_browser() as context:
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded")
        content = page.content()

    state = read_state(content) or {}
    return {
        "url": url,
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
    """Click «Откликнуться» and record what appears. Only on an applied vacancy.

    The refusal below is the whole safety of this function; see the module
    docstring for why a method-based guard and a route interceptor are both
    insufficient on their own.
    """
    if not already_applied:
        raise SystemExit(
            "Этот этап кликает «Откликнуться», а гарантировать, что hh не оформит\n"
            "отклик прямо на этом переходе, нельзя: ссылка — обычный GET, а\n"
            "перехватчик запросов не видит service worker. Поэтому кликаем только\n"
            "по вакансии, на которую отклик УЖЕ отправлен — там случайная отправка\n"
            "ничего не меняет.\n\n"
            "Запустите с --already-applied и ссылкой на такую вакансию.\n"
            "Если формы на ней недостаточно, чтобы понять селекторы, — это\n"
            "написано в отчёте, и решение рисковать одним неподтверждённым\n"
            "откликом принимает владелец аккаунта, а не эта программа."
        )

    vacancy_id = vacancy_id_from_url(url)
    if vacancy_id is None:
        raise SystemExit(f"Не похоже на ссылку вакансии: {url}")

    log = RequestLog()
    from agent.selectors import APPLY_LINK

    with open_browser() as context:

        def guard(route: Any) -> None:
            """Let this vacancy's own response flow through; refuse every other.

            The first version of this aborted *every* application-shaped
            request, which included the navigation the apply link itself
            performs — so the click landed on a Chromium error page, the report
            came back with no candidates at all, and stage 0 could not be
            completed. The whole package was unblockable by its own documented
            procedure.

            What carries the safety here is the ``--already-applied`` refusal
            above, not this interceptor. The owner has said this vacancy already
            has an application, so hh completing one on the navigation changes
            nothing. Requests naming any *other* vacancy are still refused, and
            everything is still recorded.
            """
            request_url = route.request.url
            if not looks_like_an_application(request_url):
                route.continue_()
                return
            strangers = vacancy_ids_in(request_url, post_body(route.request)) - {vacancy_id}
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
        page.goto(url, wait_until="domcontentloaded")
        try:
            page.click(APPLY_LINK.query, timeout=10_000)
            page.wait_for_timeout(2_000)
        except Exception as exc:
            screenshot_on_error(page, f"probe-{vacancy_id}")
            return {
                "url": url,
                "vacancy_id": vacancy_id,
                "stage": "open-form",
                "authenticated": False,
                "error": f"{type(exc).__name__}: {exc}",
                "requests_allowed": log.allowed,
                "requests_blocked": log.intercepted,
                "requests_escaped_interception": log.escapes(),
            }
        content = page.content()

    return {
        "url": url,
        "vacancy_id": vacancy_id,
        "labelled_already_applied": True,
        "stage": "open-form",
        # Measured, not claimed. agent/selectors.py refuses evidence from a run
        # this came back false for, because the response form is only visible
        # under an account and a hand-typed scope proves nothing.
        "authenticated": looks_authenticated(read_state(content) or {}),
        "data_qa_after_click": _collect_data_qa(content),
        "selectors_ready_to_paste": [
            f'[data-qa="{name}"]' for name in _collect_data_qa(content)["candidates"]
        ],
        # This vacancy's own response requests, which were allowed through. If
        # the list is non-empty the response really is a request we can
        # recognise, which is what agent/gate.py relies on.
        "requests_allowed": log.allowed,
        # Requests naming some other vacancy. Should be empty.
        "requests_blocked": log.intercepted,
        # If this is non-empty the interception is not total and nothing this
        # package claims about consent holds. It is the loudest line in the report.
        "requests_escaped_interception": log.escapes(),
    }


def main() -> int:
    """Run one stage against one URL and write the evidence."""
    parser = argparse.ArgumentParser(description="Stage 0 reconnaissance for the hh agent")
    parser.add_argument("--url", required=True, help="ссылка на вакансию")
    parser.add_argument("--stage", choices=("inspect", "open-form"), default="inspect")
    parser.add_argument(
        "--already-applied",
        action="store_true",
        help="эта вакансия уже с откликом (обязательно для --stage open-form)",
    )
    args = parser.parse_args()

    report = (
        inspect(args.url, already_applied=args.already_applied)
        if args.stage == "inspect"
        else open_form(args.url, already_applied=args.already_applied)
    )

    directory = _run_dir()
    path = directory / PROBE_FILENAME
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    escaped = report.get("requests_escaped_interception") or []
    print(f"Записано в {path}")
    print(f"Каталог прогона для agent/selectors.py: {directory.name}")
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
