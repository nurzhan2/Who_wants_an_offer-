"""Which hh host to open, and the redirect that used to kill the run.

Two problems live here because they are one problem seen twice.

**The host is data, not a constant.** ``agent/login.py`` had a city subdomain
written into it as a stopgap, which is the thing CLAUDE.md forbids by name —
«не хардкодить … город Алматы». The cities are already described, with aliases
and one marked default, in ``backend/app/sources/hh_sites.yaml``, so this reads
that file. Reading a data file is not importing the backend: nothing here
imports ``app``, and ``agent/tests/test_isolation.py`` checks that by parsing
the import graph rather than by grepping. The file can also be absent — this
package is meant to be runnable on a laptop that holds nothing else — so every
failure to read it degrades to :data:`FALLBACK_HOST` instead of raising. That
fallback is the country root rather than a city, because hh answers ``hh.kz``
by redirecting the visitor to whichever regional subdomain it thinks they
belong to, which is the one guess that is not a guess.

**And that redirect is the second problem.** Measured 2026-09-06: after the
owner signs in, hh sets a regional cookie and redirects to the city subdomain.
Playwright reports the interrupted navigation as an error — ``net::ERR_ABORTED``
— and ``page.goto`` raises, which is what stopped ``python -m agent.login`` from
ever finishing. The fix is deliberately not a try/except at each call site. It
is one helper that every navigation in this package goes through, because the
same bug returns wherever a URL arrives from outside, and in ``agent/submit.py``
the URL arrives from the queue.

Surviving the redirect is only half the job. A navigation that ends somewhere
other than the vacancy that was asked for is not a success at a different
address: it is a login wall, a captcha, an archived vacancy or a moved page,
and every one of those has to be a failure rather than a page whose content
gets read as though it were the right one.

**Corrected 2026-09-07: "still hh" used to mean "still the host we asked for".**
:func:`open_hh_page` compared the landing host with the *requested* one, which
every URL that does not redirect satisfies by definition — including
``https://hh.kz.example.com/vacancy/136773120``, whose landing host matches its
own request perfectly. The requested URL is not a constant: it arrives from a
queue file, from ``agent/session_signal.json``, or from a mandate built out of a
crawl, so "the host we asked for" is exactly the thing that needed checking.
``agent/submit.py`` meanwhile described this helper as one that "refuses a
lookalike host", which it did not do. It does now: both the requested URL and
the landing URL are checked against :func:`hh_sites`, and the requested one is
checked BEFORE the browser is sent anywhere, because a page whose content must
not be read is a page that must not be opened either.

The unit of comparison stays the base site rather than the whole hostname, and
that is the point rather than a compromise. Regional subdomains are the reason
this helper exists; hh may redirect to one the data file does not name, and an
allowlist of full hostnames would reject the redirect it was written to survive.
:func:`base_site` takes the last two labels, so ``almaty.hh.kz`` passes and
``hh.kz.example.com`` — the shape a lookalike takes — does not.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, final
from urllib.parse import urlsplit

#: The crawler's list of hh sites. A path, not an import: the two packages do
#: not share code, and this one shares a fact about the world that both of them
#: already have to agree on. Absence is normal and handled everywhere below.
SITES_PATH: Final[Path] = (
    Path(__file__).resolve().parents[1] / "backend" / "app" / "sources" / "hh_sites.yaml"
)

#: Used when the file above cannot be read. Not a city — hh redirects the bare
#: country root to a regional subdomain by itself, and :func:`open_hh_page`
#: already survives exactly that redirect, so the fallback needs no opinion
#: about where the owner lives.
FALLBACK_HOST: Final[str] = "hh.kz"

#: How long a page load may take before it is a failure rather than a wait.
DEFAULT_TIMEOUT_MS: Final[int] = 30_000

#: What Playwright says when a navigation is cut short by another navigation,
#: which is precisely what a redirect after sign-in looks like from here.
#: Matched on the message text because importing ``playwright.sync_api.Error``
#: at module scope would make this package unimportable without a browser, and
#: ``agent/browser.py`` explains at length why that must stay possible.
INTERRUPTED_NAVIGATION: Final[tuple[str, ...]] = (
    "net::ERR_ABORTED",
    "interrupted by another navigation",
)

#: The vacancy id, read from the URL **path** only. A login redirect carries the
#: address it interrupted in ``?backurl=…``, so a search over the whole URL
#: finds the id that was asked for on a page that never loaded and reports the
#: navigation as a success. ``agent/state_page.py`` searches the whole URL on
#: purpose — it is handed addresses this package built — and this is the version
#: for addresses hh handed back.
_VACANCY_IN_PATH: Final[re.Pattern[str]] = re.compile(r"^/vacancy/(\d+)")


class NavigatedElsewhereError(RuntimeError):
    """The page that loaded is not the page that was asked for."""


@final
class NotOnHhError(NavigatedElsewhereError):
    """This URL is not on one of hh's own sites. Not opened, and not read.

    A subclass, because every caller that already treats "somewhere other than
    the vacancy" as a failure is right to treat this the same way and none of
    them need editing. A class of its own, because this is the one refusal that
    can happen *before* the browser moves, and because the repair differs: "hh
    redirected us" is a question for hh, while "the queue handed us an address
    that is not hh" is a question about where that queue came from.
    """


@final
@dataclass(frozen=True, slots=True)
class Site:
    """One hh site, as ``hh_sites.yaml`` describes it."""

    host: str
    city: str
    is_default: bool = False


def load_sites(path: Path = SITES_PATH) -> tuple[Site, ...]:
    """The sites named in the crawler's data file, or nothing at all.

    Every failure — the file missing, unreadable, not YAML, YAML of the wrong
    shape, PyYAML not installed — returns an empty tuple rather than raising.
    The callers below all have a working answer without it, and an agent that
    refuses to open a browser because a file in a sibling package moved would
    be failing at the wrong thing.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return ()
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:  # pragma: no cover - PyYAML is a project dependency
        return ()
    try:
        # ``Any``: this is somebody else's document and its shape is validated
        # field by field immediately below rather than trusted.
        document: Any = yaml.safe_load(raw)
    except yaml.YAMLError:
        return ()
    if not isinstance(document, dict):
        return ()
    entries = document.get("sites")
    if not isinstance(entries, list):
        return ()

    sites: list[Site] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        host = entry.get("host")
        if not isinstance(host, str) or not host.strip():
            continue
        city = entry.get("city")
        sites.append(
            Site(
                host=host.strip(),
                city=city if isinstance(city, str) else "",
                is_default=entry.get("default") is True,
            )
        )
    return tuple(sites)


def default_host(path: Path = SITES_PATH) -> str:
    """The host to open when nothing more specific has been said.

    The entry marked ``default: true`` wins, then the first entry, then
    :data:`FALLBACK_HOST`. The order matters only because the file is somebody
    else's and may lose its default marker without anybody here noticing.
    """
    sites = load_sites(path)
    for site in sites:
        if site.is_default:
            return site.host
    return sites[0].host if sites else FALLBACK_HOST


def known_hosts(path: Path = SITES_PATH) -> frozenset[str]:
    """Every host the data file names. Empty when it cannot be read."""
    return frozenset(site.host for site in load_sites(path))


def hh_sites(path: Path = SITES_PATH) -> frozenset[str]:
    """The base sites this package will open a page on.

    Derived from the data file, so a deployment that starts crawling ``hh.ru``
    declares it there and nowhere else — the same reason the host itself is not
    a constant here.

    :data:`FALLBACK_HOST` is always added rather than used only when the file is
    missing. An allowlist that empties itself when a file in a sibling package
    moves would refuse every navigation, and a package that opens nothing is not
    safer than one that opens hh — it is broken in a way that invites somebody
    to delete the check. One site is the floor, never zero.
    """
    named = {base_site(host) for host in known_hosts(path)}
    named.add(base_site(FALLBACK_HOST))
    return frozenset(site for site in named if site)


#: Characters this module refuses to reason about in a URL, because Python and
#: the browser do not read them the same way. WHATWG — which is what Chromium
#: parses with — ends the authority of a special-scheme URL at a backslash;
#: :func:`urllib.parse.urlsplit` does not. So ``https://evil.com\@hh.kz/x``
#: reads to Python as the host ``hh.kz`` and navigates, in the browser, to
#: ``evil.com``. Measured, not theorised. A newline or a tab is stripped by the
#: browser and kept by Python, which is the same disagreement wearing a
#: different hat.
#:
#: There is no reason for any of these to appear in a vacancy URL, so the answer
#: is to refuse rather than to reimplement WHATWG here and hope the
#: reimplementation agrees.
AMBIGUOUS_IN_A_URL: Final[frozenset[str]] = frozenset({"\\", "\r", "\n", "\t"})


def on_hh(url: str, sites: frozenset[str] | None = None) -> bool:
    """Whether this is an ``https`` address on one of hh's own sites.

    The scheme is part of the question. ``http://hh.kz`` is not an address hh
    serves, and neither is ``about:blank``, which is where a page sits when a
    navigation never happened at all — accepting either would let this check
    pass on something that was never an hh page.

    A URL this module and the browser would read differently is refused before
    it is parsed at all; see :data:`AMBIGUOUS_IN_A_URL`. That matters because
    this check is the one place that does not trust the queue, and the queue is
    a file. A check that answers a different question from the one the browser
    is about to act on is not a check.
    """
    if any(bad in url for bad in AMBIGUOUS_IN_A_URL):
        return False
    parts = urlsplit(url)
    if parts.scheme != "https":
        return False
    host = (parts.hostname or "").lower()
    return bool(host) and base_site(host) in (hh_sites() if sites is None else sites)


def base_site(host: str) -> str:
    """The last two labels of a hostname: ``almaty.hh.kz`` -> ``hh.kz``.

    Used to decide whether a redirect stayed inside hh. Comparing the whole
    hostname would reject the regional redirect this module exists to survive;
    comparing a substring would accept ``hh.kz.example.com``, which is the
    shape a phishing host takes. Two labels is the right unit for these
    hostnames and it is checked by a test rather than assumed.
    """
    labels = [label for label in host.strip().lower().rstrip(".").split(".") if label]
    return ".".join(labels[-2:]) if len(labels) >= 2 else ".".join(labels)


def same_site(one: str, other: str) -> bool:
    """Whether two URLs or hostnames belong to the same hh site."""
    return base_site(_hostname(one)) == base_site(_hostname(other)) != ""


def _hostname(url_or_host: str) -> str:
    """The hostname of a URL, or the string itself when it is already one."""
    if "//" in url_or_host:
        return (urlsplit(url_or_host).hostname or "").lower()
    return url_or_host.strip().lower()


def vacancy_id_in_path(url: str) -> str | None:
    """The vacancy this URL's path names, ignoring everything after the ``?``.

    See :data:`_VACANCY_IN_PATH` for why the query string is excluded.
    """
    match = _VACANCY_IN_PATH.match(urlsplit(url).path)
    return match.group(1) if match else None


def vacancy_url(vacancy_id: str, host: str | None = None) -> str:
    """A vacancy address on the configured host."""
    return f"https://{host or default_host()}/vacancy/{vacancy_id}"


def open_hh_page(
    page: Any,
    url: str,
    *,
    expect_vacancy: str | None = None,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    sites_path: Path = SITES_PATH,
) -> str:
    """Open one hh page, survive the regional redirect, and check where it landed.

    ``Any`` for the page for the reason the rest of this package gives: typing
    it means importing playwright at module scope, and every pure module here
    has to stay importable and testable on a machine with no browser.

    Returns the URL that actually loaded, which is not always the one that was
    asked for and is worth recording when it is not.

    Three questions get asked, in this order, and the first is asked before the
    browser is told to move:

    1. *Is the address on hh at all?* The URL came from outside this package —
       a queue file, a mandate, ``agent/session_signal.json`` — and is not
       trusted for being well formed. See the module docstring for the
       correction this is.
    2. *Did it land on hh, and on the site it started from?* Still hh but no
       longer the country root we asked for is not the regional redirect this
       helper absorbs.
    3. *Is it the vacancy?* ``expect_vacancy`` is the id the caller believes it
       is opening. When given, a page whose path names a different vacancy — or
       no vacancy at all, which is what a sign-in wall looks like — raises.
       Leaving it out means the caller genuinely does not care which page it
       got, which is true of almost nothing in this package.

    ``sites_path`` is here so a test can hand this a different data file and
    watch the allowlist change with it. Nothing in the package passes it.
    """
    allowed = hh_sites(sites_path)
    if not on_hh(url, allowed):
        raise NotOnHhError(
            f"Этот адрес не на hh, и агент его не откроет: {url}\n"
            f"Разрешены только https-адреса на сайтах {', '.join(sorted(allowed))} "
            "и на их региональных поддоменах.\n"
            "Адрес пришёл снаружи — из очереди, из мандата или из "
            "agent/session_signal.json. Посмотрите, что записано там."
        )
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    except Exception as error:
        # Broad on purpose and re-raised immediately unless the message is the
        # one redirect this exists to absorb. Never a swallowed exception.
        if not any(marker in str(error) for marker in INTERRUPTED_NAVIGATION):
            raise
        # hh interrupted its own navigation to send us to a regional subdomain.
        # The browser is already following it; wait for the one that wins.
        page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)

    landed = str(page.url)
    if not on_hh(landed, allowed):
        raise NotOnHhError(
            f"Открылась страница не на hh: {landed}\n"
            f"Запрашивали: {url}\n"
            "Так выглядит уход на чужой сайт, а не региональный редирект hh, "
            "поэтому содержимое этой страницы не читается."
        )
    if not same_site(landed, url):
        raise NavigatedElsewhereError(
            f"Запрошена страница на {_hostname(url)}, а открылась {_hostname(landed)}.\n"
            f"Адрес: {landed}\n"
            "Это не региональный редирект hh, и читать эту страницу как вакансию нельзя."
        )
    if expect_vacancy is not None and vacancy_id_in_path(landed) != expect_vacancy:
        raise NavigatedElsewhereError(
            f"Просили вакансию {expect_vacancy}, а открылось: {landed}\n"
            "Так выглядит требование войти, архивная вакансия или капча — но не та\n"
            "страница, о которой шла речь. Проверьте вход: python -m agent.login"
        )
    return landed
