"""The scripts render their reports, and stay inside cp1251.

Same reasoning as ``test_run_pipeline_report.py``, which exists because a line
added to the crawl report referred to a field that did not exist: the suite
stayed green and the failure surfaced as a traceback replacing the summary of a
run that had just spent sixty seconds against hh. Nothing under ``scripts/`` is
imported by the suite unless a file like this one imports it.

Deliberately shallow. The value is not in what it asserts but in *when* it
fails — at import and attribute-access time, in CI, rather than after a scoring
pass or a backfill has already done its work.

The console is Russian Windows, so ``cp1251`` is the real constraint: one box
character in a report kills the program at the end of the run it was reporting
on. All three scripts use ASCII rules for exactly that reason, and this is
what holds them to it.
"""

import importlib.util
import re
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import pytest

from app.core.config import settings
from app.letters.channel import SourceScope
from app.letters.service import LetterOutcome
from app.matching.rules import UnstatedRequirement
from app.matching.scorer import ScoringOutcome
from app.normalize.description import skills_in_text
from app.normalize.sync import SyncOutcome
from app.sources.hh_probe import (
    CatalogFile,
    CatalogIndex,
    CatalogPage,
    CrawlPlan,
    PlannedRole,
    ProbeReport,
    Role,
    RoleDirectory,
    StateKey,
)

pytestmark = pytest.mark.unit

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _script(name: str) -> ModuleType:
    """``scripts/`` is not on the path for the suite, so load it by path.

    Importing rather than shelling out is the point: an attribute that does not
    exist is only found by import-and-call.
    """
    spec = importlib.util.spec_from_file_location(f"{name}_under_test", SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


matching = _script("run_matching")
backfill = _script("backfill_skills")
hh_roles = _script("probe_hh_roles")


def _scored() -> ScoringOutcome:
    """The shape a real pass returns, with the numbers the live run produced."""
    return ScoringOutcome(
        considered=643,
        written=582,
        skipped_seeds=61,
        filtered=44,
        without_embedding=128,
        without_skills=388,
        buckets={"skip": 538, "filtered": 44},
    )


@pytest.mark.parametrize(
    ("outcome", "note", "dry_run"),
    [
        (_scored(), None, False),
        (_scored(), "эмбеддинг профиля посчитан впервые", False),
        (_scored(), "эмбеддинг профиля не посчитан: нечего эмбеддить", False),
        (ScoringOutcome(), None, True),
    ],
    ids=["plain", "embedded", "embedding-failed", "empty-dry-run"],
)
def test_the_scoring_report_renders_over_every_shape(
    outcome: ScoringOutcome, note: str | None, dry_run: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    """Including the empty one: a pass that scored nothing still has to print."""
    matching.show(outcome, note, dry_run=dry_run)

    printed = capsys.readouterr().out
    assert "БАКЕТЫ" in printed
    printed.encode("cp1251")


def test_the_scoring_report_names_every_bucket_even_at_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A bucket nobody landed in is a fact, and an absent line reads as an error.

    The live pass put every real vacancy in ``skip`` or ``filtered``. Printing
    only the non-empty buckets would have left a report that looks truncated
    rather than one that says four apply_now became zero.
    """
    matching.show(ScoringOutcome(buckets={"skip": 3}), None, dry_run=False)

    printed = capsys.readouterr().out
    for _, label in matching.BUCKET_ORDER:
        assert label in printed


@pytest.mark.parametrize("rule", list(UnstatedRequirement))
def test_the_scoring_report_names_the_rule_for_the_unwritten_requirement(
    rule: UnstatedRequirement, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two passes under different rules print the same shape of report.

    Without the rule on the page, a reader comparing their bucket counts would
    be comparing two numbers without knowing what separates them.
    """
    matching.show(ScoringOutcome(unstated=rule), None, dry_run=True)

    printed = capsys.readouterr().out
    assert rule.value in printed
    printed.encode("cp1251")


def test_the_backfill_report_renders_and_stays_inside_cp1251(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The numbers here are the ones the live backfill produced."""
    backfill.show(
        SyncOutcome(considered=643, skills_written=1276, years_written=277, without_skills=449),
        194,
        337,
        dry_run=False,
    )

    printed = capsys.readouterr().out
    assert "1276" in printed
    printed.encode("cp1251")


def test_the_backfill_report_separates_the_two_kinds_of_requirement(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The split is the measurement, so it is printed whatever it says.

    A report that only mentioned the descriptions when they helped would make
    "they added nothing" invisible — and that is a result about the corpus
    worth reading, not a failure worth hiding.

    The counters that were measured on the live corpus on 9 September 2026 carry
    their real values: 1958 vacancies, 893 with an empty field, 450 of those
    rescued by their own text, 452 still without a skill, and six mentions in
    the whole corpus that a sentence denied. The rest are plausible fillers.
    """
    backfill.show(
        SyncOutcome(
            considered=1958,
            skills_written=2000,
            without_skills=452,
            without_field_skills=893,
            from_field=1200,
            from_text=800,
            rescued_by_text=450,
            optional_from_text=40,
            negated_in_text=6,
        ),
        1658,
        337,
        dry_run=True,
    )

    printed = capsys.readouterr().out
    assert "893" in printed
    assert "450" in printed
    assert re.search(r"с отрицанием\s+6", printed)
    printed.encode("cp1251")


def test_the_backfill_report_shows_what_a_description_gave_and_what_it_did_not(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--examples`` is the half of the report an argument can be built from.

    Extraction that only reports what it found is a report that cannot be
    argued with. The misses are the case for adding a spelling to the
    dictionary — or for leaving it out.
    """
    found = skills_in_text("Требования: Python и PostgreSQL.\nЗнание 1С обязательно.")
    backfill.show(
        SyncOutcome(considered=1),
        1,
        0,
        dry_run=True,
        examples=[
            backfill.Example(title="Backend-разработчик", found=found, missed=("Знание 1С",)),
            backfill.Example(title="Водитель", found=skills_in_text("График 5/2"), missed=()),
        ],
    )

    printed = capsys.readouterr().out
    assert "python" in printed
    assert "ничего не найдено" in printed
    assert "мимо" in printed
    printed.encode("cp1251")


def test_both_scripts_report_a_dry_run_as_a_dry_run(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A rehearsal that reads like a real run is worse than no rehearsal.

    Both scripts compute everything and roll back, so the counters look
    identical to a real pass. The only thing separating them is this line.
    """
    matching.show(_scored(), None, dry_run=True)
    backfill.show(SyncOutcome(considered=1), 0, 0, dry_run=True)

    printed = capsys.readouterr().out
    assert printed.count("--dry-run") == 2


# -- the hh catalogue probe --------------------------------------------


def _probe(page: CatalogPage | None) -> ProbeReport:
    """A measurement of the shape the probe returns, with that page in it."""
    return ProbeReport(
        host="almaty.hh.kz",
        index=CatalogIndex(
            host="almaty.hh.kz",
            files=(
                CatalogFile(
                    name="vacancies0",
                    url="https://almaty.hh.kz/sitemap/vacancies0.xml",
                    body_bytes=878_592,
                    locs=5716,
                    slugs=5716,
                    with_lastmod=0,
                ),
            ),
            slugs=("python-razrabotchik", "buhgalter"),
            matched=("python-razrabotchik",),
        ),
        page=page,
        roles=RoleDirectory(
            total=4,
            matched=(Role(id="96", name="Программист, разработчик", category="ИТ"),),
            advertised=Role(id="96", name="Программист, разработчик", category="ИТ"),
        ),
        notes=("справочник не прочитан: 403",),
    )


def _page(ids: int) -> CatalogPage:
    """A catalogue page carrying that many vacancy ids."""
    return CatalogPage(
        url="https://almaty.hh.kz/vacancies/python-razrabotchik",
        body_bytes=245_760,
        has_state=True,
        state_parsed=True,
        state_keys=(StateKey(name="vacancySearchResult", kind="dict", size=12),),
        ids_in_document=tuple(str(number) for number in range(ids)),
        ids_in_state=tuple(str(number) for number in range(ids)),
        links_without_query=("/vacancies/python-razrabotchik/2",),
        links_with_query=("/vacancies/python-razrabotchik?page=3",),
    )


@pytest.mark.parametrize(
    "page",
    [_page(50), _page(0), None],
    ids=["a-listing", "nothing-on-it", "unreadable"],
)
def test_the_probe_report_renders_over_every_outcome(
    page: CatalogPage | None, capsys: pytest.CaptureFixture[str]
) -> None:
    """Including the two that say no. The negative result is half the fork."""
    hh_roles.show(_probe(page))

    printed = capsys.readouterr().out
    assert "ЭТАП 0" in printed
    assert "ВЫВОД" in printed
    printed.encode("cp1251")


def test_the_verdict_states_the_rule_it_applied() -> None:
    """A verdict without its threshold beside it is an opinion.

    Three answers rather than two, and the middle one is the one that was
    learned the hard way: the first run of this probe read a rare profession,
    found almost nothing on it, and reported the catalogue as a dead end. Both
    of the answers that are not "this is a listing" now send the reader back to
    ``programmist`` before concluding anything.
    """
    listing = hh_roles.verdict(_page(hh_roles.IDS_FOR_A_LIST))
    empty = hh_roles.verdict(_page(0))
    unclear = hh_roles.verdict(_page(1))

    assert str(hh_roles.IDS_FOR_A_LIST) in listing
    assert "работает" in listing
    assert "programmist" in empty and "programmist" in unclear


def test_the_plan_section_renders_and_names_a_role_that_found_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The finding the section exists for has to be legible, not counted.

    A role the transliteration failed to match against any slug is the one
    outcome a person must act on, and a zero in a column is not how anybody
    notices one.
    """
    hh_roles.show_plan(
        CrawlPlan(
            keywords=("python", "docker"),
            headline="Python Developer — Backend / AI-интеграции",
            intent=("ai", "backend", "developer", "integracii", "python"),
            families=("backend", "devops", "qa"),
            focus=("backend",),
            roles=(
                PlannedRole(id=96, name="Программист, разработчик", slugs=("programmist",)),
                PlannedRole(id=165, name="Дата-сайентист", slugs=()),
            ),
            by_keyword=("junior-python-developer",),
            order=("python-razrabotchik", "programmist", "junior-python-developer"),
            total=3,
        )
    )

    printed = capsys.readouterr().out
    assert "НИ ОДНОГО" in printed
    assert "hh_roles.yaml" in printed
    # The order the run would open them in, which is the check the whole
    # section is for, and the two weights that produced it.
    assert "python-razrabotchik" in printed
    assert "слова намерения" in printed
    assert "из них названы headline" in printed
    printed.encode("cp1251")


def test_the_plan_section_says_when_there_was_no_profile_to_plan_for(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An empty section that looks like a result is worse than an absent one."""
    hh_roles.show_plan(None)

    printed = capsys.readouterr().out
    assert "--keyword" in printed and "ЗАМЕЧАНИЯ" in printed


def test_the_letters_report_counts_the_agents_side_and_the_rest_apart() -> None:
    """«для hh 0 из 1» is the line that says the agent queue stays empty tonight."""
    script = _script("generate_letters")

    def outcome(*, saved: bool, via_agent: bool | None, slug: str | None) -> LetterOutcome:
        return LetterOutcome(
            vacancy_id=uuid4(),
            title="t",
            company=None,
            saved=saved,
            via_agent=via_agent,
            source_slug=slug,
        )

    lines = script.split_by_channel(
        [
            outcome(saved=False, via_agent=True, slug=settings.agent_source_slug),
            outcome(saved=True, via_agent=False, slug="arbeitnow"),
            outcome(saved=True, via_agent=False, slug="arbeitnow"),
            outcome(saved=True, via_agent=False, slug="remotive"),
            outcome(saved=True, via_agent=None, slug=None),
        ]
    )

    assert lines[0].startswith(f"для {settings.agent_source_slug} ")
    assert lines[0].endswith("0 из 1")
    assert lines[1].endswith("3 из 3: arbeitnow 2, remotive 1")
    assert lines[2].endswith("1 из 1")
    for line in lines:
        line.encode("cp1251")


def test_the_letters_source_option_is_a_scope_or_one_source() -> None:
    """``--source remotive`` names a source; ``--source others`` names a side."""
    script = _script("generate_letters")

    assert script.parse_source("all") == (SourceScope.ALL, None)
    assert script.parse_source("others") == (SourceScope.OTHERS, None)
    assert script.parse_source("remotive") == (SourceScope.ALL, "remotive")
