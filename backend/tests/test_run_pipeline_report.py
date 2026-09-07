"""The crawl report renders. That is the whole claim, and it is worth a test.

Nothing under ``scripts/`` was tested, and a line added to this report in commit
``4aa5588`` referred to ``EmbeddingOutcome.backlog_known`` — a field that does not
exist. The suite stayed green, because no test imports the script; the failure
surfaced on a live crawl, in the traceback that replaced the summary of a run
that had just spent sixty seconds against hh and been stopped by a captcha. The
counters were computed and then thrown away by the code that prints them.

So this is deliberately shallow: it renders the report over outcomes built from
the real dataclasses and asserts it does not raise. Its value is not in what it
asserts but in *when* it fails — at import and attribute-access time, in CI,
rather than after a crawl.
"""

import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest

from app.pipeline.embedding import EmbeddingOutcome
from app.pipeline.runner import RunReport, SourceOutcome
from app.sources.base import SourceUnavailable, Unavailable
from app.sources.query_planner import QueryPlan

pytestmark = pytest.mark.unit

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "run_pipeline.py"


def _script() -> ModuleType:
    """``scripts/`` is not on the path for the suite, so load it by path.

    Importing it rather than shelling out is the point: the defect this file
    exists for was an attribute that does not exist, and only import-and-call
    finds that.
    """
    spec = importlib.util.spec_from_file_location("run_pipeline_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


show = _script().show


def _plan() -> QueryPlan:
    """An empty plan; the report only reads its groups and counts its queries."""
    return QueryPlan(groups=(), queries=())


def _report(embedding: EmbeddingOutcome | None, **kwargs: object) -> RunReport:
    """A finished run, shaped the way ``run_pipeline`` hands one to ``show``."""
    return RunReport(
        plan=_plan(),
        sources=[
            SourceOutcome(slug="hh", found=49, new=49, requests=61, duration_seconds=60.0),
            SourceOutcome(
                slug="jsearch",
                skipped=Unavailable(code=SourceUnavailable.MISSING_CREDENTIALS, detail="нет ключа"),
            ),
        ],
        embedding=embedding,
        started_at=datetime.now(UTC),
        duration_seconds=60.3,
        **kwargs,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    "embedding",
    [
        None,
        EmbeddingOutcome(considered=49, unchanged=0, embedded=49),
        EmbeddingOutcome(considered=200, unchanged=180, embedded=20, backlog=11_000),
        EmbeddingOutcome(considered=0, unchanged=0, embedded=0, skipped_reason="dry_run"),
    ],
    ids=["no-step", "drained", "large-backlog", "skipped"],
)
def test_the_report_renders_over_every_shape_the_step_can_return(
    embedding: EmbeddingOutcome | None, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every branch of the embedding section, including the one that crashed."""
    show(_report(embedding), {})

    printed = capsys.readouterr().out
    assert "ЭМБЕДДИНГИ" in printed
    assert "hh" in printed


def test_the_whole_report_stays_inside_cp1251(capsys: pytest.CaptureFixture[str]) -> None:
    """It is printed to a Russian Windows console, which cannot encode more.

    The report's rule was ``U+2500`` until commit ``4aa5588``, so printing it at
    all raised ``UnicodeEncodeError``. Same class of defect as the missing field,
    same reason it went unseen.
    """
    show(_report(EmbeddingOutcome(considered=49, unchanged=0, embedded=49, backlog=11_000)), {})

    capsys.readouterr().out.encode("cp1251")
