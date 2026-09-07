"""Give the agent's report columns of its own, and give the notes back to the person.

``application`` could hold a status, a date, a note and a letter. The apply
agent reports rather more than that — which letter it typed, what hh said about
the application, hh's own count of applications on the vacancy, and later the
state hh moved it to — and none of it had anywhere to go, so
``app/services/agent_queue.py`` rendered the lot into a marked text block inside
``application.notes``. The session that shipped that called it the weakest part
of the change in its own commit message, and it was right twice over. A blob
cannot be filtered, grouped or counted, so the one question the data exists to
answer — did the high scores answer more often, which missing requirement costs
a reply — could not be asked at all. And ``notes`` is the person's column: a
program that rewrites it on every result is writing over somebody's sentences.

**What is added, and why each is the shape it is.**

Scalars a dashboard groups by are columns: ``sent_at``, ``match_score``,
``match_bucket``, ``hh_negotiations_total``, ``hh_last_state``, ``agent_status``.
Prose that is read whole is text: ``sent_letter``, ``agent_reason``, and hh's
two lines, quoted verbatim and never parsed into a verdict of ours, because a
vacancy page belongs to somebody else. One document nothing filters on is JSONB:
``match_explanation``, shaped as ``app.schemas.agent.MatchExplanation``. One
list is JSONB because it is a list: ``vacancy_key_skills``.

Several of them duplicate data that exists elsewhere in normalised form, and
that is the point rather than an oversight. ``match`` rows are rewritten by
every scoring run, ``cover_letter`` by every regeneration, and a re-crawl
rewrites the posting. A snapshot taken at the send is the only way the question
"what did the employer read, and what did we believe when we sent it" still has
an answer in a month.

Nothing is indexed. This is one person's tracker — tens of rows — and what makes
``GROUP BY hh_last_state`` possible is that the values are columns, not that
they are indexed. An index here would cost a write on every result to save
nothing measurable on a sequential scan of a hundred rows.

**The existing blocks are parsed forward, not left behind.**

The previous session wrote that block as one field per line with fixed labels
"so that the day the column exists, backfilling it is a parse rather than an
archaeology". This is that day, and taking it up is what makes the earlier
decision cheap in hindsight instead of a shrug. The block is then removed from
``notes`` and everything above the marker — the person's own text — is kept
byte for byte. Copying without removing would leave the owner reading two
copies of hh's warning, one of which stops updating.

Two fields are recovered beyond the block's own six. ``sent_at`` is taken from
``applied_at`` when the block says ``статус: sent``, because the same function
wrote both in the same transaction, so that timestamp *is* the moment of the
send rather than a guess about it. Nothing else is invented: ``sent_letter``,
the match snapshot and ``vacancy_key_skills`` stay NULL on a backfilled row,
because reconstructing them from today's ``match`` and today's ``cover_letter``
would record what this project believes now as what it believed then, which is
the exact confusion the snapshot exists to prevent. ``hh_last_state_at`` stays
NULL too, even where ``hh_last_state`` was recovered: the text never carried a
date, and a time-to-answer computed from an invented one would be a statistic
about nothing.

**Going back.** :func:`downgrade` re-renders the block, byte for byte as
``render_outcome`` did, so a row that came up through :func:`upgrade` goes back
down to the text it started as. What the old format could not express is lost
with the columns that held it — the sent letter, the match snapshot, the key
skills, both timestamps. That is what dropping a column means; it is said here
rather than discovered afterwards, and an operator who cares about those five
should dump the table before running it.

Revision ID: 0008_application_send_record
Revises: 0007_fingerprint_city
Create Date: 2026-09-07
"""

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_application_send_record"
down_revision: str | None = "0007_fingerprint_city"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Not structlog: this file runs under Alembic's own logging configuration from
#: alembic.ini, and importing the application's logger would tie a frozen
#: migration to a module that keeps moving. English and ASCII, so a cp1251
#: console can print every line of it.
logger = logging.getLogger("alembic.runtime.migration")

#: ``app.db.enums.MatchBucket``, frozen. The type already exists — 0001 created
#: it for ``match.bucket`` — so the column is added with ``create_type=False``
#: and neither :func:`upgrade` nor :func:`downgrade` touches the type itself.
MATCH_BUCKET_VALUES = ("apply_now", "strong", "stretch", "skip", "filtered")

#: ``agent_queue.NOTES_MARKER`` as it was, frozen. Everything from this line
#: down inside ``application.notes`` was the agent's; everything above it is the
#: person's and is not touched by either direction of this migration.
NOTES_MARKER = "--- агент ---"

#: The block's labels, in the order ``render_outcome`` wrote them, each with the
#: column it moves into. Order matters in both directions: :func:`downgrade`
#: rebuilds the block from it, and a rebuilt block has to be the text the
#: previous code would have written, or a downgrade followed by an upgrade would
#: not land where it started.
LABELS: tuple[tuple[str, str], ...] = (
    ("статус: ", "agent_status"),
    ("причина: ", "agent_reason"),
    ("hh, блокирующее требование: ", "hh_blocking_warning"),
    ("hh, предупреждение: ", "hh_warning"),
    ("откликов по данным hh (negotiations.total): ", "hh_negotiations_total"),
    ("состояние отклика (lastState): ", "hh_last_state"),
)

#: Column widths, so a value the text cannot fit is dropped with a warning
#: rather than silently cut. A truncated ``hh_last_state`` would be a state hh
#: never reported, and this table is read as evidence.
WIDTHS: dict[str, int] = {"agent_status": 20, "hh_last_state": 100}

#: The one status that also fills ``sent_at``. Frozen copy of
#: ``app.schemas.agent.AgentStatus.SENT``.
SENT = "sent"

_SELECT_BLOCKS = sa.text(
    "SELECT id, notes, applied_at FROM application "
    "WHERE notes IS NOT NULL AND strpos(notes, :marker) > 0 ORDER BY id"
)

_APPLY_BLOCK = sa.text(
    """
    UPDATE application
       SET notes = :notes,
           agent_status = :agent_status,
           agent_reason = :agent_reason,
           hh_blocking_warning = :hh_blocking_warning,
           hh_warning = :hh_warning,
           hh_negotiations_total = :hh_negotiations_total,
           hh_last_state = :hh_last_state,
           sent_at = :sent_at
     WHERE id = :id
    """
)

_SELECT_RECORDED = sa.text(
    """
    SELECT id, notes, agent_status, agent_reason, hh_blocking_warning,
           hh_warning, hh_negotiations_total, hh_last_state
      FROM application
     WHERE agent_status IS NOT NULL
        OR agent_reason IS NOT NULL
        OR hh_blocking_warning IS NOT NULL
        OR hh_warning IS NOT NULL
        OR hh_negotiations_total IS NOT NULL
        OR hh_last_state IS NOT NULL
     ORDER BY id
    """
)

_RESTORE_NOTES = sa.text("UPDATE application SET notes = :notes WHERE id = :id")


def upgrade() -> None:
    """Add the columns, then move the text block into them."""
    op.add_column("application", sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("application", sa.Column("sent_letter", sa.Text(), nullable=True))
    op.add_column("application", sa.Column("agent_status", sa.String(length=20), nullable=True))
    op.add_column("application", sa.Column("agent_reason", sa.Text(), nullable=True))
    op.add_column("application", sa.Column("match_score", sa.Numeric(5, 2), nullable=True))
    op.add_column(
        "application",
        sa.Column(
            "match_bucket",
            postgresql.ENUM(*MATCH_BUCKET_VALUES, name="match_bucket", create_type=False),
            nullable=True,
        ),
    )
    op.add_column(
        "application",
        sa.Column("match_explanation", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "application",
        sa.Column("vacancy_key_skills", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column("application", sa.Column("hh_warning", sa.Text(), nullable=True))
    op.add_column("application", sa.Column("hh_blocking_warning", sa.Text(), nullable=True))
    op.add_column("application", sa.Column("hh_negotiations_total", sa.Integer(), nullable=True))
    op.add_column("application", sa.Column("hh_last_state", sa.String(length=100), nullable=True))
    op.add_column(
        "application", sa.Column("hh_last_state_at", sa.DateTime(timezone=True), nullable=True)
    )

    _backfill_from_notes()


def downgrade() -> None:
    """Put the block back where it was, then drop the columns.

    Run in that order on purpose: the rendering reads the columns, so dropping
    them first would leave nothing to render and the loss would be total instead
    of partial.
    """
    _restore_notes()

    for name in (
        "hh_last_state_at",
        "hh_last_state",
        "hh_negotiations_total",
        "hh_blocking_warning",
        "hh_warning",
        "vacancy_key_skills",
        "match_explanation",
        "match_bucket",
        "match_score",
        "agent_reason",
        "agent_status",
        "sent_letter",
        "sent_at",
    ):
        op.drop_column("application", name)


# ── the block, parsed out ──────────────────────────────────────────────


def _backfill_from_notes() -> None:
    """Move every agent block out of ``notes`` and into the new columns."""
    connection = op.get_bind()
    rows = connection.execute(_SELECT_BLOCKS, {"marker": NOTES_MARKER}).mappings().all()
    if not rows:
        logger.info("0008: no agent block found in application.notes; nothing to move")
        return

    left_alone = 0
    for row in rows:
        kept, block = _split(str(row["notes"]))
        agent_part, whole = _agent_lines(block)
        parsed = _parse(agent_part, str(row["id"]))
        sent_at = row["applied_at"] if parsed.get("agent_status") == SENT else None
        # Copying into the columns is always safe; rewriting the person's field
        # is not. When the tail holds anything the renderer would not have
        # written, the columns are still filled and ``notes`` is left exactly as
        # it was — a duplicated paragraph rather than a deleted sentence.
        strip = whole
        if not strip:
            left_alone += 1
            logger.warning(
                "0008: application %s has text of its own below the agent block; "
                "columns filled, notes left untouched",
                row["id"],
            )
        connection.execute(
            _APPLY_BLOCK,
            {
                "id": row["id"],
                "notes": (kept or None) if strip else str(row["notes"]),
                "sent_at": sent_at,
                **{column: parsed.get(column) for _, column in LABELS},
            },
        )
    logger.info(
        "0008: moved %d agent block(s) out of application.notes; "
        "%d row(s) kept their notes because a person had written below the block",
        len(rows) - left_alone,
        left_alone,
    )


def _split(notes: str) -> tuple[str, str]:
    """The person's text and the agent's block, in that order.

    ``rstrip`` on the kept half reproduces ``merge_notes`` exactly, so a row
    that goes down and comes back up again lands on the same bytes.

    Only the head is ever treated as the person's. Whether the TAIL is safe to
    remove is a separate question with a worse failure mode, and
    :func:`_tail_is_only_the_block` answers it before anything is deleted.
    """
    head, _, tail = notes.partition(NOTES_MARKER)
    return head.rstrip(), tail


def _agent_lines(block: str) -> tuple[str, bool]:
    """The part of the tail the renderer could have written, and whether that is all of it.

    Splitting rather than merely judging, because the judgement alone is not
    enough: ``_parse`` attaches an unlabelled line to the label above it, so a
    person's sentence sitting under the block is absorbed into whichever column
    came last — and then dropped by the width check, taking the real value with
    it. Measured: a row whose notes ended «…\n\nОни перезвонили 8-го.» parsed to
    ``agent_status = None`` instead of ``"sent"``.

    So the accountable prefix is parsed and the rest is not looked at.
    """
    kept: list[str] = []
    closed = False
    for line in block.splitlines(keepends=True):
        if any(line.startswith(label) for label, _ in LABELS):
            closed = False
        elif not line.strip():
            closed = True
        elif closed:
            return "".join(kept), False
        kept.append(line)
    return "".join(kept), True


def _tail_is_only_the_block(block: str) -> bool:
    """Whether everything after the marker is something the agent wrote.

    ``notes`` is a free-text field a person types into, and ``merge_notes`` put
    the agent's block at the END of it. So a person who came back and added a
    sentence added it *below* the block — which is exactly where this migration
    would otherwise read it as more of hh's words, move it into a column
    documented as a quotation from hh, and delete it from the only place it
    existed.

    The rule is therefore: account for every line, or touch nothing. A line
    counts as the agent's when it carries one of :data:`LABELS`, or when it
    continues a labelled line that has not yet been closed by a blank one —
    ``agent_reason`` is prose and the renderer had no way to escape a newline in
    it. A blank line closes the run, because the renderer never emits one.
    Anything after that is the person's, and its presence means the block cannot
    be told from their text with certainty. Uncertainty keeps the text.

    Deliberately asymmetric. Failing to clean a notes field leaves a duplicate
    paragraph a person can delete in a second; getting it wrong destroys
    something they wrote and cannot get back.
    """
    return _agent_lines(block)[1]


def _parse(block: str, application_id: str) -> dict[str, object]:
    """One block as column values.

    A line with no label belongs to the field above it: ``reason`` is free text
    written by a person for a person and may well contain a newline, and the
    renderer had no way to escape one.

    Anything that will not fit its column, or will not parse as the number it
    claims to be, is dropped with a warning naming the row. Storing a truncated
    version of what hh said would be worse than storing nothing: the column is
    read as a quotation.
    """
    values: dict[str, object] = {}
    current: str | None = None
    for line in block.splitlines():
        labelled = next((pair for pair in LABELS if line.startswith(pair[0])), None)
        if labelled is not None:
            label, current = labelled
            values[current] = line[len(label) :]
        elif current is not None and line:
            values[current] = f"{values[current]}\n{line}"

    total = values.pop("hh_negotiations_total", None)
    if isinstance(total, str):
        if total.strip().isdigit():
            values["hh_negotiations_total"] = int(total.strip())
        else:
            logger.warning(
                "0008: application %s has an unreadable negotiations total; left empty",
                application_id,
            )

    for column, width in WIDTHS.items():
        value = values.get(column)
        if isinstance(value, str) and len(value) > width:
            logger.warning(
                "0008: application %s has a %s longer than %d characters; left empty",
                application_id,
                column,
                width,
            )
            values.pop(column)
    return values


# ── the block, put back ────────────────────────────────────────────────


def _restore_notes() -> None:
    """Render what the old format could hold back into ``notes``."""
    connection = op.get_bind()
    rows = connection.execute(_SELECT_RECORDED).mappings().all()
    if not rows:
        logger.info("0008: no recorded agent result to render back into application.notes")
        return

    for row in rows:
        block = _render(row)
        kept = str(row["notes"] or "").split(NOTES_MARKER)[0].rstrip()
        connection.execute(
            _RESTORE_NOTES,
            {"id": row["id"], "notes": f"{kept}\n\n{block}" if kept else block},
        )
    logger.info(
        "0008: rendered %d agent result(s) back into application.notes; "
        "the sent letter, the match snapshot, the key skills and both timestamps "
        "are dropped with their columns",
        len(rows),
    )


def _render(row: sa.RowMapping) -> str:
    """``agent_queue.render_outcome`` as it was, driven by the columns.

    Frozen here rather than imported for the reason 0007 gives about the
    fingerprint algorithm: a migration has to produce the same text forever,
    and the module it came from has already stopped writing this format.
    """
    lines = [NOTES_MARKER]
    for label, column in LABELS:
        value = row[column]
        if value is not None and value != "":
            lines.append(f"{label}{value}")
    return "\n".join(lines)
