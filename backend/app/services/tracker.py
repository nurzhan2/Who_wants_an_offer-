"""The applications board: where each application is, and what came back.

Read-only, and that is a rule of the product rather than a stage of the work.
An application is sent by ``wwao apply --send``, where a person is at the
keyboard and confirms that letter for that vacancy; a browser cannot offer the
same guarantee — a page can be left open, reloaded, clicked by the wrong tab —
so there is no send here and there is not going to be one. This module has no
writes at all, which is the cheapest way to keep that true.

**Two axes, not one.** Where an application is in *this* project's pipeline
(queued, set aside for a person, sent) and what hh has since said about it are
different questions, and an application that went out has an answer to both. A
single column list would have to drop one of them: either a sent application
stops being sent the moment hh views it, or an outcome cannot be shown at all.
So the board has a row of stages and a row of outcomes, and a sent application
appears in one of each.

**hh's vocabulary stays hh's.** ``hh_last_state`` is passed through as the
string hh wrote. The set is open — hh adds states — and freezing it into an enum
here would turn "hh reported something new" into "this application has no
outcome". The frontend maps the states it recognises onto Russian words and
shows the rest verbatim, which is the same trade one layer up.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.application import ApplicationRepository
from app.schemas.dashboard import Board, BoardCard, BoardColumn

#: The stages, in the order they happen. ``queued`` and ``needs_manual`` are the
#: agent's own words for its state machine (``agent/state.py``); ``sent`` is not
#: read off that at all — see :func:`_stage_of`.
QUEUED = "queued"
NEEDS_MANUAL = "needs_manual"
#: The agent reported a send and hh has not confirmed it yet. Its own column
#: since 2026-09-16: the first real run reported four sends whose confirmation
#: never reached the tracker, and a column called «отправлено» has to mean what
#: hh says, not what our process believed. ``python -m wwao outcomes`` reads the
#: page again and moves such a row to ``sent`` or leaves it here for a person.
SENT_UNCONFIRMED = "sent_unconfirmed"
SENT = "sent"
STAGES = (QUEUED, NEEDS_MANUAL, SENT_UNCONFIRMED, SENT)

#: hh's states, grouped into the four answers a person actually wants: it was
#: looked at, it is waiting, they want to talk, they said no. The mapping covers
#: what has been observed live and what hh's own dictionary lists; anything else
#: lands in :data:`UNKNOWN_OUTCOME` under its own name rather than being forced
#: into one of these.
OUTCOME_GROUPS: dict[str, tuple[str, ...]] = {
    "viewed": ("RESPONSE", "VIEWED"),
    "waiting": ("PENDING", "NEW", "AWAITING"),
    "invitation": ("INVITATION", "INTERVIEW", "PHONE_INTERVIEW", "OFFER"),
    "rejection": ("DISCARD", "REJECTED", "DECLINE"),
}
UNKNOWN_OUTCOME = "other"
OUTCOMES = (*OUTCOME_GROUPS, UNKNOWN_OUTCOME)


async def board(session: AsyncSession) -> Board:
    """Every tracked application, on both axes."""
    cards = await ApplicationRepository(session).board()

    stages: dict[str, list[BoardCard]] = {key: [] for key in STAGES}
    outcomes: dict[str, list[BoardCard]] = {key: [] for key in OUTCOMES}
    other: list[BoardCard] = []

    for card in cards:
        stage = _stage_of(card)
        if stage is None:
            other.append(card)
        else:
            stages[stage].append(card)
        outcome = _outcome_of(card)
        if outcome is not None:
            outcomes[outcome].append(card)

    return Board(
        stages=[BoardColumn(key=key, cards=stages[key]) for key in STAGES],
        # An outcome column with nothing in it is dropped rather than shown
        # empty: "no invitations" is a fact this data cannot support with two
        # applications sent, and an empty column labelled «приглашение» reads as
        # a measurement.
        outcomes=[BoardColumn(key=key, cards=outcomes[key]) for key in OUTCOMES if outcomes[key]],
        other=BoardColumn(key="other", cards=other),
    )


def _stage_of(card: BoardCard) -> str | None:
    """Which stage column this row belongs in, or None for a row in neither.

    ``sent_at`` decides that something was sent and ``agent_status`` does not,
    even though the agent writes both; hh's own count or state then decides
    whether the send is confirmed. The column is a claim that an application actually went
    out, and only the process that did the typing writes ``sent_at``; a status
    of ``sent`` on a row with no timestamp is a report that lost its own
    evidence, and showing it beside real ones would make the column unusable as
    a count.

    A row that is in neither — typed into the tracker by hand, never queued,
    never sent — is not forced into ``queued``. It is somebody's own note about
    a job, and putting it in a queue would say the agent is about to act on it.
    """
    if card.sent_at is not None:
        return SENT if card.send_confirmed else SENT_UNCONFIRMED
    if card.agent_status in {QUEUED, NEEDS_MANUAL}:
        return card.agent_status
    return None


def _outcome_of(card: BoardCard) -> str | None:
    """What hh has said about this application, grouped.

    None when hh has said nothing, which is the ordinary state of a fresh
    application and is not the same as "no answer": an application sent
    yesterday has no outcome, an application sent in June that hh still reports
    as ``RESPONSE`` has one and it is silence.
    """
    state = (card.hh_last_state or "").strip().upper()
    if not state:
        return None
    for group, members in OUTCOME_GROUPS.items():
        if state in members:
            return group
    return UNKNOWN_OUTCOME
