"""The archive flag under hh's nested ``vacancyView.vacancyFull.vacancy`` (7 Oct 2026).

Measured on the live archived posting 136105998. Before this read, the nested
shape left ``status`` out of reach, and an absent flag is deliberately "not
archived" — so every archived vacancy passed the prefilter as open.
"""

from agent.prefilter import read_status


def nested(inner: dict[str, object]) -> dict[str, object]:
    return {"vacancyView": {"area": {}, "vacancyFull": {"vacancy": inner}}}


def test_an_archived_posting_in_the_nested_shape_reads_as_archived() -> None:
    status = read_status(nested({"status": {"archived": True}, "closedForApplicants": False}))
    assert status.archived is True


def test_closed_for_applicants_is_read_from_the_nested_posting() -> None:
    status = read_status(nested({"status": {"archived": False}, "closedForApplicants": True}))
    assert status.closed_for_applicants is True


def test_the_flat_shape_still_reads_as_before() -> None:
    status = read_status({"vacancyView": {"status": {"archived": True}}})
    assert status.archived is True
