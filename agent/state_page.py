"""Reading hh's page state: whether an application exists, and what the form says.

The vacancy page boots its frontend from a JSON blob in a hidden template. The
crawler in ``backend/app/sources/hh.py`` reads the same blob for the same
reason, and the two do not share code on purpose: that one is anonymous,
read-only and runs on a server, this one runs in the owner's browser under their
account, and a shared helper would be a thread between two programs that must
not have one.

What this module adds over the crawler's version is the applicant's half of the
page — ``applicantVacancyResponseStatuses`` — and the modal that opens on top of
it.

**Idempotency is a number, not an element and not a flag.** Measured on
2026-09-06 on the owner's logged-in profile, on two vacancies:

    "applicantVacancyResponseStatuses": {
      "136962420": {"negotiations": {"topicList": [], "total": 0, ...}, ...},
      "133542745": {"negotiations": {"topicList": [{...}], "total": 1, ...}, ...}
    }

``negotiations.total`` is the signal: ``0`` with an empty ``topicList`` means no
application exists, ``>= 1`` means one does. Two nearby things look like the
answer and are not, and both were measured:

* The apply control. A vacancy already applied to has no
  ``vacancy-response-link-top`` at all — it has ``…-top-again`` («Отклик другим
  резюме»), because hh *allows* a repeat application. So the presence of an
  apply button says nothing about whether one was already sent, and a DOM-based
  check reads "there is a button, therefore go ahead" on exactly the vacancies
  where it must not.
* The key literally called ``alreadyApplied``. On vacancy 133542745, which has
  ``negotiations.total == 1``, that key was ``false``. A name is not a
  measurement, and this is the cheapest possible demonstration of it.

:func:`read_applied` therefore stays three-valued. ``None`` means the page did
not answer in a way this code recognises, and it routes the vacancy to a person.
A design where an unreadable shape means "not applied yet" is a design that
starts double-applying, to every vacancy at once, on the day hh renames a key —
and double-applying is the one thing the owner cannot undo.

``topicList`` carries the outcome for free: ``lastState`` was ``DISCARD`` on the
applied vacancy, which the site renders as «Вам отказали». It costs nothing to
read on a page already loaded, so :func:`read_negotiations` exposes it and the
caller can persist it. The set of states is treated as open — ``RESPONSE`` and
``DISCARD`` are what was seen, and inventing the rest would be guessing.

**Corrected 2026-09-07: the list is a second stop, never a second permission.**
This module used to say ``total`` was the whole signal and
:attr:`Negotiations.exists` used nothing else, so a page reporting ``total == 0``
beside a NON-EMPTY ``topicList`` read as "no application exists" and the agent
applied. Nobody has measured that disagreement, which is exactly why it must not
be decided in favour of sending: every other unfamiliar shape in this module
stops, and this one shape resolved toward the irreversible act. A list of
conversations hh remembers against this vacancy is now read as an application
too. Note the direction — the list can only ever *add* an application, never take
one away, so this cannot turn a stop into a send.
"""

import html as html_lib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, final

#: The same marker the crawler reads. Duplicated rather than imported: see the
#: module docstring on why these two programs do not share code.
STATE_MARKER: Final[re.Pattern[str]] = re.compile(
    r'<template[^>]*id="HH-Lux-InitialState"[^>]*>(.*?)</template>', re.DOTALL
)

#: Whether anyone has ever seen what an already-sent application looks like in
#: the page state. ``agent/run.py`` asks exactly this at startup and refuses to
#: send while the answer is no, because an agent that cannot tell a fresh vacancy
#: from one it has already applied to must not apply to anything. The answer is
#: yes: measured on 2026-09-06 on the owner's profile, at ``total == 0`` and at
#: ``total == 1``.
#:
#: **Removed 2026-09-07: ``APPLIED_MARKERS``.** It was a one-element tuple of key
#: paths kept alive by a docstring claiming ``run.py`` imported it and a test
#: monkeypatched it. Neither was true — ``run.py`` imports this flag, and no test
#: mentioned either name — so it was a constant whose only content was a wrong
#: explanation of why it existed.
APPLIED_SIGNAL_MEASURED: Final[bool] = True

#: The console this runs on encodes cp1251. Text quoted from hh — a warning, an
#: employer's test question — is not under our control and one character outside
#: that codepage raises ``UnicodeEncodeError`` at print time, in the middle of a
#: run, rather than in a test.
CONSOLE_ENCODING: Final[str] = "cp1251"

#: Characters hh can put *inside* a word that :meth:`str.split` will not remove.
#: All of them are invisible in a browser and in an editor, and one between
#: «видим» and «ость» would make every comparison below miss a sentence a
#: person reads without noticing anything. The soft hyphen is the realistic one:
#: it is what a typesetter inserts to hyphenate a long Russian word. Written as
#: escapes because a constant nobody can see is one somebody deletes by accident.
#:
#: **Corrected 2026-09-07.** This was ``_ODD_SPACES``, holding U+00A0, U+202F and
#: U+2009, with a docstring saying they had to be folded by hand because a
#: comparison typed with ordinary spaces misses them. The second half of that was
#: false, and it made the loop below dead code: :meth:`str.isspace` is true for
#: all three, so the ``" ".join(text.split())`` on the next line already turned
#: them into ordinary spaces before anything was compared. The constant now holds
#: what ``split()`` genuinely leaves behind. Note the direction of the change:
#: removing an invisible character can only make a substring match where it did
#: not before, never take a match away, so this can only ever add a stop.
_INVISIBLES: Final[str] = "\u200b\u200c\u200d\u2060\ufeff\u00ad"

#: Every word of hh's demand that the resume be made visible, as stems. Measured
#: in full on 2026-09-06:
#:
#:     «Чтобы откликнуться на эту вакансию, поменяйте видимость резюме
#:      на «Видно компаниям-клиентам HeadHunter»»
#:
#: Required together, in any order, with anything at all between them. Stems
#: rather than whole words, so that «видимость», «видимости» and
#: «видимостью» all count.
#:
#: **Widened 2026-09-07.** This was ``BLOCKING_ANCHOR = "видимость резюме"``,
#: an exact bigram matched inside a single line, while :func:`read_form_warnings`
#: promised in its own docstring to read "any mention of resume visibility
#: anywhere in the modal" as the demand. It did not, and the gap was one word
#: wide: «поменяйте видимость вашего резюме» did not match, and neither did
#: «поменяйте настройки видимости резюме». The code was made to keep the
#: promise rather than the promise trimmed to fit the code, because of the
#: asymmetry this module turns on: a false positive costs one vacancy handed to
#: the owner, a false negative sends an application hh will not show to the
#: employer.
BLOCKING_WORDS: Final[tuple[str, ...]] = ("видимост", "резюме")

#: The stable half of «Такой отклик может получить отказ», which is followed by
#: hh's own reason. Measured reason on the same modal: «Английский язык в резюме
#: «Python-разработчик» ниже обязательного уровня, который указал работодатель.»
SOFT_ANCHOR: Final[str] = "может получить отказ"

#: Lines of the modal that are controls, not prose. Used only to stop reading
#: the soft warning's reason at the bottom of the card; getting this list wrong
#: costs one extra line of quoted text, never a wrong verdict.
_CONTROL_LABELS: Final[tuple[str, ...]] = (
    "добавить сопроводительное",
    "откликнуться",
)


def printable(text: str) -> str:
    """hh's words, reduced to what this console can actually put on screen.

    Applied to every string quoted out of the page. The alternative is a run
    that dies on an emoji in an employer's test question, halfway through a
    batch, after the browser is open — which has happened here before with a
    box-drawing character.

    Characters outside cp1251 become ``?``. That is a visible degradation of
    hh's exact wording, and it is the smaller loss: Cyrillic, «», — and … all
    survive, so a real warning still reads as itself.
    """
    return text.encode(CONSOLE_ENCODING, errors="replace").decode(CONSOLE_ENCODING)


def _normalised(text: str) -> str:
    """Lowercased, with hh's invisible characters gone and its spaces made plain.

    Matching only. Everything shown to a person is quoted from the original.

    The join over :meth:`str.split` does the work for hh's typographic spaces —
    U+00A0, U+202F and U+2009 are all :meth:`str.isspace`, which is why the loop
    that used to replace them by hand was doing nothing. What ``split()`` leaves
    behind is the zero-width family in :data:`_INVISIBLES`, and those are removed
    rather than turned into spaces: they sit *inside* a word, so replacing one
    with a space would break the word it is hiding in.
    """
    for character in _INVISIBLES:
        text = text.replace(character, "")
    return " ".join(text.split()).casefold()


class FormWarning(StrEnum):
    """What the response modal is warning about, if anything."""

    #: Neither of the two known warnings is in the text.
    NONE = "none"
    #: hh's own «этот отклик может получить отказ» analysis. Shown, never acted on.
    SOFT = "soft"
    #: hh will not accept an application in this state. Never send.
    BLOCKING = "blocking"


@final
@dataclass(frozen=True, slots=True)
class FormWarnings:
    """What the modal said, in hh's words, sorted by what it means.

    Both fields can be set at once, and on the one modal measured in full they
    were: vacancy 136131345 carried the visibility demand *and* the
    English-level warning in the same card. A single-valued verdict would have
    thrown one of them away, and the one it would have thrown away is the one
    worth keeping — hh naming a specific unmet requirement is more precise than
    any similarity score this project computes.
    """

    #: hh's exact demand, when it will not accept an application as things
    #: stand. ``None`` when no such line was found.
    blocking: str | None
    #: hh's exact «может получить отказ» line together with its reason.
    soft: str | None

    @property
    def verdict(self) -> FormWarning:
        """The single value a caller that only wants one can branch on."""
        if self.blocking is not None:
            return FormWarning.BLOCKING
        if self.soft is not None:
            return FormWarning.SOFT
        return FormWarning.NONE

    @property
    def may_send(self) -> bool:
        """Whether the blocking warning is absent. The soft one never blocks."""
        return self.blocking is None


def read_form_warnings(modal_text: str) -> FormWarnings:
    """Classify the response modal by what it says, not by which elements exist.

    The modal's one warning element — ``hidden-resume-warning``, named in
    ``agent/selectors.py`` because selectors live there and nowhere else —
    carries several different messages. So "the element is present" both
    false-positives, since the soft warning uses that same element and the soft
    warning is not a reason to stop, and misses, since hh is free to put the
    next warning somewhere else. The text is the thing that means something, so
    the text is what is read.

    **This reads a card, and says nothing about a card it was not given.** An
    empty or half-rendered ``modal_text`` produces ``FormWarnings(None, None)``,
    which is indistinguishable from a modal hh had nothing to say in. That is not
    a defect to fix here — a classifier cannot tell "no warning" from "no text"
    — but it is a trap for the caller, and the caller has to close it before
    reading "no warning" as permission. ``agent/submit.py`` does: it waits for the
    submit control inside the modal, then requires that control's own label to be
    inside the text it classifies, and refuses to send when it is not.

    **What happens when hh rewords it entirely**, which it eventually will. The
    two directions cost different amounts and are not symmetric:

    * Missing a blocking warning is cheap and self-correcting. The agent opens
      the form and clicks send; hh refuses; ``negotiations.total`` is re-read
      afterwards, still says no application exists, and the run reports a
      failure to a person. One slot out of a small daily budget, and nothing
      irreversible — because hh is enforcing its own rule, not us.
    * Treating anything unrecognised as blocking is expensive and permanent.
      The first time hh adds a line to that card, every vacancy goes to a human
      and the agent stops being one. Nobody would reword the card back.

    So this errs generously *within* the known family and refuses to guess
    outside it. Generously now means what the sentence above it always claimed:
    every word of :data:`BLOCKING_WORDS`, as a stem, in any order, with anything
    between them — first inside one line, so the person gets hh's own sentence
    to read, and failing that anywhere in the card, so a demand hh has split over
    two lines is still a demand. Until 2026-09-07 this said "any mention of
    resume visibility anywhere in the modal" and matched an exact two-word bigram
    inside a single line; one word between the two and the hard stop was gone.

    What makes an unrecognised wording *safe* is still not this function: it is
    that the irreversible step is guarded by hh itself and confirmed afterwards
    against ``negotiations.total``. A string comparison against a site somebody
    else rewrites is a courtesy to the human reading the card, and treating it as
    a safety control would be building a guard that reads as protection and is
    not.

    The soft family gets the opposite treatment — a short anchor, matched
    widely — because a false positive there costs one extra line on a
    confirmation card and a false negative loses hh's own analysis of why the
    application will fail.
    """
    lines = [line.strip() for line in modal_text.splitlines()]
    blocking: str | None = None
    soft: str | None = None

    for index, line in enumerate(lines):
        if not line:
            continue
        normalised = _normalised(line)
        if blocking is None and _is_the_visibility_demand(normalised):
            blocking = printable(line)
            continue
        if soft is None and SOFT_ANCHOR in normalised:
            soft = printable(" ".join([line, *_reason_after(lines, index)]).strip())

    if blocking is None:
        blocking = _visibility_demand_split_over_lines(lines)

    return FormWarnings(blocking=blocking, soft=soft)


def _is_the_visibility_demand(normalised_line: str) -> bool:
    """Whether one normalised line carries every word of the demand."""
    return all(word in normalised_line for word in BLOCKING_WORDS)


def _visibility_demand_split_over_lines(lines: list[str]) -> str | None:
    """The demand when hh has broken it across more than one line of the card.

    The measured sentence is one line, and this is the branch for the day it is
    not — a heading and a body, a bullet list, a line wrapped around an inline
    link. Nobody has seen that, so the price of being wrong is what decides the
    design, and it is the usual asymmetry: this can only ever *add* a stop, and
    the vacancy it stops goes to the owner with hh's words attached.

    Because no single line is the demand in this branch, no single line can be
    quoted as it: every line that mentions any of the words is handed over
    together. On the measured card that would also pull in hh's «может получить
    отказ» reason, which names «резюме» — an extra sentence on a card that is
    already stopping, which is the cheapest way this can be wrong.
    """
    if not all(word in _normalised(" ".join(lines)) for word in BLOCKING_WORDS):
        return None
    carrying = [line for line in lines if line and _mentions_the_demand(line)]
    return printable(" ".join(carrying)) or None


def _mentions_the_demand(line: str) -> bool:
    """Whether one line carries any of the demand's words, not necessarily all."""
    normalised = _normalised(line)
    return any(word in normalised for word in BLOCKING_WORDS)


def _reason_after(lines: list[str], index: int) -> list[str]:
    """The lines hh puts under «может получить отказ» before the buttons start.

    Measured shape: one sentence naming the unmet requirement. Written as a loop
    anyway, because a second sentence would otherwise be silently dropped and
    the reason is the useful half of that warning.
    """
    reason: list[str] = []
    for line in lines[index + 1 :]:
        if not line or _normalised(line) in _CONTROL_LABELS:
            break
        reason.append(line)
    return reason


@final
@dataclass(frozen=True, slots=True)
class Application:
    """One application hh remembers against this vacancy.

    Every field is optional because the entry is hh's and this reads four keys
    out of thirty-odd. ``last_state`` is the one worth persisting: it is the
    outcome, it arrives free with any page load, and asking for it separately
    would mean opening the negotiations list.
    """

    #: ``topicList[].id`` — hh's id for the conversation this application began.
    topic_id: int | None
    #: ``chatId``, which is what the messages hang off.
    chat_id: int | None
    #: ``initialState``. Observed: ``"RESPONSE"``.
    initial_state: str | None
    #: ``lastState``. Observed: ``"DISCARD"``, which the site renders as
    #: «Вам отказали». The set is open — do not enumerate it in a type.
    last_state: str | None


@final
@dataclass(frozen=True, slots=True)
class Negotiations:
    """hh's own count of applications on this vacancy, and what it knows of them."""

    #: ``negotiations.total``. The measured idempotency answer.
    total: int
    #: What ``topicList`` carried. Can be empty while ``total`` is not: the list
    #: is hh's to trim, and a trimmed list is not an application that stopped
    #: existing, which is why the count alone is enough to say "applied".
    applications: tuple[Application, ...]

    @property
    def exists(self) -> bool:
        """Whether an application has been sent, from either thing that can say so.

        ``total >= 1`` is the measured signal and the usual answer. A non-empty
        ``topicList`` beside a ``total`` of zero is a disagreement nobody has
        measured, and it is settled the way every other unfamiliar shape in this
        module is settled: toward the reading that stops the agent.

        **Corrected 2026-09-07.** This was ``total >= 1`` and nothing else, and
        it was the one disagreement in this reader that resolved toward sending:
        a page listing this vacancy's own conversations while reporting
        ``total == 0`` read as "no application exists", and the agent applied
        again. A second application is the mistake the owner cannot undo.

        The direction is what makes this safe rather than merely stricter. The
        list can only ever *add* an application, never take one away, so nothing
        that used to stop now proceeds. It is also read after a send, by
        ``agent/submit.py``'s confirmation, and it lands the same way there: a
        conversation hh remembers for this vacancy is an application, so the
        confirmation believes hh rather than telling the owner nothing left —
        and being told nothing left is how a vacancy gets applied to twice.
        """
        return self.total >= 1 or bool(self.applications)


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


def _status_entry(state: dict[str, Any], vacancy_id: str) -> dict[str, Any] | None:
    """``applicantVacancyResponseStatuses[str(id)]``, or None if it is not there.

    ``Any`` for the same reason as :func:`read_state`: the value is hh's, and
    every key taken out of it is type-checked where it is read.
    """
    statuses = state.get("applicantVacancyResponseStatuses")
    if not isinstance(statuses, dict):
        return None
    entry = statuses.get(str(vacancy_id))
    return entry if isinstance(entry, dict) else None


def read_negotiations(state: dict[str, Any], vacancy_id: str) -> Negotiations | None:
    """hh's application count for this vacancy, or None when the shape is unfamiliar.

    ``None`` is not "none found". It means the page did not say in a way this
    code recognises, and every caller must treat it as a reason to stop. A
    ``total`` that is not an integer is exactly that case: it is the field the
    whole decision rests on, so a surprise there is a stop, not a zero.
    """
    entry = _status_entry(state, vacancy_id)
    if entry is None:
        return None
    negotiations = entry.get("negotiations")
    if not isinstance(negotiations, dict):
        return None
    total = negotiations.get("total")
    # ``bool`` is an ``int`` in Python, and a ``true`` here would read as 1.
    if not isinstance(total, int) or isinstance(total, bool) or total < 0:
        return None

    topics = negotiations.get("topicList")
    applications: tuple[Application, ...] = ()
    if isinstance(topics, list):
        applications = tuple(_application(topic) for topic in topics if isinstance(topic, dict))

    return Negotiations(total=total, applications=applications)


def _application(topic: dict[str, Any]) -> Application:
    """Four keys out of a ``topicList`` entry, each one only if it is the right type.

    ``Any`` because the entry is hh's — thirty-odd keys, most of them irrelevant
    here — and the four that are read are checked one at a time.
    """
    return Application(
        topic_id=_int_or_none(topic.get("id")),
        chat_id=_int_or_none(topic.get("chatId")),
        initial_state=_str_or_none(topic.get("initialState")),
        last_state=_str_or_none(topic.get("lastState")),
    )


def _int_or_none(value: object) -> int | None:
    """An id, when it is one. ``bool`` is rejected: ``True`` is not an id."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _str_or_none(value: object) -> str | None:
    """A state name, when there is one. hh writes ``null`` for "not yet"."""
    return value if isinstance(value, str) and value else None


def read_applied(state: dict[str, Any], vacancy_id: str) -> bool | None:
    """Whether an application already exists: True, False, or **unknown**.

    Decided by ``negotiations.total`` and by nothing else — not by which apply
    button the page rendered, and not by the key called ``alreadyApplied``,
    which was ``false`` on a vacancy that had one.

    ``None`` means the page did not say in a way this code recognises. Every
    caller must treat that as a reason to hand the vacancy to a person; nothing
    may treat it as ``False``. See the module docstring.
    """
    negotiations = read_negotiations(state, vacancy_id)
    return None if negotiations is None else negotiations.exists


def vacancy_id_from_url(url: str) -> str | None:
    """The id in a vacancy URL, so a page can be checked against what we meant."""
    match = re.search(r"/vacancy/(\d+)", url)
    return match.group(1) if match else None
