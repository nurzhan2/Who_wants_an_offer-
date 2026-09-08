"""The first screen: what is in the database, where the crawl is, what went out.

Everything here is a read, and every number comes from a repository rather than
from Python. That is not tidiness — it is the difference between a dashboard
that describes the database and one that describes the page. Counting a list the
API happened to return would report the page size on a corpus of a thousand
vacancies, and it would be a plausible number, which is worse than an obviously
wrong one.

The one judgement this module makes is which run was the last one, and it is
made explicitly rather than by ordering rows and taking the first: see
:func:`_window`.
"""

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import CandidateProfile, PipelineRun
from app.db.repositories.application import ApplicationRepository
from app.db.repositories.pipeline_run import PipelineRunRepository
from app.db.repositories.profile import ProfileRepository
from app.db.repositories.source_state import SourceStateRepository
from app.db.repositories.vacancy import VacancyRepository
from app.schemas.dashboard import (
    Harvest,
    Overview,
    ProfileBrief,
    RunError,
    SourceCrawl,
    SourceRunState,
)
from app.sources.registry import all_sources

logger = get_logger(__name__)

#: The error stage ``app/pipeline/runner.py`` records a check for robots under.
#: Read from here rather than spelled inline: the screen shows that case as its
#: own outcome, and two spellings of the string would make it silently vanish.
CHALLENGE_STAGE = "challenge"

#: How many freshly bought postings the overview lists by name. Enough to see
#: what a run spent its budget on — the titles are the only thing that can say
#: whether the budget went on this candidate's field — and short enough to stay
#: a panel rather than a second vacancies screen, which is one click away. The
#: whole count travels beside the list, so a truncated one still says so.
HARVEST_LIMIT = 12


async def build(session: AsyncSession, *, harvest_limit: int = HARVEST_LIMIT) -> Overview:
    """Assemble the overview screen.

    Sequential rather than gathered: they share one session, and a session is
    not safe to use from two tasks at once. These are counting queries over one
    person's database, and the round trips cost less than the bug would.
    """
    profiles = ProfileRepository(session)
    profile = await profiles.get_active()
    profile_id = profile.id if profile is not None else None

    vacancies = VacancyRepository(session)
    runs = await PipelineRunRepository(session).latest_per_source()

    since = _window(runs)
    harvest = Harvest(since=since)
    if since is not None:
        total, items = await vacancies.first_seen_since(
            since, profile_id=profile_id, limit=harvest_limit
        )
        harvest = Harvest(since=since, total=total, items=items)

    return Overview(
        generated_at=datetime.now(UTC),
        profile=_brief(profile),
        vacancies=await vacancies.counts(profile_id=profile_id),
        crawl=await _crawl(session),
        runs=[_run_state(run) for run in runs],
        harvest=harvest,
        applications=await ApplicationRepository(session).counts(),
    )


def _window(runs: list[PipelineRun]) -> datetime | None:
    """When the last crawl started, across every source.

    The latest start and not the earliest, and that choice is the whole
    definition of "the last run" on this screen. Sources run on their own
    cadences — hh every few hours, a bounded feed once a week — so the earliest
    of their latest starts can be a fortnight ago, and a harvest window opened
    there would list a fortnight of postings as "what the last run bought".

    None when nothing has ever run, which is not the same as a run that bought
    nothing.
    """
    starts = [run.started_at for run in runs]
    return max(starts) if starts else None


def _brief(profile: CandidateProfile | None) -> ProfileBrief | None:
    """The active profile, in the four fields the header shows.

    Deliberately not the whole profile. This screen is about the corpus, and the
    resume's contact details have no business travelling in a payload that every
    panel of it reads — the ones that need them ask for them.
    """
    if profile is None:
        return None
    return ProfileBrief(
        id=profile.id,
        name=profile.name,
        headline=profile.headline,
        parse_status=profile.parse_status,
        skills=len(profile.skills),
        updated_at=profile.updated_at,
    )


async def _crawl(session: AsyncSession) -> list[SourceCrawl]:
    """Every registered source's position, as the source itself describes it.

    The service never looks inside a stored value. It hands each connector the
    rows that connector wrote and takes back a list it can render — CLAUDE.md
    rule 5, applied to reading: adding a source must not mean editing this file,
    and it does not, because a source that keeps no position answers with
    nothing and is left off the screen.
    """
    states = SourceStateRepository(session)
    described: list[SourceCrawl] = []
    for source in all_sources():
        stored = await states.all_for(source.slug)
        if not stored:
            continue
        positions = source.describe_position(stored)
        if positions:
            described.append(SourceCrawl(slug=source.slug, positions=positions))
    return described


def _run_state(run: PipelineRun) -> SourceRunState:
    """One source's last run, with the one outcome that needs its own word.

    ``partial`` covers both "hh decided we are a robot" and "this connector is
    half broken", and the two need opposite reactions — reschedule, or go and
    read a module. The runner already separates them where it catches them, by
    recording the challenge under a stage of its own; this carries the
    distinction to the screen rather than making a template re-derive it from an
    error message.
    """
    errors = [RunError.model_validate(entry) for entry in run.errors if isinstance(entry, dict)]
    return SourceRunState(
        slug=run.source_slug,
        run_id=run.id,
        status=run.status,
        started_at=run.started_at,
        finished_at=run.finished_at,
        found=run.found,
        new=run.new,
        updated=run.updated,
        errors=errors,
        stopped_by_robot_check=any(error.stage == CHALLENGE_STAGE for error in errors),
    )
