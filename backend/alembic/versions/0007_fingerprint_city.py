"""Put the city into the deduplication key, and recompute the rows that lack it.

``vacancy.fingerprint`` was sha1 over a normalised company and title and nothing
else, because the one caller passed no city. On a bounded feed that costs
little. On a corpus source it is a silent data loss: an employer advertising the
same title in four cities hashes to one fingerprint, ``bulk_upsert`` collapses
the four postings onto one row, and three of them are overwritten in place. The
vacancy count reads low and no column anywhere records that anything went
missing. ``app/normalize/fingerprint.py`` now stands at version 2 and its caller
supplies the city; this migration brings the stored rows to the same footing.

**What it repairs.** Every row whose fingerprint the version-1 algorithm
actually produced is recomputed with its city and stamped version 2, so a
re-crawl of that same posting lands on the same row instead of inserting a
second one. Those rows are identified first and their city is recovered second,
out of the untouched payload in ``vacancy_source.raw`` — that is what the JSONB
column is for — because the version-1 pipeline never wrote ``vacancy.city``
either, and a recompute over a column of NULLs would be an expensive no-op. In
that order rather than the reverse, so that a row this file declines to re-key
is not handed a city its own fingerprint knows nothing about.

**What it cannot repair.** A posting that was overwritten as it arrived is gone
from this database. Nothing about it was kept: the row it collided with holds
the other job's title, salary, link and description. No recomputation invents
it back — only a re-crawl of the source does. So the honest reading of this
migration is that it stops the loss and re-keys what survived; it does not undo
what version 1 already dropped.

**Why Python and not SQL.** The hashed string is NFKC-normalised, casefolded
and stripped of everything Python's Unicode ``\\w`` does not accept.
PostgreSQL can approximate each of those (``normalize``, ``lower``,
``regexp_replace``) and agrees with none of them exactly — ``lower`` is not
``str.casefold``, and PostgreSQL's ``\\w`` is the collation's alphanumerics. A
backfill that normalises even slightly differently from the running code writes
keys the application would never produce, and the next crawl of the same
posting inserts a duplicate row: precisely the failure being fixed.

The recovered city is read the same way, and for the same reason. ``btrim`` is
not ``str.strip`` — it removes spaces, not tabs or a non-breaking space, and a
Russian page supplies U+00A0 readily — and nothing in SQL collapses a run of
inner whitespace the way ``" ".join(value.split())`` does. Those two differences
move where a 120-character cut falls, and the cut is inside the hashed string.
So the SQL below only *finds* a candidate city; ``_stated_city`` reduces it,
character for character as ``app/pipeline/runner.py`` does for a live posting.

**Why the algorithm is copied in rather than imported.** A migration has to
produce the same result forever. Importing ``app.normalize.fingerprint`` would
mean that once phase 4 raises the constant to 3, re-running this file stamps
version-3 keys into rows it labels 2. The copy below is version 2, frozen.

**Why the version-1 rows cannot collide with each other.** Adding a part to the
hash can only split. Two rows that differed under version 1 differed in their
company or their title, both of which are still in the input, so no pair of
version-1 rows can hash together under version 2 — and a row that gains a city
moves to a key whose third part is non-empty, which no version-1 key has. That
much is provable, and :func:`upgrade` still checks it rather than asserting it.

**Why that is not enough.** The proof is about pairs of version-1 rows, and this
table is not only version-1 rows. Deploy the code and run the migration in the
ordinary order and the crawl runs new code against an unmigrated table: it
computes a version-2 key, finds no row holding it, and ``bulk_upsert`` inserts
one. A chain employer's five identical Almaty postings collapsed onto a single
version-1 row under the old key; the three of them the crawl re-saw now sit on a
version-2 row, while the two that had expired out of the sitemap still hang off
the old row with their city in ``vacancy_source.raw``. This migration recovers
that city, computes the very key the version-2 row already holds, and PostgreSQL
answers with ``duplicate key value violates unique constraint
"uq_vacancy_fingerprint"`` — verified, not reasoned about. So :func:`upgrade`
looks for that case before writing anything and refuses with a count and the
first of the blocked ids, the way :func:`downgrade` handles its own collisions.
Merging the two rows means choosing whose title, salary and links survive and
where the provenance rows point; that is an operator's decision, not a
migration's, and a refusal before the first write is a far better thing to read
in a deploy log than a unique violation from inside an UPDATE.

Going down has the mirror problem and is likewise not safe: dropping the city
merges rows again. :func:`downgrade` refuses rather than deleting a posting to
make room.

Revision ID: 0007_fingerprint_city
Revises: 0006_source_state
Create Date: 2026-09-06
"""

import hashlib
import logging
import re
import unicodedata
from collections import Counter
from collections.abc import Sequence
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from alembic import context, op

revision: str = "0007_fingerprint_city"
down_revision: str | None = "0006_source_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Not structlog: this file runs under Alembic's own logging configuration from
#: alembic.ini, which already routes the ``alembic`` tree to stderr at INFO, and
#: importing the application's logger would tie a frozen migration to a module
#: that keeps moving. English, like every other message here, and free of any
#: character a cp1251 console cannot render.
logger = logging.getLogger("alembic.runtime.migration")

#: The version this migration writes. Frozen; the application constant moves on
#: without it.
TARGET_VERSION = 2

#: Width of ``vacancy.city``, so a recovered value is cut here rather than by
#: the database. The same number as ``app.pipeline.runner.MAX_CITY``, frozen for
#: the same reason the algorithm below is: if the column is ever widened, this
#: migration must keep writing the keys it wrote on the day it ran.
MAX_CITY = 120

# ── version 2 of app/normalize/fingerprint.py, frozen ─────────────────

_PUNCTUATION = re.compile(r"[^\w\s]", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")
_SEPARATOR = "\x1f"


def _normalize_part(value: str | None) -> str:
    """Reduce one component to its comparable form."""
    if not value:
        return ""
    folded = unicodedata.normalize("NFKC", value).casefold()
    return _WHITESPACE.sub(" ", _PUNCTUATION.sub(" ", folded)).strip()


def _fingerprint(company: str | None, title: str, city: str | None) -> str:
    """The 40-character key. ``city=None`` reproduces version 1 exactly."""
    parts = (_normalize_part(company), _normalize_part(title), _normalize_part(city))
    digest = hashlib.sha1(_SEPARATOR.join(parts).encode("utf-8"), usedforsecurity=False)
    return digest.hexdigest()


# ── statements ────────────────────────────────────────────────────────

#: Candidate cities out of the payload a connector already stored. Only a key a
#: connector *derived* is read: ``_derived.city`` is a place lifted from a
#: structured field on the page, never a free-text location line, which is the
#: same rule ``app/pipeline/runner.py`` follows for live postings.
#:
#: Narrowed to version-1 rows that still have no city, which is as far as SQL
#: can go: whether a row is one this migration owns is a question about its
#: fingerprint, so :func:`_recover_cities` drops the rest of what comes back.
#:
#: Every provenance row is returned, oldest first, rather than DISTINCT ON:
#: whether a value survives is decided by :func:`_stated_city` in Python, and a
#: SQL pre-filter for "non-empty" cannot be made to agree with it. A vacancy
#: whose oldest source carries only whitespace therefore falls through to its
#: next one instead of recovering nothing.
_SELECT_RECOVERABLE_CITY = sa.text(
    """
    SELECT vs.vacancy_id AS vacancy_id,
           vs.raw -> '_derived' ->> 'city' AS city
      FROM vacancy_source AS vs
      JOIN vacancy AS v ON v.id = vs.vacancy_id
     WHERE v.city IS NULL
       AND v.fingerprint_version = :version
       AND vs.raw -> '_derived' ->> 'city' IS NOT NULL
     ORDER BY vs.vacancy_id, vs.created_at, vs.id
    """
)

_SET_CITY = sa.text("UPDATE vacancy SET city = :city WHERE id = :id")

_SELECT_BY_VERSION = sa.text(
    "SELECT id, company, title, city, fingerprint FROM vacancy "
    "WHERE fingerprint_version = :version ORDER BY id"
)

#: Every key held by a row this migration does not rewrite. A re-keyed row has
#: to land where none of these already sits, or the UNIQUE constraint stops the
#: upgrade mid-transaction.
_SELECT_OTHER_VERSIONS = sa.text(
    "SELECT fingerprint FROM vacancy WHERE fingerprint_version <> :version"
)

_APPLY = sa.text(
    "UPDATE vacancy SET fingerprint = :fingerprint, fingerprint_version = :version WHERE id = :id"
)


def _rows(connection: sa.Connection, version: int) -> list[dict[str, Any]]:
    """Every vacancy stamped with one fingerprint version.

    ``Any`` because a row mapping is heterogeneous — a UUID, three nullable
    strings — and naming a union here would be less honest than the mapping
    itself; each field is narrowed at the point it is used.
    """
    result = connection.execute(_SELECT_BY_VERSION, {"version": version})
    return [dict(row) for row in result.mappings()]


def _stated_city(value: str | None) -> str | None:
    """Reduce a recovered place name the way the live pipeline reduces one.

    Character for character ``app.pipeline.runner.stated_city``: collapse every
    run of whitespace to one space, drop the ends, cut to the column width, and
    call an empty result no city at all. Copied rather than imported for the
    reason the hash is — a migration has to keep writing what it wrote.
    """
    if value is None:
        return None
    return " ".join(value.split())[:MAX_CITY] or None


def _recover_cities(connection: sa.Connection, rows: list[dict[str, Any]]) -> int:
    """Fill ``vacancy.city`` for the rows about to be re-keyed, and only those.

    Without this the backfill would be a pure no-op: the version-1 pipeline
    never wrote ``vacancy.city`` either, and recomputing a hash over a column of
    NULLs reproduces the key the row already holds.

    ``rows`` is the set :func:`upgrade` has decided it owns, so a row it refuses
    to re-key is also left without a city. Writing one would leave the column
    naming a place the row's own fingerprint does not know about — the state
    ``to_vacancy`` refuses to create for a live posting.

    The dicts are updated in place as well as in the database, because the
    caller hashes from them and a second SELECT to read back what this function
    just wrote would prove nothing the write did not.
    """
    wanted = {row["id"]: row for row in rows if row["city"] is None}
    if not wanted:
        return 0

    recovered: dict[Any, str] = {}
    for candidate in connection.execute(_SELECT_RECOVERABLE_CITY, {"version": 1}).mappings():
        vacancy_id = candidate["vacancy_id"]
        if vacancy_id not in wanted or vacancy_id in recovered:
            # The ORDER BY put the oldest provenance row first, so a
            # cross-posted vacancy takes that one rather than an arbitrary one.
            continue
        city = _stated_city(candidate["city"])
        if city is not None:
            recovered[vacancy_id] = city

    if recovered:
        connection.execute(
            _SET_CITY, [{"id": key, "city": value} for key, value in recovered.items()]
        )
        for vacancy_id, city in recovered.items():
            wanted[vacancy_id]["city"] = city
    return len(recovered)


def _collisions(updates: list[dict[str, Any]], taken: set[str]) -> set[str]:
    """Which of the keys about to be written are not free.

    Two ways to be unfree, and both end in the same unique violation: two
    updates computing one key, or one update computing a key a row this
    migration is not rewriting already holds.
    """
    counts = Counter(update["fingerprint"] for update in updates)
    return {value for value, seen in counts.items() if seen > 1} | (set(counts) & taken)


def _refuse_offline(direction: str) -> None:
    """``alembic --sql`` cannot render this migration, and should say why.

    Offline mode has no connection to read from, and every decision here is made
    from the rows: which fingerprints version 1 actually produced, what city
    each row turns out to carry, whether going back would merge two jobs. None
    of that can be written as a static script, so the honest answer is a
    sentence rather than an ``AttributeError`` out of a mock connection.
    """
    if context.is_offline_mode():
        raise RuntimeError(
            f"0007_fingerprint_city cannot be {direction} with --sql: it reads every vacancy "
            "row to decide what to write. Run it against the database instead."
        )


def upgrade() -> None:
    """Recover the cities, then re-key every row version 1 actually produced."""
    _refuse_offline("applied")
    connection = op.get_bind()

    # Everything already keyed by some other writer: rows a newer crawl stamped
    # version 2 before anyone ran this file, and the version-1 rows skipped
    # below. Their keys are occupied and cannot be moved out of the way here.
    taken: set[str] = {
        row["fingerprint"]
        for row in connection.execute(_SELECT_OTHER_VERSIONS, {"version": 1}).mappings()
    }

    mine: list[dict[str, Any]] = []
    foreign = 0
    for row in _rows(connection, 1):
        # Only rows this algorithm wrote may be re-keyed. A fingerprint that is
        # not sha1(company, title, '') came from somewhere else — the dev seed
        # invents its keys from an index — and recomputing it would rename a row
        # whose owner still looks it up by the old value.
        if row["fingerprint"] != _fingerprint(row["company"], row["title"], None):
            foreign += 1
            taken.add(row["fingerprint"])
            continue
        mine.append(row)

    recovered = _recover_cities(connection, mine)
    if recovered:
        logger.info("fingerprint_city: recovered a city for %s vacancies", recovered)

    updates: list[dict[str, Any]] = [
        {
            "id": row["id"],
            "fingerprint": _fingerprint(row["company"], row["title"], row["city"]),
            "version": TARGET_VERSION,
        }
        for row in mine
    ]

    clashing = _collisions(updates, taken)
    if clashing:
        blocked: list[UUID] = [u["id"] for u in updates if u["fingerprint"] in clashing]
        # Ids rather than a query: the city that causes the clash is recovered
        # inside this transaction and is rolled back with the refusal, so no
        # SELECT the operator runs afterwards would show it. It survives in
        # vacancy_source.raw -> '_derived' ->> 'city', which the ids reach.
        sample = ", ".join(str(vacancy_id) for vacancy_id in blocked[:5])
        raise RuntimeError(
            f"0007_fingerprint_city: {len(blocked)} version-1 vacancies would be re-keyed onto "
            f"{len(clashing)} fingerprints that are already taken, and vacancy.fingerprint is "
            "UNIQUE. This is what a deploy leaves behind when the new code crawled before the "
            "migration ran: a re-crawled posting already inserted the version-2 row whose key "
            f"a stale version-1 row now computes. Blocked vacancy ids begin: {sample}. Each is "
            "the same employer, title and city as a row already stored, so merge the pair or "
            "delete the stale one, then re-run. This migration will not choose which of two "
            "postings to keep."
        )

    if updates:
        connection.execute(_APPLY, updates)
    logger.info(
        "fingerprint_city: re-keyed %s vacancies, left %s alone (fingerprint not produced by "
        "version 1)",
        len(updates),
        foreign,
    )


def downgrade() -> None:
    """Drop the city back out of the key, or refuse if that would merge rows.

    Going back is the destructive direction. Two postings this migration told
    apart by city share one version-1 key, and applying it would hit the unique
    constraint — or, if it were written to swallow that, would silently drop one
    of the two jobs. Neither is this file's decision to make, so it reports the
    clash and stops. Recovered ``vacancy.city`` values are left in place: they
    are data a crawl legitimately collected, and erasing them would lose more
    than the downgrade restores.
    """
    _refuse_offline("reversed")
    connection = op.get_bind()

    updates: list[dict[str, Any]] = []
    kept: set[str] = set()
    for row in _rows(connection, TARGET_VERSION):
        current = _fingerprint(row["company"], row["title"], row["city"])
        if row["fingerprint"] != current:
            # Not written by version 2 either; leave it exactly as it is.
            kept.add(row["fingerprint"])
            continue
        updates.append(
            {
                "id": row["id"],
                "fingerprint": _fingerprint(row["company"], row["title"], None),
                "version": 1,
            }
        )
    # The version-1 rows upgrade() deliberately left in place: a table it has
    # been through is mixed by design, and a row returning to version 1 must not
    # land on a key one of those already holds.
    kept.update(row["fingerprint"] for row in _rows(connection, 1))

    clashing = _collisions(updates, kept)
    if clashing:
        merged: list[UUID] = [u["id"] for u in updates if u["fingerprint"] in clashing]
        raise RuntimeError(
            f"downgrade would merge {len(merged)} vacancies onto {len(clashing)} version-1 "
            "fingerprints, which drops a posting per collision. Delete or export the rows "
            "listed by `SELECT id, company, title, city FROM vacancy WHERE fingerprint_version "
            "= 2` first, then re-run: this migration will not choose which job to lose."
        )

    if updates:
        connection.execute(_APPLY, updates)
    logger.info("fingerprint_city: returned %s vacancies to version 1", len(updates))
