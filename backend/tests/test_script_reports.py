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
from pathlib import Path
from types import ModuleType

import pytest

from app.matching.scorer import ScoringOutcome
from app.normalize.sync import SyncOutcome
from app.sources.hh_probe import (
    CatalogFile,
    CatalogIndex,
    CatalogPage,
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

    Three answers rather than two, because "two vacancy links on the page" is
    neither a listing nor an empty page, and rounding it into either is the
    guess the whole probe exists to replace.
    """
    listing = hh_roles.verdict(_page(hh_roles.IDS_FOR_A_LIST))
    empty = hh_roles.verdict(_page(0))
    unclear = hh_roles.verdict(_page(1))

    assert str(hh_roles.IDS_FOR_A_LIST) in listing
    assert "реализуемым" in listing
    assert "запасной путь" in empty and "НЕ экономит" in empty
    assert "Нужен человек" in unclear
