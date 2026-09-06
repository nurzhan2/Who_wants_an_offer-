"""What this agent has done, in a file on the owner's own machine.

Small on purpose. The journal answers one question — "have I already dealt with
this vacancy, and how did it go" — and it is explicitly **not** the source of
truth for whether an application was sent. hh is. A local row saying ``sent``
that hh disagrees with is a row that stops the agent from re-applying, which is
the safe direction; a local row saying ``queued`` for something already applied
to is caught on the page itself, before the click, by reading hh's own
``applicantVacancyResponseStatuses``. The brief puts it plainly: «Локальная база
может отстать — она не единственный источник правды».

``sqlite3`` from the standard library, because the whole database is one table
that one process on one laptop writes to. An ORM here would be a dependency and
a migration story in exchange for nothing.

**What is deliberately not stored.** No cookies, no tokens, no session state —
those live in the browser profile and belong nowhere else. Not the letter
either: it is the owner's own words about themselves, it is available in the
queue it came from, and a file full of somebody's cover letters is a thing to
leak rather than a thing to keep. What is stored is its digest, which is enough
to prove afterwards that the letter sent was the letter confirmed.

The unique index on ``vacancy_id`` is the brief's requirement and does real
work: two runs racing, or a queue containing the same vacancy twice, resolve to
one row rather than to two applications.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, final

from agent.state import Actor, Status, check

SCHEMA: Final[str] = """
CREATE TABLE IF NOT EXISTS application (
    vacancy_id   TEXT PRIMARY KEY,
    status       TEXT NOT NULL,
    title        TEXT,
    company      TEXT,
    url          TEXT,
    letter_digest TEXT,
    reason       TEXT,
    first_seen_at TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_application_status ON application (status);
"""


@final
@dataclass(frozen=True, slots=True)
class Entry:
    """One vacancy's history, as the journal holds it."""

    vacancy_id: str
    status: Status
    title: str | None = None
    company: str | None = None
    url: str | None = None
    #: sha256 of the letter that was confirmed. Never the letter.
    letter_digest: str | None = None
    #: Why it ended where it did, in words a person reads.
    reason: str | None = None


@final
class Journal:
    """The agent's memory of its own runs."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """One connection per operation, committed or rolled back on exit.

        A long-lived connection would hold a write lock across a browser step
        that waits on a human, and the human is the slowest part of this system.
        """
        connection = sqlite3.connect(self.path, isolation_level="DEFERRED")
        try:
            with closing(connection), connection:
                yield connection
        finally:
            pass

    def get(self, vacancy_id: str) -> Entry | None:
        """This vacancy's row, or None if it has never been seen."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT vacancy_id, status, title, company, url, letter_digest, reason "
                "FROM application WHERE vacancy_id = ?",
                (vacancy_id,),
            ).fetchone()
        if row is None:
            return None
        return Entry(
            vacancy_id=row[0],
            status=Status(row[1]),
            title=row[2],
            company=row[3],
            url=row[4],
            letter_digest=row[5],
            reason=row[6],
        )

    def record(self, entry: Entry, *, actor: Actor) -> None:
        """Write this vacancy's state, refusing a move the machine may not make.

        The transition check lives here rather than in the caller because this
        is the choke point every state change goes through, and a rule enforced
        at the only place that can break it is a rule that holds.
        """
        now = datetime.now(UTC).isoformat()
        previous = self.get(entry.vacancy_id)
        if previous is not None and previous.status is not entry.status:
            check(previous.status, entry.status, actor=actor)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO application (vacancy_id, status, title, company, url,
                                         letter_digest, reason, first_seen_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (vacancy_id) DO UPDATE SET
                    status = excluded.status,
                    title = COALESCE(excluded.title, application.title),
                    company = COALESCE(excluded.company, application.company),
                    url = COALESCE(excluded.url, application.url),
                    letter_digest = COALESCE(excluded.letter_digest, application.letter_digest),
                    reason = excluded.reason,
                    updated_at = excluded.updated_at
                """,
                (
                    entry.vacancy_id,
                    entry.status.value,
                    entry.title,
                    entry.company,
                    entry.url,
                    entry.letter_digest,
                    entry.reason,
                    now,
                    now,
                ),
            )

    def count_sent_since(self, moment: datetime) -> int:
        """Applications sent since a point in time. The daily cap reads this."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM application WHERE status = ? AND updated_at >= ?",
                (Status.SENT.value, moment.isoformat()),
            ).fetchone()
        return int(row[0])

    def by_status(self, status: Status) -> list[Entry]:
        """Every vacancy currently in one state, oldest first."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT vacancy_id, status, title, company, url, letter_digest, reason "
                "FROM application WHERE status = ? ORDER BY updated_at",
                (status.value,),
            ).fetchall()
        return [
            Entry(
                vacancy_id=row[0],
                status=Status(row[1]),
                title=row[2],
                company=row[3],
                url=row[4],
                letter_digest=row[5],
                reason=row[6],
            )
            for row in rows
        ]
