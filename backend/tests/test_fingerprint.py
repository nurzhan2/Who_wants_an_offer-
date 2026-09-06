"""The deduplication key, and the one property the backfill migration rests on.

The interesting assertions here are not "the hash is stable". They are about
which postings the key is allowed to put in the same row and which it must keep
apart, because that decision is irreversible in one direction: two jobs merged
into one row lose the second job's title, salary and link with nothing left to
say a loss happened, while two rows for one job are visible and mergeable.

That is why version 2 exists: version 1 hashed a company and a title and left an
employer's four cities sharing one row. Adding a part to the hash can only
split, and one test here proves that property rather than trusting the argument
— but it proves it about pairs of version-1 rows, which is less than the
migration needs and used to claim. A table that a crawl reached before the
migration did also holds version-2 rows, and those keys are occupied.

The rest of the file is about ``0007_fingerprint_city``, which carries its own
frozen copy of the algorithm so that a later version bump cannot change what it
writes. A copy that drifts writes keys the application would never produce, and
the next crawl of the same posting then inserts a duplicate row — so the copy is
pinned to the literal digests it produced on the day it was written, not to a
constant that is designed to move away from it.

A fake connection drives the migration's decisions here — which rows are
re-keyed, where the city comes from, when either direction refuses — because
those are cheap to enumerate and a database adds nothing to them. The same file
is then applied to PostgreSQL for real in ``test_fingerprint_migration.py``,
which is a separate module only because this one is entirely ``unit`` and that
one is entirely ``db``. Both halves are needed: the collision the migration now
refuses was first seen as ``duplicate key value violates unique constraint
"uq_vacancy_fingerprint"`` against a live database, not in an argument.
"""

import importlib.util
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from app.normalize.fingerprint import VERSION, fingerprint, normalize_part
from app.pipeline.runner import MAX_CITY, stated_city
from app.sources.base import RawPosting

pytestmark = pytest.mark.unit

#: Two cities this deployment actually serves, as test data rather than as
#: configuration: nothing in the module under test may know a city name.
ALMATY = "Алматы"
ASTANA = "Астана"
CHAIN = "Магнум"
SHOP_JOB = "Продавец-кассир"

#: What version 1 stored for that posting, and what the migration recognises a
#: row of its own by. Written out because half the tests below turn on the
#: difference between a row that holds this key and a row that does not.
VERSION_ONE_KEY = "e9827369139f32e05cd3614f0a1de9f7e252e055"


def test_the_version_is_two() -> None:
    """Rows carry this number, and the migration recomputes the ones below it."""
    assert VERSION == 2


# ── what the key separates ────────────────────────────────────────────


def test_two_cities_are_two_jobs() -> None:
    """The defect version 2 fixes. A chain employer advertising one title in two
    cities is two jobs; under version 1 they hashed together, the second
    overwrote the first, and nothing recorded that a posting had gone."""
    assert fingerprint(company=CHAIN, title=SHOP_JOB, city=ALMATY) != fingerprint(
        company=CHAIN, title=SHOP_JOB, city=ASTANA
    )


def test_the_same_job_in_the_same_city_is_one_row() -> None:
    """Which is what collapses a cross-posted job into one vacancy with two
    provenance rows instead of two rows competing in the dashboard."""
    assert fingerprint(company=CHAIN, title=SHOP_JOB, city=ALMATY) == fingerprint(
        company=CHAIN, title=SHOP_JOB, city=ALMATY
    )


def test_a_missing_city_hashes_like_an_absent_one() -> None:
    """A source that states no city and one that states an empty string are
    saying the same thing, and must not produce two rows for one job."""
    assert fingerprint(company=CHAIN, title=SHOP_JOB, city=None) == fingerprint(
        company=CHAIN, title=SHOP_JOB, city="   "
    )


def test_a_stated_city_never_hashes_like_no_city() -> None:
    """Otherwise the migration's re-key would be a silent no-op for the rows it
    exists to repair."""
    assert fingerprint(company=CHAIN, title=SHOP_JOB, city=ALMATY) != fingerprint(
        company=CHAIN, title=SHOP_JOB, city=None
    )


def test_the_key_is_forty_characters() -> None:
    """vacancy.fingerprint is String(40); a longer value is a failed insert."""
    key = fingerprint(company=CHAIN, title=SHOP_JOB, city=ALMATY)

    assert len(key) == 40


# ── what the key ignores ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Data Engineer", "  data   engineer  "),
        ("Пиксель-Мираж", "Пиксель Мираж"),
        ("Алматы", "алматы"),
        # NFKC: a full-width letter and a combining diacritic are the same name
        # typed by two sources, and without normalisation two different rows.
        ("Ａcme", "Acme"),  # noqa: RUF001
        ("Майор", "Майор"),  # noqa: RUF001
    ],
)
def test_spelling_noise_does_not_split_a_job(left: str, right: str) -> None:
    """Case, spacing, punctuation and Unicode form vary between sources
    describing one posting; none of them means a different job."""
    assert normalize_part(left) == normalize_part(right)


def test_removing_punctuation_does_not_glue_words_together() -> None:
    """A hyphen becomes a space, not nothing, so two roles that differ only in
    where the word break falls stay two roles."""
    assert normalize_part("Data Engineer") != normalize_part("DataEngineer")


def test_the_parts_cannot_be_shifted_across_the_separator() -> None:
    """A company whose name ends where a title begins must not hash like the
    same characters split one position over."""
    assert fingerprint(company="ab", title="c", city=None) != fingerprint(
        company="a", title="bc", city=None
    )


def test_the_separator_cannot_be_smuggled_in_by_a_source() -> None:
    """It is stripped as punctuation, so a payload carrying one cannot forge a
    key belonging to a different company."""
    assert "\x1f" not in normalize_part("a\x1fb")


# ── the property the migration relies on, and its limit ───────────────


def test_adding_a_city_can_only_split_never_merge() -> None:
    """Half of what ``0007_fingerprint_city`` needs, and the half that is true.

    Two rows that differed under version 1 differed in their company or their
    title, and both parts are still in the input, so recomputing every row with
    its own city cannot make any pair of them collide — whatever cities they
    carry. What it does not cover is a row that was never version 1: see
    ``test_the_backfill_refuses_when_a_re_crawled_row_already_holds_the_key``,
    where a crawl that ran before the migration has already taken the key.
    """
    postings = [
        (company, title, city)
        for company in (CHAIN, "Small Shop", None)
        for title in (SHOP_JOB, "Data Engineer")
        for city in (ALMATY, ASTANA, None)
    ]

    version_one = {(c, t): fingerprint(company=c, title=t, city=None) for c, t, _ in postings}
    version_two = {
        (c, t, city): fingerprint(company=c, title=t, city=city) for c, t, city in postings
    }

    # Distinct under version 1 in the first place: the unique constraint means
    # rows sharing a version-1 key were already one row.
    assert len(set(version_one.values())) == len(version_one)
    assert len(set(version_two.values())) == len(version_two)


# ── the migration's frozen copy ───────────────────────────────────────

MIGRATION_PATH = (
    Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0007_fingerprint_city.py"
)


def _migration() -> ModuleType:
    """Load the migration as a module. It is not importable by name: the file
    starts with a digit and its directory is not a package."""
    spec = importlib.util.spec_from_file_location("migration_0007_fingerprint_city", MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: Loaded once, because the fake connection below recognises the migration's
#: statements by identity.
MIGRATION = _migration()


#: What the frozen copy produced on the day it was written, as literals.
#:
#: Pinned this way rather than against ``app.normalize.fingerprint`` on purpose.
#: The migration exists to write *version-2* keys forever, and phase 4 is
#: expected to raise ``VERSION`` to 3 by changing the normalisation itself. An
#: equality against the live module would fail on that day and read as a broken
#: migration, when what actually happened is the design working: the copy stayed
#: put while the application moved. Digests cannot drift, so they say the thing
#: that has to stay true.
FROZEN_KEYS: list[tuple[str | None, str, str | None, str]] = [
    (CHAIN, SHOP_JOB, ALMATY, "7ab781b4b787887a1d532d91b5c12f13ab4c95c1"),
    (CHAIN, SHOP_JOB, ASTANA, "41e5b5e568dcd2fd9a2e66483f0bef6971d21b2d"),
    # The version-1 answer, and the one the migration recognises rows by.
    (CHAIN, SHOP_JOB, None, "e9827369139f32e05cd3614f0a1de9f7e252e055"),
    (None, "Data Engineer", ASTANA, "e8a80e84175209a81be68ac72a52651f2b0cef21"),
    (
        "ТОО «Пиксель-Мираж»",  # noqa: RUF001
        "Backend Engineer",
        "  Алматы  ",
        "d9039af09f933f4a10397f18294c27ccf42cc8ed",
    ),
    ("Ａcme", "QA", "", "1b047845819fe65d4b67cdfa6912c3eaf1449853"),  # noqa: RUF001
]


@pytest.mark.parametrize(("company", "title", "city", "expected"), FROZEN_KEYS)
def test_the_migration_still_writes_the_keys_it_was_written_to_write(
    company: str | None, title: str, city: str | None, expected: str
) -> None:
    """A backfilled row holds one of these forever. If the copy below the
    docstring were edited — or quietly re-pointed at the live module — every row
    it had already re-keyed would sit on a key nothing computes any more."""
    assert MIGRATION._fingerprint(company, title, city) == expected


def test_the_application_still_computes_these_keys_while_it_is_on_version_2() -> None:
    """Nothing re-keys a table twice, so while ``VERSION`` is 2 a row the crawler
    writes and a row the backfill writes have to land on the same key — otherwise
    the next crawl inserts a duplicate beside every row the migration touched.

    The guard is the point rather than an escape hatch. When phase 4 raises the
    constant, the application moves and this migration does not, and the literals
    above become the only thing it answers to.
    """
    if VERSION != 2:
        return
    for company, title, city, expected in FROZEN_KEYS:
        assert fingerprint(company=company, title=title, city=city) == expected


def test_the_migration_targets_version_two_whatever_the_application_is_on() -> None:
    """Frozen deliberately, and asserted against a literal for the same reason
    the digests are. Tying this to ``VERSION`` asserts the opposite of the design
    it looks like it protects: a phase-4 bump to 3 would then make re-running
    this file stamp ``3`` onto rows whose keys it computed with the version-2
    algorithm, and the version column would be lying about every one of them."""
    assert MIGRATION.TARGET_VERSION == 2


# ── which rows the migration touches ──────────────────────────────────
#
# What is checked here is the part that decides anything: which rows get
# re-keyed, which are left alone, where the city comes from, and whether either
# direction notices it is about to collide. The fake below stands in for the
# connection, serves canned rows and records what would have been written.
#
# It models the recovery step rather than stubbing it, because that step is
# load-bearing — without a city, recomputing a version-1 row reproduces the key
# it already holds and the whole backfill is a no-op — and a fake that answers
# it with "nothing happened" would let a broken recovery pass every test here.
#
# ``dict[str, Any]`` throughout, because a vacancy row is heterogeneous — an id,
# three nullable strings — and it is the same shape the migration itself reads;
# narrowing it here would describe the fake rather than the thing under test.


class _FakeResult:
    """Enough of a SQLAlchemy result for the migration's access patterns."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> list[dict[str, Any]]:
        """The migration only iterates this."""
        return self._rows


class _FakeConnection:
    """Serves canned vacancy rows and remembers what the migration wrote.

    ``provenance`` is what ``vacancy_source`` would yield for the recovery
    query: ``{"vacancy_id": ..., "city": ...}`` in the order the SQL's ORDER BY
    would return it, oldest first.
    """

    def __init__(
        self,
        rows_by_version: dict[int, list[dict[str, Any]]],
        provenance: list[dict[str, Any]] | None = None,
    ) -> None:
        self.rows_by_version = rows_by_version
        self.provenance = provenance or []
        self.applied: list[dict[str, Any]] = []
        self.cities: list[dict[str, Any]] = []

    def _rows(self) -> Iterator[dict[str, Any]]:
        for rows in self.rows_by_version.values():
            yield from rows

    def execute(self, statement: object, parameters: Any = None) -> _FakeResult:
        """Recognise the migration's statements by identity, not by their text."""
        if statement is MIGRATION._SELECT_RECOVERABLE_CITY:
            # The same narrowing the SQL does: version-1 rows with no city yet.
            candidates = {
                row["id"]
                for row in self.rows_by_version.get(parameters["version"], [])
                if row["city"] is None
            }
            return _FakeResult([row for row in self.provenance if row["vacancy_id"] in candidates])
        if statement is MIGRATION._SET_CITY:
            self.cities.extend(parameters)
            rows = {row["id"]: row for row in self._rows()}
            for update in parameters:
                rows[update["id"]]["city"] = update["city"]
            return _FakeResult([])
        if statement is MIGRATION._SELECT_OTHER_VERSIONS:
            return _FakeResult(
                [
                    {"fingerprint": row["fingerprint"]}
                    for version, rows in self.rows_by_version.items()
                    if version != parameters["version"]
                    for row in rows
                ]
            )
        if statement is MIGRATION._SELECT_BY_VERSION:
            return _FakeResult(list(self.rows_by_version.get(parameters["version"], [])))
        if statement is MIGRATION._APPLY:
            self.applied.extend(parameters)
            return _FakeResult([])
        raise AssertionError(f"unexpected statement: {statement}")


def _row(
    row_id: int,
    *,
    city: str | None,
    key: str | None = None,
    company: str | None = CHAIN,
    title: str = SHOP_JOB,
) -> dict[str, Any]:
    """One vacancy row, keyed the way version 1 keyed it unless told otherwise."""
    return {
        "id": row_id,
        "company": company,
        "title": title,
        "city": city,
        "fingerprint": key or fingerprint(company=company, title=title, city=city),
    }


def _drive(
    monkeypatch: pytest.MonkeyPatch,
    connection: _FakeConnection,
    direction: str,
    *,
    offline: bool = False,
) -> None:
    """Run upgrade() or downgrade() against a connection that touches no database.

    Both Alembic globals are replaced: outside a migration run they are proxies
    that raise on first use, which says nothing about this migration.
    """
    monkeypatch.setattr(MIGRATION, "op", SimpleNamespace(get_bind=lambda: connection))
    monkeypatch.setattr(MIGRATION, "context", SimpleNamespace(is_offline_mode=lambda: offline))
    getattr(MIGRATION, direction)()


def _run(
    monkeypatch: pytest.MonkeyPatch,
    direction: str,
    rows_by_version: dict[int, list[dict[str, Any]]],
    *,
    provenance: list[dict[str, Any]] | None = None,
    offline: bool = False,
) -> _FakeConnection:
    """The common case: build the fake, drive it, hand it back for assertions."""
    connection = _FakeConnection(rows_by_version, provenance)
    _drive(monkeypatch, connection, direction, offline=offline)
    return connection


@pytest.mark.parametrize(("direction", "word"), [("upgrade", "applied"), ("downgrade", "reversed")])
def test_rendering_it_as_static_sql_is_refused_in_words(
    monkeypatch: pytest.MonkeyPatch, direction: str, word: str
) -> None:
    """``--sql`` has no connection to read from, and every choice this migration
    makes comes out of the rows. Saying so beats the AttributeError a mock
    connection would otherwise raise three frames deep. Both directions, because
    a downgrade is exactly the moment somebody reaches for ``--sql`` to see what
    it would do before letting it near production."""
    with pytest.raises(RuntimeError, match=f"cannot be {word} with --sql"):
        _run(monkeypatch, direction, {1: [_row(1, city=ALMATY)]}, offline=True)


# ── the recovery step ─────────────────────────────────────────────────


def test_the_city_comes_out_of_the_stored_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    """The row itself has no city — the version-1 pipeline never wrote one — so
    the only thing that can move it to a new key is the place the connector had
    already put in ``vacancy_source.raw``. Recovery is not decoration: take it
    away and this row recomputes the fingerprint it already holds."""
    connection = _run(
        monkeypatch,
        "upgrade",
        {1: [_row(1, city=None)]},
        provenance=[{"vacancy_id": 1, "city": ALMATY}],
    )

    assert connection.cities == [{"id": 1, "city": ALMATY}]
    assert connection.applied == [
        {
            "id": 1,
            "fingerprint": fingerprint(company=CHAIN, title=SHOP_JOB, city=ALMATY),
            "version": 2,
        }
    ]


def test_the_same_row_does_not_move_when_there_is_nothing_to_recover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control for the test above, and the honest majority case: a source
    that stated no place leaves the row on version 1's own answer, so only the
    stamp changes and the row stays where every existing link points."""
    connection = _run(monkeypatch, "upgrade", {1: [_row(1, city=None)]}, provenance=[])

    assert connection.cities == []
    assert connection.applied == [
        {
            "id": 1,
            "fingerprint": fingerprint(company=CHAIN, title=SHOP_JOB, city=None),
            "version": 2,
        }
    ]


def test_the_oldest_provenance_row_supplies_the_city(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cross-posted vacancy has several, and taking whichever one the planner
    happened to return would make the backfill's output depend on row order —
    two runs of the same migration writing two different keys."""
    connection = _run(
        monkeypatch,
        "upgrade",
        {1: [_row(1, city=None)]},
        provenance=[{"vacancy_id": 1, "city": ALMATY}, {"vacancy_id": 1, "city": ASTANA}],
    )

    assert connection.cities == [{"id": 1, "city": ALMATY}]


def test_a_blank_city_falls_through_to_the_next_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """Emptiness is decided in Python, so a value SQL cannot recognise as blank —
    a tab is not a space to ``btrim`` — does not consume the vacancy's one
    chance at a city."""
    connection = _run(
        monkeypatch,
        "upgrade",
        {1: [_row(1, city=None)]},
        provenance=[{"vacancy_id": 1, "city": "\t "}, {"vacancy_id": 1, "city": ASTANA}],
    )

    assert connection.cities == [{"id": 1, "city": ASTANA}]


def test_a_row_the_migration_will_not_re_key_is_not_given_a_city_either(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Writing one would leave the column naming a place the row's own
    fingerprint knows nothing about, which is the state ``to_vacancy`` refuses
    to create for a live posting. The row is left entirely alone instead."""
    connection = _run(
        monkeypatch,
        "upgrade",
        {1: [_row(1, city=None, key="0" * 40)]},
        provenance=[{"vacancy_id": 1, "city": ALMATY}],
    )

    assert connection.cities == []
    assert connection.applied == []


# ── which rows are re-keyed ───────────────────────────────────────────


def test_the_backfill_re_keys_a_row_that_has_a_city(monkeypatch: pytest.MonkeyPatch) -> None:
    """The point of the whole migration: the row moves to the key a re-crawl of
    that same posting will now compute, so the crawl updates it instead of
    inserting a second row beside it."""
    connection = _run(monkeypatch, "upgrade", {1: [_row(1, city=ALMATY, key=VERSION_ONE_KEY)]})

    assert connection.applied == [
        {
            "id": 1,
            "fingerprint": fingerprint(company=CHAIN, title=SHOP_JOB, city=ALMATY),
            "version": 2,
        }
    ]


def test_a_key_this_algorithm_did_not_write_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dev seed invents fingerprints from an index and publishes the list it
    invented. Recomputing one would rename a row whose owner still looks it up
    by the old value, so a key that is not sha1(company, title, '') is skipped
    and stays at version 1 — which is what the version column is for."""
    connection = _run(monkeypatch, "upgrade", {1: [_row(1, city=ALMATY, key="0" * 40)]})

    assert connection.applied == []


# ── the upgrade's own collisions ──────────────────────────────────────


def test_the_backfill_refuses_when_a_re_crawled_row_already_holds_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ordinary deployment order, which the migration used to assume away.

    Code ships, the crawl runs against a table nobody has migrated yet, and one
    of a chain employer's Almaty postings gets a version-2 row of its own. The
    postings that had expired out of the sitemap still hang off the old
    version-1 row with their city in the payload — so re-keying it computes the
    fingerprint the new row is already sitting on, and ``vacancy.fingerprint``
    is UNIQUE. Against a real database this was a mid-transaction
    ``UniqueViolationError``; here it has to be a sentence and a count.
    """
    rows = {
        1: [_row(1, city=None, key=VERSION_ONE_KEY)],
        2: [_row(2, city=ALMATY)],
    }
    connection = _FakeConnection(rows, [{"vacancy_id": 1, "city": ALMATY}])

    with pytest.raises(RuntimeError, match="1 version-1 vacancies would be re-keyed onto 1"):
        _drive(monkeypatch, connection, "upgrade")

    # Nothing partially applied: the refusal happens before the first write.
    assert connection.applied == []


def test_a_key_the_migration_refuses_to_move_still_blocks_a_row_moving_onto_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A skipped row keeps its invented key, so that key is occupied even though
    nothing here wrote it. Leaving those out of the check would trade a named
    refusal for a unique violation two statements later."""
    almaty_key = fingerprint(company=CHAIN, title=SHOP_JOB, city=ALMATY)
    rows = {1: [_row(1, city=None, key=VERSION_ONE_KEY), _row(2, city=None, key=almaty_key)]}
    connection = _FakeConnection(rows, [{"vacancy_id": 1, "city": ALMATY}])

    with pytest.raises(RuntimeError, match="already taken"):
        _drive(monkeypatch, connection, "upgrade")

    assert connection.applied == []


def test_a_version_two_row_that_is_not_in_the_way_does_not_stop_the_backfill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The check must not be a blanket refusal to run on a mixed table. A crawl
    that got ahead of the migration is the normal state of a deploy, and only an
    actual clash is a reason to stop."""
    rows = {
        1: [_row(1, city=None, key=VERSION_ONE_KEY)],
        2: [_row(2, city=ASTANA)],
    }
    connection = _FakeConnection(rows, [{"vacancy_id": 1, "city": ALMATY}])

    _drive(monkeypatch, connection, "upgrade")

    assert connection.applied == [
        {
            "id": 1,
            "fingerprint": fingerprint(company=CHAIN, title=SHOP_JOB, city=ALMATY),
            "version": 2,
        }
    ]


# ── going back ────────────────────────────────────────────────────────


def test_the_downgrade_refuses_to_merge_two_cities_back_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dropping the city out of the key is the destructive direction: these two
    postings would land on one fingerprint, and one of the two jobs would have
    to go. That is not a migration's decision to make."""
    rows = {2: [_row(1, city=ALMATY), _row(2, city=ASTANA)]}

    with pytest.raises(RuntimeError, match="would merge 2 vacancies onto 1"):
        _run(monkeypatch, "downgrade", rows)


def test_the_downgrade_refuses_when_a_version_one_row_already_holds_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The collision that matters most, because ``upgrade`` leaves a mixed table
    on purpose: a key it did not write stays at version 1 for good. Dropping the
    city off the version-2 row aims it straight at that untouched row's
    fingerprint, and nothing later in the downgrade would notice."""
    rows = {
        1: [_row(9, city=None, key=VERSION_ONE_KEY)],
        2: [_row(1, city=ALMATY)],
    }

    with pytest.raises(RuntimeError, match="would merge 1 vacancies onto 1"):
        _run(monkeypatch, "downgrade", rows)


def test_the_downgrade_goes_through_when_nothing_would_merge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two employers in one city keep two keys with the city dropped, so there
    is nothing to refuse and the column returns to version 1."""
    rows = {2: [_row(1, city=ALMATY), _row(2, city=ALMATY, company="Small Shop")]}

    connection = _run(monkeypatch, "downgrade", rows)

    assert [update["version"] for update in connection.applied] == [1, 1]
    assert connection.applied[0]["fingerprint"] == VERSION_ONE_KEY


def test_the_downgrade_leaves_a_version_two_row_it_did_not_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mirror of the rule going up. A key that is not sha1 of the row's own
    company, title and city was invented by somebody else, whatever its version
    column claims, and renaming it would break whoever still looks it up. It is
    left alone — and its key still counts as occupied, so the row beside it
    cannot quietly move onto it."""
    rows = {2: [_row(1, city=ALMATY, key="0" * 40), _row(2, city=ASTANA)]}

    connection = _run(monkeypatch, "downgrade", rows)

    assert [update["id"] for update in connection.applied] == [2]


def test_the_downgrade_writes_nothing_to_a_table_it_never_touched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reversing a migration that found nothing to do must itself do nothing —
    not issue an UPDATE with an empty parameter list, which SQLAlchemy refuses
    and which would turn a harmless downgrade into a failed one."""
    connection = _run(monkeypatch, "downgrade", {1: [_row(1, city=None, key=VERSION_ONE_KEY)]})

    assert connection.applied == []


# ── what reaches the third part of the key ────────────────────────────
#
# ``stated_city`` lives in ``pipeline/runner.py`` but is tested here, with the
# key it feeds: its whole job is to decide the third hashed part, and the
# migration copies its reduction rule character for character so a backfilled
# row lands where the next crawl will look for it.


def _posting(city: str | None) -> RawPosting:
    """A posting whose connector derived exactly this city."""
    return RawPosting(
        source_slug="fake",
        external_id="1",
        url="https://example.test/1",
        title=SHOP_JOB,
        company=CHAIN,
        raw={"_derived": {"city": city}} if city is not None else {},
    )


def test_an_over_long_city_is_cut_to_the_column_width() -> None:
    """The cap is not cosmetic. ``VacancyCreate`` validates ``city`` against the
    column, and one over-long value raises inside a batch of a hundred postings,
    which loses the ninety-nine that were fine along with it."""
    city = stated_city(_posting("А" * (MAX_CITY + 40)))  # noqa: RUF001

    assert city is not None
    assert len(city) == MAX_CITY


def test_the_cut_happens_after_the_whitespace_is_collapsed() -> None:
    """Order matters, and it is the order the migration's copy uses. Cutting
    first would keep a different set of characters, and the two would then
    disagree about the key for the same posting — the exact failure the backfill
    exists to prevent."""
    spaced = "Алма" + "  " * 60 + "Ата"

    assert stated_city(_posting(spaced)) == "Алма Ата"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  Алматы  ", "Алматы"),
        ("Алматы\n", "Алматы"),
        # U+00A0 arrives readily from a Russian page and is not a space to
        # PostgreSQL's btrim, which is why this reduction is not done in SQL.
        ("\u00a0Алматы\u00a0", "Алматы"),  # noqa: RUF001
        ("Алма\u00a0\t Ата", "Алма Ата"),
        ("   ", None),
        ("", None),
        (None, None),
    ],
)
def test_whitespace_is_collapsed_and_an_empty_result_is_no_city(
    raw: str | None, expected: str | None
) -> None:
    """A place name is one line of text however the page spelled it, and a value
    that is nothing but spacing is a source saying nothing rather than a source
    naming a city called "  "."""
    assert stated_city(_posting(raw)) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "  Алматы  ",
        "Алматы\n",
        "\u00a0Алматы\u00a0",  # noqa: RUF001
        "Алма\u00a0\t Ата",
        "А" * 400,  # noqa: RUF001
        "   ",
        "",
        None,
    ],
)
def test_the_migration_reduces_a_recovered_city_exactly_as_the_pipeline_does(
    raw: str | None,
) -> None:
    """This is the assertion the backfill's whole value rests on. The migration
    writes ``vacancy.city`` and hashes it; the next crawl of the same posting
    reduces the connector's value with ``stated_city`` and hashes that. If the
    two disagree by one character the crawl inserts a duplicate row beside every
    row the migration touched — which is the loss it was written to stop."""
    assert MIGRATION._stated_city(raw) == stated_city(_posting(raw))
