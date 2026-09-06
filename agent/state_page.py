"""Reading hh's page state, and the one question this package cannot yet answer.

The vacancy page boots its frontend from a JSON blob in a hidden template. The
crawler in ``backend/app/sources/hh.py`` reads the same blob for the same
reason, and the two do not share code on purpose: that one is anonymous,
read-only and runs on a server, this one runs in the owner's browser under their
account, and a shared helper would be a thread between two programs that must
not have one.

What this module adds over the crawler's version is the applicant's half of the
page — ``applicantVacancyResponseStatuses`` — and one deliberate hole in it.

**The applied-marker is unknown, and that is a stop rather than a default.**
Measured anonymously on 2026-09-06, the key exists and carries the letter
requirement, the test flag and the letter length limit. What it looks like once
the owner has *already applied* has never been seen, because seeing it requires
their session. It is the whole idempotency signal: the check the brief demands
before every click, «читать applicantVacancyResponseStatuses из стейта».

So :func:`read_applied` is a three-valued function and its unknown answer is not
"probably fine". A design where an unreadable marker means "not applied yet" is
a design that starts double-applying, to every vacancy at once, on the day hh
renames a key — and double-applying is the one thing the owner cannot undo. The
unknown answer routes the vacancy to a human instead.

When the probe fills :data:`APPLIED_MARKERS` in, this becomes a two-valued
function for the shapes it knows and keeps returning ``None`` for everything
else. That is the shape it should have kept anyway.
"""

import html as html_lib
import json
import re
from typing import Any, Final

#: The same marker the crawler reads. Duplicated rather than imported: see the
#: module docstring on why these two programs do not share code.
STATE_MARKER: Final[re.Pattern[str]] = re.compile(
    r'<template[^>]*id="HH-Lux-InitialState"[^>]*>(.*?)</template>', re.DOTALL
)

#: Filled in from a probe run against a vacancy the owner has already applied
#: to. Each entry is a key path inside ``applicantVacancyResponseStatuses[id]``
#: whose presence means "already applied". Empty until stage 0 has been run,
#: and while it is empty :func:`read_applied` can only ever answer "unknown".
APPLIED_MARKERS: Final[tuple[tuple[str, ...], ...]] = ()


def read_state(page_html: str) -> dict[str, Any] | None:
    """hh's boot state, or None when the marker is not where it was.

    ``Any`` because this is hh's entire frontend state — dozens of unrelated
    keys — and the two this package reads are validated by the functions below.
    """
    match = STATE_MARKER.search(page_html)
    if match is None:
        return None
    try:
        decoded = json.loads(html_lib.unescape(match.group(1)))
    except ValueError:
        return None
    return decoded if isinstance(decoded, dict) else None


def read_applied(state: dict[str, Any], vacancy_id: str) -> bool | None:
    """Whether the owner has already applied: True, False, or **unknown**.

    ``None`` means the page did not say in a way this code recognises. Every
    caller must treat that as a reason to hand the vacancy to a person; nothing
    may treat it as ``False``. See the module docstring.
    """
    statuses = state.get("applicantVacancyResponseStatuses")
    if not isinstance(statuses, dict):
        return None
    entry = statuses.get(str(vacancy_id))
    if not isinstance(entry, dict):
        return None

    if not APPLIED_MARKERS:
        # Stage 0 has not been run. We can see the key, and we do not know what
        # "applied" looks like inside it, so we say so.
        return None

    for path in APPLIED_MARKERS:
        cursor: Any = entry
        for step in path:
            if not isinstance(cursor, dict) or step not in cursor:
                cursor = None
                break
            cursor = cursor[step]
        if cursor:
            return True
    return False


def vacancy_id_from_url(url: str) -> str | None:
    """The id in a vacancy URL, so a page can be checked against what we meant."""
    match = re.search(r"/vacancy/(\d+)", url)
    return match.group(1) if match else None
