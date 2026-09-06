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
unless a human-confirmed mandate for that exact vacancy is armed.** Everything
else on hh proceeds untouched.

Four things this module gets right that the obvious version does not.

**It matches on the URL, not on the verb.** The first draft of this design
aborted every non-GET during reconnaissance, on the reasoning that a write is a
POST. It is not: the apply control measured on 2026-09-06 is
``<a href="/applicant/vacancy_response?vacancyId=…">``, so following it is a GET
document navigation, and a method-based guard waves the one dangerous request
through while blocking harmless telemetry.

**It reads the vacancy id from the whole request, not from the query string.**
An earlier version parsed ``?vacancyId=`` and treated "no id found" as "fine".
Measured against the real class it was written to stop, three of four
cross-vacancy spellings walked through it: the id in a JSON post body, the id in
a path segment, and the id percent-encoded. :func:`vacancy_ids_in` now unquotes
and scans the URL *and* the body, and **any** id that is not the mandate's is a
refusal.

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
request repeats inside a window, and the count is reported.** How many requests
one application takes is hh's business; whose application it is, is ours.

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

#: What an application request looks like, measured on a live page: the response
#: flow lives under this path, and carries the vacancy id as a parameter. Either
#: is enough to refuse — a URL only has to *look* like an application for the
#: answer to be no.
RESPONSE_PATH = "/applicant/vacancy_response"
VACANCY_PARAM = "vacancyId"

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


@final
class UnmandatedRequestError(Exception):
    """Something tried to send an application with no consent armed for it."""


@final
class InterceptionEscapedError(Exception):
    """A request reached an application URL without passing the interceptor.

    Means the route handler is not covering every path out of the browser —
    a service worker, a beacon, a second context — so nothing this gate says
    about the run can be trusted.
    """


def looks_like_an_application(url: str) -> bool:
    """Whether this URL is the shape hh sends applications through.

    Deliberately broad. A telemetry beacon that happens to carry a vacancy id
    is refused too, and that costs nothing; the opposite mistake costs somebody
    an application they did not agree to send.
    """
    parts = urlsplit(url)
    if parts.path.startswith(RESPONSE_PATH):
        return True
    return f"{VACANCY_PARAM}=" in parts.query


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
        """How many application requests the open window has allowed so far."""
        return len(self._window)

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
        if url in self._window:
            # Inside one confirmation the same URL is a retry, and a retry of an
            # application is a second application.
            self._refuse(route, url, "повтор запроса внутри одного подтверждения")
            return
        self._window.append(url)
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

    def require_progress(self, mandate: SendMandate, *, since: int) -> None:
        """Confirm the submit click actually put a request on the wire.

        Called with the window's size taken just before the click, so it answers
        "did *that* click send something", not "has anything been sent since the
        process started" — which is what the previous version, a truthiness test
        on a list that was never reset, actually answered.

        This is a diagnostic, not the authority. Whether an application exists is
        decided by re-reading the page afterwards; see ``agent/submit.py``.
        """
        if self.requests_in_window() <= since:
            raise UnmandatedRequestError(
                f"Клик по кнопке отклика на вакансию {mandate.vacancy_id} не отправил "
                "ни одного запроса — форма изменилась, отправки не было"
            )
