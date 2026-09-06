"""The agent's own settings. Deliberately not the backend's.

``app.core.config`` is a fine settings module and importing it here would be a
mistake: it would put a database URL, an Anthropic key and a set of source
credentials into the process that drives a browser under somebody's personal
account. These two programs have different blast radii and they get different
configuration, which is also the cheapest way to guarantee the import never
happens by accident — there is nothing to import.

Every default here is conservative in the direction the brief asks for. The
platform allows 200 applications a day; this allows 15. hh will not notice a
pause of five seconds; this waits between forty and a hundred and forty,
randomised. None of that is tuned for throughput, and the reason is in the
brief: «200 откликов подряд в 4 утра — не поведение человека».

The pause is drawn from a range rather than being a constant because a constant
interval is the single most recognisable thing an automated client does, and
because a person reading the log should see something that looks like a person
working through a list.
"""

import os
import random
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from typing import Final, final

#: Everything the agent writes lives beside this file, on the owner's machine.
AGENT_DIR: Final[Path] = Path(__file__).parent
#: The browser profile: cookies, the live session, history. Gitignored, and the
#: single most sensitive directory in this repository.
PROFILE_DIR: Final[Path] = AGENT_DIR / "profile"
SCREENSHOT_DIR: Final[Path] = AGENT_DIR / "screenshots"
JOURNAL_PATH: Final[Path] = AGENT_DIR / "agent.sqlite3"
#: Where a queue file is read from while the backend has no endpoint. See
#: ``agent/queue.py``.
QUEUE_PATH: Final[Path] = AGENT_DIR / "queue.json"


def _int(name: str, default: int) -> int:
    """One integer from the environment, or the conservative default."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@final
@dataclass(frozen=True, slots=True)
class Limits:
    """How much this agent may do, and when.

    The clock is injected wherever these are used rather than read here, so the
    rules can be tested at four in the morning without waiting until then.
    """

    #: Applications per day. The platform's ceiling is 200; this is not a
    #: throughput setting and raising it does not make the search go faster.
    daily_cap: int = 15
    #: Bounds of the randomised wait between two applications, in seconds.
    pause_min_seconds: int = 40
    pause_max_seconds: int = 140
    #: Nothing is sent outside these hours, local time.
    work_starts: time = time(9, 0)
    work_ends: time = time(21, 0)
    #: The brief's rule: a second failure in a row stops the run. Not a retry
    #: budget — a signal that something changed and a person should look.
    stop_after_consecutive_failures: int = 2

    @classmethod
    def from_env(cls) -> "Limits":
        """Limits with the few numbers a person might reasonably move.

        The defaults come from a freshly built instance rather than from the
        class attributes: ``slots=True`` means the names are descriptors on the
        class, not the values, and reading them there is both wrong and a type
        error.
        """
        defaults = cls()
        return cls(
            daily_cap=_int("AGENT_DAILY_CAP", defaults.daily_cap),
            pause_min_seconds=_int("AGENT_PAUSE_MIN", defaults.pause_min_seconds),
            pause_max_seconds=_int("AGENT_PAUSE_MAX", defaults.pause_max_seconds),
        )

    def pause(self, rng: random.Random) -> float:
        """One randomised wait. Takes its randomness so a test can pin it."""
        return rng.uniform(self.pause_min_seconds, self.pause_max_seconds)

    def within_working_hours(self, moment: time) -> bool:
        """Whether an application may be sent at this time of day."""
        return self.work_starts <= moment <= self.work_ends
