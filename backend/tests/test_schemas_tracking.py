"""Application and pipeline-run contracts.

Small models, but they are the ones the kanban and the sources page read, and
nothing else in the suite touches them.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.db.enums import ApplicationStatus, PipelineRunStatus
from app.schemas.application import (
    ApplicationCreate,
    ApplicationRead,
    ApplicationStats,
    ApplicationUpdate,
)
from app.schemas.pipeline import PipelineRunCreate, PipelineRunFinish, PipelineRunRead

STARTED = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)


def test_application_defaults_to_saved() -> None:
    """Tracking a vacancy is not the same as having applied to it; the default
    must never imply an action the user did not take."""
    created = ApplicationCreate(vacancy_id=uuid4())

    assert created.status is ApplicationStatus.SAVED
    assert created.applied_at is None


def test_application_update_distinguishes_unset_from_null() -> None:
    """The kanban patches one field at a time. Anything not sent must stay out
    of the dump, or the repository would null the rest of the row."""
    moved = ApplicationUpdate(status=ApplicationStatus.INTERVIEW)

    assert moved.model_dump(exclude_unset=True) == {"status": ApplicationStatus.INTERVIEW}
    assert ApplicationUpdate(notes=None).model_dump(exclude_unset=True) == {"notes": None}


def test_application_read_is_built_from_orm_attributes() -> None:
    """The router hands an ORM row straight to the response model."""

    class Row:
        id = uuid4()
        vacancy_id = uuid4()
        status = ApplicationStatus.OFFER
        applied_at = STARTED
        notes = "referred by a friend"
        cover_letter = None
        created_at = STARTED
        updated_at = STARTED

    read = ApplicationRead.model_validate(Row())

    assert read.status is ApplicationStatus.OFFER
    assert read.notes == "referred by a friend"


def test_application_stats_start_empty() -> None:
    """An empty tracker renders as zero, not as a missing section."""
    stats = ApplicationStats()

    assert stats.total == 0
    assert stats.by_status == {}


def test_pipeline_run_starts_in_running_state() -> None:
    """A run record is opened before the fetch, so its initial status has to say
    'in progress' rather than claiming an outcome."""
    assert PipelineRunCreate(source_slug="hh").status is PipelineRunStatus.RUNNING


@pytest.mark.parametrize("field", ["found", "new", "updated"])
def test_pipeline_counters_cannot_be_negative(field: str) -> None:
    """A negative counter means the arithmetic upstream is wrong; surfacing it
    here beats storing it and puzzling over the sources page later."""
    with pytest.raises(ValidationError):
        PipelineRunFinish(status=PipelineRunStatus.SUCCESS, **{field: -1})


def test_pipeline_finish_defaults_to_no_errors() -> None:
    """A successful run must not carry a phantom error entry."""
    finished = PipelineRunFinish(status=PipelineRunStatus.SUCCESS, found=10, new=4, updated=6)

    assert finished.errors == []


def _run(finished_at: datetime | None) -> PipelineRunRead:
    """A read model with everything but the end time fixed."""
    return PipelineRunRead(
        id=uuid4(),
        source_slug="hh",
        status=PipelineRunStatus.SUCCESS,
        started_at=STARTED,
        finished_at=finished_at,
        found=10,
        new=4,
        updated=6,
        errors=[],
    )


def test_duration_is_none_while_the_run_is_open() -> None:
    """The sources page shows a spinner for an open run; a zero duration would
    read as 'finished instantly'."""
    assert _run(None).duration_seconds is None


def test_duration_is_measured_from_start_to_finish() -> None:
    """Run length is what tells you a source has started timing out."""
    assert _run(STARTED + timedelta(seconds=90)).duration_seconds == 90.0
