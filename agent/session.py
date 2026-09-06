"""Is the owner still logged in? Asked before every run, and asked properly.

Cookies expire on their own schedule and the failure mode is the nasty one: the
site keeps rendering, pages keep loading, and applications quietly stop going
through. A health check of the form "did the page respond" passes happily
through exactly that. The brief is specific — «открыть страницу, где видно
авторизованного пользователя, и убедиться, что он там есть» — and the difficulty
is that nobody knows offhand which field that is.

So it is measured rather than guessed. ``agent/login.py`` records which
top-level keys of hh's page state appear after the owner logs in and are absent
before, and this module asserts those keys are still there. That is a signal
derived from the account rather than from the page loading, and it was produced
by the same session it is checking.

If the signal file does not exist, this refuses rather than degrading to a
weaker check. A health check that quietly becomes "the page loaded" is worse
than none, because it reports success.
"""

import json
from pathlib import Path
from typing import Any, final

from agent.login import PROBE_URL, SIGNAL_PATH


@final
class SessionExpiredError(Exception):
    """The browser profile is no longer logged in, or never was."""


@final
class SignalUnknownError(Exception):
    """Nobody has recorded what a logged-in page looks like yet."""


def load_signal(path: Path = SIGNAL_PATH) -> list[str]:
    """The keys that mean "signed in", as ``login.py`` measured them."""
    if not path.is_file():
        raise SignalUnknownError(
            f"Нет {path.name}: неизвестно, как выглядит страница под аккаунтом.\n"
            "Сначала: uv run python -m agent.login"
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    keys = payload.get("appeared_after_login")
    if not isinstance(keys, list) or not keys:
        raise SignalUnknownError(f"{path.name} не содержит признаков авторизации")
    return [str(key) for key in keys]


def check(state: dict[str, Any], *, signal: list[str]) -> None:
    """Raise unless this page still looks like an authenticated one.

    ``Any`` for the state for the reason the rest of this package gives: it is
    hh's whole boot payload and only these keys are read.

    Every recorded key must be present and non-empty. Requiring all of them
    rather than any is deliberate: hh ships null shells for keys it has not
    filled, and a check that accepts one surviving key out of eight will pass
    for months after the session has actually gone.
    """
    missing = missing_signal_keys(state, signal=signal)
    if missing:
        raise SessionExpiredError(
            "Сессия hh больше не активна — пропали признаки входа: "
            f"{', '.join(missing[:5])}.\n"
            "Войдите заново: uv run python -m agent.login"
        )


def missing_signal_keys(state: dict[str, Any], *, signal: list[str]) -> list[str]:
    """Which recorded signs of an account are absent from this page state.

    The measuring half of :func:`check`, split out because two callers need the
    answer without the exception: the probe records whether its run was
    authenticated, and ``agent/selectors.py`` refuses evidence from a run that
    was not.
    """
    return [key for key in signal if not state.get(key)]


def looks_authenticated(state: dict[str, Any], path: Path = SIGNAL_PATH) -> bool:
    """Whether this page state carries every recorded sign of the owner's account.

    False when the signal has never been measured, which is the honest answer:
    without ``session_signal.json`` nothing here knows what a logged-in page
    looks like, and "I cannot tell" must not be recorded as "yes".
    """
    try:
        signal = load_signal(path)
    except (SignalUnknownError, json.JSONDecodeError):
        return False
    return not missing_signal_keys(state, signal=signal)


def signal_source(path: Path = SIGNAL_PATH) -> str:
    """The page the signal was measured on, so the check can use the same one.

    The keys ``login.py`` records are the ones that appear on a *vacancy* page
    after signing in. Asserting them somewhere else compares two different
    documents: a run used to check them against hh's front page, where several
    of those keys simply do not exist, so a live session could read as expired
    forever. Falling back to ``login.PROBE_URL`` keeps an older signal file
    working.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return PROBE_URL
    recorded = payload.get("probe_url")
    return recorded if isinstance(recorded, str) and recorded.startswith("https://") else PROBE_URL
