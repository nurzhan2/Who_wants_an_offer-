"""What the local apply agent is handed, and what comes back from it.

All the logic for ``app/api/v1/applications.py`` lives here; the router
validates, calls one of these two functions and answers (CLAUDE.md rule 2).

**Nothing in this module imports ``agent``, and nothing in ``agent`` imports
this.** The two packages meet over HTTP and nowhere else: ``backend/`` is
anonymous, read-only and can run on a server, while ``agent/`` drives a browser
under the owner's own login and sends things. ``agent/tests/test_isolation.py``
enforces that by parsing the import graph.

**Why the queue writes nothing.**

An ``application`` row means "the candidate acted on this vacancy". Creating one
when a vacancy is *offered* would fill the tracker with jobs nobody looked at:
the agent shows each item to a person, and a person says no to most of them. It
would also make ``GET /queue`` a write, which is the one thing a request the
agent retries must not be.

The row that matters already exists by the time an item can be served at all.
``app/letters/store.save_letter`` creates it when the cover letter is written,
and an item without a letter is one the agent refuses to send — so the ordinary
lifecycle is: letters create the row, the queue reads it, the result updates it.
:func:`record_results` creates a row only when none exists, which is the moment
something actually happened in the world (an application the owner sent by hand,
a letter stored some other way). Creation at the point of the event, not at the
point of the offer.

**Idempotency.** ``application`` has no unique constraint on ``vacancy_id`` —
deliberately, since a person may track two attempts at the same job — so
"upsert" here means *update the oldest row for this vacancy*, which is the rule
``app/letters/store.py`` already follows for the same reason. A transaction-
scoped advisory lock closes the read-then-insert window, so two results posted
at the same instant cannot both decide the row is missing.

**Where a result is written, since 2026-09-07.** Onto columns of its own. It
used to be rendered into a marked block inside ``application.notes``, because
the columns did not exist; the session that shipped that said in its own commit
message that it was the weakest part of the change. Two things were wrong with
it. A text blob cannot be filtered, grouped or counted, so the feedback loop
this data exists for — did the high scores answer more often, which missing
requirement costs a reply — could not be written at all. And ``notes`` is the
person's column: rewriting it on every result put machine output in the place
their own sentences live. Migration ``0008_application_send_record`` added the
columns and parsed the existing blocks forward. Nothing in this module writes
``notes`` any more, and nothing else in the backend ever did.

**Two write rules, and the difference between them is the whole design.**

*The send is recorded once.* ``sent_at``, ``sent_letter``, the match snapshot
and ``vacancy_key_skills`` describe one moment that has already passed. The
first result reporting ``sent`` writes them; no later result changes them,
because "what we believed when we sent" cannot be improved by learning more
afterwards — that is the thing being measured. A column still NULL may be
filled by a later report, since filling a hole records something that was never
recorded; a value already there is never overwritten.

*hh's fields are news, and news updates.* ``hh_last_state`` in particular
arrives days after the send and changes when it does. Each hh column is
overwritten whenever a result carries a value for it, and left alone when the
result carries none: a report that did not look is not a report that hh
withdrew the line. ``hh_last_state_at`` moves only when the state itself
changes, so it stays the date an outcome was *first* seen, which is what a
time-to-answer measurement needs.
"""

import hashlib
import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final, final
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import Select, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.db.base import uuid7
from app.db.enums import ApplicationStatus, MatchBucket, RuleScope
from app.db.models import (
    Application,
    CandidateProfile,
    Match,
    Vacancy,
    VacancySkill,
    VacancySource,
)
from app.letters import channel
from app.resume import ats_audit
from app.resume.ats_keywords import HeldSkill, match_requirements
from app.schemas.agent import (
    CONTRACT_VERSION,
    AgentStatus,
    ApplicationResult,
    DashboardConfirmation,
    MatchExplanation,
    QueueItem,
    QueueResponse,
    ResultAck,
    ResultsResponse,
)
from app.schemas.ats import ATSSummary, DocumentKind, Overall
from app.schemas.autopilot import SetAsideItem, SetAsideKind
from app.schemas.match import MatchComponentScores, MatchedSkill, MissingSkill
from app.services import ats as ats_service
from app.workshop import rules as workshop_rules
from app.workshop import store as workshop_store
from app.workshop.rules import RuleSpec

logger = get_logger(__name__)

#: Where the connector keeps everything it worked out about a posting. Same key
#: ``app/letters/store.py`` reads; see ``app/sources/hh.py`` for what is in it.
DERIVED_KEY = "_derived"

#: The requirement list inside that block, as ``app/sources/hh.py`` writes it
#: from hh's ``keySkills``. Read by key rather than assumed present: whether the
#: key is there at all is what separates "the posting listed no requirements"
#: from "this connector never derived any", and those become ``[]`` and ``NULL``
#: respectively in the snapshot.
KEY_SKILLS_KEY = "key_skills"

#: Ceiling on the rendered explanation, and on how many names it lists. The
#: card the agent prints is a terminal, not a page.
MAX_EXPLANATION = 400
MAX_LISTED = 6

#: How many rows to read before de-duplication and validation trim the answer.
#: A vacancy can carry more than one posting from the same source (two hh ads
#: deduplicated onto one fingerprint), and an item whose stored URL disagrees
#: with its id is dropped rather than served.
OVERFETCH = 2

#: How many rows one :func:`triage` pass reads, in total rather than per item
#: asked for. Not a multiple of the limit, and that is the whole point.
#:
#: A multiple is the natural shape for a read whose rows are mostly kept, which
#: is what the queue used to be: the statement dropped a letterless vacancy, so
#: ``limit * OVERFETCH`` rows yielded about ``limit`` items. The selection
#: cannot work that way — «письма ещё нет» is a line the owner reads, not a row
#: SQL hides — so every refusal now spends the same budget a served item does,
#: and the ratio between the two is a property of the corpus rather than of the
#: request. Measured 24 Sep 2026: 93 of the 102 candidates in the walk corpus
#: were letterless, so ``limit=10`` at eight rows apiece answered 7 items while
#: 9 qualified, and no multiple fixes that in general.
#:
#: A flat ceiling does, because the thing being read is bounded and small. The
#: statement already narrows to one profile, one source, above the score floor,
#: not filtered, not acted on, not seeded — 87 rows on the live corpus and 102
#: on the walk one. Five hundred covers both several times over in one query,
#: and a corpus that genuinely exceeds it has a queue nobody could read anyway.
CANDIDATE_CEILING: Final[int] = 500


@final
@dataclass(frozen=True, slots=True)
class ReadyItem:
    """One vacancy that passed every check, with the card the agent would get.

    Carries our own ``vacancy_id`` beside the item because :class:`QueueItem`'s
    ``vacancy_id`` is hh's number — the agent's vocabulary — and everything on
    this side of the seam (the batch, the confirmations, the tracker) is keyed
    by ours.
    """

    vacancy_id: UUID
    item: QueueItem


@final
@dataclass(frozen=True, slots=True)
class Triage:
    """The whole selection: what may be applied to, and what may not and why."""

    ready: list[ReadyItem] = field(default_factory=list)
    set_aside: list[SetAsideItem] = field(default_factory=list)


async def triage(
    session: AsyncSession,
    *,
    limit: int,
    profile_id: UUID | None = None,
    min_score: Decimal | None = None,
    require_letter: bool = True,
    vacancy_id: UUID | None = None,
) -> Triage:
    """Split the candidates into "send this" and "a person should look", with reasons.

    **This function is the whole safety argument of the autopilot.** One
    confirmation for a batch replaces the owner's look at each card, and it is
    only as safe as this selection: score, bucket, source, the experience gap,
    the language requirement, the workshop's rules over the stored letter, the
    ATS audit, and a re-read of the posting's own page. Every end-to-end run
    before this existed put something in the queue that a person then caught by
    eye — three ``filtered`` vacancies on 9 Sep, two archived of six on 16 Sep,
    a «Бариста» matched on "go" on 13 Sep. Nothing that fails a check here is
    meant to reach a batch screen, because nobody is looking any more.

    **Nothing is thrown away.** A vacancy that fails goes into
    :attr:`Triage.set_aside` with its link and one Russian sentence naming what
    was found, so the owner can open it and apply by hand. A check that only
    dropped the row would move the September defects from the card to nowhere.

    **What is not here is not a candidate at all.** The statement below already
    refuses a vacancy under the score floor, in the ``filtered`` bucket, from
    another source, marked spam, seeded, or already acted on. Those produce no
    set-aside entry on purpose: "посмотреть руками" is a short list of near
    misses, and a corpus's worth of vacancies nobody scored is not that.

    ``require_letter=False`` asks the narrower question the chain asks before it
    writes letters: "what has cleared every check *except* having a letter?".
    """
    profile = profile_id if profile_id is not None else await active_profile_id(session)
    if profile is None:
        logger.info("agent.queue.no_profile")
        return Triage()

    threshold = min_score if min_score is not None else Decimal(settings.agent_queue_min_score)
    rows = (
        await session.execute(
            _queue_statement(
                profile_id=profile,
                min_score=threshold,
                # Always False here: a vacancy with no letter is a set-aside
                # row with a reason, not a row the SQL hides. ``require_letter``
                # decides what it *becomes*, below.
                require_letter=False,
                limit=CANDIDATE_CEILING,
                vacancy_id=vacancy_id,
            )
        )
    ).all()

    # Both read once for the whole pass rather than per row. The skills are the
    # same candidate's every time, and the rules are the same workshop's; a
    # per-row read would be an N+1 over a list the owner is about to confirm.
    # ``active_rules`` rather than ``stored_rules`` so the two built-in ones —
    # no links, no contact handles — are checked here exactly as they are when
    # the letter is written; they are the ones hh files as spam.
    held = await ats_service.held_skills(session, profile)
    rules = await workshop_store.active_rules(session, scope=RuleScope.COVER_LETTER)

    ready: list[ReadyItem] = []
    set_aside: list[SetAsideItem] = []
    seen: set[UUID] = set()
    for row in rows:
        if len(ready) >= limit:
            break
        if row.vacancy_id in seen:
            continue
        seen.add(row.vacancy_id)
        item = _to_item(row)
        if item is None:
            # The agent rejects a whole batch when a URL does not name its own
            # id (``agent/queue.py``), so this one must not be served — and the
            # owner is told, because the alternative is a vacancy that scored
            # 91 and silently never appears anywhere.
            set_aside.append(_unservable(row))
            continue
        item = item.model_copy(update={"ats": _ats_summary(item, row, held)})
        item = item.model_copy(update={"confirmation": _confirmation(item, row)})
        refusal = _refusal(row, item, rules=rules, require_letter=require_letter)
        if refusal is None:
            ready.append(ReadyItem(vacancy_id=row.vacancy_id, item=item))
        else:
            set_aside.append(_aside(row, item, *refusal))

    logger.info(
        "agent.queue.triaged",
        profile_id=str(profile),
        ready=len(ready),
        set_aside=len(set_aside),
        source=settings.agent_source_slug,
    )
    return Triage(ready=ready, set_aside=set_aside)


async def build_queue(
    session: AsyncSession,
    *,
    limit: int,
    profile_id: UUID | None = None,
    min_score: Decimal | None = None,
    require_letter: bool = True,
    vacancy_id: UUID | None = None,
) -> QueueResponse:
    """Vacancies worth an application, best score first.

    :func:`triage`'s ready half and nothing else, so that what the agent is
    handed and what the batch screen offers cannot be two different lists. The
    reasons go to the screen; the agent gets the items.

    Every item carries the URL the crawler actually read. It is never rebuilt
    from the id: for hh that address is a regional subdomain, because the
    connector walks ``almaty.hh.kz``'s own sitemap, and it is both the page the
    agent opens and the link the human reads on the confirmation card. An item
    whose stored URL does not name its own id is dropped with a warning instead
    of served — the agent rejects the whole batch on that mismatch
    (``agent/queue.py``), so one bad row must not cost the run.
    """
    selection = await triage(
        session,
        limit=limit,
        profile_id=profile_id,
        min_score=min_score,
        require_letter=require_letter,
        vacancy_id=vacancy_id,
    )
    logger.info(
        "agent.queue.served",
        count=len(selection.ready),
        source=settings.agent_source_slug,
    )
    return QueueResponse(version=CONTRACT_VERSION, items=[ready.item for ready in selection.ready])


# ── the checks the batch confirmation rests on ────────────────────────


#: A language flag as ``app.matching.rules.language_verdict`` writes it: the
#: name, the level asked for and the level claimed, all three CEFR. Narrow on
#: purpose — ``red_flags`` is a column of prose and the other things in it
#: («зарплата ниже минимума…», «семантика не посчитана…», «работодатель не
#: указал ключевые навыки») must not be read as a language the owner lacks.
LANGUAGE_FLAG: Final[re.Pattern[str]] = re.compile(
    r"^(?P<name>[^:]+): требуется (?P<asked>[A-C][12]), заявлен (?P<held>[A-C][12])$"
)


def _refusal(
    row: Any,
    item: QueueItem,
    *,
    rules: Sequence[RuleSpec],
    require_letter: bool,
) -> tuple[SetAsideKind, str] | None:
    """Why this vacancy is not getting an application, or None when it is.

    **The order is the design.** A dead posting is reported as dead rather than
    as "its letter breaks a rule", because the first is what the owner would do
    something about. And the page-age check is *last*, so ``stale_page`` means
    "the only thing left wrong with this is that nobody has looked" — which is
    exactly the set ``app.services.autopilot`` re-reads before it writes letters
    or builds a batch. Moving that check up would make the chain re-read pages
    of vacancies it is never going to apply to.

    ``Any`` for the row for the reason :func:`_to_item` gives.
    """
    if item.archived:
        return (
            SetAsideKind.ARCHIVED,
            "Вакансия в архиве: при последнем чтении страницы её там уже не было. "
            "Отклик физически не уйдёт.",
        )
    if item.closed_for_applicants:
        return (
            SetAsideKind.CLOSED,
            f"{settings.agent_source_slug} закрыл вакансию для откликов — "
            "форма отклика на странице больше не работает.",
        )

    gap = row.experience_gap_years
    allowed = settings.agent_max_experience_gap_years
    if gap is not None and gap > Decimal(str(allowed)):
        # ``allowed`` is formatted from the float rather than from the Decimal
        # built out of it: ``format(Decimal("1.0"), "g")`` keeps the trailing
        # zero and the sentence reads «разрыв до 1.0 года», which is not how
        # anybody writes a number of years.
        return (
            SetAsideKind.EXPERIENCE_GAP,
            f"Просят опыта больше вашего на {gap:g} {_years(gap)}. "
            f"Без разбора отклик уходит при разрыве до {allowed:g} "
            f"{_years(Decimal(str(allowed)))} (AGENT_MAX_EXPERIENCE_GAP_YEARS). "
            "Если берётесь сознательно — откликнитесь сами.",
        )

    for flag in item.match.red_flags if item.match is not None else []:
        found = LANGUAGE_FLAG.match(flag)
        if found is not None:
            return (
                SetAsideKind.LANGUAGE,
                f"Требуется {found['name']} на уровне {found['asked']}, "
                f"в резюме заявлен {found['held']}. Такой отклик агент не отправляет.",
            )

    if item.letter is None:
        if require_letter:
            return (
                SetAsideKind.NO_LETTER,
                "Письма ещё нет. Запустите цепочку на «Обзоре» — она напишет письма "
                "для всех прошедших отбор, — или напишите его в карточке вакансии.",
            )
    else:
        broken = workshop_rules.hard(
            workshop_rules.check(item.letter, scope=RuleScope.COVER_LETTER, rules=rules)
        )
        if broken:
            # The owner's own sentence, not a restatement of it: they wrote the
            # rule, and «hh считает такое письмо спамом» is the whole reason it
            # exists.
            return (
                SetAsideKind.LETTER_RULES,
                "Письмо не проходит правила мастерской: "
                + " ".join(violation.message for violation in broken),
            )
        # ``unreadable`` and not ``degraded``: a critical finding means a parser
        # gets this letter wrong, while ``degraded`` is the ordinary state of a
        # letter that does not name every requirement — refusing on that would
        # empty the queue and teach nobody anything.
        if item.ats is not None and item.ats.overall is Overall.UNREADABLE:
            return (
                SetAsideKind.LETTER_AUDIT,
                "Письмо не прошло ATS-аудит: "
                + "; ".join(item.ats.critical[:3])
                + ". Робот-фильтр работодателя прочитает его неправильно.",
            )

    fresh_for = timedelta(hours=settings.agent_page_freshness_hours)
    if datetime.now(UTC) - row.last_seen_at > fresh_for:
        return (
            SetAsideKind.STALE_PAGE,
            f"Страницу вакансии не перечитывали дольше "
            f"{settings.agent_page_freshness_hours} ч, а база помнит только прошлый обход. "
            "Запустите цепочку на «Обзоре» — она перечитает страницу и вернёт вакансию "
            "в пачку, если та ещё открыта.",
        )
    return None


def _years(value: Decimal) -> str:
    """«год» / «года» / «лет» for a number that may be fractional.

    Fractions take the genitive singular in Russian («1,9 года»), which is the
    same form as 2–4, so the only case worth separating is a whole 1.
    """
    if value != value.to_integral_value():
        return "года"
    whole = int(value) % 100
    if 11 <= whole <= 14:
        return "лет"
    last = whole % 10
    if last == 1:
        return "год"
    return "года" if 2 <= last <= 4 else "лет"


def _aside(row: Any, item: QueueItem, kind: SetAsideKind, reason: str) -> SetAsideItem:
    """One refused vacancy, as the screen and the report read it."""
    return SetAsideItem(
        vacancy_id=row.vacancy_id,
        external_id=item.vacancy_id,
        title=item.title,
        company=item.company,
        url=item.url,
        score=item.score,
        kind=kind,
        reason=reason,
    )


def _unservable(row: Any) -> SetAsideItem:
    """The one refusal built from the row, because there is no item to build from."""
    return SetAsideItem(
        vacancy_id=row.vacancy_id,
        external_id=str(row.external_id),
        title=str(row.title),
        company=row.company,
        url=str(row.url),
        score=row.score,
        kind=SetAsideKind.UNSERVABLE,
        reason=(
            f"Сохранённая ссылка не заканчивается номером вакансии "
            f"{row.external_id}, и агент отклонил бы всю пачку из-за неё. "
            "Откройте ссылку и откликнитесь сами."
        ),
    )


async def record_results(
    session: AsyncSession, results: list[ApplicationResult]
) -> ResultsResponse:
    """Write what the agent saw back onto the tracker rows.

    Only ``sent`` moves an application forward, and only ever to ``applied``:
    a row a person has already dragged to ``interview`` is not demoted because
    a later run reported a failure against the same vacancy. Everything else
    the agent saw — its own status and reason, both of hh's lines, hh's
    negotiation count, hh's last state — lands on a column of its own, and the
    first ``sent`` also snapshots what this project believed at that moment.
    The two write rules are in the module docstring; :func:`_upsert` applies
    them.

    The profile is resolved once for the whole batch rather than per result:
    it is the same answer every time, and the snapshot needs it.
    """
    acks: list[ResultAck] = []
    unknown: list[str] = []
    accepted = 0
    profile_id = await active_profile_id(session)

    for result in results:
        vacancy_id = await _resolve(session, result.vacancy_id)
        if vacancy_id is None:
            unknown.append(result.vacancy_id)
            acks.append(
                ResultAck(
                    vacancy_id=result.vacancy_id,
                    accepted=False,
                    detail=(
                        f"Источник {settings.agent_source_slug} не знает вакансию "
                        f"{result.vacancy_id}; запись в трекере не создана."
                    ),
                )
            )
            logger.warning(
                "agent.results.unknown_vacancy",
                external_id=result.vacancy_id,
                source=settings.agent_source_slug,
            )
            continue

        application, created = await _upsert(session, vacancy_id, result, profile_id)
        accepted += 1
        acks.append(
            ResultAck(
                vacancy_id=result.vacancy_id,
                accepted=True,
                application_id=application.id,
                created=created,
            )
        )
        logger.info(
            "agent.results.recorded",
            external_id=result.vacancy_id,
            application_id=str(application.id),
            created=created,
            status=result.status.value,
            # Neither hh's sentences nor the letter is logged. Both are about
            # one person's own application to one employer, and they belong in
            # the row rather than in a file that gets pasted into a bug report.
            has_blocking_warning=result.hh_blocking_warning is not None,
            has_soft_warning=result.hh_warning is not None,
            has_sent_letter=result.sent_letter is not None,
        )

    await session.commit()
    return ResultsResponse(
        version=CONTRACT_VERSION, accepted=accepted, unknown=unknown, results=acks
    )


async def active_profile_id(session: AsyncSession) -> UUID | None:
    """The profile the dashboard is currently working from.

    Same rule as ``app/letters/store.load_profile_facts``: the newest active
    one. Duplicated rather than imported because that module's queries are
    about writing letters and this one is not.
    """
    found = await session.scalar(
        select(CandidateProfile.id)
        .where(CandidateProfile.is_active.is_(True))
        .order_by(CandidateProfile.created_at.desc())
        .limit(1)
    )
    # Checked rather than cast: SQLAlchemy types a scalar select of a UUID
    # column as Any, and an isinstance costs less than a suppression comment.
    return found if isinstance(found, UUID) else None


# ── the queue ─────────────────────────────────────────────────────────


#: The middle of how ``scripts/seed.py`` names the rows it invents: its
#: ``external_id`` is ``f"{slug}-{name}"`` with ``name = f"dev-{index:03d}"``, so
#: an hh seed row is ``hh-dev-000``. The pattern is built from the configured
#: slug rather than hardcoded, because the queue is scoped to one source and a
#: bare ``dev-%`` would match nothing at all — which is how this line was first
#: written, and it would have looked like it worked.
#:
#: They
#: carry ``source_slug = "hh"`` like a real posting, so without this they reach
#: the apply queue beside genuine vacancies and the agent opens ``example.test``
#: under the owner's account.
#:
#: They are excluded here rather than left to ``_url_names``, which does drop
#: them today — their URL is ``https://example.test/hh/dev-000`` and its path
#: does not end in ``hh-dev-000``. That is an accident of how the seed writes
#: URLs, not a rule: a seed that produced hh-shaped URLs would be served. A rule
#: that holds for the reason you think it holds is worth the one line.
SEED_ID_MARKER: Final[str] = "-dev-"


def card_digest(item: QueueItem) -> str:
    """SHA-256 of everything a confirmation card shows about this item.

    The dashboard computes it when the owner opens the card and again when they
    confirm; the queue computes it once more before serving the confirmation;
    the agent binds its mandate to it. Canonical JSON of the item without the
    confirmation itself and without ``match``, whose content the flat
    ``score_explanation`` already carries. Any change to what the owner read —
    a regenerated letter, a rescored vacancy, a new line from hh, a moved link
    — gives a different digest, and the confirmation stops applying.
    """
    payload = item.model_dump(mode="json", exclude={"confirmation", "match"})
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def letter_digest(letter: str | None) -> str:
    """SHA-256 of a letter, spelled exactly as ``agent.mandate.digest`` spells it.

    The agent compares this with the letter it is about to type, so the two
    sides must agree on the empty letter too: ``None`` digests as ``""``.
    """
    return hashlib.sha256((letter or "").encode("utf-8")).hexdigest()


def _confirmation(item: QueueItem, row: Any) -> DashboardConfirmation | None:
    """The owner's dashboard confirmation, if it still describes this item.

    ``Any`` for the row for the reason :func:`_to_item` gives.
    """
    confirmed_at = row.confirmed_at
    if confirmed_at is None or row.confirmed_card_digest is None:
        return None
    if datetime.now(UTC) - confirmed_at > timedelta(hours=settings.agent_confirmation_ttl_hours):
        return None
    if row.confirmed_letter_digest != letter_digest(item.letter):
        return None
    if row.confirmed_card_digest != card_digest(item):
        return None
    return DashboardConfirmation(
        confirmed_at=confirmed_at,
        letter_digest=row.confirmed_letter_digest,
        card_digest=row.confirmed_card_digest,
    )


def _letter_row(column: Any) -> Any:
    """One column of the row whose letter the queue serves.

    The same row :func:`_queue_statement`'s ``letter`` reads — the oldest one
    holding a letter — because a confirmation, and hh's lines shown beside it,
    belong to that letter. ``Any`` because the column type varies per call.
    """
    return (
        select(column)
        .where(Application.vacancy_id == Vacancy.id)
        .where(Application.cover_letter.is_not(None))
        .order_by(Application.created_at, Application.id)
        .limit(1)
        .scalar_subquery()
    )


def _queue_statement(
    *,
    profile_id: UUID,
    min_score: Decimal,
    require_letter: bool,
    limit: int,
    vacancy_id: UUID | None = None,
) -> Select[Any]:
    """One statement, explicit about its columns.

    ``Vacancy.matches``, ``Vacancy.applications`` and ``Match.vacancy`` are all
    ``lazy="raise"`` — the models turn an accidental N+1 into an error — so
    everything this needs is named here.
    """
    letter = (
        select(Application.cover_letter)
        .where(Application.vacancy_id == Vacancy.id)
        .where(Application.cover_letter.is_not(None))
        .order_by(Application.created_at, Application.id)
        .limit(1)
        .scalar_subquery()
    )
    # Anything past "saved" means a person or a previous run already acted on
    # this vacancy. Offering it again is how a second application gets sent.
    #
    # ``hh_negotiations_total`` is the third way to learn the same fact, and it
    # is here rather than among the set-aside reasons for that reason: it is not
    # a near miss the owner might overrule, it is an application that already
    # exists. hh publishes the count on the posting's own page and «Обновить
    # исходы» records it, so this catches the case the other two columns miss —
    # an application the owner sent by hand, outside this project, on a vacancy
    # it is still offering. The agent refuses on the same number when it opens
    # the page; refusing here as well means the owner is never asked to confirm
    # a batch row that was going to be dropped a minute later anyway.
    acted_on = (
        select(Application.id)
        .where(Application.vacancy_id == Vacancy.id)
        .where(
            (Application.status != ApplicationStatus.SAVED)
            | (Application.applied_at.is_not(None))
            | (func.coalesce(Application.hh_negotiations_total, 0) >= 1)
        )
        .exists()
    )

    statement = (
        select(
            Vacancy.id.label("vacancy_id"),
            Vacancy.title,
            Vacancy.company,
            Vacancy.is_active,
            # When somebody last read this posting's own page. The freshness
            # rule in :func:`triage` is written against it, and it is selected
            # here rather than looked up per row because the answer is one
            # column of a join the statement already makes.
            Vacancy.last_seen_at,
            VacancySource.external_id,
            VacancySource.url,
            VacancySource.raw,
            Match.score,
            Match.bucket,
            Match.verdict,
            Match.application_angle,
            Match.component_scores,
            Match.matched_skills,
            Match.missing_required,
            Match.missing_nice,
            Match.red_flags,
            Match.experience_gap_years,
            letter.label("letter"),
            _letter_row(Application.hh_warning).label("hh_warning"),
            _letter_row(Application.hh_blocking_warning).label("hh_blocking_warning"),
            _letter_row(Application.confirmed_at).label("confirmed_at"),
            _letter_row(Application.confirmed_letter_digest).label("confirmed_letter_digest"),
            _letter_row(Application.confirmed_card_digest).label("confirmed_card_digest"),
        )
        .select_from(Match)
        .join(Vacancy, Vacancy.id == Match.vacancy_id)
        .join(
            VacancySource,
            (VacancySource.vacancy_id == Vacancy.id)
            & (VacancySource.source_slug == settings.agent_source_slug),
        )
        .where(Match.profile_id == profile_id)
        .where(Match.score >= min_score)
        # ``filtered`` is "do not apply", not a low score: a vacancy asking six
        # years of a candidate with 1.1, or a language they do not speak, keeps
        # a high similarity and must still never reach the agent. Measured 13
        # Sep 2026: three such vacancies sat in the ready list above the floor.
        .where(Match.bucket != MatchBucket.FILTERED)
        # The agent already opened it and hh said there is nothing to do —
        # archived after the crawl, closed, or applied to. See
        # ``app.letters.channel.skipped_by_agent``.
        .where(~channel.skipped_by_agent(Vacancy.id))
        .where(Vacancy.is_spam.is_(False))
        .where(~VacancySource.external_id.like(f"%{SEED_ID_MARKER}%"))
        .where(~acted_on)
        .order_by(Match.score.desc(), Vacancy.id, VacancySource.external_id)
        .limit(limit)
    )
    if require_letter:
        statement = statement.where(letter.is_not(None))
    if vacancy_id is not None:
        statement = statement.where(Vacancy.id == vacancy_id)
    return statement


def _to_item(row: Any) -> QueueItem | None:
    """One row as the agent will read it, or None when it must not be served.

    ``Any`` for the row: SQLAlchemy's ``Row`` is not usefully typed for a
    select of eighteen labelled columns, and every field is validated into a
    Pydantic model immediately below — which is the boundary CLAUDE.md rule 3's
    exception is for.
    """
    external_id = str(row.external_id)
    url = str(row.url)
    if not _url_names(url, external_id):
        # The agent compares the two and rejects the entire batch when they
        # disagree, so serving this item would cost every item behind it.
        logger.warning(
            "agent.queue.url_id_mismatch",
            external_id=external_id,
            url=url,
            source=settings.agent_source_slug,
        )
        return None

    derived = _derived(row.raw)
    explanation = _explanation(row)
    return QueueItem(
        vacancy_id=external_id,
        url=url,
        title=row.title,
        company=row.company,
        letter=row.letter,
        closed_for_applicants=bool(derived.get("closed_for_applicants", False)),
        # The crawler marks a posting inactive when it stops answering, which is
        # what "archived" means to the agent. hh's own archive flag is read on
        # the page and never reaches _derived, so this is the honest source.
        archived=not bool(row.is_active),
        # Nothing this project stores says an application happens on the
        # employer's own site: hh's page does, and the agent reads it there.
        # Served as False rather than guessed, so the flag never lies.
        external_application=False,
        score=explanation.score,
        score_explanation=render_explanation(explanation),
        source=settings.agent_source_slug,
        match=explanation,
        anonymous=bool(derived.get("anonymous", False)),
        employer_on_additional_check=bool(derived.get("employer_on_additional_check", False)),
        hh_lines=_hh_lines(row.hh_blocking_warning, row.hh_warning),
    )


def _hh_lines(*texts: str | None) -> list[str]:
    """hh's stored sentences, one per line, each once, in the order given."""
    lines: list[str] = []
    for text_ in texts:
        for line in (text_ or "").splitlines():
            if line.strip() and line.strip() not in lines:
                lines.append(line.strip())
    return lines


def _ats_summary(item: QueueItem, row: Any, held: Sequence[HeldSkill]) -> ATSSummary | None:
    """The letter this item carries, audited against this vacancy.

    The third of the report's three display places, and the last one: after this
    card the next thing that happens is an application. The card gets the
    summary rather than the report because it is printed to a console — see
    :class:`app.schemas.ats.ATSSummary` — but it is a projection of the same
    object the vacancy screen renders, built by the same code, so the two cannot
    disagree.

    No extra query. The requirement list is in ``row.raw``, which the queue
    statement already selected, and the candidate's skills were read once for
    the batch. An item with no letter has nothing to audit and gets ``None``,
    which the card must show as "not checked" rather than as a pass.
    """
    if not item.letter:
        return None
    requirements = [
        name.strip()
        for name in _derived(row.raw).get("key_skills") or []
        if isinstance(name, str) and name.strip()
    ]
    keywords = match_requirements(item.letter, requirements, held)
    report = ats_audit.audit_generated(
        item.letter, kind=DocumentKind.COVER_LETTER, keywords=keywords
    )
    return ATSSummary.of(report)


def _explanation(row: Any) -> MatchExplanation:
    """The score together with the reason for it."""
    components = row.component_scores if isinstance(row.component_scores, dict) else {}
    return MatchExplanation(
        score=row.score,
        bucket=MatchBucket(row.bucket),
        verdict=row.verdict,
        application_angle=row.application_angle,
        components=MatchComponentScores.model_validate(components),
        matched_skills=_models(MatchedSkill, row.matched_skills),
        missing_required=_models(MissingSkill, row.missing_required),
        missing_nice=_models(MissingSkill, row.missing_nice),
        red_flags=[str(flag) for flag in _iterable(row.red_flags)],
        experience_gap_years=row.experience_gap_years,
    )


def render_explanation(explanation: MatchExplanation) -> str | None:
    """The reason for the score, as one line a person can read on the card.

    Russian, because it is shown to the owner; the structure it was built from
    travels alongside it in :attr:`QueueItem.match` for anything that would
    rather render than print.

    ``None`` when there is genuinely nothing to say. A sentence invented from an
    empty match would read as an explanation and be one, and the card the agent
    prints is the last thing between a generated letter and a real application.
    """
    parts: list[str] = []
    if explanation.verdict and explanation.verdict.strip():
        parts.append(explanation.verdict.strip())
    if explanation.matched_skills:
        parts.append(
            f"совпадает: {_names(skill.canonical_name for skill in explanation.matched_skills)}"
        )
    if explanation.missing_required:
        parts.append(
            f"не хватает обязательного: "
            f"{_names(skill.canonical_name for skill in explanation.missing_required)}"
        )
    if explanation.missing_nice:
        parts.append(
            f"не хватает желательного: "
            f"{_names(skill.canonical_name for skill in explanation.missing_nice)}"
        )
    if explanation.red_flags:
        parts.append(f"тревожные признаки: {_names(iter(explanation.red_flags))}")
    if explanation.experience_gap_years and explanation.experience_gap_years > 0:
        parts.append(f"разрыв по опыту, лет: {explanation.experience_gap_years:g}")
    if not parts:
        return None
    return "; ".join(parts)[:MAX_EXPLANATION]


def _names(values: Iterable[str]) -> str:
    """A short, comma-separated list. Long enough to be useful, short enough to
    stay on one line of a terminal card."""
    listed = [value.strip() for value in values if value.strip()]
    head = ", ".join(listed[:MAX_LISTED])
    return f"{head} и ещё {len(listed) - MAX_LISTED}" if len(listed) > MAX_LISTED else head


def _models[M: BaseModel](model: type[M], stored: object) -> list[M]:
    """Validate a JSONB list into models, dropping entries that do not fit.

    The annotation on a JSONB column is a promise the database does not keep,
    and these lists were last written by the scorer. One malformed entry from
    an older scoring run must not empty a queue.
    """
    built: list[M] = []
    for entry in _iterable(stored):
        if not isinstance(entry, dict):
            continue
        try:
            built.append(model.model_validate(entry))
        except ValueError:
            logger.warning("agent.queue.unreadable_match_detail", model=model.__name__)
    return built


def _iterable(stored: object) -> list[Any]:
    """A JSONB list, or nothing at all."""
    return list(stored) if isinstance(stored, list) else []


def _derived(raw: object) -> dict[str, Any]:
    """The connector's derived block out of a source payload."""
    if not isinstance(raw, dict):
        return {}
    block = raw.get(DERIVED_KEY)
    return block if isinstance(block, dict) else {}


def _url_names(url: str, external_id: str) -> bool:
    """Whether this https URL's path ends in exactly this posting's id.

    The check the agent performs, performed here first. Path only: a login
    redirect carries the address it interrupted in a query parameter, so an id
    found anywhere in the string proves nothing about where the URL leads.
    """
    if not url.startswith("https://"):
        return False
    parts = urlsplit(url)
    if not parts.hostname:
        return False
    tail = parts.path.rstrip("/").rsplit("/", 1)[-1]
    return tail == external_id


# ── the results ───────────────────────────────────────────────────────


async def _resolve(session: AsyncSession, external_id: str) -> UUID | None:
    """Our own vacancy id for a posting the agent named, or None."""
    found = await session.scalar(
        select(VacancySource.vacancy_id)
        .where(VacancySource.source_slug == settings.agent_source_slug)
        .where(VacancySource.external_id == external_id)
        .limit(1)
    )
    return found if isinstance(found, UUID) else None


async def _upsert(
    session: AsyncSession,
    vacancy_id: UUID,
    result: ApplicationResult,
    profile_id: UUID | None,
) -> tuple[Application, bool]:
    """Update this vacancy's tracker row, or create the first one.

    ``notes`` is not touched here, or anywhere else in this codebase. It is the
    person's column.
    """
    await _lock(session, vacancy_id)
    application = await session.scalar(
        select(Application)
        .where(Application.vacancy_id == vacancy_id)
        .order_by(Application.created_at, Application.id)
        .limit(1)
    )
    created = application is None
    if application is None:
        application = Application(id=uuid7(), vacancy_id=vacancy_id)
        session.add(application)

    _record_agent(application, result)
    _record_hh(application, result)
    # One confirmation is one attempt: whatever the agent reports, the "yes" it
    # acted on is spent. On every row of the vacancy, because the confirmation
    # sits on the row holding the letter and this result lands on the oldest.
    await session.execute(
        update(Application)
        .where(Application.vacancy_id == vacancy_id)
        .where(Application.confirmed_at.is_not(None))
        .values(confirmed_at=None, confirmed_letter_digest=None, confirmed_card_digest=None)
        .execution_options(synchronize_session="fetch")
    )
    if result.status is AgentStatus.SENT:
        # The only forward move this endpoint makes, and only forward: a row
        # somebody already dragged to "interview" stays there.
        if application.status is ApplicationStatus.SAVED:
            application.status = ApplicationStatus.APPLIED
        if application.applied_at is None:
            # A real instant rather than func.now(): the attribute is read back
            # in the same transaction, and an unevaluated SQL expression sitting
            # on a mapped attribute is a value nothing downstream can compare.
            application.applied_at = datetime.now(UTC)
        await _record_send(session, application, result, vacancy_id, profile_id)

    await session.flush()
    return application, created


def _record_agent(application: Application, result: ApplicationResult) -> None:
    """Where the agent left this application, and why.

    ``sent`` is terminal here for the same reason ``interview`` is terminal on
    the kanban: a run that reports a failure against a vacancy already sent to
    is reporting on an attempt, not undoing an application that exists in
    somebody's inbox. Anything short of ``sent`` may be replaced by a later
    report, because ``queued`` → ``failed`` → ``needs_manual`` is a real
    progression through one vacancy.
    """
    if application.agent_status != AgentStatus.SENT.value:
        application.agent_status = result.status.value
    if result.reason is not None:
        application.agent_reason = result.reason


def _record_hh(application: Application, result: ApplicationResult) -> None:
    """hh's own lines and hh's own numbers, as of this report.

    A field the result does not carry leaves the stored value alone. The agent
    reports what it saw on one page at one moment; silence in a report is "I
    did not look" far more often than "hh took the line down", and erasing a
    warning the owner read is the more expensive of the two mistakes.
    """
    if result.hh_warning is not None:
        application.hh_warning = result.hh_warning
    if result.hh_blocking_warning is not None:
        application.hh_blocking_warning = result.hh_blocking_warning
    if result.negotiations_total is not None:
        application.hh_negotiations_total = result.negotiations_total
    if result.last_state is not None and result.last_state != application.hh_last_state:
        # Stamped only on a change, so the pair reads "this outcome, first seen
        # on this date". Re-stamping on every identical report would turn
        # time-to-answer into time-since-the-last-run.
        application.hh_last_state = result.last_state
        application.hh_last_state_at = datetime.now(UTC)


async def _record_send(
    session: AsyncSession,
    application: Application,
    result: ApplicationResult,
    vacancy_id: UUID,
    profile_id: UUID | None,
) -> None:
    """The send itself: the moment, the letter, and what we believed.

    Written once. A column still empty may be filled by a later report — that
    records something never recorded — but a value already there is never
    replaced, because these five answer "what was true when this went out" and
    that answer does not improve with hindsight. Re-posting a result, which
    happens for real whenever a run dies between sending and reporting,
    therefore changes nothing.

    The snapshot is read at this moment rather than carried on the wire, and
    that is a known and bounded imprecision: a scoring run between the queue
    being taken and this result arriving would move ``match`` under us, and
    what is stored is then the score as of the report rather than the score on
    the card the human approved. Minutes, against a loop measured in months.
    The alternative — the agent echoing our own score back at us — would let a
    float that has been through JSON define a ``Numeric(5, 2)`` of ours, which
    is a worse thing to be wrong about.
    """
    if application.sent_at is None:
        application.sent_at = datetime.now(UTC)
    if application.sent_letter is None and result.sent_letter is not None:
        application.sent_letter = result.sent_letter

    if application.match_score is None and application.match_explanation is None:
        explanation = await _match_snapshot(session, vacancy_id, profile_id)
        if explanation is not None:
            application.match_score = explanation.score
            application.match_bucket = explanation.bucket
            # mode="json" so the Decimal scores and the bucket enum become
            # JSON scalars. The default dump keeps them as Python objects,
            # which asyncpg refuses to encode into JSONB, and the stored keys
            # have to be the field names ``MatchExplanation`` reads back.
            application.match_explanation = explanation.model_dump(mode="json")
    if application.vacancy_key_skills is None:
        application.vacancy_key_skills = await _key_skills(session, vacancy_id)


async def _match_snapshot(
    session: AsyncSession, vacancy_id: UUID, profile_id: UUID | None
) -> MatchExplanation | None:
    """This project's verdict on this pairing, or None if it never had one.

    None rather than a zero score: an unscored application and a badly scored
    one must not read the same, which is the same rule the confirmation card
    follows for the same reason.
    """
    if profile_id is None:
        return None
    row = (
        await session.execute(
            select(
                Match.score,
                Match.bucket,
                Match.verdict,
                Match.application_angle,
                Match.component_scores,
                Match.matched_skills,
                Match.missing_required,
                Match.missing_nice,
                Match.red_flags,
                Match.experience_gap_years,
            )
            .where(Match.profile_id == profile_id)
            .where(Match.vacancy_id == vacancy_id)
            .limit(1)
        )
    ).first()
    return None if row is None else _explanation(row)


async def _key_skills(session: AsyncSession, vacancy_id: UUID) -> list[str] | None:
    """What the posting was asking for on the day, or None if nothing said.

    Normalised ``vacancy_skill`` rows first, because that is the schema of
    record; the connector's derived block second, because nothing writes those
    rows yet and hh's ``keySkills`` are where the requirement list actually
    lives today. The same two places, in the same order, that
    ``app/letters/store._required_skills`` reads — duplicated rather than
    imported for the reason :func:`active_profile_id` is duplicated: that
    module's queries answer questions about writing a letter, and this one is
    not writing one.

    ``[]`` and ``None`` are different answers. ``[]`` is a posting that listed
    no requirements; ``None`` is a payload that never carried the key, which is
    what a source other than hh would leave behind, and a dashboard counting
    "applications where I was missing a listed skill" must not read the second
    as the first.
    """
    rows = (
        await session.scalars(
            select(VacancySkill.canonical_name)
            .where(VacancySkill.vacancy_id == vacancy_id)
            .order_by(VacancySkill.is_required.desc(), VacancySkill.canonical_name)
        )
    ).all()
    if rows:
        return [str(name) for name in rows]

    payloads = (
        await session.scalars(
            select(VacancySource.raw)
            .where(VacancySource.vacancy_id == vacancy_id)
            .order_by(VacancySource.created_at, VacancySource.id)
        )
    ).all()
    stated = False
    names: list[str] = []
    for payload in payloads:
        block = _derived(payload)
        if KEY_SKILLS_KEY not in block:
            continue
        stated = True
        for item in _iterable(block[KEY_SKILLS_KEY]):
            if isinstance(item, str) and item.strip() and item.strip() not in names:
                names.append(item.strip())
    return names if stated else None


async def _lock(session: AsyncSession, vacancy_id: UUID) -> None:
    """Serialise results for one vacancy for the rest of this transaction.

    Without it two results posted at the same instant both read "no row" and
    both insert, which is the one way this endpoint could produce a duplicate.
    The lock is transaction-scoped, so it is released by the commit and there
    is nothing to unlock by hand.
    """
    high = ((vacancy_id.int >> 32) & 0xFFFFFFFF) - 0x8000_0000
    low = (vacancy_id.int & 0xFFFFFFFF) - 0x8000_0000
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:high, :low)"), {"high": high, "low": low}
    )
