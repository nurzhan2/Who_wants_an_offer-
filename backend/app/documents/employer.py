"""What the employer has published about themselves, beside their vacancy.

Four facts, all of them stated by the employer on their own posting and read
out of the payload the connector already stored:

* when they were last active on hh — the «был онлайн» line;
* whether they are an accredited IT employer, which in Kazakhstan and Russia is
  a concrete eligibility fact for the candidate rather than a badge;
* whether hh is running an additional check on them, which is a useful filter
  against the postings that turn out to be recruitment farms;
* how many applications the posting already has, where the page says so.

**Nothing is looked up anywhere.** No employee of a company is searched for,
here or anywhere else in this project — not on a professional network, not in a
leak, not by pattern-matching a corporate address. That is a rule about what
this repository is, not a limit of what it can currently reach, and the module
that renders these four fields is the natural place for it to be written down.
The connector reads ``employerManager.latestActivity`` and drops the recruiter's
name that sits beside it in the same block.

**Absent is not false and not zero.** ``responses_count`` is ``None`` on a page
that did not state it, and rendering that as "0 applications so far" would turn
a gap into an encouraging number. The same goes for the activity timestamp: no
value means hh did not say, which is different from "has not logged in".
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

#: Where the connectors keep everything they worked out. Same key the letters
#: store reads, because it is the same block.
DERIVED_KEY = "_derived"


class EmployerSignals(BaseModel):
    """The four published facts, or nothing where the posting stated nothing."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: «был онлайн». None means the page did not say, never "not recently".
    last_activity: datetime | None = None
    accredited_it_employer: bool = False
    on_additional_check: bool = False
    #: None means the page did not state a count, which is not zero.
    responses_count: int | None = None

    @property
    def is_empty(self) -> bool:
        """Whether there is anything here worth putting on a screen."""
        return (
            self.last_activity is None
            and self.responses_count is None
            and not self.accredited_it_employer
            and not self.on_additional_check
        )


def from_raw(raws: list[dict[str, Any]]) -> EmployerSignals:
    """Read the signals out of a vacancy's stored payloads.

    A vacancy can be held under several sources after deduplication, and only
    some of them carry these fields at all — the API sources have no such
    concept. So the first payload that states a value wins, and a source with
    nothing to say about a field leaves it alone rather than overwriting it with
    a default. Read defensively throughout: this is JSONB, which is to say it is
    whatever a connector wrote there on the day it ran.
    """
    last_activity: datetime | None = None
    accredited = False
    additional_check = False
    responses: int | None = None

    for raw in raws:
        block = raw.get(DERIVED_KEY)
        if not isinstance(block, dict):
            continue
        if last_activity is None:
            last_activity = _timestamp(block.get("employer_last_activity"))
        accredited = accredited or block.get("accredited_it_employer") is True
        additional_check = additional_check or block.get("employer_on_additional_check") is True
        if responses is None:
            responses = _count(block.get("responses_count"))

    return EmployerSignals(
        last_activity=last_activity,
        accredited_it_employer=accredited,
        on_additional_check=additional_check,
        responses_count=responses,
    )


def _timestamp(value: object) -> datetime | None:
    """An ISO timestamp out of JSONB, or None for anything else."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _count(value: object) -> int | None:
    """A non-negative count, or None. ``True`` is not 1 here."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value
