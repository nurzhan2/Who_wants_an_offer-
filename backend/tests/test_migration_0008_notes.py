"""The only code in migration 0008 that can lose something a person wrote.

``application.notes`` is a free-text field. The previous session's agent wrote a
labelled block into the end of it, because the columns this migration adds did
not exist yet, and said so in the commit message. This migration moves that block
into the columns.

The dangerous half is not the move — it is the deletion afterwards. ``merge_notes``
appended the block, so a person who came back and typed another sentence typed it
*below* the block, and a parser that reads every line after the marker as more of
the agent's output will move that sentence into a column documented as a quotation
from hh and remove it from the only place it existed.

None of that logic had a test. It is exercised here directly, against the
migration module rather than through a database, because what is at risk is the
parsing rather than the DDL: the round trip on a live database is covered by
``test_migrations.py`` and there are no rows carrying a block on this one.
"""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit

MIGRATION = Path(__file__).resolve().parents[1] / "alembic" / "versions"
PATH = MIGRATION / "0008_application_send_record.py"


def _module() -> ModuleType:
    """The migration as an importable module.

    Alembic loads these by path and they are not a package, so the usual import
    does not reach them. Loading it here is what lets the parsing be tested at
    all — the alternative is asserting about a live database and learning
    nothing about the branch that drops text.
    """
    spec = importlib.util.spec_from_file_location("migration_0008", PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


m = _module()


def _block(**fields: str) -> str:
    """An agent block exactly as ``render_outcome`` wrote one."""
    labels = dict((column, label) for label, column in m.LABELS)
    lines = [labels[column] + value for column, value in fields.items()]
    return m.NOTES_MARKER + "\n" + "\n".join(lines)


def test_a_person_who_typed_above_the_block_keeps_their_text() -> None:
    """The ordinary case: the agent appended, so everything before the marker is theirs."""
    notes = "Позвонить в понедельник.\n\n" + _block(agent_status="sent", hh_last_state="DISCARD")

    kept, block = m._split(notes)

    assert kept == "Позвонить в понедельник."
    assert m._tail_is_only_the_block(block)
    assert m._parse(block, "id")["hh_last_state"] == "DISCARD"


def test_a_person_who_typed_below_the_block_keeps_their_text_too() -> None:
    """The case that loses data, and the reason this file exists.

    A sentence added under the block is separated from it by a blank line — that
    is what a text field does — and the renderer never emits a blank line. So the
    tail cannot be accounted for, and nothing is deleted.
    """
    notes = _block(agent_status="sent", hh_last_state="DISCARD") + "\n\nОни перезвонили 8-го."

    _, block = m._split(notes)

    assert not m._tail_is_only_the_block(block), "a person's sentence is not the agent's output"


def test_a_multi_line_reason_is_still_the_agents_own() -> None:
    """``agent_reason`` is prose and the renderer could not escape a newline in it.

    An unlabelled line that continues a labelled one is the agent's; only a line
    after a blank one is in doubt. Getting this wrong in the other direction
    would leave every reason-carrying row uncleaned for no reason.
    """
    block = _block(agent_status="needs_manual", agent_reason="нужно письмо,\nа его нет")[
        len(m.NOTES_MARKER) :
    ]

    assert m._tail_is_only_the_block(block)
    assert m._parse(block, "id")["agent_reason"] == "нужно письмо,\nа его нет"


def test_an_unreadable_number_is_left_empty_rather_than_guessed() -> None:
    """The column is read as hh's own count; a wrong integer is worse than none."""
    block = _block(agent_status="sent", hh_negotiations_total="неизвестно")[len(m.NOTES_MARKER) :]

    assert "hh_negotiations_total" not in m._parse(block, "id")


def test_something_too_long_for_its_column_is_left_empty_rather_than_cut() -> None:
    """These columns are quotations. A truncated quotation is a misquotation."""
    column, width = next(iter(m.WIDTHS.items()))
    block = _block(**{column: "я" * (width + 1)})[len(m.NOTES_MARKER) :]

    assert column not in m._parse(block, "id")


@pytest.mark.parametrize(
    "tail",
    [
        "\nстатус: sent",
        "\nстатус: sent\nпричина: письмо\nещё строка причины",
        "\n\n",
        "",
    ],
)
def test_a_tail_the_renderer_could_have_written_is_removable(tail: str) -> None:
    """The negative side, so the guard cannot become "never clean anything"."""
    assert m._tail_is_only_the_block(tail)


class _FakeResult:
    """What ``connection.execute`` returns for the SELECT."""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def mappings(self) -> "_FakeResult":
        """Alembic's chain; the rows are already mappings here."""
        return self

    def all(self) -> list[dict[str, object]]:
        """Every row the migration will walk."""
        return self._rows


class _FakeConnection:
    """Records the UPDATE parameters instead of running them."""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows
        self.applied: list[dict[str, object]] = []

    def execute(self, statement: object, parameters: dict[str, object]) -> _FakeResult:
        """SELECT hands back the seeded rows; UPDATE is recorded."""
        if "marker" in parameters:
            return _FakeResult(self._rows)
        self.applied.append(parameters)
        return _FakeResult([])


def _run_backfill(notes: str, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Drive the real ``_backfill_from_notes`` over one row."""
    connection = _FakeConnection([{"id": "row-1", "notes": notes, "applied_at": None}])
    monkeypatch.setattr(m.op, "get_bind", lambda: connection)

    m._backfill_from_notes()

    assert len(connection.applied) == 1
    return connection.applied[0]


def test_the_backfill_deletes_the_block_when_it_can_account_for_all_of_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ordinary case still gets cleaned, or the guard would be a way of doing nothing."""
    notes = "Позвонить в понедельник.\n\n" + _block(agent_status="sent")

    applied = _run_backfill(notes, monkeypatch)

    assert applied["notes"] == "Позвонить в понедельник."
    assert applied["agent_status"] == "sent"


def test_the_backfill_leaves_the_field_alone_when_a_person_wrote_below_the_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The columns are still filled; the person's field is returned untouched.

    This pins the WIRING, not the predicate. Reverting the call site to an
    unconditional strip leaves every other test in this file green, because they
    exercise ``_tail_is_only_the_block`` directly — and a guard nothing calls is
    the shape several defects in this repository have already taken.
    """
    notes = _block(agent_status="sent") + "\n\nОни перезвонили 8-го."

    applied = _run_backfill(notes, monkeypatch)

    assert applied["notes"] == notes, "nothing a person wrote may be dropped"
    assert applied["agent_status"] == "sent", "the columns are still filled"
