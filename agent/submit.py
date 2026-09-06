"""Sending one application, under a mandate, behind a gate.

This module is complete and cannot run. Every selector it touches is declared
unverified in ``agent/selectors.py``, and ``run.py`` refuses before a browser
opens. It is written out anyway rather than left as a comment, because the shape
of this function is the thing worth reviewing: what order the checks happen in,
what is re-read from the page rather than trusted from the queue, and what the
irreversible step is wrapped in. Filling in three selectors after stage 0 should
turn this on without anyone re-designing it.

The order below is the safety property, and every line of it earns its place:

1. **Open the vacancy and read its state again.** The queue is a snapshot from a
   crawl that may be hours old. A vacancy can close, be archived, or be applied
   to from the phone in between.
2. **Ask hh whether we have already applied**, and treat "cannot tell" as a
   stop. This is the brief's pre-click idempotency check, and it is done against
   the site rather than against the local journal because the journal is allowed
   to be behind and the site is not.
3. **Check the control belongs to this vacancy.** The apply link carries the
   vacancy id in its href; comparing it against the mandate costs nothing and
   catches a stale tab, a mis-scrolled list, or a "similar vacancies" card.
4. **Type the letter from the mandate**, never from the queue item. There is no
   other string in scope, which is the point of the mandate carrying it.
5. **Arm the gate and click.** Everything irreversible is inside that context
   manager, it allows exactly one application-shaped request for exactly this
   vacancy, and the mandate is spent on entry so a retry cannot reuse it.
6. **Confirm from the page that it went**, and only then write ``sent``.

A captcha anywhere in this sequence is not an error to retry. It raises, the
window is brought to the front, and a person deals with it — the brief's first
boundary, and the one place where the right behaviour is to stop and wait.
"""

from typing import Any, final

from agent import selectors
from agent.gate import SubmitGate
from agent.mandate import SendMandate
from agent.state_page import read_applied, read_state


@final
class CaptchaPresentedError(Exception):
    """hh is asking for a captcha. Never solved, never worked around."""


@final
class AlreadyAppliedError(Exception):
    """hh says this application already exists. Nothing to do."""


@final
class IdempotencyUnknownError(Exception):
    """hh did not say whether we have applied. A person decides."""


@final
class WrongVacancyError(Exception):
    """The page in front of us is not the one the mandate is for."""


#: Text that means hh is challenging us rather than serving the page. Guesses
#: until a real challenge is seen — a detector may fail open where a control may
#: not, and everything downstream still refuses to send without a mandate.
CAPTCHA_MARKERS = ("captcha", "подтвердите, что вы не робот", "recaptcha")


def looks_like_a_captcha(page_html: str) -> bool:
    """Whether this page is a challenge. Detection only; never a solver."""
    lowered = page_html.casefold()
    return any(marker in lowered for marker in CAPTCHA_MARKERS)


def submit(page: Any, mandate: SendMandate, gate: SubmitGate) -> None:
    """Send exactly the application this mandate authorises, or raise.

    ``Any`` for the page because typing it would mean importing playwright at
    module scope, and the rest of this package is deliberately importable — and
    testable — on a machine with no browser.
    """
    selectors.assert_ready_to_apply()

    page.goto(f"https://hh.kz/vacancy/{mandate.vacancy_id}", wait_until="domcontentloaded")
    content = page.content()
    if looks_like_a_captcha(content):
        page.bring_to_front()
        raise CaptchaPresentedError(
            "hh показывает капчу. Агент её не решает — окно поднято, "
            "разберитесь руками и запустите прогон заново."
        )

    state = read_state(content)
    if state is None:
        raise IdempotencyUnknownError("не удалось прочитать состояние страницы")
    applied = read_applied(state, mandate.vacancy_id)
    if applied is None:
        raise IdempotencyUnknownError("hh не сообщил, отправляли ли мы уже отклик на эту вакансию")
    if applied:
        raise AlreadyAppliedError(f"на вакансию {mandate.vacancy_id} отклик уже есть")

    link = page.locator(selectors.APPLY_LINK.query)
    href = link.get_attribute("href") or ""
    if mandate.vacancy_id not in href:
        raise WrongVacancyError(f"кнопка отклика ведёт не на вакансию {mandate.vacancy_id}: {href}")

    link.click()
    page.wait_for_selector(selectors.RESPONSE_FORM.query, timeout=15_000)

    if mandate.letter is not None:
        page.fill(selectors.LETTER_FIELD.query, mandate.letter)

    # The one irreversible step, and the only place it can happen. Outside this
    # block the gate aborts every application-shaped request.
    with gate.armed(mandate):
        page.click(selectors.SUBMIT_BUTTON.query)
        page.wait_for_timeout(2_000)
        gate.require_sent(mandate)

    # Believe the site, not the click: re-read and confirm hh agrees.
    after = read_state(page.content())
    if after is None or read_applied(after, mandate.vacancy_id) is not True:
        raise IdempotencyUnknownError(
            "после отправки hh не подтвердил отклик — проверьте вакансию руками"
        )
