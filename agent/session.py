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

from agent.login import SIGNAL_PATH


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
    missing = [key for key in signal if not state.get(key)]
    if missing:
        raise SessionExpiredError(
            "Сессия hh больше не активна — пропали признаки входа: "
            f"{', '.join(missing[:5])}.\n"
            "Войдите заново: uv run python -m agent.login"
        )
