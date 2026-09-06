"""What can happen to one application, and who is allowed to make it happen.

The brief names six states. What it does not name, and what turns out to matter
more, is *which actor* may move between them. Two of these transitions are
decisions a person makes and the rest are things a machine observes, and writing
that distinction down is most of the safety of this module: a machine that could
move an item into ``CONFIRMED`` would be a machine that confirms.

    QUEUED ──human──▶ CONFIRMED ──▶ SENT
      │                   │
      │                   └──▶ FAILED ──▶ QUEUED   (a person retries, not a loop)
      ├──▶ SKIPPED          (already applied, closed, archived — nothing to do)
      └──▶ NEEDS_MANUAL ──human──▶ QUEUED | SKIPPED

``SENT`` and ``SKIPPED`` are terminal. ``SENT`` especially: there is no
transition out of it, because there is no way back in the world either, and a
state machine that can leave it invites code that treats an application as
retryable.

``FAILED`` returns to ``QUEUED`` only by a person's hand. That is the brief's
"stop after the second consecutive error" expressed structurally — a run cannot
put an item back in its own queue, so a retry storm is not something this design
can express, whatever a future caller does with a ``for`` loop.
"""

from enum import StrEnum
from typing import Final, final


class Status(StrEnum):
    """Where one application has got to."""

    #: Selected for this run and not yet shown to anybody.
    QUEUED = "queued"
    #: A person read exactly this payload and said yes. Only a person.
    CONFIRMED = "confirmed"
    #: The application left. Terminal, because it cannot be recalled.
    SENT = "sent"
    #: Something went wrong. A person decides whether it is worth another go.
    FAILED = "failed"
    #: A person has to do this one by hand: an employer test, an external
    #: application form, a letter this agent will not send, or an idempotency
    #: reading it did not recognise.
    NEEDS_MANUAL = "needs_manual"
    #: Nothing to do: already applied, closed, archived.
    SKIPPED = "skipped"


class Actor(StrEnum):
    """Who is making a transition. Not decoration — see ``TRANSITIONS``."""

    HUMAN = "human"
    AGENT = "agent"


#: Every legal move, as (from, to) -> who may make it. Anything absent is
#: illegal; see :func:`check`. Written as data rather than as ``if`` branches so
#: that the whole policy is readable in one screen and testable as a table.
TRANSITIONS: Final[dict[tuple[Status, Status], Actor]] = {
    # Only a person confirms. This single line is the boundary the brief calls
    # «НЕ отправляет отклик без подтверждения человека».
    (Status.QUEUED, Status.CONFIRMED): Actor.HUMAN,
    # The agent may observe that there is nothing to do, or that a human is
    # needed, but it may never observe that a human agreed.
    (Status.QUEUED, Status.SKIPPED): Actor.AGENT,
    (Status.QUEUED, Status.NEEDS_MANUAL): Actor.AGENT,
    (Status.QUEUED, Status.FAILED): Actor.AGENT,
    (Status.CONFIRMED, Status.SENT): Actor.AGENT,
    (Status.CONFIRMED, Status.FAILED): Actor.AGENT,
    (Status.CONFIRMED, Status.NEEDS_MANUAL): Actor.AGENT,
    (Status.CONFIRMED, Status.SKIPPED): Actor.AGENT,
    # A retry is a decision, not a loop.
    (Status.FAILED, Status.QUEUED): Actor.HUMAN,
    (Status.NEEDS_MANUAL, Status.QUEUED): Actor.HUMAN,
    (Status.NEEDS_MANUAL, Status.SKIPPED): Actor.HUMAN,
}

#: States nothing leaves. ``SENT`` is here because the world will not take it
#: back; ``SKIPPED`` because re-examining it is what the next run is for.
TERMINAL: Final[frozenset[Status]] = frozenset({Status.SENT, Status.SKIPPED})


@final
class IllegalTransitionError(Exception):
    """A move this state machine does not have, or not for this actor."""

    def __init__(self, source: Status, target: Status, actor: Actor) -> None:
        self.source, self.target, self.actor = source, target, actor
        super().__init__(
            f"{actor.value} may not move an application from {source.value} to {target.value}"
        )


def check(source: Status, target: Status, *, actor: Actor) -> None:
    """Raise unless this actor may make this move. Never returns a bool.

    A predicate would be checked in some call sites and forgotten in others.
    Raising means the only way to skip the check is to not call the function
    that performs the move, which a reader notices.
    """
    allowed = TRANSITIONS.get((source, target))
    if allowed is None or allowed is not actor:
        raise IllegalTransitionError(source, target, actor)
