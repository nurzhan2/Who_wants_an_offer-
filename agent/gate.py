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
unless a mandate for that exact vacancy is armed.** Everything else on hh
proceeds untouched.

Three things this module gets right that the obvious version does not.

**It matches on the URL, not on the verb.** The first draft of this design
aborted every non-GET during reconnaissance, on the reasoning that a write is a
POST. It is not: the apply control measured on 2026-09-06 is
``<a href="/applicant/vacancy_response?vacancyId=…">``, so following it is a GET
document navigation, and a method-based guard waves the one dangerous request
through while blocking harmless telemetry. If hh's flow ever completes on that
GET, a method-based guard sends an application during a probe that the human
authorised only to *look* at a form.

**It fails closed on an unknown mandate.** :func:`agent.mandate.verify` is called
on arming and it spends the mandate, so an armed window cannot be re-entered, a
retry cannot reuse a confirmation, and an object that skipped its own
constructor is rejected at the point of use rather than at construction.

**It knows what it cannot see.** ``context.route`` does not observe requests
issued from a service worker, and hh is a large single-page application that may
register one. Ordering the risky operations last would make an escape
*detectable*; it would not make it impossible, and the brief is explicit that a
boundary in the way is a thing to stop at, not to route around. So the context
is launched with service workers blocked (see ``agent/browser.py``), and this
module keeps an independent record from ``page.on("request")``. Anything that
reached an application URL without passing through :meth:`SubmitGate.handle`
means the interception is not covering everything, and :meth:`assert_no_escapes`
raises rather than letting the run continue on an assumption that has just been
shown false.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Protocol, final, runtime_checkable
from urllib.parse import urlsplit

from agent.mandate import SendMandate, verify

#: What an application request looks like, measured on a live page: the response
#: flow lives under this path, and carries the vacancy id as a parameter. Either
#: is enough to refuse — a URL only has to *look* like an application for the
#: answer to be no.
RESPONSE_PATH = "/applicant/vacancy_response"
VACANCY_PARAM = "vacancyId"


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


def vacancy_id_in(url: str) -> str | None:
    """The vacancy id this URL is about, when it names one."""
    parts = urlsplit(url)
    for pair in parts.query.split("&"):
        name, _, value = pair.partition("=")
        if name == VACANCY_PARAM and value.isdigit():
            return value
    return None


@final
@dataclass(slots=True)
class SubmitGate:
    """Refuses every application-shaped request that has no consent behind it."""

    #: The mandate currently armed, if any. Never set directly; see :meth:`armed`.
    _mandate: SendMandate | None = None
    #: Application URLs this gate refused, for the run report.
    blocked: list[str] = field(default_factory=list)
    #: Application URLs it allowed. At most one per arming.
    allowed: list[str] = field(default_factory=list)
    #: Every application URL the page reported, whether or not it reached
    #: :meth:`handle`. The difference is what :meth:`assert_no_escapes` checks.
    observed: list[str] = field(default_factory=list)

    @contextmanager
    def armed(self, mandate: SendMandate) -> Iterator[None]:
        """Allow exactly one application request, for exactly this vacancy.

        Verifying inside the context manager rather than at the call site means
        the mandate is spent whether or not the body succeeds: a failure part
        way through submitting must not leave a reusable consent behind.
        """
        verify(mandate)
        self._mandate = mandate
        try:
            yield
        finally:
            self._mandate = None

    def handle(self, route: Route) -> None:
        """The ``context.route`` callback. Every request in the context passes here."""
        url = route.request.url
        if not looks_like_an_application(url):
            route.continue_()
            return

        self.observed.append(url)
        mandate = self._mandate
        if mandate is None:
            self.blocked.append(url)
            route.abort()
            return
        target = vacancy_id_in(url)
        if target is not None and target != mandate.vacancy_id:
            # A page left over from another vacancy, a mis-scrolled list, a
            # link in a "similar vacancies" block. Consent is for one job.
            self.blocked.append(url)
            route.abort()
            return
        if self.allowed and self.allowed[-1] == url:
            # One arming, one request. A framework retry is still a second
            # application.
            self.blocked.append(url)
            route.abort()
            return
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

    def require_sent(self, mandate: SendMandate) -> None:
        """Confirm that the one allowed request was the one this mandate is for."""
        if not self.allowed:
            raise UnmandatedRequestError(
                f"Отклик на вакансию {mandate.vacancy_id} не был отправлен: запроса не было"
            )
