"""The last thing between a click and an application: a gate on the network.

Every other guard in this package sits above the DOM — a confirmed mandate, a
checked letter, a state machine that will not let a machine confirm. This one
sits below it, and it is the only guard that does not depend on knowing which
element is the submit button.

That matters more than it sounds. An application is not sent when a particular
button is clicked; it is sent when a particular request leaves the browser. The
ways to cause that request are open-ended — a click, an ``Enter`` in a textarea,
a React handler on blur, a form that submits on navigation, a control nobody has
seen because stage 0 has not been run yet. Guarding the button means enumerating
them. Guarding the request means there is one place to be right.

So the rule is: **no request whose URL looks like an application may leave
unless a human-confirmed mandate for that exact vacancy is armed.** Anything
that does not look like one proceeds untouched — which, measured, is most of hh
but not all of it: see "It refuses more than it counts" below for what that
sentence used to claim and what it actually does.

Six things this module gets right that the obvious version does not.

**It matches on the URL, not on the verb.** The first draft of this design
aborted every non-GET during reconnaissance, on the reasoning that a write is a
POST. It is not: the apply control measured on 2026-09-06 is
``<a href="/applicant/vacancy_response?vacancyId=…">``, so following it is a GET
document navigation, and a method-based guard waves the one dangerous request
through while blocking harmless telemetry.

**Once a request is application-shaped, its vacancy is read from the whole of
it, not from the query string.** An earlier version parsed ``?vacancyId=`` and
treated "no id found" as "fine". Measured against the real class it was written
to stop, three of four cross-vacancy spellings walked through it: the id in a
JSON post body, the id in a path segment, and the id percent-encoded.
:func:`vacancy_ids_in` now unquotes and scans the URL *and* the body, and **any**
id that is not the mandate's is a refusal.

Note the order carefully, because an earlier draft of this paragraph overstated
it and a reader would have relied on the overstatement. :meth:`SubmitGate.handle`
decides whether a request is application-*shaped* from its URL alone, and lets
everything else past before the body is ever read. So the body can only ever
narrow which vacancy an already-suspect request is about; it can never make an
innocent-looking URL suspect. Widening that would mean reading the body of every
request the browser makes — including requests belonging to other tabs and other
sites — which costs more than it buys, and is not what this does.

**The window is the flow, not one request.** This module used to claim it
allowed "exactly one application-shaped request per arming", enforced by
comparing against the previously allowed URL. Both halves were wrong. The
comparison only looked at the last entry, so A, B, A passed; and the claim was
not even desirable, because the measured flow needs at least two such requests —
the GET that opens the response form, and whatever the form itself sends. A rule
that forbade the second request forbade applying at all, which is what
``submit()`` was doing before this was fixed: it clicked the apply link outside
the armed window, the gate aborted the navigation, and the form never loaded.

So the honest property, and the one enforced here, is: **every application
request happens inside a window a human opened for exactly this vacancy, no
application request repeats inside a window, and what each submit click put on
the wire is written down.** How many requests one application takes is hh's
business; whose application it is, is ours.

**An application is recognised by its path, not by a parameter.** Corrected
twice. On 2026-09-07 the broad rule — "a URL carrying ``vacancyId`` in its query
could be an application" — was measured refusing hh's own furniture: of
twenty-one requests on a page where nothing was sent
(``agent/probe/20260906-181519/probe.json``), twenty carried ``vacancyId`` and
were not applications — the ``/anatskytics`` beacon seventeen times, a blacklist
check, a feedback survey, an employer-reviews widget. That fix kept the broad
rule for refusals and only narrowed what the window counted.

On 2026-09-16 it was measured failing a whole run
(``agent/probe/20260916-124948`` and ``-125039``). hh sends ``/anatskytics`` as
POSTs that carry ``vacancyId`` in the query; the page-level recorder called
every one of them an application, and :meth:`SubmitGate.assert_no_escapes`
raised on fifteen and twenty of them. A parameter every analytics call on a
vacancy page carries says nothing about what the call does. What was measured
to be applying is the path: the apply control is
``/applicant/vacancy_response?vacancyId=…`` and the modal's card is
``/applicant/vacancy_response/popup?vacancyId=…``. So :data:`RESPONSE_PATH` is
the rule, for refusals and for the count alike, and a request under it is still
refused when anything in its URL *or body* names a different vacancy.

What this does not cover, said plainly: nobody has recorded the request hh's
«Откликнуться» itself emits (see below). If it leaves on a path outside
:data:`RESPONSE_PATH`, this gate does not see it. The click that emits it
happens only inside :func:`agent.submit.submit`, under an armed mandate, and
:meth:`SubmitGate.note_submit_click` is there to record what it was.

**It cannot tell you that an application was sent, and it no longer pretends
to.** Also 2026-09-07; see the note where ``require_progress`` used to be. The
shape of the request hh's «Откликнуться» emits has never been recorded by
anybody, so a gate that raised when it saw no such request after the click was
guessing — and the guess failed on the far side of the irreversible act, which
is the worst place in this package to be wrong. What is left is
:meth:`SubmitGate.note_submit_click`, which writes down what the gate saw and
cannot fail a run. Whether an application exists is answered where it can be
answered: ``agent/submit.py`` re-opens the vacancy and reads hh's own count.

**It knows what it cannot see.** ``context.route`` does not observe requests
issued from a service worker, and hh is a large single-page application that may
register one. So the context is launched with service workers blocked (see
``agent/browser.py``), and this module keeps an independent record from
``page.on("request")``. Anything that reached an application URL without passing
through :meth:`SubmitGate.handle` means the interception is not covering
everything, and :meth:`assert_no_escapes` raises rather than letting the run
continue on an assumption that has just been shown false.

One implementation note that is load-bearing rather than incidental: this class
must **not** be a ``slots`` dataclass. Playwright's ``wrap_handler`` caches its
wrapper by doing ``setattr(handler.__self__, "_pw_impl_instance_handle", …)`` on
the bound method's owner, so registering ``gate.handle`` as a route handler
raises ``AttributeError`` on an instance with no ``__dict__``. That failure
landed on the first line after the browser opened — i.e. immediately after the
human typed «отправляем» — and there is a test that hands a real gate to
playwright's own mapping so it cannot come back.
"""

import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Protocol, final, runtime_checkable
from urllib.parse import unquote, urlsplit

from agent.mandate import SendMandate, verify

#: Where applying happens, measured on a live page: the apply control and the
#: response form's card are both under this path. The whole rule — see "An
#: application is recognised by its path" in the module docstring.
RESPONSE_PATH = "/applicant/vacancy_response"

#: ``vacancyId=123``, ``vacancy_id: "123"``, ``"vacancyId":123`` — the spellings
#: a query string, a form body and a JSON body use for the same thing.
_NAMED_ID = re.compile(r"vacanc(?:y[ _-]?id|yid)\"?\s*[:=]\s*\"?(\d{4,})", re.IGNORECASE)
#: ``/vacancy/123`` and ``/applicant/vacancy_response/123`` — the id in a path.
_PATH_ID = re.compile(r"/vacanc(?:y|y_response)/(\d{4,})")


@runtime_checkable
class Request(Protocol):
    """The part of a Playwright request this module reads."""

    @property
    def url(self) -> str:
        """The absolute URL."""
        ...

    @property
    def method(self) -> str:
        """The HTTP verb. Recorded for the log, never used to decide."""
        ...


@runtime_checkable
class Route(Protocol):
    """The part of a Playwright route this module drives."""

    @property
    def request(self) -> Request:
        """What is being asked for."""
        ...

    def abort(self, error_code: str = ...) -> None:
        """Refuse the request."""
        ...

    def continue_(self) -> None:
        """Let it through."""
        ...


# **Removed 2026-09-07: ``UnmandatedRequestError``.** Despite the name, nothing
# unmandated ever raised it — an unmandated request is aborted, silently and by
# design, because raising in a route handler happens on playwright's thread and
# would not stop anything. Its one raiser was ``require_progress``, and that is
# gone (see :meth:`SubmitGate.note_submit_click`). The class is deleted rather
# than left standing, for the reason ``submit.py`` deleted ``RefusedByHHError``
# the same day: an exception nothing raises is a promise the next reader
# believes. Somebody would find it, conclude the gate stops a send by raising,
# and write code that waits for that.


@final
class InterceptionEscapedError(Exception):
    """A request reached an application URL without passing the interceptor.

    Means the route handler is not covering every path out of the browser —
    a service worker, a beacon, a second context — so nothing this gate says
    about the run can be trusted.
    """


def looks_like_an_application(url: str) -> bool:
    """Whether this request is on the path hh applies through.

    The path, never a query parameter: ``vacancyId`` rides on every beacon and
    widget of a vacancy page, and treating it as a sign of applying failed every
    run on 2026-09-16. See the module docstring.
    """
    return urlsplit(url).path.startswith(RESPONSE_PATH)


def is_the_application_itself(url: str) -> bool:
    """Whether the armed window counts this request.

    The same answer as :func:`looks_like_an_application` since both became a
    question about the path. Kept as its own name because the two questions
    are different — "may this leave" and "does this count as applying" — and a
    later measurement may separate them again.
    """
    return looks_like_an_application(url)


def post_body(request: Request) -> str | None:
    """This request's body, or None when it has none or will not give it up.

    Playwright raises on the body of some requests rather than returning None,
    and a gate that dies while deciding is a gate that fails open. The unreadable
    case is handled where it matters: a body we cannot read names no vacancy, and
    :meth:`SubmitGate.handle` still requires the *URL* to name this one or none.
    """
    try:
        data = getattr(request, "post_data", None)
    except Exception:  # any failure to read a body means "no body we can see"
        return None
    return data if isinstance(data, str) else None


def vacancy_ids_in(url: str, body: str | None = None) -> frozenset[str]:
    """Every vacancy id this request names, anywhere this module can see it.

    Unquoting first is what makes ``vacancyId%3D136773120`` the same string as
    ``vacancyId=136773120``; without it a percent-encoded id reads as no id at
    all, which used to mean "allowed".
    """
    found: set[str] = set()
    for raw in (url, body):
        if not raw:
            continue
        text = unquote(raw)
        found.update(_NAMED_ID.findall(text))
        found.update(_PATH_ID.findall(text))
    return frozenset(found)


def vacancy_id_in(url: str) -> str | None:
    """The single vacancy id in a URL, when it names exactly one.

    Kept for reading and for reports. Never used to decide whether to refuse:
    that needs :func:`vacancy_ids_in`, which sees more and answers with a set,
    so that "names two different vacancies" is not silently one of them.
    """
    ids = vacancy_ids_in(url)
    return next(iter(ids)) if len(ids) == 1 else None


@final
@dataclass(frozen=True, slots=True)
class GateMark:
    """Where the gate's own records stood at one instant.

    Taken immediately before the irreversible click and handed back afterwards,
    so that what is reported about that click is what happened *during* it
    rather than everything the process has done since it started. The previous
    version of this took a bare integer, which is the same idea with one of its
    two halves missing: it could say how many application requests the click
    produced but not whether the gate had refused anything while it ran.
    """

    #: How many application requests the open window had counted.
    window: int
    #: How many refusals the gate had recorded, over the whole run.
    refusals: int


@final
@dataclass(frozen=True, slots=True)
class SubmitClick:
    """What the gate saw of one press of hh's «Откликнуться». A record, not a verdict.

    Nothing decides anything from this. It exists because the shape of the
    request that click emits is the one measurement this package is missing, and
    an agent that runs on the owner's own account is the only thing in a
    position to take it. Every field is what the gate observed; none of it is an
    inference about whether an application exists.
    """

    vacancy_id: str
    #: Application URLs the click put through, in order. **Empty does not mean
    #: nothing was sent.** hh's submit may well be an XHR to a path this module
    #: does not recognise, and reading an empty tuple as failure is exactly the
    #: mistake ``require_progress`` made.
    allowed: tuple[str, ...]
    #: Refusals recorded while the click was in flight. Unlike the field above,
    #: this one *is* unambiguous: the gate aborting something in the middle of a
    #: send is the gate interfering with it, and a person should see that.
    refused: tuple[str, ...]


@final
@dataclass
class SubmitGate:
    """Refuses every application-shaped request that has no consent behind it.

    Not ``slots=True``: playwright ``setattr``s onto a bound method's owner when
    it registers a route handler. See the module docstring.
    """

    #: The mandate currently armed, if any. Never set directly; see :meth:`armed`.
    _mandate: SendMandate | None = None
    #: The application URLs allowed inside the window currently open. Reset on
    #: every arming, which is what makes "no repeats" mean "no repeats for this
    #: confirmation" rather than "no repeats since the process started".
    _window: list[str] = field(default_factory=list)
    #: Application URLs this gate refused, and why, for the run report.
    blocked: list[str] = field(default_factory=list)
    refused_because: list[str] = field(default_factory=list)
    #: Application URLs it allowed, across the whole run.
    allowed: list[str] = field(default_factory=list)
    #: Every application URL the page reported, whether or not it reached
    #: :meth:`handle`. The difference is what :meth:`assert_no_escapes` checks.
    observed: list[str] = field(default_factory=list)
    #: One entry per submit click, in order. Written by
    #: :meth:`note_submit_click` and read by nothing here: it is the record of a
    #: measurement nobody has taken, kept so that the first person to run this
    #: against hh can read the answer off a finished run instead of guessing.
    submit_clicks: list[SubmitClick] = field(default_factory=list)

    @contextmanager
    def armed(self, mandate: SendMandate) -> Iterator[None]:
        """Open the window in which this vacancy's application may be sent.

        Verifying inside the context manager rather than at the call site means
        the mandate is spent whether or not the body succeeds: a failure part
        way through submitting must not leave a reusable consent behind.

        The window covers the whole apply flow, including opening the form.
        That is not a relaxation of the rule — the form-opening request is
        itself application-shaped, it is for this vacancy, and the human
        confirmed this vacancy. Arming only around the final click, as this
        module used to, meant the gate aborted the navigation that reveals the
        form and nothing could ever be sent at all.
        """
        verify(mandate)
        self._mandate = mandate
        self._window = []
        try:
            yield
        finally:
            self._mandate = None

    def requests_in_window(self) -> int:
        """How many application requests the open window has allowed so far.

        Counts what :func:`is_the_application_itself` recognises, not everything
        :meth:`handle` let through: hh puts seventeen beacons on one vacancy page
        and a count they can move is a count that means nothing.
        """
        return len(self._window)

    def mark(self) -> GateMark:
        """Where the records stand right now, to compare against afterwards."""
        return GateMark(window=len(self._window), refusals=len(self.refused_because))

    def _refuse(self, route: Route, url: str, reason: str) -> None:
        """Record why, then abort. Every refusal goes through here."""
        self.blocked.append(url)
        self.refused_because.append(f"{url} — {reason}")
        route.abort()

    def handle(self, route: Route) -> None:
        """The ``context.route`` callback. Every request in the context passes here."""
        request = route.request
        url = request.url
        if not looks_like_an_application(url):
            route.continue_()
            return

        self.observed.append(url)
        mandate = self._mandate
        if mandate is None:
            self._refuse(route, url, "нет подтверждения на эту отправку")
            return
        strangers = vacancy_ids_in(url, post_body(request)) - {mandate.vacancy_id}
        if strangers:
            # A page left over from another vacancy, a mis-scrolled list, a
            # link in a "similar vacancies" block. Consent is for one job.
            self._refuse(route, url, f"чужие вакансии в запросе: {sorted(strangers)}")
            return
        if is_the_application_itself(url):
            if url in self._window:
                # Inside one confirmation the same URL is a retry, and a retry of
                # an application is a second application. Asked only of requests
                # that could be one: hh repeats its own beacon several times per
                # page, and this rule used to abort the repeats — real traffic,
                # aborted in the middle of a real apply flow, because the beacon
                # carried a vacancy id.
                self._refuse(route, url, "повтор запроса внутри одного подтверждения")
                return
            self._window.append(url)
        # Everything allowed goes in here, hh's furniture included, because this
        # list is what :meth:`assert_no_escapes` checks ``observed`` against. The
        # window above is the narrower record.
        self.allowed.append(url)
        route.continue_()

    def observe(self, request: Request) -> None:
        """The ``page.on("request")`` callback, kept independently of the router.

        Its only job is to disagree with :meth:`handle` when something got out
        another way.
        """
        if looks_like_an_application(request.url):
            self.observed.append(request.url)

    def assert_no_escapes(self) -> None:
        """Raise if any application request was seen that the router never handled."""
        handled = set(self.blocked) | set(self.allowed)
        escaped = [url for url in self.observed if url not in handled]
        if escaped:
            raise InterceptionEscapedError(
                "Запрос отклика прошёл мимо перехватчика — значит, перехват "
                f"покрывает не все пути наружу: {escaped[:3]}"
            )

    def note_submit_click(self, mandate: SendMandate, *, since: GateMark) -> SubmitClick:
        """Write down what the gate saw of one submit click. Never raises.

        **This replaced ``require_progress`` on 2026-09-07, and deleting a check
        was the fix rather than a shortcut past one.** That method refused —
        raised ``UnmandatedRequestError`` — when the click had put no
        application-shaped request through the router. Its rule needed to know
        what request hh's «Откликнуться» emits, and *nothing in this repository
        records that*. The only run that ever clicked submit is
        ``agent/probe/_cdp_send.json``, which kept four numbers and no traffic;
        the run that captured traffic (``agent/probe/send_136638256.json``)
        stopped before submitting, and its non-GET list is hh's beacon, its
        fingerprint endpoint and ``register_interaction``. So the rule was a
        guess, and it was a guess evaluated on the far side of the irreversible
        act: if hh's submit is an XHR to a path this module does not recognise —
        which is likely, since every hh request that *was* recorded is one — then
        after a **successful** application the raise fires, ``run.py`` records
        ``failed``, and a person is invited to send an application hh already
        holds. Applying twice is the one mistake the owner cannot undo, so a
        check that manufactures it is worse than no check.

        What replaced it is not nothing. Two lines below the old call site,
        ``submit()`` re-opens the vacancy and reads ``negotiations.total`` out of
        hh's own boot state — a measured fact, from the party that knows, and it
        already distinguishes "sent" from "cannot tell" instead of collapsing
        them into "failed". That was always the authority; the gate was a second
        opinion with nothing behind it.

        What is kept is the observation, because it is worth something the check
        never was: run this against hh under a real mandate and
        :attr:`submit_clicks` contains the shape of the request nobody has
        measured, next to the vacancy it belonged to. A check that guesses
        prevents the measurement that would settle it — which is exactly how the
        visibility-warning refusal survived a day in ``agent/state_page.py``.
        """
        click = SubmitClick(
            vacancy_id=mandate.vacancy_id,
            allowed=tuple(self._window[since.window :]),
            refused=tuple(self.refused_because[since.refusals :]),
        )
        self.submit_clicks.append(click)
        return click
