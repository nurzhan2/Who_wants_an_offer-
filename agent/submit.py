"""Sending one application, under a mandate, behind a gate.

The order below is the safety property, and every line of it earns its place:

1. **Refuse what cannot be typed before anything opens.** An application whose
   letter has to be typed into a field nobody has ever seen is one vacancy for
   the owner, decided before a page load is spent on it.
2. **Open the page the human was shown**, through ``agent.hosts.open_hh_page``.
   The URL comes from the mandate, so it is the regional host the confirmation
   card displayed. That helper survives hh's regional redirect, refuses a
   lookalike host, and refuses a landing page that is not this vacancy — the
   three ways a raw ``page.goto`` reads somebody else's page as this one.
3. **Read its state again and re-decide.** The queue is a snapshot from a crawl
   that may be hours old: a vacancy can close, be archived, grow an employer
   test, or be applied to from the phone in between. Everything the queue stage
   could only guess at is decided here, by ``prefilter.decide``, against the
   page.
4. **Treat "cannot tell" as a stop.** This is the pre-click idempotency check,
   done against the site rather than against the local journal because the
   journal is allowed to be behind and the site is not.
5. **Arm the gate, then open the form.** The armed window covers the whole
   flow. It has to: the apply control is a link to an application-shaped URL, so
   arming only around the final click meant the gate aborted the navigation that
   reveals the form, and nothing could ever be sent.
6. **Use whichever apply control this page carries.** Measured 2026-09-06: a
   vacancy already applied to has no «Откликнуться» at all, only «Отклик другим
   резюме». Waiting for the first on a page that has the second is waiting for
   something that will never appear.
7. **Read the open form and keep what it says.** hh puts its own advice about
   this application in the modal, and the vacancy page never carries it. The
   reading is by text, in ``agent.state_page.read_form_warnings``, because the
   one warning element carries several different messages. It decides nothing —
   see below.
8. **Type the letter from the mandate**, never from the queue. There is no
   other string in scope, which is the point of the mandate carrying it.
9. **Read the form again after typing**, because step 7 read a card the letter
   has since changed, and everything hh said about this application while its
   form was open belongs in the record of it. See :func:`_everything_hh_said`.
10. **Confirm from the page that it went**, and only then let the caller write
    ``sent``. Only hh can say an application exists — and as of 2026-09-07 the
    gate no longer offers a second opinion, because it never had one. It used to
    raise here when the submit click put no request it recognised on the wire,
    which required knowing the shape of the request that click emits, which
    nobody has ever recorded: the only run that clicked submit
    (``agent/probe/_cdp_send.json``) kept four numbers and no traffic. A guess in
    that position fails on the far side of the irreversible act — a successful
    application recorded ``failed``, and a person invited to send it a second
    time. It is now written down instead of judged; the page re-read below is
    the answer, and it already tells "sent" from "cannot tell".

**No sentence in the modal stops an application, as of 2026-09-07, and that is
the change this file most needs its reader to know.** Until then step 7 raised
``RefusedByHHError`` on hh's «поменяйте видимость резюме…» line, and since that
line stands on every vacancy while the owner's resume carries that setting, the
effect was total: this package could not send anything at all. Measured the same
day in real Chrome on the owner's account — the notice was on the card, the
submit button was clicked, ``negotiations.total`` went 0 -> 1 and the control
became ``vacancy-response-link-top-again``. The rule came from a guess in a
brief and stood for a day because the block forbade the experiment that refutes
it; ``agent/state_page.py`` carries that lesson beside the words it matches.
What stops an application now is only ever a checkable fact, all of them read in
step 3: an application that already exists, an archived or closed vacancy, an
employer's test, a required letter that is missing.

**Waiting for the modal still means waiting for its contents.** Corrected
2026-09-07 as well, for a reason that survives the change above. Step 7 used to
wait for ``selectors.RESPONSE_FORM`` — the overlay, the frame around the card —
and read its ``inner_text()`` the moment it attached. The card itself arrives
separately, on ``GET /applicant/vacancy_response/popup?vacancyId=…``; that
request is recorded in ``agent/probe/form_136131345.json``. So there is a window
in which the overlay is up and the card is not there yet, and everything read in
that window is empty. That is no longer a way to send under a refusal — there is
no refusal — but it is still a way to click a control on a form nobody has seen
render, and to lose hh's advice about an application that then goes out without
it. Both waits are kept, and :func:`_read_the_form` still requires the submit
control's own label to be inside the text it read. That check is about whether
the card is on screen, not about what the card says, which is why it survived a
change that deleted every rule of the second kind.

**Idempotency is a number, before and after** — ``negotiations.total`` from
``applicantVacancyResponseStatuses``. Not the presence of an apply button, which
is there on vacancies that already have an application, and not the key spelled
``alreadyApplied``, which was measured ``false`` on one that had one. See
``agent/state_page.py``, which owns that reading.

**The two things that can say "applied" are told apart before the send and not
after it, and the asymmetry is deliberate.** ``Negotiations.exists`` is true on
either a ``total`` of one or more — measured, twice, on 2026-09-06 — or a
non-empty ``topicList`` beside a ``total`` of zero, which its own docstring says
nobody has measured. Both stop the agent, and that is right. But before
2026-09-07 they stopped it with the same sentence, «отклик уже отправлен», so a
person reading the journal could not tell a vacancy hh says was applied to from
a vacancy where hh contradicted itself and this code chose the cautious reading.
That is a statistic presented as a measurement. Step 4 now separates them: the
measured count skips the vacancy, and the disagreement goes to a person with
:data:`CONTRADICTORY_COUNT`, which says what hh actually answered. It is routed
to a person rather than skipped because a skip is terminal — ``--requeue`` moves
``needs_manual`` and ``failed`` and nothing else — and burning a vacancy forever
on an unmeasured shape is a decision this code is not entitled to make.

After the send the same disagreement is *believed*, in
:func:`_confirm_the_application_exists`, and that is not an inconsistency. There
the question is no longer "may we send" but "did the thing we just sent arrive",
the request has already left the browser, and any trace hh keeps of this vacancy
is enough to say so. Telling somebody nothing happened is how a vacancy gets
applied to twice.

**The confirmation re-opens the page rather than re-reading the open one.** hh
boots its frontend from a JSON blob baked into the document, so an in-page
action does not change it: asking ``page.content()`` again straight after the
submit click re-reads the same numbers the page was served with, which would
report every successful application as unconfirmed. One extra page load per
application is the price of the sentence "believe the site, not the click"
meaning something.

**A challenge is decided by where the browser ended up, not by the words on the
page.** That correction is the reason this module was rewritten; see
:func:`looks_like_a_challenge`. It is never solved, never worked around: the
window comes forward and a person deals with it.
"""

from typing import Any, Final, final
from urllib.parse import urlsplit

from agent import prefilter, selectors
from agent.gate import SubmitGate, vacancy_ids_in
from agent.hosts import NavigatedElsewhereError, open_hh_page
from agent.mandate import SendMandate
from agent.prefilter import Verdict
from agent.state_page import FormWarnings, read_form_warnings, read_negotiations, read_state


@final
class CaptchaPresentedError(Exception):
    """hh is challenging us rather than serving the page. Never solved."""


@final
class AlreadyAppliedError(Exception):
    """hh says this application already exists. Nothing to do."""


@final
class IdempotencyUnknownError(Exception):
    """hh did not say whether we have applied. A person decides."""


@final
class WrongVacancyError(Exception):
    """The page in front of us is not the one the mandate is for."""


@final
class LetterNotTypedError(Exception):
    """The letter field never took the letter, so nothing was sent.

    A conclusion about one vacancy, like the errors above it: the form is open,
    the submit button has not been touched, and a person can send this one by
    hand. Raised when the field appeared but never became editable, or when what
    it holds after typing is not the letter the owner confirmed.
    """


@final
class FormUnreadableError(Exception):
    """The response modal opened and its card could not be read. Nothing is sent.

    Deliberately its own class and deliberately **not** in the list ``run.py``
    catches by name. Everything on that list is a conclusion about one vacancy —
    somebody already applied, hh will not say whether they did, a person has to
    look — and each is recorded as a sentence and moved on from. This is not a
    conclusion about a vacancy at all: it says the page did not render what it
    was asked for, which is a fact about hh or about the browser. So it falls to
    ``run.py``'s general handler, which is the one that takes a screenshot before
    recording the failure, and two of these in a row stop the run so a person can
    look at the pictures. That is the correct amount of noise for "the site
    changed".

    **Kept through the 2026-09-07 change that deleted every text rule** (see the
    module docstring), because it is not one. It does not read what the card says
    and does not classify it; it asks whether the card is on screen at all, from
    the rendered text of a control that was waited for. An application clicked
    through a form nobody has seen render is one nobody can describe afterwards
    — including to the owner, who is entitled to hh's advice about it.
    """


# **Removed 2026-09-07: ``RefusedByHHError``.** It was raised on the blocking
# family of form warnings, caught by name in ``agent/run.py``, and recorded as
# ``needs_manual`` with hh's sentence attached. There is no blocking family any
# more, so there is nothing left to raise it: hh's «поменяйте видимость
# резюме…» is advice and applications go out under it. The class is deleted
# rather than kept against a future refusal, because an exception nothing raises
# is a promise the next reader believes — they would find it in ``run.py``'s
# handler list, see a refusal being caught, and conclude the form can still stop
# a send. If hh ever does refuse in the modal, the way to find that out is a
# measurement, and the class costs nothing to write again once there is one.


#: Where hh sends a visitor once it has decided they are a robot. Measured on
#: 2026-09-06 by this project's crawler: a plain ``GET /vacancy/<id>`` with no
#: query string was answered ``302`` to ``/account/captcha?backurl=…&state=…``.
#: The agent's equivalent signal is the address the browser ended up on, which
#: is a thing that can be pointed at rather than inferred from prose.
CHALLENGE_PATHS: Final[tuple[str, ...]] = ("/account/captcha",)

#: How long the response modal may take to render. It appears later than two
#: seconds after the click — measured — so this is a timeout on a selector and
#: never a sleep that photographs the page before the form exists. Spent twice
#: over in :func:`_open_the_form`, once on the frame and once on the card inside
#: it, because those are two different arrivals; see the module docstring.
FORM_TIMEOUT_MS: Final[int] = 15_000

#: The one deliberate pause in this module, and the narrowest one available: it
#: sits between the irreversible click and the question "did that click put a
#: request on the wire". There is nothing to wait *for* — what the modal turns
#: into after a successful send has never been measured, and waiting on a
#: guessed success marker would be the failure this package exists to prevent.
SEND_SETTLE_MS: Final[int] = 2_000

#: How often the letter field is asked whether it takes input yet. Its deadline
#: is :data:`FORM_TIMEOUT_MS`, the same as every other wait on the modal.
EDITABLE_POLL_MS: Final[int] = 100

#: Said when the application may well have gone out and hh would not confirm it.
#: Deliberately not «отклик не отправлен»: the request left the browser, and
#: telling somebody nothing happened is how a vacancy gets applied to twice.
UNCONFIRMED = (
    "hh не подтвердил отклик после отправки. Запрос ушёл, так что отклик мог "
    "быть создан — проверьте вакансию руками, прежде чем отправлять снова."
)

#: Said when hh answers the idempotency question two ways at once: no
#: applications on this vacancy, and a conversation about this vacancy, in the
#: same payload. Nobody has measured what that means — see
#: :attr:`agent.state_page.Negotiations.exists` — so it is neither reported as
#: an application («отклик уже отправлен» would be a claim about something that
#: may not exist) nor sent under. It names both halves, because the next person
#: to meet this shape is the one who can measure it, and a sentence that says
#: only "hh contradicted itself" tells them nothing they can look up.
CONTRADICTORY_COUNT = (
    "hh отвечает про вакансию {vacancy_id} двумя способами сразу: откликов — 0, "
    "но переписка по этой вакансии у hh есть ({count}). Что это значит, никто не "
    "проверял, поэтому агент не отправляет и не записывает отклик как уже "
    "сделанный. Откройте вакансию сами и посмотрите."
)

#: Said when the modal was open and what was read out of it does not look like
#: hh's card. Nothing has been sent at this point, and the sentence says so
#: first: the previous message a person saw in this position was about a request
#: that had already left, and confusing the two is how a vacancy gets applied to
#: twice. It names the vacancy because the run prints one line per vacancy.
UNREADABLE_FORM = (
    "Форма отклика на вакансию {vacancy_id} открылась, но прочитать её не "
    "удалось: в тексте нет даже надписи на кнопке отправки. Ничего не "
    "отправлено. Возможно, hh не успел отрисовать карточку или изменил её — "
    "откройте вакансию руками и посмотрите, что там написано."
)


def looks_like_a_challenge(url: str) -> bool:
    """Whether this address is hh checking for a robot rather than serving a page.

    **Decided by the address, never by the body, and that is a fix rather than
    a preference.** This used to substring-match ``captcha`` against the whole
    rendered page, and measured against a real 1.18 MB hh page captured from the
    owner's signed-in session it returned ``True``: hh ships its own error
    dictionary inside every page, and one of the entries is keyed
    ``error.signup.captcha.invalid``. So every vacancy raised before the form
    was ever touched, the window was brought to the front, and nothing could be
    sent at all. The Russian marker beside it — «подтвердите, что вы не робот» —
    is *also* in that dictionary as the value of the same entry, and missed it
    only because hh writes «не робот» with a non-breaking space. A body test
    here has two ways to be wrong and no measurement behind either.

    What is left is a signal with a measurement behind it, and it is checked
    like a path rather than like a substring: ``/accountancy`` is not
    ``/account``. Path matching is what the crawler side does too, for the same
    reason and against the same host.

    **The gap, written down rather than papered over.** A challenge served in
    the body of a ``200`` at the vacancy's own address would not be recognised
    here, and nobody has ever been served one to measure. It is not guessed at,
    because a guessed marker is what produced the failure above. What happens
    instead is safe and self-correcting: the response modal never appears, the
    wait times out, the vacancy is recorded as a failure with a screenshot, and
    two of those in a row stop the run.
    """
    path = urlsplit(url).path.rstrip("/")
    return any(path == prefix or path.startswith(f"{prefix}/") for prefix in CHALLENGE_PATHS)


def submit(page: Any, mandate: SendMandate, gate: SubmitGate) -> FormWarnings:
    """Send exactly the application this mandate authorises, or raise.

    ``Any`` for the page because typing it would mean importing playwright at
    module scope, and the rest of this package is deliberately importable — and
    testable — on a machine with no browser.

    Returns everything hh said while the form was open, so the caller can
    persist it and show it to a person — both readings of the card merged, since
    the letter is typed between them. Nothing in it is a refusal any more, and
    both families are worth carrying for different reasons. «Такой отклик может
    получить отказ» names a specific unmet requirement — the measured one is an
    English level below what the employer asked for — which is more precise than
    any score this project computes. «поменяйте видимость резюме…» is about the
    resume rather than about this vacancy, so one of them is a statement about
    every application in the batch, and the owner has to be told that rather than
    have it end up in a log line nobody reads.
    """
    selectors.assert_ready_to_apply()
    if mandate.letter is not None:
        # Before anything opens. The refusal names the probe step that closes
        # the gap, and it is a subclass of the stage-0 error narrow enough for
        # the caller to route one vacancy to the owner without stopping the run.
        selectors.assert_letter_field_known()

    state = _open_the_vacancy(page, mandate)

    # The page stage of the prefilter. The letter requirement, the employer's
    # test and the idempotency reading all live here and none of them is
    # knowable from the queue.
    status = prefilter.read_status(state)
    negotiations = read_negotiations(state, mandate.vacancy_id)
    if negotiations is not None and negotiations.total == 0 and negotiations.applications:
        # hh's two answers disagree. ``Negotiations.exists`` resolves that toward
        # stopping, which is right, but it resolves it *silently* — the vacancy
        # would be filed under the same sentence as one hh plainly says was
        # applied to. Asked before ``decide`` because ``decide`` asks
        # ``already_applied`` first, so this keeps hh's own order of precedence.
        raise IdempotencyUnknownError(
            CONTRADICTORY_COUNT.format(
                vacancy_id=mandate.vacancy_id, count=len(negotiations.applications)
            )
        )
    decision = prefilter.decide(
        facts=prefilter.read(state, mandate.vacancy_id),
        closed_for_applicants=status.closed_for_applicants,
        archived=status.archived,
        # A count, not a flag and not an element. ``None`` when the shape was
        # not one this code recognises, which is a stop and not a zero.
        # Equivalent to ``total >= 1`` by the time this line runs — the other
        # thing ``exists`` answers true on was raised above — and written as
        # ``exists`` anyway, so that a rule added to it later still lands here.
        already_applied=None if negotiations is None else negotiations.exists,
        has_letter=mandate.letter is not None,
        letter_field_known=selectors.letter_field_is_known(),
    )
    if decision.verdict is Verdict.SKIP:
        raise AlreadyAppliedError(decision.reason)
    if decision.verdict is not Verdict.PROCEED:
        raise IdempotencyUnknownError(decision.reason)

    # Everything from here on is inside the window the human opened. It has to
    # cover the apply link too: following it is a request to an application URL,
    # and a gate armed only around the final click aborts it.
    with gate.armed(mandate):
        warnings = _open_the_form(page, mandate)
        if mandate.letter is not None:
            _type_the_letter(page, mandate.letter)
            # The card was read before any of that happened, and hh answers what
            # is typed. See :func:`_everything_hh_said` for why both readings are
            # kept rather than the later one replacing the earlier.
            warnings = _everything_hh_said(warnings, _read_the_form(page, mandate))

        # The irreversible step. What the gate saw of it is written down and
        # nothing is concluded from it — see :meth:`SubmitGate.note_submit_click`
        # and step 10 above. Until 2026-09-07 this line raised when the click had
        # put no request the gate recognised on the wire, and since nobody has
        # ever recorded what request hh's «Откликнуться» emits, that was a guess
        # able to report a **successful** application as a failure and invite the
        # owner to send it again. The button's own disabled state says nothing
        # either: it was measured staying enabled under hh's visibility notice,
        # on a form that then accepted the application.
        mark = gate.mark()
        page.click(selectors.SUBMIT_BUTTON.query)
        page.wait_for_timeout(SEND_SETTLE_MS)
        gate.note_submit_click(mandate, since=mark)

    _confirm_the_application_exists(page, mandate)
    return warnings


def _type_the_letter(page: Any, letter: str) -> None:
    """Reveal the letter field, wait until it takes input, type, and check it took.

    Three waits, because the field arrives in stages. It is not on the form until
    «Добавить сопроводительное» is clicked (measured 2026-09-16, see
    ``selectors.LETTER_FIELD``); once present it can still be part of a modal
    that is finishing its render, and typing into it then is a letter that
    silently does not arrive. So presence is waited for first, then
    editability, and after typing the field is read back: an application sent
    with a half-typed or empty letter is still an application, and it cannot be
    taken back.

    ``Any`` for the page, as everywhere in this module.
    """
    query = selectors.LETTER_FIELD.query
    page.click(selectors.ADD_COVER_LETTER.query)
    page.wait_for_selector(query, state="visible", timeout=FORM_TIMEOUT_MS)
    waited = 0
    while not page.is_editable(query):
        if waited >= FORM_TIMEOUT_MS:
            raise LetterNotTypedError(
                f"поле письма появилось, но за {FORM_TIMEOUT_MS // 1000} с так и не "
                "стало доступно для ввода — отклик не отправлен, отправьте его руками"
            )
        page.wait_for_timeout(EDITABLE_POLL_MS)
        waited += EDITABLE_POLL_MS
    page.fill(query, letter)
    typed = page.input_value(query)
    if typed != letter:
        raise LetterNotTypedError(
            f"в поле письма оказалось {len(typed)} знаков вместо {len(letter)} — "
            "отклик не отправлен, отправьте его руками"
        )


def _open_the_vacancy(page: Any, mandate: SendMandate) -> dict[str, Any]:
    """Open the page the human was shown and hand back hh's own state for it.

    ``Any`` for the page as above, and for the state because it is hh's whole
    boot payload — dozens of unrelated keys — every one of which is validated
    where it is read rather than here.

    A navigation that ends somewhere else is classified rather than reported as
    a generic failure: hh's captcha is a redirect, so the address it landed on
    is the difference between "stop, a person is needed" and "this is not the
    vacancy we meant".
    """
    try:
        open_hh_page(page, mandate.url, expect_vacancy=mandate.vacancy_id)
    except NavigatedElsewhereError as error:
        landed = str(page.url)
        if looks_like_a_challenge(landed):
            page.bring_to_front()
            raise CaptchaPresentedError(
                "hh показывает проверку на робота вместо вакансии "
                f"{mandate.vacancy_id}: {landed}\n"
                "Агент её не решает — окно поднято, разберитесь руками и "
                "запустите прогон заново."
            ) from error
        raise WrongVacancyError(str(error)) from error

    state = read_state(page.content())
    if state is None:
        raise IdempotencyUnknownError(
            f"не удалось прочитать состояние страницы вакансии {mandate.vacancy_id}"
        )
    return state


def _open_the_form(page: Any, mandate: SendMandate) -> FormWarnings:
    """Click the apply control, wait for the modal's card, and read what hh says.

    Reads and returns; it does not judge. Until 2026-09-07 it ended by refusing
    the whole application on one of the sentences it had just read, which was
    the block this package could never get past — see the module docstring.

    The control is whichever of the two this page carries, and which one it is
    decides nothing: hh permits a repeat application, so «Отклик другим резюме»
    is not a statement that an application exists. That question was answered
    above, from a number.

    The href is compared against the mandate because it carries the vacancy id,
    and that comparison costs nothing and catches a stale tab, a mis-scrolled
    list or a card from a "similar vacancies" block. It is compared as a set of
    ids rather than as a substring: until 2026-09-07 this asked whether the
    mandate's id appeared anywhere in the href, so a mandate for vacancy 3677312
    was satisfied by a link to vacancy 136773120, which contains it. The parser
    is ``agent.gate.vacancy_ids_in``, so this agrees with the gate that will see
    the same URL a moment later instead of having a second opinion about it, and
    an href naming two vacancies fails here rather than passing on one of them.
    """
    control = page.locator(selectors.any_apply_control()).first
    href = control.get_attribute("href") or ""
    if vacancy_ids_in(href) != frozenset({mandate.vacancy_id}):
        raise WrongVacancyError(f"кнопка отклика ведёт не на вакансию {mandate.vacancy_id}: {href}")

    control.click()
    # A selector, never a clock. The modal renders later than the two seconds an
    # earlier probe waited, which is why that run's report contained none of the
    # modal's names and the form looked as though it did not exist.
    page.wait_for_selector(selectors.RESPONSE_FORM.query, timeout=FORM_TIMEOUT_MS)
    # And then the card inside it, which arrives on its own request. Both waits
    # are kept rather than only the second: they fail with different sentences,
    # and "the modal never opened" and "the modal opened empty" send a person to
    # different places. ``agent/probe_apply.py`` waits for the same pair, on the
    # same measurement.
    page.wait_for_selector(selectors.SUBMIT_BUTTON.query, timeout=FORM_TIMEOUT_MS)

    return _read_the_form(page, mandate)


def _read_the_form(page: Any, mandate: SendMandate) -> FormWarnings:
    """The open modal's own words, or a refusal to guess at what it did not say.

    ``read_form_warnings`` cannot tell "hh said nothing" from "there was no text
    to read": both come back as no warning, and the second one means the card is
    not on screen. The distinction has to be made here, where the page is, and it
    is made from the page rather than from a Russian phrase hh is free to reword:
    the submit control's own label must appear in the text being read. That
    control was waited for, it lives inside the modal, and Playwright's
    ``inner_text`` returns "" for an element that is attached but not rendered —
    so a label missing from the card means the card is not up and nothing read
    out of it means anything.

    Measured: ``submit_text`` was «Откликнуться» on both modals dumped on
    2026-09-06 (``agent/probe/_warn.json``, ``agent/probe/send_136638256.json``).
    If hh ever ships a wordless button this stops instead of sending, which is
    the direction to fail in, and the run says so twice and then halts with a
    screenshot rather than failing quietly.

    ``.first`` on both locators, because Playwright's strict mode raises when a
    query matches twice — which turns a second overlay on the page into an
    exception in the middle of the flow rather than a reading of the first one.
    """
    modal = page.locator(selectors.RESPONSE_FORM.query).first.inner_text()
    label = str(page.locator(selectors.SUBMIT_BUTTON.query).first.inner_text()).strip()
    if not label or label not in modal:
        raise FormUnreadableError(UNREADABLE_FORM.format(vacancy_id=mandate.vacancy_id))
    return read_form_warnings(modal)


def _everything_hh_said(before: FormWarnings, after: FormWarnings) -> FormWarnings:
    """Both readings of one open card, kept rather than the later one replacing it.

    hh can answer the letter — a length it will not take, a policy it applies to
    the text — and it can equally drop a line it showed before the letter existed
    («может получить отказ: нет сопроводительного письма» is the obvious one).
    Neither direction is measured, so the rule is chosen by what being wrong
    costs, and since 2026-09-07 that cost is only ever a line on a card: a
    sentence hh showed at any point while this form was open is kept, and the
    worst case is that the owner reads one hh has since withdrawn. Dropping it
    instead would mean an application going out under advice nobody was given,
    which is the more expensive half.
    """
    return FormWarnings(
        visibility=after.visibility or before.visibility,
        likely_rejection=after.likely_rejection or before.likely_rejection,
    )


def _confirm_the_application_exists(page: Any, mandate: SendMandate) -> None:
    """Re-open the vacancy and require hh's own count to say an application is there.

    Re-opening rather than re-reading is the point: the boot state is baked into
    the document at load time, so the numbers in the page the modal was opened
    over are the numbers it was served with, whatever the modal has since done.

    Anything that goes wrong here is reported as "could not confirm", never as
    "not sent" — including a challenge, which at this moment would arrive as an
    unreadable page. The request has already left the browser and a caller told
    that nothing happened is a caller that offers this vacancy again tomorrow.

    ``exists`` is used whole here, unmeasured half included, and the pre-send
    check above deliberately does not. Same reason both ways round: a trace hh
    keeps of this vacancy, whatever it turns out to mean, is not a reason to tell
    the owner their application never left. See the module docstring.
    """
    try:
        open_hh_page(page, mandate.url, expect_vacancy=mandate.vacancy_id)
    except NavigatedElsewhereError as error:
        raise IdempotencyUnknownError(UNCONFIRMED) from error

    state = read_state(page.content())
    negotiations = None if state is None else read_negotiations(state, mandate.vacancy_id)
    if negotiations is None or not negotiations.exists:
        raise IdempotencyUnknownError(UNCONFIRMED)
