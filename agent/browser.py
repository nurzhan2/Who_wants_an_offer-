"""Opening the owner's browser, and the two launch arguments that are load-bearing.

A persistent context, so that logging in happens once by hand and the session
lives in a directory on the owner's disk rather than in anything this program
handles. The agent never sees the password, never types it, and has nowhere to
put it — which is the brief's rule expressed as an absence rather than as a
policy.

Two arguments to :func:`open_browser` do real work and the rest deliberately do
not exist.

**headless is False and is not a parameter.** The brief requires the owner to be
able to watch what happens on their account, and a boolean argument would be a
boolean somebody passes. There is no knob here, so making this headless is an
edit to this file with the reason in front of the person making it. That is a
speed bump rather than a wall — anyone determined can run the whole thing under
a virtual display — and the README says so instead of implying a guarantee this
module cannot give.

**service_workers is blocked, and this is a safety argument rather than a
performance one.** ``context.route`` — the interception the whole of
``agent/gate.py`` depends on — does not see requests issued from a service
worker. hh is a large single-page application and may well register one. If it
did, an application request could leave the browser without the gate ever being
consulted, and the gate is the only guard that does not depend on knowing which
element is the submit button. Blocking service workers is what makes the
interception total; without it every other guarantee in this package is
conditional on a fact nobody has checked.

Nothing else is configurable on purpose. No ``user_agent``, no ``proxy``, no
``args``, no ``extra_http_headers``: every one of those is a way to make this
browser claim to be something it is not, and the brief's line about honest
identification applies to the agent exactly as it does to the crawler.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from agent.config import PROFILE_DIR, SCREENSHOT_DIR

if TYPE_CHECKING:  # pragma: no cover - playwright is an agent-only dependency
    from playwright.sync_api import BrowserContext

#: How much to slow every interaction, in milliseconds. Not stealth — the point
#: is that a person watching can see what is happening and stop it.
DEFAULT_SLOW_MO_MS: Final[int] = 250


class BrowserUnavailableError(RuntimeError):
    """Playwright or its browser is not installed on this machine."""


@contextmanager
def open_browser(
    *, slow_mo_ms: int = DEFAULT_SLOW_MO_MS, profile_dir: Path = PROFILE_DIR
) -> "Iterator[BrowserContext]":
    """The owner's own browser, visible, with the session it already has.

    Imported inside the function rather than at module scope so that every other
    module in this package — the gate, the letter guard, the state machine, all
    of which are pure — can be imported and tested on a machine with no browser
    at all. That is not a convenience: it is what lets the whole safety layer be
    unit-tested in the default test run, which the brief requires to happen
    without a browser.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - environment, not logic
        raise BrowserUnavailableError(
            "playwright не установлен. Установка: uv sync --group agent && "
            "uv run playwright install chromium"
        ) from exc

    profile_dir.mkdir(parents=True, exist_ok=True)
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            # Visible, always. See the module docstring.
            headless=False,
            slow_mo=slow_mo_ms,
            # The one argument the interception depends on. See the module
            # docstring; without it agent/gate.py is not total.
            service_workers="block",
        )
        try:
            yield context
        finally:
            context.close()


def screenshot_on_error(page: Any, name: str) -> Path | None:
    """A picture of what went wrong, saved where the owner can look at it.

    ``Any`` for the page because this is called from except-blocks that may not
    have a typed handle, and because the alternative is importing playwright at
    module scope, which the docstring above explains we do not do.

    The file lands in a gitignored directory and shows a logged-in account, so
    it is for the owner's eyes on the owner's machine and goes nowhere else. No
    cookie, token or storage state is ever captured — a screenshot is pixels,
    and the code never reaches for ``context.cookies()`` or ``storage_state()``.
    """
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SCREENSHOT_DIR / f"{name}.png"
    try:
        page.screenshot(path=str(path), full_page=False)
    except Exception:  # pragma: no cover - a failed screenshot must not mask the error
        return None
    return path
