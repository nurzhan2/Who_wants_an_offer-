"""What the dashboard reads.

Four screens, four sections of this module, and one rule shared by all of them:
**the numbers are computed where the data is**. Every count here comes out of
one SQL statement over ``vacancy``, ``source_state``, ``pipeline_run`` and
``application``; nothing is totalled in the browser. A page that counts rows it
was handed can only ever show what it managed to fetch, which on a corpus of a
thousand vacancies is a plausible wrong number rather than an obvious one.

The other rule is that *not measured* and *zero* are different values and stay
different all the way to the screen. ``None`` in this module always means
nobody looked: no run has counted that sitemap file, no scoring pass has
touched that vacancy, hh never showed that line. Rendering either as ``0``
would turn an unanswered question into an answer, and on this data — one
profile, two sent applications, five vacancies in six with no salary — the
unanswered questions outnumber the answered ones.

No text in this module is Russian. Everything here is data; the words a person
reads are the frontend's, per CLAUDE.md rule 10. The one exception is text that
was written in Russian by something else — the agent's reason for setting an
application aside, hh's own warning line — which travels through verbatim
because paraphrasing somebody else's sentence is how a dashboard starts lying.
"""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field

from app.db.enums import (
    ApplicationStatus,
    MatchBucket,
    ParseStatus,
    PipelineRunStatus,
    RequirementSource,
)
from app.letters.examples import OutcomeEvidence
from app.schemas.ats import ATSReport
from app.schemas.crawl import CrawlPosition
from app.schemas.match import MatchComponentScores

# Re-exported, not re-declared: the model is a profile fact and lives with the
# other profile schemas, because the data layer reads it and importing it from
# this module closes a cycle through the letter service. The screens keep
# importing it from here.
from app.schemas.profile import SkillElsewhere as SkillElsewhere
from app.schemas.vacancy import VacancyRead

# ── overview ──────────────────────────────────────────────────────────


class VacancyCounts(BaseModel):
    """How much of the corpus is here, and how much of it is usable.

    ``embedded`` is separate from ``total`` because a vacancy without a vector
    is not scored semantically, so the gap between these two is exactly the
    part of the corpus the matching pass is currently blind to.
    """

    total: int = 0
    active: int = 0
    embedded: int = 0
    #: Rows with no vector, or one computed before the row was last written.
    needs_embedding: int = 0
    #: Rows this profile has a match row for. Never equal to ``total`` while a
    #: scoring pass is behind a crawl, which is the normal state.
    scored: int = 0
    #: Requirement rows across the corpus — what the score is computed from.
    skill_rows: int = 0


class RunError(BaseModel):
    """One entry of ``pipeline_run.errors``, given a shape.

    Stored as free JSONB by the runner, which is right for a column that has to
    accept whatever a connector failed with. It is given a model here because
    the screen distinguishes exactly one of them — see
    :attr:`SourceRunState.stopped_by_robot_check` — and a screen that tested
    ``error["stage"] == "challenge"`` in a template would be doing that in the
    one place nothing type-checks.
    """

    stage: str | None = None
    error: str | None = None
    detail: str | None = None


class SourceRunState(BaseModel):
    """The last run of one source, and how it ended."""

    slug: str
    run_id: UUID
    status: PipelineRunStatus
    started_at: datetime
    finished_at: datetime | None = None
    found: int = 0
    new: int = 0
    updated: int = 0
    errors: list[RunError] = Field(default_factory=list)

    #: True when hh answered a permitted request with a check for robots.
    #:
    #: Its own field, and not a status, because ``partial`` covers both this and
    #: a connector that half worked, and the two need opposite reactions: this
    #: one is rescheduled and nothing in the code is wrong, the other sends
    #: somebody to read a module. ``app/sources/http.HHChallengedError`` argues
    #: the same point where the distinction is made; this carries it to the
    #: screen instead of letting the screen re-derive it from an error string.
    stopped_by_robot_check: bool = False


class SourceCrawl(BaseModel):
    """One source's walk, file by file, as the connector describes it."""

    slug: str
    positions: list[CrawlPosition] = Field(default_factory=list)

    @property
    def outstanding(self) -> int | None:
        """Entries left across every file, or None when none were counted."""
        counted = [p.outstanding for p in self.positions if p.outstanding is not None]
        return sum(counted) if counted else None


class HarvestedVacancy(BaseModel):
    """One posting the last crawl actually bought, by name.

    The whole point of listing these is that hh cannot be asked a question: its
    sitemap carries a URL and a date, so a page has to be paid for before
    anyone can tell what it advertises. Whether a run spent its budget on
    postings worth reading is therefore not knowable from the counters — only
    from the titles — and this is what puts them on the screen.
    """

    id: UUID
    title: str
    company: str | None = None
    url: str | None = None
    source_slug: str | None = None
    first_seen_at: datetime
    #: The score for the active profile. None means this posting has not been
    #: scored yet, which is normal for one bought minutes ago.
    score: Decimal | None = None
    bucket: MatchBucket | None = None


class Harvest(BaseModel):
    """What the last crawl brought back, with the window it covers."""

    since: datetime | None = None
    total: int = 0
    items: list[HarvestedVacancy] = Field(default_factory=list)


class ApplicationCounts(BaseModel):
    """The tracker in five numbers.

    ``sent`` counts rows the agent reported a send for — ``sent_at IS NOT
    NULL`` — and nothing else. A row a person dragged across their own kanban
    is not evidence that an application went out, and counting it here would
    make the one number this project can actually verify unverifiable.
    """

    sent: int = 0
    #: Of :attr:`sent`, the rows hh itself has confirmed: its own count of
    #: applications on the vacancy is at least one, or it reported a state for
    #: the conversation. Added 2026-09-16, when the first real run reported four
    #: sends with neither — a send the agent reported and a send hh confirmed
    #: are different facts, and only the second is what "отправлено" may mean.
    sent_confirmed: int = 0
    queued: int = 0
    needs_manual: int = 0
    #: Rows holding a letter, whether or not it has been sent.
    with_letter: int = 0
    #: Rows whose outcome hh has since reported.
    answered: int = 0


class ProfileBrief(BaseModel):
    """Who the dashboard is showing all this for."""

    id: UUID
    name: str | None = None
    headline: str | None = None
    parse_status: ParseStatus
    skills: int = 0
    updated_at: datetime


class Overview(BaseModel):
    """Everything the first screen shows, in one request.

    One request rather than six because these numbers are read together and
    compared with each other — "1174 vacancies, 582 scored" is one fact, not
    two — and six requests would let a screen show two halves of it taken at
    different moments.
    """

    generated_at: datetime
    profile: ProfileBrief | None = None
    vacancies: VacancyCounts = Field(default_factory=VacancyCounts)
    crawl: list[SourceCrawl] = Field(default_factory=list)
    runs: list[SourceRunState] = Field(default_factory=list)
    harvest: Harvest = Field(default_factory=Harvest)
    applications: ApplicationCounts = Field(default_factory=ApplicationCounts)


# ── one vacancy, and why it scores what it scores ─────────────────────


class RequirementStanding(BaseModel):
    """One thing a vacancy asks for, and where the candidate stands on it.

    The three-way split is the point of the screen, and the middle case is the
    one that pays for it. A requirement the scorer counted as missing may still
    be something this person does — the resume in hand simply never named it —
    and that is a sentence to add to a CV, while a genuine gap is a job to skip.
    A dashboard that showed both as "missing" would hide the difference between
    an afternoon's editing and a career change.
    """

    canonical_name: str
    is_required: bool = True
    #: 1.0 exact, 0.95 an alias, 0.5-0.7 a related technology, 0.25 same group.
    #: None when the requirement is not covered at all.
    coverage: Decimal | None = None
    #: How the profile covers it, when it does: the spelling the resume used.
    spelling: str | None = None
    #: Weight the vacancy's own requirement list gave it.
    weight: Decimal | None = None
    #: Where the evidence for an uncovered requirement came from, when there is
    #: any: ``resume_text`` (the words are in this CV, extraction missed them),
    #: ``other_profile`` (an earlier CV of the same owner lists the skill).
    #: None means no evidence anywhere, which is the honest reading of "absent".
    evidence: str | None = None
    #: Human-readable pointer to that evidence — the other resume's filename,
    #: or the fragment of this one's text the name was found in.
    evidence_detail: str | None = None
    #: Who says the vacancy wants this: the employer, in their own structured
    #: field, or this project, reading their description. A third state beside
    #: the two above, and a different question — those are about the candidate's
    #: side of the requirement, this is about whether the requirement was ever
    #: stated. Defaulted to the employer so that a card built from a match
    #: stored before ``0014_requirement_source`` keeps saying what it meant.
    source: RequirementSource = RequirementSource.EMPLOYER_FIELD


class RequirementBreakdown(BaseModel):
    """A vacancy's requirements, split three ways."""

    covered: list[RequirementStanding] = Field(default_factory=list)
    #: Not covered by the score, but the candidate demonstrably has it.
    not_in_this_cv: list[RequirementStanding] = Field(default_factory=list)
    #: Not covered, and nothing anywhere says the candidate has it.
    absent: list[RequirementStanding] = Field(default_factory=list)


class MatchSummary(BaseModel):
    """The score, and everything behind it."""

    score: Decimal
    rule_score: Decimal
    semantic_score: Decimal | None = None
    llm_score: Decimal | None = None
    bucket: MatchBucket
    components: MatchComponentScores = Field(default_factory=MatchComponentScores)
    red_flags: list[str] = Field(default_factory=list)
    experience_gap_years: Decimal | None = None
    verdict: str | None = None
    application_angle: str | None = None
    scored_at: datetime


class LetterBrief(BaseModel):
    """Whether this vacancy has a letter, and what happened to it."""

    application_id: UUID
    characters: int = 0
    sent_at: datetime | None = None
    agent_status: str | None = None
    outcome: str | None = None


class VacancyCard(BaseModel):
    """One vacancy, its match and its documents — the row expanded.

    The links to the pages the crawler actually read are inside ``vacancy``, as
    its ``sources``. There is no second list of bare URLs beside them: the card
    needs to name each link by its source — for hh the address is a regional
    subdomain and cannot be rebuilt from an id — and two links labelled
    identically are two links a person cannot choose between.
    """

    vacancy: VacancyRead
    match: MatchSummary | None = None
    requirements: RequirementBreakdown = Field(default_factory=RequirementBreakdown)
    letter: LetterBrief | None = None


# ── the applications board ────────────────────────────────────────────


class BoardCard(BaseModel):
    """One application, with everything the send recorded about it.

    ``sent_letter`` is served whole. The screen exists to answer "what did the
    employer actually read", and a truncated letter answers a different
    question. ``cover_letter`` is served beside it and is *not* the same
    string: a regeneration after a send replaces the second and never the
    first, and showing one under the other's name would present an unsent draft
    as the letter that got an interview.
    """

    id: UUID
    vacancy_id: UUID
    title: str
    company: str | None = None
    url: str | None = None

    status: ApplicationStatus
    agent_status: str | None = None
    #: Why the agent stopped, in the words it wrote for a person. Russian,
    #: verbatim, because it was written to be read rather than parsed.
    agent_reason: str | None = None

    applied_at: datetime | None = None
    sent_at: datetime | None = None
    sent_letter: str | None = None
    cover_letter: str | None = None

    match_score: Decimal | None = None
    match_bucket: MatchBucket | None = None
    vacancy_key_skills: list[str] | None = None

    #: hh's two lines, kept apart because they say different things: one is
    #: about this vacancy, the other about the account and therefore true of
    #: every application sent while that setting stands.
    hh_warning: str | None = None
    hh_blocking_warning: str | None = None
    hh_negotiations_total: int | None = None
    #: hh's own word for the outcome. Never translated into a verdict of ours —
    #: that vocabulary is hh's and it is open.
    hh_last_state: str | None = None
    hh_last_state_at: datetime | None = None

    #: Whether hh itself confirmed this send: a count of at least one, or a
    #: state for the conversation. See :func:`send_confirmed`. The board puts a
    #: send it cannot confirm in a column of its own rather than beside real
    #: ones, because the first real run produced four of them.
    send_confirmed: bool = False

    #: How old the posting is and whether it is still there, so nobody confirms
    #: an application to an archive: a third of the first real queue was.
    #: ``published_at`` is the employer's date, ``last_seen_at`` the crawler's
    #: last sighting, ``vacancy_active`` false once the crawler saw it go.
    vacancy_published_at: datetime | None = None
    vacancy_last_seen_at: datetime | None = None
    vacancy_active: bool | None = None


class BoardColumn(BaseModel):
    """One column of the board: a stage, or an outcome."""

    key: str
    cards: list[BoardCard] = Field(default_factory=list)


class Board(BaseModel):
    """The applications screen.

    Two rows of columns rather than one. The first is where an application is
    in *this* project's pipeline — queued, set aside for a person, sent — and
    the second is what hh has since said about the ones that went out. They are
    not one axis: a sent application has an outcome column *and* stays sent, and
    collapsing them would either lose the send or invent an outcome.
    """

    stages: list[BoardColumn] = Field(default_factory=list)
    outcomes: list[BoardColumn] = Field(default_factory=list)
    #: Rows that are in neither: tracked by hand, never queued, never sent.
    other: BoardColumn = Field(default_factory=lambda: BoardColumn(key="other"))


# ── documents ─────────────────────────────────────────────────────────


class LetterProblemRead(BaseModel):
    """One thing the guard caught, with the code the UI keys off."""

    code: str
    message: str


class ResumeDocument(BaseModel):
    """An uploaded resume and what a machine makes of it."""

    profile_id: UUID
    filename: str | None = None
    source_format: str | None = None
    size_bytes: int | None = None
    is_active: bool = False
    parse_status: ParseStatus
    parse_error: str | None = None
    uploaded_at: datetime
    #: None when the profile predates the audit, which is not a clean report.
    ats: ATSReport | None = None


class LetterDocument(BaseModel):
    """A generated letter, and whether the loop it belongs to closed.

    The three timestamps answer the three questions this screen is for: was it
    written, did it go out, and did anything come back. A letter with the first
    and not the second is work waiting for a person at a keyboard, because
    nothing else in this project can send it.
    """

    application_id: UUID
    vacancy_id: UUID
    title: str
    company: str | None = None
    url: str | None = None

    #: The letter as it stands now.
    text: str
    characters: int = 0
    #: Which version of the guard judged this text when it was written. None
    #: means the letter predates the record — not version zero, and not a pass.
    rules_version: str | None = None
    #: What today's rules say about this text. Empty means it still passes. A
    #: stored letter that no longer does is the most interesting row on the
    #: screen: it is what the rules have learned since it was written.
    problems: list["LetterProblemRead"] = Field(default_factory=list)

    written_at: datetime
    sent_at: datetime | None = None
    #: The text that was actually typed into hh's form, when a send recorded
    #: one. Differs from :attr:`text` after any regeneration.
    sent_letter: str | None = None
    outcome: str | None = None
    outcome_at: datetime | None = None
    match_score: Decimal | None = None


class Documents(BaseModel):
    """Everything this project has generated, and what became of it."""

    resumes: list[ResumeDocument] = Field(default_factory=list)
    letters: list[LetterDocument] = Field(default_factory=list)
    #: The rules a regeneration would apply today, so the screen can say which
    #: letters were written under something else.
    current_rules_version: str = ""


# ── the workshop ──────────────────────────────────────────────────────


class QueuedLetter(BaseModel):
    """A vacancy worth a letter, and whether one has been written."""

    vacancy_id: UUID
    title: str
    company: str | None = None
    score: Decimal
    has_letter: bool = False
    #: Whether the agent applies to it (its source lists the vacancy) or the
    #: owner does, by hand, on :attr:`url` — «откликнуться самому».
    via_agent: bool = False
    source_slug: str = ""
    url: str = ""


class LetterRequest(BaseModel):
    """Ask for a letter to be written for one vacancy.

    The only write the dashboard is allowed to make, and it produces a document
    rather than an action: the letter is saved on the tracker row and sent by
    nothing. Sending is ``wwao apply --send``, where a person is at the
    keyboard; there is no button for it here and there is not meant to be.
    """

    vacancy_id: UUID
    #: Rewrite a letter that already exists. Off by default so that a repeated
    #: click costs nothing — the same idempotence the connectors follow, for the
    #: same reason: the expensive call is the one worth not making twice.
    force: bool = False


class WorkshopResult(BaseModel):
    """What one generation actually did.

    A run that fell back to the rule-based text and a run that got a letter out
    of the model reach the database looking identical, so this says which
    happened. ``evidence`` says how much past-outcome data was in the prompt,
    in counts rather than a rate, so the screen can say "too little data yet"
    and mean it.
    """

    vacancy_id: UUID
    title: str
    company: str | None = None
    saved: bool = False
    #: Set when nothing was written, naming the reason.
    skipped: str | None = None
    text: str | None = None
    characters: int = 0
    #: True when the text came from the model rather than the fallback.
    from_model: bool = False
    matched_skills: int = 0
    missing_skills: int = 0
    problems: list[LetterProblemRead] = Field(default_factory=list)
    evidence: OutcomeEvidence = Field(default_factory=OutcomeEvidence)
    #: Whether those counts could support any conclusion at all. False for a
    #: long time, on purpose — see ``app/letters/examples.MIN_FOR_A_TREND`` — so
    #: that the screen can say «данных пока мало» and mean it rather than
    #: rendering a percentage computed over two applications.
    evidence_is_enough: bool = False


def send_confirmed(
    *, sent_at: datetime | None, negotiations_total: int | None, last_state: str | None
) -> bool:
    """Whether a send is backed by something hh said, rather than by our report.

    The same rule as ``ApplicationRepository.counts`` spells in SQL. ``0`` is hh
    saying there is no application, so it does not confirm anything; ``None`` is
    nobody having looked.
    """
    if sent_at is None:
        return False
    return (negotiations_total is not None and negotiations_total >= 1) or bool(
        (last_state or "").strip()
    )
