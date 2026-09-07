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

**Corrected 2026-09-07: no sentence in the modal stops an application any more,
and the reason this file is where that is written down.** Until this change
:func:`read_form_warnings` sorted the modal into two families, and one of them —
hh's «поменяйте видимость резюме…» — was a hard refusal that no application
could get past. It was wrong, it was wrong for a full day, and the way it was
wrong is the lesson worth more than the fix. See :data:`VISIBILITY_WORDS`.
"""

import html as html_lib
import json
import re
from dataclasses import dataclass
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
#: not before, never take a match away, so this can only ever add a line of hh's
#: own words to what a person is shown. It used to be able to add a stop; since
#: 2026-09-07 nothing matched here stops anything.
_INVISIBLES: Final[str] = "\u200b\u200c\u200d\u2060\ufeff\u00ad"

#: Every word of hh's notice about the resume's visibility, as stems. Measured
#: in full on 2026-09-06:
#:
#:     «Чтобы откликнуться на эту вакансию, поменяйте видимость резюме
#:      на «Видно компаниям-клиентам HeadHunter»»
#:
#: Required together, in any order, with anything at all between them. Stems
#: rather than whole words, so that «видимость», «видимости» and
#: «видимостью» all count.
#:
#: **This sentence is advice. It is not a refusal, and treating it as one made
#: the agent unable to send anything at all.** Measured 2026-09-07 in real
#: Chrome, on the owner's own account, on a vacancy carrying exactly this line:
#: the submit button was clicked, ``negotiations.total`` went 0 -> 1, and the
#: apply control turned into ``vacancy-response-link-top-again``. hh accepted
#: the application with the notice on screen. The committed record of that run
#: is ``agent/evidence/20260907-send-under-visibility-notice.json``; the
#: unredacted artefact it was taken from is ``agent/probe/_cdp_send.json``,
#: which is gitignored because it is the owner's own account state. The owner
#: also applies by hand, regularly, with the same notice showing.
#:
#: **Where the wrong rule came from, because that is the part worth inheriting.**
#: It was a guess in an earlier brief. It then survived a full day of being
#: treated as a measured fact — and it survived precisely *because* it was a
#: block: the rule forbade the one experiment that would have refuted it, so
#: nothing in a day of running the agent could ever produce evidence against it.
#: A rule that blocks an action has to arrive with a way to check that the block
#: is real. Without that check it is a hypothesis wearing a fact's clothes, and
#: it is unfalsifiable by construction rather than by accident. Every other
#: measured claim in this package names the artefact it came from; this one
#: named a brief, and nobody noticed for a day because the code did what the
#: brief said and the brief was the only witness.
#:
#: **Why the net is still cast this wide, now that a match costs a line rather
#: than an application.** The old reason was an asymmetry between one vacancy
#: handed to a person and one application hh would hide. That asymmetry is gone
#: — nothing here stops anything — and the new one runs the same way: a missed
#: notice means the owner sends a batch without being told hh thinks the whole
#: batch is limited, while a spurious one costs an extra quoted line on a
#: confirmation card. Missing it is still the more expensive mistake, so the
#: stems stay stems and the words stay unordered.
VISIBILITY_WORDS: Final[tuple[str, ...]] = ("видимост", "резюме")

#: The stable half of «Такой отклик может получить отказ», which is followed by
#: hh's own reason. Measured reason on the same modal: «Английский язык в резюме
#: «Python-разработчик» ниже обязательного уровня, который указал работодатель.»
#: Advice as well, and always was; it now sits beside the visibility notice
#: rather than under a family that outranked it.
REJECTION_ANCHOR: Final[str] = "может получить отказ"

#: Lines of the modal that are controls, not prose. Used only to stop reading
#: the «может получить отказ» reason at the bottom of the card; getting this
#: list wrong costs one extra line of quoted text and nothing else.
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


@final
@dataclass(frozen=True, slots=True)
class FormWarnings:
    """What the modal said, in hh's words, sorted by what it is about.

    Both fields can be set at once, and on the one modal measured in full they
    were: vacancy 136131345 carried the visibility notice *and* the
    English-level warning in the same card. Neither outranks the other and
    neither stops anything — they are two different pieces of advice, and a
    person deciding needs both, so this class has two fields and no single
    summary value.

    **Removed 2026-09-07: ``verdict``, ``may_send`` and the ``FormWarning``
    enum.** ``may_send`` answered "is the blocking warning absent", and after
    this change there is no blocking warning: it would have been a property that
    returns ``True`` for ever, sitting in the path of the irreversible click and
    reading, to anyone skimming, like a permission check. ``verdict`` collapsed
    the two fields into one and preferred the visibility line, which is the
    exact loss the paragraph above says must not happen. A guard that cannot
    refuse and a summary that hides half its input are both worse than nothing,
    because the next reader believes them.
    """

    #: hh's exact sentence about the resume's visibility, when the card carried
    #: one. ``None`` when it did not. This is a property of the *resume*, not of
    #: the vacancy: hh shows it on every vacancy while the setting stands, so one
    #: of these is a statement about every application in the batch.
    visibility: str | None
    #: hh's exact «может получить отказ» line together with its reason. About
    #: this one application: it names a requirement this vacancy asks for and the
    #: resume does not meet.
    likely_rejection: str | None

    @property
    def said(self) -> tuple[str, ...]:
        """Everything hh said, in hh's own words, in the order the card had it.

        The one thing every caller wants: the journal writes it, the results
        file carries it, and the next confirmation card quotes it. Returning the
        lines rather than one joined string leaves the joining to whoever knows
        what they are joining for — a card indents each line under a gutter, a
        journal column does not.
        """
        return tuple(line for line in (self.visibility, self.likely_rejection) if line)


def mentions_visibility(text: str) -> bool:
    """Whether this text carries every word of hh's resume-visibility notice.

    Public because two callers need the same rule and must not each have their
    own. :func:`read_form_warnings` uses it to classify a line of the open card;
    ``agent/run.py`` uses it on a sentence that came back out of the journal,
    where hh's words are stored in one column and have to be told apart again
    before a card can label them.
    """
    return all(word in _normalised(text) for word in VISIBILITY_WORDS)


def read_form_warnings(modal_text: str) -> FormWarnings:
    """Read what the response modal says, by its text and not by its elements.

    The modal's one warning element — ``hidden-resume-warning``, named in
    ``agent/selectors.py`` because selectors live there and nowhere else —
    carries several different messages, so "the element is present" says nothing
    about which one is on screen. The text is the thing that means something, so
    the text is what is read.

    **Nothing this function returns stops an application** (2026-09-07). It used
    to: a line matching :data:`VISIBILITY_WORDS` was a refusal, raised out of
    ``agent/submit.py``, and no application in this package could get past it.
    hh accepts those applications — measured, see :data:`VISIBILITY_WORDS` — so
    the rule blocked every send the agent could ever make and nothing else. What
    is left here is a reader: it hands the caller hh's own sentences so a person
    can be shown them, and it decides nothing.

    That also settles what happens when hh rewords the card, which it eventually
    will. Missing a sentence now costs the owner one piece of advice they would
    have liked; inventing a meaning for a sentence nobody has measured costs
    them a card full of text hh did not write. So this still errs generously
    *within* the two measured families and refuses to guess outside them: every
    word of :data:`VISIBILITY_WORDS`, as a stem, in any order, with anything
    between them — first inside one line, so the person gets hh's own sentence
    to read, and failing that anywhere in the card, so a notice hh has split over
    two lines still reaches them.

    **This reads a card, and says nothing about a card it was not given.** An
    empty or half-rendered ``modal_text`` produces ``FormWarnings(None, None)``,
    which is indistinguishable from a modal hh had nothing to say in. A
    classifier cannot tell "no warning" from "no text", and the caller has to
    close that gap rather than reading silence as anything. ``agent/submit.py``
    does: it waits for the submit control inside the modal and then requires that
    control's own label to be inside the text it classified. That check survives
    this change, and it is not a text rule — it asks whether the card rendered at
    all, which is a fact about the browser rather than an opinion about a
    warning.
    """
    lines = [line.strip() for line in modal_text.splitlines()]
    visibility: str | None = None
    likely_rejection: str | None = None

    for index, line in enumerate(lines):
        if not line:
            continue
        if visibility is None and mentions_visibility(line):
            visibility = printable(line)
            continue
        if likely_rejection is None and REJECTION_ANCHOR in _normalised(line):
            likely_rejection = printable(" ".join([line, *_reason_after(lines, index)]).strip())

    if visibility is None:
        visibility = _visibility_notice_split_over_lines(lines)

    return FormWarnings(visibility=visibility, likely_rejection=likely_rejection)


def _visibility_notice_split_over_lines(lines: list[str]) -> str | None:
    """The notice when hh has broken it across more than one line of the card.

    The measured sentence is one line, and this is the branch for the day it is
    not — a heading and a body, a bullet list, a line wrapped around an inline
    link. Nobody has seen that, so the price of being wrong is what decides the
    design, and being wrong here now adds a quoted line to a confirmation card
    rather than taking a vacancy away from the run.

    Because no single line is the notice in this branch, no single line can be
    quoted as it: every line that mentions any of the words is handed over
    together. On the measured card that would also pull in hh's «может получить
    отказ» reason, which names «резюме» — the same sentence twice on one card,
    which is the cheapest way this can be wrong and the reason the branch is
    kept rather than tightened.
    """
    if not mentions_visibility(" ".join(lines)):
        return None
    carrying = [line for line in lines if line and _mentions_any_visibility_word(line)]
    return printable(" ".join(carrying)) or None


def _mentions_any_visibility_word(line: str) -> bool:
    """Whether one line carries any of the notice's words, not necessarily all."""
    normalised = _normalised(line)
    return any(word in normalised for word in VISIBILITY_WORDS)


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
